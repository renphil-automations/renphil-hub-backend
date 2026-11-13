"""
One-shot migration: fold `hub_users` INTO `users` and detach `hub_users`, in
the database referenced by DATABASE_URL_V2 / PG_V2_* env vars.

Owner decisions (2026-10-09/10):
  - The login key is `users.work_email`, with `users.alias_email` as the
    fallback. There is NO separate login-email column.
  - `users.is_active` means "has signed in". HR rows nobody has signed in as
    stay false; every `hub_users` row (all of them are signed-in or
    admin-added people) brings its own flag across.
  - The 26 `hub_users` people with no HR row (test personas, gmail accounts,
    `automations@`, and three short-address logins — christen@ / robyn@ /
    phoebe@ — that are deliberately LEFT as separate rows for now) become
    login-only `users` rows.
  - The duplicate HR rows were renames the old `name`-keyed sync left
    behind. The row whose name matches Airtable today is kept; the stale
    ones go, along with 7 more rows that are no longer in Airtable at all
    and that no login points at. Four rows that are gone from Airtable but
    DO back a login (Pedro Augusto, Sarah Cone, Noël Bakhtian, Jim McBride)
    are KEPT.

THE IDs ARE THE WHOLE POINT. `role_assignments`, `resource_grants` and
`bookmarks` hold `hub_users.id` values, and the frontend caches them too. So
every HR row that matches a login is RENUMBERED to that login's
`hub_users.id`, rather than the referencing rows being rewritten. HR rows
with no login get fresh ids above the highest `hub_users.id`. The five
foreign keys are then repointed at `users(id)` with their delete rules
unchanged, and none of their values moves.

Steps, all in ONE transaction (Postgres DDL is transactional, so a failure
part-way leaves the database exactly as it was):

   1. LOCK users + hub_users, so no login can provision a row mid-migration.
   2. Schema prep on `users`: add id (identity) / is_active / created_at /
      updated_at if missing, restore `is_deleted DEFAULT false`, and drop the
      old primary key on `name`.
   3. Cleanup: delete the 11 stale HR rows (by exact name, which was the
      primary key until now and so is unique on prod), and fill in Katherine
      Pippert's missing work_email from Airtable.
   4. Match every hub_users row to at most one users row (work_email first,
      then alias_email). Refuses if any email matches two rows, or one row
      matches two logins.
   5. Renumber: matched rows take their hub id (+ is_active / created_at);
      unmatched HR rows get ids above max(hub_users.id), is_active = false.
   6. Insert the login-only rows with their hub ids.
   7. Restart the identity above the new max id; add PRIMARY KEY (id) and
      partial unique indexes on lower(work_email) / lower(alias_email).
   8. Repoint every FK that references hub_users(id) to users(id).
   9. `hub_users` is LEFT IN PLACE, untouched (not renamed, not dropped). After
      step 8 nothing references it: it is a frozen snapshot, kept for rollback
      and for any hub_users-era code still pointed at this database. Drop it
      (or rename it) once the new code is verified.
  10. Verify: every hub id resolves to a users row with the same email,
      nothing references hub_users, no duplicate login emails.

Re-running is safe: a database that already has `uq_users_work_email_lower`
(created only by step 7, in the same transaction) is reported as already
migrated and left alone. A database with no `hub_users` table and no such
index is refused as the wrong target.

THE BACKEND CODE MUST SHIP WITH THIS. Because `hub_users` is kept, code that
still uses `HubUserV2` will NOT fail after a commit; it keeps working against
the detached table. New sign-ins then land only in `hub_users` (invisible to
`users`), and their role/grant inserts fail the FKs, which now point at
`users`. Run --commit only together with the code change, on whichever
database that code points at.

The HR sync (not in this repo) must cope afterwards: `users.name` is no
longer unique (an `ON CONFLICT (name)` upsert would fail), and new HR rows
get their id from the identity default.

Dry run (DEFAULT) runs every step for real inside the transaction, prints
the verification, then ROLLS BACK, so it tests the real data with no writes.

Run from renphil-hub-backend/, with the venv python:

  Dry run:   PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_merge_hub_users_into_users
  Real run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_merge_hub_users_into_users --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db_v2.database import engine_v2

USERS = "users"
HUB_USERS = "hub_users"
# Created only by step 7, so its presence means this migration already ran.
MIGRATED_MARKER = "uq_users_work_email_lower"

# (exact name, expected work_email or None for "blank"). Names are copied
# byte-for-byte from the table, trailing newlines included — they are the
# Airtable values the old sync wrote.
STALE_ROWS: list[tuple[str, str | None]] = [
    # Duplicates: the same person under a name Airtable no longer has.
    ("Matthew Goodman", "matthew.goodman@renphil.org"),  # kept: "MattyG Goodman"
    ("Pedro Mendonça\n", "pedro.mendonca@renphil.org"),  # kept: 'Pedro "Mendy" Mendonça'
    ("Pedro (Mendy) Siqueira Mendonça", "pedro.mendonca@renphil.org"),
    ("Zachary Braunstein", "zachary.braunstein@renphil.org"),  # kept: "Zack Braunstein"
    # Not in Airtable at all, and no login.
    (" ", None),
    ("Matthew Cocca", None),
    ("Rao Xiaojia", None),  # renamed in Airtable to "Rao Xiaojia Rao"
    ("Pedro Augusto Pedro Augusto\n", None),
    ("Satwik Dutta", "satwik.dutta@renphil.org"),
    ("Jesse Alma", None),
    ("Michelle Graham", None),
]

# HR rows whose work_email is blank here but set in Airtable.
WORK_EMAIL_FIXES: list[tuple[str, str]] = [
    ("Katherine Pippert", "katherine.pippert@renphil.org"),
]

_EMAIL = "lower(trim({col}))"
_HAS = "coalesce(trim({col}), '') <> ''"


def _scalar(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _rows(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).fetchall()


def _refuse(msg: str) -> None:
    raise RuntimeError(f"REFUSING: {msg}")


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    return bool(
        _scalar(
            conn,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c)",
            t=table,
            c=column,
        )
    )


def _table_exists(conn: Connection, table: str) -> bool:
    return _scalar(conn, "SELECT to_regclass(:t) IS NOT NULL", t=f"public.{table}")


def migrate(conn: Connection) -> None:
    run = lambda sql, **p: conn.execute(text(sql), p)  # noqa: E731

    # ── 1. lock ──────────────────────────────────────────────────────────
    run(f"LOCK TABLE {USERS}, {HUB_USERS} IN ACCESS EXCLUSIVE MODE")
    users_before = _scalar(conn, f"SELECT count(*) FROM {USERS}")
    hub_count = _scalar(conn, f"SELECT count(*) FROM {HUB_USERS}")
    max_hub_id = _scalar(conn, f"SELECT coalesce(max(id), 0) FROM {HUB_USERS}")
    print(f"\n[1] locked. users={users_before}, hub_users={hub_count}, max hub id={max_hub_id}")

    # ── 2. schema prep ───────────────────────────────────────────────────
    print("\n[2] schema prep")
    if not _column_exists(conn, USERS, "id"):
        run(f"ALTER TABLE {USERS} ADD COLUMN id INTEGER GENERATED BY DEFAULT AS IDENTITY")
        print("    added users.id (identity)")
    for col, ddl in (
        ("is_active", "BOOLEAN NOT NULL DEFAULT false"),
        ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
        ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
    ):
        if not _column_exists(conn, USERS, col):
            run(f"ALTER TABLE {USERS} ADD COLUMN {col} {ddl}")
            print(f"    added users.{col}")
    if _column_exists(conn, USERS, "is_deleted"):
        run(f"ALTER TABLE {USERS} ALTER COLUMN is_deleted SET DEFAULT false")
        n = run(f"UPDATE {USERS} SET is_deleted = false WHERE is_deleted IS NULL").rowcount
        print(f"    is_deleted DEFAULT false restored; {n} NULL value(s) set to false")
    for (pk_name,) in _rows(
        conn,
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'p'",
        t=USERS,
    ):
        run(f'ALTER TABLE {USERS} DROP CONSTRAINT "{pk_name}"')
        print(f"    dropped old primary key {pk_name}")

    if _scalar(conn, f"SELECT count(*) FROM {USERS} WHERE id IS NULL"):
        _refuse("users.id has NULLs")
    if _scalar(conn, f"SELECT count(*) FROM (SELECT id FROM {USERS} GROUP BY id HAVING count(*) > 1) x"):
        _refuse("users.id has duplicates before renumbering")

    # ── 3. cleanup ───────────────────────────────────────────────────────
    print("\n[3] cleanup")
    deleted = 0
    for name, expected in STALE_ROWS:
        found = _rows(conn, f"SELECT id, work_email FROM {USERS} WHERE name = :n", n=name)
        if not found:
            print(f"    skip {name!r}: not present")
            continue
        if len(found) > 1:
            _refuse(f"{len(found)} rows named {name!r}")
        work = (found[0][1] or "").strip().lower() or None
        if work != expected:
            _refuse(f"{name!r} has work_email {work!r}, expected {expected!r}")
        run(f"DELETE FROM {USERS} WHERE name = :n", n=name)
        deleted += 1
        print(f"    deleted {name!r}")
    for name, email in WORK_EMAIL_FIXES:
        if _scalar(
            conn,
            f"SELECT count(*) FROM {USERS} WHERE {_EMAIL.format(col='work_email')} = :e "
            f"OR {_EMAIL.format(col='alias_email')} = :e",
            e=email,
        ):
            print(f"    skip work_email fix for {name!r}: {email} already on a row")
            continue
        n = run(
            f"UPDATE {USERS} SET work_email = :e WHERE name = :n AND NOT {_HAS.format(col='work_email')}",
            e=email,
            n=name,
        ).rowcount
        print(f"    work_email fix {name!r} -> {email}: {n} row(s)")

    # ── 4. match logins to HR rows ───────────────────────────────────────
    print("\n[4] matching hub_users -> users")
    run(
        f"""CREATE TEMP TABLE _hub_map ON COMMIT DROP AS
            SELECT h.id AS hub_id, u.id AS old_id, 'work' AS via
              FROM {HUB_USERS} h JOIN {USERS} u ON {_EMAIL.format(col='u.work_email')} = h.email
            UNION ALL
            SELECT h.id, u.id, 'alias'
              FROM {HUB_USERS} h JOIN {USERS} u ON {_EMAIL.format(col='u.alias_email')} = h.email
             WHERE NOT EXISTS (SELECT 1 FROM {USERS} w
                                WHERE {_EMAIL.format(col='w.work_email')} = h.email)"""
    )
    multi = _rows(
        conn,
        f"SELECT h.email, array_agg(m.old_id) FROM _hub_map m JOIN {HUB_USERS} h ON h.id = m.hub_id "
        "GROUP BY h.email HAVING count(*) > 1",
    )
    if multi:
        _refuse(f"login emails matching several HR rows: {multi}")
    shared = _rows(conn, "SELECT old_id, array_agg(hub_id) FROM _hub_map GROUP BY old_id HAVING count(*) > 1")
    if shared:
        _refuse(f"HR rows matching several logins: {shared}")
    by_via = dict(_rows(conn, "SELECT via, count(*) FROM _hub_map GROUP BY via"))
    matched = sum(by_via.values())
    print(f"    matched {matched} (work_email {by_via.get('work', 0)}, alias_email {by_via.get('alias', 0)}); "
          f"login-only {hub_count - matched}")

    # ── 5. renumber ──────────────────────────────────────────────────────
    print("\n[5] renumbering")
    run(f"UPDATE {USERS} SET id = -id")  # park every id out of the way first
    n = run(
        f"""UPDATE {USERS} u
               SET id = h.id,
                   is_active = h.is_active,
                   created_at = h.created_at,
                   updated_at = greatest(u.updated_at, coalesce(h.updated_at, h.created_at))
              FROM _hub_map m JOIN {HUB_USERS} h ON h.id = m.hub_id
             WHERE u.id = -m.old_id"""
    ).rowcount
    print(f"    {n} matched row(s) took their hub id")
    n = run(
        f"""UPDATE {USERS} u
               SET id = :base + r.rn, is_active = false
              FROM (SELECT id, row_number() OVER (ORDER BY name, id) AS rn
                      FROM {USERS} WHERE id < 0) r
             WHERE u.id = r.id""",
        base=max_hub_id,
    ).rowcount
    print(f"    {n} HR row(s) with no login got ids above {max_hub_id}, is_active=false")

    # ── 6. login-only rows ───────────────────────────────────────────────
    n = run(
        f"""INSERT INTO {USERS} (id, name, work_email, is_active, created_at, updated_at)
            SELECT h.id, coalesce(nullif(trim(h.name), ''), h.email), h.email,
                   h.is_active, h.created_at, coalesce(h.updated_at, h.created_at)
              FROM {HUB_USERS} h
             WHERE NOT EXISTS (SELECT 1 FROM _hub_map m WHERE m.hub_id = h.id)"""
    ).rowcount
    print(f"\n[6] inserted {n} login-only row(s)")

    # ── 7. constraints ───────────────────────────────────────────────────
    print("\n[7] constraints")
    cross = _scalar(
        conn,
        f"SELECT count(*) FROM {USERS} a JOIN {USERS} b ON a.id <> b.id "
        f"AND {_EMAIL.format(col='a.work_email')} = {_EMAIL.format(col='b.alias_email')}",
    )
    if cross:
        _refuse(f"{cross} email(s) are one row's work_email and another row's alias_email")
    next_id = _scalar(conn, f"SELECT max(id) + 1 FROM {USERS}")
    run(f"ALTER TABLE {USERS} ALTER COLUMN id RESTART WITH {int(next_id)}")
    run(f"ALTER TABLE {USERS} ADD CONSTRAINT users_pkey PRIMARY KEY (id)")
    run(
        f"CREATE UNIQUE INDEX {MIGRATED_MARKER} ON {USERS} (lower(work_email)) "
        "WHERE work_email IS NOT NULL AND work_email <> ''"
    )
    run(
        f"CREATE UNIQUE INDEX uq_users_alias_email_lower ON {USERS} (lower(alias_email)) "
        "WHERE alias_email IS NOT NULL AND alias_email <> ''"
    )
    print(f"    PRIMARY KEY (id); unique lower(work_email) / lower(alias_email); identity restarts at {next_id}")

    # ── 8. repoint FKs ───────────────────────────────────────────────────
    print("\n[8] repointing foreign keys")
    fks = _rows(
        conn,
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE contype = 'f' AND confrelid = CAST(:t AS regclass)",
        t=HUB_USERS,
    )
    for table, name, definition in fks:
        new_def = definition.replace(f"REFERENCES {HUB_USERS}(id)", f"REFERENCES {USERS}(id)")
        if new_def == definition:
            _refuse(f"could not rewrite FK {table}.{name}: {definition}")
        run(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        run(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" {new_def}')
        print(f"    {table}.{name}: {new_def}")

    # ── 9. hub_users is left in place, now unreferenced ──────────────────
    print(f"\n[9] {HUB_USERS} left in place ({hub_count} rows), no longer referenced")

    # ── 10. verify ───────────────────────────────────────────────────────
    print("\n[10] verification")
    users_after = _scalar(conn, f"SELECT count(*) FROM {USERS}")
    expected_after = users_before - deleted + (hub_count - matched)
    checks = {
        f"users rows = {users_before} - {deleted} deleted + {hub_count - matched} inserted":
            users_after == expected_after,
        "every old hub id is a users row with the same login email": not _scalar(
            conn,
            f"SELECT count(*) FROM {HUB_USERS} h WHERE NOT EXISTS (SELECT 1 FROM {USERS} u "
            f"WHERE u.id = h.id AND h.email IN ({_EMAIL.format(col='u.work_email')}, "
            f"{_EMAIL.format(col='u.alias_email')}))",
        ),
        f"nothing references {HUB_USERS}": not _scalar(
            conn,
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND confrelid = CAST(:t AS regclass)",
            t=HUB_USERS,
        ),
        f"{len(fks)} FK(s) now reference users": _scalar(
            conn,
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND confrelid = CAST(:t AS regclass)",
            t=USERS,
        ) == len(fks),
        "no duplicate login email": not _scalar(
            conn,
            f"SELECT count(*) FROM (SELECT lower(work_email) FROM {USERS} WHERE {_HAS.format(col='work_email')} "
            "GROUP BY 1 HAVING count(*) > 1) x",
        ),
    }
    for label, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {label}")
    print(f"    users now {users_after}; is_active: "
          f"{dict(_rows(conn, f'SELECT is_active, count(*) FROM {USERS} GROUP BY 1'))}")
    if not all(checks.values()):
        _refuse("verification failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fold hub_users into users and detach hub_users.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Omit for a dry run that executes everything and rolls back.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("COMMIT MODE — writes will be made" if args.commit else "DRY RUN — runs every step, then ROLLS BACK")
    print("=" * 70)
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")

    if engine_v2.dialect.name != "postgresql":
        print(f"  REFUSING: dialect is {engine_v2.dialect.name!r}, not 'postgresql'.")
        raise SystemExit(1)

    with engine_v2.connect() as conn:
        if _table_exists(conn, MIGRATED_MARKER):
            print(f"\nNothing to do — index {MIGRATED_MARKER} exists, so this database is already migrated.")
            return
        if not _table_exists(conn, HUB_USERS):
            print(f"  REFUSING: no {HUB_USERS} table and no {MIGRATED_MARKER} index. "
                  "This database is neither pre- nor post-merge; check DATABASE_URL_V2.")
            raise SystemExit(1)
        conn.rollback()  # end the probes' autobegun transaction

        trans = conn.begin()
        try:
            migrate(conn)
        except Exception as exc:
            trans.rollback()
            print(f"\n{exc}\nRolled back — nothing was written.")
            raise SystemExit(1) from exc
        if args.commit:
            trans.commit()
            print("\n" + "=" * 70 + "\nChanges committed.\n" + "=" * 70)
        else:
            trans.rollback()
            print("\n" + "=" * 70 + "\nDRY RUN — rolled back, nothing was written. Re-run with --commit to apply.\n" + "=" * 70)


if __name__ == "__main__":
    main()
