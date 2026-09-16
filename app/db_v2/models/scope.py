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

    # "Any Scope" — the BOTTOM of the lattice, mirroring is_universal's top
    # (plan_access_control_algorithm_2026-08-27.md §4.4). Every scope
    # implicitly CONTAINS it, so it is unioned into every descendant set
    # unconditionally: a grant written on it is reachable from whatever
    # scope anyone holds, which is what makes "open to everyone" one row.
    #
    # WHY A FLAG AND NOT N EDGES. The mirror argument to is_universal's,
    # and it is worth spelling out because "just add S -> bottom for every
    # scope" is the obvious first idea. Modelling the bottom with explicit
    # edges means every newly created scope has to be remembered and given
    # an edge DOWN to it, and forgetting one is silent: nodes published to
    # "everyone" quietly stop reaching that scope's users, with no error and
    # nothing to notice. That is the same failure is_universal avoids, just
    # pointing the other way — there it is a silent access HOLE, here a
    # silent access GAP. There is nothing to forget with a flag.
    #
    # The edge encoding is also barred by construction: §5.5 keeps the
    # universal scope out of parent_child_scopes entirely, so even the one
    # edge that would matter most (top -> bottom) has no legal home.
    #
    # Corollaries enforced in the service layer, not here:
    #   - never an endpoint in parent_child_scopes, same as is_universal
    #     (rbac_graph_service.validate_scope_edge). That guard must sit
    #     BEFORE the cycle check: with the bottom in every descendant set,
    #     `parent in scope_descendants(child)` is trivially true for any
    #     edge pointing at it, so the cycle check would reject it first with
    #     a thoroughly misleading message.
    #   - never valid as an ASSIGNMENT (routers/rbac_assignments.py).
    #     Holding it grants only what everyone already reaches, and it would
    #     blow the scope half of the delegation rule wide open, since
    #     `scope_descendants(anything)` contains it for every user alive.
    #     Each flag stays in its lane: is_universal for assignments,
    #     is_public for object grants.
    #   - immutable after creation, same as is_universal — flipping it
    #     silently rewrites what every existing grant reaches.
    is_public = Column(Boolean, nullable=False, default=False)

    # Delete-guard, same terms as RoleV2.is_system — read that comment, it
    # carries the whole argument. In practice the universal scope, which
    # `scripts/set_rbac_system_flags.py` marks alongside the `hub_admin` role.
    #
    # It matters here for a reason specific to this table: §6.5 condition 1
    # requires a Hub Admin to be assigned on the UNIVERSAL scope for a
    # hub-node grant to match at all, so deleting this row would make the
    # admin pair unwritable even with the `hub_admin` role intact. Same
    # unclearability too — `update_scope` writes only name/description.
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
        # At most one public scope, for the mirror reason: a second one would
        # make "the bottom" ambiguous, and since both get unioned into every
        # descendant set, two of them silently double the reach of every
        # grant written on either.
        #
        # BOTH dialect predicates are required here for exactly the reason
        # spelled out above uq_scopes_single_universal — a dialect with no
        # matching kwarg loses the WHERE and emits a PLAIN unique index,
        # which permits one true row AND one false row, i.e. the second
        # ordinary scope anyone creates fails with "UNIQUE constraint
        # failed". Dropping either line is a live bug, not a tidy-up.
        Index(
            "uq_scopes_single_public",
            "is_public",
            unique=True,
            postgresql_where=text("is_public"),
            sqlite_where=text("is_public"),
        ),
        # The two flags are the opposite ends of one lattice, so no row may
        # be both. A row that was would return every scope (the is_universal
        # short-circuit) AND be appended to every walk (the is_public
        # union) — the top and the bottom at once, which is not a coherent
        # thing for a closure to mean.
        #
        # This CHECK has no role-table counterpart: RoleV2 has no
        # is_universal. The role lattice's top is an ordinary row (Hub
        # Admin) rather than a flag, so there is no second flag there to
        # contradict, and roles carry only the partial unique index.
        CheckConstraint(
            "NOT (is_universal AND is_public)",
            name="ck_scopes_not_universal_and_public",
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
