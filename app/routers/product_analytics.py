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
from dataclasses import replace
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import SessionLocalV2, get_db_v2
from app.dependencies import CurrentHubUser, RbacClosures, get_current_hub_user, is_hub_admin
from app.routers.bot_management import require_bot_management_admin
from app.services import product_analytics_service as service
from app.services.access_visibility_service import ViewerAccess, resolve_viewer_access
from app.services.product_analytics_refs import resolve_names, summarize_locations
from app.services.rate_limit_service import get_rate_limit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2/product-analytics", tags=["Product Analytics"])

_UNAVAILABLE = "Product analytics is temporarily unavailable."
# Bucket in `rate_limit_service.RATE_LIMITS`: the shared Upstash fixed-window
# limiter the Thread widget uses. It fails open (no Upstash config, or an
# Upstash error, means no limit), which is the right direction for analytics.
RATE_LIMIT_ACTION = "product_analytics"
# A full batch of 25 well-formed events is ~10 KB. Anything far larger is not
# a real client (browsers also cap keepalive bodies at 64 KB).
MAX_INGEST_BODY_BYTES = 64 * 1024
_TOO_LARGE = "Product analytics batch is too large."


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


def _viewer_access(db: Session, current: CurrentHubUser) -> ViewerAccess:
    """The reporter's live Hub access — the same three steps as
    `dependencies.get_viewer_access`, which gates the tab, canvas, Super Block
    Note and thread reads: one `RbacClosures` snapshot shared by
    `is_hub_admin` and `resolve_viewer_access`. Not used as a FastAPI
    dependency so a batch with no ids never pays for the access fold."""
    closures = RbacClosures(db)
    admin = is_hub_admin(db, current, closures=closures)
    return resolve_viewer_access(db, current.hub_user_id, is_admin=admin, closures=closures)


def _ingest_in_worker(events: list[Any], *, current: CurrentHubUser, environment: str) -> dict[str, Any]:
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
        return service.ingest_events(
            db,
            events,
            hub_user_id=current.hub_user_id,
            actor_email=current.email,
            environment=environment,
            access_for=lambda: _viewer_access(db, current),
        )
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@router.post(
    "/events",
    # The body is read by `read_bounded_body`, not by FastAPI, so it is
    # declared here only for the OpenAPI schema.
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": IngestRequest.model_json_schema()}}}},
)
async def ingest_product_events(
    request: Request,
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    # 1. Size: 413 before anything is parsed, validated, counted or stored.
    raw = await read_bounded_body(request)
    # 2. Shape.
    try:
        payload = IngestRequest.model_validate(json.loads(raw))
    except json.JSONDecodeError:
        raise HTTPException(422, detail="Body must be JSON.") from None
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_input=False, include_url=False)) from None
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
        )
    except SQLAlchemyError as exc:
        # Already rolled back and closed inside the worker.
        logger.warning("Product analytics ingestion failed: %s", type(exc).__name__)
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


@router.get("/catalog", dependencies=[Depends(require_bot_management_admin)])
def get_product_event_catalog():
    environment = get_settings().hub_environment
    return {
        "data": service.catalog_payload(),
        "environment": environment,
        "environment_filter_locked": environment != "local",
    }


@router.get("/summary", dependencies=[Depends(require_bot_management_admin)])
def get_product_analytics_summary(
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
):
    try:
        filters = _deployment_filters(filters)
        summary = service.summarize_events(db, filters)
        # The safe route templates in `by_page` are useful for exports and
        # diagnostics but not for a human investigation. Resolve a separate
        # protected, current-location view without persisting names/URLs.
        summary["by_location"] = summarize_locations(db, service.location_groups(db, filters))
        return summary
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc


@router.get("/events", dependencies=[Depends(require_bot_management_admin)])
def list_product_events(
    page: int = 1,
    page_size: int = 50,
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
):
    bounded_page_size = min(max(page_size, 1), 200)
    bounded_page = max(page, 1)
    try:
        filters = _deployment_filters(filters)
        data, total = service.list_events(
            db, filters, limit=bounded_page_size, offset=(bounded_page - 1) * bounded_page_size,
        )
        # Read-time only: current names for recognised ids, for this admin
        # response. Never stored, exported, logged or cached.
        data = resolve_names(db, data)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": data, "page": bounded_page, "page_size": bounded_page_size, "total": total}


@router.get("/export/xlsx", dependencies=[Depends(require_bot_management_admin)])
def export_product_usage_workbook(
    include_people: bool = False,
    time_zone: str | None = None,
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
):
    """Excel workbook with native charts, chart data and supporting detail,
    in the same style as the Agent analytics workbook. People's emails and
    current tab/widget/page/thread names are written only when
    `include_people=true` (the per-person export); never otherwise."""
    from app.routers.product_analytics_workbook import build_product_usage_workbook

    try:
        filters = _deployment_filters(filters)
        summary = service.summarize_events(db, filters)
        events, total = service.list_events(db, filters, limit=service.MAX_EXPORT_ROWS, offset=0)
        if include_people:
            # Per-person export only: current names, looked up now exactly as
            # the admin screen does. Never stored; absent from the default file.
            events = resolve_names(db, events)
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/export", dependencies=[Depends(require_bot_management_admin)])
def export_product_events_csv(
    filters: service.EventFilters = Depends(_filters),
    db: Session = Depends(get_db_v2),
):
    try:
        filters = _deployment_filters(filters)
        data, _ = service.list_events(db, filters, limit=service.MAX_EXPORT_ROWS, offset=0)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return Response(
        content=service.export_events_csv(data),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hub-product-usage.csv"},
    )
