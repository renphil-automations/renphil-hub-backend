"""S5: refresh_token grants have their own limiter budget and failure counting."""

from __future__ import annotations

import tests.test_mcp_oauth_refresh_tokens as refresh_tests
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, REDIRECT


class _Harness(refresh_tests.RefreshEnabledTests):
    pass


for _name in [name for name in dir(refresh_tests.RefreshEnabledTests) if name.startswith("test_")]:
    setattr(_Harness, _name, None)

IP = "203.0.113.50"


class RefreshBudgetTests(_Harness):
    def _bad_code(self, http):
        return http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": "wrong", "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"x-forwarded-for": IP})

    def _refresh_ip(self, http, token):
        return http.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": token, "client_id": CLAUDE_CLIENT,
        }, headers={"DPoP": self.signer.proof(), "x-forwarded-for": IP})

    def test_code_exchange_failure_lockout_does_not_block_refresh(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        statuses = [self._bad_code(http).status_code for _ in range(25)]
        self.assertEqual(statuses[-1], 429)  # the code-exchange lockout engaged
        refreshed = self._refresh_ip(http, token)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)

    def test_failed_refreshes_do_not_count_toward_the_code_exchange_lockout(self):
        http = self.client()
        for _ in range(25):
            self.assertEqual(self._refresh_ip(http, "not-a-token").status_code, 400)
        # Code exchanges from the same network are not locked out.
        self.assertEqual(self._bad_code(http).status_code, 400)
        failure_keys = [key for keys, args in self.redis.calls for key, mode in zip(keys, args[0::3]) if ":failures:" in key and mode == "i"]
        self.assertEqual(len(failure_keys), 1)  # only the single failed code exchange counted

    def test_refresh_has_its_own_per_network_budget_and_keys(self):
        http = self.client()
        statuses = [self._refresh_ip(http, "not-a-token").status_code for _ in range(31)]
        self.assertEqual(statuses[:30], [400] * 30)
        self.assertEqual(statuses[30], 429)
        refresh_keys = {key for keys, _ in self.redis.calls for key in keys if ":refresh:" in key}
        self.assertTrue(any(key.endswith(":refresh:client:claude") for key in refresh_keys), refresh_keys)
        self.assertTrue(any(":refresh:network:" in key for key in refresh_keys))
        # Code exchanges still have their own budget on this network.
        self.assertEqual(self._bad_code(http).status_code, 400)
