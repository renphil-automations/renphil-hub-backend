"""Additive operational schema for External Resources.

Run after the registry schema exists. A database is bound to one deployment
environment so a misconfigured Worker cannot process a different environment.
"""

OPERATIONS_SCHEMA = (
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS ingestion_supported BOOLEAN NULL",
    "ALTER TABLE content_links ADD COLUMN IF NOT EXISTS ingestion_block_reason TEXT NULL",
    """ALTER TABLE content_links ADD COLUMN IF NOT EXISTS folder_parent_id INTEGER
        REFERENCES content_links(id) ON DELETE SET NULL""",
    """CREATE INDEX IF NOT EXISTS ix_external_folder_children ON content_links (folder_parent_id)
        WHERE folder_parent_id IS NOT NULL""",
    """CREATE OR REPLACE VIEW external_resource_component_map AS
        SELECT link_id, component_id FROM component_link_map
        UNION
        SELECT child.id, mapping.component_id
        FROM content_links child
        JOIN content_links parent ON parent.id = child.folder_parent_id
        JOIN component_link_map mapping ON mapping.link_id = parent.id
        WHERE parent.ingestion_enabled AND NOT parent.deleted AND NOT parent.archived AND NOT child.deleted""",
    """CREATE OR REPLACE FUNCTION external_folder_parent_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.deleted AND NOT OLD.deleted)
               OR (OLD.ingestion_enabled AND NOT NEW.ingestion_enabled) THEN
                UPDATE content_links SET deleted = TRUE, deleted_at = NOW(), updated_at = NOW()
                WHERE folder_parent_id = NEW.id AND NOT deleted;
            END IF;
            RETURN NEW;
        END $$""",
    "DROP TRIGGER IF EXISTS external_folder_parent_changed ON content_links",
    """CREATE TRIGGER external_folder_parent_changed AFTER UPDATE OF deleted, ingestion_enabled ON content_links
        FOR EACH ROW WHEN (OLD.folder_parent_id IS NULL)
        EXECUTE FUNCTION external_folder_parent_changed()""",
    """CREATE OR REPLACE FUNCTION external_folder_mapping_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                UPDATE content_links SET acl_refresh_required = TRUE
                WHERE folder_parent_id = OLD.link_id AND ingested AND NOT deleted;
                RETURN OLD;
            END IF;
            UPDATE content_links SET acl_refresh_required = TRUE
            WHERE folder_parent_id = NEW.link_id AND ingested AND NOT deleted;
            RETURN NEW;
        END $$""",
    "DROP TRIGGER IF EXISTS external_folder_mapping_changed ON component_link_map",
    """CREATE TRIGGER external_folder_mapping_changed AFTER INSERT OR DELETE ON component_link_map
        FOR EACH ROW EXECUTE FUNCTION external_folder_mapping_changed()""",
    """CREATE TABLE IF NOT EXISTS external_worker_control (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        environment VARCHAR(16) NOT NULL CHECK (environment IN ('local', 'sandbox', 'production'))
    )""",
    """CREATE TABLE IF NOT EXISTS external_worker_setting_versions (
        id BIGSERIAL PRIMARY KEY,
        paused BOOLEAN NOT NULL,
        actor_email VARCHAR(320),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "ALTER TABLE external_worker_control ADD COLUMN IF NOT EXISTS version_id BIGINT REFERENCES external_worker_setting_versions(id)",
    "ALTER TABLE external_worker_runs ADD COLUMN IF NOT EXISTS environment VARCHAR(16)",
    "ALTER TABLE external_worker_runs ADD COLUMN IF NOT EXISTS config_snapshot JSONB",
    "ALTER TABLE external_worker_runs ADD COLUMN IF NOT EXISTS queue_depth INTEGER",
    "ALTER TABLE content_link_events ADD COLUMN IF NOT EXISTS environment VARCHAR(16)",
    "ALTER TABLE content_link_events ADD COLUMN IF NOT EXISTS failure_code VARCHAR(64)",
    "ALTER TABLE content_link_events ADD COLUMN IF NOT EXISTS initial_status VARCHAR(64)",
    """CREATE TABLE IF NOT EXISTS external_worker_run_items (
        run_id BIGINT NOT NULL, link_id INTEGER NOT NULL,
        initial_status VARCHAR(64), action VARCHAR(64), outcome VARCHAR(64) NOT NULL DEFAULT 'not_processed',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (run_id, link_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_external_worker_run_items_run ON external_worker_run_items (run_id, link_id)",
    "ALTER TABLE external_worker_run_items ADD COLUMN IF NOT EXISTS ocr_note VARCHAR(64)",
    "ALTER TABLE content_link_events ADD COLUMN IF NOT EXISTS duration_ms INTEGER",
    "ALTER TABLE content_link_events ADD COLUMN IF NOT EXISTS provider VARCHAR(64)",
    "ALTER TABLE content_link_events ADD COLUMN IF NOT EXISTS resource_type VARCHAR(64)",
    """CREATE TABLE IF NOT EXISTS external_worker_daily_metrics (
        day DATE NOT NULL,
        environment VARCHAR(16) NOT NULL,
        provider VARCHAR(64) NOT NULL,
        resource_type VARCHAR(64) NOT NULL,
        outcome VARCHAR(64) NOT NULL,
        failure_code VARCHAR(64) NOT NULL,
        event_count BIGINT NOT NULL,
        duration_ms_sum BIGINT NOT NULL,
        duration_count BIGINT NOT NULL,
        PRIMARY KEY (environment, day, provider, resource_type, outcome, failure_code)
    )""",
    """CREATE OR REPLACE FUNCTION external_worker_record_daily_metric() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.source = 'worker' AND NEW.environment IS NOT NULL THEN
            INSERT INTO external_worker_daily_metrics (
                day, environment, provider, resource_type, outcome, failure_code,
                event_count, duration_ms_sum, duration_count
            ) VALUES (
                (NEW.created_at AT TIME ZONE 'UTC')::date, NEW.environment,
                COALESCE(NEW.provider, 'Unknown'), COALESCE(NEW.resource_type, 'Unknown'),
                COALESCE(NEW.outcome, 'unknown'), COALESCE(NEW.failure_code, ''),
                1, COALESCE(NEW.duration_ms, 0), CASE WHEN NEW.duration_ms IS NULL THEN 0 ELSE 1 END
            )
            ON CONFLICT (environment, day, provider, resource_type, outcome, failure_code)
            DO UPDATE SET
                event_count = external_worker_daily_metrics.event_count + 1,
                duration_ms_sum = external_worker_daily_metrics.duration_ms_sum + EXCLUDED.duration_ms_sum,
                duration_count = external_worker_daily_metrics.duration_count + EXCLUDED.duration_count;
        END IF;
        RETURN NEW;
    END $$""",
    "DROP TRIGGER IF EXISTS external_worker_daily_metric ON content_link_events",
    """CREATE TRIGGER external_worker_daily_metric AFTER INSERT ON content_link_events
        FOR EACH ROW EXECUTE FUNCTION external_worker_record_daily_metric()""",
    "CREATE INDEX IF NOT EXISTS ix_external_worker_runs_env_started ON external_worker_runs (environment, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_content_link_events_worker_run ON content_link_events (worker_run_id, id)",
    "CREATE INDEX IF NOT EXISTS ix_content_link_events_env_created ON content_link_events (environment, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_component_link_map_link ON component_link_map (link_id)",
)
