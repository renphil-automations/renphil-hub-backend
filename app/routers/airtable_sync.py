"""Airtable → Hub sync router (AI Docs/plan_airtable_sync_v2_2026-10-21.md §9;
v1 plan §7).

Hub Admins only, for every endpoint. Phase 1 exposes the configuration:

  GET  /v2/airtable-sync/settings   the schedule
  PUT  /v2/airtable-sync/settings
  GET  /v2/airtable-sync/mappings   Designation → role, plus the live
                                     Designation choices from Airtable
  PUT  /v2/airtable-sync/mappings   full replace

Runs, items and the scheduler tick come in later phases.
"""

from __future__ import annotations

import logging
from datetime import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user, get_current_user, require_hub_admin
from app.schemas.airtable_sync import (
    DesignationMappingsAPIResponse,
    ReplaceDesignationMappingsRequest,
    SyncSettingsAPIResponse,
    UpdateSyncSettingsRequest,
)
from app.services.airtable_sync import airtable_reader, settings_service
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
