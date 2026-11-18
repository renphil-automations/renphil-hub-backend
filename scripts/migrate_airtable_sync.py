"""
One-shot migration: the Airtable sync's schema, in the database referenced by
DATABASE_URL_V2 / PG_V2_* env vars
(AI Docs/plan_airtable_sync_v2_2026-10-21.md §4, phase 1).

Columns added to existing tables:

  users.airtable_record_id      VARCHAR(32) NULL + partial unique index
                                uq_users_airtable_record_id
  scopes.airtable_record_id     VARCHAR(32) NULL + partial unique index
                                uq_scopes_airtable_record_id
  scopes.type                   VARCHAR(16) NULL, CHECK in ('cluster','program')
  parent_child_scopes.source    VARCHAR(16) NOT NULL DEFAULT 'manual',
                                CHECK in ('manual','airtable_sync')
  role_assignments.source       VARCHAR(16) NOT NULL DEFAULT 'manual',
                                CHECK in ('manual','default','airtable_sync')

Backfill: role_assignments rows on the exact (public role, public scope) pair
with granted_by_user_id IS NULL become 'default' (that is what
add_default_assignment wrote); every other row stays 'manual'. Every edge is
'manual'. No scope gets a type, no row gets a Record ID — the sync sets those.

New tables (created from the SQLAlchemy models, so they match them exactly):
  airtable_sync_settings (+ its single row, id = 1), airtable_designation_mappings,
  airtable_sync_runs, airtable_sync_records, airtable_sync_items, airtable_sync_log

Added in phase 2 (2026-10-22): the run lock,
  uq_airtable_sync_runs_running   UNIQUE (status) WHERE status = 'running'
On a database migrated by the phase-1 version, a re-run adds ONLY this index
(and locks only airtable_sync_runs for it). On a fresh database the table is
created with it.

Added in phase 7 (2026-10-24): the sync's bell notifications
(v1 plan §3.1 rows "notifications", §9.1),
  notifications.component_id    DROP NOT NULL (sync rows name no widget)
  notifications.thread_id       DROP NOT NULL (nor a thread)
  notifications.link            VARCHAR(512) NULL (the in-app path to open)
On a database migrated by an earlier version, a re-run makes ONLY these
changes and locks only `notifications` for them. Both foreign keys keep
their ON DELETE CASCADE; a NULL simply references nothing.

Added in phase 10 (2026-10-27): background bulk resolves,
  airtable_sync_bulk_jobs       one row per "Accept all" / "Keep all" job,
                                with uq_airtable_sync_bulk_jobs_running
                                (UNIQUE (status) WHERE status = 'running')
On a database migrated by an earlier version, a re-run ONLY creates this
table (from the model) and locks no existing table.

Steps, all in ONE transaction:
  1. Read the current state; refuse anything unexpected (wrong type on an
     existing column, etc.).
  2. Add the columns, indexes and CHECKs that are missing.
  3. Backfill role_assignments.source.
  4. Create the missing sync tables; insert the settings row; add the
     sync-table indexes missing from tables that already existed.
  5. Verify.

Idempotent: a rerun on a migrated database reports "nothing to do" and exits 0.

The backend code that maps these columns must ship WITH this (the models now
select them). Rollback:
  (phase 10 only: DROP TABLE airtable_sync_bulk_jobs;)
  (phase 7 only: DELETE FROM notifications WHERE thread_id IS NULL OR component_id IS NULL;
   ALTER TABLE notifications DROP COLUMN link,
     ALTER COLUMN component_id SET NOT NULL, ALTER COLUMN thread_id SET NOT NULL;)
  (phase 2 only: DROP INDEX uq_airtable_sync_runs_running;)
  DROP TABLE airtable_sync_log, airtable_sync_items, airtable_sync_records,
             airtable_sync_runs, airtable_designation_mappings,
             airtable_sync_settings;
  ALTER TABLE role_assignments DROP COLUMN source;
  ALTER TABLE parent_child_scopes DROP COLUMN source;
  ALTER TABLE scopes DROP COLUMN type, DROP COLUMN airtable_record_id;
  ALTER TABLE users DROP COLUMN airtable_record_id;
  + revert the code.

Dry run (DEFAULT) runs every step for real inside the transaction, prints the
verification, then ROLLS BACK, so it tests the real data with no writes.

Run from renphil-hub-backend/, with the venv python:

  Dry run:   PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync
  Real run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync --commit
"""

from __future__ import annotations

import argparse
import time

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from app.db_v2.database import engine_v2
from app.db_v2.models.airtable_sync import SYNC_TABLES

# (table, column, DDL type+null+default, expected information_schema facts)
COLUMNS = [
    ("users", "airtable_record_id", "VARCHAR(32) NULL", ("character varying", 32, "YES")),
    ("scopes", "airtable_record_id", "VARCHAR(32) NULL", ("character varying", 32, "YES")),
    ("scopes", "type", "VARCHAR(16) NULL", ("character varying", 16, "YES")),
    (
        "parent_child_scopes",
        "source",
        "VARCHAR(16) NOT NULL DEFAULT 'manual'",
        ("character varying", 16, "NO"),
    ),
    (
        "role_assignments",
        "source",
        "VARCHAR(16) NOT NULL DEFAULT 'manual'",
        ("character varying", 16, "NO"),
    ),
]

INDEXES = {
    "uq_users_airtable_record_id": (
        "CREATE UNIQUE INDEX uq_users_airtable_record_id ON users (airtable_record_id) "
        "WHERE airtable_record_id IS NOT NULL"
    ),
    "uq_scopes_airtable_record_id": (
        "CREATE UNIQUE INDEX uq_scopes_airtable_record_id ON scopes (airtable_record_id) "
        "WHERE airtable_record_id IS NOT NULL"
    ),
}

# Indexes on the sync's OWN tables that were added after phase 1 created
# them. `table.create` makes them on a fresh database; these statements
# backfill them where the table already exists. (index -> (table, DDL)); the
# DDL must match the model's Index exactly.
SYNC_INDEXES = {
    "uq_airtable_sync_runs_running": (
        "airtable_sync_runs",
        "CREATE UNIQUE INDEX uq_airtable_sync_runs_running ON airtable_sync_runs (status) "
        "WHERE status = 'running'",
    ),
}

# Phase 7: the notifications table (v1 §3.1, §9.1). Columns that must become
# NULL-able, and columns to add (column -> (DDL, expected facts)).
NOTIFICATION_NULLABLE = ("component_id", "thread_id")
NOTIFICATION_COLUMNS = {
    "link": ("VARCHAR(512) NULL", ("character varying", 512, "YES")),
}

# Sync tables added after phase 1 (phase 10: the bulk jobs). Created from
# the model like the others, but on a migrated database without locking the
# core tables.
LATER_TABLES = ("airtable_sync_bulk_jobs",)

CHECKS = {
    ("scopes", "ck_scopes_type"): "type IS NULL OR type IN ('cluster', 'program')",
    ("parent_child_scopes", "ck_parent_child_scopes_source"): "source IN ('manual', 'airtable_sync')",
    ("role_assignments", "ck_role_assignments_source"): "source IN ('manual', 'default', 'airtable_sync')",
}


def _scalar(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _refuse(msg: str) -> None:
    raise RuntimeError(f"REFUSING: {msg}")


def _column(conn: Connection, table: str, column: str):
    """(data_type, character_maximum_length, is_nullable, column_default) or None."""
    return conn.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()


def _index_exists(conn: Connection, name: str) -> bool:
    return bool(_scalar(conn, "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n", n=name))


def _check_exists(conn: Connection, table: str, name: str) -> bool:
    return bool(
        _scalar(
            conn,
            "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :t AND c.conname = :n AND c.contype = 'c'",
            t=table,
            n=name,
        )
    )


def _table_exists(conn: Connection, name: str) -> bool:
    return bool(
        _scalar(
            conn,
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t",
            t=name,
        )
    )


LOCK_TIMEOUT = "3s"
LOCK_ATTEMPTS = 20


def _lock_tables(conn: Connection, tables: tuple[str, ...]) -> None:
    """Take ACCESS EXCLUSIVE on every table (ALTER TABLE needs it anyway),
    waiting at most LOCK_TIMEOUT per attempt.

    Why not one plain LOCK: a running backend on the same database keeps short
    read transactions open on these tables. An unbounded LOCK queues behind
    them AND blocks every new reader behind itself, so the app stalls, and on
    Dev the Neon pooler dropped the waiting connection after a few minutes
    (2026-10-22). A short timeout inside a savepoint gives the app its tables
    back after each miss and retries in the gap between its transactions."""
    conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        savepoint = conn.begin_nested()
        try:
            conn.execute(text(f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE"))
        except OperationalError as exc:
            savepoint.rollback()
            if "lock timeout" not in str(exc).lower():
                raise
            print(f"    tables busy (attempt {attempt}/{LOCK_ATTEMPTS}) — retrying")
            time.sleep(1)
            continue
        savepoint.commit()
        conn.execute(text("SET LOCAL lock_timeout = 0"))
        print(f"    locked {', '.join(tables)}")
        return
    _refuse(
        f"could not lock {', '.join(tables)} after {LOCK_ATTEMPTS} attempts — a backend using this "
        "database keeps them busy. Try again, or stop that backend for a minute."
    )


def _public_pair(conn: Connection) -> tuple[int, int] | None:
    role = _scalar(conn, "SELECT id FROM roles WHERE is_public ORDER BY id LIMIT 1")
    scope = _scalar(conn, "SELECT id FROM scopes WHERE is_public ORDER BY id LIMIT 1")
    return (role, scope) if role is not None and scope is not None else None


def _source_counts(conn: Connection) -> dict:
    if _column(conn, "role_assignments", "source") is None:
        return {}
    rows = conn.execute(text("SELECT source, count(*) FROM role_assignments GROUP BY 1")).fetchall()
    return {k: v for k, v in rows}


def migrate(conn: Connection) -> bool:
    """Returns False when there was nothing to do (already migrated)."""
    # ── 1. read ──────────────────────────────────────────────────────────
    print("\n[1] current state")
    for table in ("users", "scopes", "parent_child_scopes", "role_assignments", "roles"):
        if not _table_exists(conn, table):
            _refuse(f"table {table!r} is missing — is this the merged v2 database?")
    if _table_exists(conn, "hub_users") and _column(conn, "users", "has_signed_in") is None:
        _refuse("users has no has_signed_in column — the users/hub_users merge is not applied here")

    existing_cols = {(t, c): _column(conn, t, c) for t, c, _, _ in COLUMNS}
    for (t, c), col in existing_cols.items():
        print(f"    {t}.{c}: {tuple(col) if col else 'absent'}")
    for t, c, _, (dtype, length, _nullable) in COLUMNS:
        col = existing_cols[(t, c)]
        if col is not None and (col[0] != dtype or col[1] != length):
            _refuse(f"{t}.{c} exists as {col[0]}({col[1]}), expected {dtype}({length})")
    missing_indexes = [n for n in INDEXES if not _index_exists(conn, n)]
    missing_checks = [k for k in CHECKS if not _check_exists(conn, *k)]
    missing_tables = [t.name for t in SYNC_TABLES if not _table_exists(conn, t.name)]
    # Only for tables that exist; a table created below gets its indexes then.
    missing_sync_indexes = [
        n for n, (t, _) in SYNC_INDEXES.items() if t not in missing_tables and not _index_exists(conn, n)
    ]
    print(f"    missing indexes: {missing_indexes or 'none'}")
    print(f"    missing CHECKs:  {[k[1] for k in missing_checks] or 'none'}")
    print(f"    missing tables:  {missing_tables or 'none'}")
    print(f"    missing sync-table indexes: {missing_sync_indexes or 'none'}")

    if not _table_exists(conn, "notifications"):
        _refuse("table 'notifications' is missing — is this the merged v2 database?")
    for c in NOTIFICATION_NULLABLE:
        if _column(conn, "notifications", c) is None:
            _refuse(f"notifications.{c} is missing")
    not_null_notification_cols = [
        c for c in NOTIFICATION_NULLABLE if _column(conn, "notifications", c)[2] == "NO"
    ]
    notification_cols = {c: _column(conn, "notifications", c) for c in NOTIFICATION_COLUMNS}
    for c, (_, (dtype, length, _nullable)) in NOTIFICATION_COLUMNS.items():
        col = notification_cols[c]
        if col is not None and (col[0] != dtype or col[1] != length):
            _refuse(f"notifications.{c} exists as {col[0]}({col[1]}), expected {dtype}({length})")
    missing_notification_cols = [c for c, col in notification_cols.items() if col is None]
    print(f"    notifications NOT NULL to drop: {not_null_notification_cols or 'none'}")
    print(f"    notifications columns missing:  {missing_notification_cols or 'none'}")
    notification_work = bool(not_null_notification_cols or missing_notification_cols)

    if "uq_airtable_sync_runs_running" in missing_sync_indexes:
        running = _scalar(conn, "SELECT count(*) FROM airtable_sync_runs WHERE status = 'running'")
        print(f"    airtable_sync_runs rows with status 'running': {running}")
        if running > 1:
            _refuse(f"{running} runs are 'running'; mark all but one 'interrupted' before adding the run lock")

    counts_before = {
        t: _scalar(conn, f"SELECT count(*) FROM {t}")
        for t in ("users", "scopes", "parent_child_scopes", "role_assignments")
    }
    print(f"    row counts: {counts_before}")

    # A table added after phase 1 needs no lock on the core tables: creating
    # it touches only its own FK targets, briefly.
    core_missing_tables = [t for t in missing_tables if t not in LATER_TABLES]
    core_work = not all(existing_cols.values()) or missing_indexes or missing_checks or core_missing_tables
    if not core_work and not missing_tables and not missing_sync_indexes and not notification_work:
        print("\nNothing to do — every column, index, CHECK and table is present (already migrated).")
        print(f"    role_assignments.source counts: {_source_counts(conn)}")
        return False

    # Lock only what this run changes: a phase-1-migrated database needs just
    # airtable_sync_runs, and the core tables stay free for the running app.
    lock = ("users", "scopes", "parent_child_scopes", "role_assignments") if core_work else ()
    lock += tuple(sorted({SYNC_INDEXES[n][0] for n in missing_sync_indexes}))
    lock += ("notifications",) if notification_work else ()
    if lock:
        _lock_tables(conn, lock)

    # ── 2. columns, indexes, CHECKs ──────────────────────────────────────
    print("\n[2] columns / indexes / CHECKs")
    added_source = existing_cols[("role_assignments", "source")] is None
    for t, c, ddl, _ in COLUMNS:
        if existing_cols[(t, c)] is None:
            conn.execute(text(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}"))
            print(f"    added {t}.{c} {ddl}")
    for name in missing_indexes:
        conn.execute(text(INDEXES[name]))
        print(f"    created index {name}")
    for t, name in missing_checks:
        conn.execute(text(f"ALTER TABLE {t} ADD CONSTRAINT {name} CHECK ({CHECKS[(t, name)]})"))
        print(f"    added CHECK {name} on {t}")

    # ── 3. backfill role_assignments.source ──────────────────────────────
    print("\n[3] role_assignments.source backfill")
    pair = _public_pair(conn)
    defaults = 0
    if not added_source:
        print("    column existed before this run — not re-backfilling")
    elif pair is None:
        print("    no public role and/or public scope row — every row stays 'manual'")
    else:
        defaults = conn.execute(
            text(
                "UPDATE role_assignments SET source = 'default' "
                "WHERE role_id = :r AND scope_id = :s AND granted_by_user_id IS NULL"
            ),
            {"r": pair[0], "s": pair[1]},
        ).rowcount
        print(f"    (role {pair[0]}, scope {pair[1]}) with no granter -> 'default': {defaults} row(s)")
    print(f"    counts: {_source_counts(conn)}")

    # ── 4. sync tables ───────────────────────────────────────────────────
    print("\n[4] sync tables")
    for table in SYNC_TABLES:
        if table.name in missing_tables:
            table.create(conn)
            print(f"    created {table.name}")
    if not _scalar(conn, "SELECT 1 FROM airtable_sync_settings WHERE id = 1"):
        conn.execute(text("INSERT INTO airtable_sync_settings (id) VALUES (1)"))
        print("    inserted the settings row (schedule off, daily 06:00 America/New_York)")
    for name in missing_sync_indexes:
        conn.execute(text(SYNC_INDEXES[name][1]))
        print(f"    created index {name}")

    # ── 4b. notifications (phase 7) ──────────────────────────────────────
    print("\n[4b] notifications")
    if not notification_work:
        print("    already done")
    for c in not_null_notification_cols:
        conn.execute(text(f"ALTER TABLE notifications ALTER COLUMN {c} DROP NOT NULL"))
        print(f"    notifications.{c}: dropped NOT NULL")
    for c in missing_notification_cols:
        ddl = NOTIFICATION_COLUMNS[c][0]
        conn.execute(text(f"ALTER TABLE notifications ADD COLUMN {c} {ddl}"))
        print(f"    added notifications.{c} {ddl}")

    # ── 5. verify ────────────────────────────────────────────────────────
    print("\n[5] verification")
    checks: dict[str, bool] = {}
    for t, c, _, (dtype, length, nullable) in COLUMNS:
        col = _column(conn, t, c)
        checks[f"{t}.{c} is {dtype}({length}) nullable={nullable}"] = (
            col is not None and col[0] == dtype and col[1] == length and col[2] == nullable
        )
    for t in ("parent_child_scopes", "role_assignments"):
        col = _column(conn, t, "source")
        checks[f"{t}.source defaults to 'manual'"] = col is not None and "'manual'" in (col[3] or "")
    for name in (*INDEXES, *SYNC_INDEXES):
        checks[f"index {name}"] = _index_exists(conn, name)
    checks["uq_airtable_sync_runs_running is UNIQUE ... WHERE status = 'running'"] = bool(
        _scalar(
            conn,
            "SELECT 1 FROM pg_indexes WHERE indexname = 'uq_airtable_sync_runs_running' "
            "AND indexdef ILIKE 'CREATE UNIQUE INDEX%' AND indexdef LIKE '%WHERE%running%'",
        )
    )
    checks["uq_airtable_sync_bulk_jobs_running is UNIQUE ... WHERE status = 'running'"] = bool(
        _scalar(
            conn,
            "SELECT 1 FROM pg_indexes WHERE indexname = 'uq_airtable_sync_bulk_jobs_running' "
            "AND indexdef ILIKE 'CREATE UNIQUE INDEX%' AND indexdef LIKE '%WHERE%running%'",
        )
    )
    for t, name in CHECKS:
        checks[f"CHECK {name}"] = _check_exists(conn, t, name)
    for table in SYNC_TABLES:
        checks[f"table {table.name}"] = _table_exists(conn, table.name)
    for c in NOTIFICATION_NULLABLE:
        checks[f"notifications.{c} is NULL-able"] = _column(conn, "notifications", c)[2] == "YES"
    for c, (_, (dtype, length, nullable)) in NOTIFICATION_COLUMNS.items():
        col = _column(conn, "notifications", c)
        checks[f"notifications.{c} is {dtype}({length}) nullable={nullable}"] = (
            col is not None and col[0] == dtype and col[1] == length and col[2] == nullable
        )
    checks["settings row id = 1"] = _scalar(conn, "SELECT count(*) FROM airtable_sync_settings") == 1
    for t, n in counts_before.items():
        checks[f"{t} row count unchanged ({n})"] = _scalar(conn, f"SELECT count(*) FROM {t}") == n
    checks["no scope has a type or Record ID"] = (
        _scalar(conn, "SELECT count(*) FROM scopes WHERE type IS NOT NULL OR airtable_record_id IS NOT NULL") == 0
        or existing_cols[("scopes", "type")] is not None
    )
    checks["no user has a Record ID"] = (
        _scalar(conn, "SELECT count(*) FROM users WHERE airtable_record_id IS NOT NULL") == 0
        or existing_cols[("users", "airtable_record_id")] is not None
    )
    checks["every edge is 'manual'"] = (
        _scalar(conn, "SELECT count(*) FROM parent_child_scopes WHERE source <> 'manual'") == 0
        or existing_cols[("parent_child_scopes", "source")] is not None
    )
    for label, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {label}")
    if not all(checks.values()):
        _refuse("verification failed")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Add the Airtable sync schema.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run that executes everything and rolls back.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("COMMIT MODE — writes will be made" if args.commit else "DRY RUN — runs every step, then ROLLS BACK")
    print("=" * 70)
    host = (engine_v2.url.host or "").split(".")[0]
    print(f"Target host: {host or '(none)'}  database: {engine_v2.url.database}")

    if engine_v2.dialect.name != "postgresql":
        print(f"  REFUSING: dialect is {engine_v2.dialect.name!r}, not 'postgresql'.")
        raise SystemExit(1)

    with engine_v2.connect() as conn:
        trans = conn.begin()
        try:
            changed = migrate(conn)
        except Exception as exc:
            trans.rollback()
            print(f"\n{exc}\nRolled back — nothing was written.")
            raise SystemExit(1) from exc
        if not changed:
            trans.rollback()
            return
        if args.commit:
            trans.commit()
            print("\n" + "=" * 70 + "\nChanges committed.\n" + "=" * 70)
        else:
            trans.rollback()
            print("\n" + "=" * 70 + "\nDRY RUN — rolled back, nothing was written. Re-run with --commit to apply.\n" + "=" * 70)


if __name__ == "__main__":
    main()
