"""Hub OAuth authorization-code routes for approved MCP clients."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import logging
import re
from functools import lru_cache
from typing import Annotated
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.services.mcp_access_policy import PROVIDER_LABELS, AdmissionDecision, admit_client, bearer_providers, load_approved_clients
from app.services.mcp_oauth_dpop import DPOP_SIGNING_ALGORITHMS, McpDPoPError, verify_token_request_proof
from app.services.mcp_oauth_keys import HubMcpSigningKey, McpOAuthKeyError, parse_additional_public_keys
from app.services.mcp_oauth_service import (
    LocalMcpOAuthService,
    McpOAuthDPoPBindingError,
    McpOAuthError,
    McpOAuthInvalidGrant,
    McpOAuthStorageError,
    PersistentMcpOAuthService,
    authenticate_client,
)
from app.services import mcp_oauth_dpop_replay as dpop_replay
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.agent_identity import require_agent_sync_token


logger = logging.getLogger(__name__)

# Abuse protection applies ONLY to these public MCP OAuth paths. Hub Google
# login (``/auth/*``), session creation, access checks and every other Hub
# route use the default route class and are never limited here.
RATE_LIMITED_ENDPOINTS: dict[str, str] = {
    "/mcp/oauth/authorize": "authorize",
    "/mcp/oauth/consent": "consent",
    "/mcp/oauth/revoke": "revoke",
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


# RFC 6749 §5.2: OAuth errors are a top-level JSON object, never cached.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _oauth_error(status_code: int, body: dict, retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=body,
        headers={"Retry-After": str(int(retry_after)), **_NO_STORE},
    )


class OAuthErrorResponse(HTTPException):
    """An RFC 6749 error raised by an authorization-server endpoint.

    It is still an ``HTTPException`` (status and a dict ``detail``), so code
    that only inspects the status keeps working. ``McpOAuthRateLimitedRoute``
    renders it as the top-level body ``{"error", "error_description"?}``
    with ``Cache-Control: no-store`` and ``Pragma: no-cache`` instead of
    FastAPI's ``{"detail": ...}`` wrapper.
    """

    def __init__(self, status_code: int, error: str, description: str | None = None, *, headers: dict[str, str] | None = None):
        body = {"error": error}
        if description:
            body["error_description"] = description
        super().__init__(status_code=status_code, detail=body, headers=headers)
        self.body = body


def _oauth_error_response(exc: OAuthErrorResponse) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body, headers={**(exc.headers or {}), **_NO_STORE})


def _with_oauth_errors(handler):
    async def render_oauth_errors(request: Request):
        try:
            return await handler(request)
        except OAuthErrorResponse as exc:
            return _oauth_error_response(exc)

    return render_oauth_errors


# RFC 6749 §5.1: every token-endpoint response, success included, is
# marked non-cacheable. Bodies are unchanged.
NO_STORE_PATHS = frozenset({"/mcp/oauth/token"})


def _with_no_store(handler):
    async def no_store(request: Request):
        response = await handler(request)
        response.headers.update(_NO_STORE)
        return response

    return no_store


def _bad_request(error: str, description: str | None = None) -> OAuthErrorResponse:
    return OAuthErrorResponse(status.HTTP_400_BAD_REQUEST, error, description)


def _temporarily_unavailable() -> OAuthErrorResponse:
    return OAuthErrorResponse(
        status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE["error"], _UNAVAILABLE["error_description"],
        headers={"Retry-After": str(rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS)},
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


def _basic_client_id(authorization: str | None) -> str | None:
    """Client ID from a well-formed ``Basic`` header (RFC 6749 §2.3.1), for
    bucketing only; authentication happens in the handler. Never raises."""
    scheme, _, value = (authorization or "").strip().partition(" ")
    if scheme.lower() != "basic" or not value.strip() or len(value) > 4096:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    raw_id, separator, _secret = decoded.partition(":")
    client_id = unquote_plus(raw_id)
    return client_id if separator and client_id else None


async def _token_form_bucket(request: Request, settings) -> tuple[str | None, str]:
    """``(grant_type, client bucket)`` of a token request; never raises."""
    try:
        form = await request.form()
        grant_type = form.get("grant_type")
    except Exception:
        return None, "unknown"
    return (grant_type if isinstance(grant_type, str) else None), await _token_client_bucket(request, settings)


async def _token_client_bucket(request: Request, settings) -> str:
    try:
        form = await request.form()
        client_id = form.get("client_id")
    except Exception:
        return "unknown"
    if not client_id:
        # client_secret_basic clients send their ID only in the header
        # (security review S4); otherwise they would share "unknown".
        client_id = _basic_client_id(request.headers.get("authorization"))
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
        original = _with_oauth_errors(super().get_route_handler())
        if self.path in NO_STORE_PATHS:
            original = _with_no_store(original)
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
                    grant_type, client = await _token_form_bucket(request, settings)
                    refresh_grant = grant_type == "refresh_token"
                    if refresh_grant:
                        # Own budget (per network, per client, global) and no
                        # part in the code-exchange failure lockout (S5).
                        decision = await rate_limit.evaluate(
                            store, rate_limit.build_checks(settings, "refresh", network=network, client=client), "refresh",
                        )
                    else:
                        decision = await rate_limit.evaluate(
                            store, rate_limit.build_checks(settings, endpoint, network=network, client=None, phase="pre"), endpoint,
                        )
                        if decision.allowed:
                            decision = await rate_limit.evaluate(
                                store, rate_limit.build_checks(settings, endpoint, network=network, client=client, phase="client"), endpoint,
                            )
                elif endpoint in {"consent", "revoke"}:
                    # Part of the same browser sign-in: counted against the
                    # authorize per-network budget only, so floods of unknown
                    # clients can never exhaust a shared consent bucket.
                    decision = await rate_limit.evaluate(
                        store, rate_limit.build_checks(settings, "authorize", network=network, client=None, phase="pre"), "authorize",
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
            except rate_limit.RateLimitStoreUnavailable:
                # The DPoP replay reservation shares the limiter's store and
                # its fail-closed posture.
                logger.warning("MCP OAuth request denied: endpoint=token category=dpop_replay_store_unavailable")
                return _oauth_error(503, _UNAVAILABLE, rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS)
            except Exception:
                if not refresh_grant:
                    await _record_token_failure(store, settings, network)
                raise
            if response.status_code >= 400 and not refresh_grant:
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
            signing_key=HubMcpSigningKey(
                private_pem=private_key, key_id=key_id,
                # Published in the JWKS for rotation overlap; never used to sign.
                additional_public_keys=parse_additional_public_keys(
                    getattr(settings, "MCP_OAUTH_SIGNING_PUBLIC_KEYS_NEXT", ""), current_kid=key_id,
                ),
            ),
            code_ttl_seconds=settings.MCP_OAUTH_CODE_TTL_SECONDS,
            access_token_ttl_seconds=settings.MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS,
            refresh_tokens_enabled=settings.MCP_OAUTH_REFRESH_TOKENS_ENABLED,
            refresh_token_idle_seconds=settings.MCP_OAUTH_REFRESH_TOKEN_IDLE_SECONDS,
            refresh_family_max_seconds=settings.MCP_OAUTH_REFRESH_FAMILY_MAX_SECONDS,
        )
    except (McpOAuthError, McpOAuthKeyError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP OAuth is not configured.") from exc


_CLIENT_AUTH_CHALLENGE = 'Basic realm="renphil-mcp-oauth"'


def _invalid_client() -> OAuthErrorResponse:
    return OAuthErrorResponse(
        status.HTTP_401_UNAUTHORIZED, "invalid_client", "Client authentication failed.",
        headers={"WWW-Authenticate": _CLIENT_AUTH_CHALLENGE},
    )


def _invalid_token_request() -> OAuthErrorResponse:
    return _bad_request("invalid_request", "The token request is missing a required parameter or is malformed.")


def _oauth_service() -> PersistentMcpOAuthService:
    """``_service()`` for OAuth endpoints: an unconfigured issuer is the
    RFC 6749 ``temporarily_unavailable`` error; disabled stays a plain 404."""
    try:
        return _service()
    except HTTPException as exc:
        if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            raise _temporarily_unavailable() from exc
        raise


def _token_client_credentials(
    authorization: str | None, form_client_id: str | None, form_client_secret: str | None,
) -> tuple[str, str | None]:
    """Resolve ``client_secret_basic`` / ``client_secret_post`` / ``none``.

    RFC 6749 §2.3: a client uses exactly one authentication method. Basic
    credentials are form-urlencoded before base64 (§2.3.1). Malformed
    credentials are ``invalid_client``; nothing is logged or echoed.
    """
    scheme, _, value = (authorization or "").strip().partition(" ")
    if scheme.lower() == "basic":
        if form_client_secret is not None:
            raise _invalid_client()  # two authentication methods
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise _invalid_client() from None
        raw_id, separator, raw_secret = decoded.partition(":")
        basic_id, basic_secret = unquote_plus(raw_id), unquote_plus(raw_secret)
        if not separator or not basic_id or not basic_secret:
            raise _invalid_client()
        if form_client_id is not None and not hmac.compare_digest(form_client_id.encode("utf-8"), basic_id.encode("utf-8")):
            raise _invalid_client()
        return basic_id, basic_secret
    if not isinstance(form_client_id, str) or not form_client_id:
        raise _invalid_token_request()
    if form_client_secret is not None and not form_client_secret:
        raise _invalid_client()
    return form_client_id, form_client_secret


_INVALID_TARGET_ARGS = {
    "error": "invalid_target",
    "description": "The requested resource is not served by this authorization server.",
}


def _normalized_resource(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return text[:-1] if text.endswith("/") else text


def _require_valid_resource(resources: list[str] | None) -> None:
    """RFC 8707: every ``resource`` sent must be the configured MCP resource.

    Absent ``resource`` keeps the previous behaviour. Comparison is exact
    after removing one trailing slash; no case folding or other rewriting.
    """
    if resources is None:
        return
    configured = _normalized_resource(getattr(get_settings(), "MCP_OAUTH_RESOURCE_URL", None) or "")
    if not configured or not resources:
        raise _bad_request(**_INVALID_TARGET_ARGS)
    for value in resources:
        supplied = _normalized_resource(value)
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8")):
            raise _bad_request(**_INVALID_TARGET_ARGS)


def _redirect_with_params(redirect_uri: str, params: tuple[tuple[str, str], ...]) -> str:
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _redirect_with_code(*, redirect_uri: str, code: str, state: str) -> str:
    return _redirect_with_params(redirect_uri, (("code", code), ("state", state)))


def _continuation_request_id(issuer: str, continuation_uri: str) -> str | None:
    prefix = f"{issuer}/continue/" if issuer else ""
    if not prefix or not isinstance(continuation_uri, str) or not continuation_uri.startswith(prefix):
        return None
    request_id = continuation_uri.removeprefix(prefix)
    if not request_id or "/" in request_id or "?" in request_id or "#" in request_id:
        return None
    return request_id


def continuation_browser_bound(cookies, continuation_uri: str) -> bool:
    """True when this browser started the authorization behind ``continuation_uri``.

    Used by /auth/login before sending the browser to Google, so a victim
    lured onto an attacker's continuation is refused before signing in.
    Always true when consent (and with it browser binding) is disabled.
    """
    if not _consent_required():
        return True
    issuer = (get_settings().MCP_OAUTH_ISSUER or "").rstrip("/")
    request_id = _continuation_request_id(issuer, continuation_uri)
    return request_id is not None and _browser_bound(cookies, request_id, issuer)


def complete_hub_login(*, db: Session, continuation_uri: str, email: str, subject: str, cookies=None) -> str | None:
    """Convert a validated Hub sign-in into an external OAuth code.

    Returning ``None`` preserves the normal Hub login flow. No Hub session JWT
    is placed in an internal callback URL or sent to the external MCP client.
    ``cookies`` are the callback request's cookies: with consent required,
    the sign-in must happen in the browser that started /authorize.
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
    if _consent_required():
        # The sign-in must come from the browser that started /authorize
        # (security review S1); otherwise nothing is parked.
        if not _browser_bound(cookies, request_id, service.issuer):
            raise McpOAuthError("OAuth authorization request is invalid or expired.")
        # Park the verified identity until the user approves on Hub's own
        # consent page (bound to the browser that started /authorize).
        service.record_login_for_consent(
            db, request_id=request_id, email=email, subject=subject,
            pending_hash=_consent_mac(_consent_key(), "pending-code", request_id),
        )
        return f"{service.issuer}/consent?{urlencode({'request_id': request_id})}"
    code, redirect_uri, client_state = service.complete_authorization(
        db, request_id=request_id, email=email, subject=subject,
    )
    return _redirect_with_code(redirect_uri=redirect_uri, code=code, state=client_state)


# ── Consent and browser binding (MCP_OAUTH_CONSENT_REQUIRED) ──────────────
#
# /authorize sets a short-lived HttpOnly cookie bound to the authorization
# request. After Hub sign-in the user sees a server-rendered page naming the
# provider and scope; only a POST from the same browser (cookie) carrying the
# CSRF token derived from that cookie can approve or deny. The internal Agent
# token path never uses any of this.

# A host-only, Secure, Path=/ cookie (``__Host-``) so it reaches /auth/login,
# /auth/callback and the consent page and cannot be planted by a sibling
# domain. A loopback issuer (local development without TLS) uses the plain
# name without Secure.
CONSENT_COOKIE = "renphil_mcp_oauth_consent"
CONSENT_COOKIE_HOST = "__Host-renphil_mcp_oauth_consent"
CONSENT_COOKIE_MAX_AGE_SECONDS = 600
_CONSENT_KEY_LABEL = b"renphil-hub-mcp-oauth-consent-v1"
_SCOPE_DESCRIPTION = "Read RenPhil Hub information you are already allowed to see (renphil.mcp.read)."
_CONSENT_STYLE = (
    "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;padding:24px 16px;"
    "background:#f6f7f9;color:#1d2430}main{max-width:480px;margin:0 auto;background:#fff;"
    "border:1px solid #d9dde3;border-radius:8px;padding:24px}h1{font-size:1.25rem;margin:0 0 12px}"
    "p{line-height:1.5}.actions{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}"
    "form{margin:0}button{font:inherit;padding:10px 18px;border-radius:6px;border:1px solid #1d2430;"
    "cursor:pointer;background:#fff;color:#1d2430}button.approve{background:#1d2430;color:#fff}"
    "button:focus-visible{outline:3px solid #2f6fdf;outline-offset:2px}"
)
_CONSENT_STYLE_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_CONSENT_STYLE.encode("utf-8")).digest()).decode("ascii")


def _consent_required() -> bool:
    """Default ON whenever MCP OAuth is enabled; only an explicit false disables."""
    return getattr(get_settings(), "MCP_OAUTH_CONSENT_REQUIRED", True) is not False


def _consent_key() -> bytes:
    secret = str(getattr(get_settings(), "JWT_SECRET_KEY", "") or "")
    if not secret:
        raise McpOAuthError("OAuth consent is unavailable.")
    return hmac.new(secret.encode("utf-8"), _CONSENT_KEY_LABEL, hashlib.sha256).digest()


def _consent_mac(key: bytes, label: str, value: str) -> str:
    return hmac.new(key, f"{label}\x00{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _same(left: object, right: str) -> bool:
    return isinstance(left, str) and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _consent_cookie_name(issuer: str) -> str:
    return CONSENT_COOKIE if rate_limit.is_local_issuer(issuer) else CONSENT_COOKIE_HOST


def _browser_bound(cookies, request_id: str, issuer: str) -> bool:
    try:
        binding = _consent_mac(_consent_key(), "binding", request_id)
    except McpOAuthError:
        return False
    return _same((cookies or {}).get(_consent_cookie_name(issuer)), binding)


def _set_consent_cookie(response, *, issuer: str, request_id: str) -> None:
    response.set_cookie(
        _consent_cookie_name(issuer), _consent_mac(_consent_key(), "binding", request_id),
        max_age=CONSENT_COOKIE_MAX_AGE_SECONDS, path="/",
        secure=not rate_limit.is_local_issuer(issuer), httponly=True, samesite="lax",
    )


def _clear_consent_cookie(response, *, issuer: str) -> None:
    response.delete_cookie(
        _consent_cookie_name(issuer), path="/",
        secure=not rate_limit.is_local_issuer(issuer), httponly=True, samesite="lax",
    )


def _consent_headers() -> dict[str, str]:
    return {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src '{_CONSENT_STYLE_HASH}'; frame-ancestors 'none'; base-uri 'none'"
        ),
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _consent_html(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_CONSENT_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return HTMLResponse(page, status_code=status_code, headers=_consent_headers())


def _consent_problem(status_code: int = status.HTTP_400_BAD_REQUEST) -> HTMLResponse:
    # Never redirects: an unverified request must not reach the client.
    return _consent_html(
        "Authorization unavailable",
        "<h1>This authorization request is invalid or has expired</h1>"
        "<p>Return to the application and start connecting again.</p>",
        status_code=status_code,
    )


def _verified_consent(request: Request, request_id: str, issuer: str):
    """``(key, binding, pending_hash)`` when this browser started ``request_id``."""
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        return None
    try:
        key = _consent_key()
    except McpOAuthError:
        return None
    binding = _consent_mac(key, "binding", request_id)
    if not _same(request.cookies.get(_consent_cookie_name(issuer)), binding):
        return None
    return key, binding, _consent_mac(key, "pending-code", request_id)


# Handlers that use the synchronous SQLAlchemy session are plain ``def`` so
# FastAPI runs them in its threadpool instead of blocking the event loop.
@router.get("/authorize")
def authorize(
    response_type: Annotated[str | None, Query()] = None,
    client_id: Annotated[str | None, Query()] = None,
    redirect_uri: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    code_challenge: Annotated[str | None, Query()] = None,
    code_challenge_method: Annotated[str | None, Query()] = None,
    db: Session = Depends(get_db_v2),
    resource: Annotated[list[str] | None, Query()] = None,
):
    """Begin authorization, then reuse normal Hub Google sign-in.

    Every error here is returned to the browser as an RFC 6749 JSON error and
    never redirected: the client and its exact callback are not yet trusted
    (§4.1.2.1), so nothing is sent to an unverified redirect URI.
    """
    invalid_request = "The authorization request is missing a required parameter or is malformed."
    if not response_type:
        raise _bad_request("invalid_request", invalid_request)
    if response_type != "code":
        raise _bad_request("unsupported_response_type", "Only response_type=code is supported.")
    if not all((client_id, redirect_uri, state, code_challenge, code_challenge_method)):
        raise _bad_request("invalid_request", invalid_request)
    if _is_agent_client(client_id):
        raise _bad_request("unauthorized_client", "This client may not use the authorization endpoint.")
    _require_valid_resource(resource)
    if not _external_client_is_enabled(db, client_id):
        raise OAuthErrorResponse(status.HTTP_403_FORBIDDEN, "access_denied", "External MCP access is not available for this client.")
    service = _oauth_service()
    try:
        request_id = service.start_authorization(db,
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        )
    except McpOAuthStorageError as exc:
        raise _temporarily_unavailable() from exc
    except McpOAuthError as exc:
        raise _bad_request("invalid_request", invalid_request) from exc
    # The existing Hub callback recognizes this continuation and converts the
    # verified login directly into an authorization code. This avoids a second
    # Google callback registration and never exposes a Hub session token.
    continuation = f"{service.issuer}/continue/{request_id}"
    response = RedirectResponse(url=f"/auth/login?{urlencode({'redirect_uri': continuation})}")
    if _consent_required():
        # Bind the request to this browser for the consent step.
        try:
            _set_consent_cookie(response, issuer=service.issuer, request_id=request_id)
        except McpOAuthError as exc:
            raise _temporarily_unavailable() from exc
    return response


@router.get("/consent", include_in_schema=False)
def consent_page(request: Request, request_id: str = Query(default=""), db: Session = Depends(get_db_v2)):
    """Server-rendered consent for a signed-in, browser-bound request."""
    if not _consent_required():
        raise _disabled()
    service = _service()
    verified = _verified_consent(request, request_id, service.issuer)
    if verified is None:
        return _consent_problem()
    _key, binding, pending_hash = verified
    try:
        pending = service.pending_consent(db, request_id=request_id, pending_hash=pending_hash)
    except McpOAuthError:
        return _consent_problem(status.HTTP_503_SERVICE_UNAVAILABLE)
    if pending is None:
        return _consent_problem()
    client_id, email = pending
    decision = _external_admission(db, client_id)
    if not decision.allowed or decision.client_kind not in PROVIDER_LABELS:
        return _consent_problem(status.HTTP_403_FORBIDDEN)
    label = html.escape(PROVIDER_LABELS[decision.client_kind])
    action = html.escape(f"{service.issuer}/consent", quote=True)
    hidden = (
        f'<input type="hidden" name="request_id" value="{html.escape(request_id, quote=True)}">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(_consent_mac(_key, "csrf", binding), quote=True)}">'
    )
    body = (
        f"<h1>Allow {label} to access RenPhil Hub?</h1>"
        f"<p>{label} is asking to connect to RenPhil Hub as <strong>{html.escape(email)}</strong>.</p>"
        f"<p>It will be able to: {html.escape(_SCOPE_DESCRIPTION)}</p>"
        f"<p>RenPhil Hub administrators can turn this access off at any time. "
        f"Disconnecting the connector in {label} revokes it.</p>"
        '<div class="actions">'
        f'<form method="post" action="{action}">{hidden}<input type="hidden" name="decision" value="approve">'
        f'<button type="submit" class="approve">Allow</button></form>'
        f'<form method="post" action="{action}">{hidden}<input type="hidden" name="decision" value="deny">'
        f'<button type="submit">Deny</button></form>'
        "</div>"
    )
    return _consent_html(f"Allow {PROVIDER_LABELS[decision.client_kind]} access", body)


@router.post("/consent", include_in_schema=False)
def consent_decision(
    request: Request,
    request_id: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    decision: Annotated[str, Form()] = "",
    db: Session = Depends(get_db_v2),
):
    """Approve or deny. Verifies cookie binding, CSRF token and request ID."""
    if not _consent_required():
        raise _disabled()
    service = _service()
    verified = _verified_consent(request, request_id, service.issuer)
    if verified is None or decision not in {"approve", "deny"}:
        return _consent_problem()
    key, binding, pending_hash = verified
    if not _same(csrf_token, _consent_mac(key, "csrf", binding)):
        return _consent_problem()
    try:
        if decision == "deny":
            redirect_uri, client_state = service.deny_consent(db, request_id=request_id, pending_hash=pending_hash)
            target = _redirect_with_params(redirect_uri, (("error", "access_denied"), ("state", client_state)))
        else:
            pending = service.pending_consent(db, request_id=request_id, pending_hash=pending_hash)
            if pending is None:
                return _consent_problem()
            if not _external_admission(db, pending[0]).allowed:
                # Switched off since sign-in: nothing is issued.
                return _consent_problem(status.HTTP_403_FORBIDDEN)
            code, redirect_uri, client_state = service.approve_consent(db, request_id=request_id, pending_hash=pending_hash)
            target = _redirect_with_code(redirect_uri=redirect_uri, code=code, state=client_state)
    except McpOAuthError:
        return _consent_problem()
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"})
    _clear_consent_cookie(response, issuer=service.issuer)
    return response


async def _reserve_token_dpop_proof(request: Request) -> None:
    """RFC 9449 §11.1: each verified token-request proof is accepted once.

    Runs before the (threadpool) token handler because the shared store is
    asynchronous. This is the only place the proof is verified (security
    review S7): a header that is present but does not verify is rejected
    here, and the verified thumbprint is stashed on ``request.state`` for the
    handler, so no second verification at a later clock reading can accept a
    proof that was never reserved. Only a proof that verifies is reserved.
    Store failures raise ``RateLimitStoreUnavailable``, which the route class
    turns into the limiter's 503.
    """
    request.state.mcp_dpop_jkt = None
    raw = request.headers.get("dpop")
    if raw is None:
        return
    if not raw.strip():
        raise _bad_request("invalid_dpop_proof", "A DPoP proof is required.")
    settings = get_settings()
    service = _oauth_service()
    try:
        proof = verify_token_request_proof(
            raw, token_endpoint=f"{service.issuer}/token", max_age_seconds=dpop_replay.PROOF_MAX_AGE_SECONDS,
        )
    except McpDPoPError:
        raise _bad_request("invalid_dpop_proof", "Invalid DPoP proof.") from None
    if not await dpop_replay.reserve_proof(_rate_limit_store(settings), settings, proof):
        raise _bad_request("invalid_dpop_proof", "Invalid DPoP proof.")
    request.state.mcp_dpop_jkt = proof.jkt


def _verified_dpop_jkt(request: Request | None, dpop: str | None, issuer: str) -> str | None:
    """Thumbprint of the proof verified and reserved by the dependency, or
    ``None`` when no DPoP header was sent.

    Only a direct call without a request (unit tests) verifies here.
    """
    if request is not None and hasattr(request.state, "mcp_dpop_jkt"):
        return request.state.mcp_dpop_jkt
    if dpop is None:
        return None
    if not dpop.strip():
        raise _bad_request("invalid_dpop_proof", "A DPoP proof is required.")
    try:
        return verify_token_request_proof(dpop, token_endpoint=f"{issuer}/token").jkt
    except McpDPoPError as exc:
        raise _bad_request("invalid_dpop_proof", "Invalid DPoP proof.") from exc


@router.post("/token", dependencies=[Depends(_reserve_token_dpop_proof)])
def token(
    grant_type: Annotated[str | None, Form()] = None,
    code: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    dpop: str | None = Header(default=None, alias="DPoP"),
    db: Session = Depends(get_db_v2),
    resource: Annotated[list[str] | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
    request: Request = None,  # type: ignore[assignment]  # always injected by FastAPI
):
    # Errors are RFC 6749 §5.2 bodies (rendered by the route class).
    if not grant_type:
        raise _invalid_token_request()
    refresh_grant = grant_type == "refresh_token" and _refresh_tokens_enabled()
    if grant_type != "authorization_code" and not refresh_grant:
        raise _bad_request("unsupported_grant_type", "This grant type is not supported.")
    client_id, client_secret = _token_client_credentials(authorization, client_id, client_secret)
    if _is_agent_client(client_id):
        raise _bad_request("unauthorized_client", "This client may not use the token endpoint.")
    _require_valid_resource(resource)
    decision = _external_admission(db, client_id)
    if not decision.allowed:
        if refresh_grant and isinstance(refresh_token, str):
            # External AI, the provider switch or the client mapping changed:
            # the presented family can never be used again.
            try:
                _service().revoke_refresh_family(db, refresh_token)
            except HTTPException:
                pass  # unconfigured service: nothing can have been issued
        raise OAuthErrorResponse(status.HTTP_403_FORBIDDEN, "unauthorized_client", "External MCP access is not available for this client.")
    service = _oauth_service()
    # Confidential clients (registry entry with client_secret_hash) must
    # authenticate; public clients must not send a secret (PKCE only). The
    # same authentication applies to both grants.
    if not authenticate_client(service.clients, client_id, client_secret):
        raise _invalid_client()
    unbound_allowed = decision.client_kind in bearer_providers(get_settings())
    dpop_jkt = _verified_dpop_jkt(request, dpop, service.issuer)
    if refresh_grant:
        return _refresh_grant(
            db, service, refresh_token=refresh_token, scope=scope, client_id=client_id,
            client_kind=decision.client_kind, dpop_jkt=dpop_jkt, unbound_allowed=unbound_allowed,
        )
    if not code or not redirect_uri or not code_verifier:
        raise _invalid_token_request()
    # RFC 9449 §5: external MCP tokens are bound to the client's DPoP key. A
    # missing or invalid proof is rejected without consuming the code. The
    # only bearer issuance is for a provider the operator listed in
    # MCP_OAUTH_BEARER_PROVIDERS that sent no DPoP header at all; a header
    # that is sent is always validated and binds the token (no downgrade).
    if dpop_jkt is None and not unbound_allowed:
        raise _bad_request("invalid_dpop_proof", "A DPoP proof is required.")
    try:
        return service.exchange_code(
            db, code=code, client_id=client_id, redirect_uri=redirect_uri, code_verifier=code_verifier,
            dpop_jkt=dpop_jkt, client_kind=decision.client_kind, allow_unbound=dpop_jkt is None,
        )
    except McpOAuthStorageError as exc:
        raise _temporarily_unavailable() from exc
    except McpOAuthError as exc:
        # Unknown, used or expired code, wrong redirect URI or PKCE verifier.
        raise _bad_request("invalid_grant", "The authorization code is invalid or expired.") from exc


def _refresh_tokens_enabled() -> bool:
    return getattr(get_settings(), "MCP_OAUTH_REFRESH_TOKENS_ENABLED", False) is True


def _invalid_grant() -> OAuthErrorResponse:
    return _bad_request("invalid_grant", "The refresh token is invalid or expired.")


def _hub_user_is_active(db: Session, subject: str, email: str) -> bool:
    """The refresh token's Hub user still exists and is not soft-deleted.

    Identity is re-derived server-side from the stored email; the stored
    subject must still be that user's id.
    """
    from app.services.user_db_service import find_user_by_email

    user = find_user_by_email(db, email)  # soft-deleted rows are invisible
    return user is not None and str(user.id) == str(subject) and not getattr(user, "is_deleted", False)


def _refresh_grant(db: Session, service, *, refresh_token, scope, client_id: str, client_kind, dpop_jkt: str | None, unbound_allowed: bool):
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _invalid_token_request()
    if scope is not None and scope.split() != ["renphil.mcp.read"]:
        raise _bad_request("invalid_scope", "Only renphil.mcp.read may be requested.")
    # The family decides whether the (already verified) proof is required
    # (DPoP-bound families) or forbidden (unbound families, S2).
    try:
        return service.refresh_access_token(
            db, refresh_token=refresh_token, client_id=client_id, client_kind=client_kind, dpop_jkt=dpop_jkt,
            allow_unbound=unbound_allowed, user_is_active=_hub_user_is_active,
        )
    except McpOAuthDPoPBindingError as exc:
        raise _bad_request("invalid_dpop_proof", "A DPoP proof for this refresh token is required.") from exc
    except McpOAuthInvalidGrant as exc:
        raise _invalid_grant() from exc
    except McpOAuthStorageError as exc:
        raise _temporarily_unavailable() from exc
    except McpOAuthError as exc:
        raise _invalid_token_request() from exc


@router.post("/revoke", include_in_schema=False)
def revoke(
    token: Annotated[str | None, Form()] = None,
    token_type_hint: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db_v2),
):
    """RFC 7009 token revocation (what a connector calls on disconnect).

    The client authenticates exactly as at /token. A refresh token issued to
    that client is revoked together with its whole family. The answer is 200
    for unknown, foreign or access tokens too (access tokens are short-lived
    JWTs and expire on their own), so the endpoint is not an oracle. It works
    while the provider is switched off, so users can always disconnect.
    """
    del token_type_hint  # a hint only; every token is looked up as a refresh token
    client_id, client_secret = _token_client_credentials(authorization, client_id, client_secret)
    service = _oauth_service()
    if not authenticate_client(service.clients, client_id, client_secret):
        raise _invalid_client()
    if not isinstance(token, str) or not token:
        raise _bad_request("invalid_request", "The token parameter is required.")
    try:
        service.revoke_refresh_token(db, token, client_id=client_id)
    except McpOAuthStorageError as exc:
        raise _temporarily_unavailable() from exc
    return Response(status_code=status.HTTP_200_OK, headers=_NO_STORE)


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
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=_REGISTRATION_DISABLED, headers=_NO_STORE)


@metadata_router.post("/register", include_in_schema=False)
async def register_dynamic_client_root_alias() -> JSONResponse:
    """Root-path alias some MCP clients probe; rejected identically."""
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=_REGISTRATION_DISABLED, headers=_NO_STORE)


_AGENT_TOKEN_KEY_LABEL = b"renphil-hub-mcp-oauth-agent-token-rate-limit-v1"
_AGENT_TOKEN_WINDOW_SECONDS = 60


async def _limit_agent_token(current: CurrentHubUser = Depends(get_current_hub_user)) -> None:
    """Per verified Hub user budget for delegated MCP tokens.

    Runs after the Agent sync-token check and Hub session verification, in
    the same shared store as the public MCP OAuth limiter and with the same
    posture: no store or a store failure is a 503 with Retry-After (a
    loopback issuer uses the local-only store). The key is a keyed HMAC of
    the user id; no identity is stored or logged. Disabled MCP OAuth never
    touches the store. The response shape stays this endpoint's own
    ``{"detail": ...}`` contract.
    """
    settings = get_settings()
    if not getattr(settings, "MCP_OAUTH_ENABLED", False):
        return
    unavailable = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP delegated authorization is unavailable.",
        headers={"Retry-After": str(rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS), "Cache-Control": "no-store"},
    )
    secret = str(getattr(settings, "JWT_SECRET_KEY", "") or "")
    store = _rate_limit_store(settings)
    if store is None or not secret:
        logger.warning("MCP OAuth request denied: endpoint=agent-token category=rate_limit_store_unavailable")
        raise unavailable
    key = hmac.new(secret.encode("utf-8"), _AGENT_TOKEN_KEY_LABEL, hashlib.sha256).digest()
    environment = hmac.new(key, ("issuer:" + str(getattr(settings, "MCP_OAUTH_ISSUER", "") or "")).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    user = hmac.new(key, f"user:{current.hub_user_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    limit = int(getattr(settings, "MCP_OAUTH_AGENT_TOKEN_RATE_LIMIT_PER_MINUTE", 120) or 120)
    check = rate_limit.Check(
        key=f"renphil:mcp-oauth:rl:v1:{environment}:agent-token:user:{user}", limit=limit, window=_AGENT_TOKEN_WINDOW_SECONDS,
    )
    try:
        decision = await rate_limit.evaluate(store, [check], "agent-token")
    except rate_limit.RateLimitStoreUnavailable:
        logger.warning("MCP OAuth request denied: endpoint=agent-token category=rate_limit_store_unavailable")
        raise unavailable from None
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many MCP delegation token requests. Retry later.",
            headers={"Retry-After": str(decision.retry_after_seconds), "Cache-Control": "no-store"},
        )


@router.post(
    "/agent-token", include_in_schema=False,
    # Order matters: the sync token is checked before anything is counted.
    dependencies=[Depends(require_agent_sync_token), Depends(_limit_agent_token)],
)
def agent_token(
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
        # refresh_token only when enabled (rotating, with reuse detection).
        "grant_types_supported": ["authorization_code", "refresh_token"] if service.refresh_tokens_enabled else ["authorization_code"],
        "scopes_supported": ["renphil.mcp.read"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": _token_endpoint_auth_methods(service),
        "revocation_endpoint": f"{service.issuer}/revoke",
        "revocation_endpoint_auth_methods_supported": _token_endpoint_auth_methods(service),
        # RFC 9449 §5.1: clients that can sign proofs receive DPoP-bound tokens.
        "dpop_signing_alg_values_supported": list(DPOP_SIGNING_ALGORITHMS),
    }
    # No registration_endpoint: dynamic client registration is disabled.
    return metadata


def _token_endpoint_auth_methods(service) -> list[str]:
    """Advertise ``none`` for public clients and the two secret methods only
    when at least one confidential client is registered."""
    agent = (getattr(get_settings(), "MCP_OAUTH_AGENT_CLIENT_ID", "") or "").strip()
    external = [client for client_id, client in service.clients.items() if client_id != agent]
    methods: list[str] = []
    if not external or any(client.client_secret_hash is None for client in external):
        methods.append("none")
    if any(client.client_secret_hash is not None for client in external):
        methods.extend(("client_secret_basic", "client_secret_post"))
    return methods


@router.get("/jwks.json")
async def jwks():
    """Publish only the Hub OAuth public signing key for MCP verification."""
    return _service().jwks()
