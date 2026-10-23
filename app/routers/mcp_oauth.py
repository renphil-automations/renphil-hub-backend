"""Hub OAuth authorization-code routes for approved MCP clients."""

from __future__ import annotations

import hmac
import logging
import re
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.services.mcp_access_policy import AdmissionDecision, admit_client, load_approved_clients
from app.services.mcp_oauth_dpop import DPOP_SIGNING_ALGORITHMS, McpDPoPError, verify_token_request_proof
from app.services.mcp_oauth_keys import HubMcpSigningKey, McpOAuthKeyError
from app.services.mcp_oauth_service import LocalMcpOAuthService, McpOAuthError, PersistentMcpOAuthService
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.agent_identity import require_agent_sync_token


logger = logging.getLogger(__name__)

# Abuse protection applies ONLY to these public MCP OAuth paths. Hub Google
# login (``/auth/*``), session creation, access checks and every other Hub
# route use the default route class and are never limited here.
RATE_LIMITED_ENDPOINTS: dict[str, str] = {
    "/mcp/oauth/authorize": "authorize",
    "/mcp/oauth/token": "token",
    "/mcp/oauth/register": "register",
    "/register": "register",
    "/mcp/oauth/jwks.json": "jwks",
    "/.well-known/oauth-authorization-server": "discovery",
    "/.well-known/oauth-authorization-server/mcp/oauth": "discovery",
    "/.well-known/openid-configuration/mcp/oauth": "discovery",
    "/mcp/oauth/.well-known/openid-configuration": "discovery",
}
# Server-to-server only (requires the Agent sync token plus a verified Hub
# session); it is not a public OAuth endpoint and is deliberately excluded.
UNLIMITED_MCP_ROUTES = frozenset({"/mcp/oauth/agent-token"})

_THROTTLED = {"error": "temporarily_unavailable", "error_description": "Too many requests. Retry later."}
_UNAVAILABLE = {
    "error": "temporarily_unavailable",
    "error_description": "The authorization service is temporarily unavailable. Retry later.",
}
_STORES: dict[tuple, object] = {}


def _rate_limit_store(settings):
    """One store per configuration; ``None`` means fail closed."""
    cache_key = (
        (getattr(settings, "UPSTASH_REDIS_REST_URL", None) or "").strip(),
        (getattr(settings, "UPSTASH_REDIS_REST_TOKEN", None) or "").strip(),
        rate_limit.is_local_issuer(getattr(settings, "MCP_OAUTH_ISSUER", None)),
    )
    if cache_key not in _STORES:
        _STORES.clear()
        _STORES[cache_key] = rate_limit.build_store(settings)
    return _STORES[cache_key]


def _oauth_error(status_code: int, body: dict, retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=body,
        headers={"Retry-After": str(int(retry_after)), "Cache-Control": "no-store"},
    )


def _client_bucket(settings, client_id: object) -> str:
    """Approved provider name or the shared ``unknown`` bucket; never raises."""
    try:
        approved = load_approved_clients(settings)
        provider = approved.provider_for(client_id)
        agent_client_id = approved.agent_client_id
    except Exception:  # malformed configuration or input never becomes a 500
        return "unknown"
    mapping = {client_id: provider} if isinstance(client_id, str) and provider else {}
    return rate_limit.client_bucket(client_id, mapping, agent_client_id)


async def _token_client_bucket(request: Request, settings) -> str:
    try:
        form = await request.form()
        client_id = form.get("client_id")
    except Exception:
        return "unknown"
    return _client_bucket(settings, client_id)


async def _record_token_failure(store, settings, network: str) -> None:
    """Count a failed exchange toward the per-network failure budget."""
    try:
        await store.evaluate([rate_limit.failure_check(settings, network=network)])
    except rate_limit.RateLimitStoreUnavailable:
        # The response is already decided; the next request fails closed.
        logger.warning("MCP OAuth failure not recorded: endpoint=token category=rate_limit_store_unavailable")


class McpOAuthRateLimitedRoute(APIRoute):
    """Route class used only by the MCP OAuth routers.

    The limiter runs before parameter parsing and validation, so malformed or
    unknown requests are still counted and can never bypass it or cause a 500.
    """

    def get_route_handler(self):
        original = super().get_route_handler()
        endpoint = RATE_LIMITED_ENDPOINTS.get(self.path)
        if endpoint is None:
            return original

        async def limited(request: Request):
            settings = get_settings()
            if not getattr(settings, "MCP_OAUTH_ENABLED", False):
                # Disabled MCP OAuth keeps its existing 404/400 behaviour.
                return await original(request)
            network = rate_limit.client_network(
                forwarded_for=request.headers.get("x-forwarded-for"),
                peer_host=request.client.host if request.client else None,
            )
            store = _rate_limit_store(settings)
            try:
                if store is None:
                    raise rate_limit.RateLimitStoreUnavailable("rate-limit store not configured")
                if endpoint == "token":
                    decision = await rate_limit.evaluate(
                        store, rate_limit.build_checks(settings, endpoint, network=network, client=None, phase="pre"), endpoint,
                    )
                    if decision.allowed:
                        client = await _token_client_bucket(request, settings)
                        decision = await rate_limit.evaluate(
                            store, rate_limit.build_checks(settings, endpoint, network=network, client=client, phase="client"), endpoint,
                        )
                else:
                    client = _client_bucket(settings, request.query_params.get("client_id")) if endpoint == "authorize" else None
                    decision = await rate_limit.evaluate(
                        store, rate_limit.build_checks(settings, endpoint, network=network, client=client), endpoint,
                    )
            except rate_limit.RateLimitStoreUnavailable:
                logger.warning("MCP OAuth request denied: endpoint=%s category=rate_limit_store_unavailable", endpoint)
                return _oauth_error(503, _UNAVAILABLE, rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS)
            if not decision.allowed:
                return _oauth_error(429, _THROTTLED, decision.retry_after_seconds)
            if endpoint != "token":
                return await original(request)
            try:
                response = await original(request)
            except Exception:
                await _record_token_failure(store, settings, network)
                raise
            if response.status_code >= 400:
                await _record_token_failure(store, settings, network)
            return response

        return limited


router = APIRouter(prefix="/mcp/oauth", tags=["MCP OAuth"], route_class=McpOAuthRateLimitedRoute)
metadata_router = APIRouter(tags=["MCP OAuth"], route_class=McpOAuthRateLimitedRoute)


def _disabled() -> HTTPException:
    # Keep inactive OAuth routes indistinguishable from an unavailable service.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _active_bot_settings(db: Session) -> dict:
    state = db.get(BotConfigurationStateV2, 1)
    version = db.get(BotConfigurationVersionV2, state.active_version_id) if state else None
    return version.settings if version and isinstance(version.settings, dict) else {}


def _external_admission(db: Session, client_id: str) -> AdmissionDecision:
    """Apply the strict approved-client policy before any OAuth issuance.

    Only Claude, ChatGPT and Gemini clients that are exactly registered and
    exactly mapped, with Bot Management's master and provider toggles on, are
    admitted. Everything else, including an empty or invalid mapping, is
    denied. The Hub Agent never uses these public routes.
    """
    decision = admit_client(
        active_settings=_active_bot_settings(db), client_id=client_id,
        approved=load_approved_clients(get_settings()),
    )
    if decision.client_kind == "hub_agent":
        return AdmissionDecision(False, None, "agent_client_not_public")
    return decision


def _external_client_is_enabled(db: Session, client_id: str) -> bool:
    return _external_admission(db, client_id).allowed


def _is_agent_client(client_id: str) -> bool:
    """The internal Agent is not a public OAuth authorization-code client."""
    configured = (getattr(get_settings(), "MCP_OAUTH_AGENT_CLIENT_ID", "") or "").strip()
    supplied = (client_id or "").strip()
    return bool(configured) and hmac.compare_digest(
        supplied.encode("utf-8"), configured.encode("utf-8")
    )


@lru_cache(maxsize=1)
def _service() -> PersistentMcpOAuthService:
    settings = get_settings()
    if not settings.MCP_OAUTH_ENABLED:
        raise _disabled()
    issuer = (settings.MCP_OAUTH_ISSUER or "").rstrip("/")
    private_key = (settings.MCP_OAUTH_SIGNING_PRIVATE_KEY or "").strip()
    key_id = (settings.MCP_OAUTH_SIGNING_KEY_ID or "").strip()
    if not issuer or not private_key or not key_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP OAuth is not configured.")
    try:
        clients = LocalMcpOAuthService.parse_clients(settings.MCP_OAUTH_CLIENTS_JSON)
        return PersistentMcpOAuthService(
            clients=clients,
            issuer=issuer,
            audience=settings.MCP_OAUTH_AUDIENCE,
            signing_key=HubMcpSigningKey(private_pem=private_key, key_id=key_id),
            code_ttl_seconds=settings.MCP_OAUTH_CODE_TTL_SECONDS,
            access_token_ttl_seconds=settings.MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS,
        )
    except (McpOAuthError, McpOAuthKeyError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP OAuth is not configured.") from exc


def _redirect_with_code(*, redirect_uri: str, code: str, state: str) -> str:
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((("code", code), ("state", state)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def complete_hub_login(*, db: Session, continuation_uri: str, email: str, subject: str) -> str | None:
    """Convert a validated Hub sign-in into an external OAuth code.

    Returning ``None`` preserves the normal Hub login flow. No Hub session JWT
    is placed in an internal callback URL or sent to the external MCP client.
    """
    # Check the continuation shape before loading the MCP configuration.  An
    # enabled but incomplete MCP configuration must never break normal Hub
    # sign-in, whose frontend redirect does not use this private prefix.
    issuer = (get_settings().MCP_OAUTH_ISSUER or "").rstrip("/")
    prefix = f"{issuer}/continue/" if issuer else ""
    if not prefix or not continuation_uri.startswith(prefix):
        return None
    service = _service()
    request_id = continuation_uri.removeprefix(prefix)
    if not request_id or "/" in request_id or "?" in request_id or "#" in request_id:
        raise McpOAuthError("OAuth continuation is invalid.")
    code, redirect_uri, client_state = service.complete_authorization(
        db, request_id=request_id, email=email, subject=subject,
    )
    return _redirect_with_code(redirect_uri=redirect_uri, code=code, state=client_state)


@router.get("/authorize")
async def authorize(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    db: Session = Depends(get_db_v2),
):
    """Begin authorization, then reuse normal Hub Google sign-in."""
    if response_type != "code":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="response_type must be code.")
    if _is_agent_client(client_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth authorization request.")
    if not _external_client_is_enabled(db, client_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="External MCP access is not available for this client.")
    service = _service()
    try:
        request_id = service.start_authorization(db,
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        )
    except McpOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth authorization request.") from exc
    # The existing Hub callback recognizes this continuation and converts the
    # verified login directly into an authorization code. This avoids a second
    # Google callback registration and never exposes a Hub session token.
    continuation = f"{service.issuer}/continue/{request_id}"
    return RedirectResponse(url=f"/auth/login?{urlencode({'redirect_uri': continuation})}")


@router.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_verifier: str = Form(...),
    dpop: str | None = Header(default=None, alias="DPoP"),
    db: Session = Depends(get_db_v2),
):
    if grant_type != "authorization_code":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported grant type.")
    if _is_agent_client(client_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth token request.")
    decision = _external_admission(db, client_id)
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="External MCP access is not available for this client.")
    service = _service()
    # RFC 9449 §5: every external MCP token is bound to the client's DPoP key.
    # A missing or invalid proof is rejected without consuming the code, and
    # there is no bearer-token fallback.
    if not isinstance(dpop, str) or not dpop.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_dpop_proof", "error_description": "A DPoP proof is required."},
        )
    try:
        dpop_jkt = verify_token_request_proof(dpop, token_endpoint=f"{service.issuer}/token").jkt
    except McpDPoPError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_dpop_proof", "error_description": "Invalid DPoP proof."},
        ) from exc
    try:
        return service.exchange_code(
            db, code=code, client_id=client_id, redirect_uri=redirect_uri, code_verifier=code_verifier,
            dpop_jkt=dpop_jkt, client_kind=decision.client_kind,
        )
    except McpOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth token request.") from exc


_REGISTRATION_DISABLED = {
    "error": "invalid_client_metadata",
    "error_description": "Dynamic client registration is not supported. Only pre-approved clients may connect.",
}


@router.post("/register", include_in_schema=False)
async def register_dynamic_client() -> JSONResponse:
    """RFC 7591 is disabled in this release; every request is rejected.

    The request body is never parsed, so no client metadata can influence the
    response or create a client.
    """
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=_REGISTRATION_DISABLED)


@metadata_router.post("/register", include_in_schema=False)
async def register_dynamic_client_root_alias() -> JSONResponse:
    """Root-path alias some MCP clients probe; rejected identically."""
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=_REGISTRATION_DISABLED)


@router.post("/agent-token", include_in_schema=False, dependencies=[Depends(require_agent_sync_token)])
async def agent_token(
    current: CurrentHubUser = Depends(get_current_hub_user),
    model_key: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db_v2),
) -> dict[str, object]:
    """Exchange a valid Hub session for a short-lived identity-only MCP token.

    ``model_key`` is bounded audit metadata from the Agent's server-selected
    model policy. It is never an authorization or provider-selection input.
    """
    agent_client_id = get_settings().MCP_OAUTH_AGENT_CLIENT_ID
    decision = admit_client(
        active_settings=_active_bot_settings(db), client_id=agent_client_id,
        approved=load_approved_clients(get_settings()),
    )
    if not decision.allowed:
        # Hub AI is switched off: the Hub Agent receives no MCP token.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hub AI is disabled.")
    normalized_model_key = (model_key or "").strip().lower() or None
    if normalized_model_key and not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", normalized_model_key):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid MCP telemetry model key.")
    try:
        return _service().issue_agent_delegated_token(
            email=current.email,
            subject=str(current.hub_user_id),
            client_id=agent_client_id,
            model_key=normalized_model_key,
        )
    except McpOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP delegated authorization is unavailable.") from exc


@metadata_router.get("/.well-known/oauth-authorization-server")
@metadata_router.get("/.well-known/oauth-authorization-server/mcp/oauth", include_in_schema=False)
@metadata_router.get("/.well-known/openid-configuration/mcp/oauth", include_in_schema=False)
@metadata_router.get("/mcp/oauth/.well-known/openid-configuration", include_in_schema=False)
async def authorization_server_metadata():
    """OAuth discovery, including issuer-path aliases used by MCP clients."""
    service = _service()
    metadata = {
        "issuer": service.issuer,
        "authorization_endpoint": f"{service.issuer}/authorize",
        "token_endpoint": f"{service.issuer}/token",
        "jwks_uri": f"{service.issuer}/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "scopes_supported": ["renphil.mcp.read"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        # RFC 9449 §5.1: clients that can sign proofs receive DPoP-bound tokens.
        "dpop_signing_alg_values_supported": list(DPOP_SIGNING_ALGORITHMS),
    }
    # No registration_endpoint: dynamic client registration is disabled.
    return metadata


@router.get("/jwks.json")
async def jwks():
    """Publish only the Hub OAuth public signing key for MCP verification."""
    return _service().jwks()
