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

**Two-phase check/consume, fixed 2026-08-24.** The original single-method
`check()` INCRemented on every call, including calls that turned out to be
for a request the handler went on to reject — a bad widget link (404), no
view access (403), an over-cap mention list (400). A caller fumbling
through failed attempts burned their limited quota on writes that never
happened. Split into `check()` (a read-only peek — does NOT move the
counter) and `consume()` (the actual INCR, called by `helpers/rate_limit.py`
only after the wrapped handler returns without raising, i.e. only on a
genuine success). This trades the old single-round-trip atomicity for two
round trips and a small window where concurrent requests can both pass
`check()` before either calls `consume()` — accepted deliberately, for the
same "advisory, not a hard gate" reason the module fails open at all: this
control's job is to blunt a notification-fan-out amplifier, not to enforce
an exact ceiling down to the last request.
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

    def _key(self, action: str, email: str, window_seconds: int, now: int) -> tuple[str, int]:
        # The window bucket rides IN the key, not a separately-tracked start
        # time — a request in a new bucket simply touches a key that has
        # never existed (or has already expired), so there is nothing to
        # reset and nothing that can drift. Shared by `check` and `consume`
        # so the two always agree on which window a given `now` falls in.
        window_bucket = now // window_seconds
        return f"rl:{action}:{email}:{window_bucket}", window_bucket

    async def check(self, action: str, email: str) -> int | None:
        """Read-only peek: returns the number of seconds the caller must
        wait before `action` would be allowed again, or `None` if they are
        currently under the limit. Does NOT consume a slot — call
        `consume()` separately once the caller's request has actually
        succeeded (see the module docstring's "two-phase check/consume").
        `None` covers every non-blocking case alike — an unknown action, a
        disabled service, an Upstash error, or genuinely being under the
        limit — because a limiter that cannot be consulted must let the
        write through (plan §4.7)."""
        limit = RATE_LIMITS.get(action)
        if limit is None:
            return None
        max_requests, window_seconds = limit

        client = self._redis()
        if client is None:
            return None

        now = int(time.time())
        key, window_bucket = self._key(action, email, window_seconds, now)

        try:
            raw = await client.get(key)
        except Exception:
            logger.warning(
                "Rate limit check failed for action=%s email=%s — failing open",
                action,
                email,
                exc_info=True,
            )
            return None

        count = int(raw) if raw is not None else 0
        if count >= max_requests:
            window_end = (window_bucket + 1) * window_seconds
            return max(1, window_end - now)
        return None

    async def consume(self, action: str, email: str) -> None:
        """Increments the caller's counter for `action`'s current window by
        one. Call this ONLY after the wrapped handler has completed
        successfully — never before, and never for a request the handler
        went on to reject (plan amendment 2026-08-24: a 403/404/400 must not
        cost the caller one of their limited attempts). Swallows every
        failure mode the same way `check` does: a lost increment just means
        the caller effectively got one extra allowed request this window,
        which matches this service's existing fail-open direction — it
        never raises, and callers should not await a return value from it."""
        limit = RATE_LIMITS.get(action)
        if limit is None:
            return
        _, window_seconds = limit

        client = self._redis()
        if client is None:
            return

        now = int(time.time())
        key, _ = self._key(action, email, window_seconds, now)

        try:
            await client.eval(
                _INCR_AND_EXPIRE_ON_FIRST_HIT_SCRIPT, [key], [str(window_seconds)]
            )
        except Exception:
            logger.warning(
                "Rate limit consume failed for action=%s email=%s — the "
                "caller's successful write will not count against their "
                "quota this window",
                action,
                email,
                exc_info=True,
            )


_service: RateLimitService | None = None


def get_rate_limit_service() -> RateLimitService:
    global _service
    if _service is None:
        _service = RateLimitService(get_settings())
    return _service
