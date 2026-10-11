"""Authenticated identity forwarded by the trusted Agent service.

The Agent signs the same short-lived context used by the Airtable personal
context endpoints.  This module keeps the live Hub ACL endpoint independent
from a browser JWT while still binding its answer to the requesting user.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import time

from fastapi import Header, HTTPException, status

from app.config import get_settings


_CONTEXT_DOMAIN = b"renphil-airtable-personal-v1:"
_IDENTITY_TTL_SECONDS = 60
_IDENTITY_FUTURE_SKEW_SECONDS = 30
_CONTEXT_MAX_BYTES = 4096


@dataclass(frozen=True)
class AgentIdentity:
    email: str
    roles: tuple[str, ...]


def _invalid_identity() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid Agent identity context",
    )


def require_agent_sync_token(
    x_sync_token: str | None = Header(default=None, alias="X-Sync-Token"),
) -> None:
    """Require the shared Agent-to-Backend token before reading Hub ACLs."""

    expected = (get_settings().AGENT_SYNC_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent sync authentication is not configured",
        )

    provided = (x_sync_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid sync token.",
        )


def verify_agent_identity(
    encoded_context: str | None,
    signature: str | None,
) -> AgentIdentity:
    """Verify the Agent's short-lived HMAC-bound end-user identity."""

    secret = (get_settings().AGENT_SYNC_TOKEN or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent sync authentication is not configured",
        )

    encoded = (encoded_context or "").strip()
    supplied_signature = (signature or "").strip()
    if (
        not encoded
        or not supplied_signature
        or len(encoded.encode("utf-8")) > _CONTEXT_MAX_BYTES
    ):
        raise _invalid_identity()

    try:
        encoded_bytes = encoded.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid_identity() from exc

    expected = hmac.new(
        secret.encode("utf-8"),
        _CONTEXT_DOMAIN + encoded_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected):
        raise _invalid_identity()

    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            (encoded + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid_identity() from exc

    if not isinstance(payload, dict):
        raise _invalid_identity()

    version = payload.get("v")
    issued_at = payload.get("iat")
    email = payload.get("email")
    raw_roles = payload.get("roles")
    if (
        type(version) is not int
        or version != 1
        or type(issued_at) is not int
        or not isinstance(email, str)
        or not email.strip()
        or not isinstance(raw_roles, list)
        or any(not isinstance(role, str) for role in raw_roles)
    ):
        raise _invalid_identity()

    now = int(time.time())
    if issued_at > now + _IDENTITY_FUTURE_SKEW_SECONDS:
        raise _invalid_identity()
    if now - issued_at > _IDENTITY_TTL_SECONDS:
        raise _invalid_identity()

    roles = tuple(dict.fromkeys(role.strip() for role in raw_roles if role.strip()))
    return AgentIdentity(email=email.strip().lower(), roles=roles)


def require_agent_identity(
    x_sync_token: str | None = Header(default=None, alias="X-Sync-Token"),
    x_agent_context: str | None = Header(
        default=None,
        alias="X-RenPhil-Agent-Context",
    ),
    x_agent_signature: str | None = Header(
        default=None,
        alias="X-RenPhil-Agent-Signature",
    ),
) -> AgentIdentity:
    """Authenticate the Agent service and return its signed user identity."""

    require_agent_sync_token(x_sync_token)
    return verify_agent_identity(x_agent_context, x_agent_signature)
