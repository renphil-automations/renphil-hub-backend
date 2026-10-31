"""The scheduler tick (v1 plan §8, C1, C2; plan v2 R1).

The cron calls `POST /v2/airtable-sync/tick` every few minutes
(routers/airtable_sync_tick.py); each call runs `tick` once:

  1. work out the most recent scheduled slot ≤ now, in the schedule's zone
     (daily: `run_time` today or yesterday; weekly: the latest `run_weekday`
     at `run_time`, today or up to 7 days ago; interval: the latest
     `00:00 + k × interval_minutes`, today or yesterday), as an aware UTC
     instant;
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

Retries and the bulk hold (plan_airtable_sync_retries_interval §3, §6):

  - every tick first reaps a stale `running` row of any kind, in its own
    short transaction (`runner.reap_stale`), even with the schedule off; a
    reaped scheduled attempt (slot set) then gets its end note (§7);
  - a slot already used is retried (`decide_retry`) while its latest
    attempt failed or was interrupted: up to MAX_ATTEMPTS runs per slot, on
    the next tick (RETRY_WAIT 0, so the cron's cadence is the spacing),
    unless a real run has succeeded since, the slot is SLOT_MAX_AGE old, or
    the next slot is within RETRY_ROOM. A retry never touches
    `last_scheduled_slot`; one that finds the lock busy consumes and counts
    nothing (no run row), so the next tick tries again;
  - while a live bulk answer runs, a due first try or retry is held: nothing
    is consumed, the first tick after the job starts it.

DST (owner, 2026-10-24): a wall time that happens twice (fall-back) runs
once, at its FIRST occurrence; one that doesn't exist (spring-forward gap)
runs that same day, shifted forward by the gap (02:30 → 03:30 EDT). That is
what a `fold=0` aware datetime means (PEP 495), so `resolve_local` relies
on it and the tests pin it. Interval slots use the same rule (owner,
2026-10-29, D6): each grid time is resolved like a daily one, so two grid
times can land on one instant (a missing 02:00 shifted onto 03:00) and
merge, and at 2 h or more the jump-forward day has one run off the grid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import (
    FREQUENCIES,
    INTERVAL_MINUTES,
    RUN_STEPS,
    AirtableSyncRunV2,
    AirtableSyncSettingsV2,
)
from app.services.airtable_sync import bulk_jobs
from app.services.airtable_sync.runner import (
    INTERRUPTED_BY_TICK,
    SyncRunInProgress,
    SyncRunner,
    as_utc,
    attempts_for_slot,
    is_stale,
    liveness,
    real_success_after,
    reap_stale,
    slot_utc,
    take_sync_lock,
)
from app.services.airtable_sync.settings_service import get_settings_row

logger = logging.getLogger(__name__)

SLOT_MAX_AGE = timedelta(hours=6)
# Retries (owner, 2026-10-29: D1, D9): attempts per slot, the wait after a
# failed attempt (0 = the next tick), and the room a retry needs before the
# next slot.
MAX_ATTEMPTS = 3
RETRY_WAIT = timedelta(0)
RETRY_ROOM = timedelta(minutes=20)

# Days to look back for the latest slot: a weekly slot is at most 7 days
# old; one more covers a zone that skipped a calendar day.
_LOOKBACK_DAYS = 8
# An interval grid starts every local day, so today or yesterday always has
# a slot ≤ now (yesterday only for a zone whose jump swallows its midnight
# and part of the next hour: none in tzdata 2000–2040, checked 2026-10-31);
# likewise today or tomorrow has one > now.
_INTERVAL_DAYS = 2


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_local(day: date, at: time, zone: ZoneInfo) -> datetime:
    """`at` on `day` in `zone`, as a UTC instant (DST rule in the module
    docstring: fold=0 = first occurrence / shifted past a gap)."""
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=zone).astimezone(timezone.utc)


def interval_slots(day: date, interval_minutes: int, zone: ZoneInfo) -> list[datetime]:
    """`day`'s interval slots, oldest first (aware, UTC): the grid `00:00 +
    k × interval_minutes`, each time resolved by `resolve_local`. A set, so
    two grid times on one instant are one slot (plan §5.2)."""
    return sorted(
        {resolve_local(day, time(m // 60, m % 60), zone) for m in range(0, 1440, interval_minutes)}
    )


def latest_slot(
    now: datetime,
    *,
    frequency: str,
    run_time: time,
    run_weekday: int | None,
    zone: ZoneInfo,
    interval_minutes: int | None = None,
) -> datetime:
    """The most recent scheduled instant ≤ `now` (aware, UTC).
    `interval_minutes` is read for 'interval' only; `run_time` and
    `run_weekday` for daily / weekly only."""
    today = now.astimezone(zone).date()
    if frequency == "interval":
        for back in range(_INTERVAL_DAYS):
            for slot in reversed(interval_slots(today - timedelta(days=back), interval_minutes, zone)):
                if slot <= now:
                    return slot
        raise ValueError("No interval slot in the look-back window.")  # unreachable
    for back in range(_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=back)
        if frequency == "weekly" and day.weekday() != run_weekday:
            continue
        slot = resolve_local(day, run_time, zone)
        if slot <= now:
            return slot
    raise ValueError("No scheduled slot in the look-back window.")  # unreachable for a valid schedule


def next_slot(
    now: datetime,
    *,
    frequency: str,
    run_time: time,
    run_weekday: int | None,
    zone: ZoneInfo,
    interval_minutes: int | None = None,
) -> datetime:
    """The first scheduled instant strictly after `now` (aware, UTC). Reads
    the same fields as `latest_slot`."""
    today = now.astimezone(zone).date()
    if frequency == "interval":
        for ahead in range(_INTERVAL_DAYS):
            for slot in interval_slots(today + timedelta(days=ahead), interval_minutes, zone):
                if slot > now:
                    return slot
        raise ValueError("No interval slot in the look-ahead window.")  # unreachable
    for ahead in range(_LOOKBACK_DAYS + 1):
        day = today + timedelta(days=ahead)
        if frequency == "weekly" and day.weekday() != run_weekday:
            continue
        slot = resolve_local(day, run_time, zone)
        if slot > now:
            return slot
    raise ValueError("No scheduled slot in the look-ahead window.")  # unreachable for a valid schedule


@dataclass(frozen=True)
class Decision:
    due: bool
    reason: str | None = None  # why not, when not due
    slot: datetime | None = None


def _schedule_args(row: AirtableSyncSettingsV2) -> dict | None:
    """`row`'s schedule as `latest_slot` / `next_slot` arguments, or None
    when it is invalid. The settings API refuses all of these; a row edited
    by hand is the only way here, and it must not crash every tick.
    `interval_minutes` is read for an interval row only (a daily / weekly
    row's is NULL, and the pure tests' rows have none)."""
    weekly = row.frequency == "weekly"
    interval = row.frequency == "interval"
    if row.frequency not in FREQUENCIES or (weekly and row.run_weekday not in range(7)):
        return None
    if interval and row.interval_minutes not in INTERVAL_MINUTES:
        return None
    try:
        zone = ZoneInfo(row.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return {
        "frequency": row.frequency,
        "run_time": row.run_time,
        "run_weekday": row.run_weekday if weekly else None,
        "interval_minutes": row.interval_minutes if interval else None,
        "zone": zone,
    }


def _next_slot(row: AirtableSyncSettingsV2, now: datetime) -> datetime:
    """`next_slot` for a row `decide` has accepted: the one place a row's
    fields become its call, so the retry room and the settings payload can't
    read different schedules."""
    return next_slot(now, **_schedule_args(row))


def upcoming_slot(row: AirtableSyncSettingsV2, now: datetime) -> datetime | None:
    """The schedule's next slot after `now`, for the settings payload; None
    when the schedule is off or invalid."""
    if not row.schedule_enabled or _schedule_args(row) is None:
        return None
    return _next_slot(row, now)


def decide(row: AirtableSyncSettingsV2, now: datetime) -> Decision:
    """Whether the latest slot should run now. Pure: reads `row` only."""
    if not row.schedule_enabled:
        return Decision(False, "schedule_disabled")
    args = _schedule_args(row)
    if args is None:
        return Decision(False, "invalid_schedule")

    slot = latest_slot(now, **args)
    last = as_utc(row.last_scheduled_slot)
    if last is not None and slot <= last:
        return Decision(False, "slot_already_used", slot)
    if now - slot >= SLOT_MAX_AGE:  # measured from the slot, not from `last`
        return Decision(False, "slot_too_old", slot)
    return Decision(True, None, slot)


@dataclass(frozen=True)
class RetryDecision:
    due: bool
    reason: str | None = None  # the `retry_reason`, when not due
    attempt: int | None = None  # the attempt number, when due


def decide_retry(
    slot: datetime,
    attempts: list[AirtableSyncRunV2],
    *,
    succeeded_since: bool,
    next_slot: datetime,
    now: datetime,
) -> RetryDecision:
    """Whether a used slot gets another attempt now (plan §3.2). Pure:
    `attempts` are the slot's runs oldest first, `succeeded_since` whether a
    real run newer than the latest one succeeded, `next_slot` the schedule's
    next instant. The first reason that applies wins, in this order."""
    if not attempts:
        return RetryDecision(False, "no_attempt")  # the first try was a busy skip
    latest = attempts[-1]
    if latest.status == "running" and not is_stale(latest, now):
        return RetryDecision(False, "attempt_running")
    if latest.status == "succeeded":
        return RetryDecision(False, "last_attempt_succeeded")
    if len(attempts) >= MAX_ATTEMPTS:
        return RetryDecision(False, "attempts_exhausted")
    if succeeded_since:
        return RetryDecision(False, "succeeded_since")
    if now - slot >= SLOT_MAX_AGE:
        return RetryDecision(False, "slot_too_old")
    if now < liveness(latest) + RETRY_WAIT:
        return RetryDecision(False, "retry_wait")
    if next_slot - now <= RETRY_ROOM:
        return RetryDecision(False, "next_slot_soon")
    return RetryDecision(True, None, len(attempts) + 1)


def _retry(db: Session, row: AirtableSyncSettingsV2, slot: datetime, now: datetime) -> RetryDecision:
    """`decide_retry` with its facts read from `db`. Only called after
    `decide`, so the schedule is valid."""
    attempts = attempts_for_slot(db, slot)
    return decide_retry(
        slot,
        attempts,
        succeeded_since=bool(attempts) and real_success_after(db, attempts[-1].id),
        next_slot=_next_slot(row, now),
        now=now,
    )


def retry_outlook(db: Session, run: AirtableSyncRunV2, now: datetime) -> bool:
    """Whether a tick will retry `run`'s slot: the tick's own rules (`decide`,
    then `_retry`) minus the lock and bulk facts, which only delay a retry.
    `run` is the slot's latest attempt, already closed (the end note is
    built after `_finish`'s or the reap's commit). Judged once RETRY_WAIT
    has passed, so the wait never reads as "no retry" (plan §7)."""
    if run.scheduled_slot is None or run.status == "succeeded":
        return False
    at = max(now, liveness(run) + RETRY_WAIT)
    row = get_settings_row(db)
    decision = decide(row, at)
    slot = slot_utc(run.scheduled_slot)
    # The tick's retry gate: the latest slot is this run's and is the one
    # consumed (so `decide` said slot_already_used). Fails when the schedule
    # is off or invalid, or another slot replaced this one.
    if decision.slot != slot or slot != as_utc(row.last_scheduled_slot):
        return False
    return _retry(db, row, slot, at).due


def _notify_reaped(runner: SyncRunner, run_id: int) -> None:
    """The end note for a scheduled attempt the tick reaped (plan §3.4 step
    1, §7): its thread's `_finish` will find the row closed and send none.
    Advisory: an error is logged, never raised."""
    try:
        runner.notifier.finished(runner.session_factory, run_id)
    except Exception:
        logger.exception("Airtable sync run %s: could not notify its interruption", run_id)


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
    `{"result": "started", "run_id", "slot", "attempt"?}`,
    `{"result": "skipped", "reason": "run_in_progress", "slot", "blocking_run_id", "attempt"?}`,
    `{"result": "not_due", "reason", "slot", "retry_reason"?}`, or
    `{"result": "not_due", "reason": "bulk_job_in_progress", "slot", "bulk_job_id"}`.
    `attempt` only on a retry. Commits its own transactions."""
    now = now or _utc_now()

    # 1. Reap, in its own short transaction: a dead run of any kind closes
    # within a tick of going stale, even when nothing is due. A scheduled
    # attempt notifies like a failure, after the commit; nothing else does.
    take_sync_lock(db)
    reaped = reap_stale(db, now, INTERRUPTED_BY_TICK)
    reaped_attempt = reaped.id if reaped is not None and reaped.scheduled_slot is not None else None
    db.commit()
    if reaped_attempt is not None:
        _notify_reaped(runner, reaped_attempt)

    # 2. Decide. Two overlapping ticks: the second waits here until the
    # first has committed, then sees the slot already used.
    take_sync_lock(db)
    row = get_settings_row(db)
    decision = decide(row, now)
    slot = decision.slot

    # 3. The candidate: the first try, or a retry of the slot just used.
    attempt: int | None = None  # set on a retry
    retry: RetryDecision | None = None
    if not decision.due:
        if decision.reason == "slot_already_used" and slot == as_utc(row.last_scheduled_slot):
            retry = _retry(db, row, slot, now)
        if retry is None or not retry.due:
            db.commit()  # persists the settings row if this read created it
            result = {"result": "not_due", "reason": decision.reason, "slot": _iso(slot)}
            # Nothing to say after a success: the reply stays as phase 9's (owner, 2026-10-30).
            if retry is not None and retry.reason != "last_attempt_succeeded":
                result["retry_reason"] = retry.reason
            return result
        attempt = retry.attempt

    # 4. The bulk hold (§6): nothing consumed, the next tick looks again.
    job = bulk_jobs.running_job(db)
    if job is not None and not bulk_jobs.is_stale(job, now):
        db.commit()
        logger.info("Airtable sync: scheduled slot %s held: bulk answer %s in progress", _iso(slot), job.id)
        return {"result": "not_due", "reason": "bulk_job_in_progress", "slot": _iso(slot), "bulk_job_id": job.id}

    # 5. Start.
    try:
        started = runner.start(db, steps=RUN_STEPS, preview=False, trigger="scheduled", scheduled_slot=slot)
    except SyncRunInProgress as exc:
        # `start` may have rolled back (the insert race); either way begin
        # clean. A first try consumes the slot (v1 C1); a retry consumes and
        # counts nothing (no run row), so the next tick tries again.
        db.rollback()
        blocking = exc.details.get("run_id")
        result = {"result": "skipped", "reason": "run_in_progress", "slot": _iso(slot), "blocking_run_id": blocking}
        if attempt is None:
            _consume(db, slot)
            db.commit()
            logger.warning("Airtable sync: scheduled slot %s skipped: run in progress (run %s)", _iso(slot), blocking)
            return result
        db.commit()
        logger.warning(
            "Airtable sync: scheduled slot %s retry (attempt %s) skipped: run in progress (run %s)",
            _iso(slot),
            attempt,
            blocking,
        )
        return {**result, "attempt": attempt}

    if attempt is None:
        _consume(db, slot)  # same transaction as the run row
    db.commit()
    runner.launch(started)  # start notification, then the run in the background
    if attempt is None:
        logger.info("Airtable sync: scheduled slot %s started run %s", _iso(slot), started.run_id)
        return {"result": "started", "run_id": started.run_id, "slot": _iso(slot)}
    logger.info("Airtable sync: scheduled slot %s started run %s (attempt %s)", _iso(slot), started.run_id, attempt)
    return {"result": "started", "run_id": started.run_id, "slot": _iso(slot), "attempt": attempt}
