"""H1: per-provider token binding policy (MCP_OAUTH_BEARER_PROVIDERS).

Providers in the set may receive an unbound Bearer token when they send no
DPoP header; a header that is sent is always validated and binds the token.
Providers outside the set (the default: all of them) must send DPoP.
"""

from __future__ import annotations

import json
import unittest

import jwt
from pydantic import ValidationError

from app.config import Settings
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2
from app.services.mcp_access_policy import bearer_providers, parse_bearer_providers
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ISSUER, REDIRECT, _Base, dpop_proof, mcp_settings


class BearerProviderSettingTests(unittest.TestCase):
    def test_default_is_empty_and_dpop_is_required_for_everyone(self):
        self.assertEqual(Settings(_env_file=None).MCP_OAUTH_BEARER_PROVIDERS, "")
        self.assertEqual(bearer_providers(object()), frozenset())

    def test_known_names_are_accepted_and_normalized(self):
        settings = Settings(_env_file=None, MCP_OAUTH_BEARER_PROVIDERS=" gemini, claude ,")
        self.assertEqual(settings.MCP_OAUTH_BEARER_PROVIDERS, "claude,gemini")
        self.assertEqual(bearer_providers(settings), frozenset({"claude", "gemini"}))

    def test_unknown_names_fail_settings_validation(self):
        for raw in ("claude,copilot", "hub_agent", "Claude", "chatgpt;gemini", "*"):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                Settings(_env_file=None, MCP_OAUTH_BEARER_PROVIDERS=raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_bearer_providers(raw)

    def test_invalid_runtime_value_fails_closed(self):
        self.assertEqual(bearer_providers(mcp_settings(MCP_OAUTH_BEARER_PROVIDERS="claude,copilot")), frozenset())


class BearerProviderTokenRouteTests(_Base):
    def _exchange(self, http, code, *, dpop: str | None):
        headers = {"x-forwarded-for": "203.0.113.10"}
        if dpop is not None:
            headers["DPoP"] = dpop
        return http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers=headers)

    def _claims(self, token: str) -> dict:
        public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(self.service.jwks()["keys"][0]))
        return jwt.decode(token, public, algorithms=["RS256"], audience="renphil-data-mcp", issuer=ISSUER)

    def test_listed_provider_without_dpop_gets_an_unbound_bearer_token(self):
        self.settings = mcp_settings(MCP_OAUTH_BEARER_PROVIDERS="claude")
        http = self.client()
        issued = self._exchange(http, self.issue_code(http), dpop=None)
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.json()["token_type"], "Bearer")
        claims = self._claims(issued.json()["access_token"])
        self.assertNotIn("cnf", claims)
        self.assertEqual(claims["renphil_client_kind"], "claude")

    def test_listed_provider_with_dpop_is_still_bound_never_downgraded(self):
        self.settings = mcp_settings(MCP_OAUTH_BEARER_PROVIDERS="claude")
        http = self.client()
        issued = self._exchange(http, self.issue_code(http), dpop=dpop_proof(f"{ISSUER}/token"))
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.json()["token_type"], "DPoP")
        self.assertIn("jkt", self._claims(issued.json()["access_token"])["cnf"])

    def test_listed_provider_with_an_invalid_or_empty_dpop_header_is_rejected(self):
        self.settings = mcp_settings(MCP_OAUTH_BEARER_PROVIDERS="claude")
        http = self.client()
        code = self.issue_code(http)
        for bad in (dpop_proof(f"{ISSUER}/authorize"), "not-a-proof", ""):
            with self.subTest(bad=bad):
                refused = self._exchange(http, code, dpop=bad)
                self.assertEqual(refused.status_code, 400)
                self.assertEqual(refused.json()["error"], "invalid_dpop_proof")
        # The code was never consumed by a refused request.
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)

    def test_provider_not_listed_still_requires_dpop(self):
        self.settings = mcp_settings(MCP_OAUTH_BEARER_PROVIDERS="chatgpt,gemini")
        http = self.client()
        code = self.issue_code(http)
        refused = self._exchange(http, code, dpop=None)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json(), {"error": "invalid_dpop_proof", "error_description": "A DPoP proof is required."})
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)

    def test_default_configuration_requires_dpop(self):
        http = self.client()
        refused = self._exchange(http, self.issue_code(http), dpop=None)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"], "invalid_dpop_proof")

    def test_service_never_issues_unbound_tokens_unless_the_route_allows_it(self):
        from app.services.mcp_oauth_service import McpOAuthError

        http = self.client()
        code = self.issue_code(http)
        with self.Session() as session, self.assertRaises(McpOAuthError):
            self.service.exchange_code(
                session, code=code, client_id=CLAUDE_CLIENT, redirect_uri=REDIRECT, code_verifier="v" * 64,
                dpop_jkt=None, client_kind="claude",
            )


if __name__ == "__main__":
    unittest.main()
