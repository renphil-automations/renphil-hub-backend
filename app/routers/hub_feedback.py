"""Hub feedback submission and editor-scoped reads. No shared cache or purge."""
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from uuid import uuid4
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db_v2.database import SessionLocalV2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.schemas.hub_feedback import HubFeedbackCreate
from app.routers.product_analytics import _viewer_access, read_bounded_body
from app.services import hub_feedback_service as service
from app.services.rate_limit_service import get_rate_limit_service
from app.models.airtable import FeedbackCreate, FeedbackRecord
from app.services.bot_feedback_service import create_bot_feedback

logger = logging.getLogger(__name__)
PRIVATE = {"Cache-Control": "no-store", "Vary": "Authorization"}
UNAVAILABLE = "Hub feedback is temporarily unavailable. Please try again."
NOT_FOUND = "Feedback is unavailable."


async def _feedback_body(request: Request, limit: int):
    try:
        return await read_bounded_body(request, limit=limit)
    except HTTPException as exc:
        if exc.status_code == 413:
            raise HTTPException(413, "Feedback submission is too large.", headers=PRIVATE) from None
        raise


class FeedbackRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request: Request):
            try:
                # Reads as well as writes operate as the real caller. Do not
                # let an impersonation surface display the caller's feedback.
                markers = ("view_as", "view_as_user", "view_as_role", "view_as_scope", "anonymous")
                if any(request.query_params.get(key) for key in markers):
                    raise HTTPException(403, "Exit View As to use feedback.")
                if request.method == "POST":
                    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                        raise HTTPException(415, "Feedback requires JSON.")
                    request._body = await _feedback_body(request, limit=16 * 1024)
                response = await original(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={**(exc.headers or {}), **PRIVATE})
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid feedback request."}, status_code=422, headers=PRIVATE)
            response.headers.update(PRIVATE)
            return response
        return handle


router = APIRouter(prefix="/v2/hub-feedback", tags=["Hub Feedback"], route_class=FeedbackRoute)


def _environment(requested: str | None = None) -> str | None:
    configured = get_settings().hub_environment
    if configured == "unconfigured":
        raise HTTPException(503, "Feedback environment is not configured.", headers=PRIVATE)
    if configured == "local":
        return None if requested == "all" else (requested or "local")
    return configured


def _resources(db, current):
    return service.FeedbackResources(db, _viewer_access(db, current))


def _write(payload, current, environment):
    with SessionLocalV2() as db:
        try:
            resources = _resources(db, current)
            row, created = service.create_feedback(db, payload, user_id=current.hub_user_id,
                email=current.email, environment=environment, resources=resources)
            return resources.serialize(row), created
        except service.FeedbackTargetUnavailable:
            raise HTTPException(404, NOT_FOUND) from None
        except service.FeedbackConflict:
            raise HTTPException(409, "This submission was already used for different feedback.") from None
        except SQLAlchemyError as exc:
            db.rollback()
            logger.warning("Hub feedback write unavailable: %s", type(exc).__name__)
            raise HTTPException(503, UNAVAILABLE) from None


def _legacy_write(payload, current, environment):
    with SessionLocalV2() as db:
        try:
            if (payload.source or "").strip().lower() == "bot":
                return create_bot_feedback(db, payload, user_id=current.hub_user_id, email=current.email, environment=environment)
            resources = _resources(db, current)
            row, _ = service.create_feedback(db, HubFeedbackCreate(submission_id=uuid4(), scope="hub", message=payload.message),
                user_id=current.hub_user_id, email=current.email, environment=environment, resources=resources)
            return FeedbackRecord.model_validate({"id": row.public_id, "Id": row.id, "From": row.actor_email,
                "Message": row.message, "Date & Time": row.created_at.isoformat(), "Source": "Hub", "environment": row.environment})
        except SQLAlchemyError as exc:
            db.rollback()
            logger.warning("Feedback compatibility write unavailable: %s", type(exc).__name__)
            raise HTTPException(503, UNAVAILABLE, headers=PRIVATE) from None


async def submit_legacy_feedback(request: Request, current: CurrentHubUser):
    if any(request.query_params.get(key) for key in ("view_as", "view_as_user", "view_as_role", "view_as_scope", "anonymous")):
        raise HTTPException(403, "Exit View As to use feedback.", headers=PRIVATE)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(415, "Feedback requires JSON.", headers=PRIVATE)
    try:
        payload = FeedbackCreate.model_validate_json(await _feedback_body(request, limit=96 * 1024))
        if not payload.message.strip() or any("\x00" in value for value in (payload.message, payload.query or "", payload.response or "", payload.message_id or "")):
            raise ValueError("Invalid message")
        if payload.source and payload.source.strip().lower() not in {"hub", "bot"}:
            raise ValueError("Unknown source")
    except (ValidationError, ValueError):
        raise HTTPException(422, "Invalid feedback request.", headers=PRIVATE) from None
    limiter = get_rate_limit_service()
    retry = await limiter.check("hub_feedback", current.email)
    if retry is not None:
        raise HTTPException(429, "Please wait before submitting more feedback.", headers={**PRIVATE, "Retry-After": str(retry)})
    _environment()
    record = await run_in_threadpool(_legacy_write, payload, current, get_settings().hub_environment)
    await limiter.consume("hub_feedback", current.email)
    return record


@router.post("", status_code=201)
async def submit(payload: HubFeedbackCreate, response: Response,
                 current: CurrentHubUser = Depends(get_current_hub_user)):
    limiter = get_rate_limit_service()
    retry = await limiter.check("hub_feedback", current.email)
    if retry is not None:
        raise HTTPException(429, "Please wait before submitting more feedback.", headers={"Retry-After": str(retry)})
    _environment()
    result, created = await run_in_threadpool(_write, payload, current, get_settings().hub_environment)
    if created:
        await limiter.consume("hub_feedback", current.email)
    response.status_code = 201 if created else 200
    return result


def _read(current, environment, **filters):
    with SessionLocalV2() as db:
        try:
            resources = _resources(db, current)
            if not resources.access.full_access and not (
                resources.access.visibility is not None and resources.access.visibility.granted_edit
            ):
                raise HTTPException(403, "Feedback requires current edit access.")
            rows, total = service.list_feedback(db, resources=resources, environment=environment, **filters)
            return {"data": rows, "total": total, "environment": environment or "all",
                    "can_compare_environments": get_settings().hub_environment == "local"}
        except SQLAlchemyError as exc:
            logger.warning("Hub feedback read unavailable: %s", type(exc).__name__)
            raise HTTPException(503, UNAVAILABLE) from None


@router.get("")
def list_entries(scope: Literal["hub", "page", "component"] | None = None,
                 page_id: str | None = Query(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
                 component_link: str | None = Query(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
                 environment: Literal["local", "sandbox", "production", "unknown", "all"] | None = None,
                 limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000),
                 current: CurrentHubUser = Depends(get_current_hub_user)):
    return _read(current, _environment(environment), scope=scope, page_id=page_id,
                 component_link=component_link, limit=limit, offset=offset)


@router.get("/{feedback_id}")
def get_entry(feedback_id: str,
              environment: Literal["local", "sandbox", "production", "unknown", "all"] | None = None,
              current: CurrentHubUser = Depends(get_current_hub_user)):
    from uuid import UUID
    try:
        if str(UUID(feedback_id)) != feedback_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, NOT_FOUND) from None
    result = _read(current, _environment(environment), public_id=feedback_id, limit=1)
    if not result["data"]:
        raise HTTPException(404, NOT_FOUND)
    return result["data"][0]
