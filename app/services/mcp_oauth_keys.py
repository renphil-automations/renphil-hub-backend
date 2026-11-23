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


def _load_public_key(public_pem: object) -> rsa.RSAPublicKey:
    if not isinstance(public_pem, str) or "PRIVATE KEY" in public_pem.upper():
        # Never parse (or echo) private material supplied by mistake.
        raise McpOAuthKeyError("An additional MCP OAuth JWKS key must be a PEM public key.")
    try:
        public = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    except Exception:
        raise McpOAuthKeyError("An additional MCP OAuth JWKS key is not a valid PEM public key.") from None
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < MIN_RSA_KEY_BITS:
        raise McpOAuthKeyError(f"An additional MCP OAuth JWKS key must be an RSA public key of at least {MIN_RSA_KEY_BITS} bits.")
    return public


def _valid_kid(kid: object) -> bool:
    return isinstance(kid, str) and 0 < len(kid) <= 128 and kid.isascii() and kid.isprintable() and kid == kid.strip()


def parse_additional_public_keys(raw: str | None, *, current_kid: str) -> dict[str, rsa.RSAPublicKey]:
    """Parse ``MCP_OAUTH_SIGNING_PUBLIC_KEYS_NEXT``: ``{"<kid>": "<public PEM>", ...}``.

    These keys are only published in the JWKS (the next key before a
    rotation, or the previous key until its last token expires); Hub never
    signs with them. Each kid must be distinct from the signing kid.
    """
    if raw is None or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise McpOAuthKeyError("MCP_OAUTH_SIGNING_PUBLIC_KEYS_NEXT must be a JSON object of kid -> public PEM.") from None
    if not isinstance(data, dict):
        raise McpOAuthKeyError("MCP_OAUTH_SIGNING_PUBLIC_KEYS_NEXT must be a JSON object of kid -> public PEM.")
    keys: dict[str, rsa.RSAPublicKey] = {}
    for kid, pem in data.items():
        if not _valid_kid(kid):
            raise McpOAuthKeyError("An additional MCP OAuth JWKS key has an invalid kid.")
        if kid == current_kid:
            raise McpOAuthKeyError("An additional MCP OAuth JWKS key reuses the signing key's kid.")
        keys[kid] = _load_public_key(pem)
    return keys


class HubMcpSigningKey:
    """RS256 key held only by Hub; MCP receives its public JWKS representation.

    ``additional_public_keys`` (kid -> RSA public key) are published in the
    JWKS for rotation overlap but never used to sign.
    """

    def __init__(self, *, private_pem: str, key_id: str, additional_public_keys: dict[str, rsa.RSAPublicKey] | None = None):
        if not private_pem.strip() or not key_id.strip():
            raise McpOAuthKeyError("Hub MCP OAuth signing key is not configured.")
        self._private = _load_signing_key(private_pem)
        self.key_id = key_id.strip()
        extra = dict(additional_public_keys or {})
        if self.key_id in extra:
            raise McpOAuthKeyError("An additional MCP OAuth JWKS key reuses the signing key's kid.")
        self._additional = extra

    def sign(self, claims: dict[str, object]) -> str:
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self.key_id, "typ": "at+jwt"})

    @staticmethod
    def _public_jwk(public: rsa.RSAPublicKey, kid: str) -> dict[str, object]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
        # Only the public members are ever published.
        jwk = {"kty": jwk["kty"], "n": jwk["n"], "e": jwk["e"]}
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        return jwk

    def jwks(self) -> dict[str, object]:
        keys = [self._public_jwk(self._private.public_key(), self.key_id)]
        keys.extend(self._public_jwk(public, kid) for kid, public in self._additional.items())
        return {"keys": keys}

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
    HubMcpSigningKey(
        private_pem=private_pem, key_id=key_id,
        additional_public_keys=parse_additional_public_keys(
            getattr(settings, "MCP_OAUTH_SIGNING_PUBLIC_KEYS_NEXT", None), current_kid=key_id,
        ),
    )
