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
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Any, Callable, Iterable
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.db_v2.models.product_analytics import HubProductEngagementV2, HubProductEventV2
from app.services.access_visibility_service import ViewerAccess
from app.services.product_analytics_refs import component_root_tab_ids, verify_engagement_identifiers, verify_identifiers
from app.db_v2.models.tab import TabV2


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
    "my_revisions.tab_selected": CatalogEntry("my_revisions", "select_tab", "My Revisions Needs changes/Awaiting review/Closed tab selected"),
    # plan_thread_review_merge_2026-11-11 R6: replaces threads_management /
    # revisions_management (no longer emitted; kept so stored rows resolve).
    "thread_review.tab_selected": CatalogEntry("thread_review", "select_tab", "Thread Review Pending/History tab selected"),
    "profile_menu.go_to_profile": CatalogEntry("profile_menu", "go_to_profile", "Profile menu \"Go to profile\" clicked"),
}

TRIGGERS = ("click", "default")
# A browser batch flushes every ~5 s; 25 clicks/defaults in one batch is
# already far beyond a person's pace.
MAX_BATCH = 25
MAX_ENGAGEMENT_BATCH = 10
# The browser cuts an active interval every 30 seconds and when a page is
# hidden or navigated away from. Keeping a strict server maximum prevents a
# stale tab or crafted request from reporting a whole day as active usage.
MAX_ENGAGEMENT_SECONDS = 60


def is_missing_engagement_table(exc: ProgrammingError) -> bool:
    """True only while the additive active-time table has not been deployed."""
    return "hub_product_engagements" in str(exc).lower()


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
    "view": frozenset({"pending", "history", "needs-changes", "awaiting", "closed"}),
    # Thread Review's Pending kind chip at the time of the click (R6).
    "kind": frozenset({"all", "new", "edit"}),
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
    # `management` / `revisions` only redirect to `review` now (Thread
    # Review merge); kept so a stray visit still templates readably.
    "threads": frozenset({"management", "revisions", "my-revisions", "review"}),
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


def validate_engagement(
    raw: Any,
    *,
    hub_user_id: int,
    actor_email: str,
    environment: str,
    received_at: datetime,
) -> dict[str, Any]:
    """Validate one active page-time interval without accepting free text.

    The duration is derived from the bounded client timestamps, not trusted as
    an arbitrary counter.  This is analytics rather than billing, but this
    check makes accidental duplicate/stale intervals visible and prevents a
    malformed browser from distorting totals by orders of magnitude.
    """
    if not isinstance(raw, dict):
        raise EventRejected("engagement must be an object")
    engagement_id = raw.get("engagement_id")
    if not isinstance(engagement_id, str) or not _UUID_RE.match(engagement_id):
        raise EventRejected("engagement_id must be a UUID")
    page_route = route_template(raw.get("page_route"))
    if page_route is None:
        raise EventRejected("page_route must be an internal path")
    started_at = _parse_occurred_at(raw.get("started_at"), received_at)
    ended_at = _parse_occurred_at(raw.get("ended_at"), received_at)
    seconds = int((ended_at - started_at).total_seconds())
    if not 1 <= seconds <= MAX_ENGAGEMENT_SECONDS:
        raise EventRejected("active interval must be between 1 and 60 seconds")
    supplied_seconds = raw.get("active_seconds")
    if not isinstance(supplied_seconds, int) or isinstance(supplied_seconds, bool):
        raise EventRejected("active_seconds must be an integer")
    # Browsers round at slightly different moments. Permit one second either
    # way, but retain the server-derived value as the authority.
    if abs(supplied_seconds - seconds) > 1:
        raise EventRejected("active_seconds does not match the interval")
    return {
        "engagement_id": engagement_id.lower(),
        "hub_user_id": hub_user_id,
        "actor_email": actor_email,
        "environment": environment,
        "page_route": page_route,
        # These remain opaque, client-supplied candidates until
        # `verify_engagement_identifiers` resolves them against the live Hub
        # tree and current access.  Keeping them here is essential: dropping
        # them before that verification makes every scoped editor's interval
        # indistinguishable from a legacy route-only row.
        "nav_tab_id": raw.get("nav_tab_id"),
        "tab_id": raw.get("tab_id"),
        "active_seconds": seconds,
        "started_at": started_at,
        "ended_at": ended_at,
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
    view_as_role_key: str | None = None,
    view_as_scope_key: str | None = None,
    view_as_context: dict | None = None,
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
        # Never read a simulation value from the browser event body. The
        # router supplies these only after Hub validates the adopted pair.
        row["view_as_role_key"] = view_as_role_key
        row["view_as_scope_key"] = view_as_scope_key
        row["view_as_context"] = view_as_context
        rows.append(row)
    # Client ids are only kept when the Hub records confirm them AND the
    # reporter can currently view them (see product_analytics_refs);
    # unverified and inaccessible ids are dropped the same way, silently.
    if rows:
        verify_identifiers(db, rows, access_for=access_for, actor_email=actor_email)
        # The page is determined by the server after identifier/ACL checks;
        # browser-supplied page IDs are never used. A component's current
        # placement is captured now, rather than inferred after it moves.
        links = {row["component_id"] for row in rows if row.get("component_id")}
        owners = component_root_tab_ids(db, links) if links else {}
        root_ids = set(owners.values())
        root_docs = dict(db.execute(select(TabV2.id, TabV2.document_id).where(TabV2.id.in_(root_ids))).all()) if root_ids else {}
        for row in rows:
            if row["event_name"] in {"root_tab.selected", "view_tab.selected", "sub_tab.selected"}:
                row["page_id"] = row.get("target_id")
            else:
                row["page_id"] = root_docs.get(owners.get(row.get("component_id")))
    inserted = _insert_ignoring_duplicates(db, rows)
    return {
        "accepted": inserted,
        "duplicates": in_batch_duplicates + (len(rows) - inserted),
        "rejected": rejected,
    }


def ingest_engagements(
    db: Session,
    engagements: list[Any],
    *,
    hub_user_id: int,
    actor_email: str,
    environment: str,
    view_as_role_key: str | None = None,
    view_as_scope_key: str | None = None,
    view_as_context: dict | None = None,
    access_for: Callable[[], ViewerAccess],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Insert active page-time intervals once, ignoring retry duplicates."""
    received_at = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for index, raw in enumerate(engagements):
        try:
            row = validate_engagement(
                raw, hub_user_id=hub_user_id, actor_email=actor_email,
                environment=environment, received_at=received_at,
            )
        except EventRejected as exc:
            rejected.append({"index": index, "reason": str(exc)})
            continue
        if row["engagement_id"] in seen:
            duplicates += 1
            continue
        seen.add(row["engagement_id"])
        row["view_as_role_key"] = view_as_role_key
        row["view_as_scope_key"] = view_as_scope_key
        row["view_as_context"] = view_as_context
        rows.append(row)
    if not rows:
        return {"accepted": 0, "duplicates": duplicates, "rejected": rejected}
    verify_engagement_identifiers(db, rows, access_for=access_for)
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported database dialect for product analytics: {dialect}")
    statement = (
        insert(HubProductEngagementV2)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["engagement_id"])
        .returning(HubProductEngagementV2.engagement_id)
    )
    accepted = len(db.execute(statement).scalars().all())
    db.commit()
    return {"accepted": accepted, "duplicates": duplicates + len(rows) - accepted, "rejected": rejected}


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


def list_events_for_page_scope(
    db: Session,
    filters: EventFilters,
    *,
    component_ids: set[str],
    page_ids: set[str],
    selection_events: set[str],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return only candidates that can belong to one live page scope.

    Page Insights must never read the whole environment and then filter it in
    Python: that both wastes work and turns unrelated Hub activity into a
    functional limit for a small page.  The router derives these opaque ids
    from the current page tree before calling this function; this query only
    narrows candidates and does not itself decide access.
    """
    model = HubProductEventV2
    clauses = []
    if component_ids:
        clauses.append(model.component_id.in_(component_ids))
    if page_ids and selection_events:
        clauses.append(and_(model.event_name.in_(selection_events), model.target_id.in_(page_ids)))
    if not clauses:
        return [], 0
    statement = _apply_filters(select(model), filters).where(or_(*clauses))
    total = int(db.execute(select(func.count()).select_from(statement.subquery())).scalar_one())
    if total > limit:
        raise AnalyticsScopeTooBroad()
    rows = db.execute(statement.order_by(model.occurred_at.desc(), model.id.desc()).limit(limit)).scalars().all()
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


def summarize_event_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """In-memory equivalent for an editor's ACL-filtered event set."""
    rows = list(rows)
    def groups(key: str, limit: int | None = None) -> list[dict[str, Any]]:
        values = Counter(str(row.get(key) or "") for row in rows)
        result = [{"key": value, "count": count} for value, count in values.items() if value]
        result.sort(key=lambda item: (-item["count"], item["key"]))
        return result[:limit] if limit else result
    days = Counter(str(row.get("occurred_at") or "")[:10] for row in rows)
    return {
        "totals": {"events": len(rows), "users": len({row.get("actor_email") for row in rows if row.get("actor_email")})},
        "by_event": groups("event_name"), "by_component": groups("component_type"),
        "by_trigger": groups("trigger"), "by_environment": groups("environment"),
        "by_user": groups("actor_email", 25), "by_page": groups("page_route", 25),
        "by_day": [{"day": day, "count": count} for day, count in sorted(days.items()) if day],
    }


def _engagement_statement(filters: EventFilters):
    """The common, bounded filter set for active-time intervals."""
    model = HubProductEngagementV2
    statement = select(model)
    if filters.date_from is not None:
        statement = statement.where(model.ended_at >= datetime.combine(filters.date_from, datetime.min.time(), tzinfo=timezone.utc))
    if filters.date_to is not None:
        statement = statement.where(model.ended_at < datetime.combine(filters.date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    for column, value in ((model.actor_email, filters.actor_email), (model.environment, filters.environment)):
        normalized = (value or "").strip().lower()
        if normalized:
            statement = statement.where(func.lower(column) == normalized)
    route = (filters.page_route or "").strip()
    if route:
        escaped = route.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(model.page_route.like(f"{escaped}%", escape="\\"))
    return statement


class AnalyticsScopeTooBroad(Exception):
    """A scoped editor's read is too large to check exactly; nothing partial is returned."""


# Filters with a truthful active-time meaning. Active time is page time: it
# has no component, event, action or trigger, so those filters narrow clicks
# only and are reported back as such rather than silently ignored.
ENGAGEMENT_FILTERS = ("date_from", "date_to", "actor_email", "page_route", "environment")
CLICK_ONLY_FILTERS = ("component_type", "event_name", "action", "trigger")
# Distinct (nav tab, tab, route) bindings checked for one editor read. Hub
# pages number in the hundreds, so this bounds a pathological dataset, not
# normal use; beyond it the read fails rather than under-counting.
MAX_ENGAGEMENT_GROUPS = 20_000


def click_only_filters_in_use(filters: EventFilters) -> list[str]:
    return [name for name in CLICK_ONLY_FILTERS if (getattr(filters, name) or "").strip()]


def engagement_groups(db: Session, filters: EventFilters) -> list[dict[str, Any]]:
    """Active time grouped by its page binding, for an editor's live check.

    Every interval in the filter is counted: the database sums per
    (nav_tab_id, tab_id, page_route), and the caller keeps or drops whole
    groups after verifying the binding. Grouping, not a row limit, is what
    keeps this bounded, so the editor total is exact. Raises
    AnalyticsScopeTooBroad above MAX_ENGAGEMENT_GROUPS distinct bindings.
    """
    model = HubProductEngagementV2
    statement = (
        _engagement_statement(filters)
        .with_only_columns(
            model.nav_tab_id, model.tab_id, model.page_route,
            func.coalesce(func.sum(model.active_seconds), 0), func.count(),
        )
        .group_by(model.nav_tab_id, model.tab_id, model.page_route)
        .limit(MAX_ENGAGEMENT_GROUPS + 1)
    )
    try:
        rows = db.execute(statement).all()
    except ProgrammingError as exc:
        # Same rolling-deployment allowance as `summarize_engagement`.
        if not is_missing_engagement_table(exc):
            raise
        db.rollback()
        return []
    if len(rows) > MAX_ENGAGEMENT_GROUPS:
        raise AnalyticsScopeTooBroad()
    return [
        {"nav_tab_id": nav, "tab_id": tab, "page_route": route, "active_seconds": int(seconds), "intervals": int(count)}
        for nav, tab, route, seconds, count in rows
    ]


def engagement_groups_for_tabs(
    db: Session, filters: EventFilters, tab_ids: set[str]
) -> list[dict[str, Any]]:
    """Exact active-time groups for an already verified page subtree."""
    if not tab_ids:
        return []
    model = HubProductEngagementV2
    statement = (
        _engagement_statement(filters)
        .where(model.tab_id.in_(tab_ids))
        .with_only_columns(
            model.nav_tab_id, model.tab_id, model.page_route,
            func.coalesce(func.sum(model.active_seconds), 0), func.count(),
        )
        .group_by(model.nav_tab_id, model.tab_id, model.page_route)
        .limit(MAX_ENGAGEMENT_GROUPS + 1)
    )
    try:
        rows = db.execute(statement).all()
    except ProgrammingError as exc:
        if not is_missing_engagement_table(exc):
            raise
        db.rollback()
        return []
    if len(rows) > MAX_ENGAGEMENT_GROUPS:
        raise AnalyticsScopeTooBroad()
    return [
        {"nav_tab_id": nav, "tab_id": tab, "page_route": route, "active_seconds": int(seconds), "intervals": int(count)}
        for nav, tab, route, seconds, count in rows
    ]


def engagement_people_for_tabs(db: Session, filters: EventFilters, tab_ids: set[str]) -> list[dict[str, Any]]:
    """Bounded foreground-time groups to identify people within verified tabs."""
    if not tab_ids:
        return []
    model = HubProductEngagementV2
    statement = (
        _engagement_statement(filters).where(model.tab_id.in_(tab_ids))
        .with_only_columns(model.nav_tab_id, model.tab_id, model.actor_email, func.coalesce(func.sum(model.active_seconds), 0))
        .group_by(model.nav_tab_id, model.tab_id, model.actor_email)
        .limit(MAX_ENGAGEMENT_GROUPS + 1)
    )
    try:
        rows = db.execute(statement).all()
    except ProgrammingError as exc:
        if not is_missing_engagement_table(exc):
            raise
        db.rollback()
        return []
    if len(rows) > MAX_ENGAGEMENT_GROUPS:
        raise AnalyticsScopeTooBroad()
    return [{"nav_tab_id": nav, "tab_id": tab, "actor_email": actor, "active_seconds": int(seconds)} for nav, tab, actor, seconds in rows]


def summarize_engagement_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the `engagement_groups` an editor may currently see."""
    rows = list(rows)
    page_seconds = Counter()
    for row in rows:
        route = str(row.get("page_route") or "")
        if route:
            page_seconds[route] += int(row.get("active_seconds") or 0)
    return {
        "active_seconds": sum(int(row.get("active_seconds") or 0) for row in rows),
        "intervals": sum(int(row.get("intervals") or 0) for row in rows),
        "by_page": [
            {"key": route, "active_seconds": seconds}
            for route, seconds in sorted(page_seconds.items(), key=lambda item: (-item[1], item[0]))[:25]
        ],
    }


def summarize_engagement(db: Session, filters: EventFilters) -> dict[str, Any]:
    """Aggregate active page time under the same admin filters as clicks."""
    model = HubProductEngagementV2
    statement = _engagement_statement(filters)
    try:
        totals = db.execute(
            statement.with_only_columns(
                func.coalesce(func.sum(model.active_seconds), 0).label("seconds"),
                func.count().label("intervals"),
            )
        ).mappings().one()
        pages = db.execute(
            statement.with_only_columns(
                model.page_route.label("key"), func.coalesce(func.sum(model.active_seconds), 0).label("seconds")
            ).group_by(model.page_route).order_by(func.sum(model.active_seconds).desc(), model.page_route).limit(25)
        ).all()
    except ProgrammingError as exc:
        # Rolling deployment: click analytics must stay available before the
        # additive engagement table is created. Do not mask any other DB
        # failure; only the known missing-table condition is safe to degrade.
        if not is_missing_engagement_table(exc):
            raise
        # PostgreSQL marks the whole transaction failed after an undefined
        # table query. The caller still needs to run the existing click and
        # destination queries in this session, so clear only this known,
        # additive-schema failure before returning the neutral engagement
        # result.
        db.rollback()
        return {"active_seconds": 0, "intervals": 0, "by_page": []}
    return {"active_seconds": int(totals["seconds"] or 0), "intervals": int(totals["intervals"] or 0), "by_page": [{"key": key, "active_seconds": int(seconds)} for key, seconds in pages]}


def location_groups(db: Session, filters: EventFilters) -> list[dict[str, Any]]:
    """Exact, privacy-safe event groups used to build the admin-only Pages view.

    The database stores only a route template plus verified opaque ids. This
    query deliberately returns the latter to the protected read path, where
    current names and internal destinations are resolved. It does not return
    event properties, titles, URLs, or any browser-supplied destination.
    """
    model = HubProductEventV2
    statement = _apply_filters(
        select(
            model.event_name,
            model.component_id,
            model.target_id,
            model.page_route,
            func.count().label("count"),
        ).select_from(model),
        filters,
    ).group_by(model.event_name, model.component_id, model.target_id, model.page_route)
    return [
        {
            "event_name": event_name,
            "component_id": component_id,
            "target_id": target_id,
            "page_route": page_route,
            "properties": {},
            "count": int(count),
        }
        for event_name, component_id, target_id, page_route, count in db.execute(statement).all()
    ]


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

DEFAULT_RETENTION_DAYS = 365
MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
_DISABLED_VALUES = frozenset({"0", "off", "disabled", "none"})


class RetentionConfigError(ValueError):
    """PRODUCT_ANALYTICS_RETENTION_DAYS is not a usable value; nothing is deleted."""


def parse_retention_days(raw: Any) -> int | None:
    """Days to keep, or None when purging is explicitly disabled.

    Unset/blank means the default (365). `0`, `off`, `disabled` or `none`
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


def count_expired_events(db: Session, *, cutoff: datetime, environment: str | None = None) -> int:
    statement = select(func.count()).select_from(HubProductEventV2).where(HubProductEventV2.occurred_at < cutoff)
    if environment is not None:
        statement = statement.where(HubProductEventV2.environment == environment)
    return int(db.execute(statement).scalar_one())


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
        removed = int(result.rowcount or 0)
        deleted += removed
        batches += 1
        if len(ids) < batch_size or removed == 0:
            break
    return deleted


def count_expired_engagements(db: Session, *, cutoff: datetime, environment: str | None = None) -> int:
    statement = select(func.count()).select_from(HubProductEngagementV2).where(HubProductEngagementV2.ended_at < cutoff)
    if environment is not None:
        statement = statement.where(HubProductEngagementV2.environment == environment)
    return int(db.execute(statement).scalar_one())


def purge_expired_engagements(
    db: Session, *, cutoff: datetime, batch_size: int = 5_000, max_batches: int | None = None,
) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    model = HubProductEngagementV2
    deleted = batches = 0
    while max_batches is None or batches < max_batches:
        ids = db.execute(select(model.id).where(model.ended_at < cutoff).order_by(model.id).limit(batch_size)).scalars().all()
        if not ids:
            break
        result = db.execute(model.__table__.delete().where(model.id.in_(ids)).where(model.ended_at < cutoff))
        db.commit()
        removed = int(result.rowcount or 0)
        deleted += removed
        batches += 1
        if len(ids) < batch_size or removed == 0:
            break
    return deleted


def _deleted_page_predicate(table: str):
    # NULL identifiers are deliberately retained: they are legacy/unbound
    # rows and absence of a binding does not prove a resource was deleted.
    #
    # Which resource an event belongs to, for deletion purposes:
    # * A component interaction belongs to its component. It is removed when
    #   that component no longer exists (deleting a page cascades to its
    #   components, so page deletion is covered). Its `page_id` is the page
    #   the component was on at ingest; a component that was MOVED before its
    #   old page was deleted still exists, and its history follows it to the
    #   current placement (Page Insights resolve placement at read time), so
    #   that history is kept rather than deleted with the old page.
    # * A selection event belongs to the nav tab, tab, sub-tab or Super Block
    #   Note page it selected; it is removed when that target no longer
    #   exists. `page_id` equals the target for tab selections, so the
    #   `page_id` clause only matters for rows that carry no component.
    if table == "events":
        return text("""(page_id IS NOT NULL AND component_id IS NULL AND NOT EXISTS
            (SELECT 1 FROM tabs t WHERE t.document_id = hub_product_events.page_id) AND NOT EXISTS
            (SELECT 1 FROM gridstacks g WHERE g.document_id = hub_product_events.page_id)) OR (component_id IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM components c WHERE c.link = hub_product_events.component_id)) OR (target_id IS NOT NULL AND (
            (event_name = 'navigation_tab.selected' AND NOT EXISTS
                (SELECT 1 FROM nav_tabs n WHERE n.document_id = hub_product_events.target_id)) OR
            (event_name IN ('root_tab.selected', 'view_tab.selected', 'sub_tab.selected') AND NOT EXISTS
                (SELECT 1 FROM tabs t WHERE t.document_id = hub_product_events.target_id) AND NOT EXISTS
                (SELECT 1 FROM gridstacks g WHERE g.document_id = hub_product_events.target_id)) OR
            (event_name = 'blocknote_tab.selected' AND NOT EXISTS
                (SELECT 1 FROM components c WHERE c.link = hub_product_events.target_id))))""")
    if table == "engagements":
        return text("""(nav_tab_id IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM nav_tabs n WHERE n.document_id = hub_product_engagements.nav_tab_id)) OR
            (tab_id IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM tabs t WHERE t.document_id = hub_product_engagements.tab_id) AND NOT EXISTS
                (SELECT 1 FROM gridstacks g WHERE g.document_id = hub_product_engagements.tab_id))""")
    raise ValueError("unknown analytics table")


def _scoped_deleted_predicate(model, table: str, environment: str | None):
    # Explicit parentheses are necessary when combining the OR predicate
    # with the environment restriction in SQL.
    predicate = text("(" + _deleted_page_predicate(table).text + ")")
    return and_(model.environment == environment, predicate) if environment is not None else predicate


def count_deleted_page_records(db: Session, *, table: str, environment: str | None = None) -> int:
    model = {"events": HubProductEventV2, "engagements": HubProductEngagementV2}[table]
    return int(db.execute(select(func.count()).select_from(model).where(_scoped_deleted_predicate(model, table, environment))).scalar_one())


# The Hub tables the deleted-resource predicate joins against. A database in
# which any of them is empty cannot verify a single binding: every bound row
# would look deleted. That is what a wrong DATABASE_URL_V2, a partial restore
# or a fresh schema looks like, so the cleanup refuses instead of guessing.
HUB_RESOURCE_TABLES = ("nav_tabs", "tabs", "gridstacks", "components")


class CleanupPreconditionError(RuntimeError):
    """The database cannot be used to decide what is deleted; nothing is deleted."""


def hub_resource_counts(db: Session) -> dict[str, int]:
    return {
        table: int(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in HUB_RESOURCE_TABLES
    }


def analytics_row_counts(db: Session, environment: str) -> dict[str, int]:
    return {
        table: int(db.execute(select(func.count()).select_from(model).where(model.environment == environment)).scalar_one())
        for table, model in (("events", HubProductEventV2), ("engagements", HubProductEngagementV2))
    }


def require_hub_resources(db: Session, *, environment: str | None = None) -> dict[str, int]:
    """Refuse deleted-resource cleanup unless every Hub table has rows.

    With `environment`, an environment that holds no analytics rows at all
    is exempt: it has nothing the cleanup could delete, so an empty Hub (a
    fresh deployment, a reset sandbox) is not mistaken for a wrong database.
    As soon as such an environment holds a single analytics row, the guard
    applies again.
    """
    counts = hub_resource_counts(db)
    empty = [table for table, count in counts.items() if count == 0]
    if empty and environment is not None and not any(analytics_row_counts(db, environment).values()):
        return counts
    if empty:
        raise CleanupPreconditionError(
            "this database has no rows in " + ", ".join(empty)
            + "; deleted-resource bindings cannot be verified here."
        )
    return counts


def purge_deleted_page_records(
    db: Session, *, table: str, environment: str, batch_size: int = 5_000, max_batches: int | None = None,
    _after_select: Callable[[list[int]], None] | None = None,
) -> int:
    """Delete, in batches, rows reliably tied to deleted resources.

    Each batch is selected and then deleted by one statement that re-applies
    the same predicate on the selected ids, so a row whose resource exists
    again (or whose binding changed) by the time the DELETE runs is kept; the
    predicate is evaluated inside the DELETE's own snapshot. Concurrent runs
    delete disjoint rows (each DELETE reports only what it removed) and never
    error on each other. `_after_select` is a test seam that runs between
    the selection and the deletion of a batch.
    """
    if environment not in {"local", "sandbox", "production"}:
        raise ValueError("a valid cleanup environment is required")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    require_hub_resources(db, environment=environment)
    model = {"events": HubProductEventV2, "engagements": HubProductEngagementV2}[table]
    deleted = batches = 0
    while max_batches is None or batches < max_batches:
        ids = db.execute(select(model.id).where(_scoped_deleted_predicate(model, table, environment)).order_by(model.id).limit(batch_size)).scalars().all()
        if not ids:
            break
        if _after_select is not None:
            _after_select(list(ids))
        result = db.execute(model.__table__.delete().where(model.id.in_(ids)).where(_scoped_deleted_predicate(model, table, environment)))
        db.commit()
        removed = int(result.rowcount or 0)
        deleted += removed
        batches += 1
        if removed == 0:
            # The selection no longer matches at delete time (a concurrent
            # run took it, or the resource reappeared). Never spin on it.
            break
    return deleted
