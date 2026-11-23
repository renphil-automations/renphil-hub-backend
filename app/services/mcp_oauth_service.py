"""Local OAuth authorization-code and PKCE foundation for the Hub MCP.

The existing Google sign-in remains the upstream identity provider. This module
does not grant access itself: it records a validated MCP authorization request,
waits for Hub login to establish the user, then issues a short-lived code.
Production must replace the in-memory store with a durable shared store before
``MCP_OAUTH_ENABLED`` is enabled.

Access tokens are identity-only and reusable until ``exp``.  External
clients (Claude, ChatGPT, Gemini) must send an RFC 9449 DPoP proof with the
token request; the issued token is sender-constrained through ``cnf.jkt`` and
returned with ``token_type`` ``DPoP``, and the MCP server then requires a
fresh proof per request. The only exception is a provider an operator lists
in ``MCP_OAUTH_BEARER_PROVIDERS`` (off by default) that sends no proof: it
receives an unbound ``Bearer`` token. A proof that is sent is always bound.

Every MCP access token carries ``renphil_client_kind`` (``hub_agent``,
``claude``, ``chatgpt`` or ``gemini``), assigned here from Hub's exact client
registry. Dynamic client registration is not supported in this release.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

import jwt
from sqlalchemy.orm import Session

from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2, McpOAuthRefreshTokenV2
from app.services.mcp_access_policy import (
    CLIENT_KIND_CLAIM,
    EXTERNAL_PROVIDERS,
    HUB_AGENT_CLIENT_KIND,
    is_wellformed_client_id,
)
from app.services.mcp_oauth_client_secret import ClientSecretHashError, parse_client_secret_hash, verify_client_secret
from app.services.mcp_oauth_keys import HubMcpSigningKey


class McpOAuthError(ValueError):
    """An OAuth request is invalid without disclosing sensitive state."""


class McpOAuthStorageError(McpOAuthError):
    """The shared OAuth store failed; a transient outage, not a bad request."""


class McpOAuthInvalidGrant(McpOAuthError):
    """RFC 6749 ``invalid_grant``: the refresh token cannot be used."""


class McpOAuthDPoPBindingError(McpOAuthError):
    """The request lacks the DPoP proof its refresh-token family requires."""


SCOPE = "renphil.mcp.read"
# Refresh-token lifetimes (seconds). Idle: how long one unused refresh token
# stays valid; family: absolute lifetime of a login's whole rotation chain.
REFRESH_IDLE_DEFAULT_SECONDS = 7 * 24 * 3600
REFRESH_FAMILY_DEFAULT_SECONDS = 30 * 24 * 3600
REFRESH_IDLE_BOUNDS = (300, 30 * 24 * 3600)
REFRESH_FAMILY_BOUNDS = (3600, 90 * 24 * 3600)
_MAX_REFRESH_TOKEN_CHARS = 512


def refresh_lifetimes(idle_seconds: int, family_seconds: int) -> tuple[int, int]:
    """Clamp to safe bounds; an idle lifetime never exceeds the family's."""
    family = max(REFRESH_FAMILY_BOUNDS[0], min(int(family_seconds), REFRESH_FAMILY_BOUNDS[1]))
    idle = max(REFRESH_IDLE_BOUNDS[0], min(int(idle_seconds), REFRESH_IDLE_BOUNDS[1], family))
    return idle, family


def _new_refresh_token() -> str:
    # 48 random bytes (>= 32 required); only its SHA-256 digest is stored.
    return secrets.token_urlsafe(48)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# RFC 7636 §4.1-4.2: an S256 code_challenge is BASE64URL(SHA256(verifier))
# without padding (always 43 characters); a code_verifier is 43-128
# unreserved characters. Explicit ASCII classes and fullmatch(): no Unicode
# digits and no trailing newline slip through.
_S256_CODE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_CODE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")


def _is_s256_code_challenge(value: object) -> bool:
    return isinstance(value, str) and _S256_CODE_CHALLENGE.fullmatch(value) is not None


def _is_code_verifier(value: object) -> bool:
    return isinstance(value, str) and _CODE_VERIFIER.fullmatch(value) is not None


@dataclass(frozen=True)
class McpOAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    # PBKDF2 digest of a confidential client's secret (never the secret).
    # ``None``: a public client that authenticates with PKCE only.
    client_secret_hash: str | None = None

    @property
    def is_confidential(self) -> bool:
        return self.client_secret_hash is not None


def authenticate_client(clients: dict[str, McpOAuthClient], client_id: str, client_secret: str | None) -> bool:
    """RFC 6749 §2.3.1 client authentication at the token endpoint.

    A client registered with a secret hash MUST present the matching secret
    (constant-time comparison). A public client must not present one; it is
    authenticated by PKCE alone.
    """
    client = clients.get(client_id) if isinstance(client_id, str) else None
    if client is None:
        return False
    if client.client_secret_hash is not None:
        return verify_client_secret(client_secret, client.client_secret_hash)
    return client_secret is None


@dataclass
class _PendingAuthorization:
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    created_at: int


@dataclass
class _AuthorizationCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    email: str
    subject: str
    created_at: int
    consumed: bool = False


@dataclass
class _RefreshRecord:
    family_id: str
    client_id: str
    email: str
    subject: str
    jkt: str | None
    created_at: float
    expires_at: float
    family_expires_at: float
    used_at: float | None = None
    revoked_at: float | None = None


def _token_response(token: str, *, ttl_seconds: int, dpop_jkt: str | None, refresh_token: str | None = None) -> dict[str, object]:
    response: dict[str, object] = {
        "access_token": token,
        # RFC 9449 §5: a DPoP-bound token is presented with the DPoP scheme.
        "token_type": "DPoP" if dpop_jkt else "Bearer",
        "expires_in": ttl_seconds,
        "scope": SCOPE,
    }
    if refresh_token is not None:
        response["refresh_token"] = refresh_token
    return response


def _confirmation(dpop_jkt: str | None, *, allow_unbound: bool = False) -> dict[str, object]:
    """Bind the token to the DPoP key; unbound only when explicitly allowed.

    ``allow_unbound`` is set by the token route solely for a provider listed
    in ``MCP_OAUTH_BEARER_PROVIDERS`` that sent no DPoP proof. A supplied
    thumbprint is always bound (never downgraded).
    """
    if allow_unbound and dpop_jkt is None:
        return {}
    if not isinstance(dpop_jkt, str) or not dpop_jkt.strip() or len(dpop_jkt) > 128:
        raise McpOAuthError("OAuth DPoP key binding is required.")
    return {"cnf": {"jkt": dpop_jkt.strip()}}


def _refresh_confirmation(family_jkt: str | None, dpop_jkt: str | None, *, allow_unbound: bool) -> tuple[dict[str, object], str | None]:
    """Access-token binding for a refresh.

    A DPoP-bound family needs a proof for the same key. An unbound family
    only ever issues unbound tokens (security review S2): a proof on its
    refresh is ``invalid_grant`` so a stolen bearer refresh token can never be
    upgraded to a token bound to the thief's key. Callers check unbound
    families with ``_unbound_family_refusal`` first.
    """
    if family_jkt:
        if not isinstance(dpop_jkt, str) or not hmac.compare_digest(dpop_jkt.encode("utf-8"), family_jkt.encode("utf-8")):
            raise McpOAuthDPoPBindingError("A DPoP proof for the refresh token's key is required.")
        return {"cnf": {"jkt": family_jkt}}, family_jkt
    if dpop_jkt is None and allow_unbound:
        return {}, None
    raise McpOAuthInvalidGrant("The refresh token is invalid.")


def _unbound_family_refusal(family_jkt: str | None, dpop_jkt: str | None, *, allow_unbound: bool) -> str | None:
    """For an unbound family: ``"refuse"`` (invalid_grant, nothing consumed)
    when a DPoP proof is presented, ``"revoke"`` (invalid_grant and revoke the
    family) when its provider no longer may receive unbound tokens."""
    if family_jkt:
        return None
    if dpop_jkt is not None:
        return "refuse"
    if not allow_unbound:
        return "revoke"
    return None


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes for timezone columns; treat as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _external_kind(client_kind: str | None) -> str:
    if not isinstance(client_kind, str) or client_kind not in EXTERNAL_PROVIDERS:
        raise McpOAuthError("OAuth client is not an approved MCP provider.")
    return client_kind


class LocalMcpOAuthService:
    """Thread-safe local store. It is deliberately rejected for shared use."""

    def __init__(self, *, clients: dict[str, McpOAuthClient], issuer: str, audience: str, signing_secret: str, code_ttl_seconds: int = 120, access_token_ttl_seconds: int = 300,
                 refresh_tokens_enabled: bool = False, refresh_token_idle_seconds: int = REFRESH_IDLE_DEFAULT_SECONDS,
                 refresh_family_max_seconds: int = REFRESH_FAMILY_DEFAULT_SECONDS):
        if not issuer.startswith("http://") and not issuer.startswith("https://"):
            raise McpOAuthError("MCP OAuth issuer must be an absolute URL.")
        if len(signing_secret.encode("utf-8")) < 32:
            raise McpOAuthError("Local MCP OAuth signing secret must be at least 32 bytes.")
        self.clients = clients
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._signing_secret = signing_secret
        self.code_ttl_seconds = max(30, min(int(code_ttl_seconds), 600))
        self.access_token_ttl_seconds = max(60, min(int(access_token_ttl_seconds), 3600))
        self._pending: dict[str, _PendingAuthorization] = {}
        self._codes: dict[str, _AuthorizationCode] = {}
        self.refresh_tokens_enabled = bool(refresh_tokens_enabled)
        self.refresh_token_idle_seconds, self.refresh_family_max_seconds = refresh_lifetimes(
            refresh_token_idle_seconds, refresh_family_max_seconds,
        )
        self._refresh: dict[str, _RefreshRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def parse_clients(raw: str) -> dict[str, McpOAuthClient]:
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise McpOAuthError("MCP OAuth client registry is invalid.") from exc
        if not isinstance(data, dict):
            raise McpOAuthError("MCP OAuth client registry is invalid.")
        clients: dict[str, McpOAuthClient] = {}
        for client_id, item in data.items():
            if not is_wellformed_client_id(client_id) or not isinstance(item, dict):
                raise McpOAuthError("MCP OAuth client registry is invalid.")
            uris = item.get("redirect_uris")
            if not isinstance(uris, list) or not uris or any(not isinstance(uri, str) for uri in uris):
                raise McpOAuthError("Each MCP OAuth client needs exact redirect URIs.")
            normalized = tuple(uri.strip() for uri in uris)
            if any(not LocalMcpOAuthService._safe_redirect(uri) for uri in normalized):
                raise McpOAuthError("MCP OAuth redirect URI is invalid.")
            secret_hash = item.get("client_secret_hash")
            if secret_hash is not None:
                try:
                    parse_client_secret_hash(secret_hash)
                except ClientSecretHashError as exc:
                    raise McpOAuthError("MCP OAuth client secret hash is invalid.") from exc
            clients[client_id] = McpOAuthClient(client_id=client_id, redirect_uris=normalized, client_secret_hash=secret_hash)
        return clients

    @staticmethod
    def _safe_redirect(uri: str) -> bool:
        parsed = urlparse(uri)
        # Only local loopback callbacks may use HTTP. Production client
        # callbacks must use HTTPS and are compared byte-for-byte.
        if "*" in uri:
            return False
        return bool(parsed.scheme == "https" and parsed.netloc) or (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        )

    def start_authorization(self, *, client_id: str, redirect_uri: str, state: str, code_challenge: str, code_challenge_method: str) -> str:
        client = self.clients.get(client_id)
        if client is None or redirect_uri not in client.redirect_uris:
            raise McpOAuthError("Unknown OAuth client or redirect URI.")
        if not state or len(state) > 2048 or not code_challenge or len(code_challenge) < 43 or len(code_challenge) > 128:
            raise McpOAuthError("OAuth authorization request is invalid.")
        if code_challenge_method != "S256":
            raise McpOAuthError("OAuth PKCE S256 is required.")
        if not _is_s256_code_challenge(code_challenge):
            raise McpOAuthError("OAuth authorization request is invalid.")
        request_id = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._pending[request_id] = _PendingAuthorization(client_id, redirect_uri, state, code_challenge, int(time.time()))
        return request_id

    def complete_authorization(self, *, request_id: str, email: str, subject: str) -> tuple[str, str, str]:
        with self._lock:
            self._purge()
            request = self._pending.pop(request_id, None)
            if request is None:
                raise McpOAuthError("OAuth authorization request is invalid or expired.")
            code = secrets.token_urlsafe(48)
            self._codes[_sha256(code)] = _AuthorizationCode(
                request.client_id, request.redirect_uri, request.code_challenge,
                email.strip().lower(), subject, int(time.time()),
            )
        return code, request.redirect_uri, request.state

    def exchange_code(self, *, code: str, client_id: str, redirect_uri: str, code_verifier: str, dpop_jkt: str | None = None, client_kind: str | None = None, allow_unbound: bool = False) -> dict[str, object]:
        if not code or not client_id or not redirect_uri or not code_verifier:
            raise McpOAuthError("OAuth token request is invalid.")
        # Checked before any code lookup, so a malformed verifier never
        # consumes the code (same as a wrong one).
        if not _is_code_verifier(code_verifier):
            raise McpOAuthError("OAuth token request is invalid.")
        # Validate the key binding and provider before the code is consumed.
        confirmation = _confirmation(dpop_jkt, allow_unbound=allow_unbound)
        kind = _external_kind(client_kind)
        with self._lock:
            self._purge()
            record = self._codes.get(_sha256(code))
            if record is None or record.consumed or record.client_id != client_id or record.redirect_uri != redirect_uri:
                raise McpOAuthError("OAuth authorization code is invalid or already used.")
            calculated = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
            if not hmac.compare_digest(calculated, record.code_challenge):
                raise McpOAuthError("OAuth PKCE verification failed.")
            refresh_token = _new_refresh_token() if self.refresh_tokens_enabled else None
            # Sign first: a signing failure leaves the code unconsumed.
            response = self._issue(subject=record.subject, email=record.email, client_id=client_id, kind=kind,
                                   confirmation=confirmation, dpop_jkt=dpop_jkt, refresh_token=refresh_token)
            if refresh_token is not None:
                self._new_family(token=refresh_token, client_id=client_id, email=record.email, subject=record.subject, jkt=dpop_jkt)
            record.consumed = True
        return response

    def _issue(self, *, subject: str, email: str, client_id: str, kind: str, confirmation: dict, dpop_jkt: str | None, refresh_token: str | None) -> dict[str, object]:
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": subject,
                "email": email,
                # Roles are resolved live by Hub before MCP retrieval.
                "client_id": client_id,
                "iat": now,
                "exp": now + self.access_token_ttl_seconds,
                "jti": secrets.token_urlsafe(24),
                "scope": SCOPE,
                CLIENT_KIND_CLAIM: kind,
                **confirmation,
            }, self._signing_secret, algorithm="HS256",
        )
        return _token_response(token, ttl_seconds=self.access_token_ttl_seconds, dpop_jkt=dpop_jkt, refresh_token=refresh_token)

    def _new_family(self, *, token: str, client_id: str, email: str, subject: str, jkt: str | None) -> str:
        """Start a refresh-token family (the caller holds ``self._lock``)."""
        now = time.time()
        self._refresh[_sha256(token)] = _RefreshRecord(
            family_id=secrets.token_hex(16), client_id=client_id, email=email, subject=subject, jkt=jkt,
            created_at=now, expires_at=now + self.refresh_token_idle_seconds,
            family_expires_at=now + self.refresh_family_max_seconds,
        )
        return token

    def _revoke_family(self, family_id: str, now: float) -> None:
        for item in self._refresh.values():
            if item.family_id == family_id and item.revoked_at is None:
                item.revoked_at = now

    def revoke_refresh_family(self, refresh_token: str) -> None:
        """Revoke the family of a presented refresh token (e.g. provider off)."""
        if not self.refresh_tokens_enabled or not isinstance(refresh_token, str) or len(refresh_token) > _MAX_REFRESH_TOKEN_CHARS:
            return
        with self._lock:
            record = self._refresh.get(_sha256(refresh_token))
            if record is not None:
                self._revoke_family(record.family_id, time.time())

    def refresh_access_token(
        self, *, refresh_token: str, client_id: str, client_kind: str | None, dpop_jkt: str | None = None,
        allow_unbound: bool = False, user_is_active: Callable[[str, str], bool] | None = None,
    ) -> dict[str, object]:
        """Rotate a refresh token with reuse detection (in-memory twin of the
        persistent service)."""
        if not self.refresh_tokens_enabled:
            raise McpOAuthInvalidGrant("Refresh tokens are not enabled.")
        if not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > _MAX_REFRESH_TOKEN_CHARS:
            raise McpOAuthInvalidGrant("The refresh token is invalid.")
        with self._lock:
            now = time.time()
            record = self._refresh.get(_sha256(refresh_token))
            if record is None:
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            if record.client_id != client_id or record.used_at is not None or record.revoked_at is not None:
                self._revoke_family(record.family_id, now)
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            if now >= record.expires_at or now >= record.family_expires_at:
                raise McpOAuthInvalidGrant("The refresh token has expired.")
            try:
                kind = _external_kind(client_kind)
            except McpOAuthError:
                kind = None
            if kind is None or (user_is_active is not None and not user_is_active(record.subject, record.email)):
                self._revoke_family(record.family_id, now)
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            refusal = _unbound_family_refusal(record.jkt, dpop_jkt, allow_unbound=allow_unbound)
            if refusal == "revoke":
                self._revoke_family(record.family_id, now)
            if refusal is not None:
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            # Raises before anything is consumed when the proof is missing.
            confirmation, bound_jkt = _refresh_confirmation(record.jkt, dpop_jkt, allow_unbound=allow_unbound)
            new_token = _new_refresh_token()
            # Sign first: a signing failure leaves the presented token usable.
            response = self._issue(subject=record.subject, email=record.email, client_id=client_id, kind=kind,
                                   confirmation=confirmation, dpop_jkt=bound_jkt, refresh_token=new_token)
            record.used_at = now
            self._refresh[_sha256(new_token)] = _RefreshRecord(
                family_id=record.family_id, client_id=record.client_id, email=record.email, subject=record.subject,
                jkt=record.jkt, created_at=now, expires_at=min(now + self.refresh_token_idle_seconds, record.family_expires_at),
                family_expires_at=record.family_expires_at,
            )
            self._purge_refresh(now)
        return response

    def _purge_refresh(self, now: float) -> None:
        # A used record is kept until it expires so its replay is detected.
        self._refresh = {key: value for key, value in self._refresh.items() if value.expires_at > now and value.family_expires_at > now}

    def _purge(self) -> None:
        cutoff = int(time.time()) - self.code_ttl_seconds
        self._pending = {key: value for key, value in self._pending.items() if value.created_at >= cutoff}
        self._codes = {key: value for key, value in self._codes.items() if value.created_at >= cutoff and not value.consumed}


class PersistentMcpOAuthService:
    """Shared authorization-code store for a deployed Hub OAuth issuer.

    Only SHA-256 digests of browser request IDs and authorization codes are
    stored.  Rows are short lived and code consumption is locked atomically,
    which prevents use on another Hub instance or a concurrent replay.
    """

    def __init__(self, *, clients: dict[str, McpOAuthClient], issuer: str, audience: str, signing_key: HubMcpSigningKey, code_ttl_seconds: int = 120, access_token_ttl_seconds: int = 300,
                 refresh_tokens_enabled: bool = False, refresh_token_idle_seconds: int = REFRESH_IDLE_DEFAULT_SECONDS,
                 refresh_family_max_seconds: int = REFRESH_FAMILY_DEFAULT_SECONDS):
        if not issuer.startswith("https://"):
            raise McpOAuthError("The deployed MCP OAuth issuer must use HTTPS.")
        self.clients = clients
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._signing_key = signing_key
        self.code_ttl_seconds = max(30, min(int(code_ttl_seconds), 600))
        self.access_token_ttl_seconds = max(60, min(int(access_token_ttl_seconds), 3600))
        # Off by default: when disabled the refresh-token table is never read
        # or written, so it need not exist.
        self.refresh_tokens_enabled = bool(refresh_tokens_enabled)
        self.refresh_token_idle_seconds, self.refresh_family_max_seconds = refresh_lifetimes(
            refresh_token_idle_seconds, refresh_family_max_seconds,
        )

    def _cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=self.code_ttl_seconds)

    def _purge(self, db: Session) -> None:
        cutoff = self._cutoff()
        db.query(McpOAuthAuthorizationRequestV2).filter(McpOAuthAuthorizationRequestV2.created_at < cutoff).delete(synchronize_session=False)
        db.query(McpOAuthAuthorizationCodeV2).filter(McpOAuthAuthorizationCodeV2.created_at < cutoff).delete(synchronize_session=False)

    def start_authorization(self, db: Session, *, client_id: str, redirect_uri: str, state: str, code_challenge: str, code_challenge_method: str) -> str:
        client = self.clients.get(client_id)
        if client is None or redirect_uri not in client.redirect_uris:
            raise McpOAuthError("Unknown OAuth client or redirect URI.")
        if not state or len(state) > 2048 or not code_challenge or len(code_challenge) < 43 or len(code_challenge) > 128 or code_challenge_method != "S256":
            raise McpOAuthError("OAuth authorization request is invalid.")
        if not _is_s256_code_challenge(code_challenge):
            raise McpOAuthError("OAuth authorization request is invalid.")
        request_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        try:
            self._purge(db)
            db.add(McpOAuthAuthorizationRequestV2(
                request_hash=_sha256(request_id), client_id=client_id, redirect_uri=redirect_uri,
                client_state=state, code_challenge=code_challenge, created_at=now,
            ))
            db.commit()
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth authorization storage is unavailable.") from exc
        return request_id

    def complete_authorization(self, db: Session, *, request_id: str, email: str, subject: str) -> tuple[str, str, str]:
        if not request_id:
            raise McpOAuthError("OAuth authorization request is invalid or expired.")
        now = datetime.now(timezone.utc)
        try:
            self._purge(db)
            request = db.query(McpOAuthAuthorizationRequestV2).filter(
                McpOAuthAuthorizationRequestV2.request_hash == _sha256(request_id)
            ).with_for_update().one_or_none()
            if request is None:
                db.rollback()
                raise McpOAuthError("OAuth authorization request is invalid or expired.")
            code = secrets.token_urlsafe(48)
            redirect_uri, client_state = request.redirect_uri, request.client_state
            db.delete(request)
            db.add(McpOAuthAuthorizationCodeV2(
                code_hash=_sha256(code), client_id=request.client_id, redirect_uri=redirect_uri,
                code_challenge=request.code_challenge, user_email=email.strip().lower(),
                user_subject=subject,
                # OAuth establishes identity only.  Authorization is resolved
                # live by the Hub backend for each MCP capability call.
                user_roles_json="[]",
                created_at=now,
            ))
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth authorization storage is unavailable.") from exc
        return code, redirect_uri, client_state

    # -- Consent (MCP_OAUTH_CONSENT_REQUIRED) --------------------------------
    #
    # After Hub sign-in the verified identity is parked in a *pending* code
    # row whose ``code_hash`` is a server-keyed HMAC of the request ID
    # (``pending_hash``, computed by the router). The token endpoint looks
    # codes up by plain SHA-256 of the presented code, so a pending row can
    # never be exchanged. Approval re-keys it to the SHA-256 of a fresh
    # random code; denial deletes it. No schema change is needed.

    def record_login_for_consent(self, db: Session, *, request_id: str, email: str, subject: str, pending_hash: str) -> None:
        if not request_id or not isinstance(pending_hash, str) or len(pending_hash) != 64:
            raise McpOAuthError("OAuth authorization request is invalid or expired.")
        now = datetime.now(timezone.utc)
        try:
            self._purge(db)
            request = db.query(McpOAuthAuthorizationRequestV2).filter(
                McpOAuthAuthorizationRequestV2.request_hash == _sha256(request_id)
            ).with_for_update().one_or_none()
            if request is None:
                db.rollback()
                raise McpOAuthError("OAuth authorization request is invalid or expired.")
            # A second sign-in for the same request replaces the first.
            db.query(McpOAuthAuthorizationCodeV2).filter(
                McpOAuthAuthorizationCodeV2.code_hash == pending_hash
            ).delete(synchronize_session=False)
            # The consent step gets a full code TTL from sign-in.
            request.created_at = now
            db.add(McpOAuthAuthorizationCodeV2(
                code_hash=pending_hash, client_id=request.client_id, redirect_uri=request.redirect_uri,
                code_challenge=request.code_challenge, user_email=email.strip().lower(),
                user_subject=subject, user_roles_json="[]", created_at=now,
            ))
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth authorization storage is unavailable.") from exc

    def pending_consent(self, db: Session, *, request_id: str, pending_hash: str) -> tuple[str, str] | None:
        """``(client_id, user_email)`` of a signed-in request awaiting consent."""
        if not request_id or not pending_hash:
            return None
        try:
            self._purge(db)
            db.commit()
            request = db.query(McpOAuthAuthorizationRequestV2).filter(
                McpOAuthAuthorizationRequestV2.request_hash == _sha256(request_id)
            ).one_or_none()
            pending = db.query(McpOAuthAuthorizationCodeV2).filter(
                McpOAuthAuthorizationCodeV2.code_hash == pending_hash
            ).one_or_none()
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth authorization storage is unavailable.") from exc
        if request is None or pending is None or pending.client_id != request.client_id:
            return None
        return request.client_id, pending.user_email

    def _locked_consent(self, db: Session, request_id: str, pending_hash: str):
        self._purge(db)
        request = db.query(McpOAuthAuthorizationRequestV2).filter(
            McpOAuthAuthorizationRequestV2.request_hash == _sha256(request_id)
        ).with_for_update().one_or_none()
        pending = db.query(McpOAuthAuthorizationCodeV2).filter(
            McpOAuthAuthorizationCodeV2.code_hash == pending_hash
        ).with_for_update().one_or_none()
        if request is None or pending is None or pending.client_id != request.client_id:
            db.rollback()
            raise McpOAuthError("OAuth authorization request is invalid or expired.")
        return request, pending

    def approve_consent(self, db: Session, *, request_id: str, pending_hash: str) -> tuple[str, str, str]:
        """Issue the one-time code for an approved request (single use)."""
        if not request_id or not pending_hash:
            raise McpOAuthError("OAuth authorization request is invalid or expired.")
        try:
            request, pending = self._locked_consent(db, request_id, pending_hash)
            code = secrets.token_urlsafe(48)
            redirect_uri, client_state = request.redirect_uri, request.client_state
            db.delete(request)
            pending.code_hash = _sha256(code)
            pending.created_at = datetime.now(timezone.utc)
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth authorization storage is unavailable.") from exc
        return code, redirect_uri, client_state

    def deny_consent(self, db: Session, *, request_id: str, pending_hash: str) -> tuple[str, str]:
        """Discard a denied request; returns ``(redirect_uri, state)``."""
        if not request_id or not pending_hash:
            raise McpOAuthError("OAuth authorization request is invalid or expired.")
        try:
            request, pending = self._locked_consent(db, request_id, pending_hash)
            redirect_uri, client_state = request.redirect_uri, request.client_state
            db.delete(pending)
            db.delete(request)
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth authorization storage is unavailable.") from exc
        return redirect_uri, client_state

    def exchange_code(self, db: Session, *, code: str, client_id: str, redirect_uri: str, code_verifier: str, dpop_jkt: str | None = None, client_kind: str | None = None, allow_unbound: bool = False) -> dict[str, object]:
        if not code or not client_id or not redirect_uri or not code_verifier:
            raise McpOAuthError("OAuth token request is invalid.")
        # Checked before any code lookup, so a malformed verifier never
        # consumes the code (same as a wrong one).
        if not _is_code_verifier(code_verifier):
            raise McpOAuthError("OAuth token request is invalid.")
        # Validate the key binding and provider before the code is consumed.
        confirmation = _confirmation(dpop_jkt, allow_unbound=allow_unbound)
        kind = _external_kind(client_kind)
        now = datetime.now(timezone.utc)
        try:
            self._purge(db)
            record = db.query(McpOAuthAuthorizationCodeV2).filter(
                McpOAuthAuthorizationCodeV2.code_hash == _sha256(code)
            ).with_for_update().one_or_none()
            calculated = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
            if record is None or record.client_id != client_id or record.redirect_uri != redirect_uri or not hmac.compare_digest(calculated, record.code_challenge):
                db.rollback()
                raise McpOAuthError("OAuth authorization code is invalid or already used.")
            # The code is single-use because its hash disappears atomically
            # under this row lock.  Retaining a consumed credential adds no
            # protection and creates needless user-linked retention.
            subject, email = record.user_subject, record.user_email
            db.delete(record)
            refresh_token = None
            if self.refresh_tokens_enabled:
                # Same transaction: the code is consumed only together with
                # the creation of its refresh-token family.
                self._purge_refresh(db, now)
                refresh_token = self._add_refresh_row(
                    db, family_id=secrets.token_hex(16), client_id=client_id, email=email, subject=subject,
                    jkt=dpop_jkt, now=now, family_expires_at=now + timedelta(seconds=self.refresh_family_max_seconds),
                )
            # Sign inside the transaction, before the commit that consumes the
            # code: a signing failure rolls back and the code stays usable.
            response = self._issue(subject=subject, email=email, client_id=client_id, kind=kind, confirmation=confirmation,
                                   dpop_jkt=dpop_jkt, refresh_token=refresh_token, now=now)
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth token issuance is unavailable.") from exc
        return response

    def _issue(self, *, subject: str, email: str, client_id: str, kind: str, confirmation: dict, dpop_jkt: str | None, refresh_token: str | None, now: datetime) -> dict[str, object]:
        issued_at = int(now.timestamp())
        token = self._signing_key.sign({
            "iss": self.issuer, "aud": self.audience, "sub": subject,
            "email": email, "client_id": client_id, "iat": issued_at,
            "exp": issued_at + self.access_token_ttl_seconds, "jti": secrets.token_urlsafe(24),
            "scope": SCOPE, CLIENT_KIND_CLAIM: kind, **confirmation,
        })
        return _token_response(token, ttl_seconds=self.access_token_ttl_seconds, dpop_jkt=dpop_jkt, refresh_token=refresh_token)

    def _add_refresh_row(self, db: Session, *, family_id: str, client_id: str, email: str, subject: str, jkt: str | None, now: datetime, family_expires_at: datetime) -> str:
        token = _new_refresh_token()
        db.add(McpOAuthRefreshTokenV2(
            family_id=family_id, token_hash=_sha256(token), client_id=client_id, user_email=email,
            user_subject=subject, jkt=jkt, scope=SCOPE, created_at=now,
            expires_at=min(now + timedelta(seconds=self.refresh_token_idle_seconds), family_expires_at),
            family_expires_at=family_expires_at,
        ))
        return token

    @staticmethod
    def _purge_refresh(db: Session, now: datetime) -> None:
        """Delete rows that can no longer be used (expired token or family).

        A used row is kept until it expires so that its replay is detected.
        """
        db.query(McpOAuthRefreshTokenV2).filter(
            (McpOAuthRefreshTokenV2.expires_at <= now) | (McpOAuthRefreshTokenV2.family_expires_at <= now)
        ).delete(synchronize_session=False)

    @staticmethod
    def _revoke_family(db: Session, family_id: str, now: datetime) -> None:
        db.query(McpOAuthRefreshTokenV2).filter(
            McpOAuthRefreshTokenV2.family_id == family_id, McpOAuthRefreshTokenV2.revoked_at.is_(None),
        ).update({McpOAuthRefreshTokenV2.revoked_at: now}, synchronize_session=False)

    def revoke_refresh_family(self, db: Session, refresh_token: str) -> None:
        """Revoke the family of a presented refresh token. Best effort."""
        if not self.refresh_tokens_enabled or not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > _MAX_REFRESH_TOKEN_CHARS:
            return
        try:
            row = db.query(McpOAuthRefreshTokenV2).filter(
                McpOAuthRefreshTokenV2.token_hash == _sha256(refresh_token)
            ).with_for_update().one_or_none()
            if row is not None:
                self._revoke_family(db, row.family_id, datetime.now(timezone.utc))
            db.commit()
        except Exception:
            db.rollback()

    def refresh_access_token(
        self, db: Session, *, refresh_token: str, client_id: str, client_kind: str | None, dpop_jkt: str | None = None,
        allow_unbound: bool = False, user_is_active: Callable[[Session, str, str], bool] | None = None,
    ) -> dict[str, object]:
        """Rotate a refresh token with reuse detection.

        The presented row is locked. A used or revoked token, or one presented
        by another client, revokes its whole family. The caller has already
        authenticated the client and checked the External AI / provider
        switches; this re-checks the approved provider kind and, through
        ``user_is_active``, that the Hub user still exists and is not
        soft-deleted (otherwise the family is revoked). A DPoP-bound family
        requires a proof for the same key; without it nothing is consumed.
        """
        if not self.refresh_tokens_enabled:
            raise McpOAuthInvalidGrant("Refresh tokens are not enabled.")
        if not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > _MAX_REFRESH_TOKEN_CHARS:
            raise McpOAuthInvalidGrant("The refresh token is invalid.")
        now = datetime.now(timezone.utc)
        try:
            row = db.query(McpOAuthRefreshTokenV2).filter(
                McpOAuthRefreshTokenV2.token_hash == _sha256(refresh_token)
            ).with_for_update().one_or_none()
            if row is None:
                db.rollback()
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            family_id = row.family_id
            if row.client_id != client_id or row.used_at is not None or row.revoked_at is not None:
                self._revoke_family(db, family_id, now)
                db.commit()
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            if _aware(row.expires_at) <= now or _aware(row.family_expires_at) <= now:
                db.rollback()
                raise McpOAuthInvalidGrant("The refresh token has expired.")
            try:
                kind = _external_kind(client_kind)
            except McpOAuthError:
                kind = None
            if kind is None or (user_is_active is not None and not user_is_active(db, row.user_subject, row.user_email)):
                self._revoke_family(db, family_id, now)
                db.commit()
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            refusal = _unbound_family_refusal(row.jkt, dpop_jkt, allow_unbound=allow_unbound)
            if refusal == "revoke":
                self._revoke_family(db, family_id, now)
                db.commit()
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            if refusal == "refuse":
                db.rollback()
                raise McpOAuthInvalidGrant("The refresh token is invalid.")
            try:
                confirmation, bound_jkt = _refresh_confirmation(row.jkt, dpop_jkt, allow_unbound=allow_unbound)
            except McpOAuthError:
                db.rollback()
                raise
            subject, email, family_jkt = row.user_subject, row.user_email, row.jkt
            family_expires_at = _aware(row.family_expires_at)
            row.used_at = now
            new_token = self._add_refresh_row(
                db, family_id=family_id, client_id=client_id, email=email, subject=subject, jkt=family_jkt,
                now=now, family_expires_at=family_expires_at,
            )
            self._purge_refresh(db, now)
            # Sign before the commit that marks the presented token used.
            response = self._issue(subject=subject, email=email, client_id=client_id, kind=kind, confirmation=confirmation,
                                   dpop_jkt=bound_jkt, refresh_token=new_token, now=now)
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthStorageError("OAuth token issuance is unavailable.") from exc
        return response

    def issue_agent_delegated_token(
        self, *, email: str, subject: str, client_id: str, model_key: str | None = None,
    ) -> dict[str, object]:
        """Mint an identity-only MCP token for the already-authenticated Hub Agent.

        This is not a client-credentials grant and grants no role.  The caller
        is authenticated by the ordinary Hub session before this method is
        reachable.  The named client must still be in the exact OAuth registry.
        """
        if client_id not in self.clients or not email.strip() or not subject.strip():
            raise McpOAuthError("MCP delegated authorization is unavailable.")
        issued_at = int(datetime.now(timezone.utc).timestamp())
        claims: dict[str, object] = {
            "iss": self.issuer, "aud": self.audience, "sub": subject,
            "email": email.strip().lower(), "client_id": client_id,
            "iat": issued_at, "exp": issued_at + self.access_token_ttl_seconds,
            "jti": secrets.token_urlsafe(24), "scope": "renphil.mcp.read",
            CLIENT_KIND_CLAIM: HUB_AGENT_CLIENT_KIND,
        }
        if model_key:
            claims["model_key"] = model_key
        token = self._signing_key.sign(claims)
        return {"access_token": token, "token_type": "Bearer", "expires_in": self.access_token_ttl_seconds, "scope": "renphil.mcp.read"}

    def jwks(self) -> dict[str, object]:
        return self._signing_key.jwks()
