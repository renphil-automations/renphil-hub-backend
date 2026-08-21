"""FastAPI decorator for per-user write rate limiting
(plan_thread_widget_2026-08-17.md §4.7 control 2).

Mirrors `helpers/cache.py`'s decorator shape — wraps an async router
handler, applied directly to the function BELOW the `@router.post(...)`
decorator so `functools.wraps`' `__wrapped__` lets FastAPI's
`inspect.signature()` see straight through to the real parameter list (the
same stacking `@airtable_cache`/`@invalidates_cache` already rely on
throughout `routers/airtable.py`).

Unlike `helpers/cache.py`, this decorator has nothing to catch itself: the
fail-open behaviour lives in `RateLimitService.check` (plan §4.7 — "a lost
INCR lets the write through, never blocks it"), so by the time this
decorator sees a return value, "allowed" and "Upstash is having a bad day"
are already the same thing.
"""

from __future__ import annotations

import functools
import logging
from typing import Callable

from fastapi import HTTPException, status

from app.services.rate_limit_service import get_rate_limit_service

logger = logging.getLogger(__name__)


def rate_limited(action: str) -> Callable:
    """Enforce the fixed-window write limit for `action` (one of
    `RATE_LIMITS`' keys — "thread" / "comment" / "vote", plan §4.7) before
    the wrapped handler runs. Keys on the caller's own email, read from
    `kwargs["user"]` — every decorated handler takes
    `user: UserInfo = Depends(get_current_user)`, and FastAPI always calls a
    route function with dependencies passed as keyword arguments matching
    their parameter names, so this needs no special wiring to find it."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            user = kwargs.get("user")
            email = getattr(user, "email", None) if user is not None else None
            if email:
                retry_after = await get_rate_limit_service().check(action, email)
                if retry_after is not None:
                    raise HTTPException(
                        status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=(
                            f"Too many {action} writes — try again in "
                            f"{retry_after}s."
                        ),
                        headers={"Retry-After": str(retry_after)},
                    )
            return await func(*args, **kwargs)

        return wrapper

    return decorator
