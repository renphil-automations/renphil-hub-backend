"""The `is_hub_admin` resolver — plan_access_control_algorithm_2026-08-27.md
§6.8, §10 item 6 — PHASE 2 ("parallel run"), 2026-09-03; PHASE 3
(`BOOTSTRAP_ADMIN_EMAILS` replaces the JWT's Airtable role names), 2026-10-12.

Every fixture pins `BOOTSTRAP_ADMIN_EMAILS` to "" in the environment (which
beats `.env`), so a real address configured locally can never leak into a
test; the bootstrap tests set it explicitly.

Two fixtures:

  - `ResolverTestCase`: a raw SQLAlchemy session, no FastAPI at all — same
    shape as test_rbac_graph.py. Exercises `app.dependencies.is_hub_admin`
    directly against hand-built `CurrentHubUser` values. This is where the
    §0.1 decision (closure membership, not a literal row) gets its own
    named, explicit test — the one case that cannot arise from live data
    today (nothing sits above `hub_admin` in production) and therefore has
    to be built in a fixture.

  - `HubAdminResolverApiFixture`: a full FastAPI TestClient, proving the
    THREE FORMER COPIES now agree when driven through their real endpoints —
    a role/scope-definition write (`rbac.py`, gated by the
    `require_hub_admin` DEPENDENCY), an assignment write (`rbac_assignments.
    py`, gated by `assert_can_delegate`'s bypass), and a grant write
    (`resource_grants.py`, gated by `assert_can_grant`'s bypass) — plus the
    bootstrap case (`role_assignments` empty, a `BOOTSTRAP_ADMIN_EMAILS`
    address must still work, a JWT "Hub Admin" must NOT) and the widening
    phase 2 introduced (an assignment-only admin passes surfaces, including
    nav-tab mutations, that used to be JWT-only).

MUTATION-TESTED (per the task's instructions) against `is_hub_admin`'s two
`OR` branches: see `AI Docs/session_handoff_2026-09-03-hub-admin-resolver.md`
for the scratchpad harness and its results. The harness itself is not
committed (repo convention — see `session_handoff_2026-08-31-admin-floor.md`
§"mutation harness is scratchpad-only").
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-hub-admin-resolver-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.dependencies import CurrentHubUser, is_hub_admin  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402

HUB_ADMIN = "Hub Admin"

TABLES = [
    UserV2.__table__,
    RoleV2.__table__,
    ScopeV2.__table__,
    RoleEdgeV2.__table__,
    ScopeEdgeV2.__table__,
    RoleAssignmentV2.__table__,
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# BOOTSTRAP_ADMIN_EMAILS is DISABLED (2026-10-13) pending a team decision —
# see `rbac_graph_service.is_bootstrap_admin_email`. The bootstrap tests are
# skipped, not deleted; `TestBootstrapIsDisabled` pins the disabled state and
# must be removed when the feature is restored.
BOOTSTRAP_DISABLED = "BOOTSTRAP_ADMIN_EMAILS disabled pending team decision (2026-10-13)"


def _bootstrap(test: unittest.TestCase, value: str) -> None:
    """Set `BOOTSTRAP_ADMIN_EMAILS` for the rest of ``test``. The process
    environment beats `.env`, and `get_settings()` re-reads it per call."""
    patcher = mock.patch.dict(os.environ, {"BOOTSTRAP_ADMIN_EMAILS": value})
    patcher.start()
    test.addCleanup(patcher.stop)


# =========================================================================
# Pure predicate — no FastAPI, no HTTP.
# =========================================================================


class ResolverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _bootstrap(self, "")
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        BaseV2.metadata.create_all(bind=self.engine, tables=TABLES)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- builders -------------------------------------------------

    def role(self, key: str, name: str, public: bool = False) -> RoleV2:
        row = RoleV2(key=key, name=name, is_system=False, is_public=public, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def scope(self, key: str, name: str, universal: bool = False, public: bool = False) -> ScopeV2:
        row = ScopeV2(
            key=key, name=name, is_universal=universal, is_system=False, is_public=public,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def edge(self, parent: RoleV2, child: RoleV2) -> None:
        self.db.add(RoleEdgeV2(parent_role_id=parent.id, child_role_id=child.id, created_at=_now()))
        self.db.flush()

    def user(self, email: str) -> UserV2:
        row = UserV2(email=email, has_signed_in=True, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def assign(self, hub_user: UserV2, role: RoleV2, scope: ScopeV2) -> None:
        self.db.add(
            RoleAssignmentV2(user_id=hub_user.id, role_id=role.id, scope_id=scope.id, created_at=_now())
        )
        self.db.flush()

    def current(self, hub_user: UserV2, *, jwt_roles: list[str] | None = None) -> CurrentHubUser:
        return CurrentHubUser(
            info=UserInfo(email=hub_user.email, name=hub_user.email.split("@")[0], roles=jwt_roles or []),
            hub_user_id=hub_user.id,
            email=hub_user.email,
        )

    # -- the ordinary org shape every test below builds on -------------

    def _org(self):
        self.hub_admin = self.role("hub_admin", "Hub Admin")
        self.program_lead = self.role("program_lead", "Program Lead")
        self.edge(self.hub_admin, self.program_lead)  # hub_admin -> program_lead
        self.all_scopes = self.scope("all_scopes", "All Scopes", universal=True)
        self.program_a = self.scope("program_a", "Program A")


class TestJwtRoleNoLongerAdmits(ResolverTestCase):
    """Phase 3: the Airtable-sourced "Hub Admin" role name in the JWT is
    not an admin source any more. These were `TestBranchOneJwtAlone`'s
    admitting tests in phase 2; they now pin the opposite."""

    def test_jwt_role_alone_refuses(self):
        self._org()
        nobody = self.user("nobody@renphil.org")
        current = self.current(nobody, jwt_roles=[HUB_ADMIN])
        self.assertFalse(is_hub_admin(self.db, current))

    def test_jwt_role_alone_refuses_with_zero_assignment_rows_anywhere(self):
        self._org()
        self.assertEqual(self.db.query(RoleAssignmentV2).count(), 0)
        nobody = self.user("nobody@renphil.org")
        current = self.current(nobody, jwt_roles=[HUB_ADMIN])
        self.assertFalse(is_hub_admin(self.db, current))

    def test_jwt_role_plus_assignment_still_admits_via_the_assignment(self):
        """Holding both is fine — the assignment is what admits."""
        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.hub_admin, self.all_scopes)
        self.assertTrue(is_hub_admin(self.db, self.current(alice, jwt_roles=[HUB_ADMIN])))


class TestBootstrapIsDisabled(ResolverTestCase):
    """While disabled, a listed email is NOT an admin and is not in the
    "who else is an admin" set. Remove this class when restoring."""

    def test_listed_email_without_assignment_is_not_admin(self):
        from app.services.rbac_graph_service import hub_admin_user_ids

        _bootstrap(self, "root@renphil.org")
        self._org()
        root = self.user("root@renphil.org")
        self.assertFalse(is_hub_admin(self.db, self.current(root)))
        self.assertEqual(hub_admin_user_ids(self.db), set())

    def test_demoted_listed_email_loses_admin(self):
        """The case that prompted disabling it: a listed person whose admin
        assignment is gone is no longer an admin."""
        _bootstrap(self, "root@renphil.org")
        self._org()
        root = self.user("root@renphil.org")
        self.assign(root, self.hub_admin, self.all_scopes)
        self.assertTrue(is_hub_admin(self.db, self.current(root)))
        self.db.query(RoleAssignmentV2).delete()
        self.db.commit()
        self.assertFalse(is_hub_admin(self.db, self.current(root)))


@unittest.skip(BOOTSTRAP_DISABLED)
class TestBootstrapAdminEmails(ResolverTestCase):
    """Branch 1 since phase 3: `BOOTSTRAP_ADMIN_EMAILS` admits with no
    `role_assignments` row at all (§6.5's permanent floor)."""

    def test_bootstrap_email_admits_with_zero_assignment_rows_anywhere(self):
        _bootstrap(self, "root@renphil.org")
        self._org()
        self.assertEqual(self.db.query(RoleAssignmentV2).count(), 0)
        root = self.user("root@renphil.org")
        self.assertTrue(is_hub_admin(self.db, self.current(root)))

    def test_bootstrap_admits_even_with_no_hub_admin_role_or_universal_scope(self):
        """A floor that depends on the RBAC tables being intact is not a
        floor: it must hold on a database whose role/scope rows are gone."""
        _bootstrap(self, "root@renphil.org")
        root = self.user("root@renphil.org")
        self.assertTrue(is_hub_admin(self.db, self.current(root)))

    def test_matching_is_case_and_whitespace_insensitive(self):
        _bootstrap(self, " Root@RenPhil.ORG , other@renphil.org")
        self._org()
        root = self.user("root@renphil.org")
        self.assertTrue(is_hub_admin(self.db, self.current(root)))
        other = self.user("other@renphil.org")
        self.assertTrue(is_hub_admin(self.db, self.current(other)))

    def test_an_address_not_on_the_list_is_refused(self):
        _bootstrap(self, "root@renphil.org")
        self._org()
        nobody = self.user("nobody@renphil.org")
        self.assertFalse(is_hub_admin(self.db, self.current(nobody)))

    def test_a_list_entry_does_not_prefix_match(self):
        _bootstrap(self, "root@renphil.org")
        self._org()
        lookalike = self.user("root@renphil.org.evil.example")
        self.assertFalse(is_hub_admin(self.db, self.current(lookalike)))

    def test_an_empty_setting_admits_nobody(self):
        _bootstrap(self, " , ")
        self._org()
        nobody = self.user("nobody@renphil.org")
        self.assertFalse(is_hub_admin(self.db, self.current(nobody)))


class TestHubAdminUserIds(ResolverTestCase):
    """`rbac_graph_service.hub_admin_user_ids` — "who ELSE is an admin" —
    must name exactly the people `is_hub_admin` admits."""

    @unittest.skip(BOOTSTRAP_DISABLED)
    def test_union_of_assignment_admins_and_bootstrap_admins(self):
        from app.services.rbac_graph_service import hub_admin_user_ids

        _bootstrap(self, "root@renphil.org,never-signed-in@renphil.org")
        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.hub_admin, self.all_scopes)
        root = self.user("root@renphil.org")
        self.user("nobody@renphil.org")
        # never-signed-in@ has no hub_users row: absent, not an error.
        self.assertEqual(hub_admin_user_ids(self.db), {alice.id, root.id})


class TestBranchTwoAssignmentAlone(ResolverTestCase):
    """Branch 2 alone must admit — no JWT role needed."""

    def test_literal_hub_admin_assignment_alone_admits(self):
        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.hub_admin, self.all_scopes)
        current = self.current(alice, jwt_roles=[])  # no JWT role at all
        self.assertTrue(is_hub_admin(self.db, current))

    def test_shared_closures_parameter_gives_the_same_answer(self):
        from app.services.rbac_graph_service import RbacClosures

        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.hub_admin, self.all_scopes)
        current = self.current(alice, jwt_roles=[])
        closures = RbacClosures(self.db)
        self.assertTrue(is_hub_admin(self.db, current, closures=closures))


class TestNeitherSourceAdmits(ResolverTestCase):
    def test_no_jwt_role_and_no_assignment_refuses(self):
        self._org()
        nobody = self.user("nobody@renphil.org")
        current = self.current(nobody, jwt_roles=[])
        self.assertFalse(is_hub_admin(self.db, current))

    def test_hub_admin_on_a_non_universal_scope_does_not_admit(self):
        """§6.5 condition 1 / §6.8's own named example: "(Hub Admin,
        Program A)" must NOT confer hub-wide power. `effective_pairs` only
        ever reaches `(hub_admin, program_a)`, never `(hub_admin,
        all_scopes)`, because nothing walks UP into the universal scope
        (§4.2's asymmetry)."""
        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.hub_admin, self.program_a)
        current = self.current(alice, jwt_roles=[])
        self.assertFalse(is_hub_admin(self.db, current))

    def test_a_lesser_role_on_the_universal_scope_does_not_admit(self):
        """Holding the universal scope is not enough on its own — the ROLE
        half must also reach `hub_admin`. `program_lead` is a CHILD of
        `hub_admin` (inherits FROM it), not a parent, so its descendant
        closure never contains `hub_admin`."""
        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.program_lead, self.all_scopes)
        current = self.current(alice, jwt_roles=[])
        self.assertFalse(is_hub_admin(self.db, current))


class TestEmptyOrMisconfiguredTables(ResolverTestCase):
    """A fresh/misconfigured database must return False, not raise."""

    def test_no_hub_admin_role_row_at_all(self):
        self.all_scopes = self.scope("all_scopes", "All Scopes", universal=True)
        nobody = self.user("nobody@renphil.org")
        current = self.current(nobody, jwt_roles=[])
        self.assertFalse(is_hub_admin(self.db, current))

    def test_no_universal_scope_row_at_all(self):
        self.hub_admin = self.role("hub_admin", "Hub Admin")
        nobody = self.user("nobody@renphil.org")
        # Give them the role directly, on... nothing universal exists to
        # scope it to, so there is no way branch 2 could admit them.
        self.assertFalse(is_hub_admin(self.db, self.current(nobody, jwt_roles=[])))


class TestClosureMembershipDecision(ResolverTestCase):
    """§0.1, decided 2026-09-03: CLOSURE membership, not a literal row.

    This is the one case that distinguishes the two readings and cannot
    arise from live production data today (nothing sits above `hub_admin`
    in the live role DAG) — so it has to be built here. If this resolver
    were ever "simplified" back to a literal `role_assignments` row lookup,
    this test is the one that would catch it.
    """

    def test_a_role_added_ABOVE_hub_admin_inherits_admin_power(self):
        self._org()
        super_role = self.role("super_user", "Super User")
        self.edge(super_role, self.hub_admin)  # super_user -> hub_admin: parent inherits child
        alice = self.user("alice@renphil.org")
        self.assign(alice, super_role, self.all_scopes)
        current = self.current(alice, jwt_roles=[])  # no JWT role — closure only
        self.assertTrue(
            is_hub_admin(self.db, current),
            "closure membership means a role holding hub_admin in its "
            "descendant closure must confer admin power, exactly as any "
            "other grant would (§4.2) — if this fails, the resolver has "
            "regressed to a literal-row check",
        )

    def test_holding_hub_admin_directly_is_the_degenerate_case(self):
        """Where the two readings agree: holding the row literally must
        still work under closure semantics (closure always contains the
        starting node)."""
        self._org()
        alice = self.user("alice@renphil.org")
        self.assign(alice, self.hub_admin, self.all_scopes)
        current = self.current(alice, jwt_roles=[])
        self.assertTrue(is_hub_admin(self.db, current))


# =========================================================================
# API-level — all three former call sites, driven through real endpoints.
# =========================================================================


class HubAdminResolverApiFixture(unittest.TestCase):
    def setUp(self) -> None:
        _bootstrap(self, "")
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

        # Mirrors get_db_v2's real `finally: db.close()` (test_rbac_api.py's
        # precedent) rather than the leaking one-liner most other fixtures
        # use — this file's fixture writes across three different routers in
        # sequence and a swallowed rollback would be easy to miss.
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

        self._build_org()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- org shape ------------------------------------------------------

    def _role(self, key: str, name: str) -> RoleV2:
        row = RoleV2(key=key, name=name, is_system=False, is_public=False, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def _scope(self, key: str, name: str, universal: bool = False) -> ScopeV2:
        row = ScopeV2(
            key=key, name=name, is_universal=universal, is_system=False, is_public=False,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _user(self, email: str) -> UserV2:
        row = UserV2(email=email, has_signed_in=True, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def _build_org(self) -> None:
        self.hub_admin_role = self._role("hub_admin", "Hub Admin")
        self.member_role = self._role("member", "Member")
        self.all_scopes = self._scope("all_scopes", "All Scopes", universal=True)
        self.program_a = self._scope("program_a", "Program A")

        # Emails match the JWTs minted below, so `get_current_hub_user`
        # resolves rather than provisions.
        self.assignment_admin_user = self._user("assignment-admin@renphil.org")
        self.jwt_admin_user = self._user("jwt-admin@renphil.org")
        self.bootstrap_admin_user = self._user("bootstrap-admin@renphil.org")
        self.nobody_user = self._user("nobody@renphil.org")
        self.target_user = self._user("target@renphil.org")

    def assign(self, hub_user: UserV2, role: RoleV2, scope: ScopeV2) -> None:
        self.db.add(
            RoleAssignmentV2(user_id=hub_user.id, role_id=role.id, scope_id=scope.id, created_at=_now())
        )
        self.db.commit()

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
    def jwt_admin(self) -> dict:
        """The Airtable "Hub Admin" role in the JWT and nothing else — no
        `role_assignments` row. NOT an admin since phase 3."""
        return self._auth("jwt-admin@renphil.org", [HUB_ADMIN])

    @property
    def bootstrap_admin(self) -> dict:
        """Admin by `BOOTSTRAP_ADMIN_EMAILS` alone — no JWT role, no
        assignment. Call site must `_bootstrap(self, ...)` first."""
        return self._auth("bootstrap-admin@renphil.org", [])

    @property
    def assignment_admin(self) -> dict:
        """Admin by `role_assignments` alone — JWT carries no admin role.
        Call site must `self.assign(self.assignment_admin_user, ...)` first."""
        return self._auth("assignment-admin@renphil.org", [])

    @property
    def nobody(self) -> dict:
        return self._auth("nobody@renphil.org", [])


class TestAllThreeSurfacesAgree(HubAdminResolverApiFixture):
    """An assignment-only admin (no JWT role) must pass every surface the
    old three copies used to gate independently, driven through the real
    endpoints rather than the service functions directly."""

    def setUp(self) -> None:
        super().setUp()
        self.assign(self.assignment_admin_user, self.hub_admin_role, self.all_scopes)

    def test_role_definition_write(self):
        """`rbac.py`, gated by the `require_hub_admin` DEPENDENCY."""
        res = self.client.post(
            "/v2/rbac/roles", json={"key": "new_role", "name": "New Role"}, headers=self.assignment_admin,
        )
        self.assertEqual(res.status_code, 201, res.text)

    def test_assignment_write(self):
        """`rbac_assignments.py`, gated by `assert_can_delegate`'s bypass."""
        res = self.client.post(
            "/v2/rbac/assignments",
            json={
                "user_email": "target@renphil.org",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.assignment_admin,
        )
        self.assertEqual(res.status_code, 201, res.text)

    def test_grant_write(self):
        """`resource_grants.py`, gated by `assert_can_grant`'s bypass. No
        node tree is built in this fixture (out of scope for this file —
        `test_resource_grants_api.py` owns that), so this targets the `hub`
        node created implicitly by nothing... instead we assert the FAILURE
        mode is the node-existence check, never the Hub Admin gate, proving
        the gate itself was passed."""
        res = self.client.post(
            "/v2/rbac/grants",
            json={
                "node_kind": "hub",
                "node_id": 999999,
                "level": "edit",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.assignment_admin,
        )
        # A caller who fails the Hub Admin gate never reaches the
        # node-existence check at all (assert_can_grant runs first,
        # unconditionally, before any node lookup — see create_grant's own
        # docstring). Getting to "node not found" *proves* the gate passed;
        # getting a bare 403/409 tied to the gate itself would not.
        self.assertIn(res.status_code, (404, 409), res.text)
        if res.status_code == 409:
            self.assertNotEqual(res.json()["detail"]["code"], "node_not_editable", res.text)


class TestNeitherSourceRefusesAnywhere(HubAdminResolverApiFixture):
    def test_role_definition_write_refused(self):
        res = self.client.post(
            "/v2/rbac/roles", json={"key": "sneaky", "name": "Sneaky"}, headers=self.nobody,
        )
        self.assertEqual(res.status_code, 403, res.text)

    def test_assignment_write_refused_via_delegation_not_via_is_hub_admin_directly(self):
        """`rbac_assignments.py` is never gated by `require_hub_admin` at
        all (§6.1's delegation rule instead) — a caller with neither source
        still gets refused, but as `not_delegable` (409), not a bare 403.
        `is_hub_admin` returning False here just means the bypass in
        `assert_can_delegate` does not fire; `can_delegate` is what actually
        produces the refusal."""
        res = self.client.post(
            "/v2/rbac/assignments",
            json={
                "user_email": "target@renphil.org",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.nobody,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "not_delegable", res.text)

    def test_grant_write_refused_via_the_node_gate_not_via_is_hub_admin_directly(self):
        res = self.client.post(
            "/v2/rbac/grants",
            json={
                "node_kind": "hub",
                "node_id": 999999,
                "level": "edit",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.nobody,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "node_not_editable", res.text)


@unittest.skip(BOOTSTRAP_DISABLED)
class TestBootstrapStaysOpenAgainstAnEmptyAssignmentsTable(HubAdminResolverApiFixture):
    """§6.5's floor: with `role_assignments` completely empty, a
    `BOOTSTRAP_ADMIN_EMAILS` address must still pass every surface (phase 2
    proved this with a JWT admin; phase 3 moved the floor to settings)."""

    def setUp(self) -> None:
        super().setUp()
        _bootstrap(self, "bootstrap-admin@renphil.org")

    def test_role_assignments_really_is_empty(self):
        session = self._session_factory()
        try:
            self.assertEqual(session.query(RoleAssignmentV2).count(), 0)
        finally:
            session.close()

    def test_bootstrap_admin_passes_role_definition_write_against_empty_assignments(self):
        res = self.client.post(
            "/v2/rbac/roles", json={"key": "new_role", "name": "New Role"}, headers=self.bootstrap_admin,
        )
        self.assertEqual(res.status_code, 201, res.text)

    def test_bootstrap_admin_passes_assignment_write_against_empty_assignments(self):
        """The recovery path itself: writing the first admin assignment back."""
        res = self.client.post(
            "/v2/rbac/assignments",
            json={
                "user_email": "target@renphil.org",
                "role_id": self.hub_admin_role.id,
                "scope_id": self.all_scopes.id,
            },
            headers=self.bootstrap_admin,
        )
        self.assertEqual(res.status_code, 201, res.text)

    def test_bootstrap_admin_passes_grant_write_against_empty_assignments(self):
        res = self.client.post(
            "/v2/rbac/grants",
            json={
                "node_kind": "hub",
                "node_id": 999999,
                "level": "edit",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.bootstrap_admin,
        )
        self.assertIn(res.status_code, (404, 409), res.text)
        if res.status_code == 409:
            self.assertNotEqual(res.json()["detail"]["code"], "node_not_editable", res.text)


class TestJwtOnlyAdminIsRefusedEverywhere(HubAdminResolverApiFixture):
    """Phase 3's narrowing, through the real endpoints: the Airtable "Hub
    Admin" role in the JWT, with no assignment and no bootstrap entry, gets
    exactly what `nobody` gets (`TestNeitherSourceRefusesAnywhere`)."""

    def test_role_definition_write_refused(self):
        res = self.client.post(
            "/v2/rbac/roles", json={"key": "sneaky", "name": "Sneaky"}, headers=self.jwt_admin,
        )
        self.assertEqual(res.status_code, 403, res.text)

    def test_assignment_write_refused(self):
        res = self.client.post(
            "/v2/rbac/assignments",
            json={
                "user_email": "target@renphil.org",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.jwt_admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "not_delegable", res.text)

    def test_grant_write_refused(self):
        res = self.client.post(
            "/v2/rbac/grants",
            json={
                "node_kind": "hub",
                "node_id": 999999,
                "level": "edit",
                "role_id": self.member_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.jwt_admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "node_not_editable", res.text)


class TestTheWideningIsReal(HubAdminResolverApiFixture):
    """Stated plainly in the handoff, not glossed as additive: an
    assignment-only admin now passes `is_hub_admin` on EVERY surface that
    resolver feeds, including nav-tab mutations — which this task does not
    otherwise touch.

    UPDATED for project_ac_enforcement_gap.md's §6.8 nav-tab/hub split
    (2026-09-04): `nav_tabs.py`'s four sites no longer use
    `Depends(require_hub_admin)` at all — they gate on `edit(hub)`/
    `edit(nav_tab)` via `get_viewer_access`, which still calls `is_hub_admin`
    internally (`ViewerAccess.full_access`) for exactly the same JWT-OR-
    assignment resolution this class exists to prove. The claim this class
    makes survives the split unchanged; only the mechanics of proving it
    do — a REAL nav tab is needed now, not a nonexistent `document_id`,
    because "not found" no longer distinguishes "the gate passed" from "the
    gate never ran": both an authorized AND an unauthorized caller hit the
    SAME existence check first under the new per-node gate (an ungranted
    caller against a nonexistent document_id would 404 regardless of who
    they are, since there is nothing to gate on) — see
    `test_tab_visibility_wiring.py` for the exhaustive per-node split this
    class does not attempt to re-prove.
    """

    def setUp(self) -> None:
        super().setUp()
        self.assign(self.assignment_admin_user, self.hub_admin_role, self.all_scopes)
        # access_visibility_service.resolve_hub_node needs EXACTLY ONE hub
        # row (§3.3) — absent here before this fix, same fixture gap the
        # read-wiring session found in test_nav_tabs.py/test_tab_locking.py.
        self.db.add(HubV2(document_id="hub-widening"))
        self.nav = NavTabV2(document_id="nav-widening", slug="widening", title="Widening")
        self.db.add(self.nav)
        self.db.commit()

    def test_assignment_only_admin_passes_a_nav_tab_mutation(self):
        # PUT /v2/nav-tabs/{document_id} is one of the four sites that used
        # to carry `Depends(require_hub_admin)` in nav_tabs.py. Against a
        # REAL nav tab, the assignment-only admin's `ViewerAccess.full_access`
        # bypass clears `edit(nav_tab)` and the rename actually succeeds —
        # unlike the old dependency-level gate, there is no "not found"
        # shortcut available here to lean on instead.
        # plan_lock_propagation_2026-09-08.md §5.2 gave this route a live-
        # session gate too, orthogonal to the permission axis this class is
        # actually about — Hub Admin (by any resolution path) does not
        # bypass it (owner-confirmed 2026-09-08).
        from app.services.edit_lock_service import acquire

        token = acquire(self.db, ("nav_tab", self.nav.id), "assignment-admin@renphil.org").token
        res = self.client.put(
            f"/v2/nav-tabs/{self.nav.document_id}",
            json={"title": "Renamed"},
            headers={**self.assignment_admin, "X-Edit-Tokens": token},
        )
        self.assertEqual(res.status_code, 200, res.text)

    def test_jwt_only_nobody_is_still_refused_the_same_mutation(self):
        """Control for the test above, against the SAME real nav tab: without
        either admin source, `nobody` holds zero `resource_grants` rows —
        D2, unset access control matches no one — so they cannot even VIEW
        this nav tab. That is 404, not the old blanket 403: the per-node
        gate does not distinguish "you are not an admin" from "this does not
        exist", by design (§9). The behaviour-change claim this class makes
        is unaffected — `nobody` is still refused, on the same route, either
        way — only the status code is, and that IS this session's change,
        not a regression to paper over."""
        res = self.client.put(
            f"/v2/nav-tabs/{self.nav.document_id}",
            json={"title": "Renamed"},
            headers=self.nobody,
        )
        self.assertEqual(res.status_code, 404, res.text)


if __name__ == "__main__":
    unittest.main()
