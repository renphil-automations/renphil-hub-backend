"""
One-shot migration: an author's save updates their pending edit in place —
plan_thread_pending_edit_update_2026-11-10.md §3.1 / §3.8 — plus one index
for the upcoming My Revisions section (plan_my_revisions_2026-11-10.md
§4.4), in the database referenced by DATABASE_URL_V2 / PG_V2_* env vars.

Steps, in this order:

  thread_revisions (§3.1)
    1. ADD COLUMN edit_count INTEGER NOT NULL DEFAULT 0  (the stale-check token)
    2. ADD COLUMN updated_at TIMESTAMPTZ                 (display only; NULL = never updated)
  threads (§3.8, U9)
    3. ADD COLUMN edit_count INTEGER NOT NULL DEFAULT 0  (pending-thread stale-check token)
  indexes
    4. CREATE INDEX IF NOT EXISTS ix_thread_revisions_author_open
         ON thread_revisions (submitted_by_email)
         WHERE origin = 'author' AND status IN ('rejected', 'pending')

No backfill: 0 / NULL is right for every existing row (nothing has been
updated in place yet). No "one pending per thread" index (§3.1/§3.5):
existing threads may legally hold several pending edits; they drain
through review.

WHY A MIGRATION. `create_all(checkfirst=True)` will not add a column to an
existing table. The ORM selects the new columns, so without this script the
first thread read after deploy fails with UndefinedColumn. MIGRATE EACH DB
BEFORE ITS BACKEND RUNS THIS CODE (Dev first; Sandbox and prod together
with the reject/resubmit A+B+C migration, `migrate_thread_resubmission.py`,
which must run first).

THE PARTIAL INDEX must match the model's `postgresql_where` exactly
(`ThreadRevisionV2.__table_args__` in `app/db_v2/models/thread.py`).

SAFE TO RUN MULTIPLE TIMES. Every column add is `ADD COLUMN IF NOT EXISTS`,
probed first; the index is `IF NOT EXISTS`, probed first. Everything runs in
ONE transaction (Postgres DDL is transactional).

POSTGRES ONLY; refuses on any other dialect. The test suite builds its
schema from the models on SQLite and never runs this script.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONIOENCODING=utf-8 PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_pending_edit_updates

  Real run (writes, against DATABASE_URL_V2):
    PYTHONIOENCODING=utf-8 PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_pending_edit_updates --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

THREADS_TABLE = "threads"
REVISIONS_TABLE = "thread_revisions"

COLUMNS: list[tuple[str, str, str]] = [
    (
        REVISIONS_TABLE,
        "edit_count",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS edit_count INTEGER NOT NULL DEFAULT 0",
    ),
    (
        REVISIONS_TABLE,
        "updated_at",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
    ),
    (
        THREADS_TABLE,
        "edit_count",
        f"ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS edit_count INTEGER NOT NULL DEFAULT 0",
    ),
]

INDEXES: dict[str, str] = {
    "ix_thread_revisions_author_open": (
        f"CREATE INDEX IF NOT EXISTS ix_thread_revisions_author_open ON {REVISIONS_TABLE} "
        "(submitted_by_email) WHERE origin = 'author' AND status IN ('rejected', 'pending')"
    ),
}

COLUMN_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = :table AND column_name = :column
    )
    """
)
INDEX_EXISTS_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :index)"
)
PENDING_AUTHOR_EDITS_SQL = text(
    f"SELECT COUNT(*) FROM {REVISIONS_TABLE} WHERE status = 'pending' AND origin = 'author'"
)
# §3.5 — legacy threads with several pending edits. New saves update the
# newest one; the older ones drain through review.
MULTI_PENDING_THREADS_SQL = text(
    f"""
    SELECT COUNT(*) FROM (
        SELECT thread_id FROM {REVISIONS_TABLE}
         WHERE status = 'pending' AND origin = 'author'
         GROUP BY thread_id HAVING COUNT(*) > 1
    ) s
    """
)
PENDING_THREADS_SQL = text(f"SELECT COUNT(*) FROM {THREADS_TABLE} WHERE status = 'pending'")
OPEN_INDEX_ROWS_SQL = text(
    f"SELECT COUNT(*) FROM {REVISIONS_TABLE} "
    "WHERE origin = 'author' AND status IN ('rejected', 'pending')"
)


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(COLUMN_EXISTS_SQL, {"table": table, "column": column}).scalar())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pending-edit updates: thread_revisions.edit_count/updated_at, "
        "threads.edit_count, and ix_thread_revisions_author_open."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run (default) that only prints the plan.",
    )
    args = parser.parse_args()
    dry_run = not args.commit

    print("=" * 70)
    print("DRY RUN — no writes will be made" if dry_run else "COMMIT MODE — writes will be made")
    print("=" * 70)
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    if engine_v2.dialect.name != "postgresql":
        print(f"  REFUSING: dialect is {engine_v2.dialect.name!r}, not 'postgresql'.")
        raise SystemExit(1)

    with engine_v2.connect() as conn:
        for table in (THREADS_TABLE, REVISIONS_TABLE):
            if not engine_v2.dialect.has_table(conn, table):
                print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                raise SystemExit(1)
        # This plan builds on the reject/resubmit columns (`resubmission_of_id`,
        # `closed_at`); refuse if that migration has not run here yet.
        for column in ("resubmission_of_id", "closed_at"):
            if not _column_exists(conn, REVISIONS_TABLE, column):
                print(
                    f"  REFUSING: {REVISIONS_TABLE}.{column} is missing. "
                    "Run scripts/migrate_thread_resubmission.py first."
                )
                raise SystemExit(1)

        plan: list[str] = []
        print("\nPlan:")
        for table, column, ddl in COLUMNS:
            exists = _column_exists(conn, table, column)
            print(f"  - {table}.{column}: {'already exists' if exists else 'will be added'}")
            if not exists:
                plan.append(ddl)
        for index, ddl in INDEXES.items():
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  - {index}: {'already exists' if exists else 'will be created'}")
            if not exists:
                plan.append(ddl)

        print("\nCounts (informational, nothing is backfilled):")
        print(f"  - pending author edits: {int(conn.execute(PENDING_AUTHOR_EDITS_SQL).scalar())}")
        print(
            "  - threads with several pending author edits (legacy, §3.5 — new saves "
            f"update the newest): {int(conn.execute(MULTI_PENDING_THREADS_SQL).scalar())}"
        )
        print(f"  - pending new threads: {int(conn.execute(PENDING_THREADS_SQL).scalar())}")
        print(
            "  - rows ix_thread_revisions_author_open will cover (author edits, rejected "
            f"or pending): {int(conn.execute(OPEN_INDEX_ROWS_SQL).scalar())}"
        )

    if dry_run:
        print("\nWould run:")
        for stmt in plan:
            print(f"    {stmt};")
        if not plan:
            print("    (nothing — already migrated)")
        print("\n" + "=" * 70)
        print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        print("=" * 70)
        return

    with engine_v2.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
            print(f"    ran: {stmt}")

    with engine_v2.connect() as conn:
        print("\nVerified:")
        for table, column, _ in COLUMNS:
            print(f"  {table}.{column} present = {_column_exists(conn, table, column)}")
        for index in INDEXES:
            print(f"  {index} present = {bool(conn.execute(INDEX_EXISTS_SQL, {'index': index}).scalar())}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
