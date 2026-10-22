"""Airtable sync wire shapes (AI Docs/plan_airtable_sync_v2_2026-10-21.md §9).

Follows app/schemas/rbac.py: StrictRequestModel (extra="forbid") for request
bodies, so an unknown field is a 422 rather than silently ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
