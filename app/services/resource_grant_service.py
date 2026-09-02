"""Object-grant lifecycle and the grant-matching primitive
(plan_access_control_algorithm_2026-08-27.md §7, §8.1 steps 1-2).

Split the same way ``rbac_service`` is split from ``rbac_graph_service``:
this module owns ``resource_grants`` rows' create/read/delete plus the one
read-time question built directly on them — *which stored grants does this
user reach?* It computes no visibility. The two folds of §5.1
(``granted`` descending the root path, ``visible`` ascending the subtree) are
deliberately NOT here.

Neither is the write GATE. ``app/routers/resource_grants.py`` now serves this
module, and who may call it is decided by
``app/services/resource_grant_authz_service.py`` — read that one before
changing anything here, because it is where the ⊥ decision lives. Nothing in
this module authorizes anybody, and it still enforces nothing about CONTENT:
no existing endpoint reads ``granted`` or ``visible``, and no response shape
any client reads today is affected by it.

There is no update path. A grant is three immutable facts — a node, a
principal, a level — and "changing" one is revoking it and writing another,
which is what makes the provenance columns meaningful and what keeps §6.2's
revoke-time confirmation the only place a grant ever disappears silently.
The uniqueness rule on the table says the same thing structurally: the only
mutable column would be one of the ones it keys on.

⊥ IS THE NORMAL CASE HERE, AND THAT IS THE OPPOSITE OF THE ASSIGNMENTS PATH.
``routers/rbac_assignments.py::_assert_not_public`` refuses ``is_public`` as
an ASSIGNMENT, and that refusal is a security control: holding ⊥ grants only
what everyone already reaches, and it would blow the delegation rule open,
since ``scope_descendants(anything)`` contains it for every user alive. On an
OBJECT GRANT the flag means the opposite — it is the whole point of it, the
ordinary case, and the thing that makes "publish to the whole hub" a single
row instead of one row per scope anybody might hold (§4.4). **That refusal is
deliberately not copied here.** Each flag has a lane: ``is_universal`` for
assignments, ``is_public`` for object grants.

Relatedly, and it reads like the opposite of what it is (§4.3): on a stored
grant ``All Scopes`` is the NARROWEST scope available, not the widest. It
reaches only users whose own assignment is hub-wide. Nothing here sorts,
filters or validates a grant on the assumption that the universal scope is
permissive, and §9 is explicit that the picker must label it rather than
forbid it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.hub import HubV2
from app.db_v2.models.hub_user import HubUserV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.resource_grant import (
    GRANT_LEVELS,
    NODE_COLUMNS,
    ResourceGrantV2,
)
from app.db_v2.models.role import RoleV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.tab import TabV2
from app.services.rbac_graph_service import (
    RbacClosures,
    RbacGraphError,
    effective_pairs,
)

# node kind -> the model carrying that node. Parallel to NODE_COLUMNS, and
# kept beside it for the same reason: adding a node kind is two entries, and
# a kind present in one map and missing from the other fails loudly here
# rather than writing a grant nothing can resolve.
NODE_MODELS: dict[str, type] = {
    "hub": HubV2,
    "nav_tab": NavTabV2,
    "tab": TabV2,
    "component": ComponentV2,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------
# Node addressing
# ---------------------------------------------------------


def _assert_known_kind(node_kind: str) -> None:
    if node_kind not in NODE_COLUMNS:
        raise RbacGraphError(
            "invalid_node_kind",
            (
                f"{node_kind!r} is not a grantable node kind. Expected one of: "
                f"{', '.join(NODE_COLUMNS)}."
            ),
            node_kind=node_kind,
        )


def _node_column(node_kind: str):
    _assert_known_kind(node_kind)
    return getattr(ResourceGrantV2, NODE_COLUMNS[node_kind])


def node_of(grant: ResourceGrantV2) -> tuple[str, int]:
    """``(kind, id)`` for a grant's node — the four-column arc read back as
    the one value every caller actually wants.

    The CHECK guarantees exactly one is non-null, so the first hit is the
    answer. The fallback raises rather than returning None: a row reaching
    here with no node set means the CHECK is missing from the live schema,
    and silently skipping it would drop a grant out of the read path.
    """
    for kind, column in NODE_COLUMNS.items():
        value = getattr(grant, column)
        if value is not None:
            return kind, value
    raise RbacGraphError(
        "grant_without_node",
        f"Grant {grant.id} has no node set — ck_resource_grants_one_node is not holding.",
        grant_id=grant.id,
    )


def serialize_grant(grant: ResourceGrantV2, *, user_email: str | None = None) -> dict:
    """`user_email` is resolved by the CALLER, never looked up in here — this
    stays a one-row, no-query function so a caller serializing many grants
    (``list_grants_for_node``, ``list_grants_by_ids``) can resolve every
    email in one bulk query first (``_hub_user_email_map``) rather than
    round-tripping per row. Pass ``None`` for a (role, scope)-form grant or
    when the caller has not resolved it; the schema treats both the same
    (handoff §4.1).
    """
    node_kind, node_id = node_of(grant)
    return {
        "id": grant.id,
        "node_kind": node_kind,
        "node_id": node_id,
        "role_id": grant.role_id,
        "scope_id": grant.scope_id,
        "user_id": grant.user_id,
        "user_email": user_email,
        "level": grant.level,
        "granted_by_user_id": grant.granted_by_user_id,
        "granted_by_email": grant.granted_by_email,
        "created_at": grant.created_at,
    }


def _hub_user_email_map(db: Session, user_ids: set[int]) -> dict[int, str]:
    """One query for every user-form grant's grantee a page of grants needs,
    rather than one per row — same shape as
    ``rbac_assignment_service._hub_user_email_map``, and the same live-join
    reasoning: ``user_id`` CASCADEs (§7), so a grant row can never outlive
    the ``hub_users`` row it names, and there is no snapshot column to fall
    back to the way ``granted_by_email`` is for the (SET NULL) granter side.
    """
    if not user_ids:
        return {}
    rows = db.query(HubUserV2.id, HubUserV2.email).filter(HubUserV2.id.in_(user_ids)).all()
    return {row[0]: row[1] for row in rows}


def _serialize_many(db: Session, rows: list[ResourceGrantV2]) -> list[dict]:
    """``serialize_grant`` over a whole result set, resolving every
    user-form grantee's email in ONE bulk query rather than N."""
    emails = _hub_user_email_map(db, {g.user_id for g in rows if g.user_id is not None})
    return [
        serialize_grant(g, user_email=emails.get(g.user_id) if g.user_id is not None else None)
        for g in rows
    ]


# ---------------------------------------------------------
# Validation
# ---------------------------------------------------------


def node_exists(db: Session, node_kind: str, node_id: int) -> bool:
    """Does this node exist? Public because the router needs the question
    without the exception — a GET wants a 404, not a 409.

    Deliberately answers only after the caller has been authorized for the
    node: on its own it is an existence oracle for the whole node tree, and
    §9 wants a hidden node to 404 rather than confirm itself.
    """
    _assert_known_kind(node_kind)
    model = NODE_MODELS[node_kind]
    return db.query(model.id).filter(model.id == node_id).first() is not None


def _assert_node_exists(db: Session, node_kind: str, node_id: int) -> None:
    """Named 409 instead of the FK's IntegrityError, same trade the rest of
    this codebase makes — see ``rbac_service.delete_role``'s pre-count."""
    model = NODE_MODELS[node_kind]
    if db.query(model.id).filter(model.id == node_id).first() is None:
        raise RbacGraphError(
            "node_not_found",
            f"No {node_kind} with id {node_id} exists.",
            node_kind=node_kind,
            node_id=node_id,
        )


def _assert_principal(
    db: Session, role_id: int | None, scope_id: int | None, user_id: int | None
) -> None:
    """Exactly one principal FORM, and whichever it is must exist.

    Mirrors ``ck_resource_grants_one_principal``. The CHECK is the backstop;
    this is what makes the failure legible, and it is also the only place that
    can say WHICH half is missing when someone sends a role with no scope.
    """
    has_pair = role_id is not None or scope_id is not None
    has_user = user_id is not None

    if has_pair and has_user:
        raise RbacGraphError(
            "ambiguous_principal",
            "A grant names either a (role, scope) pair or a user, never both.",
            role_id=role_id,
            scope_id=scope_id,
            user_id=user_id,
        )
    if not has_pair and not has_user:
        raise RbacGraphError(
            "missing_principal",
            "A grant must name either a (role, scope) pair or a user.",
        )

    if has_user:
        if db.query(HubUserV2.id).filter(HubUserV2.id == user_id).first() is None:
            raise RbacGraphError(
                "user_not_found", f"Hub user {user_id} does not exist", user_id=user_id
            )
        return

    # The pair is all-or-nothing. A role with no scope is the shape that
    # would quietly become "this role on every scope" if it were allowed
    # through, which is the pairing trap wearing a different hat.
    if role_id is None or scope_id is None:
        raise RbacGraphError(
            "incomplete_pair",
            "A principal pair needs both a role and a scope.",
            role_id=role_id,
            scope_id=scope_id,
        )
    if db.query(RoleV2.id).filter(RoleV2.id == role_id).first() is None:
        raise RbacGraphError("role_not_found", f"Role {role_id} does not exist", role_id=role_id)
    if db.query(ScopeV2.id).filter(ScopeV2.id == scope_id).first() is None:
        raise RbacGraphError(
            "scope_not_found", f"Scope {scope_id} does not exist", scope_id=scope_id
        )

    # NOTE, and it is load-bearing: there is deliberately NO is_public check
    # here. See the module docstring — ⊥ is refused as an assignment and is
    # the normal case on an object grant, and copying that refusal onto this
    # path would remove the one row that means "everyone".


def _assert_level(level: str) -> None:
    if level not in GRANT_LEVELS:
        raise RbacGraphError(
            "invalid_level",
            f"{level!r} is not a grant level. Expected one of: {', '.join(GRANT_LEVELS)}.",
            level=level,
        )


# ---------------------------------------------------------
# CRUD
# ---------------------------------------------------------


def list_grants_for_node(db: Session, node_kind: str, node_id: int) -> list[dict]:
    """Every grant stored ON this node — direct only.

    Direct only is not a limitation to fix later, it is the §9 requirement:
    the "who can access this node" panel lists direct and inherited grants
    SEPARATELY and names the ancestor an inherited one comes from, because
    conflating them is what makes §6.2's Alice case confusing. Inherited
    grants come from the descending fold, which is not built yet.
    """
    column = _node_column(node_kind)
    rows = (
        db.query(ResourceGrantV2)
        .filter(column == node_id)
        .order_by(ResourceGrantV2.level, ResourceGrantV2.id)
        .all()
    )
    return _serialize_many(db, rows)


def get_grant(db: Session, grant_id: int) -> dict | None:
    grant = db.query(ResourceGrantV2).filter(ResourceGrantV2.id == grant_id).first()
    if grant is None:
        return None
    user_email = None
    if grant.user_id is not None:
        row = db.query(HubUserV2.email).filter(HubUserV2.id == grant.user_id).first()
        user_email = row[0] if row is not None else None
    return serialize_grant(grant, user_email=user_email)


def list_grants_by_ids(db: Session, grant_ids: list[int]) -> list[dict]:
    """Full rows for a set of ids the caller already holds, in the order it
    gave them.

    Exists for §6.2's confirmation, which arrives holding ``GrantMatch``
    seeds — ``(grant_id, node, level)`` and nothing else — and has to render
    *"granted directly by Sam, 3 Feb"*. The provenance columns are the point:
    ``granted_by_email`` is the immutable snapshot that survives the granter
    leaving, which is exactly the case that sentence has to keep working in.

    One query, not one per id. The lists are small today, but this is called
    with every surviving seed inside a subtree and §5.5's no-break-glass rule
    makes per-child grants the normal way to author, so "small" is a property
    of current data rather than of the design.

    Order is preserved from ``grant_ids`` rather than re-sorted: the caller
    built that order from the fold, and a silent re-sort here would make the
    modal's list disagree with the traversal that produced it. Ids with no
    row are dropped rather than raising — a concurrently deleted grant is a
    shorter list, not a failed confirmation.
    """
    if not grant_ids:
        return []
    rows = db.query(ResourceGrantV2).filter(ResourceGrantV2.id.in_(set(grant_ids))).all()
    by_id = {row.id: row for row in rows}
    emails = _hub_user_email_map(db, {row.user_id for row in rows if row.user_id is not None})
    return [
        serialize_grant(
            by_id[gid],
            user_email=emails.get(by_id[gid].user_id) if by_id[gid].user_id is not None else None,
        )
        for gid in grant_ids
        if gid in by_id
    ]


def create_grant(
    db: Session,
    *,
    node_kind: str,
    node_id: int,
    level: str,
    role_id: int | None = None,
    scope_id: int | None = None,
    user_id: int | None = None,
    granted_by_user_id: int | None = None,
    granted_by_email: str | None = None,
) -> dict:
    """Write one grant on one node.

    ONE ROW ON ONE NODE, and nothing else — no cascade, no read-modify-write
    of any other node's grants, no dependence on the role DAG at write time.
    That is D4's "derive, never collapse" decision expressed in code
    (§6.2): the intuitive rule — "when granting edit, delete the now-redundant
    grants lower down" — is rejected, because the deletion is the only thing
    distinguishing it from derivation and it is irreversible. A lower grant
    that is already covered from above is DISPLAYED as redundant, never
    deleted; §6.2's confirmation modal is the one place lower rows ever go,
    and only because an admin read the list and asked.

    Does not commit — the caller owns the transaction, same contract as
    ``rbac_service.create_role``.
    """
    _assert_known_kind(node_kind)
    _assert_level(level)
    _assert_node_exists(db, node_kind, node_id)
    _assert_principal(db, role_id, scope_id, user_id)

    # Pre-check for the named error, exactly as delete_role pre-counts its
    # assignments: the partial unique index is the real guarantee, but an
    # IntegrityError surfacing as a 500 with an index name in it is not an
    # answer anyone can act on. The index remains the backstop under a race.
    column = _node_column(node_kind)
    duplicate = (
        db.query(ResourceGrantV2.id)
        .filter(
            column == node_id,
            ResourceGrantV2.role_id == role_id,
            ResourceGrantV2.scope_id == scope_id,
            ResourceGrantV2.user_id == user_id,
            ResourceGrantV2.level == level,
        )
        .first()
    )
    if duplicate is not None:
        raise RbacGraphError(
            "duplicate_grant",
            f"That principal already has {level!r} on this {node_kind}.",
            node_kind=node_kind,
            node_id=node_id,
            level=level,
        )

    grant = ResourceGrantV2(
        **{NODE_COLUMNS[node_kind]: node_id},
        role_id=role_id,
        scope_id=scope_id,
        user_id=user_id,
        level=level,
        granted_by_user_id=granted_by_user_id,
        granted_by_email=granted_by_email,
        created_at=_utc_now(),
    )
    db.add(grant)
    db.flush()
    user_email = None
    if grant.user_id is not None:
        row = db.query(HubUserV2.email).filter(HubUserV2.id == grant.user_id).first()
        user_email = row[0] if row is not None else None
    return serialize_grant(grant, user_email=user_email)


def delete_grant(db: Session, grant_id: int) -> bool:
    """Revoke one grant. False if it was not there.

    No validation, and deliberately none: removing a grant only ever shrinks
    reachability, the same reason ``delete_role_edge`` validates nothing. The
    ⊥ precedent applies here too and is worth restating — refuse the way IN,
    never the way OUT. A ⊥ grant written before any guard existed, or
    straight into the database, must stay removable, or the widest grant in
    the system is the one thing nobody can take away.

    §6.2's "what would they still retain?" confirmation belongs ABOVE this
    call, not inside it: it is advisory, `[ Leave it ]` is a legitimate
    answer, and this function must stay the plain single-row revoke that the
    approved multi-row path calls once per named row.
    """
    grant = db.query(ResourceGrantV2).filter(ResourceGrantV2.id == grant_id).first()
    if grant is None:
        return False
    db.delete(grant)
    db.flush()
    return True


# ---------------------------------------------------------
# §8.1 step 2 — which stored grants does this user reach?
# ---------------------------------------------------------


class GrantMatch(NamedTuple):
    """One stored grant this user reaches. ``S`` in §8.1's step 2 — the SEED
    SET the two folds of §5.1 consume, and the boundary this step stops at."""

    grant_id: int
    node_kind: str
    node_id: int
    level: str


def matching_grants(
    db: Session, hub_user_id: int, *, closures: RbacClosures | None = None
) -> list[GrantMatch]:
    """Every grant reached by ``hub_user_id``'s effective pairs, plus every
    grant naming them directly (§8.1 step 2, §4.2, D5).

    §8.1's step 1 and step 2 in that order: ``effective_pairs`` computes E
    per assignment ROW, and ``grants_matching_pairs`` does the lookup. The
    mechanics — and the pairing trap they exist to survive — live there;
    this is the entry point every read path should call.

    THIS IS THE ONLY CORRECT WAY TO ASK "which grants does this user reach".
    Do not write the question again anywhere else.

    Pass ``closures`` to share one snapshot with the rest of a request — the
    adjacency is loaded once per snapshot, never rebuilt per node (§8.2).
    """
    pairs = effective_pairs(db, hub_user_id, closures=closures)
    return grants_matching_pairs(db, pairs=pairs, user_id=hub_user_id)


def grants_matching_pairs(
    db: Session, *, pairs: set[tuple[int, int]], user_id: int | None
) -> list[GrantMatch]:
    """The lookup itself: every grant whose principal is one of ``pairs``,
    plus every grant naming ``user_id`` directly.

    SPLIT OUT OF ``matching_grants`` SO THE PAIRING GUARD HAS EXACTLY ONE
    HOME. Two callers need this question asked about two different pair
    sets — the read path asks it about a USER's ``effective_pairs``, and
    §6.2's revoke-time confirmation asks it about ONE PRINCIPAL's two
    closures (``seeds_for_principal``). Giving the second caller its own
    query is how the trap below gets reintroduced in a sixth place, so it
    does not get one.

    ``pairs`` must already have been built PER ASSIGNMENT ROW — one row's
    role closure crossed with that same row's scope closure, unioned
    afterwards. This function cannot check that for you, and it is the one
    precondition that matters. ``effective_pairs`` is the only thing that
    should ever construct it from a user; ``seeds_for_principal`` constructs
    it from a single pair, where there is only one row and so nothing to
    cross wrongly.

    Pass ``user_id=None`` for a principal that is not a person — a pair
    principal reaches no user-form grants, and must not.

    TWO STAGES, AND THE SPLIT IS THE WHOLE DESIGN. The SQL below is a
    deliberately WIDE filter — ``role_id IN roles(E) AND scope_id IN
    scopes(E)`` — and **on its own it is wrong**. It crosses the union of the
    user's reachable roles with the union of their reachable scopes, so a
    user holding (Program Lead, A) and (Program Member, B) would have it
    return a grant stored on (Program Lead, B). That is precisely the pairing
    bug ``role_assignments`` exists to prevent, and it is the fifth place it
    can be reintroduced. It is tolerated here, and only here, because the
    Python filter below re-narrows it and because keeping the query trivial
    is what makes this one round trip instead of one per assignment row.

    **Do not move the pairing into SQL.** ``effective_pairs`` has already
    done it correctly — it crosses each assignment row's two closures INSIDE
    the loop and unions afterwards — so the membership test below is exactly
    §4.2's match:

        a stored (R_g, S_g) matches iff R_g ∈ role_descendants*(R_h)
        AND S_g ∈ scope_descendants*(S_h) for ONE held row (R_h, S_h)

    which is what ``(role_id, scope_id) in pairs`` asks, per row, for free.
    The direction is the counterintuitive half: the stored pair must sit AT
    OR BELOW a held pair, on both axes. A grant on (Hub Member, Scope X) is
    matched by holding (Hub Member, All Scopes); a grant on
    (Hub Member, All Scopes) is NOT matched by holding (Hub Member, Scope X),
    because nothing walks up into the universal scope.

    Returns the seed set only. It does not fold, and it says nothing about
    what is visible: a node is granted if a seed lies anywhere on its root
    path, and visible if one lies anywhere in its subtree (§5.1). That is
    ``access_visibility_service``'s job.
    """
    reachable_role_ids = {role_id for role_id, _ in pairs}
    reachable_scope_ids = {scope_id for _, scope_id in pairs}

    pair_filter = and_(
        ResourceGrantV2.role_id.in_(reachable_role_ids),
        ResourceGrantV2.scope_id.in_(reachable_scope_ids),
    )
    # A pair principal names no person, so there is no user arm at all.
    #
    # THIS BRANCH IS AN EFFICIENCY AND LEGIBILITY GUARD, NOT A CORRECTNESS
    # ONE, and saying so is the honest version — it was mutation-tested on
    # 2026-08-31 and the mutation SURVIVED, correctly. Written without it,
    # `ResourceGrantV2.user_id == None` renders as `user_id IS NULL`, which
    # matches every PAIR-form row in the table (they all have a null
    # user_id) — so the query would drag the whole table into Python, where
    # the pairing guard below would then narrow it back to the same answer.
    # Same result, arbitrarily more rows. It is the shape that misleads:
    # `== None` silently becoming `IS NULL` reads like a deliberate "match
    # the rows with no user", which is not what a pair principal wants to
    # say.
    #
    # The Python guard below remains the thing that makes the ANSWER right,
    # here exactly as it does for the wide pair filter. Do not move either
    # of them into SQL.
    where = pair_filter if user_id is None else or_(ResourceGrantV2.user_id == user_id, pair_filter)

    rows = db.query(ResourceGrantV2).filter(where).all()

    matched: list[GrantMatch] = []
    for grant in rows:
        if grant.user_id is not None:
            # A user-form grant reached this far only by naming them, since
            # the pair half of the filter cannot match a row whose role_id
            # and scope_id are both NULL.
            if grant.user_id != user_id:
                continue
        elif (grant.role_id, grant.scope_id) not in pairs:
            # The wide filter admitted a cross-product pair the principal does
            # not actually hold together. This branch IS the pairing guard.
            continue

        node_kind, node_id = node_of(grant)
        matched.append(
            GrantMatch(
                grant_id=grant.id,
                node_kind=node_kind,
                node_id=node_id,
                level=grant.level,
            )
        )

    # Deterministic order so callers and tests can compare results directly;
    # the seed set itself is a set, and the folds are order-independent (§5.1).
    matched.sort(key=lambda m: m.grant_id)
    return matched


def seeds_for_principal(
    db: Session,
    *,
    role_id: int | None,
    scope_id: int | None,
    user_id: int | None,
    closures: RbacClosures | None = None,
) -> list[GrantMatch]:
    """Every grant ONE PRINCIPAL reaches — §6.2's revoke-time confirmation,
    step one.

    §6.2 requires that revoking a grant first shows *"what would this
    principal still retain?"*, and describes it as the §8.1 read algorithm
    restricted to one principal. That restriction is exactly this function:
    it produces a seed set the same shape ``matching_grants`` produces, so
    ``access_visibility_service.fold`` consumes it unchanged and there is no
    second traversal anywhere.

    THE TWO PRINCIPAL FORMS ARE NOT THE SAME QUESTION, and conflating them
    would make the confirmation lie in the dangerous direction:

    - A **user** principal is a real person with real assignments, so the
      honest answer is what that person reaches — ``matching_grants``
      itself, unchanged. This is §6.2's Alice: removing her from Nav 1 must
      surface the grant Sam wrote on Root Tab 2, and it does so whether Sam
      wrote it to her directly or to a pair she happens to match.

    - A **pair** principal is an AUDIENCE, not a person. The answer is what
      a holder of exactly that pair would reach, which is that one pair's
      two closures crossed. Note what this deliberately is NOT: the set of
      OTHER grants written on the identical pair. That narrower reading is
      the tempting one — it needs no closures at all — and it under-reports.
      Revoke ``(Program Member, A)`` from Nav 1 while ``(Hub Member, A)``
      sits on Root Tab 2 and the narrow reading says "they retain nothing",
      which is a false reassurance handed to an admin at exactly the moment
      they are deciding whether to remove more rows.

    ONE PAIR IS ONE ROW, so the cross below cannot commit the pairing bug —
    there is no union of roles and no union of scopes to cross, only one
    closure against one closure, which is precisely what ``effective_pairs``
    does inside its own loop for a single assignment. Building it here
    rather than calling ``effective_pairs`` is not a duplicate of that rule:
    a pair principal has no ``role_assignments`` row to read, and inventing
    a fake user to get one would be worse in every way.

    Reports what the principal reaches, INCLUDING the grant about to be
    revoked. Dropping that row is the caller's job — see
    ``access_visibility_service.what_would_they_retain``, which is the only
    thing that should call this — because the drop is what makes the answer
    a hypothetical rather than a description of today.
    """
    if user_id is not None:
        return matching_grants(db, user_id, closures=closures)

    if role_id is None or scope_id is None:
        raise RbacGraphError(
            "incomplete_pair",
            "A principal pair needs both a role and a scope.",
            role_id=role_id,
            scope_id=scope_id,
        )

    graph = closures if closures is not None else RbacClosures(db)
    pairs = {
        (reached_role, reached_scope)
        for reached_role in graph.role_descendants(role_id)
        for reached_scope in graph.scope_descendants(scope_id)
    }
    return grants_matching_pairs(db, pairs=pairs, user_id=None)
