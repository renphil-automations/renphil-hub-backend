"""Roles and the role DAG (plan_access_control_schema_2026-08-22.md §3.2, §3.3).

An edge means "the parent inherits everything the child has" — the same
direction ``scope.py`` uses, so one closure walk serves both graphs.

The graph is a DAG, not a tree: a role may have several parents (Hub Member
sits under both Program Member and Function Member) and several children
(Hub Admin sits above every Lead).

Acyclicity is NOT enforced here by a cycle check. It falls out of ``rank``:
every edge must satisfy ``parent.rank < child.rank``, so every path strictly
increases rank and no path can return to its origin. That is why this graph
needs no advisory lock on write, unlike ``parent_child_scopes`` (plan §5.2, §5.6).
The rank rule itself spans three rows and cannot be a CHECK — it lives in
the service layer, and the ONE thing that can break it after the fact is
editing a role's rank once edges exist (plan §5.3).

No relationship() here, matching every other model in db_v2/.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.db_v2.database import BaseV2


class RoleV2(BaseV2):
    """One role. Authority is carried by ``rank``, lineage by ``parent_child_roles``."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)

    # `key` is what CODE references; `name` is what admins see and rename.
    # Splitting them is the point: today the product keys on the literal
    # string "Hub Admin" in ~35 places, so a rename in Airtable silently
    # breaks every one of them. Nothing may key on `name`.
    key = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False, unique=True)

    description = Column(Text, nullable=True)

    # Lower = more senior. Hub Admin 100, the Leads 200, the Members 300,
    # Hub Member 400.
    #
    # NOT unique — peers share a rank on purpose. Program Lead, Function Lead
    # and Studio Lead all sit at 200, which is exactly what makes them
    # incomparable: neither `200 < 200` direction holds, so no edge between
    # them is ever legal in either direction, and nothing has to know they
    # belong to different trees for that to be true.
    #
    # Leave GAPS when assigning values (100/200/300/400, never 0/1/2/3).
    # Inserting a role between two existing levels later is then one INSERT
    # instead of renumbering every role below it and re-validating every edge
    # that touches them.
    #
    # Deliberately NOT indexed: rank is only ever read for a role already
    # located by id (the edge check loads both endpoints), never used as a
    # query predicate. An index on a table of this size would be paid for on
    # every write and chosen by nothing.
    rank = Column(Integer, nullable=False)

    # Delete-guard for rows the application references by `key` — in practice
    # just `hub_admin`. Enforces nothing at the database level; the delete
    # endpoint has to check it. It matters more than it looks: hub_admin is
    # both the role the admin gate depends on and the root every delegated
    # grant descends from (plan §6), so deleting it is unrecoverable through
    # the UI.
    is_system = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)


class RoleEdgeV2(BaseV2):
    """One parent -> child inheritance edge in the role DAG."""

    __tablename__ = "parent_child_roles"

    id = Column(Integer, primary_key=True, index=True)

    # Deliberately NO `index=True`: the UniqueConstraint below leads with
    # parent_role_id, so a single-column index here would be a redundant
    # leftmost prefix — paid for on every insert, never chosen over the
    # composite. Same reasoning as ThreadV2.component_id.
    parent_role_id = Column(
        Integer,
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )

    # This one IS indexed: walking ANCESTORS (the reverse direction) filters
    # on child_role_id, which the composite unique cannot serve.
    child_role_id = Column(
        Integer,
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Not optional. Without it the same edge inserts twice — harmless to
        # the closure (which is a set) but it makes "delete this edge"
        # ambiguous. Also the index that serves every descendant walk.
        UniqueConstraint("parent_role_id", "child_role_id", name="uq_parent_child_roles_pair"),
        # The only part of acyclicity a per-row CHECK can express. Strictly
        # redundant given the rank rule (no role has rank < its own rank),
        # but free, and it still holds if a rank is mid-edit.
        CheckConstraint("parent_role_id <> child_role_id", name="ck_parent_child_roles_no_self"),
    )
