"""
FastAPI dependency injection helpers.

Provides:
  - get_current_user  → authenticates the Bearer JWT and returns UserInfo
  - Service factories → instantiate services with settings
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.db_v2.models.hub_user import HubUserV2
from app.models.auth import UserInfo
from app.services.airtable_service import AirtableService
from app.services.auth_service import AuthService
from app.services.calendar_service import CalendarService
from app.services.dify_service import DifyService
from app.services.drive_service import DriveService
from app.services.gemini_service import GeminiService
from app.services.tab_service import HUB_ADMIN_ROLE

_bearer_scheme = HTTPBearer()

# ── Service singletons (simple module-level cache) ─────────────────────
_auth_service: AuthService | None = None
_calendar_service: CalendarService | None = None
_drive_service: DriveService | None = None
_dify_service: DifyService | None = None
_airtable_service: AirtableService | None = None
_gemini_service: GeminiService | None = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService(get_settings())
    return _auth_service


def get_drive_service() -> DriveService:
    global _drive_service
    if _drive_service is None:
        _drive_service = DriveService(get_settings())
    return _drive_service


def get_calendar_service() -> CalendarService:
    global _calendar_service
    if _calendar_service is None:
        _calendar_service = CalendarService(get_settings())
    return _calendar_service


def get_dify_service() -> DifyService:
    global _dify_service
    if _dify_service is None:
        _dify_service = DifyService(get_settings())
    return _dify_service


def get_airtable_service() -> AirtableService:
    global _airtable_service
    if _airtable_service is None:
        _airtable_service = AirtableService(get_settings())
    return _airtable_service


def get_gemini_service() -> GeminiService:
    global _gemini_service
    if _gemini_service is None:
        _gemini_service = GeminiService(get_settings())
    return _gemini_service


# ── Current-user dependency ────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> UserInfo:
    """Extract and validate the JWT from the Authorization header."""
    return auth_service.decode_access_token(credentials.credentials)


# ── Hub Admin gate ─────────────────────────────────────────────────────
#
# The single write gate in the product, used by two unrelated surfaces:
#
#   - Role/scope definitions and hierarchy edges
#     (plan_access_control_schema_2026-08-22.md §6). Roles and scopes have
#     no per-node access_control and never will — they are global org
#     structure, so a global gate is the correct shape rather than a
#     shortcut. Assignments, which DO vary by the caller's own (role,
#     scope) envelope, get the §6 delegation check instead; they must not
#     reuse this dependency.
#
#   - Nav-tab and hub-level mutations. These used to be gated by
#     `require_hub_editor` / `require_nav_tab_editor`, which evaluated the
#     propagation engine's `can_edit` against the node being written. That
#     engine is gone, and with every nav tab's stored `admins` narrowed to
#     {Hub Admin} by migrate_hub_ac_propagation.py, those gates only ever
#     admitted Hub Admins in practice — so this is the same outcome with
#     none of the coupling. It is a placeholder for whatever the new access
#     control algorithm decides, not a designed end state.


async def require_hub_admin(user: UserInfo = Depends(get_current_user)) -> UserInfo:
    """Gate for role/scope definition writes and nav-tab mutations.

    Reads the JWT's role NAMES, which are sourced from Airtable at login —
    the same signal the frontend's `isAdmin` uses.

    TRANSITION HAZARD: this deliberately does not consult the new
    `role_assignments` table, because during the parallel-running period
    that table is empty and gating on it would lock every admin out of the
    tool meant to populate it. Re-point this at the new tables as part of
    the Airtable cutover, and do it BEFORE the Airtable roles stop being
    issued into the JWT, never after.
    """
    if HUB_ADMIN_ROLE not in (user.roles or []):
        raise HTTPException(status_code=403, detail="Hub Admin access required")
    return user


# ── hub_users auto-provisioning (session_handoff_2026-08-24-permission-
# management-plan.md §5.1) ──────────────────────────────────────────────
#
# Blocking prerequisite for the whole Permission Management phase: without
# this, no signed-in person — including a would-be granter — resolves to a
# hub_users row, so §6's delegation check has nothing to query.
#
# Deliberately a lazy upsert on EVERY authenticated request that needs a
# hub_users identity, not only the OAuth callback: someone already signed in
# before this shipped must get provisioned the next time they hit an
# assignments endpoint, without having to log in again. Someone who has
# never logged in at all still correctly cannot be resolved — that is the
# intended v1 constraint (plan §3.2), not a gap in this dependency.
@dataclass
class CurrentHubUser:
    """The caller, resolved to both halves the assignments surface needs:
    the JWT-derived `UserInfo` (for `.roles`, i.e. the Hub Admin bypass) and
    the `hub_users` row id every `role_assignments` write/read joins on."""

    info: UserInfo
    hub_user_id: int
    email: str  # normalized: the same value stored on the hub_users row


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def get_current_hub_user(
    user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db_v2),
) -> CurrentHubUser:
    """Resolve (and provision if needed) the caller's `hub_users` row.

    Normalizes emails with `.strip().lower()`, the same rule every other
    email comparison in this codebase applies.

    UNLIKE every other dependency in this module, this one commits. A GET
    endpoint (e.g. "my assignments") never calls `db.commit()` itself, but
    provisioning has to survive past this request regardless of whether the
    route that triggered it writes anything — so the insert is committed
    right here, in its own small transaction, rather than left for the
    caller to remember.

    Handles the concurrent-first-login race explicitly: two simultaneous
    requests can both miss the SELECT and both attempt the INSERT, and only
    one wins the UNIQUE constraint on `email`. The loser rolls back and
    re-reads rather than 500ing.
    """
    email = (user.email or "").strip().lower()

    hub_user = db.query(HubUserV2).filter(HubUserV2.email == email).first()
    if hub_user is None:
        hub_user = HubUserV2(email=email, name=user.name, is_active=True, created_at=_utc_now())
        db.add(hub_user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            hub_user = db.query(HubUserV2).filter(HubUserV2.email == email).first()
        else:
            db.refresh(hub_user)

    if hub_user is None:
        # Only reachable if the concurrent INSERT that won the race was
        # itself rolled back by something else entirely — effectively
        # unreachable, but a 500 here would be an unreadable one.
        raise HTTPException(status_code=500, detail="Failed to resolve current user")

    return CurrentHubUser(info=user, hub_user_id=hub_user.id, email=email)
