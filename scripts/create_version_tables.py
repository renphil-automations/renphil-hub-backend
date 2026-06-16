"""
One-shot helper: create the tab_versions and page_content_versions tables
in the database referenced by DATABASE_URL / PG_* env vars.

Run from the project root:

    python -m scripts.create_version_tables

Safe to run multiple times — uses CREATE TABLE IF NOT EXISTS semantics
via SQLAlchemy's checkfirst=True, and only touches the two version tables.
"""

from __future__ import annotations

from app.database import Base, engine
from app.models.versions import PageContentVersion, TabVersion


TARGET_TABLES = [TabVersion.__table__, PageContentVersion.__table__]


def main() -> None:
    print(f"Connecting to: {engine.url.render_as_string(hide_password=True)}")

    for table in TARGET_TABLES:
        exists = engine.dialect.has_table(engine.connect(), table.name)
        print(f"  - {table.name}: {'already exists' if exists else 'will be created'}")

    Base.metadata.create_all(bind=engine, tables=TARGET_TABLES, checkfirst=True)
    print("Done.")


if __name__ == "__main__":
    main()
