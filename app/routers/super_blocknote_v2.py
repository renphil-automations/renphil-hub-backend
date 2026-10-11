"""
Super Block Note (SBN) v2 router — CRUD over a Super Block Note widget's own
nested sub-tab tree (see app/services/super_blocknote_service.py). Addressed
by ComponentV2.link (`{link}`), NOT by GridstackV2.document_id — a
deliberately separate resource/prefix from tabs_v2.py to avoid any ambiguity
between the two addressing schemes. Reuses the same Pydantic request/response
schemas as tabs_v2.py (CreateTabRequest, TabWorkspaceAPIResponse, etc.), same
design principle as the rest of v2: no new schema classes needed — including
the §5.2 triple (`view`/`edit`/`revealed`/`edit_seed`) and `node_kind`/
`node_id`, which `TabSummaryResponse`/`TabWorkspaceResponse` already declare
as optional and this router now populates.

ACCESS CONTROL, added 2026-09-09. Until then the router-level
`Depends(get_current_user)` below was this family's ONLY gate: authenticated,
never authorized. Because an SBN node is addressed by its own `link`, that
made every sub-tab in the hub readable and writable by any signed-in caller,
independent of the fold gating the tab it sits on — the largest hole left by
the enforcement-wiring sessions, which covered `tabs_v2.py` and `nav_tabs.py`
and never reached this file.

Every route now takes `access: ViewerAccess = Depends(get_viewer_access)` and
hands it to its service function, which owns the actual gate (same split as
`tabs_v2.py`: routers thread, services enforce). Read routes fail closed via
the existing `None -> 404` path, so an invisible node is indistinguishable
from a missing one (§9); write routes raise `AccessDeniedError`, mapped by
`tabs_v2.access_denied_to_http_exception` — imported rather than reimplemented
so the 404-vs-403 split cannot drift between the two routers.

EDIT SESSIONS, added 2026-09-17 (plan_component_locking_and_sbn_2026-09-17.md
§6). Every write route now also takes `session: EditSession =
Depends(get_edit_session)` (the caller's `X-Edit-Tokens`) and every read
route `lock_view = Depends(get_lock_view)`, threaded to the service exactly
as `tabs_v2.py` does — an SBN node is a real lock node now, so an SBN write
without a live session is a 409 `EDIT_SESSION_*`, and `PUT /{link}/lock`
returns the token the client must present back. `LockTabRequest.force` is
honoured here too (subtree takeover).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, get_edit_session, get_lock_view, get_viewer_access
from app.models.auth import UserInfo
from app.routers.tabs import validate_document_id, value_error_to_http_exception
from app.routers.tabs_v2 import access_denied_to_http_exception
from app.schemas.page_content import PageContentAPIResponse
from app.schemas.tab import (
    CreateTabRequest,
    LockTabRequest,
    ReorderTabsRequest,
    TabSummaryListAPIResponse,
    TabSummaryResponse,
    TabWorkspaceAPIResponse,
    UnlockTabRequest,
    UpdateTabContentRequest,
    UpdateTabRequest,
)
from app.services import edit_lock_service
from app.services.access_visibility_service import AccessDeniedError, ViewerAccess
from app.services.super_blocknote_service import (
    create_sbn_node,
    delete_sbn_subtree,
    get_sbn_children,
    get_sbn_content,
    get_sbn_workspace,
    lock_sbn_node,
    reorder_sbn_siblings,
    unlock_sbn_node,
    update_sbn_content,
    update_sbn_node,
)

router = APIRouter(prefix="/v2/sbn", tags=["Super Block Note V2"], dependencies=[Depends(get_current_user)])


COMMON_BAD_REQUEST_RESPONSE = {400: {"description": "Bad request"}}
COMMON_NOT_FOUND_RESPONSE = {404: {"description": "SBN node not found"}}
COMMON_CONFLICT_RESPONSE = {409: {"description": "Conflict"}}
COMMON_FORBIDDEN_RESPONSE = {403: {"description": "No edit access to this SBN node"}}


@router.post("/reorder", include_in_schema=False)
@router.get("/reorder", include_in_schema=False)
@router.delete("/reorder", include_in_schema=False)
@router.patch("/reorder", include_in_schema=False)
def reorder_method_not_allowed():
    raise HTTPException(
        status_code=405,
        detail="Method not allowed for /v2/sbn/reorder",
        headers={"Allow": "PUT"},
    )


@router.get(
    "/{link}/workspace",
    response_model=TabWorkspaceAPIResponse,
    summary="Get an SBN node's workspace (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_workspace(
    link: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    lock_view: edit_lock_service.LockView = Depends(get_lock_view),
):
    validate_document_id(link)
    # §9: a deep link to a node this caller cannot see 404s exactly like one
    # that does not exist — `get_sbn_workspace` returns None for both, so the
    # pre-existing line below produces the fail-closed response with no
    # branch of its own.
    workspace = get_sbn_workspace(db, link, access=access, lock_view=lock_view)
    if workspace is None:
        raise HTTPException(status_code=404, detail="SBN node not found")
    return {"data": workspace}


@router.get(
    "/{link}/children",
    response_model=TabSummaryListAPIResponse,
    summary="Get an SBN node's direct children (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_children(
    link: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    lock_view: edit_lock_service.LockView = Depends(get_lock_view),
):
    validate_document_id(link)
    children = get_sbn_children(db, link, access=access, lock_view=lock_view)
    if children is None:
        raise HTTPException(status_code=404, detail="SBN node not found")
    return {"data": children}


@router.get(
    "/{link}/content",
    response_model=PageContentAPIResponse,
    summary="Get an SBN node's content (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_content(
    link: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(link)
    # A REVEALED node (visible only because something in its subtree is
    # granted) returns 200 with `content: null`, not a 404 — §5.2's shell.
    # 404ing here would break navigation THROUGH the shell to the child that
    # earned the reveal, which is the whole point of the upward fold.
    content = get_sbn_content(db, link, access=access)
    if content is None:
        raise HTTPException(status_code=404, detail="SBN node not found")
    return {"data": content}


@router.put(
    "/{link}/content",
    response_model=PageContentAPIResponse,
    summary="Update an SBN node's content (v2)",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def update_content(
    link: str,
    request: UpdateTabContentRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    validate_document_id(link)
    try:
        updated = update_sbn_content(
            db=db, link=link, content=request.content, access=access, session=session
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": updated}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.post(
    "/",
    response_model=TabSummaryResponse,
    summary="Create a new SBN sub-tab (v2)",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
    },
)
def create_new_node(
    request: CreateTabRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )
        node = create_sbn_node(
            db=db,
            parent_link=request.parentDocumentId,
            title=request.title,
            content=request.content,
            order=request.order,
            access_control=access_control,
            access=access,
            session=session,
        )
        return node
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/reorder",
    response_model=TabSummaryListAPIResponse,
    summary="Reorder sibling SBN sub-tabs (v2)",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
    },
)
def reorder_nodes(
    request: ReorderTabsRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    try:
        # Session-exempt (decision 9) — threaded for its holder identity
        # only; see `reorder_sbn_siblings`.
        reordered = reorder_sbn_siblings(
            db=db,
            items=[item.model_dump() for item in request.items],
            access=access,
            session=session,
        )
        return {"data": reordered}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{link}/lock",
    response_model=TabWorkspaceAPIResponse,
    summary="Lock an SBN node (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def lock_node(
    link: str,
    request: LockTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(link)
    try:
        # This router reuses tabs_v2.py's LockTabRequest verbatim, so
        # plan §6.6 Fix 1 (identity-sourced holder, no client-supplied
        # locked_by) applies here too automatically — see LockTabRequest's
        # own docstring in schemas/tab.py. Not an optional extension: the
        # field simply no longer exists on the shared schema. `force` is
        # §4.2 decision 8's subtree takeover, honoured here since
        # 2026-09-17 (an SBN node is a real lock node now).
        locked_by = (user.email or "").strip().lower()
        locked = lock_sbn_node(
            db=db, link=link, locked_by=locked_by, force=request.force, access=access
        )
        if locked is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": locked}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{link}/unlock",
    response_model=TabWorkspaceAPIResponse,
    summary="Unlock an SBN node (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def unlock_node(
    link: str,
    request: UnlockTabRequest,
    db: Session = Depends(get_db_v2),
    user: UserInfo = Depends(get_current_user),
    access: ViewerAccess = Depends(get_viewer_access),
):
    validate_document_id(link)
    try:
        # Same shared-schema consequence as lock_node above — this also
        # closes the omission bypass for SBN nodes, for the same reason
        # UnlockTabRequest's docstring gives.
        unlocked_by = (user.email or "").strip().lower()
        unlocked = unlock_sbn_node(
            db=db, link=link, unlocked_by=unlocked_by, force=request.force, access=access
        )
        if unlocked is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": unlocked}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{link}",
    response_model=TabWorkspaceAPIResponse,
    summary="Update an SBN node's metadata (v2)",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
    },
)
def update_node(
    link: str,
    request: UpdateTabRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    validate_document_id(link)
    try:
        access_control = (
            request.access_control.model_dump()
            if hasattr(request.access_control, "model_dump")
            else request.access_control
        )
        updated = update_sbn_node(
            db=db,
            link=link,
            title=request.title,
            order=request.order,
            access_control=access_control,
            access=access,
            session=session,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": updated}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.delete(
    "/{link}",
    summary="Delete an SBN node and its descendants (v2)",
    responses={
        **COMMON_BAD_REQUEST_RESPONSE,
        **COMMON_NOT_FOUND_RESPONSE,
        **COMMON_FORBIDDEN_RESPONSE,
        **COMMON_CONFLICT_RESPONSE,
    },
)
def delete_node(
    link: str,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
):
    validate_document_id(link)
    try:
        result = delete_sbn_subtree(db=db, link=link, access=access, session=session)
        if result is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": result}
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)
