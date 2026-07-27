"""
Calendar router — the shared events calendar behind the dashboard widget.

All three endpoints act as the events account (GOOGLE_CALENDAR_ID) via the
CalendarService. The RSVP endpoint takes the attendee identity strictly from
the authenticated JWT, never from the request body, so a caller can only ever
add or remove *themselves*.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_calendar_service, get_current_user
from app.models.auth import UserInfo
from app.models.calendar import (
    AttendanceRequest,
    AttendanceResponse,
    CalendarEvent,
    CalendarSearchResult,
)
from app.services.calendar_service import CalendarService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data/calendar", tags=["Calendar"])


@router.get(
    "/search",
    response_model=list[CalendarSearchResult],
    summary="Search upcoming events by title (admin edit-time picker)",
)
async def search_events(
    query: str = Query(..., description="Case-insensitive title substring."),
    _user: UserInfo = Depends(get_current_user),
    service: CalendarService = Depends(get_calendar_service),
):
    """Return upcoming events whose title matches, for the widget editor."""
    return service.search_events(query)


@router.get(
    "/event",
    response_model=CalendarEvent,
    summary="Resolve a stored event to its next occurrence",
)
async def get_event(
    event_id: str = Query(..., description="Stored event / series master id."),
    user: UserInfo = Depends(get_current_user),
    service: CalendarService = Depends(get_calendar_service),
):
    """Return the nearest upcoming occurrence plus the caller's RSVP state."""
    return service.get_event(event_id, user.email)


@router.post(
    "/event/{event_id}/attendance",
    response_model=AttendanceResponse,
    summary="Add or remove yourself from an event",
)
async def set_attendance(
    event_id: str,
    body: AttendanceRequest,
    user: UserInfo = Depends(get_current_user),
    service: CalendarService = Depends(get_calendar_service),
):
    """RSVP the *authenticated* user on/off the event's guest list.

    The email comes from the verified JWT — the request body carries only the
    desired on/off state — so this can never act on anyone but the caller.
    """
    attending = service.set_attendance(event_id, user.email, body.attending)
    return AttendanceResponse(attending=attending)
