"""H6: consent page and browser binding for external MCP OAuth clients."""

from __future__ import annotations

import base64
import hashlib
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from app.services.mcp_oauth_service import PersistentMcpOAuthService
from tests.test_mcp_oauth_rate_limit import (
    CLAUDE_CLIENT, ENABLED_EXTERNAL, ISSUER, REDIRECT, _Base, dpop_proof, mcp_settings,
)

EMAIL = "member.person@renphil.org"


class ConsentTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=True)

    def browser(self) -> TestClient:
        # HTTPS so the Secure cookie is stored and sent like in a browser.
        return TestClient(self.app(), base_url="https://hub.example", follow_redirects=False)

    def start(self, http: TestClient, *, email: str = EMAIL):
        started = http.get("/mcp/oauth/authorize", params=self.authorize_params())
        self.assertEqual(started.status_code, 307, started.text)
        continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
        request_id = continuation.rsplit("/", 1)[1]
        with self.Session() as session:
            consent_url = mcp_oauth_router.complete_hub_login(db=session, continuation_uri=continuation, email=email, subject="42")
        return started, request_id, consent_url

    def page(self, http: TestClient, request_id: str):
        return http.get("/mcp/oauth/consent", params={"request_id": request_id})

    @staticmethod
    def csrf(page_html: str) -> str:
        return re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page_html).group(1)

    def decide(self, http: TestClient, request_id: str, csrf: str, decision: str):
        return http.post("/mcp/oauth/consent", data={"request_id": request_id, "csrf_token": csrf, "decision": decision})

    # -- binding cookie ------------------------------------------------------
    def test_authorize_sets_a_short_lived_httponly_secure_lax_cookie(self):
        started, _request_id, _ = self.start(self.browser())
        cookie = started.headers["set-cookie"]
        self.assertIn(f"{mcp_oauth_router.CONSENT_COOKIE}=", cookie)
        for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600", "Path=/mcp/oauth"):
            self.assertIn(attribute.lower(), cookie.lower(), attribute)

    def test_loopback_issuer_cookie_is_not_secure(self):
        self.service = PersistentMcpOAuthService(
            clients=self.service.clients, issuer="https://localhost:8443/mcp/oauth", audience="renphil-data-mcp",
            signing_key=self.service._signing_key,
        )
        response = self.browser().get("/mcp/oauth/authorize", params=self.authorize_params())
        self.assertEqual(response.status_code, 307)
        self.assertNotIn("secure", response.headers["set-cookie"].lower())

    # -- sign-in parks the identity; no code yet -----------------------------
    def test_sign_in_leads_to_hubs_consent_page_not_to_the_client(self):
        _, request_id, consent_url = self.start(self.browser())
        self.assertEqual(consent_url, f"{ISSUER}/consent?request_id={request_id}")
        pending = self.db.query(McpOAuthAuthorizationCodeV2).one()
        # The parked row cannot be exchanged: its key is a server-keyed HMAC.
        for guess in (request_id, f"pending-code\x00{request_id}"):
            self.assertNotEqual(pending.code_hash, hashlib.sha256(guess.encode()).hexdigest())
        response = self.browser().post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": request_id, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": dpop_proof(f"{ISSUER}/token")})
        self.assertEqual(response.status_code, 400)

    def test_consent_page_names_provider_scope_and_user_with_security_headers(self):
        http = self.browser()
        _, request_id, _ = self.start(http)
        page = self.page(http, request_id)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Allow Claude to access RenPhil Hub?", page.text)
        self.assertIn("renphil.mcp.read", page.text)
        self.assertIn(EMAIL, page.text)
        self.assertEqual(page.text.count('method="post"'), 2)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        csp = page.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("no-store", page.headers["cache-control"])
        style = re.search(r"<style>(.*)</style>", page.text, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
        self.assertIn(f"'sha256-{digest}'", csp)
        self.assertNotIn("<script", page.text.lower())

    def test_rendered_values_are_escaped(self):
        http = self.browser()
        _, request_id, _ = self.start(http, email='x"><script>alert(1)</script>@renphil.org')
        page = self.page(http, request_id)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page.text)

    def test_another_browser_cannot_see_or_use_the_consent(self):
        _, request_id, _ = self.start(self.browser())
        stranger = self.browser()
        page = self.page(stranger, request_id)
        self.assertEqual(page.status_code, 400)
        self.assertNotIn("csrf_token", page.text)
        self.assertNotIn("location", page.headers)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(self.decide(stranger, request_id, "0" * 64, "approve").status_code, 400)
        self.assertEqual(self.db.query(McpOAuthAuthorizationRequestV2).count(), 1)

    # -- approve / deny --------------------------------------------------------
    def test_approve_issues_a_single_use_code_to_the_registered_callback(self):
        http = self.browser()
        _, request_id, _ = self.start(http)
        csrf = self.csrf(self.page(http, request_id).text)
        binding = http.cookies.get(mcp_oauth_router.CONSENT_COOKIE)
        self.assertRegex(binding, r"^[0-9a-f]{64}$")
        approved = self.decide(http, request_id, csrf, "approve")
        self.assertEqual(approved.status_code, 303, approved.text)
        location = urlsplit(approved.headers["location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", REDIRECT)
        params = parse_qs(location.query)
        self.assertEqual(params["state"], ["client-state"])
        self.assertIn("max-age=0", approved.headers["set-cookie"].lower())
        exchanged = http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": params["code"][0], "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": dpop_proof(f"{ISSUER}/token")})
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        # The request is gone: replaying the approval (even with the old
        # cookie restored) cannot mint another code.
        self.assertIsNone(http.cookies.get(mcp_oauth_router.CONSENT_COOKIE))
        http.cookies.set(mcp_oauth_router.CONSENT_COOKIE, binding, domain="hub.example", path="/mcp/oauth")
        self.assertEqual(self.decide(http, request_id, csrf, "approve").status_code, 400)
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)

    def test_deny_redirects_with_access_denied_and_state_and_issues_nothing(self):
        http = self.browser()
        _, request_id, _ = self.start(http)
        csrf = self.csrf(self.page(http, request_id).text)
        denied = self.decide(http, request_id, csrf, "deny")
        self.assertEqual(denied.status_code, 303)
        params = parse_qs(urlsplit(denied.headers["location"]).query)
        self.assertEqual(params, {"error": ["access_denied"], "state": ["client-state"]})
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)
        self.assertEqual(self.db.query(McpOAuthAuthorizationRequestV2).count(), 0)

    def test_wrong_csrf_or_decision_is_refused_without_redirect(self):
        http = self.browser()
        _, request_id, _ = self.start(http)
        csrf = self.csrf(self.page(http, request_id).text)
        for token, decision in (("0" * 64, "approve"), ("", "approve"), (csrf, "maybe"), (csrf[:-1] + "0", "deny")):
            with self.subTest(token=token[:8], decision=decision):
                refused = self.decide(http, request_id, token, decision)
                self.assertEqual(refused.status_code, 400)
                self.assertNotIn("location", refused.headers)
        # A CSRF token from another request does not transfer.
        _, other_request, _ = self.start(http)
        other_csrf = self.csrf(self.page(http, other_request).text)
        self.assertEqual(self.decide(http, request_id, other_csrf, "approve").status_code, 400)

    def test_switching_the_provider_off_before_approval_issues_nothing(self):
        http = self.browser()
        _, request_id, _ = self.start(http)
        csrf = self.csrf(self.page(http, request_id).text)
        self._activate({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
        refused = self.decide(http, request_id, csrf, "approve")
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("location", refused.headers)
        self.assertEqual(self.page(http, request_id).status_code, 403)

    def test_consent_can_be_disabled_explicitly(self):
        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=False)
        http = self.browser()
        started, _request_id, redirect = self.start(http)
        self.assertNotIn("set-cookie", started.headers)
        self.assertIn("code", parse_qs(urlsplit(redirect).query))
        self.assertEqual(http.get("/mcp/oauth/consent", params={"request_id": "x"}).status_code, 404)

    def test_consent_is_on_by_default(self):
        from app.config import Settings

        self.assertTrue(Settings(_env_file=None).MCP_OAUTH_CONSENT_REQUIRED)
        self.settings = mcp_settings()
        del self.settings.MCP_OAUTH_CONSENT_REQUIRED
        _, request_id, consent_url = self.start(self.browser())
        self.assertTrue(consent_url.startswith(f"{ISSUER}/consent?"))


if __name__ == "__main__":
    unittest.main()
