"""The scheduler tick (v1 plan §8, C1, C2; plan v2 R1).

The cron calls `POST /v2/airtable-sync/tick` every few minutes
(routers/airtable_sync_tick.py); each call runs `tick` once:

  1. work out the most recent scheduled slot ≤ now, in the schedule's zone
     (daily: `run_time` today or yesterday; weekly: the latest `run_weekday`
     at `run_time`, today or up to 7 days ago), as an aware UTC instant;
  2. it is due only if the schedule is on, the slot is newer than
     `last_scheduled_slot`, and the slot is less than SLOT_MAX_AGE old (so a
     long outage never produces a surprise run at a random hour);
  3. due → start a real run of all four steps with `trigger='scheduled'`
     and no starter, writing `last_scheduled_slot` in the same transaction
     as the run row; commit; `launch` (start notification, background run);
  4. another run holds the lock → the slot is consumed anyway and
     "skipped: run in progress" is logged (v1 C1). The next slot runs
     normally.

A failure before that (e.g. the settings read) consumes nothing: the next
tick tries the same slot again.

DST (owner, 2026-10-24): a wall time that happens twice (fall-back) runs
once, at its FIRST occurrence; one that doesn't exist (spring-forward gap)
runs that same day, shifted forward by the gap (02:30 → 03:30 EDT). That is
what a `fold=0` aware datetime means (PEP 495), so `resolve_local` relies
on it and the tests pin it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import FREQUENCIES, RUN_STEPS, AirtableSyncSettingsV2
from app.services.airtable_sync.runner import SyncRunInProgress, SyncRunner, as_utc, take_sync_lock
from app.services.airtable_sync.settings_service import get_settings_row

logger = logging.getLogger(__name__)

SLOT_MAX_AGE = timedelta(hours=6)

# Days to look back for the latest slot: a weekly slot is at most 7 days
# old; one more covers a zone that skipped a calendar day.
_LOOKBACK_DAYS = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_local(day: date, at: time, zone: ZoneInfo) -> datetime:
    """`at` on `day` in `zone`, as a UTC instant (DST rule in the module
    docstring: fold=0 = first occurrence / shifted past a gap)."""
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=zone).astimezone(timezone.utc)


def latest_slot(
    now: datetime,
    *,
    frequency: str,
    run_time: time,
    run_weekday: int | None,
    zone: ZoneInfo,
) -> datetime:
    """The most recent scheduled instant ≤ `now` (aware, UTC)."""
    today = now.astimezone(zone).date()
    for back in range(_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=back)
        if frequency == "weekly" and day.weekday() != run_weekday:
            continue
        slot = resolve_local(day, run_time, zone)
        if slot <= now:
            return slot
    raise ValueError("No scheduled slot in the look-back window.")  # unreachable for a valid schedule


@dataclass(frozen=True)
class Decision:
    due: bool
    reason: str | None = None  # why not, when not due
    slot: datetime | None = None


def decide(row: AirtableSyncSettingsV2, now: datetime) -> Decision:
    """Whether the latest slot should run now. Pure: reads `row` only."""
    if not row.schedule_enabled:
        return Decision(False, "schedule_disabled")
    # 'interval' is in FREQUENCIES with its schema (RI-1), but its slots come
    # in RI-4; until then it must not fall through to the daily branch.
    if row.frequency == "interval":
        return Decision(False, "invalid_schedule")
    # The settings API refuses all of these; a row edited by hand is the
    # only way here, and it must not crash every tick.
    weekly = row.frequency == "weekly"
    if row.frequency not in FREQUENCIES or (weekly and row.run_weekday not in range(7)):
        return Decision(False, "invalid_schedule")
    try:
        zone = ZoneInfo(row.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return Decision(False, "invalid_schedule")

    slot = latest_slot(
        now,
        frequency=row.frequency,
        run_time=row.run_time,
        run_weekday=row.run_weekday if weekly else None,
        zone=zone,
    )
    last = as_utc(row.last_scheduled_slot)
    if last is not None and slot <= last:
        return Decision(False, "slot_already_used", slot)
    if now - slot >= SLOT_MAX_AGE:  # measured from the slot, not from `last`
        return Decision(False, "slot_too_old", slot)
    return Decision(True, None, slot)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _consume(db: Session, slot: datetime) -> None:
    """Mark `slot` used, never moving `last_scheduled_slot` backwards."""
    row = get_settings_row(db)
    last = as_utc(row.last_scheduled_slot)
    if last is None or slot > last:
        row.last_scheduled_slot = slot


def tick(db: Session, runner: SyncRunner, *, now: datetime | None = None) -> dict:
    """One scheduler tick. Returns what it did, for the cron's log:
    `{"result": "started", "run_id", "slot"}`,
    `{"result": "skipped", "reason": "run_in_progress", "slot", "blocking_run_id"}`, or
    `{"result": "not_due", "reason", "slot"}`. Commits its own transaction."""
    now = now or _utc_now()

    # Two overlapping ticks: the second waits here until the first has
    # committed, then sees the slot already used.
    take_sync_lock(db)
    decision = decide(get_settings_row(db), now)
    if not decision.due:
        db.commit()  # persists the settings row if this read created it
        return {"result": "not_due", "reason": decision.reason, "slot": _iso(decision.slot)}

    try:
        started = runner.start(db, steps=RUN_STEPS, preview=False, trigger="scheduled")
    except SyncRunInProgress as exc:
        # `start` may have rolled back (the insert race); either way begin
        # clean, then consume the slot (v1 C1).
        db.rollback()
        _consume(db, decision.slot)
        db.commit()
        blocking = exc.details.get("run_id")
        logger.warning(
            "Airtable sync: scheduled slot %s skipped: run in progress (run %s)", _iso(decision.slot), blocking
        )
        return {
            "result": "skipped",
            "reason": "run_in_progress",
            "slot": _iso(decision.slot),
            "blocking_run_id": blocking,
        }

    _consume(db, decision.slot)  # same transaction as the run row
    db.commit()
    runner.launch(started)  # start notification, then the run in the background
    logger.info("Airtable sync: scheduled slot %s started run %s", _iso(decision.slot), started.run_id)
    return {"result": "started", "run_id": started.run_id, "slot": _iso(decision.slot)}
