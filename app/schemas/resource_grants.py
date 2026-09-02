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

    # A LIVE LOOKUP, not a snapshot — safe because `user_id` CASCADEs
    # (§7's table: "user_id FK hub_users ON DELETE CASCADE"), so a grant row
    # can never outlive the hub_users row it names. That is the opposite
    # rule from `granted_by_email` two fields below, whose FK is SET NULL
    # rather than CASCADE and therefore does need a stored snapshot. Same
    # split `rbac_assignment_service._hub_user_email_map` already draws
    # between `AssignmentResponse.user_email` (live) and
    # `granted_by_email` (snapshot).
    #
    # Handoff §4.1: added because no endpoint could previously turn a bare
    # `user_id` on a user-form grant back into a name — the only lookup on
    # `hub_users` was a search-by-query, not a get-by-id. Always `None` for
    # a (role, scope)-form grant, and — for a user-form grant — `None` only
    # in the impossible case the CASCADE has not yet caught up within one
    # transaction; a client should treat that identically to "unresolved".
    user_email: str | None = None

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


# ---------------------------------------------------------
# §0.1 — inherited grants (handoff 2026-09-02, plan §9)
# ---------------------------------------------------------


class InheritedGrantsEntry(BaseModel):
    """Every grant stored on ONE ancestor of the node the caller asked about
    — the unit `list_node_grants` (direct-only) was always missing.

    Grouped rather than flattened, per §9's own wording: "listing direct and
    inherited grants SEPARATELY and naming the ancestor". A flat list with a
    tag on each row says the same thing but makes an admin re-group it by
    eye to answer "what does Nav 1 contribute here?" — the exact question
    §6.2's Alice case turns on.
    """

    node_kind: NodeKind
    node_id: int
    grants: list[ResourceGrantResponse] = Field(default_factory=list)


class InheritedGrantsAPIResponse(BaseModel):
    # Nearest ancestor first, root-ward. Ancestors with no grants of their
    # own are omitted — there is nothing to name them for, and an admin
    # scanning the list for "why is this visible" gets only entries that
    # answer the question.
    data: list[InheritedGrantsEntry] = Field(default_factory=list)


# ---------------------------------------------------------
# §0.2 — audience count (handoff 2026-09-02, plan §9)
# ---------------------------------------------------------


class AudienceCountResponse(BaseModel):
    """*"This grant currently reaches 34 people"* (§9), computed for a
    `(role, scope)` pair rather than for an existing grant row — the picker
    needs this BEFORE the grant is written, while the admin is still
    choosing.

    A count, not a list: §9's own phrasing asks for a number, and naming
    every individual who happens to hold a role/scope pair is a materially
    bigger exposure than the pair itself (role and scope NAMES are open to
    any authenticated user today; a roster of who holds one is not).
    """

    role_id: int
    scope_id: int
    count: int


class AudienceCountAPIResponse(BaseModel):
    data: AudienceCountResponse


# ---------------------------------------------------------
# §0.3 — what can this person access (handoff 2026-09-02, plan §9)
# ---------------------------------------------------------


class UserAccessNodeResponse(BaseModel):
    """One node in the caller's `visible` set for the target person, with
    §5.2's full triple — not just a bare node list, so the panel can show a
    reveal (`revealed=True`) differently from a real grant."""

    node_kind: NodeKind
    node_id: int
    view: bool
    edit: bool
    revealed: bool


class UserAccessResponse(BaseModel):
    user_id: int
    # Only VISIBLE nodes — §5.1's `visible` set, not the whole node tree.
    # An invisible node carries no information for this panel and, at
    # today's ~240-node hub, omitting it is what keeps the response
    # proportional to what the person can actually reach rather than to the
    # size of the hub.
    nodes: list[UserAccessNodeResponse] = Field(default_factory=list)


class UserAccessAPIResponse(BaseModel):
    data: UserAccessResponse
