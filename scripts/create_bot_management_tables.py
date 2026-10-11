"""Create Bot Management PostgreSQL tables safely."""

from __future__ import annotations

from app.db_v2.database import BaseV2, engine_v2
from app.db_v2.models.bot_management import (
    BotConversationModelPinV2,
    BotConfigurationStateV2,
    BotConfigurationVersionV2,
    BotSettingsAuditLogV2,
)

TARGET_TABLES = [
    BotConfigurationVersionV2.__table__,
    BotConfigurationStateV2.__table__,
    BotSettingsAuditLogV2.__table__,
    BotConversationModelPinV2.__table__,
]


def main() -> None:
    print(f"Connecting to: {engine_v2.url.render_as_string(hide_password=True)}")
    BaseV2.metadata.create_all(bind=engine_v2, tables=TARGET_TABLES, checkfirst=True)
    print("Bot Management tables are ready.")


if __name__ == "__main__":
    main()
