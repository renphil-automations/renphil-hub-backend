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
from app.dependencies import get_current_user, get_viewer_access, require_hub_admin
from app.models.auth import UserInfo
from app.routers.tabs import validate_document_id, value_error_to_http_exception
from app.schemas.page_content import PageContentAPIResponse
from app.schemas.tab import (
    CreateTabRequest,
    CreateTabVariantRequest,
    LockTabRequest,
    MoveTabRequest,
    MoveTabToNavTabRequest,
    ReorderTabsRequest,
    ReorderTabVariantsRequest,
    TabSummaryListAPIResponse,
    TabSummaryResponse,
    TabWorkspaceAPIResponse,
    UnlockTabRequest,
    UpdateComponentContentRequest,
    UpdateTabContentRequest,
    UpdateTabRequest,
)
from app.services.access_visibility_service import ViewerAccess
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
    reorder_tab_variants_v2,
    reorder_tabs_by_document_id_v2,
    resolve_component_location_v2,
    unlock_tab_by_document_id_v2,
    update_component_content,
    update_tab_by_document_id_v2,
    update_tab_content_v2,
)
from app.services.nav_tab_service import (
    get_dashboard_nav_tab,
    get_nav_tab_by_document_id,
    move_tab_to_nav_tab_v2,
)

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
def get_roots(db: Session = Depends(get_db_v2), access: ViewerAccess = Depends(get_viewer_access)):
    return {"data": get_root_tabs_v2(db, access=access)}


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
    "and highlight the component. Fails closed (404, not 403 — plan §9) if "
    "the caller cannot view the target component, so the response does not "
    "confirm the component exists — this endpoint is reachable via links "
    "shared outside the app (email, chat), not just from within an "
    "already-authorized canvas.",
    responses={**COMMON_NOT_FOUND_RESPONSE},
)
def get_component_location(
    link: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    component = get_component_by_link_for_access_check_v2(db, link)
    if component is None:
        raise HTTPException(status_code=404, detail="Component not found, or cannot be located")
    # plan_access_control_algorithm_2026-08-27.md §9: a caller who cannot
    # VIEW this component gets the same 404 as "does not exist" — not the
    # 403 this endpoint used to return, which would have confirmed the
    # component is real. Replaces the old per-widget access_control check
    # (`_user_can_view_widget` against the component's own AC) with the new
    # fold; `access.is_granted` already carries the Hub Admin bypass.
    if not access.is_granted(("component", component.id)):
        raise HTTPException(status_code=404, detail="Component not found, or cannot be located")
    result = resolve_component_location_v2(db, link)
    if result is None:
        raise HTTPException(status_code=404, detail="Component not found, or cannot be located")
    return {"data": result}


@router.put(
    "/components/by-link/{link}/content",
    summary="Update one component's content (v2)",
    description="""
Writes ONE component's own content, addressed by its stable `link`. Partial —
only the fields present in the request body are applied.

**This is the write path for an editor whose region begins at a component**
(plan_access_control_algorithm_2026-08-27.md §6.7). `PUT /{document_id}/content`
is a whole-gridstack diff that DELETES any component absent from the payload,
so using it for a single-widget edit requires sending every sibling — which
both rewrites widgets the caller may hold no grant on and deletes them by
omission. This endpoint touches one row and never deletes anything.

`data: null` is rejected (422); send `{}` to empty a widget, or omit the field
to leave it unchanged. `type`, `access_control` and the layout fields are not
accepted — see the request schema for why each one is absent.

Authorization is login-only, **identical to the canvas save** — no grant check
and no lock check. Both omissions are deliberate; see the service function's
docstring.
""",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
    },
)
def update_component_content_endpoint(
    link: str,
    request: UpdateComponentContentRequest,
    db: Session = Depends(get_db_v2),
):
    # `model_fields_set` distinguishes "absent" from "explicitly null" — the
    # difference between preserving a title and clearing it. Same mechanism
    # the Airtable config endpoint uses for its own partial update.
    provided = request.model_fields_set
    updates: dict[str, object] = {}
    if "title" in provided:
        updates["title"] = request.title
    if "description" in provided:
        updates["description"] = request.description
    if "data" in provided:
        updates["data"] = request.data

    try:
        updated = update_component_content(db, link, **updates)
    except ValueError as e:
        raise value_error_to_http_exception(e)

    if updated is None:
        raise HTTPException(status_code=404, detail="Component not found")

    return {"data": updated}


@router.get(
    "/{document_id}/workspace",
    response_model=TabWorkspaceAPIResponse,
    summary="Get tab workspace (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_workspace(
    document_id: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    # plan §8.1-8.4, §9: get_tab_workspace_v2 now does both jobs that used to
    # live here — 404s a HIDDEN tab the same way it 404s a MISSING one
    # (fail-closed, indistinguishable from outside), and filters
    # page_content via the new fold instead of the old
    # filter_widget_content_for_user(user.email, user.roles) call this
    # replaced.
    workspace = get_tab_workspace_v2(db, document_id, access=access)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Tab not found")

    return {"data": workspace}


@router.get(
    "/{document_id}/children",
    response_model=TabSummaryListAPIResponse,
    summary="Get direct child tabs (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_children(
    document_id: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    children = get_tab_children_v2(db, document_id, access=access)
    if children is None:
        raise HTTPException(status_code=404, detail="Parent tab not found")

    return {"data": children}


@router.get(
    "/{document_id}/variants",
    response_model=TabSummaryListAPIResponse,
    summary="Get tab variants (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_variants(
    document_id: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    """A tab variant (TabV2.parent_tab_id) is a distinct, one-level nesting
    axis from /{document_id}/children above (which is gridstack-level
    nesting) — see gridstack_service.py's TabV2 docstring."""
    validate_document_id(document_id)

    variants = get_tab_variants_v2(db, document_id, access=access)
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
def get_content(
    document_id: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    content = get_tab_content_v2(db, document_id, access=access)
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
def lock_tab(
    document_id: str,
    request: LockTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    validate_document_id(document_id)

    try:
        # plan §6.6 Fix 1: the holder is the AUTHENTICATED identity, never a
        # request-body field — `LockTabRequest` no longer has one to read.
        # Same normalization dependencies.get_current_hub_user applies.
        locked_by = (user.email or "").strip().lower()
        locked_workspace = lock_tab_by_document_id_v2(db=db, document_id=document_id, locked_by=locked_by)
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
def unlock_tab(
    document_id: str,
    request: UnlockTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
):
    validate_document_id(document_id)

    try:
        # plan §6.6 Fix 1: identity-sourced, same as lock_tab above. This
        # also closes the omission bypass by construction — see
        # UnlockTabRequest's docstring — there is no longer a body field an
        # unlock could omit to skip the ownership check.
        unlocked_by = (user.email or "").strip().lower()
        unlocked_workspace = unlock_tab_by_document_id_v2(
            db=db,
            document_id=document_id,
            unlocked_by=unlocked_by,
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

        # plan §6.6 Fix 1, the "third door": UpdateTabRequest no longer
        # carries locked/locked_by at all (see its docstring) — nothing to
        # pass through here, and update_tab_by_document_id_v2 no longer
        # accepts those parameters either.
        updated_workspace = update_tab_by_document_id_v2(
            db=db,
            document_id=document_id,
            title=request.title,
            order=request.order,
            access_control=access_control,
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
        403: {"description": "Hub Admin role required"},
    },
    # Moving a root tab between nav tabs is a nav-tab-level operation, so
    # it carries the same gate every other /v2/nav-tabs mutation does. It
    # used to run a per-node `can_edit` on both the tab and the destination
    # nav tab; that engine is gone, and both checks only ever admitted Hub
    # Admins in practice.
    dependencies=[Depends(require_hub_admin)],
)
def move_tab_to_nav_tab(
    document_id: str,
    request: MoveTabToNavTabRequest,
    db: Session = Depends(get_db_v2),
):
    validate_document_id(document_id)

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
