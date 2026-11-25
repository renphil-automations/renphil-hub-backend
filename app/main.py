"""
RenPhil Hub — FastAPI Application Entry Point.

Registers routers, configures CORS, and manages lifespan events
(HTTP client init/teardown).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.helpers.airtable_formulas import FormulaFieldError
from app.helpers.http_client import close_http_client, init_http_client
from app.routers import (
    agent_access,
    airtable,
    airtable_sync,
    airtable_sync_tick,
    bookmarks,
    bot_management,
    external_resources,
    auth,
    calendar,
    dify,
    drive,
    forms,
    knowledge,
    locks_v2,
    mcp_oauth,
    nav_tabs,
    product_analytics,
    hub_feedback,
    product_analytics_archive_trigger,
    rbac,
    rbac_assignments,
    resource_grants,
    super_blocknote_v2,
    tabs_v2,
    task_manager,
    threads,
    view_as,
)
from app.services import mcp_oauth_rate_limit

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.DEBUG else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[logging.StreamHandler()],
    )
    # At DEBUG these log the OAuth client secret (Basic auth header) and the
    # Google access / refresh / id tokens on every sign-in. Never below WARNING.
    for name in ("requests_oauthlib", "oauthlib"):
        logging.getLogger(name).setLevel(logging.WARNING)
    mcp_oauth_rate_limit.warn_if_forwarded_for_untrusted(settings)
    logger.info("Starting %s …", settings.APP_NAME)
    await init_http_client()
    yield
    await close_http_client()
    logger.info("Shutdown complete.")


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_cors_origins(settings) -> None:
    """Refuse to start with an unsafe credentialed-CORS origin list.

    CORS runs with ``allow_credentials=True``. ``*`` or ``null`` (the origin
    of sandboxed iframes and ``file:`` pages) is refused everywhere; outside
    local/debug every origin must also be HTTPS, loopback excepted.
    """
    strict = not settings.is_local_or_debug
    problems = []
    for origin in settings.ALLOWED_ORIGINS:
        value = str(origin).strip()
        if value == "*" or value.lower() == "null":
            problems.append(f"{value!r} is not allowed with credentialed CORS")
            continue
        if not strict:
            continue
        try:
            parts = urlsplit(value)
            host = (parts.hostname or "").lower()
        except ValueError:
            problems.append(f"{value!r} is not a valid origin")
            continue
        loopback = host in _LOOPBACK_HOSTS or host.endswith(".localhost")
        if parts.scheme.lower() == "https" and host:
            continue
        if parts.scheme.lower() == "http" and loopback:
            continue
        problems.append(f"{value!r} must be https outside local/debug")
    if problems:
        raise RuntimeError("Refusing to start: unsafe ALLOWED_ORIGINS: " + "; ".join(problems))


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    check_cors_origins(settings)

    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── CORS ───────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag"],
    )

    # ── Error mapping ──────────────────────────────────────────────────
    # A widget's stored Airtable config that can't be compiled into a
    # formula — e.g. an admin filter `Amount > abc` (`_as_number`), or a
    # field name with a brace — is the caller's config, not a server fault.
    # Unhandled, it was a bare 500 raised past CORSMiddleware, so the
    # browser saw only "Failed to fetch" (advanced-filters phase 5). As a
    # 400 it goes through CORS and the widget shows the message instead.
    @app.exception_handler(FormulaFieldError)
    async def formula_field_error_handler(_request: Request, exc: FormulaFieldError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    # ── Routers ────────────────────────────────────────────────────────
    api_prefix = ""

    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(mcp_oauth.router, prefix=api_prefix)
    app.include_router(mcp_oauth.metadata_router, prefix=api_prefix)
    app.include_router(drive.router, prefix=api_prefix)
    app.include_router(dify.router, prefix=api_prefix)
    app.include_router(bot_management.router, prefix=api_prefix)
    app.include_router(external_resources.router, prefix=api_prefix)
    app.include_router(product_analytics.router, prefix=api_prefix)
    app.include_router(hub_feedback.router, prefix=api_prefix)
    app.include_router(product_analytics_archive_trigger.router, prefix=api_prefix)
    app.include_router(airtable.router, prefix=api_prefix)
    app.include_router(agent_access.router, prefix=api_prefix)
    app.include_router(calendar.router, prefix=api_prefix)
    app.include_router(knowledge.router, prefix=api_prefix)
    app.include_router(tabs_v2.router, prefix=api_prefix)
    app.include_router(nav_tabs.router, prefix=api_prefix)
    app.include_router(super_blocknote_v2.router, prefix=api_prefix)
    app.include_router(locks_v2.router, prefix=api_prefix)
    app.include_router(threads.router, prefix=api_prefix)
    app.include_router(rbac.router, prefix=api_prefix)
    app.include_router(rbac_assignments.router, prefix=api_prefix)
    app.include_router(airtable_sync.router, prefix=api_prefix)
    app.include_router(airtable_sync_tick.router, prefix=api_prefix)
    app.include_router(resource_grants.router, prefix=api_prefix)
    app.include_router(bookmarks.router, prefix=api_prefix)
    app.include_router(forms.router, prefix=api_prefix)
    app.include_router(task_manager.router, prefix=api_prefix)
    app.include_router(view_as.router, prefix=api_prefix)

    # ── Health check ───────────────────────────────────────────────────
    @app.get("/health", tags=["Health"])
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
