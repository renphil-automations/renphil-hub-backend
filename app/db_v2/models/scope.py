"""Scopes and the scope DAG (plan_access_control_schema_2026-08-22.md §3.4, §3.5).

An edge means "the parent CONTAINS the child" — holding the parent grants
everything the child grants. That is the same direction ``role.py`` uses
("holding the parent gives you the child"), which is what lets one closure
walk serve both graphs.

Multi-parent is expected, not exceptional: Scope A can sit inside both
"All Programs" and "Onboarding" at once.

Acyclicity is a real service-layer check and every edge write must take a
transaction-scoped advisory lock first — two concurrent inserts can each be
individually acyclic yet jointly form a cycle (plan §5.4, §5.6).

This used to be the asymmetric half: the role graph got acyclicity free from
its rank ordering and needed neither check nor lock, while scopes, having no
natural authority ordering, needed both. The rank rule is now disabled, so
``role.py`` does exactly what this module does and the two are symmetric.

No relationship() here, matching every other model in db_v2/.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from app.db_v2.database import BaseV2


class ScopeV2(BaseV2):
    """One scope. A "super scope" is just a scope with children."""

    __tablename__ = "scopes"

    id = Column(Integer, primary_key=True, index=True)

    # Same split as RoleV2 — `key` for code, `name` for humans to rename.
    key = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False, unique=True)

    description = Column(Text, nullable=True)

    # "All Scopes". A universal scope IMPLICITLY contains every scope,
    # present and future — its descendant set is computed as "all rows", not
    # read from parent_child_scopes.
    #
    # That implicitness is the whole reason the flag exists. Modelling "All
    # Scopes" as an ordinary super-scope with explicit children means every
    # newly created scope has to be remembered and added underneath it, and
    # forgetting is a silent access hole for whoever holds it. There is
    # nothing to forget here.
    #
    # Corollary enforced in the service layer, not here: a universal scope
    # may never appear in parent_child_scopes as either endpoint (plan §5.5). An
    # explicit edge is redundant at best and contradictory at worst.
    is_universal = Column(Boolean, nullable=False, default=False)

    # Delete-guard, same terms as RoleV2.is_system — in practice the
    # universal scope.
    is_system = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # At most one universal scope, enforced by the database rather than
        # by whoever writes the admin UI. A second one would make
        # "every scope" ambiguous and silently double every closure.
        #
        # BOTH dialect predicates are required, and dropping either is a
        # live bug rather than a tidy-up. These kwargs are dialect-prefixed,
        # so a dialect with no matching one silently loses the WHERE and
        # emits a PLAIN unique index on is_universal — which permits exactly
        # one true row AND exactly one false row, i.e. the second ordinary
        # scope anyone creates fails with "UNIQUE constraint failed".
        # Postgres is production; SQLite is what tests/ runs against, and it
        # has supported partial indexes since 3.8.0, so both are spelled out.
        Index(
            "uq_scopes_single_universal",
            "is_universal",
            unique=True,
            postgresql_where=text("is_universal"),
            sqlite_where=text("is_universal"),
        ),
    )


class ScopeEdgeV2(BaseV2):
    """One parent -> child containment edge in the scope DAG."""

    __tablename__ = "parent_child_scopes"

    id = Column(Integer, primary_key=True, index=True)

    # No `index=True` — covered as the leftmost prefix of the composite
    # unique below. Same reasoning as RoleEdgeV2.parent_role_id.
    parent_scope_id = Column(
        Integer,
        ForeignKey("scopes.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Indexed: the ancestor walk filters on this column, which the composite
    # unique cannot serve.
    child_scope_id = Column(
        Integer,
        ForeignKey("scopes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("parent_scope_id", "child_scope_id", name="uq_parent_child_scopes_pair"),
        # Catches a self-loop. Not redundant here the way it is on
        # parent_child_roles — this graph has no rank rule behind it, so this CHECK
        # is the only cycle protection that lives in the database at all.
        CheckConstraint("parent_scope_id <> child_scope_id", name="ck_parent_child_scopes_no_self"),
    )
