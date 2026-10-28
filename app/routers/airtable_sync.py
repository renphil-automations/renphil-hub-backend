"""Airtable → Hub sync router (AI Docs/plan_airtable_sync_v2_2026-10-21.md §9;
v1 plan §7).

Hub Admins only, for every endpoint.

  GET  /v2/airtable-sync/settings   the schedule
  PUT  /v2/airtable-sync/settings
  GET  /v2/airtable-sync/mappings   Designation → role, plus the live
                                     Designation choices from Airtable
  PUT  /v2/airtable-sync/mappings   full replace
  POST /v2/airtable-sync/runs       start a run (202) or 409 if one is going
  GET  /v2/airtable-sync/runs       history, newest first
  GET  /v2/airtable-sync/runs/{id}  one run (the tab polls it)
  GET  /v2/airtable-sync/runs/{id}/log   a run's log, paginated
  GET  /v2/airtable-sync/status     schedule, current/last run, open items
  GET  /v2/airtable-sync/items      conflicts / warnings, paginated
  POST /v2/airtable-sync/items/{id}/resolve   answer one (409 during a run)
  POST /v2/airtable-sync/items/bulk-resolve   one choice for several items,
                                     in this request
  POST /v2/airtable-sync/bulk-jobs  the same in the background (202); the
                                     tab follows it through /status
  GET  /v2/airtable-sync/bulk-jobs/{id}        one job's progress
  POST /v2/airtable-sync/bulk-jobs/{id}/stop   stop before its next item

The scheduler tick (`POST /v2/airtable-sync/tick`) is token-only, so it lives
in routers/airtable_sync_tick.py, outside this router's Hub Admin gate.
"""

from __future__ import annotations

import logging
from datetime import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user, get_current_user, require_hub_admin
from app.schemas.airtable_sync import (
    BulkJobAPIResponse,
    BulkResolveAPIResponse,
    BulkResolveRequest,
    StartBulkJobRequest,
    DesignationMappingsAPIResponse,
    ItemStatusFilter,
    LogEntity,
    ReplaceDesignationMappingsRequest,
    ResolveItemAPIResponse,
    ResolveItemRequest,
    SyncItemsAPIResponse,
    StartRunAPIResponse,
    StartRunRequest,
    SyncLogAPIResponse,
    SyncRunAPIResponse,
    SyncRunListAPIResponse,
    SyncSettingsAPIResponse,
    SyncStatusAPIResponse,
    UpdateSyncSettingsRequest,
)
from app.services.airtable_sync import airtable_reader, bulk_jobs, resolutions, runs_service, settings_service
from app.services.airtable_sync.bulk_jobs import BulkJobRunner, get_bulk_job_runner
from app.services.airtable_sync.runner import SyncRunner, get_sync_runner
from app.services.airtable_sync.settings_service import AirtableSyncError, MappingEntry

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v2/airtable-sync",
    tags=["Airtable Sync"],
    dependencies=[Depends(get_current_user), Depends(require_hub_admin)],
)

FORBIDDEN_RESPONSE = {403: {"description": "Hub Admin access required"}}
CONFLICT_RESPONSE = {409: {"description": "A sync rule was violated"}}


def _conflict(error: AirtableSyncError) -> HTTPException:
    """Same 409 shape as the RBAC routers: `{"code", "message", ...}`."""
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": error.message, **error.details},
    )


# ---------------------------------------------------------
# Schedule
# ---------------------------------------------------------


@router.get(
    "/settings",
    response_model=SyncSettingsAPIResponse,
    summary="Get the sync schedule",
    responses=FORBIDDEN_RESPONSE,
)
def get_sync_settings(db: Session = Depends(get_db_v2)):
    row = settings_service.get_settings_row(db)
    db.commit()  # persists the row if this read created it
    return {"data": settings_service.serialize_settings(row)}


@router.put(
    "/settings",
    response_model=SyncSettingsAPIResponse,
    summary="Replace the sync schedule",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
)
def update_sync_settings(
    request: UpdateSyncSettingsRequest,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    hours, minutes = (int(part) for part in request.run_time.split(":"))
    try:
        data = settings_service.update_settings(
            db,
            schedule_enabled=request.schedule_enabled,
            frequency=request.frequency,
            run_time=time(hours, minutes),
            run_weekday=request.run_weekday,
            timezone_name=request.timezone.strip(),
            actor_email=current.email,
        )
    except AirtableSyncError as e:
        raise _conflict(e)
    db.commit()
    return {"data": data}


# ---------------------------------------------------------
# Designation → role mapping
# ---------------------------------------------------------


def _mappings_payload(db: Session) -> dict:
    """The mapping rows plus the live Designation choices. An Airtable
    failure never fails the request: the mapping is still shown (and
    editable), with `choices_error` saying why the choices are missing."""
    mappings = settings_service.list_mappings(db)
    choices: list[str] = []
    choices_error: str | None = None
    try:
        choices = airtable_reader.designation_choices()
    except airtable_reader.AirtableSyncConfigError as exc:
        choices_error = f"Airtable is not configured for the sync: {exc}."
    except LookupError as exc:
        choices_error = str(exc)
    except Exception:  # network / HTTP / auth — details go to the log only
        logger.exception("Could not read the Program Roles Designation choices from Airtable")
        choices_error = "Could not read the Designation choices from Airtable."
    mapped = {settings_service.normalize_designation(m["designation"]) for m in mappings}
    return {
        "mappings": mappings,
        "designation_choices": choices,
        "unmapped_choices": [c for c in choices if settings_service.normalize_designation(c) not in mapped],
        "choices_error": choices_error,
    }


@router.get(
    "/mappings",
    response_model=DesignationMappingsAPIResponse,
    summary="Get the Designation → role mapping",
    responses=FORBIDDEN_RESPONSE,
)
def get_designation_mappings(db: Session = Depends(get_db_v2)):
    return {"data": _mappings_payload(db)}


@router.put(
    "/mappings",
    response_model=DesignationMappingsAPIResponse,
    summary="Replace the Designation → role mapping",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
)
def replace_designation_mappings(
    request: ReplaceDesignationMappingsRequest,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    entries = [MappingEntry(m.designation, m.action, m.role_id) for m in request.mappings]
    try:
        settings_service.replace_mappings(db, entries, actor_email=current.email)
    except AirtableSyncError as e:
        raise _conflict(e)
    db.commit()
    return {"data": _mappings_payload(db)}


# ---------------------------------------------------------
# Runs
# ---------------------------------------------------------

NOT_FOUND_RESPONSE = {404: {"description": "No such run"}}


@router.post(
    "/runs",
    status_code=202,
    response_model=StartRunAPIResponse,
    summary="Start a run (or a preview)",
    responses={**FORBIDDEN_RESPONSE, 409: {"description": "A run is already in progress"}},
)
def start_run(
    request: StartRunRequest,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
    runner: SyncRunner = Depends(get_sync_runner),
):
    """Takes the one-run lock and returns at once; the run continues in the
    background. Poll `GET /runs/{id}` for its status."""
    try:
        started = runner.start(
            db,
            steps=request.steps,
            preview=request.preview,
            started_by_user_id=current.hub_user_id,
            started_by_email=current.email,
        )
    except AirtableSyncError as e:
        raise _conflict(e)
    # Commit BEFORE launching: the run's thread reads with its own session.
    db.commit()
    runner.launch(started)
    return {"data": {"run_id": started.run_id}}


@router.get(
    "/runs",
    response_model=SyncRunListAPIResponse,
    summary="Run history, newest first",
    responses=FORBIDDEN_RESPONSE,
)
def list_runs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db_v2)):
    return {"data": runs_service.list_runs(db, limit)}


@router.get(
    "/runs/{run_id}",
    response_model=SyncRunAPIResponse,
    summary="One run",
    responses={**FORBIDDEN_RESPONSE, **NOT_FOUND_RESPONSE},
)
def get_run(run_id: int, db: Session = Depends(get_db_v2)):
    run = runs_service.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"data": runs_service.serialize_run(run)}


@router.get(
    "/runs/{run_id}/log",
    response_model=SyncLogAPIResponse,
    summary="A run's log, oldest first, paginated",
    responses={**FORBIDDEN_RESPONSE, **NOT_FOUND_RESPONSE},
)
def get_run_log(
    run_id: int,
    entity: LogEntity | None = None,
    action: str | None = Query(None, max_length=48),
    q: str | None = Query(None, max_length=200),
    cursor: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db_v2),
):
    if runs_service.get_run(db, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "data": runs_service.list_log(db, run_id, entity=entity, action=action, q=q, cursor=cursor, limit=limit)
    }


@router.get(
    "/status",
    response_model=SyncStatusAPIResponse,
    summary="The tab's status: schedule, current and last run, open items",
    responses=FORBIDDEN_RESPONSE,
)
def get_sync_status(db: Session = Depends(get_db_v2), runner: SyncRunner = Depends(get_sync_runner)):
    data = runs_service.status_payload(db, runner.settings_provider())
    db.commit()  # persists the settings row if this read created it
    return {"data": data}


# ---------------------------------------------------------
# Items: conflicts and warnings (plan v2 §6; v1 plan §5.4)
# ---------------------------------------------------------

ITEM_NOT_FOUND_RESPONSE = {404: {"description": "No such item"}}


@router.get(
    "/items",
    response_model=SyncItemsAPIResponse,
    summary="Conflicts and warnings, oldest first, paginated",
    responses=FORBIDDEN_RESPONSE,
)
def list_items(
    status: ItemStatusFilter = "open",
    kind: str | None = Query(None, max_length=48),
    entity: LogEntity | None = None,
    q: str | None = Query(None, max_length=200),
    cursor: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db_v2),
):
    return {
        "data": resolutions.list_items(
            db,
            status=None if status == "all" else status,
            kind=kind,
            entity=entity,
            q=q,
            cursor=cursor,
            limit=limit,
        )
    }


@router.post(
    "/items/{item_id}/resolve",
    response_model=ResolveItemAPIResponse,
    summary="Answer one conflict or warning",
    responses={
        **FORBIDDEN_RESPONSE,
        **ITEM_NOT_FOUND_RESPONSE,
        409: {"description": "A run is in progress, or the choice can't be applied"},
    },
)
def resolve_item(
    item_id: int,
    request: ResolveItemRequest,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    """Applies the choice at once (from the Airtable values of the latest
    run) and logs it. Refused with 409 while a run is going."""
    post = resolutions.PostCommit()
    try:
        data = resolutions.resolve_item(db, item_id, choice=request.choice, data=request.data, current=current, post_commit=post)
    except AirtableSyncError as e:
        db.rollback()
        raise _conflict(e)
    if data is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Item not found")
    db.commit()
    post.run(db)  # old headshot objects, then the lock sweep: after the commit
    return {"data": data}


@router.post(
    "/items/bulk-resolve",
    response_model=BulkResolveAPIResponse,
    summary="Answer several items with one choice (Accept all / Keep all)",
    responses={**FORBIDDEN_RESPONSE, 409: {"description": "A run is in progress"}},
)
def bulk_resolve_items(
    request: BulkResolveRequest,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    """One transaction per item, in id order, stopping at the first error:
    `failed` names it (with the 409 it would have answered) and
    `not_attempted` lists the rest. 409 if a run is in progress at the start;
    one starting midway stops the loop like any other error."""
    try:
        data = resolutions.bulk_resolve(
            db, item_ids=request.item_ids, run_id=request.run_id, kind=request.kind, choice=request.choice, current=current
        )
    except AirtableSyncError as e:
        db.rollback()
        raise _conflict(e)
    return {"data": data}


# ---------------------------------------------------------
# Background bulk answers (owner, 2026-10-27)
# ---------------------------------------------------------

JOB_NOT_FOUND_RESPONSE = {404: {"description": "No such bulk job"}}


@router.post(
    "/bulk-jobs",
    status_code=202,
    response_model=BulkJobAPIResponse,
    summary="Answer several items with one choice, in the background",
    responses={**FORBIDDEN_RESPONSE, 409: {"description": "A run or another bulk answer is in progress"}},
)
def start_bulk_job(
    request: StartBulkJobRequest,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
    job_runner: BulkJobRunner = Depends(get_bulk_job_runner),
):
    """Fixes the items (as bulk-resolve picks them) and returns at once; the
    job answers them one by one in the background, stopping at the first
    error. Every other answer is refused (409 `bulk_in_progress`) until it
    ends. Follow it through `GET /status` or `GET /bulk-jobs/{id}`."""
    try:
        started = bulk_jobs.start_job(
            db,
            item_ids=request.item_ids,
            run_id=request.run_id,
            kind=request.kind,
            choice=request.choice,
            label=request.label,
            current=current,
        )
    except AirtableSyncError as e:
        db.rollback()
        raise _conflict(e)
    # Commit BEFORE launching: the job's thread reads with its own session.
    db.commit()
    job_runner.launch(started)
    db.expire_all()
    return {"data": bulk_jobs.serialize_job(bulk_jobs.get_job(db, started.job_id))}


@router.get(
    "/bulk-jobs/{job_id}",
    response_model=BulkJobAPIResponse,
    summary="One bulk job's progress",
    responses={**FORBIDDEN_RESPONSE, **JOB_NOT_FOUND_RESPONSE},
)
def get_bulk_job(job_id: int, db: Session = Depends(get_db_v2)):
    job = bulk_jobs.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    return {"data": bulk_jobs.serialize_job(job)}


@router.post(
    "/bulk-jobs/{job_id}/stop",
    response_model=BulkJobAPIResponse,
    summary="Stop a bulk job before its next item",
    responses={**FORBIDDEN_RESPONSE, **JOB_NOT_FOUND_RESPONSE, 409: {"description": "The job has already ended"}},
)
def stop_bulk_job(
    job_id: int,
    db: Session = Depends(get_db_v2),
    current: CurrentHubUser = Depends(get_current_hub_user),
):
    """Items already answered stay answered; the rest stay open."""
    try:
        job = bulk_jobs.request_stop(db, job_id, actor_email=current.email)
    except AirtableSyncError as e:
        db.rollback()
        raise _conflict(e)
    if job is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Bulk job not found")
    db.commit()
    return {"data": bulk_jobs.serialize_job(job)}
