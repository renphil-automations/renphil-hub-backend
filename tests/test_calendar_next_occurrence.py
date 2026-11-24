"""Next-occurrence resolution for the calendar widget, against a fake Google
Calendar client: sorted occurrences, occurrence ids, ended/cancelled one-offs,
and following a series split by "This and following events"."""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from app.services.calendar_service import CalendarService, _EventCache

NOW = datetime.now(timezone.utc)

_case = unittest.TestCase()


def _at(days: float) -> dict[str, str]:
    return {"dateTime": (NOW + timedelta(days=days)).isoformat()}


def _occ(series: str, days: float) -> dict[str, Any]:
    return {"id": f"{series}_{days}", "start": _at(days), "end": _at(days + 0.01)}


def _only_fields(items: list[dict[str, Any]], fields: str | None) -> list[dict[str, Any]]:
    """Keep only the item keys named in ``fields``, as Google does."""
    match = re.search(r"items\(([^)]*)\)", fields or "")
    if not match:
        return items
    keep = set(match.group(1).split(","))
    return [{k: v for k, v in item.items() if k in keep} for item in items]


class _Request:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.headers: dict[str, str] = {}

    def execute(self, num_retries: int = 0) -> Any:
        return self._result() if callable(self._result) else self._result


class _FakeEvents:
    """Just enough of ``service.events()`` for CalendarService.

    *events* maps id → raw event; *occurrences* maps iCalUID → upcoming
    occurrences (any order — the fake sorts, as Google does with
    ``orderBy=startTime``); *pages* optionally injects leading empty pages.
    """

    def __init__(
        self,
        events: dict[str, dict[str, Any]],
        occurrences: dict[str, list[dict[str, Any]]],
        empty_leading_pages: int = 0,
    ) -> None:
        self.events = events
        self.occurrences = occurrences
        self.empty_leading_pages = empty_leading_pages
        self.patched: list[tuple[str, dict[str, Any]]] = []

    def get(self, calendarId: str, eventId: str) -> _Request:  # noqa: N803
        return _Request(dict(self.events[eventId]))

    def list(self, calendarId: str, **kw: Any) -> _Request:  # noqa: N803
        if kw.get("iCalUID"):
            token = kw.get("pageToken")
            seen = int(token) if token else 0
            if seen < self.empty_leading_pages:
                return _Request({"items": [], "nextPageToken": str(seen + 1)})
            occ = sorted(
                self.occurrences.get(kw["iCalUID"], []),
                key=lambda o: o["start"]["dateTime"],
            )
            return _Request(
                {"items": _only_fields(occ[: kw.get("maxResults", 250)], kw.get("fields"))}
            )
        # singleEvents=False: series/one-offs with any occurrence ending after now.
        live = [
            e for e in self.events.values()
            if self.occurrences.get(e.get("iCalUID", ""))
        ]
        return _Request({"items": live})

    def patch(self, calendarId: str, eventId: str, body: dict, **_kw: Any) -> _Request:  # noqa: N803
        self.patched.append((eventId, body))
        return _Request({})


def _service(fake: _FakeEvents) -> CalendarService:
    service = object.__new__(CalendarService)
    service._settings = SimpleNamespace(  # type: ignore[attr-defined]
        GOOGLE_CALENDAR_ID="cal@example.org", GOOGLE_CALENDAR_SEND_UPDATES="none"
    )
    service._service = SimpleNamespace(events=lambda: fake)  # type: ignore[attr-defined]
    service._cache = _EventCache()  # type: ignore[attr-defined]
    return service


def _series(event_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "summary": "Weekly Sync",
        "recurrence": ["RRULE:FREQ=WEEKLY"],
        "start": _at(-30),
        "end": _at(-29.99),
        "created": "2025-01-01T00:00:00.000Z",
        "organizer": {"email": "owner@example.org"},
        **extra,
    }


def test_next_occurrence_is_earliest_even_if_google_returns_them_scrambled() -> None:
    fake = _FakeEvents(
        {"s": _series("s")},
        # Captured shape from the live calendar: Jan before Dec.
        {"s@google.com": [_occ("s", 61), _occ("s", 33), _occ("s", 5), _occ("s", 96)]},
    )

    event = _service(fake).get_event("s", "me@example.org")

    assert event.start == _at(5)["dateTime"]
    assert event.recurring is True


def test_next_occurrence_follows_empty_pages() -> None:
    fake = _FakeEvents(
        {"s": _series("s")}, {"s@google.com": [_occ("s", 2)]}, empty_leading_pages=2
    )

    assert _service(fake).get_event("s", "me@example.org").start == _at(2)["dateTime"]


def _zoom(meeting_id: str) -> dict[str, Any]:
    return {
        "conferenceSolution": {"name": "Zoom Meeting"},
        "entryPoints": [{"entryPointType": "video", "uri": f"https://zoom.us/j/{meeting_id}"}],
    }


def test_card_shows_the_next_occurrences_own_edits() -> None:
    series = _series(
        "s",
        description="Join https://zoom.us/j/111",
        location="Room A",
        htmlLink="https://calendar.google.com/series",
        conferenceData=_zoom("111"),
    )
    edited = {
        **_occ("s", 3),
        "summary": "Weekly Sync: Annual Planning",
        "description": "Join https://zoom.us/j/222",
        "location": "Room B",
        "htmlLink": "https://calendar.google.com/occurrence",
        "conferenceData": _zoom("222"),
    }
    fake = _FakeEvents({"s": series}, {"s@google.com": [edited]})

    event = _service(fake).get_event("s", "me@example.org")

    assert event.summary == "Weekly Sync: Annual Planning"
    assert event.description == "Join https://zoom.us/j/222"
    assert event.location == "Room B"
    assert event.html_link == "https://calendar.google.com/occurrence"
    assert event.meeting_link == "https://zoom.us/j/222"
    assert event.meeting_label == "Zoom Meeting"


def test_cleared_fields_on_an_occurrence_do_not_fall_back_to_the_series() -> None:
    # Google leaves out empty fields, so the occurrence has no description,
    # location or conference at all.
    series = _series(
        "s", description="Old agenda", location="Room A", hangoutLink="https://meet.google.com/old"
    )
    fake = _FakeEvents({"s": series}, {"s@google.com": [_occ("s", 3)]})

    event = _service(fake).get_event("s", "me@example.org")

    assert event.summary == "Weekly Sync"
    assert event.description is None
    assert event.location is None
    assert event.meeting_link is None


def test_occurrence_id_is_resolved_to_its_series() -> None:
    occurrence = {
        "id": "s_20260101T100000Z",
        "recurringEventId": "s",
        "start": _at(-10),
        "end": _at(-9.99),
    }
    fake = _FakeEvents(
        {"s": _series("s"), occurrence["id"]: occurrence},
        {"s@google.com": [_occ("s", 4)]},
    )

    event = _service(fake).get_event(occurrence["id"], "me@example.org")

    assert event.id == "s"
    assert event.start == _at(4)["dateTime"]


def test_ended_one_off_is_reported_expired() -> None:
    one_off = {"id": "o", "status": "confirmed", "start": _at(-1), "end": _at(-0.99)}
    fake = _FakeEvents({"o": one_off}, {})

    with _case.assertRaises(HTTPException) as exc_info:
        _service(fake).get_event("o", "me@example.org")

    assert exc_info.exception.status_code == 404
    assert "expired" in str(exc_info.exception.detail).lower()


def test_upcoming_one_off_still_shows() -> None:
    one_off = {"id": "o", "status": "confirmed", "start": _at(1), "end": _at(1.01)}
    fake = _FakeEvents({"o": one_off}, {})

    event = _service(fake).get_event("o", "me@example.org")

    assert event.start == _at(1)["dateTime"]
    assert event.recurring is False


def test_cancelled_one_off_is_unavailable() -> None:
    one_off = {"id": "o", "status": "cancelled", "start": _at(1), "end": _at(1.01)}
    fake = _FakeEvents({"o": one_off}, {})

    with _case.assertRaises(HTTPException) as exc_info:
        _service(fake).get_event("o", "me@example.org")

    assert exc_info.exception.status_code == 404


def test_split_with_guests_follows_r_suffixed_continuation() -> None:
    old = _series("abc", recurrence=["RRULE:FREQ=WEEKLY;UNTIL=20260101T000000Z"])
    new = _series("abc_R20261119T160000", created="2025-06-01T00:00:00.000Z")
    unrelated = _series("zzz", created=old["created"], organizer={"email": "x@example.org"})
    fake = _FakeEvents(
        {"abc": old, new["id"]: new, "zzz": unrelated},
        {
            f"{new['id']}@google.com": [_occ(new["id"], 6)],
            "zzz@google.com": [_occ("zzz", 1)],
        },
    )

    event = _service(fake).get_event("abc", "me@example.org")

    assert event.id == new["id"]
    assert event.start == _at(6)["dateTime"]


def test_split_without_guests_follows_continuation_by_created_and_organizer() -> None:
    # Guest-less split: Google gives the continuation an unrelated id.
    old = _series("3cfb0gl1", recurrence=["RRULE:FREQ=DAILY;UNTIL=20260101T000000Z"])
    new = _series("v76oq02c", sequence=1)
    fake = _FakeEvents(
        {"3cfb0gl1": old, "v76oq02c": new},
        {"v76oq02c@google.com": [_occ("v76oq02c", 1)]},
    )

    event = _service(fake).get_event("3cfb0gl1", "me@example.org")

    assert event.id == "v76oq02c"
    assert event.start == _at(1)["dateTime"]


def test_cancelled_original_follows_r_suffixed_continuation() -> None:
    # Google strips `created` from a cancelled original — prefix still links it.
    old = {"id": "abc", "iCalUID": "abc@google.com", "status": "cancelled",
           "recurrence": ["RRULE:FREQ=WEEKLY"], "start": _at(-400), "end": _at(-399.99)}
    new = _series("abc_R20261105T160000")
    fake = _FakeEvents(
        {"abc": old, new["id"]: new}, {f"{new['id']}@google.com": [_occ(new["id"], 3)]}
    )

    assert _service(fake).get_event("abc", "me@example.org").id == new["id"]


def test_chained_splits_pick_the_half_running_next() -> None:
    old = _series("abc", recurrence=["RRULE:FREQ=WEEKLY;UNTIL=20260101T000000Z"])
    first = _series("abc_R20261105T160000")
    second = _series("abc_R20261119T160000")
    fake = _FakeEvents(
        {"abc": old, first["id"]: first, second["id"]: second},
        {
            f"{first['id']}@google.com": [_occ(first["id"], 4)],
            f"{second['id']}@google.com": [_occ(second["id"], 18), _occ(second["id"], 32)],
        },
    )

    assert _service(fake).get_event("abc", "me@example.org").id == first["id"]


def test_ended_series_without_continuation_is_unavailable() -> None:
    old = _series("abc", recurrence=["RRULE:FREQ=WEEKLY;UNTIL=20260101T000000Z"])
    fake = _FakeEvents({"abc": old}, {})

    with _case.assertRaises(HTTPException) as exc_info:
        _service(fake).get_event("abc", "me@example.org")

    assert exc_info.exception.status_code == 404
    assert "no upcoming" in str(exc_info.exception.detail).lower()


def test_rsvp_on_ended_half_patches_the_continuation() -> None:
    old = _series("3cfb0gl1", recurrence=["RRULE:FREQ=DAILY;UNTIL=20260101T000000Z"])
    new = _series("v76oq02c", attendees=[])
    fake = _FakeEvents(
        {"3cfb0gl1": old, "v76oq02c": new},
        {"v76oq02c@google.com": [_occ("v76oq02c", 1)]},
    )

    assert _service(fake).set_attendance("3cfb0gl1", "me@example.org", True) is True
    assert [event_id for event_id, _ in fake.patched] == ["v76oq02c"]


def load_tests(loader, tests, pattern):  # noqa: ARG001
    """Run this module's plain test functions under ``python -m unittest``."""
    return unittest.TestSuite(
        unittest.FunctionTestCase(fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_")
    )
