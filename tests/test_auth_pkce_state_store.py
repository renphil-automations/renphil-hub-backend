"""The Google login state store: TTL, size cap, single use.

``get_authorization_url`` records ``state -> (code_verifier, redirect_uri)``
and ``handle_callback`` consumes it. The store is per process; entries must
not live forever (abandoned logins) nor grow without bound.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app.services.auth_service as auth_module
from app.helpers.exceptions import GoogleOAuthError


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store(clock, *, ttl=600.0, max_entries=10_000):
    return auth_module._PkceStateStore(ttl_seconds=ttl, max_entries=max_entries, clock=clock)


def test_defaults_are_ten_minutes_and_ten_thousand_entries():
    assert auth_module.PKCE_STATE_TTL_SECONDS == 600
    assert auth_module.PKCE_STATE_MAX_ENTRIES == 10_000
    assert isinstance(auth_module._pkce_store, auth_module._PkceStateStore)


def test_an_entry_is_returned_once_within_its_ttl():
    clock = _Clock()
    store = _store(clock)
    store["s"] = ("verifier", "https://hub.example/auth/callback")
    clock.now += 599
    assert store.pop("s", None) == ("verifier", "https://hub.example/auth/callback")
    # Single use: a replayed state is gone.
    assert store.pop("s", None) is None


def test_an_expired_entry_is_never_returned():
    clock = _Clock()
    store = _store(clock)
    store["s"] = ("verifier", "uri")
    clock.now += 600
    assert store.pop("s", None) is None


def test_expired_entries_are_purged_on_access():
    clock = _Clock()
    store = _store(clock)
    store["old-1"] = ("v", "u")
    store["old-2"] = ("v", "u")
    clock.now += 601
    store["fresh"] = ("v", "u")
    assert len(store) == 1
    assert "old-1" not in store and "fresh" in store


def test_the_oldest_entry_is_evicted_at_the_cap():
    clock = _Clock()
    store = _store(clock, max_entries=3)
    for name in ("a", "b", "c", "d"):
        store[name] = (name, "u")
        clock.now += 1
    assert len(store) == 3
    assert store.pop("a", None) is None
    assert [store.pop(name, None)[0] for name in ("b", "c", "d")] == ["b", "c", "d"]


def test_rewriting_a_state_refreshes_its_age_and_position():
    clock = _Clock()
    store = _store(clock, max_entries=2)
    store["a"] = ("a1", "u")
    clock.now += 1
    store["b"] = ("b", "u")
    clock.now += 1
    store["a"] = ("a2", "u")  # now the newest
    store["c"] = ("c", "u")  # evicts "b", not "a"
    assert store.pop("b", None) is None
    assert store.pop("a", None) == ("a2", "u")


def test_get_authorization_url_stores_into_the_bounded_store():
    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.com/auth", "state-from-google")
    service = auth_module.AuthService(SimpleNamespace())
    with patch.object(auth_module, "build_oauth_flow", return_value=flow):
        service.get_authorization_url("https://hub.example/auth/callback")
    try:
        verifier, uri = auth_module._pkce_store.pop("state-from-google", None)
        assert uri == "https://hub.example/auth/callback" and verifier
    finally:
        auth_module._pkce_store.pop("state-from-google", None)


def test_handle_callback_refuses_an_expired_state_before_contacting_google():
    clock = _Clock()
    store = _store(clock)
    store["late"] = ("verifier", "https://hub.example/auth/callback")
    clock.now += 601
    service = auth_module.AuthService(SimpleNamespace())
    with patch.object(auth_module, "_pkce_store", store), patch.object(
        auth_module, "build_oauth_flow", side_effect=AssertionError("Google must not be contacted")
    ):
        with pytest.raises(GoogleOAuthError):
            asyncio.run(service.handle_callback("code", "late", db=None))
