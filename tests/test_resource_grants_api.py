"""Grants router and its write gate — plan_access_control_algorithm
_2026-08-27.md §6.2, §6.3, §7, §9, and §12's undesigned question, decided by
the owner on 2026-08-31.

Router-level, where `test_resource_grants.py` is service-level: step 3 built
the table and the matching primitive and deliberately stopped before an
endpoint, so everything below is new surface.

THE FIXTURE BUILDS NODES, ROLES, SCOPES AND PEOPLE — AND NO ASSIGNMENTS AND
NO GRANTS. That is production's exact state (§10 items 1-2, D2's no-seeding
decision), and it makes the bootstrap the DEFAULT case rather than a special
one: every test that wants a caller to get past the gate has to say so, and
`TestTheBootstrapIsNotDeadlocked` needs no special fixture at all.

Two fixtures are carried over verbatim and both are load-bearing:

  - `test_rbac_api.py`'s dependency override, INCLUDING its
    `finally: db.close()`. Every other API fixture in this repo uses the
    one-liner that never closes, and on a StaticPool every session shares
    one SQLite connection — so a request that raises leaves its uncommitted
    INSERTs pending, and the next request's commit adopts them. Under that
    override the "creates NOTHING" assertions below would pass against a
    handler that forgot to roll back.
  - `test_resource_grants.py`'s `@compiles(JSONB, "sqlite")` shim,
    `PRAGMA foreign_keys=ON` (without which every cascade and RESTRICT
    assertion passes vacuously) and its committing `setUp`.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-grants-api-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.resource_grant import ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.main import create_app  # noqa: E402
from tests._real_admin import seed_real_admin  # noqa: E402

HUB_ADMIN = "Hub Admin"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GrantsApiFixture(unittest.TestCase):
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
        # `finally: db.close()`. See this module's docstring for why the
        # one-liner the other API fixtures use would make the
        # "creates NOTHING" assertions below vacuous.
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

        self._build_nodes()
        self._build_org()
        # COMMITTED, not just flushed — carried over from
        # test_resource_grants.py. Requests run on their own sessions over
        # the same StaticPool connection and cannot see uncommitted state.
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- node tree ----------------------------------------------------

    def _build_nodes(self) -> None:
        """hub → nav_tab → {tab, other_tab}; tab → gridstack → component.

        The gridstack is not a node (§3.2 — transparent, holds no grants),
        but a component cannot exist without one.
        """
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)

        self.nav_tab = NavTabV2(slug="dashboard", title="Dashboard", protected=False)
        self.db.add(self.nav_tab)
        self.db.flush()

        self.tab = TabV2(document_id="tab-1", title="Root A", nav_tab_id=self.nav_tab.id)
        self.other_tab = TabV2(document_id="tab-2", title="Root B", nav_tab_id=self.nav_tab.id)
        self.db.add_all([self.tab, self.other_tab])
        self.db.flush()

        # Both root tabs get a root gridstack, the way real ones do —
        # `get_root_tabs_v2` drives the tab bar off root GRIDSTACKS joined
        # to tabs, so a tab without one is invisible there for reasons that
        # have nothing to do with access control. Giving `other_tab` one
        # keeps `TestNoContentEnforcementWasAdded` able to tell "filtered
        # by a grant" apart from "never listed in the first place".
        self.gridstack = GridstackV2(document_id="tab-1", parent_tab_id=self.tab.id)
        self.other_gridstack = GridstackV2(document_id="tab-2", parent_tab_id=self.other_tab.id)
        self.db.add_all([self.gridstack, self.other_gridstack])
        self.db.flush()

        self.component = ComponentV2(
            link="widget-1", type="markdown", gridstack_id=self.gridstack.id
        )
        self.db.add(self.component)
        self.db.flush()

    # -- roles, scopes, people ----------------------------------------

    def _role(self, key: str, name: str, public: bool = False) -> RoleV2:
        row = RoleV2(key=key, name=name, is_system=False, is_public=public, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def _scope(
        self, key: str, name: str, universal: bool = False, public: bool = False
    ) -> ScopeV2:
        row = ScopeV2(
            key=key,
            name=name,
            is_universal=universal,
            is_system=False,
            is_public=public,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _edge(self, parent: RoleV2, child: RoleV2) -> None:
        self.db.add(
            RoleEdgeV2(parent_role_id=parent.id, child_role_id=child.id, created_at=_now())
        )
        self.db.flush()

    def _user(self, email: str) -> UserV2:
        row = UserV2(email=email, has_signed_in=True, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def _build_org(self) -> None:
        """The live role DAG's shape, plus both ⊥ rows.

        Edges point parent → child and mean "the parent inherits everything
        the child has", so role_descendants(program_lead) is
        {program_lead, program_member, hub_member, ⊥role}. That direction is
        what the whole match rule turns on.

        NO ASSIGNMENTS AND NO GRANTS ARE CREATED HERE. See the module
        docstring.
        """
        self.hub_admin = self._role("hub_admin", "Hub Admin")
        self.program_lead = self._role("program_lead", "Program Lead")
        self.program_member = self._role("program_member", "Program Member")
        self.function_member = self._role("function_member", "Function Member")
        self.hub_member = self._role("hub_member", "Hub Member")
        self.any_role = self._role("any_role", "Any Role", public=True)

        self._edge(self.hub_admin, self.program_lead)
        self._edge(self.program_lead, self.program_member)
        self._edge(self.program_member, self.hub_member)
        self._edge(self.function_member, self.hub_member)

        self.all_scopes = self._scope("all_scopes", "All Scopes", universal=True)
        self.program_a = self._scope("program_a", "Program A")
        self.program_b = self._scope("program_b", "Program B")
        self.any_scope = self._scope("any_scope", "Any Scope", public=True)

        # Emails match the JWTs minted below, so `get_current_hub_user`
        # resolves rather than provisions. `nobody` exists as a person and
        # holds nothing — the ordinary signed-in user.
        self.alice = self._user("alice@renphil.org")
        self.bob = self._user("bob@renphil.org")
        self.carol = self._user("carol@renphil.org")
        self.nobody = self._user("nobody@renphil.org")
        self.admin_user = self._user("admin@renphil.org")

    # -- state the tests opt into -------------------------------------

    def assign(self, user: UserV2, role: RoleV2, scope: ScopeV2) -> None:
        self.db.add(
            RoleAssignmentV2(
                user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=_now()
            )
        )
        self.db.commit()

    def grant(self, *, node=("tab",), level="edit", **principal) -> int:
        """Raw insert, bypassing the router, so a test can put a caller in a
        position the gate would not yet let them reach themselves."""
        node_kind, node_id = node
        row = ResourceGrantV2(
            **{{"hub": "hub_id", "nav_tab": "nav_tab_id", "tab": "tab_id",
                "component": "component_id"}[node_kind]: node_id},
            level=level,
            created_at=_now(),
            **principal,
        )
        self.db.add(row)
        self.db.commit()
        return row.id

    def give_edit(self, user_row: UserV2, role: RoleV2, scope: ScopeV2, node) -> int:
        """The two rows that put an ordinary user past the gate on `node`:
        an assignment they hold, and an edit grant that assignment reaches."""
        self.assign(user_row, role, scope)
        return self.grant(node=node, level="edit", role_id=role.id, scope_id=scope.id)

    # -- addresses ----------------------------------------------------

    @property
    def tab_node(self):
        return ("tab", self.tab.id)

    @property
    def other_tab_node(self):
        return ("tab", self.other_tab.id)

    @property
    def nav_node(self):
        return ("nav_tab", self.nav_tab.id)

    @property
    def hub_node(self):
        return ("hub", self.hub.id)

    @property
    def component_node(self):
        return ("component", self.component.id)

    # -- auth ---------------------------------------------------------

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
        """A REAL Hub Admin. Since phase 3 the JWT "Hub Admin" claim alone
        makes nobody an admin, so the first use seeds `admin_user`'s
        `(hub_admin, all_scopes)` assignment (`tests/_real_admin.py`, own
        session; reuses `_build_org`'s role, scope and user). LAZY on
        purpose: the module's "no assignments, no grants" fixture holds for
        every test that never acts as admin (`test_both_tables_really_are_
        empty` among them). Tests ABOUT a JWT-only admin use
        `jwt_only_admin` instead."""
        seed_real_admin(self._session_factory, "admin@renphil.org")
        return self._auth("admin@renphil.org", [HUB_ADMIN])

    @property
    def jwt_only_admin(self) -> dict:
        """The JWT "Hub Admin" claim with NO assignment row: what `admin`
        was before phase 3. Kept for the tests whose subject is exactly
        that caller."""
        return self._auth("admin@renphil.org", [HUB_ADMIN])

    def user(self, email: str) -> dict:
        return self._auth(email, [])

    # -- calls --------------------------------------------------------

    def post_grant(self, headers, *, node, level="view", **principal):
        node_kind, node_id = node
        body = {"node_kind": node_kind, "node_id": node_id, "level": level}
        body.update(principal)
        return self.client.post("/v2/rbac/grants", json=body, headers=headers)

    def list_grants(self, headers, node):
        node_kind, node_id = node
        return self.client.get(f"/v2/rbac/nodes/{node_kind}/{node_id}/grants", headers=headers)

    def list_inherited_grants(self, headers, node):
        node_kind, node_id = node
        return self.client.get(
            f"/v2/rbac/nodes/{node_kind}/{node_id}/grants/inherited", headers=headers
        )

    def get_hub_node(self, headers):
        return self.client.get("/v2/rbac/hub", headers=headers)

    def get_audience(self, headers, role_id: int, scope_id: int):
        return self.client.get(
            "/v2/rbac/audience",
            params={"role_id": role_id, "scope_id": scope_id},
            headers=headers,
        )

    def get_user_access(self, headers, user_id: int):
        return self.client.get(f"/v2/rbac/hub-users/{user_id}/access", headers=headers)

    def delete_grant(self, headers, grant_id: int):
        return self.client.delete(f"/v2/rbac/grants/{grant_id}", headers=headers)

    def retained(self, headers, grant_id: int):
        return self.client.get(f"/v2/rbac/grants/{grant_id}/retained", headers=headers)

    # -- assertions ---------------------------------------------------

    def assertCode(self, res, status: int, code: str | None = None):
        self.assertEqual(res.status_code, status, res.text)
        if code is not None:
            self.assertEqual(res.json()["detail"]["code"], code, res.text)

    def rows(self) -> list[ResourceGrantV2]:
        """Read through a FRESH session. `self.db` has its own transaction
        over the shared connection and would serve a stale identity map."""
        session = self._session_factory()
        try:
            return session.query(ResourceGrantV2).order_by(ResourceGrantV2.id).all()
        finally:
            session.close()


# =========================================================================
# Hazard 1 — the trap that bites first
# =========================================================================


class TestTheBootstrapIsNotDeadlocked(GrantsApiFixture):
    """`role_assignments` and `resource_grants` are BOTH EMPTY in production.

    So `effective(U) = ∅`, every seed set is empty, `edit(n)` is false for
    everyone, and both halves of the gate refuse every caller. If the Hub
    Admin branch were not first and unconditional, NOBODY could write the
    first grant and the only way back in would be direct database access.

    This is the one class in the file that would still matter if every
    other one were deleted.
    """

    def test_both_tables_really_are_empty(self):
        """Guard on the guards: if the fixture ever started seeding, every
        assertion below would still pass and would be testing nothing."""
        session = self._session_factory()
        try:
            self.assertEqual(session.query(RoleAssignmentV2).count(), 0)
            self.assertEqual(session.query(ResourceGrantV2).count(), 0)
        finally:
            session.close()

    def test_a_hub_admin_can_write_the_first_grant_against_empty_tables(self):
        res = self.post_grant(
            self.admin,
            node=self.tab_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 201)
        self.assertEqual(len(self.rows()), 1)

    def test_a_hub_admin_can_write_the_first_grant_on_the_hub_itself(self):
        """§6.5's Hub Admin grant — the row every other grant is delegated
        from. If this one cannot be written, nothing downstream can."""
        res = self.post_grant(
            self.admin,
            node=self.hub_node,
            level="edit",
            role_id=self.hub_admin.id,
            scope_id=self.all_scopes.id,
        )
        self.assertCode(res, 201)

    def test_a_hub_admin_can_write_a_bottom_grant_against_empty_tables(self):
        """The widest grant in the system, from a standing start. A Hub
        Admin holds no assignment either, so `effective_pairs` is empty for
        them too — the bypass has to skip the PAIR rule as well as the node
        rule, not just the node rule."""
        res = self.post_grant(
            self.admin,
            node=self.tab_node,
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        self.assertCode(res, 201)

    def test_a_hub_admin_can_write_a_user_grant_against_empty_tables(self):
        res = self.post_grant(self.admin, node=self.tab_node, user_id=self.alice.id)
        self.assertCode(res, 201)

    def test_an_ordinary_user_is_refused_against_empty_tables(self):
        """The other half of the same fact. If this passed, the gate would
        not be a gate."""
        res = self.post_grant(
            self.user("nobody@renphil.org"),
            node=self.tab_node,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 409, "node_not_editable")
        self.assertEqual(self.rows(), [])

    def test_an_ordinary_user_cannot_write_a_USER_form_grant_either(self):
        """The user form skips the PAIR rule, never the NODE rule.

        Its own early return sits AFTER `edit(node)` for exactly this
        reason, and the ordering is the whole gate for that form — move the
        return above the node check and anyone with a login could share any
        node in the hub with anyone. Mutation-tested 2026-08-31.
        """
        res = self.post_grant(
            self.user("nobody@renphil.org"), node=self.tab_node, user_id=self.alice.id
        )
        self.assertCode(res, 409, "node_not_editable")
        self.assertEqual(self.rows(), [])

    def test_a_user_with_an_assignment_but_no_grant_is_still_refused(self):
        """`effective(U)` is non-empty now, but `resource_grants` is still
        empty, so `edit(n)` is false and step 2 refuses. Holding a role is
        not by itself permission to write grants on anything."""
        self.assign(self.alice, self.program_lead, self.program_a)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.tab_node,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 409, "node_not_editable")


# =========================================================================
# The node half — step 2 of the gate
# =========================================================================


class TestTheNodeHalfOfTheGate(GrantsApiFixture):
    def test_edit_on_the_node_admits(self):
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 201)

    def test_a_view_grant_is_not_enough(self):
        """§6.1 says edit implies view, never the reverse. Someone who can
        SEE a node must not be able to change who else can."""
        self.assign(self.alice, self.program_lead, self.program_a)
        self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 409, "node_not_editable")

    def test_edit_inherited_from_an_ancestor_admits(self):
        """§5.1's descending fold: a grant covers everything beneath it
        unconditionally (D3). Edit on the nav tab is edit on its tabs."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.nav_node)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.component_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 201)

    def test_edit_on_one_node_does_not_admit_on_a_sibling(self):
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.other_tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 409, "node_not_editable")

    def test_edit_on_a_child_does_not_admit_on_its_parent(self):
        """Grants fold DOWN the root path, never up. A component editor is
        not a tab editor."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.component_node)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 409, "node_not_editable")

    def test_an_absent_node_is_refused_the_same_way_an_uneditable_one_is(self):
        """§9: a hidden node must not confirm its own existence. The message
        and the code must not distinguish "you cannot edit this" from
        "this does not exist", or the endpoint becomes a node-tree oracle
        for anyone with a login."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        absent = self.post_grant(
            self.user("alice@renphil.org"),
            node=("tab", 99999),
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        uneditable = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.other_tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(absent, 409, "node_not_editable")
        self.assertCode(uneditable, 409, "node_not_editable")
        self.assertEqual(
            absent.json()["detail"]["message"], uneditable.json()["detail"]["message"]
        )


# =========================================================================
# The pair half — step 3, and the reason it is not `can_delegate`
# =========================================================================


class TestThePairRule(GrantsApiFixture):
    """"You may only write a grant that you yourself would match."

    Owner's decision, 2026-08-31. See
    `app/services/resource_grant_authz_service.py` for the measurement that
    produced it.
    """

    def setUp(self):
        super().setUp()
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        self.as_alice = self.user("alice@renphil.org")

    def _write(self, role, scope):
        return self.post_grant(
            self.as_alice, node=self.tab_node, role_id=role.id, scope_id=scope.id
        )

    def test_her_own_pair_is_writable(self):
        """THE CASE `can_delegate` GETS WRONG. Sharing a page with your own
        peers is the narrowest team-sized grant there is, and the delegation
        rule refuses it because a role is not a PROPER descendant of itself.
        That properness clause exists to stop you cloning your authority
        onto another PERSON; on a node you already hold edit on there is
        nothing to clone."""
        self.assertCode(self._write(self.program_lead, self.program_a), 201)

    def test_a_junior_pair_is_writable(self):
        self.assertCode(self._write(self.program_member, self.program_a), 201)
        self.assertCode(self._write(self.hub_member, self.program_a), 201)

    def test_a_senior_pair_is_refused(self):
        """`hub_admin` is ABOVE program_lead, so it is not in her closure.
        A grant on it would reach only Hub Admins — she cannot write access
        for people she is not among."""
        self.assertCode(self._write(self.hub_admin, self.program_a), 409, "not_grantable")

    def test_a_pair_in_a_scope_she_does_not_hold_is_refused(self):
        self.assertCode(self._write(self.program_member, self.program_b), 409, "not_grantable")

    def test_the_universal_scope_is_refused(self):
        """§4.3's footgun from the other side: on an OBJECT, `All Scopes` is
        the NARROWEST scope — it reaches only users whose own assignment is
        hub-wide. Alice's is not, so she is not in that audience and may not
        write it. Refused by the ordinary rule, with no special case."""
        self.assertCode(self._write(self.program_lead, self.all_scopes), 409, "not_grantable")

    def test_an_unrelated_branch_of_the_role_dag_is_refused(self):
        """`function_member` is not below `program_lead` — the two are
        siblings under different parents. This is the pairing/closure rule
        doing its job on the role axis."""
        self.assertCode(self._write(self.function_member, self.program_a), 409, "not_grantable")

    def test_the_gate_permits_exactly_what_she_matches(self):
        """The property, quantified rather than sampled: for every pair over
        the whole role × scope space, the router's answer agrees with
        `effective_pairs` membership. One assertion that cannot be satisfied
        by a lucky special case."""
        from app.services.rbac_graph_service import effective_pairs

        session = self._session_factory()
        try:
            expected = effective_pairs(session, self.alice.id)
        finally:
            session.close()

        roles = [
            self.hub_admin, self.program_lead, self.program_member,
            self.function_member, self.hub_member, self.any_role,
        ]
        scopes = [self.all_scopes, self.program_a, self.program_b, self.any_scope]

        for role in roles:
            for scope in scopes:
                with self.subTest(role=role.key, scope=scope.key):
                    res = self._write(role, scope)
                    allowed = res.status_code == 201
                    self.assertEqual(
                        allowed,
                        (role.id, scope.id) in expected,
                        f"{role.key}/{scope.key} -> {res.status_code} {res.text}",
                    )

    def test_an_incomplete_pair_is_named_rather_than_silently_unmatched(self):
        """A role with no scope is the shape that would quietly become "this
        role on every scope" if it slipped through. It must fail as a shape
        error, not as "that pair is not in your effective pairs", which is
        true and useless."""
        res = self.post_grant(self.as_alice, node=self.tab_node, role_id=self.hub_member.id)
        self.assertCode(res, 409, "incomplete_pair")


class TestTheGateIsNotTheDelegationRule(GrantsApiFixture):
    """Pins the divergence itself, so a later session cannot "unify" the two
    rules without a test going red.

    `rbac_delegation_service.can_delegate` is UNCHANGED and still governs
    assignments; its own docstring's warning about not calling
    `effective_pairs` remains true THERE. These two rules are supposed to
    disagree, and here is exactly where.
    """

    def test_the_grants_gate_permits_what_can_delegate_refuses(self):
        from app.services.rbac_delegation_service import can_delegate

        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)

        session = self._session_factory()
        try:
            delegable = can_delegate(
                session, self.alice.id, self.program_lead.id, self.program_a.id
            )
        finally:
            session.close()

        # The assignment rule refuses her own pair (not a PROPER descendant).
        self.assertFalse(delegable)
        # The grants rule permits it — and permitting the NARROWER grant is
        # the whole point of the divergence.
        self.assertCode(
            self.post_grant(
                self.user("alice@renphil.org"),
                node=self.tab_node,
                role_id=self.program_lead.id,
                scope_id=self.program_a.id,
            ),
            201,
        )

    def test_the_audience_inversion_is_gone(self):
        """`can_delegate`, transplanted here, was anti-correlated with
        audience size: it refused the narrowest grants and permitted the
        widest. Measured over a real population, the grants gate must now
        permit the narrow one it used to refuse.

        Alice's own pair reaches {alice}; the junior pair reaches
        {alice, bob}. Both are writable, and the narrow one is no longer the
        one that is refused.
        """
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        self.assign(self.bob, self.program_member, self.program_a)

        from app.services.rbac_graph_service import effective_pairs

        session = self._session_factory()
        try:
            alice_pairs = effective_pairs(session, self.alice.id)
            bob_pairs = effective_pairs(session, self.bob.id)
        finally:
            session.close()

        narrow = (self.program_lead.id, self.program_a.id)
        wide = (self.program_member.id, self.program_a.id)

        # The audiences, measured rather than assumed.
        self.assertEqual(
            [narrow in alice_pairs, narrow in bob_pairs], [True, False], "narrow reaches alice only"
        )
        self.assertEqual(
            [wide in alice_pairs, wide in bob_pairs], [True, True], "wide reaches both"
        )

        # And both are writable, the narrow one included.
        as_alice = self.user("alice@renphil.org")
        self.assertCode(
            self.post_grant(
                as_alice, node=self.tab_node, role_id=narrow[0], scope_id=narrow[1]
            ),
            201,
        )
        self.assertCode(
            self.post_grant(as_alice, node=self.tab_node, role_id=wide[0], scope_id=wide[1]), 201
        )


# =========================================================================
# ⊥ — hazard 2, and §0's decision
# =========================================================================


class TestTheBottom(GrantsApiFixture):
    """"Any Role" / "Any Scope" — §4.4's two lattice bottoms.

    THE DECISION, owner, 2026-08-31: **no carve-out.** ⊥ is an ordinary
    principal under the pair rule, which means anyone holding any assignment
    plus `edit(n)` may publish `n` to the whole hub. That consequence was
    measured and tabled before it was chosen; see
    `resource_grant_authz_service`'s module docstring. The restraint on ⊥ is
    `edit(n)`, and it is the only one.

    Tested in BOTH directions, as the task requires: whoever is permitted to
    write ⊥ can, and whoever is not, cannot.
    """

    def test_the_bottom_is_not_refused_on_the_grants_path(self):
        """GOES RED IF SOMEONE COPIES `_assert_not_public` ACROSS.

        That refusal is a security control on the ASSIGNMENT side, where
        holding ⊥ confers only what everyone already reaches. On an object
        grant the flag means the opposite — it is the whole point of it, and
        the thing that makes "publish to the whole hub" one row instead of
        one per scope anybody holds. Each flag has a lane.
        """
        res = self.post_grant(
            self.admin,
            node=self.tab_node,
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        self.assertCode(res, 201)
        self.assertEqual(res.json()["data"]["role_id"], self.any_role.id)
        self.assertEqual(res.json()["data"]["scope_id"], self.any_scope.id)

    def test_bottom_on_one_axis_only_is_also_legal(self):
        for role, scope in (
            (self.any_role, self.program_a),
            (self.program_member, self.any_scope),
        ):
            with self.subTest(role=role.key, scope=scope.key):
                res = self.post_grant(
                    self.admin, node=self.tab_node, role_id=role.id, scope_id=scope.id
                )
                self.assertCode(res, 201)

    def test_a_node_editor_with_an_assignment_may_publish_to_the_whole_hub(self):
        """THE ACCEPTED CONSEQUENCE OF "NO CARVE-OUT", asserted deliberately
        rather than left to be discovered.

        Both ⊥ rows sit in EVERY descendant closure by construction, so
        `(⊥role, ⊥scope)` is in the effective pairs of anyone holding any
        assignment at all. If this test ever needs changing, the change is a
        DESIGN decision to add a carve-out — not a bug fix.
        """
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.tab_node,
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        self.assertCode(res, 201)

    def test_without_edit_on_the_node_the_bottom_is_refused(self):
        """The other direction. `edit(n)` is the restraint, so someone who
        holds an assignment but not the node cannot publish it."""
        self.assign(self.alice, self.program_lead, self.program_a)
        res = self.post_grant(
            self.user("alice@renphil.org"),
            node=self.tab_node,
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        self.assertCode(res, 409, "node_not_editable")

    def test_without_any_assignment_the_bottom_is_refused(self):
        res = self.post_grant(
            self.user("nobody@renphil.org"),
            node=self.tab_node,
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        self.assertCode(res, 409, "node_not_editable")

    def test_a_bottom_grant_can_still_be_revoked(self):
        """REFUSE THE WAY IN, NEVER THE WAY OUT. The widest grant in the
        system must not be the one thing nobody can take away — including
        one written straight into the database before any guard existed."""
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        self.assertEqual(self.delete_grant(self.admin, grant_id).status_code, 200)
        self.assertEqual(self.rows(), [])

    def test_a_bottom_grant_is_revocable_by_an_ordinary_node_editor_too(self):
        """Not only by a Hub Admin. ⊥ is in every user's effective pairs, so
        anyone who passes the node half passes the pair half for it."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
        )
        res = self.delete_grant(self.user("alice@renphil.org"), grant_id)
        self.assertEqual(res.status_code, 200, res.text)

    def test_the_bottom_is_still_refused_as_an_ASSIGNMENT(self):
        """The other lane, unchanged. Adding the grants path must not have
        weakened `rbac_assignments._assert_not_public` — the two flags stay
        in their own lanes.

        Checked on `(Any Role, Program A)`: since 2026-10-14 the exact pair
        `(Any Role, Any Scope)` is every new user's default assignment and is
        let through (covered in test_default_assignment.py), but the public
        role on a real scope is still refused."""
        res = self.client.post(
            "/v2/rbac/assignments",
            json={
                "user_email": "alice@renphil.org",
                "role_id": self.any_role.id,
                "scope_id": self.program_a.id,
            },
            headers=self.admin,
        )
        self.assertCode(res, 409, "public_not_assignable")


# =========================================================================
# Revoke
# =========================================================================


class TestRevoke(GrantsApiFixture):
    def test_revocation_is_symmetric_not_ownership_based(self):
        """The same choice `delete_assignment` makes: the gate re-runs
        against the grant's OWN node and principal regardless of who wrote
        it. Alice may remove a row the admin wrote."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertEqual(
            self.delete_grant(self.user("alice@renphil.org"), grant_id).status_code, 200
        )

    def test_a_principal_above_her_cannot_be_revoked(self):
        """Symmetric means symmetric: she could not have written
        `(hub_admin, …)`, so she cannot remove it either. Revoking access
        for people senior to you is not yours to do."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.hub_admin.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(
            self.delete_grant(self.user("alice@renphil.org"), grant_id), 409, "not_grantable"
        )
        self.assertEqual(len(self.rows()), 2)

    def test_a_user_form_grant_is_revocable_by_any_node_editor(self):
        """The user form stops after `edit(n)` on the way in, so it must on
        the way out too, or a grant would become unrevocable by the person
        who could have written it."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(node=self.tab_node, level="view", user_id=self.carol.id)
        self.assertEqual(
            self.delete_grant(self.user("alice@renphil.org"), grant_id).status_code, 200
        )

    def test_a_user_form_grant_is_not_revocable_without_edit(self):
        """The other direction of the same asymmetry: skipping the pair rule
        must not mean skipping the node rule on the way out either."""
        grant_id = self.grant(node=self.tab_node, level="view", user_id=self.carol.id)
        self.assertCode(
            self.delete_grant(self.user("nobody@renphil.org"), grant_id), 409, "node_not_editable"
        )
        self.assertEqual(len(self.rows()), 1)

    def test_without_edit_nothing_is_revocable(self):
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(
            self.delete_grant(self.user("nobody@renphil.org"), grant_id), 409, "node_not_editable"
        )
        self.assertEqual(len(self.rows()), 1)

    def test_an_unknown_grant_is_404(self):
        self.assertEqual(self.delete_grant(self.admin, 99999).status_code, 404)


# =========================================================================
# Level change — PATCH /grants/{id} (owner decision, 2026-09-25)
# =========================================================================


class TestLevelChange(GrantsApiFixture):
    def patch_grant(self, headers, grant_id: int, level: str):
        return self.client.patch(
            f"/v2/rbac/grants/{grant_id}", json={"level": level}, headers=headers
        )

    def test_level_changes_in_place_and_provenance_is_restamped(self):
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        res = self.patch_grant(self.user("alice@renphil.org"), grant_id, "edit")
        self.assertCode(res, 200)
        data = res.json()["data"]
        self.assertEqual(data["id"], grant_id)
        self.assertEqual(data["level"], "edit")
        self.assertEqual(data["granted_by_email"], "alice@renphil.org")
        self.assertEqual(data["granted_by_user_id"], self.alice.id)
        row = next(r for r in self.rows() if r.id == grant_id)
        self.assertEqual(row.level, "edit")
        self.assertEqual(row.granted_by_email, "alice@renphil.org")
        # Same row, not a revoke + write: node and principal untouched.
        self.assertEqual(row.tab_id, self.tab.id)
        self.assertEqual(row.role_id, self.program_member.id)
        self.assertEqual(len(self.rows()), 2)

    def test_collision_with_a_sibling_row_is_refused(self):
        view_id = self.grant(
            node=self.tab_node, level="view", role_id=self.program_member.id, scope_id=self.program_a.id
        )
        self.grant(
            node=self.tab_node, level="edit", role_id=self.program_member.id, scope_id=self.program_a.id
        )
        self.assertCode(self.patch_grant(self.admin, view_id, "edit"), 409, "duplicate_grant")
        self.assertEqual(next(r for r in self.rows() if r.id == view_id).level, "view")

    def test_same_principal_on_another_node_is_not_a_collision(self):
        view_id = self.grant(node=self.tab_node, level="view", user_id=self.carol.id)
        self.grant(node=self.other_tab_node, level="edit", user_id=self.carol.id)
        self.assertCode(self.patch_grant(self.admin, view_id, "edit"), 200)

    def test_without_edit_on_the_node_it_is_refused(self):
        grant_id = self.grant(node=self.tab_node, level="view", user_id=self.carol.id)
        self.assertCode(
            self.patch_grant(self.user("nobody@renphil.org"), grant_id, "edit"),
            409,
            "node_not_editable",
        )
        self.assertEqual(next(r for r in self.rows() if r.id == grant_id).level, "view")

    def test_the_pair_rule_applies_like_create_and_delete(self):
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(
            node=self.tab_node, level="view", role_id=self.hub_admin.id, scope_id=self.program_a.id
        )
        self.assertCode(
            self.patch_grant(self.user("alice@renphil.org"), grant_id, "edit"), 409, "not_grantable"
        )

    def test_an_inherited_row_needs_edit_on_its_own_node(self):
        """Alice edits the tab, not the nav tab above it — the row lives on
        the nav tab, so the gate there is what counts."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        grant_id = self.grant(node=self.nav_node, level="view", user_id=self.carol.id)
        self.assertCode(
            self.patch_grant(self.user("alice@renphil.org"), grant_id, "edit"),
            409,
            "node_not_editable",
        )

    def test_an_invalid_level_is_422(self):
        grant_id = self.grant(node=self.tab_node, level="view", user_id=self.carol.id)
        self.assertEqual(self.patch_grant(self.admin, grant_id, "owner").status_code, 422)

    def test_an_unknown_grant_is_404(self):
        self.assertEqual(self.patch_grant(self.admin, 99999, "edit").status_code, 404)

    def test_downgrade_releases_locks_upgrade_does_not(self):
        edit_id = self.grant(node=self.tab_node, level="edit", user_id=self.carol.id)
        view_id = self.grant(node=self.other_tab_node, level="view", user_id=self.carol.id)
        with mock.patch(
            "app.routers.resource_grants.edit_lock_service.release_locks_now_unauthorized",
            new_callable=mock.AsyncMock,
        ) as release:
            self.assertCode(self.patch_grant(self.admin, view_id, "edit"), 200)
            release.assert_not_called()
            self.assertCode(self.patch_grant(self.admin, edit_id, "view"), 200)
            release.assert_awaited_once()


# =========================================================================
# Reads
# =========================================================================


class TestReads(GrantsApiFixture):
    def test_direct_grants_only(self):
        """§9 requires direct and inherited to be listed SEPARATELY, naming
        the ancestor an inherited one comes from. A grant on the nav tab
        covers the tab, but it is not stored on it and must not appear
        here."""
        self.grant(
            node=self.nav_node,
            level="view",
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        on_tab = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
        )
        res = self.list_grants(self.admin, self.tab_node)
        self.assertCode(res, 200)
        self.assertEqual([g["id"] for g in res.json()["data"]], [on_tab])

    def test_reads_are_gated_on_edit(self):
        """A node's access list enumerates roles, scopes and named people.
        Gating it on `view` would hand that to everyone who can see the
        node — a strictly larger audience than the people who administer
        it."""
        self.assign(self.alice, self.program_lead, self.program_a)
        self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(
            self.list_grants(self.user("alice@renphil.org"), self.tab_node),
            409,
            "node_not_editable",
        )

    def test_a_node_editor_may_read(self):
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        self.assertCode(self.list_grants(self.user("alice@renphil.org"), self.tab_node), 200)

    def test_an_unknown_node_is_404_for_someone_entitled_to_know(self):
        self.assertEqual(self.list_grants(self.admin, ("tab", 99999)).status_code, 404)

    def test_an_unknown_node_does_not_confirm_itself_to_anyone_else(self):
        """404 vs 409 must not be a probe. An unentitled caller gets the
        same refusal for a real node and an imaginary one."""
        self.assertCode(
            self.list_grants(self.user("nobody@renphil.org"), ("tab", 99999)),
            409,
            "node_not_editable",
        )

    def test_an_invalid_node_kind_is_422(self):
        res = self.client.get("/v2/rbac/nodes/gridstack/1/grants", headers=self.admin)
        self.assertEqual(res.status_code, 422, res.text)


# =========================================================================
# Handoff §0.1 — inherited grants
# =========================================================================


class TestInheritedReads(GrantsApiFixture):
    """The ancestor half of `TestReads` above — `list_grants_for_node`'s own
    docstring says this was "not built yet"; it is now
    `access_visibility_service.list_inherited_grants`."""

    def test_ancestor_grants_are_grouped_and_named(self):
        on_nav = self.grant(
            node=self.nav_node, level="view", role_id=self.hub_member.id, scope_id=self.program_a.id
        )
        res = self.list_inherited_grants(self.admin, self.tab_node)
        self.assertCode(res, 200)
        data = res.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["node_kind"], "nav_tab")
        self.assertEqual(data[0]["node_id"], self.nav_tab.id)
        self.assertEqual([g["id"] for g in data[0]["grants"]], [on_nav])

    def test_a_grant_on_the_node_itself_is_not_inherited(self):
        """That is the direct list's job — this one is ancestors only."""
        self.grant(
            node=self.tab_node, level="view", role_id=self.hub_member.id, scope_id=self.program_a.id
        )
        res = self.list_inherited_grants(self.admin, self.tab_node)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"], [])

    def test_ancestors_with_no_grants_are_omitted(self):
        self.grant(
            node=self.hub_node, level="edit", role_id=self.hub_admin.id, scope_id=self.all_scopes.id
        )
        res = self.list_inherited_grants(self.admin, self.tab_node)
        self.assertCode(res, 200)
        data = res.json()["data"]
        # nav_tab sits between tab and hub and has no grant of its own.
        self.assertEqual([e["node_kind"] for e in data], ["hub"])

    def test_reads_are_gated_on_edit_same_as_the_direct_list(self):
        self.assign(self.alice, self.program_lead, self.program_a)
        self.assertCode(
            self.list_inherited_grants(self.user("alice@renphil.org"), self.tab_node),
            409,
            "node_not_editable",
        )

    def test_a_node_editor_may_read(self):
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        self.assertCode(
            self.list_inherited_grants(self.user("alice@renphil.org"), self.tab_node), 200
        )

    def test_an_unknown_node_is_404_for_someone_entitled_to_know(self):
        self.assertEqual(self.list_inherited_grants(self.admin, ("tab", 99999)).status_code, 404)

    def test_the_hub_itself_has_no_ancestors(self):
        res = self.list_inherited_grants(self.admin, self.hub_node)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"], [])

    def test_can_manage_is_true_for_a_hub_admin(self):
        self.grant(node=self.nav_node, level="view", user_id=self.carol.id)
        data = self.list_inherited_grants(self.admin, self.tab_node).json()["data"]
        self.assertEqual([e["can_manage"] for e in data], [True])

    def test_can_manage_is_false_without_edit_on_the_ancestor(self):
        """Alice edits the tab but only VIEWS the nav tab above it."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.tab_node)
        self.grant(
            node=self.nav_node, level="view", role_id=self.hub_member.id, scope_id=self.program_a.id
        )
        res = self.list_inherited_grants(self.user("alice@renphil.org"), self.tab_node)
        self.assertCode(res, 200)
        self.assertEqual([e["can_manage"] for e in res.json()["data"]], [False])

    def test_can_manage_is_per_ancestor(self):
        """Edit on the nav tab, nothing on the hub: two entries, two answers."""
        self.assign(self.alice, self.program_lead, self.program_a)
        self.grant(
            node=self.nav_node, level="edit", role_id=self.program_lead.id, scope_id=self.program_a.id
        )
        self.grant(node=self.hub_node, level="view", user_id=self.carol.id)
        res = self.list_inherited_grants(self.user("alice@renphil.org"), self.tab_node)
        self.assertCode(res, 200)
        self.assertEqual(
            [(e["node_kind"], e["can_manage"]) for e in res.json()["data"]],
            [("nav_tab", True), ("hub", False)],
        )


# =========================================================================
# `GET /v2/rbac/hub` — resolve the hub's own node identity
# =========================================================================


class TestHubNode(GrantsApiFixture):
    """Bootstraps `HubAccessPage` (frontend, admin section) — the hub node
    is not a row anywhere in the UI, so this is the only way the client
    resolves `("hub", <id>)`. Hub-Admin-gated, not node-gated, for the same
    reason `/audience` and `/hub-users/{id}/access` are: there is no node
    yet to run `edit(n)` against."""

    def test_hub_admin_only(self):
        """DELIBERATELY `program_lead`/`program_a`, NOT `hub_admin`/
        `all_scopes` — see `TestAudienceCount.test_hub_admin_only`'s own
        comment for why: `give_edit` both assigns the pair and grants on
        it, so using the admin pair here would make Alice a genuine
        assignments-side Hub Admin instead of testing the thing this test
        is for."""
        self.give_edit(self.alice, self.program_lead, self.program_a, self.hub_node)
        res = self.get_hub_node(self.user("alice@renphil.org"))
        self.assertEqual(res.status_code, 403)

    def test_resolves_the_singleton_hub_row(self):
        res = self.get_hub_node(self.admin)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"], {"node_kind": "hub", "node_id": self.hub.id})


# =========================================================================
# Handoff §0.2 — audience count
# =========================================================================


class TestAudienceCount(GrantsApiFixture):
    """`GET /v2/rbac/audience` — the live preview for the grant picker
    (plan §9). Hub-Admin-gated, not node-gated: there is no node yet when
    the admin is still choosing a pair."""

    def test_hub_admin_only(self):
        """An ordinary signed-in user is refused even if they hold edit on
        the hub itself — this gate is a pure identity check, unlike every
        node-scoped endpoint above.

        DELIBERATELY `program_lead`/`program_a`, NOT `hub_admin`/`all_scopes`
        (plan_access_control_algorithm_2026-08-27.md §10 item 6, phase 2,
        2026-09-03): `give_edit` both ASSIGNS the pair and grants on it, so
        using `hub_admin`/`all_scopes` here would make Alice a genuine
        assignments-side Hub Admin (the resolver's `OR`, working as
        intended) and the 403 below would fail for the right reason for the
        wrong test. Any non-admin pair proves the same point — edit(hub)
        via an ordinary grant is not enough — without accidentally
        satisfying `is_hub_admin` itself.
        """
        self.give_edit(self.alice, self.program_lead, self.program_a, self.hub_node)
        res = self.get_audience(self.user("alice@renphil.org"), self.program_member.id, self.program_a.id)
        self.assertEqual(res.status_code, 403)

    # Since phase 3 the caller (`self.admin`) holds a real (hub_admin,
    # all_scopes) assignment, and that pair reaches EVERY pair below it, so
    # the caller is in every audience. The counts below are "the people the
    # test assigns" + 1 for the caller, written out (`_CALLER`) so the
    # original number stays readable.
    _CALLER = 1

    def test_zero_assignments_is_zero(self):
        """Zero assignments besides the admin caller's own row."""
        res = self.get_audience(self.admin, self.program_member.id, self.program_a.id)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"]["count"], 0 + self._CALLER)

    def test_counts_distinct_users_reaching_the_pair(self):
        # Alice reaches it two different ways — still counted once.
        self.assign(self.alice, self.program_lead, self.program_a)
        self.assign(self.alice, self.hub_admin, self.program_a)
        # Bob matches directly.
        self.assign(self.bob, self.program_member, self.program_a)
        # Carol's role does not descend to program_member at all.
        self.assign(self.carol, self.function_member, self.program_b)

        res = self.get_audience(self.admin, self.program_member.id, self.program_a.id)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"]["count"], 2 + self._CALLER)

    def test_a_public_role_grant_reaches_everyone_with_any_assignment(self):
        """§4.4's ⊥ needs no special case on the audience side either —
        `role_descendants` already unions the public role in unconditionally."""
        self.assign(self.bob, self.function_member, self.program_b)
        res = self.get_audience(self.admin, self.any_role.id, self.program_b.id)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"]["count"], 1 + self._CALLER)

    def test_all_scopes_is_narrowest_not_widest(self):
        """§4.3's worked example, run through the new endpoint: a grant
        stored on All Scopes reaches only hub-wide assignees."""
        self.assign(self.alice, self.hub_member, self.program_a)
        self.assign(self.bob, self.hub_member, self.all_scopes)
        res = self.get_audience(self.admin, self.hub_member.id, self.all_scopes.id)
        self.assertCode(res, 200)
        # Bob (hub-wide) + the caller (also hub-wide); Alice (program_a) is
        # still excluded, which is the point.
        self.assertEqual(res.json()["data"]["count"], 1 + self._CALLER)

    def test_unknown_role_is_404(self):
        res = self.get_audience(self.admin, 999999, self.program_a.id)
        self.assertEqual(res.status_code, 404)

    def test_unknown_scope_is_404(self):
        res = self.get_audience(self.admin, self.program_member.id, 999999)
        self.assertEqual(res.status_code, 404)


# =========================================================================
# Handoff §0.3 — what can this person access
# =========================================================================


class TestUserAccess(GrantsApiFixture):
    """`GET /v2/rbac/hub-users/{id}/access` — §9's "what can this person
    access" panel. Same Hub-Admin gate as the audience endpoint, for the
    same reason: a person is not a node."""

    def test_hub_admin_only(self):
        res = self.get_user_access(self.user("alice@renphil.org"), self.bob.id)
        self.assertEqual(res.status_code, 403)

    def test_unknown_user_is_404(self):
        res = self.get_user_access(self.admin, 999999)
        self.assertEqual(res.status_code, 404)

    def test_a_user_with_no_assignments_sees_nothing(self):
        res = self.get_user_access(self.admin, self.nobody.id)
        self.assertCode(res, 200)
        self.assertEqual(res.json()["data"]["nodes"], [])

    def test_lists_visible_nodes_with_the_right_triple(self):
        self.give_edit(self.bob, self.program_member, self.program_a, self.tab_node)
        res = self.get_user_access(self.admin, self.bob.id)
        self.assertCode(res, 200)
        data = res.json()["data"]
        self.assertEqual(data["user_id"], self.bob.id)

        rows = {(r["node_kind"], r["node_id"]): r for r in data["nodes"]}

        tab_row = rows[("tab", self.tab.id)]
        self.assertEqual((tab_row["view"], tab_row["edit"], tab_row["revealed"]), (True, True, False))

        comp_row = rows[("component", self.component.id)]
        self.assertEqual(
            (comp_row["view"], comp_row["edit"], comp_row["revealed"]), (True, True, False)
        )

        nav_row = rows[("nav_tab", self.nav_tab.id)]
        self.assertEqual((nav_row["view"], nav_row["edit"], nav_row["revealed"]), (True, False, True))

        hub_row = rows[("hub", self.hub.id)]
        self.assertEqual((hub_row["view"], hub_row["edit"], hub_row["revealed"]), (True, False, True))

        # other_tab shares nav_tab as a parent but is on neither the seed's
        # root path nor its subtree — it must stay invisible.
        self.assertNotIn(("tab", self.other_tab.id), rows)


# =========================================================================
# §6.2 — the revoke-time confirmation
# =========================================================================


class TestRetainedAccess(GrantsApiFixture):
    """§6.2, and it ships WITH the write path rather than after it because
    it is what makes D4's "derive, never collapse" safe."""

    def test_the_alice_case(self):
        """§6.2 verbatim: Alice has edit on Root Tab 2, granted narrowly by
        Sam. She is later granted Nav 1, then that is revoked. The admin's
        model is "Alice has nothing now"; the truth is she keeps Root Tab 2,
        and this endpoint is what says so."""
        self.assign(self.alice, self.program_lead, self.program_a)
        sams_grant = self.grant(
            node=self.other_tab_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        nav_grant = self.grant(
            node=self.nav_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )

        res = self.retained(self.admin, nav_grant)
        self.assertCode(res, 200)
        data = res.json()["data"]

        retained = {(n["node_kind"], n["node_id"]) for n in data["retained_view"]}
        self.assertIn(("tab", self.other_tab.id), retained)
        self.assertEqual(
            [g["id"] for g in data["responsible_grants"]],
            [sams_grant],
            "the row `[ Remove that too ]` would delete",
        )
        self.assertEqual(data["covering_grants"], [])

    def test_the_plain_case_retains_nothing(self):
        """No other grant, so revoking really does do what the admin
        expects and the modal has nothing to warn about."""
        self.assign(self.alice, self.program_lead, self.program_a)
        nav_grant = self.grant(
            node=self.nav_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        data = self.retained(self.admin, nav_grant).json()["data"]
        self.assertEqual(data["retained_view"], [])
        self.assertEqual(data["retained_edit"], [])
        self.assertEqual(data["responsible_grants"], [])
        self.assertEqual(data["covering_grants"], [])

    def test_a_covering_ancestor_is_reported_separately(self):
        """§6.2's OTHER case, and it needs a different sentence: the grant
        being revoked is redundant, because an ancestor already covers it.
        Removing it changes nothing for this principal, and §6.2 says to
        display that rather than delete anything."""
        self.assign(self.alice, self.program_lead, self.program_a)
        hub_grant = self.grant(
            node=self.hub_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        tab_grant = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        data = self.retained(self.admin, tab_grant).json()["data"]

        self.assertEqual([g["id"] for g in data["covering_grants"]], [hub_grant])
        retained = {(n["node_kind"], n["node_id"]) for n in data["retained_view"]}
        self.assertIn(("tab", self.tab.id), retained, "still covered from the hub")
        self.assertIn(("component", self.component.id), retained)

    def test_descendants_of_the_node_are_reported(self):
        """"Which of the revoked node's DESCENDANTS are still reached" —
        the span the revoke was meant to affect, not just the node."""
        self.assign(self.alice, self.program_lead, self.program_a)
        tab_grant = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        self.grant(
            node=self.component_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        data = self.retained(self.admin, tab_grant).json()["data"]
        retained = {(n["node_kind"], n["node_id"]) for n in data["retained_view"]}
        self.assertEqual(retained, {("component", self.component.id)})

    def test_edit_is_reported_separately_from_view(self):
        """§6.2's modal says "she will still have EDIT on Root Tab 2". The
        level is part of the sentence."""
        self.assign(self.alice, self.program_lead, self.program_a)
        self.grant(
            node=self.other_tab_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        nav_grant = self.grant(
            node=self.nav_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        data = self.retained(self.admin, nav_grant).json()["data"]
        edit = {(n["node_kind"], n["node_id"]) for n in data["retained_edit"]}
        self.assertIn(("tab", self.other_tab.id), edit)

    def test_it_deletes_nothing(self):
        """ADVISORY, NOT A GATE. The grant still exists afterwards, and
        `[ Leave it ]` is a legitimate answer."""
        self.assign(self.alice, self.program_lead, self.program_a)
        nav_grant = self.grant(
            node=self.nav_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        before = len(self.rows())
        self.retained(self.admin, nav_grant)
        self.assertEqual(len(self.rows()), before)
        self.assertIsNotNone(
            self.client.get(f"/v2/rbac/grants/{nav_grant}/retained", headers=self.admin).json()
        )

    def test_a_user_principal_is_answered_from_their_real_assignments(self):
        """For a USER principal the honest answer is what that PERSON
        reaches — including grants written to a pair they merely match, not
        only grants naming them directly."""
        self.assign(self.alice, self.program_lead, self.program_a)
        pair_grant = self.grant(
            node=self.other_tab_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        direct = self.grant(node=self.nav_node, level="view", user_id=self.alice.id)

        data = self.retained(self.admin, direct).json()["data"]
        self.assertEqual([g["id"] for g in data["responsible_grants"]], [pair_grant])

    def test_a_pair_principal_is_not_answered_from_identical_rows_only(self):
        """The under-reporting trap, pinned. Revoke `(program_member, A)`
        from the nav tab while `(hub_member, A)` sits on Root Tab 2: a
        holder of `(program_member, A)` reaches hub_member's grant through
        the role closure, so "they retain nothing" would be a FALSE
        reassurance given at the moment an admin decides whether to remove
        more."""
        other = self.grant(
            node=self.other_tab_node,
            level="view",
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
        )
        nav_grant = self.grant(
            node=self.nav_node,
            level="view",
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        data = self.retained(self.admin, nav_grant).json()["data"]
        self.assertEqual([g["id"] for g in data["responsible_grants"]], [other])

    def test_it_drops_by_grant_id_not_by_node(self):
        """A node carries a BAG of grants (§7), so several rows can sit on
        one node. Dropping the node instead of the row would silently
        revoke the principal's other rows on it and under-report."""
        self.assign(self.alice, self.program_lead, self.program_a)
        view_row = self.grant(
            node=self.nav_node,
            level="view",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        edit_row = self.grant(
            node=self.nav_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        data = self.retained(self.admin, edit_row).json()["data"]
        # The view row on the SAME node survives and is what still reaches.
        self.assertEqual([g["id"] for g in data["responsible_grants"]], [view_row])
        retained = {(n["node_kind"], n["node_id"]) for n in data["retained_view"]}
        self.assertIn(("nav_tab", self.nav_tab.id), retained)

    def test_it_is_gated_like_the_write(self):
        grant_id = self.grant(
            node=self.tab_node,
            level="view",
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(
            self.retained(self.user("nobody@renphil.org"), grant_id), 409, "node_not_editable"
        )

    def test_an_unknown_grant_is_404(self):
        self.assertEqual(self.retained(self.admin, 99999).status_code, 404)


# =========================================================================
# Ordinary CRUD behaviour the gate must not have broken
# =========================================================================


class TestCrud(GrantsApiFixture):
    def test_provenance_is_stamped_from_the_authenticated_identity(self):
        """Never from the request body — the same rule §6.6 states for
        `locked_by`. `granted_by_email` is the immutable snapshot §6.2's
        "granted directly by Sam, 3 Feb" depends on after Sam leaves."""
        res = self.post_grant(
            self.admin,
            node=self.tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 201)
        self.assertEqual(res.json()["data"]["granted_by_email"], "admin@renphil.org")
        self.assertEqual(res.json()["data"]["granted_by_user_id"], self.admin_user.id)

    def test_a_duplicate_is_named(self):
        for expected in (201, 409):
            res = self.post_grant(
                self.admin,
                node=self.tab_node,
                role_id=self.program_member.id,
                scope_id=self.program_a.id,
            )
            self.assertEqual(res.status_code, expected, res.text)
        self.assertEqual(res.json()["detail"]["code"], "duplicate_grant")

    def test_the_two_levels_coexist_on_one_node(self):
        """§7: the uniqueness rule includes `level`, so view and edit for
        one principal are two ordinary rows, not a conflict."""
        for level in ("view", "edit"):
            res = self.post_grant(
                self.admin,
                node=self.tab_node,
                level=level,
                role_id=self.program_member.id,
                scope_id=self.program_a.id,
            )
            self.assertCode(res, 201)
        self.assertEqual(len(self.rows()), 2)

    def test_a_user_grant_round_trips(self):
        res = self.post_grant(self.admin, node=self.tab_node, user_id=self.bob.id)
        self.assertCode(res, 201)
        self.assertEqual(res.json()["data"]["user_id"], self.bob.id)
        self.assertIsNone(res.json()["data"]["role_id"])

    def test_both_principal_forms_at_once_is_refused(self):
        res = self.post_grant(
            self.admin,
            node=self.tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
            user_id=self.bob.id,
        )
        self.assertCode(res, 409, "ambiguous_principal")

    def test_no_principal_at_all_is_refused(self):
        self.assertCode(self.post_grant(self.admin, node=self.tab_node), 409, "missing_principal")

    def test_an_unknown_node_is_named_for_a_hub_admin(self):
        res = self.post_grant(
            self.admin,
            node=("tab", 99999),
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertCode(res, 409, "node_not_found")

    def test_an_unknown_role_is_named(self):
        res = self.post_grant(
            self.admin, node=self.tab_node, role_id=99999, scope_id=self.program_a.id
        )
        self.assertCode(res, 409, "role_not_found")

    def test_an_unknown_user_is_named(self):
        self.assertCode(
            self.post_grant(self.admin, node=self.tab_node, user_id=99999), 409, "user_not_found"
        )

    def test_an_invalid_level_is_422(self):
        res = self.client.post(
            "/v2/rbac/grants",
            json={
                "node_kind": "tab",
                "node_id": self.tab.id,
                "level": "admin",
                "role_id": self.program_member.id,
                "scope_id": self.program_a.id,
            },
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422, res.text)

    def test_an_unknown_field_is_422(self):
        """`StrictRequestModel` is extra="forbid" — a typo'd field must not
        be silently dropped on an authorization surface."""
        res = self.client.post(
            "/v2/rbac/grants",
            json={
                "node_kind": "tab",
                "node_id": self.tab.id,
                "level": "view",
                "role_id": self.program_member.id,
                "scope_id": self.program_a.id,
                "granted_by_email": "spoof@evil.org",
            },
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422, res.text)

    def test_a_refused_write_creates_NOTHING(self):
        """Depends on the fixture's `finally: db.close()` override — under
        the leaky one-liner every other API fixture uses, a handler that
        forgot to roll back would still pass this."""
        self.post_grant(
            self.user("nobody@renphil.org"),
            node=self.tab_node,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
        )
        self.assertEqual(self.rows(), [])

    def test_every_route_requires_authentication(self):
        """401/403 rather than one of them: `HTTPBearer` picks between the
        two by version, and which one it is is not this surface's rule. What
        matters is that no route here is reachable unauthenticated — every
        one of them depends on `get_current_hub_user`, and an anonymous
        caller has no hub_users row for the gate to evaluate."""
        for method, path in (
            (self.client.get, "/v2/rbac/nodes/tab/1/grants"),
            (self.client.post, "/v2/rbac/grants"),
            (self.client.delete, "/v2/rbac/grants/1"),
            (self.client.get, "/v2/rbac/grants/1/retained"),
        ):
            with self.subTest(path=path):
                res = method(path) if method is not self.client.post else method(path, json={})
                self.assertIn(res.status_code, (401, 403), res.text)


# =========================================================================
# Nothing about CONTENT changed
# =========================================================================


class TestContentEnforcementIsNowLive(GrantsApiFixture):
    """UPDATED for project_ac_enforcement_gap.md item 1 (2026-09-03): this
    class used to be `TestNoContentEnforcementWasAdded`, asserting the step
    boundary that content endpoints did NOT yet consult the folds — true
    when the grants router shipped (2026-08-31), false as of the tab-serving
    wiring. `tests/test_tab_visibility_wiring.py` owns the exhaustive
    positive-path coverage (a real grant reveals/grants the right nodes,
    mirrors, component-level redaction, the Hub Admin bypass); this class
    keeps its original, narrower job — a user whose only nearby grant names
    somebody else's pair sees nothing — updated to the now-correct
    assertion instead of the old one.
    """

    def test_the_tab_workspace_404s_a_user_with_no_matching_grant(self):
        """`nobody` holds no assignment and no grant, and the only grant on
        the tab names a DIFFERENT (role, scope) pair. Plan §9: a hidden node
        404s exactly like a missing one, not 403 — so this is what "the
        folds are now consulted" looks like from outside.
        """
        self.grant(
            node=self.tab_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        res = self.client.get(
            f"/v2/tabs/{self.tab.document_id}/workspace", headers=self.user("nobody@renphil.org")
        )
        self.assertEqual(res.status_code, 404, res.text)

    def test_the_nav_tab_list_omits_a_nav_tab_with_no_reachable_seed(self):
        res = self.client.get("/v2/nav-tabs", headers=self.user("nobody@renphil.org"))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"], [], "no seed anywhere reaches nobody@renphil.org")

    def test_the_root_tab_list_omits_both_tabs_for_a_user_with_no_matching_grant(self):
        self.grant(
            node=self.tab_node,
            level="edit",
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
        )
        res = self.client.get("/v2/tabs/root", headers=self.user("nobody@renphil.org"))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"], [], "D2: unset access control matches no one")

    def test_hub_admin_by_jwt_still_sees_everything(self):
        """The bypass that makes this rollout safe (project_ac_enforcement_
        gap.md): `admin` holds no `role_assignments` row in this fixture at
        all, only the JWT role — and must still see both root tabs fully,
        exactly as before this session, or every JWT-only Hub Admin who is
        not one of the (currently one) real assignment rows would go dark.

        2026-11-07: still a JWT-ONLY caller on purpose (`jwt_only_admin`;
        `admin` is a real assignment admin since phase 3). Phase 3 removed
        the JWT branch, so this pins phase 2 behaviour and is expected to
        fail until the owner decides to rewrite or drop it."""
        res = self.client.get("/v2/tabs/root", headers=self.jwt_only_admin)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual({row["id"] for row in res.json()["data"]}, {self.tab.id, self.other_tab.id})


if __name__ == "__main__":
    unittest.main()
