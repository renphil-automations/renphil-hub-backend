"""
One-shot migration: rename ``roles.rank`` to ``roles.depth`` and drop its
NOT NULL, in the database referenced by DATABASE_URL_V2 / PG_V2_* env vars.

Run from the project root:

    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_role_rank_to_depth

Add ``--dry-run`` to print the plan and exit without writing anything.

WHY. ``rank`` carried the role graph's acyclicity: every edge had to satisfy
``parent.rank < child.rank``, so no path could return to its origin. That rule
is disabled by requirement (the code is commented out in
``app/services/rbac_graph_service.py``, not deleted), and the column is
renamed to reflect that it no longer ranks anything. Acyclicity is now a real
cycle walk under an advisory lock, exactly as the scope graph has always done.

NULLABLE because a role created today has no meaningful value to put here and
NOT NULL would force every caller to invent one. Existing values, if any, are
preserved by the rename — this migration never writes a row.

SAFE TO RUN MULTIPLE TIMES. Each step checks the current catalog state first
and skips what is already done, so a partially-applied run (or a re-run after
the rename succeeded but the DROP NOT NULL did not) converges rather than
erroring. Both statements also run inside one transaction, so a failure
half-way leaves the column exactly as it was.

POSTGRES ONLY. ``ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL`` is not
SQLite syntax; the test suite builds its schema from the models via
``create_all`` and never runs this script, so there is nothing to support
there. The script refuses to run against a non-PostgreSQL dialect rather than
emitting SQL that would fail halfway.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from app.db_v2.database import engine_v2

TABLE = "roles"
OLD_COLUMN = "rank"
NEW_COLUMN = "depth"

# `rank` is a reserved word in PostgreSQL (it is a window function), which is
# why both identifiers are quoted here even though `depth` would not need it.
RENAME_SQL = f'ALTER TABLE {TABLE} RENAME COLUMN "{OLD_COLUMN}" TO "{NEW_COLUMN}"'
DROP_NOT_NULL_SQL = f'ALTER TABLE {TABLE} ALTER COLUMN "{NEW_COLUMN}" DROP NOT NULL'

COLUMN_STATE_SQL = text(
    """
    SELECT column_name, is_nullable
      FROM information_schema.columns
     WHERE table_name = :table
       AND column_name IN (:old_column, :new_column)
    """
)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    if engine_v2.dialect.name != "postgresql":
        print(f"  REFUSING: dialect is {engine_v2.dialect.name!r}, not 'postgresql'.")
        raise SystemExit(1)

    with engine_v2.begin() as conn:
        state = {
            row.column_name: row.is_nullable
            for row in conn.execute(
                COLUMN_STATE_SQL,
                {"table": TABLE, "old_column": OLD_COLUMN, "new_column": NEW_COLUMN},
            )
        }

        if OLD_COLUMN not in state and NEW_COLUMN not in state:
            print(
                f"  REFUSING: {TABLE!r} has neither a {OLD_COLUMN!r} nor a "
                f"{NEW_COLUMN!r} column. Is this the right database?"
            )
            raise SystemExit(1)

        if OLD_COLUMN in state and NEW_COLUMN in state:
            print(
                f"  REFUSING: {TABLE!r} has BOTH {OLD_COLUMN!r} and {NEW_COLUMN!r}. "
                f"That is not a state this script created; resolve it by hand."
            )
            raise SystemExit(1)

        needs_rename = OLD_COLUMN in state
        # After a rename the new column inherits the old one's nullability, so
        # read it from whichever column is actually present.
        is_nullable = state.get(NEW_COLUMN, state.get(OLD_COLUMN)) == "YES"

        print(f"  - rename {OLD_COLUMN} -> {NEW_COLUMN}: "
              f"{'will run' if needs_rename else 'already done'}")
        print(f"  - drop NOT NULL on {NEW_COLUMN}: "
              f"{'already done' if is_nullable else 'will run'}")

        if not needs_rename and is_nullable:
            print("Nothing to do.")
            return

        if dry_run:
            print("\n--dry-run: rolling back without writing.")
            for stmt, run in ((RENAME_SQL, needs_rename), (DROP_NOT_NULL_SQL, not is_nullable)):
                if run:
                    print(f"    {stmt}")
            raise SystemExit(0)

        if needs_rename:
            conn.execute(text(RENAME_SQL))
            print(f"    ran: {RENAME_SQL}")
        if not is_nullable:
            conn.execute(text(DROP_NOT_NULL_SQL))
            print(f"    ran: {DROP_NOT_NULL_SQL}")

    with engine_v2.connect() as conn:
        after = conn.execute(
            COLUMN_STATE_SQL,
            {"table": TABLE, "old_column": OLD_COLUMN, "new_column": NEW_COLUMN},
        ).fetchall()
        print(f"Verified: {[(r.column_name, f'nullable={r.is_nullable}') for r in after]}")

    print("Done.")


if __name__ == "__main__":
    main()
