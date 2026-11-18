"""Airtable sync, phase 5 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §3.4
A1–A4, §3.5, §8 step 6; A-1): the Program Roles step — pairs, the
Designation mapping, A3's resolution order, the three-way per pair,
`assignment_removed` — and its items' resolutions through the items API.

SQLite in memory with every session closed; FakeSource and fixture copied
from phase 4, not imported. Airtable is never called. Runs go through the
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase5-tests-only")
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
from app.services.airtable_sync.runner import SyncRunner, get_sync_runner  # noqa: E402
from app.services.rbac_graph_service import RbacGraphError  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"
MEMBER_EMAIL = "member@renphil.org"
PAT = "patFAKE.0123456789abcdefSECRET"
BASE = "appHUB"
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
    "AIRTABLE_SYNC_PROGRAM_ROLES_TABLE": T_ROLES,
    "AIRTABLE_SYNC_PROGRAM_ROLES_EMPLOYEE_RECORD_ID_FIELD": "Employee Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_PROGRAM_RECORD_ID_FIELD": "Original Program Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD": "Designation",
}

SPACE = "recCLSPACE"
P1, P2, P_ALIAS, P_MISSING = "recP1", "recP2", "recPALIAS", "recMISSING"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def U(n: int) -> str:
    return f"recU{n}"


def key(n: int | str, program: str) -> str:
    return f"{U(n) if isinstance(n, int) else n}|{program}"


def person(n: int, name: str, work: str | None = None) -> dict:
    fields = {"Record ID": U(n), "Name": name}
    if work is not None:
        fields["Work Email"] = work
    return {"id": f"recAPIu{n}", "fields": fields}


def role_row(n: int, employee: str, program: str, designation: str = "Member") -> dict:
    return {
        "id": f"recAPIr{n}",
        "fields": {"Employee Record ID": employee, "Original Program Record ID": program, "Designation": designation},
    }


def airtable_tables() -> dict:
    users = [
        person(1, "Ada", "ada@renphil.org"),  # linked by email; two pairs
        person(2, "Bo", "bo@renphil.org"),  # created
        person(3, "Cy", "cy@renphil.org"),  # linked by email
        person(4, "Di"),  # no Work Email: held (person-level)
        person(5, "Ed", "ed@renphil.org"),  # soft-deleted in the Hub (M7)
        person(6, "Fay", "fay@renphil.org"),  # only a missing program
        person(7, "Gus", "gus@renphil.org"),  # a Don't-sync row + a real one
        person(8, "Hal", "hal@renphil.org"),  # an unmapped Designation
        person(9, "Ivy", "ivy@renphil.org"),  # a duplicate pair
        person(10, "Jo", "jo@renphil.org"),  # on the C2 alias
    ]
    roles = [
        role_row(1, U(1), P1),
        role_row(2, U(1), P2, "Program Lead"),
        role_row(3, U(2), P1),
        role_row(4, U(3), P2),
        role_row(5, U(4), P1),
        role_row(6, U(5), P1),
        role_row(7, U(6), P_MISSING),
        role_row(8, U(7), P1, "Advisor"),
        role_row(9, U(7), P2),
        role_row(10, U(8), P1, "Wizard"),
        role_row(11, U(9), P1),
        role_row(12, U(9), P1, " program lead "),
        role_row(13, U(10), P_ALIAS),
        role_row(14, "recNOBODY", P1),
    ]
    return {
        T_CLUSTERS: {"fields": {"Name", "Record Id"}, "records": [{"id": "recAPIc1", "fields": {"Name": "Space", "Record Id": SPACE}}]},
        T_PROGRAMS: {
            "fields": {"Name", "Record ID", "Cluster Record Id", "Program Cluster"},
            "records": [
                {"id": "recAPIp1", "fields": {"Name": "Space Science", "Record ID": P1, "Cluster Record Id": SPACE, "Program Cluster": ["recAPIc1"]}},
                {"id": "recAPIp2", "fields": {"Name": "Lone Program", "Record ID": P2}},
                {"id": "recAPIp3", "fields": {"Name": "Space", "Record ID": P_ALIAS}},  # C2: the cluster itself
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

        def role(k, name, **kw):
            row = RoleV2(key=k, name=name, is_public=kw.pop("public", False), is_system=False, created_at=now)
            db.add(row)
            db.flush()
            return row.id

        self.admin_role = role("hub_admin", "Admin")
        self.user_role = role("user", "User", public=True)
        self.member_role = role("member", "Member")
        self.lead_role = role("program_lead", "Program Lead")
        self.fellow_role = role("fellow", "Fellow")

        def scope(k, name, **kw):
            row = ScopeV2(key=k, name=name, is_universal=kw.pop("universal", False), is_public=kw.pop("public", False), is_system=kw.pop("system", False), created_at=now)
            db.add(row)
            db.flush()
            return row.id

        self.all_scopes = scope("all_scopes", "All Scopes", universal=True, system=True)
        self.hub_scope = scope("hub", "Hub", public=True)
        self.studio = scope("studio", "Studio")

        def user(name, work=None, **kw):
            row = UserV2(name=name, work_email=work, has_signed_in=kw.pop("signed_in", False), created_at=now, updated_at=now, **kw)
            db.add(row)
            db.flush()
            return row.id

        self.admin = user("Admin", ADMIN_EMAIL, signed_in=True)
        self.member = user("Member", MEMBER_EMAIL, signed_in=True)
        self.ada = user("Ada", "ada@renphil.org", signed_in=True)
        self.cy = user("Cy", "cy@renphil.org")
        self.ed = user("Ed", "ed@renphil.org", airtable_record_id=U(5), is_deleted=True, deleted_at=now, deleted_by=ADMIN_EMAIL)
        db.add(RoleAssignmentV2(user_id=self.admin, role_id=self.admin_role, scope_id=self.all_scopes, created_at=now))
        db.add(RoleAssignmentV2(user_id=self.member, role_id=self.member_role, scope_id=self.studio, created_at=now))
        db.add(AirtableDesignationMappingV2(designation="Member", action="role", role_id=self.member_role, updated_at=now))
        db.add(AirtableDesignationMappingV2(designation="Program Lead", action="role", role_id=self.lead_role, updated_at=now))
        db.add(AirtableDesignationMappingV2(designation="Advisor", action="dont_sync", updated_at=now))
        db.commit()

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

    def pr(self, run: AirtableSyncRunV2) -> dict:
        return run.summary["steps"]["program_roles"]

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

    # ── reading ──────────────────────────────────────────────────────────

    def uid(self, n: int) -> int:
        self.db.expire_all()
        row = self.db.query(UserV2).filter(UserV2.airtable_record_id == U(n)).one()
        return row.id

    def sid(self, rid: str) -> int:
        self.db.expire_all()
        return self.db.query(ScopeV2.id).filter(ScopeV2.airtable_record_id == rid).scalar()

    def record(self, k: str) -> AirtableSyncRecordV2 | None:
        self.db.expire_all()
        return self.db.query(AirtableSyncRecordV2).filter_by(entity="program_role", airtable_record_id=k).one_or_none()

    def logs(self, run: AirtableSyncRunV2 | None = None, action: str | None = None) -> list[AirtableSyncLogV2]:
        self.db.expire_all()
        q = self.db.query(AirtableSyncLogV2)
        q = q.filter(AirtableSyncLogV2.run_id == run.id) if run is not None else q.filter(AirtableSyncLogV2.run_id.is_(None))
        if action:
            q = q.filter(AirtableSyncLogV2.action == action)
        return q.order_by(AirtableSyncLogV2.id).all()

    def pr_logs(self, run, action) -> list[str]:
        return [l.airtable_record_id for l in self.logs(run, action) if l.entity == "program_role"]

    def items(self, kind: str | None = None, status: str | None = "open", rid: str | None = None, entity: str = "program_role") -> list[AirtableSyncItemV2]:
        self.db.expire_all()
        q = self.db.query(AirtableSyncItemV2).filter(AirtableSyncItemV2.entity == entity)
        if kind:
            q = q.filter(AirtableSyncItemV2.kind == kind)
        if status:
            q = q.filter(AirtableSyncItemV2.status == status)
        if rid:
            q = q.filter(AirtableSyncItemV2.airtable_record_id == rid)
        return q.order_by(AirtableSyncItemV2.id).all()

    def item(self, kind: str, rid: str, entity: str = "program_role") -> AirtableSyncItemV2:
        found = self.items(kind, rid=rid, entity=entity)
        self.assertEqual(len(found), 1, [(i.kind, i.airtable_record_id) for i in self.items(entity=entity)])
        return found[0]

    def assignments(self, user_id: int) -> set[tuple[int, int, str]]:
        self.db.expire_all()
        return {(a.role_id, a.scope_id, a.source) for a in self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == user_id)}

    def managed(self, k: str) -> RoleAssignmentV2 | None:
        rec = self.record(k)
        return self.db.get(RoleAssignmentV2, rec.managed_assignment_id) if rec and rec.managed_assignment_id else None

    def hub_counts(self) -> dict[str, int]:
        self.db.expire_all()
        return {
            t.name: self.db.query(func.count()).select_from(t).scalar()
            for t in BaseV2.metadata.sorted_tables
            # Phase 7: a real run notifies every Hub Admin (owner, 2026-10-24:
            # left out here; tests.test_airtable_sync_phase7 covers them).
            if t.name not in ("airtable_sync_runs", "airtable_sync_log", "notifications")
        }

    # ── editing Airtable / the Hub ───────────────────────────────────────

    def set_designation(self, n: int, program: str, designation: str) -> None:
        for r in self.tables[T_ROLES]["records"]:
            f = r["fields"]
            if (f["Employee Record ID"], f["Original Program Record ID"]) == (U(n), program):
                f["Designation"] = designation
                return
        raise KeyError((n, program))

    def drop_pair(self, n: int, program: str) -> None:
        self.tables[T_ROLES]["records"] = [
            r for r in self.tables[T_ROLES]["records"]
            if (r["fields"]["Employee Record ID"], r["fields"]["Original Program Record ID"]) != (U(n), program)
        ]

    def add_pair(self, n: int, program: str, designation: str = "Member") -> None:
        self.tables[T_ROLES]["records"].append(role_row(100 + len(self.tables[T_ROLES]["records"]), U(n), program, designation))

    def at_user(self, n: int) -> dict:
        for r in self.tables[T_USERS]["records"]:
            if r["fields"].get("Record ID") == U(n):
                return r["fields"]
        raise KeyError(n)

    def drop_user(self, n: int) -> None:
        self.tables[T_USERS]["records"] = [r for r in self.tables[T_USERS]["records"] if r["fields"].get("Record ID") != U(n)]

    def add_assignment(self, user_id: int, role_id: int, scope_id: int, source: str = "manual") -> int:
        row = RoleAssignmentV2(user_id=user_id, role_id=role_id, scope_id=scope_id, source=source, created_at=_now())
        self.db.add(row)
        self.db.commit()
        return row.id

    def edit_row(self, assignment_id: int, **values) -> None:
        self.db.expire_all()
        row = self.db.get(RoleAssignmentV2, assignment_id)
        for k, v in values.items():
            setattr(row, k, v)
        self.db.commit()

    def delete_row(self, assignment_id: int) -> None:
        self.db.expire_all()
        self.db.delete(self.db.get(RoleAssignmentV2, assignment_id))
        self.db.commit()

    def scopes_only(self) -> None:
        """Scopes synced, people not (yet)."""
        self.run_sync(("clusters", "programs"))


# ---------------------------------------------------------
# A1–A3: the full run, resolution order
# ---------------------------------------------------------


class TestFullRun(SyncFixture):
    def test_full_run(self) -> None:
        run = self.run_sync()
        s = self.pr(run)
        self.assertEqual(s["records"], 14)
        self.assertEqual(s["created"], 6)  # Ada ×2, Bo, Cy, Gus/P2, Jo
        self.assertEqual(s["employee_not_in_users"], 1)
        self.assertEqual(s["user_not_synced"], 1)  # Di
        self.assertEqual(s["skipped_deleted_in_hub"], 1)  # Ed
        self.assertEqual(s["skipped_dont_sync"], 1)  # Gus/P1
        self.assertEqual(s["held"], 3)  # missing, unmapped, duplicate
        self.assertEqual(s["items_opened"], 3)
        self.assertEqual(
            sorted((i.kind, i.airtable_record_id) for i in self.items()),
            [
                ("duplicate_program_role", key(9, P1)),
                ("missing_program", key(6, P_MISSING)),
                ("unmapped_designation", key(8, P1)),
            ],
        )
        p1, p2 = self.sid(P1), self.sid(P2)
        self.assertEqual(
            self.assignments(self.ada), {(self.member_role, p1, "airtable_sync"), (self.lead_role, p2, "airtable_sync")}
        )
        self.assertEqual(self.assignments(self.uid(2)), {(self.member_role, p1, "airtable_sync")})
        self.assertEqual(self.assignments(self.uid(7)), {(self.member_role, p2, "airtable_sync")})  # Advisor skipped
        self.assertEqual(self.assignments(self.ed), set())  # M7
        rec = self.record(key(1, P1))
        self.assertEqual(rec.hub_id, self.ada)
        self.assertEqual(rec.base, {"role_id": self.member_role, "scope_id": p1, "designation": "Member"})
        self.assertEqual(rec.latest_airtable["target"], rec.base)
        self.assertEqual(self.managed(key(1, P1)).source, "airtable_sync")
        self.assertEqual(rec.last_seen_run_id, run.id)
        log = self.logs(run, "assignment_created")[0]
        self.assertEqual((log.entity, log.subject), ("program_role", "Ada · Space Science"))
        self.assertEqual(self.pr_logs(run, "employee_not_in_users"), [key("recNOBODY", P1)])
        self.assertEqual(self.pr_logs(run, "skipped_dont_sync"), [key(7, P1)])
        self.assertEqual(self.pr_logs(run, "skipped_deleted_in_hub"), [key(5, P1)])
        self.assertEqual([(l.airtable_record_id, l.field) for l in self.logs(run, "user_not_synced")], [(key(4, P1), "held")])

    def test_second_run_changes_nothing(self) -> None:
        self.run_sync()
        before = self.hub_counts()
        run = self.run_sync()
        s = self.pr(run)
        self.assertEqual(s["unchanged"], 6)
        for k in ("created", "adopted", "replaced", "removed", "hub_differs", "items_opened"):
            self.assertNotIn(k, s)
        self.assertEqual(s["items_refreshed"], 3)
        self.assertEqual(self.hub_counts(), before)

    def test_c2_alias_lands_on_the_cluster_scope(self) -> None:
        self.run_sync()
        self.assertEqual(self.assignments(self.uid(10)), {(self.member_role, self.sid(SPACE), "airtable_sync")})
        # A Program-Roles-only run resolves it from the alias's sync record.
        self.set_designation(10, P_ALIAS, "Program Lead")
        self.run_sync(("program_roles",))
        self.assertEqual(self.assignments(self.uid(10)), {(self.lead_role, self.sid(SPACE), "airtable_sync")})

    def test_scope_not_synced_waits_then_applies(self) -> None:
        run = self.run_sync(("users", "program_roles"))
        s = self.pr(run)
        # Every pair on a known program, the alias and Hal's unmapped one included.
        self.assertEqual(s["scope_not_synced"], 7)
        self.assertNotIn("created", s)
        self.assertEqual(self.items("missing_program")[0].airtable_record_id, key(6, P_MISSING))  # still raised
        self.assertEqual(self.assignments(self.ada), set())
        run = self.run_sync()
        self.assertEqual(self.pr(run)["created"], 6)

    def test_user_not_synced_when_users_did_not_run(self) -> None:
        self.scopes_only()
        run = self.run_sync(("program_roles",))
        s = self.pr(run)
        self.assertEqual(s["user_not_synced"], 11)  # every pair of a Users person: nobody is linked yet; Ed is M7
        self.assertEqual(s["skipped_deleted_in_hub"], 1)
        self.assertEqual(self.items(), [])  # pairs of people who aren't Hub users yet are not checked
        self.assertEqual(self.assignments(self.ada), set())

    def test_person_level_item_holds_every_pair(self) -> None:
        self.run_sync()
        before = self.assignments(self.ada)
        self.at_user(1).pop("Work Email")  # no_work_email holds Ada (person-level)
        self.set_designation(1, P1, "Program Lead")
        self.drop_pair(1, P2)
        run = self.run_sync()
        self.assertEqual(self.pr(run)["user_not_synced"], 2)  # Ada's P1 (P2 is gone) and Di
        self.assertEqual(self.assignments(self.ada), before)  # no replace, no removal item
        self.assertEqual(self.items("assignment_removed"), [])
        # A Program-Roles-only run reads the hold from the open item.
        run = self.run_sync(("program_roles",))
        self.assertEqual(self.assignments(self.ada), before)
        self.assertIn((key(1, P1), "held"), {(l.airtable_record_id, l.field) for l in self.logs(run, "user_not_synced")})
        self.assertEqual(self.items("assignment_removed"), [])

    def test_held_person_carries_their_pair_items(self) -> None:
        self.run_sync()
        hal_item = self.item("unmapped_designation", key(8, P1))
        self.at_user(8).pop("Work Email")
        self.run_sync()
        self.assertEqual(self.item("unmapped_designation", key(8, P1)).id, hal_item.id)  # carried, not closed

    def test_m7_soft_deleted_person_is_never_touched(self) -> None:
        self.run_sync()
        # Ed had a sync pair before the soft delete; it is never stripped.
        a = self.add_assignment(self.ed, self.member_role, self.sid(P2), source="airtable_sync")
        self.db.add(AirtableSyncRecordV2(entity="program_role", airtable_record_id=key(5, P2), hub_id=self.ed, base={"role_id": self.member_role, "scope_id": self.sid(P2), "designation": "Member"}, managed_assignment_id=a))
        self.db.commit()
        run = self.run_sync()  # Ed's P2 pair is not in Airtable
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertIn((self.member_role, self.sid(P2), "airtable_sync"), self.assignments(self.ed))
        self.assertEqual(self.pr_logs(run, "skipped_deleted_in_hub"), [key(5, P1)])

    def test_employee_not_in_users_closes_its_items(self) -> None:
        self.run_sync()
        self.drop_user(6)  # Fay leaves Users; her missing_program has nothing left to ask
        run = self.run_sync()
        self.assertEqual(self.items("missing_program"), [])
        self.assertEqual(self.pr(run)["employee_not_in_users"], 2)

    def test_missing_program_closes_once_airtable_is_fixed(self) -> None:
        self.run_sync()
        self.tables[T_PROGRAMS]["records"].append({"id": "recAPIp9", "fields": {"Name": "Studio", "Record ID": P_MISSING}})
        run = self.run_sync()
        self.assertEqual(self.items("missing_program"), [])
        self.assertEqual(self.assignments(self.uid(6)), {(self.member_role, self.studio, "airtable_sync")})  # linked by name
        self.assertEqual(self.pr(run)["items_closed"], 1)

    def test_duplicate_pair_holds_only_that_pair(self) -> None:
        self.add_pair(9, P2)
        self.run_sync()
        it = self.item("duplicate_program_role", key(9, P1))
        self.assertEqual(it.payload["designations"], ["Member", "program lead"])  # the reader trims
        self.assertEqual(self.assignments(self.uid(9)), {(self.member_role, self.sid(P2), "airtable_sync")})
        self.assertEqual(allowed(self, it), [])

    def test_blank_record_ids_are_skipped(self) -> None:
        self.tables[T_ROLES]["records"].append(role_row(99, "", P1))
        run = self.run_sync()
        self.assertEqual(self.pr(run)["skipped"], 1)
        self.assertEqual(len(self.logs(run, "skipped_no_record_id")), 1)

    def test_a_data_refusal_skips_one_pair(self) -> None:
        from app.services.airtable_sync import program_roles_step

        real = program_roles_step.PairWriter.ensure

        def ensure(writer, k, *a, **kw):
            if k == key(2, P1):
                raise RbacGraphError("refused", "Refused for the test.")
            return real(writer, k, *a, **kw)

        with mock.patch.object(program_roles_step.PairWriter, "ensure", ensure):
            run = self.run_sync()
        self.assertEqual(self.pr(run)["errors"], 1)
        self.assertEqual(self.pr(run)["created"], 5)
        self.assertEqual(self.assignments(self.uid(2)), set())
        self.assertIsNone(self.record(key(2, P1)))
        self.assertEqual(self.pr_logs(run, "assignment_error"), [key(2, P1)])


def allowed(case: SyncFixture, it: AirtableSyncItemV2) -> list[str]:
    res = case.client.get("/v2/airtable-sync/items", params={"kind": it.kind}, headers=case.auth(ADMIN_EMAIL))
    case.assertEqual(res.status_code, 200, res.text)
    return next(i["choices"] for i in res.json()["data"]["items"] if i["id"] == it.id)


# ---------------------------------------------------------
# A2: the Designation mapping
# ---------------------------------------------------------


class TestMapping(SyncFixture):
    def test_unmapped_designation_then_mapped(self) -> None:
        self.run_sync()
        it = self.item("unmapped_designation", key(8, P1))
        self.assertEqual((it.payload["reason"], it.payload["designation"]), ("unmapped", "Wizard"))
        self.assertEqual(allowed(self, it), ["skip"])
        self.assertEqual(it.hub_id, self.uid(8))
        self.db.add(AirtableDesignationMappingV2(designation="wizard", action="role", role_id=self.fellow_role, updated_at=_now()))
        self.db.commit()
        self.run_sync(("program_roles",))
        self.assertEqual(self.items("unmapped_designation"), [])
        self.assertEqual(self.assignments(self.uid(8)), {(self.fellow_role, self.sid(P1), "airtable_sync")})

    def test_deleted_role_is_unmapped(self) -> None:
        self.db.add(AirtableDesignationMappingV2(designation="Wizard", action="role", role_id=self.fellow_role, updated_at=_now()))
        self.db.commit()
        self.run_sync()
        self.assertEqual(self.assignments(self.uid(8)), {(self.fellow_role, self.sid(P1), "airtable_sync")})
        self.db.query(RoleAssignmentV2).filter_by(role_id=self.fellow_role).delete()
        self.db.delete(self.db.get(RoleV2, self.fellow_role))
        self.db.commit()
        self.run_sync()
        it = self.item("unmapped_designation", key(8, P1))
        self.assertEqual(it.payload["reason"], "role_deleted")

    def test_dont_sync_is_quiet_and_never_creates(self) -> None:
        run = self.run_sync()
        self.assertEqual(self.pr_logs(run, "skipped_dont_sync"), [key(7, P1)])
        self.assertIsNone(self.record(key(7, P1)).base)
        self.assertEqual(self.items(rid=key(7, P1)), [])

    def test_dont_sync_on_a_given_pair_is_a_removal(self) -> None:
        self.run_sync()
        a = self.managed(key(1, P1)).id
        self.set_designation(1, P1, "Advisor")
        run = self.run_sync()
        it = self.item("assignment_removed", key(1, P1))
        self.assertEqual((it.payload["reason"], it.payload["assignment"]["assignment_id"]), ("dont_sync", a))
        self.assertEqual(allowed(self, it), ["remove", "keep"])
        self.assertIn((self.member_role, self.sid(P1), "airtable_sync"), self.assignments(self.ada))  # nothing removed silently
        self.assertEqual(self.pr(run)["removals"], 1)
        # Refreshed, not superseded, on the next run.
        self.run_sync()
        self.assertEqual(self.item("assignment_removed", key(1, P1)).id, it.id)
        self.resolved_ok(it.id, "remove")
        self.assertNotIn((self.member_role, self.sid(P1), "airtable_sync"), self.assignments(self.ada))
        self.assertIsNone(self.record(key(1, P1)).base)
        run = self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertIn(key(1, P1), self.pr_logs(run, "skipped_dont_sync"))

    def test_dont_sync_keep_hands_the_row_over(self) -> None:
        self.run_sync()
        self.set_designation(1, P1, "Advisor")
        self.run_sync()
        self.resolved_ok(self.item("assignment_removed", key(1, P1)).id, "keep")
        self.assertIn((self.member_role, self.sid(P1), "manual"), self.assignments(self.ada))
        self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertIn((self.member_role, self.sid(P1), "manual"), self.assignments(self.ada))
        # Mapped again: A4 adopts the identical manual row.
        self.set_designation(1, P1, "Member")
        run = self.run_sync()
        self.assertEqual(self.pr_logs(run, "assignment_adopted"), [key(1, P1)])
        self.assertIn((self.member_role, self.sid(P1), "airtable_sync"), self.assignments(self.ada))


# ---------------------------------------------------------
# A4: three-way per pair
# ---------------------------------------------------------


class TestThreeWay(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.scopes_only()
        self.p1, self.p2 = self.sid(P1), self.sid(P2)

    def test_first_link_adopts_an_identical_row_of_any_source(self) -> None:
        manual = self.add_assignment(self.ada, self.member_role, self.p1, "manual")
        default = self.add_assignment(self.cy, self.member_role, self.p2, "default")
        other = self.add_assignment(self.ada, self.member_role, self.p2, "manual")  # not what Airtable says
        run = self.run_sync()
        self.assertEqual(sorted(self.pr_logs(run, "assignment_adopted")), [key(1, P1), key(3, P2)])
        self.assertEqual(self.managed(key(1, P1)).id, manual)
        self.assertEqual(self.managed(key(3, P2)).id, default)
        self.assertEqual(self.managed(key(3, P2)).source, "airtable_sync")
        log = self.logs(run, "assignment_adopted")[0]
        self.assertEqual(log.old_value, "manual")
        self.assertEqual(self.db.get(RoleAssignmentV2, other).source, "manual")  # untouched
        self.assertEqual(self.pr(run)["adopted"], 2)

    def test_designation_changed_replaces(self) -> None:
        self.run_sync()
        old = self.managed(key(1, P1)).id
        self.set_designation(1, P1, "Program Lead")
        run = self.run_sync()
        self.assertEqual(self.pr_logs(run, "assignment_replaced"), [key(1, P1)])
        self.assertIsNone(self.db.get(RoleAssignmentV2, old))
        self.assertEqual(self.managed(key(1, P1)).role_id, self.lead_role)
        self.assertEqual(self.record(key(1, P1)).base["designation"], "Program Lead")
        self.assertEqual(
            self.assignments(self.ada), {(self.lead_role, self.p1, "airtable_sync"), (self.lead_role, self.p2, "airtable_sync")}
        )

    def test_replace_onto_an_existing_manual_row_adopts_it(self) -> None:
        self.run_sync()
        old = self.managed(key(1, P1)).id
        manual = self.add_assignment(self.ada, self.lead_role, self.p1, "manual")
        self.set_designation(1, P1, "Program Lead")
        run = self.run_sync()
        self.assertIsNone(self.db.get(RoleAssignmentV2, old))
        self.assertEqual(self.managed(key(1, P1)).id, manual)
        self.assertEqual(self.managed(key(1, P1)).source, "airtable_sync")
        replaced = self.logs(run, "assignment_replaced")[0]
        self.assertIn('"adopted": true', replaced.new_value)

    def test_hub_edited_is_kept(self) -> None:
        self.run_sync()
        a = self.managed(key(1, P1)).id
        self.edit_row(a, role_id=self.fellow_role)
        run = self.run_sync()
        self.assertEqual(self.pr_logs(run, "hub_differs"), [key(1, P1)])
        self.assertEqual(self.db.get(RoleAssignmentV2, a).role_id, self.fellow_role)
        self.assertEqual(self.items("assignment_conflict"), [])

    def test_hub_removed_is_not_re_added(self) -> None:
        self.run_sync()
        self.delete_row(self.managed(key(1, P1)).id)
        run = self.run_sync()
        self.assertEqual(self.pr_logs(run, "hub_differs"), [key(1, P1)])
        self.assertNotIn((self.member_role, self.p1, "airtable_sync"), self.assignments(self.ada))
        self.assertEqual(self.items(), [i for i in self.items() if i.airtable_record_id != key(1, P1)])

    def test_both_changed_is_a_conflict(self) -> None:
        self.run_sync()
        a = self.managed(key(1, P1)).id
        self.edit_row(a, role_id=self.fellow_role)
        self.set_designation(1, P1, "Program Lead")
        run = self.run_sync()
        it = self.item("assignment_conflict", key(1, P1))
        self.assertEqual(it.payload["base"]["role_id"], self.member_role)
        self.assertEqual(it.payload["airtable"]["role_id"], self.lead_role)
        self.assertEqual(it.payload["hub"]["role_id"], self.fellow_role)
        self.assertEqual(allowed(self, it), ["accept_airtable", "keep_current"])
        self.assertEqual(self.db.get(RoleAssignmentV2, a).role_id, self.fellow_role)  # held
        self.assertEqual(self.record(key(1, P1)).base["role_id"], self.member_role)
        self.assertEqual(self.pr(run)["held"], 4)

    def test_hub_removed_and_airtable_changed_is_a_conflict(self) -> None:
        self.run_sync()
        self.delete_row(self.managed(key(1, P1)).id)
        self.set_designation(1, P1, "Program Lead")
        self.run_sync()
        self.assertIsNone(self.item("assignment_conflict", key(1, P1)).payload["hub"])

    def test_both_changed_to_the_same_pair_moves_the_base(self) -> None:
        self.run_sync()
        self.edit_row(self.managed(key(1, P1)).id, role_id=self.lead_role)
        self.set_designation(1, P1, "Program Lead")
        self.run_sync()
        self.assertEqual(self.items("assignment_conflict"), [])
        self.assertEqual(self.record(key(1, P1)).base["role_id"], self.lead_role)

    def test_the_default_pair_is_untouched_by_real_roles(self) -> None:
        # Gus with no usable rows → no_program_roles → Assign default.
        self.drop_pair(7, P2)
        self.run_sync()
        it = self.item("no_program_roles", U(7), entity="user")
        self.resolved_ok(it.id, "assign_default")
        gus = self.uid(7)
        default = (self.user_role, self.hub_scope, "airtable_sync")
        self.assertEqual(self.assignments(gus), {default})
        # A manual (User, Hub) on Ada stays as it is, too.
        self.add_assignment(self.ada, self.user_role, self.hub_scope, "default")
        self.add_pair(7, P2)
        self.run_sync()
        self.assertEqual(self.assignments(gus), {default, (self.member_role, self.p2, "airtable_sync")})
        self.assertIn((self.user_role, self.hub_scope, "default"), self.assignments(self.ada))
        self.drop_pair(7, P2)
        self.run_sync()
        self.assertIn(default, self.assignments(gus))  # never silently removed


# ---------------------------------------------------------
# Pairs gone from Airtable
# ---------------------------------------------------------


class TestRemoved(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_sync()
        self.p1, self.p2 = self.sid(P1), self.sid(P2)

    def test_gone_pair_raises_assignment_removed(self) -> None:
        a = self.managed(key(1, P2)).id
        self.drop_pair(1, P2)
        run = self.run_sync()
        it = self.item("assignment_removed", key(1, P2))
        self.assertEqual((it.payload["reason"], it.payload["assignment"]["assignment_id"], it.hub_id), ("gone", a, self.ada))
        self.assertEqual(it.subject, "Ada · Lone Program")
        self.assertIn((self.lead_role, self.p2, "airtable_sync"), self.assignments(self.ada))
        self.assertEqual(self.pr(run)["removals"], 1)
        self.run_sync()
        self.assertEqual(self.item("assignment_removed", key(1, P2)).id, it.id)  # refreshed

    def test_back_in_airtable_closes_it(self) -> None:
        self.drop_pair(1, P2)
        self.run_sync()
        self.add_pair(1, P2, "Program Lead")
        self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])

    def test_admin_removed_row_is_retired_quietly(self) -> None:
        self.delete_row(self.managed(key(1, P2)).id)
        self.drop_pair(1, P2)
        run = self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertEqual(self.pr_logs(run, "pair_retired"), [key(1, P2)])
        self.assertIsNone(self.record(key(1, P2)).base)
        # A return is a first link: given again.
        self.add_pair(1, P2, "Program Lead")
        run = self.run_sync()
        self.assertEqual(self.pr_logs(run, "assignment_created"), [key(1, P2)])

    def test_not_for_a_person_who_left_users(self) -> None:
        self.drop_user(1)
        self.drop_pair(1, P2)
        self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertEqual(len(self.items("user_deleted", entity="user")), 1)

    def test_not_for_a_held_person_and_carried(self) -> None:
        self.drop_pair(1, P2)
        self.run_sync()
        it = self.item("assignment_removed", key(1, P2))
        self.at_user(1).pop("Work Email")
        self.run_sync()
        self.assertEqual(self.item("assignment_removed", key(1, P2)).id, it.id)  # carried

    def test_not_for_an_unlinked_person(self) -> None:
        self.db.expire_all()
        self.db.get(UserV2, self.ada).airtable_record_id = None
        self.db.commit()
        self.drop_user(1)  # stays out, so the Users step can't relink Ada
        self.drop_pair(1, P2)
        self.run_sync(("program_roles",))
        self.assertEqual(self.items("assignment_removed"), [])

    def test_remove(self) -> None:
        a = self.managed(key(1, P2)).id
        self.drop_pair(1, P2)
        self.run_sync()
        out = self.resolved_ok(self.item("assignment_removed", key(1, P2)).id, "remove")
        self.assertTrue(out["outcome"]["applied"])
        self.assertIsNone(self.db.get(RoleAssignmentV2, a))
        rec = self.record(key(1, P2))
        self.assertEqual((rec.base, rec.managed_assignment_id), (None, None))
        logs = self.logs(None)
        self.assertIn("assignment_removed", [l.action for l in logs])
        self.assertTrue(all(l.actor_email == ADMIN_EMAIL and l.resolution_item_id for l in logs))
        run = self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertNotIn("removals", self.pr(run))

    def test_remove_goes_through_the_last_admin_guard(self) -> None:
        self.drop_pair(1, P2)
        self.run_sync()
        it = self.item("assignment_removed", key(1, P2))
        with mock.patch(
            "app.services.rbac_assignment_service.delete_assignment",
            side_effect=RbacGraphError("last_hub_admin", "The last Hub Admin can't be removed."),
        ) as guard:
            res = self.resolve(it.id, "remove")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("last_hub_admin", res.text)
        guard.assert_called_once()
        self.assertEqual(self.item("assignment_removed", key(1, P2)).id, it.id)  # still open, nothing kept

    def test_keep_becomes_manual(self) -> None:
        a = self.managed(key(1, P2)).id
        self.drop_pair(1, P2)
        self.run_sync()
        self.resolved_ok(self.item("assignment_removed", key(1, P2)).id, "keep")
        self.assertEqual(self.db.get(RoleAssignmentV2, a).source, "manual")
        self.run_sync()
        self.assertEqual(self.items("assignment_removed"), [])
        self.assertEqual(self.db.get(RoleAssignmentV2, a).source, "manual")

    def test_answer_waits_while_the_person_is_held_then_applies(self) -> None:
        a = self.managed(key(1, P2)).id
        self.drop_pair(1, P2)
        self.run_sync()
        it = self.item("assignment_removed", key(1, P2))
        self.at_user(1).pop("Work Email")
        self.run_sync(("users",))
        out = self.resolved_ok(it.id, "remove")
        self.assertEqual(out["outcome"], {"applied": False, "waiting_on": ["no_work_email"]})
        self.assertIsNotNone(self.db.get(RoleAssignmentV2, a))
        self.at_user(1)["Work Email"] = "ada@renphil.org"
        run = self.run_sync()
        self.assertIsNone(self.db.get(RoleAssignmentV2, a))  # the remembered Remove, applied by the step
        self.assertEqual(self.pr_logs(run, "assignment_removed"), [key(1, P2)])
        self.assertEqual(self.items("assignment_removed"), [])


# ---------------------------------------------------------
# Resolutions
# ---------------------------------------------------------


class TestResolutions(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_sync()
        self.p1 = self.sid(P1)
        self.a = self.managed(key(1, P1)).id
        self.edit_row(self.a, role_id=self.fellow_role)
        self.set_designation(1, P1, "Program Lead")
        self.run_sync()
        self.conflict = self.item("assignment_conflict", key(1, P1))

    def test_accept_airtable(self) -> None:
        out = self.resolved_ok(self.conflict.id, "accept_airtable")
        self.assertTrue(out["outcome"]["applied"])
        self.assertIsNone(self.db.get(RoleAssignmentV2, self.a))
        self.assertEqual(self.managed(key(1, P1)).role_id, self.lead_role)
        self.assertEqual(self.record(key(1, P1)).base["role_id"], self.lead_role)
        self.assertEqual([l.action for l in self.logs(None)], ["assignment_replaced", "item_resolved"])
        run = self.run_sync()
        self.assertEqual(self.items("assignment_conflict"), [])
        self.assertNotIn("hub_differs", self.pr(run))

    def test_keep_current(self) -> None:
        out = self.resolved_ok(self.conflict.id, "keep_current")
        self.assertTrue(out["outcome"]["applied"])
        self.assertEqual(self.db.get(RoleAssignmentV2, self.a).role_id, self.fellow_role)
        self.assertEqual(self.record(key(1, P1)).base["role_id"], self.lead_role)  # M3
        run = self.run_sync()
        self.assertEqual(self.items("assignment_conflict"), [])
        self.assertEqual(self.pr_logs(run, "hub_differs"), [key(1, P1)])

    def test_accept_waits_while_the_person_is_held_then_the_step_applies_it(self) -> None:
        self.at_user(1).pop("Work Email")
        self.run_sync()
        out = self.resolved_ok(self.conflict.id, "accept_airtable")
        self.assertEqual(out["outcome"], {"applied": False, "waiting_on": ["no_work_email"]})
        self.assertEqual(self.db.get(RoleAssignmentV2, self.a).role_id, self.fellow_role)
        self.at_user(1)["Work Email"] = "ada@renphil.org"
        run = self.run_sync()
        self.assertEqual(self.managed(key(1, P1)).role_id, self.lead_role)
        self.assertEqual(self.pr(run)["items_remembered"], 1)
        self.assertEqual(self.items("assignment_conflict"), [])

    def test_unmapped_skip_is_remembered_and_holds(self) -> None:
        it = self.item("unmapped_designation", key(8, P1))
        self.resolved_ok(it.id, "skip")
        run = self.run_sync()
        self.assertEqual(self.items("unmapped_designation"), [])
        self.assertEqual(self.pr(run)["items_remembered"], 1)
        self.assertIn(key(8, P1), self.pr_logs(run, "held"))
        # A new Designation is a new question.
        self.set_designation(8, P1, "Sorcerer")
        self.run_sync()
        self.assertEqual(len(self.items("unmapped_designation")), 1)

    def test_no_choice_kinds_and_bad_choices(self) -> None:
        for kind, rid in (("missing_program", key(6, P_MISSING)), ("duplicate_program_role", key(9, P1))):
            res = self.resolve(self.item(kind, rid).id, "skip")
            self.assertEqual(res.status_code, 409, res.text)
            self.assertIn("item_has_no_choices", res.text)
        res = self.resolve(self.conflict.id, "remove")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("invalid_choice", res.text)

    def test_409_during_a_run(self) -> None:
        self.db.add(AirtableSyncRunV2(steps=["program_roles"], trigger="manual", status="running", started_at=_now(), heartbeat_at=_now()))
        self.db.commit()
        res = self.resolve(self.conflict.id, "accept_airtable")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertIn("run_in_progress", res.text)
        self.assertEqual(self.item("assignment_conflict", key(1, P1)).status, "open")

    def test_hub_admins_only(self) -> None:
        member = self.auth(MEMBER_EMAIL)
        self.assertEqual(self.client.get("/v2/airtable-sync/items", params={"entity": "program_role"}, headers=member).status_code, 403)
        self.assertEqual(self.resolve(self.conflict.id, "keep_current", email=MEMBER_EMAIL).status_code, 403)
        res = self.client.get("/v2/airtable-sync/items", params={"entity": "program_role"}, headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(len(res.json()["data"]["items"]), 4)

    def test_user_deleted_remove_retires_the_pairs(self) -> None:
        self.resolved_ok(self.conflict.id, "accept_airtable")
        self.drop_user(1)
        self.run_sync()
        self.resolved_ok(self.item("user_deleted", U(1), entity="user").id, "remove_synced_access")
        self.assertEqual(self.assignments(self.ada), set())
        self.assertIsNone(self.record(key(1, P1)).base)
        # Ada returns: linked again by email, her pairs given afresh.
        self.tables[T_USERS]["records"].insert(0, person(1, "Ada", "ada@renphil.org"))
        run = self.run_sync()
        self.assertEqual(sorted(self.pr_logs(run, "assignment_created")), [key(1, P1), key(1, P2)])

    def test_user_deleted_keep_then_return_adopts(self) -> None:
        self.resolved_ok(self.conflict.id, "accept_airtable")
        self.drop_user(1)
        self.run_sync()
        self.resolved_ok(self.item("user_deleted", U(1), entity="user").id, "keep")
        before = self.assignments(self.ada)
        self.assertTrue(all(src == "airtable_sync" for _r, _s, src in before))
        self.tables[T_USERS]["records"].insert(0, person(1, "Ada", "ada@renphil.org"))
        run = self.run_sync()
        self.assertEqual(sorted(self.pr_logs(run, "assignment_adopted")), [key(1, P1), key(1, P2)])
        self.assertEqual(self.assignments(self.ada), before)


# ---------------------------------------------------------
# Preview
# ---------------------------------------------------------


class TestPreview(SyncFixture):
    def test_full_preview_writes_only_log_rows(self) -> None:
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        s = self.pr(run)
        self.assertEqual(s["created"], 6)
        created = {l.airtable_record_id: l for l in self.logs(run, "assignment_created") if l.entity == "program_role"}
        self.assertEqual(set(created), {key(1, P1), key(1, P2), key(2, P1), key(3, P2), key(7, P2), key(10, P_ALIAS)})
        self.assertEqual(created[key(1, P1)].hub_id, self.ada)  # a would-be link
        self.assertIsNone(created[key(2, P1)].hub_id)  # a would-be user
        self.assertIn('"scope": "Space"', created[key(10, P_ALIAS)].new_value)  # the alias's would-be cluster
        self.assertIn('"scope": "Space Science"', created[key(1, P1)].new_value)
        would = sorted((l.action, l.airtable_record_id) for l in self.logs(run) if l.action.startswith("would_raise_") and l.entity == "program_role")
        self.assertEqual(
            would,
            [
                ("would_raise_duplicate_program_role", key(9, P1)),
                ("would_raise_missing_program", key(6, P_MISSING)),
                ("would_raise_unmapped_designation", key(8, P1)),
            ],
        )

    def test_preview_after_a_real_run(self) -> None:
        self.run_sync()
        old = self.managed(key(1, P1)).id
        self.set_designation(1, P1, "Program Lead")
        self.drop_pair(1, P2)
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual(self.pr_logs(run, "assignment_replaced"), [key(1, P1)])
        self.assertIn(("would_raise_assignment_removed", key(1, P2)), {(l.action, l.airtable_record_id) for l in self.logs(run)})
        self.assertIsNotNone(self.db.get(RoleAssignmentV2, old))
        self.assertEqual(self.items("assignment_removed"), [])


if __name__ == "__main__":
    unittest.main()
