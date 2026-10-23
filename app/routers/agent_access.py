"""Private live-ACL endpoint used by the Hub Agent before retrieval."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.dependencies import CurrentHubUser, get_airtable_service, is_hub_admin
from app.models.auth import UserInfo
from app.services.access_visibility_service import list_visible_nodes, resolve_viewer_access
from app.services.agent_identity import AgentIdentity, require_agent_identity
from app.services.mcp_access_policy import admit_client, load_approved_clients
from app.services.airtable_service import AirtableService
from app.services.rbac_graph_service import RbacClosures
from app.services.user_db_service import find_user_by_email


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

    # An HR row nobody has signed in as (`has_signed_in` False, 2026-10
    # merge) still has no grants worth resolving, same as before.
    hub_user = find_user_by_email(db, identity.email)
    if hub_user is None or not hub_user.has_signed_in:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent identity has not signed in to the Hub yet.",
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


async def _build_verified_live_acl_response(
    identity: AgentIdentity,
    db: Session,
    airtable_service: AirtableService,
) -> dict[str, object]:
    """Resolve MCP authorization from current Hub roles, never token claims."""

    live_roles = await airtable_service.get_user_roles(identity.email)
    response = _build_live_acl_response(
        AgentIdentity(email=identity.email, roles=tuple(live_roles)), db
    )
    # This private response is consumed only by the Agent capability route.
    # It is never forwarded to an MCP client or exposed to model tool inputs.
    response["resolved_roles"] = live_roles
    return response


@router.get("/granted-nodes", include_in_schema=False)
def get_granted_nodes(
    identity: AgentIdentity = Depends(require_agent_identity),
    db: Session = Depends(get_db_v2),
) -> dict[str, object]:
    """Return the current payload grants for one Agent-authenticated user."""

    return _build_live_acl_response(identity, db)


def _require_mcp_client_admission(db: Session, client_id: str) -> str:
    """Re-check MCP client admission on every capability call.

    ``client_id`` is the verified ``client_id`` claim of the Hub-issued OAuth
    token, forwarded by the Agent capability API after RS256 verification (the
    endpoint itself requires the Agent service credential). Bot Management's
    master and per-provider toggles and the exact client mapping therefore take
    effect on the next tool call rather than at token expiry.
    """
    state = db.get(BotConfigurationStateV2, 1)
    version = db.get(BotConfigurationVersionV2, state.active_version_id) if state else None
    active = version.settings if version and isinstance(version.settings, dict) else {}
    decision = admit_client(active_settings=active, client_id=client_id, approved=load_approved_clients(get_settings()))
    if not decision.allowed or decision.client_kind is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MCP access is not permitted for this client.",
        )
    return decision.client_kind


@router.get("/verified-granted-nodes", include_in_schema=False)
async def get_verified_granted_nodes(
    client_id: str = Query(..., min_length=1, max_length=512),
    identity: AgentIdentity = Depends(require_agent_identity),
    db: Session = Depends(get_db_v2),
    airtable_service: AirtableService = Depends(get_airtable_service),
) -> dict[str, object]:
    """Live ACL for OAuth-authenticated MCP capability calls.

    The calling MCP client must be currently admitted (Hub Agent while Hub AI
    is on; Claude/ChatGPT/Gemini while enabled and exactly mapped). Any roles
    carried in the signed Agent envelope are ignored and reloaded from Hub's
    role authority for this request.
    """

    client_kind = _require_mcp_client_admission(db, client_id)
    response = await _build_verified_live_acl_response(identity, db, airtable_service)
    response["client_kind"] = client_kind
    return response
