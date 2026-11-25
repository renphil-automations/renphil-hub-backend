"""H4: RFC 8707 resource indicators at /authorize and /token."""

from __future__ import annotations

import unittest

from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ISSUER, REDIRECT, _Base, dpop_proof, mcp_settings

RESOURCE = "https://mcp.example/mcp"
INVALID_TARGET = "invalid_target"


class ResourceIndicatorTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.settings = mcp_settings(MCP_OAUTH_RESOURCE_URL=RESOURCE)

    def _authorize(self, http, **extra):
        return http.get("/mcp/oauth/authorize", params={**self.authorize_params(), **extra})

    def _token(self, http, code, **extra):
        return http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64, **extra,
        }, headers={"DPoP": dpop_proof(f"{ISSUER}/token")})

    def test_matching_resource_is_accepted_with_or_without_a_trailing_slash(self):
        http = self.client()
        for value in (RESOURCE, RESOURCE + "/"):
            with self.subTest(value=value):
                self.assertEqual(self._authorize(http, resource=value).status_code, 307)
                issued = self._token(http, self.issue_code(http), resource=value)
                self.assertEqual(issued.status_code, 200, issued.text)

    def test_absent_resource_keeps_the_existing_behaviour(self):
        http = self.client()
        self.assertEqual(self._authorize(http).status_code, 307)
        self.assertEqual(self._token(http, self.issue_code(http)).status_code, 200)

    def test_other_resources_are_invalid_target_at_authorize_without_a_redirect(self):
        http = self.client()
        before = self.db.query(McpOAuthAuthorizationRequestV2).count()
        for value in ("https://mcp.example/other", "https://MCP.example/mcp", "https://mcp.example/mcp//",
                      "https://attacker.example/mcp", "", "https://mcp.example/mcp?x=1"):
            with self.subTest(value=value):
                refused = self._authorize(http, resource=value)
                self.assertEqual(refused.status_code, 400)
                self.assertEqual(refused.json()["error"], INVALID_TARGET)
                self.assertNotIn("location", refused.headers)
        # One valid and one invalid value: still refused.
        refused = http.get("/mcp/oauth/authorize", params=[*self.authorize_params().items(), ("resource", RESOURCE), ("resource", "https://other.example/")])
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.db.query(McpOAuthAuthorizationRequestV2).count(), before)

    def test_other_resources_are_invalid_target_at_token_and_keep_the_code(self):
        http = self.client()
        code = self.issue_code(http)
        refused = self._token(http, code, resource="https://other.example/mcp")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"], INVALID_TARGET)
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        self.assertEqual(self._token(http, code, resource=RESOURCE).status_code, 200)

    def test_any_resource_is_refused_when_no_resource_url_is_configured(self):
        self.settings = mcp_settings()
        http = self.client()
        refused = self._authorize(http, resource=RESOURCE)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["error"], INVALID_TARGET)


if __name__ == "__main__":
    unittest.main()
