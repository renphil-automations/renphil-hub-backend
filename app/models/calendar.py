"""Pydantic schemas for the shared-calendar event widget."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarSearchResult(BaseModel):
    """One matching event/series shown in the admin's edit-time picker."""

    id: str = Field(
        description=(
            "Event id to store on the widget. For a recurring event this is "
            "the series master id, so attendance applies to the whole series."
        )
    )
    summary: str
    start: str | None = Field(
        default=None,
        description="ISO start of the next upcoming occurrence (date or dateTime).",
    )
    recurring: bool = False


class CalendarEvent(BaseModel):
    """The resolved next occurrence of a stored event, for the viewer."""

    id: str
    summary: str
    start: str | None = None
    end: str | None = None
    is_all_day: bool = False
    location: str | None = None
    description: str | None = None
    html_link: str | None = Field(
        default=None, description="Link back to the canonical event in Google Calendar."
    )
    meeting_link: str | None = Field(
        default=None,
        description=(
            "Video conferencing join URL, when the event has one. Sourced from "
            "conferenceData (Zoom / Meet / Teams / Webex added via Google's "
            "conferencing UI), falling back to the Google Meet hangoutLink."
        ),
    )
    meeting_label: str | None = Field(
        default=None,
        description="Conferencing provider name, e.g. 'Zoom' or 'Google Meet'.",
    )
    recurring: bool = False
    attending: bool = Field(
        description="Whether the requesting user is on the event's guest list."
    )


class AttendanceRequest(BaseModel):
    """Body of the RSVP toggle. The email is taken from the JWT, never here."""

    attending: bool


class AttendanceResponse(BaseModel):
    """Result of an RSVP toggle — the user's attendance after the change."""

    attending: bool
