"""The carousel's batch lookup, against the fake Google client from
test_calendar_next_occurrence: per-id statuses, order, the concurrency limit,
stopping when Google is busy, and the route's cap and auth."""

from __future__ import annotations

import threading
import unittest
from typing import Any, Callable

import httplib2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from app.dependencies import get_calendar_service, get_current_user
from app.models.auth import UserInfo
from app.routers import calendar as calendar_router
from app.services.calendar_service import MAX_BATCH_EVENTS, CalendarService
from tests.test_calendar_next_occurrence import (
    _at,
    _FakeEvents,
    _occ,
    _Request,
    _series,
    _service,
)

BUSY = "Google Calendar is busy — try again in a minute."
VIEWER = "me@example.org"


def _http_error(code: int, reason: str = "x") -> HttpError:
    return HttpError(
        httplib2.Response({"status": code}),
        f'{{"error": {{"errors": [{{"reason": "{reason}"}}]}}}}'.encode(),
    )


def _one_off(event_id: str, days: float = 1, **extra: Any) -> dict[str, Any]:
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": event_id,
        "start": _at(days),
        "end": _at(days + 0.01),
        **extra,
    }


class _Scripted(_FakeEvents):
    """Fake client where ``on_get(id)`` runs inside each events.get call.

    It may raise to simulate a Google failure, and gets are recorded by id.
    """

    def __init__(
        self,
        events: dict[str, dict[str, Any]],
        occurrences: dict[str, list[dict[str, Any]]] | None = None,
        on_get: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(events, occurrences or {})
        self.on_get = on_get
        self.gets: list[str] = []
        self._lock = threading.Lock()

    def get(self, calendarId: str, eventId: str) -> _Request:  # noqa: N803
        def run() -> dict[str, Any]:
            with self._lock:
                self.gets.append(eventId)
            if self.on_get:
                self.on_get(eventId)
            return dict(self.events[eventId])

        return _Request(run)


class BatchOutcomeTests(unittest.TestCase):
    def test_each_id_gets_its_own_status_in_request_order(self) -> None:
        def fail(event_id: str) -> None:
            if event_id == "deleted":
                raise _http_error(404)
            if event_id == "forbidden":
                raise _http_error(403, "forbiddenForNonOrganizer")
            if event_id == "network":
                raise TimeoutError("socket timed out")

        ended_series = _series(
            "ended-series", recurrence=["RRULE:FREQ=WEEKLY;UNTIL=20260101T000000Z"]
        )
        deleted_series = _series(
            "deleted-series", status="cancelled", created=None, organizer=None
        )
        fake = _Scripted(
            {
                "soon": _one_off("soon"),
                "ended": _one_off("ended", days=-1),
                "cancelled": _one_off("cancelled", status="cancelled"),
                "ended-series": ended_series,
                "deleted-series": deleted_series,
                "deleted": {},
                "forbidden": {},
                "network": {},
            },
            on_get=fail,
        )
        requested = [
            "network",
            "ended-series",
            "soon",
            "deleted",
            "ended",
            "forbidden",
            "deleted-series",
            "cancelled",
        ]

        with self.assertLogs("app.services.calendar_service", "ERROR"):
            entries = _service(fake).get_events(requested, VIEWER)

        self.assertEqual([e.requested_id for e in entries], requested)
        by_id = {e.requested_id: e for e in entries}
        self.assertEqual(
            {i: e.status for i, e in by_id.items()},
            {
                "soon": "ok",
                "ended": "expired",
                "ended-series": "expired",
                "cancelled": "unavailable",
                "deleted": "unavailable",
                "deleted-series": "unavailable",
                "forbidden": "error",
                "network": "error",
            },
        )
        self.assertEqual(by_id["soon"].event.summary, "soon")
        self.assertIsNone(by_id["ended"].event)
        self.assertIn("Event read failed", by_id["forbidden"].detail)
        self.assertEqual(by_id["network"].detail, "Could not load this event.")

    def test_split_series_keeps_the_requested_id_and_returns_the_live_series(self) -> None:
        old = _series("abc", recurrence=["RRULE:FREQ=WEEKLY;UNTIL=20260101T000000Z"])
        new = _series("abc_R20261119T160000")
        fake = _FakeEvents(
            {"abc": old, new["id"]: new},
            {f"{new['id']}@google.com": [_occ(new["id"], 6)]},
        )

        [entry] = _service(fake).get_events(["abc"], VIEWER)

        self.assertEqual(entry.requested_id, "abc")
        self.assertEqual(entry.event.id, new["id"])

    def test_attending_is_for_the_viewer_asking(self) -> None:
        event = _one_off("o", attendees=[{"email": "ME@example.org"}])
        service = _service(_FakeEvents({"o": event}, {}))

        self.assertIs(service.get_events(["o"], VIEWER)[0].event.attending, True)
        self.assertIs(
            service.get_events(["o"], "other@example.org")[0].event.attending, False
        )

    def test_repeated_id_is_looked_up_once_but_answered_each_time(self) -> None:
        fake = _Scripted({"a": _one_off("a"), "b": _one_off("b")})

        entries = _service(fake).get_events(["a", "b", "a"], VIEWER)

        self.assertEqual([e.requested_id for e in entries], ["a", "b", "a"])
        self.assertEqual(sorted(fake.gets), ["a", "b"])

    def test_blank_id_never_reaches_google(self) -> None:
        fake = _Scripted({})

        [entry] = _service(fake).get_events(["  "], VIEWER)

        self.assertEqual(entry.status, "unavailable")
        self.assertEqual(fake.gets, [])


class BatchConcurrencyTests(unittest.TestCase):
    def test_runs_five_lookups_at_a_time_and_no_more(self) -> None:
        # Each lookup waits for four others, so this fails unless five really
        # overlap; `peak` fails if a sixth ever joins them.
        barrier = threading.Barrier(5, timeout=10)
        lock = threading.Lock()
        running = peak = 0

        def on_get(_event_id: str) -> None:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            barrier.wait()
            with lock:
                running -= 1

        ids = [f"e{i}" for i in range(20)]
        fake = _Scripted({i: _one_off(i) for i in ids}, on_get=on_get)

        entries = _service(fake).get_events(ids, VIEWER)

        self.assertEqual({e.status for e in entries}, {"ok"})
        self.assertEqual(peak, 5)

    def test_google_busy_stops_ids_not_yet_started(self) -> None:
        # The five running lookups all fail together; the other fifteen must
        # report the same error without calling Google.
        barrier = threading.Barrier(5, timeout=10)

        def on_get(_event_id: str) -> None:
            barrier.wait()
            raise _http_error(429, "rateLimitExceeded")

        ids = [f"e{i}" for i in range(20)]
        fake = _Scripted({i: {} for i in ids}, on_get=on_get)

        with self.assertLogs("app.services.calendar_service", "WARNING"):
            entries = _service(fake).get_events(ids, VIEWER)

        self.assertEqual(len(fake.gets), 5)
        self.assertEqual([e.requested_id for e in entries], ids)
        self.assertEqual({(e.status, e.detail) for e in entries}, {("error", BUSY)})

    def test_other_errors_do_not_stop_the_remaining_ids(self) -> None:
        def on_get(event_id: str) -> None:
            if event_id == "e0":
                raise _http_error(403, "forbiddenForNonOrganizer")

        ids = [f"e{i}" for i in range(10)]
        fake = _Scripted({i: _one_off(i) for i in ids}, on_get=on_get)

        entries = _service(fake).get_events(ids, VIEWER)

        self.assertEqual(len(fake.gets), 10)
        self.assertEqual([e.status for e in entries], ["error"] + ["ok"] * 9)


class BatchRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = [f"e{i}" for i in range(MAX_BATCH_EVENTS)]
        self.fake = _Scripted({i: _one_off(i) for i in self.ids})
        self.app = FastAPI()
        self.app.include_router(calendar_router.router)
        self.app.dependency_overrides[get_calendar_service] = lambda: _service(self.fake)
        self.client = TestClient(self.app)

    def _sign_in(self) -> None:
        self.app.dependency_overrides[get_current_user] = lambda: UserInfo.model_construct(
            email=VIEWER
        )

    def test_returns_entries_in_request_order_with_the_full_event(self) -> None:
        self._sign_in()

        res = self.client.get("/data/calendar/events", params={"ids": ["e3", "e1"]})

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual([e["requested_id"] for e in body], ["e3", "e1"])
        self.assertEqual(body[0]["status"], "ok")
        self.assertEqual(body[0]["event"]["id"], "e3")
        self.assertIs(body[0]["event"]["attending"], False)

    def test_cap_allows_twenty_and_rejects_twenty_one(self) -> None:
        self._sign_in()

        ok = self.client.get("/data/calendar/events", params={"ids": self.ids})
        over = self.client.get("/data/calendar/events", params={"ids": self.ids + ["x"]})

        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()), MAX_BATCH_EVENTS)
        self.assertEqual(over.status_code, 422)

    def test_rejected_cap_makes_no_google_calls(self) -> None:
        self._sign_in()

        self.client.get("/data/calendar/events", params={"ids": self.ids + ["x"]})

        self.assertEqual(self.fake.gets, [])

    def test_missing_ids_is_rejected(self) -> None:
        self._sign_in()

        self.assertEqual(self.client.get("/data/calendar/events").status_code, 422)

    def test_requires_sign_in(self) -> None:
        res = self.client.get("/data/calendar/events", params={"ids": ["e1"]})

        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.fake.gets, [])


if __name__ == "__main__":
    unittest.main()
