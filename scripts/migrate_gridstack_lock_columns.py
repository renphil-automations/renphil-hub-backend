"""
One-shot migration: add `gridstacks.locked` / `locked_by` / `locked_at`, in
the database referenced by DATABASE_URL_V2 / PG_V2_* env vars — see
session_handoff_2026-09-07-edit-mode-gap.md's follow-up session (independent
per-sub-gridstack locking).

Three steps:

  ALTER TABLE gridstacks ADD COLUMN IF NOT EXISTS locked BOOLEAN DEFAULT false
  ALTER TABLE gridstacks ADD COLUMN IF NOT EXISTS locked_by VARCHAR(255) DEFAULT ''
  ALTER TABLE gridstacks ADD COLUMN IF NOT EXISTS locked_at TIMESTAMP

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. Same reasoning as
`migrate_tab_locked_at.py`: `gridstacks` already exists in the live Neon
database, and `create_all(checkfirst=True)` only ever creates whole tables —
it will not add columns to one that already exists. Without this script the
model and the live schema silently disagree, and the first request that
touches one of these three columns fails with UndefinedColumn.

WHAT THESE COLUMNS ARE FOR. Locking used to be whole-tab-only: a non-root
gridstack (a sub-gridstack nested under a root/variant's own canvas) had no
lock columns at all, and `lock_tab_by_document_id_v2` refused to lock one
("Locking is only supported for top-level tabs in this schema version").
These three columns let a sub-gridstack be locked independently of its
owning root/variant and of any sibling sub-gridstack — see
`gridstack_service._is_root` / `_safe_locked_triple` /
`lock_tab_by_document_id_v2` / `unlock_tab_by_document_id_v2`, which now
branch on root vs. non-root instead of raising. Deliberately the SAME three
columns, same shape, as `TabV2.locked` / `locked_by` / `locked_at` (not a
`components.props` JSONB key, unlike Super Block Note's own node-level
lock) — every sub-gridstack already has its own row here, so there is no
JSONB-blob workaround to reach for.

NO BACKFILL, AND THAT IS THE POINT — same rationale as
`migrate_tab_locked_at.py`. Every gridstack row gets `locked = false`,
`locked_by = ''`, `locked_at = NULL`. Nothing was ever lockable before this
column existed, so there is no pre-existing lock state to reconcile.

NOTHING IS ENFORCED YET AND NOTHING CHANGES FOR ANY USER by running this
alone. Only `lock_tab_by_document_id_v2` / `unlock_tab_by_document_id_v2`
write to these columns going forward.

SAFE TO RUN MULTIPLE TIMES. Each column is probed in the catalog first and
only added if missing, so a partially-applied run converges rather than
erroring.

POSTGRES ONLY, matching every other script in this directory. The test
suite builds its schema from the models via `create_all` on in-memory
SQLite and never runs this script, so there is nothing to support on
SQLite; the script refuses on any other dialect rather than emitting SQL
that would fail halfway.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_gridstack_lock_columns

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_gridstack_lock_columns --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

TABLE = "gridstacks"

COLUMNS: dict[str, str] = {
    "locked": "BOOLEAN DEFAULT false",
    "locked_by": "VARCHAR(255) DEFAULT ''",
    "locked_at": "TIMESTAMP",
}

COLUMN_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = :table AND column_name = :column
    )
    """
)


def add_column_sql(column: str, ddl_type: str) -> str:
    return f'ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "{column}" {ddl_type}'


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add gridstacks.locked / locked_by / locked_at (independent sub-gridstack locking)."
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
            print("  Run scripts/create_v2_schema_tables.py first.")
            raise SystemExit(1)

        existing = {
            column: bool(
                conn.execute(COLUMN_EXISTS_SQL, {"table": TABLE, "column": column}).scalar()
            )
            for column in COLUMNS
        }

    missing = [column for column, present in existing.items() if not present]

    print("\nPlan:")
    for column in COLUMNS:
        print(f"  - {TABLE}.{column} column: {'already exists' if existing[column] else 'will be added'}")

    if not missing:
        print("\nNothing to do — already up to date.")
        return

    if dry_run:
        print("\nWould run:")
        for column in missing:
            print(f"    {add_column_sql(column, COLUMNS[column])};")
        print("\n" + "=" * 70)
        print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        print("=" * 70)
        return

    with engine_v2.begin() as conn:
        for column in missing:
            sql = add_column_sql(column, COLUMNS[column])
            conn.execute(text(sql))
            print(f"    ran: {sql}")

    with engine_v2.connect() as conn:
        print("\nVerified:")
        for column in COLUMNS:
            present = bool(
                conn.execute(COLUMN_EXISTS_SQL, {"table": TABLE, "column": column}).scalar()
            )
            print(f"  {TABLE}.{column} present = {present}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
