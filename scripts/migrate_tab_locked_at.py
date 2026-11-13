"""
One-shot migration: add `tabs.locked_at`, in the database referenced by
DATABASE_URL_V2 / PG_V2_* env vars — see
plan_access_control_algorithm_2026-08-27.md §6.6 Fix 2 and
session_handoff_2026-09-03-locking.md.

One step:

  ALTER TABLE tabs ADD COLUMN locked_at TIMESTAMP

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. `tabs` already exists in the
live Neon database (`scripts/create_v2_schema_tables.py` created it), and
`create_all(checkfirst=True)` only ever creates whole tables — it will not
add a column to one that is already there. Without this script the model
and the live schema silently disagree, and the first request that touches
`tabs.locked_at` fails with UndefinedColumn. Same rationale as
`migrate_rbac_is_public.py`, whose pattern this script follows exactly.

WHAT THE COLUMN IS FOR. A lock (`tabs.locked` / `tabs.locked_by`) with no
`locked_at`, or one older than `Settings.TAB_LOCK_TTL_SECONDS`, is STALE and
claimable by any authenticated user without needing `force` — see
`gridstack_service.is_lock_stale`. This is Fix 2 of the two §6.6 fixes;
Fix 1 (the holder is the authenticated identity, not a request-body field)
needs no schema change and shipped in the same session as this script.

NO BACKFILL, AND THAT IS THE POINT. Every row already locked when this
column appears — including the live tab 42, locked by
roy.abdelnour@renphil.org since 2026-08-07 — gets `locked_at = NULL`. NULL
is treated as "already stale" by `is_lock_stale` (see the model's own
comment on `TabV2.locked_at`), which is what makes tab 42 releasable
immediately after this migration runs, with no second write needed. The
alternative — backfilling from `updated_at` — was considered and rejected:
`updated_at` is touched by rename/reorder/access-control edits that have
nothing to do with when the lock was actually taken, so it would either
under- or over-state a lock's true age with no way to tell which.

NOTHING IS ENFORCED YET AND NOTHING CHANGES FOR ANY USER by running this
alone. The column lands NULL on every existing row (including currently
locked ones); staleness is only ever READ (never written back) by
`is_lock_stale`, and only `lock_tab_by_document_id_v2` /
`unlock_tab_by_document_id_v2` write to it going forward.

SAFE TO RUN MULTIPLE TIMES. The column is probed in the catalog first and
only added if missing, so a partially-applied run (there is only one
statement here, but the shape matches every other script in this
directory) converges rather than erroring.

POSTGRES ONLY, matching scripts/migrate_rbac_is_public.py and
scripts/migrate_resource_grants.py. The test suite builds its schema from
the models via `create_all` on in-memory SQLite and never runs this script,
so there is nothing to support on SQLite; the script refuses on any other
dialect rather than emitting SQL that would fail halfway.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_tab_locked_at

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_tab_locked_at --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

TABLE = "tabs"
COLUMN = "locked_at"

ADD_COLUMN_SQL = f'ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "{COLUMN}" TIMESTAMP'

COLUMN_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = :table AND column_name = :column
    )
    """
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Add tabs.locked_at (plan §6.6 Fix 2).")
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
        # Guard against pointing this at the wrong database entirely: if
        # `tabs` is not here, adding a column to it is not the problem to
        # solve.
        if not engine_v2.dialect.has_table(conn, TABLE):
            print(f"  REFUSING: table {TABLE!r} does not exist. Is this the right database?")
            print("  Run scripts/create_v2_schema_tables.py first.")
            raise SystemExit(1)

        exists = bool(
            conn.execute(COLUMN_EXISTS_SQL, {"table": TABLE, "column": COLUMN}).scalar()
        )
        print("\nPlan:")
        print(f"  - {TABLE}.{COLUMN} column: {'already exists' if exists else 'will be added'}")

    if exists:
        print("\nNothing to do — already up to date.")
        return

    if dry_run:
        print("\nWould run:")
        print(f"    {ADD_COLUMN_SQL};")
        print("\n" + "=" * 70)
        print("This was a DRY RUN — nothing was written. Re-run with --commit to apply.")
        print("=" * 70)
        return

    with engine_v2.begin() as conn:
        conn.execute(text(ADD_COLUMN_SQL))
        print(f"    ran: {ADD_COLUMN_SQL}")

    with engine_v2.connect() as conn:
        print(
            f"\nVerified: {TABLE}.{COLUMN} present = "
            f"{bool(conn.execute(COLUMN_EXISTS_SQL, {'table': TABLE, 'column': COLUMN}).scalar())}"
        )

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
