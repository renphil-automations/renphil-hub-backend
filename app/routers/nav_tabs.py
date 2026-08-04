"""
Nav tabs router (phase 1 — see AI Docs/plan_nav_tabs_2026-07-28.md; gate
split in phase 2, see AI Docs/plan_nav_tabs_phase2_2026-07-30.md §5.3).

Router-level authentication, plus a per-endpoint access-control gate on
every mutating route: require_hub_editor for collection-level operations
(create, reorder — there's no single node to ask about yet), and
require_nav_tab_editor for per-node operations (rename / AC / delete on
one specific nav tab — a hub-wide gate would let a principal blocked from
that node act on it anyway, since it never consulted the node itself).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, require_hub_editor, require_nav_tab_editor
from app.schemas.tab import (
    CreateNavTabRequest,
    NavTabListAPIResponse,
    NavTabResponse,
    PreviewAccessWriteRequest,
    PurgePrincipalRequest,
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
    preview_nav_tab_access_v2,
    purge_nav_tab_principal_v2,
    reorder_nav_tabs_v2,
    reset_nav_tab_access_v2,
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
    dependencies=[Depends(require_hub_editor)],
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
            icon=request.icon,
        )
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/reorder",
    response_model=NavTabListAPIResponse,
    summary="Reorder nav tabs",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_hub_editor)],
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
    dependencies=[Depends(require_nav_tab_editor)],
)
def update_nav_tab(document_id: str, request: UpdateNavTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )
        # model_fields_set distinguishes "icon absent" (leave alone) from
        # "icon explicitly null" (clear to the default icon) — the same
        # three-way the airtable `pat` field already needs for its own
        # omit/set/clear distinction.
        icon_kwargs = {"icon": request.icon} if "icon" in request.model_fields_set else {}
        updated = update_nav_tab_v2(
            db=db,
            document_id=document_id,
            title=request.title,
            order=request.order,
            access_control=access_control,
            **icon_kwargs,
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
    dependencies=[Depends(require_nav_tab_editor)],
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


@router.post(
    "/{document_id}/access/preview",
    summary="Preview the blast radius of a nav-tab access-control write",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_nav_tab_editor)],
)
def preview_nav_tab_access(
    document_id: str, request: PreviewAccessWriteRequest, db: Session = Depends(get_db_v2)
):
    validate_document_id(document_id)
    access_control = (
        request.access_control.model_dump()
        if hasattr(request.access_control, "model_dump")
        else request.access_control
    )
    preview = preview_nav_tab_access_v2(db, document_id, access_control)
    if preview is None:
        raise HTTPException(status_code=404, detail="Nav tab not found")
    return preview


@router.post(
    "/{document_id}/access/purge",
    summary="Purge a principal from a nav tab and its entire subtree",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_nav_tab_editor)],
)
def purge_nav_tab_principal(
    document_id: str, request: PurgePrincipalRequest, db: Session = Depends(get_db_v2)
):
    validate_document_id(document_id)
    try:
        result = purge_nav_tab_principal_v2(db, document_id, request.principal.model_dump())
        if result is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return result
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.post(
    "/{document_id}/access/reset",
    summary="Reset a nav tab's access control to inherit from the hub",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_nav_tab_editor)],
)
def reset_nav_tab_access(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)
    try:
        result = reset_nav_tab_access_v2(db, document_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return result
    except ValueError as e:
        raise value_error_to_http_exception(e)
