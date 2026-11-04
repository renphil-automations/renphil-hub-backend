"""Task Manager component router — the tasks linked to a `task_manager`
component.

* `GET    /v2/task-manager/components/{component_id}/tasks` — list tasks.
* `POST   /v2/task-manager/tasks` — create a task (caller becomes its owner).
* `PATCH  /v2/task-manager/tasks/{task_id}` — update a task's details/status.
* `DELETE /v2/task-manager/tasks` — delete one or more tasks (partial success).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user, get_viewer_access, is_hub_admin
from app.schemas.task_manager import (
    CreateTaskRequest,
    DeleteTasksRequest,
    DeleteTasksResponse,
    ListTasksResponse,
    TaskMutationResponse,
    UpdateTaskRequest,
)
from app.services import task_manager_service
from app.services.access_visibility_service import ViewerAccess, granted_single_node
from app.services.rbac_graph_service import RbacClosures

router = APIRouter(prefix="/v2/task-manager", tags=["Task Manager"])


def _task_permissions(
    db: Session, current: CurrentHubUser
) -> tuple[bool, task_manager_service.ComponentEditCheck]:
    """``(is Admin on All Scopes, component_id -> has edit on it)``, sharing
    one closure snapshot. The edit check is the single-node fast path (no
    whole-tree build), only run when owner/admin doesn't already decide."""
    closures = RbacClosures(db)
    admin = is_hub_admin(db, current, closures=closures)

    def can_edit_component(component_id: int) -> bool:
        return granted_single_node(
            db,
            current.hub_user_id,
            ("component", component_id),
            is_admin=admin,
            closures=closures,
            edit_only=True,
        )

    return admin, can_edit_component


def _to_http(error: task_manager_service.TaskManagerError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get(
    "/components/{component_id}/tasks",
    response_model=ListTasksResponse,
    summary="List the tasks of a Task Manager component",
    description="""
The caller needs at least view on the component — granted directly or
inherited from an ancestor (role/scope pairs or a share to their email).

* Admin on All Scopes, or edit on the component → every task of the component
  (and they can manage all of them).
* Otherwise → only the tasks the caller owns or is one of the assignees of.

Each task carries `is_owner` / `is_assignee` / `can_manage` / `can_edit_status`
for the caller.
""",
    responses={
        400: {"description": "The component is not a Task Manager component"},
        403: {"description": "The component is visible but its content isn't shared with you"},
        404: {"description": "Component not found or not visible to you"},
    },
)
def list_tasks(
    component_id: int = Path(..., description="The Task Manager component's id."),
    current: CurrentHubUser = Depends(get_current_hub_user),
    access: ViewerAccess = Depends(get_viewer_access),
    db: Session = Depends(get_db_v2),
) -> ListTasksResponse:
    try:
        result = task_manager_service.list_tasks(
            db, component_id, caller_id=current.hub_user_id, access=access
        )
    except task_manager_service.TaskManagerError as e:
        raise _to_http(e)
    return ListTasksResponse(success=True, **result)


@router.post(
    "/tasks",
    response_model=TaskMutationResponse,
    status_code=201,
    summary="Create a task in a Task Manager component",
    description="""
The caller (from the JWT) becomes the task's owner and must be able to view
the component (directly, inherited, or as Admin on All Scopes). `status` defaults
to `Pending`. `assignees` is a list of emails; each must match a user
(duplicates are ignored). `notification_cadence` defaults to `always` and
`notify_on_create_and_delete` to true; when the latter is true, each assignee
gets a "You've been assigned …" notification.
""",
    responses={
        400: {"description": "The component is not a Task Manager component"},
        403: {"description": "The component is visible but its content isn't shared with you"},
        404: {"description": "Component not found / not visible, or an assignee email not found"},
        422: {"description": "Invalid body (e.g. empty title, unsupported status)"},
    },
)
def create_task(
    body: CreateTaskRequest = Body(...),
    current: CurrentHubUser = Depends(get_current_hub_user),
    access: ViewerAccess = Depends(get_viewer_access),
    db: Session = Depends(get_db_v2),
) -> TaskMutationResponse:
    try:
        task = task_manager_service.create_task(
            db,
            caller_id=current.hub_user_id,
            access=access,
            component_id=body.component_id,
            title=body.title,
            description=body.description,
            status=body.status,
            assignee_emails=body.assignees,
            editable_by_assignee=body.editable_by_assignee,
            editable_via_webhook=body.editable_via_webhook,
            notification_cadence=body.notification_cadence,
            notify_on_create_and_delete=body.notify_on_create_and_delete,
        )
    except task_manager_service.TaskManagerError as e:
        raise _to_http(e)
    return TaskMutationResponse(success=True, task=task)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskMutationResponse,
    summary="Update a task",
    description="""
Partial update — only the fields present in the body change.

* `title`, `description`, `assignees` (list of emails, replaces the whole
  list; `[]`/null removes all), `editable_by_assignee`, `editable_via_webhook`:
  `notification_cadence`, `notify_on_create_and_delete`: task owner, editor of
  the task's Task Manager, or Admin on All Scopes. Sets `last_updated_at` /
  `last_updated_by`.
* `status` (`Pending`, `In Progress`, `Blocked`, `Closed`): the same people, or
  an assignee when `editable_by_assignee` is true. Sets
  `status_last_updated_at` / `status_last_updated_by`.

Timestamps only move when a value actually changes.

Notifications (to the owner and assignees, never the caller), following the
task's settings after the update:
* assignees added/removed → the added/removed users and the owner, when
  `notify_on_create_and_delete` is true;
* title/description/editable flags → owner + assignees, when cadence is `always`;
* status → owner + assignees, when cadence is `always` or `on_status_change`;
* the two notification settings themselves → none.
""",
    responses={
        400: {"description": "No updatable field was provided"},
        403: {"description": "Not allowed to make this change"},
        404: {"description": "Task not found, or an assignee email not found"},
        422: {"description": "Invalid body (e.g. empty title, unsupported status)"},
    },
)
def update_task(
    task_id: int = Path(...),
    body: UpdateTaskRequest = Body(...),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> TaskMutationResponse:
    # `model_fields_set` keeps "absent" (unchanged) distinct from "null".
    changes = {field: getattr(body, field) for field in body.model_fields_set}
    is_admin, can_edit_component = _task_permissions(db, current)
    try:
        task = task_manager_service.update_task(
            db,
            task_id,
            caller_id=current.hub_user_id,
            is_admin=is_admin,
            can_edit_component=can_edit_component,
            changes=changes,
        )
    except task_manager_service.TaskManagerError as e:
        raise _to_http(e)
    return TaskMutationResponse(success=True, task=task)


@router.delete(
    "/tasks",
    response_model=DeleteTasksResponse,
    summary="Delete one or more tasks",
    description="""
Deletes each task the caller may manage: one they own, one in a Task Manager
they have edit on, or any task for an Admin on All Scopes. A task that can't be
deleted (not found, not allowed) doesn't block the others: it's listed in
`failed` with its reason. Always 200 — check `success` / `failed`.

The owner and assignees of a deleted task are notified unless it was `Closed`
or `notify_on_create_and_delete` is false.
""",
)
def delete_tasks(
    body: DeleteTasksRequest = Body(...),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> DeleteTasksResponse:
    is_admin, can_edit_component = _task_permissions(db, current)
    result = task_manager_service.delete_tasks(
        db,
        body.task_ids,
        caller_id=current.hub_user_id,
        is_admin=is_admin,
        can_edit_component=can_edit_component,
    )
    return DeleteTasksResponse(success=not result["failed"], **result)
