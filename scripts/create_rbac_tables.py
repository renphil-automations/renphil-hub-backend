"""
One-shot helper: create the access-control redesign's six new tables
(hub_users, roles, parent_child_roles, scopes,
parent_child_scopes, role_assignments) in the
database referenced by DATABASE_URL_V2 / PG_V2_* env vars — see
plan_access_control_schema_2026-08-22.md §3.

Modelled directly on scripts/create_thread_tables.py: import the models so
they register on BaseV2.metadata, then create_all with checkfirst=True.

Run from the project root:

    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.create_rbac_tables

Safe to run multiple times — CREATE TABLE IF NOT EXISTS semantics via
SQLAlchemy's checkfirst=True.

The tables are created EMPTY and nothing reads them yet. The existing
Airtable-backed access-control path is untouched and keeps running: this
script adds tables, it does not migrate, backfill, or change any behaviour.

Unlike create_thread_tables.py, every FK here points at another table in
this same list — nothing outside the set is referenced — so this script has
no external prerequisite. SQLAlchemy's create_all sorts by FK dependency
automatically; the order below is kept dependency-first for readable log
output.

WHAT THIS DOES NOT ENFORCE. Three of the design's constraints span multiple
rows and cannot be DDL (plan §5). Until the validation service exists,
writing to these tables by hand can produce a graph the design forbids:

  - role edges must satisfy parent.rank < child.rank (§5.2) — this is also
    what makes role cycles impossible, so without it both go unchecked
  - scope edges must not form a cycle, under an advisory lock (§5.4, §5.6)
  - a universal scope must not appear in parent_child_scopes at all (§5.5)

The DDL below does carry the per-row half: no self-edges, no duplicate
edges, no duplicate assignments, at most one universal scope, at most one
public role and one public scope, and no scope that is both universal and
public.

ONLY FOR A DATABASE THAT DOES NOT HAVE THESE TABLES YET. `create_all` with
checkfirst=True creates whole tables and nothing else — it will NOT add a
column to a table that already exists, and it will not notice one is
missing. The live Neon database already has all six, so a schema change to
any of these models needs a migration script beside this one, not a re-run
of this. `scripts/migrate_rbac_is_public.py` is the current example
(roles/scopes `is_public`, the ⊥ flag — algorithm plan §4.4).

`resource_grants` IS DELIBERATELY NOT IN THE LIST BELOW, and it is the
seventh access-control table — run `scripts/migrate_resource_grants.py` for
it, on a fresh database as well as on the live one. Two reasons it is not
here. First, it would break the paragraph above this one: its node arc has
real foreign keys to `hub`, `nav_tabs`, `tabs` and `components`, none of
which are in this set, so this script would stop being self-contained and
would start requiring `create_v2_schema_tables.py` to have run first.
Second, that migration is dry-run by default and probes the catalog per
object; having two scripts able to create the same table with different
safety properties is precisely the confusion the paragraph above warns
about. One creator, and it is the careful one.
"""

from __future__ import annotations

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.user import UserV2
from app.db_v2.models.role import RoleEdgeV2, RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2

TARGET_TABLES = [
    UserV2.__table__,
    RoleV2.__table__,
    ScopeV2.__table__,
    RoleEdgeV2.__table__,
    ScopeEdgeV2.__table__,
    RoleAssignmentV2.__table__,
]


def main() -> None:
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    with engine_v2.connect() as conn:
        for table in TARGET_TABLES:
            exists = engine_v2.dialect.has_table(conn, table.name)
            print(f"  - {table.name}: {'already exists' if exists else 'will be created'}")

    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    print("Done.")


if __name__ == "__main__":
    main()
