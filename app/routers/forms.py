"""
Embedded forms router.

* `GET /forms` / `GET /forms/{form_id}` — proxy the form-builder service under
  a caller-supplied PAT, sent in the `X-Form-PAT` header (never the URL, so it
  stays out of access logs). The PAT is used for that one call and not stored.
* `PUT /forms/component` — upsert a `form` component's settings.
* `GET /forms/component/{link}/embed` — the embed URL to render one form
  component, with a per-viewer token when the form requires authentication.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Response
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, get_edit_session, get_viewer_access
from app.models.auth import UserInfo
from app.routers.tabs import value_error_to_http_exception
from app.routers.tabs_v2 import access_denied_to_http_exception
from app.schemas.forms import (
    FormComponentEmbedResponse,
    FormEmbedInfoResponse,
    ListFormsResponse,
    SaveFormComponentRequest,
    SaveFormComponentResponse,
)
from app.services import edit_lock_service, form_service
from app.services.access_visibility_service import AccessDeniedError, ViewerAccess

router = APIRouter(prefix="/forms", tags=["Forms"], dependencies=[Depends(get_current_user)])

_FORM_BUILDER_ERRORS = {
    422: {"description": "Missing PAT/form id, PAT rejected, form not found/unavailable, or service not configured"},
    502: {"description": "The form-builder service failed or couldn't be reached"},
}


def _form_pat(
    x_form_pat: str | None = Header(
        default=None,
        alias="X-Form-PAT",
        description="The form-builder personal access token.",
    ),
) -> str:
    # Missing/blank is reported by the service as a 422, same as before.
    return x_form_pat or ""


def _no_store(response: Response) -> None:
    # The PAT isn't part of the URL, so a cache keyed on the URL could serve
    # one PAT's results for another — never cache these.
    response.headers["Cache-Control"] = "no-store"


@router.get(
    "",
    response_model=ListFormsResponse,
    summary="List the forms a form-builder PAT can access",
    responses=_FORM_BUILDER_ERRORS,
)
async def list_forms(response: Response, pat: str = Depends(_form_pat)) -> ListFormsResponse:
    _no_store(response)
    try:
        forms = await form_service.list_forms(pat)
    except form_service.FormBuilderError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail, headers={"Cache-Control": "no-store"})
    return ListFormsResponse(success=True, forms=forms)


@router.get(
    "/{form_id}",
    response_model=FormEmbedInfoResponse,
    summary="Get one form's embed URL and authentication requirement",
    responses=_FORM_BUILDER_ERRORS,
)
async def get_form_embed_info(
    response: Response,
    form_id: str = Path(..., description="A form id returned by `GET /forms`."),
    pat: str = Depends(_form_pat),
) -> FormEmbedInfoResponse:
    _no_store(response)
    try:
        info = await form_service.get_form_embed_info(pat, form_id)
    except form_service.FormBuilderError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail, headers={"Cache-Control": "no-store"})
    return FormEmbedInfoResponse(success=True, **info)


@router.put(
    "/component",
    response_model=SaveFormComponentResponse,
    summary="Create or update a form component's settings",
    description="""
Upsert: when `link` matches an existing component it is updated (it must be a
`form` component); otherwise a new `form` component is created on the
gridstack named by `document_id`.

Authorization is `edit(n)` on the component (update) or on the canvas
(create), then a live edit session covering it (`X-Edit-Tokens`) — refused
with a 409 `EDIT_SESSION_*` code otherwise.

A newly created component must be added to the open canvas's state before the
next canvas save (`PUT /v2/tabs/{id}/content`), which deletes components
absent from its payload.
""",
    responses={
        400: {"description": "Bad request (e.g. missing document_id, missing pat on create, link isn't a form)"},
        403: {"description": "You do not have edit access to this item"},
        404: {"description": "Tab not found"},
        409: {"description": "No live edit session covering this item"},
    },
)
def save_form_component(
    body: SaveFormComponentRequest = Body(...),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    session: edit_lock_service.EditSession = Depends(get_edit_session),
) -> SaveFormComponentResponse:
    # `model_fields_set` keeps "absent" (preserve) distinct from "null".
    provided = body.model_fields_set
    optional: dict[str, object] = {}
    for field in ("title", "description", "pat"):
        if field in provided:
            optional[field] = getattr(body, field)

    try:
        result = form_service.save_form_component(
            db,
            link=body.link,
            document_id=body.document_id,
            form_id=body.form_id,
            embed_url=body.embed_url,
            requires_authentication=body.requires_authentication,
            layout={"x": body.x, "y": body.y, "w": body.w, "h": body.h},
            access=access,
            session=session,
            **optional,
        )
    except AccessDeniedError as e:
        raise access_denied_to_http_exception(e)
    except ValueError as e:
        raise value_error_to_http_exception(e)

    if result is None:
        raise HTTPException(status_code=404, detail="Tab not found")
    return SaveFormComponentResponse(success=True, **result)


@router.get(
    "/component/{link}/embed",
    response_model=FormComponentEmbedResponse,
    summary="Get the embed URL for one form component",
    description="""
Looks up the form component by its `link` in `form_details`.

* `requires_authentication: false` — returns the stored `embed_url` as-is.
* `requires_authentication: true` — calls the form builder's
  `POST /api/authenticate_with_pat` with the stored PAT, the form id and the
  signed-in user's email (from the JWT), and returns `embed_url` with
  `token=<jwt>` appended. The token is per-viewer and short-lived
  (`expires_in_seconds`, `max_uses`): fetch this right before rendering the
  iframe, and again to refresh it. The response is never cached.

The PAT is never returned. The caller must be able to view the component;
otherwise (or for an unknown/non-form link) it's a 404.
""",
    responses={
        404: {"description": "Form component not found / not visible, or not configured yet"},
        422: {"description": "The form builder rejected the request (PAT no longer granted, form deleted, …) or the service isn't configured"},
        502: {"description": "The form-builder service failed or couldn't be reached"},
        503: {"description": "The form-builder service is busy"},
    },
)
async def get_form_component_embed(
    response: Response,
    link: str = Path(..., description="The form component's stable `link`."),
    user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
) -> FormComponentEmbedResponse:
    _no_store(response)
    try:
        # Plain sync SQLAlchemy — kept off the event loop.
        target = await asyncio.to_thread(form_service.get_form_embed_target, db, link, access=access)
        embed = await form_service.resolve_form_embed(target, str(user.email))
    except form_service.FormBuilderError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail, headers={"Cache-Control": "no-store"})
    return FormComponentEmbedResponse(success=True, **embed)
