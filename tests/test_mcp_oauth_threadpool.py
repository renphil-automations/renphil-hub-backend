"""MCP OAuth handlers that do synchronous DB work must not block the event loop.

FastAPI runs a plain ``def`` endpoint in its threadpool; an ``async def``
endpoint runs on the event loop, where a synchronous SQLAlchemy call blocks
every other request served by that worker.
"""

from __future__ import annotations

import inspect
import threading
import unittest

from fastapi.routing import APIRoute

import app.routers.mcp_oauth as mcp_oauth_router

# Every MCP OAuth route whose handler takes the synchronous DB session.
DB_BOUND_PATHS = {"/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/agent-token"}


class McpOAuthThreadpoolTests(unittest.TestCase):
    def _routes(self):
        return [
            route for route in (*mcp_oauth_router.router.routes, *mcp_oauth_router.metadata_router.routes)
            if isinstance(route, APIRoute)
        ]

    def test_db_bound_handlers_are_plain_functions(self):
        seen = set()
        for route in self._routes():
            takes_db = any(param.name == "db" for param in route.dependant.dependencies) or "db" in inspect.signature(route.endpoint).parameters
            if not takes_db:
                continue
            seen.add(route.path)
            with self.subTest(path=route.path):
                self.assertFalse(
                    inspect.iscoroutinefunction(route.endpoint),
                    f"{route.path} does synchronous DB work and must run in the threadpool",
                )
        self.assertTrue(DB_BOUND_PATHS <= seen, seen)

    def test_handlers_run_off_the_event_loop_thread_under_testclient(self):
        """The token handler executes on a worker thread, not the loop thread."""
        from unittest.mock import patch

        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        from app.db_v2.database import get_db_v2

        loop_threads: set[int] = set()
        handler_threads: list[int] = []

        def refuse(*_args, **_kwargs):
            handler_threads.append(threading.get_ident())
            raise HTTPException(status_code=403, detail="refused")

        app = FastAPI()

        @app.middleware("http")
        async def record_loop_thread(request, call_next):
            loop_threads.add(threading.get_ident())
            return await call_next(request)

        app.include_router(mcp_oauth_router.router)
        app.dependency_overrides[get_db_v2] = lambda: None
        settings = type("S", (), {"MCP_OAUTH_ENABLED": False, "MCP_OAUTH_AGENT_CLIENT_ID": "renphil-agent"})()
        with patch.object(mcp_oauth_router, "get_settings", return_value=settings), \
                patch.object(mcp_oauth_router, "_external_admission", side_effect=refuse):
            response = TestClient(app).get("/mcp/oauth/authorize", params={
                "response_type": "code", "client_id": "client", "redirect_uri": "https://client.example/cb",
                "state": "s", "code_challenge": "A" * 43, "code_challenge_method": "S256",
            })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(handler_threads), 1)
        self.assertNotIn(handler_threads[0], loop_threads)


if __name__ == "__main__":
    unittest.main()
