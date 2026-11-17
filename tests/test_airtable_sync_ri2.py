"""Airtable sync RI-2 (AI Docs/plan_airtable_sync_retries_interval_2026-10-29.md
§3.2, §3.4, §3.5, §6, §9 RI-2): the retry engine and the bulk hold.

  - pure `decide_retry`: every reason of §3.2's table, in order (each case
    also satisfies every later one, so the order is pinned); RETRY_WAIT = 0;
    RETRY_ROOM at 19 m 59 s / 20 m / 20 m 1 s; `next_slot` daily / weekly
    incl. the New York DST days and Beirut's midnight changes
  - the tick: a failed attempt is retried on the next tick (same slot,
    `last_scheduled_slot` untouched), up to 3 attempts; a success, a real
    run's success, the schedule turned off, a 6 h old slot or a near next
    slot call it off; a busy retry consumes and counts nothing; the tick
    reaps stale rows first (previews too, schedule off too); manual runs and
    previews are never retried; a whole year of failing days → 3 runs a day
  - the bulk hold: a live job holds first tries and retries without
    consuming anything; a stale job doesn't hold
  - POST /tick and the runs API carry the new fields

In-memory SQLite on phase 9's TickFixture (phase 7's admins, a runner with
stub steps, `fail_step`, frozen clocks).
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

# First: phase 7's module (via phase 9's) sets the test environment.
from tests.test_airtable_sync_phase9 import NOW, SLOT, TOKEN, UTC, TickFixture, utc  # isort: skip

from app.db_v2.models.airtable_sync import RUN_STEPS, AirtableSyncBulkJobV2  # noqa: E402
from app.routers import airtable_sync_tick  # noqa: E402
from app.services.airtable_sync import runner as runner_module  # noqa: E402
from app.services.airtable_sync import scheduler  # noqa: E402
from app.services.airtable_sync.runner import INTERRUPTED_BY_TICK, INTERRUPTED_ERROR, as_utc  # noqa: E402
from tests.test_airtable_sync_phase7 import ADMIN_EMAIL  # noqa: E402

NY = ZoneInfo("America/New_York")
BEIRUT = ZoneInfo("Asia/Beirut")
TOKYO = ZoneInfo("Asia/Tokyo")


def attempt(id_: int, status: str = "failed", at: datetime | None = None) -> SimpleNamespace:
    """A run row for the pure function: only what `decide_retry` reads."""
    at = at or SLOT
    return SimpleNamespace(id=id_, status=status, started_at=at, heartbeat_at=at)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────


class ConstantTests(unittest.TestCase):
    def test_the_owners_numbers(self) -> None:
        self.assertEqual(scheduler.MAX_ATTEMPTS, 3)
        self.assertEqual(scheduler.RETRY_WAIT, timedelta(0))
        self.assertEqual(scheduler.RETRY_ROOM, timedelta(minutes=20))
        self.assertEqual(scheduler.SLOT_MAX_AGE, timedelta(hours=6))

    def test_the_tick_text_differs_from_the_starts(self) -> None:
        self.assertTrue(INTERRUPTED_BY_TICK.endswith("Marked interrupted by the scheduler tick."))
        self.assertNotEqual(INTERRUPTED_BY_TICK, INTERRUPTED_ERROR)


# ─────────────────────────────────────────────────────────────────────────────
# decide_retry (pure)
# ─────────────────────────────────────────────────────────────────────────────


class DecideRetryTests(unittest.TestCase):
    """The base case is due; each reason's case also meets every LATER
    condition, so dropping or reordering a check changes its answer."""

    # 20 minutes after the slot, attempt 1 closed 5 minutes ago.
    NOW = SLOT + timedelta(minutes=20)
    TOMORROW = SLOT + timedelta(days=1)

    def decide(self, attempts, *, succeeded_since=False, next_slot=None, now=None):
        return scheduler.decide_retry(
            SLOT,
            attempts,
            succeeded_since=succeeded_since,
            next_slot=next_slot or self.TOMORROW,
            now=now or self.NOW,
        )

    def test_due(self) -> None:
        result = self.decide([attempt(1, at=self.NOW - timedelta(minutes=5))])
        self.assertEqual(result, scheduler.RetryDecision(True, None, 2))

    def test_the_attempt_number_counts_every_run_of_the_slot(self) -> None:
        early = self.NOW - timedelta(minutes=10)
        result = self.decide([attempt(1, at=early), attempt(2, "interrupted", at=early)])
        self.assertEqual((result.due, result.attempt), (True, 3))

    # Every later condition at once: too old, a success since, the next
    # slot soon, the latest attempt just closed.
    def everything_later(self, now: datetime) -> dict:
        return {"succeeded_since": True, "next_slot": now + timedelta(minutes=1), "now": now}

    def test_1_no_attempt(self) -> None:
        old = SLOT + timedelta(hours=7)
        self.assertEqual(self.decide([], **self.everything_later(old)).reason, "no_attempt")

    def test_2_attempt_running(self) -> None:
        old = SLOT + timedelta(hours=7)
        runs = [attempt(1, at=old), attempt(2, at=old), attempt(3, "running", at=old)]
        self.assertEqual(self.decide(runs, **self.everything_later(old)).reason, "attempt_running")

    def test_2b_a_stale_running_attempt_is_not_running(self) -> None:
        # The reap closes such a row first; if one slips through it counts
        # as a failed attempt, and `start` interrupts it.
        stale = attempt(1, "running", at=self.NOW - timedelta(minutes=5))
        self.assertEqual(self.decide([stale]), scheduler.RetryDecision(True, None, 2))

    def test_3_last_attempt_succeeded(self) -> None:
        old = SLOT + timedelta(hours=7)
        later = old + timedelta(seconds=1)  # also inside the retry wait
        runs = [attempt(1, at=old), attempt(2, at=old), attempt(3, "succeeded", at=later)]
        self.assertEqual(self.decide(runs, **self.everything_later(old)).reason, "last_attempt_succeeded")

    def test_4_attempts_exhausted(self) -> None:
        old = SLOT + timedelta(hours=7)
        later = old + timedelta(seconds=1)
        runs = [attempt(1, at=old), attempt(2, "interrupted", at=old), attempt(3, at=later)]
        self.assertEqual(self.decide(runs, **self.everything_later(old)).reason, "attempts_exhausted")

    def test_5_succeeded_since(self) -> None:
        old = SLOT + timedelta(hours=7)
        runs = [attempt(1, at=old + timedelta(seconds=1))]
        self.assertEqual(self.decide(runs, **self.everything_later(old)).reason, "succeeded_since")

    def test_6_slot_too_old(self) -> None:
        old = SLOT + scheduler.SLOT_MAX_AGE  # exactly 6 h: too old
        runs = [attempt(1, at=old + timedelta(seconds=1))]
        result = self.decide(runs, next_slot=old + timedelta(minutes=1), now=old)
        self.assertEqual(result.reason, "slot_too_old")

    def test_6b_just_under_six_hours_is_not_too_old(self) -> None:
        now = SLOT + scheduler.SLOT_MAX_AGE - timedelta(seconds=1)
        self.assertTrue(self.decide([attempt(1, at=now - timedelta(minutes=5))], now=now).due)

    def test_7_retry_wait(self) -> None:
        runs = [attempt(1, at=self.NOW + timedelta(seconds=1))]  # liveness after now
        result = self.decide(runs, next_slot=self.NOW + timedelta(minutes=1))
        self.assertEqual(result.reason, "retry_wait")

    def test_7b_retry_wait_is_zero(self) -> None:
        # One second after the attempt closed, and the very instant: both retry.
        for gap in (timedelta(seconds=1), timedelta(0)):
            with self.subTest(gap=gap):
                self.assertTrue(self.decide([attempt(1, at=self.NOW - gap)]).due)

    def test_8_next_slot_soon(self) -> None:
        runs = [attempt(1, at=self.NOW - timedelta(minutes=5))]
        for room, reason in (
            (timedelta(minutes=19, seconds=59), "next_slot_soon"),
            (timedelta(minutes=20), "next_slot_soon"),
            (timedelta(minutes=20, seconds=1), None),
        ):
            with self.subTest(room=room):
                result = self.decide(runs, next_slot=self.NOW + room)
                self.assertEqual((result.due, result.reason), (reason is None, reason))

    def test_every_failure_status_is_retryable(self) -> None:
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                self.assertTrue(self.decide([attempt(1, status, at=self.NOW - timedelta(minutes=5))]).due)


# ─────────────────────────────────────────────────────────────────────────────
# next_slot (daily / weekly)
# ─────────────────────────────────────────────────────────────────────────────


class NextSlotTests(unittest.TestCase):
    def next(self, now: datetime, at: time, zone: ZoneInfo, weekday: int | None = None) -> datetime:
        return scheduler.next_slot(
            now, frequency="weekly" if weekday is not None else "daily", run_time=at, run_weekday=weekday, zone=zone
        )

    def test_daily(self) -> None:
        at = time(6, 0)  # 10:00 UTC in EDT
        self.assertEqual(self.next(utc(2026, 10, 24, 9, 59), at, NY), utc(2026, 10, 24, 10, 0))
        # Strictly after now: the slot itself is "latest", not "next".
        self.assertEqual(self.next(utc(2026, 10, 24, 10, 0), at, NY), utc(2026, 10, 25, 10, 0))
        self.assertEqual(self.next(utc(2026, 10, 24, 12, 0), at, NY), utc(2026, 10, 25, 10, 0))

    def test_next_is_the_slot_after_latest(self) -> None:
        now = utc(2026, 10, 24, 12, 0)
        latest = scheduler.latest_slot(now, frequency="daily", run_time=time(6, 0), run_weekday=None, zone=NY)
        self.assertEqual(self.next(now, time(6, 0), NY) - latest, timedelta(days=1))

    def test_new_york_spring_forward_shifts_by_the_gap(self) -> None:
        # 2026-03-08 02:30 doesn't exist → 03:30 EDT.
        self.assertEqual(self.next(utc(2026, 3, 7, 12, 0), time(2, 30), NY), utc(2026, 3, 8, 7, 30))

    def test_new_york_fall_back_runs_the_first_occurrence_only(self) -> None:
        at = time(1, 30)
        self.assertEqual(self.next(utc(2026, 10, 31, 12, 0), at, NY), utc(2026, 11, 1, 5, 30))  # 01:30 EDT
        # Between the two 01:30s: the second (06:30 UTC, EST) is not a slot.
        self.assertEqual(self.next(utc(2026, 11, 1, 6, 0), at, NY), utc(2026, 11, 2, 6, 30))

    def test_beirut_midnight_spring_forward(self) -> None:
        # 2026-03-29 00:00–00:59 doesn't exist: 00:30 → 01:30 EEST = 22:30 UTC the day before.
        self.assertEqual(self.next(utc(2026, 3, 28, 12, 0), time(0, 30), BEIRUT), utc(2026, 3, 28, 22, 30))
        self.assertEqual(self.next(utc(2026, 3, 28, 22, 30), time(0, 30), BEIRUT), utc(2026, 3, 29, 21, 30))

    def test_beirut_midnight_fall_back(self) -> None:
        # 2026-10-24 23:00–23:59 happens twice; only the first (EEST) is a slot.
        at = time(23, 30)
        self.assertEqual(self.next(utc(2026, 10, 24, 20, 0), at, BEIRUT), utc(2026, 10, 24, 20, 30))
        self.assertEqual(self.next(utc(2026, 10, 24, 20, 45), at, BEIRUT), utc(2026, 10, 25, 21, 30))

    def test_weekly(self) -> None:
        at = time(7, 15)  # Saturday 07:15 JST = Friday 22:15 UTC
        self.assertEqual(self.next(utc(2026, 10, 23, 22, 0), at, TOKYO, weekday=5), utc(2026, 10, 23, 22, 15))
        self.assertEqual(self.next(utc(2026, 10, 23, 22, 20), at, TOKYO, weekday=5), utc(2026, 10, 30, 22, 15))

    def test_weekly_across_new_york_fall_back(self) -> None:
        # Sunday 06:00: 10:00 UTC on 2026-10-25 (EDT), 11:00 UTC on 2026-11-01 (EST).
        self.assertEqual(self.next(utc(2026, 10, 25, 10, 1), time(6, 0), NY, weekday=6), utc(2026, 11, 1, 11, 0))


# ─────────────────────────────────────────────────────────────────────────────
# The tick
# ─────────────────────────────────────────────────────────────────────────────

T5 = NOW + timedelta(minutes=5)
T10 = NOW + timedelta(minutes=10)
T15 = NOW + timedelta(minutes=15)


class RetryTickFixture(TickFixture):
    def slots(self) -> list:
        return [as_utc(r.scheduled_slot) for r in self.runs()]

    def add_job(self, *, status: str = "running", heartbeat: datetime = NOW - timedelta(minutes=1)) -> AirtableSyncBulkJobV2:
        job = AirtableSyncBulkJobV2(
            choice="accept_airtable",
            label="Accept all from Airtable",
            item_ids=[],
            status=status,
            started_at=heartbeat,
            heartbeat_at=heartbeat,
            finished_at=None if status == "running" else heartbeat,
        )
        self.db.add(job)
        self.db.commit()
        return job

    def end_job(self, job: AirtableSyncBulkJobV2) -> None:
        self.db.expire_all()
        row = self.db.get(AirtableSyncBulkJobV2, job.id)
        row.status, row.finished_at = "succeeded", NOW
        self.db.commit()

    def close_run(self, run_id: int, status: str) -> None:
        row = self.run_row(run_id)
        row.status, row.finished_at = status, NOW
        self.db.commit()


class RetryTickTests(RetryTickFixture):
    def test_a_failed_first_try_is_retried_on_the_next_tick(self) -> None:
        self.fail_step = "users"
        first = self.tick()
        self.assertEqual(first, {"result": "started", "run_id": first["run_id"], "slot": SLOT.isoformat()})
        self.assertEqual(self.run_row(first["run_id"]).status, "failed")

        with mock.patch.object(scheduler, "_consume", wraps=scheduler._consume) as consume:
            second = self.tick(T5)
        consume.assert_not_called()  # a retry never touches last_scheduled_slot
        run = self.runs()[-1]
        self.assertEqual(
            second, {"result": "started", "run_id": run.id, "slot": SLOT.isoformat(), "attempt": 2}
        )
        self.assertEqual(run.trigger, "scheduled")
        self.assertEqual(list(run.steps), list(RUN_STEPS))
        self.assertIsNone(run.started_by_email)
        self.assertEqual(self.slots(), [SLOT, SLOT])
        self.assertEqual(self.last_slot(), SLOT)

    def test_three_attempts_then_exhausted(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.assertEqual(self.tick(T5)["attempt"], 2)
        self.assertEqual(self.tick(T10)["attempt"], 3)
        self.assertEqual(
            self.tick(T15),
            {
                "result": "not_due",
                "reason": "slot_already_used",
                "slot": SLOT.isoformat(),
                "retry_reason": "attempts_exhausted",
            },
        )
        self.assertEqual(self.slots(), [SLOT] * 3)
        self.assertEqual([r.status for r in self.runs()], ["failed"] * 3)
        self.assertEqual(self.last_slot(), SLOT)

    def test_success_on_attempt_two_ends_it(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.assertEqual(self.tick(T5)["attempt"], 2)
        self.assertEqual(self.runs()[-1].status, "succeeded")
        # Exactly phase 9's reply: no retry_reason after a success (owner, 2026-10-30).
        self.assertEqual(
            self.tick(T10), {"result": "not_due", "reason": "slot_already_used", "slot": SLOT.isoformat()}
        )
        self.assertEqual(len(self.runs()), 2)

    def test_an_attempt_still_running_is_waited_for(self) -> None:
        pending: list = []
        self.executor = pending.append
        self.tick()
        self.assertEqual(self.tick(NOW + timedelta(minutes=1))["retry_reason"], "attempt_running")
        self.assertEqual(len(self.runs()), 1)

    # ── the reap ────────────────────────────────────────────────────────

    def test_a_stale_scheduled_attempt_is_reaped_by_the_tick_and_retried(self) -> None:
        pending: list = []
        self.executor = pending.append  # attempt 1 never runs: its process "died"
        first = self.tick()
        result = self.tick(NOW + timedelta(minutes=6))
        dead = self.run_row(first["run_id"])
        self.assertEqual(dead.status, "interrupted")
        self.assertEqual(dead.error, INTERRUPTED_BY_TICK)
        self.assertEqual(as_utc(dead.finished_at), NOW + timedelta(minutes=6))
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(self.slots(), [SLOT, SLOT])

    def test_a_stale_preview_is_reaped_even_with_the_schedule_off(self) -> None:
        """Dev run 12's case: nothing is due, yet the dead row closes."""
        self.set_schedule(schedule_enabled=False)
        dead = self.add_run(status="running", trigger="preview", started=NOW - timedelta(minutes=30))
        self.assertEqual(self.tick(), {"result": "not_due", "reason": "schedule_disabled", "slot": None})
        row = self.run_row(dead.id)
        self.assertEqual((row.status, row.error), ("interrupted", INTERRUPTED_BY_TICK))
        self.assertEqual(as_utc(row.finished_at), NOW)

    def test_a_live_run_is_not_reaped(self) -> None:
        self.set_schedule(schedule_enabled=False)
        live = self.add_run(status="running", trigger="manual", started=NOW - timedelta(minutes=4))
        self.tick()
        self.assertEqual(self.run_row(live.id).status, "running")

    def test_the_reap_commits_even_if_the_rest_of_the_tick_fails(self) -> None:
        dead = self.add_run(status="running", trigger="manual", started=NOW - timedelta(minutes=30))
        with mock.patch.object(scheduler, "get_settings_row", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.assertEqual(self.run_row(dead.id).status, "interrupted")
        self.assertIsNone(self.last_slot())

    def test_start_still_uses_its_own_text(self) -> None:
        dead = self.add_run(status="running", trigger="manual", started=NOW - timedelta(minutes=30))
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.run_row(dead.id).error, INTERRUPTED_ERROR)

    # ── what calls the retries off ──────────────────────────────────────

    def test_a_manual_success_of_two_steps_calls_it_off(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.assertEqual(self.start(steps=["clusters", "programs"]).status_code, 202)
        self.assertEqual(self.runs()[-1].status, "succeeded")
        self.assertEqual(self.tick(T5)["retry_reason"], "succeeded_since")
        self.assertEqual(len(self.runs()), 2)

    def test_a_failed_manual_run_does_not_call_it_off(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.runs()[-1].status, "failed")
        self.assertEqual(self.tick(T5)["attempt"], 2)

    def test_a_preview_success_does_not_call_it_off(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.assertEqual(self.start(preview=True).status_code, 202)
        self.assertEqual(self.runs()[-1].status, "succeeded")
        self.assertEqual(self.tick(T5)["attempt"], 2)

    def test_turning_the_schedule_off_calls_it_off(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.set_schedule(schedule_enabled=False)
        result = self.tick(T5)
        self.assertEqual(result, {"result": "not_due", "reason": "schedule_disabled", "slot": None})
        self.assertEqual(len(self.runs()), 1)

    def test_a_slot_replaced_by_a_newer_schedule_is_not_retried(self) -> None:
        self.fail_step = "users"
        self.tick()
        # Moved earlier: today's 05:00 slot is older than the used one.
        self.set_schedule(run_time=time(5, 0))
        result = self.tick(T5)
        self.assertEqual(result["reason"], "slot_already_used")
        self.assertNotIn("retry_reason", result)
        self.assertEqual(len(self.runs()), 1)

    def test_slot_too_old(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.assertEqual(self.tick(SLOT + timedelta(hours=6))["retry_reason"], "slot_too_old")
        self.assertEqual(len(self.runs()), 1)

    def test_next_slot_too_close(self) -> None:
        self.fail_step = "users"
        self.tick()
        # 20 minutes before tomorrow's slot.
        late = SLOT + timedelta(days=1) - timedelta(minutes=20)
        with mock.patch.object(scheduler, "SLOT_MAX_AGE", timedelta(days=2)):
            self.assertEqual(self.tick(late)["retry_reason"], "next_slot_soon")
        self.assertEqual(len(self.runs()), 1)

    # ── the lock ────────────────────────────────────────────────────────

    def test_a_busy_retry_consumes_and_counts_nothing(self) -> None:
        self.fail_step = "users"
        self.tick()
        busy = self.add_run(status="running", trigger="manual", started=NOW + timedelta(minutes=4))
        with mock.patch.object(scheduler, "_consume", wraps=scheduler._consume) as consume:
            with self.assertLogs(scheduler.logger, "WARNING") as logs:
                result = self.tick(T5)
        consume.assert_not_called()
        self.assertEqual(
            result,
            {
                "result": "skipped",
                "reason": "run_in_progress",
                "slot": SLOT.isoformat(),
                "blocking_run_id": busy.id,
                "attempt": 2,
            },
        )
        self.assertIn("retry", logs.output[0])
        self.assertEqual(self.slots(), [SLOT, None])  # no run row for the retry
        self.assertEqual(self.last_slot(), SLOT)
        # The blocker ends without a success: the next tick retries, still as attempt 2.
        self.close_run(busy.id, "failed")
        self.assertEqual(self.tick(T10)["attempt"], 2)

    def test_a_busy_first_try_is_consumed_and_then_never_retried(self) -> None:
        busy = self.add_run(status="running", trigger="manual", started=NOW - timedelta(minutes=1))
        self.assertEqual(self.tick()["result"], "skipped")
        self.assertEqual(self.last_slot(), SLOT)
        self.close_run(busy.id, "failed")
        result = self.tick(T5)
        self.assertEqual(result["retry_reason"], "no_attempt")
        self.assertEqual([r.id for r in self.runs()], [busy.id])

    def test_manual_and_preview_failures_are_never_retried(self) -> None:
        self.set_schedule(last_scheduled_slot=SLOT)  # today's slot was used
        self.fail_step = "users"
        for preview in (False, True):
            self.assertEqual(self.start(preview=preview).status_code, 202)
        self.assertEqual(self.tick()["retry_reason"], "no_attempt")
        self.assertEqual(self.slots(), [None, None])


class YearSimulationTests(RetryTickFixture):
    def test_a_year_of_failing_days_gives_three_runs_a_day(self) -> None:
        """Daily 06:00 New York, every attempt fails, 5-minute ticks around
        each day's slot: exactly 3 runs per day, all for that day's slot,
        across both DST changes."""
        self.fail_step = "users"
        clock = {"now": NOW}
        patcher = mock.patch.object(runner_module, "_utc_now", side_effect=lambda: clock["now"])
        patcher.start()
        self.addCleanup(patcher.stop)

        start = date(2026, 1, 1)
        expected = []
        for n in range(365):
            day = start + timedelta(days=n)
            slot = scheduler.resolve_local(day, time(6, 0), NY)
            expected.append(slot)
            for k in range(-1, 6):  # 5 min before to 25 min after: 3 attempts, then 2 exhausted ticks
                clock["now"] = slot + timedelta(minutes=5 * k)
                self.tick(clock["now"])

        runs = self.runs()
        self.assertEqual(len(runs), 3 * 365)
        self.assertEqual(self.slots(), [s for s in expected for _ in range(3)])
        self.assertTrue(all(r.trigger == "scheduled" and r.status == "failed" for r in runs))
        self.assertEqual(self.last_slot(), expected[-1])


# ─────────────────────────────────────────────────────────────────────────────
# The bulk hold (§6)
# ─────────────────────────────────────────────────────────────────────────────


class BulkHoldTests(RetryTickFixture):
    def test_a_live_job_holds_a_first_try_without_consuming_it(self) -> None:
        job = self.add_job()
        with self.assertLogs(scheduler.logger, "INFO") as logs:
            result = self.tick()
        self.assertEqual(
            result,
            {"result": "not_due", "reason": "bulk_job_in_progress", "slot": SLOT.isoformat(), "bulk_job_id": job.id},
        )
        self.assertIn(f"bulk answer {job.id} in progress", logs.output[-1])
        self.assertIsNone(self.last_slot())
        self.assertEqual(self.runs(), [])
        # The job ends: the next tick starts the slot.
        self.end_job(job)
        self.assertEqual(self.tick(T5)["result"], "started")
        self.assertEqual(self.last_slot(), SLOT)

    def test_a_live_job_holds_a_retry(self) -> None:
        self.fail_step = "users"
        self.tick()
        job = self.add_job(heartbeat=T5 - timedelta(minutes=1))
        result = self.tick(T5)
        self.assertEqual((result["reason"], result["bulk_job_id"]), ("bulk_job_in_progress", job.id))
        self.assertEqual(len(self.runs()), 1)
        self.end_job(job)
        self.assertEqual(self.tick(T10)["attempt"], 2)

    def test_a_stale_job_does_not_hold(self) -> None:
        self.add_job(heartbeat=NOW - timedelta(minutes=5))
        self.assertEqual(self.tick()["result"], "started")

    def test_nothing_due_reads_as_today_while_a_job_runs(self) -> None:
        self.add_job()
        self.set_schedule(schedule_enabled=False)
        self.assertEqual(self.tick(), {"result": "not_due", "reason": "schedule_disabled", "slot": None})

    def test_a_job_outlasting_six_hours_makes_the_slot_too_old(self) -> None:
        late = SLOT + timedelta(hours=6)
        self.add_job(heartbeat=late - timedelta(minutes=1))
        result = self.tick(late)
        self.assertEqual(result["reason"], "slot_too_old")
        self.assertNotIn("bulk_job_id", result)
        self.assertEqual(self.runs(), [])


# ─────────────────────────────────────────────────────────────────────────────
# POST /tick and the runs API
# ─────────────────────────────────────────────────────────────────────────────


class EndpointTests(RetryTickFixture):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            airtable_sync_tick, "get_settings", side_effect=lambda: SimpleNamespace(AIRTABLE_SYNC_TOKEN=TOKEN)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = {"now": NOW}
        clock = mock.patch.object(scheduler, "_utc_now", side_effect=lambda: self.clock["now"])
        clock.start()
        self.addCleanup(clock.stop)

    def post(self):
        return self.client.post("/v2/airtable-sync/tick", headers={"X-Sync-Token": TOKEN})

    def test_a_retry_is_202_with_its_attempt(self) -> None:
        self.fail_step = "users"
        self.assertEqual(self.post().status_code, 202)
        self.clock["now"] = T5
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["data"],
            {
                "result": "started",
                "reason": None,
                "slot": "2026-10-24T10:00:00Z",
                "run_id": self.runs()[-1].id,
                "blocking_run_id": None,
                "attempt": 2,
                "retry_reason": None,
                "bulk_job_id": None,
            },
        )

    def test_held_is_200(self) -> None:
        job = self.add_job()
        response = self.post()
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual((data["result"], data["reason"], data["bulk_job_id"]), ("not_due", "bulk_job_in_progress", job.id))

    def test_retry_reason_reaches_the_reply(self) -> None:
        self.fail_step = "users"
        for minutes in (0, 5, 10, 15):
            self.clock["now"] = NOW + timedelta(minutes=minutes)
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["retry_reason"], "attempts_exhausted")

    # ── runs API ────────────────────────────────────────────────────────

    def test_runs_carry_their_slot_and_attempt(self) -> None:
        self.fail_step = "users"
        self.post()
        self.clock["now"] = T5
        self.post()
        self.assertEqual(self.start().status_code, 202)  # a manual run: no slot
        response = self.client.get("/v2/airtable-sync/runs", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(response.status_code, 200, response.text)
        rows = sorted(response.json()["data"], key=lambda r: r["id"])
        self.assertEqual(
            [(r["scheduled_slot"], r["attempt"], r["attempts"], r["max_attempts"]) for r in rows],
            [
                ("2026-10-24T10:00:00Z", 1, 2, 3),
                ("2026-10-24T10:00:00Z", 2, 2, 3),
                (None, None, None, None),
            ],
        )
        one = self.client.get(f"/v2/airtable-sync/runs/{rows[0]['id']}", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual((one.json()["data"]["attempt"], one.json()["data"]["attempts"]), (1, 2))

    def test_status_payload_carries_them_too(self) -> None:
        self.fail_step = "users"
        self.post()
        response = self.client.get("/v2/airtable-sync/status", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(response.status_code, 200, response.text)
        last = response.json()["data"]["last_run"]
        self.assertEqual((last["attempt"], last["attempts"], last["max_attempts"]), (1, 1, 3))


class RunnerQueryTests(RetryTickFixture):
    def test_attempts_for_slot_matches_aware_and_naive_and_other_zones(self) -> None:
        self.fail_step = "users"
        self.tick()
        db = self.Session()
        try:
            for value in (SLOT, SLOT.replace(tzinfo=None), SLOT.astimezone(NY)):
                with self.subTest(value=value):
                    self.assertEqual(len(runner_module.attempts_for_slot(db, value)), 1)
            self.assertEqual(runner_module.attempts_for_slot(db, SLOT + timedelta(days=1)), [])
        finally:
            db.close()

    def test_a_slot_in_another_zone_is_stored_as_utc(self) -> None:
        db = self.Session()
        try:
            started = self.make_runner().start(
                db, steps=RUN_STEPS, preview=False, trigger="scheduled", scheduled_slot=SLOT.astimezone(NY)
            )
            db.commit()
        finally:
            db.close()
        self.assertEqual(as_utc(self.run_row(started.run_id).scheduled_slot), SLOT)

    def test_real_success_after(self) -> None:
        first = self.add_run(status="failed", trigger="scheduled", started=NOW)
        db = self.Session()
        try:
            self.assertFalse(runner_module.real_success_after(db, first.id))
            self.add_run(status="succeeded", trigger="preview", started=NOW)
            self.assertFalse(runner_module.real_success_after(db, first.id))
            self.add_run(status="failed", trigger="manual", started=NOW)
            self.assertFalse(runner_module.real_success_after(db, first.id))
            later = self.add_run(status="succeeded", trigger="scheduled", started=NOW)
            self.assertTrue(runner_module.real_success_after(db, first.id))
            self.assertFalse(runner_module.real_success_after(db, later.id))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
