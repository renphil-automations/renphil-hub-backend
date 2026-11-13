"""Airtable sync: each admin's "Receive run notifications" mode.

  - NULL or `all`: every note; `failures`: final failures only; `none`: nothing.
    NULL (never chose) counts as `all` in every filter and stays NULL through
    runs and GETs, and on non-admins
  - a final failure is a real run that did not succeed with no retry planned;
    the excerpt and the recipients come from ONE `retry_outlook` result
  - the tick's reap: a dead manual run notifies as a final failure, once; a dead
    preview doesn't
  - the mode is read when each note is sent
  - the endpoints only touch the caller's own row, are admin-only, and refuse
    anything but the three values
  - the column: nullable string, no default, a CHECK

Uses phase 7's fixture (real admins, the real SyncRunner, stub steps) and RI-3's
for the retry sequences (real ticks, the clock frozen at NOW).
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from sqlalchemy import String, text
from sqlalchemy.exc import IntegrityError

# First: phase 7's module sets the test environment.
from tests.test_airtable_sync_phase7 import (  # isort: skip
    ADMIN_EMAIL,
    ALIAS_ADMIN_ALIAS,
    DELETED_ADMIN_EMAIL,
    FINISHED,
    MEMBER_EMAIL,
    OTHER_ADMIN_EMAIL,
    STARTED,
    Phase7Fixture,
)

from app.db_v2.models.airtable_sync import AirtableSyncRunV2  # noqa: E402
from app.db_v2.models.user import (  # noqa: E402
    AIRTABLE_SYNC_NOTIFICATION_DEFAULT_MODE,
    AIRTABLE_SYNC_NOTIFICATION_MODES,
    AIRTABLE_SYNC_NOTIFICATIONS_MODE_CHECK_NAME,
    UserV2,
)
from app.services.airtable_sync import notifications as sync_notifications  # noqa: E402
from app.services.airtable_sync import scheduler, settings_service  # noqa: E402
from app.services.airtable_sync.notifications import SyncNotifier  # noqa: E402
from app.services.airtable_sync.runner import INTERRUPTED_BY_TICK  # noqa: E402
from tests.test_airtable_sync_phase9 import NOW, SLOT  # noqa: E402
from tests.test_airtable_sync_ri2 import T5, T10  # noqa: E402
from tests.test_airtable_sync_ri3 import CRASH, T6, Ri3Fixture  # noqa: E402

URL = "/v2/airtable-sync/notification-preference"
ADMINS = sorted([ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS])
ALL_NOTE = "Airtable sync finished: 0 updated, 0 created, 0 conflicts, 0 warnings"
MANUAL_CRASH = f"Airtable sync failed: {CRASH}"
RETRYING_1 = f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {CRASH}"
RETRYING_2 = f"Scheduled Airtable sync failed (attempt 2 of 3), retrying: {CRASH}"
EXHAUSTED = f"Scheduled Airtable sync failed after 3 attempts: {CRASH}"
REAPED = f"Airtable sync failed: {INTERRUPTED_BY_TICK}"


class ModeHelpers:
    def set_mode(self, key: str, mode: str | None) -> None:
        self.db.get(UserV2, self.users[key]).airtable_sync_notifications_mode = mode
        self.db.commit()

    def stored(self, key: str) -> str | None:
        self.db.expire_all()
        return self.db.get(UserV2, self.users[key]).airtable_sync_notifications_mode

    def excerpts_for(self, email: str, type_: str | None = None) -> list[str]:
        return [n.excerpt for n in self.notes(type_) if n.recipient_email == email]


class PreferenceFixture(ModeHelpers, Phase7Fixture):
    pass


class RetryFixture(ModeHelpers, Ri3Fixture):
    """ADMIN never chose, ALIAS is on `all`, OTHER is on `failures`, MEMBER_EMAIL
    no admin. `none` is tried per test."""

    def setUp(self) -> None:
        super().setUp()
        self.set_mode(ALIAS_ADMIN_ALIAS, "all")
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")


# ---------------------------------------------------------
# Recipients
# ---------------------------------------------------------


class TestRecipients(PreferenceFixture):
    def every_note(self, **kw) -> list[str]:
        return sync_notifications.admin_recipients(self.db, **kw)

    def final_failure(self, **kw) -> list[str]:
        return sync_notifications.admin_recipients(self.db, final_failure=True, **kw)

    def test_null_counts_as_all_in_both_filters(self) -> None:
        for key in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, MEMBER_EMAIL):
            self.assertIsNone(self.stored(key))
        self.assertEqual(self.every_note(), ADMINS)
        self.assertEqual(self.final_failure(), ADMINS)

    def test_all_gets_both(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "all")
        self.assertIn(OTHER_ADMIN_EMAIL, self.every_note())
        self.assertIn(OTHER_ADMIN_EMAIL, self.final_failure())

    def test_failures_gets_only_the_final_failure(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")
        self.assertNotIn(OTHER_ADMIN_EMAIL, self.every_note())
        self.assertIn(OTHER_ADMIN_EMAIL, self.final_failure())

    def test_none_gets_neither(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.assertNotIn(OTHER_ADMIN_EMAIL, self.every_note())
        self.assertNotIn(OTHER_ADMIN_EMAIL, self.final_failure())
        # …and nobody else lost anything.
        self.assertEqual(self.final_failure(), sorted([ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))

    def test_a_mixed_set(self) -> None:
        self.set_mode(ALIAS_ADMIN_ALIAS, "failures")
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.assertEqual(self.every_note(), [ADMIN_EMAIL])
        self.assertEqual(self.final_failure(), sorted([ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))

    def test_a_member_with_a_value_is_still_never_a_recipient(self) -> None:
        for mode in ("all", "failures"):
            self.set_mode(MEMBER_EMAIL, mode)
            self.assertNotIn(MEMBER_EMAIL, self.every_note())
            self.assertNotIn(MEMBER_EMAIL, self.final_failure())

    def test_a_deleted_admin_on_all_is_never_a_recipient(self) -> None:
        self.set_mode(DELETED_ADMIN_EMAIL, "all")
        self.assertNotIn(DELETED_ADMIN_EMAIL, self.every_note())
        self.assertNotIn(DELETED_ADMIN_EMAIL, self.final_failure())

    def test_exclusion_still_applies_with_the_filter(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.assertEqual(self.every_note(exclude_user_id=self.users[ADMIN_EMAIL]), [ALIAS_ADMIN_ALIAS])

    def test_everyone_off_means_nobody(self) -> None:
        for key in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS):
            self.set_mode(key, "none")
        self.assertEqual(self.every_note(), [])
        self.assertEqual(self.final_failure(), [])


# ---------------------------------------------------------
# Manual runs
# ---------------------------------------------------------


class TestManualRuns(PreferenceFixture):
    def test_a_run_notifies_admins_who_never_chose_and_writes_no_value(self) -> None:
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.recipients(FINISHED), ADMINS)
        for key in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, MEMBER_EMAIL, DELETED_ADMIN_EMAIL):
            self.assertIsNone(self.stored(key))

    def test_a_failed_run_writes_no_value_either(self) -> None:
        self.fail_step = "users"
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.recipients(FINISHED), ADMINS)
        for key in (ADMIN_EMAIL, OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, MEMBER_EMAIL):
            self.assertIsNone(self.stored(key))

    def test_null_and_all_get_every_note_of_a_success(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "all")
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.recipients(STARTED), sorted([OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))
        self.assertEqual(self.recipients(FINISHED), ADMINS)

    def test_null_and_all_get_every_note_of_a_failure(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "all")
        self.fail_step = "users"
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.recipients(STARTED), sorted([OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))
        self.assertEqual(self.recipients(FINISHED), ADMINS)

    def test_a_starter_on_any_mode_is_left_out_of_their_start_note(self) -> None:
        for mode in (None, "all"):
            with self.subTest(mode=mode):
                self.set_mode(ADMIN_EMAIL, mode)
                self.db.query(sync_notifications.NotificationV2).delete()
                self.db.commit()
                self.assertEqual(self.start().status_code, 202)
                self.assertEqual(self.recipients(STARTED), sorted([OTHER_ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))
                self.assertIn(ADMIN_EMAIL, self.recipients(FINISHED))

    def test_failures_gets_a_real_manual_failure_but_no_start_note(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")
        self.fail_step = "users"
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [MANUAL_CRASH])
        self.assertEqual(self.recipients(STARTED), [ALIAS_ADMIN_ALIAS])
        self.assertEqual(self.recipients(FINISHED), ADMINS)

    def test_failures_gets_nothing_from_a_success(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])
        self.assertEqual(self.recipients(FINISHED), sorted([ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))

    def test_failures_gets_nothing_from_a_success_with_conflicts_and_warnings(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")
        self.step_items = [("field_conflict", "user"), ("no_work_email", "user")]
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(
            {n.excerpt for n in self.notes(FINISHED)},
            {"Airtable sync finished: 0 updated, 0 created, 1 conflicts, 1 warnings"},
        )
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])

    def test_a_starter_on_failures_gets_their_runs_failure_once(self) -> None:
        self.set_mode(ADMIN_EMAIL, "failures")
        self.fail_step = "users"
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.excerpts_for(ADMIN_EMAIL), [MANUAL_CRASH])

    def test_none_gets_nothing_from_a_failure_or_a_success(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.assertEqual(self.start().status_code, 202)
        self.fail_step = "users"
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])
        self.assertEqual(len(self.excerpts_for(ALIAS_ADMIN_ALIAS)), 4)

    def test_a_failed_preview_sends_nothing_to_anyone(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")
        self.fail_step = "users"
        self.assertEqual(self.start(preview=True).status_code, 202)
        self.assertEqual(self.notes(), [])

    def test_a_failure_with_no_error_text_still_reaches_failures(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "failures")
        run = self.add_run(status="interrupted")
        sync_notifications.run_finished(self.Session, run.id)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), ["Airtable sync failed: interrupted"])

    def test_the_mode_is_read_when_each_note_is_sent(self) -> None:
        run = self.add_run(status="running", trigger="manual", email=ADMIN_EMAIL)
        sync_notifications.run_started(self.Session, run.id)
        self.assertIn(OTHER_ADMIN_EMAIL, self.recipients(STARTED))
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        sync_notifications.run_finished(self.Session, run.id)
        self.assertNotIn(OTHER_ADMIN_EMAIL, self.recipients(FINISHED))

    def test_changing_the_mode_keeps_notes_already_sent(self) -> None:
        note = self.add_notification(ADMIN_EMAIL)
        response = self.client.put(URL, json={"mode": "none"}, headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([n.id for n in self.notes()], [note])

    def test_a_failed_read_of_the_mode_never_fails_a_run(self) -> None:
        with mock.patch.object(sync_notifications, "admin_recipients", side_effect=RuntimeError("no column")):
            self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.notes(), [])
        self.db.expire_all()
        self.assertEqual(self.run_row(1).status, "succeeded")


# ---------------------------------------------------------
# Scheduled runs: the retry sequence
# ---------------------------------------------------------


class TestRetrySequence(RetryFixture):
    def test_a_failed_attempt_with_a_retry_planned_sends_failures_nothing(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.assertEqual(self.finished(), [RETRYING_1])  # ADMIN, on NULL
        self.assertEqual(self.excerpts_for(ALIAS_ADMIN_ALIAS, FINISHED), [RETRYING_1])
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])

    def test_a_retry_that_succeeds_sends_failures_nothing(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.tick(T5)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])
        self.assertEqual(
            self.finished(),
            [RETRYING_1, "Airtable sync finished on attempt 2 of 3: 0 updated, 0 created, 0 conflicts, 0 warnings"],
        )

    def test_exhausted_attempts_send_failures_exactly_one(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.tick(T5)
        self.tick(T10)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [EXHAUSTED])

    def test_null_and_all_still_get_every_note_of_the_sequence(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.tick(T5)
        self.tick(T10)
        sequence = ["Scheduled Airtable sync started", RETRYING_1, RETRYING_2, EXHAUSTED]
        self.assertEqual(self.excerpts_for(ADMIN_EMAIL), sequence)
        self.assertEqual(self.excerpts_for(ALIAS_ADMIN_ALIAS), sequence)

    def test_none_gets_nothing_from_the_sequence(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.fail_step = "users"
        self.tick()
        self.tick(T5)
        self.tick(T10)
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])

    def test_a_scheduled_success_sends_failures_nothing(self) -> None:
        self.tick()
        self.assertEqual(self.finished(), [ALL_NOTE])
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])

    def test_a_first_attempt_with_no_retry_planned_reaches_failures(self) -> None:
        pending: list = []
        self.executor = pending.append
        self.fail_step = "users"
        self.tick()
        self.set_schedule(schedule_enabled=False)  # so no retry is planned when it ends
        pending[0]()
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [MANUAL_CRASH])
        self.assertEqual(self.finished(), [MANUAL_CRASH])

    def test_a_reaped_attempt_with_a_retry_planned_sends_failures_nothing(self) -> None:
        self.executor = [].append  # attempt 1's process "died"
        self.tick()
        self.executor = lambda fn: fn()
        self.tick(T6)
        self.assertEqual(
            self.finished()[0], f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {INTERRUPTED_BY_TICK}"
        )
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])

    def test_one_outlook_decides_the_excerpt_and_the_recipients(self) -> None:
        self.set_schedule(last_scheduled_slot=SLOT)
        run = self.add_attempt()
        for planned, excerpt, reaches_failures in ((True, RETRYING_1, False), (False, MANUAL_CRASH, True)):
            with self.subTest(planned=planned):
                self.db.query(sync_notifications.NotificationV2).delete()
                self.db.commit()
                # A second call would see the opposite answer.
                with mock.patch.object(scheduler, "retry_outlook", side_effect=[planned, not planned]) as outlook:
                    sync_notifications.run_finished(self.Session, run.id)
                self.assertEqual(outlook.call_count, 1)
                self.assertEqual(self.finished(), [excerpt])
                self.assertEqual(bool(self.excerpts_for(OTHER_ADMIN_EMAIL)), reaches_failures)

    def test_the_outlook_is_judged_at_the_runs_end_not_the_notifiers_clock(self) -> None:
        self.set_schedule(last_scheduled_slot=SLOT)
        run = self.add_attempt()
        with mock.patch.object(sync_notifications, "_utc_now", return_value=NOW + timedelta(days=30)):
            sync_notifications.run_finished(self.Session, run.id)
        self.assertEqual(self.finished(), [RETRYING_1])
        self.assertEqual(self.excerpts_for(OTHER_ADMIN_EMAIL), [])


# ---------------------------------------------------------
# The tick's reap
# ---------------------------------------------------------


class TestReap(RetryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.set_schedule(schedule_enabled=False)  # the tick only reaps

    def add_dead(self, trigger: str, email: str | None = ADMIN_EMAIL) -> AirtableSyncRunV2:
        return self.add_run(status="running", trigger=trigger, email=email, started=NOW - timedelta(minutes=6))

    def test_a_dead_manual_run_notifies_as_a_final_failure(self) -> None:
        self.set_mode(MEMBER_EMAIL, "all")
        dead = self.add_dead("manual")
        self.tick()
        self.assertEqual(self.run_row(dead.id).status, "interrupted")
        self.assertEqual(self.recipients(FINISHED), ADMINS)  # NULL, `all`, `failures`
        self.assertEqual(self.notes(STARTED), [])
        for n in self.notes(FINISHED):
            self.assertEqual(n.excerpt, REAPED)
            self.assertEqual((n.actor_email, n.actor_name), (ADMIN_EMAIL, "Ada Admin"))

    def test_none_gets_nothing_from_a_dead_manual_run(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.add_dead("manual")
        self.tick()
        self.assertEqual(self.recipients(FINISHED), sorted([ADMIN_EMAIL, ALIAS_ADMIN_ALIAS]))

    def test_a_dead_preview_notifies_nobody_and_never_reaches_the_notifier(self) -> None:
        calls: list[int] = []

        class Counting(SyncNotifier):
            def finished(inner, session_factory, run_id):
                calls.append(run_id)
                return super().finished(session_factory, run_id)

        runner = self.make_runner()
        runner.notifier = Counting()
        dead = self.add_dead("preview")
        self.tick(runner=runner)
        self.assertEqual(self.run_row(dead.id).status, "interrupted")
        self.assertEqual(calls, [])
        self.assertEqual(self.notes(), [])

    def test_a_preview_row_never_notifies_even_if_handed_to_the_notifier(self) -> None:
        dead = self.add_dead("preview")
        self.assertEqual(sync_notifications.run_finished(self.Session, dead.id), 0)
        self.assertEqual(sync_notifications.run_started(self.Session, dead.id), 0)
        self.assertEqual(self.notes(), [])

    def test_a_slot_less_scheduled_run_stays_silent(self) -> None:
        self.add_dead("scheduled", email=None)
        self.tick()
        self.assertEqual(self.notes(), [])

    def test_no_double_note_when_the_old_thread_finishes_or_a_later_tick_runs(self) -> None:
        pending: list = []
        self.executor = pending.append  # its process "died"
        self.assertEqual(self.start().status_code, 202)
        started = len(self.notes(STARTED))
        self.tick(T6)
        self.assertEqual(self.recipients(FINISHED), ADMINS)
        pending[0]()  # the old thread finally finishes: `_finish`'s guard
        self.tick(T6 + timedelta(minutes=6))
        self.assertEqual(self.recipients(FINISHED), ADMINS)
        self.assertEqual({n.excerpt for n in self.notes(FINISHED)}, {REAPED})
        self.assertEqual(len(self.notes(STARTED)), started)

    def test_a_live_manual_run_is_not_reaped_and_sends_nothing(self) -> None:
        live = self.add_run(status="running", trigger="manual", email=ADMIN_EMAIL, started=NOW - timedelta(minutes=4))
        self.tick()
        self.assertEqual(self.run_row(live.id).status, "running")
        self.assertEqual(self.notes(), [])

    def test_a_later_manual_starts_reap_stays_silent(self) -> None:
        dead = self.add_dead("manual", email=OTHER_ADMIN_EMAIL)
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.run_row(dead.id).status, "interrupted")
        self.assertEqual(self.excerpts_for(ADMIN_EMAIL), [ALL_NOTE])

    def test_a_raising_notifier_never_fails_the_tick(self) -> None:
        class Broken(SyncNotifier):
            def finished(self, session_factory, run_id):
                raise RuntimeError("notifier down")

        runner = self.make_runner()
        runner.notifier = Broken()
        dead = self.add_dead("manual")
        with self.assertLogs("app.services.airtable_sync.scheduler", level="ERROR"):
            self.assertEqual(self.tick(runner=runner)["result"], "not_due")
        self.assertEqual(self.run_row(dead.id).status, "interrupted")


# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------


class TestEndpoints(PreferenceFixture):
    def test_get_reads_null_as_all_without_storing_it(self) -> None:
        response = self.client.get(URL, headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": {"mode": "all"}})
        self.assertIsNone(self.stored(ADMIN_EMAIL))

    def test_put_stores_each_mode_and_get_reads_it_back(self) -> None:
        for mode in ("failures", "none", "all"):
            with self.subTest(mode=mode):
                response = self.client.put(URL, json={"mode": mode}, headers=self.auth(ADMIN_EMAIL))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"data": {"mode": mode}})
                self.assertEqual(self.stored(ADMIN_EMAIL), mode)
                self.assertEqual(self.client.get(URL, headers=self.auth(ADMIN_EMAIL)).json(), {"data": {"mode": mode}})

    def test_put_only_changes_the_callers_row(self) -> None:
        self.set_mode(OTHER_ADMIN_EMAIL, "none")
        self.client.put(URL, json={"mode": "failures"}, headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(self.stored(ADMIN_EMAIL), "failures")
        self.assertEqual(self.stored(OTHER_ADMIN_EMAIL), "none")
        self.assertIsNone(self.stored(ALIAS_ADMIN_ALIAS))
        self.assertIsNone(self.stored(MEMBER_EMAIL))
        self.assertEqual(self.client.get(URL, headers=self.auth(ALIAS_ADMIN_ALIAS)).json()["data"]["mode"], "all")

    def test_a_member_is_refused_and_stays_null(self) -> None:
        self.assertEqual(self.client.get(URL, headers=self.auth(MEMBER_EMAIL)).status_code, 403)
        for mode in AIRTABLE_SYNC_NOTIFICATION_MODES:
            self.assertEqual(self.client.put(URL, json={"mode": mode}, headers=self.auth(MEMBER_EMAIL)).status_code, 403)
        self.assertIsNone(self.stored(MEMBER_EMAIL))

    def test_no_token_is_refused(self) -> None:
        self.assertIn(self.client.get(URL).status_code, (401, 403))
        self.assertIn(self.client.put(URL, json={"mode": "none"}).status_code, (401, 403))

    def test_bad_bodies_are_422(self) -> None:
        bodies = (
            {},
            {"mode": "bogus"},
            {"mode": "ALL"},
            {"mode": ""},
            {"mode": None},
            {"mode": 1},
            {"mode": True},
            {"mode": ["all"]},
            {"enabled": False},  # the old body
            {"mode": "all", "enabled": True},
            {"mode": "all", "user_id": 1},
        )
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.put(URL, json=body, headers=self.auth(ADMIN_EMAIL))
                self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.stored(ADMIN_EMAIL))

    def test_the_schedule_save_leaves_it_alone(self) -> None:
        self.client.put(URL, json={"mode": "none"}, headers=self.auth(ADMIN_EMAIL))
        response = self.client.put(
            "/v2/airtable-sync/settings",
            json={
                "schedule_enabled": False,
                "frequency": "daily",
                "run_time": "06:00",
                "run_weekday": None,
                "timezone": "America/New_York",
                "interval_minutes": None,
            },
            headers=self.auth(ADMIN_EMAIL),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("mode", response.json()["data"])
        self.assertEqual(self.stored(ADMIN_EMAIL), "none")

    def test_the_service_setter_and_getter(self) -> None:
        user_id = self.users[ADMIN_EMAIL]
        self.assertEqual(settings_service.get_notifications_mode(self.db, user_id), "all")
        self.assertEqual(settings_service.set_notifications_mode(self.db, user_id, "failures"), "failures")
        self.assertEqual(settings_service.get_notifications_mode(self.db, user_id), "failures")
        self.db.rollback()  # flushes, never commits
        self.assertIsNone(self.stored(ADMIN_EMAIL))


# ---------------------------------------------------------
# The column
# ---------------------------------------------------------


class TestColumn(PreferenceFixture):
    def column(self):
        return UserV2.__table__.c.airtable_sync_notifications_mode

    def test_a_nullable_string_with_no_default(self) -> None:
        column = self.column()
        self.assertIsInstance(column.type, String)
        self.assertEqual(column.type.length, 8)
        self.assertTrue(column.nullable)
        self.assertIsNone(column.default)
        self.assertIsNone(column.server_default)

    def test_the_longest_value_fits(self) -> None:
        self.assertLessEqual(max(len(m) for m in AIRTABLE_SYNC_NOTIFICATION_MODES), self.column().type.length)

    def test_the_constants(self) -> None:
        self.assertEqual(AIRTABLE_SYNC_NOTIFICATION_MODES, ("all", "failures", "none"))
        self.assertEqual(AIRTABLE_SYNC_NOTIFICATION_DEFAULT_MODE, "all")

    def test_the_boolean_is_gone_from_the_model(self) -> None:
        self.assertNotIn("airtable_sync_notifications_enabled", UserV2.__table__.c)

    def test_a_row_inserted_without_the_column_reads_null(self) -> None:
        # No server default: what a row created outside the ORM gets.
        self.db.execute(text("INSERT INTO users (name) VALUES ('raw')"))
        self.db.commit()
        row = self.db.query(UserV2).filter(UserV2.name == "raw").one()
        self.assertIsNone(row.airtable_sync_notifications_mode)

    def test_an_orm_row_reads_null(self) -> None:
        self.assertIsNone(self.stored(MEMBER_EMAIL))

    def test_the_check_refuses_a_bad_value(self) -> None:
        self.assertIn(
            AIRTABLE_SYNC_NOTIFICATIONS_MODE_CHECK_NAME,
            {c.name for c in UserV2.__table__.constraints},
        )
        for bad in ("bogus", "ALL", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(IntegrityError):
                    self.set_mode(ADMIN_EMAIL, bad)
                self.db.rollback()
                with self.assertRaises(IntegrityError):
                    self.db.execute(
                        text("UPDATE users SET airtable_sync_notifications_mode = :m WHERE id = :i"),
                        {"m": bad, "i": self.users[ADMIN_EMAIL]},
                    )
                self.db.rollback()
        self.assertIsNone(self.stored(ADMIN_EMAIL))

    def test_the_check_takes_each_value_and_null(self) -> None:
        for mode in (*AIRTABLE_SYNC_NOTIFICATION_MODES, None):
            self.set_mode(ADMIN_EMAIL, mode)
            self.assertEqual(self.stored(ADMIN_EMAIL), mode)


if __name__ == "__main__":
    unittest.main()
