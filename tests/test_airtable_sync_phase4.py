"""Airtable sync, phase 4 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §3.1
K2/M7, §3.4 A5, §3.5, §8 step 5; v1 plan §5.3.1–§5.3.4, §5.3.8, §5.4): the
Users step without HR fields — K2 matching, M7, the person-level items,
creation, `user_deleted` — and the items API (list + resolve), including
the scope choices phase 3 raises.

SQLite in memory with every session closed; FakeSource and fixture copied
from phases 2–3, not imported. Airtable is never called. Runs go through the
real runner with an inline executor; resolutions through the real router.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase4-tests-only")
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
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.airtable_sync import runner as runner_module  # noqa: E402
from app.services.airtable_sync import users_step  # noqa: E402
from app.services.airtable_sync.runner import SyncRunner, get_sync_runner  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"
MEMBER_EMAIL = "member@renphil.org"
PAT = "patFAKE.0123456789abcdefSECRET"
BASE = "appHUB"
T_CLUSTERS, T_PROGRAMS, T_USERS, T_ROLES = "tblCLUSTER", "tblPUBLIC", "tblUSERS", "tblROLES"

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
    "AIRTABLE_SYNC_PROGRAM_ROLES_TABLE": T_ROLES,
    "AIRTABLE_SYNC_PROGRAM_ROLES_EMPLOYEE_RECORD_ID_FIELD": "Employee Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_PROGRAM_RECORD_ID_FIELD": "Original Program Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD": "Designation",
}

SPACE, CLIMATE = "recCLSPACE", "recCLCLIM"
P1, P2, P_MISSING = "recP1", "recP2", "recMISSING"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def person(n: int, name: str | None, work: str | None = None, alias: str | None = None) -> dict:
    fields = {"Record ID": f"recU{n}"}
    if name is not None:
        fields["Name"] = name
    if work is not None:
        fields["Work Email"] = work
    if alias is not None:
        fields["Alias Email"] = alias
    return {"id": f"recAPIu{n}", "fields": fields}


def role_row(n: int, employee: str, program: str, designation: str = "Member") -> dict:
    return {
        "id": f"recAPIr{n}",
        "fields": {"Employee Record ID": employee, "Original Program Record ID": program, "Designation": designation},
    }


def airtable_tables() -> dict:
    """Users shaped to hit every K2 branch against the seeded Hub rows."""
    users = [
        person(1, "Ada", "ada@renphil.org"),  # work email → Ada
        person(2, "Bo", "bo@renphil.org", "bo.alias@renphil.org"),  # alias → "Bo Hub"
        person(3, "Cy", "cy@renphil.org"),  # unique name, Hub row has no email
        person(4, "Di", "di@renphil.org"),  # name, Hub row has another email
        person(5, "Ed", "ed@renphil.org"),  # email of a soft-deleted row (M7)
        person(6, "Fay", "fay@renphil.org"),  # new; only role on a missing program
        person(7, "Gus"),  # no Work Email
        person(8, "Hal", "hal@renphil.org"),  # duplicate Work Email (D-3)
        person(9, "Hal Two", " HAL@renphil.org "),
        person(10, "Ivy", "ivy@renphil.org"),  # no Program Roles (A5)
        person(11, "Jo", "jo@renphil.org"),  # only a Don't-sync row (A5)
        person(12, "Kim", "kim@renphil.org"),  # same name as a deleted row
    ]
    roles = [role_row(n, f"recU{n}", P1) for n in (1, 2, 3, 4, 5, 7, 8, 9, 12)]
    roles += [role_row(6, "recU6", P_MISSING), role_row(11, "recU11", P1, "Advisor")]
    return {
        T_CLUSTERS: {
            "fields": {"Name", "Record Id"},
            "records": [
                {"id": "recAPIc1", "fields": {"Name": "Space", "Record Id": SPACE}},
                {"id": "recAPIc2", "fields": {"Name": "Climate", "Record Id": CLIMATE}},
            ],
        },
        T_PROGRAMS: {
            "fields": {"Name", "Record ID", "Cluster Record Id", "Program Cluster"},
            "records": [
                {"id": "recAPIp1", "fields": {"Name": "Space Science", "Record ID": P1, "Cluster Record Id": SPACE, "Program Cluster": ["recAPIc1"]}},
                {"id": "recAPIp2", "fields": {"Name": "Lone Program", "Record ID": P2}},
            ],
        },
        T_USERS: {"fields": {"Name", "Record ID", "Work Email", "Alias Email"}, "records": users},
        T_ROLES: {"fields": {"Employee Record ID", "Original Program Record ID", "Designation"}, "records": roles},
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
        self.admin_role = RoleV2(key="hub_admin", name="Admin", is_public=False, is_system=False, created_at=now)
        self.user_role = RoleV2(key="user", name="User", is_public=True, is_system=False, created_at=now)
        self.member_role = RoleV2(key="member", name="Member", is_public=False, is_system=False, created_at=now)
        db.add_all([self.admin_role, self.user_role, self.member_role])
        db.flush()

        def scope(key, name, **kw):
            row = ScopeV2(key=key, name=name, is_universal=kw.pop("universal", False), is_public=kw.pop("public", False), is_system=kw.pop("system", False), created_at=now, **kw)
            db.add(row)
            db.flush()
            return row.id

        self.all_scopes = scope("all_scopes", "All Scopes", universal=True, system=True)
        self.hub_scope = scope("hub", "Hub", public=True)
        self.studio = scope("studio", "Studio")

        def user(name, work=None, alias=None, **kw):
            row = UserV2(name=name, work_email=work, alias_email=alias, has_signed_in=kw.pop("signed_in", False), created_at=now, updated_at=now, **kw)
            db.add(row)
            db.flush()
            return row.id

        self.admin = user("Admin", ADMIN_EMAIL, signed_in=True)
        self.member = user("Member", MEMBER_EMAIL, signed_in=True)
        self.ada = user("Ada", "ada@renphil.org", signed_in=True)
        self.bo = user("Bo Hub", "bo.old@renphil.org", "bo.alias@renphil.org")
        self.cy = user("Cy")
        self.di = user("Di", "di.old@renphil.org")
        self.ed = user("Ed", "ed@renphil.org", is_deleted=True, deleted_at=now, deleted_by=ADMIN_EMAIL)
        self.kim_deleted = user("Kim", is_deleted=True, deleted_at=now, deleted_by=ADMIN_EMAIL)
        db.add(RoleAssignmentV2(user_id=self.admin, role_id=self.admin_role.id, scope_id=self.all_scopes, created_at=now))
        db.add(RoleAssignmentV2(user_id=self.member, role_id=self.member_role.id, scope_id=self.studio, created_at=now))
        db.add(AirtableDesignationMappingV2(designation="Member", action="role", role_id=self.member_role.id, updated_at=now))
        db.add(AirtableDesignationMappingV2(designation="Advisor", action="dont_sync", updated_at=now))
        db.commit()

    # ── running ──────────────────────────────────────────────────────────

    def run_sync(self, steps=("users",), preview=False) -> AirtableSyncRunV2:
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

    def auth(self, email: str) -> dict:
        token = jwt.encode(
            {"sub": email, "name": email.split("@")[0], "roles": [], "exp": _now() + timedelta(minutes=30)},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    def resolve(self, item_id: int, choice: str, data: dict | None = None, email: str = ADMIN_EMAIL):
        body = {"choice": choice}
        if data is not None:
            body["data"] = data
        return self.client.post(f"/v2/airtable-sync/items/{item_id}/resolve", json=body, headers=self.auth(email))

    def resolved_ok(self, item_id: int, choice: str, data: dict | None = None) -> dict:
        res = self.resolve(item_id, choice, data)
        self.assertEqual(res.status_code, 200, res.text)
        self.db.expire_all()
        return res.json()["data"]

    def answer_fields(self, rid: str, choice: str = "keep_current") -> dict:
        """Phase 6 (M2): answer every open first-link field conflict of one
        person, through bulk resolve."""
        ids = [i.id for i in self.items("field_conflict", rid=rid)]
        self.assertTrue(ids, rid)
        res = self.client.post(
            "/v2/airtable-sync/items/bulk-resolve", json={"item_ids": ids, "choice": choice}, headers=self.auth(ADMIN_EMAIL)
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()["data"]["failed"], res.text)
        self.db.expire_all()
        return res.json()["data"]

    # ── reading ──────────────────────────────────────────────────────────

    def user_by_rid(self, rid: str) -> UserV2 | None:
        self.db.expire_all()
        return self.db.query(UserV2).filter(UserV2.airtable_record_id == rid).one_or_none()

    def get_user(self, user_id: int) -> UserV2:
        self.db.expire_all()
        return self.db.get(UserV2, user_id)

    def record(self, rid: str, entity: str = "user") -> AirtableSyncRecordV2 | None:
        self.db.expire_all()
        return self.db.query(AirtableSyncRecordV2).filter_by(entity=entity, airtable_record_id=rid).one_or_none()

    def logs(self, run: AirtableSyncRunV2 | None = None, action: str | None = None) -> list[AirtableSyncLogV2]:
        self.db.expire_all()
        q = self.db.query(AirtableSyncLogV2)
        q = q.filter(AirtableSyncLogV2.run_id == run.id) if run is not None else q.filter(AirtableSyncLogV2.run_id.is_(None))
        if action:
            q = q.filter(AirtableSyncLogV2.action == action)
        return q.order_by(AirtableSyncLogV2.id).all()

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

    def item(self, kind: str, rid: str) -> AirtableSyncItemV2:
        found = self.items(kind, rid=rid)
        self.assertEqual(len(found), 1, [(i.kind, i.airtable_record_id) for i in self.items()])
        return found[0]

    def user_count(self) -> int:
        self.db.expire_all()
        return self.db.query(func.count(UserV2.id)).scalar()

    def assignments(self, user_id: int) -> set[tuple[int, int, str]]:
        self.db.expire_all()
        return {
            (a.role_id, a.scope_id, a.source)
            for a in self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == user_id)
        }

    def hub_counts(self) -> dict[str, int]:
        self.db.expire_all()
        return {
            t.name: self.db.query(func.count()).select_from(t).scalar()
            for t in BaseV2.metadata.sorted_tables
            if t.name not in ("airtable_sync_runs", "airtable_sync_log")
        }

    # ── editing Airtable / the Hub ───────────────────────────────────────

    def at_user(self, n: int) -> dict:
        for r in self.tables[T_USERS]["records"]:
            if r["fields"].get("Record ID") == f"recU{n}":
                return r["fields"]
        raise KeyError(n)

    def drop_user(self, n: int) -> None:
        self.tables[T_USERS]["records"] = [r for r in self.tables[T_USERS]["records"] if r["fields"].get("Record ID") != f"recU{n}"]

    def only_users(self, *ns: int) -> None:
        keep = {f"recU{n}" for n in ns}
        self.tables[T_USERS]["records"] = [r for r in self.tables[T_USERS]["records"] if r["fields"]["Record ID"] in keep]

    def add_user(self, name: str, work=None, alias=None, rid=None, **kw) -> int:
        now = _now()
        row = UserV2(name=name, work_email=work, alias_email=alias, airtable_record_id=rid, has_signed_in=False, created_at=now, updated_at=now, **kw)
        self.db.add(row)
        self.db.commit()
        return row.id


# ---------------------------------------------------------
# K2 matching, M7, creation
# ---------------------------------------------------------


class TestMatching(SyncFixture):
    def test_full_users_run(self) -> None:
        before = self.user_count()
        run = self.run_sync()
        s = run.summary["steps"]["users"]
        self.assertEqual(s["records"], 12)
        self.assertEqual(s["linked"], 1)  # Ada (phase 6: Bo and Cy wait on M2's first-link conflicts)
        self.assertEqual(s["created"], 2)  # Fay, Kim
        self.assertEqual(s["skipped_deleted_in_hub"], 1)  # Ed
        self.assertEqual(s["held"], 8)  # Bo, Cy, Di, Gus, Hal ×2, Ivy, Jo
        self.assertEqual(self.user_count(), before + 2)
        open_kinds = sorted((i.kind, i.airtable_record_id) for i in self.items())
        self.assertEqual(
            open_kinds,
            [
                ("duplicate_work_email", "recU8"),
                ("duplicate_work_email", "recU9"),
                ("field_conflict", "recU2"),  # name, work_email
                ("field_conflict", "recU2"),
                ("field_conflict", "recU3"),  # work_email
                ("no_program_roles", "recU10"),
                ("no_program_roles", "recU11"),
                ("no_work_email", "recU7"),
                ("possible_match", "recU4"),
            ],
        )

    def test_link_by_work_email_alias_and_name(self) -> None:
        run = self.run_sync()
        self.assertEqual(self.user_by_rid("recU1").id, self.ada)
        # Phase 6 (M2): Bo (by alias) and Cy (by name) are matched, and held
        # by their first-link field conflicts, which name the matched row.
        conflicts = {(i.airtable_record_id, i.field): i.payload["hub_user"]["user_id"] for i in self.items("field_conflict")}
        self.assertEqual(conflicts, {("recU2", "name"): self.bo, ("recU2", "work_email"): self.bo, ("recU3", "work_email"): self.cy})
        self.assertIsNone(self.user_by_rid("recU2"))
        self.assertEqual({l.airtable_record_id: l.field for l in self.logs(run, "linked")}, {"recU1": "work_email"})
        self.answer_fields("recU2")
        self.answer_fields("recU3")
        self.assertEqual(self.user_by_rid("recU2").id, self.bo)
        self.assertEqual(self.user_by_rid("recU3").id, self.cy)
        via = {l.airtable_record_id: l.field for l in self.logs(action="linked")}
        self.assertEqual(via, {"recU2": "alias_email", "recU3": "name"})
        # Keep current: the Hub row's name and emails are left alone.
        bo = self.get_user(self.bo)
        self.assertEqual((bo.name, bo.work_email), ("Bo Hub", "bo.old@renphil.org"))
        self.assertIsNone(self.get_user(self.cy).work_email)

    def test_record_base_holds_only_agreed_identity_fields(self) -> None:
        self.run_sync()
        ada = self.record("recU1")
        self.assertEqual(ada.hub_id, self.ada)
        self.assertEqual(ada.base, {"name": "Ada", "work_email": "ada@renphil.org", "alias_email": None})
        self.assertEqual(ada.latest_airtable["work_email"], "ada@renphil.org")
        self.assertIsNone(ada.managed_assignment_id)
        # Phase 6: Bo and Cy are held (M2), so nothing is agreed yet...
        self.assertIsNone(self.record("recU2").base)
        self.assertIsNone(self.record("recU3").base)
        # ...until their conflicts are answered: Keep current agrees on
        # Airtable's values (M3), the equal ones were agreed already.
        self.answer_fields("recU2")
        self.answer_fields("recU3")
        self.assertEqual(self.record("recU2").base, {"name": "Bo", "work_email": "bo@renphil.org", "alias_email": "bo.alias@renphil.org"})
        self.assertEqual(self.record("recU3").base, {"name": "Cy", "work_email": "cy@renphil.org", "alias_email": None})
        # Phase 6 moves the base: Airtable renames Ada, the Hub still has the
        # base value → updated.
        self.at_user(1)["Name"] = "Ada L."
        self.run_sync()
        self.assertEqual(self.record("recU1").base["name"], "Ada L.")
        self.assertEqual(self.get_user(self.ada).name, "Ada L.")

    def test_create_new_user(self) -> None:
        run = self.run_sync()
        fay = self.user_by_rid("recU6")
        self.assertEqual((fay.name, fay.work_email, fay.alias_email), ("Fay", "fay@renphil.org", None))
        self.assertFalse(fay.has_signed_in)
        self.assertFalse(fay.is_deleted)
        self.assertEqual(self.assignments(fay.id), set())  # add_default_assignment NOT called
        created = self.logs(run, "user_created")
        self.assertEqual({l.airtable_record_id for l in created}, {"recU6", "recU12"})
        self.assertEqual(self.record("recU6").base, {"name": "Fay", "work_email": "fay@renphil.org", "alias_email": None})
        # The Hal Two record's padded, upper-case email was normalized.
        self.assertEqual(self.item("duplicate_work_email", "recU9").payload["work_email"], "hal@renphil.org")

    def test_second_run_changes_nothing(self) -> None:
        self.run_sync()
        users, items = self.user_count(), [(i.id, i.status) for i in self.items(status=None)]
        run = self.run_sync()
        self.assertEqual(self.user_count(), users)
        self.assertEqual([(i.id, i.status) for i in self.items(status=None)], items)
        s = run.summary["steps"]["users"]
        self.assertEqual(s["unchanged"], 3)  # Ada, Fay, Kim (Bo and Cy held: M2)
        self.assertNotIn("linked", s)
        self.assertNotIn("created", s)
        self.assertEqual(s["items_refreshed"], 9)  # + 3 field conflicts

    def test_m7_soft_deleted_by_email_and_record_id(self) -> None:
        run = self.run_sync()
        skipped = self.logs(run, "skipped_deleted_in_hub")
        self.assertEqual([(l.airtable_record_id, l.hub_id, l.field) for l in skipped], [("recU5", self.ed, "work_email")])
        self.assertEqual(self.items(rid="recU5"), [])
        self.assertIsNone(self.record("recU5"))
        self.assertIsNone(self.get_user(self.ed).airtable_record_id)
        # By Record ID: the deleted row holds it.
        self.add_user("Zed", "zed.hub@renphil.org", rid="recU13", is_deleted=True)
        self.tables[T_USERS]["records"].append(person(13, "Zed", "zed@renphil.org"))
        before = self.user_count()
        run = self.run_sync()
        self.assertIn(("recU13", "record_id"), {(l.airtable_record_id, l.field) for l in self.logs(run, "skipped_deleted_in_hub")})
        self.assertEqual(self.user_count(), before)
        self.assertEqual(self.items(rid="recU13"), [])

    def test_name_matching_ignores_deleted_rows(self) -> None:
        self.run_sync()
        kim = self.user_by_rid("recU12")
        self.assertNotEqual(kim.id, self.kim_deleted)
        self.assertIsNone(self.get_user(self.kim_deleted).airtable_record_id)
        self.assertFalse(self.get_user(self.kim_deleted).has_signed_in)

    def test_possible_match(self) -> None:
        run = self.run_sync()
        item = self.item("possible_match", "recU4")
        self.assertEqual(item.payload["candidate"]["user_id"], self.di)
        self.assertEqual(item.payload["choices"], ["link", "create_new", "skip"])
        self.assertEqual(item.payload["airtable"]["work_email"], "di@renphil.org")
        self.assertIsNone(item.hub_id)
        self.assertIsNone(self.get_user(self.di).airtable_record_id)
        held = [l for l in self.logs(run, "held") if l.airtable_record_id == "recU4"]
        self.assertEqual(len(held), 1)
        self.assertIn("possible_match", held[0].new_value)
        self.assertIsNone(self.record("recU4").hub_id)  # held, but latest_airtable kept (C4)
        self.assertEqual(self.record("recU4").latest_airtable["name"], "Di")

    def test_ambiguous_by_email_offers_no_create(self) -> None:
        a = self.add_user("Amb A", "amb@renphil.org")
        b = self.add_user("Amb B", "amb2@renphil.org")
        self.only_users(1)
        self.tables[T_USERS]["records"].append(person(20, "Amb", "amb@renphil.org", "amb2@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(20, "recU20", P1))
        self.run_sync()
        item = self.item("ambiguous_match", "recU20")
        self.assertEqual(item.payload["reason"], "email")
        self.assertEqual(item.payload["choices"], ["link", "skip"])
        self.assertEqual({c["user_id"] for c in item.payload["candidates"]}, {a, b})
        self.assertIsNone(self.user_by_rid("recU20"))

    def test_ambiguous_by_name_offers_create(self) -> None:
        self.add_user("Pat", "pat.one@renphil.org")
        self.add_user(" pat ", "pat.two@renphil.org")
        self.only_users(1)
        self.tables[T_USERS]["records"].append(person(21, "Pat", "pat@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(21, "recU21", P1))
        self.run_sync()
        item = self.item("ambiguous_match", "recU21")
        self.assertEqual(item.payload["reason"], "name")
        self.assertEqual(item.payload["choices"], ["link", "create_new", "skip"])
        self.assertIsNone(self.user_by_rid("recU21"))

    def test_email_on_a_row_linked_elsewhere_is_email_taken(self) -> None:
        other = self.add_user("Old Zoe", "zoe@renphil.org", rid="recGONE")
        self.only_users(1)
        self.tables[T_USERS]["records"].append(person(22, "Zoe", "zoe@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(22, "recU22", P1))
        before = self.user_count()
        self.run_sync()
        item = self.item("email_taken", "recU22")
        self.assertEqual(item.payload["taken_by"]["user_id"], other)
        self.assertEqual(self.user_count(), before)
        self.assertEqual(self.get_user(other).airtable_record_id, "recGONE")
        # The old record's row is reported deleted from Airtable meanwhile.
        self.assertEqual(self.item("user_deleted", "recGONE").hub_id, other)

    def test_stored_record_id_wins_over_email(self) -> None:
        linked = self.add_user("Someone", "someone@renphil.org", rid="recU1")
        self.run_sync()
        self.assertEqual(self.user_by_rid("recU1").id, linked)
        self.assertIsNone(self.get_user(self.ada).airtable_record_id)

    def test_a_data_refusal_skips_one_person(self) -> None:
        original = users_step.UserWriter.create

        def create(writer, rid, fields):
            if rid == "recU6":
                raise IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint"))
            return original(writer, rid, fields)

        with mock.patch.object(users_step.UserWriter, "create", create):
            run = self.run_sync()
        errors = self.logs(run, "user_error")
        self.assertEqual([e.airtable_record_id for e in errors], ["recU6"])
        self.assertIsNone(self.user_by_rid("recU6"))
        self.assertIsNotNone(self.user_by_rid("recU12"))  # the others went on
        self.assertEqual(run.summary["steps"]["users"]["errors"], 1)


# ---------------------------------------------------------
# Validation, A5, holding, auto-close
# ---------------------------------------------------------


class TestPersonItems(SyncFixture):
    def test_no_work_email(self) -> None:
        self.run_sync()
        item = self.item("no_work_email", "recU7")
        self.assertNotIn("choices", item.payload)
        self.assertIsNone(self.record("recU7").hub_id)
        self.assertEqual(self.user_count(), 10)  # 8 seeded + Fay + Kim; Gus not created

    def test_duplicate_work_email_holds_both(self) -> None:
        self.run_sync()
        hal, two = self.item("duplicate_work_email", "recU8"), self.item("duplicate_work_email", "recU9")
        self.assertEqual(hal.payload["other_records"], ["recU9"])
        self.assertEqual(two.payload["other_records"], ["recU8"])
        self.assertIsNone(self.user_by_rid("recU8"))
        self.assertIsNone(self.user_by_rid("recU9"))

    def test_items_auto_close_once_fixed(self) -> None:
        self.run_sync()
        self.at_user(7)["Work Email"] = "gus@renphil.org"
        self.at_user(9)["Work Email"] = "hal.two@renphil.org"
        run = self.run_sync()
        self.assertEqual(self.items("no_work_email"), [])
        self.assertEqual(self.items("duplicate_work_email"), [])
        closed = {i.airtable_record_id: i.resolution for i in self.items("duplicate_work_email", status="resolved")}
        self.assertEqual(closed, {"recU8": "auto_closed", "recU9": "auto_closed"})
        self.assertEqual(len(self.logs(run, "item_auto_closed")), 3)
        self.assertIsNotNone(self.user_by_rid("recU7"))
        self.assertIsNotNone(self.user_by_rid("recU9"))

    def test_all_items_raised_together_and_carried(self) -> None:
        # Di: possible_match AND no_program_roles, raised in one run.
        self.tables[T_ROLES]["records"] = [r for r in self.tables[T_ROLES]["records"] if r["fields"]["Employee Record ID"] != "recU4"]
        self.run_sync()
        self.assertEqual({i.kind for i in self.items(rid="recU4")}, {"possible_match", "no_program_roles"})
        self.run_sync()
        self.assertEqual({i.kind for i in self.items(rid="recU4")}, {"possible_match", "no_program_roles"})

    def test_supersede_on_an_airtable_change(self) -> None:
        self.run_sync()
        first = self.item("possible_match", "recU4")
        self.at_user(4)["Work Email"] = "di.new@renphil.org"
        self.run_sync()
        second = self.item("possible_match", "recU4")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.items("possible_match", status="superseded")[0].id, first.id)

    def test_no_program_roles_counts_missing_programs_not_dont_sync(self) -> None:
        self.run_sync()
        ivy = self.item("no_program_roles", "recU10")
        self.assertEqual(ivy.payload["choices"], ["assign_default", "skip"])
        self.assertEqual(ivy.payload["dont_sync_rows"], [])
        jo = self.item("no_program_roles", "recU11")
        self.assertEqual(jo.payload["dont_sync_rows"], [[P1, "Advisor"]])
        self.assertEqual(self.items(rid="recU6"), [])  # Fay: a missing-program row counts
        self.assertIsNone(self.user_by_rid("recU10"))

    def test_no_program_roles_for_a_linked_person(self) -> None:
        self.tables[T_ROLES]["records"] = [r for r in self.tables[T_ROLES]["records"] if r["fields"]["Employee Record ID"] != "recU1"]
        self.run_sync()
        item = self.item("no_program_roles", "recU1")
        self.assertEqual(item.hub_id, self.ada)
        self.assertEqual(item.payload["hub_user"]["user_id"], self.ada)
        # Matched by email but held: the link waits.
        self.assertIsNone(self.get_user(self.ada).airtable_record_id)

    def test_m7_closes_a_person_s_items(self) -> None:
        self.run_sync()
        self.assertEqual(len(self.items(rid="recU4")), 1)
        di = self.get_user(self.di)
        di.is_deleted = True
        self.db.commit()
        self.at_user(4)["Work Email"] = "di.old@renphil.org"
        self.run_sync()
        self.assertEqual(self.items(rid="recU4"), [])


# ---------------------------------------------------------
# Deleted from Airtable (M6)
# ---------------------------------------------------------


class TestUserDeleted(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_sync()
        now = _now()
        self.db.add(RoleAssignmentV2(user_id=self.ada, role_id=self.member_role.id, scope_id=self.studio, source="airtable_sync", created_at=now))
        self.db.add(RoleAssignmentV2(user_id=self.ada, role_id=self.user_role.id, scope_id=self.hub_scope, source="default", created_at=now))
        self.db.commit()
        self.drop_user(1)

    def test_raised_with_the_synced_access(self) -> None:
        self.run_sync()
        item = self.item("user_deleted", "recU1")
        self.assertEqual(item.hub_id, self.ada)
        self.assertEqual(item.payload["choices"], ["remove_synced_access", "keep", "delete_user"])
        self.assertEqual([(a["role"], a["scope"]) for a in item.payload["synced_assignments"]], [("Member", "Studio")])
        self.assertEqual(self.get_user(self.ada).airtable_record_id, "recU1")  # nothing changed

    def test_closes_when_the_person_comes_back(self) -> None:
        self.run_sync()
        self.tables[T_USERS]["records"].append(person(1, "Ada", "ada@renphil.org"))
        self.run_sync()
        self.assertEqual(self.items("user_deleted"), [])
        self.assertEqual(self.items("user_deleted", status="resolved")[0].resolution, "auto_closed")

    def test_soft_deleted_rows_are_never_reported(self) -> None:
        ada = self.get_user(self.ada)
        ada.is_deleted = True
        self.db.commit()
        self.run_sync()
        self.assertEqual(self.items("user_deleted"), [])

    def test_remove_synced_access(self) -> None:
        self.run_sync()
        out = self.resolved_ok(self.item("user_deleted", "recU1").id, "remove_synced_access")
        self.assertTrue(out["outcome"]["applied"])
        self.assertEqual(self.assignments(self.ada), {(self.user_role.id, self.hub_scope, "default")})
        self.assertIsNone(self.get_user(self.ada).airtable_record_id)
        actions = [l.action for l in self.logs()]
        self.assertEqual(actions, ["assignment_removed", "unlinked", "item_resolved"])
        self.run_sync()
        self.assertEqual(self.items("user_deleted"), [])

    def test_keep(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("user_deleted", "recU1").id, "keep")
        self.assertIsNone(self.get_user(self.ada).airtable_record_id)
        self.assertEqual(len(self.assignments(self.ada)), 2)
        self.run_sync()
        self.assertEqual(self.items("user_deleted"), [])

    def test_delete_user_and_restore_asks_again(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("user_deleted", "recU1").id, "delete_user")
        ada = self.get_user(self.ada)
        self.assertTrue(ada.is_deleted)
        self.assertEqual(ada.deleted_by, ADMIN_EMAIL)
        self.assertEqual(ada.airtable_record_id, "recU1")
        self.assertEqual(len(self.assignments(self.ada)), 2)  # a soft delete keeps everything
        self.run_sync()
        self.assertEqual(self.items("user_deleted"), [])
        ada.is_deleted = False
        self.db.commit()
        self.run_sync()
        self.assertEqual(len(self.items("user_deleted")), 1)  # not a remembered answer

    def test_delete_user_guards(self) -> None:
        self.run_sync()
        admin = self.get_user(self.admin)
        admin.airtable_record_id = "recADMIN"
        self.db.commit()
        self.run_sync()
        res = self.resolve(self.item("user_deleted", "recADMIN").id, "delete_user")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["code"], "cannot_delete_self")
        self.assertFalse(self.get_user(self.admin).is_deleted)
        self.assertEqual(self.item("user_deleted", "recADMIN").status, "open")

    def test_keep_is_not_remembered_across_a_return(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("user_deleted", "recU1").id, "keep")
        self.tables[T_USERS]["records"].append(person(1, "Ada", "ada@renphil.org"))
        self.run_sync()  # re-links Ada by email
        self.assertEqual(self.user_by_rid("recU1").id, self.ada)
        self.drop_user(1)
        self.run_sync()
        self.assertEqual(len(self.items("user_deleted")), 1)


# ---------------------------------------------------------
# Person resolutions
# ---------------------------------------------------------


class TestUserResolutions(SyncFixture):
    def test_possible_match_link(self) -> None:
        self.run_sync()
        item = self.item("possible_match", "recU4")
        out = self.resolved_ok(item.id, "link")
        # Phase 6 (M2): the Link matches Di's row, whose work email differs:
        # that first-link conflict is opened at once, and the link waits.
        self.assertEqual(out["outcome"], {"applied": False, "waiting_on": ["field_conflict"]})
        self.assertEqual([(i["kind"], i["field"]) for i in out["open_items"]], [("field_conflict", "work_email")])
        self.assertIsNone(self.user_by_rid("recU4"))
        conflict = self.item("field_conflict", "recU4")
        out = self.resolved_ok(conflict.id, "keep_current")
        self.assertEqual(out["outcome"], {"applied": True, "user_id": self.di})
        self.assertEqual(self.user_by_rid("recU4").id, self.di)
        resolved = self.db.get(AirtableSyncItemV2, item.id)
        self.assertEqual((resolved.status, resolved.resolution, resolved.resolved_by_email), ("resolved", "link", ADMIN_EMAIL))
        self.assertEqual(resolved.resolution_data, {"user_id": self.di})
        logs = self.logs()
        self.assertEqual([l.action for l in logs], ["item_resolved", "linked", "hub_differs", "item_resolved"])
        self.assertTrue(all(l.run_id is None and l.actor_email == ADMIN_EMAIL for l in logs))
        self.assertEqual([l.resolution_item_id for l in logs], [item.id, conflict.id, conflict.id, conflict.id])
        self.assertEqual(self.record("recU4").hub_id, self.di)
        self.assertEqual(self.record("recU4").base, {"name": "Di", "work_email": "di@renphil.org", "alias_email": None})
        self.assertEqual(self.get_user(self.di).work_email, "di.old@renphil.org")  # kept
        run = self.run_sync()  # matched by Record ID now
        self.assertEqual(self.items(rid="recU4"), [])
        self.assertNotIn("recU4", {l.airtable_record_id for l in self.logs(run, "linked")})

    def test_possible_match_create_new(self) -> None:
        self.run_sync()
        before = self.user_count()
        out = self.resolved_ok(self.item("possible_match", "recU4").id, "create_new")
        new = self.user_by_rid("recU4")
        self.assertEqual(out["outcome"]["user_id"], new.id)
        self.assertNotEqual(new.id, self.di)
        self.assertEqual((new.name, new.work_email), ("Di", "di@renphil.org"))
        self.assertEqual(self.user_count(), before + 1)

    def test_skip_is_remembered_and_holds(self) -> None:
        self.run_sync()
        item = self.item("possible_match", "recU4")
        self.resolved_ok(item.id, "skip")
        run = self.run_sync()
        self.assertEqual(self.items(rid="recU4"), [])
        self.assertIsNone(self.user_by_rid("recU4"))
        self.assertIn("recU4", {l.airtable_record_id for l in self.logs(run, "held")})
        self.assertEqual(self.db.get(AirtableSyncItemV2, item.id).last_run_id, run.id)
        # Airtable changes the inputs → asked again.
        self.at_user(4)["Work Email"] = "di.new@renphil.org"
        self.run_sync()
        self.assertEqual(len(self.items("possible_match", rid="recU4")), 1)

    def test_answer_waits_for_the_last_open_item(self) -> None:
        self.tables[T_ROLES]["records"] = [r for r in self.tables[T_ROLES]["records"] if r["fields"]["Employee Record ID"] != "recU4"]
        self.run_sync()
        out = self.resolved_ok(self.item("possible_match", "recU4").id, "link")
        self.assertEqual(out["outcome"], {"applied": False, "waiting_on": ["no_program_roles"]})
        self.assertIsNone(self.get_user(self.di).airtable_record_id)
        self.assertEqual([i["kind"] for i in out["open_items"]], ["no_program_roles"])
        out = self.resolved_ok(self.item("no_program_roles", "recU4").id, "assign_default")
        # Phase 6 (M2): now matched, Di's work email is a first-link conflict.
        self.assertEqual(out["outcome"], {"applied": False, "waiting_on": ["field_conflict"]})
        out = self.resolved_ok(self.item("field_conflict", "recU4").id, "accept_airtable")
        self.assertTrue(out["outcome"]["applied"])
        self.assertEqual(self.user_by_rid("recU4").id, self.di)  # the earlier Link applied too
        self.assertEqual(self.get_user(self.di).work_email, "di@renphil.org")  # Accept Airtable
        self.assertEqual(self.assignments(self.di), {(self.user_role.id, self.hub_scope, "airtable_sync")})

    def test_remembered_link_applies_in_the_next_run(self) -> None:
        self.tables[T_ROLES]["records"] = [r for r in self.tables[T_ROLES]["records"] if r["fields"]["Employee Record ID"] != "recU4"]
        self.run_sync()
        self.resolved_ok(self.item("possible_match", "recU4").id, "link")
        self.tables[T_ROLES]["records"].append(role_row(4, "recU4", P1))  # no_program_roles goes away
        self.run_sync()
        # Phase 6 (M2): the remembered Link matches Di; her work email is a
        # first-link conflict, answered, and the next run applies both.
        self.assertIsNone(self.user_by_rid("recU4"))
        self.answer_fields("recU4", "keep_current")
        self.assertEqual(self.user_by_rid("recU4").id, self.di)
        self.run_sync()
        self.assertEqual(self.user_by_rid("recU4").id, self.di)
        self.assertEqual(self.items(rid="recU4"), [])

    def test_ambiguous_email_link_and_refusals(self) -> None:
        a = self.add_user("Amb A", "amb@renphil.org")
        b = self.add_user("Amb B", "amb2@renphil.org")
        self.tables[T_USERS]["records"].append(person(20, "Amb", "amb@renphil.org", "amb2@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(20, "recU20", P1))
        self.run_sync()
        item = self.item("ambiguous_match", "recU20")
        res = self.resolve(item.id, "create_new")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "email_taken"))
        res = self.resolve(item.id, "link", {"user_id": self.ada})
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "invalid_data"))
        res = self.resolve(item.id, "link")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.items(rid="recU20")[0].status, "open")  # nothing kept from refusals
        self.assertEqual(self.logs(), [])
        out = self.resolved_ok(item.id, "link", {"user_id": b})
        # Phase 6: Amb B's row would take Airtable's work email, which Amb A
        # holds → `email_taken` with a field; the name and alias are
        # first-link conflicts. Nothing is linked.
        self.assertEqual(out["outcome"], {"applied": False, "waiting_on": ["email_taken", "field_conflict"]})
        opened = {(i["kind"], i["field"]) for i in out["open_items"]}
        self.assertEqual(opened, {("email_taken", "work_email"), ("field_conflict", "name"), ("field_conflict", "alias_email")})
        taken = self.item("email_taken", "recU20")
        self.assertEqual((taken.field, taken.payload["taken_by"]["user_id"]), ("work_email", a))
        self.assertIsNone(self.user_by_rid("recU20"))
        self.assertIsNone(self.get_user(a).airtable_record_id)

    def test_ambiguous_name_create_new(self) -> None:
        self.add_user("Pat", "pat.one@renphil.org")
        self.add_user("PAT", "pat.two@renphil.org")
        self.tables[T_USERS]["records"].append(person(21, "Pat", "pat@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(21, "recU21", P1))
        self.run_sync()
        self.resolved_ok(self.item("ambiguous_match", "recU21").id, "create_new")
        self.assertEqual(self.user_by_rid("recU21").work_email, "pat@renphil.org")

    def test_link_target_gone_is_refused(self) -> None:
        self.run_sync()
        di = self.get_user(self.di)
        di.is_deleted = True
        self.db.commit()
        res = self.resolve(self.item("possible_match", "recU4").id, "link")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "candidate_unavailable"))

    def test_assign_default_creates_the_person_and_the_pair(self) -> None:
        self.run_sync()
        item = self.item("no_program_roles", "recU10")
        out = self.resolved_ok(item.id, "assign_default")
        ivy = self.user_by_rid("recU10")
        self.assertEqual(out["outcome"]["user_id"], ivy.id)
        self.assertEqual(self.assignments(ivy.id), {(self.user_role.id, self.hub_scope, "airtable_sync")})
        managed = self.record("recU10").managed_assignment_id
        self.assertEqual(self.db.get(RoleAssignmentV2, managed).user_id, ivy.id)
        self.assertEqual([l.action for l in self.logs()], ["user_created", "assignment_created", "item_resolved"])
        run = self.run_sync()  # a sync-given default: not raised again
        self.assertEqual(self.items(rid="recU10"), [])
        self.assertNotIn("items_remembered", run.summary["steps"]["users"])  # not even checked
        self.assertEqual(len(self.assignments(ivy.id)), 1)  # never stacked

    def test_no_program_roles_skip_keeps_the_person_out(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("no_program_roles", "recU10").id, "skip")
        self.assertIsNone(self.user_by_rid("recU10"))
        run = self.run_sync()
        self.assertEqual(self.items(rid="recU10"), [])
        self.assertIsNone(self.user_by_rid("recU10"))  # a remembered Skip still holds
        self.assertIn("recU10", {l.airtable_record_id for l in self.logs(run, "held")})

    def test_ensure_default_respects_a_hub_removal(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("no_program_roles", "recU10").id, "assign_default")
        ivy = self.user_by_rid("recU10")
        self.db.query(RoleAssignmentV2).filter_by(user_id=ivy.id).delete()
        self.db.commit()
        db = self.Session()
        try:
            writer = users_step.UserWriter(db, lambda *a, **k: None, dry_run=False, run_id=None)
            hu = writer.index.by_record_id["recU10"]
            self.assertTrue(writer.default_removed_by_hub("recU10", hu))
            self.assertIsNone(writer.ensure_default("recU10", hu))
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.assignments(ivy.id), set())

    def test_assign_default_never_stacks_on_a_held_pair(self) -> None:
        self.tables[T_ROLES]["records"] = [r for r in self.tables[T_ROLES]["records"] if r["fields"]["Employee Record ID"] != "recU1"]
        self.db.add(RoleAssignmentV2(user_id=self.ada, role_id=self.user_role.id, scope_id=self.hub_scope, source="default", created_at=_now()))
        self.db.commit()
        self.run_sync()
        self.resolved_ok(self.item("no_program_roles", "recU1").id, "assign_default")
        self.assertEqual(self.assignments(self.ada), {(self.user_role.id, self.hub_scope, "default")})
        self.assertEqual(self.user_by_rid("recU1").id, self.ada)
        self.assertEqual([l.action for l in self.logs()], ["linked", "default_already_held", "item_resolved"])
        self.run_sync()  # asked again, but the answer is remembered: nothing reopens
        self.assertEqual(self.items(rid="recU1"), [])

    def test_admin_removing_the_sync_default_is_respected(self) -> None:
        self.run_sync()
        self.resolved_ok(self.item("no_program_roles", "recU10").id, "assign_default")
        ivy = self.user_by_rid("recU10")
        self.assertTrue(self.record("recU10").base["default"])
        self.db.query(RoleAssignmentV2).filter_by(user_id=ivy.id).delete()
        self.db.commit()
        run = self.run_sync()
        self.assertEqual(self.items(rid="recU10"), [])  # not asked again
        self.assertEqual(self.assignments(ivy.id), set())  # the Hub's removal stands (M1)
        self.assertEqual([(l.airtable_record_id, l.field) for l in self.logs(run, "hub_differs")], [("recU10", "default")])


# ---------------------------------------------------------
# The items API: rules, the lock, access, listing
# ---------------------------------------------------------


class TestItemsAPI(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_sync()

    def test_no_button_items_are_refused(self) -> None:
        for kind, rid in (("no_work_email", "recU7"), ("duplicate_work_email", "recU8")):
            res = self.resolve(self.item(kind, rid).id, "skip")
            self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "item_has_no_choices"))

    def test_invalid_choice_not_open_and_unknown(self) -> None:
        item = self.item("possible_match", "recU4")
        res = self.resolve(item.id, "assign_default")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "invalid_choice"))
        self.assertEqual(res.json()["detail"]["choices"], ["link", "create_new", "skip"])
        self.resolved_ok(item.id, "skip")
        res = self.resolve(item.id, "skip")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "item_not_open"))
        self.assertEqual(self.resolve(99999, "skip").status_code, 404)
        res = self.client.post(f"/v2/airtable-sync/items/{item.id}/resolve", json={"choice": "skip", "extra": 1}, headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 422)

    def test_refused_while_a_run_is_going(self) -> None:
        item = self.item("possible_match", "recU4")
        now = _now()
        run = AirtableSyncRunV2(steps=["users"], trigger="manual", status="running", started_by_email="other@renphil.org", started_at=now, heartbeat_at=now)
        self.db.add(run)
        self.db.commit()
        res = self.resolve(item.id, "link")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "run_in_progress"))
        self.assertEqual(res.json()["detail"]["run_id"], run.id)
        self.assertEqual(self.item("possible_match", "recU4").status, "open")
        self.assertIsNone(self.get_user(self.di).airtable_record_id)
        # A stale row (its thread is gone) does not block.
        run.heartbeat_at = now - timedelta(minutes=10)
        run.started_at = now - timedelta(minutes=10)
        self.db.commit()
        self.resolved_ok(item.id, "link")

    def test_start_and_resolve_take_the_sync_lock(self) -> None:
        with mock.patch.object(runner_module, "take_sync_lock", wraps=runner_module.take_sync_lock) as lock:
            self.run_sync()
            self.assertEqual(lock.call_count, 1)
            self.resolved_ok(self.item("possible_match", "recU4").id, "skip")
            self.assertEqual(lock.call_count, 2)

    def test_hub_admins_only(self) -> None:
        member = self.auth(MEMBER_EMAIL)
        item = self.item("possible_match", "recU4")
        self.assertEqual(self.client.get("/v2/airtable-sync/items", headers=member).status_code, 403)
        res = self.client.post(f"/v2/airtable-sync/items/{item.id}/resolve", json={"choice": "link"}, headers=member)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.item("possible_match", "recU4").status, "open")
        self.assertIsNone(self.get_user(self.di).airtable_record_id)
        self.assertIn(self.client.get("/v2/airtable-sync/items").status_code, (401, 403))

    def test_list_filters_and_pages(self) -> None:
        admin = self.auth(ADMIN_EMAIL)
        res = self.client.get("/v2/airtable-sync/items", headers=admin)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(len(data["items"]), 9)  # 6 + phase 6's three first-link field conflicts
        self.assertIsNone(data["next_cursor"])
        by_kind = {i["kind"]: i for i in data["items"]}
        self.assertEqual(by_kind["possible_match"]["choices"], ["link", "create_new", "skip"])
        self.assertEqual(by_kind["no_work_email"]["choices"], [])
        self.assertEqual(by_kind["field_conflict"]["choices"], ["accept_airtable", "keep_current"])
        page = self.client.get("/v2/airtable-sync/items?limit=4", headers=admin).json()["data"]
        self.assertEqual(len(page["items"]), 4)
        rest = self.client.get(f"/v2/airtable-sync/items?limit=5&cursor={page['next_cursor']}", headers=admin).json()["data"]
        self.assertEqual(len(rest["items"]), 5)
        self.assertIsNone(rest["next_cursor"])
        kinds = self.client.get("/v2/airtable-sync/items?kind=no_program_roles", headers=admin).json()["data"]["items"]
        self.assertEqual({i["airtable_record_id"] for i in kinds}, {"recU10", "recU11"})
        q = self.client.get("/v2/airtable-sync/items?q=hal%20two", headers=admin).json()["data"]["items"]
        self.assertEqual([i["airtable_record_id"] for i in q], ["recU9"])
        self.assertEqual(self.client.get("/v2/airtable-sync/items?entity=cluster", headers=admin).json()["data"]["items"], [])
        self.resolved_ok(self.item("possible_match", "recU4").id, "skip")
        resolved = self.client.get("/v2/airtable-sync/items?status=resolved", headers=admin).json()["data"]["items"]
        self.assertEqual([(i["kind"], i["resolution"], i["choices"]) for i in resolved], [("possible_match", "skip", [])])
        self.assertEqual(len(self.client.get("/v2/airtable-sync/items?status=all", headers=admin).json()["data"]["items"]), 9)
        self.assertEqual(self.client.get("/v2/airtable-sync/items?status=bogus", headers=admin).status_code, 422)
        status = self.client.get("/v2/airtable-sync/status", headers=admin).json()["data"]
        self.assertEqual(status["open_items_total"], 8)


# ---------------------------------------------------------
# Scope resolutions (phase 3's kinds)
# ---------------------------------------------------------


class TestScopeResolutions(SyncFixture):
    def scope_by_rid(self, rid: str) -> ScopeV2 | None:
        self.db.expire_all()
        return self.db.query(ScopeV2).filter(ScopeV2.airtable_record_id == rid).one_or_none()

    def cluster(self, rid: str) -> dict:
        return next(r["fields"] for r in self.tables[T_CLUSTERS]["records"] if r["fields"]["Record Id"] == rid)

    def conflict(self) -> AirtableSyncItemV2:
        self.run_sync(("clusters",))
        space = self.scope_by_rid(SPACE)
        space.name = "Space Hub"
        self.db.commit()
        self.cluster(SPACE)["Name"] = "Outer Space"
        self.run_sync(("clusters",))
        return self.item("scope_name_conflict", SPACE)

    def test_name_conflict_accept_airtable(self) -> None:
        item = self.conflict()
        self.resolved_ok(item.id, "accept_airtable")
        self.assertEqual(self.scope_by_rid(SPACE).name, "Outer Space")
        self.assertEqual(self.record(SPACE, "cluster").base["name"], "Outer Space")
        self.assertEqual([l.action for l in self.logs()], ["scope_renamed", "item_resolved"])
        self.run_sync(("clusters",))
        self.assertEqual(self.items(), [])

    def test_name_conflict_keep_current(self) -> None:
        item = self.conflict()
        self.resolved_ok(item.id, "keep_current")
        self.assertEqual(self.scope_by_rid(SPACE).name, "Space Hub")
        self.assertEqual(self.record(SPACE, "cluster").base["name"], "Outer Space")
        run = self.run_sync(("clusters",))
        self.assertEqual(self.items(), [])
        self.assertEqual([l.airtable_record_id for l in self.logs(run, "hub_differs")], [SPACE])

    def test_name_conflict_accept_when_taken(self) -> None:
        item = self.conflict()
        self.db.add(ScopeV2(key="outer", name="outer space", is_universal=False, is_public=False, is_system=False, created_at=_now()))
        self.db.commit()
        res = self.resolve(item.id, "accept_airtable")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "scope_name_taken"))
        self.assertEqual(self.scope_by_rid(SPACE).name, "Space Hub")

    def ambiguous(self) -> tuple[AirtableSyncItemV2, int, int]:
        now = _now()
        a = ScopeV2(key="climate_a", name="Climate", is_universal=False, is_public=False, is_system=False, created_at=now)
        b = ScopeV2(key="climate_b", name="climate", is_universal=False, is_public=False, is_system=False, created_at=now)
        self.db.add_all([a, b])
        self.db.commit()
        self.run_sync(("clusters",))
        return self.item("scope_ambiguous_match", CLIMATE), a.id, b.id

    def test_ambiguous_link(self) -> None:
        item, a, b = self.ambiguous()
        self.assertEqual(item.payload["choices"], ["link", "skip"])
        res = self.resolve(item.id, "create_new")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "scope_name_taken"))
        res = self.resolve(item.id, "link", {"scope_id": self.studio})
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "invalid_data"))
        self.resolved_ok(item.id, "link", {"scope_id": b})
        scope = self.scope_by_rid(CLIMATE)
        self.assertEqual((scope.id, scope.type, scope.name), (b, "cluster", "climate"))
        self.assertEqual(self.record(CLIMATE, "cluster").hub_id, b)
        self.assertEqual([l.action for l in self.logs()], ["linked", "hub_differs", "item_resolved"])
        self.run_sync(("clusters",))
        self.assertEqual(self.items(), [])
        self.assertIsNone(self.db.get(ScopeV2, a).airtable_record_id)

    def test_ambiguous_skip_is_remembered(self) -> None:
        item, _a, _b = self.ambiguous()
        self.resolved_ok(item.id, "skip")
        self.run_sync(("clusters",))
        self.assertEqual(self.items(), [])
        self.assertIsNone(self.scope_by_rid(CLIMATE))

    def deleted(self, *, in_use: bool = False) -> AirtableSyncItemV2:
        self.run_sync(("clusters",))
        climate = self.scope_by_rid(CLIMATE)
        if in_use:
            self.db.add(RoleAssignmentV2(user_id=self.member, role_id=self.member_role.id, scope_id=climate.id, created_at=_now()))
            self.db.commit()
        self.tables[T_CLUSTERS]["records"] = [r for r in self.tables[T_CLUSTERS]["records"] if r["fields"]["Record Id"] != CLIMATE]
        self.run_sync(("clusters",))
        return self.item("scope_deleted", CLIMATE)

    def test_scope_deleted_delete(self) -> None:
        item = self.deleted()
        scope_id = item.hub_id
        self.resolved_ok(item.id, "delete_scope")
        self.assertIsNone(self.db.get(ScopeV2, scope_id))
        self.assertIsNone(self.record(CLIMATE, "cluster").hub_id)
        self.run_sync(("clusters",))
        self.assertEqual(self.items(), [])

    def test_scope_deleted_in_use_cannot_be_deleted(self) -> None:
        item = self.deleted(in_use=True)
        self.assertEqual(item.payload["choices"], ["keep_hub_only"])
        res = self.resolve(item.id, "delete_scope")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "invalid_choice"))

    def test_scope_deleted_keep_hub_only(self) -> None:
        item = self.deleted(in_use=True)
        scope_id = item.hub_id
        self.resolved_ok(item.id, "keep_hub_only")
        scope = self.db.get(ScopeV2, scope_id)
        self.assertIsNone(scope.airtable_record_id)
        self.assertEqual(scope.type, "cluster")  # kept (v2 §4.1)
        record = self.record(CLIMATE, "cluster")
        self.assertEqual((record.hub_id, record.base), (None, None))
        self.run_sync(("clusters",))
        self.assertEqual(self.items(), [])

    def edge_item(self) -> AirtableSyncItemV2:
        self.run_sync(("clusters", "programs"))
        program = next(r["fields"] for r in self.tables[T_PROGRAMS]["records"] if r["fields"]["Record ID"] == P1)
        del program["Cluster Record Id"]
        del program["Program Cluster"]
        self.run_sync(("clusters", "programs"))
        return self.item("cluster_edge_removed", P1)

    def edges(self) -> dict[int, str]:
        self.db.expire_all()
        child = self.scope_by_rid(P1).id
        return {p: s for p, s in self.db.query(ScopeEdgeV2.parent_scope_id, ScopeEdgeV2.source).filter(ScopeEdgeV2.child_scope_id == child)}

    def test_edge_remove(self) -> None:
        item = self.edge_item()
        self.assertEqual(self.edges(), {self.scope_by_rid(SPACE).id: "airtable_sync"})
        self.resolved_ok(item.id, "remove_edge")
        self.assertEqual(self.edges(), {})
        self.assertEqual(self.record(P1, "program").base["clusters"], [])
        self.run_sync(("clusters", "programs"))
        self.assertEqual(self.items(), [])

    def test_edge_keep_becomes_manual(self) -> None:
        item = self.edge_item()
        self.resolved_ok(item.id, "keep")
        self.assertEqual(self.edges(), {self.scope_by_rid(SPACE).id: "manual"})
        self.assertEqual(self.record(P1, "program").base["clusters"], [])
        self.run_sync(("clusters", "programs"))
        self.assertEqual(self.items(), [])
        self.assertEqual(self.edges(), {self.scope_by_rid(SPACE).id: "manual"})


# ---------------------------------------------------------
# Preview
# ---------------------------------------------------------


class TestPreview(SyncFixture):
    def test_preview_writes_only_log_rows(self) -> None:
        before = self.hub_counts()
        run = self.run_sync(("clusters", "programs", "users", "program_roles"), preview=True)
        self.assertEqual(self.hub_counts(), before)
        s = run.summary["steps"]["users"]
        self.assertEqual((s["linked"], s["created"], s["held"]), (1, 2, 8))  # phase 6: Bo, Cy held (M2)
        self.assertEqual({l.airtable_record_id for l in self.logs(run, "user_created")}, {"recU6", "recU12"})
        self.assertTrue(all(l.hub_id is None for l in self.logs(run, "user_created")))
        # Users-entity items only (phase 5's pair items have "recU…|…" keys).
        would = {(l.action, l.airtable_record_id, l.old_value) for l in self.logs(run) if l.action.startswith("would_raise_") and l.entity == "user"}
        self.assertIn(("would_raise_possible_match", "recU4", "open"), would)
        self.assertIn(("would_raise_no_program_roles", "recU10", "open"), would)
        self.assertIn(("would_raise_field_conflict", "recU2", "open"), would)  # phase 6 (M2)
        self.assertEqual(len([w for w in would if w[1].startswith("recU")]), 8)  # 6 + Bo's and Cy's field conflicts

    def test_preview_reports_refresh_and_auto_close(self) -> None:
        self.run_sync()
        self.at_user(7)["Work Email"] = "gus@renphil.org"
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual([(l.airtable_record_id, l.field) for l in self.logs(run, "would_auto_close")], [("recU7", "no_work_email")])
        self.assertIn("recU7", {l.airtable_record_id for l in self.logs(run, "user_created")})
        refresh = {l.airtable_record_id for l in self.logs(run) if l.action.startswith("would_raise_") and l.old_value == "refresh"}
        self.assertEqual(refresh, {"recU2", "recU3", "recU4", "recU8", "recU9", "recU10", "recU11"})  # + phase 6's field conflicts

    def test_preview_sees_its_own_would_be_users(self) -> None:
        # Two new people: the second's alias is the first's work email. A
        # real run creates the first, then finds its row linked elsewhere
        # (email_taken); the preview must say the same.
        self.only_users(1)
        self.tables[T_USERS]["records"] += [person(30, "New One", "one@renphil.org"), person(31, "New Two", "two@renphil.org", "one@renphil.org")]
        self.tables[T_ROLES]["records"] += [role_row(30, "recU30", P1), role_row(31, "recU31", P1)]
        run = self.run_sync(preview=True)
        self.assertIn(("would_raise_email_taken", "recU31"), {(l.action, l.airtable_record_id) for l in self.logs(run)})
        self.run_sync()
        self.assertEqual(self.item("email_taken", "recU31").payload["taken_by"]["airtable_record_id"], "recU30")


if __name__ == "__main__":
    unittest.main()
