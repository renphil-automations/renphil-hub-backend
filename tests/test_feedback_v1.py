from __future__ import annotations

import asyncio
import unittest

from app.models.airtable import FeedbackCreate
from app.services.airtable_service import AirtableService


class _FakeFeedbackTable:
    def __init__(self) -> None:
        self.fields = None

    def create(self, fields, *, typecast=True):
        self.fields = dict(fields)
        return {
            "id": "recFeedbackV1",
            "fields": {
                "Id": 123,
                **fields,
            },
        }


def _feedback_service(table: _FakeFeedbackTable) -> AirtableService:
    service = AirtableService.__new__(AirtableService)
    service._feedbacks_table = lambda: table
    # Model aliases and Airtable field names must describe the same fields.
    # Use explicit, distinct names so tests do not depend on deployment env.
    service._F_FEEDBACK_FROM = "From"
    service._F_FEEDBACK_MESSAGE = "Message"
    service._F_FEEDBACK_SOURCE = "Source"
    service._F_FEEDBACK_IMPRESSION = "Impression"
    service._F_FEEDBACK_MESSAGE_ID = "Message ID"
    service._F_FEEDBACK_QUERY = "Query"
    service._F_FEEDBACK_RESPONSE = "Response"
    return service


class FeedbackV1Tests(unittest.TestCase):
    def test_message_feedback_maps_v1_fields_and_uses_authenticated_email(self) -> None:
        table = _FakeFeedbackTable()
        service = _feedback_service(table)

        payload = FeedbackCreate(
            from_email="spoofed@example.org",
            message="Didn't search enough\n\nPlease try another useful path.",
            source="Bot",
            impression="Dislike",
            message_id="msg_abc123",
            query="What is RenPhil's holiday policy?",
            response="I don't have enough information.",
        )

        record = asyncio.run(
            service.create_feedback(
                payload,
                from_email="member@renphil.org",
            )
        )

        self.assertEqual(
            table.fields[service._F_FEEDBACK_FROM],
            "member@renphil.org",
        )
        self.assertNotEqual(
            table.fields[service._F_FEEDBACK_FROM],
            str(payload.from_email),
        )
        self.assertEqual(table.fields[service._F_FEEDBACK_SOURCE], "Bot")
        self.assertEqual(table.fields[service._F_FEEDBACK_IMPRESSION], "Dislike")
        self.assertEqual(table.fields[service._F_FEEDBACK_MESSAGE_ID], "msg_abc123")
        self.assertEqual(
            table.fields[service._F_FEEDBACK_QUERY],
            "What is RenPhil's holiday policy?",
        )
        self.assertEqual(
            table.fields[service._F_FEEDBACK_RESPONSE],
            "I don't have enough information.",
        )
        self.assertEqual(record.message_id, "msg_abc123")

    def test_legacy_feedback_remains_supported(self) -> None:
        table = _FakeFeedbackTable()
        service = _feedback_service(table)

        payload = FeedbackCreate(
            from_email="legacy@example.org",
            message="General feedback",
        )

        record = asyncio.run(
            service.create_feedback(
                payload,
                from_email="member@renphil.org",
            )
        )

        self.assertEqual(
            table.fields,
            {
                service._F_FEEDBACK_FROM: "member@renphil.org",
                service._F_FEEDBACK_MESSAGE: "General feedback",
            },
        )
        self.assertEqual(record.message, "General feedback")


if __name__ == "__main__":
    unittest.main()
