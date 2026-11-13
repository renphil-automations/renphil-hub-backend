"""Airtable sync, phase 7 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §12;
v1 plan §9, C3): the run notifications and the bell's null-safe reads.

  - recipients: every Hub Admin; the starter left out of a MANUAL start's
    notification; soft-deleted admins never; members never
  - start / finish: actor, excerpt (summary line or failure text), `link`,
    no thread or component; a scheduled run has no actor
  - none for a preview, a start that 409s, an interrupted row, a resolution
  - advisory: a notification error never fails the run
  - the bell: sync rows (no thread) listed next to thread rows, the access
    filter still applied to thread rows only, the unread count agreeing with
    the list, mark-read working on a sync row, `link` passed through

In-memory SQLite like the earlier phases, every session closed. The steps are
stubs that return chosen counts (and open items), so the summary line is
checked against known numbers; Airtable is a fake source as in phase 2.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase7-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.airtable_sync import AirtableSyncItemV2, AirtableSyncRunV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.notification import NotificationV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.db_v2.models.thread import THREAD_WIDGET_TYPE, ThreadV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import thread_service  # noqa: E402
from app.services.airtable_sync import notifications as sync_notifications  # noqa: E402
from app.services.airtable_sync.runner import SyncRunner, get_sync_runner  # noqa: E402
from app.services.gridstack_service import create_tab_v2  # noqa: E402
from app.services.nav_tab_service import create_nav_tab_v2, get_nav_tab_by_document_id  # noqa: E402

# The phase-2 fixture's Airtable (four tables, every required field), reused.
from tests.test_airtable_sync_phase2 import SETTINGS, FakeSource, airtable_tables  # noqa: E402

ADMIN_EMAIL = "ada.admin@renphil.org"
OTHER_ADMIN_EMAIL = "other.admin@renphil.org"
ALIAS_ADMIN_ALIAS = "alias.admin@renphil.org"  # an admin row with only an alias email
DELETED_ADMIN_EMAIL = "gone.admin@renphil.org"
MEMBER_EMAIL = "member@renphil.org"
LINK = "/admin/access-control/airtable-sync"
ALL_STEPS = ("clusters", "programs", "users", "program_roles")

STARTED = thread_service.NOTIFICATION_TYPE_AIRTABLE_SYNC_STARTED
FINISHED = thread_service.NOTIFICATION_TYPE_AIRTABLE_SYNC_FINISHED


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Phase7Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self._seed_org()

        self.settings = SimpleNamespace(**SETTINGS)
        self.source = FakeSource(airtable_tables())
        self.step_counts: dict[str, dict] = {}
        self.step_items: list[tuple[str, str]] = []  # (kind, entity) opened by the users stub
        self.fail_step: str | None = None
        self.executor = lambda fn: fn()
        self.step_functions = {step: self._stub(step) for step in ALL_STEPS}

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = create_app()
        app.dependency_overrides[get_db_v2] = _db
        app.dependency_overrides[get_sync_runner] = self.make_runner
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # ── the runner, with stub steps ──────────────────────────────────────

    def _stub(self, step: str):
        def run(ctx) -> dict:
            if self.fail_step == step:
                raise RuntimeError("boom")
            if step == "users":
                for n, (kind, entity) in enumerate(self.step_items):
                    ctx.db.add(
                        AirtableSyncItemV2(
                            entity=entity,
                            airtable_record_id=f"recITEM{n}",
                            kind=kind,
                            field="name" if kind == "field_conflict" else "",
                            payload={},
                            fingerprint=f"fp{n}",
                            status="open",
                            first_run_id=ctx.run_id,
                            last_run_id=ctx.run_id,
                        )
                    )
            return dict(self.step_counts.get(step, {}))

        return run

    def make_runner(self) -> SyncRunner:
        return SyncRunner(
            session_factory=self.Session,
            executor=self.executor,
            source_factory=lambda _config: self.source,
            settings_provider=lambda: self.settings,
            step_functions=self.step_functions,
            heartbeat_interval=None,
        )

    # ── seed ────────────────────────────────────────────────────────────

    def _seed_org(self) -> None:
        db = self.db
        now = _now()
        admin_role = RoleV2(key="hub_admin", name="Admin", is_public=False, is_system=False, created_at=now)
        member_role = RoleV2(key="member", name="Member", is_public=False, is_system=False, created_at=now)
        all_scopes = ScopeV2(key="all_scopes", name="All Scopes", is_universal=True, is_public=False, is_system=False, created_at=now)
        scope_a = ScopeV2(key="scope_a", name="Scope A", is_universal=False, is_public=False, is_system=False, created_at=now)
        db.add_all([admin_role, member_role, all_scopes, scope_a])
        db.flush()
        self.users: dict[str, int] = {}

        def user(key: str, **kw) -> int:
            row = UserV2(has_signed_in=True, created_at=now, updated_at=now, **kw)
            db.add(row)
            db.flush()
            self.users[key] = row.id
            return row.id

        user(ADMIN_EMAIL, work_email=ADMIN_EMAIL, name="Ada Admin")
        user(OTHER_ADMIN_EMAIL, work_email=" Other.Admin@RenPhil.org ", name="Other")
        user(ALIAS_ADMIN_ALIAS, alias_email=ALIAS_ADMIN_ALIAS, name="Alias Only")
        user(DELETED_ADMIN_EMAIL, work_email=DELETED_ADMIN_EMAIL, name="Gone", is_deleted=True)
        user(MEMBER_EMAIL, work_email=MEMBER_EMAIL, name="Member")
        for key in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, DELETED_ADMIN_EMAIL):
            db.add(RoleAssignmentV2(user_id=self.users[key], role_id=admin_role.id, scope_id=all_scopes.id, created_at=now))
        db.add(RoleAssignmentV2(user_id=self.users[MEMBER_EMAIL], role_id=member_role.id, scope_id=scope_a.id, created_at=now))
        db.commit()

    def seed_thread_rows(self) -> tuple[ComponentV2, ThreadV2]:
        """One thread widget and one thread, for thread notifications."""
        db = self.db
        db.add(HubV2(document_id="hub-doc", access_control=None))
        db.commit()
        nav = create_nav_tab_v2(db, title="Nav")
        nav_row = get_nav_tab_by_document_id(db, nav["documentId"])
        root = create_tab_v2(db, title="Root", nav_tab_id=nav_row.id)
        root_row = db.query(TabV2).filter(TabV2.document_id == root["documentId"]).first()
        gridstack = (
            db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == root_row.id, GridstackV2.parent_id.is_(None))
            .first()
        )
        component = ComponentV2(link="thread-widget", title="Board", type=THREAD_WIDGET_TYPE, gridstack_id=gridstack.id)
        db.add(component)
        db.flush()
        thread = ThreadV2(
            component_id=component.id,
            title="The thread",
            content="body",
            author_email="author@renphil.org",
            author_name="author",
            mentions=[],
            status="approved",
            up_count=0,
            down_count=0,
            comment_count=0,
            created_at=_now(),
        )
        db.add(thread)
        db.commit()
        return component, thread

    def add_notification(self, email: str, *, component=None, thread=None, minutes_ago: int = 0, **kw) -> int:
        row = NotificationV2(
            recipient_email=email,
            type=kw.pop("type", "mention" if thread is not None else FINISHED),
            actor_email=kw.pop("actor_email", "actor@renphil.org"),
            actor_name=kw.pop("actor_name", "Actor"),
            component_id=component.id if component is not None else None,
            thread_id=thread.id if thread is not None else None,
            excerpt=kw.pop("excerpt", "an excerpt"),
            link=kw.pop("link", None if thread is not None else LINK),
            created_at=_now() - timedelta(minutes=minutes_ago),
            **kw,
        )
        self.db.add(row)
        self.db.commit()
        return row.id

    # ── helpers ─────────────────────────────────────────────────────────

    def auth(self, email: str) -> dict:
        token = jwt.encode(
            {"sub": email, "name": email.split("@")[0], "roles": [], "exp": _now() + timedelta(minutes=30)},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    def start(self, steps=ALL_STEPS, preview=False, email=ADMIN_EMAIL):
        return self.client.post(
            "/v2/airtable-sync/runs", json={"steps": list(steps), "preview": preview}, headers=self.auth(email)
        )

    def notes(self, type_: str | None = None) -> list[NotificationV2]:
        self.db.expire_all()
        query = self.db.query(NotificationV2)
        if type_ is not None:
            query = query.filter(NotificationV2.type == type_)
        return query.order_by(NotificationV2.id).all()

    def recipients(self, type_: str) -> list[str]:
        return sorted(n.recipient_email for n in self.notes(type_))

    def run_row(self, run_id: int) -> AirtableSyncRunV2:
        self.db.expire_all()
        return self.db.get(AirtableSyncRunV2, run_id)

    def add_run(self, *, status="running", trigger="manual", email=OTHER_ADMIN_EMAIL, started=None) -> AirtableSyncRunV2:
        started = started or _now()
        run = AirtableSyncRunV2(
            steps=["clusters"],
            trigger=trigger,
            status=status,
            started_by_user_id=self.users.get(email) if email else None,
            started_by_email=email,
            started_at=started,
            heartbeat_at=started,
            finished_at=None if status == "running" else started,
        )
        self.db.add(run)
        self.db.commit()
        return run


# ---------------------------------------------------------
# Recipients and content
# ---------------------------------------------------------


class TestRunNotifications(Phase7Fixture):
    def test_manual_run_start_and_finish(self) -> None:
        self.step_counts = {
            "clusters": {"records": 3, "created": 2, "renamed": 1},
            "programs": {"records": 4, "created": 3, "edges_added": 4, "linked": 1},
            "users": {"records": 5, "created": 1, "fields_updated": 5, "linked": 9, "defaults_assigned": 1},
            "program_roles": {"records": 6, "created": 6, "replaced": 1, "adopted": 2, "removed": 1, "unchanged": 7},
        }
        self.step_items = [
            ("field_conflict", "user"),
            ("field_conflict", "user"),
            ("assignment_conflict", "program_role"),
            ("scope_name_conflict", "program"),
            ("no_work_email", "user"),
            ("missing_program", "program_role"),
            ("unknown_cluster", "program"),
            ("unmapped_designation", "program_role"),
        ]
        resp = self.start()
        self.assertEqual(resp.status_code, 202, resp.text)
        run_id = resp.json()["data"]["run_id"]
        self.assertEqual(self.run_row(run_id).status, "succeeded")

        # Start: every admin but the starter; never the deleted admin or a member.
        self.assertEqual(self.recipients(STARTED), [ALIAS_ADMIN_ALIAS, OTHER_ADMIN_EMAIL])
        for n in self.notes(STARTED):
            self.assertEqual(n.excerpt, "Manual Airtable sync started by Ada Admin")
            self.assertEqual((n.actor_email, n.actor_name), (ADMIN_EMAIL, "Ada Admin"))
            self.assertEqual(n.link, LINK)
            self.assertIsNone(n.thread_id)
            self.assertIsNone(n.component_id)
            self.assertIsNone(n.read_at)

        # Finish: every admin, the starter included.
        self.assertEqual(self.recipients(FINISHED), [ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, OTHER_ADMIN_EMAIL])
        # updated = renamed 1 + edges 4 + fields 5 + defaults 1 + replaced 1 + adopted 2 + removed 1 = 15
        # created = 2 + 3 + 1 + 6 = 12; conflicts = 4; warnings = 4.
        for n in self.notes(FINISHED):
            self.assertEqual(n.excerpt, "Airtable sync finished: 15 updated, 12 created, 4 conflicts, 4 warnings")
            self.assertEqual((n.actor_email, n.actor_name), (ADMIN_EMAIL, "Ada Admin"))
            self.assertEqual(n.link, LINK)
            self.assertIsNone(n.thread_id)
            self.assertIsNone(n.component_id)
        # Starts come before finishes.
        self.assertLess(max(n.id for n in self.notes(STARTED)), min(n.id for n in self.notes(FINISHED)))

    def test_only_this_runs_open_items_are_counted(self) -> None:
        older = self.add_run(status="succeeded")
        for n, status in enumerate(("open", "resolved")):
            self.db.add(
                AirtableSyncItemV2(
                    entity="user", airtable_record_id=f"recOLD{n}", kind="no_work_email", field="", payload={},
                    fingerprint="x", status=status, first_run_id=older.id, last_run_id=older.id,
                )
            )
        self.db.commit()
        self.step_items = [("no_work_email", "user")]
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(
            {n.excerpt for n in self.notes(FINISHED)},
            {"Airtable sync finished: 0 updated, 0 created, 0 conflicts, 1 warnings"},
        )

    def test_scheduled_run_has_no_actor_and_no_exclusion(self) -> None:
        runner = self.make_runner()
        db = self.Session()
        try:
            started = runner.start(db, steps=ALL_STEPS, preview=False, trigger="scheduled")
            db.commit()
        finally:
            db.close()
        runner.launch(started)
        self.assertEqual(self.run_row(started.run_id).status, "succeeded")
        everyone = [ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, OTHER_ADMIN_EMAIL]
        self.assertEqual(self.recipients(STARTED), everyone)
        self.assertEqual(self.recipients(FINISHED), everyone)
        for n in self.notes():
            self.assertIsNone(n.actor_email)
            self.assertIsNone(n.actor_name)
        self.assertEqual({n.excerpt for n in self.notes(STARTED)}, {"Scheduled Airtable sync started"})

    def test_scheduled_row_never_names_or_excludes_a_starter(self) -> None:
        # Even if a scheduled row carried a starter, it has no actor (v1 §9.1).
        run = self.add_run(status="running", trigger="scheduled", email=ADMIN_EMAIL)
        self.assertEqual(sync_notifications.run_started(self.Session, run.id), 3)
        for n in self.notes(STARTED):
            self.assertEqual((n.actor_email, n.actor_name), (None, None))
            self.assertEqual(n.excerpt, "Scheduled Airtable sync started")

    def test_manual_starter_without_a_name_is_named_by_email(self) -> None:
        user = self.db.get(UserV2, self.users[ADMIN_EMAIL])
        user.name = "  "
        self.db.commit()
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual({n.excerpt for n in self.notes(STARTED)}, {f"Manual Airtable sync started by {ADMIN_EMAIL}"})
        self.assertEqual({n.actor_name for n in self.notes(STARTED)}, {None})

    def test_soft_deleted_and_demoted_admins_get_nothing(self) -> None:
        # Demote the other admin; the deleted one stays deleted.
        self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == self.users[OTHER_ADMIN_EMAIL]).delete()
        self.db.commit()
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.recipients(STARTED), [ALIAS_ADMIN_ALIAS])
        self.assertEqual(self.recipients(FINISHED), [ADMIN_EMAIL, ALIAS_ADMIN_ALIAS])
        self.assertNotIn(DELETED_ADMIN_EMAIL, {n.recipient_email for n in self.notes()})
        self.assertNotIn(MEMBER_EMAIL, {n.recipient_email for n in self.notes()})

    def test_lone_admin_manual_run_gets_only_the_finish(self) -> None:
        for key in (OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS):
            self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == self.users[key]).delete()
        self.db.commit()
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.notes(STARTED), [])
        self.assertEqual(self.recipients(FINISHED), [ADMIN_EMAIL])


class TestFailures(Phase7Fixture):
    def test_missing_field_failure_text(self) -> None:
        self.source.tables["tblUSERS"]["fields"].discard("Work Email")
        run_id = self.start().json()["data"]["run_id"]
        run = self.run_row(run_id)
        self.assertEqual(run.status, "failed")
        excerpts = {n.excerpt for n in self.notes(FINISHED)}
        self.assertEqual(excerpts, {f"Airtable sync failed: {run.error}"[:200]})
        self.assertIn("Work Email", excerpts.pop())
        self.assertEqual(self.recipients(FINISHED), [ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, OTHER_ADMIN_EMAIL])
        self.assertEqual(self.recipients(STARTED), [ALIAS_ADMIN_ALIAS, OTHER_ADMIN_EMAIL])
        for n in self.notes():
            self.assertNotIn(SETTINGS["AIRTABLE_API_KEY"], n.excerpt)

    def test_step_crash_failure_text(self) -> None:
        self.fail_step = "users"
        run_id = self.start().json()["data"]["run_id"]
        self.assertEqual(self.run_row(run_id).status, "failed")
        self.assertEqual(
            {n.excerpt for n in self.notes(FINISHED)},
            {"Airtable sync failed: Unexpected error (RuntimeError); details are in the server log."},
        )

    def test_long_error_is_clipped_to_the_column(self) -> None:
        run = self.add_run(status="failed")
        run.error = "x" * 500
        self.db.commit()
        sync_notifications.run_finished(self.Session, run.id)
        for n in self.notes(FINISHED):
            self.assertEqual(len(n.excerpt), 200)
            self.assertTrue(n.excerpt.startswith("Airtable sync failed: xxx"))
            self.assertTrue(n.excerpt.endswith("…"))

    def test_launch_failure_still_notifies_the_end(self) -> None:
        def broken(_fn):
            raise RuntimeError("no threads")

        self.executor = broken
        run_id = self.start().json()["data"]["run_id"]
        self.assertEqual(self.run_row(run_id).status, "failed")
        self.assertEqual(len(self.notes(STARTED)), 2)
        self.assertEqual({n.excerpt for n in self.notes(FINISHED)}, {"Airtable sync failed: The run could not be started."})


class TestNoNotifications(Phase7Fixture):
    def test_preview_sends_none(self) -> None:
        resp = self.start(preview=True)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.run_row(resp.json()["data"]["run_id"]).status, "succeeded")
        self.assertEqual(self.notes(), [])

    def test_failed_preview_sends_none(self) -> None:
        self.fail_step = "clusters"
        resp = self.start(preview=True)
        self.assertEqual(self.run_row(resp.json()["data"]["run_id"]).status, "failed")
        self.assertEqual(self.notes(), [])

    def test_a_409_sends_none(self) -> None:
        self.add_run(status="running")
        resp = self.start()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.notes(), [])

    def test_an_interrupted_row_gets_no_end_notification(self) -> None:
        pending = []
        self.executor = pending.append  # the run doesn't execute yet
        run_id = self.start().json()["data"]["run_id"]
        self.assertEqual(len(self.notes(STARTED)), 2)
        run = self.run_row(run_id)
        run.status = "interrupted"  # as a later start would mark it
        self.db.commit()
        pending[0]()  # the old thread finally finishes
        self.assertEqual(self.run_row(run_id).status, "interrupted")
        self.assertEqual(self.notes(FINISHED), [])

    def test_resolutions_send_none(self) -> None:
        self.step_items = [("unmapped_designation", "program_role"), ("unmapped_designation", "program_role")]
        run_id = self.start().json()["data"]["run_id"]
        before = len(self.notes())
        items = self.db.query(AirtableSyncItemV2).order_by(AirtableSyncItemV2.id).all()
        resp = self.client.post(
            f"/v2/airtable-sync/items/{items[0].id}/resolve", json={"choice": "skip"}, headers=self.auth(ADMIN_EMAIL)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.post(
            "/v2/airtable-sync/items/bulk-resolve", json={"run_id": run_id, "choice": "skip"}, headers=self.auth(ADMIN_EMAIL)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.db.expire_all()
        self.assertEqual(self.db.query(AirtableSyncItemV2).filter_by(status="open").count(), 0)
        self.assertEqual(len(self.notes()), before)

    def test_notification_errors_never_fail_the_run(self) -> None:
        with mock.patch.object(sync_notifications, "admin_recipients", side_effect=RuntimeError("db gone")):
            with self.assertLogs("app.services.airtable_sync.notifications", level="ERROR") as logs:
                resp = self.start()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.run_row(resp.json()["data"]["run_id"]).status, "succeeded")
        self.assertEqual(self.notes(), [])
        self.assertEqual(len(logs.records), 2)  # the start and the end

    def test_notifier_returns_none_on_error_and_counts_otherwise(self) -> None:
        run = self.add_run(status="succeeded", email=ADMIN_EMAIL)
        self.assertEqual(sync_notifications.run_finished(self.Session, run.id), 3)
        self.assertEqual(sync_notifications.run_started(self.Session, run.id), 2)
        self.assertEqual(sync_notifications.run_finished(self.Session, 9999), 0)
        preview = self.add_run(status="succeeded", trigger="preview", email=ADMIN_EMAIL)
        self.assertEqual(sync_notifications.run_finished(self.Session, preview.id), 0)
        with mock.patch.object(sync_notifications, "admin_recipients", side_effect=RuntimeError("x")):
            with self.assertLogs("app.services.airtable_sync.notifications", level="ERROR"):
                self.assertIsNone(sync_notifications.run_finished(self.Session, run.id))


# ---------------------------------------------------------
# The bell: null-safe reads
# ---------------------------------------------------------


class TestBellReads(Phase7Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.component, self.thread = self.seed_thread_rows()

    def bell(self, email: str, **params) -> dict:
        resp = self.client.get("/data/notifications", params=params, headers=self.auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def unread(self, email: str) -> int:
        resp = self.client.get("/data/notifications/unread-count", headers=self.auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["count"]

    def test_admin_sees_sync_and_thread_rows_mixed(self) -> None:
        t1 = self.add_notification(ADMIN_EMAIL, component=self.component, thread=self.thread, minutes_ago=3)
        s1 = self.add_notification(ADMIN_EMAIL, type=STARTED, excerpt="Manual Airtable sync started by X", minutes_ago=2)
        t2 = self.add_notification(ADMIN_EMAIL, component=self.component, thread=self.thread, minutes_ago=1)
        s2 = self.add_notification(ADMIN_EMAIL, excerpt="Airtable sync finished: 1 updated, 0 created, 0 conflicts, 0 warnings")
        items = self.bell(ADMIN_EMAIL)["items"]
        self.assertEqual([i["id"] for i in items], [s2, t2, s1, t1])
        by_id = {i["id"]: i for i in items}
        for sid in (s1, s2):
            self.assertIsNone(by_id[sid]["thread_id"])
            self.assertIsNone(by_id[sid]["thread_title"])
            self.assertIsNone(by_id[sid]["component_link"])
            self.assertEqual(by_id[sid]["link"], LINK)
        self.assertEqual(by_id[s1]["type"], STARTED)
        self.assertEqual(by_id[s2]["type"], FINISHED)
        for tid in (t1, t2):
            # Thread notifications keep their payload; `link` is the only new key.
            self.assertEqual(by_id[tid]["thread_id"], self.thread.id)
            self.assertEqual(by_id[tid]["thread_title"], "The thread")
            self.assertEqual(by_id[tid]["component_link"], "thread-widget")
            self.assertIsNone(by_id[tid]["link"])
        self.assertEqual(self.unread(ADMIN_EMAIL), 4)

    def test_access_filter_applies_to_thread_rows_only(self) -> None:
        # A member who can't see the widget: the thread row is filtered out
        # of the list AND the count; the sync row stays in both.
        self.add_notification(MEMBER_EMAIL, component=self.component, thread=self.thread, minutes_ago=1)
        sync_id = self.add_notification(MEMBER_EMAIL)
        items = self.bell(MEMBER_EMAIL)["items"]
        self.assertEqual([i["id"] for i in items], [sync_id])
        self.assertEqual(self.unread(MEMBER_EMAIL), 1)
        self.assertEqual(len(self.bell(MEMBER_EMAIL, unread_only="true")["items"]), 1)

    def test_unread_count_agrees_with_the_list_after_reads(self) -> None:
        sync_id = self.add_notification(MEMBER_EMAIL)
        hidden = self.add_notification(MEMBER_EMAIL, component=self.component, thread=self.thread)
        resp = self.client.post(f"/data/notifications/{sync_id}/read", headers=self.auth(MEMBER_EMAIL))
        self.assertEqual(resp.status_code, 204)
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(NotificationV2, sync_id).read_at)
        self.assertEqual(self.unread(MEMBER_EMAIL), 0)
        self.assertEqual(self.bell(MEMBER_EMAIL, unread_only="true")["items"], [])
        # The hidden thread row still can't be marked by the member.
        self.client.post(f"/data/notifications/{hidden}/read", headers=self.auth(MEMBER_EMAIL))
        self.db.expire_all()
        self.assertIsNone(self.db.get(NotificationV2, hidden).read_at)

    def test_service_reads_with_a_fake_access(self) -> None:
        granted = SimpleNamespace(is_granted=lambda ref: ref == ("component", self.component.id))
        denied = SimpleNamespace(is_granted=lambda ref: False)
        user = SimpleNamespace(email=MEMBER_EMAIL)
        thread_id = self.add_notification(MEMBER_EMAIL, component=self.component, thread=self.thread, minutes_ago=1)
        sync_id = self.add_notification(MEMBER_EMAIL)
        db = self.Session()
        try:
            listed = thread_service.list_notifications_for_user(db, user, None, False, access=granted)
            self.assertEqual([i.id for i in listed.items], [sync_id, thread_id])
            self.assertEqual(listed.items[0].link, LINK)
            self.assertEqual(thread_service.get_unread_notification_count(db, user, access=granted)[0], 2)
            listed = thread_service.list_notifications_for_user(db, user, None, False, access=denied)
            self.assertEqual([i.id for i in listed.items], [sync_id])
            count, etag = thread_service.get_unread_notification_count(db, user, access=denied)
            self.assertEqual((count, etag), (1, f'"1-{sync_id}"'))
            # No access object (the unfiltered path) counts both.
            self.assertEqual(thread_service.get_unread_notification_count(db, user)[0], 2)
        finally:
            db.close()

    def test_pagination_crosses_sync_and_thread_rows(self) -> None:
        ids = []
        for n in range(25):
            if n % 2:
                ids.append(self.add_notification(ADMIN_EMAIL, minutes_ago=100 - n))
            else:
                ids.append(self.add_notification(ADMIN_EMAIL, component=self.component, thread=self.thread, minutes_ago=100 - n))
        first = self.bell(ADMIN_EMAIL)
        self.assertEqual(len(first["items"]), 20)
        second = self.bell(ADMIN_EMAIL, cursor=first["next_cursor"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([i["id"] for i in first["items"] + second["items"]], list(reversed(ids)))

    def test_mark_all_read_covers_sync_rows(self) -> None:
        self.add_notification(ADMIN_EMAIL)
        self.add_notification(ADMIN_EMAIL, component=self.component, thread=self.thread)
        self.client.post("/data/notifications/read-all", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(self.unread(ADMIN_EMAIL), 0)

    def test_a_real_run_lands_in_the_bell(self) -> None:
        # Started by the admin who signs in with their alias.
        self.assertEqual(self.start(email=ALIAS_ADMIN_ALIAS).status_code, 202)
        items = self.bell(ADMIN_EMAIL)["items"]
        self.assertEqual([i["type"] for i in items], [FINISHED, STARTED])
        self.assertEqual(items[1]["excerpt"], "Manual Airtable sync started by Alias Only")
        self.assertEqual(items[1]["actor_email"], ALIAS_ADMIN_ALIAS)
        self.assertEqual({i["link"] for i in items}, {LINK})
        self.assertEqual(self.unread(ADMIN_EMAIL), 2)
        # The starter gets only the finish.
        self.assertEqual([i["type"] for i in self.bell(ALIAS_ADMIN_ALIAS)["items"]], [FINISHED])


class TestSchema(unittest.TestCase):
    def test_model_columns(self) -> None:
        cols = NotificationV2.__table__.c
        self.assertTrue(cols.component_id.nullable)
        self.assertTrue(cols.thread_id.nullable)
        self.assertTrue(cols.link.nullable)
        self.assertEqual(cols.link.type.length, 512)
        self.assertGreaterEqual(cols.type.type.length, len(FINISHED))
        # Both keys still cascade from their parents.
        self.assertEqual({fk.ondelete for fk in cols.component_id.foreign_keys}, {"CASCADE"})
        self.assertEqual({fk.ondelete for fk in cols.thread_id.foreign_keys}, {"CASCADE"})

    def test_entry_schema_optional_thread_fields(self) -> None:
        from app.schemas.thread import NotificationEntry

        entry = NotificationEntry(
            id=1, type=FINISHED, actor_email=None, actor_name=None, thread_id=None, thread_title=None,
            component_link=None, excerpt="x", link=LINK, read_at=None, created_at=_now(),
        )
        self.assertEqual(entry.link, LINK)
        self.assertIsNone(entry.thread_id)


if __name__ == "__main__":
    unittest.main()
