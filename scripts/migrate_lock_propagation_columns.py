"""
One-shot migration: add lock-propagation columns, in the database referenced
by DATABASE_URL_V2 / PG_V2_* env vars — see
plan_lock_propagation_2026-09-08.md §3.

Six columns across three tables:

  ALTER TABLE tabs      ADD COLUMN IF NOT EXISTS lock_token VARCHAR(64)
  ALTER TABLE gridstacks ADD COLUMN IF NOT EXISTS lock_token VARCHAR(64)
  ALTER TABLE nav_tabs   ADD COLUMN IF NOT EXISTS locked     BOOLEAN DEFAULT false
  ALTER TABLE nav_tabs   ADD COLUMN IF NOT EXISTS locked_by  VARCHAR(255) DEFAULT ''
  ALTER TABLE nav_tabs   ADD COLUMN IF NOT EXISTS locked_at  TIMESTAMP
  ALTER TABLE nav_tabs   ADD COLUMN IF NOT EXISTS lock_token VARCHAR(64)

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. Same reasoning as every other
script in this directory: `tabs`, `gridstacks` and `nav_tabs` all already
exist in the live Neon database, and `create_all(checkfirst=True)` only ever
creates whole tables — it will not add a column to one that is already
there. Without this script the models and the live schema silently disagree,
and the first request that touches one of these six columns fails with
UndefinedColumn.

PREREQUISITE, checked by hand rather than folded into this script (plan §3.2
and §9 item 5 both flag it): `scripts/migrate_gridstack_lock_columns.py`
must already be `--commit`ed, since it's what put
`gridstacks.locked`/`locked_by`/`locked_at` there in the first place — this
script only adds the ONE new column those two tables were still missing
(`lock_token`) plus all four on `nav_tabs`, which had no lock columns at
all. Verified 2026-09-08 against live Neon: already applied, so this script
does not re-add or re-check those three pre-existing columns — it only
touches what plan §3.1 marks NEW.

WHAT THESE COLUMNS ARE FOR. `lock_token` on `tabs`/`gridstacks` is the
new invalidatable-token half of edit-session enforcement — see those two
models' own comments and `app/services/edit_lock_service.py`. The four
`nav_tabs` columns make a nav tab a real lock node for the first time (its
edit-mode toggle now acquires an actual lock, plan decision 3) — same
three-column shape as `TabV2`/`GridstackV2`'s existing lock columns, plus
the token.

NO BACKFILL, AND THAT IS THE POINT — same rationale as
`migrate_tab_locked_at.py` and `migrate_gridstack_lock_columns.py`. Every
`tabs`/`gridstacks` row (locked or not) gets `lock_token = NULL`, which
`edit_lock_service` treats as "no live session" — the same fail-open-to-
stale posture `locked_at IS NULL` already has, never as an open door. Every
`nav_tabs` row gets `locked = false`, `locked_by = ''`, `locked_at = NULL`,
`lock_token = NULL` — nothing was ever lockable there before these columns
existed, so there is no pre-existing lock state to reconcile.

NOTHING IS ENFORCED YET AND NOTHING CHANGES FOR ANY USER by running this
alone. Phase 1 (this script + the tree resolver) makes no behaviour change;
token enforcement doesn't ship until phase 3.

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
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_lock_propagation_columns

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_lock_propagation_columns --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

# table -> {column: ddl_type}, in the order they'll be applied.
TABLES: dict[str, dict[str, str]] = {
    "tabs": {
        "lock_token": "VARCHAR(64)",
    },
    "gridstacks": {
        "lock_token": "VARCHAR(64)",
    },
    "nav_tabs": {
        "locked": "BOOLEAN DEFAULT false",
        "locked_by": "VARCHAR(255) DEFAULT ''",
        "locked_at": "TIMESTAMP",
        "lock_token": "VARCHAR(64)",
    },
}

COLUMN_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = :table AND column_name = :column
    )
    """
)


def add_column_sql(table: str, column: str, ddl_type: str) -> str:
    return f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{column}" {ddl_type}'


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Add tabs.lock_token, gridstacks.lock_token, and "
            "nav_tabs.{locked,locked_by,locked_at,lock_token} "
            "(lock propagation, plan_lock_propagation_2026-09-08.md §3)."
        )
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
        for table in TABLES:
            if not engine_v2.dialect.has_table(conn, table):
                print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                print("  Run scripts/create_v2_schema_tables.py first.")
                raise SystemExit(1)

        existing: dict[str, dict[str, bool]] = {}
        for table, columns in TABLES.items():
            existing[table] = {
                column: bool(
                    conn.execute(COLUMN_EXISTS_SQL, {"table": table, "column": column}).scalar()
                )
                for column in columns
            }

    missing: list[tuple[str, str]] = [
        (table, column)
        for table, columns in existing.items()
        for column, present in columns.items()
        if not present
    ]

    print("\nPlan:")
    for table, columns in existing.items():
        for column, present in columns.items():
            print(f"  - {table}.{column} column: {'already exists' if present else 'will be added'}")

    if not missing:
        print("\nNothing to do — already up to date.")
        return

    if dry_run:
        print("\nWould run:")
        for table, column in missing:
            print(f"    {add_column_sql(table, column, TABLES[table][column])};")
        print("\n" + "=" * 70)
        print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        print("=" * 70)
        return

    with engine_v2.begin() as conn:
        for table, column in missing:
            sql = add_column_sql(table, column, TABLES[table][column])
            conn.execute(text(sql))
            print(f"    ran: {sql}")

    with engine_v2.connect() as conn:
        print("\nVerified:")
        for table, columns in TABLES.items():
            for column in columns:
                present = bool(
                    conn.execute(COLUMN_EXISTS_SQL, {"table": table, "column": column}).scalar()
                )
                print(f"  {table}.{column} present = {present}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
