from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import calendar as calendar_router
from app.services.calendar_service import CalendarService


def _service(master: dict, instance: dict | None = None) -> CalendarService:
    service = object.__new__(CalendarService)
    service._get_raw = lambda _event_id: master  # type: ignore[method-assign]
    service._next_instance = lambda _event_id: instance  # type: ignore[method-assign]
    return service


def test_index_event_excludes_viewer_specific_attendance() -> None:
    future = datetime.now(timezone.utc) + timedelta(days=3)
    service = _service(
        {
            "id": "event-1",
            "summary": "All Hands",
            "start": {"dateTime": future.isoformat()},
            "end": {"dateTime": (future + timedelta(hours=1)).isoformat()},
            "location": "New York",
            "description": "Quarterly update",
            "htmlLink": "https://calendar.google.com/event-1",
            "attendees": [{"email": "person@example.org"}],
        }
    )

    result = service.get_index_event("event-1")

    assert result.summary == "All Hands"
    assert result.location == "New York"
    assert "attending" not in result.model_dump()
    assert "attendees" not in result.model_dump()


def test_index_event_uses_next_recurring_instance() -> None:
    future = datetime.now(timezone.utc) + timedelta(days=7)
    service = _service(
        {
            "id": "series-1",
            "summary": "Weekly Meeting",
            "recurrence": ["RRULE:FREQ=WEEKLY"],
            "start": {"dateTime": "2026-01-01T10:00:00+00:00"},
            "end": {"dateTime": "2026-01-01T11:00:00+00:00"},
        },
        {
            "start": {"dateTime": future.isoformat()},
            "end": {"dateTime": (future + timedelta(hours=1)).isoformat()},
        },
    )

    result = service.get_index_event("series-1")

    assert result.recurring is True
    assert result.start == future.isoformat()


def test_index_event_rejects_expired_one_off_event() -> None:
    past = datetime.now(timezone.utc) - timedelta(days=2)
    service = _service(
        {
            "id": "old-event",
            "summary": "Old Event",
            "start": {"dateTime": past.isoformat()},
            "end": {"dateTime": (past + timedelta(hours=1)).isoformat()},
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        service.get_index_event("old-event")

    assert exc_info.value.status_code == 404
    assert "expired" in str(exc_info.value.detail).lower()


def test_index_event_rejects_cancelled_event() -> None:
    future = datetime.now(timezone.utc) + timedelta(days=1)
    service = _service(
        {
            "id": "cancelled-event",
            "status": "cancelled",
            "summary": "Cancelled Event",
            "start": {"dateTime": future.isoformat()},
            "end": {"dateTime": (future + timedelta(hours=1)).isoformat()},
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        service.get_index_event("cancelled-event")

    assert exc_info.value.status_code == 404


def test_internal_calendar_endpoint_token_check_is_constant_time_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        calendar_router,
        "get_settings",
        lambda: SimpleNamespace(AGENT_SYNC_TOKEN="shared-secret"),
    )

    assert calendar_router._require_agent_sync_token("shared-secret") is None

    with pytest.raises(HTTPException) as exc_info:
        calendar_router._require_agent_sync_token("wrong-secret")

    assert exc_info.value.status_code == 401
