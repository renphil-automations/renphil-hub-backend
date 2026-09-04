"""
Nav tabs router (phase 1 — see AI Docs/plan_nav_tabs_2026-07-28.md).

Router-level authentication on every route. Mutating routes used to also
carry `require_hub_admin` — the propagation engine's per-node
`require_hub_editor` / `require_nav_tab_editor`, replaced with an identity
gate when that engine was removed, since every nav tab's stored `admins`
had already been narrowed to {Hub Admin} by the same migration, making the
two equivalent in practice. That was always a placeholder — dependencies.py's
`require_hub_admin` docstring said so in as many words — pending the new
access control algorithm.

project_ac_enforcement_gap.md's §6.8 nav-tab/hub split (2026-09-04) is that
replacement: every mutating route below now gates on `edit(hub)` /
`edit(nav_tab)` (plan_access_control_algorithm_2026-08-27.md §6.3), the same
node-scoped primitive `tabs_v2.py`'s write routes already use
(project_ac_enforcement_gap.md item 2). `require_hub_admin` itself is
UNCHANGED and still gates role/scope DEFINITION writes (`rbac.py`) — see
that dependency's own docstring on why that half does not move.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, get_viewer_access
from app.schemas.tab import (
    CreateNavTabRequest,
    NavTabListAPIResponse,
    NavTabResponse,
    ReorderNavTabsRequest,
    TabSummaryListAPIResponse,
    UpdateNavTabRequest,
)
from app.services.access_visibility_service import AccessDeniedError, ViewerAccess
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
from app.routers.tabs_v2 import access_denied_to_http_exception, validate_document_id

router = APIRouter(prefix="/v2/nav-tabs", tags=["Nav Tabs V2"], dependencies=[Depends(get_current_user)])


COMMON_BAD_REQUEST_RESPONSE = {400: {"description": "Bad request"}}
COMMON_NOT_FOUND_RESPONSE = {404: {"description": "Requested nav tab was not found"}}
COMMON_CONFLICT_RESPONSE = {409: {"description": "Conflict"}}
COMMON_FORBIDDEN_RESPONSE = {403: {"description": "You do not have edit access to this item"}}


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
def get_nav_tabs(db: Session = Depends(get_db_v2), access: ViewerAccess = Depends(get_viewer_access)):
    return {"data": get_nav_tabs_v2(db, access=access)}


@router.get(
    "/{document_id}/tabs",
    response_model=TabSummaryListAPIResponse,
    summary="Get root tabs scoped to one nav tab",
    responses={**COMMON_NOT_FOUND_RESPONSE},
)
def get_nav_tab_tabs(
    document_id: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    nav_tab = get_nav_tab_by_document_id(db, document_id)
    if nav_tab is None:
        raise HTTPException(status_code=404, detail="Nav tab not found")
    # Same fail-closed convention as the tabs_v2.py list endpoints: listing
    # what is under a nav tab the caller cannot even see would itself leak
    # more than the accepted §5.2 reveal.
    if not access.verdict(("nav_tab", nav_tab.id)).view:
        raise HTTPException(status_code=404, detail="Nav tab not found")

    return {"data": get_root_tabs_v2(db, nav_tab_id=nav_tab.id, access=access)}


@router.post(
    "",
    response_model=NavTabResponse,
    summary="Create a nav tab",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
)
def create_nav_tab(
    request: CreateNavTabRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
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
            access=access,
        )
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/reorder",
    response_model=NavTabListAPIResponse,
    summary="Reorder nav tabs",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_FORBIDDEN_RESPONSE},
)
def reorder_nav_tabs(
    request: ReorderNavTabsRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    try:
        return {
            "data": reorder_nav_tabs_v2(
                db=db, ordered_document_ids=request.orderedDocumentIds, access=access
            )
        }
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
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
)
def update_nav_tab(
    document_id: str,
    request: UpdateNavTabRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
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
            access=access,
            **icon_kwargs,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return updated
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
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
)
def delete_nav_tab(
    document_id: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    try:
        delete_result = delete_nav_tab_v2(db=db, document_id=document_id, access=access)
        if delete_result is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return {"data": delete_result}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)
