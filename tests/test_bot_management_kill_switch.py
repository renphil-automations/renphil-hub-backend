"""Turning access off must never depend on the Agent being reachable.

Regression for QA 2026-10-24 (BM1b, M-DRIVE-DEADLOCK): every settings save
re-validated the unchanged model against Agent /agent/models (and re-probed
Drive readiness while Drive was on), so External AI Off, provider Off and
RenPhil AI Off could not be saved during an Agent outage -- the emergency off
switch failed exactly when it was needed.
"""
from __future__ import annotations

import inspect
import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app.routers import bot_management
from tests.test_bot_management_mcp import ADMIN, _app_settings, _Base

HAIKU = "anthropic_claude_haiku_45"
AGENT_ENV = {"AGENT_API_URL": "https://agent.internal.example"}


class KillSwitchWithAgentDownTests(_Base):
    def _save(self, *, model: str = HAIKU, **fields):
        fields.setdefault("expected_version_id", bot_management._active_version(self.db).id)
        request = bot_management.UpdateBotSettingsRequest(active_model_key=model, **fields)
        http_request = SimpleNamespace(headers={"Authorization": "Bearer admin-session"})
        return bot_management.update_settings(request, http_request, current_user=ADMIN, db=self.db)

    def _enable_external_access(self):
        # Set up a state with external access on while the Agent is healthy.
        with patch.object(bot_management, "_validate_active_model_key", return_value=None):
            self._save(mcp_external_connections_enabled=True, mcp_claude_enabled=True)

    def _agent_down(self):
        return patch.object(bot_management.httpx, "get", side_effect=httpx.ConnectError("agent unreachable"))

    def test_external_ai_off_saves_while_agent_is_unreachable(self):
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV):
            self._enable_external_access()
            with self._agent_down() as agent_get:
                saved = self._save(mcp_external_connections_enabled=False)["active"]["settings"]
        self.assertFalse(saved["mcp_external_connections_enabled"])
        agent_get.assert_not_called()

    def test_provider_off_saves_while_agent_is_unreachable(self):
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV):
            self._enable_external_access()
            with self._agent_down() as agent_get:
                saved = self._save(mcp_external_connections_enabled=True, mcp_claude_enabled=False)["active"]["settings"]
        self.assertFalse(saved["mcp_claude_enabled"])
        agent_get.assert_not_called()

    def test_renphil_ai_off_saves_while_agent_is_unreachable(self):
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV):
            with self._agent_down() as agent_get:
                saved = self._save(bot_enabled=False)["active"]["settings"]
        self.assertFalse(saved["bot_enabled"])
        agent_get.assert_not_called()

    def test_changing_the_model_still_requires_the_agent_registry(self):
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV):
            with self._agent_down(), self.assertRaises(HTTPException) as caught:
                self._save(model="openai_gpt_5_mini")
        self.assertEqual(caught.exception.status_code, 503)

    def test_drive_already_on_does_not_block_other_saves(self):
        ready = {"drive_knowledge": {"available": True}}
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV):
            with patch.object(bot_management, "_validate_active_model_key", return_value=None), \
                    patch.object(bot_management, "_drive_runtime_status", return_value=ready):
                self._save(google_drive_knowledge_enabled=True)
            with self._agent_down() as agent_get, \
                    patch.object(bot_management, "_drive_runtime_status", side_effect=AssertionError("must not probe")):
                saved = self._save(google_drive_knowledge_enabled=True, answer_cache_enabled=False)["active"]["settings"]
        self.assertTrue(saved["google_drive_knowledge_enabled"])
        self.assertFalse(saved["answer_cache_enabled"])
        agent_get.assert_not_called()

    def test_turning_drive_off_while_it_is_unavailable_saves(self):
        ready = {"drive_knowledge": {"available": True}}
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV):
            with patch.object(bot_management, "_validate_active_model_key", return_value=None), \
                    patch.object(bot_management, "_drive_runtime_status", return_value=ready):
                self._save(google_drive_knowledge_enabled=True)
            with self._agent_down():
                saved = self._save(google_drive_knowledge_enabled=False)["active"]["settings"]
        self.assertFalse(saved["google_drive_knowledge_enabled"])

    def test_enabling_drive_while_not_ready_is_still_refused(self):
        not_ready = {"drive_knowledge": {"available": False}}
        with patch("app.config.get_settings", return_value=_app_settings()), patch.dict(os.environ, AGENT_ENV), \
                patch.object(bot_management, "_drive_runtime_status", return_value=not_ready):
            with self.assertRaises(HTTPException) as caught:
                self._save(google_drive_knowledge_enabled=True)
        self.assertEqual(caught.exception.status_code, 409)


def test_admin_dependency_does_not_block_the_event_loop():
    # A sync dependency runs in FastAPI's threadpool; an ``async def`` one runs
    # its blocking Postgres queries on the event loop and stalls every request.
    assert not inspect.iscoroutinefunction(bot_management.require_bot_management_admin)
