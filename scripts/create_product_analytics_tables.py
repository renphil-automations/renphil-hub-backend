"""Create the Hub product-usage analytics tables safely (idempotent)."""

from __future__ import annotations

from sqlalchemy import inspect, text

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.product_analytics import HubProductEngagementV2, HubProductEventV2

TARGET_TABLES = [HubProductEventV2.__table__, HubProductEngagementV2.__table__]


def _add_additive_columns() -> None:
    """Add nullable analytics columns to databases created before this change.

    This deployment script is the product-analytics schema authority today.
    Both columns are nullable so historic route-only intervals remain valid
    and are simply withheld from editor-scoped reads.
    """
    with engine_v2.begin() as connection:
        requirements = {
            "hub_product_events": {
                "view_as_role_key": "VARCHAR(128)",
                "view_as_scope_key": "VARCHAR(128)",
                "view_as_context": "JSON",
            },
            "hub_product_engagements": {
                "nav_tab_id": "VARCHAR(128)",
                "tab_id": "VARCHAR(128)",
                "view_as_role_key": "VARCHAR(128)",
                "view_as_scope_key": "VARCHAR(128)",
                "view_as_context": "JSON",
            },
        }
        inspector = inspect(connection)
        for table, required in requirements.items():
            columns = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in sorted(required.items()):
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def main() -> None:
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")
    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    _add_additive_columns()
    # create_all skips indexes on tables that already exist. Older installs
    # may have gained columns above without receiving their lookup indexes.
    for table in TARGET_TABLES:
        for index in table.indexes:
            index.create(bind=engine_v2, checkfirst=True)
    print("Hub product analytics tables are ready.")


if __name__ == "__main__":
    main()
