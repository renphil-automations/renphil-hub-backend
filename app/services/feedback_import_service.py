"""Offline, repeatable import of a reviewed Airtable feedback snapshot.

No live Airtable calls and no deletion. Unknown origin/environment is never
guessed. Callers must provide an environment for an already-separated snapshot.
"""
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.db_v2.models.hub_feedback import HubFeedbackV2, BotFeedbackV2


class FeedbackImportError(ValueError):
    pass


DEFAULT_FIELDS = {"source": "Source", "message": "Message", "email": "From", "date": "Date & Time",
                  "impression": "Impression", "message_id": "Message ID", "query": "Query", "response": "Response"}


def prepare_snapshot(records, *, environment, missing_source=None, fields=None, skip_ambiguous=False):
    if environment not in {"sandbox", "production", "local", "unknown"}:
        raise FeedbackImportError("An explicit valid snapshot environment is required.")
    if not isinstance(records, list) or len(records) > 20000:
        raise FeedbackImportError("Snapshot must contain at most 20000 records.")
    names = {**DEFAULT_FIELDS, **(fields or {})}
    prepared, ambiguous = [], 0
    seen = set()
    for index, record in enumerate(records):
        try:
            data = record["fields"]
            rid = record["id"]
            if not isinstance(data, dict) or not isinstance(rid, str) or not rid or len(rid) > 128 or rid in seen:
                raise ValueError()
            seen.add(rid)
            source = str(data.get(names["source"]) or "").strip().lower()
            if not source and missing_source in {"hub", "bot"}:
                source = missing_source
            if source not in {"hub", "bot"}:
                ambiguous += 1
                continue
            message = data.get(names["message"])
            email = data.get(names["email"]) or "Unknown author"
            if not isinstance(message, str) or not message.strip() or len(message) > 2000 or "\x00" in message:
                raise ValueError()
            if not isinstance(email, str) or len(email) > 320 or "\x00" in email:
                raise ValueError()
            raw_date = data.get(names["date"]) or record.get("createdTime")
            created = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError()
            key = str(uuid5(NAMESPACE_URL, f"airtable-feedback:{environment}:{rid}"))
            values = dict(public_id=key, submission_id=key, actor_user_id=None, actor_email=email,
                environment=environment, message=message.strip(), airtable_record_id=rid,
                created_at=created.astimezone(timezone.utc))
            if source == "hub":
                values["scope"] = "hub"
            else:
                for key, maximum in (("impression", 16), ("message_id", 128), ("query", 12000), ("response", 50000)):
                    value = data.get(names[key])
                    if value is not None and (not isinstance(value, str) or len(value) > maximum or "\x00" in value):
                        raise ValueError()
                    values[key] = value
            prepared.append((source, values))
        except (KeyError, ValueError, TypeError, AttributeError):
            # No source fields or record contents are included in errors.
            raise FeedbackImportError(f"Snapshot record at index {index} is invalid; no rows imported.") from None
    if ambiguous and not skip_ambiguous:
        raise FeedbackImportError(f"{ambiguous} record(s) have an ambiguous source. Review classification or explicitly skip them.")
    return prepared, ambiguous


def import_prepared(db, prepared):
    counts = {"hub": 0, "bot": 0, "existing": 0}
    try:
        for kind, values in prepared:
            model = HubFeedbackV2 if kind == "hub" else BotFeedbackV2
            other = BotFeedbackV2 if kind == "hub" else HubFeedbackV2
            query = lambda cls: select(cls.id).where(cls.environment == values["environment"], cls.airtable_record_id == values["airtable_record_id"])
            if db.scalar(query(other)) is not None:
                raise FeedbackImportError("An existing source record has a different feedback classification; no rows imported.")
            existing_id = db.scalar(query(model))
            if existing_id is not None:
                existing = db.get(model, existing_id)
                for key, value in values.items():
                    actual = getattr(existing, key)
                    if isinstance(actual, datetime) and actual.tzinfo is None:
                        actual = actual.replace(tzinfo=timezone.utc)
                    if actual != value:
                        raise FeedbackImportError("An existing source record changed; review reconciliation before importing. No rows imported.")
                counts["existing"] += 1
                continue
            db.add(model(**values))
            db.flush()
            counts[kind] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return counts
