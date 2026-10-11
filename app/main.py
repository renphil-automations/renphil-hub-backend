"""
RenPhil Hub — FastAPI Application Entry Point.

Registers routers, configures CORS, and manages lifespan events
(HTTP client init/teardown).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.helpers.airtable_formulas import FormulaFieldError
from app.helpers.http_client import close_http_client, init_http_client
from app.routers import (
    agent_access,
    airtable,
    bookmarks,
    bot_management,
    auth,
    calendar,
    dify,
    drive,
    knowledge,
    locks_v2,
    nav_tabs,
    rbac,
    rbac_assignments,
    resource_grants,
    super_blocknote_v2,
    tabs_v2,
    threads,
)

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
    logger.info("Starting %s …", settings.APP_NAME)
    await init_http_client()
    yield
    await close_http_client()
    logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

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
    app.include_router(drive.router, prefix=api_prefix)
    app.include_router(dify.router, prefix=api_prefix)
    app.include_router(bot_management.router, prefix=api_prefix)
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
    app.include_router(resource_grants.router, prefix=api_prefix)
    app.include_router(bookmarks.router, prefix=api_prefix)

    # ── Health check ───────────────────────────────────────────────────
    @app.get("/health", tags=["Health"])
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
