"""Edit-lock propagation — plan_lock_propagation_2026-09-08.md.

THE LOCK TREE IS NOT THE ACCESS-CONTROL TREE (plan §2). The two look
similar enough to be conflated, so this module is deliberately separate from
`access_visibility_service.build_node_tree` / `NodeTree.ancestors()` /
`NodeTree.descendants()` rather than reusing them:

  | | AC tree | Lock tree |
  |---|---|---|
  | sub-grid | not a node (transparent; represented by a component) | a node — GridstackV2 owns the lock columns |
  | component | a node (grants live there) | not a node — checks ancestors, takes no lock |
  | hub | the root | not lockable at all |

So a `LockNode` has exactly three kinds — "nav_tab", "tab", "gridstack" — and
NO "component" or "hub" case, the mirror image of `NodeRef`
(access_visibility_service.py) which has "hub"/"nav_tab"/"tab"/"component"
and no "gridstack".

This phase (§8 phase 1) only builds the tree resolver — `resolve_lock_node`,
`ancestors`, `descendants` — with no behaviour change: nothing here is
called by `lock_tab_by_document_id_v2` / `unlock_tab_by_document_id_v2` yet.
Acquire/release move onto this tree in phase 2.
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

_TabOrGridstackOrNavTab = Union[TabV2, GridstackV2, NavTabV2]

# ("nav_tab" | "tab" | "gridstack", id). Deliberately the same shape as
# access_visibility_service.NodeRef (a 2-tuple of kind + id) — same
# convention, different domain; the kind strings do not overlap ("gridstack"
# has no AC-tree counterpart, "component"/"hub" have no lock-tree one), so a
# LockNode and a NodeRef are never confusable in practice even though both
# are typed as plain tuples.
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
    a single denormalized column read — no recursive walk, because the tree
    is fixed-depth: gridstack -> tab -> [tab ->] nav_tab, at most three hops
    even through a variant.

    A missing row (id no longer exists) yields an empty remaining chain
    rather than raising — this mirrors every other "fail closed, not open"
    convention in this codebase (access_visibility_service.py §3.3): a
    caller checking "is any ancestor held" against a dangling reference
    should see no ancestors, not an exception, since there is nothing left
    above it to be held.
    """
    kind, node_id = node

    if kind == "nav_tab":
        return []

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


def descendants(db: Session, node: LockNode) -> list[LockNode]:
    """Every node in `node`'s subtree, in no particular order. 2-3 flat
    queries, never recursive — sub-grids never nest (a permanent tree-shape
    rule, confirmed by the owner 2026-09-07), so a gridstack is always a
    leaf and a tab's own descendants are always exactly one or two levels
    down.

    THE §1.1 HOLE THIS CLOSES: a root tab's descendants include not just its
    own sub-grids but every VARIANT's sub-grids too. Today's
    `_cascade_lock_to_nested_gridstacks` only reaches the root's own
    `parent_tab_id`-matched gridstacks — a variant is a wholly separate
    `TabV2` row the cascade never touches, so locking a root currently
    leaves its variants (and their sub-grids) unlocked. This resolver is
    what makes `edit_lock_service.acquire` (phase 2) see the whole subtree.
    """
    kind, node_id = node

    if kind == "gridstack":
        return []

    if kind == "tab":
        tab = db.query(TabV2).filter(TabV2.id == node_id).first()
        if tab is None:
            return []

        if tab.parent_tab_id is not None:
            # A variant: its own sub-grids only. Variants can never
            # themselves have variants (enforced in gridstack_service.py),
            # so there is no further "variant of a variant" branch here.
            subgrids = (
                db.query(GridstackV2.id)
                .filter(GridstackV2.parent_tab_id == node_id, GridstackV2.parent_id.isnot(None))
                .all()
            )
            return [("gridstack", sg.id) for sg in subgrids]

        # A root: its variants, its own sub-grids, AND every variant's
        # sub-grids (the §1.1 hole).
        variants = db.query(TabV2.id).filter(TabV2.parent_tab_id == node_id).all()
        variant_ids = [v.id for v in variants]

        owning_tab_ids = [node_id] + variant_ids
        subgrids = (
            db.query(GridstackV2.id)
            .filter(
                GridstackV2.parent_tab_id.in_(owning_tab_ids),
                GridstackV2.parent_id.isnot(None),
            )
            .all()
        )

        result: list[LockNode] = [("tab", v.id) for v in variants]
        result.extend(("gridstack", sg.id) for sg in subgrids)
        return result

    if kind == "nav_tab":
        # Every TabV2 under this nav tab — roots AND variants
        # (create_tab_variant_v2 copies the parent's nav_tab_id, so a
        # variant is just as much "under" the nav tab as its root is) —
        # plus every sub-grid under any of them.
        tabs = db.query(TabV2.id).filter(TabV2.nav_tab_id == node_id).all()
        tab_ids = [t.id for t in tabs]

        subgrids = (
            db.query(GridstackV2.id)
            .filter(GridstackV2.parent_tab_id.in_(tab_ids), GridstackV2.parent_id.isnot(None))
            .all()
            if tab_ids
            else []
        )

        result = [("tab", t.id) for t in tabs]
        result.extend(("gridstack", sg.id) for sg in subgrids)
        return result

    raise ValueError(f"Unknown lock node kind: {kind!r}")


# ---------------------------------------------------------------------------
# Row access — one place that knows how to read/write the lock quadruple
# regardless of which of the three tables `node` addresses.
# ---------------------------------------------------------------------------


def _node_row(db: Session, node: LockNode) -> _TabOrGridstackOrNavTab | None:
    kind, node_id = node
    if kind == "tab":
        return db.query(TabV2).filter(TabV2.id == node_id).first()
    if kind == "gridstack":
        return db.query(GridstackV2).filter(GridstackV2.id == node_id).first()
    if kind == "nav_tab":
        return db.query(NavTabV2).filter(NavTabV2.id == node_id).first()
    raise ValueError(f"Unknown lock node kind: {kind!r}")


def _locked_quad(row: _TabOrGridstackOrNavTab) -> tuple[bool, str, datetime | None, str | None]:
    return bool(row.locked), (row.locked_by or ""), row.locked_at, row.lock_token


def _label_for(node: LockNode, row: _TabOrGridstackOrNavTab) -> str:
    """A sub-grid's human name lives on `GridstackV2.name`; a tab's or nav
    tab's on `.title` — same split `_format_tab_summary` already reads."""
    kind, _ = node
    if kind == "gridstack":
        return row.name or ""
    return row.title or ""


def _clear_lock(row: _TabOrGridstackOrNavTab) -> None:
    row.locked = False
    row.locked_by = ""
    row.locked_at = None
    row.lock_token = None


def _write_lock(row: _TabOrGridstackOrNavTab, holder: str, now: datetime, token: str) -> None:
    row.locked = True
    row.locked_by = holder
    row.locked_at = now
    row.lock_token = token


def _force_break_lock(row: _TabOrGridstackOrNavTab) -> None:
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
        descendant_rows: list[tuple[LockNode, _TabOrGridstackOrNavTab]] = []
        for desc in descendants(db, node):
            desc_row = _node_row(db, desc)
            if desc_row is None:
                continue
            descendant_rows.append((desc, desc_row))
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
    """Three queries — every currently-`locked == True` row across all
    three tables — then STALE ones are dropped (a stale lock is "no live
    session" everywhere else in this design; the read side is no
    exception, even though the node's own raw `locked`/`locked_by`/
    `locked_at`/`lock_is_stale` fields keep showing the true row state
    regardless — see those fields' own docstring in schemas/tab.py)."""
    viewer = (viewer or "").strip().lower()

    rows: list[tuple[LockNode, _TabOrGridstackOrNavTab]] = [
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
    write, never pass this `None` through to it. `component` is the one
    real translation: a component that IS a sub-grid's own representation
    row (`current_grid_id` set — see that column's own model comment) maps
    to THAT sub-grid; an ordinary widget component maps to the canvas it
    lives on (`gridstack_id`) — either way, `resolve_lock_node` on the
    resolved gridstack gives the answer, exactly mirroring how
    `resolve_gridstack_node` derives the AC node in the first place.

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
        owning_gridstack_id = (
            component.current_grid_id if component.current_grid_id is not None else component.gridstack_id
        )
        gridstack = db.query(GridstackV2).filter(GridstackV2.id == owning_gridstack_id).first()
        if gridstack is None:
            return None
        return resolve_lock_node(gridstack)
    raise ValueError(f"Unknown AC node kind: {kind!r}")


def require_live_session(session: EditSession | None, node: LockNode, db: Session) -> None:
    """§5.2's gate. Passes iff some token in `session.tokens` matches a
    lock row where ALL of: the row is `node` itself OR an ancestor of it
    (the session covers `node`'s subtree); the row's `locked_by ==
    session.holder` (a leaked token is useless to another identity); the
    row is not stale.

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
    locked.** `lock_node_of` is `resolve_lock_node` for a gridstack, or the
    AC node's own kind/id unchanged for a tab/nav_tab (they're already the
    same shape a lock node is — no gridstack indirection to resolve).

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
        return

    if not session.tokens:
        raise EditSessionError(
            "This action requires an active editing session.", code="EDIT_SESSION_MISSING"
        )

    chain = [node] + ancestors(db, node)

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
        row.locked_at = _utc_now()  # decision 5: renewal is a side effect of THIS write
        return  # a live, matching, self-held session covers `node`

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
