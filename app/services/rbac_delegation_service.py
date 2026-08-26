"""The §6 delegation rule
(plan_access_control_schema_2026-08-22.md §6,
session_handoff_2026-08-24-permission-management-plan.md §3.1, §4).

Kept separate from ``rbac_graph_service`` for the same split reason that
module documents for itself: that one owns the two DAGs' structural rules
(acyclicity); this one owns the one RULE built on top of their
closures — who may create or revoke a `role_assignments` row. Neither module
touches the other's tables.

THE TRAP THIS MODULE EXISTS TO AVOID (design doc §6.2, restated a third
time because it is the single most likely bug in this phase): the check
below iterates the granter's own `role_assignments` ROWS and must succeed on
ONE of them entirely. It must never gather the granter's roles into one set
and their scopes into another and test the union — that reads as "Alice is
Program Lead on X and Hub Member on Y, so she may grant Program Member on Y"
which is exactly the pairing bug the assignment row exists to prevent
(plan §2.4), resurfacing here because the union query is the easier one to
write.

Hub Admin's unconditional bypass (plan session handoff 2026-08-24 §3.1) is
deliberately NOT folded into `can_delegate` itself — that function answers
one question ("does this row's closures cover the target"), and mixing in a
JWT-role check would make it untestable as a pure graph predicate. Callers
needing the full gate (bypass first, §6.1 check second) use
`assert_can_delegate` below.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.services.rbac_graph_service import RbacGraphError, role_descendants, scope_descendants


def can_delegate(db: Session, granter_hub_user_id: int, role_id: int, scope_id: int) -> bool:
    """§6.1: may the holder of ``granter_hub_user_id``'s assignments create
    or revoke ``(_, role_id, scope_id)``?

    Iterates the granter's own assignment ROWS and succeeds on one held
    ENTIRELY — never on the union of their roles crossed with the union of
    their scopes (§6.2, restated in this module's docstring).

    Role: strictly a proper descendant of the held role (``!=``excluded) —
    granting or revoking your own role, or anything at/above it, is refused.
    Scope: inclusive of the held scope itself — granting on your own scope
    is the primary use case (plan §6.1's asymmetry, deliberate).
    """
    assignments = (
        db.query(RoleAssignmentV2.role_id, RoleAssignmentV2.scope_id)
        .filter(RoleAssignmentV2.user_id == granter_hub_user_id)
        .all()
    )
    for held_role_id, held_scope_id in assignments:
        if role_id == held_role_id:
            continue  # not a PROPER descendant of itself
        if role_id not in role_descendants(db, held_role_id):
            continue
        if scope_id not in scope_descendants(db, held_scope_id):
            continue
        return True
    return False


def assert_can_delegate(
    db: Session,
    *,
    granter_hub_user_id: int,
    is_hub_admin: bool,
    role_id: int,
    scope_id: int,
) -> None:
    """The full write-gate for an assignment create/revoke (session handoff
    2026-08-24 §3.1, §4): Hub Admin bypasses unconditionally; everyone else
    is checked against their own held assignments only.

    Revocation uses this exact same check, not "only what I granted" — the
    caller passes the assignment's role/scope regardless of who originally
    granted it (§6.3, §6.4; restated as the landmine in the handoff's §7).

    THE BOOTSTRAP TRAP (handoff §7): the Hub Admin branch must stay first
    and unconditional. `role_assignments` starts empty, so if this ever
    fell through to `can_delegate` before checking Hub Admin status, or
    required a `role_assignments` row to exist for the Hub Admin path too,
    the system would lock itself out of its own bootstrap with no way back
    in except direct DB access.
    """
    if is_hub_admin:
        return
    if can_delegate(db, granter_hub_user_id, role_id, scope_id):
        return
    raise RbacGraphError(
        "not_delegable",
        (
            "You do not hold a role and scope that lets you grant or revoke "
            "this combination. You can only act on roles strictly below one "
            "you hold, on that same scope or a scope it contains."
        ),
        role_id=role_id,
        scope_id=scope_id,
    )
