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
from app.dependencies import get_current_user
from app.models.auth import UserInfo
from app.services import access_control_service
from app.routers.tabs import validate_document_id, value_error_to_http_exception
from app.schemas.page_content import PageContentAPIResponse
from app.schemas.tab import (
    CreateTabRequest,
    CreateTabVariantRequest,
    LockTabRequest,
    MoveTabRequest,
    MoveTabToNavTabRequest,
    PreviewAccessWriteRequest,
    PurgePrincipalRequest,
    ReorderTabsRequest,
    ReorderTabVariantsRequest,
    TabSummaryListAPIResponse,
    TabSummaryResponse,
    TabWorkspaceAPIResponse,
    UnlockTabRequest,
    UpdateTabContentRequest,
    UpdateTabRequest,
)
from app.services.gridstack_service import (
    create_tab_v2,
    create_tab_variant_v2,
    delete_tab_subtree_by_document_id_v2,
    get_component_by_link_for_access_check_v2,
    get_component_by_link_v2,
    get_root_tabs_v2,
    get_tab_children_v2,
    get_tab_content_v2,
    get_tab_variants_v2,
    get_tab_workspace_v2,
    lock_tab_by_document_id_v2,
    move_tab_by_document_id_v2,
    preview_tab_access_v2,
    purge_tab_principal_v2,
    reorder_tab_variants_v2,
    reorder_tabs_by_document_id_v2,
    reset_tab_access_v2,
    resolve_component_location_v2,
    unlock_tab_by_document_id_v2,
    update_tab_by_document_id_v2,
    update_tab_content_v2,
)
from app.services.nav_tab_service import (
    get_dashboard_nav_tab,
    get_nav_tab_by_document_id,
    move_tab_to_nav_tab_v2,
    resolve_move_authorization_targets,
)
from app.services.tab_service import filter_widget_content_for_user

router = APIRouter(prefix="/v2/tabs", tags=["Tabs V2"], dependencies=[Depends(get_current_user)])


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
    description="Backs the mirror widget's 'jump to original' affordance and "
    "the external component deep-link feature — resolves the root tab, the "
    "ordered chain of ancestor sub-tab `document_id`s, and (if the "
    "component is a Super Block Note descendant) the ordered chain of "
    "ancestor SBN component `link`s, so the frontend can navigate there "
    "and highlight the component. Fails closed (403) if the caller cannot "
    "see the target component — this endpoint is reachable via links "
    "shared outside the app (email, chat), not just from within an "
    "already-authorized canvas.",
    responses={
        **COMMON_NOT_FOUND_RESPONSE,
        403: {"description": "Caller cannot view this component"},
    },
)
def get_component_location(
    link: str,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    component = get_component_by_link_for_access_check_v2(db, link)
    if component is None:
        raise HTTPException(status_code=404, detail="Component not found, or cannot be located")
    # The nearest ancestor with a non-NULL access_control (§3.4) — a
    # component with no explicit AC inherits it, and an explicit AC is
    # always subset-constrained against it, so this one check covers both
    # widget-level and tab-level restriction. Same primitive
    # `filter_widget_content_for_user` uses server-side to redact a
    # restricted widget from a workspace response; this is that same rule
    # applied to the one endpoint that hands out a component's location
    # instead of its content.
    effective_ac = component.access_control or access_control_service.resolved_parent_ac(
        db, component
    )
    if not access_control_service.can_view(effective_ac, user.email, list(user.roles)):
        raise HTTPException(status_code=403, detail="You don't have access to this component")
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
    user: UserInfo = Depends(get_current_user),
):
    validate_document_id(document_id)

    workspace = get_tab_workspace_v2(db, document_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Tab not found")

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
    "/{document_id}/variants",
    response_model=TabSummaryListAPIResponse,
    summary="Get tab variants (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_variants(document_id: str, db: Session = Depends(get_db_v2)):
    """A tab variant (TabV2.parent_tab_id) is a distinct, one-level nesting
    axis from /{document_id}/children above (which is gridstack-level
    nesting) — see gridstack_service.py's TabV2 docstring."""
    validate_document_id(document_id)

    variants = get_tab_variants_v2(db, document_id)
    if variants is None:
        raise HTTPException(status_code=404, detail="Parent tab not found")

    return {"data": variants}


@router.post(
    "/{document_id}/variants",
    response_model=TabSummaryResponse,
    summary="Create a tab variant (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def create_variant(document_id: str, request: CreateTabVariantRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )
        return create_tab_variant_v2(
            db=db,
            parent_document_id=document_id,
            title=request.title,
            access_control=access_control,
            order=request.order,
        )
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/variants/reorder",
    response_model=TabSummaryListAPIResponse,
    summary="Reorder tab variants (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def reorder_variants(document_id: str, request: ReorderTabVariantsRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)

    try:
        reordered = reorder_tab_variants_v2(
            db=db,
            parent_document_id=document_id,
            ordered_document_ids=request.orderedDocumentIds,
        )
        if reordered is None:
            raise HTTPException(status_code=404, detail="Parent tab not found")
        return {"data": reordered}
    except ValueError as e:
        raise value_error_to_http_exception(e)


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

        # navTabDocumentId only means anything for a ROOT create
        # (parentDocumentId absent) — a sub-tab gridstack has no nav_tab_id
        # of its own. When absent for a root create, fall back to the
        # Dashboard nav tab so any existing caller keeps working unchanged.
        nav_tab_id: int | None = None
        if request.parentDocumentId is None:
            if request.navTabDocumentId is not None:
                nav_tab = get_nav_tab_by_document_id(db, request.navTabDocumentId)
                if nav_tab is None:
                    raise ValueError("Nav tab does not exist")
                nav_tab_id = nav_tab.id
            else:
                dashboard = get_dashboard_nav_tab(db)
                nav_tab_id = dashboard.id if dashboard is not None else None

        return create_tab_v2(
            db=db,
            title=request.title,
            parent_document_id=request.parentDocumentId,
            content=request.content,
            order=request.order,
            access_control=access_control,
            nav_tab_id=nav_tab_id,
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


@router.put(
    "/{document_id}/nav-tab",
    response_model=TabWorkspaceAPIResponse,
    summary="Move a root tab to a different nav tab",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        403: {"description": "Editor access required on the tab and the destination nav tab"},
    },
    # No blanket `Depends` here (plan §5.3, §9.9c) — this writes to BOTH the
    # root tab and the destination nav tab, and the destination is only
    # known from the request body, which a per-node path-param dependency
    # can't see. Checked inline below instead.
)
def move_tab_to_nav_tab(
    document_id: str,
    request: MoveTabToNavTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    validate_document_id(document_id)

    tab, dest_nav_tab = resolve_move_authorization_targets(db, document_id, request.navTabDocumentId)
    if tab is None:
        raise HTTPException(status_code=404, detail="Tab not found")
    if not access_control_service.can_edit(tab.access_control, user.email, list(user.roles)):
        raise HTTPException(status_code=403, detail="Cannot edit this tab")
    if dest_nav_tab is None:
        raise HTTPException(status_code=404, detail="Destination nav tab does not exist")
    if not access_control_service.can_edit(dest_nav_tab.access_control, user.email, list(user.roles)):
        raise HTTPException(status_code=403, detail="Cannot edit the destination nav tab")

    try:
        moved_workspace = move_tab_to_nav_tab_v2(
            db=db,
            tab_document_id=document_id,
            nav_tab_document_id=request.navTabDocumentId,
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


# No additional gate on the three endpoints below, matching every other
# mutating endpoint on this router (update/move/delete) — landmine 6,
# explicitly out of phase-2 scope: "any authenticated user can do anything
# to a tab" is a pre-existing gap this phase never closes, not one these
# introduce.
@router.post(
    "/{document_id}/access/preview",
    summary="Preview the blast radius of a tab access-control write (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def preview_tab_access(
    document_id: str, request: PreviewAccessWriteRequest, db: Session = Depends(get_db_v2)
):
    validate_document_id(document_id)
    access_control = (
        request.access_control.model_dump()
        if hasattr(request.access_control, "model_dump")
        else request.access_control
    )
    preview = preview_tab_access_v2(db, document_id, access_control)
    if preview is None:
        raise HTTPException(status_code=404, detail="Tab not found")
    return preview


@router.post(
    "/{document_id}/access/purge",
    summary="Purge a principal from a tab and its entire subtree (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def purge_tab_principal(
    document_id: str, request: PurgePrincipalRequest, db: Session = Depends(get_db_v2)
):
    validate_document_id(document_id)
    try:
        result = purge_tab_principal_v2(db, document_id, request.principal.model_dump())
        if result is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return result
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.post(
    "/{document_id}/access/reset",
    summary="Reset a tab's access control to inherit from its resolved parent (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def reset_tab_access(document_id: str, db: Session = Depends(get_db_v2)):
    validate_document_id(document_id)
    try:
        result = reset_tab_access_v2(db, document_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        return result
    except ValueError as e:
        raise value_error_to_http_exception(e)
