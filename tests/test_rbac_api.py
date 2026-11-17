"""Access Management router — plan_access_control_schema_2026-08-22.md §3, §5.

Same in-memory-SQLite router fixture as tests/test_threads.py: a real
FastAPI TestClient over a real (SQLite) v2 schema with JWTs minted locally,
so these exercise router + service + ORM together rather than a mocked
service.

Covers the v1 surface only — role and scope definitions plus the two
hierarchies. There is no assignments endpoint yet, so the §6 delegation rule
is not exercised here; the graph rules it will be built on are covered in
tests/test_rbac_graph.py.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-rbac-api-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.main import create_app  # noqa: E402

HUB_ADMIN = "Hub Admin"
ADMIN_EMAIL = "admin@renphil.org"


class RbacApiFixture(unittest.TestCase):
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

        # Mirrors app/db_v2/database.py's real get_db_v2, INCLUDING its
        # `finally: db.close()`. The one-liner the other fixtures use
        # (`lambda: (yield self._session_factory())`) never closes, and on
        # this StaticPool every session shares one SQLite connection — so a
        # request that raises leaves its uncommitted INSERTs pending on that
        # connection, and the next request's commit adopts them. Under that
        # override a handler could fail to roll back and no test could tell:
        # TestCreateWithParents' "creates NOTHING" cases fail against it and
        # pass against this. The leak is repo-wide (test_nav_tabs,
        # test_threads, test_rbac_assignments_api, test_airtable_widget_cache
        # all have it); only this file is corrected here, because only this
        # file asserts on rollback.
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- auth -----------------------------------------------------------

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
        roles). Lazy, so tests that never act as admin seed nothing.

        There can be only one `hub_admin` key and one universal scope, so
        `make_role("hub_admin", ...)` and `make_scope(..., universal=True)`
        return these seeded rows instead of creating new ones."""
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
        self.seeded_hub_admin_role = role.id
        self.seeded_universal_scope = scope.id
        self.admin_assignment_id = assignment.id

    @property
    def member(self) -> dict:
        return self._auth("member@renphil.org", ["Hub Member"])

    # -- builders -------------------------------------------------------

    def make_role(
        self, key: str, name: str, depth: int | None = None, public: bool = False
    ) -> int:
        """`depth` is omitted from the body unless a test explicitly wants
        one — that is what the create form sends now that the field is out of
        the UI, so it is what the tests should exercise by default. Same for
        `is_public`, which only the ⊥ tests send."""
        if key == "hub_admin" and depth is None and not public:
            self._ensure_admin()
            return self.seeded_hub_admin_role
        body: dict = {"key": key, "name": name}
        if depth is not None:
            body["depth"] = depth
        if public:
            body["is_public"] = True
        res = self.client.post("/v2/rbac/roles", json=body, headers=self.admin)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]["id"]

    def make_scope(
        self, key: str, name: str, universal: bool = False, public: bool = False
    ) -> int:
        if universal and not public:
            self._ensure_admin()
            return self.seeded_universal_scope
        body: dict = {"key": key, "name": name, "is_universal": universal}
        if public:
            body["is_public"] = True
        res = self.client.post("/v2/rbac/scopes", json=body, headers=self.admin)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]["id"]

    def code_of(self, res) -> str:
        return res.json()["detail"]["code"]


# ---------------------------------------------------------
# Authorization
# ---------------------------------------------------------


class TestAuthorization(RbacApiFixture):
    def test_unauthenticated_is_rejected(self) -> None:
        self.assertIn(self.client.get("/v2/rbac/roles").status_code, (401, 403))

    def test_any_authenticated_user_can_read(self) -> None:
        """Reads are open on purpose: half a hierarchy is unreadable, and
        role names are not the sensitive part — assignments are."""
        self.make_role("hub_admin", "Hub Admin")
        res = self.client.get("/v2/rbac/roles", headers=self.member)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["data"]), 1)

    def test_non_admin_cannot_write(self) -> None:
        role_id = self.make_role("hub_admin", "Hub Admin")
        other_id = self.make_role("program_lead", "Program Lead")

        attempts = [
            self.client.post(
                "/v2/rbac/roles",
                json={"key": "sneaky", "name": "Sneaky"},
                headers=self.member,
            ),
            self.client.patch(
                f"/v2/rbac/roles/{role_id}", json={"name": "Renamed"}, headers=self.member
            ),
            self.client.delete(f"/v2/rbac/roles/{role_id}", headers=self.member),
            self.client.post(
                f"/v2/rbac/roles/{role_id}/children/{other_id}", headers=self.member
            ),
            self.client.post(
                "/v2/rbac/scopes", json={"key": "s", "name": "S"}, headers=self.member
            ),
        ]
        for res in attempts:
            self.assertEqual(res.status_code, 403, res.text)


# ---------------------------------------------------------
# Roles
# ---------------------------------------------------------


class TestRoles(RbacApiFixture):
    def test_create_and_read_back(self) -> None:
        role_id = self.make_role("program_lead", "Program Lead")
        res = self.client.get(f"/v2/rbac/roles/{role_id}", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["key"], "program_lead")
        # Created without one, and that is the normal path now: the field is
        # optional, nullable, and absent from the UI.
        self.assertIsNone(data["depth"])
        self.assertEqual(data["parent_ids"], [])
        self.assertEqual(data["child_ids"], [])

    def test_depth_round_trips_when_sent(self) -> None:
        """It stays on the wire so the rank rule can be restored without a
        schema migration — so it has to actually persist."""
        role_id = self.make_role("program_lead", "Program Lead", depth=200)
        res = self.client.get(f"/v2/rbac/roles/{role_id}", headers=self.admin)
        self.assertEqual(res.json()["data"]["depth"], 200)

    def test_depth_can_be_patched_and_cleared(self) -> None:
        role_id = self.make_role("program_lead", "Program Lead", depth=200)

        set_res = self.client.patch(
            f"/v2/rbac/roles/{role_id}", json={"depth": 250}, headers=self.admin
        )
        self.assertEqual(set_res.status_code, 200, set_res.text)
        self.assertEqual(set_res.json()["data"]["depth"], 250)

        # Explicit null clears it; the three-way that `description` uses.
        clear_res = self.client.patch(
            f"/v2/rbac/roles/{role_id}", json={"depth": None}, headers=self.admin
        )
        self.assertIsNone(clear_res.json()["data"]["depth"])

        # Omitting it leaves it alone rather than clearing it.
        self.client.patch(
            f"/v2/rbac/roles/{role_id}", json={"depth": 300}, headers=self.admin
        )
        untouched = self.client.patch(
            f"/v2/rbac/roles/{role_id}", json={"name": "Renamed"}, headers=self.admin
        )
        self.assertEqual(untouched.json()["data"]["depth"], 300)

    def test_rank_is_no_longer_accepted(self) -> None:
        """extra="forbid" turns the old field name into a 422 rather than a
        silently ignored no-op — worth pinning so a stale client fails loudly."""
        res = self.client.post(
            "/v2/rbac/roles",
            json={"key": "stale", "name": "Stale", "rank": 200},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422)

    def test_duplicate_key_and_name_are_conflicts(self) -> None:
        self.make_role("program_lead", "Program Lead")

        dup_key = self.client.post(
            "/v2/rbac/roles",
            json={"key": "program_lead", "name": "Something Else"},
            headers=self.admin,
        )
        self.assertEqual(dup_key.status_code, 409)
        self.assertEqual(self.code_of(dup_key), "duplicate_key")

        dup_name = self.client.post(
            "/v2/rbac/roles",
            json={"key": "other_key", "name": "Program Lead"},
            headers=self.admin,
        )
        self.assertEqual(dup_name.status_code, 409)
        self.assertEqual(self.code_of(dup_name), "duplicate_name")

    def test_key_must_be_snake_case(self) -> None:
        res = self.client.post(
            "/v2/rbac/roles",
            json={"key": "Program Lead", "name": "Program Lead"},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422)

    def test_key_is_immutable(self) -> None:
        """extra="forbid" is what enforces this — the whole point of `key`
        is being the handle that survives a rename."""
        role_id = self.make_role("program_lead", "Program Lead")
        res = self.client.patch(
            f"/v2/rbac/roles/{role_id}", json={"key": "renamed"}, headers=self.admin
        )
        self.assertEqual(res.status_code, 422)

    def test_rename_and_clear_description(self) -> None:
        role_id = self.make_role("program_lead", "Program Lead")
        self.client.patch(
            f"/v2/rbac/roles/{role_id}", json={"description": "Leads a program"},
            headers=self.admin,
        )
        res = self.client.patch(
            f"/v2/rbac/roles/{role_id}",
            json={"name": "Programme Lead", "description": None},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["name"], "Programme Lead")
        self.assertIsNone(res.json()["data"]["description"])

    def test_missing_role_is_404(self) -> None:
        self.assertEqual(
            self.client.get("/v2/rbac/roles/9999", headers=self.admin).status_code, 404
        )
        self.assertEqual(
            self.client.delete("/v2/rbac/roles/9999", headers=self.admin).status_code, 404
        )

    def test_system_role_cannot_be_deleted(self) -> None:
        role_id = self.make_role("hub_admin", "Hub Admin")
        self.db.query(RoleV2).filter(RoleV2.id == role_id).update({"is_system": True})
        self.db.commit()

        res = self.client.delete(f"/v2/rbac/roles/{role_id}", headers=self.admin)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "system_role")

    def test_delete_role_cascades_its_edges(self) -> None:
        lead = self.make_role("program_lead", "Program Lead")
        member = self.make_role("program_member", "Program Member")
        self.client.post(f"/v2/rbac/roles/{lead}/children/{member}", headers=self.admin)

        self.assertEqual(
            self.client.delete(f"/v2/rbac/roles/{lead}", headers=self.admin).status_code, 204
        )
        res = self.client.get(f"/v2/rbac/roles/{member}", headers=self.admin)
        self.assertEqual(res.json()["data"]["parent_ids"], [])


# ---------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------


class TestRoleHierarchy(RbacApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.hub_admin = self.make_role("hub_admin", "Hub Admin")
        self.program_lead = self.make_role("program_lead", "Program Lead")
        self.function_lead = self.make_role("function_lead", "Function Lead")
        self.program_member = self.make_role("program_member", "Program Member")
        self.hub_member = self.make_role("hub_member", "Hub Member")

    def add(self, parent: int, child: int):
        return self.client.post(
            f"/v2/rbac/roles/{parent}/children/{child}", headers=self.admin
        )

    def test_edge_appears_on_both_endpoints(self) -> None:
        self.assertEqual(self.add(self.program_lead, self.program_member).status_code, 204)
        roles = {r["id"]: r for r in self.client.get("/v2/rbac/roles", headers=self.admin).json()["data"]}
        self.assertEqual(roles[self.program_lead]["child_ids"], [self.program_member])
        self.assertEqual(roles[self.program_member]["parent_ids"], [self.program_lead])

    def test_the_landmine_is_now_a_204(self) -> None:
        """Program Member -> Function Lead used to be 409 `rank_violation`.
        It is acyclic, so rank was the only thing that ever caught it, and
        rank is disabled — the API now accepts it. Mirrors
        test_rbac_graph.TestRoleGraph.test_the_landmine_is_now_ALLOWED at the
        HTTP layer."""
        res = self.add(self.program_member, self.function_lead)
        self.assertEqual(res.status_code, 204, res.text)

    def test_cycle_is_a_409(self) -> None:
        """What replaced `rank_violation` as the role graph's guard rail."""
        self.assertEqual(self.add(self.program_lead, self.program_member).status_code, 204)
        res = self.add(self.program_member, self.program_lead)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "cycle")
        detail = res.json()["detail"]
        self.assertEqual(detail["parent_role_id"], self.program_member)
        self.assertEqual(detail["child_role_id"], self.program_lead)

    def test_transitive_cycle_is_a_409(self) -> None:
        self.add(self.hub_admin, self.program_lead)
        self.add(self.program_lead, self.program_member)
        res = self.add(self.program_member, self.hub_admin)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "cycle")

    def test_self_and_duplicate_edges(self) -> None:
        self.assertEqual(
            self.code_of(self.add(self.program_lead, self.program_lead)), "self_edge"
        )
        self.add(self.program_lead, self.program_member)
        self.assertEqual(
            self.code_of(self.add(self.program_lead, self.program_member)), "duplicate_edge"
        )

    def test_remove_edge(self) -> None:
        self.add(self.program_lead, self.program_member)
        res = self.client.delete(
            f"/v2/rbac/roles/{self.program_lead}/children/{self.program_member}",
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 204)

        again = self.client.delete(
            f"/v2/rbac/roles/{self.program_lead}/children/{self.program_member}",
            headers=self.admin,
        )
        self.assertEqual(self.code_of(again), "edge_not_found")

    def test_depth_change_cannot_conflict_with_an_edge(self) -> None:
        """Replaces the two `rank_change_conflict` tests. A rank edit used to
        be able to invert an existing edge and return 409 with a `conflicts[]`
        array; `depth` constrains nothing, so the identical edit is now a
        plain 200 no matter what edges exist."""
        self.add(self.hub_admin, self.program_lead)
        self.add(self.program_lead, self.program_member)

        res = self.client.patch(
            f"/v2/rbac/roles/{self.program_lead}", json={"depth": 350}, headers=self.admin
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["depth"], 350)

    def test_multi_parent(self) -> None:
        self.add(self.program_member, self.hub_member)
        self.add(self.function_lead, self.hub_member)
        res = self.client.get(f"/v2/rbac/roles/{self.hub_member}", headers=self.admin)
        self.assertEqual(
            res.json()["data"]["parent_ids"],
            sorted([self.program_member, self.function_lead]),
        )


# ---------------------------------------------------------
# Scopes
# ---------------------------------------------------------


class TestScopes(RbacApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.all_scopes = self.make_scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = self.make_scope("scope_a", "Scope A")
        self.scope_b = self.make_scope("scope_b", "Scope B")
        self.onboarding = self.make_scope("onboarding", "Onboarding")

    def add(self, parent: int, child: int):
        return self.client.post(
            f"/v2/rbac/scopes/{parent}/children/{child}", headers=self.admin
        )

    def test_only_one_universal_scope(self) -> None:
        res = self.client.post(
            "/v2/rbac/scopes",
            json={"key": "all_two", "name": "All Scopes 2", "is_universal": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "universal_scope_exists")

    def test_is_universal_cannot_be_changed_after_creation(self) -> None:
        res = self.client.patch(
            f"/v2/rbac/scopes/{self.scope_a}",
            json={"is_universal": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422)

    def test_universal_scope_rejects_edges_both_ways(self) -> None:
        for parent, child in (
            (self.all_scopes, self.scope_a),
            (self.scope_a, self.all_scopes),
        ):
            res = self.add(parent, child)
            self.assertEqual(res.status_code, 409)
            self.assertEqual(self.code_of(res), "universal_scope_edge")

    def test_composite_scope_holds_several_children(self) -> None:
        self.assertEqual(self.add(self.onboarding, self.scope_a).status_code, 204)
        self.assertEqual(self.add(self.onboarding, self.scope_b).status_code, 204)
        res = self.client.get(f"/v2/rbac/scopes/{self.onboarding}", headers=self.admin)
        self.assertEqual(
            res.json()["data"]["child_ids"], sorted([self.scope_a, self.scope_b])
        )

    def test_transitive_cycle_is_a_409(self) -> None:
        self.add(self.scope_a, self.scope_b)
        self.add(self.scope_b, self.onboarding)
        res = self.add(self.onboarding, self.scope_a)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "cycle")

    def test_scope_can_sit_in_two_containers(self) -> None:
        all_programs = self.make_scope("all_programs", "All Programs")
        self.add(all_programs, self.scope_a)
        self.add(self.onboarding, self.scope_a)
        res = self.client.get(f"/v2/rbac/scopes/{self.scope_a}", headers=self.admin)
        self.assertEqual(
            res.json()["data"]["parent_ids"], sorted([all_programs, self.onboarding])
        )

    def test_universal_scope_lists_first(self) -> None:
        data = self.client.get("/v2/rbac/scopes", headers=self.admin).json()["data"]
        self.assertTrue(data[0]["is_universal"])

    def test_system_scope_cannot_be_deleted(self) -> None:
        self.db.query(ScopeV2).filter(ScopeV2.id == self.all_scopes).update(
            {"is_system": True}
        )
        self.db.commit()
        res = self.client.delete(f"/v2/rbac/scopes/{self.all_scopes}", headers=self.admin)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "system_scope")


# ---------------------------------------------------------
# The ⊥ flags over HTTP — "Any Role" / "Any Scope"
# (plan_access_control_algorithm_2026-08-27.md §4.4)
# ---------------------------------------------------------


class TestBottomFlags(RbacApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.all_scopes = self.make_scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = self.make_scope("scope_a", "Scope A")
        self.program_lead = self.make_role("program_lead", "Program Lead")

    # -- the flag on the wire -------------------------------------------

    def test_ordinary_rows_are_not_public(self) -> None:
        """The field is on every response, defaulting false, so the UI can
        label the two lattice ends without a second request."""
        role = self.client.get(f"/v2/rbac/roles/{self.program_lead}", headers=self.admin)
        scope = self.client.get(f"/v2/rbac/scopes/{self.scope_a}", headers=self.admin)
        self.assertFalse(role.json()["data"]["is_public"])
        self.assertFalse(scope.json()["data"]["is_public"])

    def test_public_role_and_scope_round_trip(self) -> None:
        any_role = self.make_role("any_role", "Any Role", public=True)
        any_scope = self.make_scope("any_scope", "Any Scope", public=True)

        role = self.client.get(f"/v2/rbac/roles/{any_role}", headers=self.admin)
        self.assertTrue(role.json()["data"]["is_public"])
        scope = self.client.get(f"/v2/rbac/scopes/{any_scope}", headers=self.admin)
        self.assertTrue(scope.json()["data"]["is_public"])
        # Not accidentally the other end of the lattice too.
        self.assertFalse(scope.json()["data"]["is_universal"])

    # -- at most one, and never both ------------------------------------

    def test_only_one_public_role(self) -> None:
        self.make_role("any_role", "Any Role", public=True)
        res = self.client.post(
            "/v2/rbac/roles",
            json={"key": "any_role_2", "name": "Any Role 2", "is_public": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_role_exists")

    def test_only_one_public_scope(self) -> None:
        self.make_scope("any_scope", "Any Scope", public=True)
        res = self.client.post(
            "/v2/rbac/scopes",
            json={"key": "any_2", "name": "Any Scope 2", "is_universal": False, "is_public": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_scope_exists")

    def test_a_scope_cannot_be_both_ends_of_the_lattice(self) -> None:
        """"All Scopes" held in an assignment covers every scope; "Any
        Scope" written on an object is reachable from every scope. One row
        cannot be both, and asking for it is a named 409 rather than the
        IntegrityError the CHECK would otherwise produce."""
        res = self.client.post(
            "/v2/rbac/scopes",
            json={"key": "both", "name": "Both Ends", "is_universal": True, "is_public": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "universal_and_public")

    def test_the_refused_both_ends_scope_is_not_created(self) -> None:
        """The check runs before the INSERT, so there is nothing to unwind —
        asserted anyway, since the fixture's dependency override closes its
        session and can therefore actually catch a missing rollback."""
        self.client.post(
            "/v2/rbac/scopes",
            json={"key": "both", "name": "Both Ends", "is_universal": True, "is_public": True},
            headers=self.admin,
        )
        names = [
            s["name"] for s in self.client.get("/v2/rbac/scopes", headers=self.admin).json()["data"]
        ]
        self.assertNotIn("Both Ends", names)

    # -- immutable ------------------------------------------------------

    def test_is_public_cannot_be_changed_after_creation(self) -> None:
        """Flipping it silently rewrites what every existing grant reaches,
        in whichever direction it is flipped. `extra="forbid"` makes the
        attempt a 422 rather than a silently ignored no-op — same treatment
        `is_universal` already gets."""
        for path, body in (
            (f"/v2/rbac/roles/{self.program_lead}", {"is_public": True}),
            (f"/v2/rbac/scopes/{self.scope_a}", {"is_public": True}),
        ):
            res = self.client.patch(path, json=body, headers=self.admin)
            self.assertEqual(res.status_code, 422, res.text)

    # -- barred from the edge tables ------------------------------------

    def test_public_role_rejects_edges_both_ways(self) -> None:
        any_role = self.make_role("any_role", "Any Role", public=True)
        for parent, child in ((any_role, self.program_lead), (self.program_lead, any_role)):
            res = self.client.post(
                f"/v2/rbac/roles/{parent}/children/{child}", headers=self.admin
            )
            self.assertEqual(res.status_code, 409, res.text)
            self.assertEqual(self.code_of(res), "public_role_edge")

    def test_public_scope_rejects_edges_both_ways(self) -> None:
        any_scope = self.make_scope("any_scope", "Any Scope", public=True)
        for parent, child in ((any_scope, self.scope_a), (self.scope_a, any_scope)):
            res = self.client.post(
                f"/v2/rbac/scopes/{parent}/children/{child}", headers=self.admin
            )
            self.assertEqual(res.status_code, 409, res.text)
            self.assertEqual(self.code_of(res), "public_scope_edge")

    def test_creating_a_public_role_with_parents_is_refused_whole(self) -> None:
        """Same all-or-nothing contract `is_universal` + `parent_ids` has:
        the edge is illegal, so the row goes back with it rather than
        leaving an unattached ⊥ behind."""
        res = self.client.post(
            "/v2/rbac/roles",
            json={
                "key": "any_role",
                "name": "Any Role",
                "is_public": True,
                "parent_ids": [self.program_lead],
            },
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_role_edge")
        names = [
            r["name"] for r in self.client.get("/v2/rbac/roles", headers=self.admin).json()["data"]
        ]
        self.assertNotIn("Any Role", names)

    def test_creating_a_public_scope_with_parents_is_refused_whole(self) -> None:
        res = self.client.post(
            "/v2/rbac/scopes",
            json={
                "key": "any_scope",
                "name": "Any Scope",
                "is_universal": False,
                "is_public": True,
                "parent_ids": [self.scope_a],
            },
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "public_scope_edge")
        names = [
            s["name"] for s in self.client.get("/v2/rbac/scopes", headers=self.admin).json()["data"]
        ]
        self.assertNotIn("Any Scope", names)

    # -- ordering -------------------------------------------------------

    def test_the_two_ends_bracket_the_scope_list(self) -> None:
        """Universal first, public last, ordinary scopes by name between.

        Not cosmetic: sorted by name, "Any Scope" would land directly above
        "All Scopes" — the one place it must not be, since on an object
        grant those two mean close to opposite things (§9)."""
        self.make_scope("any_scope", "Any Scope", public=True)
        data = self.client.get("/v2/rbac/scopes", headers=self.admin).json()["data"]
        self.assertTrue(data[0]["is_universal"])
        self.assertTrue(data[-1]["is_public"])
        self.assertEqual([s["is_public"] for s in data[:-1]], [False] * (len(data) - 1))


# ---------------------------------------------------------
# Create-with-parents  (the per-leaf "Add role"/"Add scope" affordance)
# ---------------------------------------------------------


class TestCreateWithParents(RbacApiFixture):
    """`parent_ids` on POST /roles and POST /scopes.

    The point of the field is ATOMICITY — the alternative the UI used to have
    was create-then-edge from the client, which strands an unattached node
    when the second call fails. So the failure cases below matter more than
    the happy ones: each asserts not just the 409 but that nothing was left
    behind, which is the only reason this field exists rather than two calls.
    """

    def role_names(self) -> list[str]:
        return [r["name"] for r in self.client.get("/v2/rbac/roles", headers=self.admin).json()["data"]]

    def scope_names(self) -> list[str]:
        return [s["name"] for s in self.client.get("/v2/rbac/scopes", headers=self.admin).json()["data"]]

    def create_role(self, key: str, name: str, parent_ids: list[int]):
        return self.client.post(
            "/v2/rbac/roles",
            json={"key": key, "name": name, "parent_ids": parent_ids},
            headers=self.admin,
        )

    # -- roles ----------------------------------------------------------

    def test_role_is_created_and_attached_in_one_call(self) -> None:
        parent = self.make_role("program_lead", "Program Lead")
        res = self.create_role("program_member", "Program Member", [parent])
        self.assertEqual(res.status_code, 201, res.text)

        child = res.json()["data"]
        self.assertEqual(child["parent_ids"], [parent])
        self.assertEqual(child["child_ids"], [])

        # ...and from the parent's side, so this is a real edge row rather
        # than the response echoing the request back.
        parent_row = self.client.get(f"/v2/rbac/roles/{parent}", headers=self.admin).json()["data"]
        self.assertEqual(parent_row["child_ids"], [child["id"]])

    def test_several_parents_at_once(self) -> None:
        a = self.make_role("ops_lead", "Operations Lead")
        b = self.make_role("legal_lead", "Legal Lead")
        res = self.create_role("finance_lead", "Finance Lead", [a, b])
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["data"]["parent_ids"], sorted([a, b]))

    def test_repeated_parent_id_is_deduped(self) -> None:
        """Would otherwise hit the edge table's composite primary key and
        surface as a 500 rather than a 201."""
        parent = self.make_role("program_lead", "Program Lead")
        res = self.create_role("program_member", "Program Member", [parent, parent])
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["data"]["parent_ids"], [parent])

    def test_omitting_parent_ids_is_unchanged(self) -> None:
        role_id = self.make_role("standalone", "Standalone")
        data = self.client.get(f"/v2/rbac/roles/{role_id}", headers=self.admin).json()["data"]
        self.assertEqual(data["parent_ids"], [])

    def test_unknown_parent_creates_NOTHING(self) -> None:
        res = self.create_role("orphan", "Orphan", [9999])
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "role_not_found")
        # The role row was flushed before the edge was attempted. If the
        # transaction did not unwind, this name would be taken.
        self.assertNotIn("Orphan", self.role_names())

    def test_second_parent_failing_rolls_back_the_first_edge_too(self) -> None:
        parent = self.make_role("program_lead", "Program Lead")
        res = self.create_role("program_member", "Program Member", [parent, 9999])
        self.assertEqual(res.status_code, 409, res.text)
        self.assertNotIn("Program Member", self.role_names())
        parent_row = self.client.get(f"/v2/rbac/roles/{parent}", headers=self.admin).json()["data"]
        self.assertEqual(parent_row["child_ids"], [])

    def test_duplicate_key_still_wins_over_parent_validation(self) -> None:
        """Key checks run before anything is inserted, so a request that is
        wrong in both ways reports the key — not a confusing edge error about
        a role that was never going to exist."""
        parent = self.make_role("program_lead", "Program Lead")
        res = self.create_role("program_lead", "Something Else", [parent])
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "duplicate_key")

    def test_too_many_parents_is_a_422(self) -> None:
        parent = self.make_role("program_lead", "Program Lead")
        res = self.create_role("program_member", "Program Member", [parent] * 33)
        self.assertEqual(res.status_code, 422, res.text)

    # -- scopes ---------------------------------------------------------

    def test_scope_is_created_and_attached_in_one_call(self) -> None:
        parent = self.make_scope("programs", "Programs")
        res = self.client.post(
            "/v2/rbac/scopes",
            json={"key": "grants", "name": "Grants", "parent_ids": [parent]},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["data"]["parent_ids"], [parent])

    def test_new_scope_cannot_be_universal_AND_have_a_parent(self) -> None:
        parent = self.make_scope("programs", "Programs")
        res = self.client.post(
            "/v2/rbac/scopes",
            json={
                "key": "everything",
                "name": "Everything",
                "is_universal": True,
                "parent_ids": [parent],
            },
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "universal_scope_edge")
        # Critically: it did not become the universal scope on the way out.
        self.assertNotIn("Everything", self.scope_names())

    def test_scope_cannot_be_created_under_the_universal_scope(self) -> None:
        universal = self.make_scope("all_scopes", "All Scopes", universal=True)
        res = self.client.post(
            "/v2/rbac/scopes",
            json={"key": "grants", "name": "Grants", "parent_ids": [universal]},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "universal_scope_edge")
        self.assertNotIn("Grants", self.scope_names())

    def test_non_admin_cannot_create_with_parents(self) -> None:
        parent = self.make_role("program_lead", "Program Lead")
        res = self.client.post(
            "/v2/rbac/roles",
            json={"key": "sneaky", "name": "Sneaky", "parent_ids": [parent]},
            headers=self.member,
        )
        self.assertEqual(res.status_code, 403, res.text)


if __name__ == "__main__":
    unittest.main()
