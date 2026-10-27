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
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
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
from app.db_v2.models.product_analytics import HubProductEventV2
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
    ComponentV2.__table__, ThreadV2.__table__, HubProductEventV2.__table__,
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
            patch.object(product_analytics, "get_settings", lambda: SimpleNamespace(hub_environment="sandbox")),
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

    def _ingest(self, *events: dict):
        return self.client.post("/v2/product-analytics/events", json={"events": list(events)})

    def _rows(self) -> list[HubProductEventV2]:
        with self.Session() as db:
            return db.query(HubProductEventV2).order_by(HubProductEventV2.id).all()

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
        summary = self.client.get("/v2/product-analytics/summary").text
        self.assertNotIn("Jane", summary)

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
        self.assertEqual(total(environment="production"), 0)
        self.assertEqual(total(date_from="2000-01-01", date_to="2000-01-02"), 0)
        self.assertGreaterEqual(total(date_from=today, date_to=today), 1)

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
        self.assertEqual(book["sheets"], ["Overview", "Activity by day", "By action", "By component", "Top pages", "Events"])
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
        self.assertEqual(book["sheets"], ["Overview", "Activity by day", "By action", "By component", "Top pages", "Top people", "Visited by person", "Events"])
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
