"""Role/scope graph rules — plan_access_control_schema_2026-08-22.md §5.

Same harness as test_access_propagation.py: SQLite in memory on a StaticPool,
env vars stubbed before any app import. Only the six RBAC tables are created,
so none of the JSONB-bearing v2 models are involved and no JSONB->JSON
compile shim is needed here.
"""

from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-rbac-graph-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

from datetime import datetime, timezone  # noqa: E402

from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.services import rbac_graph_service as rbac  # noqa: E402

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


class RbacGraphTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(bind=self.engine, tables=TABLES)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- builders -------------------------------------------------

    def role(
        self, key: str, name: str, depth: int | None = None, public: bool = False
    ) -> RoleV2:
        """`depth` defaults to None — the normal case now that it is nullable
        and constrains nothing. Only TestRoleDepth passes a value."""
        row = RoleV2(
            key=key,
            name=name,
            depth=depth,
            is_system=False,
            is_public=public,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def scope(
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

    def build_org(self) -> None:
        """The graph from §2.5, including the Studio third branch.

        Built with no depths at all. The shape used to be pinned by rank
        (100 / 200 / 300 / 400 top-to-bottom); with that rule disabled the
        edges alone define it, which is the point of these tests.
        """
        self.hub_admin = self.role("hub_admin", "Hub Admin")
        self.program_lead = self.role("program_lead", "Program Lead")
        self.function_lead = self.role("function_lead", "Function Lead")
        self.studio_lead = self.role("studio_lead", "Studio Lead")
        self.program_member = self.role("program_member", "Program Member")
        self.function_member = self.role("function_member", "Function Member")
        self.studio_member = self.role("studio_member", "Studio Member")
        self.hub_member = self.role("hub_member", "Hub Member")

        for lead in (self.program_lead, self.function_lead, self.studio_lead):
            rbac.create_role_edge(self.db, self.hub_admin.id, lead.id)
        for lead, member in (
            (self.program_lead, self.program_member),
            (self.function_lead, self.function_member),
            (self.studio_lead, self.studio_member),
        ):
            rbac.create_role_edge(self.db, lead.id, member.id)
        for member in (self.program_member, self.function_member, self.studio_member):
            rbac.create_role_edge(self.db, member.id, self.hub_member.id)


# ---------------------------------------------------------
# Role graph
# ---------------------------------------------------------


class TestRoleGraph(RbacGraphTestCase):
    def test_full_org_graph_builds(self) -> None:
        self.build_org()
        self.assertEqual(self.db.query(RoleEdgeV2).count(), 9)

    def test_hub_member_has_three_parents(self) -> None:
        """Multi-parent is the whole reason this is a DAG and not a tree."""
        self.build_org()
        parents = {
            row[0]
            for row in self.db.query(RoleEdgeV2.parent_role_id)
            .filter(RoleEdgeV2.child_role_id == self.hub_member.id)
            .all()
        }
        self.assertEqual(
            parents,
            {self.program_member.id, self.function_member.id, self.studio_member.id},
        )

    def test_the_landmine_is_now_ALLOWED(self) -> None:
        """Program Member -> Function Lead.

        THIS IS A DELIBERATE BEHAVIOUR CHANGE, recorded rather than deleted.
        This edge is acyclic, so rank was the only thing that ever caught it
        ("the landmine", §2.5) — and rank is disabled. It is now accepted.

        If the rank rule is ever restored, this test is what fails and tells
        you the delegation surface just narrowed.
        """
        self.build_org()
        rbac.create_role_edge(self.db, self.program_member.id, self.function_lead.id)
        self.assertIn(
            self.function_lead.id, rbac.role_descendants(self.db, self.program_member.id)
        )

    def test_inverted_edge_is_still_rejected_as_a_cycle(self) -> None:
        """Hub Member -> Program Lead. Rank used to catch this; the cycle walk
        catches it now, because Program Lead already reaches Hub Member.

        Not every edge rank used to block is a cycle (see the test above) —
        this one happens to be both, which is why it survives the change with
        only its error code altered."""
        self.build_org()
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, self.hub_member.id, self.program_lead.id)
        self.assertEqual(ctx.exception.code, "cycle")

    def test_former_peers_can_now_be_related_one_way(self) -> None:
        """Program Lead and Function Lead shared rank 200, which made them
        incomparable in both directions. With rank off they are ordinary
        siblings: either direction is legal on its own, and whichever is
        inserted first makes the reverse a cycle."""
        self.build_org()
        rbac.create_role_edge(self.db, self.program_lead.id, self.function_lead.id)
        self.assertIn(
            self.function_lead.id, rbac.role_descendants(self.db, self.program_lead.id)
        )

        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, self.function_lead.id, self.program_lead.id)
        self.assertEqual(ctx.exception.code, "cycle")

    def test_cross_tree_edge_is_allowed(self) -> None:
        """A Lead may parent another tree's Member. This was already true
        under rank (§2.5's documented trade-off: rank encoded authority, not
        lineage) and stays true without it. Asserted so that if it ever
        becomes undesirable, this test is the thing that fails."""
        self.build_org()
        rbac.create_role_edge(self.db, self.program_lead.id, self.function_member.id)
        self.assertIn(self.function_member.id, rbac.role_descendants(self.db, self.program_lead.id))

    def test_self_edge_rejected(self) -> None:
        self.build_org()
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, self.program_lead.id, self.program_lead.id)
        self.assertEqual(ctx.exception.code, "self_edge")

    def test_duplicate_edge_rejected(self) -> None:
        self.build_org()
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, self.hub_admin.id, self.program_lead.id)
        self.assertEqual(ctx.exception.code, "duplicate_edge")

    def test_descendants_and_ancestors(self) -> None:
        self.build_org()
        self.assertEqual(
            rbac.role_descendants(self.db, self.program_lead.id),
            {self.program_lead.id, self.program_member.id, self.hub_member.id},
        )
        # Hub Admin reaches every role.
        self.assertEqual(len(rbac.role_descendants(self.db, self.hub_admin.id)), 8)
        self.assertEqual(
            rbac.role_ancestors(self.db, self.program_member.id),
            {self.program_member.id, self.program_lead.id, self.hub_admin.id},
        )

    def test_delegation_shape_program_member_is_not_a_leaf(self) -> None:
        """§6.5, recorded as a fact rather than a judgement: while Hub Member
        sits in the DAG, Program Member has a proper descendant and can
        therefore delegate. If Hub Member is later lifted out of the graph,
        this set becomes empty and this test is what says so."""
        self.build_org()
        grantable = rbac.role_descendants(self.db, self.program_member.id) - {
            self.program_member.id
        }
        self.assertEqual(grantable, {self.hub_member.id})

    def test_delete_edge(self) -> None:
        self.build_org()
        rbac.delete_role_edge(self.db, self.program_member.id, self.hub_member.id)
        self.assertNotIn(
            self.hub_member.id, rbac.role_descendants(self.db, self.program_member.id)
        )
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.delete_role_edge(self.db, self.program_member.id, self.hub_member.id)
        self.assertEqual(ctx.exception.code, "edge_not_found")


# ---------------------------------------------------------
# Role cycle detection (§5.4)
#
# This class replaces TestRankChange, whose four tests targeted
# set_role_rank / rank_change_conflicts — both commented out along with the
# rank rule. They are not ported: there is no rank invariant left for a rank
# edit to break, so the behaviour they pinned no longer exists. Restoring the
# rule means restoring those tests from git history alongside it.
#
# What replaces them is the protection that took over the same job. These
# mirror TestScopeGraph's cycle tests one-for-one, which is the point — the
# two graphs are symmetric now.
# ---------------------------------------------------------


class TestRoleCycleDetection(RbacGraphTestCase):
    def test_direct_cycle_rejected(self) -> None:
        a = self.role("a", "A")
        b = self.role("b", "B")
        rbac.create_role_edge(self.db, a.id, b.id)
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, b.id, a.id)
        self.assertEqual(ctx.exception.code, "cycle")

    def test_transitive_cycle_rejected(self) -> None:
        a = self.role("a", "A")
        b = self.role("b", "B")
        c = self.role("c", "C")
        rbac.create_role_edge(self.db, a.id, b.id)
        rbac.create_role_edge(self.db, b.id, c.id)
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, c.id, a.id)
        self.assertEqual(ctx.exception.code, "cycle")

    def test_diamond_is_not_a_cycle(self) -> None:
        """Multi-parent convergence is the whole reason this is a DAG rather
        than a tree, and it must not read as a cycle."""
        top = self.role("top", "Top")
        left = self.role("left", "Left")
        right = self.role("right", "Right")
        bottom = self.role("bottom", "Bottom")
        rbac.create_role_edge(self.db, top.id, left.id)
        rbac.create_role_edge(self.db, top.id, right.id)
        rbac.create_role_edge(self.db, left.id, bottom.id)
        rbac.create_role_edge(self.db, right.id, bottom.id)
        self.assertEqual(len(rbac.role_descendants(self.db, top.id)), 4)

    def test_long_chain_cycle_rejected(self) -> None:
        """Rank made cycles structurally impossible at any length. The walk
        has to actually traverse, so depth of chain is worth pinning."""
        chain = [self.role(f"r{i}", f"R{i}") for i in range(6)]
        for parent, child in zip(chain, chain[1:]):
            rbac.create_role_edge(self.db, parent.id, child.id)
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, chain[-1].id, chain[0].id)
        self.assertEqual(ctx.exception.code, "cycle")


# ---------------------------------------------------------
# depth (formerly rank) — inert metadata
# ---------------------------------------------------------


class TestRoleDepth(RbacGraphTestCase):
    def test_depth_is_nullable_and_does_not_gate_edges(self) -> None:
        """The two facts requirements 2 and 3 asked for, in one place: a role
        may have no depth at all, and an edge whose depths run 'backwards'
        (parent 900, child 100 — an inverted rank under the old rule) is
        accepted."""
        deep = self.role("deep", "Deep", depth=900)
        shallow = self.role("shallow", "Shallow", depth=100)
        unset = self.role("unset", "Unset")
        self.assertIsNone(unset.depth)

        rbac.create_role_edge(self.db, deep.id, shallow.id)
        rbac.create_role_edge(self.db, shallow.id, unset.id)
        self.assertEqual(len(rbac.role_descendants(self.db, deep.id)), 3)

    def test_equal_depth_roles_can_be_related(self) -> None:
        """Equal ranks were peers and could never be related in either
        direction. Equal depths mean nothing."""
        a = self.role("a", "A", depth=200)
        b = self.role("b", "B", depth=200)
        rbac.create_role_edge(self.db, a.id, b.id)
        self.assertIn(b.id, rbac.role_descendants(self.db, a.id))


# ---------------------------------------------------------
# Scope graph
# ---------------------------------------------------------


class TestScopeGraph(RbacGraphTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.all_scopes = self.scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = self.scope("scope_a", "Scope A")
        self.scope_b = self.scope("scope_b", "Scope B")
        self.hr = self.scope("hr", "HR")
        self.onboarding = self.scope("onboarding", "Onboarding")

    def test_universal_scope_contains_everything_including_later_scopes(self) -> None:
        before = rbac.scope_descendants(self.db, self.all_scopes.id)
        self.assertIn(self.scope_a.id, before)

        created_later = self.scope("scope_c", "Scope C")
        after = rbac.scope_descendants(self.db, self.all_scopes.id)
        self.assertIn(created_later.id, after)
        self.assertEqual(len(after), len(before) + 1)

    def test_only_one_universal_scope(self) -> None:
        """Enforced by the partial unique index, not by application code.
        This is also what catches the index losing its WHERE clause on a
        dialect that does not understand it — without the predicate the
        index would instead reject the second ORDINARY scope above."""
        self.db.add(
            ScopeV2(
                key="all_scopes_2",
                name="All Scopes 2",
                is_universal=True,
                is_system=False,
                created_at=_now(),
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_universal_scope_cannot_take_edges(self) -> None:
        for parent, child in (
            (self.all_scopes.id, self.scope_a.id),
            (self.scope_a.id, self.all_scopes.id),
        ):
            with self.assertRaises(rbac.RbacGraphError) as ctx:
                rbac.create_scope_edge(self.db, parent, child)
            self.assertEqual(ctx.exception.code, "universal_scope_edge")

    def test_composite_scope_spans_branches(self) -> None:
        """Onboarding holding both a program scope and a function scope is
        the case that killed the old `branch` column."""
        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        rbac.create_scope_edge(self.db, self.onboarding.id, self.hr.id)
        self.assertEqual(
            rbac.scope_descendants(self.db, self.onboarding.id),
            {self.onboarding.id, self.scope_a.id, self.hr.id},
        )

    def test_scope_multi_parent(self) -> None:
        all_programs = self.scope("all_programs", "All Programs")
        rbac.create_scope_edge(self.db, all_programs.id, self.scope_a.id)
        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        self.assertEqual(
            rbac.scope_ancestors(self.db, self.scope_a.id),
            {self.scope_a.id, all_programs.id, self.onboarding.id},
        )

    def test_direct_cycle_rejected(self) -> None:
        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_scope_edge(self.db, self.scope_a.id, self.onboarding.id)
        self.assertEqual(ctx.exception.code, "cycle")

    def test_transitive_cycle_rejected(self) -> None:
        """A -> B -> C, then C -> A. Each edge is fine alone; the third
        closes a loop only a full descendant walk can see."""
        rbac.create_scope_edge(self.db, self.scope_a.id, self.scope_b.id)
        rbac.create_scope_edge(self.db, self.scope_b.id, self.hr.id)
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_scope_edge(self.db, self.hr.id, self.scope_a.id)
        self.assertEqual(ctx.exception.code, "cycle")

    def test_self_and_duplicate_edges_rejected(self) -> None:
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_scope_edge(self.db, self.scope_a.id, self.scope_a.id)
        self.assertEqual(ctx.exception.code, "self_edge")

        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        self.assertEqual(ctx.exception.code, "duplicate_edge")

    def test_diamond_is_not_a_cycle(self) -> None:
        """Two paths to the same scope must stay legal — the scope graph's
        equivalent of Hub Member's two parents."""
        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_b.id)
        rbac.create_scope_edge(self.db, self.scope_a.id, self.hr.id)
        rbac.create_scope_edge(self.db, self.scope_b.id, self.hr.id)
        self.assertEqual(
            rbac.scope_descendants(self.db, self.onboarding.id),
            {self.onboarding.id, self.scope_a.id, self.scope_b.id, self.hr.id},
        )


# ---------------------------------------------------------
# The ⊥ flags — "Any Role" / "Any Scope"
# (plan_access_control_algorithm_2026-08-27.md §4.4)
#
# The bottom of each lattice, mirroring is_universal's top. Every role
# inherits ⊥role and every scope contains ⊥scope, so both are unioned into
# every descendant set unconditionally — which is what makes "open to
# everyone" one grant row on a node instead of one per scope anyone holds.
# ---------------------------------------------------------


class TestPublicRole(RbacGraphTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.any_role = self.role("any_role", "Any Role", public=True)

    def test_public_role_is_in_every_descendant_set(self) -> None:
        """The defining property. Note it holds for a LEAF as much as for
        the root: Hub Member has no children at all and still reaches ⊥."""
        for role in (self.hub_admin, self.program_lead, self.hub_member):
            self.assertIn(
                self.any_role.id,
                rbac.role_descendants(self.db, role.id),
                f"{role.name!r} should reach the public role",
            )

    def test_public_role_reaches_only_itself(self) -> None:
        """⊥ is the bottom: nothing is beneath it. The union adds it to its
        own walk, which is a no-op — not a second copy, and not everything."""
        self.assertEqual(
            rbac.role_descendants(self.db, self.any_role.id), {self.any_role.id}
        )

    def test_a_role_created_later_also_reaches_it(self) -> None:
        """The point of a flag over N edges: nobody has to remember to wire
        a new role up to the bottom, so nothing can be silently forgotten."""
        newcomer = self.role("intern", "Intern")
        self.assertIn(self.any_role.id, rbac.role_descendants(self.db, newcomer.id))

    def test_ancestors_do_not_gain_it(self) -> None:
        """Deliberate asymmetry, matching scope_ancestors' treatment of the
        universal scope: ⊥ is beneath everything, so it is never an
        ANCESTOR, and this direction must not be used to ask "is X covered
        by Y"."""
        self.assertNotIn(
            self.any_role.id, rbac.role_ancestors(self.db, self.program_member.id)
        )

    def test_only_one_public_role(self) -> None:
        """Enforced by the partial unique index. Also what catches the index
        losing its WHERE clause on a dialect that does not understand it —
        without the predicate it would instead have rejected the second
        ORDINARY role in build_org() above."""
        self.db.add(
            RoleV2(
                key="any_role_2",
                name="Any Role 2",
                is_system=False,
                is_public=True,
                created_at=_now(),
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_public_role_cannot_take_edges_in_either_direction(self) -> None:
        for parent, child in (
            (self.any_role.id, self.program_lead.id),
            (self.program_lead.id, self.any_role.id),
        ):
            with self.assertRaises(rbac.RbacGraphError) as ctx:
                rbac.create_role_edge(self.db, parent, child)
            self.assertEqual(ctx.exception.code, "public_role_edge")

    def test_the_guard_beats_the_cycle_check_to_it(self) -> None:
        """ORDERING, and this is the whole reason the guard sits where it
        sits (§4.4's second hazard). ⊥ is in every descendant set, so
        `parent in role_descendants(child)` is TRIVIALLY TRUE for any edge
        whose parent is ⊥ — reached in the other order, an ordinary mistake
        comes back as 'Any Role already inherits from Program Lead', a cycle
        error naming a cycle that does not exist.

        Asserted as `!= "cycle"` as well as `== "public_role_edge"` so that
        a future reordering fails here loudly rather than degrading into a
        misleading message nobody notices."""
        self.assertIn(
            self.any_role.id, rbac.role_descendants(self.db, self.program_lead.id)
        )
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_role_edge(self.db, self.any_role.id, self.program_lead.id)
        self.assertNotEqual(ctx.exception.code, "cycle")
        self.assertEqual(ctx.exception.code, "public_role_edge")


class TestPublicScope(RbacGraphTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.all_scopes = self.scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = self.scope("scope_a", "Scope A")
        self.hr = self.scope("hr", "HR")
        self.onboarding = self.scope("onboarding", "Onboarding")
        rbac.create_scope_edge(self.db, self.onboarding.id, self.scope_a.id)
        self.any_scope = self.scope("any_scope", "Any Scope", public=True)

    def test_public_scope_is_in_every_descendant_set(self) -> None:
        for scope in (self.scope_a, self.hr, self.onboarding):
            self.assertIn(
                self.any_scope.id,
                rbac.scope_descendants(self.db, scope.id),
                f"{scope.name!r} should contain the public scope",
            )

    def test_universal_scope_already_contains_it(self) -> None:
        """No special case needed on that branch: "every scope row" includes
        ⊥ by definition, which is why scope_descendants only unions on the
        ordinary path."""
        self.assertIn(
            self.any_scope.id, rbac.scope_descendants(self.db, self.all_scopes.id)
        )

    def test_public_scope_contains_only_itself(self) -> None:
        self.assertEqual(
            rbac.scope_descendants(self.db, self.any_scope.id), {self.any_scope.id}
        )

    def test_a_scope_created_later_also_contains_it(self) -> None:
        newcomer = self.scope("scope_z", "Scope Z")
        self.assertIn(self.any_scope.id, rbac.scope_descendants(self.db, newcomer.id))

    def test_ancestors_do_not_gain_it(self) -> None:
        self.assertNotIn(
            self.any_scope.id, rbac.scope_ancestors(self.db, self.scope_a.id)
        )

    def test_unknown_scope_still_covers_nothing(self) -> None:
        """The empty-set branch must not pick up ⊥ on its way out. An id
        that does not exist covers nothing at all — including the bottom."""
        self.assertEqual(rbac.scope_descendants(self.db, 9999), set())

    def test_only_one_public_scope(self) -> None:
        self.db.add(
            ScopeV2(
                key="any_scope_2",
                name="Any Scope 2",
                is_universal=False,
                is_system=False,
                is_public=True,
                created_at=_now(),
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_a_scope_cannot_be_both_ends_of_the_lattice(self) -> None:
        """The CHECK. A row that was both would short-circuit to every scope
        AND be appended to every other walk — the top and the bottom at
        once, which is not a coherent thing for a closure to mean.

        Enforced in the database, not just by create_scope's named
        `universal_and_public` error (covered in test_rbac_api.py), because
        the service check is a courtesy and this is the guarantee."""
        self.db.add(
            ScopeV2(
                key="both",
                name="Both Ends",
                is_universal=True,
                is_system=False,
                is_public=True,
                created_at=_now(),
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_public_scope_cannot_take_edges_in_either_direction(self) -> None:
        for parent, child in (
            (self.any_scope.id, self.scope_a.id),
            (self.scope_a.id, self.any_scope.id),
        ):
            with self.assertRaises(rbac.RbacGraphError) as ctx:
                rbac.create_scope_edge(self.db, parent, child)
            self.assertEqual(ctx.exception.code, "public_scope_edge")

    def test_the_guard_beats_the_cycle_check_to_it(self) -> None:
        """The scope-side mirror of TestPublicRole's ordering test. Here the
        existing is_universal guard already held the correct position, so
        adding ⊥ to the same loop inherited it — this pins that it stays."""
        self.assertIn(self.any_scope.id, rbac.scope_descendants(self.db, self.scope_a.id))
        with self.assertRaises(rbac.RbacGraphError) as ctx:
            rbac.create_scope_edge(self.db, self.any_scope.id, self.scope_a.id)
        self.assertNotEqual(ctx.exception.code, "cycle")
        self.assertEqual(ctx.exception.code, "public_scope_edge")


# ---------------------------------------------------------
# The picker contract
# ---------------------------------------------------------


class TestPickerOffersExactlyWhatTheServerAccepts(RbacGraphTestCase):
    """The requirement behind the client-side filters: a parent/child
    dropdown must offer a node if and only if adding that edge keeps the
    graph a DAG.

    The filtering itself lives in the frontend (AccessManagementPage.tsx's
    roleParentCandidates / roleChildCandidates / scopeParentCandidates /
    scopeChildCandidates), computed from the one-hop parent_ids/child_ids the
    list endpoints already return. That split is deliberate — no extra
    round-trip, and the server re-validates regardless — but it means the two
    rules are written twice and can drift.

    These tests pin the equivalence by brute force: for every ordered pair of
    nodes, compare what the client rule WOULD offer against what
    create_*_edge actually accepts. `_client_descendants` below is a verbatim
    port of the TSX `descendantsOf` helper, so a change on either side that
    breaks the correspondence fails here.
    """

    @staticmethod
    def _client_descendants(start: int, edges: list[tuple[int, int]]) -> set[int]:
        """Verbatim port of descendantsOf() in AccessManagementPage.tsx."""
        adjacency: dict[int, list[int]] = {}
        for parent, child in edges:
            adjacency.setdefault(parent, []).append(child)
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for nxt in adjacency.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def _server_accepts(self, create, delete, pairs: list[tuple[int, int]]) -> set[int]:
        """Which of `pairs` the validator actually takes. Each accepted edge
        is removed again immediately, so the graph is unchanged on exit —
        cheaper and less driver-dependent than a SAVEPOINT per attempt."""
        accepted = set()
        for parent, child, candidate in pairs:
            try:
                create(self.db, parent, child)
            except rbac.RbacGraphError:
                continue
            accepted.add(candidate)
            delete(self.db, parent, child)
        return accepted

    # -- roles ----------------------------------------------------

    def _role_fixture(self) -> tuple[list[RoleV2], list[tuple[int, int]]]:
        self.build_org()
        # One cross-tree edge so the graph is a genuine DAG with converging
        # paths, not a tree — a tree would not exercise the walk.
        rbac.create_role_edge(self.db, self.program_lead.id, self.function_member.id)
        roles = self.db.query(RoleV2).all()
        edges = [
            (row[0], row[1])
            for row in self.db.query(RoleEdgeV2.parent_role_id, RoleEdgeV2.child_role_id).all()
        ]
        return roles, edges

    def test_role_parent_picker(self) -> None:
        roles, edges = self._role_fixture()
        ids = [r.id for r in roles]

        for node in roles:
            parent_ids = {p for p, c in edges if c == node.id}
            descendants = self._client_descendants(node.id, edges)
            offered = {
                c for c in ids
                if c != node.id and c not in parent_ids and c not in descendants
            }
            accepted = self._server_accepts(
                rbac.create_role_edge,
                rbac.delete_role_edge,
                [(c, node.id, c) for c in ids],
            )
            self.assertEqual(offered, accepted, f"parent picker mismatch on {node.name!r}")

    def test_role_child_picker(self) -> None:
        roles, edges = self._role_fixture()
        ids = [r.id for r in roles]

        for node in roles:
            child_ids = {c for p, c in edges if p == node.id}
            offered = {
                c for c in ids
                if c != node.id
                and c not in child_ids
                and node.id not in self._client_descendants(c, edges)
            }
            accepted = self._server_accepts(
                rbac.create_role_edge,
                rbac.delete_role_edge,
                [(node.id, c, c) for c in ids],
            )
            self.assertEqual(offered, accepted, f"child picker mismatch on {node.name!r}")

    # -- scopes ---------------------------------------------------

    def _scope_fixture(self) -> tuple[list[ScopeV2], list[tuple[int, int]]]:
        self.scope("all_scopes", "All Scopes", universal=True)
        a = self.scope("a", "A")
        b = self.scope("b", "B")
        c = self.scope("c", "C")
        d = self.scope("d", "D")
        rbac.create_scope_edge(self.db, a.id, b.id)
        rbac.create_scope_edge(self.db, a.id, c.id)
        rbac.create_scope_edge(self.db, b.id, d.id)
        rbac.create_scope_edge(self.db, c.id, d.id)
        scopes = self.db.query(ScopeV2).all()
        edges = [
            (row[0], row[1])
            for row in self.db.query(ScopeEdgeV2.parent_scope_id, ScopeEdgeV2.child_scope_id).all()
        ]
        return scopes, edges

    def test_scope_pickers(self) -> None:
        """Same contract on the scope side, including the universal scope —
        which the client filters out of every picker (§5.5) and the server
        refuses with `universal_scope_edge`."""
        scopes, edges = self._scope_fixture()
        ids = [s.id for s in scopes]
        ordinary = [s for s in scopes if not s.is_universal]

        for node in ordinary:
            parent_ids = {p for p, c in edges if c == node.id}
            child_ids = {c for p, c in edges if p == node.id}
            descendants = self._client_descendants(node.id, edges)
            universal_ids = {s.id for s in scopes if s.is_universal}

            offered_parents = {
                c for c in ids
                if c not in universal_ids
                and c != node.id
                and c not in parent_ids
                and c not in descendants
            }
            self.assertEqual(
                offered_parents,
                self._server_accepts(
                    rbac.create_scope_edge, rbac.delete_scope_edge, [(c, node.id, c) for c in ids]
                ),
                f"scope parent picker mismatch on {node.name!r}",
            )

            offered_children = {
                c for c in ids
                if c not in universal_ids
                and c != node.id
                and c not in child_ids
                and node.id not in self._client_descendants(c, edges)
            }
            self.assertEqual(
                offered_children,
                self._server_accepts(
                    rbac.create_scope_edge, rbac.delete_scope_edge, [(node.id, c, c) for c in ids]
                ),
                f"scope child picker mismatch on {node.name!r}",
            )


if __name__ == "__main__":
    unittest.main()
