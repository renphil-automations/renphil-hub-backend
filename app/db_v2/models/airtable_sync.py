"""The Airtable → Hub sync's own tables
(AI Docs/plan_airtable_sync_v2_2026-10-21.md §4, on top of
plan_airtable_sync_2026-10-18.md §3.2).

The sync reads four Airtable tables (Cluster, Public incl. Finance Dashboard,
Users, Program Roles) and writes `scopes`, `parent_child_scopes`, `users` and
`role_assignments`. Everything it needs to REMEMBER between runs lives here:

  airtable_sync_settings          one row: the schedule
  airtable_designation_mappings   Program Roles "Designation" -> Hub role
  airtable_sync_records           one row per Airtable record ever seen: the
                                  three-way merge's BASE and latest snapshot
  airtable_sync_runs              one row per run (real or preview)
  airtable_sync_log               one row per change made (or, in a preview,
                                  that would be made)
  airtable_sync_items             open and closed conflicts / warnings
  airtable_sync_bulk_jobs         one row per background "Accept all" /
                                  "Keep all" (its progress, for the tab)

The sync is one-way (Airtable → Hub) and never writes to Airtable.

No relationship() here, matching every other model in db_v2/.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2

# The FK targets must be in BaseV2.metadata before `create_all` — see the
# long note in role_assignment.py; the same applies to every FK below.
from app.db_v2.models.role import RoleV2  # noqa: F401
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: F401
from app.db_v2.models.user import UserV2  # noqa: F401


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Value sets. Kept here, beside the CHECKs that enforce them, so the services
# and the migration script import one definition.
ENTITIES = ("cluster", "program", "user", "program_role")
DESIGNATION_ACTIONS = ("role", "dont_sync")
FREQUENCIES = ("daily", "weekly", "interval")
# "Every N hours" (frequency 'interval'), in minutes from local midnight: what
# the settings API accepts (owner, 2026-10-29; plan_airtable_sync_retries_interval
# §5.1). The DB CHECK is looser on purpose (≥ 15 and a divisor of 24 h), so a
# shorter choice later needs no migration.
INTERVAL_MINUTES = (60, 120, 180, 240, 360, 480, 720)
RUN_TRIGGERS = ("manual", "scheduled", "preview")
RUN_STATUSES = ("running", "succeeded", "failed", "interrupted")
# The steps a run may tick, in the only order they ever run (plan v2 R1).
RUN_STEPS = ("clusters", "programs", "users", "program_roles")
ITEM_STATUSES = ("open", "resolved", "superseded")
# succeeded: every item answered; failed: stopped at an item's error;
# stopped: an admin pressed Stop, or a run started midway (`stop_reason`);
# interrupted: no heartbeat for 5 minutes (its thread died).
BULK_JOB_STATUSES = ("running", "succeeded", "failed", "stopped", "interrupted")

DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_RUN_TIME = time(6, 0)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AirtableSyncSettingsV2(BaseV2):
    """Exactly one row (`id = 1`, enforced by a CHECK). Created by the
    migration; `airtable_sync.settings_service.get_settings_row` also creates
    it on first read so a fresh test database needs no seeding."""

    __tablename__ = "airtable_sync_settings"

    id = Column(Integer, primary_key=True, autoincrement=False)
    schedule_enabled = Column(Boolean, nullable=False, default=False, server_default=false())
    frequency = Column(String(8), nullable=False, default="daily", server_default="daily")
    run_time = Column(Time, nullable=False, default=DEFAULT_RUN_TIME, server_default=text("'06:00:00'"))
    # 0 = Monday … 6 = Sunday (Python's `weekday()`). NULL when daily.
    run_weekday = Column(Integer, nullable=True)
    # Minutes between slots when frequency = 'interval'; NULL otherwise
    # (ck_airtable_sync_settings_interval).
    interval_minutes = Column(Integer, nullable=True)
    timezone = Column(String(64), nullable=False, default=DEFAULT_TIMEZONE, server_default=DEFAULT_TIMEZONE)
    # The scheduled slot already used, so a tick never runs one slot twice
    # (v1 plan §8). Written only by the scheduler.
    last_scheduled_slot = Column(DateTime(timezone=True), nullable=True)
    updated_by_email = Column(String(320), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_airtable_sync_settings_single_row"),
        CheckConstraint(_in("frequency", FREQUENCIES), name="ck_airtable_sync_settings_frequency"),
        CheckConstraint(
            "run_weekday IS NULL OR (run_weekday >= 0 AND run_weekday <= 6)",
            name="ck_airtable_sync_settings_weekday",
        ),
        CheckConstraint(
            "interval_minutes IS NULL OR (interval_minutes >= 15 AND 1440 % interval_minutes = 0)",
            name="ck_airtable_sync_settings_interval_minutes",
        ),
        CheckConstraint(
            "(frequency = 'interval' AND interval_minutes IS NOT NULL) "
            "OR (frequency <> 'interval' AND interval_minutes IS NULL)",
            name="ck_airtable_sync_settings_interval",
        ),
    )


class AirtableDesignationMappingV2(BaseV2):
    """Program Roles "Designation" → Hub role (plan v2 §3.4 A2): one role per
    Designation, edited by Hub Admins.

    `role_id` is SET NULL, not RESTRICT: deleting a mapped role must stay
    possible, and the row then reads as "unmapped", which the sync raises as
    an `unmapped_designation` warning. So `action = 'role'` with a NULL
    `role_id` is a legal, meaningful state and no CHECK forbids it.

    Never targets the Admin role or the public ("User") role — a service
    check, since both are identified by row flags/keys, not by this table.
    """

    __tablename__ = "airtable_designation_mappings"

    id = Column(Integer, primary_key=True)
    # Stored as Airtable spells it (trimmed); unique case-insensitively.
    designation = Column(String(255), nullable=False)
    action = Column(String(16), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True)
    updated_by_email = Column(String(320), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_airtable_designation_mappings_lower", func.lower(designation), unique=True),
        CheckConstraint(_in("action", DESIGNATION_ACTIONS), name="ck_airtable_designation_mappings_action"),
        CheckConstraint(
            "action = 'role' OR role_id IS NULL",
            name="ck_airtable_designation_mappings_dont_sync_has_no_role",
        ),
    )


class AirtableSyncRunV2(BaseV2):
    """One run: manual, scheduled or preview (v1 plan §5.1).

    The `running` row IS the sync's lock (v1 C1: one run at a time, of any
    kind). `uq_airtable_sync_runs_running` lets at most one row be `running`,
    so starting a run = inserting that row, and a second start fails on the
    index. Chosen by the owner (2026-10-22) over v1's session advisory lock,
    which a transaction-mode pooler (Neon `-pooler`) can't hold reliably and
    SQLite doesn't have. A row whose `heartbeat_at` is 5 minutes old is
    taken as dead (a restart killed its thread) and marked `interrupted` by
    the next start."""

    __tablename__ = "airtable_sync_runs"

    id = Column(Integer, primary_key=True)
    # The ticked steps, in run order: a subset of
    # ["clusters", "programs", "users", "program_roles"] (plan v2 §3.6).
    steps = Column(JSONB, nullable=False, default=list)
    trigger = Column(String(16), nullable=False)
    started_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_by_email = Column(String(320), nullable=True)
    status = Column(String(16), nullable=False, default="running")
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    summary = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)
    # The slot a scheduled attempt is for (plan_airtable_sync_retries_interval
    # §3.1): the attempts at a slot = the runs carrying it. NULL on manual
    # runs and previews, and on scheduled runs from before the column (no
    # backfill), which are therefore never retried.
    scheduled_slot = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("trigger", RUN_TRIGGERS), name="ck_airtable_sync_runs_trigger"),
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_airtable_sync_runs_status"),
        CheckConstraint(
            "trigger = 'scheduled' OR scheduled_slot IS NULL",
            name="ck_airtable_sync_runs_scheduled_slot",
        ),
        Index(
            "ix_airtable_sync_runs_scheduled_slot",
            "scheduled_slot",
            # Both dialect predicates, as on uq_airtable_sync_runs_running.
            postgresql_where=text("scheduled_slot IS NOT NULL"),
            sqlite_where=text("scheduled_slot IS NOT NULL"),
        ),
        Index("ix_airtable_sync_runs_started_at", "started_at"),
        Index(
            "uq_airtable_sync_runs_running",
            "status",
            unique=True,
            # BOTH dialect predicates (see uq_airtable_sync_items_open): a
            # missing one would allow only ONE run row, ever, per status.
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )


class AirtableSyncRecordV2(BaseV2):
    """One row per Airtable record the sync has ever seen.

    `airtable_record_id` is the Airtable "Record ID"/"Record Id" COLUMN value,
    never the API id (it differs between synced copies of a table). For
    `program_role` it is the pair key "<employee record id>|<program record
    id>" (plan v2 §3.4 A1).

    `hub_id` is a plain integer, not an FK: it points at `scopes.id` or
    `users.id` depending on `entity`, and must survive the target going away.
    A C2 alias (a program row that IS a cluster) is a `program` record whose
    `hub_id` is the cluster's scope, with `base.is_cluster = true`.

    `managed_assignment_id` is how "an admin changed the sync's assignment"
    is detected: SET NULL when the row is deleted. Set on `program_role`
    records, and on a `user` record only for a sync-given default pair.
    """

    __tablename__ = "airtable_sync_records"

    id = Column(Integer, primary_key=True)
    entity = Column(String(16), nullable=False)
    airtable_record_id = Column(String(64), nullable=False)
    hub_id = Column(Integer, nullable=True)
    # The last AGREED Airtable values (the three-way merge's base).
    base = Column(JSONB, nullable=True)
    # The normalized Airtable values from the latest run; resolutions apply
    # from this.
    latest_airtable = Column(JSONB, nullable=True)
    managed_assignment_id = Column(
        Integer, ForeignKey("role_assignments.id", ondelete="SET NULL"), nullable=True
    )
    last_seen_run_id = Column(
        Integer, ForeignKey("airtable_sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)

    __table_args__ = (
        UniqueConstraint("entity", "airtable_record_id", name="uq_airtable_sync_records_entity_record"),
        CheckConstraint(_in("entity", ENTITIES), name="ck_airtable_sync_records_entity"),
        Index("ix_airtable_sync_records_entity_hub", "entity", "hub_id"),
    )


class AirtableSyncItemV2(BaseV2):
    """A conflict or warning (plan v2 §6). At most one OPEN item per
    (entity, record, kind, field). A resolved item is not reopened while a
    later run computes the same `fingerprint` (a remembered "Skip")."""

    __tablename__ = "airtable_sync_items"

    id = Column(Integer, primary_key=True)
    entity = Column(String(16), nullable=False)
    airtable_record_id = Column(String(64), nullable=False)
    hub_id = Column(Integer, nullable=True)
    subject = Column(String(512), nullable=True)
    kind = Column(String(48), nullable=False)
    # Only for per-field kinds (`field_conflict`); '' otherwise, never NULL,
    # so the partial unique index below needs no COALESCE.
    field = Column(String(128), nullable=False, default="", server_default="")
    payload = Column(JSONB, nullable=True)
    fingerprint = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="open", server_default="open")
    resolution = Column(String(48), nullable=True)
    resolution_data = Column(JSONB, nullable=True)
    first_run_id = Column(Integer, ForeignKey("airtable_sync_runs.id", ondelete="SET NULL"), nullable=True)
    last_run_id = Column(Integer, ForeignKey("airtable_sync_runs.id", ondelete="SET NULL"), nullable=True)
    resolved_by_email = Column(String(320), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        CheckConstraint(_in("entity", ENTITIES), name="ck_airtable_sync_items_entity"),
        CheckConstraint(_in("status", ITEM_STATUSES), name="ck_airtable_sync_items_status"),
        Index(
            "uq_airtable_sync_items_open",
            "entity",
            "airtable_record_id",
            "kind",
            "field",
            unique=True,
            # BOTH dialect predicates, for the reason spelled out on
            # uq_scopes_single_universal: a missing one emits a PLAIN unique
            # index, which would forbid ever re-raising a resolved item.
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
        Index("ix_airtable_sync_items_status_kind", "status", "kind"),
    )


class AirtableSyncLogV2(BaseV2):
    """One row per thing that changed (or, in a preview, would change).
    `run_id` is NULL for a change applied by a resolution; then
    `resolution_item_id` and `actor_email` are set."""

    __tablename__ = "airtable_sync_log"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("airtable_sync_runs.id", ondelete="CASCADE"), nullable=True, index=True)
    resolution_item_id = Column(
        Integer, ForeignKey("airtable_sync_items.id", ondelete="SET NULL"), nullable=True
    )
    entity = Column(String(16), nullable=False)
    airtable_record_id = Column(String(64), nullable=True)
    hub_id = Column(Integer, nullable=True)
    subject = Column(String(512), nullable=True)
    action = Column(String(48), nullable=False)
    field = Column(String(128), nullable=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    actor_email = Column(String(320), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        CheckConstraint(_in("entity", ENTITIES), name="ck_airtable_sync_log_entity"),
    )


class AirtableSyncBulkJobV2(BaseV2):
    """One background bulk resolve ("Accept all from Airtable" / "Keep all";
    owner, 2026-10-27): the same loop as `resolutions.bulk_resolve`, run in a
    thread so the tab can follow it after the admin navigates away, a reload,
    or from another admin's screen.

    Like the run row, the `running` row is a lock: at most one job runs
    (`uq_airtable_sync_bulk_jobs_running`), and while it does every other
    answer is refused (409 `bulk_in_progress`). `item_ids` is the exact list
    the job works through, fixed at its start. `heartbeat_at` moves after
    every item; a row 5 minutes without one is taken as dead and marked
    `interrupted` by the next job start (or ignored by a resolve)."""

    __tablename__ = "airtable_sync_bulk_jobs"

    id = Column(Integer, primary_key=True)
    choice = Column(String(48), nullable=False)
    # What the admin asked for: {"run_id", "kind"} or {"item_ids": true}.
    target = Column(JSONB, nullable=True)
    # The button's wording, for the tracker ("Accept all from Airtable (run 9, Field conflict)").
    label = Column(String(255), nullable=True)
    item_ids = Column(JSONB, nullable=False, default=list)
    total = Column(Integer, nullable=False, default=0, server_default="0")
    done = Column(Integer, nullable=False, default=0, server_default="0")
    last_item_id = Column(Integer, nullable=True)
    last_subject = Column(String(512), nullable=True)
    status = Column(String(16), nullable=False, default="running")
    # The item that stopped the loop, with its 409's {code, message, ...}.
    failed = Column(JSONB, nullable=True)
    not_attempted = Column(Integer, nullable=False, default=0, server_default="0")
    stop_requested_by_email = Column(String(320), nullable=True)
    # stopped: "stop_requested" or "run_started".
    stop_reason = Column(String(32), nullable=True)
    error = Column(Text, nullable=True)
    started_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_by_email = Column(String(320), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("status", BULK_JOB_STATUSES), name="ck_airtable_sync_bulk_jobs_status"),
        Index("ix_airtable_sync_bulk_jobs_started_at", "started_at"),
        Index(
            "uq_airtable_sync_bulk_jobs_running",
            "status",
            unique=True,
            # Both dialect predicates, as on uq_airtable_sync_runs_running.
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )


# Every table this module defines, in FK-dependency order. The migration
# script creates them in this order.
SYNC_TABLES = (
    AirtableSyncSettingsV2.__table__,
    AirtableDesignationMappingV2.__table__,
    AirtableSyncRunV2.__table__,
    AirtableSyncRecordV2.__table__,
    AirtableSyncItemV2.__table__,
    AirtableSyncLogV2.__table__,
    AirtableSyncBulkJobV2.__table__,
)
