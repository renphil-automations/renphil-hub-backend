"""Per-user write rate limiting for the Thread widget
(plan_thread_widget_2026-08-17.md §4.7 control 2).

Fixed-window counters via Upstash Redis's `INCR` + `EXPIRE` — the pattern
the plan names explicitly, and the only rate limiting that will exist at
all now that the Vercel Firewall control (3) is gone (plan §4.7 control 3,
amended 2026-08-19: "the per-user limiter is now the ONLY rate limiting
that will exist").

Modelled directly on `cache_service.py`'s `CacheService`: same settings,
same lazy `Redis(url=..., token=..., allow_telemetry=False)` client, same
"a missing Upstash config is a silent pass-through" posture. The one
deliberate difference is WHY it fails open: `CacheService` fails open
because a cache is an optimization and an outage must not surface as a
500. This service fails open because the plan says so explicitly — "a lost
INCR lets the write through, never blocks it" (§4.7) — a rate limiter is
advisory in the same sense §13.5 uses that word: under-counting lets one
extra write through, which is the correct failure direction for a control
whose only job is to blunt an amplifier, not to gate access.
"""

from __future__ import annotations

import logging
import time

from upstash_redis.asyncio import Redis

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# plan §4.7 control 2 — "suggested starting points, tuned after observation
# rather than guessed at precisely". action -> (max_requests, window_seconds).
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "thread": (10, 3600),
    "comment": (30, 3600),
    "vote": (120, 3600),
}

# Atomic INCR-then-EXPIRE-only-on-the-first-hit-of-the-window, as a single
# Lua script — one Upstash round trip rather than a GET-then-SET race, and
# the same "atomic compare-and-act via EVAL" shape `cache_service.py`
# already uses for `_RELEASE_IF_OWNER_SCRIPT`. Only the FIRST caller in a
# window sets the TTL; every subsequent caller in the same window just
# increments, so a burst of concurrent requests can't each reset the
# window's expiry and extend it indefinitely.
_INCR_AND_EXPIRE_ON_FIRST_HIT_SCRIPT = (
    "local count = redis.call('INCR', KEYS[1]) "
    "if tonumber(count) == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end "
    "return count"
)


class RateLimitService:
    """Thin async wrapper around Upstash Redis for fixed-window write limits."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis | None = None
        self._enabled = bool(
            settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN
        )
        if not self._enabled:
            logger.warning(
                "Upstash Redis is not configured — write rate limiting is "
                "DISABLED (fails open, same posture as an unreachable "
                "Upstash at request time — plan §4.7)."
            )

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

    async def check(self, action: str, email: str) -> int | None:
        """Returns the number of seconds the caller must wait before
        retrying `action`, or `None` if the request is allowed. `None`
        covers every failure mode alike — an unknown action, a disabled
        service, or any Upstash error — because a limiter that cannot be
        consulted must let the write through (plan §4.7)."""
        limit = RATE_LIMITS.get(action)
        if limit is None:
            return None
        max_requests, window_seconds = limit

        client = self._redis()
        if client is None:
            return None

        now = int(time.time())
        # The window bucket rides IN the key, not a separately-tracked
        # start time — a request in a new bucket simply increments a key
        # that has never existed (or has already expired), so there is
        # nothing to reset and nothing that can drift.
        window_bucket = now // window_seconds
        key = f"rl:{action}:{email}:{window_bucket}"

        try:
            count = await client.eval(
                _INCR_AND_EXPIRE_ON_FIRST_HIT_SCRIPT, [key], [str(window_seconds)]
            )
        except Exception:
            logger.warning(
                "Rate limit check failed for action=%s email=%s — failing open",
                action,
                email,
                exc_info=True,
            )
            return None

        if int(count) > max_requests:
            window_end = (window_bucket + 1) * window_seconds
            return max(1, window_end - now)
        return None


_service: RateLimitService | None = None


def get_rate_limit_service() -> RateLimitService:
    global _service
    if _service is None:
        _service = RateLimitService(get_settings())
    return _service
