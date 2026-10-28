"""The sync's run machinery (v1 plan §5.1; plan v2 R1–R3, §8).

  start   take the lock (insert the `running` run row) — in the caller's
          transaction; the caller commits, then calls `launch`
  launch  hand the run to the executor: a background thread in production
  execute read all four Airtable tables (fail fast on a missing field), run
          the ticked steps in the fixed order, record summary/status; the
          row's summary is also published as each step starts
          (`current_step`, `_progress`), for the tab's progress line

A real run notifies every Hub Admin when it starts (in `launch`, so only
after the caller's commit: a 409 notifies nobody) and when its row is closed
(`_finish`); previews notify nobody (notifications.py, v1 C3).

The lock is the `running` row itself (owner's choice, 2026-10-22; see
AirtableSyncRunV2): at most one row may be `running`, enforced by the partial
unique index `uq_airtable_sync_runs_running`. Every kind of run takes it —
manual, scheduled and preview (v1 C1). Item resolutions can't insert a run
row, so they and `start` share a transaction-scoped advisory lock
(`take_sync_lock`): a resolve refuses while a live run exists, and a start
waits for a resolve in progress (`refuse_if_running`).
A `running` row whose heartbeat is STALE_AFTER old is marked `interrupted` by
the next start, so a restart that killed the thread never wedges the sync.

Everything a run needs from outside (session factory, executor, Airtable
source, settings, step functions, heartbeat interval) is injectable, so the
tests run a whole run synchronously against SQLite and a fake Airtable.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import event, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.models.airtable_sync import (
    ENTITIES,
    RUN_STEPS,
    AirtableSyncLogV2,
    AirtableSyncRunV2,
)
from app.services.airtable_sync import airtable_reader
from app.services.airtable_sync.airtable_reader import (
    AirtableReadError,
    AirtableSource,
    AirtableSyncConfigError,
    Snapshot,
    SyncConfig,
)
from app.services.airtable_sync.lock_sweep import ACCESS_REMOVED, release_unauthorized_locks
from app.services.airtable_sync.notifications import SyncNotifier
from app.services.airtable_sync.settings_service import AirtableSyncError
from app.services.airtable_sync.steps import STEP_FUNCTIONS

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 15.0  # seconds (v1 plan §5.1)
STALE_AFTER = timedelta(minutes=5)
# Transaction-scoped advisory lock serializing a run's START with item
# resolutions (v1 C1): "ATSYNC". Held only for the start's / the resolve's
# own transaction, so it works through the transaction-mode pooler.
SYNC_LOCK_KEY = 0x4154_5359_4E43

INTERRUPTED_ERROR = (
    "No heartbeat for 5 minutes: the run's process probably stopped (e.g. a restart). "
    "Marked interrupted when the next run started."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands timezone-aware columns back naive; they were stored as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class SyncRunInProgress(AirtableSyncError):
    """A start while another run holds the lock (409). Phase 9's scheduler
    tick catches this one to consume its slot."""


class PreviewWriteError(RuntimeError):
    """A preview step tried to write something other than its log."""


def order_steps(steps: Iterable[str]) -> tuple[str, ...]:
    """The ticked steps in the fixed run order, whatever order they were
    given in; repeats collapse. Empty or unknown → ValueError (the API
    rejects both with a 422 before getting here)."""
    wanted = set(steps)
    unknown = wanted - set(RUN_STEPS)
    if unknown:
        raise ValueError(f"Unknown step(s): {', '.join(sorted(unknown))}")
    if not wanted:
        raise ValueError("Tick at least one step.")
    return tuple(s for s in RUN_STEPS if s in wanted)


def liveness(run: AirtableSyncRunV2) -> datetime:
    """When the run last showed signs of life."""
    return as_utc(run.heartbeat_at) or as_utc(run.started_at)


def is_stale(run: AirtableSyncRunV2, now: datetime | None = None) -> bool:
    return run.status == "running" and (now or _utc_now()) - liveness(run) >= STALE_AFTER


def running_run(db: Session) -> AirtableSyncRunV2 | None:
    return db.query(AirtableSyncRunV2).filter(AirtableSyncRunV2.status == "running").first()


def take_sync_lock(db: Session) -> None:
    """Serialize run starts and item resolutions for the rest of `db`'s
    transaction. A resolve takes it, then refuses if a live run exists; a
    start takes it before looking for one. So a start arriving mid-resolve
    waits for the resolve to commit, and a resolve arriving once a run's
    row is committed sees it and answers 409. No-op on SQLite (the tests
    are single-threaded)."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SYNC_LOCK_KEY})


def refuse_if_running(db: Session) -> None:
    """Take the lock, then 409 (`SyncRunInProgress`) if a live run holds the
    run lock. A stale `running` row (no heartbeat for 5 minutes) does not
    block: its thread is gone, as `start` also assumes."""
    take_sync_lock(db)
    current = running_run(db)
    if current is not None and not is_stale(current):
        raise _in_progress(current)


def _in_progress(run: AirtableSyncRunV2 | None) -> SyncRunInProgress:
    if run is None:  # it finished between the failed insert and this read
        return SyncRunInProgress("run_in_progress", "Another Airtable sync run has just started.")
    started_at = as_utc(run.started_at)
    who = run.started_by_email or "the scheduler"
    return SyncRunInProgress(
        "run_in_progress",
        f"An Airtable sync run is already in progress (run {run.id}, {run.trigger}, started by {who} "
        f"at {started_at.isoformat(timespec='seconds')}).",
        run_id=run.id,
        trigger=run.trigger,
        started_by_email=run.started_by_email,
        started_at=started_at.isoformat(),
    )


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return str(value)
    return json.dumps(value, default=str, sort_keys=True)


@dataclass(frozen=True)
class StartedRun:
    """What `start` hands to `launch`: enough to execute without re-reading."""

    run_id: int
    steps: tuple[str, ...]
    dry_run: bool


@dataclass
class RunContext:
    """What every step receives (see steps.py for the contract)."""

    db: Session
    run_id: int
    dry_run: bool
    steps: tuple[str, ...]
    snapshot: Snapshot
    config: SyncConfig
    # What one step hands a later step of the same run. A preview writes no
    # scopes, so the Clusters step leaves its would-be cluster scopes here
    # for the Programs step's edges (scopes_step.VIRTUAL_SCOPES).
    state: dict[str, Any] = field(default_factory=dict)

    def log(
        self,
        entity: str,
        action: str,
        *,
        airtable_record_id: str | None = None,
        hub_id: int | None = None,
        subject: str | None = None,
        field: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
    ) -> AirtableSyncLogV2:
        """Add one log row to the run (flushed with the step's next commit).
        In a preview this is the ONLY kind of write allowed. Values that are
        not text are stored as JSON."""
        if entity not in ENTITIES:
            raise ValueError(f"Log entity must be one of {', '.join(ENTITIES)}; run-level messages go in the summary.")
        row = AirtableSyncLogV2(
            run_id=self.run_id,
            entity=entity,
            action=action,
            airtable_record_id=airtable_record_id,
            hub_id=hub_id,
            subject=subject,
            field=field,
            old_value=_as_text(old_value),
            new_value=_as_text(new_value),
        )
        self.db.add(row)
        return row

    def commit(self) -> None:
        self.db.commit()


# Models a preview may write. Anything else in a flush fails the run.
_PREVIEW_WRITABLE = (AirtableSyncLogV2, AirtableSyncRunV2)


def _install_preview_guard(db: Session) -> None:
    """Fail any flush that would write more than log rows. It catches ORM
    writes (including those made by the rbac services, which flush); a step
    issuing raw SQL is on its own, so steps must not, in a preview."""

    @event.listens_for(db, "before_flush")
    def _guard(session: Session, _flush_context, _instances) -> None:
        dirty = [o for o in session.dirty if session.is_modified(o)]
        for obj in (*session.new, *dirty, *session.deleted):
            if not isinstance(obj, _PREVIEW_WRITABLE):
                raise PreviewWriteError(f"A preview tried to write {type(obj).__name__}; previews write only their log.")


def _quiet_rollback(db: Session) -> None:
    """Roll back after a failed run; a broken connection must not stop the
    run row from being closed."""
    try:
        db.rollback()
    except Exception:
        logger.warning("Airtable sync: rollback after a failed run also failed", exc_info=True)


def _thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="airtable-sync-run", daemon=True).start()


class _Heartbeat:
    """Bumps the run's `heartbeat_at` every `interval` seconds from its own
    thread and its own short sessions, so a long Airtable read or step keeps
    the run alive. Only touches a row that is still `running`. `model` is the
    row's table: runs, or a bulk job (bulk_jobs.py), which has the same
    `id` / `status` / `heartbeat_at` columns."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        run_id: int,
        interval: float | None,
        model: type = AirtableSyncRunV2,
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id
        self._interval = interval
        self._model = model
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._interval:
            return
        self._thread = threading.Thread(target=self._loop, name=f"airtable-sync-heartbeat-{self._run_id}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.beat()
            except Exception:  # a missed beat only matters after 5 minutes of them
                logger.warning("Airtable sync run %s: heartbeat failed", self._run_id, exc_info=True)

    def beat(self) -> None:
        db = self._session_factory()
        try:
            db.execute(
                update(self._model)
                .where(self._model.id == self._run_id, self._model.status == "running")
                .values(heartbeat_at=_utc_now())
            )
            db.commit()
        finally:
            db.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class SyncRunner:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        executor: Callable[[Callable[[], None]], None] | None = None,
        source_factory: Callable[[SyncConfig], AirtableSource] | None = None,
        settings_provider: Callable[[], Any] | None = None,
        step_functions: dict[str, Callable[[RunContext], dict]] | None = None,
        heartbeat_interval: float | None = HEARTBEAT_INTERVAL,
        notifier: SyncNotifier | None = None,
    ) -> None:
        if session_factory is None:
            from app.db_v2.database import SessionLocalV2

            session_factory = SessionLocalV2
        self.session_factory = session_factory
        self.executor = executor or _thread_executor
        self.source_factory = source_factory or airtable_reader.PyairtableSource
        self.settings_provider = settings_provider or get_settings
        self.step_functions = step_functions or STEP_FUNCTIONS
        self.heartbeat_interval = heartbeat_interval
        self.notifier = notifier or SyncNotifier()

    # ── start: take the lock ────────────────────────────────────────────

    def start(
        self,
        db: Session,
        *,
        steps: Iterable[str],
        preview: bool,
        trigger: str | None = None,
        started_by_user_id: int | None = None,
        started_by_email: str | None = None,
    ) -> StartedRun:
        """Insert the `running` row in `db`'s transaction (flush, no commit).
        A stale `running` row is first marked `interrupted`; a live one →
        SyncRunInProgress. The caller commits, THEN calls `launch`: the run's
        thread uses its own session and must see the committed row.

        On the losing side of a race the insert hits the unique index; this
        rolls `db` back (so call it before anything else in the transaction)."""
        ordered = order_steps(steps)
        trigger = trigger or ("preview" if preview else "manual")
        now = _utc_now()

        take_sync_lock(db)  # waits for a resolution in progress
        current = running_run(db)
        if current is not None:
            if not is_stale(current, now):
                raise _in_progress(current)
            logger.warning("Airtable sync run %s: no heartbeat since %s, marking it interrupted", current.id, liveness(current))
            current.status = "interrupted"
            current.finished_at = now
            current.error = INTERRUPTED_ERROR
            db.flush()  # free the index before the insert below

        run = AirtableSyncRunV2(
            steps=list(ordered),
            trigger=trigger,
            started_by_user_id=started_by_user_id,
            started_by_email=started_by_email,
            status="running",
            started_at=now,
            heartbeat_at=now,
        )
        db.add(run)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise _in_progress(running_run(db)) from None
        return StartedRun(run_id=run.id, steps=ordered, dry_run=preview)

    # ── launch / execute ────────────────────────────────────────────────

    def launch(self, started: StartedRun) -> None:
        """Call after the caller committed `start`'s row. Sends the start
        notifications (advisory), then hands the run to the executor."""
        if not started.dry_run:
            self.notifier.started(self.session_factory, started.run_id)
        try:
            self.executor(lambda: self.execute(started))
        except Exception:
            logger.exception("Airtable sync run %s: could not start its thread", started.run_id)
            self._finish(started.run_id, "failed", {"preview": started.dry_run}, "The run could not be started.")

    def execute(self, started: StartedRun) -> None:
        """The run body. Never raises: every outcome ends in `_finish`."""
        summary: dict[str, Any] = {"preview": started.dry_run, "read": None, "steps": {}}
        status, error = "failed", "The run stopped unexpectedly."
        secret = ""
        heartbeat = _Heartbeat(self.session_factory, started.run_id, self.heartbeat_interval)
        heartbeat.start()
        db = self.session_factory()
        try:
            if started.dry_run:
                _install_preview_guard(db)
            # Settings are read ONCE: the whole run sees one configuration.
            config = airtable_reader.build_config(self.settings_provider())
            secret = config.api_key
            # Snapshot first, fail fast: all four tables read and every
            # required field checked before any step writes anything.
            snapshot = airtable_reader.read_snapshot(self.source_factory(config), config)
            summary["read"] = snapshot.summary()
            ctx = RunContext(
                db=db,
                run_id=started.run_id,
                dry_run=started.dry_run,
                steps=started.steps,
                snapshot=snapshot,
                config=config,
            )
            for step in started.steps:
                summary["current_step"] = step
                self._progress(started.run_id, summary)
                summary["steps"][step] = self.step_functions[step](ctx) or {}
                ctx.commit()
            summary.pop("current_step", None)
            if ctx.state.get(ACCESS_REMOVED) and not started.dry_run:
                # Once per run: whoever lost edit() keeps no lock (lock_sweep.py).
                summary["locks_released"] = release_unauthorized_locks(db)
            status, error = "succeeded", None
        except (AirtableSyncConfigError, AirtableReadError, PreviewWriteError) as exc:
            _quiet_rollback(db)
            status, error = "failed", str(exc)
        except Exception as exc:
            _quiet_rollback(db)
            logger.exception("Airtable sync run %s failed", started.run_id)
            status, error = "failed", f"Unexpected error ({type(exc).__name__}); details are in the server log."
        finally:
            db.close()
            heartbeat.stop()
            if "current_step" in summary:
                summary["failed_step"] = summary.pop("current_step")
            if error and secret:  # belt and braces: no message should carry it
                error = error.replace(secret, "***")
            self._finish(started.run_id, status, summary, error)

    def _progress(self, run_id: int, summary: dict) -> None:
        """Publish the summary so far (`read`, the finished steps' counts,
        `current_step`) on the run row, so `GET /runs/{id}` shows what a
        running run is doing (the Airtable Sync tab; owner, 2026-10-24).
        Same rules as `_finish`: a fresh session (a preview's own is
        guarded), and only a row still `running`. Advisory: a failed write is
        logged and the run carries on; `_finish` writes the final summary."""
        db = self.session_factory()
        try:
            db.execute(
                update(AirtableSyncRunV2)
                .where(AirtableSyncRunV2.id == run_id, AirtableSyncRunV2.status == "running")
                .values(summary=copy.deepcopy(summary))
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("Airtable sync run %s: could not record its progress", run_id, exc_info=True)
        finally:
            db.close()

    def _finish(self, run_id: int, status: str, summary: dict, error: str | None) -> None:
        """Close the run row from a fresh session (the run's own may be
        broken). Only a row still `running` is touched: one already marked
        `interrupted` by a later start keeps that status, and sends no end
        notification. A closed real run notifies every Hub Admin (advisory:
        after the commit, from its own session)."""
        now = _utc_now()
        closed = False
        db = self.session_factory()
        try:
            result = db.execute(
                update(AirtableSyncRunV2)
                .where(AirtableSyncRunV2.id == run_id, AirtableSyncRunV2.status == "running")
                .values(status=status, summary=summary, error=error, finished_at=now, heartbeat_at=now)
            )
            db.commit()
            closed = result.rowcount > 0
            if not closed:
                logger.warning("Airtable sync run %s finished as %s, but its row was no longer running", run_id, status)
        except Exception:
            db.rollback()
            logger.exception("Airtable sync run %s: could not record its end (%s)", run_id, status)
        finally:
            db.close()
        if closed and not summary.get("preview"):
            self.notifier.finished(self.session_factory, run_id)


_default_runner: SyncRunner | None = None


def get_sync_runner() -> SyncRunner:
    """FastAPI dependency; tests override it with an inline runner."""
    global _default_runner
    if _default_runner is None:
        _default_runner = SyncRunner()
    return _default_runner
