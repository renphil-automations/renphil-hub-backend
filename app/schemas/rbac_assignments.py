"""Permission Management wire shapes
(plan_access_control_schema_2026-08-22.md §6,
session_handoff_2026-08-24-permission-management-plan.md §5).

Separate from `app/schemas/rbac.py` on purpose — that module's own
docstring says it deliberately references neither `hub_users` nor
`role_assignments`, since role/scope DEFINITIONS (Step 2) shipped before
this surface existed. Keeping the split means that docstring stays true
rather than stale.

Same conventions as `rbac.py`: `StrictRequestModel` (extra="forbid") for
request bodies, `StrictStr` everywhere.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.tab import StrictRequestModel


class AssignmentResponse(BaseModel):
    id: int
    user_id: int
    user_email: str
    role_id: int
    scope_id: int

    # Both nullable together only when the granter's hub_users row was later
    # deleted (ON DELETE SET NULL on granted_by_user_id) — granted_by_email
    # is the immutable snapshot that survives that, so display IT, never a
    # live lookup through granted_by_user_id, whenever it might be null
    # (handoff §7).
    granted_by_user_id: int | None = None
    granted_by_email: str | None = None

    created_at: datetime


class AssignmentListAPIResponse(BaseModel):
    data: list[AssignmentResponse] = Field(default_factory=list)


class AssignmentAPIResponse(BaseModel):
    data: AssignmentResponse


class CreateAssignmentRequest(StrictRequestModel):
    """Target is identified by email, not a hub_users id — the caller
    (granter) has no reason to know the target's internal id. The server
    resolves it and 409s `target_not_provisioned` if no hub_users row
    exists yet (plan session handoff 2026-08-24 §3.2, §5.3)."""

    user_email: EmailStr
    role_id: int
    scope_id: int


class HubUserResponse(BaseModel):
    """Deliberately minimal — id, email, name only, never is_active or
    anything else. This is a people-directory search, a different kind of
    exposure than role/scope names (handoff §5.4)."""

    id: int
    email: str
    name: str | None = None


class HubUserListAPIResponse(BaseModel):
    data: list[HubUserResponse] = Field(default_factory=list)
