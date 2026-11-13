"""Object grants — plan_access_control_algorithm_2026-08-27.md §7, §4.2, §8.1.

Service-level, not router-level: step 3 builds the table and the matching
primitive and stops before any endpoint, so there is nothing to drive a
TestClient against. The harness is test_rbac_graph.py's — SQLite in memory on
a StaticPool, env stubbed before any app import — with two additions that
test_rbac_api.py carries and that are load-bearing here:

  - the ``@compiles(JSONB, "sqlite")`` shim, because the four node tables and
    their two support tables all carry JSONB columns;
  - ``PRAGMA foreign_keys=ON``, WITHOUT WHICH HALF THIS FILE PASSES
    VACUOUSLY. SQLite does not enforce foreign keys by default, so every
    cascade-on-node-delete and restrict-on-role-delete assertion below would
    succeed against a database doing nothing at all.

No session-leak concern arises here: this module never goes through a
FastAPI dependency override, so ``test_rbac_assignments_api.py``'s leaky
one-liner fixture is not in play either way.
"""

from __future__ import annotations

import os
import unittest

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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-resource-grants-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from datetime import datetime, timezone  # noqa: E402
from typing import get_args  # noqa: E402

from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.resource_grant import (  # noqa: E402
    GRANT_LEVELS,
    NODE_COLUMNS,
    NODE_KINDS,
    ResourceGrantV2,
)
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.schemas.resource_grants import (  # noqa: E402
    GrantLevel,
    NodeKind,
    ResourceGrantResponse,
)
from app.services import rbac_service  # noqa: E402
from app.services import resource_grant_service as grants  # noqa: E402
from app.services.rbac_graph_service import RbacGraphError  # noqa: E402

TABLES = [
    HubV2.__table__,
    NavTabV2.__table__,
    TabV2.__table__,
    GridstackV2.__table__,
    PageContentV2.__table__,
    ComponentV2.__table__,
    UserV2.__table__,
    RoleV2.__table__,
    ScopeV2.__table__,
    RoleEdgeV2.__table__,
    ScopeEdgeV2.__table__,
    RoleAssignmentV2.__table__,
    ResourceGrantV2.__table__,
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GrantTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(bind=self.engine, tables=TABLES)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self._build_nodes()
        self._build_org()
        # COMMITTED, not just flushed. Several tests below have to roll back —
        # a CHECK or unique violation leaves the session unusable until they
        # do — and a rollback would otherwise discard the whole fixture along
        # with the row under test, so the next statement would fail on a
        # missing foreign key rather than on what it was asserting.
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- node tree ----------------------------------------------------

    def _build_nodes(self) -> None:
        """One of each node kind, wired the way the real schema wires them.

        The gridstack is not a node (§3.2 — it is a transparent pass-through
        and holds no grants), but a component cannot exist without one, so it
        is built and then ignored.
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

        self.gridstack = GridstackV2(document_id="tab-1", parent_tab_id=self.tab.id)
        self.db.add(self.gridstack)
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

    def _assign(self, user: UserV2, role: RoleV2, scope: ScopeV2) -> None:
        self.db.add(
            RoleAssignmentV2(
                user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=_now()
            )
        )
        self.db.flush()

    def _build_org(self) -> None:
        """A role chain and four scopes, matching §5.3's shape.

        Edges point parent -> child and mean "the parent inherits everything
        the child has", so ``role_descendants(program_lead)`` is
        {program_lead, program_member, hub_member} — everything AT OR BELOW
        it. That is the direction the whole match rule turns on.
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

        self.alice = self._user("alice@renphil.org")
        self.bob = self._user("bob@renphil.org")
        self.admin = self._user("admin@renphil.org")

    # -- grant builders -----------------------------------------------

    def _grant(self, **kwargs) -> ResourceGrantV2:
        """Raw insert, bypassing the service, so the DATABASE's own rules are
        what these assertions are testing."""
        row = ResourceGrantV2(created_at=_now(), **kwargs)
        self.db.add(row)
        self.db.flush()
        return row

    def _pair_grant(self, role: RoleV2, scope: ScopeV2, node=None, level="view"):
        node = node if node is not None else self.tab
        return grants.create_grant(
            self.db,
            node_kind=self._kind_of(node),
            node_id=node.id,
            role_id=role.id,
            scope_id=scope.id,
            level=level,
        )

    def _kind_of(self, node) -> str:
        return {
            HubV2: "hub",
            NavTabV2: "nav_tab",
            TabV2: "tab",
            ComponentV2: "component",
        }[type(node)]

    def _matched_nodes(self, user: UserV2) -> set[tuple[str, int]]:
        return {(m.node_kind, m.node_id) for m in grants.matching_grants(self.db, user.id)}


# =====================================================================
# The two exclusive arcs (§7)
# =====================================================================


class TestNodeArcCheck(GrantTestCase):
    """`ck_resource_grants_one_node` — exactly one of the four node columns.

    Written as CASE WHEN rather than the Postgres-idiomatic
    `(x IS NOT NULL)::int` precisely so it runs here; if it ever reverts,
    these three fail on SQLite rather than passing and leaving production as
    the only place the rule exists.
    """

    def test_no_node_is_rejected(self):
        with self.assertRaises(IntegrityError):
            self._grant(role_id=self.hub_member.id, scope_id=self.program_a.id, level="view")

    def test_two_nodes_are_rejected(self):
        with self.assertRaises(IntegrityError):
            self._grant(
                tab_id=self.tab.id,
                nav_tab_id=self.nav_tab.id,
                role_id=self.hub_member.id,
                scope_id=self.program_a.id,
                level="view",
            )

    def test_all_four_nodes_are_rejected(self):
        with self.assertRaises(IntegrityError):
            self._grant(
                hub_id=self.hub.id,
                nav_tab_id=self.nav_tab.id,
                tab_id=self.tab.id,
                component_id=self.component.id,
                role_id=self.hub_member.id,
                scope_id=self.program_a.id,
                level="view",
            )

    def test_each_node_kind_alone_is_accepted(self):
        for kind, node in (
            ("hub", self.hub),
            ("nav_tab", self.nav_tab),
            ("tab", self.tab),
            ("component", self.component),
        ):
            with self.subTest(kind=kind):
                row = self._grant(
                    **{NODE_COLUMNS[kind]: node.id},
                    role_id=self.hub_member.id,
                    scope_id=self.program_a.id,
                    level="view",
                )
                self.assertEqual(grants.node_of(row), (kind, node.id))


class TestPrincipalArcCheck(GrantTestCase):
    """`ck_resource_grants_one_principal` — the pair, or a user, never both."""

    def test_both_forms_are_rejected(self):
        with self.assertRaises(IntegrityError):
            self._grant(
                tab_id=self.tab.id,
                role_id=self.hub_member.id,
                scope_id=self.program_a.id,
                user_id=self.alice.id,
                level="view",
            )

    def test_neither_form_is_rejected(self):
        with self.assertRaises(IntegrityError):
            self._grant(tab_id=self.tab.id, level="view")

    def test_half_a_pair_is_rejected(self):
        """A role with no scope would read as "this role on every scope" if it
        were ever allowed through — the pairing trap wearing a different hat."""
        with self.assertRaises(IntegrityError):
            self._grant(tab_id=self.tab.id, role_id=self.hub_member.id, level="view")

        self.db.rollback()
        with self.assertRaises(IntegrityError):
            self._grant(tab_id=self.tab.id, scope_id=self.program_a.id, level="view")

    def test_pair_alone_and_user_alone_are_accepted(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(tab_id=self.tab.id, user_id=self.alice.id, level="view")


class TestLevelCheck(GrantTestCase):
    def test_unknown_level_is_rejected(self):
        """A third level would not error anywhere on the read path — it would
        simply never match, i.e. a grant that silently does nothing."""
        with self.assertRaises(IntegrityError):
            self._grant(
                tab_id=self.tab.id,
                role_id=self.hub_member.id,
                scope_id=self.program_a.id,
                level="admin",
            )


# =====================================================================
# The uniqueness rule (hazard 1 — §7's UNIQUE is inert as written)
# =====================================================================


class TestUniquenessRule(GrantTestCase):
    """Eight partial unique indexes, one per node kind × principal form.

    §7 specifies a plain `UNIQUE` over all eight columns, which enforces
    NOTHING: every row has at least three NULLs in that list and NULLs are
    distinct in a unique constraint on both dialects, so exact duplicates
    both insert. THESE TESTS ARE THE REASON THE REPLACEMENT WAS CHOSEN THE
    WAY IT WAS — `NULLS NOT DISTINCT` is silently dropped by SQLAlchemy on
    SQLite, so under it every `assertRaises` below would fail and the rule
    would exist only in production.
    """

    def test_duplicate_pair_grant_is_rejected(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        with self.assertRaises(IntegrityError):
            self._grant(
                tab_id=self.tab.id,
                role_id=self.hub_member.id,
                scope_id=self.program_a.id,
                level="view",
            )

    def test_duplicate_user_grant_is_rejected(self):
        self._grant(tab_id=self.tab.id, user_id=self.alice.id, level="view")
        with self.assertRaises(IntegrityError):
            self._grant(tab_id=self.tab.id, user_id=self.alice.id, level="view")

    def test_every_node_kind_is_covered(self):
        """One index pair per node kind, so a duplicate must be refused on all
        four — not just on whichever one happened to get tested."""
        for kind, node in (
            ("hub", self.hub),
            ("nav_tab", self.nav_tab),
            ("tab", self.tab),
            ("component", self.component),
        ):
            with self.subTest(kind=kind):
                self._grant(
                    **{NODE_COLUMNS[kind]: node.id},
                    role_id=self.hub_member.id,
                    scope_id=self.program_a.id,
                    level="view",
                )
                with self.assertRaises(IntegrityError):
                    self._grant(
                        **{NODE_COLUMNS[kind]: node.id},
                        role_id=self.hub_member.id,
                        scope_id=self.program_a.id,
                        level="view",
                    )
                self.db.rollback()

    def test_same_pair_at_a_different_level_is_allowed(self):
        """`level` is in the key on purpose — a node holding both view and
        edit for one principal is an ordinary bag of grants, not a conflict."""
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="edit",
        )

    def test_same_id_under_a_different_node_kind_is_allowed(self):
        """The one thing a naive fix gets wrong in the other direction: ids
        are per-table, so tab 1 and component 1 are different nodes and a
        grant on each is two legitimate rows."""
        self._grant(
            tab_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(
            component_id=self.component.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(
            nav_tab_id=self.nav_tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )

    def test_multiple_distinct_grants_on_one_node_are_allowed(self):
        """§7's "a node carries a bag of grants, not an ACL" — three ordinary
        rows on one node, which must not trip any of the eight."""
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="edit",
        )
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_b.id,
            level="edit",
        )
        self._grant(tab_id=self.tab.id, user_id=self.alice.id, level="edit")
        self.assertEqual(len(grants.list_grants_for_node(self.db, "tab", self.tab.id)), 4)


# =====================================================================
# Foreign key discipline (§7)
# =====================================================================


class TestCascadeOnNodeDelete(GrantTestCase):
    """Deleting a node takes its grants with it.

    This is the property the four-nullable-FK node arc was chosen FOR. Under
    a polymorphic `(node_type, node_id)` pair these rows would survive their
    node, and since ids are reused, a stale grant on tab 41 becomes a live
    grant on whatever tab 41 is next.
    """

    def _count(self) -> int:
        return self.db.query(ResourceGrantV2).count()

    def _delete_tab(self, tab: TabV2) -> None:
        """Remove a tab the way the content tree requires: components, then
        gridstacks, then the tab itself.

        `gridstacks.parent_tab_id` carries no ON DELETE at all, so a bare
        `DELETE FROM tabs` is refused by that foreign key long before the
        grant cascade under test is reached. That is the content tree's own
        delete ordering — `gridstack_service` owns it — and it is stepped
        around here rather than asserted on.
        """
        grid_ids = [
            g.id
            for g in self.db.query(GridstackV2.id).filter(GridstackV2.parent_tab_id == tab.id)
        ]
        if grid_ids:
            self.db.query(ComponentV2).filter(ComponentV2.gridstack_id.in_(grid_ids)).delete(
                synchronize_session=False
            )
            self.db.query(GridstackV2).filter(GridstackV2.parent_tab_id == tab.id).delete(
                synchronize_session=False
            )
        self.db.delete(tab)
        self.db.flush()

    def test_deleting_a_tab_removes_its_grants(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(
            tab_id=self.other_tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self.assertEqual(self._count(), 2)

        self._delete_tab(self.tab)
        # The sibling's grant is untouched — the cascade follows the FK, not
        # the table.
        self.assertEqual(self._count(), 1)
        self.assertEqual(
            len(grants.list_grants_for_node(self.db, "tab", self.other_tab.id)), 1
        )

    def test_deleting_a_component_removes_its_grants(self):
        self._grant(
            component_id=self.component.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self.db.delete(self.component)
        self.db.flush()
        self.assertEqual(self._count(), 0)

    def test_deleting_a_nav_tab_removes_its_grants(self):
        self._grant(
            nav_tab_id=self.nav_tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        # The tabs beneath it have no ON DELETE on their own nav_tab_id, so
        # remove them first — this is asserting the grant cascade, not the
        # content tree's own delete ordering.
        self._delete_tab(self.tab)
        self._delete_tab(self.other_tab)
        self.db.delete(self.nav_tab)
        self.db.flush()
        self.assertEqual(self._count(), 0)

    def test_deleting_a_person_removes_their_direct_grants(self):
        """CASCADE on user_id, deliberately the opposite of the pair's
        RESTRICT: there is nothing for an admin to review, because the
        principal itself is gone."""
        self._grant(tab_id=self.tab.id, user_id=self.alice.id, level="edit")
        self._grant(tab_id=self.tab.id, user_id=self.bob.id, level="edit")
        self.db.delete(self.alice)
        self.db.flush()
        self.assertEqual(self._count(), 1)

    def test_deleting_a_granter_keeps_the_grant_and_its_email(self):
        """SET NULL on granted_by_user_id, with granted_by_email surviving —
        §6.2's "granted directly by Sam, 3 Feb" has to keep working after Sam
        leaves."""
        row = self._grant(
            tab_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
            granted_by_user_id=self.admin.id,
            granted_by_email="admin@renphil.org",
        )
        self.db.delete(self.admin)
        self.db.flush()
        self.db.refresh(row)
        self.assertIsNone(row.granted_by_user_id)
        self.assertEqual(row.granted_by_email, "admin@renphil.org")


class TestRestrictOnPrincipalDelete(GrantTestCase):
    """RESTRICT on role_id / scope_id — deleting a role or scope that is still
    granted somewhere must not silently revoke access."""

    def test_deleting_a_granted_role_is_refused_by_the_database(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self.db.delete(self.program_lead)
        with self.assertRaises(IntegrityError):
            self.db.flush()

    def test_deleting_a_granted_scope_is_refused_by_the_database(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self.db.delete(self.program_a)
        with self.assertRaises(IntegrityError):
            self.db.flush()


class TestDeleteRefusesWithANamedError(GrantTestCase):
    """Hazard 2 — `delete_role` / `delete_scope` pre-count grants.

    Without the second count, a role that is GRANTED on a node but ASSIGNED
    to nobody sails past the assignment check and is refused by the FK
    instead: an opaque IntegrityError, i.e. a 500, from a code path that
    already knows how to say why.
    """

    def test_delete_role_names_the_grant_count(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self._grant(
            nav_tab_id=self.nav_tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_b.id,
            level="edit",
        )
        with self.assertRaises(RbacGraphError) as caught:
            rbac_service.delete_role(self.db, self.program_lead.id)
        self.assertEqual(caught.exception.code, "role_granted_on_nodes")
        self.assertEqual(caught.exception.details["grant_count"], 2)

    def test_delete_scope_names_the_grant_count(self):
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="view",
        )
        with self.assertRaises(RbacGraphError) as caught:
            rbac_service.delete_scope(self.db, self.program_a.id)
        self.assertEqual(caught.exception.code, "scope_granted_on_nodes")
        self.assertEqual(caught.exception.details["grant_count"], 1)

    def test_the_two_counts_are_independent(self):
        """Assignments are checked first, so clearing every grant still leaves
        the assignment refusal — and vice versa. A distinct code for each is
        what makes that legible to whoever is trying to delete the role."""
        self._assign(self.alice, self.program_lead, self.program_a)
        self._grant(
            tab_id=self.tab.id,
            role_id=self.program_lead.id,
            scope_id=self.program_a.id,
            level="view",
        )

        with self.assertRaises(RbacGraphError) as caught:
            rbac_service.delete_role(self.db, self.program_lead.id)
        self.assertEqual(caught.exception.code, "role_in_use")

        self.db.query(RoleAssignmentV2).delete()
        self.db.flush()

        with self.assertRaises(RbacGraphError) as caught:
            rbac_service.delete_role(self.db, self.program_lead.id)
        self.assertEqual(caught.exception.code, "role_granted_on_nodes")

    def test_an_ungranted_role_still_deletes(self):
        self.assertTrue(rbac_service.delete_role(self.db, self.function_member.id))


# =====================================================================
# §4.2 — the match direction
# =====================================================================


class TestMatchDirection(GrantTestCase):
    """A stored grant matches when it sits AT OR BELOW a pair the user holds,
    on BOTH axes. The inverse reading — "the grant names my pair or any of its
    parents" — would hand every Program Member whatever was granted to Hub
    Admin.
    """

    def test_a_grant_below_the_held_pair_matches(self):
        self._assign(self.alice, self.program_lead, self.program_a)
        self._pair_grant(self.program_member, self.program_a)
        self.assertEqual(self._matched_nodes(self.alice), {("tab", self.tab.id)})

    def test_a_grant_above_the_held_pair_does_not_match(self):
        self._assign(self.alice, self.program_member, self.program_a)
        self._pair_grant(self.program_lead, self.program_a)
        self.assertEqual(self._matched_nodes(self.alice), set())

    def test_the_exact_pair_matches(self):
        """"The grant names my exact pair" and "the grant names a pair below
        mine" are the same lookup — the closure already contains both, so
        there is no separate case to branch on (§4.2)."""
        self._assign(self.alice, self.program_member, self.program_a)
        self._pair_grant(self.program_member, self.program_a)
        self.assertEqual(self._matched_nodes(self.alice), {("tab", self.tab.id)})

    def test_an_unrelated_scope_does_not_match(self):
        self._assign(self.alice, self.program_lead, self.program_a)
        self._pair_grant(self.program_member, self.program_b)
        self.assertEqual(self._matched_nodes(self.alice), set())

    def test_a_user_with_no_assignments_matches_nothing(self):
        """D2/§10 item 1: `role_assignments` is empty in production, so
        `effective(U)` is empty for everyone and nothing is reachable. The
        read path must handle that without erroring on the empty IN clause."""
        self._pair_grant(self.program_member, self.program_a)
        self.assertEqual(self._matched_nodes(self.alice), set())


class TestUniversalScopeIsNarrowestOnAnObjectGrant(GrantTestCase):
    """§4.3 — the corollary that reads like the opposite of what it is.

    `(Hub Member, All Scopes)` STORED on a node reaches only users whose own
    assignment is hub-wide. On an object grant the universal scope is the most
    RESTRICTIVE choice available, not the most permissive.
    """

    def test_a_narrowly_scoped_user_does_not_reach_it(self):
        self._assign(self.alice, self.hub_member, self.program_a)
        self._pair_grant(self.hub_member, self.all_scopes)
        self.assertEqual(self._matched_nodes(self.alice), set())

    def test_a_hub_wide_user_does_reach_it(self):
        self._assign(self.bob, self.hub_member, self.all_scopes)
        self._pair_grant(self.hub_member, self.all_scopes)
        self.assertEqual(self._matched_nodes(self.bob), {("tab", self.tab.id)})

    def test_a_hub_wide_user_reaches_a_narrow_grant_too(self):
        """The asymmetry, from the other side: holding the universal scope
        covers every scope row, so a grant on Program A is reached."""
        self._assign(self.bob, self.hub_member, self.all_scopes)
        self._pair_grant(self.hub_member, self.program_a)
        self.assertEqual(self._matched_nodes(self.bob), {("tab", self.tab.id)})


class TestBottomGrantReachesEveryone(GrantTestCase):
    """§4.4 — `(Any Role, Any Scope)` is what makes "open to everyone" ONE
    row, independently of how anyone is scoped.

    This is the case §11.1 says no ordinary pair can express: Alice on
    (Program Member, A) and Bob on (Function Member, B) share no pair at all.
    """

    def test_two_users_on_disjoint_scopes_both_reach_one_bottom_grant(self):
        self._assign(self.alice, self.program_member, self.program_a)
        self._assign(self.bob, self.function_member, self.program_b)

        self._pair_grant(self.any_role, self.any_scope)

        self.assertEqual(self._matched_nodes(self.alice), {("tab", self.tab.id)})
        self.assertEqual(self._matched_nodes(self.bob), {("tab", self.tab.id)})

    def test_no_ordinary_pair_reaches_both(self):
        """The control for the test above: without ⊥ there is nothing to
        write that covers them both, which is why the flag exists."""
        self._assign(self.alice, self.program_member, self.program_a)
        self._assign(self.bob, self.function_member, self.program_b)

        self._pair_grant(self.hub_member, self.program_a)

        self.assertEqual(self._matched_nodes(self.alice), {("tab", self.tab.id)})
        self.assertEqual(self._matched_nodes(self.bob), set())

    def test_a_bottom_scope_with_an_ordinary_role_still_narrows_by_role(self):
        """`(Hub Member, Any Scope)` reaches everyone at Hub Member OR ABOVE
        — which is exactly why §4.4 puts ⊥ on both axes rather than just on
        scopes. Nobody holds a role beneath Hub Member here, so both users
        match; the day one is added, this grant silently narrows and
        `(Any Role, Any Scope)` does not."""
        self._assign(self.alice, self.program_member, self.program_a)
        self._assign(self.bob, self.function_member, self.program_b)

        self._pair_grant(self.hub_member, self.any_scope)

        self.assertEqual(self._matched_nodes(self.alice), {("tab", self.tab.id)})
        self.assertEqual(self._matched_nodes(self.bob), {("tab", self.tab.id)})

    def test_the_bottom_is_not_refused_on_the_grants_path(self):
        """Hazard 3. ⊥ is refused as an ASSIGNMENT
        (`rbac_assignments.py::_assert_not_public`) and that refusal is a
        security control. On an OBJECT GRANT it is the whole point of the
        flag, so the refusal is deliberately NOT copied here. If someone ever
        does copy it, this is what goes red."""
        created = self._pair_grant(self.any_role, self.any_scope)
        self.assertEqual(created["role_id"], self.any_role.id)
        self.assertEqual(created["scope_id"], self.any_scope.id)


class TestThePairingTrap(GrantTestCase):
    """§4.1 / §8.1 step 2 — the wide SQL filter is a SUPERSET and the Python
    filter is what makes the answer correct.

    Alice holds (Program Lead, A) and (Program Member, B). The SQL below her
    matches on `role_id IN {lead, member, hub_member} AND scope_id IN {A, B}`,
    which ADMITS (Program Lead, B) — a pair she does not hold together. If
    the per-row filter is ever removed, or the pairing is "optimised" into
    SQL, this is the test that goes red.
    """

    def setUp(self) -> None:
        super().setUp()
        self._assign(self.alice, self.program_lead, self.program_a)
        self._assign(self.alice, self.program_member, self.program_b)

    def test_the_cross_product_pair_does_not_match(self):
        self._pair_grant(self.program_lead, self.program_b)
        self.assertEqual(self._matched_nodes(self.alice), set())

    def test_the_wide_sql_filter_really_does_admit_it(self):
        """Asserts the trap EXISTS, so the test above cannot pass vacuously.
        If the SQL filter were somehow already exact, the guard it protects
        would be untested and nobody would know."""
        from app.services.rbac_graph_service import effective_pairs

        pairs = effective_pairs(self.db, self.alice.id)
        reachable_roles = {r for r, _ in pairs}
        reachable_scopes = {s for _, s in pairs}

        self.assertIn(self.program_lead.id, reachable_roles)
        self.assertIn(self.program_b.id, reachable_scopes)
        # ...and yet the pair itself is not held.
        self.assertNotIn((self.program_lead.id, self.program_b.id), pairs)

    def test_both_genuinely_held_pairs_still_match(self):
        self._pair_grant(self.program_member, self.program_a)
        self._pair_grant(self.program_member, self.program_b, node=self.other_tab)
        self.assertEqual(
            self._matched_nodes(self.alice),
            {("tab", self.tab.id), ("tab", self.other_tab.id)},
        )


class TestDirectUserGrants(GrantTestCase):
    """D5 — a grant may name a user directly, and it needs no assignment at
    all. `automations@renphil.org` depends on this today."""

    def test_a_direct_grant_matches_with_no_assignments(self):
        grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="edit"
        )
        self.assertEqual(self._matched_nodes(self.alice), {("tab", self.tab.id)})

    def test_someone_elses_direct_grant_does_not_match(self):
        grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="edit"
        )
        self.assertEqual(self._matched_nodes(self.bob), set())

    def test_the_level_is_carried_through(self):
        grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="edit"
        )
        matched = grants.matching_grants(self.db, self.alice.id)
        self.assertEqual([m.level for m in matched], ["edit"])


class TestUserEmailResolution(GrantTestCase):
    """Handoff §4.1 — a user-form grant's `user_id` resolves to an email on
    every read path, the same live-join pattern
    `rbac_assignment_service._hub_user_email_map` already uses for
    `AssignmentResponse.user_email` (safe because `user_id` CASCADEs, §7)."""

    def test_create_grant_resolves_the_email_immediately(self):
        created = grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="edit"
        )
        self.assertEqual(created["user_email"], "alice@renphil.org")

    def test_a_pair_form_grant_has_no_user_email(self):
        created = self._pair_grant(self.hub_member, self.program_a)
        self.assertIsNone(created["user_id"])
        self.assertIsNone(created["user_email"])

    def test_get_grant_resolves_the_email(self):
        created = grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.bob.id, level="view"
        )
        fetched = grants.get_grant(self.db, created["id"])
        self.assertEqual(fetched["user_email"], "bob@renphil.org")

    def test_list_grants_for_node_resolves_every_grantees_email_in_one_bulk_query(self):
        grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="view"
        )
        grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.bob.id, level="edit"
        )
        listed = grants.list_grants_for_node(self.db, "tab", self.tab.id)
        by_user = {g["user_id"]: g["user_email"] for g in listed}
        self.assertEqual(by_user, {self.alice.id: "alice@renphil.org", self.bob.id: "bob@renphil.org"})

    def test_list_grants_by_ids_resolves_the_email(self):
        created = grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="view"
        )
        listed = grants.list_grants_by_ids(self.db, [created["id"]])
        self.assertEqual(listed[0]["user_email"], "alice@renphil.org")

    def test_the_response_schema_accepts_the_new_field(self):
        created = grants.create_grant(
            self.db, node_kind="tab", node_id=self.tab.id, user_id=self.alice.id, level="view"
        )
        parsed = ResourceGrantResponse.model_validate(created)
        self.assertEqual(parsed.user_email, "alice@renphil.org")


class TestMatchingAcrossNodeKinds(GrantTestCase):
    def test_every_node_kind_comes_back_addressed(self):
        """The seed set §5.1's folds consume — each match carries the node it
        sits on, resolved out of the four-column arc."""
        self._assign(self.alice, self.hub_member, self.program_a)
        for node in (self.hub, self.nav_tab, self.tab, self.component):
            self._pair_grant(self.hub_member, self.program_a, node=node)

        self.assertEqual(
            self._matched_nodes(self.alice),
            {
                ("hub", self.hub.id),
                ("nav_tab", self.nav_tab.id),
                ("tab", self.tab.id),
                ("component", self.component.id),
            },
        )


# =====================================================================
# CRUD
# =====================================================================


class TestGrantCrud(GrantTestCase):
    def test_create_list_get_delete(self):
        created = self._pair_grant(self.hub_member, self.program_a)
        self.assertEqual(created["node_kind"], "tab")
        self.assertEqual(created["level"], "view")

        listed = grants.list_grants_for_node(self.db, "tab", self.tab.id)
        self.assertEqual([g["id"] for g in listed], [created["id"]])

        # Compared field by field rather than as whole dicts: SQLite stores no
        # timezone, so a re-read `created_at` comes back naive where the one
        # built in this session is aware. That difference is the storage
        # layer's, not this function's.
        fetched = grants.get_grant(self.db, created["id"])
        for key in ("id", "node_kind", "node_id", "role_id", "scope_id", "user_id", "level"):
            self.assertEqual(fetched[key], created[key], key)

        self.assertTrue(grants.delete_grant(self.db, created["id"]))
        self.assertEqual(grants.list_grants_for_node(self.db, "tab", self.tab.id), [])
        self.assertIsNone(grants.get_grant(self.db, created["id"]))

    def test_deleting_a_missing_grant_returns_false(self):
        self.assertFalse(grants.delete_grant(self.db, 9999))

    def test_a_bottom_grant_can_still_be_revoked(self):
        """"Refuse the way in, never the way out" — the same rule the ⊥
        assignment refusal follows. The widest grant in the system must not be
        the one thing nobody can take away."""
        created = self._pair_grant(self.any_role, self.any_scope)
        self.assertTrue(grants.delete_grant(self.db, created["id"]))

    def test_duplicate_is_a_named_error_not_an_integrity_error(self):
        self._pair_grant(self.hub_member, self.program_a)
        with self.assertRaises(RbacGraphError) as caught:
            self._pair_grant(self.hub_member, self.program_a)
        self.assertEqual(caught.exception.code, "duplicate_grant")

    def test_list_is_scoped_to_the_node(self):
        self._pair_grant(self.hub_member, self.program_a, node=self.tab)
        self._pair_grant(self.hub_member, self.program_a, node=self.other_tab)
        self.assertEqual(len(grants.list_grants_for_node(self.db, "tab", self.tab.id)), 1)

    def test_a_component_and_a_tab_with_the_same_id_do_not_collide(self):
        """`list_grants_for_node` filters on the node's OWN column, so ids
        colliding across tables cannot leak one node's grants into another's."""
        self._pair_grant(self.hub_member, self.program_a, node=self.component)
        listed = grants.list_grants_for_node(self.db, "tab", self.component.id)
        self.assertEqual(listed, [])


class TestGrantValidation(GrantTestCase):
    def _expect(self, code: str, **kwargs):
        with self.assertRaises(RbacGraphError) as caught:
            grants.create_grant(self.db, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_unknown_node_kind(self):
        self._expect(
            "invalid_node_kind",
            node_kind="gridstack",
            node_id=1,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )

    def test_missing_node(self):
        self._expect(
            "node_not_found",
            node_kind="tab",
            node_id=9999,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )

    def test_unknown_level(self):
        self._expect(
            "invalid_level",
            node_kind="tab",
            node_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="admin",
        )

    def test_both_principal_forms(self):
        self._expect(
            "ambiguous_principal",
            node_kind="tab",
            node_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            user_id=self.alice.id,
            level="view",
        )

    def test_no_principal(self):
        self._expect("missing_principal", node_kind="tab", node_id=self.tab.id, level="view")

    def test_half_a_pair(self):
        self._expect(
            "incomplete_pair",
            node_kind="tab",
            node_id=self.tab.id,
            role_id=self.hub_member.id,
            level="view",
        )

    def test_missing_role_and_scope(self):
        self._expect(
            "role_not_found",
            node_kind="tab",
            node_id=self.tab.id,
            role_id=9999,
            scope_id=self.program_a.id,
            level="view",
        )
        self._expect(
            "scope_not_found",
            node_kind="tab",
            node_id=self.tab.id,
            role_id=self.hub_member.id,
            scope_id=9999,
            level="view",
        )

    def test_missing_user(self):
        self._expect(
            "user_not_found",
            node_kind="tab",
            node_id=self.tab.id,
            user_id=9999,
            level="view",
        )

    def test_a_refused_create_writes_nothing(self):
        self._expect(
            "node_not_found",
            node_kind="tab",
            node_id=9999,
            role_id=self.hub_member.id,
            scope_id=self.program_a.id,
            level="view",
        )
        self.assertEqual(self.db.query(ResourceGrantV2).count(), 0)


# =====================================================================
# Wire shapes
# =====================================================================


class TestSchemaAgreesWithTheModel(GrantTestCase):
    """`app/schemas/resource_grants.py` spells its two Literals out, because a
    Literal needs literal members. These keep it honest — adding a node kind
    to the model and not to the schema fails here rather than silently
    rejecting valid requests at the edge."""

    def test_node_kinds_agree(self):
        self.assertEqual(get_args(NodeKind), NODE_KINDS)

    def test_levels_agree(self):
        self.assertEqual(get_args(GrantLevel), GRANT_LEVELS)

    def test_node_columns_cover_every_kind(self):
        self.assertEqual(tuple(NODE_COLUMNS), NODE_KINDS)
        self.assertEqual(set(NODE_COLUMNS), set(grants.NODE_MODELS))

    def test_serialize_grant_validates_against_the_response_model(self):
        serialized = grants.create_grant(
            self.db,
            node_kind="component",
            node_id=self.component.id,
            role_id=self.any_role.id,
            scope_id=self.any_scope.id,
            level="edit",
            granted_by_user_id=self.admin.id,
            granted_by_email="admin@renphil.org",
        )
        parsed = ResourceGrantResponse.model_validate(serialized)
        self.assertEqual(parsed.node_kind, "component")
        self.assertEqual(parsed.node_id, self.component.id)
        self.assertIsNone(parsed.user_id)

    def test_a_user_grant_serializes_with_no_pair(self):
        serialized = grants.create_grant(
            self.db, node_kind="hub", node_id=self.hub.id, user_id=self.alice.id, level="view"
        )
        parsed = ResourceGrantResponse.model_validate(serialized)
        self.assertEqual(parsed.user_id, self.alice.id)
        self.assertIsNone(parsed.role_id)
        self.assertIsNone(parsed.scope_id)


if __name__ == "__main__":
    unittest.main()
