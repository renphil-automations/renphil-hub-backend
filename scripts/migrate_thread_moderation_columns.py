"""
One-shot migration: add thread moderation (approve/reject) support to the
`threads` table, in the database referenced by DATABASE_URL_V2 / PG_V2_*
env vars — see plan_thread_moderation_2026-09-18.md §3.

Six steps, in this order (columns before anything that reads them):

  1. ALTER `threads` ADD COLUMN IF NOT EXISTS reviewed_by_email VARCHAR(320)
  2. ALTER `threads` ADD COLUMN IF NOT EXISTS reviewed_by_name  VARCHAR(255)
  3. ALTER `threads` ADD COLUMN IF NOT EXISTS reviewed_at       TIMESTAMPTZ
  4. CREATE INDEX ix_threads_pending_created_id
       ON threads (component_id, created_at, id) WHERE status = 'pending'
  5. CREATE INDEX ix_threads_reviewed_at_id
       ON threads (reviewed_at DESC, id DESC) WHERE reviewed_at IS NOT NULL
  6. ALTER `threads` ADD CONSTRAINT ck_threads_status
       CHECK (status IN ('pending', 'approved', 'rejected'))

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. `threads` already exists in the
live Neon database (Phase 1's `scripts/create_thread_tables.py`), and
`create_all(checkfirst=True)` only ever creates whole tables — it will not
add a column, index or constraint to one that is already there. Without
this script the model and the live schema silently disagree, and the first
request that touches `reviewed_at` fails with UndefinedColumn.

WHAT THIS ENABLES. Plan §0.1: a viewer's new thread lands `pending`; an
editor of the widget (or of any ancestor — a root-tab editor approves every
thread widget under that root) approves or rejects it via two new router
endpoints. `reviewed_by_email`/`reviewed_by_name`/`reviewed_at` record WHO
decided and WHEN — all three NULL for a pre-existing row and for an editor's
own auto-approved post (plan M5: History lists only EXPLICIT decisions).
The two partial indexes back the pending queue and the history list
respectively, the same "only the interesting rows, so the index stays tiny
regardless of table growth" shape `notifications`' own unread-only index
already uses.

NO BACKFILL, AND THAT IS THE POINT — plan §3.3. Every live row today is
`approved` with all three review columns NULL, which is EXACTLY what "never
reviewed" (M5) means — there is nothing to compute and nothing to write.

THE CHECK CONSTRAINT MATTERS. `status` has taken exactly one value
(`'approved'`) since Phase 1 and had no CHECK at all. This migration adds
one enforcing the full three-value lifecycle at the database, not just in
application code — a stray fourth value from a future bug or a hand-written
INSERT is rejected at write time instead of silently reaching the router.

THE PARTIAL INDEXES ARE NOT OPTIONAL, and the WHERE clause is the whole
point — same reasoning `scripts/migrate_rbac_is_public.py` gives for its own
partial indexes. A plain index on `status` or `reviewed_at` would grow with
the whole table; these stay bounded by how many threads are actually
pending, or actually reviewed, regardless of how many threads exist overall.

THE BACKEND MUST NOT BE DEPLOYED BEFORE THIS RUNS. `reviewed_at` is read by
the list/history queries on the very first request once phase 2a code is
live (plan §3.3's own note).

SAFE TO RUN MULTIPLE TIMES. Each step probes the catalog first and only
acts if missing, so a partially-applied run converges rather than erroring.
All six statements run inside one transaction — Postgres is transactional
for DDL — so a failure part-way leaves the table exactly as it was.

POSTGRES ONLY, matching every other script in this directory. The test
suite builds its schema from the models via `create_all` on in-memory
SQLite and never runs this script, so there is nothing to support on
SQLite; the script refuses on any other dialect rather than emitting SQL
that would fail halfway.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_moderation_columns

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_moderation_columns --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

TABLE = "threads"
CHECK_NAME = "ck_threads_status"

COLUMNS: dict[str, str] = {
    "reviewed_by_email": "VARCHAR(320)",
    "reviewed_by_name": "VARCHAR(255)",
    "reviewed_at": "TIMESTAMPTZ",
}

INDEXES: dict[str, str] = {
    "ix_threads_pending_created_id": (
        'CREATE INDEX IF NOT EXISTS ix_threads_pending_created_id ON threads '
        '(component_id, created_at, id) WHERE status = \'pending\''
    ),
    "ix_threads_reviewed_at_id": (
        'CREATE INDEX IF NOT EXISTS ix_threads_reviewed_at_id ON threads '
        '(reviewed_at DESC, id DESC) WHERE reviewed_at IS NOT NULL'
    ),
}

ADD_CHECK_SQL = (
    f'ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_NAME} '
    f"CHECK (status IN ('pending', 'approved', 'rejected'))"
)

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

CHECK_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = :name AND contype = 'c'
    )
    """
)


def add_column_sql(column: str, ddl_type: str) -> str:
    return f'ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "{column}" {ddl_type}'


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add threads.reviewed_by_email/reviewed_by_name/reviewed_at, "
        "ck_threads_status, and the pending/history partial indexes (thread moderation)."
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
        if not engine_v2.dialect.has_table(conn, TABLE):
            print(f"  REFUSING: table {TABLE!r} does not exist. Is this the right database?")
            print("  Run scripts/create_thread_tables.py first.")
            raise SystemExit(1)

        plan: list[str] = []

        print("\nPlan:")
        for column, ddl_type in COLUMNS.items():
            exists = bool(
                conn.execute(COLUMN_EXISTS_SQL, {"table": TABLE, "column": column}).scalar()
            )
            print(f"  - {TABLE}.{column} column: {'already exists' if exists else 'will be added'}")
            if not exists:
                plan.append(add_column_sql(column, ddl_type))

        for index, ddl in INDEXES.items():
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  - {index}: {'already exists' if exists else 'will be created'}")
            if not exists:
                plan.append(ddl)

        check_exists = bool(conn.execute(CHECK_EXISTS_SQL, {"name": CHECK_NAME}).scalar())
        print(f"  - {CHECK_NAME}: {'already exists' if check_exists else 'will be added'}")
        if not check_exists:
            plan.append(ADD_CHECK_SQL)

    if not plan:
        print("\nNothing to do — already up to date.")
        return

    if dry_run:
        print("\nWould run:")
        for stmt in plan:
            print(f"    {stmt};")
        print("\n" + "=" * 70)
        print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        print("=" * 70)
        return

    # One transaction for all six: a failure part-way leaves the table
    # exactly as it was, not half-migrated.
    with engine_v2.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
            print(f"    ran: {stmt}")

    with engine_v2.connect() as conn:
        print("\nVerified:")
        for column in COLUMNS:
            present = bool(
                conn.execute(COLUMN_EXISTS_SQL, {"table": TABLE, "column": column}).scalar()
            )
            print(f"  {TABLE}.{column} present = {present}")
        for index in INDEXES:
            present = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  {index} present = {present}")
        check_present = bool(conn.execute(CHECK_EXISTS_SQL, {"name": CHECK_NAME}).scalar())
        print(f"  {CHECK_NAME} present = {check_present}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
