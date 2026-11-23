"""S3: refresh-token revocation (RFC 7009 endpoint and Bot Management switch-off)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import app.routers.bot_management as bot_management
from app.db_v2.models.mcp_oauth import McpOAuthRefreshTokenV2
from app.services.mcp_oauth_client_secret import hash_client_secret
from app.services.mcp_oauth_service import LocalMcpOAuthService
from tests.test_mcp_oauth_dpop_replay import Signer
import tests.test_mcp_oauth_refresh_tokens as refresh_tests
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ENABLED_EXTERNAL, GPT_CLIENT, mcp_settings


class _Harness(refresh_tests.RefreshEnabledTests):
    """Reuses the refresh-token fixture; its own tests are not re-run here."""


for _name in [name for name in dir(refresh_tests.RefreshEnabledTests) if name.startswith("test_")]:
    setattr(_Harness, _name, None)


class RevocationEndpointTests(_Harness):
    def revoke(self, http, token, **extra):
        return http.post("/mcp/oauth/revoke", data={"token": token, **extra})

    def test_revoking_a_refresh_token_revokes_its_whole_family(self):
        http = self.client()
        first = self._login(http)["refresh_token"]
        second = self._refresh(http, first).json()["refresh_token"]
        other = self._login(http)["refresh_token"]
        response = self.revoke(http, second, client_id=CLAUDE_CLIENT, token_type_hint="refresh_token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(self._refresh(http, second).json()["error"], "invalid_grant")
        # Another login's family is untouched.
        self.assertEqual(self._refresh(http, other).status_code, 200)

    def test_unknown_or_foreign_tokens_still_get_200_and_change_nothing(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        self.assertEqual(self.revoke(http, "never-issued", client_id=CLAUDE_CLIENT).status_code, 200)
        self.assertEqual(self.revoke(http, "an.access.token", client_id=CLAUDE_CLIENT, token_type_hint="access_token").status_code, 200)
        # Presented by another client: 200 per RFC 7009, but not revoked.
        self.assertEqual(self.revoke(http, token, client_id=GPT_CLIENT).status_code, 200)
        self.assertTrue(all(row.revoked_at is None for row in self._rows()))

    def test_client_authentication_is_required_like_the_token_endpoint(self):
        registry = json.loads(self.settings.MCP_OAUTH_CLIENTS_JSON)
        registry[CLAUDE_CLIENT]["client_secret_hash"] = hash_client_secret("s3cret-value", iterations=100_000)
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_CLIENTS_JSON=json.dumps(registry))
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)
        http = self.client()
        token = self._login(http, extra={"client_secret": "s3cret-value"})["refresh_token"]
        refused = self.revoke(http, token, client_id=CLAUDE_CLIENT)
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json()["error"], "invalid_client")
        self.assertIsNone(self._rows()[0].revoked_at)
        self.assertEqual(self.revoke(http, token, client_id=CLAUDE_CLIENT, client_secret="s3cret-value").status_code, 200)
        self.assertIsNotNone(self._rows()[0].revoked_at)

    def test_missing_token_is_invalid_request_and_metadata_advertises_the_endpoint(self):
        http = self.client()
        missing = http.post("/mcp/oauth/revoke", data={"client_id": CLAUDE_CLIENT})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"], "invalid_request")
        metadata = http.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["revocation_endpoint"], "https://hub.example/mcp/oauth/revoke")
        self.assertIn("none", metadata["revocation_endpoint_auth_methods_supported"])

    def test_revocation_works_even_when_the_provider_is_switched_off(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        self._activate({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
        self.assertEqual(self.revoke(http, token, client_id=CLAUDE_CLIENT).status_code, 200)
        self.assertIsNotNone(self._rows()[0].revoked_at)


class BotManagementSwitchOffTests(_Harness):
    def _save(self, settings: dict):
        from app.models.auth import UserInfo

        admin = UserInfo(email="admin@renphil.org", name="Admin", roles=["Hub Admin"])
        with patch.object(bot_management, "get_app_settings", lambda: self.settings):
            bot_management._save_version(self.db, admin, settings, "settings_updated")

    def _families(self):
        return {row.client_id: row.revoked_at is not None for row in self._rows()}

    def test_switching_a_provider_off_revokes_that_providers_families_only(self):
        from app.db_v2.database import BaseV2
        from app.db_v2.models.bot_management import BotSettingsAuditLogV2

        BaseV2.metadata.create_all(self.engine, tables=[BotSettingsAuditLogV2.__table__])
        http = self.client()
        self._login(http)
        self.db.add(McpOAuthRefreshTokenV2(
            family_id="gpt-family", token_hash="f" * 64, client_id=GPT_CLIENT, user_email="x@renphil.org", user_subject="9",
            jkt=None, scope="renphil.mcp.read", created_at=self._rows()[0].created_at, expires_at=self._rows()[0].expires_at,
            family_expires_at=self._rows()[0].family_expires_at,
        ))
        self.db.commit()
        self._save({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
        self.assertEqual(self._families(), {CLAUDE_CLIENT: True, GPT_CLIENT: False})
        self._save({**ENABLED_EXTERNAL, "mcp_external_connections_enabled": False})
        self.assertEqual(self._families(), {CLAUDE_CLIENT: True, GPT_CLIENT: True})

    def test_refresh_disabled_never_touches_the_table(self):
        from app.db_v2.database import BaseV2
        from app.db_v2.models.bot_management import BotSettingsAuditLogV2

        BaseV2.metadata.create_all(self.engine, tables=[BotSettingsAuditLogV2.__table__])
        self.settings = mcp_settings()
        McpOAuthRefreshTokenV2.__table__.drop(self.engine)
        self._save({**ENABLED_EXTERNAL, "mcp_external_connections_enabled": False})  # no error

    def test_a_revocation_failure_never_blocks_the_switch_off(self):
        from app.db_v2.database import BaseV2
        from app.db_v2.models.bot_management import BotSettingsAuditLogV2

        BaseV2.metadata.create_all(self.engine, tables=[BotSettingsAuditLogV2.__table__])
        McpOAuthRefreshTokenV2.__table__.drop(self.engine)  # enabled but table missing
        self._save({**ENABLED_EXTERNAL, "mcp_external_connections_enabled": False})
        self.assertFalse(bot_management._active_version(self.db).settings["mcp_external_connections_enabled"])

    def test_a_row_committed_by_an_in_flight_refresh_is_also_revoked(self):
        # N-3: on PostgreSQL a refresh holding its row lock makes the bulk
        # UPDATE wait, then commits a new row that UPDATE's snapshot misses.
        # Simulate that commit landing right after the first pass.
        from sqlalchemy.orm import Query

        from app.services.mcp_oauth_service import revoke_refresh_families_for_clients

        now = datetime.now(timezone.utc)
        self.db.add(McpOAuthRefreshTokenV2(
            family_id="claude-family", token_hash="a" * 64, client_id=CLAUDE_CLIENT, user_email="x@renphil.org",
            user_subject="9", jkt=None, scope="renphil.mcp.read", created_at=now,
            expires_at=now + timedelta(hours=1), family_expires_at=now + timedelta(days=1),
        ))
        self.db.commit()
        original_update = Query.update
        passes = []

        def racing_update(query, values, **kwargs):
            result = original_update(query, values, **kwargs)
            if not passes:
                query.session.add(McpOAuthRefreshTokenV2(
                    family_id="claude-family", token_hash="b" * 64, client_id=CLAUDE_CLIENT,
                    user_email="x@renphil.org", user_subject="9", jkt=None, scope="renphil.mcp.read",
                    created_at=now, expires_at=now + timedelta(hours=1), family_expires_at=now + timedelta(days=1),
                ))
                query.session.flush()
            passes.append(result)
            return result

        with patch.object(Query, "update", racing_update):
            revoke_refresh_families_for_clients(self.db, [CLAUDE_CLIENT])
        self.assertGreaterEqual(len(passes), 2)
        self.assertTrue(all(row.revoked_at is not None for row in self._rows() if row.client_id == CLAUDE_CLIENT))

class FamilyRevocationRaceTests(_Harness):
    """A refresh holding the family's live row commits a new row after the
    family UPDATE waited on it (PostgreSQL READ COMMITTED). Simulate that
    commit landing right after the first pass."""

    def _racing_update(self, row_template):
        from sqlalchemy.orm import Query

        original_update = Query.update
        passes = []

        def racing_update(query, values, **kwargs):
            result = original_update(query, values, **kwargs)
            if not passes:
                query.session.add(McpOAuthRefreshTokenV2(
                    family_id=row_template.family_id, token_hash="c" * 64, client_id=row_template.client_id,
                    user_email=row_template.user_email, user_subject=row_template.user_subject, jkt=row_template.jkt,
                    scope=row_template.scope, created_at=row_template.created_at, expires_at=row_template.expires_at,
                    family_expires_at=row_template.family_expires_at,
                ))
                query.session.flush()
            passes.append(result)
            return result

        return patch.object(Query, "update", racing_update), passes

    def test_rfc7009_revoke_also_revokes_a_row_committed_by_an_in_flight_refresh(self):
        token = self._login(self.client())["refresh_token"]
        racing, passes = self._racing_update(self._rows()[0])
        with racing:
            self.service.revoke_refresh_token(self.db, token, client_id=CLAUDE_CLIENT)
        self.assertGreaterEqual(len(passes), 2)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.revoked_at is not None for row in rows))

    def test_reuse_detection_also_revokes_a_row_committed_by_an_in_flight_refresh(self):
        http = self.client()
        first = self._login(http)["refresh_token"]
        self.assertEqual(self._refresh(http, first).status_code, 200)
        racing, passes = self._racing_update(self._rows()[-1])
        with racing:
            self.assertEqual(self._refresh(http, first).json()["error"], "invalid_grant")  # replay
        self.assertGreaterEqual(len(passes), 2)
        self.assertTrue(all(row.revoked_at is not None for row in self._rows()))

    def test_expired_rows_are_still_purged_and_live_rows_kept(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        live = self._rows()[0]
        past = live.created_at - timedelta(days=60)
        self.db.add(McpOAuthRefreshTokenV2(
            family_id="old-family", token_hash="d" * 64, client_id=CLAUDE_CLIENT, user_email="x@renphil.org",
            user_subject="9", jkt=None, scope="renphil.mcp.read", created_at=past,
            expires_at=past + timedelta(hours=1), family_expires_at=past + timedelta(days=1),
        ))
        self.db.commit()
        self.assertEqual(self._refresh(http, token).status_code, 200)
        families = {row.family_id for row in self._rows()}
        self.assertNotIn("old-family", families)
        self.assertEqual(len(self._rows()), 2)

class ConsentTextTests(_Harness):
    def test_consent_page_describes_revocation_truthfully(self):
        import re
        from urllib.parse import parse_qs, urlsplit

        from fastapi.testclient import TestClient

        import app.routers.mcp_oauth as router

        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=True)
        http = TestClient(self.app(), base_url="https://hub.example", follow_redirects=False)
        started = http.get("/mcp/oauth/authorize", params=self.authorize_params())
        continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
        with self.Session() as session:
            url = router.complete_hub_login(db=session, continuation_uri=continuation, email="m@renphil.org", subject="42", cookies=dict(http.cookies))
        page = http.get("/mcp/oauth/consent", params={"request_id": parse_qs(urlsplit(url).query)["request_id"][0]}).text
        self.assertNotIn("at any time by disconnecting it in the application", page)
        self.assertIn("RenPhil Hub administrators can turn this access off at any time", page)
        self.assertIn("Disconnecting the connector in Claude may also revoke it", page)
