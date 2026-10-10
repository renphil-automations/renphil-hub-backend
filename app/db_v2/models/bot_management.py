"""PostgreSQL persistence for the admin Bot Management surface."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String

from app.db_v2.database import BaseV2


class BotConfigurationVersionV2(BaseV2):
    """An immutable Hub-wide bot configuration version."""

    __tablename__ = "bot_configuration_versions"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(Integer, nullable=False, unique=True, index=True)
    settings = Column(JSON, nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    created_by_email = Column(String(320), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class BotConfigurationStateV2(BaseV2):
    """Singleton pointer to the configuration used by new conversations."""

    __tablename__ = "bot_configuration_state"

    id = Column(Integer, primary_key=True)
    active_version_id = Column(
        Integer,
        ForeignKey("bot_configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_at = Column(DateTime(timezone=True), nullable=False)


class BotSettingsAuditLogV2(BaseV2):
    """Settings changes only; page visits and rejected attempts are excluded."""

    __tablename__ = "bot_settings_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(64), nullable=False)
    actor_email = Column(String(320), nullable=False, index=True)
    from_version_id = Column(
        Integer,
        ForeignKey("bot_configuration_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    to_version_id = Column(
        Integer,
        ForeignKey("bot_configuration_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (Index("ix_bot_settings_audit_created_at", created_at.desc()),)


class BotConversationModelPinV2(BaseV2):
    """Immutable model pin for one privacy-preserving conversation key."""

    __tablename__ = "bot_conversation_model_pins"

    id = Column(Integer, primary_key=True, index=True)
    conversation_key = Column(String(129), nullable=False, unique=True, index=True)
    model_key = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)