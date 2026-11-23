"""K-3: a token-signing failure never burns a valid code or refresh token."""

from __future__ import annotations

import base64
import hashlib
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_v2.database import BaseV2
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2, McpOAuthRefreshTokenV2
from app.services import mcp_oauth_service as service_module
from app.services.mcp_oauth_keys import HubMcpSigningKey
from app.services.mcp_oauth_service import LocalMcpOAuthService, McpOAuthError, PersistentMcpOAuthService
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ISSUER, REDIRECT, _Base, dpop_proof

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class SigningDown(RuntimeError):
    pass


class PersistentIssueOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        BaseV2.metadata.create_all(engine, tables=[
            McpOAuthAuthorizationRequestV2.__table__, McpOAuthAuthorizationCodeV2.__table__, McpOAuthRefreshTokenV2.__table__,
        ])
        self.db = sessionmaker(bind=engine)()
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ).decode()
        self.service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"client":{"redirect_uris":["https://client.example/callback"]}}'),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp",
            signing_key=HubMcpSigningKey(private_pem=pem, key_id="k"), refresh_tokens_enabled=True,
        )
        self.addCleanup(self.db.close)

    def _code(self) -> str:
        request_id = self.service.start_authorization(self.db, client_id="client", redirect_uri="https://client.example/callback", state="s", code_challenge=CHALLENGE, code_challenge_method="S256")
        return self.service.complete_authorization(self.db, request_id=request_id, email="m@renphil.org", subject="42")[0]

    def _exchange(self, code):
        return self.service.exchange_code(self.db, code=code, client_id="client", redirect_uri="https://client.example/callback",
                                          code_verifier=VERIFIER, dpop_jkt="thumb", client_kind="claude")

    def test_signing_failure_keeps_the_code_and_creates_no_refresh_family(self):
        code = self._code()
        with patch.object(self.service._signing_key, "sign", side_effect=SigningDown("hsm down")), \
                self.assertRaises(McpOAuthError):
            self._exchange(code)
        self.db.expire_all()
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        self.assertEqual(self.db.query(McpOAuthRefreshTokenV2).count(), 0)
        issued = self._exchange(code)  # the same code still works
        self.assertIn("access_token", issued)

    def test_signing_failure_during_refresh_keeps_the_presented_token_usable(self):
        refresh_token = self._exchange(self._code())["refresh_token"]
        with patch.object(self.service._signing_key, "sign", side_effect=SigningDown("hsm down")), \
                self.assertRaises(McpOAuthError):
            self.service.refresh_access_token(self.db, refresh_token=refresh_token, client_id="client", client_kind="claude", dpop_jkt="thumb")
        self.db.expire_all()
        rows = self.db.query(McpOAuthRefreshTokenV2).all()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].used_at)
        self.assertIsNone(rows[0].revoked_at)
        rotated = self.service.refresh_access_token(self.db, refresh_token=refresh_token, client_id="client", client_kind="claude", dpop_jkt="thumb")
        self.assertIn("refresh_token", rotated)


class LocalIssueOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LocalMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"c":{"redirect_uris":["http://127.0.0.1:9876/cb"]}}'),
            issuer="http://127.0.0.1:8000/mcp/oauth", audience="aud",
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes", refresh_tokens_enabled=True,
        )

    def _code(self):
        request_id = self.service.start_authorization(client_id="c", redirect_uri="http://127.0.0.1:9876/cb", state="s", code_challenge=CHALLENGE, code_challenge_method="S256")
        return self.service.complete_authorization(request_id=request_id, email="m@renphil.org", subject="1")[0]

    def _exchange(self, code):
        return self.service.exchange_code(code=code, client_id="c", redirect_uri="http://127.0.0.1:9876/cb", code_verifier=VERIFIER, dpop_jkt="thumb", client_kind="claude")

    def test_signing_failure_keeps_code_and_refresh_token(self):
        code = self._code()
        with patch.object(service_module.jwt, "encode", side_effect=SigningDown("down")), self.assertRaises(SigningDown):
            self._exchange(code)
        self.assertEqual(self.service._refresh, {})
        refresh_token = self._exchange(code)["refresh_token"]
        with patch.object(service_module.jwt, "encode", side_effect=SigningDown("down")), self.assertRaises(SigningDown):
            self.service.refresh_access_token(refresh_token=refresh_token, client_id="c", client_kind="claude", dpop_jkt="thumb")
        self.assertIn("refresh_token", self.service.refresh_access_token(refresh_token=refresh_token, client_id="c", client_kind="claude", dpop_jkt="thumb"))


class TokenRouteIssueOrderTests(_Base):
    def test_signing_failure_is_503_and_the_code_survives(self):
        http = self.client()
        code = self.issue_code(http)
        form = {"grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT, "redirect_uri": REDIRECT, "code_verifier": VERIFIER}
        with patch.object(self.service._signing_key, "sign", side_effect=SigningDown("down")):
            failed = http.post("/mcp/oauth/token", data=form, headers={"DPoP": dpop_proof(f"{ISSUER}/token")})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.json()["error"], "temporarily_unavailable")
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        self.assertEqual(http.post("/mcp/oauth/token", data=form, headers={"DPoP": dpop_proof(f"{ISSUER}/token")}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
