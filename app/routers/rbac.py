"""Access Management router
(plan_access_control_schema_2026-08-22.md §3, §5).

The v1 surface: role and scope DEFINITIONS plus the two hierarchies.
Assignments — and the §6 delegation rule governing them — are a later phase,
so every write here is gated on Hub Admin and nothing consults the caller's
own (role, scope) envelope.

Reads are open to any authenticated user. Hiding half a hierarchy makes it
unreadable, and neither role nor scope NAMES are sensitive; what is
sensitive is who holds what, which lives on the assignments surface this
router does not yet expose.

Every rule violation returns 409 with a machine-readable `code`. That is the
main product of this API: building a role graph by hand is mostly a
conversation with the validator, so the errors name both endpoints and both
ranks rather than just refusing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, require_hub_admin
from app.schemas.rbac import (
    CreateRoleRequest,
    CreateScopeRequest,
    RoleAPIResponse,
    RoleListAPIResponse,
    ScopeAPIResponse,
    ScopeListAPIResponse,
    UpdateRoleRequest,
    UpdateScopeRequest,
)
from app.services import rbac_service
from app.services.rbac_graph_service import (
    RbacGraphError,
    create_role_edge,
    create_scope_edge,
    delete_role_edge,
    delete_scope_edge,
)

router = APIRouter(
    prefix="/v2/rbac",
    tags=["Access Management"],
    dependencies=[Depends(get_current_user)],
)

ADMIN_ONLY = [Depends(require_hub_admin)]

FORBIDDEN_RESPONSE = {403: {"description": "Hub Admin access required"}}
CONFLICT_RESPONSE = {409: {"description": "A graph or uniqueness rule was violated"}}


def _conflict(error: RbacGraphError) -> HTTPException:
    """Map a validator failure onto 409 with its full payload.

    409 rather than 400 throughout: every one of these is a collision with
    the CURRENT STATE of the graph (a cycle, an inverted rank, a name
    already taken), not a malformed request — a body that is rejected today
    may be accepted tomorrow once the conflicting edge is gone. Malformed
    bodies are still 422, handled by Pydantic before reaching here.
    """
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": error.message, **error.details},
    )


# ---------------------------------------------------------
# Roles
# ---------------------------------------------------------


@router.get("/roles", response_model=RoleListAPIResponse, summary="List every role")
def list_roles(db: Session = Depends(get_db_v2)):
    """Each row carries its DIRECT parent and child ids, so the whole roles
    screen — including both pickers — renders from this one request."""
    return {"data": rbac_service.list_roles(db)}


@router.get(
    "/roles/{role_id}",
    response_model=RoleAPIResponse,
    summary="Get one role",
    responses={404: {"description": "Role not found"}},
)
def get_role(role_id: int, db: Session = Depends(get_db_v2)):
    role = rbac_service.get_role(db, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return {"data": role}


@router.post(
    "/roles",
    response_model=RoleAPIResponse,
    status_code=201,
    summary="Create a role",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def create_role(request: CreateRoleRequest, db: Session = Depends(get_db_v2)):
    try:
        role = rbac_service.create_role(
            db,
            key=request.key,
            name=request.name,
            description=request.description,
            rank=request.rank,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
    return {"data": role}


@router.patch(
    "/roles/{role_id}",
    response_model=RoleAPIResponse,
    summary="Rename a role, or change its description or rank",
    responses={
        404: {"description": "Role not found"},
        **FORBIDDEN_RESPONSE,
        **CONFLICT_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
def update_role(role_id: int, request: UpdateRoleRequest, db: Session = Depends(get_db_v2)):
    """A rank change that would invert an existing edge returns 409
    `rank_change_conflict` listing every offending edge — refusing without
    naming them is useless to whoever has to fix it (§5.3)."""
    try:
        role = rbac_service.update_role(
            db,
            role_id,
            name=request.name,
            description=request.description,
            rank=request.rank,
            description_provided="description" in request.model_fields_set,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    db.commit()
    return {"data": role}


@router.delete(
    "/roles/{role_id}",
    status_code=204,
    summary="Delete a role",
    responses={
        404: {"description": "Role not found"},
        **FORBIDDEN_RESPONSE,
        **CONFLICT_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
def delete_role(role_id: int, db: Session = Depends(get_db_v2)):
    try:
        deleted = rbac_service.delete_role(db, role_id)
    except RbacGraphError as e:
        raise _conflict(e)
    if not deleted:
        raise HTTPException(status_code=404, detail="Role not found")
    db.commit()


@router.post(
    "/roles/{parent_id}/children/{child_id}",
    status_code=204,
    summary="Make one role inherit another",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def add_role_edge(parent_id: int, child_id: int, db: Session = Depends(get_db_v2)):
    """`parent` inherits everything `child` has. Requires
    `parent.rank < child.rank`, which is what blocks the inverted edge and
    also makes cycles structurally impossible (§5.2)."""
    try:
        create_role_edge(db, parent_id, child_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()


@router.delete(
    "/roles/{parent_id}/children/{child_id}",
    status_code=204,
    summary="Remove a role inheritance edge",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def remove_role_edge(parent_id: int, child_id: int, db: Session = Depends(get_db_v2)):
    try:
        delete_role_edge(db, parent_id, child_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()


# ---------------------------------------------------------
# Scopes
# ---------------------------------------------------------


@router.get("/scopes", response_model=ScopeListAPIResponse, summary="List every scope")
def list_scopes(db: Session = Depends(get_db_v2)):
    return {"data": rbac_service.list_scopes(db)}


@router.get(
    "/scopes/{scope_id}",
    response_model=ScopeAPIResponse,
    summary="Get one scope",
    responses={404: {"description": "Scope not found"}},
)
def get_scope(scope_id: int, db: Session = Depends(get_db_v2)):
    scope = rbac_service.get_scope(db, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    return {"data": scope}


@router.post(
    "/scopes",
    response_model=ScopeAPIResponse,
    status_code=201,
    summary="Create a scope",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def create_scope(request: CreateScopeRequest, db: Session = Depends(get_db_v2)):
    """`is_universal` is set here or never — it cannot be changed later
    (see UpdateScopeRequest for why neither direction has a migration)."""
    try:
        scope = rbac_service.create_scope(
            db,
            key=request.key,
            name=request.name,
            description=request.description,
            is_universal=request.is_universal,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
    return {"data": scope}


@router.patch(
    "/scopes/{scope_id}",
    response_model=ScopeAPIResponse,
    summary="Rename a scope or change its description",
    responses={
        404: {"description": "Scope not found"},
        **FORBIDDEN_RESPONSE,
        **CONFLICT_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
def update_scope(scope_id: int, request: UpdateScopeRequest, db: Session = Depends(get_db_v2)):
    try:
        scope = rbac_service.update_scope(
            db,
            scope_id,
            name=request.name,
            description=request.description,
            description_provided="description" in request.model_fields_set,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    db.commit()
    return {"data": scope}


@router.delete(
    "/scopes/{scope_id}",
    status_code=204,
    summary="Delete a scope",
    responses={
        404: {"description": "Scope not found"},
        **FORBIDDEN_RESPONSE,
        **CONFLICT_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
def delete_scope(scope_id: int, db: Session = Depends(get_db_v2)):
    try:
        deleted = rbac_service.delete_scope(db, scope_id)
    except RbacGraphError as e:
        raise _conflict(e)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scope not found")
    db.commit()


@router.post(
    "/scopes/{parent_id}/children/{child_id}",
    status_code=204,
    summary="Put one scope inside another",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def add_scope_edge(parent_id: int, child_id: int, db: Session = Depends(get_db_v2)):
    """Holding `parent` covers `child`. Unlike the role graph this one has
    no rank, so the call takes an advisory lock and walks for cycles
    (§5.4, §5.6). A scope may sit inside several containers at once."""
    try:
        create_scope_edge(db, parent_id, child_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()


@router.delete(
    "/scopes/{parent_id}/children/{child_id}",
    status_code=204,
    summary="Remove a scope containment edge",
    responses={**FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def remove_scope_edge(parent_id: int, child_id: int, db: Session = Depends(get_db_v2)):
    """Once assignments exist this grows a warning: narrowing a composite
    scope can strand grants made when it was wider. It cannot today."""
    try:
        delete_scope_edge(db, parent_id, child_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
