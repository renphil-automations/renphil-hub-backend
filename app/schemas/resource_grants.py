"""Object-grant wire shapes
(plan_access_control_algorithm_2026-08-27.md §7).

Separate from `app/schemas/rbac.py` and `app/schemas/rbac_assignments.py` for
the reason the latter already records: `rbac.py`'s docstring says it covers
role/scope DEFINITIONS only and references neither `hub_users` nor
`role_assignments`, and keeping the split is what keeps that true rather than
stale. Grants are a third surface again — they reference nodes, which neither
of the others does.

NOTHING SERVES THESE YET. There is no grants router: this step builds the
table and the matching primitive, and stops before any enforcement. These
shapes exist because `resource_grant_service.serialize_grant` produces them
and something has to pin that contract — `test_resource_grants.py` validates
the service's output against `ResourceGrantResponse`, so the two cannot drift
before the router lands.

Same conventions as `rbac.py`: `StrictRequestModel` (extra="forbid") for
request bodies, `StrictStr` everywhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.tab import StrictRequestModel

# Spelled out rather than derived from `resource_grant.NODE_KINDS` /
# `GRANT_LEVELS`, because a Literal needs literal members. The model remains
# the source of truth and `test_resource_grants` asserts these two agree with
# it, so adding a node kind in one place and not the other fails a test rather
# than silently rejecting valid requests at the edge.
NodeKind = Literal["hub", "nav_tab", "tab", "component"]
GrantLevel = Literal["view", "edit"]


class ResourceGrantResponse(BaseModel):
    id: int

    # The node arc, read back as the one value callers want. Stored as four
    # nullable FK columns (see the model's docstring for why real foreign
    # keys were worth that); `node_of` collapses them on the way out.
    node_kind: NodeKind
    node_id: int

    # Exactly one FORM is populated: either both of role_id/scope_id, or
    # user_id alone. Enforced by `ck_resource_grants_one_principal`.
    #
    # A NOTE FOR WHOEVER RENDERS THIS, because it reads as the opposite of
    # what it is (§4.3): on a stored grant the lower and narrower the pair,
    # the MORE people it reaches. "All Scopes" here reaches only users whose
    # own assignment is hub-wide — the narrowest option, not the widest — and
    # "Any Scope" (the ⊥ flag) is the one that reaches everybody. §9 requires
    # both ends be labelled rather than shown by their bare names, and
    # requires that neither be forbidden.
    role_id: int | None = None
    scope_id: int | None = None
    user_id: int | None = None

    level: GrantLevel

    # Both nullable together only when the granter's hub_users row was later
    # deleted (ON DELETE SET NULL on granted_by_user_id) — granted_by_email is
    # the immutable snapshot that survives that, so display IT whenever it
    # might be null, never a live lookup through granted_by_user_id. Same rule
    # as AssignmentResponse's, and §6.2's revoke confirmation depends on it:
    # "granted directly by Sam, 3 Feb" has to keep working after Sam leaves.
    granted_by_user_id: int | None = None
    granted_by_email: str | None = None

    created_at: datetime


class ResourceGrantListAPIResponse(BaseModel):
    data: list[ResourceGrantResponse] = Field(default_factory=list)


class ResourceGrantAPIResponse(BaseModel):
    data: ResourceGrantResponse


class CreateGrantRequest(StrictRequestModel):
    """One grant on one node.

    No update counterpart, deliberately: a grant is three immutable facts, and
    "changing" one is a revoke plus a write. See the service module docstring.

    `user_id` is an id here where `CreateAssignmentRequest` takes an email,
    and that difference is not yet a decision — it is the router's to make
    when the grants surface lands, along with whether the §6 delegation rule
    gates writing a grant at all (the algorithm plan assumes it at §6.3 and
    explicitly leaves it undesigned at §12).
    """

    node_kind: NodeKind
    node_id: int
    level: GrantLevel

    role_id: int | None = None
    scope_id: int | None = None
    user_id: int | None = None
