"""Asymmetric Hub signing and JWKS publication for MCP OAuth tokens."""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
from cryptography.hazmat.primitives import serialization


class McpOAuthKeyError(RuntimeError):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class HubMcpSigningKey:
    """RS256 key held only by Hub; MCP receives its public JWKS representation."""

    def __init__(self, *, private_pem: str, key_id: str):
        if not private_pem.strip() or not key_id.strip():
            raise McpOAuthKeyError("Hub MCP OAuth signing key is not configured.")
        try:
            self._private = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
        except Exception as exc:
            raise McpOAuthKeyError("Hub MCP OAuth signing key is invalid.") from exc
        self.key_id = key_id.strip()

    def sign(self, claims: dict[str, object]) -> str:
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self.key_id, "typ": "at+jwt"})

    def jwks(self) -> dict[str, object]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._private.public_key()))
        jwk.update({"kid": self.key_id, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    @property
    def fingerprint(self) -> str:
        public = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return hashlib.sha256(public).hexdigest()
