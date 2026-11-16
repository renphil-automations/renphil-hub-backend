"""Request / response schemas for the Task Manager component's tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.view_as import VIEW_AS_FLAG_DESCRIPTION, VIEW_AS_USER_DESCRIPTION

TaskStatus = Literal["Pending", "In Progress", "Blocked", "Closed"]
NotificationCadence = Literal["always", "never", "on_status_change"]
TaskMode = Literal["group_task", "individual_task"]

_CADENCE_DESCRIPTION = (
    "When owner/assignees are notified of changes: `always` (detail and status changes), "
    "`on_status_change` (status changes only), `never`."
)
_NOTIFY_CREATE_DELETE_DESCRIPTION = (
    "Notify on creation, on assignees being added/removed, and on deletion (unless the task is Closed)."
)
_MODE_DESCRIPTION = (
    "`group_task`: one task shared by every assignee (a status change is seen by all); needs at "
    "least 2 assignees. `individual_task`: one separate task per assignee. With 0 or 1 assignee "
    "the task is always `individual_task`."
)


class TaskAssignee(BaseModel):
    id: int = Field(..., description="users.id")
    email: str | None = None


class _ViewAsEcho(BaseModel):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)
    view_as_user: str | None = Field(default=None, description=VIEW_AS_USER_DESCRIPTION)


class TaskResponse(BaseModel):
    id: int
    title: str
    description: str | None = None
    status: str
    owner_id: int
    owner_email: str | None = None
    assignees: list[TaskAssignee]
    component_id: int | None = None
    created_at: datetime
    last_updated_at: datetime
    last_updated_by: int | None = None
    status_last_updated_at: datetime
    status_last_updated_by: int | None = None
    editable_by_assignee: bool
    editable_via_webhook: bool
    notification_cadence: NotificationCadence
    notify_on_create_and_delete: bool
    mode: TaskMode = Field(..., description=_MODE_DESCRIPTION)
    is_owner: bool = Field(..., description="The caller owns this task.")
    is_assignee: bool = Field(..., description="The caller is one of this task's assignees.")
    can_manage: bool = Field(
        ...,
        description="The caller can edit every field of this task and delete it "
        "(task owner, editor of the Task Manager, or Admin on All Scopes).",
    )
    can_edit_status: bool = Field(
        ..., description="The caller can change the status (can_manage, or assignee while editable_by_assignee)."
    )


class ListTasksResponse(_ViewAsEcho):
    success: bool = True
    component_id: int
    can_view_all_tasks: bool = Field(
        ...,
        description="True when the caller has edit on the component (or is an Admin on All Scopes) "
        "and therefore sees every task; false when only their own / assigned tasks are returned.",
    )
    can_create_tasks: bool = Field(
        ...,
        description="Always true: anyone who can see the Task Manager can create tasks in it. In "
        "anonymous View As mode this previews what the roles allow; the write itself is refused.",
    )
    tasks: list[TaskResponse]


class CreateTaskRequest(BaseModel):
    component_id: int = Field(..., description="The Task Manager component's id.")
    title: str = Field(..., min_length=1)
    description: str | None = None
    status: TaskStatus | None = Field(default=None, description="Defaults to `Pending`.")
    assignees: list[str] = Field(default_factory=list, description="Assignees' emails (any number, may be empty).")
    editable_by_assignee: bool = True
    editable_via_webhook: bool = False
    notification_cadence: NotificationCadence = Field(default="always", description=_CADENCE_DESCRIPTION)
    notify_on_create_and_delete: bool = Field(default=True, description=_NOTIFY_CREATE_DELETE_DESCRIPTION)
    mode: TaskMode | None = Field(
        default=None,
        description=_MODE_DESCRIPTION + " Defaults to `group_task` with 2+ assignees, `individual_task` "
        "otherwise. `individual_task` with N assignees creates N tasks.",
    )


class UpdateTaskRequest(BaseModel):
    """Partial update: only the fields present in the body are changed.

    `title`, `description`, `assignees`, `editable_by_assignee`,
    `editable_via_webhook`, `notification_cadence`, `notify_on_create_and_delete`,
    `mode` — task owner, editor of the Task Manager, or Admin on All Scopes. `status` — the same, or an assignee when the task's
    `editable_by_assignee` is true.
    """

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    assignees: list[str] | None = Field(
        default=None, description="Replaces the full assignee list (emails); [] or null removes all assignees."
    )
    editable_by_assignee: bool | None = None
    editable_via_webhook: bool | None = None
    notification_cadence: NotificationCadence | None = Field(default=None, description=_CADENCE_DESCRIPTION)
    notify_on_create_and_delete: bool | None = Field(default=None, description=_NOTIFY_CREATE_DELETE_DESCRIPTION)
    status: TaskStatus | None = None
    mode: TaskMode | None = Field(
        default=None,
        description=_MODE_DESCRIPTION + " Only the task owner, an editor of the Task Manager or an Admin on "
        "All Scopes may change it. When omitted, the task keeps its mode: a `group_task` reduced to fewer "
        "than 2 assignees is refused, and an `individual_task` given N > 1 assignees is split. "
        "`individual_task` on a task left with N > 1 assignees splits it: this task keeps one assignee and "
        "N-1 copies are created, one per other assignee. A mode change alone never notifies anyone.",
    )


class TaskMutationResponse(_ViewAsEcho):
    success: bool = True
    tasks: list[TaskResponse] = Field(
        ...,
        description="Every task created or updated by the request: one, or one per assignee for an "
        "`individual_task` with several assignees (on update, the updated task first, then the copies).",
    )


class DeleteTasksRequest(BaseModel):
    task_ids: list[int] = Field(..., min_length=1)


class DeleteTaskFailure(BaseModel):
    task_id: int
    status_code: int
    code: str
    message: str


class DeleteTasksResponse(_ViewAsEcho):
    success: bool = Field(..., description="True when every requested task was deleted.")
    deleted: list[int]
    failed: list[DeleteTaskFailure]
    deleted_count: int
    failed_count: int
