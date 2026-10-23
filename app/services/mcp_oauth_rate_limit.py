"""Abuse protection for Hub's public MCP OAuth endpoints.

Scope is deliberately narrow: only the MCP OAuth authorization server routes
(``/mcp/oauth/authorize``, ``/mcp/oauth/token``, discovery metadata, JWKS and
the disabled registration routes) are limited. Hub Google login (``/auth/*``),
session creation, user access checks and every other Hub API are untouched.

Enforcement is a shared, atomic fixed-window counter in the existing Upstash
Redis (``UPSTASH_REDIS_REST_URL`` / ``UPSTASH_REDIS_REST_TOKEN``), so every
Hub instance shares one budget. One Lua ``EVAL`` evaluates a request's
buckets in order and stops at the first bucket that is over its limit, so a
throttled caller never consumes later shared buckets (the per-client and
global ones), which keeps one abusive network from exhausting the budget of
everyone else.

Privacy: no IP address, email, authorization code, token, PKCE verifier,
redirect URI, client ID or request body is used verbatim in a key, a log line
or a response. The caller's network (IPv4 address, or IPv6 /64) is reduced to
a keyed HMAC-SHA256 digest under a key derived from the existing server-only
``JWT_SECRET_KEY``. Client buckets use only the approved provider name
(``claude``/``chatgpt``/``gemini``) or the single shared ``unknown`` bucket, so
unknown or malformed client IDs cannot create new keys.

Failure posture: if the shared store is unavailable (or not configured)
outside local development, requests are refused with a safe 503 and
``Retry-After``. There is no production in-memory fallback; the in-memory
store exists only for a loopback issuer with no Upstash configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_KEY_LABEL = b"renphil-hub-mcp-oauth-rate-limit-v1"
_STORE_TIMEOUT_SECONDS = 2.0
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
UNAVAILABLE_RETRY_AFTER_SECONDS = 5


@dataclass(frozen=True)
class Bucket:
    """One counter: ``limit`` requests per ``window`` seconds."""

    dimension: str  # "network", "client", "global", "failures"
    limit: int
    window: int
    peek: bool = False  # True: check without incrementing (failure counters)


# Bounded, deliberately conservative limits (requests per window).
#
# * authorize: 60/min per network leaves room for many people behind one
#   corporate NAT (each sign-in is one authorize request), while the shared
#   per-client and global caps stop distributed floods. Unknown or malformed
#   client IDs share one small bucket so they can never exhaust a real
#   provider's budget.
# * token: a legitimate PKCE exchange is one request. 30/min per network,
#   and at most 20 failed exchanges per network per 10 minutes, so repeated
#   code guessing or replay is cut off quickly while a normal exchange (and a
#   few retries) stays reliable.
# * register: dynamic registration is disabled (always 400); strictest limit.
# * discovery / jwks: cheap, cacheable documents; protected from flooding.
MCP_OAUTH_RATE_LIMITS: dict[str, tuple[Bucket, ...]] = {
    "authorize": (
        Bucket("network", 60, 60),
        Bucket("client", 600, 60),
        Bucket("global", 3000, 60),
    ),
    "token": (
        Bucket("failures", 20, 600, peek=True),
        Bucket("network", 30, 60),
        Bucket("client", 300, 60),
        Bucket("global", 1500, 60),
    ),
    "register": (
        Bucket("network", 5, 60),
        Bucket("global", 60, 60),
    ),
    "discovery": (
        Bucket("network", 120, 60),
        Bucket("global", 6000, 60),
    ),
    "jwks": (
        Bucket("network", 120, 60),
        Bucket("global", 6000, 60),
    ),
}
# Per-window limit of the single shared bucket for unknown/malformed clients.
UNKNOWN_CLIENT_LIMITS = {"authorize": 60, "token": 30}
TOKEN_FAILURE_BUCKET = Bucket("failures", 20, 600)


class RateLimitStoreUnavailable(RuntimeError):
    """The shared rate-limit store could not answer; callers must deny."""


@dataclass(frozen=True)
class Check:
    key: str
    limit: int
    window: int
    peek: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_seconds: int = 0
    dimension: str | None = None


class RateLimitStore(Protocol):
    async def evaluate(self, checks: Sequence[Check]) -> tuple[bool, int, int]: ...


# Evaluate buckets in order; stop at the first one over its limit. Returns
# {allowed (1/0), 1-based index of the denying bucket, its remaining ms}.
_EVALUATE_SCRIPT = (
    "for i = 1, #KEYS do "
    "local base = (i - 1) * 3 "
    "local peek = ARGV[base + 1] == 'p' "
    "local limit = tonumber(ARGV[base + 2]) "
    "local window = tonumber(ARGV[base + 3]) "
    "local count "
    "if peek then count = tonumber(redis.call('GET', KEYS[i]) or '0') + 1 "
    "else count = redis.call('INCR', KEYS[i]) end "
    "local ttl = redis.call('PTTL', KEYS[i]) "
    "if (not peek) and ttl < 0 then redis.call('PEXPIRE', KEYS[i], window) ttl = window end "
    "if ttl < 0 then ttl = window end "
    "if count > limit then return {0, i, ttl} end "
    "end "
    "return {1, 0, 0}"
)


class UpstashRateLimitStore:
    """Atomic shared counters in Upstash Redis via one ``EVAL`` per check."""

    def __init__(self, *, url: str, token: str, client=None, timeout_seconds: float = _STORE_TIMEOUT_SECONDS) -> None:
        self._url = url
        self._token = token
        self._client = client
        self._timeout = float(timeout_seconds)

    def _redis(self):
        if self._client is None:
            from upstash_redis.asyncio import Redis

            self._client = Redis(url=self._url, token=self._token, rest_retries=0, allow_telemetry=False)
        return self._client

    async def evaluate(self, checks: Sequence[Check]) -> tuple[bool, int, int]:
        args: list[str] = []
        for check in checks:
            args.extend(("p" if check.peek else "i", str(int(check.limit)), str(int(check.window) * 1000)))
        try:
            result = await asyncio.wait_for(
                self._redis().eval(_EVALUATE_SCRIPT, [check.key for check in checks], args),
                timeout=self._timeout,
            )
        except Exception:
            # Never carry store details (URL, token, response) upward or into logs.
            raise RateLimitStoreUnavailable("rate-limit store unavailable") from None
        if (
            not isinstance(result, (list, tuple)) or len(result) != 3
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in result)
        ):
            raise RateLimitStoreUnavailable("rate-limit store unavailable")
        return bool(result[0]), int(result[1]), int(result[2])


class LocalOnlyRateLimitStore:
    """Single-process store for a loopback (local development) issuer only."""

    def __init__(self, *, clock=time.monotonic, max_entries: int = 10_000) -> None:
        self._entries: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._max_entries = max(100, int(max_entries))

    async def evaluate(self, checks: Sequence[Check]) -> tuple[bool, int, int]:
        now = self._clock()
        with self._lock:
            if len(self._entries) >= self._max_entries:
                self._entries = {k: v for k, v in self._entries.items() if v[1] > now}
                if len(self._entries) >= self._max_entries:
                    raise RateLimitStoreUnavailable("rate-limit store is full")
            for index, check in enumerate(checks, start=1):
                count, expires = self._entries.get(check.key, (0, 0.0))
                if expires <= now:
                    count, expires = 0, now + check.window
                if check.peek:
                    observed = count + 1
                else:
                    count += 1
                    observed = count
                    self._entries[check.key] = (count, expires)
                if observed > check.limit:
                    return False, index, int((expires - now) * 1000)
        return True, 0, 0


def is_local_issuer(issuer: str | None) -> bool:
    """Local development is identified by a loopback MCP OAuth issuer.

    A deployed issuer must be reachable by external MCP clients, so it can
    never be loopback; no extra environment flag is needed.
    """
    try:
        host = (urlsplit(str(issuer or "")).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS or host.endswith(".localhost")


def build_store(settings) -> RateLimitStore | None:
    """Shared Upstash store, the local-only store, or ``None`` (fail closed)."""
    url = (getattr(settings, "UPSTASH_REDIS_REST_URL", None) or "").strip()
    token = (getattr(settings, "UPSTASH_REDIS_REST_TOKEN", None) or "").strip()
    if url and token:
        return UpstashRateLimitStore(url=url, token=token)
    if is_local_issuer(getattr(settings, "MCP_OAUTH_ISSUER", None)):
        return LocalOnlyRateLimitStore()
    return None


def _hash_key(settings) -> bytes:
    secret = str(getattr(settings, "JWT_SECRET_KEY", "") or "")
    if not secret:
        raise RateLimitStoreUnavailable("rate-limit key material unavailable")
    return hmac.new(secret.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()


def _digest(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def client_network(*, forwarded_for: str | None, peer_host: str | None) -> str:
    """Normalized caller network: IPv4 address, IPv6 /64, or ``invalid``.

    Behind Hub's hosting proxy the right-most ``X-Forwarded-For`` entry is the
    address the proxy itself observed (a client can prepend entries but not
    replace the proxy's own). Values that do not parse share one bucket.
    """
    candidate = (forwarded_for.split(",")[-1] if forwarded_for else (peer_host or "")).strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return "invalid"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/64", strict=False))
    return str(address)


def client_bucket(client_id: object, approved_provider_by_client: dict[str, str], agent_client_id: str | None) -> str:
    """Approved provider name, or the single shared ``unknown`` bucket."""
    if not isinstance(client_id, str) or not client_id:
        return "unknown"
    if agent_client_id and client_id == agent_client_id:
        return "unknown"  # the Hub Agent never uses the public routes
    provider = approved_provider_by_client.get(client_id)
    return provider if provider in {"claude", "chatgpt", "gemini"} else "unknown"


def _bucket_key(settings, key: bytes, endpoint: str, bucket: Bucket, *, network: str, client: str) -> str:
    environment = _digest(key, "issuer:" + str(getattr(settings, "MCP_OAUTH_ISSUER", "") or ""))[:16]
    if bucket.dimension in {"network", "failures"}:
        value = _digest(key, "network:" + network)[:40]
    elif bucket.dimension == "client":
        value = client
    else:
        value = "all"
    return f"renphil:mcp-oauth:rl:v1:{environment}:{endpoint}:{bucket.dimension}:{value}"


def build_checks(settings, endpoint: str, *, network: str, client: str | None, phase: str = "all") -> list[Check]:
    """Ordered checks for ``endpoint``.

    ``phase`` splits the token endpoint so network buckets are evaluated
    before the form body is parsed: ``pre`` = buckets that need no client,
    ``client`` = buckets that need the client bucket, ``all`` = both.
    """
    key = _hash_key(settings)
    checks: list[Check] = []
    for bucket in MCP_OAUTH_RATE_LIMITS[endpoint]:
        needs_client = bucket.dimension in {"client", "global"}
        if phase == "pre" and needs_client:
            continue
        if phase == "client" and not needs_client:
            continue
        limit = bucket.limit
        if bucket.dimension == "client" and client == "unknown":
            limit = min(limit, UNKNOWN_CLIENT_LIMITS.get(endpoint, limit))
        checks.append(Check(
            key=_bucket_key(settings, key, endpoint, bucket, network=network, client=client or "unknown"),
            limit=limit, window=bucket.window, peek=bucket.peek,
        ))
    return checks


def failure_check(settings, *, network: str) -> Check:
    """Counter incremented after a failed token exchange.

    Uses the same key as the token policy's peeked ``failures`` bucket; it is
    only ever incremented here, never denied.
    """
    key = _hash_key(settings)
    bucket = next(
        (item for item in MCP_OAUTH_RATE_LIMITS["token"] if item.dimension == "failures"), TOKEN_FAILURE_BUCKET,
    )
    return Check(
        key=_bucket_key(settings, key, "token", bucket, network=network, client="unknown"),
        limit=2**31 - 1, window=bucket.window,
    )


async def evaluate(store: RateLimitStore, checks: Sequence[Check], endpoint: str) -> Decision:
    if not checks:
        return Decision(True)
    allowed, index, ttl_ms = await store.evaluate(checks)
    if allowed:
        return Decision(True)
    denied = checks[max(0, min(len(checks) - 1, index - 1))]
    retry_after = max(1, min(int(denied.window), math.ceil(max(0, ttl_ms) / 1000)))
    dimension = denied.key.split(":")[6] if denied.key.count(":") >= 7 else "unknown"
    logger.warning("MCP OAuth request throttled: endpoint=%s bucket=%s", endpoint, dimension)
    return Decision(False, retry_after, dimension)


__all__ = [
    "Bucket",
    "Check",
    "Decision",
    "LocalOnlyRateLimitStore",
    "MCP_OAUTH_RATE_LIMITS",
    "RateLimitStore",
    "RateLimitStoreUnavailable",
    "TOKEN_FAILURE_BUCKET",
    "UNAVAILABLE_RETRY_AFTER_SECONDS",
    "UNKNOWN_CLIENT_LIMITS",
    "UpstashRateLimitStore",
    "build_checks",
    "build_store",
    "client_bucket",
    "client_network",
    "evaluate",
    "failure_check",
    "is_local_issuer",
]
