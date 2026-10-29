"""View As — let a caller see the hub the way a holder of one or more
WEAKER ``(role, scope)`` pairs would. Pairs arrive as repeatable
``view_as=role_key:scope_key`` query values.

A caller may adopt a ``(role, scope)`` pair only when:

  1. ``scope`` is one they hold in ``role_assignments``, a direct or
     indirect child of one (``parent_child_scopes``), or the public scope.
     A universal scope covers every scope, exactly as everywhere else in the
     engine (``RbacClosures.scope_descendants``).
  2. ``role`` is one they hold on a scope that covers ``scope`` (the row's
     own scope or any ancestor of ``scope``), a direct or indirect child of
     such a role (``parent_child_roles``), or the public role.

That is ``effective_pairs`` read per assignment ROW, so the pairing rule
holds: holding (Program Lead, A) and (Program Member, B) never allows
viewing as (Program Lead, B). Every requested pair is checked on its own,
and several adopted pairs combine exactly like several real assignment rows.

Read-only: nothing here writes, and the caller's real access is never
widened — every adopted pair is within what they already reach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.user import UserV2
from app.services.access_visibility_service import ViewerAccess, build_node_tree, fold
from app.services.rbac_graph_service import (
    HeldClosure,
    RbacClosures,
    effective_pairs,
    held_assignments,
    hub_admin_target_ids,
)
from app.services.resource_grant_service import grants_matching_pairs

logger = logging.getLogger(__name__)


class ViewAsError(Exception):
    """A View As request was refused. ``status_code`` / ``code`` are what the
    router turns into the HTTP error body."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ViewAsTarget:
    role_id: int
    role_key: str
    role_name: str
    scope_id: int
    scope_key: str
    scope_name: str


# ---------------------------------------------------------
# Identity
# ---------------------------------------------------------


def resolve_user_id(db: Session, email: str) -> int | None:
    """``users.id`` for ``email``: ``work_email`` first, ``alias_email`` only
    when no row has it as a work email. Case-insensitive; soft-deleted rows
    are ignored."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    for column in (UserV2.work_email, UserV2.alias_email):
        row = (
            db.query(UserV2.id)
            .filter(func.lower(column) == normalized, UserV2.not_deleted())
            .first()
        )
        if row is not None:
            return row[0]
    return None


# ---------------------------------------------------------
# What the caller may adopt
# ---------------------------------------------------------


def _public_role_ids(db: Session) -> set[int]:
    return {row[0] for row in db.query(RoleV2.id).filter(RoleV2.is_public.is_(True)).all()}


def _public_scope_ids(db: Session) -> set[int]:
    return {row[0] for row in db.query(ScopeV2.id).filter(ScopeV2.is_public.is_(True)).all()}


def _sorted_options(rows) -> list[dict]:
    """``(id, key, name)`` rows → ``[{"id", "key", "name"}]`` sorted by name."""
    return [
        {"id": row_id, "key": key, "name": name}
        for row_id, key, name in sorted(rows, key=lambda r: (r[2].lower(), r[0]))
    ]


def viewable_scope_ids(db: Session, user_id: int, *, closures: RbacClosures | None = None) -> set[int]:
    """Every scope the user holds, every direct/indirect child of those, and
    the public scope(s)."""
    graph = closures if closures is not None else RbacClosures(db)
    scope_ids = _public_scope_ids(db)
    for _role_id, scope_id in held_assignments(db, user_id):
        scope_ids |= graph.scope_descendants(scope_id)
    return scope_ids


def viewable_role_ids(
    db: Session, user_id: int, scope_id: int, *, closures: RbacClosures | None = None
) -> set[int]:
    """Every role the user may adopt on ``scope_id``: the roles they hold on
    a scope covering ``scope_id``, every direct/indirect child of those, and
    the public role(s). Pairing is preserved — only rows whose OWN scope
    covers ``scope_id`` contribute their role."""
    graph = closures if closures is not None else RbacClosures(db)
    role_ids = _public_role_ids(db)
    for held_role_id, held_scope_id in held_assignments(db, user_id):
        if scope_id in graph.scope_descendants(held_scope_id):
            role_ids |= graph.role_descendants(held_role_id)
    return role_ids


def list_viewable_scopes(db: Session, user_id: int) -> list[dict]:
    """``[{"id", "key", "name", "is_universal"}]`` for every scope
    ``viewable_scope_ids`` allows, unique and sorted by name."""
    scope_ids = viewable_scope_ids(db, user_id)
    if not scope_ids:
        return []
    rows = (
        db.query(ScopeV2.id, ScopeV2.key, ScopeV2.name, ScopeV2.is_universal)
        .filter(ScopeV2.id.in_(scope_ids))
        .all()
    )
    return [
        {"id": scope_id, "key": key, "name": name, "is_universal": bool(is_universal)}
        for scope_id, key, name, is_universal in sorted(rows, key=lambda r: (r[2].lower(), r[0]))
    ]


def list_viewable_roles(db: Session, user_id: int, scope_id: int) -> list[dict]:
    """``[{"id", "key", "name"}]`` for every role the user may adopt on
    ``scope_id``, unique and sorted by name.

    Raises ``ViewAsError`` 404 for an unknown scope and 403 for a scope the
    user may not adopt at all — offering roles there would only lead to a
    View As request that is refused anyway."""
    if db.query(ScopeV2.id).filter(ScopeV2.id == scope_id).first() is None:
        raise ViewAsError(404, "view_as_scope_not_found", f"Scope {scope_id} not found.")

    closures = RbacClosures(db)
    if scope_id not in viewable_scope_ids(db, user_id, closures=closures):
        raise ViewAsError(403, "view_as_scope_not_allowed", "You are not allowed to view the hub as this scope.")

    role_ids = viewable_role_ids(db, user_id, scope_id, closures=closures)
    if not role_ids:
        return []
    return _sorted_options(db.query(RoleV2.id, RoleV2.key, RoleV2.name).filter(RoleV2.id.in_(role_ids)).all())


# ---------------------------------------------------------
# Verifying a View As request
# ---------------------------------------------------------


PAIR_SEPARATOR = ":"
MAX_VIEW_AS_PAIRS = 20


class ViewAsPairsError(Exception):
    """The View As request was refused. ``failures`` lists every blocking
    item (not just the first), each as
    ``{"source", "pair", "role_key", "scope_key", "code", "message"}`` —
    ``source`` is ``"view_as"`` for a given pair and ``"view_as_user"`` for
    the selected user (unknown, or holding a row the caller can't adopt)."""

    def __init__(
        self, status_code: int, code: str, message: str, failures: list[dict], *, user_email: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.failures = failures
        self.user_email = user_email


def normalize_pair_values(values: list[str] | None) -> list[str]:
    """Trimmed, non-blank ``view_as`` values, duplicates removed (first
    occurrence kept). An empty result means "View As not requested"."""
    seen: dict[str, None] = {}
    for value in values or ():
        value = (value or "").strip()
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _split_pair(value: str) -> tuple[str, str] | None:
    """``"role_key:scope_key"`` → ``(role_key, scope_key)``; ``None`` unless
    there is exactly one separator with a non-blank key on each side. Keys
    match ``schemas.rbac.KEY_PATTERN``, which never contains the separator."""
    parts = value.split(PAIR_SEPARATOR)
    if len(parts) != 2:
        return None
    role_key, scope_key = (part.strip() for part in parts)
    if not role_key or not scope_key:
        return None
    return role_key, scope_key


def verify_view_as(
    db: Session,
    user_id: int,
    *,
    role_key: str,
    scope_key: str,
    closures: RbacClosures | None = None,
) -> ViewAsTarget:
    """Resolve ONE pair of KEYS (``roles.key`` / ``scopes.key``, exact match)
    and check the caller may adopt it — scope first, then role on that
    scope. Raises ``ViewAsError`` on any failure."""
    graph = closures if closures is not None else RbacClosures(db)

    scope = db.query(ScopeV2.id, ScopeV2.key, ScopeV2.name).filter(ScopeV2.key == scope_key).first()
    if scope is None:
        raise ViewAsError(400, "view_as_scope_not_found", f"Scope '{scope_key}' does not exist.")
    if scope[0] not in viewable_scope_ids(db, user_id, closures=graph):
        raise ViewAsError(
            403, "view_as_scope_not_allowed", f"You are not allowed to view the hub as scope '{scope_key}'."
        )

    role = db.query(RoleV2.id, RoleV2.key, RoleV2.name).filter(RoleV2.key == role_key).first()
    if role is None:
        raise ViewAsError(400, "view_as_role_not_found", f"Role '{role_key}' does not exist.")
    if role[0] not in viewable_role_ids(db, user_id, scope[0], closures=graph):
        raise ViewAsError(
            403,
            "view_as_role_not_allowed",
            f"You are not allowed to view the hub as role '{role_key}' in scope '{scope_key}'.",
        )

    return ViewAsTarget(
        role_id=role[0], role_key=role[1], role_name=role[2],
        scope_id=scope[0], scope_key=scope[1], scope_name=scope[2],
    )


SOURCE_PAIR = "view_as"
SOURCE_USER = "view_as_user"


@dataclass(frozen=True)
class ViewAsUser:
    """The user picked through ``view_as_user``. ``email`` is the one given;
    ``emails`` is every address the person is known by (work and alias),
    normalized — what person-keyed rows such as notifications are matched
    against."""

    id: int
    email: str
    name: str | None
    emails: tuple[str, ...] = ()


@dataclass(frozen=True)
class ViewAsSelection:
    """A verified View As request. ``targets`` is the union of the given
    ``view_as`` pairs and the selected user's assignment rows, one per
    ``(role, scope)``. ``user`` is the selected user, when one was given —
    their DIRECT grants count too (``resolve_view_as_access``); with pairs
    only, the caller's own direct grants count instead."""

    targets: list[ViewAsTarget]
    user: ViewAsUser | None = None

    @property
    def user_email(self) -> str | None:
        return self.user.email if self.user is not None else None


class _Adoptable:
    """"May ``caller_id`` adopt ``(role_id, scope_id)``?" — the same rule as
    ``verify_view_as``, by id, with the caller's viewable scopes loaded once
    and their viewable roles cached per scope. Used to vet a selected user's
    rows and to list the users a caller may select."""

    def __init__(self, db: Session, caller_id: int, closures: RbacClosures) -> None:
        self._db = db
        self._caller_id = caller_id
        self._closures = closures
        self.scope_ids = viewable_scope_ids(db, caller_id, closures=closures)
        self._roles_by_scope: dict[int, set[int]] = {}

    def role_ids(self, scope_id: int) -> set[int]:
        if scope_id not in self._roles_by_scope:
            self._roles_by_scope[scope_id] = viewable_role_ids(
                self._db, self._caller_id, scope_id, closures=self._closures
            )
        return self._roles_by_scope[scope_id]

    def blocking_code(self, role_id: int, scope_id: int) -> str | None:
        """``None`` when adoptable, else the per-pair error code."""
        if scope_id not in self.scope_ids:
            return "view_as_scope_not_allowed"
        if role_id not in self.role_ids(scope_id):
            return "view_as_role_not_allowed"
        return None


def _find_user(db: Session, email: str) -> UserV2 | None:
    """Active user by ``work_email``, else ``alias_email`` — same order as
    ``resolve_user_id``."""
    for column in (UserV2.work_email, UserV2.alias_email):
        user = db.query(UserV2).filter(func.lower(column) == email, UserV2.not_deleted()).first()
        if user is not None:
            return user
    return None


def _check_pairs(
    db: Session, caller_id: int, values: list[str], closures: RbacClosures
) -> tuple[list[ViewAsTarget], list[dict]]:
    targets: list[ViewAsTarget] = []
    failures: list[dict] = []
    for value in values:
        split = _split_pair(value)
        if split is None:
            failures.append(
                {
                    "source": SOURCE_PAIR,
                    "pair": value,
                    "role_key": None,
                    "scope_key": None,
                    "code": "view_as_malformed_pair",
                    "message": f"'{value}' is not in the form role_key{PAIR_SEPARATOR}scope_key.",
                    "_status": 400,
                }
            )
            continue
        role_key, scope_key = split
        try:
            targets.append(verify_view_as(db, caller_id, role_key=role_key, scope_key=scope_key, closures=closures))
        except ViewAsError as e:
            failures.append(
                {
                    "source": SOURCE_PAIR,
                    "pair": value,
                    "role_key": role_key,
                    "scope_key": scope_key,
                    "code": e.code,
                    "message": e.message,
                    "_status": e.status_code,
                }
            )
    return targets, failures


def _check_user(
    db: Session, caller_id: int, email: str, closures: RbacClosures
) -> tuple[ViewAsUser | None, list[ViewAsTarget], list[dict]]:
    """Resolve the selected user and vet EVERY one of their assignment rows
    against what the caller may adopt. Failures name the selected user, so
    the caller can tell the refusal came from them and not from a pair."""
    user = _find_user(db, email)
    if user is None:
        return None, [], [
            {
                "source": SOURCE_USER,
                "pair": None,
                "role_key": None,
                "scope_key": None,
                "code": "view_as_user_not_found",
                "message": f"No active user has the email '{email}'.",
                "_status": 400,
            }
        ]

    rows = (
        db.query(
            RoleAssignmentV2.role_id, RoleV2.key, RoleV2.name,
            RoleAssignmentV2.scope_id, ScopeV2.key, ScopeV2.name,
        )
        .join(RoleV2, RoleV2.id == RoleAssignmentV2.role_id)
        .join(ScopeV2, ScopeV2.id == RoleAssignmentV2.scope_id)
        .filter(RoleAssignmentV2.user_id == user.id)
        .order_by(RoleAssignmentV2.id)
        .all()
    )
    adoptable = _Adoptable(db, caller_id, closures)
    targets: list[ViewAsTarget] = []
    failures: list[dict] = []
    for role_id, role_key, role_name, scope_id, scope_key, scope_name in rows:
        code = adoptable.blocking_code(role_id, scope_id)
        if code is None:
            targets.append(ViewAsTarget(role_id, role_key, role_name, scope_id, scope_key, scope_name))
            continue
        blocked_part = f"scope '{scope_key}'" if code == "view_as_scope_not_allowed" else (
            f"role '{role_key}' in scope '{scope_key}'"
        )
        failures.append(
            {
                "source": SOURCE_USER,
                "pair": f"{role_key}{PAIR_SEPARATOR}{scope_key}",
                "role_key": role_key,
                "scope_key": scope_key,
                "code": code,
                "message": (
                    f"The selected user '{email}' holds role '{role_key}' in scope '{scope_key}', "
                    f"and you are not allowed to view the hub as {blocked_part}."
                ),
                "_status": 403,
            }
        )
    emails = tuple(sorted({e for e in (normalize_email(user.work_email), normalize_email(user.alias_email)) if e}))
    return ViewAsUser(id=user.id, email=email, name=user.name, emails=emails), targets, failures


# Top-level error code by (who blocked, status): only the selected user,
# only the pairs, or both.
_TOP_LEVEL_CODES = {
    (SOURCE_USER, 400): "view_as_user_not_found",
    (SOURCE_USER, 403): "view_as_user_not_allowed",
    (SOURCE_PAIR, 400): "view_as_invalid_pairs",
    (SOURCE_PAIR, 403): "view_as_pairs_not_allowed",
    (None, 400): "view_as_invalid",
    (None, 403): "view_as_not_allowed",
}


def _raise_failures(failures: list[dict], user_email: str | None) -> None:
    status_code = 400 if any(f["_status"] == 400 for f in failures) else 403
    for failure in failures:
        del failure["_status"]
    sources = {f["source"] for f in failures}
    code = _TOP_LEVEL_CODES[(sources.pop() if len(sources) == 1 else None, status_code)]

    parts = []
    user_failures = [f for f in failures if f["source"] == SOURCE_USER]
    pair_failures = [f for f in failures if f["source"] == SOURCE_PAIR]
    if any(f["code"] == "view_as_user_not_found" for f in user_failures):
        parts.append(f"No active user has the email '{user_email}'.")
    elif user_failures:
        blocked = ", ".join(f"'{f['pair']}'" for f in user_failures)
        parts.append(
            f"The selected user '{user_email}' holds {blocked}, which you are not allowed to view the hub as."
        )
    if pair_failures:
        parts.append(f"View As refused for {', '.join(repr(f['pair']) for f in pair_failures)}.")
    raise ViewAsPairsError(status_code, code, " ".join(parts), failures, user_email=user_email)


def resolve_view_as_selection(
    db: Session,
    caller_id: int,
    values: list[str],
    user_email: str | None,
    *,
    closures: RbacClosures | None = None,
) -> ViewAsSelection:
    """Verify the ``view_as`` pairs (already normalized by
    ``normalize_pair_values``) and the selected user (``user_email``,
    already normalized by ``normalize_email``). Everything must pass;
    otherwise ``ViewAsPairsError`` lists EVERY blocking item, each tagged
    with its ``source``. 400 when any failure is a malformed value, an
    unknown key or an unknown user, else 403."""
    if len(values) > MAX_VIEW_AS_PAIRS:
        raise ViewAsPairsError(
            400,
            "view_as_too_many_pairs",
            f"At most {MAX_VIEW_AS_PAIRS} view_as pairs can be given ({len(values)} were).",
            [],
            user_email=user_email,
        )

    graph = closures if closures is not None else RbacClosures(db)
    user, user_targets, failures = (None, [], [])
    if user_email is not None:
        user, user_targets, failures = _check_user(db, caller_id, user_email, graph)
    pair_targets, pair_failures = _check_pairs(db, caller_id, values, graph)
    failures += pair_failures
    if failures:
        _raise_failures(failures, user_email)

    unique: dict[tuple[int, int], ViewAsTarget] = {}
    for target in user_targets + pair_targets:
        unique.setdefault((target.role_id, target.scope_id), target)
    return ViewAsSelection(targets=list(unique.values()), user=user)


def verify_view_as_pairs(
    db: Session,
    user_id: int,
    values: list[str],
    *,
    closures: RbacClosures | None = None,
) -> list[ViewAsTarget]:
    """``resolve_view_as_selection`` for pairs only."""
    return resolve_view_as_selection(db, user_id, values, None, closures=closures).targets


def normalize_email(value: str | None) -> str | None:
    """Trimmed, lower-cased email; ``None`` when missing or blank."""
    value = (value or "").strip().lower()
    return value or None


def list_viewable_users(db: Session, caller_id: int) -> list[dict]:
    """``[{"id", "name", "email", "assignments": [{"role_key", "scope_key"}]}]``
    for every active user the caller may pick through ``view_as_user``:
    EVERY one of their assignment rows must be adoptable by the caller (a
    single row above the caller's reach excludes them), and a user with no
    assignment at all qualifies (with ``assignments: []``). The caller
    themselves and users with no email are left out. Sorted by name;
    assignments sorted by role key, then scope key."""
    adoptable = _Adoptable(db, caller_id, RbacClosures(db))
    blocked: set[int] = set()
    assignments: dict[int, list[dict]] = {}
    for user_id, role_id, role_key, scope_id, scope_key in (
        db.query(
            RoleAssignmentV2.user_id, RoleAssignmentV2.role_id, RoleV2.key, RoleAssignmentV2.scope_id, ScopeV2.key
        )
        .join(RoleV2, RoleV2.id == RoleAssignmentV2.role_id)
        .join(ScopeV2, ScopeV2.id == RoleAssignmentV2.scope_id)
        .all()
    ):
        if user_id in blocked:
            continue
        if adoptable.blocking_code(role_id, scope_id) is not None:
            blocked.add(user_id)
            assignments.pop(user_id, None)
            continue
        assignments.setdefault(user_id, []).append({"role_key": role_key, "scope_key": scope_key})

    users = (
        db.query(UserV2.id, UserV2.name, UserV2.work_email, UserV2.alias_email)
        .filter(UserV2.not_deleted(), UserV2.id != caller_id)
        .all()
    )
    result = []
    for user_id, name, work_email, alias_email in users:
        email = normalize_email(work_email) or normalize_email(alias_email)
        if user_id in blocked or email is None:
            continue
        rows = sorted(assignments.get(user_id, []), key=lambda a: (a["role_key"], a["scope_key"]))
        result.append({"id": user_id, "name": name, "email": email, "assignments": rows})
    result.sort(key=lambda u: ((u["name"] or "").lower(), u["id"]))
    return result


def pair_is_hub_admin(db: Session, target: ViewAsTarget, *, closures: RbacClosures | None = None) -> bool:
    """``dependencies.is_hub_admin``'s closure rule applied to one adopted
    pair: true only when it reaches ``(hub_admin, universal scope)``."""
    graph = closures if closures is not None else RbacClosures(db)
    hub_admin_role_id, universal_scope_ids = hub_admin_target_ids(db, closures=graph)
    return bool(
        hub_admin_role_id is not None
        and hub_admin_role_id in graph.role_descendants(target.role_id)
        and universal_scope_ids & graph.scope_descendants(target.scope_id)
    )


def any_pair_is_hub_admin(
    db: Session, targets: list[ViewAsTarget], *, closures: RbacClosures | None = None
) -> bool:
    """Hub Admin if ANY adopted pair is — the same "any one row" rule
    ``is_hub_admin`` applies to real assignments."""
    graph = closures if closures is not None else RbacClosures(db)
    return any(pair_is_hub_admin(db, target, closures=graph) for target in targets)


def held_closures_for(targets: list[ViewAsTarget], *, closures: RbacClosures) -> list[HeldClosure]:
    """Each adopted pair as one assignment row — the shape
    ``rbac_delegation_service.can_delegate`` reads, so the delegation rule
    (any ONE row must cover the target) is reused unchanged."""
    return [
        HeldClosure(
            role_id=target.role_id,
            scope_id=target.scope_id,
            role_ids=closures.role_descendants(target.role_id),
            scope_ids=closures.scope_descendants(target.scope_id),
        )
        for target in targets
    ]


def resolve_view_as_access(
    db: Session,
    targets: list[ViewAsTarget],
    *,
    user_id: int | None = None,
    closures: RbacClosures | None = None,
) -> ViewerAccess:
    """The ``ViewerAccess`` a holder of exactly these pairs would get, plus
    the grants shared DIRECTLY with ``user_id`` when given — the selected
    View As user, or the caller themselves when only pairs were given
    without ``anonymous``.

    Pairs are expanded ONE AT A TIME and unioned afterwards — the same
    per-row rule ``effective_pairs`` applies to real assignments, so a role
    from one pair is never combined with a scope from another.

    ``full_access`` follows ``any_pair_is_hub_admin``.
    """
    graph = closures if closures is not None else RbacClosures(db)

    if any_pair_is_hub_admin(db, targets, closures=graph):
        return ViewerAccess(visibility=None, full_access=True)

    pairs: set[tuple[int, int]] = set()
    for held in held_closures_for(targets, closures=graph):
        for role_id in held.role_ids:
            for scope_id in held.scope_ids:
                pairs.add((role_id, scope_id))
    seeds = grants_matching_pairs(db, pairs=pairs, user_id=user_id)
    return ViewerAccess(visibility=fold(build_node_tree(db), seeds), full_access=False)


def resolve_access_without_direct_grants(
    db: Session, user_id: int, *, closures: RbacClosures | None = None
) -> ViewerAccess:
    """``user_id``'s own access from their assignments ONLY — what
    ``anonymous=true`` gives a non-admin caller with no View As pairs.
    Same per-row expansion as the regular read path (``effective_pairs``),
    without the grants shared directly with them."""
    graph = closures if closures is not None else RbacClosures(db)
    pairs = effective_pairs(db, user_id, closures=graph)
    seeds = grants_matching_pairs(db, pairs=pairs, user_id=None)
    return ViewerAccess(visibility=fold(build_node_tree(db), seeds), full_access=False)
