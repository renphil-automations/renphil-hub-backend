"""Blocking work must not run on the event loop.

FastAPI runs a plain ``def`` dependency in its threadpool but runs an
``async def`` one directly on the loop, so a synchronous SQLAlchemy query
inside an ``async def`` dependency stalls every concurrent request. The
Google token exchange in the OAuth callback is a blocking HTTP call too.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app.dependencies as deps
import app.services.auth_service as auth_module

DB_DEPENDENCIES = [
    "get_current_hub_user",
    "require_hub_admin",
    "get_viewer_access",
    "get_view_as_selection",
    "get_view_as_access",
    "get_lock_view",
]


@pytest.mark.parametrize("name", DB_DEPENDENCIES)
def test_database_dependencies_are_plain_functions(name):
    assert not inspect.iscoroutinefunction(getattr(deps, name)), name


def test_get_view_as_access_without_view_as_still_delegates_to_get_viewer_access():
    sentinel = object()
    current = SimpleNamespace(hub_user_id=7, email="a@renphil.org")
    with patch.object(deps, "get_viewer_access", return_value=sentinel) as viewer:
        result = deps.get_view_as_access(selection=None, anonymous=False, current=current, db="db")
    viewer.assert_called_once_with(current=current, db="db")
    assert result.access is sentinel and result.view_as is False


def test_callback_token_exchange_and_id_token_check_run_off_the_event_loop():
    seen: dict[str, int] = {}

    def fetch_token(**_kwargs):
        seen["fetch"] = threading.get_ident()

    def verify(*_args, **_kwargs):
        seen["verify"] = threading.get_ident()
        return {"email": "someone@renphil.org", "name": "Someone"}

    flow = MagicMock()
    flow.fetch_token.side_effect = fetch_token
    flow.credentials = SimpleNamespace(id_token="id-token")
    service = auth_module.AuthService(
        SimpleNamespace(
            GOOGLE_CLIENT_ID="client",
            allowed_email_domains=["renphil.org"],
            JWT_ACCESS_TOKEN_EXPIRE_MINUTES=5,
            JWT_SECRET_KEY="k" * 32,
            JWT_ALGORITHM="HS256",
        )
    )

    async def run():
        seen["loop"] = threading.get_ident()
        auth_module._pkce_store["blocking-state"] = ("verifier", "https://hub.example/auth/callback")
        return await service.handle_callback("code", "blocking-state", db=None)

    with patch.object(auth_module, "build_oauth_flow", return_value=flow), patch.object(
        auth_module.google_id_token, "verify_oauth2_token", side_effect=verify
    ):
        token, redirect = asyncio.run(run())

    assert token.email == "someone@renphil.org"
    assert redirect == "https://hub.example/auth/callback"
    flow.fetch_token.assert_called_once_with(code="code", code_verifier="verifier")
    assert seen["fetch"] != seen["loop"]
    assert seen["verify"] != seen["loop"]
