"""GET /auth/dev-login — session_handoff_2026-09-10-dev-login-tool.md.

Standalone, additive dev-only tool ("mint a session for any email, no
Google") — NOT part of the AC enforcement closeout arc's own numbered
steps. Covers the four properties the task's own §3 names at minimum:

  1. The route 404s (not 403, not a different error shape — literally the
     same body as an unknown path) when Settings.DEBUG is False.
  2. With DEBUG True, the redirect's access_token decodes (via the REAL
     AuthService.decode_access_token, not a hand re-decode) to
     sub == <email> and roles == [].
  3. The redirect's query-param shape matches what /auth/callback already
     produces (same param names — no frontend branching needed).
  4. No AirtableService method is ever called on this path — the concrete
     regression this route must never reintroduce is writing a test email
     into the real Airtable Access Control table.

Plus two properties from the handoff's own design that are just as easy to
regress silently: `hub_users` is provisioned SYNCHRONOUSLY (item 6 — the
whole reason this route calls `_ensure_hub_user` itself rather than
leaning on `get_current_hub_user`'s lazy upsert), and the bare-URL form
(no `?email=`) is gated exactly the same as the real thing.

Fixture mirrors tests/test_rbac_api.py's: a real FastAPI TestClient over an
in-memory SQLite stand-in for the v2 schema, `get_db_v2` overridden the
"mirrors the real generator, including `finally: db.close()`" way that
file's own comment explains is required for correctness on a StaticPool.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-dev-login-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.config import get_settings  # noqa: E402
from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role import RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeV2  # noqa: E402
from app.dependencies import HUB_ADMIN_ROLE_KEY  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.airtable_service import AirtableService  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402

# The full set of query params /auth/callback's own `urlencode(..., doseq=
# True)` call can ever produce (picture only when truthy, roles only when
# non-empty) — the redirect shape test asserts dev-login's own params are a
# subset of this AND cover the four that are always present.
# `is_hub_admin` added by findings_dev_login_live_testing_2026-09-12.md #1:
# AuthCallbackPage.tsx never calls GET /me, so the real `is_hub_admin`
# closure has to ride the redirect like `roles` does — unconditionally on
# both /auth/callback and /auth/dev-login, unlike `roles`/`picture` which
# are omitted when falsy.
_CALLBACK_PARAM_NAMES = {"access_token", "email", "name", "roles", "picture", "is_hub_admin"}
_CALLBACK_ALWAYS_PRESENT = {"access_token", "email", "name", "is_hub_admin"}


class DevLoginFixture(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        os.environ.pop("DEBUG", None)

    def _get(self, **params):
        return self.client.get("/auth/dev-login", params=params, follow_redirects=False)


class TestDevLoginGate(DevLoginFixture):
    """§3 item 1: 404, not 403, not a different shape, when DEBUG is False."""

    def test_404s_when_debug_is_false(self):
        os.environ["DEBUG"] = "False"
        resp = self._get(email="persona@example.com")
        self.assertEqual(resp.status_code, 404)

    def test_bare_url_form_also_404s_when_debug_is_false(self):
        """The `?email=` omitted convenience form must not leak the route's
        existence any more than the emailed form does."""
        os.environ["DEBUG"] = "False"
        resp = self._get()
        self.assertEqual(resp.status_code, 404)

    def test_404_body_is_indistinguishable_from_an_unknown_path(self):
        """The route's existence must not be observable at all — not just
        the status code, the exact response body too — when DEBUG is off."""
        os.environ["DEBUG"] = "False"
        known_route = self._get(email="persona@example.com")
        unknown_path = self.client.get("/auth/this-route-does-not-exist")
        self.assertEqual(known_route.status_code, unknown_path.status_code)
        self.assertEqual(known_route.json(), unknown_path.json())

    def test_reachable_at_all_once_debug_is_true(self):
        """Sanity check that the 404s above are really the DEBUG gate and
        not some unrelated wiring mistake making the route always 404."""
        os.environ["DEBUG"] = "True"
        resp = self._get(email="persona@example.com")
        self.assertNotEqual(resp.status_code, 404)


class TestDevLoginMintsToken(DevLoginFixture):
    """§3 item 2: a real, decodable JWT with roles always []."""

    def test_redirect_token_decodes_to_the_requested_email_with_no_roles(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(email="Persona1@Example.com")
        self.assertEqual(resp.status_code, 307)

        location = resp.headers["location"]
        token = parse_qs(urlparse(location).query)["access_token"][0]

        decoded = AuthService(get_settings()).decode_access_token(token)
        self.assertEqual(decoded.email, "persona1@example.com")
        self.assertEqual(decoded.roles, [])

    def test_a_different_persona_gets_their_own_token(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(email="second-persona@example.com")
        location = resp.headers["location"]
        token = parse_qs(urlparse(location).query)["access_token"][0]
        decoded = AuthService(get_settings()).decode_access_token(token)
        self.assertEqual(decoded.email, "second-persona@example.com")
        self.assertEqual(decoded.roles, [])

    def test_bare_url_form_is_rendered_when_debug_true_and_email_omitted(self):
        os.environ["DEBUG"] = "True"
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("email", resp.text.lower())
        self.assertNotIn("access_token", resp.text)

    def test_malformed_email_is_a_400_not_a_500(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(email="not-an-email-at-all")
        self.assertEqual(resp.status_code, 400)


class TestDevLoginRedirectShape(DevLoginFixture):
    """§3 item 3: same query-param names /auth/callback already produces —
    the property that lets AuthCallbackPage.tsx need zero changes."""

    def test_redirect_params_are_a_subset_of_callbacks_own_shape(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(email="persona@example.com")
        location = resp.headers["location"]
        parsed = urlparse(location)
        params = parse_qs(parsed.query)

        self.assertTrue(set(params.keys()).issubset(_CALLBACK_PARAM_NAMES))
        self.assertTrue(_CALLBACK_ALWAYS_PRESENT.issubset(params.keys()))
        # roles=[] must produce ZERO `roles` params (matching
        # URLSearchParams.getAll('roles') === [] on the frontend), not a
        # stray `roles=` or `roles=[]`.
        self.assertNotIn("roles", params)
        # dev-login never has a picture.
        self.assertNotIn("picture", params)

    def test_redirect_target_defaults_to_the_frontend_dev_server(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(email="persona@example.com")
        parsed = urlparse(resp.headers["location"])
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                          "http://localhost:5000/auth/callback")

    def test_redirect_target_honors_an_explicit_redirect_uri(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(
            email="persona@example.com",
            redirect_uri="http://localhost:5173/auth/callback",
        )
        parsed = urlparse(resp.headers["location"])
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                          "http://localhost:5173/auth/callback")


class TestDevLoginProvisionsHubUserSynchronously(DevLoginFixture):
    """Item 6: the whole point of extracting `_ensure_hub_user` — the row
    must exist immediately after this one request, not after a second,
    later authenticated one."""

    def test_hub_user_row_exists_immediately_after_dev_login(self):
        os.environ["DEBUG"] = "True"
        self.assertIsNone(
            self.db.query(UserV2).filter(UserV2.email == "persona@example.com").first()
        )
        resp = self._get(email="persona@example.com")
        self.assertEqual(resp.status_code, 307)

        hub_user = self.db.query(UserV2).filter(UserV2.email == "persona@example.com").first()
        self.assertIsNotNone(hub_user)
        self.assertTrue(hub_user.has_signed_in)

    def test_a_second_dev_login_for_the_same_email_does_not_duplicate_the_row(self):
        os.environ["DEBUG"] = "True"
        self._get(email="persona@example.com")
        self._get(email="persona@example.com")
        rows = self.db.query(UserV2).filter(UserV2.email == "persona@example.com").all()
        self.assertEqual(len(rows), 1)


class TestDevLoginNeverTouchesAirtable(DevLoginFixture):
    """§3 item 4 — the concrete regression: this path must never write a
    test email into the real Airtable Access Control table."""

    def test_no_airtable_service_method_is_called(self):
        os.environ["DEBUG"] = "True"
        with patch.object(
            AirtableService, "ensure_access_control_member", create=True
        ) as ensure_mock, patch.object(
            AirtableService, "access_control_email_exists", create=True
        ) as exists_mock, patch.object(
            AirtableService, "get_user_roles", create=True
        ) as roles_mock:
            resp = self._get(email="persona@example.com")

        self.assertEqual(resp.status_code, 307)
        ensure_mock.assert_not_called()
        exists_mock.assert_not_called()
        roles_mock.assert_not_called()


class TestDevLoginIsHubAdmin(DevLoginFixture):
    """findings_dev_login_live_testing_2026-09-12.md #1: the exact live
    scenario that finding confirmed — a persona with `roles: []` (forced,
    per this route's own design) but a real `role_assignments` row must
    still come back `is_hub_admin=true` on the redirect, because that flag
    now rides the redirect params instead of only being computed
    server-side and dropped on the floor.
    """

    def test_no_assignment_comes_back_false(self):
        os.environ["DEBUG"] = "True"
        resp = self._get(email="persona@example.com")
        params = parse_qs(urlparse(resp.headers["location"]).query)
        self.assertEqual(params["is_hub_admin"], ["false"])

    def test_a_live_role_assignments_row_comes_back_true(self):
        os.environ["DEBUG"] = "True"

        # First login provisions the hub_users row synchronously (item 6,
        # covered above) — reuse that instead of inserting one by hand.
        self._get(email="persona@example.com")
        hub_user = (
            self.db.query(UserV2)
            .filter(UserV2.email == "persona@example.com")
            .first()
        )
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

        resp = self._get(email="persona@example.com")
        params = parse_qs(urlparse(resp.headers["location"]).query)
        self.assertEqual(params["is_hub_admin"], ["true"])
        # roles is still forced to [] regardless — branch 2 alone, per the
        # route's own docstring, is what's being exercised here.
        self.assertNotIn("roles", params)


if __name__ == "__main__":
    unittest.main()
