from __future__ import annotations

import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.mcp_oauth_keys import HubMcpSigningKey


class McpOAuthKeyTests(unittest.TestCase):
    def test_rs256_token_and_public_jwks_are_verifiable(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("utf-8")
        key = HubMcpSigningKey(private_pem=pem, key_id="sandbox-2026-10")
        token = key.sign({"sub": "hub-user-1", "iss": "https://hub.example/mcp/oauth", "aud": "mcp", "exp": 4_000_000_000})
        header = jwt.get_unverified_header(token)
        self.assertEqual((header["alg"], header["kid"]), ("RS256", "sandbox-2026-10"))
        jwk = key.jwks()["keys"][0]
        public = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        claims = jwt.decode(token, public, algorithms=["RS256"], audience="mcp", issuer="https://hub.example/mcp/oauth")
        self.assertEqual(claims["sub"], "hub-user-1")
        self.assertNotIn("PRIVATE", str(key.jwks()))


def _pem(private) -> str:
    return private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("utf-8")


class SigningKeyStartupValidationTests(unittest.TestCase):
    """K-2: an unusable signing key fails start-up, only when MCP OAuth is on."""

    def _settings(self, **overrides):
        from app.config import Settings

        return Settings(_env_file=None, **overrides)

    def test_weak_non_rsa_garbage_or_missing_keys_fail_startup_without_leaking_material(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        from app.services.mcp_oauth_keys import McpOAuthKeyError

        weak = _pem(rsa.generate_private_key(public_exponent=65537, key_size=1024))
        cases = {
            "rsa-1024": (weak, "k"),
            "ec": (_pem(ec.generate_private_key(ec.SECP256R1())), "k"),
            "garbage": ("-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----", "k"),
            "missing-key": ("", "k"),
            "missing-kid": (_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)), ""),
        }
        for name, (pem, kid) in cases.items():
            with self.subTest(name), self.assertRaises(McpOAuthKeyError) as caught:
                self._settings(MCP_OAUTH_ENABLED=True, MCP_OAUTH_SIGNING_PRIVATE_KEY=pem, MCP_OAUTH_SIGNING_KEY_ID=kid)
            message = str(caught.exception)
            self.assertNotIn("BEGIN", message)
            self.assertNotIn("not-a-key", message)
            self.assertLess(len(message), 200)

    def test_valid_key_and_disabled_oauth_start(self):
        good = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
        self.assertTrue(self._settings(MCP_OAUTH_ENABLED=True, MCP_OAUTH_SIGNING_PRIVATE_KEY=good, MCP_OAUTH_SIGNING_KEY_ID="k").MCP_OAUTH_ENABLED)
        # Disabled: nothing is validated, even a weak key.
        weak = _pem(rsa.generate_private_key(public_exponent=65537, key_size=1024))
        self._settings(MCP_OAUTH_ENABLED=False, MCP_OAUTH_SIGNING_PRIVATE_KEY=weak, MCP_OAUTH_SIGNING_KEY_ID="k")
        self._settings()

    def test_signing_key_object_rejects_weak_keys(self):
        from app.services.mcp_oauth_keys import McpOAuthKeyError

        with self.assertRaises(McpOAuthKeyError):
            HubMcpSigningKey(private_pem=_pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)), key_id="k")


if __name__ == "__main__":
    unittest.main()
