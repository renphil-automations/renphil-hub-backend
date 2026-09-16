"""
Super Block Note (SBN) v2 service layer: CRUD over a Super Block Note
widget's own nested "sub-tab" tree, which lives as ordinary `ComponentV2`
rows tagged with `super_blocknote_id` (self-referential FK — see
ComponentV2's docstring), NOT as separate `GridstackV2`/`TabV2` rows.

Every function here returns `TabSummaryResponse`/`TabWorkspaceResponse`/
`PageContentAPIResponse`-shaped plain dicts — the exact same shapes
`gridstack_service.py` and `tab_service.py` already return — so the router
can reuse those existing Pydantic schemas unchanged, and the frontend's
`SuperBlockNoteWidget.tsx` needs no new data model, only a new set of
client functions pointed at the new `/v2/sbn/...` endpoints (mirroring how
the rest of v2 was designed to be a drop-in translation layer).

An SBN node is addressed by its own `ComponentV2.link` (never its raw `id`
or its transient `layout`/`widgets` canvas key), same addressing convention
as everything else in this schema.

ACCESS CONTROL (plan_access_control_algorithm_2026-08-27.md), added
2026-09-09. Until then this whole family was authenticated-only: the router
carried `Depends(get_current_user)` and nothing else, so any signed-in
caller could read, write, create, delete, reorder and lock any SBN node by
its `link` — bypassing the fold that gates the tab it lives on. That was
not an oversight in the algorithm, which has always counted SBN sub-tabs as
§3.1 nodes (`access_visibility_service._component_parent`'s branch 1 walks
`super_blocknote_id` and calls the nesting "unbounded by design"); it was
simply the one v2 surface the wiring sessions never reached.

Every gate here is the SAME `ViewerAccess` / `require_edit` pair
`gridstack_service.py` already uses, threaded the same way — `access:
ViewerAccess | None = None`, where `None` means "no check requested" (an
internal caller), never "deny". An SBN node's `NodeRef` is always
`("component", id)`, and its parent is `("component", super_blocknote_id)`
for every node except the SBN root itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.page_content import PageContentV2
from app.services.access_visibility_service import NodeRef, ViewerAccess, require_edit
from app.services.gridstack_service import (
    _generate_id,
    _resolve_component_data,
    _utc_now,
    _validate_document_id_value,
    _validate_locked_by,
    _validate_order,
    _validate_title,
    _write_component_data,
    is_lock_stale,
)

SBN_ROOT_TYPE = "super_block_note"
SBN_LEAF_TYPE = "block_note"
# Title of the auto-managed leaf that holds a Super Block Note root's own
# blocknote content once it's moved off the (never-rendered) root — created
# either when the root gains its first real child (create_sbn_node) or when
# content is saved to a root that has no sub-tabs yet (update_sbn_content).
OVERVIEW_TITLE = "Overview"


def get_component_by_link(db: Session, link: str) -> ComponentV2 | None:
    link = _validate_document_id_value(link, "link")
    return db.query(ComponentV2).filter(ComponentV2.link == link).first()


def _is_sbn_member(component: ComponentV2) -> bool:
    """True for the SBN root itself (the widget's own top-level component
    row), or any node reached via `super_blocknote_id` — i.e. every node
    that's part of some Super Block Note's own internally-managed tree."""
    return component.type == SBN_ROOT_TYPE or component.super_blocknote_id is not None


def _sbn_node(component: ComponentV2) -> NodeRef:
    """This node's own `NodeRef` — plan §6.3's `n`.

    Always `("component", id)`: all three of an SBN tree's row kinds (the
    root widget, a nested container, a leaf) are `ComponentV2` rows, so
    unlike `resolve_gridstack_node` there is no root-vs-sub-grid case
    analysis to do here."""
    return ("component", component.id)


def _sbn_parent_node(component: ComponentV2) -> NodeRef | None:
    """Plan §6.3's `parent(n)` — the node that delete and reorder gate on,
    because those change the PARENT's contents rather than the node's own.

    `None` for an SBN root (`super_blocknote_id is None`), whose real parent
    is the canvas its widget sits on rather than another component. Callers
    must treat that `None` as "cannot resolve — fail closed", which
    `require_edit` already does by construction (`ViewerAccess.verdict(None)`
    returns `INVISIBLE` for everyone but a Hub Admin), matching
    `resolve_gridstack_parent_node`'s identical convention (§3.3). No caller
    here needs the canvas answer: the only parent-gated operation that can
    reach a root is `delete_sbn_subtree`, which refuses to delete a root at
    all and gates on `edit(n)` instead — see its own comment."""
    if component.super_blocknote_id is None:
        return None
    return ("component", component.super_blocknote_id)


def _sbn_props(component: ComponentV2) -> dict[str, Any]:
    return component.props or {}


def _sbn_locked_at(props: dict[str, Any]) -> datetime | None:
    """`props["locked_at"]` is an ISO-8601 string (JSONB has no native
    datetime type), written by `_utc_now().isoformat()` in `lock_sbn_node`
    below. Missing OR unparseable both return None — which `is_lock_stale`
    (gridstack_service.py) already treats as stale, the same "no usable
    timestamp ⇒ assume stale, never assume fresh" convention
    `airtable_service._envelope_age_seconds` uses for cache envelopes. Every
    node locked before plan §6.6 Fix 2 landed has no `locked_at` key at all
    and lands here."""
    stamp = props.get("locked_at")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _has_sbn_children(db: Session, component_id: int) -> bool:
    return (
        db.query(ComponentV2.id)
        .filter(ComponentV2.super_blocknote_id == component_id)
        .first()
        is not None
    )


def _format_sbn_summary(
    db: Session, component: ComponentV2, *, access: ViewerAccess | None = None
) -> dict[str, Any]:
    props = _sbn_props(component)
    locked = bool(props.get("locked", False))
    locked_at = _sbn_locked_at(props)
    node = _sbn_node(component)
    summary = {
        "id": component.id,
        "documentId": component.link,
        "title": component.title,
        "order": props.get("order", 0),
        "locked": locked,
        "locked_by": props.get("locked_by", "") or "",
        "locked_at": locked_at,
        "lock_is_stale": locked and is_lock_stale(locked_at),
        "has_children": _has_sbn_children(db, component.id),
        "has_content": component.page_content_id is not None,
        "apiVersion": "v2",
        # Resolved unconditionally, exactly like `_format_tab_summary`'s
        # identical pair: every summary — mutation-response echoes included —
        # names the real `resource_grants` node, whether or not a caller
        # gates on it.
        "node_kind": node[0],
        "node_id": node[1],
    }
    # §5.2's triple, added only when a caller passes `access` — the same
    # convention `_format_tab_summary` uses, and the reason the frontend
    # needs no change to consume this: `DashboardV2Page.canViewTab` already
    # prefers a non-null `view` over its legacy `access_control` fallback,
    # and its doc comment named THIS surface as one of the three cases the
    # fallback existed for.
    if access is not None:
        verdict = access.verdict(node)
        summary["view"] = verdict.view
        summary["edit"] = verdict.edit
        summary["revealed"] = verdict.revealed
        summary["edit_seed"] = verdict.edit_seed
    return summary


def get_sbn_children(
    db: Session, link: str, *, access: ViewerAccess | None = None
) -> list[dict[str, Any]] | None:
    component = get_component_by_link(db, link)
    if component is None or not _is_sbn_member(component):
        return None

    # Same fail-closed convention as `get_tab_children_v2`: a caller who
    # cannot see this node does not get to learn what is under it, and
    # "invisible" is indistinguishable from "does not exist" (§9) because
    # both return None and the router maps that to one 404.
    if access is not None and not access.verdict(_sbn_node(component)).view:
        return None

    children = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == component.id).all()
    summaries = [_format_sbn_summary(db, c, access=access) for c in children]
    # §5.2: `visible` gates the CHROME — an invisible sub-tab must not appear
    # in the SBN's own tab rail at all, not merely render disabled.
    if access is not None:
        summaries = [s for s in summaries if s["view"]]
    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def get_sbn_workspace(
    db: Session, link: str, *, access: ViewerAccess | None = None
) -> dict[str, Any] | None:
    component = get_component_by_link(db, link)
    if component is None or not _is_sbn_member(component):
        return None

    node = _sbn_node(component)
    own_verdict = None
    if access is not None:
        own_verdict = access.verdict(node)
        if not own_verdict.view:
            return None

    props = _sbn_props(component)

    parent = None
    if component.super_blocknote_id is not None:
        parent_component = (
            db.query(ComponentV2).filter(ComponentV2.id == component.super_blocknote_id).first()
        )
        if parent_component is not None:
            parent_props = _sbn_props(parent_component)
            parent = {
                "id": parent_component.id,
                "documentId": parent_component.link,
                "title": parent_component.title,
                "order": parent_props.get("order", 0),
            }
    # else: component IS the SBN root — no parent within its own tree.

    children = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == component.id).all()
    child_summaries = [_format_sbn_summary(db, c, access=access) for c in children]
    if access is not None:
        child_summaries = [s for s in child_summaries if s["view"]]
    child_summaries.sort(key=lambda s: (s["order"], s["id"] or 0))

    locked = bool(props.get("locked", False))
    locked_at = _sbn_locked_at(props)

    workspace = {
        "id": component.id,
        "documentId": component.link,
        "title": component.title,
        "order": props.get("order", 0),
        "parent": parent,
        "page_content": {
            "documentId": component.link,
            "content": _sbn_content_for(db, component, access=access),
        },
        # NULL means "no access_control set" -- pass it through as NULL
        # rather than manufacturing DEFAULT_ACCESS_CONTROL. Load-bearing:
        # the SBN child filter's only consumer is canViewTab (frontend),
        # which must treat an absent access_control as viewable.
        "access_control": component.access_control,
        "locked": locked,
        "locked_by": props.get("locked_by", "") or "",
        "locked_at": locked_at,
        "lock_is_stale": locked and is_lock_stale(locked_at),
        "children": child_summaries,
        "apiVersion": "v2",
        "node_kind": node[0],
        "node_id": node[1],
    }
    if own_verdict is not None:
        workspace["view"] = own_verdict.view
        workspace["edit"] = own_verdict.edit
        workspace["revealed"] = own_verdict.revealed
        workspace["edit_seed"] = own_verdict.edit_seed
    return workspace


def _sbn_content_for(
    db: Session, component: ComponentV2, *, access: ViewerAccess | None
) -> Any:
    """This node's own BlockNote blocks, or `None` when the caller may see
    the node but was not GRANTED it — §5.2's split between the two
    permissions, applied to the payload this surface actually carries.

    `granted` gates the payload, `visible` gates the chrome, and §5.2 names
    "BlockNote text" in the payload list explicitly. So a REVEALED SBN node
    — one the caller reaches only because something in its subtree is
    granted — renders as the shell §5.2 describes: its title and its
    visible children come back so the caller can navigate through to the
    child that earned the reveal, and its own text does not.

    `None` rather than `[]` or a sentinel, because `None` is already what
    this field carries for a node that simply has no content yet (a
    freshly-created sub-tab, or an SBN root whose content was transplanted
    into an "Overview" leaf), so every existing client path renders it
    correctly with no change. This deliberately differs from
    `gridstack_service._apply_visibility_to_content`, whose per-widget
    `restricted` sentinel exists because a CANVAS must keep the widget's key
    to survive a round trip through the canvas save (§10 item 7); a
    BlockNote doc has no such keyed structure and no equivalent save hazard —
    `update_sbn_content` writes what it is given and is itself gated on
    `edit(n)`, which a revealed caller does not have."""
    if access is not None and not access.is_granted(_sbn_node(component)):
        return None
    return _resolve_component_data(db, component).get("content")


def get_sbn_content(
    db: Session, link: str, *, access: ViewerAccess | None = None
) -> dict[str, Any] | None:
    component = get_component_by_link(db, link)
    if component is None or not _is_sbn_member(component):
        return None
    if access is not None and not access.verdict(_sbn_node(component)).view:
        return None
    return {
        "documentId": component.link,
        "content": _sbn_content_for(db, component, access=access),
    }


def _create_overview_leaf(
    db: Session, root: ComponentV2, content: Any, order: int = 0
) -> ComponentV2:
    """Create the auto-managed "Overview" leaf under an SBN root and store
    `content` in it. The root is a pure container from then on; the leaf is a
    fully normal, renameable / deletable / reorderable sub-tab. Used from two
    call sites — the first-child transplant (`create_sbn_node`) and the
    save-with-no-sub-tabs redirect (`update_sbn_content`)."""
    leaf = ComponentV2(
        link=_generate_id(),
        type=SBN_LEAF_TYPE,
        title=OVERVIEW_TITLE,
        props={"locked": False, "locked_by": "", "order": order},
        # NULL through: a leaf under a NULL root is itself NULL (§5.2),
        # not a manufactured default -- it inherits the same way the root
        # itself does.
        access_control=root.access_control,
        x=0,
        y=0,
        width=6,
        height=6,
        gridstack_id=root.gridstack_id,
        super_blocknote_id=root.id,
        page_content_id=None,
        current_grid_id=None,
    )
    db.add(leaf)
    db.flush()
    # A leaf (type="block_note") unwraps `data["content"]` before storing the
    # bare list — re-write through its own convention rather than transplanting
    # a raw page_content_id pointer, which would double-wrap the block list.
    _write_component_data(db, leaf, {"content": content})
    return leaf


def _drop_root_content(db: Session, root: ComponentV2) -> None:
    """Clear a root's own directly-held content, making it a pure container.
    Its content has just been (or is being) moved into an "Overview" leaf."""
    old_page_content_id = root.page_content_id
    if old_page_content_id is None:
        return
    root.page_content_id = None
    old_page_content = (
        db.query(PageContentV2).filter(PageContentV2.id == old_page_content_id).first()
    )
    if old_page_content is not None:
        db.delete(old_page_content)
    db.flush()


def update_sbn_content(
    db: Session,
    link: str,
    content: dict[str, Any] | list[Any] | None,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any] | None:
    try:
        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        # §6.3 "change a canvas, widget config, or text → edit on `n`".
        # Before any of the transplant machinery below, which mutates rows.
        require_edit(access, _sbn_node(component))

        # A Super Block Note's root is never rendered as a selectable row, so
        # content saved directly on it would be unreachable once sub-tabs
        # exist. When content is saved to a root that has NO sub-tabs yet,
        # auto-create an "Overview" leaf holding it (first in order) and keep
        # the root a pure container — so the content is always reachable as a
        # normal sub-tab. Mirrors create_sbn_node's first-child transplant.
        is_childless_root = (
            component.type == SBN_ROOT_TYPE
            and component.super_blocknote_id is None
            and not _has_sbn_children(db, component.id)
        )
        block_content = content.get("content") if isinstance(content, dict) else content
        if is_childless_root and block_content:
            overview = _create_overview_leaf(db, component, block_content, order=0)
            _drop_root_content(db, component)
            db.commit()
            # The root is now an empty container; its content lives in the
            # freshly-created "Overview" child (surfaced via /workspace).
            response = get_sbn_content(db, link)
            if response is not None:
                response["search_updates"] = [
                    {"component_id": component.id, "action": "upsert"},
                    {"component_id": overview.id, "action": "upsert"},
                ]
            return response

        before_content = (_resolve_component_data(db, component).get("content") or [])

        # Every SBN node's content is a plain BlockNote doc (Block[]) — the
        # user's confirmed scope narrowing (no rich per-sub-tab canvas yet).
        _write_component_data(db, component, {"content": content})
        db.commit()

        response = get_sbn_content(db, link)
        if response is not None and before_content != (content or []):
            response["search_updates"] = [
                {"component_id": component.id, "action": "upsert"}
            ]
        return response

    except Exception:
        db.rollback()
        raise


def create_sbn_node(
    db: Session,
    parent_link: str,
    title: str,
    content: dict[str, Any] | list[Any] | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any] | None:
    """Creates a new leaf sub-tab (`type="block_note"`) under `parent_link`,
    which must itself be an SBN member (the root widget, or an existing
    nested container). There's no UI yet to create a nested container
    (`type="super_block_note"`) sub-tab — every new node defaults to a
    plain-text leaf for this pass."""
    try:
        title = _validate_title(title)
        order = _validate_order(order)

        parent = get_component_by_link(db, parent_link)
        if parent is None or not _is_sbn_member(parent):
            raise ValueError("Parent SBN node does not exist")

        # §6.3 "create a child of `n` → edit on `n`" — the parent, which is
        # the node whose contents this changes. The new row does not exist
        # yet, so there is nothing else it could be gated against.
        require_edit(access, _sbn_node(parent))

        # Root's own content becomes permanently unreachable once real
        # sub-tabs exist — the root itself is never rendered as a selectable
        # row (see SuperBlockNoteWidget.tsx). The first time the root gains a
        # real child, transplant whatever it already holds into an
        # auto-created "Overview" leaf, first in order, so it stays reachable
        # (and is a fully normal, renameable/deletable sub-tab from then on)
        # instead of being silently stranded on the inert root.
        is_root = parent.super_blocknote_id is None and parent.type == SBN_ROOT_TYPE
        inserted_root_content = False
        overview_component: ComponentV2 | None = None
        if is_root and parent.page_content_id is not None and not _has_sbn_children(db, parent.id):
            root_data = _resolve_component_data(db, parent)
            root_content = root_data.get("content") or []
            if root_content:
                overview_component = _create_overview_leaf(
                    db, parent, root_content, order=0
                )
                _drop_root_content(db, parent)
                inserted_root_content = True

        if order is None:
            order = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == parent.id).count()
        elif inserted_root_content:
            # Caller computed this order from its own pre-fetch, unaware the
            # auto-created "Root Content" node above just took position 0.
            order += 1

        new_component = ComponentV2(
            link=_generate_id(),
            type=SBN_LEAF_TYPE,
            title=title,
            props={"locked": False, "locked_by": "", "order": order},
            # Store what was passed, NULL included (§5.2) -- NULL means
            # inherit, exactly like a canvas widget's own AC (§3.4). No
            # fallback to a manufactured default.
            access_control=access_control,
            x=0,
            y=0,
            width=6,
            height=6,
            gridstack_id=parent.gridstack_id,
            super_blocknote_id=parent.id,
            page_content_id=None,
            current_grid_id=None,
        )
        db.add(new_component)
        db.flush()
        _write_component_data(db, new_component, {"content": content or []})

        db.commit()
        response = _format_sbn_summary(db, new_component)
        receipts = [{"component_id": new_component.id, "action": "upsert"}]
        if overview_component is not None:
            receipts.insert(
                0,
                {"component_id": overview_component.id, "action": "upsert"},
            )
        response["search_updates"] = receipts
        return response

    except Exception:
        db.rollback()
        raise


def update_sbn_node(
    db: Session,
    link: str,
    title: str | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any] | None:
    try:
        title = _validate_title(title)
        order = _validate_order(order)

        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        # §6.3 "rename `n` → edit on `n`". One gate for all three fields
        # this endpoint writes, matching `update_tab_by_document_id_v2` —
        # including `order`, which the tab equivalent splits onto the parent
        # only where reordering has its OWN endpoint (nav tabs). SBN order
        # has one too (`reorder_sbn_siblings`, parent-gated below); this
        # field is the single-node echo of it and stays with the node.
        # §11.2 records `edit(n)` vs `edit(parent(n))` for rename as still
        # open with the owner — this follows §6.3's proposal, same as tabs.
        require_edit(access, _sbn_node(component))

        if title is not None:
            component.title = title
        if access_control is not None:
            component.access_control = access_control
        if order is not None:
            component.props = {**_sbn_props(component), "order": order}

        db.commit()
        response = get_sbn_workspace(db, link)
        # SBN order is UI-only. Title and component access are indexed.
        if response is not None and (title is not None or access_control is not None):
            response["search_updates"] = [
                {"component_id": component.id, "action": "upsert"}
            ]
        return response

    except Exception:
        db.rollback()
        raise


def lock_sbn_node(
    db: Session, link: str, locked_by: str, *, access: ViewerAccess | None = None
) -> dict[str, Any] | None:
    """Every SBN node — the root widget itself, or any descendant — is
    independently lockable, matching v1 (where the host tab and every
    sub-tab could each be locked independently). Unlike
    `gridstack_service.py`'s tab-level lock (root-tab-only), there's no
    "must be root" restriction here.

    `locked_by` is the caller's OWN identity — the router derives it from
    the authenticated JWT (plan §6.6 Fix 1), same as
    `lock_tab_by_document_id_v2`. This function reuses that one's staleness
    rule (`is_lock_stale`, owner decision 2026-09-03: Fix 2 extends to SBN
    nodes too) with `locked_at` stored as an ISO-8601 string under
    `props["locked_at"]` rather than a column — `component.props` is
    already JSONB and this needed no migration.

    §6.3 gates this on `edit(n)`, NEW as of 2026-09-09 and the reason that
    table lists lock/unlock as new rows: "without it any signed-in user can
    lock a tab they cannot edit and deny service to those who can". Locking
    is a different axis from *may you edit* — it answers *is someone else
    editing right now* — but taking one is a write, and only an editor has
    any business taking it."""
    try:
        locked_by = _validate_locked_by(locked_by)
        if not locked_by:
            raise ValueError("locked_by is required")

        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        require_edit(access, _sbn_node(component))

        props = _sbn_props(component)
        current_locked_by = props.get("locked_by") or ""
        held_by_someone_else = bool(props.get("locked")) and current_locked_by and current_locked_by != locked_by
        if held_by_someone_else and not is_lock_stale(_sbn_locked_at(props)):
            raise ValueError(f"Node is already locked by {current_locked_by}")

        component.props = {
            **props,
            "locked": True,
            "locked_by": locked_by,
            "locked_at": _utc_now().isoformat(),
        }
        db.commit()
        return get_sbn_workspace(db, link)

    except Exception:
        db.rollback()
        raise


def unlock_sbn_node(
    db: Session,
    link: str,
    unlocked_by: str | None = None,
    force: bool = False,
    *,
    access: ViewerAccess | None = None,
) -> dict[str, Any] | None:
    """`unlocked_by` is identity-sourced and `force` is unchanged — see
    `unlock_tab_by_document_id_v2`'s docstring in gridstack_service.py,
    which this mirrors exactly including the omission-bypass note.

    §6.3 gates unlock on `edit(n)` and FORCE-unlock on "`n` or `parent(n)`",
    and one check covers both: `edit` folds DOWN the root path
    (`edit(n) = seed_edit(n) ∨ edit(parent(n))`, §6.1), so anyone holding
    `edit(parent(n))` already holds `edit(n)`. The disjunction in that table
    row is describing where the grant may LIVE, not two separate lookups —
    the same reason §4.2 notes that "the ACL names my exact pair" and "names
    a pair below mine" are one lookup rather than two branches. Until this,
    `force: true` "skip[ped] every check with nothing gating who may use
    it"; now it skips only the holder check, never the grant check."""
    try:
        unlocked_by = _validate_locked_by(unlocked_by)

        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        require_edit(access, _sbn_node(component))

        props = _sbn_props(component)
        current_locked_by = props.get("locked_by") or ""
        # No `and unlocked_by` short-circuit — see
        # unlock_tab_by_document_id_v2's docstring in gridstack_service.py
        # for why: a falsy unlocked_by must read as "not proven to be the
        # holder", never as "no identity ⇒ let it through".
        held_by_someone_else = bool(props.get("locked")) and current_locked_by and current_locked_by != unlocked_by
        if not force and held_by_someone_else and not is_lock_stale(_sbn_locked_at(props)):
            raise ValueError(f"Node is locked by {current_locked_by}")

        component.props = {**props, "locked": False, "locked_by": "", "locked_at": None}
        db.commit()
        return get_sbn_workspace(db, link)

    except Exception:
        db.rollback()
        raise


def reorder_sbn_siblings(
    db: Session, items: list[dict[str, Any]], *, access: ViewerAccess | None = None
) -> list[dict[str, Any]]:
    try:
        if not items:
            raise ValueError("Reorder items cannot be empty")

        links = [_validate_document_id_value(item["documentId"], "documentId") for item in items]
        orders = [_validate_order(item["order"]) for item in items]

        if len(links) != len(set(links)):
            raise ValueError("Duplicate documentId values are not allowed")

        components = db.query(ComponentV2).filter(ComponentV2.link.in_(links)).all()
        by_link = {c.link: c for c in components}

        missing = [link for link in links if link not in by_link]
        if missing:
            raise ValueError(f"SBN nodes not found: {', '.join(missing)}")

        parent_ids = {c.super_blocknote_id for c in components}
        if len(parent_ids) != 1:
            raise ValueError("All reordered nodes must share the same SBN parent")

        parent_id = next(iter(parent_ids))

        # §6.3 "reorder `n` → parent(n)", resolved per item and gated on the
        # resulting SET, exactly as `reorder_tabs_v2` does. The shared-parent
        # validation above already collapses that set to one node for every
        # real batch; going through the set anyway means this cannot silently
        # depend on that validation staying as strict as it is today.
        #
        # A batch of SBN ROOTS resolves to `{None}` and is refused for
        # everyone but a Hub Admin — `require_edit(access, None)` fails
        # closed. That is the right answer rather than an edge case to
        # special-case: a root's position is its WIDGET's position on a
        # canvas, written by the canvas save, and this endpoint reordering
        # roots would be writing a field nothing reads.
        for parent_node in {_sbn_parent_node(by_link[link]) for link in links}:
            require_edit(access, parent_node)

        for link, order in zip(links, orders):
            component = by_link[link]
            component.props = {**_sbn_props(component), "order": order}

        db.commit()

        if parent_id is None:
            return []
        parent_component = db.query(ComponentV2).filter(ComponentV2.id == parent_id).first()
        if parent_component is None:
            return []
        # Sibling order is not part of an indexed SBN document.
        return get_sbn_children(db, parent_component.link) or []

    except Exception:
        db.rollback()
        raise


def _get_descendant_sbn_ids(db: Session, component_id: int) -> list[int]:
    descendants: list[int] = []
    visited: set[int] = set()

    def walk(current_id: int) -> None:
        if current_id in visited:
            return
        visited.add(current_id)
        children = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == current_id).all()
        for child in children:
            descendants.append(child.id)
            walk(child.id)

    walk(component_id)
    return descendants


def delete_sbn_subtree(
    db: Session, link: str, *, access: ViewerAccess | None = None
) -> dict[str, Any] | None:
    try:
        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None
        if component.type == SBN_ROOT_TYPE and component.super_blocknote_id is None:
            # A root is not deletable through this endpoint at all, so the
            # gate here is only about WHICH refusal the caller gets. Gating
            # on `edit(n)` rather than §6.3's `parent(n)` keeps the
            # explanatory 400 for someone who can actually edit this SBN,
            # while anyone else still gets the fail-closed 404/403 before
            # learning that this link is a root. `_sbn_parent_node` would
            # return None here (a root's real parent is its canvas, not
            # another component), which would turn that 400 into a 404 for
            # every non-admin — correct but needlessly confusing on an
            # operation that is refused for everyone regardless.
            require_edit(access, _sbn_node(component))
            raise ValueError(
                "Deleting the Super Block Note widget itself is done by removing it "
                "from the canvas, not via this endpoint"
            )

        # §6.3 "delete / move / reorder `n` → parent(n)" — deleting a node
        # changes its PARENT's contents. Stricter than `edit(n)` by design:
        # `edit` folds down, so `edit(parent(n))` implies `edit(n)` but not
        # the reverse, and a caller granted edit on one sub-tab must not be
        # able to delete it out of a tree they hold nothing else on.
        require_edit(access, _sbn_parent_node(component))

        descendant_ids = _get_descendant_sbn_ids(db, component.id)
        # Leaves-first, same self-referential-FK-without-relationship
        # ordering care as delete_tab_subtree_by_document_id_v2 — flush per
        # node so SQLAlchemy doesn't batch these into one arbitrary-order
        # executemany.
        ids_to_delete = list(reversed(descendant_ids)) + [component.id]

        deleted = []
        for cid in ids_to_delete:
            node = db.query(ComponentV2).filter(ComponentV2.id == cid).first()
            if node is None:
                continue
            deleted.append({"id": node.id, "documentId": node.link, "title": node.title})
            if node.page_content_id is not None:
                page_content = db.query(PageContentV2).filter(PageContentV2.id == node.page_content_id).first()
                if page_content is not None:
                    db.delete(page_content)
            db.delete(node)
            db.flush()

        db.commit()
        return {
            "message": "SBN subtree deleted successfully",
            "deleted_count": len(deleted),
            "deleted_tabs": deleted,
            "search_updates": [
                {"component_id": item["id"], "action": "delete"}
                for item in deleted
            ],
        }

    except Exception:
        db.rollback()
        raise
