"""Airtable sync, phase 2 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §12;
v1 plan §4, §5.1): the runner — the run-row lock, steps in the fixed order,
the four-table snapshot with fail-fast field checks, preview, heartbeat and
stale-run interruption — plus the runs / log / status endpoints and the
normalization helpers.

In-memory SQLite like test_airtable_sync_phase1.py, but every session is
closed. Airtable is never called: the runner gets a fake source shaped like
the 2026-10-21 survey (plan v2 §2), fake settings, and an inline executor so
a run finishes inside the POST that starts it.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import jwt
import requests
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase2-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.airtable_sync import (  # noqa: E402
    AirtableSyncItemV2,
    AirtableSyncLogV2,
    AirtableSyncRunV2,
)
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.airtable_sync import airtable_reader, normalize, runner as runner_module  # noqa: E402
from app.services.airtable_sync.airtable_reader import (  # noqa: E402
    AirtableReadError,
    AirtableSyncConfigError,
    build_config,
    read_snapshot,
)
from app.services.airtable_sync.runner import STALE_AFTER, StartedRun, SyncRunner, get_sync_runner  # noqa: E402
from app.services.airtable_sync.steps import STEP_FUNCTIONS  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"
OTHER_ADMIN_EMAIL = "other.admin@renphil.org"
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
    # Two HR fields configured and present, one configured but absent from
    # Airtable (like "Start Date" vs "StartDate" on 2026-10-22), the rest unset.
    "USERS_DEPARTMENT_FIELD": "Department",
    "USERS_EMPLOYMENT_TYPE_FIELD": "Employment Type",
    "USERS_START_DATE_FIELD": "Start Date",
    "AIRTABLE_SYNC_PROGRAM_ROLES_TABLE": T_ROLES,
    "AIRTABLE_SYNC_PROGRAM_ROLES_EMPLOYEE_RECORD_ID_FIELD": "Employee Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_PROGRAM_RECORD_ID_FIELD": "Original Program Record ID",
    "AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD": "Designation",
}


def _rec(api_id: str, **fields) -> dict:
    return {"id": api_id, "fields": fields}


def airtable_tables() -> dict:
    """Shaped like plan v2 §2: copied Record ID columns that differ from the
    API ids, a multiline "Record Id" with a trailing newline, a program in
    two clusters, a program that is its cluster (no Cluster Record Id)."""
    return {
        T_CLUSTERS: {
            "fields": {"Name", "Record Id", "Related Funds/Programs", "Cluster Reporting Lead"},
            "records": [
                _rec("recAPIc1", **{"Name": "Space", "Record Id": "rec7Be3xDg3nL8W6Z\n"}),
                _rec("recAPIc2", **{"Name": "AI for Science", "Record Id": "recCLAIS"}),
                _rec("recAPIc3", **{"Name": " Climate ", "Record Id": "recCLCLI"}),
            ],
        },
        T_PROGRAMS: {
            "fields": {"Name", "Record ID", "Cluster Record Id", "Program Cluster", "Sub-Track Of"},
            "records": [
                _rec("recAPIp1", **{"Name": "Space Science", "Record ID": "recP1", "Cluster Record Id": "rec7Be3xDg3nL8W6Z"}),
                _rec(
                    "recAPIp2",
                    **{
                        "Name": "Tertiary Impact Capital",
                        "Record ID": "recP2",
                        "Cluster Record Id": "recCLCLI, recCLAIS",
                        "Program Cluster": ["recAPIc3", "recAPIc2"],
                    },
                ),
                _rec("recAPIp3", **{"Name": "AI for Science", "Record ID": "recD9C4MEUFblmzy7"}),
                _rec("recAPIp4", **{"Name": "AI for Math", "Record ID": "recP4", "Cluster Record Id": "recCLAIS"}),
            ],
        },
        T_USERS: {
            "fields": {"Name", "Record ID", "Work Email", "Alias Email", "Department", "Employment Type", "StartDate"},
            "records": [
                _rec("recAPIu1", **{"Name": "Ada Lovelace", "Record ID": "recU1", "Work Email": " Ada@RenPhil.org "}),
                _rec("recAPIu2", **{"Name": "Bob", "Record ID": "recU2", "Department": "Studio Team"}),
                _rec("recAPIu3", **{"Name": "Cy", "Record ID": "recU3", "Employment Type": ["Full-time", "Fellow"]}),
                _rec("recAPIu4", **{"Name": "Di", "Record ID": "recU4", "Work Email": "di@renphil.org"}),
                _rec("recAPIu5", **{"Name": "Ed", "Record ID": "recU5", "StartDate": "2024-01-15"}),
            ],
        },
        T_ROLES: {
            "fields": {"Name", "Employee Record ID", "Original Program Record ID", "Designation", "Program PIN"},
            "records": [
                _rec("recAPIr1", **{"Employee Record ID": "recU1", "Original Program Record ID": "recP1", "Designation": "Member"}),
                _rec("recAPIr2", **{"Employee Record ID": "recU1", "Original Program Record ID": "recP2", "Designation": "Program Lead"}),
                _rec("recAPIr3", **{"Employee Record ID": "recU2", "Original Program Record ID": "recgyWDVGeX6gSgSN", "Designation": "Member"}),
                _rec("recAPIr4", **{"Employee Record ID": "recNOTINUSERS", "Original Program Record ID": "recP4", "Designation": "Advisor"}),
                _rec("recAPIr5", **{"Employee Record ID": "recU3", "Original Program Record ID": "recD9C4MEUFblmzy7", "Designation": "Member"}),
                _rec("recAPIr6", **{"Employee Record ID": "recU4", "Original Program Record ID": "recP4", "Designation": "Program Designate"}),
            ],
        },
    }


class FakeSource:
    """Airtable as the runner sees it. Records come back with only the asked
    fields, the way `table.all(fields=...)` returns them."""

    def __init__(self, tables: dict, *, fail_on: tuple[str, str] | None = None, error: Exception | None = None):
        self.tables = tables
        self.fail_on = fail_on  # ("schema" | "records", table_id)
        self.error = error
        self.calls: list[tuple] = []

    def _maybe_fail(self, what: str, table_id: str) -> None:
        if self.fail_on == (what, table_id):
            raise self.error

    def field_names(self, table_id: str) -> set[str]:
        self.calls.append(("schema", table_id))
        self._maybe_fail("schema", table_id)
        return set(self.tables[table_id]["fields"])

    def records(self, table_id: str, field_names: list[str]) -> list[dict]:
        self.calls.append(("records", table_id, tuple(field_names)))
        self._maybe_fail("records", table_id)
        return [
            {"id": r["id"], "fields": {k: v for k, v in r["fields"].items() if k in field_names}}
            for r in self.tables[table_id]["records"]
        ]


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(
        f"{status} Client Error: for url: https://api.airtable.com/v0/{BASE}/{T_USERS} token={PAT}",
        response=response,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


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
        self._seed_org()

        self.settings = SimpleNamespace(**SETTINGS)
        self.source = FakeSource(airtable_tables())
        self.step_functions = None  # None → the real (phase 2 stub) steps
        self.executor = lambda fn: fn()

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
            executor=self.executor,
            source_factory=lambda _config: self.source,
            settings_provider=lambda: self.settings,
            step_functions=self.step_functions,
            heartbeat_interval=None,
        )

    def _seed_org(self) -> None:
        db = self.db
        now = _now()
        admin_role = RoleV2(key="hub_admin", name="Admin", is_public=False, is_system=False, created_at=now)
        member_role = RoleV2(key="member", name="Member", is_public=False, is_system=False, created_at=now)
        all_scopes = ScopeV2(key="all_scopes", name="All Scopes", is_universal=True, is_public=False, is_system=False, created_at=now)
        scope_a = ScopeV2(key="scope_a", name="Scope A", is_universal=False, is_public=False, is_system=False, created_at=now)
        db.add_all([admin_role, member_role, all_scopes, scope_a])
        db.flush()
        self.users = {}
        for email in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL, MEMBER_EMAIL):
            user = UserV2(work_email=email, name=email.split("@")[0], has_signed_in=True, created_at=now, updated_at=now)
            db.add(user)
            db.flush()
            self.users[email] = user.id
        for email in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL):
            db.add(RoleAssignmentV2(user_id=self.users[email], role_id=admin_role.id, scope_id=all_scopes.id, created_at=now))
        db.add(RoleAssignmentV2(user_id=self.users[MEMBER_EMAIL], role_id=member_role.id, scope_id=scope_a.id, created_at=now))
        db.commit()

    def auth(self, email: str) -> dict:
        token = jwt.encode(
            {"sub": email, "name": email.split("@")[0], "roles": [], "exp": _now() + timedelta(minutes=30)},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def admin(self) -> dict:
        return self.auth(ADMIN_EMAIL)

    def start(self, steps=("clusters", "programs", "users", "program_roles"), preview=False, email=ADMIN_EMAIL):
        return self.client.post(
            "/v2/airtable-sync/runs", json={"steps": list(steps), "preview": preview}, headers=self.auth(email)
        )

    def run_row(self, run_id: int) -> AirtableSyncRunV2:
        self.db.expire_all()
        return self.db.get(AirtableSyncRunV2, run_id)

    def hub_counts(self) -> dict[str, int]:
        """Row counts of every table except the run and its log: what a
        phase-2 run (and any preview) must leave untouched."""
        self.db.expire_all()
        return {
            t.name: self.db.query(func.count()).select_from(t).scalar()
            for t in BaseV2.metadata.sorted_tables
            # Phase 7: a real run notifies every Hub Admin (owner, 2026-10-24:
            # left out here; tests.test_airtable_sync_phase7 covers them).
            if t.name not in ("airtable_sync_runs", "airtable_sync_log", "notifications")
        }

    def log_count(self) -> int:
        return self.db.query(func.count(AirtableSyncLogV2.id)).scalar()

    def add_run(self, *, status="running", trigger="manual", email=OTHER_ADMIN_EMAIL, started=None, heartbeat=None, **kw):
        started = started or _now()
        run = AirtableSyncRunV2(
            steps=["clusters"],
            trigger=trigger,
            status=status,
            started_by_user_id=self.users.get(email) if email else None,
            started_by_email=email,
            started_at=started,
            heartbeat_at=heartbeat if heartbeat is not None else started,
            **kw,
        )
        self.db.add(run)
        self.db.commit()
        return run


# ---------------------------------------------------------
# Normalization (pure)
# ---------------------------------------------------------


class TestNormalize(unittest.TestCase):
    def test_text_trims_and_blank_is_none(self) -> None:
        self.assertEqual(normalize.text("  Space  "), "Space")
        self.assertEqual(normalize.text("rec7Be3xDg3nL8W6Z\n"), "rec7Be3xDg3nL8W6Z")
        self.assertIsNone(normalize.text("   "))
        self.assertIsNone(normalize.text(None))
        self.assertEqual(normalize.text([" a ", "", "b"]), "a, b")
        self.assertIsNone(normalize.text([]))

    def test_email_is_trimmed_and_lowercased(self) -> None:
        self.assertEqual(normalize.email(" Ada@RenPhil.ORG "), "ada@renphil.org")
        self.assertIsNone(normalize.email(""))

    def test_absent_checkbox_is_false(self) -> None:
        self.assertIs(normalize.checkbox(None), False)
        self.assertIs(normalize.checkbox(True), True)

    def test_dates_are_iso(self) -> None:
        self.assertEqual(normalize.iso_date("2024-01-15"), "2024-01-15")
        self.assertEqual(normalize.iso_date("2024-01-15T22:00:00.000Z"), "2024-01-15")
        self.assertEqual(normalize.iso_date(datetime(2024, 1, 15, 9)), "2024-01-15")
        self.assertIsNone(normalize.iso_date(" "))
        self.assertEqual(normalize.iso_date("soon"), "soon")  # kept, not dropped
        self.assertEqual(normalize.iso_datetime("2024-01-15T10:00:00.000Z"), "2024-01-15T10:00:00Z")
        self.assertEqual(normalize.iso_datetime("2024-01-15T12:00:00+02:00"), "2024-01-15T10:00:00Z")
        self.assertIsNone(normalize.iso_datetime(None))

    def test_multi_select_compares_as_a_set_and_joins_in_order(self) -> None:
        a = normalize.multi(["Full-time", " Fellow ", ""])
        self.assertEqual(a, ["Full-time", "Fellow"])
        self.assertEqual(normalize.multi_key(a), normalize.multi_key(["Fellow", "Full-time", "Fellow"]))
        self.assertEqual(normalize.join_multi(a), "Full-time, Fellow")
        self.assertIsNone(normalize.join_multi([]))
        self.assertEqual(normalize.multi(None), [])
        self.assertEqual(normalize.multi("Solo"), ["Solo"])

    def test_id_list_splits_commas_and_drops_repeats(self) -> None:
        self.assertEqual(normalize.id_list("recA, recB,,recA "), ["recA", "recB"])
        self.assertEqual(normalize.id_list(None), [])

    def test_attachments_keep_id_filename_url(self) -> None:
        value = [{"id": "att1", "filename": "a.png", "url": "https://x", "size": 3}, {"filename": "no id"}]
        self.assertEqual(normalize.attachments(value), [{"id": "att1", "filename": "a.png", "url": "https://x"}])
        self.assertEqual(normalize.attachments(None), [])


# ---------------------------------------------------------
# The snapshot (reader, no database)
# ---------------------------------------------------------


class TestSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(**SETTINGS)

    def test_config_names_every_missing_required_setting(self) -> None:
        settings = SimpleNamespace(**{**SETTINGS, "AIRTABLE_SYNC_CLUSTERS_TABLE": None, "USERS_WORK_EMAIL_FIELD": "  "})
        with self.assertRaises(AirtableSyncConfigError) as ctx:
            build_config(settings)
        self.assertIn("AIRTABLE_SYNC_CLUSTERS_TABLE", str(ctx.exception))
        self.assertIn("USERS_WORK_EMAIL_FIELD", str(ctx.exception))

    def test_config_keeps_the_token_out_of_its_repr(self) -> None:
        self.assertNotIn(PAT, repr(build_config(self.settings)))

    def test_reads_all_four_tables_normalized(self) -> None:
        source = FakeSource(airtable_tables())
        snap = read_snapshot(source, build_config(self.settings))
        self.assertEqual(
            {k: v for k, v in snap.summary().items() if k != "optional_fields_missing"},
            {"clusters": 3, "programs": 4, "users": 5, "program_roles": 6},
        )
        self.assertEqual([c.record_id for c in snap.clusters], ["rec7Be3xDg3nL8W6Z", "recCLAIS", "recCLCLI"])
        self.assertEqual(snap.clusters[2].fields["name"], "Climate")
        tic = snap.programs[1]
        self.assertEqual(tic.api_id, "recAPIp2")
        self.assertEqual(tic.fields["cluster_record_ids"], ["recCLCLI", "recCLAIS"])
        self.assertEqual(tic.fields["cluster_links"], ["recAPIc3", "recAPIc2"])
        self.assertEqual(snap.programs[2].fields["cluster_record_ids"], [])  # the C2 row
        ada = snap.users[0]
        self.assertEqual(ada.fields["work_email"], "ada@renphil.org")
        self.assertIsNone(ada.fields["alias_email"])  # absent cell → present, blank
        self.assertIsNone(ada.fields["department"])
        self.assertEqual(snap.users[2].fields["employment_type"], ["Full-time", "Fellow"])
        self.assertEqual(snap.program_roles[3].fields["employee_record_id"], "recNOTINUSERS")

    def test_every_schema_is_checked_before_any_record_is_read(self) -> None:
        source = FakeSource(airtable_tables())
        read_snapshot(source, build_config(self.settings))
        kinds = [c[0] for c in source.calls]
        self.assertEqual(kinds, ["schema"] * 4 + ["records"] * 4)

    def test_a_missing_required_field_fails_naming_field_and_setting(self) -> None:
        tables = airtable_tables()
        tables[T_CLUSTERS]["fields"].discard("Record Id")
        tables[T_ROLES]["fields"].discard("Designation")
        source = FakeSource(tables)
        with self.assertRaises(AirtableReadError) as ctx:
            read_snapshot(source, build_config(self.settings))
        message = str(ctx.exception)
        self.assertIn("'Record Id' (AIRTABLE_SYNC_CLUSTERS_RECORD_ID_FIELD) in Cluster", message)
        self.assertIn("'Designation' (AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD) in Program Roles", message)
        self.assertFalse([c for c in source.calls if c[0] == "records"])  # nothing fetched

    def test_missing_optional_fields_are_reported_not_fatal(self) -> None:
        source = FakeSource(airtable_tables())
        snap = read_snapshot(source, build_config(self.settings))
        missing = snap.optional_fields_missing
        self.assertIn("USERS_START_DATE_FIELD ('Start Date' not in Users)", missing)
        self.assertIn("AIRTABLE_SYNC_USERS_IS_FELLOW_FIELD (not set)", missing)
        self.assertNotIn("start_date", snap.users[4].fields)  # not read at all
        users_call = next(c for c in source.calls if c[:2] == ("records", T_USERS))
        self.assertNotIn("Start Date", users_call[2])
        self.assertIn("Department", users_call[2])

    def test_http_error_is_short_and_has_no_token(self) -> None:
        source = FakeSource(airtable_tables(), fail_on=("records", T_USERS), error=http_error(401))
        with self.assertLogs("app.services.airtable_sync.airtable_reader", "WARNING") as logs:
            with self.assertRaises(AirtableReadError) as ctx:
                read_snapshot(source, build_config(self.settings))
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("Users", str(ctx.exception))
        self.assertNotIn(PAT, str(ctx.exception))
        self.assertNotIn(PAT, "\n".join(logs.output))

    def test_unknown_table_id_is_named(self) -> None:
        source = FakeSource(airtable_tables(), fail_on=("schema", T_PROGRAMS), error=KeyError(T_PROGRAMS))
        with self.assertRaises(AirtableReadError) as ctx:
            read_snapshot(source, build_config(self.settings))
        self.assertIn("AIRTABLE_SYNC_PROGRAMS_TABLE", str(ctx.exception))


# ---------------------------------------------------------
# Starting runs: steps, lifecycle, preview
# ---------------------------------------------------------


class TestStartRun(SyncFixture):
    def test_steps_are_validated(self) -> None:
        for body in (
            {"steps": [], "preview": False},
            {"steps": ["clusters", "scopes"]},
            {"steps": ["clusters"], "preview": "yes"},
            {"steps": ["clusters"], "mode": "both"},
            {"preview": True},
        ):
            res = self.client.post("/v2/airtable-sync/runs", json=body, headers=self.admin)
            self.assertEqual(res.status_code, 422, body)
        self.assertEqual(self.db.query(AirtableSyncRunV2).count(), 0)

    def test_steps_run_in_the_fixed_order_whatever_the_request_order(self) -> None:
        called = []

        def recorder(name):
            return lambda ctx: called.append(name) or {"seen": name}

        self.step_functions = {s: recorder(s) for s in STEP_FUNCTIONS}
        res = self.start(steps=["program_roles", "users", "clusters", "program_roles"])
        self.assertEqual(res.status_code, 202, res.text)
        run = self.run_row(res.json()["data"]["run_id"])
        self.assertEqual(run.steps, ["clusters", "users", "program_roles"])
        self.assertEqual(called, ["clusters", "users", "program_roles"])
        self.assertEqual(list(run.summary["steps"]), ["clusters", "users", "program_roles"])

    def test_full_run_lifecycle_and_summary(self) -> None:
        # Pinned to phase-2-style stubs (owner, 2026-10-22): this test is about
        # the lifecycle and summary shape; phase 3's real Clusters/Programs
        # steps write scopes and are covered by test_airtable_sync_phase3.
        # Phase 4's real Users step (users) likewise: test_airtable_sync_phase4.
        # Phase 5's real Program Roles step likewise: test_airtable_sync_phase5.
        def stub(table):
            return lambda ctx: {"records": len(getattr(ctx.snapshot, table)), "stub": True}

        self.step_functions = {
            **STEP_FUNCTIONS,
            "clusters": stub("clusters"),
            "programs": stub("programs"),
            "users": stub("users"),
            "program_roles": stub("program_roles"),
        }
        before = self.hub_counts()
        res = self.start()
        self.assertEqual(res.status_code, 202, res.text)
        run = self.run_row(res.json()["data"]["run_id"])
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.trigger, "manual")
        self.assertEqual(run.started_by_email, ADMIN_EMAIL)
        self.assertEqual(run.started_by_user_id, self.users[ADMIN_EMAIL])
        self.assertIsNone(run.error)
        self.assertIsNotNone(run.finished_at)
        self.assertGreaterEqual(_utc(run.finished_at), _utc(run.started_at))
        self.assertEqual(run.summary["preview"], False)
        read = run.summary["read"]
        self.assertEqual(
            (read["clusters"], read["programs"], read["users"], read["program_roles"]), (3, 4, 5, 6)
        )
        self.assertIn("USERS_START_DATE_FIELD ('Start Date' not in Users)", read["optional_fields_missing"])
        self.assertEqual(run.summary["steps"]["programs"], {"records": 4, "stub": True})
        self.assertEqual(run.summary["steps"]["program_roles"], {"records": 6, "stub": True})
        self.assertNotIn("failed_step", run.summary)
        self.assertEqual(self.hub_counts(), before)  # phase 2 writes nothing else
        self.assertEqual(self.log_count(), 0)

    def test_every_table_is_read_even_when_one_step_is_ticked(self) -> None:
        res = self.start(steps=["users"])
        run = self.run_row(res.json()["data"]["run_id"])
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.summary["read"]["program_roles"], 6)  # v2 R2
        self.assertEqual(list(run.summary["steps"]), ["users"])
        self.assertEqual(len([c for c in self.source.calls if c[0] == "records"]), 4)

    def test_preview_writes_only_its_run_row(self) -> None:
        before = self.hub_counts()
        res = self.start(preview=True)
        self.assertEqual(res.status_code, 202, res.text)
        run = self.run_row(res.json()["data"]["run_id"])
        self.assertEqual((run.trigger, run.status), ("preview", "succeeded"))
        self.assertTrue(run.summary["preview"])
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual(self.db.query(AirtableSyncRunV2).count(), 1)

    def test_a_preview_step_may_log_but_not_write(self) -> None:
        def logs_and_writes(ctx):
            ctx.log("cluster", "scope_created", airtable_record_id="recCLAIS", subject="AI for Science", new_value={"name": "x"})
            if ctx.dry_run:
                ctx.commit()  # the log alone is fine
            ctx.db.add(
                ScopeV2(key="ai_for_science", name="AI for Science", is_universal=False, is_public=False, is_system=False, created_at=_now())
            )
            ctx.commit()
            return {"created": 1}

        self.step_functions = {**STEP_FUNCTIONS, "clusters": logs_and_writes}
        before = self.hub_counts()
        run = self.run_row(self.start(steps=["clusters"], preview=True).json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIn("preview tried to write ScopeV2", run.error)
        self.assertEqual(run.summary["failed_step"], "clusters")
        self.assertEqual(self.hub_counts(), before)
        log = self.db.query(AirtableSyncLogV2).one()
        self.assertEqual((log.run_id, log.entity, log.action), (run.id, "cluster", "scope_created"))
        self.assertEqual(log.new_value, '{"name": "x"}')

        # The same step in a real run writes: the guard is preview-only.
        run = self.run_row(self.start(steps=["clusters"]).json()["data"]["run_id"])
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.summary["steps"]["clusters"], {"created": 1})
        self.assertEqual(self.db.query(ScopeV2).filter_by(key="ai_for_science").count(), 1)
        self.assertEqual(self.log_count(), 2)

    def test_log_entity_must_be_one_of_the_four(self) -> None:
        self.step_functions = {**STEP_FUNCTIONS, "clusters": lambda ctx: ctx.log("run", "started")}
        with self.assertLogs("app.services.airtable_sync.runner", "ERROR"):
            run = self.run_row(self.start(steps=["clusters"]).json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIn("ValueError", run.error)
        self.assertEqual(self.log_count(), 0)

    def test_a_failing_step_keeps_earlier_steps_and_rolls_back_its_own(self) -> None:
        def boom(ctx):
            ctx.log("program", "scope_created", subject="half-done")
            raise RuntimeError(f"kaboom {PAT}")

        def logs(ctx):
            ctx.log("cluster", "scope_created", subject="done")
            return {"ok": 1}

        self.step_functions = {**STEP_FUNCTIONS, "clusters": logs, "programs": boom}
        with self.assertLogs("app.services.airtable_sync.runner", "ERROR"):
            run = self.run_row(self.start(steps=["clusters", "programs", "users"]).json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "Unexpected error (RuntimeError); details are in the server log.")
        self.assertEqual(run.summary["failed_step"], "programs")
        self.assertEqual(run.summary["steps"], {"clusters": {"ok": 1}})
        self.assertEqual([r.subject for r in self.db.query(AirtableSyncLogV2).all()], ["done"])


class TestFailFast(SyncFixture):
    def test_missing_field_fails_before_any_write(self) -> None:
        self.source.tables[T_PROGRAMS]["fields"].discard("Cluster Record Id")
        wrote = []
        self.step_functions = {s: (lambda ctx: wrote.append(1)) for s in STEP_FUNCTIONS}
        before = self.hub_counts()
        run = self.run_row(self.start().json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIn("'Cluster Record Id' (AIRTABLE_SYNC_PROGRAMS_CLUSTER_RECORD_ID_FIELD)", run.error)
        self.assertIsNone(run.summary["read"])
        self.assertEqual(run.summary["steps"], {})
        self.assertEqual(wrote, [])
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual(self.log_count(), 0)
        self.assertIsNotNone(run.finished_at)

    def test_missing_setting_fails_naming_it(self) -> None:
        self.settings.AIRTABLE_SYNC_CLUSTERS_TABLE = None
        run = self.run_row(self.start().json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIn("AIRTABLE_SYNC_CLUSTERS_TABLE", run.error)
        self.assertEqual(self.source.calls, [])  # Airtable never called

    def test_airtable_http_error_fails_without_the_token(self) -> None:
        self.source = FakeSource(airtable_tables(), fail_on=("records", T_ROLES), error=http_error(401))
        with self.assertLogs("app.services.airtable_sync.airtable_reader", "WARNING") as logs:
            run = self.run_row(self.start().json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIn("HTTP 401", run.error)
        self.assertIn("Program Roles", run.error)
        self.assertNotIn(PAT, run.error)
        self.assertNotIn(PAT, str(run.summary))
        self.assertNotIn(PAT, "\n".join(logs.output))

    def test_airtable_unreachable_fails_with_a_short_message(self) -> None:
        self.source = FakeSource(
            airtable_tables(), fail_on=("schema", T_CLUSTERS), error=requests.ConnectionError(f"pool {PAT}")
        )
        with self.assertLogs("app.services.airtable_sync.airtable_reader", "WARNING"):
            run = self.run_row(self.start().json()["data"]["run_id"])
        self.assertEqual(run.status, "failed")
        self.assertIn("Could not reach Airtable (ConnectionError)", run.error)
        self.assertNotIn(PAT, run.error)

    def test_launch_failure_fails_the_run(self) -> None:
        def broken_executor(_fn):
            raise RuntimeError("can't start new thread")

        self.executor = broken_executor
        with self.assertLogs("app.services.airtable_sync.runner", "ERROR"):
            res = self.start()
        self.assertEqual(res.status_code, 202)
        run = self.run_row(res.json()["data"]["run_id"])
        self.assertEqual((run.status, run.error), ("failed", "The run could not be started."))


# ---------------------------------------------------------
# The lock: one run at a time
# ---------------------------------------------------------


class TestLock(SyncFixture):
    def test_the_index_allows_one_running_row_only(self) -> None:
        self.add_run(status="succeeded")
        self.add_run(status="failed")
        self.add_run(status="running")
        self.db.add(AirtableSyncRunV2(steps=["users"], trigger="manual", status="running", started_at=_now()))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_second_start_gets_409_naming_the_running_run(self) -> None:
        busy = self.add_run(trigger="manual", email=OTHER_ADMIN_EMAIL)
        for preview in (False, True):  # previews take the same lock (v1 C1)
            res = self.start(preview=preview)
            self.assertEqual(res.status_code, 409, res.text)
            detail = res.json()["detail"]
            self.assertEqual(detail["code"], "run_in_progress")
            self.assertEqual(detail["run_id"], busy.id)
            self.assertEqual(detail["trigger"], "manual")
            self.assertEqual(detail["started_by_email"], OTHER_ADMIN_EMAIL)
            self.assertIn(OTHER_ADMIN_EMAIL, detail["message"])
            self.assertEqual(datetime.fromisoformat(detail["started_at"]), _utc(busy.started_at))
        self.assertEqual(self.db.query(AirtableSyncRunV2).count(), 1)
        self.assertEqual(self.run_row(busy.id).status, "running")

    def test_a_scheduled_run_is_named_as_such(self) -> None:
        self.add_run(trigger="scheduled", email=None)
        detail = self.start().json()["detail"]
        self.assertEqual((detail["trigger"], detail["started_by_email"]), ("scheduled", None))
        self.assertIn("the scheduler", detail["message"])

    def test_stale_heartbeat_is_interrupted_and_the_new_run_starts(self) -> None:
        old = _now() - STALE_AFTER - timedelta(seconds=5)
        stale = self.add_run(started=old - timedelta(minutes=10), heartbeat=old)
        with self.assertLogs("app.services.airtable_sync.runner", "WARNING"):
            res = self.start()
        self.assertEqual(res.status_code, 202, res.text)
        stale = self.run_row(stale.id)
        self.assertEqual(stale.status, "interrupted")
        self.assertIn("No heartbeat for 5 minutes", stale.error)
        self.assertIsNotNone(stale.finished_at)
        self.assertEqual(self.run_row(res.json()["data"]["run_id"]).status, "succeeded")

    def test_no_heartbeat_at_all_falls_back_to_started_at(self) -> None:
        run = AirtableSyncRunV2(
            steps=["users"], trigger="manual", status="running", started_at=_now() - timedelta(minutes=6)
        )
        self.db.add(run)
        self.db.commit()
        with self.assertLogs("app.services.airtable_sync.runner", "WARNING"):
            self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.run_row(run.id).status, "interrupted")

    def test_a_recent_heartbeat_keeps_an_old_run_alive(self) -> None:
        self.add_run(started=_now() - timedelta(hours=2), heartbeat=_now() - timedelta(minutes=4))
        self.assertEqual(self.start().status_code, 409)

    def test_losing_a_race_on_the_index_is_a_409(self) -> None:
        busy = self.add_run()
        real = runner_module.running_run
        calls = []

        def miss_first_time(db):
            calls.append(1)
            return None if len(calls) == 1 else real(db)

        with mock.patch.object(runner_module, "running_run", side_effect=miss_first_time):
            res = self.start()
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["run_id"], busy.id)
        self.assertEqual(self.db.query(AirtableSyncRunV2).count(), 1)

    def test_finishing_never_overwrites_an_interrupted_row(self) -> None:
        def interrupted_meanwhile(ctx):
            other = self.Session()
            try:
                other.get(AirtableSyncRunV2, ctx.run_id).status = "interrupted"
                other.commit()
            finally:
                other.close()
            return {}

        self.step_functions = {**STEP_FUNCTIONS, "clusters": interrupted_meanwhile}
        with self.assertLogs("app.services.airtable_sync.runner", "WARNING"):
            run = self.run_row(self.start(steps=["clusters"]).json()["data"]["run_id"])
        self.assertEqual(run.status, "interrupted")
        self.assertIsNone(run.finished_at)

    def test_heartbeat_touches_only_a_running_row(self) -> None:
        old = _now() - timedelta(minutes=3)
        running = self.add_run(heartbeat=old)
        done = self.add_run(status="succeeded", heartbeat=old)
        for run in (running, done):
            runner_module._Heartbeat(self.Session, run.id, interval=None).beat()
        self.assertGreater(_utc(self.run_row(running.id).heartbeat_at), old)
        self.assertEqual(_utc(self.run_row(done.id).heartbeat_at), old)

    def test_start_service_rejects_bad_steps(self) -> None:
        runner = self.make_runner()
        for steps in ([], ["nope"]):
            with self.assertRaises(ValueError):
                runner.start(self.db, steps=steps, preview=False)

    def test_scheduled_trigger_and_execute_are_callable_without_http(self) -> None:
        """Phase 9's tick path: start(trigger='scheduled'), commit, launch."""
        runner = self.make_runner()
        started = runner.start(self.db, steps=list(STEP_FUNCTIONS), preview=False, trigger="scheduled")
        self.db.commit()
        self.assertEqual(started, StartedRun(started.run_id, tuple(STEP_FUNCTIONS), False))
        runner.launch(started)
        run = self.run_row(started.run_id)
        self.assertEqual((run.trigger, run.status, run.started_by_email), ("scheduled", "succeeded", None))


# ---------------------------------------------------------
# Reading runs, logs and status
# ---------------------------------------------------------


class TestReadEndpoints(SyncFixture):
    def test_runs_list_newest_first_with_limit(self) -> None:
        t = _now() - timedelta(hours=3)
        a = self.add_run(status="succeeded", started=t)
        b = self.add_run(status="failed", started=t + timedelta(hours=1), error="x")
        c = self.add_run(status="succeeded", trigger="preview", started=t + timedelta(hours=2))
        res = self.client.get("/v2/airtable-sync/runs", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual([r["id"] for r in res.json()["data"]], [c.id, b.id, a.id])
        self.assertTrue(res.json()["data"][0]["preview"])
        res = self.client.get("/v2/airtable-sync/runs?limit=1", headers=self.admin)
        self.assertEqual([r["id"] for r in res.json()["data"]], [c.id])
        self.assertEqual(self.client.get("/v2/airtable-sync/runs?limit=0", headers=self.admin).status_code, 422)

    def test_one_run_and_its_stale_flag(self) -> None:
        old = _now() - timedelta(minutes=6)
        run = self.add_run(started=old, heartbeat=old)
        res = self.client.get(f"/v2/airtable-sync/runs/{run.id}", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual((data["id"], data["status"], data["stale"]), (run.id, "running", True))
        self.assertEqual(data["started_by_email"], OTHER_ADMIN_EMAIL)
        self.assertEqual(self.client.get("/v2/airtable-sync/runs/999", headers=self.admin).status_code, 404)

    def test_poll_a_finished_run(self) -> None:
        run_id = self.start().json()["data"]["run_id"]
        data = self.client.get(f"/v2/airtable-sync/runs/{run_id}", headers=self.admin).json()["data"]
        self.assertEqual((data["status"], data["stale"]), ("succeeded", False))
        self.assertEqual(data["steps"], ["clusters", "programs", "users", "program_roles"])
        self.assertEqual(data["summary"]["read"]["users"], 5)

    def _seed_log(self) -> tuple[int, int]:
        run = self.add_run(status="succeeded")
        other = self.add_run(status="succeeded")
        rows = [
            ("cluster", "scope_created", "Space", None, "Space"),
            ("program", "scope_created", "Space Science", None, "Space Science"),
            ("program", "edge_added", "Tertiary Impact Capital", None, "Climate"),
            ("user", "employee_not_in_users", None, None, None),
            ("program", "scope_renamed", "AI for Math", "AI 4 Math", "AI for 100% Math"),
        ]
        for entity, action, subject, old, new in rows:
            self.db.add(
                AirtableSyncLogV2(
                    run_id=run.id, entity=entity, action=action, subject=subject, old_value=old, new_value=new,
                    airtable_record_id=f"rec_{action}",
                )
            )
        self.db.add(AirtableSyncLogV2(run_id=other.id, entity="cluster", action="scope_created", subject="Other run"))
        self.db.commit()
        return run.id, other.id

    def _log(self, run_id: int, query: str = "") -> dict:
        res = self.client.get(f"/v2/airtable-sync/runs/{run_id}/log{query}", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["data"]

    def test_log_pages_through_every_entry_in_order(self) -> None:
        run_id, _ = self._seed_log()
        seen, cursor, pages = [], None, 0
        while True:
            page = self._log(run_id, f"?limit=2{f'&cursor={cursor}' if cursor else ''}")
            pages += 1
            seen.extend(e["subject"] for e in page["entries"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(seen, ["Space", "Space Science", "Tertiary Impact Capital", None, "AI for Math"])
        self.assertIsNone(self._log(run_id, "?limit=5")["next_cursor"])  # exact fit: no empty extra page

    def test_log_filters(self) -> None:
        run_id, _ = self._seed_log()

        def subjects(query: str) -> list:
            return [e["subject"] for e in self._log(run_id, query)["entries"]]

        self.assertEqual(subjects("?entity=cluster"), ["Space"])
        self.assertEqual(subjects("?action=scope_created"), ["Space", "Space Science"])
        self.assertEqual(subjects("?entity=program&action=scope_created"), ["Space Science"])
        self.assertEqual(subjects("?q=SPACE"), ["Space", "Space Science"])  # case-insensitive subject
        self.assertEqual(subjects("?q=climate"), ["Tertiary Impact Capital"])  # matches new_value
        self.assertEqual(subjects("?q=rec_employee"), [None])  # matches the record id
        self.assertEqual(subjects("?q=100%25"), ["AI for Math"])  # a literal %
        self.assertEqual(subjects("?q=1_0"), [])  # `_` is literal too
        res = self.client.get(f"/v2/airtable-sync/runs/{run_id}/log?entity=run", headers=self.admin)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.client.get("/v2/airtable-sync/runs/999/log", headers=self.admin).status_code, 404)

    def test_status_on_an_empty_database(self) -> None:
        res = self.client.get("/v2/airtable-sync/status", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertIsNone(data["current_run"])
        self.assertIsNone(data["last_run"])
        self.assertEqual((data["open_items"], data["open_items_total"]), ({}, 0))
        self.assertIsNone(data["config_error"])
        self.assertEqual(data["settings"]["frequency"], "daily")

    def test_status_with_runs_and_items(self) -> None:
        t = _now() - timedelta(hours=2)
        self.add_run(status="succeeded", started=t)
        last = self.add_run(status="failed", started=t + timedelta(minutes=30), error="x")
        current = self.add_run(started=_now())
        for kind, status in (("no_work_email", "open"), ("no_work_email", "open"), ("scope_deleted", "open"), ("missing_program", "resolved")):
            self.db.add(
                AirtableSyncItemV2(
                    entity="user", airtable_record_id=f"rec{self.db.query(AirtableSyncItemV2).count()}",
                    kind=kind, fingerprint="f", status=status,
                )
            )
            self.db.commit()
        data = self.client.get("/v2/airtable-sync/status", headers=self.admin).json()["data"]
        self.assertEqual(data["current_run"]["id"], current.id)
        self.assertEqual(data["last_run"]["id"], last.id)
        self.assertEqual(data["open_items"], {"no_work_email": 2, "scope_deleted": 1})
        self.assertEqual(data["open_items_total"], 3)

    def test_status_reports_missing_settings(self) -> None:
        self.settings.AIRTABLE_SYNC_PROGRAMS_TABLE = ""
        data = self.client.get("/v2/airtable-sync/status", headers=self.admin).json()["data"]
        self.assertIn("AIRTABLE_SYNC_PROGRAMS_TABLE", data["config_error"])


# ---------------------------------------------------------
# Access
# ---------------------------------------------------------


class TestAccess(SyncFixture):
    def test_non_admin_is_refused_everywhere(self) -> None:
        run = self.add_run(status="succeeded")
        member = self.auth(MEMBER_EMAIL)
        calls = [
            self.client.post("/v2/airtable-sync/runs", json={"steps": ["users"]}, headers=member),
            self.client.post("/v2/airtable-sync/runs", json={"steps": ["users"], "preview": True}, headers=member),
            self.client.get("/v2/airtable-sync/runs", headers=member),
            self.client.get(f"/v2/airtable-sync/runs/{run.id}", headers=member),
            self.client.get(f"/v2/airtable-sync/runs/{run.id}/log", headers=member),
            self.client.get("/v2/airtable-sync/status", headers=member),
        ]
        self.assertEqual([c.status_code for c in calls], [403] * 6)
        self.assertEqual(self.db.query(AirtableSyncRunV2).count(), 1)
        self.assertEqual(self.source.calls, [])

    def test_anonymous_is_refused(self) -> None:
        for res in (
            self.client.post("/v2/airtable-sync/runs", json={"steps": ["users"]}),
            self.client.get("/v2/airtable-sync/status"),
        ):
            self.assertIn(res.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
