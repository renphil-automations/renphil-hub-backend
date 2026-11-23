"""RFC 7636 PKCE charset checks in both MCP OAuth service variants.

An S256 ``code_challenge`` is BASE64URL(SHA256(verifier)) without padding,
so it is exactly 43 characters of ``[A-Za-z0-9_-]``; a ``code_verifier`` is
43-128 unreserved characters ``[A-Za-z0-9._~-]``. Malformed values are the
same OAuth error the services already raise for an invalid request, never a
server error, and a refused token request never consumes the code.
"""

from __future__ import annotations

import base64
import hashlib
import string
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.database import BaseV2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from app.services.mcp_oauth_keys import HubMcpSigningKey
from app.services.mcp_oauth_service import LocalMcpOAuthService, McpOAuthError, PersistentMcpOAuthService

LOCAL_REDIRECT = "http://127.0.0.1:9876/callback"
REDIRECT = "https://client.example/callback"
UNRESERVED = string.ascii_letters + string.digits + "-._~"


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


VALID_VERIFIER = "v" * 64
VALID_CHALLENGE = challenge_for(VALID_VERIFIER)

MALFORMED_CHALLENGES = (
    VALID_CHALLENGE[:42],                 # too short
    VALID_CHALLENGE + "A",                # 44: longer than any S256 output
    "A" * 64,                             # was accepted by the old 43-128 length check
    "A" * 128,
    VALID_CHALLENGE[:42] + "=",           # padding is not base64url-without-padding
    VALID_CHALLENGE[:42] + "+",           # standard base64 alphabet
    VALID_CHALLENGE[:42] + "/",
    VALID_CHALLENGE[:42] + ".",           # verifier-only character
    VALID_CHALLENGE[:42] + "é",      # non-ASCII
    VALID_CHALLENGE[:42] + "١",      # Unicode digit (\d would accept it)
    VALID_CHALLENGE + "\n",               # trailing newline ($ would accept it)
    " " + VALID_CHALLENGE[:42],
)

MALFORMED_VERIFIERS = (
    "v" * 42,                             # too short
    "v" * 129,                            # too long
    "v" * 63 + "+",
    "v" * 63 + "/",
    "v" * 63 + "=",
    "v" * 63 + " ",
    "v" * 63 + "é",                  # non-ASCII: used to raise UnicodeEncodeError
    "é" * 64,
    "v" * 63 + "١",
    "v" * 64 + "\n",
)

VALID_VERIFIERS = (
    "v" * 43,
    "v" * 128,
    (UNRESERVED * 2)[:128],
    "a-b.c_d~" * 6,
)


class LocalServicePkceCharsetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LocalMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"local-client":{"redirect_uris":["%s"]}}' % LOCAL_REDIRECT),
            issuer="http://127.0.0.1:8000/mcp/oauth",
            audience="renphil-data-mcp-local",
            signing_secret="local-oauth-signing-secret-with-at-least-thirty-two-bytes",
        )

    def _start(self, challenge: str) -> str:
        return self.service.start_authorization(
            client_id="local-client", redirect_uri=LOCAL_REDIRECT, state="s",
            code_challenge=challenge, code_challenge_method="S256",
        )

    def _code(self, verifier: str) -> str:
        code, _, _ = self.service.complete_authorization(request_id=self._start(challenge_for(verifier)), email="member@renphil.org", subject="1")
        return code

    def _exchange(self, code: str, verifier: str) -> dict:
        return self.service.exchange_code(
            code=code, client_id="local-client", redirect_uri=LOCAL_REDIRECT, code_verifier=verifier,
            dpop_jkt="thumb", client_kind="claude",
        )

    def test_malformed_code_challenge_is_an_invalid_request(self):
        for challenge in MALFORMED_CHALLENGES:
            with self.subTest(challenge=challenge), self.assertRaises(McpOAuthError):
                self._start(challenge)
        self.assertEqual(self.service._pending, {})

    def test_plain_method_is_still_refused_with_a_well_formed_challenge(self):
        with self.assertRaises(McpOAuthError):
            self.service.start_authorization(
                client_id="local-client", redirect_uri=LOCAL_REDIRECT, state="s",
                code_challenge=VALID_CHALLENGE, code_challenge_method="plain",
            )

    def test_malformed_verifier_is_refused_without_consuming_the_code(self):
        code = self._code(VALID_VERIFIER)
        for verifier in MALFORMED_VERIFIERS:
            with self.subTest(verifier=verifier), self.assertRaises(McpOAuthError):
                self._exchange(code, verifier)
        self.assertIn("access_token", self._exchange(code, VALID_VERIFIER))

    def test_rfc7636_boundary_verifiers_still_complete_the_flow(self):
        for verifier in VALID_VERIFIERS:
            with self.subTest(verifier=verifier):
                self.assertIn("access_token", self._exchange(self._code(verifier), verifier))


class PersistentServicePkceCharsetTests(unittest.TestCase):
    @staticmethod
    def _resolve_noawait(coroutine):
        if not hasattr(coroutine, "send"):
            return coroutine  # plain ``def`` handler (threadpool)
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        raise AssertionError("OAuth route unexpectedly awaited I/O")

    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        BaseV2.metadata.create_all(engine, tables=[
            McpOAuthAuthorizationRequestV2.__table__, McpOAuthAuthorizationCodeV2.__table__,
            BotConfigurationVersionV2.__table__, BotConfigurationStateV2.__table__,
        ])
        self.db = sessionmaker(bind=engine)()
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.service = PersistentMcpOAuthService(
            clients=LocalMcpOAuthService.parse_clients('{"client":{"redirect_uris":["%s"]}}' % REDIRECT),
            issuer="https://hub.example/mcp/oauth", audience="renphil-data-mcp",
            signing_key=HubMcpSigningKey(private_pem=pem, key_id="test-key"),
        )

    def tearDown(self) -> None:
        self.db.close()

    def _start(self, challenge: str) -> str:
        return self.service.start_authorization(
            self.db, client_id="client", redirect_uri=REDIRECT, state="s",
            code_challenge=challenge, code_challenge_method="S256",
        )

    def _code(self, verifier: str) -> str:
        code, _, _ = self.service.complete_authorization(self.db, request_id=self._start(challenge_for(verifier)), email="member@renphil.org", subject="42")
        return code

    def _exchange(self, code: str, verifier: str) -> dict:
        return self.service.exchange_code(
            self.db, code=code, client_id="client", redirect_uri=REDIRECT, code_verifier=verifier,
            dpop_jkt="thumb", client_kind="claude",
        )

    def test_malformed_code_challenge_is_an_invalid_request_and_stores_nothing(self):
        for challenge in MALFORMED_CHALLENGES:
            with self.subTest(challenge=challenge), self.assertRaises(McpOAuthError) as caught:
                self._start(challenge)
            self.assertEqual(str(caught.exception), "OAuth authorization request is invalid.")
        self.assertEqual(self.db.query(McpOAuthAuthorizationRequestV2).count(), 0)

    def test_malformed_verifier_is_an_invalid_request_and_keeps_the_code(self):
        code = self._code(VALID_VERIFIER)
        for verifier in MALFORMED_VERIFIERS:
            with self.subTest(verifier=verifier), self.assertRaises(McpOAuthError) as caught:
                self._exchange(code, verifier)
            self.assertEqual(str(caught.exception), "OAuth token request is invalid.")
            self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        self.assertIn("access_token", self._exchange(code, VALID_VERIFIER))
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 0)

    def test_rfc7636_boundary_verifiers_still_complete_the_flow(self):
        for verifier in VALID_VERIFIERS:
            with self.subTest(verifier=verifier):
                self.assertIn("access_token", self._exchange(self._code(verifier), verifier))

    def test_routes_answer_malformed_pkce_with_the_existing_400(self):
        with patch.object(mcp_oauth_router, "_service", return_value=self.service), \
                patch.object(mcp_oauth_router, "_external_client_is_enabled", return_value=True):
            with self.assertRaises(HTTPException) as caught:
                self._resolve_noawait(mcp_oauth_router.authorize(
                    response_type="code", client_id="client", redirect_uri=REDIRECT, state="s",
                    code_challenge="A" * 64, code_challenge_method="S256", db=self.db,
                ))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail["error"], "invalid_request")

        code = self._code(VALID_VERIFIER)
        admitted = type("Decision", (), {"allowed": True, "client_kind": "claude"})()
        with patch.object(mcp_oauth_router, "_service", return_value=self.service), \
                patch.object(mcp_oauth_router, "_external_admission", return_value=admitted), \
                patch.object(mcp_oauth_router, "verify_token_request_proof", return_value=type("P", (), {"jkt": "thumb"})()):
            for verifier in ("v" * 63 + "é", "v" * 42):
                with self.subTest(verifier=verifier), self.assertRaises(HTTPException) as caught:
                    self._resolve_noawait(mcp_oauth_router.token(
                        grant_type="authorization_code", code=code, client_id="client", redirect_uri=REDIRECT,
                        code_verifier=verifier, dpop="proof", db=self.db,
                    ))
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(caught.exception.detail["error"], "invalid_grant")
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)


if __name__ == "__main__":
    unittest.main()
