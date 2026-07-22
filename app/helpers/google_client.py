"""
Factory functions for Google API clients.
- OAuth2 flow client (user-facing sign-in)
- Drive service client  (service-account, server-side)
"""

from __future__ import annotations

import json
import os

from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build, Resource
from google_auth_oauthlib.flow import Flow

from app.config import Settings

# Allow Google to return a superset of the requested scopes (e.g. previously
# granted drive scopes re-attached by Google) without raising a scope mismatch.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# Scopes required for each integration
OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Read/write events only — NOT the full `calendar` scope, which would also
# permit deleting calendars and rewriting sharing ACLs.
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def build_oauth_flow(settings: Settings) -> Flow:
    """Return a configured Google OAuth2 *web* flow."""
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=OAUTH_SCOPES)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


def build_drive_service(settings: Settings) -> Resource:
    """
    Return an authenticated Google Drive API v3 service using a service account.

    Credential resolution order:
      1. GOOGLE_SERVICE_ACCOUNT_JSON env var (raw JSON string) — used in production.
      2. GOOGLE_SERVICE_ACCOUNT_FILE path (local JSON file)   — used in local dev.
    """
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = ServiceAccountCredentials.from_service_account_info(
            info,
            scopes=DRIVE_SCOPES,
        )
    else:
        credentials = ServiceAccountCredentials.from_service_account_file(
            settings.GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=DRIVE_SCOPES,
        )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def build_calendar_service(settings: Settings) -> Resource:
    """
    Return an authenticated Google Calendar API v3 service acting *as* the
    shared events account (GOOGLE_CALENDAR_ID).

    Unlike the Drive client, this uses **user** credentials (an OAuth refresh
    token the events account granted once), not a service account. That is
    deliberate: a service account cannot add attendees to an event without
    domain-wide delegation, whereas a real user account can. The refresh token
    keeps the blast radius to this one account instead of the whole domain.

    The access token is minted lazily and auto-refreshed by the client on
    expiry; only the long-lived refresh token is stored.
    """
    if not settings.GOOGLE_CALENDAR_REFRESH_TOKEN:
        raise RuntimeError(
            "GOOGLE_CALENDAR_REFRESH_TOKEN is not set. Run "
            "scripts/get_calendar_refresh_token.py and add the token to the "
            "environment."
        )
    credentials = UserCredentials(
        token=None,
        refresh_token=settings.GOOGLE_CALENDAR_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=CALENDAR_SCOPES,
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)
