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
from app.db_v2.models.role import RoleV2
from app.db_v2.models.scope import ScopeV2
from app.models.auth import UserInfo
from app.services.airtable_service import AirtableService
from app.services.auth_service import AuthService
from app.services.calendar_service import CalendarService
from app.services.dify_service import DifyService
from app.services.drive_service import DriveService
from app.services.gemini_service import GeminiService
from app.services.rbac_graph_service import RbacClosures, effective_pairs
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
#
# MOVED ABOVE the Hub Admin gate (plan_access_control_algorithm_2026-08-27.md
# §6.8, §10 item 6, phase 2): `require_hub_admin` now needs a `hub_users` row
# to check `role_assignments` against, which means it depends on
# `get_current_hub_user` rather than bare `get_current_user` — so this has to
# be defined first. See that section's own docstring for what this newly
# costs role/scope-definition and nav-tab writes: a possible provisioning
# commit that used to be exclusive to the assignments/grants surfaces.
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


# ── Hub Admin identity resolver ─────────────────────────────────────────
# (plan_access_control_algorithm_2026-08-27.md §6.8, §10 item 6 — PHASE 2
# of 3, "parallel run", 2026-09-03)
#
# `require_hub_admin` used to gate two unrelated surfaces (its own old
# docstring said so): nav-tab/hub mutations, which are becoming an ordinary
# `edit(hub)` / `edit(nav_tab)` check under the read-time visibility
# algorithm — NOT this session's work, still gated by `require_hub_admin`
# below exactly as before — and role/scope DEFINITION writes, which have no
# resource node to hang a grant on and so keep a global identity gate. This
# section is the surviving identity gate. `dependencies=[Depends(
# require_hub_admin)]` on nav_tabs.py / tabs_v2.py / rbac.py is UNCHANGED;
# only what it resolves from changes.
#
# ONE RESOLVER, replacing three copies that used to say the same six lines
# independently: this function (formerly inlined in `require_hub_admin`
# itself), `routers/rbac_assignments.py`'s `_is_hub_admin` (fed into
# `assert_can_delegate`), and `routers/resource_grants.py`'s `_is_hub_admin`
# (fed into `assert_can_administer_node` / `assert_can_grant`, and left as a
# deliberate third copy when the grants router was built — see that file's
# git history; the reason for the duplication was that consolidating early
# would move the gate without moving the cutover's sequencing, and that
# sequencing is exactly what this section now implements). All three call
# sites now call `is_hub_admin(db, current)` below.
HUB_ADMIN_ROLE_KEY = "hub_admin"  # `roles.key`. NOT `HUB_ADMIN_ROLE` above, which
# is "Hub Admin" — a JWT DISPLAY NAME sourced from Airtable, a different
# namespace entirely (same split `rbac_assignment_service.HUB_ADMIN_ROLE_KEY`
# documents for the admin-floor guard; that module's copy of this same
# role/universal-scope lookup is intentionally not imported from here — it
# is five lines serving a differently-scoped rule (an INTEGRITY check with
# its own, deliberately LITERAL semantics, see below), and importing across
# would couple this identity gate to that guard's private plumbing for no
# real gain).


def _hub_admin_target_ids(db: Session) -> tuple[int | None, set[int]]:
    """`(hub_admin role id, universal scope ids)` — the pair `is_hub_admin`
    treats as a virtual grant on the hub node (plan §6.5's "an `edit` grant
    on the `hub` node of `(Hub Admin, All Scopes)`").

    Universal scope ids come back as a SET even though at most one row can
    ever have `is_universal=true` (`uq_scopes_single_universal`) — the same
    defensive shape `RbacClosures` and the admin-floor guard both use for
    ⊥/universal ids, and for the same reason: unioning a set of any size is
    the same operation, and this code should not independently assume what
    the index already guarantees.
    """
    role_row = db.query(RoleV2.id).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).first()
    universal_ids = {
        row[0] for row in db.query(ScopeV2.id).filter(ScopeV2.is_universal.is_(True)).all()
    }
    return (role_row[0] if role_row else None), universal_ids


def is_hub_admin(
    db: Session,
    current: CurrentHubUser,
    *,
    closures: RbacClosures | None = None,
) -> bool:
    """Is ``current`` a Hub Admin? THE ONE PLACE THIS QUESTION IS ANSWERED.

    ══════════════════════════════════════════════════════════════════════
    THE `OR` IS THE ENTIRE SAFETY PROPERTY OF THE PARALLEL RUN. DO NOT
    COLLAPSE IT TO ONE SIDE. DO NOT GATE EITHER BRANCH BEHIND A FEATURE
    FLAG THAT COULD DISABLE IT. THIS IS A PHASE, NOT AN END STATE.
    ══════════════════════════════════════════════════════════════════════
    Two independent sources, either one sufficient:

      1. The JWT's role NAMES, sourced from Airtable at login (unchanged
         from before this task — the ORIGINAL admin path, still the only
         one most people have today).
      2. `role_assignments`, via the closures every ordinary grant match
         already uses (NEW as of this task).

    §10 item 6 states why both must stay live at once: *"while assignments
    are being populated, neither source alone can lock anyone out."*
    `role_assignments` holds exactly one row as of 2026-09-03 — if this
    resolved from assignments ALONE today, every admin but the one person
    named in that row would be locked out of the tool that populates the
    table. If it resolved from the JWT alone forever, `role_assignments`
    would never need populating and §10's cutover could never happen. Only
    the `OR` lets population and enforcement-readiness proceed
    concurrently, which is the entire point of calling this "phase 2 of 3"
    rather than "the fix."

    The NEXT session's job (§10 item 6, phase 3) is to drop branch 1 and add
    a settings-level `BOOTSTRAP_ADMIN_EMAILS` backstop in its place — NOT to
    delete this docstring's warning, which stays true until that happens.

    ══════════════════════════════════════════════════════════════════════
    §0.1's decision: CLOSURE membership, not a literal row (owner-confirmed
    2026-09-03).
    ══════════════════════════════════════════════════════════════════════
    Branch 2 asks "is `(hub_admin, universal_scope)` in this caller's
    `effective_pairs`?" — the ORDINARY §4.2 match rule, treating the hub
    node's Hub Admin grant like any other stored grant. That means a role
    added ABOVE `hub_admin` in the role DAG in the future would inherit Hub
    Admin power automatically, with no separate assignment naming it —
    which is exactly how every other grant in this design already works
    (`role.py`: "the parent inherits everything the child has"; §4.2's
    match direction is symmetric for every pair, this one included). This
    was deliberately re-examined against the DIFFERENT, LITERAL rule the
    admin-floor guard uses (`rbac_assignment_service._hub_admin_pair_ids`,
    "confirmed with the owner 2026-08-31, §6.8 verbatim") before being
    decided this way — the two do not have to agree, because they answer
    different questions. The floor guard is an INTEGRITY rule ("is there
    at least one un-losable admin"), deliberately conservative so the floor
    cannot silently move when someone edits the role DAG. This function is
    an AUTHORIZATION rule ("can this caller act as admin right now"), and
    for that question closure membership is the ordinary, consistent
    answer — not a special case. Do not "fix" this to match the floor
    guard's literalness; that would be re-litigating a decision already
    made both ways, deliberately, for different reasons.

    ══════════════════════════════════════════════════════════════════════
    Implementation notes
    ══════════════════════════════════════════════════════════════════════
    NOT ITS OWN QUERY (§6.8): branch 2 is `effective_pairs(db,
    current.hub_user_id, closures=closures)` — the exact helper every
    ordinary grant match already calls. Pass `closures` when the caller is
    already holding one this request (e.g. `RbacClosures(db)` built once
    and shared with `assert_can_administer_node` / `assert_can_grant` /
    `assert_can_delegate`); omitted, `effective_pairs` builds its own.

    Branch 1 is checked FIRST and short-circuits branch 2 entirely — a Hub
    Admin recognized by JWT never needs a `hub_users` row to exist in
    `role_assignments` at all, which matters because `role_assignments` is
    still mostly empty.
    """
    if HUB_ADMIN_ROLE in (current.info.roles or []):
        return True

    hub_admin_role_id, universal_scope_ids = _hub_admin_target_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        # No `hub_admin` role row, or no universal scope: the pair this
        # function looks for cannot exist, so branch 2 cannot admit anyone.
        # Same reasoning as the admin-floor guard's identical early-return —
        # this is the shape of a fresh/misconfigured database, not an error
        # to raise on every unrelated request.
        return False

    pairs = effective_pairs(db, current.hub_user_id, closures=closures)
    return any((hub_admin_role_id, scope_id) in pairs for scope_id in universal_scope_ids)


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
#     none of the coupling. Nav-tab/hub mutations are still gated here,
#     unconditionally, because splitting them out to `edit(hub)` /
#     `edit(nav_tab)` (plan §6.3) needs the visibility folds wired into real
#     endpoints, which is blocked on grants being authored and verified
#     (§10's ordering) — NOT this session's work. Role/scope DEFINITION
#     writes are the half `is_hub_admin` above now genuinely resolves.


async def require_hub_admin(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> UserInfo:
    """Gate for role/scope definition writes and nav-tab mutations.

    PHASE 2 OF 3 (plan §10 item 6, 2026-09-03): resolves from `is_hub_admin`
    above — the JWT role check this dependency has always done, `OR` a
    `role_assignments` closure check that is new as of this task. Neither
    source alone is authoritative yet; see `is_hub_admin`'s docstring for
    why the `OR` must stay live until phase 3 (assignments-only, plus a
    `BOOTSTRAP_ADMIN_EMAILS` backstop — not this session).

    THIS NOW TAKES `CurrentHubUser`, NOT BARE `UserInfo` — a change from
    before this task, and it has a real side effect. `get_current_hub_user`
    auto-provisions (and COMMITS) a `hub_users` row for the caller if one
    does not exist yet (see that dependency's own docstring for the
    concurrent-first-login race it already handles). Before this task, that
    commit was reachable only through the assignments and grants routers;
    after it, EVERY role/scope-definition write and EVERY nav-tab mutation
    can trigger it too, because both now sit behind this same dependency.
    That is a widening of who gets auto-provisioned and when, not a new
    kind of risk — `get_current_hub_user` was already built to be called
    from arbitrarily many routes on arbitrarily many requests, and its race
    handling does not care which route triggered it.

    WIDENING, STATED PLAINLY (not "purely additive"): someone who holds
    `(hub_admin, universal_scope)` in `role_assignments` but lacks the
    Airtable "Hub Admin" role in their JWT now newly passes THIS gate too —
    including on nav-tab and hub mutations, which this task does not
    otherwise touch. That is exactly what phase 2 is for (populating and
    proving out assignments before Airtable is retired), but it is a real
    behaviour change on every surface this dependency guards, not a
    no-op affecting only the assignments/grants routers.
    """
    if not is_hub_admin(db, current):
        raise HTTPException(status_code=403, detail="Hub Admin access required")
    return current.info
