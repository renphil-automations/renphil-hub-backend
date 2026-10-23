"""Strict approved-client admission and readiness for the RenPhil MCP."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_v2.database import BaseV2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.routers import agent_access
from app.services.agent_identity import AgentIdentity
from app.services.mcp_access_policy import (
    DEFAULT_MCP_SETTINGS,
    admit_client,
    load_approved_clients,
    normalized_mcp_settings,
    provider_readiness,
)

REGISTRY = (
    '{"renphil-agent":{"redirect_uris":["https://hub.example/agent"]},'
    '"claude-id":{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"]},'
    '"https://chatgpt.example/client.json":{"redirect_uris":["https://chatgpt.com/connector_platform_oauth_redirect"]},'
    '"gemini-id":{"redirect_uris":["https://gemini.example/callback"]},'
    '"other-id":{"redirect_uris":["https://other.example/callback"]}}'
)
MAPPING = '{"claude-id":"claude","https://chatgpt.example/client.json":"chatgpt","gemini-id":"gemini"}'
ALL_ON = {
    "bot_enabled": True,
    "mcp_external_connections_enabled": True,
    "mcp_claude_enabled": True, "mcp_chatgpt_enabled": True, "mcp_gemini_enabled": True,
}


def _config(**overrides) -> SimpleNamespace:
    values = dict(
        MCP_OAUTH_ENABLED=True, MCP_OAUTH_AGENT_CLIENT_ID="renphil-agent",
        MCP_OAUTH_CLIENTS_JSON=REGISTRY, MCP_OAUTH_CLIENT_PROVIDERS_JSON=MAPPING,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ApprovedClientPolicyTests(unittest.TestCase):
    def test_only_the_four_approved_callers_are_admitted(self):
        approved = load_approved_clients(_config())
        expected = {
            "renphil-agent": "hub_agent",
            "claude-id": "claude",
            "https://chatgpt.example/client.json": "chatgpt",
            "gemini-id": "gemini",
        }
        for client_id, kind in expected.items():
            decision = admit_client(active_settings=ALL_ON, client_id=client_id, approved=approved)
            self.assertTrue(decision.allowed, client_id)
            self.assertEqual(decision.client_kind, kind)
        for denied in ("other-id", "unknown", "", None, 42, "Claude-ID", "CLAUDE-ID", "claude-id ", " claude-id",
                       "claude-id​", "clаude-id", "claude", "claude-i", "claude-id/extra", "RENPHIL-AGENT",
                       "renphil-agent ", "https://chatgpt.example/client.json?x=1"):
            with self.subTest(client_id=denied):
                self.assertFalse(admit_client(active_settings=ALL_ON, client_id=denied, approved=approved).allowed)

    def test_external_providers_default_to_disabled(self):
        self.assertFalse(DEFAULT_MCP_SETTINGS["mcp_external_connections_enabled"])
        for provider in ("claude", "chatgpt", "gemini"):
            self.assertFalse(DEFAULT_MCP_SETTINGS[f"mcp_{provider}_enabled"])
            self.assertEqual(DEFAULT_MCP_SETTINGS[f"mcp_{provider}_verification"], "unverified")
        self.assertEqual(DEFAULT_MCP_SETTINGS["hub_ai_tool_mode"], "local")
        approved = load_approved_clients(_config())
        for settings in (None, {}, {"bot_enabled": True}):
            self.assertFalse(admit_client(active_settings=settings, client_id="claude-id", approved=approved).allowed)

    def test_master_toggle_denies_all_external_and_provider_toggle_denies_only_itself(self):
        approved = load_approved_clients(_config())
        master_off = {**ALL_ON, "mcp_external_connections_enabled": False}
        for client_id in ("claude-id", "https://chatgpt.example/client.json", "gemini-id"):
            decision = admit_client(active_settings=master_off, client_id=client_id, approved=approved)
            self.assertEqual((decision.allowed, decision.reason), (False, "external_disabled"))
        # Master off never affects the Hub Agent.
        self.assertTrue(admit_client(active_settings=master_off, client_id="renphil-agent", approved=approved).allowed)
        chatgpt_off = {**ALL_ON, "mcp_chatgpt_enabled": False}
        self.assertFalse(admit_client(active_settings=chatgpt_off, client_id="https://chatgpt.example/client.json", approved=approved).allowed)
        self.assertTrue(admit_client(active_settings=chatgpt_off, client_id="claude-id", approved=approved).allowed)
        self.assertTrue(admit_client(active_settings=chatgpt_off, client_id="gemini-id", approved=approved).allowed)
        # Truthy non-boolean values never enable a provider.
        self.assertFalse(admit_client(active_settings={**ALL_ON, "mcp_claude_enabled": "yes"}, client_id="claude-id", approved=approved).allowed)

    def test_hub_ai_disabled_denies_only_the_hub_agent(self):
        approved = load_approved_clients(_config())
        off = {**ALL_ON, "bot_enabled": False}
        decision = admit_client(active_settings=off, client_id="renphil-agent", approved=approved)
        self.assertEqual((decision.allowed, decision.reason), (False, "hub_ai_disabled"))
        # External access follows the master/provider toggles (admin's explicit choice).
        self.assertTrue(admit_client(active_settings=off, client_id="claude-id", approved=approved).allowed)

    def test_invalid_or_ambiguous_configuration_admits_no_external_client(self):
        bad_configs = (
            _config(MCP_OAUTH_CLIENT_PROVIDERS_JSON="not json"),
            _config(MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"claude-id":"copilot"}'),
            _config(MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"claude-id":"claude","gemini-id":"claude"}'),
            _config(MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"renphil-agent":"claude"}'),
            _config(MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"bad id":"claude"}'),
            _config(MCP_OAUTH_CLIENT_PROVIDERS_JSON='["claude-id"]'),
            _config(MCP_OAUTH_ENABLED=False),
        )
        for config in bad_configs:
            approved = load_approved_clients(config)
            with self.subTest(config=config.MCP_OAUTH_CLIENT_PROVIDERS_JSON):
                for client_id in ("claude-id", "gemini-id", "https://chatgpt.example/client.json"):
                    self.assertFalse(admit_client(active_settings=ALL_ON, client_id=client_id, approved=approved).allowed)

    def test_mapped_client_without_exact_registered_callback_is_not_admissible(self):
        registry = '{"claude-id":{"redirect_uris":["https://*.claude.ai/callback"]},"gemini-id":{"redirect_uris":[]}}'
        approved = load_approved_clients(_config(MCP_OAUTH_CLIENTS_JSON=registry))
        self.assertFalse(admit_client(active_settings=ALL_ON, client_id="claude-id", approved=approved).allowed)
        self.assertFalse(admit_client(active_settings=ALL_ON, client_id="gemini-id", approved=approved).allowed)
        self.assertFalse(approved.mapped("claude"))

    def test_readiness_states_never_claim_ready_without_mapping_or_verification(self):
        approved = load_approved_clients(_config(MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"claude-id":"claude","gemini-id":"gemini"}'))
        settings = {
            **ALL_ON,
            "mcp_claude_verification": "unverified",
            "mcp_chatgpt_verification": "deployment_verified",  # no mapping
            "mcp_gemini_verification": "dpop_staging_verified",
        }
        rows = {row["provider"]: row for row in provider_readiness(active_settings=settings, approved=approved)}
        self.assertEqual(rows["claude"]["readiness"], "blocked_pending_dpop_verification")
        self.assertEqual(rows["chatgpt"]["readiness"], "needs_client_mapping")
        self.assertEqual(rows["chatgpt"]["readiness_label"], "Needs approved client mapping")
        self.assertEqual(rows["gemini"]["readiness"], "ready_for_staging_qa")
        rows = {row["provider"]: row for row in provider_readiness(
            active_settings={**settings, "mcp_gemini_verification": "deployment_verified"}, approved=approved)}
        self.assertEqual(rows["gemini"]["readiness"], "operational")
        rows = {row["provider"]: row for row in provider_readiness(active_settings={}, approved=approved)}
        self.assertEqual({row["readiness"] for row in rows.values()}, {"disabled"})
        # Readiness rows never contain client identifiers or callback URLs.
        self.assertNotIn("claude-id", repr(rows))
        self.assertNotIn("https://", repr(rows))

    def test_normalization_rejects_unknown_values(self):
        values = normalized_mcp_settings({"hub_ai_tool_mode": "compare", "mcp_claude_verification": "trusted"})
        self.assertEqual(values["hub_ai_tool_mode"], "local")
        self.assertEqual(values["mcp_claude_verification"], "unverified")


class CapabilityCallAdmissionTests(unittest.TestCase):
    """Hub re-checks client admission on every MCP capability call."""

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        BaseV2.metadata.create_all(engine, tables=[BotConfigurationVersionV2.__table__, BotConfigurationStateV2.__table__])
        self.db = sessionmaker(bind=engine)()

    def tearDown(self) -> None:
        self.db.close()

    def _activate(self, settings: dict) -> None:
        from datetime import datetime, timezone
        version = BotConfigurationVersionV2(
            version=self.db.query(BotConfigurationVersionV2).count() + 1, settings=settings, is_default=False,
            created_by_email="admin@renphil.org", created_at=datetime.now(timezone.utc),
        )
        self.db.add(version)
        self.db.flush()
        state = self.db.get(BotConfigurationStateV2, 1)
        if state is None:
            self.db.add(BotConfigurationStateV2(id=1, active_version_id=version.id, updated_at=datetime.now(timezone.utc)))
        else:
            state.active_version_id = version.id
        self.db.commit()

    def _call(self, client_id: str):
        identity = AgentIdentity(email="member@renphil.org", roles=("Hub Admin",))
        acl = {"email": "member@renphil.org", "is_admin": False, "granted_node_keys": ["tab:1"], "authorization_fingerprint": "f", "resolved_roles": ["Member"]}
        with patch.object(agent_access, "get_settings", return_value=_config()), \
                patch.object(agent_access, "_build_verified_live_acl_response", AsyncMock(return_value=dict(acl))):
            return asyncio.run(agent_access.get_verified_granted_nodes(
                client_id=client_id, identity=identity, db=self.db, airtable_service=object(),
            ))

    def test_toggle_change_takes_effect_on_the_next_tool_call(self):
        self._activate(ALL_ON)
        self.assertEqual(self._call("claude-id")["client_kind"], "claude")
        self._activate({**ALL_ON, "mcp_claude_enabled": False})
        with self.assertRaises(HTTPException) as caught:
            self._call("claude-id")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self._call("gemini-id")["client_kind"], "gemini")
        self._activate({**ALL_ON, "mcp_external_connections_enabled": False})
        for client_id in ("claude-id", "gemini-id", "https://chatgpt.example/client.json"):
            with self.assertRaises(HTTPException):
                self._call(client_id)
        self.assertEqual(self._call("renphil-agent")["client_kind"], "hub_agent")

    def test_unknown_clients_and_hub_ai_off_are_denied(self):
        self._activate(ALL_ON)
        for client_id in ("other-id", "Claude-ID", "renphil-agent2"):
            with self.assertRaises(HTTPException) as caught:
                self._call(client_id)
            self.assertEqual(caught.exception.status_code, 403)
        self._activate({**ALL_ON, "bot_enabled": False})
        with self.assertRaises(HTTPException):
            self._call("renphil-agent")


if __name__ == "__main__":
    unittest.main()
