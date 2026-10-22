"""Airtable sync wire shapes (AI Docs/plan_airtable_sync_v2_2026-10-21.md §9).

Follows app/schemas/rbac.py: StrictRequestModel (extra="forbid") for request
bodies, so an unknown field is a 422 rather than silently ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr

from app.schemas.tab import StrictRequestModel

# "HH:MM", 24-hour.
RUN_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# Generous ceiling: Airtable has 6 Designation choices today.
MAX_MAPPINGS = 200


# ---------------------------------------------------------
# Schedule
# ---------------------------------------------------------


class SyncSettingsResponse(BaseModel):
    schedule_enabled: bool
    frequency: Literal["daily", "weekly"]
    run_time: str
    # 0 = Monday … 6 = Sunday. Null when daily.
    run_weekday: int | None = None
    timezone: str
    last_scheduled_slot: datetime | None = None
    updated_by_email: str | None = None
    updated_at: datetime | None = None


class SyncSettingsAPIResponse(BaseModel):
    data: SyncSettingsResponse


class UpdateSyncSettingsRequest(StrictRequestModel):
    """A full replace of the schedule. A scheduled run always runs every
    step (plan v2 R1), so there is no step choice here."""

    schedule_enabled: StrictBool
    frequency: Literal["daily", "weekly"]
    run_time: StrictStr = Field(pattern=RUN_TIME_PATTERN)
    run_weekday: StrictInt | None = Field(default=None, ge=0, le=6)
    timezone: StrictStr = Field(min_length=1, max_length=64)


# ---------------------------------------------------------
# Designation → role mapping
# ---------------------------------------------------------


class DesignationMappingResponse(BaseModel):
    id: int
    designation: str
    action: Literal["role", "dont_sync"]
    role_id: int | None = None
    role_name: str | None = None
    # 'unmapped' when the mapped role was deleted (role_id went NULL).
    status: Literal["ok", "unmapped"]
    updated_by_email: str | None = None
    updated_at: datetime | None = None


class DesignationMappingsData(BaseModel):
    mappings: list[DesignationMappingResponse] = Field(default_factory=list)
    # The Designation choices read live from the Program Roles table schema,
    # in Airtable's order. Empty when Airtable could not be read; then
    # `choices_error` says why, and the mapping is still shown and editable.
    designation_choices: list[str] = Field(default_factory=list)
    # Choices with no mapping row yet (compared trimmed, case-insensitively).
    # The sync raises `unmapped_designation` for rows that use them.
    unmapped_choices: list[str] = Field(default_factory=list)
    choices_error: str | None = None


class DesignationMappingsAPIResponse(BaseModel):
    data: DesignationMappingsData


class DesignationMappingEntry(StrictRequestModel):
    designation: StrictStr = Field(min_length=1, max_length=255)
    action: Literal["role", "dont_sync"]
    # Required for "role", forbidden for "dont_sync" (checked by the service,
    # which answers 409 with a code the UI can show next to the row).
    role_id: StrictInt | None = None


class ReplaceDesignationMappingsRequest(StrictRequestModel):
    """The whole mapping. Designations left out are deleted."""

    mappings: list[DesignationMappingEntry] = Field(default_factory=list, max_length=MAX_MAPPINGS)


# ---------------------------------------------------------
# Runs (plan v2 §3.6, §9; v1 plan §5.1, §7)
# ---------------------------------------------------------

# Keep in step with models.airtable_sync.RUN_STEPS (a Literal needs the
# values spelled out).
SyncStep = Literal["clusters", "programs", "users", "program_roles"]
LogEntity = Literal["cluster", "program", "user", "program_role"]


class StartRunRequest(StrictRequestModel):
    """The ticked steps, in any order (they always run clusters → programs
    → users → program_roles); at least one. `preview` computes everything
    but writes only the run row and its log."""

    steps: list[SyncStep] = Field(min_length=1, max_length=16)
    preview: StrictBool = False


class StartRunData(BaseModel):
    run_id: int


class StartRunAPIResponse(BaseModel):
    data: StartRunData


class SyncRunResponse(BaseModel):
    id: int
    steps: list[str]
    trigger: Literal["manual", "scheduled", "preview"]
    preview: bool
    status: Literal["running", "succeeded", "failed", "interrupted"]
    started_by_user_id: int | None = None
    started_by_email: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    # True for a `running` run with no heartbeat for 5 minutes.
    stale: bool = False
    # {"preview", "read": {table counts, optional_fields_missing},
    #  "steps": {step: counts}, "failed_step"?}
    summary: dict[str, Any] | None = None
    error: str | None = None


class SyncRunAPIResponse(BaseModel):
    data: SyncRunResponse


class SyncRunListAPIResponse(BaseModel):
    data: list[SyncRunResponse]


class SyncLogEntryResponse(BaseModel):
    id: int
    run_id: int | None = None
    resolution_item_id: int | None = None
    entity: str
    airtable_record_id: str | None = None
    hub_id: int | None = None
    subject: str | None = None
    action: str
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    actor_email: str | None = None
    created_at: datetime


class SyncLogPage(BaseModel):
    entries: list[SyncLogEntryResponse] = Field(default_factory=list)
    # Pass back as `cursor` for the next page; null on the last page.
    next_cursor: int | None = None


class SyncLogAPIResponse(BaseModel):
    data: SyncLogPage


class SyncStatusData(BaseModel):
    settings: SyncSettingsResponse
    # Names every missing sync setting; null when the configuration is
    # complete. Checked locally (Airtable is not called).
    config_error: str | None = None
    current_run: SyncRunResponse | None = None
    last_run: SyncRunResponse | None = None
    # Open conflicts/warnings by kind; the tab's badge is the total.
    open_items: dict[str, int] = Field(default_factory=dict)
    open_items_total: int = 0


class SyncStatusAPIResponse(BaseModel):
    data: SyncStatusData
