"""
Tests for the Thread widget's schema + backend API
(plan_thread_widget_2026-08-17.md) — Phase 1 only.

Covers the subset of the plan's §10 backend test list that Phase 1 actually
implements (threads/comments/votes, access checks, pagination, counter
recompute). Deliberately does NOT test mentions, notifications, the mention
cap, write rate limits, or excerpt generation — those are Phase 3/4 and are
not built yet; see app/services/thread_service.py's module docstring for
why. See the Phase 1 session handoff for the full list of what's covered vs.
deferred.

Same in-memory-SQLite router-fixture pattern as
tests/test_airtable_widget_cache.py's `_AirtableCacheFixture` — a real
FastAPI TestClient over a real (SQLite) v2 schema, JWT minted locally, so
these exercise the actual router + service + ORM layer, not a mocked
service.

SQLite foreign_keys enforcement is OFF by default per-connection; it is
turned ON explicitly in `_ThreadFixture.setUp` (a `PRAGMA foreign_keys=ON`
on every new DBAPI connection) so the `ON DELETE CASCADE` FKs this feature
depends on (plan §11.1) are actually exercised here, the same way Postgres
would enforce them in production — without it, every cascade test below
would silently pass for the wrong reason (nothing would ever actually
enforce the constraint).
"""

from __future__ import annotations

import base64
import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Same fallback-only env defaults as tests/test_airtable_widget_cache.py, so
# this file is independently runnable regardless of import order.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-thread-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.notification import NotificationV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.thread import (  # noqa: E402
    THREAD_WIDGET_TYPE,
    ThreadCommentV2,
    ThreadV2,
    ThreadVoteV2,
)
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import thread_service  # noqa: E402
from app.services.gridstack_service import (  # noqa: E402
    create_tab_v2,
    update_tab_content_v2,
)
from app.services.nav_tab_service import create_nav_tab_v2, get_nav_tab_by_document_id  # noqa: E402

def _treat_example_com_as_org_domain(test: unittest.TestCase) -> None:
    """This module's @example.com personas stand in for signed-in staff.
    Since the delete-user re-creation fix (plan_admin_delete_user_2026-10-18.md
    §4) the lazy sign-in path only provisions ORG-DOMAIN emails, so
    example.com is made one here. Provisioning itself is tested in
    test_admin_delete_user.py."""
    # `get_settings()` builds a fresh Settings each call, so the env var is what to patch.
    patcher = mock.patch.dict(os.environ, {"ALLOWED_EMAIL_DOMAIN": "renphil.org,example.com"})
    patcher.start()
    test.addCleanup(patcher.stop)


class _FakeRateLimitService:
    """Test double for `RateLimitService` — no real Upstash round trip, so
    the CRUD/mention/pagination test classes below (none of which are
    ABOUT rate limiting) don't inherit a shared, real, cross-test-run
    Upstash counter. `retry_after` defaults to `None` (every request
    allowed); a test that specifically wants the 429 path sets it before
    making the request. `calls` records every `(action, email)` peeked via
    `check()`; `consumed` records every `(action, email)` actually charged
    via `consume()` — kept separate so a test can assert the 2026-08-24
    fix directly: a request the handler rejects appears in `calls` but
    never in `consumed`."""

    def __init__(self) -> None:
        self.retry_after: int | None = None
        self.calls: list[tuple[str, str]] = []
        self.consumed: list[tuple[str, str]] = []

    async def check(self, action: str, email: str) -> int | None:
        self.calls.append((action, email))
        return self.retry_after

    async def consume(self, action: str, email: str) -> None:
        self.consumed.append((action, email))


class _ThreadFixture(unittest.TestCase):
    """Shared fixture: in-memory SQLite v2 schema (FK enforcement ON), a
    TestClient, JWT minting and a thread-widget seeder. Holds no test
    methods itself."""

    def setUp(self) -> None:
        _treat_example_com_as_org_domain(self)
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        # Every write-path test in this file goes through
        # `@rate_limited(...)`-decorated handlers (plan §4.7 control 2).
        # Patched here, once, for every test class in this file — a real
        # Upstash round trip would (a) make these tests depend on network
        # reachability and (b) share ONE fixed-window counter across every
        # test run in this process, since "alice@example.com" posting a
        # thread in one test and another test an hour of wall-clock time
        # away both hash to a key keyed only on THAT email — see
        # `RateLimitEndpointTests` below for the tests that deliberately
        # exercise the real 429 path against this fake.
        self.fake_rate_limiter = _FakeRateLimitService()
        self._rate_limit_patcher = mock.patch(
            "app.helpers.rate_limit.get_rate_limit_service",
            return_value=self.fake_rate_limiter,
        )
        self._rate_limit_patcher.start()

        self.hub = HubV2(
            document_id="hub-doc",
            access_control={
                "admins": {
                    "users": [],
                    "roles": [{"name": "Hub Admin", "scope": "", "program": "", "function": ""}],
                },
                "viewers": {"users": [], "roles": []},
            },
        )
        self.db.add(self.hub)
        self.db.commit()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

        nav = create_nav_tab_v2(self.db, title="Nav Threads")
        nav_row = get_nav_tab_by_document_id(self.db, nav["documentId"])
        self.root = create_tab_v2(self.db, title="Root Threads", nav_tab_id=nav_row.id)
        root_row = self.db.query(TabV2).filter(TabV2.document_id == self.root["documentId"]).first()
        # The root tab's node id, for ancestor-level grants
        # (plan_thread_moderation_2026-09-18.md §9.1's one-line fixture ask).
        self.root_tab_id = root_row.id
        self.root_gridstack = (
            self.db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == root_row.id, GridstackV2.parent_id.is_(None))
            .first()
        )

    def tearDown(self) -> None:
        self._rate_limit_patcher.stop()
        self.db.close()
        self.engine.dispose()

    # -- auth -------------------------------------------------------------

    def _token(self, email: str, roles: list[str] | None = None) -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": email.split("@")[0], "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        return {"Authorization": f"Bearer {self._token(email, roles)}"}

    def _member_auth(self, email: str) -> dict:
        """plan_ac_enforcement_closeout_2026-09-09.md §4 re-gated every
        `_check_view_access`/`_require_hub_admin` call site onto the
        read-time visibility fold (`ViewerAccess.is_granted`/`.full_access`)
        instead of the component's own legacy `access_control` blob. This
        file's CRUD/mention/vote/notification/rate-limit tests are not ABOUT
        that gate — they seed thread widgets with no `resource_grants` at
        all, the same way `test_tab_locking.py`'s `_editor_auth` re-pointed
        its pre-existing functional tests at a Hub Admin bypass to isolate
        them from ITS new gate. A Hub Admin token trips
        `ViewerAccess.full_access`, which bypasses the fold entirely, so
        these tests keep exercising what they were written to exercise.
        Tests that are actually ABOUT the gate build a real
        `ResourceGrantV2` row and call `_auth` directly — see
        `ThreadAccessControlTests` and `NotificationAccessControlTests`
        below.

        Since phase 3 the JWT "Hub Admin" claim alone makes nobody an admin,
        so this seeds a real `(hub_admin, all_scopes)` assignment for `email`
        (`_seed_real_admin`)."""
        return self._admin_auth(email)

    def _admin_auth(self, email: str) -> dict:
        """A real Hub Admin: `_seed_real_admin` plus a token. The "Hub Admin"
        claim stays on the token (a real admin's JWT carries it) but is not
        what makes them one. Tests about a JWT-ONLY admin call `_auth(...,
        roles=["Hub Admin"])` directly instead."""
        self._seed_real_admin(email)
        return self._auth(email, roles=["Hub Admin"])

    def _seed_real_admin(self, email: str) -> UserV2:
        """Gives `email` a `(hub_admin, universal scope)` role_assignments
        row, the only thing `is_hub_admin` accepts since phase 3 (the
        BOOTSTRAP_ADMIN_EMAILS list is disabled). Reuses a `hub_admin` role
        and universal scope a subclass already seeded; idempotent per email.

        Written through its OWN short-lived session, one commit per row, so
        it neither expires the instances a test holds in `self.db` nor
        leaves a flushed-but-uncommitted row for a leaked request session's
        rollback-on-return to discard (every session here shares the one
        StaticPool connection)."""
        now = thread_service._utc_now()
        normalized = email.strip().lower()
        db = self._session_factory()
        try:
            role = db.query(RoleV2).filter(RoleV2.key == "hub_admin").first()
            if role is None:
                role = RoleV2(key="hub_admin", name="Hub Admin", is_public=False, is_system=False, created_at=now)
                db.add(role)
                db.commit()
            scope = db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).first()
            if scope is None:
                scope = ScopeV2(
                    key="all_scopes",
                    name="All Scopes",
                    is_universal=True,
                    is_public=False,
                    is_system=False,
                    created_at=now,
                )
                db.add(scope)
                db.commit()
            user = db.query(UserV2).filter(UserV2.matches_email(normalized)).first()
            if user is None:
                user = UserV2(work_email=normalized, has_signed_in=True, created_at=now)
                db.add(user)
                db.commit()
            role_id, scope_id, user_id = role.id, scope.id, user.id
            held = (
                db.query(RoleAssignmentV2)
                .filter(
                    RoleAssignmentV2.user_id == user_id,
                    RoleAssignmentV2.role_id == role_id,
                    RoleAssignmentV2.scope_id == scope_id,
                )
                .first()
            )
            if held is None:
                db.add(RoleAssignmentV2(user_id=user_id, role_id=role_id, scope_id=scope_id, created_at=now))
                db.commit()
        finally:
            db.close()
        return self.db.get(UserV2, user_id)

    # -- resource grants (plan_access_control_algorithm_2026-08-27.md §7,
    # D5 — a direct per-user grant, no role/scope/assignment needed) -------

    def _seed_hub_user(self, email: str) -> UserV2:
        """`get_viewer_access` resolves a caller through `hub_users`
        (`get_current_hub_user` auto-provisions a row on first request keyed
        by normalized email, but a `ResourceGrantV2.user_id` grant has to
        name a row that already exists to be reachable by that same
        caller) — pre-seeded here with the same `.strip().lower()`
        normalization `UserV2.email`'s own docstring requires, so a grant
        written against this row is the SAME row the caller's request
        resolves to."""
        normalized = email.strip().lower()
        existing = self.db.query(UserV2).filter(UserV2.email == normalized).first()
        if existing is not None:
            return existing
        row = UserV2(email=normalized, has_signed_in=True, created_at=thread_service._utc_now())
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _grant(self, *, email: str, node: tuple[str, int], level: str = "view") -> None:
        """A direct per-user grant (D5) on `node = (node_kind, node_id)` —
        same shape `test_sbn_access_control.py`'s own `grant()` helper uses.
        Auto-provisions the `hub_users` row via `_seed_hub_user` so callers
        don't have to remember the ordering."""
        user_row = self._seed_hub_user(email)
        node_kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[node_kind]: node_id},
                user_id=user_row.id,
                level=level,
                created_at=thread_service._utc_now(),
            )
        )
        self.db.commit()

    # -- seeders ------------------------------------------------------------

    def _seed_thread_widget(self, link: str, *, access_control: dict | None = None) -> ComponentV2:
        component = ComponentV2(
            link=link,
            title=link,
            type=THREAD_WIDGET_TYPE,
            gridstack_id=self.root_gridstack.id,
            access_control=access_control,
        )
        self.db.add(component)
        self.db.commit()
        self.db.refresh(component)
        return component

    def _insert_thread(
        self,
        component_id: int,
        *,
        title: str = "Thread",
        content: str = "body",
        author_email: str = "author@example.com",
        created_at: datetime | None = None,
        status: str = "approved",
        reviewed_by_email: str | None = None,
        reviewed_by_name: str | None = None,
        reviewed_at: datetime | None = None,
    ) -> ThreadV2:
        """`reviewed_by_email`/`reviewed_by_name`/`reviewed_at` (plan_thread_
        moderation_2026-09-18.md §3.1) default to `None`, matching every
        pre-phase-2a row and an editor's own auto-approved post (M5) — only
        the moderation tests that need a hand-seeded DECIDED row pass them."""
        thread = ThreadV2(
            component_id=component_id,
            title=title,
            content=content,
            author_email=author_email,
            author_name=author_email.split("@")[0],
            mentions=[],
            status=status,
            reviewed_by_email=reviewed_by_email,
            reviewed_by_name=reviewed_by_name,
            reviewed_at=reviewed_at,
            up_count=0,
            down_count=0,
            comment_count=0,
            created_at=created_at or thread_service._utc_now(),
        )
        self.db.add(thread)
        self.db.commit()
        self.db.refresh(thread)
        return thread

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, int]:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        iso, row_id = json.loads(raw)
        return iso, row_id

    def _seed_user(
        self,
        name: str,
        *,
        work_email: str | None,
        end_date: date | None = None,
    ) -> UserV2:
        """A roster (HR) row. Since `hub_users` was folded into `users`
        (2026-10), the roster row and the signed-in row for one email ARE the
        same row — so if `_seed_hub_user` already made it, fill in its HR
        fields instead of inserting a second row for the same work_email."""
        existing = (
            self.db.query(UserV2).filter(UserV2.matches_email(work_email.strip().lower())).first()
            if work_email
            else None
        )
        if existing is not None:
            existing.name = name
            existing.end_date = end_date
            self.db.commit()
            self.db.refresh(existing)
            return existing
        row = UserV2(name=name, work_email=work_email, end_date=end_date)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _seed_mentionable(
        self,
        email: str,
        node: tuple[str, int],
        *,
        name: str | None = None,
        level: str = "view",
        end_date: date | None = None,
    ) -> UserV2:
        """A roster row that is ALSO mentionable on `node`'s subtree under
        M9 (plan_thread_moderation_2026-09-18.md §5.9 / §9.1a): `_seed_user`
        + `_seed_hub_user` + a direct `_grant(level=...)` on `node`. Before
        phase 2m a bare `users` row was enough to be mentioned anywhere (the
        original plan's D7); now the person must be GRANTED on the widget or
        an ancestor — this is the repoint the 2m handoff lists by test name.
        `name` defaults to the email's local part title-cased with the domain
        dropped, matching how the mention tests name people."""
        row = self._seed_user(name or email.split("@")[0].title(), work_email=email, end_date=end_date)
        self._grant(email=email, node=node, level=level)
        return row

    def _seed_notification(
        self,
        *,
        recipient_email: str,
        component_id: int,
        thread_id: int,
        comment_id: int | None = None,
        type_: str = "mention",
        actor_email: str = "actor@example.com",
        actor_name: str = "Actor",
        excerpt: str | None = "an excerpt",
        created_at: datetime | None = None,
        read_at: datetime | None = None,
    ) -> NotificationV2:
        notification = NotificationV2(
            recipient_email=recipient_email,
            type=type_,
            actor_email=actor_email,
            actor_name=actor_name,
            component_id=component_id,
            thread_id=thread_id,
            comment_id=comment_id,
            excerpt=excerpt,
            read_at=read_at,
            created_at=created_at or thread_service._utc_now(),
        )
        self.db.add(notification)
        self.db.commit()
        self.db.refresh(notification)
        return notification


# ---------------------------------------------------------
# Threads — CRUD + access control
# ---------------------------------------------------------


class ThreadCrudTests(_ThreadFixture):
    def test_create_then_list_round_trip(self) -> None:
        self._seed_thread_widget("crud-widget")
        create = self.client.post(
            "/data/threads/component/crud-widget",
            json={"title": "First topic", "content": "Hello world"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(create.status_code, 201, create.text)
        body = create.json()
        self.assertEqual(body["title"], "First topic")
        self.assertEqual(body["author_email"], "alice@example.com")
        # Two counts, no net score — D10 (amended 2026-08-19).
        self.assertEqual(body["up_count"], 0)
        self.assertEqual(body["down_count"], 0)
        self.assertNotIn("score", body)
        self.assertEqual(body["comment_count"], 0)
        self.assertIsNone(body["edited_at"])

        listing = self.client.get(
            "/data/threads/component/crud-widget", headers=self._member_auth("bob@example.com")
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        items = listing.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "First topic")
        self.assertIsNone(listing.json()["next_cursor"])

    def test_unknown_link_is_404(self) -> None:
        resp = self.client.get(
            "/data/threads/component/does-not-exist", headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(resp.status_code, 404)

    def test_link_pointing_at_a_non_thread_widget_is_404(self) -> None:
        # Mirrors gridstack_service._airtable_component_by_link's own "wrong
        # type reads as not found" convention (plan §1.4's template).
        component = ComponentV2(
            link="not-a-thread",
            type="airtable",
            gridstack_id=self.root_gridstack.id,
        )
        self.db.add(component)
        self.db.commit()
        resp = self.client.get(
            "/data/threads/component/not-a-thread", headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(resp.status_code, 404)

    def test_widget_access_control_is_enforced(self) -> None:
        """plan_ac_enforcement_closeout_2026-09-09.md §4 replaced the
        per-widget `access_control` JSONB viewer list this test used to seed
        — that column is no longer read by `_check_view_access` at all — with
        a real `ResourceGrantV2` row on the widget's own `("component", id)`
        node (D5, a direct per-user grant). `allowed` holds one; `outsider`
        is an ordinary authenticated user with none and is refused; Hub
        Admin bypasses the fold regardless of any grant."""
        component = self._seed_thread_widget("restricted-widget")
        self._grant(email="allowed@example.com", node=("component", component.id))

        allowed = self.client.get(
            "/data/threads/component/restricted-widget", headers=self._auth("allowed@example.com")
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

        denied = self.client.get(
            "/data/threads/component/restricted-widget",
            headers=self._auth("outsider@example.com", roles=["Hub Member"]),
        )
        self.assertEqual(denied.status_code, 403)

        # Hub Admin bypasses even though never granted directly.
        admin = self.client.get(
            "/data/threads/component/restricted-widget",
            headers=self._admin_auth("admin@example.com"),
        )
        self.assertEqual(admin.status_code, 200, admin.text)

    def test_creating_a_thread_also_enforces_widget_access(self) -> None:
        # D2's default ("no one") applies — no grant is seeded at all.
        self._seed_thread_widget("restricted-create-widget")
        resp = self.client.post(
            "/data/threads/component/restricted-create-widget",
            json={"title": "T", "content": "c"},
            headers=self._auth("outsider@example.com", roles=["Hub Member"]),
        )
        self.assertEqual(resp.status_code, 403)

    def test_author_can_edit_own_thread_and_edited_marker_appears(self) -> None:
        component = self._seed_thread_widget("edit-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        resp = self.client.patch(
            f"/data/threads/{thread.id}",
            json={"content": "updated body"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["thread"]
        self.assertEqual(body["content"], "updated body")
        self.assertIsNotNone(body["edited_at"])

    def test_empty_patch_does_not_stamp_an_edited_marker(self) -> None:
        component = self._seed_thread_widget("empty-patch-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        resp = self.client.patch(
            f"/data/threads/{thread.id}", json={}, headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["thread"]["edited_at"])

    def test_non_author_patch_is_403(self) -> None:
        component = self._seed_thread_widget("non-author-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        resp = self.client.patch(
            f"/data/threads/{thread.id}",
            json={"content": "hijacked"},
            headers=self._member_auth("mallory@example.com"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_delete_is_403_and_admin_delete_succeeds(self) -> None:
        component = self._seed_thread_widget("delete-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        # Even the author may not delete their own thread (D3). A plain
        # (non-admin) caller here, deliberately NOT `_member_auth` — delete
        # gates on `_require_hub_admin`/`ViewerAccess.full_access` alone, no
        # `_check_view_access`, so this assertion is specifically about the
        # admin gate and would pass vacuously under the Hub Admin bypass.
        denied = self.client.delete(
            f"/data/threads/{thread.id}", headers=self._auth("alice@example.com")
        )
        self.assertEqual(denied.status_code, 403)

        admin_delete = self.client.delete(
            f"/data/threads/{thread.id}", headers=self._admin_auth("admin@example.com")
        )
        self.assertEqual(admin_delete.status_code, 204)
        self.assertIsNone(self.db.query(ThreadV2).filter(ThreadV2.id == thread.id).first())

    def test_admin_delete_cascades_comments_votes_and_notifications(self) -> None:
        component = self._seed_thread_widget("cascade-delete-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        comment = ThreadCommentV2(
            thread_id=thread.id,
            content="a comment",
            author_email="bob@example.com",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(comment)
        self.db.commit()
        vote = ThreadVoteV2(
            thread_id=thread.id,
            comment_id=None,
            voter_email="carol@example.com",
            value=1,
            created_at=thread_service._utc_now(),
            updated_at=thread_service._utc_now(),
        )
        self.db.add(vote)
        # A notification pointing at this thread — nothing in Phase 1 writes
        # these yet, but the FK's cascade must already be correct for when
        # Phase 4 starts writing them (plan §3.4's "no orphan state and no
        # tombstone").
        notification = NotificationV2(
            recipient_email="dave@example.com",
            type="thread_comment",
            component_id=component.id,
            thread_id=thread.id,
            comment_id=comment.id,
            created_at=thread_service._utc_now(),
        )
        self.db.add(notification)
        self.db.commit()
        comment_id, vote_id, notification_id = comment.id, vote.id, notification.id

        resp = self.client.delete(
            f"/data/threads/{thread.id}", headers=self._admin_auth("admin@example.com")
        )
        self.assertEqual(resp.status_code, 204)

        self.assertIsNone(self.db.query(ThreadCommentV2).filter(ThreadCommentV2.id == comment_id).first())
        self.assertIsNone(self.db.query(ThreadVoteV2).filter(ThreadVoteV2.id == vote_id).first())
        self.assertIsNone(
            self.db.query(NotificationV2).filter(NotificationV2.id == notification_id).first()
        )

    def test_deleting_the_widget_component_deletes_its_threads(self) -> None:
        # plan §11.1 — the landmine itself, exercised through the REAL
        # canvas-save deletion path (update_tab_content_v2), not a bare
        # db.delete(component). This is the code path an editor actually
        # triggers by removing the widget and saving.
        component = self._seed_thread_widget("widget-removed-from-canvas")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        thread_id = thread.id

        update_tab_content_v2(
            self.db,
            self.root["documentId"],
            {"schemaVersion": 2, "layout": [], "widgets": {}},
        )
        self.db.commit()

        self.assertIsNone(self.db.query(ComponentV2).filter(ComponentV2.id == component.id).first())
        self.assertIsNone(self.db.query(ThreadV2).filter(ThreadV2.id == thread_id).first())


# ---------------------------------------------------------
# GET /threads/{thread_id} — the content-availability gap fix (session
# handoff 2026-08-20 §0/addendum). Gated like the list endpoint
# (_check_view_access), deliberately WITHOUT _require_author — this is a
# read, and closes the gap the Phase 2 frontend could only work around for a
# thread's own author via an empty PATCH.
# ---------------------------------------------------------


class ThreadDetailFetchTests(_ThreadFixture):
    def test_any_viewer_with_view_access_can_read_any_threads_content(self) -> None:
        component = self._seed_thread_widget("detail-fetch-widget")
        thread = self._insert_thread(
            component.id, author_email="alice@example.com", content="alice's own words"
        )

        # A viewer who is neither the author nor an admin.
        resp = self.client.get(
            f"/data/threads/{thread.id}", headers=self._member_auth("bob@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["content"], "alice's own words")
        self.assertEqual(body["author_email"], "alice@example.com")

        # The author themself also reads fine through the same endpoint.
        as_author = self.client.get(
            f"/data/threads/{thread.id}", headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(as_author.status_code, 200, as_author.text)
        self.assertEqual(as_author.json()["content"], "alice's own words")

    def test_missing_thread_is_404(self) -> None:
        resp = self.client.get("/data/threads/999999", headers=self._member_auth("alice@example.com"))
        self.assertEqual(resp.status_code, 404)

    def test_no_view_access_is_403(self) -> None:
        # Same swap as `test_widget_access_control_is_enforced` — a real
        # `ResourceGrantV2` row in place of the retired `access_control`
        # JSONB, which `_check_view_access` no longer reads.
        component = self._seed_thread_widget("detail-fetch-restricted-widget")
        self._grant(email="allowed@example.com", node=("component", component.id))
        thread = self._insert_thread(component.id, author_email="allowed@example.com")

        denied = self.client.get(
            f"/data/threads/{thread.id}",
            headers=self._auth("outsider@example.com", roles=["Hub Member"]),
        )
        self.assertEqual(denied.status_code, 403)

        allowed = self.client.get(
            f"/data/threads/{thread.id}", headers=self._auth("allowed@example.com")
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

        # Hub Admin bypasses even though not in the viewers list, same as
        # every other read on this router.
        admin = self.client.get(
            f"/data/threads/{thread.id}",
            headers=self._admin_auth("admin@example.com"),
        )
        self.assertEqual(admin.status_code, 200, admin.text)


# ---------------------------------------------------------
# Content / length validation (plan §4.2, finding E8)
# ---------------------------------------------------------


class ContentValidationTests(_ThreadFixture):
    def test_title_over_200_chars_is_rejected(self) -> None:
        self._seed_thread_widget("title-len-widget")
        resp = self.client.post(
            "/data/threads/component/title-len-widget",
            json={"title": "x" * 201, "content": "body"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 422)

    def test_title_exactly_200_chars_is_accepted(self) -> None:
        self._seed_thread_widget("title-len-widget-2")
        resp = self.client.post(
            "/data/threads/component/title-len-widget-2",
            json={"title": "x" * 200, "content": "body"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_content_length_is_counted_in_code_points_not_utf16_units(self) -> None:
        # A body of exactly 5,000 emoji is accepted; 5,001 is rejected
        # (plan §4.2, finding E8). Each of these emoji is outside the BMP —
        # 2 UTF-16 code units apiece — so this specifically distinguishes
        # "len() in Python (code points)" from a UTF-16-unit count, which
        # would see 10,000/10,002 and reject both.
        self._seed_thread_widget("emoji-widget")
        emoji = "\U0001F600"  # 😀 — one code point, two UTF-16 code units

        ok = self.client.post(
            "/data/threads/component/emoji-widget",
            json={"title": "Emoji", "content": emoji * 5000},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(len(ok.json()["content"]), 5000)

        too_long = self.client.post(
            "/data/threads/component/emoji-widget",
            json={"title": "Emoji", "content": emoji * 5001},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(too_long.status_code, 422)

    def test_empty_content_is_rejected(self) -> None:
        self._seed_thread_widget("empty-content-widget")
        resp = self.client.post(
            "/data/threads/component/empty-content-widget",
            json={"title": "T", "content": "   "},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------
# Comments
# ---------------------------------------------------------


class CommentTests(_ThreadFixture):
    def test_create_comment_recomputes_thread_comment_count(self) -> None:
        component = self._seed_thread_widget("comment-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        for i in range(3):
            resp = self.client.post(
                f"/data/threads/{thread.id}/comments",
                json={"content": f"comment {i}"},
                headers=self._member_auth("bob@example.com"),
            )
            self.assertEqual(resp.status_code, 201, resp.text)

        self.db.refresh(thread)
        self.assertEqual(thread.comment_count, 3)

        listing = self.client.get(
            f"/data/threads/{thread.id}/comments", headers=self._member_auth("bob@example.com")
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(len(listing.json()["items"]), 3)
        # Oldest first (D9's mirror for comments, plan §3.2).
        contents = [c["content"] for c in listing.json()["items"]]
        self.assertEqual(contents, ["comment 0", "comment 1", "comment 2"])

    def test_delete_comment_recomputes_thread_comment_count(self) -> None:
        component = self._seed_thread_widget("comment-delete-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        comment = ThreadCommentV2(
            thread_id=thread.id,
            content="doomed",
            author_email="bob@example.com",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(comment)
        thread.comment_count = 1
        self.db.commit()
        comment_id = comment.id

        resp = self.client.delete(
            f"/data/threads/comments/{comment_id}",
            headers=self._admin_auth("admin@example.com"),
        )
        self.assertEqual(resp.status_code, 204)

        self.db.refresh(thread)
        self.assertEqual(thread.comment_count, 0)

    def test_comment_non_author_patch_403_non_admin_delete_403(self) -> None:
        component = self._seed_thread_widget("comment-authz-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        comment = ThreadCommentV2(
            thread_id=thread.id,
            content="mine",
            author_email="bob@example.com",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(comment)
        self.db.commit()

        patch_resp = self.client.patch(
            f"/data/threads/comments/{comment.id}",
            json={"content": "hijacked"},
            headers=self._member_auth("mallory@example.com"),
        )
        self.assertEqual(patch_resp.status_code, 403)

        # Deliberately `_auth`, not `_member_auth` — same reasoning as
        # `test_non_admin_delete_is_403_and_admin_delete_succeeds`: delete
        # gates on the admin check alone, so this must be a genuinely
        # non-admin caller (bob is even the comment's own author) to test
        # anything.
        delete_resp = self.client.delete(
            f"/data/threads/comments/{comment.id}", headers=self._auth("bob@example.com")
        )
        self.assertEqual(delete_resp.status_code, 403)


# ---------------------------------------------------------
# Votes (plan §4.4, landmine §11.9)
# ---------------------------------------------------------


class VoteTests(_ThreadFixture):
    def test_vote_then_switch_then_clear(self) -> None:
        component = self._seed_thread_widget("vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        up = self.client.put(
            f"/data/threads/{thread.id}/vote",
            json={"value": 1},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(up.status_code, 200, up.text)
        # No `score` key anywhere in the vote response — D10 (amended
        # 2026-08-19) renders up and down as independent counts, and an exact
        # dict comparison is what keeps a net score from creeping back in.
        self.assertEqual(up.json(), {"up_count": 1, "down_count": 0, "my_vote": 1})

        switch = self.client.put(
            f"/data/threads/{thread.id}/vote",
            json={"value": -1},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(switch.status_code, 200, switch.text)
        # Switching moves the vote across BOTH counters — up_count -1 and
        # down_count +1 — never one side alone (plan §10).
        self.assertEqual(
            switch.json(), {"up_count": 0, "down_count": 1, "my_vote": -1}
        )

        clear = self.client.put(
            f"/data/threads/{thread.id}/vote",
            json={"value": 0},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(clear.status_code, 200, clear.text)
        self.assertEqual(
            clear.json(), {"up_count": 0, "down_count": 0, "my_vote": 0}
        )
        self.assertEqual(
            self.db.query(ThreadVoteV2).filter(ThreadVoteV2.thread_id == thread.id).count(), 0
        )

    def test_duplicate_vote_row_is_rejected_at_the_db_layer(self) -> None:
        # Regression guard for the partial unique index (plan §3.3) —
        # exercised directly at the ORM/DB layer since the service's own
        # upsert-by-lookup path never attempts a raw duplicate insert.
        component = self._seed_thread_widget("dup-vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        now = thread_service._utc_now()
        self.db.add(
            ThreadVoteV2(
                thread_id=thread.id, voter_email="bob@example.com", value=1,
                created_at=now, updated_at=now,
            )
        )
        self.db.commit()
        self.db.add(
            ThreadVoteV2(
                thread_id=thread.id, voter_email="bob@example.com", value=-1,
                created_at=now, updated_at=now,
            )
        )
        with self.assertRaises(Exception):
            self.db.commit()
        self.db.rollback()

    def test_same_vote_request_applied_twice_leaves_counters_identical_to_once(self) -> None:
        # Finding E2 — the regression test a plain "vote once, check score"
        # test cannot catch: retries/double-clicks must be idempotent
        # because counters are recomputed from source, never incremented.
        component = self._seed_thread_widget("idempotent-vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        headers = self._member_auth("bob@example.com")

        first = self.client.put(
            f"/data/threads/{thread.id}/vote", json={"value": 1}, headers=headers
        )
        second = self.client.put(
            f"/data/threads/{thread.id}/vote", json={"value": 1}, headers=headers
        )
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second.json()["up_count"], 1)
        self.assertEqual(
            self.db.query(ThreadVoteV2).filter(ThreadVoteV2.thread_id == thread.id).count(), 1
        )

    def test_counters_converge_after_tampering(self) -> None:
        # Finding E3 — recompute-on-write self-heals a corrupted counter the
        # moment ANY vote is cast, without needing the repair script.
        component = self._seed_thread_widget("tamper-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        thread.up_count = 999  # corrupt it directly, bypassing the service
        self.db.commit()

        resp = self.client.put(
            f"/data/threads/{thread.id}/vote",
            json={"value": 1},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["up_count"], 1)

    def test_self_vote_is_403(self) -> None:
        component = self._seed_thread_widget("self-vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        resp = self.client.put(
            f"/data/threads/{thread.id}/vote",
            json={"value": 1},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_comment_vote_also_enforces_self_vote_and_recomputes(self) -> None:
        component = self._seed_thread_widget("comment-vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        comment = ThreadCommentV2(
            thread_id=thread.id,
            content="c",
            author_email="bob@example.com",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(comment)
        self.db.commit()

        self_vote = self.client.put(
            f"/data/threads/comments/{comment.id}/vote",
            json={"value": 1},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(self_vote.status_code, 403)

        ok = self.client.put(
            f"/data/threads/comments/{comment.id}/vote",
            json={"value": 1},
            headers=self._member_auth("carol@example.com"),
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["up_count"], 1)


# ---------------------------------------------------------
# Keyset pagination (plan §4.3, finding E10)
# ---------------------------------------------------------


class PaginationTests(_ThreadFixture):
    def test_pagination_stable_when_a_row_is_inserted_mid_scroll(self) -> None:
        component = self._seed_thread_widget("mid-scroll-widget")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(25):
            self._insert_thread(
                component.id, title=f"T{i}", created_at=base + timedelta(minutes=i)
            )

        first_page = self.client.get(
            "/data/threads/component/mid-scroll-widget", headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(first_page.status_code, 200, first_page.text)
        first_items = first_page.json()["items"]
        self.assertEqual(len(first_items), 20)
        cursor = first_page.json()["next_cursor"]
        self.assertIsNotNone(cursor)

        # Someone posts a brand-new (newest) thread between page 1 and page 2.
        self._insert_thread(
            component.id, title="brand-new", created_at=base + timedelta(minutes=100)
        )

        second_page = self.client.get(
            "/data/threads/component/mid-scroll-widget",
            params={"cursor": cursor},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(second_page.status_code, 200, second_page.text)
        second_items = second_page.json()["items"]
        self.assertIsNone(second_page.json()["next_cursor"])

        first_ids = {i["id"] for i in first_items}
        second_ids = {i["id"] for i in second_items}
        # No overlap and no gap: page 2 is exactly the 5 oldest threads,
        # unaffected by the newer insert (which sorts ahead of the cursor,
        # not behind it).
        self.assertEqual(len(first_ids & second_ids), 0)
        self.assertEqual(len(second_ids), 5)
        titles_seen = {i["title"] for i in first_items} | {i["title"] for i in second_items}
        self.assertNotIn("brand-new", titles_seen)

    def test_all_rows_appear_exactly_once_with_tied_timestamps(self) -> None:
        # The literal plan §10 scenario: 30 threads sharing one created_at
        # value exactly, as every row inserted in one transaction would
        # (plan §4.3). Mainly guards ORDER BY determinism (the trailing
        # `id` tie-break) and LIMIT-boundary correctness across pages.
        component = self._seed_thread_widget("tied-widget")
        tied = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(30):
            self._insert_thread(component.id, title=f"tied-{i}", created_at=tied)

        seen_ids: set[int] = set()
        cursor = None
        headers = self._member_auth("alice@example.com")
        for _ in range(5):
            params = {"cursor": cursor} if cursor else {}
            resp = self.client.get(
                "/data/threads/component/tied-widget", params=params, headers=headers
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            for item in body["items"]:
                self.assertNotIn(item["id"], seen_ids, "a row appeared on more than one page")
                seen_ids.add(item["id"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen_ids), 30)

    def test_row_comparison_not_two_anded_predicates(self) -> None:
        # Finding E10, made concrete: a row with an EARLIER created_at but a
        # HIGHER id than the page boundary. `created_at <= $2 AND id < $3`
        # (the tempting, wrong form) drops it — `id < $3` excludes it even
        # though `created_at < $2` alone should have included it outright.
        # The correct row-value comparison `(created_at, id) < ($2, $3)`
        # keeps it. This is the test that actually distinguishes the two
        # forms; the all-tied-timestamps test above does not (both forms
        # agree when every row shares one timestamp).
        component = self._seed_thread_widget("anti-correlated-widget")
        recent = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(25):
            self._insert_thread(component.id, title=f"recent-{i}", created_at=recent)
        # Inserted LAST (so it gets the highest id) but with an EARLIER
        # created_at than every "recent" row above.
        old = self._insert_thread(
            component.id, title="actually-oldest", created_at=recent - timedelta(days=1)
        )

        seen_titles: list[str] = []
        cursor = None
        headers = self._member_auth("alice@example.com")
        for _ in range(5):
            params = {"cursor": cursor} if cursor else {}
            resp = self.client.get(
                "/data/threads/component/anti-correlated-widget", params=params, headers=headers
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            seen_titles.extend(item["title"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        self.assertEqual(len(seen_titles), 26)
        self.assertIn("actually-oldest", seen_titles)
        # Sorts last — its created_at is the oldest of all.
        self.assertEqual(seen_titles[-1], "actually-oldest")

    def test_invalid_cursor_is_400(self) -> None:
        self._seed_thread_widget("bad-cursor-widget")
        resp = self.client.get(
            "/data/threads/component/bad-cursor-widget",
            params={"cursor": "not-a-real-cursor"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------
# Repair script support (plan §4.4's "this is also the repair path", §3.6)
# ---------------------------------------------------------


class RepairScriptSupportTests(_ThreadFixture):
    def test_recompute_all_thread_counters_fixes_a_drifted_row_and_reports_it(self) -> None:
        component = self._seed_thread_widget("repair-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        now = thread_service._utc_now()
        self.db.add(
            ThreadVoteV2(
                thread_id=thread.id, voter_email="bob@example.com", value=1,
                created_at=now, updated_at=now,
            )
        )
        # Drift the stored counters directly — no vote/comment write will
        # ever touch this row again in this test, matching the "residual
        # case" the script exists for (plan §4.4).
        thread.up_count = 42
        thread.down_count = 7
        thread.comment_count = 3
        self.db.commit()

        changes = thread_service.recompute_all_thread_counters(self.db)
        self.db.commit()

        self.db.refresh(thread)
        self.assertEqual(thread.up_count, 1)
        self.assertEqual(thread.down_count, 0)
        self.assertEqual(thread.comment_count, 0)
        self.assertTrue(
            any(c.get("thread_id") == thread.id and c["field"] == "up_count" for c in changes)
        )

    def test_recompute_is_a_no_op_report_when_nothing_drifted(self) -> None:
        component = self._seed_thread_widget("repair-noop-widget")
        self._insert_thread(component.id, author_email="alice@example.com")
        first_pass = thread_service.recompute_all_thread_counters(self.db)
        self.db.commit()
        second_pass = thread_service.recompute_all_thread_counters(self.db)
        self.assertEqual(first_pass, [])
        self.assertEqual(second_pass, [])

    def test_rekey_email_rewrites_author_and_mentions(self) -> None:
        component = self._seed_thread_widget("rekey-widget")
        thread = ThreadV2(
            component_id=component.id,
            title="T",
            content="c",
            author_email="old@example.com",
            # {email, name, token} entries (plan D13) — email is identity, the
            # other two are display snapshots.
            mentions=[
                {"email": "old@example.com", "name": "Old Name", "token": "OldName"}
            ],
            status="approved",
            up_count=0,
            down_count=0,
            comment_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(thread)
        self.db.commit()

        counts = thread_service.rekey_email(
            self.db, old_email="old@example.com", new_email="new@example.com"
        )
        self.db.commit()

        self.db.refresh(thread)
        self.assertEqual(thread.author_email, "new@example.com")
        # Only `email` moves. `name` and `token` are snapshots of how the
        # mention read when it was posted — an address change is not a name
        # change, and rewriting them would retroactively alter the rendered
        # post (plan §5.6).
        self.assertEqual(
            thread.mentions,
            [{"email": "new@example.com", "name": "Old Name", "token": "OldName"}],
        )
        self.assertEqual(counts["threads"], 1)
        self.assertEqual(counts["mentions_rewritten"], 1)

    def test_rekey_email_rewrites_a_mention_on_someone_elses_row(self) -> None:
        """A mention doesn't require the mentioning row's own author to be the
        rekeyed user — the mentions scan is separate from the authorship one."""
        component = self._seed_thread_widget("rekey-mention-widget")
        thread = ThreadV2(
            component_id=component.id,
            title="T",
            content="hey @OldName",
            author_email="someone.else@example.com",
            mentions=[
                {"email": "old@example.com", "name": "Old Name", "token": "OldName"}
            ],
            status="approved",
            up_count=0,
            down_count=0,
            comment_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(thread)
        self.db.commit()

        counts = thread_service.rekey_email(
            self.db, old_email="old@example.com", new_email="new@example.com"
        )
        self.db.commit()

        self.db.refresh(thread)
        self.assertEqual(thread.author_email, "someone.else@example.com")
        self.assertEqual(
            thread.mentions,
            [{"email": "new@example.com", "name": "Old Name", "token": "OldName"}],
        )
        self.assertEqual(counts["threads"], 0)
        self.assertEqual(counts["mentions_rewritten"], 1)

    def test_rekey_email_leaves_other_users_rows_untouched(self) -> None:
        component = self._seed_thread_widget("rekey-unrelated-widget")
        target = self._insert_thread(component.id, author_email="old@example.com")
        bystander = ThreadV2(
            component_id=component.id,
            title="T2",
            content="c",
            author_email="unrelated@example.com",
            mentions=[
                {
                    "email": "unrelated@example.com",
                    "name": "Unrelated Person",
                    "token": "UnrelatedPerson",
                }
            ],
            status="approved",
            up_count=0,
            down_count=0,
            comment_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(bystander)
        self.db.commit()

        counts = thread_service.rekey_email(
            self.db, old_email="old@example.com", new_email="new@example.com"
        )
        self.db.commit()

        self.db.refresh(target)
        self.db.refresh(bystander)
        self.assertEqual(target.author_email, "new@example.com")
        self.assertEqual(bystander.author_email, "unrelated@example.com")
        self.assertEqual(
            bystander.mentions,
            [
                {
                    "email": "unrelated@example.com",
                    "name": "Unrelated Person",
                    "token": "UnrelatedPerson",
                }
            ],
        )
        self.assertEqual(counts["threads"], 1)
        self.assertEqual(counts["mentions_rewritten"], 0)


# ---------------------------------------------------------
# Mentions (plan §5, D13) — Phase 3. Directory endpoint, server-side
# validation/resolution, and the token derivation the two share.
# Notification fan-out is Phase 4 and not tested here (nothing writes a
# notification row yet) — see thread_service.py's module docstring.
#
# REPOINTED for M9 (plan_thread_moderation_2026-09-18.md §5.9 / §9.1a,
# phase 2m): a mentionable person must now be GRANTED on the widget (or an
# ancestor), so the tests below that need a mention to SURVIVE seed people
# with `_seed_mentionable(email, ("component", widget.id))` instead of a
# bare `_seed_user`, and the directory calls moved to the nested
# `/threads/component/{link}/mentionable-users`. Tests that are ABOUT the
# scoping itself live in tests/test_thread_mentions_scoping.py; these keep
# testing what they always tested (eligibility, tokens, dedupe, the cap,
# fan-out rules) with the access precondition satisfied.
# ---------------------------------------------------------


def _without(rows: list[dict], email: str) -> list[dict]:
    """The directory minus the caller. Since phase 3 `_member_auth` makes the
    caller a real Hub Admin, and an assignment admin is mentionable on every
    widget (M10, `HubAdminByAssignmentRoute` in
    test_thread_mentions_scoping.py), so the caller is listed too. Before,
    the JWT-only caller was not (the documented gap)."""
    return [row for row in rows if row["email"] != email]


class MentionDirectoryTests(_ThreadFixture):
    CALLER = "alice@example.com"

    def setUp(self) -> None:
        super().setUp()
        self.widget = self._seed_thread_widget("dir-widget")
        self.node = ("component", self.widget.id)

    def _directory(self, q: str | None = None):
        qs = f"?q={q}" if q else ""
        return self.client.get(
            f"/data/threads/component/dir-widget/mentionable-users{qs}",
            headers=self._member_auth(self.CALLER),
        )

    def test_filters_on_end_date_only_ignores_status_excludes_no_email(self) -> None:
        # D7 as amended by D13 (plan §5.1/§10): EndDate IS NULL OR EndDate >
        # current_date, `status` never consulted, no `work_email` excluded
        # structurally regardless of everything else. Everyone with an email
        # is GRANTED here, so the exclusions below are the roster rule's,
        # not M9's.
        today = date.today()
        self._seed_mentionable("noend@example.com", self.node, name="No End Date", end_date=None)
        self._seed_mentionable(
            "future@example.com", self.node, name="Future Leaver", end_date=today + timedelta(days=1)
        )
        self._seed_mentionable(
            "past@example.com", self.node, name="Past Leaver", end_date=today - timedelta(days=1)
        )
        self._seed_user("No Email Person", work_email=None, end_date=None)

        resp = self._directory()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn(self.CALLER, {row["email"] for row in resp.json()})  # the admin caller (M10)
        emails = {row["email"] for row in _without(resp.json(), self.CALLER)}
        self.assertIn("noend@example.com", emails)
        self.assertIn("future@example.com", emails)
        self.assertNotIn("past@example.com", emails)
        self.assertNotIn(None, emails)
        self.assertEqual(len(emails), 2)

    def test_search_matches_name_substring_and_token_prefix_accent_insensitive(self) -> None:
        self._seed_mentionable("jose@example.com", self.node, name="José Núñez")
        self._seed_mentionable("roy@example.com", self.node, name="Roy Abdelnour")

        by_name = self._directory(q="jose")
        self.assertEqual(by_name.status_code, 200, by_name.text)
        self.assertEqual([r["email"] for r in by_name.json()], ["jose@example.com"])

        by_token_prefix = self._directory(q="royab")
        self.assertEqual([r["email"] for r in by_token_prefix.json()], ["roy@example.com"])

    def test_capped_at_eight_results(self) -> None:
        for i in range(12):
            self._seed_mentionable(f"person{i:02d}@example.com", self.node, name=f"Person {i:02d}")
        resp = self._directory()
        self.assertEqual(len(resp.json()), 8)

    def test_token_and_name_response_shape(self) -> None:
        self._seed_mentionable("mjo@example.com", self.node, name="Mary-Jane O'Brien")
        resp = self._directory()
        row = _without(resp.json(), self.CALLER)[0]
        self.assertEqual(row["name"], "Mary-Jane O'Brien")
        # Hyphen, apostrophe and space all stripped (plan §5.5 rule 2).
        self.assertEqual(row["token"], "MaryJaneOBrien")
        self.assertEqual(row["email"], "mjo@example.com")


class MentionValidationTests(_ThreadFixture):
    def test_unclaimed_email_with_no_users_row_is_dropped(self) -> None:
        component = self._seed_thread_widget("mention-widget-1")
        resp = self.client.post(
            "/data/threads/component/mention-widget-1",
            json={
                "title": "T",
                "content": "hi @Nobody",
                "mentions": [{"email": "nobody@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["mentions"], [])
        thread = self.db.query(ThreadV2).filter(ThreadV2.component_id == component.id).first()
        self.assertEqual(thread.mentions, [])

    def test_eligible_but_token_absent_from_content_is_dropped(self) -> None:
        self._seed_thread_widget("mention-widget-2")
        self._seed_user("Bob Smith", work_email="bob@example.com")
        resp = self.client.post(
            "/data/threads/component/mention-widget-2",
            json={
                "title": "T",
                # Content never actually contains "@BobSmith".
                "content": "hi everyone, no mention here",
                "mentions": [{"email": "bob@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["mentions"], [])

    def test_valid_mention_stored_and_server_overwrites_client_name_and_token(self) -> None:
        widget = self._seed_thread_widget("mention-widget-3")
        self._seed_mentionable("bob@example.com", ("component", widget.id), name="Bob Smith")
        resp = self.client.post(
            "/data/threads/component/mention-widget-3",
            json={
                "title": "T",
                "content": "hi @BobSmith, welcome",
                "mentions": [
                    {"email": "Bob@Example.com", "name": "NOT Bob", "token": "FAKE"}
                ],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(
            resp.json()["mentions"],
            [{"email": "bob@example.com", "name": "Bob Smith", "token": "BobSmith"}],
        )

    def test_non_ascii_name_mention_round_trips(self) -> None:
        widget = self._seed_thread_widget("mention-widget-4")
        self._seed_mentionable("jose@example.com", ("component", widget.id), name="José Núñez")
        directory = self.client.get(
            "/data/threads/component/mention-widget-4/mentionable-users",
            headers=self._member_auth("alice@example.com"),
        )
        token = _without(directory.json(), "alice@example.com")[0]["token"]
        self.assertEqual(token, "JoséNúñez")

        resp = self.client.post(
            "/data/threads/component/mention-widget-4",
            json={
                "title": "T",
                "content": f"cc @{token}",
                "mentions": [{"email": "jose@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(
            resp.json()["mentions"],
            [{"email": "jose@example.com", "name": "José Núñez", "token": "JoséNúñez"}],
        )

    def test_token_from_directory_is_byte_identical_to_validation_recompute(self) -> None:
        # E5/D13's own round-trip requirement — a second, independent
        # derivation is exactly the bug this guards against.
        widget = self._seed_thread_widget("mention-widget-5")
        self._seed_mentionable("mjo@example.com", ("component", widget.id), name="Mary-Jane O'Brien")
        directory_token = _without(
            self.client.get(
                "/data/threads/component/mention-widget-5/mentionable-users",
                headers=self._member_auth("alice@example.com"),
            ).json(),
            "alice@example.com",
        )[0]["token"]

        resp = self.client.post(
            "/data/threads/component/mention-widget-5",
            json={
                "title": "T",
                "content": f"hey @{directory_token}",
                "mentions": [{"email": "mjo@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.json()["mentions"][0]["token"], directory_token)

    def test_duplicate_claimed_email_deduped_to_one_entry(self) -> None:
        widget = self._seed_thread_widget("mention-widget-6")
        self._seed_mentionable("bob@example.com", ("component", widget.id), name="Bob Smith")
        resp = self.client.post(
            "/data/threads/component/mention-widget-6",
            json={
                "title": "T",
                "content": "hi @BobSmith and also @BobSmith again",
                "mentions": [{"email": "bob@example.com"}, {"email": "BOB@EXAMPLE.COM"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(len(resp.json()["mentions"]), 1)

    def test_over_cap_mentions_is_400_and_creates_nothing(self) -> None:
        component = self._seed_thread_widget("mention-widget-7")
        claimed = []
        content_parts = []
        for i in range(26):
            email = f"person{i}@example.com"
            self._seed_mentionable(email, ("component", component.id), name=f"Person{i}")
            claimed.append({"email": email})
            content_parts.append(f"@Person{i}")
        resp = self.client.post(
            "/data/threads/component/mention-widget-7",
            json={"title": "T", "content": " ".join(content_parts), "mentions": claimed},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(
            self.db.query(ThreadV2).filter(ThreadV2.component_id == component.id).count(), 0
        )

    def test_comment_mentions_validated_the_same_way(self) -> None:
        component = self._seed_thread_widget("mention-widget-8")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_mentionable("bob@example.com", ("component", component.id), name="Bob Smith")
        resp = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={"content": "cc @BobSmith", "mentions": [{"email": "bob@example.com"}]},
            headers=self._member_auth("carol@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(
            resp.json()["mentions"],
            [{"email": "bob@example.com", "name": "Bob Smith", "token": "BobSmith"}],
        )

    def test_edit_omitting_mentions_leaves_them_untouched(self) -> None:
        component = self._seed_thread_widget("mention-widget-9")
        self._seed_mentionable("bob@example.com", ("component", component.id), name="Bob Smith")
        create = self.client.post(
            "/data/threads/component/mention-widget-9",
            json={
                "title": "T",
                "content": "hi @BobSmith",
                "mentions": [{"email": "bob@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        thread_id = create.json()["id"]

        # Title-only PATCH, no `mentions` key at all.
        resp = self.client.patch(
            f"/data/threads/{thread_id}",
            json={"title": "New title"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["thread"]["mentions"],
            [{"email": "bob@example.com", "name": "Bob Smith", "token": "BobSmith"}],
        )

    def test_edit_with_explicit_empty_mentions_clears_them(self) -> None:
        component = self._seed_thread_widget("mention-widget-10")
        self._seed_mentionable("bob@example.com", ("component", component.id), name="Bob Smith")
        create = self.client.post(
            "/data/threads/component/mention-widget-10",
            json={
                "title": "T",
                "content": "hi @BobSmith",
                "mentions": [{"email": "bob@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        thread_id = create.json()["id"]
        # Guard against this test passing vacuously: the mention must have
        # actually been STORED before the PATCH below clears it.
        self.assertEqual(len(create.json()["mentions"]), 1, create.text)

        resp = self.client.patch(
            f"/data/threads/{thread_id}",
            json={"mentions": []},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["thread"]["mentions"], [])

    def test_edit_resending_mentions_revalidates_against_new_content(self) -> None:
        component = self._seed_thread_widget("mention-widget-11")
        self._seed_mentionable("bob@example.com", ("component", component.id), name="Bob Smith")
        create = self.client.post(
            "/data/threads/component/mention-widget-11",
            json={
                "title": "T",
                "content": "hi @BobSmith",
                "mentions": [{"email": "bob@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        thread_id = create.json()["id"]
        # Same vacuity guard as the test above: stored first, then dropped
        # for the RIGHT reason (token gone from the new content).
        self.assertEqual(len(create.json()["mentions"]), 1, create.text)

        # Content edited to remove the token text; mentions resent anyway —
        # check 2 (token occurs in content) must re-run against the NEW
        # content, dropping the now-absent mention.
        resp = self.client.patch(
            f"/data/threads/{thread_id}",
            json={"content": "no mention anymore", "mentions": [{"email": "bob@example.com"}]},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["thread"]["mentions"], [])


# ---------------------------------------------------------
# Notification fan-out on write (plan §5.7) — Phase 4. Driven through the
# real create/edit endpoints (not direct DB inserts) so mention validation
# and the fan-out rules are exercised together, the same way
# MentionValidationTests already drives mentions end to end.
# ---------------------------------------------------------


class NotificationFanoutTests(_ThreadFixture):
    def _notifications_for(self, email: str) -> list[NotificationV2]:
        return (
            self.db.query(NotificationV2)
            .filter(NotificationV2.recipient_email == email)
            .order_by(NotificationV2.id)
            .all()
        )

    def test_thread_create_notifies_every_mention_except_the_author(self) -> None:
        widget = self._seed_thread_widget("notif-widget-1")
        node = ("component", widget.id)
        self._seed_mentionable("bob@example.com", node, name="Bob Smith")
        self._seed_mentionable("carol@example.com", node, name="Carol Jones")
        # Alice has a `users` row too (mentionable), and mentions herself —
        # "except the author" (§5.7) must exclude this even though the
        # mention itself validates fine (author-ness isn't part of §5.3's
        # checks, only of the notification decision).
        self._seed_mentionable("alice@example.com", node, name="Alice Adams")

        resp = self.client.post(
            "/data/threads/component/notif-widget-1",
            json={
                "title": "T",
                "content": "cc @BobSmith @CarolJones @AliceAdams",
                "mentions": [
                    {"email": "bob@example.com"},
                    {"email": "carol@example.com"},
                    {"email": "alice@example.com"},
                ],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        thread_id = resp.json()["id"]

        bob_notifs = self._notifications_for("bob@example.com")
        carol_notifs = self._notifications_for("carol@example.com")
        alice_notifs = self._notifications_for("alice@example.com")

        self.assertEqual(len(bob_notifs), 1)
        self.assertEqual(bob_notifs[0].type, "mention")
        self.assertEqual(bob_notifs[0].thread_id, thread_id)
        self.assertEqual(bob_notifs[0].comment_id, None)
        self.assertEqual(bob_notifs[0].actor_email, "alice@example.com")
        self.assertEqual(len(carol_notifs), 1)
        self.assertEqual(len(alice_notifs), 0)  # no self-ping

    def test_comment_create_notifies_thread_author_when_not_mentioned(self) -> None:
        component = self._seed_thread_widget("notif-widget-2")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        resp = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={"content": "just a comment, no mentions"},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        comment_id = resp.json()["id"]

        alice_notifs = self._notifications_for("alice@example.com")
        self.assertEqual(len(alice_notifs), 1)
        self.assertEqual(alice_notifs[0].type, "thread_comment")
        self.assertEqual(alice_notifs[0].comment_id, comment_id)
        self.assertEqual(alice_notifs[0].actor_email, "bob@example.com")

    def test_author_commenting_on_own_thread_notifies_no_one(self) -> None:
        component = self._seed_thread_widget("notif-widget-3")
        thread = self._insert_thread(component.id, author_email="alice@example.com")

        resp = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={"content": "replying to myself"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(len(self._notifications_for("alice@example.com")), 0)

    def test_mentioning_the_thread_author_in_a_comment_sends_one_mention_notification_not_two(
        self,
    ) -> None:
        # plan §5.7 — "one notification per person per event, mention wins".
        component = self._seed_thread_widget("notif-widget-4")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_mentionable("alice@example.com", ("component", component.id), name="Alice Adams")

        resp = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={
                "content": "hey @AliceAdams check this out",
                "mentions": [{"email": "alice@example.com"}],
            },
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)

        alice_notifs = self._notifications_for("alice@example.com")
        self.assertEqual(len(alice_notifs), 1)
        self.assertEqual(alice_notifs[0].type, "mention")

    def test_edit_notifies_only_newly_added_mentions(self) -> None:
        component = self._seed_thread_widget("notif-widget-5")
        self._seed_mentionable("bob@example.com", ("component", component.id), name="Bob Smith")
        self._seed_mentionable("carol@example.com", ("component", component.id), name="Carol Jones")

        create = self.client.post(
            "/data/threads/component/notif-widget-5",
            json={
                "title": "T",
                "content": "hi @BobSmith",
                "mentions": [{"email": "bob@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(create.status_code, 201, create.text)
        thread_id = create.json()["id"]
        self.assertEqual(len(self._notifications_for("bob@example.com")), 1)

        edit = self.client.patch(
            f"/data/threads/{thread_id}",
            json={
                "content": "hi @BobSmith and @CarolJones",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(edit.status_code, 200, edit.text)

        # Bob was already notified at creation — the edit resends his
        # (still valid) mention but must NOT notify him again.
        self.assertEqual(len(self._notifications_for("bob@example.com")), 1)
        # Carol is new to this edit and must be notified exactly once.
        carol_notifs = self._notifications_for("carol@example.com")
        self.assertEqual(len(carol_notifs), 1)
        self.assertEqual(carol_notifs[0].type, "mention")

    def test_comment_edit_notifies_only_newly_added_mentions(self) -> None:
        component = self._seed_thread_widget("notif-widget-6")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_mentionable("bob@example.com", ("component", component.id), name="Bob Smith")
        self._seed_mentionable("carol@example.com", ("component", component.id), name="Carol Jones")

        create = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={"content": "cc @BobSmith", "mentions": [{"email": "bob@example.com"}]},
            headers=self._member_auth("dave@example.com"),
        )
        comment_id = create.json()["id"]
        self.assertEqual(len(self._notifications_for("bob@example.com")), 1)

        edit = self.client.patch(
            f"/data/threads/comments/{comment_id}",
            json={
                "content": "cc @BobSmith and @CarolJones",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("dave@example.com"),
        )
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual(len(self._notifications_for("bob@example.com")), 1)
        self.assertEqual(len(self._notifications_for("carol@example.com")), 1)

    def test_dropped_mention_creates_no_notification(self) -> None:
        # A mention that fails §5.3's validation (no matching users row)
        # must produce no notification row at all — validation happens
        # before fan-out, not after.
        self._seed_thread_widget("notif-widget-7")
        resp = self.client.post(
            "/data/threads/component/notif-widget-7",
            json={
                "title": "T",
                "content": "hi @Nobody",
                "mentions": [{"email": "nobody@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(len(self._notifications_for("nobody@example.com")), 0)


# ---------------------------------------------------------
# The notification excerpt (plan §4.8) — direct unit tests of the pure
# stripper function, independent of the fan-out tests above (which only
# assert an excerpt gets STORED, not that it's correct in detail).
# ---------------------------------------------------------


class ExcerptGenerationTests(unittest.TestCase):
    def test_adversarial_body_produces_a_bounded_excerpt_quickly(self) -> None:
        import time

        adversarial = "*" * 5000
        start = time.perf_counter()
        excerpt = thread_service.generate_notification_excerpt(adversarial)
        elapsed = time.perf_counter() - start
        self.assertLessEqual(len(excerpt), 200)
        # "well under a millisecond" in the plan's own words — generous
        # here so this doesn't flake on a loaded CI box, but still catches
        # a genuinely catastrophic (exponential/quadratic) regex.
        self.assertLess(elapsed, 0.05)

    def test_nested_emphasis_and_unterminated_brackets_do_not_hang(self) -> None:
        import time

        adversarial = ("**" * 500) + ("[" * 500) + "unterminated"
        start = time.perf_counter()
        thread_service.generate_notification_excerpt(adversarial)
        self.assertLess(time.perf_counter() - start, 0.05)

    def test_markdown_characters_are_stripped(self) -> None:
        excerpt = thread_service.generate_notification_excerpt(
            "**bold** _em_ `code` # H1 > quote [link](url)"
        )
        for char in "*_`#>[]()":
            self.assertNotIn(char, excerpt)

    def test_list_markers_stripped_from_line_start(self) -> None:
        excerpt = thread_service.generate_notification_excerpt("- item one\n- item two")
        self.assertNotIn("- ", excerpt)

    def test_whitespace_collapsed(self) -> None:
        excerpt = thread_service.generate_notification_excerpt("hello    \n\n  world")
        self.assertEqual(excerpt, "hello world")

    def test_truncated_with_ellipsis_when_over_200(self) -> None:
        body = "word " * 100  # 500 plain characters, well under the 400-bound...
        excerpt = thread_service.generate_notification_excerpt(body)
        self.assertTrue(excerpt.endswith("…"))
        self.assertLessEqual(len(excerpt), 201)

    def test_short_body_is_not_truncated(self) -> None:
        self.assertEqual(
            thread_service.generate_notification_excerpt("hi there"), "hi there"
        )

    def test_input_is_bounded_before_stripping_runs(self) -> None:
        # plan §4.8 rule 1 — content past the ~400-code-point bound must
        # never reach the regexes at all. A marker placed well beyond the
        # bound (but inside the 5,000-char cap a real post could carry)
        # must never surface in the excerpt.
        body = ("x" * 400) + "UNIQUEMARKERPASTBOUND"
        excerpt = thread_service.generate_notification_excerpt(body)
        self.assertNotIn("UNIQUEMARKERPASTBOUND", excerpt)

    def test_does_not_corrupt_a_combining_character_sequence(self) -> None:
        # A base letter + combining acute accent (2 distinct code points)
        # straddling the 200-char truncation boundary. Python string
        # slicing is code-point granular, so this can only ever separate
        # whole code points, never corrupt one (plan §4.8 rule 3 — "on a
        # code-point boundary... trivially avoided"). `.encode` raises if
        # the result contains anything that isn't validly encodable.
        base_and_mark = "é"  # visually "é", stored as 2 code points
        body = ("x" * 199) + base_and_mark + ("y" * 50)
        excerpt = thread_service.generate_notification_excerpt(body)
        excerpt.encode("utf-8")  # raises UnicodeEncodeError on corruption

    # 2026-10-06 — a link used to strip to `labelurl` ("websitehttps://…").
    def test_link_keeps_only_its_label(self) -> None:
        excerpt = thread_service.generate_notification_excerpt(
            "Go to our [website](https://example.org/a_b) now"
        )
        self.assertEqual(excerpt, "Go to our website now")

    def test_color_directive_keeps_only_its_text(self) -> None:
        excerpt = thread_service.generate_notification_excerpt(
            "a :color[red text]{value=#ff0000} b"
        )
        self.assertEqual(excerpt, "a red text b")


# ---------------------------------------------------------
# The thread-card preview (2026-10-06) — markdown-PRESERVING sibling of the
# panel excerpt above: inline syntax kept, block syntax flattened.
# ---------------------------------------------------------


class PreviewExcerptTests(unittest.TestCase):
    def _preview(self, body: str) -> str:
        return thread_service.generate_preview_excerpt(body)

    def test_links_and_inline_markdown_are_kept(self) -> None:
        body = "See our [website](https://example.org/x) and **bold** `code`."
        self.assertEqual(self._preview(body), body)

    def test_block_syntax_is_flattened(self) -> None:
        body = "# Title\n\n> quoted\n\n- one\n- two\n\n```\ncode\n```\n---\nend"
        self.assertEqual(self._preview(body), "Title quoted one two code end")

    def test_url_does_not_count_toward_visible_length(self) -> None:
        url = "https://example.org/" + "p" * 150
        body = ("w" * 150) + f" [link]({url}) tail"
        self.assertEqual(self._preview(body), body)

    def test_link_is_never_cut_in_half(self) -> None:
        body = ("w" * 195) + " [a long label here](https://example.org) more text"
        excerpt = self._preview(body)
        self.assertTrue(excerpt.endswith("…"))
        self.assertNotIn("[", excerpt.replace("[a long label here](https://example.org)", ""))

    def test_text_is_truncated_at_200_visible_characters(self) -> None:
        excerpt = self._preview("word " * 100)
        self.assertTrue(excerpt.endswith("…"))
        self.assertLessEqual(len(excerpt), 201)

    def test_partial_link_left_by_the_input_bound_is_dropped(self) -> None:
        # A link sliced by the 1500-char input bound, after short visible
        # text (the URL padding is invisible, so the budget isn't hit).
        body = "intro [ok](https://a.org/" + "u" * 1460 + ") [cut](https://b.org/" + "v" * 100 + ")"
        excerpt = self._preview(body)
        self.assertTrue(excerpt.endswith("…"))
        self.assertNotIn("[cut", excerpt)
        self.assertIn("[ok](", excerpt)

    def test_unclosed_bold_left_by_a_cut_is_dropped(self) -> None:
        body = ("w" * 190) + " **this bold runs past the cut** end"
        excerpt = self._preview(body)
        self.assertNotIn("**", excerpt)

    def test_adversarial_input_is_fast_and_bounded(self) -> None:
        import time

        for adversarial in (
            "*" * 5000,
            ("[" * 2000) + "](" * 1000,
            "[a](" * 1500,
            ":color[" * 1000,
        ):
            start = time.perf_counter()
            excerpt = self._preview(adversarial)
            self.assertLess(time.perf_counter() - start, 0.05)
            self.assertLessEqual(len(excerpt), 1501)


# ---------------------------------------------------------
# GET/POST /notifications* (plan §4.1, §4.5, §7.2) — the read side + mark-
# read, exercised through the real router.
# ---------------------------------------------------------


class NotificationEndpointTests(_ThreadFixture):
    def test_list_is_newest_first_and_paginates(self) -> None:
        component = self._seed_thread_widget("notif-list-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        base = thread_service._utc_now()
        for i in range(3):
            self._seed_notification(
                recipient_email="dave@example.com",
                component_id=component.id,
                thread_id=thread.id,
                created_at=base + timedelta(seconds=i),
            )

        page1 = self.client.get("/data/notifications", headers=self._member_auth("dave@example.com"))
        self.assertEqual(page1.status_code, 200, page1.text)
        body = page1.json()
        self.assertEqual(len(body["items"]), 3)
        # Newest (i=2) first.
        created_ats = [item["created_at"] for item in body["items"]]
        self.assertEqual(created_ats, sorted(created_ats, reverse=True))
        self.assertEqual(body["items"][0]["thread_title"], thread.title)
        self.assertEqual(body["items"][0]["component_link"], "notif-list-widget")

    def test_unread_only_filters_read_rows(self) -> None:
        component = self._seed_thread_widget("notif-unread-only-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_notification(
            recipient_email="dave@example.com",
            component_id=component.id,
            thread_id=thread.id,
            read_at=thread_service._utc_now(),
        )
        self._seed_notification(
            recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
        )

        resp = self.client.get(
            "/data/notifications?unread_only=true", headers=self._member_auth("dave@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["items"]), 1)
        self.assertIsNone(resp.json()["items"][0]["read_at"])

    def test_list_only_returns_the_callers_own_notifications(self) -> None:
        component = self._seed_thread_widget("notif-scope-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_notification(
            recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
        )
        resp = self.client.get("/data/notifications", headers=self._member_auth("erin@example.com"))
        self.assertEqual(resp.json()["items"], [])

    def test_unread_count_and_etag_304(self) -> None:
        component = self._seed_thread_widget("notif-count-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_notification(
            recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
        )
        self._seed_notification(
            recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
        )

        first = self.client.get(
            "/data/notifications/unread-count", headers=self._member_auth("dave@example.com")
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["count"], 2)
        etag = first.headers.get("etag")
        self.assertTrue(etag)

        cached = self.client.get(
            "/data/notifications/unread-count",
            headers={**self._member_auth("dave@example.com"), "If-None-Match": etag},
        )
        self.assertEqual(cached.status_code, 304)

        # Reading a notification changes the count, which must change the
        # ETag too — a stale If-None-Match must NOT keep returning 304.
        notif_id = (
            self.db.query(NotificationV2)
            .filter(NotificationV2.recipient_email == "dave@example.com")
            .first()
            .id
        )
        self.client.post(
            f"/data/notifications/{notif_id}/read", headers=self._member_auth("dave@example.com")
        )
        after_read = self.client.get(
            "/data/notifications/unread-count",
            headers={**self._member_auth("dave@example.com"), "If-None-Match": etag},
        )
        self.assertEqual(after_read.status_code, 200, after_read.text)
        self.assertEqual(after_read.json()["count"], 1)
        self.assertNotEqual(after_read.headers.get("etag"), etag)

    def test_unread_count_is_never_stored(self) -> None:
        # followups plan §1 — `Cache-Control: no-store` on both the 200 and
        # the 304, same reasoning as the moderation summary's own test.
        component = self._seed_thread_widget("notif-count-no-store")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self._seed_notification(
            recipient_email="erin@example.com", component_id=component.id, thread_id=thread.id
        )
        headers = self._member_auth("erin@example.com")

        first = self.client.get("/data/notifications/unread-count", headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers.get("cache-control"), "no-store")

        etag = first.headers["etag"]
        cached = self.client.get(
            "/data/notifications/unread-count", headers={**headers, "If-None-Match": etag}
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.headers.get("cache-control"), "no-store")

    def test_unread_count_survives_the_rate_limiter_being_irrelevant_and_is_derived(
        self,
    ) -> None:
        # Regression shape for finding C6 (plan §4.5, §10): the count comes
        # from a live COUNT(*), so it is correct even though nothing in
        # this test process ever wrote a cache for it — there is no cache
        # to be stale, unreachable, or under-counting.
        component = self._seed_thread_widget("notif-derived-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        for _ in range(5):
            self._seed_notification(
                recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
            )
        resp = self.client.get(
            "/data/notifications/unread-count", headers=self._member_auth("dave@example.com")
        )
        self.assertEqual(resp.json()["count"], 5)

    def test_mark_read_is_idempotent(self) -> None:
        component = self._seed_thread_widget("notif-idempotent-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        notif = self._seed_notification(
            recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
        )

        first = self.client.post(
            f"/data/notifications/{notif.id}/read", headers=self._member_auth("dave@example.com")
        )
        self.assertEqual(first.status_code, 204)
        second = self.client.post(
            f"/data/notifications/{notif.id}/read", headers=self._member_auth("dave@example.com")
        )
        self.assertEqual(second.status_code, 204)

        count = self.client.get(
            "/data/notifications/unread-count", headers=self._member_auth("dave@example.com")
        ).json()["count"]
        self.assertEqual(count, 0)

    def test_mark_read_is_scoped_to_the_recipient(self) -> None:
        # The IDOR guard plan §4.5 calls out explicitly.
        component = self._seed_thread_widget("notif-idor-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        notif = self._seed_notification(
            recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
        )

        # Mallory calling with Dave's notification id must not affect it.
        resp = self.client.post(
            f"/data/notifications/{notif.id}/read", headers=self._member_auth("mallory@example.com")
        )
        self.assertEqual(resp.status_code, 204)  # looks identical either way — no leak

        dave_count = self.client.get(
            "/data/notifications/unread-count", headers=self._member_auth("dave@example.com")
        ).json()["count"]
        self.assertEqual(dave_count, 1)  # untouched by Mallory's call

    def test_mark_all_read(self) -> None:
        component = self._seed_thread_widget("notif-read-all-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        for _ in range(3):
            self._seed_notification(
                recipient_email="dave@example.com", component_id=component.id, thread_id=thread.id
            )

        resp = self.client.post(
            "/data/notifications/read-all", headers=self._member_auth("dave@example.com")
        )
        self.assertEqual(resp.status_code, 204, resp.text)

        count = self.client.get(
            "/data/notifications/unread-count", headers=self._member_auth("dave@example.com")
        ).json()["count"]
        self.assertEqual(count, 0)


# ---------------------------------------------------------
# Access control (plan_ac_enforcement_closeout_2026-09-09.md §4) — the gate
# itself, isolated from the CRUD/mention/notification mechanics every other
# class in this file tests (those are all re-pointed at `_member_auth`, a
# Hub Admin bypass, for exactly that reason — see `_member_auth`'s own
# docstring). Real `ResourceGrantV2` rows (D5, direct per-user grants), not
# roles or scopes — matching `test_sbn_access_control.py`'s own convention,
# since what is under test here is the wiring, not role/scope matching.
# ---------------------------------------------------------


class ThreadAccessControlTests(_ThreadFixture):
    """An ungranted, non-admin, authenticated caller must be refused on
    every one of the 9 `_check_view_access` call sites; a caller holding a
    direct grant on the widget's own component succeeds; Hub Admin bypasses
    regardless of any grant."""

    def setUp(self) -> None:
        super().setUp()
        self.component = self._seed_thread_widget("ac-widget")
        self._grant(email="grantee@example.com", node=("component", self.component.id))
        self.thread = self._insert_thread(
            self.component.id, author_email="grantee@example.com", content="body"
        )
        self.comment = ThreadCommentV2(
            thread_id=self.thread.id,
            content="a comment",
            author_email="grantee@example.com",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(self.comment)
        self.db.commit()
        self.db.refresh(self.comment)

    def test_ungranted_caller_403s_on_every_view_site(self) -> None:
        """The 9 sites named by plan §4.1's own audit
        (thread_service.py:771, :814, :867, :886, :954, :998, :1064, :1207,
        :1229 as of that audit) — one request per site, same ungranted,
        non-admin caller throughout."""
        headers = self._auth("outsider@example.com", roles=["Hub Member"])
        cases = [
            ("GET", f"/data/threads/component/{self.component.link}", None),
            ("POST", f"/data/threads/component/{self.component.link}", {"title": "t", "content": "c"}),
            ("GET", f"/data/threads/{self.thread.id}", None),
            ("PATCH", f"/data/threads/{self.thread.id}", {"content": "x"}),
            ("GET", f"/data/threads/{self.thread.id}/comments", None),
            ("POST", f"/data/threads/{self.thread.id}/comments", {"content": "x"}),
            ("PATCH", f"/data/threads/comments/{self.comment.id}", {"content": "x"}),
            ("PUT", f"/data/threads/{self.thread.id}/vote", {"value": 1}),
            ("PUT", f"/data/threads/comments/{self.comment.id}/vote", {"value": 1}),
        ]
        for method, path, json_body in cases:
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, json=json_body, headers=headers)
                self.assertEqual(resp.status_code, 403, resp.text)

    def test_granted_caller_succeeds_on_the_same_sites(self) -> None:
        headers = self._auth("grantee@example.com")
        listing = self.client.get(f"/data/threads/component/{self.component.link}", headers=headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        detail = self.client.get(f"/data/threads/{self.thread.id}", headers=headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        comments = self.client.get(f"/data/threads/{self.thread.id}/comments", headers=headers)
        self.assertEqual(comments.status_code, 200, comments.text)

    def test_admin_bypasses_without_any_grant(self) -> None:
        headers = self._admin_auth("admin@example.com")
        resp = self.client.get(f"/data/threads/component/{self.component.link}", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_both_deletes_require_hub_admin_not_merely_a_grant(self) -> None:
        """A caller who is `is_granted` — even a direct grant — is still not
        a Hub Admin. §4.1 gates delete on `_require_hub_admin`
        (`ViewerAccess.full_access`) alone, never on the grant that makes
        view/post/vote succeed."""
        headers = self._auth("grantee@example.com")
        resp = self.client.delete(f"/data/threads/{self.thread.id}", headers=headers)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.delete(f"/data/threads/comments/{self.comment.id}", headers=headers)
        self.assertEqual(resp.status_code, 403)


class RevealedTabDoesNotGrantThreadContentTests(_ThreadFixture):
    """A widget's own `is_granted` is checked directly — never derived from
    an ancestor's `visible`/`revealed` state (§5.2's granted-vs-visible
    split, the same principle the canvas serializer already applies via
    `content: None` on a revealed component).

    Constructed at the TAB, not at the widget itself, and that is a finding
    worth recording rather than a stylistic choice: a genuine thread-widget
    component is always a tree LEAF (never a sub-grid representation or an
    SBN sub-tab — §3.1's other two component roles), and for a leaf,
    `visible(n) = granted(n)` always (§5.1's fold has no subtree to fold
    up over). So `is_granted` and `verdict(...).view` are PROVABLY IDENTICAL
    at the exact node `_check_view_access` evaluates — there is no way to
    put a thread widget itself into a revealed-but-ungranted state, and no
    value-based test could ever distinguish `is_granted` from
    `verdict().view` at that node. What IS reachable, and is what this class
    pins instead: an ENCLOSING tab can be "revealed" (visible in the nav,
    not granted) by a granted SIBLING component, and the thread widget
    sitting next to that sibling must stay fully refused — not merely
    downgraded to a shell — because nothing here ever consults the tab's own
    verdict."""

    def setUp(self) -> None:
        super().setUp()
        self.thread_widget = self._seed_thread_widget("sibling-widget")
        # An unrelated, granted sibling under the SAME tab/gridstack. This
        # alone makes the enclosing TAB visible (revealed) via the upward
        # fold, without granting the tab or the thread widget themselves.
        self.granted_sibling = ComponentV2(
            link="granted-sibling",
            title="granted-sibling",
            type="text",
            gridstack_id=self.root_gridstack.id,
        )
        self.db.add(self.granted_sibling)
        self.db.commit()
        self.db.refresh(self.granted_sibling)
        self._grant(
            email="revealed-viewer@example.com", node=("component", self.granted_sibling.id)
        )
        self.thread = self._insert_thread(
            self.thread_widget.id, author_email="someone-else@example.com", content="secret"
        )

    def test_widget_next_to_a_revealed_sibling_is_still_refused(self) -> None:
        headers = self._auth("revealed-viewer@example.com")
        resp = self.client.get(
            f"/data/threads/component/{self.thread_widget.link}", headers=headers
        )
        self.assertEqual(resp.status_code, 403, resp.text)

        resp = self.client.get(f"/data/threads/{self.thread.id}", headers=headers)
        self.assertEqual(resp.status_code, 403, resp.text)


class NotificationAccessControlTests(_ThreadFixture):
    """plan_ac_enforcement_closeout_2026-09-09.md §4.2 — the third gap:
    `list_notifications_for_user` and `get_unread_notification_count` must
    agree with each other, and both must drop a notification whose
    component the caller does not (or no longer) hold a grant on."""

    def setUp(self) -> None:
        super().setUp()
        self.granted_component = self._seed_thread_widget("notif-granted-widget")
        self.ungranted_component = self._seed_thread_widget("notif-ungranted-widget")
        self._grant(email="dana@example.com", node=("component", self.granted_component.id))

        granted_thread = self._insert_thread(
            self.granted_component.id, author_email="actor@example.com"
        )
        ungranted_thread = self._insert_thread(
            self.ungranted_component.id, author_email="actor@example.com"
        )
        self.granted_notification = self._seed_notification(
            recipient_email="dana@example.com",
            component_id=self.granted_component.id,
            thread_id=granted_thread.id,
            excerpt="visible excerpt",
        )
        self.ungranted_notification = self._seed_notification(
            recipient_email="dana@example.com",
            component_id=self.ungranted_component.id,
            thread_id=ungranted_thread.id,
            excerpt="should not leak",
        )

    def test_list_filters_out_the_ungranted_notification(self) -> None:
        resp = self.client.get("/data/notifications", headers=self._auth("dana@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = {item["id"] for item in resp.json()["items"]}
        self.assertIn(self.granted_notification.id, ids)
        self.assertNotIn(self.ungranted_notification.id, ids)
        # The excerpt itself must not be in the response body at all — not
        # just filtered from the id set above.
        excerpts = {item["excerpt"] for item in resp.json()["items"]}
        self.assertNotIn("should not leak", excerpts)

    def test_unread_count_excludes_the_ungranted_notification(self) -> None:
        resp = self.client.get(
            "/data/notifications/unread-count", headers=self._auth("dana@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["count"], 1)

    def test_hub_admin_recipient_sees_every_notification_regardless_of_grant(self) -> None:
        resp = self.client.get(
            "/data/notifications",
            headers=self._admin_auth("dana@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = {item["id"] for item in resp.json()["items"]}
        self.assertEqual(ids, {self.granted_notification.id, self.ungranted_notification.id})

        count_resp = self.client.get(
            "/data/notifications/unread-count",
            headers=self._admin_auth("dana@example.com"),
        )
        self.assertEqual(count_resp.json()["count"], 2)

    def test_mark_read_no_ops_for_an_ungranted_notification(self) -> None:
        """plan §4.2's third function — `mark_notification_read` extended the
        same "does not distinguish already-read / not-yours / does-not-exist"
        idempotent no-op (thread_service.py) to cover "no longer granted"
        too, rather than a distinguishable error."""
        headers = self._auth("dana@example.com")
        resp = self.client.post(
            f"/data/notifications/{self.ungranted_notification.id}/read", headers=headers
        )
        self.assertEqual(resp.status_code, 204)

        still_unread = (
            self.db.query(NotificationV2)
            .filter(NotificationV2.id == self.ungranted_notification.id)
            .first()
        )
        self.assertIsNone(still_unread.read_at)


# ---------------------------------------------------------
# Per-user write rate limiting (plan §4.7 control 2) — endpoint-level
# behaviour, exercised through `self.fake_rate_limiter`
# (`_FakeRateLimitService`, set up by `_ThreadFixture`). The underlying
# fixed-window Upstash counting logic itself is tested in isolation by
# `RateLimitServiceTests` below, against a fake Redis client — not through
# the router, and not through this fake.
# ---------------------------------------------------------


class RateLimitEndpointTests(_ThreadFixture):
    def test_thread_create_429s_with_retry_after_when_the_limiter_says_wait(self) -> None:
        self._seed_thread_widget("rl-thread-widget")
        self.fake_rate_limiter.retry_after = 42

        resp = self.client.post(
            "/data/threads/component/rl-thread-widget",
            json={"title": "T", "content": "c"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertEqual(resp.headers.get("retry-after"), "42")
        self.assertIn(("thread", "alice@example.com"), self.fake_rate_limiter.calls)
        # Blocked BEFORE the DB write — nothing was created, and the
        # handler never ran, so `consume()` was never reached either.
        self.assertEqual(self.db.query(ThreadV2).count(), 0)
        self.assertEqual(self.fake_rate_limiter.consumed, [])

    def test_comment_create_checks_the_comment_bucket(self) -> None:
        component = self._seed_thread_widget("rl-comment-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self.fake_rate_limiter.retry_after = 5

        resp = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={"content": "hi"},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertIn(("comment", "bob@example.com"), self.fake_rate_limiter.calls)

    def test_thread_vote_checks_the_vote_bucket(self) -> None:
        component = self._seed_thread_widget("rl-vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self.fake_rate_limiter.retry_after = 5

        resp = self.client.put(
            f"/data/threads/{thread.id}/vote",
            json={"value": 1},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertIn(("vote", "bob@example.com"), self.fake_rate_limiter.calls)

    def test_comment_vote_also_checks_the_vote_bucket(self) -> None:
        component = self._seed_thread_widget("rl-comment-vote-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        comment = ThreadCommentV2(
            thread_id=thread.id,
            content="c",
            author_email="carol@example.com",
            mentions=[],
            up_count=0,
            down_count=0,
            created_at=thread_service._utc_now(),
        )
        self.db.add(comment)
        self.db.commit()
        self.fake_rate_limiter.retry_after = 5

        resp = self.client.put(
            f"/data/threads/comments/{comment.id}/vote",
            json={"value": 1},
            headers=self._member_auth("bob@example.com"),
        )
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertIn(("vote", "bob@example.com"), self.fake_rate_limiter.calls)

    def test_edits_and_deletes_carry_no_rate_limit(self) -> None:
        # Session decision, flagged in the handoff: the plan (§4.7) only
        # names thread/comment/vote CREATES ("10 threads/hour, 30
        # comments/hour, 120 votes/hour") — PATCH/DELETE endpoints carry no
        # `@rate_limited(...)` decorator.
        component = self._seed_thread_widget("rl-edit-widget")
        thread = self._insert_thread(component.id, author_email="alice@example.com")
        self.fake_rate_limiter.retry_after = 999  # would 429 everything if consulted

        resp = self.client.patch(
            f"/data/threads/{thread.id}",
            json={"content": "updated"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_allowed_when_the_limiter_reports_no_wait(self) -> None:
        self._seed_thread_widget("rl-allowed-widget")
        self.fake_rate_limiter.retry_after = None  # the default; explicit for clarity

        resp = self.client.post(
            "/data/threads/component/rl-allowed-widget",
            json={"title": "T", "content": "c"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(self.fake_rate_limiter.consumed, [("thread", "alice@example.com")])

    def test_a_rejected_write_does_not_consume_the_callers_quota(self) -> None:
        # The bug this fix addresses: the OLD `@rate_limited` incremented
        # unconditionally, so a request the handler went on to reject still
        # burned one of the caller's ten allowed writes. Posting to a link
        # that doesn't resolve to a thread widget 404s inside the handler
        # itself — `check()` is still consulted (it's in `calls`), but
        # `consume()` must never fire because `func` raised.
        resp = self.client.post(
            "/data/threads/component/does-not-exist",
            json={"title": "T", "content": "c"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertIn(("thread", "alice@example.com"), self.fake_rate_limiter.calls)
        self.assertEqual(self.fake_rate_limiter.consumed, [])

    def test_a_successful_write_consumes_exactly_one_slot(self) -> None:
        self._seed_thread_widget("rl-consume-widget")

        resp = self.client.post(
            "/data/threads/component/rl-consume-widget",
            json={"title": "T", "content": "c"},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(self.fake_rate_limiter.consumed, [("thread", "alice@example.com")])


# ---------------------------------------------------------
# RateLimitService in isolation (plan §4.7 control 2) — the real fixed-
# window INCR+EXPIRE counting logic, against a fake Redis client. No
# FastAPI, no router, no `_ThreadFixture` — mirrors
# `CacheServiceLockOwnershipTests`' own pattern in
# test_airtable_widget_cache.py for testing an Upstash-backed service
# without a live connection.
# ---------------------------------------------------------


class _FakeRateLimitRedisClient:
    """In-memory stand-in for `upstash_redis.asyncio.Redis`, implementing
    `get` (the read-only peek `RateLimitService.check` makes) and `eval`
    (the atomic increment `RateLimitService.consume` makes) — the two calls
    the service makes now that checking and consuming are split. Tracks a
    real per-key counter, closely enough mirroring the actual GET / Lua
    script (INCR, EXPIRE only on the first hit) pair to exercise real
    window-counting behaviour."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        count = self.counts.get(key)
        return str(count) if count is not None else None

    async def eval(self, _script, keys, args) -> int:
        key = keys[0]
        int(args[0])  # the TTL argument — unused by this fake, but shaped
        # the same as the real call for anyone reading this over the real
        # client's shoulder.
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


class _RaisingRateLimitRedisClient:
    async def get(self, *_args, **_kwargs):
        raise RuntimeError("Upstash unreachable")

    async def eval(self, *_args, **_kwargs):
        raise RuntimeError("Upstash unreachable")


class RateLimitServiceTests(unittest.IsolatedAsyncioTestCase):
    """`check()`/`consume()` are exercised as the pair the decorator
    actually calls them as — peek, then consume only on a simulated
    success — mirroring `helpers/rate_limit.py`'s real sequencing rather
    than re-testing `check()` in isolation as if it still incremented on
    its own (that was the pre-2026-08-24 behaviour; see the module and
    decorator docstrings for the bug this fixed)."""

    def _service(self):
        from app.services.rate_limit_service import RateLimitService

        return RateLimitService(
            SimpleNamespace(
                UPSTASH_REDIS_REST_URL="https://fake",
                UPSTASH_REDIS_REST_TOKEN="fake",
            )
        )

    async def test_allows_up_to_the_limit_then_blocks_with_a_positive_retry_after(
        self,
    ) -> None:
        service = self._service()
        client = _FakeRateLimitRedisClient()
        with mock.patch.object(service, "_redis", return_value=client):
            for _ in range(10):  # RATE_LIMITS["thread"] == (10, 3600)
                self.assertIsNone(await service.check("thread", "alice@example.com"))
                await service.consume("thread", "alice@example.com")
            retry_after = await service.check("thread", "alice@example.com")
        self.assertIsNotNone(retry_after)
        self.assertGreater(retry_after, 0)

    async def test_a_peek_alone_never_moves_the_counter(self) -> None:
        # The whole point of the split: `check()` with no matching
        # `consume()` (the shape of a request the handler goes on to
        # reject) must never bring the caller closer to the limit, no
        # matter how many times it's called.
        service = self._service()
        client = _FakeRateLimitRedisClient()
        with mock.patch.object(service, "_redis", return_value=client):
            for _ in range(50):  # far more than RATE_LIMITS["thread"][0] == 10
                self.assertIsNone(await service.check("thread", "alice@example.com"))
        self.assertEqual(client.counts, {})

    async def test_different_actions_have_independent_buckets(self) -> None:
        service = self._service()
        client = _FakeRateLimitRedisClient()
        with mock.patch.object(service, "_redis", return_value=client):
            for _ in range(10):
                await service.check("thread", "alice@example.com")
                await service.consume("thread", "alice@example.com")
            # The thread bucket is now exhausted; the vote bucket for the
            # same person is untouched.
            self.assertIsNone(await service.check("vote", "alice@example.com"))

    async def test_different_users_have_independent_buckets(self) -> None:
        service = self._service()
        client = _FakeRateLimitRedisClient()
        with mock.patch.object(service, "_redis", return_value=client):
            for _ in range(10):
                await service.check("thread", "alice@example.com")
                await service.consume("thread", "alice@example.com")
            self.assertIsNone(await service.check("thread", "bob@example.com"))

    async def test_unknown_action_is_always_allowed_and_never_consults_redis(self) -> None:
        service = self._service()
        client = _FakeRateLimitRedisClient()
        with mock.patch.object(service, "_redis", return_value=client):
            self.assertIsNone(await service.check("delete", "alice@example.com"))
            await service.consume("delete", "alice@example.com")
        self.assertEqual(client.counts, {})

    async def test_disabled_when_upstash_is_not_configured(self) -> None:
        from app.services.rate_limit_service import RateLimitService

        service = RateLimitService(
            SimpleNamespace(UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None)
        )
        self.assertIsNone(await service.check("thread", "alice@example.com"))
        await service.consume("thread", "alice@example.com")  # must not raise

    async def test_fails_open_when_upstash_errors(self) -> None:
        # plan §4.7 — "a lost INCR lets the write through, never blocks
        # it." This is the regression test for that rule: if `check` or
        # `consume` ever started re-raising instead of swallowing, this
        # would fail.
        service = self._service()
        client = _RaisingRateLimitRedisClient()
        with mock.patch.object(service, "_redis", return_value=client):
            self.assertIsNone(await service.check("thread", "alice@example.com"))
            await service.consume("thread", "alice@example.com")  # must not raise


if __name__ == "__main__":
    unittest.main()
