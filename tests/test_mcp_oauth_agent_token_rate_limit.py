"""K-4: per-user rate limit on /mcp/oauth/agent-token (shared limiter store)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.dependencies import get_current_hub_user
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.agent_identity import require_agent_sync_token
from app.services.mcp_oauth_service import LocalMcpOAuthService, PersistentMcpOAuthService
from tests.test_mcp_oauth_rate_limit import _Base, mcp_settings

USER_A = SimpleNamespace(email="a.person@renphil.org", hub_user_id=41)
USER_B = SimpleNamespace(email="b.person@renphil.org", hub_user_id=42)


class AgentTokenRateLimitTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._activate({"bot_enabled": True})
        self.current = USER_A
        # The Hub Agent client must be in the service registry.
        self.service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"renphil-agent":{"redirect_uris":["https://hub.example/agent"]}}'),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp", signing_key=self.service._signing_key,
        )

    def http(self) -> TestClient:
        app = self.app()
        app.dependency_overrides[require_agent_sync_token] = lambda: None
        app.dependency_overrides[get_current_hub_user] = lambda: self.current
        return TestClient(app)

    def call(self, http):
        return http.post("/mcp/oauth/agent-token")

    def test_success_contract_is_unchanged_under_the_limit(self):
        response = self.call(self.http())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"access_token", "token_type", "expires_in", "scope"})
        self.assertEqual(response.json()["token_type"], "Bearer")

    def test_default_is_120_per_minute_per_user_then_429_with_retry_after(self):
        http = self.http()
        statuses = [self.call(http).status_code for _ in range(121)]
        self.assertEqual(statuses[:120], [200] * 120)
        limited = self.call(http)
        self.assertEqual(limited.status_code, 429)
        self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)
        self.assertNotIn("access_token", limited.text)
        # Another verified user has an independent budget.
        self.current = USER_B
        self.assertEqual(self.call(http).status_code, 200)

    def test_limit_is_configurable(self):
        self.settings = mcp_settings(MCP_OAUTH_AGENT_TOKEN_RATE_LIMIT_PER_MINUTE=2)
        http = self.http()
        self.assertEqual([self.call(http).status_code for _ in range(3)], [200, 200, 429])

    def test_keys_hold_no_user_identity(self):
        self.call(self.http())
        keys = [key for keys, _ in self.redis.calls for key in keys if ":agent-token:" in key]
        self.assertEqual(len(keys), 1)
        self.assertRegex(keys[0], r"^renphil:mcp-oauth:rl:v1:[0-9a-f]{16}:agent-token:user:[0-9a-f]{40}$")
        self.assertNotIn("a.person", keys[0])

    def test_store_outage_fails_closed_with_503_and_retry_after(self):
        self.redis.down = True
        response = self.call(self.http())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], str(rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS))
        self.store_override = None  # store not configured outside local development
        self.assertEqual(self.call(self.http()).status_code, 503)

    def test_loopback_issuer_uses_the_local_only_store(self):
        self.settings = mcp_settings(MCP_OAUTH_AGENT_TOKEN_RATE_LIMIT_PER_MINUTE=1, MCP_OAUTH_ISSUER="http://127.0.0.1:8000/mcp/oauth",
                                     UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None)
        self.store_override = rate_limit.LocalOnlyRateLimitStore()
        http = self.http()
        self.assertEqual([self.call(http).status_code for _ in range(2)], [200, 429])

    def test_sync_token_is_still_checked_before_the_limiter(self):
        self.redis.down = True
        app = self.app()
        with patch("app.services.agent_identity.get_settings", return_value=SimpleNamespace(AGENT_SYNC_TOKEN="sync-secret")):
            response = TestClient(app).post("/mcp/oauth/agent-token", headers={"X-Sync-Token": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.redis.calls, [])

    def test_disabled_mcp_oauth_never_touches_the_store(self):
        self.settings = mcp_settings(MCP_OAUTH_ENABLED=False)
        self.store_override = None
        response = self.call(self.http())
        self.assertNotEqual(response.status_code, 503)
        self.assertEqual(self.redis.calls, [])


if __name__ == "__main__":
    unittest.main()
