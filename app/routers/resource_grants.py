"""Object grants router
(plan_access_control_algorithm_2026-08-27.md §6.2, §6.3, §7, §9;
session_handoff_2026-09-02-grant-editor.md §0).

The grants surface: list what is stored on a node (direct AND, since this
session, inherited-from-ancestor), write a grant, revoke one, ask what a
principal would still retain if a grant were revoked (§6.2 — ships WITH the
write path, not after it), preview how many people a `(role, scope)` pair
would reach before writing a grant on it, and list every node one person can
currently reach.

Its own module rather than more routes on `rbac.py`, following
`rbac_assignments.py`'s precedent. The three surfaces are genuinely
different: `rbac.py` owns role/scope DEFINITIONS behind `require_hub_admin`,
`rbac_assignments.py` owns WHO HOLDS WHAT behind the §6.1 delegation rule,
and this owns WHAT CONTENT REACHES WHOM behind a third rule again — the
first of the three that is evaluated against a NODE. They share a URL prefix
because they are one product surface; they share no gate. Two of this
session's additions — the audience preview and the per-person access list —
have no node to evaluate at all and fall back to `require_hub_admin`
instead; see `ADMIN_ONLY`'s own comment for why.

═══════════════════════════════════════════════════════════════════════════
NOTHING HERE ENFORCES ANYTHING ABOUT CONTENT
═══════════════════════════════════════════════════════════════════════════

This router is additive. No existing endpoint reads `granted` or `visible`,
no response shape any client reads today changed when it landed, and no
signed-in user's view of any content is different because it exists. The
only thing gated below is the grants surface itself.

That is a step boundary, not an accident. §10 orders the cutover — populate
assignments, author grants, verify, and only THEN flip enforcement — and
both tables are empty in production today, so flipping first would take the
hub dark for everyone at once. Wiring these folds into a content endpoint is
a later step and a deliberate one.

═══════════════════════════════════════════════════════════════════════════
THE GATE, INCLUDING ON READS
═══════════════════════════════════════════════════════════════════════════

Every route here requires `edit` on the node in question, with the Hub Admin
bypass first and unconditional. The write rule is
`resource_grant_authz_service.assert_can_grant`, which is where the ⊥
reasoning and the §0 decision of 2026-08-31 are recorded — read that module
before changing anything below.

READS ARE GATED THE SAME WAY, and that is a judgement call worth stating
because §9 does not specify it. The alternative — gating the list on
`view(n)` — would hand the access list for a node to everyone who can see
the node, which is a strictly larger audience than the people who administer
it. "Who can reach this?" is an administrative view: it enumerates roles,
scopes and named individuals, and it is the read half of the same §6.3 row
that governs the write ("Edit `n`'s grants — requires edit on `n`"). Since
this is a new endpoint, gating it cannot change anyone's existing view.

`_conflict` maps every rule failure onto 409, matching `rbac.py` and
`rbac_assignments.py` — these are collisions with the CURRENT STATE (you do
not hold edit on this node right now; that principal already has this level)
rather than malformed requests, and a body rejected today may be accepted
tomorrow once an assignment exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.db_v2.models.hub_user import HubUserV2
from app.dependencies import CurrentHubUser, get_current_hub_user, require_hub_admin
from app.schemas.resource_grants import (
    AudienceCountAPIResponse,
    CreateGrantRequest,
    InheritedGrantsAPIResponse,
    NodeKind,
    ResourceGrantAPIResponse,
    ResourceGrantListAPIResponse,
    RetainedAccessAPIResponse,
    UserAccessAPIResponse,
)
from app.services import rbac_service
from app.services import resource_grant_service as grants
from app.services.access_visibility_service import (
    build_node_tree,
    compute_visibility,
    list_inherited_grants,
    list_visible_nodes,
    what_would_they_retain,
)
from app.services.rbac_graph_service import RbacClosures, RbacGraphError, audience_count
from app.services.resource_grant_authz_service import (
    assert_can_administer_node,
    assert_can_grant,
)
from app.services.tab_service import HUB_ADMIN_ROLE

router = APIRouter(prefix="/v2/rbac", tags=["Access Control — Grants"])

CONFLICT_RESPONSE = {
    409: {"description": "The write gate, a uniqueness rule, or a node/principal rule was violated"}
}

# §0.2 / §0.3's gate (handoff 2026-09-02): both endpoints are org-wide admin
# views with no NODE to hang `edit(n)` on — an audience count is asked about
# a (role, scope) pair before any grant naming it exists, and a per-person
# access list is about a PERSON, not a node. §12 leaves delegated
# grant-administration undesigned, so both stay behind the same identity
# gate role/scope DEFINITION writes already use (`rbac.py`'s `ADMIN_ONLY`),
# rather than inventing a node-based rule for a question that has none.
ADMIN_ONLY = [Depends(require_hub_admin)]


def _conflict(error: RbacGraphError) -> HTTPException:
    """Same shape and same status as `rbac.py`'s and
    `rbac_assignments.py`'s — duplicated rather than imported for the reason
    the latter already records: it is five lines, and importing it would
    couple three routers together to avoid a duplicate."""
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": error.message, **error.details},
    )


def _is_hub_admin(current: CurrentHubUser) -> bool:
    """Reads the JWT's role names, exactly as `require_hub_admin` and
    `rbac_assignments._is_hub_admin` do.

    THE THIRD COPY, AND DELIBERATELY SO FOR NOW. §6.8 calls for a single
    `is_hub_admin(db, current)` resolver so that §10 item 6's re-pointing at
    `role_assignments` is one edit rather than a hunt — but that re-pointing
    has an ordering requirement the plan calls "not negotiable" (re-point
    BEFORE Airtable stops issuing roles into the JWT, never after) and it
    belongs with the cutover, not here. Consolidating the three now would
    move the gate without moving the sequencing, which is the half that
    matters. Three greppable copies of one line are a better handover than
    one shared helper that hides which surfaces the cutover has to visit.
    """
    return HUB_ADMIN_ROLE in (current.info.roles or [])


# ---------------------------------------------------------
# Reads
# ---------------------------------------------------------


@router.get(
    "/nodes/{node_kind}/{node_id}/grants",
    response_model=ResourceGrantListAPIResponse,
    summary="Grants stored directly ON this node",
    responses={404: {"description": "Node not found"}, **CONFLICT_RESPONSE},
)
def list_node_grants(
    node_kind: NodeKind,
    node_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """DIRECT GRANTS ONLY — inherited ones are deliberately not merged in.

    §9 requires the "who can access this node" panel to list direct and
    inherited grants SEPARATELY and to name the ancestor an inherited one
    comes from, because conflating them is what makes §6.2's Alice case
    confusing: an admin who cannot tell which rows live HERE cannot tell
    which ones revoking here would remove. Anything inherited is derived
    from the descending fold and belongs in its own response, not blended
    into this list.
    """
    # The NODE half of the gate only — a read has no principal to test.
    try:
        assert_can_administer_node(
            db,
            hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            node_kind=node_kind,
            node_id=node_id,
        )
    except RbacGraphError as e:
        raise _conflict(e)

    # AFTER the gate, never before. A caller without edit on the node has
    # already been refused with a message that does not distinguish
    # "uneditable" from "does not exist", so this 404 can only be reached by
    # someone entitled to know the difference — in practice a Hub Admin, who
    # bypasses the gate and would otherwise get a silent empty list for a
    # node id that is simply wrong.
    if not grants.node_exists(db, node_kind, node_id):
        raise HTTPException(status_code=404, detail=f"No {node_kind} with id {node_id} exists")

    return {"data": grants.list_grants_for_node(db, node_kind, node_id)}


@router.get(
    "/nodes/{node_kind}/{node_id}/grants/inherited",
    response_model=InheritedGrantsAPIResponse,
    summary="Grants stored on this node's ancestors, grouped by ancestor",
    responses={404: {"description": "Node not found"}, **CONFLICT_RESPONSE},
)
def list_node_inherited_grants(
    node_kind: NodeKind,
    node_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """The ancestor half of "who can access this node" (§9), which
    `list_node_grants` above never served — see its own docstring: inherited
    grants come from the descending fold, and that fold's read side is
    `access_visibility_service.list_inherited_grants` (handoff §0.1).

    SAME GATE, SAME ORDER, FOR THE SAME REASON as `list_node_grants`: an
    administrative view of a node is gated on `edit(n)` even though it is a
    read, and the gate runs BEFORE the existence check so a caller without
    edit on the node gets the same answer whether it is absent, orphaned,
    invisible, or uneditable (§9: a hidden node should not confirm itself
    exists).
    """
    try:
        assert_can_administer_node(
            db,
            hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            node_kind=node_kind,
            node_id=node_id,
        )
    except RbacGraphError as e:
        raise _conflict(e)

    if not grants.node_exists(db, node_kind, node_id):
        raise HTTPException(status_code=404, detail=f"No {node_kind} with id {node_id} exists")

    return {"data": list_inherited_grants(db, node_kind, node_id)}


@router.get(
    "/audience",
    response_model=AudienceCountAPIResponse,
    dependencies=ADMIN_ONLY,
    summary="How many people a (role, scope) pair currently reaches",
    responses={404: {"description": "Role or scope not found"}},
)
def get_audience_count(
    role_id: int = Query(...),
    scope_id: int = Query(...),
    db: Session = Depends(get_db_v2),
):
    """*"This grant currently reaches 34 people"* (§9) — a live preview for
    the grant-creation picker, computed for a pair BEFORE any grant naming it
    exists, not read back off a stored row. See `ADMIN_ONLY`'s comment above
    for why this is Hub-Admin-gated rather than node-gated (handoff §0.2).

    404 on an unknown role or scope, checked before anything closure-related
    runs — same ordering `create_assignment` already uses.
    """
    if rbac_service.get_role(db, role_id) is None:
        raise HTTPException(status_code=404, detail="Role not found")
    if rbac_service.get_scope(db, scope_id) is None:
        raise HTTPException(status_code=404, detail="Scope not found")

    count = audience_count(db, role_id, scope_id)
    return {"data": {"role_id": role_id, "scope_id": scope_id, "count": count}}


@router.get(
    "/hub-users/{user_id}/access",
    response_model=UserAccessAPIResponse,
    dependencies=ADMIN_ONLY,
    summary="Every node this person can currently reach",
    responses={404: {"description": "Hub user not found"}},
)
def get_user_access(
    user_id: int,
    db: Session = Depends(get_db_v2),
):
    """§9's "what can this person access" panel (handoff §0.3) — the
    per-user mirror of `list_node_grants` / `list_node_inherited_grants`'s
    per-node view. See `ADMIN_ONLY`'s comment above for why this is
    Hub-Admin-gated rather than node-gated: a person is not a node, so there
    is nothing to run `edit()` against.

    Runs the full §5.1 fold for this one user (`compute_visibility`) and
    returns only their `visible` set with the §5.2 triple — see
    `list_visible_nodes`'s docstring for why the invisible majority is
    omitted rather than returned as `view: false` rows.
    """
    if db.query(HubUserV2.id).filter(HubUserV2.id == user_id).first() is None:
        raise HTTPException(status_code=404, detail="Hub user not found")

    visibility = compute_visibility(db, user_id)
    return {"data": {"user_id": user_id, "nodes": list_visible_nodes(visibility)}}


@router.get(
    "/grants/{grant_id}/retained",
    response_model=RetainedAccessAPIResponse,
    summary="§6.2 — what would this principal still retain if this grant were revoked?",
    responses={404: {"description": "Grant not found"}, **CONFLICT_RESPONSE},
)
def get_retained_access(
    grant_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Run this BEFORE the DELETE and show the answer. It is what makes D4
    safe.

    D4 chose to derive edit grants at read time rather than collapse them
    into storage, so revoking a high grant leaves the low ones in place.
    Without this, the admin's model ("Alice has nothing now") silently
    diverges from the truth — which is precisely the surprise the collapse
    rule was meant to prevent, relocated to where it can be answered
    honestly.

    IT IS ADVISORY AND IT CHANGES NOTHING. Nothing is deleted, no transaction
    is opened, and the grant still exists when this returns — the drop
    happens on the seed set, in memory. `[ Leave it ]` is a legitimate
    answer (§6.2), the DELETE below does not require this to have been
    called, and `[ Remove that too ]` is an ordinary multi-row revoke the
    admin has read and approved, one DELETE per named row.
    """
    grant = grants.get_grant(db, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")

    # ONE SNAPSHOT, SHARED (§8.2, §8.4). This route asks two questions that
    # both need the closures and the tree — the gate's `edit(node)` for the
    # CALLER, and the retained fold for the grant's PRINCIPAL, who is
    # usually somebody else — so building them here and passing them down is
    # the difference between one load of each edge table and two. Never hold
    # either across requests: both are authorization inputs.
    closures = RbacClosures(db)
    tree = build_node_tree(db)

    try:
        assert_can_administer_node(
            db,
            hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            node_kind=grant["node_kind"],
            node_id=grant["node_id"],
            closures=closures,
            tree=tree,
        )
    except RbacGraphError as e:
        raise _conflict(e)

    retained = what_would_they_retain(db, grant_id, closures=closures, tree=tree)
    if retained is None:
        # Only reachable if the grant was deleted between the read above and
        # here. A 404 is the honest answer, and it is the same one the
        # caller would have got a moment earlier.
        raise HTTPException(status_code=404, detail="Grant not found")

    node_kind, node_id = retained.node
    return {
        "data": {
            "node_kind": node_kind,
            "node_id": node_id,
            "retained_view": [{"node_kind": k, "node_id": i} for k, i in retained.retained_view],
            "retained_edit": [{"node_kind": k, "node_id": i} for k, i in retained.retained_edit],
            "responsible_grants": grants.list_grants_by_ids(
                db, [m.grant_id for m in retained.responsible_grants]
            ),
            "covering_grants": grants.list_grants_by_ids(
                db, [m.grant_id for m in retained.covering_grants]
            ),
        }
    }


# ---------------------------------------------------------
# Writes
# ---------------------------------------------------------


@router.post(
    "/grants",
    response_model=ResourceGrantAPIResponse,
    status_code=201,
    summary="Grant a principal view or edit on a node",
    responses=CONFLICT_RESPONSE,
)
def create_grant(
    request: CreateGrantRequest,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """THE GATE RUNS FIRST, BEFORE ANY VALIDATION OF THE NODE OR THE
    PRINCIPAL, and the order is the opposite of `create_assignment`'s on
    purpose.

    There, role and scope 404s are checked before the delegation gate, so a
    bad id never leaks whether it would otherwise have been delegable —
    which is safe because roles and scopes are global org structure anyone
    may list. Here the sensitive fact is the NODE: whether it exists, and
    what it is. Validating it first would let anyone probe the node tree by
    watching a 409 turn into a 404. So the node half of the gate runs first
    and answers uniformly for absent, orphaned, invisible and uneditable
    nodes alike; principal validation happens afterwards, inside
    `create_grant`, where a 409 leaks nothing that `/v2/rbac/roles` does not
    already publish.

    ⊥ IS A LEGAL PRINCIPAL HERE. `rbac_assignments._assert_not_public` has
    no counterpart on this path, deliberately — see
    `resource_grant_authz_service`'s module docstring, which records the
    measurement and the owner's decision.
    """
    try:
        assert_can_grant(
            db,
            granter_hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            node_kind=request.node_kind,
            node_id=request.node_id,
            role_id=request.role_id,
            scope_id=request.scope_id,
            user_id=request.user_id,
        )
        grant = grants.create_grant(
            db,
            node_kind=request.node_kind,
            node_id=request.node_id,
            level=request.level,
            role_id=request.role_id,
            scope_id=request.scope_id,
            user_id=request.user_id,
            granted_by_user_id=current.hub_user_id,
            granted_by_email=current.email,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
    return {"data": grant}


@router.delete(
    "/grants/{grant_id}",
    status_code=204,
    summary="Revoke a grant",
    responses={404: {"description": "Grant not found"}, **CONFLICT_RESPONSE},
)
def delete_grant(
    grant_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Symmetric with create, not ownership-based — the same choice
    `delete_assignment` makes and for the same reason: the gate re-runs
    against the grant's OWN node and principal regardless of who wrote it.
    There is deliberately no "I granted it, so I may remove it" shortcut,
    and equally no requirement that you granted it.

    A ⊥ GRANT IS STILL REVOCABLE, and that is not incidental. Both ⊥ rows
    sit in every user's effective pairs by construction, so anyone who
    passes the node half of the gate passes the pair half for ⊥ too, and a
    Hub Admin passes regardless. `resource_grant_service.delete_grant`
    itself validates nothing about the row's content, deliberately: refuse
    the way IN, never the way OUT. The widest grant in the system must never
    become the one thing nobody can take away.

    §6.2's confirmation is a separate GET and is not required to have been
    called. It is advisory, and making it a precondition would turn
    `[ Leave it ]` into a dead end.
    """
    grant = grants.get_grant(db, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")

    try:
        assert_can_grant(
            db,
            granter_hub_user_id=current.hub_user_id,
            is_hub_admin=_is_hub_admin(current),
            node_kind=grant["node_kind"],
            node_id=grant["node_id"],
            role_id=grant["role_id"],
            scope_id=grant["scope_id"],
            user_id=grant["user_id"],
        )
        grants.delete_grant(db, grant_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
