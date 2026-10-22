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
FREQUENCIES = ("daily", "weekly")
RUN_TRIGGERS = ("manual", "scheduled", "preview")
RUN_STATUSES = ("running", "succeeded", "failed", "interrupted")
ITEM_STATUSES = ("open", "resolved", "superseded")

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
    """One run: manual, scheduled or preview (v1 plan §5.1)."""

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

    __table_args__ = (
        CheckConstraint(_in("trigger", RUN_TRIGGERS), name="ck_airtable_sync_runs_trigger"),
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_airtable_sync_runs_status"),
        Index("ix_airtable_sync_runs_started_at", "started_at"),
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


# Every table this module defines, in FK-dependency order. The migration
# script creates them in this order.
SYNC_TABLES = (
    AirtableSyncSettingsV2.__table__,
    AirtableDesignationMappingV2.__table__,
    AirtableSyncRunV2.__table__,
    AirtableSyncRecordV2.__table__,
    AirtableSyncItemV2.__table__,
    AirtableSyncLogV2.__table__,
)
