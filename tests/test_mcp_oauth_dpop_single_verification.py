"""S7: the token-request DPoP proof is verified exactly once.

Attack (security review S7/P6): a proof dated slightly in the future failed
verification in the replay-reservation dependency (so nothing was reserved)
but passed the handler's second verification a moment later, so the same
proof could be used twice.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import jwt

import app.routers.mcp_oauth as mcp_oauth_router
from app.services import mcp_oauth_dpop
from tests.test_mcp_oauth_dpop_replay import TOKEN_ENDPOINT, Signer
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, REDIRECT, _Base, mcp_settings


class SingleVerificationTests(_Base):
    def _token(self, http, code, proof):
        return http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": proof})

    def test_future_dated_proof_cannot_be_used_twice_across_a_clock_tick(self):
        base = int(time.time())
        signer = Signer()
        proof = jwt.encode({"jti": uuid.uuid4().hex, "htm": "POST", "htu": TOKEN_ENDPOINT, "iat": base + 6},
                           signer.private, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": signer.jwk})
        http = self.client()
        statuses = []
        for _ in range(2):
            code = self.issue_code(http)
            ticks = iter([base, base + 1, base + 1])

            class Clock:
                @staticmethod
                def time():
                    return next(ticks, base + 1)

            with patch.object(mcp_oauth_dpop, "time", Clock):
                statuses.append(self._token(http, code, proof).status_code)
        self.assertNotIn(200, statuses)
        self.assertEqual(statuses, [400, 400])

    def test_a_proof_is_verified_once_per_request(self):
        http = self.client()
        code = self.issue_code(http)
        with patch.object(mcp_oauth_router, "verify_token_request_proof", wraps=mcp_oauth_router.verify_token_request_proof) as verify:
            issued = self._token(http, code, Signer().proof())
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(verify.call_count, 1)

    def test_an_invalid_header_is_rejected_even_for_a_bearer_provider(self):
        self.settings = mcp_settings(MCP_OAUTH_BEARER_PROVIDERS="claude")
        http = self.client()
        code = self.issue_code(http)
        for bad in ("not-a-proof", " "):
            with self.subTest(bad=bad):
                refused = self._token(http, code, bad)
                self.assertEqual(refused.status_code, 400)
                self.assertEqual(refused.json()["error"], "invalid_dpop_proof")
        # No header at all: the bearer path still works.
        unbound = http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        })
        self.assertEqual(unbound.status_code, 200)
        self.assertEqual(unbound.json()["token_type"], "Bearer")
