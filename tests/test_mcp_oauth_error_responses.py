"""RFC 6749 §5.2-shaped error responses from the Hub MCP authorization server.

Hosted connectors (Claude, ChatGPT, Gemini) parse a top-level JSON body
``{"error": ..., "error_description"?: ...}`` with ``Cache-Control: no-store``
and ``Pragma: no-cache``. FastAPI's default ``{"detail": ...}`` wrapper is not
an OAuth error and must never be returned by the OAuth endpoints. Success
responses, the consent pages and the internal agent-token contract are
unchanged.
"""

from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.database import BaseV2
from app.db_v2.models.mcp_oauth import McpOAuthRefreshTokenV2
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.mcp_oauth_client_secret import hash_client_secret
from app.services.mcp_oauth_service import LocalMcpOAuthService, McpOAuthStorageError
from tests.test_mcp_oauth_dpop_replay import JtiStoreDown, Signer
from tests.test_mcp_oauth_rate_limit import (
    _ORIGINAL_SERVICE, CLAUDE_CLIENT, ENABLED_EXTERNAL, ISSUER, REDIRECT, _Base, dpop_proof, mcp_settings,
)

ALLOWED_KEYS = {"error", "error_description"}


class OAuthErrorShapeTests(_Base):
    def assertOAuthError(self, response, status: int, error: str):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body.get("error"), error, body)
        self.assertLessEqual(set(body), ALLOWED_KEYS, body)
        if "error_description" in body:
            self.assertIsInstance(body["error_description"], str)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("pragma"), "no-cache")
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        return body

    def token(self, http, data=None, headers=None, dpop=True):
        merged = {"DPoP": dpop_proof(f"{ISSUER}/token")} if dpop else {}
        merged.update(headers or {})
        return http.post("/mcp/oauth/token", data=data or {}, headers=merged)

    def code_form(self, code, **extra):
        return {"grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
                "redirect_uri": REDIRECT, "code_verifier": "v" * 64, **extra}

    # -- /token -----------------------------------------------------------
    def test_unsupported_and_missing_grant_type(self):
        http = self.client()
        self.assertOAuthError(self.token(http, {"grant_type": "password", "client_id": CLAUDE_CLIENT}), 400, "unsupported_grant_type")
        self.assertOAuthError(self.token(http, {"grant_type": "refresh_token", "refresh_token": "x", "client_id": CLAUDE_CLIENT}), 400, "unsupported_grant_type")
        self.assertOAuthError(self.token(http, {"client_id": CLAUDE_CLIENT}), 400, "invalid_request")
        self.assertOAuthError(self.token(http, {}), 400, "invalid_request")

    def test_missing_parameters_are_invalid_request(self):
        http = self.client()
        for missing in ("code", "redirect_uri", "code_verifier", "client_id"):
            with self.subTest(missing=missing):
                form = self.code_form("some-code")
                del form[missing]
                self.assertOAuthError(self.token(http, form), 400, "invalid_request")

    def test_bad_or_replayed_code_is_invalid_grant(self):
        http = self.client()
        self.assertOAuthError(self.token(http, self.code_form("not-a-code")), 400, "invalid_grant")
        code = self.issue_code(http)
        self.assertOAuthError(self.token(http, self.code_form(code, code_verifier="w" * 64)), 400, "invalid_grant")
        self.assertOAuthError(self.token(http, self.code_form(code, code_verifier="short")), 400, "invalid_grant")
        self.assertEqual(self.token(http, self.code_form(code)).status_code, 200)
        self.assertOAuthError(self.token(http, self.code_form(code)), 400, "invalid_grant")

    def test_dpop_errors(self):
        http = self.client()
        code = self.issue_code(http)
        self.assertOAuthError(self.token(http, self.code_form(code), dpop=False), 400, "invalid_dpop_proof")
        self.assertOAuthError(self.token(http, self.code_form(code), headers={"DPoP": "junk"}, dpop=False), 400, "invalid_dpop_proof")
        proof = Signer().proof()
        self.assertEqual(self.token(http, self.code_form(code), headers={"DPoP": proof}, dpop=False).status_code, 200)
        replay = self.token(http, self.code_form(self.issue_code(http)), headers={"DPoP": proof}, dpop=False)
        self.assertOAuthError(replay, 400, "invalid_dpop_proof")

    def test_invalid_target(self):
        self.settings = mcp_settings(MCP_OAUTH_RESOURCE_URL="https://mcp.example/mcp")
        http = self.client()
        self.assertOAuthError(self.token(http, self.code_form("c", resource="https://other.example/")), 400, "invalid_target")

    def test_invalid_client_is_401_with_basic_challenge(self):
        registry = json.loads(self.settings.MCP_OAUTH_CLIENTS_JSON)
        registry[CLAUDE_CLIENT]["client_secret_hash"] = hash_client_secret("right-secret", iterations=100_000)
        self.settings = mcp_settings(MCP_OAUTH_CLIENTS_JSON=json.dumps(registry))
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)
        http = self.client()
        wrong = self.token(http, self.code_form("c", client_secret="wrong"))
        self.assertOAuthError(wrong, 401, "invalid_client")
        self.assertIn("Basic", wrong.headers["www-authenticate"])
        basic = "Basic " + base64.b64encode(b"x").decode()
        self.assertOAuthError(self.token(http, {"grant_type": "authorization_code"}, headers={"Authorization": basic}), 401, "invalid_client")

    def test_switched_off_provider_stays_403_and_agent_client_is_unauthorized(self):
        http = self.client()
        self._activate({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
        self.assertOAuthError(self.token(http, self.code_form("c")), 403, "unauthorized_client")
        self._activate(ENABLED_EXTERNAL)
        self.assertOAuthError(self.token(http, self.code_form("c", client_id="renphil-agent")), 400, "unauthorized_client")

    def test_refresh_grant_errors(self):
        BaseV2.metadata.create_all(self.engine, tables=[McpOAuthRefreshTokenV2.__table__])
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True)
        self.service.refresh_tokens_enabled = True
        http = self.client()
        base = {"grant_type": "refresh_token", "client_id": CLAUDE_CLIENT}
        self.assertOAuthError(self.token(http, base), 400, "invalid_request")
        self.assertOAuthError(self.token(http, {**base, "refresh_token": "unknown"}), 400, "invalid_grant")
        self.assertOAuthError(self.token(http, {**base, "refresh_token": "unknown", "scope": "admin"}), 400, "invalid_scope")

    def test_store_outage_is_503_with_retry_after(self):
        self.redis = JtiStoreDown()
        self.store_override = rate_limit.UpstashRateLimitStore(url="https://example-upstash.invalid", token="t", client=self.redis)
        body = self.assertOAuthError(self.token(self.client(), self.code_form("c")), 503, "temporarily_unavailable")
        self.assertTrue(body)
        self.redis.down = True
        limited = self.token(self.client(), self.code_form("c"))
        self.assertOAuthError(limited, 503, "temporarily_unavailable")
        self.assertEqual(limited.headers["retry-after"], str(rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS))

    def test_storage_outage_during_exchange_is_503_not_invalid_grant(self):
        http = self.client()
        with patch.object(self.service, "exchange_code", side_effect=McpOAuthStorageError("down")):
            response = self.token(http, self.code_form("c"))
        self.assertOAuthError(response, 503, "temporarily_unavailable")
        self.assertIn("retry-after", response.headers)

    def test_unconfigured_service_is_temporarily_unavailable(self):
        def unconfigured():
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="MCP OAuth is not configured.")

        with patch.object(mcp_oauth_router, "_service", unconfigured):
            self.assertOAuthError(self.token(self.client(), self.code_form("c")), 503, "temporarily_unavailable")

    def test_throttling_uses_the_same_shape(self):
        http = self.client()
        for _ in range(30):
            self.token(http, self.code_form("bad"), dpop=False)
        throttled = self.token(http, self.code_form("bad"), dpop=False)
        self.assertOAuthError(throttled, 429, "temporarily_unavailable")
        self.assertIn("retry-after", throttled.headers)

    def test_success_response_is_unchanged(self):
        http = self.client()
        issued = self.token(http, self.code_form(self.issue_code(http)))
        self.assertEqual(issued.status_code, 200)
        self.assertEqual(set(issued.json()), {"access_token", "token_type", "expires_in", "scope"})

    def test_disabled_oauth_stays_an_indistinguishable_404(self):
        self.settings = mcp_settings(MCP_OAUTH_ENABLED=False)
        with patch.object(mcp_oauth_router, "_service", _ORIGINAL_SERVICE.__wrapped__):
            response = self.token(self.client(), self.code_form("c"))
        self.assertEqual(response.status_code, 404)

    # -- /authorize -------------------------------------------------------
    def test_authorize_errors_use_the_oauth_shape_without_redirecting(self):
        http = self.client()
        params = self.authorize_params()
        cases = [
            ({**params, "response_type": "token"}, 400, "unsupported_response_type"),
            ({**params, "client_id": "renphil-agent"}, 400, "unauthorized_client"),
            ({**params, "code_challenge_method": "plain"}, 400, "invalid_request"),
            ({key: value for key, value in params.items() if key != "state"}, 400, "invalid_request"),
            ({key: value for key, value in params.items() if key != "client_id"}, 400, "invalid_request"),
            ({**params, "resource": "https://elsewhere.example/"}, 400, "invalid_target"),
        ]
        for query, status, error in cases:
            with self.subTest(error=error, query=sorted(query)):
                response = http.get("/mcp/oauth/authorize", params=query)
                self.assertOAuthError(response, status, error)
                self.assertNotIn("location", response.headers)
        self._activate({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
        self.assertOAuthError(http.get("/mcp/oauth/authorize", params=params), 403, "access_denied")

    # -- registration -------------------------------------------------------
    def test_registration_disabled_body_and_headers(self):
        http = self.client()
        for path in ("/mcp/oauth/register", "/register"):
            with self.subTest(path=path):
                self.assertOAuthError(http.post(path, json={}), 400, "invalid_client_metadata")

    # -- agent-token contract is unchanged ---------------------------------------
    def test_agent_token_contract_is_unchanged(self):
        from app.dependencies import get_current_hub_user
        from app.services.agent_identity import require_agent_sync_token

        app = self.app()
        app.dependency_overrides[require_agent_sync_token] = lambda: None
        app.dependency_overrides[get_current_hub_user] = lambda: SimpleNamespace(email="m@renphil.org", hub_user_id=42)
        self._activate({"bot_enabled": False})
        from fastapi.testclient import TestClient

        response = TestClient(app).post("/mcp/oauth/agent-token")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Hub AI is disabled."})


if __name__ == "__main__":
    unittest.main()
