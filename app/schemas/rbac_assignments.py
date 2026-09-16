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


# ---------------------------------------------------------
# Admin user-management table (owner decision, 2026-09-12) — every hub_users
# row plus every role_assignments row it holds. Deliberately NOT the same
# shape as HubUserResponse above: that one is the privacy-conscious search
# picker (handoff §5.4); this is the Hub-Admin-only management surface, so
# is_active is included and there is no query-length gate at all — the
# privacy boundary here is `require_hub_admin` itself (see the router).
# ---------------------------------------------------------


class HubUserAdminResponse(BaseModel):
    id: int
    email: str
    name: str | None = None
    is_active: bool
    # Every (role, scope) row this user holds directly, one row per grant —
    # never the expanded closure, same convention as AssignmentResponse
    # everywhere else on this router. Empty for the (currently: 181 of 182)
    # users who hold none.
    assignments: list[AssignmentResponse] = Field(default_factory=list)


class HubUserAdminListAPIResponse(BaseModel):
    data: list[HubUserAdminResponse] = Field(default_factory=list)


class UpdateAssignmentRequest(StrictRequestModel):
    """Both fields optional; the router requires at least one. Whichever is
    omitted keeps the assignment's current value — the same partial-update
    convention as UpdateRoleRequest/UpdateScopeRequest in schemas/rbac.py.

    user_id is deliberately not editable here: re-pointing an assignment at
    a different PERSON is a revoke-and-grant, not an update, since it would
    also change granted_by/created_at's meaning. Only role_id and/or
    scope_id — the pairing an existing grant holds — may move."""

    role_id: int | None = None
    scope_id: int | None = None
