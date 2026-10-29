"""Hub Admin SOFT delete and Restore of a `users` row
(AI Docs/plan_admin_soft_delete_user_2026-10-19.md, which replaces the hard
delete of plan_admin_delete_user_2026-10-18.md).

Kept out of `rbac_assignment_service` so that module stays about
assignments. The router (`routers/rbac_assignments.py`) gates every entry
point on `require_hub_admin` and commits.

WHAT A DELETE TOUCHES (owner and colleague 2026-10-19, option B "keep
everything"): ONLY the user row. `is_deleted = true`, `deleted_at = now`,
`deleted_by = <the admin's email>`. Their role assignments, resource
grants, bookmarks, notifications and the `granted_by_*` attributions on
what they gave others all stay exactly as they are; the rows themselves
are the backlog, and a Restore brings all of it back.

What makes the kept rows inert is everyone ELSE's queries: every read that
decides access, an audience, the admin set, a picker or a roster filters
on `UserV2.not_deleted()` (plan §3.4). A deleted person cannot sign in,
whatever their domain (`dependencies._ensure_hub_user` raises
`HubUserRemoved`).

Edit locks they hold are released IN the delete's own transaction
(`_release_locks_held_by`). The router's post-commit sweep would also
release them (its holder lookup no longer sees a deleted row), but it is
best-effort and swallows errors, so the delete doesn't rely on it.

No audit table (owner, Q6 of the hard-delete plan): one `logger.info`
line per delete or restore. When the colleague's action-log table exists,
those lines become rows there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db_v2.models.bookmark import BookmarkV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.resource_grant import ResourceGrantV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.user import UserV2
from app.services.rbac_assignment_service import _hub_admin_pair_ids, admin_row_for_user, live_admin_pairs
from app.services.rbac_graph_service import RbacGraphError

if TYPE_CHECKING:
    from app.dependencies import CurrentHubUser

logger = logging.getLogger(__name__)

# A guard returns the error that refuses the delete, or None to allow it.
DeleteGuard = Callable[[Session, UserV2, "CurrentHubUser"], "RbacGraphError | None"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _user_emails(user: UserV2) -> list[str]:
    """Both login emails, normalized, blanks dropped."""
    emails: list[str] = []
    for raw in (user.work_email, user.alias_email):
        email = (raw or "").strip().lower()
        if email and email not in emails:
            emails.append(email)
    return emails


# ---------------------------------------------------------
# Guards
# ---------------------------------------------------------


def _refuse_already_deleted(db: Session, user: UserV2, current: "CurrentHubUser") -> RbacGraphError | None:
    if user.is_deleted:
        return RbacGraphError("already_deleted", "This user is already deleted.", user_id=user.id)
    return None


def _refuse_self(db: Session, user: UserV2, current: "CurrentHubUser") -> RbacGraphError | None:
    """Owner, Q3: an admin cannot delete their own row."""
    if user.id == current.hub_user_id:
        return RbacGraphError("cannot_delete_self", "You cannot delete your own user.", user_id=user.id)
    return None


def _refuse_last_hub_admin(db: Session, user: UserV2, current: "CurrentHubUser") -> RbacGraphError | None:
    """The admin floor for a user delete. A soft delete keeps the user's
    admin pair, but a deleted holder no longer counts as an admin, so this
    is a real removal of an admin and must not leave the hub with none.

    Same literal `(hub_admin, universal scope)` definition as
    `rbac_assignment_service._assert_not_the_last_hub_admin`, and like that
    guard: no `is_hub_admin` bypass, `has_signed_in` not counted, deleted
    holders not counted (`live_admin_pairs`). The one difference: that
    guard counts other ASSIGNMENTS (`id != a.id`); this one counts other
    USERS, since a user can hold two admin pairs and the delete removes
    both."""
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return None
    admin_pairs = RoleAssignmentV2.role_id == hub_admin_role_id, RoleAssignmentV2.scope_id.in_(universal_scope_ids)
    if not db.query(RoleAssignmentV2.id).filter(RoleAssignmentV2.user_id == user.id, *admin_pairs).first():
        return None
    other_admins = (
        live_admin_pairs(db, hub_admin_role_id, universal_scope_ids)
        .filter(RoleAssignmentV2.user_id != user.id)
        .with_entities(func.count(func.distinct(RoleAssignmentV2.user_id)))
        .scalar()
    )
    if other_admins:
        return None
    return RbacGraphError(
        "last_hub_admin",
        (
            "This user is the only Hub Admin on a hub-wide scope. Deleting "
            "them would leave the hub with nobody who can administer it. "
            "Assign Hub Admin to somebody else first."
        ),
        user_id=user.id,
    )


# Every guard runs, in order, on both the preview (all failures listed) and
# the delete (the first failure refuses). The extension point for future
# rules; the Airtable sync no longer adds one (sync plan §11, 2026-10-19).
DELETE_GUARDS: list[DeleteGuard] = [_refuse_already_deleted, _refuse_self, _refuse_last_hub_admin]


# ---------------------------------------------------------
# Counts
# ---------------------------------------------------------


def _locks_held(db: Session, emails: list[str]) -> int:
    """Fresh (non-stale) edit locks held under either email: what the delete
    releases."""
    from app.services import edit_lock_service

    held = 0
    for _node, row in edit_lock_service._held_rows(db):
        holder = (row.locked_by or "").strip().lower()
        if holder in emails and not edit_lock_service.is_lock_stale(row.locked_at):
            held += 1
    return held


def _counts(db: Session, user: UserV2, emails: list[str]) -> dict:
    """What the user has. A delete KEEPS all of it (and a Restore makes it
    live again); only `locks_held` is released by a delete."""
    notifications = db.query(NotificationV2).filter(func.lower(NotificationV2.recipient_email).in_(emails))
    return {
        "assignments": db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == user.id).count(),
        "resource_grants": db.query(ResourceGrantV2).filter(ResourceGrantV2.user_id == user.id).count(),
        "bookmarks": db.query(BookmarkV2).filter(BookmarkV2.user_id == user.id).count(),
        "locks_held": _locks_held(db, emails),
        "notifications": notifications.count() if emails else 0,
        "unread_notifications": (
            notifications.filter(NotificationV2.read_at.is_(None)).count() if emails else 0
        ),
        # Rows this user GRANTED to other people. Kept, attribution intact.
        "granted_by_others": {
            "assignments": db.query(RoleAssignmentV2)
            .filter(RoleAssignmentV2.granted_by_user_id == user.id, RoleAssignmentV2.user_id != user.id)
            .count(),
            "resource_grants": db.query(ResourceGrantV2)
            .filter(
                ResourceGrantV2.granted_by_user_id == user.id,
                (ResourceGrantV2.user_id.is_(None)) | (ResourceGrantV2.user_id != user.id),
            )
            .count(),
        },
    }


def _holds_hub_admin_pair(db: Session, user: UserV2) -> bool:
    """Does the user hold the literal `(hub_admin, universal)` pair? Read off
    the rows, not `is_hub_admin`: for a deleted user it answers "would be an
    admin again after a Restore", which is what the restore dialog warns
    about."""
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return False
    return (
        db.query(RoleAssignmentV2.id)
        .filter(
            RoleAssignmentV2.user_id == user.id,
            RoleAssignmentV2.role_id == hub_admin_role_id,
            RoleAssignmentV2.scope_id.in_(universal_scope_ids),
        )
        .first()
        is not None
    )


def _is_hub_admin(db: Session, user: UserV2) -> bool:
    """The same resolver every gate uses, for the "this person is a Hub
    Admin" line (owner, Q4). Local import: `app.dependencies` sits above
    the services.

    `model_construct` skips `UserInfo`'s `EmailStr` check: a stored email
    can be malformed (e.g. contains a space), and cleaning those up is one
    of the reasons to delete a user, so the preview must not 500 on it."""
    from app.dependencies import CurrentHubUser, is_hub_admin
    from app.models.auth import UserInfo

    email = user.email or ""
    if not email:
        return False
    synthetic = CurrentHubUser(
        info=UserInfo.model_construct(email=email, name=user.name or email, picture=None, roles=[]),
        hub_user_id=user.id,
        email=email,
    )
    return is_hub_admin(db, synthetic)


def _base(user: UserV2, current: "CurrentHubUser") -> dict:
    return {
        "id": user.id,
        "email": user.email or "",
        "name": user.name,
        "has_signed_in": user.has_signed_in,
        "is_self": user.id == current.hub_user_id,
        "is_deleted": bool(user.is_deleted),
        "deleted_at": user.deleted_at,
        "deleted_by": user.deleted_by,
    }


# ---------------------------------------------------------
# Delete
# ---------------------------------------------------------


def delete_preview(db: Session, user_id: int, current: "CurrentHubUser") -> dict | None:
    """Read-only. What the user has (all of it kept), what a delete would
    release (their locks), and every guard that would refuse it, so the
    confirm dialog can show that before the click. None when there is no
    such user."""
    user = db.get(UserV2, user_id)
    if user is None:
        return None
    emails = _user_emails(user)
    blockers = []
    for guard in DELETE_GUARDS:
        error = guard(db, user, current)
        if error is not None:
            blockers.append({"code": error.code, "message": error.message})
    return {
        **_base(user, current),
        "is_hub_admin": _is_hub_admin(db, user),
        "counts": _counts(db, user, emails),
        "blockers": blockers,
    }


def _lock_admin_pairs(db: Session) -> None:
    """Row-lock every admin pair before the last-admin guard counts them,
    so two admins deleting each other at once cannot both pass it: the
    second waits, then sees the first's delete. A no-op on SQLite.

    The soft delete writes the `users` row, not the pair; the lock still
    serializes the two, and the guard's count is a new statement after the
    wait, so under READ COMMITTED it sees the first delete's
    `is_deleted = true` through `live_admin_pairs`' join."""
    if db.get_bind().dialect.name != "postgresql":
        return
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return
    (
        db.query(RoleAssignmentV2.id)
        .filter(
            RoleAssignmentV2.role_id == hub_admin_role_id,
            RoleAssignmentV2.scope_id.in_(universal_scope_ids),
        )
        .with_for_update()
        .all()
    )


def _warn_if_admin_floor_nominal(db: Session, user: UserV2) -> None:
    """Same signal `_assert_not_the_last_hub_admin` logs: the delete is
    allowed, but every remaining (non-deleted) admin has never signed in."""
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return
    if not _holds_hub_admin_pair(db, user):
        return
    signed_in_remaining = (
        live_admin_pairs(db, hub_admin_role_id, universal_scope_ids)
        .filter(RoleAssignmentV2.user_id != user.id, UserV2.has_signed_in.is_(True))
        .count()
    )
    if signed_in_remaining == 0:
        logger.warning(
            "Admin floor is nominal: user %s was deleted, and none of the remaining "
            "hub-wide Hub Admin assignments belongs to a hub user who has signed in. "
            "BOOTSTRAP_ADMIN_EMAILS (plan §6.5) is the recovery path.",
            user.id,
        )


def _release_locks_held_by(db: Session, emails: list[str]) -> int:
    """Release every lock row held under either of the deleted user's
    emails, fresh or stale. `_force_break_lock`, the same release the
    revoke sweep uses (it keeps `locked_by` so a later write with their old
    token reads as TAKEN_OVER, and rotates the token). Other people's locks
    are never touched, so the admin-protection rules don't come into it."""
    from app.services import edit_lock_service

    released = 0
    for _node, row in edit_lock_service._held_rows(db):
        if (row.locked_by or "").strip().lower() in emails:
            edit_lock_service._force_break_lock(row)
            released += 1
    return released


def delete_user(db: Session, user_id: int, current: "CurrentHubUser") -> bool:
    """Soft-delete `user_id`. False when there is no such user (404).
    Raises the first failing guard's `RbacGraphError` (409). Flushes; the
    router commits."""
    user = db.query(UserV2).filter(UserV2.id == user_id).with_for_update().first()
    if user is None:
        return False

    _lock_admin_pairs(db)
    for guard in DELETE_GUARDS:
        error = guard(db, user, current)
        if error is not None:
            raise error

    emails = _user_emails(user)
    counts = _counts(db, user, emails)
    _warn_if_admin_floor_nominal(db, user)

    user.is_deleted = True
    user.deleted_at = _utc_now()
    user.deleted_by = (current.email or "").strip().lower() or None
    locks_released = _release_locks_held_by(db, emails)
    db.flush()

    logger.info(
        "User soft-deleted by %s: id=%s work_email=%s alias_email=%s; kept %s; locks released %s",
        current.email,
        user.id,
        user.work_email,
        user.alias_email,
        counts,
        locks_released,
    )
    return True


# ---------------------------------------------------------
# Restore
# ---------------------------------------------------------


def restore_preview(db: Session, user_id: int, current: "CurrentHubUser") -> dict | None:
    """Read-only. What a Restore brings back: every row was kept, so this is
    just what they hold. `restores_hub_admin` drives the dialog's warning.
    `blockers` has `not_deleted` for a user who isn't deleted. None when
    there is no such user."""
    user = db.get(UserV2, user_id)
    if user is None:
        return None
    emails = _user_emails(user)
    counts = _counts(db, user, emails)
    blockers = []
    if not user.is_deleted:
        blockers.append({"code": "not_deleted", "message": "This user is not deleted."})
    return {
        **_base(user, current),
        "restores_hub_admin": _holds_hub_admin_pair(db, user),
        "counts": {
            "assignments": counts["assignments"],
            "resource_grants": counts["resource_grants"],
            "bookmarks": counts["bookmarks"],
            "notifications": counts["notifications"],
        },
        "blockers": blockers,
    }


def restore_user(db: Session, user_id: int, current: "CurrentHubUser") -> dict | None:
    """Clear the soft-delete flag. Nothing is re-created or re-granted: every
    row was kept, so their access is live again as soon as this commits.
    `has_signed_in` is unchanged. None when there is no such user (404);
    raises `RbacGraphError("not_deleted")` (409). Flushes; the router
    commits. Returns the user's admin-table row."""
    user = db.query(UserV2).filter(UserV2.id == user_id).with_for_update().first()
    if user is None:
        return None
    if not user.is_deleted:
        raise RbacGraphError("not_deleted", "This user is not deleted.", user_id=user.id)

    deleted_at, deleted_by = user.deleted_at, user.deleted_by
    user.is_deleted = False
    user.deleted_at = None
    user.deleted_by = None
    db.flush()

    logger.info(
        "User restored by %s: id=%s work_email=%s alias_email=%s (deleted %s by %s)",
        current.email,
        user.id,
        user.work_email,
        user.alias_email,
        deleted_at,
        deleted_by,
    )
    return admin_row_for_user(db, user)
