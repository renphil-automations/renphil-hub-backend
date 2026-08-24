"""
FastAPI dependency injection helpers.

Provides:
  - get_current_user  → authenticates the Bearer JWT and returns UserInfo
  - Service factories → instantiate services with settings
"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.models.auth import UserInfo
from app.services import access_control_service
from app.services.airtable_service import AirtableService
from app.services.auth_service import AuthService
from app.services.calendar_service import CalendarService
from app.services.dify_service import DifyService
from app.services.drive_service import DriveService
from app.services.gemini_service import GeminiService
from app.services.nav_tab_service import get_nav_tab_by_document_id
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


# ── Hub / nav-tab authorization dependencies (phase 2, §5.3) ───────────
#
# Replaces the phase-1 `require_hub_admin` JWT-role gate. Phase 1's own
# comment predicted "a one-line dependency swap, every call site
# unchanged" — that turned out to be wrong (plan §5.3): a single hub-wide
# gate would let a principal who fails `can_edit` on a specific nav tab
# delete or rename it anyway, since the check never consulted the node
# being mutated. So this is two dependencies, not one:
#   - require_hub_editor: collection-level (create a nav tab, reorder them)
#   - require_nav_tab_editor: per-node (rename / AC / delete on THAT tab)
# Both evaluate access_control_service.can_edit, which already implements
# rule C (admins AND viewers), the empty-group rules (§3.3), the
# Hub-Admin-role bypass, and the scoped-role skip — no new logic here,
# just the lookup + the two shapes of "which node to check."


async def require_hub_editor(
    user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db_v2),
) -> UserInfo:
    """The collection-level gate: creating a nav tab, or reordering them —
    there is no single node to check yet (or the check is a property of
    the whole collection), so this asks the hub."""
    hub = access_control_service.get_hub(db)
    hub_ac = hub.access_control if hub is not None else None
    if not access_control_service.can_edit(hub_ac, user.email, list(user.roles)):
        raise HTTPException(status_code=403, detail="Hub editor access required")
    return user


async def require_nav_tab_editor(
    document_id: str,
    user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db_v2),
) -> UserInfo:
    """Per-node: can_edit on THAT nav tab. FastAPI injects `document_id`
    from the route's own path parameter of the same name — used for
    rename / access-control edits / delete, all of which mutate (or
    cascade-delete from) one specific nav tab, not the collection."""
    nav_tab = get_nav_tab_by_document_id(db, document_id)
    if nav_tab is None:
        raise HTTPException(status_code=404, detail="Nav tab not found")
    if not access_control_service.can_edit(nav_tab.access_control, user.email, list(user.roles)):
        raise HTTPException(status_code=403, detail="Nav tab editor access required")
    return user


# ── Access Management gate (plan_access_control_schema_2026-08-22.md §6) ─
#
# Yes, this is the name phase 2 removed above — reintroduced deliberately,
# not by accident. The reason it was wrong there does not apply here.
#
# Phase 2's objection was that a hub-wide gate let someone who fails
# can_edit on a SPECIFIC nav tab mutate it anyway, because the check never
# consulted the node being mutated. That argument is about per-node
# resources: every nav tab and tab carries its own access_control.
#
# Roles and scopes have no per-node access_control and never will. They are
# global org structure, so a global gate is the correct shape rather than a
# shortcut — there is no node to consult. Assignments, which DO vary by the
# caller's own (role, scope) envelope, get the §6 delegation check instead
# when that surface lands; they must not reuse this dependency.


async def require_hub_admin(user: UserInfo = Depends(get_current_user)) -> UserInfo:
    """Gate for writing role/scope definitions and hierarchy edges.

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
