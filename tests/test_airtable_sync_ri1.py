"""Airtable sync, RI-1 (AI Docs/plan_airtable_sync_retries_interval_2026-10-29.md
§9 RI-1, §5.1, §10.1): the retry + interval schema, with no behaviour change.

  - CHECKs: `interval_minutes` NULL, or ≥ 15 and a divisor of 24 h (looser
    than the API's list on purpose); set exactly when frequency = 'interval';
    a run's `scheduled_slot` only on a scheduled run; the partial index
  - the guards until RI-4, now flipped by RI-4 (`decide` schedules
    'interval'; PUT /settings answers 200 / 409 by value; `update_settings`
    accepts it, and `invalid_frequency` lists all three)
  - nothing writes the new columns yet (RI-2 wires `scheduled_slot`)
  - the migration script takes its CHECK texts from the model

In-memory SQLite, reusing phase 7's fixture and phase 9's tick fixture.
"""

from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta, timezone

# First: phase 7's module sets the test environment before importing the app.
from tests.test_airtable_sync_phase7 import ADMIN_EMAIL, ALL_STEPS, Phase7Fixture  # isort: skip
from tests.test_airtable_sync_phase9 import NOW, SLOT, TickFixture, schedule  # noqa: E402

from sqlalchemy import CheckConstraint, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.db_v2.models.airtable_sync import (  # noqa: E402
    FREQUENCIES,
    INTERVAL_MINUTES,
    AirtableSyncRunV2,
    AirtableSyncSettingsV2,
)
from app.schemas.airtable_sync import SyncSettingsResponse, UpdateSyncSettingsRequest  # noqa: E402
from app.services.airtable_sync import scheduler  # noqa: E402
from app.services.airtable_sync.settings_service import (  # noqa: E402
    AirtableSyncError,
    get_settings_row,
    update_settings,
)
from scripts import migrate_airtable_sync_retries_interval as migration  # noqa: E402


class ConstantTests(unittest.TestCase):
    def test_interval_is_a_frequency(self) -> None:
        self.assertEqual(FREQUENCIES, ("daily", "weekly", "interval"))

    def test_interval_fits_the_frequency_column(self) -> None:
        self.assertEqual(len("interval"), AirtableSyncSettingsV2.__table__.c.frequency.type.length)

    def test_the_api_list(self) -> None:
        self.assertEqual(INTERVAL_MINUTES, (60, 120, 180, 240, 360, 480, 720))
        for minutes in INTERVAL_MINUTES:
            self.assertEqual(1440 % minutes, 0, minutes)


# ─────────────────────────────────────────────────────────────────────────────
# CHECKs (SQLite)
# ─────────────────────────────────────────────────────────────────────────────


class SettingsCheckTests(Phase7Fixture):
    def save(self, **kw) -> None:
        row = get_settings_row(self.db)
        for key, value in kw.items():
            setattr(row, key, value)
        self.db.commit()

    def assert_refused(self, **kw) -> None:
        with self.assertRaises(IntegrityError):
            self.save(**kw)
        self.db.rollback()

    def test_a_new_row_is_daily_with_no_interval(self) -> None:
        row = get_settings_row(self.db)
        self.db.commit()
        self.assertEqual((row.frequency, row.interval_minutes), ("daily", None))

    def test_interval_with_60_is_accepted(self) -> None:
        self.save(frequency="interval", interval_minutes=60)
        self.db.expire_all()
        self.assertEqual(get_settings_row(self.db).interval_minutes, 60)

    def test_every_api_value_is_accepted(self) -> None:
        for minutes in INTERVAL_MINUTES:
            with self.subTest(minutes=minutes):
                self.save(frequency="interval", interval_minutes=minutes)

    def test_the_db_is_looser_than_the_api(self) -> None:
        # A later "every 15 / 30 min" needs no migration (plan §5.1).
        for minutes in (15, 30, 1440):
            with self.subTest(minutes=minutes):
                self.save(frequency="interval", interval_minutes=minutes)

    def test_interval_with_a_bad_value_is_refused(self) -> None:
        # 7: not a divisor of 1440; 10: a divisor, but under 15; 0: no
        # interval at all (SQLite's 1440 % 0 is NULL, so only `>= 15` stops
        # it); 2880: more than a day.
        for minutes in (7, 10, 0, -60, 2880):
            with self.subTest(minutes=minutes):
                self.assert_refused(frequency="interval", interval_minutes=minutes)

    def test_interval_without_minutes_is_refused(self) -> None:
        self.assert_refused(frequency="interval", interval_minutes=None)

    def test_daily_or_weekly_with_minutes_is_refused(self) -> None:
        self.assert_refused(frequency="daily", interval_minutes=60)
        self.assert_refused(frequency="weekly", run_weekday=0, interval_minutes=60)

    def test_daily_and_weekly_without_minutes_are_accepted(self) -> None:
        self.save(frequency="weekly", run_weekday=2, interval_minutes=None)
        self.save(frequency="daily", run_weekday=None, interval_minutes=None)

    def test_an_unknown_frequency_is_still_refused(self) -> None:
        self.assert_refused(frequency="hourly")


class RunCheckTests(Phase7Fixture):
    def insert(self, trigger: str, slot) -> int:
        run = AirtableSyncRunV2(steps=list(ALL_STEPS), trigger=trigger, status="succeeded", scheduled_slot=slot)
        self.db.add(run)
        self.db.commit()
        return run.id

    def test_a_scheduled_run_with_a_slot_is_accepted(self) -> None:
        run_id = self.insert("scheduled", SLOT)
        self.db.expire_all()
        self.assertEqual(self.db.get(AirtableSyncRunV2, run_id).scheduled_slot.replace(tzinfo=None), SLOT.replace(tzinfo=None))

    def test_a_scheduled_run_without_a_slot_is_accepted(self) -> None:
        # Every scheduled run from before RI-2 (no backfill: Dev runs 10, 11).
        self.insert("scheduled", None)

    def test_manual_runs_and_previews_with_a_slot_are_refused(self) -> None:
        for trigger in ("manual", "preview"):
            with self.subTest(trigger=trigger):
                with self.assertRaises(IntegrityError):
                    self.insert(trigger, SLOT)
                self.db.rollback()

    def test_manual_runs_and_previews_without_a_slot_are_accepted(self) -> None:
        self.insert("manual", None)
        self.insert("preview", None)

    def test_the_slot_index_is_partial(self) -> None:
        sql = self.db.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ix_airtable_sync_runs_scheduled_slot'")
        ).scalar()
        self.assertIsNotNone(sql)
        self.assertNotIn("UNIQUE", sql.upper())
        self.assertIn("WHERE scheduled_slot IS NOT NULL", sql)

    def test_many_runs_may_share_a_slot(self) -> None:
        # Up to 3 attempts per slot (RI-2): the index must not be unique.
        for _ in range(3):
            self.insert("scheduled", SLOT)


# ─────────────────────────────────────────────────────────────────────────────
# Guards until RI-4
# ─────────────────────────────────────────────────────────────────────────────


class DecideGuardTests(unittest.TestCase):
    def test_interval_has_its_own_slots(self) -> None:
        # Flipped by RI-4 (was test_interval_is_an_invalid_schedule_until_ri4):
        # NOW = 08:00 New York, on the 60-min grid.
        row = schedule(frequency="interval", interval_minutes=60)
        self.assertEqual(scheduler.decide(row, NOW), scheduler.Decision(True, None, NOW))

    def test_a_disabled_interval_schedule_is_just_disabled(self) -> None:
        row = schedule(schedule_enabled=False, frequency="interval", interval_minutes=60)
        self.assertEqual(scheduler.decide(row, NOW).reason, "schedule_disabled")

    def test_daily_is_unchanged(self) -> None:
        self.assertEqual(scheduler.decide(schedule(), NOW), scheduler.Decision(True, None, SLOT))


class TickGuardTests(TickFixture):
    def test_an_interval_row_starts_a_run_and_consumes(self) -> None:
        # Flipped by RI-4 (was test_an_interval_row_never_starts_a_run_or_consumes).
        self.set_schedule(frequency="interval", interval_minutes=60)
        result = self.tick(NOW)
        self.assertEqual(result, {"result": "started", "run_id": self.runs()[-1].id, "slot": NOW.isoformat()})
        self.assertEqual(self.last_slot(), NOW)
        self.assertEqual(self.tick(NOW + timedelta(minutes=5))["reason"], "slot_already_used")
        self.assertEqual(self.tick(NOW + timedelta(minutes=60))["result"], "started")
        self.assertEqual(len(self.runs()), 2)


class SettingsGuardTests(Phase7Fixture):
    def put(self, **body):
        payload = {"schedule_enabled": True, "frequency": "daily", "run_time": "07:30", "timezone": "Asia/Beirut"}
        payload.update(body)
        return self.client.put("/v2/airtable-sync/settings", json=payload, headers=self.auth(ADMIN_EMAIL))

    def test_put_interval_is_200_or_409_by_value(self) -> None:
        # Flipped by RI-4 (was test_put_interval_is_422_on_frequency).
        res = self.put(frequency="interval")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "interval_required")
        self.assertEqual(get_settings_row(self.db).frequency, "daily")
        res = self.put(frequency="interval", interval_minutes=60)
        self.assertEqual(res.status_code, 200, res.text)
        self.db.expire_all()
        self.assertEqual((get_settings_row(self.db).frequency, get_settings_row(self.db).interval_minutes), ("interval", 60))

    def test_put_interval_minutes_on_daily_is_409(self) -> None:
        # Flipped by RI-4 (was test_put_interval_minutes_is_an_unknown_field_until_ri4).
        res = self.put(interval_minutes=60)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "interval_not_allowed")

    def test_the_schemas_take_interval(self) -> None:
        # Flipped by RI-4 (was test_the_schemas_keep_daily_and_weekly).
        for model in (UpdateSyncSettingsRequest, SyncSettingsResponse):
            with self.subTest(model=model.__name__):
                self.assertEqual(model.model_fields["frequency"].annotation.__args__, ("daily", "weekly", "interval"))

    def test_update_settings_accepts_interval(self) -> None:
        # Flipped by RI-4 (was test_update_settings_refuses_interval); an
        # unknown frequency is still `invalid_frequency`, now listing all three.
        data = update_settings(
            self.db,
            schedule_enabled=True,
            frequency="interval",
            run_time=time(7, 30),
            run_weekday=None,
            interval_minutes=60,
            timezone_name="Asia/Beirut",
            actor_email=ADMIN_EMAIL,
        )
        self.assertEqual((data["frequency"], data["interval_minutes"]), ("interval", 60))
        with self.assertRaises(AirtableSyncError) as caught:
            update_settings(
                self.db,
                schedule_enabled=True,
                frequency="hourly",
                run_time=time(7, 30),
                run_weekday=None,
                timezone_name="Asia/Beirut",
                actor_email=ADMIN_EMAIL,
            )
        self.assertEqual(caught.exception.code, "invalid_frequency")
        self.assertEqual(caught.exception.message, "Frequency must be one of daily, weekly, interval.")
        self.db.rollback()
        self.assertEqual(get_settings_row(self.db).frequency, "daily")

    def test_daily_and_weekly_still_save_with_no_interval(self) -> None:
        self.assertEqual(self.put().status_code, 200)
        self.assertEqual(self.put(frequency="weekly", run_weekday=4).status_code, 200)
        self.db.expire_all()
        row = get_settings_row(self.db)
        self.assertEqual((row.frequency, row.run_weekday, row.interval_minutes), ("weekly", 4, None))

    def test_get_still_loads(self) -> None:
        res = self.client.get("/v2/airtable-sync/settings", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 200, res.text)
        # Flipped by RI-4 (was assertNotIn): the field is there, null for daily.
        self.assertIsNone(res.json()["data"]["interval_minutes"])


# ─────────────────────────────────────────────────────────────────────────────
# Nothing writes the new columns yet
# ─────────────────────────────────────────────────────────────────────────────


class NoWriterYetTests(TickFixture):
    def test_a_scheduled_run_carries_its_slot(self) -> None:
        # Flipped by RI-2 (was test_a_scheduled_run_has_no_slot_until_ri2):
        # `runner.start(scheduled_slot=…)`.
        result = self.tick()
        self.assertEqual(result["result"], "started")
        slot = self.runs()[-1].scheduled_slot
        self.assertEqual(slot.replace(tzinfo=timezone.utc) if slot.tzinfo is None else slot, datetime.fromisoformat(result["slot"]))

    def test_manual_runs_and_previews_have_no_slot(self) -> None:
        for preview in (False, True):
            res = self.start(preview=preview)
            self.assertIn(res.status_code, (200, 202), res.text)
        self.assertEqual([r.scheduled_slot for r in self.runs()], [None, None])

    def test_runs_list_still_loads(self) -> None:
        self.start()
        res = self.client.get("/v2/airtable-sync/runs", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 200, res.text)


# ─────────────────────────────────────────────────────────────────────────────
# The migration script matches the model
# ─────────────────────────────────────────────────────────────────────────────


class MigrationScriptTests(unittest.TestCase):
    def test_every_check_it_adds_comes_from_the_model(self) -> None:
        for table, name in [*migration.NEW_CHECKS, (migration.SETTINGS, migration.FREQUENCY_CHECK)]:
            with self.subTest(name=name):
                self.assertTrue(migration._model_check(table, name))
                self.assertIn(name, migration.CHECK_MARKERS)

    def test_the_model_has_no_check_the_script_misses(self) -> None:
        names = {
            c.name
            for t in (AirtableSyncRunV2.__table__, AirtableSyncSettingsV2.__table__)
            for c in t.constraints
            if isinstance(c, CheckConstraint) and ("scheduled_slot" in str(c.sqltext) or "interval" in str(c.sqltext))
        }
        self.assertEqual(names, set(migration.CHECK_MARKERS))

    def test_the_index_ddl_matches_the_model(self) -> None:
        (index,) = [i for i in AirtableSyncRunV2.__table__.indexes if i.name == migration.INDEX_NAME]
        self.assertFalse(index.unique)
        self.assertEqual([c.name for c in index.columns], ["scheduled_slot"])
        where = str(index.dialect_options["postgresql"]["where"])
        self.assertEqual(str(index.dialect_options["sqlite"]["where"]), where)
        self.assertTrue(migration.INDEX_DDL.endswith(f"WHERE {where}"))

    def test_the_column_ddl_matches_the_model(self) -> None:
        self.assertTrue(AirtableSyncRunV2.__table__.c.scheduled_slot.nullable)
        self.assertTrue(AirtableSyncRunV2.__table__.c.scheduled_slot.type.timezone)
        self.assertTrue(AirtableSyncSettingsV2.__table__.c.interval_minutes.nullable)


if __name__ == "__main__":
    unittest.main()
