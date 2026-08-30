"""Object-grant lifecycle and the grant-matching primitive
(plan_access_control_algorithm_2026-08-27.md §7, §8.1 steps 1-2).

Split the same way ``rbac_service`` is split from ``rbac_graph_service``:
this module owns ``resource_grants`` rows' create/read/delete plus the one
read-time question built directly on them — *which stored grants does this
user reach?* It computes no visibility. The two folds of §5.1
(``granted`` descending the root path, ``visible`` ascending the subtree) are
deliberately NOT here, and neither is any enforcement: nothing in this module
is wired into an endpoint, and no response shape any client reads today is
affected by it.

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


def serialize_grant(grant: ResourceGrantV2) -> dict:
    node_kind, node_id = node_of(grant)
    return {
        "id": grant.id,
        "node_kind": node_kind,
        "node_id": node_id,
        "role_id": grant.role_id,
        "scope_id": grant.scope_id,
        "user_id": grant.user_id,
        "level": grant.level,
        "granted_by_user_id": grant.granted_by_user_id,
        "granted_by_email": grant.granted_by_email,
        "created_at": grant.created_at,
    }


# ---------------------------------------------------------
# Validation
# ---------------------------------------------------------


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
    return [serialize_grant(g) for g in rows]


def get_grant(db: Session, grant_id: int) -> dict | None:
    grant = db.query(ResourceGrantV2).filter(ResourceGrantV2.id == grant_id).first()
    return None if grant is None else serialize_grant(grant)


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
    return serialize_grant(grant)


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

    Pass ``closures`` to share one snapshot with the rest of a request — the
    adjacency is loaded once per snapshot, never rebuilt per node (§8.2).

    Returns the seed set only. It does not fold, and it says nothing about
    what is visible: a node is granted if a seed lies anywhere on its root
    path, and visible if one lies anywhere in its subtree (§5.1). Both are
    the next step.
    """
    pairs = effective_pairs(db, hub_user_id, closures=closures)

    reachable_role_ids = {role_id for role_id, _ in pairs}
    reachable_scope_ids = {scope_id for _, scope_id in pairs}

    rows = (
        db.query(ResourceGrantV2)
        .filter(
            or_(
                # D5's direct grants. Unconditional — no closure is involved,
                # the row names this person.
                ResourceGrantV2.user_id == hub_user_id,
                and_(
                    ResourceGrantV2.role_id.in_(reachable_role_ids),
                    ResourceGrantV2.scope_id.in_(reachable_scope_ids),
                ),
            )
        )
        .all()
    )

    matched: list[GrantMatch] = []
    for grant in rows:
        if grant.user_id is not None:
            # A user-form grant reached this far only by naming them, since
            # the pair half of the filter cannot match a row whose role_id
            # and scope_id are both NULL.
            if grant.user_id != hub_user_id:
                continue
        elif (grant.role_id, grant.scope_id) not in pairs:
            # The wide filter admitted a cross-product pair the user does not
            # actually hold together. This branch IS the pairing guard.
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
