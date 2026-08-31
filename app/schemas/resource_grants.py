"""Object-grant wire shapes
(plan_access_control_algorithm_2026-08-27.md §7).

Separate from `app/schemas/rbac.py` and `app/schemas/rbac_assignments.py` for
the reason the latter already records: `rbac.py`'s docstring says it covers
role/scope DEFINITIONS only and references neither `hub_users` nor
`role_assignments`, and keeping the split is what keeps that true rather than
stale. Grants are a third surface again — they reference nodes, which neither
of the others does.

`app/routers/resource_grants.py` serves these. It is a NEW surface, added
2026-08-31, and it enforces nothing about CONTENT: no existing endpoint reads
`granted` or `visible`, and no response shape a client reads today changed
when it landed. The only thing gated here is the grants surface itself.

`test_resource_grants.py` additionally validates
`resource_grant_service.serialize_grant`'s output against
`ResourceGrantResponse`, so the service and the wire shape cannot drift even
where the router is not involved.

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

    `user_id` IS AN ID, where `CreateAssignmentRequest` takes an email. Both
    of the questions this docstring used to leave open were decided by the
    owner on 2026-08-31, when the router landed:

      - **Addressed by id.** The `/v2/rbac/hub-users` picker already returns
        ids, so the client has one in hand at the moment it builds this
        request, and a grant — unlike an assignment — is written about
        somebody who is already on screen rather than typed in from memory.
        The assignments path keeps its email for its own stated reason (the
        granter has no reason to know an internal id when inviting someone),
        and the two surfaces differ deliberately rather than by accident.
      - **The write gate is NOT the §6 delegation rule.** It is
        `edit(node)` plus "the pair must be one you match yourself". See
        `app/services/resource_grant_authz_service.py`, which records why
        `can_delegate` is the wrong rule here and what was measured.
    """

    node_kind: NodeKind
    node_id: int
    level: GrantLevel

    role_id: int | None = None
    scope_id: int | None = None
    user_id: int | None = None


class NodeRefResponse(BaseModel):
    """One node's address. The tree is not returned with it — the caller
    already has the node titles it is rendering, and shipping a second
    representation of the hierarchy from an authorization endpoint is how
    the two drift apart."""

    node_kind: NodeKind
    node_id: int


class RetainedAccessResponse(BaseModel):
    """§6.2's revoke-time confirmation: *"what would this principal still
    retain?"*, answered for one grant that has NOT been deleted.

    ADVISORY, NOT A GATE. §6.2 is explicit that `[ Leave it ]` is a
    legitimate answer — narrow grants made by other admins are usually
    deliberate — so nothing here is a precondition of the DELETE, and the
    DELETE does not check that it was called. It exists so D4's "derive,
    never collapse" decision is safe: derivation leaves lower grants in
    place, and an admin who is not shown them will believe they removed
    more than they did.

    `retained_view` / `retained_edit` cover the revoked node AND everything
    beneath it, because that is the span the revoke was meant to affect.
    """

    node_kind: NodeKind
    node_id: int

    retained_view: list[NodeRefResponse] = Field(default_factory=list)
    retained_edit: list[NodeRefResponse] = Field(default_factory=list)

    # The rows `[ Remove that too ]` would delete — surviving grants sitting
    # at or below the revoked node.
    responsible_grants: list[ResourceGrantResponse] = Field(default_factory=list)

    # A different sentence in the modal, not a longer list: surviving grants
    # sitting ABOVE the node. These cover it from an ancestor, so revoking
    # this row changes nothing for this principal at all. §6.2 says to
    # DISPLAY that ("already granted by Nav 1") and offer a tidy-up, never
    # to delete it for them.
    covering_grants: list[ResourceGrantResponse] = Field(default_factory=list)


class RetainedAccessAPIResponse(BaseModel):
    data: RetainedAccessResponse
