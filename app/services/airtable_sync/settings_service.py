"""The sync's admin-edited configuration
(AI Docs/plan_airtable_sync_v2_2026-10-21.md §3.4 A2, §9; v1 plan §3.2, §7):

  - the schedule (the single `airtable_sync_settings` row)
  - the Program Roles "Designation" → Hub role mapping

Flushes, never commits; the router owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import (
    DESIGNATION_ACTIONS,
    FREQUENCIES,
    AirtableDesignationMappingV2,
    AirtableSyncSettingsV2,
)
from app.db_v2.models.role import RoleV2
from app.services.rbac_assignment_service import HUB_ADMIN_ROLE_KEY

SETTINGS_ROW_ID = 1

# What `update_settings` accepts. 'interval' is in FREQUENCIES with its schema
# (RI-1) but is refused until RI-4 builds its slots and validation.
_SETTABLE_FREQUENCIES = tuple(f for f in FREQUENCIES if f != "interval")


class AirtableSyncError(Exception):
    """A request that collides with a sync rule. The router turns it into a
    409 `{"code", "message", ...details}`, the same shape as RbacGraphError."""

    def __init__(self, code: str, message: str, **details) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_designation(value: str) -> str:
    """How two Designations are compared: trimmed, case-insensitive."""
    return (value or "").strip().lower()


# ---------------------------------------------------------
# Schedule
# ---------------------------------------------------------


def get_settings_row(db: Session) -> AirtableSyncSettingsV2:
    """The single settings row, created with its defaults if the migration
    has not inserted it (fresh test databases)."""
    row = db.get(AirtableSyncSettingsV2, SETTINGS_ROW_ID)
    if row is None:
        row = AirtableSyncSettingsV2(id=SETTINGS_ROW_ID)
        db.add(row)
        db.flush()
    return row


def serialize_settings(row: AirtableSyncSettingsV2) -> dict:
    return {
        "schedule_enabled": bool(row.schedule_enabled),
        "frequency": row.frequency,
        "run_time": row.run_time.strftime("%H:%M"),
        "run_weekday": row.run_weekday,
        "timezone": row.timezone,
        "last_scheduled_slot": row.last_scheduled_slot,
        "updated_by_email": row.updated_by_email,
        "updated_at": row.updated_at,
    }


def update_settings(
    db: Session,
    *,
    schedule_enabled: bool,
    frequency: str,
    run_time: time,
    run_weekday: int | None,
    timezone_name: str,
    actor_email: str,
) -> dict:
    """Replace the schedule. A weekly schedule needs a weekday; a daily one
    must not carry one (it would be silently ignored, and then come back
    the day someone switches to weekly)."""
    if frequency not in _SETTABLE_FREQUENCIES:
        raise AirtableSyncError(
            "invalid_frequency", f"Frequency must be one of {', '.join(_SETTABLE_FREQUENCIES)}."
        )
    if frequency == "weekly" and run_weekday is None:
        raise AirtableSyncError("weekday_required", "A weekly schedule needs a weekday.")
    if frequency == "daily" and run_weekday is not None:
        raise AirtableSyncError("weekday_not_allowed", "A daily schedule has no weekday.")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AirtableSyncError(
            "invalid_timezone", f"{timezone_name!r} is not a known time zone.", timezone=timezone_name
        ) from exc

    row = get_settings_row(db)
    row.schedule_enabled = schedule_enabled
    row.frequency = frequency
    row.run_time = run_time.replace(second=0, microsecond=0)
    row.run_weekday = run_weekday
    row.timezone = timezone_name
    row.updated_by_email = actor_email
    row.updated_at = _utc_now()
    db.flush()
    return serialize_settings(row)


# ---------------------------------------------------------
# Designation → role mapping
# ---------------------------------------------------------


@dataclass(frozen=True)
class MappingEntry:
    designation: str
    action: str
    role_id: int | None


def list_mappings(db: Session) -> list[dict]:
    """Every mapping row, ordered by Designation. `status` is 'unmapped'
    when the row maps to a role that has since been deleted (`role_id` went
    NULL): the sync treats that Designation as unmapped and warns."""
    roles = {r.id: r.name for r in db.query(RoleV2.id, RoleV2.name).all()}
    rows = db.query(AirtableDesignationMappingV2).all()
    rows.sort(key=lambda r: normalize_designation(r.designation))
    return [
        {
            "id": r.id,
            "designation": r.designation,
            "action": r.action,
            "role_id": r.role_id,
            "role_name": roles.get(r.role_id) if r.role_id is not None else None,
            "status": "unmapped" if r.action == "role" and r.role_id is None else "ok",
            "updated_by_email": r.updated_by_email,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


def _validate_entries(db: Session, entries: Sequence[MappingEntry]) -> None:
    seen: dict[str, str] = {}
    for entry in entries:
        key = normalize_designation(entry.designation)
        if not key:
            raise AirtableSyncError("empty_designation", "A Designation cannot be empty.")
        if key in seen:
            raise AirtableSyncError(
                "duplicate_designation",
                f"{entry.designation.strip()!r} is listed twice.",
                designation=entry.designation.strip(),
            )
        seen[key] = entry.designation
        if entry.action not in DESIGNATION_ACTIONS:
            raise AirtableSyncError("invalid_action", f"Action must be one of {', '.join(DESIGNATION_ACTIONS)}.")
        if entry.action == "dont_sync":
            if entry.role_id is not None:
                raise AirtableSyncError(
                    "role_not_allowed",
                    f"{entry.designation.strip()!r} is set to \"Don't sync\", so it cannot also name a role.",
                    designation=entry.designation.strip(),
                )
            continue
        if entry.role_id is None:
            raise AirtableSyncError(
                "role_required",
                f"Pick a role for {entry.designation.strip()!r}, or choose \"Don't sync\".",
                designation=entry.designation.strip(),
            )
        role = db.get(RoleV2, entry.role_id)
        if role is None:
            raise AirtableSyncError("role_not_found", "That role does not exist.", role_id=entry.role_id)
        # Admin is never handed out by the sync: an Airtable edit must not be
        # able to make someone a Hub Admin (v1 plan D1).
        if role.key == HUB_ADMIN_ROLE_KEY:
            raise AirtableSyncError(
                "admin_role_not_allowed",
                "The Admin role cannot be given by the Airtable sync.",
                role_id=role.id,
            )
        # The public role ("User") may only sit on the Hub scope, and a
        # Program Roles row always names a program (plan v2 A2).
        if role.is_public:
            raise AirtableSyncError(
                "public_role_not_allowed",
                f"{role.name!r} can only be held on the Hub scope, and Program Roles always name a program.",
                role_id=role.id,
            )


def replace_mappings(db: Session, entries: Sequence[MappingEntry], *, actor_email: str) -> list[dict]:
    """Full replace, validated as a whole before anything is written.
    Rows whose Designation survives are UPDATED in place (keeping their id and
    only bumping `updated_*` when something changed), new ones are inserted,
    and Designations left out are deleted."""
    _validate_entries(db, entries)

    existing = {normalize_designation(r.designation): r for r in db.query(AirtableDesignationMappingV2).all()}
    now = _utc_now()
    wanted: set[str] = set()
    for entry in entries:
        key = normalize_designation(entry.designation)
        wanted.add(key)
        designation = entry.designation.strip()
        row = existing.get(key)
        if row is None:
            db.add(
                AirtableDesignationMappingV2(
                    designation=designation,
                    action=entry.action,
                    role_id=entry.role_id,
                    updated_by_email=actor_email,
                    updated_at=now,
                )
            )
            continue
        if (row.designation, row.action, row.role_id) != (designation, entry.action, entry.role_id):
            row.designation = designation
            row.action = entry.action
            row.role_id = entry.role_id
            row.updated_by_email = actor_email
            row.updated_at = now
    for key, row in existing.items():
        if key not in wanted:
            db.delete(row)
    db.flush()
    return list_mappings(db)
