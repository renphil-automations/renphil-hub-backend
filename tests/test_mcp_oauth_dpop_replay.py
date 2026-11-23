"""H5: DPoP proof (jkt + jti) replay protection at /mcp/oauth/token."""

from __future__ import annotations

import time
import unittest
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.mcp_oauth_dpop import jwk_thumbprint
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, FakeUpstash, ISSUER, REDIRECT, _Base, mcp_settings

TOKEN_ENDPOINT = f"{ISSUER}/token"


class Signer:
    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        self.jwk = {
            "kty": "EC", "crv": "P-256",
            "x": jwt.utils.base64url_encode(numbers.x.to_bytes(32, "big")).decode(),
            "y": jwt.utils.base64url_encode(numbers.y.to_bytes(32, "big")).decode(),
        }

    def proof(self, jti: str | None = None) -> str:
        return jwt.encode(
            {"jti": jti or uuid.uuid4().hex, "htm": "POST", "htu": TOKEN_ENDPOINT, "iat": int(time.time())},
            self.private, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": self.jwk},
        )


class JtiStoreDown(FakeUpstash):
    """The limiter's buckets answer; only the replay reservation fails."""

    async def eval(self, script, keys, args):
        if any(":dpop-jti:" in key for key in keys):
            raise ConnectionError("upstash unreachable")
        return await super().eval(script, keys, args)


class DPoPReplayTests(_Base):
    def _token(self, http, code, proof, *, verifier="v" * 64):
        return http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": verifier,
        }, headers={"DPoP": proof})

    def test_a_proof_is_accepted_once_and_a_replay_never_consumes_a_code(self):
        http = self.client()
        signer = Signer()
        proof = signer.proof()
        self.assertEqual(self._token(http, self.issue_code(http), proof).status_code, 200)
        second_code = self.issue_code(http)
        replay = self._token(http, second_code, proof)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["detail"]["error"], "invalid_dpop_proof")
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        # A fresh proof from the same key still works.
        self.assertEqual(self._token(http, second_code, signer.proof()).status_code, 200)

    def test_a_proof_used_by_a_failed_exchange_cannot_be_reused(self):
        http = self.client()
        code = self.issue_code(http)
        proof = Signer().proof()
        self.assertEqual(self._token(http, code, proof, verifier="w" * 64).status_code, 400)
        self.assertEqual(self._token(http, code, proof).status_code, 400)
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)

    def test_same_jti_from_a_different_key_is_a_different_proof(self):
        http = self.client()
        self.assertEqual(self._token(http, self.issue_code(http), Signer().proof("shared-jti")).status_code, 200)
        self.assertEqual(self._token(http, self.issue_code(http), Signer().proof("shared-jti")).status_code, 200)

    def test_invalid_proofs_are_not_reserved(self):
        http = self.client()
        signer = Signer()
        forged = jwt.encode(
            {"jti": "victim-jti", "htm": "POST", "htu": TOKEN_ENDPOINT, "iat": int(time.time())},
            ec.generate_private_key(ec.SECP256R1()), algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": signer.jwk},
        )
        code = self.issue_code(http)
        self.assertEqual(self._token(http, code, forged).status_code, 400)
        self.assertFalse(any(":dpop-jti:" in key for keys, _ in self.redis.calls for key in keys))
        self.assertEqual(self._token(http, code, signer.proof("victim-jti")).status_code, 200)

    def test_reservation_keys_hold_no_raw_jti_or_thumbprint(self):
        http = self.client()
        signer = Signer()
        self._token(http, self.issue_code(http), signer.proof("raw-jti-value"))
        keys = [key for keys, _ in self.redis.calls for key in keys if ":dpop-jti:" in key]
        self.assertEqual(len(keys), 1)
        self.assertNotIn("raw-jti-value", keys[0])
        self.assertNotIn(jwk_thumbprint(signer.jwk), keys[0])
        args = next(args for call_keys, args in self.redis.calls if keys[0] in call_keys)
        self.assertEqual(args, ["i", "1", "310000"])

    def test_replay_store_outage_fails_closed_like_the_limiter(self):
        self.redis = JtiStoreDown()
        self.store = self.store_override = rate_limit.UpstashRateLimitStore(
            url="https://example-upstash.invalid", token="t", client=self.redis,
        )
        http = self.client()
        code = self.issue_code(http)
        with self.assertLogs("app.routers.mcp_oauth", level="WARNING") as logs:
            refused = self._token(http, code, Signer().proof())
        self.assertEqual(refused.status_code, 503)
        self.assertEqual(refused.json()["error"], "temporarily_unavailable")
        self.assertEqual(refused.headers["retry-after"], str(rate_limit.UNAVAILABLE_RETRY_AFTER_SECONDS))
        self.assertIn("dpop_replay_store_unavailable", "\n".join(logs.output))
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)

    def test_loopback_local_mode_uses_the_local_only_store(self):
        self.settings = mcp_settings(UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None)
        self.store_override = rate_limit.LocalOnlyRateLimitStore()
        http = self.client()
        proof = Signer().proof()
        self.assertEqual(self._token(http, self.issue_code(http), proof).status_code, 200)
        self.assertEqual(self._token(http, self.issue_code(http), proof).status_code, 400)

    def test_missing_jwt_secret_fails_closed(self):
        self.settings = mcp_settings(JWT_SECRET_KEY="")
        http = self.client()
        # The limiter itself already fails closed without key material.
        self.assertEqual(self._token(http, "code", Signer().proof()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
