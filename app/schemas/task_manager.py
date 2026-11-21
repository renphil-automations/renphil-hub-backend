"""Request / response schemas for the Task Manager component's tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.view_as import VIEW_AS_FLAG_DESCRIPTION, VIEW_AS_USER_DESCRIPTION

TaskStatus = Literal["Pending", "In Progress", "Blocked", "Closed"]
NotificationCadence = Literal["always", "never", "on_status_change"]
TaskMode = Literal["individual", "shared_task", "super_task", "replication"]
CreatableTaskMode = Literal["individual", "shared_task", "super_task"]
UpdateScope = Literal["update_all", "update_selected"]

_CADENCE_DESCRIPTION = (
    "When owner/assignees are notified of changes: `always` (detail and status changes), "
    "`on_status_change` (status changes only), `never`."
)
_NOTIFY_CREATE_DELETE_DESCRIPTION = (
    "Notify on creation, on assignees being added/removed, and on deletion (unless the task is Closed)."
)
_MODE_DESCRIPTION = (
    "`individual`: 0 or 1 assignee. `shared_task`: one task shared by 2+ assignees (a status change "
    "is seen by all). `super_task`: created for 2+ assignees, with one `replication` of it per "
    "assignee; its assignees are its replications' and its status is derived from theirs "
    "(In Progress > Pending > Blocked > Closed). `replication`: one assignee's copy of a super task "
    "(see `abstract_task`); at most one assignee, its mode never changes."
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
    abstract_task: int | None = Field(default=None, description="The super task's id, for a `replication`.")
    is_owner: bool = Field(..., description="The caller owns this task.")
    is_assignee: bool = Field(..., description="The caller is one of this task's assignees.")
    can_manage: bool = Field(
        ...,
        description="The caller can edit every field of this task and delete it (task owner, editor of "
        "the Task Manager, Admin on All Scopes, or the owner of a replication's super task).",
    )
    can_edit_status: bool = Field(
        ...,
        description="The caller can change the status (can_manage, or assignee while editable_by_assignee; "
        "never an assignee on a super task).",
    )
    replications: list[TaskResponse] | None = Field(
        default=None,
        description="A `super_task`'s replications (null on other tasks). Only the super task's managers "
        "ever get a super task; an assignee who can't manage it gets their replication alone.",
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
    tasks: list[TaskResponse] = Field(
        ...,
        description="Super tasks come with their replications nested in `replications` (which aren't "
        "repeated at the top level); a replication of a super task the caller can't see stands alone.",
    )


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
    mode: CreatableTaskMode | None = Field(
        default=None,
        description=_MODE_DESCRIPTION + " Defaults to `individual` with 0 or 1 assignee, `shared_task` "
        "otherwise. `shared_task` and `super_task` need 2+ assignees, `individual` at most one.",
    )


class UpdateTaskRequest(BaseModel):
    """Partial update: only the fields present in the body are changed.

    `title`, `description`, `assignees`, `owner`, `editable_by_assignee`,
    `editable_via_webhook`, `notification_cadence`, `notify_on_create_and_delete`,
    `mode` — task owner, editor of the Task Manager, Admin on All Scopes, or the
    owner of a replication's super task. `status` — the same, or an assignee
    when the task's `editable_by_assignee` is true (never on a super task).
    """

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    assignees: list[str] | None = Field(
        default=None,
        description="Replaces the full assignee list (emails); [] or null removes all assignees. At most one "
        "on a replication. On a super task (`update_all` only), adds a replication per new assignee and "
        "deletes the replications of removed ones.",
    )
    owner: str | None = Field(default=None, description="The new owner's email.")
    editable_by_assignee: bool | None = None
    editable_via_webhook: bool | None = None
    notification_cadence: NotificationCadence | None = Field(default=None, description=_CADENCE_DESCRIPTION)
    notify_on_create_and_delete: bool | None = Field(default=None, description=_NOTIFY_CREATE_DELETE_DESCRIPTION)
    status: TaskStatus | None = None
    mode: TaskMode | None = Field(
        default=None,
        description=_MODE_DESCRIPTION + " Sending the current mode is a no-op. On an `individual` / "
        "`shared_task` task, omitted or null follows the assignee count; `super_task` turns it into a "
        "super task whose assignees each get a replication. A super task or a replication never "
        "changes mode. A mode change alone never notifies anyone.",
    )
    update_scope: UpdateScope | None = Field(
        default=None,
        description="Required when editing a super task (refused on other tasks). `update_all`: the super "
        "task and every replication. `update_selected`: only the `replication_ids` (the super task itself "
        "isn't changed, apart from its derived status).",
    )
    replication_ids: list[int] | None = Field(
        default=None,
        description="With `update_selected`: the replications to update; must be non-empty and all "
        "replications of the edited super task.",
    )


class TaskMutationResponse(_ViewAsEcho):
    success: bool = True
    tasks: list[TaskResponse] = Field(
        ...,
        description="The created or updated task (a super task with its replications nested). After a "
        "replication's update, followed by its super task when the caller can manage that one.",
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
