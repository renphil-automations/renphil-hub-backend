"""Hub product-usage (click) analytics: ingestion plus All-Scopes admin reads.

Ingestion is open to every authenticated Hub user and is attributed to that
user server-side. Reading, filtering and exporting reuse Bot Management's
(hub_admin, All Scopes) gate, so the audience is exactly the one that can
already see Agent analytics. The two datasets stay separate: nothing here
reads or writes `bot_telemetry_*`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import date
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import SessionLocalV2, get_db_v2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.tab import TabV2
from app.dependencies import CurrentHubUser, RbacClosures, get_current_hub_user, is_hub_admin
from app.routers.bot_management import require_bot_management_admin
from app.services import product_analytics_service as service
from app.services.access_visibility_service import ViewerAccess, resolve_viewer_access
from app.services import view_as_service
from app.services.product_analytics_refs import (
    component_root_tab_ids,
    filter_editable_engagements,
    filter_editable_events,
    resolve_names,
    summarize_locations,
)
from app.services.rate_limit_service import get_rate_limit_service

logger = logging.getLogger(__name__)

_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization"}


class _PrivateAnalyticsRoute(APIRoute):
    """Every response this router produces is private to its caller.

    Success bodies, exports and the router's own refusals (the reader gate's
    403, a Page Insights 404, a 422 "too broad", a 503) all carry the same
    no-store headers, so no browser or proxy can replay one user's analytics
    — or an existence signal — to another. FastAPI's own request-validation
    422 is produced by the application handler and is outside this router.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), **_PRIVATE_HEADERS}
                raise
            response.headers.update(_PRIVATE_HEADERS)
            return response

        return private_handler


router = APIRouter(prefix="/v2/product-analytics", tags=["Product Analytics"], route_class=_PrivateAnalyticsRoute)

_UNAVAILABLE = "Product analytics is temporarily unavailable."
# An editor read is checked row by row against live access; past the safe
# bound nothing partial is returned. Generic on purpose: it says nothing
# about how much data exists beyond the editor's own resources.
_TOO_BROAD = "This selection is too large to analyse. Narrow the date range or filters and try again."
# Bucket in `rate_limit_service.RATE_LIMITS`: the shared Upstash fixed-window
# limiter the Thread widget uses. It fails open (no Upstash config, or an
# Upstash error, means no limit), which is the right direction for analytics.
RATE_LIMIT_ACTION = "product_analytics"
# A full batch of 25 well-formed events is ~10 KB. Anything far larger is not
# a real client (browsers also cap keepalive bodies at 64 KB).
MAX_INGEST_BODY_BYTES = 64 * 1024
_TOO_LARGE = "Product analytics batch is too large."
_PAGE_INSIGHTS_NOT_FOUND = "Page insights are unavailable."
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _log_database_unavailable(operation: str, exc: SQLAlchemyError) -> None:
    """Keep production diagnostics actionable without logging filters or rows."""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    logger.warning("Product analytics %s unavailable: %s sqlstate=%s", operation, type(exc).__name__, sqlstate)


def _private_analytics_response(response: Response) -> None:
    """Prevent browser/proxy reuse of authenticated analytics responses."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"


async def read_bounded_body(request: Request, limit: int = MAX_INGEST_BODY_BYTES) -> bytes:
    """The request body, or 413 as soon as it exceeds `limit` bytes.

    A declared Content-Length over the limit is refused without reading
    anything (fast path). Without one (chunked transfer), the stream is read
    incrementally and abandoned the moment the running total passes the
    limit, so an oversized body is never fully buffered, parsed, validated,
    rate-counted or stored.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(413, detail=_TOO_LARGE)
        except ValueError:
            raise HTTPException(413, detail=_TOO_LARGE) from None
    received = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(413, detail=_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each event is validated individually (see the service), so one stale or
    # malformed event never discards the rest of a user's batch.
    events: list[Any] = Field(min_length=1, max_length=service.MAX_BATCH)


class EngagementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Active intervals are short (the browser flushes every 30 seconds) and
    # contain only a UUID, route template and numeric timestamps/duration.
    engagements: list[Any] = Field(min_length=1, max_length=service.MAX_ENGAGEMENT_BATCH)


class AnalyticsReader(NamedTuple):
    current: CurrentHubUser
    access: ViewerAccess


def require_product_analytics_reader(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> AnalyticsReader:
    """All Scopes admins see Hub-wide data; others require a live edit grant."""
    access = _viewer_access(db, current)
    if access.full_access:
        return AnalyticsReader(current, access)
    if access.visibility is not None and access.visibility.granted_edit:
        return AnalyticsReader(current, access)
    raise HTTPException(status_code=403, detail="Hub Analytics requires current edit access.")


def _viewer_access(db: Session, current: CurrentHubUser) -> ViewerAccess:
    """The reporter's live Hub access — the same three steps as
    `dependencies.get_viewer_access`, which gates the tab, canvas, Super Block
    Note and thread reads: one `RbacClosures` snapshot shared by
    `is_hub_admin` and `resolve_viewer_access`. Not used as a FastAPI
    dependency so a batch with no ids never pays for the access fold."""
    closures = RbacClosures(db)
    admin = is_hub_admin(db, current, closures=closures)
    return resolve_viewer_access(db, current.hub_user_id, is_admin=admin, closures=closures)


def _ingest_in_worker(
    events: list[Any], *, current: CurrentHubUser, environment: str,
    view_as_role: str | None = None, view_as_scope: str | None = None,
    view_as: list[str] | None = None, view_as_user: str | None = None,
) -> dict[str, Any]:
    """The whole synchronous database half of one ingest, run in ONE worker
    thread with a session created and closed in that same thread.

    The request-scoped `get_db_v2` session is never handed to the threadpool:
    a SQLAlchemy `Session` is not thread-safe, and the dependency's own
    cleanup runs on the event loop. Everything that touches the V2 database
    — resolving the reporter's live `ViewerAccess`, verifying identifiers,
    inserting (the service commits), rolling back on failure — happens here.
    `current` is plain request data (ids and email), safe to pass across.
    """
    db = SessionLocalV2()
    try:
        view_as_role_key, view_as_scope_key = _verified_view_as_context(
            db, current=current, role_candidate=view_as_role, scope_candidate=view_as_scope
        )
        context = _verified_selection_context(db, current, view_as, view_as_user)
        return service.ingest_events(
            db,
            events,
            hub_user_id=current.hub_user_id,
            actor_email=current.email,
            environment=environment,
            view_as_role_key=view_as_role_key,
            view_as_scope_key=view_as_scope_key,
            view_as_context=context,
            access_for=lambda: _viewer_access(db, current),
        )
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _ingest_engagements_in_worker(
    engagements: list[Any], *, current: CurrentHubUser, environment: str,
    view_as_role: str | None = None, view_as_scope: str | None = None,
    view_as: list[str] | None = None, view_as_user: str | None = None,
) -> dict[str, Any]:
    """Persist active-time intervals in their own short-lived DB session."""
    db = SessionLocalV2()
    try:
        view_as_role_key, view_as_scope_key = _verified_view_as_context(
            db, current=current, role_candidate=view_as_role, scope_candidate=view_as_scope
        )
        context = _verified_selection_context(db, current, view_as, view_as_user)
        return service.ingest_engagements(
            db,
            engagements,
            hub_user_id=current.hub_user_id,
            actor_email=current.email,
            environment=environment,
            view_as_role_key=view_as_role_key,
            view_as_scope_key=view_as_scope_key,
            view_as_context=context,
            access_for=lambda: _viewer_access(db, current),
        )
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _verified_selection_context(db: Session, current: CurrentHubUser, values: list[str] | None, user: str | None) -> dict | None:
    pairs = view_as_service.normalize_pair_values(values)
    email = view_as_service.normalize_email(user)
    if not pairs and not email:
        return None
    caller_id = view_as_service.resolve_user_id(db, current.email) or current.hub_user_id
    selection = view_as_service.resolve_view_as_selection(db, caller_id, pairs, email)
    return {"pairs": [{"role": target.role_key, "scope": target.scope_key} for target in selection.targets],
            "user_id": selection.user.id if selection.user else None}


def _verified_view_as_context(
    db: Session,
    *,
    current: CurrentHubUser,
    role_candidate: str | None,
    scope_candidate: str | None,
) -> tuple[str | None, str | None]:
    """Return a server-verified, internal-only View As context.

    Analytics always remains attributed to ``current`` — the real browser
    identity.  The optional adopted pair is merely diagnostic metadata.  It
    is resolved inside the worker's own session so analytics ingestion never
    borrows a request-scoped SQLAlchemy session or trusts role/scope strings
    supplied by the browser.
    """
    role_key = (role_candidate or '').strip() or None
    scope_key = (scope_candidate or '').strip() or None
    if role_key is None and scope_key is None:
        return None, None
    if role_key is None or scope_key is None:
        raise view_as_service.ViewAsError(
            400,
            "view_as_incomplete",
            "Both view_as_role and view_as_scope must be provided together.",
        )

    user_id = view_as_service.resolve_user_id(db, current.email) or current.hub_user_id
    target = view_as_service.verify_view_as(db, user_id, role_key=role_key, scope_key=scope_key)
    return target.role_key, target.scope_key


def _normalized_view_as_candidates(
    role_candidate: str | None, scope_candidate: str | None
) -> tuple[str | None, str | None]:
    """Reject incomplete View As input before rate-limit or DB work.

    This only normalizes shape. Authorization remains in
    ``_verified_view_as_context`` inside the worker session.
    """
    role_key = (role_candidate or '').strip() or None
    scope_key = (scope_candidate or '').strip() or None
    if role_key is None and scope_key is None:
        return None, None
    if role_key is None or scope_key is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "view_as_incomplete",
                "message": "Both view_as_role and view_as_scope must be provided together.",
            },
        )
    return role_key, scope_key


@router.post(
    "/events",
    # The body is read by `read_bounded_body`, not by FastAPI, so it is
    # declared here only for the OpenAPI schema.
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": IngestRequest.model_json_schema()}}}},
)
async def ingest_product_events(
    request: Request,
    current: CurrentHubUser = Depends(get_current_hub_user),
    view_as_role: str | None = Query(default=None),
    view_as_scope: str | None = Query(default=None),
    view_as: list[str] | None = Query(default=None),
    view_as_user: str | None = Query(default=None),
    view_as_anonymous: bool = Query(default=False, alias="anonymous"),
):
    # 1. Size: 413 before anything is parsed, validated, counted or stored.
    raw = await read_bounded_body(request)
    # Anonymous View As is intentionally non-telemetry mode. This server-side
    # guard covers a manually replayed otherwise-valid analytics request too;
    # it performs no parsing, rate-limit work, access fold, or database write.
    if view_as_anonymous:
        return {"accepted": 0, "duplicates": 0, "rejected": []}
    # 2. Shape.
    try:
        payload = IngestRequest.model_validate(json.loads(raw))
    except json.JSONDecodeError:
        raise HTTPException(422, detail="Body must be JSON.") from None
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_input=False, include_url=False)) from None
    view_as_role, view_as_scope = _normalized_view_as_candidates(view_as_role, view_as_scope)
    # 3. Per-user batch limit: peek before, consume only after a successful
    # ingest (the same two-phase pattern as `helpers/rate_limit.py`).
    limiter = get_rate_limit_service()
    retry_after = await limiter.check(RATE_LIMIT_ACTION, current.email)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many product analytics batches.",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        # 4. Access, identifiers and insert: one worker thread, its own session.
        result = await run_in_threadpool(
            _ingest_in_worker,
            payload.events,
            current=current,
            environment=get_settings().hub_environment,
            view_as_role=view_as_role,
            view_as_scope=view_as_scope,
            view_as=view_as, view_as_user=view_as_user,
        )
    except view_as_service.ViewAsPairsError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None
    except view_as_service.ViewAsError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None
    except SQLAlchemyError as exc:
        # Already rolled back and closed inside the worker.
        logger.warning("Product analytics ingestion failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    await limiter.consume(RATE_LIMIT_ACTION, current.email)
    return result


@router.post(
    "/engagements",
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": EngagementRequest.model_json_schema()}}}},
)
async def ingest_product_engagements(
    request: Request,
    current: CurrentHubUser = Depends(get_current_hub_user),
    view_as_role: str | None = Query(default=None),
    view_as_scope: str | None = Query(default=None),
    view_as: list[str] | None = Query(default=None),
    view_as_user: str | None = Query(default=None),
    view_as_anonymous: bool = Query(default=False, alias="anonymous"),
):
    """Record active page time. Hidden/background time is never sent by the UI."""
    raw = await read_bounded_body(request)
    if view_as_anonymous:
        return {"accepted": 0, "duplicates": 0, "rejected": []}
    try:
        payload = EngagementRequest.model_validate(json.loads(raw))
    except json.JSONDecodeError:
        raise HTTPException(422, detail="Body must be JSON.") from None
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_input=False, include_url=False)) from None
    view_as_role, view_as_scope = _normalized_view_as_candidates(view_as_role, view_as_scope)
    limiter = get_rate_limit_service()
    retry_after = await limiter.check(RATE_LIMIT_ACTION, current.email)
    if retry_after is not None:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many product analytics batches.", headers={"Retry-After": str(retry_after)})
    try:
        result = await run_in_threadpool(
            _ingest_engagements_in_worker,
            payload.engagements,
            current=current,
            environment=get_settings().hub_environment,
            view_as_role=view_as_role,
            view_as_scope=view_as_scope,
            view_as=view_as, view_as_user=view_as_user,
        )
    except view_as_service.ViewAsPairsError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None
    except view_as_service.ViewAsError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None
    except SQLAlchemyError as exc:
        # During an additive rolling deployment the click dashboard remains
        # fully usable before the active-time table exists. Treat only that
        # known schema gap as temporarily unavailable storage; do not retry
        # the same interval or log an avoidable 503 on every page transition.
        if isinstance(exc, ProgrammingError) and service.is_missing_engagement_table(exc):
            logger.info("Product analytics engagement storage is awaiting schema deployment")
            return {"accepted": 0, "duplicates": 0, "rejected": []}
        logger.warning("Product analytics engagement ingestion failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    await limiter.consume(RATE_LIMIT_ACTION, current.email)
    return result


def _filters(
    date_from: date | None = None,
    date_to: date | None = None,
    actor_email: str | None = None,
    page_route: str | None = None,
    component_type: str | None = None,
    event_name: str | None = None,
    action: str | None = None,
    environment: str | None = None,
    trigger: str | None = None,
) -> service.EventFilters:
    return service.EventFilters(
        date_from=date_from, date_to=date_to, actor_email=actor_email,
        page_route=page_route, component_type=component_type, event_name=event_name,
        action=action, environment=environment, trigger=trigger,
    )


def _deployment_filters(filters: service.EventFilters) -> service.EventFilters:
    """Constrain admin reads to this deployment outside local development.

    A browser query parameter must never allow someone on Sandbox to inspect
    Production usage (or the reverse). Local is deliberately the only
    operator environment allowed to compare all stamped environments.
    """
    configured_environment = get_settings().hub_environment
    if configured_environment == "local":
        return filters
    return replace(filters, environment=configured_environment)


def _reader_filters(filters: service.EventFilters, reader: AnalyticsReader) -> service.EventFilters:
    """Deployment lock for everyone, plus the people boundary for editors.

    A scoped editor never sees who used their resources, so a person filter
    would turn counts into an identity oracle ("did this person open my
    page?"). It is refused rather than silently ignored.
    """
    if not reader.access.full_access and (filters.actor_email or "").strip():
        raise HTTPException(status_code=403, detail="Filtering by person is limited to All Scopes admins.")
    return _deployment_filters(filters)


def _rows_for_reader(db: Session, filters: service.EventFilters, reader: AnalyticsReader) -> tuple[list[dict[str, Any]], int]:
    """The only source for a non-admin read: filter before any aggregation.

    The editor path is intentionally bounded. A larger result fails with an
    explicit "too broad" answer until a future SQL-level ACL projection is
    added; returning a partial aggregate would be misleading.
    """
    if reader.access.full_access:
        return service.list_events(db, filters, limit=service.MAX_EXPORT_ROWS, offset=0)
    rows, total = service.list_events(db, filters, limit=service.MAX_EXPORT_ROWS + 1, offset=0)
    if total > service.MAX_EXPORT_ROWS:
        raise service.AnalyticsScopeTooBroad()
    visible = filter_editable_events(db, rows, access=reader.access, actor_email=reader.current.email)
    return visible, len(visible)


def _engagement_summary(db: Session, filters: service.EventFilters, reader: AnalyticsReader) -> dict[str, Any]:
    """Active time under the filters that truthfully apply to it.

    Admins get a SQL aggregate. Editors get every interval of the filter,
    grouped by binding in SQL and then kept or dropped per group by the same
    live edit gate as click rows, so the total is complete, never a sample.
    Click-only filters (component, event, action, trigger) are listed in
    `click_only_filters` so the UI can say they do not narrow active time.
    """
    if reader.access.full_access:
        engagement = service.summarize_engagement(db, filters)
    else:
        groups = filter_editable_engagements(db, service.engagement_groups(db, filters), access=reader.access)
        engagement = service.summarize_engagement_rows(groups)
    engagement["applied_filters"] = [name for name in service.ENGAGEMENT_FILTERS if getattr(filters, name)]
    engagement["click_only_filters"] = service.click_only_filters_in_use(filters)
    return engagement


def _redact_people_for_scoped_editor(summary: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> None:
    """Remove person-level data from a scoped editor's analytics response.

    Editors can analyse the performance of resources they currently edit;
    that does not grant access to the identities of everyone who visited
    those resources.  Aggregate counts remain useful, while the admin-only
    people breakdown and per-event email are withheld.
    """
    summary["by_user"] = []
    if rows is not None:
        for row in rows:
            row["actor_email"] = None


def _location_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["event_name"], row.get("component_id"), row.get("target_id"), row["page_route"])
        group = groups.setdefault(key, {"event_name": key[0], "component_id": key[1], "target_id": key[2], "page_route": key[3], "properties": {}, "count": 0})
        group["count"] += 1
    return list(groups.values())


def _page_insights_error(status_code: int, detail: str) -> HTTPException:
    """A non-cacheable error response for a private analytics resource."""
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"Cache-Control": "no-store", "Vary": "Authorization"},
    )


# The only events whose `target_id` may place them on a page. Every other
# event joins a page through its widget's current placement, so a target id
# that happens to equal a page id (a card, a thread, a view code) never counts.
_PAGE_SELECTION_EVENTS = frozenset({"root_tab.selected", "view_tab.selected", "sub_tab.selected"})


class _PageScope(NamedTuple):
    page: dict[str, Any]
    # (TabV2 id, document id, display name): the page first, then on a root
    # page each current variant it rolls up.
    tabs: list[tuple[int, str, str]]
    # Every opaque id that addresses this scope: those tabs plus each canvas
    # (root canvas, child view tab, SGS sub-tab) beneath them.
    page_ids: set[str]
    # Opaque page id -> the document id of the tab in `tabs` that owns it.
    owner: dict[str, str]


def _page_insight_scope(db: Session, page_id: str, reader: AnalyticsReader) -> _PageScope:
    """The live, exact page scope behind a Page Insights request.

    ``page_id`` is merely an opaque client candidate.  We resolve it to a
    reachable TabV2 row and require current *edit* access before any event or
    engagement query runs.  Unknown, malformed, deleted, orphaned and
    view-only ids, and sub-tab (sub-gridstack) ids, deliberately collapse to
    the same 404 response.

    A root page rolls up its current variants for clicks and active time
    alike, so the root reports the page people are using. Intervals recorded
    before the browser bound variant time to the variant stay on the root's
    own row in ``time_by_tab``; they cannot be split after the fact.
    """
    if not _OPAQUE_ID.fullmatch(page_id):
        raise _page_insights_error(404, _PAGE_INSIGHTS_NOT_FOUND)
    page = db.execute(
        select(TabV2.id, TabV2.document_id, TabV2.title, TabV2.nav_tab_id, TabV2.parent_tab_id)
        .where(TabV2.document_id == page_id)
    ).one_or_none()
    if page is None or page.nav_tab_id is None or not reader.access.verdict(("tab", page.id)).edit:
        raise _page_insights_error(404, _PAGE_INSIGHTS_NOT_FOUND)
    # A stale tab row must not become an addressable analytics surface simply
    # because its foreign-key value happens to be present.  Variants must also
    # retain a live root parent (variants are one level deep) in the same
    # navigation tree.
    nav_exists = db.scalar(select(NavTabV2.id).where(NavTabV2.id == page.nav_tab_id))
    parent = None
    if page.parent_tab_id is not None:
        parent = db.execute(
            select(TabV2.id, TabV2.nav_tab_id, TabV2.parent_tab_id).where(TabV2.id == page.parent_tab_id)
        ).one_or_none()
    if nav_exists is None or (page.parent_tab_id is not None and (
        parent is None or parent.nav_tab_id != page.nav_tab_id or parent.parent_tab_id is not None
    )):
        raise _page_insights_error(404, _PAGE_INSIGHTS_NOT_FOUND)

    tabs = [(page.id, page.document_id, page.title or "Untitled page")]
    if page.parent_tab_id is None:
        variants = db.execute(
            select(TabV2.id, TabV2.document_id, TabV2.title)
            .where(
                TabV2.parent_tab_id == page.id,
                TabV2.nav_tab_id == page.nav_tab_id,
                TabV2.document_id.is_not(None),
            )
            .order_by(TabV2.order, TabV2.id)
        ).all()
        # Edit folds down from the root, so this is normally every variant;
        # it is still checked so the roll-up never exceeds what the reader
        # could open as a page of its own.
        tabs += [
            (variant.id, variant.document_id, variant.title or "Untitled page")
            for variant in variants
            if variant.document_id and reader.access.verdict(("tab", variant.id)).edit
        ]
    doc_by_tab = {tab_id: document_id for tab_id, document_id, _ in tabs}
    owner = {document_id: document_id for document_id in doc_by_tab.values()}
    # `parent_tab_id` is denormalized to the owning root/variant at every
    # depth, so one flat query covers each canvas beneath these tabs.
    for document_id, tab_id in db.execute(
        select(GridstackV2.document_id, GridstackV2.parent_tab_id).where(
            GridstackV2.parent_tab_id.in_(set(doc_by_tab)),
            GridstackV2.document_id.is_not(None),
        )
    ).all():
        if document_id:
            owner.setdefault(document_id, doc_by_tab[tab_id])
    return _PageScope(
        page={
            "id": page.document_id,
            "name": page.title or "Untitled page",
            "kind": "variant" if page.parent_tab_id is not None else "tab",
            "variants_included": len(tabs) - 1,
        },
        tabs=tabs,
        page_ids=set(owner),
        owner=owner,
    )


def _page_insight_rows(db: Session, filters: service.EventFilters, reader: AnalyticsReader, scope: _PageScope) -> list[dict[str, Any]]:
    """Current-placement clicks for one verified page scope.

    The SQL candidates are only the widgets currently placed beneath the
    scope's tabs, plus the three selection events whose target is one of its
    page ids, so unrelated Hub volume never reaches this read. The live edit
    filter (including the mirror/original and thread rules) is then applied,
    and each widget's current placement is re-checked before aggregation.
    """
    tab_ids = {tab_id for tab_id, _, _ in scope.tabs}
    component_ids = set(db.execute(
        select(ComponentV2.link)
        .join(GridstackV2, GridstackV2.id == ComponentV2.gridstack_id)
        .where(GridstackV2.parent_tab_id.in_(tab_ids), ComponentV2.link.is_not(None))
    ).scalars())
    rows, _ = service.list_events_for_page_scope(
        db,
        filters,
        component_ids=component_ids,
        page_ids=scope.page_ids,
        selection_events=set(_PAGE_SELECTION_EVENTS),
        limit=service.MAX_EXPORT_ROWS,
    )
    visible = filter_editable_events(db, rows, access=reader.access, actor_email=reader.current.email)
    component_pages = component_root_tab_ids(db, {row["component_id"] for row in visible if row.get("component_id")})
    return [
        row for row in visible
        if (row.get("event_name") in _PAGE_SELECTION_EVENTS and row.get("target_id") in scope.page_ids)
        or component_pages.get(row.get("component_id")) in tab_ids
    ]


@router.get("/catalog")
def get_product_event_catalog(
    response: Response,
    reader: AnalyticsReader = Depends(require_product_analytics_reader),
):
    _private_analytics_response(response)
    environment = get_settings().hub_environment
    return {
        "data": service.catalog_payload(),
        "environment": environment,
        "environment_filter_locked": environment != "local",
        # Lets the UI hide person-level controls for scoped editors; the
        # endpoints enforce the same boundary themselves.
        "people_visible": reader.access.full_access,
        "engagement_filters": list(service.ENGAGEMENT_FILTERS),
    }


@router.get("/summary")
def get_product_analytics_summary(
    response: Response,
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
    reader: AnalyticsReader = Depends(require_product_analytics_reader),
):
    _private_analytics_response(response)
    filters = _reader_filters(filters, reader)
    try:
        if reader.access.full_access:
            summary = service.summarize_events(db, filters)
            locations_source = service.location_groups(db, filters)
        else:
            rows, _ = _rows_for_reader(db, filters, reader)
            summary = service.summarize_event_rows(rows)
            locations_source = _location_groups(rows)
            _redact_people_for_scoped_editor(summary)
        summary["engagement"] = _engagement_summary(db, filters, reader)
        # The safe route templates in `by_page` are useful for exports and
        # diagnostics but not for a human investigation. Resolve a separate
        # protected, current-location view without persisting names/URLs.
        summary["by_location"] = summarize_locations(db, locations_source, access=reader.access)
        return summary
    except service.AnalyticsScopeTooBroad:
        raise HTTPException(status_code=422, detail=_TOO_BROAD) from None
    except SQLAlchemyError as exc:
        _log_database_unavailable("summary", exc)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc


@router.get("/pages/{page_id}/insights")
def get_page_insights(
    page_id: str,
    response: Response,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db_v2),
    reader: AnalyticsReader = Depends(require_product_analytics_reader),
):
    """Analytics for exactly one current, editable Hub page.

    There are deliberately no actor, component, environment or arbitrary
    route filters here.  The selected page is the complete scope; the server
    re-derives its live tree and current RBAC before each read.
    """
    _private_analytics_response(response)
    filters = _deployment_filters(service.EventFilters(date_from=date_from, date_to=date_to))
    try:
        scope = _page_insight_scope(db, page_id, reader)
        rows = _page_insight_rows(db, filters, reader, scope)
        clicks = service.summarize_event_rows(rows)
        engagement_groups = service.engagement_groups_for_tabs(db, filters, scope.page_ids)
        engagements = filter_editable_engagements(db, engagement_groups, access=reader.access)
        active_time = service.summarize_engagement_rows(engagements)
        seconds_by_tab: dict[str, int] = {}
        for group in engagements:
            owner = scope.owner.get(group.get("tab_id"))
            if owner:
                seconds_by_tab[owner] = seconds_by_tab.get(owner, 0) + int(group.get("active_seconds") or 0)
        triggers = {item["key"]: item["count"] for item in clicks["by_trigger"]}
        reselects = sum(
            1 for row in rows
            if row.get("trigger") == "click" and bool((row.get("properties") or {}).get("reselect"))
        )
        return {
            "page": scope.page,
            "environment": get_settings().hub_environment,
            "range": {"from": date_from.isoformat() if date_from else None, "to": date_to.isoformat() if date_to else None},
            "active_time": active_time,
            "time_by_tab": [
                {"id": document_id, "name": name, "active_seconds": seconds_by_tab.get(document_id, 0)}
                for _, document_id, name in scope.tabs
            ],
            "clicks": {
                "total": clicks["totals"]["events"],
                "clicks": triggers.get("click", 0),
                "defaults": triggers.get("default", 0),
                "reselects": reselects,
                "by_action": clicks["by_event"],
                "by_component": clicks["by_component"],
            },
        }
    except HTTPException:
        raise
    except service.AnalyticsScopeTooBroad:
        raise _page_insights_error(422, _TOO_BROAD) from None
    except SQLAlchemyError as exc:
        _log_database_unavailable("page insights", exc)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc


@router.get("/events")
def list_product_events(
    response: Response,
    page: int = 1,
    page_size: int = 50,
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
    reader: AnalyticsReader = Depends(require_product_analytics_reader),
):
    _private_analytics_response(response)
    bounded_page_size = min(max(page_size, 1), 200)
    bounded_page = max(page, 1)
    filters = _reader_filters(filters, reader)
    try:
        if reader.access.full_access:
            data, total = service.list_events(db, filters, limit=bounded_page_size, offset=(bounded_page - 1) * bounded_page_size)
        else:
            all_rows, total = _rows_for_reader(db, filters, reader)
            data = all_rows[(bounded_page - 1) * bounded_page_size:bounded_page * bounded_page_size]
            _redact_people_for_scoped_editor({}, data)
        # Read-time only: current names for ids this reader can open now.
        # Never stored, exported, logged or cached.
        data = resolve_names(db, data, access=reader.access)
    except service.AnalyticsScopeTooBroad:
        raise HTTPException(status_code=422, detail=_TOO_BROAD) from None
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": data, "page": bounded_page, "page_size": bounded_page_size, "total": total}


@router.get("/export/xlsx")
def export_product_usage_workbook(
    response: Response,
    include_people: bool = False,
    time_zone: str | None = None,
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
    reader: AnalyticsReader = Depends(require_product_analytics_reader),
):
    """Excel workbook with native charts, chart data and supporting detail,
    in the same style as the Agent analytics workbook. People's emails and
    current tab/widget/page/thread names are written only when
    `include_people=true` (the per-person export); never otherwise."""
    from app.routers.product_analytics_workbook import build_product_usage_workbook

    _private_analytics_response(response)
    if include_people and not reader.access.full_access:
        raise HTTPException(status_code=403, detail="Only All Scopes admins may export people.")
    filters = _reader_filters(filters, reader)
    try:
        if reader.access.full_access:
            summary = service.summarize_events(db, filters)
            events, total = service.list_events(db, filters, limit=service.MAX_EXPORT_ROWS, offset=0)
        else:
            events, total = _rows_for_reader(db, filters, reader)
            summary = service.summarize_event_rows(events)
        summary["engagement"] = _engagement_summary(db, filters, reader)
        if include_people:
            # Per-person export only (admin, checked above): current names,
            # looked up now exactly as the admin screen does. Never stored;
            # absent from the default file.
            events = resolve_names(db, events, access=reader.access)
    except service.AnalyticsScopeTooBroad:
        raise HTTPException(status_code=422, detail=_TOO_BROAD) from None
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    content = build_product_usage_workbook(
        summary=summary, events=events, total_events=total, filters=filters,
        include_people=include_people, environment=get_settings().hub_environment,
        viewer_time_zone=time_zone,
    )
    filename = "hub-product-usage-with-people.xlsx" if include_people else "hub-product-usage.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "Vary": "Authorization"},
    )


@router.get("/export")
def export_product_events_csv(
    response: Response,
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
    reader: AnalyticsReader = Depends(require_product_analytics_reader),
):
    _private_analytics_response(response)
    if not reader.access.full_access:
        # The legacy CSV includes actor emails.  The current screen uses the
        # privacy-safe Excel export, so fail closed until a no-people CSV is
        # intentionally designed rather than leaking identities by default.
        raise HTTPException(status_code=403, detail="Only All Scopes admins may export people.")
    try:
        filters = _reader_filters(filters, reader)
        data, _ = _rows_for_reader(db, filters, reader)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return Response(
        content=service.export_events_csv(data),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hub-product-usage.csv", "Cache-Control": "no-store", "Vary": "Authorization"},
    )
