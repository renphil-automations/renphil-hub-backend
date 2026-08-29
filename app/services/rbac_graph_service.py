"""Role/scope graph validation
(plan_access_control_schema_2026-08-22.md §5).

Owns every rule the two DAGs obey that a per-row CHECK constraint cannot
express. The models carry the per-row half (no self-edge, no duplicate edge,
at most one universal scope); everything spanning more than one row lives
here, and nothing may write ``parent_child_roles`` / ``parent_child_scopes``
without going through this module.

THE TWO GRAPHS ARE NOW SYMMETRIC. Both enforce acyclicity the same way: a
real descendant walk (§5.4) under a transaction-scoped advisory lock (§5.6),
because two concurrent inserts can each be individually acyclic yet jointly
form a cycle.

THAT IS A CHANGE, and the reason the rank code below is commented out rather
than deleted. The role graph used to get acyclicity for free from
``parent.rank < child.rank`` (§5.2): every path strictly increased rank, so
none could return to its origin, so the role graph needed neither a cycle
walk nor a lock — §5.6 says so explicitly. The rank rule has been DISABLED by
requirement (``rank`` is now ``depth``, nullable, and constrains nothing), so
that immunity is gone and the role graph needs exactly what the scope graph
always needed. Every commented-out block below is kept verbatim so the rule
can be switched back on; if it is, the role-side cycle walk and lock become
redundant again but stay correct, so they can be left in place.

Delegation (§6) is deliberately absent: it governs ASSIGNMENTS, and the
first cut of Access Management is role/scope definitions only, with every
write gated to Hub Admin. The closure helpers below are what it will be
built from when the assignments surface lands.

WHY ``effective_pairs`` LIVES HERE, given the paragraph above says this
module does not own assignments. It reads ``role_assignments``, but it is
not a rule ABOUT assignments — it is the closures' own expansion, and the
per-row pairing it protects is a property of how the two closures compose,
not of who may write a row. Putting it here is what lets the delegation
rule, and the read-time visibility fold that comes next
(plan_access_control_algorithm_2026-08-27.md §8.1), share one
``RbacClosures`` snapshot instead of each rebuilding the adjacency. The
module still writes nothing outside ``parent_child_roles`` /
``parent_child_scopes``; the assignments table is read-only from here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db_v2.models.role import RoleEdgeV2, RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2

# Distinct constants so the two graphs never serialize against each other.
# Arbitrary, but must stay stable — the lock is only meaningful if every
# writer picks the same number. No transaction ever takes both, so there is
# no lock-ordering deadlock to reason about.
ROLE_GRAPH_LOCK_KEY = 0x5242_4143_0001
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


def _lock_graph(db: Session, key: int) -> None:
    """Take a transaction-scoped advisory lock (§5.6).

    No-op on any non-PostgreSQL dialect. ``pg_advisory_xact_lock`` does not
    exist on SQLite, which is what tests/ runs against; the tests are
    single-threaded so there is nothing to serialize there anyway.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def lock_role_graph(db: Session) -> None:
    """Serialize role-edge writers for the rest of the transaction (§5.6).

    Required now that the rank rule is disabled. §5.6 originally exempted
    this graph on the grounds that rank ordering is a per-edge property, so
    no combination of individually-valid inserts could produce a cycle — true
    while ranks were enforced, and false the moment they stopped being. With
    the rule off, A->B and B->A can pass validation independently and commit
    a two-node cycle, exactly the scope-graph hazard.

    Must be called BEFORE ``validate_role_edge``, in the same transaction as
    the insert.
    """
    _lock_graph(db, ROLE_GRAPH_LOCK_KEY)


def lock_scope_graph(db: Session) -> None:
    """Serialize scope-edge writers for the rest of the transaction (§5.6).

    Must be called BEFORE ``validate_scope_edge``, in the same transaction as
    the insert — validating without it is a time-of-check/time-of-use hole:
    A->B and B->A can pass independently and commit a two-node cycle.
    """
    _lock_graph(db, SCOPE_GRAPH_LOCK_KEY)


# ---------------------------------------------------------
# Closures — both include the starting node
# ---------------------------------------------------------


def _walk_edges(start_id: int, edges: list[tuple[int, int]]) -> set[int]:
    """Breadth-first reachability over ``(from, to)`` pairs. The caller has
    already loaded the whole edge table: both graphs are tens of rows, so one
    round trip beats N, and the walk is also what runs inside the advisory
    lock where round trips are most expensive.

    Takes no Session — it never had a use for one, and dropping it is what
    lets ``RbacClosures`` walk the same edge list repeatedly without touching
    the database again."""
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


class RbacClosures:
    """Both DAGs and both ⊥ rows, loaded ONCE, with memoized walks
    (algorithm plan §8.2).

    Every closure helper below is a one-shot wrapper around an instance of
    this, so the ordinary call shape is unchanged. The class exists for the
    callers that need MANY closures from one consistent snapshot —
    ``held_closures`` / ``effective_pairs`` expand one closure per assignment
    row, and ``can_delegate`` walks the granter's rows the same way.

    THE COST THIS EXISTS TO AVOID, quoted because it is the thing that makes
    the read-time visibility path unusable if it is copied: the free
    functions rescan the WHOLE of ``parent_child_roles`` (and
    ``parent_child_scopes``) on every single call. ``can_delegate`` used to
    call both inside a loop over the granter's assignments, and
    ``list_revocable`` calls ``can_delegate`` once per assignment row in the
    org — O(rows × assignments) full table scans. Harmless at today's volumes
    (both graphs are tens of rows), lethal the moment the per-request
    visibility fold starts asking the same questions per node.

    A snapshot is exactly as stale as the transaction that built it. Build
    one per request, never cache one across requests: an edge added in
    between would not be seen, and this is an authorization input.
    """

    def __init__(self, db: Session) -> None:
        self._role_edges = _role_edge_pairs(db)
        self._scope_edges = _scope_edge_pairs(db)
        self._role_edges_up = [(c, p) for p, c in self._role_edges]
        self._scope_edges_up = [(c, p) for p, c in self._scope_edges]

        # Both ⊥ ids are held as SETS rather than as a single id or None.
        # What keeps them to one row each is the partial unique index, and
        # this code should not independently assume what the index already
        # guarantees — unioning a set of any size is the same operation, so
        # the defensive shape costs nothing and cannot go wrong if a second
        # row ever appears through some path nobody anticipated.
        self._public_role_ids: set[int] = {
            row[0] for row in db.query(RoleV2.id).filter(RoleV2.is_public.is_(True)).all()
        }

        # One pass over `scopes` for all three sets rather than three
        # queries: the universal short-circuit needs every id, the ordinary
        # branch needs the ⊥ ids, and both branches need to know whether the
        # starting id exists at all.
        self._all_scope_ids: set[int] = set()
        self._universal_scope_ids: set[int] = set()
        self._public_scope_ids: set[int] = set()
        for scope_id, is_universal, is_public in db.query(
            ScopeV2.id, ScopeV2.is_universal, ScopeV2.is_public
        ).all():
            self._all_scope_ids.add(scope_id)
            if is_universal:
                self._universal_scope_ids.add(scope_id)
            if is_public:
                self._public_scope_ids.add(scope_id)

        self._role_descendant_cache: dict[int, set[int]] = {}
        self._scope_descendant_cache: dict[int, set[int]] = {}

    # -- descendants ----------------------------------------------

    def role_descendants(self, role_id: int) -> set[int]:
        """See the module-level ``role_descendants``."""
        cached = self._role_descendant_cache.get(role_id)
        if cached is None:
            cached = _walk_edges(role_id, self._role_edges) | self._public_role_ids
            self._role_descendant_cache[role_id] = cached
        # A copy, so a caller mutating the result cannot corrupt the cache
        # for the next one. These sets are tens of ids.
        return set(cached)

    def scope_descendants(self, scope_id: int) -> set[int]:
        """See the module-level ``scope_descendants``."""
        cached = self._scope_descendant_cache.get(scope_id)
        if cached is None:
            if scope_id not in self._all_scope_ids:
                cached = set()
            elif scope_id in self._universal_scope_ids:
                cached = set(self._all_scope_ids)
            else:
                cached = _walk_edges(scope_id, self._scope_edges) | self._public_scope_ids
            self._scope_descendant_cache[scope_id] = cached
        return set(cached)

    # -- ancestors: no cache, no ⊥ (see the free functions) --------

    def role_ancestors(self, role_id: int) -> set[int]:
        return _walk_edges(role_id, self._role_edges_up)

    def scope_ancestors(self, scope_id: int) -> set[int]:
        return _walk_edges(scope_id, self._scope_edges_up)


def role_descendants(db: Session, role_id: int) -> set[int]:
    """Every role whose access ``role_id`` inherits, INCLUDING itself.

    Self-inclusion is the convention both closures share (§2.4): a grant on
    (Program Lead, A) is satisfied by holding exactly that. Callers wanting
    "strictly beneath me" — the delegation rule (§6.1) is the one that does —
    must subtract the starting id themselves.

    The PUBLIC role — "Any Role", the lattice's bottom — is unioned in
    unconditionally (algorithm plan §4.4), because every role implicitly
    inherits it. Unlike the universal scope's short-circuit this is an
    addition, not a replacement: the walk still runs and its result still
    matters.

    Deliberately does NOT validate that ``role_id`` exists, which is the
    behaviour it has always had — ``validate_role_edge`` looks roles up
    separately via ``_get_role`` and wants its own error message. An unknown
    id therefore returns ``{that id} | {public}`` rather than empty. The
    scope side does validate, because it has to read ``is_universal``
    anyway; the asymmetry predates this change.
    """
    return RbacClosures(db).role_descendants(role_id)


def role_ancestors(db: Session, role_id: int) -> set[int]:
    """Every role that inherits ``role_id``'s access, including itself.

    No ⊥ here, and the asymmetry is the same one ``scope_ancestors``
    documents: everything inherits the public role, so nothing meaningful is
    gained by walking UP from an arbitrary role into it, and this direction
    must never be used to answer "is X covered by Y" — that is a descendants
    question. ``role_ancestors(public_role)`` would be every role, but the
    public role is barred from the edge table, so the walk cannot reach it.
    """
    return RbacClosures(db).role_ancestors(role_id)


def scope_descendants(db: Session, scope_id: int) -> set[int]:
    """Every scope covered by holding ``scope_id``, including itself.

    A universal scope short-circuits to EVERY scope row rather than reading
    ``parent_child_scopes`` (§3.4). That is the whole point of the flag: a scope
    created tomorrow is inside "All Scopes" without anyone remembering to
    add an edge. §5.5 keeps a universal scope out of ``parent_child_scopes``
    entirely, so there is no path where both branches could disagree.

    The PUBLIC scope — "Any Scope", the bottom — is unioned into the
    ordinary branch unconditionally (algorithm plan §4.4). The universal
    branch needs no special case: "every scope row" already contains it, and
    the CHECK on ``scopes`` makes sure no single row is both ends at once.
    """
    return RbacClosures(db).scope_descendants(scope_id)


def scope_ancestors(db: Session, scope_id: int) -> set[int]:
    """Every scope containing ``scope_id``, including itself.

    Note the asymmetry with ``scope_descendants``: a universal scope contains
    everything, but nothing contains it, so it is NOT added here. Walking up
    from an ordinary scope will never reach it — it holds no edges — which is
    exactly right. It also means this must never be used to answer "is X
    covered by Y"; that is a descendants question.

    The public scope gets the mirror treatment for the mirror reason: every
    scope contains it, so it is not an ANCESTOR of anything and is not
    unioned in here. It holds no edges either, so the walk cannot wander
    into it.
    """
    return RbacClosures(db).scope_ancestors(scope_id)


# ---------------------------------------------------------
# Effective access — §4.1, expanded PER ASSIGNMENT ROW
# ---------------------------------------------------------


class HeldClosure(NamedTuple):
    """One ``role_assignments`` row with both of its closures expanded.

    ``role_id`` / ``scope_id`` are the row's own ids, kept alongside the
    expansions because the delegation rule needs them: "strictly beneath the
    role I hold" cannot be answered once the held role has been absorbed
    into a set with everything below it.
    """

    role_id: int
    scope_id: int
    role_ids: set[int]
    scope_ids: set[int]


def held_assignments(db: Session, hub_user_id: int) -> list[tuple[int, int]]:
    """The user's raw ``(role_id, scope_id)`` assignment ROWS.

    One query, one place, so every consumer of "what does this user hold"
    starts from the same rows.
    """
    return [
        (row[0], row[1])
        for row in db.query(RoleAssignmentV2.role_id, RoleAssignmentV2.scope_id)
        .filter(RoleAssignmentV2.user_id == hub_user_id)
        .all()
    ]


def held_closures(
    db: Session, hub_user_id: int, *, closures: RbacClosures | None = None
) -> list[HeldClosure]:
    """Every assignment row the user holds, each with its two closures.

    THIS IS THE PRIMITIVE, and everything about effective access is built on
    it precisely so the pairing stays intact one level down. Returning a
    LIST OF ROWS rather than a flat set is the whole point: the moment the
    rows are merged, which role went with which scope is gone, and no
    consumer can recover it.

    Pass ``closures`` to share one snapshot across several calls; omitted, a
    fresh one is built (one load of each edge table, not one per row).
    """
    graph = closures if closures is not None else RbacClosures(db)
    return [
        HeldClosure(
            role_id=role_id,
            scope_id=scope_id,
            role_ids=graph.role_descendants(role_id),
            scope_ids=graph.scope_descendants(scope_id),
        )
        for role_id, scope_id in held_assignments(db, hub_user_id)
    ]


def effective_pairs(
    db: Session, hub_user_id: int, *, closures: RbacClosures | None = None
) -> set[tuple[int, int]]:
    """§4.1's ``effective(U)`` — every ``(role, scope)`` pair the user's
    assignments reach::

        effective(U) = ⋃ over each of U's assignment ROWS (R, S):
                           role_descendants*(R) × scope_descendants*(S)

    PER ROW, NEVER THE UNION OF ROLES CROSSED WITH THE UNION OF SCOPES. This
    is the pairing bug ``role_assignments`` exists to prevent, and it is the
    easier version to write, which is why it is now restated in five places:
    ``role_assignment.py``'s module docstring, schema plan §6.2,
    ``rbac_delegation_service``'s module docstring, algorithm plan §4.1, and
    here. The cross-product is taken INSIDE the loop, over one row's two
    closures; the union across rows happens after. Alice holding
    (Program Lead, A) and (Program Member, B) must never yield
    (Program Lead, B).

    WHAT THIS IS AND IS NOT USABLE FOR. It answers "does the user hold a
    pair at or above this stored grant?" — §4.2's match direction, and the
    read path in §8.1. It CANNOT answer the delegation question, and
    ``can_delegate`` deliberately does not call it: §6.1 needs the target
    role to be a PROPER descendant of a held role, and properness is a
    per-row fact that flattening has already destroyed. A user holding
    exactly (Program Lead, A) has (Program Lead, A) in this set, yet may not
    grant it. ``can_delegate`` uses ``held_closures`` instead, one level
    down, sharing this function's snapshot and its walks.
    """
    pairs: set[tuple[int, int]] = set()
    for held in held_closures(db, hub_user_id, closures=closures):
        for role_id in held.role_ids:
            for scope_id in held.scope_ids:
                pairs.add((role_id, scope_id))
    return pairs


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

    Callers must hold ``lock_role_graph`` for the cycle check to mean
    anything under concurrency (§5.6) — ``create_role_edge`` takes it.

    The rank ordering rule that used to live here is disabled (see the module
    docstring) and kept commented out below. With it off, the cycle walk is
    the only thing keeping this graph acyclic.
    """
    if parent_role_id == child_role_id:
        raise RbacGraphError(
            "self_edge", "A role cannot be its own parent", role_id=parent_role_id
        )

    parent = _get_role(db, parent_role_id)
    child = _get_role(db, child_role_id)

    # ── DISABLED: rank ordering (§5.2) ────────────────────────────────
    # Kept, not deleted — the requirement was to switch the rule off, and it
    # may be wanted back. Restoring it is uncommenting this block; the cycle
    # check below then becomes redundant but stays correct, so it can stay.
    # Note `rank` is now the nullable `depth` column, so a restored rule has
    # to decide what a NULL means before this compares cleanly.
    #
    # if parent.rank >= child.rank:
    #     raise RbacGraphError(
    #         "rank_violation",
    #         (
    #             f"{parent.name!r} (rank {parent.rank}) cannot be a parent of "
    #             f"{child.name!r} (rank {child.rank}): a parent must rank "
    #             f"strictly above its child. Equal ranks are peers and can "
    #             f"never be related in either direction."
    #         ),
    #         parent_role_id=parent_role_id,
    #         child_role_id=child_role_id,
    #         parent_rank=parent.rank,
    #         child_rank=child.rank,
    #     )

    # §4.4 — the public role is implicitly beneath every role. An edge INTO
    # it is redundant with the union in `role_descendants`, and an edge OUT
    # of it says some role is beneath the bottom, which is a contradiction.
    #
    # THIS MUST PRECEDE THE CYCLE WALK, exactly as the scope graph's
    # equivalent does (see validate_scope_edge, where the reasoning is
    # spelled out in full). The public role is in every descendant set, so
    # `parent_role_id in role_descendants(child_role_id)` is trivially true
    # whenever the parent IS the public role — the cycle check would get
    # there first and blame a cycle that does not exist. Unlike the scope
    # side there was no existing guard here to extend: roles have no
    # is_universal, so this loop is new, and its POSITION is the part that
    # matters.
    for role in (parent, child):
        if role.is_public:
            raise RbacGraphError(
                "public_role_edge",
                (
                    f"{role.name!r} is the public role: every role already "
                    f"inherits it implicitly and it cannot take explicit edges."
                ),
                role_id=role.id,
            )

    # Adding parent -> child closes a loop exactly when `child` can already
    # reach `parent`. Same test, same direction, same error code as
    # validate_scope_edge — the two graphs are symmetric now.
    if parent_role_id in role_descendants(db, child_role_id):
        raise RbacGraphError(
            "cycle",
            (
                f"{parent.name!r} cannot inherit from {child.name!r}: "
                f"{child.name!r} already inherits from {parent.name!r}, "
                f"directly or through another role."
            ),
            parent_role_id=parent_role_id,
            child_role_id=child_role_id,
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
    """Lock, validate, insert. Does not commit — the caller owns the
    transaction. The lock is taken here rather than left to the caller so
    there is no way to reach ``validate_role_edge`` unprotected on the write
    path — same shape as ``create_scope_edge``."""
    lock_role_graph(db)
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
# DISABLED: rank changes (§5.3)
# ---------------------------------------------------------
#
# Both functions below are switched off, not deleted, along with the rank
# ordering rule in validate_role_edge. They existed only to protect that
# rule's invariant: a rank edit was the one thing that could put an existing
# edge in violation, so changing a rank had to re-validate every incident
# edge. With the rule off there is no invariant to protect — `depth` is inert
# metadata — so `update_role` now assigns it directly.
#
# The 409 `rank_change_conflict` error code disappears with them. The
# frontend's dedicated renderer for its `conflicts[]` array was removed at
# the same time; restoring these means restoring that too.
#
# If this comes back: `rank` is now the nullable `depth` column, so both
# functions need a stated rule for NULL (is an unranked role above or below
# everything, or simply un-edgeable?) before the `>=` comparisons are sound.
# On NULL, `new_rank >= child.rank` raises TypeError rather than returning
# False — it will not fail quietly.
#
# def rank_change_conflicts(db: Session, role_id: int, new_rank: int) -> list[dict[str, object]]:
#     """Every existing edge that ``new_rank`` would put in violation.
#
#     This is the ONE thing that can break the role graph's free acyclicity,
#     so it is exposed separately from ``set_role_rank``: the admin API reports
#     the conflicts rather than just refusing, because "you cannot do that" is
#     useless without "these four edges are why".
#     """
#     conflicts: list[dict[str, object]] = []
#
#     as_parent = (
#         db.query(RoleEdgeV2.child_role_id).filter(RoleEdgeV2.parent_role_id == role_id).all()
#     )
#     for (child_id,) in as_parent:
#         child = _get_role(db, child_id)
#         if new_rank >= child.rank:
#             conflicts.append(
#                 {
#                     "edge": "parent_of",
#                     "other_role_id": child.id,
#                     "other_role_name": child.name,
#                     "other_rank": child.rank,
#                 }
#             )
#
#     as_child = (
#         db.query(RoleEdgeV2.parent_role_id).filter(RoleEdgeV2.child_role_id == role_id).all()
#     )
#     for (parent_id,) in as_child:
#         parent = _get_role(db, parent_id)
#         if parent.rank >= new_rank:
#             conflicts.append(
#                 {
#                     "edge": "child_of",
#                     "other_role_id": parent.id,
#                     "other_role_name": parent.name,
#                     "other_rank": parent.rank,
#                 }
#             )
#
#     return conflicts
#
#
# def set_role_rank(db: Session, role_id: int, new_rank: int) -> RoleV2:
#     """Change a role's rank, refusing if any incident edge would break."""
#     role = _get_role(db, role_id)
#     if role.rank == new_rank:
#         return role
#
#     conflicts = rank_change_conflicts(db, role_id, new_rank)
#     if conflicts:
#         raise RbacGraphError(
#             "rank_change_conflict",
#             (
#                 f"Rank {new_rank} would invalidate {len(conflicts)} existing "
#                 f"edge(s) on {role.name!r}. Remove or re-point them first."
#             ),
#             role_id=role_id,
#             new_rank=new_rank,
#             conflicts=conflicts,
#         )
#
#     role.rank = new_rank
#     role.updated_at = _utc_now()
#     db.flush()
#     return role


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
    #
    # BOTH checks must stay AHEAD OF THE CYCLE WALK below, and for the
    # public scope that ordering is load-bearing rather than tidy: it sits
    # in EVERY descendant set by construction, so
    # `parent_scope_id in scope_descendants(child_scope_id)` is trivially
    # true for any edge whose parent is the public scope. Reached in the
    # other order, a perfectly ordinary mistake would come back as "Any
    # Scope already contains X" — a cycle error naming a cycle that does not
    # exist. The is_universal guard already occupied this position, which is
    # why adding to the same loop inherits the right answer for free.
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
        if scope.is_public:
            raise RbacGraphError(
                "public_scope_edge",
                (
                    f"{scope.name!r} is the public scope: every scope already "
                    f"contains it implicitly and it cannot take explicit edges."
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
