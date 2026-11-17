"""Revoke-triggered lock release — edit_lock_service.release_locks_now_
unauthorized, wired into the three REVOKE/NARROW doors: DELETE /v2/rbac/
assignments/{id}, PATCH /v2/rbac/admin/assignments/{id}, and DELETE
/v2/rbac/grants/{id}.

Found investigating a live report: a user's access revoked mid-edit left
their lock fully live (not stale) for the rest of the TTL, with no
automatic path back — see edit_lock_service.py's own module-level comment
above the function for the full story, including why the SAME investigation
also turned up a real, separate bug (an ancestor's independently-held lock
defeating a descendant's own "Force take over", by design, per `acquire`'s
own docstring) that this fix does not attempt to change.

THE ASYMMETRIC FAIL-SAFE IS THE MOST IMPORTANT THING THIS FILE PINS. A
DB-only admin check would silently force-release a genuine Hub Admin's lock
the moment anyone revokes anything else, anywhere, while that admin is
mid-edit — `role_assignments` holds far fewer rows than there are real
(Airtable-sourced) Hub Admins today. `TestAsymmetricFailSafe` below is the
one class here that must never be weakened to make a future change easier.

Fixture mirrors tests/test_tab_locking.py's `TabLockingFixture`/
`TestLockAuthorization` (same in-memory-SQLite router fixture, same
`_grant` helper shape) — grants/assignments are written directly through
`self.db`, matching that file's convention that only the door actually
under test (here: the three revoke endpoints, and the real `/lock` door to
establish the starting state) goes through the real HTTP door.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-lock-release-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.dependencies import HUB_ADMIN_ROLE_KEY  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.airtable_service import AirtableService  # noqa: E402
from app.services.edit_lock_service import acquire  # noqa: E402
from app.services.gridstack_service import create_tab_v2, get_gridstack_by_document_id  # noqa: E402
from tests._real_admin import seed_real_admin  # noqa: E402

ALL_SCOPES_KEY = "all_scopes"


class LockReleaseFixture(unittest.TestCase):
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

        app = create_app()

        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

        # Deterministic by default: nobody is an Airtable-only admin unless
        # a specific test overrides this. Never hits real Airtable.
        # `create=True` (2026-11-07): `AirtableService.is_hub_admin` was
        # removed with phase 3's Airtable fallback, so patching it without
        # `create` raised AttributeError in EVERY test's setUp. The mock now
        # stands in for a method nothing calls: `assert_not_called()` below
        # still holds, and the two Airtable-scenario tests in
        # `TestAsymmetricFailSafe` fail on their own assertions (behaviour
        # removed on purpose; owner decision, see the 2026-11-07 handoff).
        self._airtable_patcher = patch.object(
            AirtableService, "is_hub_admin", new_callable=AsyncMock, create=True
        )
        self.mock_airtable_is_hub_admin = self._airtable_patcher.start()
        self.mock_airtable_is_hub_admin.return_value = False

        now = datetime.now(timezone.utc)
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(document_id="nav-1", slug="nav-1", title="Nav 1", protected=False)
        self.db.add(self.nav1)
        self.db.flush()

        root = create_tab_v2(self.db, title="Root 1", nav_tab_id=self.nav1.id)
        self.root_document_id = root["documentId"]
        self.root_gridstack = get_gridstack_by_document_id(self.db, self.root_document_id)
        self.root_tab_id = self.root_gridstack.parent_tab_id

        other_root = create_tab_v2(self.db, title="Root 2", nav_tab_id=self.nav1.id)
        self.other_root_document_id = other_root["documentId"]
        self.other_root_tab_id = get_gridstack_by_document_id(
            self.db, self.other_root_document_id
        ).parent_tab_id

        self.editor = UserV2(email="editor@renphil.org", has_signed_in=True, created_at=now)
        self.other_editor = UserV2(email="other-editor@renphil.org", has_signed_in=True, created_at=now)
        self.db.add_all([self.editor, self.other_editor])
        self.db.flush()
        self.db.commit()

    def tearDown(self) -> None:
        self._airtable_patcher.stop()
        self.db.close()
        self.engine.dispose()

    # -- auth ---------------------------------------------------------------

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": roles or [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    def _admin_auth(self) -> dict:
        """A real Hub Admin (phase 3: a `(hub_admin, universal)` assignment,
        seeded lazily on first use; the JWT claim alone no longer counts).
        Seeded AFTER any `_role_and_scope(HUB_ADMIN_ROLE_KEY)` a test made,
        so it reuses that role rather than clashing with it."""
        seed_real_admin(self._session_factory, "admin@renphil.org")
        return self._auth("admin@renphil.org", roles=["Hub Admin"])

    # -- builders -------------------------------------------------------------

    def _grant(self, *, node: tuple[str, int], level: str, user: UserV2 | None = None,
               role: RoleV2 | None = None, scope: ScopeV2 | None = None) -> ResourceGrantV2:
        node_kind, node_id = node
        kwargs = {NODE_COLUMNS[node_kind]: node_id}
        if user is not None:
            kwargs["user_id"] = user.id
        else:
            kwargs["role_id"] = role.id
            kwargs["scope_id"] = scope.id
        row = ResourceGrantV2(**kwargs, level=level, created_at=datetime.now(timezone.utc))
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _role_and_scope(self, role_key: str) -> tuple[RoleV2, ScopeV2]:
        now = datetime.now(timezone.utc)
        scope = (
            self.db.query(ScopeV2).filter(ScopeV2.key == ALL_SCOPES_KEY).first()
        )
        if scope is None:
            scope = ScopeV2(key=ALL_SCOPES_KEY, name="All Scopes", is_universal=True, created_at=now)
            self.db.add(scope)
        role = RoleV2(key=role_key, name=role_key, created_at=now)
        self.db.add(role)
        self.db.commit()
        self.db.refresh(role)
        self.db.refresh(scope)
        return role, scope

    def _assign(self, user: UserV2, role: RoleV2, scope: ScopeV2) -> RoleAssignmentV2:
        row = RoleAssignmentV2(
            user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=datetime.now(timezone.utc)
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _lock(self, document_id: str, email: str) -> None:
        resp = self.client.put(f"/v2/tabs/{document_id}/lock", json={}, headers=self._auth(email))
        self.assertEqual(resp.status_code, 200, resp.text)

    def _is_locked(self, document_id: str) -> tuple[bool, str]:
        resp = self.client.get(f"/v2/tabs/{document_id}/workspace", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()["data"]
        return bool(data["locked"]), data.get("locked_by") or ""


class TestGrantRevokeReleasesTheHoldersLock(LockReleaseFixture):
    def test_direct_user_grant_revoke_releases_the_lock(self) -> None:
        grant = self._grant(node=("tab", self.root_tab_id), level="edit", user=self.editor)
        self._lock(self.root_document_id, self.editor.email)
        locked, locked_by = self._is_locked(self.root_document_id)
        self.assertTrue(locked)
        self.assertEqual(locked_by, self.editor.email)

        resp = self.client.delete(f"/v2/rbac/grants/{grant.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        locked, locked_by = self._is_locked(self.root_document_id)
        self.assertFalse(locked)
        # `_force_break_lock`'s own contract: `locked_by` survives the
        # release so the ousted holder's next write reads
        # EDIT_SESSION_TAKEN_OVER, not EDIT_SESSION_MISSING.
        self.assertEqual(locked_by, self.editor.email)

    def test_revoking_an_unrelated_grant_leaves_a_still_valid_lock_alone(self) -> None:
        """The sweep is whole-system, not scoped to the write that
        triggered it — this pins that it is not OVERLY broad: a
        still-legitimate holder on a DIFFERENT node must survive."""
        editor_grant = self._grant(node=("tab", self.root_tab_id), level="edit", user=self.editor)
        self._grant(node=("tab", self.other_root_tab_id), level="edit", user=self.other_editor)
        self._lock(self.root_document_id, self.editor.email)
        self._lock(self.other_root_document_id, self.other_editor.email)

        resp = self.client.delete(f"/v2/rbac/grants/{editor_grant.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertEqual(self._is_locked(self.root_document_id), (False, self.editor.email))
        locked, locked_by = self._is_locked(self.other_root_document_id)
        self.assertTrue(locked)
        self.assertEqual(locked_by, self.other_editor.email)


class TestAssignmentRevokeReleasesTheHoldersLock(LockReleaseFixture):
    def test_deleting_the_assignment_releases_the_lock(self) -> None:
        role, scope = self._role_and_scope("Contributor")
        self._grant(node=("tab", self.root_tab_id), level="edit", role=role, scope=scope)
        assignment = self._assign(self.editor, role, scope)
        self._lock(self.root_document_id, self.editor.email)
        self.assertEqual(self._is_locked(self.root_document_id), (True, self.editor.email))

        resp = self.client.delete(
            f"/v2/rbac/assignments/{assignment.id}", headers=self._admin_auth()
        )
        # The route declares `status_code=204` (rbac_assignments.py); this
        # test said 200 but never got past setUp until 2026-11-07.
        self.assertEqual(resp.status_code, 204, resp.text)

        self.assertEqual(self._is_locked(self.root_document_id), (False, self.editor.email))

    def test_narrowing_the_assignment_in_place_releases_the_lock(self) -> None:
        """PATCH /v2/rbac/admin/assignments/{id} — narrowing a live
        assignment is just as much a revoke of the OLD pair as deleting it
        outright."""
        contributor, scope = self._role_and_scope("Contributor")
        powerless, _ = self._role_and_scope("Powerless")
        self._grant(node=("tab", self.root_tab_id), level="edit", role=contributor, scope=scope)
        # `Powerless` deliberately has no grant anywhere.
        assignment = self._assign(self.editor, contributor, scope)
        self._lock(self.root_document_id, self.editor.email)
        self.assertEqual(self._is_locked(self.root_document_id), (True, self.editor.email))

        resp = self.client.patch(
            f"/v2/rbac/admin/assignments/{assignment.id}",
            json={"role_id": powerless.id},
            headers=self._admin_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertEqual(self._is_locked(self.root_document_id), (False, self.editor.email))


class TestComponentLocksAreSweptToo(LockReleaseFixture):
    """plan_component_locking_and_sbn_2026-09-17.md §4: `release_locks_now_
    unauthorized` gains a fourth row source — `ComponentV2 WHERE locked AND
    current_grid_id IS NULL` — so a revoked narrow editor's WIDGET lock is
    released the same way a tab lock is. Service-level acquire (no component
    lock route exists until phase B), real HTTP revoke door."""

    def setUp(self) -> None:
        super().setUp()
        self.widget = ComponentV2(
            link="widget-1", title="Widget", type="text", gridstack_id=self.root_gridstack.id
        )
        self.db.add(self.widget)
        self.db.commit()
        self.WIDGET = ("component", self.widget.id)

    def _widget_lock(self) -> tuple[bool, str]:
        self.db.expire_all()
        row = self.db.query(ComponentV2).filter(ComponentV2.id == self.widget.id).first()
        return bool(row.locked), row.locked_by or ""

    def test_revoking_the_holders_only_grant_releases_their_widget_lock(self) -> None:
        grant = self._grant(node=self.WIDGET, level="edit", user=self.editor)
        acquire(self.db, self.WIDGET, self.editor.email)
        self.assertEqual(self._widget_lock(), (True, self.editor.email))

        resp = self.client.delete(f"/v2/rbac/grants/{grant.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        # `_force_break_lock`'s contract, same as for a tab: `locked_by`
        # survives so the next write reads TAKEN_OVER, not MISSING.
        self.assertEqual(self._widget_lock(), (False, self.editor.email))

    def test_revoking_an_unrelated_grant_leaves_a_still_valid_widget_lock_alone(self) -> None:
        self._grant(node=self.WIDGET, level="edit", user=self.editor)
        other = self._grant(node=("tab", self.other_root_tab_id), level="edit", user=self.other_editor)
        acquire(self.db, self.WIDGET, self.editor.email)

        resp = self.client.delete(f"/v2/rbac/grants/{other.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertEqual(self._widget_lock(), (True, self.editor.email))

    def test_a_hub_admins_widget_lock_is_never_swept(self) -> None:
        """The asymmetric fail-safe, unchanged for the fourth row source:
        a `role_assignments` admin holding a WIDGET lock survives every
        unrelated revoke, with no Airtable call needed."""
        admin_role, admin_scope = self._role_and_scope(HUB_ADMIN_ROLE_KEY)
        self._assign(self.editor, admin_role, admin_scope)
        acquire(self.db, self.WIDGET, self.editor.email)

        unrelated = self._grant(node=("tab", self.other_root_tab_id), level="edit", user=self.other_editor)
        resp = self.client.delete(f"/v2/rbac/grants/{unrelated.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertEqual(self._widget_lock(), (True, self.editor.email))
        self.mock_airtable_is_hub_admin.assert_not_called()


class TestAsymmetricFailSafe(LockReleaseFixture):
    """The most important class in this file — see its own module
    docstring above. Every test here must end with the lock STILL held."""

    def test_a_role_assignments_hub_admin_is_never_released(self) -> None:
        """Branch 2 (DB-only) admin — the fast path, no Airtable call
        needed at all: `hub_admin`/`all_scopes` reaches every node
        unconditionally via `is_hub_admin`'s own closure check, so this
        holder has no `resource_grants` row on `root_tab_id` whatsoever,
        the same shape a real admin's lock has today."""
        admin_role, admin_scope = self._role_and_scope(HUB_ADMIN_ROLE_KEY)
        self._assign(self.editor, admin_role, admin_scope)
        self._lock(self.root_document_id, self.editor.email)

        unrelated_grant = self._grant(
            node=("tab", self.other_root_tab_id), level="edit", user=self.other_editor
        )
        resp = self.client.delete(f"/v2/rbac/grants/{unrelated_grant.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        locked, locked_by = self._is_locked(self.root_document_id)
        self.assertTrue(locked, "a genuine Hub Admin's lock must never be swept away")
        self.assertEqual(locked_by, self.editor.email)
        self.mock_airtable_is_hub_admin.assert_not_called()

    def test_an_airtable_only_admin_with_zero_db_grants_is_never_released(self) -> None:
        """THE scenario the whole live-Airtable check exists for: zero
        `role_assignments`, zero `resource_grants` — exactly what a real
        Hub Admin whose only signal is their Airtable-sourced JWT role
        looks like from the database alone (22 such admins to 1
        role_assignments row, per the last parity survey). Without the
        live check, this test fails."""
        self.mock_airtable_is_hub_admin.return_value = True
        # No grant, no assignment for `editor` anywhere — locking would
        # ordinarily 403, so this can only be reached as the Hub Admin
        # bypass in the real lock door too.
        resp = self.client.put(
            f"/v2/tabs/{self.root_document_id}/lock",
            json={},
            headers=self._auth(self.editor.email, roles=["Hub Admin"]),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        unrelated_grant = self._grant(
            node=("tab", self.other_root_tab_id), level="edit", user=self.other_editor
        )
        resp = self.client.delete(f"/v2/rbac/grants/{unrelated_grant.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        locked, locked_by = self._is_locked(self.root_document_id)
        self.assertTrue(locked, "an Airtable-only admin's lock must never be swept away")
        self.assertEqual(locked_by, self.editor.email)
        self.mock_airtable_is_hub_admin.assert_called_with(self.editor.email)

    def test_an_airtable_call_failure_leaves_the_lock_alone_not_releases_it(self) -> None:
        """The fail-safe direction, pinned directly: an ordinary editor
        (no admin signal at all) whose ONLY grant is revoked would
        ordinarily lose their lock (TestGrantRevokeReleasesTheHoldersLock)
        — but if the live Airtable check itself blows up, uncertainty must
        resolve to "leave them alone", never to "release"."""
        self.mock_airtable_is_hub_admin.side_effect = RuntimeError("Airtable is down")
        grant = self._grant(node=("tab", self.root_tab_id), level="edit", user=self.editor)
        self._lock(self.root_document_id, self.editor.email)

        resp = self.client.delete(f"/v2/rbac/grants/{grant.id}", headers=self._admin_auth())
        self.assertEqual(resp.status_code, 200, resp.text)

        locked, locked_by = self._is_locked(self.root_document_id)
        self.assertTrue(locked, "an Airtable-check failure must never be treated as 'not admin'")
        self.assertEqual(locked_by, self.editor.email)


if __name__ == "__main__":
    unittest.main()
