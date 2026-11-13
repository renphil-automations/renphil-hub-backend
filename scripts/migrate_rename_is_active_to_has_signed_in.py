"""
One-shot migration: rename `users.is_active` -> `users.has_signed_in`, in the
database referenced by DATABASE_URL_V2 / PG_V2_* env vars.

Why (owner decision, 2026-10-18): since the users/hub_users merge the flag
means "has signed in" (HR rows and admin-added people start false; the first
sign-in flips it). It never meant "deactivated", so the name misleads.

Touches ONLY `public.users`. Dev's `hub_users` table (still used by the team's
hub_users-era code) has its own `is_active` and is deliberately left alone.

Steps, all in ONE transaction:

   1. LOCK users, count true/false before.
   2. Read information_schema for public.users:
        is_active present, has_signed_in absent -> RENAME COLUMN
        has_signed_in present, is_active absent -> already renamed, exit 0
        both or neither                         -> refuse
   3. Verify: has_signed_in is boolean, NOT NULL, DEFAULT false; is_active is
      gone; the true/false counts are unchanged.

`ALTER ... RENAME COLUMN` is metadata-only (brief exclusive lock, no rewrite).

THE BACKEND CODE MUST SHIP WITH THIS. Old code breaks on the new column name,
new code on the old one. Rollback:
  ALTER TABLE users RENAME COLUMN has_signed_in TO is_active;  + revert the code.

Dry run (DEFAULT) runs every step for real inside the transaction, prints the
verification, then ROLLS BACK, so it tests the real data with no writes.

Run from renphil-hub-backend/, with the venv python:

  Dry run:   PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_rename_is_active_to_has_signed_in
  Real run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_rename_is_active_to_has_signed_in --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db_v2.database import engine_v2

USERS = "users"
OLD = "is_active"
NEW = "has_signed_in"


def _scalar(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _rows(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).fetchall()


def _refuse(msg: str) -> None:
    raise RuntimeError(f"REFUSING: {msg}")


def _column(conn: Connection, column: str):
    """(data_type, is_nullable, column_default) for public.users.<column>, or None."""
    return conn.execute(
        text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": USERS, "c": column},
    ).first()


def _counts(conn: Connection, column: str) -> dict:
    return dict(_rows(conn, f"SELECT {column}, count(*) FROM {USERS} GROUP BY 1"))


def migrate(conn: Connection) -> bool:
    """Returns False when there was nothing to do (already renamed)."""
    # ── 1. lock ──────────────────────────────────────────────────────────
    conn.execute(text(f"LOCK TABLE {USERS} IN ACCESS EXCLUSIVE MODE"))
    old_col, new_col = _column(conn, OLD), _column(conn, NEW)
    print(f"\n[1] locked {USERS}. {OLD}: {'present' if old_col else 'absent'}, "
          f"{NEW}: {'present' if new_col else 'absent'}")

    # ── 2. rename ────────────────────────────────────────────────────────
    if new_col and not old_col:
        print(f"\nNothing to do — {USERS}.{NEW} already exists and {OLD} is gone (already renamed).")
        return False
    if old_col and new_col:
        _refuse(f"{USERS} has BOTH {OLD} and {NEW}")
    if not old_col:
        _refuse(f"{USERS} has NEITHER {OLD} nor {NEW}")

    before = _counts(conn, OLD)
    print(f"    {OLD} before: {before}")
    conn.execute(text(f"ALTER TABLE {USERS} RENAME COLUMN {OLD} TO {NEW}"))
    print(f"\n[2] renamed {USERS}.{OLD} -> {NEW}")

    # ── 3. verify ────────────────────────────────────────────────────────
    print("\n[3] verification")
    col = _column(conn, NEW)
    after = _counts(conn, NEW)
    checks = {
        f"{NEW} exists": col is not None,
        f"{OLD} is gone": _column(conn, OLD) is None,
        f"{NEW} is boolean": col is not None and col[0] == "boolean",
        f"{NEW} is NOT NULL": col is not None and col[1] == "NO",
        f"{NEW} DEFAULT false": col is not None and (col[2] or "").lower() == "false",
        f"true/false counts unchanged ({before})": after == before,
    }
    for label, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {label}")
    print(f"    {NEW} after: {after}; column: {tuple(col) if col else None}")
    if not all(checks.values()):
        _refuse("verification failed")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Rename users.is_active to users.has_signed_in.")
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
