"""GET /auth/me — is_hub_admin reconciliation.

findings_dev_login_live_testing_2026-09-12.md #1 fixed the *first* login of
a session (`/auth/callback` and `/auth/dev-login` now both surface the real
`is_hub_admin(db, current)` closure on their redirect). But
`AuthContext.tsx` caches that flag in `localStorage` for the rest of the
browser session and never calls `/auth/me` again while a cached `user`
exists — so an admin grant or revoke that happens *after* someone's last
login stays invisible to them until they happen to log out and back in.

This covers `/auth/me` itself picking up the same real `is_hub_admin`
answer (not just whether "Hub Admin" is in the JWT `roles` claim), which is
what lets the frontend's background reconciliation in `AuthContext.tsx`
repair a stale cached flag on a plain page reload instead of requiring a
fresh login. See `AI Docs` for the full write-up; no handoff doc for this
specific fix exists yet.

Fixture mirrors tests/test_dev_login.py's and tests/test_rbac_api.py's: a
real FastAPI TestClient over an in-memory SQLite stand-in for the v2
schema, `get_db_v2` overridden the "mirrors the real generator, including
`finally: db.close()`" way tests/test_rbac_api.py's own comment explains is
required for correctness on a StaticPool. `AirtableService.get_user_scoped_
roles` is patched everywhere here — this file is not testing the legacy
Airtable-scoped-roles surface, and must never hit real Airtable.
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-auth-me-tests-only")
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
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.dependencies import HUB_ADMIN_ROLE_KEY  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.airtable_service import AirtableService  # noqa: E402

HUB_ADMIN = "Hub Admin"


class AuthMeFixture(unittest.TestCase):
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

        # /auth/me no longer reads Airtable (plan_login_airtable_removal_
        # 2026-10-31.md): fail loudly if the scoped-roles call comes back.
        self._scoped_roles_patcher = patch.object(
            AirtableService,
            "get_user_scoped_roles",
            new_callable=AsyncMock,
            side_effect=AssertionError("/auth/me called AirtableService.get_user_scoped_roles"),
            create=True,
        )
        self.mock_scoped_roles = self._scoped_roles_patcher.start()

    def tearDown(self) -> None:
        self._scoped_roles_patcher.stop()
        self.db.close()
        self.engine.dispose()

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

    def _me(self, email: str, roles: list[str] | None = None):
        return self.client.get("/auth/me", headers=self._auth(email, roles))


class TestAuthMeIsHubAdmin(AuthMeFixture):
    def test_no_admin_signal_comes_back_false(self):
        resp = self._me("nobody@renphil.org")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["is_hub_admin"], False)

    def test_scoped_roles_is_gone_and_airtable_is_not_called(self):
        resp = self._me("nobody@renphil.org")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("scoped_roles", resp.json())
        self.mock_scoped_roles.assert_not_called()

    def test_jwt_hub_admin_role_comes_back_true_with_no_assignment_row(self):
        """Branch 1 alone (the ORIGINAL admin path, still the only one most
        people have today) — no role_assignments row involved at all."""
        resp = self._me("jwt-admin@renphil.org", [HUB_ADMIN])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["is_hub_admin"], True)

    def test_role_assignments_only_admin_comes_back_true_with_empty_roles(self):
        """Branch 2 alone — the exact scenario a dev-login persona or a
        newly-granted real admin without a fresh login exercises."""
        email = "assignment-admin@renphil.org"

        # First call provisions the hub_users row (the same lazy upsert
        # every authenticated endpoint relies on).
        first = self._me(email)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["is_hub_admin"], False)

        hub_user = self.db.query(UserV2).filter(UserV2.email == email).first()
        self.assertIsNotNone(hub_user)

        now = datetime.now(timezone.utc)
        role = RoleV2(key=HUB_ADMIN_ROLE_KEY, name="Hub Admin", created_at=now)
        scope = ScopeV2(key="all_scopes", name="All Scopes", is_universal=True, created_at=now)
        self.db.add_all([role, scope])
        self.db.commit()
        self.db.add(
            RoleAssignmentV2(
                user_id=hub_user.id, role_id=role.id, scope_id=scope.id, created_at=now
            )
        )
        self.db.commit()

        # Same JWT (roles: []) as the first call — only the DB changed.
        resp = self._me(email)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["is_hub_admin"], True)
        self.assertEqual(resp.json()["roles"], [])

    def test_reconciles_after_a_mid_session_revocation(self):
        """The exact bug this endpoint exists to let the frontend repair:
        an admin who was granted, then revoked, without logging out in
        between — /me must reflect the revocation on the very next call,
        since it re-derives is_hub_admin live rather than trusting the
        original login's answer."""
        email = "revoked-admin@renphil.org"
        self._me(email)
        hub_user = self.db.query(UserV2).filter(UserV2.email == email).first()

        now = datetime.now(timezone.utc)
        role = RoleV2(key=HUB_ADMIN_ROLE_KEY, name="Hub Admin", created_at=now)
        scope = ScopeV2(key="all_scopes", name="All Scopes", is_universal=True, created_at=now)
        self.db.add_all([role, scope])
        self.db.commit()
        assignment = RoleAssignmentV2(
            user_id=hub_user.id, role_id=role.id, scope_id=scope.id, created_at=now
        )
        self.db.add(assignment)
        self.db.commit()

        self.assertEqual(self._me(email).json()["is_hub_admin"], True)

        self.db.delete(
            self.db.query(RoleAssignmentV2)
            .filter(RoleAssignmentV2.user_id == hub_user.id)
            .first()
        )
        self.db.commit()

        self.assertEqual(self._me(email).json()["is_hub_admin"], False)


if __name__ == "__main__":
    unittest.main()
