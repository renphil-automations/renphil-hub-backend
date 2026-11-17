"""Airtable sync, phase 1 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §4, §9):
the schema additions, `source` on assignments and scope edges, `scopes.type`
on the wire, and the settings + Designation mapping endpoints.

Same in-memory-SQLite router fixture as test_default_assignment.py: the org
and the admin's literal `(hub_admin, all_scopes)` pair are seeded straight
into the database. Airtable is never called — the Designation choices reader
is patched.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase1-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.airtable_sync import (  # noqa: E402
    AirtableDesignationMappingV2,
    AirtableSyncItemV2,
)
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"
MEMBER_EMAIL = "member@renphil.org"
CHOICES = ["Program Lead", "Program Designate", "Member", "Studio Lead", "Studio Designate", "Advisor"]
READER = "app.services.airtable_sync.airtable_reader.designation_choices"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(email: str, name: str) -> UserV2:
    now = _now()
    return UserV2(work_email=email, name=name, has_signed_in=True, created_at=now, updated_at=now)


class SyncFixture(unittest.TestCase):
    def setUp(self) -> None:
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
        self._seed_org()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _seed_org(self) -> None:
        db = self.db

        def role(key, name, public=False):
            row = RoleV2(key=key, name=name, is_public=public, is_system=False, created_at=_now())
            db.add(row)
            db.flush()
            return row.id

        def scope(key, name, universal=False, public=False):
            row = ScopeV2(
                key=key, name=name, is_universal=universal, is_public=public, is_system=False, created_at=_now()
            )
            db.add(row)
            db.flush()
            return row.id

        self.admin_role = role("hub_admin", "Admin")
        self.member_role = role("member", "Member")
        self.lead_role = role("program_lead", "Program Lead")
        self.spare_role = role("spare", "Spare")
        self.user_role = role("user", "User", public=True)
        db.add(RoleEdgeV2(parent_role_id=self.admin_role, child_role_id=self.member_role, created_at=_now()))
        self.all_scopes = scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = scope("scope_a", "Scope A")
        self.scope_b = scope("scope_b", "Scope B")
        self.hub_scope = scope("hub", "Hub", public=True)

        admin = _user(ADMIN_EMAIL, "admin")
        member = _user(MEMBER_EMAIL, "member")
        db.add_all([admin, member])
        db.flush()
        db.add(RoleAssignmentV2(user_id=admin.id, role_id=self.admin_role, scope_id=self.all_scopes, created_at=_now()))
        db.add(RoleAssignmentV2(user_id=member.id, role_id=self.member_role, scope_id=self.scope_a, created_at=_now()))
        db.commit()

    def auth(self, email: str) -> dict:
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def admin(self) -> dict:
        return self.auth(ADMIN_EMAIL)

    def code_of(self, res) -> str:
        return res.json()["detail"]["code"]


# ---------------------------------------------------------
# Schema
# ---------------------------------------------------------


class TestSchema(SyncFixture):
    def test_scope_type_only_accepts_cluster_or_program(self) -> None:
        scope = self.db.get(ScopeV2, self.scope_a)
        scope.type = "cluster"
        self.db.commit()
        scope.type = "fund"
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_scope_record_id_is_unique_but_many_may_be_null(self) -> None:
        self.db.get(ScopeV2, self.scope_a).airtable_record_id = "recA"
        self.db.commit()  # scope_b and the rest stay NULL — no clash
        self.db.get(ScopeV2, self.scope_b).airtable_record_id = "recA"
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_user_record_id_is_unique_but_many_may_be_null(self) -> None:
        users = self.db.query(UserV2).order_by(UserV2.id).all()
        users[0].airtable_record_id = "recU"
        self.db.commit()
        users[1].airtable_record_id = "recU"
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_assignment_source_defaults_to_manual_and_is_checked(self) -> None:
        rows = self.db.query(RoleAssignmentV2).all()
        self.assertEqual({r.source for r in rows}, {"manual"})
        rows[0].source = "imported"
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_only_one_open_item_per_record_kind_and_field(self) -> None:
        def item(status="open"):
            return AirtableSyncItemV2(
                entity="user", airtable_record_id="recX", kind="no_work_email", fingerprint="f", status=status
            )

        self.db.add(item())
        self.db.commit()
        self.db.add(item(status="resolved"))
        self.db.add(item(status="resolved"))
        self.db.commit()  # closed items may repeat
        self.db.add(item())
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()


# ---------------------------------------------------------
# `source` on assignments and edges
# ---------------------------------------------------------


class TestSource(SyncFixture):
    def test_sign_in_default_pair_is_marked_default(self) -> None:
        res = self.client.get("/v2/rbac/me/assignments", headers=self.auth("new@renphil.org"))
        self.assertEqual(res.status_code, 200, res.text)
        self.db.expire_all()
        user = self.db.query(UserV2).filter(UserV2.matches_email("new@renphil.org")).one()
        rows = self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == user.id).all()
        self.assertEqual([(r.role_id, r.scope_id, r.source) for r in rows], [(self.user_role, self.hub_scope, "default")])

    def test_admin_grant_is_marked_manual(self) -> None:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": MEMBER_EMAIL, "role_id": self.lead_role, "scope_id": self.scope_b},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 201, res.text)
        self.db.expire_all()
        row = self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.scope_id == self.scope_b).one()
        self.assertEqual(row.source, "manual")

    def test_admin_scope_edge_is_marked_manual(self) -> None:
        res = self.client.post(f"/v2/rbac/scopes/{self.scope_a}/children/{self.scope_b}", headers=self.admin)
        self.assertEqual(res.status_code, 204, res.text)
        self.db.expire_all()
        self.assertEqual(self.db.query(ScopeEdgeV2).one().source, "manual")


# ---------------------------------------------------------
# scopes.type on the wire (plan v2 §4.1)
# ---------------------------------------------------------


class TestScopeType(SyncFixture):
    def test_list_and_get_return_type(self) -> None:
        self.db.get(ScopeV2, self.scope_a).type = "cluster"
        self.db.get(ScopeV2, self.scope_b).type = "program"
        self.db.commit()
        res = self.client.get("/v2/rbac/scopes", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        types = {s["id"]: s["type"] for s in res.json()["data"]}
        self.assertEqual(types[self.scope_a], "cluster")
        self.assertEqual(types[self.scope_b], "program")
        self.assertIsNone(types[self.hub_scope])
        one = self.client.get(f"/v2/rbac/scopes/{self.scope_a}", headers=self.admin)
        self.assertEqual(one.json()["data"]["type"], "cluster")

    def test_type_cannot_be_sent_on_create(self) -> None:
        res = self.client.post(
            "/v2/rbac/scopes", json={"key": "new_scope", "name": "New", "type": "cluster"}, headers=self.admin
        )
        self.assertEqual(res.status_code, 422, res.text)

    def test_type_cannot_be_sent_on_update(self) -> None:
        res = self.client.patch(f"/v2/rbac/scopes/{self.scope_a}", json={"type": "cluster"}, headers=self.admin)
        self.assertEqual(res.status_code, 422, res.text)
        self.db.expire_all()
        self.assertIsNone(self.db.get(ScopeV2, self.scope_a).type)

    def test_a_typed_scope_keeps_its_type_on_rename(self) -> None:
        self.db.get(ScopeV2, self.scope_a).type = "program"
        self.db.commit()
        res = self.client.patch(f"/v2/rbac/scopes/{self.scope_a}", json={"name": "Renamed"}, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["type"], "program")


# ---------------------------------------------------------
# Access: Hub Admins only
# ---------------------------------------------------------


class TestAccess(SyncFixture):
    def test_non_admin_is_refused_everywhere(self) -> None:
        member = self.auth(MEMBER_EMAIL)
        with mock.patch(READER, return_value=CHOICES):
            calls = [
                self.client.get("/v2/airtable-sync/settings", headers=member),
                self.client.put(
                    "/v2/airtable-sync/settings",
                    json={"schedule_enabled": False, "frequency": "daily", "run_time": "06:00", "timezone": "UTC"},
                    headers=member,
                ),
                self.client.get("/v2/airtable-sync/mappings", headers=member),
                self.client.put("/v2/airtable-sync/mappings", json={"mappings": []}, headers=member),
            ]
        self.assertEqual([c.status_code for c in calls], [403, 403, 403, 403])

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self.client.get("/v2/airtable-sync/settings").status_code, (401, 403))


# ---------------------------------------------------------
# Schedule settings
# ---------------------------------------------------------


class TestSettings(SyncFixture):
    def put(self, **body):
        payload = {"schedule_enabled": True, "frequency": "daily", "run_time": "07:30", "timezone": "Europe/London"}
        payload.update(body)
        return self.client.put("/v2/airtable-sync/settings", json=payload, headers=self.admin)

    def test_defaults(self) -> None:
        res = self.client.get("/v2/airtable-sync/settings", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(
            (data["schedule_enabled"], data["frequency"], data["run_time"], data["run_weekday"], data["timezone"]),
            (False, "daily", "06:00", None, "America/New_York"),
        )

    def test_daily_round_trip(self) -> None:
        res = self.put()
        self.assertEqual(res.status_code, 200, res.text)
        again = self.client.get("/v2/airtable-sync/settings", headers=self.admin).json()["data"]
        self.assertEqual(
            (again["schedule_enabled"], again["run_time"], again["timezone"], again["updated_by_email"]),
            (True, "07:30", "Europe/London", ADMIN_EMAIL),
        )

    def test_weekly_needs_a_weekday(self) -> None:
        res = self.put(frequency="weekly")
        self.assertEqual((res.status_code, self.code_of(res)), (409, "weekday_required"))
        ok = self.put(frequency="weekly", run_weekday=0)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["data"]["run_weekday"], 0)

    def test_daily_refuses_a_weekday(self) -> None:
        res = self.put(run_weekday=3)
        self.assertEqual((res.status_code, self.code_of(res)), (409, "weekday_not_allowed"))

    def test_unknown_timezone(self) -> None:
        res = self.put(timezone="Mars/Olympus")
        self.assertEqual((res.status_code, self.code_of(res)), (409, "invalid_timezone"))

    def test_malformed_bodies_are_422(self) -> None:
        for body in ({"run_time": "25:00"}, {"run_time": "7:30"}, {"run_weekday": 7}, {"frequency": "hourly"}, {"extra": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.put(**body).status_code, 422)


# ---------------------------------------------------------
# Designation → role mapping
# ---------------------------------------------------------


class TestMappings(SyncFixture):
    def get(self, choices=CHOICES, side_effect=None):
        with mock.patch(READER, return_value=choices, side_effect=side_effect):
            return self.client.get("/v2/airtable-sync/mappings", headers=self.admin)

    def put(self, mappings, choices=CHOICES):
        with mock.patch(READER, return_value=choices):
            return self.client.put("/v2/airtable-sync/mappings", json={"mappings": mappings}, headers=self.admin)

    def test_empty_mapping_lists_every_choice_as_unmapped(self) -> None:
        res = self.get()
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual((data["mappings"], data["designation_choices"], data["unmapped_choices"]), ([], CHOICES, CHOICES))
        self.assertIsNone(data["choices_error"])

    def test_replace_and_read_back(self) -> None:
        res = self.put(
            [
                {"designation": " Program Lead ", "action": "role", "role_id": self.lead_role},
                {"designation": "Member", "action": "role", "role_id": self.member_role},
                {"designation": "Advisor", "action": "dont_sync"},
            ]
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        by_name = {m["designation"]: m for m in data["mappings"]}
        self.assertEqual(set(by_name), {"Program Lead", "Member", "Advisor"})  # trimmed
        self.assertEqual(
            (by_name["Program Lead"]["role_name"], by_name["Advisor"]["action"], by_name["Advisor"]["role_id"]),
            ("Program Lead", "dont_sync", None),
        )
        self.assertEqual(by_name["Member"]["updated_by_email"], ADMIN_EMAIL)
        self.assertEqual(data["unmapped_choices"], ["Program Designate", "Studio Lead", "Studio Designate"])

    def test_replace_updates_in_place_and_deletes_what_is_left_out(self) -> None:
        self.put(
            [
                {"designation": "Member", "action": "role", "role_id": self.member_role},
                {"designation": "Advisor", "action": "dont_sync"},
            ]
        )
        self.db.expire_all()
        member_id = self.db.query(AirtableDesignationMappingV2).filter_by(designation="Member").one().id

        res = self.put([{"designation": "member", "action": "role", "role_id": self.spare_role}])
        self.assertEqual(res.status_code, 200, res.text)
        self.db.expire_all()
        rows = self.db.query(AirtableDesignationMappingV2).all()
        self.assertEqual([(r.id, r.designation, r.role_id) for r in rows], [(member_id, "member", self.spare_role)])

    def test_duplicate_designations_are_refused_case_insensitively(self) -> None:
        res = self.put(
            [
                {"designation": "Member", "action": "dont_sync"},
                {"designation": "MEMBER ", "action": "dont_sync"},
            ]
        )
        self.assertEqual((res.status_code, self.code_of(res)), (409, "duplicate_designation"))

    def test_role_rules(self) -> None:
        cases = [
            ({"designation": "Member", "action": "role"}, "role_required"),
            ({"designation": "Member", "action": "dont_sync", "role_id": self.member_role}, "role_not_allowed"),
            ({"designation": "Member", "action": "role", "role_id": 9999}, "role_not_found"),
            ({"designation": "Member", "action": "role", "role_id": self.admin_role}, "admin_role_not_allowed"),
            ({"designation": "Member", "action": "role", "role_id": self.user_role}, "public_role_not_allowed"),
            ({"designation": "  ", "action": "dont_sync"}, "empty_designation"),
        ]
        for entry, code in cases:
            with self.subTest(code=code):
                res = self.put([entry])
                self.assertEqual((res.status_code, self.code_of(res)), (409, code))
        self.db.expire_all()
        self.assertEqual(self.db.query(AirtableDesignationMappingV2).count(), 0)  # nothing half-written

    def test_a_refused_replace_writes_nothing(self) -> None:
        self.put([{"designation": "Member", "action": "role", "role_id": self.member_role}])
        res = self.put(
            [
                {"designation": "Advisor", "action": "dont_sync"},
                {"designation": "Member", "action": "role", "role_id": self.admin_role},
            ]
        )
        self.assertEqual(res.status_code, 409)
        self.db.expire_all()
        rows = self.db.query(AirtableDesignationMappingV2).all()
        self.assertEqual([(r.designation, r.role_id) for r in rows], [("Member", self.member_role)])

    def test_deleting_the_mapped_role_leaves_the_designation_unmapped(self) -> None:
        self.put([{"designation": "Program Lead", "action": "role", "role_id": self.spare_role}])
        res = self.client.delete(f"/v2/rbac/roles/{self.spare_role}", headers=self.admin)
        self.assertEqual(res.status_code, 204, res.text)
        data = self.get().json()["data"]
        self.assertEqual(
            [(m["designation"], m["role_id"], m["status"]) for m in data["mappings"]],
            [("Program Lead", None, "unmapped")],
        )

    def test_airtable_failure_still_returns_the_mapping(self) -> None:
        self.put([{"designation": "Member", "action": "dont_sync"}])
        res = self.get(side_effect=RuntimeError("boom"))
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual([m["designation"] for m in data["mappings"]], ["Member"])
        self.assertEqual(data["designation_choices"], [])
        self.assertIn("Could not read", data["choices_error"])
        self.assertNotIn("boom", data["choices_error"])  # details stay in the log

    def test_missing_configuration_is_reported(self) -> None:
        from app.services.airtable_sync.airtable_reader import AirtableSyncConfigError

        res = self.get(side_effect=AirtableSyncConfigError("AIRTABLE_SYNC_PROGRAM_ROLES_TABLE is not set"))
        self.assertIn("AIRTABLE_SYNC_PROGRAM_ROLES_TABLE", res.json()["data"]["choices_error"])


if __name__ == "__main__":
    unittest.main()
