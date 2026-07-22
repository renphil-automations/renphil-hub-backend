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
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from googleapiclient.errors import HttpError

from app.config import Settings
from app.helpers.exceptions import GoogleCalendarError
from app.helpers.google_client import build_calendar_service
from app.models.calendar import CalendarEvent, CalendarSearchResult

logger = logging.getLogger(__name__)

# Google's `q` param is full-text (matches description, location, attendees),
# so results are additionally filtered to a case-insensitive SUMMARY substring
# to honour "title match". This caps how many upcoming instances we scan.
_SEARCH_SCAN = 100
_SEARCH_RETURN = 25
# Optimistic-concurrency retries on a 412 (ETag precondition failed).
_ATTENDANCE_MAX_ATTEMPTS = 4


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_str(node: dict[str, Any] | None) -> str | None:
    """Extract an ISO string from a Calendar start/end node (date or dateTime)."""
    if not node:
        return None
    return node.get("dateTime") or node.get("date")


def _is_all_day(node: dict[str, Any] | None) -> bool:
    return bool(node) and "date" in node and "dateTime" not in node


class CalendarService:
    """Thin wrapper around the Google Calendar v3 API for one shared calendar."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service = build_calendar_service(settings)

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
                .execute()
            )
        except HttpError as exc:
            raise GoogleCalendarError(f"Calendar search failed: {exc}") from exc

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
        next instance. Raises 404 if the event is gone or the series has no
        future occurrences left.
        """
        master = self._get_raw(event_id)

        recurring = bool(master.get("recurrence"))
        start_node = master.get("start")
        end_node = master.get("end")

        if recurring:
            instance = self._next_instance(event_id)
            if instance is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="This event has no upcoming occurrences.",
                )
            # start/end come from the concrete next instance.
            start_node = instance.get("start")
            end_node = instance.get("end")

        attendees = master.get("attendees", []) or []
        attending = any(
            (a.get("email", "").lower() == viewer_email.lower()) for a in attendees
        )

        return CalendarEvent(
            id=event_id,
            summary=master.get("summary", ""),
            start=_start_str(start_node),
            end=_start_str(end_node),
            is_all_day=_is_all_day(start_node),
            location=master.get("location"),
            description=master.get("description"),
            html_link=master.get("htmlLink"),
            hangout_link=master.get("hangoutLink"),
            recurring=recurring,
            attending=attending,
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
        """
        email_lower = email.lower()

        for attempt in range(_ATTENDANCE_MAX_ATTEMPTS):
            event = self._get_raw(event_id)
            etag = event.get("etag")
            attendees: list[dict[str, Any]] = list(event.get("attendees", []) or [])
            present = any(a.get("email", "").lower() == email_lower for a in attendees)

            if attending and present:
                return True
            if not attending and not present:
                return False

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
                request.execute()
                return attending
            except HttpError as exc:
                if exc.resp is not None and exc.resp.status == 412:
                    logger.info(
                        "Attendee patch race on %s (attempt %d), retrying.",
                        event_id, attempt + 1,
                    )
                    continue
                raise GoogleCalendarError(f"Could not update attendance: {exc}") from exc

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
                .execute()
            )
        except HttpError as exc:
            if exc.resp is not None and exc.resp.status == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Event not found. It may have been deleted.",
                ) from exc
            raise GoogleCalendarError(f"Could not read event: {exc}") from exc

    def _next_instance(self, master_id: str) -> dict[str, Any] | None:
        """Return the next upcoming instance of a recurring series, or None."""
        try:
            resp = (
                self._service.events()
                .instances(
                    calendarId=self._calendar_id,
                    eventId=master_id,
                    timeMin=_now_rfc3339(),
                    maxResults=1,
                    fields="items(id,start,end)",
                )
                .execute()
            )
        except HttpError as exc:
            raise GoogleCalendarError(
                f"Could not read event occurrences: {exc}"
            ) from exc
        items = resp.get("items", [])
        return items[0] if items else None
