"""Airtable sync, phase 10 follow-up (owner, 2026-10-27): background bulk
answers. "Accept all" / "Keep all" as a job with tracked progress
(services/airtable_sync/bulk_jobs.py), so the tab can follow it after the
admin navigates away, and a second click can't race the first.

SQLite in memory with every session closed; phase 6's fixture (Bo has two
first-link field conflicts, Dee one, after the first run). Jobs run through
the real router with an inline or a deferred executor.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from tests.test_airtable_sync_phase6 import (  # noqa: E402  (sets the env first)
    ADMIN_EMAIL,
    MEMBER_EMAIL,
    T_ROLES,
    T_USERS,
    U,
    SyncFixture,
    person,
    role_row,
)

from app.db_v2.models.airtable_sync import AirtableSyncBulkJobV2, AirtableSyncRunV2  # noqa: E402
from app.services.airtable_sync import bulk_jobs, resolutions  # noqa: E402
from app.services.airtable_sync.bulk_jobs import BulkJobRunner, get_bulk_job_runner  # noqa: E402
from app.services.airtable_sync.runner import _Heartbeat  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobFixture(SyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.dee = self.add_user(
            "Dee", "dee@renphil.org", first_name="D.", employment_type="Full-time US", start_date=date(2024, 1, 15),
            manager_tech_stack_selections="Slack, Notion", add_to_website_date=datetime(2024, 2, 1, 9, 30, tzinfo=timezone.utc),
            birthday_shoutout_opt_out=True,
        )
        self.tables[T_USERS]["records"].append(person(4, "Dee", "dee@renphil.org"))
        self.tables[T_ROLES]["records"].append(role_row(4, U(4)))
        self.run = self.run_sync()
        self.conflicts = [i.id for i in self.items("field_conflict")]
        self.assertEqual(len(self.conflicts), 3)
        # Inline by default; `self.defer = True` keeps the body for later.
        self.defer = False
        self.deferred: list = []
        self.client.app.dependency_overrides[get_bulk_job_runner] = self.make_job_runner

    def make_job_runner(self) -> BulkJobRunner:
        def executor(fn):
            if self.defer:
                self.deferred.append(fn)
            else:
                fn()

        return BulkJobRunner(session_factory=self.Session, executor=executor, heartbeat_interval=None)

    # ── requests ─────────────────────────────────────────────────────────

    def start(self, body: dict | None = None, email: str = ADMIN_EMAIL):
        body = body or {"run_id": self.run.id, "kind": "field_conflict", "choice": "accept_airtable", "label": "Accept all"}
        return self.client.post("/v2/airtable-sync/bulk-jobs", json=body, headers=self.auth(email))

    def start_ok(self, body: dict | None = None) -> dict:
        res = self.start(body)
        self.assertEqual(res.status_code, 202, res.text)
        self.db.expire_all()
        return res.json()["data"]

    def status(self) -> dict:
        res = self.client.get("/v2/airtable-sync/status", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["data"]

    def job(self, job_id: int) -> AirtableSyncBulkJobV2:
        self.db.expire_all()
        return self.db.get(AirtableSyncBulkJobV2, job_id)

    def fresh_job(self, job_id: int) -> AirtableSyncBulkJobV2:
        db = self.Session()
        try:
            job = db.get(AirtableSyncBulkJobV2, job_id)
            db.expunge(job)
            return job
        finally:
            db.close()

    def write_job(self, job_id: int, **values) -> None:
        db = self.Session()
        try:
            job = db.get(AirtableSyncBulkJobV2, job_id)
            for k, v in values.items():
                setattr(job, k, v)
            db.commit()
        finally:
            db.close()

    def after_item(self, n: int, action):
        """Patch resolve_item so `action(job_id)` runs right after the n-th
        (1-based) call returns."""
        original = resolutions.resolve_item
        calls = []

        def wrapper(db, item_id, **kw):
            out = original(db, item_id, **kw)
            calls.append(item_id)
            if len(calls) == n:
                action(kw.get("bulk_job_id"))
            return out

        return mock.patch.object(resolutions, "resolve_item", wrapper)


class TestJob(JobFixture):
    def test_answers_every_item_in_the_background(self) -> None:
        data = self.start_ok()
        self.assertEqual((data["status"], data["total"], data["done"], data["not_attempted"]), ("succeeded", 3, 3, 0))
        self.assertEqual(data["label"], "Accept all")
        self.assertEqual(data["target"], {"run_id": self.run.id, "kind": "field_conflict"})
        self.assertEqual(data["started_by_email"], ADMIN_EMAIL)
        self.assertIsNotNone(data["finished_at"])
        self.assertEqual(self.items("field_conflict"), [])
        self.assertEqual(self.user(self.bo).first_name, "Bo")  # Airtable's value
        resolved = self.items("field_conflict", status="resolved")
        self.assertEqual({(i.resolution, i.resolved_by_email) for i in resolved}, {("accept_airtable", ADMIN_EMAIL)})
        self.assertEqual(self.job(data["id"]).item_ids, sorted(self.conflicts))
        self.assertEqual(data["last_item_id"], max(self.conflicts))

    def test_status_shows_the_live_job_then_the_last_one(self) -> None:
        self.defer = True
        data = self.start_ok()
        self.assertEqual((data["status"], data["done"], data["total"]), ("running", 0, 3))
        s = self.status()
        self.assertEqual(s["current_bulk_job"]["id"], data["id"])
        self.assertIsNone(s["last_bulk_job"])
        self.deferred.pop()()
        s = self.status()
        self.assertIsNone(s["current_bulk_job"])
        self.assertEqual((s["last_bulk_job"]["id"], s["last_bulk_job"]["status"]), (data["id"], "succeeded"))
        res = self.client.get(f"/v2/airtable-sync/bulk-jobs/{data['id']}", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual((res.status_code, res.json()["data"]["done"]), (200, 3))

    def test_progress_moves_after_every_item(self) -> None:
        seen = []
        original = resolutions.resolve_item

        def wrapper(db, item_id, **kw):
            job = self.fresh_job(kw["bulk_job_id"])
            seen.append((job.done, job.last_item_id))
            return original(db, item_id, **kw)

        with mock.patch.object(resolutions, "resolve_item", wrapper):
            data = self.start_ok()
        ids = sorted(self.conflicts)
        self.assertEqual(seen, [(0, None), (1, ids[0]), (2, ids[1])])
        self.assertEqual(data["done"], 3)

    def test_item_ids_target_and_empty_target(self) -> None:
        bo = [i.id for i in self.items("field_conflict", rid=U(2))]
        data = self.start_ok({"item_ids": list(reversed(bo)), "choice": "keep_current"})
        self.assertEqual((data["status"], data["total"], data["target"]), ("succeeded", 2, {"item_ids": True}))
        self.assertEqual(len(self.items("field_conflict", rid=U(4))), 1)  # Dee untouched
        data = self.start_ok({"run_id": self.run.id + 99, "choice": "keep_current"})
        self.assertEqual((data["status"], data["total"], data["done"]), ("succeeded", 0, 0))

    def test_sweeps_the_locks_once(self) -> None:
        with mock.patch.object(bulk_jobs, "release_unauthorized_locks", return_value=0) as sweep:
            self.start_ok()
        self.assertEqual(sweep.call_count, 1)
        with mock.patch.object(bulk_jobs, "release_unauthorized_locks", return_value=0) as sweep:
            self.start_ok({"run_id": self.run.id + 99, "choice": "keep_current"})
        self.assertEqual(sweep.call_count, 0)  # nothing answered


class TestOneAtATime(JobFixture):
    def test_a_live_job_refuses_every_other_answer(self) -> None:
        self.defer = True
        data = self.start_ok()
        res = self.resolve(self.conflicts[0], "keep_current")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "bulk_in_progress"))
        self.assertEqual(res.json()["detail"]["job_id"], data["id"])
        res = self.bulk({"item_ids": self.conflicts, "choice": "keep_current"})
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "bulk_in_progress"))
        res = self.start()
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "bulk_in_progress"))
        self.assertEqual(len(self.items("field_conflict")), 3)  # nothing answered
        self.assertEqual(self.db.query(AirtableSyncBulkJobV2).count(), 1)
        # The job itself is not refused, and answers go through once it ends.
        self.deferred.pop()()
        self.assertEqual(self.job(data["id"]).status, "succeeded")
        self.assertEqual(self.items("field_conflict"), [])

    def test_refused_during_a_run(self) -> None:
        self.db.add(AirtableSyncRunV2(steps=["users"], trigger="manual", status="running", started_at=_now(), heartbeat_at=_now()))
        self.db.commit()
        res = self.start()
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "run_in_progress"))
        self.assertEqual(self.db.query(AirtableSyncBulkJobV2).count(), 0)

    def test_a_stale_job_blocks_nothing_and_is_marked_interrupted(self) -> None:
        old = _now() - timedelta(minutes=6)
        self.db.add(AirtableSyncBulkJobV2(choice="keep_current", item_ids=[1, 2], total=2, done=1, status="running", started_at=old, heartbeat_at=old))
        self.db.commit()
        stale = self.db.query(AirtableSyncBulkJobV2).one()
        self.assertTrue(self.status()["current_bulk_job"]["stale"])
        self.resolved_ok(self.conflicts[0], "keep_current")  # not blocked
        data = self.start_ok()
        self.assertEqual(data["status"], "succeeded")
        stale = self.job(stale.id)
        self.assertEqual((stale.status, stale.not_attempted), ("interrupted", 1))
        self.assertIn("No heartbeat", stale.error)

    def test_a_fresh_job_still_blocks(self) -> None:
        recent = _now() - timedelta(minutes=4)
        self.db.add(AirtableSyncBulkJobV2(choice="keep_current", item_ids=[], total=0, status="running", started_at=recent, heartbeat_at=recent))
        self.db.commit()
        self.assertEqual(self.resolve(self.conflicts[0], "keep_current").status_code, 409)
        self.assertEqual(self.start().status_code, 409)


class TestStopping(JobFixture):
    def test_stop_before_the_first_item(self) -> None:
        self.defer = True
        data = self.start_ok()
        res = self.client.post(f"/v2/airtable-sync/bulk-jobs/{data['id']}/stop", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["data"]["stop_requested"])
        self.assertEqual(res.json()["data"]["stop_requested_by_email"], ADMIN_EMAIL)
        self.deferred.pop()()
        job = self.job(data["id"])
        self.assertEqual((job.status, job.stop_reason, job.done, job.not_attempted), ("stopped", "stop_requested", 0, 3))
        self.assertEqual(len(self.items("field_conflict")), 3)

    def test_stop_midway_keeps_what_was_answered(self) -> None:
        with self.after_item(1, lambda job_id: self.write_job(job_id, stop_requested_by_email="other@renphil.org")):
            data = self.start_ok()
        self.assertEqual((data["status"], data["stop_reason"], data["done"], data["not_attempted"]), ("stopped", "stop_requested", 1, 2))
        self.assertEqual(len(self.items("field_conflict")), 2)
        self.assertEqual(len(self.items("field_conflict", status="resolved")), 1)

    def test_stop_closes_a_stale_job_at_once(self) -> None:
        # Its thread is gone, so nothing would honour the flag (2026-10-29).
        old = _now() - timedelta(minutes=6)
        self.db.add(AirtableSyncBulkJobV2(choice="keep_current", item_ids=[1, 2, 3], total=3, done=1, status="running", started_at=old, heartbeat_at=old))
        self.db.commit()
        job_id = self.db.query(AirtableSyncBulkJobV2).one().id
        res = self.client.post(f"/v2/airtable-sync/bulk-jobs/{job_id}/stop", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual((data["status"], data["not_attempted"], data["stale"]), ("interrupted", 2, False))
        self.assertEqual(data["stop_requested_by_email"], ADMIN_EMAIL)
        self.assertIn("pressed Stop", data["error"])
        self.assertIsNone(self.status()["current_bulk_job"])
        self.start_ok()  # nothing left in the way

    def test_stop_on_a_fresh_job_only_asks(self) -> None:
        recent = _now() - timedelta(minutes=4)
        self.db.add(AirtableSyncBulkJobV2(choice="keep_current", item_ids=[], total=0, status="running", started_at=recent, heartbeat_at=recent))
        self.db.commit()
        job_id = self.db.query(AirtableSyncBulkJobV2).one().id
        res = self.client.post(f"/v2/airtable-sync/bulk-jobs/{job_id}/stop", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual((res.status_code, res.json()["data"]["status"]), (200, "running"))
        self.assertTrue(res.json()["data"]["stop_requested"])

    def test_stop_an_ended_or_unknown_job(self) -> None:
        data = self.start_ok()
        res = self.client.post(f"/v2/airtable-sync/bulk-jobs/{data['id']}/stop", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "bulk_job_not_running"))
        res = self.client.post("/v2/airtable-sync/bulk-jobs/999/stop", headers=self.auth(ADMIN_EMAIL))
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.client.get("/v2/airtable-sync/bulk-jobs/999", headers=self.auth(ADMIN_EMAIL)).status_code, 404)

    def test_a_run_starting_midway_stops_the_job(self) -> None:
        def start_run(_job_id):
            db = self.Session()
            try:
                db.add(AirtableSyncRunV2(steps=["users"], trigger="manual", status="running", started_at=_now(), heartbeat_at=_now()))
                db.commit()
            finally:
                db.close()

        with self.after_item(1, start_run):
            data = self.start_ok()
        self.assertEqual((data["status"], data["stop_reason"], data["done"], data["not_attempted"]), ("stopped", "run_started", 1, 2))
        self.assertEqual(data["failed"]["code"], "run_in_progress")
        self.assertEqual(data["failed"]["item_id"], sorted(self.conflicts)[1])
        self.assertEqual(len(self.items("field_conflict")), 2)

    def test_the_first_error_stops_it(self) -> None:
        ids = sorted(self.conflicts)
        self.resolved_ok(ids[1], "keep_current")  # no longer open
        data = self.start_ok({"item_ids": ids, "choice": "accept_airtable"})
        self.assertEqual((data["status"], data["done"], data["not_attempted"]), ("failed", 1, 1))
        self.assertEqual((data["failed"]["item_id"], data["failed"]["code"]), (ids[1], "item_not_open"))
        self.assertEqual(len(self.items("field_conflict")), 1)  # the last one, not attempted

    def test_an_interrupted_row_keeps_its_status(self) -> None:
        with self.after_item(1, lambda job_id: self.write_job(job_id, status="interrupted")):
            data = self.start_ok()
        # Marked before the job wrote its count: the row stays as the
        # interrupter left it; the item answered meanwhile is kept.
        self.assertEqual((data["status"], data["done"]), ("interrupted", 0))
        self.assertEqual(len(self.items("field_conflict")), 2)  # it stopped

    def test_an_unexpected_error_fails_the_job(self) -> None:
        with mock.patch.object(resolutions, "resolve_item", side_effect=RuntimeError("boom")):
            data = self.start_ok()
        self.assertEqual((data["status"], data["done"], data["not_attempted"]), ("failed", 0, 2))
        self.assertIn("Unexpected error (RuntimeError)", data["error"])
        self.assertEqual(len(self.items("field_conflict")), 3)

    def test_an_executor_that_fails_to_start(self) -> None:
        def broken() -> BulkJobRunner:
            return BulkJobRunner(session_factory=self.Session, executor=mock.Mock(side_effect=RuntimeError("no thread")), heartbeat_interval=None)

        self.client.app.dependency_overrides[get_bulk_job_runner] = broken
        data = self.start_ok()
        self.assertEqual((data["status"], data["not_attempted"], data["error"]), ("failed", 3, "The job could not be started."))
        self.assertIsNone(self.status()["current_bulk_job"])


class TestAccessAndShape(JobFixture):
    def test_admins_only(self) -> None:
        self.assertEqual(self.start(email=MEMBER_EMAIL).status_code, 403)
        data = self.start_ok()
        self.assertEqual(self.client.get(f"/v2/airtable-sync/bulk-jobs/{data['id']}", headers=self.auth(MEMBER_EMAIL)).status_code, 403)
        self.assertEqual(self.client.post(f"/v2/airtable-sync/bulk-jobs/{data['id']}/stop", headers=self.auth(MEMBER_EMAIL)).status_code, 403)
        self.assertIn(self.client.post("/v2/airtable-sync/bulk-jobs", json={"run_id": 1, "choice": "skip"}).status_code, (401, 403))

    def test_request_shape(self) -> None:
        for body in (
            {"choice": "keep_current"},
            {"item_ids": [1], "run_id": 1, "choice": "keep_current"},
            {"item_ids": [1], "kind": "x", "choice": "keep_current"},
            {"run_id": 1, "choice": "keep_current", "extra": 1},
            {"run_id": 1, "choice": "keep_current", "label": "x" * 256},
        ):
            self.assertEqual(self.start(body).status_code, 422, body)
        self.assertEqual(self.db.query(AirtableSyncBulkJobV2).count(), 0)


class TestHeartbeat(JobFixture):
    def test_the_heartbeat_moves_a_running_job_only(self) -> None:
        old = _now() - timedelta(minutes=10)
        self.db.add(AirtableSyncBulkJobV2(choice="keep_current", item_ids=[], total=0, status="running", started_at=old, heartbeat_at=old))
        self.db.add(AirtableSyncBulkJobV2(choice="keep_current", item_ids=[], total=0, status="succeeded", started_at=old, heartbeat_at=old))
        self.db.commit()
        live, ended = self.db.query(AirtableSyncBulkJobV2).order_by(AirtableSyncBulkJobV2.id).all()
        for job in (live, ended):
            _Heartbeat(self.Session, job.id, None, model=AirtableSyncBulkJobV2).beat()
        self.assertFalse(bulk_jobs.is_stale(self.job(live.id)))
        self.assertLess(bulk_jobs.liveness(self.job(ended.id)), _now() - timedelta(minutes=9))


if __name__ == "__main__":
    unittest.main()
