"""Airtable sync wire shapes (AI Docs/plan_airtable_sync_v2_2026-10-21.md §9).

Follows app/schemas/rbac.py: StrictRequestModel (extra="forbid") for request
bodies, so an unknown field is a 422 rather than silently ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, model_validator

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
    # A scheduled attempt's slot, its number among the runs with that slot
    # (1-based, by id), the runs so far for it, and the most there will be.
    # All null on manual runs, previews and runs from before the column.
    scheduled_slot: datetime | None = None
    attempt: int | None = None
    attempts: int | None = None
    max_attempts: int | None = None


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


# ---------------------------------------------------------
# Items (plan v2 §6; v1 plan §5.4, §7)
# ---------------------------------------------------------

ItemStatusFilter = Literal["open", "resolved", "superseded", "all"]


class SyncItemResponse(BaseModel):
    id: int
    entity: str
    airtable_record_id: str
    hub_id: int | None = None
    subject: str | None = None
    kind: str
    # Per-field / per-edge kinds only (e.g. the cluster of a removed edge).
    field: str | None = None
    # Base / Airtable / Hub values, candidates, usage… per kind.
    payload: dict[str, Any] | None = None
    # What resolve accepts for this item now; empty for "—" items and for
    # items that are not open.
    choices: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved", "superseded"]
    resolution: str | None = None
    resolution_data: dict[str, Any] | None = None
    first_run_id: int | None = None
    last_run_id: int | None = None
    resolved_by_email: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class SyncItemPage(BaseModel):
    items: list[SyncItemResponse] = Field(default_factory=list)
    next_cursor: int | None = None


class SyncItemsAPIResponse(BaseModel):
    data: SyncItemPage


class ResolveItemRequest(StrictRequestModel):
    """`choice` is one of the item's `choices`. `data` carries the pick for
    "Link to X": `{"user_id": n}` (ambiguous_match) or `{"scope_id": n}`
    (scope_ambiguous_match)."""

    choice: StrictStr = Field(min_length=1, max_length=48)
    data: dict[str, Any] | None = None


class ResolveItemData(BaseModel):
    item: SyncItemResponse
    # What the resolution did: {"applied": bool, ...}; a person-level
    # answer that must wait says `waiting_on` (the open or skipped kinds).
    outcome: dict[str, Any]
    # The same record's items still open afterwards.
    open_items: list[SyncItemResponse] = Field(default_factory=list)


class ResolveItemAPIResponse(BaseModel):
    data: ResolveItemData


class BulkResolveRequest(StrictRequestModel):
    """v1 plan §5.4 "Accept all from Airtable" / "Keep all": the same choice
    for several items, in id order, stopping at the first error.
      item_ids  these items (e.g. one person's conflicts); each must offer
                the choice
      run_id    every item still open from that run (its `last_run_id`)
                that offers the choice, optionally of one `kind` (e.g. the
                first run's `field_conflict`s)
    Exactly one of `item_ids` and `run_id`."""

    item_ids: list[StrictInt] | None = Field(default=None, min_length=1, max_length=1000)
    run_id: StrictInt | None = None
    kind: StrictStr | None = Field(default=None, min_length=1, max_length=48)
    choice: StrictStr = Field(min_length=1, max_length=48)

    @model_validator(mode="after")
    def _one_target(self) -> BulkResolveRequest:
        if (self.item_ids is None) == (self.run_id is None):
            raise ValueError("Give exactly one of item_ids and run_id.")
        if self.kind is not None and self.item_ids is not None:
            raise ValueError("kind only narrows a run_id request.")
        return self


class BulkResolvedItem(BaseModel):
    item_id: int
    outcome: dict[str, Any]


class BulkResolveData(BaseModel):
    resolved: list[BulkResolvedItem] = Field(default_factory=list)
    # The item that stopped the loop, with the 409's {code, message, ...};
    # null when every item was resolved.
    failed: dict[str, Any] | None = None
    # Items after the failed one, left as they were.
    not_attempted: list[int] = Field(default_factory=list)


class BulkResolveAPIResponse(BaseModel):
    data: BulkResolveData


class StartBulkJobRequest(BulkResolveRequest):
    """`BulkResolveRequest` run in the background (services/airtable_sync/
    bulk_jobs.py), plus the button's wording for the tracker."""

    label: StrictStr | None = Field(default=None, max_length=255)


class BulkJobResponse(BaseModel):
    id: int
    choice: str
    label: str | None = None
    # {"run_id", "kind"} or {"item_ids": true}
    target: dict[str, Any] | None = None
    status: Literal["running", "succeeded", "failed", "stopped", "interrupted"]
    total: int
    done: int
    last_item_id: int | None = None
    last_subject: str | None = None
    # The item that stopped the loop, with the 409's {code, message, ...}.
    failed: dict[str, Any] | None = None
    not_attempted: int = 0
    stop_requested: bool = False
    stop_requested_by_email: str | None = None
    # stopped: "stop_requested" (an admin's Stop) or "run_started".
    stop_reason: str | None = None
    error: str | None = None
    started_by_email: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    # True for a `running` job with no heartbeat for 5 minutes.
    stale: bool = False


class BulkJobAPIResponse(BaseModel):
    data: BulkJobResponse


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
    # The background bulk answer going now (any admin's), and the last ended one.
    current_bulk_job: BulkJobResponse | None = None
    last_bulk_job: BulkJobResponse | None = None


class SyncStatusAPIResponse(BaseModel):
    data: SyncStatusData


# ---------------------------------------------------------
# Scheduler tick (token-only; routers/airtable_sync_tick.py)
# ---------------------------------------------------------


class SyncTickData(BaseModel):
    """What one tick did (services/airtable_sync/scheduler.py)."""

    result: Literal["started", "skipped", "not_due"]
    # skipped: "run_in_progress". not_due: "schedule_disabled",
    # "slot_already_used", "slot_too_old", "invalid_schedule" or
    # "bulk_job_in_progress".
    reason: str | None = None
    # The slot the tick looked at (null when the schedule is off or invalid).
    slot: datetime | None = None
    run_id: int | None = None  # started
    blocking_run_id: int | None = None  # skipped: the run holding the lock
    attempt: int | None = None  # started / skipped: a retry's attempt number
    # not_due slot_already_used: why the slot isn't retried ("no_attempt",
    # "attempt_running", "attempts_exhausted", "succeeded_since",
    # "slot_too_old", "retry_wait", "next_slot_soon"); null after a success.
    retry_reason: str | None = None
    bulk_job_id: int | None = None  # not_due bulk_job_in_progress: the live job


class SyncTickAPIResponse(BaseModel):
    data: SyncTickData
