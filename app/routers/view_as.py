"""View As pickers — the scopes, then the roles on a scope, that the caller
may adopt through the ``view_as_role`` / ``view_as_scope`` parameters of the
hub's read endpoints. Rules: ``app/services/view_as_service.py``."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.schemas.view_as import ViewAsRoleListAPIResponse, ViewAsScopeListAPIResponse
from app.services import view_as_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2/view-as", tags=["View As"])


def _user_id(db: Session, current: CurrentHubUser) -> int:
    user_id = view_as_service.resolve_user_id(db, current.email)
    return current.hub_user_id if user_id is None else user_id


@router.get(
    "/scopes",
    response_model=ViewAsScopeListAPIResponse,
    summary="Scopes the caller may adopt in View As",
)
def list_view_as_scopes(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """The caller's assigned scopes, all their direct and indirect child
    scopes, and the public scope — unique, sorted by name."""
    return {"data": view_as_service.list_viewable_scopes(db, _user_id(db, current))}


@router.get(
    "/scopes/{scope_id}/roles",
    response_model=ViewAsRoleListAPIResponse,
    summary="Roles the caller may adopt in View As on one scope",
    responses={
        403: {"description": "The caller may not view the hub as this scope"},
        404: {"description": "Scope not found"},
    },
)
def list_view_as_roles(
    scope_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """The roles the caller holds on this scope (directly or through an
    ancestor scope), all their direct and indirect child roles, and the
    public role — unique, sorted by name."""
    user_id = _user_id(db, current)
    try:
        roles = view_as_service.list_viewable_roles(db, user_id, scope_id)
    except view_as_service.ViewAsError as e:
        logger.warning("View As roles refused for %s (user %s) on scope %s: %s", current.email, user_id, scope_id, e.code)
        raise HTTPException(status_code=e.status_code, detail={"code": e.code, "message": e.message})
    return {"scope_id": scope_id, "data": roles}
