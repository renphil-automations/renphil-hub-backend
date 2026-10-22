"""Read side of the sync's runs (v1 plan §7, plan v2 §9): run history, one
run, a run's log (keyset-paginated) and the tab's status payload. Read-only
apart from `status_payload`, which may create the settings row."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import AirtableSyncItemV2, AirtableSyncLogV2, AirtableSyncRunV2
from app.services.airtable_sync import airtable_reader, settings_service
from app.services.airtable_sync.runner import as_utc, is_stale, running_run


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def serialize_run(run: AirtableSyncRunV2, now: datetime | None = None) -> dict:
    return {
        "id": run.id,
        "steps": list(run.steps or []),
        "trigger": run.trigger,
        "preview": run.trigger == "preview",
        "status": run.status,
        "started_by_user_id": run.started_by_user_id,
        "started_by_email": run.started_by_email,
        "started_at": as_utc(run.started_at),
        "finished_at": as_utc(run.finished_at),
        "heartbeat_at": as_utc(run.heartbeat_at),
        # A `running` row with no heartbeat for 5 minutes: its thread is
        # gone; the next start will mark it interrupted.
        "stale": is_stale(run, now),
        "summary": run.summary,
        "error": run.error,
    }


def list_runs(db: Session, limit: int) -> list[dict]:
    now = _utc_now()
    rows = (
        db.query(AirtableSyncRunV2)
        .order_by(AirtableSyncRunV2.started_at.desc(), AirtableSyncRunV2.id.desc())
        .limit(limit)
        .all()
    )
    return [serialize_run(r, now) for r in rows]


def get_run(db: Session, run_id: int) -> AirtableSyncRunV2 | None:
    return db.get(AirtableSyncRunV2, run_id)


def _serialize_log(row: AirtableSyncLogV2) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "resolution_item_id": row.resolution_item_id,
        "entity": row.entity,
        "airtable_record_id": row.airtable_record_id,
        "hub_id": row.hub_id,
        "subject": row.subject,
        "action": row.action,
        "field": row.field,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "actor_email": row.actor_email,
        "created_at": as_utc(row.created_at),
    }


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_log(
    db: Session,
    run_id: int,
    *,
    entity: str | None,
    action: str | None,
    q: str | None,
    cursor: int | None,
    limit: int,
) -> dict:
    """A run's log in the order it was written (id ascending), `limit` at a
    time. `next_cursor` is the last id returned when more rows follow; pass
    it back as `cursor`. `q` matches, case-insensitively, the subject, the
    Airtable record id, the field and both values."""
    query = db.query(AirtableSyncLogV2).filter(AirtableSyncLogV2.run_id == run_id)
    if entity:
        query = query.filter(AirtableSyncLogV2.entity == entity)
    if action:
        query = query.filter(AirtableSyncLogV2.action == action)
    if q and q.strip():
        pattern = _like_pattern(q.strip())
        query = query.filter(
            or_(
                *(
                    column.ilike(pattern, escape="\\")
                    for column in (
                        AirtableSyncLogV2.subject,
                        AirtableSyncLogV2.airtable_record_id,
                        AirtableSyncLogV2.field,
                        AirtableSyncLogV2.old_value,
                        AirtableSyncLogV2.new_value,
                    )
                )
            )
        )
    if cursor is not None:
        query = query.filter(AirtableSyncLogV2.id > cursor)
    rows = query.order_by(AirtableSyncLogV2.id.asc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "entries": [_serialize_log(r) for r in rows],
        "next_cursor": rows[-1].id if more and rows else None,
    }


def open_item_counts(db: Session) -> dict[str, int]:
    rows = (
        db.query(AirtableSyncItemV2.kind, func.count(AirtableSyncItemV2.id))
        .filter(AirtableSyncItemV2.status == "open")
        .group_by(AirtableSyncItemV2.kind)
        .all()
    )
    return {kind: count for kind, count in rows}


def _config_error(settings: Any) -> str | None:
    try:
        airtable_reader.build_config(settings)
    except airtable_reader.AirtableSyncConfigError as exc:
        return str(exc)
    return None


def status_payload(db: Session, settings: Any) -> dict:
    """What the tab polls: the schedule, the running run (if any), the last
    finished run, open items by kind, and whether the sync's settings are
    complete (checked locally; Airtable is not called)."""
    now = _utc_now()
    current = running_run(db)
    last = (
        db.query(AirtableSyncRunV2)
        .filter(AirtableSyncRunV2.status != "running")
        .order_by(AirtableSyncRunV2.started_at.desc(), AirtableSyncRunV2.id.desc())
        .first()
    )
    counts = open_item_counts(db)
    return {
        "settings": settings_service.serialize_settings(settings_service.get_settings_row(db)),
        "config_error": _config_error(settings),
        "current_run": serialize_run(current, now) if current else None,
        "last_run": serialize_run(last, now) if last else None,
        "open_items": counts,
        "open_items_total": sum(counts.values()),
    }
