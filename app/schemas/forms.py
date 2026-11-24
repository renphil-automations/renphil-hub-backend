"""Request/response schemas for the embedded-forms endpoints (`/forms/*`)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, StrictStr, field_validator

from app.schemas.tab import StrictRequestModel

# The form builder's `authType` values (see `form_service.FORM_AUTH_TYPES`).
FormAuthType = Literal["none", "submission", "user"]


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
    authentication_type: FormAuthType | None = Field(
        default=None,
        description="The form builder's `authType`; null if it didn't report a known one.",
    )


class FormComponentEmbedResponse(BaseModel):
    """What the Hub needs to render one form component's iframe."""

    success: bool
    link: str = Field(..., description="The form component's `link`.")
    form_id: str
    embed_url: str = Field(
        ...,
        description="URL to load in the iframe, freshly fetched from the form builder. "
        "For a non-public form it carries `hide_back_btn=true|false`; when "
        "`requires_authentication` is true and `authentication_type` is `user`, it "
        "also carries the viewer's `token=<jwt>` query param.",
    )
    requires_authentication: bool
    authentication_type: FormAuthType | None = None
    hide_back_button: bool = False
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

    `form_id` is replaced on every save. The form's embed URL and
    authentication settings aren't stored — they're fetched from the form
    builder whenever the form is loaded. `title`/`description` are partial:
    omit to keep the stored value on update.

    `pat` is write-only (never returned, including by
    `GET /v2/tabs/{id}/content`): omit it, or send `null`/"", to keep the
    stored PAT; send a value to replace it. Required when the form is first
    created.

    `hide_back_button` is meant for forms with `user` or `submission`
    authentication: omit it (or send `null`) to keep the stored value
    (false on create).

    `x`/`y`/`w`/`h` are applied only when creating; ignored on update.
    """

    link: StrictStr | None = Field(default=None, max_length=255)
    document_id: StrictStr | None = Field(default=None, max_length=255)

    title: StrictStr | None = Field(default=None, max_length=255)
    description: StrictStr | None = None

    form_id: StrictStr = Field(..., min_length=1, max_length=255)
    pat: StrictStr | None = Field(default=None, max_length=1024)
    hide_back_button: StrictBool | None = None

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


class FormComponentSpec(BaseModel):
    """The form settings stored in the component's `form_details` row. The
    PAT is never returned."""

    form_id: str | None = None
    hide_back_button: bool = False


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
