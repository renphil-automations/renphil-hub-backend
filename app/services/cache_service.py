"""
Endpoint response cache backed by Upstash Redis (REST).

Storage layout
--------------
Keys are of the form::

    airtable:{version}:{endpoint_name}:{sha256(sorted_params)[:32]}

The stored value is a JSON object::

    {
        "endpoint":          <endpoint_name>,
        "table":             <str | list[str] | None — Settings attr name(s)>,
        "params":            <canonical params dict>,
        "last_updated_date": <ISO-8601 UTC timestamp>,
        "response":          <jsonable response body>,
    }

A missing ``UPSTASH_REDIS_REST_URL`` or ``UPSTASH_REDIS_REST_TOKEN``
transparently disables the cache — every call becomes a pass-through so
the app keeps working even if the cache is misconfigured.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
from typing import Any

from upstash_redis.asyncio import Redis

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_HASH_LEN = 32  # truncated sha256 hex — 128 bits of entropy
_SCAN_COUNT = 200

# Marker prefixing a gzip+base64 payload. Compression is OPT-IN per call site
# (`set(..., compress=True)`), because this service is shared with the 123
# `@airtable_cache`-decorated endpoints whose already-stored entries carry no
# marker and must keep decoding unchanged
# (plan_airtable_cache_scaling_2026-08-08.md §4.3, landmine L2).
#
# The marker cannot collide with a legacy entry: every legacy value is
# `json.dumps(...)` output, which always begins with `{`, `[`, `"`, `-`, a
# digit, or one of `true`/`false`/`null` — never `g`. So "starts with the
# marker" is a sound discriminator rather than a heuristic.
_GZIP_MARKER = "gz:"


def _encode(value: Any, *, compress: bool) -> str:
    """JSON-encode `value`, optionally gzip+base64 behind `_GZIP_MARKER`.
    Measured ~4.8x on realistic Airtable row shapes — a saving on stored
    bytes AND on every read's transfer."""
    payload = json.dumps(value, separators=(",", ":"), default=str)
    if not compress:
        return payload
    # mtime=0 keeps the output deterministic — the gzip header would
    # otherwise embed a timestamp and make identical data encode differently
    # on every write.
    packed = gzip.compress(payload.encode("utf-8"), mtime=0)
    return _GZIP_MARKER + base64.b64encode(packed).decode("ascii")


def _decode(raw: Any) -> Any:
    """Inverse of `_encode`, for marked AND unmarked payloads alike."""
    if isinstance(raw, str) and raw.startswith(_GZIP_MARKER):
        packed = base64.b64decode(raw[len(_GZIP_MARKER):])
        raw = gzip.decompress(packed).decode("utf-8")
    return json.loads(raw)


def _cache_root(version: str) -> str:
    return f"airtable:{version}"


def build_key(endpoint_name: str, params: dict, *, version: str = "v1") -> str:
    """Build a canonical Redis key for a given endpoint invocation."""
    canonical = json.dumps(
        params, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{_cache_root(version)}:{endpoint_name}:{digest}"


def endpoint_prefix(endpoint_name: str, *, version: str = "v1") -> str:
    """Return the prefix used to match every key of a given endpoint."""
    return f"{_cache_root(version)}:{endpoint_name}:"


class CacheService:
    """Thin async wrapper around Upstash Redis for endpoint caching."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis | None = None
        self._enabled = bool(
            settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN
        )
        self._version = settings.CACHE_VERSION or "v1"
        if not self._enabled:
            logger.warning(
                "Upstash Redis is not configured — endpoint cache is DISABLED."
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def version(self) -> str:
        return self._version

    def _redis(self) -> Redis | None:
        if not self._enabled:
            return None
        if self._client is None:
            self._client = Redis(
                url=self._settings.UPSTASH_REDIS_REST_URL,
                token=self._settings.UPSTASH_REDIS_REST_TOKEN,
                allow_telemetry=False,
            )
        return self._client

    # ── key helpers ────────────────────────────────────────────────────
    def build_key(self, endpoint_name: str, params: dict) -> str:
        return build_key(endpoint_name, params, version=self._version)

    def endpoint_prefix(self, endpoint_name: str) -> str:
        return endpoint_prefix(endpoint_name, version=self._version)

    # ── read / write ───────────────────────────────────────────────────
    async def get(self, key: str) -> Any | None:
        """Returns None on ANY failure — a miss, a decode error, or Upstash
        being unreachable. A cache is an optimization: an outage must degrade
        callers to their live path, never surface as a 500. `acquire_lock` /
        `release_lock` already fail open this way, and the `@airtable_cache`
        decorator wraps its own call (`helpers/cache.py:161-167`); the widget
        row path does not, so without this an Upstash outage 500s every
        Airtable widget (plan_airtable_cache_scaling_2026-08-08.md §4.5.1).
        """
        client = self._redis()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception:
            logger.warning("Cache GET failed for key=%s", key, exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return _decode(raw)
        except Exception:
            logger.warning("Cache GET: failed to decode value for key=%s", key)
            return None

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: int | None = None,
        compress: bool = False,
    ) -> None:
        """Store ``value`` at ``key``. ``ttl_seconds`` and ``compress`` are
        both optional and additive — existing callers that omit them keep
        today's no-expiry, uncompressed behavior; only a caller that opts in
        (the airtable widget row cache) gets an expiring, gzipped key.
        """
        client = self._redis()
        if client is None:
            return
        payload = _encode(value, compress=compress)
        # Swallow write failures for the same reason `get` swallows read
        # failures (§4.5.1): a cache that cannot be written is a cache miss
        # next time, not a request that should fail now.
        try:
            if ttl_seconds is not None:
                await client.set(key, payload, ex=ttl_seconds)
            else:
                await client.set(key, payload)
        except Exception:
            logger.warning("Cache SET failed for key=%s", key, exc_info=True)

    # ── single-flight lock ────────────────────────────────────────────
    async def acquire_lock(self, key: str, *, ttl_seconds: int = 120) -> bool:
        """Best-effort ``SET key 1 NX EX ttl_seconds``. Returns True iff this
        call won the lock. When the cache is disabled, returns True — every
        caller "wins" and just does the work directly, matching the
        fail-open behavior of the rest of this service.
        """
        client = self._redis()
        if client is None:
            return True
        try:
            result = await client.set(key, "1", nx=True, ex=ttl_seconds)
        except Exception:
            logger.warning("Cache lock acquire failed for key=%s", key, exc_info=True)
            return True
        return bool(result)

    async def release_lock(self, key: str) -> None:
        client = self._redis()
        if client is None:
            return
        try:
            await client.delete(key)
        except Exception:
            logger.warning("Cache lock release failed for key=%s", key, exc_info=True)

    # ── invalidation ───────────────────────────────────────────────────
    async def invalidate_prefixes(self, endpoint_names: list[str]) -> int:
        """Delete every cached key belonging to the given endpoints.

        Returns the number of keys deleted.
        """
        client = self._redis()
        if client is None or not endpoint_names:
            return 0

        total_deleted = 0
        for name in endpoint_names:
            pattern = f"{self.endpoint_prefix(name)}*"
            keys = await self._scan_keys(client, pattern)
            if not keys:
                continue
            try:
                total_deleted += int(await client.delete(*keys) or 0)
            except Exception:
                logger.exception("Cache DEL failed for pattern=%s", pattern)
        logger.info(
            "Cache invalidation: endpoints=%s deleted=%d",
            endpoint_names,
            total_deleted,
        )
        return total_deleted

    async def _scan_keys(self, client: Redis, pattern: str) -> list[str]:
        keys: list[str] = []
        cursor: int | str = 0
        while True:
            try:
                cursor, batch = await client.scan(
                    cursor, match=pattern, count=_SCAN_COUNT
                )
            except Exception:
                logger.exception("Cache SCAN failed for pattern=%s", pattern)
                break
            if batch:
                keys.extend(batch)
            # Upstash returns the cursor as an int (or int-like string).
            if str(cursor) == "0":
                break
        return keys


# ── module-level singleton ────────────────────────────────────────────
_service: CacheService | None = None


def get_cache_service() -> CacheService:
    global _service
    if _service is None:
        _service = CacheService(get_settings())
    return _service
