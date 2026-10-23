"""Bot Management MCP settings, safe status, and MCP/answer analytics separation."""

from __future__ import annotations

import asyncio
import csv
import io
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_v2.database import BaseV2
from app.db_v2.models.bot_management import BotConfigurationStateV2, BotConfigurationVersionV2, BotSettingsAuditLogV2
from app.models.auth import UserInfo
from app.routers import bot_management

REGISTRY = (
    '{"renphil-agent":{"redirect_uris":["https://hub.example/agent"]},'
    '"claude-id":{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"]}}'
)
ADMIN = UserInfo(email="admin@renphil.org", name="Admin", roles=["Hub Admin"])


def _app_settings(**overrides) -> SimpleNamespace:
    values = dict(
        MCP_OAUTH_ENABLED=True, MCP_OAUTH_AGENT_CLIENT_ID="renphil-agent",
        MCP_OAUTH_CLIENTS_JSON=REGISTRY, MCP_OAUTH_CLIENT_PROVIDERS_JSON='{"claude-id":"claude"}',
        AGENT_API_URL="https://agent.internal.example", AGENT_SYNC_TOKEN="server-token",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        BaseV2.metadata.create_all(engine, tables=[
            BotConfigurationVersionV2.__table__, BotConfigurationStateV2.__table__, BotSettingsAuditLogV2.__table__,
        ])
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE bot_telemetry_runs (
                    id TEXT PRIMARY KEY, request_id TEXT, actor_email TEXT, actor_kind TEXT NOT NULL,
                    kind TEXT NOT NULL, job_name TEXT, model_key TEXT, configuration_version_id INTEGER,
                    started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP, elapsed_ms INTEGER,
                    status TEXT NOT NULL, error_code TEXT
                )"""))
            connection.execute(text("""
                CREATE TABLE bot_telemetry_spans (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, tool_name TEXT,
                    stage TEXT, attempt_number INTEGER, started_at TIMESTAMP, status TEXT
                )"""))
            connection.execute(text("""
                CREATE TABLE bot_token_measurements (
                    id TEXT PRIMARY KEY, span_id TEXT NOT NULL, direction TEXT, category TEXT,
                    quantity INTEGER, metric TEXT, is_detail BOOLEAN
                )"""))
        self.engine = engine
        self.db = sessionmaker(bind=engine)()

    def tearDown(self) -> None:
        self.db.close()


class BotManagementMcpSettingsTests(_Base):
    def _request(self, **fields):
        # The admin form always submits the version it was loaded from.
        fields.setdefault("expected_version_id", bot_management._active_version(self.db).id)
        return bot_management.UpdateBotSettingsRequest(active_model_key="anthropic_claude_haiku_45", **fields)

    def _update(self, **fields):
        request = self._request(**fields)
        http_request = SimpleNamespace(headers={"Authorization": "Bearer admin-session"})
        with patch.object(bot_management, "_validate_active_model_key", return_value=None):
            return bot_management.update_settings(request, http_request, current_user=ADMIN, db=self.db)

    def test_bot_management_requires_admin_with_all_scopes(self):
        current = SimpleNamespace(hub_user_id=7, info=ADMIN)
        with patch.object(bot_management, "hub_admin_target_ids", return_value=(1, [10])), \
                patch.object(bot_management, "effective_pairs", return_value={(1, 22)}):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(bot_management.require_bot_management_admin(current=current, db=self.db))
            self.assertEqual(caught.exception.status_code, 403)
        with patch.object(bot_management, "hub_admin_target_ids", return_value=(1, [10])), \
                patch.object(bot_management, "effective_pairs", return_value={(1, 10)}):
            self.assertEqual(asyncio.run(bot_management.require_bot_management_admin(current=current, db=self.db)), ADMIN)

    def test_a_save_based_on_an_older_version_is_rejected_with_409(self):
        with patch("app.config.get_settings", return_value=_app_settings()):
            loaded = bot_management._active_version(self.db).id
            # Admin A turns external access on, then off (the kill switch).
            self._update(mcp_external_connections_enabled=True, mcp_claude_enabled=True)
            self._update(mcp_external_connections_enabled=False)
            versions_before = self.db.query(BotConfigurationVersionV2).count()
            audits_before = self.db.query(BotSettingsAuditLogV2).count()
            # Admin B saves from a page loaded before both changes.
            with self.assertRaises(HTTPException) as caught:
                self._update(expected_version_id=loaded, mcp_external_connections_enabled=True, mcp_claude_enabled=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, bot_management.STALE_SETTINGS_DETAIL)
        self.db.rollback()
        active = bot_management._active_version(self.db).settings
        self.assertFalse(active["mcp_external_connections_enabled"])
        self.assertEqual(self.db.query(BotConfigurationVersionV2).count(), versions_before)
        self.assertEqual(self.db.query(BotSettingsAuditLogV2).count(), audits_before)

    def test_current_version_saves_and_each_version_id_is_single_use(self):
        loaded = bot_management._active_version(self.db).id
        saved = self._update(expected_version_id=loaded, answer_cache_enabled=False)["active"]
        self.assertNotEqual(saved["id"], loaded)
        with self.assertRaises(HTTPException) as caught:
            self._update(expected_version_id=loaded, answer_cache_enabled=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.db.rollback()
        self.assertEqual(self._update(expected_version_id=saved["id"], answer_cache_enabled=True)["active"]["settings"]["answer_cache_enabled"], True)

    def test_expected_version_is_required_and_bounded(self):
        for bad in ({}, {"expected_version_id": 0}, {"expected_version_id": "latest"}, {"expected_version_id": None}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                bot_management.UpdateBotSettingsRequest(active_model_key="anthropic_claude_haiku_45", **bad)

    def test_reset_still_creates_a_new_active_version(self):
        before = bot_management._active_version(self.db).version
        reset = bot_management.reset_settings(current_user=ADMIN, db=self.db)["active"]
        self.assertEqual(reset["version"], before + 1)
        self.assertFalse(reset["settings"]["mcp_external_connections_enabled"])

    def test_unknown_settings_fields_are_rejected(self):
        for bad in ({"mcp_copilot_enabled": True}, {"hub_ai_tool_mode": "compare"}, {"mcp_claude_verification": "trusted"},
                    {"mcp_dpop_required": False}, {"mcp_claude_enabled": "maybe"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self._request(**bad)

    def test_defaults_are_disabled_external_and_local_tool_mode(self):
        active = bot_management.get_settings(db=self.db)["active"]["settings"]
        self.assertEqual(active["hub_ai_tool_mode"], "local")
        self.assertFalse(active["mcp_external_connections_enabled"])
        for provider in ("claude", "chatgpt", "gemini"):
            self.assertFalse(active[f"mcp_{provider}_enabled"])
            self.assertEqual(active[f"mcp_{provider}_verification"], "unverified")

    def test_settings_persist_and_provider_needs_an_approved_mapping(self):
        with patch("app.config.get_settings", return_value=_app_settings()):
            saved = self._update(mcp_external_connections_enabled=True, mcp_claude_enabled=True)["active"]["settings"]
            self.assertTrue(saved["mcp_external_connections_enabled"])
            self.assertTrue(saved["mcp_claude_enabled"])
            # ChatGPT has no approved mapping: it cannot be enabled or verified.
            with self.assertRaises(HTTPException) as caught:
                self._update(mcp_chatgpt_enabled=True)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertIn("approved client mapping", caught.exception.detail)
            with self.assertRaises(HTTPException):
                self._update(mcp_chatgpt_verification="dpop_staging_verified")
        active = bot_management.get_settings(db=self.db)["active"]["settings"]
        self.assertFalse(active["mcp_chatgpt_enabled"])
        audit = self.db.query(BotSettingsAuditLogV2).count()
        self.assertEqual(audit, 1)

    def test_operational_requires_prior_staging_verification(self):
        with patch("app.config.get_settings", return_value=_app_settings()):
            with self.assertRaises(HTTPException) as caught:
                self._update(mcp_claude_verification="deployment_verified")
            self.assertEqual(caught.exception.status_code, 409)
            self._update(mcp_claude_verification="dpop_staging_verified")
            saved = self._update(mcp_claude_verification="deployment_verified")["active"]["settings"]
        self.assertEqual(saved["mcp_claude_verification"], "deployment_verified")

    def test_hub_agent_mcp_mode_requires_a_configured_agent_mcp_connection(self):
        with patch.object(bot_management, "_mcp_runtime_probe", return_value={"health": "unavailable", "configured": False, "reason": "x"}):
            with self.assertRaises(HTTPException) as caught:
                self._update(hub_ai_tool_mode="mcp")
            self.assertEqual(caught.exception.status_code, 409)
        with patch.object(bot_management, "_mcp_runtime_probe", return_value={"health": "degraded", "configured": True, "reason": "x"}):
            self.assertEqual(self._update(hub_ai_tool_mode="mcp")["active"]["settings"]["hub_ai_tool_mode"], "mcp")
        # Switching back to the approved local fallback never needs the probe.
        with patch.object(bot_management, "_mcp_runtime_probe", side_effect=AssertionError("not needed")):
            self.assertEqual(self._update(hub_ai_tool_mode="local")["active"]["settings"]["hub_ai_tool_mode"], "local")

    def test_mcp_status_is_safe_and_reports_readiness(self):
        probe = {"health": "operational", "configured": True, "reason": "MCP server is reachable."}
        with patch("app.config.get_settings", return_value=_app_settings()), \
                patch.object(bot_management, "_mcp_runtime_probe", return_value=probe):
            self._update(mcp_external_connections_enabled=True, mcp_claude_enabled=True)
            status = bot_management.get_mcp_status(db=self.db)
        providers = {row["provider"]: row for row in status["providers"]}
        self.assertEqual(providers["claude"]["readiness"], "blocked_pending_dpop_verification")
        self.assertEqual(providers["chatgpt"]["readiness"], "disabled")
        self.assertFalse(providers["chatgpt"]["client_mapping_configured"])
        self.assertEqual(status["runtime"]["health"], "operational")
        rendered = repr(status)
        for secret in ("claude-id", "server-token", "agent.internal", "https://claude.ai"):
            self.assertNotIn(secret, rendered)

    def test_runtime_probe_uses_the_server_token_and_hides_failures(self):
        captured = {}

        def fake_get(url, headers, timeout):
            captured.update(url=url, headers=headers)
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"mcp": {"available": False, "configured": True, "reason": "MCP server is temporarily unavailable."}}
            return response

        with patch("app.config.get_settings", return_value=_app_settings()), patch.object(bot_management.httpx, "get", fake_get):
            result = bot_management._mcp_runtime_probe()
        self.assertEqual(result["health"], "degraded")
        self.assertEqual(captured["headers"]["X-Sync-Token"], "server-token")
        self.assertNotIn("Authorization", captured["headers"])
        self.assertTrue(captured["url"].endswith("/agent/mcp-runtime-status"))
        with patch("app.config.get_settings", return_value=_app_settings()), \
                patch.object(bot_management.httpx, "get", side_effect=httpx.ConnectError("https://agent.internal.example secret")):
            failed = bot_management._mcp_runtime_probe()
        self.assertEqual(failed["health"], "unavailable")
        self.assertNotIn("agent.internal", repr(failed))
        with patch("app.config.get_settings", return_value=_app_settings(AGENT_SYNC_TOKEN="")):
            self.assertFalse(bot_management._mcp_runtime_probe()["configured"])


class BotManagementMcpAnalyticsTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        base = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        rows = [
            # (actor_email, actor_kind, kind, model_key, status, elapsed, tool, day_offset)
            ("ana@renphil.org", "user", "request", "anthropic_claude_haiku_45", "succeeded", 4000, "hub_search", 0),
            ("ana@renphil.org", "user", "request", "anthropic_claude_haiku_45", "failed", 6000, "hub_search", 0),
            (None, "system", "job", None, "succeeded", 1000, None, 0),
            ("ana@renphil.org", "mcp_hub_agent", "mcp", "mcp:renphil-agent:anthropic_claude_haiku_45", "succeeded", 900, "hub_search", 0),
            ("ben@renphil.org", "mcp_claude", "mcp", "mcp:claude", "succeeded", 700, "airtable_search", 1),
            ("ben@renphil.org", "mcp_gemini", "mcp", "mcp:gemini", "failed", 50000, "hub_search", 1),
        ]
        with self.engine.begin() as connection:
            for email, actor_kind, kind, model_key, status, elapsed, tool, offset in rows:
                run_id = str(uuid.uuid4())
                started = base + timedelta(days=offset)
                connection.execute(text("""
                    INSERT INTO bot_telemetry_runs (id, actor_email, actor_kind, kind, model_key, started_at, finished_at, elapsed_ms, status, error_code)
                    VALUES (:id, :email, :actor_kind, :kind, :model_key, :started, :finished, :elapsed, :status, :error)
                """), {"id": run_id, "email": email, "actor_kind": actor_kind, "kind": kind, "model_key": model_key,
                       "started": started, "finished": started, "elapsed": elapsed, "status": status,
                       "error": "upstream_unavailable" if status == "failed" and kind == "mcp" else None})
                if tool:
                    connection.execute(text("""
                        INSERT INTO bot_telemetry_spans (id, run_id, kind, tool_name, stage, attempt_number, started_at, status)
                        VALUES (:id, :run, 'tool', :tool, 'x', 1, :started, :status)
                    """), {"id": str(uuid.uuid4()), "run": run_id, "tool": tool, "started": started, "status": status})

    def _filters(self, **overrides):
        values = dict(date_from=None, date_to=None, actor_email=None, model_key=None, status=None, kind=None, tool_name=None, search=None, provider=None)
        values.update(overrides)
        return values

    def test_summary_never_counts_mcp_calls_as_answers(self):
        summary = bot_management.get_bot_analytics_summary(**self._filters(), db=self.db)
        totals = summary["totals"]
        self.assertEqual(totals["run_count"], 3)
        self.assertEqual(totals["request_count"], 2)
        self.assertEqual(totals["background_job_count"], 1)
        self.assertEqual((totals["succeeded_count"], totals["failed_count"]), (2, 1))
        self.assertEqual(totals["total_elapsed_ms"], 11000)
        self.assertAlmostEqual(totals["average_elapsed_ms"], 11000 / 3, places=1)
        self.assertEqual(summary["mcp"], {"call_count": 3, "succeeded_count": 2, "failed_count": 1, "average_elapsed_ms": round((900 + 700 + 50000) / 3, 2)})

    def test_charts_exclude_mcp_unless_mcp_is_selected(self):
        points = bot_management.get_bot_analytics_chart_data(**self._filters(), db=self.db)["data"]
        self.assertEqual({point["kind"] for point in points}, {"request", "job"})
        mcp_points = bot_management.get_bot_analytics_chart_data(**self._filters(kind="mcp"), db=self.db)["data"]
        self.assertEqual(len(mcp_points), 3)
        claude_points = bot_management.get_bot_analytics_chart_data(**self._filters(provider="claude"), db=self.db)["data"]
        self.assertEqual(len(claude_points), 1)

    def test_mcp_filters_for_type_provider_tool_model_outcome_user_and_date(self):
        def ids(**filters):
            return bot_management.list_bot_analytics_runs(page=1, page_size=50, **self._filters(**filters), sort_by="when", sort_direction="desc", db=self.db)["data"]

        self.assertEqual(len(ids(kind="mcp")), 3)
        hub_agent = ids(provider="hub_agent")
        self.assertEqual([(row["mcp_provider"], row["mcp_tool_name"]) for row in hub_agent], [("hub_agent", "hub_search")])
        self.assertEqual(len(ids(provider="gemini", status="failed")), 1)
        self.assertEqual(len(ids(kind="mcp", tool_name="airtable_search")), 1)
        self.assertEqual(len(ids(kind="mcp", actor_email="ben@renphil.org")), 2)
        # A certified model also selects the Hub Agent's MCP calls made with it.
        self.assertEqual({row["kind"] for row in ids(model_key="anthropic_claude_haiku_45")}, {"request", "mcp"})
        self.assertEqual(len(ids(kind="mcp", date_from=datetime(2026, 10, 21).date(), date_to=datetime(2026, 10, 21).date())), 2)
        with self.assertRaises(HTTPException):
            ids(provider="copilot")

    def test_csv_export_respects_the_selected_filters_exactly(self):
        response = bot_management.export_bot_analytics_csv(**self._filters(kind="mcp", provider="claude"), db=self.db)
        rows = list(csv.DictReader(io.StringIO(response.body.decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["mcp_provider"], rows[0]["mcp_tool_name"]), ("mcp", "claude", "airtable_search"))
        answers = list(csv.DictReader(io.StringIO(bot_management.export_bot_analytics_csv(**self._filters(kind="request"), db=self.db).body.decode())))
        self.assertEqual({row["kind"] for row in answers}, {"request"})

    def test_workbook_usage_and_duration_sheets_exclude_mcp_and_mcp_sheet_lists_calls(self):
        import re
        import zipfile

        runs, _ = bot_management._analytics_rows(self.db, **self._filters(), limit=100, offset=0)
        points = bot_management._analytics_chart_rows(self.db, **self._filters())
        payload = bot_management._build_bot_analytics_workbook(
            report="all", points=points, runs=runs, feedback_records=[], viewer_time_zone="UTC", include_mcp=False,
        )
        archive = zipfile.ZipFile(io.BytesIO(payload))
        workbook_xml = archive.read("xl/workbook.xml").decode()
        sheet_names = re.findall(r'<sheet name="([^"]+)"', workbook_xml)
        self.assertIn("MCP calls", sheet_names)
        shared = re.findall(r"<t[^>]*>([^<]*)</t>", archive.read("xl/sharedStrings.xml").decode())

        def sheet_strings(name: str) -> list[str]:
            index = sheet_names.index(name) + 1
            xml = archive.read(f"xl/worksheets/sheet{index}.xml").decode()
            return [shared[int(value)] for value in re.findall(r'<c [^>]*t="s"[^>]*><v>(\d+)</v>', xml)]

        self.assertNotIn("mcp", sheet_strings("Response time"))
        self.assertNotIn("mcp", sheet_strings("Time of day"))
        self.assertNotIn("ben@renphil.org", sheet_strings("Usage by user"))
        mcp_values = sheet_strings("MCP calls")
        for expected in ("hub_agent", "claude", "gemini", "airtable_search", "upstream_unavailable"):
            self.assertIn(expected, mcp_values)


if __name__ == "__main__":
    unittest.main()
