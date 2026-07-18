from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from icalendar import Calendar as ICalendar
import recurring_ical_events

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_current_user
from app.helpers.http_client import get_http_client
from app.models.auth import UserInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Data"])


@router.get(
    "/calendar/events",
    summary="Find the nearest upcoming event matching a query in an iCal feed",
    description=(
        "Fetches an iCal URL server-side (bypassing browser CORS restrictions), "
        "expands recurring events up to one year ahead, and returns the single "
        "nearest upcoming VEVENT whose SUMMARY contains `query` as a "
        "case-insensitive substring. Returns 404 if no match is found."
    ),
)
async def get_calendar_event(
    url: str = Query(..., description="iCal subscription URL to fetch."),
    query: str = Query(..., description="Event title search term (case-insensitive substring)."),
    _user: UserInfo = Depends(get_current_user),
):
    if not url.strip():
        raise HTTPException(status_code=400, detail="url must not be empty.")
    if not query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty.")

    # webcal:// is a URI scheme used by calendar apps; the underlying transport
    # is plain HTTPS — convert before fetching.
    fetch_url = url.strip()
    if fetch_url.lower().startswith("webcal://"):
        fetch_url = "https://" + fetch_url[len("webcal://"):]

    client = get_http_client()
    try:
        resp = await client.get(
            fetch_url,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RenPhilHub/1.0; +calendar-widget)"},
        )
        if resp.status_code == 401:
            raise HTTPException(
                status_code=502,
                detail=(
                    "The calendar server rejected the request (401 Unauthorized). "
                    "Make sure you are using the correct iCal subscription URL. "
                    "For Google Calendar, use the 'Secret address in iCal format' "
                    "(Settings → your calendar → Integrate calendar) and ensure the "
                    "calendar's external sharing is not disabled by your organization."
                ),
            )
        resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not fetch calendar URL: {exc}"
        ) from exc

    try:
        cal = ICalendar.from_ical(resp.content)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid iCal data: {exc}"
        ) from exc

    now_naive = datetime.utcnow()
    search_end = now_naive + timedelta(days=365)
    query_lower = query.strip().lower()

    try:
        all_events = recurring_ical_events.of(cal).between(now_naive, search_end)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Failed to read calendar events: {exc}"
        ) from exc

    best = None
    best_start: datetime | None = None

    for event in all_events:
        summary = str(event.get("SUMMARY", ""))
        if query_lower not in summary.lower():
            continue

        dtstart_prop = event.get("DTSTART")
        if dtstart_prop is None:
            continue

        raw = dtstart_prop.dt
        if isinstance(raw, datetime):
            start_dt = raw.replace(tzinfo=None) if raw.tzinfo else raw
        elif isinstance(raw, date):
            start_dt = datetime(raw.year, raw.month, raw.day)
        else:
            continue

        if start_dt < now_naive:
            continue

        if best_start is None or start_dt < best_start:
            best = event
            best_start = start_dt

    if best is None:
        raise HTTPException(
            status_code=404,
            detail="No upcoming event found matching the search term.",
        )

    dtstart_prop = best.get("DTSTART")
    dtend_prop = best.get("DTEND")
    raw_start = dtstart_prop.dt
    is_all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)

    if is_all_day:
        start_str: str = raw_start.isoformat()
        end_raw = dtend_prop.dt if dtend_prop else None
        end_str: str | None = end_raw.isoformat() if end_raw else None
    else:
        if isinstance(raw_start, datetime):
            if raw_start.tzinfo is not None:
                raw_start = raw_start.astimezone(timezone.utc)
            start_str = raw_start.isoformat()
        else:
            start_str = raw_start.isoformat()

        end_str = None
        if dtend_prop is not None:
            raw_end = dtend_prop.dt
            if isinstance(raw_end, datetime):
                if raw_end.tzinfo is not None:
                    raw_end = raw_end.astimezone(timezone.utc)
                end_str = raw_end.isoformat()
            elif isinstance(raw_end, date):
                end_str = raw_end.isoformat()

    return {
        "summary": str(best.get("SUMMARY", "")),
        "start": start_str,
        "end": end_str,
        "is_all_day": is_all_day,
    }
