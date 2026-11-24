"""Opt-in hardening for external MCP OAuth clients (Claude first).

Everything here is off by default; an empty setting changes nothing.

* Provider source networks (``MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON``): a
  provider listed there may only use ``/token`` (code exchange and refresh)
  from inside its CIDRs. The caller address is resolved strictly: the direct
  peer, or, only when that peer is a ``TRUSTED_PROXY_CIDRS`` proxy, the
  right-most ``X-Forwarded-For`` hop that is not itself a trusted proxy. A
  malformed hop or an all-trusted chain yields no address (deny). The CIDR
  rules match the MCP server's ``network.py``: strict parsing (host bits set
  is an error) and nothing broader than /16 (IPv4) or /32 (IPv6).
* Per-provider token lifetimes (``MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON``),
  only ever shorter than the global ones.
* One active refresh family per (user, client)
  (``MCP_OAUTH_SINGLE_FAMILY_PROVIDERS``).
* Daily tool-call anomaly limits (``MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON``).
* Structured audit log lines. They never contain a token, a code, an email or
  an IP address: users and refresh families appear only as keyed HMAC
  digests under a key derived from the server-only ``JWT_SECRET_KEY``.

Parsers raise ``ValueError`` (used by settings validation, so an invalid value
refuses to start). The runtime accessors never raise: an invalid value there
fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.services.mcp_access_policy import EXTERNAL_PROVIDERS

logger = logging.getLogger("app.services.mcp_oauth_audit")

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Same floor as renphil_hub_mcp.network (MIN_IPV4_PREFIX / MIN_IPV6_PREFIX):
# real provider egress ranges are far narrower (Anthropic /20-/21).
MIN_IPV4_PREFIX = 16
MIN_IPV6_PREFIX = 32

# Hard ceiling for a per-provider access-token lifetime (seconds).
MAX_PROVIDER_ACCESS_TOKEN_TTL_SECONDS = 600
_POLICY_FIELDS = ("access_token_ttl_seconds", "refresh_idle_seconds", "refresh_family_max_seconds")
_POLICY_MINIMUMS = {"access_token_ttl_seconds": 60, "refresh_idle_seconds": 300, "refresh_family_max_seconds": 3600}

# Access-token ``sid`` claim and the header the Agent forwards it in.
SID_CLAIM = "sid"
SID_HEADER = "X-RenPhil-MCP-Sid"
_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

_AUDIT_KEY_LABEL = b"renphil-hub-mcp-oauth-audit-v1"


class McpOAuthHardeningConfigError(RuntimeError):
    """Invalid hardening configuration (raised at settings load).

    Deliberately not a ``ValueError`` when raised from a model validator, so
    pydantic never renders the whole settings object (and its secrets).
    """


# ── Parsing (settings validation) ─────────────────────────────────────────


def _json_object(raw: object, setting: str) -> dict:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if not isinstance(raw, str):
        raise ValueError(f"{setting} must be a JSON object.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{setting} is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{setting} must be a JSON object.")
    for key in data:
        if key not in EXTERNAL_PROVIDERS:
            raise ValueError(f"{setting} may only name: " + ", ".join(EXTERNAL_PROVIDERS) + ".")
    return data


def is_too_broad(network: IPNetwork) -> bool:
    minimum = MIN_IPV4_PREFIX if network.version == 4 else MIN_IPV6_PREFIX
    return network.prefixlen < minimum


def parse_provider_source_cidrs(raw: object) -> dict[str, tuple[IPNetwork, ...]]:
    """``{"claude": ["160.79.104.0/21", ...]}`` -> parsed networks.

    Each listed provider needs a non-empty list. Parsing is strict (host bits
    set is an error, never silently widened) and ranges broader than /16
    (IPv4) or /32 (IPv6) are refused, as in the MCP server.
    """
    setting = "MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON"
    parsed: dict[str, tuple[IPNetwork, ...]] = {}
    for provider, values in _json_object(raw, setting).items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"{setting}: {provider} needs a non-empty list of CIDRs.")
        networks: list[IPNetwork] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{setting}: {provider} contains an invalid IP network.")
            try:
                network = ipaddress.ip_network(value.strip(), strict=True)
            except ValueError as exc:
                raise ValueError(f"{setting}: {provider} contains an invalid IP network: {value.strip()!r}.") from exc
            if is_too_broad(network):
                raise ValueError(
                    f"{setting}: {provider} range {value.strip()!r} is too broad "
                    f"(at most /{MIN_IPV4_PREFIX} IPv4 or /{MIN_IPV6_PREFIX} IPv6)."
                )
            networks.append(network)
        parsed[provider] = tuple(networks)
    return parsed


def parse_provider_set(raw: object, setting: str) -> frozenset[str]:
    """Comma-separated subset of claude,chatgpt,gemini; unknown names raise."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, str):
        raise ValueError(f"{setting} must be a comma-separated string.")
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if any(name not in EXTERNAL_PROVIDERS for name in names):
        raise ValueError(f"{setting} may only contain: " + ", ".join(EXTERNAL_PROVIDERS) + ".")
    return frozenset(names)


@dataclass(frozen=True)
class TokenLifetimes:
    """Effective lifetimes for one issuance (seconds)."""

    access_token_ttl_seconds: int
    refresh_idle_seconds: int
    refresh_family_max_seconds: int


def parse_provider_token_policy(raw: object) -> dict[str, dict[str, int]]:
    """``{"claude": {"access_token_ttl_seconds": 300, ...}}``; shape only.

    Upper bounds against the global settings are checked separately
    (``check_provider_token_policy``), because they depend on other settings.
    """
    setting = "MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON"
    parsed: dict[str, dict[str, int]] = {}
    for provider, policy in _json_object(raw, setting).items():
        if not isinstance(policy, dict):
            raise ValueError(f"{setting}: {provider} must be an object.")
        entry: dict[str, int] = {}
        for field, value in policy.items():
            if field not in _POLICY_FIELDS:
                raise ValueError(f"{setting}: {provider} has an unknown field {field!r}.")
            if not isinstance(value, int) or isinstance(value, bool) or value < _POLICY_MINIMUMS[field]:
                raise ValueError(f"{setting}: {provider}.{field} must be an integer >= {_POLICY_MINIMUMS[field]}.")
            entry[field] = value
        if entry.get("access_token_ttl_seconds", 0) > MAX_PROVIDER_ACCESS_TOKEN_TTL_SECONDS:
            raise ValueError(f"{setting}: {provider}.access_token_ttl_seconds may be at most {MAX_PROVIDER_ACCESS_TOKEN_TTL_SECONDS}.")
        parsed[provider] = entry
    return parsed


def check_provider_token_policy(policy: dict[str, dict[str, int]], *, access_ttl: int, idle: int, family: int) -> None:
    """Every per-provider value must be <= the global one (never longer)."""
    globals_ = {"access_token_ttl_seconds": access_ttl, "refresh_idle_seconds": idle, "refresh_family_max_seconds": family}
    for provider, entry in policy.items():
        for field, value in entry.items():
            if value > globals_[field]:
                raise McpOAuthHardeningConfigError(
                    f"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON: {provider}.{field} may not exceed the global value."
                )


def parse_anomaly_limits(raw: object) -> dict[str, int]:
    """``{"claude": 2000}``: max tool calls per user per UTC day."""
    setting = "MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON"
    parsed: dict[str, int] = {}
    for provider, value in _json_object(raw, setting).items():
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10_000_000:
            raise ValueError(f"{setting}: {provider} must be an integer between 1 and 10000000.")
        parsed[provider] = value
    return parsed


# ── Runtime accessors (never raise; invalid fails closed) ─────────────────


class _Invalid:
    """Marker: the configured value is invalid; deny."""


INVALID = _Invalid()


def provider_source_networks(settings: Any) -> dict[str, tuple[IPNetwork, ...]] | _Invalid:
    try:
        return parse_provider_source_cidrs(getattr(settings, "MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON", "") or "")
    except ValueError:
        return INVALID


def single_family_providers(settings: Any) -> frozenset[str]:
    try:
        return parse_provider_set(getattr(settings, "MCP_OAUTH_SINGLE_FAMILY_PROVIDERS", "") or "", "MCP_OAUTH_SINGLE_FAMILY_PROVIDERS")
    except ValueError:
        # Fail closed toward fewer live sessions: treat every provider as single-family.
        return frozenset(EXTERNAL_PROVIDERS)


def anomaly_limit(settings: Any, provider: str | None) -> int | None | _Invalid:
    try:
        limits = parse_anomaly_limits(getattr(settings, "MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON", "") or "")
    except ValueError:
        return INVALID
    return limits.get(provider) if provider else None


def token_lifetimes(settings: Any, provider: str | None, *, access_ttl: int, idle: int, family: int) -> TokenLifetimes:
    """Effective lifetimes for ``provider``: the global ones (as already
    clamped by the service), shortened by that provider's policy. An invalid
    policy (start-up validation normally refuses it) uses the shortest safe
    values."""
    base = TokenLifetimes(access_ttl, idle, family)
    try:
        policy = parse_provider_token_policy(getattr(settings, "MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON", "") or "")
    except ValueError:
        return TokenLifetimes(min(access_ttl, 60), min(idle, 300), min(family, 3600))
    entry = policy.get(provider or "", {})
    if not entry:
        return base
    family_value = min(family, entry.get("refresh_family_max_seconds", family))
    return TokenLifetimes(
        access_token_ttl_seconds=min(access_ttl, entry.get("access_token_ttl_seconds", access_ttl)),
        refresh_idle_seconds=min(idle, entry.get("refresh_idle_seconds", idle), family_value),
        refresh_family_max_seconds=family_value,
    )


def session_live_check_enabled(settings: Any) -> bool:
    return getattr(settings, "MCP_OAUTH_SESSION_LIVE_CHECK_ENABLED", False) is True


def is_valid_sid(value: object) -> bool:
    return isinstance(value, str) and _SID_RE.fullmatch(value) is not None


# ── Caller address (strict trusted-proxy rule) ────────────────────────────


def _address(value: object) -> IPAddress | None:
    try:
        text = str(value or "").strip()
        if text.startswith("[") and "]" in text:  # [IPv6]:port
            text = text[1:text.index("]")]
        elif text.count(":") == 1:  # IPv4:port
            host, _, port = text.partition(":")
            if not port.isascii() or not port.isdigit():
                return None
            text = host
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _inside(address: IPAddress | None, networks) -> bool:
    return address is not None and any(address.version == network.version and address in network for network in networks)


def strict_client_address(*, forwarded_for: str | None, peer_host: str | None, trusted_proxies) -> IPAddress | None:
    """Caller address, or ``None`` when it cannot be determined safely.

    Unlike the rate limiter's bucketing rule, ``X-Forwarded-For`` is never
    used unless the direct peer is a configured trusted proxy.
    """
    trusted = tuple(trusted_proxies or ())
    peer = _address(peer_host)
    if peer is None or not trusted or not _inside(peer, trusted):
        return peer
    hops = [hop.strip() for hop in str(forwarded_for or "").split(",") if hop.strip()]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            return None  # a malformed hop: refuse to guess
        if not _inside(address, trusted):
            return address
    return None  # only trusted hops: the real client is unknown


def trusted_proxies(settings: Any) -> tuple:
    networks = getattr(settings, "trusted_proxy_networks", None)
    if networks is not None:
        return tuple(networks)
    raw = getattr(settings, "TRUSTED_PROXY_CIDRS", "") or ""
    try:
        from app.config import _parse_cidrs

        return _parse_cidrs(raw)
    except Exception:
        return ()


def address_in(address: IPAddress | None, networks) -> bool:
    return _inside(address, networks)


def in_any_provider_network(settings: Any, address: IPAddress | None) -> bool:
    networks = provider_source_networks(settings)
    if isinstance(networks, _Invalid):
        return False
    return any(_inside(address, ranges) for ranges in networks.values())


# ── Audit logging ─────────────────────────────────────────────────────────


def _audit_key() -> bytes | None:
    try:
        from app.config import get_settings

        secret = str(getattr(get_settings(), "JWT_SECRET_KEY", "") or "")
    except Exception:
        return None
    if not secret:
        return None
    return hmac.new(secret.encode("utf-8"), _AUDIT_KEY_LABEL, hashlib.sha256).digest()


def audit_digest(label: str, value: object) -> str:
    """Keyed, truncated digest of ``value`` for log correlation only."""
    if value is None or value == "":
        return "-"
    key = _audit_key()
    if key is None:
        return "unavailable"
    return hmac.new(key, f"{label}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]


_SAFE_VALUE = re.compile(r"[^A-Za-z0-9._~:/@+=-]")


def _safe(value: object) -> str:
    text = str(value if value is not None else "-")
    return _SAFE_VALUE.sub("_", text)[:128] or "-"


def audit(event: str, *, level: int = logging.INFO, category: str | None = None, **fields: object) -> None:
    """One structured line: ``MCP OAuth audit: event=... key=value ...``.

    Callers pass only safe fields (grant type, provider, reason) and
    pre-hashed identifiers (``user``, ``family``). Client IDs are never
    logged verbatim (same privacy rule as the rate limiter): a ``*client_id``
    field is written as a keyed digest under ``*client``. Values are
    sanitized so they cannot break the line format.
    """
    parts = [f"event={_safe(event)}"]
    if category:
        parts.append(f"category={_safe(category)}")
    for name, value in fields.items():
        if name.endswith("client_id"):
            name, value = name[: -len("_id")], audit_digest("client", value)
        parts.append(f"{name}={_safe(value)}")
    try:
        logger.log(level, "MCP OAuth audit: %s", " ".join(parts))
    except Exception:  # logging must never break a token response
        pass


def user_ref(subject: object) -> str:
    return audit_digest("user", subject)


def family_ref(family_id: object) -> str:
    return audit_digest("family", family_id)


def audit_family_revoked(*, family_id: str, reason: str, client_id: str | None = None, subject: object = None, rows: int | None = None) -> None:
    level = logging.WARNING if reason in {"reuse", "anomaly"} else logging.INFO
    fields: dict[str, object] = {"reason": reason, "client_id": client_id, "family": family_ref(family_id), "user": user_ref(subject)}
    if rows is not None:
        fields["rows"] = rows
    audit("family_revoked", level=level, **fields)


def notify_user_of_anomaly(*, provider: str, subject_digest: str, family_digest: str) -> None:
    """TODO(mcp-anomaly-notify): tell the user (and/or admins) that a
    connector session was revoked for unusual volume. Deliberately a no-op:
    no email or message is sent until a channel and wording are approved."""
    return None


__all__ = [
    "INVALID",
    "MAX_PROVIDER_ACCESS_TOKEN_TTL_SECONDS",
    "McpOAuthHardeningConfigError",
    "SID_CLAIM",
    "SID_HEADER",
    "TokenLifetimes",
    "address_in",
    "anomaly_limit",
    "audit",
    "audit_digest",
    "audit_family_revoked",
    "check_provider_token_policy",
    "family_ref",
    "in_any_provider_network",
    "is_valid_sid",
    "notify_user_of_anomaly",
    "parse_anomaly_limits",
    "parse_provider_set",
    "parse_provider_source_cidrs",
    "parse_provider_token_policy",
    "provider_source_networks",
    "session_live_check_enabled",
    "single_family_providers",
    "strict_client_address",
    "token_lifetimes",
    "trusted_proxies",
    "user_ref",
]
