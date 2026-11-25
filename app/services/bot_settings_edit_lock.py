"""Advisory edit lock for Bot Management settings.

While an admin has unsaved changes they hold one short-lived lock; other
admins see who is editing and cannot save or reset until it is released (on
save or discard) or expires. The frontend refreshes the lock while the form
stays dirty, so a closed tab frees it within ``LOCK_TTL_SECONDS``.

The lock is a courtesy layer for a rare situation (two admins editing at the
same time). The expected-version check on save remains the integrity
guarantee, so when the lock store is unreachable editing is allowed rather
than blocked.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from app import config
from app.models.auth import UserInfo

logger = logging.getLogger(__name__)

LOCK_KEY = "renphil:bot-management:settings-edit-lock:v1"
LOCK_TTL_SECONDS = 120


@dataclass(frozen=True)
class EditLock:
    email: str
    name: str
    since: str

    def payload(self, viewer: UserInfo | None = None) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "since": self.since,
            "held_by_me": bool(viewer and _same_user(self.email, viewer)),
        }


@lru_cache(maxsize=1)
def _client(url: str, token: str):
    from upstash_redis import Redis

    # Best effort: no retry wait, so an unreachable store never slows a save.
    return Redis(url=url, token=token, rest_retries=0, allow_telemetry=False)


def _store():
    settings = config.get_settings()  # looked up per call so tests and reloads see current settings
    url = str(getattr(settings, "UPSTASH_REDIS_REST_URL", "") or "")
    token = str(getattr(settings, "UPSTASH_REDIS_REST_TOKEN", "") or "")
    if not url or not token:
        return None
    return _client(url, token)


def _same_user(email: str, user: UserInfo) -> bool:
    return email.strip().lower() == str(user.email).strip().lower()


def _decode(raw: Any) -> EditLock | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return EditLock(email=str(data["email"]), name=str(data.get("name") or data["email"]), since=str(data["since"]))
    except (ValueError, KeyError, TypeError):
        return None


def current() -> EditLock | None:
    """The lock holder, or None when free or when the store is unreachable."""
    store = _store()
    if store is None:
        return None
    try:
        return _decode(store.get(LOCK_KEY))
    except Exception:
        logger.warning("Bot settings edit lock unavailable; editing is not locked.", exc_info=True)
        return None


def acquire(user: UserInfo) -> tuple[bool, EditLock | None]:
    """Take or refresh the lock. Returns ``(granted, holder)``."""
    store = _store()
    if store is None:
        return True, None
    mine = EditLock(email=str(user.email), name=user.name or str(user.email),
                    since=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        holder = _decode(store.get(LOCK_KEY))
        if holder is not None and not _same_user(holder.email, user):
            return False, holder
        if holder is not None:
            # Refresh keeps the original start time so others see how long it has been held.
            mine = holder
            if store.set(LOCK_KEY, json.dumps(mine.__dict__), ex=LOCK_TTL_SECONDS, xx=True):
                return True, mine
        if store.set(LOCK_KEY, json.dumps(mine.__dict__), ex=LOCK_TTL_SECONDS, nx=True):
            return True, mine
        # Someone took it between our read and write.
        holder = _decode(store.get(LOCK_KEY))
        return (holder is None or _same_user(holder.email, user)), holder
    except Exception:
        logger.warning("Bot settings edit lock unavailable; editing is not locked.", exc_info=True)
        return True, None


def release(user: UserInfo) -> None:
    """Release the lock if ``user`` holds it."""
    store = _store()
    if store is None:
        return
    try:
        holder = _decode(store.get(LOCK_KEY))
        if holder is not None and _same_user(holder.email, user):
            store.delete(LOCK_KEY)
    except Exception:
        logger.warning("Bot settings edit lock could not be released; it expires on its own.", exc_info=True)


def held_by_other(user: UserInfo) -> EditLock | None:
    holder = current()
    return holder if holder is not None and not _same_user(holder.email, user) else None
