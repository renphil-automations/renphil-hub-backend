"""
Nav tabs router (phase 1 — see AI Docs/plan_nav_tabs_2026-07-28.md).

Router-level authentication like tabs_v2.py, plus per-endpoint
require_hub_admin on every mutating route — the first authorization check on
any tab-family endpoint (see app.dependencies.require_hub_admin).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, require_hub_admin
from app.schemas.tab import (
    CreateNavTabRequest,
    NavTabListAPIResponse,
    NavTabResponse,
    ReorderNavTabsRequest,
    TabSummaryListAPIResponse,
    UpdateNavTabRequest,
)
from app.services.gridstack_service import get_root_tabs_v2
from app.services.nav_tab_service import (
    create_nav_tab_v2,
    delete_nav_tab_v2,
    get_nav_tab_by_document_id,
    get_nav_tabs_v2,
    reorder_nav_tabs_v2,
    update_nav_tab_v2,
)
from app.routers.tabs import value_error_to_http_exception
from app.routers.tabs_v2 import validate_document_id

router = APIRouter(prefix="/v2/nav-tabs", tags=["Nav Tabs V2"], dependencies=[Depends(get_current_user)])


COMMON_BAD_REQUEST_RESPONSE = {400: {"description": "Bad request"}}
COMMON_NOT_FOUND_RESPONSE = {404: {"description": "Requested nav tab was not found"}}
COMMON_CONFLICT_RESPONSE = {409: {"description": "Conflict"}}
COMMON_FORBIDDEN_RESPONSE = {403: {"description": "Hub Admin role required"}}


# Declared before /{document_id} so "reorder" is never captured as a
# documentId path param, exactly as tabs_v2.py does for /v2/tabs/reorder.
@router.delete("/reorder", include_in_schema=False)
@router.post("/reorder", include_in_schema=False)
@router.get("/reorder", include_in_schema=False)
@router.patch("/reorder", include_in_schema=False)
def reorder_method_not_allowed():
    raise HTTPException(
        status_code=405,
        detail="Method not allowed for /v2/nav-tabs/reorder",
        headers={"Allow": "PUT"},
    )


@router.get("", response_model=NavTabListAPIResponse, summary="Get all nav tabs")
def get_nav_tabs(db: Session = Depends(get_db_v2)):
    return {"data": get_nav_tabs_v2(db)}


@router.get(
    "/{document_id}/tabs",
    response_model=TabSummaryListAPIResponse,
    summary="Get root tabs scoped to one nav tab",
    responses={**COMMON_NOT_FOUND_RESPONSE},
)
def get_nav_tab_tabs(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    nav_tab = get_nav_tab_by_document_id(db, document_id)
    if nav_tab is None:
        raise HTTPException(status_code=404, detail="Nav tab not found")

    return {"data": get_root_tabs_v2(db, nav_tab_id=nav_tab.id)}


@router.post(
    "",
    response_model=NavTabResponse,
    summary="Create a nav tab",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_CONFLICT_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_hub_admin)],
)
def create_nav_tab(request: CreateNavTabRequest, db: Session = Depends(get_db_v2)):
    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )
        return create_nav_tab_v2(
            db=db,
            title=request.title,
            access_control=access_control,
            order=request.order,
        )
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/reorder",
    response_model=NavTabListAPIResponse,
    summary="Reorder nav tabs",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_hub_admin)],
)
def reorder_nav_tabs(request: ReorderNavTabsRequest, db: Session = Depends(get_db_v2)):
    try:
        return {"data": reorder_nav_tabs_v2(db=db, ordered_document_ids=request.orderedDocumentIds)}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}",
    response_model=NavTabResponse,
    summary="Update a nav tab",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
    },
    dependencies=[Depends(require_hub_admin)],
)
def update_nav_tab(document_id: str, request: UpdateNavTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )
        updated = update_nav_tab_v2(
            db=db,
            document_id=document_id,
            title=request.title,
            order=request.order,
            access_control=access_control,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return updated
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.delete(
    "/{document_id}",
    summary="Delete a nav tab and every root tab inside it",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
    },
    dependencies=[Depends(require_hub_admin)],
)
def delete_nav_tab(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        delete_result = delete_nav_tab_v2(db=db, document_id=document_id)
        if delete_result is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return {"data": delete_result}
    except ValueError as e:
        raise value_error_to_http_exception(e)
