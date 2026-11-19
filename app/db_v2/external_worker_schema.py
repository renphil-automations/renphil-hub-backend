"""Idempotent PostgreSQL schema for the external-resource worker.

Single source of truth used by scripts/create_content_link_tables.py and the
Alembic revision ``0002_external_worker``. Every statement can run any number
of times, so either path (or both) leaves the same schema.
"""

from __future__ import annotations

# Columns added to content_links after its first release (admin review,
# archive, and worker access assessment). create_all does not alter tables.
CONTENT_LINK_REVIEW_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS ingestion_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ NULL",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ NULL",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS is_public BOOLEAN NULL",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS service_account_accessible BOOLEAN NULL",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS access_checked_at TIMESTAMPTZ NULL",
    "CREATE INDEX IF NOT EXISTS ix_content_links_ingestion_enabled ON content_links (ingestion_enabled)",
    "CREATE INDEX IF NOT EXISTS ix_content_links_archived ON content_links (archived)",
    "CREATE INDEX IF NOT EXISTS ix_content_links_is_public ON content_links (is_public)",
    "CREATE INDEX IF NOT EXISTS ix_content_links_service_account_accessible ON content_links (service_account_accessible)",
)

EXTERNAL_WORKER_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # Persisted retry backoff.
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NULL",
    "CREATE INDEX IF NOT EXISTS ix_content_links_next_attempt_at ON content_links (next_attempt_at)",
    # Change validators for periodic re-checks of ingested documents.
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS source_etag TEXT NULL",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS source_modified TEXT NULL",
    # Per-resource history (worker outcomes and administrator actions).
    """CREATE TABLE IF NOT EXISTS content_link_events (
        id BIGSERIAL PRIMARY KEY,
        link_id INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        source VARCHAR(16) NOT NULL,
        action VARCHAR(64) NOT NULL,
        actor_email VARCHAR(320) NULL,
        outcome VARCHAR(64) NULL,
        reason TEXT NULL,
        worker_run_id BIGINT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_content_link_events_link_created ON content_link_events (link_id, created_at)",
    # Worker run log for Bot Management health.
    """CREATE TABLE IF NOT EXISTS external_worker_runs (
        id BIGSERIAL PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ NULL,
        mode VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL,
        selected INTEGER NULL,
        outcomes JSONB NULL,
        stopped_reason TEXT NULL,
        error TEXT NULL,
        host TEXT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_external_worker_runs_started_at ON external_worker_runs (started_at)",
)


CONTENT_LINK_SCHEMA_STATEMENTS: tuple[str, ...] = CONTENT_LINK_REVIEW_STATEMENTS + EXTERNAL_WORKER_SCHEMA_STATEMENTS
