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

import hashlib
import json
import logging
from typing import Any

from upstash_redis.asyncio import Redis

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_HASH_LEN = 32  # truncated sha256 hex — 128 bits of entropy
_SCAN_COUNT = 200


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
        client = self._redis()
        if client is None:
            return None
        raw = await client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            logger.warning("Cache GET: failed to decode JSON for key=%s", key)
            return None

    async def set(self, key: str, value: Any) -> None:
        client = self._redis()
        if client is None:
            return
        payload = json.dumps(value, separators=(",", ":"), default=str)
        await client.set(key, payload)

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
