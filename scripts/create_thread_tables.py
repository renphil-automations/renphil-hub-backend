"""
One-shot helper: create the Thread widget's four new tables (threads,
thread_comments, thread_votes, notifications) in the database referenced by
DATABASE_URL_V2 / PG_V2_* env vars — see plan_thread_widget_2026-08-17.md §3.5.

Modelled directly on scripts/create_v2_schema_tables.py: import the models so
they register on BaseV2.metadata, then create_all with checkfirst=True.

Run from the project root:

    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.create_thread_tables

Safe to run multiple times — CREATE TABLE IF NOT EXISTS semantics via
SQLAlchemy's checkfirst=True.

Order matters here beyond readability, unlike create_v2_schema_tables.py's
own comment about its list: thread_comments, thread_votes and notifications
all carry a real FK to threads (and thread_votes/notifications to
thread_comments), and threads itself FKs to components — which this script
does NOT create (it must already exist; see create_v2_schema_tables.py).
SQLAlchemy's metadata.create_all sorts by FK dependency automatically
regardless of this list's order, but the order below is kept dependency-first
for anyone reading the log output.
"""

from __future__ import annotations

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.component import ComponentV2  # noqa: F401 -- registers the FK target
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.thread import ThreadCommentV2, ThreadV2, ThreadVoteV2

TARGET_TABLES = [
    ThreadV2.__table__,
    ThreadCommentV2.__table__,
    ThreadVoteV2.__table__,
    NotificationV2.__table__,
]


def main() -> None:
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    with engine_v2.connect() as conn:
        components_exists = engine_v2.dialect.has_table(conn, "components")
        if not components_exists:
            print(
                "  ! 'components' table not found — run "
                "scripts/create_v2_schema_tables.py first (threads.component_id FKs to it)."
            )
        for table in TARGET_TABLES:
            exists = engine_v2.dialect.has_table(conn, table.name)
            print(f"  - {table.name}: {'already exists' if exists else 'will be created'}")

    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    print("Done.")


if __name__ == "__main__":
    main()
