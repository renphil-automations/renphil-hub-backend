"""Private live-ACL endpoint used by the Hub Agent before retrieval."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.db_v2.models.hub_user import HubUserV2
from app.dependencies import CurrentHubUser, is_hub_admin
from app.models.auth import UserInfo
from app.services.access_visibility_service import list_visible_nodes, resolve_viewer_access
from app.services.agent_identity import AgentIdentity, require_agent_identity
from app.services.rbac_graph_service import RbacClosures


router = APIRouter(prefix="/data/agent/access", tags=["Agent access"])


def _authorization_fingerprint(
    *,
    email: str,
    is_admin: bool,
    granted_node_keys: list[str],
) -> str:
    """Hash exactly the authorization facts that affect retrieval."""

    canonical = json.dumps(
        {
            "email": email,
            "is_admin": is_admin,
            "granted_node_keys": granted_node_keys,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_live_acl_response(identity: AgentIdentity, db: Session) -> dict[str, object]:
    """Resolve the Agent caller's current Hub grants without mutating data."""

    hub_user = (
        db.query(HubUserV2)
        .filter(HubUserV2.email == identity.email)
        .first()
    )
    if hub_user is None or not hub_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent identity does not have an active Hub user.",
        )

    current = CurrentHubUser(
        info=UserInfo(
            email=identity.email,
            name=hub_user.name or "Hub user",
            roles=list(identity.roles),
        ),
        hub_user_id=hub_user.id,
        email=identity.email,
    )
    closures = RbacClosures(db)
    is_admin = is_hub_admin(db, current, closures=closures)

    granted_node_keys: list[str] = []
    if not is_admin:
        viewer_access = resolve_viewer_access(
            db,
            hub_user.id,
            is_admin=False,
            closures=closures,
        )
        if viewer_access.visibility is not None:
            granted_node_keys = sorted(
                {
                    f"{node['node_kind']}:{node['node_id']}"
                    for node in list_visible_nodes(viewer_access.visibility)
                    if node.get("granted")
                }
            )

    return {
        "email": identity.email,
        "is_admin": is_admin,
        "granted_node_keys": granted_node_keys,
        "authorization_fingerprint": _authorization_fingerprint(
            email=identity.email,
            is_admin=is_admin,
            granted_node_keys=granted_node_keys,
        ),
    }


@router.get("/granted-nodes", include_in_schema=False)
def get_granted_nodes(
    identity: AgentIdentity = Depends(require_agent_identity),
    db: Session = Depends(get_db_v2),
) -> dict[str, object]:
    """Return the current payload grants for one Agent-authenticated user."""

    return _build_live_acl_response(identity, db)
