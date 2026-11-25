"""S1: the consent browser binding also covers the Hub sign-in.

Attack (security review S1/P1): the attacker starts /authorize in their own
browser (getting the binding cookie and continuation R), lures the victim to
/auth/login?redirect_uri=<issuer>/continue/R, the victim signs in, and the
attacker approves consent in their own browser. The binding cookie must
therefore be required at /auth/login and again in /auth/callback.
"""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import app.routers.auth as auth_router
import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2
from app.services.mcp_oauth_service import McpOAuthError, PersistentMcpOAuthService
from tests.test_mcp_oauth_rate_limit import _Base, mcp_settings

VICTIM = "victim.person@renphil.org"
HOST_COOKIE = "__Host-renphil_mcp_oauth_consent"


class FakeAuth:
    """Stands in for Google: login allows the issuer continuation, callback
    returns the continuation the login started with."""

    continuation = ""

    def is_allowed_login_redirect_uri(self, uri):
        return True

    def get_authorization_url(self, redirect_uri):
        FakeAuth.continuation = redirect_uri
        return "https://accounts.google.example/o/oauth2/auth?state=s"

    async def handle_callback(self, code, state, db):
        token = SimpleNamespace(access_token="hub-session-jwt", email=VICTIM, name="Victim", roles=["Hub Member"], picture=None)
        return token, FakeAuth.continuation


class LoginBindingTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=True)
        app = self.app()
        app.dependency_overrides[auth_router.get_auth_service] = lambda: FakeAuth()
        self.web = app
        hub_user = SimpleNamespace(id=77, email=VICTIM)
        for target in (
            patch.object(auth_router, "_ensure_hub_user", lambda *a, **k: hub_user),
            patch.object(auth_router, "is_hub_admin", lambda *a, **k: False),
            patch.object(auth_router, "get_settings", lambda: self.settings),
        ):
            target.start()
            self.addCleanup(target.stop)

    def browser(self) -> TestClient:
        return TestClient(self.web, base_url="https://hub.example", follow_redirects=False)

    def start(self, http):
        started = http.get("/mcp/oauth/authorize", params=self.authorize_params())
        self.assertEqual(started.status_code, 307, started.text)
        return started, started.headers["location"]

    def test_binding_cookie_is_a_host_prefixed_secure_root_cookie(self):
        started, _ = self.start(self.browser())
        cookie = started.headers["set-cookie"]
        self.assertTrue(cookie.startswith(HOST_COOKIE + "_"), cookie)
        for attribute in ("httponly", "secure", "samesite=lax", "path=/;", "max-age=600"):
            self.assertIn(attribute, cookie.lower() + ";", attribute)
        self.assertNotIn("domain=", cookie.lower())

    def test_loopback_issuer_uses_an_unprefixed_non_secure_cookie(self):
        self.service = PersistentMcpOAuthService(
            clients=self.service.clients, issuer="https://localhost:8443/mcp/oauth", audience="renphil-data-mcp",
            signing_key=self.service._signing_key,
        )
        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=True, MCP_OAUTH_ISSUER="https://localhost:8443/mcp/oauth")
        started = self.browser().get("/mcp/oauth/authorize", params=self.authorize_params())
        cookie = started.headers["set-cookie"]
        self.assertTrue(cookie.startswith("renphil_mcp_oauth_consent_"), cookie)
        self.assertNotIn("secure", cookie.lower())

    def test_sign_in_in_another_browser_is_refused_at_login(self):
        attacker = self.browser()
        _, login_location = self.start(attacker)
        victim = self.browser()
        refused = victim.get(login_location)
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("accounts.google", refused.headers.get("location", ""))
        # The attacker's own browser is allowed to sign in.
        self.assertEqual(attacker.get(login_location).status_code, 307)

    def test_callback_in_another_browser_is_refused_and_parks_nothing(self):
        attacker = self.browser()
        _, login_location = self.start(attacker)
        self.assertEqual(attacker.get(login_location).status_code, 307)  # sets FakeAuth.continuation
        victim = self.browser()
        refused = victim.get("/auth/callback", params={"code": "g", "state": "s"})
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("hub-session-jwt", refused.headers.get("location", ""))
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)
        page = attacker.get("/mcp/oauth/consent", params={"request_id": FakeAuth.continuation.rsplit("/", 1)[1]})
        self.assertEqual(page.status_code, 400)
        self.assertNotIn(VICTIM, page.text)

    def test_complete_hub_login_requires_the_matching_cookie(self):
        attacker = self.browser()
        _, login_location = self.start(attacker)
        continuation = parse_qs(urlsplit(login_location).query)["redirect_uri"][0]
        for cookies in (None, {}, {HOST_COOKIE: "0" * 64}):
            with self.subTest(cookies=cookies), self.Session() as session, self.assertRaises(McpOAuthError) as caught:
                mcp_oauth_router.complete_hub_login(db=session, continuation_uri=continuation, email=VICTIM, subject="77", cookies=cookies)
            self.assertIn("invalid or expired", str(caught.exception))
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)

    def test_the_same_browser_completes_the_whole_flow(self):
        http = self.browser()
        _, login_location = self.start(http)
        self.assertEqual(http.get(login_location).status_code, 307)
        callback = http.get("/auth/callback", params={"code": "g", "state": "s"})
        self.assertEqual(callback.status_code, 307, callback.text)
        consent_url = callback.headers["location"]
        self.assertTrue(consent_url.startswith("https://hub.example/mcp/oauth/consent?"))
        request_id = parse_qs(urlsplit(consent_url).query)["request_id"][0]
        page = http.get("/mcp/oauth/consent", params={"request_id": request_id})
        self.assertEqual(page.status_code, 200)
        csrf = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text).group(1)
        approved = http.post("/mcp/oauth/consent", data={"request_id": request_id, "csrf_token": csrf, "decision": "approve"})
        self.assertEqual(approved.status_code, 303)
        self.assertIn("code=", approved.headers["location"])

    def test_consent_disabled_keeps_the_previous_login_behaviour(self):
        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=False)
        attacker = self.browser()
        _, login_location = self.start(attacker)
        self.assertEqual(self.browser().get(login_location).status_code, 307)


if __name__ == "__main__":
    unittest.main()
