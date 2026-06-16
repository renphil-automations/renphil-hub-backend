from typing import Any
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.tab import Tab
from app.models.page_content import PageContent
from app.models.links import PageContentTabLink, TabParentLink
from app.models.versions import TabVersion, PageContentVersion


# ---------------------------------------------------------
# Default frontend access-control object
# ---------------------------------------------------------

DEFAULT_ACCESS_CONTROL = {
    "admins": {
        "roles": [
            {"name": "Hub Admin", "scope": "Hub", "program": "", "function": ""},
            {"name": "Hub Member", "scope": "Hub", "program": "", "function": ""},
            {"name": "Studio Lead", "scope": "Hub", "program": "", "function": ""},
            {"name": "Studio Member", "scope": "Hub", "program": "", "function": ""},
        ],
        "users": [{"email": "automations@renphil.org"}],
    },
    "viewers": {
        "roles": [
            {"name": "Hub Admin", "scope": "Hub", "program": "", "function": ""},
            {"name": "Hub Member", "scope": "Hub", "program": "", "function": ""},
            {"name": "Studio Lead", "scope": "Hub", "program": "", "function": ""},
            {"name": "Studio Member", "scope": "Hub", "program": "", "function": ""},
        ],
        "users": [{"email": "automations@renphil.org"}],
    },
}


# ---------------------------------------------------------
# Validation constants
# ---------------------------------------------------------

MAX_TITLE_LENGTH = 255
MAX_DOCUMENT_ID_LENGTH = 255
MAX_LOCKED_BY_LENGTH = 255

MIN_ORDER_VALUE = -2147483648
MAX_ORDER_VALUE = 2147483647


# ---------------------------------------------------------
# Small safe helpers
# ---------------------------------------------------------

def _safe_order(tab: Tab) -> int:
    return tab.order if tab.order is not None else 0


def _safe_locked(tab: Tab) -> bool:
    return bool(tab.locked) if tab.locked is not None else False


def _safe_locked_by(tab: Tab) -> str:
    return tab.locked_by or ""


def _safe_access_control(tab: Tab) -> dict[str, Any]:
    return tab.access_control or DEFAULT_ACCESS_CONTROL


def _generate_document_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None

    return value.isoformat()


def _validate_title(title: str | None) -> str | None:
    if title is None:
        return None

    clean_title = title.strip()

    if not clean_title:
        raise ValueError("Title cannot be empty")

    if len(clean_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title cannot be longer than {MAX_TITLE_LENGTH} characters")

    return clean_title


def _validate_document_id_value(
    document_id: str | None,
    field_name: str = "documentId",
) -> str | None:
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
        raise ValueError(
            f"Order must be between {MIN_ORDER_VALUE} and {MAX_ORDER_VALUE}"
        )

    return order


# ---------------------------------------------------------
# Basic lookup helpers
# ---------------------------------------------------------

def get_tab_by_id(db: Session, tab_id: int) -> Tab | None:
    return db.query(Tab).filter(Tab.id == tab_id).first()


def get_tab_by_document_id(db: Session, document_id: str) -> Tab | None:
    document_id = _validate_document_id_value(document_id, "document_id")

    return db.query(Tab).filter(Tab.document_id == document_id).first()


def get_parent_link(db: Session, tab_id: int) -> TabParentLink | None:
    return (
        db.query(TabParentLink)
        .filter(TabParentLink.tab_id == tab_id)
        .first()
    )


def get_parent_id(db: Session, tab_id: int) -> int | None:
    link = get_parent_link(db, tab_id)
    return link.inv_tab_id if link else None


def get_parent_tab(db: Session, tab_id: int) -> Tab | None:
    parent_id = get_parent_id(db, tab_id)

    if parent_id is None:
        return None

    return db.query(Tab).filter(Tab.id == parent_id).first()


def get_tab_content_model(db: Session, tab_id: int) -> PageContent | None:
    link = (
        db.query(PageContentTabLink)
        .filter(PageContentTabLink.tab_id == tab_id)
        .first()
    )

    if not link or not link.page_content_id:
        return None

    return (
        db.query(PageContent)
        .filter(PageContent.id == link.page_content_id)
        .first()
    )


# ---------------------------------------------------------
# Version-control helpers
# ---------------------------------------------------------

def _tab_snapshot(db: Session, tab: Tab) -> dict[str, Any]:
    parent = get_parent_tab(db, tab.id)

    return {
        "id": tab.id,
        "document_id": tab.document_id,
        "title": tab.title,
        "order": tab.order,
        "parent_id": parent.id if parent else None,
        "parent_document_id": parent.document_id if parent else None,
        "created_at": _json_datetime(tab.created_at),
        "updated_at": _json_datetime(tab.updated_at),
        "published_at": _json_datetime(tab.published_at),
        "created_by_id": tab.created_by_id,
        "updated_by_id": tab.updated_by_id,
        "locale": tab.locale,
        "google_source_id": tab.google_source_id,
        "source_link": getattr(tab, "source_link", None),
        "access_control": tab.access_control,
        "locked": tab.locked,
        "locked_by": tab.locked_by,
    }


def _page_content_snapshot(
    db: Session,
    page_content: PageContent,
    tab: Tab | None,
) -> dict[str, Any]:
    return {
        "id": page_content.id,
        "document_id": page_content.document_id,
        "content": page_content.content,
        "tab_id": tab.id if tab else None,
        "tab_document_id": tab.document_id if tab else None,
        "created_at": _json_datetime(page_content.created_at),
        "updated_at": _json_datetime(page_content.updated_at),
        "published_at": _json_datetime(page_content.published_at),
        "created_by_id": page_content.created_by_id,
        "updated_by_id": page_content.updated_by_id,
        "locale": page_content.locale,
    }


def save_tab_version(
    db: Session,
    tab: Tab,
    action: str = "update",
    edited_by: str | None = None,
) -> None:
    db.add(
        TabVersion(
            tab_id=tab.id,
            tab_document_id=tab.document_id,
            action=action,
            edited_by=edited_by,
            snapshot=_tab_snapshot(db, tab),
            created_at=_utc_now(),
        )
    )


def save_page_content_version(
    db: Session,
    page_content: PageContent,
    tab: Tab | None,
    action: str = "update",
    edited_by: str | None = None,
) -> None:
    db.add(
        PageContentVersion(
            page_content_id=page_content.id,
            page_content_document_id=page_content.document_id,
            tab_id=tab.id if tab else None,
            tab_document_id=tab.document_id if tab else None,
            action=action,
            edited_by=edited_by,
            snapshot=_page_content_snapshot(db, page_content, tab),
            created_at=_utc_now(),
        )
    )


# ---------------------------------------------------------
# Efficient existence helpers
# ---------------------------------------------------------

def has_content(db: Session, tab_id: int) -> bool:
    return (
        db.query(PageContentTabLink.id)
        .filter(PageContentTabLink.tab_id == tab_id)
        .first()
        is not None
    )


def has_children(db: Session, tab_id: int) -> bool:
    return (
        db.query(TabParentLink.id)
        .filter(TabParentLink.inv_tab_id == tab_id)
        .first()
        is not None
    )


def has_parent(db: Session, tab_id: int) -> bool:
    return (
        db.query(TabParentLink.id)
        .filter(TabParentLink.tab_id == tab_id)
        .first()
        is not None
    )


def get_children_ids(db: Session, tab_id: int) -> list[int]:
    return [
        row.tab_id
        for row in (
            db.query(TabParentLink.tab_id)
            .filter(TabParentLink.inv_tab_id == tab_id)
            .all()
        )
    ]


# ---------------------------------------------------------
# Bulk helper for root/children summaries
# ---------------------------------------------------------

def _format_tab_summaries(db: Session, tabs: list[Tab]) -> list[dict[str, Any]]:
    if not tabs:
        return []

    tab_ids = [tab.id for tab in tabs]

    tabs_with_content = {
        row.tab_id
        for row in (
            db.query(PageContentTabLink.tab_id)
            .filter(PageContentTabLink.tab_id.in_(tab_ids))
            .all()
        )
    }

    tabs_with_children = {
        row.inv_tab_id
        for row in (
            db.query(TabParentLink.inv_tab_id)
            .filter(TabParentLink.inv_tab_id.in_(tab_ids))
            .all()
        )
    }

    return [
        {
            "id": tab.id,
            "documentId": tab.document_id,
            "title": tab.title,
            "order": _safe_order(tab),
            "locked": _safe_locked(tab),
            "locked_by": _safe_locked_by(tab),
            "has_children": tab.id in tabs_with_children,
            "has_content": tab.id in tabs_with_content,
        }
        for tab in tabs
    ]


def _format_parent(tab: Tab | None) -> dict[str, Any] | None:
    if tab is None:
        return None

    return {
        "id": tab.id,
        "documentId": tab.document_id,
        "title": tab.title,
        "order": _safe_order(tab),
    }


def _format_page_content(content: PageContent | None) -> dict[str, Any] | None:
    if content is None:
        return None

    return {
        "documentId": content.document_id,
        "content": content.content,
    }


# ---------------------------------------------------------
# Layer 1 read API service functions
# ---------------------------------------------------------

def get_root_tabs(db: Session) -> list[dict[str, Any]]:
    child_tab_ids_subquery = db.query(TabParentLink.tab_id)

    root_tabs = (
        db.query(Tab)
        .filter(~Tab.id.in_(child_tab_ids_subquery))
        .order_by(Tab.order.asc().nullslast(), Tab.id.asc())
        .all()
    )

    return _format_tab_summaries(db, root_tabs)


def get_tab_children(db: Session, parent_document_id: str) -> list[dict[str, Any]] | None:
    parent_document_id = _validate_document_id_value(
        parent_document_id,
        "parent_document_id",
    )
    parent = get_tab_by_document_id(db, parent_document_id)

    if parent is None:
        return None

    children = (
        db.query(Tab)
        .join(TabParentLink, Tab.id == TabParentLink.tab_id)
        .filter(TabParentLink.inv_tab_id == parent.id)
        .order_by(Tab.order.asc().nullslast(), Tab.id.asc())
        .all()
    )

    return _format_tab_summaries(db, children)


def get_tab_content(db: Session, document_id: str) -> dict[str, Any] | None:
    document_id = _validate_document_id_value(document_id, "document_id")
    tab = get_tab_by_document_id(db, document_id)

    if tab is None:
        return None

    content = get_tab_content_model(db, tab.id)

    return _format_page_content(content)


def update_tab_content(
    db: Session,
    document_id: str,
    content: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | None:
    try:
        document_id = _validate_document_id_value(document_id, "document_id")
        tab = get_tab_by_document_id(db, document_id)

        if tab is None:
            return None

        page_content = get_tab_content_model(db, tab.id)

        if page_content is None:
            page_content = PageContent(
                document_id=_generate_document_id(),
                content=content if content is not None else {},
                created_at=_utc_now(),
                updated_at=_utc_now(),
                published_at=_utc_now(),
            )

            db.add(page_content)
            db.flush()

            db.add(
                PageContentTabLink(
                    tab_id=tab.id,
                    page_content_id=page_content.id,
                )
            )
        else:
            save_page_content_version(
                db=db,
                page_content=page_content,
                tab=tab,
                action="content_update",
            )

            page_content.content = content if content is not None else {}
            page_content.updated_at = _utc_now()

            if page_content.published_at is None:
                page_content.published_at = _utc_now()

        tab.updated_at = _utc_now()

        if tab.published_at is None:
            tab.published_at = _utc_now()

        db.commit()
        db.refresh(page_content)

        return _format_page_content(page_content)

    except Exception:
        db.rollback()
        raise


def update_tab_by_document_id(
    db: Session,
    document_id: str,
    title: str | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
    locked: bool | None = None,
    locked_by: str | None = None,
) -> dict[str, Any] | None:
    try:
        document_id = _validate_document_id_value(document_id, "document_id")
        title = _validate_title(title)
        order = _validate_order(order)
        locked_by = _validate_locked_by(locked_by)

        tab = get_tab_by_document_id(db, document_id)

        if tab is None:
            return None

        should_save_version = (
            title is not None
            or order is not None
            or access_control is not None
            or locked is not None
            or locked_by is not None
        )

        if should_save_version:
            save_tab_version(
                db=db,
                tab=tab,
                action="metadata_update",
                edited_by=locked_by,
            )

        if title is not None and title != tab.title:
            parent_id = get_parent_id(db, tab.id)

            existing_tab = tab_exists_under_parent(
                db=db,
                title=title,
                parent_id=parent_id,
            )

            if existing_tab and existing_tab.id != tab.id:
                raise ValueError(
                    "A tab with this title already exists under the same parent"
                )

            tab.title = title

        if order is not None:
            tab.order = order

        if access_control is not None:
            tab.access_control = access_control

        if locked is not None:
            tab.locked = locked

        if locked_by is not None:
            tab.locked_by = locked_by

        tab.updated_at = _utc_now()

        if tab.published_at is None:
            tab.published_at = _utc_now()

        db.commit()
        db.refresh(tab)

        return get_tab_workspace(db, document_id)

    except Exception:
        db.rollback()
        raise


def lock_tab_by_document_id(
    db: Session,
    document_id: str,
    locked_by: str,
) -> dict[str, Any] | None:
    try:
        document_id = _validate_document_id_value(document_id, "document_id")
        locked_by = _validate_locked_by(locked_by)

        if not locked_by:
            raise ValueError("locked_by is required")

        tab = get_tab_by_document_id(db, document_id)

        if tab is None:
            return None

        if tab.locked and tab.locked_by and tab.locked_by != locked_by:
            raise ValueError(f"Tab is already locked by {tab.locked_by}")

        tab.locked = True
        tab.locked_by = locked_by
        tab.updated_at = _utc_now()

        if tab.published_at is None:
            tab.published_at = _utc_now()

        db.commit()
        db.refresh(tab)

        return get_tab_workspace(db, document_id)

    except Exception:
        db.rollback()
        raise


def unlock_tab_by_document_id(
    db: Session,
    document_id: str,
    unlocked_by: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    try:
        document_id = _validate_document_id_value(document_id, "document_id")
        unlocked_by = _validate_locked_by(unlocked_by)

        tab = get_tab_by_document_id(db, document_id)

        if tab is None:
            return None

        if (
            not force
            and tab.locked
            and tab.locked_by
            and unlocked_by
            and tab.locked_by != unlocked_by
        ):
            raise ValueError(f"Tab is locked by {tab.locked_by}")

        tab.locked = False
        tab.locked_by = ""
        tab.updated_at = _utc_now()

        if tab.published_at is None:
            tab.published_at = _utc_now()

        db.commit()
        db.refresh(tab)

        return get_tab_workspace(db, document_id)

    except Exception:
        db.rollback()
        raise


def get_tab_workspace(db: Session, document_id: str) -> dict[str, Any] | None:
    document_id = _validate_document_id_value(document_id, "document_id")
    tab = get_tab_by_document_id(db, document_id)

    if tab is None:
        return None

    parent = get_parent_tab(db, tab.id)
    content = get_tab_content_model(db, tab.id)

    children = (
        db.query(Tab)
        .join(TabParentLink, Tab.id == TabParentLink.tab_id)
        .filter(TabParentLink.inv_tab_id == tab.id)
        .order_by(Tab.order.asc().nullslast(), Tab.id.asc())
        .all()
    )

    return {
        "id": tab.id,
        "documentId": tab.document_id,
        "title": tab.title,
        "order": _safe_order(tab),
        "parent": _format_parent(parent),
        "page_content": _format_page_content(content),
        "access_control": _safe_access_control(tab),
        "locked": _safe_locked(tab),
        "locked_by": _safe_locked_by(tab),
        "children": _format_tab_summaries(db, children),
    }


def get_tab_breadcrumb(db: Session, document_id: str) -> list[dict[str, Any]] | None:
    document_id = _validate_document_id_value(document_id, "document_id")
    tab = get_tab_by_document_id(db, document_id)

    if tab is None:
        return None

    breadcrumb = []
    visited_ids = set()

    current_tab = tab

    while current_tab is not None:
        if current_tab.id in visited_ids:
            break

        visited_ids.add(current_tab.id)

        breadcrumb.append(
            {
                "id": current_tab.id,
                "documentId": current_tab.document_id,
                "title": current_tab.title,
                "order": _safe_order(current_tab),
            }
        )

        current_tab = get_parent_tab(db, current_tab.id)

    breadcrumb.reverse()
    return breadcrumb


# ---------------------------------------------------------
# Optional schema-map helper for debugging/admin inspection
# ---------------------------------------------------------

def format_tab_schema(db: Session, tab: Tab) -> dict[str, Any]:
    return {
        "id": tab.id,
        "document_id": tab.document_id,
        "title": tab.title,
        "order": tab.order,
        "parent_id": get_parent_id(db, tab.id),
        "children_ids": get_children_ids(db, tab.id),
        "has_children": has_children(db, tab.id),
        "has_parent": has_parent(db, tab.id),
        "has_content": has_content(db, tab.id),
    }


def get_all_tabs_schema_map(db: Session) -> list[dict[str, Any]]:
    tabs = db.query(Tab).order_by(Tab.order.asc().nullslast(), Tab.id.asc()).all()
    links = db.query(TabParentLink).all()
    content_links = db.query(PageContentTabLink.tab_id).all()

    parent_by_child_id = {
        link.tab_id: link.inv_tab_id
        for link in links
    }

    children_by_parent_id: dict[int, list[int]] = {}

    for link in links:
        children_by_parent_id.setdefault(link.inv_tab_id, []).append(link.tab_id)

    tabs_with_content = {
        row.tab_id
        for row in content_links
    }

    return [
        {
            "id": tab.id,
            "document_id": tab.document_id,
            "title": tab.title,
            "order": tab.order,
            "parent_id": parent_by_child_id.get(tab.id),
            "children_ids": children_by_parent_id.get(tab.id, []),
            "has_children": tab.id in children_by_parent_id,
            "has_parent": tab.id in parent_by_child_id,
            "has_content": tab.id in tabs_with_content,
        }
        for tab in tabs
    ]


# ---------------------------------------------------------
# Legacy compatibility helpers
# ---------------------------------------------------------

def format_tab_summary(db: Session, tab: Tab) -> dict[str, Any]:
    return {
        "id": tab.id,
        "documentId": tab.document_id,
        "title": tab.title,
        "order": _safe_order(tab),
        "locked": _safe_locked(tab),
        "locked_by": _safe_locked_by(tab),
        "has_children": has_children(db, tab.id),
        "has_parent": has_parent(db, tab.id),
        "has_content": has_content(db, tab.id),
    }


def get_all_tabs(db: Session) -> list[dict[str, Any]]:
    tabs = db.query(Tab).order_by(Tab.order.asc().nullslast(), Tab.id.asc()).all()
    return [format_tab_summary(db, tab) for tab in tabs]


def get_tab_tree(db: Session) -> list[dict[str, Any]]:
    tabs = db.query(Tab).all()
    links = db.query(TabParentLink).all()

    tab_map = {
        tab.id: {
            "id": tab.id,
            "documentId": tab.document_id,
            "title": tab.title,
            "order": _safe_order(tab),
            "children": [],
        }
        for tab in tabs
    }

    child_ids = set()

    for link in links:
        child_id = link.tab_id
        parent_id = link.inv_tab_id

        if child_id in tab_map and parent_id in tab_map:
            tab_map[parent_id]["children"].append(tab_map[child_id])
            child_ids.add(child_id)

    return [
        tab
        for tab_id, tab in tab_map.items()
        if tab_id not in child_ids
    ]


def get_tab_full(db: Session, tab_id: int) -> dict[str, Any] | None:
    tab = get_tab_by_id(db, tab_id)

    if tab is None:
        return None

    content = get_tab_content_model(db, tab.id)

    children = (
        db.query(Tab)
        .join(TabParentLink, Tab.id == TabParentLink.tab_id)
        .filter(TabParentLink.inv_tab_id == tab.id)
        .order_by(Tab.order.asc().nullslast(), Tab.id.asc())
        .all()
    )

    return {
        "tab": format_tab_summary(db, tab),
        "content": content,
        "children": [format_tab_summary(db, child) for child in children],
    }


def get_tab_parent(db: Session, tab_id: int) -> dict[str, Any] | None:
    parent = get_parent_tab(db, tab_id)

    if parent is None:
        return None

    return format_tab_summary(db, parent)


# ---------------------------------------------------------
# Create/update/delete helpers
# ---------------------------------------------------------

def tab_exists_under_parent(
    db: Session,
    title: str,
    parent_id: int | None,
) -> Tab | None:
    title = _validate_title(title)

    if parent_id is None:
        child_tab_ids_subquery = db.query(TabParentLink.tab_id)

        return (
            db.query(Tab)
            .filter(
                Tab.title == title,
                ~Tab.id.in_(child_tab_ids_subquery),
            )
            .first()
        )

    return (
        db.query(Tab)
        .join(TabParentLink, Tab.id == TabParentLink.tab_id)
        .filter(
            Tab.title == title,
            TabParentLink.inv_tab_id == parent_id,
        )
        .first()
    )


def create_tab(
    db: Session,
    title: str,
    parent_document_id: str | None = None,
    content: dict[str, Any] | list[Any] | None = None,
    order: int | None = None,
    access_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        title = _validate_title(title)
        parent_document_id = _validate_document_id_value(
            parent_document_id,
            "parentDocumentId",
        )
        order = _validate_order(order)

        parent = None

        if parent_document_id is not None:
            parent = get_tab_by_document_id(db, parent_document_id)

            if parent is None:
                raise ValueError("Parent tab does not exist")

        parent_id = parent.id if parent else None

        existing_tab = tab_exists_under_parent(
            db=db,
            title=title,
            parent_id=parent_id,
        )

        if existing_tab:
            raise ValueError("A tab with this title already exists under the same parent")

        now = _utc_now()

        new_tab = Tab(
            document_id=_generate_document_id(),
            title=title,
            order=order,
            access_control=access_control or DEFAULT_ACCESS_CONTROL,
            locked=False,
            locked_by="",
            created_at=now,
            updated_at=now,
            published_at=now,
        )

        db.add(new_tab)
        db.flush()

        new_page_content = PageContent(
            document_id=_generate_document_id(),
            content=content if content is not None else {},
            created_at=now,
            updated_at=now,
            published_at=now,
        )

        db.add(new_page_content)
        db.flush()

        db.add(
            PageContentTabLink(
                tab_id=new_tab.id,
                page_content_id=new_page_content.id,
            )
        )

        if parent_id is not None:
            db.add(
                TabParentLink(
                    tab_id=new_tab.id,
                    inv_tab_id=parent_id,
                )
            )

        db.commit()
        db.refresh(new_tab)

        return {
            "id": new_tab.id,
            "documentId": new_tab.document_id,
            "title": new_tab.title,
            "order": _safe_order(new_tab),
            "locked": _safe_locked(new_tab),
            "locked_by": _safe_locked_by(new_tab),
            "has_children": False,
            "has_content": True,
        }

    except Exception:
        db.rollback()
        raise


def update_tab_title(db: Session, tab_id: int, new_title: str) -> dict[str, Any]:
    try:
        new_title = _validate_title(new_title)

        tab = get_tab_by_id(db, tab_id)

        if tab is None:
            raise ValueError("Tab not found")

        save_tab_version(
            db=db,
            tab=tab,
            action="title_update",
        )

        parent_id = get_parent_id(db, tab_id)

        existing_tab = tab_exists_under_parent(
            db=db,
            title=new_title,
            parent_id=parent_id,
        )

        if existing_tab and existing_tab.id != tab_id:
            raise ValueError("A tab with this title already exists under the same parent")

        tab.title = new_title
        tab.updated_at = _utc_now()

        if tab.published_at is None:
            tab.published_at = _utc_now()

        db.commit()
        db.refresh(tab)

        return format_tab_summary(db, tab)

    except Exception:
        db.rollback()
        raise


def get_descendant_ids(db: Session, tab_id: int) -> list[int]:
    descendants = []
    visited = set()

    def walk(current_tab_id: int) -> None:
        if current_tab_id in visited:
            return

        visited.add(current_tab_id)

        children = (
            db.query(TabParentLink)
            .filter(TabParentLink.inv_tab_id == current_tab_id)
            .all()
        )

        for child in children:
            descendants.append(child.tab_id)
            walk(child.tab_id)

    walk(tab_id)
    return descendants


def get_descendant_tabs(db: Session, tab_id: int) -> list[Tab]:
    descendant_tabs = []
    visited = set()

    def walk(current_tab_id: int) -> None:
        if current_tab_id in visited:
            return

        visited.add(current_tab_id)

        child_links = (
            db.query(TabParentLink)
            .filter(TabParentLink.inv_tab_id == current_tab_id)
            .all()
        )

        for child_link in child_links:
            child_tab = (
                db.query(Tab)
                .filter(Tab.id == child_link.tab_id)
                .first()
            )

            if child_tab:
                descendant_tabs.append(child_tab)
                walk(child_tab.id)

    walk(tab_id)
    return descendant_tabs


def delete_tab_subtree(db: Session, tab_id: int) -> dict[str, Any]:
    try:
        tab = get_tab_by_id(db, tab_id)

        if tab is None:
            raise ValueError("Tab not found")

        descendant_ids = get_descendant_ids(db, tab_id)
        tab_ids_to_delete = descendant_ids + [tab_id]

        for current_tab_id in reversed(tab_ids_to_delete):
            link = (
                db.query(PageContentTabLink)
                .filter(PageContentTabLink.tab_id == current_tab_id)
                .first()
            )

            page_content_id = link.page_content_id if link else None

            db.query(TabParentLink).filter(
                (TabParentLink.tab_id == current_tab_id)
                | (TabParentLink.inv_tab_id == current_tab_id)
            ).delete(synchronize_session=False)

            if link:
                db.delete(link)

            if page_content_id:
                page_content = (
                    db.query(PageContent)
                    .filter(PageContent.id == page_content_id)
                    .first()
                )

                if page_content:
                    db.delete(page_content)

            current_tab = (
                db.query(Tab)
                .filter(Tab.id == current_tab_id)
                .first()
            )

            if current_tab:
                db.delete(current_tab)

        db.commit()

        return {
            "message": "Tab subtree deleted successfully",
            "deleted_tab_ids": tab_ids_to_delete,
        }

    except Exception:
        db.rollback()
        raise


def delete_tab_subtree_by_document_id(
    db: Session,
    document_id: str,
) -> dict[str, Any] | None:
    try:
        document_id = _validate_document_id_value(document_id, "document_id")
        tab = get_tab_by_document_id(db, document_id)

        if tab is None:
            return None

        descendant_tabs = get_descendant_tabs(db, tab.id)

        tabs_to_delete = list(reversed(descendant_tabs)) + [tab]

        deleted_tabs = []

        for current_tab in tabs_to_delete:
            deleted_tabs.append(
                {
                    "id": current_tab.id,
                    "documentId": current_tab.document_id,
                    "title": current_tab.title,
                }
            )

            content_link = (
                db.query(PageContentTabLink)
                .filter(PageContentTabLink.tab_id == current_tab.id)
                .first()
            )

            page_content_id = content_link.page_content_id if content_link else None

            db.query(TabParentLink).filter(
                (TabParentLink.tab_id == current_tab.id)
                | (TabParentLink.inv_tab_id == current_tab.id)
            ).delete(synchronize_session=False)

            if content_link:
                db.delete(content_link)

            if page_content_id:
                page_content = (
                    db.query(PageContent)
                    .filter(PageContent.id == page_content_id)
                    .first()
                )

                if page_content:
                    db.delete(page_content)

            db.delete(current_tab)

        db.commit()

        return {
            "message": "Tab subtree deleted successfully",
            "deleted_count": len(deleted_tabs),
            "deleted_tabs": deleted_tabs,
        }

    except Exception:
        db.rollback()
        raise


def is_descendant_of(
    db: Session,
    possible_descendant_id: int,
    possible_ancestor_id: int,
) -> bool:
    descendant_ids = get_descendant_ids(db, possible_ancestor_id)
    return possible_descendant_id in descendant_ids


def move_tab_by_document_id(
    db: Session,
    document_id: str,
    new_parent_document_id: str | None = None,
    order: int | None = None,
) -> dict[str, Any] | None:
    try:
        document_id = _validate_document_id_value(document_id, "document_id")
        new_parent_document_id = _validate_document_id_value(
            new_parent_document_id,
            "newParentDocumentId",
        )
        order = _validate_order(order)

        tab = get_tab_by_document_id(db, document_id)

        if tab is None:
            return None

        new_parent = None

        if new_parent_document_id is not None:
            new_parent = get_tab_by_document_id(db, new_parent_document_id)

            if new_parent is None:
                raise ValueError("New parent tab does not exist")

            if new_parent.id == tab.id:
                raise ValueError("A tab cannot be moved under itself")

            if is_descendant_of(
                db=db,
                possible_descendant_id=new_parent.id,
                possible_ancestor_id=tab.id,
            ):
                raise ValueError("A tab cannot be moved under one of its descendants")

        save_tab_version(
            db=db,
            tab=tab,
            action="move",
        )

        parent_link = get_parent_link(db, tab.id)

        if new_parent is None:
            if parent_link:
                db.delete(parent_link)
        else:
            if parent_link:
                parent_link.inv_tab_id = new_parent.id
            else:
                db.add(
                    TabParentLink(
                        tab_id=tab.id,
                        inv_tab_id=new_parent.id,
                    )
                )

        if order is not None:
            tab.order = order

        tab.updated_at = _utc_now()

        if tab.published_at is None:
            tab.published_at = _utc_now()

        db.commit()
        db.refresh(tab)

        return get_tab_workspace(db, document_id)

    except Exception:
        db.rollback()
        raise


def reorder_tabs_by_document_id(
    db: Session,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        if not items:
            raise ValueError("Reorder items cannot be empty")

        document_ids = [
            _validate_document_id_value(item["documentId"], "documentId")
            for item in items
        ]

        orders = [
            _validate_order(item["order"])
            for item in items
        ]

        if len(document_ids) != len(set(document_ids)):
            raise ValueError("Duplicate documentId values are not allowed")

        tabs = (
            db.query(Tab)
            .filter(Tab.document_id.in_(document_ids))
            .all()
        )

        tabs_by_document_id = {
            tab.document_id: tab
            for tab in tabs
        }

        missing_document_ids = [
            document_id
            for document_id in document_ids
            if document_id not in tabs_by_document_id
        ]

        if missing_document_ids:
            raise ValueError(
                f"Tabs not found: {', '.join(missing_document_ids)}"
            )

        parent_ids = {
            get_parent_id(db, tab.id)
            for tab in tabs
        }

        if len(parent_ids) != 1:
            raise ValueError("All reordered tabs must belong to the same parent level")

        parent_id = next(iter(parent_ids))

        for document_id, order in zip(document_ids, orders):
            tab = tabs_by_document_id[document_id]

            if tab.order != order:
                save_tab_version(
                    db=db,
                    tab=tab,
                    action="reorder",
                )

            tab.order = order
            tab.updated_at = _utc_now()

            if tab.published_at is None:
                tab.published_at = _utc_now()

        db.commit()

        if parent_id is None:
            return get_root_tabs(db)

        parent_tab = db.query(Tab).filter(Tab.id == parent_id).first()

        if parent_tab is None:
            return []

        children = (
            db.query(Tab)
            .join(TabParentLink, Tab.id == TabParentLink.tab_id)
            .filter(TabParentLink.inv_tab_id == parent_tab.id)
            .order_by(Tab.order.asc().nullslast(), Tab.id.asc())
            .all()
        )

        return _format_tab_summaries(db, children)

    except Exception:
        db.rollback()
        raise