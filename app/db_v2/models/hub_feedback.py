"""Hub product feedback; independent of Agent feedback and usage telemetry."""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from app.db_v2.database import BaseV2


class HubFeedbackV2(BaseV2):
    __tablename__ = "hub_feedback"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    public_id = Column(String(36), nullable=False, unique=True)
    submission_id = Column(String(36), nullable=False)
    # No FK cascades: deleting a resource or an account must not erase feedback.
    actor_user_id = Column(Integer, nullable=True)  # NULL only for historical imports without a verified user binding.
    actor_email = Column(String(320), nullable=False)
    environment = Column(String(16), nullable=False)
    scope = Column(String(16), nullable=False)
    message = Column(Text, nullable=False)
    page_id = Column(String(128), nullable=True)
    component_link = Column(String(128), nullable=True)
    # Never part of ordinary list/detail/submission responses, including admin lists.
    diagnostic_page_id = Column(String(128), nullable=True)
    diagnostic_route = Column(String(256), nullable=True)
    airtable_record_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("actor_user_id", "submission_id", name="uq_hub_feedback_actor_submission"),
        UniqueConstraint("environment", "airtable_record_id", name="uq_hub_feedback_airtable_environment"),
        Index("ix_hub_feedback_environment_created", "environment", "created_at"),
        Index("ix_hub_feedback_page_created", "page_id", "created_at"),
        Index("ix_hub_feedback_component_created", "component_link", "created_at"),
    )


class BotFeedbackV2(BaseV2):
    """Message-level Agent feedback, readable only through Bot Management."""
    __tablename__ = "bot_feedback"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    public_id = Column(String(36), nullable=False, unique=True)
    submission_id = Column(String(36), nullable=False)
    actor_user_id = Column(Integer, nullable=True)
    actor_email = Column(String(320), nullable=False)
    environment = Column(String(16), nullable=False)
    message = Column(Text, nullable=False)
    impression = Column(String(16), nullable=True)
    message_id = Column(String(128), nullable=True)
    query = Column(Text, nullable=True)
    response = Column(Text, nullable=True)
    airtable_record_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("actor_user_id", "submission_id", name="uq_bot_feedback_actor_submission"),
        UniqueConstraint("environment", "airtable_record_id", name="uq_bot_feedback_airtable_environment"),
        Index("ix_bot_feedback_environment_created", "environment", "created_at"),
    )
