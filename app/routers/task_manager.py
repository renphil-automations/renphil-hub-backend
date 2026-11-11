"""Task Manager component router — the tasks linked to a `task_manager`
component.

* `GET    /v2/task-manager/components/{component_id}/tasks` — list tasks.
* `POST   /v2/task-manager/tasks` — create a task (caller becomes its owner).
* `PATCH  /v2/task-manager/tasks/{task_id}` — update a task's details/status.
* `DELETE /v2/task-manager/tasks` — delete one or more tasks (partial success).

All four accept the View As parameters (`view_as`, `view_as_user`,
`anonymous`). Component access and edit rights then follow the selected
pairs (plus content shared directly with the caller, unless `anonymous`); the
caller's own and assigned tasks stay theirs unless `anonymous`. Writes are
refused under `anonymous`, and are always recorded as the real caller's.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import (
    CurrentHubUser,
    ViewAsAccess,
    get_current_hub_user,
    get_view_as_access,
    get_view_as_anonymous,
    get_view_as_selection,
    is_hub_admin,
)
from app.schemas.task_manager import (
    CreateTaskRequest,
    DeleteTasksRequest,
    DeleteTasksResponse,
    ListTasksResponse,
    TaskMutationResponse,
    UpdateTaskRequest,
)
from app.services import task_manager_service, view_as_service
from app.services.access_visibility_service import granted_single_node
from app.services.rbac_graph_service import RbacClosures

router = APIRouter(prefix="/v2/task-manager", tags=["Task Manager"])

ANONYMOUS_READ_ONLY_CODE = "VIEW_AS_ANONYMOUS_READ_ONLY"

_VIEW_AS_400 = "View As: a malformed `view_as` pair, an unknown key or user, or too many pairs"
_VIEW_AS_403 = (
    "View As refused (the caller may not adopt one or more of the given pairs), or "
    f"`{ANONYMOUS_READ_ONLY_CODE}`: tasks can't be created, edited or deleted with `anonymous=true`"
)

_VIEW_AS_WRITE_DOC = """
**View As** (`view_as` pairs, `view_as_user`): edit on the Task Manager and
Admin on All Scopes follow the selected pairs (plus content shared directly
with the caller) instead of the caller's own assignments; the caller's own
ownership / assignments still count. The change is recorded and notified as
the caller's. Refused with 403 `VIEW_AS_ANONYMOUS_READ_ONLY` when
`anonymous=true`, before anything else is checked.
"""


def _refuse_anonymous(anonymous: bool = Depends(get_view_as_anonymous)) -> None:
    """Task writes are read-only under anonymous View As. A route-level
    dependency, so it runs before the View As verification and access fold."""
    if anonymous:
        raise HTTPException(
            status_code=403,
            detail={
                "code": ANONYMOUS_READ_ONLY_CODE,
                "message": "Tasks can't be created, edited or deleted in anonymous View As mode.",
            },
        )


def _task_permissions(
    db: Session,
    current: CurrentHubUser,
    selection: view_as_service.ViewAsSelection | None = None,
) -> tuple[bool, task_manager_service.ComponentEditCheck]:
    """``(is Admin on All Scopes, component_id -> has edit on it)``.

    Without View As: the caller's own access, sharing one closure snapshot;
    the edit check is the single-node fast path (no whole-tree build), only
    run when owner/admin doesn't already decide. With View As: the selected
    pairs' access plus the caller's direct shares — the same access the View
    As reads use (``get_view_as_access``), built once for the request."""
    if selection is not None:
        access = view_as_service.resolve_view_as_access(db, selection.targets, user_id=current.hub_user_id)
        return access.full_access, lambda component_id: access.verdict(("component", component_id)).edit

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


def _view_as_echo(selection: view_as_service.ViewAsSelection | None) -> dict:
    return {
        "view_as": selection is not None,
        "view_as_user": selection.user_email if selection is not None else None,
    }


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

**View As** (`view_as` pairs, `view_as_user`, `anonymous`): view and edit on
the component (and Admin on All Scopes) follow the selected pairs, plus
content shared directly with the caller unless `anonymous`.
* Without `anonymous`, the caller's own and assigned tasks are still listed
  and flagged as theirs, exactly as in their normal view.
* With `anonymous`, only what the roles/scopes allow: every task when they
  give edit on the component, none otherwise; `is_owner` / `is_assignee` are
  false. `can_manage`, `can_edit_status` and `can_create_tasks` preview what
  the roles/scopes allow, although task writes are refused in that mode.
""",
    responses={
        400: {"description": f"The component is not a Task Manager component, or {_VIEW_AS_400}"},
        403: {"description": f"The component is visible but its content isn't shared with you, or {_VIEW_AS_403}"},
        404: {"description": "Component not found or not visible to you"},
    },
)
def list_tasks(
    component_id: int = Path(..., description="The Task Manager component's id."),
    current: CurrentHubUser = Depends(get_current_hub_user),
    anonymous: bool = Depends(get_view_as_anonymous),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
    db: Session = Depends(get_db_v2),
) -> ListTasksResponse:
    try:
        result = task_manager_service.list_tasks(
            db, component_id, caller_id=current.hub_user_id, access=view_as_access.access, anonymous=anonymous
        )
    except task_manager_service.TaskManagerError as e:
        raise _to_http(e)
    return ListTasksResponse(
        success=True, view_as=view_as_access.view_as, view_as_user=view_as_access.view_as_user, **result
    )


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

**View As** (`view_as` pairs, `view_as_user`): the selected pairs (plus content
shared directly with the caller) must be able to view the component. The task
is still owned by the caller. Refused with 403 `VIEW_AS_ANONYMOUS_READ_ONLY`
when `anonymous=true`.
""",
    dependencies=[Depends(_refuse_anonymous)],
    responses={
        400: {"description": f"The component is not a Task Manager component, or {_VIEW_AS_400}"},
        403: {"description": f"The component is visible but its content isn't shared with you, or {_VIEW_AS_403}"},
        404: {"description": "Component not found / not visible, or an assignee email not found"},
        422: {"description": "Invalid body (e.g. empty title, unsupported status)"},
    },
)
def create_task(
    body: CreateTaskRequest = Body(...),
    current: CurrentHubUser = Depends(get_current_hub_user),
    view_as_access: ViewAsAccess = Depends(get_view_as_access),
    db: Session = Depends(get_db_v2),
) -> TaskMutationResponse:
    try:
        task = task_manager_service.create_task(
            db,
            caller_id=current.hub_user_id,
            access=view_as_access.access,
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
    return TaskMutationResponse(
        success=True, task=task, view_as=view_as_access.view_as, view_as_user=view_as_access.view_as_user
    )


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
""" + _VIEW_AS_WRITE_DOC,
    dependencies=[Depends(_refuse_anonymous)],
    responses={
        400: {"description": f"No updatable field was provided, or {_VIEW_AS_400}"},
        403: {"description": f"Not allowed to make this change, or {_VIEW_AS_403}"},
        404: {"description": "Task not found, or an assignee email not found"},
        422: {"description": "Invalid body (e.g. empty title, unsupported status)"},
    },
)
def update_task(
    task_id: int = Path(...),
    body: UpdateTaskRequest = Body(...),
    current: CurrentHubUser = Depends(get_current_hub_user),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
    db: Session = Depends(get_db_v2),
) -> TaskMutationResponse:
    # `model_fields_set` keeps "absent" (unchanged) distinct from "null".
    changes = {field: getattr(body, field) for field in body.model_fields_set}
    is_admin, can_edit_component = _task_permissions(db, current, selection)
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
    return TaskMutationResponse(success=True, task=task, **_view_as_echo(selection))


@router.delete(
    "/tasks",
    response_model=DeleteTasksResponse,
    summary="Delete one or more tasks",
    description="""
Deletes each task the caller may manage: one they own, one in a Task Manager
they have edit on, or any task for an Admin on All Scopes. A task that can't be
deleted (not found, not allowed) doesn't block the others: it's listed in
`failed` with its reason. 200 unless the View As parameters are refused — check
`success` / `failed`.

The owner and assignees of a deleted task are notified unless it was `Closed`
or `notify_on_create_and_delete` is false.
""" + _VIEW_AS_WRITE_DOC,
    dependencies=[Depends(_refuse_anonymous)],
    responses={400: {"description": _VIEW_AS_400}, 403: {"description": _VIEW_AS_403}},
)
def delete_tasks(
    body: DeleteTasksRequest = Body(...),
    current: CurrentHubUser = Depends(get_current_hub_user),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
    db: Session = Depends(get_db_v2),
) -> DeleteTasksResponse:
    is_admin, can_edit_component = _task_permissions(db, current, selection)
    result = task_manager_service.delete_tasks(
        db,
        body.task_ids,
        caller_id=current.hub_user_id,
        is_admin=is_admin,
        can_edit_component=can_edit_component,
    )
    return DeleteTasksResponse(success=not result["failed"], **_view_as_echo(selection), **result)
