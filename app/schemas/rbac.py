"""Access Management wire shapes
(plan_access_control_schema_2026-08-22.md §3).

Covers the v1 surface only: role and scope DEFINITIONS plus the two
hierarchies. Assignments — and therefore the delegation rule in §6 — are a
later phase, so nothing here references hub_users or role_assignments.

Follows app/schemas/tab.py's conventions: StrictRequestModel (extra="forbid")
for request bodies, StrictStr everywhere, and shared regex patterns rather
than ad-hoc validators.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, StrictBool, StrictStr

from app.schemas.tab import CLEAN_TEXT_PATTERN, StrictRequestModel

# A stable code handle: lower snake_case. Mirrors ICON_NAME_PATTERN's style in
# schemas/tab.py (kebab there, snake here to match Python/SQL naming).
KEY_PATTERN = r"^[a-z0-9]+(_[a-z0-9]+)*$"

# `depth` (formerly `rank`) is nullable and constrains nothing — the rank
# ordering rule it served is disabled (see rbac_graph_service). These bounds
# are all that is left of its validation: a plain non-negative int well clear
# of the int32 ceiling, so there is always room to insert above or below if
# the rule is ever restored.
MIN_DEPTH = 0
MAX_DEPTH = 1_000_000


# ---------------------------------------------------------
# Roles
# ---------------------------------------------------------


class RoleResponse(BaseModel):
    id: int
    key: StrictStr
    name: StrictStr
    description: StrictStr | None = None

    # Nullable and inert. Kept on the wire so the rank rule can be switched
    # back on without a schema migration; the UI does not render it.
    depth: int | None = None

    is_system: StrictBool = False

    # The role's DIRECT parents and children — one hop, not the closure.
    # Both are carried on the list response so the per-node parent/child
    # picker can render the whole roles screen from a single request; the
    # closure is never sent because the UI edits edges, not reachability.
    parent_ids: list[int] = Field(default_factory=list)
    child_ids: list[int] = Field(default_factory=list)


class RoleListAPIResponse(BaseModel):
    data: list[RoleResponse] = Field(default_factory=list)


class RoleAPIResponse(BaseModel):
    data: RoleResponse


class CreateRoleRequest(StrictRequestModel):
    key: StrictStr = Field(min_length=1, max_length=64, pattern=KEY_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=255, pattern=CLEAN_TEXT_PATTERN)
    description: StrictStr | None = Field(default=None, max_length=2000)

    # Was `rank`, required. Now optional and inert — the create form does not
    # send it, and a role created without one is the normal case.
    depth: int | None = Field(default=None, ge=MIN_DEPTH, le=MAX_DEPTH)


class UpdateRoleRequest(StrictRequestModel):
    """`key` is deliberately absent — it is immutable.

    The whole reason `key` exists separately from `name` (§3.2) is to give
    code a handle that survives renaming. A mutable key would reintroduce
    exactly the breakage the split was introduced to prevent, just one layer
    down: today the product hardcodes the string "Hub Admin" in ~35 places,
    and after the cutover it will hardcode "hub_admin" instead. Rename
    `name` freely; `key` is permanent for the life of the row.
    """

    name: StrictStr | None = Field(
        default=None, min_length=1, max_length=255, pattern=CLEAN_TEXT_PATTERN
    )
    description: StrictStr | None = Field(default=None, max_length=2000)

    # Sending it explicitly as null clears it; omitting it leaves it alone —
    # the router distinguishes the two via `model_fields_set`, same as
    # `description`. Unlike a rank edit, this can never invalidate an edge.
    depth: int | None = Field(default=None, ge=MIN_DEPTH, le=MAX_DEPTH)


# ---------------------------------------------------------
# Scopes
# ---------------------------------------------------------


class ScopeResponse(BaseModel):
    id: int
    key: StrictStr
    name: StrictStr
    description: StrictStr | None = None
    is_universal: StrictBool = False
    is_system: StrictBool = False

    # Always empty for a universal scope: it contains everything implicitly
    # and is barred from the edge table entirely (§5.5), so rendering it with
    # children would be a lie the UI then lets someone try to edit.
    parent_ids: list[int] = Field(default_factory=list)
    child_ids: list[int] = Field(default_factory=list)


class ScopeListAPIResponse(BaseModel):
    data: list[ScopeResponse] = Field(default_factory=list)


class ScopeAPIResponse(BaseModel):
    data: ScopeResponse


class CreateScopeRequest(StrictRequestModel):
    key: StrictStr = Field(min_length=1, max_length=64, pattern=KEY_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=255, pattern=CLEAN_TEXT_PATTERN)
    description: StrictStr | None = Field(default=None, max_length=2000)

    # Set once, at creation. See UpdateScopeRequest for why it cannot change.
    is_universal: StrictBool = False


class UpdateScopeRequest(StrictRequestModel):
    """`key` and `is_universal` are both immutable.

    `key` for the same reason as a role's (see UpdateRoleRequest).

    `is_universal` because flipping it silently rewrites what every existing
    assignment on that scope means. Turning it ON widens every holder to the
    entire scope graph in one write, with no edge added anywhere to show it
    happened; turning it OFF collapses the scope to whatever edges it has,
    which is none, since §5.5 kept it out of the edge table while it was
    universal. Neither direction has a sane migration, so the answer is to
    create a different scope and move people to it deliberately.
    """

    name: StrictStr | None = Field(
        default=None, min_length=1, max_length=255, pattern=CLEAN_TEXT_PATTERN
    )
    description: StrictStr | None = Field(default=None, max_length=2000)
