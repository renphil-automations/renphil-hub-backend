"""Task Manager notifications — rows in ``notifications`` for a task's owner
and assignees.

Written in the caller's transaction (no commit here), so a task change and
its notifications land together. One row per recipient, keyed by email like
every other notification; ``component_id`` is the task's Task Manager, so the
bell links to it and hides it from anyone without access to that component.
The actor never notifies themselves.

When a notification is sent:

* ``notify_on_create_and_delete`` — creation (each assignee), assignees added
  or removed later (each of them, plus the owner), and deletion of a task that
  isn't Closed (owner + assignees).
* ``notification_cadence`` — ``always``: detail changes and status changes;
  ``on_status_change``: status changes only; ``never``: neither. Changing the
  two settings themselves never notifies.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.task import TaskV2
from app.db_v2.models.user import UserV2

NOTIFICATION_TYPE_TASK_ASSIGNED = "task_assigned"
NOTIFICATION_TYPE_TASK_UNASSIGNED = "task_unassigned"
NOTIFICATION_TYPE_TASK_ASSIGNEES_CHANGED = "task_assignees_changed"
NOTIFICATION_TYPE_TASK_UPDATED = "task_updated"
NOTIFICATION_TYPE_TASK_STATUS_CHANGED = "task_status_changed"
NOTIFICATION_TYPE_TASK_DELETED = "task_deleted"

CADENCE_ALWAYS = "always"
CADENCE_NEVER = "never"
CADENCE_ON_STATUS_CHANGE = "on_status_change"
NOTIFICATION_CADENCES = (CADENCE_ALWAYS, CADENCE_NEVER, CADENCE_ON_STATUS_CHANGE)

CLOSED_STATUS = "Closed"
EXCERPT_MAX = 200  # notifications.excerpt is VARCHAR(200)


def _clip(text: str) -> str:
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 1].rstrip() + "…"


def cadence_of(task: TaskV2) -> str:
    return task.notification_cadence or CADENCE_ALWAYS


class TaskNotifier:
    """Resolves users once and writes the rows for one request."""

    def __init__(self, db: Session, actor_id: int) -> None:
        self.db = db
        self.actor_id = actor_id
        self._users: dict[int, UserV2 | None] = {}
        self.written = 0

    def _user(self, user_id: int) -> UserV2 | None:
        if user_id not in self._users:
            self._users[user_id] = self.db.get(UserV2, user_id)
        return self._users[user_id]

    def _name(self, user_id: int) -> str:
        user = self._user(user_id)
        if user is None:
            return "Someone"
        return (user.name or "").strip() or user.email or "Someone"

    @property
    def actor_name(self) -> str:
        return self._name(self.actor_id)

    def _write(self, task: TaskV2, kind: str, recipient_ids: Iterable[int], message: str) -> None:
        actor = self._user(self.actor_id)
        now = datetime.now(timezone.utc)
        for user_id in dict.fromkeys(recipient_ids):
            if user_id == self.actor_id:
                continue
            recipient = self._user(user_id)
            if recipient is None or recipient.is_deleted or not recipient.email:
                continue
            self.db.add(
                NotificationV2(
                    recipient_email=recipient.email,
                    type=kind,
                    actor_email=actor.email if actor is not None else None,
                    actor_name=self.actor_name,
                    component_id=task.component_id,
                    thread_id=None,
                    excerpt=_clip(message),
                    created_at=now,
                )
            )
            self.written += 1

    # ── events ──────────────────────────────────────────────────────────

    def assignees_changed(self, task: TaskV2, added: list[int], removed: list[int]) -> None:
        """Assignees added (task creation included) or removed."""
        if not task.notify_on_create_and_delete or not (added or removed):
            return
        actor = self.actor_name
        if added:
            self._write(
                task, NOTIFICATION_TYPE_TASK_ASSIGNED, added,
                f"You've been assigned the task {task.title} by {actor}",
            )
        if removed:
            self._write(
                task, NOTIFICATION_TYPE_TASK_UNASSIGNED, removed,
                f"You've been unassigned from the task {task.title} by {actor}",
            )
        added_names = ", ".join(self._name(uid) for uid in added)
        removed_names = ", ".join(self._name(uid) for uid in removed)
        if added and removed:
            message = f"{actor} assigned {added_names} to the task {task.title} and unassigned {removed_names} from it"
        elif added:
            message = f"{actor} assigned {added_names} to the task {task.title}"
        else:
            message = f"{actor} unassigned {removed_names} from the task {task.title}"
        self._write(task, NOTIFICATION_TYPE_TASK_ASSIGNEES_CHANGED, [task.owner_id], message)

    def details_changed(self, task: TaskV2, assignee_ids: list[int]) -> None:
        if cadence_of(task) != CADENCE_ALWAYS:
            return
        self._write(
            task, NOTIFICATION_TYPE_TASK_UPDATED, [task.owner_id, *assignee_ids],
            f"{self.actor_name} updated the task {task.title}",
        )

    def status_changed(self, task: TaskV2, assignee_ids: list[int]) -> None:
        if cadence_of(task) not in (CADENCE_ALWAYS, CADENCE_ON_STATUS_CHANGE):
            return
        self._write(
            task, NOTIFICATION_TYPE_TASK_STATUS_CHANGED, [task.owner_id, *assignee_ids],
            f"{self.actor_name} updated the status of the task {task.title} to {task.status}",
        )

    def deleted(self, task: TaskV2, assignee_ids: list[int]) -> None:
        """Call before the task row is deleted (its assignee links cascade)."""
        if not task.notify_on_create_and_delete or task.status == CLOSED_STATUS:
            return
        self._write(
            task, NOTIFICATION_TYPE_TASK_DELETED, [task.owner_id, *assignee_ids],
            f"The task {task.title} was deleted by {self.actor_name}",
        )
