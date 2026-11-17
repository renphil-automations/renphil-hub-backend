"""
One-shot migration: scheduled-run retries + "every N hours" for the Airtable
sync, in the database referenced by DATABASE_URL_V2 / PG_V2_* env vars
(AI Docs/plan_airtable_sync_retries_interval_2026-10-29.md §10.1, phase RI-1).

Needs every earlier sync migration first (scripts/migrate_airtable_sync.py,
incl. phase 10's airtable_sync_bulk_jobs); refuses otherwise.

  airtable_sync_runs.scheduled_slot      TIMESTAMPTZ NULL: the slot a
                                         scheduled attempt is for
    + ix_airtable_sync_runs_scheduled_slot  (scheduled_slot)
                                         WHERE scheduled_slot IS NOT NULL
    + ck_airtable_sync_runs_scheduled_slot  trigger = 'scheduled' OR
                                         scheduled_slot IS NULL
  airtable_sync_settings.interval_minutes INTEGER NULL: minutes between
                                         slots for frequency 'interval'
    + ck_airtable_sync_settings_interval_minutes  the value: NULL, or ≥ 15
                                         and a divisor of 1440
    + ck_airtable_sync_settings_interval  the pair: set exactly when
                                         frequency = 'interval'
    ck_airtable_sync_settings_frequency  rebuilt with 'interval', only if
                                         its definition lacks it

The CHECK texts are taken from the SQLAlchemy model, so a database migrated
here and one created fresh from the model carry the same definitions.

Backfill: NONE. Old scheduled runs keep a NULL slot, so they are never
retried and show no attempt number (incl. Dev runs 10 and 11).

Steps, all in ONE transaction:
  1. Read the current state; refuse anything unexpected (a missing sync
     table, `frequency` shorter than 8, a new column with the wrong type).
  2. Runs: column, index, CHECK.
  3. Settings: column, frequency CHECK, the two interval CHECKs.
  4. Backfill: none.
  5. Verify. Also on a database that needed nothing (already migrated, or
     created fresh from the current model by migrate_airtable_sync.py).

Idempotent: a rerun on a migrated database reports "Nothing to do", verifies,
and exits 0. Locks only what it changes: airtable_sync_runs and/or
airtable_sync_settings.

The backend code that maps these columns must not run against a database
without them (the models select them: every run or settings query 500s).

Rollback:
  UPDATE airtable_sync_settings SET frequency = 'daily', interval_minutes = NULL WHERE frequency = 'interval';
  ALTER TABLE airtable_sync_settings DROP CONSTRAINT ck_airtable_sync_settings_interval_minutes,
    DROP CONSTRAINT ck_airtable_sync_settings_interval, DROP CONSTRAINT ck_airtable_sync_settings_frequency,
    ADD CONSTRAINT ck_airtable_sync_settings_frequency CHECK (frequency IN ('daily', 'weekly')),
    DROP COLUMN interval_minutes;
  DROP INDEX ix_airtable_sync_runs_scheduled_slot;
  ALTER TABLE airtable_sync_runs DROP CONSTRAINT ck_airtable_sync_runs_scheduled_slot, DROP COLUMN scheduled_slot;
  + revert the code.

Dry run (DEFAULT) runs every step for real inside the transaction, prints the
verification, then ROLLS BACK, so it tests the real data with no writes.

Run from renphil-hub-backend/, with the venv python:

  Dry run:   PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync_retries_interval
  Real run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync_retries_interval --commit
"""

from __future__ import annotations

import argparse
import time

from sqlalchemy import CheckConstraint, Table, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from app.db_v2.database import engine_v2
from app.db_v2.models.airtable_sync import AirtableSyncRunV2, AirtableSyncSettingsV2

RUNS = "airtable_sync_runs"
SETTINGS = "airtable_sync_settings"
BULK_JOBS = "airtable_sync_bulk_jobs"

# (table, column, DDL type+null, expected information_schema (data_type, is_nullable))
COLUMNS = [
    (RUNS, "scheduled_slot", "TIMESTAMPTZ NULL", ("timestamp with time zone", "YES")),
    (SETTINGS, "interval_minutes", "INTEGER NULL", ("integer", "YES")),
]

INDEX_NAME = "ix_airtable_sync_runs_scheduled_slot"
# Must match the model's Index exactly (a plain, partial index).
INDEX_DDL = f"CREATE INDEX {INDEX_NAME} ON {RUNS} (scheduled_slot) WHERE scheduled_slot IS NOT NULL"

FREQUENCY_CHECK = "ck_airtable_sync_settings_frequency"
# (table, name); added in this order. The text comes from the model.
NEW_CHECKS = [
    (RUNS, "ck_airtable_sync_runs_scheduled_slot"),
    (SETTINGS, "ck_airtable_sync_settings_interval_minutes"),
    (SETTINGS, "ck_airtable_sync_settings_interval"),
]
# What each CHECK's pg_get_constraintdef must contain (Postgres rewrites the
# text: casts, parentheses, IN → = ANY (ARRAY[...])).
CHECK_MARKERS = {
    "ck_airtable_sync_runs_scheduled_slot": ("'scheduled'", "scheduled_slot IS NULL"),
    "ck_airtable_sync_settings_interval_minutes": (
        "interval_minutes IS NULL",
        "interval_minutes >= 15",
        "(1440 % interval_minutes) = 0",
    ),
    "ck_airtable_sync_settings_interval": (
        "'interval'",
        "interval_minutes IS NOT NULL",
        "<>",
        "interval_minutes IS NULL",
    ),
    FREQUENCY_CHECK: ("'daily'", "'weekly'", "'interval'"),
}

SETTINGS_COLUMNS = (
    "id, schedule_enabled, frequency, run_time, run_weekday, timezone, "
    "last_scheduled_slot, updated_by_email, updated_at"
)

_TABLES: dict[str, Table] = {RUNS: AirtableSyncRunV2.__table__, SETTINGS: AirtableSyncSettingsV2.__table__}


def _model_check(table: str, name: str) -> str:
    for constraint in _TABLES[table].constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    raise RuntimeError(f"the model has no CHECK {name} on {table} — is the RI-1 backend code in place?")


def _scalar(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _refuse(msg: str) -> None:
    raise RuntimeError(f"REFUSING: {msg}")


def _column(conn: Connection, table: str, column: str):
    """(data_type, character_maximum_length, is_nullable) or None."""
    return conn.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()


def _indexdef(conn: Connection, name: str) -> str | None:
    return _scalar(conn, "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n", n=name)


def _checkdef(conn: Connection, table: str, name: str) -> str | None:
    return _scalar(
        conn,
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = :t AND c.conname = :n AND c.contype = 'c'",
        t=table,
        n=name,
    )


def _table_exists(conn: Connection, name: str) -> bool:
    return bool(
        _scalar(
            conn,
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t",
            t=name,
        )
    )


def _settings_row(conn: Connection):
    row = conn.execute(text(f"SELECT {SETTINGS_COLUMNS} FROM {SETTINGS} ORDER BY id")).fetchall()
    return [tuple(r) for r in row]


LOCK_TIMEOUT = "3s"
LOCK_ATTEMPTS = 20


def _lock_tables(conn: Connection, tables: tuple[str, ...]) -> None:
    """Take ACCESS EXCLUSIVE on every table (ALTER TABLE needs it anyway),
    waiting at most LOCK_TIMEOUT per attempt; a miss gives the running app
    its tables back and retries (see migrate_airtable_sync._lock_tables for
    why one unbounded LOCK stalled Dev on 2026-10-22)."""
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


def migrate(conn: Connection) -> bool:
    """Returns False when there was nothing to do (already migrated). Verifies
    either way."""
    # ── 1. read ──────────────────────────────────────────────────────────
    print("\n[1] current state")
    for table in (RUNS, SETTINGS, BULK_JOBS):
        if not _table_exists(conn, table):
            _refuse(f"table {table!r} is missing — run scripts/migrate_airtable_sync.py first")

    frequency = _column(conn, SETTINGS, "frequency")
    print(f"    {SETTINGS}.frequency: {tuple(frequency) if frequency else 'absent'}")
    if frequency is None or frequency[0] != "character varying" or (frequency[1] or 0) < len("interval"):
        _refuse(f"{SETTINGS}.frequency must be VARCHAR(8) or longer to hold 'interval'")

    existing_cols = {(t, c): _column(conn, t, c) for t, c, _, _ in COLUMNS}
    for t, c, _, (dtype, _nullable) in COLUMNS:
        col = existing_cols[(t, c)]
        print(f"    {t}.{c}: {tuple(col) if col else 'absent'}")
        if col is not None and col[0] != dtype:
            _refuse(f"{t}.{c} exists as {col[0]}, expected {dtype}")

    index_before = _indexdef(conn, INDEX_NAME)
    print(f"    index {INDEX_NAME}: {index_before or 'absent'}")
    checks_before = {name: _checkdef(conn, t, name) for t, name in [*NEW_CHECKS, (SETTINGS, FREQUENCY_CHECK)]}
    for name, definition in checks_before.items():
        print(f"    CHECK {name}: {definition or 'absent'}")

    missing_cols = [(t, c) for (t, c), col in existing_cols.items() if col is None]
    missing_checks = [(t, name) for t, name in NEW_CHECKS if checks_before[name] is None]
    rebuild_frequency = "'interval'" not in (checks_before[FREQUENCY_CHECK] or "")

    # A CHECK added below validates every existing row; say which rows would
    # break it rather than fail on Postgres's error. Only possible where the
    # column already existed (a half-applied run, or a hand edit).
    if existing_cols[(RUNS, "scheduled_slot")] is not None and checks_before["ck_airtable_sync_runs_scheduled_slot"] is None:
        bad = _scalar(conn, f"SELECT count(*) FROM {RUNS} WHERE trigger <> 'scheduled' AND scheduled_slot IS NOT NULL")
        if bad:
            _refuse(f"{bad} non-scheduled run(s) have a scheduled_slot; clear them first")
    if existing_cols[(SETTINGS, "interval_minutes")] is not None:
        bad = _scalar(
            conn,
            f"SELECT count(*) FROM {SETTINGS} WHERE (frequency = 'interval') <> (interval_minutes IS NOT NULL) "
            "OR (interval_minutes IS NOT NULL AND (interval_minutes < 15 OR 1440 % interval_minutes <> 0))",
        )
        if bad:
            _refuse(f"{bad} settings row(s) break the interval CHECKs; fix them first")

    counts_before = {t: _scalar(conn, f"SELECT count(*) FROM {t}") for t in (RUNS, SETTINGS, BULK_JOBS)}
    settings_before = _settings_row(conn)
    print(f"    row counts: {counts_before}")
    print(f"    settings row: {settings_before}")

    runs_work = (RUNS, "scheduled_slot") in missing_cols or index_before is None or any(
        t == RUNS for t, _ in missing_checks
    )
    settings_work = (
        (SETTINGS, "interval_minutes") in missing_cols
        or rebuild_frequency
        or any(t == SETTINGS for t, _ in missing_checks)
    )
    changed = runs_work or settings_work
    if not changed:
        print("\nNothing to do — every column, index and CHECK is present (already migrated, or created fresh).")
    else:
        lock = ((RUNS,) if runs_work else ()) + ((SETTINGS,) if settings_work else ())
        _lock_tables(conn, lock)

        # ── 2. runs ──────────────────────────────────────────────────────
        print(f"\n[2] {RUNS}")
        if not runs_work:
            print("    already done")
        if (RUNS, "scheduled_slot") in missing_cols:
            conn.execute(text(f"ALTER TABLE {RUNS} ADD COLUMN scheduled_slot TIMESTAMPTZ NULL"))
            print("    added scheduled_slot TIMESTAMPTZ NULL")
        if index_before is None:
            conn.execute(text(INDEX_DDL))
            print(f"    created index {INDEX_NAME}")

        # ── 3. settings ──────────────────────────────────────────────────
        print(f"\n[3] {SETTINGS}")
        if not settings_work:
            print("    already done")
        if (SETTINGS, "interval_minutes") in missing_cols:
            conn.execute(text(f"ALTER TABLE {SETTINGS} ADD COLUMN interval_minutes INTEGER NULL"))
            print("    added interval_minutes INTEGER NULL")
        if rebuild_frequency:
            if checks_before[FREQUENCY_CHECK] is not None:
                conn.execute(text(f"ALTER TABLE {SETTINGS} DROP CONSTRAINT {FREQUENCY_CHECK}"))
            sql = _model_check(SETTINGS, FREQUENCY_CHECK)
            conn.execute(text(f"ALTER TABLE {SETTINGS} ADD CONSTRAINT {FREQUENCY_CHECK} CHECK ({sql})"))
            print(f"    rebuilt CHECK {FREQUENCY_CHECK}: {sql}")

        # Both tables' new CHECKs, after their columns exist.
        for t, name in missing_checks:
            sql = _model_check(t, name)
            conn.execute(text(f"ALTER TABLE {t} ADD CONSTRAINT {name} CHECK ({sql})"))
            print(f"    added CHECK {name} on {t}: {sql}")

        # ── 4. backfill ──────────────────────────────────────────────────
        print("\n[4] backfill")
        print("    none — old scheduled runs keep a NULL slot (never retried)")

    # ── 5. verify ────────────────────────────────────────────────────────
    print("\n[5] verification")
    checks: dict[str, bool] = {}
    for t, c, _, (dtype, nullable) in COLUMNS:
        col = _column(conn, t, c)
        checks[f"{t}.{c} is {dtype} nullable={nullable}"] = col is not None and col[0] == dtype and col[2] == nullable
    frequency = _column(conn, SETTINGS, "frequency")
    checks[f"{SETTINGS}.frequency holds 8 characters ({frequency[1]})"] = (frequency[1] or 0) >= 8
    indexdef = _indexdef(conn, INDEX_NAME) or ""
    checks[f"index {INDEX_NAME} is plain and partial on scheduled_slot"] = (
        indexdef.startswith("CREATE INDEX")
        and "(scheduled_slot)" in indexdef
        and "WHERE (scheduled_slot IS NOT NULL)" in indexdef
    )
    for t, name in [*NEW_CHECKS, (SETTINGS, FREQUENCY_CHECK)]:
        definition = _checkdef(conn, t, name) or ""
        checks[f"CHECK {name} ({definition or 'absent'})"] = all(m in definition for m in CHECK_MARKERS[name])
    for t, n in counts_before.items():
        checks[f"{t} row count unchanged ({n})"] = _scalar(conn, f"SELECT count(*) FROM {t}") == n
    checks["settings row unchanged"] = _settings_row(conn) == settings_before
    checks["settings interval_minutes is NULL"] = (
        _scalar(conn, f"SELECT count(*) FROM {SETTINGS} WHERE interval_minutes IS NOT NULL") == 0
    )
    checks["every run has a NULL scheduled_slot"] = (
        _scalar(conn, f"SELECT count(*) FROM {RUNS} WHERE scheduled_slot IS NOT NULL") == 0
    )
    for label, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {label}")
    if not all(checks.values()):
        _refuse("verification failed")
    print("    verify OK")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Add the Airtable sync's retry + interval schema.")
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
