"""Hub product-usage analytics: event catalog, sanitising, ingestion, queries.

Reviewer catalog: PRODUCT_ANALYTICS_EVENTS.md (repository root).

Three rules hold everything here together:

1. The catalog is the contract. A client may only send a known `event_name`;
   `component_type` and `action` are derived from it server-side, and only the
   selection events may carry `trigger="default"`.
2. Identity and environment come from the server: the authenticated Hub user
   and `HUB_ENVIRONMENT`. Nothing the browser says about either is stored.
3. No free text is stored. Titles and labels (tab names, link text, card or
   document titles, thread text) can contain personal or sensitive
   information, so they are not part of the schema at all; any the client
   sends are ignored. A target is an opaque stable id or a catalog code, and
   properties are a fixed allowlist of enums, small integers and booleans.
   A page is stored as its route TEMPLATE (``/:nav_tab/:tab``), never as a
   path, because Hub URLs carry slugs derived from tab titles.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Any, Callable, Iterable
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db_v2.models.product_analytics import HubProductEventV2
from app.services.access_visibility_service import ViewerAccess
from app.services.product_analytics_refs import verify_identifiers


@dataclass(frozen=True)
class CatalogEntry:
    component_type: str
    action: str
    description: str
    # Selection events may also be recorded as the default a click opened.
    selection: bool = False


EVENT_CATALOG: dict[str, CatalogEntry] = {
    "navigation_tab.selected": CatalogEntry("navigation_tab", "select", "Sidebar navigation tab opened", True),
    "root_tab.selected": CatalogEntry("root_tab", "select", "Root tab opened", True),
    "view_tab.selected": CatalogEntry("view_tab", "select", "View tab (variant or first-level child) opened under a root tab", True),
    "sub_tab.selected": CatalogEntry("sub_tab", "select", "Sub-tab opened under a view tab", True),
    "blocknote_tab.selected": CatalogEntry("blocknote_tab", "select", "Super Block Note tab opened", True),
    "link.clicked": CatalogEntry("link", "open_link", "Link component item clicked"),
    "thread_item.opened": CatalogEntry("thread", "open_thread", "Thread item opened in the Thread component"),
    "airtable_metric_card.opened": CatalogEntry("airtable_metric", "open_modal", "Clickable Airtable metric card opened its records modal"),
    "quick_action_link.clicked": CatalogEntry("quick_actions", "open_link", "Quick Actions link clicked"),
    "detail_card.details_opened": CatalogEntry("detail_cards", "see_details", "Detail Cards \"See details\" opened"),
    "button.clicked": CatalogEntry("button", "open_link", "Button component clicked"),
    "calendar.add_myself": CatalogEntry("calendar", "add_myself", "Calendar \"Add myself\" succeeded"),
    "booking.schedule_now": CatalogEntry("booking", "schedule_now", "Booking \"Schedule Now\" clicked"),
    "resource_carousel.document_opened": CatalogEntry("resource_carousel", "open_document", "Resource Carousel document opened (arrow or button)"),
    "resource_carousel.scrolled": CatalogEntry("resource_carousel", "scroll", "Resource Carousel previous/next arrow clicked"),
    "threads_management.tab_selected": CatalogEntry("threads_management", "select_tab", "Threads Management Pending/History tab selected"),
    "revisions_management.tab_selected": CatalogEntry("revisions_management", "select_tab", "Revisions Management Pending/History tab selected"),
    "profile_menu.go_to_profile": CatalogEntry("profile_menu", "go_to_profile", "Profile menu \"Go to profile\" clicked"),
}

TRIGGERS = ("click", "default")
# A browser batch flushes every ~5 s; 25 clicks/defaults in one batch is
# already far beyond a person's pace.
MAX_BATCH = 25
MAX_EXPORT_ROWS = 50_000
# Client clocks drift; anything further out than this is stamped with the
# server's receive time instead of trusted.
MAX_CLOCK_SKEW = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")

# property key -> validator returning the safe value or None (dropped).
_ENUM_PROPERTIES: dict[str, frozenset[str]] = {
    "tab_kind": frozenset({"nav_tab", "root", "variant", "child", "sgs", "sbn"}),
    "destination_kind": frozenset({"external", "internal", "email", "scheme", "none"}),
    "button_style": frozenset({"arrow", "button"}),
    "link_style": frozenset({"link", "button"}),
    "direction": frozenset({"previous", "next"}),
    "view": frozenset({"pending", "history"}),
}
_INT_PROPERTIES: dict[str, tuple[int, int]] = {"depth": (0, 20), "item_index": (0, 1000)}
_BOOL_PROPERTIES = frozenset({"reselect"})


class EventRejected(ValueError):
    """One event failed validation; the rest of the batch still proceeds."""


# Hub URLs carry slugs derived from admin-authored titles (nav tab, root tab,
# view tab, sub-tab), e.g. ``/finance/q3-board-pack/jane-doe-review``. A slug
# is a title, so it is never stored: only a route TEMPLATE is. Built-in
# section names (product constants, see HomePage.tsx ``parsePathname``) are
# kept; every other segment becomes a placeholder. The browser applies the
# same template before sending (lib/productAnalytics.ts ``routeTemplate``);
# this server copy is the authority.
_STATIC_SECTIONS: dict[str, frozenset[str]] = {
    "profile": frozenset(),
    "tracking": frozenset(),
    "tracking-airtable": frozenset(),
    "tickets": frozenset(),
    "bookmarks": frozenset(),
    "workflows": frozenset(),
    "knowledge": frozenset(),
    "my-access": frozenset(),
    "admin": frozenset({"llm", "bot-management", "access-control", "roles-and-scopes"}),
    "threads": frozenset({"management", "revisions"}),
    # The built-in Dashboard nav tab; anything below it is a title slug,
    # except the legacy announcements page.
    "dashboard": frozenset({"announcements"}),
}
_STATIC_THIRD: dict[tuple[str, str], frozenset[str]] = {
    ("admin", "access-control"): frozenset({"root-access"}),
    ("dashboard", "announcements"): frozenset({"new"}),
}
MAX_ROUTE_SEGMENTS = 8


def route_template(value: Any) -> str | None:
    """The Hub route TEMPLATE of an internal path, or None if it is not one.

    Query strings and fragments are dropped, full or protocol-relative URLs
    are rejected, and no title-derived slug, id, email or token survives:
    ``/finance/q3/jane-doe`` becomes ``/:nav_tab/:tab/:tab`` and
    ``/admin/bot-management`` stays as it is.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().split("?", 1)[0].split("#", 1)[0]
    # "//host/..." is a protocol-relative URL, not a Hub path.
    if not raw.startswith("/") or raw.startswith("//") or "://" in raw or "\\" in raw:
        return None
    segments = [segment for segment in raw.split("/") if segment][:MAX_ROUTE_SEGMENTS]
    if not segments:
        return "/"
    first = segments[0].lower()
    if first in _STATIC_SECTIONS:
        out = [first]
        nav_page = first == "dashboard"
        for index, segment in enumerate(segments[1:], start=1):
            lowered = segment.lower()
            if index == 1 and lowered in _STATIC_SECTIONS[first]:
                out.append(lowered)
            elif index == 2 and lowered in _STATIC_THIRD.get((first, out[1] if len(out) > 1 else ""), frozenset()):
                out.append(lowered)
            elif nav_page and not (len(out) > 1 and out[1] == "announcements"):
                out.append(":tab")
            else:
                out.append(":segment")
        return "/" + "/".join(out)
    # Any other first segment is a nav tab's title slug.
    return "/" + "/".join([":nav_tab"] + [":tab"] * (len(segments) - 1))


def sanitize_id(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _ID_RE.match(value.strip()):
        return value.strip()
    return None


def sanitize_properties(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, raw in value.items():
        if key in _ENUM_PROPERTIES:
            if isinstance(raw, str) and raw in _ENUM_PROPERTIES[key]:
                clean[key] = raw
        elif key in _INT_PROPERTIES:
            low, high = _INT_PROPERTIES[key]
            if isinstance(raw, int) and not isinstance(raw, bool) and low <= raw <= high:
                clean[key] = raw
        elif key in _BOOL_PROPERTIES:
            if isinstance(raw, bool):
                clean[key] = raw
    return clean


def _parse_occurred_at(value: Any, received_at: datetime) -> datetime:
    parsed: datetime | None = None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        return received_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed > received_at + MAX_FUTURE_SKEW or parsed < received_at - MAX_CLOCK_SKEW:
        return received_at
    return parsed


def validate_event(
    raw: Any,
    *,
    hub_user_id: int,
    actor_email: str,
    environment: str,
    received_at: datetime,
) -> dict[str, Any]:
    """Return a row dict for `hub_product_events`, or raise EventRejected."""
    if not isinstance(raw, dict):
        raise EventRejected("event must be an object")
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not _UUID_RE.match(event_id):
        raise EventRejected("event_id must be a UUID")
    event_name = raw.get("event_name")
    entry = EVENT_CATALOG.get(event_name) if isinstance(event_name, str) else None
    if entry is None:
        raise EventRejected("unknown event_name")
    trigger = raw.get("trigger", "click")
    if trigger not in TRIGGERS:
        raise EventRejected("trigger must be click or default")
    if trigger == "default" and not entry.selection:
        raise EventRejected("only selection events may be recorded as defaults")
    page_route = route_template(raw.get("page_route"))
    if page_route is None:
        raise EventRejected("page_route must be an internal path")
    cascade_id = raw.get("cascade_id")
    if cascade_id is not None and not (isinstance(cascade_id, str) and _UUID_RE.match(cascade_id)):
        cascade_id = None
    return {
        "event_id": event_id.lower(),
        "event_name": event_name,
        "component_type": entry.component_type,
        "action": entry.action,
        "trigger": trigger,
        "cascade_id": cascade_id.lower() if cascade_id else None,
        "hub_user_id": hub_user_id,
        "actor_email": actor_email,
        "environment": environment,
        "page_route": page_route,
        "component_id": sanitize_id(raw.get("component_id")),
        "target_id": sanitize_id(raw.get("target_id")),
        "properties": sanitize_properties(raw.get("properties")),
        "occurred_at": _parse_occurred_at(raw.get("occurred_at"), received_at),
        "received_at": received_at,
    }


def _insert_ignoring_duplicates(db: Session, rows: list[dict[str, Any]]) -> int:
    """Insert rows, skipping any whose event_id already exists. Returns inserted count."""
    if not rows:
        return 0
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - only Postgres (deployed) and SQLite (tests) are used
        raise RuntimeError(f"Unsupported database dialect for product analytics: {dialect}")
    statement = (
        insert(HubProductEventV2)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(HubProductEventV2.event_id)
    )
    inserted = db.execute(statement).scalars().all()
    db.commit()
    return len(inserted)


def ingest_events(
    db: Session,
    events: list[Any],
    *,
    hub_user_id: int,
    actor_email: str,
    environment: str,
    access_for: Callable[[], ViewerAccess],
    now: datetime | None = None,
) -> dict[str, Any]:
    received_at = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    in_batch_duplicates = 0
    for index, raw in enumerate(events):
        try:
            row = validate_event(
                raw, hub_user_id=hub_user_id, actor_email=actor_email,
                environment=environment, received_at=received_at,
            )
        except EventRejected as exc:
            rejected.append({"index": index, "reason": str(exc)})
            continue
        if row["event_id"] in seen:
            in_batch_duplicates += 1
            continue
        seen.add(row["event_id"])
        rows.append(row)
    # Client ids are only kept when the Hub records confirm them AND the
    # reporter can currently view them (see product_analytics_refs);
    # unverified and inaccessible ids are dropped the same way, silently.
    if rows:
        verify_identifiers(db, rows, access_for=access_for, actor_email=actor_email)
    inserted = _insert_ignoring_duplicates(db, rows)
    return {
        "accepted": inserted,
        "duplicates": in_batch_duplicates + (len(rows) - inserted),
        "rejected": rejected,
    }


# ── Admin queries ────────────────────────────────────────────────────────


@dataclass
class EventFilters:
    date_from: date | None = None
    date_to: date | None = None
    actor_email: str | None = None
    page_route: str | None = None
    component_type: str | None = None
    event_name: str | None = None
    action: str | None = None
    environment: str | None = None
    trigger: str | None = None


def _apply_filters(statement, filters: EventFilters):
    model = HubProductEventV2
    if filters.date_from is not None:
        statement = statement.where(model.occurred_at >= datetime.combine(filters.date_from, datetime.min.time(), tzinfo=timezone.utc))
    if filters.date_to is not None:
        statement = statement.where(model.occurred_at < datetime.combine(filters.date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    for column, value in (
        (model.actor_email, filters.actor_email),
        (model.component_type, filters.component_type),
        (model.event_name, filters.event_name),
        (model.action, filters.action),
        (model.environment, filters.environment),
        (model.trigger, filters.trigger),
    ):
        normalized = (value or "").strip().lower()
        if normalized:
            statement = statement.where(func.lower(column) == normalized)
    route = (filters.page_route or "").strip()
    if route:
        # Prefix match so `/admin` covers every admin page beneath it.
        escaped = route.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(model.page_route.like(f"{escaped}%", escape="\\"))
    return statement


def _serialize(row: HubProductEventV2) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()

    return {
        "event_id": row.event_id,
        "event_name": row.event_name,
        "component_type": row.component_type,
        "action": row.action,
        "trigger": row.trigger,
        "cascade_id": row.cascade_id,
        "actor_email": row.actor_email,
        "environment": row.environment,
        "page_route": row.page_route,
        "component_id": row.component_id,
        "target_id": row.target_id,
        "properties": row.properties or {},
        "occurred_at": iso(row.occurred_at),
        "received_at": iso(row.received_at),
    }


def list_events(
    db: Session, filters: EventFilters, *, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    model = HubProductEventV2
    total = int(db.execute(_apply_filters(select(func.count()).select_from(model), filters)).scalar_one())
    rows = db.execute(
        _apply_filters(select(model), filters)
        .order_by(model.occurred_at.desc(), model.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return [_serialize(row) for row in rows], total


def summarize_events(db: Session, filters: EventFilters) -> dict[str, Any]:
    model = HubProductEventV2

    def grouped(column, limit: int | None = None) -> list[dict[str, Any]]:
        statement = _apply_filters(
            select(column.label("key"), func.count().label("count")).select_from(model), filters
        ).group_by(column).order_by(func.count().desc(), column)
        if limit:
            statement = statement.limit(limit)
        return [{"key": key, "count": int(count)} for key, count in db.execute(statement).all()]

    totals = db.execute(
        _apply_filters(
            select(
                func.count().label("events"),
                func.count(func.distinct(model.actor_email)).label("users"),
            ).select_from(model),
            filters,
        )
    ).mappings().one()
    day = func.date(model.occurred_at)
    by_day = db.execute(
        _apply_filters(select(day.label("day"), func.count().label("count")).select_from(model), filters)
        .group_by(day)
        .order_by(day)
    ).all()
    return {
        "totals": {"events": int(totals["events"] or 0), "users": int(totals["users"] or 0)},
        "by_event": grouped(model.event_name),
        "by_component": grouped(model.component_type),
        "by_trigger": grouped(model.trigger),
        "by_environment": grouped(model.environment),
        "by_user": grouped(model.actor_email, limit=25),
        "by_page": grouped(model.page_route, limit=25),
        "by_day": [{"day": str(value), "count": int(count)} for value, count in by_day],
    }


EXPORT_COLUMNS = [
    "occurred_at", "received_at", "environment", "actor_email", "event_name",
    "component_type", "action", "trigger", "page_route", "component_id",
    "target_id", "properties", "cascade_id", "event_id",
]


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in exported text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_events_csv(rows: Iterable[dict[str, Any]]) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        record = dict(row)
        record["properties"] = json.dumps(record.get("properties") or {}, sort_keys=True)
        writer.writerow({key: _csv_safe(record.get(key)) for key in EXPORT_COLUMNS})
    return output.getvalue()


def catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "event_name": name,
            "component_type": entry.component_type,
            "action": entry.action,
            "description": entry.description,
            "selection": entry.selection,
        }
        for name, entry in EVENT_CATALOG.items()
    ]


# ── Retention ────────────────────────────────────────────────────────────

DEFAULT_RETENTION_DAYS = 396  # 13 months: a full year-on-year comparison
MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
_DISABLED_VALUES = frozenset({"0", "off", "disabled", "none"})


class RetentionConfigError(ValueError):
    """PRODUCT_ANALYTICS_RETENTION_DAYS is not a usable value; nothing is deleted."""


def parse_retention_days(raw: Any) -> int | None:
    """Days to keep, or None when purging is explicitly disabled.

    Unset/blank means the default (396). `0`, `off`, `disabled` or `none`
    disable purging. Anything else must be an integer between 30 and 3650;
    otherwise RetentionConfigError is raised and nothing may be deleted.
    """
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    text = str(raw).strip().lower()
    if text == "":
        return DEFAULT_RETENTION_DAYS
    if text in _DISABLED_VALUES:
        return None
    if not re.fullmatch(r"[0-9]{1,5}", text):
        raise RetentionConfigError("PRODUCT_ANALYTICS_RETENTION_DAYS must be a whole number of days.")
    days = int(text)
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        raise RetentionConfigError(
            f"PRODUCT_ANALYTICS_RETENTION_DAYS must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}."
        )
    return days


def retention_cutoff(retention_days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)


def count_expired_events(db: Session, *, cutoff: datetime) -> int:
    return int(db.execute(
        select(func.count()).select_from(HubProductEventV2).where(HubProductEventV2.occurred_at < cutoff)
    ).scalar_one())


def purge_expired_events(
    db: Session, *, cutoff: datetime, batch_size: int = 5_000, max_batches: int | None = None,
) -> int:
    """Delete rows with `occurred_at` strictly before `cutoff`, `batch_size`
    rows per transaction, oldest first. Returns the number deleted; never
    reads or returns row contents."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    model = HubProductEventV2
    deleted = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        ids = db.execute(
            select(model.id).where(model.occurred_at < cutoff).order_by(model.id).limit(batch_size)
        ).scalars().all()
        if not ids:
            break
        result = db.execute(model.__table__.delete().where(model.id.in_(ids)).where(model.occurred_at < cutoff))
        db.commit()
        deleted += int(result.rowcount or 0)
        batches += 1
        if len(ids) < batch_size:
            break
    return deleted
