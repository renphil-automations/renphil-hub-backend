"""Edit-lock propagation — plan_lock_propagation_2026-09-08.md, extended by
plan_component_locking_and_sbn_2026-09-17.md (components become lock nodes).

THE LOCK TREE IS NOT THE ACCESS-CONTROL TREE (plan §2). The two look
similar enough to be conflated — and MORE so now that components sit in
both — so this module is deliberately separate from
`access_visibility_service.build_node_tree` / `NodeTree.ancestors()` /
`NodeTree.descendants()` rather than reusing them:

  | | AC tree | Lock tree |
  |---|---|---|
  | sub-grid | not a node (transparent; represented by a component) | a node — GridstackV2 owns the lock columns |
  | sub-grid's representation row (`current_grid_id` set) | a node (the sub-grid's grants live there) | NOT a node — `lock_node_of` maps it to the gridstack |
  | real component (`current_grid_id IS NULL`) | a node | a node — an ordinary widget or SBN member IS a lock node (2026-09-17 decision A; reverses lock-propagation decision 6) |
  | hub | the root | not lockable at all |

So a `LockNode` has exactly four kinds — "nav_tab", "tab", "gridstack",
"component" — and NO "hub" case. "gridstack" has no AC-tree counterpart;
"hub" has no lock-tree one; and "component" means something narrower here
than in `NodeRef` (a representation row is a `("component", …)` AC node but
never a `("component", …)` lock node).

The tree, top to bottom: nav_tab -> tab [-> tab, a variant] -> gridstack
(a sub-grid; a root/variant canvas has no lock node of its own, its TabV2
row is the node) -> component -> component (an SBN sub-tab) -> ... Every
real component under a canvas — root canvases included — is that canvas's
descendant, so an admin entering canvas edit mode is refused while any
widget or SBN node beneath is held, and `force` breaks the whole subtree
(plan_component_locking §2.1 row 4).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Union
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.tab import TabV2

# Reused AS-IS (plan §3.1) — imported, not duplicated, so the three tables'
# staleness rule can never drift from TabV2/SBN's existing one. Safe as a
# module-level import in THIS direction only: gridstack_service.py's own
# lock_tab_by_document_id_v2/unlock_tab_by_document_id_v2 wrappers import
# edit_lock_service back, but do it as a LOCAL import inside their function
# bodies (see that module) specifically to avoid the circular top-level
# import this would otherwise create.
from app.services.gridstack_service import _validate_locked_by, is_lock_stale  # noqa: F401

_LockRow = Union[TabV2, GridstackV2, NavTabV2, ComponentV2]

# ("nav_tab" | "tab" | "gridstack" | "component", id). Deliberately the same
# shape as access_visibility_service.NodeRef (a 2-tuple of kind + id) — same
# convention, different domain. "gridstack" has no AC-tree counterpart and
# "hub" has no lock-tree one; "component" appears in BOTH, and for a real
# component (`current_grid_id IS NULL`) the two tuples are literally equal —
# which is fine, `lock_node_of` is the one sanctioned crossing point and it
# refuses to let a representation row through as a lock node.
LockNode = tuple[str, int]


def resolve_lock_node(gridstack: GridstackV2) -> LockNode:
    """The lock node a `GridstackV2` row itself is, for a caller addressing
    it directly by its own `document_id` — exactly the split
    `_safe_locked_triple` (gridstack_service.py:366) already reads off, so
    the two cannot drift: a ROOT canvas (`parent_id IS NULL`, whether it
    belongs to a root tab or a variant — both are equally "root" here) has
    no lock of its own; its owning `TabV2` row is what's actually locked. A
    SUB-grid (`parent_id IS NOT NULL`) is locked on its own row.
    """
    if gridstack.parent_id is None:
        return ("tab", gridstack.parent_tab_id)
    return ("gridstack", gridstack.id)


def ancestors(db: Session, node: LockNode) -> list[LockNode]:
    """Every node whose subtree contains `node`, nearest first. Each hop is
    a single denormalized column read — no recursive walk above the
    component level, because that part of the tree is fixed-depth:
    gridstack -> tab -> [tab ->] nav_tab, at most three hops even through a
    variant. The ONE exception is the component level (2026-09-17): an SBN
    sub-tab's chain first climbs `super_blocknote_id` link by link to its
    SBN root — unbounded in principle, "chains four deep" live — before
    reaching its canvas and the fixed part above.

    A missing row (id no longer exists) yields an empty remaining chain
    rather than raising — this mirrors every other "fail closed, not open"
    convention in this codebase (access_visibility_service.py §3.3): a
    caller checking "is any ancestor held" against a dangling reference
    should see no ancestors, not an exception, since there is nothing left
    above it to be held. An SBN chain that loops back on itself (a corrupt
    `super_blocknote_id` cycle — the AC tree guards the same case,
    `test_an_sbn_cycle_terminates_and_fails_closed`) terminates at the
    repeat rather than walking forever.
    """
    kind, node_id = node

    if kind == "nav_tab":
        return []

    if kind == "component":
        component = db.query(ComponentV2).filter(ComponentV2.id == node_id).first()
        if component is None:
            return []
        chain: list[LockNode] = []
        # The SBN chain first: each `super_blocknote_id` hop is itself a
        # ("component", …) lock node. `gridstack_id` is cascaded onto every
        # SBN member (`_cascade_gridstack_id_to_sbn_descendants`), so it is
        # the same canvas at every depth and we only need to read it once,
        # off the node itself — no need to reach the root to learn it.
        seen: set[int] = {component.id}
        parent_id = component.super_blocknote_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            chain.append(("component", parent_id))
            parent = db.query(ComponentV2).filter(ComponentV2.id == parent_id).first()
            if parent is None:
                # Dangling — fail closed: nothing above it exists to be held.
                return chain
            parent_id = parent.super_blocknote_id
        gridstack = db.query(GridstackV2).filter(GridstackV2.id == component.gridstack_id).first()
        if gridstack is None:
            return chain
        canvas_node = resolve_lock_node(gridstack)
        return chain + [canvas_node] + ancestors(db, canvas_node)

    if kind == "tab":
        tab = db.query(TabV2).filter(TabV2.id == node_id).first()
        if tab is None:
            return []
        chain: list[LockNode] = []
        # A variant's parent is the root tab it varies, not the nav tab
        # directly (mirrors resolve_gridstack_parent_node's own comment on
        # why parent_tab_id must be read before nav_tab_id).
        if tab.parent_tab_id is not None:
            chain.append(("tab", tab.parent_tab_id))
        if tab.nav_tab_id is not None:
            chain.append(("nav_tab", tab.nav_tab_id))
        return chain

    if kind == "gridstack":
        gridstack = db.query(GridstackV2).filter(GridstackV2.id == node_id).first()
        if gridstack is None:
            return []
        tab_node: LockNode = ("tab", gridstack.parent_tab_id)
        return [tab_node] + ancestors(db, tab_node)

    raise ValueError(f"Unknown lock node kind: {kind!r}")


def _components_under_gridstacks(db: Session, gridstack_ids: list[int]) -> list[LockNode]:
    """Every REAL component on any of `gridstack_ids` — `current_grid_id IS
    NULL`, so a sub-grid's representation row is never returned (it is not
    a lock node; its gridstack is). SBN members are included by
    construction: `gridstack_id` is cascaded onto every node of an SBN tree
    (`_cascade_gridstack_id_to_sbn_descendants`), so one flat query over
    the canvas ids reaches every depth of every SBN on those canvases with
    no `super_blocknote_id` walk. Mirrors and restricted-sentinel rows are
    included too — they are never locked, so they are harmless here, and
    excluding them would mean re-deriving the canvas serializer's own type
    filters in a second place."""
    if not gridstack_ids:
        return []
    rows = (
        db.query(ComponentV2.id)
        .filter(ComponentV2.gridstack_id.in_(gridstack_ids), ComponentV2.current_grid_id.is_(None))
        .all()
    )
    return [("component", row.id) for row in rows]


def _sbn_subtree(db: Session, component_id: int) -> list[LockNode]:
    """Every component reachable from `component_id` via `super_blocknote_id`,
    at any depth — adapted from `gridstack_service._collect_sbn_descendant_ids`
    (plan §4) with a visited set so a corrupt cycle terminates instead of
    recursing forever (same posture as `ancestors`' own SBN walk above)."""
    result: list[LockNode] = []
    seen: set[int] = {component_id}
    frontier = [component_id]
    while frontier:
        children = (
            db.query(ComponentV2.id).filter(ComponentV2.super_blocknote_id.in_(frontier)).all()
        )
        frontier = []
        for (child_id,) in children:
            if child_id in seen:
                continue
            seen.add(child_id)
            result.append(("component", child_id))
            frontier.append(child_id)
    return result


def descendants(db: Session, node: LockNode) -> list[LockNode]:
    """Every node in `node`'s subtree, in no particular order. A few flat
    queries, never recursive above the component level — sub-grids never
    nest (a permanent tree-shape rule, confirmed by the owner 2026-09-07),
    so a tab's own gridstacks are always exactly one or two levels down.

    A GRIDSTACK IS NO LONGER A LEAF (2026-09-17, decision A): its
    descendants are the real components on it, SBN members included. And a
    tab's/nav tab's component query must run over EVERY gridstack under it
    — root/variant canvases included — not just the sub-grids that are lock
    nodes in their own right: a root canvas has no lock node of its own
    (its TabV2 row is the node), but the widgets ON it are its descendants
    all the same, and filtering to `parent_id IS NOT NULL` for the component
    query would drop every widget on every root/variant canvas.

    THE §1.1 HOLE THIS CLOSES: a root tab's descendants include not just its
    own sub-grids but every VARIANT's sub-grids too. Today's
    `_cascade_lock_to_nested_gridstacks` only reaches the root's own
    `parent_tab_id`-matched gridstacks — a variant is a wholly separate
    `TabV2` row the cascade never touches, so locking a root currently
    leaves its variants (and their sub-grids) unlocked. This resolver is
    what makes `edit_lock_service.acquire` (phase 2) see the whole subtree.
    """
    kind, node_id = node

    if kind == "component":
        # An ordinary widget has no SBN subtree — empty. An SBN root or
        # sub-tab: its whole subtree, each as its own lock node.
        return _sbn_subtree(db, node_id)

    if kind == "gridstack":
        return _components_under_gridstacks(db, [node_id])

    if kind == "tab":
        tab = db.query(TabV2).filter(TabV2.id == node_id).first()
        if tab is None:
            return []

        if tab.parent_tab_id is not None:
            # A variant: its own sub-grids only. Variants can never
            # themselves have variants (enforced in gridstack_service.py),
            # so there is no further "variant of a variant" branch here.
            owning_tab_ids = [node_id]
            variant_ids: list[int] = []
        else:
            # A root: its variants, its own sub-grids, AND every variant's
            # sub-grids (the §1.1 hole).
            variants = db.query(TabV2.id).filter(TabV2.parent_tab_id == node_id).all()
            variant_ids = [v.id for v in variants]
            owning_tab_ids = [node_id] + variant_ids

        # ALL gridstacks under the owning tabs, root canvases included —
        # the sub-grid ones become lock nodes, every one feeds the
        # component query.
        all_gridstacks = (
            db.query(GridstackV2.id, GridstackV2.parent_id)
            .filter(GridstackV2.parent_tab_id.in_(owning_tab_ids))
            .all()
        )

        result: list[LockNode] = [("tab", v_id) for v_id in variant_ids]
        result.extend(("gridstack", g.id) for g in all_gridstacks if g.parent_id is not None)
        result.extend(_components_under_gridstacks(db, [g.id for g in all_gridstacks]))
        return result

    if kind == "nav_tab":
        # Every TabV2 under this nav tab — roots AND variants
        # (create_tab_variant_v2 copies the parent's nav_tab_id, so a
        # variant is just as much "under" the nav tab as its root is) —
        # plus every sub-grid under any of them, plus every real component
        # on any canvas under any of them.
        tabs = db.query(TabV2.id).filter(TabV2.nav_tab_id == node_id).all()
        tab_ids = [t.id for t in tabs]

        all_gridstacks = (
            db.query(GridstackV2.id, GridstackV2.parent_id)
            .filter(GridstackV2.parent_tab_id.in_(tab_ids))
            .all()
            if tab_ids
            else []
        )

        result = [("tab", t.id) for t in tabs]
        result.extend(("gridstack", g.id) for g in all_gridstacks if g.parent_id is not None)
        result.extend(_components_under_gridstacks(db, [g.id for g in all_gridstacks]))
        return result

    raise ValueError(f"Unknown lock node kind: {kind!r}")


# ---------------------------------------------------------------------------
# Row access — one place that knows how to read/write the lock quadruple
# regardless of which of the four tables `node` addresses.
# ---------------------------------------------------------------------------


def _node_row(db: Session, node: LockNode) -> _LockRow | None:
    kind, node_id = node
    if kind == "tab":
        return db.query(TabV2).filter(TabV2.id == node_id).first()
    if kind == "gridstack":
        return db.query(GridstackV2).filter(GridstackV2.id == node_id).first()
    if kind == "nav_tab":
        return db.query(NavTabV2).filter(NavTabV2.id == node_id).first()
    if kind == "component":
        return db.query(ComponentV2).filter(ComponentV2.id == node_id).first()
    raise ValueError(f"Unknown lock node kind: {kind!r}")


def _node_rows(db: Session, nodes: list[LockNode]) -> list[tuple[LockNode, _LockRow]]:
    """Batch form of `_node_row` — one `IN` query per kind instead of one
    query per node, preserving `nodes`' order and dropping the missing.
    Added 2026-09-17 with components as lock nodes: `acquire`'s descendant
    walk used to touch a handful of tab/gridstack rows, and now reaches
    every widget under the node (144 real components under one nav tab on
    live, 21 under the largest root tab). At ~165ms per Neon round trip
    (`_serialize_gridstack_content`'s own profiling note) a per-row loop
    would turn an admin's canvas edit-mode entry into a multi-second wait;
    this keeps it at four queries regardless of subtree size."""
    by_kind: dict[str, list[int]] = {}
    for kind, node_id in nodes:
        by_kind.setdefault(kind, []).append(node_id)
    model_for = {"tab": TabV2, "gridstack": GridstackV2, "nav_tab": NavTabV2, "component": ComponentV2}
    found: dict[LockNode, _LockRow] = {}
    for kind, ids in by_kind.items():
        model = model_for.get(kind)
        if model is None:
            raise ValueError(f"Unknown lock node kind: {kind!r}")
        for row in db.query(model).filter(model.id.in_(ids)).all():
            found[(kind, row.id)] = row
    return [(node, found[node]) for node in nodes if node in found]


def _locked_quad(row: _LockRow) -> tuple[bool, str, datetime | None, str | None]:
    return bool(row.locked), (row.locked_by or ""), row.locked_at, row.lock_token


def _label_for(node: LockNode, row: _LockRow) -> str:
    """A sub-grid's human name lives on `GridstackV2.name`; a tab's or nav
    tab's on `.title` — same split `_format_tab_summary` already reads. A
    component's is `.title`, falling back to its `.type`: widgets very often
    have no title at all, and `'"" is being edited by …'` must never be
    emitted — "text is being edited by bob@…" at least names the kind of
    thing that is held."""
    kind, _ = node
    if kind == "gridstack":
        return row.name or ""
    if kind == "component":
        return row.title or row.type or ""
    return row.title or ""


def _clear_lock(row: _LockRow) -> None:
    row.locked = False
    row.locked_by = ""
    row.locked_at = None
    row.lock_token = None


def _write_lock(row: _LockRow, holder: str, now: datetime, token: str) -> None:
    row.locked = True
    row.locked_by = holder
    row.locked_at = now
    row.lock_token = token


def _force_break_lock(row: _LockRow) -> None:
    """The descendant-clearing half of a force takeover — DELIBERATELY NOT
    `_clear_lock`. `locked` still flips to False (the row reads as free —
    decision 2's single-lock-row-per-subtree holds again the instant the
    takeover completes) and `locked_at` is dropped, but `locked_by` is KEPT
    rather than blanked: it is the only surviving trace that lets
    `require_live_session` tell the ousted holder's next write apart as
    EDIT_SESSION_TAKEN_OVER rather than EDIT_SESSION_MISSING (§5.5) — a
    fully-blanked row is indistinguishable from "never held," which is the
    WRONG story to tell someone who very much did hold it a moment ago.
    `lock_token` is rotated to a fresh, unpublished value (never `None` —
    `None` reads as "no live session ever" everywhere else in this module,
    which would misreport this the same way blanking `locked_by` would) so
    their old token can never validate again, matching a full release's
    guarantee via a different mechanism."""
    row.locked = False
    row.locked_at = None
    row.lock_token = uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_seconds() -> int:
    return get_settings().TAB_LOCK_TTL_SECONDS


# ---------------------------------------------------------------------------
# Acquire / release / force — plan §4. Multiple-granularity locking: an X
# lock on `node` plus an implicit IX on every ancestor (§1's worked table).
# ---------------------------------------------------------------------------


class BlockingHolder(NamedTuple):
    """One entry of a refused acquire's conflicting-holder list (§4.2,
    §5.5's `NODE_LOCKED` body) — who holds what, relative to the node the
    caller tried to acquire."""

    holder: str
    node_label: str
    relation: str  # "self" | "ancestor" | "descendant"


class LockConflictError(ValueError):
    """Raised by `acquire` when refused. A `ValueError` on purpose — every
    v2 tabs/nav-tabs router already does `except ValueError as e: raise
    value_error_to_http_exception(e)`, which maps an unrecognized message to
    400 (`tabs.py:96`) — the exact status code today's lock-conflict tests
    already pin. Phase 3's dedicated `NODE_LOCKED` 409 (§5.5) reads
    `.blocking` off this same exception rather than replacing it, so this
    phase changes NOTHING about the wire contract; only the conflict
    detection underneath (self-only -> whole subtree) does."""

    def __init__(self, message: str, blocking: list[BlockingHolder]) -> None:
        super().__init__(message)
        self.blocking = blocking


class LockGrant(NamedTuple):
    """What a successful `acquire` hands back. Named distinctly from
    `EditSession` below (§5.1) — that one is the REQUEST-side object
    (holder + every token the caller's headers presented), built once per
    request from `X-Edit-Tokens`; this one is the ACQUIRE-side receipt for
    a single node, returned once per acquire call. The two are related
    (a `LockGrant.token` is exactly the kind of value `EditSession.tokens`
    holds) but are not the same shape and must not be conflated."""

    token: str
    holder: str
    node: LockNode
    expires_at: datetime


def _conflict_message(conflicts: list[BlockingHolder]) -> str:
    """Decision 1's example phrasing (§1, §6.4/§6.5's own wording), keyed
    off the FIRST conflict found — self, if there is one (most directly
    actionable for the caller), else the first descendant encountered.
    Ancestor conflicts never reach here: `acquire` raises for those before
    this is ever called (see its own comment on why force can't help
    there)."""
    first = conflicts[0]
    if first.relation == "self":
        return f'"{first.node_label}" is already being edited by {first.holder}.'
    return f'An item inside this, "{first.node_label}", is being edited by {first.holder}.'


def acquire(db: Session, node: LockNode, holder: str, *, force: bool = False) -> LockGrant:
    """§4.1. Refuses if `node` itself, any ANCESTOR, or any DESCENDANT is
    held fresh by someone else; same holder never conflicts with themself,
    at any level. `force` (§4.2, decision 8) only ever applies to `node`'s
    own subtree (self + descendants) — an ancestor conflict is ALWAYS
    refused regardless of `force`, because taking over `node` does not
    grant any right to end a DIFFERENT session someone else holds further
    up the tree; only a force call made ON that ancestor itself could do
    that.
    """
    try:
        holder = _validate_locked_by(holder) or ""
        if not holder:
            raise ValueError("locked_by is required")

        # 1. Ancestors — checked first and unconditionally (§5.2's own
        # ordering note doesn't apply here, that's about auth-before-session
        # on WRITES; this is just "the broadest veto wins first" for a
        # clear message).
        for anc in ancestors(db, node):
            anc_row = _node_row(db, anc)
            if anc_row is None:
                continue
            anc_locked, anc_holder, anc_locked_at, _ = _locked_quad(anc_row)
            if anc_locked and anc_holder and anc_holder != holder and not is_lock_stale(anc_locked_at):
                raise LockConflictError(
                    f'A parent item, "{_label_for(anc, anc_row)}", is being edited by {anc_holder}.',
                    blocking=[BlockingHolder(anc_holder, _label_for(anc, anc_row), "ancestor")],
                )

        # 2. Self.
        self_row = _node_row(db, node)
        if self_row is None:
            raise ValueError("Node not found")
        self_locked, self_holder, self_locked_at, _self_token = _locked_quad(self_row)

        conflicts: list[BlockingHolder] = []
        if self_locked and self_holder and self_holder != holder and not is_lock_stale(self_locked_at):
            conflicts.append(BlockingHolder(self_holder, _label_for(node, self_row), "self"))

        # 3. Descendants — every one of them, not just the first, so a
        # refused (non-force) acquire can name everyone force would kick
        # (§4.2's own requirement: "a refused acquire returns the full
        # conflicting-holder list").
        descendant_rows = _node_rows(db, descendants(db, node))
        for desc, desc_row in descendant_rows:
            d_locked, d_holder, d_locked_at, _d_token = _locked_quad(desc_row)
            if d_locked and d_holder and d_holder != holder and not is_lock_stale(d_locked_at):
                conflicts.append(BlockingHolder(d_holder, _label_for(desc, desc_row), "descendant"))

        if conflicts and not force:
            raise LockConflictError(_conflict_message(conflicts), blocking=conflicts)

        now = _utc_now()

        if force:
            # Decision 8: force breaks the WHOLE subtree, not just the rows
            # that actually conflicted — after this call there is exactly
            # one lock row for the whole subtree (decision 2), so any other
            # row still claiming to be locked (fresh OR stale — a stale one
            # would be cleared as housekeeping anyway) is stale bookkeeping
            # the instant `node` is taken over. `_force_break_lock`, NOT
            # `_clear_lock` — see its own docstring on why the ousted
            # holder's next validated write needs EDIT_SESSION_TAKEN_OVER
            # (phase 3), not EDIT_SESSION_MISSING. Rows already held by
            # THIS holder are left alone — nothing to take over from
            # yourself.
            for _desc, desc_row in descendant_rows:
                d_locked, d_holder, _d_at, _d_token = _locked_quad(desc_row)
                if d_locked and d_holder and d_holder != holder:
                    _force_break_lock(desc_row)
        else:
            # Housekeeping (§4.1 step 4): a STALE foreign row left in the
            # subtree is cleared even on an ordinary acquire with no
            # conflicts — nobody's live session is ended by this (it was
            # already dead), it just keeps derived reads from showing a
            # ghost.
            for _desc, desc_row in descendant_rows:
                d_locked, d_holder, d_locked_at, _d_token = _locked_quad(desc_row)
                if d_locked and d_holder and d_holder != holder and is_lock_stale(d_locked_at):
                    _clear_lock(desc_row)

        # Re-entry (same holder, whether the previous state was fresh or
        # stale) keeps the existing token — pins
        # test_the_same_holder_can_still_directly_lock_a_sub_grid_the_root_already_cascaded_onto's
        # intent. Anyone else (including a force takeover of a foreign
        # lock) mints a new one, which is what invalidates the old
        # holder's copy.
        token = _self_token if (self_locked and self_holder == holder and _self_token) else uuid4().hex

        _write_lock(self_row, holder, now, token)
        db.commit()

        return LockGrant(
            token=token, holder=holder, node=node, expires_at=now + timedelta(seconds=_ttl_seconds())
        )
    except Exception:
        db.rollback()
        raise


def release(db: Session, node: LockNode, holder: str, *, force: bool = False) -> None:
    """Ownership rule UNCHANGED from `unlock_tab_by_document_id_v2`'s
    existing one — deliberately no `and unlocked_by` short-circuit (the
    omission bypass fixed 2026-09-03 in three places): a falsy `holder`
    must read as "not proven to be the holder", never as "no identity ⇒ let
    it through". `force` here is the EXISTING unlock force (owner decision
    2026-09-03: unrestricted, skips ownership AND staleness) — a different
    flag from `acquire`'s new one above; unlock's force is unchanged by
    this plan."""
    try:
        holder = _validate_locked_by(holder) or ""

        row = _node_row(db, node)
        if row is None:
            raise ValueError("Node not found")

        locked, locked_by, locked_at, _token = _locked_quad(row)
        held_by_someone_else = locked and locked_by and locked_by != holder
        if not force and held_by_someone_else and not is_lock_stale(locked_at):
            raise ValueError(f"Tab is locked by {locked_by}")

        _clear_lock(row)
        db.commit()
    except Exception:
        db.rollback()
        raise


def _held_rows(db: Session) -> list[tuple[LockNode, _LockRow]]:
    """Every `locked == True` row in the whole system, as (lock node, row)
    — the shared row source for BOTH whole-system sweeps below
    (`release_locks_now_unauthorized`, `resolve_lock_view`), so the two can
    never disagree about which tables count. Four queries, one per lock
    node kind. A root/variant canvas's `GridstackV2` row is skipped
    (`parent_id IS NULL` — not a lock node; its TabV2 row is), and so is a
    sub-grid's representation component (`current_grid_id IS NOT NULL` —
    not a lock node either; its gridstack is). Neither is ever written by
    `acquire`, so this is belt-and-braces against a hand-edited row
    misreporting as a live session."""
    rows: list[tuple[LockNode, _LockRow]] = [
        (("tab", row.id), row) for row in db.query(TabV2).filter(TabV2.locked.is_(True)).all()
    ]
    rows += [
        (("gridstack", row.id), row)
        for row in db.query(GridstackV2)
        .filter(GridstackV2.locked.is_(True), GridstackV2.parent_id.isnot(None))
        .all()
    ]
    rows += [
        (("nav_tab", row.id), row) for row in db.query(NavTabV2).filter(NavTabV2.locked.is_(True)).all()
    ]
    rows += [
        (("component", row.id), row)
        for row in db.query(ComponentV2)
        .filter(ComponentV2.locked.is_(True), ComponentV2.current_grid_id.is_(None))
        .all()
    ]
    return rows


# ---------------------------------------------------------------------------
# Revoke-triggered cleanup — locks and access are two separate systems
# (this module's own docstring, "THE LOCK TREE IS NOT THE ACCESS-CONTROL
# TREE"), and NOTHING before this normally keeps them in sync: revoking a
# `role_assignments` row or a `resource_grants` row never touched a single
# `locked`/`locked_by` column. A user edited mid-session, then revoked, kept
# a fully live (non-stale) lock — on the node they were editing AND on every
# ancestor they separately held — for the rest of the TTL, with no
# automatic path back except an admin's manual force takeover (and that
# takeover, on the DESCENDANT node alone, cannot even clear an ancestor's
# own independently-held lock — `acquire`'s own docstring above: "only a
# force call made ON that ancestor itself could do that"). This closes that
# gap at the source: every REVOKE call site below calls this right after
# its own commit, so a held lock the holder can no longer justify is
# cleared the moment the revoke that invalidated it lands, not whenever
# someone next happens to notice.
async def release_locks_now_unauthorized(db: Session) -> list[LockNode]:
    """Sweep every FRESH (non-stale) held lock in the system and force-
    release any whose holder no longer has `edit()` on that lock's node
    under the CURRENT database state. Returns the nodes actually released.

    Whole-system sweep, not scoped to the specific write that triggered it
    — deliberately, mirroring `resolve_lock_view`'s own reasoning ("~240
    nodes total and, in practice, a handful of concurrent editors at most —
    this is small"): a revoked `role_assignments` row can widen or narrow
    access anywhere the resulting closure touches, and a `resource_grants`
    row's effect passes through the mirror/fold machinery
    (`access_visibility_service.py`) which does not map onto the lock tree
    one-to-one (this module's own top docstring). Re-deriving "every node
    this write could possibly have affected" through that fold would be far
    more code than just re-checking the small number of things that are
    actually held right now.

    Best-effort in the same sense every other release path in this module
    already is (`Sidebar.tsx`'s "Advisory unlock" convention, mirrored
    here): a holder this can't resolve to a live `hub_users` row (deleted
    account) is treated as no-longer-authorized and released too, matching
    every other "fail closed on an orphan" rule in this codebase.

    ══════════════════════════════════════════════════════════════════════
    ASYMMETRIC FAIL-SAFE — READ BEFORE "SIMPLIFYING" THIS. Getting this
    wrong in one direction (a lock stays stuck a little longer) just
    annoys someone until the TTL or a manual takeover; getting it wrong in
    the OTHER direction (a lock is force-released out from under someone
    who was legitimately still allowed to hold it) can lose their unsaved
    work with no warning at all — the exact hazard the takeover confirm
    dialog exists to make an admin consciously accept ("Take over anyway?
    Their unsaved changes may be lost."), except here nobody gets asked.
    So every check below is written to require POSITIVE proof of "no
    longer authorized" before releasing; anything uncertain is treated as
    "still authorized" and left alone.

    THIS IS WHY THE AIRTABLE CALL EXISTS AND MUST NOT BE REMOVED. `is_hub_
    admin`'s branch 1 (the JWT `roles` claim, sourced from Airtable at
    login) cannot be reconstructed from a stored `locked_by` — it is a
    bare email, not a live token. `role_assignments` holds exactly 1 row
    against 22 real Hub Admins as of the last parity survey
    (project-ac-hub-admin-resolver), so "not a role_assignments admin" is
    NOT "not an admin" — it is the common case for a real admin today.
    Skipping the live Airtable check and trusting the DB-only closure
    alone would silently force-release a genuine admin's lock the next
    time ANYONE revokes ANYTHING ELSE, anywhere in the system, while that
    admin is mid-edit — a strictly worse bug than the one this function
    exists to fix. `AirtableService.is_hub_admin` is awaited here for
    every holder the DB-only check doesn't already clear, and ANY failure
    of that call (network, rate limit, timeout) is caught and treated the
    same as "yes, admin" — never as "no, not admin" — for that holder.
    ══════════════════════════════════════════════════════════════════════
    """
    # Local imports: `app.dependencies` and `access_visibility_service` both
    # sit ABOVE this module in the dependency layering (`resolve_viewer_access`'s
    # own docstring: "a FastAPI dependency module belongs above domain
    # services, not below one") — a top-level import here would risk the
    # exact cycle `gridstack_service.py`'s own local import of this module
    # already has to dodge. See this file's own module docstring.
    from app.dependencies import CurrentHubUser, get_airtable_service, is_hub_admin
    from app.db_v2.models.hub_user import HubUserV2
    from app.models.auth import UserInfo
    from app.services.access_visibility_service import resolve_gridstack_node, resolve_viewer_access
    from app.services.rbac_graph_service import RbacClosures

    rows = _held_rows(db)

    released: list[LockNode] = []
    if not rows:
        return released

    closures = RbacClosures(db)
    airtable = get_airtable_service()
    # Caches the FINAL "may still be an admin, leave them alone" verdict —
    # true for a DB-closure admin, a live-Airtable admin, OR an unresolved
    # Airtable call (fail-safe) — never just the fast DB-only half, and
    # computed at most once per distinct holder regardless of how many
    # locks they hold.
    protected_by_hub_user_id: dict[int, bool] = {}

    for node, row in rows:
        _locked, holder, locked_at, _token = _locked_quad(row)
        if not holder or is_lock_stale(locked_at):
            # Already free in every way that matters (the ordinary silent-
            # reclaim path already covers it) — not this sweep's job.
            continue

        hub_user = db.query(HubUserV2).filter(HubUserV2.email == holder).first()
        if hub_user is None:
            _force_break_lock(row)
            released.append(node)
            continue

        if hub_user.id not in protected_by_hub_user_id:
            synthetic = CurrentHubUser(
                info=UserInfo(email=holder, name=hub_user.name or holder, roles=[]),
                hub_user_id=hub_user.id,
                email=holder,
            )
            is_db_admin = is_hub_admin(db, synthetic, closures=closures)
            if is_db_admin:
                protected = True
            else:
                try:
                    protected = await airtable.is_hub_admin(holder)
                except Exception:
                    protected = True  # uncertain -> leave them alone
            protected_by_hub_user_id[hub_user.id] = protected
        if protected_by_hub_user_id[hub_user.id]:
            continue

        # A gridstack lock node's AC node is its representation component
        # (`resolve_gridstack_node`); a component lock node IS its own AC
        # node — a real component's `("component", id)` is the same tuple
        # in both trees (`_held_rows` never returns a representation row);
        # a tab's/nav tab's is itself.
        ac_node = resolve_gridstack_node(db, row) if node[0] == "gridstack" else node
        access = resolve_viewer_access(db, hub_user.id, is_admin=False, closures=closures)
        if access.verdict(ac_node).edit:
            continue

        _force_break_lock(row)
        released.append(node)

    if released:
        db.commit()
    return released


# ---------------------------------------------------------------------------
# Derived read state — §4.3. Mirrors ViewerAccess's resolve-once-and-thread
# pattern: one `LockView` built per request, then every node's state comes
# from three small precomputed maps instead of a query per node.
# ---------------------------------------------------------------------------


class LockNodeState(NamedTuple):
    state: str  # "free" | "self" | "locked_here" | "locked_by_ancestor" | "blocked_by_descendant"
    holder: str  # effective holder; "" when free
    node_label: str  # WHICH node is actually held — this node's own for
    # self/locked_here, the ancestor's/descendant's own for the other two.
    expires_at: datetime | None


_FREE = LockNodeState(state="free", holder="", node_label="", expires_at=None)


class _ActiveLock(NamedTuple):
    node: LockNode
    holder: str
    label: str
    expires_at: datetime


class LockView(NamedTuple):
    """Built once per request via `resolve_lock_view`, never by hand — see
    that function. `viewer` is the reader's own identity (for self vs.
    foreign phrasing, not an access check: this is read-side display state,
    not a gate). `active` holds every FRESH lock in the whole system as of
    build time (there are ~240 nodes total and, in practice, a handful of
    concurrent editors at most — this is small); `ancestors_of`/
    `descendants_of` are precomputed ONLY for those few active nodes, so
    `state_for` below never touches the DB."""

    viewer: str
    active: list[_ActiveLock]
    ancestors_of: dict[LockNode, list[LockNode]]
    descendants_of: dict[LockNode, list[LockNode]]

    def state_for(self, node: LockNode) -> LockNodeState:
        # Same holder never conflicts with themself at any level (§4.1) —
        # applied here too: any active lock in `node`'s own chain (its own
        # row, an ancestor, or a descendant) held by THE VIEWER reads as
        # "self", never as "blocking me". Checked first, across all three
        # relations, before the foreign-holder branches below.
        for a in self.active:
            if a.holder != self.viewer:
                continue
            if a.node == node or node in self.descendants_of[a.node] or node in self.ancestors_of[a.node]:
                return LockNodeState(
                    state="self", holder=a.holder, node_label=a.label, expires_at=a.expires_at
                )

        for a in self.active:
            if a.node == node:
                return LockNodeState(
                    state="locked_here", holder=a.holder, node_label=a.label, expires_at=a.expires_at
                )

        for a in self.active:
            if node in self.descendants_of[a.node]:  # `a` is an ancestor of `node`
                return LockNodeState(
                    state="locked_by_ancestor", holder=a.holder, node_label=a.label, expires_at=a.expires_at
                )

        for a in self.active:
            if node in self.ancestors_of[a.node]:  # `a` is a descendant of `node`
                return LockNodeState(
                    state="blocked_by_descendant",
                    holder=a.holder,
                    node_label=a.label,
                    expires_at=a.expires_at,
                )

        return _FREE


def resolve_lock_view(db: Session, viewer: str) -> LockView:
    """Four queries — every currently-`locked == True` row across all four
    tables (`_held_rows`) — then STALE ones are dropped (a stale lock is
    "no live session" everywhere else in this design; the read side is no
    exception, even though the node's own raw `locked`/`locked_by`/
    `locked_at`/`lock_is_stale` fields keep showing the true row state
    regardless — see those fields' own docstring in schemas/tab.py)."""
    viewer = (viewer or "").strip().lower()

    rows = _held_rows(db)

    ttl = timedelta(seconds=_ttl_seconds())
    active: list[_ActiveLock] = []
    for node, row in rows:
        _locked, holder, locked_at, _token = _locked_quad(row)
        if not holder or is_lock_stale(locked_at):
            continue
        active.append(
            _ActiveLock(node=node, holder=holder, label=_label_for(node, row), expires_at=locked_at + ttl)
        )

    ancestors_of = {a.node: ancestors(db, a.node) for a in active}
    descendants_of = {a.node: descendants(db, a.node) for a in active}

    return LockView(viewer=viewer, active=active, ancestors_of=ancestors_of, descendants_of=descendants_of)


# ---------------------------------------------------------------------------
# Token enforcement — §5. Phase 3: this is the part that can break a stale
# client (decision 4, "every mutating v2 write must carry a live edit-
# session token"), which is why it ships together with phase 4's frontend
# plumbing.
# ---------------------------------------------------------------------------


class EditSession(NamedTuple):
    """The REQUEST's own object (§5.1) — NOT `LockGrant` above. Built once
    per request from the `X-Edit-Tokens` header (`app.dependencies.get_edit_session`),
    the same "resolve once, thread through services beside `access`"
    convention `ViewerAccess`/`LockView` already use. `tokens` holds every
    session the caller currently claims to be holding — in practice at most
    two (a root/ancestor session plus one surgical descendant session,
    §5.1) — so a call site never needs to know WHICH of the caller's
    sessions covers its own target; `require_live_session` below checks
    all of them."""

    holder: str
    tokens: frozenset[str]


class EditSessionError(ValueError):
    """§5.5's dedicated exception — a `ValueError` for the same reason
    `LockConflictError` is (every v2 router already does
    `except ValueError as e: raise value_error_to_http_exception(e)`), but
    carrying a `code` so a phase-3 error handler can map it to the right
    HTTP status/body without pattern-matching the message the way
    `describeTabsApiError` does today. `value_error_to_http_exception`
    itself is UNCHANGED here — see that router-level handler's own update
    for where these three codes actually become 409s."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def lock_node_of(db: Session, ac_node: tuple[str, int] | None) -> LockNode | None:
    """§5.2: maps an access-control `NodeRef` (access_visibility_service.py
    — "hub"/"nav_tab"/"tab"/"component") to its lock node. `nav_tab` and
    `tab` map straight onto themselves (already the same shape a lock node
    is). `hub` maps to `None` — the hub is never lockable (§9 item 1) — a
    caller must not call `require_live_session` at all for a hub-gated
    write, never pass this `None` through to it.

    `component` splits on `current_grid_id` (see that column's own model
    comment): a component that IS a sub-grid's own representation row maps
    to THAT sub-grid via `resolve_lock_node` — exactly mirroring how
    `resolve_gridstack_node` derives the AC node in the first place — and
    is never a lock node itself. A REAL component (`current_grid_id IS
    NULL`) maps to ITSELF, `("component", id)` (2026-09-17 decision A).
    This one branch is the pivot that reroutes every
    `_require_live_session(db, session, ("component", …))` call site —
    `update_component_content`, `update_airtable_component_config`, and
    (phase B) every SBN write — onto the component's own row with no
    per-call edits: the session chain becomes `[component, (SBN parents…),
    canvas, tab, nav_tab]`, so a narrow editor's own token on the widget
    passes, and so does an admin's canvas-edit-mode token one hop up (an
    ancestor session covers a descendant write, as it always has for
    sub-grids). Until the modal locks the widget itself (phase C) it still
    holds the canvas, and that ancestor hop is what keeps it working.

    `None` is also this function's own fail-closed answer for an orphaned
    reference (dangling component/gridstack id) — same convention as
    `resolve_gridstack_node`'s own `None`-on-orphan case; by construction
    this should never actually reach a live call site, since `require_edit`
    resolves the identical reference first and already fails closed on it.
    """
    if ac_node is None:
        return None
    kind, node_id = ac_node
    if kind in ("nav_tab", "tab"):
        return (kind, node_id)
    if kind == "hub":
        return None
    if kind == "component":
        component = db.query(ComponentV2).filter(ComponentV2.id == node_id).first()
        if component is None:
            return None
        if component.current_grid_id is None:
            return ("component", component.id)
        gridstack = db.query(GridstackV2).filter(GridstackV2.id == component.current_grid_id).first()
        if gridstack is None:
            return None
        return resolve_lock_node(gridstack)
    raise ValueError(f"Unknown AC node kind: {kind!r}")


def require_live_session(
    session: EditSession | None, node: LockNode, db: Session, *, self_only: bool = False
) -> datetime | None:
    """§5.2's gate. Passes iff some token in `session.tokens` matches a
    lock row where ALL of: the row is `node` itself OR an ancestor of it
    (the session covers `node`'s subtree); the row's `locked_by ==
    session.holder` (a leaked token is useless to another identity); the
    row is not stale.

    `self_only=True` (the `renew` door below, 2026-09-16 TTL fix) narrows
    the chain to `node`'s OWN row — no ancestor walk. A write gate wants
    "does ANY session I hold cover this node" (an ancestor session
    legitimately covers a descendant write); a session RENEWAL wants "is
    THIS specific session still live" — letting a fresh root session
    vouch for a stale sub-grid session would silently mark the sub-grid
    session renewed on the client while its own row stayed stale, which
    is exactly the kind of client/server disagreement the renew door
    exists to eliminate.

    Returns the renewed row's new `locked_at` on success (so a caller can
    derive `expires_at` without re-reading the row); `None` only for the
    `session=None` no-check case. Every existing write-gate caller ignores
    the return value — additive.

    `session=None` is "no check was requested" — the SAME convention
    `require_edit`'s `access=None` already uses (access_visibility_service.py)
    for an internal caller that never intended to gate this call (e.g. one
    service function seeding content via another's own write path). Every
    real v2 router passes an actual `EditSession` from
    `Depends(get_edit_session)`; this is NOT a bypass reachable from
    outside. `node` itself stays REQUIRED (not optional) — the hub-exempt
    call sites (§9 item 1: create_nav_tab_v2, reorder_nav_tabs_v2) skip
    calling this function AT ALL rather than passing a None node, so a
    None ever reaching here is a real bug to fail loudly on, not a case to
    silently wave through.

    **Placement rule, mechanical (§5.2): call this immediately after every
    `require_edit(access, X)`, as `require_live_session(session,
    lock_node_of(X), db)` — auth first (403), then session (409), so a
    caller with no edit grant never learns whether the node happens to be
    locked.** `lock_node_of` is `resolve_lock_node` for a representation
    row's gridstack, or the AC node's own kind/id unchanged for a
    tab/nav_tab/real component (they're already the same shape a lock node
    is — no gridstack indirection to resolve).

    ON SUCCESS, renews the matched row's `locked_at` (decision 5: "TTL is
    renewed by saving, not by a heartbeat") — sets the attribute only, no
    `db.commit()` of its own, so the renewal rides along with whatever
    commit the enclosing service function already does at the end of the
    validated write it is gating. No separate renewal endpoint or client
    timer traffic is needed.

    NO TOKENS SENT AT ALL is always EDIT_SESSION_MISSING immediately, no
    chain walk — this is what makes MISSING mean specifically "you never
    presented a token for this," as distinct from "you presented one and
    it just doesn't work" (EXPIRED/TAKEN_OVER below). Checking a non-empty
    `session.tokens` against the chain any other way could not
    tell "the client forgot to send its still-good token" apart from "the
    client sent a token that's since gone bad" — both look identical from
    inside the loop — so this case is carved out structurally instead.

    Otherwise, on failure, walks the chain again to name the specific
    reason (§5.5) for a row that's genuinely this holder's own
    (`locked_by` matches — checked WITHOUT also requiring `locked=True`
    here: `_force_break_lock` deliberately leaves `locked_by` in place on
    a row it frees, exactly so this still finds it): its token being among
    `session.tokens` but not validating above means it went stale —
    EDIT_SESSION_EXPIRED; its token NOT being present means it was rotated
    out from under them — EDIT_SESSION_TAKEN_OVER. A NULL-token row
    (pre-migration, §3.2 — "no backfill") is skipped entirely in this
    second pass, never attributed to anyone: it was never a live session
    to take over or expire, so falling through to EDIT_SESSION_MISSING for
    it is the correct fail-closed answer, matching `lock_token`'s own
    "NULL validates as no live session" convention. A force-TAKEN-OVER
    node's OWN row (as opposed to its descendants) is NOT
    distinguishable this way — `acquire`'s own `_write_lock` legitimately
    overwrites `locked_by` to the NEW holder there (decision 2 allows only
    one row per node), so the ousted holder's next write on THAT exact
    node resolves to MISSING, not TAKEN_OVER. Accepted asymmetry: both
    codes drive the identical "re-acquire and retry" flow client-side
    (§6.3), so the distinction is cosmetic wording, not behaviour — and
    only the descendant case can preserve it for free.
    """
    if session is None:
        return None

    if not session.tokens:
        raise EditSessionError(
            "This action requires an active editing session.", code="EDIT_SESSION_MISSING"
        )

    chain = [node] if self_only else [node] + ancestors(db, node)

    for candidate in chain:
        row = _node_row(db, candidate)
        if row is None:
            continue
        locked, locked_by, locked_at, token = _locked_quad(row)
        if not locked or not token or token not in session.tokens:
            continue
        if locked_by != session.holder:
            continue
        if is_lock_stale(locked_at):
            continue
        renewed_at = _utc_now()
        row.locked_at = renewed_at  # decision 5: renewal is a side effect of THIS write
        return renewed_at  # a live, matching, self-held session covers `node`

    for candidate in chain:
        row = _node_row(db, candidate)
        if row is None:
            continue
        _locked, locked_by, _locked_at, token = _locked_quad(row)
        if locked_by != session.holder or token is None:
            continue
        label = _label_for(candidate, row)
        if token in session.tokens:
            raise EditSessionError(
                f'Your editing session on "{label}" has expired.', code="EDIT_SESSION_EXPIRED"
            )
        raise EditSessionError(
            f'Your editing session on "{label}" was ended — someone took over.',
            code="EDIT_SESSION_TAKEN_OVER",
        )

    raise EditSessionError(
        "This action requires an active editing session.", code="EDIT_SESSION_MISSING"
    )


def renew(db: Session, session: EditSession, node: LockNode) -> LockGrant:
    """The save-preflight's VALIDATE door (plan §6.2 "validate/renew the
    session once, up front") — added 2026-09-16 after a live TTL test
    showed the preflight could never actually fail on expiry.

    WHY THIS IS NOT `acquire`. The frontend's preflight used to call the
    plain lock endpoint (`acquire`) on every held session. `acquire`
    treats a same-holder re-entry as never conflicting, FRESH OR STALE
    (§4.1 — deliberately, so a holder can reclaim their own lock after a
    crash) — it keeps the token and stamps `locked_at = now`. Used as a
    preflight, that silently RESURRECTED an expired session moments
    before the content write reached `require_live_session`, so
    `EDIT_SESSION_EXPIRED` was unreachable through a normal save and
    decision 7's "expired save keeps local changes and offers re-acquire
    & retry" modal never fired. This door only ever VALIDATES: it passes
    (and renews, per decision 5) iff `node`'s OWN row is a fresh,
    self-held session whose token the caller presented; otherwise it
    raises the same `EditSessionError` codes every write gate raises —
    and it never mints, reclaims, or touches any row on failure. The
    client's "Re-acquire & Retry" still goes through `acquire`, which is
    exactly where the reclaim belongs: an explicit user choice, not a
    side effect of pressing Save.

    `self_only=True`: see `require_live_session`'s own note — a session
    renewal is about THIS row, never about an ancestor that happens to
    cover it."""
    try:
        renewed_at = require_live_session(session, node, db, self_only=True)
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = _node_row(db, node)
    # `renewed_at` is non-None here: `session` is never None on this path
    # (the router always passes a real `EditSession`), and every failure
    # branch raised above.
    assert renewed_at is not None and row is not None
    return LockGrant(
        token=row.lock_token or "",
        holder=session.holder,
        node=node,
        expires_at=renewed_at + timedelta(seconds=_ttl_seconds()),
    )


def refuse_if_any_held(db: Session, nodes: list[LockNode], holder: str) -> None:
    """§5.4, decision 9 — reorder's session-EXEMPT check: no token
    required, but refused (`LockConflictError`, the same lock-conflict
    `ValueError` family every existing acquire refusal already raises, so
    reorder's failure mode needs no new client handling) if ANY node in
    `nodes` is held FRESH by someone else. A stale hold never refuses
    (claimable, same as everywhere else); the holder's OWN hold never
    refuses either — they may reorder their own held set. Checks each
    node's OWN row only, not its ancestors/descendants — reorder cares
    whether the SPECIFIC items being reordered are mid-edit right now, not
    whether something nested under one of them happens to be."""
    conflicts: list[BlockingHolder] = []
    for node in nodes:
        row = _node_row(db, node)
        if row is None:
            continue
        locked, locked_by, locked_at, _token = _locked_quad(row)
        if locked and locked_by and locked_by != holder and not is_lock_stale(locked_at):
            conflicts.append(BlockingHolder(locked_by, _label_for(node, row), "self"))

    if conflicts:
        raise LockConflictError(_conflict_message(conflicts), blocking=conflicts)
