"""H3: confidential MCP OAuth clients (client_secret_basic / client_secret_post)."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import quote_plus

from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2
from app.services.mcp_oauth_client_secret import (
    ClientSecretHashError,
    hash_client_secret,
    parse_client_secret_hash,
    verify_client_secret,
)
from app.services.mcp_oauth_service import LocalMcpOAuthService, McpOAuthError, authenticate_client
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, GPT_CLIENT, ISSUER, REDIRECT, _Base, dpop_proof, mcp_settings

SECRET = "gemini-enterprise-secret:with/odd+chars"
FAST = 100_000  # the minimum allowed; keeps the suite quick
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def basic(client_id: str, secret: str) -> str:
    raw = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class ClientSecretHashTests(unittest.TestCase):
    def test_round_trip_and_mismatch(self):
        encoded = hash_client_secret(SECRET, iterations=FAST)
        self.assertTrue(encoded.startswith("pbkdf2_sha256$100000$"))
        self.assertNotIn(SECRET, encoded)
        self.assertTrue(verify_client_secret(SECRET, encoded))
        for wrong in ("", None, SECRET + " ", SECRET.upper(), "x" * 600, 42):
            with self.subTest(wrong=wrong):
                self.assertFalse(verify_client_secret(wrong, encoded))

    def test_salt_is_random(self):
        self.assertNotEqual(hash_client_secret(SECRET, iterations=FAST), hash_client_secret(SECRET, iterations=FAST))

    def test_malformed_or_weak_hashes_are_rejected(self):
        good = hash_client_secret(SECRET, iterations=FAST)
        _, iterations, salt, digest = good.split("$")
        for bad in (
            "", "plain-text-secret", f"pbkdf2_sha1${iterations}${salt}${digest}",
            f"pbkdf2_sha256$1000${salt}${digest}", f"pbkdf2_sha256$99999999${salt}${digest}",
            f"pbkdf2_sha256${iterations}$AAAA${digest}", f"pbkdf2_sha256${iterations}${salt}$AAAA",
            f"pbkdf2_sha256${iterations}${salt}${digest}=", f"pbkdf2_sha256$１００００0${salt}${digest}",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ClientSecretHashError):
                    parse_client_secret_hash(bad)
                self.assertFalse(verify_client_secret(SECRET, bad))

    def test_registry_rejects_an_invalid_secret_hash(self):
        with self.assertRaises(McpOAuthError):
            LocalMcpOAuthService.parse_clients(json.dumps({"c": {"redirect_uris": [REDIRECT], "client_secret_hash": "nope"}}))
        clients = LocalMcpOAuthService.parse_clients(json.dumps({
            "conf": {"redirect_uris": [REDIRECT], "client_secret_hash": hash_client_secret(SECRET, iterations=FAST)},
            "pub": {"redirect_uris": [REDIRECT]},
        }))
        self.assertTrue(clients["conf"].is_confidential)
        self.assertTrue(authenticate_client(clients, "conf", SECRET))
        self.assertFalse(authenticate_client(clients, "conf", None))
        self.assertTrue(authenticate_client(clients, "pub", None))
        self.assertFalse(authenticate_client(clients, "pub", "anything"))
        self.assertFalse(authenticate_client(clients, "unknown", None))

    def test_helper_script_reads_stdin_and_prints_only_the_hash(self):
        result = subprocess.run(
            [sys.executable, str(BACKEND_ROOT / "scripts" / "hash_mcp_client_secret.py")],
            input=SECRET + "\n", capture_output=True, text=True, timeout=60, cwd=str(BACKEND_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        encoded = result.stdout.strip()
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertTrue(verify_client_secret(SECRET, encoded))
        empty = subprocess.run(
            [sys.executable, str(BACKEND_ROOT / "scripts" / "hash_mcp_client_secret.py")],
            input="\n", capture_output=True, text=True, timeout=60, cwd=str(BACKEND_ROOT),
        )
        self.assertEqual(empty.returncode, 2)
        self.assertEqual(empty.stdout, "")


class ClientSecretCostTests(unittest.TestCase):
    """S9: high-entropy client secrets need no slow KDF; verification cost is capped."""

    def test_new_hashes_use_about_ten_thousand_iterations(self):
        from app.services import mcp_oauth_client_secret as module

        self.assertEqual(module.DEFAULT_ITERATIONS, 10_000)
        self.assertTrue(hash_client_secret(SECRET).startswith("pbkdf2_sha256$10000$"))

    def test_existing_600k_hashes_still_verify_but_the_cap_bounds_cpu(self):
        import base64 as b64
        import hashlib as hl

        salt = b"0123456789abcdef"
        digest = hl.pbkdf2_hmac("sha256", SECRET.encode(), salt, 600_000, dklen=32)
        encode = lambda raw: b64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
        legacy = f"pbkdf2_sha256$600000${encode(salt)}${encode(digest)}"
        self.assertTrue(verify_client_secret(SECRET, legacy))
        too_slow = f"pbkdf2_sha256$1000000${encode(salt)}${encode(digest)}"
        with self.assertRaises(ClientSecretHashError):
            parse_client_secret_hash(too_slow)
        with self.assertRaises(ClientSecretHashError):
            hash_client_secret(SECRET, iterations=1_000_000)

    def test_helper_script_refuses_short_secrets(self):
        result = subprocess.run(
            [sys.executable, str(BACKEND_ROOT / "scripts" / "hash_mcp_client_secret.py")],
            input="short-secret\n", capture_output=True, text=True, timeout=60, cwd=str(BACKEND_ROOT),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("short-secret", result.stderr)


class ConfidentialClientTokenRouteTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        registry = json.loads(self.settings.MCP_OAUTH_CLIENTS_JSON)
        registry[CLAUDE_CLIENT]["client_secret_hash"] = hash_client_secret(SECRET, iterations=FAST)
        self.settings = mcp_settings(MCP_OAUTH_CLIENTS_JSON=json.dumps(registry))
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)

    def _token(self, http, code, *, form_extra=None, headers=None, include_client_id=True):
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": "v" * 64}
        if include_client_id:
            data["client_id"] = CLAUDE_CLIENT
        data.update(form_extra or {})
        return http.post("/mcp/oauth/token", data=data, headers={"DPoP": dpop_proof(f"{ISSUER}/token"), **(headers or {})})

    def test_client_secret_basic_is_accepted(self):
        http = self.client()
        issued = self._token(http, self.issue_code(http), headers={"Authorization": basic(CLAUDE_CLIENT, SECRET)}, include_client_id=False)
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertIn("access_token", issued.json())

    def test_client_secret_post_is_accepted(self):
        http = self.client()
        issued = self._token(http, self.issue_code(http), form_extra={"client_secret": SECRET})
        self.assertEqual(issued.status_code, 200, issued.text)

    def test_missing_wrong_or_ambiguous_credentials_are_invalid_client_and_keep_the_code(self):
        http = self.client()
        code = self.issue_code(http)
        cases = {
            "missing": {},
            "wrong-post": {"form_extra": {"client_secret": SECRET + "x"}},
            "empty-post": {"form_extra": {"client_secret": ""}},
            "wrong-basic": {"headers": {"Authorization": basic(CLAUDE_CLIENT, "nope")}},
            "malformed-basic": {"headers": {"Authorization": "Basic !!!not-base64"}},
            "basic-without-secret": {"headers": {"Authorization": "Basic " + base64.b64encode(CLAUDE_CLIENT.encode()).decode()}},
            "two-methods": {"headers": {"Authorization": basic(CLAUDE_CLIENT, SECRET)}, "form_extra": {"client_secret": SECRET}},
            "basic-id-mismatch": {"headers": {"Authorization": basic(GPT_CLIENT, SECRET)}},
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                refused = self._token(http, code, **kwargs)
                self.assertEqual(refused.status_code, 401, refused.text)
                self.assertEqual(refused.json()["error"], "invalid_client")
                self.assertIn("Basic", refused.headers["www-authenticate"])
                self.assertNotIn(SECRET, refused.text)
        self.assertEqual(self.db.query(McpOAuthAuthorizationCodeV2).count(), 1)
        self.assertEqual(self._token(http, code, form_extra={"client_secret": SECRET}).status_code, 200)

    def test_public_clients_keep_pkce_only_and_may_not_send_a_secret(self):
        self.settings = mcp_settings()
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)
        http = self.client()
        code = self.issue_code(http)
        refused = self._token(http, code, form_extra={"client_secret": "unexpected"})
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(self._token(http, code).status_code, 200)

    def test_metadata_advertises_the_supported_methods(self):
        http = self.client()
        methods = http.get("/.well-known/oauth-authorization-server").json()["token_endpoint_auth_methods_supported"]
        # The ChatGPT client is public, Claude is confidential.
        self.assertEqual(methods, ["none", "client_secret_basic", "client_secret_post"])
        self.settings = mcp_settings()
        self.service.clients = LocalMcpOAuthService.parse_clients(self.settings.MCP_OAUTH_CLIENTS_JSON)
        self.assertEqual(http.get("/.well-known/oauth-authorization-server").json()["token_endpoint_auth_methods_supported"], ["none"])


if __name__ == "__main__":
    unittest.main()
