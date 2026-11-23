"""DPoP proof replay protection for the Hub MCP OAuth token endpoint.

RFC 9449 §11.1: a DPoP proof is single-use. Hub reserves each verified token
request proof's ``jkt`` + ``jti`` exactly once, for slightly longer than the
window in which ``verify_token_request_proof`` accepts the proof, in the same
shared store the MCP OAuth rate limiter uses (Upstash Redis, or the
single-process store for a loopback issuer). The reservation reuses the
limiter's atomic ``INCR``/``PEXPIRE`` script with a limit of one, which is the
same contract as ``SET key NX EX window``: the first use succeeds, every
replay inside the window is refused.

Failure posture is the limiter's: if the store cannot answer, callers must
deny (``RateLimitStoreUnavailable``). Neither the ``jti`` nor the key
thumbprint is stored or logged verbatim; the key is a keyed HMAC digest.
"""

from __future__ import annotations

import hashlib
import hmac

from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.mcp_oauth_dpop import DPoPTokenRequestProof

_KEY_LABEL = b"renphil-hub-mcp-oauth-dpop-jti-v1"
# verify_token_request_proof accepts a proof up to 300 s old and 5 s in the
# future; keep the reservation a little longer than that whole span.
PROOF_MAX_AGE_SECONDS = 300
RESERVATION_SECONDS = PROOF_MAX_AGE_SECONDS + 10


def _key_material(settings) -> bytes:
    secret = str(getattr(settings, "JWT_SECRET_KEY", "") or "")
    if not secret:
        raise rate_limit.RateLimitStoreUnavailable("replay key material unavailable")
    return hmac.new(secret.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()


def reservation_check(settings, proof: DPoPTokenRequestProof) -> rate_limit.Check:
    key = _key_material(settings)
    environment = hmac.new(key, ("issuer:" + str(getattr(settings, "MCP_OAUTH_ISSUER", "") or "")).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    digest = hmac.new(key, f"{proof.jkt}\x00{proof.jti}".encode("utf-8"), hashlib.sha256).hexdigest()[:48]
    return rate_limit.Check(
        key=f"renphil:mcp-oauth:dpop-jti:v1:{environment}:{digest}",
        limit=1, window=RESERVATION_SECONDS,
    )


async def reserve_proof(store, settings, proof: DPoPTokenRequestProof) -> bool:
    """``True`` on first use, ``False`` for a replay.

    Raises ``RateLimitStoreUnavailable`` when there is no store or it fails.
    """
    if store is None:
        raise rate_limit.RateLimitStoreUnavailable("replay store not configured")
    allowed, _index, _ttl = await store.evaluate([reservation_check(settings, proof)])
    return bool(allowed)


__all__ = ["PROOF_MAX_AGE_SECONDS", "RESERVATION_SECONDS", "reservation_check", "reserve_proof"]
