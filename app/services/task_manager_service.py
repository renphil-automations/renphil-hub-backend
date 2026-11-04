"""Task Manager component — the tasks stored in ``tasks`` and linked to a
``task_manager`` component by ``component_id``.

Access reuses the existing checkers rather than re-deriving anything:

* Component access (view / edit, direct or inherited from ancestors, through
  the caller's role/scope pairs or a grant shared with their email) is
  ``ViewerAccess`` from ``dependencies.get_viewer_access``.
* "Admin on All Scopes" is ``dependencies.is_hub_admin`` (the ``hub_admin``
  role on the universal scope) — which is also what ``ViewerAccess.full_access``
  is set from.

Who may manage a task (edit its details, change its status, delete it): its
owner, an Admin on All Scopes, or anyone with edit on its Task Manager
component. Its assignees may change its status only, and only when
``editable_by_assignee`` is true. Assignees are many-per-task, stored in
``task_assignee_relationship``.

Service functions raise ``TaskManagerError`` (carrying the HTTP status) or the
access service's ``AccessDeniedError``; the router maps both.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.task import TaskAssigneeV2, TaskV2
from app.db_v2.models.user import UserV2
from app.services.access_visibility_service import NodeVerdict, ViewerAccess
from app.services.task_notification_service import NOTIFICATION_CADENCES, TaskNotifier, cadence_of
from app.services.user_db_service import find_user_by_email

TASK_MANAGER_WIDGET_TYPE = "task_manager"

TASK_STATUSES = ("Pending", "In Progress", "Blocked", "Closed")
DEFAULT_TASK_STATUS = "Pending"

# Fields only a task manager (owner, Admin on All Scopes, component editor) may change.
CONTENT_FIELDS = (
    "title",
    "description",
    "assignees",
    "editable_by_assignee",
    "editable_via_webhook",
    "notification_cadence",
    "notify_on_create_and_delete",
)
# The subset whose change sends the "updated the task" notification. Assignees
# and status have their own notifications; the two notification settings none.
DETAIL_FIELDS = ("title", "description", "editable_by_assignee", "editable_via_webhook")
BOOLEAN_FIELDS = ("editable_by_assignee", "editable_via_webhook", "notify_on_create_and_delete")

# ``component_id -> caller has edit on it``; supplied by the router.
ComponentEditCheck = Callable[[int], bool]


class TaskManagerError(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class TaskManagerHasTasksError(ValueError):
    """A Task Manager component that still has tasks was about to be deleted.

    A ``ValueError`` so every v2 router's existing
    ``except ValueError: raise value_error_to_http_exception(e)`` routes it,
    where it becomes a 409 with ``detail.code``."""

    code = "TASK_MANAGER_HAS_TASKS"

    def __init__(self, component_ids: list[int]) -> None:
        self.component_ids = component_ids
        super().__init__(
            "Cannot delete a Task Manager component that still has tasks. "
            f"Delete its tasks first (component ids: {', '.join(map(str, component_ids))})."
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _component_node(component_id: int) -> tuple[str, int]:
    return ("component", component_id)


def _get_task_manager_component(db: Session, component_id: int) -> ComponentV2:
    component = db.query(ComponentV2).filter(ComponentV2.id == component_id).first()
    if component is None:
        raise TaskManagerError(404, "Task Manager component not found", "COMPONENT_NOT_FOUND")
    if component.type != TASK_MANAGER_WIDGET_TYPE:
        raise TaskManagerError(
            400, f"Component {component_id} is not a Task Manager component", "NOT_A_TASK_MANAGER"
        )
    return component


def _resolve_assignee_ids(db: Session, emails: Iterable[str | None] | None) -> list[int]:
    """``users.id`` of each assignee email, in request order, without
    duplicates (blank entries ignored). Every unknown email is reported at once."""
    normalized = list(dict.fromkeys((e or "").strip().lower() for e in (emails or [])))
    ids: list[int] = []
    missing: list[str] = []
    for email in normalized:
        if not email:
            continue
        user = find_user_by_email(db, email)
        if user is None:
            missing.append(email)
        elif user.id not in ids:
            ids.append(user.id)
    if missing:
        raise TaskManagerError(404, f"No user found with email(s): {', '.join(missing)}", "ASSIGNEE_NOT_FOUND")
    return ids


def _validate_status(status: str | None) -> str:
    if status not in TASK_STATUSES:
        raise TaskManagerError(
            422, f"Invalid status {status!r}. Supported values: {', '.join(TASK_STATUSES)}", "INVALID_STATUS"
        )
    return status


def _validate_cadence(cadence: str | None) -> str:
    if cadence not in NOTIFICATION_CADENCES:
        raise TaskManagerError(
            422,
            f"Invalid notification_cadence {cadence!r}. Supported values: {', '.join(NOTIFICATION_CADENCES)}",
            "INVALID_NOTIFICATION_CADENCE",
        )
    return cadence


def _validate_title(title: str | None) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        raise TaskManagerError(422, "title is required", "TITLE_REQUIRED")
    return cleaned


def _assignee_ids_by_task(db: Session, task_ids: Iterable[int]) -> dict[int, list[int]]:
    ids = set(task_ids)
    result: dict[int, list[int]] = {tid: [] for tid in ids}
    if not ids:
        return result
    rows = (
        db.query(TaskAssigneeV2.task_id, TaskAssigneeV2.assignee_id)
        .filter(TaskAssigneeV2.task_id.in_(ids))
        .order_by(TaskAssigneeV2.id)
        .all()
    )
    for task_id, assignee_id in rows:
        if assignee_id not in result[task_id]:
            result[task_id].append(assignee_id)
    return result


def _set_assignees(db: Session, task_id: int, current: list[int], wanted: list[int]) -> bool:
    """Make the task's links exactly ``wanted``; True if anything changed."""
    removed = [uid for uid in current if uid not in wanted]
    added = [uid for uid in wanted if uid not in current]
    if removed:
        db.query(TaskAssigneeV2).filter(
            TaskAssigneeV2.task_id == task_id, TaskAssigneeV2.assignee_id.in_(removed)
        ).delete(synchronize_session=False)
    for uid in added:
        db.add(TaskAssigneeV2(task_id=task_id, assignee_id=uid))
    return bool(removed or added)


def _emails_by_user_id(db: Session, user_ids: Iterable[int | None]) -> dict[int, str | None]:
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    return {user.id: user.email for user in db.query(UserV2).filter(UserV2.id.in_(ids)).all()}


def serialize_task(
    task: TaskV2,
    caller_id: int,
    assignee_ids: list[int],
    emails: dict[int, str | None],
    *,
    can_manage: bool,
) -> dict[str, Any]:
    is_assignee = caller_id in assignee_ids
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "owner_id": task.owner_id,
        "owner_email": emails.get(task.owner_id),
        "assignees": [{"id": uid, "email": emails.get(uid)} for uid in assignee_ids],
        "component_id": task.component_id,
        "created_at": task.created_at,
        "last_updated_at": task.last_updated_at,
        "last_updated_by": task.last_updated_by,
        "status_last_updated_at": task.status_last_updated_at,
        "status_last_updated_by": task.status_last_updated_by,
        "editable_by_assignee": task.editable_by_assignee,
        "editable_via_webhook": task.editable_via_webhook,
        "notification_cadence": cadence_of(task),
        "notify_on_create_and_delete": task.notify_on_create_and_delete,
        "is_owner": task.owner_id == caller_id,
        "is_assignee": is_assignee,
        "can_manage": can_manage,
        "can_edit_status": can_manage or (is_assignee and bool(task.editable_by_assignee)),
    }


def _serialize_one(db: Session, task: TaskV2, caller_id: int, *, can_manage: bool) -> dict[str, Any]:
    assignee_ids = _assignee_ids_by_task(db, [task.id])[task.id]
    emails = _emails_by_user_id(db, [task.owner_id, *assignee_ids])
    return serialize_task(task, caller_id, assignee_ids, emails, can_manage=can_manage)


def _can_manage(task: TaskV2, caller_id: int, is_admin: bool, can_edit_component: ComponentEditCheck) -> bool:
    if is_admin or task.owner_id == caller_id:
        return True
    return task.component_id is not None and can_edit_component(task.component_id)


def _cached(check: ComponentEditCheck) -> ComponentEditCheck:
    cache: dict[int, bool] = {}

    def wrapped(component_id: int) -> bool:
        if component_id not in cache:
            cache[component_id] = bool(check(component_id))
        return cache[component_id]

    return wrapped


# ---------------------------------------------------------
# Read
# ---------------------------------------------------------


def _require_component_view(db: Session, access: ViewerAccess, component_id: int) -> NodeVerdict:
    """The caller must be able to see the Task Manager — view granted on it
    directly or inherited from an ancestor. An invisible / unknown component
    is a 404 (same fail-closed rule as the rest of the canvas API); one the
    caller sees only because a descendant was shared (revealed, not granted)
    is a 403; a component of another type is a 400."""
    node = _component_node(component_id)
    verdict = access.verdict(node)
    if not verdict.view:
        raise TaskManagerError(404, "Task Manager component not found", "COMPONENT_NOT_FOUND")
    if not access.is_granted(node):
        raise TaskManagerError(403, "You do not have view access to this Task Manager component", "FORBIDDEN")
    _get_task_manager_component(db, component_id)
    return verdict


def list_tasks(db: Session, component_id: int, *, caller_id: int, access: ViewerAccess) -> dict[str, Any]:
    """Tasks of one Task Manager component.

    The caller needs at least view on the component (``_require_component_view``).
    Callers with edit on the component (Hub Admins included) get every task
    and can manage all of them; everyone else only the tasks they own or are
    assigned to.
    """
    verdict = _require_component_view(db, access, component_id)
    is_editor = bool(verdict.edit)
    query = db.query(TaskV2).filter(TaskV2.component_id == component_id)
    if not is_editor:
        assigned = db.query(TaskAssigneeV2.task_id).filter(TaskAssigneeV2.assignee_id == caller_id)
        query = query.filter(or_(TaskV2.owner_id == caller_id, TaskV2.id.in_(assigned)))
    tasks = query.order_by(TaskV2.created_at.desc(), TaskV2.id.desc()).all()

    assignees = _assignee_ids_by_task(db, [t.id for t in tasks])
    emails = _emails_by_user_id(
        db, [uid for t in tasks for uid in (t.owner_id, *assignees[t.id])]
    )
    return {
        "component_id": component_id,
        "can_view_all_tasks": is_editor,
        # Anyone who can see the Task Manager can create tasks in it.
        "can_create_tasks": True,
        "tasks": [
            serialize_task(
                t, caller_id, assignees[t.id], emails, can_manage=is_editor or t.owner_id == caller_id
            )
            for t in tasks
        ],
    }


# ---------------------------------------------------------
# Create
# ---------------------------------------------------------


def create_task(
    db: Session,
    *,
    caller_id: int,
    access: ViewerAccess,
    component_id: int,
    title: str,
    description: str | None = None,
    status: str | None = None,
    assignee_emails: list[str] | None = None,
    editable_by_assignee: bool = True,
    editable_via_webhook: bool = False,
    notification_cadence: str | None = None,
    notify_on_create_and_delete: bool = True,
) -> dict[str, Any]:
    """Create a task owned by the caller, with any number of assignees.
    Requires view on the component (``_require_component_view``). Each
    assignee is notified when ``notify_on_create_and_delete`` is true."""
    _require_component_view(db, access, component_id)

    title = _validate_title(title)
    status = _validate_status(status or DEFAULT_TASK_STATUS)
    assignee_ids = _resolve_assignee_ids(db, assignee_emails)
    cadence = _validate_cadence(notification_cadence or "always")

    now = _utc_now()
    task = TaskV2(
        title=title,
        description=description,
        status=status,
        owner_id=caller_id,
        component_id=component_id,
        created_at=now,
        last_updated_at=now,
        status_last_updated_at=now,
        last_updated_by=caller_id,
        status_last_updated_by=caller_id,
        editable_by_assignee=editable_by_assignee,
        editable_via_webhook=editable_via_webhook,
        notification_cadence=cadence,
        notify_on_create_and_delete=notify_on_create_and_delete,
    )
    try:
        db.add(task)
        db.flush()
        _set_assignees(db, task.id, [], assignee_ids)
        TaskNotifier(db, caller_id).assignees_changed(task, added=assignee_ids, removed=[])
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return _serialize_one(db, task, caller_id, can_manage=True)


# ---------------------------------------------------------
# Update
# ---------------------------------------------------------


def update_task(
    db: Session,
    task_id: int,
    *,
    caller_id: int,
    is_admin: bool,
    can_edit_component: ComponentEditCheck,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Apply ``changes`` (only the keys the client actually sent).

    * Content fields (``CONTENT_FIELDS``; ``assignees`` replaces the whole
      list): task owner, Admin on All Scopes, or an editor of the task's
      component. Bumps ``last_updated_at`` / ``last_updated_by``.
    * ``status``: the same people, or an assignee when the task's
      ``editable_by_assignee`` is true. Bumps ``status_last_updated_at`` /
      ``status_last_updated_by``.

    Everything is validated before anything is written. Timestamps only move
    when a value actually changes. Notifications (``task_notification_service``)
    follow the task's settings AFTER this update, and are written in the same
    commit.
    """
    task = db.query(TaskV2).filter(TaskV2.id == task_id).first()
    if task is None:
        raise TaskManagerError(404, "Task not found", "TASK_NOT_FOUND")

    content_changes = {k: v for k, v in changes.items() if k in CONTENT_FIELDS}
    status_change = "status" in changes
    if not content_changes and not status_change:
        raise TaskManagerError(400, "No updatable fields were provided", "NO_CHANGES")

    current_assignees = _assignee_ids_by_task(db, [task.id])[task.id]
    can_manage = _can_manage(task, caller_id, is_admin, can_edit_component)
    is_assignee = caller_id in current_assignees

    if content_changes and not can_manage:
        raise TaskManagerError(
            403,
            "Only the task owner, an editor of this Task Manager or an Admin on All Scopes "
            "can edit this task's details",
            "FORBIDDEN",
        )
    if status_change and not can_manage:
        if not is_assignee:
            raise TaskManagerError(
                403,
                "Only the task owner, its assignees, an editor of this Task Manager or an Admin "
                "on All Scopes can change its status",
                "FORBIDDEN",
            )
        if not task.editable_by_assignee:
            raise TaskManagerError(
                403, "Assignees are not allowed to change this task's status", "ASSIGNEE_EDIT_DISABLED"
            )

    new_values: dict[str, Any] = {}
    if "title" in content_changes:
        new_values["title"] = _validate_title(content_changes["title"])
    if "description" in content_changes:
        new_values["description"] = content_changes["description"]
    if "notification_cadence" in content_changes:
        new_values["notification_cadence"] = _validate_cadence(content_changes["notification_cadence"])
    for flag in BOOLEAN_FIELDS:
        if flag in content_changes:
            if content_changes[flag] is None:
                raise TaskManagerError(422, f"{flag} cannot be null", "INVALID_VALUE")
            new_values[flag] = bool(content_changes[flag])
    wanted_assignees = (
        _resolve_assignee_ids(db, content_changes["assignees"]) if "assignees" in content_changes else None
    )
    new_status = _validate_status(changes["status"]) if status_change else None

    try:
        now = _utc_now()
        changed_fields: set[str] = set()
        for field, value in new_values.items():
            current = cadence_of(task) if field == "notification_cadence" else getattr(task, field)
            if current != value:
                setattr(task, field, value)
                changed_fields.add(field)
        assignees_after = current_assignees
        if wanted_assignees is not None and _set_assignees(db, task.id, current_assignees, wanted_assignees):
            changed_fields.add("assignees")
            assignees_after = wanted_assignees
        if changed_fields:
            task.last_updated_at = now
            task.last_updated_by = caller_id
        status_changed = status_change and task.status != new_status
        if status_changed:
            task.status = new_status
            task.status_last_updated_at = now
            task.status_last_updated_by = caller_id

        notifier = TaskNotifier(db, caller_id)
        if "assignees" in changed_fields:
            notifier.assignees_changed(
                task,
                added=[uid for uid in assignees_after if uid not in current_assignees],
                removed=[uid for uid in current_assignees if uid not in assignees_after],
            )
        if changed_fields & set(DETAIL_FIELDS):
            notifier.details_changed(task, assignees_after)
        if status_changed:
            notifier.status_changed(task, assignees_after)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return _serialize_one(db, task, caller_id, can_manage=can_manage)


# ---------------------------------------------------------
# Delete
# ---------------------------------------------------------


def delete_tasks(
    db: Session,
    task_ids: list[int],
    *,
    caller_id: int,
    is_admin: bool,
    can_edit_component: ComponentEditCheck,
) -> dict[str, Any]:
    """Delete every task the caller may manage (owner, Admin on All Scopes,
    editor of the task's component). A task that can't be deleted doesn't
    block the others; it is reported in ``failed`` instead. Owner and
    assignees are notified when the task wasn't Closed and its
    ``notify_on_create_and_delete`` is true."""
    unique_ids = list(dict.fromkeys(task_ids))
    tasks = {t.id: t for t in db.query(TaskV2).filter(TaskV2.id.in_(unique_ids)).all()} if unique_ids else {}
    check = _cached(can_edit_component)

    deleted: list[int] = []
    failed: list[dict[str, Any]] = []
    for task_id in unique_ids:
        task = tasks.get(task_id)
        if task is None:
            failed.append(
                {"task_id": task_id, "status_code": 404, "code": "TASK_NOT_FOUND", "message": "Task not found"}
            )
            continue
        if not _can_manage(task, caller_id, is_admin, check):
            failed.append(
                {
                    "task_id": task_id,
                    "status_code": 403,
                    "code": "FORBIDDEN",
                    "message": "Only the task owner, an editor of this Task Manager or an Admin on "
                    "All Scopes can delete this task",
                }
            )
            continue
        deleted.append(task_id)

    if deleted:
        try:
            # Read the assignees before their links go.
            assignees = _assignee_ids_by_task(db, deleted)
            notifier = TaskNotifier(db, caller_id)
            for task_id in deleted:
                notifier.deleted(tasks[task_id], assignees[task_id])
            # The FK cascades too; deleting the links explicitly doesn't rely on it.
            db.query(TaskAssigneeV2).filter(TaskAssigneeV2.task_id.in_(deleted)).delete(
                synchronize_session=False
            )
            for task_id in deleted:
                db.delete(tasks[task_id])
            db.commit()
        except Exception:
            db.rollback()
            raise

    return {
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
    }


# ---------------------------------------------------------
# Component-deletion guard
# ---------------------------------------------------------


def assert_no_linked_tasks(db: Session, component_ids: Iterable[int]) -> None:
    """Raise ``TaskManagerHasTasksError`` if any of ``component_ids`` is a
    Task Manager component that still has tasks. Call before any deletion
    step, so nothing is half-deleted."""
    ids = {cid for cid in component_ids if cid is not None}
    if not ids:
        return
    rows = (
        db.query(TaskV2.component_id)
        .join(ComponentV2, ComponentV2.id == TaskV2.component_id)
        .filter(TaskV2.component_id.in_(ids), ComponentV2.type == TASK_MANAGER_WIDGET_TYPE)
        .distinct()
        .all()
    )
    if rows:
        raise TaskManagerHasTasksError(sorted(row[0] for row in rows))
