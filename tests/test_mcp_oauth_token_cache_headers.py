"""S6: successful /token responses are never cached (RFC 6749 §5.1)."""

from __future__ import annotations

import tests.test_mcp_oauth_refresh_tokens as refresh_tests


class _Harness(refresh_tests.RefreshEnabledTests):
    pass


for _name in [name for name in dir(refresh_tests.RefreshEnabledTests) if name.startswith("test_")]:
    setattr(_Harness, _name, None)


class TokenCacheHeaderTests(_Harness):
    def test_code_exchange_and_refresh_success_carry_no_store_and_no_cache(self):
        http = self.client()
        code = self.issue_code(http)
        issued = http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": refresh_tests.CLAUDE_CLIENT,
            "redirect_uri": refresh_tests.REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": self.signer.proof()})
        self.assertEqual(issued.status_code, 200)
        self.assertEqual(issued.headers.get("cache-control"), "no-store")
        self.assertEqual(issued.headers.get("pragma"), "no-cache")
        refreshed = self._refresh(http, issued.json()["refresh_token"])
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.headers.get("cache-control"), "no-store")
        self.assertEqual(refreshed.headers.get("pragma"), "no-cache")
        # The body is unchanged.
        self.assertEqual(set(refreshed.json()), {"access_token", "token_type", "expires_in", "scope", "refresh_token"})
