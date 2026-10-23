"""RFC 9449 DPoP support for the Hub MCP OAuth token endpoint.

External MCP clients may sender-constrain their access token by sending a
``DPoP`` proof JWT with the token request.  Hub validates the proof against
the token endpoint request and binds the issued access token to the proof's
public key through the ``cnf.jkt`` claim (RFC 9449 §5-6).  The standalone MCP
server then requires a fresh proof signed by the same key on every request.

Hub stores nothing for this: the binding lives in the signed access token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt

DPOP_SIGNING_ALGORITHMS: tuple[str, ...] = ("ES256", "RS256", "PS256", "EdDSA")
_MAX_PROOF_CHARS = 8_192
_PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth", "k")


class McpDPoPError(ValueError):
    """A DPoP proof is absent, malformed, stale, or not bound to this request."""


@dataclass(frozen=True)
class DPoPTokenRequestProof:
    jkt: str
    jti: str
    issued_at: int


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 SHA-256 JWK thumbprint, base64url without padding."""
    kty = str(jwk.get("kty") or "")
    if kty == "EC":
        members = {"crv": jwk.get("crv"), "kty": "EC", "x": jwk.get("x"), "y": jwk.get("y")}
    elif kty == "RSA":
        members = {"e": jwk.get("e"), "kty": "RSA", "n": jwk.get("n")}
    elif kty == "OKP":
        members = {"crv": jwk.get("crv"), "kty": "OKP", "x": jwk.get("x")}
    else:
        raise McpDPoPError("DPoP proof key type is not supported.")
    if any(not isinstance(value, str) or not value for value in members.values()):
        raise McpDPoPError("DPoP proof key is incomplete.")
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(hashlib.sha256(canonical).digest())


def canonical_htu(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise McpDPoPError("DPoP proof URL is invalid.")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


def verify_token_request_proof(
    proof: str | None,
    *,
    token_endpoint: str,
    max_age_seconds: int = 300,
    now: int | None = None,
) -> DPoPTokenRequestProof:
    """Validate the proof sent with ``POST <token_endpoint>`` (RFC 9449 §4.3).

    ``ath`` is not expected because no access token exists yet.  The returned
    thumbprint becomes the access token's ``cnf.jkt``.
    """
    raw = str(proof or "").strip()
    if not raw or "," in raw or len(raw) > _MAX_PROOF_CHARS or raw.count(".") != 2:
        raise McpDPoPError("DPoP proof is malformed.")
    try:
        header = jwt.get_unverified_header(raw)
    except jwt.PyJWTError as exc:
        raise McpDPoPError("DPoP proof header is invalid.") from exc
    if str(header.get("typ") or "") != "dpop+jwt":
        raise McpDPoPError("DPoP proof type is invalid.")
    algorithm = str(header.get("alg") or "")
    if algorithm not in DPOP_SIGNING_ALGORITHMS:
        raise McpDPoPError("DPoP proof algorithm is not permitted.")
    jwk = header.get("jwk")
    if not isinstance(jwk, dict) or any(member in jwk for member in _PRIVATE_JWK_MEMBERS):
        raise McpDPoPError("DPoP proof key is invalid.")
    try:
        key = jwt.PyJWK(jwk, algorithm=algorithm).key
        claims = jwt.decode(
            raw, key=key, algorithms=[algorithm],
            options={"require": ["jti", "htm", "htu", "iat"], "verify_aud": False, "verify_exp": False, "verify_iat": False},
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise McpDPoPError("DPoP proof signature is invalid.") from exc
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti.strip() or len(jti) > 256:
        raise McpDPoPError("DPoP proof identifier is invalid.")
    if not hmac.compare_digest(str(claims.get("htm") or "").upper(), "POST"):
        raise McpDPoPError("DPoP proof method binding is invalid.")
    if not hmac.compare_digest(canonical_htu(str(claims.get("htu") or "")), canonical_htu(token_endpoint)):
        raise McpDPoPError("DPoP proof URL binding is invalid.")
    issued_raw = claims.get("iat")
    if isinstance(issued_raw, bool) or not isinstance(issued_raw, (int, float)):
        raise McpDPoPError("DPoP proof issue time is invalid.")
    issued_at = int(issued_raw)
    current = int(time.time() if now is None else now)
    if issued_at <= 0 or current - issued_at > int(max_age_seconds) or issued_at - current > 5:
        raise McpDPoPError("DPoP proof is expired or not yet valid.")
    return DPoPTokenRequestProof(jkt=jwk_thumbprint(jwk), jti=jti.strip(), issued_at=issued_at)


__all__ = [
    "DPOP_SIGNING_ALGORITHMS",
    "DPoPTokenRequestProof",
    "McpDPoPError",
    "canonical_htu",
    "jwk_thumbprint",
    "verify_token_request_proof",
]
