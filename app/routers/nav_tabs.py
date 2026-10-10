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
from app.dependencies import get_current_user, get_edit_session, get_lock_view, get_viewer_access
from app.models.auth import UserInfo
from app.schemas.tab import (
    CreateNavTabRequest,
    LockTabRequest,
    NavTabListAPIResponse,
    NavTabResponse,
    RenewLockRequest,
    ReorderNavTabsRequest,
    TabSummaryListAPIResponse,
    UnlockTabRequest,
    UpdateNavTabRequest,
)
from app.services import edit_lock_service
from app.services.access_visibility_service import AccessDeniedError, ViewerAccess, resolve_hub_node
from app.services.gridstack_service import get_root_tabs_v2
from app.services.nav_tab_service import (
    create_nav_tab_v2,
    delete_nav_tab_v2,
    get_nav_tab_by_document_id,
    get_nav_tabs_v2,
    lock_nav_tab_by_document_id_v2,
    renew_nav_tab_lock_by_document_id_v2,
    reorder_nav_tabs_v2,
    unlock_nav_tab_by_document_id_v2,
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
def get_nav_tabs(
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    lock_view: edit_lock_service.LockView = Depends(get_lock_view),
):
    # findings_dev_login_live_testing_2026-09-12.md #4: the frontend has no
    # other way to learn whether the caller holds edit(hub) — needed to gate
    # Delete/reorder correctly (§6.3: both are edit(parent(n)), not the
    # plain edit(n) each row's own `edit` field already answers). One
    # `verdict()` lookup against the SAME `access` already computed for
    # this request — no extra query.
    hub_edit = access.verdict(resolve_hub_node(db)).edit
    return {"data": get_nav_tabs_v2(db, access=access, lock_view=lock_view), "hub_edit": hub_edit}


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
    lock_view: edit_lock_service.LockView = Depends(get_lock_view),
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

    return {"data": get_root_tabs_v2(db, nav_tab_id=nav_tab.id, access=access, lock_view=lock_view)}


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
    user: UserInfo = Depends(get_current_user),
):
    try:
        # plan_lock_propagation_2026-09-08.md §5.4/§9 item 1 — session-
        # EXEMPT permission-wise (edit(hub)), but still refused if a
        # reordered nav tab is held fresh by someone else.
        return {
            "data": reorder_nav_tabs_v2(
                db=db,
                ordered_document_ids=request.orderedDocumentIds,
                access=access,
                holder=(user.email or "").strip().lower(),
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
    session: edit_lock_service.EditSession = Depends(get_edit_session),
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
            session=session,
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
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    validate_document_id(document_id)

    try:
        delete_result = delete_nav_tab_v2(db=db, document_id=document_id, access=access, session=session)
        if delete_result is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return {"data": delete_result}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


# ---------------------------------------------------------
# Locking — plan_lock_propagation_2026-09-08.md §6.7/§8 phase 5, decision 3:
# a nav tab's edit-mode toggle acquires a REAL lock. Endpoint-for-endpoint
# mirror of tabs_v2.py's lock_tab/unlock_tab — same request schemas
# (LockTabRequest/UnlockTabRequest have no tab-specific fields, so they are
# reused as-is rather than duplicated), same identity-sourced holder (no
# `locked_by`/`unlocked_by` body field, per those schemas' own Fix 1
# docstrings). plan_ac_enforcement_closeout_2026-09-09.md §3 closed the gap
# this comment used to describe: lock/unlock (and force-unlock, which
# collapses to the same check — see lock_nav_tab_by_document_id_v2's own
# docstring) now require `edit(nav_tab)`, same as rename/icon/delete
# (`update_nav_tab`/`delete_nav_tab` above).
# ---------------------------------------------------------

@router.put(
    "/{document_id}/lock",
    response_model=NavTabResponse,
    summary="Lock nav tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def lock_nav_tab(
    document_id: str,
    request: LockTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    try:
        locked_by = (user.email or "").strip().lower()
        locked = lock_nav_tab_by_document_id_v2(
            db=db, document_id=document_id, locked_by=locked_by, force=request.force, access=access
        )
        if locked is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return locked
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/lock/renew",
    response_model=NavTabResponse,
    summary="Renew an existing edit session on a nav tab (v2)",
    description="""
Nav-tab mirror of `PUT /v2/tabs/{id}/lock/renew` — the save preflight's
VALIDATE door (2026-09-16 TTL fix). Succeeds, bumping the session's expiry,
only if this nav tab's own lock row is a fresh session held by the caller
under one of the presented `X-Edit-Tokens`; otherwise 409 with the matching
`EDIT_SESSION_*` code and no change to the row. Never acquires or reclaims —
see `edit_lock_service.renew`.
""",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
    },
)
def renew_nav_tab_lock(
    document_id: str,
    # Parsed for schema symmetry with the tab route (same `{}` body from the
    # same client helper) — `link` has no meaning for a nav tab and is not
    # read here.
    request: RenewLockRequest,  # noqa: ARG001
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    validate_document_id(document_id)

    try:
        renewed = renew_nav_tab_lock_by_document_id_v2(
            db=db, document_id=document_id, session=session, access=access
        )
        if renewed is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return renewed
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{document_id}/unlock",
    response_model=NavTabResponse,
    summary="Unlock nav tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def unlock_nav_tab(
    document_id: str,
    request: UnlockTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(document_id)

    try:
        unlocked_by = (user.email or "").strip().lower()
        unlocked = unlock_nav_tab_by_document_id_v2(
            db=db,
            document_id=document_id,
            unlocked_by=unlocked_by,
            force=request.force,
            access=access,
        )
        if unlocked is None:
            raise HTTPException(status_code=404, detail="Nav tab not found")
        return unlocked
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)
