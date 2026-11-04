from __future__ import annotations

import asyncio
import unittest

from app.helpers.exceptions import UserNotAuthorizedError
from app.services.auth_service import AuthService


class _FakeSettings:
    """Only the attribute the gate reads."""

    def __init__(self, allowed: str) -> None:
        self.ALLOWED_EMAIL_DOMAIN = allowed

    @property
    def allowed_email_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.ALLOWED_EMAIL_DOMAIN.split(",") if d.strip()]


class _FakeAirtable:
    """Stub Access Control lookup — no real email needed."""

    def __init__(self, known: set[str] | None = None) -> None:
        self._known = known or set()

    async def access_control_email_exists(self, email: str) -> bool:
        return email.lower() in self._known


class LoginAllowedDomainTests(unittest.TestCase):
    def _gate(self, allowed: str, email: str, known: set[str] | None = None) -> None:
        service = AuthService.__new__(AuthService)
        service._settings = _FakeSettings(allowed)
        asyncio.run(service._enforce_login_allowed(email, _FakeAirtable(known)))

    def test_second_domain_is_allowed(self) -> None:
        # Would raise if the CCF domain were rejected.
        self._gate("renphil.org, ccf.org", "someone@ccf.org")

    def test_original_domain_still_allowed(self) -> None:
        self._gate("renphil.org, ccf.org", "user@renphil.org")

    def test_unknown_external_domain_is_refused(self) -> None:
        with self.assertRaises(UserNotAuthorizedError):
            self._gate("renphil.org, ccf.org", "stranger@gmail.com")

    def test_external_email_in_access_control_is_allowed(self) -> None:
        self._gate(
            "renphil.org",
            "contractor@gmail.com",
            known={"contractor@gmail.com"},
        )


if __name__ == "__main__":
    unittest.main()
