"""
Google Calendar service for the shared events calendar.

Acts *as* the events account (GOOGLE_CALENDAR_ID) via a stored refresh token,
backing three operations for the calendar widget:

  * ``search_events``  — edit-time picker: upcoming events whose title matches.
  * ``get_event``      — view-time: resolve a stored id to its next occurrence,
                         plus whether the requesting user is on the guest list.
  * ``set_attendance`` — RSVP toggle: add / remove the user on the event's
                         guest list, on the whole series for recurring events.

All writes are read-modify-write on the full ``attendees`` array guarded by an
``If-Match`` ETag precondition with retry, so two viewers clicking at the same
moment cannot silently drop each other from the list.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException
from googleapiclient.errors import HttpError

from app.config import Settings
from app.helpers.exceptions import (
    CalendarEventGoneError,
    GoogleCalendarBusyError,
    GoogleCalendarError,
)
from app.helpers.google_client import build_calendar_service
from app.models.calendar import (
    CalendarBatchEntry,
    CalendarEvent,
    CalendarIndexEvent,
    CalendarSearchResult,
)

logger = logging.getLogger(__name__)

MAX_BATCH_EVENTS = 20
# Shared by every batch in this process, so the limit holds however many viewers
# load at once, and its threads keep their Google connections open between
# batches (a new connection costs a few tenths of a second). Threads start on demand.
_BATCH_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="calendar-batch")

# Google's `q` param is full-text (matches description, location, attendees),
# so results are additionally filtered to a case-insensitive SUMMARY substring
# to honour "title match". This caps how many upcoming instances we scan.
_SEARCH_SCAN = 100
_SEARCH_RETURN = 25
# Optimistic-concurrency retries on a 412 (ETag precondition failed).
_ATTENDANCE_MAX_ATTEMPTS = 4
# Retries on Google rate limits and 5xx, done by the client's execute() with
# exponential backoff and random jitter. Each retry also logs a "Sleeping ...
# before retry" warning from googleapiclient.http, the only trace of a rate
# limit that recovers. A user is waiting on the RSVP button, so its patch
# retries less; the If-Match ETag keeps that retry safe.
_READ_RETRIES = 3
_WRITE_RETRIES = 1
# Event cache (plan_calendar_carousel_2026-11-01.md §4b). Ids come from signed-in
# callers, so the size is capped as well as the age.
_EVENT_CACHE_TTL_SECONDS = 60
_EVENT_CACHE_MAX_ENTRIES = 2000


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def _google_error(exc: HttpError, action: str) -> HTTPException:
    """Translate a Google failure that retries couldn't get past.

    The whole org shares one quota, so rate limits are logged separately: with the
    event cache in place, they are the signal to reconsider a request-counting
    queue (plan_calendar_carousel_2026-11-01.md §0 #6).
    """
    code = exc.resp.status if exc.resp is not None else 0
    rate_limited = code == 429 or (
        code == 403 and b"ratelimitexceeded" in (exc.content or b"").lower()
    )
    if rate_limited:
        logger.warning(
            "Google Calendar rate limit: %s failed after retries (%s %s).",
            action, code, exc.reason,
        )
        return GoogleCalendarBusyError()
    if code >= 500:
        logger.warning(
            "Google Calendar unavailable: %s failed after retries (%s %s).",
            action, code, exc.reason,
        )
        return GoogleCalendarBusyError()
    return GoogleCalendarError(f"{action} failed: {exc}")


def _start_str(node: dict[str, Any] | None) -> str | None:
    """Extract an ISO string from a Calendar start/end node (date or dateTime)."""
    if not node:
        return None
    return node.get("dateTime") or node.get("date")


def _is_all_day(node: dict[str, Any] | None) -> bool:
    return bool(node) and "date" in node and "dateTime" not in node


def _event_has_ended(
    start_node: dict[str, Any] | None,
    end_node: dict[str, Any] | None,
) -> bool:
    """Return True once a one-off event is fully in the past.

    Missing or malformed timestamps fail open here; the live Calendar API
    remains the source of truth and callers can still render the event rather
    than deleting it on a parsing accident.
    """
    ends_at = _end_instant(start_node, end_node)
    return ends_at is not None and ends_at <= datetime.now(timezone.utc)


def _end_instant(
    start_node: dict[str, Any] | None,
    end_node: dict[str, Any] | None,
) -> datetime | None:
    """When an event ends in UTC, or None if its timestamps can't be read.

    Google all-day event end dates are exclusive, so an all-day event ends at
    midnight UTC at the start of that date.
    """
    raw = _start_str(end_node) or _start_str(start_node)
    if not raw:
        return None

    try:
        if "T" not in raw:
            day = date.fromisoformat(raw)
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        logger.warning("Could not parse Calendar end timestamp %r", raw)
        return None


def _start_key(event: dict[str, Any]) -> datetime:
    """Sortable UTC start of an event; all-day dates count from midnight UTC."""
    raw = _start_str(event.get("start")) or ""
    try:
        if "T" not in raw:
            day = date.fromisoformat(raw)
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)


def _meeting_from(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Return ``(join_url, provider_label)`` for the event's video conference.

    Prefers structured ``conferenceData`` — populated for Zoom, Meet, Teams and
    Webex added through Google's conferencing UI (e.g. the Zoom for Google
    Workspace add-on) — and picks the ``video`` entry point. Falls back to the
    Meet-only ``hangoutLink`` field. Returns ``(None, None)`` when the event has
    no attached conference.
    """
    conf = event.get("conferenceData") or {}
    label = (conf.get("conferenceSolution") or {}).get("name")
    for entry in conf.get("entryPoints", []) or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"], label
    hangout = event.get("hangoutLink")
    if hangout:
        return hangout, label or "Google Meet"
    return None, None


# (series_id, master, next_instance): see CalendarService._resolve.
_Resolved = tuple[str, dict[str, Any], "dict[str, Any] | None"]


@dataclass
class _CachedLookup:
    """One remembered ``_resolve`` outcome: a result, or the 404 it raised."""

    expires_at: float
    resolved: _Resolved | None = None
    gone_status: str | None = None
    gone_detail: str = ""

    def outcome(self) -> _Resolved:
        if self.resolved is None:
            # A new exception each time: threads share this entry.
            raise CalendarEventGoneError(self.gone_status or "unavailable", self.gone_detail)
        return self.resolved


class _EventCache:
    """Short-lived ``_resolve`` outcomes, keyed by the requested event id.

    Every Google call runs as one account, so the whole org shares its quota.
    Google failures are never stored, and callers must not mutate what they get.

    * Concurrent misses for one id share a single lookup, so viewers arriving
      together cost Google one lookup rather than one each.
    * An entry never outlives the occurrence it shows. The widget refetches just
      after an event ends and doesn't ask again for up to 15 minutes, so a stored
      answer at that moment would leave the ended event on screen.
    * ``update_master`` detaches lookups already running. They may have read the
      guest list before an RSVP, and must not store it over the newer one.
    """

    def __init__(
        self,
        ttl_seconds: float = _EVENT_CACHE_TTL_SECONDS,
        max_entries: int = _EVENT_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _CachedLookup] = {}
        self._running: dict[str, Future[_CachedLookup]] = {}

    def has(self, event_id: str) -> bool:
        with self._lock:
            return self._live(event_id) is not None

    def get_or_load(self, event_id: str, load: Callable[[], _Resolved]) -> _Resolved:
        with self._lock:
            entry = self._live(event_id)
            if entry is not None:
                return entry.outcome()
            running = self._running.get(event_id)
            if running is None:
                mine: Future[_CachedLookup] = Future()
                self._running[event_id] = mine
        if running is not None:
            return running.result().outcome()

        try:
            entry = self._entry_from(load)
        except BaseException as exc:
            self._finish(event_id, mine, None)
            mine.set_exception(exc)
            raise
        self._finish(event_id, mine, entry)
        mine.set_result(entry)
        return entry.outcome()

    def update_master(self, series_id: str, master: dict[str, Any]) -> None:
        """Put a just-written or just-read series into every entry showing it."""
        with self._lock:
            for entry in self._entries.values():
                if entry.resolved is not None and entry.resolved[0] == series_id:
                    entry.resolved = (series_id, master, entry.resolved[2])
            self._running.clear()

    def _live(self, event_id: str) -> _CachedLookup | None:
        entry = self._entries.get(event_id)
        if entry is None or entry.expires_at <= self._clock():
            return None
        return entry

    def _entry_from(self, load: Callable[[], _Resolved]) -> _CachedLookup:
        try:
            resolved = load()
        except CalendarEventGoneError as exc:
            return _CachedLookup(
                expires_at=self._clock() + self._ttl,
                gone_status=exc.gone_status,
                gone_detail=str(exc.detail),
            )
        return _CachedLookup(expires_at=self._expiry_of(resolved), resolved=resolved)

    def _expiry_of(self, resolved: _Resolved) -> float:
        now = self._clock()
        expires_at = now + self._ttl
        _series_id, master, instance = resolved
        shown = instance or master
        ends_at = _end_instant(shown.get("start"), shown.get("end"))
        if ends_at is not None:
            end = ends_at.timestamp()
            # An end already past means Google still calls it upcoming (an
            # all-day event late in its day, or clock skew): use the plain TTL.
            if now < end < expires_at:
                return end
        return expires_at

    def _finish(
        self,
        event_id: str,
        future: Future[_CachedLookup],
        entry: _CachedLookup | None,
    ) -> None:
        with self._lock:
            if self._running.get(event_id) is not future:
                return  # Detached by update_master: this answer may be older.
            del self._running[event_id]
            if entry is None:
                return
            now = self._clock()
            for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
                del self._entries[key]
            self._entries.pop(event_id, None)
            while len(self._entries) >= self._max_entries:
                del self._entries[next(iter(self._entries))]  # Oldest stored.
            self._entries[event_id] = entry


class CalendarService:
    """Thin wrapper around the Google Calendar v3 API for one shared calendar."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service = build_calendar_service(settings)
        self._cache = _EventCache()

    @property
    def _calendar_id(self) -> str:
        cid = self._settings.GOOGLE_CALENDAR_ID
        if not cid:
            raise GoogleCalendarError(
                "GOOGLE_CALENDAR_ID is not configured on the server."
            )
        return cid

    # ── Edit-time: search upcoming events by title ─────────────────────
    def search_events(self, query: str) -> list[CalendarSearchResult]:
        """
        Return upcoming events whose title contains *query* (case-insensitive).

        Recurring events are collapsed to one entry keyed by their series
        master id, dated by their next upcoming occurrence — so the admin
        picks a series once and stores the id that attendance will patch.
        """
        query = query.strip()
        if not query:
            return []

        try:
            resp = (
                self._service.events()
                .list(
                    calendarId=self._calendar_id,
                    q=query,
                    timeMin=_now_rfc3339(),
                    singleEvents=True,  # expand recurrences to instances…
                    orderBy="startTime",
                    maxResults=_SEARCH_SCAN,
                    fields=(
                        "items(id,summary,start,recurringEventId,recurrence)"
                    ),
                )
                .execute(num_retries=_READ_RETRIES)
            )
        except HttpError as exc:
            raise _google_error(exc, "Calendar search") from exc

        needle = query.lower()
        results: list[CalendarSearchResult] = []
        seen: set[str] = set()

        for item in resp.get("items", []):
            summary = item.get("summary", "")
            if needle not in summary.lower():
                continue
            # …then collapse instances back to their series master id, so the
            # stored id patches the whole series (the admin's "whole series"
            # choice) and duplicates of a weekly event don't flood the picker.
            master_id = item.get("recurringEventId") or item["id"]
            if master_id in seen:
                continue
            seen.add(master_id)
            results.append(
                CalendarSearchResult(
                    id=master_id,
                    summary=summary,
                    start=_start_str(item.get("start")),
                    recurring=bool(item.get("recurringEventId")),
                )
            )
            if len(results) >= _SEARCH_RETURN:
                break

        return results

    # ── View-time: resolve a stored id to its next occurrence ──────────
    def get_event(self, event_id: str, viewer_email: str) -> CalendarEvent:
        """
        Resolve *event_id* to the nearest upcoming occurrence and report
        whether *viewer_email* is on the guest list.

        Series-level details (summary, location, links, attendees) come from
        the master; for a recurring event the start/end are taken from its
        next instance. The returned ``id`` is the series actually shown, which
        differs from *event_id* once a split series has been followed (see
        ``_resolve``) — the widget RSVPs against it, so attendance lands on the
        live series. Raises 404 if the event is gone, a one-off has ended, or
        the series (and any continuation of it) has no future occurrences.

        Served from the event cache, so Google-side edits can take up to a minute
        to show. ``attending`` is worked out per viewer from the cached guest list.
        """
        series_id, master, instance = self._resolve_cached(event_id)

        recurring = instance is not None
        start_node = (instance or master).get("start")
        end_node = (instance or master).get("end")

        attendees = master.get("attendees", []) or []
        attending = any(
            (a.get("email", "").lower() == viewer_email.lower()) for a in attendees
        )

        meeting_link, meeting_label = _meeting_from(master)

        return CalendarEvent(
            id=series_id,
            summary=master.get("summary", ""),
            start=_start_str(start_node),
            end=_start_str(end_node),
            is_all_day=_is_all_day(start_node),
            location=master.get("location"),
            description=master.get("description"),
            html_link=master.get("htmlLink"),
            meeting_link=meeting_link,
            meeting_label=meeting_label,
            recurring=recurring,
            attending=attending,
        )

    def get_events(
        self, event_ids: list[str], viewer_email: str
    ) -> list[CalendarBatchEntry]:
        """
        Run ``get_event`` for each id and return one entry per id, in request
        order. A failure only ever affects its own entry.

        Once one lookup finds Google busy, retries have already run out, so the
        ids not yet started report the same error without calling Google. That
        eases the shared quota and stops a rate limit from costing every id its
        own backoff sleeps. Cached ids are still answered.

        Cached ids are answered on this thread rather than through the pool, so
        they don't queue behind other viewers' lookups.
        """
        busy = threading.Event()

        def lookup(event_id: str) -> CalendarBatchEntry:
            if not event_id.strip():
                # A blank id would address Google's list endpoint, not an event.
                return CalendarBatchEntry(requested_id=event_id, status="unavailable")
            if busy.is_set() and not self._cache.has(event_id):
                return CalendarBatchEntry(
                    requested_id=event_id,
                    status="error",
                    detail=GoogleCalendarBusyError().detail,
                )
            try:
                return CalendarBatchEntry(
                    requested_id=event_id,
                    status="ok",
                    event=self.get_event(event_id, viewer_email),
                )
            except CalendarEventGoneError as exc:
                return CalendarBatchEntry(requested_id=event_id, status=exc.gone_status)
            except HTTPException as exc:
                if isinstance(exc, GoogleCalendarBusyError):
                    busy.set()
                return CalendarBatchEntry(
                    requested_id=event_id, status="error", detail=str(exc.detail)
                )
            except Exception:  # e.g. network failures that outlast the retries
                logger.exception("Calendar batch lookup failed for %s", event_id)
                return CalendarBatchEntry(
                    requested_id=event_id,
                    status="error",
                    detail="Could not load this event.",
                )

        unique_ids = list(dict.fromkeys(event_ids))
        cached_ids = [event_id for event_id in unique_ids if self._cache.has(event_id)]
        missing_ids = [event_id for event_id in unique_ids if event_id not in cached_ids]
        entries = {event_id: lookup(event_id) for event_id in cached_ids}
        entries.update(zip(missing_ids, _BATCH_POOL.map(lookup, missing_ids)))
        return [entries[event_id] for event_id in event_ids]

    def get_index_event(self, event_id: str) -> CalendarIndexEvent:
        """Return a viewer-independent upcoming snapshot for search ingestion.

        Attendees and RSVP state are intentionally never read into the returned
        model. A deleted/cancelled event, an expired one-off event, or a recurring
        series with no future instance returns 404 so the Agent can remove stale
        vectors instead of indexing cached widget metadata. Shares the event
        cache, so this can be up to a minute behind Google.
        """
        if not event_id.strip():
            # A blank id would address Google's list endpoint, not an event.
            raise CalendarEventGoneError(
                "unavailable", "This event is no longer available."
            )

        _series_id, master, instance = self._resolve_cached(event_id)

        recurring = instance is not None
        start_node = (instance or master).get("start")
        end_node = (instance or master).get("end")

        meeting_link, meeting_label = _meeting_from(master)
        return CalendarIndexEvent(
            # Keyed by the stored id, not the followed series, so the Agent's
            # vectors stay attached to the widget that references them.
            id=event_id,
            summary=master.get("summary", ""),
            start=_start_str(start_node),
            end=_start_str(end_node),
            is_all_day=_is_all_day(start_node),
            location=master.get("location"),
            description=master.get("description"),
            html_link=master.get("htmlLink"),
            meeting_link=meeting_link,
            meeting_label=meeting_label,
            recurring=recurring,
        )

    # ── RSVP toggle ────────────────────────────────────────────────────
    def set_attendance(self, event_id: str, email: str, attending: bool) -> bool:
        """
        Add or remove *email* on the event's guest list and return the
        resulting attendance state.

        Read-modify-write on the full attendee array, guarded by an If-Match
        ETag precondition and retried on 412 so simultaneous clicks cannot lose
        an attendee. No-ops (and sends no email) when already in the desired
        state, so re-clicking is harmless.

        Reads Google directly rather than the event cache, because a write should
        act on the live event, then puts the result in the cache so every viewer
        sees the change straight away.
        """
        email_lower = email.lower()
        # Patch the series the widget is actually showing: a stale client may
        # still hold an occurrence id or the ended half of a split series.
        event_id, _master, _instance = self._resolve(event_id)

        for attempt in range(_ATTENDANCE_MAX_ATTEMPTS):
            event = self._get_raw(event_id)
            etag = event.get("etag")
            attendees: list[dict[str, Any]] = list(event.get("attendees", []) or [])
            present = any(a.get("email", "").lower() == email_lower for a in attendees)

            if present == attending:
                self._cache.update_master(event_id, event)
                return attending

            if attending:
                attendees.append({"email": email})
            else:
                attendees = [
                    a for a in attendees if a.get("email", "").lower() != email_lower
                ]

            request = self._service.events().patch(
                calendarId=self._calendar_id,
                eventId=event_id,
                body={"attendees": attendees},
                sendUpdates=self._settings.GOOGLE_CALENDAR_SEND_UPDATES,
            )
            # google-api-python-client sends this as the If-Match header, making
            # the patch fail with 412 if another writer changed the event first.
            if etag:
                request.headers["If-Match"] = etag

            try:
                patched = request.execute(num_retries=_WRITE_RETRIES)
                self._cache.update_master(
                    event_id, {**event, "attendees": attendees, **patched}
                )
                return attending
            except HttpError as exc:
                if exc.resp is not None and exc.resp.status == 412:
                    logger.info(
                        "Attendee patch race on %s (attempt %d), retrying.",
                        event_id, attempt + 1,
                    )
                    continue
                raise _google_error(exc, "Attendance update") from exc

        raise GoogleCalendarError(
            "Could not update attendance after multiple attempts. Please retry."
        )

    # ── Internal helpers ───────────────────────────────────────────────
    def _get_raw(self, event_id: str) -> dict[str, Any]:
        """Fetch a raw event, translating a 404 into a clean not-found error."""
        try:
            return (
                self._service.events()
                .get(calendarId=self._calendar_id, eventId=event_id)
                .execute(num_retries=_READ_RETRIES)
            )
        except HttpError as exc:
            if exc.resp is not None and exc.resp.status == 404:
                raise CalendarEventGoneError(
                    "unavailable", "Event not found. It may have been deleted."
                ) from exc
            raise _google_error(exc, "Event read") from exc

    def _resolve_cached(self, event_id: str) -> _Resolved:
        """``_resolve`` through the event cache."""
        return self._cache.get_or_load(event_id, lambda: self._resolve(event_id))

    def _resolve(
        self, event_id: str
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """
        Map a stored event id to what the widget should show right now.

        Returns ``(series_id, master, next_instance)`` — ``next_instance`` is
        None for a one-off event. Handles three ways a stored id goes stale:

        * an occurrence id (``<series>_<timestamp>``) is swapped for its
          series, so it advances instead of pinning one date forever;
        * a one-off that is cancelled or over raises 404;
        * a series with no future occurrences — typically the ended half of a
          "This and following events" edit — is replaced by its continuation
          (``_find_continuation``), else raises 404.
        """
        master = self._get_raw(event_id)
        parent_id = master.get("recurringEventId")
        if parent_id:
            event_id, master = parent_id, self._get_raw(parent_id)

        if not master.get("recurrence"):
            if str(master.get("status") or "").lower() == "cancelled":
                raise CalendarEventGoneError(
                    "unavailable", "This event is no longer available."
                )
            if _event_has_ended(master.get("start"), master.get("end")):
                raise CalendarEventGoneError("expired", "This event has expired.")
            return event_id, master, None

        cancelled = str(master.get("status") or "").lower() == "cancelled"
        instance = None if cancelled else self._next_instance(master)
        if instance is not None:
            return event_id, master, instance

        continuation = self._find_continuation(master)
        if continuation is None:
            raise CalendarEventGoneError(
                "unavailable" if cancelled else "expired",
                "This event has no upcoming occurrences.",
            )
        successor, instance = continuation
        logger.info(
            "Calendar series %s has ended; following continuation %s.",
            event_id, successor["id"],
        )
        return successor["id"], self._get_raw(successor["id"]), instance

    def _find_continuation(
        self, ended: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """
        Find the live series that continues *ended* after a split, with its
        next instance, or None.

        Google records no link between the two halves of a "This and following
        events" edit, but leaves two fingerprints, checked against every
        upcoming recurring series:

        * events with guests get the id ``<original>_R<start>`` — a later split
          of that half reuses the same ``<original>`` prefix;
        * the continuation copies the original's ``created`` timestamp and
          organizer (this is the only trace for guest-less events, which get an
          unrelated id; Google strips ``created`` from a cancelled original, so
          the prefix test still matters).

        Several halves can match after repeated splits; the one whose next
        occurrence comes soonest is the one currently running.
        """
        base = ended["id"].split("_R", 1)[0]
        created = ended.get("created")
        organizer = (ended.get("organizer") or {}).get("email")

        best: tuple[dict[str, Any], dict[str, Any]] | None = None
        for series in self._upcoming_series():
            if series["id"] == ended["id"]:
                continue
            same_lineage = series["id"].startswith(f"{base}_R") or (
                created is not None
                and series.get("created") == created
                and (series.get("organizer") or {}).get("email") == organizer
            )
            if not same_lineage:
                continue
            instance = self._next_instance(series)
            if instance is None:
                continue
            if best is None or _start_key(instance) < _start_key(best[1]):
                best = (series, instance)
        return best

    def _upcoming_series(self) -> list[dict[str, Any]]:
        """Every live recurring series on the calendar with a future occurrence."""
        series: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            try:
                resp = (
                    self._service.events()
                    .list(
                        calendarId=self._calendar_id,
                        singleEvents=False,
                        timeMin=_now_rfc3339(),
                        maxResults=250,
                        pageToken=page_token,
                        fields=(
                            "nextPageToken,"
                            "items(id,iCalUID,status,recurrence,created,organizer)"
                        ),
                    )
                    .execute(num_retries=_READ_RETRIES)
                )
            except HttpError as exc:
                raise _google_error(exc, "Calendar series list") from exc
            series.extend(
                item
                for item in resp.get("items", [])
                if item.get("recurrence")
                and str(item.get("status") or "").lower() != "cancelled"
            )
            page_token = resp.get("nextPageToken")
            if not page_token:
                return series

    def _next_instance(self, master: dict[str, Any]) -> dict[str, Any] | None:
        """Return the next upcoming instance of a recurring series, or None.

        Uses ``events.list`` by iCalUID with ``orderBy=startTime`` rather than
        ``events.instances``: the latter's order is undocumented and in practice
        scrambled for some series, so its first item can be weeks away. Pages
        are followed because Google may return an empty page with a
        ``nextPageToken``. ``timeMin`` filters on *end* time, so a meeting in
        progress is still "next" until it finishes.
        """
        page_token: str | None = None
        while True:
            try:
                resp = (
                    self._service.events()
                    .list(
                        calendarId=self._calendar_id,
                        iCalUID=master["iCalUID"],
                        singleEvents=True,
                        orderBy="startTime",
                        timeMin=_now_rfc3339(),
                        maxResults=5,
                        pageToken=page_token,
                        fields="nextPageToken,items(id,start,end)",
                    )
                    .execute(num_retries=_READ_RETRIES)
                )
            except HttpError as exc:
                raise _google_error(exc, "Event occurrences read") from exc
            items = resp.get("items", [])
            if items:
                return items[0]
            page_token = resp.get("nextPageToken")
            if not page_token:
                return None
