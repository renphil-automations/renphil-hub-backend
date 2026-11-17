"""
One-time: mint a long-lived refresh token for the shared events calendar account.

Why this exists: the "add myself to this event" button has to modify an event
owned by cal@renphil.org, adding the signed-in user to its attendee list. A
service account cannot do that - Google rejects attendee changes from service
accounts with 403 `forbiddenForServiceAccounts` unless the account is granted
domain-wide delegation, and DWD was rejected deliberately: it would let one
leaked key impersonate every user in renphil.org.

So the backend acts as cal@renphil.org itself, using a refresh token that
account grants once. Blast radius is one account instead of the whole domain.
This script performs that one-time grant and prints the token.

Google has no password grant, so there is no shortcut here - putting the
account's password in .env would do nothing. A refresh token is the only
credential the Calendar API will accept for this.

    *** STOP THE BACKEND BEFORE RUNNING THIS. ***

The redirect URI is a live route (app/routers/auth.py). If uvicorn is running
on :8000 it will intercept the callback, fail its PKCE state check, and discard
the refresh token before you ever see it. With nothing listening the browser
shows "connection refused" and the code stays in the address bar - which is
exactly what this script wants.

    cd renphil-hub-backend
    ./.venv/Scripts/python.exe scripts/get_calendar_refresh_token.py

Prints the refresh token to stdout only; never writes it to disk. Paste it into
.env as GOOGLE_CALENDAR_REFRESH_TOKEN and into the Vercel env vars.
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import parse_qs, urlparse

# The redirect URI is http://localhost, which oauthlib refuses by default, and
# Google returns a superset of the requested scopes (it re-attaches previously
# granted ones). Both must be relaxed before the oauthlib imports below run.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from dotenv import load_dotenv  # noqa: E402
from google.auth.transport import requests as google_requests  # noqa: E402
from google.oauth2 import id_token as google_id_token  # noqa: E402
from google_auth_oauthlib.flow import Flow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

DEFAULT_ACCOUNT = "cal@renphil.org"
DEFAULT_REDIRECT_URI = "http://localhost:8000/auth/callback"

# `calendar.events` grants read/write on events only - deliberately not the full
# `calendar` scope, which would also permit deleting calendars and rewriting
# sharing ACLs. openid + userinfo.email are along for one reason: they yield an
# id_token, which is how we prove *which* account actually consented. Consenting
# as the wrong account is the single easiest way to get this wrong, and without
# an id_token the mistake stays invisible until production posts events as you.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.events",
]


def _extract_code(pasted: str) -> str:
    """Accept either the full redirected URL or a bare authorization code."""
    pasted = pasted.strip()
    if not pasted:
        raise ValueError("Nothing pasted.")

    if pasted.startswith("http://") or pasted.startswith("https://"):
        params = parse_qs(urlparse(pasted).query)
        if "error" in params:
            raise ValueError(f"Google returned an error: {params['error'][0]}")
        codes = params.get("code")
        if not codes:
            raise ValueError("That URL has no `code` parameter.")
        return codes[0]

    return pasted


def _verify_account(credentials, client_id: str, expected: str) -> str:
    """Return the email that consented, aborting if it is not `expected`."""
    if not credentials.id_token:
        print(
            "\n!! Could not confirm which account consented (no id_token).\n"
            "   Verify manually before using this token.",
            file=sys.stderr,
        )
        return ""

    info = google_id_token.verify_oauth2_token(
        credentials.id_token, google_requests.Request(), client_id
    )
    email = info.get("email", "")

    if email.lower() != expected.lower():
        raise SystemExit(
            f"\n!! Wrong account. You consented as {email!r}, expected {expected!r}.\n"
            f"   This token would make the backend post events as {email}.\n"
            f"   Re-run and sign in as {expected} - use an incognito window if\n"
            f"   Google keeps picking your own account."
        )

    return email


def _smoke_test(credentials, calendar_id: str) -> None:
    """Prove the token can actually read the target calendar before we trust it."""
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    try:
        result = (
            service.events()
            .list(calendarId=calendar_id, maxResults=3, orderBy="startTime",
                  singleEvents=True, timeMin="2000-01-01T00:00:00Z")
            .execute()
        )
    except Exception as exc:
        print(f"\n!! Could not read calendar {calendar_id!r}: {exc}", file=sys.stderr)
        print("   The token is still valid - check the calendar id.", file=sys.stderr)
        return

    items = result.get("items", [])
    print(f"\n   Read OK - {calendar_id} ({len(items)} event(s) sampled):")
    for item in items:
        start = item.get("start", {})
        when = start.get("dateTime") or start.get("date") or "?"
        print(f"     - {item.get('summary', '(untitled)')} : {when}")
    if not items:
        print("     (calendar is empty - that is fine, access still works)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT,
                        help=f"Account that must consent (default: {DEFAULT_ACCOUNT})")
    parser.add_argument("--calendar-id", default=None,
                        help="Calendar to smoke-test (default: the account itself)")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                        help="Must exactly match a URI registered on the OAuth client")
    parser.add_argument("--env-file", default=".env", help="Where to read client creds")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            f"GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not found in {args.env_file}.\n"
            "Run this from the renphil-hub-backend directory.",
            file=sys.stderr,
        )
        return 1

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [args.redirect_uri],
            }
        },
        scopes=SCOPES,
        autogenerate_code_verifier=True,
    )
    flow.redirect_uri = args.redirect_uri

    # prompt=consent forces a refresh token even if this account already granted
    # access; without it Google returns only an access token on re-consent.
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")

    print("=" * 72)
    print(" ONE-TIME CONSENT - shared events calendar")
    print("=" * 72)
    print(f"\n  Is the backend stopped? If uvicorn is on :8000 it will eat the code.\n")
    print(f"  1. Open this URL and sign in as {args.account}:\n")
    print(f"     {auth_url}\n")
    print("  2. Approve the calendar permission.")
    print("  3. The browser will show \"connection refused\" - that is expected.")
    print("     The code is still in the address bar.")
    print("  4. Copy the whole URL from the address bar and paste it below.")
    print("     (Codes expire in ~10 minutes, so do this promptly.)\n")

    try:
        pasted = input("  Paste the redirected URL (or just the code): ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return 1

    try:
        code = _extract_code(pasted)
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    if pasted.strip().startswith("http"):
        returned = parse_qs(urlparse(pasted.strip()).query).get("state", [None])[0]
        if returned and returned != state:
            print("\n!! State mismatch - that URL is from a different run. Start over.",
                  file=sys.stderr)
            return 1

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        print(f"\nToken exchange failed: {exc}", file=sys.stderr)
        print("Most likely the code expired or was already used. Re-run.", file=sys.stderr)
        return 1

    credentials = flow.credentials
    email = _verify_account(credentials, client_id, args.account)

    if not credentials.refresh_token:
        print(
            "\n!! Google returned no refresh token.\n"
            "   Revoke this app at https://myaccount.google.com/permissions\n"
            "   (as that account) and re-run.",
            file=sys.stderr,
        )
        return 1

    _smoke_test(credentials, args.calendar_id or args.account)

    print("\n" + "=" * 72)
    print(f" SUCCESS - refresh token for {email or args.account}")
    print("=" * 72)
    print("\nAdd to .env and to the Vercel environment variables:\n")
    print(f"GOOGLE_CALENDAR_ID={args.calendar_id or args.account}")
    print(f"GOOGLE_CALENDAR_REFRESH_TOKEN={credentials.refresh_token}")
    print(f"GOOGLE_CALENDAR_SEND_UPDATES=all")
    print(
        "\nTreat this token like a password: it grants read/write on that\n"
        "account's events until revoked. Do not commit it or paste it into\n"
        "chat. To revoke: https://myaccount.google.com/permissions as that\n"
        "account, then re-run this script.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
