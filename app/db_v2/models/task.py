"""The ``tasks`` table — tasks created inside a ``task_manager`` component.

Linked to its Task Manager by ``component_id``. The FK has NO cascade, so a
Task Manager that still has tasks cannot be deleted (the canvas save and the
tab-subtree delete refuse it up front — see
``task_manager_service.assert_no_linked_tasks``).

Assignees live in ``task_assignee_relationship`` (many per task; both FKs
``ON DELETE CASCADE``, so deleting a task or a user drops the links).

No ORM ``relationship()`` — same rule as every other ``db_v2`` model.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)

from app.db_v2.database import BaseV2


class TaskV2(BaseV2):
    __tablename__ = "tasks"

    id = Column(Integer, Identity(always=True), primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(
        Integer, ForeignKey("users.id", name="owner_user_id_constraint"), nullable=False
    )
    status = Column(String, nullable=False)
    component_id = Column(
        Integer, ForeignKey("components.id", name="component_id_constraint"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status_last_updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    editable_by_assignee = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    editable_via_webhook = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_updated_by = Column(Integer, ForeignKey("users.id", name="last_updated_by_constraint"), nullable=True)
    status_last_updated_by = Column(
        Integer, ForeignKey("users.id", name="status_last_updated_by_constraint"), nullable=True
    )
    # 'always' | 'never' | 'on_status_change'; NULL is treated as 'always'.
    notification_cadence = Column(String, nullable=True, default="always", server_default=text("'always'"))
    notify_on_create_and_delete = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    __table_args__ = (
        Index(
            "tasks_component_id_idx",
            "component_id",
            postgresql_where=text("component_id IS NOT NULL"),
        ),
        Index("tasks_owner_id_idx", "owner_id"),
    )


class TaskAssigneeV2(BaseV2):
    """One (task, assignee) link; the pair is unique."""

    __tablename__ = "task_assignee_relationship"

    id = Column(Integer, Identity(always=True), primary_key=True)
    assignee_id = Column(
        Integer,
        ForeignKey("users.id", name="assignee_user_id_constraint", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    task_id = Column(
        Integer,
        ForeignKey("tasks.id", name="task_id_constraint", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        Index("task_assignee_relationship_task_assignee_key", "task_id", "assignee_id", unique=True),
        Index("task_assignee_relationship_assignee_id_idx", "assignee_id"),
    )
