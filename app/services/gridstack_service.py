"""
Phase 3 v2 service layer: CRUD + GridCanvasContent<->rows translation for the
normalized tabs/gridstacks/components/page_content schema (app.db_v2).

Every function here mirrors a same-named function in tab_service.py, but the
response shapes match exactly (TabSummaryResponse / TabWorkspaceResponse /
PageContentWorkspaceResponse) so the v2 router can reuse the v1 Pydantic
schemas unchanged, and so the frontend needs no new data model for v2 tabs.

A "tab" the frontend addresses via documentId is either a TabV2 (root) or a
nested GridstackV2 (sub-tab) — both have their own document_id, and every
lookup here goes through GridstackV2.document_id (a root's own gridstack row
carries the same document_id as its TabV2, see create_tab_v2).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db_v2.models.tab import TabV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.page_content import PageContentV2

from app.services.tab_service import DEFAULT_ACCESS_CONTROL


# ---------------------------------------------------------
# Constants shared with tab_service.py's validation rules
# ---------------------------------------------------------

MAX_TITLE_LENGTH = 255
MAX_DOCUMENT_ID_LENGTH = 255
MAX_LOCKED_BY_LENGTH = 255

MIN_ORDER_VALUE = -2147483648
MAX_ORDER_VALUE = 2147483647

RESTRICTED_WIDGET_TYPE = "restricted"
MIRROR_WIDGET_TYPE = "mirror"


# ---------------------------------------------------------
# Small validation / generation helpers
# ---------------------------------------------------------

def _validate_title(title: str | None) -> str | None:
    if title is None:
        return None
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Title cannot be empty")
    if len(clean_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title cannot be longer than {MAX_TITLE_LENGTH} characters")
    return clean_title


def _validate_document_id_value(document_id: str | None, field_name: str = "documentId") -> str | None:
    if document_id is None:
        return None
    clean_document_id = document_id.strip()
    if not clean_document_id:
        raise ValueError(f"{field_name} cannot be empty")
    if len(clean_document_id) > MAX_DOCUMENT_ID_LENGTH:
        raise ValueError(f"{field_name} cannot be longer than {MAX_DOCUMENT_ID_LENGTH} characters")
    return clean_document_id


def _validate_locked_by(value: str | None) -> str | None:
    if value is None:
        return None
    clean_value = value.strip()
    if len(clean_value) > MAX_LOCKED_BY_LENGTH:
        raise ValueError(f"locked_by cannot be longer than {MAX_LOCKED_BY_LENGTH} characters")
    return clean_value


def _validate_order(order: int | None) -> int | None:
    if order is None:
        return None
    if order < MIN_ORDER_VALUE or order > MAX_ORDER_VALUE:
        raise ValueError(f"Order must be between {MIN_ORDER_VALUE} and {MAX_ORDER_VALUE}")
    return order


def _generate_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _access_control_or_default(access_control: dict[str, Any] | None) -> dict[str, Any]:
    return access_control if access_control else DEFAULT_ACCESS_CONTROL


# ---------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------

def get_gridstack_by_document_id(db: Session, document_id: str) -> GridstackV2 | None:
    document_id = _validate_document_id_value(document_id, "document_id")
    return db.query(GridstackV2).filter(GridstackV2.document_id == document_id).first()


def _is_root(gridstack: GridstackV2) -> bool:
    return gridstack.parent_id is None


def _get_root_tab(db: Session, gridstack: GridstackV2) -> TabV2 | None:
    return db.query(TabV2).filter(TabV2.id == gridstack.parent_tab_id).first()


def _has_children(db: Session, gridstack_id: int) -> bool:
    return (
        db.query(GridstackV2.id)
        .filter(GridstackV2.parent_id == gridstack_id)
        .first()
        is not None
    )


def _has_content(db: Session, gridstack_id: int) -> bool:
    return (
        db.query(ComponentV2.id)
        .filter(ComponentV2.gridstack_id == gridstack_id)
        .first()
        is not None
    )


def _safe_access_control_for_gridstack(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    if _is_root(gridstack):
        tab = _get_root_tab(db, gridstack)
        return _access_control_or_default(tab.access_control if tab else None)
    settings = gridstack.settings or {}
    return _access_control_or_default(settings.get("access_control"))


def _safe_locked_pair(db: Session, gridstack: GridstackV2) -> tuple[bool, str]:
    if _is_root(gridstack):
        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return False, ""
        return bool(tab.locked), (tab.locked_by or "")
    # Nested gridstacks have no lock columns — lock granularity is
    # whole-tab-only in this schema (Phase 2 decision).
    return False, ""


def _format_tab_summary(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    locked, locked_by = _safe_locked_pair(db, gridstack)
    node_id = gridstack.parent_tab_id if _is_root(gridstack) else gridstack.id
    title = None
    order = gridstack.position if gridstack.position is not None else 0

    if _is_root(gridstack):
        tab = _get_root_tab(db, gridstack)
        title = tab.title if tab else gridstack.name
        order = tab.order if tab and tab.order is not None else order
    else:
        title = gridstack.name

    return {
        "id": node_id,
        "documentId": gridstack.document_id,
        "title": title,
        "order": order,
        "locked": locked,
        "locked_by": locked_by,
        "has_children": _has_children(db, gridstack.id),
        "has_content": _has_content(db, gridstack.id),
        "apiVersion": "v2",
    }


# ---------------------------------------------------------
# Content serialization: ComponentV2 rows <-> GridCanvasContent
# ---------------------------------------------------------

def _resolve_component_data(db: Session, component: ComponentV2) -> dict[str, Any]:
    """The widget-facing `data` blob for one component — shared by the normal
    serializer path, a mirror's resolution of its target, and the by-link
    lookup endpoint, so all three agree on how content gets resolved.

    Every component type's `data` lives in `page_content` via
    `page_content_id` (not `props` — see ComponentV2's docstring). `block_note`
    wraps the raw stored `Block[]` as `{"content": [...]}`; every other type's
    `page_content.content` already IS its `data` dict, returned as-is.

    Never called on a mirror component itself (only ever on mirror TARGETS,
    which by construction are never mirrors — see `_serialize_component`'s
    cycle guard) — falls back to `{}` rather than raising, since this is
    defense-in-depth for an invariant already enforced elsewhere, not a case
    that should ever surface a 500.

    Rows that predate this change (data still sitting in `props`, no
    `page_content_id` yet) fall back to reading `props` — purely transitional;
    any subsequent save through `update_tab_content_v2`/`_write_component_data`
    moves that row onto `page_content` for good.
    """
    if component.type == MIRROR_WIDGET_TYPE:
        return {}

    if component.page_content_id is not None:
        page_content = (
            db.query(PageContentV2)
            .filter(PageContentV2.id == component.page_content_id)
            .first()
        )
        stored = page_content.content if page_content else None
        if component.type == "block_note":
            return {"content": stored if stored is not None else []}
        return stored if isinstance(stored, dict) else {}

    # Legacy fallback: no page_content_id yet — this row's data (if any) is
    # still sitting in `props` from before the universal page_content move.
    props = component.props or {}
    legacy_data = {k: v for k, v in props.items() if k not in ("min_w", "min_h", "locked", "locked_by", "order")}
    if component.type == "block_note":
        return {"content": legacy_data.get("content", [])}
    return legacy_data


def _write_component_data(db: Session, component: ComponentV2, data: dict[str, Any] | None) -> None:
    """Persists `data` into `page_content`, creating a new `PageContentV2` row
    on first write if `page_content_id` is still unset. Mirrors
    `_resolve_component_data`'s wrap/unwrap convention for `block_note`. Never
    called for a `mirror` component (its only persisted data is `target_link`,
    which lives in `props`, not `page_content` — see `update_tab_content_v2`)."""
    stored = (data or {}).get("content", []) if component.type == "block_note" else (data or {})

    if component.page_content_id is not None:
        page_content = (
            db.query(PageContentV2)
            .filter(PageContentV2.id == component.page_content_id)
            .first()
        )
    else:
        page_content = None

    if page_content is not None:
        page_content.content = stored
    else:
        page_content = PageContentV2(content=stored)
        db.add(page_content)
        db.flush()
        component.page_content_id = page_content.id


def _serialize_component(db: Session, component: ComponentV2) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (layout_entry, widget_entry) for one component, matching
    gridContent.ts's GridWidgetLayout / GridWidgetEntry shapes."""

    props = component.props or {}

    layout_entry: dict[str, Any] = {
        "id": str(component.id),
        "x": component.x if component.x is not None else 0,
        "y": component.y if component.y is not None else 0,
        "w": component.width if component.width is not None else 6,
        "h": component.height if component.height is not None else 6,
        "type": component.type,
    }
    if props.get("min_w") is not None:
        layout_entry["minW"] = props["min_w"]
    if props.get("min_h") is not None:
        layout_entry["minH"] = props["min_h"]

    if component.type == MIRROR_WIDGET_TYPE:
        target_link = props.get("target_link")
        target = (
            db.query(ComponentV2).filter(ComponentV2.link == target_link).first()
            if target_link
            else None
        )

        if target is None or target.type == MIRROR_WIDGET_TYPE:
            # Dangling target, or defensive cycle guard (a mirror should
            # never be able to target another mirror — the picker already
            # excludes mirrors as pickable targets; this is defense in depth).
            widget_entry = {
                "type": MIRROR_WIDGET_TYPE,
                "link": component.link,
                "data": {"targetLink": target_link, "mirroredType": None, "mirroredData": None},
            }
        else:
            widget_entry = {
                "type": MIRROR_WIDGET_TYPE,
                "link": component.link,
                "data": {
                    "targetLink": target_link,
                    "mirroredType": target.type,
                    "mirroredData": _resolve_component_data(db, target),
                },
            }
            # Key simplification: substitute the TARGET's own access_control
            # so filter_widget_content_for_user (unchanged) enforces exactly
            # the same visibility a direct view of the target would have.
            if target.access_control:
                widget_entry["access_control"] = target.access_control

        # A mirror has its own independent title/description (labeling the
        # mirror INSTANCE itself, e.g. "Announcements (mirrored)") — never
        # the target's, which stays fully separate metadata.
        widget_entry["title"] = component.title
        widget_entry["description"] = component.description

        return layout_entry, widget_entry

    data = _resolve_component_data(db, component)

    # Every widget entry carries its own stable `link` (not just mirrors) —
    # this is how the mirror target picker learns what to reference; the
    # widget's `layout`/`widgets` KEY (str(component.id)) is only a local,
    # same-canvas identity and must never be used for cross-tab addressing.
    widget_entry = {
        "type": component.type,
        "link": component.link,
        "title": component.title,
        "description": component.description,
        "data": data,
    }
    if component.access_control:
        widget_entry["access_control"] = component.access_control

    return layout_entry, widget_entry


def _serialize_gridstack_content(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    # Excludes Super Block Note descendants (super_blocknote_id set) even
    # though they share this same gridstack_id (a NOT NULL column they must
    # still populate) — they're never real top-level canvas widgets, only
    # reachable through their own SBN tree (see super_blocknote_service.py).
    components = (
        db.query(ComponentV2)
        .filter(
            ComponentV2.gridstack_id == gridstack.id,
            ComponentV2.super_blocknote_id.is_(None),
        )
        .order_by(ComponentV2.id.asc())
        .all()
    )

    layout: list[dict[str, Any]] = []
    widgets: dict[str, Any] = {}

    for component in components:
        layout_entry, widget_entry = _serialize_component(db, component)
        layout.append(layout_entry)
        widgets[str(component.id)] = widget_entry

    content: dict[str, Any] = {
        "schemaVersion": 2,
        "layout": layout,
        "widgets": widgets,
    }

    settings = gridstack.settings or {}
    if settings.get("sgs"):
        content["sgs"] = settings["sgs"]

    return content


def _format_page_content(db: Session, gridstack: GridstackV2) -> dict[str, Any]:
    return {
        "documentId": gridstack.document_id,
        "content": _serialize_gridstack_content(db, gridstack),
    }


def get_component_by_link_v2(db: Session, link: str) -> dict[str, Any] | None:
    """Resolves a component by its stable `link` — backs the mirror picker's
    "paste a link" flow (as opposed to browsing tabs/children/workspace).
    Returns None for an unknown link, or one that points at a mirror itself
    (cycle prevention — matches the picker's browse mode, which never lists
    mirrors as pickable targets)."""
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type == MIRROR_WIDGET_TYPE:
        return None
    return {
        "type": component.type,
        "title": component.title,
        "data": _resolve_component_data(db, component),
    }


def _get_gridstack_ancestor_chain(db: Session, gridstack: GridstackV2) -> list[str]:
    """Root-first ordered `document_id`s from the root's immediate child
    down to (and including) `gridstack` itself — i.e. the sequence of SGS/
    nav-tree sub-tab clicks needed to reach `gridstack`'s own canvas. Empty
    if `gridstack` is already the root (`_is_root`)."""
    chain: list[str] = []
    current: GridstackV2 | None = gridstack
    while current is not None and current.parent_id is not None:
        chain.append(current.document_id)
        current = db.query(GridstackV2).filter(GridstackV2.id == current.parent_id).first()
    chain.reverse()
    return chain


def _get_sbn_ancestor_chain(db: Session, component: ComponentV2) -> list[str]:
    """Root-first ordered component `link`s from the top-level Super Block
    Note widget down to (and including) `component` itself. Empty if
    `component` isn't an SBN descendant (`super_blocknote_id is None`) —
    i.e. it's already a top-level canvas widget, addressed directly via its
    own gridstack location instead."""
    if component.super_blocknote_id is None:
        return []
    chain: list[str] = [component.link]
    current = component
    while current.super_blocknote_id is not None:
        current = db.query(ComponentV2).filter(ComponentV2.id == current.super_blocknote_id).first()
        if current is None:
            break
        chain.append(current.link)
    chain.reverse()
    return chain


def resolve_component_location_v2(db: Session, link: str) -> dict[str, Any] | None:
    """Resolves a component's `link` into the full path needed to navigate
    to and locate it in the UI — backs the mirror widget's "jump to
    original" affordance. Returns None for an unknown link, or one that
    points at a mirror itself (same cycle-prevention convention as
    `get_component_by_link_v2` — a mirror is never a navigable "original")."""
    link = (link or "").strip()
    if not link:
        return None
    component = db.query(ComponentV2).filter(ComponentV2.link == link).first()
    if component is None or component.type == MIRROR_WIDGET_TYPE:
        return None

    sbn_path = _get_sbn_ancestor_chain(db, component)
    # An SBN descendant's own gridstack_id is its SBN root's — see
    # ComponentV2's docstring — so the gridstack/tab lookup below must
    # always resolve against the top-level widget, not the descendant.
    top_level_component = component
    if sbn_path:
        top_level_component = db.query(ComponentV2).filter(ComponentV2.link == sbn_path[0]).first()
        if top_level_component is None:
            return None

    gridstack = (
        db.query(GridstackV2).filter(GridstackV2.id == top_level_component.gridstack_id).first()
    )
    if gridstack is None:
        return None

    root_tab = _get_root_tab(db, gridstack)
    if root_tab is None:
        return None

    return {
        "rootTabDocumentId": root_tab.document_id,
        "gridstackPath": _get_gridstack_ancestor_chain(db, gridstack),
        "sbnPath": sbn_path,
        "componentLink": component.link,
    }


# ---------------------------------------------------------
# Read API
# ---------------------------------------------------------

def get_root_tabs_v2(db: Session) -> list[dict[str, Any]]:
    root_gridstacks = (
        db.query(GridstackV2)
        .filter(GridstackV2.parent_id.is_(None))
        .all()
    )
    summaries = [_format_tab_summary(db, g) for g in root_gridstacks]
    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def get_tab_children_v2(db: Session, document_id: str) -> list[dict[str, Any]] | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None

    children = (
        db.query(GridstackV2)
        .filter(GridstackV2.parent_id == gridstack.id)
        .all()
    )
    summaries = [_format_tab_summary(db, c) for c in children]
    summaries.sort(key=lambda s: (s["order"], s["id"] or 0))
    return summaries


def get_tab_content_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None
    return _format_page_content(db, gridstack)


def get_tab_workspace_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    gridstack = get_gridstack_by_document_id(db, document_id)
    if gridstack is None:
        return None

    if _is_root(gridstack):
        tab = _get_root_tab(db, gridstack)
        node_id = tab.id if tab else None
        title = tab.title if tab else gridstack.name
        order = tab.order if tab and tab.order is not None else (gridstack.position or 0)
    else:
        node_id = gridstack.id
        title = gridstack.name
        order = gridstack.position if gridstack.position is not None else 0

    parent = None
    if gridstack.parent_id is not None:
        parent_gridstack = db.query(GridstackV2).filter(GridstackV2.id == gridstack.parent_id).first()
        if parent_gridstack is not None:
            parent = {
                "id": parent_gridstack.id,
                "documentId": parent_gridstack.document_id,
                "title": parent_gridstack.name,
                "order": parent_gridstack.position if parent_gridstack.position is not None else 0,
            }

    children = (
        db.query(GridstackV2)
        .filter(GridstackV2.parent_id == gridstack.id)
        .all()
    )
    child_summaries = [_format_tab_summary(db, c) for c in children]
    child_summaries.sort(key=lambda s: (s["order"], s["id"] or 0))

    locked, locked_by = _safe_locked_pair(db, gridstack)

    return {
        "id": node_id,
        "documentId": gridstack.document_id,
        "title": title,
        "order": order,
        "parent": parent,
        "page_content": _format_page_content(db, gridstack),
        "access_control": _safe_access_control_for_gridstack(db, gridstack),
        "locked": locked,
        "locked_by": locked_by,
        "children": child_summaries,
        "apiVersion": "v2",
    }


# ---------------------------------------------------------
# Content write: diff incoming GridCanvasContent against existing rows
# ---------------------------------------------------------

def update_tab_content_v2(
    db: Session,
    document_id: str,
    content: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | None:
    try:
        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        incoming = content if isinstance(content, dict) else {}
        incoming_layout = {entry.get("id"): entry for entry in (incoming.get("layout") or [])}
        incoming_widgets = incoming.get("widgets") or {}

        # Excludes Super Block Note descendants (super_blocknote_id set) —
        # they share this gridstack_id but are never part of the top-level
        # canvas diff; without this filter, every save of an unrelated
        # top-level widget would see them as "removed" (never present in
        # incoming_widgets, which only ever carries top-level entries) and
        # delete them. Matches _serialize_gridstack_content's same filter.
        existing_components = {
            str(c.id): c
            for c in db.query(ComponentV2)
            .filter(
                ComponentV2.gridstack_id == gridstack.id,
                ComponentV2.super_blocknote_id.is_(None),
            )
            .all()
        }

        incoming_ids = set(incoming_widgets.keys())

        # Delete components removed from the canvas.
        for existing_id, component in list(existing_components.items()):
            if existing_id not in incoming_ids:
                if component.page_content_id is not None:
                    page_content = (
                        db.query(PageContentV2)
                        .filter(PageContentV2.id == component.page_content_id)
                        .first()
                    )
                    if page_content is not None:
                        db.delete(page_content)
                db.delete(component)
                del existing_components[existing_id]

        # Update-in-place or insert.
        for widget_id, widget_entry in incoming_widgets.items():
            if not isinstance(widget_entry, dict):
                continue

            layout_entry = incoming_layout.get(widget_id) or {}
            widget_type = widget_entry.get("type")
            widget_data = widget_entry.get("data")

            # Structural-metadata-only now — a widget's actual `data` never
            # lives in `props` (see ComponentV2's docstring); it's persisted
            # via `_write_component_data` below instead.
            if widget_type == MIRROR_WIDGET_TYPE:
                structural_props: dict[str, Any] = {
                    "target_link": (widget_data or {}).get("targetLink")
                    if isinstance(widget_data, dict)
                    else None
                }
            else:
                structural_props = {}
                if layout_entry.get("minW") is not None:
                    structural_props["min_w"] = layout_entry["minW"]
                if layout_entry.get("minH") is not None:
                    structural_props["min_h"] = layout_entry["minH"]

            access_control = widget_entry.get("access_control")
            title = widget_entry.get("title")
            description = widget_entry.get("description")

            existing = existing_components.get(widget_id)
            if existing is not None:
                existing.type = widget_type
                existing.x = layout_entry.get("x", existing.x)
                existing.y = layout_entry.get("y", existing.y)
                existing.width = layout_entry.get("w", existing.width)
                existing.height = layout_entry.get("h", existing.height)
                existing.access_control = access_control
                existing.title = title
                existing.description = description

                # Merge, don't overwrite: a top-level widget that's the SBN
                # root (type == "super_block_note") may already have
                # locked/locked_by set in `props` via the SBN lock endpoints —
                # a plain resize/move save here must not silently wipe that.
                existing.props = {**(existing.props or {}), **structural_props}

                if widget_type != MIRROR_WIDGET_TYPE:
                    _write_component_data(db, existing, widget_data if isinstance(widget_data, dict) else {})
            else:
                new_component = ComponentV2(
                    link=_generate_id(),
                    type=widget_type,
                    title=title,
                    description=description,
                    props=structural_props,
                    access_control=access_control,
                    x=layout_entry.get("x", 0),
                    y=layout_entry.get("y", 0),
                    width=layout_entry.get("w", 6),
                    height=layout_entry.get("h", 6),
                    gridstack_id=gridstack.id,
                    page_content_id=None,
                    current_grid_id=None,
                )
                db.add(new_component)
                if widget_type != MIRROR_WIDGET_TYPE:
                    db.flush()
                    _write_component_data(db, new_component, widget_data if isinstance(widget_data, dict) else {})

        if _is_root(gridstack):
            tab = _get_root_tab(db, gridstack)
            if tab is not None:
                tab.updated_at = _utc_now()

        db.commit()

        return _format_page_content(db, gridstack)

    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------

def _gridstack_exists_under_parent(
    db: Session,
    name: str,
    parent_tab_id: int,
    parent_id: int,
) -> GridstackV2 | None:
    name = _validate_title(name)
    return (
        db.query(GridstackV2)
        .filter(
            GridstackV2.name == name,
            GridstackV2.parent_tab_id == parent_tab_id,
            GridstackV2.parent_id == parent_id,
        )
        .first()
    )


def _root_tab_exists_by_title(db: Session, title: str) -> TabV2 | None:
    title = _validate_title(title)
    return db.query(TabV2).filter(TabV2.title == title).first()


def create_tab_v2(
    db: Session,
    title: str,
    parent_document_id: str | None = None,
    content: dict[str, Any] | list[Any] | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        title = _validate_title(title)
        parent_document_id = _validate_document_id_value(parent_document_id, "parentDocumentId")
        order = _validate_order(order)

        parent_gridstack: GridstackV2 | None = None
        if parent_document_id is not None:
            parent_gridstack = get_gridstack_by_document_id(db, parent_document_id)
            if parent_gridstack is None:
                raise ValueError("Parent tab does not exist")

        now = _utc_now()

        if parent_gridstack is None:
            if _root_tab_exists_by_title(db, title) is not None:
                raise ValueError("A tab with this title already exists under the same parent")

            new_tab = TabV2(
                document_id=_generate_id(),
                title=title,
                order=order,
                access_control=access_control or DEFAULT_ACCESS_CONTROL,
                locked=False,
                locked_by="",
                created_at=now,
                updated_at=now,
            )
            db.add(new_tab)
            db.flush()

            new_gridstack = GridstackV2(
                document_id=new_tab.document_id,
                name=title,
                settings={},
                position=order,
                parent_id=None,
                parent_tab_id=new_tab.id,
            )
            db.add(new_gridstack)
            db.flush()
        else:
            existing = _gridstack_exists_under_parent(
                db, title, parent_tab_id=parent_gridstack.parent_tab_id, parent_id=parent_gridstack.id
            )
            if existing is not None:
                raise ValueError("A tab with this title already exists under the same parent")

            new_gridstack = GridstackV2(
                document_id=_generate_id(),
                name=title,
                settings={"access_control": access_control or DEFAULT_ACCESS_CONTROL},
                position=order,
                parent_id=parent_gridstack.id,
                parent_tab_id=parent_gridstack.parent_tab_id,
            )
            db.add(new_gridstack)
            db.flush()

        if content:
            update_tab_content_v2(db, new_gridstack.document_id, content)

        db.commit()

        return _format_tab_summary(db, new_gridstack)

    except Exception:
        db.rollback()
        raise


def update_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    title: str | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    locked: bool | None = None,
    locked_by: str | None = None,
) -> dict[str, Any] | None:
    try:
        title = _validate_title(title)
        order = _validate_order(order)
        locked_by = _validate_locked_by(locked_by)

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if _is_root(gridstack):
            tab = _get_root_tab(db, gridstack)
            if tab is None:
                return None
            if title is not None:
                tab.title = title
                gridstack.name = title
            if order is not None:
                tab.order = order
                gridstack.position = order
            if access_control is not None:
                tab.access_control = access_control
            if locked is not None:
                tab.locked = locked
            if locked_by is not None:
                tab.locked_by = locked_by
            tab.updated_at = _utc_now()
        else:
            if title is not None:
                gridstack.name = title
            if order is not None:
                gridstack.position = order
            if access_control is not None:
                settings = dict(gridstack.settings or {})
                settings["access_control"] = access_control
                gridstack.settings = settings
            if locked is not None or locked_by is not None:
                raise ValueError(
                    "Locking is only supported for top-level tabs in this schema version"
                )

        db.commit()
        return get_tab_workspace_v2(db, document_id)

    except Exception:
        db.rollback()
        raise


def lock_tab_by_document_id_v2(db: Session, document_id: str, locked_by: str) -> dict[str, Any] | None:
    try:
        locked_by = _validate_locked_by(locked_by)
        if not locked_by:
            raise ValueError("locked_by is required")

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if not _is_root(gridstack):
            raise ValueError("Locking is only supported for top-level tabs in this schema version")

        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return None

        if tab.locked and tab.locked_by and tab.locked_by != locked_by:
            raise ValueError(f"Tab is already locked by {tab.locked_by}")

        tab.locked = True
        tab.locked_by = locked_by
        tab.updated_at = _utc_now()

        db.commit()
        return get_tab_workspace_v2(db, document_id)

    except Exception:
        db.rollback()
        raise


def unlock_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    unlocked_by: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    try:
        unlocked_by = _validate_locked_by(unlocked_by)

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if not _is_root(gridstack):
            raise ValueError("Locking is only supported for top-level tabs in this schema version")

        tab = _get_root_tab(db, gridstack)
        if tab is None:
            return None

        if not force and tab.locked and tab.locked_by and unlocked_by and tab.locked_by != unlocked_by:
            raise ValueError(f"Tab is locked by {tab.locked_by}")

        tab.locked = False
        tab.locked_by = ""
        tab.updated_at = _utc_now()

        db.commit()
        return get_tab_workspace_v2(db, document_id)

    except Exception:
        db.rollback()
        raise


def get_descendant_gridstack_ids(db: Session, gridstack_id: int) -> list[int]:
    descendants: list[int] = []
    visited: set[int] = set()

    def walk(current_id: int) -> None:
        if current_id in visited:
            return
        visited.add(current_id)
        children = db.query(GridstackV2).filter(GridstackV2.parent_id == current_id).all()
        for child in children:
            descendants.append(child.id)
            walk(child.id)

    walk(gridstack_id)
    return descendants


def _is_descendant_of(db: Session, possible_descendant_id: int, possible_ancestor_id: int) -> bool:
    return possible_descendant_id in get_descendant_gridstack_ids(db, possible_ancestor_id)


def move_tab_by_document_id_v2(
    db: Session,
    document_id: str,
    new_parent_document_id: str | None = None,
    order: int | None = None,
) -> dict[str, Any] | None:
    try:
        new_parent_document_id = _validate_document_id_value(new_parent_document_id, "newParentDocumentId")
        order = _validate_order(order)

        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        if _is_root(gridstack):
            raise ValueError("Moving a root tab is not supported in this schema version")

        new_parent: GridstackV2 | None = None
        if new_parent_document_id is not None:
            new_parent = get_gridstack_by_document_id(db, new_parent_document_id)
            if new_parent is None:
                raise ValueError("New parent tab does not exist")
            if new_parent.id == gridstack.id:
                raise ValueError("A tab cannot be moved under itself")
            if _is_descendant_of(db, new_parent.id, gridstack.id):
                raise ValueError("A tab cannot be moved under one of its descendants")

            gridstack.parent_id = new_parent.id
            gridstack.parent_tab_id = new_parent.parent_tab_id
            for descendant_id in get_descendant_gridstack_ids(db, gridstack.id):
                descendant = db.query(GridstackV2).filter(GridstackV2.id == descendant_id).first()
                if descendant is not None:
                    descendant.parent_tab_id = new_parent.parent_tab_id
        else:
            raise ValueError("Moving a sub-tab to root is not supported in this schema version")

        if order is not None:
            gridstack.position = order

        db.commit()
        return get_tab_workspace_v2(db, document_id)

    except Exception:
        db.rollback()
        raise


def reorder_tabs_by_document_id_v2(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        if not items:
            raise ValueError("Reorder items cannot be empty")

        document_ids = [_validate_document_id_value(item["documentId"], "documentId") for item in items]
        orders = [_validate_order(item["order"]) for item in items]

        if len(document_ids) != len(set(document_ids)):
            raise ValueError("Duplicate documentId values are not allowed")

        gridstacks = (
            db.query(GridstackV2)
            .filter(GridstackV2.document_id.in_(document_ids))
            .all()
        )
        by_document_id = {g.document_id: g for g in gridstacks}

        missing = [d for d in document_ids if d not in by_document_id]
        if missing:
            raise ValueError(f"Tabs not found: {', '.join(missing)}")

        parent_ids = {g.parent_id for g in gridstacks}
        if len(parent_ids) != 1:
            raise ValueError("All reordered tabs must belong to the same parent level")

        parent_id = next(iter(parent_ids))

        for document_id, order in zip(document_ids, orders):
            gridstack = by_document_id[document_id]
            gridstack.position = order
            if _is_root(gridstack):
                tab = _get_root_tab(db, gridstack)
                if tab is not None:
                    tab.order = order

        db.commit()

        if parent_id is None:
            return get_root_tabs_v2(db)

        parent = db.query(GridstackV2).filter(GridstackV2.id == parent_id).first()
        if parent is None:
            return []
        return get_tab_children_v2(db, parent.document_id) or []

    except Exception:
        db.rollback()
        raise


def delete_tab_subtree_by_document_id_v2(db: Session, document_id: str) -> dict[str, Any] | None:
    try:
        gridstack = get_gridstack_by_document_id(db, document_id)
        if gridstack is None:
            return None

        # get_descendant_gridstack_ids returns a pre-order walk (each node
        # appears before its own descendants) — reversing it alone already
        # guarantees every descendant is deleted before its ancestors within
        # that set. The originally-requested node must always be deleted
        # LAST regardless, since (unlike v1's tabs_parent_lnk, which has no
        # real FK) GridstackV2.parent_id is a genuine FK constraint.
        descendant_ids = get_descendant_gridstack_ids(db, gridstack.id)
        gridstack_ids_to_delete = list(reversed(descendant_ids)) + [gridstack.id]

        deleted_tabs = []
        for gid in gridstack_ids_to_delete:
            node = db.query(GridstackV2).filter(GridstackV2.id == gid).first()
            if node is None:
                continue

            deleted_tabs.append({"id": node.id, "documentId": node.document_id, "title": node.name})

            # Descending by id, not query order: a Super Block Note's own
            # nested sub-tab components (super_blocknote_id set) always have
            # a strictly higher id than their SBN parent (the parent must
            # already exist, and be flushed to get its id, before a child
            # row referencing it can be created) — so id-descending order
            # always deletes children before the parent they reference,
            # exactly like the gridstack-level fix below for parent_id.
            components = (
                db.query(ComponentV2)
                .filter(ComponentV2.gridstack_id == gid)
                .order_by(ComponentV2.id.desc())
                .all()
            )
            for component in components:
                if component.page_content_id is not None:
                    page_content = (
                        db.query(PageContentV2)
                        .filter(PageContentV2.id == component.page_content_id)
                        .first()
                    )
                    if page_content is not None:
                        db.delete(page_content)
                db.delete(component)
                # Same self-referential-FK-without-relationship reasoning as
                # the gridstack flush below — force each delete to actually
                # execute now, in this loop's intended child-before-parent
                # order, rather than letting SQLAlchemy batch them into one
                # arbitrary-order executemany.
                db.flush()

            is_root_node = node.parent_id is None
            db.delete(node)
            # Flush immediately: without a declared ORM `relationship()` on
            # the self-referential parent_id FK, SQLAlchemy has no way to
            # know it must order same-table deletes child-before-parent —
            # it may batch multiple gridstacks deletes into one executemany
            # in arbitrary order at commit time. Flushing per-node forces
            # each delete to actually execute in the loop's intended order.
            db.flush()

            if is_root_node:
                tab = db.query(TabV2).filter(TabV2.id == node.parent_tab_id).first()
                if tab is not None:
                    db.delete(tab)

        db.commit()

        return {
            "message": "Tab subtree deleted successfully",
            "deleted_count": len(deleted_tabs),
            "deleted_tabs": deleted_tabs,
        }

    except Exception:
        db.rollback()
        raise
