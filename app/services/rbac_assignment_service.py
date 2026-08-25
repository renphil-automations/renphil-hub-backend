"""Assignment CRUD and listings
(plan_access_control_schema_2026-08-22.md §3.6,
session_handoff_2026-08-24-permission-management-plan.md §5.3, §5.4).

Split from ``rbac_delegation_service`` the same way ``rbac_service`` is split
from ``rbac_graph_service``: that module owns the §6.1 RULE, this one owns
the rows — resolving a target email to a `hub_users` id, inserting/deleting
`role_assignments`, and serializing for the three read surfaces the frontend
needs (what I hold, what I granted, what I could revoke right now).

The write gate (`assert_can_delegate`) is deliberately NOT called from this
module — it is called once, from the router, so the 409 mapping stays in one
place (`_conflict`) and this module never imports FastAPI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db_v2.models.hub_user import HubUserV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.services.rbac_delegation_service import can_delegate
from app.services.rbac_graph_service import RbacGraphError

HUB_USER_SEARCH_MIN_LENGTH = 3
HUB_USER_SEARCH_LIMIT = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------
# Serialization
# ---------------------------------------------------------


def _hub_user_email_map(db: Session, hub_user_ids: set[int]) -> dict[int, str]:
    """One query for every target/granter email a page of assignments needs,
    rather than one per row — same N+1 avoidance as rbac_service's edge maps.

    Safe to join live (never a snapshot) for the TARGET side: `user_id`
    CASCADEs (plan §3.6), so an assignment row can never outlive the
    hub_users row it points to. `granted_by_email` stays a stored snapshot
    for the granter side, since that FK is SET NULL, not CASCADE.
    """
    if not hub_user_ids:
        return {}
    rows = db.query(HubUserV2.id, HubUserV2.email).filter(HubUserV2.id.in_(hub_user_ids)).all()
    return {row[0]: row[1] for row in rows}


def _serialize(assignment: RoleAssignmentV2, user_email: str) -> dict:
    return {
        "id": assignment.id,
        "user_id": assignment.user_id,
        "user_email": user_email,
        "role_id": assignment.role_id,
        "scope_id": assignment.scope_id,
        "granted_by_user_id": assignment.granted_by_user_id,
        "granted_by_email": assignment.granted_by_email,
        "created_at": assignment.created_at,
    }


def _serialize_many(db: Session, assignments: list[RoleAssignmentV2]) -> list[dict]:
    emails = _hub_user_email_map(db, {a.user_id for a in assignments})
    return [_serialize(a, emails.get(a.user_id, "")) for a in assignments]


# ---------------------------------------------------------
# Reads
# ---------------------------------------------------------


def list_my_assignments(db: Session, hub_user_id: int) -> list[dict]:
    """Every `(role, scope)` the caller holds directly — one row per
    assignment, not the expanded closure (plan §2.4). The closure is a
    client-side concern, same as the roles/scopes screen (§8)."""
    rows = (
        db.query(RoleAssignmentV2)
        .filter(RoleAssignmentV2.user_id == hub_user_id)
        .order_by(RoleAssignmentV2.created_at.desc())
        .all()
    )
    return _serialize_many(db, rows)


def list_granted_by_me(db: Session, hub_user_id: int) -> list[dict]:
    """Audit trail: every assignment the caller personally granted, whether
    or not they could still revoke it today (see `list_revocable` for that).
    A delegator losing their own assignment does not cascade to what they
    granted (plan §6.4), so these two lists can genuinely diverge."""
    rows = (
        db.query(RoleAssignmentV2)
        .filter(RoleAssignmentV2.granted_by_user_id == hub_user_id)
        .order_by(RoleAssignmentV2.created_at.desc())
        .all()
    )
    return _serialize_many(db, rows)


def list_revocable(db: Session, hub_user_id: int, *, is_hub_admin: bool) -> list[dict]:
    """Every assignment, ORG-WIDE, the caller is currently eligible to
    revoke under §6.1's symmetric rule (design doc §6.3) — not just the ones
    they granted. A Hub Admin is eligible for all of them, unconditionally,
    same bypass as everywhere else in this phase.

    A plain per-row `can_delegate` call, not a bulk query: both graphs are
    tens of rows (rbac_graph_service's own docstring), so doing this in
    Python rather than as one large SQL join keeps the eligibility rule in
    exactly one place instead of two.
    """
    rows = db.query(RoleAssignmentV2).order_by(RoleAssignmentV2.created_at.desc()).all()
    if not is_hub_admin:
        rows = [r for r in rows if can_delegate(db, hub_user_id, r.role_id, r.scope_id)]
    return _serialize_many(db, rows)


def search_hub_users(db: Session, query: str) -> list[dict]:
    """Target picker (handoff §5.4). Requires a real search term — the
    query itself must already be at least `HUB_USER_SEARCH_MIN_LENGTH`
    characters, enforced by the router's `Query(min_length=...)`, so this
    never has to decide what "no query" should return. Restricted to
    `is_active` rows: an inactive person cannot presently be granted a role
    either way, so surfacing them in the picker only invites a confusing
    `target_not_provisioned`-style failure with no matching error code.
    """
    term = f"%{query.strip().lower()}%"
    rows = (
        db.query(HubUserV2)
        .filter(HubUserV2.is_active.is_(True))
        .filter(or_(HubUserV2.email.ilike(term), HubUserV2.name.ilike(term)))
        .order_by(HubUserV2.email)
        .limit(HUB_USER_SEARCH_LIMIT)
        .all()
    )
    return [{"id": r.id, "email": r.email, "name": r.name} for r in rows]


def get_assignment(db: Session, assignment_id: int) -> RoleAssignmentV2 | None:
    return db.query(RoleAssignmentV2).filter(RoleAssignmentV2.id == assignment_id).first()


# ---------------------------------------------------------
# Writes — delegation gate is asserted by the ROUTER, not here (see module
# docstring); this module only knows how to insert/delete once cleared.
# ---------------------------------------------------------


def create_assignment(
    db: Session,
    *,
    granter_hub_user_id: int,
    granter_email: str,
    target_email: str,
    role_id: int,
    scope_id: int,
) -> dict:
    """Insert `(target, role, scope)`. Caller must already have: validated
    the delegation gate, and confirmed `role_id`/`scope_id` exist (both the
    router's job — see `rbac_assignments.py`).

    Target provisioning is deliberately NOT done here (plan session handoff
    2026-08-24 §3.2): granting requires the target to already hold a
    `hub_users` row, i.e. to have logged in at least once. No
    auto-create-on-grant — that is `target_not_provisioned`, below.
    """
    normalized_target = target_email.strip().lower()
    target = db.query(HubUserV2).filter(HubUserV2.email == normalized_target).first()
    if target is None:
        raise RbacGraphError(
            "target_not_provisioned",
            (
                f"{target_email!r} has not signed in yet, so there is no "
                f"account to grant a role to. They must log in at least "
                f"once first."
            ),
            email=normalized_target,
        )

    existing = (
        db.query(RoleAssignmentV2)
        .filter(
            RoleAssignmentV2.user_id == target.id,
            RoleAssignmentV2.role_id == role_id,
            RoleAssignmentV2.scope_id == scope_id,
        )
        .first()
    )
    if existing is not None:
        raise RbacGraphError(
            "duplicate_assignment",
            f"{target_email!r} already holds that role on that scope.",
            user_id=target.id,
            role_id=role_id,
            scope_id=scope_id,
        )

    assignment = RoleAssignmentV2(
        user_id=target.id,
        role_id=role_id,
        scope_id=scope_id,
        granted_by_user_id=granter_hub_user_id,
        granted_by_email=granter_email,
        created_at=_utc_now(),
    )
    db.add(assignment)
    db.flush()
    return _serialize(assignment, normalized_target)


def delete_assignment(db: Session, assignment_id: int) -> bool:
    """Removing an assignment can never violate any rule — it only ever
    shrinks access — so there is nothing left to validate once the caller's
    delegation gate (router-side) has already passed."""
    assignment = get_assignment(db, assignment_id)
    if assignment is None:
        return False
    db.delete(assignment)
    db.flush()
    return True
