"""Qdrant update receipts for a committed resource-grant mutation."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.access_visibility_service import build_node_tree


def affected_component_search_updates(
    db: Session,
    *,
    node_kind: str,
    node_id: int,
) -> list[dict[str, int | str]]:
    """Return every component whose inherited ACL changes at this node."""
    tree = build_node_tree(db)
    root = (node_kind, node_id)
    affected = tree.descendants(root)
    if node_kind == "component":
        affected.add(root)
    return [
        {"component_id": component_id, "action": "upsert"}
        for kind, component_id in sorted(affected)
        if kind == "component"
    ]
