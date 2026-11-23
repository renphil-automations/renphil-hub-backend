-- =====================================================================
-- Bot telemetry: allow MCP tool-call runs (PostgreSQL) - idempotent
--
-- REVIEW REQUIRED before use. Apply ONLY to an approved database after
-- confirming the target host and database name. NEVER auto-run this file
-- (no CI, start-up hook or deploy step may execute it). It contains no
-- secrets and changes no rows.
--
-- Why: the standalone RenPhil MCP server records one content-free row per
-- tool call in bot_telemetry_runs with kind = 'mcp' and the approved caller
-- in actor_kind ('mcp_hub_agent', 'mcp_claude', 'mcp_chatgpt', 'mcp_gemini',
-- or 'mcp_client' for an unrecognised kind). Bot Management filters MCP
-- activity by kind and actor_kind (app/routers/bot_management.py). Apply
-- this BEFORE enabling MCP_TELEMETRY_ENABLED on the MCP server
-- (renphil-hub-mcp docs/OPERATIONS.md, deployment step 4). The values and
-- index match the Agent's scripts/create_bot_telemetry_tables.py.
--
-- What it does (all guarded, so running it twice changes nothing):
--   Transaction 1:
--     1. Refuses to run if bot_telemetry_runs does not exist (create the Bot
--        telemetry tables with the Agent's script first).
--     2. Widens actor_kind / kind to VARCHAR(16) only if a narrower type is
--        found (16 already fits every value; no rewrite otherwise).
--     3. Replaces the actor_kind and kind CHECK constraints with supersets
--        of the previous ones, added NOT VALID. This takes a brief ACCESS
--        EXCLUSIVE lock but does not scan the table; new and updated rows
--        are checked from the moment it commits.
--   Transaction 2:
--     4. VALIDATE CONSTRAINT scans the existing rows under a SHARE UPDATE
--        EXCLUSIVE lock, so reads and writes continue meanwhile. If it
--        fails, the constraint stays NOT VALID (new rows are still checked);
--        investigate the reported rows, then run this file again.
--   Separately:
--     5. Adds index ix_bot_telemetry_runs_kind_actor_started. CREATE INDEX
--        blocks writes to bot_telemetry_runs while it builds; on a very large
--        table run that one statement as CREATE INDEX CONCURRENTLY IF NOT
--        EXISTS ... instead (it cannot run inside a transaction block).
--
-- Usage (after review, against the approved database only; do NOT pass
-- psql -1 / --single-transaction, which would merge the transactions):
--   psql "<approved connection string>" -v ON_ERROR_STOP=1 -f scripts/sql/mcp_telemetry_actor_kind.sql
-- =====================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('bot_telemetry_runs') IS NULL THEN
        RAISE EXCEPTION 'bot_telemetry_runs does not exist; create the Bot telemetry tables first';
    END IF;
    IF (SELECT character_maximum_length FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'bot_telemetry_runs' AND column_name = 'actor_kind') < 16 THEN
        ALTER TABLE bot_telemetry_runs ALTER COLUMN actor_kind TYPE VARCHAR(16);
    END IF;
    IF (SELECT character_maximum_length FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'bot_telemetry_runs' AND column_name = 'kind') < 16 THEN
        ALTER TABLE bot_telemetry_runs ALTER COLUMN kind TYPE VARCHAR(16);
    END IF;
END
$$;

ALTER TABLE bot_telemetry_runs DROP CONSTRAINT IF EXISTS bot_telemetry_runs_actor_kind_check;
ALTER TABLE bot_telemetry_runs ADD CONSTRAINT bot_telemetry_runs_actor_kind_check
    CHECK (actor_kind IN ('user', 'system', 'mcp_client', 'mcp_hub_agent', 'mcp_claude', 'mcp_chatgpt', 'mcp_gemini'))
    NOT VALID;

ALTER TABLE bot_telemetry_runs DROP CONSTRAINT IF EXISTS bot_telemetry_runs_kind_check;
ALTER TABLE bot_telemetry_runs ADD CONSTRAINT bot_telemetry_runs_kind_check
    CHECK (kind IN ('request', 'job', 'mcp'))
    NOT VALID;

COMMIT;

BEGIN;
ALTER TABLE bot_telemetry_runs VALIDATE CONSTRAINT bot_telemetry_runs_actor_kind_check;
ALTER TABLE bot_telemetry_runs VALIDATE CONSTRAINT bot_telemetry_runs_kind_check;
COMMIT;

CREATE INDEX IF NOT EXISTS ix_bot_telemetry_runs_kind_actor_started
    ON bot_telemetry_runs (kind, actor_kind, started_at DESC);
