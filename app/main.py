"""
RenPhil Hub — FastAPI Application Entry Point.

Registers routers, configures CORS, and manages lifespan events
(HTTP client init/teardown).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.helpers.http_client import close_http_client, init_http_client
from app.routers import airtable, auth, dify, drive, tabs, page_contents, diagnostics

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
    )

    # ── Routers ────────────────────────────────────────────────────────
    api_prefix = ""

    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(drive.router, prefix=api_prefix)
    app.include_router(dify.router, prefix=api_prefix)
    app.include_router(airtable.router, prefix=api_prefix)

    app.include_router(tabs.router, prefix=api_prefix)
    app.include_router(page_contents.router, prefix=api_prefix)
    app.include_router(diagnostics.router, prefix=api_prefix)

    # ── Health check ───────────────────────────────────────────────────
    @app.get("/health", tags=["Health"])
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
