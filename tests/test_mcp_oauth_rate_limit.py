"""Abuse protection for Hub's public MCP OAuth endpoints.

Covers the limiter itself (authorize, token, registration, discovery/JWKS),
its fail-closed behaviour, privacy of keys/logs/responses, single-use codes
under limiting, and the compatibility boundary: Hub Google login, ``/auth/*``,
normal session creation and authenticated Hub APIs are never limited.

The production ``UpstashRateLimitStore`` is exercised against an in-test fake
Upstash client that executes the same Lua contract atomically, so shared and
concurrent behaviour runs through the deployed code path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routers.auth as auth_router
import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.database import BaseV2, get_db_v2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from app.services import mcp_oauth_rate_limit as rate_limit
from app.services.mcp_oauth_keys import HubMcpSigningKey
from app.services.mcp_oauth_service import LocalMcpOAuthService, PersistentMcpOAuthService

_ORIGINAL_SERVICE = mcp_oauth_router._service
_ORIGINAL_STORE = mcp_oauth_router._rate_limit_store

ISSUER = "https://hub.example/mcp/oauth"
CLAUDE_CLIENT = "claude-approved-client-7f3a"
GPT_CLIENT = "chatgpt-approved-client-91c2"
REDIRECT = "https://claude.example/api/mcp/auth_callback"
JWT_SECRET = "server-only-jwt-secret-for-tests-0123456789"
UPSTASH_TOKEN = "upstash-rest-token-secret-value"
EMAIL = "Member.Person@renphil.org"
CLIENT_IP = "203.0.113.77"
ENABLED_EXTERNAL = {
    "bot_enabled": True, "mcp_external_connections_enabled": True,
    "mcp_claude_enabled": True, "mcp_chatgpt_enabled": True, "mcp_gemini_enabled": True,
}


def mcp_settings(**overrides) -> SimpleNamespace:
    values = dict(
        MCP_OAUTH_ENABLED=True,
        MCP_OAUTH_ISSUER=ISSUER,
        MCP_OAUTH_AGENT_CLIENT_ID="renphil-agent",
        MCP_OAUTH_CLIENTS_JSON=json.dumps({
            CLAUDE_CLIENT: {"redirect_uris": [REDIRECT]},
            GPT_CLIENT: {"redirect_uris": ["https://chatgpt.example/connector/callback"]},
            "renphil-agent": {"redirect_uris": ["https://hub.example/agent"]},
        }),
        MCP_OAUTH_CLIENT_PROVIDERS_JSON=json.dumps({CLAUDE_CLIENT: "claude", GPT_CLIENT: "chatgpt"}),
        UPSTASH_REDIS_REST_URL="https://example-upstash.invalid",
        UPSTASH_REDIS_REST_TOKEN=UPSTASH_TOKEN,
        JWT_SECRET_KEY=JWT_SECRET,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def dpop_proof(token_endpoint: str) -> str:
    import uuid

    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "EC", "crv": "P-256",
        "x": jwt.utils.base64url_encode(numbers.x.to_bytes(32, "big")).decode(),
        "y": jwt.utils.base64url_encode(numbers.y.to_bytes(32, "big")).decode(),
    }
    return jwt.encode(
        {"jti": uuid.uuid4().hex, "htm": "POST", "htu": token_endpoint, "iat": int(time.time())},
        private, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk},
    )


class FakeUpstash:
    """Executes the limiter's EVAL contract atomically (single event loop)."""

    def __init__(self, clock=time.monotonic) -> None:
        self.data: dict[str, tuple[int, float | None]] = {}
        self.clock = clock
        self.down = False
        self.calls: list[tuple[list[str], list[str]]] = []

    async def eval(self, script, keys, args):
        if self.down:
            raise ConnectionError("connect failed https://example-upstash.invalid token=" + UPSTASH_TOKEN)
        assert "INCR" in script and "PTTL" in script
        self.calls.append((list(keys), list(args)))
        now = self.clock()
        for index, key in enumerate(keys):
            mode, limit, window_ms = args[index * 3], int(args[index * 3 + 1]), int(args[index * 3 + 2])
            count, expires = self.data.get(key, (0, None))
            if expires is not None and expires <= now:
                count, expires = 0, None
            if mode == "p":
                observed = count + 1
            else:
                count += 1
                observed = count
                if expires is None:
                    expires = now + window_ms / 1000
                self.data[key] = (count, expires)
            ttl = int(math.ceil((expires - now) * 1000)) if expires is not None else window_ms
            if observed > limit:
                return [0, index + 1, ttl]
        return [1, 0, 0]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        BaseV2.metadata.create_all(self.engine, tables=[
            McpOAuthAuthorizationRequestV2.__table__, McpOAuthAuthorizationCodeV2.__table__,
            BotConfigurationVersionV2.__table__, BotConfigurationStateV2.__table__,
        ])
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self._activate(ENABLED_EXTERNAL)
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.settings = mcp_settings()
        self.service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON),
            issuer=ISSUER, audience="renphil-data-mcp", signing_key=HubMcpSigningKey(private_pem=pem, key_id="k1"),
        )
        self.redis = FakeUpstash()
        self.store = rate_limit.UpstashRateLimitStore(url="https://example-upstash.invalid", token=UPSTASH_TOKEN, client=self.redis)
        self.store_override = self.store
        patches = [
            patch.object(mcp_oauth_router, "get_settings", lambda: self.settings),
            patch.object(mcp_oauth_router, "_service", lambda: self.service),
            patch.object(mcp_oauth_router, "_rate_limit_store", lambda _settings: self.store_override),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.db.close)

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

    def app(self) -> FastAPI:
        application = FastAPI()
        application.include_router(auth_router.router)
        application.include_router(mcp_oauth_router.router)
        application.include_router(mcp_oauth_router.metadata_router)

        def db_override():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db_v2] = db_override
        return application

    def client(self) -> TestClient:
        return TestClient(self.app(), follow_redirects=False)

    @staticmethod
    def challenge(verifier: str) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    def authorize_params(self, *, client_id: str = CLAUDE_CLIENT, verifier: str = "v" * 64) -> dict:
        return {
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "client-state",
            "code_challenge": self.challenge(verifier), "code_challenge_method": "S256",
        }

    def issue_code(self, http: TestClient, *, verifier: str = "v" * 64, ip: str = CLIENT_IP) -> str:
        started = http.get("/mcp/oauth/authorize", params=self.authorize_params(verifier=verifier), headers={"x-forwarded-for": ip})
        self.assertEqual(started.status_code, 307, started.text)
        continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
        with self.Session() as session:
            completed = mcp_oauth_router.complete_hub_login(db=session, continuation_uri=continuation, email=EMAIL, subject="42")
        return parse_qs(urlsplit(completed).query)["code"][0]

    def exchange(self, http: TestClient, code: str, *, verifier: str = "v" * 64, ip: str = CLIENT_IP, client_id: str = CLAUDE_CLIENT):
        return http.post(
            "/mcp/oauth/token",
            data={"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": REDIRECT, "code_verifier": verifier},
            headers={"DPoP": dpop_proof(f"{ISSUER}/token"), "x-forwarded-for": ip},
        )


class LegitimateFlowTests(_Base):
    def test_authorize_login_and_single_dpop_bound_exchange_succeed_under_the_limiter(self):
        http = self.client()
        code = self.issue_code(http)
        issued = self.exchange(http, code)
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.json()["token_type"], "DPoP")
        # A code is still single use while limiting is active.
        replay = self.exchange(http, code)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json(), {"detail": "Invalid OAuth token request."})
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)

    def test_authorize_still_redirects_into_normal_hub_login(self):
        response = self.client().get("/mcp/oauth/authorize", params=self.authorize_params())
        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["location"].startswith("/auth/login?redirect_uri="))

    def test_discovery_and_jwks_still_served(self):
        http = self.client()
        metadata = http.get("/.well-known/oauth-authorization-server")
        self.assertEqual(metadata.status_code, 200)
        self.assertNotIn("registration_endpoint", metadata.json())
        self.assertEqual(http.get("/mcp/oauth/jwks.json").json()["keys"][0]["kid"], "k1")
        self.assertEqual(http.post("/mcp/oauth/register", json={"redirect_uris": ["https://x"]}).status_code, 400)
        self.assertEqual(http.post("/register").status_code, 400)

    def test_disabled_mcp_oauth_keeps_its_behaviour_and_never_touches_the_store(self):
        self.settings = mcp_settings(MCP_OAUTH_ENABLED=False)
        self.store_override = None  # would be a 503 if consulted
        with patch.object(mcp_oauth_router, "_service", _ORIGINAL_SERVICE.__wrapped__):
            response = self.client().get("/mcp/oauth/jwks.json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.redis.calls, [])


class AuthorizeAbuseTests(_Base):
    def test_repeated_authorization_is_throttled_with_retry_after(self):
        http = self.client()
        statuses = [http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP}).status_code for _ in range(61)]
        self.assertEqual(statuses[:60], [307] * 60)
        throttled = http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP})
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled.json(), {"error": "temporarily_unavailable", "error_description": "Too many requests. Retry later."})
        self.assertTrue(1 <= int(throttled.headers["retry-after"]) <= 60)
        self.assertNotIn("location", throttled.headers)
        self.assertEqual(throttled.headers["cache-control"], "no-store")

    def test_one_network_being_throttled_does_not_lock_out_other_networks(self):
        http = self.client()
        for _ in range(61):
            http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP})
        other = http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": "198.51.100.9"})
        self.assertEqual(other.status_code, 307)

    def test_retry_after_tracks_the_remaining_window(self):
        clock = [1000.0]
        self.redis.clock = lambda: clock[0]
        http = self.client()
        for _ in range(60):
            http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP})
        clock[0] += 22.5
        self.assertEqual(http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP}).headers["retry-after"], "38")
        clock[0] += 38.0
        self.assertEqual(http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP}).status_code, 307)

    def test_ipv6_privacy_addresses_in_one_slash_64_share_a_bucket(self):
        self.assertEqual(
            rate_limit.client_network(forwarded_for="2001:db8:1:2:aaaa::1", peer_host=None),
            rate_limit.client_network(forwarded_for="2001:db8:1:2:bbbb::9", peer_host=None),
        )
        self.assertNotEqual(
            rate_limit.client_network(forwarded_for="2001:db8:1:2::1", peer_host=None),
            rate_limit.client_network(forwarded_for="2001:db8:1:3::1", peer_host=None),
        )
        # The right-most (proxy-observed) entry is used; prepended spoofs are ignored.
        self.assertEqual(rate_limit.client_network(forwarded_for="10.9.9.9, 203.0.113.77", peer_host=None), "203.0.113.77")
        self.assertEqual(rate_limit.client_network(forwarded_for="not-an-ip", peer_host=None), "invalid")
        self.assertEqual(rate_limit.client_network(forwarded_for=None, peer_host="::ffff:198.51.100.1"), "198.51.100.1")


class TokenAbuseTests(_Base):
    def test_repeated_invalid_codes_are_throttled_and_then_block_even_a_valid_code(self):
        http = self.client()
        code = self.issue_code(http)
        statuses = [self.exchange(http, f"guess-{index}").status_code for index in range(20)]
        self.assertEqual(statuses, [400] * 20)
        blocked = self.exchange(http, code)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("retry-after", blocked.headers)
        # The throttled request did not consume the valid code.
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        # Another network can still complete the legitimate exchange.
        self.assertEqual(self.exchange(http, code, ip="198.51.100.20").status_code, 200)

    def test_request_volume_per_network_is_bounded(self):
        http = self.client()
        with patch.dict(rate_limit.MCP_OAUTH_RATE_LIMITS, {"token": (
            rate_limit.Bucket("failures", 1000, 600, peek=True), rate_limit.Bucket("network", 30, 60),
            rate_limit.Bucket("client", 300, 60), rate_limit.Bucket("global", 1500, 60),
        )}):
            statuses = [self.exchange(http, "bad").status_code for _ in range(31)]
        self.assertEqual(statuses[:30], [400] * 30)
        self.assertEqual(statuses[30], 429)

    def test_a_code_remains_single_use_under_concurrent_exchanges(self):
        http = self.client()
        code = self.issue_code(http)
        app = self.app()

        async def race():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                calls = [client.post(
                    "/mcp/oauth/token",
                    data={"grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT, "redirect_uri": REDIRECT, "code_verifier": "v" * 64},
                    headers={"DPoP": dpop_proof(f"{ISSUER}/token"), "x-forwarded-for": f"198.51.100.{index}"},
                ) for index in range(10)]
                return [response.status_code for response in await asyncio.gather(*calls)]

        statuses = asyncio.run(race())
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(statuses.count(400), 9, statuses)


class UnknownAndMalformedClientTests(_Base):
    def test_unknown_or_malformed_client_ids_never_500_and_share_one_bounded_bucket(self):
        http = self.client()
        weird = ["", "x" * 5000, "nul\x00byte", "日本語", "renphil-agent", "unknown-client", " " + CLAUDE_CLIENT]
        for index, client_id in enumerate(weird):
            params = dict(self.authorize_params(), client_id=client_id)
            response = http.get("/mcp/oauth/authorize", params=params, headers={"x-forwarded-for": f"192.0.2.{index}"})
            self.assertIn(response.status_code, {400, 403, 422}, (client_id[:20], response.status_code))
            token = self.exchange(http, "c", client_id=client_id, ip=f"192.0.2.{index}")
            self.assertIn(token.status_code, {400, 403, 422})
        missing = http.get("/mcp/oauth/authorize", headers={"x-forwarded-for": "192.0.2.200"})
        self.assertEqual(missing.status_code, 422)
        garbage = http.post("/mcp/oauth/token", content=b"\xff\xfe\x00", headers={"content-type": "multipart/form-data; boundary=x"})
        self.assertLess(garbage.status_code, 500)
        client_keys = {key for keys, _ in self.redis.calls for key in keys if ":client:" in key}
        self.assertEqual({key.rsplit(":", 1)[1] for key in client_keys}, {"unknown"})

    def test_unknown_client_floods_cannot_exhaust_a_real_providers_budget(self):
        http = self.client()
        for index in range(61):
            http.get("/mcp/oauth/authorize", params=dict(self.authorize_params(), client_id=f"bogus-{index}"), headers={"x-forwarded-for": f"10.0.{index}.1"})
        flood = http.get("/mcp/oauth/authorize", params=dict(self.authorize_params(), client_id="bogus-x"), headers={"x-forwarded-for": "10.9.9.9"})
        self.assertEqual(flood.status_code, 429)
        real = http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": "10.9.9.10"})
        self.assertEqual(real.status_code, 307)


class SharedAndConcurrentTests(_Base):
    def test_concurrent_requests_respect_one_global_limit(self):
        app = self.app()
        with patch.dict(rate_limit.MCP_OAUTH_RATE_LIMITS, {"discovery": (rate_limit.Bucket("network", 120, 60), rate_limit.Bucket("global", 10, 60))}):
            async def burst():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    calls = [client.get("/.well-known/oauth-authorization-server", headers={"x-forwarded-for": f"198.51.100.{index}"}) for index in range(50)]
                    return [response.status_code for response in await asyncio.gather(*calls)]

            statuses = asyncio.run(burst())
        self.assertEqual(statuses.count(200), 10)
        self.assertEqual(statuses.count(429), 40)

    def test_two_hub_instances_share_one_budget(self):
        first, second = self.client(), self.client()
        with patch.dict(rate_limit.MCP_OAUTH_RATE_LIMITS, {"jwks": (rate_limit.Bucket("network", 4, 60), rate_limit.Bucket("global", 1000, 60))}):
            statuses = [(first if index % 2 else second).get("/mcp/oauth/jwks.json", headers={"x-forwarded-for": CLIENT_IP}).status_code for index in range(6)]
        self.assertEqual(statuses, [200, 200, 200, 200, 429, 429])

    def test_registration_has_the_strictest_limit_and_stays_disabled(self):
        http = self.client()
        statuses = [http.post("/mcp/oauth/register", json={}, headers={"x-forwarded-for": CLIENT_IP}).status_code for _ in range(6)]
        self.assertEqual(statuses, [400] * 5 + [429])
        self.assertEqual(http.post("/register", headers={"x-forwarded-for": "198.51.100.5"}).status_code, 400)


class StoreOutageTests(_Base):
    def test_store_outage_fails_closed_with_retry_after_on_every_limited_endpoint(self):
        http = self.client()
        self.redis.down = True
        with self.assertLogs("app.routers.mcp_oauth", level="WARNING") as logs:
            responses = [
                http.get("/mcp/oauth/authorize", params=self.authorize_params()),
                http.post("/mcp/oauth/token", data={"grant_type": "authorization_code", "code": "c", "client_id": CLAUDE_CLIENT, "redirect_uri": REDIRECT, "code_verifier": "v" * 64}),
                http.get("/.well-known/oauth-authorization-server"),
                http.get("/mcp/oauth/jwks.json"),
                http.post("/mcp/oauth/register"),
            ]
        for response in responses:
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["retry-after"], "5")
            self.assertEqual(response.json()["error"], "temporarily_unavailable")
        rendered = " ".join(logs.output) + " ".join(response.text for response in responses)
        self.assertIn("category=rate_limit_store_unavailable", rendered)
        for secret in (UPSTASH_TOKEN, "example-upstash", "connect failed"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(self.db.query(McpOAuthAuthorizationRequestV2).count(), 0)

    def test_missing_store_outside_local_development_fails_closed(self):
        settings = mcp_settings(UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None)
        self.assertIsNone(rate_limit.build_store(settings))
        self.store_override = None
        self.assertEqual(self.client().get("/mcp/oauth/jwks.json").status_code, 503)

    def test_local_development_uses_the_local_only_store(self):
        local = mcp_settings(MCP_OAUTH_ISSUER="http://127.0.0.1:8000/mcp/oauth", UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None)
        self.assertIsInstance(rate_limit.build_store(local), rate_limit.LocalOnlyRateLimitStore)
        for issuer in ("https://hub.renphil.org/mcp/oauth", "https://localhost.evil.example/mcp/oauth", "not a url", None):
            self.assertIsNone(rate_limit.build_store(mcp_settings(MCP_OAUTH_ISSUER=issuer, UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None)), issuer)
        self.assertIsInstance(rate_limit.build_store(mcp_settings()), rate_limit.UpstashRateLimitStore)

    def test_malformed_store_reply_is_an_outage(self):
        class Weird:
            async def eval(self, *_args):
                return "OK"

        store = rate_limit.UpstashRateLimitStore(url="u", token="t", client=Weird())
        with self.assertRaises(rate_limit.RateLimitStoreUnavailable):
            asyncio.run(store.evaluate([rate_limit.Check("k", 1, 60)]))

    def test_router_store_cache_builds_one_store_per_configuration(self):
        mcp_oauth_router._STORES.clear()
        first = _ORIGINAL_STORE(mcp_settings())
        self.assertIs(first, _ORIGINAL_STORE(mcp_settings()))
        self.assertIsInstance(first, rate_limit.UpstashRateLimitStore)
        self.assertIsNone(_ORIGINAL_STORE(mcp_settings(UPSTASH_REDIS_REST_URL=None)))
        mcp_oauth_router._STORES.clear()


class PrivacyTests(_Base):
    def test_no_raw_sensitive_values_in_keys_logs_or_responses(self):
        http = self.client()
        verifier = "pkce-verifier-" + "q" * 50
        with self.assertLogs("app", level="DEBUG") as logs:
            code = self.issue_code(http, verifier=verifier)
            ok = self.exchange(http, code, verifier=verifier)
            for _ in range(25):
                bad = self.exchange(http, code, verifier=verifier)  # replays -> failures -> 429
            logging.getLogger("app").debug("marker")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 429)
        access_token = ok.json()["access_token"]
        material = json.dumps(self.redis.calls) + " ".join(logs.output) + bad.text
        for value in (CLIENT_IP, "203.0.113", EMAIL, EMAIL.lower(), code, verifier, REDIRECT, "claude.example",
                      CLAUDE_CLIENT, access_token, JWT_SECRET, UPSTASH_TOKEN):
            self.assertNotIn(value, material, value[:24])
        for keys, _ in self.redis.calls:
            for key in keys:
                self.assertRegex(key, r"^renphil:mcp-oauth:rl:v1:[0-9a-f]{16}:(authorize|token|register|discovery|jwks):(network|failures|client|global):([0-9a-f]{40}|claude|chatgpt|gemini|unknown|all)$")

    def test_network_digest_is_keyed_and_environment_scoped(self):
        one = rate_limit.build_checks(mcp_settings(), "jwks", network=CLIENT_IP, client=None)[0].key
        other_secret = rate_limit.build_checks(mcp_settings(JWT_SECRET_KEY="another-secret"), "jwks", network=CLIENT_IP, client=None)[0].key
        other_env = rate_limit.build_checks(mcp_settings(MCP_OAUTH_ISSUER="https://sandbox.hub.example/mcp/oauth"), "jwks", network=CLIENT_IP, client=None)[0].key
        self.assertEqual(len({one, other_secret, other_env}), 3)
        self.assertNotIn(hashlib.sha256(CLIENT_IP.encode()).hexdigest()[:40], one)
        with self.assertRaises(rate_limit.RateLimitStoreUnavailable):
            rate_limit.build_checks(mcp_settings(JWT_SECRET_KEY=""), "jwks", network=CLIENT_IP, client=None)


class CompatibilityBoundaryTests(_Base):
    """Hub Google login and normal Hub APIs are never limited or changed."""

    MCP_PATHS = set(mcp_oauth_router.RATE_LIMITED_ENDPOINTS) | set(mcp_oauth_router.UNLIMITED_MCP_ROUTES)

    def _full_app(self):
        try:
            from app.main import create_app

            return create_app()
        except Exception as exc:  # pragma: no cover - environment without Hub settings
            self.skipTest(f"Hub settings unavailable: {type(exc).__name__}")

    @staticmethod
    def _walk(routes):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from CompatibilityBoundaryTests._walk(original.routes)
            else:
                yield route

    def test_only_mcp_oauth_paths_receive_the_limiter(self):
        limited, unlimited_mcp, others = set(), set(), []
        for route in self._walk(self._full_app().routes):
            if not isinstance(route, APIRoute):
                continue
            if isinstance(route, mcp_oauth_router.McpOAuthRateLimitedRoute):
                (limited if route.path in mcp_oauth_router.RATE_LIMITED_ENDPOINTS else unlimited_mcp).add(route.path)
            else:
                others.append(route.path)
        self.assertEqual(limited, set(mcp_oauth_router.RATE_LIMITED_ENDPOINTS))
        self.assertEqual(unlimited_mcp, set(mcp_oauth_router.UNLIMITED_MCP_ROUTES))
        self.assertTrue(any(path.startswith("/auth/") for path in others))
        for path in others:
            self.assertNotIn(path, self.MCP_PATHS)
            self.assertFalse(path.startswith("/mcp/oauth"), path)
        for route in auth_router.router.routes:
            self.assertIs(type(route), APIRoute, route.path)

    def test_hub_google_login_is_unchanged_and_never_throttled_even_when_mcp_limiter_is_down_or_exhausted(self):
        app = self._full_app()
        http = TestClient(app, follow_redirects=False)
        before = http.get("/auth/login", params={"redirect_uri": "https://hub-frontend.example/auth/callback"})
        self.assertEqual(before.status_code, 307)
        location = urlsplit(before.headers["location"])
        self.assertEqual(location.netloc, "accounts.google.com")
        query = parse_qs(location.query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["prompt"], ["select_account consent"])
        # Exhaust and then break the MCP limiter: Hub login is unaffected.
        self.redis.down = True
        for _ in range(200):
            response = http.get("/auth/login", params={"redirect_uri": "https://hub-frontend.example/auth/callback"}, headers={"x-forwarded-for": CLIENT_IP})
            self.assertEqual(response.status_code, 307)
            self.assertEqual(urlsplit(response.headers["location"]).netloc, "accounts.google.com")
        self.assertEqual(self.redis.calls, [])

    def _callback_app(self, frontend_uri: str):
        app = self.app()
        token = SimpleNamespace(access_token="hub-session-jwt", email=EMAIL, name="Member Person", roles=["Hub Member"], picture=None)

        class FakeAuth:
            async def handle_callback(self, code, state, airtable_service, db):
                return token, frontend_uri

        app.dependency_overrides[auth_router.get_auth_service] = lambda: FakeAuth()
        app.dependency_overrides[auth_router.get_airtable_service] = lambda: object()
        return app

    def test_hub_google_callback_behaviour_is_unchanged_while_mcp_limiting_is_down(self):
        self.redis.down = True
        hub_user = SimpleNamespace(id=42, email=EMAIL.lower())
        real_settings = SimpleNamespace(MCP_OAUTH_ENABLED=True)
        with patch.object(auth_router, "_ensure_hub_user", lambda *a, **k: hub_user), \
                patch.object(auth_router, "is_hub_admin", lambda *a, **k: False), \
                patch.object(auth_router, "get_settings", lambda: real_settings):
            http = TestClient(self._callback_app("https://hub-frontend.example/auth/callback"), follow_redirects=False)
            for _ in range(100):
                response = http.get("/auth/callback", params={"code": "google-code", "state": "s"}, headers={"x-forwarded-for": CLIENT_IP})
                self.assertEqual(response.status_code, 307)
                target = urlsplit(response.headers["location"])
                self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}", "https://hub-frontend.example/auth/callback")
                params = parse_qs(target.query)
                self.assertEqual(params["access_token"], ["hub-session-jwt"])
                self.assertEqual(params["is_hub_admin"], ["false"])
        self.assertEqual(self.redis.calls, [])

    def test_mcp_throttling_cannot_block_the_hub_login_that_completes_an_mcp_authorization(self):
        http = self.client()
        started = http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP})
        continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
        # The MCP limiter is now exhausted for this network and then unavailable.
        for _ in range(70):
            http.get("/mcp/oauth/authorize", params=self.authorize_params(), headers={"x-forwarded-for": CLIENT_IP})
        self.redis.down = True
        hub_user = SimpleNamespace(id=42, email=EMAIL.lower())
        with patch.object(auth_router, "_ensure_hub_user", lambda *a, **k: hub_user), \
                patch.object(auth_router, "is_hub_admin", lambda *a, **k: False), \
                patch.object(auth_router, "get_settings", lambda: SimpleNamespace(MCP_OAUTH_ENABLED=True)):
            callback = TestClient(self._callback_app(continuation), follow_redirects=False).get(
                "/auth/callback", params={"code": "google-code", "state": "s"}, headers={"x-forwarded-for": CLIENT_IP},
            )
        self.assertEqual(callback.status_code, 307)
        self.assertTrue(callback.headers["location"].startswith(REDIRECT + "?"))
        self.assertIn("code=", callback.headers["location"])
        self.assertNotIn("hub-session-jwt", callback.headers["location"])

    def test_normal_authenticated_hub_api_requests_are_unaffected(self):
        from app.dependencies import CurrentHubUser, get_current_hub_user
        from app.models.auth import UserInfo

        app = self._full_app()
        current = CurrentHubUser(info=UserInfo(email=EMAIL, name="Member Person", roles=["Hub Member"]), hub_user_id=42, email=EMAIL.lower())

        class FakeAirtable:
            async def get_user_scoped_roles(self, _email):
                return []

        app.dependency_overrides[get_current_hub_user] = lambda: current
        app.dependency_overrides[auth_router.get_airtable_service] = lambda: FakeAirtable()
        app.dependency_overrides[get_db_v2] = lambda: None
        self.redis.down = True
        with patch.object(auth_router, "is_hub_admin", lambda *a, **k: False):
            http = TestClient(app)
            for _ in range(150):
                me = http.get("/auth/me", headers={"x-forwarded-for": CLIENT_IP})
                self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(http.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.redis.calls, [])

    def test_agent_token_route_is_not_rate_limited(self):
        self.redis.down = True
        with patch("app.services.agent_identity.get_settings", return_value=SimpleNamespace(AGENT_SYNC_TOKEN="sync-secret")):
            response = self.client().post("/mcp/oauth/agent-token", headers={"X-Sync-Token": "wrong"})
        # Rejected by its own sync-token authentication, never by the limiter.
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("temporarily_unavailable", response.text)
        self.assertEqual(self.redis.calls, [])


if __name__ == "__main__":
    unittest.main()
