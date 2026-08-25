"""Permission Management router
(plan_access_control_schema_2026-08-22.md §6,
session_handoff_2026-08-24-permission-management-plan.md §5).

Assignments — who holds what, and delegating that to others. Distinct from
`app/routers/rbac.py` (role/scope DEFINITIONS, Hub-Admin-gated writes): this
whole surface is open to any authenticated user, gated instead by §6.1's
delegation rule, evaluated per-request against the CALLER's own held
assignments (plus the unconditional Hub Admin bypass, handoff §3.1). No
route here uses `require_hub_admin` — see that dependency's own docstring
for why assignments must not reuse it.

Every route depends on `get_current_hub_user`, not `get_current_user`: this
is the surface that made hub_users auto-provisioning a hard prerequisite
(handoff §3.2) rather than a nice-to-have, since nobody — including the
granter — can be resolved to a row without it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.schemas.rbac_assignments import (
    AssignmentAPIResponse,
    AssignmentListAPIResponse,
    CreateAssignmentRequest,
    HubUserListAPIResponse,
)
from app.services import rbac_assignment_service, rbac_service
from app.services.rbac_delegation_service import assert_can_delegate
from app.services.rbac_graph_service import RbacGraphError
from app.services.tab_service import HUB_ADMIN_ROLE

router = APIRouter(prefix="/v2/rbac", tags=["Permission Management"])

CONFLICT_RESPONSE = {409: {"description": "The delegation rule, or a uniqueness rule, was violated"}}


def _conflict(error: RbacGraphError) -> HTTPException:
    """Same shape as rbac.py's own `_conflict` — duplicated rather than
    imported, since it is five lines and importing it would couple this
    router to that one for no reason beyond avoiding a duplicate. 409 for
    the same reason as there: every one of these is a collision with the
    CURRENT STATE (you don't hold a covering assignment right now, this
    pair already exists), not a malformed request."""
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": error.message, **error.details},
    )


def _is_hub_admin(current: CurrentHubUser) -> bool:
    return HUB_ADMIN_ROLE in (current.info.roles or [])


# ---------------------------------------------------------
# Reads — all "mine", all open to any authenticated user once resolved to a
# hub_users row.
# ---------------------------------------------------------


@router.get("/me/assignments", response_model=AssignmentListAPIResponse, summary="My held assignments")
def get_my_assignments(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    return {"data": rbac_assignment_service.list_my_assignments(db, current.hub_user_id)}


@router.get(
    "/me/granted",
    response_model=AssignmentListAPIResponse,
    summary="Assignments I personally granted (audit trail — may include ones I can no longer revoke)",
)
def get_my_granted(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    return {"data": rbac_assignment_service.list_granted_by_me(db, current.hub_user_id)}


@router.get(
    "/me/revocable",
    response_model=AssignmentListAPIResponse,
    summary="Every assignment, org-wide, I am currently eligible to revoke under §6.1",
)
def get_my_revocable(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Deliberately broader than `/me/granted` (design doc §6.3): revocation
    is symmetric, not ownership-based, so this can include assignments some
    other admin granted. A Hub Admin gets everything, unconditionally."""
    return {
        "data": rbac_assignment_service.list_revocable(
            db, current.hub_user_id, is_hub_admin=_is_hub_admin(current)
        )
    }


@router.get(
    "/hub-users",
    response_model=HubUserListAPIResponse,
    summary="Search people who have logged in at least once (target picker)",
)
def search_hub_users(
    q: str = Query(..., min_length=rbac_assignment_service.HUB_USER_SEARCH_MIN_LENGTH, max_length=320),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Requires a real search term (handoff §5.4's privacy question,
    resolved this way rather than an unrestricted dump): this is a people
    directory, a different kind of exposure than role/scope names."""
    return {"data": rbac_assignment_service.search_hub_users(db, q)}


# ---------------------------------------------------------
# Writes — gated on `assert_can_delegate`, never `require_hub_admin`.
# ---------------------------------------------------------


@router.post(
    "/assignments",
    response_model=AssignmentAPIResponse,
    status_code=201,
    summary="Grant a role on a scope to another user",
    responses={
        404: {"description": "Role or scope not found"},
        **CONFLICT_RESPONSE,
    },
)
def create_assignment(
    request: CreateAssignmentRequest,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """`role_id`/`scope_id` not found stays a plain 404, matching every
    other `/v2/rbac` endpoint — checked BEFORE the delegation gate so a bad
    id never leaks whether it would otherwise have been delegable."""
    if rbac_service.get_role(db, request.role_id) is None:
        raise HTTPException(status_code=404, detail="Role not found")
    if rbac_service.get_scope(db, request.scope_id) is None:
        raise HTTPException(status_code=404, detail="Scope not found")

    try:
        assert_can_delegate(
            db,
            granter_hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            role_id=request.role_id,
            scope_id=request.scope_id,
        )
        assignment = rbac_assignment_service.create_assignment(
            db,
            granter_hub_user_id=current.hub_user_id,
            granter_email=current.email,
            target_email=request.user_email,
            role_id=request.role_id,
            scope_id=request.scope_id,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
    return {"data": assignment}


@router.delete(
    "/assignments/{assignment_id}",
    status_code=204,
    summary="Revoke an assignment",
    responses={
        404: {"description": "Assignment not found"},
        **CONFLICT_RESPONSE,
    },
)
def delete_assignment(
    assignment_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Symmetric with create, not ownership-based (design doc §6.3,
    restated as a landmine in handoff §7): the check re-runs §6.1 against
    the assignment's OWN role/scope, regardless of who originally granted
    it. There is deliberately no `granted_by_user_id == me` shortcut here."""
    assignment = rbac_assignment_service.get_assignment(db, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    try:
        assert_can_delegate(
            db,
            granter_hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            role_id=assignment.role_id,
            scope_id=assignment.scope_id,
        )
        rbac_assignment_service.delete_assignment(db, assignment_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
