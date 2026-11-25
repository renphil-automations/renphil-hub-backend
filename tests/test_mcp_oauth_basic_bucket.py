"""S4: client_secret_basic requests use their provider's rate-limit bucket."""

from __future__ import annotations

import base64
import json
from urllib.parse import quote_plus

from app.services.mcp_oauth_client_secret import hash_client_secret
from app.services.mcp_oauth_service import LocalMcpOAuthService
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, REDIRECT, _Base, mcp_settings


def basic(client_id: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()).decode()


class BasicAuthBucketTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        registry = json.loads(self.settings.MCP_OAUTH_CLIENTS_JSON)
        registry[CLAUDE_CLIENT]["client_secret_hash"] = hash_client_secret("s3cret-value", iterations=100_000)
        self.settings = mcp_settings(MCP_OAUTH_CLIENTS_JSON=json.dumps(registry))
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)

    def _client_keys(self, headers):
        self.redis.calls.clear()
        self.client().post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": "x", "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers=headers)
        return [key for keys, _ in self.redis.calls for key in keys if ":client:" in key]

    def test_basic_credentials_select_the_provider_bucket(self):
        keys = self._client_keys({"Authorization": basic(CLAUDE_CLIENT, "s3cret-value")})
        self.assertTrue(keys)
        self.assertTrue(all(key.endswith(":client:claude") for key in keys), keys)

    def test_malformed_or_unknown_basic_credentials_share_the_unknown_bucket(self):
        for header in ("Basic !!!", "Basic " + base64.b64encode(b"no-separator").decode(), basic("not-registered", "x"), "Bearer abc"):
            with self.subTest(header=header):
                keys = self._client_keys({"Authorization": header})
                self.assertTrue(all(key.endswith(":client:unknown") for key in keys), keys)
