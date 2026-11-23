"""Asymmetric Hub signing and JWKS publication for MCP OAuth tokens."""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# RS256 with RSA keys of at least 2048 bits (RFC 7518 §3.3).
MIN_RSA_KEY_BITS = 2048


class McpOAuthKeyError(RuntimeError):
    """The signing configuration is unusable. Messages never contain key material."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_signing_key(private_pem: str) -> rsa.RSAPrivateKey:
    """Load and prove the private key: RSA, >= 2048 bits, able to sign RS256."""
    try:
        private = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    except Exception:
        raise McpOAuthKeyError("Hub MCP OAuth signing key is invalid.") from None
    if not isinstance(private, rsa.RSAPrivateKey):
        raise McpOAuthKeyError("Hub MCP OAuth signing key must be an RSA private key (RS256).")
    if private.key_size < MIN_RSA_KEY_BITS:
        raise McpOAuthKeyError(f"Hub MCP OAuth signing key must be at least {MIN_RSA_KEY_BITS} bits.")
    try:
        probe = jwt.encode({"probe": True}, private, algorithm="RS256")
        jwt.decode(probe, private.public_key(), algorithms=["RS256"])
    except Exception:
        raise McpOAuthKeyError("Hub MCP OAuth signing key cannot sign RS256 tokens.") from None
    return private


class HubMcpSigningKey:
    """RS256 key held only by Hub; MCP receives its public JWKS representation."""

    def __init__(self, *, private_pem: str, key_id: str):
        if not private_pem.strip() or not key_id.strip():
            raise McpOAuthKeyError("Hub MCP OAuth signing key is not configured.")
        self._private = _load_signing_key(private_pem)
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


def validate_signing_configuration(settings) -> None:
    """Start-up check (only when MCP OAuth is enabled); raises ``McpOAuthKeyError``."""
    if not getattr(settings, "MCP_OAUTH_ENABLED", False):
        return
    private_pem = (getattr(settings, "MCP_OAUTH_SIGNING_PRIVATE_KEY", None) or "").strip()
    key_id = (getattr(settings, "MCP_OAUTH_SIGNING_KEY_ID", None) or "").strip()
    if not private_pem or not key_id:
        raise McpOAuthKeyError("MCP_OAUTH_ENABLED=true requires MCP_OAUTH_SIGNING_PRIVATE_KEY and MCP_OAUTH_SIGNING_KEY_ID.")
    HubMcpSigningKey(private_pem=private_pem, key_id=key_id)
