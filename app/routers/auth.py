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

from app.dependencies import get_auth_service, get_current_user
from app.models.auth import TokenResponse, UserInfo
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
):
    """
    Google redirects here after the user consents.
    Exchanges the code for tokens (using the PKCE verifier tied to state),
    verifies the domain, and redirects to the frontend with the JWT.
    """
    token_response, frontend_redirect_uri = await auth_service.handle_callback(code, state)
    params = urlencode({
        "access_token": token_response.access_token,
        "email": token_response.email,
        "name": token_response.name,
        **({
            "picture": token_response.picture
        } if token_response.picture else {}),
    })
    return RedirectResponse(f"{frontend_redirect_uri}?{params}")


@router.get("/me", response_model=UserInfo, summary="Current user info")
async def me(user: UserInfo = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return user
