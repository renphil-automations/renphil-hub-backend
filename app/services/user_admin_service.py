"""Hub Admin hard-delete of a `users` row
(AI Docs/plan_admin_delete_user_2026-10-18.md, owner answers §8).

Kept out of `rbac_assignment_service` so that module stays about
assignments. The router (`routers/rbac_assignments.py`) gates both entry
points on `require_hub_admin`, commits, and then runs the lock sweep.

WHAT A DELETE TOUCHES (plan §1):
- `role_assignments` / `resource_grants` rows the user HOLDS are deleted.
  Postgres would cascade them anyway (`ON DELETE CASCADE`); they are deleted
  explicitly so the result doesn't depend on the dialect (SQLite tests).
- Rows the user GRANTED to other people stay, with `granted_by_user_id`
  nulled (the FK's `SET NULL`, again done explicitly); the
  `granted_by_email` snapshot is kept.
- Their notifications (`recipient_email` = either of their emails) are
  deleted (owner, Q5). Threads, comments, votes, mentions and moderation
  history are keyed by email with no FK and are KEPT.
- Bookmarks: the delete is REFUSED while any exist (owner, Q2/Q2b). The
  bookmarks FK is NO ACTION and its code belongs to a colleague; neither is
  touched here. If the FK later becomes `ON DELETE CASCADE`, drop
  `_refuse_has_bookmarks` from `DELETE_GUARDS`.
- Edit locks they hold are released by the router's post-commit sweep
  (`edit_lock_service.release_locks_now_unauthorized` releases any holder
  with no `users` row).
- No audit table (owner, Q6): one `logger.info` line. When the colleague's
  action-log table exists, that line becomes a row there.

A deleted ORG-DOMAIN person comes back on their next sign-in (or their
next request, with a live token); that is accepted and the confirm dialog
says so. An external person does not: `dependencies._ensure_hub_user` only
inserts a non-org-domain row from the OAuth callback and dev-login (plan §4,
option B).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db_v2.models.bookmark import BookmarkV2
from app.db_v2.models.notification import NotificationV2
from app.db_v2.models.resource_grant import ResourceGrantV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.user import UserV2
from app.services.rbac_assignment_service import _hub_admin_pair_ids
from app.services.rbac_graph_service import RbacGraphError

if TYPE_CHECKING:
    from app.dependencies import CurrentHubUser

logger = logging.getLogger(__name__)

# A guard returns the error that refuses the delete, or None to allow it.
DeleteGuard = Callable[[Session, UserV2, "CurrentHubUser"], "RbacGraphError | None"]


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


def _refuse_self(db: Session, user: UserV2, current: "CurrentHubUser") -> RbacGraphError | None:
    """Owner, Q3: an admin cannot delete their own row."""
    if user.id == current.hub_user_id:
        return RbacGraphError("cannot_delete_self", "You cannot delete your own user.", user_id=user.id)
    return None


def _refuse_last_hub_admin(db: Session, user: UserV2, current: "CurrentHubUser") -> RbacGraphError | None:
    """The admin floor for a user delete. The CASCADE on
    `role_assignments.user_id` would otherwise remove the last admin's pair
    without passing `rbac_assignment_service._assert_not_the_last_hub_admin`.

    Same literal `(hub_admin, universal scope)` definition, and like that
    guard: no `is_hub_admin` bypass, `has_signed_in` not counted. The one
    difference: that guard counts other ASSIGNMENTS (`id != a.id`); this one
    counts other USERS, since a user can hold two admin pairs and the
    delete removes both."""
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return None
    admin_pairs = RoleAssignmentV2.role_id == hub_admin_role_id, RoleAssignmentV2.scope_id.in_(universal_scope_ids)
    if not db.query(RoleAssignmentV2.id).filter(RoleAssignmentV2.user_id == user.id, *admin_pairs).first():
        return None
    other_admins = (
        db.query(func.count(func.distinct(RoleAssignmentV2.user_id)))
        .filter(RoleAssignmentV2.user_id != user.id, *admin_pairs)
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


def _refuse_has_bookmarks(db: Session, user: UserV2, current: "CurrentHubUser") -> RbacGraphError | None:
    """Owner, Q2: refuse while the user has bookmarks (the FK is NO ACTION,
    and the bookmarks code is not this feature's to change)."""
    n = db.query(func.count(BookmarkV2.id)).filter(BookmarkV2.user_id == user.id).scalar() or 0
    if not n:
        return None
    noun = "bookmark" if n == 1 else "bookmarks"
    return RbacGraphError(
        "user_has_bookmarks",
        f"This user has {n} {noun}. Delete the bookmarks to delete the user.",
        user_id=user.id,
        bookmarks=n,
    )


# Every guard runs, in order, on both the preview (all failures listed) and
# the delete (the first failure refuses). Airtable sync §11 appends
# `_refuse_still_in_airtable` here.
DELETE_GUARDS: list[DeleteGuard] = [_refuse_self, _refuse_last_hub_admin, _refuse_has_bookmarks]


# ---------------------------------------------------------
# Counts
# ---------------------------------------------------------


def _locks_held(db: Session, emails: list[str]) -> int:
    """Fresh (non-stale) edit locks held under either email: what the
    post-delete sweep will release."""
    from app.services import edit_lock_service

    held = 0
    for _node, row in edit_lock_service._held_rows(db):
        holder = (row.locked_by or "").strip().lower()
        if holder in emails and not edit_lock_service.is_lock_stale(row.locked_at):
            held += 1
    return held


def _counts(db: Session, user: UserV2, emails: list[str]) -> dict:
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
        # Rows this user GRANTED to other people. Kept by the delete, with
        # `granted_by_user_id` nulled.
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


def _is_hub_admin(db: Session, user: UserV2) -> bool:
    """The same resolver every gate uses, for the "this person is a Hub
    Admin" line (owner, Q4). Local import: `app.dependencies` sits above
    the services."""
    from app.dependencies import CurrentHubUser, is_hub_admin
    from app.models.auth import UserInfo

    email = user.email or ""
    if not email:
        return False
    synthetic = CurrentHubUser(
        info=UserInfo(email=email, name=user.name or email, roles=[]),
        hub_user_id=user.id,
        email=email,
    )
    return is_hub_admin(db, synthetic)


# ---------------------------------------------------------
# Entry points
# ---------------------------------------------------------


def delete_preview(db: Session, user_id: int, current: "CurrentHubUser") -> dict | None:
    """Read-only. What a delete would remove and keep, and every guard that
    would refuse it, so the confirm dialog can show that before the click.
    None when there is no such user."""
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
        "id": user.id,
        "email": user.email or "",
        "name": user.name,
        "has_signed_in": user.has_signed_in,
        "is_self": user.id == current.hub_user_id,
        "is_hub_admin": _is_hub_admin(db, user),
        "counts": _counts(db, user, emails),
        "blockers": blockers,
    }


def _lock_admin_pairs(db: Session) -> None:
    """Row-lock every admin pair before the last-admin guard counts them,
    so two admins deleting each other at once cannot both pass it: the
    second waits, then sees the first's delete. A no-op on SQLite."""
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
    allowed, but every remaining admin has never signed in."""
    hub_admin_role_id, universal_scope_ids = _hub_admin_pair_ids(db)
    if hub_admin_role_id is None or not universal_scope_ids:
        return
    admin_pairs = RoleAssignmentV2.role_id == hub_admin_role_id, RoleAssignmentV2.scope_id.in_(universal_scope_ids)
    if not db.query(RoleAssignmentV2.id).filter(RoleAssignmentV2.user_id == user.id, *admin_pairs).first():
        return
    signed_in_remaining = (
        db.query(RoleAssignmentV2)
        .join(UserV2, UserV2.id == RoleAssignmentV2.user_id)
        .filter(RoleAssignmentV2.user_id != user.id, UserV2.has_signed_in.is_(True), *admin_pairs)
        .count()
    )
    if signed_in_remaining == 0:
        logger.warning(
            "Admin floor is nominal: user %s was deleted, and none of the remaining "
            "hub-wide Hub Admin assignments belongs to a hub user who has signed in. "
            "BOOTSTRAP_ADMIN_EMAILS (plan §6.5) is the recovery path.",
            user.id,
        )


def delete_user(db: Session, user_id: int, current: "CurrentHubUser") -> bool:
    """Hard-delete `user_id`. False when there is no such user (404).
    Raises the first failing guard's `RbacGraphError` (409). Flushes; the
    router commits, then runs the lock sweep."""
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
    snapshot = {
        "id": user.id,
        "work_email": user.work_email,
        "alias_email": user.alias_email,
        "name": user.name,
        "has_signed_in": user.has_signed_in,
    }
    _warn_if_admin_floor_nominal(db, user)

    if emails:
        db.query(NotificationV2).filter(func.lower(NotificationV2.recipient_email).in_(emails)).delete(
            synchronize_session=False
        )
    db.query(RoleAssignmentV2).filter(RoleAssignmentV2.user_id == user.id).delete(synchronize_session=False)
    db.query(ResourceGrantV2).filter(ResourceGrantV2.user_id == user.id).delete(synchronize_session=False)
    db.query(RoleAssignmentV2).filter(RoleAssignmentV2.granted_by_user_id == user.id).update(
        {RoleAssignmentV2.granted_by_user_id: None}, synchronize_session=False
    )
    db.query(ResourceGrantV2).filter(ResourceGrantV2.granted_by_user_id == user.id).update(
        {ResourceGrantV2.granted_by_user_id: None}, synchronize_session=False
    )
    db.delete(user)
    db.flush()

    logger.info(
        "User deleted by %s: %s; counts %s",
        current.email,
        snapshot,
        counts,
    )
    return True
