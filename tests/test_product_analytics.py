"""Hub product-usage analytics: ingestion, authorization, validation,
deduplication, rate limiting, filtering, export and privacy-safe metadata."""

from __future__ import annotations

import asyncio
import csv
import logging
import io
import json
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_v2.database import get_db_v2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.hub import HubV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.product_analytics import HubProductEngagementV2, HubProductEventV2
from app.db_v2.models.tab import TabV2
from app.db_v2.models.thread import ThreadV2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.routers import bot_management, product_analytics
from app.services import access_visibility_service
from app.services import product_analytics_service as service
from app.services.resource_grant_service import GrantMatch
from app.services.rate_limit_service import RATE_LIMITS, RateLimitService

ADMIN = CurrentHubUser(info=SimpleNamespace(email="admin@renphil.org", roles=[]), hub_user_id=1, email="admin@renphil.org")
MEMBER = CurrentHubUser(info=SimpleNamespace(email="member@renphil.org", roles=[]), hub_user_id=2, email="member@renphil.org")
HUB_ADMIN_ROLE_ID = 10
ALL_SCOPES_ID = 20
# Stands in for any admin- or user-authored title (tab, link, card, document,
# thread). It must never be persisted or returned anywhere.
SENSITIVE_TITLE = "Jane Doe performance review"

HUB_TABLES = [
    HubV2.__table__, NavTabV2.__table__, TabV2.__table__, GridstackV2.__table__, PageContentV2.__table__,
    ComponentV2.__table__, ThreadV2.__table__, HubProductEventV2.__table__, HubProductEngagementV2.__table__,
]


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):  # pragma: no cover - test plumbing
    return "JSON"


def seed_hub(session) -> dict:
    """A small Hub whose titles look like personal data."""
    now = datetime.now(timezone.utc)
    hub = HubV2(id=1)
    session.add(hub)
    nav = NavTabV2(document_id="nav-doc-1", slug="jane-doe-review-hub", title="Jane Doe Review Hub")
    session.add(nav)
    session.flush()
    root = TabV2(document_id="root-doc-1", title="Jane Doe Salary", nav_tab_id=nav.id)
    session.add(root)
    session.flush()
    child = TabV2(document_id="child-doc-1", title="Jane Doe Medical Leave", parent_tab_id=root.id)
    session.add(child)
    grid = GridstackV2(parent_tab_id=root.id)
    session.add(grid)
    session.flush()
    cards_content = PageContentV2(content={"cards": [{"id": "card-a", "values": {}}], "sections": []})
    session.add(cards_content)
    session.flush()

    def component(link, ctype, title, **extra):
        c = ComponentV2(link=link, type=ctype, title=title, gridstack_id=grid.id, **extra)
        session.add(c)
        session.flush()
        return c

    component("cmp-links", "links", "Jane Doe links")
    thread_widget = component("cmp-thread", "thread", "Jane Doe threads")
    other_thread_widget = component("cmp-thread-2", "thread", "Other threads")
    component("cmp-cards", "detail_cards", "Jane Doe cards", page_content_id=cards_content.id)
    sbn = component("cmp-sbn", "super_block_note", "Jane Doe notebook")
    component("cmp-sbn-child", "block_note", "Jane Doe private page", super_blocknote_id=sbn.id)
    component("cmp-sbn-secret", "block_note", "Jane Doe sealed page", super_blocknote_id=sbn.id)
    component("cmp-button", "button", "Jane Doe button")
    component("cmp-mirror", "mirror", "Mirror of cards", props={"target_link": "cmp-cards"})
    session.add(ThreadV2(id=7, component_id=thread_widget.id, title="Jane Doe private thread", content="x",
                         author_email="a@renphil.org", status="approved", created_at=now))
    session.add(ThreadV2(id=8, component_id=other_thread_widget.id, title="Other thread", content="x",
                         author_email="a@renphil.org", status="approved", created_at=now))
    # A second, unrelated nav tab with its own tab.
    secret_nav = NavTabV2(document_id="nav-doc-2", slug="board-pay", title="Board Pay Review")
    session.add(secret_nav)
    session.flush()
    session.add(TabV2(document_id="root-doc-2", title="Board Pay Detail", nav_tab_id=secret_nav.id))
    session.commit()
    return {"nav": nav, "root": root, "child": child}


def node(session, link: str) -> tuple[str, int]:
    return ("component", session.query(ComponentV2.id).filter(ComponentV2.link == link).scalar())


def grant(kind: str, node_id: int, level: str = "view") -> GrantMatch:
    return GrantMatch(grant_id=0, node_kind=kind, node_id=node_id, level=level)


def _event(name: str = "root_tab.selected", **overrides) -> dict:
    event = {
        "event_id": str(uuid.uuid4()),
        "event_name": name,
        "trigger": "click",
        "page_route": "/dashboard/finance",
        "target_id": "root-doc-1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "properties": {"tab_kind": "root"},
    }
    event.update(overrides)
    return event


def _engagement(**overrides) -> dict:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    engagement = {
        "engagement_id": str(uuid.uuid4()),
        "page_route": "/finance/private-board",
        "nav_tab_id": "nav-doc-1",
        "tab_id": "root-doc-1",
        "active_seconds": 3,
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(seconds=3)).isoformat(),
    }
    engagement.update(overrides)
    return engagement


class FakeLimiter:
    """Stands in for RateLimitService (the shared Upstash limiter)."""

    def __init__(self) -> None:
        self.retry_after: int | None = None
        self.checks: list[tuple[str, str]] = []
        self.consumed: list[tuple[str, str]] = []

    async def check(self, action: str, email: str) -> int | None:
        self.checks.append((action, email))
        return self.retry_after

    async def consume(self, action: str, email: str) -> None:
        self.consumed.append((action, email))


class ProductAnalyticsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        from app.db_v2.database import BaseV2

        BaseV2.metadata.create_all(bind=self.engine, tables=HUB_TABLES)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.Session() as session:
            seed_hub(session)
        self.current = MEMBER
        self.limiter = FakeLimiter()
        self.environment = "sandbox"
        # Live access: the real node tree and fold run over the seeded Hub;
        # only the per-user grant lookup and the Hub Admin answer are faked.
        # By default the member holds a view grant on the hub root (sees
        # everything); access tests narrow it.
        self.grants: dict[int, list[GrantMatch]] = {MEMBER.hub_user_id: [grant("hub", 1)]}
        self.hub_admins = {ADMIN.hub_user_id}
        self.access_calls = 0

        app = FastAPI()
        app.include_router(product_analytics.router)

        def db_override():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        self.request_sessions = 0

        def counted_db_override():
            self.request_sessions += 1
            yield from db_override()

        app.dependency_overrides[get_db_v2] = counted_db_override
        app.dependency_overrides[get_current_hub_user] = lambda: self.current
        self.client = TestClient(app)
        self.app = app
        # Ingestion opens its own session in the worker thread; record each one.
        self.worker_sessions: list[dict] = []
        self.access_seen: list[tuple[int, object]] = []

        # The real All-Scopes gate runs; only its RBAC graph lookups are faked.
        self._patches = [
            patch.object(bot_management, "hub_admin_target_ids", lambda _db: (HUB_ADMIN_ROLE_ID, [ALL_SCOPES_ID])),
            patch.object(
                bot_management,
                "effective_pairs",
                lambda _db, user_id: {(HUB_ADMIN_ROLE_ID, ALL_SCOPES_ID)} if user_id == ADMIN.hub_user_id else set(),
            ),
            patch.object(product_analytics, "get_settings", lambda: SimpleNamespace(hub_environment=self.environment)),
            patch.object(product_analytics, "get_rate_limit_service", lambda: self.limiter),
            patch.object(product_analytics, "RbacClosures", self._closures),
            patch.object(product_analytics, "SessionLocalV2", self._worker_session),
            patch.object(product_analytics, "is_hub_admin", self._is_hub_admin),
            patch.object(access_visibility_service, "matching_grants",
                         lambda _db, user_id, closures=None: list(self.grants.get(user_id, []))),
        ]
        for p in self._patches:
            p.start()

    def _closures(self, _db):
        self.access_calls += 1
        return object()

    def _is_hub_admin(self, db, current, closures=None) -> bool:
        self.access_seen.append((threading.get_ident(), db))
        return current.hub_user_id in self.hub_admins

    def _worker_session(self):
        db = self.Session()
        record = {"session": db, "thread": threading.get_ident(), "closed_in": None, "rolled_back": False}
        close, rollback = db.close, db.rollback

        def tracked_close():
            record["closed_in"] = threading.get_ident()
            close()

        def tracked_rollback():
            record["rolled_back"] = True
            rollback()

        db.close, db.rollback = tracked_close, tracked_rollback
        self.worker_sessions.append(record)
        return db

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def _ingest(self, *events: dict, query: str = ""):
        return self.client.post(f"/v2/product-analytics/events{query}", json={"events": list(events)})

    def _ingest_engagements(self, *engagements: dict, query: str = ""):
        return self.client.post(f"/v2/product-analytics/engagements{query}", json={"engagements": list(engagements)})

    def _rows(self) -> list[HubProductEventV2]:
        with self.Session() as db:
            return db.query(HubProductEventV2).order_by(HubProductEventV2.id).all()

    def _engagement_rows(self) -> list[HubProductEngagementV2]:
        with self.Session() as db:
            return db.query(HubProductEngagementV2).order_by(HubProductEngagementV2.id).all()

    # ── Ingestion ────────────────────────────────────────────────────────

    def test_ingest_attributes_user_and_environment_server_side(self) -> None:
        response = self._ingest(_event(actor_email="spoof@evil.test", environment="production", hub_user_id=999))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": 1, "duplicates": 0, "rejected": []})
        (row,) = self._rows()
        self.assertEqual(row.actor_email, "member@renphil.org")
        self.assertEqual(row.hub_user_id, 2)
        self.assertEqual(row.environment, "sandbox")
        self.assertEqual(row.component_type, "root_tab")
        self.assertEqual(row.action, "select")
        # A route template: "finance" is a tab title slug and is never stored.
        self.assertEqual(row.page_route, "/dashboard/:tab")

    def test_standard_view_as_keeps_real_actor_and_hides_verified_context(self) -> None:
        """A simulated role/scope is diagnostic context, never impersonation.

        The browser may request View As, but only the server-verified keys are
        persisted.  The event remains owned by the real authenticated user
        and public analytics responses deliberately expose neither key.
        """
        target = SimpleNamespace(role_key="program_member", scope_key="test_scope")
        with patch.object(product_analytics.view_as_service, "resolve_user_id", return_value=MEMBER.hub_user_id), \
                patch.object(product_analytics.view_as_service, "verify_view_as", return_value=target):
            response = self._ingest(
                _event(),
                query="?view_as_role=program_member&view_as_scope=test_scope",
            )
        self.assertEqual(response.status_code, 200)
        (row,) = self._rows()
        self.assertEqual(row.actor_email, MEMBER.email)
        self.assertEqual(row.hub_user_id, MEMBER.hub_user_id)
        self.assertEqual((row.view_as_role_key, row.view_as_scope_key), ("program_member", "test_scope"))

        self.current = ADMIN
        returned = self.client.get("/v2/product-analytics/events").json()["data"][0]
        self.assertNotIn("view_as_role_key", returned)
        self.assertNotIn("view_as_scope_key", returned)

    def test_incomplete_view_as_is_rejected_before_worker_or_write(self) -> None:
        response = self._ingest(_event(), query="?view_as_role=program_member")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "view_as_incomplete")
        self.assertEqual(self.worker_sessions, [])
        self.assertEqual(self.request_sessions, 0)
        self.assertEqual(self._rows(), [])

    def test_new_view_as_selection_is_verified_and_private(self) -> None:
        targets = [SimpleNamespace(role_key="editor", scope_key="one"),
                   SimpleNamespace(role_key="member", scope_key="two")]
        selection = SimpleNamespace(targets=targets, user=SimpleNamespace(id=42))
        with patch.object(product_analytics.view_as_service, "resolve_user_id", return_value=2), \
                patch.object(product_analytics.view_as_service, "resolve_view_as_selection", return_value=selection) as verify:
            response = self._ingest(_event(), query="?view_as=editor%3Aone&view_as=member%3Atwo&view_as_user=other%40example.org")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(verify.call_args.args[2:], (["editor:one", "member:two"], "other@example.org"))
        (row,) = self._rows()
        self.assertEqual(row.actor_email, MEMBER.email)
        self.assertEqual(row.view_as_context, {"pairs": [{"role": "editor", "scope": "one"}, {"role": "member", "scope": "two"}], "user_id": 42})
        self.current = ADMIN
        self.assertNotIn("view_as_context", self.client.get("/v2/product-analytics/events").json()["data"][0])

    def test_new_view_as_refusal_writes_nothing(self) -> None:
        refusal = product_analytics.view_as_service.ViewAsPairsError(403, "view_as_refused", "not allowed", [])
        with patch.object(product_analytics.view_as_service, "resolve_user_id", return_value=2), \
                patch.object(product_analytics.view_as_service, "resolve_view_as_selection", side_effect=refusal):
            response = self._ingest(_event(), query="?view_as=editor%3Asecret")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._rows(), [])

    def test_refused_view_as_pair_writes_nothing(self) -> None:
        refusal = product_analytics.view_as_service.ViewAsError(
            403, "view_as_scope_not_allowed", "not allowed"
        )
        with patch.object(product_analytics.view_as_service, "resolve_user_id", return_value=MEMBER.hub_user_id), \
                patch.object(product_analytics.view_as_service, "verify_view_as", side_effect=refusal):
            response = self._ingest(_event(), query="?view_as_role=program_member&view_as_scope=secret")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "view_as_scope_not_allowed")
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.limiter.consumed, [])

    def test_click_event_rows_carry_only_event_table_columns(self) -> None:
        # Page bindings belong to `hub_product_engagements` only. A click
        # event that also sends them (modified or newer client) is still
        # stored; a row key with no column made every batch a 503.
        response = self._ingest(_event(nav_tab_id="nav-doc-1", tab_id="root-doc-1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], 1)
        row = service.validate_event(
            _event(nav_tab_id="nav-doc-1", tab_id="root-doc-1"), hub_user_id=2, actor_email=MEMBER.email,
            environment="sandbox", received_at=datetime.now(timezone.utc),
        )
        self.assertLessEqual(set(row), set(HubProductEventV2.__table__.columns.keys()))

    def test_engagement_records_only_bounded_active_time_and_deduplicates_retries(self) -> None:
        payload = _engagement(environment="production", actor_email="spoof@evil.test")
        response = self._ingest_engagements(payload, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": 1, "duplicates": 1, "rejected": []})
        (row,) = self._engagement_rows()
        self.assertEqual(row.actor_email, MEMBER.email)
        self.assertEqual(row.environment, "sandbox")
        self.assertEqual(row.page_route, "/:nav_tab/:tab")
        self.assertEqual(row.nav_tab_id, "nav-doc-1")
        self.assertEqual(row.tab_id, "root-doc-1")
        self.assertEqual(row.active_seconds, 3)

    def test_anonymous_view_as_request_is_server_suppressed_without_a_write(self) -> None:
        """The browser normally creates no request in anonymous View As;
        this covers a manually replayed valid request as well."""
        query = "?view_as_role=program_member&view_as_scope=test_scope&anonymous=true"
        event = self._ingest(_event(), query=query)
        engagement = self._ingest_engagements(_engagement(), query=query)
        self.assertEqual(event.json(), {"accepted": 0, "duplicates": 0, "rejected": []})
        self.assertEqual(engagement.json(), {"accepted": 0, "duplicates": 0, "rejected": []})
        self.assertEqual(self._rows(), [])
        self.assertEqual(self._engagement_rows(), [])
        self.assertEqual(self.worker_sessions, [])
        self.assertEqual(self.limiter.consumed, [])

    def test_engagement_rejects_background_sized_or_inconsistent_intervals(self) -> None:
        payload = _engagement(active_seconds=3)
        payload["ended_at"] = (datetime.fromisoformat(payload["started_at"]) + timedelta(minutes=5)).isoformat()
        response = self._ingest_engagements(payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], 0)
        self.assertEqual(len(response.json()["rejected"]), 1)
        self.assertEqual(self._engagement_rows(), [])

    def test_summary_reports_active_page_time_under_the_same_filters(self) -> None:
        self.assertEqual(self._ingest_engagements(_engagement()).status_code, 200)
        self.current = ADMIN
        response = self.client.get("/v2/product-analytics/summary?environment=sandbox")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["engagement"], {
            "active_seconds": 3,
            "intervals": 1,
            "by_page": [{"key": "/:nav_tab/:tab", "active_seconds": 3}],
            "applied_filters": ["environment"],
            "click_only_filters": [],
        })

    def test_authenticated_read_and_export_responses_are_never_cacheable(self) -> None:
        self.current = ADMIN
        responses = [
            self.client.get("/v2/product-analytics/catalog"),
            self.client.get("/v2/product-analytics/summary"),
            self.client.get("/v2/product-analytics/events"),
            self.client.get("/v2/product-analytics/export"),
            self.client.get("/v2/product-analytics/export/xlsx"),
        ]
        for response in responses:
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertEqual(response.headers.get("vary"), "Authorization")

    def test_router_refusals_are_never_cacheable_either(self) -> None:
        """403, 404, 422 and 503 answers are as private as a 200: each one
        says something about a specific caller's access or data."""
        down = OperationalError("SELECT", {}, Exception("database unavailable"))
        self.current = ADMIN
        self._ingest(_event(target_id="root-doc-1"), _event(target_id="root-doc-1"))
        responses = {}
        with patch.object(service, "summarize_events", side_effect=down):
            responses["summary 503"] = self.client.get("/v2/product-analytics/summary")
        with patch.object(service, "list_events_for_page_scope", side_effect=down):
            responses["insights 503"] = self.client.get("/v2/product-analytics/pages/root-doc-1/insights")
        with patch.object(service, "MAX_EXPORT_ROWS", 1):
            responses["insights 422"] = self.client.get("/v2/product-analytics/pages/root-doc-1/insights")
            self._edit_root_tab()
            responses["summary 422"] = self.client.get("/v2/product-analytics/summary")
        responses["insights 404"] = self.client.get("/v2/product-analytics/pages/root-doc-2/insights")
        responses["people export 403"] = self.client.get("/v2/product-analytics/export/xlsx?include_people=true")
        self.grants = {MEMBER.hub_user_id: [grant("hub", 1, "view")]}
        responses["reader gate 403"] = self.client.get("/v2/product-analytics/summary")
        for name, response in responses.items():
            with self.subTest(name):
                self.assertEqual(str(response.status_code), name.split()[-1])
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.headers.get("vary"), "Authorization")

    def test_summary_keeps_click_analytics_available_before_engagement_table_rollout(self) -> None:
        """The additive active-time table must not break pre-existing click reports."""
        missing_table = ProgrammingError(
            "SELECT", {}, Exception('relation "hub_product_engagements" does not exist')
        )
        db = Mock()
        db.execute.side_effect = missing_table

        self.assertEqual(
            service.summarize_engagement(db, service.EventFilters()),
            {"active_seconds": 0, "intervals": 0, "by_page": []},
        )
        db.rollback.assert_called_once()

    def test_editor_reads_only_currently_editable_resources_and_never_global_counts(self) -> None:
        with self.Session() as db:
            editable = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
        self.current = ADMIN
        self.assertEqual(self._ingest(_event(target_id="root-doc-1"), _event(target_id="root-doc-2")).json()["accepted"], 2)
        self.assertEqual(self._ingest_engagements(_engagement()).json()["accepted"], 1)
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(summary["totals"]["events"], 1)
        self.assertEqual(summary["by_page"][0]["count"], 1)
        self.assertEqual(summary["engagement"]["active_seconds"], 3)
        self.assertEqual(summary["engagement"]["intervals"], 1)
        events = self.client.get("/v2/product-analytics/events").json()
        self.assertEqual(events["total"], 1)
        self.assertEqual(events["data"][0]["target_id"], "root-doc-1")
        # CSV is legacy and carries person-level fields. Scoped editors use
        # the privacy-safe default Excel export instead; this endpoint must
        # not expose visitors' identities.
        self.assertEqual(self.client.get("/v2/product-analytics/export").status_code, 403)

    def test_editor_active_time_is_limited_to_currently_editable_bound_tabs(self) -> None:
        """An editor receives time for their tab, never another tab or old route-only rows."""
        self.current = ADMIN
        own = _engagement(tab_id="root-doc-1")
        other = _engagement(tab_id="root-doc-2")
        legacy = _engagement(nav_tab_id=None, tab_id=None)
        self.assertEqual(self._ingest_engagements(own, other, legacy).json()["accepted"], 3)
        with self.Session() as db:
            editable = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}
        response = self.client.get("/v2/product-analytics/summary")
        self.assertEqual(response.status_code, 200)
        engagement = response.json()["engagement"]
        self.assertEqual((engagement["active_seconds"], engagement["intervals"], engagement["by_page"]),
                         (3, 1, [{"key": "/:nav_tab/:tab", "active_seconds": 3}]))

    def test_mismatched_navigation_and_tab_bindings_are_not_stored(self) -> None:
        """Two valid resources must still form one real navigation tree."""
        self.current = ADMIN
        response = self._ingest_engagements(_engagement(nav_tab_id="nav-doc-1", tab_id="root-doc-2"))
        self.assertEqual(response.status_code, 200)
        (row,) = self._engagement_rows()
        self.assertIsNone(row.nav_tab_id)
        self.assertIsNone(row.tab_id)

    def test_revoked_edit_grant_hides_analytics_on_the_next_read(self) -> None:
        with self.Session() as db:
            editable = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
            other = db.query(TabV2).filter(TabV2.document_id == "root-doc-2").one()
        self.current = ADMIN
        self._ingest(_event(target_id="root-doc-1"))
        self._ingest_engagements(_engagement())
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}
        self.assertEqual(self.client.get("/v2/product-analytics/events").json()["total"], 1)
        # The grant moves to another tab: still an editor, but nothing of the old tab remains.
        self.grants = {MEMBER.hub_user_id: [grant("tab", other.id, "edit")]}
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual((summary["totals"]["events"], summary["engagement"]["active_seconds"]), (0, 0))
        self.assertEqual(self.client.get("/v2/product-analytics/events").json()["total"], 0)
        # Every edit grant gone: the page itself is refused.
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "view")]}
        self.assertEqual(self.client.get("/v2/product-analytics/summary").status_code, 403)

    def test_component_only_editor_gets_component_clicks_but_no_page_time(self) -> None:
        self.current = ADMIN
        self._ingest(_event("link.clicked", component_id="cmp-links", target_id=None), _event(target_id="root-doc-1"))
        self._ingest_engagements(_engagement())
        with self.Session() as db:
            links = node(db, "cmp-links")
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant(*links, "edit")]}
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(summary["by_event"], [{"key": "link.clicked", "count": 1}])
        engagement = summary["engagement"]
        self.assertEqual((engagement["active_seconds"], engagement["intervals"], engagement["by_page"]), (0, 0, []))

    def test_page_insights_are_fixed_to_one_currently_editable_page(self) -> None:
        """The page id is live-RBAC-scoped; unrelated ids cannot widen it."""
        self.current = ADMIN
        self._ingest(
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("link.clicked", component_id="cmp-links"),
            _event("button.clicked", component_id="cmp-button"),
        )
        self._ingest_engagements(_engagement(tab_id="root-doc-1"))
        response = self.client.get("/v2/product-analytics/pages/root-doc-1/insights", params={"actor_email": "spoof@evil.test"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["page"]["id"], "root-doc-1")
        self.assertEqual(body["clicks"]["total"], 3)
        self.assertEqual(body["active_time"]["active_seconds"], 3)
        self.assertEqual(response.headers["cache-control"], "no-store")
        # The other root is both a valid id and a different page. A member
        # without current edit access receives the same generic 404 as a
        # malformed candidate, not an existence signal.
        self.current = MEMBER
        with self.Session() as db:
            editable_tab_id = db.query(TabV2.id).filter(TabV2.document_id == "root-doc-1").scalar()
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable_tab_id, "edit")]}
        allowed = self.client.get("/v2/product-analytics/pages/root-doc-1/insights")
        denied = self.client.get("/v2/product-analytics/pages/root-doc-2/insights")
        malformed = self.client.get("/v2/product-analytics/pages/not a valid id/insights")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual((denied.status_code, denied.json()), (404, {"detail": "Page insights are unavailable."}))
        self.assertEqual((malformed.status_code, malformed.json()), (404, denied.json()))

    # ── Page Insights: scale, scope and parity ───────────────────────────

    _INSIGHTS = "/v2/product-analytics/pages/{}/insights"

    def _insights(self, page_id: str, **kwargs):
        return self.client.get(self._INSIGHTS.format(page_id), **kwargs)

    def _insert_events(self, count: int, **overrides) -> None:
        now = datetime.now(timezone.utc)
        rows = [{
            "event_id": str(uuid.uuid4()), "event_name": "root_tab.selected", "component_type": "root_tab",
            "action": "select", "trigger": "click", "cascade_id": None, "hub_user_id": 2,
            "actor_email": MEMBER.email, "environment": "sandbox", "page_route": "/:nav_tab/:tab",
            "component_id": None, "target_id": "root-doc-2", "properties": {},
            "occurred_at": now, "received_at": now, **overrides,
        } for _ in range(count)]
        with self.Session() as db:
            db.execute(HubProductEventV2.__table__.insert(), rows)
            db.commit()

    def _add_variant(self, document_id: str = "variant-doc-1", widget: str = "cmp-variant-links") -> int:
        """A tab variant as `create_tab_variant_v2` stores it: a sibling TabV2
        with the parent's nav tree and its own root canvas."""
        with self.Session() as db:
            root = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
            variant = TabV2(document_id=document_id, title="Jane Doe Salary B", parent_tab_id=root.id, nav_tab_id=root.nav_tab_id)
            db.add(variant)
            db.flush()
            canvas = GridstackV2(document_id=document_id, parent_tab_id=variant.id)
            db.add(canvas)
            db.flush()
            db.add(ComponentV2(link=widget, type="links", title="Variant links", gridstack_id=canvas.id))
            db.commit()
            return variant.id

    def _assert_private_404(self, response) -> None:
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "Page insights are unavailable."}))
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("vary"), "Authorization")

    def test_small_page_insights_stay_exact_when_the_hub_exceeds_every_global_cap(self) -> None:
        """Unrelated Hub volume beyond the export and active-time caps must
        neither reach the Page Insights query nor make a small page fail."""
        self.current = ADMIN
        self._ingest(
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("link.clicked", component_id="cmp-links", properties={}),
        )
        self._ingest_engagements(_engagement(tab_id="root-doc-1"))
        self._insert_events(service.MAX_EXPORT_ROWS + 1)
        self._insert_intervals(1, tab_id="root-doc-2", nav_tab_id="nav-doc-2")
        now = datetime.now(timezone.utc)
        with self.Session() as db:
            db.execute(HubProductEngagementV2.__table__.insert(), [{
                "engagement_id": str(uuid.uuid4()), "hub_user_id": 2, "actor_email": MEMBER.email,
                "environment": "sandbox", "page_route": f"/:nav_tab/:tab/{index}", "nav_tab_id": "nav-doc-2",
                "tab_id": "root-doc-2", "active_seconds": 1, "started_at": now - timedelta(seconds=1),
                "ended_at": now, "received_at": now,
            } for index in range(service.MAX_ENGAGEMENT_GROUPS + 1)])
            db.commit()

        for reader in ("admin", "editor"):
            with self.subTest(reader=reader):
                if reader == "editor":
                    self._edit_root_tab()
                response = self._insights("root-doc-1")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["clicks"]["total"], 2)
                self.assertEqual((body["active_time"]["active_seconds"], body["active_time"]["intervals"]), (3, 1))
        # The editor's Hub-wide summary is still bounded; only the page read is exact.
        self.assertEqual(self.client.get("/v2/product-analytics/summary").status_code, 422)

    def test_page_insights_place_events_by_target_only_for_the_three_selection_events(self) -> None:
        """A non-selection target id equal to a page id is a collision, not a page."""
        self.current = ADMIN
        self._insert_events(1, target_id="root-doc-1")
        self._insert_events(1, event_name="sub_tab.selected", component_type="sub_tab", target_id="root-doc-1")
        for name, ctype in (("detail_card.details_opened", "detail_cards"), ("thread_item.opened", "thread"),
                            ("navigation_tab.selected", "navigation_tab"), ("blocknote_tab.selected", "blocknote_tab")):
            self._insert_events(1, event_name=name, component_type=ctype, target_id="root-doc-1")
        body = self._insights("root-doc-1").json()
        self.assertEqual(body["clicks"]["total"], 2)
        self.assertEqual({item["key"] for item in body["clicks"]["by_action"]}, {"root_tab.selected", "sub_tab.selected"})

    def test_page_insights_refusals_share_one_private_404(self) -> None:
        with self.Session() as db:
            root_id, nav_id = db.query(TabV2.id, TabV2.nav_tab_id).filter(TabV2.document_id == "root-doc-1").one()
            other_nav_id = db.query(NavTabV2.id).filter(NavTabV2.document_id == "nav-doc-2").scalar()
            deleted = TabV2(document_id="deleted-doc", title="Deleted", nav_tab_id=nav_id)
            db.add(deleted)
            db.flush()
            db.delete(deleted)
            db.add_all([
                TabV2(document_id="orphan-tab", title="Orphan", nav_tab_id=9999),
                TabV2(document_id="orphan-variant", title="Orphan variant", parent_tab_id=9999, nav_tab_id=nav_id),
                TabV2(document_id="cross-variant", title="Cross", parent_tab_id=root_id, nav_tab_id=other_nav_id),
            ])
            db.commit()
        variant_id = self._add_variant()
        with self.Session() as db:
            db.add(TabV2(document_id="nested-variant", title="Nested", parent_tab_id=variant_id, nav_tab_id=nav_id))
            db.commit()
        self._add_sub_tab("sub-doc-1")

        self.current = ADMIN
        for page_id in ("not a valid id", "x" * 129, "unknown-doc", "deleted-doc", "orphan-tab",
                        "orphan-variant", "cross-variant", "nested-variant", "child-doc-1", "sub-doc-1"):
            with self.subTest(reader="admin", page_id=page_id):
                self._assert_private_404(self._insights(page_id))
        self.assertEqual(self._insights("variant-doc-1").status_code, 200)

        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", root_id, "edit"), grant("nav_tab", other_nav_id, "view")]}
        for page_id in ("root-doc-2", "unknown-doc", "sub-doc-1"):
            with self.subTest(reader="editor", page_id=page_id):
                self._assert_private_404(self._insights(page_id))
        self.assertEqual(self._insights("root-doc-1").status_code, 200)

    def test_page_insights_keep_the_general_reader_gate_before_any_lookup(self) -> None:
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("hub", 1, "view")]}
        existing, unknown = self._insights("root-doc-1"), self._insights("unknown-doc")
        self.assertEqual(existing.status_code, 403)
        self.assertEqual((unknown.status_code, unknown.json()), (existing.status_code, existing.json()))
        for response in (existing, unknown):
            self.assertEqual((response.headers.get("cache-control"), response.headers.get("vary")), ("no-store", "Authorization"))

    def test_page_insights_ignore_scope_widening_parameters_and_get_bodies(self) -> None:
        self.current = ADMIN
        self._ingest(
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("root_tab.selected", target_id="root-doc-2"),
            _event("button.clicked", component_id="cmp-button", properties={}),
        )
        self._ingest_engagements(_engagement(tab_id="root-doc-1"), _engagement(tab_id="root-doc-2", nav_tab_id="nav-doc-2"))
        baseline = self._insights("root-doc-1").json()
        self.assertEqual((baseline["clicks"]["total"], baseline["active_time"]["active_seconds"]), (2, 3))
        widening = {
            "actor_email": ADMIN.email, "environment": "production", "component_type": "root_tab",
            "event_name": "root_tab.selected", "page_route": "/", "page_id": "root-doc-2",
            "tab_id": "root-doc-2", "nav_tab_id": "nav-doc-2", "page_ids": "root-doc-2",
        }
        self.assertEqual(self._insights("root-doc-1", params=widening).json(), baseline)
        with_body = self.client.request("GET", self._INSIGHTS.format("root-doc-1"), json={"page_ids": ["root-doc-2"], **widening})
        self.assertEqual(with_body.json(), baseline)

    def test_root_page_insights_roll_up_variants_and_variants_report_only_themselves(self) -> None:
        """Clicks and active time follow one policy: a root includes its current
        variants; a variant is exactly its own page."""
        variant_id = self._add_variant()
        self.current = ADMIN
        self._ingest(
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("link.clicked", component_id="cmp-links", properties={}),
            _event("view_tab.selected", target_id="variant-doc-1", properties={"tab_kind": "variant"}),
            _event("link.clicked", component_id="cmp-variant-links", properties={}),
        )
        start = datetime.now(timezone.utc).replace(microsecond=0)
        self._ingest_engagements(
            _engagement(tab_id="root-doc-1", active_seconds=3),
            _engagement(tab_id="variant-doc-1", active_seconds=5, started_at=start.isoformat(),
                        ended_at=(start + timedelta(seconds=5)).isoformat()),
        )
        self.assertEqual([row.tab_id for row in self._engagement_rows()], ["root-doc-1", "variant-doc-1"])

        def view(page_id):
            body = self._insights(page_id).json()
            return (body["page"]["kind"], body["page"]["variants_included"], body["clicks"]["total"],
                    body["active_time"]["active_seconds"],
                    [(row["id"], row["active_seconds"]) for row in body["time_by_tab"]])

        root = ("tab", 1, 4, 8, [("root-doc-1", 3), ("variant-doc-1", 5)])
        variant = ("variant", 0, 2, 5, [("variant-doc-1", 5)])
        self.assertEqual((view("root-doc-1"), view("variant-doc-1")), (root, variant))
        self.assertEqual(self._insights("root-doc-1").json()["page_openers"], [{"person": ADMIN.email, "openings": 2}])
        self.assertEqual(self._insights("variant-doc-1").json()["page_openers"], [{"person": ADMIN.email, "openings": 1}])
        # An editor of the root sees the same roll-up; an editor of the variant
        # alone gets the variant and never its parent.
        self._edit_root_tab()
        self.assertEqual((view("root-doc-1"), view("variant-doc-1")), (root, variant))
        self.grants = {MEMBER.hub_user_id: [grant("tab", variant_id, "edit")]}
        self.assertEqual(view("variant-doc-1"), variant)
        self._assert_private_404(self._insights("root-doc-1"))

    def test_sgs_sub_tabs_roll_into_their_host_page_and_are_not_addressable(self) -> None:
        self._add_sub_tab("sub-doc-1")
        with self.Session() as db:
            sub = db.query(GridstackV2).filter(GridstackV2.document_id == "sub-doc-1").one()
            db.add(ComponentV2(link="cmp-sub-links", type="links", title="Sub links", gridstack_id=sub.id))
            db.commit()
        self.current = ADMIN
        self._ingest(
            _event("sub_tab.selected", target_id="sub-doc-1", properties={"tab_kind": "sgs"}),
            _event("link.clicked", component_id="cmp-sub-links", properties={}),
        )
        self._ingest_engagements(_engagement(tab_id="sub-doc-1"))
        body = self._insights("root-doc-1").json()
        self.assertEqual(body["clicks"]["total"], 2)
        # Host-level time only: the page never claims an individual sub-tab row.
        self.assertEqual(body["time_by_tab"], [{"id": "root-doc-1", "name": "Jane Doe Salary", "active_seconds": 3}])
        self._assert_private_404(self._insights("sub-doc-1"))

    def test_page_insights_count_a_mirror_only_when_its_original_is_editable(self) -> None:
        with self.Session() as db:
            other_id = db.query(TabV2.id).filter(TabV2.document_id == "root-doc-2").scalar()
            canvas = GridstackV2(document_id="root-doc-2", parent_tab_id=other_id)
            db.add(canvas)
            db.flush()
            db.add(ComponentV2(link="cmp-board-links", type="links", title="Board links", gridstack_id=canvas.id))
            root_id = db.query(TabV2.id).filter(TabV2.document_id == "root-doc-1").scalar()
            host = db.query(GridstackV2).filter(GridstackV2.parent_tab_id == root_id, GridstackV2.parent_id.is_(None)).one()
            db.add(ComponentV2(link="cmp-board-mirror", type="mirror", title="Mirror", gridstack_id=host.id,
                               props={"target_link": "cmp-board-links"}))
            db.commit()
        self.current = ADMIN
        self._ingest(_event("link.clicked", component_id="cmp-board-mirror", properties={}))
        self.assertEqual(self._insights("root-doc-1").json()["clicks"]["total"], 1)
        self._edit_root_tab()
        self.assertEqual(self._insights("root-doc-1").json()["clicks"]["total"], 0)
        self.grants[MEMBER.hub_user_id].append(grant("tab", other_id, "edit"))
        self.assertEqual(self._insights("root-doc-1").json()["clicks"]["total"], 1)

    def test_scoped_editor_receives_openers_only_for_editable_pages(self) -> None:
        self.current = ADMIN
        self._ingest(_event(target_id="root-doc-1", actor_email="ignored@client.test"), _event(target_id="root-doc-2"))
        with self.Session() as db:
            editable = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
            other = db.query(TabV2).filter(TabV2.document_id == "root-doc-2").one()
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(summary["by_user"], [])
        self.assertEqual(len(summary["page_openers"]), 1)
        self.assertEqual(summary["page_openers"][0]["person"], ADMIN.email)
        self.assertEqual(summary["page_openers"][0]["count"], 1)
        self.grants = {MEMBER.hub_user_id: [grant("tab", other.id, "edit")]}
        after_revoke = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(len(after_revoke["page_openers"]), 1)
        self.assertNotEqual(after_revoke["page_openers"][0]["page_id"], "root-doc-1")
        self.grants = {MEMBER.hub_user_id: []}
        self.assertNotEqual(self.client.get("/v2/product-analytics/summary").status_code, 200)
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}
        event = self.client.get("/v2/product-analytics/events").json()["data"][0]
        self.assertIsNone(event["actor_email"])
        self.assertEqual(self.client.get("/v2/product-analytics/export").status_code, 403)

    def test_page_insights_openers_are_scoped_and_rechecked_after_revocation(self) -> None:
        self.current = ADMIN
        self._ingest(_event(target_id="root-doc-1"), _event(target_id="root-doc-2"),
                     _event("button.clicked", component_id="cmp-button", properties={}))
        self._edit_root_tab()
        response = self._insights("root-doc-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["page_openers"], [{"person": ADMIN.email, "openings": 1}])
        self._assert_private_404(self._insights("root-doc-2"))
        with self.Session() as db:
            other_id = db.query(TabV2.id).filter_by(document_id="root-doc-2").scalar()
        self.grants = {MEMBER.hub_user_id: [grant("tab", other_id, "edit")]}
        self._assert_private_404(self._insights("root-doc-1"))

    def test_event_page_binding_is_derived_from_verified_placement_not_client(self) -> None:
        self.current = ADMIN
        first = _event("link.clicked", component_id="cmp-links", target_id=None, page_id="root-doc-2", properties={})
        self.assertEqual(self._ingest(first).json()["accepted"], 1)
        with self.Session() as db:
            row = db.query(HubProductEventV2).filter_by(event_id=first["event_id"]).one()
            self.assertEqual(row.page_id, "root-doc-1")
            other = db.query(TabV2).filter_by(document_id="root-doc-2").one()
            grid = GridstackV2(document_id="root-doc-2", parent_tab_id=other.id)
            db.add(grid)
            db.flush()
            component = db.query(ComponentV2).filter_by(link="cmp-links").one()
            component.gridstack_id = grid.id
            db.commit()
        second = _event("link.clicked", component_id="cmp-links", target_id=None, page_id="root-doc-1", properties={})
        self.assertEqual(self._ingest(second).json()["accepted"], 1)
        with self.Session() as db:
            self.assertEqual(db.query(HubProductEventV2).filter_by(event_id=first["event_id"]).one().page_id, "root-doc-1")
            self.assertEqual(db.query(HubProductEventV2).filter_by(event_id=second["event_id"]).one().page_id, "root-doc-2")

    def _add_sub_tab(self, document_id: str) -> int:
        """A sub-tab as the Hub stores it: a sub-gridstack (what
        `/v2/tabs/{id}/children` returns) whose access lives on its
        representation component. Returns that component's id."""
        with self.Session() as db:
            root = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
            host = db.query(GridstackV2).filter(GridstackV2.parent_tab_id == root.id, GridstackV2.parent_id.is_(None)).one()
            sub = GridstackV2(document_id=document_id, name=SENSITIVE_TITLE, parent_id=host.id, parent_tab_id=root.id)
            db.add(sub)
            db.flush()
            representation = ComponentV2(link=f"rep-{document_id}", type="gridstack", gridstack_id=host.id, current_grid_id=sub.id)
            db.add(representation)
            db.commit()
            return representation.id

    def test_sub_tab_ids_are_verified_through_their_representation_component(self) -> None:
        """Child view tabs and sub-tabs are sub-gridstacks, not TabV2 rows."""
        own = self._add_sub_tab("sub-doc-1")
        self._add_sub_tab("sub-doc-2")
        self.current = ADMIN
        body = self._ingest(
            _event("sub_tab.selected", target_id="sub-doc-1", properties={"tab_kind": "sgs"}),
            _event("view_tab.selected", target_id="sub-doc-2", properties={"tab_kind": "child"}),
        ).json()
        self.assertEqual(body["accepted"], 2)
        self.assertEqual([r.target_id for r in self._rows()], ["sub-doc-1", "sub-doc-2"])
        self.assertEqual(self._ingest_engagements(_engagement(nav_tab_id=None, tab_id="sub-doc-1")).json()["accepted"], 1)
        self.assertEqual(self._engagement_rows()[0].tab_id, "sub-doc-1")
        # An editor of one sub-tab sees that sub-tab's clicks and time only.
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("component", own, "edit")]}
        events = self.client.get("/v2/product-analytics/events").json()
        self.assertEqual([e["target_id"] for e in events["data"]], ["sub-doc-1"])
        self.assertEqual(self.client.get("/v2/product-analytics/summary").json()["engagement"]["active_seconds"], 3)
        # Without access to the sub-tab, the id is dropped at ingest like any unknown id.
        self.grants = {MEMBER.hub_user_id: [grant("nav_tab", 2, "view")]}
        self._ingest(_event("sub_tab.selected", target_id="sub-doc-1"))
        self.assertIsNone(self._rows()[-1].target_id)

    # ── Active-time filters ──────────────────────────────────────────────

    def test_click_only_filters_do_not_narrow_active_time_and_say_so(self) -> None:
        self._ingest_engagements(_engagement())
        self.current = ADMIN
        engagement = self.client.get("/v2/product-analytics/summary?component_type=link&trigger=click").json()["engagement"]
        # Active time is page time: a component filter cannot truthfully narrow it.
        self.assertEqual(engagement["active_seconds"], 3)
        self.assertEqual(engagement["click_only_filters"], ["component_type", "trigger"])
        self.assertNotIn("component_type", engagement["applied_filters"])

    def test_page_user_date_and_environment_filters_narrow_active_time(self) -> None:
        self._ingest_engagements(_engagement())
        self.current = ADMIN
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        for query, expected in (
            ("page_route=/:nav_tab", 3), ("page_route=/admin", 0),
            (f"actor_email={MEMBER.email}", 3), ("actor_email=someone@renphil.org", 0),
            (f"date_from={tomorrow}", 0), ("environment=sandbox", 3),
        ):
            with self.subTest(query=query):
                engagement = self.client.get(f"/v2/product-analytics/summary?{query}").json()["engagement"]
                self.assertEqual(engagement["active_seconds"], expected)
                self.assertEqual(engagement["click_only_filters"], [])
        self.environment = "production"
        self.assertEqual(self.client.get("/v2/product-analytics/summary").json()["engagement"]["active_seconds"], 0)

    def test_scoped_editor_cannot_filter_by_person(self) -> None:
        with self.Session() as db:
            editable = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
        self.current = ADMIN
        self._ingest(_event(target_id="root-doc-1"))
        self.assertTrue(self.client.get("/v2/product-analytics/catalog").json()["people_visible"])
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}
        self.assertFalse(self.client.get("/v2/product-analytics/catalog").json()["people_visible"])
        for path in ("summary", "events", "export/xlsx"):
            with self.subTest(path=path):
                response = self.client.get(f"/v2/product-analytics/{path}?actor_email={ADMIN.email}")
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(ADMIN.email, response.text)

    # ── Editor scale ─────────────────────────────────────────────────────

    def _insert_intervals(self, count: int, **overrides) -> None:
        now = datetime.now(timezone.utc)
        rows = [{
            "engagement_id": str(uuid.uuid4()), "hub_user_id": 2, "actor_email": MEMBER.email,
            "environment": "sandbox", "page_route": "/:nav_tab/:tab", "nav_tab_id": "nav-doc-1",
            "tab_id": "root-doc-1", "active_seconds": 3, "started_at": now - timedelta(seconds=3),
            "ended_at": now, "received_at": now, **overrides,
        } for _ in range(count)]
        with self.Session() as db:
            db.execute(HubProductEngagementV2.__table__.insert(), rows)
            db.commit()

    def _edit_root_tab(self) -> None:
        with self.Session() as db:
            editable = db.query(TabV2).filter(TabV2.document_id == "root-doc-1").one()
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", editable.id, "edit")]}

    def test_editor_active_time_counts_every_interval_not_a_capped_sample(self) -> None:
        # More intervals than the 50,000 raw rows the editor path used to read.
        count = service.MAX_EXPORT_ROWS + 25
        self._insert_intervals(count)
        self._insert_intervals(5, tab_id="root-doc-2", nav_tab_id="nav-doc-2")
        self._edit_root_tab()
        engagement = self.client.get("/v2/product-analytics/summary").json()["engagement"]
        self.assertEqual((engagement["active_seconds"], engagement["intervals"]), (3 * count, count))

    def test_editor_active_time_beyond_the_group_bound_fails_without_partial_totals(self) -> None:
        for route in ("/:nav_tab/:tab", "/:nav_tab/:tab/:tab", "/:nav_tab/:tab/:tab/:tab"):
            self._insert_intervals(2, page_route=route)
        self._edit_root_tab()
        with patch.object(service, "MAX_ENGAGEMENT_GROUPS", 2):
            for path in ("summary", "export/xlsx"):
                with self.subTest(path=path):
                    response = self.client.get(f"/v2/product-analytics/{path}")
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("Narrow the date range", response.json()["detail"])
                    self.assertNotIn("active_seconds", response.text)
            # Admin totals are a single SQL aggregate and remain complete.
            self.current = ADMIN
            self.assertEqual(self.client.get("/v2/product-analytics/summary").json()["engagement"]["active_seconds"], 18)

    def test_editor_click_rows_beyond_the_bound_fail_explicitly(self) -> None:
        self.current = ADMIN
        self._ingest(_event(target_id="root-doc-1"), _event(target_id="root-doc-1"))
        self._edit_root_tab()
        with patch.object(service, "MAX_EXPORT_ROWS", 1):
            for path in ("summary", "events"):
                with self.subTest(path=path):
                    response = self.client.get(f"/v2/product-analytics/{path}")
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("Narrow the date range", response.json()["detail"])

    # ── Read-time destinations ───────────────────────────────────────────

    def test_sub_tab_resolves_to_a_readable_current_destination_without_storing_it(self) -> None:
        own = self._add_sub_tab("sub-doc-1")
        self.current = ADMIN
        self._ingest(_event("sub_tab.selected", target_id="sub-doc-1", properties={"tab_kind": "sgs"}))
        expected_path = "/jane-doe-review-hub/jane-doe-salary/jane-doe-performance-review"
        (event,) = self.client.get("/v2/product-analytics/events").json()["data"]
        self.assertEqual((event["target_name"], event["open_path"]), (SENSITIVE_TITLE, expected_path))
        locations = self.client.get("/v2/product-analytics/summary").json()["by_location"]
        self.assertEqual([(l["label"], l["href"]) for l in locations], [(SENSITIVE_TITLE, expected_path)])
        # An editor of that sub-tab gets the same destination.
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("component", own, "edit")]}
        (event,) = self.client.get("/v2/product-analytics/events").json()["data"]
        self.assertEqual(event["open_path"], expected_path)
        # Nothing readable was persisted.
        with self.engine.connect() as conn:
            dump = json.dumps([list(r) for r in conn.execute(text("SELECT * FROM hub_product_events"))], default=str)
        self.assertNotIn("Jane", dump)
        self.assertNotIn("jane-doe", dump)

    def test_deleted_sub_tab_stays_opaque_and_unclickable(self) -> None:
        self._add_sub_tab("sub-doc-1")
        self.current = ADMIN
        self._ingest(_event("sub_tab.selected", target_id="sub-doc-1"))
        with self.Session() as db:
            db.query(ComponentV2).filter(ComponentV2.link == "rep-sub-doc-1").delete()
            db.query(GridstackV2).filter(GridstackV2.document_id == "sub-doc-1").delete()
            db.commit()
        (event,) = self.client.get("/v2/product-analytics/events").json()["data"]
        self.assertEqual((event["target_id"], event["target_name"], event["open_path"]), ("sub-doc-1", None, None))
        (location,) = self.client.get("/v2/product-analytics/summary").json()["by_location"]
        self.assertIsNone(location["href"])
        self.assertNotIn(SENSITIVE_TITLE, location["label"])

    def test_names_and_destinations_need_the_readers_current_access(self) -> None:
        self._add_sub_tab("sub-doc-1")
        events = [
            {"event_name": "navigation_tab.selected", "target_id": "nav-doc-1", "component_id": None, "page_route": "/"},
            {"event_name": "root_tab.selected", "target_id": "root-doc-1", "component_id": None, "page_route": "/"},
            {"event_name": "sub_tab.selected", "target_id": "sub-doc-1", "component_id": None, "page_route": "/"},
            {"event_name": "link.clicked", "target_id": None, "component_id": "cmp-links", "page_route": "/"},
            {"event_name": "thread_item.opened", "target_id": "7", "component_id": "cmp-thread", "page_route": "/"},
            {"event_name": "detail_card.details_opened", "target_id": "card-a", "component_id": "cmp-mirror", "page_route": "/"},
            {"event_name": "blocknote_tab.selected", "target_id": "cmp-sbn-child", "component_id": "cmp-sbn", "page_route": "/"},
        ]
        from app.services.product_analytics_refs import resolve_names

        # Someone who can see only the unrelated nav tab gets nothing readable or clickable.
        self.grants = {MEMBER.hub_user_id: [grant("nav_tab", 2, "view")]}
        with self.Session() as db:
            outsider = product_analytics._viewer_access(db, MEMBER)
            resolved = resolve_names(db, [dict(e) for e in events], access=outsider)
        for row in resolved:
            with self.subTest(event=row["event_name"]):
                self.assertEqual(
                    [row[k] for k in ("target_name", "component_name", "open_path", "open_component_link", "open_component_name")],
                    [None] * 5,
                )
        # A Hub-wide viewer gets every name and the mirror's server-derived original.
        self.grants = {MEMBER.hub_user_id: [grant("hub", 1)]}
        with self.Session() as db:
            insider = product_analytics._viewer_access(db, MEMBER)
            resolved = {r["event_name"]: r for r in resolve_names(db, [dict(e) for e in events], access=insider)}
        self.assertEqual(resolved["sub_tab.selected"]["target_name"], SENSITIVE_TITLE)
        self.assertEqual(resolved["detail_card.details_opened"]["open_component_link"], "cmp-cards")
        self.assertEqual(resolved["detail_card.details_opened"]["mirror_component_link"], "cmp-mirror")

    def test_mirror_opens_its_original_only_when_the_original_is_accessible(self) -> None:
        from app.services.product_analytics_refs import resolve_names

        with self.Session() as db:
            mirror, cards = node(db, "cmp-mirror"), node(db, "cmp-cards")
        event = {"event_name": "link.clicked", "target_id": None, "component_id": "cmp-mirror", "page_route": "/"}
        self.grants = {MEMBER.hub_user_id: [grant(*mirror), grant(*cards)]}
        with self.Session() as db:
            (row,) = resolve_names(db, [dict(event)], access=product_analytics._viewer_access(db, MEMBER))
        self.assertEqual((row["open_component_link"], row["mirror_component_link"]), ("cmp-cards", "cmp-mirror"))
        self.grants = {MEMBER.hub_user_id: [grant(*mirror)]}
        with self.Session() as db:
            (row,) = resolve_names(db, [dict(event)], access=product_analytics._viewer_access(db, MEMBER))
        self.assertIsNone(row["open_component_link"])
        self.assertIsNone(row["open_component_name"])

    def test_view_only_user_cannot_open_analytics_even_when_events_exist(self) -> None:
        self.current = ADMIN
        self._ingest(_event())
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("hub", 1, "view")]}
        self.assertEqual(self.client.get("/v2/product-analytics/summary").status_code, 403)
        self.assertEqual(self.client.get("/v2/product-analytics/events").status_code, 403)
        self.assertEqual(self.client.get("/v2/product-analytics/export").status_code, 403)

    def test_component_and_action_are_derived_from_catalog_not_client(self) -> None:
        self._ingest(_event("button.clicked", component_type="root_tab", action="delete"))
        (row,) = self._rows()
        self.assertEqual((row.component_type, row.action), ("button", "open_link"))

    def test_every_catalog_event_is_accepted(self) -> None:
        events = [_event(name, properties={}) for name in service.EVENT_CATALOG]
        response = self._ingest(*events)
        self.assertEqual(response.json()["accepted"], len(service.EVENT_CATALOG))

    def test_requires_authentication(self) -> None:
        app = FastAPI()
        app.include_router(product_analytics.router)
        response = TestClient(app).post("/v2/product-analytics/events", json={"events": [_event()]})
        self.assertIn(response.status_code, (401, 403))

    # ── Validation ───────────────────────────────────────────────────────

    def test_invalid_events_are_rejected_individually(self) -> None:
        response = self._ingest(
            _event(),
            _event("unknown.event"),
            _event(event_id="not-a-uuid"),
            _event(trigger="hover"),
            _event("button.clicked", trigger="default"),
            _event(page_route="https://evil.test"),
            "not-an-object",
        )
        body = response.json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual([r["index"] for r in body["rejected"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(self._rows()), 1)

    def test_selection_events_may_be_defaults(self) -> None:
        cascade = str(uuid.uuid4())
        body = self._ingest(_event("view_tab.selected", trigger="default", cascade_id=cascade)).json()
        self.assertEqual(body["accepted"], 1)
        (row,) = self._rows()
        self.assertEqual((row.trigger, row.cascade_id), ("default", cascade))

    def test_batch_size_is_bounded_to_25(self) -> None:
        self.assertEqual(service.MAX_BATCH, 25)
        self.assertEqual(self._ingest(*[_event() for _ in range(25)]).status_code, 200)
        self.assertEqual(self._ingest(*[_event() for _ in range(26)]).status_code, 422)
        self.assertEqual(self.client.post("/v2/product-analytics/events", json={"events": []}).status_code, 422)

    def test_skewed_client_clock_uses_receive_time(self) -> None:
        far_past = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self._ingest(_event(occurred_at=far_past))
        (row,) = self._rows()
        self.assertEqual(row.occurred_at.date(), row.received_at.date())

    # ── Deduplication ────────────────────────────────────────────────────

    def test_retried_batch_is_ingested_once(self) -> None:
        event = _event()
        first = self._ingest(event).json()
        retry = self._ingest(event).json()
        self.assertEqual((first["accepted"], first["duplicates"]), (1, 0))
        self.assertEqual((retry["accepted"], retry["duplicates"]), (0, 1))
        self.assertEqual(len(self._rows()), 1)

    def test_duplicate_ids_inside_one_batch_are_ingested_once(self) -> None:
        event = _event()
        body = self._ingest(event, dict(event), _event()).json()
        self.assertEqual((body["accepted"], body["duplicates"]), (2, 1))

    # ── Privacy-safe metadata ────────────────────────────────────────────

    def test_titles_and_labels_never_reach_db_responses_or_export(self) -> None:
        """Whatever a (stale or malicious) client sends, no free text survives."""
        self._ingest(_event(
            "link.clicked",
            target_label=SENSITIVE_TITLE,
            title=SENSITIVE_TITLE,
            label=SENSITIVE_TITLE,
            target_id=SENSITIVE_TITLE,
            component_id=SENSITIVE_TITLE,
            properties={
                "label": SENSITIVE_TITLE,
                "title": SENSITIVE_TITLE,
                "tab_kind": SENSITIVE_TITLE,
                "destination_host": "docs.google.com",
                "destination_path": "/dashboard/hr",
                "destination_kind": "external",
            },
        ))
        (row,) = self._rows()
        self.assertFalse(hasattr(row, "target_label"))
        self.assertIsNone(row.target_id)
        self.assertIsNone(row.component_id)
        self.assertEqual(row.properties, {"destination_kind": "external"})
        with self.engine.connect() as conn:
            raw = json.dumps([list(r) for r in conn.execute(text("SELECT * FROM hub_product_events")).all()], default=str)
        self.assertNotIn("Jane", raw)

        self.current = ADMIN
        for path in ("events", "summary", "export"):
            body = self.client.get(f"/v2/product-analytics/{path}").text
            self.assertNotIn("Jane", body, path)
            self.assertNotIn("target_label", body, path)

    def test_unsafe_paths_are_cleaned(self) -> None:
        token = "a1" * 20
        self._ingest(_event(page_route=f"/profile/jane@renphil.org/{token}/communications-and-marketing?token=abc#frag"))
        (row,) = self._rows()
        self.assertEqual(row.page_route, "/profile/:segment/:segment/:segment")

    def test_title_derived_route_slugs_are_never_stored(self) -> None:
        """Hub URLs are built from nav/root/view/sub-tab TITLES; only the
        route template may be stored, while built-in sections stay readable."""
        cases = {
            "/jane-doe-performance-review/home": "/:nav_tab/:tab",
            "/finance/q3-board-pack/jane-doe-salary/sub": "/:nav_tab/:tab/:tab/:tab",
            "/dashboard/jane-doe-performance-review/us": "/dashboard/:tab/:tab",
            "/admin/bot-management": "/admin/bot-management",
            "/admin/access-control/root-access": "/admin/access-control/root-access",
            "/admin/jane-doe": "/admin/:segment",
            "/threads/management": "/threads/management",
            "/threads/revisions": "/threads/revisions",
            "/dashboard/announcements/recJaneDoe": "/dashboard/announcements/:segment",
            "/": "/",
            # Already-templated input from the browser is stable.
            "/:nav_tab/:tab": "/:nav_tab/:tab",
        }
        for raw, expected in cases.items():
            self.assertEqual(service.route_template(raw), expected, raw)
        for rejected in ("https://evil.test/x", "//evil.test/x", "dashboard", "/a\\b", "", None, 42):
            self.assertIsNone(service.route_template(rejected), rejected)
        self._ingest(*[_event(page_route=raw) for raw in cases])
        with self.engine.connect() as conn:
            stored = json.dumps([list(r) for r in conn.execute(text("SELECT * FROM hub_product_events")).all()], default=str).lower()
        for fragment in ("jane", "finance", "q3-board", "salary", "recjanedoe"):
            self.assertNotIn(fragment, stored)

    # ── Body size ────────────────────────────────────────────────────────

    def _assert_nothing_happened(self) -> None:
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.limiter.checks, [])
        self.assertEqual(self.limiter.consumed, [])

    def test_oversized_declared_content_length_is_refused_before_processing(self) -> None:
        padding = "x" * (product_analytics.MAX_INGEST_BODY_BYTES + 1)
        with patch.object(product_analytics.json, "loads", side_effect=AssertionError("parsed")) as loads:
            response = self.client.post(
                "/v2/product-analytics/events",
                content=json.dumps({"events": [_event(properties={"pad": padding})]}),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 413)
        loads.assert_not_called()
        self._assert_nothing_happened()

    def test_invalid_content_length_is_refused(self) -> None:
        response = self.client.post(
            "/v2/product-analytics/events", content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "abc"},
        )
        self.assertIn(response.status_code, (400, 413))
        self._assert_nothing_happened()

    def test_oversized_chunked_body_without_content_length_is_refused(self) -> None:
        valid = json.dumps({"events": [_event() for _ in range(5)]}).encode()

        def chunks():
            # Valid events first, then padding: nothing may be stored.
            yield valid[:-2]
            yield b', "pad": "'
            for _ in range(20):
                yield b"y" * 8192
            yield b'"}'

        with patch.object(product_analytics.json, "loads", side_effect=AssertionError("parsed")) as loads:
            response = self.client.post(
                "/v2/product-analytics/events", content=chunks(), headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 413)
        loads.assert_not_called()
        self._assert_nothing_happened()

    def test_valid_chunked_batch_without_content_length_is_ingested(self) -> None:
        body = json.dumps({"events": [_event(), _event()]}).encode()

        def chunks():
            for i in range(0, len(body), 64):
                yield body[i:i + 64]

        response = self.client.post("/v2/product-analytics/events", content=chunks(), headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["accepted"], 2)
        self.assertEqual(len(self._rows()), 2)

    def test_bounded_reader_stops_reading_at_the_limit(self) -> None:
        pulled: list[int] = []

        class FakeRequest:
            headers: dict = {}

            async def stream(self):
                for i in range(1000):
                    pulled.append(i)
                    yield b"z" * 1024

        with self.assertRaises(product_analytics.HTTPException) as caught:
            asyncio.run(product_analytics.read_bounded_body(FakeRequest(), limit=4096))
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(len(pulled), 5)  # stopped on the first chunk past 4 KB

    def test_malformed_json_is_refused_without_counting(self) -> None:
        response = self.client.post(
            "/v2/product-analytics/events", content=b"{not json", headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self._assert_nothing_happened()

    # ── Identifiers: only verified ids are stored ─────────────────────────

    def _stored(self, *events: dict) -> list[tuple]:
        response = self._ingest(*events)
        self.assertEqual(response.status_code, 200, response.text)
        return [(r.event_name, r.component_id, r.target_id) for r in self._rows()]

    def test_verified_ids_are_kept(self) -> None:
        stored = self._stored(
            _event("navigation_tab.selected", target_id="nav-doc-1"),
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("view_tab.selected", target_id="child-doc-1"),
            _event("sub_tab.selected", target_id="child-doc-1"),
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn-child"),
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn"),
            _event("thread_item.opened", component_id="cmp-thread", target_id="7"),
            _event("detail_card.details_opened", component_id="cmp-cards", target_id="card-a"),
            _event("detail_card.details_opened", component_id="cmp-mirror", target_id="card-a"),
            _event("link.clicked", component_id="cmp-links", properties={}),
            _event("threads_management.tab_selected", target_id="history", properties={}),
            _event("revisions_management.tab_selected", target_id="pending", properties={}),
        )
        self.assertEqual(stored, [
            ("navigation_tab.selected", None, "nav-doc-1"),
            ("root_tab.selected", None, "root-doc-1"),
            ("view_tab.selected", None, "child-doc-1"),
            ("sub_tab.selected", None, "child-doc-1"),
            ("blocknote_tab.selected", "cmp-sbn", "cmp-sbn-child"),
            ("blocknote_tab.selected", "cmp-sbn", "cmp-sbn"),
            ("thread_item.opened", "cmp-thread", "7"),
            ("detail_card.details_opened", "cmp-cards", "card-a"),
            ("detail_card.details_opened", "cmp-mirror", "card-a"),
            ("link.clicked", "cmp-links", None),
            ("threads_management.tab_selected", None, "history"),
            ("revisions_management.tab_selected", None, "pending"),
        ])

    def test_modified_client_cannot_store_arbitrary_identifiers(self) -> None:
        smuggled = [
            "Jane Doe",                                   # a name
            "jane-doe-salary-2026",                        # arbitrary hyphenated text
            "amFuZS5kb2VAcmVucGhpbC5vcmc",               # base64 of an email address
            "6a616e652e646f65",                            # hex-encoded personal data
            "3337558d-2ec4-439c-9c50-66dd0e4cfe4a",        # valid-looking, nonexistent
            "1234567",                                     # numeric, nonexistent
        ]
        events = []
        for value in smuggled:
            events += [
                _event("navigation_tab.selected", target_id=value, component_id=value),
                _event("root_tab.selected", target_id=value),
                _event("view_tab.selected", target_id=value),
                _event("blocknote_tab.selected", component_id=value, target_id=value),
                _event("thread_item.opened", component_id=value, target_id=value),
                _event("detail_card.details_opened", component_id=value, target_id=value),
                _event("link.clicked", component_id=value, target_id=value, properties={}),
                _event("threads_management.tab_selected", target_id=value, properties={}),
                _event("profile_menu.go_to_profile", component_id=value, target_id=value, properties={}),
            ]
        for start in range(0, len(events), service.MAX_BATCH):
            self.assertEqual(self._ingest(*events[start:start + service.MAX_BATCH]).status_code, 200)
        rows = self._rows()
        self.assertEqual(len(rows), len(events))
        self.assertTrue(all(r.component_id is None and r.target_id is None for r in rows))

    def test_real_ids_used_in_the_wrong_place_are_dropped(self) -> None:
        stored = self._stored(
            _event("navigation_tab.selected", target_id="root-doc-1"),         # a tab, not a nav tab
            _event("root_tab.selected", target_id="child-doc-1"),              # a child, not a root
            _event("root_tab.selected", target_id="nav-doc-1"),                # a nav tab
            _event("link.clicked", component_id="cmp-button", properties={}),  # wrong widget type
            _event("thread_item.opened", component_id="cmp-thread", target_id="8"),   # thread of another widget
            _event("detail_card.details_opened", component_id="cmp-cards", target_id="card-zzz"),
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-links"),  # not its page
            _event("button.clicked", component_id="cmp-button", target_id="root-doc-1", properties={}),
            _event("root_tab.selected", component_id="cmp-links", target_id="root-doc-1"),
        )
        self.assertEqual(stored, [
            ("navigation_tab.selected", None, None),
            ("root_tab.selected", None, None),
            ("root_tab.selected", None, None),
            ("link.clicked", None, None),
            ("thread_item.opened", "cmp-thread", None),
            ("detail_card.details_opened", "cmp-cards", None),
            ("blocknote_tab.selected", "cmp-sbn", None),
            ("button.clicked", "cmp-button", None),
            ("root_tab.selected", None, "root-doc-1"),
        ])

    def test_unknown_ids_are_not_revealed_in_the_response(self) -> None:
        known = self._ingest(_event("root_tab.selected", target_id="root-doc-1")).json()
        unknown = self._ingest(_event("root_tab.selected", target_id="3337558d-2ec4-439c-9c50-66dd0e4cfe4a")).json()
        self.assertEqual(known, unknown)
        self.assertEqual(known, {"accepted": 1, "duplicates": 0, "rejected": []})

    # ── Live Hub access: a real but inaccessible id is dropped like a fake ──

    def _narrow_member(self, *links: str) -> None:
        """The member is granted view on exactly these widgets/pages, nothing
        else. The real fold then reveals their ancestors (nav tab, root tab,
        SBN root) for navigation, as it does in the Hub UI."""
        with self.Session() as db:
            self.grants[MEMBER.hub_user_id] = [grant(*node(db, link)) for link in links]

    def _assert_dropped_like_nonexistent(self, real: dict, fake: dict) -> None:
        """Same response, same stored row shape, and nothing logged by the app."""
        with self.assertNoLogs("app", level=logging.DEBUG):
            real_response = self._ingest(real)
            fake_response = self._ingest(fake)
        self.assertEqual(real_response.status_code, fake_response.status_code)
        self.assertEqual(real_response.json(), fake_response.json())
        self.assertEqual(real_response.json(), {"accepted": 1, "duplicates": 0, "rejected": []})
        self.assertEqual(real_response.headers.get("content-length"), fake_response.headers.get("content-length"))
        # The fake differs from the real event only in the id under test, so
        # identical stored rows mean that id was dropped exactly like a fake.
        rows = {r.event_id: (r.component_id, r.target_id) for r in self._rows()}
        self.assertEqual(rows[real["event_id"]], rows[fake["event_id"]])

    def test_member_existing_but_inaccessible_nav_tab_is_dropped(self) -> None:
        self._narrow_member("cmp-links")
        self._assert_dropped_like_nonexistent(
            _event("navigation_tab.selected", target_id="nav-doc-2", properties={"tab_kind": "nav_tab"}),
            _event("navigation_tab.selected", target_id=str(uuid.uuid4()), properties={"tab_kind": "nav_tab"}),
        )
        self._assert_dropped_like_nonexistent(
            _event("root_tab.selected", target_id="root-doc-2"),
            _event("root_tab.selected", target_id=str(uuid.uuid4())),
        )
        # A variant under a tab the member only reaches by reveal is not visible.
        self._assert_dropped_like_nonexistent(
            _event("view_tab.selected", target_id="child-doc-1"),
            _event("view_tab.selected", target_id=str(uuid.uuid4())),
        )

    def test_member_inaccessible_widget_is_dropped(self) -> None:
        self._narrow_member("cmp-links")
        self._assert_dropped_like_nonexistent(
            _event("button.clicked", component_id="cmp-button", properties={"destination_kind": "external"}),
            _event("button.clicked", component_id=str(uuid.uuid4()), properties={"destination_kind": "external"}),
        )
        self._assert_dropped_like_nonexistent(
            _event("detail_card.details_opened", component_id="cmp-cards", target_id="card-a", properties={}),
            _event("detail_card.details_opened", component_id=str(uuid.uuid4()), target_id="card-a", properties={}),
        )

    def test_mirror_needs_both_its_position_and_its_target(self) -> None:
        # Granted on the mirror's position only: the UI redacts it (§3.4).
        self._narrow_member("cmp-mirror")
        self._assert_dropped_like_nonexistent(
            _event("detail_card.details_opened", component_id="cmp-mirror", target_id="card-a", properties={}),
            _event("detail_card.details_opened", component_id=str(uuid.uuid4()), target_id="card-a", properties={}),
        )
        self._narrow_member("cmp-mirror", "cmp-cards")
        stored = self._stored(_event("detail_card.details_opened", component_id="cmp-mirror", target_id="card-a", properties={}))
        self.assertIn(("detail_card.details_opened", "cmp-mirror", "card-a"), stored)

    def test_member_thread_inside_inaccessible_widget_is_dropped(self) -> None:
        self._narrow_member("cmp-thread-2")
        self._assert_dropped_like_nonexistent(
            _event("thread_item.opened", component_id="cmp-thread", target_id="7", properties={}),
            _event("thread_item.opened", component_id="cmp-thread", target_id="999999", properties={}),
        )
        stored = self._stored(_event("thread_item.opened", component_id="cmp-thread-2", target_id="8", properties={}))
        self.assertIn(("thread_item.opened", "cmp-thread-2", "8"), stored)

    def test_member_pending_thread_of_someone_else_is_dropped(self) -> None:
        with self.Session() as db:
            widget = db.query(ComponentV2).filter(ComponentV2.link == "cmp-thread-2").one()
            db.add(ThreadV2(id=20, component_id=widget.id, title="Pending", content="x",
                            author_email="someone@renphil.org", status="pending", created_at=datetime.now(timezone.utc)))
            db.add(ThreadV2(id=21, component_id=widget.id, title="Mine", content="x",
                            author_email="Member@Renphil.org", status="pending", created_at=datetime.now(timezone.utc)))
            db.commit()
        self._narrow_member("cmp-thread-2")
        self._assert_dropped_like_nonexistent(
            _event("thread_item.opened", component_id="cmp-thread-2", target_id="20", properties={}),
            _event("thread_item.opened", component_id="cmp-thread-2", target_id="999998", properties={}),
        )
        # Its own author sees a pending thread; so does an editor of the widget.
        stored = self._stored(_event("thread_item.opened", component_id="cmp-thread-2", target_id="21", properties={}))
        self.assertIn(("thread_item.opened", "cmp-thread-2", "21"), stored)
        with self.Session() as db:
            self.grants[MEMBER.hub_user_id] = [grant(*node(db, "cmp-thread-2"), level="edit")]
        stored = self._stored(_event("thread_item.opened", component_id="cmp-thread-2", target_id="20", properties={}))
        self.assertIn(("thread_item.opened", "cmp-thread-2", "20"), stored)

    def test_member_inaccessible_blocknote_page_is_dropped(self) -> None:
        self._narrow_member("cmp-sbn-child")
        self._assert_dropped_like_nonexistent(
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn-secret"),
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id=str(uuid.uuid4())),
        )
        # With no grant anywhere in the notebook, the notebook itself is dropped too.
        self._narrow_member("cmp-links")
        self._assert_dropped_like_nonexistent(
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn-child"),
            _event("blocknote_tab.selected", component_id=str(uuid.uuid4()), target_id="cmp-sbn-child"),
        )

    def test_member_permitted_scoped_resources_remain_recorded(self) -> None:
        self._narrow_member("cmp-links", "cmp-sbn-child", "cmp-thread-2")
        stored = self._stored(
            # Revealed ancestors of a granted widget stay navigable, as in the UI.
            _event("navigation_tab.selected", target_id="nav-doc-1", properties={"tab_kind": "nav_tab"}),
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("link.clicked", component_id="cmp-links", properties={}),
            # Revealed SBN root + granted page.
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn-child"),
            _event("thread_item.opened", component_id="cmp-thread-2", target_id="8", properties={}),
        )
        self.assertEqual(stored, [
            ("navigation_tab.selected", None, "nav-doc-1"),
            ("root_tab.selected", None, "root-doc-1"),
            ("link.clicked", "cmp-links", None),
            ("blocknote_tab.selected", "cmp-sbn", "cmp-sbn-child"),
            ("thread_item.opened", "cmp-thread-2", "8"),
        ])

    def test_hub_admin_with_no_grants_keeps_every_real_id(self) -> None:
        self.current = ADMIN
        self.assertEqual(self.grants.get(ADMIN.hub_user_id), None)
        stored = self._stored(
            _event("navigation_tab.selected", target_id="nav-doc-2", properties={"tab_kind": "nav_tab"}),
            _event("root_tab.selected", target_id="root-doc-2"),
            _event("view_tab.selected", target_id="child-doc-1"),
            _event("button.clicked", component_id="cmp-button", properties={"destination_kind": "external"}),
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn-secret"),
            _event("thread_item.opened", component_id="cmp-thread", target_id="7", properties={}),
            _event("detail_card.details_opened", component_id="cmp-mirror", target_id="card-a", properties={}),
        )
        self.assertEqual([row[1:] for row in stored], [
            (None, "nav-doc-2"), (None, "root-doc-2"), (None, "child-doc-1"), ("cmp-button", None),
            ("cmp-sbn", "cmp-sbn-secret"), ("cmp-thread", "7"), ("cmp-mirror", "card-a"),
        ])
        # A Hub Admin that loses the role is a member again, immediately.
        self.hub_admins = set()
        self._assert_dropped_like_nonexistent(
            _event("root_tab.selected", target_id="root-doc-2"),
            _event("root_tab.selected", target_id=str(uuid.uuid4())),
        )

    def test_access_is_resolved_once_per_batch_and_only_when_needed(self) -> None:
        self._ingest(_event("profile_menu.go_to_profile", properties={}),
                     _event("threads_management.tab_selected", target_id="history", properties={}),
                     _event("root_tab.selected", target_id=str(uuid.uuid4())))
        self.assertEqual(self.access_calls, 0)
        self._ingest(*[_event("root_tab.selected", target_id="root-doc-1") for _ in range(5)],
                     _event("link.clicked", component_id="cmp-links", properties={}))
        self.assertEqual(self.access_calls, 1)

    def test_grant_revoked_after_a_first_batch_applies_to_the_next(self) -> None:
        self._narrow_member("cmp-links")
        first = self._stored(_event("link.clicked", component_id="cmp-links", properties={}))
        self.assertEqual(first[-1][1], "cmp-links")
        self.grants[MEMBER.hub_user_id] = []
        later = _event("link.clicked", component_id="cmp-links", properties={})
        self._ingest(later)
        rows = {r.event_id: r.component_id for r in self._rows()}
        self.assertIsNone(rows[later["event_id"]])

    # ── Session ownership: the worker thread opens and closes its own ──────

    def test_ingest_runs_in_its_own_worker_session_closed_on_success(self) -> None:
        response = self._ingest(_event("root_tab.selected", target_id="root-doc-1"),
                                _event("link.clicked", component_id="cmp-links", properties={}))
        self.assertEqual(response.json(), {"accepted": 2, "duplicates": 0, "rejected": []})
        # The request-scoped dependency session is never opened for ingest.
        self.assertEqual(self.request_sessions, 0)
        self.assertEqual(len(self.worker_sessions), 1)
        record = self.worker_sessions[0]
        # Created, used for access resolution, and closed in ONE thread that
        # is not the test thread.
        self.assertNotEqual(record["thread"], threading.get_ident())
        self.assertEqual(record["closed_in"], record["thread"])
        self.assertEqual(self.access_seen, [(record["thread"], record["session"])])
        self.assertFalse(record["rolled_back"])
        self.assertEqual([r.target_id for r in self._rows()], ["root-doc-1", None])

    def test_each_batch_gets_a_fresh_worker_session(self) -> None:
        self._ingest(_event())
        self._ingest(_event())
        self.assertEqual(len(self.worker_sessions), 2)
        self.assertIsNot(self.worker_sessions[0]["session"], self.worker_sessions[1]["session"])
        self.assertTrue(all(r["closed_in"] for r in self.worker_sessions))

    def test_database_failure_rolls_back_closes_and_returns_503(self) -> None:
        def broken(_db, _rows):
            raise OperationalError("INSERT", {}, Exception("connection lost"))

        with patch.object(service, "_insert_ignoring_duplicates", broken), \
                self.assertLogs("app.routers.product_analytics", level="WARNING") as logs:
            response = self._ingest(_event("root_tab.selected", target_id="root-doc-1"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Product analytics is temporarily unavailable."})
        record = self.worker_sessions[0]
        self.assertTrue(record["rolled_back"])
        self.assertEqual(record["closed_in"], record["thread"])
        self.assertEqual(self.limiter.consumed, [])  # a failed batch is not counted
        self.assertEqual(self._rows(), [])
        self.assertEqual(logs.output, ["WARNING:app.routers.product_analytics:Product analytics ingestion failed: OperationalError"])

    def test_access_lookup_failure_fails_closed_and_closes_the_session(self) -> None:
        def broken(_db, _user_id, closures=None):
            raise OperationalError("SELECT", {}, Exception("connection lost"))

        with patch.object(access_visibility_service, "matching_grants", broken), \
                self.assertLogs("app.routers.product_analytics", level="WARNING"):
            response = self._ingest(_event("root_tab.selected", target_id="root-doc-1"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self._rows(), [])  # nothing stored with an unchecked id
        record = self.worker_sessions[0]
        self.assertTrue(record["rolled_back"])
        self.assertEqual(record["closed_in"], record["thread"])
        self.assertEqual(self.limiter.consumed, [])

    def test_unexpected_error_still_closes_the_worker_session(self) -> None:
        client = TestClient(self.app, raise_server_exceptions=False)
        with patch.object(service, "_insert_ignoring_duplicates", side_effect=RuntimeError("boom")):
            response = client.post("/v2/product-analytics/events", json={"events": [_event()]})
        self.assertEqual(response.status_code, 500)
        record = self.worker_sessions[0]
        self.assertTrue(record["rolled_back"])
        self.assertEqual(record["closed_in"], record["thread"])
        self.assertEqual(self.limiter.consumed, [])

    def test_rejections_before_the_worker_open_no_session(self) -> None:
        self.assertEqual(self._ingest(*[_event() for _ in range(26)]).status_code, 422)
        self.assertEqual(self.client.post("/v2/product-analytics/events", content=b"{not json",
                                          headers={"Content-Type": "application/json"}).status_code, 422)
        self.assertEqual(self.client.post("/v2/product-analytics/events", content=b"x" * (65 * 1024),
                                          headers={"Content-Type": "application/json"}).status_code, 413)
        self.limiter.retry_after = 30
        self.assertEqual(self._ingest(_event()).status_code, 429)
        self.assertEqual(self.worker_sessions, [])
        self.assertEqual(self.request_sessions, 0)

    # ── Read-time names (admin view only) ─────────────────────────────────

    def test_admin_view_resolves_current_names_without_storing_them(self) -> None:
        self._ingest(
            _event("navigation_tab.selected", target_id="nav-doc-1"),
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("thread_item.opened", component_id="cmp-thread", target_id="7", properties={}),
            _event("blocknote_tab.selected", component_id="cmp-sbn", target_id="cmp-sbn-child"),
            _event("detail_card.details_opened", component_id="cmp-cards", target_id="card-a", properties={}),
        )
        self.current = ADMIN
        data = self.client.get("/v2/product-analytics/events").json()["data"]
        names = {(e["event_name"], e["component_name"], e["target_name"]) for e in data}
        self.assertEqual(names, {
            ("navigation_tab.selected", None, "Jane Doe Review Hub"),
            ("root_tab.selected", None, "Jane Doe Salary"),
            ("thread_item.opened", "Jane Doe threads", "Jane Doe private thread"),
            ("blocknote_tab.selected", "Jane Doe notebook", "Jane Doe private page"),
            # Cards stay opaque: no name is resolved for them.
            ("detail_card.details_opened", "Jane Doe cards", None),
        })
        with self.engine.connect() as conn:
            stored = json.dumps([list(r) for r in conn.execute(text("SELECT * FROM hub_product_events")).all()], default=str)
        self.assertNotIn("Jane", stored)
        export = self.client.get("/v2/product-analytics/export").text
        self.assertNotIn("Jane", export)
        header = export.splitlines()[0].split(",")
        self.assertFalse({"component_name", "target_name"} & set(header))
        # The protected admin summary now has a separate current-location
        # view. Names are still absent from persistence and exports above.
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertIn("Jane Doe Review Hub", {item["label"] for item in summary["by_location"]})

    def test_admin_view_derives_safe_open_destinations_without_storing_them(self) -> None:
        """Admins can inspect current Hub context, including a mirror's source.

        Destinations are calculated only for this protected response. They
        must never become analytics rows, CSV data, or a way for a browser to
        supply a redirect URL.
        """
        self._ingest(
            _event("navigation_tab.selected", target_id="nav-doc-1"),
            _event("root_tab.selected", target_id="root-doc-1"),
            _event("view_tab.selected", target_id="child-doc-1"),
            _event("link.clicked", component_id="cmp-links", properties={}),
            _event("detail_card.details_opened", component_id="cmp-mirror", target_id="card-a", properties={}),
        )
        self.current = ADMIN
        events = {event["event_name"]: event for event in self.client.get("/v2/product-analytics/events").json()["data"]}
        self.assertEqual(events["navigation_tab.selected"]["open_path"], "/jane-doe-review-hub")
        self.assertEqual(events["root_tab.selected"]["open_path"], "/jane-doe-review-hub/jane-doe-salary")
        self.assertEqual(events["view_tab.selected"]["open_path"], "/jane-doe-review-hub/jane-doe-salary/jane-doe-medical-leave")
        self.assertEqual(events["link.clicked"]["open_component_link"], "cmp-links")
        self.assertEqual(
            events["link.clicked"]["open_path"],
            "/jane-doe-review-hub/jane-doe-salary?component=cmp-links",
        )
        mirror = events["detail_card.details_opened"]
        self.assertEqual(mirror["open_component_link"], "cmp-cards")
        self.assertEqual(
            mirror["open_path"],
            "/jane-doe-review-hub/jane-doe-salary?component=cmp-cards",
        )
        self.assertEqual(mirror["mirror_component_link"], "cmp-mirror")
        with self.engine.connect() as conn:
            stored = json.dumps([list(r) for r in conn.execute(text("SELECT * FROM hub_product_events")).all()], default=str)
        self.assertNotIn("open_component_link", stored)
        self.assertNotIn("open_path", stored)
        self.assertNotIn("mirror_component_link", self.client.get("/v2/product-analytics/export").text)

    def test_deleted_records_stay_opaque(self) -> None:
        self._ingest(_event("root_tab.selected", target_id="root-doc-1"))
        with self.Session() as session:
            session.execute(text("UPDATE tabs SET document_id = 'gone' WHERE document_id = 'root-doc-1'"))
            session.commit()
        self.current = ADMIN
        (event,) = self.client.get("/v2/product-analytics/events").json()["data"]
        self.assertEqual((event["target_id"], event["target_name"]), ("root-doc-1", None))

    def test_names_are_not_logged(self) -> None:
        self._ingest(_event("root_tab.selected", target_id="root-doc-1"))
        self.current = ADMIN
        with self.assertLogs(level="DEBUG") as captured:
            logging.getLogger("qa").debug("marker")
            self.client.get("/v2/product-analytics/events")
        self.assertFalse(any("Jane" in line for line in captured.output))

    def test_only_allowlisted_properties_are_kept(self) -> None:
        self._ingest(_event(properties={
            "tab_kind": "variant", "reselect": True, "depth": 2, "item_index": 3,
            "view": "history", "direction": "next", "button_style": "arrow", "link_style": "button",
            "destination_kind": "email",
            "depth_bad": 1, "reselect_str": "yes", "full_url": "https://x.test/?a=1", "document_body": "text",
        }))
        (row,) = self._rows()
        self.assertEqual(row.properties, {
            "tab_kind": "variant", "reselect": True, "depth": 2, "item_index": 3,
            "view": "history", "direction": "next", "button_style": "arrow", "link_style": "button",
            "destination_kind": "email",
        })

    # ── Rate limiting (shared Upstash limiter, fail-open) ────────────────

    def test_over_limit_returns_429_with_retry_after_and_stores_nothing(self) -> None:
        self.limiter.retry_after = 42
        response = self._ingest(_event())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "42")
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.limiter.consumed, [])

    def test_limit_is_per_authenticated_user_and_consumed_only_on_success(self) -> None:
        self._ingest(_event())
        self.assertEqual(self.limiter.checks, [("product_analytics", "member@renphil.org")])
        self.assertEqual(self.limiter.consumed, [("product_analytics", "member@renphil.org")])
        self._ingest(*[_event() for _ in range(51)])  # 422: not consumed
        self.assertEqual(len(self.limiter.consumed), 1)

    def test_limit_bucket_is_bounded(self) -> None:
        # 25-event batches, flushed at most every ~5 s by the browser: 20/min
        # never limits a person and caps a runaway client at 500 events/min.
        self.assertEqual(RATE_LIMITS["product_analytics"], (20, 60))
        self.assertLessEqual(RATE_LIMITS["product_analytics"][0] * service.MAX_BATCH, 500)

    def test_limiter_fails_open_without_upstash_or_on_errors(self) -> None:
        disabled = RateLimitService(SimpleNamespace(UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None))
        self.assertIsNone(asyncio.run(disabled.check("product_analytics", "a@renphil.org")))

        class Broken:
            async def get(self, _key):
                raise ConnectionError("upstash down")

            async def eval(self, *_args):
                raise ConnectionError("upstash down")

        failing = RateLimitService(SimpleNamespace(UPSTASH_REDIS_REST_URL="https://u.test", UPSTASH_REDIS_REST_TOKEN="t"))
        failing._client = Broken()
        self.assertIsNone(asyncio.run(failing.check("product_analytics", "a@renphil.org")))
        asyncio.run(failing.consume("product_analytics", "a@renphil.org"))  # must not raise

    # ── Authorization of admin views ─────────────────────────────────────

    def test_admin_reads_require_all_scopes_admin(self) -> None:
        self._ingest(_event())
        for path in ("summary", "events", "export", "catalog"):
            self.current = MEMBER
            denied = self.client.get(f"/v2/product-analytics/{path}")
            self.assertEqual(denied.status_code, 403, path)
            self.current = ADMIN
            allowed = self.client.get(f"/v2/product-analytics/{path}")
            self.assertEqual(allowed.status_code, 200, path)

    def test_ingest_does_not_require_admin(self) -> None:
        self.current = MEMBER
        self.assertEqual(self._ingest(_event()).status_code, 200)

    # ── Filtering, summary and export ────────────────────────────────────

    def _seed(self) -> None:
        now = datetime.now(timezone.utc)
        self.current = MEMBER
        self._ingest(
            _event("root_tab.selected", page_route="/dashboard/finance"),
            _event("view_tab.selected", trigger="default", page_route="/dashboard/finance/us"),
            _event("button.clicked", page_route="/hr/benefits", properties={}),
            _event("link.clicked", page_route="/dashboard/hr", occurred_at=(now - timedelta(hours=23)).isoformat(), properties={}),
        )
        self.current = ADMIN
        self._ingest(_event("profile_menu.go_to_profile", page_route="/dashboard", properties={}))

    def test_filters(self) -> None:
        self._seed()
        today = datetime.now(timezone.utc).date().isoformat()

        def total(**params) -> int:
            return self.client.get("/v2/product-analytics/events", params=params).json()["total"]

        self.current = ADMIN
        self.assertEqual(total(), 5)
        self.assertEqual(total(actor_email="MEMBER@renphil.org"), 4)
        self.assertEqual(total(page_route="/dashboard"), 4)
        self.assertEqual(total(page_route="/dashboard/:tab"), 3)
        self.assertEqual(total(page_route="/dashboard/:tab/:tab"), 1)
        self.assertEqual(total(page_route="/:nav_tab"), 1)
        # Titles are not stored, so filtering by one finds nothing.
        self.assertEqual(total(page_route="/dashboard/finance"), 0)
        self.assertEqual(total(component_type="root_tab"), 1)
        self.assertEqual(total(event_name="button.clicked"), 1)
        self.assertEqual(total(action="select"), 2)
        self.assertEqual(total(trigger="default"), 1)
        self.assertEqual(total(environment="sandbox"), 5)
        # This Sandbox deployment ignores a cross-environment query.
        self.assertEqual(total(environment="production"), 5)
        self.assertEqual(total(date_from="2000-01-01", date_to="2000-01-02"), 0)
        self.assertGreaterEqual(total(date_from=today, date_to=today), 1)

    def test_nonlocal_deployments_are_server_locked_to_their_configured_environment(self) -> None:
        """A forged query parameter cannot cross the Sandbox/Production wall.

        Local is intentionally the sole operator environment allowed to
        compare the separately stamped datasets.
        """
        self._seed()
        now = datetime.now(timezone.utc)
        with self.Session() as db:
            db.add(HubProductEventV2(
                event_id=str(uuid.uuid4()), event_name="button.clicked",
                component_type="button", action="click", trigger="click",
                cascade_id=None, hub_user_id=1, actor_email="admin@renphil.org",
                environment="production", page_route="/dashboard", component_id=None,
                target_id=None, properties={}, occurred_at=now, received_at=now,
            ))
            db.commit()

        self.current = ADMIN
        self.environment = "production"
        # Supplying sandbox is ignored for every admin read/export surface.
        self.assertEqual(self.client.get("/v2/product-analytics/summary", params={"environment": "sandbox"}).json()["totals"]["events"], 1)
        self.assertEqual(self.client.get("/v2/product-analytics/events", params={"environment": "sandbox"}).json()["total"], 1)
        self.assertIn("production", self.client.get("/v2/product-analytics/export", params={"environment": "sandbox"}).text)
        self.assertNotIn("sandbox", self.client.get("/v2/product-analytics/export", params={"environment": "sandbox"}).text)
        catalog = self.client.get("/v2/product-analytics/catalog").json()
        self.assertEqual(catalog["environment"], "production")
        self.assertTrue(catalog["environment_filter_locked"])

        self.environment = "local"
        self.assertEqual(self.client.get("/v2/product-analytics/events", params={"environment": "sandbox"}).json()["total"], 5)
        self.assertEqual(self.client.get("/v2/product-analytics/events", params={"environment": "production"}).json()["total"], 1)
        self.assertFalse(self.client.get("/v2/product-analytics/catalog").json()["environment_filter_locked"])

    def test_page_route_filter_treats_wildcards_literally(self) -> None:
        self._seed()
        self.current = ADMIN
        self.assertEqual(
            self.client.get("/v2/product-analytics/events", params={"page_route": "/%"}).json()["total"], 0,
        )

    def test_summary_counts(self) -> None:
        self._seed()
        self.current = ADMIN
        body = self.client.get("/v2/product-analytics/summary", params={"actor_email": "member@renphil.org"}).json()
        self.assertEqual(body["totals"], {"events": 4, "users": 1})
        self.assertEqual({item["key"]: item["count"] for item in body["by_trigger"]}, {"click": 3, "default": 1})
        self.assertEqual(sum(item["count"] for item in body["by_day"]), 4)

    def test_pages_summary_uses_current_readable_destinations_not_route_templates(self) -> None:
        self.current = MEMBER
        self._ingest(
            _event("root_tab.selected", target_id="root-doc-1", page_route="/secret/title", properties={}),
            _event("link.clicked", component_id="cmp-links", page_route="/secret/title", properties={}),
            _event("detail_card.details_opened", component_id="cmp-mirror", target_id="card-a", page_route="/secret/title", properties={}),
        )
        self.current = ADMIN
        locations = self.client.get("/v2/product-analytics/summary").json()["by_location"]
        self.assertEqual(
            {(item["label"], item["href"], item["count"], item["mirror"]) for item in locations},
            {
                ("Jane Doe Salary", "/jane-doe-review-hub/jane-doe-salary", 1, False),
                ("Jane Doe cards", "/jane-doe-review-hub/jane-doe-salary?component=cmp-cards", 1, True),
                ("Jane Doe links", "/jane-doe-review-hub/jane-doe-salary?component=cmp-links", 1, False),
            },
        )
        self.assertNotIn("secret/title", repr(locations))

    def test_legacy_component_uri_is_replaced_with_the_current_readable_destination(self) -> None:
        """A pre-release literal URI may help locate a widget, but is never
        displayed or followed unless its current placement still matches."""
        from app.services.product_analytics_refs import summarize_locations

        raw_uri = "/jane-doe-review-hub/jane-doe-salary?component=cmp-links"
        with self.Session() as db:
            locations = summarize_locations(
                db,
                [{
                    "event_name": "link.clicked", "component_id": None,
                    "target_id": None, "page_route": raw_uri, "count": 2,
                }],
            )
        self.assertEqual(locations, [{
            "key": "path:/jane-doe-review-hub/jane-doe-salary?component=cmp-links",
            "label": "Jane Doe links",
            "href": "/jane-doe-review-hub/jane-doe-salary?component=cmp-links",
            "count": 2,
            "mirror": False,
        }])
        self.assertNotIn(raw_uri, locations[0]["label"])

    def test_unresolved_legacy_uri_never_exposes_the_raw_path_or_component_id(self) -> None:
        from app.services.product_analytics_refs import summarize_locations

        raw_uri = "/private-review?component=not-a-real-component"
        with self.Session() as db:
            locations = summarize_locations(
                db,
                [{"event_name": "link.clicked", "component_id": None, "target_id": None,
                  "page_route": raw_uri, "count": 1}],
            )
        self.assertEqual(locations[0]["label"], "Deleted or unavailable destination")
        self.assertIsNone(locations[0]["href"])
        self.assertNotIn("private-review", repr(locations))
        self.assertNotIn("not-a-real-component", repr(locations))

    def test_legacy_uri_cannot_relabel_an_action_as_the_wrong_component_type(self) -> None:
        """A stale component parameter is only a lookup hint, never evidence.

        A button cannot become a readable Link destination merely because an
        old client happened to place its id in a link-click route.
        """
        from app.services.product_analytics_refs import summarize_locations

        raw_uri = "/jane-doe-review-hub/jane-doe-salary?component=cmp-button"
        with self.Session() as db:
            locations = summarize_locations(
                db,
                [{"event_name": "link.clicked", "component_id": None, "target_id": None,
                  "page_route": raw_uri, "count": 1}],
            )
        self.assertEqual(locations[0]["label"], "Deleted or unavailable destination")
        self.assertIsNone(locations[0]["href"])
        self.assertNotIn("cmp-button", repr(locations))

    def test_legacy_uri_needs_the_same_page_an_expected_widget_type_and_current_access(self) -> None:
        from app.services.product_analytics_refs import summarize_locations

        raw_uri = "/jane-doe-review-hub/jane-doe-salary?component=cmp-links"
        cases = {
            # The widget has since moved to another page.
            "moved": ("link.clicked", "/jane-doe-review-hub/old-page?component=cmp-links", None),
            # Selection events have no widget, so a component hint proves nothing.
            "no widget event": ("root_tab.selected", raw_uri, None),
            # The reader cannot currently open that widget.
            "no access": ("link.clicked", raw_uri, [grant("nav_tab", 2, "view")]),
        }
        for name, (event_name, route, grants) in cases.items():
            with self.subTest(name):
                self.grants = {MEMBER.hub_user_id: grants or [grant("hub", 1)]}
                with self.Session() as db:
                    access = product_analytics._viewer_access(db, MEMBER) if grants else None
                    locations = summarize_locations(db, [{
                        "event_name": event_name, "component_id": None, "target_id": None,
                        "page_route": route, "count": 1,
                    }], access=access)
                self.assertEqual(locations, [{"key": "unresolved", "label": "Deleted or unavailable destination",
                                              "href": None, "count": 1, "mirror": False}])

    def test_event_list_destinations_are_current_names_never_routes_or_ids(self) -> None:
        self.current = ADMIN
        self._ingest(
            _event("link.clicked", component_id="cmp-links", page_route="/secret/title", properties={}),
            _event("button.clicked", component_id="cmp-button", page_route="/secret/title", properties={}),
        )
        with self.Session() as db:
            db.query(ComponentV2).filter(ComponentV2.link == "cmp-button").delete()
            db.commit()
        data = self.client.get("/v2/product-analytics/events").json()["data"]
        destinations = {(event["component_id"], event["destination_label"], event["destination_href"]) for event in data}
        self.assertEqual(destinations, {
            ("cmp-links", "Jane Doe links", "/jane-doe-review-hub/jane-doe-salary?component=cmp-links"),
            ("cmp-button", "Deleted or unavailable destination", None),
        })
        for event in data:
            self.assertNotIn("secret", event["destination_label"])
            self.assertNotIn("cmp-", event["destination_label"])

    def test_component_destination_uses_current_revealed_page_access(self) -> None:
        """A component grant may use its current, ACL-revealed containing page."""
        from app.services.product_analytics_refs import resolve_names

        with self.Session() as db:
            links = node(db, "cmp-links")
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant(*links)]}
        with self.Session() as db:
            access = product_analytics._viewer_access(db, MEMBER)
            (event,) = resolve_names(
                db,
                [{"event_name": "link.clicked", "component_id": "cmp-links", "target_id": None, "page_route": "/"}],
                access=access,
            )
        self.assertEqual(event["component_name"], "Jane Doe links")
        self.assertEqual(event["open_path"], "/jane-doe-review-hub/jane-doe-salary?component=cmp-links")
        self.assertEqual(event["open_component_link"], "cmp-links")

    def test_unnamed_component_uses_a_readable_type_label_and_live_page_path(self) -> None:
        """An optional widget title must never make a UUID the visible label."""
        from app.services.product_analytics_refs import resolve_names

        with self.Session() as db:
            db.query(ComponentV2).filter(ComponentV2.link == "cmp-links").update({"title": "  "})
            db.commit()
        with self.Session() as db:
            (event,) = resolve_names(
                db,
                [{"event_name": "link.clicked", "component_id": "cmp-links", "target_id": None, "page_route": "/"}],
            )
        self.assertEqual(event["component_name"], "Links component")
        self.assertEqual(event["open_component_name"], "Links component")
        self.assertEqual(event["open_path"], "/jane-doe-review-hub/jane-doe-salary?component=cmp-links")

    def test_events_pagination_is_bounded(self) -> None:
        self._seed()
        self.current = ADMIN
        body = self.client.get("/v2/product-analytics/events", params={"page_size": 2, "page": 2}).json()
        self.assertEqual((body["page"], body["page_size"], len(body["data"]), body["total"]), (2, 2, 2, 5))
        self.assertEqual(self.client.get("/v2/product-analytics/events", params={"page_size": 10_000}).json()["page_size"], 200)

    def test_export_respects_filters(self) -> None:
        self.current = MEMBER
        self._ingest(_event("root_tab.selected"), _event("button.clicked", properties={}))
        self.current = ADMIN
        response = self.client.get("/v2/product-analytics/export", params={"component_type": "root_tab"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("hub-product-usage.csv", response.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_name"], "root_tab.selected")
        self.assertEqual(rows[0]["environment"], "sandbox")
        self.assertEqual(list(rows[0].keys()), service.EXPORT_COLUMNS)

    def test_csv_cells_are_formula_safe(self) -> None:
        self.assertEqual(service._csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(service._csv_safe("/dashboard"), "/dashboard")


    # ── Excel workbook (same style as the Agent analytics workbook) ───────

    @staticmethod
    def _xlsx(content: bytes) -> dict:
        import re
        import zipfile

        package = zipfile.ZipFile(io.BytesIO(content))
        names = package.namelist()
        workbook_xml = package.read("xl/workbook.xml").decode()
        shared = package.read("xl/sharedStrings.xml").decode() if "xl/sharedStrings.xml" in names else ""
        everything = "\n".join(package.read(n).decode("utf-8", "ignore") for n in names if n.endswith(".xml") or n.endswith(".rels"))
        sheets_xml = [package.read(n).decode() for n in names if n.startswith("xl/worksheets/sheet")]
        return {
            "sheets": re.findall(r'<sheet name="([^"]+)"', workbook_xml),
            "charts": [n for n in names if n.startswith("xl/charts/chart")],
            "strings": shared,
            "everything": everything,
            "formulas": sum(xml.count("<f>") + xml.count("<f ") for xml in sheets_xml),
        }

    def _seed_usage(self) -> None:
        self.current = MEMBER
        self._ingest(
            _event("navigation_tab.selected", target_id="nav-doc-1", properties={"tab_kind": "nav_tab"}),
            _event("root_tab.selected", target_id="root-doc-1", trigger="default", cascade_id=str(uuid.uuid4())),
            _event("root_tab.selected", target_id="root-doc-1", properties={"tab_kind": "root", "reselect": True}),
            _event("link.clicked", component_id="cmp-links", properties={"destination_kind": "external"}),
        )
        self.current = ADMIN
        self._ingest(_event("button.clicked", component_id="cmp-button", properties={"destination_kind": "internal"}))

    def test_workbook_has_charts_and_data_without_emails_by_default(self) -> None:
        self._seed_usage()
        response = self.client.get("/v2/product-analytics/export/xlsx", params={"time_zone": "Asia/Beirut"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn('filename="hub-product-usage.xlsx"', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        book = self._xlsx(response.content)
        self.assertEqual(book["sheets"], ["Overview", "Activity by day", "By action", "By component", "Top pages", "Active time", "Events"])
        self.assertGreaterEqual(len(book["charts"]), 5)  # doughnut + one per report sheet
        for text in ("Sidebar navigation tab opened", "Root tab opened", "Reselect", "Default", "/dashboard/:tab", "Asia/Beirut", "not included"):
            self.assertIn(text, book["strings"], text)
        # No person's email anywhere in the package, and no live names.
        self.assertNotIn("renphil.org", book["everything"])
        self.assertNotIn("Jane Doe", book["everything"])
        self.assertEqual(book["formulas"], 0)

    def test_workbook_includes_people_only_when_asked(self) -> None:
        self._seed_usage()
        response = self.client.get("/v2/product-analytics/export/xlsx", params={"include_people": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="hub-product-usage-with-people.xlsx"', response.headers["content-disposition"])
        book = self._xlsx(response.content)
        self.assertEqual(book["sheets"], ["Overview", "Activity by day", "By action", "By component", "Top pages", "Active time", "Top people", "Visited by person", "Events"])
        self.assertIn("member@renphil.org", book["strings"])
        self.assertIn("admin@renphil.org", book["strings"])
        self.assertGreaterEqual(len(book["charts"]), 7)  # + Top people pie + per-person stacked bar
        self.assertEqual(book["formulas"], 0)

    def test_people_workbook_shows_what_each_person_used_with_live_names(self) -> None:
        import re
        import zipfile

        self._seed_usage()
        content = self.client.get("/v2/product-analytics/export/xlsx", params={"include_people": "true"}).content
        book = self._xlsx(content)
        # Live names, looked up at export time (never stored).
        for name in ("Jane Doe Review Hub", "Jane Doe Salary", "Jane Doe links", "Jane Doe button"):
            self.assertIn(name, book["strings"], name)
        self.assertNotIn("Jane Doe", repr([(r.target_id, r.component_id, r.properties) for r in self._rows()]))
        # Per-person headline rows with click / reselect / default counts.
        self.assertIn("member@renphil.org  ·  4 events (2 clicks, 1 reselects, 1 defaults)  ·  3 items", book["strings"])
        self.assertIn("admin@renphil.org  ·  1 events (1 clicks, 0 reselects, 0 defaults)  ·  1 items", book["strings"])
        # Rows are grouped (outline level 1) so each person can be collapsed.
        package = zipfile.ZipFile(io.BytesIO(content))
        names = re.findall(r'<sheet name="([^"]+)" sheetId="(\d+)"', package.read("xl/workbook.xml").decode())
        index = [n for n, _ in names].index("Visited by person") + 1
        sheet = package.read(f"xl/worksheets/sheet{index}.xml").decode()
        self.assertIn('outlineLevel="1"', sheet)
        # The stacked "per person by component" chart is present.
        charts = "".join(package.read(n).decode() for n in package.namelist() if n.startswith("xl/charts/chart"))
        self.assertIn("Clicks per person by component", charts)
        self.assertIn('<c:grouping val="stacked"/>', charts)

    def test_default_workbook_still_has_no_names_or_people_sheets(self) -> None:
        self._seed_usage()
        book = self._xlsx(self.client.get("/v2/product-analytics/export/xlsx").content)
        self.assertNotIn("Visited by person", book["sheets"])
        self.assertNotIn("Jane Doe", book["everything"])
        self.assertNotIn("renphil.org", book["everything"])

    def test_workbook_user_filter_does_not_leak_the_email(self) -> None:
        self._seed_usage()
        response = self.client.get("/v2/product-analytics/export/xlsx", params={"actor_email": "member@renphil.org"})
        book = self._xlsx(response.content)
        self.assertNotIn("renphil.org", book["everything"])
        self.assertIn("one person, email not included", book["strings"])

    def test_workbook_respects_filters_and_admin_gate(self) -> None:
        self._seed_usage()
        response = self.client.get("/v2/product-analytics/export/xlsx", params={"component_type": "root_tab"})
        book = self._xlsx(response.content)
        self.assertIn("Root tab opened", book["strings"])
        self.assertNotIn("Link component item clicked", book["strings"])
        self.current = MEMBER
        self.assertEqual(self.client.get("/v2/product-analytics/export/xlsx").status_code, 403)
        self.assertEqual(self.client.get("/v2/product-analytics/export/xlsx", params={"include_people": "true"}).status_code, 403)

    def test_workbook_with_no_events_still_opens(self) -> None:
        self.current = ADMIN
        response = self.client.get("/v2/product-analytics/export/xlsx", params={"date_from": "2020-01-01", "date_to": "2020-01-03"})
        self.assertEqual(response.status_code, 200, response.text[:500])
        book = self._xlsx(response.content)
        self.assertEqual(book["sheets"][0], "Overview")
        self.assertIn("No matching events", book["strings"])

    def test_workbook_day_series_is_zero_filled_without_changing_counts(self) -> None:
        from datetime import date as _date

        from app.routers.product_analytics_workbook import _day_series

        series = _day_series([{"day": "2026-10-27", "count": 3}], service.EventFilters())
        self.assertEqual(len(series), 14)
        self.assertEqual(series[0], ("2026-10-14", 0))
        self.assertEqual(series[-1], ("2026-10-27", 3))
        self.assertEqual(sum(c for _, c in series), 3)
        ranged = _day_series([], service.EventFilters(date_from=_date(2026, 10, 1), date_to=_date(2026, 10, 3)))
        self.assertEqual([d for d, _ in ranged], ["2026-10-01", "2026-10-02", "2026-10-03"])

    def test_workbook_never_writes_formulas_or_links(self) -> None:
        from app.routers.product_analytics_workbook import build_product_usage_workbook

        hostile = '=HYPERLINK("http://evil.test","x")'
        event = {
            "event_id": hostile, "event_name": "root_tab.selected", "component_type": "root_tab", "action": "select",
            "trigger": "click", "cascade_id": None, "actor_email": "+cmd@renphil.org", "environment": "sandbox",
            "page_route": hostile, "component_id": "@SUM(1,2)", "target_id": "-2+3", "properties": {},
            "occurred_at": "2026-10-27T09:00:00+00:00", "received_at": "2026-10-27T09:00:00+00:00",
        }
        summary = {
            "totals": {"events": 1, "users": 1}, "by_event": [{"key": "root_tab.selected", "count": 1}],
            "by_component": [{"key": "root_tab", "count": 1}], "by_trigger": [{"key": "click", "count": 1}],
            "by_environment": [{"key": "sandbox", "count": 1}], "by_user": [{"key": "+cmd@renphil.org", "count": 1}],
            "by_page": [{"key": hostile, "count": 1}], "by_day": [{"day": "2026-10-27", "count": 1}],
        }
        content = build_product_usage_workbook(
            summary=summary, events=[event], total_events=1, filters=service.EventFilters(),
            include_people=True, environment="sandbox", viewer_time_zone="UTC",
        )
        book = self._xlsx(content)
        self.assertEqual(book["formulas"], 0)
        self.assertNotIn("<hyperlink", book["everything"])
        self.assertIn("HYPERLINK", book["strings"])  # kept as plain text


class CatalogTests(unittest.TestCase):
    def test_catalog_covers_the_17_requested_actions_with_18_events(self) -> None:
        requested = {
            1: {"navigation_tab.selected"}, 2: {"root_tab.selected"}, 3: {"view_tab.selected"},
            4: {"sub_tab.selected"}, 5: {"link.clicked"}, 6: {"blocknote_tab.selected"},
            7: {"thread_item.opened"}, 8: {"airtable_metric_card.opened"}, 9: {"quick_action_link.clicked"},
            10: {"detail_card.details_opened"}, 11: {"button.clicked"}, 12: {"calendar.add_myself"},
            13: {"booking.schedule_now"},
            # "Resource Carousel arrow / open-document action": two distinct events.
            14: {"resource_carousel.document_opened", "resource_carousel.scrolled"},
            15: {"threads_management.tab_selected"}, 16: {"revisions_management.tab_selected"},
            17: {"profile_menu.go_to_profile"},
        }
        mapped = set().union(*requested.values())
        self.assertEqual(len(requested), 17)
        self.assertEqual(mapped, set(service.EVENT_CATALOG))
        self.assertEqual(len(service.EVENT_CATALOG), 18)

    def test_link_quick_action_and_carousel_events_are_distinct(self) -> None:
        kinds = {name: (e.component_type, e.action) for name, e in service.EVENT_CATALOG.items()}
        distinct = [kinds[n] for n in (
            "link.clicked", "quick_action_link.clicked",
            "resource_carousel.document_opened", "resource_carousel.scrolled",
        )]
        self.assertEqual(len(set(distinct)), 4)

    def test_no_shared_storage_with_agent_telemetry(self) -> None:
        self.assertEqual(HubProductEventV2.__tablename__, "hub_product_events")
        import inspect
        source = inspect.getsource(service) + inspect.getsource(product_analytics)
        self.assertNotIn("bot_telemetry_runs", source.replace("`bot_telemetry_*`", ""))


class EnvironmentSettingTests(unittest.TestCase):
    def test_hub_environment_never_guesses(self) -> None:
        from app.config import Settings

        def env(value: str) -> str:
            return Settings.hub_environment.fget(SimpleNamespace(HUB_ENVIRONMENT=value))

        self.assertEqual(env("Sandbox "), "sandbox")
        self.assertEqual(env("production"), "production")
        self.assertEqual(env(""), "unconfigured")
        self.assertEqual(env("prod"), "unconfigured")


if __name__ == "__main__":
    unittest.main()
