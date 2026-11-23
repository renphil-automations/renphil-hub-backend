"""X-Forwarded-For is honoured only from configured trusted proxies.

``TRUSTED_PROXY_CIDRS`` (comma-separated, default empty). Empty keeps the
pre-existing behaviour exactly: the right-most XFF entry, from any peer.
"""

from __future__ import annotations

import ipaddress
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import mcp_oauth_rate_limit as rate_limit

TRUSTED = tuple(ipaddress.ip_network(c) for c in ("10.0.0.0/8", "2001:db8:ffff::/48"))


def _network(forwarded_for, peer_host, trusted=TRUSTED):
    return rate_limit.client_network(forwarded_for=forwarded_for, peer_host=peer_host, trusted_proxies=trusted)


# ── Settings ────────────────────────────────────────────────────────────


def test_setting_defaults_to_empty():
    assert Settings().TRUSTED_PROXY_CIDRS == ""
    assert Settings().trusted_proxy_networks == ()


def test_setting_parses_a_csv_of_ipv4_and_ipv6_cidrs_and_bare_addresses():
    settings = Settings(TRUSTED_PROXY_CIDRS=" 10.0.0.0/8, 2001:db8::/32 ,192.0.2.7,")
    assert settings.trusted_proxy_networks == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("2001:db8::/32"),
        ipaddress.ip_network("192.0.2.7/32"),
    )


@pytest.mark.parametrize("bad", ["10.0.0.0/33", "not-a-cidr", "10.0.0.0/8, nope", "300.1.1.1"])
def test_an_invalid_cidr_is_a_settings_validation_error(bad):
    with pytest.raises(ValidationError):
        Settings(TRUSTED_PROXY_CIDRS=bad)


# ── Unconfigured: today's behaviour, unchanged ──────────────────────────


def test_empty_trust_list_keeps_the_right_most_entry_from_any_peer():
    assert _network("198.51.100.9, 203.0.113.77", "192.0.2.1", trusted=()) == "203.0.113.77"
    assert _network(None, "192.0.2.1", trusted=()) == "192.0.2.1"
    assert _network("not-an-ip", "192.0.2.1", trusted=()) == "invalid"


def test_without_an_explicit_list_the_setting_is_read(monkeypatch):
    monkeypatch.setattr(
        rate_limit, "_configured_trusted_proxies", lambda: (ipaddress.ip_network("10.0.0.0/8"),)
    )
    # Untrusted peer: its own address, never the header.
    assert rate_limit.client_network(forwarded_for="203.0.113.77", peer_host="192.0.2.1") == "192.0.2.1"
    monkeypatch.setattr(rate_limit, "_configured_trusted_proxies", lambda: ())
    assert rate_limit.client_network(forwarded_for="203.0.113.77", peer_host="192.0.2.1") == "203.0.113.77"


# ── Configured ──────────────────────────────────────────────────────────


def test_an_untrusted_peer_cannot_choose_its_bucket_with_xff():
    assert _network("203.0.113.77", "192.0.2.1") == "192.0.2.1"
    assert _network("10.1.1.1", "192.0.2.1") == "192.0.2.1"


def test_a_trusted_peer_yields_the_right_most_untrusted_hop():
    assert _network("198.51.100.9, 203.0.113.77", "10.0.0.5") == "203.0.113.77"


def test_trusted_hops_are_skipped_right_to_left():
    # client -> 203.0.113.77 -> proxy 10.2.2.2 -> proxy 10.0.0.5 (peer)
    assert _network("6.6.6.6, 203.0.113.77, 10.2.2.2", "10.0.0.5") == "203.0.113.77"


def test_a_spoofed_left_entry_is_ignored():
    assert _network("10.9.9.9, 203.0.113.77", "10.0.0.5") == "203.0.113.77"


def test_trusted_peer_without_xff_uses_the_peer():
    assert _network(None, "10.0.0.5") == "10.0.0.5"


def test_all_hops_trusted_uses_the_left_most():
    assert _network("10.3.3.3, 10.2.2.2", "10.0.0.5") == "10.3.3.3"


def test_an_unparseable_hop_is_the_shared_invalid_bucket():
    assert _network("203.0.113.77, garbage", "10.0.0.5") == "invalid"


def test_ipv6_and_ipv4_mapped_peers_are_matched():
    assert _network("203.0.113.77", "2001:db8:ffff:1::1") == "203.0.113.77"
    assert _network("203.0.113.77", "::ffff:10.0.0.5") == "203.0.113.77"
    assert _network("2001:db8:1:2:aaaa::1", "10.0.0.5") == "2001:db8:1:2::/64"


def test_a_missing_peer_never_trusts_the_header():
    assert _network("203.0.113.77", None) == "invalid"


# ── Startup warning ─────────────────────────────────────────────────────


def _settings(**overrides):
    base = dict(TRUSTED_PROXY_CIDRS="", trusted_proxy_networks=(), is_local_or_debug=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_unset_outside_local_logs_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=rate_limit.logger.name):
        rate_limit.warn_if_forwarded_for_untrusted(_settings())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "TRUSTED_PROXY_CIDRS" in warnings[0].getMessage()


def test_the_warning_runs_at_application_startup():
    import asyncio
    from unittest.mock import AsyncMock, patch

    import app.main as main_module

    async def start_and_stop():
        async with main_module.lifespan(main_module.app):
            pass

    with patch.object(main_module, "init_http_client", AsyncMock()), patch.object(
        main_module, "close_http_client", AsyncMock()
    ), patch.object(rate_limit, "warn_if_forwarded_for_untrusted") as warn:
        asyncio.run(start_and_stop())
    warn.assert_called_once()


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_local_or_debug": True},
        {"TRUSTED_PROXY_CIDRS": "10.0.0.0/8", "trusted_proxy_networks": TRUSTED},
    ],
)
def test_no_warning_locally_or_when_configured(caplog, overrides):
    with caplog.at_level(logging.WARNING, logger=rate_limit.logger.name):
        rate_limit.warn_if_forwarded_for_untrusted(_settings(**overrides))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
