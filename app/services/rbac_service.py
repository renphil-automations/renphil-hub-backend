"""Role/scope entity lifecycle
(plan_access_control_schema_2026-08-22.md §3.2, §3.4).

Split from ``rbac_graph_service`` on purpose: that module owns the RULES the
two DAGs obey, this one owns the rows' create/read/update/delete. Edge
operations live there, not here — anything touching ``parent_child_roles`` or
``parent_child_scopes`` has to go through the validator.

v1 covers definitions only. Assignments (and the §6 delegation rule that
governs them) are a later phase.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db_v2.models.role import RoleEdgeV2, RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2
from app.services.rbac_graph_service import RbacGraphError, set_role_rank


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------
# Serialization — direct edges only, never the closure
# ---------------------------------------------------------


def _role_edge_map(db: Session) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    """(parents_by_child, children_by_parent) for every role in one query.

    Built once per list request rather than per row: the alternative is two
    queries per role, which is the classic N+1 on a screen whose entire job
    is showing every role at once.
    """
    parents: dict[int, list[int]] = {}
    children: dict[int, list[int]] = {}
    for parent_id, child_id in db.query(
        RoleEdgeV2.parent_role_id, RoleEdgeV2.child_role_id
    ).all():
        parents.setdefault(child_id, []).append(parent_id)
        children.setdefault(parent_id, []).append(child_id)
    return parents, children


def _scope_edge_map(db: Session) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    parents: dict[int, list[int]] = {}
    children: dict[int, list[int]] = {}
    for parent_id, child_id in db.query(
        ScopeEdgeV2.parent_scope_id, ScopeEdgeV2.child_scope_id
    ).all():
        parents.setdefault(child_id, []).append(parent_id)
        children.setdefault(parent_id, []).append(child_id)
    return parents, children


def serialize_role(
    role: RoleV2, parents: dict[int, list[int]], children: dict[int, list[int]]
) -> dict:
    return {
        "id": role.id,
        "key": role.key,
        "name": role.name,
        "description": role.description,
        "rank": role.rank,
        "is_system": bool(role.is_system),
        "parent_ids": sorted(parents.get(role.id, [])),
        "child_ids": sorted(children.get(role.id, [])),
    }


def serialize_scope(
    scope: ScopeV2, parents: dict[int, list[int]], children: dict[int, list[int]]
) -> dict:
    return {
        "id": scope.id,
        "key": scope.key,
        "name": scope.name,
        "description": scope.description,
        "is_universal": bool(scope.is_universal),
        "is_system": bool(scope.is_system),
        "parent_ids": sorted(parents.get(scope.id, [])),
        "child_ids": sorted(children.get(scope.id, [])),
    }


# ---------------------------------------------------------
# Roles
# ---------------------------------------------------------


def list_roles(db: Session) -> list[dict]:
    """Ordered by rank so the response reads top-of-org first, then by name
    so equal-rank peers (which is every Lead, by design) have a stable
    order rather than whatever the heap returns."""
    parents, children = _role_edge_map(db)
    rows = db.query(RoleV2).order_by(RoleV2.rank, RoleV2.name).all()
    return [serialize_role(r, parents, children) for r in rows]


def get_role(db: Session, role_id: int) -> dict | None:
    role = db.query(RoleV2).filter(RoleV2.id == role_id).first()
    if role is None:
        return None
    parents, children = _role_edge_map(db)
    return serialize_role(role, parents, children)


def _assert_role_key_free(db: Session, key: str, name: str) -> None:
    if db.query(RoleV2.id).filter(RoleV2.key == key).first():
        raise RbacGraphError("duplicate_key", f"A role with key {key!r} already exists", key=key)
    if db.query(RoleV2.id).filter(RoleV2.name == name).first():
        raise RbacGraphError(
            "duplicate_name", f"A role named {name!r} already exists", name=name
        )


def create_role(
    db: Session, *, key: str, name: str, description: str | None, rank: int
) -> dict:
    _assert_role_key_free(db, key, name)
    role = RoleV2(
        key=key,
        name=name,
        description=description,
        rank=rank,
        is_system=False,
        created_at=_utc_now(),
    )
    db.add(role)
    db.flush()
    return serialize_role(role, {}, {})


def update_role(
    db: Session,
    role_id: int,
    *,
    name: str | None,
    description: str | None,
    rank: int | None,
    description_provided: bool,
) -> dict | None:
    """`key` is not updatable — see UpdateRoleRequest's docstring.

    `description_provided` distinguishes "omitted, leave alone" from
    "explicitly sent as null, clear it", the same three-way the nav-tab
    `icon` field uses (schemas/tab.py). Without it there is no way to clear
    a description once set.
    """
    role = db.query(RoleV2).filter(RoleV2.id == role_id).first()
    if role is None:
        return None

    if name is not None and name != role.name:
        if db.query(RoleV2.id).filter(RoleV2.name == name, RoleV2.id != role_id).first():
            raise RbacGraphError(
                "duplicate_name", f"A role named {name!r} already exists", name=name
            )
        role.name = name

    if description_provided:
        role.description = description

    # Routed through the validator, never assigned directly: a rank change is
    # the one edit that can invalidate existing edges (§5.3).
    if rank is not None:
        set_role_rank(db, role_id, rank)

    role.updated_at = _utc_now()
    db.flush()

    parents, children = _role_edge_map(db)
    return serialize_role(role, parents, children)


def delete_role(db: Session, role_id: int) -> bool:
    """Refuses on a system role or one that still has assignments.

    The assignment check is done here rather than left to the FK's RESTRICT
    so the caller gets a count and a reason instead of an opaque
    IntegrityError. Incident EDGES are not checked — they CASCADE, which is
    correct: an edge describing a role that no longer exists is meaningless,
    and keeping it would block the delete for no benefit.
    """
    role = db.query(RoleV2).filter(RoleV2.id == role_id).first()
    if role is None:
        return False

    if role.is_system:
        raise RbacGraphError(
            "system_role",
            f"{role.name!r} is a system role and cannot be deleted",
            role_id=role_id,
        )

    assignment_count = (
        db.query(RoleAssignmentV2).filter(RoleAssignmentV2.role_id == role_id).count()
    )
    if assignment_count:
        raise RbacGraphError(
            "role_in_use",
            (
                f"{role.name!r} is still assigned to {assignment_count} "
                f"user/scope combination(s). Remove those assignments first."
            ),
            role_id=role_id,
            assignment_count=assignment_count,
        )

    db.delete(role)
    db.flush()
    return True


# ---------------------------------------------------------
# Scopes
# ---------------------------------------------------------


def list_scopes(db: Session) -> list[dict]:
    """Universal scope first (it is conceptually the root), then by name."""
    parents, children = _scope_edge_map(db)
    rows = db.query(ScopeV2).order_by(ScopeV2.is_universal.desc(), ScopeV2.name).all()
    return [serialize_scope(s, parents, children) for s in rows]


def get_scope(db: Session, scope_id: int) -> dict | None:
    scope = db.query(ScopeV2).filter(ScopeV2.id == scope_id).first()
    if scope is None:
        return None
    parents, children = _scope_edge_map(db)
    return serialize_scope(scope, parents, children)


def create_scope(
    db: Session, *, key: str, name: str, description: str | None, is_universal: bool
) -> dict:
    if db.query(ScopeV2.id).filter(ScopeV2.key == key).first():
        raise RbacGraphError("duplicate_key", f"A scope with key {key!r} already exists", key=key)
    if db.query(ScopeV2.id).filter(ScopeV2.name == name).first():
        raise RbacGraphError(
            "duplicate_name", f"A scope named {name!r} already exists", name=name
        )

    # Checked here as well as by the partial unique index, purely so the
    # caller gets a named error instead of an IntegrityError. The index is
    # still the thing that makes it true under concurrency.
    if is_universal:
        existing = db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).first()
        if existing is not None:
            raise RbacGraphError(
                "universal_scope_exists",
                (
                    f"{existing.name!r} is already the universal scope. There can "
                    f"only be one, or \"every scope\" becomes ambiguous."
                ),
                scope_id=existing.id,
            )

    scope = ScopeV2(
        key=key,
        name=name,
        description=description,
        is_universal=is_universal,
        is_system=False,
        created_at=_utc_now(),
    )
    db.add(scope)
    db.flush()
    return serialize_scope(scope, {}, {})


def update_scope(
    db: Session,
    scope_id: int,
    *,
    name: str | None,
    description: str | None,
    description_provided: bool,
) -> dict | None:
    """Neither `key` nor `is_universal` is updatable — see
    UpdateScopeRequest's docstring for why flipping `is_universal` has no
    sane migration in either direction."""
    scope = db.query(ScopeV2).filter(ScopeV2.id == scope_id).first()
    if scope is None:
        return None

    if name is not None and name != scope.name:
        if db.query(ScopeV2.id).filter(ScopeV2.name == name, ScopeV2.id != scope_id).first():
            raise RbacGraphError(
                "duplicate_name", f"A scope named {name!r} already exists", name=name
            )
        scope.name = name

    if description_provided:
        scope.description = description

    scope.updated_at = _utc_now()
    db.flush()

    parents, children = _scope_edge_map(db)
    return serialize_scope(scope, parents, children)


def delete_scope(db: Session, scope_id: int) -> bool:
    """Same terms as delete_role. Edges cascade; assignments block."""
    scope = db.query(ScopeV2).filter(ScopeV2.id == scope_id).first()
    if scope is None:
        return False

    if scope.is_system:
        raise RbacGraphError(
            "system_scope",
            f"{scope.name!r} is a system scope and cannot be deleted",
            scope_id=scope_id,
        )

    assignment_count = (
        db.query(RoleAssignmentV2).filter(RoleAssignmentV2.scope_id == scope_id).count()
    )
    if assignment_count:
        raise RbacGraphError(
            "scope_in_use",
            (
                f"{scope.name!r} is still assigned to {assignment_count} "
                f"user/role combination(s). Remove those assignments first."
            ),
            scope_id=scope_id,
            assignment_count=assignment_count,
        )

    db.delete(scope)
    db.flush()
    return True
