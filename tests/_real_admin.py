"""Shared test helper: seed a REAL Hub Admin.

Since phase 3 (2026-10-12) `is_hub_admin` is `BOOTSTRAP_ADMIN_EMAILS` OR a
`(hub_admin, universal scope)` `role_assignments` row. The JWT "Hub Admin"
claim alone makes nobody an admin, and the bootstrap list is disabled
(owner, 2026-10-13). A fixture that wants an admin caller must therefore
seed an assignment row; never the bootstrap list.

Same shape as `tests/test_threads.py::_ThreadFixture._seed_real_admin`
(2026-10-19 handoff): written through its OWN short-lived session, one
commit per row. Every session in these fixtures shares one StaticPool
connection, so seeding through the test's `self.db` can (a) expire
instances the test still holds and (b) leave a flushed row for a leaked
request session's rollback-on-return to discard.

Reuses an existing `hub_admin` role and universal scope (only one of each
may exist), and is idempotent per email.

Not a test module (leading underscore), so unittest never collects it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db_v2.models.role import RoleV2
from app.db_v2.models.role_assignment import RoleAssignmentV2
from app.db_v2.models.scope import ScopeV2
from app.db_v2.models.user import UserV2
from app.dependencies import HUB_ADMIN_ROLE_KEY

ALL_SCOPES_KEY = "all_scopes"


def seed_real_admin(session_factory, email: str) -> int:
    """Give `email` a `(hub_admin, universal scope)` assignment. Returns the
    user's id (look the row up in your own session if you need it)."""
    now = datetime.now(timezone.utc)
    normalized = email.strip().lower()
    db = session_factory()
    try:
        role = db.query(RoleV2).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).first()
        if role is None:
            role = RoleV2(key=HUB_ADMIN_ROLE_KEY, name="Hub Admin", is_public=False, is_system=False, created_at=now)
            db.add(role)
            db.commit()
        scope = db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).first()
        if scope is None:
            scope = ScopeV2(
                key=ALL_SCOPES_KEY,
                name="All Scopes",
                is_universal=True,
                is_public=False,
                is_system=False,
                created_at=now,
            )
            db.add(scope)
            db.commit()
        user = db.query(UserV2).filter(UserV2.matches_email(normalized)).first()
        if user is None:
            user = UserV2(work_email=normalized, has_signed_in=True, created_at=now)
            db.add(user)
            db.commit()
        role_id, scope_id, user_id = role.id, scope.id, user.id
        held = (
            db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.user_id == user_id,
                RoleAssignmentV2.role_id == role_id,
                RoleAssignmentV2.scope_id == scope_id,
            )
            .first()
        )
        if held is None:
            db.add(RoleAssignmentV2(user_id=user_id, role_id=role_id, scope_id=scope_id, created_at=now))
            db.commit()
        return user_id
    finally:
        db.close()


def revoke_real_admin(session_factory, email: str) -> None:
    """Delete `email`'s `(hub_admin, universal)` assignment(s), straight from
    the database (own session). For a test whose HOLDER only needed the
    admin bypass to TAKE a lock on an orphaned fixture tab, and which then
    exercises a force takeover of that lock: an admin's session can't be
    taken over (project_ancestor_takeover_admin_protection), so the holder
    is turned back into an ordinary holder after acquiring. The lock row
    itself is untouched."""
    normalized = email.strip().lower()
    db = session_factory()
    try:
        role = db.query(RoleV2).filter(RoleV2.key == HUB_ADMIN_ROLE_KEY).first()
        user = db.query(UserV2).filter(UserV2.matches_email(normalized)).first()
        if role is None or user is None:
            return
        universal_ids = [s.id for s in db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).all()]
        (
            db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.user_id == user.id,
                RoleAssignmentV2.role_id == role.id,
                RoleAssignmentV2.scope_id.in_(universal_ids),
            )
            .delete(synchronize_session=False)
        )
        db.commit()
    finally:
        db.close()
