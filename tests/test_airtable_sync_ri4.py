"""Airtable sync RI-4 (AI Docs/plan_airtable_sync_retries_interval_2026-10-29.md
§5.1–§5.4, §9 RI-4): interval schedules ("every N hours"), backend.

  - slots: every allowed interval in zones without DST (Kolkata +05:30,
    Tokyo, UTC) at assorted times; `latest_slot` ≤ now, `next_slot` > now;
    two grid times on one instant merge; the 2-day look-back (a made-up zone,
    since no tzdata zone needs it 2000–2040)
  - §5.3's table, exact instants (New York and Beirut DST days)
  - year simulations of 5-min ticks (60/120/240/720, New York and Beirut):
    each slot runs on the first tick after it, no local time twice on a date,
    off the grid only on the jump-forward day, exactly §5.3's shifted run
  - 480/720 after a 7 h outage → `slot_too_old`, then the next slot runs
  - §5.4's switches (daily ↔ interval, enabling)
  - `decide`: RI-1's guard gone; an interval row with no / an unlisted value
    is `invalid_schedule`; `interval_minutes` ignored for daily / weekly
  - the API: 422 for 30/45/0/1441, the four 409 codes, `next_slot` in GET /
    PUT / status
  - retry room under interval: the 60-min "failed after 2 attempts" case,
    end to end (RI-3's wording, no stub)

In-memory SQLite on phase 9's TickFixture / RI-3's Ri3Fixture (phase 7's
admins, stub steps, `fail_step`, frozen clocks). Pure tests use phase 9's
`schedule()` rows.
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import date, datetime, time, timedelta, tzinfo
from unittest import mock
from zoneinfo import ZoneInfo

# First: phase 7's module (via phase 9's) sets the test environment.
from tests.test_airtable_sync_phase9 import NOW, SLOT, UTC, TickFixture, schedule, utc  # isort: skip

from app.db_v2.models.airtable_sync import INTERVAL_MINUTES  # noqa: E402
from app.schemas.airtable_sync import SyncSettingsResponse, UpdateSyncSettingsRequest  # noqa: E402
from app.services.airtable_sync import runs_service, scheduler, settings_service  # noqa: E402
from app.services.airtable_sync.settings_service import (  # noqa: E402
    AirtableSyncError,
    get_settings_row,
    serialize_settings,
    update_settings,
)
from tests.test_airtable_sync_phase7 import ADMIN_EMAIL  # noqa: E402
from tests.test_airtable_sync_ri3 import CRASH, Ri3Fixture  # noqa: E402

NY = ZoneInfo("America/New_York")
BEIRUT = ZoneInfo("Asia/Beirut")
KOLKATA = ZoneInfo("Asia/Kolkata")
TOKYO = ZoneInfo("Asia/Tokyo")
FIVE = timedelta(minutes=5)


def slots(day: date, minutes: int, zone) -> list[datetime]:
    return scheduler.interval_slots(day, minutes, zone)


def latest(now: datetime, minutes: int, zone, **kw) -> datetime:
    args = dict(frequency="interval", run_time=time(6, 0), run_weekday=None, interval_minutes=minutes, zone=zone)
    args.update(kw)
    return scheduler.latest_slot(now, **args)


def upcoming(now: datetime, minutes: int, zone, **kw) -> datetime:
    args = dict(frequency="interval", run_time=time(6, 0), run_weekday=None, interval_minutes=minutes, zone=zone)
    args.update(kw)
    return scheduler.next_slot(now, **args)


def interval_row(minutes: int = 60, **kw):
    return schedule(frequency="interval", interval_minutes=minutes, **kw)


def wall(slot: datetime, zone) -> tuple[date, time]:
    local = slot.astimezone(zone)
    return local.date(), local.time().replace(tzinfo=None)


class JumpZone(tzinfo):
    """A made-up zone: UTC+0 until 2026-01-01 23:30 UTC, then UTC+1. Its
    clocks jump 23:30 → 00:30, so 2026-01-02 starts at 00:30 while its
    midnight (fold=0, shifted by the jump) resolves to 01:00: between the
    two, today has no slot yet and `latest_slot` needs yesterday. No tzdata
    zone does this in 2000–2040 (scanned 2026-10-31); this is what the 2-day
    look-back is for."""

    JUMP = datetime(2026, 1, 1, 23, 30)  # UTC = the old wall time

    def utcoffset(self, dt):
        w = dt.replace(tzinfo=None)
        if w < self.JUMP:
            return timedelta(0)
        if w >= self.JUMP + timedelta(hours=1):
            return timedelta(hours=1)
        return timedelta(hours=1) if dt.fold else timedelta(0)  # the gap

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "JUMP"

    def fromutc(self, dt):
        return dt + (timedelta(hours=1) if dt.replace(tzinfo=None) >= self.JUMP else timedelta(0))


# ─────────────────────────────────────────────────────────────────────────────
# Slots, zones without DST
# ─────────────────────────────────────────────────────────────────────────────


class GridTests(unittest.TestCase):
    ZONES = {"Asia/Kolkata": (KOLKATA, timedelta(hours=5, minutes=30)), "Asia/Tokyo": (TOKYO, timedelta(hours=9)), "UTC": (ZoneInfo("UTC"), timedelta(0))}
    TIMES = (time(0, 0), time(0, 1), time(5, 59), time(6, 0), time(11, 29), time(12, 0), time(17, 45), time(23, 59, 59))

    def test_every_allowed_interval_has_its_grid(self) -> None:
        day = date(2026, 6, 15)
        for name, (zone, offset) in self.ZONES.items():
            midnight = datetime.combine(day, time(0), tzinfo=UTC) - offset
            for minutes in INTERVAL_MINUTES:
                with self.subTest(zone=name, minutes=minutes):
                    expected = [midnight + timedelta(minutes=m) for m in range(0, 1440, minutes)]
                    self.assertEqual(slots(day, minutes, zone), expected)
                    self.assertEqual(len(expected), 1440 // minutes)

    def test_latest_and_next_at_assorted_times(self) -> None:
        day = date(2026, 6, 15)
        for name, (zone, offset) in self.ZONES.items():
            midnight = datetime.combine(day, time(0), tzinfo=UTC) - offset
            for minutes in INTERVAL_MINUTES:
                for at in self.TIMES:
                    now = midnight + timedelta(hours=at.hour, minutes=at.minute, seconds=at.second)
                    since = at.hour * 60 + at.minute
                    want = midnight + timedelta(minutes=since - since % minutes)
                    with self.subTest(zone=name, minutes=minutes, at=at):
                        self.assertEqual(latest(now, minutes, zone), want)
                        self.assertEqual(upcoming(now, minutes, zone), want + timedelta(minutes=minutes))

    def test_a_slot_is_its_own_latest_and_not_its_own_next(self) -> None:
        slot = utc(2026, 6, 15, 3, 30)  # Kolkata 09:00, on the 180 grid
        self.assertEqual(latest(slot, 180, KOLKATA), slot)
        self.assertEqual(upcoming(slot, 180, KOLKATA), slot + timedelta(hours=3))
        self.assertEqual(latest(slot - timedelta(seconds=1), 180, KOLKATA), slot - timedelta(hours=3))

    def test_the_last_slot_of_the_day_reaches_tomorrow(self) -> None:
        # Tokyo 23:00 → the next 720 slot is tomorrow's midnight.
        now = utc(2026, 6, 15, 14, 0)
        self.assertEqual(upcoming(now, 720, TOKYO), utc(2026, 6, 15, 15, 0))
        self.assertEqual(latest(now, 720, TOKYO), utc(2026, 6, 15, 3, 0))

    def test_the_two_day_look_back(self) -> None:
        zone = JumpZone()
        now = utc(2026, 1, 1, 23, 45)  # wall 2026-01-02 00:45: today has no slot yet
        self.assertEqual(now.astimezone(zone).date(), date(2026, 1, 2))
        self.assertGreater(slots(date(2026, 1, 2), 60, zone)[0], now)
        self.assertEqual(latest(now, 60, zone), utc(2026, 1, 1, 23, 0))
        self.assertEqual(upcoming(now, 60, zone), utc(2026, 1, 2, 0, 0))

    def test_run_time_and_weekday_are_ignored_for_interval(self) -> None:
        now = utc(2026, 6, 15, 7, 10)
        for run_time, weekday in ((time(6, 0), None), (time(13, 37), 3)):
            with self.subTest(run_time=run_time):
                self.assertEqual(latest(now, 120, ZoneInfo("UTC"), run_time=run_time, run_weekday=weekday), utc(2026, 6, 15, 6, 0))
                self.assertEqual(upcoming(now, 120, ZoneInfo("UTC"), run_time=run_time, run_weekday=weekday), utc(2026, 6, 15, 8, 0))

    def test_interval_minutes_is_ignored_for_daily_and_weekly(self) -> None:
        now = utc(2026, 6, 15, 12, 0)  # a Monday; NY 08:00
        for frequency, weekday in (("daily", None), ("weekly", 0)):
            with self.subTest(frequency=frequency):
                base = dict(frequency=frequency, run_time=time(6, 0), run_weekday=weekday, zone=NY)
                self.assertEqual(scheduler.latest_slot(now, **base, interval_minutes=60), scheduler.latest_slot(now, **base))
                self.assertEqual(scheduler.next_slot(now, **base, interval_minutes=60), scheduler.next_slot(now, **base))
                self.assertEqual(scheduler.latest_slot(now, **base, interval_minutes=60), utc(2026, 6, 15, 10, 0))


# ─────────────────────────────────────────────────────────────────────────────
# §5.3's table, exact instants
# ─────────────────────────────────────────────────────────────────────────────


class DstTableTests(unittest.TestCase):
    def assert_starts(self, day: date, minutes: int, zone, first: list[datetime], count: int) -> None:
        got = slots(day, minutes, zone)
        self.assertEqual(got[: len(first)], first)
        self.assertEqual(len(got), count)
        self.assertEqual(got, sorted(set(got)))

    # New York, 2026-03-08: 02:00–02:59 missing.
    def test_ny_spring_60(self) -> None:
        got = slots(date(2026, 3, 8), 60, NY)
        self.assertEqual(got[:4], [utc(2026, 3, 8, 5), utc(2026, 3, 8, 6), utc(2026, 3, 8, 7), utc(2026, 3, 8, 8)])
        self.assertEqual([wall(s, NY)[1].hour for s in got[:4]], [0, 1, 3, 4])  # 01:00 → 03:00
        self.assertEqual(len(got), 23)  # the shifted 02:00 is the 03:00 slot: merged

    def test_ny_spring_120(self) -> None:
        self.assert_starts(date(2026, 3, 8), 120, NY, [utc(2026, 3, 8, 5), utc(2026, 3, 8, 7), utc(2026, 3, 8, 8), utc(2026, 3, 8, 10)], 12)
        self.assertEqual(wall(utc(2026, 3, 8, 7), NY)[1], time(3, 0))  # the off-grid 03:00

    def test_ny_spring_240(self) -> None:
        got = slots(date(2026, 3, 8), 240, NY)
        self.assertEqual(got[:2], [utc(2026, 3, 8, 5), utc(2026, 3, 8, 8)])  # 00:00 → 04:00: 3 h real
        self.assertEqual(len(got), 6)

    # New York, 2026-11-01: 01:00–01:59 twice.
    def test_ny_fall_60(self) -> None:
        got = slots(date(2026, 11, 1), 60, NY)
        self.assertEqual(got[:3], [utc(2026, 11, 1, 4), utc(2026, 11, 1, 5), utc(2026, 11, 1, 7)])  # 00 EDT, 01 EDT, 02 EST
        self.assertEqual(got[2] - got[1], timedelta(hours=2))
        self.assertEqual(len(got), 24)

    def test_ny_fall_120(self) -> None:
        got = slots(date(2026, 11, 1), 120, NY)
        self.assertEqual(got[:2], [utc(2026, 11, 1, 4), utc(2026, 11, 1, 7)])  # 3 h real
        self.assertEqual(len(got), 12)

    def test_ny_fall_240(self) -> None:
        got = slots(date(2026, 11, 1), 240, NY)
        self.assertEqual(got[:2], [utc(2026, 11, 1, 4), utc(2026, 11, 1, 9)])  # 5 h real
        self.assertEqual(len(got), 6)

    # Beirut, 2026-03-29: 00:00–00:59 missing (the anchor itself).
    def test_beirut_spring_60(self) -> None:
        got = slots(date(2026, 3, 29), 60, BEIRUT)
        self.assertEqual(got[0], utc(2026, 3, 28, 22))  # 01:00 EEST
        self.assertEqual(wall(got[0], BEIRUT), (date(2026, 3, 29), time(1, 0)))
        self.assertEqual(len(got), 23)

    def test_beirut_spring_120(self) -> None:
        self.assert_starts(date(2026, 3, 29), 120, BEIRUT, [utc(2026, 3, 28, 22), utc(2026, 3, 28, 23), utc(2026, 3, 29, 1)], 12)

    def test_beirut_spring_240(self) -> None:
        self.assert_starts(date(2026, 3, 29), 240, BEIRUT, [utc(2026, 3, 28, 22), utc(2026, 3, 29, 1), utc(2026, 3, 29, 5)], 6)

    # Beirut, 2026-10-24: 23:00–23:59 twice.
    def test_beirut_fall_60(self) -> None:
        day = slots(date(2026, 10, 24), 60, BEIRUT)
        self.assertEqual(day[-1], utc(2026, 10, 24, 20))  # 23:00 EEST; the second 23:00 (21:00 UTC) doesn't run
        self.assertEqual(len(day), 24)
        self.assertEqual(slots(date(2026, 10, 25), 60, BEIRUT)[0], utc(2026, 10, 24, 22))  # 00:00 EET, 2 h later

    def test_beirut_fall_120(self) -> None:
        self.assertEqual(slots(date(2026, 10, 24), 120, BEIRUT)[-1], utc(2026, 10, 24, 19))  # 22:00 EEST
        self.assertEqual(slots(date(2026, 10, 25), 120, BEIRUT)[0], utc(2026, 10, 24, 22))  # 3 h real

    def test_beirut_fall_240(self) -> None:
        self.assertEqual(slots(date(2026, 10, 24), 240, BEIRUT)[-1], utc(2026, 10, 24, 17))  # 20:00 EEST
        self.assertEqual(slots(date(2026, 10, 25), 240, BEIRUT)[0], utc(2026, 10, 24, 22))

    def test_latest_and_next_across_the_ny_gap(self) -> None:
        # 01:30 EST → next is 03:00 EDT (the shifted 02:00 and 03:00, one instant).
        self.assertEqual(upcoming(utc(2026, 3, 8, 6, 30), 60, NY), utc(2026, 3, 8, 7))
        self.assertEqual(latest(utc(2026, 3, 8, 6, 59), 60, NY), utc(2026, 3, 8, 6))

    def test_latest_across_the_ny_repeat(self) -> None:
        # During the second 01:xx (EST), the latest slot is still 01:00 EDT.
        self.assertEqual(latest(utc(2026, 11, 1, 6, 30), 60, NY), utc(2026, 11, 1, 5))
        self.assertEqual(upcoming(utc(2026, 11, 1, 6, 30), 60, NY), utc(2026, 11, 1, 7))


# ─────────────────────────────────────────────────────────────────────────────
# Year simulations (5-min ticks, `decide` + the tick's consume rule)
# ─────────────────────────────────────────────────────────────────────────────


def simulate(minutes: int, zone_name: str) -> list[tuple[datetime, datetime]]:
    """Every 5-min tick from 2025-12-31 12:00 UTC to 2027-01-01 12:00 UTC,
    as the tick runs them: due → run + consume. (tick, slot) pairs."""
    now, end = utc(2025, 12, 31, 12), utc(2027, 1, 1, 12)
    # The slot before the first tick counts as used, so no catch-up run.
    row = interval_row(minutes, timezone=zone_name, last_scheduled_slot=latest(now, minutes, ZoneInfo(zone_name)))
    runs = []
    while now < end:
        decision = scheduler.decide(row, now)
        if decision.due:
            runs.append((now, decision.slot))
            row.last_scheduled_slot = decision.slot
        now += FIVE
    return runs


class YearSimulationTests(unittest.TestCase):
    # The off-grid run of each (zone, interval): §5.3, on the jump-forward day only.
    OFF_GRID = {
        ("America/New_York", 60): set(),
        ("America/New_York", 120): {(date(2026, 3, 8), time(3, 0))},
        ("America/New_York", 240): set(),
        ("America/New_York", 720): set(),
        ("Asia/Beirut", 60): set(),
        ("Asia/Beirut", 120): {(date(2026, 3, 29), time(1, 0))},
        ("Asia/Beirut", 240): {(date(2026, 3, 29), time(1, 0))},
        ("Asia/Beirut", 720): {(date(2026, 3, 29), time(1, 0))},
    }

    def test_a_year_of_ticks(self) -> None:
        for (zone_name, minutes), off_grid in self.OFF_GRID.items():
            with self.subTest(zone=zone_name, minutes=minutes):
                zone = ZoneInfo(zone_name)
                runs = simulate(minutes, zone_name)
                in_2026 = [(t, s) for t, s in runs if s.astimezone(zone).year == 2026]
                # One run per grid time a day, minus the one missing on the
                # jump-forward day at 60 (merged); the repeated hour runs once.
                per_day = 1440 // minutes
                self.assertEqual(len(in_2026), 365 * per_day - (1 if minutes == 60 else 0))
                # Each slot on the first tick after it.
                for t, s in runs:
                    self.assertLessEqual(s, t)
                    self.assertLess(t - s, FIVE)
                # Every slot of every day ran.
                expected = {s for d in range(365) for s in slots(date(2026, 1, 1) + timedelta(days=d), minutes, zone)}
                self.assertEqual({s for _, s in in_2026}, expected)
                # No local time twice on a date.
                walls = Counter(wall(s, zone) for _, s in in_2026)
                self.assertEqual([k for k, n in walls.items() if n > 1], [])
                # Off the grid: exactly §5.3's shifted run, nothing else.
                off = {k for k in walls if (k[1].hour * 60 + k[1].minute) % minutes}
                self.assertEqual(off, off_grid)


class RealTickDstDayTests(TickFixture):
    def test_ny_spring_day_every_120_through_real_ticks(self) -> None:
        self.set_schedule(frequency="interval", interval_minutes=120, timezone="America/New_York", last_scheduled_slot=utc(2026, 3, 8, 3))
        started = []
        now = utc(2026, 3, 8, 4)
        with mock.patch("app.services.airtable_sync.runner._utc_now", side_effect=lambda: now):
            while now < utc(2026, 3, 9, 4):
                result = self.tick(now)
                if result["result"] == "started":
                    started.append(datetime.fromisoformat(result["slot"]))
                now += FIVE
        self.assertEqual(started, slots(date(2026, 3, 8), 120, NY))
        self.assertEqual([wall(s, NY)[1].hour for s in started], [0, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22])
        self.assertEqual([r.status for r in self.runs()], ["succeeded"] * 12)


# ─────────────────────────────────────────────────────────────────────────────
# decide: the guard is gone; invalid rows; outages; §5.4's switches
# ─────────────────────────────────────────────────────────────────────────────


class DecideTests(unittest.TestCase):
    def test_an_interval_row_is_due_on_its_latest_slot(self) -> None:
        # NOW = 12:00 UTC = 08:00 New York: on the 60 grid.
        self.assertEqual(scheduler.decide(interval_row(60), NOW), scheduler.Decision(True, None, NOW))
        self.assertEqual(scheduler.decide(interval_row(360), NOW), scheduler.Decision(True, None, utc(2026, 10, 24, 10)))

    def test_no_or_an_unlisted_interval_is_an_invalid_schedule(self) -> None:
        # The DB allows 15 / 30 (looser on purpose); the scheduler takes the API list only.
        for minutes in (None, 30, 15, 90, 1440):
            with self.subTest(minutes=minutes):
                self.assertEqual(scheduler.decide(interval_row(minutes), NOW), scheduler.Decision(False, "invalid_schedule"))

    def test_a_disabled_interval_row_is_disabled(self) -> None:
        self.assertEqual(scheduler.decide(interval_row(60, schedule_enabled=False), NOW).reason, "schedule_disabled")

    def test_daily_and_weekly_rows_need_no_interval_field(self) -> None:
        # phase 9's schedule() rows have no `interval_minutes` at all.
        row = schedule()
        self.assertFalse(hasattr(row, "interval_minutes"))
        self.assertEqual(scheduler.decide(row, NOW), scheduler.Decision(True, None, SLOT))

    def test_a_daily_row_with_minutes_stays_daily(self) -> None:
        for frequency, weekday in (("daily", None), ("weekly", NOW.weekday())):
            with self.subTest(frequency=frequency):
                row = schedule(frequency=frequency, run_weekday=weekday, interval_minutes=60)
                self.assertEqual(scheduler.decide(row, NOW), scheduler.Decision(True, None, SLOT))

    def test_the_reasons_are_unchanged(self) -> None:
        row = interval_row(60, last_scheduled_slot=NOW)
        self.assertEqual(scheduler.decide(row, NOW + timedelta(minutes=30)), scheduler.Decision(False, "slot_already_used", NOW))
        self.assertEqual(scheduler.decide(interval_row(60, timezone="Nowhere/Land"), NOW), scheduler.Decision(False, "invalid_schedule"))

    def test_480_and_720_after_a_7h_outage(self) -> None:
        # UTC grid: 480 → 00, 08, 16; 720 → 00, 12. The cron is down from
        # just after a slot until 7 h later.
        for minutes, slot, following in ((480, utc(2026, 6, 15, 8), utc(2026, 6, 15, 16)), (720, utc(2026, 6, 15, 12), utc(2026, 6, 16, 0))):
            with self.subTest(minutes=minutes):
                row = interval_row(minutes, timezone="UTC", last_scheduled_slot=slot - timedelta(minutes=minutes))
                back = slot + timedelta(hours=7)
                self.assertEqual(scheduler.decide(row, back), scheduler.Decision(False, "slot_too_old", slot))
                self.assertEqual(scheduler.decide(row, following - FIVE).reason, "slot_too_old")
                self.assertEqual(scheduler.decide(row, following), scheduler.Decision(True, None, following))

    def test_360_and_shorter_never_go_too_old(self) -> None:
        # A newer slot always exists before 6 h pass.
        for minutes in (60, 120, 180, 240, 360):
            with self.subTest(minutes=minutes):
                row = interval_row(minutes, timezone="UTC", last_scheduled_slot=utc(2026, 6, 15, 0))
                self.assertTrue(scheduler.decide(row, utc(2026, 6, 15, 7)).due)


class OutageTickTests(TickFixture):
    def test_720_after_a_7h_outage_then_the_next_slot_runs(self) -> None:
        self.set_schedule(frequency="interval", interval_minutes=720, timezone="UTC", last_scheduled_slot=utc(2026, 10, 24, 0))
        self.assertEqual(
            self.tick(utc(2026, 10, 24, 19)),
            {"result": "not_due", "reason": "slot_too_old", "slot": utc(2026, 10, 24, 12).isoformat()},
        )
        self.assertEqual(self.runs(), [])
        result = self.tick(utc(2026, 10, 25, 0, 2))
        self.assertEqual((result["result"], result["slot"]), ("started", utc(2026, 10, 25).isoformat()))
        self.assertEqual(self.last_slot(), utc(2026, 10, 25))


class SwitchTests(TickFixture):
    """§5.4: `last_scheduled_slot` carries over. NOW = 12:00 UTC = 08:00 NY;
    the daily slot (06:00 NY) is SLOT = 10:00 UTC."""

    def test_daily_to_interval_runs_the_latest_interval_slot_next_tick(self) -> None:
        self.assertEqual(self.tick(SLOT + FIVE)["result"], "started")  # today's daily run
        self.set_schedule(frequency="interval", interval_minutes=60)
        result = self.tick(NOW + FIVE)
        self.assertEqual((result["result"], result["slot"]), ("started", NOW.isoformat()))
        self.assertEqual(self.last_slot(), NOW)

    def test_interval_to_daily_can_make_today_count_as_used(self) -> None:
        self.set_schedule(frequency="interval", interval_minutes=60, last_scheduled_slot=NOW)
        self.set_schedule(frequency="daily", interval_minutes=None)
        # Today's 06:00 is before the last interval slot: used. No retry talk
        # (the latest slot isn't the one consumed).
        self.assertEqual(
            self.tick(NOW + FIVE),
            {"result": "not_due", "reason": "slot_already_used", "slot": SLOT.isoformat()},
        )
        self.assertEqual(self.runs(), [])
        result = self.tick(SLOT + timedelta(days=1, minutes=2))
        self.assertEqual((result["result"], result["slot"]), ("started", (SLOT + timedelta(days=1)).isoformat()))

    def test_enabling_an_interval_schedule_runs_the_current_slot(self) -> None:
        self.set_schedule(schedule_enabled=False, frequency="interval", interval_minutes=60, last_scheduled_slot=SLOT - timedelta(days=3))
        self.assertEqual(self.tick(NOW + timedelta(minutes=3))["reason"], "schedule_disabled")
        self.set_schedule(schedule_enabled=True)
        result = self.tick(NOW + timedelta(minutes=4))
        self.assertEqual((result["result"], result["slot"]), ("started", NOW.isoformat()))


# ─────────────────────────────────────────────────────────────────────────────
# Settings: service, API, payloads
# ─────────────────────────────────────────────────────────────────────────────


class SettingsServiceTests(TickFixture):
    def save(self, **kw):
        args = dict(
            schedule_enabled=True,
            frequency="interval",
            run_time=time(7, 30),
            run_weekday=None,
            interval_minutes=120,
            timezone_name="Asia/Beirut",
            actor_email=ADMIN_EMAIL,
        )
        args.update(kw)
        return update_settings(self.db, **args)

    def assert_code(self, code: str, message: str | None = None, **kw) -> None:
        with self.assertRaises(AirtableSyncError) as caught:
            self.save(**kw)
        self.assertEqual(caught.exception.code, code)
        if message is not None:
            self.assertEqual(caught.exception.message, message)
        self.db.rollback()

    def test_every_listed_interval_saves(self) -> None:
        for minutes in INTERVAL_MINUTES:
            with self.subTest(minutes=minutes):
                data = self.save(interval_minutes=minutes)
                self.db.commit()
                self.assertEqual((data["frequency"], data["interval_minutes"]), ("interval", minutes))

    def test_run_time_is_stored_for_interval(self) -> None:
        self.save(run_time=time(9, 15))
        self.db.commit()
        self.db.expire_all()
        row = get_settings_row(self.db)
        self.assertEqual((row.run_time, row.interval_minutes, row.run_weekday), (time(9, 15), 120, None))

    def test_the_four_409_codes(self) -> None:
        self.assert_code("interval_required", "An interval schedule needs an interval.", interval_minutes=None)
        self.assert_code("invalid_interval", "The interval must be one of 60, 120, 180, 240, 360, 480, 720 minutes.", interval_minutes=90)
        self.assert_code("invalid_interval", interval_minutes=30)
        self.assert_code("interval_not_allowed", "A daily schedule has no interval.", frequency="daily")
        self.assert_code("interval_not_allowed", "A weekly schedule has no interval.", frequency="weekly", run_weekday=2)
        self.assert_code("weekday_not_allowed", "An interval schedule has no weekday.", run_weekday=0)

    def test_daily_and_weekly_save_with_no_interval(self) -> None:
        self.save(frequency="weekly", run_weekday=4, interval_minutes=None)
        self.save(frequency="daily", interval_minutes=None)
        self.db.commit()
        self.assertEqual(get_settings_row(self.db).interval_minutes, None)

    def test_switching_away_clears_the_interval(self) -> None:
        self.save()
        self.save(frequency="daily", interval_minutes=None)
        self.db.commit()  # the pair CHECK holds
        self.assertIsNone(get_settings_row(self.db).interval_minutes)

    def test_an_unknown_frequency_lists_all_three(self) -> None:
        self.assert_code("invalid_frequency", "Frequency must be one of daily, weekly, interval.", frequency="hourly")

    def test_existing_callers_need_no_interval_argument(self) -> None:
        update_settings(
            self.db,
            schedule_enabled=True,
            frequency="daily",
            run_time=time(6, 0),
            run_weekday=None,
            timezone_name="America/New_York",
            actor_email=ADMIN_EMAIL,
        )


class NextSlotPayloadTests(TickFixture):
    def serialize(self, now: datetime = NOW) -> dict:
        return serialize_settings(get_settings_row(self.db), now)

    def test_next_slot_for_every_frequency(self) -> None:
        self.assertEqual(self.serialize()["next_slot"], SLOT + timedelta(days=1))  # daily 06:00 NY
        self.set_schedule(frequency="weekly", run_weekday=NOW.weekday())
        self.assertEqual(self.serialize()["next_slot"], SLOT + timedelta(days=7))
        self.set_schedule(frequency="interval", run_weekday=None, interval_minutes=240)
        payload = self.serialize()
        self.assertEqual((payload["interval_minutes"], payload["next_slot"]), (240, utc(2026, 10, 24, 16)))

    def test_null_when_off_or_invalid(self) -> None:
        self.set_schedule(schedule_enabled=False)
        self.assertIsNone(self.serialize()["next_slot"])
        self.set_schedule(schedule_enabled=True, frequency="interval", interval_minutes=30)  # the DB allows it
        self.assertIsNone(self.serialize()["next_slot"])
        self.set_schedule(frequency="daily", interval_minutes=None, timezone="Nowhere/Land")
        self.assertIsNone(self.serialize()["next_slot"])

    def test_interval_minutes_is_null_for_daily(self) -> None:
        self.assertIsNone(self.serialize()["interval_minutes"])

    def test_the_default_clock(self) -> None:
        with mock.patch.object(settings_service, "_utc_now", return_value=NOW):
            self.assertEqual(serialize_settings(get_settings_row(self.db))["next_slot"], SLOT + timedelta(days=1))


class SettingsApiTests(TickFixture):
    def put(self, **body):
        payload = {"schedule_enabled": True, "frequency": "interval", "run_time": "07:30", "timezone": "America/New_York", "interval_minutes": 60}
        payload.update(body)
        payload = {k: v for k, v in payload.items() if v is not Ellipsis}
        with mock.patch.object(settings_service, "_utc_now", return_value=NOW):
            return self.client.put("/v2/airtable-sync/settings", json=payload, headers=self.auth(ADMIN_EMAIL))

    def get(self, path: str = "/v2/airtable-sync/settings"):
        with mock.patch.object(settings_service, "_utc_now", return_value=NOW), mock.patch.object(runs_service, "_utc_now", return_value=NOW):
            return self.client.get(path, headers=self.auth(ADMIN_EMAIL))

    def assert_409(self, code: str, **body) -> None:
        res = self.put(**body)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], code)
        self.db.expire_all()
        self.assertEqual(get_settings_row(self.db).frequency, "daily")

    def test_put_interval_saves_and_answers_with_next_slot(self) -> None:
        res = self.put()
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual((data["frequency"], data["interval_minutes"], data["run_time"]), ("interval", 60, "07:30"))
        self.assertEqual(datetime.fromisoformat(data["next_slot"]), NOW + timedelta(hours=1))
        self.db.expire_all()
        self.assertEqual(get_settings_row(self.db).interval_minutes, 60)

    def test_get_and_status_carry_interval_and_next_slot(self) -> None:
        self.assertEqual(self.put(interval_minutes=180).status_code, 200)
        for path, pick in (("/v2/airtable-sync/settings", lambda j: j["data"]), ("/v2/airtable-sync/status", lambda j: j["data"]["settings"])):
            with self.subTest(path=path):
                res = self.get(path)
                self.assertEqual(res.status_code, 200, res.text)
                data = pick(res.json())
                self.assertEqual(data["interval_minutes"], 180)
                self.assertEqual(datetime.fromisoformat(data["next_slot"]), utc(2026, 10, 24, 13))  # NY 09:00

    def test_a_daily_get_has_null_interval_and_its_next_slot(self) -> None:
        data = self.get().json()["data"]
        self.assertIsNone(data["interval_minutes"])
        self.assertEqual(datetime.fromisoformat(data["next_slot"]), SLOT + timedelta(days=1))

    def test_get_loads_a_hand_edited_interval_row(self) -> None:
        # The response Literal widened too, or this would 500 (RI-1 handoff).
        self.set_schedule(frequency="interval", interval_minutes=30)
        res = self.get()
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual((res.json()["data"]["interval_minutes"], res.json()["data"]["next_slot"]), (30, None))

    def test_422_outside_the_offered_range(self) -> None:
        for minutes in (30, 45, 0, 1441, -60, "60", True, 60.0):
            with self.subTest(minutes=minutes):
                res = self.put(interval_minutes=minutes)
                self.assertEqual(res.status_code, 422, res.text)
                self.assertIn(["body", "interval_minutes"], [e["loc"] for e in res.json()["detail"]])

    def test_the_409_codes(self) -> None:
        self.assert_409("interval_required", interval_minutes=None)
        self.assert_409("interval_required", interval_minutes=...)
        self.assert_409("invalid_interval", interval_minutes=90)
        self.assert_409("interval_not_allowed", frequency="daily")
        self.assert_409("interval_not_allowed", frequency="weekly", run_weekday=1)

    def test_a_weekday_on_interval_is_refused(self) -> None:
        res = self.put(run_weekday=3)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "weekday_not_allowed")
        self.assertEqual(res.json()["detail"]["message"], "An interval schedule has no weekday.")

    def test_daily_and_weekly_still_save(self) -> None:
        self.assertEqual(self.put(frequency="daily", interval_minutes=None).status_code, 200)
        self.assertEqual(self.put(frequency="weekly", run_weekday=4, interval_minutes=...).status_code, 200)

    def test_the_schemas(self) -> None:
        for model in (UpdateSyncSettingsRequest, SyncSettingsResponse):
            with self.subTest(model=model.__name__):
                self.assertEqual(model.model_fields["frequency"].annotation.__args__, ("daily", "weekly", "interval"))
        self.assertIn("next_slot", SyncSettingsResponse.model_fields)


# ─────────────────────────────────────────────────────────────────────────────
# Retry room under interval (RI-2's engine, RI-3's notes, no stubs)
# ─────────────────────────────────────────────────────────────────────────────


class IntervalRetryTests(Ri3Fixture):
    """A 60-min New York schedule; the slot is NOW = 12:00 UTC (08:00 EDT),
    the next one 13:00. Each attempt "ends" at a chosen minute: the failing
    stub step moves the runner's clock there before it raises."""

    def setUp(self) -> None:
        super().setUp()
        self.set_schedule(frequency="interval", interval_minutes=60)
        self.clock = NOW
        self.ends: list[datetime] = []
        clock = mock.patch("app.services.airtable_sync.runner._utc_now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        users = self.step_functions["users"]

        def users_then_fail(ctx):
            if self.ends:
                self.clock = self.ends.pop(0)
            return users(ctx)

        self.step_functions["users"] = users_then_fail

    def run_tick(self, minute: int, ends_at: int | None = None) -> dict:
        self.clock = NOW + timedelta(minutes=minute)
        if ends_at is not None:
            self.ends.append(NOW + timedelta(minutes=ends_at))
        return self.tick(self.clock)

    def test_attempt_two_then_no_room(self) -> None:
        self.fail_step = "users"
        first = self.run_tick(0, ends_at=18)
        self.assertEqual((first["result"], first["slot"]), ("started", NOW.isoformat()))
        self.assertEqual(
            self.finished(), [f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {CRASH}"]
        )  # at :18 the next slot is 42 min away

        second = self.run_tick(20, ends_at=45)
        self.assertEqual((second["result"], second["slot"], second["attempt"]), ("started", NOW.isoformat(), 2))
        # At :45 the next slot (13:00) is 15 min away: no room, so no retry.
        self.assertEqual(self.finished()[-1], f"Scheduled Airtable sync failed after 2 attempts: {CRASH}")

        self.assertEqual(
            self.run_tick(50),
            {"result": "not_due", "reason": "slot_already_used", "slot": NOW.isoformat(), "retry_reason": "next_slot_soon"},
        )
        third = self.run_tick(60)
        self.assertEqual((third["result"], third["slot"]), ("started", (NOW + timedelta(hours=1)).isoformat()))
        self.assertNotIn("attempt", third)  # the new slot's first try
        runs = self.runs()
        self.assertEqual([(r.status, r.finished_at.replace(tzinfo=UTC) if r.finished_at.tzinfo is None else r.finished_at) for r in runs[:2]],
                         [("failed", NOW + timedelta(minutes=18)), ("failed", NOW + timedelta(minutes=45))])
        self.assertEqual(self.started(), ["Scheduled Airtable sync started"] * 2)  # slot 1's attempt 1, slot 2's

    def test_the_room_check_uses_the_interval_grid(self) -> None:
        # Attempt 1 ends at :41 → 19 min to 13:00: no retry, today's text.
        self.fail_step = "users"
        self.run_tick(0, ends_at=41)
        self.assertEqual(self.finished(), [f"Airtable sync failed: {CRASH}"])
        self.assertEqual(self.run_tick(45)["retry_reason"], "next_slot_soon")
        self.assertEqual(len(self.runs()), 1)

    def test_with_room_attempt_two_starts(self) -> None:
        self.fail_step = "users"
        self.run_tick(0, ends_at=39)  # 21 min to 13:00
        self.assertEqual(self.run_tick(39)["attempt"], 2)


if __name__ == "__main__":
    unittest.main()
