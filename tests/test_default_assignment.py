"""The default `(User, Hub)` assignment — owner decision 2026-10-14.

Every NEWLY created `hub_users` row gets the ⊥ pair (public role on public
scope), whether it is born from the lazy sign-in upsert
(`dependencies._ensure_hub_user`) or from an admin's "Add user"
(`rbac_assignment_service.create_hub_user`). Existing rows are not
backfilled. Revoking and re-granting the pair follow the normal delegation
rules; the pair is the only ⊥ combination `_assert_not_public` lets through.

Same in-memory-SQLite router fixture as test_rbac_assignments_api.py, except
the org (and the Hub Admin's `(hub_admin, all_scopes)` row) is seeded
straight into the database: since phase 3 a JWT "Hub Admin" role no longer
makes anyone an admin, so the API-built org those modules use cannot be
built through the API at all until their fixtures are migrated.
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-default-assignment-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import rbac_assignments  # noqa: E402
from app.services import rbac_assignment_service  # noqa: E402
from app.services.rbac_delegation_service import can_delegate  # noqa: E402
from app.services.rbac_graph_service import effective_pairs  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(email: str, name: str) -> UserV2:
    now = _now()
    return UserV2(work_email=email, name=name, has_signed_in=True, created_at=now, updated_at=now)


class DefaultAssignmentFixture(unittest.TestCase):
    seed_public_rows = True

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
        """Admin -> Member, plus "User" (⊥ role); All Scopes (⊤), Scope A,
        and "Hub" (⊥ scope). The admin holds the literal admin pair."""
        db = self.db

        def role(key, name, public=False):
            row = RoleV2(key=key, name=name, is_public=public, is_system=False, created_at=_now())
            db.add(row)
            db.flush()
            return row.id

        def scope(key, name, universal=False, public=False):
            row = ScopeV2(
                key=key,
                name=name,
                is_universal=universal,
                is_public=public,
                is_system=False,
                created_at=_now(),
            )
            db.add(row)
            db.flush()
            return row.id

        self.admin_role = role("hub_admin", "Admin")
        self.member = role("member", "Member")
        db.add(RoleEdgeV2(parent_role_id=self.admin_role, child_role_id=self.member, created_at=_now()))
        self.all_scopes = scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = scope("scope_a", "Scope A")
        if self.seed_public_rows:
            self.user_role = role("any_role", "User", public=True)
            self.hub_scope = scope("any_scope", "Hub", public=True)

        admin = _user(ADMIN_EMAIL, "admin")
        db.add(admin)
        db.flush()
        db.add(
            RoleAssignmentV2(
                user_id=admin.id,
                role_id=self.admin_role,
                scope_id=self.all_scopes,
                created_at=_now(),
            )
        )
        db.commit()

    # -- helpers ------------------------------------------------------

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

    def my_assignments(self, email: str) -> list[dict]:
        res = self.client.get("/v2/rbac/me/assignments", headers=self.auth(email))
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["data"]

    def pairs_of(self, email: str) -> set[tuple[int, int]]:
        self.db.expire_all()
        user = self.db.query(UserV2).filter(UserV2.matches_email(email)).one()
        rows = self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == user.id).all()
        return {(r.role_id, r.scope_id) for r in rows}

    def hub_user_id(self, email: str) -> int:
        self.db.expire_all()
        return self.db.query(UserV2.id).filter(UserV2.matches_email(email)).scalar()

    def grant(self, granter: dict, email: str, role_id: int, scope_id: int):
        return self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": email, "role_id": role_id, "scope_id": scope_id},
            headers=granter,
        )

    def default_row_id(self, email: str) -> int:
        for a in self.my_assignments(email):
            if (a["role_id"], a["scope_id"]) == (self.user_role, self.hub_scope):
                return a["id"]
        self.fail(f"{email} holds no default assignment")

    def code_of(self, res) -> str:
        return res.json()["detail"]["code"]


# ---------------------------------------------------------
# Creation paths
# ---------------------------------------------------------


class TestSignInProvisioning(DefaultAssignmentFixture):
    def test_first_request_creates_the_default_pair(self) -> None:
        rows = self.my_assignments("new@renphil.org")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["role_id"], rows[0]["scope_id"]), (self.user_role, self.hub_scope))
        self.assertIsNone(rows[0]["granted_by_user_id"])
        self.assertIsNone(rows[0]["granted_by_email"])

    def test_repeated_requests_do_not_duplicate_it(self) -> None:
        for _ in range(3):
            self.my_assignments("again@renphil.org")
        self.assertEqual(self.pairs_of("again@renphil.org"), {(self.user_role, self.hub_scope)})

    def test_existing_users_are_not_backfilled(self) -> None:
        self.db.add(_user("old@renphil.org", "old"))
        self.db.commit()
        self.assertEqual(self.my_assignments("old@renphil.org"), [])

    def test_a_revoked_default_does_not_come_back_on_the_next_request(self) -> None:
        row_id = self.default_row_id("gone@renphil.org")
        res = self.client.delete(f"/v2/rbac/assignments/{row_id}", headers=self.admin)
        self.assertIn(res.status_code, (200, 204), res.text)
        self.assertEqual(self.my_assignments("gone@renphil.org"), [])

    def test_the_default_reaches_only_the_public_pair_and_delegates_nothing(self) -> None:
        self.my_assignments("plain@renphil.org")
        uid = self.hub_user_id("plain@renphil.org")
        self.assertEqual(effective_pairs(self.db, uid), {(self.user_role, self.hub_scope)})
        for role_id in (self.admin_role, self.member, self.user_role):
            for scope_id in (self.all_scopes, self.scope_a, self.hub_scope):
                self.assertFalse(can_delegate(self.db, uid, role_id, scope_id), (role_id, scope_id))


class TestWithoutPublicRows(DefaultAssignmentFixture):
    seed_public_rows = False

    def test_provisioning_still_works_and_writes_nothing(self) -> None:
        with self.assertLogs(rbac_assignment_service.logger, level="WARNING"):
            self.assertEqual(self.my_assignments("nobottom@renphil.org"), [])
        self.assertIsNotNone(self.hub_user_id("nobottom@renphil.org"))

    def test_admin_create_still_works_and_returns_no_assignments(self) -> None:
        res = self.client.post(
            "/v2/rbac/admin/hub-users",
            json={"email": "ext@example.com", "name": "Ext"},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["data"]["assignments"], [])


class TestAdminCreateHubUser(DefaultAssignmentFixture):
    def test_created_user_gets_the_default_and_the_response_shows_it(self) -> None:
        res = self.client.post(
            "/v2/rbac/admin/hub-users",
            json={"email": "  Ext@Example.com ", "name": "Ext"},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 201, res.text)
        assignments = res.json()["data"]["assignments"]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(
            (assignments[0]["role_id"], assignments[0]["scope_id"]),
            (self.user_role, self.hub_scope),
        )
        self.assertEqual(assignments[0]["user_email"], "ext@example.com")
        self.assertEqual(self.pairs_of("ext@example.com"), {(self.user_role, self.hub_scope)})

    def test_first_sign_in_after_admin_create_does_not_add_a_second(self) -> None:
        self.client.post(
            "/v2/rbac/admin/hub-users",
            json={"email": "pre@example.com", "name": "Pre"},
            headers=self.admin,
        )
        self.assertEqual(len(self.my_assignments("pre@example.com")), 1)

    def test_duplicate_email_writes_no_extra_default(self) -> None:
        self.my_assignments("dup@renphil.org")
        res = self.client.post(
            "/v2/rbac/admin/hub-users",
            json={"email": "dup@renphil.org", "name": "Dup"},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.pairs_of("dup@renphil.org"), {(self.user_role, self.hub_scope)})


# ---------------------------------------------------------
# Granting and revoking under the normal delegation rules
# ---------------------------------------------------------


class TestDefaultPairDelegation(DefaultAssignmentFixture):
    def setUp(self) -> None:
        super().setUp()
        # A delegator holding an ordinary role, and a target whose default
        # has been revoked so it can be granted back.
        self.my_assignments("lead@renphil.org")
        self.assertEqual(
            self.grant(self.admin, "lead@renphil.org", self.member, self.scope_a).status_code, 201
        )
        row_id = self.default_row_id("target@renphil.org")
        self.client.delete(f"/v2/rbac/assignments/{row_id}", headers=self.admin)
        self.assertEqual(self.my_assignments("target@renphil.org"), [])

    def test_admin_can_grant_it_back(self) -> None:
        res = self.grant(self.admin, "target@renphil.org", self.user_role, self.hub_scope)
        self.assertEqual(res.status_code, 201, res.text)

    def test_a_holder_of_an_ordinary_role_can_grant_it_back(self) -> None:
        res = self.grant(self.auth("lead@renphil.org"), "target@renphil.org", self.user_role, self.hub_scope)
        self.assertEqual(res.status_code, 201, res.text)

    def test_a_default_only_holder_cannot_grant_it(self) -> None:
        self.my_assignments("plain@renphil.org")
        res = self.grant(self.auth("plain@renphil.org"), "target@renphil.org", self.user_role, self.hub_scope)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_a_holder_of_an_ordinary_role_can_revoke_someone_elses_default(self) -> None:
        row_id = self.default_row_id("victim@renphil.org")
        res = self.client.delete(f"/v2/rbac/assignments/{row_id}", headers=self.auth("lead@renphil.org"))
        self.assertIn(res.status_code, (200, 204), res.text)
        self.assertEqual(self.my_assignments("victim@renphil.org"), [])

    def test_a_default_only_holder_cannot_revoke_someone_elses_default(self) -> None:
        self.my_assignments("plain@renphil.org")
        row_id = self.default_row_id("victim@renphil.org")
        res = self.client.delete(f"/v2/rbac/assignments/{row_id}", headers=self.auth("plain@renphil.org"))
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_the_public_role_on_a_real_scope_is_still_refused_even_for_an_admin(self) -> None:
        for granter in (self.admin, self.auth("lead@renphil.org")):
            res = self.grant(granter, "target@renphil.org", self.user_role, self.scope_a)
            self.assertEqual(res.status_code, 409, res.text)
            self.assertEqual(self.code_of(res), "public_not_assignable")
        self.assertEqual(self.my_assignments("target@renphil.org"), [])

    def test_the_exception_does_not_depend_on_the_temporary_scope_flag(self) -> None:
        with mock.patch.object(rbac_assignments, "TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT", False):
            ok = self.grant(self.admin, "target@renphil.org", self.user_role, self.hub_scope)
            self.assertEqual(ok.status_code, 201, ok.text)
            refused = self.grant(self.admin, "target@renphil.org", self.member, self.hub_scope)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(self.code_of(refused), "public_not_assignable")


class TestAdminPatchChecksTheResultingPair(DefaultAssignmentFixture):
    def patch(self, assignment_id: int, body: dict):
        return self.client.patch(f"/v2/rbac/admin/assignments/{assignment_id}", json=body, headers=self.admin)

    def test_moving_a_default_onto_a_real_scope_is_refused(self) -> None:
        row_id = self.default_row_id("u@renphil.org")
        res = self.patch(row_id, {"scope_id": self.scope_a})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")
        self.assertEqual(self.pairs_of("u@renphil.org"), {(self.user_role, self.hub_scope)})

    def test_changing_only_the_role_of_a_default_follows_the_scope_flag(self) -> None:
        row_id = self.default_row_id("u@renphil.org")
        with mock.patch.object(rbac_assignments, "TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT", False):
            res = self.patch(row_id, {"role_id": self.member})
            self.assertEqual(res.status_code, 409, res.text)
            self.assertEqual(self.code_of(res), "public_not_assignable")
        res = self.patch(row_id, {"role_id": self.member})
        self.assertEqual(res.status_code, 200, res.text)

    def test_an_ordinary_row_can_become_the_default_pair_but_not_half_of_it(self) -> None:
        self.my_assignments("v@renphil.org")
        row = self.grant(self.admin, "v@renphil.org", self.member, self.scope_a).json()["data"]
        half = self.patch(row["id"], {"role_id": self.user_role})
        self.assertEqual(half.status_code, 409, half.text)
        self.assertEqual(self.code_of(half), "public_not_assignable")

        # v already holds the default, so a second one is a duplicate; drop
        # it first.
        self.client.delete(f"/v2/rbac/assignments/{self.default_row_id('v@renphil.org')}", headers=self.admin)
        whole = self.patch(row["id"], {"role_id": self.user_role, "scope_id": self.hub_scope})
        self.assertEqual(whole.status_code, 200, whole.text)

    def test_a_missing_assignment_is_still_a_404(self) -> None:
        res = self.patch(999999, {"role_id": self.member})
        self.assertEqual(res.status_code, 404, res.text)


if __name__ == "__main__":
    unittest.main()
