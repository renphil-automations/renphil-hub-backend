-- =====================================================================
-- Hub MCP OAuth tables (PostgreSQL) - idempotent create script
--
-- REVIEW REQUIRED before use. Apply ONLY to an approved database after
-- confirming the target host and database name. NEVER auto-run this file
-- (no CI, start-up hook or deploy step may execute it). It contains no
-- secrets and creates no data.
--
-- Matches the ORM models in app/db_v2/models/mcp_oauth.py and the Alembic
-- revisions migrations_v2/versions/0001_mcp_oauth.py and
-- 0002_mcp_oauth_refresh_tokens.py:
--   * mcp_oauth_authorization_requests  (short-lived, hashed request IDs)
--   * mcp_oauth_authorization_codes     (short-lived, hashed one-time codes)
--   * mcp_oauth_refresh_tokens          (hashed rotating refresh tokens;
--     used only when MCP_OAUTH_REFRESH_TOKENS_ENABLED=true)
--
-- Every statement is additive and guarded (IF NOT EXISTS), so running it
-- twice, or on a database prepared by an earlier manual setup or by
-- Alembic, changes nothing that already exists. It never drops, rewrites
-- or deletes anything.
--
-- Usage (after review, against the approved database only):
--   psql "<approved connection string>" -v ON_ERROR_STOP=1 -f scripts/sql/mcp_oauth_create.sql
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS mcp_oauth_authorization_requests (
    id SERIAL PRIMARY KEY,
    request_hash VARCHAR(64) NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    redirect_uri TEXT NOT NULL,
    client_state TEXT NOT NULL,
    code_challenge VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_requests_request_hash
    ON mcp_oauth_authorization_requests (request_hash);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_requests_client_id
    ON mcp_oauth_authorization_requests (client_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_requests_created_at
    ON mcp_oauth_authorization_requests (created_at);

CREATE TABLE IF NOT EXISTS mcp_oauth_authorization_codes (
    id SERIAL PRIMARY KEY,
    code_hash VARCHAR(64) NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge VARCHAR(128) NOT NULL,
    user_email VARCHAR(320) NOT NULL,
    user_subject VARCHAR(128) NOT NULL,
    user_roles_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);
-- Tables from an earlier manual sandbox setup may predate this column.
ALTER TABLE mcp_oauth_authorization_codes
    ADD COLUMN IF NOT EXISTS user_roles_json TEXT NOT NULL DEFAULT '[]';
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_codes_code_hash
    ON mcp_oauth_authorization_codes (code_hash);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_codes_client_id
    ON mcp_oauth_authorization_codes (client_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_codes_user_email
    ON mcp_oauth_authorization_codes (user_email);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_codes_created_at
    ON mcp_oauth_authorization_codes (created_at);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_authorization_codes_active
    ON mcp_oauth_authorization_codes (client_id, created_at);

CREATE TABLE IF NOT EXISTS mcp_oauth_refresh_tokens (
    id SERIAL PRIMARY KEY,
    family_id VARCHAR(64) NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    user_email VARCHAR(320) NOT NULL,
    user_subject VARCHAR(128) NOT NULL,
    jkt VARCHAR(128),
    scope VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    family_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcp_oauth_refresh_tokens_token_hash
    ON mcp_oauth_refresh_tokens (token_hash);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_refresh_tokens_family_id
    ON mcp_oauth_refresh_tokens (family_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_refresh_tokens_client_id
    ON mcp_oauth_refresh_tokens (client_id);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_refresh_tokens_user_email
    ON mcp_oauth_refresh_tokens (user_email);
CREATE INDEX IF NOT EXISTS ix_mcp_oauth_refresh_tokens_expires_at
    ON mcp_oauth_refresh_tokens (expires_at);

COMMIT;
