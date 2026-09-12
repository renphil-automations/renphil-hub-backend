"""
Auth router — Google OAuth2 login flow.

Endpoints:
  GET  /login     → redirects the user to Google consent screen
  GET  /callback  → handles the OAuth callback, returns JWT
  GET  /me        → returns the current authenticated user's info
  GET  /dev-login → DEBUG-only: mint a session for any email, no Google
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db_v2.database import get_db_v2
from app.dependencies import (
    CurrentHubUser,
    _ensure_hub_user,
    get_airtable_service,
    get_auth_service,
    get_current_user,
    is_hub_admin,
)
from app.models.auth import MeResponse, TokenResponse, UserInfo
from app.services.airtable_service import AirtableService
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/login", summary="Start Google OAuth flow")
async def login(
    redirect_uri: str = Query(
        ...,
        description="Frontend URL to redirect to after authentication",
    ),
    auth_service: AuthService = Depends(get_auth_service),
):
    """Redirect the caller to the Google consent screen."""
    url = auth_service.get_authorization_url(redirect_uri)
    return RedirectResponse(url)


@router.get("/callback", summary="OAuth callback")
async def callback(
    code: str = Query(..., description="Authorization code from Google"),
    state: str = Query(..., description="OAuth state parameter (PKCE tie-back)"),
    auth_service: AuthService = Depends(get_auth_service),
    airtable_service: AirtableService = Depends(get_airtable_service),
    db: Session = Depends(get_db_v2),
):
    """
    Google redirects here after the user consents.
    Exchanges the code for tokens (using the PKCE verifier tied to state),
    verifies the domain, resolves the user's roles from Airtable, and
    redirects to the frontend with the JWT and roles.
    """
    token_response, frontend_redirect_uri = await auth_service.handle_callback(
        code, state, airtable_service
    )

    # findings_dev_login_live_testing_2026-09-12.md #1: the frontend never
    # actually calls GET /me after this redirect (AuthCallbackPage.tsx builds
    # `user` straight from these query params), so `is_hub_admin` has to ride
    # along here to be seen at all — computed the same way `/dev-login` below
    # computes it, via the real `is_hub_admin` closure (JWT roles OR a live
    # `role_assignments` row), not the JWT-roles-only check the ~20 frontend
    # call sites used to do. Provisions the `hub_users` row synchronously
    # (same helper `/dev-login` uses) since this can be a brand-new user's
    # very first request — nothing has provisioned it yet, and branch 2 of
    # `is_hub_admin` needs a `hub_user_id` to check `role_assignments`
    # against. `hub_user is None` is only reachable in an effectively
    # unreachable concurrent-insert race (see `_ensure_hub_user`'s own
    # docstring) — falling back to `admin=False` there rather than raising,
    # so that edge case never breaks login itself over a nice-to-have flag.
    normalized_email = token_response.email.strip().lower()
    hub_user = _ensure_hub_user(db, email=normalized_email, name=token_response.name)
    admin = False
    if hub_user is not None:
        current = CurrentHubUser(
            info=UserInfo(
                email=token_response.email,
                name=token_response.name,
                roles=token_response.roles,
            ),
            hub_user_id=hub_user.id,
            email=normalized_email,
        )
        admin = is_hub_admin(db, current)

    params = urlencode(
        {
            "access_token": token_response.access_token,
            "email": token_response.email,
            "name": token_response.name,
            "roles": token_response.roles,
            "is_hub_admin": str(admin).lower(),
            **({"picture": token_response.picture} if token_response.picture else {}),
        },
        doseq=True,
    )
    return RedirectResponse(f"{frontend_redirect_uri}?{params}")


@router.get("/me", response_model=MeResponse, summary="Current user info")
async def me(
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    """Return the authenticated user's profile.

    Includes ``scoped_roles``: per-assignment role info from the Access
    Control table, with each entry's ``role_name``, ``scope``, and
    ``fund_or_program_name`` (null when the role's scope is ``Hub``).
    """
    scoped_roles = await airtable_service.get_user_scoped_roles(user.email)
    return MeResponse(
        email=user.email,
        name=user.name,
        picture=user.picture,
        roles=user.roles,
        scoped_roles=scoped_roles,
    )


# ── Dev-only: sign in as any email without Google ───────────────────────
# (session_handoff_2026-09-10-dev-login-tool.md — standalone, additive,
# NOT part of the AC enforcement closeout arc's own numbered steps.)
#
# Lets the read-time visibility system be exercised end to end as several
# different personas locally, without minting a real, distinct Google
# account for each one. Hard-gated on Settings.DEBUG — see `dev_login`
# below for why that's the ONLY gate, matching the dead
# `DEV_ADMIN_OVERRIDE_EMAILS` scaffold's own stated convention
# (app/config.py:22-25, app/services/airtable_service.py ~2550).

_DEV_LOGIN_FORM_HTML = """\
<!doctype html>
<title>Dev login</title>
<form>
  <label>Email <input name="email" type="email" required autofocus></label>
  <button type="submit">Sign in</button>
</form>
"""


def _default_dev_name(email: str) -> str:
    """A readable placeholder name for a dev-minted persona, derived from
    the email's local part (e.g. "jane.doe@x.com" → "Jane Doe"). Never
    shown to anyone but the person testing locally."""
    local_part = email.split("@", 1)[0]
    return local_part.replace(".", " ").replace("_", " ").replace("-", " ").title() or email


@router.get(
    "/dev-login",
    summary="Dev-only: sign in as any email without Google (requires DEBUG=true)",
)
async def dev_login(
    email: str | None = Query(
        default=None,
        description="Email to mint a session for. Omit for a bare local-dev login form.",
    ),
    redirect_uri: str = Query(
        default="http://localhost:5000/auth/callback",
        description=(
            "Frontend callback URL to redirect to — same param/shape as "
            "/auth/login's own redirect_uri, for a frontend dev server on "
            "a non-default port."
        ),
    ),
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
    db: Session = Depends(get_db_v2),
):
    """Mint a real session for an arbitrary email, entirely without Google.

    Hard-gated on ``Settings.DEBUG``: 404s — not 403 — when DEBUG is not
    True, so the route is indistinguishable from an unknown path in any
    environment where it must not exist. No other gate (no separate env
    var). Never touches the real OAuth flow (`handle_callback`,
    `_enforce_login_allowed`) and never calls any `AirtableService`
    method — must never write a test email into the real Access Control
    table.

    `roles` on the minted token is always `[]`, unconditionally — not a
    parameter. A dev-minted token that could claim "Hub Admin" via the JWT
    `roles` claim would let a local test session exercise `is_hub_admin`'s
    branch 1 (app/dependencies.py), the branch a future session's phase 3
    will remove — making local results stop predicting production. Forcing
    `roles: []` means admin status in a dev-login session can only come
    from a real `role_assignments` row, which is also exactly how you want
    to test: grant the persona through the real Grants UI, then dev-login
    as them and see what they see.

    Provisions the caller's `hub_users` row synchronously (the same
    `_ensure_hub_user` helper `get_current_hub_user` uses, called directly
    here rather than waiting for the next authenticated request) — so the
    email is immediately selectable in the Grants UI, not just after one
    extra page load.

    Redirects with the identical query-param shape `/auth/callback`
    already produces, so `AuthCallbackPage.tsx` needs no changes and no
    branching between a real and a dev-minted login.
    """
    if not settings.DEBUG:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    if not email:
        return HTMLResponse(_DEV_LOGIN_FORM_HTML)

    normalized_email = email.strip().lower()
    try:
        user = UserInfo(email=normalized_email, name=_default_dev_name(normalized_email), roles=[])
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid email: {email}") from exc

    hub_user = _ensure_hub_user(db, email=normalized_email, name=user.name)

    access_token = auth_service._create_access_token(user)

    # Same `is_hub_admin` resolution the real /callback now does (see its
    # own comment) — findings_dev_login_live_testing_2026-09-12.md #1. Since
    # `roles` is forced to `[]` above, this can only ever come back `true`
    # via branch 2 (a live `role_assignments` row), which is exactly the
    # scenario this tool exists to exercise.
    admin = False
    if hub_user is not None:
        current = CurrentHubUser(info=user, hub_user_id=hub_user.id, email=normalized_email)
        admin = is_hub_admin(db, current)

    params = urlencode(
        {
            "access_token": access_token,
            "email": user.email,
            "name": user.name,
            "roles": user.roles,
            "is_hub_admin": str(admin).lower(),
        },
        doseq=True,
    )
    return RedirectResponse(f"{redirect_uri}?{params}")
