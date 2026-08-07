"""
Hub service layer (phase 2 — see AI Docs/plan_nav_tabs_phase2_2026-07-30.md
§5.4). HubV2 is a one-row singleton with no title/slug/collection semantics
of its own, so this is thin: mostly `access_control_service.get_hub` +
`apply_write`, both already built. Deliberately not merged into
access_control_service.py itself — that module owns the propagation
ENGINE (NodeRef-generic), not per-collection CRUD/formatting, matching how
nav_tab_service.py / gridstack_service.py are the CRUD layers over the same
engine for their own kinds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.services import access_control_service
from app.services.access_control_service import NodeRef


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_hub(hub: Any) -> dict[str, Any]:
    return {
        "documentId": hub.document_id,
        "title": "Hub",
        "access_control": hub.access_control,
    }


def get_hub_v2(db: Session) -> dict[str, Any] | None:
    hub = access_control_service.get_hub(db)
    return _format_hub(hub) if hub is not None else None


def update_hub_v2(db: Session, access_control: dict[str, Any] | None) -> dict[str, Any] | None:
    try:
        hub = access_control_service.get_hub(db)
        if hub is None:
            return None
        if access_control is not None:
            access_control_service.apply_write(db, NodeRef("hub", hub.id), access_control)
        hub.updated_at = _utc_now()
        db.commit()
        return _format_hub(hub)
    except Exception:
        db.rollback()
        raise


def preview_hub_access_v2(
    db: Session, access_control: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Read-only — backs the preview modal (§6.3). Never writes; a caller
    that wants the write applies it separately via `update_hub_v2`."""
    hub = access_control_service.get_hub(db)
    if hub is None:
        return None
    plan = access_control_service.plan_write(db, NodeRef("hub", hub.id), access_control)
    return access_control_service.describe_write_plan(db, plan)


def purge_hub_principal_v2(
    db: Session, principal_payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Strips `principal_payload` from the hub's own admins/viewers and its
    entire subtree (every nav tab, root, variant, sub-tab) — the global kill
    switch (§6.4, §5.1's `purge_principal` docstring). No `reset_to_inherited`
    for the hub: it has no parent to inherit from."""
    try:
        hub = access_control_service.get_hub(db)
        if hub is None:
            return None
        principal = access_control_service.principal_from_payload(principal_payload)
        touched = access_control_service.purge_principal(db, NodeRef("hub", hub.id), principal)
        db.commit()
        return {"touched": [access_control_service.node_summary(db, r) for r in touched]}
    except Exception:
        db.rollback()
        raise
