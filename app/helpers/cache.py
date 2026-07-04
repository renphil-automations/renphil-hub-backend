"""
FastAPI decorators for the Airtable endpoint cache.

Two decorators are exposed:

* :func:`airtable_cache` — read-through cache for GET-style endpoints.
  Wraps a handler so that identical (path + params + body) requests are
  served from Upstash Redis without hitting Airtable. Bypass with the
  query param ``?nocache=1`` or the request header ``X-No-Cache: 1``
  (both still refresh the cache after fetching from Airtable).

* :func:`invalidates_cache` — post-write cache invalidation. Applied to
  POST/PATCH/DELETE endpoints, it deletes every cached entry belonging
  to the listed endpoint names once the wrapped handler returns
  successfully.

Both decorators fail-open: any error talking to Redis is logged and the
request continues as if the cache were disabled.
"""

from __future__ import annotations

import functools
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.services.cache_service import get_cache_service

logger = logging.getLogger(__name__)

# Handler parameters that identify the caller / injected services and
# must NOT participate in the cache key.
_IGNORED_PARAMS: set[str] = {
    "request",
    "user",
    "_user",
    "airtable_service",
    "gemini_service",
    "auth_service",
    "drive_service",
    "dify_service",
    "credentials",
    "x_webhook_secret",
}

# Truthy values recognised by the cache-bypass query param / header.
_TRUTHY = {"1", "true", "yes", "on"}


def _canonicalize(value: Any) -> Any:
    """Recursively convert ``value`` to a JSON-serialisable, canonical form.

    Dicts are emitted with sorted keys so that any two logically-equal
    inputs hash to the same cache key.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {
            str(k): _canonicalize(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if hasattr(value, "model_dump"):
        try:
            return _canonicalize(value.model_dump(mode="json"))
        except Exception:
            pass
    return jsonable_encoder(value)


def _extract_params(kwargs: dict) -> dict:
    return {
        k: _canonicalize(v)
        for k, v in kwargs.items()
        if k not in _IGNORED_PARAMS and not k.startswith("_")
    }


def _should_bypass(request: Request | None) -> bool:
    if request is None:
        return False
    q = request.query_params.get("nocache")
    if q and q.lower() in _TRUTHY:
        return True
    h = request.headers.get("x-no-cache")
    if h and h.lower() in _TRUTHY:
        return True
    return False


def airtable_cache(
    endpoint_name: str | None = None,
    *,
    table: str | list[str] | None = None,
) -> Callable:
    """Cache the response of an Airtable-backed endpoint in Upstash Redis.

    ``endpoint_name`` defaults to the wrapped function's ``__name__`` and
    is used as the human-readable prefix in every cache key belonging to
    this endpoint.

    ``table`` records the Airtable table(s) the endpoint reads from.
    Stored alongside every cache entry for tracking / observability
    (e.g. so an operator can list every cached key backed by a given
    table). Accepts a single Settings attribute name (``"GLOSSARY_TABLE"``)
    or a list of them for endpoints that join multiple tables.

    On cache hit the response is served directly as a ``JSONResponse``
    (bypassing ``response_model`` re-validation for speed). On cache
    miss the wrapped handler runs normally and its jsonable-encoded
    response is stored along with ``table`` and ``last_updated_date``.
    """

    def decorator(func: Callable) -> Callable:
        name = endpoint_name or func.__name__
        original_sig = inspect.signature(func)
        original_params = list(original_sig.parameters.values())
        has_request = any(
            p.annotation is Request or p.name == "request"
            for p in original_params
        )

        if has_request:
            new_sig = original_sig
        else:
            injected = inspect.Parameter(
                "request",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Request,
            )
            new_sig = original_sig.replace(
                parameters=original_params + [injected]
            )

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if has_request:
                request = kwargs.get("request")
            else:
                request = kwargs.pop("request", None)

            bypass = _should_bypass(request)
            cache_params = _extract_params(kwargs)
            cache = get_cache_service()
            key = cache.build_key(name, cache_params)

            if not bypass:
                try:
                    cached = await cache.get(key)
                except Exception:
                    logger.warning(
                        "Cache GET failed for key=%s", key, exc_info=True
                    )
                    cached = None
                if (
                    cached is not None
                    and isinstance(cached, dict)
                    and "response" in cached
                ):
                    logger.debug("Cache HIT  %s", key)
                    return JSONResponse(content=cached["response"])
                logger.debug("Cache MISS %s", key)

            result = await func(*args, **kwargs)

            try:
                encoded = jsonable_encoder(result)
                await cache.set(
                    key,
                    {
                        "endpoint": name,
                        "table": table,
                        "params": cache_params,
                        "last_updated_date": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "response": encoded,
                    },
                )
            except Exception:
                logger.warning(
                    "Cache SET failed for key=%s", key, exc_info=True
                )
            return result

        wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
        return wrapper

    return decorator


def invalidates_cache(endpoints: list[str]) -> Callable:
    """Invalidate cached entries for every endpoint in ``endpoints`` after
    the wrapped write handler succeeds.

    Deletion runs after the handler returns; exceptions raised by the
    handler propagate untouched and the cache is left alone.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            try:
                await get_cache_service().invalidate_prefixes(endpoints)
            except Exception:
                logger.warning(
                    "Cache invalidation failed for endpoints=%s",
                    endpoints,
                    exc_info=True,
                )
            return result

        return wrapper

    return decorator
