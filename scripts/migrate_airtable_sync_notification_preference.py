"""
One-shot migration: each admin's "Receive run notifications" mode for the
Airtable sync, in the database referenced by DATABASE_URL_V2 / PG_V2_* env vars.

  users.airtable_sync_notifications_mode  VARCHAR(8) NULL, no default,
                                          CHECK IN ('all', 'failures', 'none')

It replaces the earlier boolean users.airtable_sync_notifications_enabled.
NULL means "never chose", which counts as `all`, and it stays NULL on everyone
who isn't an admin.

What a run does depends on what it finds:

  Boolean present, mode absent (Dev)
      Add the mode column and its CHECK, then convert once: FALSE -> 'none',
      TRUE -> 'all', NULL stays NULL. Verify the counts match. The boolean stays,
      because local backends on branches without this work still select it.
  Neither present (a fresh database, such as Sandbox or prod)
      Add only the mode column and its CHECK. The boolean is never created.
      Verify every row is NULL.
  Mode already present
      "Nothing to do". It never converts again, which would overwrite later
      choices. It still verifies the column's shape and the CHECK.
  --drop-old-column
      Drop the boolean, only when the mode column exists. Before dropping, print
      how many rows disagree with the value the boolean converts to. A later
      choice made in this UI counts too, so read the breakdown: a row whose
      boolean differs because an OLDER branch changed it after the conversion is
      the case to look for. Wait until the other branches have merged this work.

Steps, all in ONE transaction:
  1. Read the current state; refuse a column or CHECK with the wrong shape.
  2. Do the work above, taking a bounded ACCESS EXCLUSIVE lock on `users`, which
     sign-ins read, so a miss gives the table back and retries. Only when there
     is work to do.
  3. Verify. Also on a database that needed nothing.

The backend code that maps the mode column must not run against a database
without it: the model selects it, so every UserV2 query, sign-in included, would
500. Migrate first, then deploy.

Rollback (revert the code FIRST, for the same reason):
  ALTER TABLE users DROP COLUMN airtable_sync_notifications_mode;
That also drops its CHECK. Where the boolean is still there, the older code picks
up from it, without any change made since the conversion. Once --drop-old-column
has run, older code also needs the boolean back, so first run:
  ALTER TABLE users ADD COLUMN airtable_sync_notifications_enabled BOOLEAN NULL;
  UPDATE users SET airtable_sync_notifications_enabled = FALSE
    WHERE airtable_sync_notifications_mode = 'none';
and only then drop the mode column. `failures` has no boolean equivalent, so
those admins read as on.

Dry run (DEFAULT) runs every step for real inside the transaction, prints the
verification, then ROLLS BACK, so it tests the real data with no writes. It
still takes the lock when there is work to do.

Run from renphil-hub-backend/, with the venv python:

  Dry run:   PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync_notification_preference
  Real run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync_notification_preference --commit
  Drop the boolean, dry run:
             PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync_notification_preference --drop-old-column
  Drop the boolean, real run:
             PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_airtable_sync_notification_preference --drop-old-column --commit
"""

from __future__ import annotations

import argparse
import re
import time

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from app.db_v2.database import engine_v2
from app.db_v2.models.user import (
    AIRTABLE_SYNC_NOTIFICATION_MODES,
    AIRTABLE_SYNC_NOTIFICATIONS_MODE_CHECK,
    AIRTABLE_SYNC_NOTIFICATIONS_MODE_CHECK_NAME,
    UserV2,
)

USERS = "users"
OLD_COLUMN = "airtable_sync_notifications_enabled"
COLUMN = "airtable_sync_notifications_mode"
LENGTH = UserV2.__table__.c[COLUMN].type.length
CHECK_NAME = AIRTABLE_SYNC_NOTIFICATIONS_MODE_CHECK_NAME
ADD_COLUMN_DDL = f"ALTER TABLE {USERS} ADD COLUMN {COLUMN} VARCHAR({LENGTH}) NULL"
ADD_CHECK_DDL = f"ALTER TABLE {USERS} ADD CONSTRAINT {CHECK_NAME} CHECK ({AIRTABLE_SYNC_NOTIFICATIONS_MODE_CHECK})"
DROP_OLD_DDL = f"ALTER TABLE {USERS} DROP COLUMN {OLD_COLUMN}"
# What the boolean converts to; NULL stays NULL. The one definition of the
# conversion, used to write it, verify it, and report disagreements before a drop.
CONVERTED = f"CASE WHEN {OLD_COLUMN} IS FALSE THEN 'none' WHEN {OLD_COLUMN} IS TRUE THEN 'all' END"
CONVERT_DML = f"UPDATE {USERS} SET {COLUMN} = {CONVERTED} WHERE {OLD_COLUMN} IS NOT NULL"
DISAGREE = f"({CONVERTED}) IS DISTINCT FROM {COLUMN}"

LOCK_TIMEOUT = "3s"
LOCK_ATTEMPTS = 20


def _refuse(msg: str) -> None:
    raise RuntimeError(f"REFUSING: {msg}")


def _scalar(conn: Connection, sql: str):
    return conn.execute(text(sql)).scalar()


def _column(conn: Connection, name: str):
    """(data_type, character_maximum_length, is_nullable, column_default) or None."""
    return conn.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": USERS, "c": name},
    ).first()


def _check_def(conn: Connection) -> str | None:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND t.relname = :t AND c.conname = :n "
            "AND c.contype = 'c' AND c.convalidated"
        ),
        {"t": USERS, "n": CHECK_NAME},
    ).scalar()


def _old_right_shape(col) -> bool:
    return col[0] == "boolean" and col[2] == "YES" and col[3] is None


def _mode_right_shape(col) -> bool:
    return col[0] == "character varying" and col[1] == LENGTH and col[2] == "YES" and col[3] is None


def _right_check(definition: str | None) -> bool:
    """Postgres prints the CHECK back in its own form, so compare what it
    allows, not its text."""
    return (
        definition is not None
        and COLUMN in definition
        and sorted(re.findall(r"'([^']*)'", definition)) == sorted(AIRTABLE_SYNC_NOTIFICATION_MODES)
    )


def _lock_users(conn: Connection) -> None:
    """Wait at most LOCK_TIMEOUT per attempt; a miss gives the running app its
    table back and retries (see migrate_airtable_sync._lock_tables)."""
    conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        savepoint = conn.begin_nested()
        try:
            conn.execute(text(f"LOCK TABLE {USERS} IN ACCESS EXCLUSIVE MODE"))
        except OperationalError as exc:
            savepoint.rollback()
            if "lock timeout" not in str(exc).lower():
                raise
            print(f"    table busy (attempt {attempt}/{LOCK_ATTEMPTS}) — retrying")
            time.sleep(1)
            continue
        savepoint.commit()
        conn.execute(text("SET LOCAL lock_timeout = 0"))
        print(f"    locked {USERS}")
        return
    _refuse(
        f"could not lock {USERS} after {LOCK_ATTEMPTS} attempts — a backend using this database "
        "keeps it busy. Try again, or stop that backend for a minute."
    )


def _mode_counts(conn: Connection) -> dict[str | None, int]:
    return {row[0]: row[1] for row in conn.execute(text(f"SELECT {COLUMN}, count(*) FROM {USERS} GROUP BY 1"))}


def _old_counts(conn: Connection) -> dict[bool | None, int]:
    return {row[0]: row[1] for row in conn.execute(text(f"SELECT {OLD_COLUMN}, count(*) FROM {USERS} GROUP BY 1"))}


def _report_disagreements(conn: Connection) -> None:
    rows = conn.execute(
        text(f"SELECT {OLD_COLUMN}, {COLUMN}, count(*) FROM {USERS} WHERE {DISAGREE} GROUP BY 1, 2 ORDER BY 1, 2")
    ).all()
    print(f"    rows whose mode differs from what the boolean converts to: {sum(row[2] for row in rows)}")
    for old, mode, count in rows:
        print(f"      boolean {old!s:>5}, mode {mode!s:>8}: {count}")
    if rows:
        print("    A mode chosen in the new UI shows up here too. A boolean an older branch changed after")
        print("    the conversion is lost by this drop: rerun without --commit and fix it first if that's wrong.")


def _verify(conn: Connection, count_before: int, extra: dict[str, bool]) -> None:
    print("\n[3] verification")
    col = _column(conn, COLUMN)
    checks = {
        f"{USERS}.{COLUMN} is VARCHAR({LENGTH}) NULL with no default": col is not None and _mode_right_shape(col),
        f"CHECK {CHECK_NAME} allows exactly {AIRTABLE_SYNC_NOTIFICATION_MODES}": _right_check(_check_def(conn)),
        f"{USERS} row count unchanged ({count_before})": _scalar(conn, f"SELECT count(*) FROM {USERS}") == count_before,
        **extra,
    }
    for label, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {label}")
    if not all(checks.values()):
        _refuse("verification failed")
    print("    verify OK")


def _add_mode_column(conn: Connection) -> None:
    conn.execute(text(ADD_COLUMN_DDL))
    print(f"    {ADD_COLUMN_DDL}")
    conn.execute(text(ADD_CHECK_DDL))
    print(f"    {ADD_CHECK_DDL}")


def migrate(conn: Connection, *, drop_old_column: bool = False) -> bool:
    """Returns False when there was nothing to do. Verifies either way."""
    # ── 1. read ──────────────────────────────────────────────────────────
    print("\n[1] current state")
    old = _column(conn, OLD_COLUMN)
    mode = _column(conn, COLUMN)
    check = _check_def(conn)
    print(f"    {USERS}.{OLD_COLUMN}: {tuple(old) if old else 'absent'}")
    print(f"    {USERS}.{COLUMN}: {tuple(mode) if mode else 'absent'}")
    print(f"    CHECK {CHECK_NAME}: {check or 'absent'}")
    if old is not None and not _old_right_shape(old):
        _refuse(f"{USERS}.{OLD_COLUMN} exists as {tuple(old)}, expected BOOLEAN NULL with no default")
    if mode is not None and not _mode_right_shape(mode):
        _refuse(f"{USERS}.{COLUMN} exists as {tuple(mode)}, expected VARCHAR({LENGTH}) NULL with no default")
    if mode is not None and not _right_check(check):
        _refuse(f"{USERS}.{COLUMN} exists, but its CHECK {CHECK_NAME} is missing or wrong: {check}")
    if mode is None and check is not None:
        _refuse(f"CHECK {CHECK_NAME} exists without {USERS}.{COLUMN}: {check}")
    if old is not None:
        print(f"    boolean values: {_old_counts(conn)}")
    if mode is not None:
        print(f"    mode values: {_mode_counts(conn)}")
    count_before = _scalar(conn, f"SELECT count(*) FROM {USERS}")
    print(f"    {USERS} rows: {count_before}")

    # ── 2. work, then 3. verify ──────────────────────────────────────────
    if drop_old_column:
        if mode is None:
            _refuse(f"--drop-old-column needs {USERS}.{COLUMN}, which is absent: migrate first, without the flag")
        if old is None:
            print(f"\nNothing to do — {USERS}.{OLD_COLUMN} is already gone.")
            _verify(conn, count_before, {})
            return False
        print("\n[2] drop the boolean column")
        _lock_users(conn)
        _report_disagreements(conn)
        modes_before = _mode_counts(conn)
        count_before = _scalar(conn, f"SELECT count(*) FROM {USERS}")
        conn.execute(text(DROP_OLD_DDL))
        print(f"    {DROP_OLD_DDL}")
        _verify(
            conn,
            count_before,
            {
                f"{USERS}.{OLD_COLUMN} is gone": _column(conn, OLD_COLUMN) is None,
                "mode values unchanged": _mode_counts(conn) == modes_before,
            },
        )
        return True

    if mode is not None:
        print("\nNothing to do — the mode column is present (already migrated, or created fresh). Never converting again.")
        _verify(conn, count_before, {})
        return False

    if old is None:
        print("\n[2] add the mode column (no boolean here, so nothing to convert)")
        _lock_users(conn)
        count_before = _scalar(conn, f"SELECT count(*) FROM {USERS}")
        _add_mode_column(conn)
        _verify(
            conn,
            count_before,
            {
                "every row reads NULL": _scalar(conn, f"SELECT count(*) FROM {USERS} WHERE {COLUMN} IS NOT NULL") == 0,
                f"{USERS}.{OLD_COLUMN} was not created": _column(conn, OLD_COLUMN) is None,
            },
        )
        return True

    print("\n[2] add the mode column and convert the boolean once")
    _lock_users(conn)
    # Read under the lock: an older branch may have written since step 1.
    counts = _old_counts(conn)
    count_before = _scalar(conn, f"SELECT count(*) FROM {USERS}")
    _add_mode_column(conn)
    converted = conn.execute(text(CONVERT_DML)).rowcount
    print(f"    {CONVERT_DML}")
    print(f"    converted {converted} rows; boolean values were {counts}")
    modes = _mode_counts(conn)
    _verify(
        conn,
        count_before,
        {
            f"FALSE ({counts.get(False, 0)}) became 'none'": modes.get("none", 0) == counts.get(False, 0),
            f"TRUE ({counts.get(True, 0)}) became 'all'": modes.get("all", 0) == counts.get(True, 0),
            f"NULL ({counts.get(None, 0)}) stayed NULL": modes.get(None, 0) == counts.get(None, 0),
            "nothing is 'failures'": modes.get("failures", 0) == 0,
            "every row matches its boolean": _scalar(conn, f"SELECT count(*) FROM {USERS} WHERE {DISAGREE}") == 0,
            f"{USERS}.{OLD_COLUMN} is still there": _column(conn, OLD_COLUMN) is not None,
        },
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Add the Airtable sync's per-admin notification mode.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run that executes everything and rolls back.",
    )
    parser.add_argument(
        "--drop-old-column",
        action="store_true",
        help=f"Drop {USERS}.{OLD_COLUMN} instead of migrating. Only when the mode column exists.",
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
            changed = migrate(conn, drop_old_column=args.drop_old_column)
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
