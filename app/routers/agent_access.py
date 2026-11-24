"""Private live-ACL endpoint used by the Hub Agent before retrieval."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.db_v2.models.role import RoleV2
from app.dependencies import CurrentHubUser, is_hub_admin
from app.models.auth import UserInfo
from app.services.access_visibility_service import list_visible_nodes, resolve_viewer_access
from app.services.agent_identity import AgentIdentity, require_agent_identity
from app.services import mcp_oauth_hardening as hardening
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.mcp_access_policy import EXTERNAL_PROVIDERS, admit_client, load_approved_clients
from app.services.rbac_graph_service import RbacClosures, held_assignments
from app.services.user_db_service import find_user_by_email


router = APIRouter(prefix="/data/agent/access", tags=["Agent access"])
logger = logging.getLogger(__name__)

_MCP_DENIED_DETAIL = "MCP access is not permitted for this client."


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
            detail="Agent identity has not signed in to the Hub yet.",
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
            detail=_MCP_DENIED_DETAIL,
        )
    return decision.client_kind


def _mcp_session_denied(reason: str, client_kind: str, client_id: str) -> HTTPException:
    hardening.audit(
        "mcp_session_denied", level=logging.WARNING, category="session", reason=reason,
        client_kind=client_kind, client_id=client_id,
    )
    # Exactly the admission denial: the Agent maps 403 to PermissionError.
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_MCP_DENIED_DETAIL)


_ANOMALY_EXCEEDED = "exceeded"


async def _count_external_mcp_call(
    request: Request,
    identity: AgentIdentity = Depends(require_agent_identity),
) -> str | None:
    """Daily per-(provider, user) tool-call counter (anomaly guard).

    Active only for a provider listed in
    ``MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON`` and only when the Agent
    forwards the token's ``sid`` (``X-RenPhil-MCP-Sid``). Counted in the
    shared MCP OAuth limiter store under a key bound to the UTC day; the user
    is a keyed HMAC digest. No store, or a store failure, is a 503 (fail
    closed, same posture as the OAuth limiters). Returns ``"exceeded"`` when
    the limit is passed; the endpoint then revokes the family and denies.
    """
    sid = request.headers.get(hardening.SID_HEADER)
    client_id = request.query_params.get("client_id")
    if not sid or not client_id:
        return None
    settings = get_settings()
    try:
        provider = load_approved_clients(settings).provider_for(client_id)
    except Exception:
        provider = None
    if provider is None:
        return None  # the Hub Agent, or a client admission will refuse anyway
    limit = hardening.anomaly_limit(settings, provider)
    if limit is None:
        return None
    unavailable = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP access is temporarily unavailable.",
        headers={"Retry-After": str(rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS), "Cache-Control": "no-store"},
    )
    if not isinstance(limit, int):
        logger.warning("MCP tool call denied: category=anomaly_configuration_invalid")
        raise unavailable
    from app.routers.mcp_oauth import _rate_limit_store

    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    try:
        store = _rate_limit_store(settings)
        if store is None:
            raise rate_limit.RateLimitStoreUnavailable("rate-limit store not configured")
        key = rate_limit._hash_key(settings)
        environment = rate_limit._digest(key, "issuer:" + str(getattr(settings, "MCP_OAUTH_ISSUER", "") or ""))[:16]
        user = rate_limit._digest(key, "user:" + identity.email.strip().lower())[:40]
        check = rate_limit.Check(
            key=f"renphil:mcp-oauth:rl:v1:{environment}:mcp-daily:{provider}:{user}:{now:%Y%m%d}",
            limit=limit,
            # Outlives the day it counts; the key itself names the day.
            window=int((midnight - now).total_seconds()) + 3600,
        )
        decision = await rate_limit.evaluate(store, [check], "mcp-daily")
    except rate_limit.RateLimitStoreUnavailable:
        logger.warning("MCP tool call denied: category=rate_limit_store_unavailable")
        raise unavailable from None
    return None if decision.allowed else _ANOMALY_EXCEEDED


def _require_live_mcp_session(
    db: Session, *, client_kind: str, client_id: str, email: str, sid: str | None, anomaly: str | None,
) -> None:
    """Session (refresh family) checks for external MCP clients.

    * Anomaly: over the daily limit -> revoke this family (``anomaly``),
      call the notification hook, deny.
    * ``MCP_OAUTH_SESSION_LIVE_CHECK_ENABLED``: the forwarded ``sid`` must
      name a live family of this client and user (one indexed lookup);
      revoked, expired, unknown, malformed or missing -> deny. This endpoint
      knows the caller is an external client from the verified ``client_id``
      the Agent already sends, so a missing ``sid`` fails closed.
    The Hub Agent's own client is never subject to either check.
    """
    if client_kind not in EXTERNAL_PROVIDERS:
        return
    from app.services.mcp_oauth_service import revoke_session_family, session_is_live

    if anomaly == _ANOMALY_EXCEEDED and hardening.is_valid_sid(sid):
        try:
            revoke_session_family(db, sid=sid, client_id=client_id, email=email, reason="anomaly")
        except Exception:
            db.rollback()
            logger.warning("MCP anomaly revocation failed; the call is still denied.")
        subject_digest = hardening.audit_digest("user-email", email.strip().lower())
        hardening.audit(
            "anomaly_limit_exceeded", level=logging.WARNING, category="anomaly",
            client_kind=client_kind, client_id=client_id, family=hardening.family_ref(sid), user_email=subject_digest,
        )
        hardening.notify_user_of_anomaly(provider=client_kind, subject_digest=subject_digest, family_digest=hardening.family_ref(sid))
        raise _mcp_session_denied("anomaly", client_kind, client_id)
    if not hardening.session_live_check_enabled(get_settings()):
        return
    if sid is None:
        raise _mcp_session_denied("sid_missing", client_kind, client_id)
    if not hardening.is_valid_sid(sid):
        raise _mcp_session_denied("sid_malformed", client_kind, client_id)
    try:
        live = session_is_live(db, sid=sid, client_id=client_id, email=email)
    except Exception:
        db.rollback()
        live = False  # fail closed
    if not live:
        raise _mcp_session_denied("session_not_live", client_kind, client_id)


@router.get("/verified-granted-nodes", include_in_schema=False)
def get_verified_granted_nodes(
    client_id: str | None = Query(default=None, min_length=1, max_length=512),
    identity: AgentIdentity = Depends(require_agent_identity),
    db: Session = Depends(get_db_v2),
    mcp_sid: Annotated[str | None, Header(alias=hardening.SID_HEADER)] = None,
    mcp_anomaly: Annotated[str | None, Depends(_count_external_mcp_call)] = None,
) -> dict[str, object]:
    """Private, fail-closed live authorization for Agent and MCP retrieval.

    Unlike the legacy endpoint, supplied JWT-derived role values are ignored.
    Synchronous on purpose: the work is blocking database I/O, so FastAPI
    runs it in its worker pool instead of on the event loop.

    Only the OAuth-authenticated MCP capability route supplies ``client_id``;
    the calling MCP client must then be currently admitted (Hub Agent while
    Hub AI is on; Claude/ChatGPT/Gemini while enabled and exactly mapped).
    Direct Hub Agent retrieval keeps its existing signed identity contract.

    External clients are also subject to the opt-in session checks
    (``X-RenPhil-MCP-Sid``; see ``_require_live_mcp_session``).
    """
    client_kind = _require_mcp_client_admission(db, client_id) if client_id is not None else None
    if client_kind is not None:
        _require_live_mcp_session(
            db, client_kind=client_kind, client_id=client_id, email=identity.email, sid=mcp_sid, anomaly=mcp_anomaly,
        )
    response = _build_verified_live_acl_response(identity, db)
    if client_kind is not None:
        response["client_kind"] = client_kind
    return response
