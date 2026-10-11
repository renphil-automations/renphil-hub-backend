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

# Ceiling on `parent_ids` at creation. The UI sends at most one (the leaf the
# "Add" affordance hangs off), so this is only here to stop an unbounded list
# from turning one request into an unbounded number of cycle walks.
MAX_CREATE_PARENTS = 32


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

    # "Any Role" — the lattice's bottom (algorithm plan §4.4). Every role
    # implicitly inherits it, so it is in every descendant set. The UI needs
    # it to label the pair pickers honestly: on an OBJECT grant this is the
    # widest thing you can pick, and in an ASSIGNMENT picker it must not
    # appear at all.
    is_public: StrictBool = False

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

    # Set once, at creation, or never — see UpdateRoleRequest. There can be
    # at most one public role, and combining it with `parent_ids` is refused
    # by validate_role_edge (the bottom takes no explicit edges).
    is_public: StrictBool = False

    # Optional: attach the new role beneath these existing roles in the same
    # transaction as the insert. Exists so the "Add role" affordance on a leaf
    # node is one atomic call rather than create-then-edge, which can strand
    # an unattached role if the second call fails. Empty (the default) creates
    # a free-standing role exactly as before.
    #
    # Only PARENTS, never children: the affordance creates leaves, and a role
    # that does not exist yet cannot meaningfully be given descendants in the
    # same breath. Edges in the other direction go through the edge endpoint.
    parent_ids: list[int] = Field(default_factory=list, max_length=MAX_CREATE_PARENTS)


class UpdateRoleRequest(StrictRequestModel):
    """`key` and `is_public` are both deliberately absent — both immutable.

    The whole reason `key` exists separately from `name` (§3.2) is to give
    code a handle that survives renaming. A mutable key would reintroduce
    exactly the breakage the split was introduced to prevent, just one layer
    down: today the product hardcodes the string "Hub Admin" in ~35 places,
    and after the cutover it will hardcode "hub_admin" instead. Rename
    `name` freely; `key` is permanent for the life of the row.

    `is_public` for the same reason `is_universal` is immutable on a scope
    (see UpdateScopeRequest): flipping it silently rewrites what every
    existing grant reaches, with nothing written down anywhere to show it
    happened. Turning it ON drops the role into every descendant set at
    once, so every grant naming it becomes reachable by everybody; turning
    it OFF removes it from all of them, so grants that meant "everyone"
    quietly collapse to whatever edges the role happens to have — which is
    none, since the bottom is barred from the edge table while it is the
    bottom. Neither direction has a sane migration.

    Because this model is `extra="forbid"`, sending either field is a 422
    rather than a silently ignored no-op.
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

    # "Any Scope" — the lattice's bottom, the mirror of is_universal's top
    # (algorithm plan §4.4). Never both on one row; the DB carries a CHECK.
    #
    # The UI must label these two ends apart rather than showing the bare
    # names, because they are one word apart and mean close to opposites on
    # an object grant (§9): "All Scopes" there reaches only users whose own
    # assignment is hub-wide — the NARROWEST option — while "Any Scope"
    # reaches everyone on any scope, present or future.
    is_public: StrictBool = False

    # Always empty for a universal scope: it contains everything implicitly
    # and is barred from the edge table entirely (§5.5), so rendering it with
    # children would be a lie the UI then lets someone try to edit. Same for
    # a public scope, barred by the same rule at the other end.
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

    # Likewise set once or never. Sending it together with `is_universal` is
    # refused with 409 `universal_and_public` — one row cannot be both ends
    # of the lattice — and together with `parent_ids` by validate_scope_edge.
    is_public: StrictBool = False

    # Same contract as CreateRoleRequest.parent_ids. Combining it with
    # `is_universal` is refused by validate_scope_edge (§5.5 — the universal
    # scope takes no explicit edges in either direction), so the whole create
    # is rejected rather than half-applied.
    parent_ids: list[int] = Field(default_factory=list, max_length=MAX_CREATE_PARENTS)


class UpdateScopeRequest(StrictRequestModel):
    """`key`, `is_universal` and `is_public` are all immutable.

    `key` for the same reason as a role's (see UpdateRoleRequest).

    `is_universal` because flipping it silently rewrites what every existing
    assignment on that scope means. Turning it ON widens every holder to the
    entire scope graph in one write, with no edge added anywhere to show it
    happened; turning it OFF collapses the scope to whatever edges it has,
    which is none, since §5.5 kept it out of the edge table while it was
    universal. Neither direction has a sane migration, so the answer is to
    create a different scope and move people to it deliberately.

    `is_public` is the same argument reflected through the other end of the
    lattice, and the damage lands on OBJECT GRANTS rather than assignments:
    turning it ON makes every grant naming that scope reachable by everyone
    at a stroke, and turning it OFF silently un-publishes every node that was
    made public through it. Same answer — create a different scope.
    """

    name: StrictStr | None = Field(
        default=None, min_length=1, max_length=255, pattern=CLEAN_TEXT_PATTERN
    )
    description: StrictStr | None = Field(default=None, max_length=2000)
