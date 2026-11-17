"""One-shot migration: create the `resource_grants` table — object grants,
the storage behind the read-time visibility algorithm — in the database
referenced by DATABASE_URL_V2 / PG_V2_* env vars. See
plan_access_control_algorithm_2026-08-27.md §7.

Creates, in one transaction:

  1. TABLE resource_grants, with both exclusive-arc CHECKs, the `level`
     CHECK, and seven real foreign keys carrying the §7 delete discipline:
       - hub_id / nav_tab_id / tab_id / component_id  ON DELETE CASCADE
       - role_id / scope_id                           ON DELETE RESTRICT
       - user_id                                      ON DELETE CASCADE
       - granted_by_user_id                           ON DELETE SET NULL
  2. Thirteen indexes: four single-column (id, scope_id, user_id,
     granted_by_user_id), the (role_id, scope_id) read-path index, and the
     EIGHT partial unique indexes that carry the uniqueness rule.

THE DDL IS THE MODEL'S OWN, not a hand-written copy of it. Every statement
below is produced by compiling `CreateTable` / `CreateIndex` over
`ResourceGrantV2.__table__` for the PostgreSQL dialect, so there is no second
spelling of the schema that could drift from the first. That is deliberately
stronger than `migrate_rbac_is_public.py`, which hand-writes its DDL and
verifies it against the models by comparison — a check that has to be re-run
by hand every time either side changes. Here the two cannot disagree, because
there is only one.

WHY EIGHT UNIQUE INDEXES AND NOT THE `UNIQUE` §7 SPECIFIES. §7 names
`UNIQUE (hub_id, nav_tab_id, tab_id, component_id, role_id, scope_id,
user_id, level)`, and that constraint ENFORCES NOTHING: every row here has at
least three NULLs among those columns, and NULLs are distinct in a unique
constraint in both Postgres and SQLite, so exact duplicates all insert. This
was confirmed by building it and inserting duplicates. The replacement —
one partial unique index per node kind × principal form — was chosen by the
owner over `NULLS NOT DISTINCT` (which SQLAlchemy silently drops on SQLite,
so the test suite could never verify it) and over COALESCE sentinel indexes
(which cannot serve "which grants are on this node?"). The full argument is
in the model, above `_uniqueness_indexes`.

NOTHING IS ENFORCED BY RUNNING THIS AND NOTHING CHANGES FOR ANY USER. The
table is created EMPTY, no endpoint reads or writes it, and no existing
response shape is affected. `granted` / `visible` are not wired into
anything; the two folds of §5.1 are not built yet. Authoring grants is a
deliberate admin action afterwards, and per D2 there is no seeding script —
the hub grants nothing until someone writes rows here.

PREREQUISITES, both checked below rather than assumed. The seven tables this
one points at must already exist: `hub`, `nav_tabs`, `tabs`, `components`
(the node arc) and `roles`, `scopes`, `hub_users` (the principal arc and
provenance). On the live Neon database all seven are present. This is also
why `resource_grants` is NOT in `scripts/create_rbac_tables.py`'s list —
that script is documented as self-contained, and adding a table with foreign
keys outside its own set would end that. See its docstring.

SAFE TO RUN MULTIPLE TIMES. The table and every index are probed in the
catalog first and only the missing ones are emitted, so a partially-applied
run converges. All statements run inside one transaction — Postgres is
transactional for DDL — so a failure part-way leaves the database exactly as
it was.

POSTGRES ONLY, matching scripts/migrate_rbac_is_public.py and
scripts/migrate_role_rank_to_depth.py. The test suite builds its schema from
the models via `create_all` on in-memory SQLite and never runs this, so there
is nothing to support on SQLite; the script refuses on any other dialect
rather than emitting SQL that would fail halfway.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_resource_grants

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_resource_grants --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db_v2.database import engine_v2
from app.db_v2.models.resource_grant import ResourceGrantV2

TABLE = ResourceGrantV2.__table__

# Every table the node arc, the principal arc and the provenance FK point at.
# If any is missing, this is either the wrong database or one that has not
# had create_v2_schema_tables.py / create_rbac_tables.py run against it —
# either way, creating this table is not the problem to solve.
REQUIRED_TABLES = ("hub", "nav_tabs", "tabs", "components", "roles", "scopes", "hub_users")

INDEX_EXISTS_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :index)"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create resource_grants (object grants).")
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

    dialect = engine_v2.dialect

    with engine_v2.connect() as conn:
        for table in REQUIRED_TABLES:
            if not dialect.has_table(conn, table):
                print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                print("  resource_grants has real foreign keys to it; run the")
                print("  create_*_tables.py scripts first.")
                raise SystemExit(1)

        plan: list[str] = []

        print("\nPlan:")
        table_exists = dialect.has_table(conn, TABLE.name)
        print(f"  - {TABLE.name} table: {'already exists' if table_exists else 'will be created'}")
        if not table_exists:
            plan.append(str(CreateTable(TABLE).compile(dialect=dialect)).strip())

        # Indexes are probed and emitted individually rather than assumed to
        # follow the table. CreateTable does not carry them, and a run that
        # failed between the two would otherwise leave a table with no
        # uniqueness rule on it — which is the one state this migration must
        # never converge to quietly.
        for index in sorted(TABLE.indexes, key=lambda i: i.name):
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index.name}).scalar())
            print(f"  - {index.name}: {'already exists' if exists else 'will be created'}")
            if not exists:
                plan.append(str(CreateIndex(index).compile(dialect=dialect)).strip())

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

    with engine_v2.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
            print(f"    ran: {stmt.splitlines()[0]} ...")

    with engine_v2.connect() as conn:
        print(f"\nVerified: {TABLE.name} present = {dialect.has_table(conn, TABLE.name)}")
        missing = [
            index.name
            for index in TABLE.indexes
            if not conn.execute(INDEX_EXISTS_SQL, {"index": index.name}).scalar()
        ]
        print(f"Verified: {len(TABLE.indexes) - len(missing)}/{len(TABLE.indexes)} indexes present")
        if missing:
            print(f"  MISSING: {', '.join(sorted(missing))}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
