"""User <-> role <-> scope assignments
(plan_access_control_schema_2026-08-22.md §3.6).

THIS ROW IS THE ROLE-SCOPE PAIRING. There is no ``role_scopes`` table and
there must never be a pair of ``user_roles`` / ``user_scopes`` tables: two
separate lists lose which role went with which scope and yield the
cross-product, so a user who is Program Lead on A and Program Member on B
would read as Program Lead on B. One three-column row cannot be ambiguous.

Effective access expands each row independently (plan §2.4):

    effective(R, S) = role_descendants*(R) x scope_descendants*(S)

unioned across the user's rows — never the union of their roles crossed with
the union of their scopes. The same trap reappears in the delegation check
(plan §6.2), where it is easier to write the broken version because it is
the simpler query.

No relationship() here, matching every other model in db_v2/.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.db_v2.database import BaseV2

# Importing the three FK targets is load-bearing, not tidiness. SQLAlchemy
# resolves ``ForeignKey("hub_users.id")`` by TABLE NAME against
# BaseV2.metadata at DDL time, so any caller that imports this module and
# then runs ``create_all`` over the whole metadata -- which is what
# tests/ does -- fails with NoReferencedTableError unless the target tables
# were also imported. scripts/create_rbac_tables.py only avoids it by
# naming every model explicitly. Same reason create_thread_tables.py
# imports ComponentV2 with a noqa.
from app.db_v2.models.hub_user import HubUserV2  # noqa: F401
from app.db_v2.models.role import RoleV2  # noqa: F401
from app.db_v2.models.scope import ScopeV2  # noqa: F401


class RoleAssignmentV2(BaseV2):
    """One (user, role, scope) grant."""

    __tablename__ = "role_assignments"

    id = Column(Integer, primary_key=True, index=True)

    # CASCADE: deleting a person takes their grants with them.
    #
    # No `index=True` — the composite unique below leads with user_id, and
    # "every assignment for this user" (the hot path: expanding the closure
    # on a request) is exactly that leftmost prefix.
    user_id = Column(
        Integer,
        ForeignKey("hub_users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # RESTRICT, not CASCADE, on both of these: deleting a role or scope that
    # people still hold must not silently revoke access. RESTRICT forces the
    # caller to look at what would be lost and remove the assignments
    # explicitly. Both are indexed — the composite unique cannot serve
    # either, and both the RESTRICT check and "who holds this role?" need it.
    role_id = Column(
        Integer,
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scope_id = Column(
        Integer,
        ForeignKey("scopes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Provenance, in two columns rather than one. Delegation (plan §6) makes
    # "who granted this?" a question the system has to answer, so this FK is
    # joinable and aggregable ("show me everything Alice granted"). But
    # SET NULL on delete would erase the provenance of every grant a
    # departing admin ever made, which is why the email snapshot below sits
    # beside it. Same pattern as ThreadV2.mentions storing display snapshots
    # next to the identity, for the same reason.
    granted_by_user_id = Column(
        Integer,
        ForeignKey("hub_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Immutable snapshot of the granter's email as it read at grant time.
    # Never updated, never an FK — it has to outlive the row above.
    granted_by_email = Column(String(320), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # The same person cannot hold the same role on the same scope twice.
        # Also the index serving the per-request closure expansion.
        UniqueConstraint(
            "user_id",
            "role_id",
            "scope_id",
            name="uq_role_assignments_user_role_scope",
        ),
    )
