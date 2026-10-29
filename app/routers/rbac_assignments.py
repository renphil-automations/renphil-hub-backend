"""Permission Management router
(plan_access_control_schema_2026-08-22.md §6,
session_handoff_2026-08-24-permission-management-plan.md §5).

Assignments — who holds what, and delegating that to others. Distinct from
`app/routers/rbac.py` (role/scope DEFINITIONS, Hub-Admin-gated writes): this
whole surface is open to any authenticated user, gated instead by §6.1's
delegation rule, evaluated per-request against the CALLER's own held
assignments (plus the unconditional Hub Admin bypass, handoff §3.1). No
route here uses the `require_hub_admin` DEPENDENCY — see its own docstring
for why assignments must not reuse it. They do call the shared
`is_hub_admin(db, current)` RESOLVER directly (§6.8, §10 item 6) to compute
that bypass, since `require_hub_admin` and this router's bypass must resolve
"is this caller a Hub Admin" identically even though only one of them is a
FastAPI dependency.

Every route depends on `get_current_hub_user`, not `get_current_user`: this
is the surface that made hub_users auto-provisioning a hard prerequisite
(handoff §3.2) rather than a nice-to-have, since nobody — including the
granter — can be resolved to a row without it.

This router also owns the one rule that keeps the ⊥ flags
("Any Role" / "Any Scope", renamed "User" / "Hub" 2026-10-14;
plan_access_control_algorithm_2026-08-27.md §4.4) from being a delegation
hole: they may never be assigned, except as the exact default pair every new
user gets. See `_assert_not_public`, which explains why it lives here and why
it runs where it runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import (
    CurrentHubUser,
    get_current_hub_user,
    get_view_as_selection,
    is_hub_admin,
    require_hub_admin,
)
from app.schemas.rbac_assignments import (
    AssignmentAPIResponse,
    AssignmentListAPIResponse,
    CreateAssignmentRequest,
    CreatedAssignmentAPIResponse,
    CreateHubUserRequest,
    DeleteUserPreviewAPIResponse,
    HubUserAdminAPIResponse,
    HubUserAdminListAPIResponse,
    HubUserListAPIResponse,
    RestoreUserPreviewAPIResponse,
    RevocableAssignmentListAPIResponse,
    UpdateAssignmentRequest,
)
from app.services import edit_lock_service, rbac_assignment_service, rbac_service, user_admin_service, view_as_service
from app.services.rbac_delegation_service import assert_can_delegate
from app.services.rbac_graph_service import RbacClosures, RbacGraphError

router = APIRouter(prefix="/v2/rbac", tags=["Permission Management"])


async def _release_now_unauthorized_locks(db: Session) -> None:
    """Called after a revoke/narrow write's OWN commit has already landed —
    a bug in here must never turn a successful revoke into a failed
    request. Same "advisory, never blocks the primary action" convention as
    every other release path in this codebase (`Sidebar.tsx`'s "Advisory
    unlock"). See `edit_lock_service.release_locks_now_unauthorized`'s own
    docstring for what this actually does, why it's `async` (a live
    Airtable check, fail-safe), and why."""
    try:
        await edit_lock_service.release_locks_now_unauthorized(db)
    except Exception:
        pass

ADMIN_ONLY = [Depends(require_hub_admin)]

VIEW_AS_ERROR_RESPONSES = {
    400: {"description": "View As: `anonymous=true` with `view_as_user`, a malformed `view_as` pair, an unknown key or user, or too many pairs"},
    403: {"description": "View As refused: the caller may not adopt one or more of the given pairs"},
}


def _delegation_identity(
    db: Session,
    current: CurrentHubUser,
    selection: view_as_service.ViewAsSelection | None,
    closures: RbacClosures,
) -> tuple[bool, list | None]:
    """``(is_hub_admin, held)`` for ``assert_can_delegate`` /
    ``list_revocable``: the caller's own assignments normally (``held=None``
    lets the delegation service load them), or exactly the verified View As
    selection — the given pairs plus the selected user's rows, one held row
    per pair. Direct-to-user grants play no part in delegation, so
    ``anonymous`` changes nothing here beyond its 400 with
    ``view_as_user``."""
    if selection is None:
        return is_hub_admin(db, current, closures=closures), None
    return (
        view_as_service.any_pair_is_hub_admin(db, selection.targets, closures=closures),
        view_as_service.held_closures_for(selection.targets, closures=closures),
    )


def _view_as_user(selection: view_as_service.ViewAsSelection | None) -> str | None:
    return selection.user_email if selection is not None else None


CONFLICT_RESPONSE = {
    409: {
        "description": (
            "The delegation rule, a uniqueness rule, the ⊥-not-assignable "
            "rule, or the admin floor was violated"
        )
    }
}
FORBIDDEN_RESPONSE = {403: {"description": "Hub Admin access required"}}

# TEMPORARY (owner decision, 2026-09-24, testing phase only): lets "Any
# Scope" (the ⊥ scope) be ASSIGNED to any user, under the normal delegation
# rules. "Any Role" stays refused regardless, except as the exact default
# pair (see `_assert_not_public`), which does not depend on this flag. This
# deliberately reopens the scope half of the hole `_assert_not_public`
# documents — anyone holding a non-leaf role can grant
# `(<a role beneath theirs>, Any Scope)` to anyone, and that reaches every
# object grant written on `(<that role>, Any Scope)`. Flip back to False (and
# the frontend twin, `ALLOW_ANY_SCOPE_ASSIGNMENT` in UserManagementPage.tsx)
# when testing ends. Existing ⊥ assignments are NOT cleaned up by flipping it
# — revoke them first, but leave the `(⊥, ⊥)` defaults alone.
TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT = True


def _conflict(error: RbacGraphError) -> HTTPException:
    """Same shape as rbac.py's own `_conflict` — duplicated rather than
    imported, since it is five lines and importing it would couple this
    router to that one for no reason beyond avoiding a duplicate. 409 for
    the same reason as there: every one of these is a collision with the
    CURRENT STATE (you don't hold a covering assignment right now, this
    pair already exists), not a malformed request."""
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": error.message, **error.details},
    )


def _assert_not_public(role: dict | None, scope: dict | None) -> None:
    """⊥ IS NEVER VALID AS AN ASSIGNMENT — EXCEPT AS THE EXACT DEFAULT PAIR
    (plan_access_control_algorithm_2026-08-27.md §4.4).

    THE EXCEPTION (owner decision, 2026-10-14): `(public role, public scope)`
    — "User" on "Hub" — is every new user's default assignment
    (`rbac_assignment_service.add_default_assignment`), and it may be granted
    back under the normal delegation rules once revoked. That pair is safe to
    let through where either half alone is not: it reaches only grants
    written on `(⊥, ⊥)`, i.e. content published to everyone, and its holder
    can delegate nothing (the public role has nothing strictly beneath it).
    The public role on any REAL scope stays refused: `(⊥, X)` would let
    anyone holding a role on X add anyone to X's "everyone" audience. The
    exception sits above `TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT` and does not
    depend on it, so it survives that flag being turned off.

    "Any Role" and "Any Scope" are the two lattice bottoms. They exist so
    that a grant written on an OBJECT can be reached from whatever anyone
    holds — which is exactly why holding one yourself is meaningless: it
    confers only what every single person already reaches. Each flag stays
    in its lane. `is_universal` is the one that belongs in an assignment
    ("every scope"); `is_public` is the one that belongs on an object grant.

    THIS IS A SECURITY CHECK, not a tidiness rule, and it is the other half
    of the flag rather than a follow-up to it. ⊥ sits in EVERY descendant
    set by construction, so `scope_id in scope_descendants(held)` is true
    for every user alive — the scope half of §6.1's delegation rule passes
    unconditionally against a ⊥ target, and the role half does the same for
    anyone holding any role but ⊥ itself. Without this refusal, adding the
    flags would hand every user in the hub the ability to grant
    `(anyone, Any Role, Any Scope)` — universal access, delegable by
    everybody, in a feature nobody is using yet. Ship the two together or
    ship neither.

    Hence the ORDER in `create_assignment`: this runs BEFORE
    `assert_can_delegate`, because the delegation gate is precisely what ⊥
    walks through. Running it after would be running it never.

    Deliberately NOT applied on the revoke path. A ⊥ row should not exist,
    but if one ever does — written before these flags landed, or straight
    into the database — refusing to revoke it would strand it permanently,
    with the widest reach in the system and no way to remove it through the
    UI. Refuse the way in, never the way out.

    `role`/`scope` are individually optional — a `None` here means "not part
    of this write", not "checked and fine", and is simply skipped. Both
    callers now pass the full pair the row will END UP holding (the admin
    update endpoint fills the unchanged half in from the existing row),
    because the exception above is a property of the pair, not of either
    half: editing only the scope of a default row must not yield `(⊥, X)`.

    TEMPORARILY WEAKENED for the scope half — see
    `TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT`.
    """
    if role is not None and scope is not None and role.get("is_public") and scope.get("is_public"):
        return
    for kind, row, key in (("role", role, "role_id"), ("scope", scope, "scope_id")):
        if row is None:
            continue
        if kind == "scope" and TEMP_ALLOW_PUBLIC_SCOPE_ASSIGNMENT:
            continue
        if row.get("is_public"):
            raise RbacGraphError(
                "public_not_assignable",
                (
                    f"{row['name']!r} is the public {kind} — the bottom of the "
                    f"{kind} lattice. It can only be assigned as the default "
                    f"pair, the public role on the public scope. Otherwise it "
                    f"is meant for grants written ON CONTENT, to publish "
                    f"something to the whole hub."
                ),
                **{key: row["id"]},
            )


# ---------------------------------------------------------
# Reads — all "mine", all open to any authenticated user once resolved to a
# hub_users row.
# ---------------------------------------------------------


@router.get("/me/assignments", response_model=AssignmentListAPIResponse, summary="My held assignments")
def get_my_assignments(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    return {"data": rbac_assignment_service.list_my_assignments(db, current.hub_user_id)}


@router.get(
    "/me/granted",
    response_model=AssignmentListAPIResponse,
    summary="Assignments I personally granted (audit trail — may include ones I can no longer revoke)",
)
def get_my_granted(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    return {"data": rbac_assignment_service.list_granted_by_me(db, current.hub_user_id)}


@router.get(
    "/me/revocable",
    response_model=RevocableAssignmentListAPIResponse,
    summary="Every assignment, org-wide, I am currently eligible to revoke under §6.1",
    responses=VIEW_AS_ERROR_RESPONSES,
)
def get_my_revocable(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
):
    """Deliberately broader than `/me/granted` (design doc §6.3): revocation
    is symmetric, not ownership-based, so this can include assignments some
    other admin granted. A Hub Admin gets everything, unconditionally.

    With View As parameters, the answer is for a holder of exactly the
    given pairs plus the selected user's assignments, instead of the
    caller's own."""
    admin, held = _delegation_identity(db, current, selection, RbacClosures(db))
    data = rbac_assignment_service.list_revocable(db, current.hub_user_id, is_hub_admin=admin, held=held)
    return {"data": data, "view_as": selection is not None, "view_as_user": _view_as_user(selection)}


@router.get(
    "/hub-users",
    response_model=HubUserListAPIResponse,
    summary="Search people who have logged in at least once (target picker)",
)
def search_hub_users(
    q: str = Query(..., min_length=rbac_assignment_service.HUB_USER_SEARCH_MIN_LENGTH, max_length=320),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Requires a real search term (handoff §5.4's privacy question,
    resolved this way rather than an unrestricted dump): this is a people
    directory, a different kind of exposure than role/scope names."""
    return {"data": rbac_assignment_service.search_hub_users(db, q)}


# ---------------------------------------------------------
# Writes — gated on `assert_can_delegate`, never `require_hub_admin`.
# ---------------------------------------------------------


@router.post(
    "/assignments",
    response_model=CreatedAssignmentAPIResponse,
    status_code=201,
    summary="Grant a role on a scope to another user",
    responses={
        404: {"description": "Role or scope not found"},
        **CONFLICT_RESPONSE,
        **VIEW_AS_ERROR_RESPONSES,
    },
)
def create_assignment(
    request: CreateAssignmentRequest,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
):
    """`role_id`/`scope_id` not found stays a plain 404, matching every
    other `/v2/rbac` endpoint — checked BEFORE the delegation gate so a bad
    id never leaks whether it would otherwise have been delegable.

    The ⊥ refusal below sits between the two, and its position is not
    cosmetic — see `_assert_not_public`.

    With View As parameters, the delegation gate checks the verified
    pairs (given and the selected user's) instead of the caller's own assignments. The row is still
    recorded as granted by the real caller."""
    role = rbac_service.get_role(db, request.role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    scope = rbac_service.get_scope(db, request.scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")

    try:
        _assert_not_public(role, scope)
        # ONE SNAPSHOT (§8.2, §6.8): shared between the Hub Admin bypass and
        # the delegation check below rather than each building its own.
        closures = RbacClosures(db)
        admin, held = _delegation_identity(db, current, selection, closures)
        assert_can_delegate(
            db,
            granter_hub_user_id=current.hub_user_id,
            is_hub_admin=admin,
            role_id=request.role_id,
            scope_id=request.scope_id,
            closures=closures,
            held=held,
        )
        assignment = rbac_assignment_service.create_assignment(
            db,
            granter_hub_user_id=current.hub_user_id,
            granter_email=current.email,
            target_email=request.user_email,
            role_id=request.role_id,
            scope_id=request.scope_id,
        )
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
    return {"data": assignment, "view_as": selection is not None, "view_as_user": _view_as_user(selection)}


@router.delete(
    "/assignments/{assignment_id}",
    status_code=204,
    summary="Revoke an assignment",
    responses={
        404: {"description": "Assignment not found"},
        **CONFLICT_RESPONSE,
        **VIEW_AS_ERROR_RESPONSES,
    },
)
async def delete_assignment(
    assignment_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
    selection: view_as_service.ViewAsSelection | None = Depends(get_view_as_selection),
):
    """Symmetric with create, not ownership-based (design doc §6.3,
    restated as a landmine in handoff §7): the check re-runs §6.1 against
    the assignment's OWN role/scope, regardless of who originally granted
    it. There is deliberately no `granted_by_user_id == me` shortcut here.

    A SECOND, DIFFERENT REFUSAL can also come back as a 409 here: the admin
    floor, raised from inside `rbac_assignment_service.delete_assignment` as
    `last_hub_admin`. It is NOT gated on `is_hub_admin` and must never be —
    it is an integrity rule rather than an authorization one, and a Hub Admin
    is precisely the person it exists to stop. See
    `_assert_not_the_last_hub_admin`'s docstring, which explains at length
    why the bypass every other gate applies first is inverted there.

    With View As parameters, the delegation gate checks the verified
    pairs (given and the selected user's) instead of the caller's own assignments. The admin floor
    is unaffected."""
    assignment = rbac_assignment_service.get_assignment(db, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    try:
        closures = RbacClosures(db)
        admin, held = _delegation_identity(db, current, selection, closures)
        assert_can_delegate(
            db,
            granter_hub_user_id=current.hub_user_id,
            is_hub_admin=admin,
            role_id=assignment.role_id,
            scope_id=assignment.scope_id,
            closures=closures,
            held=held,
        )
        rbac_assignment_service.delete_assignment(db, assignment_id)
    except RbacGraphError as e:
        raise _conflict(e)
    db.commit()
    # A user edited mid-session, then revoked here, would otherwise keep a
    # fully live lock for the rest of the TTL with no automatic way back —
    # see edit_lock_service.release_locks_now_unauthorized's own docstring.
    await _release_now_unauthorized_locks(db)


# ---------------------------------------------------------
# Admin user-management table (owner decision, 2026-09-12) — every hub user
# and every (role, scope) pair they hold, with in-place editing.
#
# THIS IS THE ONE PLACE IN THIS ROUTER GATED ON `require_hub_admin` RATHER
# THAN `assert_can_delegate`, and that is a deliberate, narrow exception to
# `app/dependencies.py`'s own note that assignment writes "must not reuse
# this dependency" — that note is about the CREATE/DELETE surface above,
# which stays exactly as delegation-gated as it always was. This is a
# separate, explicitly Hub-Admin-only management surface added alongside
# it, not a replacement: an admin here can edit ANY user's assignment
# regardless of what the admin's own held pairs would otherwise delegate,
# which is exactly what a management table is for and exactly why it must
# not be reachable by an ordinary delegating user.
#
# The admin floor still applies in full to Hub Admins here — see
# `rbac_assignment_service._assert_update_does_not_remove_last_hub_admin`,
# the update-shaped twin of the guard `delete_assignment` already runs, and
# note its docstring's reminder that this rule has no `is_hub_admin` bypass
# by design.
# ---------------------------------------------------------


@router.get(
    "/admin/hub-users",
    response_model=HubUserAdminListAPIResponse,
    summary="Every hub user and every role/scope pair they hold (Hub Admin management table)",
    responses=FORBIDDEN_RESPONSE,
    dependencies=ADMIN_ONLY,
)
def list_hub_users_admin(db: Session = Depends(get_db_v2)):
    """Unlike `/hub-users` above (the query-gated target picker), this
    returns everyone — including the users holding zero assignments, which
    is most of them — since the table this feeds exists precisely so an
    admin can find and assign those people, not just search for ones who
    already hold something."""
    return {"data": rbac_assignment_service.list_all_hub_users_with_assignments(db)}


@router.post(
    "/admin/hub-users",
    response_model=HubUserAdminAPIResponse,
    status_code=201,
    summary="Pre-register a hub user by email + name (Hub Admin only)",
    responses={
        409: {
            "description": (
                "A hub user with this email already exists (`hub_user_exists`), or it "
                "belongs to a deleted user (`hub_user_deleted`, with `user_id`: offer Restore)"
            )
        },
        **FORBIDDEN_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
def create_hub_user_admin(
    request: CreateHubUserRequest,
    db: Session = Depends(get_db_v2),
):
    """Owner decision, 2026-10-04: lets an admin add someone OUTSIDE the org
    email domains so they can sign in — `AuthService._enforce_login_allowed`
    admits any email with a `hub_users` row (Airtable Access Control remains
    a fallback until cutover). Org-domain emails are accepted too: they
    could sign in anyway, and pre-registering them lets an admin assign
    roles before their first login."""
    try:
        created = rbac_assignment_service.create_hub_user(
            db, email=str(request.email), name=request.name
        )
    except RbacGraphError as e:
        db.rollback()
        raise _conflict(e)
    db.commit()
    return {"data": created}


@router.get(
    "/admin/hub-users/{user_id}/delete-preview",
    response_model=DeleteUserPreviewAPIResponse,
    summary="What deleting this user keeps and releases, and what would refuse it (Hub Admin only)",
    responses={404: {"description": "User not found"}, **FORBIDDEN_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def delete_hub_user_preview(
    user_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Read-only (plan_admin_soft_delete_user_2026-10-19.md §3.1). `blockers`
    lists every guard in `user_admin_service.DELETE_GUARDS` that would
    refuse, so the confirm dialog can show it before the click."""
    preview = user_admin_service.delete_preview(db, user_id, current)
    if preview is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"data": preview}


@router.delete(
    "/admin/hub-users/{user_id}",
    status_code=204,
    summary="Soft-delete a user (Hub Admin only)",
    responses={
        404: {"description": "User not found"},
        409: {"description": "A delete guard refused: already deleted, self, or the last Hub Admin"},
        **FORBIDDEN_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
async def delete_hub_user_admin(
    user_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Owner and colleague, 2026-10-19 (plan_admin_soft_delete_user, option
    B): marks the row deleted and keeps everything else — assignments,
    grants, bookmarks, notifications, attributions. They can't sign in and
    drop out of every picker, list and audience; their locks are released.
    `POST .../restore` undoes it. See `user_admin_service`."""
    try:
        deleted = user_admin_service.delete_user(db, user_id, current)
    except RbacGraphError as e:
        db.rollback()
        raise _conflict(e)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    db.commit()
    # The delete released the user's own locks in its transaction; the sweep
    # (whose holder lookup no longer sees a deleted row) is only a backstop.
    await _release_now_unauthorized_locks(db)


@router.get(
    "/admin/hub-users/{user_id}/restore-preview",
    response_model=RestoreUserPreviewAPIResponse,
    summary="What restoring this deleted user brings back (Hub Admin only)",
    responses={404: {"description": "User not found"}, **FORBIDDEN_RESPONSE},
    dependencies=ADMIN_ONLY,
)
def restore_hub_user_preview(
    user_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Read-only (plan_admin_soft_delete_user §3.2). `restores_hub_admin`
    drives the dialog's Hub Admin warning."""
    preview = user_admin_service.restore_preview(db, user_id, current)
    if preview is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"data": preview}


@router.post(
    "/admin/hub-users/{user_id}/restore",
    response_model=HubUserAdminAPIResponse,
    summary="Restore a soft-deleted user (Hub Admin only)",
    responses={
        404: {"description": "User not found"},
        409: {"description": "The user is not deleted"},
        **FORBIDDEN_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
def restore_hub_user_admin(
    user_id: int,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
):
    """Clears the deleted flag. Nothing is re-created: every row was kept,
    so all of their access returns as it was. Returns the user's row of
    the admin table."""
    try:
        row = user_admin_service.restore_user(db, user_id, current)
    except RbacGraphError as e:
        db.rollback()
        raise _conflict(e)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    db.commit()
    return {"data": row}


@router.patch(
    "/admin/assignments/{assignment_id}",
    response_model=AssignmentAPIResponse,
    summary="Update an assignment's role and/or scope in place (Hub Admin only)",
    responses={
        400: {"description": "Neither role_id nor scope_id was provided"},
        404: {"description": "Assignment, role, or scope not found"},
        **CONFLICT_RESPONSE,
        **FORBIDDEN_RESPONSE,
    },
    dependencies=ADMIN_ONLY,
)
async def update_assignment_admin(
    assignment_id: int,
    request: UpdateAssignmentRequest,
    db: Session = Depends(get_db_v2),
):
    """404s for a bad `role_id`/`scope_id` are checked before the write,
    matching `create_assignment`'s ordering. `_assert_not_public` runs
    against the RESULTING pair — the provided half or halves plus the row's
    current value for the other — because the default-pair exception is a
    property of the pair (see its docstring)."""
    if not request.model_fields_set:
        raise HTTPException(
            status_code=400,
            detail="At least one of role_id or scope_id must be provided.",
        )

    role = None
    if request.role_id is not None:
        role = rbac_service.get_role(db, request.role_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Role not found")

    scope = None
    if request.scope_id is not None:
        scope = rbac_service.get_scope(db, request.scope_id)
        if scope is None:
            raise HTTPException(status_code=404, detail="Scope not found")

    # The ⊥ check is about the pair the row ends up holding (see
    # `_assert_not_public`), so fill the unchanged half in from the row.
    existing = rbac_assignment_service.get_assignment(db, assignment_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if role is None:
        role = rbac_service.get_role(db, existing.role_id)
    if scope is None:
        scope = rbac_service.get_scope(db, existing.scope_id)

    try:
        _assert_not_public(role, scope)
        updated = rbac_assignment_service.update_assignment(
            db,
            assignment_id,
            role_id=request.role_id,
            scope_id=request.scope_id,
        )
    except RbacGraphError as e:
        raise _conflict(e)

    if updated is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.commit()
    # Narrowing an assignment in place is just as much a revoke of the OLD
    # (role, scope) pair as delete_assignment above — same gap, same fix.
    await _release_now_unauthorized_locks(db)
    return {"data": updated}
