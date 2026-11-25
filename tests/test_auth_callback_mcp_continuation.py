"""The Google callback never appends the Hub session token to an MCP continuation.

``/auth/callback`` converts an MCP OAuth continuation into a client code via
``complete_hub_login``. If that does not happen (MCP disabled, no Hub user
row, or a spelling of the issuer that the exact-prefix match in
``complete_hub_login`` does not consume), the continuation must be refused
instead of falling through to the normal frontend redirect that carries
``access_token``. The continuation match uses the same case-insensitive
scheme/host normalisation as ``AuthService.is_allowed_login_redirect_uri``.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.auth as auth_router
import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.database import get_db_v2
from app.services.auth_service import AuthService, is_mcp_continuation_uri

ISSUER = "https://hub.example/mcp/oauth"
FRONTEND_CALLBACK = "https://hub.example/auth/callback"
SESSION_JWT = "hub-session-jwt"


class ContinuationMatchTests(unittest.TestCase):
    def test_matches_issuer_continuations_case_insensitively_on_scheme_and_host(self):
        for uri in (
            "https://hub.example/mcp/oauth/continue/request-id",
            "HTTPS://HUB.EXAMPLE/mcp/oauth/continue/request-id",
            "https://Hub.Example/mcp/oauth/continue/request-id",
            "https://hub.example/mcp/oauth/continue/",
            "https://hub.example/mcp/oauth/continue/a/b",
            "https://hub.example/mcp/oauth/continue/request-id?x=1",
            "https://hub.example/mcp/oauth/continue/request-id#frag",
        ):
            with self.subTest(uri=uri):
                self.assertTrue(is_mcp_continuation_uri(uri, ISSUER))
                self.assertTrue(is_mcp_continuation_uri(uri, ISSUER + "/"))

    def test_does_not_match_frontend_callbacks_other_hosts_or_paths(self):
        for uri in (
            FRONTEND_CALLBACK,
            "http://localhost:5000/auth/callback",
            "https://attacker.example/mcp/oauth/continue/request-id",
            "http://hub.example/mcp/oauth/continue/request-id",
            "https://hub.example/mcp/oauth/authorize",
            "https://hub.example/mcp/oauth/continuex/request-id",
            "https://hub.example/MCP/OAUTH/continue/request-id",
            "",
        ):
            with self.subTest(uri=uri):
                self.assertFalse(is_mcp_continuation_uri(uri, ISSUER))
        self.assertFalse(is_mcp_continuation_uri("https://hub.example/mcp/oauth/continue/x", None))
        self.assertFalse(is_mcp_continuation_uri("https://hub.example/mcp/oauth/continue/x", ""))

    def test_login_redirect_check_is_unchanged_and_agrees_on_normalisation(self):
        service = AuthService(SimpleNamespace(ALLOWED_ORIGINS=["https://hub.example"], MCP_OAUTH_ISSUER=ISSUER))
        for uri in (
            "https://hub.example/mcp/oauth/continue/request-id",
            "HTTPS://HUB.EXAMPLE/mcp/oauth/continue/request-id",
        ):
            with self.subTest(uri=uri):
                self.assertTrue(service.is_allowed_login_redirect_uri(uri))
                self.assertTrue(is_mcp_continuation_uri(uri, ISSUER))
        for uri in (
            "https://hub.example/mcp/oauth/continue/",
            "https://hub.example/mcp/oauth/continue/a/b",
            "https://hub.example/mcp/oauth/continue/request-id?x=1",
            "https://attacker.example/mcp/oauth/continue/request-id",
        ):
            with self.subTest(uri=uri):
                self.assertFalse(service.is_allowed_login_redirect_uri(uri))
        self.assertFalse(
            AuthService(SimpleNamespace(ALLOWED_ORIGINS=[], MCP_OAUTH_ISSUER=None))
            .is_allowed_login_redirect_uri("https://hub.example/mcp/oauth/continue/request-id")
        )


class CallbackContinuationGuardTests(unittest.TestCase):
    def _get(self, frontend_uri: str, *, mcp_enabled: bool, hub_user=SimpleNamespace(id=42, email="member@renphil.org")):
        token = SimpleNamespace(access_token=SESSION_JWT, email="member@renphil.org", name="Member", roles=[], picture=None)

        class FakeAuth:
            async def handle_callback(self, code, state, db):
                return token, frontend_uri

        app = FastAPI()
        app.include_router(auth_router.router)
        app.dependency_overrides[auth_router.get_auth_service] = lambda: FakeAuth()
        app.dependency_overrides[get_db_v2] = lambda: None
        settings = SimpleNamespace(MCP_OAUTH_ENABLED=mcp_enabled, MCP_OAUTH_ISSUER=ISSUER)
        with patch.object(auth_router, "_ensure_hub_user", lambda *a, **k: hub_user), \
                patch.object(auth_router, "is_hub_admin", lambda *a, **k: False), \
                patch.object(auth_router, "get_settings", lambda: settings), \
                patch.object(mcp_oauth_router, "get_settings", lambda: settings):
            return TestClient(app, follow_redirects=False).get("/auth/callback", params={"code": "g", "state": "s"})

    def assertRefusedWithoutToken(self, response):
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "OAuth authorization request expired or is invalid.")
        self.assertNotIn("location", response.headers)
        self.assertNotIn(SESSION_JWT, response.text)

    def test_unconsumed_continuation_is_refused_when_mcp_is_disabled(self):
        self.assertRefusedWithoutToken(self._get(f"{ISSUER}/continue/request-id", mcp_enabled=False))

    def test_unconsumed_continuation_is_refused_without_a_hub_user_row(self):
        self.assertRefusedWithoutToken(self._get(f"{ISSUER}/continue/request-id", mcp_enabled=True, hub_user=None))

    def test_uppercase_issuer_continuation_is_not_consumed_and_gets_no_token(self):
        # complete_hub_login only consumes the exact issuer spelling; the
        # login check accepts this variant, so it must not fall through.
        with patch.object(mcp_oauth_router, "_service", side_effect=AssertionError("must not be consumed")):
            response = self._get("HTTPS://HUB.EXAMPLE/mcp/oauth/continue/request-id", mcp_enabled=True)
        self.assertRefusedWithoutToken(response)

    def test_consumed_continuation_still_redirects_to_the_client_with_a_code(self):
        with patch.object(mcp_oauth_router, "complete_hub_login", return_value="https://client.example/callback?code=c&state=s") as complete:
            response = self._get(f"{ISSUER}/continue/request-id", mcp_enabled=True)
        complete.assert_called_once()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://client.example/callback?code=c&state=s")
        self.assertNotIn(SESSION_JWT, response.headers["location"])

    def test_normal_frontend_callback_still_receives_the_session(self):
        for mcp_enabled in (False, True):
            with self.subTest(mcp_enabled=mcp_enabled):
                response = self._get(FRONTEND_CALLBACK, mcp_enabled=mcp_enabled)
                self.assertEqual(response.status_code, 307)
                target = urlsplit(response.headers["location"])
                self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}", FRONTEND_CALLBACK)
                self.assertEqual(parse_qs(target.query)["access_token"], [SESSION_JWT])


if __name__ == "__main__":
    unittest.main()
