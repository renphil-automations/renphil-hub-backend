"""
Phase 3 v2 tabs router — endpoint-for-endpoint mirror of app/routers/tabs.py,
bound to the normalized tabs/gridstacks/components/page_content schema
(app.db_v2) instead of the original schema. Reuses the same Pydantic
response/request schemas and the same router helpers (validate_document_id,
value_error_to_http_exception) since both are schema-agnostic.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_optional_user
from app.models.auth import UserInfo
from app.routers.tabs import validate_document_id, value_error_to_http_exception
from app.schemas.page_content import PageContentAPIResponse
from app.schemas.tab import (
    CreateTabRequest,
    LockTabRequest,
    MoveTabRequest,
    ReorderTabsRequest,
    TabSummaryListAPIResponse,
    TabSummaryResponse,
    TabWorkspaceAPIResponse,
    UnlockTabRequest,
    UpdateTabContentRequest,
    UpdateTabRequest,
)
from app.services.gridstack_service import (
    create_tab_v2,
    delete_tab_subtree_by_document_id_v2,
    get_component_by_link_v2,
    get_root_tabs_v2,
    get_tab_children_v2,
    get_tab_content_v2,
    get_tab_workspace_v2,
    lock_tab_by_document_id_v2,
    move_tab_by_document_id_v2,
    reorder_tabs_by_document_id_v2,
    resolve_component_location_v2,
    unlock_tab_by_document_id_v2,
    update_tab_by_document_id_v2,
    update_tab_content_v2,
)
from app.services.tab_service import filter_widget_content_for_user

router = APIRouter(prefix="/v2/tabs", tags=["Tabs V2"])


COMMON_BAD_REQUEST_RESPONSE = {400: {"description": "Bad request"}}
COMMON_NOT_FOUND_RESPONSE = {404: {"description": "Requested tab or resource was not found"}}
COMMON_CONFLICT_RESPONSE = {409: {"description": "Conflict"}}


@router.put("/root", include_in_schema=False)
@router.post("/root", include_in_schema=False)
@router.delete("/root", include_in_schema=False)
@router.patch("/root", include_in_schema=False)
def root_method_not_allowed():
    raise HTTPException(
        status_code=405,
        detail="Method not allowed for /v2/tabs/root",
        headers={"Allow": "GET"},
    )


@router.delete("/reorder", include_in_schema=False)
@router.post("/reorder", include_in_schema=False)
@router.get("/reorder", include_in_schema=False)
@router.patch("/reorder", include_in_schema=False)
def reorder_method_not_allowed():
    raise HTTPException(
        status_code=405,
        detail="Method not allowed for /v2/tabs/reorder",
        headers={"Allow": "PUT"},
    )


@router.get("/root", response_model=TabSummaryListAPIResponse, summary="Get root tabs (v2)")
def get_roots(db: Session = Depends(get_db_v2)):
    return {"data": get_root_tabs_v2(db)}


@router.get(
    "/components/by-link/{link}",
    summary="Resolve a component by its stable link (v2)",
    description="Backs the mirror target picker's 'paste a link' flow — "
    "resolves a component's current type/title/data directly by its stable "
    "`link`, without needing to browse to the tab that contains it.",
    responses={**COMMON_NOT_FOUND_RESPONSE},
)
def get_component_by_link(link: str, db: Session = Depends(get_db_v2)):
    result = get_component_by_link_v2(db, link)
    if result is None:
        raise HTTPException(status_code=404, detail="Component not found, or cannot be mirrored")
    return {"data": result}


@router.get(
    "/components/by-link/{link}/location",
    summary="Resolve a component's navigable location by its stable link (v2)",
    description="Backs the mirror widget's 'jump to original' affordance — "
    "resolves the root tab, the ordered chain of ancestor sub-tab "
    "`document_id`s, and (if the component is a Super Block Note "
    "descendant) the ordered chain of ancestor SBN component `link`s, so "
    "the frontend can navigate there and highlight the component.",
    responses={**COMMON_NOT_FOUND_RESPONSE},
)
def get_component_location(link: str, db: Session = Depends(get_db_v2)):
    result = resolve_component_location_v2(db, link)
    if result is None:
        raise HTTPException(status_code=404, detail="Component not found, or cannot be located")
    return {"data": result}


@router.get(
    "/{document_id}/workspace",
    response_model=TabWorkspaceAPIResponse,
    summary="Get tab workspace (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_workspace(
    document_id: str,
    db: Session = Depends(get_db_v2),
    user: UserInfo | None = Depends(get_optional_user),
):
    validate_document_id(document_id)

    workspace = get_tab_workspace_v2(db, document_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Tab not found")

    if user is not None:
        page_content = workspace.get("page_content")
        if isinstance(page_content, dict):
            raw_content = page_content.get("content")
            filtered = filter_widget_content_for_user(raw_content, user.email, list(user.roles))
            if filtered is not raw_content:
                workspace = {**workspace, "page_content": {**page_content, "content": filtered}}

    return {"data": workspace}


@router.get(
    "/{document_id}/children",
    response_model=TabSummaryListAPIResponse,
    summary="Get direct child tabs (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_children(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    children = get_tab_children_v2(db, document_id)
    if children is None:
        raise HTTPException(status_code=404, detail="Parent tab not found")

    return {"data": children}


@router.get(
    "/{document_id}/content",
    response_model=PageContentAPIResponse,
    summary="Get tab page content (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_content(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    content = get_tab_content_v2(db, document_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Tab or page content not found")

    return {"data": content}


@router.put(
    "/{document_id}/content",
    response_model=PageContentAPIResponse,
    summary="Update tab page content (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def update_content(
    document_id: str,
    request: UpdateTabContentRequest,
    db: Session = Depends(get_db_v2),
):
    validate_document_id(document_id)

    try:
        updated_content = update_tab_content_v2(db=db, document_id=document_id, content=request.content)
        if updated_content is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return {"data": updated_content}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.post(
    "/",
    response_model=TabSummaryResponse,
    summary="Create a new tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def create_new_tab(request: CreateTabRequest, db: Session = Depends(get_db_v2)):
    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )

        return create_tab_v2(
            db=db,
            title=request.title,
            parent_document_id=request.parentDocumentId,
            content=request.content,
            order=request.order,
            access_control=access_control,
        )
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/reorder",
    response_model=TabSummaryListAPIResponse,
    summary="Reorder sibling tabs (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def reorder_tabs(request: ReorderTabsRequest, db: Session = Depends(get_db_v2)):
    try:
        reordered = reorder_tabs_by_document_id_v2(
            db=db,
            items=[item.model_dump() for item in request.items],
        )
        return {"data": reordered}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/lock",
    response_model=TabWorkspaceAPIResponse,
    summary="Lock tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def lock_tab(document_id: str, request: LockTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        locked_workspace = lock_tab_by_document_id_v2(db=db, document_id=document_id, locked_by=request.locked_by)
        if locked_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return {"data": locked_workspace}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/unlock",
    response_model=TabWorkspaceAPIResponse,
    summary="Unlock tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def unlock_tab(document_id: str, request: UnlockTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        unlocked_workspace = unlock_tab_by_document_id_v2(
            db=db,
            document_id=document_id,
            unlocked_by=request.unlocked_by,
            force=request.force,
        )
        if unlocked_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return {"data": unlocked_workspace}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}",
    response_model=TabWorkspaceAPIResponse,
    summary="Update tab metadata (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def update_tab_metadata(document_id: str, request: UpdateTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )

        updated_workspace = update_tab_by_document_id_v2(
            db=db,
            document_id=document_id,
            title=request.title,
            order=request.order,
            access_control=access_control,
            locked=request.locked,
            locked_by=request.locked_by,
        )
        if updated_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return {"data": updated_workspace}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/move",
    response_model=TabWorkspaceAPIResponse,
    summary="Move tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def move_tab(document_id: str, request: MoveTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        moved_workspace = move_tab_by_document_id_v2(
            db=db,
            document_id=document_id,
            new_parent_document_id=request.newParentDocumentId,
            order=request.order,
        )
        if moved_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return {"data": moved_workspace}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.delete(
    "/{document_id}",
    summary="Delete tab subtree (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def delete_tab(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        delete_result = delete_tab_subtree_by_document_id_v2(db=db, document_id=document_id)
        if delete_result is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return {"data": delete_result}
    except ValueError as e:
        raise value_error_to_http_exception(e)
