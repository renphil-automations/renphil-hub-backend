"""
One-shot helper: create the Phase 2 new-schema tables (tabs, gridstacks,
components, page_content) in the database referenced by DATABASE_URL_V2 /
PG_V2_* env vars (see docker-compose.yml's db_v2 service).

Run from the project root:

    python -m scripts.create_v2_schema_tables

Safe to run multiple times — uses CREATE TABLE IF NOT EXISTS semantics via
SQLAlchemy's checkfirst=True. This is also called automatically at the start
of every run of scripts/migrate_to_v2_schema.py, so a freshly-created empty
db_v2 container bootstraps on its own.
"""

from __future__ import annotations

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.tab import TabV2

# Order matters for a fresh create_all only in that FK targets should exist
# first; SQLAlchemy's metadata sorts this out automatically from the
# ForeignKey declarations, so this list order is just for readable log output.
TARGET_TABLES = [
    TabV2.__table__,
    GridstackV2.__table__,
    PageContentV2.__table__,
    ComponentV2.__table__,
]


def main() -> None:
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    with engine_v2.connect() as conn:
        for table in TARGET_TABLES:
            exists = engine_v2.dialect.has_table(conn, table.name)
            print(f"  - {table.name}: {'already exists' if exists else 'will be created'}")

    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    print("Done.")


if __name__ == "__main__":
    main()
