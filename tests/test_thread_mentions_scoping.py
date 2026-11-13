"""Mention scoping — plan_thread_moderation_2026-09-18.md §5.9 / §9.1a
(phase 2m, M9 + M10).

Two halves, two harnesses, one file:

1. **The reverse fold** (``access_visibility_service.users_granted_on_node``
   and the ``rbac_graph_service`` primitives under it) — service-level,
   built on ``test_access_visibility.py``'s harness shape (its own SQLite
   schema, its own hand-built tree and org). ``ReverseFoldAgreesWithForward``
   is THE test that makes §5.9 safe: for every user and every node, over
   many seeded random grant bags, the reverse answer must equal the forward
   fold's ``is_granted`` — the exact gate every thread read already runs for
   the caller. It is written first and deliberately quantified over random
   configurations, because the two known divergence points (orphans, the
   zero-assignment user) are exactly the kind of thing an example test
   forgets.

2. **The routes** (the nested directory, the four validation call sites) —
   router-level, reusing ``tests.test_threads._ThreadFixture`` so the
   TestClient, JWT minting, grant helpers and thread seeders are the same
   ones every other thread test uses.

Run with ``.venv/Scripts/python.exe -m unittest tests.test_thread_mentions_scoping``
(reference_renphil_test_invocation — no pytest, and ``unittest discover``
does not work here).
"""

from __future__ import annotations

import os
import random
import unittest
from datetime import date, datetime, timedelta, timezone

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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-thread-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.notification import NotificationV2  # noqa: E402
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
from app.db_v2.models.thread import THREAD_WIDGET_TYPE, ThreadV2  # noqa: E402
from app.dependencies import CurrentHubUser, is_hub_admin  # noqa: E402
from app.models.auth import UserInfo  # noqa: E402
from app.services import access_visibility_service as visibility  # noqa: E402
from app.services import thread_service  # noqa: E402
from app.services.gridstack_service import create_tab_v2  # noqa: E402
from app.services.nav_tab_service import create_nav_tab_v2, get_nav_tab_by_document_id  # noqa: E402
from app.services.rbac_graph_service import (  # noqa: E402
    RbacClosures,
    audience_count,
    audience_user_ids,
    hub_admin_user_ids,
)
from tests.test_threads import _ThreadFixture  # noqa: E402

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

NodeRef = tuple[str, int]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =====================================================================
# Half 1 — the reverse fold, service-level
# =====================================================================


class ReverseFoldTestCase(unittest.TestCase):
    """A tree built to exercise every nesting form AND both orphan shapes,
    with thread widgets placed so that each shape has one::

        hub
        ├── nav1
        │   ├── tabA                     root tab
        │   │   ├── threadA              thread widget on tabA's root canvas
        │   │   ├── sbn_root └── sbn1 └── sbn2 └── thread_in_sbn   (SBN chain, thread on the leaf)
        │   │   ├── subgrid_rep          sub-grid representation component
        │   │   │   └── thread_sub       thread widget on that sub-grid
        │   │   └── variantA             tab variant of tabA
        │   │       └── thread_var
        │   └── tabB
        │       ├── threadB
        │       └── mirrorB              mirrors threadA (§3.4 conjunction)
        │       └── mirror_dangling      target_link points nowhere
        └── nav2
            └── tabC
                └── threadC
        orphan_tab                       nav_tab_id IS NULL, parent_tab_id IS NULL — §3.3 shape 1
        └── thread_orphan
        (tabA) g_norep                   a SUB-GRID WITH NO REPRESENTATION ROW — §3.3 shape 2
        └── thread_norep                 its chain ends at the component itself

    Org: six roles (two chains under hub_admin, plus ⊥), four scopes
    (universal, A ⊃ B, plus ⊥), twelve ``hub_users`` covering: an
    assignment-resolved Hub Admin, holders at every level/scope, a user
    holding ``(hub_admin, program_a)`` — the admin ROLE at a non-universal
    scope, which must NOT count as admin — and one user with ZERO
    assignment rows.
    """

    ITERATIONS = 50

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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

    def _gridstack(self, tab: TabV2, parent: GridstackV2 | None = None, *, representation: bool = True):
        grid = GridstackV2(
            document_id=f"grid-{tab.id}-{parent.id if parent else 'root'}-{representation}",
            parent_id=parent.id if parent else None,
            parent_tab_id=tab.id,
        )
        self.db.add(grid)
        self.db.flush()
        if representation:
            rep = ComponentV2(
                link=f"rep-{grid.id}",
                type=visibility.GRIDSTACK_WIDGET_TYPE,
                gridstack_id=grid.id,
                current_grid_id=grid.id,
            )
            self.db.add(rep)
            self.db.flush()
            grid.representation = rep
        return grid

    def _component(self, grid: GridstackV2, link: str, ctype: str = "markdown", **kw) -> ComponentV2:
        row = ComponentV2(link=link, type=ctype, gridstack_id=grid.id, **kw)
        self.db.add(row)
        self.db.flush()
        return row

    def _build_tree(self) -> None:
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(slug="dashboard", title="Dashboard", protected=False)
        self.nav2 = NavTabV2(slug="programs", title="Programs", protected=False)
        self.db.add_all([self.nav1, self.nav2])
        self.db.flush()

        self.tabA = TabV2(document_id="tab-a", title="Root A", nav_tab_id=self.nav1.id)
        self.tabB = TabV2(document_id="tab-b", title="Root B", nav_tab_id=self.nav1.id)
        self.tabC = TabV2(document_id="tab-c", title="Root C", nav_tab_id=self.nav2.id)
        self.db.add_all([self.tabA, self.tabB, self.tabC])
        self.db.flush()
        self.variantA = TabV2(
            document_id="tab-a-v1", title="Variant 1", nav_tab_id=self.nav1.id, parent_tab_id=self.tabA.id
        )
        self.orphan_tab = TabV2(document_id="tab-orphan", title="Testing 1", nav_tab_id=None)
        self.db.add_all([self.variantA, self.orphan_tab])
        self.db.flush()

        self.gA = self._gridstack(self.tabA)
        self.gB = self._gridstack(self.tabB)
        self.gC = self._gridstack(self.tabC)
        self.gV = self._gridstack(self.variantA)
        self.gO = self._gridstack(self.orphan_tab)
        self.g_sub = self._gridstack(self.tabA, parent=self.gA)
        # §3.3 shape 2: a sub-grid whose representation row was never
        # backfilled. `_owner_of_gridstack` returns None for it, so anything
        # on it is an orphan whose chain ends at the component itself.
        self.g_norep = self._gridstack(self.tabA, parent=self.gA, representation=False)

        self.threadA = self._component(self.gA, "thread-a", ctype=THREAD_WIDGET_TYPE)
        self.threadB = self._component(self.gB, "thread-b", ctype=THREAD_WIDGET_TYPE)
        self.threadC = self._component(self.gC, "thread-c", ctype=THREAD_WIDGET_TYPE)
        self.thread_var = self._component(self.gV, "thread-var", ctype=THREAD_WIDGET_TYPE)
        self.thread_sub = self._component(self.g_sub, "thread-sub", ctype=THREAD_WIDGET_TYPE)
        self.thread_orphan = self._component(self.gO, "thread-orphan", ctype=THREAD_WIDGET_TYPE)
        self.thread_norep = self._component(self.g_norep, "thread-norep", ctype=THREAD_WIDGET_TYPE)

        self.sbn_root = self._component(self.gA, "sbn-root", ctype="super_block_note")
        self.sbn1 = self._component(self.gA, "sbn-1", ctype="block_note", super_blocknote_id=self.sbn_root.id)
        self.sbn2 = self._component(self.gA, "sbn-2", ctype="block_note", super_blocknote_id=self.sbn1.id)
        self.thread_in_sbn = self._component(
            self.gA, "thread-in-sbn", ctype=THREAD_WIDGET_TYPE, super_blocknote_id=self.sbn2.id
        )

        self.mirrorB = self._component(
            self.gB, "mirror-b", ctype=visibility.MIRROR_WIDGET_TYPE, props={"target_link": "thread-a"}
        )
        self.mirror_dangling = self._component(
            self.gB, "mirror-dangling", ctype=visibility.MIRROR_WIDGET_TYPE, props={"target_link": "nope"}
        )
        self.subgrid_rep = self.g_sub.representation

        self.thread_components = [
            self.threadA, self.threadB, self.threadC, self.thread_var, self.thread_sub,
            self.thread_in_sbn, self.thread_orphan, self.thread_norep,
        ]

    def _role(self, key: str, name: str, **kw) -> RoleV2:
        row = RoleV2(key=key, name=name, created_at=_now(), **kw)
        self.db.add(row)
        self.db.flush()
        return row

    def _scope(self, key: str, name: str, **kw) -> ScopeV2:
        row = ScopeV2(key=key, name=name, created_at=_now(), **kw)
        self.db.add(row)
        self.db.flush()
        return row

    def _user(self, email: str) -> UserV2:
        row = UserV2(email=email, has_signed_in=True, created_at=_now())
        self.db.add(row)
        self.db.flush()
        return row

    def _assign(self, user: UserV2, role: RoleV2, scope: ScopeV2) -> None:
        self.db.add(RoleAssignmentV2(user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=_now()))
        self.db.flush()

    def _build_org(self) -> None:
        self.hub_admin = self._role("hub_admin", "Hub Admin")
        self.program_lead = self._role("program_lead", "Program Lead")
        self.program_member = self._role("program_member", "Program Member")
        self.ops_lead = self._role("ops_lead", "Ops Lead")
        self.staff = self._role("staff", "Staff")
        self.any_role = self._role("any_role", "Any Role", is_public=True)
        for parent, child in (
            (self.hub_admin, self.program_lead),
            (self.program_lead, self.program_member),
            (self.hub_admin, self.ops_lead),
            (self.ops_lead, self.staff),
        ):
            self.db.add(RoleEdgeV2(parent_role_id=parent.id, child_role_id=child.id, created_at=_now()))
        self.all_scopes = self._scope("all_scopes", "All Scopes", is_universal=True)
        self.program_a = self._scope("program_a", "Program A")
        self.program_b = self._scope("program_b", "Program B")
        self.any_scope = self._scope("any_scope", "Any Scope", is_public=True)
        self.db.add(
            ScopeEdgeV2(parent_scope_id=self.program_a.id, child_scope_id=self.program_b.id, created_at=_now())
        )
        self.db.flush()
        self.roles = [self.hub_admin, self.program_lead, self.program_member, self.ops_lead, self.staff, self.any_role]
        self.scopes = [self.all_scopes, self.program_a, self.program_b, self.any_scope]

        self.users = [self._user(f"u{i:02d}@renphil.org") for i in range(12)]
        (u0, u1, u2, u3, u4, u5, u6, u7, u8, u9, u10, _u11) = self.users
        self._assign(u0, self.hub_admin, self.all_scopes)  # Hub Admin BY ASSIGNMENT
        self._assign(u1, self.program_lead, self.program_a)
        self._assign(u2, self.program_member, self.program_b)
        self._assign(u3, self.staff, self.all_scopes)
        self._assign(u4, self.ops_lead, self.program_a)
        self._assign(u4, self.program_member, self.program_b)
        self._assign(u5, self.program_member, self.all_scopes)
        self._assign(u6, self.staff, self.program_b)
        self._assign(u7, self.program_lead, self.program_b)
        self._assign(u8, self.program_member, self.program_a)
        self._assign(u9, self.ops_lead, self.all_scopes)
        self._assign(u10, self.hub_admin, self.program_a)  # admin ROLE, non-universal scope: NOT admin
        # u11: ZERO assignment rows — the "everyone with an assignment" edge.
        self.admin_by_assignment = u0
        self.admin_role_wrong_scope = u10
        self.unassigned = _u11

    # -- helpers ------------------------------------------------------

    def ref(self, row) -> NodeRef:
        if isinstance(row, HubV2):
            return ("hub", row.id)
        if isinstance(row, NavTabV2):
            return ("nav_tab", row.id)
        if isinstance(row, TabV2):
            return ("tab", row.id)
        return ("component", row.id)

    def grant(self, node: NodeRef, *, user=None, role=None, scope=None, level: str = LEVEL_VIEW) -> None:
        kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[kind]: node_id},
                user_id=user.id if user is not None else None,
                role_id=role.id if role is not None else None,
                scope_id=scope.id if scope is not None else None,
                level=level,
                created_at=_now(),
            )
        )
        self.db.flush()

    def current(self, user: UserV2) -> CurrentHubUser:
        """A `CurrentHubUser` with NO JWT roles — so `is_hub_admin` can only
        admit through branch 2 (assignments), which is the only branch the
        reverse fold can see (M10)."""
        return CurrentHubUser(
            info=UserInfo(email=user.email, name=user.email.split("@")[0], roles=[]),
            hub_user_id=user.id,
            email=user.email,
        )

    def reverse(self, node: NodeRef, closures: RbacClosures | None = None) -> set[int]:
        return visibility.users_granted_on_node(self.db, node, closures=closures)

    def forward(self, user: UserV2, node: NodeRef, *, closures, tree) -> bool:
        """What `resolve_viewer_access(...).is_granted(node)` would say for a
        caller with no JWT roles: the admin bypass from branch 2, else the
        whole-tree fold's payload verdict."""
        if is_hub_admin(self.db, self.current(user), closures=closures):
            return True
        return visibility.compute_visibility(
            self.db, user.id, closures=closures, tree=tree
        ).is_granted(*node)

    def all_nodes(self) -> list[NodeRef]:
        return sorted(visibility.build_node_tree(self.db).parents)

    def assert_reverse_matches_forward(self, *, label: str, fast_path_nodes: set[NodeRef] | None = None) -> None:
        """The property, asserted for EVERY user × EVERY node against the
        whole-tree fold, and — for `fast_path_nodes` — also against
        `granted_single_node`, the O(depth) primitive the directory route
        gates its caller with, so all three answers agree."""
        closures = RbacClosures(self.db)
        tree = visibility.build_node_tree(self.db)
        nodes = sorted(tree.parents)
        reverse_by_node = {node: self.reverse(node, closures) for node in nodes}
        for user in self.users:
            admin = is_hub_admin(self.db, self.current(user), closures=closures)
            vis = visibility.compute_visibility(self.db, user.id, closures=closures, tree=tree)
            for node in nodes:
                expected = admin or vis.is_granted(*node)
                actual = user.id in reverse_by_node[node]
                self.assertEqual(
                    actual,
                    expected,
                    f"[{label}] user={user.email} node={node}: reverse={actual} forward={expected}",
                )
                if fast_path_nodes and node in fast_path_nodes:
                    fast = visibility.granted_single_node(
                        self.db, user.id, node, is_admin=admin, closures=closures
                    )
                    self.assertEqual(
                        fast, expected, f"[{label}] user={user.email} node={node}: fast={fast} forward={expected}"
                    )


class ReverseFoldAgreesWithForward(ReverseFoldTestCase):
    """§9.1a — the property test. Random grant bags over the whole node
    alphabet (orphans included), both principal forms, both levels, every
    (role, scope) pair including ⊥ on either axis and the universal scope."""

    def _random_grant_bag(self, rng: random.Random, nodes: list[NodeRef]) -> list[str]:
        """Write 3–10 grants; return a description of each for failure
        messages. De-duplicates on the partial unique indexes' keys."""
        written: set[tuple] = set()
        described: list[str] = []
        for _ in range(rng.randint(3, 10)):
            node = rng.choice(nodes)
            level = rng.choice((LEVEL_VIEW, LEVEL_EDIT))
            if rng.random() < 0.35:
                user = rng.choice(self.users)
                key = (node, "user", user.id, level)
                if key in written:
                    continue
                written.add(key)
                self.grant(node, user=user, level=level)
                described.append(f"{node} user={user.email} {level}")
            else:
                role = rng.choice(self.roles)
                scope = rng.choice(self.scopes)
                key = (node, "pair", role.id, scope.id, level)
                if key in written:
                    continue
                written.add(key)
                self.grant(node, role=role, scope=scope, level=level)
                described.append(f"{node} ({role.key}, {scope.key}) {level}")
        return described

    def test_reverse_equals_forward_over_seeded_random_grant_bags(self):
        nodes = self.all_nodes()
        thread_nodes = {self.ref(c) for c in self.thread_components}
        # Sanity on the alphabet: both orphan shapes and the mirror ARE
        # nodes the RNG can land grants on, so the divergence points get
        # exercised rather than merely being possible.
        self.assertIn(self.ref(self.orphan_tab), nodes)
        self.assertIn(self.ref(self.thread_orphan), nodes)
        self.assertIn(self.ref(self.thread_norep), nodes)
        self.assertIn(self.ref(self.mirrorB), nodes)

        saw_orphan_grant = saw_bottom_pair = saw_unassigned_user_grant = False
        for iteration in range(self.ITERATIONS):
            rng = random.Random(1000 + iteration)
            bag = self._random_grant_bag(rng, nodes)
            joined = "; ".join(bag)
            saw_orphan_grant |= any(
                str(self.ref(o)) in d for d in bag for o in (self.orphan_tab, self.thread_orphan, self.thread_norep)
            )
            saw_bottom_pair |= any("(any_role, any_scope)" in d for d in bag)
            saw_unassigned_user_grant |= any(f"user={self.unassigned.email}" in d for d in bag)
            try:
                self.assert_reverse_matches_forward(
                    label=f"iteration {iteration}: {joined}", fast_path_nodes=thread_nodes
                )
            finally:
                self.db.rollback()
        # The RNG is seeded, so these are deterministic facts about the 50
        # bags above, not flaky hopes: if a future edit to the alphabet or
        # the seeds stops exercising a divergence point, this says so.
        self.assertTrue(saw_orphan_grant, "no iteration wrote a grant on an orphan node")
        self.assertTrue(saw_bottom_pair, "no iteration wrote a (⊥, ⊥) grant")
        self.assertTrue(saw_unassigned_user_grant, "no iteration wrote a direct grant to the unassigned user")

    def test_no_grants_at_all_only_the_assignment_admin_is_granted_anywhere(self):
        """Orphans included: the bypass sits ABOVE the fold, so an admin is
        granted on a node the fold never reaches — exactly what
        `ViewerAccess.is_granted` returns for them."""
        closures = RbacClosures(self.db)
        for node in self.all_nodes():
            self.assertEqual(self.reverse(node, closures), {self.admin_by_assignment.id}, node)


class OrphansReachNobody(ReverseFoldTestCase):
    """§3.3 / §5.9's one named divergence point, pinned deterministically as
    well as by the random bags: grants written ON an orphan (either shape)
    reach nobody in the reverse direction, because the forward fold never
    descends to it."""

    def _assert_orphan_dark(self, node: NodeRef) -> None:
        # Every principal form, both levels, directly on the orphan node.
        self.grant(node, user=self.unassigned, level=LEVEL_EDIT)
        self.grant(node, user=self.users[1], level=LEVEL_VIEW)
        self.grant(node, role=self.any_role, scope=self.any_scope, level=LEVEL_EDIT)
        self.grant(node, role=self.program_member, scope=self.all_scopes, level=LEVEL_VIEW)
        tree = visibility.build_node_tree(self.db)
        self.assertTrue(tree.is_orphan(node))
        # Reverse: the admin-by-assignment is the ONLY id that can appear,
        # and only because the forward side's bypass would admit them too.
        self.assertEqual(self.reverse(node), {self.admin_by_assignment.id})
        # Forward, for every non-admin: INVISIBLE, grants notwithstanding.
        closures = RbacClosures(self.db)
        for user in self.users:
            if user is self.admin_by_assignment:
                continue
            self.assertFalse(self.forward(user, node, closures=closures, tree=tree), user.email)

    def test_shape_1_tab_with_no_nav_tab(self):
        self._assert_orphan_dark(self.ref(self.orphan_tab))

    def test_shape_1_widget_on_the_orphan_tab(self):
        self._assert_orphan_dark(self.ref(self.thread_orphan))

    def test_shape_2_widget_on_a_sub_grid_with_no_representation_row(self):
        # The chain here is [component] alone: resolve_gridstack_node finds
        # no representation for g_norep and returns None one step up.
        self.assertEqual(visibility.walk_ancestors(self.db, self.ref(self.thread_norep)), [])
        self._assert_orphan_dark(self.ref(self.thread_norep))

    def test_an_ancestor_grant_does_not_leak_into_an_orphan_either(self):
        # A grant on tabA covers thread_sub (rooted through the sub-grid
        # rep) but NOT thread_norep, whose chain never reaches tabA at all.
        self.grant(self.ref(self.tabA), role=self.any_role, scope=self.any_scope)
        closures = RbacClosures(self.db)
        assigned = {u.id for u in self.users if u is not self.unassigned}
        self.assertEqual(self.reverse(self.ref(self.thread_sub), closures), assigned)
        self.assertEqual(self.reverse(self.ref(self.thread_norep), closures), {self.admin_by_assignment.id})


class BottomReachesAssignmentHoldersOnly(ReverseFoldTestCase):
    """Landmine 13: ⊥ is "everyone with an assignment", not "everyone"."""

    def test_bottom_grant_on_the_hub_reaches_every_assignment_holder_and_not_the_unassigned(self):
        self.grant(self.ref(self.hub), role=self.any_role, scope=self.any_scope)
        closures = RbacClosures(self.db)
        assigned = {u.id for u in self.users if u is not self.unassigned}
        for component in self.thread_components:
            node = self.ref(component)
            expected = assigned if not visibility.build_node_tree(self.db).is_orphan(node) else {self.admin_by_assignment.id}
            self.assertEqual(self.reverse(node, closures), expected, node)
        # And the forward fold agrees about the unassigned user specifically.
        tree = visibility.build_node_tree(self.db)
        self.assertFalse(self.forward(self.unassigned, self.ref(self.threadA), closures=closures, tree=tree))

    def test_a_direct_user_grant_is_the_only_way_an_unassigned_user_gets_in(self):
        self.grant(self.ref(self.tabA), user=self.unassigned)
        closures = RbacClosures(self.db)
        # Reaches tabA's subtree (including the variant and the sub-grid)...
        for component in (self.threadA, self.thread_var, self.thread_sub, self.thread_in_sbn):
            self.assertIn(self.unassigned.id, self.reverse(self.ref(component), closures), component.link)
        # ...and nothing outside it.
        for component in (self.threadB, self.threadC):
            self.assertNotIn(self.unassigned.id, self.reverse(self.ref(component), closures), component.link)

    def test_role_at_all_scopes_reaches_only_hub_wide_assignees(self):
        """§4.3 — on a stored grant the universal scope is the NARROWEST."""
        self.grant(self.ref(self.threadA), role=self.program_member, scope=self.all_scopes)
        got = self.reverse(self.ref(self.threadA))
        (u0, u1, u2, u3, u4, u5, u6, u7, u8, u9, u10, u11) = self.users
        # u5 holds (program_member, all_scopes); u0 is the assignment admin
        # (hub_admin, all_scopes) whose role descends to program_member.
        # u3/u9 hold all_scopes but via the staff/ops chain, which does not
        # reach program_member; u1/u8 hold program roles at program_a only.
        self.assertEqual(got, {u0.id, u5.id})


class HubAdminByAssignment(ReverseFoldTestCase):
    """M10 — the one seam, and the documented gap pinned so it cannot be
    forgotten: an assignment-resolved admin is granted everywhere rooted; a
    JWT-only admin is invisible to the reverse fold."""

    def test_assignment_admin_is_in_every_rooted_nodes_set_with_no_grant_anywhere(self):
        closures = RbacClosures(self.db)
        tree = visibility.build_node_tree(self.db)
        for node in self.all_nodes():
            if tree.is_orphan(node):
                continue
            self.assertIn(self.admin_by_assignment.id, self.reverse(node, closures), node)

    def test_hub_admin_role_at_a_non_universal_scope_is_not_an_admin(self):
        self.assertNotIn(self.admin_role_wrong_scope.id, hub_admin_user_ids(self.db))
        self.assertNotIn(self.admin_role_wrong_scope.id, self.reverse(self.ref(self.threadA)))
        # ...and `is_hub_admin` branch 2 agrees, which is what makes the
        # forward and reverse sides consistent about this user.
        self.assertFalse(is_hub_admin(self.db, self.current(self.admin_role_wrong_scope)))

    def test_jwt_only_admin_is_NOT_in_the_set_this_is_the_documented_gap(self):
        """§11.12. `is_hub_admin` admits this caller through branch 1 (JWT
        roles), so on the FORWARD side they can open every thread. The
        reverse fold cannot see a JWT, so they are absent. When
        `BOOTSTRAP_ADMIN_EMAILS` (phase 3) is unioned into
        `hub_admin_user_ids`, THIS assertion is the one that goes red — and
        that is the point: the gap closes by someone looking, not silently."""
        jwt_admin = self.unassigned  # a hub_users row, zero assignments
        jwt_current = CurrentHubUser(
            info=UserInfo(email=jwt_admin.email, name="jwt", roles=["Hub Admin"]),
            hub_user_id=jwt_admin.id,
            email=jwt_admin.email,
        )
        self.assertTrue(is_hub_admin(self.db, jwt_current))  # forward: admitted
        self.assertNotIn(jwt_admin.id, hub_admin_user_ids(self.db))  # reverse: absent
        self.assertNotIn(jwt_admin.id, self.reverse(self.ref(self.threadA)))

    def test_hub_admin_user_ids_is_empty_without_the_role_or_the_universal_scope(self):
        self.db.query(RoleAssignmentV2).delete()
        self.db.query(ScopeV2).filter(ScopeV2.id == self.all_scopes.id).delete()
        self.db.flush()
        self.assertEqual(hub_admin_user_ids(self.db), set())


class MirrorsAreAConjunction(ReverseFoldTestCase):
    """§3.4 defence in depth on the general primitive — thread widgets are
    never mirrors, but a caller that passes one must get the conjunction."""

    def test_position_and_target_both_required(self):
        # u1 reaches tabB (position) only; u2 reaches tabA (target) only;
        # u3 reaches both.
        (u0, u1, u2, u3, *_rest) = self.users
        self.grant(self.ref(self.tabB), user=u1)
        self.grant(self.ref(self.tabA), user=u2)
        self.grant(self.ref(self.tabA), user=u3)
        self.grant(self.ref(self.tabB), user=u3)
        got = self.reverse(self.ref(self.mirrorB))
        self.assertEqual(got, {u0.id, u3.id})
        # ...which is exactly the forward fold's answer.
        self.assert_reverse_matches_forward(label="mirror conjunction")

    def test_dangling_mirror_reaches_only_the_admin_bypass(self):
        self.grant(self.ref(self.tabB), role=self.any_role, scope=self.any_scope)
        self.assertEqual(self.reverse(self.ref(self.mirror_dangling)), {self.admin_by_assignment.id})
        self.assert_reverse_matches_forward(label="dangling mirror")


class WalkAncestorsShape(ReverseFoldTestCase):
    def test_rooted_chain_is_nearest_first_and_ends_at_the_hub(self):
        self.assertEqual(
            visibility.walk_ancestors(self.db, self.ref(self.thread_sub)),
            [self.ref(self.subgrid_rep), self.ref(self.tabA), self.ref(self.nav1), self.ref(self.hub)],
        )
        self.assertEqual(
            visibility.walk_ancestors(self.db, self.ref(self.thread_in_sbn)),
            [self.ref(self.sbn2), self.ref(self.sbn1), self.ref(self.sbn_root), self.ref(self.tabA),
             self.ref(self.nav1), self.ref(self.hub)],
        )
        self.assertEqual(
            visibility.walk_ancestors(self.db, self.ref(self.thread_var)),
            [self.ref(self.variantA), self.ref(self.tabA), self.ref(self.nav1), self.ref(self.hub)],
        )
        self.assertEqual(visibility.walk_ancestors(self.db, self.ref(self.hub)), [])

    def test_orphan_chain_is_the_partial_chain_not_empty(self):
        """Unlike `NodeTree.ancestors` — the location label wants whatever
        ancestors exist; `users_granted_on_node` is what checks rootedness."""
        self.assertEqual(
            visibility.walk_ancestors(self.db, self.ref(self.thread_orphan)), [self.ref(self.orphan_tab)]
        )

    def test_unknown_node_and_unknown_kind_walk_to_nothing(self):
        self.assertEqual(visibility.walk_ancestors(self.db, ("component", 999_999)), [])
        self.assertEqual(visibility.walk_ancestors(self.db, ("gridstack", 1)), [])
        # Unrooted → the grant bag contributes nothing; only the bypass does.
        self.assertEqual(self.reverse(("component", 999_999)), {self.admin_by_assignment.id})
        self.assertEqual(self.reverse(("gridstack", 1)), {self.admin_by_assignment.id})

    def test_a_second_hub_row_darkens_everything_but_the_bypass(self):
        """`build_node_tree` sets root=None with two hub rows; the reverse
        side must fail closed the same way rather than pick one."""
        self.db.add(HubV2(document_id="hub-2"))
        self.db.flush()
        self.grant(self.ref(self.tabA), role=self.any_role, scope=self.any_scope)
        self.assertEqual(self.reverse(self.ref(self.threadA)), {self.admin_by_assignment.id})
        self.assert_reverse_matches_forward(label="two hub rows")

    def test_a_cyclic_sbn_chain_terminates_and_fails_closed(self):
        self.sbn_root.super_blocknote_id = self.sbn2.id  # sbn_root -> sbn2 -> sbn1 -> sbn_root
        self.db.flush()
        chain = visibility.walk_ancestors(self.db, self.ref(self.thread_in_sbn))
        self.assertEqual(chain, [self.ref(self.sbn2), self.ref(self.sbn1), self.ref(self.sbn_root)])
        self.grant(self.ref(self.sbn1), role=self.any_role, scope=self.any_scope)
        self.assertEqual(self.reverse(self.ref(self.thread_in_sbn)), {self.admin_by_assignment.id})


class AudiencePrimitives(ReverseFoldTestCase):
    def test_audience_count_is_len_of_audience_user_ids(self):
        for role in self.roles:
            for scope in self.scopes:
                self.assertEqual(
                    audience_count(self.db, role.id, scope.id),
                    len(audience_user_ids(self.db, {(role.id, scope.id)})),
                    (role.key, scope.key),
                )

    def test_audience_of_a_pair_set_is_the_union_of_each_pairs_audience(self):
        pairs = {(self.program_member.id, self.program_b.id), (self.staff.id, self.all_scopes.id)}
        union = set()
        for pair in pairs:
            union |= audience_user_ids(self.db, {pair})
        self.assertEqual(audience_user_ids(self.db, pairs), union)
        self.assertTrue(union)  # non-vacuous

    def test_empty_pair_set_is_empty_without_a_query(self):
        self.assertEqual(audience_user_ids(self.db, set()), set())

    def test_audience_matches_effective_pairs_per_user(self):
        """The two directions of §4.2, pair by pair, user by user."""
        from app.services.rbac_graph_service import effective_pairs

        closures = RbacClosures(self.db)
        for role in self.roles:
            for scope in self.scopes:
                pair = (role.id, scope.id)
                reached = audience_user_ids(self.db, {pair}, closures=closures)
                for user in self.users:
                    self.assertEqual(
                        user.id in reached,
                        pair in effective_pairs(self.db, user.id, closures=closures),
                        (role.key, scope.key, user.email),
                    )


# =====================================================================
# Half 2 — the routes, on the thread fixture
# =====================================================================


class _ScopedMentionFixture(_ThreadFixture):
    """`_ThreadFixture` plus what the scoped directory needs: a SECOND nav
    tab / root tab / thread widget (so "granted on A, absent on B" is a real
    tree fact, not two widgets on one tab), a ⊥ role + ⊥ scope + pair-grant
    helper, and the hub_admin role + universal scope for M10."""

    def setUp(self) -> None:
        super().setUp()
        nav_b = create_nav_tab_v2(self.db, title="Nav B")
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])
        self.nav_b_id = nav_b_row.id
        root_b = create_tab_v2(self.db, title="Root B", nav_tab_id=nav_b_row.id)
        root_b_row = self.db.query(TabV2).filter(TabV2.document_id == root_b["documentId"]).first()
        self.root_b_tab_id = root_b_row.id
        self.root_b_gridstack = (
            self.db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == root_b_row.id, GridstackV2.parent_id.is_(None))
            .first()
        )
        self.widget_a = self._seed_thread_widget("scoped-widget-a")
        self.widget_b = self._seed_thread_widget_on(self.root_b_gridstack, "scoped-widget-b")

    def _seed_thread_widget_on(self, gridstack: GridstackV2, link: str) -> ComponentV2:
        component = ComponentV2(link=link, title=link, type=THREAD_WIDGET_TYPE, gridstack_id=gridstack.id)
        self.db.add(component)
        self.db.commit()
        self.db.refresh(component)
        return component

    def _seed_org(self) -> None:
        self.hub_admin_role = RoleV2(key="hub_admin", name="Hub Admin", created_at=_now())
        self.any_role = RoleV2(key="any_role", name="Any Role", is_public=True, created_at=_now())
        self.all_scopes = ScopeV2(key="all_scopes", name="All Scopes", is_universal=True, created_at=_now())
        self.any_scope = ScopeV2(key="any_scope", name="Any Scope", is_public=True, created_at=_now())
        self.db.add_all([self.hub_admin_role, self.any_role, self.all_scopes, self.any_scope])
        self.db.commit()

    def _pair_grant(self, *, node: tuple[str, int], role: RoleV2, scope: ScopeV2, level: str = LEVEL_VIEW) -> None:
        kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[kind]: node_id}, role_id=role.id, scope_id=scope.id, level=level, created_at=_now()
            )
        )
        self.db.commit()

    def _assign(self, email: str, role: RoleV2, scope: ScopeV2) -> UserV2:
        user = self._seed_hub_user(email)
        self.db.add(RoleAssignmentV2(user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=_now()))
        self.db.commit()
        return user

    def directory(self, link: str, headers: dict, q: str | None = None):
        qs = f"?q={q}" if q else ""
        return self.client.get(f"/data/threads/component/{link}/mentionable-users{qs}", headers=headers)

    def emails(self, resp, *, caller: str | None = None) -> set[str]:
        """`caller`: the Hub Admin making the request. Since phase 3
        `_member_auth` makes them a REAL admin, and an assignment admin is
        mentionable on every widget (M10, `HubAdminByAssignmentRoute`), so
        they are listed too; asserted present, then left out of the set the
        test compares. (Before, the JWT-only caller was never listed.)"""
        self.assertEqual(resp.status_code, 200, resp.text)
        got = {row["email"] for row in resp.json()}
        if caller is not None:
            self.assertIn(caller, got)
            got.discard(caller)
        return got


class DirectoryScopingTests(_ScopedMentionFixture):
    def test_non_thread_link_is_404(self):
        # A markdown widget's link — same "wrong type reads as not found"
        # rule every other `link` route applies.
        other = ComponentV2(link="not-a-thread", title="md", type="markdown", gridstack_id=self.root_gridstack.id)
        self.db.add(other)
        self.db.commit()
        resp = self.directory("not-a-thread", self._member_auth("alice@example.com"))
        self.assertEqual(resp.status_code, 404, resp.text)
        resp = self.directory("no-such-link", self._member_auth("alice@example.com"))
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_caller_not_granted_on_the_widget_is_403(self):
        self._seed_mentionable("bob@example.com", ("component", self.widget_a.id), name="Bob Smith")
        resp = self.directory("scoped-widget-a", self._auth("stranger@example.com"))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_granted_caller_sees_only_granted_roster_users(self):
        """Since the 2026-10 merge every signed-in person is a `users` row, so
        the granted caller (no HR data at all) is on the roster too — and is
        listed. Before, a login with no HR row could never be mentioned."""
        self._grant(email="caller@example.com", node=("component", self.widget_a.id))
        self._seed_mentionable("bob@example.com", ("component", self.widget_a.id), name="Bob Smith")
        self._seed_user("Carol Jones", work_email="carol@example.com")  # roster, no grant
        self._seed_mentionable("dave@example.com", ("component", self.widget_b.id), name="Dave Brown")  # other widget
        got = self.emails(self.directory("scoped-widget-a", self._auth("caller@example.com")))
        self.assertEqual(got, {"bob@example.com", "caller@example.com"})

    def test_hub_admin_caller_needs_no_grant_but_the_list_is_still_scoped(self):
        self._seed_mentionable("bob@example.com", ("component", self.widget_a.id), name="Bob Smith")
        self._seed_user("Carol Jones", work_email="carol@example.com")
        got = self.emails(self.directory("scoped-widget-a", self._member_auth("alice@example.com")), caller="alice@example.com")
        self.assertEqual(got, {"bob@example.com"})

    def test_same_roster_user_listed_on_A_via_the_tab_and_absent_on_B(self):
        """The ancestor case is the requirement: a grant on ROOT TAB A (not
        the widget) makes Bob mentionable on widget A and not on widget B,
        which hangs off a different nav tab."""
        self._seed_mentionable("bob@example.com", ("tab", self.root_tab_id), name="Bob Smith")
        admin = self._member_auth("alice@example.com")
        self.assertEqual(self.emails(self.directory("scoped-widget-a", admin), caller="alice@example.com"), {"bob@example.com"})
        self.assertEqual(self.emails(self.directory("scoped-widget-b", admin), caller="alice@example.com"), set())

    def test_a_nav_tab_grant_reaches_the_widget_beneath_it(self):
        nav_a_id = self.db.query(TabV2.nav_tab_id).filter(TabV2.id == self.root_tab_id).scalar()
        self._seed_mentionable("bob@example.com", ("nav_tab", nav_a_id), name="Bob Smith")
        admin = self._member_auth("alice@example.com")
        self.assertEqual(self.emails(self.directory("scoped-widget-a", admin), caller="alice@example.com"), {"bob@example.com"})
        self.assertEqual(self.emails(self.directory("scoped-widget-b", admin), caller="alice@example.com"), set())

    def test_edit_grant_counts_as_granted(self):
        self._seed_mentionable("bob@example.com", ("component", self.widget_a.id), name="Bob Smith", level=LEVEL_EDIT)
        got = self.emails(self.directory("scoped-widget-a", self._member_auth("alice@example.com")), caller="alice@example.com")
        self.assertEqual(got, {"bob@example.com"})

    def test_q_still_filters_within_the_scoped_set(self):
        node = ("component", self.widget_a.id)
        self._seed_mentionable("jose@example.com", node, name="José Núñez")
        self._seed_mentionable("roy@example.com", node, name="Roy Abdelnour")
        self._seed_user("Roy Other", work_email="royother@example.com")  # matches q, NOT granted
        admin = self._member_auth("alice@example.com")
        self.assertEqual(self.emails(self.directory("scoped-widget-a", admin, q="jose")), {"jose@example.com"})
        self.assertEqual(self.emails(self.directory("scoped-widget-a", admin, q="roy")), {"roy@example.com"})

    def test_cap_of_eight_applies_after_scoping(self):
        node = ("component", self.widget_a.id)
        for i in range(10):
            self._seed_mentionable(f"granted{i:02d}@example.com", node, name=f"Granted {i:02d}")
        for i in range(10):
            self._seed_user(f"Aaa Ungranted {i:02d}", work_email=f"ungranted{i:02d}@example.com")
        resp = self.directory("scoped-widget-a", self._member_auth("alice@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()
        self.assertEqual(len(rows), 8)
        # Had the cap applied BEFORE scoping, the alphabetically-first
        # "Aaa Ungranted" rows would have eaten the eight slots. The admin
        # caller is in the scoped set too since phase 3 (see `emails`).
        self.assertTrue(
            all(r["email"].startswith("granted") or r["email"] == "alice@example.com" for r in rows)
        )

    def test_roster_row_with_no_hub_users_row_is_absent_even_under_a_bottom_grant(self):
        """Landmine 13 at the route: ⊥ on the hub reaches assignment holders;
        a roster row that never signed in (pre-merge: no hub_users row) has no
        assignments, and is not mentionable."""
        self._seed_org()
        self._pair_grant(node=("hub", self.hub.id), role=self.any_role, scope=self.any_scope)
        self._seed_user("Never Signed In", work_email="never@example.com")
        # An assignment holder IS reached by ⊥ (any role at any scope).
        self._assign("holder@example.com", self.hub_admin_role, self.all_scopes)
        self._seed_user("Holder Person", work_email="holder@example.com")
        # A hub_users row with ZERO assignments is not.
        self._seed_hub_user("zero@example.com")
        self._seed_user("Zero Assignments", work_email="zero@example.com")
        got = self.emails(self.directory("scoped-widget-a", self._member_auth("alice@example.com")), caller="alice@example.com")
        self.assertEqual(got, {"holder@example.com"})

    def test_the_old_unscoped_path_is_gone(self):
        # The static segment now falls through to `GET /threads/{thread_id}`
        # and fails its `int` path validation — a 422, not a directory. What
        # matters is that no component-less directory is served any more.
        resp = self.client.get("/data/threads/mentionable-users", headers=self._member_auth("alice@example.com"))
        self.assertNotEqual(resp.status_code, 200)
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_directory_route_does_not_build_the_whole_tree(self):
        """Landmine 16 — `granted_single_node`, not `get_viewer_access`."""
        from unittest import mock

        self._grant(email="caller@example.com", node=("component", self.widget_a.id))
        with mock.patch.object(visibility, "build_node_tree", side_effect=AssertionError("tree built")):
            resp = self.directory("scoped-widget-a", self._auth("caller@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)


class ValidationScopingTests(_ScopedMentionFixture):
    """§5.9.2 — one helper, two callers: the validator applies the SAME
    scoping as the directory on all four write paths."""

    def _notifications_for(self, email: str) -> list[NotificationV2]:
        return self.db.query(NotificationV2).filter(NotificationV2.recipient_email == email).all()

    def test_thread_create_drops_an_ungranted_roster_mention_and_keeps_a_granted_one(self):
        node = ("component", self.widget_a.id)
        self._seed_mentionable("bob@example.com", node, name="Bob Smith")
        self._seed_user("Carol Jones", work_email="carol@example.com")  # roster, ungranted
        resp = self.client.post(
            "/data/threads/component/scoped-widget-a",
            json={
                "title": "T",
                "content": "hi @BobSmith and @CarolJones",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual([m["email"] for m in resp.json()["mentions"]], ["bob@example.com"])
        self.assertEqual(len(self._notifications_for("bob@example.com")), 1)
        self.assertEqual(len(self._notifications_for("carol@example.com")), 0)

    def test_scoping_is_per_widget_not_global(self):
        """Bob is granted on widget B only: mentionable there, dropped on A."""
        self._seed_mentionable("bob@example.com", ("component", self.widget_b.id), name="Bob Smith")
        payload = {"title": "T", "content": "hi @BobSmith", "mentions": [{"email": "bob@example.com"}]}
        on_b = self.client.post(
            "/data/threads/component/scoped-widget-b", json=payload, headers=self._member_auth("alice@example.com")
        )
        self.assertEqual([m["email"] for m in on_b.json()["mentions"]], ["bob@example.com"])
        on_a = self.client.post(
            "/data/threads/component/scoped-widget-a", json=payload, headers=self._member_auth("alice@example.com")
        )
        self.assertEqual(on_a.json()["mentions"], [])

    def test_thread_update_revalidates_under_scoping(self):
        node = ("component", self.widget_a.id)
        self._seed_mentionable("bob@example.com", node, name="Bob Smith")
        self._seed_user("Carol Jones", work_email="carol@example.com")
        create = self.client.post(
            "/data/threads/component/scoped-widget-a",
            json={"title": "T", "content": "hi @BobSmith", "mentions": [{"email": "bob@example.com"}]},
            headers=self._member_auth("alice@example.com"),
        )
        thread_id = create.json()["id"]
        edit = self.client.patch(
            f"/data/threads/{thread_id}",
            json={
                "content": "hi @BobSmith and @CarolJones",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual([m["email"] for m in edit.json()["thread"]["mentions"]], ["bob@example.com"])
        self.assertEqual(len(self._notifications_for("carol@example.com")), 0)

    def test_comment_create_and_update_apply_the_same_scoping(self):
        node = ("component", self.widget_a.id)
        self._seed_mentionable("bob@example.com", node, name="Bob Smith")
        self._seed_user("Carol Jones", work_email="carol@example.com")
        thread = self._insert_thread(self.widget_a.id, author_email="alice@example.com")
        create = self.client.post(
            f"/data/threads/{thread.id}/comments",
            json={
                "content": "cc @BobSmith @CarolJones",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("dave@example.com"),
        )
        self.assertEqual(create.status_code, 201, create.text)
        self.assertEqual([m["email"] for m in create.json()["mentions"]], ["bob@example.com"])
        edit = self.client.patch(
            f"/data/threads/comments/{create.json()['id']}",
            json={
                "content": "cc @BobSmith @CarolJones again",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("dave@example.com"),
        )
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual([m["email"] for m in edit.json()["mentions"]], ["bob@example.com"])
        self.assertEqual(len(self._notifications_for("carol@example.com")), 0)

    def test_the_cap_counts_survivors_after_scoping(self):
        """26 claimed, all valid roster tokens, only 3 granted → 201 with 3,
        not a 400 — the same "survivors, not raw claims" rule the cap has
        always applied to non-roster emails."""
        node = ("component", self.widget_a.id)
        claimed, tokens = [], []
        for i in range(26):
            email = f"person{i}@example.com"
            if i < 3:
                self._seed_mentionable(email, node, name=f"Person{i}")
            else:
                self._seed_user(f"Person{i}", work_email=email)
            claimed.append({"email": email})
            tokens.append(f"@Person{i}")
        resp = self.client.post(
            "/data/threads/component/scoped-widget-a",
            json={"title": "T", "content": " ".join(tokens), "mentions": claimed},
            headers=self._member_auth("alice@example.com"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(len(resp.json()["mentions"]), 3)

    def test_validator_and_directory_agree_on_the_same_helper(self):
        """Landmine 15, asserted the direct way: whatever the directory
        offers, the validator keeps; whatever it withholds, the validator
        drops — driven from the same seeded state."""
        node = ("component", self.widget_a.id)
        self._seed_mentionable("bob@example.com", node, name="Bob Smith")
        self._seed_user("Carol Jones", work_email="carol@example.com")
        offered = self.emails(self.directory("scoped-widget-a", self._member_auth("alice@example.com")), caller="alice@example.com")
        resp = self.client.post(
            "/data/threads/component/scoped-widget-a",
            json={
                "title": "T",
                "content": "hi @BobSmith and @CarolJones",
                "mentions": [{"email": "bob@example.com"}, {"email": "carol@example.com"}],
            },
            headers=self._member_auth("alice@example.com"),
        )
        kept = {m["email"] for m in resp.json()["mentions"]}
        self.assertEqual(kept, offered)
        self.assertEqual(kept, {"bob@example.com"})


class HubAdminByAssignmentRoute(_ScopedMentionFixture):
    """M10 at the route: an assignment-resolved admin is mentionable
    everywhere with no grant; a JWT-only admin is not (the documented gap)."""

    def test_assignment_admin_is_mentionable_with_no_grant_anywhere(self):
        self._seed_org()
        self._assign("boss@example.com", self.hub_admin_role, self.all_scopes)
        self._seed_user("Big Boss", work_email="boss@example.com")
        admin = self._member_auth("alice@example.com")
        self.assertEqual(self.emails(self.directory("scoped-widget-a", admin), caller="alice@example.com"), {"boss@example.com"})
        self.assertEqual(self.emails(self.directory("scoped-widget-b", admin), caller="alice@example.com"), {"boss@example.com"})

    def test_jwt_only_admin_is_not_mentionable_the_documented_gap(self):
        """§11.12 — `jwtadmin@` would carry "Hub Admin" in their own JWT and
        can open every thread, but nothing server-side can say so about
        them; with no assignment row and no grant they are absent."""
        self._seed_org()
        self._seed_hub_user("jwtadmin@example.com")  # signed in before, no assignment
        self._seed_user("Jwt Admin", work_email="jwtadmin@example.com")
        got = self.emails(self.directory("scoped-widget-a", self._member_auth("alice@example.com")), caller="alice@example.com")
        self.assertEqual(got, set())


if __name__ == "__main__":
    unittest.main()
