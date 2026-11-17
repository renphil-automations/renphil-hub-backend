"""Airtable sync RI-3 (AI Docs/plan_airtable_sync_retries_interval_2026-10-29.md
§7, §3.4 step 1, §9 RI-3): notifications for retries.

  - each row of §7's table, exact text: attempt 1's start; no start for
    attempts 2–3; "failed (attempt n of 3), retrying"; "failed after n
    attempts"; today's "failed" for attempt 1 with no retry; "finished on
    attempt n of 3"; the 200-char clip on each failure wording
  - per-slot totals: all fail → 4 notes per admin; success on 2 → 3
  - `retry_outlook`: the tick's own rules (`decide` + `_retry` →
    `decide_retry`) minus the lock and bulk facts; RETRY_WAIT never reads
    as "no retry"
  - the tick's reap: a scheduled attempt (slot set) notifies like a failure,
    once; a manual start's reap, a reaped preview / slot-less scheduled run
    notify nobody (a reaped manual run does notify: see
    test_airtable_sync_notification_preference)
  - manual runs and previews unchanged (phase 7's module covers the rest,
    unedited); a notifier error never fails the tick

In-memory SQLite on RI-2's RetryTickFixture (phase 9's TickFixture: phase 7's
three admins, stub steps, `fail_step`, the runner's clock frozen at NOW).
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import datetime, time, timedelta
from unittest import mock

# First: phase 7's module (via phase 9's) sets the test environment.
from tests.test_airtable_sync_phase9 import NOW, SLOT  # isort: skip

from app.db_v2.models.airtable_sync import AirtableSyncRunV2  # noqa: E402
from app.services.airtable_sync import notifications  # noqa: E402
from app.services.airtable_sync import scheduler  # noqa: E402
from app.services.airtable_sync.notifications import SyncNotifier  # noqa: E402
from app.services.airtable_sync.runner import INTERRUPTED_BY_TICK  # noqa: E402
from tests.test_airtable_sync_phase7 import (  # noqa: E402
    ADMIN_EMAIL,
    ALIAS_ADMIN_ALIAS,
    FINISHED,
    OTHER_ADMIN_EMAIL,
    STARTED,
)
from tests.test_airtable_sync_ri2 import T5, T10, T15, RetryTickFixture  # noqa: E402

ADMINS = [ADMIN_EMAIL, ALIAS_ADMIN_ALIAS, OTHER_ADMIN_EMAIL]
CRASH = "Unexpected error (RuntimeError); details are in the server log."
T6 = NOW + timedelta(minutes=6)
T12 = NOW + timedelta(minutes=12)


class Ri3Fixture(RetryTickFixture):
    def finished(self) -> list[str]:
        """The end notes' excerpts, one per note set (all admins get the same)."""
        excerpts: list[str] = []
        for n in self.notes(FINISHED):
            if n.recipient_email == ADMIN_EMAIL:
                excerpts.append(n.excerpt)
        return excerpts

    def started(self) -> list[str]:
        return [n.excerpt for n in self.notes(STARTED) if n.recipient_email == ADMIN_EMAIL]

    def per_admin(self) -> Counter:
        return Counter(n.recipient_email for n in self.notes())

    def add_attempt(self, *, status: str = "failed", error: str | None = CRASH, at: datetime = NOW, slot=SLOT,
                    trigger: str = "scheduled") -> AirtableSyncRunV2:
        """A closed run row written directly (for the line / outlook tests)."""
        run = AirtableSyncRunV2(
            steps=["clusters"],
            trigger=trigger,
            status=status,
            error=error if status != "succeeded" else None,
            started_at=at,
            heartbeat_at=at,
            finished_at=None if status == "running" else at,
            scheduled_slot=slot,
            summary={"preview": False, "steps": {}},
        )
        self.db.add(run)
        self.db.commit()
        return run

    def line(self, run: AirtableSyncRunV2) -> str:
        db = self.Session()
        try:
            return notifications.finished_line(db, db.get(AirtableSyncRunV2, run.id))
        finally:
            db.close()

    def outlook(self, run: AirtableSyncRunV2, now: datetime = NOW) -> bool:
        db = self.Session()
        try:
            return scheduler.retry_outlook(db, db.get(AirtableSyncRunV2, run.id), now)
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# §7's table, through real ticks
# ─────────────────────────────────────────────────────────────────────────────


class WordingTests(Ri3Fixture):
    def test_attempt_one_start_is_unchanged(self) -> None:
        self.tick()
        self.assertEqual(self.recipients(STARTED), ADMINS)
        self.assertEqual({n.excerpt for n in self.notes(STARTED)}, {"Scheduled Airtable sync started"})

    def test_all_attempts_fail(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.tick(T5)
        self.tick(T10)
        self.assertEqual(self.tick(T15)["retry_reason"], "attempts_exhausted")
        self.assertEqual(self.started(), ["Scheduled Airtable sync started"])  # attempts 2–3: none
        self.assertEqual(
            self.finished(),
            [
                f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {CRASH}",
                f"Scheduled Airtable sync failed (attempt 2 of 3), retrying: {CRASH}",
                f"Scheduled Airtable sync failed after 3 attempts: {CRASH}",
            ],
        )

    def test_success_on_attempt_two(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.step_counts = {"clusters": {"created": 2, "renamed": 1}}
        self.tick(T5)
        self.assertEqual(self.started(), ["Scheduled Airtable sync started"])
        self.assertEqual(
            self.finished(),
            [
                f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {CRASH}",
                "Airtable sync finished on attempt 2 of 3: 1 updated, 2 created, 0 conflicts, 0 warnings",
            ],
        )

    def test_success_on_attempt_three(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.tick(T5)
        self.fail_step = None
        self.tick(T10)
        self.assertEqual(
            self.finished()[-1], "Airtable sync finished on attempt 3 of 3: 0 updated, 0 created, 0 conflicts, 0 warnings"
        )

    def test_a_first_attempt_success_reads_as_today(self) -> None:
        self.tick()
        self.assertEqual(self.finished(), ["Airtable sync finished: 0 updated, 0 created, 0 conflicts, 0 warnings"])

    def test_attempt_one_with_no_retry_reads_as_today(self) -> None:
        # The schedule is switched off while attempt 1 runs: no retry follows.
        pending: list = []
        self.executor = pending.append
        self.fail_step = "users"
        self.tick()
        self.set_schedule(schedule_enabled=False)
        pending[0]()
        self.assertEqual(self.finished(), [f"Airtable sync failed: {CRASH}"])

    def test_attempt_two_with_no_retry_says_after_two_attempts(self) -> None:
        self.fail_step = "users"
        self.tick()
        pending: list = []
        self.executor = pending.append
        self.assertEqual(self.tick(T5)["attempt"], 2)
        self.set_schedule(schedule_enabled=False)
        pending[0]()
        self.assertEqual(
            self.finished(),
            [
                f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {CRASH}",
                f"Scheduled Airtable sync failed after 2 attempts: {CRASH}",
            ],
        )

    def test_a_called_off_retry_sends_no_extra_note(self) -> None:
        # "retrying" can be the last note: a manual success calls it off.
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.assertEqual(self.start(steps=("clusters", "programs")).status_code, 202)
        self.assertEqual(self.tick(T5)["retry_reason"], "succeeded_since")
        self.assertEqual(
            self.finished(),
            [
                f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {CRASH}",
                "Airtable sync finished: 0 updated, 0 created, 0 conflicts, 0 warnings",  # the manual run
            ],
        )


class TotalsTests(Ri3Fixture):
    def test_all_fail_is_four_notes_per_admin(self) -> None:
        self.fail_step = "users"
        for at in (NOW, T5, T10, T15):
            self.tick(at)
        self.assertEqual(self.per_admin(), Counter({email: 4 for email in ADMINS}))

    def test_success_on_two_is_three_notes_per_admin(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.fail_step = None
        self.tick(T5)
        self.tick(T10)
        self.assertEqual(self.per_admin(), Counter({email: 3 for email in ADMINS}))

    def test_first_try_success_is_two_notes_per_admin(self) -> None:
        self.tick()
        self.tick(T5)
        self.assertEqual(self.per_admin(), Counter({email: 2 for email in ADMINS}))


# ─────────────────────────────────────────────────────────────────────────────
# The start note and the attempt number
# ─────────────────────────────────────────────────────────────────────────────


class StartNoteTests(Ri3Fixture):
    def test_run_started_skips_attempts_after_the_first(self) -> None:
        first = self.add_attempt(status="failed")
        second = self.add_attempt(status="running")
        self.assertEqual(notifications.run_started(self.Session, second.id), 0)
        self.assertEqual(self.notes(STARTED), [])
        self.assertEqual(notifications.run_started(self.Session, first.id), 3)

    def test_a_slotless_scheduled_run_still_gets_its_start(self) -> None:
        run = self.add_attempt(status="running", slot=None)
        self.assertEqual(notifications.run_started(self.Session, run.id), 3)

    def test_attempt_number_counts_by_id_within_the_slot(self) -> None:
        a = self.add_attempt()
        other = self.add_attempt(slot=SLOT - timedelta(days=1))
        b = self.add_attempt()
        db = self.Session()
        try:
            got = [notifications.attempt_number(db, db.get(AirtableSyncRunV2, r.id)) for r in (a, other, b)]
            manual = self.add_run(status="failed")
            got.append(notifications.attempt_number(db, db.get(AirtableSyncRunV2, manual.id)))
        finally:
            db.close()
        self.assertEqual(got, [1, 1, 2, None])


# ─────────────────────────────────────────────────────────────────────────────
# finished_line on rows written directly: the clip, each branch
# ─────────────────────────────────────────────────────────────────────────────


class LineTests(Ri3Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.set_schedule(last_scheduled_slot=SLOT)  # as the first try's tick left it

    def assert_clipped(self, text: str, prefix: str) -> None:
        self.assertEqual(len(text), 200)
        self.assertEqual(text, (prefix + "x" * 500)[:199] + "…")

    def test_retrying_is_clipped(self) -> None:
        run = self.add_attempt(error="x" * 500)
        self.assert_clipped(self.line(run), "Scheduled Airtable sync failed (attempt 1 of 3), retrying: ")

    def test_after_n_attempts_is_clipped(self) -> None:
        self.add_attempt()
        self.add_attempt()
        run = self.add_attempt(error="x" * 500)
        self.assert_clipped(self.line(run), "Scheduled Airtable sync failed after 3 attempts: ")

    def test_attempt_one_no_retry_is_clipped(self) -> None:
        self.set_schedule(schedule_enabled=False)
        run = self.add_attempt(error="x" * 500)
        self.assert_clipped(self.line(run), "Airtable sync failed: ")

    def test_interrupted_reads_like_failed(self) -> None:
        run = self.add_attempt(status="interrupted", error=INTERRUPTED_BY_TICK)
        self.assertEqual(self.line(run), f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {INTERRUPTED_BY_TICK}")

    def test_no_error_text_falls_back_to_the_status(self) -> None:
        run = self.add_attempt(status="interrupted", error=None)
        self.assertEqual(self.line(run), "Scheduled Airtable sync failed (attempt 1 of 3), retrying: interrupted")

    def test_judged_at_the_runs_end_not_the_notifiers_clock(self) -> None:
        run = self.add_attempt()
        with mock.patch.object(notifications, "_utc_now", return_value=NOW + timedelta(days=30)):
            self.assertIn("retrying", self.line(run))

    def test_a_manual_failure_after_a_scheduled_one_reads_as_today(self) -> None:
        self.add_attempt()
        manual = self.add_run(status="failed")
        manual.error = "boom"
        self.db.commit()
        self.assertEqual(self.line(manual), "Airtable sync failed: boom")


# ─────────────────────────────────────────────────────────────────────────────
# retry_outlook = the tick's rules minus the lock and bulk facts
# ─────────────────────────────────────────────────────────────────────────────


class OutlookTests(Ri3Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.set_schedule(last_scheduled_slot=SLOT)

    def test_a_failed_first_attempt_is_retried(self) -> None:
        self.assertTrue(self.outlook(self.add_attempt()))

    def test_it_asks_decide_retry(self) -> None:
        run = self.add_attempt()
        for due in (True, False):
            verdict = scheduler.RetryDecision(due, None if due else "attempts_exhausted", 2 if due else None)
            with self.subTest(due=due), mock.patch.object(scheduler, "decide_retry", return_value=verdict) as spy:
                self.assertIs(self.outlook(run), due)
                spy.assert_called_once()
                self.assertEqual([a.id for a in spy.call_args.args[1]], [run.id])

    def test_attempts_exhausted(self) -> None:
        self.add_attempt()
        self.add_attempt()
        self.assertFalse(self.outlook(self.add_attempt()))

    def test_succeeded_since(self) -> None:
        run = self.add_attempt()
        self.add_run(status="succeeded")
        self.assertFalse(self.outlook(run))

    def test_a_preview_success_does_not_call_it_off(self) -> None:
        run = self.add_attempt()
        self.add_run(status="succeeded", trigger="preview")
        self.assertTrue(self.outlook(run))

    def test_slot_too_old(self) -> None:
        late = SLOT + scheduler.SLOT_MAX_AGE
        self.assertFalse(self.outlook(self.add_attempt(at=late), late))

    def test_next_slot_soon(self) -> None:
        # A daily slot can't be near its next one inside 6 h (that is RI-4's
        # interval case), so the next slot is stubbed at exactly RETRY_ROOM.
        with mock.patch.object(scheduler, "next_slot", return_value=NOW + scheduler.RETRY_ROOM):
            self.assertFalse(self.outlook(self.add_attempt()))

    def test_schedule_off(self) -> None:
        run = self.add_attempt()
        self.set_schedule(schedule_enabled=False)
        self.assertFalse(self.outlook(run))

    def test_a_slot_replaced_by_a_newer_one(self) -> None:
        run = self.add_attempt()
        self.set_schedule(last_scheduled_slot=SLOT + timedelta(days=1))
        self.assertFalse(self.outlook(run, NOW + timedelta(days=1)))

    def test_a_used_slot_that_is_not_the_last_one_is_not_retried(self) -> None:
        # The tick retries only `slot == last_scheduled_slot` (RI-2); a later
        # instant was consumed (run time moved back), so neither does the note.
        run = self.add_attempt()
        self.set_schedule(last_scheduled_slot=SLOT + timedelta(hours=1))
        self.assertFalse(self.outlook(run))

    def test_a_run_time_moved_earlier_replaces_the_slot(self) -> None:
        # The latest slot is now 09:00 (used: ≤ last), not this run's 10:00;
        # the tick wouldn't retry 10:00 (RI-2's "slot replaced" case).
        run = self.add_attempt()
        self.set_schedule(run_time=time(9, 0))
        self.assertFalse(self.outlook(run))

    def test_a_newer_unused_slot_is_a_first_try_not_a_retry(self) -> None:
        run = self.add_attempt()
        self.assertFalse(self.outlook(run, NOW + timedelta(days=1)))  # tomorrow's slot is due

    def test_a_success_or_a_slotless_run_is_never_retried(self) -> None:
        self.assertFalse(self.outlook(self.add_attempt(status="succeeded")))
        self.assertFalse(self.outlook(self.add_attempt(slot=None)))

    def test_the_wait_never_reads_as_no_retry(self) -> None:
        run = self.add_attempt()
        with mock.patch.object(scheduler, "RETRY_WAIT", timedelta(minutes=15)):
            self.assertTrue(self.outlook(run))
            # ...but it is judged when the wait ends: past the 6 h limit then.
            late = self.add_attempt(at=SLOT + scheduler.SLOT_MAX_AGE - timedelta(minutes=10))
            self.assertFalse(self.outlook(late, SLOT + scheduler.SLOT_MAX_AGE - timedelta(minutes=10)))

    def test_a_busy_lock_or_a_bulk_job_does_not_matter(self) -> None:
        run = self.add_attempt()
        self.add_job()  # a live bulk job only delays the retry
        self.add_run(status="running")  # nor does a live manual run
        self.assertTrue(self.outlook(run))


# ─────────────────────────────────────────────────────────────────────────────
# The tick's reap
# ─────────────────────────────────────────────────────────────────────────────


class ReapNoteTests(Ri3Fixture):
    def test_a_reaped_scheduled_attempt_notifies_once(self) -> None:
        pending: list = []
        self.executor = pending.append  # attempt 1's process "died"
        self.tick()
        result = self.tick(T6)
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(self.recipients(FINISHED), ADMINS)
        self.assertEqual(
            self.finished(), [f"Scheduled Airtable sync failed (attempt 1 of 3), retrying: {INTERRUPTED_BY_TICK}"]
        )
        for n in self.notes(FINISHED):
            self.assertEqual((n.actor_email, n.actor_name), (None, None))
        self.assertEqual(self.started(), ["Scheduled Airtable sync started"])  # attempt 2: none
        pending[0]()  # the old thread finally finishes: `_finish`'s guard, no second note
        self.assertEqual(len(self.finished()), 1)

    def test_the_note_comes_after_the_reaps_commit(self) -> None:
        pending: list = []
        self.executor = pending.append
        first = self.tick()
        seen: dict = {}

        class Peek(SyncNotifier):
            def finished(inner, session_factory, run_id):
                seen["status"] = self.run_row(run_id).status
                return super().finished(session_factory, run_id)

        runner = self.make_runner()
        runner.notifier = Peek()
        self.tick(T6, runner=runner)
        self.assertEqual(seen, {"status": "interrupted"})
        self.assertEqual(self.run_row(first["run_id"]).error, INTERRUPTED_BY_TICK)

    def test_a_reaped_third_attempt_says_after_three(self) -> None:
        self.fail_step = "users"
        self.tick()
        self.tick(T5)
        pending: list = []
        self.executor = pending.append
        self.assertEqual(self.tick(T10)["attempt"], 3)
        self.assertEqual(self.tick(T10 + timedelta(minutes=6))["retry_reason"], "attempts_exhausted")
        self.assertEqual(self.finished()[-1], f"Scheduled Airtable sync failed after 3 attempts: {INTERRUPTED_BY_TICK}")

    def test_a_reaped_attempt_with_the_schedule_off_reads_as_today(self) -> None:
        pending: list = []
        self.executor = pending.append
        self.tick()
        self.set_schedule(schedule_enabled=False)
        self.assertEqual(self.tick(T6)["reason"], "schedule_disabled")
        self.assertEqual(self.finished(), [f"Airtable sync failed: {INTERRUPTED_BY_TICK}"])

    def test_a_manual_starts_reap_notifies_nobody(self) -> None:
        pending: list = []
        self.executor = pending.append
        self.tick()
        self.executor = lambda fn: fn()
        with mock.patch("app.services.airtable_sync.runner._utc_now", return_value=T6):
            self.assertEqual(self.start().status_code, 202)  # reaps attempt 1 with INTERRUPTED_ERROR
        self.assertEqual(self.finished(), ["Airtable sync finished: 0 updated, 0 created, 0 conflicts, 0 warnings"])

    def test_reaped_non_attempts_notify_nobody(self) -> None:
        # Schedule off: the tick only reaps. A preview and a scheduled run
        # from before RI-1 (no slot) close silently.
        self.set_schedule(schedule_enabled=False)
        for trigger in ("preview", "scheduled"):
            with self.subTest(trigger=trigger):
                stale = self.add_attempt(status="running", trigger=trigger, slot=None, at=NOW - timedelta(minutes=6))
                self.assertEqual(self.tick()["reason"], "schedule_disabled")
                self.assertEqual(self.run_row(stale.id).status, "interrupted")
                self.assertEqual(self.notes(), [])

    def test_a_reaped_scheduled_attempt_notifies_even_with_the_schedule_off(self) -> None:
        self.set_schedule(schedule_enabled=False, last_scheduled_slot=SLOT)
        self.add_attempt(status="running", at=NOW - timedelta(minutes=6))
        self.tick()
        self.assertEqual(self.finished(), [f"Airtable sync failed: {INTERRUPTED_BY_TICK}"])


class AdvisoryTests(Ri3Fixture):
    def _dead_attempt(self) -> None:
        pending: list = []
        self.executor = pending.append
        self.tick()

    def test_a_raising_notifier_never_fails_the_tick(self) -> None:
        self._dead_attempt()

        class Broken(SyncNotifier):
            def finished(self, session_factory, run_id):
                raise RuntimeError("notifier down")

        runner = self.make_runner()
        runner.notifier = Broken()
        with self.assertLogs("app.services.airtable_sync.scheduler", level="ERROR") as logs:
            result = self.tick(T6, runner=runner)
        self.assertEqual(result["attempt"], 2)  # the retry still starts
        self.assertIn("could not notify", logs.output[0])
        self.assertEqual(self.notes(FINISHED), [])

    def test_an_error_inside_the_note_never_fails_the_tick(self) -> None:
        self._dead_attempt()
        with mock.patch.object(notifications, "admin_recipients", side_effect=RuntimeError("db gone")):
            with self.assertLogs("app.services.airtable_sync.notifications", level="ERROR"):
                result = self.tick(T6)
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(self.notes(FINISHED), [])

    def test_an_outlook_error_never_fails_the_run(self) -> None:
        self.fail_step = "users"
        with mock.patch.object(scheduler, "retry_outlook", side_effect=RuntimeError("x")):
            with self.assertLogs("app.services.airtable_sync.notifications", level="ERROR"):
                result = self.tick()
        self.assertEqual(self.run_row(result["run_id"]).status, "failed")
        self.assertEqual(self.notes(FINISHED), [])


class UnchangedTests(Ri3Fixture):
    def test_manual_and_preview_runs_read_as_today(self) -> None:
        self.set_schedule(schedule_enabled=False)
        self.fail_step = "users"
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.start(preview=True).status_code, 202)
        self.assertEqual(self.finished(), [f"Airtable sync failed: {CRASH}"])  # the preview sends none
        self.assertEqual({n.excerpt for n in self.notes(STARTED)}, {"Manual Airtable sync started by Ada Admin"})


if __name__ == "__main__":
    unittest.main()
