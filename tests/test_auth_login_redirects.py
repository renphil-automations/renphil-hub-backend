from types import SimpleNamespace

import asyncio

import pytest
from fastapi import HTTPException

from app.routers.auth import dev_login, login
from app.services.auth_service import AuthService


def _service(*, origins=None):
    return AuthService(SimpleNamespace(
        ALLOWED_ORIGINS=origins or ["https://hub.example", "http://localhost:5000"],
    ))


def test_only_exact_allowed_frontend_callback_is_accepted():
    service = _service()
    assert service.is_allowed_login_redirect_uri("https://hub.example/auth/callback")
    assert service.is_allowed_login_redirect_uri("http://localhost:5000/auth/callback")
    for rejected in (
        "https://hub.example/other",
        "https://hub.example/auth/callback/",
        "https://hub.example/auth/callback?next=/x",
        "https://hub.example/auth/callback#token",
        "https://attacker.example/auth/callback",
        "http://hub.example/auth/callback",
        "http://localhost:5001/auth/callback",
        "javascript:alert(1)",
        "data:text/html,hello",
        "//hub.example/auth/callback",
        "/auth/callback",
        "",
        "https://hub.example@attacker.example/auth/callback",
        "https://user:pass@hub.example/auth/callback",
    ):
        assert not service.is_allowed_login_redirect_uri(rejected), rejected


def test_host_and_scheme_are_compared_case_insensitively_but_path_is_exact():
    service = _service()
    assert service.is_allowed_login_redirect_uri("HTTPS://HUB.Example/auth/callback")
    assert not service.is_allowed_login_redirect_uri("https://hub.example/Auth/Callback")


def test_malformed_or_non_http_allowed_origins_never_admit_anything():
    service = _service(origins=["*", "null", "ftp://hub.example", "https://hub.example/app", "https://hub.example?x=1"])
    for candidate in (
        "https://hub.example/auth/callback",
        "ftp://hub.example/auth/callback",
        "https://anything.example/auth/callback",
    ):
        assert not service.is_allowed_login_redirect_uri(candidate), candidate
    assert _service(origins=["https://hub.example/"]).is_allowed_login_redirect_uri("https://hub.example/auth/callback")


def test_sandbox_has_no_mcp_continuation_exception():
    # Sandbox has no MCP OAuth: a continuation-shaped path is just another
    # unapproved path, even on an allowed origin.
    service = _service()
    assert not service.is_allowed_login_redirect_uri("https://hub.example/mcp/oauth/continue/request-id")


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


class _NoDb:
    def __getattr__(self, name):
        raise AssertionError("dev-login must refuse the redirect before touching the database")


def test_dev_login_rejects_an_unapproved_redirect_before_minting_a_session():
    service = _service()
    service._create_access_token = lambda user: pytest.fail("no session may be minted")  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as caught:
        asyncio.run(dev_login(
            email="someone@renphil.org",
            redirect_uri="https://attacker.example/auth/callback",
            settings=SimpleNamespace(DEBUG=True),
            auth_service=service,
            db=_NoDb(),
        ))
    assert caught.value.status_code == 400
    assert caught.value.detail == "Invalid login redirect URI."


def test_dev_login_still_404s_when_debug_is_off_even_for_a_bad_redirect():
    with pytest.raises(HTTPException) as caught:
        asyncio.run(dev_login(
            email="someone@renphil.org",
            redirect_uri="https://attacker.example/auth/callback",
            settings=SimpleNamespace(DEBUG=False),
            auth_service=_service(),
            db=_NoDb(),
        ))
    assert caught.value.status_code == 404


def test_dev_login_form_is_still_served_without_an_email():
    response = asyncio.run(dev_login(
        email=None,
        redirect_uri="http://localhost:5000/auth/callback",
        settings=SimpleNamespace(DEBUG=True),
        auth_service=_service(),
        db=_NoDb(),
    ))
    assert response.status_code == 200
