"""
Super Block Note (SBN) v2 router — CRUD over a Super Block Note widget's own
nested sub-tab tree (see app/services/super_blocknote_service.py). Addressed
by ComponentV2.link (`{link}`), NOT by GridstackV2.document_id — a
deliberately separate resource/prefix from tabs_v2.py to avoid any ambiguity
between the two addressing schemes. Reuses the same Pydantic request/response
schemas as tabs_v2.py (CreateTabRequest, TabWorkspaceAPIResponse, etc.), same
design principle as the rest of v2: no new schema classes needed.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.routers.tabs import validate_document_id, value_error_to_http_exception
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

router = APIRouter(prefix="/v2/sbn", tags=["Super Block Note V2"])


COMMON_BAD_REQUEST_RESPONSE = {400: {"description": "Bad request"}}
COMMON_NOT_FOUND_RESPONSE = {404: {"description": "SBN node not found"}}
COMMON_CONFLICT_RESPONSE = {409: {"description": "Conflict"}}


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
def get_workspace(link: str, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    workspace = get_sbn_workspace(db, link)
    if workspace is None:
        raise HTTPException(status_code=404, detail="SBN node not found")
    return {"data": workspace}


@router.get(
    "/{link}/children",
    response_model=TabSummaryListAPIResponse,
    summary="Get an SBN node's direct children (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_children(link: str, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    children = get_sbn_children(db, link)
    if children is None:
        raise HTTPException(status_code=404, detail="SBN node not found")
    return {"data": children}


@router.get(
    "/{link}/content",
    response_model=PageContentAPIResponse,
    summary="Get an SBN node's content (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def get_content(link: str, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    content = get_sbn_content(db, link)
    if content is None:
        raise HTTPException(status_code=404, detail="SBN node not found")
    return {"data": content}


@router.put(
    "/{link}/content",
    response_model=PageContentAPIResponse,
    summary="Update an SBN node's content (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def update_content(link: str, request: UpdateTabContentRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    try:
        updated = update_sbn_content(db=db, link=link, content=request.content)
        if updated is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": updated}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.post(
    "/",
    response_model=TabSummaryResponse,
    summary="Create a new SBN sub-tab (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def create_new_node(request: CreateTabRequest, db: Session = Depends(get_db_v2)):
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
        )
        return node
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/reorder",
    response_model=TabSummaryListAPIResponse,
    summary="Reorder sibling SBN sub-tabs (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def reorder_nodes(request: ReorderTabsRequest, db: Session = Depends(get_db_v2)):
    try:
        reordered = reorder_sbn_siblings(db=db, items=[item.model_dump() for item in request.items])
        return {"data": reordered}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{link}/lock",
    response_model=TabWorkspaceAPIResponse,
    summary="Lock an SBN node (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def lock_node(link: str, request: LockTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    try:
        locked = lock_sbn_node(db=db, link=link, locked_by=request.locked_by)
        if locked is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": locked}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{link}/unlock",
    response_model=TabWorkspaceAPIResponse,
    summary="Unlock an SBN node (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def unlock_node(link: str, request: UnlockTabRequest, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    try:
        unlocked = unlock_sbn_node(db=db, link=link, unlocked_by=request.unlocked_by, force=request.force)
        if unlocked is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": unlocked}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.put(
    "/{link}",
    response_model=TabWorkspaceAPIResponse,
    summary="Update an SBN node's metadata (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE, **COMMON_CONFLICT_RESPONSE},
)
def update_node(link: str, request: UpdateTabRequest, db: Session = Depends(get_db_v2)):
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
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": updated}
    except ValueError as e:
        raise value_error_to_http_exception(e)


@router.delete(
    "/{link}",
    summary="Delete an SBN node and its descendants (v2)",
    responses={**COMMON_BAD_REQUEST_RESPONSE, **COMMON_NOT_FOUND_RESPONSE},
)
def delete_node(link: str, db: Session = Depends(get_db_v2)):
    validate_document_id(link)
    try:
        result = delete_sbn_subtree(db=db, link=link)
        if result is None:
            raise HTTPException(status_code=404, detail="SBN node not found")
        return {"data": result}
    except ValueError as e:
        raise value_error_to_http_exception(e)
