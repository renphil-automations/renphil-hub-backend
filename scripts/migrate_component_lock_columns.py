"""
One-shot migration: add `components.locked` / `locked_by` / `locked_at` /
`lock_token`, in the database referenced by DATABASE_URL_V2 / PG_V2_* env
vars — see plan_component_locking_and_sbn_2026-09-17.md §3.

Four columns on one table:

  ALTER TABLE components ADD COLUMN IF NOT EXISTS locked     BOOLEAN DEFAULT false
  ALTER TABLE components ADD COLUMN IF NOT EXISTS locked_by  VARCHAR(255) DEFAULT ''
  ALTER TABLE components ADD COLUMN IF NOT EXISTS locked_at  TIMESTAMP
  ALTER TABLE components ADD COLUMN IF NOT EXISTS lock_token VARCHAR(64)

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. Same reasoning as every other
script in this directory: `components` already exists in the live Neon
database, and `create_all(checkfirst=True)` only ever creates whole tables —
it will not add a column to one that is already there. Without this script
the model and the live schema silently disagree, and the first request that
touches one of these four columns fails with UndefinedColumn.

WHAT THESE COLUMNS ARE FOR. Plan decision A (2026-09-17): every REAL
component (`current_grid_id IS NULL` — an ordinary canvas widget, a Super
Block Note root, or any SBN sub-tab node) becomes a lock-tree node of its
own, the fourth kind beside `tabs`/`gridstacks`/`nav_tabs`. The per-widget
edit modal locks the widget's own row instead of squatting on its whole
canvas, and the Super Block Note's separate `props.locked`/`locked_by`/
`locked_at` JSONB lock (invisible to the lock tree — plan §1 finding 4)
moves onto these same four typed columns so `edit_lock_service` can see it.
Deliberately the SAME four-column shape as the other three tables (mirrored,
not independently designed) and the same `is_lock_stale` TTL helper — real
columns, not a `props` adapter (plan's stated assumption), so a fourth lock
shape is not invented. A sub-grid's representation row (`current_grid_id`
set) gets the columns too — every row of the table does — but is never a
lock node and never written; see `edit_lock_service.lock_node_of`.

NO BACKFILL, AND THAT IS THE POINT — plan §3. Every `components` row gets
`locked = false`, `locked_by = ''`, `locked_at = NULL`, `lock_token = NULL`.
An ordinary widget was never lockable before these columns existed. An SBN
node's `props.locked*` keys are NOT copied over: the moment phase B ships,
those keys stop being read, so any SBN lock held at that instant is simply
dropped — the holder re-locks (same-holder re-entry never conflicts). The
`props` keys are left in place, no longer written; a follow-up may strip
them.

NOTHING IS ENFORCED YET AND NOTHING CHANGES FOR ANY USER by running this
alone. Phase A (this script + the tree resolver) makes no behaviour change;
component lock routes and SBN session enforcement don't ship until phase B.

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
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_component_lock_columns

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_component_lock_columns --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

TABLE = "components"

COLUMNS: dict[str, str] = {
    "locked": "BOOLEAN DEFAULT false",
    "locked_by": "VARCHAR(255) DEFAULT ''",
    "locked_at": "TIMESTAMP",
    "lock_token": "VARCHAR(64)",
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
        description="Add components.locked / locked_by / locked_at / lock_token (components as lock nodes)."
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
