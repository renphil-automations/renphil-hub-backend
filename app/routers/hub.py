"""
Hub router (phase 2 — see AI Docs/plan_nav_tabs_phase2_2026-07-30.md §5.4).
No list endpoint: HubV2 is a one-row singleton, looked up by
`access_control_service.get_hub` (order_by(id).first()), never by id.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, require_hub_editor
from app.routers.tabs import value_error_to_http_exception
from app.schemas.tab import (
    HubAPIResponse,
    PreviewAccessWriteRequest,
    PurgePrincipalRequest,
    UpdateHubRequest,
)
from app.services.hub_service import (
    get_hub_v2,
    preview_hub_access_v2,
    purge_hub_principal_v2,
    update_hub_v2,
)

router = APIRouter(prefix="/v2/hub", tags=["Hub V2"], dependencies=[Depends(get_current_user)])


COMMON_NOT_FOUND_RESPONSE = {404: {"description": "Hub row does not exist"}}
COMMON_FORBIDDEN_RESPONSE = {403: {"description": "Hub editor access required"}}


@router.get(
    "",
    response_model=HubAPIResponse,
    summary="Get the hub",
    responses={**COMMON_NOT_FOUND_RESPONSE},
)
def get_hub(db: Session = Depends(get_db_v2)):
    hub = get_hub_v2(db)
    if hub is None:
        raise HTTPException(status_code=404, detail="Hub row does not exist")
    return {"data": hub}


@router.put(
    "",
    response_model=HubAPIResponse,
    summary="Update the hub's access control",
    responses={**COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_hub_editor)],
)
def update_hub(request: UpdateHubRequest, db: Session = Depends(get_db_v2)):
    access_control = (
        request.access_control.model_dump()
        if hasattr(request.access_control, "model_dump")
        else request.access_control
    )
    try:
        updated = update_hub_v2(db, access_control)
        if updated is None:
            raise HTTPException(status_code=404, detail="Hub row does not exist")
        return {"data": updated}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.post(
    "/preview",
    summary="Preview the blast radius of a hub access-control write",
    responses={**COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_hub_editor)],
)
def preview_hub_access(request: PreviewAccessWriteRequest, db: Session = Depends(get_db_v2)):
    access_control = (
        request.access_control.model_dump()
        if hasattr(request.access_control, "model_dump")
        else request.access_control
    )
    preview = preview_hub_access_v2(db, access_control)
    if preview is None:
        raise HTTPException(status_code=404, detail="Hub row does not exist")
    return preview


@router.post(
    "/access/purge",
    summary="Purge a principal from the hub and its entire subtree",
    responses={**COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_hub_editor)],
)
def purge_hub_principal(request: PurgePrincipalRequest, db: Session = Depends(get_db_v2)):
    try:
        result = purge_hub_principal_v2(db, request.principal.model_dump())
        if result is None:
            raise HTTPException(status_code=404, detail="Hub row does not exist")
        return result
    except ValueError as e:
        raise value_error_to_http_exception(e)
