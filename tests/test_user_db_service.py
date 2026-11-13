"""`users` as the sign-in identity (hub_users folded into users, 2026-10).

`find_user_by_email` is the one lookup the login gate, sign-in provisioning,
grant targets and lock holders share; `update_user_by_work_email` must not let
an admin move someone's work_email onto an address another row signs in as.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-user-db-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.models.airtable import UserUpdate  # noqa: E402
from app.services.user_db_service import find_user_by_email, update_user_by_work_email  # noqa: E402


class UserDbFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        BaseV2.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all(
            [
                UserV2(name="Ada", work_email="ada@renphil.org"),
                UserV2(name="Bea", work_email="bea.long@renphil.org", alias_email="bea@renphil.org"),
                UserV2(name="No Email"),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()


class TestFindUserByEmail(UserDbFixture):
    def test_matches_work_email_case_and_whitespace_insensitively(self) -> None:
        self.assertEqual(find_user_by_email(self.db, "  ADA@renphil.org ").name, "Ada")

    def test_falls_back_to_alias_email(self) -> None:
        self.assertEqual(find_user_by_email(self.db, "bea@renphil.org").name, "Bea")

    def test_unknown_and_blank_find_nothing(self) -> None:
        self.assertIsNone(find_user_by_email(self.db, "nobody@renphil.org"))
        self.assertIsNone(find_user_by_email(self.db, "   "))


class TestModel(UserDbFixture):
    def test_email_hybrid_prefers_work_email(self) -> None:
        bea = find_user_by_email(self.db, "bea@renphil.org")
        self.assertEqual(bea.email, "bea.long@renphil.org")

    def test_name_defaults_to_the_login_email(self) -> None:
        self.db.add(UserV2(work_email="nameless@partner.example"))
        self.db.commit()
        self.assertEqual(find_user_by_email(self.db, "nameless@partner.example").name, "nameless@partner.example")

    def test_new_rows_have_not_signed_in(self) -> None:
        self.assertFalse(find_user_by_email(self.db, "ada@renphil.org").has_signed_in)

    def test_work_email_is_unique_case_insensitively(self) -> None:
        self.db.add(UserV2(name="Ada Two", work_email="ADA@renphil.org"))
        with self.assertRaises(IntegrityError):
            self.db.commit()

    def test_rows_without_email_do_not_collide(self) -> None:
        self.db.add_all([UserV2(name="Blank 1", work_email=""), UserV2(name="Blank 2")])
        self.db.commit()


class TestUpdateWorkEmail(UserDbFixture):
    def test_moving_onto_another_rows_work_email_is_409(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            update_user_by_work_email(self.db, "ada@renphil.org", UserUpdate(work_email="bea.long@renphil.org"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_moving_onto_another_rows_alias_is_409(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            update_user_by_work_email(self.db, "ada@renphil.org", UserUpdate(work_email="BEA@renphil.org"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_free_address_is_stored_normalized(self) -> None:
        record = update_user_by_work_email(self.db, "ada@renphil.org", UserUpdate(work_email=" Ada.New@Renphil.org "))
        self.assertEqual(record.work_email, "ada.new@renphil.org")

    def test_resaving_your_own_address_is_fine(self) -> None:
        record = update_user_by_work_email(self.db, "ada@renphil.org", UserUpdate(work_email="ada@renphil.org"))
        self.assertEqual(record.work_email, "ada@renphil.org")


if __name__ == "__main__":
    unittest.main()
