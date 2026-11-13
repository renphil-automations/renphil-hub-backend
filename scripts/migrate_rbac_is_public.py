"""
One-shot migration: add the ⊥ ("Any Role" / "Any Scope") flag to the two
RBAC definition tables, in the database referenced by DATABASE_URL_V2 /
PG_V2_* env vars — see plan_access_control_algorithm_2026-08-27.md §4.4.

Five steps, in this order:

  1. ALTER `roles`  ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT FALSE
  2. ALTER `scopes` ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT FALSE
  3. CREATE UNIQUE INDEX uq_roles_single_public  ON roles  (is_public)
                                                 WHERE is_public
  4. CREATE UNIQUE INDEX uq_scopes_single_public ON scopes (is_public)
                                                 WHERE is_public
  5. ALTER `scopes` ADD CONSTRAINT ck_scopes_not_universal_and_public
       CHECK (NOT (is_universal AND is_public))

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. The six RBAC tables already
exist in the live Neon database (`scripts/create_rbac_tables.py` created
them), and `create_all(checkfirst=True)` only ever creates whole tables — it
will not add a column to one that is already there. Without this script the
models and the live schema silently disagree, and the first request that
touches `roles.is_public` fails with UndefinedColumn.

WHAT THE FLAG IS. The BOTTOM of each lattice, mirroring `is_universal`'s top
on the scope side. Every scope implicitly contains "Any Scope" and every role
implicitly inherits "Any Role", so both are unioned into every descendant set
by `rbac_graph_service`. That is what makes "open to everyone" a single grant
row on a node rather than one row per scope anybody might hold. It is a FLAG
rather than an edge from every row for the same reason `is_universal` is one:
edges have to be remembered for every newly created scope, and forgetting is
silent. See the comment above `ScopeV2.is_public` for the full argument.

THE PARTIAL INDEXES ARE NOT OPTIONAL, and the WHERE clause is the whole
point. A plain unique index on a boolean permits exactly one `true` row AND
exactly one `false` row — i.e. the second ordinary role anyone creates would
fail with a unique violation. The models spell the predicate out for both
Postgres and SQLite (`postgresql_where` / `sqlite_where`) for the same
reason; this script is the Postgres half of that.

NOTHING IS ENFORCED YET AND NOTHING CHANGES FOR ANY USER. Both columns land
`false` on every existing row, no row is written, and no ⊥ row is created —
authoring those is a deliberate admin action afterwards. The one behaviour
this migration enables is that `is_public` becomes settable at creation; it
is immutable after, so there is no UPDATE path to worry about here.

SAFE TO RUN MULTIPLE TIMES. Every step checks the catalog first and skips
what is already done, so a partially-applied run converges rather than
erroring. All five statements run inside one transaction — Postgres is
transactional for DDL — so a failure part-way leaves both tables exactly as
they were.

On Postgres 11+ an `ADD COLUMN ... NOT NULL DEFAULT FALSE` is a catalog-only
change: the default is stored in the catalog rather than written into every
row, so there is no table rewrite. These tables hold tens of rows regardless.

ONE DELIBERATE DIVERGENCE FROM THE MODELS, worth knowing before comparing
schemas. The models declare `default=False`, which is a CLIENT-side default
SQLAlchemy fills in — `create_all` therefore emits a bare
`is_public BOOLEAN NOT NULL` with no server default. This script has to use
`DEFAULT FALSE`, because an existing table's rows need a value at the moment
the column appears. The `DEFAULT` is then left in place: dropping it would
buy nothing (every INSERT through the ORM supplies the value anyway) and
would make a hand-written INSERT fail instead of doing the obvious thing.
Everything else — both index predicates and the CHECK — is byte-for-byte
what the models emit.

POSTGRES ONLY, matching scripts/migrate_role_rank_to_depth.py. The test suite
builds its schema from the models via `create_all` and never runs this, so
there is nothing to support on SQLite. The script refuses on any other
dialect rather than emitting SQL that would fail halfway.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_rbac_is_public

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_rbac_is_public --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

COLUMN = "is_public"
CHECK_NAME = "ck_scopes_not_universal_and_public"

# Ordered: columns first, then the constraints that depend on them. Each
# entry is (label, catalog-probe, DDL).
ADD_COLUMN_SQL = (
    'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{column}" '
    "BOOLEAN NOT NULL DEFAULT FALSE"
)
CREATE_INDEX_SQL = (
    'CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ("{column}") '
    'WHERE "{column}"'
)
ADD_CHECK_SQL = (
    f'ALTER TABLE scopes ADD CONSTRAINT {CHECK_NAME} '
    f'CHECK (NOT ("is_universal" AND "{COLUMN}"))'
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

TABLES = ("roles", "scopes")
INDEXES = (("roles", "uq_roles_single_public"), ("scopes", "uq_scopes_single_public"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Add roles/scopes.is_public (⊥).")
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
        # Guard against pointing this at the wrong database entirely: if the
        # RBAC tables are not here, adding a column to them is not the
        # problem to solve.
        for table in TABLES:
            if not engine_v2.dialect.has_table(conn, table):
                print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                print("  Run scripts/create_rbac_tables.py first.")
                raise SystemExit(1)

        plan: list[str] = []

        print("\nPlan:")
        for table in TABLES:
            exists = bool(
                conn.execute(COLUMN_EXISTS_SQL, {"table": table, "column": COLUMN}).scalar()
            )
            print(f"  - {table}.{COLUMN} column: {'already exists' if exists else 'will be added'}")
            if not exists:
                plan.append(ADD_COLUMN_SQL.format(table=table, column=COLUMN))

        for table, index in INDEXES:
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  - {index}: {'already exists' if exists else 'will be created'}")
            if not exists:
                plan.append(CREATE_INDEX_SQL.format(index=index, table=table, column=COLUMN))

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

    # One transaction for all five: a failure part-way leaves both tables
    # untouched rather than half-constrained.
    with engine_v2.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
            print(f"    ran: {stmt}")

    with engine_v2.connect() as conn:
        for table in TABLES:
            print(
                f"Verified: {table}.{COLUMN} present = "
                f"{bool(conn.execute(COLUMN_EXISTS_SQL, {'table': table, 'column': COLUMN}).scalar())}"
            )
        for _table, index in INDEXES:
            print(
                f"Verified: {index} present = "
                f"{bool(conn.execute(INDEX_EXISTS_SQL, {'index': index}).scalar())}"
            )
        print(
            f"Verified: {CHECK_NAME} present = "
            f"{bool(conn.execute(CHECK_EXISTS_SQL, {'name': CHECK_NAME}).scalar())}"
        )

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
