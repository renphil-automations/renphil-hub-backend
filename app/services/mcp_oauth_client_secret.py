"""Client-secret hashing for confidential MCP OAuth clients.

Some hosted connectors (for example Gemini Enterprise) are registered with a
client ID *and* a client secret. Hub stores only a PBKDF2-HMAC-SHA256 digest
of that secret in the client registry (``MCP_OAUTH_CLIENTS_JSON``), encoded as::

    pbkdf2_sha256$<iterations>$<salt, base64url>$<digest, base64url>

Generate the value with ``scripts/hash_mcp_client_secret.py`` (reads the
secret from stdin without echoing it). Only the standard library is used.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass

ALGORITHM = "pbkdf2_sha256"
# Client secrets are high-entropy random values (the helper script requires
# at least 32 characters), so a slow password KDF adds nothing; ~10k rounds
# keep each token request cheap. Existing hashes up to 600k rounds verify.
DEFAULT_ITERATIONS = 10_000
# Bounded so a registry value can neither weaken the hash nor turn every
# token request into a CPU denial of service.
MIN_ITERATIONS = 10_000
MAX_ITERATIONS = 600_000
MIN_NEW_SECRET_CHARS = 32
MIN_SALT_BYTES = 16
DIGEST_BYTES = 32
MAX_SECRET_CHARS = 512


class ClientSecretHashError(ValueError):
    """An encoded client-secret hash is malformed or outside safe bounds."""


@dataclass(frozen=True)
class ClientSecretHash:
    iterations: int
    salt: bytes
    digest: bytes


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ClientSecretHashError("Client secret hash is malformed.")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ClientSecretHashError("Client secret hash is malformed.") from exc


def parse_client_secret_hash(encoded: object) -> ClientSecretHash:
    if not isinstance(encoded, str):
        raise ClientSecretHashError("Client secret hash is malformed.")
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM or not parts[1].isascii() or not parts[1].isdigit():
        raise ClientSecretHashError("Client secret hash is malformed.")
    iterations = int(parts[1])
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ClientSecretHashError("Client secret hash iterations are outside the allowed range.")
    salt, digest = _b64url_decode(parts[2]), _b64url_decode(parts[3])
    if len(salt) < MIN_SALT_BYTES or len(digest) != DIGEST_BYTES:
        raise ClientSecretHashError("Client secret hash is malformed.")
    return ClientSecretHash(iterations=iterations, salt=salt, digest=digest)


def _derive(secret: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations, dklen=DIGEST_BYTES)


def hash_client_secret(secret: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    if not isinstance(secret, str) or not secret or len(secret) > MAX_SECRET_CHARS:
        raise ClientSecretHashError("Client secret must be 1-512 characters.")
    if not MIN_ITERATIONS <= int(iterations) <= MAX_ITERATIONS:
        raise ClientSecretHashError("Client secret hash iterations are outside the allowed range.")
    salt = secrets.token_bytes(MIN_SALT_BYTES) if salt is None else salt
    if len(salt) < MIN_SALT_BYTES:
        raise ClientSecretHashError("Client secret salt is too short.")
    return f"{ALGORITHM}${int(iterations)}${_b64url(salt)}${_b64url(_derive(secret, salt, int(iterations)))}"


def verify_client_secret(secret: object, encoded: str) -> bool:
    """Constant-time check of ``secret`` against an encoded hash.

    A malformed stored hash or an unusable secret is a failed check, never
    an exception that could reveal more than "invalid client".
    """
    try:
        parsed = parse_client_secret_hash(encoded)
    except ClientSecretHashError:
        return False
    if not isinstance(secret, str) or not secret or len(secret) > MAX_SECRET_CHARS:
        # Still spend comparable work so absence and mismatch look alike.
        _derive("invalid-client-secret", parsed.salt, parsed.iterations)
        return False
    return hmac.compare_digest(_derive(secret, parsed.salt, parsed.iterations), parsed.digest)


__all__ = [
    "ALGORITHM",
    "ClientSecretHash",
    "ClientSecretHashError",
    "DEFAULT_ITERATIONS",
    "hash_client_secret",
    "parse_client_secret_hash",
    "verify_client_secret",
]
