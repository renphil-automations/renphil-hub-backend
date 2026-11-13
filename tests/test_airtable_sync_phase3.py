"""Airtable sync, phase 3 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §3.3,
§4.1, §8 steps 3–4; v1 plan §5.2, K3, S1–S6): the Clusters and Programs
steps — K3 matching, create/link/rename three-way, `scope_deleted`, C2
aliases, A-2, the sync edges' three-way (C3), the text/link mismatch log
(D-6), `scopes.type` — and the items machinery (open / refresh / supersede /
remembered Skip / auto-close).

SQLite in memory with every session closed, like test_airtable_sync_phase2.py
(its FakeSource and fixture are copied, not imported, so the two modules stay
independent). Airtable is never called. Runs go through the real runner with
an inline executor; the Users and Program Roles steps are still stubs.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase3-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.airtable_sync import (  # noqa: E402
    AirtableSyncItemV2,
    AirtableSyncLogV2,
    AirtableSyncRecordV2,
    AirtableSyncRunV2,
)
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.resource_grant import ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.services import rbac_service  # noqa: E402
from app.services.airtable_sync import items as items_module  # noqa: E402
from app.services.airtable_sync import scopes_step  # noqa: E402
from app.services.airtable_sync.runner import SyncRunner  # noqa: E402
from app.services.rbac_graph_service import RbacGraphError, create_scope_edge  # noqa: E402

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

# Cluster "Record Id" values (copied column, ≠ API id).
SPACE, AIS, CLIMATE = "rec7Be3xDg3nL8W6Z", "recCLAIS", "recCLCLI"
P_SPACE_SCI, P_TERTIARY, P_AIS_ALIAS, P_MATH, P_LONE = "recP1", "recP2", "recD9C4MEUFblmzy7", "recP4", "recP5"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cluster(api: str, name: str | None, rid: str | None) -> dict:
    fields = {}
    if name is not None:
        fields["Name"] = name
    if rid is not None:
        fields["Record Id"] = rid
    return {"id": api, "fields": fields}


def program(api: str, name: str | None, rid: str | None, text: str | None = None, links: list[str] | None = None) -> dict:
    fields = {}
    if name is not None:
        fields["Name"] = name
    if rid is not None:
        fields["Record ID"] = rid
    if text is not None:
        fields["Cluster Record Id"] = text
    if links:
        fields["Program Cluster"] = links
    return {"id": api, "fields": fields}


def airtable_tables() -> dict:
    """Shaped like the 2026-10-21 survey (plan v2 §2): a multiline cluster
    "Record Id"; Space Science named by text but not by link (D-6); a program
    in two clusters (D-7); "AI for Science" in both tables, the program row
    without a Cluster Record Id (C2); "AI for Math" already in the Hub."""
    return {
        T_CLUSTERS: {
            "fields": {"Name", "Record Id", "Cluster Reporting Lead"},
            "records": [
                cluster("recAPIc1", "Space", SPACE + "\n"),
                cluster("recAPIc2", "AI for Science", AIS),
                cluster("recAPIc3", " Climate ", CLIMATE),
            ],
        },
        T_PROGRAMS: {
            "fields": {"Name", "Record ID", "Cluster Record Id", "Program Cluster", "Sub-Track Of"},
            "records": [
                program("recAPIp1", "Space Science", P_SPACE_SCI, SPACE),
                program("recAPIp2", "Tertiary Impact Capital", P_TERTIARY, f"{CLIMATE}, {AIS}", ["recAPIc3", "recAPIc2"]),
                program("recAPIp3", "AI for Science", P_AIS_ALIAS),
                program("recAPIp4", "AI for Math", P_MATH, AIS, ["recAPIc2"]),
                program("recAPIp5", "Lone Program", P_LONE),
            ],
        },
        T_USERS: {
            "fields": {"Name", "Record ID", "Work Email", "Alias Email"},
            "records": [{"id": "recAPIu1", "fields": {"Name": "Ada", "Record ID": "recU1", "Work Email": "ada@renphil.org"}}],
        },
        T_ROLES: {
            "fields": {"Employee Record ID", "Original Program Record ID", "Designation"},
            "records": [
                {"id": "recAPIr1", "fields": {"Employee Record ID": "recU1", "Original Program Record ID": P_MATH, "Designation": "Member"}}
            ],
        },
    }


class FakeSource:
    """Airtable as the runner sees it (copied from phase 2)."""

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
        self._seed_org()
        self.settings = SimpleNamespace(**SETTINGS)
        self.tables = airtable_tables()
        self.source = FakeSource(self.tables)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _seed_org(self) -> None:
        db, now = self.db, _now()
        self.member_role = RoleV2(key="member", name="Member", is_public=False, is_system=False, created_at=now)
        db.add(self.member_role)

        def scope(key, name, **kw):
            row = ScopeV2(key=key, name=name, is_universal=kw.pop("universal", False), is_public=kw.pop("public", False), is_system=kw.pop("system", False), created_at=now, **kw)
            db.add(row)
            db.flush()
            return row.id

        self.all_scopes = scope("all_scopes", "All Scopes", universal=True, system=True)
        self.hub_scope = scope("hub", "Hub", public=True)
        self.studio = scope("studio", "Studio")  # hand-made Hub-only scope (S6)
        self.math = scope("ai_for_math", "AI for Math")  # links by name (K3)
        self.user = UserV2(work_email="ada@renphil.org", name="Ada", has_signed_in=True, created_at=now, updated_at=now)
        db.add(self.user)
        db.commit()

    # ── running ──────────────────────────────────────────────────────────

    def run_sync(self, steps=("clusters", "programs"), preview=False) -> AirtableSyncRunV2:
        runner = SyncRunner(
            session_factory=self.Session,
            executor=lambda fn: fn(),
            source_factory=lambda _config: self.source,
            settings_provider=lambda: self.settings,
            heartbeat_interval=None,
        )
        db = self.Session()
        try:
            started = runner.start(db, steps=list(steps), preview=preview, started_by_email="admin@renphil.org")
            db.commit()
        finally:
            db.close()
        runner.launch(started)
        self.db.expire_all()
        run = self.db.get(AirtableSyncRunV2, started.run_id)
        self.assertEqual(run.status, "succeeded", (run.error, run.summary))
        return run

    # ── reading ──────────────────────────────────────────────────────────

    def scope_by_rid(self, rid: str) -> ScopeV2 | None:
        self.db.expire_all()
        return self.db.query(ScopeV2).filter(ScopeV2.airtable_record_id == rid).one_or_none()

    def scope_named(self, name: str) -> ScopeV2 | None:
        self.db.expire_all()
        return self.db.query(ScopeV2).filter(ScopeV2.name == name).one_or_none()

    def record(self, entity: str, rid: str) -> AirtableSyncRecordV2 | None:
        self.db.expire_all()
        return self.db.query(AirtableSyncRecordV2).filter_by(entity=entity, airtable_record_id=rid).one_or_none()

    def logs(self, run: AirtableSyncRunV2, action: str | None = None) -> list[AirtableSyncLogV2]:
        q = self.db.query(AirtableSyncLogV2).filter(AirtableSyncLogV2.run_id == run.id)
        if action:
            q = q.filter(AirtableSyncLogV2.action == action)
        return q.order_by(AirtableSyncLogV2.id).all()

    def items(self, kind: str | None = None, status: str | None = "open") -> list[AirtableSyncItemV2]:
        self.db.expire_all()
        q = self.db.query(AirtableSyncItemV2)
        if kind:
            q = q.filter(AirtableSyncItemV2.kind == kind)
        if status:
            q = q.filter(AirtableSyncItemV2.status == status)
        return q.order_by(AirtableSyncItemV2.id).all()

    def edges(self, child_id: int) -> dict[int, str]:
        self.db.expire_all()
        return {p: s for p, s in self.db.query(ScopeEdgeV2.parent_scope_id, ScopeEdgeV2.source).filter(ScopeEdgeV2.child_scope_id == child_id)}

    def hub_counts(self) -> dict[str, int]:
        self.db.expire_all()
        return {
            t.name: self.db.query(func.count()).select_from(t).scalar()
            for t in BaseV2.metadata.sorted_tables
            if t.name not in ("airtable_sync_runs", "airtable_sync_log")
        }

    # ── editing Airtable / the Hub ───────────────────────────────────────

    def at_record(self, table: str, rid: str) -> dict:
        key = "Record Id" if table == T_CLUSTERS else "Record ID"
        for r in self.tables[table]["records"]:
            if (r["fields"].get(key) or "").strip() == rid:
                return r["fields"]
        raise KeyError(rid)

    def drop_record(self, table: str, rid: str) -> None:
        key = "Record Id" if table == T_CLUSTERS else "Record ID"
        self.tables[table]["records"] = [r for r in self.tables[table]["records"] if (r["fields"].get(key) or "").strip() != rid]

    def rename_in_hub(self, scope_id: int, name: str) -> None:
        self.db.get(ScopeV2, scope_id).name = name
        self.db.commit()


# ---------------------------------------------------------
# Create, link, keys, records, Hub-only scopes
# ---------------------------------------------------------


class TestCreateAndLink(SyncFixture):
    def test_full_run_creates_clusters_and_programs(self) -> None:
        run = self.run_sync()
        space = self.scope_by_rid(SPACE)
        self.assertEqual((space.name, space.key, space.type, space.is_universal), ("Space", "space", "cluster", False))
        self.assertEqual(self.scope_by_rid(CLIMATE).name, "Climate")  # trimmed
        sci = self.scope_by_rid(P_SPACE_SCI)
        self.assertEqual((sci.name, sci.key, sci.type), ("Space Science", "space_science", "program"))
        self.assertEqual(self.scope_by_rid(P_LONE).type, "program")  # S1: no cluster, still a scope
        self.assertEqual(len(self.logs(run, "cluster_created")), 3)
        self.assertEqual(len(self.logs(run, "scope_created")), 3)  # Space Science, Tertiary, Lone
        steps = run.summary["steps"]
        self.assertEqual((steps["clusters"]["created"], steps["programs"]["created"], steps["programs"]["linked"]), (3, 3, 1))
        self.assertEqual(steps["programs"]["aliases"], 1)

    def test_existing_scope_links_by_name_and_gets_its_type(self) -> None:
        run = self.run_sync()
        math = self.db.get(ScopeV2, self.math)
        self.assertEqual((math.airtable_record_id, math.type, math.name, math.key), (P_MATH, "program", "AI for Math", "ai_for_math"))
        self.assertEqual([(l.action, l.hub_id) for l in self.logs(run, "linked")], [("linked", self.math)])
        self.assertEqual(self.record("program", P_MATH).base, {"name": "AI for Math", "clusters": [AIS]})

    def test_name_match_is_trimmed_and_case_insensitive_and_hub_name_kept(self) -> None:
        self.rename_in_hub(self.math, "ai for math")
        run = self.run_sync(steps=["programs"])
        math = self.db.get(ScopeV2, self.math)
        self.assertEqual((math.airtable_record_id, math.name), (P_MATH, "ai for math"))
        differs = self.logs(run, "hub_differs")
        self.assertEqual([(d.field, d.old_value, d.new_value) for d in differs], [("name", "AI for Math", "ai for math")])
        self.assertEqual(self.record("program", P_MATH).base["name"], "AI for Math")

    def test_key_is_a_slug_with_a_suffix_on_clash_and_never_changes(self) -> None:
        now = _now()
        self.db.add(ScopeV2(key="space", name="Outer Space Fund", is_universal=False, is_public=False, is_system=False, created_at=now))
        self.db.commit()
        self.run_sync(steps=["clusters"])
        self.assertEqual(self.scope_by_rid(SPACE).key, "space_2")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Deep Space"
        self.run_sync(steps=["clusters"])
        space = self.scope_by_rid(SPACE)
        self.assertEqual((space.name, space.key), ("Deep Space", "space_2"))

    def test_slugify(self) -> None:
        self.assertEqual(scopes_step.slugify("AI for Science"), "ai_for_science")
        self.assertEqual(scopes_step.slugify(" Clean Énergie -- (UK) "), "clean_energie_uk")
        self.assertEqual(scopes_step.slugify("!!!"), "scope")
        self.assertEqual(len(scopes_step.slugify("x" * 90)), 64)
        index = scopes_step.ScopeIndex([scopes_step.HubScope(id=1, key="x" * 64, name="a", type=None, record_id=None)])
        self.assertEqual(index.unique_key("x" * 90), "x" * 62 + "_2")

    def test_sync_records_hold_base_latest_hub_id_and_run(self) -> None:
        run = self.run_sync()
        rec = self.record("cluster", SPACE)
        self.assertEqual((rec.hub_id, rec.base, rec.last_seen_run_id), (self.scope_by_rid(SPACE).id, {"name": "Space"}, run.id))
        self.assertEqual(rec.latest_airtable, {"record_id": SPACE, "name": "Space"})
        rec = self.record("program", P_TERTIARY)
        self.assertEqual(rec.base, {"name": "Tertiary Impact Capital", "clusters": sorted([AIS, CLIMATE])})
        self.assertEqual(rec.latest_airtable["cluster_record_ids"], [CLIMATE, AIS])
        self.assertEqual(self.db.query(AirtableSyncRecordV2).count(), 3 + 5)

    def test_hub_only_scopes_are_never_touched_or_reported(self) -> None:
        before = {s.id: (s.key, s.name, s.type, s.airtable_record_id, s.updated_at) for s in self.db.query(ScopeV2)}
        self.run_sync()
        self.run_sync()
        for sid in (self.all_scopes, self.hub_scope, self.studio):
            s = self.db.get(ScopeV2, sid)
            self.assertEqual((s.key, s.name, s.type, s.airtable_record_id, s.updated_at), before[sid])
        self.assertEqual(self.items(), [])

    def test_second_identical_run_changes_nothing(self) -> None:
        self.run_sync()
        counts = self.hub_counts()
        run = self.run_sync()
        self.assertEqual(self.hub_counts(), counts)
        actions = {l.action for l in self.logs(run)}
        self.assertEqual(actions, {"program_is_cluster", "cluster_mismatch"})  # standing log lines only
        self.assertEqual(run.summary["steps"]["clusters"]["unchanged"], 3)

    def test_reserved_scopes_never_link_by_name(self) -> None:
        self.at_record(T_CLUSTERS, CLIMATE)["Name"] = "hub"
        self.run_sync(steps=["clusters"])
        self.assertIsNone(self.scope_by_rid(CLIMATE))
        self.assertIsNone(self.db.get(ScopeV2, self.hub_scope).airtable_record_id)
        [item] = self.items("scope_name_taken")
        self.assertEqual((item.entity, item.airtable_record_id, item.payload["taken_by"]["scope_id"]), ("cluster", CLIMATE, self.hub_scope))

    def test_bad_records_are_logged_and_skipped(self) -> None:
        self.tables[T_CLUSTERS]["records"] += [
            cluster("recAPIx1", "No Id", None),
            cluster("recAPIx2", None, "recNONAME"),
            cluster("recAPIx3", "Space Again", SPACE),
        ]
        run = self.run_sync(steps=["clusters"])
        self.assertEqual(
            {l.action for l in self.logs(run) if l.action.startswith("skipped")},
            {"skipped_no_record_id", "skipped_no_name", "skipped_duplicate_record_id"},
        )
        self.assertEqual(self.scope_by_rid(SPACE).name, "Space")
        self.assertEqual(run.summary["steps"]["clusters"]["skipped"], 3)


# ---------------------------------------------------------
# Rename three-way (S3), name taken, ambiguous, type mismatch
# ---------------------------------------------------------


class TestRename(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_sync(steps=["clusters"])
        self.space = self.scope_by_rid(SPACE).id

    def test_airtable_changed_hub_not_renames(self) -> None:
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space & Stars"
        run = self.run_sync(steps=["clusters"])
        self.assertEqual(self.db.get(ScopeV2, self.space).name, "Space & Stars")
        [log] = self.logs(run, "scope_renamed")
        self.assertEqual((log.old_value, log.new_value, log.hub_id), ("Space", "Space & Stars", self.space))
        self.assertEqual(self.record("cluster", SPACE).base, {"name": "Space & Stars"})

    def test_hub_changed_airtable_not_keeps_hub(self) -> None:
        self.rename_in_hub(self.space, "Space (Hub name)")
        run = self.run_sync(steps=["clusters"])
        self.assertEqual(self.db.get(ScopeV2, self.space).name, "Space (Hub name)")
        [log] = self.logs(run, "hub_differs")
        self.assertEqual((log.old_value, log.new_value), ("Space", "Space (Hub name)"))
        self.assertEqual(self.record("cluster", SPACE).base, {"name": "Space"})
        self.assertEqual(self.items(), [])

    def test_both_changed_differently_is_a_conflict(self) -> None:
        self.rename_in_hub(self.space, "Space (Hub)")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space (Airtable)"
        self.run_sync(steps=["clusters"])
        self.assertEqual(self.db.get(ScopeV2, self.space).name, "Space (Hub)")
        [item] = self.items("scope_name_conflict")
        self.assertEqual((item.entity, item.airtable_record_id, item.hub_id), ("cluster", SPACE, self.space))
        self.assertEqual(
            item.payload,
            {"scope_id": self.space, "base": "Space", "airtable": "Space (Airtable)", "hub": "Space (Hub)", "choices": ["accept_airtable", "keep_current"]},
        )
        self.assertEqual(self.record("cluster", SPACE).base, {"name": "Space"})

    def test_both_changed_the_same_way_moves_the_base(self) -> None:
        self.rename_in_hub(self.space, "Space 2.0")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space 2.0"
        run = self.run_sync(steps=["clusters"])
        self.assertEqual(self.record("cluster", SPACE).base, {"name": "Space 2.0"})
        self.assertEqual((self.items(), self.logs(run, "scope_renamed")), ([], []))

    def test_new_name_taken_is_an_item_case_insensitively(self) -> None:
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "STUDIO"
        self.run_sync(steps=["clusters"])
        self.assertEqual(self.db.get(ScopeV2, self.space).name, "Space")
        [item] = self.items("scope_name_taken")
        self.assertEqual(item.payload["reason"], "rename")
        self.assertEqual(item.payload["taken_by"]["scope_id"], self.studio)
        self.assertEqual(item.payload["current_name"], "Space")
        self.assertEqual(self.record("cluster", SPACE).base, {"name": "Space"})

    def test_create_with_a_taken_name_is_an_item(self) -> None:
        self.tables[T_CLUSTERS]["records"].append(cluster("recAPIc9", "space", "recCNEW"))
        self.run_sync(steps=["clusters"])
        self.assertIsNone(self.scope_by_rid("recCNEW"))  # "Space" is linked: never stolen (K3)
        [item] = self.items("scope_name_taken")
        self.assertEqual((item.airtable_record_id, item.payload["reason"], item.payload["taken_by"]["scope_id"]), ("recCNEW", "create", self.space))
        self.assertIsNone(self.record("cluster", "recCNEW").hub_id)

    def test_two_unlinked_candidates_is_ambiguous(self) -> None:
        now = _now()
        for name in ("Ocean", "ocean"):
            self.db.add(ScopeV2(key=name.lower() + name[0], name=name, is_universal=False, is_public=False, is_system=False, created_at=now))
        self.db.commit()
        self.tables[T_CLUSTERS]["records"].append(cluster("recAPIc8", "OCEAN ", "recCOCEAN"))
        self.run_sync(steps=["clusters"])
        [item] = self.items("scope_ambiguous_match")
        self.assertEqual(sorted(c["name"] for c in item.payload["candidates"]), ["Ocean", "ocean"])
        # create_new dropped in phase 4 (owner, 2026-10-22): the name is taken.
        self.assertEqual(item.payload["choices"], ["link", "skip"])
        self.assertIsNone(self.scope_by_rid("recCOCEAN"))
        self.assertEqual(self.db.query(ScopeV2).filter(func.lower(ScopeV2.name) == "ocean").count(), 2)

    def test_linked_scope_of_the_other_type_is_a_mismatch_and_held(self) -> None:
        self.db.get(ScopeV2, self.space).type = "program"
        self.db.commit()
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Renamed"
        self.run_sync(steps=["clusters"])
        space = self.db.get(ScopeV2, self.space)
        self.assertEqual((space.type, space.name), ("program", "Space"))  # never flipped, never renamed
        [item] = self.items("scope_type_mismatch")
        self.assertEqual((item.hub_id, item.payload["expected_type"], item.payload["airtable_table"]), (self.space, "cluster", "Cluster"))

    def test_name_match_of_the_other_type_is_a_mismatch_not_a_link(self) -> None:
        now = _now()
        self.db.add(ScopeV2(key="kept", name="Kept Program", type="program", is_universal=False, is_public=False, is_system=False, created_at=now))
        self.db.commit()
        self.tables[T_CLUSTERS]["records"].append(cluster("recAPIc7", "Kept Program", "recCKEPT"))
        self.run_sync(steps=["clusters"])
        self.assertIsNone(self.scope_by_rid("recCKEPT"))
        [item] = self.items("scope_type_mismatch")
        self.assertEqual(item.airtable_record_id, "recCKEPT")
        self.assertIsNone(self.record("cluster", "recCKEPT").hub_id)

    def test_a_service_refusal_skips_one_scope_only(self) -> None:
        real = rbac_service.create_scope

        def refuse_climate(db, **kw):
            if kw["name"] == "Lone Program":
                raise RbacGraphError("duplicate_name", "refused for the test")
            return real(db, **kw)

        with mock.patch.object(rbac_service, "create_scope", side_effect=refuse_climate):
            run = self.run_sync(steps=["programs"])
        self.assertIsNone(self.scope_by_rid(P_LONE))
        self.assertIsNotNone(self.scope_by_rid(P_SPACE_SCI))
        [log] = self.logs(run, "scope_error")
        self.assertEqual((log.airtable_record_id, log.old_value), (P_LONE, "duplicate_name"))
        self.assertIsNone(self.record("program", P_LONE))
        self.assertEqual(run.summary["steps"]["programs"]["errors"], 1)


# ---------------------------------------------------------
# Deleted from Airtable (S4)
# ---------------------------------------------------------


class TestDeleted(SyncFixture):
    def test_deleted_cluster_and_program_raise_scope_deleted_with_usage(self) -> None:
        self.run_sync()
        climate, lone = self.scope_by_rid(CLIMATE).id, self.scope_by_rid(P_LONE).id
        now = _now()
        self.db.add(RoleAssignmentV2(user_id=self.user.id, role_id=self.member_role.id, scope_id=climate, created_at=now))
        hub = HubV2(created_at=now.replace(tzinfo=None))
        self.db.add(hub)
        self.db.flush()
        self.db.add(ResourceGrantV2(hub_id=hub.id, role_id=self.member_role.id, scope_id=climate, level="view", created_at=now))
        self.db.commit()
        self.drop_record(T_CLUSTERS, CLIMATE)
        self.drop_record(T_PROGRAMS, P_LONE)
        self.at_record(T_PROGRAMS, P_TERTIARY)["Cluster Record Id"] = AIS
        self.run_sync()

        items = {i.airtable_record_id: i for i in self.items("scope_deleted")}
        self.assertEqual(set(items), {CLIMATE, P_LONE})
        used = items[CLIMATE].payload
        self.assertEqual((used["assignment_count"], used["resource_grant_count"]), (1, 1))
        self.assertEqual(used["people"], [{"user_id": self.user.id, "name": "Ada", "email": "ada@renphil.org", "is_deleted": False, "role": "Member"}])
        self.assertEqual(used["choices"], ["keep_hub_only"])
        self.assertEqual(items[CLIMATE].entity, "cluster")
        self.assertEqual(items[P_LONE].payload["choices"], ["delete_scope", "keep_hub_only"])
        self.assertEqual(items[P_LONE].entity, "program")
        # Nothing deleted, record ids kept.
        self.assertEqual(self.db.get(ScopeV2, climate).airtable_record_id, CLIMATE)
        self.assertEqual(self.db.get(ScopeV2, lone).type, "program")

    def test_deleted_item_closes_when_the_record_comes_back(self) -> None:
        self.run_sync(steps=["clusters"])
        saved = list(self.tables[T_CLUSTERS]["records"])
        self.drop_record(T_CLUSTERS, SPACE)
        self.run_sync(steps=["clusters"])
        self.assertEqual(len(self.items("scope_deleted")), 1)
        self.tables[T_CLUSTERS]["records"] = saved
        run = self.run_sync(steps=["clusters"])
        self.assertEqual(self.items("scope_deleted"), [])
        [closed] = self.items("scope_deleted", status="resolved")
        self.assertEqual((closed.resolution, closed.last_run_id), ("auto_closed", run.id))
        self.assertEqual(len(self.logs(run, "item_auto_closed")), 1)


# ---------------------------------------------------------
# C2 aliases and A-2
# ---------------------------------------------------------


class TestClusterAliases(SyncFixture):
    def test_program_row_without_cluster_id_named_like_a_cluster_is_the_cluster(self) -> None:
        run = self.run_sync()
        ais = self.scope_by_rid(AIS)
        self.assertEqual(self.db.query(ScopeV2).filter(func.lower(ScopeV2.name) == "ai for science").count(), 1)
        self.assertEqual(ais.type, "cluster")
        rec = self.record("program", P_AIS_ALIAS)
        self.assertEqual((rec.hub_id, rec.base), (ais.id, {"name": "AI for Science", "is_cluster": True, "cluster_record_id": AIS}))
        [log] = self.logs(run, "program_is_cluster")
        self.assertEqual((log.airtable_record_id, log.hub_id, log.new_value), (P_AIS_ALIAS, ais.id, AIS))
        self.assertIsNone(self.scope_by_rid(P_AIS_ALIAS))

    def test_alias_waits_in_a_programs_only_run_before_clusters_exist(self) -> None:
        run = self.run_sync(steps=["programs"])
        self.assertEqual(len(self.logs(run, "program_is_cluster")), 1)  # detected from the snapshot (R2)
        self.assertIsNone(self.record("program", P_AIS_ALIAS).hub_id)
        self.assertIn(P_AIS_ALIAS, {l.airtable_record_id for l in self.logs(run, "scope_not_synced")})
        self.run_sync()
        self.assertEqual(self.record("program", P_AIS_ALIAS).hub_id, self.scope_by_rid(AIS).id)

    def test_same_name_with_a_cluster_id_is_name_taken(self) -> None:
        self.at_record(T_PROGRAMS, P_AIS_ALIAS)["Cluster Record Id"] = SPACE
        self.run_sync()
        self.assertIsNone(self.scope_by_rid(P_AIS_ALIAS))
        [item] = self.items("scope_name_taken")
        self.assertEqual((item.entity, item.airtable_record_id, item.payload["reason"]), ("program", P_AIS_ALIAS, "cluster_name"))
        self.assertEqual(item.payload["taken_by"], {"kind": "cluster", "cluster_record_id": AIS})
        self.assertEqual(self.scope_by_rid(AIS).type, "cluster")

    def test_a_program_cannot_be_renamed_to_a_cluster_name(self) -> None:
        self.run_sync()
        self.at_record(T_PROGRAMS, P_MATH)["Name"] = "climate"  # has a Cluster Record Id
        self.run_sync(steps=["programs"])
        self.assertEqual(self.scope_by_rid(P_MATH).name, "AI for Math")
        [item] = self.items("scope_name_taken")
        self.assertEqual(item.payload["reason"], "rename")

    def test_a_linked_program_turning_into_a_cluster_alias_is_scope_deleted(self) -> None:
        self.run_sync()
        lone = self.scope_by_rid(P_LONE).id
        self.at_record(T_PROGRAMS, P_LONE)["Name"] = "climate"  # no Cluster Record Id: C2
        run = self.run_sync(steps=["programs"])
        self.assertEqual(self.record("program", P_LONE).hub_id, self.scope_by_rid(CLIMATE).id)
        [item] = self.items("scope_deleted")
        self.assertEqual((item.airtable_record_id, item.hub_id), (P_LONE, lone))
        self.assertEqual(len(self.logs(run, "program_is_cluster")), 2)


# ---------------------------------------------------------
# Edges (C3) and the text/link mismatch (D-6)
# ---------------------------------------------------------


class TestEdges(SyncFixture):
    def ids(self, *rids) -> list[int]:
        return [self.scope_by_rid(r).id for r in rids]

    def test_edges_are_added_as_airtable_sync_with_multiple_parents(self) -> None:
        run = self.run_sync()
        space, ais, climate, sci, tertiary, math = self.ids(SPACE, AIS, CLIMATE, P_SPACE_SCI, P_TERTIARY, P_MATH)
        self.assertEqual(self.edges(sci), {space: "airtable_sync"})
        self.assertEqual(self.edges(tertiary), {climate: "airtable_sync", ais: "airtable_sync"})  # D-7
        self.assertEqual(self.edges(math), {ais: "airtable_sync"})
        self.assertEqual(self.edges(self.scope_by_rid(P_LONE).id), {})
        self.assertEqual(len(self.logs(run, "edge_added")), 4)
        self.assertEqual(run.summary["steps"]["programs"]["edges_added"], 4)

    def test_admin_edges_still_default_to_manual(self) -> None:
        edge = create_scope_edge(self.db, self.studio, self.math)
        self.db.commit()
        self.assertEqual(edge.source, "manual")

    def test_text_link_mismatch_is_a_log_line_only(self) -> None:
        run = self.run_sync()
        [log] = self.logs(run, "cluster_mismatch")
        self.assertEqual((log.airtable_record_id, log.old_value, log.new_value), (P_SPACE_SCI, "[]", f'["{SPACE}"]'))
        self.assertEqual(self.edges(self.scope_by_rid(P_SPACE_SCI).id), {self.scope_by_rid(SPACE).id: "airtable_sync"})  # text wins
        self.assertEqual(self.items(), [])

    def test_link_api_ids_are_mapped_to_record_ids(self) -> None:
        self.at_record(T_PROGRAMS, P_SPACE_SCI)["Program Cluster"] = ["recAPIc1"]
        run = self.run_sync()
        self.assertEqual(self.logs(run, "cluster_mismatch"), [])

    def test_airtable_dropping_a_sync_edge_is_an_item_never_a_removal(self) -> None:
        self.run_sync()
        tertiary, climate, ais = self.ids(P_TERTIARY, CLIMATE, AIS)
        self.at_record(T_PROGRAMS, P_TERTIARY)["Cluster Record Id"] = AIS
        self.run_sync()
        self.assertEqual(self.edges(tertiary), {climate: "airtable_sync", ais: "airtable_sync"})
        [item] = self.items("cluster_edge_removed")
        self.assertEqual((item.airtable_record_id, item.field, item.hub_id), (P_TERTIARY, CLIMATE, tertiary))
        self.assertEqual(item.payload["cluster_scope_id"], climate)
        self.assertEqual(item.payload["choices"], ["remove_edge", "keep"])
        self.assertEqual(self.record("program", P_TERTIARY).base["clusters"], sorted([AIS, CLIMATE]))

    def test_moving_a_program_adds_the_new_edge_and_asks_about_the_old(self) -> None:
        self.run_sync()
        sci, space, climate = self.ids(P_SPACE_SCI, SPACE, CLIMATE)
        self.at_record(T_PROGRAMS, P_SPACE_SCI)["Cluster Record Id"] = CLIMATE
        self.run_sync()
        self.assertEqual(self.edges(sci), {space: "airtable_sync", climate: "airtable_sync"})
        self.assertEqual([i.field for i in self.items("cluster_edge_removed")], [SPACE])

    def test_admin_removing_a_sync_edge_is_kept(self) -> None:
        self.run_sync()
        sci, space = self.ids(P_SPACE_SCI, SPACE)
        self.db.query(ScopeEdgeV2).filter_by(child_scope_id=sci).delete()
        self.db.commit()
        run = self.run_sync()
        self.assertEqual(self.edges(sci), {})
        self.assertEqual([(l.airtable_record_id, l.field) for l in self.logs(run, "hub_differs")], [(P_SPACE_SCI, "cluster")])
        self.assertEqual(self.items(), [])

    def test_hand_made_edges_are_never_touched(self) -> None:
        self.run_sync(steps=["clusters"])
        space, climate = self.ids(SPACE, CLIMATE)
        self.run_sync(steps=["programs"])
        sci, lone = self.ids(P_SPACE_SCI, P_LONE)
        # A hand-made edge to a non-cluster, and one duplicating Airtable's.
        self.db.query(ScopeEdgeV2).filter_by(child_scope_id=sci).delete()
        self.db.commit()
        create_scope_edge(self.db, space, sci)
        create_scope_edge(self.db, self.studio, lone)
        create_scope_edge(self.db, climate, lone)
        self.db.commit()
        self.db.query(AirtableSyncRecordV2).filter_by(airtable_record_id=P_SPACE_SCI).one().base = {"name": "Space Science"}
        self.db.commit()
        run = self.run_sync(steps=["programs"])
        self.assertEqual(self.edges(sci), {space: "manual"})  # present, left manual
        self.assertEqual([l.airtable_record_id for l in self.logs(run, "edge_is_manual")], [P_SPACE_SCI])
        self.assertEqual(self.edges(lone), {self.studio: "manual", climate: "manual"})
        # Airtable later dropping the cluster leaves the manual edge alone.
        self.at_record(T_PROGRAMS, P_SPACE_SCI).pop("Cluster Record Id")
        self.run_sync(steps=["programs"])
        self.assertEqual(self.edges(sci), {space: "manual"})
        self.assertEqual(self.items(), [])
        self.assertEqual(self.record("program", P_SPACE_SCI).base["clusters"], [])

    def test_unknown_cluster_holds_only_that_programs_edges(self) -> None:
        self.at_record(T_PROGRAMS, P_TERTIARY)["Cluster Record Id"] = f"{AIS}, recTYPO"
        self.run_sync()
        tertiary = self.scope_by_rid(P_TERTIARY)
        self.assertIsNotNone(tertiary)  # the scope itself is not held
        self.assertEqual(self.edges(tertiary.id), {})
        [item] = self.items("unknown_cluster")
        self.assertEqual((item.airtable_record_id, item.payload["unknown_cluster_record_ids"]), (P_TERTIARY, ["recTYPO"]))
        self.assertEqual(len(self.edges(self.scope_by_rid(P_MATH).id)), 1)  # others unaffected
        # Fixed in Airtable → closes by itself, edges follow.
        self.at_record(T_PROGRAMS, P_TERTIARY)["Cluster Record Id"] = AIS
        self.run_sync()
        self.assertEqual(self.items("unknown_cluster"), [])
        self.assertEqual(self.items("unknown_cluster", status="resolved")[0].resolution, "auto_closed")
        self.assertEqual(self.edges(tertiary.id), {self.scope_by_rid(AIS).id: "airtable_sync"})

    def test_unknown_cluster_carries_the_programs_other_edge_items(self) -> None:
        self.run_sync()
        self.at_record(T_PROGRAMS, P_TERTIARY)["Cluster Record Id"] = AIS
        self.run_sync()
        self.assertEqual(len(self.items("cluster_edge_removed")), 1)
        self.at_record(T_PROGRAMS, P_TERTIARY)["Cluster Record Id"] = f"{AIS}, recTYPO"
        self.run_sync()
        self.assertEqual(len(self.items("cluster_edge_removed")), 1)  # held, not closed
        self.assertEqual(len(self.items("unknown_cluster")), 1)

    def test_programs_only_run_waits_on_missing_clusters(self) -> None:
        run = self.run_sync(steps=["programs"])
        self.assertEqual(run.status, "succeeded")
        sci = self.scope_by_rid(P_SPACE_SCI)
        self.assertEqual(self.edges(sci.id), {})
        waiting = [l for l in self.logs(run, "scope_not_synced") if l.field == "cluster" and l.airtable_record_id != P_AIS_ALIAS]
        self.assertEqual(len(waiting), 4)
        self.assertEqual(self.record("program", P_SPACE_SCI).base["clusters"], [])
        self.run_sync()
        self.assertEqual(self.edges(sci.id), {self.scope_by_rid(SPACE).id: "airtable_sync"})

    def test_a_refused_edge_is_logged_and_the_step_goes_on(self) -> None:
        self.run_sync(steps=["clusters"])
        self.run_sync(steps=["programs"])
        sci, space = self.ids(P_SPACE_SCI, SPACE)
        self.db.query(ScopeEdgeV2).filter_by(child_scope_id=sci).delete()
        create_scope_edge(self.db, sci, space)  # by hand: the program contains its cluster
        self.db.commit()
        self.db.query(AirtableSyncRecordV2).filter_by(airtable_record_id=P_SPACE_SCI).one().base = {"name": "Space Science"}
        self.db.commit()
        run = self.run_sync(steps=["programs"])
        [log] = self.logs(run, "edge_refused")
        self.assertEqual((log.airtable_record_id, log.old_value), (P_SPACE_SCI, "cycle"))
        self.assertEqual(self.edges(sci), {})
        self.assertEqual(self.record("program", P_SPACE_SCI).base["clusters"], [])  # retried next run


# ---------------------------------------------------------
# The items machinery
# ---------------------------------------------------------


class TestItems(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_sync(steps=["clusters"])
        self.space = self.scope_by_rid(SPACE).id
        self.rename_in_hub(self.space, "Space (Hub)")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space A"
        self.first = self.run_sync(steps=["clusters"])

    def test_same_fingerprint_refreshes_the_open_item(self) -> None:
        [item] = self.items("scope_name_conflict")
        self.rename_in_hub(self.space, "Space (Hub 2)")  # a Hub-side change only
        run = self.run_sync(steps=["clusters"])
        [again] = self.items("scope_name_conflict")
        self.assertEqual(again.id, item.id)
        self.assertEqual((again.first_run_id, again.last_run_id), (self.first.id, run.id))
        self.assertEqual(again.payload["hub"], "Space (Hub 2)")
        self.assertEqual(run.summary["steps"]["clusters"]["items_refreshed"], 1)

    def test_new_fingerprint_supersedes(self) -> None:
        [old] = self.items("scope_name_conflict")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space B"
        run = self.run_sync(steps=["clusters"])
        [new] = self.items("scope_name_conflict")
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.payload["airtable"], "Space B")
        self.db.expire_all()
        old = self.db.get(AirtableSyncItemV2, old.id)
        self.assertEqual((old.status, old.last_run_id), ("superseded", run.id))
        self.assertEqual(old.fingerprint, items_module.fingerprint("scope_name_conflict", "Space A"))

    def test_an_answered_item_is_remembered_for_the_same_fingerprint(self) -> None:
        [item] = self.items("scope_name_conflict")
        item.status, item.resolution, item.resolved_by_email = "resolved", "skip", "admin@renphil.org"
        self.db.commit()
        run = self.run_sync(steps=["clusters"])
        self.assertEqual(self.items("scope_name_conflict"), [])
        self.assertEqual(run.summary["steps"]["clusters"]["items_remembered"], 1)
        self.assertEqual(self.db.get(ScopeV2, self.space).name, "Space (Hub)")  # still held
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space C"  # Airtable changed again
        self.run_sync(steps=["clusters"])
        self.assertEqual(len(self.items("scope_name_conflict")), 1)

    def test_an_auto_closed_item_reopens_when_the_problem_returns(self) -> None:
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space (Hub)"  # agree: conflict gone
        self.run_sync(steps=["clusters"])
        self.assertEqual(self.items("scope_name_conflict"), [])
        self.rename_in_hub(self.space, "Space (Hub again)")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space A"
        self.run_sync(steps=["clusters"])
        self.assertEqual(len(self.items("scope_name_conflict")), 1)

    def test_a_programs_only_run_leaves_cluster_items_alone(self) -> None:
        self.run_sync(steps=["programs"])
        self.assertEqual(len(self.items("scope_name_conflict")), 1)

    def test_rollback_restores_the_books_index(self) -> None:
        run = self.run_sync(steps=["clusters"])  # a run, for the context below
        ctx = SimpleNamespace(db=self.db, run_id=run.id, dry_run=False, log=lambda *a, **k: None)
        book = items_module.ItemBook(ctx, "cluster")
        [old] = self.items("scope_name_conflict")
        book.raise_item(SPACE, "scope_name_conflict", inputs="Other", payload={}, subject="x")
        self.db.rollback()
        book.rolled_back()
        self.assertEqual(book.open_kinds(SPACE), {"scope_name_conflict"})
        self.assertEqual(self.items("scope_name_conflict")[0].id, old.id)


# ---------------------------------------------------------
# Preview
# ---------------------------------------------------------


class TestPreview(SyncFixture):
    def test_full_preview_writes_only_log_rows(self) -> None:
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        self.assertIsNone(self.db.get(ScopeV2, self.math).airtable_record_id)
        actions = [l.action for l in self.logs(run)]
        self.assertEqual(actions.count("cluster_created"), 3)
        self.assertEqual(actions.count("scope_created"), 3)
        self.assertEqual(actions.count("linked"), 1)
        # Would-be clusters reach the Programs step: the edges show up.
        self.assertEqual(actions.count("edge_added"), 4)
        self.assertEqual(actions.count("scope_not_synced"), 0)
        self.assertEqual(run.summary["steps"]["programs"]["edges_added"], 4)

    def test_preview_records_would_be_items(self) -> None:
        self.run_sync(steps=["clusters"])
        space = self.scope_by_rid(SPACE).id
        self.rename_in_hub(space, "Space (Hub)")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space A"
        self.drop_record(T_CLUSTERS, CLIMATE)
        before = self.hub_counts()
        run = self.run_sync(steps=["clusters"], preview=True)
        self.assertEqual(self.hub_counts(), before)
        [conflict] = self.logs(run, "would_raise_scope_name_conflict")
        self.assertEqual((conflict.airtable_record_id, conflict.old_value), (SPACE, "open"))
        self.assertEqual(len(self.logs(run, "would_raise_scope_deleted")), 1)
        self.run_sync(steps=["clusters"])  # real: opens them
        run = self.run_sync(steps=["clusters"], preview=True)
        self.assertEqual(self.logs(run, "would_raise_scope_name_conflict")[0].old_value, "refresh")
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space (Hub)"
        run = self.run_sync(steps=["clusters"], preview=True)
        [close] = self.logs(run, "would_auto_close")
        self.assertEqual((close.airtable_record_id, close.field), (SPACE, "scope_name_conflict"))
        self.assertEqual(len(self.items("scope_name_conflict")), 1)  # still open

    def test_preview_of_a_rename_and_edges_on_existing_scopes(self) -> None:
        self.run_sync()
        self.at_record(T_CLUSTERS, SPACE)["Name"] = "Space 2"
        self.at_record(T_PROGRAMS, P_LONE)["Cluster Record Id"] = SPACE
        before = self.hub_counts()
        run = self.run_sync(preview=True)
        self.assertEqual(self.hub_counts(), before)
        self.assertEqual(self.scope_by_rid(SPACE).name, "Space")
        self.assertEqual([l.new_value for l in self.logs(run, "scope_renamed")], ["Space 2"])
        self.assertEqual([l.airtable_record_id for l in self.logs(run, "edge_added")], [P_LONE])


if __name__ == "__main__":
    unittest.main()
