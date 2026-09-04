"""Read-time visibility — the node tree and the two folds
(plan_access_control_algorithm_2026-08-27.md §3, §5, §6.1, §8.1 steps 3-4).

Originally written as step 4, "computed, never enforced" — nothing wired
into an endpoint, no response shape any client read affected. That is no
longer true and this paragraph is corrected rather than left to rot:
``session_handoff_2026-09-04-tab-visibility-wiring.md`` wired ``ViewerAccess``
into every ``/v2/tabs`` and ``/v2/nav-tabs`` READ endpoint, and
project_ac_enforcement_gap.md item 2 (below, ``require_edit`` and
``resolve_gridstack_parent_node``) wires ``edit(n)``/``edit(parent(n))`` into
the WRITE endpoints — ``create_new_tab``, ``create_variant``,
``reorder_tabs``, ``reorder_variants``, ``update_content``,
``update_component_content_endpoint``, ``update_tab_metadata``, ``move_tab``,
``delete_tab``. ``require_hub_admin`` is still untouched — §6.8's nav-tab/hub
half is a separate, later step, blocked on this one.

``role_assignments`` and ``resource_grants`` are no longer empty in
production (one Hub Admin assignment, and grants authored through the
grant-editor UI as of 2026-09-03), but they are still sparse, and the Hub
Admin bypass (``ViewerAccess.full_access``) remains load-bearing for exactly
the reason §10 items 1-2 give: an ordinary user who has been granted nothing
correctly sees, and can now change, nothing.

WHAT THIS MODULE OWES ITS READER, in the order the surprises arrive:

1. **The parent chain is three different lookups, not one** (§3.1). See
   ``_component_parent``.
2. **Gridstacks are transparent** (§3.2) — they hold no grants and are a
   pass-through link. See ``_owner_of_gridstack``.
3. **``visible`` is NOT computed as the subtree fold the plan states**
   declaratively. The two forms are equivalent; see ``_reveal_ancestors``
   for the proof and the reason the equivalent one is the one that runs.
4. **Orphans and cycles fail CLOSED** (§3.3), and they do so structurally
   rather than by a check anyone has to remember. See ``_walk_from_root``.
5. **Mirrors are the one place a node's verdict is not its own** (§3.4).
   See ``_apply_mirror_substitution`` — including the collision with §5.4's
   property 2 that the owner settled on 2026-08-30.

WHAT IS DELIBERATELY NOT HERE. §8.4 names a materialized
"has-granted-descendant" summary as the thing this design exists to avoid —
it is write-time propagation wearing a different hat, and the propagation
engine was torn out in the 2026-08-27 session precisely so that nothing
derives visibility into storage. There is also no performance argument for
one: the whole computation is linear in the NODE COUNT, which is measured at
~240 live rows (1 hub, 5 nav tabs, 31 tabs, 203 components). If a future
session argues for a materialized summary, the number to challenge is the
node count, not the grant count.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.hub import HubV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.resource_grant import LEVEL_EDIT
from app.db_v2.models.tab import TabV2
from app.services.rbac_graph_service import RbacClosures
from app.services.resource_grant_service import (
    GrantMatch,
    get_grant,
    list_grants_for_node,
    matching_grants,
    seeds_for_principal,
)

# A node's address: the same ``(kind, id)`` pair ``resource_grant_service``
# already speaks in, so a ``GrantMatch`` drops straight into this tree with no
# translation layer between the seed set and the fold that consumes it.
NodeRef = tuple[str, int]

# Widget type constants, duplicated rather than imported from
# ``gridstack_service``. That module imports half the application (page
# content, Airtable, super blocknotes) and importing it here would make an
# authorization primitive depend on the entire content stack — a circular
# import waiting to happen the first time content code wants a visibility
# check. The three values below are stable string literals stored in
# ``components.type``; ``tests.test_access_visibility`` pins them against
# ``gridstack_service``'s own definitions so the copies cannot drift.
MIRROR_WIDGET_TYPE = "mirror"
GRIDSTACK_WIDGET_TYPE = "gridstack"
SUPER_GRIDSTACK_WIDGET_TYPE = "super_gridstack"


class NodeVerdict(NamedTuple):
    """§5.2's per-node triple.

    ``view``  — ``visible(n)``. Gates the CHROME: a row in the nav rail, a
                pill in the tab bar, a breadcrumb segment.
    ``edit``  — ``granted_edit(n)``. Gates writes, and by §6.1 carries view
                with it.
    ``revealed`` — ``view and not granted``. The node appears and opens, but
                its own payload is filtered by the same rule — usually to
                nothing but the child that earned the reveal.

    ``granted`` — which gates the PAYLOAD (canvas content, widget data,
    BlockNote text, Airtable rows) — is deliberately not a fourth field,
    because §5.2 specifies exactly these three. It is recovered as
    ``view and not revealed``, and ``VisibilityResult.is_granted`` is the
    named way to ask.

    DO NOT COLLAPSE ``view`` AND ``granted`` INTO ONE BOOLEAN. They are
    different permissions, and the gap between them is the entire reveal
    mechanism — the thing that lets someone be given one deep component
    without being given its ancestors. A caller that treats ``view`` as
    permission to serve the payload has silently deleted §5.2.

    Accepted leak, recorded in §5.2 as a decision rather than an oversight: a
    reveal exposes the ancestor's existence and title. Sharing one widget out
    of a nav tab called "Board Compensation" tells the recipient that tab
    exists and what it is called.
    """

    view: bool
    edit: bool
    revealed: bool


# The verdict every node that does not exist, or whose parent chain does not
# reach the hub, evaluates to. §3.3: an orphan is INVISIBLE, not
# "unreachable-therefore-open".
INVISIBLE = NodeVerdict(view=False, edit=False, revealed=False)


# ---------------------------------------------------------
# §3.1-3.2 — the node tree
# ---------------------------------------------------------


class NodeTree:
    """Every node's parent, resolved once, for the whole hub.

    Built from five bulk queries — one per table — rather than from a walk
    that queries per node. §8.4 requires the whole tree anyway ("a lazily
    loaded subtree cannot answer its own visibility": *is nav1 visible?*
    depends on nodes several fetches away), so there is nothing to be saved
    by loading it lazily and a per-node query would turn one round trip into
    hundreds.

    ``rooted`` is the set of nodes whose parent chain terminates at the hub.
    Everything else is an orphan and evaluates to ``INVISIBLE``.
    """

    def __init__(
        self,
        *,
        parents: dict[NodeRef, NodeRef | None],
        root: NodeRef | None,
        mirror_targets: dict[NodeRef, NodeRef | None],
    ) -> None:
        self.parents = parents
        self.root = root
        self.mirror_targets = mirror_targets

        self.children: dict[NodeRef, list[NodeRef]] = {}
        for ref, parent in parents.items():
            if parent is not None:
                self.children.setdefault(parent, []).append(ref)

        # Sorted so the traversal order is deterministic. §5.4 property 5
        # requires the RESULT to be invariant to ordering — this makes the
        # order stable as well, which is what lets a failure be reproduced
        # from a test name instead of from a lucky dict iteration.
        for child_list in self.children.values():
            child_list.sort()

        self.rooted: set[NodeRef] = _walk_from_root(self.root, self.children)

    def parent_of(self, ref: NodeRef) -> NodeRef | None:
        return self.parents.get(ref)

    def exists(self, ref: NodeRef) -> bool:
        return ref in self.parents

    def is_orphan(self, ref: NodeRef) -> bool:
        """True for a node that does not exist, or whose chain does not reach
        the hub — including every member of a parent cycle (§3.3)."""
        return ref not in self.rooted

    def ancestors(self, ref: NodeRef) -> list[NodeRef]:
        """``ref``'s STRICT ancestors, nearest first. Empty for the root, for
        an unknown node, and for an orphan.

        Iterative with a visited set, never recursive (§3.3). Live data has
        SBN chains four deep and gridstack nesting one deep, but both are
        unbounded by design, so a cycle must terminate rather than blow the
        stack. The visited set is what guarantees that; the orphan check
        above it means a cycle never gets here in the first place, and the
        two together are belt and braces on the one traversal that a
        malformed row could otherwise hang a request on.
        """
        if self.is_orphan(ref):
            return []
        chain: list[NodeRef] = []
        seen = {ref}
        current = self.parents.get(ref)
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            current = self.parents.get(current)
        return chain

    def descendants(self, ref: NodeRef) -> set[NodeRef]:
        """``ref``'s STRICT descendants. Empty for a leaf, for an unknown
        node, and for an orphan.

        Added for §6.2's revoke-time confirmation, which asks *"which of the
        revoked node's descendants would this principal still reach?"* — the
        one question in this design that is scoped to a subtree rather than
        to a root path. Nothing in the two folds needs it: ``_fold_down``
        descends the whole tree at once and ``_reveal_ancestors`` walks
        upward, which is exactly why the subtree walk did not exist until
        something outside the folds wanted it.

        Orphans return empty rather than their real subtree, matching
        ``ancestors``. A node that fails closed has no reachable descendants
        to report, and reporting them would let the confirmation modal name
        nodes that are invisible to everyone (§3.3).

        Iterative with a visited set, never recursive (§3.3), and the visited
        set is load-bearing here in a way it is not in ``ancestors``: this
        walk fans out, so a cycle among ``children`` would revisit nodes
        combinatorially rather than merely looping. ``rooted`` already
        excludes cycle members, so this is the second lock on that door —
        see ``_walk_from_root`` for the first.
        """
        if self.is_orphan(ref):
            return set()
        found: set[NodeRef] = set()
        frontier = [ref]
        while frontier:
            current = frontier.pop()
            for child in self.children.get(current, ()):
                if child not in found and child != ref:
                    found.add(child)
                    frontier.append(child)
        return found


def _walk_from_root(
    root: NodeRef | None, children: dict[NodeRef, list[NodeRef]]
) -> set[NodeRef]:
    """Every node reachable from the hub by following CHILD edges.

    THIS IS WHERE FAILING CLOSED HAPPENS, AND IT HAPPENS STRUCTURALLY. §3.3
    requires a node whose parent chain does not terminate at ``hub`` to
    evaluate invisible rather than unreachable-therefore-open, and the
    tempting way to get there is a per-node "did I reach the hub?" check that
    somebody later forgets to call on a new code path. Descending from the
    root instead makes the property fall out of the traversal: a node is
    rooted if and only if this walk reaches it, and nothing else can be.

    It disposes of cycles for free, which is the part worth spelling out. If
    ``parent(A) = B`` and ``parent(B) = A``, then ``A ∈ children[B]`` and
    ``B ∈ children[A]``, so A is reachable only through B and B only through
    A. Neither has the hub on its path, neither is reached from the root, and
    both are correctly orphans — with no cycle detection written anywhere.
    The root itself cannot be inside a cycle, having no parent at all.

    Iterative, with the visited set doubling as the result (§3.3).
    """
    if root is None:
        return set()
    reached = {root}
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child not in reached:
                reached.add(child)
                frontier.append(child)
    return reached


def build_node_tree(db: Session) -> NodeTree:
    """Resolve every node's parent in one pass (§3.1, §3.2, §8.4)."""
    # -- the root --------------------------------------------------
    #
    # EXACTLY ONE ROW, OR THERE IS NO ROOT AND EVERYTHING FAILS CLOSED.
    # ``HubV2``'s own docstring says "Exactly one row, ever", §3.3 says there
    # is exactly one hub row and it is the root, and live data has one. Zero
    # rows and two rows are both schema violations rather than states this
    # code has to have an opinion about, and the fail-closed answer is the
    # only one that is safe under either: with two rows there is no fact of
    # the matter about which one grants written on the OTHER row reach, and
    # picking the lower id would silently strand every grant on the higher
    # one. A dark hub is a bug someone reports in minutes; a stranded grant
    # set is a hole nobody sees.
    hub_ids = [row[0] for row in db.query(HubV2.id).all()]
    root: NodeRef | None = ("hub", hub_ids[0]) if len(hub_ids) == 1 else None

    parents: dict[NodeRef, NodeRef | None] = {}
    if root is not None:
        parents[root] = None

    # -- nav tabs --------------------------------------------------
    #
    # There is no ``nav_tabs.hub_id`` column — the link to the root is
    # implicit, and always has been. Every nav tab hangs off the single hub
    # row; if there isn't one, they are all orphans, which is the same
    # fail-closed answer arrived at from the other side.
    for (nav_tab_id,) in db.query(NavTabV2.id).all():
        parents[("nav_tab", nav_tab_id)] = root

    # -- tabs: two axes, and they are not the same axis ------------
    #
    # ``parent_tab_id`` is a VARIANT (§3.1) — a full sibling TabV2 with its
    # own document_id, canvas and access control, selected from a pill row,
    # nested strictly one level. ``nav_tab_id`` is PLACEMENT. A variant
    # carries the same ``nav_tab_id`` as its parent (create_tab_variant_v2),
    # so both columns are populated on a variant and the order of these two
    # branches is load-bearing: reading ``nav_tab_id`` first would hang every
    # variant directly off the nav tab, and a grant on the parent tab would
    # then stop covering its own variants — silently, since the variant
    # would still be reachable and still evaluate to something plausible.
    for tab_id, parent_tab_id, nav_tab_id in db.query(
        TabV2.id, TabV2.parent_tab_id, TabV2.nav_tab_id
    ).all():
        if parent_tab_id is not None:
            parents[("tab", tab_id)] = ("tab", parent_tab_id)
        elif nav_tab_id is not None:
            parents[("tab", tab_id)] = ("nav_tab", nav_tab_id)
        else:
            # §3.3's orphan. ``TabV2.nav_tab_id`` is nullable so the nav-tabs
            # migration could add the column before backfilling, and the
            # model comment records that a NULL is an anomaly the service
            # layer never produces — which makes failing closed a bug you
            # find rather than a hole you ship. TWO SUCH ROWS EXIST TODAY
            # (tabs id=71 "Testing 1" and id=73 "ingest test", each with one
            # gridstack and two components). The owner's decision on
            # 2026-08-30 was to leave them failing closed and settle their
            # disposition before the cutover; they are deliberately NOT
            # special-cased here, because an exception for two rows is how a
            # hole ships.
            parents[("tab", tab_id)] = None

    # -- gridstacks: NOT nodes, just the link between two nodes ----
    gridstacks = {
        row[0]: (row[1], row[2])
        for row in db.query(GridstackV2.id, GridstackV2.parent_id, GridstackV2.parent_tab_id).all()
    }

    components = db.query(
        ComponentV2.id,
        ComponentV2.gridstack_id,
        ComponentV2.current_grid_id,
        ComponentV2.super_blocknote_id,
        ComponentV2.type,
        ComponentV2.props,
        ComponentV2.link,
    ).all()

    # gridstack id -> the component that REPRESENTS it (``current_grid_id``).
    # Every gridstack gets exactly one, created alongside it by
    # ``_create_gridstack_component``; a sub-grid's representation component
    # is where its access control now lives, relocated off
    # ``gridstacks.settings`` by migrate_subtab_access_control_to_components.
    representation_of: dict[int, int] = {
        current_grid_id: component_id
        for component_id, _, current_grid_id, _, _, _, _ in components
        if current_grid_id is not None
    }

    for (
        component_id,
        gridstack_id,
        current_grid_id,
        super_blocknote_id,
        _type,
        _props,
        _link,
    ) in components:
        parents[("component", component_id)] = _component_parent(
            gridstack_id=gridstack_id,
            current_grid_id=current_grid_id,
            super_blocknote_id=super_blocknote_id,
            gridstacks=gridstacks,
            representation_of=representation_of,
        )

    # -- mirrors (§3.4) --------------------------------------------
    by_link: dict[str, int] = {
        link: component_id for component_id, _, _, _, _, _, link in components if link
    }
    types_by_id: dict[int, str] = {
        component_id: component_type for component_id, _, _, _, component_type, _, _ in components
    }
    mirror_targets: dict[NodeRef, NodeRef | None] = {}
    for component_id, _, _, _, component_type, props, _link in components:
        if component_type != MIRROR_WIDGET_TYPE:
            continue
        target_link = (props or {}).get("target_link")
        target_id = by_link.get(target_link) if target_link else None
        # A MIRROR OF A MIRROR COUNTS AS DANGLING, matching
        # ``_format_component_entries`` (:671), which refuses the same shape
        # with the same reasoning — the picker already excludes mirrors as
        # pickable targets, and this is defense in depth. It also removes an
        # ordering hazard from ``_apply_mirror_substitution``: a chain of
        # mirrors would make each one's verdict depend on whether the next
        # had been processed yet, which §5.4 property 5 forbids outright.
        if target_id is not None and types_by_id.get(target_id) == MIRROR_WIDGET_TYPE:
            target_id = None
        # A dangling target resolves to None and the mirror is treated as
        # ungranted, matching ``_format_component_entries``, which serves a
        # dangling mirror with ``mirroredData: None``. There is nothing to
        # show and nothing to check against.
        mirror_targets[("component", component_id)] = (
            ("component", target_id) if target_id is not None else None
        )

    return NodeTree(parents=parents, root=root, mirror_targets=mirror_targets)


def _component_parent(
    *,
    gridstack_id: int | None,
    current_grid_id: int | None,
    super_blocknote_id: int | None,
    gridstacks: dict[int, tuple[int | None, int | None]],
    representation_of: dict[int, int],
) -> NodeRef | None:
    """THE THREE ROLES ``components`` PLAYS, AND THEY NEST DIFFERENTLY (§3.1).

    One table, three kinds of row, and reading the wrong column for the wrong
    kind is the easiest thing in this step to get subtly wrong:

    1. an **SBN sub-tab** (``super_blocknote_id`` set) — points at its parent
       Super Block Note component, which may itself be one. Live data has
       chains four deep and the nesting is unbounded by design.
    2. a **sub-grid representation** (``current_grid_id`` set) — the row
       whose ``access_control`` IS the sub-tab's, relocated there by
       migrate_subtab_access_control_to_components.py.
    3. an ordinary **widget** — reached through its gridstack.

    The branches are ordered by how specific they are. In today's data the
    first two are mutually exclusive — ``_create_gridstack_component`` sets
    ``super_blocknote_id=None`` and ``create_sbn_node`` sets
    ``current_grid_id=None`` — so the order is a statement of precedence
    rather than a live tiebreak, and it is written down so a row that somehow
    carried both would resolve predictably instead of by column order.

    WHAT ``current_grid_id`` IS FOR, since branch 2 reads as a special case
    and is not one. It is the device that lets gridstacks and components be
    fetched from a SINGLE TABLE: every gridstack gets one component row
    standing in for it, so a canvas and its widgets come back from one query
    instead of two. ``_create_gridstack_component`` writes
    ``current_grid_id=gridstack.id`` AND ``gridstack_id=gridstack.id`` on that
    row — verified true of all 21 live representation rows — which is the
    natural consequence of a row that represents its own grid, not an
    anomaly.

    **The authoritative test for "is this a sub-grid" is
    ``gridstacks.parent_id``, not anything on the component.** A non-null
    ``parent_id`` means the grid is a sub-grid inside a super-gridstack
    component; that is the column ``_owner_of_gridstack`` branches on, and
    the only one that should ever be used for the question.

    What branch 2 is therefore doing is stepping up one level, not dodging a
    bug: a component representing grid G lives, in tree terms, on whatever
    canvas G is nested INSIDE, so it resolves through G's PARENT gridstack.
    The reason it cannot simply fall through to branch 3 is the single-table
    design above — with ``gridstack_id == current_grid_id``, branch 3 would
    ask "who owns grid G?", get back "G's own representation component", and
    hand the row itself as its own parent.
    """
    if super_blocknote_id is not None:
        return ("component", super_blocknote_id)

    if current_grid_id is not None:
        grid = gridstacks.get(current_grid_id)
        if grid is None:
            return None
        parent_grid_id, parent_tab_id = grid
        if parent_grid_id is None:
            # The representation component of a ROOT gridstack. Its
            # access_control is not read (a root canvas's lives on its TabV2
            # row — see ComponentV2's docstring), so nothing will ever grant
            # this row; giving it the tab as its parent makes it a harmless
            # mirror of the tab's own verdict rather than a spurious orphan.
            return ("tab", parent_tab_id) if parent_tab_id is not None else None
        return _owner_of_gridstack(parent_grid_id, gridstacks, representation_of)

    if gridstack_id is None:
        return None
    return _owner_of_gridstack(gridstack_id, gridstacks, representation_of)


def _owner_of_gridstack(
    gridstack_id: int,
    gridstacks: dict[int, tuple[int | None, int | None]],
    representation_of: dict[int, int],
) -> NodeRef | None:
    """The node that holds grants for content sitting on ``gridstack_id``.

    GRIDSTACKS ARE TRANSPARENT (§3.2). ``GridstackV2`` has no
    ``access_control`` column and is not one of the four node kinds — it is a
    pass-through link, and a component's owner resolves THROUGH it to the tab
    or parent component that does hold grants. Both folds skip it.

    ``parent_id`` IS THE TEST, and it is the only one. A non-null
    ``parent_id`` means this grid is a sub-grid inside a super-gridstack
    component; NULL means it is a tab's own root canvas. Nothing on the
    component side answers this question — see ``_component_parent`` on what
    ``current_grid_id`` is actually for — and the two halves of ``_is_root``
    (``gridstack_service:256``) are the same split.

    - ``parent_id IS NULL`` — a tab's root canvas, so its owner is that tab,
      read straight off ``parent_tab_id``, which ``gridstack_service``
      already relies on being denormalized to the owning root tab at every
      nesting depth (:260).
    - otherwise — a sub-grid, whose owner is its own representation
      component, which is where its access control was relocated to.

    WHY THE ROOT BRANCH DOES NOT ROUTE THROUGH THE ROOT'S OWN REPRESENTATION
    COMPONENT, since that row exists and is often typed ``super_gridstack``.
    A root grid's representation component is not a widget sitting on the
    tab's canvas — it IS the tab's canvas, flagged as an SGS container — and
    ``ComponentV2``'s docstring is explicit that its ``access_control`` is
    the one place a representation row's AC is NOT read, because a root tab
    or variant keeps its own on the ``TabV2`` row. Inserting it into the
    chain would put a node that by design holds no grants between a tab and
    its sub-grids, so a grant on the tab and a grant on that row would mean
    different things for no reason. Live shape, confirmed read-only: grids
    85, 86 and 94 hang off root grids whose representation rows (324, 338)
    are typed ``super_gridstack``, and they resolve to tabs 80 and 88. A
    NESTED sub-grid is the opposite case — its parent grid's representation
    row does hold the AC — and the recursive call below routes through it.

    A sub-grid with no representation component returns None and its contents
    become orphans. That is the correct answer rather than a gap to paper
    over: ``_get_gridstack_component``'s docstring records that such rows can
    exist if the current_grid_id backfill has not run for them, and a
    sub-grid with nowhere to hang a grant is a sub-grid nobody can be granted
    — §3.3's fail-closed rule, arrived at from a third direction. It does not
    fire on live data: 15 of the 36 gridstacks carry no representation row,
    but all 15 are ROOTS, which resolve through ``parent_tab_id`` and never
    consult ``representation_of`` at all.
    """
    grid = gridstacks.get(gridstack_id)
    if grid is None:
        return None
    parent_grid_id, parent_tab_id = grid
    if parent_grid_id is None:
        return ("tab", parent_tab_id) if parent_tab_id is not None else None
    representation_id = representation_of.get(gridstack_id)
    if representation_id is None:
        return None
    return ("component", representation_id)


# ---------------------------------------------------------
# §5.1, §8.1 steps 3-4 — the two folds
# ---------------------------------------------------------


class VisibilityResult:
    """Every node's verdict for one user, computed in one pass.

    Cache this per ``(user, content version)`` if a caller needs it twice in
    a request (§8.4). Never cache it ACROSS requests, for the same reason
    ``RbacClosures`` must not be: a grant or an edge written in between would
    not be seen, and this is an authorization input.
    """

    def __init__(
        self,
        *,
        tree: NodeTree,
        granted_view: set[NodeRef],
        granted_edit: set[NodeRef],
        visible: set[NodeRef],
    ) -> None:
        self.tree = tree
        self.granted_view = granted_view
        self.granted_edit = granted_edit
        self.visible = visible

    def verdict(self, node_kind: str, node_id: int) -> NodeVerdict:
        """§5.2's triple for one node. ``INVISIBLE`` for an unknown node and
        for an orphan — the same answer, deliberately, since "this node does
        not exist" and "this node is not reachable from the hub" must not be
        distinguishable from outside."""
        ref = (node_kind, node_id)
        view = ref in self.visible
        if not view:
            return INVISIBLE
        granted = ref in self.granted_view
        return NodeVerdict(
            view=True,
            # §6.1's EDIT IMPLIES VIEW, and it is a deliberate behaviour
            # change. Today ``canEdit = canView(...) && admins-match``
            # (permissions.ts:217-235, DashboardV2Page.canEditTab), which
            # lets someone be granted edit on a node they cannot see.
            # Dropping the conjunction is intended; do not reproduce it.
            # Here it needs no code at all — an edit seed is also a view
            # seed (see ``compute_visibility``), so ``granted_edit`` is a
            # subset of ``granted_view`` is a subset of ``visible`` by
            # construction, and the implication cannot come apart.
            edit=ref in self.granted_edit,
            revealed=not granted,
        )

    def is_granted(self, node_kind: str, node_id: int) -> bool:
        """Gates the PAYLOAD — canvas content, widget data, BlockNote text,
        Airtable rows. Not the same question as ``verdict().view``, which
        gates the chrome; see ``NodeVerdict``."""
        return (node_kind, node_id) in self.granted_view


def compute_visibility(
    db: Session,
    hub_user_id: int,
    *,
    closures: RbacClosures | None = None,
    tree: NodeTree | None = None,
) -> VisibilityResult:
    """§5.1's two folds, run over the whole tree for one user.

    DRIVEN FROM THE GRANTS, NOT FROM THE TREE (§8.1). The seed set arrives
    from ``matching_grants`` in one query; nothing below runs an ACL check,
    a closure intersection, or a query per node.

    Pass ``closures`` to share one ``RbacClosures`` snapshot with the rest of
    a request (§8.2); pass ``tree`` to reuse one node tree across several
    users, which is what a "who can access this node?" panel wants.
    """
    graph = closures if closures is not None else RbacClosures(db)
    node_tree = tree if tree is not None else build_node_tree(db)
    seeds = matching_grants(db, hub_user_id, closures=graph)
    return fold(node_tree, seeds)


def fold(tree: NodeTree, seeds: list[GrantMatch]) -> VisibilityResult:
    """The pure half — a tree and a seed set in, three sets out.

    Split from ``compute_visibility`` so the §5.4 property tests can drive
    the algebra directly with hand-built seed sets, and so the folds
    themselves never touch a Session.
    """
    # An EDIT seed is also a VIEW seed. §6.1 states the two folds as
    #
    #     edit(n) = seed_edit(n) ∨ edit(parent(n))
    #     view(n) = seed_view(n) ∨ view(parent(n)) ∨ edit(n)
    #
    # and folding the UNION of the two seed sets is exactly that third
    # disjunct, because the fold of a union is the union of the folds. So
    # "edit implies view" needs no separate pass and, more importantly,
    # cannot be forgotten by one: it is a subset relation between the two
    # seed sets, established here, on one line.
    view_seeds = {(m.node_kind, m.node_id) for m in seeds}
    edit_seeds = {(m.node_kind, m.node_id) for m in seeds if m.level == LEVEL_EDIT}

    granted_view = _fold_down(tree, view_seeds)
    granted_edit = _fold_down(tree, edit_seeds)

    visible = set(granted_view)
    _reveal_ancestors(tree, view_seeds, visible)

    _apply_mirror_substitution(tree, granted_view, granted_edit)

    return VisibilityResult(
        tree=tree,
        granted_view=granted_view,
        granted_edit=granted_edit,
        visible=visible,
    )


def _fold_down(tree: NodeTree, seeds: set[NodeRef]) -> set[NodeRef]:
    """``granted(n) = seed(n) ∨ granted(parent(n))`` — the fold DOWN the root
    path (§5.1), for every node at once.

    §8.1 step 3 states it as ``(ancestors(n) ∪ {n}) ∩ S ≠ ∅``, i.e. O(depth)
    per node with no subtree walk. Descending from the root computes the same
    predicate for the whole tree in a single O(nodes) pass, because a node's
    parent is always settled before the node is reached — the same traversal
    ``_walk_from_root`` already uses to decide rootedness, so orphans are
    excluded here for free rather than by a second check.

    Iterative (§3.3). A node's descendants inherit unconditionally: D3 says a
    grant covers everything beneath it and there is NO per-node "stop
    inheriting" flag. To hide something, do not grant the ancestor — grant the
    object, and let ``_reveal_ancestors`` open the path to it. The rejected
    alternative (an ``inherits_access`` boolean) fails the other way: new
    children of a granted node would become visible with nobody deciding so.
    If the authoring volume this implies comes up — and it will, since 111 of
    the 203 live components carry their own ``access_control`` today — that
    is §5.5's known and accepted cost, not a reason to add a flag.
    """
    granted: set[NodeRef] = set()
    if tree.root is None:
        return granted

    frontier: list[tuple[NodeRef, bool]] = [(tree.root, tree.root in seeds)]
    seen = {tree.root}
    while frontier:
        ref, inherited = frontier.pop()
        if inherited:
            granted.add(ref)
        for child in tree.children.get(ref, ()):
            if child in seen:
                continue
            seen.add(child)
            frontier.append((child, inherited or child in seeds))
    return granted


def _reveal_ancestors(tree: NodeTree, seeds: set[NodeRef], visible: set[NodeRef]) -> None:
    """``visible(n) = granted(n) ∨ ∃c ∈ children(n): visible(c)`` — the fold
    UP the subtree (§5.1), computed WITHOUT walking any subtree.

    WHY THIS IS NOT THE FOLD THE PLAN STATES, AND WHY IT IS THE SAME
    PREDICATE. §5.1 states ``visible`` declaratively, as a fold up the
    subtree, and the naive reading of that invites an O(nodes × subtree)
    implementation that asks every node about everything beneath it. §8.1
    step 4 gives the equivalent that is O(|S| × depth)::

        granted(n)  ⟺  some seed lies on n's root path   (n itself, or above)
        visible(n)  ⟺  granted(n)  OR  n is an ANCESTOR of some seed
        visible      =  granted ∪ ⋃ ancestors(s) for s ∈ S

    The two are one statement read from either end: "a seed lies in n's
    subtree" and "n is an ancestor of that seed" are the same fact. The
    declarative form is the specification and the one to reason with; this is
    the form that runs, and the reader is owed the equivalence rather than
    left to rediscover it. §5.1's own summary sentence is the bridge — *a
    node is visible if and only if some node on its root-path, or anywhere in
    its subtree, is a seed.*

    DO NOT CHOOSE A STRUCTURE WHOSE COST DEPENDS ON ``|S|`` STAYING SMALL.
    §8.1 opens "grants are far fewer than nodes", and that is an expectation
    about authoring practice, not an invariant. It has a known and
    non-hypothetical counterexample: §5.5 has no break-glass, so hiding one
    widget on an otherwise-open canvas means granting its 19 siblings
    individually, and 111 of 203 live components already carry their own
    ``access_control`` — 111 places that may each force a parent grant to be
    decomposed. Grants could plausibly end up on a majority of nodes. That
    costs nothing HERE, because ``_fold_down`` is O(nodes × depth) regardless
    and this is O(|S| × depth), so a large ``|S|`` makes the second
    comparable to the first rather than making anything quadratic. It would
    cost a great deal in a design that assumed otherwise.

    Mutates ``visible`` in place, and the early exit is what keeps the bound
    honest: a walk stops the moment it reaches a node already known visible,
    so the total work is the size of the union of the ancestor paths, not the
    sum of their lengths.

    ORPHAN SEEDS REVEAL NOTHING. ``tree.ancestors`` returns empty for an
    orphan, so a grant written on a node whose chain does not reach the hub
    opens no path — §5.4 property 6, and the same fail-closed rule as
    everywhere else. A grant is not a way to smuggle an unreachable node back
    into the tree.
    """
    for seed in seeds:
        for ancestor in tree.ancestors(seed):
            if ancestor in visible:
                # Everything above this point is already visible — every
                # ancestor path ends at the root, so reaching a visible node
                # means the rest of this one has been walked before.
                break
            visible.add(ancestor)


def _apply_mirror_substitution(
    tree: NodeTree, granted_view: set[NodeRef], granted_edit: set[NodeRef]
) -> None:
    """§3.4 — a mirror is gated by its TARGET as well as by its own position.

    THE RULE: ``granted(mirror) = granted(mirror's own position) AND
    granted(target)``. Both halves, and the conjunction is the point.

    WHY BOTH HALVES, since §3.4's wording says "evaluated at its target's
    node, NOT at its own position in the tree" and that reads like a
    replacement. Two things settle it, and the owner confirmed the reading on
    2026-08-30:

    1. **It is what the code does today.** ``gridstack_service.py:698-702``
       substitutes the target's ``access_control`` into the mirror's widget
       entry, but that substitution runs inside
       ``filter_widget_content_for_user``, which only ever sees a canvas the
       user has ALREADY passed the tab-level check for — ``tab_service.py:93``
       says so in as many words ("inherits from tab, which is already
       enforced separately"). So today's effective rule is already
       position AND target; the substitution is only ever the second half.
    2. **Pure replacement breaks §5.4 property 2.** ``visible(n) ⟹
       visible(parent(n))`` is one of the six properties the whole step is
       tested against. Give a user a grant on target ``t`` in tab A and put a
       mirror of it in tab B they hold nothing on: replacement makes the
       mirror visible while its own parent tab is not, and upward-closure
       fails. The conjunction cannot produce that, because a mirror is
       granted only if its own position was.

    What §3.4 is actually protecting against survives intact, and it is the
    reason any of this exists: **dropping a mirror into a widely-granted tab
    must not bypass the target's grants.** Under the conjunction the mirror's
    position is granted, the target is not, so the mirror is not — which is
    exactly the intended refusal.

    ``visible`` IS DELIBERATELY NOT TOUCHED. A mirror whose target is
    ungranted stays visible-but-not-granted, i.e. ``revealed``, i.e. a shell
    — which is precisely today's ``{type: 'restricted', data: null}``
    sentinel, whose KEY stays in the payload (``tab_service.py:100``). Making
    the mirror invisible instead would delete it from the canvas and change
    behaviour that this step must not change.

    SOUND ONLY BECAUSE A MIRROR IS A LEAF. This runs AFTER ``_fold_down``, so
    it cannot un-grant a node's descendants, which would break §5.4 property
    3 (``granted(n) ⟹ granted(c)``). Mirrors have no children: a mirror's
    ``type`` is ``mirror``, so it is never an SBN root and never a gridstack
    representation, and nothing can name it as a parent. If a mirror ever
    becomes able to hold children, this adjustment has to move INTO the fold
    rather than sit after it.
    """
    for granted in (granted_view, granted_edit):
        # SNAPSHOT, so every mirror is judged against the same state. Reading
        # the set while removing from it would make one mirror's verdict
        # depend on whether another had been processed first, which §5.4
        # property 5 forbids. ``build_node_tree`` already resolves a mirror
        # of a mirror to a dangling target, so no chain can reach here — this
        # is the second lock on the same door, and it costs one set copy of a
        # few hundred entries.
        before = frozenset(granted)
        for mirror_ref, target_ref in tree.mirror_targets.items():
            if mirror_ref not in before:
                continue
            # A dangling target (None) never satisfies the second half.
            if target_ref is None or target_ref not in before:
                granted.discard(mirror_ref)


# ---------------------------------------------------------
# §6.2 — the revoke-time confirmation: "what would they still retain?"
# ---------------------------------------------------------


class RetainedAccess(NamedTuple):
    """The answer to §6.2's question, for one grant about to be revoked.

    ``node``               the grant's own node.
    ``retained_view``      nodes in ``{node} ∪ descendants(node)`` this
                           principal would STILL reach after the revoke.
    ``retained_edit``      the subset of those they would still EDIT.
    ``responsible_grants`` surviving seeds sitting AT OR BELOW ``node``.
                           These are the rows §6.2's `[ Remove that too ]`
                           would delete — the narrow grants some other admin
                           made deliberately.
    ``covering_grants``    surviving seeds sitting STRICTLY ABOVE ``node``.
                           Different case, different sentence in the modal:
                           the principal keeps everything regardless, because
                           an ancestor grant covers it, so revoking this row
                           changes nothing for them. This is §6.2's
                           "already granted by Nav 1" redundancy, and the one
                           it says to DISPLAY rather than delete.

    Empty ``retained_*`` with empty ``covering_grants`` is the plain case:
    the revoke does what the admin expects and no confirmation is needed.
    """

    node: NodeRef
    retained_view: list[NodeRef]
    retained_edit: list[NodeRef]
    responsible_grants: list[GrantMatch]
    covering_grants: list[GrantMatch]


def what_would_they_retain(
    db: Session,
    grant_id: int,
    *,
    closures: RbacClosures | None = None,
    tree: NodeTree | None = None,
) -> RetainedAccess | None:
    """§6.2's revoke-time confirmation. ``None`` if the grant does not exist.

    THIS IS WHAT MAKES D4 SAFE, and that is why §9 calls it required rather
    than optional and why it ships WITH the write path rather than after it.
    D4 chose to DERIVE edit grants at read time instead of collapsing them
    into storage, and the one thing the collapse rule was actually good at
    was making revocation feel complete: delete the high grant and the low
    ones went with it. Derivation leaves them, so the admin's mental model
    ("Alice has nothing now") diverges from the truth unless something says
    so out loud. This function is that something.

    IT COMPUTES, IT DOES NOT ENFORCE, AND IT DELETES NOTHING. §6.2 is
    explicit that the confirmation is ADVISORY and that `[ Leave it ]` is a
    legitimate answer — narrow grants made by other admins are usually
    deliberate. Nothing here writes; the caller shows the answer and the
    admin decides. ``resource_grant_service.delete_grant`` stays the plain
    single-row revoke that an approved multi-row removal calls once per
    named row.

    ONE FOLD, NOT A SECOND TRAVERSAL. ``fold`` takes a seed set directly and
    touches no Session, which is exactly so this can be the same function
    called with one seed removed. If a future change makes ``fold`` need a
    ``db``, this is the caller that breaks and it should be fixed by keeping
    ``fold`` pure, not by writing a subtree walk here.

    THE DROP HAPPENS ON THE SEED SET, NOT IN THE DATABASE. No transaction is
    opened, nothing is deleted-then-rolled-back, and the answer is a genuine
    hypothetical about a row that still exists. That matters for a GET.

    Pass ``closures`` / ``tree`` to share one snapshot per request (§8.2,
    §8.4). Never cache either across requests — both are authorization
    inputs.
    """
    grant = get_grant(db, grant_id)
    if grant is None:
        return None

    graph = closures if closures is not None else RbacClosures(db)
    node_tree = tree if tree is not None else build_node_tree(db)

    seeds = seeds_for_principal(
        db,
        role_id=grant["role_id"],
        scope_id=grant["scope_id"],
        user_id=grant["user_id"],
        closures=graph,
    )
    # The whole hypothetical, on one line: the same seeds this principal has
    # today, minus the one row being revoked. Compared by grant_id rather
    # than by node, because several DIFFERENT grants can sit on one node —
    # §7's "a node carries a bag of grants, not an ACL" — and dropping the
    # node would silently revoke the principal's other rows on it too.
    surviving = [seed for seed in seeds if seed.grant_id != grant_id]

    result = fold(node_tree, surviving)

    node: NodeRef = (grant["node_kind"], grant["node_id"])
    region = {node} | node_tree.descendants(node)
    ancestors = set(node_tree.ancestors(node))

    return RetainedAccess(
        node=node,
        retained_view=sorted(ref for ref in region if ref in result.granted_view),
        retained_edit=sorted(ref for ref in region if ref in result.granted_edit),
        responsible_grants=[s for s in surviving if (s.node_kind, s.node_id) in region],
        covering_grants=[s for s in surviving if (s.node_kind, s.node_id) in ancestors],
    )


# ---------------------------------------------------------
# §0.1 (session_handoff_2026-09-02-grant-editor.md) — inherited grants,
# the ancestor half of §9's "who can access this node" panel
# ---------------------------------------------------------


def list_inherited_grants(
    db: Session,
    node_kind: str,
    node_id: int,
    *,
    tree: NodeTree | None = None,
) -> list[dict]:
    """Every grant stored on an ANCESTOR of ``(node_kind, node_id)``, grouped
    by which ancestor it came from.

    THE GAP THIS FILLS. ``resource_grant_service.list_grants_for_node``'s own
    docstring says so directly: *"Inherited grants come from the descending
    fold, which is not built yet."* This is that read, finally built —
    handoff §0.1 confirmed it was still the one thing blocking §9's "who can
    access this node" panel from showing the whole picture, not just the
    direct rows.

    NOT A VISIBILITY COMPUTATION, AND DELIBERATELY SO. This does not
    intersect anything with any user's ``effective_pairs`` — it lists what
    is STORED, full stop. That is the right answer for this panel because of
    §5.5's D3: a grant is unconditional for everything beneath it, with no
    per-node "stop inheriting" flag, so EVERY ancestor's grants reach this
    node regardless of who happens to be asking. An admin auditing "why can
    people see this" needs the stored facts, not one user's filtered view of
    them — ``compute_visibility`` is the function for that question, and it
    answers a different one.

    Nearest ancestor first, root-ward — the order ``NodeTree.ancestors``
    already returns, and the one worth keeping: the closest thing controlling
    this node is the first line of the answer, not the last.

    ANCESTORS WITH NO GRANTS ARE OMITTED, not returned with an empty
    ``grants`` list. There is nothing to name them for, and listing every
    ancestor whether or not it has anything to report would make the
    response's length track the node's DEPTH rather than the number of
    grants actually reaching it from above — the thing an admin is scanning
    for.

    Returns ``[]`` for an unknown node, for the hub itself (no ancestors),
    and for an orphan (§3.3) — ``NodeTree.ancestors`` already returns ``[]``
    for all three, so none of them needs a separate check here.
    """
    node_tree = tree if tree is not None else build_node_tree(db)
    entries: list[dict] = []
    for ancestor_kind, ancestor_id in node_tree.ancestors((node_kind, node_id)):
        rows = list_grants_for_node(db, ancestor_kind, ancestor_id)
        if not rows:
            continue
        entries.append({"node_kind": ancestor_kind, "node_id": ancestor_id, "grants": rows})
    return entries


# ---------------------------------------------------------
# §0.3 (session_handoff_2026-09-02-grant-editor.md) — what can this
# person access
# ---------------------------------------------------------


# ---------------------------------------------------------
# Wiring — resolving a gridstack's own node, and one caller's access
# for a whole request (plan §8.1 step 1, §8.3, §8.4)
# ---------------------------------------------------------


def resolve_gridstack_node(db: Session, gridstack: GridstackV2) -> NodeRef | None:
    """The AC node THIS gridstack itself is, for a caller addressing it
    directly by its own ``document_id`` (every ``tabs_v2.py`` route does).

    Gridstacks hold no grants and are not a node kind (§3.2) — a root
    gridstack's own node is the tab it belongs to (root or variant); a
    sub-grid's own node is its representation component, the row its
    access control was relocated onto by
    ``migrate_subtab_access_control_to_components.py``. Both branches are
    exactly ``_owner_of_gridstack``'s two branches, re-derived here as a
    single-gridstack query rather than from the whole-tree bulk maps —
    appropriate because a router resolves ONE gridstack per request (the one
    named in the URL), not all of them.

    ``None`` for a sub-grid with no representation row yet (a pre-backfill
    row — see ``_owner_of_gridstack``'s own docstring on why this can exist)
    and for a root gridstack with no ``parent_tab_id`` (schema-invalid, never
    produced by the service layer). The caller must treat ``None`` as
    INVISIBLE (§3.3), never as open — there is nothing here to grant.
    """
    if gridstack.parent_id is None:
        return ("tab", gridstack.parent_tab_id) if gridstack.parent_tab_id is not None else None
    representation = (
        db.query(ComponentV2.id).filter(ComponentV2.current_grid_id == gridstack.id).first()
    )
    return ("component", representation[0]) if representation is not None else None


def resolve_gridstack_parent_node(db: Session, gridstack: GridstackV2) -> NodeRef | None:
    """The AC node ONE LEVEL ABOVE ``gridstack``'s own (``resolve_gridstack_node``'s
    answer) — plan §6.3's ``parent(n)``, for a caller addressing ``n`` by its
    own ``document_id`` the way every ``tabs_v2.py`` write route does
    (project_ac_enforcement_gap.md item 2: delete / move / reorder → parent(n)).

    Re-derived as a single-gridstack lookup rather than pulled from
    ``NodeTree``, for the same reason ``resolve_gridstack_node`` is: a write
    route resolves ONE node per request, not the whole tree.

    Two branches, and the ROOT one has two cases inside it — exactly
    ``NodeTree``'s own comment on why ``parent_tab_id`` must be read before
    ``nav_tab_id`` (§3.1): a variant's parent is the tab it varies, not the
    nav tab it happens to share a ``nav_tab_id`` with.

    - ``gridstack.parent_id is None`` (a root gridstack — a root tab OR a
      variant): its parent depends on which. A VARIANT's parent is the tab
      it varies (``TabV2.parent_tab_id``); an ordinary root tab's parent is
      its nav tab (``TabV2.nav_tab_id``).
    - otherwise (a sub-grid): its parent is whatever AC node owns its own
      PARENT gridstack — exactly what ``resolve_gridstack_node`` already
      answers for any gridstack, so the sub-grid case is one recursive call
      rather than a second case analysis.

    ``None`` for an orphaned root (no owning ``TabV2`` row, or one with
    neither ``parent_tab_id`` nor ``nav_tab_id`` set — §3.3's known
    two-row anomaly, tabs id=71/73) and for a sub-grid whose own parent
    gridstack row is gone. Callers must treat ``None`` as "cannot resolve —
    fail closed", never as open, matching every other ``None`` in this
    module (§3.3).
    """
    if gridstack.parent_id is None:
        tab = db.query(TabV2).filter(TabV2.id == gridstack.parent_tab_id).first()
        if tab is None:
            return None
        if tab.parent_tab_id is not None:
            return ("tab", tab.parent_tab_id)
        return ("nav_tab", tab.nav_tab_id) if tab.nav_tab_id is not None else None
    parent_gridstack = db.query(GridstackV2).filter(GridstackV2.id == gridstack.parent_id).first()
    if parent_gridstack is None:
        return None
    return resolve_gridstack_node(db, parent_gridstack)


class AccessDeniedError(Exception):
    """Base for the two ways ``require_edit`` below can refuse a write.
    Carries the ``node`` that was being checked so a caller that wants to
    log or report the refusal does not have to re-derive it."""

    def __init__(self, node: NodeRef | None) -> None:
        self.node = node
        super().__init__(f"access denied for node {node!r}")


class NodeNotViewableError(AccessDeniedError):
    """The caller cannot even VIEW ``node``. §9's fail-closed convention,
    carried from the read side to the write side: a router should map this
    to 404 — indistinguishable from "does not exist", exactly like every
    GET this plan's read step already wired (project_ac_enforcement_gap.md
    item 1)."""


class NodeNotEditableError(AccessDeniedError):
    """The caller can VIEW ``node`` but lacks EDIT. A router should map this
    to 403, not 404 — unlike the view case, the caller's own UI already
    confirms the node exists (they can see it), so hiding that fact behind a
    404 would not protect anything and would only be confusing."""


def require_edit(access: ViewerAccess | None, node: NodeRef | None) -> None:
    """Plan §6.3's ``edit(n)`` write gate — one call, shared by every
    ``gridstack_service`` mutation project_ac_enforcement_gap.md item 2
    gates (create/reorder/rename/move/delete/content writes).

    Raises rather than returning a bool, matching
    ``resource_grant_authz_service.assert_can_administer_node``'s shape: a
    caller that forgets to check a return value cannot silently let a write
    through.

    ``access=None`` is NOT "deny" — it is "no check was requested", exactly
    like every other ``access: ViewerAccess | None = None`` default already
    threaded through this module's READ-side callers (§1 of
    session_handoff_2026-09-04-tab-visibility-wiring.md): an internal caller
    that never intended to gate this call (e.g. ``create_tab_v2``'s own
    internal call into ``update_tab_content_v2`` to seed initial content)
    keeps working unchanged. Every ``tabs_v2.py`` route that mutates content
    passes a real ``ViewerAccess`` from ``Depends(get_viewer_access)``.

    The Hub Admin bypass needs no special case here — ``ViewerAccess.verdict``
    already returns ``edit=True`` unconditionally under ``full_access``.
    """
    if access is None:
        return
    verdict = access.verdict(node)
    if not verdict.view:
        raise NodeNotViewableError(node)
    if not verdict.edit:
        raise NodeNotEditableError(node)


class ViewerAccess(NamedTuple):
    """One caller's access for a whole request — computed ONCE (§8.4) and
    threaded through every tab-serving function that needs to gate content.

    ``full_access=True`` BYPASSES THE FOLD ENTIRELY, and this is not
    optional polish. Every other AC-gated surface in this codebase applies
    the Hub Admin bypass first and unconditionally
    (``resource_grants.py``'s own docstring says so in as many words), and
    the reason is concrete here, not theoretical: ``role_assignments`` holds
    exactly one row as of 2026-09-03. Without this bypass, wiring the fold
    into a read endpoint would make every JWT-recognized Hub Admin who is
    NOT that one row see a fully dark hub the moment this ships — which is
    not what phase 2's parallel run (``dependencies.is_hub_admin``) is for.
    Construct this via ``resolve_viewer_access`` below, never by hand.

    For anyone else, ``visibility`` is the real ``VisibilityResult`` and the
    fold runs exactly as designed — including D2: a non-admin with no
    grants correctly sees nothing, which is this design's own stated
    default, not a bug introduced here.
    """

    visibility: VisibilityResult | None
    full_access: bool

    def verdict(self, node: NodeRef | None) -> NodeVerdict:
        if self.full_access:
            return NodeVerdict(view=True, edit=True, revealed=False)
        if node is None or self.visibility is None:
            return INVISIBLE
        return self.visibility.verdict(*node)

    def is_granted(self, node: NodeRef | None) -> bool:
        """Gates the PAYLOAD for one node — see ``NodeVerdict``'s own
        docstring on why this is not just ``verdict(node).view``."""
        if self.full_access:
            return True
        if node is None or self.visibility is None:
            return False
        return self.visibility.is_granted(*node)


def resolve_viewer_access(
    db: Session,
    hub_user_id: int,
    *,
    is_admin: bool,
    closures: RbacClosures | None = None,
) -> ViewerAccess:
    """Build one ``ViewerAccess`` for a request.

    ``is_admin`` is supplied by the caller (``dependencies.is_hub_admin``)
    rather than computed here, so this module never has to import
    ``app.dependencies`` — that import would run backwards (a FastAPI
    dependency module belongs above domain services, not below one) and
    would risk the same kind of import cycle §6.8's own docstring already
    warns about for the reverse direction.

    Skips building a ``VisibilityResult`` at all when ``is_admin`` is true —
    the fold's answer would be discarded by ``ViewerAccess.full_access``
    regardless, and an admin is the caller most likely to hit every read
    endpoint in one page load.
    """
    if is_admin:
        return ViewerAccess(visibility=None, full_access=True)
    return ViewerAccess(
        visibility=compute_visibility(db, hub_user_id, closures=closures),
        full_access=False,
    )


def list_visible_nodes(visibility: VisibilityResult) -> list[dict]:
    """Every node in ``visibility.visible``, each with its full §5.2 triple
    — §9's "what can this person access" panel, the per-user mirror of
    ``list_inherited_grants``'s per-node one (handoff §0.3).

    PURE, matching every other pure/impure split in this module: it takes an
    already-computed ``VisibilityResult`` — the same object
    ``compute_visibility`` returns — rather than a ``db`` and a
    ``hub_user_id``, so a caller that already holds one for another reason
    in the same request never recomputes it.

    ONLY VISIBLE NODES ARE RETURNED, not the whole tree. An invisible node
    carries no information for this panel (§5.2: ``view`` gates the chrome,
    and an invisible node fails that by definition), and at the hub's
    ~240-node scale (§8.1's docstring) the omission is what keeps the
    response proportional to what the PERSON can reach, not to the size of
    the hub.

    Sorted by ``(node_kind, node_id)`` — the tuple order this module already
    uses everywhere else a node set is returned (``RetainedAccess.retained_view``
    is the same shape) — so two calls a moment apart diff meaningfully
    instead of differing only by dict/set iteration order.
    """
    return [
        {"node_kind": kind, "node_id": node_id, **visibility.verdict(kind, node_id)._asdict()}
        for kind, node_id in sorted(visibility.visible)
    ]
