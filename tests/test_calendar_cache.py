"""The calendar event cache, against the fake Google client from
test_calendar_next_occurrence: Google call counts, expiry, the 404 outcomes,
per-viewer attendance, RSVPs, and viewers arriving together."""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Callable
from unittest.mock import patch

from fastapi import HTTPException

from app.helpers.exceptions import CalendarEventGoneError
from app.services import calendar_service
from app.services.calendar_service import CalendarService, _EventCache
from tests.test_calendar_batch import _http_error, _one_off, _Scripted
from tests.test_calendar_next_occurrence import _occ, _series, _service

ALICE = "alice@example.org"
BOB = "bob@example.org"


class _Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _cached_service(fake: _Scripted) -> tuple[CalendarService, _Clock]:
    clock = _Clock()
    service = _service(fake)
    service._cache = _EventCache(clock=clock)  # type: ignore[attr-defined]
    return service, clock


class RepeatLookupTests(unittest.TestCase):
    def test_repeat_lookups_within_the_ttl_call_google_once(self) -> None:
        fake = _Scripted({"o": _one_off("o")})
        service, clock = _cached_service(fake)

        service.get_event("o", ALICE)
        clock.now += 59
        service.get_event("o", ALICE)

        self.assertEqual(fake.gets, ["o"])

    def test_lookup_after_the_ttl_calls_google_again(self) -> None:
        fake = _Scripted({"o": _one_off("o")})
        service, clock = _cached_service(fake)

        service.get_event("o", ALICE)
        clock.now += 60
        service.get_event("o", ALICE)

        self.assertEqual(fake.gets, ["o", "o"])

    def test_google_side_edit_shows_after_the_ttl(self) -> None:
        fake = _Scripted({"o": _one_off("o", summary="Old title")})
        service, clock = _cached_service(fake)
        service.get_event("o", ALICE)

        fake.events["o"]["summary"] = "New title"
        self.assertEqual(service.get_event("o", ALICE).summary, "Old title")
        clock.now += 60
        self.assertEqual(service.get_event("o", ALICE).summary, "New title")

    def test_each_viewer_gets_their_own_attending(self) -> None:
        fake = _Scripted({"o": _one_off("o", attendees=[{"email": ALICE.upper()}])})
        service, _clock = _cached_service(fake)

        self.assertTrue(service.get_event("o", ALICE).attending)
        self.assertFalse(service.get_event("o", BOB).attending)
        self.assertEqual(fake.gets, ["o"])

    def test_index_event_shares_the_cache_and_keeps_the_requested_id(self) -> None:
        fake = _Scripted(
            {"s_20270101": {"id": "s_20270101", "recurringEventId": "s"}, "s": _series("s")},
            {"s@google.com": [_occ("s", 1)]},
        )
        service, _clock = _cached_service(fake)

        self.assertEqual(service.get_event("s_20270101", ALICE).id, "s")
        self.assertEqual(service.get_index_event("s_20270101").id, "s_20270101")
        self.assertEqual(fake.gets, ["s_20270101", "s"])


class OccurrenceEndTests(unittest.TestCase):
    def test_entry_does_not_outlive_the_occurrence_it_shows(self) -> None:
        # The widget refetches just after an event ends and then not again for
        # up to 15 minutes, so it must not get the ended occurrence back.
        fake = _Scripted(
            {"s": _series("s")},
            {"s@google.com": [_occ("s", 30 / 86400 - 0.01)]},  # Ends in about 30 s.
        )
        service, clock = _cached_service(fake)
        shown = service._cache._expiry_of(service._resolve("s"))  # type: ignore[attr-defined]
        self.assertLess(shown, clock.now + 59)

        service.get_event("s", ALICE)
        fake.gets.clear()
        clock.now = shown - 1
        service.get_event("s", ALICE)
        self.assertEqual(fake.gets, [])
        clock.now = shown
        service.get_event("s", ALICE)
        self.assertEqual(fake.gets, ["s"])

    def test_one_off_ending_soon_expires_at_its_end(self) -> None:
        fake = _Scripted({"o": _one_off("o", 30 / 86400 - 0.01)})
        service, clock = _cached_service(fake)
        service.get_event("o", ALICE)

        clock.now += 40
        fake.gets.clear()
        service.get_event("o", ALICE)

        self.assertEqual(fake.gets, ["o"])

    def test_end_already_past_falls_back_to_the_ttl(self) -> None:
        # Google can call an all-day event upcoming after midnight UTC. Expiring
        # at its end would mean no caching for the rest of that day.
        fake = _Scripted(
            {"s": _series("s")},
            {"s@google.com": [_occ("s", -1)]},
        )
        service, clock = _cached_service(fake)

        service.get_event("s", ALICE)
        clock.now += 30
        service.get_event("s", ALICE)

        self.assertEqual(fake.gets, ["s"])


class GoneOutcomeTests(unittest.TestCase):
    def test_expired_and_deleted_are_remembered_with_their_status(self) -> None:
        def fail(event_id: str) -> None:
            if event_id == "deleted":
                raise _http_error(404)

        fake = _Scripted(
            {"deleted": {}, "ended": _one_off("ended", -1)}, on_get=fail
        )
        service, _clock = _cached_service(fake)

        for _ in range(2):
            entries = service.get_events(["deleted", "ended"], ALICE)
            self.assertEqual([e.status for e in entries], ["unavailable", "expired"])

        self.assertEqual(sorted(fake.gets), ["deleted", "ended"])

    def test_cached_404_keeps_its_message_for_the_single_event_route(self) -> None:
        fake = _Scripted({"ended": _one_off("ended", -1)})
        service, _clock = _cached_service(fake)
        raised = []

        for _ in range(2):
            with self.assertRaises(CalendarEventGoneError) as caught:
                service.get_event("ended", ALICE)
            self.assertEqual(caught.exception.status_code, 404)
            self.assertEqual(caught.exception.detail, "This event has expired.")
            raised.append(caught.exception)

        self.assertIsNot(raised[0], raised[1])

    def test_google_failures_are_never_stored(self) -> None:
        failures = [_http_error(429), _http_error(500), _http_error(403, "forbiddenForNonOrganizer")]

        def fail(_event_id: str) -> None:
            if failures:
                raise failures.pop(0)

        fake = _Scripted({"o": _one_off("o")}, on_get=fail)
        service, _clock = _cached_service(fake)

        for expected in (503, 503, 502):
            with self.assertLogs(calendar_service.logger, "WARNING") if expected == 503 else _noop():
                with self.assertRaises(HTTPException) as caught:
                    service.get_event("o", ALICE)
            self.assertEqual(caught.exception.status_code, expected)

        self.assertEqual(service.get_event("o", ALICE).id, "o")
        self.assertEqual(len(fake.gets), 4)


class _noop:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> bool:
        return False


class AttendanceTests(unittest.TestCase):
    def test_rsvp_shows_at_once_for_the_user_and_everyone_else(self) -> None:
        fake = _Scripted({"o": _one_off("o", attendees=[{"email": BOB}])})
        service, _clock = _cached_service(fake)
        self.assertFalse(service.get_event("o", ALICE).attending)

        self.assertTrue(service.set_attendance("o", ALICE, True))

        self.assertTrue(service.get_event("o", ALICE).attending)
        self.assertTrue(service.get_event("o", BOB).attending)

    def test_removal_shows_at_once(self) -> None:
        fake = _Scripted({"o": _one_off("o", attendees=[{"email": ALICE}, {"email": BOB}])})
        service, _clock = _cached_service(fake)
        self.assertTrue(service.get_event("o", ALICE).attending)

        self.assertFalse(service.set_attendance("o", ALICE, False))

        self.assertFalse(service.get_event("o", ALICE).attending)
        self.assertTrue(service.get_event("o", BOB).attending)

    def test_rsvp_updates_every_id_that_resolves_to_the_series(self) -> None:
        # A widget can hold an occurrence id or the ended half of a split.
        fake = _Scripted(
            {"s_20270101": {"id": "s_20270101", "recurringEventId": "s"}, "s": _series("s")},
            {"s@google.com": [_occ("s", 1)]},
        )
        service, _clock = _cached_service(fake)
        service.get_event("s", ALICE)
        service.get_event("s_20270101", ALICE)

        service.set_attendance("s", ALICE, True)

        self.assertTrue(service.get_event("s", ALICE).attending)
        self.assertTrue(service.get_event("s_20270101", ALICE).attending)

    def test_rsvp_keeps_the_shown_occurrence(self) -> None:
        fake = _Scripted({"s": _series("s")}, {"s@google.com": [_occ("s", 1)]})
        service, _clock = _cached_service(fake)
        before = service.get_event("s", ALICE)

        service.set_attendance("s", ALICE, True)

        self.assertEqual(service.get_event("s", ALICE).start, before.start)

    def test_rsvp_that_changes_nothing_still_refreshes_the_guest_list(self) -> None:
        # Added in Google after the entry was stored, then clicked in the Hub.
        fake = _Scripted({"o": _one_off("o")})
        service, _clock = _cached_service(fake)
        self.assertFalse(service.get_event("o", ALICE).attending)
        fake.events["o"]["attendees"] = [{"email": ALICE}]

        self.assertTrue(service.set_attendance("o", ALICE, True))

        self.assertEqual(fake.patched, [])
        self.assertTrue(service.get_event("o", ALICE).attending)

    def test_rsvp_acts_on_google_not_the_cache(self) -> None:
        fake = _Scripted({"o": _one_off("o")})
        service, _clock = _cached_service(fake)
        service.get_event("o", ALICE)
        fake.gets.clear()

        service.set_attendance("o", ALICE, True)

        self.assertGreaterEqual(len(fake.gets), 2)

    def test_lookup_running_during_an_rsvp_cannot_overwrite_it(self) -> None:
        fake = _Scripted({"o": _one_off("o")})
        service, _clock = _cached_service(fake)
        release = threading.Event()
        reading = threading.Event()
        old = {**fake.events["o"]}

        def slow_read() -> tuple[str, dict[str, Any], None]:
            reading.set()
            release.wait(5)
            return "o", old, None

        reader = threading.Thread(
            target=lambda: service._cache.get_or_load("o", slow_read)  # type: ignore[attr-defined]
        )
        reader.start()
        reading.wait(5)
        service.set_attendance("o", ALICE, True)
        fake.events["o"]["attendees"] = [{"email": ALICE}]  # Google now has it.
        release.set()
        reader.join(5)

        # The older read wasn't stored, so this one asks Google.
        self.assertTrue(service.get_event("o", ALICE).attending)


class ArrivingTogetherTests(unittest.TestCase):
    def _run_together(self, count: int, target: Callable[[], Any]) -> list[Any]:
        results: list[Any] = []
        lock = threading.Lock()

        def run() -> None:
            try:
                outcome: Any = target()
            except Exception as exc:  # noqa: BLE001
                outcome = exc
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        return results

    def test_viewers_arriving_together_share_one_lookup(self) -> None:
        entered = threading.Event()

        def hold(_event_id: str) -> None:
            entered.set()
            time.sleep(0.2)

        fake = _Scripted({"o": _one_off("o")}, on_get=hold)
        service, _clock = _cached_service(fake)

        results = self._run_together(8, lambda: service.get_event("o", ALICE))

        self.assertEqual(fake.gets, ["o"])
        self.assertEqual([r.id for r in results], ["o"] * 8)

    def test_waiting_viewers_share_the_failure_and_the_next_lookup_retries(self) -> None:
        failing = [True]

        def hold(_event_id: str) -> None:
            time.sleep(0.2)
            if failing[0]:
                raise _http_error(429)

        fake = _Scripted({"o": _one_off("o")}, on_get=hold)
        service, _clock = _cached_service(fake)

        with self.assertLogs(calendar_service.logger, "WARNING"):
            results = self._run_together(6, lambda: service.get_event("o", ALICE))

        self.assertEqual(fake.gets, ["o"])
        self.assertEqual([r.status_code for r in results], [503] * 6)

        failing[0] = False
        self.assertEqual(service.get_event("o", ALICE).id, "o")

    def test_waiting_viewers_get_a_404_with_its_status(self) -> None:
        def hold(_event_id: str) -> None:
            time.sleep(0.2)

        fake = _Scripted({"ended": _one_off("ended", -1)}, on_get=hold)
        service, _clock = _cached_service(fake)

        results = self._run_together(4, lambda: service.get_event("ended", ALICE))

        self.assertEqual(fake.gets, ["ended"])
        self.assertEqual([r.gone_status for r in results], ["expired"] * 4)


class BatchTests(unittest.TestCase):
    def test_batch_and_single_route_share_entries(self) -> None:
        fake = _Scripted({"a": _one_off("a"), "b": _one_off("b")})
        service, _clock = _cached_service(fake)

        service.get_event("a", ALICE)
        service.get_events(["a", "b"], ALICE)
        service.get_events(["a", "b"], ALICE)

        self.assertEqual(sorted(fake.gets), ["a", "b"])

    def test_overlapping_batches_cost_one_lookup_per_id(self) -> None:
        def hold(_event_id: str) -> None:
            time.sleep(0.05)

        ids = [f"e{i}" for i in range(10)]
        fake = _Scripted({i: _one_off(i) for i in ids}, on_get=hold)
        service, _clock = _cached_service(fake)
        results: list[Any] = []

        def viewer() -> None:
            results.append(service.get_events(ids, ALICE))

        threads = [threading.Thread(target=viewer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(sorted(fake.gets), sorted(ids))
        for entries in results:
            self.assertEqual([e.status for e in entries], ["ok"] * 10)

    def test_cached_ids_skip_the_pool_so_they_cannot_queue(self) -> None:
        fake = _Scripted({"a": _one_off("a"), "b": _one_off("b")})
        service, _clock = _cached_service(fake)
        service.get_events(["a", "b"], ALICE)

        with patch.object(calendar_service, "_BATCH_POOL") as pool:
            pool.map.side_effect = lambda fn, ids: [fn(i) for i in ids]
            entries = service.get_events(["b", "a"], ALICE)

        self.assertEqual(pool.map.call_args.args[1], [])
        self.assertEqual([e.requested_id for e in entries], ["b", "a"])
        self.assertEqual([e.status for e in entries], ["ok", "ok"])

    def test_cached_id_is_still_answered_when_google_is_busy(self) -> None:
        def fail(event_id: str) -> None:
            if event_id != "cached":
                raise _http_error(429)

        fake = _Scripted(
            {"cached": _one_off("cached"), "new": _one_off("new")}, on_get=fail
        )
        service, _clock = _cached_service(fake)
        service.get_event("cached", ALICE)

        with self.assertLogs(calendar_service.logger, "WARNING"):
            entries = service.get_events(["new", "cached"], ALICE)

        self.assertEqual([e.status for e in entries], ["error", "ok"])


class SizeTests(unittest.TestCase):
    def _resolved(self, event_id: str) -> Callable[[], Any]:
        return lambda: (event_id, _one_off(event_id), None)

    def test_size_is_capped_by_dropping_the_oldest(self) -> None:
        cache = _EventCache(max_entries=2)
        for event_id in ("a", "b", "c"):
            cache.get_or_load(event_id, self._resolved(event_id))

        self.assertEqual([cache.has(i) for i in ("a", "b", "c")], [False, True, True])

    def test_expired_entries_are_dropped_when_storing(self) -> None:
        clock = _Clock()
        cache = _EventCache(clock=clock)
        cache.get_or_load("a", self._resolved("a"))
        clock.now += 61
        cache.get_or_load("b", self._resolved("b"))

        self.assertEqual(list(cache._entries), ["b"])


if __name__ == "__main__":
    unittest.main()
