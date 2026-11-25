from types import SimpleNamespace

import asyncio

import pytest
from fastapi import HTTPException

from app.routers.auth import login
from app.services.auth_service import AuthService


def _service(*, origins=None, issuer="https://hub.example/mcp/oauth"):
    return AuthService(SimpleNamespace(
        ALLOWED_ORIGINS=origins or ["https://hub.example", "http://localhost:5000"],
        MCP_OAUTH_ISSUER=issuer,
    ))


def test_only_exact_allowed_frontend_callback_is_accepted():
    service = _service()
    assert service.is_allowed_login_redirect_uri("https://hub.example/auth/callback")
    assert service.is_allowed_login_redirect_uri("http://localhost:5000/auth/callback")
    for rejected in (
        "https://hub.example/other",
        "https://hub.example/auth/callback?next=/x",
        "https://hub.example/auth/callback#token",
        "https://attacker.example/auth/callback",
        "javascript:alert(1)",
        "data:text/html,hello",
        "https://hub.example@attacker.example/auth/callback",
    ):
        assert not service.is_allowed_login_redirect_uri(rejected)


def test_only_issuer_continuation_path_is_accepted_for_mcp_login():
    service = _service()
    assert service.is_allowed_login_redirect_uri("https://hub.example/mcp/oauth/continue/request-id")
    for rejected in (
        "https://hub.example/mcp/oauth/continue/",
        "https://hub.example/mcp/oauth/continue/request/id",
        "https://hub.example/mcp/oauth/authorize",
        "https://attacker.example/mcp/oauth/continue/request-id",
        "https://hub.example/mcp/oauth/continue/request-id?x=1",
    ):
        assert not service.is_allowed_login_redirect_uri(rejected)


def test_login_route_rejects_an_unapproved_redirect_before_google_is_called():
    class Service:
        def is_allowed_login_redirect_uri(self, redirect_uri):
            return False

        def get_authorization_url(self, redirect_uri):
            raise AssertionError("Google OAuth must not start for an unapproved redirect")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(login("https://attacker.example/auth/callback", auth_service=Service()))
    assert caught.value.status_code == 400
    assert caught.value.detail == "Invalid login redirect URI."


def test_login_route_keeps_the_normal_google_flow_for_an_allowed_callback():
    class Service:
        def is_allowed_login_redirect_uri(self, redirect_uri):
            return redirect_uri == "https://hub.example/auth/callback"

        def get_authorization_url(self, redirect_uri):
            assert redirect_uri == "https://hub.example/auth/callback"
            return "https://accounts.google.com/o/oauth2/auth?state=opaque"

    response = asyncio.run(login("https://hub.example/auth/callback", auth_service=Service()))
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://accounts.google.com/")
