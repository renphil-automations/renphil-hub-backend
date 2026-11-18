"""Permission Management router — plan_access_control_schema_2026-08-22.md
§6, session_handoff_2026-08-24-permission-management-plan.md §4, §5.

Same in-memory-SQLite router fixture as test_rbac_api.py: a real FastAPI
TestClient over a real (SQLite) v2 schema with JWTs minted locally.

Covers: hub_users auto-provisioning (the phase's blocking prerequisite,
§5.1), the §6.1 delegation gate on create AND revoke (symmetric, not
ownership-based — §4), the three read surfaces, and the hub-users search
endpoint's required search prefix.
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-rbac-assignments-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.main import create_app  # noqa: E402

HUB_ADMIN = "Hub Admin"
ADMIN_EMAIL = "admin@renphil.org"


class RbacAssignmentsApiFixture(unittest.TestCase):
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

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- auth -------------------------------------------------------

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": roles or [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def admin(self) -> dict:
        self._ensure_admin()
        return self._auth(ADMIN_EMAIL, [HUB_ADMIN])

    def _ensure_admin(self) -> None:
        """Since phase 3 the JWT "Hub Admin" claim makes nobody an admin, so
        the admin is a real `(hub_admin, all_scopes)` row, seeded straight
        into the database (the API can't build it: only an admin may create
        roles). Lazy, so tests that never act as admin keep an empty
        `users` table; `build_org` reuses the seeded role and scope."""
        if getattr(self, "admin_assignment_id", None) is not None:
            return
        now = datetime.now(timezone.utc)
        role = RoleV2(key="hub_admin", name="Hub Admin", is_public=False, is_system=False, created_at=now)
        scope = ScopeV2(
            key="all_scopes",
            name="All Scopes",
            is_universal=True,
            is_public=False,
            is_system=False,
            created_at=now,
        )
        user = UserV2(work_email=ADMIN_EMAIL, name="admin", has_signed_in=True, created_at=now, updated_at=now)
        self.db.add_all([role, scope, user])
        self.db.flush()
        assignment = RoleAssignmentV2(user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=now)
        self.db.add(assignment)
        self.db.commit()
        self.hub_admin_role = role.id
        self.all_scopes = scope.id
        self.admin_assignment_id = assignment.id

    def user(self, email: str) -> dict:
        return self._auth(email, [])

    # -- builders -----------------------------------------------------

    def make_role(self, key: str, name: str, public: bool = False) -> int:
        # No `depth` sent — the field is optional, nullable, and out of the UI.
        body: dict = {"key": key, "name": name}
        if public:
            body["is_public"] = True
        res = self.client.post("/v2/rbac/roles", json=body, headers=self.admin)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]["id"]

    def make_scope(self, key: str, name: str, universal: bool = False, public: bool = False) -> int:
        body: dict = {"key": key, "name": name, "is_universal": universal}
        if public:
            body["is_public"] = True
        res = self.client.post("/v2/rbac/scopes", json=body, headers=self.admin)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]["id"]

    def add_role_edge(self, parent_id: int, child_id: int) -> None:
        res = self.client.post(f"/v2/rbac/roles/{parent_id}/children/{child_id}", headers=self.admin)
        self.assertEqual(res.status_code, 204, res.text)

    def add_scope_edge(self, parent_id: int, child_id: int) -> None:
        res = self.client.post(f"/v2/rbac/scopes/{parent_id}/children/{child_id}", headers=self.admin)
        self.assertEqual(res.status_code, 204, res.text)

    def provision(self, headers: dict) -> None:
        """Simulates "has logged in at least once": hits any endpoint that
        depends on get_current_hub_user, the same lazy upsert a real first
        request would trigger. Asserts success so a broken provisioning
        path fails loudly at the builder, not deep inside a later test."""
        res = self.client.get("/v2/rbac/me/assignments", headers=headers)
        self.assertEqual(res.status_code, 200, res.text)

    def grant(self, granter_headers: dict, target_email: str, role_id: int, scope_id: int) -> dict:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": target_email, "role_id": role_id, "scope_id": scope_id},
            headers=granter_headers,
        )
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]

    def code_of(self, res) -> str:
        return res.json()["detail"]["code"]

    def build_org(self) -> None:
        """Design doc §9's worked example: Hub Admin(100) -> Program
        Lead(200)/Function Lead(200) -> Program Member(300)/Function
        Member(300) -> Hub Member(400); scopes All Scopes, A, B, HR,
        Onboarding (children A + HR). Hub Admin and All Scopes come from
        `_ensure_admin`."""
        self._ensure_admin()
        self.program_lead = self.make_role("program_lead", "Program Lead")
        self.function_lead = self.make_role("function_lead", "Function Lead")
        self.program_member = self.make_role("program_member", "Program Member")
        self.function_member = self.make_role("function_member", "Function Member")
        self.hub_member = self.make_role("hub_member", "Hub Member")
        self.add_role_edge(self.hub_admin_role, self.program_lead)
        self.add_role_edge(self.hub_admin_role, self.function_lead)
        self.add_role_edge(self.program_lead, self.program_member)
        self.add_role_edge(self.function_lead, self.function_member)
        self.add_role_edge(self.program_member, self.hub_member)
        self.add_role_edge(self.function_member, self.hub_member)

        self.scope_a =self.make_scope("scope_a", "Scope A")
        self.scope_b = self.make_scope("scope_b", "Scope B")
        self.scope_hr = self.make_scope("hr", "HR")
        self.scope_onboarding = self.make_scope("onboarding", "Onboarding")
        self.add_scope_edge(self.scope_onboarding, self.scope_a)
        self.add_scope_edge(self.scope_onboarding, self.scope_hr)


# ---------------------------------------------------------
# hub_users auto-provisioning (handoff §5.1) — the blocking prerequisite
# ---------------------------------------------------------


class TestAutoProvisioning(RbacAssignmentsApiFixture):
    def test_first_request_provisions_a_hub_users_row(self) -> None:
        res = self.client.get("/v2/rbac/me/assignments", headers=self.user("first@renphil.org"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"], [])

        self.db.expire_all()
        row = self.db.query(UserV2).filter(UserV2.email == "first@renphil.org").first()
        self.assertIsNotNone(row)
        self.assertTrue(row.has_signed_in)

    def test_repeated_requests_do_not_duplicate_the_row(self) -> None:
        headers = self.user("again@renphil.org")
        for _ in range(3):
            self.assertEqual(self.client.get("/v2/rbac/me/assignments", headers=headers).status_code, 200)

        self.db.expire_all()
        count = self.db.query(UserV2).filter(UserV2.email == "again@renphil.org").count()
        self.assertEqual(count, 1)

    def test_email_is_normalized(self) -> None:
        res = self.client.get(
            "/v2/rbac/me/assignments",
            headers=self._auth(" MixedCase@Renphil.ORG  ".strip(), []),
        )
        self.assertEqual(res.status_code, 200)
        self.db.expire_all()
        row = self.db.query(UserV2).filter(UserV2.email == "mixedcase@renphil.org").first()
        self.assertIsNotNone(row)

    def test_never_logged_in_cannot_be_a_grant_target(self) -> None:
        """§3.2's intended v1 constraint: no auto-create-on-grant. Someone
        who has never hit any authenticated endpoint has no hub_users row,
        so granting to them 409s rather than silently provisioning them."""
        self.build_org()
        res = self.client.post(
            "/v2/rbac/assignments",
            json={
                "user_email": "ghost@renphil.org",
                "role_id": self.program_member,
                "scope_id": self.scope_a,
            },
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "target_not_provisioned")

    # ── merge (2026-10): `hub_users` is now the HR `users` table ──────────

    def _hr_row(self, **kw) -> None:
        self.db.add(UserV2(status="Onboarding", has_signed_in=False, **kw))
        self.db.commit()

    def test_first_sign_in_claims_the_hr_row_instead_of_adding_one(self) -> None:
        self._hr_row(name="Staff Person", work_email="staff@renphil.org")
        self.provision(self._auth("staff@renphil.org", []))
        self.db.expire_all()
        rows = self.db.query(UserV2).filter(UserV2.matches_email("staff@renphil.org")).all()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].has_signed_in)
        self.assertEqual(rows[0].name, "Staff Person")  # the HR name is never overwritten
        self.assertEqual(rows[0].status, "Onboarding")

    def test_signing_in_by_alias_claims_the_same_row(self) -> None:
        self._hr_row(name="Aliased", work_email="long.name@renphil.org", alias_email="short@renphil.org")
        self.provision(self._auth("short@renphil.org", []))
        self.db.expire_all()
        self.assertEqual(self.db.query(UserV2).count(), 1)
        row = self.db.query(UserV2).filter(UserV2.matches_email("short@renphil.org")).one()
        self.assertTrue(row.has_signed_in)
        self.assertEqual(row.work_email, "long.name@renphil.org")

    def test_an_hr_row_can_be_granted_a_role_before_first_sign_in(self) -> None:
        self.build_org()
        self._hr_row(name="Not Yet", work_email="notyet@renphil.org")
        self.grant(self.admin, "notyet@renphil.org", self.program_member, self.scope_a)


# ---------------------------------------------------------
# Create — the bootstrap bypass, and the §6.1 rule (handoff §4)
# ---------------------------------------------------------


class TestCreateAssignment(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = "alice@renphil.org"
        self.carol = "carol@renphil.org"
        self.provision(self.user(self.alice))
        self.provision(self.user(self.carol))

    def test_unauthenticated_is_rejected(self) -> None:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.alice, "role_id": self.program_lead, "scope_id": self.scope_a},
        )
        self.assertIn(res.status_code, (401, 403))

    def test_hub_admin_bootstraps_the_first_assignment_ever(self) -> None:
        """The precise trap the plan's docstring warns about: this must
        work even though role_assignments is completely empty — Hub Admin
        never needs to already hold a row (handoff §3.1, §7)."""
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        self.assertEqual(assignment["role_id"], self.program_lead)
        self.assertEqual(assignment["scope_id"], self.scope_a)
        self.assertEqual(assignment["user_email"], self.alice)
        self.assertEqual(assignment["granted_by_email"], "admin@renphil.org")

    def test_hub_admin_can_grant_anything_unconditionally(self) -> None:
        self.grant(self.admin, self.alice, self.hub_admin_role, self.all_scopes)

    def test_role_not_found_is_404(self) -> None:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.alice, "role_id": 999999, "scope_id": self.scope_a},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 404)

    def test_scope_not_found_is_404(self) -> None:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.alice, "role_id": self.program_lead, "scope_id": 999999},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 404)

    def test_duplicate_assignment_is_409(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.alice, "role_id": self.program_lead, "scope_id": self.scope_a},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "duplicate_assignment")

    def test_delegated_grant_within_rule_succeeds(self) -> None:
        """Alice, Program Lead on A (granted by admin), grants Carol
        Program Member on A — proper descendant, her own scope."""
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        assignment = self.grant(self.user(self.alice), self.carol, self.program_member, self.scope_a)
        self.assertEqual(assignment["granted_by_email"], self.alice)

    def test_the_602_trap_is_rejected(self) -> None:
        """§6.2 / handoff §4's canonical example, run through the real API:
        Alice is Program Lead on A AND (separately) Program Member on B.
        Granting Carol Program Member on B must be REJECTED — Alice's
        authority on B is Program Member (equal, not a proper descendant),
        not Program Lead. A union-of-roles-crossed-with-union-of-scopes
        implementation would wrongly allow this."""
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        self.grant(self.admin, self.alice, self.program_member, self.scope_b)

        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.carol, "role_id": self.program_member, "scope_id": self.scope_b},
            headers=self.user(self.alice),
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_equal_role_is_rejected(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.carol, "role_id": self.program_lead, "scope_id": self.scope_a},
            headers=self.user(self.alice),
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_non_descendant_role_is_rejected(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.carol, "role_id": self.hub_admin_role, "scope_id": self.scope_a},
            headers=self.user(self.alice),
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_containing_scope_is_rejected(self) -> None:
        """All Scopes is a PARENT of A, never a descendant — Alice cannot
        climb from A to All Scopes."""
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.carol, "role_id": self.program_member, "scope_id": self.all_scopes},
            headers=self.user(self.alice),
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_holder_of_zero_assignments_cannot_delegate(self) -> None:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": self.carol, "role_id": self.hub_member, "scope_id": self.scope_a},
            headers=self.user(self.alice),
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "not_delegable")


# ---------------------------------------------------------
# Revoke — symmetric, not ownership-based (design doc §6.3, handoff §4/§7)
# ---------------------------------------------------------


class TestRevokeAssignment(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = "alice@renphil.org"
        self.bob = "bob@renphil.org"
        self.carol = "carol@renphil.org"
        for email in (self.alice, self.bob, self.carol):
            self.provision(self.user(email))

    def test_revoke_something_i_granted(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        assignment = self.grant(self.user(self.alice), self.carol, self.program_member, self.scope_a)
        res = self.client.delete(f"/v2/rbac/assignments/{assignment['id']}", headers=self.user(self.alice))
        self.assertEqual(res.status_code, 204, res.text)

    def test_revoke_not_found_is_404(self) -> None:
        res = self.client.delete("/v2/rbac/assignments/999999", headers=self.admin)
        self.assertEqual(res.status_code, 404)

    def test_hub_admin_can_revoke_anything(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        assignment = self.grant(self.user(self.alice), self.carol, self.program_member, self.scope_a)
        res = self.client.delete(f"/v2/rbac/assignments/{assignment['id']}", headers=self.admin)
        self.assertEqual(res.status_code, 204)

    def test_revocation_is_symmetric_not_ownership_based(self) -> None:
        """The counter-intuitive rule, restated as the landmine in handoff
        §7: Bob did NOT grant Carol's assignment (admin did), but Bob can
        still revoke it because he independently satisfies §6.1 against
        it — Program Lead on A, revoking Program Member on A."""
        self.grant(self.admin, self.bob, self.program_lead, self.scope_a)
        assignment = self.grant(self.admin, self.carol, self.program_member, self.scope_a)

        res = self.client.delete(f"/v2/rbac/assignments/{assignment['id']}", headers=self.user(self.bob))
        self.assertEqual(res.status_code, 204, res.text)

    def test_cannot_revoke_without_satisfying_the_rule(self) -> None:
        """Bob holds only Program Member on B — unrelated role, unrelated
        scope — and cannot revoke Carol's Program Member on A."""
        self.grant(self.admin, self.bob, self.program_member, self.scope_b)
        assignment = self.grant(self.admin, self.carol, self.program_member, self.scope_a)

        res = self.client.delete(f"/v2/rbac/assignments/{assignment['id']}", headers=self.user(self.bob))
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "not_delegable")

    def test_losing_authority_after_granting_blocks_your_own_revoke(self) -> None:
        """§6.4: no cascade on delegator removal. Alice grants Carol via
        her Program Lead on A; Alice's OWN assignment is then revoked by
        admin. Alice can no longer revoke what she granted, because her
        authority against it is gone — grants are independent facts, not
        derivations of the granter's authority (design doc §6.4)."""
        alice_assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        carol_assignment = self.grant(self.user(self.alice), self.carol, self.program_member, self.scope_a)

        revoke_alice = self.client.delete(
            f"/v2/rbac/assignments/{alice_assignment['id']}", headers=self.admin
        )
        self.assertEqual(revoke_alice.status_code, 204, revoke_alice.text)

        res = self.client.delete(
            f"/v2/rbac/assignments/{carol_assignment['id']}", headers=self.user(self.alice)
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "not_delegable")
        # But Carol still holds it — no cascade.
        held = self.client.get("/v2/rbac/me/assignments", headers=self.user(self.carol))
        self.assertEqual(len(held.json()["data"]), 1)


# ---------------------------------------------------------
# Reads — /me/assignments, /me/granted, /me/revocable
# ---------------------------------------------------------


class TestReadEndpoints(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = "alice@renphil.org"
        self.bob = "bob@renphil.org"
        self.carol = "carol@renphil.org"
        for email in (self.alice, self.bob, self.carol):
            self.provision(self.user(email))

    def test_me_assignments_lists_only_my_own(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        self.grant(self.admin, self.bob, self.program_member, self.scope_b)

        res = self.client.get("/v2/rbac/me/assignments", headers=self.user(self.alice))
        data = res.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["role_id"], self.program_lead)
        self.assertEqual(data[0]["scope_id"], self.scope_a)

    def test_me_granted_is_an_audit_trail_that_can_outlive_current_eligibility(self) -> None:
        """Proves /me/granted and /me/revocable are genuinely different
        lists, per design doc §6.4: Alice's grant to Carol stays in her
        /me/granted history even after Alice loses the authority that
        would let her revoke it today."""
        alice_assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        self.grant(self.user(self.alice), self.carol, self.program_member, self.scope_a)

        self.client.delete(f"/v2/rbac/assignments/{alice_assignment['id']}", headers=self.admin)

        granted = self.client.get("/v2/rbac/me/granted", headers=self.user(self.alice)).json()["data"]
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0]["user_email"], self.carol)

        revocable = self.client.get("/v2/rbac/me/revocable", headers=self.user(self.alice)).json()["data"]
        self.assertEqual(revocable, [])

    def test_me_revocable_is_org_wide_not_just_what_i_granted(self) -> None:
        """The decision behind this endpoint (session handoff follow-up,
        2026-08-24 §6): Bob can revoke Carol's assignment even though
        admin, not Bob, granted it — so it must appear in Bob's
        /me/revocable even though it will never appear in his /me/granted."""
        self.grant(self.admin, self.bob, self.program_lead, self.scope_a)
        self.grant(self.admin, self.carol, self.program_member, self.scope_a)

        granted = self.client.get("/v2/rbac/me/granted", headers=self.user(self.bob)).json()["data"]
        self.assertEqual(granted, [])

        revocable = self.client.get("/v2/rbac/me/revocable", headers=self.user(self.bob)).json()["data"]
        self.assertEqual(len(revocable), 1)
        self.assertEqual(revocable[0]["user_email"], self.carol)

    def test_me_revocable_hub_admin_sees_everything(self) -> None:
        """Everything includes the admin's own seeded `(hub_admin,
        all_scopes)` row: 2 granted here + 1."""
        a = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        b = self.grant(self.admin, self.bob, self.function_lead, self.scope_b)

        revocable = self.client.get("/v2/rbac/me/revocable", headers=self.admin).json()["data"]
        self.assertEqual(len(revocable), 3)
        self.assertEqual(
            {row["id"] for row in revocable}, {a["id"], b["id"], self.admin_assignment_id}
        )


# ---------------------------------------------------------
# hub-users search (handoff §5.4) — required search prefix
# ---------------------------------------------------------


class TestHubUserSearch(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.provision(self.user("searchable.person@renphil.org"))

    def test_query_below_min_length_is_422(self) -> None:
        res = self.client.get("/v2/rbac/hub-users?q=ab", headers=self.admin)
        self.assertEqual(res.status_code, 422)

    def test_missing_query_is_422(self) -> None:
        res = self.client.get("/v2/rbac/hub-users", headers=self.admin)
        self.assertEqual(res.status_code, 422)

    def test_finds_by_email_substring(self) -> None:
        res = self.client.get("/v2/rbac/hub-users?q=searchable", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        emails = [row["email"] for row in res.json()["data"]]
        self.assertIn("searchable.person@renphil.org", emails)

    def test_no_match_returns_empty_list(self) -> None:
        res = self.client.get("/v2/rbac/hub-users?q=nobodyhasthisname", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"], [])

    def test_response_never_carries_has_signed_in(self) -> None:
        res = self.client.get("/v2/rbac/hub-users?q=searchable", headers=self.admin)
        row = res.json()["data"][0]
        self.assertEqual(set(row.keys()), {"id", "email", "name"})
        self.assertNotIn("has_signed_in", row)
        self.assertNotIn("is_active", row)


# ---------------------------------------------------------
# ⊥ is never valid as an assignment
# (plan_access_control_algorithm_2026-08-27.md §4.4)
#
# THE OTHER HALF OF THE FLAGS, and a security check rather than a tidiness
# rule. "Any Role" / "Any Scope" sit in EVERY descendant set by
# construction, so the scope half of the §6.1 delegation rule passes for
# every user alive against a ⊥ target, and the role half does the same for
# anyone holding any role but ⊥. tests/test_rbac_delegation.py's
# TestBottomFlagsAndDelegation asserts that hole exists; this class is what
# closes it. Adding the flags without these refusals would hand every signed-
# in user the ability to grant `(anyone, Any Role, Any Scope)`.
# ---------------------------------------------------------


class TestBottomNotAssignable(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        # Pinned OFF: this class guards the permanent rule, independent of
        # the temporary testing-phase exception (see
        # TestTemporaryAnyScopeAssignable below).
        patcher = mock.patch(
            "app.routers.rbac_assignments.TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT", False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.build_org()
        self.any_role = self.make_role("any_role", "Any Role", public=True)
        self.any_scope = self.make_scope("any_scope", "Any Scope", public=True)
        self.target = self.user("target@renphil.org")
        self.provision(self.target)

    def attempt(self, headers: dict, role_id: int, scope_id: int):
        return self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": "target@renphil.org", "role_id": role_id, "scope_id": scope_id},
            headers=headers,
        )

    def test_public_scope_is_refused(self) -> None:
        res = self.attempt(self.admin, self.program_member, self.any_scope)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")
        self.assertEqual(res.json()["detail"]["scope_id"], self.any_scope)

    def test_public_role_is_refused(self) -> None:
        res = self.attempt(self.admin, self.any_role, self.scope_a)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")
        self.assertEqual(res.json()["detail"]["role_id"], self.any_role)

    def test_both_at_once_is_the_default_pair_and_allowed(self) -> None:
        """`(Any Role, Any Scope)` is every new user's default assignment
        (owner decision, 2026-10-14) — the one ⊥ combination let through,
        even with the temporary scope flag pinned off. The target got it on
        provisioning; once revoked it can be granted back."""
        self.db.expire_all()
        target_id = self.db.query(UserV2.id).filter(UserV2.email == "target@renphil.org").scalar()
        default = (
            self.db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.user_id == target_id,
                RoleAssignmentV2.role_id == self.any_role,
                RoleAssignmentV2.scope_id == self.any_scope,
            )
            .one()
        )
        default_id = default.id
        self.assertEqual(
            self.client.delete(f"/v2/rbac/assignments/{default_id}", headers=self.admin).status_code, 204
        )

        res = self.attempt(self.admin, self.any_role, self.any_scope)
        self.assertEqual(res.status_code, 201, res.text)

    def test_hub_admin_does_not_bypass_it(self) -> None:
        """POSITION, not politeness. Hub Admin bypasses the delegation rule
        unconditionally, so a refusal folded into `assert_can_delegate`
        would be skipped for exactly the people most able to do damage with
        it. This runs before that gate and applies to everyone.

        Every other test in this class already uses `self.admin`; this one
        states the reason so the ordering is not "simplified" later."""
        self.assertEqual(self.attempt(self.admin, self.any_role, self.scope_a).status_code, 409)

    def test_a_delegator_who_would_otherwise_pass_the_gate_is_refused(self) -> None:
        """THE HOLE, closed. Alice is Program Lead on A. Program Member is a
        proper descendant of Program Lead, and the public scope is in
        `scope_descendants(A)` — as it is in every scope's — so §6.1's rule
        would say yes. She is refused anyway, because ⊥ never gets that far.

        Without this refusal, ANY user holding any non-leaf role could
        publish to the entire hub."""
        alice = self.user("alice@renphil.org")
        self.provision(alice)
        self.grant(self.admin, "alice@renphil.org", self.program_lead, self.scope_a)

        res = self.attempt(alice, self.program_member, self.any_scope)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")

    def test_nothing_is_written(self) -> None:
        self.attempt(self.admin, self.any_role, self.scope_a)
        self.db.expire_all()
        self.assertEqual(
            self.db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.role_id == self.any_role,
                RoleAssignmentV2.scope_id == self.scope_a,
            )
            .count(),
            0,
        )

    def test_a_missing_id_is_still_a_404_first(self) -> None:
        """The 404 checks stay ahead of this one, so a bad id never leaks
        whether it would otherwise have been refused for a different
        reason — the same ordering the delegation gate already has."""
        res = self.attempt(self.admin, 999_999, self.any_scope)
        self.assertEqual(res.status_code, 404, res.text)

    def test_ordinary_assignments_are_unaffected(self) -> None:
        """The refusal must be narrow: with both ⊥ rows sitting in the
        tables, a perfectly ordinary grant still goes through."""
        res = self.attempt(self.admin, self.program_member, self.scope_a)
        self.assertEqual(res.status_code, 201, res.text)

    def test_an_existing_bottom_assignment_can_still_be_revoked(self) -> None:
        """REFUSE THE WAY IN, NEVER THE WAY OUT. A ⊥ row should not exist,
        but one written before these flags landed — or straight into the
        database — must be removable. Refusing the revoke path too would
        strand it permanently: the widest reach in the system, with no way
        to take it away through the UI.

        Inserted directly rather than through the API, since the API is
        exactly what now refuses to create it. `(Any Role, Scope A)` rather
        than `(Any Role, Any Scope)`: the latter is now the legitimate
        default pair, and the target already holds it from provisioning."""
        target_row = (
            self.db.query(UserV2).filter(UserV2.email == "target@renphil.org").first()
        )
        stranded = RoleAssignmentV2(
            user_id=target_row.id,
            role_id=self.any_role,
            scope_id=self.scope_a,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(stranded)
        self.db.commit()
        # Read the id out BEFORE the delete: once the row is gone, touching
        # the attribute on the expired instance raises ObjectDeletedError
        # rather than returning it.
        stranded_id = stranded.id

        res = self.client.delete(f"/v2/rbac/assignments/{stranded_id}", headers=self.admin)
        self.assertEqual(res.status_code, 204, res.text)

        self.db.expire_all()
        self.assertIsNone(
            self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.id == stranded_id).first()
        )


class TestTemporaryAnyScopeAssignable(RbacAssignmentsApiFixture):
    """TEMPORARY testing-phase exception (owner decision, 2026-09-24):
    "Any Scope" may be assigned to any user under the normal delegation
    rules; "Any Role" stays refused. Delete this class when
    `TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT` goes away."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(
            "app.routers.rbac_assignments.TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT", True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.build_org()
        self.any_role = self.make_role("any_role", "Any Role", public=True)
        self.any_scope = self.make_scope("any_scope", "Any Scope", public=True)
        self.target = self.user("target@renphil.org")
        self.provision(self.target)

    def attempt(self, headers: dict, role_id: int, scope_id: int):
        return self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": "target@renphil.org", "role_id": role_id, "scope_id": scope_id},
            headers=headers,
        )

    def test_admin_can_assign_any_scope(self) -> None:
        res = self.attempt(self.admin, self.program_member, self.any_scope)
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["data"]["scope_id"], self.any_scope)

    def test_delegator_can_assign_any_scope_under_normal_rules(self) -> None:
        alice = self.user("alice@renphil.org")
        self.provision(alice)
        self.grant(self.admin, "alice@renphil.org", self.program_lead, self.scope_a)

        res = self.attempt(alice, self.program_member, self.any_scope)
        self.assertEqual(res.status_code, 201, res.text)

    def test_delegation_rule_still_applies_to_the_role(self) -> None:
        """Only the ⊥ refusal is lifted — a role the granter can't delegate
        is still refused by §6.1."""
        alice = self.user("alice@renphil.org")
        self.provision(alice)
        self.grant(self.admin, "alice@renphil.org", self.program_member, self.scope_a)

        res = self.attempt(alice, self.program_lead, self.any_scope)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertNotEqual(self.code_of(res), "public_not_assignable")

    def test_any_role_is_still_refused(self) -> None:
        res = self.attempt(self.admin, self.any_role, self.scope_a)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")

    def test_any_role_any_scope_passes_the_bottom_check(self) -> None:
        """The default pair (owner decision, 2026-10-14). The target already
        holds it from provisioning, so the write gets past the ⊥ check and
        the delegation gate and stops at the duplicate rule."""
        res = self.attempt(self.admin, self.any_role, self.any_scope)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "duplicate_assignment")

    def test_admin_patch_to_any_scope_is_allowed(self) -> None:
        assignment = self.grant(self.admin, "target@renphil.org", self.program_lead, self.scope_a)
        res = self.client.patch(
            f"/v2/rbac/admin/assignments/{assignment['id']}",
            json={"scope_id": self.any_scope},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["scope_id"], self.any_scope)


# ---------------------------------------------------------
# Admin user-management table (owner decision, 2026-09-12) —
# GET /v2/rbac/admin/hub-users and PATCH /v2/rbac/admin/assignments/{id}.
# Hub-Admin-only, the one exception to this router's delegation gate.
# ---------------------------------------------------------


class TestAdminHubUsersList(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = "alice@renphil.org"
        self.bob = "bob@renphil.org"
        self.provision(self.user(self.alice))
        self.provision(self.user(self.bob))

    def test_non_admin_is_forbidden(self) -> None:
        res = self.client.get("/v2/rbac/admin/hub-users", headers=self.user(self.alice))
        self.assertEqual(res.status_code, 403)

    def test_unauthenticated_is_rejected(self) -> None:
        res = self.client.get("/v2/rbac/admin/hub-users")
        self.assertIn(res.status_code, (401, 403))

    def test_lists_every_provisioned_user_including_those_with_no_assignments(self) -> None:
        """Bob holds nothing — the common case (181 of 182 users as of the
        last audit) — and must still appear, since this table exists so an
        admin can find and assign exactly these people."""
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)

        res = self.client.get("/v2/rbac/admin/hub-users", headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        by_email = {row["email"]: row for row in res.json()["data"]}

        self.assertIn(self.alice, by_email)
        self.assertEqual(len(by_email[self.alice]["assignments"]), 1)
        self.assertEqual(by_email[self.alice]["assignments"][0]["role_id"], self.program_lead)

        self.assertIn(self.bob, by_email)
        self.assertEqual(by_email[self.bob]["assignments"], [])
        self.assertIn("has_signed_in", by_email[self.bob])

    def test_a_user_with_multiple_pairs_shows_all_of_them(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        self.grant(self.admin, self.alice, self.program_member, self.scope_b)

        res = self.client.get("/v2/rbac/admin/hub-users", headers=self.admin)
        by_email = {row["email"]: row for row in res.json()["data"]}
        pairs = {
            (a["role_id"], a["scope_id"]) for a in by_email[self.alice]["assignments"]
        }
        self.assertEqual(pairs, {(self.program_lead, self.scope_a), (self.program_member, self.scope_b)})


class TestAdminCreateHubUser(RbacAssignmentsApiFixture):
    """POST /v2/rbac/admin/hub-users (owner decision, 2026-10-04) — admin
    pre-registration, chiefly so external emails can pass the login gate."""

    URL = "/v2/rbac/admin/hub-users"

    def test_non_admin_is_forbidden(self) -> None:
        res = self.client.post(
            self.URL, json={"email": "guest@partner.example", "name": "Guest"}, headers=self.user("alice@renphil.org")
        )
        self.assertEqual(res.status_code, 403)

    def test_creates_normalized_not_yet_signed_in_row_and_lists_it(self) -> None:
        """`has_signed_in` False = "has not signed in yet" (owner, 2026-10-09)."""
        res = self.client.post(
            self.URL, json={"email": "  Guest@Partner.EXAMPLE ", "name": "  Guest Person "}, headers=self.admin
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()["data"]
        self.assertEqual(data["email"], "guest@partner.example")
        self.assertEqual(data["name"], "Guest Person")
        self.assertFalse(data["has_signed_in"])
        self.assertEqual(data["assignments"], [])

        listed = self.client.get(self.URL, headers=self.admin).json()["data"]
        self.assertIn("guest@partner.example", {u["email"] for u in listed})

    def test_duplicate_email_is_409_case_insensitively(self) -> None:
        self.provision(self.user("bob@renphil.org"))
        res = self.client.post(self.URL, json={"email": "BOB@renphil.org", "name": "Bob"}, headers=self.admin)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "hub_user_exists")

    def test_name_is_required_and_not_blank(self) -> None:
        for body in ({"email": "g@partner.example"}, {"email": "g@partner.example", "name": "   "}):
            res = self.client.post(self.URL, json=body, headers=self.admin)
            self.assertEqual(res.status_code, 422, body)

    def test_invalid_email_is_422(self) -> None:
        res = self.client.post(self.URL, json={"email": "not-an-email", "name": "X"}, headers=self.admin)
        self.assertEqual(res.status_code, 422)

    def test_created_user_can_then_be_granted_a_role(self) -> None:
        """The point of pre-registering: no `target_not_provisioned` anymore."""
        self.build_org()
        self.client.post(self.URL, json={"email": "guest@partner.example", "name": "Guest"}, headers=self.admin)
        self.grant(self.admin, "guest@partner.example", self.program_member, self.scope_a)

    def test_an_hr_rows_alias_email_is_already_taken(self) -> None:
        """Merge (2026-10): the alias is a login email too."""
        self.db.add(UserV2(name="Staff", work_email="staff@renphil.org", alias_email="s@partner.example"))
        self.db.commit()
        res = self.client.post(self.URL, json={"email": "S@partner.example", "name": "S"}, headers=self.admin)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "hub_user_exists")

    def test_first_sign_in_flips_the_added_user_to_active(self) -> None:
        self.client.post(self.URL, json={"email": "guest@partner.example", "name": "Guest"}, headers=self.admin)
        self.provision(self.user("guest@partner.example"))
        self.db.expire_all()
        rows = self.db.query(UserV2).filter(UserV2.matches_email("guest@partner.example")).all()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].has_signed_in)
        self.assertEqual(rows[0].name, "Guest")


class TestAdminUpdateAssignment(RbacAssignmentsApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = "alice@renphil.org"
        self.bob = "bob@renphil.org"
        self.provision(self.user(self.alice))
        self.provision(self.user(self.bob))

    def patch(self, assignment_id: int, body: dict, headers: dict | None = None):
        return self.client.patch(
            f"/v2/rbac/admin/assignments/{assignment_id}",
            json=body,
            headers=headers if headers is not None else self.admin,
        )

    def test_non_admin_is_forbidden(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(
            assignment["id"], {"scope_id": self.scope_b}, headers=self.user(self.alice)
        )
        self.assertEqual(res.status_code, 403)

    def test_no_fields_is_400(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {})
        self.assertEqual(res.status_code, 400)

    def test_assignment_not_found_is_404(self) -> None:
        res = self.patch(999999, {"scope_id": self.scope_b})
        self.assertEqual(res.status_code, 404)

    def test_role_not_found_is_404(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {"role_id": 999999})
        self.assertEqual(res.status_code, 404)

    def test_scope_not_found_is_404(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {"scope_id": 999999})
        self.assertEqual(res.status_code, 404)

    def test_update_scope_only_keeps_role(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {"scope_id": self.scope_b})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["id"], assignment["id"])
        self.assertEqual(data["role_id"], self.program_lead)
        self.assertEqual(data["scope_id"], self.scope_b)
        # Provenance is untouched by an in-place edit. Compared as instants,
        # not raw strings — SQLite round-trips a stored aware datetime back
        # as naive, so the re-serialized value legitimately drops the "Z"
        # suffix the original in-memory response carried.
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        self.assertEqual(parse(data["created_at"]), parse(assignment["created_at"]))
        self.assertEqual(data["granted_by_email"], assignment["granted_by_email"])

    def test_update_role_only_keeps_scope(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {"role_id": self.function_lead})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["role_id"], self.function_lead)
        self.assertEqual(data["scope_id"], self.scope_a)

    def test_update_both_at_once(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(
            assignment["id"], {"role_id": self.function_lead, "scope_id": self.scope_b}
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["role_id"], self.function_lead)
        self.assertEqual(data["scope_id"], self.scope_b)

    def test_colliding_with_another_existing_pair_is_409(self) -> None:
        self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        target = self.grant(self.admin, self.alice, self.function_lead, self.scope_b)

        res = self.patch(
            target["id"], {"role_id": self.program_lead, "scope_id": self.scope_a}
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "duplicate_assignment")

    def test_updating_to_its_own_current_values_is_not_a_collision(self) -> None:
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(
            assignment["id"], {"role_id": self.program_lead, "scope_id": self.scope_a}
        )
        self.assertEqual(res.status_code, 200, res.text)

    def test_public_role_is_refused(self) -> None:
        any_role = self.make_role("any_role_2", "Any Role 2", public=True)
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {"role_id": any_role})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")

    @mock.patch("app.routers.rbac_assignments.TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT", False)
    def test_public_scope_is_refused(self) -> None:
        any_scope = self.make_scope("any_scope_2", "Any Scope 2", public=True)
        assignment = self.grant(self.admin, self.alice, self.program_lead, self.scope_a)
        res = self.patch(assignment["id"], {"scope_id": any_scope})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_not_assignable")

    # Since phase 3 the caller of this admin-only endpoint holds a real admin
    # row, so the "last" admin row is always the caller's own seeded one.

    def test_moving_the_last_hub_admin_off_the_role_is_refused(self) -> None:
        res = self.patch(self.admin_assignment_id, {"role_id": self.program_lead})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "last_hub_admin")

    def test_moving_the_last_hub_admin_off_the_universal_scope_is_refused(self) -> None:
        res = self.patch(self.admin_assignment_id, {"scope_id": self.scope_a})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "last_hub_admin")

    def test_a_second_hub_admin_makes_moving_the_first_one_off_safe(self) -> None:
        self.grant(self.admin, self.alice, self.hub_admin_role, self.all_scopes)

        res = self.patch(self.admin_assignment_id, {"role_id": self.program_lead})
        self.assertEqual(res.status_code, 200, res.text)

    def test_reaffirming_the_last_hub_admin_unchanged_is_not_blocked(self) -> None:
        """Regression guard: the admin-floor check must key off whether the
        EDIT removes admin status, not off "is this the only admin row" in
        isolation — otherwise the last admin could never be touched again at
        all, even to leave them exactly as they are."""
        res = self.patch(
            self.admin_assignment_id,
            {"role_id": self.hub_admin_role, "scope_id": self.all_scopes},
        )
        self.assertEqual(res.status_code, 200, res.text)

    def test_editing_an_ordinary_assignment_is_unaffected_by_the_admin_floor(self) -> None:
        self.grant(self.admin, self.alice, self.hub_admin_role, self.all_scopes)
        ordinary = self.grant(self.admin, self.bob, self.program_member, self.scope_a)

        res = self.patch(ordinary["id"], {"role_id": self.function_member})
        self.assertEqual(res.status_code, 200, res.text)
