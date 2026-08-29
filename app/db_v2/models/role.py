"""Roles and the role DAG (plan_access_control_schema_2026-08-22.md §3.2, §3.3).

An edge means "the parent inherits everything the child has" — the same
direction ``scope.py`` uses, so one closure walk serves both graphs.

The graph is a DAG, not a tree: a role may have several parents (Hub Member
sits under both Program Member and Function Member) and several children
(Hub Admin sits above every Lead).

Acyclicity is enforced by a REAL CYCLE WALK plus a transaction-scoped
advisory lock, exactly as ``parent_child_scopes`` does — the two graphs are
now symmetric. See ``rbac_graph_service.validate_role_edge``.

HISTORICAL NOTE — this was not always true. ``rank`` (renamed ``depth``
below) used to carry acyclicity for free: every edge had to satisfy
``parent.rank < child.rank``, so every path strictly increased rank and no
path could return to its origin, which is why this graph needed neither a
cycle check nor a lock. That rule was DISABLED by requirement, not deleted —
the code is commented out in ``rbac_graph_service`` in case it is wanted
back. With it off, nothing about ``depth`` constrains the graph, so the
cycle walk is the only thing standing between the role DAG and a cycle.
Two concurrent inserts CAN now each be individually acyclic yet jointly form
a cycle, which is precisely why the advisory lock is no longer optional.

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


class RoleV2(BaseV2):
    """One role. Lineage is carried by ``parent_child_roles``; ``depth`` is
    currently inert metadata (see below)."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)

    # `key` is what CODE references; `name` is what admins see and rename.
    # Splitting them is the point: today the product keys on the literal
    # string "Hub Admin" in ~35 places, so a rename in Airtable silently
    # breaks every one of them. Nothing may key on `name`.
    key = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False, unique=True)

    description = Column(Text, nullable=True)

    # Renamed from `rank`, and NULLABLE, because it no longer constrains
    # anything. Nothing in the application reads it: the rank ordering rule it
    # existed to serve is commented out in rbac_graph_service, and the UI does
    # not surface it. It is kept in the schema and on the wire so the rule can
    # be switched back on without another migration.
    #
    # Nullable is the honest shape for that: a role created today has no
    # meaningful value to put here, and NOT NULL would force every caller to
    # invent one. If the rank rule ever comes back, backfilling a value per
    # role is the migration — re-adding the column would not be.
    #
    # Deliberately NOT indexed: never used as a query predicate. An index on a
    # table of this size would be paid for on every write and chosen by
    # nothing. That was true when it was `rank` and is more true now.
    #
    # WHAT IT MEANT WHILE IT WAS ENFORCED, kept because the commented-out
    # validator in rbac_graph_service refers to it:
    #   - Lower = more senior. Hub Admin 100, Leads 200, Members 300,
    #     Hub Member 400.
    #   - NOT unique — peers shared a rank on purpose. Program/Function/Studio
    #     Lead all sat at 200, which is what made them incomparable: neither
    #     `200 < 200` direction holds, so no edge between them was ever legal
    #     in either direction, and nothing had to know they belonged to
    #     different trees for that to be true.
    #   - Values were spaced in hundreds (100/200/300/400, never 0/1/2/3) so a
    #     role could be inserted between two levels with one INSERT instead of
    #     renumbering every role below it and re-validating every incident edge.
    depth = Column(Integer, nullable=True)

    # Delete-guard for rows the application references by `key` — in practice
    # just `hub_admin`. Enforces nothing at the database level; the delete
    # endpoint has to check it. It matters more than it looks: hub_admin is
    # both the role the admin gate depends on and the root every delegated
    # grant descends from (plan §6), so deleting it is unrecoverable through
    # the UI.
    #
    # KNOWN GAP: no code path sets this to True — rbac_service.create_role
    # hardcodes False and it is absent from every request schema, so the guard
    # is presently inert and hub_admin is deletable. Fixing it is a one-line
    # UPDATE plus a decision about who may set the flag; out of scope here.
    is_system = Column(Boolean, nullable=False, default=False)

    # "Any Role" — the BOTTOM of the role lattice
    # (plan_access_control_algorithm_2026-08-27.md §4.4). Every role
    # implicitly inherits it, so it is unioned into every descendant set
    # unconditionally. See ScopeV2.is_public for the full argument on why
    # this is a flag rather than an edge from every role; it applies here
    # verbatim, minus the CHECK — this table has no is_universal, because
    # the role lattice's top is an ordinary row (Hub Admin) and not a flag.
    #
    # WHY BOTH AXES AND NOT JUST SCOPES, which is the obvious economy:
    # `(Hub Member, any-scope)` reaches everyone at Hub Member OR ABOVE, and
    # roles BELOW Hub Member are anticipated. The day one is added, every
    # grant written as `(Hub Member, any-scope)` and meaning "everyone"
    # SILENTLY NARROWS — holders of the new junior role stop matching, and
    # nothing errors. Recovering means auditing every such grant with no way
    # left to tell which ones meant "everyone" from the ones that genuinely
    # meant "Hub Member and up". `(any-role, any-scope)` never narrows. One
    # extra boolean now against an audit-and-rewrite later, on a trigger that
    # is already expected.
    #
    # Same three service-layer corollaries as the scope flag: never an edge
    # endpoint (and that guard must precede the cycle check — see
    # rbac_graph_service.validate_role_edge), never valid as an assignment,
    # immutable after creation.
    is_public = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # At most one public role. Mirrors uq_scopes_single_public exactly,
        # including the requirement that BOTH dialect predicates be spelled
        # out: a dialect with no matching kwarg silently drops the WHERE and
        # emits a plain unique index on is_public, which permits one true row
        # AND one false row — so the second ordinary role anyone creates
        # fails with "UNIQUE constraint failed". Postgres is production,
        # SQLite is what tests/ runs against.
        Index(
            "uq_roles_single_public",
            "is_public",
            unique=True,
            postgresql_where=text("is_public"),
            sqlite_where=text("is_public"),
        ),
    )


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
        # Catches a self-loop, and is now the ONLY cycle protection living in
        # the database at all — exactly the note parent_child_scopes carries,
        # for exactly the same reason. It used to be redundant against the
        # rank rule (no role has rank < its own rank); with that rule disabled
        # it is load-bearing, and every longer cycle is the service layer's
        # job (rbac_graph_service.validate_role_edge).
        CheckConstraint("parent_role_id <> child_role_id", name="ck_parent_child_roles_no_self"),
    )
