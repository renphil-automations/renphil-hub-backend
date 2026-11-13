"""
One-shot migration: thread versioning — add `threads.version`, create
`thread_revisions`, and backfill every approved thread's v1, in the database
referenced by DATABASE_URL_V2 / PG_V2_* env vars — see
plan_thread_edit_versioning_2026-09-22.md §2 and its 2026-09-23 amendments.

Six steps, in this order (the column and the table before the indexes that
read them; the backfill last, once the unique index backs it):

  1. ALTER `threads` ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1
  2. CREATE TABLE IF NOT EXISTS thread_revisions (..., origin, and three
       CHECKs: ck_thread_revisions_status, ck_thread_revisions_origin,
       ck_thread_revisions_origin_shape)
  3. CREATE UNIQUE INDEX uq_thread_revisions_thread_version
       ON thread_revisions (thread_id, version)
  4. CREATE INDEX ix_thread_revisions_pending_submitted_id
       ON thread_revisions (submitted_at, id) WHERE status = 'pending'
  5. CREATE INDEX ix_thread_revisions_reviewed_at_id
       ON thread_revisions (reviewed_at DESC, id DESC) WHERE reviewed_at IS NOT NULL
  6. INSERT the v1 (`origin = 'original'`, `status = 'approved'`) of every
       APPROVED thread that has no revision yet — its CURRENT title/content/
       mentions, submitted by its author at `created_at`, published by its
       own reviewer at `reviewed_at` if an editor approved it, else by its
       author at `created_at` (an editor's auto-approved post). Pending and
       rejected threads get nothing: a thread's history starts when it is
       published (amendment A3), and `decide_thread` writes the v1 of any
       thread approved later.

THE BACKFILLED v1 IS TODAY'S TEXT, NOT NECESSARILY THE ORIGINAL POST. Before
versioning, an approved thread's edits wrote over it in place, and the
earlier text is gone. For a thread with `edited_at` set, v1 records the
content as of this migration. That is the best available history, and it is
exactly what `threads` shows, so the invariant "the revision at
`threads.version` is the live content" holds for every row from here on.

WHY A MIGRATION AND NOT JUST A MODEL CHANGE. `threads` already exists in the
live Neon database, and `create_all(checkfirst=True)` only ever creates whole
tables — it will not add `version` to a table that is already there. Without
this script the model and the live schema silently disagree, and the first
thread read once the new code is live fails with UndefinedColumn.

WHAT THIS ENABLES. Plan §0.2: a non-editor author's edit of an APPROVED
thread is stored as a pending `thread_revisions` snapshot instead of going
live; the thread stays `approved` and readable until an editor decides the
revision. `threads.version` is the version of the content currently live.

`threads.version` NEEDS NO BACKFILL: `DEFAULT 1` is exactly right for every
existing row — each one's live content IS the v1 step 6 writes. Postgres
fills the default in as part of the ADD COLUMN.

NO "AT MOST ONE PENDING" INDEX, DELIBERATELY. The design doc recommended
`UNIQUE(thread_id) WHERE status = 'pending'`; the owner rejected it (plan
§0.2 decision 1) — many pending revisions per thread are legal, and each is
decided individually. The one unique index here is `(thread_id, version)`,
the hard backstop for the version-allocation race.

THE CHECKS ARE ITS OWN. `thread_revisions.status` has FOUR values, not
`ck_threads_status`'s three: an edit's lifecycle and the thread's are
separate machines (plan §1.1). `origin` has three, and
`ck_thread_revisions_origin_shape` pins the two rules the service relies on:
only an 'author' row is ever anything but 'approved', and version 1 is
exactly the 'original' row.

THE PARTIAL INDEXES ARE NOT OPTIONAL, and the WHERE clause is the whole
point — same reasoning `scripts/migrate_thread_moderation_columns.py` gives
for its own. They stay bounded by how many revisions are pending, or have
been decided, however large the table grows. They must match the model's
`postgresql_where` exactly (`app/db_v2/models/thread.py`, `ThreadRevisionV2`).

THE BACKEND MUST NOT BE DEPLOYED BEFORE THIS RUNS. `threads.version` is read
on the very first thread read once phase A code is live (plan §2.2).

SAFE TO RUN MULTIPLE TIMES. Each DDL step probes the catalog first and only
acts if missing, and each statement is itself `IF NOT EXISTS`; the backfill
only inserts for approved threads with NO revision row (`NOT EXISTS`), so a
second run inserts nothing and a partially-applied run converges rather
than erroring. All statements run
inside one transaction — Postgres is transactional for DDL — so a failure
part-way leaves the database exactly as it was.

POSTGRES ONLY, matching every other script in this directory. The test
suite builds its schema from the models via `create_all` on in-memory
SQLite and never runs this script; the script refuses on any other dialect
rather than emitting SQL that would fail halfway.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_revisions

  Real run (writes, against the live Neon renphil-hub-db — DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_revisions --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

THREADS_TABLE = "threads"
REVISIONS_TABLE = "thread_revisions"

VERSION_COLUMN = "version"
ADD_VERSION_SQL = (
    f'ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS "{VERSION_COLUMN}" '
    "INTEGER NOT NULL DEFAULT 1"
)

CREATE_REVISIONS_SQL = """CREATE TABLE IF NOT EXISTS thread_revisions (
        id                 SERIAL PRIMARY KEY,
        thread_id          INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        version            INTEGER NOT NULL,
        title              VARCHAR(200) NOT NULL,
        content            TEXT NOT NULL,
        mentions           JSONB NOT NULL DEFAULT '[]'::jsonb,
        status             VARCHAR(16) NOT NULL,
        origin             VARCHAR(16) NOT NULL,
        submitted_by_email VARCHAR(320) NOT NULL,
        submitted_by_name  VARCHAR(255),
        submitted_at       TIMESTAMPTZ NOT NULL,
        reviewed_by_email  VARCHAR(320),
        reviewed_by_name   VARCHAR(255),
        reviewed_at        TIMESTAMPTZ,
        CONSTRAINT ck_thread_revisions_status
            CHECK (status IN ('pending', 'approved', 'rejected', 'overwritten')),
        CONSTRAINT ck_thread_revisions_origin
            CHECK (origin IN ('original', 'author', 'editor')),
        CONSTRAINT ck_thread_revisions_origin_shape
            CHECK ((origin = 'author' OR status = 'approved')
                   AND ((origin = 'original') = (version = 1)))
    )"""

# Upgrade path for a `thread_revisions` created by this script's FIRST cut
# (phase A before the 2026-09-23 amendments — applied to live Neon that
# day): no `origin` column and only the status CHECK. `DEFAULT 'author'` so
# the ADD works on a non-empty table — every row the first cut could ever
# have written was an author's staged edit (it wrote no v1s, no editor
# rows) — then the default is dropped so the service must always say.
ORIGIN_COLUMN = "origin"
ADD_ORIGIN_SQL = (
    f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS {ORIGIN_COLUMN} "
    "VARCHAR(16) NOT NULL DEFAULT 'author'"
)
DROP_ORIGIN_DEFAULT_SQL = f"ALTER TABLE {REVISIONS_TABLE} ALTER COLUMN {ORIGIN_COLUMN} DROP DEFAULT"
UPGRADE_CHECKS: dict[str, str] = {
    "ck_thread_revisions_origin": (
        f"ALTER TABLE {REVISIONS_TABLE} ADD CONSTRAINT ck_thread_revisions_origin "
        "CHECK (origin IN ('original', 'author', 'editor'))"
    ),
    "ck_thread_revisions_origin_shape": (
        f"ALTER TABLE {REVISIONS_TABLE} ADD CONSTRAINT ck_thread_revisions_origin_shape "
        "CHECK ((origin = 'author' OR status = 'approved') "
        "AND ((origin = 'original') = (version = 1)))"
    ),
}
CHECK_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = :name AND contype = 'c'
    )
    """
)

# Step 6 — v1 of every approved thread that has no revision yet. Mirrors
# `thread_service._ensure_original_revision` exactly (publisher = the
# thread's own reviewer if an editor approved it, else its author).
BACKFILL_SQL = """INSERT INTO thread_revisions (
        thread_id, version, title, content, mentions, status, origin,
        submitted_by_email, submitted_by_name, submitted_at,
        reviewed_by_email, reviewed_by_name, reviewed_at
    )
    SELECT t.id, 1, t.title, t.content, t.mentions, 'approved', 'original',
           t.author_email, t.author_name, t.created_at,
           COALESCE(t.reviewed_by_email, t.author_email),
           CASE WHEN t.reviewed_by_email IS NOT NULL THEN t.reviewed_by_name ELSE t.author_name END,
           COALESCE(t.reviewed_at, t.created_at)
      FROM threads t
     WHERE t.status = 'approved'
       AND NOT EXISTS (SELECT 1 FROM thread_revisions r WHERE r.thread_id = t.id)"""

# What the backfill WOULD insert — runnable before the table exists (every
# approved thread) and after (only those with no revision yet).
BACKFILL_COUNT_ALL_SQL = text("SELECT COUNT(*) FROM threads WHERE status = 'approved'")
BACKFILL_COUNT_MISSING_SQL = text(
    """
    SELECT COUNT(*) FROM threads t
     WHERE t.status = 'approved'
       AND NOT EXISTS (SELECT 1 FROM thread_revisions r WHERE r.thread_id = t.id)
    """
)
EDITED_APPROVED_SQL = text(
    "SELECT COUNT(*) FROM threads WHERE status = 'approved' AND edited_at IS NOT NULL"
)

INDEXES: dict[str, str] = {
    "uq_thread_revisions_thread_version": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_revisions_thread_version "
        "ON thread_revisions (thread_id, version)"
    ),
    "ix_thread_revisions_pending_submitted_id": (
        "CREATE INDEX IF NOT EXISTS ix_thread_revisions_pending_submitted_id "
        "ON thread_revisions (submitted_at, id) WHERE status = 'pending'"
    ),
    "ix_thread_revisions_reviewed_at_id": (
        "CREATE INDEX IF NOT EXISTS ix_thread_revisions_reviewed_at_id "
        "ON thread_revisions (reviewed_at DESC, id DESC) WHERE reviewed_at IS NOT NULL"
    ),
}

COLUMN_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = :table AND column_name = :column
    )
    """
)

TABLE_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = :table
    )
    """
)

INDEX_EXISTS_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :index)"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add threads.version and create thread_revisions with its CHECK, "
        "unique index and two partial indexes (thread edit versioning)."
    )
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
        if not engine_v2.dialect.has_table(conn, THREADS_TABLE):
            print(f"  REFUSING: table {THREADS_TABLE!r} does not exist. Is this the right database?")
            print("  Run scripts/create_thread_tables.py first.")
            raise SystemExit(1)

        plan: list[str] = []

        print("\nPlan:")
        version_exists = bool(
            conn.execute(
                COLUMN_EXISTS_SQL, {"table": THREADS_TABLE, "column": VERSION_COLUMN}
            ).scalar()
        )
        print(
            f"  - {THREADS_TABLE}.{VERSION_COLUMN} column: "
            f"{'already exists' if version_exists else 'will be added'}"
        )
        if not version_exists:
            plan.append(ADD_VERSION_SQL)

        table_exists = bool(conn.execute(TABLE_EXISTS_SQL, {"table": REVISIONS_TABLE}).scalar())
        print(
            f"  - {REVISIONS_TABLE} table (with ck_thread_revisions_status): "
            f"{'already exists' if table_exists else 'will be created'}"
        )
        if not table_exists:
            plan.append(CREATE_REVISIONS_SQL)
        else:
            # The table predates the amendments? Bring it up to shape.
            origin_exists = bool(
                conn.execute(
                    COLUMN_EXISTS_SQL, {"table": REVISIONS_TABLE, "column": ORIGIN_COLUMN}
                ).scalar()
            )
            print(
                f"  - {REVISIONS_TABLE}.{ORIGIN_COLUMN} column: "
                f"{'already exists' if origin_exists else 'will be added (upgrade from the first cut)'}"
            )
            if not origin_exists:
                plan.append(ADD_ORIGIN_SQL)
                plan.append(DROP_ORIGIN_DEFAULT_SQL)
            for name, ddl in UPGRADE_CHECKS.items():
                exists = bool(conn.execute(CHECK_EXISTS_SQL, {"name": name}).scalar())
                print(f"  - {name}: {'already exists' if exists else 'will be added'}")
                if not exists:
                    plan.append(ddl)

        for index, ddl in INDEXES.items():
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  - {index}: {'already exists' if exists else 'will be created'}")
            if not exists:
                plan.append(ddl)

        # Before the table exists, every approved thread needs its v1.
        to_backfill = int(
            conn.execute(BACKFILL_COUNT_MISSING_SQL if table_exists else BACKFILL_COUNT_ALL_SQL).scalar()
        )
        edited = int(conn.execute(EDITED_APPROVED_SQL).scalar())
        print(
            f"  - v1 backfill: {to_backfill} approved thread(s) without a revision "
            f"{'will get their v1' if to_backfill else '— nothing to insert'}"
        )
        if edited:
            print(
                f"    note: {edited} approved thread(s) have edited_at set — their v1 will be "
                "today's text; the pre-edit original was overwritten before versioning existed"
            )
        if to_backfill:
            plan.append(BACKFILL_SQL)

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

    # One transaction for all of it: a failure part-way leaves the database
    # exactly as it was, not half-migrated.
    with engine_v2.begin() as conn:
        for stmt in plan:
            conn.execute(text(stmt))
            print(f"    ran: {stmt}")

    with engine_v2.connect() as conn:
        print("\nVerified:")
        present = bool(
            conn.execute(
                COLUMN_EXISTS_SQL, {"table": THREADS_TABLE, "column": VERSION_COLUMN}
            ).scalar()
        )
        print(f"  {THREADS_TABLE}.{VERSION_COLUMN} present = {present}")
        present = bool(conn.execute(TABLE_EXISTS_SQL, {"table": REVISIONS_TABLE}).scalar())
        print(f"  {REVISIONS_TABLE} present = {present}")
        present = bool(
            conn.execute(
                COLUMN_EXISTS_SQL, {"table": REVISIONS_TABLE, "column": ORIGIN_COLUMN}
            ).scalar()
        )
        print(f"  {REVISIONS_TABLE}.{ORIGIN_COLUMN} present = {present}")
        for name in ("ck_thread_revisions_status", *UPGRADE_CHECKS):
            present = bool(conn.execute(CHECK_EXISTS_SQL, {"name": name}).scalar())
            print(f"  {name} present = {present}")
        for index in INDEXES:
            present = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  {index} present = {present}")
        missing = int(conn.execute(BACKFILL_COUNT_MISSING_SQL).scalar())
        print(f"  approved threads still without a v1 = {missing}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
