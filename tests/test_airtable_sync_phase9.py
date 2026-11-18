"""Airtable sync, phase 9 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §12;
v1 plan §7, §8, C1, C2): the scheduler tick.

  - slots: daily / weekly, zones west and east of UTC (and a half-hour one),
    DST spring-forward (shifted by the gap) and fall-back (first
    occurrence; owner, 2026-10-24), exactly one slot per day all year
  - due only when enabled, newer than `last_scheduled_slot`, < 6 h old
    (measured from the slot)
  - due → a real run: trigger 'scheduled', all four steps, no starter,
    `last_scheduled_slot` in the run row's transaction; start and finish
    notifications to every Hub Admin (no actor, nobody excluded)
  - busy → the slot is consumed and "skipped" logged; the next slot runs; a
    stale running row is interrupted and the scheduled run starts
  - other failures consume nothing
  - POST /tick: X-Sync-Token only (unset 503, missing / wrong / non-ASCII
    401, a Hub Admin JWT is not enough), 202 when started, back at once

In-memory SQLite with every session closed, reusing phase 7's fixture (admins,
a runner with stub steps, the app with its dependencies overridden). The
clock is frozen: `scheduler.tick(now=…)`, or `scheduler._utc_now` patched.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

# First: phase 7's module sets the test environment before importing the app.
from tests.test_airtable_sync_phase7 import (  # isort: skip
    ADMIN_EMAIL,
    ALIAS_ADMIN_ALIAS,
    ALL_STEPS,
    FINISHED,
    OTHER_ADMIN_EMAIL,
    STARTED,
    Phase7Fixture,
)

from fastapi import HTTPException  # noqa: E402

from app.db_v2.models.airtable_sync import AirtableSyncRunV2, AirtableSyncSettingsV2  # noqa: E402
from app.routers import airtable_sync_tick  # noqa: E402
from app.services.airtable_sync import scheduler  # noqa: E402
from app.services.airtable_sync.runner import SyncRunInProgress, SyncRunner  # noqa: E402
from app.services.airtable_sync.settings_service import get_settings_row  # noqa: E402

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
TOKEN = "tick-token-for-tests"
ADMINS = sorted([ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS])


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def schedule(**kw) -> SimpleNamespace:
    """A settings row for the pure functions."""
    values = dict(
        schedule_enabled=True,
        frequency="daily",
        run_time=time(6, 0),
        run_weekday=None,
        timezone="America/New_York",
        last_scheduled_slot=None,
    )
    values.update(kw)
    return SimpleNamespace(**values)


def slot_of(now: datetime, **kw) -> datetime:
    row = schedule(**kw)
    return scheduler.latest_slot(
        now, frequency=row.frequency, run_time=row.run_time, run_weekday=row.run_weekday, zone=ZoneInfo(row.timezone)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slots (pure)
# ─────────────────────────────────────────────────────────────────────────────


class SlotTests(unittest.TestCase):
    def test_daily_today_once_the_time_has_passed(self) -> None:
        # 2026-10-24 08:00 EDT: today's 06:00 EDT = 10:00 UTC.
        self.assertEqual(slot_of(utc(2026, 10, 24, 12, 0)), utc(2026, 10, 24, 10, 0))

    def test_daily_yesterday_before_the_time(self) -> None:
        # 05:00 EDT: today's slot is still ahead.
        self.assertEqual(slot_of(utc(2026, 10, 24, 9, 0)), utc(2026, 10, 23, 10, 0))

    def test_the_slot_instant_itself_counts(self) -> None:
        self.assertEqual(slot_of(utc(2026, 10, 24, 10, 0)), utc(2026, 10, 24, 10, 0))
        self.assertEqual(slot_of(utc(2026, 10, 24, 9, 59, 59)), utc(2026, 10, 23, 10, 0))

    def test_the_local_date_decides_not_the_utc_date(self) -> None:
        # 2026-10-25 02:00 UTC is still the 24th (22:00 EDT) in New York.
        self.assertEqual(slot_of(utc(2026, 10, 25, 2, 0), run_time=time(21, 0)), utc(2026, 10, 25, 1, 0))
        self.assertEqual(slot_of(utc(2026, 10, 25, 0, 30), run_time=time(21, 0)), utc(2026, 10, 24, 1, 0))

    def test_zone_east_of_utc(self) -> None:
        # Tokyo 06:00 = 21:00 UTC the day before.
        self.assertEqual(slot_of(utc(2026, 10, 24, 22, 0), timezone="Asia/Tokyo"), utc(2026, 10, 24, 21, 0))
        self.assertEqual(slot_of(utc(2026, 10, 24, 20, 0), timezone="Asia/Tokyo"), utc(2026, 10, 23, 21, 0))

    def test_half_hour_zone_without_dst(self) -> None:
        # Kolkata (+05:30, no DST) 06:00 = 00:30 UTC, all year.
        for day in (date(2026, 1, 15), date(2026, 3, 8), date(2026, 7, 1), date(2026, 11, 1)):
            now = datetime.combine(day, time(12, 0), tzinfo=UTC)
            self.assertEqual(slot_of(now, timezone="Asia/Kolkata"), datetime.combine(day, time(0, 30), tzinfo=UTC))

    def test_utc_zone(self) -> None:
        self.assertEqual(slot_of(utc(2026, 10, 24, 6, 1), timezone="UTC"), utc(2026, 10, 24, 6, 0))

    def test_slots_are_aware_utc(self) -> None:
        slot = slot_of(utc(2026, 10, 24, 12, 0), timezone="Asia/Tokyo")
        self.assertIs(slot.tzinfo, UTC)

    # ── weekly ──────────────────────────────────────────────────────────

    def test_weekly_today_when_the_time_has_passed(self) -> None:
        # 2026-10-24 is a Saturday (5).
        self.assertEqual(slot_of(utc(2026, 10, 24, 12, 0), frequency="weekly", run_weekday=5), utc(2026, 10, 24, 10, 0))

    def test_weekly_a_week_ago_when_today_is_too_early(self) -> None:
        self.assertEqual(slot_of(utc(2026, 10, 24, 9, 0), frequency="weekly", run_weekday=5), utc(2026, 10, 17, 10, 0))

    def test_weekly_latest_matching_weekday(self) -> None:
        # Monday (0) before Saturday the 24th = the 19th; Friday (4) = the 23rd.
        self.assertEqual(slot_of(utc(2026, 10, 24, 12, 0), frequency="weekly", run_weekday=0), utc(2026, 10, 19, 10, 0))
        self.assertEqual(slot_of(utc(2026, 10, 24, 12, 0), frequency="weekly", run_weekday=4), utc(2026, 10, 23, 10, 0))
        # Sunday (6) = the 18th.
        self.assertEqual(slot_of(utc(2026, 10, 24, 12, 0), frequency="weekly", run_weekday=6), utc(2026, 10, 18, 10, 0))

    def test_weekly_weekday_is_the_local_one(self) -> None:
        # Tokyo Monday 06:00 = Sunday 21:00 UTC.
        self.assertEqual(
            slot_of(utc(2026, 10, 25, 22, 0), frequency="weekly", run_weekday=0, timezone="Asia/Tokyo"),
            utc(2026, 10, 25, 21, 0),
        )

    # ── DST (owner, 2026-10-24: gap → shifted forward; repeat → first) ──

    def test_spring_forward_gap_is_shifted_by_the_gap(self) -> None:
        # 2026-03-08 02:30 doesn't exist in New York → 03:30 EDT = 07:30 UTC.
        slot = slot_of(utc(2026, 3, 8, 12, 0), run_time=time(2, 30))
        self.assertEqual(slot, utc(2026, 3, 8, 7, 30))
        self.assertEqual(slot.astimezone(NY).strftime("%H:%M %Z"), "03:30 EDT")
        # The days around it are normal: 02:30 EST / 02:30 EDT.
        self.assertEqual(slot_of(utc(2026, 3, 7, 12, 0), run_time=time(2, 30)), utc(2026, 3, 7, 7, 30))
        self.assertEqual(slot_of(utc(2026, 3, 9, 12, 0), run_time=time(2, 30)), utc(2026, 3, 9, 6, 30))

    def test_spring_forward_ordinary_time_moves_one_hour_in_utc(self) -> None:
        self.assertEqual(slot_of(utc(2026, 3, 7, 12, 0)), utc(2026, 3, 7, 11, 0))  # 06:00 EST
        self.assertEqual(slot_of(utc(2026, 3, 8, 12, 0)), utc(2026, 3, 8, 10, 0))  # 06:00 EDT

    def test_fall_back_repeated_time_is_the_first_occurrence(self) -> None:
        # 2026-11-01 01:30 happens twice; the first is 01:30 EDT = 05:30 UTC.
        slot = slot_of(utc(2026, 11, 1, 12, 0), run_time=time(1, 30))
        self.assertEqual(slot, utc(2026, 11, 1, 5, 30))
        self.assertEqual(slot.astimezone(NY).strftime("%H:%M %Z"), "01:30 EDT")
        # During the second pass (01:40 EST) it is still that first instant.
        self.assertEqual(slot_of(utc(2026, 11, 1, 6, 40), run_time=time(1, 30)), utc(2026, 11, 1, 5, 30))

    def test_another_zones_gap(self) -> None:
        # London 2026-03-29 01:30 doesn't exist → 02:30 BST = 01:30 UTC.
        self.assertEqual(
            slot_of(utc(2026, 3, 29, 12, 0), run_time=time(1, 30), timezone="Europe/London"), utc(2026, 3, 29, 1, 30)
        )

    def _simulate_year(self, **kw) -> list[tuple[datetime, datetime]]:
        """Every 5-minute tick of 2026, consuming each slot it runs:
        (slot, the tick that ran it)."""
        row = schedule(**kw)
        runs: list[tuple[datetime, datetime]] = []
        now = utc(2026, 1, 1, 0, 0)
        while now < utc(2027, 1, 1, 0, 0):
            decision = scheduler.decide(row, now)
            if decision.due:
                runs.append((decision.slot, now))
                row.last_scheduled_slot = decision.slot
            now += timedelta(minutes=5)
        return runs

    def test_exactly_one_run_per_local_day_all_year_through_both_changes(self) -> None:
        for run_time in (time(2, 30), time(1, 30), time(6, 0), time(0, 0), time(23, 55)):
            with self.subTest(run_time=run_time):
                runs = self._simulate_year(run_time=run_time)
                days = [slot.astimezone(NY).date() for slot, _ in runs]
                self.assertEqual(len(days), len(set(days)))
                # 2026-01-01 00:00 UTC is still Dec 31 in New York, and the
                # year's last local day ends after 2027-01-01 00:00 UTC.
                in_2026 = {d for d in days if d.year == 2026}
                expected = 365 if run_time < time(19, 0) else 364
                self.assertEqual(len(in_2026), expected)
                # Each slot runs on the first tick at or after it.
                for slot, ran_at in runs[1:]:
                    self.assertTrue(timedelta(0) <= ran_at - slot < timedelta(minutes=5), (slot, ran_at))

    def test_weekly_all_year(self) -> None:
        runs = self._simulate_year(frequency="weekly", run_weekday=6, run_time=time(2, 30))
        self.assertEqual(len(runs), 52)  # Sundays of 2026: Jan 4 … Dec 27
        self.assertTrue(all(slot.astimezone(NY).weekday() == 6 for slot, _ in runs))


# ─────────────────────────────────────────────────────────────────────────────
# decide (pure)
# ─────────────────────────────────────────────────────────────────────────────


class DecideTests(unittest.TestCase):
    NOW = utc(2026, 10, 24, 12, 0)  # today's slot: 10:00 UTC
    SLOT = utc(2026, 10, 24, 10, 0)

    def test_due(self) -> None:
        self.assertEqual(scheduler.decide(schedule(), self.NOW), scheduler.Decision(True, None, self.SLOT))

    def test_disabled_is_never_due(self) -> None:
        self.assertEqual(scheduler.decide(schedule(schedule_enabled=False), self.NOW).reason, "schedule_disabled")

    def test_slot_already_used(self) -> None:
        decision = scheduler.decide(schedule(last_scheduled_slot=self.SLOT), self.NOW)
        self.assertEqual((decision.due, decision.reason, decision.slot), (False, "slot_already_used", self.SLOT))

    def test_a_newer_last_slot_also_blocks(self) -> None:
        # e.g. the time was moved earlier after today's run.
        decision = scheduler.decide(schedule(last_scheduled_slot=self.SLOT + timedelta(hours=1)), self.NOW)
        self.assertEqual(decision.reason, "slot_already_used")

    def test_an_older_last_slot_is_due(self) -> None:
        self.assertTrue(scheduler.decide(schedule(last_scheduled_slot=self.SLOT - timedelta(days=1)), self.NOW).due)

    def test_naive_last_slot_is_read_as_utc(self) -> None:
        # SQLite hands it back naive.
        naive = self.SLOT.replace(tzinfo=None)
        self.assertEqual(scheduler.decide(schedule(last_scheduled_slot=naive), self.NOW).reason, "slot_already_used")

    def test_six_hour_limit(self) -> None:
        self.assertTrue(scheduler.decide(schedule(), self.SLOT + timedelta(hours=5, minutes=59, seconds=59)).due)
        decision = scheduler.decide(schedule(), self.SLOT + timedelta(hours=6))
        self.assertEqual((decision.due, decision.reason, decision.slot), (False, "slot_too_old", self.SLOT))

    def test_six_hours_are_measured_from_the_slot_not_the_last_run(self) -> None:
        # The last run was long ago; the slot itself is 1 h old → due.
        old = schedule(last_scheduled_slot=utc(2026, 1, 1))
        self.assertTrue(scheduler.decide(old, self.SLOT + timedelta(hours=1)).due)
        # The last run was recent-ish; the slot is 7 h old → not due.
        recent = schedule(last_scheduled_slot=self.SLOT - timedelta(days=1))
        self.assertEqual(scheduler.decide(recent, self.SLOT + timedelta(hours=7)).reason, "slot_too_old")

    def test_a_long_outage_skips_to_the_next_slot(self) -> None:
        # Down from before yesterday's slot until 20:00 today: nothing runs now…
        row = schedule(last_scheduled_slot=self.SLOT - timedelta(days=2))
        self.assertEqual(scheduler.decide(row, self.SLOT + timedelta(hours=10)).reason, "slot_too_old")
        # …and tomorrow's slot runs normally.
        self.assertTrue(scheduler.decide(row, self.SLOT + timedelta(days=1, minutes=3)).due)

    def test_invalid_schedules_never_crash(self) -> None:
        for kw in (
            {"frequency": "weekly", "run_weekday": None},
            {"frequency": "weekly", "run_weekday": 7},
            {"frequency": "hourly"},
            {"timezone": "Mars/Olympus_Mons"},
            {"timezone": "../etc"},
        ):
            with self.subTest(**kw):
                self.assertEqual(scheduler.decide(schedule(**kw), self.NOW).reason, "invalid_schedule")

    def test_daily_ignores_a_stray_weekday(self) -> None:
        self.assertTrue(scheduler.decide(schedule(run_weekday=3), self.NOW).due)


# ─────────────────────────────────────────────────────────────────────────────
# tick (SQLite, the real runner with stub steps)
# ─────────────────────────────────────────────────────────────────────────────

NOW = utc(2026, 10, 24, 12, 0)
SLOT = utc(2026, 10, 24, 10, 0)


class TickFixture(Phase7Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.set_schedule(schedule_enabled=True)
        # The runner's clock too: staleness is judged against it.
        clock = mock.patch("app.services.airtable_sync.runner._utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def set_schedule(self, **kw) -> None:
        row = get_settings_row(self.db)
        for key, value in kw.items():
            setattr(row, key, value)
        self.db.commit()

    def settings_row(self) -> AirtableSyncSettingsV2:
        db = self.Session()
        try:
            return get_settings_row(db)
        finally:
            db.close()

    def last_slot(self) -> datetime | None:
        value = self.settings_row().last_scheduled_slot
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

    def runs(self) -> list[AirtableSyncRunV2]:
        self.db.expire_all()
        return self.db.query(AirtableSyncRunV2).order_by(AirtableSyncRunV2.id).all()

    def tick(self, now: datetime = NOW, runner: SyncRunner | None = None) -> dict:
        db = self.Session()
        try:
            return scheduler.tick(db, runner or self.make_runner(), now=now)
        finally:
            db.close()


class TickTests(TickFixture):
    def test_due_slot_starts_a_scheduled_run_of_every_step(self) -> None:
        result = self.tick()
        run = self.runs()[-1]
        self.assertEqual(result, {"result": "started", "run_id": run.id, "slot": SLOT.isoformat()})
        self.assertEqual(run.trigger, "scheduled")
        self.assertEqual(list(run.steps), list(ALL_STEPS))
        self.assertIsNone(run.started_by_user_id)
        self.assertIsNone(run.started_by_email)
        self.assertEqual(run.status, "succeeded")
        self.assertFalse(run.summary["preview"])
        self.assertEqual(sorted(run.summary["steps"]), sorted(ALL_STEPS))
        self.assertEqual(self.last_slot(), SLOT)

    def test_notifications_go_to_every_admin_with_no_actor(self) -> None:
        self.tick()
        for type_ in (STARTED, FINISHED):
            self.assertEqual(self.recipients(type_), ADMINS)
            for note in self.notes(type_):
                self.assertIsNone(note.actor_email)
                self.assertIsNone(note.actor_name)
        self.assertEqual({n.excerpt for n in self.notes(STARTED)}, {"Scheduled Airtable sync started"})

    def test_the_slot_is_written_in_the_run_rows_transaction(self) -> None:
        seen: dict = {}

        def launch(started) -> None:  # called right after the tick's commit
            seen["slot"] = self.last_slot()
            seen["status"] = self.run_row(started.run_id).status

        runner = self.make_runner()
        runner.launch = launch
        self.tick(runner=runner)
        self.assertEqual(seen, {"slot": SLOT, "status": "running"})

    def test_same_slot_runs_once(self) -> None:
        self.tick()
        again = self.tick(NOW + timedelta(minutes=5))
        self.assertEqual(again, {"result": "not_due", "reason": "slot_already_used", "slot": SLOT.isoformat()})
        self.assertEqual(len(self.runs()), 1)
        # The next day's slot runs.
        self.assertEqual(self.tick(NOW + timedelta(days=1))["result"], "started")
        self.assertEqual(self.last_slot(), SLOT + timedelta(days=1))
        self.assertEqual(len(self.runs()), 2)

    def test_disabled_does_nothing(self) -> None:
        self.set_schedule(schedule_enabled=False)
        self.assertEqual(self.tick(), {"result": "not_due", "reason": "schedule_disabled", "slot": None})
        self.assertEqual(self.runs(), [])
        self.assertIsNone(self.last_slot())
        self.assertEqual(self.notes(), [])

    def test_too_old_does_nothing_and_consumes_nothing(self) -> None:
        result = self.tick(SLOT + timedelta(hours=6, minutes=1))
        self.assertEqual(result["reason"], "slot_too_old")
        self.assertEqual(self.runs(), [])
        self.assertIsNone(self.last_slot())

    def test_weekly_on_sqlite(self) -> None:
        self.set_schedule(frequency="weekly", run_weekday=5, run_time=time(7, 15), timezone="Asia/Tokyo")
        # Saturday 07:15 JST = Friday 22:15 UTC.
        self.assertEqual(self.tick(utc(2026, 10, 23, 22, 20))["result"], "started")
        self.assertEqual(self.last_slot(), utc(2026, 10, 23, 22, 15))
        self.assertEqual(self.tick(utc(2026, 10, 24, 22, 20))["reason"], "slot_already_used")

    def test_creates_the_settings_row_if_missing(self) -> None:
        self.db.query(AirtableSyncSettingsV2).delete()
        self.db.commit()
        self.assertEqual(self.tick()["reason"], "schedule_disabled")
        self.assertFalse(self.settings_row().schedule_enabled)

    # ── busy lock (v1 C1) ───────────────────────────────────────────────

    def test_busy_lock_consumes_the_slot(self) -> None:
        busy = self.add_run(status="running", trigger="manual", started=NOW - timedelta(minutes=1))
        with self.assertLogs(scheduler.logger, "WARNING") as logs:
            result = self.tick()
        self.assertEqual(
            result,
            {"result": "skipped", "reason": "run_in_progress", "slot": SLOT.isoformat(), "blocking_run_id": busy.id},
        )
        self.assertIn("skipped: run in progress", logs.output[0])
        self.assertEqual(self.last_slot(), SLOT)
        self.assertEqual([r.id for r in self.runs()], [busy.id])
        self.assertEqual(self.run_row(busy.id).status, "running")
        self.assertEqual(self.notes(), [])
        # Still busy later: the consumed slot isn't retried.
        self.assertEqual(self.tick(NOW + timedelta(minutes=30))["reason"], "slot_already_used")

    def test_after_a_skip_the_next_slot_runs_normally(self) -> None:
        busy = self.add_run(status="running", started=NOW - timedelta(minutes=1))
        self.tick()
        busy = self.run_row(busy.id)
        busy.status, busy.finished_at = "succeeded", NOW
        self.db.commit()
        self.assertEqual(self.tick(NOW + timedelta(days=1))["result"], "started")
        self.assertEqual(self.last_slot(), SLOT + timedelta(days=1))

    def test_a_running_preview_also_blocks_and_consumes(self) -> None:
        self.add_run(status="running", trigger="preview", started=NOW - timedelta(minutes=1))
        self.assertEqual(self.tick()["result"], "skipped")
        self.assertEqual(self.last_slot(), SLOT)

    def test_a_stale_running_row_is_interrupted_and_the_scheduled_run_starts(self) -> None:
        stale = self.add_run(status="running", started=NOW - timedelta(minutes=30))
        result = self.tick()
        self.assertEqual(result["result"], "started")
        self.assertEqual(self.run_row(stale.id).status, "interrupted")
        self.assertEqual(self.run_row(result["run_id"]).trigger, "scheduled")
        self.assertEqual(self.last_slot(), SLOT)

    def test_insert_race_still_consumes_the_slot(self) -> None:
        """`start` rolls the session back on the losing side of the insert
        race; the slot write must come after that."""
        runner = self.make_runner()

        def lose_the_race(db, **_kw):
            get_settings_row(db).last_scheduled_slot = utc(2020, 1, 1)  # would be lost
            db.rollback()
            raise SyncRunInProgress("run_in_progress", "Another Airtable sync run has just started.")

        runner.start = lose_the_race
        result = self.tick(runner=runner)
        self.assertEqual(result["result"], "skipped")
        self.assertIsNone(result["blocking_run_id"])
        self.assertEqual(self.last_slot(), SLOT)

    def test_consume_never_moves_backwards(self) -> None:
        later = SLOT + timedelta(days=3)
        self.set_schedule(last_scheduled_slot=later)
        db = self.Session()
        try:
            scheduler._consume(db, SLOT)
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.last_slot(), later)

    # ── other failures consume nothing ──────────────────────────────────

    def test_a_start_failure_consumes_nothing(self) -> None:
        runner = self.make_runner()
        runner.start = mock.Mock(side_effect=RuntimeError("database went away"))
        with self.assertRaises(RuntimeError):
            self.tick(runner=runner)
        self.assertIsNone(self.last_slot())
        self.assertEqual(self.runs(), [])
        # The next tick tries the same slot again.
        self.assertEqual(self.tick(NOW + timedelta(minutes=5))["result"], "started")
        self.assertEqual(self.last_slot(), SLOT)

    def test_a_settings_read_failure_consumes_nothing(self) -> None:
        with mock.patch.object(scheduler, "get_settings_row", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.assertIsNone(self.last_slot())
        self.assertEqual(self.runs(), [])

    def test_a_failed_run_still_used_its_slot(self) -> None:
        # Rewritten in RI-2 (owner's OK, 2026-10-30): the next ticks retry the
        # slot (attempts 2 and 3) without ever moving it, then stop.
        self.fail_step = "users"
        result = self.tick()
        self.assertEqual(self.run_row(result["run_id"]).status, "failed")
        self.assertEqual(self.last_slot(), SLOT)
        for minutes, attempt in ((5, 2), (10, 3)):
            retry = self.tick(NOW + timedelta(minutes=minutes))
            self.assertEqual((retry["result"], retry["slot"], retry["attempt"]), ("started", SLOT.isoformat(), attempt))
            self.assertEqual(self.last_slot(), SLOT)
        after = self.tick(NOW + timedelta(minutes=15))
        self.assertEqual((after["reason"], after["retry_reason"]), ("slot_already_used", "attempts_exhausted"))
        self.assertEqual(self.last_slot(), SLOT)
        self.assertEqual(len(self.runs()), 3)

    # ── only the tick writes the slot ───────────────────────────────────

    def test_manual_runs_and_previews_never_touch_the_slot(self) -> None:
        for preview in (True, False):
            self.assertEqual(self.start(preview=preview).status_code, 202)
        self.assertIsNone(self.last_slot())
        # …and don't use the scheduled slot up either.
        self.assertEqual(self.tick()["result"], "started")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/airtable-sync/tick
# ─────────────────────────────────────────────────────────────────────────────


class TickEndpointTests(TickFixture):
    def setUp(self) -> None:
        super().setUp()
        self.token = TOKEN
        patcher = mock.patch.object(
            airtable_sync_tick, "get_settings", side_effect=lambda: SimpleNamespace(AIRTABLE_SYNC_TOKEN=self.token)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = mock.patch.object(scheduler, "_utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def post(self, headers: dict | None = None):
        return self.client.post("/v2/airtable-sync/tick", headers=headers or {})

    def test_good_token_starts_the_run_202(self) -> None:
        response = self.post({"X-Sync-Token": TOKEN})
        self.assertEqual(response.status_code, 202)
        run = self.runs()[-1]
        self.assertEqual(
            response.json(),
            {
                "data": {
                    "result": "started",
                    "reason": None,
                    "slot": "2026-10-24T10:00:00Z",
                    "run_id": run.id,
                    "blocking_run_id": None,
                    # RI-2 (owner's OK, 2026-10-30): null on a first try.
                    "attempt": None,
                    "retry_reason": None,
                    "bulk_job_id": None,
                }
            },
        )
        self.assertEqual(run.trigger, "scheduled")

    def test_not_due_is_200(self) -> None:
        self.post({"X-Sync-Token": TOKEN})
        response = self.post({"X-Sync-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["result"], "not_due")
        self.assertEqual(response.json()["data"]["reason"], "slot_already_used")

    def test_skipped_is_200_with_the_blocking_run(self) -> None:
        busy = self.add_run(status="running", started=NOW - timedelta(minutes=1))
        response = self.post({"X-Sync-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual((data["result"], data["reason"], data["blocking_run_id"]), ("skipped", "run_in_progress", busy.id))

    def test_returns_with_the_run_still_in_the_background(self) -> None:
        pending: list = []
        self.executor = pending.append  # the run's body is not executed yet
        response = self.post({"X-Sync-Token": TOKEN})
        self.assertEqual(response.status_code, 202)
        run_id = response.json()["data"]["run_id"]
        self.assertEqual(self.run_row(run_id).status, "running")
        self.assertEqual(self.recipients(STARTED), ADMINS)  # sent before the background work
        self.assertEqual(self.notes(FINISHED), [])
        pending[0]()
        self.assertEqual(self.run_row(run_id).status, "succeeded")
        self.assertEqual(self.recipients(FINISHED), ADMINS)

    # ── the token ───────────────────────────────────────────────────────

    def test_token_unset_is_503(self) -> None:
        for unset in (None, "", "   "):
            with self.subTest(token=unset):
                self.token = unset
                response = self.post({"X-Sync-Token": TOKEN})
                self.assertEqual(response.status_code, 503)
        self.assertEqual(self.runs(), [])

    def test_missing_or_wrong_token_is_401(self) -> None:
        for headers in ({}, {"X-Sync-Token": ""}, {"X-Sync-Token": "wrong"}, {"X-Sync-Token": TOKEN + "x"}, {"X-Sync-Token": TOKEN[:-1]}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers).status_code, 401)
        self.assertEqual(self.runs(), [])
        self.assertIsNone(self.last_slot())

    def test_non_ascii_token_is_401_not_500(self) -> None:
        response = self.post({"X-Sync-Token": "tök€n".encode("utf-8")})
        self.assertEqual(response.status_code, 401)
        with self.assertRaises(HTTPException) as caught:
            airtable_sync_tick._check_sync_token("tök€n")
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_non_ascii_backend_token_is_401_never_500(self) -> None:
        # Headers arrive latin-1-decoded, so a non-ASCII secret can't match
        # over HTTP: the token must be ASCII (the handoff says so). It still
        # fails closed, and the comparison itself works on str.
        self.token = "jeton-é"
        self.assertEqual(self.post({"X-Sync-Token": "jeton-é".encode("utf-8")}).status_code, 401)
        airtable_sync_tick._check_sync_token("jeton-é")  # no exception

    def test_the_expected_token_is_trimmed(self) -> None:
        self.token = f"  {TOKEN}\n"
        self.assertEqual(self.post({"X-Sync-Token": TOKEN}).status_code, 202)

    def test_a_hub_admin_jwt_is_not_enough(self) -> None:
        self.assertEqual(self.post(self.auth(ADMIN_EMAIL)).status_code, 401)
        self.assertEqual(self.post({**self.auth(ADMIN_EMAIL), "X-Sync-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.runs(), [])

    def test_no_user_is_needed(self) -> None:
        # A token alone works; no Authorization header at all.
        self.assertEqual(self.post({"X-Sync-Token": TOKEN}).status_code, 202)

    def test_the_admin_routes_still_need_a_hub_admin(self) -> None:
        token_only = {"X-Sync-Token": TOKEN}
        for method, path in (
            ("get", "/v2/airtable-sync/status"),
            ("get", "/v2/airtable-sync/settings"),
            ("get", "/v2/airtable-sync/runs"),
            ("get", "/v2/airtable-sync/items"),
        ):
            with self.subTest(path=path):
                self.assertIn(getattr(self.client, method)(path, headers=token_only).status_code, (401, 403))
        member = self.client.get("/v2/airtable-sync/status", headers=self.auth("member@renphil.org"))
        self.assertEqual(member.status_code, 403)
        self.assertEqual(self.client.get("/v2/airtable-sync/status", headers=self.auth(ADMIN_EMAIL)).status_code, 200)

    def test_get_is_not_allowed(self) -> None:
        self.assertEqual(self.client.get("/v2/airtable-sync/tick", headers={"X-Sync-Token": TOKEN}).status_code, 405)

    def test_a_server_error_consumes_nothing(self) -> None:
        with mock.patch.object(scheduler, "get_settings_row", side_effect=RuntimeError("boom")):
            self.assertEqual(self.post({"X-Sync-Token": TOKEN}).status_code, 500)
        self.assertIsNone(self.last_slot())


if __name__ == "__main__":
    unittest.main()
