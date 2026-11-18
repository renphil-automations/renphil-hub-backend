"""Read-time visibility — plan_access_control_algorithm_2026-08-27.md §3, §5, §6.1, §8.1.

Step 4: the node tree and the two folds. Service-level, like
``test_resource_grants.py`` — step 4 builds no router and wires nothing into
an endpoint, so there is nothing to drive a TestClient against and no FastAPI
dependency override is in play.

The harness is ``test_resource_grants.py``'s, and both of its additions are
load-bearing here for the same reasons:

  - the ``@compiles(JSONB, "sqlite")`` shim, because every node table carries
    JSONB columns and ``components.props`` is where a mirror's target lives;
  - ``PRAGMA foreign_keys=ON``, so the self-referential FKs this file leans
    on (``components.super_blocknote_id``, ``gridstacks.parent_id``,
    ``tabs.parent_tab_id``) are real rather than decorative;
  - ``setUp`` COMMITS rather than flushing, so a test that rolls back does
    not take the fixture tree with it.

§5.4'S SIX ARE WRITTEN AS PROPERTY TESTS, NOT EXAMPLE TESTS, and the
distinction is what makes them worth having: each one is asserted for EVERY
node in the tree across MANY grant sets (``_all_seed_sets`` enumerates the
powerset of six candidate seed nodes at both levels), rather than for one
hand-picked node under one hand-picked grant. Between them they catch nearly
every implementation bug, which is why they are the ones worth writing first.
"""

from __future__ import annotations

import itertools
import os
import unittest

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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-access-visibility-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from datetime import datetime, timezone  # noqa: E402

from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.resource_grant import (  # noqa: E402
    LEVEL_EDIT,
    LEVEL_VIEW,
    NODE_COLUMNS,
    ResourceGrantV2,
)
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.services import access_visibility_service as visibility  # noqa: E402
from app.services import gridstack_service  # noqa: E402
from app.services.resource_grant_service import GrantMatch  # noqa: E402

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


class VisibilityTestCase(unittest.TestCase):
    """The fixture tree, and it is built to exercise every nesting form at
    once rather than one per test.

    ::

        hub
        └── nav1
            ├── tabA                        (root tab)
            │   ├── widget1                 ordinary widget on tabA's root canvas
            │   ├── sbn_root                a Super Block Note widget
            │   │   └── sbn1 └── sbn2 └── sbn3     depth 4, matching live max
            │   ├── subgrid_rep             a sub-grid representation component
            │   │   └── widget2             a widget on that sub-grid's canvas
            │   └── variantA                a TAB VARIANT of tabA
            │       └── widget3
            └── tabB
                └── mirror1                 mirrors widget1
        orphan_tab                          nav_tab_id IS NULL — §3.3
        └── widget4

    Every gridstack also gets its representation component, the way
    ``_create_gridstack_component`` really creates them — including for root
    gridstacks, where the row exists but its access_control is not the one
    that counts. That is deliberate: the representation component of a root
    gridstack is the shape that would self-loop if resolved through its own
    ``gridstack_id``, and it must be in the fixture for that to be tested.
    """

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
        self._build_tree()
        self._build_org()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- builders -----------------------------------------------------

    def _gridstack(self, tab: TabV2, parent: GridstackV2 | None = None) -> GridstackV2:
        """A gridstack plus the representation component every real gridstack
        is created with (``_create_gridstack_component``), including the
        ``gridstack_id == current_grid_id`` self-reference that makes the
        naive parent resolution loop."""
        grid = GridstackV2(
            document_id=f"grid-{tab.id}-{parent.id if parent else 'root'}",
            parent_id=parent.id if parent else None,
            parent_tab_id=tab.id,
        )
        self.db.add(grid)
        self.db.flush()
        rep = ComponentV2(
            link=f"rep-{grid.id}",
            type=visibility.GRIDSTACK_WIDGET_TYPE,
            gridstack_id=grid.id,
            current_grid_id=grid.id,
        )
        self.db.add(rep)
        self.db.flush()
        grid.representation = rep  # test-only convenience handle
        return grid

    def _component(self, grid: GridstackV2, link: str, ctype: str = "markdown", **kw):
        row = ComponentV2(link=link, type=ctype, gridstack_id=grid.id, **kw)
        self.db.add(row)
        self.db.flush()
        return row

    def _build_tree(self) -> None:
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(slug="dashboard", title="Dashboard", protected=False)
        self.db.add(self.nav1)
        self.db.flush()

        self.tabA = TabV2(document_id="tab-a", title="Root A", nav_tab_id=self.nav1.id)
        self.tabB = TabV2(document_id="tab-b", title="Root B", nav_tab_id=self.nav1.id)
        self.db.add_all([self.tabA, self.tabB])
        self.db.flush()

        # A VARIANT carries its parent's nav_tab_id as well as parent_tab_id
        # (create_tab_variant_v2). Both columns populated is the whole point:
        # it is what makes the branch order in build_node_tree load-bearing.
        self.variantA = TabV2(
            document_id="tab-a-v1",
            title="Variant 1",
            nav_tab_id=self.nav1.id,
            parent_tab_id=self.tabA.id,
        )
        self.orphan_tab = TabV2(document_id="tab-orphan", title="Testing 1", nav_tab_id=None)
        self.db.add_all([self.variantA, self.orphan_tab])
        self.db.flush()

        self.gA = self._gridstack(self.tabA)
        self.gB = self._gridstack(self.tabB)
        self.gV = self._gridstack(self.variantA)
        self.gO = self._gridstack(self.orphan_tab)
        self.g_sub = self._gridstack(self.tabA, parent=self.gA)

        self.widget1 = self._component(self.gA, "widget-1")
        self.widget2 = self._component(self.g_sub, "widget-2")
        self.widget3 = self._component(self.gV, "widget-3")
        self.widget4 = self._component(self.gO, "widget-4")

        self.sbn_root = self._component(self.gA, "sbn-root", ctype="super_block_note")
        self.sbn1 = self._component(
            self.gA, "sbn-1", ctype="block_note", super_blocknote_id=self.sbn_root.id
        )
        self.sbn2 = self._component(
            self.gA, "sbn-2", ctype="block_note", super_blocknote_id=self.sbn1.id
        )
        self.sbn3 = self._component(
            self.gA, "sbn-3", ctype="block_note", super_blocknote_id=self.sbn2.id
        )

        self.mirror1 = self._component(
            self.gB,
            "mirror-1",
            ctype=visibility.MIRROR_WIDGET_TYPE,
            props={"target_link": "widget-1"},
        )

        self.subgrid_rep = self.g_sub.representation

    def _build_org(self) -> None:
        self.hub_admin = RoleV2(key="hub_admin", name="Hub Admin", created_at=_now())
        self.program_lead = RoleV2(key="program_lead", name="Program Lead", created_at=_now())
        self.program_member = RoleV2(
            key="program_member", name="Program Member", created_at=_now()
        )
        self.db.add_all([self.hub_admin, self.program_lead, self.program_member])
        self.db.flush()
        self.db.add_all(
            [
                RoleEdgeV2(
                    parent_role_id=self.hub_admin.id,
                    child_role_id=self.program_lead.id,
                    created_at=_now(),
                ),
                RoleEdgeV2(
                    parent_role_id=self.program_lead.id,
                    child_role_id=self.program_member.id,
                    created_at=_now(),
                ),
            ]
        )
        self.all_scopes = ScopeV2(
            key="all_scopes", name="All Scopes", is_universal=True, created_at=_now()
        )
        self.program_a = ScopeV2(key="program_a", name="Program A", created_at=_now())
        self.db.add_all([self.all_scopes, self.program_a])
        self.db.flush()

        self.alice = UserV2(email="alice@renphil.org", has_signed_in=True, created_at=_now())
        self.db.add(self.alice)
        self.db.flush()

    # -- helpers ------------------------------------------------------

    def tree(self) -> visibility.NodeTree:
        return visibility.build_node_tree(self.db)

    def ref(self, row) -> tuple[str, int]:
        """``(kind, id)`` for a fixture row, so tests read as node names."""
        if isinstance(row, HubV2):
            return ("hub", row.id)
        if isinstance(row, NavTabV2):
            return ("nav_tab", row.id)
        if isinstance(row, TabV2):
            return ("tab", row.id)
        return ("component", row.id)

    def seeds(self, *specs) -> list[GrantMatch]:
        """``(row, level)`` pairs as a seed set, with synthetic grant ids.

        The folds consume ``GrantMatch`` and never look at the database, so
        the seed set can be built directly. That is the point of ``fold``
        being split out from ``compute_visibility``: the algebra is testable
        without authoring grants, assignments, roles and scopes for every
        case, and 64 grant sets per property test would otherwise be 64 sets
        of fixture writes.
        """
        out = []
        for index, (row, level) in enumerate(specs):
            kind, node_id = self.ref(row)
            out.append(
                GrantMatch(grant_id=index + 1, node_kind=kind, node_id=node_id, level=level)
            )
        return out

    def fold(self, *specs) -> visibility.VisibilityResult:
        return visibility.fold(self.tree(), self.seeds(*specs))

    def all_nodes(self, tree: visibility.NodeTree) -> list[tuple[str, int]]:
        return sorted(tree.parents)

    def _candidate_seed_rows(self) -> list:
        """Six nodes spanning every kind and every nesting form, used as the
        alphabet the property tests enumerate grant sets over."""
        return [
            self.nav1,
            self.tabA,
            self.variantA,
            self.widget1,
            self.sbn2,
            self.widget2,
        ]

    def _all_seed_sets(self):
        """Every subset of the six candidates, at both levels — 64 grant sets
        per level, and each property is asserted for every node under each.

        This is what makes these PROPERTY tests: the claims below are
        quantified over the grant set and over the node, not demonstrated on
        one of each.
        """
        rows = self._candidate_seed_rows()
        for level in (LEVEL_VIEW, LEVEL_EDIT):
            for size in range(len(rows) + 1):
                for combo in itertools.combinations(rows, size):
                    yield [(row, level) for row in combo]


# =====================================================================
# §3.1-3.2 — the parent chain
# =====================================================================


class TestParentResolution(VisibilityTestCase):
    def test_nav_tab_hangs_off_the_hub(self):
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.nav1)), self.ref(self.hub))
        self.assertIsNone(tree.parent_of(self.ref(self.hub)))

    def test_root_tab_hangs_off_its_nav_tab(self):
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.tabA)), self.ref(self.nav1))

    def test_a_variant_hangs_off_its_parent_TAB_not_its_nav_tab(self):
        """§3.1's second tab axis, and the one that is easy to get wrong.

        ``variantA`` carries BOTH ``parent_tab_id`` and ``nav_tab_id`` — a
        variant is given its parent's nav tab by ``create_tab_variant_v2``.
        Reading ``nav_tab_id`` first would hang it off the nav tab and a
        grant on ``tabA`` would silently stop covering its own variants.
        """
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.variantA)), self.ref(self.tabA))
        self.assertNotEqual(tree.parent_of(self.ref(self.variantA)), self.ref(self.nav1))

    def test_a_widget_resolves_THROUGH_its_gridstack_to_its_tab(self):
        """§3.2 — gridstacks are transparent and hold no grants."""
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.widget1)), self.ref(self.tabA))

    def test_no_gridstack_is_ever_a_node(self):
        tree = self.tree()
        kinds = {kind for kind, _ in tree.parents}
        self.assertEqual(kinds, set(NODE_COLUMNS))
        self.assertNotIn("gridstack", kinds)

    def test_a_sub_grid_widget_hangs_off_the_representation_component(self):
        """The sub-grid's own node is its representation component, which is
        where migrate_subtab_access_control_to_components.py put its access
        control."""
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.widget2)), self.ref(self.subgrid_rep))

    def test_a_representation_component_does_not_become_its_own_parent(self):
        """``current_grid_id`` is the single-table device — every gridstack
        gets one component row standing in for it, so a canvas and its
        widgets come back from one query. ``_create_gridstack_component``
        consequently writes ``current_grid_id == gridstack_id`` on that row,
        which is what makes resolving it through its own ``gridstack_id``
        circular: it asks "who owns grid G?", gets "G's own representation
        component", and returns the row itself. It steps up a level instead.
        """
        tree = self.tree()
        rep = self.ref(self.subgrid_rep)
        self.assertNotEqual(tree.parent_of(rep), rep)
        # Onto the canvas the represented grid is nested inside — here tabA's
        # root canvas, which is transparent, so tabA itself.
        self.assertEqual(tree.parent_of(rep), self.ref(self.tabA))

    def test_the_representation_of_a_ROOT_gridstack_resolves_to_its_tab(self):
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.gA.representation)), self.ref(self.tabA))

    def test_parent_id_is_the_sub_grid_test_not_anything_on_the_component(self):
        """A non-null ``gridstacks.parent_id`` is what makes a grid a sub-grid
        inside a super-gridstack component. Flipping only that column moves
        the sub-grid's contents, with nothing on the component side touched.
        """
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.widget2)), self.ref(self.subgrid_rep))

        self.g_sub.parent_id = None  # now a root canvas of tabA, not a sub-grid
        self.db.commit()
        self.assertEqual(self.tree().parent_of(self.ref(self.widget2)), self.ref(self.tabA))

    def test_a_super_gridstack_root_does_not_sit_between_a_tab_and_its_subgrids(self):
        """The live SGS shape: grids 85/86/94 hang off root grids whose
        representation rows (324, 338) are typed ``super_gridstack``.

        That row is not a widget on the tab's canvas — it IS the canvas,
        flagged as an SGS container — and ``ComponentV2``'s docstring is
        explicit that a ROOT grid's representation row is the one place a
        representation's ``access_control`` is not read, since a root tab
        keeps its own on the ``TabV2`` row. Routing sub-grids through it
        would put a node that by design holds no grants between a tab and its
        contents.
        """
        self.gA.representation.type = visibility.SUPER_GRIDSTACK_WIDGET_TYPE
        self.db.commit()
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.subgrid_rep)), self.ref(self.tabA))
        self.assertEqual(tree.children.get(self.ref(self.gA.representation), []), [])

    def test_a_NESTED_sub_grid_DOES_route_through_its_parents_representation(self):
        """The opposite case, and the reason the recursion exists: a sub-grid
        inside a sub-grid has a parent whose representation row really does
        hold the access control. Live max nesting depth is 1, so this path
        does not fire today — which is exactly why it wants a test."""
        deeper = self._gridstack(self.tabA, parent=self.g_sub)
        widget = self._component(deeper, "widget-deep")
        self.db.commit()
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(widget)), self.ref(deeper.representation))
        self.assertEqual(
            tree.parent_of(self.ref(deeper.representation)), self.ref(self.subgrid_rep)
        )

    def test_the_sbn_chain_resolves_link_by_link_to_its_widget(self):
        """Depth 4, matching the deepest live chain."""
        tree = self.tree()
        self.assertEqual(tree.parent_of(self.ref(self.sbn3)), self.ref(self.sbn2))
        self.assertEqual(tree.parent_of(self.ref(self.sbn2)), self.ref(self.sbn1))
        self.assertEqual(tree.parent_of(self.ref(self.sbn1)), self.ref(self.sbn_root))
        self.assertEqual(tree.parent_of(self.ref(self.sbn_root)), self.ref(self.tabA))

    def test_every_ancestor_chain_ends_at_the_hub(self):
        tree = self.tree()
        for ref in self.all_nodes(tree):
            if ref == self.ref(self.hub) or tree.is_orphan(ref):
                continue
            self.assertEqual(
                tree.ancestors(ref)[-1], self.ref(self.hub), f"{ref} does not reach the hub"
            )

    def test_the_deep_chain_is_the_full_expected_path(self):
        tree = self.tree()
        self.assertEqual(
            tree.ancestors(self.ref(self.widget2)),
            [
                self.ref(self.subgrid_rep),
                self.ref(self.tabA),
                self.ref(self.nav1),
                self.ref(self.hub),
            ],
        )


# =====================================================================
# §3.3 — orphans and cycles fail CLOSED
# =====================================================================


class TestFailsClosed(VisibilityTestCase):
    def test_a_tab_with_no_nav_tab_is_an_orphan(self):
        tree = self.tree()
        self.assertTrue(tree.is_orphan(self.ref(self.orphan_tab)))
        self.assertTrue(tree.is_orphan(self.ref(self.widget4)))

    def test_an_orphan_is_invisible_even_when_granted_directly(self):
        """§3.3's rule stated the strong way: a grant is not a way to smuggle
        an unreachable node back into the tree."""
        result = self.fold((self.orphan_tab, LEVEL_EDIT), (self.widget4, LEVEL_EDIT))
        self.assertEqual(result.verdict(*self.ref(self.orphan_tab)), visibility.INVISIBLE)
        self.assertEqual(result.verdict(*self.ref(self.widget4)), visibility.INVISIBLE)
        self.assertFalse(result.is_granted(*self.ref(self.widget4)))

    def test_a_grant_on_an_orphan_reveals_nothing_anywhere(self):
        result = self.fold((self.widget4, LEVEL_VIEW))
        self.assertEqual(result.visible, set())

    def test_an_sbn_cycle_terminates_and_fails_closed(self):
        """§3.3 — unbounded by design, so a cycle must terminate and the node
        must fail closed rather than hang a request or blow the stack."""
        self.sbn1.super_blocknote_id = self.sbn3.id  # sbn1 -> sbn3 -> sbn2 -> sbn1
        self.db.commit()
        tree = self.tree()
        for row in (self.sbn1, self.sbn2, self.sbn3):
            self.assertTrue(tree.is_orphan(self.ref(row)))
            self.assertEqual(tree.ancestors(self.ref(row)), [])
        result = self.fold((self.sbn2, LEVEL_EDIT))
        self.assertEqual(result.verdict(*self.ref(self.sbn2)), visibility.INVISIBLE)
        # The rest of the tree is unaffected — a cycle is not contagious.
        self.assertFalse(tree.is_orphan(self.ref(self.widget1)))

    def test_a_gridstack_parent_cycle_terminates_and_fails_closed(self):
        self.gA.parent_id = self.g_sub.id  # gA -> g_sub -> gA
        self.db.commit()
        tree = self.tree()
        self.assertTrue(tree.is_orphan(self.ref(self.widget1)))
        self.assertTrue(tree.is_orphan(self.ref(self.widget2)))
        # tabA itself is still perfectly reachable — only its canvases broke.
        self.assertFalse(tree.is_orphan(self.ref(self.tabA)))

    def test_the_ancestor_walk_terminates_on_a_cycle_by_itself(self):
        """§3.3's "iterative with a visited set" pinned as its own claim.

        ``ancestors`` has TWO defences against a cycle and only the first one
        normally runs: ``is_orphan`` rejects cycle members before the walk
        starts, because a cycle is unreachable from the root by construction
        (``_walk_from_root``). That makes the visited set inside the walk
        genuinely unreachable through ``build_node_tree`` — and therefore
        untested, which was confirmed by deleting it and watching all 51
        tests stay green.

        So this test picks the first lock to exercise the second: it forces
        the cycle into ``rooted`` and asserts the walk still terminates and
        still visits each node once. Deleting the visited set makes this the
        only test that notices — by hanging rather than failing, which is the
        correct and loudest signal for a non-termination bug.
        """
        self.sbn1.super_blocknote_id = self.sbn3.id  # sbn1 -> sbn3 -> sbn2 -> sbn1
        self.db.commit()
        tree = self.tree()
        cycle = {self.ref(self.sbn1), self.ref(self.sbn2), self.ref(self.sbn3)}
        self.assertTrue(cycle.isdisjoint(tree.rooted), "the first lock should hold")

        tree.rooted |= cycle  # pick it, so the second lock is what answers
        chain = tree.ancestors(self.ref(self.sbn2))
        self.assertEqual(len(chain), len(set(chain)), "a node was visited twice")
        self.assertLess(len(chain), len(tree.parents) + 1)

    def test_no_hub_row_means_nothing_is_visible(self):
        self.db.query(HubV2).delete()
        self.db.commit()
        result = self.fold((self.tabA, LEVEL_EDIT), (self.widget1, LEVEL_EDIT))
        self.assertEqual(result.visible, set())
        self.assertEqual(result.verdict(*self.ref(self.tabA)), visibility.INVISIBLE)

    def test_two_hub_rows_mean_nothing_is_visible(self):
        """Not a state this code has an opinion about — ``HubV2`` says
        "exactly one row, ever" — but the fail-closed answer is the only safe
        one under it, since picking a row would strand every grant on the
        other."""
        self.db.add(HubV2(document_id="hub-2"))
        self.db.commit()
        result = self.fold((self.tabA, LEVEL_EDIT))
        self.assertEqual(result.visible, set())

    def test_an_unknown_node_is_invisible_not_an_error(self):
        result = self.fold((self.tabA, LEVEL_EDIT))
        self.assertEqual(result.verdict("component", 999_999), visibility.INVISIBLE)
        self.assertEqual(result.verdict("tab", 999_999), visibility.INVISIBLE)

    def test_a_sub_grid_with_no_representation_component_fails_closed(self):
        """``_get_gridstack_component``'s docstring records that these can
        exist where the current_grid_id backfill has not run. A sub-grid with
        nowhere to hang a grant is one nobody can be granted."""
        self.db.delete(self.subgrid_rep)
        self.db.commit()
        tree = self.tree()
        self.assertTrue(tree.is_orphan(self.ref(self.widget2)))
        self.assertFalse(tree.is_orphan(self.ref(self.widget1)))


# =====================================================================
# §5.4 — the six properties
# =====================================================================


class TestTheSixProperties(VisibilityTestCase):
    """Each assertion below is quantified over EVERY node and over 64 grant
    sets per level (``_all_seed_sets``)."""

    def test_property_1_granted_implies_visible(self):
        for specs in self._all_seed_sets():
            result = visibility.fold(self.tree(), self.seeds(*specs))
            for ref in result.granted_view:
                self.assertIn(ref, result.visible, f"granted but not visible: {ref} / {specs}")

    def test_property_2_visibility_is_upward_closed(self):
        """``visible(n) ⟹ visible(parent(n))``.

        This is the one the literal reading of §3.4 breaks, which is why the
        mirror rule is a conjunction — see ``_apply_mirror_substitution``.
        """
        for specs in self._all_seed_sets():
            tree = self.tree()
            result = visibility.fold(tree, self.seeds(*specs))
            for ref in result.visible:
                parent = tree.parent_of(ref)
                if parent is None:
                    self.assertEqual(ref, tree.root)
                    continue
                self.assertIn(
                    parent, result.visible, f"visible {ref} has invisible parent {parent}"
                )

    def test_property_3_granted_is_inherited_by_every_child(self):
        """``granted(n) ⟹ granted(c)`` — D3, and there is no break-glass.

        Mirrors are the documented exception and are excluded here rather
        than quietly weakening the property: the conjunction can un-grant a
        mirror under a granted parent, which is sound only because a mirror
        is a leaf and cannot pass the loss on. ``TestMirrors`` asserts the
        leaf-ness the exception rests on.
        """
        for specs in self._all_seed_sets():
            tree = self.tree()
            result = visibility.fold(tree, self.seeds(*specs))
            for ref in result.granted_view:
                for child in tree.children.get(ref, ()):
                    if child in tree.mirror_targets:
                        continue
                    self.assertIn(
                        child, result.granted_view, f"granted {ref} has ungranted child {child}"
                    )

    def test_property_4_adding_a_grant_never_removes_anything_from_visible(self):
        """Monotonicity in the grant set."""
        rows = self._candidate_seed_rows()
        for specs in self._all_seed_sets():
            base = visibility.fold(self.tree(), self.seeds(*specs)).visible
            already = {row for row, _ in specs}
            for extra in rows:
                if extra in already:
                    continue
                for level in (LEVEL_VIEW, LEVEL_EDIT):
                    wider = visibility.fold(
                        self.tree(), self.seeds(*specs, (extra, level))
                    ).visible
                    self.assertTrue(
                        base <= wider,
                        f"adding {self.ref(extra)}/{level} removed {base - wider}",
                    )

    def test_property_5_the_result_is_invariant_to_ordering(self):
        """Invariant to the order of the seed set AND to the order rows come
        out of the database."""
        for specs in self._all_seed_sets():
            forward = visibility.fold(self.tree(), self.seeds(*specs))
            backward = visibility.fold(self.tree(), list(reversed(self.seeds(*specs))))
            self.assertEqual(forward.visible, backward.visible)
            self.assertEqual(forward.granted_view, backward.granted_view)
            self.assertEqual(forward.granted_edit, backward.granted_edit)

    def test_property_5_is_invariant_to_child_ordering_too(self):
        tree = self.tree()
        shuffled = visibility.NodeTree(
            parents=dict(reversed(list(tree.parents.items()))),
            root=tree.root,
            mirror_targets=tree.mirror_targets,
        )
        for specs in self._all_seed_sets():
            a = visibility.fold(tree, self.seeds(*specs))
            b = visibility.fold(shuffled, self.seeds(*specs))
            self.assertEqual(a.visible, b.visible)
            self.assertEqual(a.granted_view, b.granted_view)

    def test_property_6_an_orphaned_parent_chain_yields_invisible(self):
        for specs in self._all_seed_sets():
            tree = self.tree()
            result = visibility.fold(tree, self.seeds(*specs))
            for ref in self.all_nodes(tree):
                if tree.is_orphan(ref):
                    self.assertEqual(
                        result.verdict(*ref), visibility.INVISIBLE, f"orphan {ref} is not invisible"
                    )

    def test_the_six_are_asserted_over_a_tree_that_is_not_trivially_small(self):
        """A guard on the guards: if the fixture ever shrank to a couple of
        nodes, every property above would still pass and say nothing."""
        tree = self.tree()
        self.assertGreaterEqual(len(tree.parents), 20)
        self.assertGreaterEqual(len(tree.ancestors(self.ref(self.sbn3))), 5)


# =====================================================================
# §5.3 — the plan's own worked scenario
# =====================================================================


class TestWorkedScenario(VisibilityTestCase):
    """§5.3 end to end: four seeds produce the whole picture.

    Mapped onto this fixture — ``nav1`` is the plan's ``nav1``, ``tabA`` its
    ``nav1.root1`` (granted outright), ``tabB`` its ``nav1.root2`` (revealed),
    and ``mirror1``/``widget-in-tabB`` stand in for its ``component1`` /
    ``component3`` pair.
    """

    def setUp(self) -> None:
        super().setUp()
        # A second widget on tabB, so the "granted sibling / hidden sibling"
        # half of §5.3 has something to be hidden.
        self.tabB_widget = self._component(self.gB, "widget-b1")
        self.db.commit()

    def test_the_whole_picture_from_the_plan(self):
        result = self.fold((self.tabA, LEVEL_VIEW), (self.tabB_widget, LEVEL_VIEW))

        # Granted outright, and everything beneath it inherits (descent stops).
        for row in (self.tabA, self.variantA, self.widget1, self.sbn3, self.widget2):
            verdict = result.verdict(*self.ref(row))
            self.assertTrue(verdict.view, f"{self.ref(row)} should be visible")
            self.assertFalse(verdict.revealed, f"{self.ref(row)} should be granted, not revealed")
            self.assertTrue(result.is_granted(*self.ref(row)))

        # Revealed: on the path to a seed, but not granted themselves.
        for row in (self.hub, self.nav1, self.tabB):
            verdict = result.verdict(*self.ref(row))
            self.assertTrue(verdict.view, f"{self.ref(row)} should be revealed")
            self.assertTrue(verdict.revealed, f"{self.ref(row)} should be a shell")
            self.assertFalse(result.is_granted(*self.ref(row)))

        # Granted: the deep seed that earned the reveal.
        self.assertTrue(result.is_granted(*self.ref(self.tabB_widget)))

        # Hidden: no seed on the root path or in the subtree. ``mirror1`` is
        # a sibling of the seed inside the revealed shell, and a revealed
        # ancestor does NOT hand out its other children.
        self.assertEqual(result.verdict(*self.ref(self.mirror1)), visibility.INVISIBLE)
        self.assertEqual(result.verdict(*self.ref(self.orphan_tab)), visibility.INVISIBLE)

    def test_a_reveal_does_not_leak_the_revealed_nodes_siblings(self):
        """§5.5's D3 read from the other side: granting a deep component
        reveals the PATH to it, never the path's other branches."""
        result = self.fold((self.widget2, LEVEL_VIEW))
        self.assertTrue(result.verdict(*self.ref(self.tabA)).revealed)
        self.assertEqual(result.verdict(*self.ref(self.widget1)), visibility.INVISIBLE)
        self.assertEqual(result.verdict(*self.ref(self.variantA)), visibility.INVISIBLE)
        self.assertEqual(result.verdict(*self.ref(self.sbn_root)), visibility.INVISIBLE)

    def test_a_grant_on_the_hub_reaches_every_rooted_node(self):
        """§6.5 — Hub Admin is an edit grant on the hub node, reaching every
        node by inheritance and carrying view with it by §6.1."""
        result = self.fold((self.hub, LEVEL_EDIT))
        tree = self.tree()
        for ref in self.all_nodes(tree):
            verdict = result.verdict(*ref)
            if tree.is_orphan(ref):
                self.assertEqual(verdict, visibility.INVISIBLE)
                continue
            self.assertTrue(verdict.view, f"{ref} not visible under a hub grant")
            self.assertTrue(verdict.edit, f"{ref} not editable under a hub edit grant")
            self.assertFalse(verdict.revealed)

    def test_no_seeds_means_nothing_at_all(self):
        """§10 items 1-2 and hazard 11: with ``role_assignments`` empty in
        production, ``effective(U) = ∅`` and every fold correctly returns
        nothing. That is the expected state, not a bug in the fold."""
        result = self.fold()
        self.assertEqual(result.visible, set())
        self.assertEqual(result.granted_view, set())
        for ref in self.all_nodes(self.tree()):
            self.assertEqual(result.verdict(*ref), visibility.INVISIBLE)


# =====================================================================
# §5.2 / §6.1 — the triple
# =====================================================================


class TestTheTriple(VisibilityTestCase):
    def test_edit_implies_view_for_every_node_and_every_grant_set(self):
        """§6.1's deliberate behaviour change. Today
        ``canEdit = canView && admins-match`` lets someone hold edit on a
        node they cannot see; dropping the conjunction is intended."""
        for specs in self._all_seed_sets():
            tree = self.tree()
            result = visibility.fold(tree, self.seeds(*specs))
            for ref in self.all_nodes(tree):
                verdict = result.verdict(*ref)
                if verdict.edit:
                    self.assertTrue(verdict.view, f"edit without view on {ref}")

    def test_an_edit_grant_alone_confers_view(self):
        result = self.fold((self.widget1, LEVEL_EDIT))
        verdict = result.verdict(*self.ref(self.widget1))
        self.assertTrue(verdict.edit)
        self.assertTrue(verdict.view)
        self.assertFalse(verdict.revealed)

    def test_a_view_grant_alone_does_not_confer_edit(self):
        verdict = self.fold((self.widget1, LEVEL_VIEW)).verdict(*self.ref(self.widget1))
        self.assertTrue(verdict.view)
        self.assertFalse(verdict.edit)

    def test_revealed_is_view_and_not_granted(self):
        result = self.fold((self.widget1, LEVEL_VIEW))
        tree = self.tree()
        for ref in self.all_nodes(tree):
            verdict = result.verdict(*ref)
            self.assertEqual(verdict.revealed, verdict.view and not result.is_granted(*ref))

    def test_a_revealed_ancestor_is_never_editable(self):
        """A shell is chrome. Nothing about revealing the path to a granted
        child hands out write access to the path."""
        result = self.fold((self.widget2, LEVEL_EDIT))
        for row in (self.hub, self.nav1, self.tabA):
            verdict = result.verdict(*self.ref(row))
            self.assertTrue(verdict.revealed)
            self.assertFalse(verdict.edit)
        self.assertTrue(result.verdict(*self.ref(self.widget2)).edit)

    def test_granted_and_visible_are_not_the_same_predicate(self):
        """§5.2 — collapsing them loses the reveal, which is the mechanism
        that lets someone be given one deep component without its ancestors.
        A test that would go green on a one-boolean implementation is no
        test, so this asserts the two sets actually differ."""
        result = self.fold((self.widget2, LEVEL_VIEW))
        self.assertTrue(result.granted_view < result.visible)
        self.assertEqual(
            result.visible - result.granted_view,
            {self.ref(self.hub), self.ref(self.nav1), self.ref(self.tabA), self.ref(self.subgrid_rep)},
        )


class TestEditWithin(VisibilityTestCase):
    """``edit_within(n)`` — edit on n or anything beneath it. Gates the
    frontend's Edit Mode toggle for non-admins (2026-10-07)."""

    def test_a_deep_component_grant_marks_its_whole_ancestor_path(self):
        result = self.fold((self.widget2, LEVEL_EDIT))
        for row in (self.hub, self.nav1, self.tabA, self.subgrid_rep, self.widget2):
            self.assertTrue(result.edit_within(*self.ref(row)), row)
        for row in (self.tabB, self.variantA, self.widget1, self.mirror1):
            self.assertFalse(result.edit_within(*self.ref(row)), row)

    def test_a_view_grant_never_counts(self):
        result = self.fold((self.widget2, LEVEL_VIEW), (self.nav1, LEVEL_VIEW))
        for ref in self.all_nodes(self.tree()):
            self.assertFalse(result.edit_within(*ref), ref)

    def test_a_mirror_whose_conjunction_failed_does_not_count(self):
        """An edit seed on the mirror itself, but only view on its target:
        the mirror is not editable, so nothing above it is "edit within"."""
        result = self.fold((self.mirror1, LEVEL_EDIT), (self.widget1, LEVEL_VIEW))
        self.assertFalse(result.verdict(*self.ref(self.mirror1)).edit)
        self.assertFalse(result.edit_within(*self.ref(self.tabB)))
        self.assertFalse(result.edit_within(*self.ref(self.nav1)))

    def test_matches_the_subtree_definition_for_every_grant_set(self):
        """Oracle: some node in ``{n} ∪ descendants(n)`` has ``edit``."""
        for specs in self._all_seed_sets():
            tree = self.tree()
            result = visibility.fold(tree, self.seeds(*specs))
            for ref in self.all_nodes(tree):
                expected = any(
                    node in result.granted_edit for node in {ref} | tree.descendants(ref)
                )
                self.assertEqual(result.edit_within(*ref), expected, (specs, ref))

    def test_viewer_access_bypass_and_none(self):
        admin = visibility.ViewerAccess(visibility=None, full_access=True)
        self.assertTrue(admin.edit_within(self.ref(self.nav1)))
        nobody = visibility.ViewerAccess(visibility=self.fold(), full_access=False)
        self.assertFalse(nobody.edit_within(self.ref(self.nav1)))
        self.assertFalse(nobody.edit_within(None))


# =====================================================================
# §3.4 — mirrors
# =====================================================================


class TestMirrors(VisibilityTestCase):
    """The conjunction: ``granted(mirror) = granted(position) AND
    granted(target)``. Owner decision, 2026-08-30 — see
    ``_apply_mirror_substitution`` for why both halves."""

    def test_a_mirror_in_a_granted_tab_does_NOT_bypass_the_targets_grants(self):
        """§3.4's whole reason for existing: dropping a mirror into a widely
        granted tab must not hand out the target."""
        result = self.fold((self.tabB, LEVEL_VIEW))
        self.assertTrue(result.verdict(*self.ref(self.tabB)).view)
        self.assertFalse(result.is_granted(*self.ref(self.mirror1)))

    def test_an_ungranted_mirror_is_a_SHELL_not_a_disappearance(self):
        """Today the server redacts it to ``{type:'restricted', data:null}``
        and KEEPS ITS KEY (tab_service.py:100). ``revealed`` is that same
        state, and this step must not change it."""
        verdict = self.fold((self.tabB, LEVEL_VIEW)).verdict(*self.ref(self.mirror1))
        self.assertTrue(verdict.view)
        self.assertTrue(verdict.revealed)

    def test_a_mirror_is_granted_when_both_halves_are(self):
        result = self.fold((self.tabB, LEVEL_VIEW), (self.widget1, LEVEL_VIEW))
        self.assertTrue(result.is_granted(*self.ref(self.mirror1)))
        self.assertFalse(result.verdict(*self.ref(self.mirror1)).revealed)

    def test_granting_only_the_target_does_not_reveal_the_mirrors_tab(self):
        """The half that pure replacement would get wrong, and the reason
        property 2 survives: the mirror's own position still governs whether
        it is reachable at all."""
        result = self.fold((self.widget1, LEVEL_VIEW))
        self.assertFalse(result.verdict(*self.ref(self.tabB)).view)
        self.assertFalse(result.is_granted(*self.ref(self.mirror1)))

    def test_the_conjunction_applies_per_level(self):
        result = self.fold((self.tabB, LEVEL_EDIT), (self.widget1, LEVEL_VIEW))
        # Position gives edit, but the target is only viewable, so the mirror
        # is viewable and not editable.
        verdict = result.verdict(*self.ref(self.mirror1))
        self.assertTrue(verdict.view)
        self.assertFalse(verdict.edit)

    def test_a_dangling_mirror_is_never_granted(self):
        self.mirror1.props = {"target_link": "does-not-exist"}
        self.db.commit()
        result = self.fold((self.tabB, LEVEL_EDIT))
        self.assertFalse(result.is_granted(*self.ref(self.mirror1)))
        self.assertTrue(result.verdict(*self.ref(self.mirror1)).revealed)

    def test_a_mirror_of_a_mirror_is_treated_as_dangling(self):
        """Matching ``_format_component_entries`` (:671), and it is also what
        keeps the substitution order-independent."""
        second = self._component(
            self.gB,
            "mirror-2",
            ctype=visibility.MIRROR_WIDGET_TYPE,
            props={"target_link": "mirror-1"},
        )
        self.db.commit()
        tree = self.tree()
        self.assertIsNone(tree.mirror_targets[self.ref(second)])
        result = visibility.fold(
            tree, self.seeds((self.tabB, LEVEL_VIEW), (self.widget1, LEVEL_VIEW))
        )
        self.assertTrue(result.is_granted(*self.ref(self.mirror1)))
        self.assertFalse(result.is_granted(*self.ref(second)))

    def test_a_mirror_is_a_leaf_which_is_what_makes_the_exception_sound(self):
        """The substitution runs AFTER the descending fold, so it would break
        §5.4 property 3 if a mirror could have children. It cannot — and if
        that ever changes, the adjustment has to move into the fold."""
        tree = self.tree()
        for mirror_ref in tree.mirror_targets:
            self.assertEqual(tree.children.get(mirror_ref, []), [])


# =====================================================================
# Wiring: the seed set, and the constants this module copies
# =====================================================================


class TestDrivenFromRealGrants(VisibilityTestCase):
    """``compute_visibility`` end to end, through ``matching_grants`` and a
    real assignment — the §8.1 pipeline rather than a hand-built seed set."""

    def _grant(self, row, role, scope, level):
        kind, node_id = self.ref(row)
        grant = ResourceGrantV2(
            **{NODE_COLUMNS[kind]: node_id},
            role_id=role.id,
            scope_id=scope.id,
            level=level,
            created_at=_now(),
        )
        self.db.add(grant)
        self.db.flush()
        return grant

    def test_an_assigned_user_reaches_a_grant_below_their_pair(self):
        self.db.add(
            RoleAssignmentV2(
                user_id=self.alice.id,
                role_id=self.program_lead.id,
                scope_id=self.all_scopes.id,
                created_at=_now(),
            )
        )
        self._grant(self.tabA, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()

        result = visibility.compute_visibility(self.db, self.alice.id)
        self.assertTrue(result.is_granted(*self.ref(self.tabA)))
        self.assertTrue(result.is_granted(*self.ref(self.widget2)))
        self.assertTrue(result.verdict(*self.ref(self.nav1)).revealed)
        self.assertEqual(result.verdict(*self.ref(self.tabB)), visibility.INVISIBLE)

    def test_a_user_with_no_assignments_sees_nothing(self):
        """Production's live state (§10 item 1), and the expected one."""
        self._grant(self.tabA, self.program_member, self.program_a, LEVEL_EDIT)
        self.db.commit()
        result = visibility.compute_visibility(self.db, self.alice.id)
        self.assertEqual(result.visible, set())

    def test_a_shared_closures_snapshot_gives_the_same_answer(self):
        """§8.2 — build one ``RbacClosures`` per request and pass it down."""
        self.db.add(
            RoleAssignmentV2(
                user_id=self.alice.id,
                role_id=self.program_lead.id,
                scope_id=self.all_scopes.id,
                created_at=_now(),
            )
        )
        self._grant(self.widget1, self.program_lead, self.program_a, LEVEL_EDIT)
        self.db.commit()

        from app.services.rbac_graph_service import RbacClosures

        shared = RbacClosures(self.db)
        tree = self.tree()
        a = visibility.compute_visibility(self.db, self.alice.id)
        b = visibility.compute_visibility(self.db, self.alice.id, closures=shared, tree=tree)
        self.assertEqual(a.visible, b.visible)
        self.assertEqual(a.granted_edit, b.granted_edit)

    def test_the_folds_themselves_never_touch_a_session(self):
        """``fold`` takes a tree and a seed set and nothing else. That is the
        structural version of §8.1's "do not walk the tree evaluating ACLs" —
        it cannot run a query per node because it has nothing to run one
        with."""
        import inspect

        self.assertNotIn("db", inspect.signature(visibility.fold).parameters)


class TestInheritedGrants(VisibilityTestCase):
    """Handoff §0.1 — ``list_inherited_grants``, the ancestor half of §9's
    "who can access this node" panel that ``list_grants_for_node``'s own
    docstring says is "not built yet"."""

    def _grant(self, row, role, scope, level=LEVEL_VIEW):
        kind, node_id = self.ref(row)
        row_ = ResourceGrantV2(
            **{NODE_COLUMNS[kind]: node_id}, role_id=role.id, scope_id=scope.id,
            level=level, created_at=_now(),
        )
        self.db.add(row_)
        self.db.flush()
        return row_

    def test_no_grants_anywhere_is_an_empty_list(self):
        self.db.commit()
        self.assertEqual(visibility.list_inherited_grants(self.db, "component", self.widget2.id), [])

    def test_ancestor_grants_are_grouped_and_ordered_nearest_first(self):
        """widget2's chain is subgrid_rep -> tabA -> nav1 -> hub. A grant on
        tabA and one on hub must come back as two separate entries, tabA
        before hub."""
        self._grant(self.tabA, self.program_member, self.program_a)
        self._grant(self.hub, self.hub_admin, self.all_scopes)
        self.db.commit()

        entries = visibility.list_inherited_grants(self.db, "component", self.widget2.id)
        self.assertEqual([(e["node_kind"], e["node_id"]) for e in entries], [self.ref(self.tabA), self.ref(self.hub)])
        self.assertEqual(len(entries[0]["grants"]), 1)
        self.assertEqual(len(entries[1]["grants"]), 1)

    def test_a_grant_on_the_node_itself_is_not_inherited(self):
        """That is `list_grants_for_node`'s job — inherited means STRICTLY
        above, never the node's own row."""
        self._grant(self.widget2, self.program_member, self.program_a)
        self._grant(self.tabA, self.program_member, self.program_a)
        self.db.commit()

        entries = visibility.list_inherited_grants(self.db, "component", self.widget2.id)
        self.assertEqual([(e["node_kind"], e["node_id"]) for e in entries], [self.ref(self.tabA)])

    def test_ancestors_with_no_grants_are_omitted(self):
        """nav1 sits between tabA and hub but gets no grant of its own — it
        must not appear with an empty ``grants`` list."""
        self._grant(self.tabA, self.program_member, self.program_a)
        self.db.commit()

        entries = visibility.list_inherited_grants(self.db, "component", self.widget2.id)
        self.assertEqual([(e["node_kind"], e["node_id"]) for e in entries], [self.ref(self.tabA)])

    def test_the_hub_itself_has_no_ancestors(self):
        self._grant(self.hub, self.hub_admin, self.all_scopes)
        self.db.commit()
        self.assertEqual(visibility.list_inherited_grants(self.db, "hub", self.hub.id), [])

    def test_an_orphans_ancestors_are_empty_too(self):
        """§3.3 — ``orphan_tab``'s chain does not reach the hub, so it has no
        ancestors to report, matching ``NodeTree.ancestors``."""
        self._grant(self.hub, self.hub_admin, self.all_scopes)
        self.db.commit()
        self.assertEqual(visibility.list_inherited_grants(self.db, "tab", self.orphan_tab.id), [])

    def test_a_reused_tree_gives_the_same_answer(self):
        self._grant(self.tabA, self.program_member, self.program_a)
        self.db.commit()
        tree = self.tree()
        self.assertEqual(
            visibility.list_inherited_grants(self.db, "component", self.widget2.id, tree=tree),
            visibility.list_inherited_grants(self.db, "component", self.widget2.id),
        )


class TestListVisibleNodes(VisibilityTestCase):
    """Handoff §0.3 — ``list_visible_nodes``, §9's "what can this person
    access" panel: every node in one user's ``visible`` set, with the full
    §5.2 triple."""

    def test_no_seeds_is_an_empty_list(self):
        result = self.fold()
        self.assertEqual(visibility.list_visible_nodes(result), [])

    def test_only_visible_nodes_are_returned_with_the_right_triple(self):
        result = self.fold((self.tabA, LEVEL_EDIT))
        rows = {(r["node_kind"], r["node_id"]): r for r in visibility.list_visible_nodes(result)}

        tab_row = rows[self.ref(self.tabA)]
        self.assertEqual((tab_row["view"], tab_row["edit"], tab_row["revealed"]), (True, True, False))

        # A granted descendant, reached by the descending fold.
        widget_row = rows[self.ref(self.widget1)]
        self.assertEqual(
            (widget_row["view"], widget_row["edit"], widget_row["revealed"]), (True, True, False)
        )

        # nav1 is only reached by the ascending reveal — visible, not granted.
        nav_row = rows[self.ref(self.nav1)]
        self.assertEqual((nav_row["view"], nav_row["edit"], nav_row["revealed"]), (True, False, True))

        # tabB and its descendants share no ancestor or subtree with the seed.
        self.assertNotIn(self.ref(self.tabB), rows)

    def test_the_list_is_sorted_by_kind_then_id(self):
        result = self.fold((self.tabA, LEVEL_VIEW))
        rows = visibility.list_visible_nodes(result)
        keys = [(r["node_kind"], r["node_id"]) for r in rows]
        self.assertEqual(keys, sorted(keys))

    def test_driven_from_real_grants_end_to_end(self):
        self.db.add(
            RoleAssignmentV2(
                user_id=self.alice.id,
                role_id=self.program_lead.id,
                scope_id=self.all_scopes.id,
                created_at=_now(),
            )
        )
        self.db.add(
            ResourceGrantV2(
                tab_id=self.tabA.id,
                role_id=self.program_member.id,
                scope_id=self.program_a.id,
                level=LEVEL_EDIT,
                created_at=_now(),
            )
        )
        self.db.commit()

        result = visibility.compute_visibility(self.db, self.alice.id)
        rows = {(r["node_kind"], r["node_id"]) for r in visibility.list_visible_nodes(result)}
        self.assertIn(self.ref(self.tabA), rows)
        self.assertIn(self.ref(self.nav1), rows)
        self.assertNotIn(self.ref(self.tabB), rows)

    def test_a_user_with_nothing_sees_nothing(self):
        result = visibility.compute_visibility(self.db, self.alice.id)
        self.assertEqual(visibility.list_visible_nodes(result), [])


class TestGrantedSingleNode(VisibilityTestCase):
    """``granted_single_node`` — plan_ac_enforcement_closeout_2026-09-09.md
    §5.3/§5.4, Option B: the O(depth) single-node fast path added so
    ``app/routers/airtable.py``'s six component-data routes can ask "is THIS
    one node granted" without building the whole tree per request (measured
    against live Neon before writing it — see that function's own docstring
    and ``scripts/profile_viewer_access.py``).

    THE CENTRAL CLAIM: for every node in the fixture tree, under a range of
    real ``ResourceGrantV2`` configurations, ``granted_single_node`` must
    agree with ``compute_visibility(...).is_granted(...)`` — the
    already-proven whole-tree reference this function exists to avoid
    calling. ``TestDrivenFromRealGrants``'s ``_grant`` shape is reused so the
    seed set actually flows through ``matching_grants`` (a real DB round
    trip), not a synthetic ``GrantMatch`` list — that round trip is exactly
    what a single-node caller like airtable.py depends on being correct.
    """

    def _grant(self, row, role, scope, level):
        kind, node_id = self.ref(row)
        grant = ResourceGrantV2(
            **{NODE_COLUMNS[kind]: node_id},
            role_id=role.id,
            scope_id=scope.id,
            level=level,
            created_at=_now(),
        )
        self.db.add(grant)
        self.db.flush()
        return grant

    def _assign(self, role, scope):
        self.db.add(
            RoleAssignmentV2(
                user_id=self.alice.id, role_id=role.id, scope_id=scope.id, created_at=_now()
            )
        )

    def _assert_agrees_with_compute_visibility(self):
        reference = visibility.compute_visibility(self.db, self.alice.id)
        for node in self.all_nodes(self.tree()):
            expected = reference.is_granted(*node)
            actual = visibility.granted_single_node(self.db, self.alice.id, node, is_admin=False)
            self.assertEqual(
                actual, expected, f"disagreement on {node}: fast path={actual}, fold={expected}"
            )

    def test_no_grants_no_assignment_agrees(self):
        self._assert_agrees_with_compute_visibility()

    def test_single_grant_on_a_root_tab_agrees(self):
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.tabA, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()

    def test_grant_on_a_deeply_nested_sbn_node_agrees(self):
        """sbn3 sits 3 ``super_blocknote_id`` hops below the grant — the
        unbounded-nesting case §3.3 requires the walk stay iterative for."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.sbn_root, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        self.assertTrue(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.sbn3), is_admin=False
            )
        )

    def test_grant_on_a_subgrid_widget_agrees(self):
        """widget2 resolves through its sub-grid's REPRESENTATION component,
        not its own gridstack row (§3.1/§3.2) — the branch
        ``resolve_component_parent_node`` shares with ``_component_parent``."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.widget2, self.program_member, self.program_a, LEVEL_EDIT)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()

    def test_a_variant_is_reached_through_its_parent_tab_not_its_nav_tab(self):
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.tabA, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        self.assertTrue(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.widget3), is_admin=False
            )
        )

    def test_grant_on_the_mirrors_target_only_does_not_grant_the_mirror(self):
        """§3.4's conjunction — granting only the target must not grant the
        mirror, because its own position (tabB) is still ungranted."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.widget1, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        self.assertFalse(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.mirror1), is_admin=False
            )
        )

    def test_grant_on_both_the_mirrors_position_and_target_grants_it(self):
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.tabB, self.program_member, self.program_a, LEVEL_VIEW)
        self._grant(self.widget1, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        self.assertTrue(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.mirror1), is_admin=False
            )
        )

    def test_a_dangling_mirror_is_never_granted(self):
        self.mirror1.props = {"target_link": "does-not-exist"}
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.tabB, self.program_member, self.program_a, LEVEL_EDIT)
        self.db.commit()
        self.assertFalse(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.mirror1), is_admin=False
            )
        )

    def test_the_orphan_tab_is_never_granted_even_by_a_hub_wide_grant(self):
        """§3.3 fail-closed: a grant on the hub itself covers almost the
        whole tree, but ``orphan_tab`` (``nav_tab_id IS NULL``) never reaches
        it through its own parent chain, and must stay ungranted."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.hub, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        self.assertFalse(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.widget4), is_admin=False
            )
        )
        self.assertTrue(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.widget1), is_admin=False
            )
        )

    def test_a_grant_written_ON_the_orphan_still_does_not_grant_it(self):
        """The case the test above does not cover, and the one that was
        WRONG until 2026-09-19 (thread-moderation phase 2m): a seed sitting
        on the orphan itself, or on the orphan tab above it. The old walk
        returned True the moment it met a seed, before discovering the chain
        never reaches the hub; ``compute_visibility`` has always said
        INVISIBLE. Found by ``tests/test_thread_mentions_scoping.py``'s
        three-way property test — the fixture there has both orphan shapes
        in its grant alphabet, so a random bag landed a grant on one."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.orphan_tab, self.program_member, self.program_a, LEVEL_EDIT)
        self._grant(self.widget4, self.program_member, self.program_a, LEVEL_EDIT)
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        for row in (self.orphan_tab, self.widget4):
            self.assertFalse(
                visibility.granted_single_node(
                    self.db, self.alice.id, self.ref(row), is_admin=False
                ),
                row,
            )

    def test_a_second_hub_row_fails_the_fast_path_closed_like_the_tree(self):
        """``build_node_tree`` has no root with two hub rows and everything
        is INVISIBLE; the fast path must not pick one and carry on."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.tabA, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.add(HubV2(document_id="hub-2"))
        self.db.commit()
        self._assert_agrees_with_compute_visibility()
        self.assertFalse(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.widget1), is_admin=False
            )
        )

    def test_admin_bypasses_with_no_grant_at_all(self):
        self.assertTrue(
            visibility.granted_single_node(
                self.db, self.alice.id, self.ref(self.widget1), is_admin=True
            )
        )

    def test_none_node_is_never_granted_even_for_an_admin_free_caller(self):
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.hub, self.program_member, self.program_a, LEVEL_VIEW)
        self.db.commit()
        self.assertFalse(
            visibility.granted_single_node(self.db, self.alice.id, None, is_admin=False)
        )

    def test_a_shared_closures_snapshot_gives_the_same_answer(self):
        """§8.2 — passing a pre-built ``RbacClosures`` must not change the
        answer, matching ``TestDrivenFromRealGrants``'s own test for
        ``compute_visibility``."""
        self._assign(self.program_lead, self.all_scopes)
        self._grant(self.widget1, self.program_lead, self.program_a, LEVEL_EDIT)
        self.db.commit()

        from app.services.rbac_graph_service import RbacClosures

        shared = RbacClosures(self.db)
        a = visibility.granted_single_node(
            self.db, self.alice.id, self.ref(self.widget1), is_admin=False
        )
        b = visibility.granted_single_node(
            self.db, self.alice.id, self.ref(self.widget1), is_admin=False, closures=shared
        )
        self.assertEqual(a, b)
        self.assertTrue(a)


class TestCopiedConstantsHaveNotDrifted(VisibilityTestCase):
    """``access_visibility_service`` deliberately does not import
    ``gridstack_service`` — an authorization primitive must not depend on the
    whole content stack — so the three widget-type literals are copied. This
    is what stops the copies drifting."""

    def test_the_widget_type_constants_match_gridstack_service(self):
        self.assertEqual(visibility.MIRROR_WIDGET_TYPE, gridstack_service.MIRROR_WIDGET_TYPE)
        self.assertEqual(visibility.GRIDSTACK_WIDGET_TYPE, gridstack_service.GRIDSTACK_WIDGET_TYPE)
        self.assertEqual(
            visibility.SUPER_GRIDSTACK_WIDGET_TYPE,
            gridstack_service.SUPER_GRIDSTACK_WIDGET_TYPE,
        )


class TestRequireForceAllowed(unittest.TestCase):
    """Owner decision 2026-09-25: lock/unlock `force` is Hub Admin only.
    Pure — the helper reads only `access.full_access`. Every lock door
    (tab, component, nav tab, SBN) calls this same function."""

    NODE = ("tab", 1)

    def test_admin_may_force(self) -> None:
        access = visibility.ViewerAccess(visibility=None, full_access=True)
        visibility.require_force_allowed(access, self.NODE, True)

    def test_non_admin_may_not_force(self) -> None:
        access = visibility.ViewerAccess(visibility=None, full_access=False)
        with self.assertRaises(visibility.ForceLockNotAllowedError):
            visibility.require_force_allowed(access, self.NODE, True)

    def test_non_admin_without_force_is_untouched(self) -> None:
        access = visibility.ViewerAccess(visibility=None, full_access=False)
        visibility.require_force_allowed(access, self.NODE, False)

    def test_no_access_means_no_check_requested(self) -> None:
        visibility.require_force_allowed(None, self.NODE, True)

    def test_it_is_an_access_denied_error_so_every_router_maps_it(self) -> None:
        self.assertTrue(issubclass(visibility.ForceLockNotAllowedError, visibility.AccessDeniedError))


if __name__ == "__main__":
    unittest.main()
