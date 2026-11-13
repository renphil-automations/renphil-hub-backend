"""Hub Admins SOFT-delete and restore a `users` row —
AI Docs/plan_admin_soft_delete_user_2026-10-19.md (replaces the hard delete
of plan_admin_delete_user_2026-10-18.md; the old version of this file is in
test_backups/tests_before_soft_delete_2026-10-19/).

  GET    /v2/rbac/admin/hub-users/{id}/delete-preview
  DELETE /v2/rbac/admin/hub-users/{id}
  GET    /v2/rbac/admin/hub-users/{id}/restore-preview
  POST   /v2/rbac/admin/hub-users/{id}/restore

A delete touches only the user row (option B, "keep everything"); every
query that reads OTHER people's rows ignores deleted users (plan §3.4); a
deleted person can't sign in, whatever their domain (§3.3).

Same seeded-org SQLite fixture as test_default_assignment.py: since phase 3
a JWT "Hub Admin" role makes nobody an admin, so the admin's
`(hub_admin, all_scopes)` row is written straight into the database.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-admin-delete-user-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.bookmark import BookmarkV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.notification import NotificationV2  # noqa: E402
from app.db_v2.models.resource_grant import ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.db_v2.models.thread import ThreadCommentV2, ThreadV2, ThreadVoteV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.dependencies import HubUserRemoved, _ensure_hub_user, get_auth_service  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.airtable import UserUpdate  # noqa: E402
from app.models.auth import TokenResponse, UserInfo  # noqa: E402
from app.routers import agent_access as agent_access_router  # noqa: E402
from app.routers import airtable as airtable_router  # noqa: E402
from app.services import (  # noqa: E402
    edit_lock_service,
    rbac_assignment_service,
    rbac_service,
    resource_grant_service,
    thread_service,
    user_admin_service,
    user_db_service,
)
from app.services.access_visibility_service import users_granted_on_node  # noqa: E402
from app.services.rbac_graph_service import RbacGraphError, audience_count, hub_admin_user_ids  # noqa: E402

ADMIN_EMAIL = "admin@renphil.org"
ADMIN2_EMAIL = "admin2@renphil.org"
GUEST_EMAIL = "guest@partner.example"
GUEST_ALIAS = "guest.alias@partner.example"
STAFF_EMAIL = "staff@renphil.org"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdminDeleteUserFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()
        self._seed()

        self.app = create_app()
        self.app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- seeding --------------------------------------------------------

    def _seed(self) -> None:
        db = self.db

        def role(key, name, public=False):
            row = RoleV2(key=key, name=name, is_public=public, is_system=False, created_at=_now())
            db.add(row)
            db.flush()
            return row.id

        def scope(key, name, universal=False, public=False):
            row = ScopeV2(key=key, name=name, is_universal=universal, is_public=public, is_system=False, created_at=_now())
            db.add(row)
            db.flush()
            return row.id

        self.admin_role = role("hub_admin", "Admin")
        self.member = role("member", "Member")
        db.add(RoleEdgeV2(parent_role_id=self.admin_role, child_role_id=self.member, created_at=_now()))
        self.all_scopes = scope("all_scopes", "All Scopes", universal=True)
        self.scope_a = scope("scope_a", "Scope A")
        self.scope_b = scope("scope_b", "Scope B")
        self.user_role = role("any_role", "User", public=True)
        self.hub_scope = scope("any_scope", "Hub", public=True)

        self.admin_id = self.add_user(ADMIN_EMAIL, "Admin")
        self.admin_pair = self.assign(self.admin_id, self.admin_role, self.all_scopes)
        self.guest_id = self.add_user(GUEST_EMAIL, "Guest", alias=GUEST_ALIAS)
        db.commit()

    def add_user(self, email: str, name: str, *, alias: str | None = None, signed_in: bool = True) -> int:
        row = UserV2(
            work_email=email, alias_email=alias, name=name, has_signed_in=signed_in, created_at=_now(), updated_at=_now()
        )
        self.db.add(row)
        self.db.flush()
        return row.id

    def assign(self, user_id: int, role_id: int, scope_id: int, *, granted_by: int | None = None) -> int:
        row = RoleAssignmentV2(
            user_id=user_id,
            role_id=role_id,
            scope_id=scope_id,
            granted_by_user_id=granted_by,
            granted_by_email=ADMIN_EMAIL if granted_by == self.admin_id else None,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row.id

    def user_grant(self, user_id: int, *, granted_by: int | None = None, tab_id: int = 1) -> int:
        """A per-user grant on a tab that doesn't exist (FKs off, see
        `fks_off`): the delete never looks at the node."""
        row = ResourceGrantV2(
            tab_id=tab_id,
            user_id=user_id,
            level="view",
            granted_by_user_id=granted_by,
            granted_by_email="granter@renphil.org" if granted_by else None,
            created_at=_now(),
        )
        with self.fks_off():
            self.db.add(row)
        return row.id

    @contextlib.contextmanager
    def fks_off(self):
        """Threads, notifications and grants need a component or tab; the
        delete doesn't care what they point at, so they are inserted with FK
        checks off."""
        self.db.commit()
        self.db.execute(text("PRAGMA foreign_keys=OFF"))
        try:
            yield
            self.db.commit()
        finally:
            self.db.execute(text("PRAGMA foreign_keys=ON"))

    def notify(self, recipient: str, *, read: bool = False) -> None:
        self.db.add(
            NotificationV2(
                recipient_email=recipient,
                type="mention",
                actor_email=ADMIN_EMAIL,
                component_id=1,
                thread_id=1,
                read_at=_now() if read else None,
                created_at=_now(),
            )
        )

    def hold_lock(self, slug: str, holder: str, *, stale: bool = False) -> int:
        locked_at = _now() - timedelta(days=30) if stale else _now()
        row = NavTabV2(slug=slug, protected=False, locked=True, locked_by=holder, locked_at=locked_at)
        self.db.add(row)
        self.db.commit()
        return row.id

    def soft_delete(self, user_id: int) -> None:
        """Mark a row deleted directly, for the audit tests that don't need
        to go through the endpoint."""
        user = self.db.get(UserV2, user_id)
        user.is_deleted = True
        user.deleted_at = _now()
        user.deleted_by = ADMIN_EMAIL
        self.db.commit()

    # -- http -----------------------------------------------------------

    def _auth(self, email: str) -> dict:
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def admin(self) -> dict:
        return self._auth(ADMIN_EMAIL)

    def preview(self, user_id: int, headers: dict | None = None):
        """The raw response; `.json()["data"]` is the preview."""
        return self.client.get(f"/v2/rbac/admin/hub-users/{user_id}/delete-preview", headers=headers or self.admin)

    def delete(self, user_id: int, headers: dict | None = None):
        return self.client.delete(f"/v2/rbac/admin/hub-users/{user_id}", headers=headers or self.admin)

    def restore_preview(self, user_id: int, headers: dict | None = None):
        return self.client.get(f"/v2/rbac/admin/hub-users/{user_id}/restore-preview", headers=headers or self.admin)

    def restore(self, user_id: int, headers: dict | None = None):
        return self.client.post(f"/v2/rbac/admin/hub-users/{user_id}/restore", headers=headers or self.admin)

    def fresh(self):
        self.db.expire_all()
        return self.db

    def row(self, user_id: int) -> UserV2 | None:
        return self.fresh().get(UserV2, user_id)

    def rows_for(self, email: str) -> int:
        return self.fresh().query(UserV2).filter(UserV2.matches_email(email)).count()

    def dev_login(self, email: str):
        os.environ["DEBUG"] = "True"
        try:
            return self.client.get("/auth/dev-login", params={"email": email}, follow_redirects=False)
        finally:
            os.environ.pop("DEBUG", None)


# ---------------------------------------------------------
# Access and the basic soft delete
# ---------------------------------------------------------


class TestAccess(AdminDeleteUserFixture):
    def test_unknown_user_is_404(self) -> None:
        self.assertEqual(self.preview(9999).status_code, 404)
        self.assertEqual(self.delete(9999).status_code, 404)
        self.assertEqual(self.restore_preview(9999).status_code, 404)
        self.assertEqual(self.restore(9999).status_code, 404)

    def test_non_admin_is_403(self) -> None:
        self.add_user(STAFF_EMAIL, "Staff")
        self.db.commit()
        staff = self._auth(STAFF_EMAIL)
        self.assertEqual(self.preview(self.guest_id, staff).status_code, 403)
        self.assertEqual(self.delete(self.guest_id, staff).status_code, 403)
        self.assertFalse(self.row(self.guest_id).is_deleted)
        self.soft_delete(self.guest_id)
        self.assertEqual(self.restore_preview(self.guest_id, staff).status_code, 403)
        self.assertEqual(self.restore(self.guest_id, staff).status_code, 403)
        self.assertTrue(self.row(self.guest_id).is_deleted)

    def test_the_row_survives_flagged_deleted(self) -> None:
        before = _now()
        res = self.delete(self.guest_id)
        self.assertEqual(res.status_code, 204, res.text)
        row = self.row(self.guest_id)
        self.assertIsNotNone(row)
        self.assertTrue(row.is_deleted)
        self.assertEqual(row.deleted_by, ADMIN_EMAIL)
        deleted_at = row.deleted_at if row.deleted_at.tzinfo else row.deleted_at.replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(deleted_at, before - timedelta(seconds=1))
        self.assertEqual(row.work_email, GUEST_EMAIL)  # the email stays reserved
        self.assertEqual(row.alias_email, GUEST_ALIAS)

    def test_the_admin_list_returns_deleted_users_flagged(self) -> None:
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        listed = {u["email"]: u for u in self.client.get("/v2/rbac/admin/hub-users", headers=self.admin).json()["data"]}
        self.assertTrue(listed[GUEST_EMAIL]["is_deleted"])
        self.assertEqual(listed[GUEST_EMAIL]["deleted_by"], ADMIN_EMAIL)
        self.assertIsNotNone(listed[GUEST_EMAIL]["deleted_at"])
        self.assertFalse(listed[ADMIN_EMAIL]["is_deleted"])
        self.assertIsNone(listed[ADMIN_EMAIL]["deleted_at"])

    def test_a_second_delete_is_409_already_deleted(self) -> None:
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        res = self.delete(self.guest_id)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "already_deleted")

    def test_an_hr_row_that_never_signed_in_is_deletable(self) -> None:
        hr_id = self.add_user("hr.person@renphil.org", "HR Person", signed_in=False)
        self.db.commit()
        self.assertEqual(self.delete(hr_id).status_code, 204)
        row = self.row(hr_id)
        self.assertTrue(row.is_deleted)
        self.assertFalse(row.has_signed_in)

    def test_a_malformed_email_is_previewable_and_deletable(self) -> None:
        """2026-10-29: `aaditya.singh rathour@renphil.org` (a space) made the
        preview 500 — `UserInfo`'s `EmailStr` rejected it — so the dialog
        showed "Failed to fetch" and Delete stayed disabled. Cleaning up
        rows like this is one of the reasons to delete a user."""
        bad = "bad.space name@renphil.org"
        bad_id = self.add_user(bad, "Bad Email")
        self.db.commit()
        self.hold_lock("bad-email-tab", bad)
        res = self.preview(bad_id)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["blockers"], [])
        self.assertEqual(self.delete(bad_id).status_code, 204)
        self.assertTrue(self.row(bad_id).is_deleted)


# ---------------------------------------------------------
# Everything is kept
# ---------------------------------------------------------


class TestEverythingIsKept(AdminDeleteUserFixture):
    def test_assignments_grants_bookmarks_and_notifications_are_untouched(self) -> None:
        held = self.assign(self.guest_id, self.member, self.scope_a, granted_by=self.admin_id)
        default = self.assign(self.guest_id, self.user_role, self.hub_scope)
        grant = self.user_grant(self.guest_id, granted_by=self.admin_id)
        self.db.add(BookmarkV2(user_id=self.guest_id, bookmark_link="/x"))
        with self.fks_off():
            self.notify(GUEST_EMAIL)
            self.notify(GUEST_ALIAS.upper(), read=True)
            self.notify(ADMIN_EMAIL)

        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        db = self.fresh()
        self.assertEqual(
            sorted(a.id for a in db.query(RoleAssignmentV2).filter_by(user_id=self.guest_id)), sorted([held, default])
        )
        self.assertEqual(db.get(RoleAssignmentV2, held).granted_by_user_id, self.admin_id)
        self.assertEqual([g.id for g in db.query(ResourceGrantV2).filter_by(user_id=self.guest_id)], [grant])
        self.assertEqual(db.query(BookmarkV2).filter_by(user_id=self.guest_id).count(), 1)
        self.assertEqual(db.query(NotificationV2).count(), 3)

    def test_what_they_granted_others_keeps_its_attribution(self) -> None:
        self.assign(self.guest_id, self.admin_role, self.all_scopes)  # so they could grant
        other = self.add_user("other@renphil.org", "Other")
        given = RoleAssignmentV2(
            user_id=other,
            role_id=self.member,
            scope_id=self.scope_b,
            granted_by_user_id=self.guest_id,
            granted_by_email=GUEST_EMAIL,
            created_at=_now(),
        )
        self.db.add(given)
        grant_id = self.user_grant(other, granted_by=self.guest_id, tab_id=7)
        self.db.commit()
        given_id = given.id

        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        db = self.fresh()
        self.assertEqual(db.get(RoleAssignmentV2, given_id).granted_by_user_id, self.guest_id)
        self.assertEqual(db.get(RoleAssignmentV2, given_id).granted_by_email, GUEST_EMAIL)
        self.assertEqual(db.get(ResourceGrantV2, grant_id).granted_by_user_id, self.guest_id)

    def test_threads_comments_and_votes_are_kept(self) -> None:
        with self.fks_off():
            thread = ThreadV2(
                component_id=1, title="T", content="C", author_email=GUEST_EMAIL, mentions=[], created_at=_now()
            )
            self.db.add(thread)
            self.db.flush()
            self.db.add(
                ThreadCommentV2(thread_id=thread.id, content="c", author_email=GUEST_EMAIL, mentions=[], created_at=_now())
            )
            self.db.add(
                ThreadVoteV2(thread_id=thread.id, voter_email=GUEST_EMAIL, value=1, created_at=_now(), updated_at=_now())
            )
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        db = self.fresh()
        self.assertEqual(db.query(ThreadV2).filter_by(author_email=GUEST_EMAIL).count(), 1)
        self.assertEqual(db.query(ThreadCommentV2).filter_by(author_email=GUEST_EMAIL).count(), 1)
        self.assertEqual(db.query(ThreadVoteV2).filter_by(voter_email=GUEST_EMAIL).count(), 1)

    def test_their_locks_are_released_others_are_not(self) -> None:
        theirs = self.hold_lock("guest-tab", GUEST_EMAIL)
        by_alias = self.hold_lock("guest-alias-tab", GUEST_ALIAS)
        stale = self.hold_lock("guest-stale-tab", GUEST_EMAIL, stale=True)
        admins = self.hold_lock("admin-tab", ADMIN_EMAIL)
        self.assertEqual(self.preview(self.guest_id).json()["data"]["counts"]["locks_held"], 2)
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        db = self.fresh()
        for nav_tab_id in (theirs, by_alias, stale):
            self.assertFalse(db.get(NavTabV2, nav_tab_id).locked)
        self.assertTrue(db.get(NavTabV2, admins).locked)
        self.assertEqual(db.get(NavTabV2, admins).locked_by, ADMIN_EMAIL)

    def test_locks_are_released_even_without_the_router_sweep(self) -> None:
        """The delete releases them in its own transaction; the post-commit
        sweep is only a backstop."""
        theirs = self.hold_lock("guest-tab", GUEST_EMAIL)
        with mock.patch.object(edit_lock_service, "release_locks_now_unauthorized", side_effect=RuntimeError):
            self.assertEqual(self.delete(self.guest_id).status_code, 204)
        self.assertFalse(self.fresh().get(NavTabV2, theirs).locked)


# ---------------------------------------------------------
# Guards
# ---------------------------------------------------------


class TestGuards(AdminDeleteUserFixture):
    def test_bookmarks_no_longer_block(self) -> None:
        self.db.add(BookmarkV2(user_id=self.guest_id, bookmark_link="/x"))
        self.db.commit()
        self.assertEqual(self.preview(self.guest_id).json()["data"]["blockers"], [])
        self.assertEqual(self.delete(self.guest_id).status_code, 204)

    def test_self_delete_is_refused(self) -> None:
        self.assign(self.guest_id, self.admin_role, self.all_scopes)  # not the last admin
        self.db.commit()
        res = self.delete(self.admin_id)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["code"], "cannot_delete_self")
        self.assertFalse(self.row(self.admin_id).is_deleted)

    def test_the_last_admin_guard_counts_users_not_assignments(self) -> None:
        """The target holds TWO admin pairs and nobody else holds one.
        Counting other ASSIGNMENTS (as the revoke guard does) would find the
        target's second pair and allow it. Two admin pairs need two
        universal scopes, which `uq_scopes_single_universal` forbids, so the
        index is dropped for this test: the guard must not rely on it."""
        self.db.execute(text("DROP INDEX uq_scopes_single_universal"))
        second_universal = ScopeV2(
            key="all_scopes_2", name="All 2", is_universal=True, is_public=False, is_system=False, created_at=_now()
        )
        self.db.add(second_universal)
        self.db.flush()
        target = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(target, self.admin_role, self.all_scopes)
        self.assign(target, self.admin_role, second_universal.id)
        self.db.query(RoleAssignmentV2).filter_by(user_id=self.admin_id).delete()
        self.db.commit()

        user = self.row(target)
        error = user_admin_service._refuse_last_hub_admin(self.db, user, mock.Mock(hub_user_id=self.admin_id))
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "last_hub_admin")

    def test_last_admin_refused_over_http(self) -> None:
        """Admin A (let in by a patched resolver, holding no pair) tries to
        delete B, the only holder of an admin pair."""
        target = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(target, self.admin_role, self.all_scopes)
        self.db.query(RoleAssignmentV2).filter_by(user_id=self.admin_id).delete()
        self.db.commit()
        with mock.patch("app.dependencies.is_hub_admin", side_effect=lambda db, current, **kw: current.email == ADMIN_EMAIL):
            res = self.delete(target)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "last_hub_admin")
        self.assertFalse(self.row(target).is_deleted)

    def test_a_deleted_admin_does_not_count_as_the_other_admin(self) -> None:
        """B is deleted, so A is the last live admin: A's admin row is
        protected even though B's admin pair is still in the table."""
        b = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(b, self.admin_role, self.all_scopes)
        self.db.commit()
        self.soft_delete(b)
        user = self.row(self.admin_id)
        error = user_admin_service._refuse_last_hub_admin(self.db, user, mock.Mock(hub_user_id=b))
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "last_hub_admin")

    def test_a_non_last_admin_is_deletable(self) -> None:
        target = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(target, self.admin_role, self.all_scopes)
        self.db.commit()
        preview = self.preview(target).json()["data"]
        self.assertTrue(preview["is_hub_admin"])
        self.assertEqual(preview["blockers"], [])
        self.assertEqual(self.delete(target).status_code, 204)
        self.assertTrue(self.row(target).is_deleted)

    def test_an_appended_guard_blocks_the_delete(self) -> None:
        """`DELETE_GUARDS` stays the extension point."""

        def refuse_for_test(db, user, current):
            return RbacGraphError("test_guard", "Refused for the test.")

        guards = [*user_admin_service.DELETE_GUARDS, refuse_for_test]
        with mock.patch.object(user_admin_service, "DELETE_GUARDS", guards):
            preview = self.preview(self.guest_id).json()["data"]
            res = self.delete(self.guest_id)
        self.assertEqual([b["code"] for b in preview["blockers"]], ["test_guard"])
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["code"], "test_guard")
        self.assertFalse(self.row(self.guest_id).is_deleted)


class TestAdminFloorIgnoresDeletedAdmins(AdminDeleteUserFixture):
    """Plan §5: "Delete the only other admin, then try to demote yourself →
    refused." The revoke and update guards must not count a deleted admin."""

    def setUp(self) -> None:
        super().setUp()
        self.b = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.b_pair = self.assign(self.b, self.admin_role, self.all_scopes)
        self.db.commit()
        self.assertEqual(self.delete(self.b).status_code, 204)

    def test_revoking_your_own_admin_pair_is_refused(self) -> None:
        res = self.client.delete(f"/v2/rbac/assignments/{self.admin_pair}", headers=self.admin)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "last_hub_admin")

    def test_moving_your_own_admin_pair_off_admin_is_refused(self) -> None:
        res = self.client.patch(
            f"/v2/rbac/admin/assignments/{self.admin_pair}", json={"role_id": self.member}, headers=self.admin
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "last_hub_admin")

    def test_revoking_the_deleted_admins_pair_is_allowed(self) -> None:
        """Their pair isn't a live admin, so removing it can't lower the floor."""
        res = self.client.delete(f"/v2/rbac/assignments/{self.b_pair}", headers=self.admin)
        self.assertIn(res.status_code, (200, 204), res.text)
        self.assertIsNone(self.fresh().get(RoleAssignmentV2, self.b_pair))

    def test_after_restore_the_other_admin_counts_again(self) -> None:
        self.assertEqual(self.restore(self.b).status_code, 200)
        res = self.client.delete(f"/v2/rbac/assignments/{self.admin_pair}", headers=self.admin)
        self.assertIn(res.status_code, (200, 204), res.text)


# ---------------------------------------------------------
# The previews
# ---------------------------------------------------------


class TestPreview(AdminDeleteUserFixture):
    def test_counts_describe_what_is_kept(self) -> None:
        self.assign(self.guest_id, self.member, self.scope_a)
        self.assign(self.guest_id, self.user_role, self.hub_scope)
        self.user_grant(self.guest_id)
        other = self.add_user("other@renphil.org", "Other")
        self.db.add(
            RoleAssignmentV2(
                user_id=other, role_id=self.member, scope_id=self.scope_b, granted_by_user_id=self.guest_id, created_at=_now()
            )
        )
        self.user_grant(other, granted_by=self.guest_id, tab_id=9)
        self.db.add(BookmarkV2(user_id=self.guest_id, bookmark_link="/x"))
        with self.fks_off():
            self.notify(GUEST_EMAIL)
            self.notify(GUEST_ALIAS, read=True)
        self.hold_lock("guest-tab", GUEST_EMAIL)

        res = self.preview(self.guest_id)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()["data"]
        self.assertEqual(body["id"], self.guest_id)
        self.assertEqual(body["email"], GUEST_EMAIL)
        self.assertEqual(body["name"], "Guest")
        self.assertTrue(body["has_signed_in"])
        self.assertFalse(body["is_self"])
        self.assertFalse(body["is_deleted"])
        self.assertFalse(body["is_hub_admin"])
        self.assertEqual(body["blockers"], [])
        self.assertEqual(
            body["counts"],
            {
                "assignments": 2,
                "resource_grants": 1,
                "bookmarks": 1,
                "locks_held": 1,
                "notifications": 2,
                "unread_notifications": 1,
                "granted_by_others": {"assignments": 1, "resource_grants": 1},
            },
        )

    def test_preview_lists_every_blocker_and_writes_nothing(self) -> None:
        body = self.preview(self.admin_id).json()["data"]
        self.assertTrue(body["is_self"])
        self.assertTrue(body["is_hub_admin"])
        self.assertEqual([b["code"] for b in body["blockers"]], ["cannot_delete_self", "last_hub_admin"])
        self.assertFalse(self.row(self.admin_id).is_deleted)

    def test_preview_of_a_deleted_user_says_already_deleted(self) -> None:
        self.soft_delete(self.guest_id)
        body = self.preview(self.guest_id).json()["data"]
        self.assertTrue(body["is_deleted"])
        self.assertEqual(body["deleted_by"], ADMIN_EMAIL)
        self.assertEqual([b["code"] for b in body["blockers"]], ["already_deleted"])


# ---------------------------------------------------------
# Restore
# ---------------------------------------------------------


class TestRestore(AdminDeleteUserFixture):
    def test_restore_brings_everything_back(self) -> None:
        pair = self.assign(self.guest_id, self.member, self.scope_a)
        grant = self.user_grant(self.guest_id)
        self.db.commit()
        guest = self._auth(GUEST_EMAIL)
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        self.assertEqual(self.client.get("/v2/rbac/me/assignments", headers=guest).status_code, 401)

        res = self.restore(self.guest_id)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()["data"]
        self.assertFalse(body["is_deleted"])
        self.assertIsNone(body["deleted_at"])
        self.assertIsNone(body["deleted_by"])
        self.assertEqual([a["id"] for a in body["assignments"]], [pair])

        row = self.row(self.guest_id)
        self.assertFalse(row.is_deleted)
        self.assertIsNone(row.deleted_at)
        self.assertIsNone(row.deleted_by)
        self.assertIsNotNone(self.fresh().get(ResourceGrantV2, grant))

        mine = self.client.get("/v2/rbac/me/assignments", headers=guest)
        self.assertEqual(mine.status_code, 200, mine.text)
        self.assertEqual([a["id"] for a in mine.json()["data"]], [pair])
        found = self.client.get("/v2/rbac/hub-users", params={"q": "guest"}, headers=self.admin).json()["data"]
        self.assertIn(GUEST_EMAIL, {u["email"] for u in found})

    def test_restoring_a_user_who_is_not_deleted_is_409(self) -> None:
        res = self.restore(self.guest_id)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "not_deleted")
        preview = self.restore_preview(self.guest_id).json()["data"]
        self.assertEqual([b["code"] for b in preview["blockers"]], ["not_deleted"])

    def test_restore_preview_counts_and_hub_admin_warning(self) -> None:
        b = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(b, self.admin_role, self.all_scopes)
        self.assign(b, self.member, self.scope_a)
        self.user_grant(b)
        self.db.add(BookmarkV2(user_id=b, bookmark_link="/x"))
        self.db.commit()
        self.assertEqual(self.delete(b).status_code, 204)

        res = self.restore_preview(b)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()["data"]
        self.assertTrue(body["is_deleted"])
        self.assertTrue(body["restores_hub_admin"])
        self.assertEqual(body["blockers"], [])
        self.assertEqual(
            body["counts"], {"assignments": 2, "resource_grants": 1, "bookmarks": 1, "notifications": 0}
        )
        guest_preview = self.restore_preview(self.guest_id).json()["data"]
        self.assertFalse(guest_preview["restores_hub_admin"])

    def test_a_restored_hub_admin_is_an_admin_again(self) -> None:
        b = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(b, self.admin_role, self.all_scopes)
        self.db.commit()
        b_auth = self._auth(ADMIN2_EMAIL)
        self.assertEqual(self.delete(b).status_code, 204)
        self.assertNotIn(b, hub_admin_user_ids(self.fresh()))
        self.assertEqual(self.client.get("/v2/rbac/admin/hub-users", headers=b_auth).status_code, 401)

        self.assertEqual(self.restore(b).status_code, 200)
        self.assertIn(b, hub_admin_user_ids(self.fresh()))
        self.assertEqual(self.client.get("/v2/rbac/admin/hub-users", headers=b_auth).status_code, 200)

    def test_restore_leaves_has_signed_in_alone(self) -> None:
        hr_id = self.add_user("hr.person@renphil.org", "HR Person", signed_in=False)
        self.db.commit()
        self.assertEqual(self.delete(hr_id).status_code, 204)
        self.assertEqual(self.restore(hr_id).status_code, 200)
        self.assertFalse(self.row(hr_id).has_signed_in)


# ---------------------------------------------------------
# Sign-in refusal (plan §3.3)
# ---------------------------------------------------------


class TestSignIn(AdminDeleteUserFixture):
    def test_a_deleted_org_domain_user_gets_401_and_no_new_row(self) -> None:
        staff_id = self.add_user(STAFF_EMAIL, "Staff")
        self.db.commit()
        staff = self._auth(STAFF_EMAIL)
        self.assertEqual(self.delete(staff_id).status_code, 204)
        res = self.client.get("/v2/rbac/me/assignments", headers=staff)
        self.assertEqual(res.status_code, 401, res.text)
        self.assertEqual(res.json()["detail"]["code"], "user_removed")
        self.assertEqual(self.rows_for(STAFF_EMAIL), 1)
        self.assertTrue(self.row(staff_id).is_deleted)

    def test_a_deleted_external_user_gets_401_user_removed(self) -> None:
        guest = self._auth(GUEST_EMAIL)
        self.assertEqual(self.client.get("/v2/rbac/me/assignments", headers=guest).status_code, 200)
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        for headers in (guest, self._auth(GUEST_ALIAS)):
            res = self.client.get("/v2/rbac/me/assignments", headers=headers)
            self.assertEqual(res.status_code, 401, res.text)
            self.assertEqual(res.json()["detail"]["code"], "user_removed")
        self.assertEqual(self.rows_for(GUEST_EMAIL), 1)

    def test_a_deleted_user_who_never_signed_in_is_not_flipped(self) -> None:
        hr_id = self.add_user("hr.person@renphil.org", "HR Person", signed_in=False)
        self.db.commit()
        self.soft_delete(hr_id)
        res = self.client.get("/v2/rbac/me/assignments", headers=self._auth("hr.person@renphil.org"))
        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.row(hr_id).has_signed_in)

    def test_dev_login_refuses_a_deleted_user_and_creates_no_row(self) -> None:
        staff_id = self.add_user(STAFF_EMAIL, "Staff")
        self.db.commit()
        self.soft_delete(staff_id)
        self.soft_delete(self.guest_id)
        for email in (STAFF_EMAIL, GUEST_EMAIL):
            res = self.dev_login(email)
            self.assertEqual(res.status_code, 403, res.text)
            self.assertIn("removed", res.json()["detail"])
            self.assertEqual(self.rows_for(email), 1)

    def test_the_oauth_callback_refuses_a_deleted_user(self) -> None:
        staff_id = self.add_user(STAFF_EMAIL, "Staff")
        self.db.commit()
        self.soft_delete(staff_id)

        async def handle_callback(code, state, db):
            token = TokenResponse(access_token="t", email=STAFF_EMAIL, name="Staff", picture=None, roles=[])
            return token, "http://localhost:5000/auth/callback"

        self.app.dependency_overrides[get_auth_service] = lambda: SimpleNamespace(handle_callback=handle_callback)
        res = self.client.get("/auth/callback", params={"code": "c", "state": "s"}, follow_redirects=False)
        self.assertEqual(res.status_code, 403, res.text)
        self.assertIn("removed", res.json()["detail"])
        self.assertEqual(self.rows_for(STAFF_EMAIL), 1)

    def test_ensure_hub_user_raises_for_a_deleted_row_whatever_the_flag(self) -> None:
        self.soft_delete(self.guest_id)
        for allow in (False, True):
            with self.assertRaises(HubUserRemoved):
                _ensure_hub_user(self.db, email=GUEST_EMAIL, name="Guest", allow_insert_external=allow)
        self.assertEqual(self.rows_for(GUEST_EMAIL), 1)

    def test_the_agent_paths_refuse_a_deleted_user(self) -> None:
        self.soft_delete(self.guest_id)
        with self.assertRaises(HTTPException) as error:
            airtable_router._resolve_agent_granted_nodes(self.db, {"email": GUEST_EMAIL, "roles": []})
        self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as error:
            agent_access_router._build_live_acl_response(SimpleNamespace(email=GUEST_EMAIL, roles=[]), self.db)
        self.assertEqual(error.exception.status_code, 403)

    def test_the_login_gate_no_longer_admits_a_deleted_external_row(self) -> None:
        from app.helpers.exceptions import UserNotAuthorizedError
        from app.services.auth_service import AuthService
        from app.config import get_settings

        self.soft_delete(self.guest_id)
        service = AuthService(get_settings())

        with self.assertRaises(UserNotAuthorizedError):
            service._enforce_login_allowed(GUEST_EMAIL, self.db)


class TestReCreation(AdminDeleteUserFixture):
    """Option B (2026-10-18) still holds for an email with NO row at all."""

    def test_the_oauth_callback_path_may_insert_an_external_row(self) -> None:
        user = _ensure_hub_user(self.db, email="new@partner.example", name="New", allow_insert_external=True)
        self.assertIsNotNone(user)
        self.assertTrue(user.has_signed_in)
        self.assertIsNone(_ensure_hub_user(self.db, email="lazy@partner.example", name="Lazy"))
        self.assertEqual(self.rows_for("lazy@partner.example"), 0)

    def test_the_agent_path_refuses_an_unknown_external_email(self) -> None:
        with self.assertRaises(HTTPException) as error:
            airtable_router._resolve_agent_granted_nodes(self.db, {"email": "agent.user@partner.example", "roles": []})
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.rows_for("agent.user@partner.example"), 0)


# ---------------------------------------------------------
# §3.4: deleted users count toward nothing
# ---------------------------------------------------------


class TestDeletedUsersCountTowardNothing(AdminDeleteUserFixture):
    def test_audiences_exclude_deleted_users(self) -> None:
        self.assign(self.guest_id, self.member, self.scope_a)
        self.db.commit()
        self.assertEqual(audience_count(self.fresh(), self.member, self.scope_a), 2)  # admin reaches it too
        self.soft_delete(self.guest_id)
        self.assertEqual(audience_count(self.fresh(), self.member, self.scope_a), 1)

    def test_hub_admin_set_and_lock_admin_holders_exclude_deleted_admins(self) -> None:
        b = self.add_user(ADMIN2_EMAIL, "Admin Two")
        self.assign(b, self.admin_role, self.all_scopes)
        self.db.commit()
        self.assertEqual(hub_admin_user_ids(self.fresh()), {self.admin_id, b})
        self.assertEqual(edit_lock_service._admin_holder_emails(self.db, {ADMIN2_EMAIL}), {ADMIN2_EMAIL})
        self.soft_delete(b)
        self.assertEqual(hub_admin_user_ids(self.fresh()), {self.admin_id})
        self.assertEqual(edit_lock_service._admin_holder_emails(self.fresh(), {ADMIN2_EMAIL}), set())

    def test_people_search_excludes_deleted_users(self) -> None:
        self.soft_delete(self.guest_id)
        found = self.client.get("/v2/rbac/hub-users", params={"q": "guest"}, headers=self.admin).json()["data"]
        self.assertEqual(found, [])

    def test_a_deleted_user_is_not_a_grant_target(self) -> None:
        self.soft_delete(self.guest_id)
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": GUEST_EMAIL, "role_id": self.member, "scope_id": self.scope_a},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "target_not_provisioned")
        with self.assertRaises(RbacGraphError) as error:
            resource_grant_service._assert_principal(self.db, None, None, self.guest_id)
        self.assertEqual(error.exception.code, "user_deleted")

    def test_revocable_excludes_deleted_users_rows(self) -> None:
        pair = self.assign(self.guest_id, self.member, self.scope_a)
        self.db.commit()
        ids = lambda: {a["id"] for a in rbac_assignment_service.list_revocable(self.fresh(), self.admin_id, is_hub_admin=True)}  # noqa: E731
        self.assertIn(pair, ids())
        self.soft_delete(self.guest_id)
        self.assertNotIn(pair, ids())

    def test_grant_panel_rows_flag_a_deleted_grantee(self) -> None:
        grant = self.user_grant(self.guest_id)
        rows = resource_grant_service.list_grants_for_node(self.fresh(), "tab", 1)
        self.assertEqual([(r["id"], r["user_is_deleted"]) for r in rows], [(grant, False)])
        self.soft_delete(self.guest_id)
        rows = resource_grant_service.list_grants_for_node(self.fresh(), "tab", 1)
        self.assertEqual([(r["id"], r["user_is_deleted"]) for r in rows], [(grant, True)])
        self.assertTrue(resource_grant_service.get_grant(self.fresh(), grant)["user_is_deleted"])
        self.assertTrue(resource_grant_service.list_grants_by_ids(self.fresh(), [grant])[0]["user_is_deleted"])

    def test_a_deleted_users_grant_makes_nothing_visible(self) -> None:
        hub = HubV2(created_at=datetime.now())
        self.db.add(hub)
        self.db.flush()
        self.db.add(ResourceGrantV2(hub_id=hub.id, user_id=self.guest_id, level="view", created_at=_now()))
        self.db.commit()
        self.assertIn(self.guest_id, users_granted_on_node(self.fresh(), ("hub", hub.id)))
        self.soft_delete(self.guest_id)
        granted = users_granted_on_node(self.fresh(), ("hub", hub.id))
        self.assertNotIn(self.guest_id, granted)
        self.assertIn(self.admin_id, granted)

    def test_the_mention_roster_excludes_deleted_users(self) -> None:
        emails = lambda: {u.work_email for u in thread_service._eligible_mentionable_users_query(self.fresh()).all()}  # noqa: E731
        self.assertIn(GUEST_EMAIL, emails())
        self.soft_delete(self.guest_id)
        self.assertNotIn(GUEST_EMAIL, emails())

    def test_a_deleted_thread_author_gets_no_new_notification(self) -> None:
        self.soft_delete(self.guest_id)
        actor = UserInfo(email=ADMIN_EMAIL, name="Admin", roles=[])
        with self.fks_off():
            thread_service._notify_new_thread_comment(
                self.db,
                thread_author_email=GUEST_EMAIL,
                actor=actor,
                component_id=1,
                thread_id=1,
                comment_id=1,
                content="hi",
                already_notified=set(),
            )
        self.assertEqual(self.fresh().query(NotificationV2).count(), 0)

    def test_the_hr_profile_lookup_hides_a_deleted_user(self) -> None:
        self.soft_delete(self.guest_id)
        res = self.client.get(f"/users/by-email/{GUEST_EMAIL}", headers=self.admin)
        self.assertEqual(res.status_code, 404, res.text)

    def test_role_and_scope_deletes_stay_blocked_and_say_so(self) -> None:
        self.assign(self.guest_id, self.member, self.scope_a)
        self.db.commit()
        self.soft_delete(self.guest_id)
        with self.assertRaises(RbacGraphError) as error:
            rbac_service.delete_scope(self.db, self.scope_a)
        self.assertEqual(error.exception.code, "scope_in_use")
        self.assertEqual(error.exception.details["deleted_user_assignment_count"], 1)
        self.assertIn("1 of these belongs to a deleted user", error.exception.message)
        with self.assertRaises(RbacGraphError) as error:
            rbac_service.delete_role(self.db, self.member)
        self.assertEqual(error.exception.details["deleted_user_assignment_count"], 1)


class TestEmailsStayReserved(AdminDeleteUserFixture):
    def test_add_user_with_a_deleted_email_is_409_hub_user_deleted(self) -> None:
        self.assertEqual(self.delete(self.guest_id).status_code, 204)
        for email in (GUEST_EMAIL, GUEST_ALIAS.upper()):
            res = self.client.post("/v2/rbac/admin/hub-users", json={"email": email, "name": "Again"}, headers=self.admin)
            self.assertEqual(res.status_code, 409, res.text)
            detail = res.json()["detail"]
            self.assertEqual(detail["code"], "hub_user_deleted")
            self.assertEqual(detail["user_id"], self.guest_id)
            self.assertEqual(detail["deleted_by"], ADMIN_EMAIL)
            self.assertIsNotNone(detail["deleted_at"])
        self.assertEqual(self.rows_for(GUEST_EMAIL), 1)

    def test_add_user_with_a_live_email_is_still_hub_user_exists(self) -> None:
        res = self.client.post("/v2/rbac/admin/hub-users", json={"email": GUEST_EMAIL, "name": "Again"}, headers=self.admin)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "hub_user_exists")

    def test_changing_a_work_email_to_a_deleted_address_is_409(self) -> None:
        staff_id = self.add_user(STAFF_EMAIL, "Staff")
        self.db.commit()
        self.soft_delete(self.guest_id)
        with self.assertRaises(HTTPException) as error:
            user_db_service.update_user_by_work_email(self.db, STAFF_EMAIL, UserUpdate(work_email=GUEST_EMAIL))
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("deleted user", error.exception.detail)
        self.assertEqual(self.row(staff_id).work_email, STAFF_EMAIL)


if __name__ == "__main__":
    unittest.main()
