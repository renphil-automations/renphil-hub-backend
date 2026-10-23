"""Alembic coverage for the Hub MCP OAuth schema (``0001_mcp_oauth``).

The real ``migrations_v2`` chain has a single root revision, ``0001_mcp_oauth``
(``down_revision = None``); its preceding state is the empty Alembic base.
These tests run the real migration scripts against a temporary SQLite database.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db_v2.database import BaseV2
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REVISION = "0001_mcp_oauth"
REQUESTS, CODES = "mcp_oauth_authorization_requests", "mcp_oauth_authorization_codes"


class McpOAuthMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self.tmp.name) / 'v2.db'}"
        self.config = Config(str(BACKEND_ROOT / "alembic.ini"))
        self.config.set_main_option("script_location", str(BACKEND_ROOT / "migrations_v2"))
        self.config.set_main_option("sqlalchemy.url", self.url)
        self.engine = create_engine(self.url)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def _tables(self) -> set[str]:
        return set(inspect(self.engine).get_table_names())

    def _indexes(self, table: str) -> dict[str, bool]:
        return {index["name"]: bool(index["unique"]) for index in inspect(self.engine).get_indexes(table)}

    def _columns(self, table: str) -> dict[str, dict]:
        return {column["name"]: column for column in inspect(self.engine).get_columns(table)}

    def _current(self) -> set[str]:
        if "alembic_version" not in self._tables():
            return set()
        with self.engine.connect() as connection:
            return {row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))}

    def test_real_chain_has_one_root_head(self):
        script = ScriptDirectory.from_config(self.config)
        self.assertEqual(script.get_heads(), [REVISION])
        self.assertEqual(script.get_bases(), [REVISION])
        revision = script.get_revision(REVISION)
        self.assertIsNone(revision.down_revision)
        self.assertIsNone(revision.dependencies)

    def test_fresh_upgrade_creates_tables_columns_indexes_and_constraints(self):
        command.upgrade(self.config, "head")
        self.assertEqual(self._current(), {REVISION})
        self.assertTrue({REQUESTS, CODES} <= self._tables())
        code_columns = self._columns(CODES)
        self.assertEqual(
            set(code_columns),
            {"id", "code_hash", "client_id", "redirect_uri", "code_challenge", "user_email", "user_subject", "user_roles_json", "created_at"},
        )
        self.assertEqual(
            set(self._columns(REQUESTS)),
            {"id", "request_hash", "client_id", "redirect_uri", "client_state", "code_challenge", "created_at"},
        )
        for name in ("code_hash", "client_id", "redirect_uri", "code_challenge", "user_email", "user_subject", "created_at"):
            self.assertFalse(code_columns[name]["nullable"], name)
        self.assertEqual(self._indexes(REQUESTS), {
            "ix_mcp_oauth_authorization_requests_request_hash": True,
            "ix_mcp_oauth_authorization_requests_client_id": False,
            "ix_mcp_oauth_authorization_requests_created_at": False,
        })
        self.assertEqual(self._indexes(CODES), {
            "ix_mcp_oauth_authorization_codes_code_hash": True,
            "ix_mcp_oauth_authorization_codes_client_id": False,
            "ix_mcp_oauth_authorization_codes_user_email": False,
            "ix_mcp_oauth_authorization_codes_created_at": False,
            "ix_mcp_oauth_authorization_codes_active": False,
        })

    def test_downgrade_to_base_and_re_upgrade(self):
        command.upgrade(self.config, "head")
        command.downgrade(self.config, "base")
        self.assertEqual(self._current(), set())
        self.assertFalse({REQUESTS, CODES} & self._tables())
        command.upgrade(self.config, "head")
        self.assertEqual(self._current(), {REVISION})
        self.assertTrue({REQUESTS, CODES} <= self._tables())

    def test_upgrade_is_idempotent_over_model_created_tables_without_the_role_column(self):
        BaseV2.metadata.create_all(
            bind=self.engine,
            tables=[McpOAuthAuthorizationRequestV2.__table__, McpOAuthAuthorizationCodeV2.__table__],
        )
        with self.engine.begin() as connection:
            connection.execute(text("DROP INDEX IF EXISTS ix_mcp_oauth_authorization_codes_active"))
            connection.execute(text(f"ALTER TABLE {CODES} DROP COLUMN user_roles_json"))
        command.upgrade(self.config, "head")
        self.assertIn("user_roles_json", self._columns(CODES))
        self.assertIn("ix_mcp_oauth_authorization_codes_active", self._indexes(CODES))

    def test_unique_code_hash_constraint_and_model_round_trip(self):
        command.upgrade(self.config, "head")
        session = sessionmaker(bind=self.engine)()
        now = datetime.now(timezone.utc)
        try:
            session.add(McpOAuthAuthorizationRequestV2(
                request_hash="a" * 64, client_id="claude", redirect_uri="https://c.example/cb",
                client_state="s", code_challenge="c" * 43, created_at=now,
            ))
            session.add(McpOAuthAuthorizationCodeV2(
                code_hash="b" * 64, client_id="claude", redirect_uri="https://c.example/cb",
                code_challenge="c" * 43, user_email="member@renphil.org", user_subject="42", created_at=now,
            ))
            session.commit()
            self.assertEqual(session.query(McpOAuthAuthorizationCodeV2).one().user_roles_json, "[]")
            session.add(McpOAuthAuthorizationCodeV2(
                code_hash="b" * 64, client_id="claude", redirect_uri="https://c.example/cb",
                code_challenge="c" * 43, user_email="other@renphil.org", user_subject="43", created_at=now,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.close()

    def test_service_purges_expired_rows_on_the_migrated_schema(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from app.services.mcp_oauth_keys import HubMcpSigningKey
        from app.services.mcp_oauth_service import LocalMcpOAuthService, PersistentMcpOAuthService

        command.upgrade(self.config, "head")
        session = sessionmaker(bind=self.engine)()
        old = datetime.now(timezone.utc) - timedelta(seconds=600)
        session.add(McpOAuthAuthorizationRequestV2(
            request_hash="e" * 64, client_id="claude", redirect_uri="https://c.example/cb",
            client_state="s", code_challenge="c" * 43, created_at=old,
        ))
        session.add(McpOAuthAuthorizationCodeV2(
            code_hash="f" * 64, client_id="claude", redirect_uri="https://c.example/cb",
            code_challenge="c" * 43, user_email="member@renphil.org", user_subject="42", created_at=old,
        ))
        session.commit()
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"claude":{"redirect_uris":["https://c.example/cb"]}}'),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp",
            signing_key=HubMcpSigningKey(private_pem=pem, key_id="k"), code_ttl_seconds=120,
        )
        service.start_authorization(
            session, client_id="claude", redirect_uri="https://c.example/cb", state="s",
            code_challenge="c" * 43, code_challenge_method="S256",
        )
        # Expired code and request are purged; only the fresh request remains.
        self.assertEqual(session.query(McpOAuthAuthorizationCodeV2).count(), 0)
        self.assertEqual(session.query(McpOAuthAuthorizationRequestV2).count(), 1)
        session.close()


if __name__ == "__main__":
    unittest.main()
