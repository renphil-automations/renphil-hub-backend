"""
Google OAuth service.

Responsibilities:
  1. Build the authorization URL.
  2. Exchange the callback code for tokens.
  3. Fetch user info from Google.
  4. Enforce email-domain restriction.
  5. Issue a local JWT for subsequent API calls.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import SplitResult, urlsplit

import jwt
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from sqlalchemy.orm import Session

from app.config import Settings
from app.helpers.exceptions import GoogleOAuthError, UserNotAuthorizedError
from app.helpers.google_client import build_oauth_flow
from app.models.auth import TokenResponse, UserInfo
from app.services.user_db_service import find_user_by_email

logger = logging.getLogger(__name__)

# In-memory PKCE store: state → (code_verifier, frontend_redirect_uri)
# Fine for single-process / dev; replace with Redis/cache in production.
_pkce_store: dict[str, tuple[str, str]] = {}

# Which rule in `_enforce_login_allowed` let an email through.
LoginAdmission = Literal["domain", "users"]


def _generate_code_verifier() -> str:
    """Generate a cryptographically random PKCE code verifier."""
    return secrets.token_urlsafe(96)


def _derive_code_challenge(verifier: str) -> str:
    """Derive the S256 code challenge from a verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _mcp_continuation_request_id(candidate: SplitResult, issuer: str | None) -> str | None:
    """Return what follows ``<issuer>/continue/`` in ``candidate``'s path.

    ``None`` when no issuer is configured, the scheme/host differ from the
    issuer's (compared case-insensitively), or the path is not under the
    issuer's ``/continue/`` prefix. The one place this match is defined, so
    the login redirect check and the callback guard cannot drift apart.
    """
    issuer = (issuer or "").rstrip("/")
    if not issuer:
        return None
    issuer_parts = urlsplit(issuer)
    candidate_origin = f"{candidate.scheme.lower()}://{candidate.netloc.lower()}"
    if candidate_origin != f"{issuer_parts.scheme.lower()}://{issuer_parts.netloc.lower()}":
        return None
    prefix = f"{issuer_parts.path.rstrip('/')}/continue/"
    if not candidate.path.startswith(prefix):
        return None
    return candidate.path.removeprefix(prefix)


def is_mcp_continuation_uri(redirect_uri: str, issuer: str | None) -> bool:
    """True when ``redirect_uri`` points at the MCP OAuth continuation prefix.

    Deliberately broader than ``is_allowed_login_redirect_uri``: any query,
    fragment or request id (even an empty or malformed one) still counts, so
    the callback can refuse to append the Hub session token to such a URI
    when the MCP flow did not consume it.
    """
    try:
        candidate = urlsplit(redirect_uri)
    except (TypeError, ValueError):
        return False
    return _mcp_continuation_request_id(candidate, issuer) is not None


class AuthService:
    """Handles every step of the Google OAuth & JWT flow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_allowed_login_redirect_uri(self, redirect_uri: str) -> bool:
        """Accept only a Hub frontend callback or an internal MCP continuation.

        ``/auth/callback`` later adds the Hub session token to this location,
        so accepting arbitrary schemes, origins, paths, queries, or fragments
        would turn the normal Google login into an open redirect/token leak.
        """
        try:
            candidate = urlsplit(redirect_uri)
        except (TypeError, ValueError):
            return False
        if (
            candidate.scheme.lower() not in {"http", "https"}
            or not candidate.netloc
            or candidate.username is not None
            or candidate.password is not None
            or candidate.query
            or candidate.fragment
        ):
            return False

        candidate_origin = f"{candidate.scheme.lower()}://{candidate.netloc.lower()}"
        for origin in self._settings.ALLOWED_ORIGINS:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme.lower() in {"http", "https"}
                and parsed_origin.netloc
                and not parsed_origin.query
                and not parsed_origin.fragment
                and parsed_origin.path in {"", "/"}
                and candidate_origin == f"{parsed_origin.scheme.lower()}://{parsed_origin.netloc.lower()}"
                and candidate.path == "/auth/callback"
            ):
                return True

        request_id = _mcp_continuation_request_id(candidate, self._settings.MCP_OAUTH_ISSUER)
        return bool(request_id) and "/" not in request_id

    # ── Step 1: authorization URL ──────────────────────────────────────
    def get_authorization_url(self, redirect_uri: str) -> str:
        """Return the Google consent-screen URL the frontend should redirect to."""
        flow = build_oauth_flow(self._settings)

        # Generate PKCE challenge
        code_verifier = _generate_code_verifier()
        code_challenge = _derive_code_challenge(code_verifier)

        auth_url, state = flow.authorization_url(
            access_type="offline",
            # select_account renders the account chooser; without it Google can
            # route users to "add a Google account" and reject an account that
            # already exists on the device. consent keeps the refresh token.
            prompt="select_account consent",
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )

        # Store verifier + frontend redirect so callback can retrieve them by state
        _pkce_store[state] = (code_verifier, redirect_uri)
        return auth_url

    # ── Step 2: exchange code → user info → JWT ────────────────────────
    async def handle_callback(
        self,
        code: str,
        state: str,
        db: Session,
    ) -> tuple[TokenResponse, str]:
        """
        Complete the OAuth callback:
          - exchange auth code for tokens (with PKCE verifier)
          - verify & decode the id_token
          - enforce the login allow-list (domain, users)
          - mint a local JWT

        Never touches Airtable (plan_login_airtable_removal_2026-10-31.md):
        admission is decided by Postgres alone, and the token's `roles` is
        always `[]` — nothing server-side reads it since phase 3
        (`is_hub_admin` resolves from `role_assignments`), same as
        `/auth/dev-login`.

        Returns (TokenResponse, frontend_redirect_uri).
        """
        # Retrieve and consume the PKCE verifier + redirect URI stored during /login
        stored = _pkce_store.pop(state, None)
        if stored is None:
            raise GoogleOAuthError("Invalid or expired OAuth state. Please log in again.")

        code_verifier, frontend_redirect_uri = stored

        flow = build_oauth_flow(self._settings)

        try:
            flow.fetch_token(code=code, code_verifier=code_verifier)
        except Exception as exc:
            logger.error("Token exchange failed: %s", exc)
            raise GoogleOAuthError("Failed to exchange authorization code.") from exc

        credentials = flow.credentials

        # Verify the Google-issued id_token
        try:
            id_info = google_id_token.verify_oauth2_token(
                credentials.id_token,
                google_requests.Request(),
                self._settings.GOOGLE_CLIENT_ID,
            )
        except ValueError as exc:
            logger.error("ID token verification failed: %s", exc)
            raise GoogleOAuthError("Invalid ID token from Google.") from exc

        email: str = id_info.get("email", "")
        name: str = id_info.get("name", "")
        picture: str | None = id_info.get("picture")

        # Gate login: org domain, or a pre-registered external email
        self._enforce_login_allowed(email, db)

        roles: list[str] = []
        access_token = self._create_access_token(
            UserInfo(email=email, name=name, picture=picture, roles=roles)
        )

        return TokenResponse(
            access_token=access_token,
            email=email,
            name=name,
            picture=picture,
            roles=roles,
        ), frontend_redirect_uri

    # ── JWT helpers ────────────────────────────────────────────────────
    def _create_access_token(self, user: UserInfo) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=self._settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        )
        payload = {
            "sub": user.email,
            "name": user.name,
            "picture": user.picture,
            "roles": user.roles,
            "exp": expire,
        }
        return jwt.encode(
            payload,
            self._settings.JWT_SECRET_KEY,
            algorithm=self._settings.JWT_ALGORITHM,
        )

    def decode_access_token(self, token: str) -> UserInfo:
        """Verify & decode a locally-issued JWT, returning the user info."""
        try:
            payload = jwt.decode(
                token,
                self._settings.JWT_SECRET_KEY,
                algorithms=[self._settings.JWT_ALGORITHM],
            )
        except jwt.ExpiredSignatureError as exc:
            raise GoogleOAuthError("Token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            raise GoogleOAuthError("Invalid token.") from exc

        return UserInfo(
            email=payload["sub"],
            name=payload.get("name", ""),
            picture=payload.get("picture"),
            roles=payload.get("roles") or [],
        )

    # ── Login allow-list ────────────────────────────────────────────────
    def _enforce_login_allowed(self, email: str, db: Session) -> LoginAdmission:
        """Gate login by email, returning which rule admitted it.

        Org-domain emails are always allowed. A non-org email is allowed only
        when a `users` row signs in as it (`work_email`, else `alias_email` —
        an HR row, or a person added from the Access Control page's Users
        tab). Otherwise login is refused. The Airtable Access Control table
        is no longer a fallback (plan_login_airtable_removal_2026-10-31.md;
        owner accepted that externals known only there lose access).

        `users.has_signed_in` is deliberately NOT checked (owner decision,
        2026-10-04): an admin-added person
        has not yet — that is the whole point of adding them.
        """
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in self._settings.allowed_email_domains:
            return "domain"
        if find_user_by_email(db, email) is not None:
            return "users"
        raise UserNotAuthorizedError(email)
