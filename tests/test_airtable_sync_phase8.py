"""Airtable sync, phase 8 (AI Docs/plan_airtable_sync_v2_2026-10-21.md §12):
the one backend change the frontend tab needed (owner, 2026-10-24): a running
run's row carries its progress, so `GET /runs/{id}` can show the current step.

  - at the start of each step the row's `summary` holds `current_step`, the
    snapshot's `read` counts and the earlier steps' counts
  - previews too (the write goes through its own session, not the run's
    guarded one)
  - only a row still `running` is touched: one marked `interrupted` meanwhile
    keeps its status and summary
  - advisory: a failed progress write never fails the run
  - the final summary is unchanged: no `current_step`; `failed_step` on a
    crash

In-memory SQLite, stub steps that read the run row from a fresh session while
they run; Airtable is phase 2's fake source.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-sync-phase8-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


import app.main  # noqa: E402,F401  (registers every model)
from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.airtable_sync import AirtableSyncRunV2  # noqa: E402
from app.services.airtable_sync import runner as runner_module  # noqa: E402
from app.services.airtable_sync.runner import SyncRunner  # noqa: E402
from tests.test_airtable_sync_phase2 import SETTINGS, T_USERS, FakeSource, airtable_tables  # noqa: E402

ALL_STEPS = ("clusters", "programs", "users", "program_roles")


class Phase8Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.seen: dict[str, tuple[str, dict | None]] = {}  # step → (status, summary) read mid-step
        self.fail_step: str | None = None
        self.during: dict[str, callable] = {}  # step → extra action while it runs
        self.notifier = mock.Mock()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _stub(self, step: str):
        def run(ctx) -> dict:
            if step in self.during:
                self.during[step](ctx)
            row = self.run_row(ctx.run_id)
            self.seen[step] = (row.status, row.summary)
            if self.fail_step == step:
                raise RuntimeError("boom")
            return {"records": len(step), "created": 1}

        return run

    def runner(self) -> SyncRunner:
        return SyncRunner(
            session_factory=self.Session,
            executor=lambda fn: fn(),
            source_factory=lambda _config: FakeSource(airtable_tables()),
            settings_provider=lambda: SimpleNamespace(**SETTINGS),
            step_functions={s: self._stub(s) for s in ALL_STEPS},
            heartbeat_interval=None,
            notifier=self.notifier,
        )

    def run_row(self, run_id: int) -> AirtableSyncRunV2:
        db = self.Session()
        try:
            return db.get(AirtableSyncRunV2, run_id)
        finally:
            db.close()

    def run_sync(self, *, preview: bool, steps=ALL_STEPS) -> int:
        runner = self.runner()
        db = self.Session()
        try:
            started = runner.start(db, steps=steps, preview=preview, started_by_email="ada@renphil.org")
            db.commit()
        finally:
            db.close()
        runner.launch(started)
        return started.run_id


class ProgressTests(Phase8Fixture):
    def test_each_step_sees_its_own_progress(self) -> None:
        run_id = self.run_sync(preview=False)
        self.assertEqual(self.run_row(run_id).status, "succeeded")
        for i, step in enumerate(ALL_STEPS):
            status, summary = self.seen[step]
            self.assertEqual(status, "running")
            self.assertEqual(summary["current_step"], step)
            self.assertFalse(summary["preview"])
            # The snapshot is read before the first step.
            self.assertEqual(summary["read"]["users"], len(airtable_tables()[T_USERS]["records"]))
            # The earlier steps' counts are there; this one's and later ones aren't.
            self.assertEqual(sorted(summary["steps"]), sorted(ALL_STEPS[:i]))
            for earlier in ALL_STEPS[:i]:
                self.assertEqual(summary["steps"][earlier], {"records": len(earlier), "created": 1})

    def test_final_summary_has_no_current_step(self) -> None:
        run_id = self.run_sync(preview=False)
        summary = self.run_row(run_id).summary
        self.assertNotIn("current_step", summary)
        self.assertNotIn("failed_step", summary)
        self.assertEqual(sorted(summary["steps"]), sorted(ALL_STEPS))

    def test_preview_progress_is_written_too(self) -> None:
        run_id = self.run_sync(preview=True, steps=("users", "clusters"))
        row = self.run_row(run_id)
        self.assertEqual(row.status, "succeeded", row.error)
        self.assertEqual(self.seen["clusters"][1]["current_step"], "clusters")
        self.assertTrue(self.seen["clusters"][1]["preview"])
        # Steps run in the fixed order whatever the request's order.
        self.assertEqual(self.seen["users"][1]["current_step"], "users")
        self.assertEqual(sorted(self.seen["users"][1]["steps"]), ["clusters"])
        self.assertNotIn("programs", self.seen)

    def test_a_crash_keeps_failed_step(self) -> None:
        self.fail_step = "users"
        run_id = self.run_sync(preview=False)
        row = self.run_row(run_id)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.summary["failed_step"], "users")
        self.assertNotIn("current_step", row.summary)
        self.assertEqual(self.seen["users"][1]["current_step"], "users")

    def test_an_interrupted_row_is_not_touched(self) -> None:
        def interrupt(ctx) -> None:
            db = self.Session()
            try:
                row = db.get(AirtableSyncRunV2, ctx.run_id)
                row.status = "interrupted"
                row.summary = {"marker": True}
                db.commit()
            finally:
                db.close()

        self.during["programs"] = interrupt
        run_id = self.run_sync(preview=False)
        row = self.run_row(run_id)
        # The users step's progress write found no running row: nothing changed.
        self.assertEqual(self.seen["users"], ("interrupted", {"marker": True}))
        self.assertEqual(row.status, "interrupted")
        self.assertEqual(row.summary, {"marker": True})

    def test_a_failed_progress_write_never_fails_the_run(self) -> None:
        real = SyncRunner._progress
        calls = []

        def broken(self_, run_id, summary):
            calls.append(summary.get("current_step"))
            with mock.patch.object(runner_module, "update", side_effect=RuntimeError("db down")):
                real(self_, run_id, summary)

        with mock.patch.object(SyncRunner, "_progress", broken):
            run_id = self.run_sync(preview=False)
        self.assertEqual(calls, list(ALL_STEPS))
        row = self.run_row(run_id)
        self.assertEqual(row.status, "succeeded")
        self.assertEqual(sorted(row.summary["steps"]), sorted(ALL_STEPS))
        # Nothing was written mid-run.
        self.assertIsNone(self.seen["clusters"][1])


if __name__ == "__main__":
    unittest.main()
