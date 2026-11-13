"""Airtable sync, phase 6 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §7,
§12; v1 plan §5.3.4, §5.3.6, §5.3.7, §5.4): the HR fields' three-way merge,
Work Email changes of signed-in people (E1), headshots on R2 (A-5), bulk
resolve, and the lock sweep after sync removals.

SQLite in memory with every session closed; FakeSource and the fixture
copied from phase 5, not imported. Airtable is never called; R2 is a fake
store (r2.STORE_FACTORY / r2.FETCH patched). Runs go through the real runner
with an inline executor; resolutions through the real router.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase6-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.airtable_sync import (  # noqa: E402
    AirtableDesignationMappingV2,
    AirtableSyncItemV2,
    AirtableSyncLogV2,
    AirtableSyncRecordV2,
    AirtableSyncRunV2,
)
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.airtable_sync import hr_fields, lock_sweep, r2, resolutions, runner as runner_module  # noqa: E402
from app.services.airtable_sync import airtable_reader  # noqa: E402
from app.services.airtable_sync.runner import SyncRunner, get_sync_runner  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"
MEMBER_EMAIL = "member@renphil.org"
PAT = "patFAKE.0123456789abcdefSECRET"
R2_SECRET = "r2SECRETKEYvalue0123456789"
R2_KEY_ID = "r2KEYIDvalue0123"
BASE = "appHUB"
PUBLIC = "https://pub-test.r2.dev"
T_CLUSTERS, T_PROGRAMS, T_USERS, T_ROLES = "tblCLUSTER", "tblPUBLIC", "tblUSERS", "tblROLES"
ALL_STEPS = ("clusters", "programs", "users", "program_roles")

SETTINGS = {
    "AIRTABLE_API_KEY": PAT,
    "RENPHIL_HUB_BASE_ID": BASE,
    "AIRTABLE_SYNC_CLUSTERS_TABLE": T_CLUSTERS,
    "AIRTABLE_SYNC_CLUSTERS_NAME_FIELD": "Name",
    "AIRTABLE_SYNC_CLUSTERS_RECORD_ID_FIELD": "Record Id",
    "AIRTABLE_SYNC_PROGRAMS_TABLE": T_PROGRAMS,
    "AIRTABLE_SYNC_PROGRAMS_NAME_FIELD": "Name",
    "AIRTABLE_SYNC_PROGRAMS_RECORD_ID_FIELD": "Record ID",
    "AIRTABLE_SYNC_PROGRAMS_CLUSTER_RECORD_ID_FIELD": "Cluster Record Id",
    "AIRTABLE_SYNC_PROGRAMS_CLUSTER_LINK_FIELD": "Program Cluster",
    "USERS_TABLE": T_USERS,
    "AIRTABLE_SYNC_USERS_RECORD_ID_FIELD": "Record ID",
    "USERS_NAME_FIELD": "Name",
    "USERS_WORK_EMAIL_FIELD": "Work Email",
    "USERS_ALIAS_EMAIL_FIELD": "Alias Email",
    # HR fields (E-1): a mix of every kind.
    "USERS_FIRST_NAME_FIELD": "First Name",
    "USERS_EMPLOYMENT_TYPE_FIELD": "Employment Type",
    "USERS_PERSONAL_EMAIL_FIELD": "Personal Email",
    "AIRTABLE_SYNC_USERS_BIRTHDAY_OPT_OUT_FIELD": "Birthday Shout-Out Opt-Out",
    "USERS_HEADSHOT_FIELD": "Headshot",
    "AIRTABLE_SYNC_USERS_ADD_TO_WEBSITE_DATE_FIELD": "Add to Website Date",
    # The sync's own name wins over the drifted shared one (owner, 2026-10-23).
    "USERS_START_DATE_FIELD": "Start Date",
    "AIRTABLE_SYNC_USERS_START_DATE_FIELD": "StartDate",
    # Only the shared one set: it is the fallback.
    "USERS_TECH_STACK_SELECTIONS_FIELD": "Manager Tech Stack Selections",
    "AIRTABLE_SYNC_PROGRAM_ROLES_TABLE": T_ROLES,
    "AIRTABLE_SYNC_PROGRAM_ROLES_EMPLOYEE_RECORD_ID_FIELD": "Employee Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_PROGRAM_RECORD_ID_FIELD": "Original Program Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD": "Designation",
    "R2_ACCESS_KEY_ID": R2_KEY_ID,
    "R2_SECRET_ACCESS_KEY": R2_SECRET,
    "R2_BUCKET": "bucket",
    "R2_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com",
    "R2_KEY_PREFIX": "headshots/",
    "R2_PUBLIC_BASE_URL": PUBLIC + "/",
}
USER_FIELDS = {
    "Name", "Record ID", "Work Email", "Alias Email", "First Name", "Employment Type", "Personal Email",
    "Birthday Shout-Out Opt-Out", "Headshot", "Add to Website Date", "StartDate", "Manager Tech Stack Selections",
}

SPACE, P1 = "recCLSPACE", "recP1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def U(n: int) -> str:
    return f"recU{n}"


def headshot_url(att: str, filename: str) -> str:
    return f"{PUBLIC}/headshots/{att}/{filename}"


def attachment(att: str, filename: str) -> list[dict]:
    return [{"id": att, "filename": filename, "url": f"https://dl.airtable.test/{att}"}]


def person(n: int, name: str, work: str | None, **extra) -> dict:
    fields = {
        "Record ID": U(n),
        "Name": name,
        "First Name": name,
        "Employment Type": ["Full-time US"],
        "StartDate": "2024-01-15",
        "Manager Tech Stack Selections": ["Slack", "Notion"],
        "Add to Website Date": "2024-02-01T09:30:00.000Z",
        "Birthday Shout-Out Opt-Out": True,
    }
    if work is not None:
        fields["Work Email"] = work
    fields.update(extra)
    return {"id": f"recAPIu{n}", "fields": {k: v for k, v in fields.items() if v is not None}}


def role_row(n: int, employee: str, program: str = P1, designation: str = "Member") -> dict:
    return {
        "id": f"recAPIr{n}",
        "fields": {"Employee Record ID": employee, "Original Program Record ID": program, "Designation": designation},
    }


def airtable_tables() -> dict:
    users = [
        person(1, "Ada", "ada@renphil.org"),  # the Hub row agrees on everything
        person(2, "Bo", "bo@renphil.org"),  # the Hub row differs on two HR fields
        person(3, "Cy", "cy@renphil.org", **{"Employment Type": ["Fellow"], "Headshot": attachment("attCY1", "cy pic.jpg")}),  # new
    ]
    return {
        T_CLUSTERS: {"fields": {"Name", "Record Id"}, "records": [{"id": "recAPIc1", "fields": {"Name": "Space", "Record Id": SPACE}}]},
        T_PROGRAMS: {
            "fields": {"Name", "Record ID", "Cluster Record Id", "Program Cluster"},
            "records": [{"id": "recAPIp1", "fields": {"Name": "Space Science", "Record ID": P1, "Cluster Record Id": SPACE, "Program Cluster": ["recAPIc1"]}}],
        },
        T_USERS: {"fields": set(USER_FIELDS), "records": users},
        T_ROLES: {
            "fields": {"Employee Record ID", "Original Program Record ID", "Designation"},
            "records": [role_row(1, U(1)), role_row(2, U(2)), role_row(3, U(3))],
        },
    }


class FakeSource:
    def __init__(self, tables: dict):
        self.tables = tables

    def field_names(self, table_id: str) -> set[str]:
        return set(self.tables[table_id]["fields"])

    def records(self, table_id: str, field_names: list[str]) -> list[dict]:
        return [
            {"id": r["id"], "fields": {k: v for k, v in r["fields"].items() if k in field_names}}
            for r in self.tables[table_id]["records"]
        ]


class FakeStore:
    """R2: records every call; `fail` makes the named operation raise. On a
    delete it checks, from a FRESH session, that no committed `users` row
    still points at the object (A-5: delete only after the Hub switched)."""

    def __init__(self, session_factory) -> None:
        self.calls: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = {}
        self.fail: set[str] = set()
        self.session_factory = session_factory
        self.seen_at_delete: list[list[str]] = []

    def put(self, key: str, body: bytes, content_type: str | None) -> None:
        if "put" in self.fail:
            raise r2.HeadshotError("R2 upload failed (ClientError: AccessDenied)")
        self.calls.append(("put", key))
        self.objects[key] = body

    def delete(self, key: str) -> None:
        db = self.session_factory()
        try:
            self.seen_at_delete.append([h for (h,) in db.query(UserV2.headshot).filter(UserV2.headshot.isnot(None))])
        finally:
            db.close()
        if "delete" in self.fail:
            raise r2.HeadshotError("R2 delete failed (ClientError: InternalError)")
        self.calls.append(("delete", key))
        self.objects.pop(key, None)


# ---------------------------------------------------------
# Fixture
# ---------------------------------------------------------


class SyncFixture(unittest.TestCase):
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
        self._seed()
        self.settings = SimpleNamespace(**SETTINGS)
        self.tables = airtable_tables()
        self.source = FakeSource(self.tables)
        self.store = FakeStore(self.Session)
        self.fetched: list[str] = []

        def fetch(url: str):
            self.fetched.append(url)
            if "fetch" in self.store.fail:
                raise r2.HeadshotError("Airtable attachment download answered HTTP 410")
            return b"jpeg-bytes", "image/jpeg"

        for patcher in (
            mock.patch.object(r2, "STORE_FACTORY", lambda _cfg: self.store),
            mock.patch.object(r2, "FETCH", fetch),
            mock.patch.object(resolutions, "get_settings", lambda: self.settings),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def make_runner(self) -> SyncRunner:
        return SyncRunner(
            session_factory=self.Session,
            executor=lambda fn: fn(),
            source_factory=lambda _config: self.source,
            settings_provider=lambda: self.settings,
            heartbeat_interval=None,
        )

    def _seed(self) -> None:
        db, now = self.db, _now()

        def role(k, name, **kw):
            row = RoleV2(key=k, name=name, is_public=kw.pop("public", False), is_system=False, created_at=now)
            db.add(row)
            db.flush()
            return row.id

        self.admin_role = role("hub_admin", "Admin")
        self.user_role = role("user", "User", public=True)
        self.member_role = role("member", "Member")
        self.lead_role = role("program_lead", "Program Lead")

        def scope(k, name, **kw):
            row = ScopeV2(key=k, name=name, is_universal=kw.pop("universal", False), is_public=kw.pop("public", False), is_system=kw.pop("system", False), created_at=now)
            db.add(row)
            db.flush()
            return row.id

        self.all_scopes = scope("all_scopes", "All Scopes", universal=True, system=True)
        self.hub_scope = scope("hub", "Hub", public=True)

        self.admin = self.add_user("Admin", ADMIN_EMAIL, signed_in=True, commit=False)
        self.member = self.add_user("Member", MEMBER_EMAIL, signed_in=True, commit=False)
        # Ada agrees with Airtable on every field (her headshot: no attachment).
        self.ada = self.add_user(
            "Ada", "ada@renphil.org", signed_in=True, commit=False,
            first_name="Ada", employment_type="Full-time US", start_date=date(2024, 1, 15),
            manager_tech_stack_selections="Notion, Slack",  # another order: compared as sets
            add_to_website_date=datetime(2024, 2, 1, 9, 30, tzinfo=timezone.utc), birthday_shoutout_opt_out=True,
            is_fellow=None,  # absent = false
        )
        # Bo differs on First Name and StartDate.
        self.bo = self.add_user(
            "Bo", "bo@renphil.org", commit=False,
            first_name="Robert", employment_type="Full-time US", start_date=date(2023, 6, 1),
            manager_tech_stack_selections="Slack, Notion", add_to_website_date=datetime(2024, 2, 1, 9, 30, tzinfo=timezone.utc),
            birthday_shoutout_opt_out=True, is_fellow=False,
        )
        db.add(RoleAssignmentV2(user_id=self.admin, role_id=self.admin_role, scope_id=self.all_scopes, created_at=now))
        db.add(AirtableDesignationMappingV2(designation="Member", action="role", role_id=self.member_role, updated_at=now))
        db.add(AirtableDesignationMappingV2(designation="Program Lead", action="role", role_id=self.lead_role, updated_at=now))
        db.commit()

    def add_user(self, name: str, work=None, alias=None, *, signed_in=False, commit=True, rid=None, **kw) -> int:
        now = _now()
        row = UserV2(name=name, work_email=work, alias_email=alias, airtable_record_id=rid, has_signed_in=signed_in, created_at=now, updated_at=now, **kw)
        self.db.add(row)
        self.db.flush()
        if commit:
            self.db.commit()
        return row.id

    # ── running ──────────────────────────────────────────────────────────

    def run_sync(self, steps=ALL_STEPS, preview=False) -> AirtableSyncRunV2:
        runner = self.make_runner()
        db = self.Session()
        try:
            started = runner.start(db, steps=list(steps), preview=preview, started_by_email=ADMIN_EMAIL)
            db.commit()
        finally:
            db.close()
        runner.launch(started)
        self.db.expire_all()
        run = self.db.get(AirtableSyncRunV2, started.run_id)
        self.assertEqual(run.status, "succeeded", (run.error, run.summary))
        return run

    def users_summary(self, run: AirtableSyncRunV2) -> dict:
        return run.summary["steps"]["users"]

    def auth(self, email: str) -> dict:
        token = jwt.encode(
            {"sub": email, "name": email.split("@")[0], "roles": [], "exp": _now() + timedelta(minutes=30)},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    def resolve(self, item_id: int, choice: str, email: str = ADMIN_EMAIL):
        return self.client.post(f"/v2/airtable-sync/items/{item_id}/resolve", json={"choice": choice}, headers=self.auth(email))

    def resolved_ok(self, item_id: int, choice: str) -> dict:
        res = self.resolve(item_id, choice)
        self.assertEqual(res.status_code, 200, res.text)
        self.db.expire_all()
        return res.json()["data"]

    def bulk(self, body: dict, email: str = ADMIN_EMAIL):
        return self.client.post("/v2/airtable-sync/items/bulk-resolve", json=body, headers=self.auth(email))

    def bulk_ok(self, body: dict) -> dict:
        res = self.bulk(body)
        self.assertEqual(res.status_code, 200, res.text)
        self.db.expire_all()
        return res.json()["data"]

    def answer_fields(self, rid: str, choice: str = "keep_current") -> dict:
        data = self.bulk_ok({"item_ids": [i.id for i in self.items("field_conflict", rid=rid)], "choice": choice})
        self.assertIsNone(data["failed"])
        return data

    # ── reading ──────────────────────────────────────────────────────────

    def user(self, user_id: int) -> UserV2:
        self.db.expire_all()
        return self.db.get(UserV2, user_id)

    def by_rid(self, rid: str) -> UserV2 | None:
        self.db.expire_all()
        return self.db.query(UserV2).filter(UserV2.airtable_record_id == rid).one_or_none()

    def record(self, rid: str) -> AirtableSyncRecordV2 | None:
        self.db.expire_all()
        return self.db.query(AirtableSyncRecordV2).filter_by(entity="user", airtable_record_id=rid).one_or_none()

    def items(self, kind: str | None = None, status: str | None = "open", rid: str | None = None) -> list[AirtableSyncItemV2]:
        self.db.expire_all()
        q = self.db.query(AirtableSyncItemV2)
        if kind:
            q = q.filter(AirtableSyncItemV2.kind == kind)
        if status:
            q = q.filter(AirtableSyncItemV2.status == status)
        if rid:
            q = q.filter(AirtableSyncItemV2.airtable_record_id == rid)
        return q.order_by(AirtableSyncItemV2.id).all()

    def item(self, kind: str, rid: str, field: str = "") -> AirtableSyncItemV2:
        found = [i for i in self.items(kind, rid=rid) if (i.field or "") == field]
        self.assertEqual(len(found), 1, [(i.kind, i.airtable_record_id, i.field) for i in self.items()])
        return found[0]

    def logs(self, run: AirtableSyncRunV2 | None = None, action: str | None = None, rid: str | None = None) -> list[AirtableSyncLogV2]:
        self.db.expire_all()
        q = self.db.query(AirtableSyncLogV2)
        q = q.filter(AirtableSyncLogV2.run_id == run.id) if run is not None else q.filter(AirtableSyncLogV2.run_id.is_(None))
        if action:
            q = q.filter(AirtableSyncLogV2.action == action)
        if rid:
            q = q.filter(AirtableSyncLogV2.airtable_record_id == rid)
        return q.order_by(AirtableSyncLogV2.id).all()

    def at(self, n: int) -> dict:
        for r in self.tables[T_USERS]["records"]:
            if r["fields"].get("Record ID") == U(n):
                return r["fields"]
        raise KeyError(n)

    def hub_counts(self) -> dict[str, int]:
        self.db.expire_all()
        return {
            t.name: self.db.query(func.count()).select_from(t).scalar()
            for t in BaseV2.metadata.sorted_tables
            if t.name not in ("airtable_sync_runs", "airtable_sync_log")
        }

    def edit_user(self, user_id: int, **values) -> None:
        self.db.expire_all()
        row = self.db.get(UserV2, user_id)
        for k, v in values.items():
            setattr(row, k, v)
        self.db.commit()

    def linked_and_agreed(self) -> None:
        """First run, and Bo's first-link conflicts answered with Keep
        current: Ada and Bo linked with a full base, Cy created."""
        self.run_sync()
        self.answer_fields(U(2), "keep_current")
        self.store.calls.clear()
        self.fetched.clear()


# ---------------------------------------------------------
# Pure: canonical values, the planner, R2 helpers
# ---------------------------------------------------------


class TestCanonical(unittest.TestCase):
    def f(self, name: str) -> hr_fields.HrField:
        return hr_fields.BY_NAME[name]

    def test_airtable_values(self) -> None:
        fields = {
            "employment_type": ["Full-time US", "Fellow"],
            "manager_tech_stack_selections": ["b", "a", "b"],
            "birthday_shoutout_opt_out": False,
            "headshot": attachment("attX", "x.jpg"),
        }
        self.assertEqual(hr_fields.airtable_value(self.f("employment_type"), fields), ["Fellow", "Full-time US"])
        self.assertEqual(hr_fields.airtable_value(self.f("manager_tech_stack_selections"), fields), ["a", "b"])
        self.assertTrue(hr_fields.airtable_value(self.f("is_fellow"), fields))  # derived (owner, 2026-10-23)
        self.assertFalse(hr_fields.airtable_value(self.f("is_fellow"), {"employment_type": []}))
        self.assertFalse(hr_fields.airtable_value(self.f("birthday_shoutout_opt_out"), fields))
        self.assertEqual(hr_fields.airtable_value(self.f("headshot"), fields), "attX")
        self.assertIs(hr_fields.airtable_value(self.f("bio"), fields), hr_fields.MISSING)  # not read

    def test_hub_values(self) -> None:
        cfg = r2.R2Config.from_settings(SimpleNamespace(**SETTINGS))
        row = UserV2(
            name=" Ada ", work_email="ADA@renphil.org", manager_tech_stack_selections="Slack, Notion",
            start_date=date(2024, 1, 15), add_to_website_date=datetime(2024, 2, 1, 9, 30), birthday_shoutout_opt_out=None,
            headshot=headshot_url("attH", "a b.jpg"),
        )
        self.assertEqual(hr_fields.hub_value(self.f("name"), row, cfg), "Ada")
        self.assertEqual(hr_fields.hub_value(self.f("work_email"), row, cfg), "ada@renphil.org")
        self.assertEqual(hr_fields.hub_value(self.f("manager_tech_stack_selections"), row, cfg), ["Notion", "Slack"])
        self.assertEqual(hr_fields.hub_value(self.f("start_date"), row, cfg), "2024-01-15")
        self.assertEqual(hr_fields.hub_value(self.f("add_to_website_date"), row, cfg), "2024-02-01T09:30:00Z")  # naive = UTC
        self.assertFalse(hr_fields.hub_value(self.f("birthday_shoutout_opt_out"), row, cfg))  # NULL = false
        self.assertFalse(hr_fields.hub_value(self.f("is_fellow"), row, cfg))
        self.assertEqual(hr_fields.hub_value(self.f("headshot"), row, cfg), "attH")
        row.headshot = "https://elsewhere.test/me.jpg"
        self.assertEqual(hr_fields.hub_value(self.f("headshot"), row, cfg), "https://elsewhere.test/me.jpg")

    def test_column_values(self) -> None:
        fields = {"manager_tech_stack_selections": ["Slack", "Notion", "Slack"], "start_date": "2024-01-15", "add_to_website_date": "2024-02-01T09:30:00Z"}
        self.assertEqual(hr_fields.column_value(self.f("manager_tech_stack_selections"), fields), "Slack, Notion")  # Airtable's order
        self.assertEqual(hr_fields.column_value(self.f("start_date"), fields), date(2024, 1, 15))
        self.assertEqual(hr_fields.column_value(self.f("add_to_website_date"), fields), datetime(2024, 2, 1, 9, 30, tzinfo=timezone.utc))
        self.assertFalse(hr_fields.valid(self.f("start_date"), "next week"))


class TestPlanner(unittest.TestCase):
    """Every three-way cell, on `first_name` (and E1 / email_taken)."""

    def plan(self, base: dict, airtable: str | None, hub: str | None, **kw) -> hr_fields.Plan:
        row = UserV2(name="P", work_email="p@renphil.org", first_name=hub)
        fields = {"name": "P", "work_email": "p@renphil.org", "first_name": airtable}
        return hr_fields.plan_fields(row, {"name": "P", "work_email": "p@renphil.org", **base}, fields, signed_in=kw.get("signed_in", False), r2=None, email_owner=kw.get("owner", lambda e: None))

    def outcome(self, plan: hr_fields.Plan) -> str:
        for name in ("updates", "conflicts", "hub_differs"):
            if any(c.field == "first_name" for c in getattr(plan, name)):
                return name
        return f"base={plan.base.get('first_name', 'MISSING')}"

    def test_cells(self) -> None:
        self.assertEqual(self.outcome(self.plan({}, "A", "A")), "base=A")  # first link, equal
        self.assertEqual(self.outcome(self.plan({}, "A", "H")), "conflicts")  # first link, differ (M2)
        self.assertEqual(self.outcome(self.plan({}, "A", None)), "conflicts")  # the Hub blank still differs (M2)
        self.assertEqual(self.outcome(self.plan({"first_name": "B"}, "B", "B")), "base=B")  # nothing
        self.assertEqual(self.outcome(self.plan({"first_name": "B"}, "B", "H")), "hub_differs")
        self.assertEqual(self.outcome(self.plan({"first_name": "B"}, "A", "B")), "updates")
        self.assertEqual(self.outcome(self.plan({"first_name": "B"}, "A", "A")), "base=A")  # both the same: base moves
        self.assertEqual(self.outcome(self.plan({"first_name": "B"}, "A", "H")), "conflicts")
        self.assertEqual(self.outcome(self.plan({"first_name": "B"}, None, "B")), "updates")  # cleared in Airtable

    def test_signed_in_work_email_is_e1_and_taken_emails(self) -> None:
        row = UserV2(name="P", work_email="old@renphil.org", alias_email=None)
        fields = {"name": "P", "work_email": "new@renphil.org", "alias_email": None}
        base = {"name": "P", "work_email": "old@renphil.org", "alias_email": None}
        plan = hr_fields.plan_fields(row, base, fields, signed_in=True, r2=None, email_owner=lambda e: None)
        self.assertEqual((plan.email_change.airtable, plan.email_change.hub), ("new@renphil.org", "old@renphil.org"))
        self.assertTrue(plan.alias_offer)
        self.assertEqual(plan.updates, [])
        plan = hr_fields.plan_fields(row, base, fields, signed_in=False, r2=None, email_owner=lambda e: None)
        self.assertEqual([c.field for c in plan.updates], ["work_email"])  # not signed in: automatic
        plan = hr_fields.plan_fields(row, base, fields, signed_in=True, r2=None, email_owner=lambda e: {"user_id": 99})
        self.assertIsNone(plan.email_change)
        self.assertEqual([(c.field, o["user_id"]) for c, o in plan.taken], [("work_email", 99)])
        # A blank Airtable Work Email is never merged (no_work_email holds).
        plan = hr_fields.plan_fields(row, base, {**fields, "work_email": None}, signed_in=True, r2=None, email_owner=lambda e: None)
        self.assertIsNone(plan.email_change)
        self.assertEqual(plan.updates, [])

    def test_creation_and_skips(self) -> None:
        fields = {"name": "N", "work_email": "n@renphil.org", "alias_email": None, "start_date": "soon", "headshot": attachment("attN", "n.jpg")}
        plan = hr_fields.plan_fields(None, {}, fields, signed_in=False, r2=None, email_owner=lambda e: None)
        self.assertEqual({c.field for c in plan.updates}, {"name", "work_email", "alias_email"})
        self.assertEqual([c.field for c in plan.invalid], ["start_date"])
        self.assertTrue(plan.headshot_skipped)  # no R2 settings


class TestR2Config(unittest.TestCase):
    def test_from_settings_and_paths(self) -> None:
        cfg = r2.R2Config.from_settings(SimpleNamespace(**SETTINGS))
        self.assertNotIn(R2_SECRET, repr(cfg))
        self.assertNotIn(R2_KEY_ID, repr(cfg))
        self.assertEqual(cfg.public_base_url, PUBLIC)
        self.assertEqual(cfg.key_for("attX", "a b.jpg"), "headshots/attX/a b.jpg")
        self.assertEqual(cfg.url_for("headshots/attX/a b.jpg"), headshot_url("attX", "a b.jpg"))  # unencoded, like Dev's
        self.assertEqual(cfg.key_of(headshot_url("attX", "a.jpg")), "headshots/attX/a.jpg")
        self.assertIsNone(cfg.key_of(f"{PUBLIC}/other/attX/a.jpg"))
        self.assertIsNone(cfg.key_of("https://elsewhere.test/headshots/attX/a.jpg"))
        self.assertEqual(cfg.attachment_id_of(headshot_url("attX", "a.jpg")), "attX")
        self.assertIsNone(r2.R2Config.from_settings(SimpleNamespace(**{**SETTINGS, "R2_BUCKET": " "})))
        built = airtable_reader.build_config(SimpleNamespace(**SETTINGS))
        self.assertNotIn(R2_SECRET, repr(built))

    def test_errors_never_carry_the_message(self) -> None:
        exc = Exception(f"bad key {R2_KEY_ID}")
        exc.response = {"Error": {"Code": "InvalidAccessKeyId"}}
        self.assertEqual(r2._describe(exc), "Exception: InvalidAccessKeyId")


# ---------------------------------------------------------
# The Users step: three-way, M2, M3, creation
# ---------------------------------------------------------


class TestHrMerge(SyncFixture):
    def test_first_run_m2(self) -> None:
        run = self.run_sync()
        s = self.users_summary(run)
        # Ada agrees on everything → linked, a full base.
        self.assertEqual(self.by_rid(U(1)).id, self.ada)
        base = self.record(U(1)).base
        self.assertEqual(base["manager_tech_stack_selections"], ["Notion", "Slack"])
        self.assertEqual((base["start_date"], base["is_fellow"], base["add_to_website_date"]), ("2024-01-15", False, "2024-02-01T09:30:00Z"))
        self.assertIsNone(base["headshot"])
        # Bo: one field_conflict per differing field, holding him.
        conflicts = {i.field: i.payload for i in self.items("field_conflict", rid=U(2))}
        self.assertEqual(set(conflicts), {"first_name", "start_date"})
        self.assertEqual(
            {k: conflicts["start_date"][k] for k in ("base", "airtable", "hub", "first_link")},
            {"base": None, "airtable": "2024-01-15", "hub": "2023-06-01", "first_link": True},
        )
        self.assertEqual(conflicts["first_name"]["hub_user"]["user_id"], self.bo)
        self.assertEqual(conflicts["first_name"]["choices"], ["accept_airtable", "keep_current"])
        self.assertIsNone(self.by_rid(U(2)))
        self.assertEqual(s["held"], 1)
        # A field_conflict holds the person's Program Roles pairs too.
        pr = run.summary["steps"]["program_roles"]
        self.assertEqual(pr["user_not_synced"], 1)
        self.assertEqual(self.db.query(RoleAssignmentV2).filter_by(user_id=self.bo).count(), 0)

    def test_creation_gets_every_hr_field(self) -> None:
        self.run_sync()
        cy = self.by_rid(U(3))
        self.assertEqual((cy.first_name, cy.employment_type, cy.start_date), ("Cy", "Fellow", date(2024, 1, 15)))
        self.assertTrue(cy.is_fellow)  # derived from Employment Type
        self.assertEqual(cy.manager_tech_stack_selections, "Slack, Notion")
        self.assertTrue(cy.birthday_shoutout_opt_out)
        self.assertEqual(cy.headshot, headshot_url("attCY1", "cy pic.jpg"))
        self.assertFalse(cy.has_signed_in)
        self.assertEqual(self.store.calls, [("put", "headshots/attCY1/cy pic.jpg")])
        self.assertEqual(self.record(U(3)).base["headshot"], "attCY1")
        self.assertEqual(self.record(U(3)).base["employment_type"], ["Fellow"])

    def test_creation_with_a_failed_upload_retries_next_run(self) -> None:
        self.store.fail.add("put")
        run = self.run_sync()
        cy = self.by_rid(U(3))
        self.assertIsNone(cy.headshot)
        self.assertIsNone(self.record(U(3)).base["headshot"])
        self.assertEqual(len(self.logs(run, "headshot_upload_failed", rid=U(3))), 1)
        self.store.fail.clear()
        self.run_sync()
        self.assertEqual(self.by_rid(U(3)).headshot, headshot_url("attCY1", "cy pic.jpg"))
        self.assertEqual(self.items(rid=U(3)), [])  # an update, not a first-link conflict

    def test_keep_current_moves_the_base_m3(self) -> None:
        self.run_sync()
        out = self.answer_fields(U(2), "keep_current")
        self.assertEqual([r["outcome"]["applied"] for r in out["resolved"]], [False, True])  # the last applies (C4)
        bo = self.user(self.bo)
        self.assertEqual((bo.airtable_record_id, bo.first_name, bo.start_date), (U(2), "Robert", date(2023, 6, 1)))
        base = self.record(U(2)).base
        self.assertEqual((base["first_name"], base["start_date"]), ("Bo", "2024-01-15"))
        # Next run: the Hub differs from the agreed value → kept, logged once per field.
        run = self.run_sync()
        differs = sorted(l.field for l in self.logs(run, "hub_differs", rid=U(2)))
        self.assertEqual(differs, ["first_name", "start_date"])
        self.assertEqual(self.items(rid=U(2)), [])
        # The pair, held before, is given now.
        self.assertEqual(self.db.query(RoleAssignmentV2).filter_by(user_id=self.bo).count(), 1)

    def test_accept_airtable_writes(self) -> None:
        self.run_sync()
        self.answer_fields(U(2), "accept_airtable")
        bo = self.user(self.bo)
        self.assertEqual((bo.first_name, bo.start_date), ("Bo", date(2024, 1, 15)))
        updated = {l.field: (l.old_value, l.new_value) for l in self.logs(action="field_updated", rid=U(2))}
        self.assertEqual(updated, {"first_name": ("Robert", "Bo"), "start_date": ("2023-06-01", "2024-01-15")})
        self.assertTrue(all(l.actor_email == ADMIN_EMAIL for l in self.logs(action="field_updated")))

    def test_three_way_after_the_base(self) -> None:
        self.linked_and_agreed()
        # Airtable changes, the Hub still has the base value → updated.
        self.at(1)["Manager Tech Stack Selections"] = ["Figma", "Slack"]
        self.at(1)["Birthday Shout-Out Opt-Out"] = None  # unticked: absent = false
        self.at(1)["Employment Type"] = ["Fellow"]
        run = self.run_sync()
        ada = self.user(self.ada)
        self.assertEqual(ada.manager_tech_stack_selections, "Figma, Slack")
        self.assertFalse(ada.birthday_shoutout_opt_out)
        self.assertTrue(ada.is_fellow)
        self.assertEqual(
            {l.field for l in self.logs(run, "field_updated", rid=U(1))},
            {"manager_tech_stack_selections", "birthday_shoutout_opt_out", "employment_type", "is_fellow"},
        )
        self.assertEqual(self.record(U(1)).base["manager_tech_stack_selections"], ["Figma", "Slack"])
        # Reordered multi-select: the same set → nothing.
        self.at(1)["Manager Tech Stack Selections"] = ["Slack", "Figma"]
        run = self.run_sync()
        self.assertEqual(self.logs(run, "field_updated", rid=U(1)), [])
        # Both sides changed, differently → a conflict (person-level).
        self.edit_user(self.ada, first_name="Adelaide")
        self.at(1)["First Name"] = "Addie"
        run = self.run_sync()
        conflict = self.item("field_conflict", U(1), "first_name")
        self.assertEqual({k: conflict.payload[k] for k in ("base", "airtable", "hub", "first_link")}, {"base": "Ada", "airtable": "Addie", "hub": "Adelaide", "first_link": False})
        self.assertIn(U(1), {l.airtable_record_id for l in self.logs(run, "held")})
        # Both changed the same way → the base moves, no item.
        self.at(1)["First Name"] = "Adelaide"
        self.run_sync()
        self.assertEqual(self.items("field_conflict", rid=U(1)), [])
        self.assertEqual(self.record(U(1)).base["first_name"], "Adelaide")

    def test_email_case_and_dates_normalize(self) -> None:
        self.at(1)["Work Email"] = "  ADA@RenPhil.org "
        self.at(1)["StartDate"] = "2024-01-15T00:00:00.000Z"
        self.run_sync()
        self.assertEqual(self.items(rid=U(1)), [])
        self.assertEqual(self.by_rid(U(1)).id, self.ada)

    def test_m7_deleted_person_untouched(self) -> None:
        self.linked_and_agreed()
        self.edit_user(self.ada, is_deleted=True, deleted_at=_now(), deleted_by=ADMIN_EMAIL)
        self.at(1)["First Name"] = "Changed"
        self.at(1)["Headshot"] = attachment("attNEW", "n.jpg")
        run = self.run_sync()
        self.assertEqual(self.user(self.ada).first_name, "Ada")
        self.assertEqual(self.logs(run, "field_updated", rid=U(1)), [])
        self.assertEqual(self.store.calls, [])

    def test_data_refusal_rolls_back_one_person(self) -> None:
        self.linked_and_agreed()
        self.at(1)["First Name"] = "Ada II"
        self.at(3)["First Name"] = "Cy II"

        from sqlalchemy.exc import IntegrityError

        def fail_on(writer, row, hu, c, fields, log, **kw):
            if row is not None and row.id == self.ada:
                raise IntegrityError("UPDATE", {}, Exception("duplicate key"))
            return real(writer, row, hu, c, fields, log, **kw)

        from app.services.airtable_sync import users_step

        real = users_step.UserWriter.write_field
        with mock.patch.object(users_step.UserWriter, "write_field", fail_on):
            run = self.run_sync()
        self.assertEqual([l.airtable_record_id for l in self.logs(run, "user_error")], [U(1)])
        self.assertEqual(self.user(self.ada).first_name, "Ada")
        self.assertEqual(self.record(U(1)).base["first_name"], "Ada")  # not moved
        self.assertEqual(self.by_rid(U(3)).first_name, "Cy II")  # the others went on


# ---------------------------------------------------------
# E1: Work Email of a signed-in person
# ---------------------------------------------------------


class TestWorkEmail(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.linked_and_agreed()
        self.at(1)["Work Email"] = "ada.new@renphil.org"

    def test_e1_raised_with_the_alias_offer(self) -> None:
        run = self.run_sync()
        item = self.item("work_email_change_signed_in", U(1))
        self.assertEqual(item.payload["choices"], ["apply_keep_alias", "apply_drop_old", "skip"])
        self.assertEqual((item.payload["airtable"], item.payload["hub"]), ("ada.new@renphil.org", "ada@renphil.org"))
        self.assertEqual(self.user(self.ada).work_email, "ada@renphil.org")
        self.assertIn(U(1), {l.airtable_record_id for l in self.logs(run, "held")})
        # No alias offer when the Hub has an alias.
        self.edit_user(self.ada, alias_email="ada.alias@renphil.org")
        self.run_sync()
        self.assertEqual(self.item("work_email_change_signed_in", U(1)).payload["choices"], ["apply_drop_old", "skip"])
        res = self.resolve(self.item("work_email_change_signed_in", U(1)).id, "apply_keep_alias")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "invalid_choice"))

    def test_apply_keep_alias(self) -> None:
        self.run_sync()
        out = self.resolved_ok(self.item("work_email_change_signed_in", U(1)).id, "apply_keep_alias")
        self.assertTrue(out["outcome"]["applied"])
        ada = self.user(self.ada)
        self.assertEqual((ada.work_email, ada.alias_email), ("ada.new@renphil.org", "ada@renphil.org"))
        base = self.record(U(1)).base
        self.assertEqual((base["work_email"], base["alias_email"]), ("ada.new@renphil.org", None))
        self.assertTrue(self.logs(action="email_change_not_rekeyed", rid=U(1)))
        run = self.run_sync()  # the Hub's alias now differs from Airtable's: kept
        self.assertEqual([l.field for l in self.logs(run, "hub_differs", rid=U(1))], ["alias_email"])
        self.assertEqual(self.user(self.ada).alias_email, "ada@renphil.org")

    def test_apply_drop_old(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("work_email_change_signed_in", U(1)).id, "apply_drop_old")
        ada = self.user(self.ada)
        self.assertEqual((ada.work_email, ada.alias_email), ("ada.new@renphil.org", None))
        self.run_sync()
        self.assertEqual(self.items(rid=U(1)), [])

    def test_skip_holds(self) -> None:
        self.run_sync()
        item = self.item("work_email_change_signed_in", U(1))
        self.resolved_ok(item.id, "skip")
        self.at(1)["First Name"] = "Ada II"
        run = self.run_sync()
        self.assertEqual(self.user(self.ada).work_email, "ada@renphil.org")
        self.assertEqual(self.user(self.ada).first_name, "Ada")  # the whole person is held
        self.assertIn(U(1), {l.airtable_record_id for l in self.logs(run, "held")})
        self.assertEqual(self.items(rid=U(1)), [])  # remembered

    def test_not_signed_in_is_automatic(self) -> None:
        self.edit_user(self.ada, has_signed_in=False)
        run = self.run_sync()
        self.assertEqual(self.user(self.ada).work_email, "ada.new@renphil.org")
        self.assertEqual([l.field for l in self.logs(run, "field_updated", rid=U(1))], ["work_email"])

    def test_email_taken_with_a_field(self) -> None:
        other = self.add_user("Other", "ada.new@renphil.org")
        self.at(2)["Alias Email"] = "member@renphil.org"
        self.run_sync()
        taken = self.item("email_taken", U(1), "work_email")
        self.assertEqual((taken.payload["email"], taken.payload["taken_by"]["user_id"]), ("ada.new@renphil.org", other))
        self.assertEqual(self.items("work_email_change_signed_in"), [])
        self.assertEqual(self.item("email_taken", U(2), "alias_email").payload["taken_by"]["user_id"], self.member)
        self.assertEqual(self.resolve(taken.id, "skip").json()["detail"]["code"], "item_has_no_choices")
        # Fixed in the Hub → the item closes; the E1 warning takes its place.
        self.edit_user(other, work_email="other@renphil.org")
        self.run_sync()
        self.assertEqual(self.items("email_taken", rid=U(1)), [])
        self.item("work_email_change_signed_in", U(1))


# ---------------------------------------------------------
# Headshots (A-5)
# ---------------------------------------------------------


class TestHeadshots(SyncFixture):
    OLD = headshot_url("attOLD", "old.jpg")

    def setUp(self) -> None:
        super().setUp()
        self.at(1)["Headshot"] = attachment("attOLD", "old.jpg")
        self.edit_user(self.ada, headshot=self.OLD)
        self.linked_and_agreed()

    def test_upload_only_on_a_new_attachment_and_delete_after_commit(self) -> None:
        self.run_sync()
        self.assertEqual(self.store.calls, [])  # same attachment id
        self.at(1)["Headshot"] = attachment("attNEW", "new pic.png")
        run = self.run_sync()
        new = headshot_url("attNEW", "new pic.png")
        self.assertEqual(self.user(self.ada).headshot, new)
        self.assertEqual(self.store.calls, [("put", "headshots/attNEW/new pic.png"), ("delete", "headshots/attOLD/old.jpg")])
        self.assertNotIn(self.OLD, self.store.seen_at_delete[0])  # the Hub had switched
        self.assertIn(new, self.store.seen_at_delete[0])
        self.assertEqual(self.logs(run, "headshot_object_deleted", rid=U(1))[0].old_value, "headshots/attOLD/old.jpg")
        self.assertEqual(self.record(U(1)).base["headshot"], "attNEW")

    def test_cleared_in_airtable(self) -> None:
        del self.at(1)["Headshot"]
        self.run_sync()
        self.assertIsNone(self.user(self.ada).headshot)
        self.assertEqual(self.store.calls, [("delete", "headshots/attOLD/old.jpg")])

    def test_never_on_rollback(self) -> None:
        from sqlalchemy.exc import IntegrityError

        from app.services.airtable_sync import users_step

        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        real = users_step.UserWriter.save_record

        def save(writer, rid, *a, **kw):
            if rid == U(1) and kw.get("base") is not None:
                raise IntegrityError("UPDATE", {}, Exception("duplicate key"))
            return real(writer, rid, *a, **kw)

        with mock.patch.object(users_step.UserWriter, "save_record", save):
            run = self.run_sync()
        self.assertEqual(self.user(self.ada).headshot, self.OLD)
        self.assertEqual(self.store.calls, [("put", "headshots/attNEW/new.jpg")])  # an orphan, accepted
        self.assertTrue(self.logs(run, "user_error", rid=U(1)))
        # The rolled-back person's old object was never even considered.
        self.assertEqual([l.action for l in self.logs(run) if l.action.startswith("headshot_")], [])

    def test_never_for_a_hub_kept_value(self) -> None:
        self.edit_user(self.ada, headshot=headshot_url("attHUB", "mine.jpg"))
        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        self.run_sync()  # both changed → conflict, nothing touched
        self.assertEqual(self.store.calls, [])
        self.resolved_ok(self.item("field_conflict", U(1), "headshot").id, "keep_current")
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.user(self.ada).headshot, headshot_url("attHUB", "mine.jpg"))

    def test_accept_airtable_uploads_and_deletes_after_the_resolution_commits(self) -> None:
        self.edit_user(self.ada, headshot=headshot_url("attHUB", "mine.jpg"))
        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        self.run_sync()
        self.resolved_ok(self.item("field_conflict", U(1), "headshot").id, "accept_airtable")
        self.assertEqual(self.user(self.ada).headshot, headshot_url("attNEW", "new.jpg"))
        self.assertEqual(self.store.calls, [("put", "headshots/attNEW/new.jpg"), ("delete", "headshots/attHUB/mine.jpg")])
        self.assertNotIn(headshot_url("attHUB", "mine.jpg"), self.store.seen_at_delete[0])
        deleted = self.logs(action="headshot_object_deleted")
        self.assertEqual(len(deleted), 1)
        self.assertIsNotNone(deleted[0].resolution_item_id)

    def test_never_outside_the_area_or_when_shared(self) -> None:
        self.edit_user(self.ada, headshot="https://elsewhere.test/old.jpg")
        self.at(1)["Headshot"] = attachment("attX", "x.jpg")
        self.run_sync()  # the Hub URL is not the base's id → conflict
        self.resolved_ok(self.item("field_conflict", U(1), "headshot").id, "accept_airtable")
        self.assertEqual(self.store.calls, [("put", "headshots/attX/x.jpg")])
        self.assertEqual(self.logs(action="headshot_delete_skipped")[0].new_value, "outside_r2_area")
        # A URL another row still holds (the D-3 duplicates) is kept.
        shared = headshot_url("attX", "x.jpg")
        self.add_user("Twin", "twin@renphil.org", headshot=shared)
        self.at(1)["Headshot"] = attachment("attY", "y.jpg")
        run = self.run_sync()
        self.assertEqual(self.store.calls[-1], ("put", "headshots/attY/y.jpg"))
        self.assertEqual(self.logs(run, "headshot_delete_skipped")[0].new_value, "still_used")

    def test_upload_failure_keeps_the_field_and_does_not_hold(self) -> None:
        self.store.fail.add("put")
        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        self.at(1)["First Name"] = "Ada II"
        run = self.run_sync()
        ada = self.user(self.ada)
        self.assertEqual((ada.headshot, ada.first_name), (self.OLD, "Ada II"))
        self.assertEqual(self.record(U(1)).base["headshot"], "attOLD")  # tried again next run
        failed = self.logs(run, "headshot_upload_failed", rid=U(1))
        self.assertEqual(len(failed), 1)
        self.assertNotIn(R2_SECRET, failed[0].new_value)
        self.assertEqual(self.users_summary(run)["headshot_upload_failed"], 1)
        self.store.fail.clear()
        self.run_sync()
        self.assertEqual(self.user(self.ada).headshot, headshot_url("attNEW", "new.jpg"))

    def test_a_failed_delete_only_logs(self) -> None:
        self.store.fail.add("delete")
        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        run = self.run_sync()
        self.assertEqual(self.user(self.ada).headshot, headshot_url("attNEW", "new.jpg"))
        self.assertEqual(self.logs(run, "headshot_delete_failed", rid=U(1))[0].old_value, "headshots/attOLD/old.jpg")
        self.assertEqual(self.users_summary(run)["headshot_delete_failed"], 1)

    def test_preview_never_calls_r2(self) -> None:
        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        self.at(3)["Headshot"] = attachment("attCY2", "cy2.jpg")
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual(self.user(self.ada).headshot, self.OLD)
        would = self.logs(run, "field_updated", rid=U(1))
        self.assertEqual([(l.field, l.new_value) for l in would], [("headshot", headshot_url("attNEW", "new.jpg"))])

    def test_r2_missing_skips_with_one_warning(self) -> None:
        self.settings.R2_BUCKET = ""
        self.at(1)["Headshot"] = attachment("attNEW", "new.jpg")
        self.at(3)["Headshot"] = attachment("attCY2", "cy2.jpg")
        run = self.run_sync()
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.user(self.ada).headshot, self.OLD)
        self.assertEqual(len(self.logs(run, "headshot_skipped")), 1)
        self.assertEqual(self.users_summary(run)["headshot_skipped_no_r2"], 3)  # every person whose Headshot was read
        self.assertEqual(self.items("field_conflict"), [])


# ---------------------------------------------------------
# Bulk resolve, access, the run lock, the lock sweep
# ---------------------------------------------------------


class TestBulkResolve(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        # Dee: a second person with one first-link conflict (First Name).
        self.dee = self.add_user(
            "Dee", "dee@renphil.org", first_name="D.", employment_type="Full-time US", start_date=date(2024, 1, 15),
            manager_tech_stack_selections="Slack, Notion", add_to_website_date=datetime(2024, 2, 1, 9, 30, tzinfo=timezone.utc),
            birthday_shoutout_opt_out=True,
        )
        self.tables[T_USERS]["records"].append(person(4, "Dee", "dee@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(4, U(4)))
        self.run = self.run_sync()

    def test_per_person(self) -> None:
        ids = [i.id for i in self.items("field_conflict", rid=U(2))]
        out = self.bulk_ok({"item_ids": list(reversed(ids)), "choice": "accept_airtable"})
        self.assertEqual([r["item_id"] for r in out["resolved"]], sorted(ids))  # id order
        self.assertEqual(self.user(self.bo).first_name, "Bo")
        self.assertEqual(len(self.items("field_conflict", rid=U(4))), 1)  # Dee untouched

    def test_whole_run_with_kind(self) -> None:
        out = self.bulk_ok({"run_id": self.run.id, "kind": "field_conflict", "choice": "keep_current"})
        self.assertEqual(len(out["resolved"]), 3)
        self.assertEqual(self.items("field_conflict"), [])
        self.assertEqual((self.by_rid(U(2)).id, self.by_rid(U(4)).id), (self.bo, self.dee))
        # Items of another run are not picked.
        self.assertEqual(self.bulk_ok({"run_id": self.run.id + 99, "choice": "keep_current"})["resolved"], [])

    def test_whole_run_skips_items_without_the_choice(self) -> None:
        # Eve: a possible_match (Link / Create new / Skip), open in the same run.
        self.add_user("Eve", "eve.old@renphil.org")
        self.tables[T_USERS]["records"].append(person(5, "Eve", "eve@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(5, U(5)))
        run = self.run_sync()
        eve = self.item("possible_match", U(5))
        out = self.bulk_ok({"run_id": run.id, "choice": "keep_current"})
        self.assertIsNone(out["failed"])
        self.assertEqual(len(out["resolved"]), 3)  # the field conflicts only
        self.assertEqual(self.item("possible_match", U(5)).id, eve.id)  # still open

    def test_stops_at_the_first_error(self) -> None:
        bo = [i.id for i in self.items("field_conflict", rid=U(2))]
        dee = [i.id for i in self.items("field_conflict", rid=U(4))]
        bad = self.items("field_conflict", rid=U(2))[0]
        self.resolved_ok(bad.id, "keep_current")  # no longer open
        out = self.bulk_ok({"item_ids": bo + dee, "choice": "accept_airtable"})
        self.assertEqual(out["resolved"], [])
        self.assertEqual((out["failed"]["item_id"], out["failed"]["code"]), (bad.id, "item_not_open"))
        self.assertEqual(out["not_attempted"], sorted(bo + dee)[1:])
        self.assertEqual(len(self.items("field_conflict", rid=U(4))), 1)

    def test_earlier_items_are_kept(self) -> None:
        dee = self.items("field_conflict", rid=U(4))[0]
        self.db.add(AirtableSyncItemV2(entity="user", airtable_record_id="recZZ", kind="no_work_email", field="", payload={}, fingerprint="f" * 64, status="open", first_run_id=self.run.id, last_run_id=self.run.id))
        self.db.commit()
        z = self.items("no_work_email", rid="recZZ")[0]
        out = self.bulk_ok({"item_ids": [dee.id, z.id], "choice": "keep_current"})
        self.assertEqual([r["item_id"] for r in out["resolved"]], [dee.id])
        self.assertEqual(out["failed"]["code"], "item_has_no_choices")
        self.assertEqual(self.by_rid(U(4)).id, self.dee)  # the first one was committed

    def test_request_shape(self) -> None:
        self.assertEqual(self.bulk({"choice": "keep_current"}).status_code, 422)
        self.assertEqual(self.bulk({"item_ids": [1], "run_id": 1, "choice": "keep_current"}).status_code, 422)
        self.assertEqual(self.bulk({"item_ids": [1], "kind": "x", "choice": "keep_current"}).status_code, 422)
        self.assertEqual(self.bulk({"item_ids": [], "choice": "keep_current"}).status_code, 422)

    def test_admins_only_and_409_during_a_run(self) -> None:
        ids = [i.id for i in self.items("field_conflict")]
        res = self.bulk({"item_ids": ids, "choice": "keep_current"}, email=MEMBER_EMAIL)
        self.assertEqual(res.status_code, 403)
        anonymous = self.client.post("/v2/airtable-sync/items/bulk-resolve", json={"item_ids": ids, "choice": "keep_current"})
        self.assertIn(anonymous.status_code, (401, 403))
        self.db.add(AirtableSyncRunV2(steps=["users"], trigger="manual", status="running", started_at=_now(), heartbeat_at=_now()))
        self.db.commit()
        res = self.bulk({"item_ids": ids, "choice": "keep_current"})
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "run_in_progress"))
        res = self.resolve(ids[0], "keep_current")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "run_in_progress"))
        self.assertEqual(len(self.items("field_conflict")), len(ids))


class TestPreview(SyncFixture):
    def test_first_run_preview_writes_only_log_rows(self) -> None:
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        would = {(l.airtable_record_id, l.field) for l in self.logs(run, "would_raise_field_conflict")}
        self.assertEqual(would, {(U(2), "first_name"), (U(2), "start_date")})
        self.assertEqual(self.store.calls, [])
        created = self.logs(run, "user_created", rid=U(3))[0]
        self.assertIn('"is_fellow": true', created.new_value)

    def test_preview_after_a_real_run(self) -> None:
        self.linked_and_agreed()
        self.at(1)["First Name"] = "Ada II"
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual(self.user(self.ada).first_name, "Ada")
        self.assertEqual([(l.field, l.new_value) for l in self.logs(run, "field_updated", rid=U(1))], [("first_name", "Ada II")])


class TestLockSweep(SyncFixture):
    def test_run_sweeps_once_after_a_replace_never_in_a_preview(self) -> None:
        with mock.patch.object(runner_module, "release_unauthorized_locks", return_value=0) as sweep:
            self.linked_and_agreed()
            self.assertEqual(sweep.call_count, 0)  # nothing removed
            self.tables[T_ROLES]["records"][0]["fields"]["Designation"] = "Program Lead"
            self.run_sync(preview=True)
            self.assertEqual(sweep.call_count, 0)
            run = self.run_sync()
            self.assertEqual(sweep.call_count, 1)
            self.assertEqual(run.summary["locks_released"], 0)
            self.assertEqual(run.summary["steps"]["program_roles"]["replaced"], 1)

    def test_resolve_and_bulk_sweep_after_commit(self) -> None:
        self.run_sync()
        ids = [i.id for i in self.items("field_conflict", rid=U(2))]
        with mock.patch.object(resolutions, "release_unauthorized_locks", return_value=0) as sweep:
            self.resolved_ok(ids[0], "keep_current")
            self.assertEqual(sweep.call_count, 1)
            self.bulk_ok({"item_ids": ids[1:], "choice": "keep_current"})
            self.assertEqual(sweep.call_count, 2)

    def test_sweep_helper(self) -> None:
        db = self.Session()
        try:
            self.assertEqual(lock_sweep.release_unauthorized_locks(db), 0)  # nothing held
            with mock.patch("app.services.edit_lock_service.release_locks_now_unauthorized", side_effect=RuntimeError("boom")):
                self.assertIsNone(lock_sweep.release_unauthorized_locks(db))
        finally:
            db.close()


class TestSettings(unittest.TestCase):
    def test_fallback_settings_and_messages(self) -> None:
        cfg = airtable_reader.build_config(SimpleNamespace(**SETTINGS))
        users = cfg.table("users")
        self.assertEqual(users.optional["start_date"], "StartDate")  # the sync's own name wins
        self.assertEqual(users.optional_settings["start_date"], "AIRTABLE_SYNC_USERS_START_DATE_FIELD")
        self.assertEqual(users.optional["manager_tech_stack_selections"], "Manager Tech Stack Selections")
        self.assertEqual(users.optional_settings["manager_tech_stack_selections"], "USERS_TECH_STACK_SELECTIONS_FIELD")
        self.assertNotIn("office_location", users.optional)
        self.assertIn("AIRTABLE_SYNC_USERS_OFFICE_LOCATION_FIELD", users.unconfigured)


if __name__ == "__main__":
    unittest.main()
