"""
One-shot migration: thread rejection reasons, resubmission and the Close
model — plan_thread_reject_resubmit_2026-11-07.md §2, as amended by §12 —
in the database referenced by DATABASE_URL_V2 / PG_V2_* env vars.

Steps, in this order (columns before the CHECKs and indexes that read them,
the new table before its indexes, the backfills last):

  threads (§2.1, §12.3, §12.8 G1)
    1. ADD COLUMN submitted_at TIMESTAMPTZ; backfill = created_at; SET NOT NULL;
       SET DEFAULT now() (G1 — rollback safety: the pre-feature create path
       INSERTs without it)
    2. ADD COLUMN resubmission_count INTEGER NOT NULL DEFAULT 0
    3. ADD COLUMN last_decided_at TIMESTAMPTZ; backfill = reviewed_at
    4. ADD COLUMN closed_at TIMESTAMPTZ, closed_kind VARCHAR(16)
  thread_revisions (§2.3, §12.3)
    5. ADD COLUMN rejection_reason TEXT
    6. ADD COLUMN resubmission_of_id INTEGER REFERENCES thread_revisions(id) ON DELETE SET NULL
    7. ADD COLUMN resubmission_number INTEGER NOT NULL DEFAULT 0
    8. ADD COLUMN closed_at TIMESTAMPTZ, closed_kind VARCHAR(16), closed_by_version INTEGER
  legacy soft-delete columns (only on a DB the PRE-§12 version of this
  script was committed to — Dev, 2026-11-07)
    9. copy threads.author_deleted_at / thread_revisions.author_dismissed_at
       into closed_at with closed_kind = 'author' (the owner's R1: a deleted
       item is a closed one), then DROP CONSTRAINT IF EXISTS
       ck_threads_author_deleted_rejected / ck_thread_revisions_dismissed_rejected
       and DROP COLUMN IF EXISTS author_deleted_at / author_dismissed_at
  CHECKs
   10. ck_thread_revisions_reason_rejected, ck_threads_closed_shape,
       ck_thread_revisions_closed_shape
  thread_submissions (§2.2)
   11. CREATE TABLE IF NOT EXISTS thread_submissions (+ its two CHECKs)
  indexes
   12. CREATE ix_threads_pending_submitted_id, ix_threads_last_decided_id,
       uq_thread_revisions_resubmission_of, uq_thread_submissions_thread_round,
       ix_thread_submissions_reviewed_at_id
   13. DROP INDEX IF EXISTS ix_threads_pending_created_id, ix_threads_reviewed_at_id
  backfills (§2.4, §12.3)
   14. one round-0 `thread_submissions` row for every DECIDED thread
       (`reviewed_at IS NOT NULL`) that has no round yet: status = the
       thread's status, reviewer = its reviewed_*, submitted_at = created_at,
       reason = NULL; snapshot = the thread's v1 revision for an approved
       thread (its live text may have been edited since), else the thread's
       own columns (a rejected thread could never be edited).
   15. superseded: every OPEN declined edit (rejected, origin 'author', not
       closed, not reworked) whose version is below the thread's live
       version is closed as 'superseded' — otherwise it would be
       resubmittable and could silently revert newer live text (§12.2 G2).
       closed_by_version = the lowest APPROVED version above it (fallback:
       the live version), closed_at = that revision's reviewed_at (fallback:
       the migration instant). No bell for backfilled rows.

Existing rejected revisions keep `rejection_reason = NULL`; they show "No
reason was recorded" (owner decision 12). `resubmission_count` /
`resubmission_number` default to 0, which is right for every existing row.

WHY A MIGRATION. `create_all(checkfirst=True)` only creates whole tables —
it will not add a column to `threads` or `thread_revisions`. Without this
script the first pending-queue request after deploy fails with
UndefinedColumn (`threads.submitted_at`), and the first decision fails on
the missing `thread_submissions`. MIGRATE EACH DB BEFORE ITS BACKEND RUNS
THIS CODE.

THE PARTIAL INDEXES must match the model's `postgresql_where` exactly
(`app/db_v2/models/thread.py`).

SAFE TO RUN MULTIPLE TIMES. Every column add is `ADD COLUMN IF NOT EXISTS`,
probed first; every CHECK is guarded by a `pg_constraint` probe; the table
and indexes are `IF NOT EXISTS`, the drops `IF EXISTS`. The backfills only
touch rows still missing their value: `submitted_at IS NULL`,
`last_decided_at IS NULL AND reviewed_at IS NOT NULL` (a thread resubmitted
after deploy has `reviewed_at` NULL, so a re-run never moves its
`last_decided_at` back), and the round insert is `NOT EXISTS` any round for
that thread (a thread decided after deploy already has one). The legacy
copy runs only while the old columns exist (they are dropped in the same
transaction); the superseded backfill only touches OPEN rows, and after
deploy the service closes every superseded edit itself (§12.4), so a re-run
finds none. `SET NOT NULL` / `SET DEFAULT` on an already-converged column
is a no-op. Everything runs in ONE
transaction (Postgres DDL is transactional), so a failure part-way leaves
the database exactly as it was.

POSTGRES ONLY; refuses on any other dialect. The test suite builds its
schema from the models on SQLite and never runs this script.

Run from renphil-hub-backend/, with the venv python:

  Dry run (default, no writes):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_resubmission

  Real run (writes, against DATABASE_URL_V2):
    PYTHONPATH=. ./.venv/Scripts/python.exe -m scripts.migrate_thread_resubmission --commit
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db_v2.database import engine_v2

THREADS_TABLE = "threads"
REVISIONS_TABLE = "thread_revisions"
SUBMISSIONS_TABLE = "thread_submissions"

# (table, column, ADD COLUMN DDL, follow-up statements run only when the
# column was added). `submitted_at` is added nullable, backfilled, then made
# NOT NULL — the backfill and SET NOT NULL are ALSO planned whenever any row
# still has NULL (a partially-applied run converges).
THREAD_COLUMNS: list[tuple[str, str]] = [
    (
        "submitted_at",
        f"ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ",
    ),
    (
        "resubmission_count",
        f"ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS resubmission_count "
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "last_decided_at",
        f"ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS last_decided_at TIMESTAMPTZ",
    ),
    (
        "closed_at",
        f"ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ",
    ),
    (
        "closed_kind",
        f"ALTER TABLE {THREADS_TABLE} ADD COLUMN IF NOT EXISTS closed_kind VARCHAR(16)",
    ),
]
BACKFILL_SUBMITTED_AT_SQL = (
    f"UPDATE {THREADS_TABLE} SET submitted_at = created_at WHERE submitted_at IS NULL"
)
SUBMITTED_AT_NOT_NULL_SQL = f"ALTER TABLE {THREADS_TABLE} ALTER COLUMN submitted_at SET NOT NULL"
# §12.8 G1 — matches the model's `server_default=func.now()`. AFTER the
# backfill, so existing rows get created_at, never the migration instant.
SUBMITTED_AT_DEFAULT_SQL = f"ALTER TABLE {THREADS_TABLE} ALTER COLUMN submitted_at SET DEFAULT now()"
BACKFILL_LAST_DECIDED_SQL = (
    f"UPDATE {THREADS_TABLE} SET last_decided_at = reviewed_at "
    "WHERE last_decided_at IS NULL AND reviewed_at IS NOT NULL"
)

REVISION_COLUMNS: list[tuple[str, str]] = [
    (
        "rejection_reason",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS rejection_reason TEXT",
    ),
    (
        "resubmission_of_id",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS resubmission_of_id INTEGER "
        f"REFERENCES {REVISIONS_TABLE}(id) ON DELETE SET NULL",
    ),
    (
        "resubmission_number",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS resubmission_number "
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "closed_at",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ",
    ),
    (
        "closed_kind",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS closed_kind VARCHAR(16)",
    ),
    (
        "closed_by_version",
        f"ALTER TABLE {REVISIONS_TABLE} ADD COLUMN IF NOT EXISTS closed_by_version INTEGER",
    ),
]

# Step 9 — the PRE-§12 soft-delete columns, present only where the old
# version of this script was committed (Dev). (table, old column, old CHECK).
LEGACY_COLUMNS: list[tuple[str, str, str]] = [
    (THREADS_TABLE, "author_deleted_at", "ck_threads_author_deleted_rejected"),
    (REVISIONS_TABLE, "author_dismissed_at", "ck_thread_revisions_dismissed_rejected"),
]

# Must match the model's CheckConstraint text (app/db_v2/models/thread.py).
CHECKS: dict[str, str] = {
    "ck_thread_revisions_reason_rejected": (
        f"ALTER TABLE {REVISIONS_TABLE} ADD CONSTRAINT ck_thread_revisions_reason_rejected "
        "CHECK (rejection_reason IS NULL OR status = 'rejected')"
    ),
    "ck_threads_closed_shape": (
        f"ALTER TABLE {THREADS_TABLE} ADD CONSTRAINT ck_threads_closed_shape CHECK ("
        "((closed_at IS NULL) = (closed_kind IS NULL)) "
        "AND (closed_at IS NULL OR status = 'rejected') "
        "AND (closed_kind IS NULL OR closed_kind = 'author'))"
    ),
    "ck_thread_revisions_closed_shape": (
        f"ALTER TABLE {REVISIONS_TABLE} ADD CONSTRAINT ck_thread_revisions_closed_shape CHECK ("
        "((closed_at IS NULL) = (closed_kind IS NULL)) "
        "AND (closed_at IS NULL OR status = 'rejected') "
        "AND (closed_kind IS NULL OR closed_kind IN ('author', 'superseded')) "
        "AND ((closed_by_version IS NOT NULL) = "
        "(closed_kind IS NOT NULL AND closed_kind = 'superseded')))"
    ),
}

# Step 15 (§12.3) — close every OPEN declined edit that is already behind
# the live text. The SET subqueries read the pre-update snapshot.
_NEXT_APPROVED = (
    "FROM thread_revisions a WHERE a.thread_id = r.thread_id "
    "AND a.status = 'approved' AND a.version > r.version"
)
SUPERSEDED_WHERE = (
    "t.id = r.thread_id AND r.version < t.version "
    "AND r.status = 'rejected' AND r.origin = 'author' AND r.closed_at IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM thread_revisions w WHERE w.resubmission_of_id = r.id)"
)
BACKFILL_SUPERSEDED_SQL = f"""UPDATE thread_revisions r
       SET closed_kind = 'superseded',
           closed_by_version = COALESCE((SELECT MIN(a.version) {_NEXT_APPROVED}), t.version),
           closed_at = COALESCE(
               (SELECT a.reviewed_at {_NEXT_APPROVED} ORDER BY a.version ASC LIMIT 1),
               now())
      FROM threads t
     WHERE {SUPERSEDED_WHERE}"""

CREATE_SUBMISSIONS_SQL = """CREATE TABLE IF NOT EXISTS thread_submissions (
        id                 SERIAL PRIMARY KEY,
        thread_id          INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        round              INTEGER NOT NULL,
        title              VARCHAR(200) NOT NULL,
        content            TEXT NOT NULL,
        mentions           JSONB NOT NULL DEFAULT '[]'::jsonb,
        status             VARCHAR(16) NOT NULL,
        reason             TEXT,
        submitted_by_email VARCHAR(320) NOT NULL,
        submitted_by_name  VARCHAR(255),
        submitted_at       TIMESTAMPTZ NOT NULL,
        reviewed_by_email  VARCHAR(320) NOT NULL,
        reviewed_by_name   VARCHAR(255),
        reviewed_at        TIMESTAMPTZ NOT NULL,
        CONSTRAINT ck_thread_submissions_status
            CHECK (status IN ('approved', 'rejected')),
        CONSTRAINT ck_thread_submissions_reason
            CHECK (reason IS NULL OR status = 'rejected')
    )"""

INDEXES: dict[str, str] = {
    "ix_threads_pending_submitted_id": (
        "CREATE INDEX IF NOT EXISTS ix_threads_pending_submitted_id "
        "ON threads (component_id, submitted_at, id) WHERE status = 'pending'"
    ),
    "ix_threads_last_decided_id": (
        "CREATE INDEX IF NOT EXISTS ix_threads_last_decided_id "
        "ON threads (last_decided_at DESC, id DESC) WHERE last_decided_at IS NOT NULL"
    ),
    "uq_thread_revisions_resubmission_of": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_revisions_resubmission_of "
        "ON thread_revisions (resubmission_of_id) WHERE resubmission_of_id IS NOT NULL"
    ),
    "uq_thread_submissions_thread_round": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_submissions_thread_round "
        "ON thread_submissions (thread_id, round)"
    ),
    "ix_thread_submissions_reviewed_at_id": (
        "CREATE INDEX IF NOT EXISTS ix_thread_submissions_reviewed_at_id "
        "ON thread_submissions (reviewed_at DESC, id DESC)"
    ),
}
RETIRED_INDEXES = ("ix_threads_pending_created_id", "ix_threads_reviewed_at_id")

# Step 14 — mirrors `thread_service.decide_thread`'s round write, for the
# decisions taken before this feature existed.
BACKFILL_ROUNDS_SQL = """INSERT INTO thread_submissions (
        thread_id, round, title, content, mentions, status, reason,
        submitted_by_email, submitted_by_name, submitted_at,
        reviewed_by_email, reviewed_by_name, reviewed_at
    )
    SELECT t.id, 0,
           COALESCE(v1.title, t.title), COALESCE(v1.content, t.content),
           COALESCE(v1.mentions, t.mentions),
           t.status, NULL,
           t.author_email, t.author_name, t.created_at,
           t.reviewed_by_email, t.reviewed_by_name, t.reviewed_at
      FROM threads t
      LEFT JOIN thread_revisions v1
        ON v1.thread_id = t.id AND v1.version = 1 AND t.status = 'approved'
     WHERE t.reviewed_at IS NOT NULL
       AND t.reviewed_by_email IS NOT NULL
       AND t.status IN ('approved', 'rejected')
       AND NOT EXISTS (SELECT 1 FROM thread_submissions s WHERE s.thread_id = t.id)"""

DECIDED_ALL_SQL = text(
    "SELECT status, COUNT(*) FROM threads "
    "WHERE reviewed_at IS NOT NULL AND reviewed_by_email IS NOT NULL "
    "AND status IN ('approved', 'rejected') GROUP BY status ORDER BY status"
)
DECIDED_MISSING_SQL = text(
    """
    SELECT t.status, COUNT(*) FROM threads t
     WHERE t.reviewed_at IS NOT NULL AND t.reviewed_by_email IS NOT NULL
       AND t.status IN ('approved', 'rejected')
       AND NOT EXISTS (SELECT 1 FROM thread_submissions s WHERE s.thread_id = t.id)
     GROUP BY t.status ORDER BY t.status
    """
)
# A decided thread that would be skipped: reviewed_at without a reviewer
# (never written by the service — reported, not silently dropped).
DECIDED_ANOMALY_SQL = text(
    "SELECT COUNT(*) FROM threads WHERE reviewed_at IS NOT NULL "
    "AND (reviewed_by_email IS NULL OR status NOT IN ('approved', 'rejected'))"
)
APPROVED_WITHOUT_V1_SQL = text(
    """
    SELECT COUNT(*) FROM threads t
     WHERE t.status = 'approved' AND t.reviewed_at IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM thread_revisions r WHERE r.thread_id = t.id AND r.version = 1)
    """
)
THREAD_COUNT_SQL = text(f"SELECT COUNT(*) FROM {THREADS_TABLE}")
NULL_SUBMITTED_AT_SQL = text(f"SELECT COUNT(*) FROM {THREADS_TABLE} WHERE submitted_at IS NULL")
LAST_DECIDED_ALL_SQL = text(f"SELECT COUNT(*) FROM {THREADS_TABLE} WHERE reviewed_at IS NOT NULL")
LAST_DECIDED_MISSING_SQL = text(
    f"SELECT COUNT(*) FROM {THREADS_TABLE} WHERE last_decided_at IS NULL AND reviewed_at IS NOT NULL"
)
REJECTED_REVISIONS_SQL = text(
    f"SELECT COUNT(*) FROM {REVISIONS_TABLE} WHERE status = 'rejected'"
)

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
CHECK_EXISTS_SQL = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = :name AND contype = 'c'
    )
    """
)
INDEX_EXISTS_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :index)"
)


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(conn.execute(COLUMN_EXISTS_SQL, {"table": table, "column": column}).scalar())


def _fmt_counts(rows) -> str:
    counts = {status: int(n) for status, n in rows}
    total = sum(counts.values())
    detail = ", ".join(f"{k}={v}" for k, v in counts.items()) or "none"
    return f"{total} ({detail})"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Thread reject/resubmit: new threads/thread_revisions columns, the "
        "thread_submissions table, index swaps and the round-0 backfill."
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
        for table in (THREADS_TABLE, REVISIONS_TABLE):
            if not engine_v2.dialect.has_table(conn, table):
                print(f"  REFUSING: table {table!r} does not exist. Is this the right database?")
                print("  Run scripts/migrate_thread_revisions.py first.")
                raise SystemExit(1)

        plan: list[str] = []
        print("\nPlan:")

        # -- threads columns -------------------------------------------------
        submitted_exists = _column_exists(conn, THREADS_TABLE, "submitted_at")
        last_decided_exists = _column_exists(conn, THREADS_TABLE, "last_decided_at")
        for column, ddl in THREAD_COLUMNS:
            exists = _column_exists(conn, THREADS_TABLE, column)
            print(f"  - {THREADS_TABLE}.{column}: {'already exists' if exists else 'will be added'}")
            if not exists:
                plan.append(ddl)

        null_submitted = int(
            conn.execute(NULL_SUBMITTED_AT_SQL if submitted_exists else THREAD_COUNT_SQL).scalar()
        )
        print(f"  - submitted_at backfill (= created_at): {null_submitted} thread(s)")
        if null_submitted or not submitted_exists:
            plan.append(BACKFILL_SUBMITTED_AT_SQL)
        # Idempotent; always planned so a run that added the column nullable
        # and then failed before this statement still converges.
        plan.append(SUBMITTED_AT_NOT_NULL_SQL)
        print("  - threads.submitted_at DEFAULT now() (G1): always set (idempotent)")
        plan.append(SUBMITTED_AT_DEFAULT_SQL)

        missing_last_decided = int(
            conn.execute(
                LAST_DECIDED_MISSING_SQL if last_decided_exists else LAST_DECIDED_ALL_SQL
            ).scalar()
        )
        print(f"  - last_decided_at backfill (= reviewed_at): {missing_last_decided} thread(s)")
        if missing_last_decided:
            plan.append(BACKFILL_LAST_DECIDED_SQL)

        # -- thread_revisions columns ---------------------------------------
        for column, ddl in REVISION_COLUMNS:
            exists = _column_exists(conn, REVISIONS_TABLE, column)
            print(f"  - {REVISIONS_TABLE}.{column}: {'already exists' if exists else 'will be added'}")
            if not exists:
                plan.append(ddl)

        # -- legacy soft-delete columns (pre-§12 script, committed on Dev) ---
        legacy_present: dict[str, bool] = {}
        for table, column, check in LEGACY_COLUMNS:
            present = _column_exists(conn, table, column)
            legacy_present[column] = present
            if not present:
                print(f"  - {table}.{column} (legacy): not present, nothing to convert")
                continue
            n = int(
                conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL")).scalar()
            )
            print(
                f"  - {table}.{column} (legacy): present; {n} row(s) -> closed_kind = 'author'; "
                f"then {check} and the column will be dropped"
            )
            plan.append(
                f"UPDATE {table} SET closed_at = {column}, closed_kind = 'author' "
                f"WHERE {column} IS NOT NULL AND closed_at IS NULL"
            )
            plan.append(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {check}")
            plan.append(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")

        # -- CHECKs ------------------------------------------------------------
        for name, ddl in CHECKS.items():
            exists = bool(conn.execute(CHECK_EXISTS_SQL, {"name": name}).scalar())
            print(f"  - {name}: {'already exists' if exists else 'will be added'}")
            if not exists:
                plan.append(ddl)

        # -- thread_submissions ----------------------------------------------
        submissions_exists = bool(
            conn.execute(TABLE_EXISTS_SQL, {"table": SUBMISSIONS_TABLE}).scalar()
        )
        print(
            f"  - {SUBMISSIONS_TABLE} table (with ck_thread_submissions_status/_reason): "
            f"{'already exists' if submissions_exists else 'will be created'}"
        )
        if not submissions_exists:
            plan.append(CREATE_SUBMISSIONS_SQL)

        # -- indexes -----------------------------------------------------------
        for index, ddl in INDEXES.items():
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  - {index}: {'already exists' if exists else 'will be created'}")
            if not exists:
                plan.append(ddl)
        for index in RETIRED_INDEXES:
            exists = bool(conn.execute(INDEX_EXISTS_SQL, {"index": index}).scalar())
            print(f"  - {index} (retired): {'will be dropped' if exists else 'already gone'}")
            if exists:
                plan.append(f"DROP INDEX IF EXISTS {index}")

        # -- round backfill ----------------------------------------------------
        round_rows = conn.execute(
            DECIDED_MISSING_SQL if submissions_exists else DECIDED_ALL_SQL
        ).all()
        to_insert = sum(int(n) for _, n in round_rows)
        print(f"  - round-0 backfill: {_fmt_counts(round_rows)} decided thread(s) without a round")
        no_v1 = int(conn.execute(APPROVED_WITHOUT_V1_SQL).scalar())
        if no_v1:
            print(
                f"    note: {no_v1} decided approved thread(s) have no v1 revision — their "
                "round snapshot falls back to the live columns"
            )
        anomalies = int(conn.execute(DECIDED_ANOMALY_SQL).scalar())
        if anomalies:
            print(
                f"    WARNING: {anomalies} thread(s) have reviewed_at but no reviewer or a "
                "non-decided status — SKIPPED by the backfill, investigate"
            )
        if to_insert:
            plan.append(BACKFILL_ROUNDS_SQL)
        rejected_revisions = int(conn.execute(REJECTED_REVISIONS_SQL).scalar())
        print(
            f"  - existing rejected revisions (keep rejection_reason = NULL, decision 12): "
            f"{rejected_revisions}"
        )

        # -- superseded backfill (§12.3) --------------------------------------
        # Counted against TODAY's columns: before step 4/8 there is no
        # closed_at (nothing is closed yet), before step 6 no reworks exist,
        # and a legacy-dismissed row becomes author-closed in step 9 first.
        open_parts = [
            "t.id = r.thread_id", "r.version < t.version",
            "r.status = 'rejected'", "r.origin = 'author'",
        ]
        if _column_exists(conn, REVISIONS_TABLE, "closed_at"):
            open_parts.append("r.closed_at IS NULL")
        if legacy_present.get("author_dismissed_at"):
            open_parts.append("r.author_dismissed_at IS NULL")
        if _column_exists(conn, REVISIONS_TABLE, "resubmission_of_id"):
            open_parts.append(
                "NOT EXISTS (SELECT 1 FROM thread_revisions w WHERE w.resubmission_of_id = r.id)"
            )
        superseded = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM thread_revisions r, threads t WHERE "
                    + " AND ".join(open_parts)
                )
            ).scalar()
        )
        print(
            f"  - superseded backfill (open declined edits behind the live version -> "
            f"closed_kind = 'superseded', no bell): {superseded} revision(s)"
        )
        if superseded:
            plan.append(BACKFILL_SUPERSEDED_SQL)

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
            result = conn.execute(text(stmt))
            rowcount = result.rowcount if stmt.lstrip().upper().startswith(("UPDATE", "INSERT")) else None
            print(f"    ran: {stmt.splitlines()[0]}" + (f"  [{rowcount} row(s)]" if rowcount is not None else ""))

    with engine_v2.connect() as conn:
        print("\nVerified:")
        for column, _ in THREAD_COLUMNS:
            print(f"  {THREADS_TABLE}.{column} present = {_column_exists(conn, THREADS_TABLE, column)}")
        for column, _ in REVISION_COLUMNS:
            print(f"  {REVISIONS_TABLE}.{column} present = {_column_exists(conn, REVISIONS_TABLE, column)}")
        for table, column, check in LEGACY_COLUMNS:
            print(f"  {table}.{column} (legacy) present = {_column_exists(conn, table, column)}")
            print(f"  {check} (legacy) present = {bool(conn.execute(CHECK_EXISTS_SQL, {'name': check}).scalar())}")
        print(
            f"  {SUBMISSIONS_TABLE} present = "
            f"{bool(conn.execute(TABLE_EXISTS_SQL, {'table': SUBMISSIONS_TABLE}).scalar())}"
        )
        for name in (*CHECKS, "ck_thread_submissions_status", "ck_thread_submissions_reason"):
            print(f"  {name} present = {bool(conn.execute(CHECK_EXISTS_SQL, {'name': name}).scalar())}")
        for index in INDEXES:
            print(f"  {index} present = {bool(conn.execute(INDEX_EXISTS_SQL, {'index': index}).scalar())}")
        for index in RETIRED_INDEXES:
            print(f"  {index} present = {bool(conn.execute(INDEX_EXISTS_SQL, {'index': index}).scalar())}")
        print(f"  threads with NULL submitted_at = {int(conn.execute(NULL_SUBMITTED_AT_SQL).scalar())}")
        print(f"  decided threads without a round = {_fmt_counts(conn.execute(DECIDED_MISSING_SQL).all())}")

    print("\n" + "=" * 70)
    print("Changes committed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
