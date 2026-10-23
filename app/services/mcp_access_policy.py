"""Single source of truth for which MCP callers Hub admits.

Only four callers may ever use the RenPhil MCP:

* the RenPhil Hub Agent (``MCP_OAUTH_AGENT_CLIENT_ID``), controlled by the
  Hub AI switch and the Hub Agent tool mode, never by external toggles;
* Claude, ChatGPT and Gemini, each through exactly one client registered in
  ``MCP_OAUTH_CLIENTS_JSON`` and mapped to its provider in
  ``MCP_OAUTH_CLIENT_PROVIDERS_JSON``, and only while Bot Management's master
  external-MCP toggle and that provider's toggle are both on.

Everything else is denied. Client IDs are compared byte-for-byte (no case
folding, trimming, Unicode normalization, or prefix matching). The same
decision is applied at OAuth ``/authorize`` and ``/token`` and again on every
MCP capability call (``/data/agent/access/verified-granted-nodes``), so a
toggle change takes effect on the next tool call.

Readiness is reported separately and never grants access by itself:
``disabled`` -> ``needs_client_mapping`` -> ``blocked_pending_dpop_verification``
-> ``ready_for_staging_qa`` -> ``operational``. DPoP is enforced for every
external call regardless of readiness; the readiness label records what staging
and deployment QA have proven.
"""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

EXTERNAL_PROVIDERS: tuple[str, ...] = ("claude", "chatgpt", "gemini")
PROVIDER_LABELS = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}
HUB_AGENT_CLIENT_KIND = "hub_agent"
CLIENT_KIND_CLAIM = "renphil_client_kind"

VerificationState = Literal["unverified", "dpop_staging_verified", "deployment_verified"]
VERIFICATION_STATES: tuple[str, ...] = ("unverified", "dpop_staging_verified", "deployment_verified")
ReadinessState = Literal[
    "disabled",
    "needs_client_mapping",
    "blocked_pending_dpop_verification",
    "ready_for_staging_qa",
    "operational",
]
READINESS_LABELS = {
    "disabled": "Disabled",
    "needs_client_mapping": "Needs approved client mapping",
    "blocked_pending_dpop_verification": "Blocked pending DPoP-compatible staging verification",
    "ready_for_staging_qa": "Ready for staging QA",
    "operational": "Operational",
}

# Persisted Bot Management defaults. External providers default to disabled and
# unverified; the Hub Agent defaults to the existing local tool path.
DEFAULT_MCP_SETTINGS: dict[str, Any] = {
    "hub_ai_tool_mode": "local",
    "mcp_external_connections_enabled": False,
    **{f"mcp_{provider}_enabled": False for provider in EXTERNAL_PROVIDERS},
    **{f"mcp_{provider}_verification": "unverified" for provider in EXTERNAL_PROVIDERS},
}

# Printable ASCII without whitespace. Approved client IDs may be opaque
# strings or HTTPS client-metadata URLs; both are compared exactly.
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,512}")


class McpClientConfigurationError(ValueError):
    """The approved client configuration is invalid; nothing is admitted."""


def is_wellformed_client_id(client_id: object) -> bool:
    return isinstance(client_id, str) and bool(_CLIENT_ID_RE.fullmatch(client_id))


def _exact(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True)
class ApprovedClients:
    """Validated view of the deployment's client allowlist."""

    agent_client_id: str | None
    provider_by_client: dict[str, str]
    client_by_provider: dict[str, str]
    oauth_enabled: bool
    problem: str | None = None

    def provider_for(self, client_id: object) -> str | None:
        if not is_wellformed_client_id(client_id):
            return None
        for registered, provider in self.provider_by_client.items():
            if _exact(registered, str(client_id)):
                return provider
        return None

    def is_agent_client(self, client_id: object) -> bool:
        return bool(
            self.agent_client_id
            and is_wellformed_client_id(client_id)
            and _exact(self.agent_client_id, str(client_id))
        )

    def mapped(self, provider: str) -> bool:
        return self.oauth_enabled and self.problem is None and provider in self.client_by_provider


def load_approved_clients(settings: Any) -> ApprovedClients:
    """Parse the existing OAuth client registry and provider mapping strictly.

    Invalid configuration never widens access: it yields no external clients
    and a safe ``problem`` description for the readiness view.
    """
    oauth_enabled = bool(getattr(settings, "MCP_OAUTH_ENABLED", False))
    agent_client_id = str(getattr(settings, "MCP_OAUTH_AGENT_CLIENT_ID", "") or "")
    if agent_client_id and not is_wellformed_client_id(agent_client_id):
        return ApprovedClients(None, {}, {}, oauth_enabled, "The Hub Agent client ID is invalid.")
    try:
        registry = json.loads(getattr(settings, "MCP_OAUTH_CLIENTS_JSON", "") or "{}")
        mapping = json.loads(getattr(settings, "MCP_OAUTH_CLIENT_PROVIDERS_JSON", "") or "{}")
    except json.JSONDecodeError:
        return ApprovedClients(agent_client_id or None, {}, {}, oauth_enabled, "The approved client configuration is not valid JSON.")
    if not isinstance(registry, dict) or not isinstance(mapping, dict):
        return ApprovedClients(agent_client_id or None, {}, {}, oauth_enabled, "The approved client configuration is invalid.")

    provider_by_client: dict[str, str] = {}
    client_by_provider: dict[str, str] = {}
    for client_id, provider in mapping.items():
        if not is_wellformed_client_id(client_id):
            return ApprovedClients(agent_client_id or None, {}, {}, oauth_enabled, "A mapped client ID is malformed.")
        if not isinstance(provider, str) or provider not in EXTERNAL_PROVIDERS:
            return ApprovedClients(agent_client_id or None, {}, {}, oauth_enabled, "A client is mapped to an unsupported provider.")
        if agent_client_id and _exact(client_id, agent_client_id):
            return ApprovedClients(agent_client_id or None, {}, {}, oauth_enabled, "The Hub Agent client cannot be mapped to an external provider.")
        if provider in client_by_provider:
            return ApprovedClients(agent_client_id or None, {}, {}, oauth_enabled, "Each provider may have exactly one approved client.")
        entry = registry.get(client_id)
        uris = entry.get("redirect_uris") if isinstance(entry, dict) else None
        if not isinstance(uris, list) or not uris or not all(isinstance(uri, str) and _exact_https_or_loopback(uri) for uri in uris):
            # Mapped but not registered with exact callbacks: not admissible.
            continue
        provider_by_client[client_id] = provider
        client_by_provider[provider] = client_id
    return ApprovedClients(agent_client_id or None, provider_by_client, client_by_provider, oauth_enabled)


def _exact_https_or_loopback(uri: str) -> bool:
    parsed = urlparse(uri)
    if "*" in uri:
        return False
    return bool(parsed.scheme == "https" and parsed.netloc) or (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    )


def normalized_mcp_settings(active: dict | None) -> dict[str, Any]:
    values = dict(DEFAULT_MCP_SETTINGS)
    if isinstance(active, dict):
        for key in DEFAULT_MCP_SETTINGS:
            if key in active:
                values[key] = active[key]
    if values["hub_ai_tool_mode"] not in ("mcp", "local"):
        values["hub_ai_tool_mode"] = "local"
    for provider in EXTERNAL_PROVIDERS:
        if values[f"mcp_{provider}_verification"] not in VERIFICATION_STATES:
            values[f"mcp_{provider}_verification"] = "unverified"
        values[f"mcp_{provider}_enabled"] = values[f"mcp_{provider}_enabled"] is True
    values["mcp_external_connections_enabled"] = values["mcp_external_connections_enabled"] is True
    return values


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    client_kind: str | None
    reason: str


def admit_client(*, active_settings: dict | None, client_id: object, approved: ApprovedClients) -> AdmissionDecision:
    """Decide whether ``client_id`` may use MCP right now. Deny by default."""
    settings = active_settings if isinstance(active_settings, dict) else {}
    if approved.is_agent_client(client_id):
        # The Hub Agent is governed by the Hub AI switch, not external toggles.
        if settings.get("bot_enabled", True) is False:
            return AdmissionDecision(False, HUB_AGENT_CLIENT_KIND, "hub_ai_disabled")
        return AdmissionDecision(True, HUB_AGENT_CLIENT_KIND, "hub_agent")
    if not approved.oauth_enabled or approved.problem is not None:
        return AdmissionDecision(False, None, "not_configured")
    provider = approved.provider_for(client_id)
    if provider is None:
        return AdmissionDecision(False, None, "unknown_client")
    values = normalized_mcp_settings(settings)
    if not values["mcp_external_connections_enabled"]:
        return AdmissionDecision(False, provider, "external_disabled")
    if not values[f"mcp_{provider}_enabled"]:
        return AdmissionDecision(False, provider, "provider_disabled")
    return AdmissionDecision(True, provider, "provider_enabled")


def provider_readiness(*, active_settings: dict | None, approved: ApprovedClients) -> list[dict[str, Any]]:
    """Admin-safe readiness rows. Never contains client IDs or callback URLs."""
    values = normalized_mcp_settings(active_settings)
    rows: list[dict[str, Any]] = []
    for provider in EXTERNAL_PROVIDERS:
        enabled = values["mcp_external_connections_enabled"] and values[f"mcp_{provider}_enabled"]
        mapped = approved.mapped(provider)
        verification = values[f"mcp_{provider}_verification"]
        if not enabled:
            state: str = "disabled"
        elif not mapped:
            state = "needs_client_mapping"
        elif verification == "deployment_verified":
            state = "operational"
        elif verification == "dpop_staging_verified":
            state = "ready_for_staging_qa"
        else:
            state = "blocked_pending_dpop_verification"
        rows.append({
            "provider": provider,
            "label": PROVIDER_LABELS[provider],
            "enabled": values[f"mcp_{provider}_enabled"],
            "client_mapping_configured": mapped,
            "verification": verification,
            "readiness": state,
            "readiness_label": READINESS_LABELS[state],
        })
    return rows


__all__ = [
    "AdmissionDecision",
    "ApprovedClients",
    "CLIENT_KIND_CLAIM",
    "DEFAULT_MCP_SETTINGS",
    "EXTERNAL_PROVIDERS",
    "HUB_AGENT_CLIENT_KIND",
    "McpClientConfigurationError",
    "READINESS_LABELS",
    "VERIFICATION_STATES",
    "admit_client",
    "is_wellformed_client_id",
    "load_approved_clients",
    "normalized_mcp_settings",
    "provider_readiness",
]
