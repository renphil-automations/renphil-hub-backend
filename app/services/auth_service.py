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

import jwt
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from app.config import Settings
from app.helpers.exceptions import DomainNotAllowedError, GoogleOAuthError
from app.helpers.google_client import build_oauth_flow
from app.models.auth import TokenResponse, UserInfo

logger = logging.getLogger(__name__)

# In-memory PKCE store: state → (code_verifier, frontend_redirect_uri)
# Fine for single-process / dev; replace with Redis/cache in production.
_pkce_store: dict[str, tuple[str, str]] = {}


def _generate_code_verifier() -> str:
    """Generate a cryptographically random PKCE code verifier."""
    return secrets.token_urlsafe(96)


def _derive_code_challenge(verifier: str) -> str:
    """Derive the S256 code challenge from a verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class AuthService:
    """Handles every step of the Google OAuth & JWT flow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ── Step 1: authorization URL ──────────────────────────────────────
    def get_authorization_url(self, redirect_uri: str) -> str:
        """Return the Google consent-screen URL the frontend should redirect to."""
        flow = build_oauth_flow(self._settings)

        # Generate PKCE challenge
        code_verifier = _generate_code_verifier()
        code_challenge = _derive_code_challenge(code_verifier)

        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            hd=self._settings.ALLOWED_EMAIL_DOMAIN,  # hint — enforced server-side too
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )

        # Store verifier + frontend redirect so callback can retrieve them by state
        _pkce_store[state] = (code_verifier, redirect_uri)
        return auth_url

    # ── Step 2: exchange code → user info → JWT ────────────────────────
    async def handle_callback(self, code: str, state: str) -> tuple[TokenResponse, str]:
        """
        Complete the OAuth callback:
          - exchange auth code for tokens (with PKCE verifier)
          - verify & decode the id_token
          - enforce domain
          - mint a local JWT

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

        # Enforce domain
        self._enforce_domain(email)

        # Mint a local JWT for session management
        access_token = self._create_access_token(
            UserInfo(email=email, name=name, picture=picture)
        )

        return TokenResponse(
            access_token=access_token,
            email=email,
            name=name,
            picture=picture,
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
        )

    # ── Domain restriction ─────────────────────────────────────────────
    def _enforce_domain(self, email: str) -> None:
        domain = email.rsplit("@", 1)[-1].lower()
        if domain != self._settings.ALLOWED_EMAIL_DOMAIN.lower():
            raise DomainNotAllowedError(email, self._settings.ALLOWED_EMAIL_DOMAIN)
