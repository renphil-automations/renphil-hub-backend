"""Prepare operational External Resources tables on an explicitly reviewed target."""

import argparse

from sqlalchemy import text

from app.config import get_settings
from app.services.external_resource_policy import resource_policy
from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.external_worker_operations_schema import OPERATIONS_SCHEMA
from app.db_v2.external_worker_schema import CONTENT_LINK_SCHEMA_STATEMENTS
from scripts.create_content_link_tables import TARGET_TABLES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    environment = get_settings().hub_environment
    if environment not in {"local", "sandbox", "production"}:
        parser.error("Configure HUB_ENVIRONMENT before applying the schema.")
    if (engine_v2.url.host, engine_v2.url.database) != (args.expected_host, args.expected_database):
        parser.error("Configured database differs from the reviewed target.")
    with engine_v2.begin() as connection:
        # Keep concurrent setup attempts from interleaving trigger creation.
        connection.execute(text("SELECT pg_advisory_xact_lock(341027651, 1)"))
        BaseV2.metadata.create_all(bind=connection, tables=TARGET_TABLES, checkfirst=True)
        for statement in (*CONTENT_LINK_SCHEMA_STATEMENTS, *OPERATIONS_SCHEMA):
            connection.execute(text(statement))
        connection.execute(
            text("INSERT INTO external_worker_control (environment) VALUES (:environment) ON CONFLICT DO NOTHING"),
            {"environment": environment},
        )
        control = connection.execute(
            text("SELECT environment, version_id FROM external_worker_control WHERE singleton FOR UPDATE")
        ).one()
        if control.environment != environment:
            raise SystemExit("Database belongs to a different Worker environment. Schema transaction rolled back.")
        rows = connection.execute(text(
            "SELECT id, url, resource_type, reason, ingestion_supported FROM content_links FOR UPDATE"
        )).mappings().all()
        for row in rows:
            policy = resource_policy(row["url"], row["resource_type"], row["reason"])
            if policy.supported is False:
                connection.execute(text(
                    "UPDATE content_links SET ingestion_supported = FALSE, ingestion_block_reason = :reason, "
                    "ingestion_enabled = CASE WHEN ingested THEN FALSE ELSE ingestion_enabled END, "
                    "reviewed_at = CASE WHEN ingested THEN COALESCE(reviewed_at, NOW()) ELSE reviewed_at END, "
                    "updated_at = clock_timestamp() WHERE id = :id"
                ), {"id": row["id"], "reason": policy.reason})
            elif row["ingestion_supported"] is None:
                connection.execute(text(
                    "UPDATE content_links SET ingestion_supported = :supported WHERE id = :id"
                ), {"id": row["id"], "supported": policy.supported})
        if control.version_id is None:
            version_id = connection.execute(
                text("INSERT INTO external_worker_setting_versions (paused) VALUES (FALSE) RETURNING id")
            ).scalar_one()
            connection.execute(
                text("UPDATE external_worker_control SET version_id = :version_id WHERE singleton"),
                {"version_id": version_id},
            )
    print(f"External Resources operational schema is ready for {environment}.")


if __name__ == "__main__":
    main()
