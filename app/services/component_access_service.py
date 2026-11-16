"""Who can access one component — the reverse of the per-caller access check.

Built on ``access_visibility_service.users_granted_on_node`` (role/scope
grants, grants shared with an email, on the component or any ancestor, plus
the Hub Admin bypass and the mirror rule), run once for view and once for edit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.resource_grant import LEVEL_EDIT, LEVEL_VIEW
from app.db_v2.models.user import UserV2
from app.services.access_visibility_service import users_granted_on_node
from app.services.rbac_graph_service import RbacClosures


def list_users_with_component_access(
    db: Session, component: ComponentV2, *, closures: RbacClosures | None = None
) -> list[dict[str, Any]]:
    """Every non-deleted user with at least view on ``component``, with their
    level (``edit`` implies view, so an editor is reported once, as ``edit``).
    Sorted by name."""
    graph = closures if closures is not None else RbacClosures(db)
    node = ("component", component.id)
    view_ids = users_granted_on_node(db, node, closures=graph)
    if not view_ids:
        return []
    edit_ids = users_granted_on_node(db, node, closures=graph, edit_only=True)

    users = (
        db.query(UserV2)
        .filter(UserV2.id.in_(view_ids | edit_ids), UserV2.not_deleted())
        .order_by(UserV2.name, UserV2.id)
        .all()
    )
    return [
        {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "name": user.name,
            "work_email": user.work_email,
            "alias_email": user.alias_email,
            "personal_email": user.personal_email,
            "access_level": LEVEL_EDIT if user.id in edit_ids else LEVEL_VIEW,
        }
        for user in users
    ]
