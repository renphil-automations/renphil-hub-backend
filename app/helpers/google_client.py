"""
Factory functions for Google API clients.
- OAuth2 flow client (user-facing sign-in)
- Drive service client  (service-account, server-side)
"""

from __future__ import annotations

import json
import os

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
