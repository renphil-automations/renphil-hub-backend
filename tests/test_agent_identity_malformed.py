"""Malformed Agent credentials must be a 401, never a 500.

Regression for QA 2026-10-25 (FIX-8): a non-ASCII X-Sync-Token reached
hmac.compare_digest(str, str), which raises TypeError for non-ASCII text, so
/mcp/oauth/agent-token answered 500 instead of 401.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.services.agent_identity import require_agent_sync_token, verify_agent_identity

SETTINGS = SimpleNamespace(AGENT_SYNC_TOKEN="shared-token")


class MalformedAgentCredentialTests(unittest.TestCase):
    def _status(self, call) -> int:
        with patch("app.services.agent_identity.get_settings", return_value=SETTINGS):
            with self.assertRaises(HTTPException) as denied:
                call()
        return denied.exception.status_code

    def test_non_ascii_sync_token_is_401(self) -> None:
        # Starlette decodes header bytes as latin-1, so these arrive as non-ASCII str.
        for value in ("é" * 8, "shared-tokené", "☃"):
            with self.subTest(value=value):
                self.assertEqual(self._status(lambda: require_agent_sync_token(value)), 401)

    def test_non_ascii_identity_signature_is_401(self) -> None:
        self.assertEqual(self._status(lambda: verify_agent_identity("eyJ2IjoxfQ", "é" * 64)), 401)

    def test_matching_sync_token_still_accepted(self) -> None:
        with patch("app.services.agent_identity.get_settings", return_value=SETTINGS):
            self.assertIsNone(require_agent_sync_token("shared-token"))

    def test_sibling_sync_token_checks_are_401_for_non_ascii(self) -> None:
        # Independent review of FIX-8: calendar and airtable repeat the same check.
        from app.routers import airtable, calendar

        checks = {
            "calendar sync token": ("app.routers.calendar.get_settings", lambda: calendar._require_agent_sync_token("é" * 8)),
            "airtable index sync token": ("app.routers.airtable.get_settings", lambda: airtable._require_airtable_index_sync_token("é" * 8)),
            "airtable personal signature": ("app.routers.airtable.get_settings", lambda: airtable._verify_airtable_personal_identity("eyJ2IjoxfQ", "é" * 64)),
        }
        for name, (target, call) in checks.items():
            with self.subTest(name), patch(target, return_value=SETTINGS):
                with self.assertRaises(HTTPException) as denied:
                    call()
                self.assertEqual(denied.exception.status_code, 401)


class MalformedDPoPClaimTests(unittest.TestCase):
    """A self-signed DPoP proof with non-ASCII htm/htu must be a DPoP error (400), not a 500."""

    TOKEN_ENDPOINT = "https://hub.example/mcp/oauth/token"

    def _proof(self, **claims) -> str:
        import time
        import uuid

        import jwt
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        jwk = {"kty": "EC", "crv": "P-256",
               "x": jwt.utils.base64url_encode(numbers.x.to_bytes(32, "big")).decode("ascii"),
               "y": jwt.utils.base64url_encode(numbers.y.to_bytes(32, "big")).decode("ascii")}
        body = {"jti": uuid.uuid4().hex, "htm": "POST", "htu": self.TOKEN_ENDPOINT, "iat": int(time.time())}
        body.update(claims)
        return jwt.encode(body, key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})

    def test_non_ascii_claims_raise_dpop_error(self) -> None:
        from app.services.mcp_oauth_dpop import McpDPoPError, verify_token_request_proof

        for name, claims in {"htm": {"htm": "PÖST"}, "htu": {"htu": "https://hub.example/mcp/oauth/töken"}}.items():
            with self.subTest(name), self.assertRaises(McpDPoPError):
                verify_token_request_proof(self._proof(**claims), token_endpoint=self.TOKEN_ENDPOINT)

    def test_valid_proof_still_accepted(self) -> None:
        from app.services.mcp_oauth_dpop import verify_token_request_proof

        verify_token_request_proof(self._proof(), token_endpoint=self.TOKEN_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
