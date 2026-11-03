"""The Bot Management runtime-status proxy must authenticate to the Agent."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routers import bot_management


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class RuntimeStatusProxyTests(unittest.TestCase):
    def test_model_probe_timeout_comes_from_validated_settings(self) -> None:
        with patch.object(
            bot_management,
            "get_app_settings",
            lambda: SimpleNamespace(AGENT_MODELS_TIMEOUT_SECONDS=37.5),
        ):
            self.assertEqual(bot_management._agent_models_timeout_seconds(), 37.5)

    def test_sends_agent_sync_token(self) -> None:
        seen: dict = {}

        def fake_get(url, headers, timeout):
            seen["url"], seen["headers"] = url, headers
            return _Response({"drive_knowledge": {"available": True, "reason": "ok"}})

        with (
            patch.dict("os.environ", {"AGENT_API_URL": "http://agent.test/"}),
            patch.object(
                bot_management,
                "get_app_settings",
                lambda: SimpleNamespace(
                    AGENT_API_URL="http://agent.test/",
                    AGENT_SYNC_TOKEN="shared-secret",
                ),
            ),
            patch.object(bot_management.httpx, "get", fake_get),
        ):
            payload = bot_management._drive_runtime_status("Bearer user-jwt")

        self.assertEqual(seen["url"], "http://agent.test/agent/runtime-status")
        self.assertEqual(seen["headers"]["X-Sync-Token"], "shared-secret")
        self.assertEqual(payload, {"drive_knowledge": {"available": True, "reason": "ok"}})

    def test_missing_sync_token_fails_closed_without_calling_the_agent(self) -> None:
        with (
            patch.dict("os.environ", {"AGENT_API_URL": "http://agent.test"}),
            patch.object(
                bot_management,
                "get_app_settings",
                lambda: SimpleNamespace(
                    AGENT_API_URL="http://agent.test",
                    AGENT_SYNC_TOKEN="",
                ),
            ),
            patch.object(bot_management.httpx, "get", lambda *a, **k: self.fail("must not call the Agent")),
        ):
            with self.assertRaises(HTTPException) as error:
                bot_management._drive_runtime_status(None)
        self.assertEqual(error.exception.status_code, 503)
