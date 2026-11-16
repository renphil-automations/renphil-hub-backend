"""Neon persistence behind the existing Bot feedback contract."""
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer

from app.db_v2.models.hub_feedback import BotFeedbackV2
from app.models.airtable import FeedbackCreate, FeedbackRecord
from app.services.feedback_status_service import status_fields, status_ready


def as_record(row: BotFeedbackV2, status_available: bool = False) -> FeedbackRecord:
    extra = status_fields(row, True) if status_available else {}
    return FeedbackRecord.model_validate({**extra, "id": row.public_id, "Id": row.id,
        "From": row.actor_email, "Message": row.message, "Date & Time": row.created_at.isoformat(),
        "Source": "Bot", "Impression": row.impression, "Message ID": row.message_id,
        "Query": row.query, "Response": row.response, "environment": row.environment})


def create_bot_feedback(db, payload: FeedbackCreate, *, user_id: int, email: str, environment: str):
    # Legacy message-feedback clients do not send a retry key. Identical
    # feedback on the same message is the same submission for that caller.
    values = {key: value for key, value in payload.model_dump().items() if key != "from_email"}
    key = str(uuid5(NAMESPACE_URL, json.dumps([user_id, environment, values], sort_keys=True)))
    where = (BotFeedbackV2.actor_user_id == user_id, BotFeedbackV2.submission_id == key)
    existing = db.scalar(select(BotFeedbackV2).where(*where))
    if existing is not None:
        return as_record(existing)
    row = BotFeedbackV2(public_id=str(uuid4()), submission_id=key, actor_user_id=user_id,
        actor_email=email, environment=environment, message=payload.message.strip(),
        impression=payload.impression, message_id=payload.message_id, query=payload.query,
        response=payload.response, created_at=datetime.now(timezone.utc))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(BotFeedbackV2).where(*where))
        if existing is None:
            raise
        return as_record(existing)
    return as_record(row)


def list_bot_feedback(db, *, environment: str | None, limit: int = 500) -> list[FeedbackRecord]:
    available = status_ready(db, "bot_feedback")
    statement = select(BotFeedbackV2)
    if available:
        statement = statement.options(undefer(BotFeedbackV2.status), undefer(BotFeedbackV2.status_updated_at))
    if environment:
        statement = statement.where(BotFeedbackV2.environment == environment)
    return [as_record(row, available) for row in db.scalars(statement.order_by(BotFeedbackV2.created_at.desc(), BotFeedbackV2.id.desc()).limit(min(max(limit, 1), 500))) ]


def find_bot_feedback(db, *, environment: str, public_id: str) -> BotFeedbackV2 | None:
    available = status_ready(db, "bot_feedback")
    statement = select(BotFeedbackV2).where(BotFeedbackV2.environment == environment, BotFeedbackV2.public_id == public_id)
    if available:
        statement = statement.options(undefer(BotFeedbackV2.status), undefer(BotFeedbackV2.status_updated_at))
    return db.scalar(statement)
