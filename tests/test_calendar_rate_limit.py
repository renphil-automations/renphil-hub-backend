"""Rate-limit handling in CalendarService, against the real Google client with
scripted HTTP responses: backoff retries, the 503 when they run out, logging,
and the per-thread transport that makes the worker-thread routes safe."""

from __future__ import annotations

import inspect
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from app.helpers.google_client import build_calendar_service
from app.routers import calendar as calendar_router
from app.services.calendar_service import CalendarService, _EventCache

SERVICE_LOGGER = "app.services.calendar_service"


def _error(code: int, reason: str) -> tuple[dict[str, str], bytes]:
    body = {"error": {"code": code, "message": reason, "errors": [{"reason": reason}]}}
    return {"status": str(code)}, json.dumps(body).encode()


RATE_429 = _error(429, "rateLimitExceeded")
RATE_403 = _error(403, "rateLimitExceeded")
USER_RATE_403 = _error(403, "userRateLimitExceeded")
FORBIDDEN_403 = _error(403, "forbiddenForNonOrganizer")
SERVER_500 = _error(500, "backendError")
NOT_FOUND_404 = _error(404, "notFound")


def _ok(body: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    return {"status": "200"}, json.dumps(body).encode()


def _one_off(**extra: Any) -> dict[str, Any]:
    start = datetime.now(timezone.utc) + timedelta(days=1)
    return {
        "id": "o",
        "etag": '"1"',
        "status": "confirmed",
        "summary": "Town Hall",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        **extra,
    }


class CalendarRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        sleeper = patch("googleapiclient.http.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def _service(self, *responses: tuple[dict[str, str], bytes]) -> CalendarService:
        self.http = HttpMockSequence(list(responses))
        service = object.__new__(CalendarService)
        service._settings = SimpleNamespace(  # type: ignore[attr-defined]
            GOOGLE_CALENDAR_ID="cal@example.org", GOOGLE_CALENDAR_SEND_UPDATES="none"
        )
        service._service = build(  # type: ignore[attr-defined]
            "calendar", "v3", http=self.http, static_discovery=True
        )
        service._cache = _EventCache()  # type: ignore[attr-defined]
        return service

    def _assert_busy(self, exc: HTTPException) -> None:
        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.headers, {"Retry-After": "60"})
        self.assertEqual(exc.detail, "Google Calendar is busy — try again in a minute.")

    def test_rate_limit_followed_by_success_returns_the_event(self) -> None:
        service = self._service(RATE_429, _ok(_one_off()))

        event = service.get_event("o", "me@example.org")

        self.assertEqual(event.summary, "Town Hall")
        self.assertEqual(self.sleep.call_count, 1)

    def test_backoff_delays_are_randomised_and_grow(self) -> None:
        service = self._service(RATE_429, RATE_429, RATE_429, _ok(_one_off()))

        with patch("googleapiclient.http.random.random", return_value=0.5):
            # HttpRequest captures random.random when it is constructed.
            service.get_event("o", "me@example.org")

        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1.0, 2.0, 4.0])

    def test_persistent_429_becomes_503_after_three_retries(self) -> None:
        service = self._service(RATE_429, RATE_429, RATE_429, RATE_429)

        with self.assertLogs(SERVICE_LOGGER, "WARNING") as logs:
            with self.assertRaises(HTTPException) as caught:
                service.get_event("o", "me@example.org")

        self._assert_busy(caught.exception)
        self.assertEqual(self.sleep.call_count, 3)
        self.assertIn("rate limit", logs.output[0].lower())
        self.assertIn("Event read", logs.output[0])

    def test_rate_limit_403s_are_retried_and_become_503(self) -> None:
        for response in (RATE_403, USER_RATE_403):
            with self.subTest(response=response):
                self.sleep.reset_mock()
                service = self._service(response, response, response, response)

                with self.assertLogs(SERVICE_LOGGER, "WARNING"):
                    with self.assertRaises(HTTPException) as caught:
                        service.search_events("town")

                self._assert_busy(caught.exception)
                self.assertEqual(self.sleep.call_count, 3)

    def test_persistent_server_error_becomes_503_but_is_not_called_rate_limit(self) -> None:
        service = self._service(SERVER_500, SERVER_500, SERVER_500, SERVER_500)

        with self.assertLogs(SERVICE_LOGGER, "WARNING") as logs:
            with self.assertRaises(HTTPException) as caught:
                service.get_event("o", "me@example.org")

        self._assert_busy(caught.exception)
        self.assertNotIn("rate limit", logs.output[0].lower())

    def test_other_403_is_not_retried_and_stays_502(self) -> None:
        service = self._service(FORBIDDEN_403)

        with self.assertRaises(HTTPException) as caught:
            service.get_event("o", "me@example.org")

        self.assertEqual(caught.exception.status_code, 502)
        self.sleep.assert_not_called()

    def test_404_is_not_retried_and_stays_404(self) -> None:
        service = self._service(NOT_FOUND_404)

        with self.assertRaises(HTTPException) as caught:
            service.get_event("o", "me@example.org")

        self.assertEqual(caught.exception.status_code, 404)
        self.sleep.assert_not_called()

    def test_rsvp_patch_retries_once_then_503(self) -> None:
        # Resolve, read for the ETag, then the patch and its one retry.
        service = self._service(_ok(_one_off()), _ok(_one_off()), RATE_429, RATE_429)

        with self.assertLogs(SERVICE_LOGGER, "WARNING") as logs:
            with self.assertRaises(HTTPException) as caught:
                service.set_attendance("o", "me@example.org", True)

        self._assert_busy(caught.exception)
        self.assertEqual(self.sleep.call_count, 1)
        self.assertIn("Attendance update", logs.output[0])

    def test_rsvp_patch_retry_that_succeeds_returns_attending(self) -> None:
        service = self._service(
            _ok(_one_off()), _ok(_one_off()), RATE_429, _ok(_one_off())
        )

        self.assertIs(service.set_attendance("o", "me@example.org", True), True)

    def test_batch_reports_persistent_429_on_the_entry_after_real_retries(self) -> None:
        service = self._service(RATE_429, RATE_429, RATE_429, RATE_429)

        with self.assertLogs(SERVICE_LOGGER, "WARNING"):
            [entry] = service.get_events(["o"], "me@example.org")

        self.assertEqual(entry.status, "error")
        self.assertEqual(entry.detail, "Google Calendar is busy — try again in a minute.")
        self.assertEqual(self.sleep.call_count, 3)

    def test_batch_recovers_when_a_retry_succeeds(self) -> None:
        service = self._service(RATE_429, _ok(_one_off()))

        [entry] = service.get_events(["o"], "me@example.org")

        self.assertEqual(entry.status, "ok")
        self.assertEqual(entry.event.summary, "Town Hall")

    def test_each_thread_gets_its_own_transport_with_shared_credentials(self) -> None:
        settings = SimpleNamespace(
            GOOGLE_CALENDAR_REFRESH_TOKEN="token",
            GOOGLE_CLIENT_ID="id",
            GOOGLE_CLIENT_SECRET="secret",
        )
        events = build_calendar_service(settings).events()  # type: ignore[arg-type]
        request = lambda: events.get(calendarId="c", eventId="a")  # noqa: E731

        here, again = request(), request()
        with ThreadPoolExecutor(max_workers=1) as pool:
            elsewhere = pool.submit(request).result()

        self.assertIs(here.http, again.http)
        self.assertIsNot(here.http, elsewhere.http)
        self.assertIsNot(here.http.http, elsewhere.http.http)
        self.assertIs(here.http.credentials, elsewhere.http.credentials)

    def test_routes_run_on_worker_threads(self) -> None:
        for route in (
            calendar_router.search_events,
            calendar_router.get_event,
            calendar_router.get_events,
            calendar_router.get_index_event,
            calendar_router.set_attendance,
        ):
            self.assertFalse(inspect.iscoroutinefunction(route), route.__name__)


if __name__ == "__main__":
    unittest.main()
