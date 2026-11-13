"""Login allow-list: org domain, or a `users` row (owner decision, 2026-10-04;
`hub_users` was folded into `users`, 2026-10).

`AuthService._enforce_login_allowed` admits, in order: an org-domain email,
or an email a `users` row signs in as (`work_email`, else `alias_email` — an
HR row or someone an admin added from the Users tab). The Airtable Access
Control table is no longer consulted or written by sign-in
(plan_login_airtable_removal_2026-10-31.md): an external email known only
there is refused, and the minted token's `roles` is always `[]`.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-login-gate-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

import re  # noqa: E402
from pathlib import Path  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.config import get_settings  # noqa: E402
from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.helpers.exceptions import UserNotAuthorizedError  # noqa: E402
from app.services import auth_service as auth_module  # noqa: E402
from app.services.airtable_service import AirtableService  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402

EXTERNAL = "guest@partner.example"

# Every AirtableService method sign-in used to call. Deleted in Step 5 of
# plan_login_airtable_removal_2026-10-31.md; still patched (create=True) to
# fail loudly, so a reintroduced method + call breaks these tests.
_FORMER_LOGIN_AIRTABLE_CALLS = (
    "access_control_email_exists",
    "ensure_access_control_member",
    "get_user_roles",
)


def _run(coro):
    return asyncio.run(coro)


class LoginGateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        BaseV2.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.service = AuthService(get_settings())
        self.airtable_mocks: dict[str, AsyncMock] = {}
        for name in _FORMER_LOGIN_AIRTABLE_CALLS:
            mock = AsyncMock(side_effect=AssertionError(f"sign-in called AirtableService.{name}"))
            patcher = patch.object(AirtableService, name, mock, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            self.airtable_mocks[name] = mock

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def assert_no_airtable_calls(self) -> None:
        for name, mock in self.airtable_mocks.items():
            mock.assert_not_called()

    def add_hub_user(self, email: str, *, active: bool = True) -> None:
        self.db.add(
            UserV2(email=email, name="Guest", has_signed_in=active, created_at=datetime.now(timezone.utc))
        )
        self.db.commit()

    def gate(self, email: str):
        return self.service._enforce_login_allowed(email, self.db)


class TestEnforceLoginAllowed(LoginGateFixture):
    def test_org_domain_is_admitted_without_any_lookup(self) -> None:
        domain = get_settings().allowed_email_domains[0]
        self.assertEqual(self.gate(f"someone@{domain}"), "domain")
        self.assert_no_airtable_calls()

    def test_unknown_external_is_refused(self) -> None:
        with self.assertRaises(UserNotAuthorizedError):
            self.gate(EXTERNAL)
        self.assert_no_airtable_calls()

    def test_external_with_hub_users_row_is_admitted(self) -> None:
        self.add_hub_user(EXTERNAL)
        self.assertEqual(self.gate(EXTERNAL), "users")
        self.assert_no_airtable_calls()

    def test_hub_users_match_is_case_and_whitespace_insensitive(self) -> None:
        self.add_hub_user(EXTERNAL)
        self.assertEqual(self.gate("  Guest@Partner.EXAMPLE "), "users")

    def test_inactive_hub_users_row_still_admits(self) -> None:
        """Owner decision: `has_signed_in` is ignored by the gate."""
        self.add_hub_user(EXTERNAL, active=False)
        self.assertEqual(self.gate(EXTERNAL), "users")

    def test_alias_email_admits_like_a_work_email(self) -> None:
        """Merge (2026-10): `alias_email` is the login fallback."""
        self.db.add(UserV2(name="Aliased", work_email="aliased@renphil.org", alias_email=EXTERNAL))
        self.db.commit()
        self.assertEqual(self.gate(EXTERNAL), "users")

    def test_hr_row_with_external_work_email_admits(self) -> None:
        """Merge (2026-10): an HR row nobody has signed in as still admits."""
        self.db.add(UserV2(name="Staff", work_email=EXTERNAL, status="Onboarding"))
        self.db.commit()
        self.assertEqual(self.gate(EXTERNAL), "users")


class TestHandleCallback(LoginGateFixture):
    """Drives the real `handle_callback` with Google's token exchange and
    id_token verification stubbed out."""

    def _callback_as(self, email: str):
        auth_module._pkce_store["state-x"] = ("verifier", "http://localhost/cb")
        flow = MagicMock()
        flow.credentials = SimpleNamespace(id_token="id-token")
        with patch.object(auth_module, "build_oauth_flow", return_value=flow), patch.object(
            auth_module.google_id_token,
            "verify_oauth2_token",
            return_value={"email": email, "name": "Guest"},
        ):
            return _run(self.service.handle_callback("code", "state-x", self.db))

    def test_hub_users_admission_mints_token_without_airtable(self) -> None:
        self.add_hub_user(EXTERNAL)
        token, redirect = self._callback_as(EXTERNAL)
        self.assertEqual(token.email, EXTERNAL)
        self.assertEqual(token.roles, [])
        self.assertEqual(redirect, "http://localhost/cb")
        self.assert_no_airtable_calls()

    def test_org_domain_admission_mints_token_without_airtable(self) -> None:
        email = f"someone@{get_settings().allowed_email_domains[0]}"
        token, _ = self._callback_as(email)
        self.assertEqual(token.roles, [])
        self.assertEqual(self.service.decode_access_token(token.access_token).roles, [])
        self.assert_no_airtable_calls()

    def test_unknown_external_is_refused(self) -> None:
        """Covers an external known only to the Airtable Access Control
        table too: with Airtable no longer consulted, it is indistinguishable
        from an unknown email (owner accepted, 2026-10-31)."""
        with self.assertRaises(UserNotAuthorizedError):
            self._callback_as(EXTERNAL)
        self.assert_no_airtable_calls()




class TestAirtableAccessControlSurfaceIsGone(unittest.TestCase):
    """Step 5 of plan_login_airtable_removal_2026-10-31.md: the Airtable
    Access Control table's readers/writers, routes and settings are deleted."""

    def test_service_methods_are_gone(self) -> None:
        for name in (
            *_FORMER_LOGIN_AIRTABLE_CALLS,
            "get_user_scoped_roles",
            "is_admin",
            "list_access_control_records",
            "upsert_access_control",
            "revoke_access_control",
        ):
            self.assertFalse(hasattr(AirtableService, name), name)

    def test_nothing_in_app_still_calls_a_deleted_method(self) -> None:
        """A merge can bring back a caller after the method is gone: the
        sandbox merge after Step 5 re-added `get_user_roles` to
        /airtable/personal-context, which then returned 500 on every call."""
        deleted = "|".join(
            (
                *_FORMER_LOGIN_AIRTABLE_CALLS,
                "get_user_scoped_roles",
                "is_admin",
                "list_access_control_records",
                "upsert_access_control",
                "revoke_access_control",
            )
        )
        call = re.compile(rf"\bairtable_service\.({deleted})\b")
        app_dir = Path(auth_module.__file__).resolve().parents[1]
        found = [
            f"{path.relative_to(app_dir)}:{number}"
            for path in app_dir.rglob("*.py")
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if call.search(line)
        ]
        self.assertEqual(found, [])

    def test_routes_are_gone(self) -> None:
        from app.routers import airtable as airtable_router

        prefix = airtable_router.router.prefix
        paths = {getattr(r, "path", "") for r in airtable_router.router.routes}
        for path in ("/access-control", "/access-control/assign", "/access-control/revoke"):
            self.assertNotIn(prefix + path, paths)
        # The Roles / Teams endpoints under the same prefix stay.
        self.assertIn(prefix + "/access-control/roles", paths)
        self.assertIn(prefix + "/access-control/team-emails", paths)

    def test_settings_are_gone(self) -> None:
        settings = get_settings()
        for name in ("ACCESS_CONTROL_TABLE", "ADMINS_TABLE", "DEFAULT_MEMBER_ROLE"):
            self.assertFalse(hasattr(settings, name), name)


if __name__ == "__main__":
    unittest.main()
