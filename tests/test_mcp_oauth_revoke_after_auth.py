"""S8: revoke-on-admission-denied only after client authentication."""

from __future__ import annotations

import json

import tests.test_mcp_oauth_refresh_tokens as refresh_tests
from app.services.mcp_oauth_client_secret import hash_client_secret
from app.services.mcp_oauth_service import LocalMcpOAuthService
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ENABLED_EXTERNAL, mcp_settings


class _Harness(refresh_tests.RefreshEnabledTests):
    pass


for _name in [name for name in dir(refresh_tests.RefreshEnabledTests) if name.startswith("test_")]:
    setattr(_Harness, _name, None)


class RevokeAfterAuthTests(_Harness):
    def test_unauthenticated_or_foreign_callers_cannot_revoke_a_family(self):
        registry = json.loads(self.settings.MCP_OAUTH_CLIENTS_JSON)
        registry[CLAUDE_CLIENT]["client_secret_hash"] = hash_client_secret("s3cret-value", iterations=100_000)
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_CLIENTS_JSON=json.dumps(registry))
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)
        http = self.client()
        token = self._login(http, extra={"client_secret": "s3cret-value"})["refresh_token"]
        self._activate({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
        # No client secret: refused, nothing revoked.
        self.assertEqual(self._refresh(http, token).status_code, 403)
        # A client ID that is not registered at all: refused, nothing revoked.
        self.assertEqual(self._refresh(http, token, client_id="some-unmapped-client").status_code, 403)
        self.assertTrue(all(row.revoked_at is None for row in self._rows()))
        # The authenticated owner presenting it while switched off: revoked.
        self.assertEqual(self._refresh(http, token, extra={"client_secret": "s3cret-value"}).status_code, 403)
        self.assertTrue(all(row.revoked_at is not None for row in self._rows()))
