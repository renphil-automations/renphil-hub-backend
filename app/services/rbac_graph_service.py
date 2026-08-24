"""Role/scope graph validation
(plan_access_control_schema_2026-08-22.md §5).

Owns every rule the two DAGs obey that a per-row CHECK constraint cannot
express. The models carry the per-row half (no self-edge, no duplicate edge,
at most one universal scope); everything spanning more than one row lives
here, and nothing may write ``parent_child_roles`` / ``parent_child_scopes`` / ``roles.rank``
without going through this module.

THE TWO GRAPHS ARE NOT SYMMETRIC, which is the thing most likely to trip up
a reader:

  roles   — acyclicity is FREE. Every edge must satisfy
            ``parent.rank < child.rank`` (§5.2), so every path strictly
            increases rank and none can return to its origin. No cycle walk,
            no advisory lock. The cost is §5.3: editing a rank after edges
            exist is the only way to break it, so ``set_role_rank`` has to
            re-validate every incident edge.

  scopes  — no rank, so acyclicity is a real descendant walk (§5.4) and every
            write must hold an advisory lock (§5.6). Two concurrent inserts
            can each be individually acyclic yet jointly form a cycle.

Delegation (§6) is deliberately absent: it governs ASSIGNMENTS, and the
first cut of Access Management is role/scope definitions only, with every
write gated to Hub Admin. The closure helpers below are what it will be
built from when the assignments surface lands.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db_v2.models.role import RoleEdgeV2, RoleV2
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2

# Distinct constants so the two graphs never serialize against each other.
# Arbitrary, but must stay stable — the lock is only meaningful if every
# writer picks the same number.
ROLE_GRAPH_LOCK_KEY = 0x5242_4143_0001  # unused today; reserved, see below
SCOPE_GRAPH_LOCK_KEY = 0x5242_4143_0002


class RbacGraphError(Exception):
    """A graph rule was violated. ``code`` is the machine-readable tag the
    router turns into a 409 body — these errors ARE the product of the admin
    API, since hand-building a role graph is mostly a conversation with the
    validator."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------
# Advisory locking
# ---------------------------------------------------------


def lock_scope_graph(db: Session) -> None:
    """Serialize scope-edge writers for the rest of the transaction (§5.6).

    Must be called BEFORE ``validate_scope_edge``, in the same transaction as
    the insert — validating without it is a time-of-check/time-of-use hole:
    A->B and B->A can pass independently and commit a two-node cycle.

    No-op on any non-PostgreSQL dialect. ``pg_advisory_xact_lock`` does not
    exist on SQLite, which is what tests/ runs against; the tests are
    single-threaded so there is nothing to serialize there anyway.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCOPE_GRAPH_LOCK_KEY})


# ---------------------------------------------------------
# Closures — both include the starting node
# ---------------------------------------------------------


def _walk(db: Session, start_id: int, edges: list[tuple[int, int]]) -> set[int]:
    """Breadth-first reachability over ``(from, to)`` pairs. Loads the whole
    edge table once rather than querying per level: both graphs are tens of
    rows, so one round trip beats N, and the walk is also what runs inside
    the advisory lock where round trips are most expensive."""
    adjacency: dict[int, list[int]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)

    seen = {start_id}
    frontier = [start_id]
    while frontier:
        current = frontier.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def _role_edge_pairs(db: Session) -> list[tuple[int, int]]:
    return [
        (row[0], row[1])
        for row in db.query(RoleEdgeV2.parent_role_id, RoleEdgeV2.child_role_id).all()
    ]


def _scope_edge_pairs(db: Session) -> list[tuple[int, int]]:
    return [
        (row[0], row[1])
        for row in db.query(ScopeEdgeV2.parent_scope_id, ScopeEdgeV2.child_scope_id).all()
    ]


def role_descendants(db: Session, role_id: int) -> set[int]:
    """Every role whose access ``role_id`` inherits, INCLUDING itself.

    Self-inclusion is the convention both closures share (§2.4): a grant on
    (Program Lead, A) is satisfied by holding exactly that. Callers wanting
    "strictly beneath me" — the delegation rule (§6.1) is the one that does —
    must subtract the starting id themselves.
    """
    return _walk(db, role_id, _role_edge_pairs(db))


def role_ancestors(db: Session, role_id: int) -> set[int]:
    """Every role that inherits ``role_id``'s access, including itself."""
    return _walk(db, role_id, [(c, p) for p, c in _role_edge_pairs(db)])


def scope_descendants(db: Session, scope_id: int) -> set[int]:
    """Every scope covered by holding ``scope_id``, including itself.

    A universal scope short-circuits to EVERY scope row rather than reading
    ``parent_child_scopes`` (§3.4). That is the whole point of the flag: a scope
    created tomorrow is inside "All Scopes" without anyone remembering to
    add an edge. §5.5 keeps a universal scope out of ``parent_child_scopes``
    entirely, so there is no path where both branches could disagree.
    """
    scope = db.query(ScopeV2).filter(ScopeV2.id == scope_id).first()
    if scope is None:
        return set()
    if scope.is_universal:
        return {row[0] for row in db.query(ScopeV2.id).all()}
    return _walk(db, scope_id, _scope_edge_pairs(db))


def scope_ancestors(db: Session, scope_id: int) -> set[int]:
    """Every scope containing ``scope_id``, including itself.

    Note the asymmetry with ``scope_descendants``: a universal scope contains
    everything, but nothing contains it, so it is NOT added here. Walking up
    from an ordinary scope will never reach it — it holds no edges — which is
    exactly right. It also means this must never be used to answer "is X
    covered by Y"; that is a descendants question.
    """
    return _walk(db, scope_id, [(c, p) for p, c in _scope_edge_pairs(db)])


# ---------------------------------------------------------
# Lookups
# ---------------------------------------------------------


def _get_role(db: Session, role_id: int) -> RoleV2:
    role = db.query(RoleV2).filter(RoleV2.id == role_id).first()
    if role is None:
        raise RbacGraphError("role_not_found", f"Role {role_id} does not exist", role_id=role_id)
    return role


def _get_scope(db: Session, scope_id: int) -> ScopeV2:
    scope = db.query(ScopeV2).filter(ScopeV2.id == scope_id).first()
    if scope is None:
        raise RbacGraphError(
            "scope_not_found", f"Scope {scope_id} does not exist", scope_id=scope_id
        )
    return scope


# ---------------------------------------------------------
# Role edges (§5.1, §5.2)
# ---------------------------------------------------------


def validate_role_edge(db: Session, parent_role_id: int, child_role_id: int) -> None:
    """Rules for adding ``parent -> child`` to the role DAG.

    There is deliberately NO cycle check here. Rank ordering makes one
    unreachable: if every existing edge satisfies parent.rank < child.rank
    and this one does too, every path strictly increases rank. That
    invariant is only ever at risk from a rank EDIT, which is why
    ``set_role_rank`` re-validates instead (§5.3), and from raw SQL, which
    bypasses this module entirely.
    """
    if parent_role_id == child_role_id:
        raise RbacGraphError(
            "self_edge", "A role cannot be its own parent", role_id=parent_role_id
        )

    parent = _get_role(db, parent_role_id)
    child = _get_role(db, child_role_id)

    if parent.rank >= child.rank:
        raise RbacGraphError(
            "rank_violation",
            (
                f"{parent.name!r} (rank {parent.rank}) cannot be a parent of "
                f"{child.name!r} (rank {child.rank}): a parent must rank "
                f"strictly above its child. Equal ranks are peers and can "
                f"never be related in either direction."
            ),
            parent_role_id=parent_role_id,
            child_role_id=child_role_id,
            parent_rank=parent.rank,
            child_rank=child.rank,
        )

    exists = (
        db.query(RoleEdgeV2.id)
        .filter(
            RoleEdgeV2.parent_role_id == parent_role_id,
            RoleEdgeV2.child_role_id == child_role_id,
        )
        .first()
    )
    if exists is not None:
        raise RbacGraphError(
            "duplicate_edge",
            f"{parent.name!r} is already a parent of {child.name!r}",
            parent_role_id=parent_role_id,
            child_role_id=child_role_id,
        )


def create_role_edge(db: Session, parent_role_id: int, child_role_id: int) -> RoleEdgeV2:
    """Validate and insert. Does not commit — the caller owns the transaction."""
    validate_role_edge(db, parent_role_id, child_role_id)
    edge = RoleEdgeV2(
        parent_role_id=parent_role_id,
        child_role_id=child_role_id,
        created_at=_utc_now(),
    )
    db.add(edge)
    db.flush()
    return edge


def delete_role_edge(db: Session, parent_role_id: int, child_role_id: int) -> None:
    """Removing an edge can never violate any rule — it only ever shrinks
    reachability — so there is nothing to validate."""
    edge = (
        db.query(RoleEdgeV2)
        .filter(
            RoleEdgeV2.parent_role_id == parent_role_id,
            RoleEdgeV2.child_role_id == child_role_id,
        )
        .first()
    )
    if edge is None:
        raise RbacGraphError(
            "edge_not_found",
            "That role edge does not exist",
            parent_role_id=parent_role_id,
            child_role_id=child_role_id,
        )
    db.delete(edge)
    db.flush()


# ---------------------------------------------------------
# Rank changes (§5.3)
# ---------------------------------------------------------


def rank_change_conflicts(db: Session, role_id: int, new_rank: int) -> list[dict[str, object]]:
    """Every existing edge that ``new_rank`` would put in violation.

    This is the ONE thing that can break the role graph's free acyclicity,
    so it is exposed separately from ``set_role_rank``: the admin API reports
    the conflicts rather than just refusing, because "you cannot do that" is
    useless without "these four edges are why".
    """
    conflicts: list[dict[str, object]] = []

    as_parent = (
        db.query(RoleEdgeV2.child_role_id).filter(RoleEdgeV2.parent_role_id == role_id).all()
    )
    for (child_id,) in as_parent:
        child = _get_role(db, child_id)
        if new_rank >= child.rank:
            conflicts.append(
                {
                    "edge": "parent_of",
                    "other_role_id": child.id,
                    "other_role_name": child.name,
                    "other_rank": child.rank,
                }
            )

    as_child = (
        db.query(RoleEdgeV2.parent_role_id).filter(RoleEdgeV2.child_role_id == role_id).all()
    )
    for (parent_id,) in as_child:
        parent = _get_role(db, parent_id)
        if parent.rank >= new_rank:
            conflicts.append(
                {
                    "edge": "child_of",
                    "other_role_id": parent.id,
                    "other_role_name": parent.name,
                    "other_rank": parent.rank,
                }
            )

    return conflicts


def set_role_rank(db: Session, role_id: int, new_rank: int) -> RoleV2:
    """Change a role's rank, refusing if any incident edge would break."""
    role = _get_role(db, role_id)
    if role.rank == new_rank:
        return role

    conflicts = rank_change_conflicts(db, role_id, new_rank)
    if conflicts:
        raise RbacGraphError(
            "rank_change_conflict",
            (
                f"Rank {new_rank} would invalidate {len(conflicts)} existing "
                f"edge(s) on {role.name!r}. Remove or re-point them first."
            ),
            role_id=role_id,
            new_rank=new_rank,
            conflicts=conflicts,
        )

    role.rank = new_rank
    role.updated_at = _utc_now()
    db.flush()
    return role


# ---------------------------------------------------------
# Scope edges (§5.1, §5.4, §5.5, §5.6)
# ---------------------------------------------------------


def validate_scope_edge(db: Session, parent_scope_id: int, child_scope_id: int) -> None:
    """Rules for adding ``parent -> child`` to the scope DAG.

    Callers must hold ``lock_scope_graph`` for this to mean anything under
    concurrency (§5.6).
    """
    if parent_scope_id == child_scope_id:
        raise RbacGraphError(
            "self_edge", "A scope cannot contain itself", scope_id=parent_scope_id
        )

    parent = _get_scope(db, parent_scope_id)
    child = _get_scope(db, child_scope_id)

    # §5.5 — the universal scope is implicitly the root. An edge INTO it is
    # a contradiction (nothing contains everything), and an edge OUT of it is
    # redundant with the is_universal short-circuit in scope_descendants,
    # which would then be the only one of the two that anybody reads.
    for scope in (parent, child):
        if scope.is_universal:
            raise RbacGraphError(
                "universal_scope_edge",
                (
                    f"{scope.name!r} is the universal scope: it already contains "
                    f"every scope implicitly and cannot take explicit edges."
                ),
                scope_id=scope.id,
            )

    if parent_scope_id in scope_descendants(db, child_scope_id):
        raise RbacGraphError(
            "cycle",
            (
                f"{parent.name!r} cannot contain {child.name!r}: "
                f"{child.name!r} already contains {parent.name!r}, directly or "
                f"through another scope."
            ),
            parent_scope_id=parent_scope_id,
            child_scope_id=child_scope_id,
        )

    exists = (
        db.query(ScopeEdgeV2.id)
        .filter(
            ScopeEdgeV2.parent_scope_id == parent_scope_id,
            ScopeEdgeV2.child_scope_id == child_scope_id,
        )
        .first()
    )
    if exists is not None:
        raise RbacGraphError(
            "duplicate_edge",
            f"{parent.name!r} already contains {child.name!r}",
            parent_scope_id=parent_scope_id,
            child_scope_id=child_scope_id,
        )


def create_scope_edge(db: Session, parent_scope_id: int, child_scope_id: int) -> ScopeEdgeV2:
    """Lock, validate, insert. The lock is taken here rather than left to the
    caller so there is no way to reach ``validate_scope_edge`` unprotected on
    the write path."""
    lock_scope_graph(db)
    validate_scope_edge(db, parent_scope_id, child_scope_id)
    edge = ScopeEdgeV2(
        parent_scope_id=parent_scope_id,
        child_scope_id=child_scope_id,
        created_at=_utc_now(),
    )
    db.add(edge)
    db.flush()
    return edge


def delete_scope_edge(db: Session, parent_scope_id: int, child_scope_id: int) -> None:
    """Removing an edge only shrinks reachability, so nothing to validate.

    Once the assignments surface exists this grows a caller-visible warning:
    narrowing a composite scope can strand assignments that were made when it
    was wider. It cannot today — v1 has no assignments.
    """
    edge = (
        db.query(ScopeEdgeV2)
        .filter(
            ScopeEdgeV2.parent_scope_id == parent_scope_id,
            ScopeEdgeV2.child_scope_id == child_scope_id,
        )
        .first()
    )
    if edge is None:
        raise RbacGraphError(
            "edge_not_found",
            "That scope edge does not exist",
            parent_scope_id=parent_scope_id,
            child_scope_id=child_scope_id,
        )
    db.delete(edge)
    db.flush()
