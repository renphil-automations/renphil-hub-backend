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


if __name__ == "__main__":
    unittest.main()
