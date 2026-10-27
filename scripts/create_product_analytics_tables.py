"""Create the Hub product-usage analytics table safely (idempotent)."""

from __future__ import annotations

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.product_analytics import HubProductEventV2

TARGET_TABLES = [HubProductEventV2.__table__]


def main() -> None:
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")
    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    print("Hub product analytics table is ready.")


if __name__ == "__main__":
    main()
