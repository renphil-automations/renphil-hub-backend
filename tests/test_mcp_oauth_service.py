from __future__ import annotations

import base64
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_v2.database import BaseV2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from app.services.mcp_oauth_keys import HubMcpSigningKey
from app.services.mcp_oauth_service import LocalMcpOAuthService, McpOAuthError, PersistentMcpOAuthService
from app.services.agent_identity import require_agent_sync_token
import app.routers.mcp_oauth as mcp_oauth_router


ENABLED_EXTERNAL = {
    "bot_enabled": True,
    "mcp_external_connections_enabled": True,
    "mcp_claude_enabled": True, "mcp_chatgpt_enabled": True, "mcp_gemini_enabled": True,
}


def configured_settings(**overrides) -> SimpleNamespace:
    values = dict(
        MCP_OAUTH_ENABLED=True,
        MCP_OAUTH_ISSUER="https://hub.example/mcp/oauth",
        MCP_OAUTH_AGENT_CLIENT_ID="renphil-agent",
        MCP_OAUTH_CLIENTS_JSON=(
            '{"client":{"redirect_uris":["https://client.example/callback"]},'
            '"gpt-client":{"redirect_uris":["https://client.example/callback"]},'
            '"unmapped-client":{"redirect_uris":["https://client.example/callback"]},'
            '"renphil-agent":{"redirect_uris":["https://hub.example/agent"]}}'
        ),
        MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"client": "claude", "gpt-client": "chatgpt"}',
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def dpop_proof(token_endpoint: str) -> str:
    import time
    import uuid
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


class McpOAuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        clients = LocalMcpOAuthService.parse_clients(
            '{"local-client":{"redirect_uris":["http://127.0.0.1:9876/callback"]}}'
        )
        self.service = LocalMcpOAuthService(
            clients=clients,
            issuer="http://127.0.0.1:8000/mcp/oauth",
            audience="renphil-data-mcp-local",
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes",
        )

    def _challenge(self, verifier: str) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

    def test_exact_client_redirect_pkce_and_one_time_code(self):
        verifier = "V" * 64
        request_id = self.service.start_authorization(
            client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback",
            state="opaque-client-state", code_challenge=self._challenge(verifier), code_challenge_method="S256",
        )
        code, redirect_uri, state = self.service.complete_authorization(
            request_id=request_id, email="Member@RenPhil.org", subject="hub-user-1",
        )
        self.assertEqual((redirect_uri, state), ("http://127.0.0.1:9876/callback", "opaque-client-state"))
        token_response = self.service.exchange_code(
            code=code, client_id="local-client", redirect_uri=redirect_uri, code_verifier=verifier,
            dpop_jkt="thumb", client_kind="claude",
        )
        claims = jwt.decode(
            token_response["access_token"], "local-oauth-signing-secret-with-at-least-thirty-two-bytes",
            algorithms=["HS256"], audience="renphil-data-mcp-local", issuer="http://127.0.0.1:8000/mcp/oauth",
        )
        self.assertEqual((claims["email"], claims["client_id"]), ("member@renphil.org", "local-client"))
        self.assertEqual(claims["renphil_client_kind"], "claude")
        self.assertNotIn("roles", claims)
        with self.assertRaises(McpOAuthError):
            self.service.exchange_code(code=code, client_id="local-client", redirect_uri=redirect_uri, code_verifier=verifier, dpop_jkt="thumb", client_kind="claude")

    def test_rejects_unknown_redirect_and_bad_pkce(self):
        verifier = "V" * 64
        with self.assertRaises(McpOAuthError):
            self.service.start_authorization(
                client_id="local-client", redirect_uri="https://attacker.invalid/callback", state="s",
                code_challenge=self._challenge(verifier), code_challenge_method="S256",
            )
        request_id = self.service.start_authorization(
            client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback", state="s",
            code_challenge=self._challenge(verifier), code_challenge_method="S256",
        )
        code, redirect_uri, _ = self.service.complete_authorization(request_id=request_id, email="member@renphil.org", subject="hub-user-1")
        with self.assertRaises(McpOAuthError):
            self.service.exchange_code(code=code, client_id="local-client", redirect_uri=redirect_uri, code_verifier="x" * 64, dpop_jkt="thumb", client_kind="claude")

    def test_access_token_ttl_is_configurable_and_bounded(self):
        service = LocalMcpOAuthService(
            clients=self.service.clients,
            issuer=self.service.issuer,
            audience=self.service.audience,
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes",
            access_token_ttl_seconds=90,
        )
        verifier = "v" * 64
        request_id = service.start_authorization(
            client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback",
            state="state", code_challenge=self._challenge(verifier), code_challenge_method="S256",
        )
        code, redirect_uri, _ = service.complete_authorization(
            request_id=request_id, email="member@renphil.org", subject="hub-user-1",
        )
        result = service.exchange_code(
            code=code, client_id="local-client", redirect_uri=redirect_uri, code_verifier=verifier,
            dpop_jkt="thumb", client_kind="claude",
        )
        claims = jwt.decode(result["access_token"], options={"verify_signature": False})
        self.assertEqual(result["expires_in"], 90)
        self.assertEqual(claims["exp"] - claims["iat"], 90)
        self.assertEqual(LocalMcpOAuthService(
            clients=self.service.clients, issuer=self.service.issuer, audience=self.service.audience,
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes",
            access_token_ttl_seconds=9999,
        ).access_token_ttl_seconds, 3600)


class PersistentMcpOAuthServiceTests(unittest.TestCase):
    @staticmethod
    def _resolve_noawait(coroutine):
        """Route handlers under test contain no awaits; avoid Windows sandbox I/O."""
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        raise AssertionError("OAuth route unexpectedly awaited I/O")

    def setUp(self):
        engine = create_engine("sqlite://")
        # The OAuth routes consult the active Bot Management configuration
        # (external-connection and per-provider toggles) before issuing codes.
        BaseV2.metadata.create_all(engine, tables=[
            McpOAuthAuthorizationRequestV2.__table__, McpOAuthAuthorizationCodeV2.__table__,
            BotConfigurationVersionV2.__table__, BotConfigurationStateV2.__table__,
        ])
        self.db = sessionmaker(bind=engine)()
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.key = HubMcpSigningKey(private_pem=pem, key_id="test-key")
        self.service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"client":{"redirect_uris":["https://client.example/callback"]}}'),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp",
            signing_key=self.key,
        )

    def tearDown(self):
        self.db.close()

    def test_persists_hashed_one_time_code_and_issues_rs256_token(self):
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        request_id = self.service.start_authorization(self.db, client_id="client", redirect_uri="https://client.example/callback", state="state", code_challenge=challenge, code_challenge_method="S256")
        code, redirect_uri, state = self.service.complete_authorization(
            self.db, request_id=request_id, email="Member@RenPhil.org", subject="42"        )
        self.assertEqual((redirect_uri, state), ("https://client.example/callback", "state"))
        stored = self.db.query(McpOAuthAuthorizationCodeV2).one()
        self.assertNotEqual(stored.code_hash, code)
        token = self.service.exchange_code(self.db, code=code, client_id="client", redirect_uri=redirect_uri, code_verifier=verifier, dpop_jkt="thumb", client_kind="claude")["access_token"]
        public = jwt.algorithms.RSAAlgorithm.from_jwk(self.service.jwks()["keys"][0])
        claims = jwt.decode(token, public, algorithms=["RS256"], audience="renphil-data-mcp", issuer="https://hub.example/mcp/oauth")
        self.assertEqual((claims["email"], claims["sub"]), ("member@renphil.org", "42"))
        self.assertNotIn("roles", claims)
        self.assertEqual(claims["renphil_client_kind"], "claude")
        with self.assertRaises(McpOAuthError):
            self.service.exchange_code(self.db, code=code, client_id="client", redirect_uri=redirect_uri, code_verifier=verifier, dpop_jkt="thumb", client_kind="claude")

    def test_agent_delegated_token_is_identity_only_and_requires_registered_client(self):
        token = self.service.issue_agent_delegated_token(
            email="Member@RenPhil.org", subject="42", client_id="client", model_key="anthropic_claude_haiku_45",
        )["access_token"]
        public = jwt.algorithms.RSAAlgorithm.from_jwk(self.service.jwks()["keys"][0])
        claims = jwt.decode(token, public, algorithms=["RS256"], audience="renphil-data-mcp", issuer="https://hub.example/mcp/oauth")
        self.assertEqual((claims["email"], claims["client_id"]), ("member@renphil.org", "client"))
        self.assertEqual(claims["model_key"], "anthropic_claude_haiku_45")
        self.assertEqual(claims["renphil_client_kind"], "hub_agent")
        self.assertNotIn("roles", claims)
        with self.assertRaises(McpOAuthError):
            self.service.issue_agent_delegated_token(email="member@renphil.org", subject="42", client_id="unknown")

    def test_dynamic_registration_is_disabled_and_not_advertised(self):
        for route in (mcp_oauth_router.register_dynamic_client, mcp_oauth_router.register_dynamic_client_root_alias):
            response = self._resolve_noawait(route())
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"not supported", response.body)
        self.assertFalse(hasattr(self.service, "register_dynamic_client"))
        with patch.object(mcp_oauth_router, "_service", return_value=self.service):
            metadata = self._resolve_noawait(mcp_oauth_router.authorization_server_metadata())
        self.assertNotIn("registration_endpoint", metadata)

    def test_service_refuses_tokens_without_dpop_binding_or_approved_provider(self):
        verifier = "q" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        for dpop_jkt, client_kind in ((None, "claude"), ("", "claude"), ("thumb", None), ("thumb", "hub_agent"), ("thumb", "copilot")):
            request_id = self.service.start_authorization(self.db, client_id="client", redirect_uri="https://client.example/callback", state="s", code_challenge=challenge, code_challenge_method="S256")
            code, _, _ = self.service.complete_authorization(self.db, request_id=request_id, email="member@renphil.org", subject="42")
            with self.subTest(dpop_jkt=dpop_jkt, client_kind=client_kind), self.assertRaises(McpOAuthError):
                self.service.exchange_code(self.db, code=code, client_id="client", redirect_uri="https://client.example/callback", code_verifier=verifier, dpop_jkt=dpop_jkt, client_kind=client_kind)
            # The code was not consumed by the refused request.
            self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
            self.db.query(McpOAuthAuthorizationCodeV2).delete()
            self.db.commit()

    def test_normal_hub_login_never_loads_or_requires_mcp_configuration(self):
        with patch("app.routers.mcp_oauth.get_settings", return_value=SimpleNamespace(MCP_OAUTH_ISSUER=None)):
            self.assertIsNone(mcp_oauth_router.complete_hub_login(
                db=self.db, continuation_uri="https://hub-frontend.example/auth/callback",
                email="member@renphil.org", subject="42",
            ))

    def test_routes_complete_pkce_flow_and_remove_records_after_exchange(self):
        verifier = "z" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self._activate_bot_settings(ENABLED_EXTERNAL)
        with patch.object(mcp_oauth_router, "_service", return_value=self.service), \
                patch("app.routers.mcp_oauth.get_settings", return_value=configured_settings()):
            started = self._resolve_noawait(mcp_oauth_router.authorize(
                response_type="code", client_id="client", redirect_uri="https://client.example/callback",
                state="client-state", code_challenge=challenge, code_challenge_method="S256", db=self.db,
            ))
            continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
            completed = mcp_oauth_router.complete_hub_login(
                db=self.db, continuation_uri=continuation, email="member@renphil.org", subject="42",
            )
            callback = parse_qs(urlsplit(completed).query)
            issued = self._resolve_noawait(mcp_oauth_router.token(
                grant_type="authorization_code", code=callback["code"][0], client_id="client",
                redirect_uri="https://client.example/callback", code_verifier=verifier,
                dpop=dpop_proof("https://hub.example/mcp/oauth/token"), db=self.db,
            ))
        self.assertIn("access_token", issued)
        self.assertEqual(issued["token_type"], "DPoP")
        self.assertEqual(self.db.query(McpOAuthAuthorizationRequestV2).count(), 0)
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)

    def _activate_bot_settings(self, settings: dict) -> None:
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

    def _authorize(self, client_id: str):
        from fastapi import HTTPException
        challenge = base64.urlsafe_b64encode(hashlib.sha256(b"z" * 64).digest()).rstrip(b"=").decode()
        with patch.object(mcp_oauth_router, "_service", return_value=self.service):
            try:
                return self._resolve_noawait(mcp_oauth_router.authorize(
                    response_type="code", client_id=client_id, redirect_uri="https://client.example/callback",
                    state="client-state", code_challenge=challenge, code_challenge_method="S256", db=self.db,
                )).status_code
            except HTTPException as exc:
                return exc.status_code

    def test_external_client_mapping_is_deny_by_default_and_follows_bot_management_toggles(self):
        self._activate_bot_settings({
            "mcp_external_connections_enabled": True,
            "mcp_claude_enabled": True, "mcp_chatgpt_enabled": False, "mcp_gemini_enabled": True,
        })
        configured = configured_settings()
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured):
            # Exact mapping to an enabled provider is authorized.
            self.assertEqual(self._authorize("client"), 307)
            # Mapped to a provider the admin switched off.
            self.assertEqual(self._authorize("gpt-client"), 403)
            # Registered OAuth client with no mapping entry: denied, never inferred.
            self.assertEqual(mcp_oauth_router._external_client_is_enabled(self.db, "unmapped-client"), False)
            self.assertEqual(self._authorize("unmapped-client"), 403)
            # Exact, case-sensitive, Unicode-safe matching only.
            for crafted in ("Client", "CLIENT", "client ", " client", "clïent", "client\u200b", "", "client/extra"):
                self.assertFalse(mcp_oauth_router._external_client_is_enabled(self.db, crafted), crafted)

        # An empty mapping admits nobody (deny by default), even with toggles on.
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured_settings(MCP_OAUTH_CLIENT_PROVIDERS_JSON="{}")):
            self.assertEqual(self._authorize("client"), 403)
        # Invalid mapping (unsupported provider) admits nobody.
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured_settings(MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"client":"copilot"}')):
            self.assertEqual(self._authorize("client"), 403)

        # The master switch blocks every external client regardless of mapping.
        self._activate_bot_settings({"mcp_external_connections_enabled": False, "mcp_claude_enabled": True})
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured):
            self.assertEqual(self._authorize("client"), 403)
        # Defaults: a configuration with no MCP keys keeps every provider off.
        self._activate_bot_settings({"active_model_key": "anthropic_claude_haiku_45"})
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured):
            self.assertEqual(self._authorize("client"), 403)
        # Hub AI's own delegated token path is independent of the external toggle.
        self.assertIn("access_token", self.service.issue_agent_delegated_token(
            email="member@renphil.org", subject="42", client_id="client",
        ))

    def test_token_route_rechecks_toggles_and_requires_a_dpop_proof(self):
        from fastapi import HTTPException
        verifier = "z" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self._activate_bot_settings(ENABLED_EXTERNAL)
        with patch.object(mcp_oauth_router, "_service", return_value=self.service), \
                patch("app.routers.mcp_oauth.get_settings", return_value=configured_settings()):
            started = self._resolve_noawait(mcp_oauth_router.authorize(
                response_type="code", client_id="client", redirect_uri="https://client.example/callback",
                state="s", code_challenge=challenge, code_challenge_method="S256", db=self.db,
            ))
            continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
            code = parse_qs(urlsplit(mcp_oauth_router.complete_hub_login(
                db=self.db, continuation_uri=continuation, email="member@renphil.org", subject="42",
            )).query)["code"][0]
            # Missing proof: rejected, code not consumed, no bearer fallback.
            with self.assertRaises(HTTPException) as missing:
                self._resolve_noawait(mcp_oauth_router.token(
                    grant_type="authorization_code", code=code, client_id="client",
                    redirect_uri="https://client.example/callback", code_verifier=verifier, dpop=None, db=self.db,
                ))
            self.assertEqual(missing.exception.status_code, 400)
            self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
            # Provider switched off between authorize and token: refused.
            self._activate_bot_settings({**ENABLED_EXTERNAL, "mcp_claude_enabled": False})
            with self.assertRaises(HTTPException) as disabled:
                self._resolve_noawait(mcp_oauth_router.token(
                    grant_type="authorization_code", code=code, client_id="client",
                    redirect_uri="https://client.example/callback", code_verifier=verifier,
                    dpop=dpop_proof("https://hub.example/mcp/oauth/token"), db=self.db,
                ))
            self.assertEqual(disabled.exception.status_code, 403)

    def test_agent_token_is_denied_when_hub_ai_is_disabled(self):
        from fastapi import HTTPException
        current = SimpleNamespace(email="member@renphil.org", hub_user_id=42)
        service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"renphil-agent":{"redirect_uris":["https://hub.example/agent"]}}'),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp", signing_key=self.key,
        )
        with patch.object(mcp_oauth_router, "_service", return_value=service), \
                patch("app.routers.mcp_oauth.get_settings", return_value=configured_settings()):
            self._activate_bot_settings({"bot_enabled": True})
            issued = self._resolve_noawait(mcp_oauth_router.agent_token(current=current, model_key=None, db=self.db))
            self.assertIn("access_token", issued)
            self._activate_bot_settings({"bot_enabled": False})
            with self.assertRaises(HTTPException) as caught:
                self._resolve_noawait(mcp_oauth_router.agent_token(current=current, model_key=None, db=self.db))
            self.assertEqual(caught.exception.status_code, 403)

    def test_agent_client_is_not_accepted_by_public_oauth_routes(self):
        from fastapi import HTTPException
        configured = SimpleNamespace(MCP_OAUTH_AGENT_CLIENT_ID="renphil-agent")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(b"z" * 64).digest()).rstrip(b"=").decode()
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured):
            with self.assertRaises(HTTPException) as authorize_error:
                self._resolve_noawait(mcp_oauth_router.authorize(
                    response_type="code", client_id="renphil-agent", redirect_uri="https://client.example/callback",
                    state="state", code_challenge=challenge, code_challenge_method="S256", db=self.db,
                ))
            self.assertEqual(authorize_error.exception.status_code, 400)
            with self.assertRaises(HTTPException) as token_error:
                self._resolve_noawait(mcp_oauth_router.token(
                    grant_type="authorization_code", code="not-used", client_id="renphil-agent",
                    redirect_uri="https://client.example/callback", code_verifier="z" * 64, db=self.db,
                ))
            self.assertEqual(token_error.exception.status_code, 400)

    def test_agent_sync_token_requires_matching_configured_secret(self):
        from fastapi import HTTPException
        with patch("app.services.agent_identity.get_settings", return_value=SimpleNamespace(AGENT_SYNC_TOKEN="shared-token")):
            with self.assertRaises(HTTPException) as missing:
                require_agent_sync_token(None)
            self.assertEqual(missing.exception.status_code, 401)
            self.assertIsNone(require_agent_sync_token("shared-token"))
        with patch("app.services.agent_identity.get_settings", return_value=SimpleNamespace(AGENT_SYNC_TOKEN="")):
            with self.assertRaises(HTTPException) as unavailable:
                require_agent_sync_token("shared-token")
            self.assertEqual(unavailable.exception.status_code, 503)

    def test_agent_client_check_rejects_unicode_without_a_server_error(self):
        configured = SimpleNamespace(MCP_OAUTH_AGENT_CLIENT_ID="renphil-agent")
        with patch("app.routers.mcp_oauth.get_settings", return_value=configured):
            self.assertFalse(mcp_oauth_router._is_agent_client("clïent"))


if __name__ == "__main__":
    unittest.main()


class McpOAuthDPoPTests(unittest.TestCase):
    """RFC 9449 sender-constraining at the Hub token endpoint."""

    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        self.jwk = {
            "kty": "EC", "crv": "P-256",
            "x": jwt.utils.base64url_encode(numbers.x.to_bytes(32, "big")).decode(),
            "y": jwt.utils.base64url_encode(numbers.y.to_bytes(32, "big")).decode(),
        }
        self.token_endpoint = "https://hub.example/mcp/oauth/token"

    def _proof(self, *, htm="POST", htu=None, iat=None, typ="dpop+jwt", jwk=None, key=None):
        import time
        claims = {"jti": "proof-1", "htm": htm, "htu": htu or self.token_endpoint, "iat": int(time.time()) if iat is None else iat}
        return jwt.encode(claims, key or self.private, algorithm="ES256", headers={"typ": typ, "jwk": jwk or self.jwk})

    def test_valid_token_request_proof_yields_rfc7638_thumbprint(self):
        from app.services.mcp_oauth_dpop import jwk_thumbprint, verify_token_request_proof
        proof = verify_token_request_proof(self._proof(), token_endpoint=self.token_endpoint)
        self.assertEqual(proof.jkt, jwk_thumbprint(self.jwk))
        # Query/fragment are ignored for htu, the path is not.
        verify_token_request_proof(self._proof(htu=self.token_endpoint + "?a=1"), token_endpoint=self.token_endpoint)

    def test_invalid_token_request_proofs_are_rejected(self):
        import time
        from cryptography.hazmat.primitives.asymmetric import ec
        from app.services.mcp_oauth_dpop import McpDPoPError, verify_token_request_proof
        other = ec.generate_private_key(ec.SECP256R1())
        cases = {
            "method": self._proof(htm="GET"),
            "url": self._proof(htu="https://hub.example/mcp/oauth/authorize"),
            "stale": self._proof(iat=int(time.time()) - 1000),
            "typ": self._proof(typ="JWT"),
            "forged": self._proof(key=other),
            "private-key": self._proof(jwk=dict(self.jwk, d="AAAA")),
            "empty": "",
        }
        for name, proof in cases.items():
            with self.subTest(name), self.assertRaises(McpDPoPError):
                verify_token_request_proof(proof, token_endpoint=self.token_endpoint)

    def test_persistent_service_binds_token_to_dpop_key_and_returns_dpop_scheme(self):
        from app.services.mcp_oauth_dpop import jwk_thumbprint
        engine = create_engine("sqlite://")
        BaseV2.metadata.create_all(engine, tables=[McpOAuthAuthorizationRequestV2.__table__, McpOAuthAuthorizationCodeV2.__table__])
        db = sessionmaker(bind=engine)()
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"client":{"redirect_uris":["https://client.example/callback"]}}'),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp",
            signing_key=HubMcpSigningKey(private_pem=pem, key_id="k"),
        )
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        request_id = service.start_authorization(db, client_id="client", redirect_uri="https://client.example/callback", state="s", code_challenge=challenge, code_challenge_method="S256")
        code, _, _ = service.complete_authorization(db, request_id=request_id, email="member@renphil.org", subject="42")
        jkt = jwk_thumbprint(self.jwk)
        issued = service.exchange_code(db, code=code, client_id="client", redirect_uri="https://client.example/callback", code_verifier=verifier, dpop_jkt=jkt, client_kind="gemini")
        self.assertEqual(issued["token_type"], "DPoP")
        public = jwt.algorithms.RSAAlgorithm.from_jwk(service.jwks()["keys"][0])
        claims = jwt.decode(issued["access_token"], public, algorithms=["RS256"], audience="renphil-data-mcp", issuer="https://hub.example/mcp/oauth")
        self.assertEqual(claims["cnf"], {"jkt": jkt})
        self.assertEqual(claims["renphil_client_kind"], "gemini")
        # Agent delegation never carries a DPoP binding; it uses the request envelope.
        agent = service.issue_agent_delegated_token(email="member@renphil.org", subject="42", client_id="client")
        self.assertEqual(agent["token_type"], "Bearer")
        self.assertNotIn("cnf", jwt.decode(agent["access_token"], public, algorithms=["RS256"], audience="renphil-data-mcp", issuer="https://hub.example/mcp/oauth"))
        db.close()

    def test_local_service_binds_token_when_a_proof_thumbprint_is_supplied(self):
        service = LocalMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"local-client":{"redirect_uris":["http://127.0.0.1:9876/callback"]}}'),
            issuer="http://127.0.0.1:8000/mcp/oauth", audience="renphil-data-mcp-local",
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes",
        )
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        request_id = service.start_authorization(client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback", state="s", code_challenge=challenge, code_challenge_method="S256")
        code, _, _ = service.complete_authorization(request_id=request_id, email="member@renphil.org", subject="1")
        issued = service.exchange_code(code=code, client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback", code_verifier=verifier, dpop_jkt="thumb", client_kind="chatgpt")
        claims = jwt.decode(issued["access_token"], "local-oauth-signing-secret-with-at-least-thirty-two-bytes", algorithms=["HS256"], audience="renphil-data-mcp-local")
        self.assertEqual((issued["token_type"], claims["cnf"]), ("DPoP", {"jkt": "thumb"}))

    def test_token_route_rejects_a_bad_proof_and_binds_a_good_one(self):
        from fastapi import HTTPException
        from app.services.mcp_oauth_dpop import jwk_thumbprint
        harness = PersistentMcpOAuthServiceTests()
        harness.setUp()
        harness._activate_bot_settings(ENABLED_EXTERNAL)
        try:
            verifier = "z" * 64
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            with patch.object(mcp_oauth_router, "_service", return_value=harness.service), \
                    patch("app.routers.mcp_oauth.get_settings", return_value=configured_settings()):
                for proof, expect_ok in ((self._proof(htm="GET"), False), (self._proof(), True)):
                    started = harness._resolve_noawait(mcp_oauth_router.authorize(
                        response_type="code", client_id="client", redirect_uri="https://client.example/callback",
                        state="client-state", code_challenge=challenge, code_challenge_method="S256", db=harness.db,
                    ))
                    continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
                    completed = mcp_oauth_router.complete_hub_login(db=harness.db, continuation_uri=continuation, email="member@renphil.org", subject="42")
                    code = parse_qs(urlsplit(completed).query)["code"][0]
                    call = lambda: harness._resolve_noawait(mcp_oauth_router.token(
                        grant_type="authorization_code", code=code, client_id="client",
                        redirect_uri="https://client.example/callback", code_verifier=verifier, dpop=proof, db=harness.db,
                    ))
                    if expect_ok:
                        issued = call()
                        self.assertEqual(issued["token_type"], "DPoP")
                        public = jwt.algorithms.RSAAlgorithm.from_jwk(harness.service.jwks()["keys"][0])
                        self.assertEqual(jwt.decode(issued["access_token"], public, algorithms=["RS256"], audience="renphil-data-mcp", issuer="https://hub.example/mcp/oauth")["cnf"]["jkt"], jwk_thumbprint(self.jwk))
                    else:
                        with self.assertRaises(HTTPException) as caught:
                            call()
                        self.assertEqual(caught.exception.status_code, 400)
                        # The code is not consumed by a rejected proof; the row still exists.
                        self.assertEqual(harness.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
                        harness.db.query(McpOAuthAuthorizationCodeV2).delete()
                        harness.db.commit()
                metadata = harness._resolve_noawait(mcp_oauth_router.authorization_server_metadata())
                self.assertIn("ES256", metadata["dpop_signing_alg_values_supported"])
        finally:
            harness.tearDown()
