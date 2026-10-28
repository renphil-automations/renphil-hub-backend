"""Background bulk resolves (owner, 2026-10-27).

"Accept all from Airtable" / "Keep all" used to be one long request
(`resolutions.bulk_resolve`): the tab forgot it as soon as the admin left,
and a second click raced the first ("This item is no longer open"). A job is
the same loop, run in a thread, with its progress on a row the tab polls:

  start_job    take the sync lock, refuse while a run or another job is
               live, fix the item list, insert the `running` job row — in
               the caller's transaction; the caller commits, then `launch`es
  execute      `resolve_item` per item, one transaction each, in id order,
               stopping at the first error (v1 §5.4, unchanged); `done`,
               the last item and the heartbeat move after every item
  request_stop the admin's Stop: honoured before the next item

While a job is live every other answer is refused with 409
`bulk_in_progress` (`refuse_if_busy`, used by `resolve_item` and the
synchronous bulk route); the job's own items pass. A run may still start
(owner's choice): the job's next item then meets the run and the job stops
(`stop_reason = "run_started"`). A job 5 minutes without a heartbeat is
taken as dead, exactly like a run (runner.STALE_AFTER).

Nothing here notifies anyone; resolutions never did.

Everything from outside (session factory, executor, heartbeat interval) is
injectable, so the tests run a job inline against SQLite.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import AirtableSyncBulkJobV2
from app.services.airtable_sync.lock_sweep import release_unauthorized_locks
from app.services.airtable_sync.runner import (
    HEARTBEAT_INTERVAL,
    STALE_AFTER,
    SyncRunInProgress,
    _Heartbeat,
    as_utc,
    refuse_if_running,
)
from app.services.airtable_sync.settings_service import AirtableSyncError

if TYPE_CHECKING:
    from app.dependencies import CurrentHubUser

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = (
    "No heartbeat for 5 minutes: the job's process probably stopped (e.g. a restart). "
    "Marked interrupted when the next job started."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BulkJobInProgress(AirtableSyncError):
    """An answer (or a second job) while a bulk job is live (409)."""


# ── the lock ─────────────────────────────────────────────────────────────


def liveness(job: AirtableSyncBulkJobV2) -> datetime:
    return as_utc(job.heartbeat_at) or as_utc(job.started_at)


def is_stale(job: AirtableSyncBulkJobV2, now: datetime | None = None) -> bool:
    return job.status == "running" and (now or _utc_now()) - liveness(job) >= STALE_AFTER


def running_job(db: Session) -> AirtableSyncBulkJobV2 | None:
    return db.query(AirtableSyncBulkJobV2).filter(AirtableSyncBulkJobV2.status == "running").first()


def _in_progress(job: AirtableSyncBulkJobV2 | None) -> BulkJobInProgress:
    if job is None:  # it ended between the failed insert and this read
        return BulkJobInProgress("bulk_in_progress", "Another bulk answer has just started.")
    started_at = as_utc(job.started_at)
    return BulkJobInProgress(
        "bulk_in_progress",
        f"A bulk answer is in progress ({job.label or job.choice}: {job.done} of {job.total}, started by "
        f"{job.started_by_email or 'an admin'} at {started_at.isoformat(timespec='seconds')}). "
        "Answering is paused until it ends.",
        job_id=job.id,
        started_by_email=job.started_by_email,
        started_at=started_at.isoformat(),
    )


def refuse_if_busy(db: Session, *, own_job_id: int | None = None) -> None:
    """`runner.refuse_if_running` (takes the sync lock; 409 during a live
    run), then 409 `bulk_in_progress` while a live job other than
    `own_job_id` exists. A stale job doesn't block."""
    refuse_if_running(db)
    job = running_job(db)
    if job is not None and job.id != own_job_id and not is_stale(job):
        raise _in_progress(job)


# ── reads ────────────────────────────────────────────────────────────────


def serialize_job(job: AirtableSyncBulkJobV2, now: datetime | None = None) -> dict:
    return {
        "id": job.id,
        "choice": job.choice,
        "label": job.label,
        "target": job.target,
        "status": job.status,
        "total": job.total,
        "done": job.done,
        "last_item_id": job.last_item_id,
        "last_subject": job.last_subject,
        "failed": job.failed,
        "not_attempted": job.not_attempted,
        "stop_requested": job.stop_requested_by_email is not None,
        "stop_requested_by_email": job.stop_requested_by_email,
        "stop_reason": job.stop_reason,
        "error": job.error,
        "started_by_email": job.started_by_email,
        "started_at": as_utc(job.started_at),
        "finished_at": as_utc(job.finished_at),
        "heartbeat_at": as_utc(job.heartbeat_at),
        "stale": is_stale(job, now),
    }


def get_job(db: Session, job_id: int) -> AirtableSyncBulkJobV2 | None:
    return db.get(AirtableSyncBulkJobV2, job_id)


def last_finished_job(db: Session) -> AirtableSyncBulkJobV2 | None:
    return (
        db.query(AirtableSyncBulkJobV2)
        .filter(AirtableSyncBulkJobV2.status != "running")
        .order_by(AirtableSyncBulkJobV2.started_at.desc(), AirtableSyncBulkJobV2.id.desc())
        .first()
    )


# ── start / stop ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StartedJob:
    job_id: int
    item_ids: tuple[int, ...]
    choice: str
    current: Any  # CurrentHubUser: the actor every answer is logged under


def start_job(
    db: Session,
    *,
    item_ids: list[int] | None,
    run_id: int | None,
    kind: str | None,
    choice: str,
    label: str | None,
    current: CurrentHubUser,
) -> StartedJob:
    """Insert the `running` job row in `db`'s transaction (flush, no commit;
    the caller commits, THEN calls `launch`). 409 `run_in_progress` during a
    live run, `bulk_in_progress` while another job is live; a stale job is
    first marked `interrupted`. The items are fixed here, exactly as
    `bulk_resolve` picks them."""
    from app.services.airtable_sync.resolutions import bulk_targets  # resolutions imports this module

    now = _utc_now()
    refuse_if_running(db)  # the sync lock, held to the commit: a second start waits here
    current_job = running_job(db)
    if current_job is not None:
        if not is_stale(current_job, now):
            raise _in_progress(current_job)
        logger.warning("Airtable sync bulk job %s: no heartbeat since %s, marking it interrupted", current_job.id, liveness(current_job))
        current_job.status = "interrupted"
        current_job.finished_at = now
        current_job.error = INTERRUPTED_ERROR
        current_job.not_attempted = max(current_job.total - current_job.done, 0)
        db.flush()  # free the index before the insert below

    ids = bulk_targets(db, item_ids=item_ids, run_id=run_id, kind=kind, choice=choice)
    job = AirtableSyncBulkJobV2(
        choice=choice,
        target={"item_ids": True} if item_ids is not None else {"run_id": run_id, "kind": kind},
        label=(label or "").strip()[:255] or None,
        item_ids=ids,
        total=len(ids),
        done=0,
        status="running",
        started_by_user_id=current.hub_user_id,
        started_by_email=(current.email or "").strip().lower() or None,
        started_at=now,
        heartbeat_at=now,
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _in_progress(running_job(db)) from None
    return StartedJob(job_id=job.id, item_ids=tuple(ids), choice=choice, current=current)


def request_stop(db: Session, job_id: int, *, actor_email: str | None) -> AirtableSyncBulkJobV2 | None:
    """Mark a live job to stop before its next item (flush; the caller
    commits). None when there is no such job; 409 when it isn't running."""
    job = db.query(AirtableSyncBulkJobV2).filter(AirtableSyncBulkJobV2.id == job_id).with_for_update().one_or_none()
    if job is None:
        return None
    if job.status != "running":
        raise AirtableSyncError("bulk_job_not_running", "This bulk answer has already ended.", status=job.status)
    if job.stop_requested_by_email is None:
        job.stop_requested_by_email = (actor_email or "").strip().lower() or "an admin"
        db.flush()
    return job


# ── the job body ─────────────────────────────────────────────────────────


def _thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="airtable-sync-bulk-job", daemon=True).start()


@dataclass
class _Outcome:
    status: str = "succeeded"
    failed: dict | None = None
    not_attempted: int = 0
    stop_reason: str | None = None
    error: str | None = None


class BulkJobRunner:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        executor: Callable[[Callable[[], None]], None] | None = None,
        heartbeat_interval: float | None = HEARTBEAT_INTERVAL,
    ) -> None:
        if session_factory is None:
            from app.db_v2.database import SessionLocalV2

            session_factory = SessionLocalV2
        self.session_factory = session_factory
        self.executor = executor or _thread_executor
        self.heartbeat_interval = heartbeat_interval

    def launch(self, started: StartedJob) -> None:
        """Call after the caller committed `start_job`'s row."""
        try:
            self.executor(lambda: self.execute(started))
        except Exception:
            logger.exception("Airtable sync bulk job %s: could not start its thread", started.job_id)
            self._finish(started.job_id, _Outcome("failed", not_attempted=len(started.item_ids), error="The job could not be started."))

    def execute(self, started: StartedJob) -> None:
        """The job body. Never raises: every outcome ends in `_finish`
        (unless the row is no longer `running`: a later start marked it
        interrupted, and it keeps that)."""
        from app.services.airtable_sync import resolutions  # resolutions imports this module

        ids = started.item_ids
        outcome: _Outcome | None = _Outcome()
        done = 0
        heartbeat = _Heartbeat(self.session_factory, started.job_id, self.heartbeat_interval, model=AirtableSyncBulkJobV2)
        heartbeat.start()
        db = self.session_factory()
        try:
            for n, item_id in enumerate(ids):
                job = db.get(AirtableSyncBulkJobV2, started.job_id)
                if job is None or job.status != "running":
                    outcome = None
                    break
                if job.stop_requested_by_email is not None:
                    outcome = _Outcome("stopped", not_attempted=len(ids) - n, stop_reason="stop_requested")
                    break
                db.rollback()  # end the read: each item is its own transaction
                post = resolutions.PostCommit()
                try:
                    out = resolutions.resolve_item(
                        db, item_id, choice=started.choice, data=None, current=started.current,
                        post_commit=post, bulk_job_id=started.job_id,
                    )
                    if out is None:
                        raise AirtableSyncError("item_not_found", "No such item.", item_id=item_id)
                except SyncRunInProgress as exc:
                    db.rollback()
                    failed = {"item_id": item_id, "code": exc.code, "message": exc.message, **exc.details}
                    outcome = _Outcome("stopped", failed=failed, not_attempted=len(ids) - n, stop_reason="run_started")
                    break
                except AirtableSyncError as exc:
                    db.rollback()
                    failed = {"item_id": item_id, "code": exc.code, "message": exc.message, **exc.details}
                    outcome = _Outcome("failed", failed=failed, not_attempted=len(ids) - n - 1)
                    break
                db.commit()
                post.run(db, sweep=False)
                done += 1
                if not self._progress(db, started.job_id, done, item_id, out["item"].get("subject")):
                    outcome = None
                    break
            if done:
                release_unauthorized_locks(db)  # once, after the loop, as bulk_resolve does
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                logger.warning("Airtable sync bulk job %s: rollback also failed", started.job_id, exc_info=True)
            logger.exception("Airtable sync bulk job %s failed", started.job_id)
            outcome = _Outcome(
                "failed",
                not_attempted=max(len(ids) - done - 1, 0),
                error=f"Unexpected error ({type(exc).__name__}); details are in the server log.",
            )
        finally:
            db.close()
            heartbeat.stop()
            if outcome is not None:
                self._finish(started.job_id, outcome)

    def _progress(self, db: Session, job_id: int, done: int, item_id: int, subject: Any) -> bool:
        """After each committed item: the count, the last item, a heartbeat.
        False when the row is no longer `running` (the job then stops)."""
        result = db.execute(
            update(AirtableSyncBulkJobV2)
            .where(AirtableSyncBulkJobV2.id == job_id, AirtableSyncBulkJobV2.status == "running")
            .values(
                done=done,
                last_item_id=item_id,
                last_subject=str(subject)[:512] if subject else None,
                heartbeat_at=_utc_now(),
            )
        )
        db.commit()
        return result.rowcount > 0

    def _finish(self, job_id: int, outcome: _Outcome) -> None:
        """Close the row from a fresh session; only a row still `running`."""
        now = _utc_now()
        db = self.session_factory()
        try:
            result = db.execute(
                update(AirtableSyncBulkJobV2)
                .where(AirtableSyncBulkJobV2.id == job_id, AirtableSyncBulkJobV2.status == "running")
                .values(
                    status=outcome.status,
                    failed=outcome.failed,
                    not_attempted=outcome.not_attempted,
                    stop_reason=outcome.stop_reason,
                    error=outcome.error,
                    finished_at=now,
                    heartbeat_at=now,
                )
            )
            db.commit()
            if result.rowcount == 0:
                logger.warning("Airtable sync bulk job %s ended as %s, but its row was no longer running", job_id, outcome.status)
        except Exception:
            db.rollback()
            logger.exception("Airtable sync bulk job %s: could not record its end (%s)", job_id, outcome.status)
        finally:
            db.close()


_default_runner: BulkJobRunner | None = None


def get_bulk_job_runner() -> BulkJobRunner:
    """FastAPI dependency; tests override it with an inline runner."""
    global _default_runner
    if _default_runner is None:
        _default_runner = BulkJobRunner()
    return _default_runner
