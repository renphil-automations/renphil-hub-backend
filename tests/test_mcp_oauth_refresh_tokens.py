"""H2: rotating refresh tokens with reuse detection for external MCP clients."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import unittest
from datetime import datetime, timedelta, timezone

import jwt

from app.db_v2.database import BaseV2
from app.db_v2.models.mcp_oauth import McpOAuthRefreshTokenV2
from app.db_v2.models.user import UserV2
from app.services.mcp_oauth_client_secret import hash_client_secret
from app.services.mcp_oauth_service import (
    LocalMcpOAuthService,
    McpOAuthDPoPBindingError,
    McpOAuthInvalidGrant,
    refresh_lifetimes,
)
from tests.test_mcp_oauth_dpop_replay import Signer
from tests.test_mcp_oauth_rate_limit import (
    CLAUDE_CLIENT, ENABLED_EXTERNAL, GPT_CLIENT, ISSUER, REDIRECT, _Base, mcp_settings,
)
from app.services.mcp_oauth_dpop import jwk_thumbprint

USER_EMAIL = "member.person@renphil.org"


class RefreshDisabledTests(_Base):
    """Default configuration: no refresh tokens and the table is never touched."""

    def test_no_refresh_token_is_issued_or_accepted_and_none_is_advertised(self):
        http = self.client()
        signer = Signer()
        code = self.issue_code(http)
        issued = http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": signer.proof()})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertNotIn("refresh_token", issued.json())
        refused = http.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": "x" * 64, "client_id": CLAUDE_CLIENT,
        }, headers={"DPoP": signer.proof()})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json(), {"detail": "Unsupported grant type."})
        metadata = http.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["grant_types_supported"], ["authorization_code"])


class RefreshEnabledTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        BaseV2.metadata.create_all(self.engine, tables=[McpOAuthRefreshTokenV2.__table__, UserV2.__table__])
        self.db.add(UserV2(id=42, name="Member Person", work_email=USER_EMAIL, has_signed_in=True))
        self.db.commit()
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True)
        self.service.refresh_tokens_enabled = True
        self.signer = Signer()

    # -- helpers -----------------------------------------------------------
    def _login(self, http, *, signer=None, client_id=CLAUDE_CLIENT, extra=None, headers=None):
        code = self.issue_code(http)
        dpop = {} if signer is False else {"DPoP": (signer or self.signer).proof()}
        response = http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64, **(extra or {}),
        }, headers={**dpop, **(headers or {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _refresh(self, http, token, *, signer=None, client_id=CLAUDE_CLIENT, extra=None):
        headers = {} if signer is False else {"DPoP": (signer or self.signer).proof()}
        return http.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": token, "client_id": client_id, **(extra or {}),
        }, headers=headers)

    def _claims(self, access_token: str) -> dict:
        public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(self.service.jwks()["keys"][0]))
        return jwt.decode(access_token, public, algorithms=["RS256"], audience="renphil-data-mcp", issuer=ISSUER)

    def _rows(self):
        self.db.expire_all()
        return self.db.query(McpOAuthRefreshTokenV2).order_by(McpOAuthRefreshTokenV2.id).all()

    # -- issuance ----------------------------------------------------------
    def test_code_exchange_returns_an_opaque_refresh_token_stored_only_as_a_hash(self):
        http = self.client()
        issued = self._login(http)
        token = issued["refresh_token"]
        self.assertGreaterEqual(len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))), 32)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps([str(value) for value in row.__dict__.values()]))
        self.assertEqual((row.client_id, row.user_email, row.user_subject, row.scope), (CLAUDE_CLIENT, USER_EMAIL, "42", "renphil.mcp.read"))
        self.assertEqual(row.jkt, jwk_thumbprint(self.signer.jwk))
        self.assertIsNone(row.used_at)
        self.assertIsNone(row.revoked_at)
        self.assertEqual(round((row.expires_at - row.created_at).total_seconds()), 7 * 24 * 3600)
        self.assertEqual(round((row.family_expires_at - row.created_at).total_seconds()), 30 * 24 * 3600)
        metadata = http.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["grant_types_supported"], ["authorization_code", "refresh_token"])

    # -- rotation and reuse detection ---------------------------------------
    def test_rotation_issues_a_new_pair_in_the_same_family(self):
        http = self.client()
        first = self._login(http)["refresh_token"]
        rotated = self._refresh(http, first)
        self.assertEqual(rotated.status_code, 200, rotated.text)
        body = rotated.json()
        self.assertEqual(body["token_type"], "DPoP")
        self.assertNotEqual(body["refresh_token"], first)
        claims = self._claims(body["access_token"])
        self.assertEqual(claims["cnf"], {"jkt": jwk_thumbprint(self.signer.jwk)})
        self.assertEqual((claims["sub"], claims["email"], claims["renphil_client_kind"]), ("42", USER_EMAIL, "claude"))
        old, new = self._rows()
        self.assertEqual(old.family_id, new.family_id)
        self.assertIsNotNone(old.used_at)
        self.assertIsNone(new.used_at)
        self.assertEqual(new.family_expires_at, old.family_expires_at)
        self.assertEqual(self._refresh(http, body["refresh_token"]).status_code, 200)

    def test_reusing_a_rotated_token_revokes_the_whole_family(self):
        http = self.client()
        first = self._login(http)["refresh_token"]
        second = self._refresh(http, first).json()["refresh_token"]
        reuse = self._refresh(http, first)
        self.assertEqual(reuse.status_code, 400)
        self.assertEqual(reuse.json()["detail"]["error"], "invalid_grant")
        self.assertTrue(all(row.revoked_at is not None for row in self._rows()))
        # The legitimate holder's newest token is dead too.
        self.assertEqual(self._refresh(http, second).json()["detail"]["error"], "invalid_grant")

    def test_other_families_are_untouched_by_a_revocation(self):
        http = self.client()
        victim = self._login(http)["refresh_token"]
        other = self._login(http)["refresh_token"]
        self._refresh(http, victim)
        self._refresh(http, victim)  # reuse -> revoke the victim's family
        self.assertEqual(self._refresh(http, other).status_code, 200)

    def test_unknown_or_expired_tokens_are_invalid_grant(self):
        http = self.client()
        self.assertEqual(self._refresh(http, "not-a-real-token").json()["detail"]["error"], "invalid_grant")
        token = self._login(http)["refresh_token"]
        row = self._rows()[0]
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self._refresh(http, token).json()["detail"]["error"], "invalid_grant")
        family_expired = self._login(http)["refresh_token"]
        row = self._rows()[-1]
        row.family_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self._refresh(http, family_expired).json()["detail"]["error"], "invalid_grant")

    # -- DPoP binding --------------------------------------------------------
    def test_a_bound_family_needs_a_proof_for_the_same_key_and_is_not_consumed_without_it(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        for signer in (False, Signer()):
            with self.subTest(signer="missing" if signer is False else "other-key"):
                refused = self._refresh(http, token, signer=signer)
                self.assertEqual(refused.status_code, 400)
                self.assertEqual(refused.json()["detail"]["error"], "invalid_dpop_proof")
        self.assertIsNone(self._rows()[0].used_at)
        self.assertEqual(self._refresh(http, token).status_code, 200)

    def test_unbound_family_for_a_bearer_provider(self):
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_BEARER_PROVIDERS="claude")
        http = self.client()
        token = self._login(http, signer=False)["refresh_token"]
        self.assertIsNone(self._rows()[0].jkt)
        rotated = self._refresh(http, token, signer=False)
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json()["token_type"], "Bearer")
        self.assertNotIn("cnf", self._claims(rotated.json()["access_token"]))
        # A proof that is sent binds the new access token (never downgraded).
        bound = self._refresh(http, rotated.json()["refresh_token"])
        self.assertEqual(bound.json()["token_type"], "DPoP")
        # Provider removed from the bearer set: an unbound family needs DPoP.
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True)
        refused = self._refresh(http, bound.json()["refresh_token"], signer=False)
        self.assertEqual(refused.json()["detail"]["error"], "invalid_dpop_proof")

    # -- live re-checks ------------------------------------------------------
    def test_switching_the_provider_off_refuses_and_revokes_the_family(self):
        http = self.client()
        for switches in ({**ENABLED_EXTERNAL, "mcp_claude_enabled": False}, {**ENABLED_EXTERNAL, "mcp_external_connections_enabled": False}):
            with self.subTest(switches=switches):
                self._activate(ENABLED_EXTERNAL)
                token = self._login(http)["refresh_token"]
                self._activate(switches)
                self.assertEqual(self._refresh(http, token).status_code, 403)
                # Switching back on does not resurrect the revoked family.
                self._activate(ENABLED_EXTERNAL)
                self.assertEqual(self._refresh(http, token).json()["detail"]["error"], "invalid_grant")

    def test_removing_the_client_mapping_revokes_the_family(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_CLIENT_PROVIDERS_JSON=json.dumps({GPT_CLIENT: "chatgpt"}))
        self.assertEqual(self._refresh(http, token).status_code, 403)
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True)
        self.assertEqual(self._refresh(http, token).json()["detail"]["error"], "invalid_grant")

    def test_soft_deleted_or_missing_user_revokes_the_family(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        user = self.db.get(UserV2, 42)
        user.is_deleted = True
        self.db.commit()
        self.assertEqual(self._refresh(http, token).json()["detail"]["error"], "invalid_grant")
        user.is_deleted = False
        self.db.commit()
        self.assertEqual(self._refresh(http, token).json()["detail"]["error"], "invalid_grant")
        self.assertTrue(all(row.revoked_at is not None for row in self._rows()))

    def test_another_client_presenting_the_token_revokes_the_family(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        stolen = self._refresh(http, token, client_id=GPT_CLIENT)
        self.assertEqual(stolen.json()["detail"]["error"], "invalid_grant")
        self.assertEqual(self._refresh(http, token).json()["detail"]["error"], "invalid_grant")

    def test_confidential_client_must_authenticate_to_refresh(self):
        registry = json.loads(self.settings.MCP_OAUTH_CLIENTS_JSON)
        registry[CLAUDE_CLIENT]["client_secret_hash"] = hash_client_secret("s3cret-value", iterations=100_000)
        self.settings = mcp_settings(MCP_OAUTH_REFRESH_TOKENS_ENABLED=True, MCP_OAUTH_CLIENTS_JSON=json.dumps(registry))
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)
        http = self.client()
        token = self._login(http, extra={"client_secret": "s3cret-value"})["refresh_token"]
        refused = self._refresh(http, token)
        self.assertEqual(refused.status_code, 401)
        self.assertIsNone(self._rows()[0].used_at)
        self.assertEqual(self._refresh(http, token, extra={"client_secret": "s3cret-value"}).status_code, 200)

    def test_only_the_mcp_scope_may_be_requested(self):
        http = self.client()
        token = self._login(http)["refresh_token"]
        refused = self._refresh(http, token, extra={"scope": "renphil.mcp.read admin"})
        self.assertEqual(refused.json()["detail"]["error"], "invalid_scope")
        self.assertEqual(self._refresh(http, token, extra={"scope": "renphil.mcp.read"}).status_code, 200)


class LocalServiceRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LocalMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"local-client":{"redirect_uris":["http://127.0.0.1:9876/callback"]}}'),
            issuer="http://127.0.0.1:8000/mcp/oauth", audience="renphil-data-mcp-local",
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes",
            refresh_tokens_enabled=True, refresh_token_idle_seconds=600, refresh_family_max_seconds=7200,
        )

    def _login(self, jkt="thumb"):
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        request_id = self.service.start_authorization(client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback", state="s", code_challenge=challenge, code_challenge_method="S256")
        code, _, _ = self.service.complete_authorization(request_id=request_id, email="member@renphil.org", subject="1")
        return self.service.exchange_code(code=code, client_id="local-client", redirect_uri="http://127.0.0.1:9876/callback", code_verifier=verifier, dpop_jkt=jkt, client_kind="claude")

    def _refresh(self, token, **kwargs):
        options = dict(client_id="local-client", client_kind="claude", dpop_jkt="thumb")
        options.update(kwargs)
        return self.service.refresh_access_token(refresh_token=token, **options)

    def test_rotation_reuse_detection_and_binding(self):
        first = self._login()["refresh_token"]
        with self.assertRaises(McpOAuthDPoPBindingError):
            self._refresh(first, dpop_jkt=None)
        with self.assertRaises(McpOAuthDPoPBindingError):
            self._refresh(first, dpop_jkt="other")
        second = self._refresh(first)
        self.assertEqual(second["token_type"], "DPoP")
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(first)  # reuse -> family revoked
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(second["refresh_token"])

    def test_inactive_user_or_unapproved_kind_revokes(self):
        token = self._login()["refresh_token"]
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(token, user_is_active=lambda subject, email: False)
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(token)
        other = self._login()["refresh_token"]
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(other, client_kind="hub_agent")
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(other)

    def test_disabled_service_issues_and_accepts_nothing(self):
        self.service.refresh_tokens_enabled = False
        self.assertNotIn("refresh_token", self._login())
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh("anything")

    def test_expiry_is_enforced(self):
        token = self._login()["refresh_token"]
        record = next(iter(self.service._refresh.values()))
        record.expires_at = time.time() - 1
        with self.assertRaises(McpOAuthInvalidGrant):
            self._refresh(token)


class RefreshLifetimeBoundsTests(unittest.TestCase):
    def test_lifetimes_are_clamped(self):
        self.assertEqual(refresh_lifetimes(7 * 86400, 30 * 86400), (7 * 86400, 30 * 86400))
        self.assertEqual(refresh_lifetimes(1, 1), (300, 3600))
        self.assertEqual(refresh_lifetimes(10**9, 10**9), (30 * 86400, 90 * 86400))
        self.assertEqual(refresh_lifetimes(20 * 86400, 2 * 86400), (2 * 86400, 2 * 86400))

    def test_settings_reject_out_of_range_values(self):
        from pydantic import ValidationError

        from app.config import Settings

        defaults = Settings(_env_file=None)
        self.assertFalse(defaults.MCP_OAUTH_REFRESH_TOKENS_ENABLED)
        self.assertEqual((defaults.MCP_OAUTH_REFRESH_TOKEN_IDLE_SECONDS, defaults.MCP_OAUTH_REFRESH_FAMILY_MAX_SECONDS), (604800, 2592000))
        for overrides in ({"MCP_OAUTH_REFRESH_TOKEN_IDLE_SECONDS": 10}, {"MCP_OAUTH_REFRESH_FAMILY_MAX_SECONDS": 10**9}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                Settings(_env_file=None, **overrides)


if __name__ == "__main__":
    unittest.main()
