"""
Embedded forms: the form-builder proxy (list forms / one form's embed info),
the `form` component upsert, and resolving a form component's embed URL for
the viewer (authenticated via the stored PAT when the form requires it).

A form component's spec lives in its own `form_details` row (1:1 with the
component, deleted with it by `ON DELETE CASCADE`), never in the component's
`props`/`page_content`, so the canvas save can't touch it and the canvas read
doesn't expose it. The form-builder PAT is stored retrievable (it's needed for
runtime authentication) but never returned.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.form_details import FormDetailsV2
from app.db_v2.models.gridstack import GridstackV2
from app.helpers.http_client import get_http_client
from app.services.access_visibility_service import (
    ViewerAccess,
    require_edit,
    resolve_gridstack_node,
)
from app.services.content_link_registry import sync_component_links
from app.services.gridstack_service import (
    _UNSET,
    _component_persistence_signature,
    _generate_id,
    _require_live_session,
    _search_update_receipts,
    _write_component_data,
    get_gridstack_by_document_id,
)

logger = logging.getLogger(__name__)

# An embedded form-builder form. Its spec lives in `form_details`, not in the
# component's own data.
FORM_WIDGET_TYPE = "form"

FORM_BUILDER_TIMEOUT_SECONDS = 15.0

_FORM_BUSY_DETAIL = "The form-builder service is busy right now. Please try again in a moment."

_GENERIC_UPSTREAM_DETAIL = (
    "The form-builder service couldn't complete this request. Please try again, "
    "or contact support if it keeps happening."
)


class FormBuilderError(Exception):
    """A forms request failed (a form-builder call, or the form component
    lookup); carries the HTTP status/detail to return."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------
# Form-builder proxy
# ---------------------------------------------------------

def _require_base_url() -> str:
    base_url = (get_settings().FORM_BUILDER_BASE_URL or "").strip().rstrip("/")
    if not base_url:
        raise FormBuilderError(422, "The form-builder service isn't configured yet (FORM_BUILDER_BASE_URL).")
    return base_url


def _require_source_ui() -> str:
    source_ui = (get_settings().FORM_BUILDER_SOURCE_UI or "").strip()
    if not source_ui:
        raise FormBuilderError(422, "The form-builder service isn't configured yet (FORM_BUILDER_SOURCE_UI).")
    return source_ui


def _require_pat(pat: str | None) -> str:
    key = (pat or "").strip()
    if not key:
        raise FormBuilderError(422, "A form-builder PAT is required.")
    return key


async def _form_builder_send(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Send one request to the form-builder service. Returns the response
    whatever its status; only a transport failure raises (as a 502)."""
    base_url = _require_base_url()
    try:
        return await get_http_client().request(
            method, f"{base_url}{path}", timeout=FORM_BUILDER_TIMEOUT_SECONDS, **kwargs
        )
    except httpx.RequestError as e:
        logger.warning("Couldn't reach the form-builder service for %s: %r", path, e)
        raise FormBuilderError(502, "Couldn't reach the form-builder service.") from e


def _json_object(resp: httpx.Response, path: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError as e:
        logger.warning("Form-builder returned non-JSON for %s", path)
        raise FormBuilderError(502, _GENERIC_UPSTREAM_DETAIL) from e
    if not isinstance(data, dict):
        logger.warning("Form-builder returned a non-object body for %s", path)
        raise FormBuilderError(502, _GENERIC_UPSTREAM_DETAIL)
    return data


async def _form_builder_get(
    path: str, pat: str, *, not_found_detail: str | None = None
) -> dict[str, Any]:
    """GET `path` on the form-builder service and return its JSON object,
    translating every failure into a `FormBuilderError`."""
    source_ui = _require_source_ui()
    resp = await _form_builder_send("GET", path, headers={"x-pat": pat}, params={"sourceUi": source_ui})
    if not resp.is_success:
        status = resp.status_code
        if status in (401, 403):
            raise FormBuilderError(422, "The form-builder service rejected the PAT.")
        if status == 404 and not_found_detail:
            raise FormBuilderError(422, not_found_detail)
        logger.warning("Form-builder request %s failed (%s)", path, status)
        raise FormBuilderError(502, _GENERIC_UPSTREAM_DETAIL)
    return _json_object(resp, path)


async def list_forms(pat: str) -> list[dict[str, str]]:
    """The forms a form-builder PAT can access, as `[{id, title}]`."""
    key = _require_pat(pat)
    data = await _form_builder_get("/api/forms", key)
    forms = data.get("forms") or []
    if not isinstance(forms, list):
        raise FormBuilderError(502, _GENERIC_UPSTREAM_DETAIL)
    return [
        {"id": str(f["id"]), "title": str(f.get("title") or "")}
        for f in forms
        if isinstance(f, dict) and f.get("id") not in (None, "")
    ]


async def get_form_embed_info(pat: str, form_id: str) -> dict[str, Any]:
    """One form's embed metadata: id, embed URL, and whether the embed needs
    a per-user authentication token."""
    key = _require_pat(pat)
    fid = (form_id or "").strip()
    if not fid:
        raise FormBuilderError(422, "A form id is required.")
    data = await _form_builder_get(
        f"/api/forms/{quote(fid, safe='')}",
        key,
        not_found_detail="Form not found for this PAT.",
    )
    # A 200 with `unavailable: true` (or no `embedUrl`, e.g. a never-published
    # draft) isn't an HTTP error, so it's surfaced here rather than letting a
    # null embed URL be saved onto a component and break the embed at runtime.
    embed_url = data.get("embedUrl")
    if data.get("unavailable") or not embed_url:
        raise FormBuilderError(
            422,
            "This form isn't available for embedding yet - it may still be a draft. "
            "Deploy/publish it in the form builder, then load forms again.",
        )
    return {
        "id": str(data.get("id") or fid),
        "embed_url": str(embed_url),
        "requires_authentication": bool(data.get("requiresAuthentication", False)),
    }


async def authenticate_with_pat(pat: str, form_id: str, user_email: str) -> dict[str, Any]:
    """Mint a per-user embed token for one form via the form builder's
    `POST /api/authenticate_with_pat`. Returns `{token, expires_in_seconds,
    max_uses}`. The token is a credential: never log it."""
    key = _require_pat(pat)
    path = "/api/authenticate_with_pat"
    resp = await _form_builder_send(
        "POST",
        path,
        json={
            "pat": key,
            "formId": form_id,
            "userEmail": user_email,
            "sourceUi": _require_source_ui(),
        },
    )
    if not resp.is_success:
        status = resp.status_code
        if status == 400:
            raise FormBuilderError(422, _upstream_error(resp) or "The form-builder service rejected the authentication request.")
        if status in (401, 403):
            raise FormBuilderError(
                422,
                "The PAT saved on this form no longer has access to it. "
                "Ask an editor to update the form's PAT.",
            )
        if status == 404:
            raise FormBuilderError(422, "This form no longer exists on the form-builder service.")
        if status == 503:
            raise FormBuilderError(503, _FORM_BUSY_DETAIL)
        logger.warning("Form-builder request %s failed (%s)", path, status)
        raise FormBuilderError(502, _GENERIC_UPSTREAM_DETAIL)

    data = _json_object(resp, path)
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        logger.warning("Form-builder returned no token for %s", path)
        raise FormBuilderError(502, _GENERIC_UPSTREAM_DETAIL)
    return {
        "token": token.strip(),
        "expires_in_seconds": _int_or_none(data.get("expiresInSeconds")),
        "max_uses": _int_or_none(data.get("maxUses")),
    }


def _upstream_error(resp: httpx.Response) -> str | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    message = data.get("error") if isinstance(data, dict) else None
    return message.strip() if isinstance(message, str) and message.strip() else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def with_embed_token(embed_url: str, token: str) -> str:
    """`embed_url` with `token=<token>` added to its query string (replacing
    any existing `token` param). The rest of the query is kept byte-for-byte
    rather than re-encoded, and a `#fragment` stays at the end."""
    parts = urlsplit(embed_url)
    kept = [p for p in parts.query.split("&") if p and p.split("=", 1)[0] != "token"]
    kept.append("token=" + quote(token, safe=""))
    return urlunsplit(parts._replace(query="&".join(kept)))


# ---------------------------------------------------------
# Form component embed (viewer side)
# ---------------------------------------------------------

@dataclass(frozen=True)
class FormEmbedTarget:
    """One form component's stored embed settings. SERVER-INTERNAL: carries
    the PAT, which must never be returned to the client."""

    link: str
    form_id: str
    embed_url: str
    requires_authentication: bool
    pat: str


def get_form_embed_target(
    db: Session, link: str, *, access: ViewerAccess | None = None
) -> FormEmbedTarget:
    """Resolve a form component's stored embed settings by its `link`.

    The caller must be able to view the component (`is_granted`, the same
    payload gate the canvas read uses); an unknown link, an invisible
    component and a non-form component are all the same 404, so the endpoint
    can't be used to probe for components. A visible form that hasn't been
    configured yet is its own 404.
    """
    link = (link or "").strip()
    component = (
        db.query(ComponentV2).filter(ComponentV2.link == link).first() if link else None
    )
    if (
        component is None
        or component.type != FORM_WIDGET_TYPE
        or (access is not None and not access.is_granted(("component", component.id)))
    ):
        raise FormBuilderError(404, "Form component not found")

    details = db.query(FormDetailsV2).filter(FormDetailsV2.component_id == component.id).first()
    if details is None:
        raise FormBuilderError(404, "This form component hasn't been configured yet")

    return FormEmbedTarget(
        link=component.link,
        form_id=str(details.form_id),
        embed_url=details.embed_url,
        requires_authentication=bool(details.requires_authentication),
        pat=details.pat,
    )


async def resolve_form_embed(target: FormEmbedTarget, user_email: str) -> dict[str, Any]:
    """The embed info to hand the viewer. A form that doesn't require
    authentication gets its stored embed URL as-is (no form-builder call); one
    that does gets a per-user token, minted under the stored PAT for
    `user_email`, appended to it."""
    result: dict[str, Any] = {
        "link": target.link,
        "form_id": target.form_id,
        "embed_url": target.embed_url,
        "requires_authentication": target.requires_authentication,
        "expires_in_seconds": None,
        "max_uses": None,
    }
    if not target.requires_authentication:
        return result

    auth = await authenticate_with_pat(target.pat, target.form_id, user_email)
    result["embed_url"] = with_embed_token(target.embed_url, auth["token"])
    result["expires_in_seconds"] = auth["expires_in_seconds"]
    result["max_uses"] = auth["max_uses"]
    return result


# ---------------------------------------------------------
# Form component upsert
# ---------------------------------------------------------

def _form_details_view(details: FormDetailsV2 | None) -> dict[str, Any] | None:
    """Client-safe view of a `form_details` row (never the PAT), or None
    when the form hasn't been configured yet."""
    if details is None:
        return None
    return {
        "form_id": str(details.form_id),
        "embed_url": details.embed_url,
        "requires_authentication": bool(details.requires_authentication),
    }

def _form_component_view(
    db: Session, component: ComponentV2, details: FormDetailsV2 | None
) -> dict[str, Any]:
    gridstack = db.query(GridstackV2).filter(GridstackV2.id == component.gridstack_id).first()
    return {
        "id": component.id,
        "link": component.link,
        "type": component.type,
        "title": component.title,
        "description": component.description,
        "document_id": gridstack.document_id if gridstack is not None else None,
        "x": component.x,
        "y": component.y,
        "w": component.width,
        "h": component.height,
        "form": _form_details_view(details),
    }


def save_form_component(
    db: Session,
    *,
    link: str | None,
    document_id: str | None,
    form_id: str,
    embed_url: str,
    requires_authentication: bool,
    pat: Any = _UNSET,
    title: Any = _UNSET,
    description: Any = _UNSET,
    layout: dict[str, float | None] | None = None,
    access: ViewerAccess | None = None,
    session: Any = None,
) -> dict[str, Any] | None:
    """Upsert one `form` component and its `form_details` row.

    An existing component addressed by `link` is updated (it must be a
    `form`); otherwise a new one is created on the gridstack named by
    `document_id`. Gated like every other component write: `edit(n)` on the
    component (update) or on the gridstack's own node (create), then a live
    edit session covering that node.

    `pat` is write-only (the canvas read never returns it, so an editor
    re-saving a form can't resend it): `_UNSET`, `None` or blank keeps the
    stored PAT; a non-blank value replaces it. Required whenever the
    `form_details` row is being created (the column is NOT NULL).

    Returns `{"created", "component", "search_updates"}`, or None when the
    gridstack to create on doesn't exist (404 at the router). Raises
    ValueError (400) for an unusable request.
    """
    try:
        form_uuid = uuid.UUID(str(form_id).strip())
    except ValueError:
        raise ValueError("form_id must be a UUID") from None

    new_pat = str(pat).strip() if pat is not _UNSET and pat is not None else ""
    pat_required_detail = "pat is required when creating a form component"

    link = (link or "").strip()
    component = (
        db.query(ComponentV2).filter(ComponentV2.link == link).first() if link else None
    )

    gridstack: GridstackV2 | None = None
    details: FormDetailsV2 | None = None
    if component is not None:
        node = ("component", component.id)
        require_edit(access, node)
        _require_live_session(db, session, node)
        if component.type != FORM_WIDGET_TYPE:
            raise ValueError(
                f"This link points at a '{component.type}' component, not a form"
            )
        details = (
            db.query(FormDetailsV2).filter(FormDetailsV2.component_id == component.id).first()
        )
        # A `form` widget placed by the canvas save has no details row yet;
        # this save creates it, so it needs a PAT just like a brand-new form.
        if details is None and not new_pat:
            raise ValueError(pat_required_detail)
    else:
        doc = (document_id or "").strip()
        if not doc:
            raise ValueError("document_id is required to create a new form component")
        gridstack = get_gridstack_by_document_id(db, doc)
        if gridstack is None:
            return None
        node = resolve_gridstack_node(db, gridstack)
        require_edit(access, node)
        _require_live_session(db, session, node)
        if not new_pat:
            raise ValueError(pat_required_detail)

    try:
        created = component is None
        if created:
            layout = layout or {}
            component = ComponentV2(
                link=_generate_id(),
                type=FORM_WIDGET_TYPE,
                title=None if title is _UNSET else title,
                description=None if description is _UNSET else description,
                props={},
                access_control=None,
                x=layout.get("x") if layout.get("x") is not None else 0,
                y=layout.get("y") if layout.get("y") is not None else 0,
                width=layout.get("w") if layout.get("w") is not None else 6,
                height=layout.get("h") if layout.get("h") is not None else 6,
                gridstack_id=gridstack.id,
                page_content_id=None,
                current_grid_id=None,
            )
            db.add(component)
            db.flush()
            before_signature = None
        else:
            before_signature = _component_persistence_signature(db, component)
            if title is not _UNSET:
                component.title = title
            if description is not _UNSET:
                component.description = description

        # Every non-mirror widget has a page_content row (the canvas save
        # creates one); keep a form created here consistent with that.
        if component.page_content_id is None:
            _write_component_data(db, component, {})

        db.flush()
        changed = created or before_signature != _component_persistence_signature(db, component)
        if changed:
            sync_component_links(db, component)

        now = datetime.now(timezone.utc)
        if details is None:
            details = FormDetailsV2(component_id=component.id, created_at=now)
            db.add(details)
        details.form_id = form_uuid
        details.embed_url = embed_url
        details.requires_authentication = bool(requires_authentication)
        if new_pat:
            details.pat = new_pat
        details.updated_at = now

        db.flush()
        db.commit()

        if created:
            logger.info("Created form component %s on gridstack %s", component.link, gridstack.document_id)

        return {
            "created": created,
            "component": _form_component_view(db, component, details),
            "search_updates": _search_update_receipts({component.id: "upsert"}) if changed else [],
        }
    except Exception:
        db.rollback()
        raise
