"""Batch lock-state read backing the frontend's lock-badge poll
(`POST /v2/locks/state`).

Every list/workspace endpoint already stamps the derived lock block
(`lock_state`/`lock_holder`/... — plan_lock_propagation_2026-09-08.md §4.3)
onto each row it returns, but the frontend fetches those lists once and
never again, so a badge shows whatever was true at load time: another
user's lock/unlock, a takeover, or a TTL expiry stays invisible until a
hard refresh. Re-polling the lists themselves would be wrong twice over —
each carries far more than lock state (content flags, access blobs; each
one a Neon round-trip), and merging whole rows back would clobber what the
user is editing. This returns ONLY the lock block, for exactly the ids the
client currently shows, so the client can patch those fields in place.

Same answers as the list endpoints by construction: the derived half comes
from the request's one `LockView` (`get_lock_view`), the raw half is read
off the same columns `_format_tab_summary` / `_format_nav_tab` /
`_sbn_lock_fields` read, and an id is reported only when
`access.verdict(node).view` — the SAME chrome gate every list endpoint
filters rows on — so this can never reveal a lock holder (or a
`lock_holder_node_label`) on a node the caller could not already list.

Batched rather than looping the per-node helpers: ~165ms per Neon
round-trip (see `_ROOT_TAB_NOT_FETCHED`'s comment in gridstack_service.py),
and this runs every 15 seconds for every open dashboard. A poll is a fixed
handful of `IN (...)` queries regardless of how many ids it names, on top of
`get_viewer_access` / `get_lock_view`'s own per-request cost.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.tab import TabV2
from app.services import edit_lock_service
from app.services.access_visibility_service import NodeRef, ViewerAccess, resolve_gridstack_node
from app.services.gridstack_service import MAX_DOCUMENT_ID_LENGTH, _safe_locked_triple, is_lock_stale
from app.services.nav_tab_service import _format_nav_tab
from app.services.super_blocknote_service import _is_sbn_member, _sbn_lock_fields

# The lock block, and only the lock block — `LockStateResponse`'s fields.
LOCK_FIELDS = (
    "locked",
    "locked_by",
    "locked_at",
    "lock_is_stale",
    "lock_state",
    "lock_holder",
    "lock_holder_node_label",
    "lock_expires_at",
    "lock_takeover_blocked",
)


def _clean_ids(ids: Iterable[str]) -> list[str]:
    """Stripped, de-duplicated, order-preserving. An id no real row could
    have (empty, over-long) is dropped rather than rejected — it simply
    matches nothing, the same outcome as an id that does not exist."""
    seen: dict[str, None] = {}
    for raw in ids:
        clean = raw.strip()
        if clean and len(clean) <= MAX_DOCUMENT_ID_LENGTH:
            seen.setdefault(clean, None)
    return list(seen)


def _derived(lock_view: edit_lock_service.LockView, node: edit_lock_service.LockNode) -> dict[str, Any]:
    state = lock_view.state_for(node)
    return {
        "lock_state": state.state,
        "lock_holder": state.holder,
        "lock_holder_node_label": state.node_label,
        "lock_expires_at": state.expires_at,
        "lock_takeover_blocked": state.takeover_blocked,
    }


def _nav_tab_states(
    db: Session, ids: list[str], access: ViewerAccess, lock_view: edit_lock_service.LockView
) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for nav_tab in db.query(NavTabV2).filter(NavTabV2.document_id.in_(ids)).all():
        if not access.verdict(("nav_tab", nav_tab.id)).view:
            continue
        # `_format_nav_tab` is query-free, so reusing it whole costs nothing
        # and keeps the raw/derived split identical to GET /v2/nav-tabs.
        summary = _format_nav_tab(nav_tab, lock_view=lock_view)
        out[nav_tab.document_id] = {key: summary[key] for key in LOCK_FIELDS}
    return out


def _tab_states(
    db: Session, ids: list[str], access: ViewerAccess, lock_view: edit_lock_service.LockView
) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    gridstacks = db.query(GridstackV2).filter(GridstackV2.document_id.in_(ids)).all()

    # The two per-gridstack lookups `_format_tab_summary` makes — a root's
    # owning TabV2 row (its lock columns) and a sub-grid's representation
    # component (its AC node) — prefetched in one query each.
    root_tab_ids = {g.parent_tab_id for g in gridstacks if g.parent_id is None and g.parent_tab_id is not None}
    root_tabs = (
        {t.id: t for t in db.query(TabV2).filter(TabV2.id.in_(root_tab_ids)).all()} if root_tab_ids else {}
    )
    sub_grid_ids = [g.id for g in gridstacks if g.parent_id is not None]
    representation_of: dict[int, int] = {}
    if sub_grid_ids:
        rows = (
            db.query(ComponentV2.current_grid_id, ComponentV2.id)
            .filter(ComponentV2.current_grid_id.in_(sub_grid_ids))
            .order_by(ComponentV2.id)
            .all()
        )
        for grid_id, component_id in rows:
            # First by id, matching `resolve_gridstack_node`'s `.first()`.
            representation_of.setdefault(grid_id, component_id)

    out: dict[str, dict[str, Any]] = {}
    for gridstack in gridstacks:
        if gridstack.parent_id is None:
            # Query-free for a root gridstack.
            ac_node: NodeRef | None = resolve_gridstack_node(db, gridstack)
            root_tab = root_tabs.get(gridstack.parent_tab_id)
        else:
            rep = representation_of.get(gridstack.id)
            ac_node = ("component", rep) if rep is not None else None
            root_tab = None  # unused for a sub-grid
        if not access.verdict(ac_node).view:
            continue

        locked, locked_by, locked_at = _safe_locked_triple(db, gridstack, root_tab)
        out[gridstack.document_id] = {
            "locked": locked,
            "locked_by": locked_by,
            "locked_at": locked_at,
            "lock_is_stale": locked and is_lock_stale(locked_at),
            **_derived(lock_view, edit_lock_service.resolve_lock_node(gridstack)),
        }
    return out


def _sbn_states(
    db: Session, links: list[str], access: ViewerAccess, lock_view: edit_lock_service.LockView
) -> dict[str, dict[str, Any]]:
    if not links:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for component in db.query(ComponentV2).filter(ComponentV2.link.in_(links)).all():
        if not _is_sbn_member(component):
            continue
        if not access.verdict(("component", component.id)).view:
            continue
        out[component.link] = _sbn_lock_fields(component, lock_view)
    return out


def get_lock_states(
    db: Session,
    *,
    nav_tabs: Iterable[str] = (),
    tabs: Iterable[str] = (),
    sbn: Iterable[str] = (),
    access: ViewerAccess,
    lock_view: edit_lock_service.LockView,
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "nav_tabs": _nav_tab_states(db, _clean_ids(nav_tabs), access, lock_view),
        "tabs": _tab_states(db, _clean_ids(tabs), access, lock_view),
        "sbn": _sbn_states(db, _clean_ids(sbn), access, lock_view),
    }
