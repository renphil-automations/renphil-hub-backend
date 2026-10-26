"""Private live-ACL endpoint used by the Hub Agent before retrieval."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.db_v2.models.role import RoleV2
from app.dependencies import CurrentHubUser, is_hub_admin
from app.models.auth import UserInfo
from app.services.access_visibility_service import list_visible_nodes, resolve_viewer_access
from app.services.agent_identity import AgentIdentity, require_agent_identity
from app.services.rbac_graph_service import RbacClosures, held_assignments
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


def _signed_in_hub_user(db: Session, email: str):
    """Return the caller's Hub user row, or refuse (fail closed)."""

    # An HR row nobody has signed in as (`has_signed_in` False, 2026-10
    # merge) still has no grants worth resolving, same as before.
    hub_user = find_user_by_email(db, email)
    if hub_user is None or not hub_user.has_signed_in:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent identity does not have an active Hub user.",
        )
    return hub_user


def _postgres_role_names(db: Session, hub_user_id: int, closures: RbacClosures) -> list[str]:
    """The caller's current role NAMES from Postgres ``role_assignments``.

    Same authority `is_hub_admin` and every grant match use: each held role
    plus every role beneath it in the role DAG (a parent inherits its
    children).  Airtable's Access Control table is never consulted.
    """

    role_ids: set[int] = set()
    for role_id, _scope_id in held_assignments(db, hub_user_id):
        role_ids |= closures.role_descendants(role_id)
    if not role_ids:
        return []
    rows = db.query(RoleV2.name).filter(RoleV2.id.in_(sorted(role_ids))).all()
    return sorted({str(row[0]).strip() for row in rows if row and str(row[0] or "").strip()})


def _build_live_acl_response(
    identity: AgentIdentity,
    db: Session,
    *,
    hub_user=None,
    closures: RbacClosures | None = None,
) -> dict[str, object]:
    """Resolve the Agent caller's current Hub grants without mutating data."""

    if hub_user is None:
        hub_user = _signed_in_hub_user(db, identity.email)

    current = CurrentHubUser(
        info=UserInfo(
            email=identity.email,
            name=hub_user.name or "Hub user",
            roles=list(identity.roles),
        ),
        hub_user_id=hub_user.id,
        email=identity.email,
    )
    closures = closures if closures is not None else RbacClosures(db)
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


def _build_verified_live_acl_response(
    identity: AgentIdentity,
    db: Session,
) -> dict[str, object]:
    """Resolve current roles from Hub's Postgres authority.

    The signed envelope binds the authenticated email to the trusted Agent.
    It deliberately does not grant access through the role snapshot it
    carries, and Airtable's Access Control table is not consulted: roles,
    the admin decision and grants all come from Postgres ``role_assignments``
    and resource grants, in one database session, with no external call.
    """
    hub_user = _signed_in_hub_user(db, identity.email)
    closures = RbacClosures(db)
    live_roles = _postgres_role_names(db, hub_user.id, closures)
    response = _build_live_acl_response(
        AgentIdentity(email=identity.email, roles=tuple(live_roles)),
        db,
        hub_user=hub_user,
        closures=closures,
    )
    response["resolved_roles"] = list(live_roles)
    return response


@router.get("/granted-nodes", include_in_schema=False)
def get_granted_nodes(
    identity: AgentIdentity = Depends(require_agent_identity),
    db: Session = Depends(get_db_v2),
) -> dict[str, object]:
    """Return the current payload grants for one Agent-authenticated user."""

    return _build_live_acl_response(identity, db)


@router.get("/verified-granted-nodes", include_in_schema=False)
def get_verified_granted_nodes(
    identity: AgentIdentity = Depends(require_agent_identity),
    db: Session = Depends(get_db_v2),
) -> dict[str, object]:
    """Private, fail-closed live authorization for Agent retrieval.

    Unlike the legacy endpoint, supplied JWT-derived role values are ignored.
    Synchronous on purpose: the work is blocking database I/O, so FastAPI
    runs it in its worker pool instead of on the event loop.
    """
    return _build_verified_live_acl_response(identity, db)
