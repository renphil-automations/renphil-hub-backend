"""S10: parallel authorizations, consent lifetime, callback off the event loop."""

from __future__ import annotations

import re
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import app.routers.auth as auth_router
import app.routers.mcp_oauth as mcp_oauth_router
from app.db_v2.models.mcp_oauth import McpOAuthAuthorizationCodeV2, McpOAuthAuthorizationRequestV2
from tests.test_mcp_oauth_rate_limit import CLAUDE_CLIENT, ISSUER, REDIRECT, _Base, dpop_proof, mcp_settings


class ParallelConsentTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.settings = mcp_settings(MCP_OAUTH_CONSENT_REQUIRED=True)

    def browser(self):
        return TestClient(self.app(), base_url="https://hub.example", follow_redirects=False)

    def start(self, http):
        started = http.get("/mcp/oauth/authorize", params=self.authorize_params())
        continuation = parse_qs(urlsplit(started.headers["location"]).query)["redirect_uri"][0]
        return continuation, continuation.rsplit("/", 1)[1]

    def login(self, http, continuation):
        with self.Session() as session:
            return mcp_oauth_router.complete_hub_login(
                db=session, continuation_uri=continuation, email="member@renphil.org", subject="42",
                cookies={cookie.name: cookie.value for cookie in http.cookies.jar},
            )

    def approve(self, http, request_id):
        page = http.get("/mcp/oauth/consent", params={"request_id": request_id})
        self.assertEqual(page.status_code, 200, page.text)
        csrf = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text).group(1)
        return http.post("/mcp/oauth/consent", data={"request_id": request_id, "csrf_token": csrf, "decision": "approve"})

    def test_two_parallel_authorizations_in_one_browser_both_complete(self):
        http = self.browser()
        first_continuation, first = self.start(http)
        second_continuation, second = self.start(http)
        names = {cookie.name for cookie in http.cookies.jar}
        self.assertEqual(len(names), 2, names)
        self.assertTrue(all(name.startswith("__Host-renphil_mcp_oauth_consent_") for name in names), names)
        self.login(http, first_continuation)
        self.login(http, second_continuation)
        for request_id in (first, second):
            with self.subTest(request_id=request_id[:6]):
                approved = self.approve(http, request_id)
                self.assertEqual(approved.status_code, 303)
                self.assertIn("code=", approved.headers["location"])

    def test_consent_lives_as_long_as_the_cookie_but_codes_keep_their_short_ttl(self):
        http = self.browser()
        continuation, request_id = self.start(http)
        self.login(http, continuation)
        # Five minutes pass on the consent page (code TTL is 120 s).
        for model in (McpOAuthAuthorizationRequestV2, McpOAuthAuthorizationCodeV2):
            for row in self.db.query(model).all():
                row.created_at = row.created_at - timedelta(seconds=300)
        self.db.commit()
        approved = self.approve(http, request_id)
        self.assertEqual(approved.status_code, 303, approved.text)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        # The issued code itself still expires after the code TTL.
        row = self.db.query(McpOAuthAuthorizationCodeV2).one()
        row.created_at = row.created_at - timedelta(seconds=self.service.code_ttl_seconds + 1)
        self.db.commit()
        late = http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLAUDE_CLIENT,
            "redirect_uri": REDIRECT, "code_verifier": "v" * 64,
        }, headers={"DPoP": dpop_proof(f"{ISSUER}/token")})
        self.assertEqual(late.status_code, 400)
        self.assertEqual(late.json()["error"], "invalid_grant")

    def test_callback_database_work_runs_off_the_event_loop(self):
        loop_threads: set[int] = set()
        worker_threads: list[int] = []
        app = self.app()

        @app.middleware("http")
        async def record(request, call_next):
            loop_threads.add(threading.get_ident())
            return await call_next(request)

        token = SimpleNamespace(access_token="hub-session-jwt", email="m@renphil.org", name="M", roles=["Hub Member"], picture=None)

        class FakeAuth:
            async def handle_callback(self, code, state, db):
                return token, "https://hub-frontend.example/auth/callback"

        app.dependency_overrides[auth_router.get_auth_service] = lambda: FakeAuth()

        def ensure(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return SimpleNamespace(id=42, email="m@renphil.org")

        with patch.object(auth_router, "_ensure_hub_user", ensure), \
                patch.object(auth_router, "is_hub_admin", lambda *a, **k: False), \
                patch.object(auth_router, "get_settings", lambda: SimpleNamespace(MCP_OAUTH_ENABLED=False)):
            response = TestClient(app, follow_redirects=False).get("/auth/callback", params={"code": "c", "state": "s"})
        self.assertEqual(response.status_code, 307)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotIn(worker_threads[0], loop_threads)
