from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import HTTPException

from app.routers import bot_management


def _response(status_code: int, payload: object) -> httpx.Response:
    request = httpx.Request("GET", "https://agent.example/agent/runtime-status")
    return httpx.Response(status_code, json=payload, request=request)


def test_runtime_status_proxies_the_safe_agent_contract(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_API_URL", "https://agent.example")
    observed: dict[str, object] = {}

    def fake_get(url: str, *, headers: dict[str, str], timeout: float):
        observed.update(url=url, headers=headers, timeout=timeout)
        return _response(200, {"drive_knowledge": {"available": True, "reason": "Ready"}})

    monkeypatch.setattr(bot_management.httpx, "get", fake_get)

    assert bot_management._drive_runtime_status("Bearer verified-admin") == {
        "drive_knowledge": {"available": True, "reason": "Ready"}
    }
    assert observed == {
        "url": "https://agent.example/agent/runtime-status",
        "headers": {"Accept": "application/json", "Authorization": "Bearer verified-admin"},
        "timeout": 10.0,
    }


def test_runtime_status_logs_the_upstream_status_without_exposing_it_to_browser(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("AGENT_API_URL", "https://agent.example")
    monkeypatch.setattr(bot_management.httpx, "get", lambda *args, **kwargs: _response(404, {"detail": "not found"}))

    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as exc_info:
        bot_management._drive_runtime_status("Bearer verified-admin")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "message": "Agent runtime status is unavailable",
        "category": "agent_response_rejected",
    }
    assert "status_code=404" in caplog.text
    assert "not found" not in caplog.text


def test_runtime_status_reports_missing_agent_configuration_as_a_safe_category(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENT_API_URL", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        bot_management._drive_runtime_status(None)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["category"] == "agent_not_configured"


def test_runtime_status_reports_an_unreachable_agent_as_a_safe_category(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_API_URL", "https://agent.example")

    def fail_get(*args, **kwargs):
        raise httpx.ConnectTimeout("connect timed out")

    monkeypatch.setattr(bot_management.httpx, "get", fail_get)

    with pytest.raises(HTTPException) as exc_info:
        bot_management._drive_runtime_status(None)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["category"] == "agent_unreachable"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"drive_knowledge": {"available": "true", "reason": "Ready"}},
        {"drive_knowledge": {"available": True}},
    ],
)
def test_runtime_status_rejects_an_invalid_agent_contract(monkeypatch: pytest.MonkeyPatch, payload: object):
    monkeypatch.setenv("AGENT_API_URL", "https://agent.example")
    monkeypatch.setattr(bot_management.httpx, "get", lambda *args, **kwargs: _response(200, payload))

    with pytest.raises(HTTPException) as exc_info:
        bot_management._drive_runtime_status(None)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["category"] == "agent_invalid_contract"
