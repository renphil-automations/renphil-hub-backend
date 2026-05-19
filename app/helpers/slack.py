"""
Slack-related helpers.

Provides:
  - verify_slack_signature: validate the X-Slack-Signature header on
    incoming requests from Slack (slash commands, events, etc.).
  - post_to_response_url: send a follow-up ephemeral message to the user
    via the ``response_url`` Slack provided with the slash-command payload.
  - get_user_email: resolve a Slack user_id to their profile email via
    the ``users.info`` Web API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from app.helpers.http_client import get_http_client

logger = logging.getLogger(__name__)

# Slack rejects requests older than 5 minutes; we mirror that here to
# protect against replay attacks.
_MAX_REQUEST_AGE_SECONDS = 60 * 5


def verify_slack_signature(
    *,
    signing_secret: str,
    request_body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """Return True when the request signature matches Slack's signing secret.

    Implements the v0 signing scheme described at
    https://api.slack.com/authentication/verifying-requests-from-slack.
    """
    if not signing_secret or not timestamp or not signature:
        logger.warning(
            "Slack signature verification skipped: missing signing_secret/timestamp/signature."
        )
        return False

    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        logger.warning("Slack signature verification: invalid timestamp %r", timestamp)
        return False

    if abs(time.time() - ts_int) > _MAX_REQUEST_AGE_SECONDS:
        logger.warning("Slack request timestamp outside allowed window (ts=%s).", timestamp)
        return False

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + request_body
    digest = hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    expected = f"v0={digest}"
    valid = hmac.compare_digest(expected, signature)
    if not valid:
        logger.warning("Slack signature mismatch.")
    return valid


async def post_to_response_url(response_url: str, text: str) -> None:
    """Send a follow-up ephemeral message to the user via Slack's response_url."""
    client = get_http_client()
    try:
        response = await client.post(
            response_url,
            json={"response_type": "ephemeral", "text": text},
        )
        if response.status_code >= 400:
            logger.warning(
                "Slack response_url returned HTTP %s (url=%s, body=%r)",
                response.status_code,
                response_url,
                response.text[:200],
            )
        else:
            logger.debug(
                "Posted follow-up to Slack response_url (status=%s)",
                response.status_code,
            )
    except Exception:
        logger.exception("Failed to post follow-up message to Slack response_url.")


async def get_user_email(user_id: str, bot_token: str) -> str | None:
    """Resolve a Slack ``user_id`` to the user's profile email address.

    Returns ``None`` when the lookup fails, the bot token is missing, or
    the user has not made their email visible on their Slack profile.
    """
    if not user_id or not bot_token:
        logger.warning(
            "get_user_email: missing user_id or bot_token (user_id_set=%s, token_set=%s)",
            bool(user_id),
            bool(bot_token),
        )
        return None

    client = get_http_client()
    try:
        response = await client.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"user": user_id},
        )
    except Exception:
        logger.exception("get_user_email: HTTP call to Slack users.info failed.")
        return None

    try:
        data = response.json()
    except ValueError:
        logger.warning(
            "get_user_email: non-JSON response from Slack (status=%s, body=%r)",
            response.status_code,
            response.text[:200],
        )
        return None

    if not data.get("ok"):
        logger.warning(
            "get_user_email: Slack users.info returned not-ok (user_id=%s, error=%s)",
            user_id,
            data.get("error"),
        )
        return None

    email = (data.get("user") or {}).get("profile", {}).get("email")
    if not email:
        logger.info(
            "get_user_email: no email on profile (user_id=%s).", user_id
        )
        return None
    return email
