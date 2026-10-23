"""Alembic environment for the Hub V2 database (DATABASE_URL_V2 / PG_V2_*)."""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db_v2 import models as _models_package  # noqa: E402
from app.db_v2.database import BaseV2, _resolve_database_url_v2  # noqa: E402

# Register every V2 model on BaseV2.metadata so autogenerate sees the full
# schema. Revisions themselves create only what they declare.
for _module in pkgutil.iter_modules(_models_package.__path__):
    importlib.import_module(f"{_models_package.__name__}.{_module.name}")

target_metadata = BaseV2.metadata


def _url() -> str:
    configured = context.config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    url = _resolve_database_url_v2()
    return url.render_as_string(hide_password=False) if hasattr(url, "render_as_string") else str(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
