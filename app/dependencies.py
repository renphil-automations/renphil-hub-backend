"""
FastAPI dependency injection helpers.

Provides:
  - get_current_user  → authenticates the Bearer JWT and returns UserInfo
  - Service factories → instantiate services with settings
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.db_v2.models.user import UserV2
from app.models.auth import UserInfo
from app.services.airtable_service import AirtableService
from app.services.auth_service import AuthService
from app.services.calendar_service import CalendarService
from app.services.dify_service import DifyService
from app.services.drive_service import DriveService
from app.services.gemini_service import GeminiService
from app.services import edit_lock_service, rbac_assignment_service, view_as_service
from app.services.access_visibility_service import ViewerAccess, resolve_viewer_access
from app.services.rbac_graph_service import (  # noqa: F401 — HUB_ADMIN_ROLE_KEY is re-exported, see below
    HUB_ADMIN_ROLE_KEY,
    RbacClosures,
    effective_pairs,
    hub_admin_target_ids,
    is_bootstrap_admin_email,
)
from app.services.user_db_service import find_user_by_email

logger = logging.getLogger(__name__)

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
# `hub_users` was folded into `users` in 2026-10: every "hub_users row" below
# is now a `users` row, and `hub_user_id` is a `users.id`. The names stay so
# the ~15 modules built on them did not have to move.
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
    email: str  # normalized: the JWT email the row was found by (work or alias email)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_org_domain_email(email: str) -> bool:
    """True for an email in one of `ALLOWED_EMAIL_DOMAIN`'s domains — the
    people the login gate admits by domain alone."""
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    return domain in get_settings().allowed_email_domains


class HubUserRemoved(Exception):
    """`_ensure_hub_user` found a soft-deleted row for the email
    (plan_admin_soft_delete_user_2026-10-19.md §3.3). Deliberately NOT an
    HTTPException: each caller maps it to its own refusal (401
    `user_removed`, the sign-in 403, the Agent 403), and a caller that
    forgets to fails closed with a 500 rather than admitting the person."""

    def __init__(self, user: UserV2) -> None:
        super().__init__(f"users row {user.id} is deleted")
        self.user = user


def _ensure_hub_user(
    db: Session, *, email: str, name: str, allow_insert_external: bool = False
) -> UserV2 | None:
    """Look up or create the signed-in `users` row for `email` (already
    normalized by the caller), and mark it signed in. Extracted from
    `get_current_hub_user` below so the OAuth callback and the dev-only
    `/auth/dev-login` route (session_handoff_2026-09-10-dev-login-tool.md)
    can provision the same row synchronously, before minting a token,
    instead of waiting for the next authenticated request.

    Since `hub_users` was folded into `users` (2026-10), the lookup goes
    through `work_email` then `alias_email`, so someone on the HR roster
    claims their EXISTING row on first sign-in rather than getting a second
    one. Only an email no row has gets a new login-only row (named from the
    Google profile, else the email). `has_signed_in` (owner decision,
    2026-10-09; renamed from `is_active` 2026-10-18): an HR row or an
    admin-added person starts False and is flipped True here — the HR `name`
    is never overwritten.

    UNLIKE every other dependency in this module, this commits. A GET
    endpoint (e.g. "my assignments") never calls `db.commit()` itself, but
    provisioning has to survive past this request regardless of whether the
    route that triggered it writes anything — so the write is committed
    right here, in its own small transaction, rather than left for the
    caller to remember. A signed-in row costs no write at all.

    Handles the concurrent-first-login race explicitly: two simultaneous
    requests can both miss the SELECT and both attempt the INSERT, and only
    one wins the unique index on `lower(work_email)`. The loser rolls back
    and re-reads rather than 500ing. Two concurrent has_signed_in flips write the
    same value, so that path needs no guard.

    A newly created row gets the default ⊥ assignment in the SAME commit
    (`rbac_assignment_service.add_default_assignment`, owner decision
    2026-10-14). Only on the insert branch: this runs on every request, so
    doing it for existing rows would both backfill (not wanted) and bring
    back a default someone revoked. The race loser's rollback discards its
    assignment along with its row; the winner's row already carries one.

    A SOFT-DELETED ROW (plan_admin_soft_delete_user_2026-10-19.md §3.3, SD4)
    raises `HubUserRemoved`, whatever the email's domain: never an insert
    (the kept row still holds the email in the unique indexes) and never a
    `has_signed_in` flip. The lookup therefore passes `include_deleted=True`;
    with the default it would see "no row" and try to insert.

    WHO MAY BE INSERTED (option B, owner 2026-10-18), for an email with NO
    row at all. An org-domain email always may. A non-org-domain email only
    when `allow_insert_external` is set, which only the OAuth callback (the
    login gate has just admitted the email) and dev-login pass. The lazy
    callers (`get_current_hub_user`, the Agent envelope path) never re-check
    the gate, so for them an external email with no row returns None
    instead: a live JWT must not provision an external person the gate
    never admitted.
    """
    user = find_user_by_email(db, email, include_deleted=True)
    if user is not None and user.is_deleted:
        raise HubUserRemoved(user)
    if user is None:
        if not allow_insert_external and not is_org_domain_email(email):
            return None
        now = _utc_now()
        user = UserV2(
            work_email=email,
            name=(name or "").strip() or email,
            has_signed_in=True,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        try:
            db.flush()
            rbac_assignment_service.add_default_assignment(db, user.id)
            db.commit()
        except IntegrityError:
            db.rollback()
            user = find_user_by_email(db, email, include_deleted=True)
            if user is not None and user.is_deleted:
                raise HubUserRemoved(user)
        else:
            db.refresh(user)
    elif not user.has_signed_in:
        user.has_signed_in = True
        db.commit()
        db.refresh(user)
    return user


async def get_current_hub_user(
    user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db_v2),
) -> CurrentHubUser:
    """Resolve (and provision if needed) the caller's `hub_users` row.

    Normalizes emails with `.strip().lower()`, the same rule every other
    email comparison in this codebase applies.
    """
    email = (user.email or "").strip().lower()

    try:
        hub_user = _ensure_hub_user(db, email=email, name=user.name)
    except HubUserRemoved:
        # Soft-deleted by an admin, any domain (plan_admin_soft_delete_user
        # §3.3). 401 so the frontend's session-expired handling signs them out.
        hub_user = None
        removed = True
    else:
        # An external email with no `users` row at all: not provisioned from
        # a bare JWT — see `_ensure_hub_user`.
        removed = hub_user is None and not is_org_domain_email(email)

    if removed:
        raise HTTPException(
            status_code=401,
            detail={"code": "user_removed", "message": "Your account was removed."},
        )
    if hub_user is None:
        # Only reachable if the concurrent INSERT that won the race was
        # itself rolled back by something else entirely — effectively
        # unreachable, but a 500 here would be an unreadable one.
        raise HTTPException(status_code=500, detail="Failed to resolve current user")

    return CurrentHubUser(info=user, hub_user_id=hub_user.id, email=email)


# ── Hub Admin identity resolver ─────────────────────────────────────────
# (plan_access_control_algorithm_2026-08-27.md §6.8, §10 item 6 — phase 2
# "parallel run" 2026-09-03; PHASE 3, assignments + BOOTSTRAP_ADMIN_EMAILS
# only, no Airtable, 2026-10-12)
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
# `HUB_ADMIN_ROLE_KEY` is `roles.key` ("hub_admin"). NOT
# `tab_service.HUB_ADMIN_ROLE`, which is "Hub Admin" — a JWT DISPLAY NAME
# sourced from Airtable, no longer read by this module at all, a
# different namespace entirely (same split
# `rbac_assignment_service.HUB_ADMIN_ROLE_KEY` documents for the admin-floor
# guard; that module's copy of this same role/universal-scope lookup is
# intentionally not imported from here — it is five lines serving a
# differently-scoped rule (an INTEGRITY check with its own, deliberately
# LITERAL semantics, see below), and importing across would couple this
# identity gate to that guard's private plumbing for no real gain).
#
# Both the constant and the lookup itself now LIVE IN `rbac_graph_service`
# (plan_thread_moderation_2026-09-18.md §5.9): the mention directory's
# reverse fold (`rbac_graph_service.hub_admin_user_ids`) needs the same
# `(hub_admin, universal)` pair from a domain service, and a domain service
# must not import this module backwards. `HUB_ADMIN_ROLE_KEY` is imported
# above and re-exported unchanged; the lookup keeps its old private name
# here as an alias, so `is_hub_admin` below and this module's readers are
# unchanged.
_hub_admin_target_ids = hub_admin_target_ids


def is_hub_admin(
    db: Session,
    current: CurrentHubUser,
    *,
    closures: RbacClosures | None = None,
) -> bool:
    """Is ``current`` a Hub Admin? THE ONE PLACE THIS QUESTION IS ANSWERED.

    ══════════════════════════════════════════════════════════════════════
    PHASE 3 (§10 item 6, 2026-10-12): AIRTABLE IS NO LONGER CONSULTED.
    ══════════════════════════════════════════════════════════════════════
    Two sources, either one sufficient:

      1. ``BOOTSTRAP_ADMIN_EMAILS`` (settings, plan §6.5) — checked FIRST
         and unconditionally. A permanent recovery floor, not a transition
         device: nothing ranks above `hub_admin`, so if every admin
         assignment were revoked nobody could write one back from inside
         the app. Do not remove it once assignments are "complete".
      2. `role_assignments`, via the closures every ordinary grant match
         already uses.

    The JWT's role NAMES (sourced from Airtable at login) are deliberately
    NOT read here any more. Phase 2 (2026-09-03) ran them in parallel with
    branch 2 while `role_assignments` was populated; phase 3 dropped them
    once the parity report (`scripts/survey_hub_admin_parity.py`) could be
    driven to zero missing. A token carrying "Hub Admin" with no assignment
    row and no bootstrap entry is NOT an admin — do not add that branch
    back as a "fallback"; it would make Airtable authoritative again.

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

    Branch 1 is checked FIRST and short-circuits branch 2 entirely — a
    bootstrap admin needs no `role_assignments` row at all, which is the
    point of a floor. It matches ``current.email`` (already stripped and
    lower-cased by `get_current_hub_user`), the verified Google sign-in
    address, never anything client-supplied.
    """
    # Branch 1 is currently DISABLED (always False) pending a team decision;
    # see `rbac_graph_service.is_bootstrap_admin_email`.
    if is_bootstrap_admin_email(current.email):
        return True

    hub_admin_role_id, universal_scope_ids = _hub_admin_target_ids(db, closures=closures)
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

    PHASE 3 (plan §10 item 6, 2026-10-12): resolves from `is_hub_admin`
    above — `BOOTSTRAP_ADMIN_EMAILS` OR a `role_assignments` closure check.
    The JWT's Airtable-sourced role names are no longer consulted; see
    `is_hub_admin`'s docstring.

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

    NARROWING, STATED PLAINLY (phase 3): someone with the Airtable "Hub
    Admin" role in their JWT but no `(hub_admin, universal_scope)`
    assignment and no bootstrap entry NO LONGER passes this gate — on every
    surface it guards, nav-tab and hub mutations included. Run the parity
    report to zero missing before deploying this.
    """
    if not is_hub_admin(db, current):
        raise HTTPException(status_code=403, detail="Hub Admin access required")
    return current.info


# ── Content visibility (plan §8.1-8.4, §10 item — closing project-ac-
# enforcement-gap's item 1: wiring the read-time fold into the tab-serving
# endpoints) ─────────────────────────────────────────────────────────────


async def get_viewer_access(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> ViewerAccess:
    """One ``ViewerAccess`` per request, shared by every tab-serving endpoint
    that gates content on the new algorithm (§8.4 — compute once, not once
    per node).

    Builds ONE ``RbacClosures`` and passes it to both ``is_hub_admin`` and
    ``resolve_viewer_access`` (§8.2 — the closure adjacency must not be
    rebuilt per call in the same request), exactly the sharing pattern
    ``resource_grants.py``'s routes already use for
    ``assert_can_administer_node`` / ``assert_can_grant``.

    Depends on ``get_current_hub_user``, not bare ``get_current_user`` — this
    needs a ``hub_users`` row to query ``role_assignments``/``resource_grants``
    against, the same reason ``require_hub_admin`` was widened to it in the
    hub-admin-resolver session.
    """
    closures = RbacClosures(db)
    admin = is_hub_admin(db, current, closures=closures)
    return resolve_viewer_access(db, current.hub_user_id, is_admin=admin, closures=closures)


# ── View As ─────────────────────────────────────────────────────────────


@dataclass
class ViewAsAccess:
    """``access`` is what the read endpoint gates content on; ``view_as`` is
    True when it was built from the View As parameters rather than from the
    caller's own assignments; ``view_as_user`` echoes the selected user's
    email, when one was given."""

    access: ViewerAccess
    view_as: bool
    view_as_user: str | None = None


VIEW_AS_USER_DESCRIPTION = (
    "View As: email of a user to see the hub as (their assignments AND the content "
    "shared directly with them). Combines with `view_as` pairs where those are accepted. "
    "Pick from `GET /v2/view-as/users`."
)


def _verify_view_as(
    db: Session,
    current: CurrentHubUser,
    values: list[str],
    user_email: str | None,
    *,
    anonymous: bool = False,
) -> view_as_service.ViewAsSelection:
    """Verify already-normalized View As input, with the logging and the
    400/403 body every View As endpoint shares."""
    logger.info(
        "View As requested by %s: view_as=%r view_as_user=%r anonymous=%s",
        current.email,
        values,
        user_email,
        anonymous,
    )
    caller_id = view_as_service.resolve_user_id(db, current.email)
    if caller_id is None:
        caller_id = current.hub_user_id
    try:
        selection = view_as_service.resolve_view_as_selection(db, caller_id, values, user_email)
    except view_as_service.ViewAsPairsError as e:
        logger.warning(
            "View As verification failed for %s (user %s): %s — %s",
            current.email,
            caller_id,
            e.code,
            "; ".join(f"{f['source']} {f['pair']!r}: {f['code']}" for f in e.failures) or e.message,
        )
        detail = {"code": e.code, "message": e.message, "pairs": e.failures}
        if e.user_email is not None:
            detail["view_as_user"] = e.user_email
        raise HTTPException(status_code=e.status_code, detail=detail)

    logger.info(
        "View As verified for %s (user %s): view_as_user=%s; adopting %s",
        current.email,
        caller_id,
        f"{selection.user.email!r} (id {selection.user.id})" if selection.user else None,
        ", ".join(
            f"role {t.role_key!r} ({t.role_name!r}, id {t.role_id}) in scope "
            f"{t.scope_key!r} ({t.scope_name!r}, id {t.scope_id})"
            for t in selection.targets
        )
        or "no role/scope pairs",
    )
    return selection


async def get_view_as_anonymous(
    anonymous: bool = Query(
        default=False,
        description=(
            "When true, nothing of the caller's own is included: no content shared directly with "
            "their email, and on the thread endpoints no own pending/rejected threads, no own "
            "votes (`my_vote` 0; vote counts stay exact) and no own pending edits. Works with or "
            "without `view_as` pairs. Cannot be combined with `view_as_user`."
        ),
    ),
) -> bool:
    """The ``anonymous`` flag every View As endpoint accepts — one
    dependency, so FastAPI reads it once per request for all its users."""
    return anonymous


async def get_view_as_selection(
    view_as: list[str] | None = Query(
        default=None,
        description=(
            "View As: a `role_key:scope_key` pair (`roles.key`:`scopes.key`) to adopt. "
            "Repeat the parameter to adopt several pairs, e.g. "
            "`?view_as=program_lead:program_a&view_as=program_member:program_b`."
        ),
    ),
    view_as_user: str | None = Query(default=None, description=VIEW_AS_USER_DESCRIPTION),
    anonymous: bool = Depends(get_view_as_anonymous),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> view_as_service.ViewAsSelection | None:
    """Parse and verify the View As parameters. ``None`` when neither was
    sent (blank values are ignored); the verified selection when everything
    passes; 400/403 listing every blocking item otherwise. ``anonymous``
    with ``view_as_user`` is a 400 (``view_as_user_with_anonymous``).

    Runs AFTER ``get_current_hub_user``, so authentication and first-login
    provisioning are never skipped. Endpoints that gate content on the hub
    tree use ``get_view_as_access`` below; endpoints that only need the
    adopted pairs (the assignment endpoints) depend on this directly.
    """
    values = view_as_service.normalize_pair_values(view_as)
    user_email = view_as_service.normalize_email(view_as_user)
    if not values and user_email is None:
        if anonymous:
            logger.info("Anonymous view requested by %s (own assignments, no direct shares)", current.email)
        return None

    if anonymous and user_email is not None:
        logger.warning("View As refused for %s: anonymous=true with view_as_user=%r", current.email, user_email)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "view_as_user_with_anonymous",
                "message": "anonymous=true cannot be combined with view_as_user.",
                "view_as_user": user_email,
            },
        )

    return _verify_view_as(db, current, values, user_email, anonymous=anonymous)


async def get_view_as_user_selection(
    view_as_user: str | None = Query(default=None, description=VIEW_AS_USER_DESCRIPTION),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> view_as_service.ViewAsSelection | None:
    """``get_view_as_selection`` for endpoints where only a selected USER
    means anything (``/v2/rbac/me/granted``: a role/scope pair never grants
    anything itself), so ``view_as`` pairs and ``anonymous`` are not
    accepted. Same verification, logging and errors."""
    user_email = view_as_service.normalize_email(view_as_user)
    if user_email is None:
        return None
    return _verify_view_as(db, current, [], user_email)


async def get_view_as_access(
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
    anonymous: bool = Depends(get_view_as_anonymous),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> ViewAsAccess:
    """``get_viewer_access`` for the read endpoints that support View As:
    exactly ``get_viewer_access`` without View As parameters, the selection's
    combined access with them (see ``get_view_as_selection``).

    Content shared DIRECTLY with a person is the selected user's under
    ``view_as_user``, the caller's own otherwise — unless ``anonymous``,
    which drops the caller's direct shares, with or without pairs. A Hub
    Admin keeps full access under ``anonymous`` (direct shares add nothing
    to it)."""
    if selection is None:
        if not anonymous:
            return ViewAsAccess(access=await get_viewer_access(current=current, db=db), view_as=False)
        closures = RbacClosures(db)
        if is_hub_admin(db, current, closures=closures):
            access = resolve_viewer_access(db, current.hub_user_id, is_admin=True, closures=closures)
        else:
            access = view_as_service.resolve_access_without_direct_grants(db, current.hub_user_id, closures=closures)
        return ViewAsAccess(access=access, view_as=False)

    if selection.user is not None:
        direct_grants_user_id = selection.user.id
    else:
        direct_grants_user_id = None if anonymous else current.hub_user_id
    access = view_as_service.resolve_view_as_access(db, selection.targets, user_id=direct_grants_user_id)
    return ViewAsAccess(access=access, view_as=True, view_as_user=selection.user_email)


async def get_view_as_user_access(
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_user_selection),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> ViewAsAccess:
    """``get_view_as_access`` for the endpoints that accept ``view_as_user``
    only (no pairs, no ``anonymous``): the caller's own access without it,
    the selected user's full access — assignments and direct shares — with
    it."""
    if selection is None:
        return ViewAsAccess(access=await get_viewer_access(current=current, db=db), view_as=False)
    access = view_as_service.resolve_view_as_access(db, selection.targets, user_id=selection.user.id)
    return ViewAsAccess(access=access, view_as=True, view_as_user=selection.user_email)


# ── Lock propagation (plan_lock_propagation_2026-09-08.md §4.3) ──────────


async def get_lock_view(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> edit_lock_service.LockView:
    """One ``LockView`` per request, mirroring ``get_viewer_access`` above —
    same shape, same place, same convention (§4.3's own wording). Depends
    on ``get_current_hub_user`` for the same reason ``get_viewer_access``
    does: ``current.email`` is what tells a node's derived state apart as
    "self" vs. a foreign holder."""
    return edit_lock_service.resolve_lock_view(db, current.email)


async def get_edit_session(
    request: Request,
    current: CurrentHubUser = Depends(get_current_hub_user),
) -> edit_lock_service.EditSession:
    """§5.1. `X-Edit-Tokens: <token>[,<token>]` — a request HEADER, not a
    body field, because the eight-plus mutating v2 schemas are all
    `StrictRequestModel` (`extra="forbid"`), so a body field would mean
    editing every one of them AND every client call site; a header is one
    function on each side. Sends every token the caller holds (in practice
    at most two — a root/ancestor session plus one surgical sub-grid
    session) so no call site needs to know which of the caller's sessions
    covers its own target — `require_live_session` checks all of them.

    No dependency on `get_db_v2` — parsing the header needs nothing from
    the database; only `require_live_session` (called downstream, inside
    each service function) does."""
    header = request.headers.get("X-Edit-Tokens", "")
    tokens = frozenset(t.strip() for t in header.split(",") if t.strip())
    return edit_lock_service.EditSession(holder=current.email, tokens=tokens)
