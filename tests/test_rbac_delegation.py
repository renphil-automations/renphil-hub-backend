"""The §6 delegation rule — plan_access_control_schema_2026-08-22.md §6,
session_handoff_2026-08-24-permission-management-plan.md §3, §4.

Same in-memory-SQLite harness as test_rbac_graph.py: only the six RBAC
tables, env vars stubbed before any app import. Exercises `can_delegate` /
`assert_can_delegate` directly against `role_assignments` rows, not through
the HTTP layer — the router-level gate is covered by
test_rbac_assignments_api.py.
"""

from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, event
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-rbac-delegation-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

from datetime import datetime, timezone  # noqa: E402

from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.services import rbac_graph_service as graph  # noqa: E402
from app.services.rbac_delegation_service import assert_can_delegate, can_delegate  # noqa: E402
from app.services.rbac_graph_service import (  # noqa: E402
    RbacGraphError,
    effective_pairs,
    held_closures,
)

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


class RbacDelegationTestCase(unittest.TestCase):
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
        # `depth` is inert metadata now; the delegation rule never reads it.
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

    def hub_user(self, email: str) -> UserV2:
        row = UserV2(email=email, name=email.split("@")[0], has_signed_in=True, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def assign(self, user: UserV2, role: RoleV2, scope: ScopeV2, granter: UserV2 | None = None) -> RoleAssignmentV2:
        row = RoleAssignmentV2(
            user_id=user.id,
            role_id=role.id,
            scope_id=scope.id,
            granted_by_user_id=granter.id if granter else None,
            granted_by_email=granter.email if granter else None,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def build_org(self) -> None:
        """Same shape as test_rbac_graph.py's build_org, plus scopes A/B/HR
        and Onboarding (children = A + HR) from design doc §9's worked
        example, so delegation tests can be written against the exact
        table there."""
        self.hub_admin = self.role("hub_admin", "Hub Admin")
        self.program_lead = self.role("program_lead", "Program Lead")
        self.function_lead = self.role("function_lead", "Function Lead")
        self.program_member = self.role("program_member", "Program Member")
        self.function_member = self.role("function_member", "Function Member")
        self.hub_member = self.role("hub_member", "Hub Member")

        graph.create_role_edge(self.db, self.hub_admin.id, self.program_lead.id)
        graph.create_role_edge(self.db, self.hub_admin.id, self.function_lead.id)
        graph.create_role_edge(self.db, self.program_lead.id, self.program_member.id)
        graph.create_role_edge(self.db, self.function_lead.id, self.function_member.id)
        graph.create_role_edge(self.db, self.program_member.id, self.hub_member.id)
        graph.create_role_edge(self.db, self.function_member.id, self.hub_member.id)

        self.all_scopes = self.scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = self.scope("scope_a", "Scope A")
        self.scope_b = self.scope("scope_b", "Scope B")
        self.scope_hr = self.scope("hr", "HR")
        self.scope_onboarding = self.scope("onboarding", "Onboarding")
        graph.create_scope_edge(self.db, self.scope_onboarding.id, self.scope_a.id)
        graph.create_scope_edge(self.db, self.scope_onboarding.id, self.scope_hr.id)


# ---------------------------------------------------------
# can_delegate — design doc §9's worked example, verbatim
# ---------------------------------------------------------


class TestCanDelegate(RbacDelegationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = self.hub_user("alice@renphil.org")
        self.assign(self.alice, self.program_lead, self.scope_a)
        self.assign(self.alice, self.program_member, self.scope_b)

    def test_proper_descendant_on_own_scope_allowed(self) -> None:
        """§9: Grant Carol Program Member on A — proper descendant of
        Program Lead, and A is Alice's own scope."""
        self.assertTrue(can_delegate(self.db, self.alice.id, self.program_member.id, self.scope_a.id))

    def test_equal_role_rejected(self) -> None:
        """§9: Grant Carol Program Lead on A — equal role, not a PROPER
        descendant."""
        self.assertFalse(can_delegate(self.db, self.alice.id, self.program_lead.id, self.scope_a.id))

    def test_non_descendant_role_rejected(self) -> None:
        """§9: Grant Carol Hub Admin on A — not a descendant at all."""
        self.assertFalse(can_delegate(self.db, self.alice.id, self.hub_admin.id, self.scope_a.id))

    def test_the_trap_rejected(self) -> None:
        """§6.2 / §9's central case: Alice is Program Lead on A AND Program
        Member on B — two SEPARATE rows. Grant Carol Program Member on B
        must fail, because Alice's authority on B is Program Member
        (equal, not proper-descendant), not Program Lead. A union-of-roles-
        crossed-with-union-of-scopes implementation would wrongly allow
        this, since {Program Lead, Program Member} x {A, B} contains
        (Program Member, B)."""
        self.assertFalse(can_delegate(self.db, self.alice.id, self.program_member.id, self.scope_b.id))

    def test_descendant_on_a_containing_scope_rejected(self) -> None:
        """§9: Grant Carol Program Member on All Scopes — All Scopes is a
        PARENT of A, never a descendant. scope_descendants never climbs."""
        self.assertFalse(can_delegate(self.db, self.alice.id, self.program_member.id, self.all_scopes.id))

    def test_grant_on_uneligible_role_but_contained_scope_allowed(self) -> None:
        """Design doc §9: Program Lead on HR is allowed with no eligibility
        rule remaining — Alice, Program Lead on A, may grant Program Member
        on HR only if HR is within some scope she holds. She does not hold
        HR itself, so this specific grant is NOT delegable to her (HR is
        not a descendant of A) — asserted to pin down the boundary, not
        confuse it with the §9 table entry about role/scope ELIGIBILITY at
        the definitions layer, which is a different rule this module does
        not implement."""
        self.assertFalse(can_delegate(self.db, self.alice.id, self.program_member.id, self.scope_hr.id))

    def test_onboarding_contains_a_so_delegable_via_composite_scope(self) -> None:
        """A holder of (Program Lead, Onboarding) can grant Program Member
        on A, since A is a descendant of Onboarding — scope inclusion
        follows containment, not just the exact scope held."""
        bob = self.hub_user("bob@renphil.org")
        self.assign(bob, self.program_lead, self.scope_onboarding)
        self.assertTrue(can_delegate(self.db, bob.id, self.program_member.id, self.scope_a.id))
        self.assertTrue(can_delegate(self.db, bob.id, self.program_member.id, self.scope_hr.id))

    def test_self_grant_is_technically_delegable_but_a_no_op(self) -> None:
        """§9: Alice granting HERSELF Program Member on A is allowed by the
        rule (Program Member IS a proper descendant of Program Lead on the
        same scope) — it is simply a no-op in effect, since she already
        holds it via the role closure. Not this module's job to special-
        case "target == granter"; §6.3 explains why none is needed."""
        self.assertTrue(can_delegate(self.db, self.alice.id, self.program_member.id, self.scope_a.id))

    def test_no_assignments_cannot_delegate_anything(self) -> None:
        nobody = self.hub_user("nobody@renphil.org")
        self.assertFalse(can_delegate(self.db, nobody.id, self.hub_member.id, self.scope_a.id))

    def test_leaf_role_cannot_delegate(self) -> None:
        """A holder of Hub Member (a leaf — no children) has no proper
        descendants, so can_delegate is False for every target — derived,
        not a declared can_delegate flag (design doc §6.1)."""
        carol = self.hub_user("carol@renphil.org")
        self.assign(carol, self.hub_member, self.scope_a)
        self.assertFalse(can_delegate(self.db, carol.id, self.hub_member.id, self.scope_a.id))


# ---------------------------------------------------------
# assert_can_delegate — the Hub Admin bypass (handoff §3.1)
# ---------------------------------------------------------


class TestAssertCanDelegate(RbacDelegationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = self.hub_user("alice@renphil.org")
        self.assign(self.alice, self.program_lead, self.scope_a)

    def test_hub_admin_bypasses_even_with_zero_assignments(self) -> None:
        """The bootstrap case (handoff §7): an admin with NO role_assignments
        rows at all must still be able to create the very first one."""
        admin = self.hub_user("admin@renphil.org")
        assert_can_delegate(
            self.db,
            granter_hub_user_id=admin.id,
            is_hub_admin=True,
            role_id=self.hub_admin.id,
            scope_id=self.all_scopes.id,
        )  # does not raise

    def test_hub_admin_bypasses_the_normal_rule_entirely(self) -> None:
        """A Hub Admin may grant anything, including something no
        role_assignments row of theirs would otherwise cover."""
        admin = self.hub_user("admin2@renphil.org")
        assert_can_delegate(
            self.db,
            granter_hub_user_id=admin.id,
            is_hub_admin=True,
            role_id=self.program_lead.id,
            scope_id=self.scope_b.id,
        )  # does not raise

    def test_non_admin_within_rule_passes(self) -> None:
        assert_can_delegate(
            self.db,
            granter_hub_user_id=self.alice.id,
            is_hub_admin=False,
            role_id=self.program_member.id,
            scope_id=self.scope_a.id,
        )  # does not raise

    def test_non_admin_outside_rule_raises_not_delegable(self) -> None:
        with self.assertRaises(RbacGraphError) as ctx:
            assert_can_delegate(
                self.db,
                granter_hub_user_id=self.alice.id,
                is_hub_admin=False,
                role_id=self.program_lead.id,
                scope_id=self.scope_a.id,
            )
        self.assertEqual(ctx.exception.code, "not_delegable")
        self.assertEqual(ctx.exception.details["role_id"], self.program_lead.id)
        self.assertEqual(ctx.exception.details["scope_id"], self.scope_a.id)


# ---------------------------------------------------------
# effective_pairs — plan_access_control_algorithm_2026-08-27.md §4.1, §8.1
# ---------------------------------------------------------


class TestEffectivePairs(RbacDelegationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.alice = self.hub_user("alice@renphil.org")
        # TWO SEPARATE ROWS, and the difference between them is the point:
        # a higher role on A, a lower role on B.
        self.assign(self.alice, self.program_lead, self.scope_a)
        self.assign(self.alice, self.program_member, self.scope_b)

    def test_expansion_is_per_assignment_row(self) -> None:
        """THE TRAP, asserted as an exact set rather than a spot check.

        Alice is Program Lead on A and Program Member on B. Her effective
        pairs are row1's closures crossed, unioned with row2's closures
        crossed — never the union of her roles crossed with the union of her
        scopes. The broken version is the easier query to write and yields
        {Program Lead, Program Member, Hub Member} × {A, B}, which contains
        (Program Lead, B): authority on B that she does not have.

        Exact equality is deliberate. A subset assertion would pass against
        the cross-product implementation, which is the one bug this test
        exists to catch.
        """
        expected = {
            # Row 1: (Program Lead, A) -> {PL, PM, HM} × {A}
            (self.program_lead.id, self.scope_a.id),
            (self.program_member.id, self.scope_a.id),
            (self.hub_member.id, self.scope_a.id),
            # Row 2: (Program Member, B) -> {PM, HM} × {B}
            (self.program_member.id, self.scope_b.id),
            (self.hub_member.id, self.scope_b.id),
        }
        self.assertEqual(effective_pairs(self.db, self.alice.id), expected)

    def test_the_cross_product_pair_is_absent(self) -> None:
        """The same fact stated on its own, so a failure names the culprit
        instead of dumping two five-element sets."""
        self.assertNotIn(
            (self.program_lead.id, self.scope_b.id),
            effective_pairs(self.db, self.alice.id),
        )

    def test_no_assignments_is_the_empty_set(self) -> None:
        """§10 item 1's production reality: role_assignments is empty, so
        every user is here today. With enforcement on, this is what makes
        the whole hub go dark — hence "populate assignments first, verify,
        then flip enforcement", never the other order."""
        nobody = self.hub_user("nobody@renphil.org")
        self.assertEqual(effective_pairs(self.db, nobody.id), set())

    def test_universal_scope_row_expands_to_every_scope(self) -> None:
        """A universal scope in an ASSIGNMENT means what it reads like:
        every scope, present and future. (On an OBJECT grant it means close
        to the opposite — §4.3 — which is why the picker must label them.)"""
        bob = self.hub_user("bob@renphil.org")
        self.assign(bob, self.hub_member, self.all_scopes)
        every_scope = {
            self.all_scopes.id,
            self.scope_a.id,
            self.scope_b.id,
            self.scope_hr.id,
            self.scope_onboarding.id,
        }
        self.assertEqual(
            effective_pairs(self.db, bob.id),
            {(self.hub_member.id, s) for s in every_scope},
        )

    def test_composite_scope_expands_through_containment(self) -> None:
        """Onboarding contains A and HR, so one row reaches three scopes."""
        carol = self.hub_user("carol@renphil.org")
        self.assign(carol, self.hub_member, self.scope_onboarding)
        self.assertEqual(
            {s for _r, s in effective_pairs(self.db, carol.id)},
            {self.scope_onboarding.id, self.scope_a.id, self.scope_hr.id},
        )

    def test_bottom_flags_widen_every_row(self) -> None:
        """With ⊥ present, every assignment row reaches it on both axes —
        which is precisely what makes a `(⊥role, ⊥scope)` grant written on a
        NODE reachable by everybody, however they are scoped (§4.4)."""
        any_role = self.role("any_role", "Any Role", public=True)
        any_scope = self.scope("any_scope", "Any Scope", public=True)
        pairs = effective_pairs(self.db, self.alice.id)
        self.assertIn((any_role.id, any_scope.id), pairs)
        # And it is genuinely still per-row: ⊥ widens each row, it does not
        # merge them.
        self.assertNotIn((self.program_lead.id, self.scope_b.id), pairs)

    def test_two_users_on_disjoint_scopes_share_only_the_bottom(self) -> None:
        """§11.1's case, and why ⊥ exists on both axes. Alice on
        (Program Member, A) and Bob on (Function Member, B) share no
        ordinary SCOPE at all — so before ⊥ there was no single grant row
        that reached both, and "public to the hub" was inexpressible."""
        any_role = self.role("any_role", "Any Role", public=True)
        any_scope = self.scope("any_scope", "Any Scope", public=True)

        alice2 = self.hub_user("alice2@renphil.org")
        bob = self.hub_user("bob2@renphil.org")
        self.assign(alice2, self.program_member, self.scope_a)
        self.assign(bob, self.function_member, self.scope_b)

        shared = effective_pairs(self.db, alice2.id) & effective_pairs(self.db, bob.id)
        self.assertEqual(
            shared,
            {
                (self.hub_member.id, any_scope.id),
                (any_role.id, any_scope.id),
            },
        )

    def test_held_closures_keeps_the_rows_apart(self) -> None:
        """The primitive underneath. Two rows in, two rows out, each with
        its own pair of closures — flattening is the caller's choice, never
        the helper's."""
        rows = held_closures(self.db, self.alice.id)
        self.assertEqual(len(rows), 2)
        by_role = {r.role_id: r for r in rows}
        self.assertEqual(by_role[self.program_lead.id].scope_ids, {self.scope_a.id})
        self.assertEqual(by_role[self.program_member.id].scope_ids, {self.scope_b.id})

    def test_edge_tables_are_loaded_once_per_call(self) -> None:
        """§8.2, and the reason this is worth a test rather than a comment:
        the free closure helpers rescan an entire edge table on EVERY call,
        and that is the shape which makes the read-time visibility fold
        unusable if copied. Alice has two assignment rows; an implementation
        calling the free role_descendants/scope_descendants per row would
        scan each edge table twice.

        Counted at the driver rather than by asserting on internals, so it
        fails however the caching comes to be bypassed."""
        counts = {"parent_child_roles": 0, "parent_child_scopes": 0}

        @event.listens_for(self.engine, "before_cursor_execute")
        def _count(_conn, _cursor, statement, _params, _context, _many):
            for table in counts:
                if f"FROM {table}" in statement:
                    counts[table] += 1

        try:
            effective_pairs(self.db, self.alice.id)
        finally:
            event.remove(self.engine, "before_cursor_execute", _count)

        self.assertEqual(counts["parent_child_roles"], 1, counts)
        self.assertEqual(counts["parent_child_scopes"], 1, counts)


# ---------------------------------------------------------
# ⊥ and delegation — the hole, and where it is closed
# ---------------------------------------------------------


class TestBottomFlagsAndDelegation(RbacDelegationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.any_role = self.role("any_role", "Any Role", public=True)
        self.any_scope = self.scope("any_scope", "Any Scope", public=True)
        self.alice = self.hub_user("alice@renphil.org")
        self.assign(self.alice, self.program_lead, self.scope_a)

    def test_the_scope_half_of_the_rule_passes_for_everyone(self) -> None:
        """RECORDED AS A FACT, NOT AS APPROVAL (§4.4's "one power to watch").

        ⊥ is in scope_descendants(anything), so the scope half of §6.1 can
        never refuse a ⊥ target — for any user, whatever they hold. This
        asserts the hole exists, so that nobody removes the thing that
        closes it and finds the suite still green.

        WHAT CLOSES IT: `_assert_not_public` in routers/rbac_assignments.py
        refuses ⊥ as an assignment outright, and runs BEFORE this check.
        See test_rbac_assignments_api.TestBottomNotAssignable.
        """
        self.assertTrue(
            can_delegate(self.db, self.alice.id, self.program_member.id, self.any_scope.id)
        )

    def test_the_role_half_passes_for_anyone_holding_any_role(self) -> None:
        """The mirror on the other axis: ⊥role is a proper descendant of
        every role, so it clears the "strictly beneath me" test for
        everybody — including a Hub Member, who can otherwise delegate
        nothing at all (contrast the first assertion with the second)."""
        carol = self.hub_user("carol@renphil.org")
        self.assign(carol, self.hub_member, self.scope_a)
        self.assertFalse(
            can_delegate(self.db, carol.id, self.hub_member.id, self.scope_a.id)
        )
        self.assertTrue(can_delegate(self.db, carol.id, self.any_role.id, self.scope_a.id))

    def test_ordinary_delegation_is_unchanged_by_the_flags(self) -> None:
        """The flags must not perturb anything that does not name ⊥. The
        core cases from TestCanDelegate, re-run with both ⊥ rows present."""
        self.assign(self.alice, self.program_member, self.scope_b)
        self.assertTrue(
            can_delegate(self.db, self.alice.id, self.program_member.id, self.scope_a.id)
        )
        self.assertFalse(
            can_delegate(self.db, self.alice.id, self.program_lead.id, self.scope_a.id)
        )
        self.assertFalse(
            can_delegate(self.db, self.alice.id, self.hub_admin.id, self.scope_a.id)
        )
        # The pairing trap, still caught with ⊥ in play.
        self.assertFalse(
            can_delegate(self.db, self.alice.id, self.program_member.id, self.scope_b.id)
        )

    def test_can_delegate_is_not_membership_in_effective_pairs(self) -> None:
        """Why can_delegate does NOT call effective_pairs, pinned as a test
        because the consolidation looks obviously correct and is not.

        Alice holds (Program Lead, A), so that pair IS in her effective
        pairs — §4.2's match direction says she reaches a grant stored
        there. But §6.1 needs a PROPER descendant, and properness is a
        per-row fact that flattening destroys, so she may not GRANT it.
        Anything built on `(role, scope) in effective_pairs(...)` would let
        every user delegate their own role.
        """
        pair = (self.program_lead.id, self.scope_a.id)
        self.assertIn(pair, effective_pairs(self.db, self.alice.id))
        self.assertFalse(can_delegate(self.db, self.alice.id, *pair))
