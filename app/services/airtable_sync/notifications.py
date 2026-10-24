"""The sync's bell notifications (v1 plan §9.1, C3; plan v2 §12 phase 7).

  run_started    after the run row is committed (`SyncRunner.launch`): every
                 Hub Admin, minus the starter on a manual run
                 ("Manual Airtable sync started by <admin>")
  run_finished   after the run row is closed (`SyncRunner._finish`): every
                 Hub Admin. The excerpt is the summary line ("Airtable sync
                 finished: 12 updated, 3 created, 5 conflicts, 4 warnings") or
                 "Airtable sync failed: <short error>"

Previews send none (C3), and neither do item resolutions. A start that 409s
never reaches `launch`, so it notifies nobody.

Recipients come from `rbac_graph_service.hub_admin_user_ids`, the one seam
for "who is an admin" (it already skips soft-deleted users, v1 §11), not v1
§9.1's `edit_lock_service._admin_holder_emails`, which answers a different
question (which of these lock holders is an admin) on top of the same seam.

Both are ADVISORY, like the lock sweep: each reads the committed run row from
its own session, and any error is logged and swallowed, so a notification
can never fail, delay or roll back a run.

The rows are ordinary `notifications` rows with no thread and no component
(both NULL) and `link` set; the bell lists them through the same endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db_v2.models.airtable_sync import AirtableSyncItemV2, AirtableSyncRunV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.user import UserV2
from app.services.thread_service import (
    NOTIFICATION_TYPE_AIRTABLE_SYNC_FINISHED,
    NOTIFICATION_TYPE_AIRTABLE_SYNC_STARTED,
)

logger = logging.getLogger(__name__)

# The Airtable Sync tab (v1 plan §10; built in phase 8).
LINK = "/admin/access-control/airtable-sync"
EXCERPT_MAX = 200  # notifications.excerpt is String(200)

# Item kinds counted as "conflicts" in the summary line: both sides changed
# (v1 M1). Every other open item is a "warning".
CONFLICT_KINDS = frozenset({"field_conflict", "assignment_conflict", "scope_name_conflict"})

# summary.steps counts that make "created" and "updated". Per step, zero
# counts are omitted, so a missing key is 0.
CREATED_COUNTS = ("created",)
UPDATED_COUNTS = (
    "renamed",  # clusters / programs
    "edges_added",  # programs
    "fields_updated",  # users (one per field)
    "defaults_assigned",  # users (no_program_roles → Assign default)
    "replaced",  # program roles
    "adopted",  # program roles
    "removed",  # program roles (a remembered Remove)
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: str, limit: int = EXCERPT_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def admin_recipients(db: Session, *, exclude_user_id: int | None = None) -> list[str]:
    """The login email of every Hub Admin (soft-deleted ones are never in
    the set), minus `exclude_user_id`. Normalized and de-duplicated, like
    the thread notifications' `recipient_email`."""
    from app.services.rbac_graph_service import hub_admin_user_ids

    ids = hub_admin_user_ids(db) - {exclude_user_id}
    if not ids:
        return []
    rows = db.query(UserV2.work_email, UserV2.alias_email).filter(UserV2.id.in_(ids), UserV2.not_deleted()).all()
    emails = {(work or "").strip().lower() or (alias or "").strip().lower() for work, alias in rows}
    return sorted(e for e in emails if e)


def _starter(db: Session, run: AirtableSyncRunV2) -> tuple[str | None, str | None]:
    """(actor_email, actor_name): the starter, None for a scheduled run."""
    if run.trigger == "scheduled":
        return None, None
    name = None
    if run.started_by_user_id is not None:
        row = db.query(UserV2.name).filter(UserV2.id == run.started_by_user_id).first()
        if row is not None:
            name = (row[0] or "").strip() or None
    return run.started_by_email, name


def started_line(run: AirtableSyncRunV2, actor_name: str | None) -> str:
    if run.trigger == "scheduled":
        return "Scheduled Airtable sync started"
    return _clip(f"Manual Airtable sync started by {actor_name or run.started_by_email or 'an admin'}")


def _step_total(summary: dict | None, keys: tuple[str, ...]) -> int:
    steps = (summary or {}).get("steps") or {}
    return sum(int(counts.get(k) or 0) for counts in steps.values() if isinstance(counts, dict) for k in keys)


def _open_items(db: Session, run_id: int) -> tuple[int, int]:
    """(conflicts, warnings): the open items this run raised or refreshed."""
    kinds = (
        db.query(AirtableSyncItemV2.kind)
        .filter(AirtableSyncItemV2.status == "open", AirtableSyncItemV2.last_run_id == run_id)
        .all()
    )
    conflicts = sum(1 for (kind,) in kinds if kind in CONFLICT_KINDS)
    return conflicts, len(kinds) - conflicts


def finished_line(db: Session, run: AirtableSyncRunV2) -> str:
    """v1 §5.1 step 4. A failure (or an interruption) is a `finished` with
    the failure text; the run's `error` is already short and secret-free."""
    if run.status != "succeeded":
        return _clip(f"Airtable sync failed: {run.error or run.status}")
    conflicts, warnings = _open_items(db, run.id)
    return _clip(
        f"Airtable sync finished: {_step_total(run.summary, UPDATED_COUNTS)} updated, "
        f"{_step_total(run.summary, CREATED_COUNTS)} created, {conflicts} conflicts, {warnings} warnings"
    )


def _write(
    db: Session, kind: str, recipients: list[str], *, excerpt: str, actor: tuple[str | None, str | None]
) -> int:
    now = _utc_now()
    for email in recipients:
        db.add(
            NotificationV2(
                recipient_email=email,
                type=kind,
                actor_email=actor[0],
                actor_name=actor[1],
                component_id=None,
                thread_id=None,
                excerpt=excerpt,
                link=LINK,
                created_at=now,
            )
        )
    db.commit()
    return len(recipients)


def _advisory(what: str, session_factory: Callable[[], Session], run_id: int, body: Callable[[Session, AirtableSyncRunV2], int]) -> int | None:
    """Run `body` on the committed run row in a fresh session. Returns the
    number of notifications written, 0 for a preview, None on an error
    (logged, never raised)."""
    db = session_factory()
    try:
        run = db.query(AirtableSyncRunV2).filter(AirtableSyncRunV2.id == run_id).first()
        if run is None or run.trigger == "preview":
            return 0
        return body(db, run)
    except Exception:
        logger.exception("Airtable sync run %s: could not send the %s notifications", run_id, what)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def run_started(session_factory: Callable[[], Session], run_id: int) -> int | None:
    def body(db: Session, run: AirtableSyncRunV2) -> int:
        actor = _starter(db, run)
        exclude = run.started_by_user_id if run.trigger == "manual" else None
        return _write(
            db,
            NOTIFICATION_TYPE_AIRTABLE_SYNC_STARTED,
            admin_recipients(db, exclude_user_id=exclude),
            excerpt=started_line(run, actor[1]),
            actor=actor,
        )

    return _advisory("start", session_factory, run_id, body)


def run_finished(session_factory: Callable[[], Session], run_id: int) -> int | None:
    def body(db: Session, run: AirtableSyncRunV2) -> int:
        return _write(
            db,
            NOTIFICATION_TYPE_AIRTABLE_SYNC_FINISHED,
            admin_recipients(db),
            excerpt=finished_line(db, run),
            actor=_starter(db, run),
        )

    return _advisory("end", session_factory, run_id, body)


class SyncNotifier:
    """What the runner calls; tests replace it to count or to fail."""

    def started(self, session_factory: Callable[[], Session], run_id: int) -> Any:
        return run_started(session_factory, run_id)

    def finished(self, session_factory: Callable[[], Session], run_id: int) -> Any:
        return run_finished(session_factory, run_id)
