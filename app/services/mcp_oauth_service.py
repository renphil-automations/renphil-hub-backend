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
fresh proof per request. There is no bearer-only issuance for external
clients.

Every MCP access token carries ``renphil_client_kind`` (``hub_agent``,
``claude``, ``chatgpt`` or ``gemini``), assigned here from Hub's exact client
registry. Dynamic client registration is not supported in this release.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import jwt
from sqlalchemy.orm import Session

from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from app.services.mcp_access_policy import (
    CLIENT_KIND_CLAIM,
    EXTERNAL_PROVIDERS,
    HUB_AGENT_CLIENT_KIND,
    is_wellformed_client_id,
)
from app.services.mcp_oauth_keys import HubMcpSigningKey


class McpOAuthError(ValueError):
    """An OAuth request is invalid without disclosing sensitive state."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class McpOAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]


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


def _token_response(token: str, *, ttl_seconds: int, dpop_jkt: str | None) -> dict[str, object]:
    return {
        "access_token": token,
        # RFC 9449 §5: a DPoP-bound token is presented with the DPoP scheme.
        "token_type": "DPoP" if dpop_jkt else "Bearer",
        "expires_in": ttl_seconds,
        "scope": "renphil.mcp.read",
    }


def _confirmation(dpop_jkt: str | None) -> dict[str, object]:
    """External MCP tokens are always DPoP-bound; there is no bearer fallback."""
    if not isinstance(dpop_jkt, str) or not dpop_jkt.strip() or len(dpop_jkt) > 128:
        raise McpOAuthError("OAuth DPoP key binding is required.")
    return {"cnf": {"jkt": dpop_jkt.strip()}}


def _external_kind(client_kind: str | None) -> str:
    if not isinstance(client_kind, str) or client_kind not in EXTERNAL_PROVIDERS:
        raise McpOAuthError("OAuth client is not an approved MCP provider.")
    return client_kind


class LocalMcpOAuthService:
    """Thread-safe local store. It is deliberately rejected for shared use."""

    def __init__(self, *, clients: dict[str, McpOAuthClient], issuer: str, audience: str, signing_secret: str, code_ttl_seconds: int = 120, access_token_ttl_seconds: int = 300):
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
            clients[client_id] = McpOAuthClient(client_id=client_id, redirect_uris=normalized)
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

    def exchange_code(self, *, code: str, client_id: str, redirect_uri: str, code_verifier: str, dpop_jkt: str | None = None, client_kind: str | None = None) -> dict[str, object]:
        if not code or not client_id or not redirect_uri or not code_verifier:
            raise McpOAuthError("OAuth token request is invalid.")
        # Validate the key binding and provider before the code is consumed.
        confirmation = _confirmation(dpop_jkt)
        kind = _external_kind(client_kind)
        with self._lock:
            self._purge()
            record = self._codes.get(_sha256(code))
            if record is None or record.consumed or record.client_id != client_id or record.redirect_uri != redirect_uri:
                raise McpOAuthError("OAuth authorization code is invalid or already used.")
            calculated = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
            if not hmac.compare_digest(calculated, record.code_challenge):
                raise McpOAuthError("OAuth PKCE verification failed.")
            record.consumed = True
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": record.subject,
                "email": record.email,
                # Roles are resolved live by Hub before MCP retrieval.
                "client_id": client_id,
                "iat": now,
                "exp": now + self.access_token_ttl_seconds,
                "jti": secrets.token_urlsafe(24),
                "scope": "renphil.mcp.read",
                CLIENT_KIND_CLAIM: kind,
                **confirmation,
            }, self._signing_secret, algorithm="HS256",
        )
        return _token_response(token, ttl_seconds=self.access_token_ttl_seconds, dpop_jkt=dpop_jkt)

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

    def __init__(self, *, clients: dict[str, McpOAuthClient], issuer: str, audience: str, signing_key: HubMcpSigningKey, code_ttl_seconds: int = 120, access_token_ttl_seconds: int = 300):
        if not issuer.startswith("https://"):
            raise McpOAuthError("The deployed MCP OAuth issuer must use HTTPS.")
        self.clients = clients
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._signing_key = signing_key
        self.code_ttl_seconds = max(30, min(int(code_ttl_seconds), 600))
        self.access_token_ttl_seconds = max(60, min(int(access_token_ttl_seconds), 3600))

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
            raise McpOAuthError("OAuth authorization storage is unavailable.") from exc
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
            raise McpOAuthError("OAuth authorization storage is unavailable.") from exc
        return code, redirect_uri, client_state

    def exchange_code(self, db: Session, *, code: str, client_id: str, redirect_uri: str, code_verifier: str, dpop_jkt: str | None = None, client_kind: str | None = None) -> dict[str, object]:
        if not code or not client_id or not redirect_uri or not code_verifier:
            raise McpOAuthError("OAuth token request is invalid.")
        # Validate the key binding and provider before the code is consumed.
        confirmation = _confirmation(dpop_jkt)
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
            db.delete(record)
            db.commit()
        except McpOAuthError:
            raise
        except Exception as exc:
            db.rollback()
            raise McpOAuthError("OAuth authorization storage is unavailable.") from exc
        issued_at = int(now.timestamp())
        token = self._signing_key.sign({
            "iss": self.issuer, "aud": self.audience, "sub": record.user_subject,
            "email": record.user_email, "client_id": client_id, "iat": issued_at,
            "exp": issued_at + self.access_token_ttl_seconds, "jti": secrets.token_urlsafe(24),
            "scope": "renphil.mcp.read", CLIENT_KIND_CLAIM: kind, **confirmation,
        })
        return _token_response(token, ttl_seconds=self.access_token_ttl_seconds, dpop_jkt=dpop_jkt)

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
