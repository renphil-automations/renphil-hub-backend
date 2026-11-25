"""External MCP client hardening (Claude restricted bearer profile).

Covers: provider source networks at /token, audit logging (refresh reuse
WARNING), the ``sid`` claim and the live session check on the Agent's
verified live-ACL endpoint, the admin per-user revoke, one family per
(user, client), per-provider lifetimes, the daily anomaly guard and refresh
rate-limit fairness. Everything is off by default.
"""

from __future__ import annotations

import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.test_mcp_oauth_refresh_tokens as refresh_tests
from app.db_v2.database import BaseV2, get_db_v2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.db_v2.models.mcp_oauth import McpOAuthRefreshTokenV2
from app.routers import agent_access, bot_management
from app.routers import mcp_oauth as mcp_oauth_router
from app.services import mcp_oauth_hardening as hardening
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.agent_identity import AgentIdentity, require_agent_identity
from app.dependencies import get_current_hub_user
from app.models.auth import UserInfo
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ENABLED_EXTERNAL, FakeUpstash, JWT_SECRET, REDIRECT, mcp_settings

AUDIT_LOGGER = "app.services.mcp_oauth_audit"
ANTHROPIC = ["160.79.104.0/21", "153.61.208.0/20"]
INSIDE_IP = "160.79.104.10"
OUTSIDE_IP = "198.51.100.23"


class _Harness(refresh_tests.RefreshEnabledTests):
    """The refresh-enabled flow, with a chosen TCP peer address."""

    peer = "203.0.113.77"

    def client(self) -> TestClient:
        return TestClient(self.app(), follow_redirects=False, client=(self.peer, 50000))

    def configure(self, **overrides) -> None:
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, **overrides)


for _name in [name for name in dir(refresh_tests.RefreshEnabledTests) if name.startswith("test_")]:
    setattr(_Harness, _name, None)


# ── Item 1: provider source networks ─────────────────────────────────────


class ProviderSourceNetworkTests(_Harness):
    def setUp(self) -> None:
        super().setUp()
        self.configure(MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON=json.dumps({"claude": ANTHROPIC}))

    def _exchange(self, http, code, headers=None):
        return http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": self.signer.proof(), **(headers or {})})

    def test_inside_the_provider_ranges_both_grants_succeed(self):
        self.peer = INSIDE_IP
        http = self.client()
        token = self._login(http)["refresh_token"]
        self.assertEqual(self._refresh(http, token).status_code, 200)

    def test_outside_the_ranges_both_grants_are_invalid_client_and_the_code_survives(self):
        self.peer = OUTSIDE_IP
        http = self.client()
        code = self.issue_code(http)
        with self.assertLogs(AUDIT_LOGGER, level="WARNING") as logs:
            denied = self._exchange(http, code)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json(), {"error": "invalid_client", "error_description": "Client authentication failed."})
        joined = "\n".join(logs.output)
        self.assertIn("event=source_network_denied", joined)
        self.assertIn("category=source_network", joined)
        self.assertIn("reason=outside_provider_networks", joined)
        self.assertNotIn(OUTSIDE_IP, joined)
        # The code was not consumed: the same code works from inside.
        self.peer = INSIDE_IP
        inside = self.client()
        issued = self._exchange(inside, code)
        self.assertEqual(issued.status_code, 200, issued.text)
        # A refresh from outside is refused too and consumes nothing.
        self.peer = OUTSIDE_IP
        refused = self._refresh(self.client(), issued.json()["refresh_token"])
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json()["error"], "invalid_client")
        self.assertIsNone(self._rows()[0].used_at)

    def test_forwarded_for_is_only_honoured_from_a_trusted_proxy(self):
        # An untrusted peer cannot claim an Anthropic address.
        self.peer = OUTSIDE_IP
        http = self.client()
        self.assertEqual(self._exchange(http, self.issue_code(http), {"x-forwarded-for": INSIDE_IP}).status_code, 401)
        # Behind a configured proxy the right-most untrusted hop counts.
        self.configure(MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON=json.dumps({"claude": ANTHROPIC}), TRUSTED_PROXY_CIDRS="10.0.0.0/24")
        self.peer = "10.0.0.5"
        proxied = self.client()
        self.assertEqual(self._exchange(proxied, self.issue_code(proxied), {"x-forwarded-for": f"{OUTSIDE_IP}, {INSIDE_IP}"}).status_code, 200)
        self.assertEqual(self._exchange(proxied, self.issue_code(proxied), {"x-forwarded-for": f"{INSIDE_IP}, {OUTSIDE_IP}"}).status_code, 401)
        self.assertEqual(self._exchange(proxied, self.issue_code(proxied), {"x-forwarded-for": "not-an-ip"}).status_code, 401)

    def test_unlisted_providers_and_the_default_are_unaffected(self):
        self.configure(MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON=json.dumps({"chatgpt": ["203.0.113.0/24"]}))
        self.peer = OUTSIDE_IP
        self._login(self.client())
        self.configure()
        self._login(self.client())


class StrictClientAddressTests(unittest.TestCase):
    def test_strict_resolution(self):
        import ipaddress

        proxies = (ipaddress.ip_network("10.0.0.0/24"),)
        resolve = hardening.strict_client_address
        self.assertEqual(str(resolve(forwarded_for="1.2.3.4", peer_host="5.6.7.8", trusted_proxies=())), "5.6.7.8")
        self.assertEqual(str(resolve(forwarded_for="1.2.3.4", peer_host="5.6.7.8", trusted_proxies=proxies)), "5.6.7.8")
        self.assertEqual(str(resolve(forwarded_for="9.9.9.9, 1.2.3.4, 10.0.0.7", peer_host="10.0.0.5", trusted_proxies=proxies)), "1.2.3.4")
        self.assertIsNone(resolve(forwarded_for="10.0.0.9", peer_host="10.0.0.5", trusted_proxies=proxies))
        self.assertIsNone(resolve(forwarded_for="garbage", peer_host="10.0.0.5", trusted_proxies=proxies))
        self.assertIsNone(resolve(forwarded_for=None, peer_host="testclient", trusted_proxies=()))


# ── Settings validation (refuse to start) ─────────────────────────────────


class SettingsValidationTests(unittest.TestCase):
    def _settings(self, **values):
        from app.config import Settings

        return Settings(**values)

    def test_defaults_are_off(self):
        settings = self._settings()
        self.assertEqual(settings.MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON, "{}")
        self.assertFalse(settings.MCP_OAUTH_SESSION_LIVE_CHECK_ENABLED)
        self.assertEqual(settings.MCP_OAUTH_SINGLE_FAMILY_PROVIDERS, "")
        self.assertEqual(settings.MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON, "{}")
        self.assertEqual(settings.MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON, "{}")
        self.assertEqual(settings.MCP_OAUTH_PROVIDER_NETWORK_REFRESH_LIMIT_PER_MINUTE, 30)

    def test_valid_values_are_accepted(self):
        settings = self._settings(
            MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON=json.dumps({"claude": ANTHROPIC + ["2600:1f00::/32"]}),
            MCP_OAUTH_SINGLE_FAMILY_PROVIDERS="claude",
            MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON=json.dumps({"claude": {"access_token_ttl_seconds": 120, "refresh_idle_seconds": 3600, "refresh_family_max_seconds": 86400}}),
            MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON=json.dumps({"claude": 2000}),
            MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_SESSION_LIVE_CHECK_ENABLED=True,
        )
        self.assertEqual(settings.MCP_OAUTH_SINGLE_FAMILY_PROVIDERS, "claude")

    def test_invalid_values_refuse_to_start(self):
        from pydantic import ValidationError

        invalid = [
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": json.dumps({"claude": ["0.0.0.0/1"]})},
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": json.dumps({"claude": ["10.0.0.0/8"]})},
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": json.dumps({"claude": ["2600::/16"]})},
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": json.dumps({"claude": ["160.79.104.9/21"]})},  # host bits
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": json.dumps({"claude": []})},
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": json.dumps({"other": ANTHROPIC})},
            {"MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON": "not json"},
            {"MCP_OAUTH_SINGLE_FAMILY_PROVIDERS": "claude,hub_agent"},
            {"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON": json.dumps({"claude": {"access_token_ttl_seconds": 900}})},
            {"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON": json.dumps({"claude": {"unknown": 5}})},
            {"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON": json.dumps({"claude": {"refresh_idle_seconds": 10}})},
            {"MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON": json.dumps({"claude": 0})},
            {"MCP_OAUTH_PROVIDER_NETWORK_REFRESH_LIMIT_PER_MINUTE": 0},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self._settings(**values)

    def test_cross_field_rules_refuse_to_start(self):
        for values in (
            # Longer than the global lifetimes.
            {"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON": json.dumps({"claude": {"access_token_ttl_seconds": 600}}), "MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS": 300},
            {"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON": json.dumps({"claude": {"refresh_idle_seconds": 8 * 24 * 3600}})},
            {"MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON": json.dumps({"claude": {"refresh_family_max_seconds": 31 * 24 * 3600}})},
            # A live session check needs refresh families.
            {"MCP_OAUTH_SESSION_LIVE_CHECK_ENABLED": True},
        ):
            with self.subTest(values=values), self.assertRaises(hardening.McpOAuthHardeningConfigError):
                self._settings(**values)


# ── Item 2: audit logging ────────────────────────────────────────────────


class AuditLoggingTests(_Harness):
    def test_refresh_reuse_logs_a_warning_with_the_category(self):
        http = self.client()
        with self.assertLogs(AUDIT_LOGGER, level="INFO") as logs:
            issued = self._login(http)
            first = issued["refresh_token"]
            second = self._refresh(http, first).json()["refresh_token"]
            self.assertEqual(self._refresh(http, first).status_code, 400)  # replay
        warnings = [record for record in logs.records if record.levelno == logging.WARNING]
        reuse = [record.getMessage() for record in warnings if "event=refresh_reuse_detected" in record.getMessage()]
        self.assertEqual(len(reuse), 1, logs.output)
        self.assertIn("category=refresh_reuse", reuse[0])
        joined = "\n".join(logs.output)
        self.assertIn("event=token_issued grant=authorization_code client_kind=claude", joined)
        self.assertIn("event=refresh_rotated grant=refresh_token client_kind=claude", joined)
        self.assertIn("event=family_revoked reason=reuse", joined)
        # No tokens, emails, raw client or family IDs in the audit lines.
        family_id = self._rows()[0].family_id
        for secret in (first, second, issued["access_token"], refresh_tests.USER_EMAIL, CLAUDE_CLIENT, family_id):
            self.assertNotIn(secret, joined)

    def test_revoke_endpoint_logs_the_reason(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        with self.assertLogs(AUDIT_LOGGER, level="INFO") as logs:
            self.assertEqual(http.post("/mcp/oauth/revoke", data={"token": token, "client_id": CLAUDE_CLIENT}).status_code, 200)
        self.assertIn("event=family_revoked reason=revoke_endpoint", "\n".join(logs.output))


# ── Item 3: sid claim ────────────────────────────────────────────────────


class SidClaimTests(_Harness):
    def test_sid_is_the_family_and_survives_rotation(self):
        http = self.client()
        issued = self._login(http)
        sid = self._claims(issued["access_token"])["sid"]
        self.assertEqual(sid, self._rows()[0].family_id)
        self.assertTrue(hardening.is_valid_sid(sid))
        rotated = self._refresh(http, issued["refresh_token"]).json()
        self.assertEqual(self._claims(rotated["access_token"])["sid"], sid)

    def test_no_sid_without_refresh_tokens_or_for_agent_delegated_tokens(self):
        self.service.refresh_tokens_enabled = False
        self.settings = mcp_settings()
        issued = self._login(self.client())
        self.assertNotIn("sid", self._claims(issued["access_token"]))
        delegated = self.service.issue_agent_delegated_token(email=refresh_tests.USER_EMAIL, subject="42", client_id="renphil-agent")
        self.assertNotIn("sid", self._claims(delegated["access_token"]))


# ── Item 5: one family per (user, client) ────────────────────────────────


class SingleFamilyTests(_Harness):
    def test_a_new_login_supersedes_older_families_when_enabled(self):
        self.configure(MCP_OAUTH_SINGLE_FAMILY_PROVIDERS="claude")
        http = self.client()
        old = self._login(http)["refresh_token"]
        with self.assertLogs(AUDIT_LOGGER, level="INFO") as logs:
            new = self._login(http)["refresh_token"]
        self.assertIn("event=family_revoked reason=superseded", "\n".join(logs.output))
        self.assertEqual(self._refresh(http, old).json()["error"], "invalid_grant")
        self.assertEqual(self._refresh(http, new).status_code, 200)

    def test_default_keeps_parallel_families(self):
        http = self.client()
        first = self._login(http)["refresh_token"]
        second = self._login(http)["refresh_token"]
        self.assertEqual(self._refresh(http, first).status_code, 200)
        self.assertEqual(self._refresh(http, second).status_code, 200)


# ── Item 6: per-provider lifetimes ───────────────────────────────────────


class ProviderLifetimeTests(_Harness):
    def test_policy_shortens_access_idle_and_family_lifetimes(self):
        self.configure(MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON=json.dumps({"claude": {
            "access_token_ttl_seconds": 120, "refresh_idle_seconds": 900, "refresh_family_max_seconds": 7200,
        }}))
        http = self.client()
        issued = self._login(http)
        self.assertEqual(issued["expires_in"], 120)
        claims = self._claims(issued["access_token"])
        self.assertEqual(claims["exp"] - claims["iat"], 120)
        row = self._rows()[0]
        self.assertEqual(round((row.expires_at - row.created_at).total_seconds()), 900)
        self.assertEqual(round((row.family_expires_at - row.created_at).total_seconds()), 7200)
        rotated = self._refresh(http, issued["refresh_token"]).json()
        self.assertEqual(rotated["expires_in"], 120)
        new = self._rows()[-1]
        self.assertEqual(round((new.expires_at - new.created_at).total_seconds()), 900)

    def test_other_providers_and_the_default_keep_global_lifetimes(self):
        self.configure(MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON=json.dumps({"chatgpt": {"access_token_ttl_seconds": 120}}))
        self.assertEqual(self._login(self.client())["expires_in"], 300)

    def test_runtime_never_lengthens(self):
        lifetimes = hardening.token_lifetimes(
            SimpleNamespace(MCP_OAUTH_PROVIDER_TOKEN_POLICY_JSON=json.dumps({"claude": {"refresh_idle_seconds": 9000}})),
            "claude", access_ttl=300, idle=600, family=3600,
        )
        self.assertEqual((lifetimes.access_token_ttl_seconds, lifetimes.refresh_idle_seconds, lifetimes.refresh_family_max_seconds), (300, 600, 3600))


# ── Item 8: refresh rate-limit fairness ──────────────────────────────────


class RefreshFairnessTests(_Harness):
    def _junk(self, http):
        # A public client must not send a secret: client authentication fails.
        return http.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": "x" * 64, "client_id": CLAUDE_CLIENT, "client_secret": "guess",
        }, headers={"DPoP": self.signer.proof()})

    def _keys(self, mode="i"):
        return [key for keys, args in self.redis.calls for key, item in zip(keys, args[0::3]) if item == mode]

    def test_unauthenticated_refreshes_charge_only_network_and_failure_budgets(self):
        http = self.client()
        self.assertEqual(self._junk(http).status_code, 401)
        charged = self._keys()
        self.assertFalse([key for key in charged if ":refresh:client:" in key or ":refresh:global:" in key], charged)
        self.assertTrue([key for key in charged if ":refresh:network:" in key])
        self.assertTrue([key for key in charged if ":token:failures:" in key])

    def test_junk_cannot_exhaust_the_client_bucket(self):
        limits = (rate_limit.Bucket("network", 1000, 60), rate_limit.Bucket("client", 2, 60), rate_limit.Bucket("global", 1000, 60))
        with patch.dict(rate_limit.MCP_OAUTH_RATE_LIMITS, {"refresh": limits}):
            http = self.client()
            for _ in range(5):
                self.assertEqual(self._junk(http).status_code, 401)
            token = self._login(http)["refresh_token"]
            rotated = self._refresh(http, token)
            self.assertEqual(rotated.status_code, 200, rotated.text)
            self.assertEqual(self._refresh(http, rotated.json()["refresh_token"]).status_code, 200)
            throttled = self._refresh(http, "not-a-token")
            self.assertEqual(throttled.status_code, 429)
            self.assertEqual(throttled.json()["error"], "temporarily_unavailable")
            self.assertIn("Retry-After", throttled.headers)

    def test_provider_networks_get_the_higher_per_network_refresh_limit(self):
        self.configure(
            MCP_OAUTH_PROVIDER_SOURCE_CIDRS_JSON=json.dumps({"claude": ANTHROPIC}),
            MCP_OAUTH_PROVIDER_NETWORK_REFRESH_LIMIT_PER_MINUTE=40,
        )
        self.peer = INSIDE_IP
        http = self.client()
        statuses = [self._refresh(http, "not-a-token").status_code for _ in range(41)]
        self.assertEqual(statuses[:40], [400] * 40)
        self.assertEqual(statuses[40], 429)

    def test_ordinary_networks_keep_the_ordinary_limit(self):
        self.configure(MCP_OAUTH_PROVIDER_NETWORK_REFRESH_LIMIT_PER_MINUTE=40)
        http = self.client()
        statuses = [self._refresh(http, "not-a-token").status_code for _ in range(31)]
        self.assertEqual(statuses[30], 429)


# ── Items 3 and 7: live session check and anomaly guard ──────────────────


ACL = {"email": refresh_tests.USER_EMAIL, "is_admin": False, "granted_node_keys": [], "authorization_fingerprint": "f", "resolved_roles": []}


class LiveSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        BaseV2.metadata.create_all(self.engine, tables=[
            BotConfigurationVersionV2.__table__, BotConfigurationStateV2.__table__, McpOAuthRefreshTokenV2.__table__,
        ])
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        now = datetime.now(timezone.utc)
        self.db.add(BotConfigurationVersionV2(id=1, version=1, settings=ENABLED_EXTERNAL, is_default=False, created_by_email="a@renphil.org", created_at=now))
        self.db.add(BotConfigurationStateV2(id=1, active_version_id=1, updated_at=now))
        self.db.commit()
        self.settings = self._settings()
        self.redis = FakeUpstash()
        self.store = rate_limit.UpstashRateLimitStore(url="https://example-upstash.invalid", token="t", client=self.redis)
        for item in (
            patch.object(agent_access, "get_settings", lambda: self.settings),
            patch.object(agent_access, "_build_verified_live_acl_response", lambda identity, db: dict(ACL)),
            patch.object(mcp_oauth_router, "_rate_limit_store", lambda _settings: self.store),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.db.close)

    @staticmethod
    def _settings(**overrides):
        return mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_SESSION_LIVE_CHECK_ENABLED=True, **overrides)

    def _family(self, *, client_id=CLAUDE_CLIENT, email=refresh_tests.USER_EMAIL, **fields) -> str:
        now = datetime.now(timezone.utc)
        family_id = f"fam{self.db.query(McpOAuthRefreshTokenV2).count():029d}"
        values = dict(
            family_id=family_id, token_hash=family_id.ljust(64, "0"), client_id=client_id, user_email=email,
            user_subject="42", jkt=None, scope="renphil.mcp.read", created_at=now,
            expires_at=now + timedelta(days=1), family_expires_at=now + timedelta(days=30),
        )
        values.update(fields)
        self.db.add(McpOAuthRefreshTokenV2(**values))
        self.db.commit()
        return family_id

    def http(self) -> TestClient:
        application = FastAPI()
        application.include_router(agent_access.router)

        def db_override():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db_v2] = db_override
        application.dependency_overrides[require_agent_identity] = lambda: AgentIdentity(email=refresh_tests.USER_EMAIL, roles=())
        return TestClient(application)

    def call(self, client_id=CLAUDE_CLIENT, sid: str | None = None):
        headers = {hardening.SID_HEADER: sid} if sid is not None else {}
        return self.http().get("/data/agent/access/verified-granted-nodes", params={"client_id": client_id}, headers=headers)

    def test_live_family_is_admitted(self):
        sid = self._family()
        response = self.call(sid=sid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["client_kind"], "claude")

    def test_revoked_expired_unknown_foreign_or_missing_sessions_are_403(self):
        now = datetime.now(timezone.utc)
        cases = {
            "revoked": self._family(revoked_at=now),
            "idle-expired": self._family(expires_at=now - timedelta(seconds=1)),
            "family-expired": self._family(family_expires_at=now - timedelta(seconds=1)),
            "rotated-only": self._family(used_at=now),
            "other-client": self._family(client_id="chatgpt-approved-client-91c2"),
            "other-user": self._family(email="someone.else@renphil.org"),
            "unknown": "f" * 32,
            "malformed": "bad sid!",
            "missing": None,
        }
        for label, sid in cases.items():
            with self.subTest(label):
                response = self.call(sid=sid)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "MCP access is not permitted for this client."})

    def test_hub_agent_and_disabled_check_are_unaffected(self):
        self.assertEqual(self.call(client_id="renphil-agent").status_code, 200)
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True)
        self.assertEqual(self.call().status_code, 200)  # off by default: no sid needed

    def test_anomaly_limit_revokes_the_family_and_denies(self):
        self.settings = self._settings(MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON=json.dumps({"claude": 3}))
        sid = self._family()
        other = self._family()
        self.assertEqual([self.call(sid=sid).status_code for _ in range(3)], [200, 200, 200])
        with patch.object(hardening, "notify_user_of_anomaly") as notify, self.assertLogs(AUDIT_LOGGER, level="WARNING") as logs:
            self.assertEqual(self.call(sid=sid).status_code, 403)
        notify.assert_called_once()
        joined = "\n".join(logs.output)
        self.assertIn("category=anomaly", joined)
        self.assertIn("event=family_revoked reason=anomaly", joined)
        self.assertNotIn(refresh_tests.USER_EMAIL, joined)
        self.db.expire_all()
        revoked = {row.family_id: row.revoked_at for row in self.db.query(McpOAuthRefreshTokenV2)}
        self.assertIsNotNone(revoked[sid])
        self.assertIsNone(revoked[other])  # only the offending session
        keys = [key for keys, _ in self.redis.calls for key in keys]
        self.assertTrue(all(":mcp-daily:claude:" in key for key in keys))
        self.assertFalse(any(refresh_tests.USER_EMAIL in key for key in keys))

    def test_anomaly_store_outage_fails_closed(self):
        self.settings = self._settings(MCP_OAUTH_ANOMALY_DAILY_CALL_LIMIT_JSON=json.dumps({"claude": 3}))
        sid = self._family()
        self.redis.down = True
        response = self.call(sid=sid)
        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response.headers)


# ── Item 4: admin per-user revoke ────────────────────────────────────────


ADMIN = UserInfo(email="admin@renphil.org", name="Admin", roles=["Hub Admin"])


class AdminRevokeSessionsTests(unittest.TestCase):
    PATH = "/v2/bot-management/mcp/users/42/revoke-sessions"

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        BaseV2.metadata.create_all(self.engine, tables=[McpOAuthRefreshTokenV2.__table__])
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.is_admin = True
        self.app_settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True)
        for item in (
            patch.object(bot_management, "get_app_settings", lambda: self.app_settings),
            patch.object(bot_management, "hub_admin_target_ids", lambda db: (1, [10])),
            patch.object(bot_management, "effective_pairs", lambda db, user_id: {(1, 10)} if self.is_admin else {(1, 22)}),
            patch("app.config.get_settings", lambda: SimpleNamespace(JWT_SECRET_KEY=JWT_SECRET)),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _row(self, family_id: str, *, client_id=CLAUDE_CLIENT, subject="42", revoked=False, rotated=False) -> None:
        now = datetime.now(timezone.utc)
        self.db.add(McpOAuthRefreshTokenV2(
            family_id=family_id, token_hash=(family_id + ("r" if rotated else "")).ljust(64, "0"), client_id=client_id,
            user_email="member.person@renphil.org", user_subject=subject, jkt=None, scope="renphil.mcp.read",
            created_at=now, expires_at=now + timedelta(days=1), family_expires_at=now + timedelta(days=30),
            revoked_at=now if revoked else None, used_at=now if rotated else None,
        ))
        self.db.commit()

    def http(self) -> TestClient:
        application = FastAPI()
        application.include_router(bot_management.router)

        def db_override():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db_v2] = db_override
        application.dependency_overrides[get_current_hub_user] = lambda: SimpleNamespace(hub_user_id=7, info=ADMIN, email=ADMIN.email)
        return TestClient(application)

    def _revoked(self) -> dict[str, bool]:
        self.db.expire_all()
        states: dict[str, bool] = {}
        for row in self.db.query(McpOAuthRefreshTokenV2):
            states[row.family_id] = states.get(row.family_id, True) and row.revoked_at is not None
        return states

    def _seed(self) -> None:
        self._row("claude-a")
        self._row("claude-a", rotated=True)  # two rows, one family
        self._row("claude-b")
        self._row("gpt-a", client_id="chatgpt-approved-client-91c2")
        self._row("already", revoked=True)
        self._row("other-user", subject="43")

    def test_admin_revokes_all_external_families_of_the_user(self):
        self._seed()
        with self.assertLogs(AUDIT_LOGGER, level="INFO") as logs:
            response = self.http().post(self.PATH, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"revoked": 3})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self._revoked(), {"claude-a": True, "claude-b": True, "gpt-a": True, "already": True, "other-user": False})
        self.assertIn("reason=user_revoke", "\n".join(logs.output))
        # No body at all behaves the same; nothing is left to revoke.
        self.assertEqual(self.http().post(self.PATH).json(), {"revoked": 0})

    def test_client_kind_limits_the_revocation(self):
        self._seed()
        response = self.http().post(self.PATH, json={"client_kind": "claude"})
        self.assertEqual(response.json(), {"revoked": 2})
        self.assertFalse(self._revoked()["gpt-a"])
        self.assertEqual(self.http().post(self.PATH, json={"client_kind": "gemini"}).json(), {"revoked": 0})
        self.assertEqual(self.http().post(self.PATH, json={"client_kind": "hub_agent"}).status_code, 422)
        self.assertEqual(self.http().post(self.PATH, json={"client_kind": "claude", "extra": 1}).status_code, 422)

    def test_non_admin_is_403_and_revokes_nothing(self):
        self._seed()
        self.is_admin = False
        response = self.http().post(self.PATH, json={})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self._revoked()["claude-a"])

    def test_view_as_is_blocked(self):
        self._seed()
        for param in ("view_as", "view_as_from", "anonymous"):
            with self.subTest(param):
                response = self.http().post(self.PATH, params={param: "1"}, json={})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], "Exit View As to revoke MCP sessions.")
        self.assertFalse(self._revoked()["claude-a"])

    def test_refresh_tokens_disabled_touches_nothing(self):
        self.app_settings = mcp_settings()
        self.engine.dispose()
        response = self.http().post(self.PATH, json={})
        self.assertEqual(response.json(), {"revoked": 0})


if __name__ == "__main__":
    unittest.main()
