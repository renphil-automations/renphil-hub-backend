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
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.page_content import PageContentV2
from app.services.gridstack_service import (
    _access_control_or_default,
    _generate_id,
    _resolve_component_data,
    _validate_document_id_value,
    _validate_locked_by,
    _validate_order,
    _validate_title,
    _write_component_data,
)

SBN_ROOT_TYPE = "super_block_note"
SBN_LEAF_TYPE = "block_note"


def get_component_by_link(db: Session, link: str) -> ComponentV2 | None:
    link = _validate_document_id_value(link, "link")
    return db.query(ComponentV2).filter(ComponentV2.link == link).first()


def _is_sbn_member(component: ComponentV2) -> bool:
    """True for the SBN root itself (the widget's own top-level component
    row), or any node reached via `super_blocknote_id` — i.e. every node
    that's part of some Super Block Note's own internally-managed tree."""
    return component.type == SBN_ROOT_TYPE or component.super_blocknote_id is not None


def _sbn_props(component: ComponentV2) -> dict[str, Any]:
    return component.props or {}


def _has_sbn_children(db: Session, component_id: int) -> bool:
    return (
        db.query(ComponentV2.id)
        .filter(ComponentV2.super_blocknote_id == component_id)
        .first()
        is not None
    )


def _format_sbn_summary(db: Session, component: ComponentV2) -> dict[str, Any]:
    props = _sbn_props(component)
    return {
        "id": component.id,
        "documentId": component.link,
        "title": component.title,
        "order": props.get("order", 0),
        "locked": bool(props.get("locked", False)),
        "locked_by": props.get("locked_by", "") or "",
        "has_children": _has_sbn_children(db, component.id),
        "has_content": component.page_content_id is not None,
        "apiVersion": "v2",
    }


def get_sbn_children(db: Session, link: str) -> list[dict[str, Any]] | None:
    component = get_component_by_link(db, link)
    if component is None or not _is_sbn_member(component):
        return None

    children = db.query(ComponentV2).filter(ComponentV2.super_blocknote_id == component.id).all()
    summaries = [_format_sbn_summary(db, c) for c in children]
    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def get_sbn_workspace(db: Session, link: str) -> dict[str, Any] | None:
    component = get_component_by_link(db, link)
    if component is None or not _is_sbn_member(component):
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
    child_summaries = [_format_sbn_summary(db, c) for c in children]
    child_summaries.sort(key=lambda s: (s["order"], s["id"] or 0))

    data = _resolve_component_data(db, component)

    return {
        "id": component.id,
        "documentId": component.link,
        "title": component.title,
        "order": props.get("order", 0),
        "parent": parent,
        "page_content": {"documentId": component.link, "content": data.get("content")},
        "access_control": _access_control_or_default(component.access_control),
        "locked": bool(props.get("locked", False)),
        "locked_by": props.get("locked_by", "") or "",
        "children": child_summaries,
        "apiVersion": "v2",
    }


def get_sbn_content(db: Session, link: str) -> dict[str, Any] | None:
    component = get_component_by_link(db, link)
    if component is None or not _is_sbn_member(component):
        return None
    data = _resolve_component_data(db, component)
    return {"documentId": component.link, "content": data.get("content")}


def update_sbn_content(
    db: Session, link: str, content: dict[str, Any] | list[Any] | None
) -> dict[str, Any] | None:
    try:
        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        # Every SBN node's content is a plain BlockNote doc (Block[]) — the
        # user's confirmed scope narrowing (no rich per-sub-tab canvas yet).
        _write_component_data(db, component, {"content": content})
        db.commit()

        return get_sbn_content(db, link)

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

        # Root's own content becomes permanently unreachable once real
        # sub-tabs exist — the root itself is never rendered as a selectable
        # row (see SuperBlockNoteWidget.tsx). The first time the root gains a
        # real child, transplant whatever it already holds into an
        # auto-created "Root Content" leaf, first in order, so it stays
        # reachable (and is a fully normal, renameable/deletable sub-tab from
        # then on) instead of being silently stranded on the inert root.
        is_root = parent.super_blocknote_id is None and parent.type == SBN_ROOT_TYPE
        inserted_root_content = False
        if is_root and parent.page_content_id is not None and not _has_sbn_children(db, parent.id):
            root_data = _resolve_component_data(db, parent)
            root_content = root_data.get("content") or []
            if root_content:
                old_page_content_id = parent.page_content_id
                root_content_node = ComponentV2(
                    link=_generate_id(),
                    type=SBN_LEAF_TYPE,
                    title="Root Content",
                    props={"locked": False, "locked_by": "", "order": 0},
                    access_control=parent.access_control or _access_control_or_default(None),
                    x=0,
                    y=0,
                    width=6,
                    height=6,
                    gridstack_id=parent.gridstack_id,
                    super_blocknote_id=parent.id,
                    page_content_id=None,
                    current_grid_id=None,
                )
                db.add(root_content_node)
                db.flush()
                # Root (type="super_block_note") and a leaf (type="block_note")
                # use different storage conventions in _write_component_data —
                # a leaf unwraps `data["content"]` before storing, root stores
                # the dict as-is. Re-writing through the leaf's own convention
                # here (rather than transplanting the raw page_content_id
                # pointer) avoids double-wrapping the block list.
                _write_component_data(db, root_content_node, {"content": root_content})
                parent.page_content_id = None
                old_page_content = (
                    db.query(PageContentV2).filter(PageContentV2.id == old_page_content_id).first()
                )
                if old_page_content is not None:
                    db.delete(old_page_content)
                db.flush()
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
            access_control=access_control or _access_control_or_default(None),
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
        return _format_sbn_summary(db, new_component)

    except Exception:
        db.rollback()
        raise


def update_sbn_node(
    db: Session,
    link: str,
    title: str | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        title = _validate_title(title)
        order = _validate_order(order)

        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        if title is not None:
            component.title = title
        if access_control is not None:
            component.access_control = access_control
        if order is not None:
            component.props = {**_sbn_props(component), "order": order}

        db.commit()
        return get_sbn_workspace(db, link)

    except Exception:
        db.rollback()
        raise


def lock_sbn_node(db: Session, link: str, locked_by: str) -> dict[str, Any] | None:
    """Every SBN node — the root widget itself, or any descendant — is
    independently lockable, matching v1 (where the host tab and every
    sub-tab could each be locked independently). Unlike
    `gridstack_service.py`'s tab-level lock (root-tab-only), there's no
    "must be root" restriction here."""
    try:
        locked_by = _validate_locked_by(locked_by)
        if not locked_by:
            raise ValueError("locked_by is required")

        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        props = _sbn_props(component)
        current_locked_by = props.get("locked_by") or ""
        if props.get("locked") and current_locked_by and current_locked_by != locked_by:
            raise ValueError(f"Node is already locked by {current_locked_by}")

        component.props = {**props, "locked": True, "locked_by": locked_by}
        db.commit()
        return get_sbn_workspace(db, link)

    except Exception:
        db.rollback()
        raise


def unlock_sbn_node(
    db: Session, link: str, unlocked_by: str | None = None, force: bool = False
) -> dict[str, Any] | None:
    try:
        unlocked_by = _validate_locked_by(unlocked_by)

        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None

        props = _sbn_props(component)
        current_locked_by = props.get("locked_by") or ""
        if not force and props.get("locked") and current_locked_by and unlocked_by and current_locked_by != unlocked_by:
            raise ValueError(f"Node is locked by {current_locked_by}")

        component.props = {**props, "locked": False, "locked_by": ""}
        db.commit()
        return get_sbn_workspace(db, link)

    except Exception:
        db.rollback()
        raise


def reorder_sbn_siblings(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

        for link, order in zip(links, orders):
            component = by_link[link]
            component.props = {**_sbn_props(component), "order": order}

        db.commit()

        if parent_id is None:
            return []
        parent_component = db.query(ComponentV2).filter(ComponentV2.id == parent_id).first()
        if parent_component is None:
            return []
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


def delete_sbn_subtree(db: Session, link: str) -> dict[str, Any] | None:
    try:
        component = get_component_by_link(db, link)
        if component is None or not _is_sbn_member(component):
            return None
        if component.type == SBN_ROOT_TYPE and component.super_blocknote_id is None:
            raise ValueError(
                "Deleting the Super Block Note widget itself is done by removing it "
                "from the canvas, not via this endpoint"
            )

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
        }

    except Exception:
        db.rollback()
        raise
