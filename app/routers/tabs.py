from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.page_content import PageContentAPIResponse
from app.schemas.tab import (
    BreadcrumbAPIResponse,
    CreateTabRequest,
    TabSummaryListAPIResponse,
    TabSummaryResponse,
    TabWorkspaceAPIResponse,
    UpdateTabContentRequest,
    UpdateTabRequest,
    MoveTabRequest,
    ReorderTabsRequest,
    LockTabRequest,
    UnlockTabRequest,
)
from app.services.tab_service import (
    create_tab,
    get_all_tabs_schema_map,
    get_root_tabs,
    get_tab_breadcrumb,
    get_tab_children,
    get_tab_content,
    get_tab_workspace,
    update_tab_by_document_id,
    update_tab_content,
    move_tab_by_document_id,
    reorder_tabs_by_document_id,
    lock_tab_by_document_id,
    unlock_tab_by_document_id,
    delete_tab_subtree_by_document_id,
)

router = APIRouter(prefix="/tabs", tags=["Tabs"])


# ---------------------------------------------------------
# Router helpers
# ---------------------------------------------------------

RESERVED_TAB_DOCUMENT_IDS = {
    "root",
    "reorder",
    "schema-map",
}


COMMON_BAD_REQUEST_RESPONSE = {
    400: {"description": "Bad request"}
}

COMMON_NOT_FOUND_RESPONSE = {
    404: {"description": "Requested tab or resource was not found"}
}

COMMON_CONFLICT_RESPONSE = {
    409: {"description": "Conflict"}
}


def validate_document_id(document_id: str) -> None:
    """
    Validate path document_id before it reaches service/database logic.
    """
    if document_id in RESERVED_TAB_DOCUMENT_IDS:
        raise HTTPException(
            status_code=405,
            detail=f"Method not allowed for reserved tabs route: {document_id}",
            headers={"Allow": "GET"},
        )

    if document_id is None or not document_id.strip():
        raise HTTPException(
            status_code=404,
            detail="Tab not found",
        )

    if len(document_id) > 255:
        raise HTTPException(
            status_code=404,
            detail="Tab not found",
        )

    if any(ord(char) < 32 or ord(char) == 127 for char in document_id):
        raise HTTPException(
            status_code=404,
            detail="Tab not found",
        )


def value_error_to_http_exception(error: ValueError) -> HTTPException:
    """
    Convert service-layer ValueError into clear HTTP errors.
    """
    message = str(error)
    lower_message = message.lower()

    if "already exists" in lower_message or "duplicate" in lower_message:
        return HTTPException(status_code=409, detail=message)

    if "does not exist" in lower_message or "not found" in lower_message:
        return HTTPException(status_code=404, detail=message)

    return HTTPException(status_code=400, detail=message)


# ---------------------------------------------------------
# Hidden unsupported-method handlers for static routes
# ---------------------------------------------------------

@router.put("/root", include_in_schema=False)
@router.post("/root", include_in_schema=False)
@router.delete("/root", include_in_schema=False)
@router.patch("/root", include_in_schema=False)
def root_method_not_allowed():
    raise HTTPException(
        status_code=405,
        detail="Method not allowed for /tabs/root",
        headers={"Allow": "GET"},
    )


@router.delete("/reorder", include_in_schema=False)
@router.post("/reorder", include_in_schema=False)
@router.get("/reorder", include_in_schema=False)
@router.patch("/reorder", include_in_schema=False)
def reorder_method_not_allowed():
    raise HTTPException(
        status_code=405,
        detail="Method not allowed for /tabs/reorder",
        headers={"Allow": "PUT"},
    )


# ---------------------------------------------------------
# Layer 1 read endpoints
# ---------------------------------------------------------

@router.get(
    "/root",
    response_model=TabSummaryListAPIResponse,
    summary="Get root tabs",
    description="""
Returns only root-level tabs.

This is the first endpoint the UI should call when loading the sidebar/tree.

It returns lightweight tab summaries only:
- documentId
- title
- order
- locked
- locked_by
- has_children
- has_content

It does not load page content and does not load the full tree.
""",
    response_description="Root tabs wrapped in a data object",
)
def get_roots(db: Session = Depends(get_db)):
    return {
        "data": get_root_tabs(db)
    }


@router.get(
    "/{document_id}/workspace",
    response_model=TabWorkspaceAPIResponse,
    summary="Get tab workspace",
    description="""
Returns everything the UI needs when opening one tab.

This endpoint loads:
- selected tab fields
- parent summary
- page content
- access_control
- locked / locked_by
- direct children only

It does not recursively load grandchildren.
When the user clicks a child tab, the UI should call this endpoint again
using the child documentId.
""",
    response_description="Selected tab workspace wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def get_workspace(
    document_id: str,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    workspace = get_tab_workspace(db, document_id)

    if workspace is None:
        raise HTTPException(status_code=404, detail="Tab not found")

    return {
        "data": workspace
    }


@router.get(
    "/{document_id}/children",
    response_model=TabSummaryListAPIResponse,
    summary="Get direct child tabs",
    description="""
Returns only the direct children of one tab.

This is useful if the UI wants to expand a branch in the sidebar
without loading the selected tab content.
""",
    response_description="Direct child tabs wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def get_children(
    document_id: str,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    children = get_tab_children(db, document_id)

    if children is None:
        raise HTTPException(status_code=404, detail="Parent tab not found")

    return {
        "data": children
    }


@router.get(
    "/{document_id}/content",
    response_model=PageContentAPIResponse,
    summary="Get tab page content",
    description="""
Returns only the page content linked to one tab.

Use this when the UI/editor needs to refresh content without reloading
children or parent information.
""",
    response_description="Page content wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def get_content(
    document_id: str,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    content = get_tab_content(db, document_id)

    if content is None:
        raise HTTPException(status_code=404, detail="Tab or page content not found")

    return {
        "data": content
    }


@router.put(
    "/{document_id}/content",
    response_model=PageContentAPIResponse,
    summary="Update tab page content",
    description="""
Updates the page content linked to one tab.

This endpoint is intended for editor save/autosave behavior.

If the tab exists but does not have page_content yet,
the backend creates the missing page_content and link safely.
""",
    response_description="Updated page content wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def update_content(
    document_id: str,
    request: UpdateTabContentRequest,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    try:
        updated_content = update_tab_content(
            db=db,
            document_id=document_id,
            content=request.content,
        )

        if updated_content is None:
            raise HTTPException(status_code=404, detail="Tab not found")

        return {
            "data": updated_content
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.get(
    "/{document_id}/breadcrumb",
    response_model=BreadcrumbAPIResponse,
    summary="Get tab breadcrumb",
    description="""
Returns the path from the root tab to the selected tab.

Example:
Comms > Content and Guidance > Guide: Headshots

This helps the UI display clear navigation without loading the full tree.
""",
    response_description="Breadcrumb items wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def get_breadcrumb(
    document_id: str,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    breadcrumb = get_tab_breadcrumb(db, document_id)

    if breadcrumb is None:
        raise HTTPException(status_code=404, detail="Tab not found")

    return {
        "data": breadcrumb
    }


# ---------------------------------------------------------
# Create endpoint
# ---------------------------------------------------------

@router.post(
    "/",
    response_model=TabSummaryResponse,
    summary="Create a new tab",
    description="""
Creates a new tab using documentId-based relationships.

This creates:
- tab row
- page_content row
- tab/page_content link
- optional parent/child link

If parentDocumentId is null, the tab is created as a root tab.
""",
    response_description="Created tab summary",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def create_new_tab(
    request: CreateTabRequest,
    db: Session = Depends(get_db),
):
    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )

        return create_tab(
            db=db,
            title=request.title,
            parent_document_id=request.parentDocumentId,
            content=request.content,
            order=request.order,
            access_control=access_control,
        )

    except ValueError as e:
        raise value_error_to_http_exception(e)


# ---------------------------------------------------------
# Update / reorder / lock endpoints
# ---------------------------------------------------------

@router.put(
    "/reorder",
    response_model=TabSummaryListAPIResponse,
    summary="Reorder sibling tabs",
    description="""
Updates the order of tabs inside the same parent level.

This endpoint does not move tabs to another parent.
Use /tabs/{document_id}/move for parent changes.

Rules:
- all documentIds must exist
- all tabs must belong to the same parent level
- duplicate documentIds are rejected
""",
    response_description="Updated sibling tabs wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def reorder_tabs(
    request: ReorderTabsRequest,
    db: Session = Depends(get_db),
):
    try:
        reordered_tabs = reorder_tabs_by_document_id(
            db=db,
            items=[
                item.model_dump()
                for item in request.items
            ],
        )

        return {
            "data": reordered_tabs
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/lock",
    response_model=TabWorkspaceAPIResponse,
    summary="Lock tab",
    description="""
Locks a tab for editing.

If the tab is already locked by another user, the request is rejected.
If the same user calls lock again, the lock is refreshed safely.
""",
    response_description="Locked tab workspace wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def lock_tab(
    document_id: str,
    request: LockTabRequest,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    try:
        locked_workspace = lock_tab_by_document_id(
            db=db,
            document_id=document_id,
            locked_by=request.locked_by,
        )

        if locked_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")

        return {
            "data": locked_workspace
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/unlock",
    response_model=TabWorkspaceAPIResponse,
    summary="Unlock tab",
    description="""
Unlocks a tab.

By default, only the user who owns the lock can unlock it.
Admins or system processes may use force=true to unlock regardless of owner.
""",
    response_description="Unlocked tab workspace wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def unlock_tab(
    document_id: str,
    request: UnlockTabRequest,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    try:
        unlocked_workspace = unlock_tab_by_document_id(
            db=db,
            document_id=document_id,
            unlocked_by=request.unlocked_by,
            force=request.force,
        )

        if unlocked_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")

        return {
            "data": unlocked_workspace
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}",
    response_model=TabWorkspaceAPIResponse,
    summary="Update tab metadata",
    description="""
Updates one tab using its documentId.

Supported fields:
- title
- order
- access_control
- locked
- locked_by

Only the fields sent by the frontend are updated.

The endpoint returns the full workspace after the update so the UI can refresh
the current tab state immediately.
""",
    response_description="Updated tab workspace wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def update_tab_metadata(
    document_id: str,
    request: UpdateTabRequest,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )

        updated_workspace = update_tab_by_document_id(
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

        return {
            "data": updated_workspace
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/move",
    response_model=TabWorkspaceAPIResponse,
    summary="Move tab",
    description="""
Moves a tab under a new parent or moves it to root.

Rules:
- newParentDocumentId can be null to make the tab a root tab
- a tab cannot be moved under itself
- a tab cannot be moved under one of its descendants
- existing parent link is updated safely
""",
    response_description="Moved tab workspace wrapped in a data object",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def move_tab(
    document_id: str,
    request: MoveTabRequest,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    try:
        moved_workspace = move_tab_by_document_id(
            db=db,
            document_id=document_id,
            new_parent_document_id=request.newParentDocumentId,
            order=request.order,
        )

        if moved_workspace is None:
            raise HTTPException(status_code=404, detail="Tab not found")

        return {
            "data": moved_workspace
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.delete(
    "/{document_id}",
    summary="Delete tab subtree",
    description="""
Deletes a tab and all of its descendants using documentId.

This is a hard delete.

Deleted records include:
- selected tab
- all child tabs recursively
- linked page_contents
- page_contents_tab_lnk rows
- tabs_parent_lnk rows
""",
    response_description="Delete result",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def delete_tab(
    document_id: str,
    db: Session = Depends(get_db),
):
    validate_document_id(document_id)

    try:
        delete_result = delete_tab_subtree_by_document_id(
            db=db,
            document_id=document_id,
        )

        if delete_result is None:
            raise HTTPException(status_code=404, detail="Tab not found")

        return {
            "data": delete_result
        }

    except ValueError as e:
        raise value_error_to_http_exception(e)


# ---------------------------------------------------------
# Debug/admin helper
# ---------------------------------------------------------

@router.get(
    "/schema-map",
    summary="Get tab relationship schema map",
    description="""
Debug/admin endpoint.

Returns each tab with:
- internal id
- document_id
- parent_id
- children_ids
- has_children
- has_parent
- has_content

This endpoint is not used by the frontend.

It is hidden from OpenAPI because it can be heavy on very large datasets
and should not be included in Schemathesis fuzzing.
""",
    include_in_schema=False,
)
def get_schema_map(db: Session = Depends(get_db)):
    return {
        "data": get_all_tabs_schema_map(db)
    }