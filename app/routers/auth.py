"""
Auth router — Google OAuth2 login flow.

Endpoints:
  GET  /login    → redirects the user to Google consent screen
  GET  /callback → handles the OAuth callback, returns JWT
  GET  /me       → returns the current authenticated user's info
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from app.dependencies import get_airtable_service, get_auth_service, get_current_user
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
    params = urlencode(
        {
            "access_token": token_response.access_token,
            "email": token_response.email,
            "name": token_response.name,
            "roles": token_response.roles,
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
