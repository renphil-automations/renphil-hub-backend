"""Request/response schemas for the embedded-forms endpoints (`/forms/*`)."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StrictBool, StrictStr, field_validator

from app.schemas.tab import StrictRequestModel


def _validate_http_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("must be an absolute http(s) URL")
    return value


# ── Form-builder proxy ────────────────────────────────────────────────
# Both are GETs; the PAT travels in the `X-Form-PAT` header (see
# app/routers/forms.py), so there are no request bodies.


class FormInfo(BaseModel):
    id: str
    title: str


class ListFormsResponse(BaseModel):
    success: bool
    forms: list[FormInfo]


class FormEmbedInfoResponse(BaseModel):
    success: bool
    id: str
    embed_url: str
    requires_authentication: bool


class FormComponentEmbedResponse(BaseModel):
    """What the Hub needs to render one form component's iframe."""

    success: bool
    link: str = Field(..., description="The form component's `link`.")
    form_id: str
    embed_url: str = Field(
        ...,
        description="URL to load in the iframe. When `requires_authentication` is true "
        "it already carries the viewer's `token=<jwt>` query param.",
    )
    requires_authentication: bool
    expires_in_seconds: int | None = Field(
        default=None,
        description="Token lifetime; null when no token was issued. Re-fetch to get a fresh one.",
    )
    max_uses: int | None = Field(
        default=None, description="How many times the token can be used; null when no token was issued."
    )


# ── Form component upsert ─────────────────────────────────────────────


class SaveFormComponentRequest(StrictRequestModel):
    """Upsert one `form` component.

    `link` set and matching an existing component → update it. Otherwise a
    new `form` component is created on the gridstack named by `document_id`.

    The form spec (`form_id`, `embed_url`, `requires_authentication`) is
    replaced on every save. `title`/`description` are partial: omit to keep
    the stored value on update.

    `pat` is write-only (never returned, including by
    `GET /v2/tabs/{id}/content`): omit it, or send `null`/"", to keep the
    stored PAT; send a value to replace it. Required when the form is first
    created.

    `x`/`y`/`w`/`h` are applied only when creating; ignored on update.
    """

    link: StrictStr | None = Field(default=None, max_length=255)
    document_id: StrictStr | None = Field(default=None, max_length=255)

    title: StrictStr | None = Field(default=None, max_length=255)
    description: StrictStr | None = None

    form_id: StrictStr = Field(..., min_length=1, max_length=255)
    embed_url: StrictStr = Field(..., min_length=1, max_length=2048)
    requires_authentication: StrictBool = False
    pat: StrictStr | None = Field(default=None, max_length=1024)

    x: float | None = None
    y: float | None = None
    w: float | None = Field(default=None, gt=0)
    h: float | None = Field(default=None, gt=0)

    @field_validator("form_id")
    @classmethod
    def _check_form_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value.strip()))
        except ValueError:
            raise ValueError("form_id must be a UUID") from None

    @field_validator("embed_url")
    @classmethod
    def _check_embed_url(cls, value: str) -> str:
        return _validate_http_url(value)


class FormComponentSpec(BaseModel):
    """The form spec stored in the component's `form_details` row. The PAT
    is never returned."""

    form_id: str | None = None
    embed_url: str | None = None
    requires_authentication: bool = False


class FormComponentResponse(BaseModel):
    id: int
    link: str
    type: str
    title: str | None = None
    description: str | None = None
    document_id: str | None = Field(
        default=None, description="`document_id` of the gridstack the component lives on."
    )
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    form: FormComponentSpec


class SaveFormComponentResponse(BaseModel):
    success: bool
    created: bool
    component: FormComponentResponse
    search_updates: list[dict[str, Any]] = Field(default_factory=list)
