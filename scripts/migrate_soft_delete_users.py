"""
One-shot migration: the soft-delete columns on `users`, in the database
referenced by DATABASE_URL_V2 / PG_V2_* env vars
(AI Docs/plan_admin_soft_delete_user_2026-10-19.md §2).

Why (owner and colleague, 2026-10-19): an admin delete keeps the `users` row
"for backlog purposes" and marks it deleted, so a Restore can bring everything
back. The flag is the EXISTING `users.is_deleted` (its name is kept); the two
new columns record when and by whom.

  is_deleted   BOOLEAN NOT NULL DEFAULT false   (exists; NULLs -> false, then
                                                  NOT NULL / DEFAULT enforced;
                                                  added if absent)
  deleted_at   TIMESTAMPTZ NULL                 (new)
  deleted_by   VARCHAR(320) NULL                (new; the deleting admin's
                                                  EMAIL, a snapshot, no FK)

The email unique indexes are NOT touched: a deleted row keeps its email, so
re-adding that person always means Restore, never a second row.

Touches ONLY `public.users`. Steps, all in ONE transaction:

   1. LOCK users, read the three columns and the is_deleted counts.
   2. is_deleted: add it if absent; otherwise backfill NULL -> false, then
      SET DEFAULT false and SET NOT NULL where missing.
   3. ADD COLUMN deleted_at / deleted_by where absent.
   4. Verify every column's type / nullability / default, the row count, and
      that no row was flagged deleted by this script.

Idempotent: a rerun on a migrated table reports "nothing to do" and exits 0.

The backend code that maps these columns must ship WITH this (the model's
`is_deleted` / `deleted_at` / `deleted_by` would fail on a table without them).
Rollback:
  ALTER TABLE users DROP COLUMN deleted_at, DROP COLUMN deleted_by;  + revert
  the code. (`is_deleted` existed before; leave it.)

Dry run (DEFAULT) runs every step for real inside the transaction, prints the
verification, then ROLLS BACK, so it tests the real data with no writes.

Run from renphil-hub-backend/, with the venv python:

  Dry run:   PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_soft_delete_users
  Real run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_soft_delete_users --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db_v2.database import engine_v2

USERS = "users"
FLAG = "is_deleted"
NEW_COLUMNS = {
    "deleted_at": "TIMESTAMPTZ NULL",
    "deleted_by": "VARCHAR(320) NULL",
}


def _scalar(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _rows(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).fetchall()


def _refuse(msg: str) -> None:
    raise RuntimeError(f"REFUSING: {msg}")


def _column(conn: Connection, column: str):
    """(data_type, is_nullable, column_default, character_maximum_length) for
    public.users.<column>, or None."""
    return conn.execute(
        text(
            "SELECT data_type, is_nullable, column_default, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": USERS, "c": column},
    ).first()


def _flag_counts(conn: Connection) -> dict:
    return {str(k): v for k, v in _rows(conn, f"SELECT {FLAG}, count(*) FROM {USERS} GROUP BY 1")}


def _flag_ok(col) -> bool:
    return (
        col is not None
        and col[0] == "boolean"
        and col[1] == "NO"
        and (col[2] or "").lower() == "false"
    )


def migrate(conn: Connection) -> bool:
    """Returns False when there was nothing to do (already migrated)."""
    # ── 1. lock + read ───────────────────────────────────────────────────
    conn.execute(text(f"LOCK TABLE {USERS} IN ACCESS EXCLUSIVE MODE"))
    total_before = _scalar(conn, f"SELECT count(*) FROM {USERS}")
    flag = _column(conn, FLAG)
    existing = {name: _column(conn, name) for name in NEW_COLUMNS}
    print(f"\n[1] locked {USERS} ({total_before} rows).")
    print(f"    {FLAG}: {tuple(flag) if flag else 'absent'}")
    for name, col in existing.items():
        print(f"    {name}: {tuple(col) if col else 'absent'}")

    if _flag_ok(flag) and all(existing.values()):
        print(f"\nNothing to do — {USERS}.{FLAG} is NOT NULL DEFAULT false and "
              f"{', '.join(NEW_COLUMNS)} exist (already migrated).")
        print(f"    {FLAG} counts: {_flag_counts(conn)}")
        return False

    # ── 2. is_deleted ────────────────────────────────────────────────────
    print(f"\n[2] {FLAG}")
    if flag is None:
        conn.execute(text(f"ALTER TABLE {USERS} ADD COLUMN {FLAG} BOOLEAN NOT NULL DEFAULT false"))
        print(f"    added {FLAG} BOOLEAN NOT NULL DEFAULT false")
        before = {"False": total_before} if total_before else {}
    else:
        if flag[0] != "boolean":
            _refuse(f"{USERS}.{FLAG} is {flag[0]!r}, not boolean")
        before = _flag_counts(conn)
        print(f"    counts before: {before}")
        if before.get("True"):
            # Not a refusal: nothing set these on purpose today, but the
            # owner should see them before committing.
            print(f"    NOTE: {before['True']} row(s) already have {FLAG} = true. They will be "
                  "treated as deleted by the new code.")
        nulls = before.get("None", 0)
        if nulls:
            conn.execute(text(f"UPDATE {USERS} SET {FLAG} = false WHERE {FLAG} IS NULL"))
            print(f"    backfilled {nulls} NULL -> false")
        if (flag[2] or "").lower() != "false":
            conn.execute(text(f"ALTER TABLE {USERS} ALTER COLUMN {FLAG} SET DEFAULT false"))
            print("    SET DEFAULT false")
        if flag[1] != "NO":
            conn.execute(text(f"ALTER TABLE {USERS} ALTER COLUMN {FLAG} SET NOT NULL"))
            print("    SET NOT NULL")
        if _flag_ok(flag):
            print("    already NOT NULL DEFAULT false")

    # ── 3. deleted_at / deleted_by ───────────────────────────────────────
    print("\n[3] new columns")
    for name, ddl in NEW_COLUMNS.items():
        if existing[name] is None:
            conn.execute(text(f"ALTER TABLE {USERS} ADD COLUMN {name} {ddl}"))
            print(f"    added {name} {ddl}")
        else:
            print(f"    {name} already present")

    # ── 4. verify ────────────────────────────────────────────────────────
    print("\n[4] verification")
    flag_after = _column(conn, FLAG)
    at, by = _column(conn, "deleted_at"), _column(conn, "deleted_by")
    after = _flag_counts(conn)
    expected_true = before.get("True", 0)
    checks = {
        f"{FLAG} is boolean NOT NULL DEFAULT false": _flag_ok(flag_after),
        "deleted_at is timestamptz, nullable": at is not None
        and at[0] == "timestamp with time zone"
        and at[1] == "YES",
        "deleted_by is varchar(320), nullable": by is not None
        and by[0] == "character varying"
        and by[3] == 320
        and by[1] == "YES",
        f"row count unchanged ({total_before})": _scalar(conn, f"SELECT count(*) FROM {USERS}") == total_before,
        f"no NULL {FLAG}": "None" not in after,
        f"no row newly flagged deleted ({expected_true} before)": after.get("True", 0) == expected_true,
        "no deleted_at / deleted_by values": _scalar(
            conn, f"SELECT count(*) FROM {USERS} WHERE deleted_at IS NOT NULL OR deleted_by IS NOT NULL"
        ) == 0
        or any(existing.values()),
    }
    for label, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {label}")
    print(f"    {FLAG} counts after: {after}")
    if not all(checks.values()):
        _refuse("verification failed")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Add the soft-delete columns to users.")
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
