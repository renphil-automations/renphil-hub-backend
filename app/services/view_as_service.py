"""View As — let a caller see the hub the way a holder of a WEAKER
``(role, scope)`` pair would.

A caller may adopt ``(role, scope)`` only when:

  1. ``scope`` is one they hold in ``role_assignments``, a direct or
     indirect child of one (``parent_child_scopes``), or the public scope.
     A universal scope covers every scope, exactly as everywhere else in the
     engine (``RbacClosures.scope_descendants``).
  2. ``role`` is one they hold on a scope that covers ``scope`` (the row's
     own scope or any ancestor of ``scope``), a direct or indirect child of
     such a role (``parent_child_roles``), or the public role.

That is ``effective_pairs`` read per assignment ROW, so the pairing rule
holds: holding (Program Lead, A) and (Program Member, B) never allows
viewing as (Program Lead, B).

Read-only: nothing here writes, and the caller's real access is never
widened — the adopted pair is always within what they already reach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db_v2.models.role import RoleV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.user import UserV2
from app.services.access_visibility_service import ViewerAccess, build_node_tree, fold
from app.services.rbac_graph_service import RbacClosures, held_assignments, hub_admin_target_ids
from app.services.resource_grant_service import seeds_for_principal

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
    role_name: str
    scope_id: int
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


def _sorted_id_names(rows) -> list[dict]:
    return [{"id": row_id, "name": name} for row_id, name in sorted(rows, key=lambda r: (r[1].lower(), r[0]))]


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
    """``[{"id", "name"}]`` for every scope ``viewable_scope_ids`` allows,
    unique and sorted by name."""
    scope_ids = viewable_scope_ids(db, user_id)
    if not scope_ids:
        return []
    return _sorted_id_names(db.query(ScopeV2.id, ScopeV2.name).filter(ScopeV2.id.in_(scope_ids)).all())


def list_viewable_roles(db: Session, user_id: int, scope_id: int) -> list[dict]:
    """``[{"id", "name"}]`` for every role the user may adopt on
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
    return _sorted_id_names(db.query(RoleV2.id, RoleV2.name).filter(RoleV2.id.in_(role_ids)).all())


# ---------------------------------------------------------
# Verifying a View As request
# ---------------------------------------------------------


def normalize_param(value: str | None) -> str | None:
    """``None`` for a missing, empty or whitespace-only parameter."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def verify_view_as(
    db: Session,
    user_id: int,
    *,
    role_name: str,
    scope_name: str,
    closures: RbacClosures | None = None,
) -> ViewAsTarget:
    """Resolve the two names and check the caller may adopt them — scope
    first, then role on that scope. Raises ``ViewAsError`` on any failure."""
    graph = closures if closures is not None else RbacClosures(db)

    scope = db.query(ScopeV2.id, ScopeV2.name).filter(ScopeV2.name == scope_name).first()
    if scope is None:
        raise ViewAsError(400, "view_as_scope_not_found", f"Scope '{scope_name}' does not exist.")
    if scope[0] not in viewable_scope_ids(db, user_id, closures=graph):
        raise ViewAsError(
            403, "view_as_scope_not_allowed", f"You are not allowed to view the hub as scope '{scope_name}'."
        )

    role = db.query(RoleV2.id, RoleV2.name).filter(RoleV2.name == role_name).first()
    if role is None:
        raise ViewAsError(400, "view_as_role_not_found", f"Role '{role_name}' does not exist.")
    if role[0] not in viewable_role_ids(db, user_id, scope[0], closures=graph):
        raise ViewAsError(
            403,
            "view_as_role_not_allowed",
            f"You are not allowed to view the hub as role '{role_name}' in scope '{scope_name}'.",
        )

    return ViewAsTarget(role_id=role[0], role_name=role[1], scope_id=scope[0], scope_name=scope[1])


def resolve_view_as_access(
    db: Session, target: ViewAsTarget, *, closures: RbacClosures | None = None
) -> ViewerAccess:
    """The ``ViewerAccess`` a holder of exactly ``target``'s pair would get.

    Grants naming the CALLER directly are deliberately not included — the
    point is to see what the role/scope sees, not the caller's own extras.

    ``full_access`` follows ``dependencies.is_hub_admin``'s closure rule
    applied to the adopted pair: true only when it reaches
    ``(hub_admin, universal scope)``.
    """
    graph = closures if closures is not None else RbacClosures(db)

    hub_admin_role_id, universal_scope_ids = hub_admin_target_ids(db, closures=graph)
    if (
        hub_admin_role_id is not None
        and hub_admin_role_id in graph.role_descendants(target.role_id)
        and universal_scope_ids & graph.scope_descendants(target.scope_id)
    ):
        return ViewerAccess(visibility=None, full_access=True)

    seeds = seeds_for_principal(db, role_id=target.role_id, scope_id=target.scope_id, user_id=None, closures=graph)
    return ViewerAccess(visibility=fold(build_node_tree(db), seeds), full_access=False)
