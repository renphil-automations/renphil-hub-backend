"""Admin(All Scopes) Bot Management settings foundation."""

from __future__ import annotations

import os
import csv
import asyncio
from collections import Counter
from io import BytesIO, StringIO
from datetime import date, timedelta
from datetime import datetime, timezone
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import xlsxwriter
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.db_v2.models.bot_management import (
    BotConfigurationStateV2,
    BotConfigurationVersionV2,
    BotSettingsAuditLogV2,
)
from app.dependencies import (
    CurrentHubUser,
    get_airtable_service,
    get_current_hub_user,
)
from app.services.airtable_service import AirtableService
from app.models.auth import UserInfo
from app.services.rbac_graph_service import effective_pairs, hub_admin_target_ids


DEFAULT_SETTINGS = {
    "active_model_key": "anthropic_claude_haiku_45",
    "general_assistant_mode_enabled": True,
    "answer_cache_enabled": True,
    "google_drive_knowledge_enabled": False,
}

# Feedback remains sourced from Airtable. A brief process-local cache prevents
# every Analytics/Feedback tab switch from walking the same table again. The
# Refresh button passes force_refresh=True and always bypasses this cache.
_FEEDBACK_CACHE_TTL_SECONDS = 60.0
_feedback_cache: tuple[float, list[dict]] | None = None
_feedback_cache_lock = asyncio.Lock()


async def _feedback_payload(
    airtable_service: AirtableService,
    *,
    limit: int,
    force_refresh: bool = False,
) -> list[dict]:
    global _feedback_cache
    bounded_limit = min(max(int(limit), 1), 500)
    now = monotonic()
    if not force_refresh and _feedback_cache and now - _feedback_cache[0] < _FEEDBACK_CACHE_TTL_SECONDS:
        return _feedback_cache[1][:bounded_limit]

    async with _feedback_cache_lock:
        now = monotonic()
        if not force_refresh and _feedback_cache and now - _feedback_cache[0] < _FEEDBACK_CACHE_TTL_SECONDS:
            return _feedback_cache[1][:bounded_limit]
        records = await airtable_service.list_feedbacks(limit=500)
        payload = [record.model_dump(by_alias=False) for record in records]
        _feedback_cache = (monotonic(), payload)
        return payload[:bounded_limit]


def _validate_active_model_key(
    active_model_key: str,
    authorization: str | None,
) -> None:
    agent_url = os.getenv("AGENT_API_URL", "").strip().rstrip("/")
    if not agent_url:
        raise HTTPException(
            status_code=503,
            detail="AGENT_API_URL is required to validate the selected model.",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header is required")

    try:
        response = httpx.get(
            f"{agent_url}/agent/models",
            headers={"Authorization": authorization},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="The Agent model registry is unavailable.",
        ) from exc

    allowed_model_keys = {
        str(model.get("key") or "").strip().lower()
        for model in payload.get("models", [])
        if bool(model.get("available")) and bool(model.get("certified"))
    }
    if active_model_key not in allowed_model_keys:
        raise HTTPException(
            status_code=422,
            detail="The selected model is not currently certified and configured.",
        )

async def require_bot_management_admin(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> UserInfo:
    """Require the explicit (hub_admin, All Scopes) assignment for Bot Management."""
    role_id, universal_scope_ids = hub_admin_target_ids(db)
    pairs = effective_pairs(db, current.hub_user_id)
    allowed = bool(
        role_id is not None
        and any((role_id, scope_id) in pairs for scope_id in universal_scope_ids)
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="Bot Management requires Admin access with All Scopes.",
        )
    return current.info


router = APIRouter(
    prefix="/v2/bot-management",
    tags=["Bot Management"],
    dependencies=[Depends(require_bot_management_admin)],
)


class UpdateBotSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_model_key: str = Field(min_length=1, max_length=128)
    general_assistant_mode_enabled: bool | None = None
    answer_cache_enabled: bool | None = None
    google_drive_knowledge_enabled: bool | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _analytics_filters(
    *,
    date_from: date | None,
    date_to: date | None,
    actor_email: str | None,
    model_key: str | None,
    status: str | None,
    kind: str | None,
    tool_name: str | None,
    search: str | None = None,
) -> tuple[str, dict[str, object]]:
    clauses = ["1 = 1"]
    params: dict[str, object] = {}
    if date_from is not None:
        clauses.append("runs.started_at >= :date_from")
        params["date_from"] = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
    if date_to is not None:
        clauses.append("runs.started_at < :date_to")
        params["date_to"] = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    for column, value, key in (
        ("runs.actor_email", actor_email, "actor_email"),
        ("runs.model_key", model_key, "model_key"),
        ("runs.status", status, "status"),
        ("runs.kind", kind, "kind"),
    ):
        normalized = (value or "").strip().lower()
        if normalized:
            clauses.append(f"{column} = :{key}")
            params[key] = normalized
    normalized_tool = (tool_name or "").strip().lower()
    if normalized_tool:
        clauses.append(
            "EXISTS (SELECT 1 FROM bot_telemetry_spans AS filter_spans "
            "WHERE filter_spans.run_id = runs.id AND LOWER(filter_spans.tool_name) = :tool_name)"
        )
        params["tool_name"] = normalized_tool
    normalized_search = (search or "").strip().lower()
    if normalized_search:
        clauses.append("""(
            LOWER(COALESCE(runs.actor_email, '')) LIKE :search
            OR LOWER(COALESCE(runs.job_name, '')) LIKE :search
            OR LOWER(COALESCE(runs.model_key, '')) LIKE :search
            OR LOWER(COALESCE(runs.kind, '')) LIKE :search
            OR LOWER(COALESCE(runs.status, '')) LIKE :search
            OR LOWER(COALESCE(runs.error_code, '')) LIKE :search
            OR CAST(runs.configuration_version_id AS TEXT) LIKE :search
            OR EXISTS (
                SELECT 1 FROM bot_telemetry_spans AS search_spans
                WHERE search_spans.run_id = runs.id
                  AND LOWER(COALESCE(search_spans.tool_name, '')) LIKE :search
            )
        )""")
        params["search"] = f"%{normalized_search}%"
    return " AND ".join(clauses), params


def _analytics_rows(
    db: Session,
    *,
    date_from: date | None,
    date_to: date | None,
    actor_email: str | None,
    model_key: str | None,
    status: str | None,
    kind: str | None,
    tool_name: str | None,
    limit: int,
    offset: int,
    search: str | None = None,
) -> tuple[list[dict], int]:
    where_sql, params = _analytics_filters(
        date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
    )
    try:
        total = int(db.execute(text(f"SELECT COUNT(*) FROM bot_telemetry_runs AS runs WHERE {where_sql}"), params).scalar_one())
        rows = db.execute(text(f"""
            SELECT runs.id, runs.actor_email, runs.kind, runs.job_name, runs.model_key,
                   runs.configuration_version_id, runs.started_at, runs.finished_at,
                   runs.elapsed_ms, runs.status, runs.error_code,
                   COUNT(spans.id) AS span_count
            FROM bot_telemetry_runs AS runs
            LEFT JOIN bot_telemetry_spans AS spans ON spans.run_id = runs.id
            WHERE {where_sql}
            GROUP BY runs.id, runs.actor_email, runs.kind, runs.job_name, runs.model_key,
                     runs.configuration_version_id, runs.started_at, runs.finished_at,
                     runs.elapsed_ms, runs.status, runs.error_code
            ORDER BY runs.started_at DESC, runs.id DESC
            LIMIT :limit OFFSET :offset
        """), {**params, "limit": limit, "offset": offset}).mappings().all()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Bot analytics is temporarily unavailable.") from exc
    return [
        {
            "id": str(row["id"]), "actor_email": row["actor_email"],
            "kind": row["kind"], "job_name": row["job_name"],
            "model_key": row["model_key"], "configuration_version_id": row["configuration_version_id"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "elapsed_ms": row["elapsed_ms"], "status": row["status"],
            "error_code": row["error_code"], "span_count": int(row["span_count"] or 0),
        }
        for row in rows
    ], total


def _analytics_chart_rows(
    db: Session,
    *,
    date_from: date | None,
    date_to: date | None,
    actor_email: str | None,
    model_key: str | None,
    status: str | None,
    kind: str | None,
    tool_name: str | None,
    search: str | None = None,
) -> list[dict]:
    """Return bounded, non-content telemetry points for browser-local charts."""
    where_sql, params = _analytics_filters(
        date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
    )
    try:
        rows = db.execute(text(f"""
            SELECT runs.actor_email, runs.kind, runs.started_at, runs.elapsed_ms
            FROM bot_telemetry_runs AS runs
            WHERE {where_sql}
            ORDER BY runs.started_at DESC, runs.id DESC
            LIMIT 10000
        """), params).mappings().all()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Bot analytics is temporarily unavailable.") from exc
    return [
        {
            "actor_email": row["actor_email"],
            "kind": row["kind"],
            "started_at": row["started_at"],
            "elapsed_ms": row["elapsed_ms"],
        }
        for row in rows
    ]


def _analytics_distribution(values: list[str | None], fallback: str) -> list[tuple[str, int]]:
    counts = Counter((value or "").strip() or fallback for value in values)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))


def _analytics_response_time_buckets(points: list[dict]) -> list[tuple[str, int]]:
    durations = [int(point["elapsed_ms"]) for point in points if point.get("elapsed_ms") is not None and int(point["elapsed_ms"]) >= 0]
    if not durations:
        return []
    maximum = max(max(durations), 1)
    bucket_size = max(1_000, ((maximum + 7_999) // 8_000) * 1_000)
    bucket_count = min(8, maximum // bucket_size + 1)
    counts = [0] * bucket_count
    for duration in durations:
        counts[min(duration // bucket_size, bucket_count - 1)] += 1
    return [
        (f"{index * bucket_size // 1000}–{(index + 1) * bucket_size // 1000} s", count)
        for index, count in enumerate(counts)
    ]


def _analytics_export_timezone(value: str | None) -> tuple[timezone | ZoneInfo, str]:
    try:
        timezone_name = (value or "UTC").strip()
        return ZoneInfo(timezone_name), timezone_name
    except Exception:
        return timezone.utc, "UTC"


def _analytics_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _analytics_timestamp(value: object, viewer_timezone: timezone | ZoneInfo) -> str:
    parsed = _analytics_datetime(value)
    if parsed is not None:
        return parsed.astimezone(viewer_timezone).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _write_analytics_report_sheet(
    workbook: xlsxwriter.Workbook,
    *,
    sheet_name: str,
    title: str,
    subtitle: str,
    series_headers: tuple[str, str],
    series_rows: list[tuple[str, int]],
    detail_headers: list[str],
    detail_rows: list[list[object]],
    chart_kind: Literal["pie", "column", "line", "bar"],
) -> None:
    worksheet = workbook.add_worksheet(sheet_name)
    worksheet.hide_gridlines(2)
    worksheet.set_tab_color("#FF7F50")
    worksheet.set_column(0, 0, 34)
    worksheet.set_column(1, 1, 21)
    worksheet.set_column(2, 2, 18)
    worksheet.set_column(3, 3, 16)
    worksheet.set_column(4, 4, 24)
    worksheet.set_column(5, 5, 3)
    worksheet.set_column(6, 13, 15)
    detail_widths = {
        "User": 30, "Started at": 21, "Duration (ms)": 16, "Outcome": 15,
        "Model": 28, "Run type": 13, "Job name": 28, "Feedback": 34,
        "Submitted at": 21, "Message ID": 24, "Question": 48, "Response": 56,
    }
    for column_index, header in enumerate(detail_headers):
        worksheet.set_column(column_index, column_index, detail_widths.get(header, 20))

    title_format = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1D1D1F"})
    subtitle_format = workbook.add_format({"font_size": 9, "font_color": "#6E6E73"})
    section_format = workbook.add_format({"bold": True, "font_color": "#1D1D1F", "bg_color": "#F5F5F7"})
    header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#FF7F50", "border": 0})
    text_format = workbook.add_format({"font_color": "#1D1D1F", "bottom": 1, "bottom_color": "#E5E5EA"})
    count_format = workbook.add_format({"font_color": "#1D1D1F", "num_format": "#,##0", "bottom": 1, "bottom_color": "#E5E5EA"})
    empty_format = workbook.add_format({"italic": True, "font_color": "#6E6E73"})

    worksheet.merge_range(0, 0, 0, 7, title, title_format)
    worksheet.merge_range(1, 0, 1, 7, subtitle, subtitle_format)
    worksheet.set_row(0, 28)
    worksheet.write(4, 0, "Chart data", section_format)
    worksheet.write_row(5, 0, series_headers, header_format)
    for row_index, row in enumerate(series_rows, start=6):
        worksheet.write(row_index, 0, row[0], text_format)
        worksheet.write_number(row_index, 1, row[1], count_format)

    if series_rows:
        chart_type = "column" if chart_kind == "column" else chart_kind
        chart = workbook.add_chart({"type": chart_type})
        series: dict[str, object] = {
            "name": series_headers[1],
            "categories": [sheet_name, 6, 0, 5 + len(series_rows), 0],
            "values": [sheet_name, 6, 1, 5 + len(series_rows), 1],
        }
        if chart_kind == "pie":
            colors = ["#FF7F50", "#0A84FF", "#30D158", "#BF5AF2", "#FF9F0A", "#64D2FF", "#FF6482", "#5E5CE6"]
            series["points"] = [{"fill": {"color": colors[index % len(colors)]}} for index in range(len(series_rows))]
        else:
            series["fill"] = {"color": "#FF7F50"}
            series["line"] = {"color": "#FF7F50"}
        chart.add_series(series)
        chart.set_title({"name": title})
        chart.set_legend({"none": chart_kind != "pie"})
        chart.set_style(10)
        chart.set_size({"width": 560, "height": 280})
        worksheet.insert_chart(3, 6, chart, {"x_scale": 1, "y_scale": 1})
    else:
        worksheet.write(6, 0, "No matching data", empty_format)

    # The chart occupies the upper-right region through approximately row 20.
    # Supporting detail begins below it, and moves farther down for a long
    # chart-data table, so neither region can cover the other in Excel.
    detail_start = max(22, 8 + len(series_rows))
    worksheet.write(detail_start, 0, "Supporting detail", section_format)
    worksheet.write_row(detail_start + 1, 0, detail_headers, header_format)
    for row_index, row in enumerate(detail_rows, start=detail_start + 2):
        for column_index, value in enumerate(row):
            if isinstance(value, int):
                worksheet.write_number(row_index, column_index, value, count_format)
            else:
                worksheet.write(row_index, column_index, value if value is not None else "", text_format)
    if not detail_rows:
        worksheet.write(detail_start + 2, 0, "No matching records", empty_format)
    else:
        worksheet.autofilter(detail_start + 1, 0, detail_start + 1 + len(detail_rows), len(detail_headers) - 1)
    worksheet.freeze_panes(3, 0)


def _build_bot_analytics_workbook(
    *,
    report: Literal["all", "usage_by_user", "time_of_day", "response_time", "negative_feedback"],
    points: list[dict],
    runs: list[dict],
    feedback_records: list[object],
    viewer_time_zone: str | None,
) -> bytes:
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    workbook.set_properties({"title": "RenPhil Hub bot analytics", "company": "Renaissance Philanthropy"})
    viewer_timezone, timezone_label = _analytics_export_timezone(viewer_time_zone)
    subtitle = f"RenPhil Hub bot analytics. Times are shown in {timezone_label}."
    requested_points = [point for point in points if point.get("kind") == "request"]
    user_series = _analytics_distribution([point.get("actor_email") for point in requested_points], "Unknown user")
    hours = [0] * 24
    for point in points:
        timestamp = _analytics_datetime(point.get("started_at"))
        if timestamp is not None:
            hours[timestamp.astimezone(viewer_timezone).hour] += 1
    hour_series = [(f"{hour:02}:00", count) for hour, count in enumerate(hours)]
    response_series = _analytics_response_time_buckets(points)
    negative_feedback = [record for record in feedback_records if getattr(record, "impression", None) == "Dislike"]
    feedback_series = _analytics_distribution([getattr(record, "message", None) for record in negative_feedback], "No type supplied")

    if report in ("all", "usage_by_user"):
        _write_analytics_report_sheet(
            workbook, sheet_name="Usage by user", title="Usage by user", subtitle=subtitle,
            series_headers=("User", "Requests"), series_rows=user_series,
            detail_headers=["User", "Started at", "Duration (ms)", "Outcome", "Model"],
            detail_rows=[[(run.get("actor_email") or "Unknown user"), _analytics_timestamp(run.get("started_at"), viewer_timezone), run.get("elapsed_ms") if run.get("elapsed_ms") is not None else "", run.get("status") or "", run.get("model_key") or ""] for run in runs if run.get("kind") == "request"],
            chart_kind="pie",
        )
    if report in ("all", "time_of_day"):
        _write_analytics_report_sheet(
            workbook, sheet_name="Time of day", title="Usage by time of day", subtitle=subtitle,
            series_headers=(f"Hour ({timezone_label})", "Runs"), series_rows=hour_series,
            detail_headers=["Started at", "User", "Run type", "Duration (ms)", "Outcome", "Model", "Job name"],
            detail_rows=[[_analytics_timestamp(run.get("started_at"), viewer_timezone), run.get("actor_email") or "", run.get("kind") or "", run.get("elapsed_ms") if run.get("elapsed_ms") is not None else "", run.get("status") or "", run.get("model_key") or "", run.get("job_name") or ""] for run in runs],
            chart_kind="column",
        )
    if report in ("all", "response_time"):
        _write_analytics_report_sheet(
            workbook, sheet_name="Response time", title="Response-time distribution", subtitle=subtitle,
            series_headers=("Duration range", "Runs"), series_rows=response_series,
            detail_headers=["Started at", "User", "Run type", "Duration (ms)", "Outcome", "Model", "Job name"],
            detail_rows=[[_analytics_timestamp(run.get("started_at"), viewer_timezone), run.get("actor_email") or "", run.get("kind") or "", run.get("elapsed_ms") if run.get("elapsed_ms") is not None else "", run.get("status") or "", run.get("model_key") or "", run.get("job_name") or ""] for run in runs if run.get("elapsed_ms") is not None],
            chart_kind="line",
        )
    if report in ("all", "negative_feedback"):
        _write_analytics_report_sheet(
            workbook, sheet_name="Negative feedback", title="Negative feedback by type", subtitle=subtitle,
            series_headers=("Feedback type", "Negative feedback"), series_rows=feedback_series,
            detail_headers=["Feedback", "User", "Submitted at", "Message ID", "Question", "Response"],
            detail_rows=[[
                getattr(record, "message", None) or "No type supplied", getattr(record, "from_email", None) or "",
                getattr(record, "date_time", None) or "", getattr(record, "message_id", None) or "",
                getattr(record, "query", None) or "", getattr(record, "response", None) or "",
            ] for record in negative_feedback],
            chart_kind="bar",
        )
    if report == "all":
        worksheet = workbook.add_worksheet("Runs")
        worksheet.hide_gridlines(2)
        worksheet.set_tab_color("#FF7F50")
        worksheet.set_column(0, 0, 38)
        worksheet.set_column(1, 1, 30)
        worksheet.set_column(2, 2, 13)
        worksheet.set_column(3, 3, 28)
        worksheet.set_column(4, 4, 18)
        worksheet.set_column(5, 6, 21)
        worksheet.set_column(7, 10, 15)
        worksheet.set_column(11, 11, 28)
        title_format = workbook.add_format({"bold": True, "font_size": 18, "font_color": "#1D1D1F"})
        subtitle_format = workbook.add_format({"font_size": 9, "font_color": "#6E6E73"})
        header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#FF7F50"})
        worksheet.merge_range(0, 0, 0, 11, "Bot analytics runs", title_format)
        worksheet.merge_range(1, 0, 1, 11, subtitle, subtitle_format)
        headers = ["Run ID", "User / job", "Type", "Model", "Configuration version", "Started at", "Finished at", "Duration (ms)", "Outcome", "Error code", "Spans", "Job name"]
        worksheet.write_row(4, 0, headers, header_format)
        for row_index, run in enumerate(runs, start=5):
            worksheet.write_row(row_index, 0, [
                run["id"], run.get("actor_email") or "", run.get("kind") or "", run.get("model_key") or "",
                run.get("configuration_version_id") or "", _analytics_timestamp(run.get("started_at"), viewer_timezone), _analytics_timestamp(run.get("finished_at"), viewer_timezone),
                run.get("elapsed_ms") if run.get("elapsed_ms") is not None else "", run.get("status") or "", run.get("error_code") or "", run.get("span_count") or 0, run.get("job_name") or "",
            ])
        worksheet.autofilter(4, 0, max(5, 4 + len(runs)), len(headers) - 1)
        worksheet.freeze_panes(5, 0)
    workbook.close()
    return output.getvalue()


def _version_payload(version: BotConfigurationVersionV2) -> dict:
    return {
        "id": version.id,
        "version": version.version,
        "settings": _normalized_settings(version.settings),
        "is_default": version.is_default,
        "created_by_email": version.created_by_email,
        "created_at": version.created_at,
    }


def _normalized_settings(settings: object) -> dict:
    """Supply safe defaults for configuration versions created before new fields."""
    normalized = dict(DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        normalized.update(settings)
    return normalized


def _active_version(db: Session) -> BotConfigurationVersionV2:
    state = db.get(BotConfigurationStateV2, 1)
    if state is None:
        now = _now()
        default = BotConfigurationVersionV2(
            version=1,
            settings=dict(DEFAULT_SETTINGS),
            is_default=True,
            created_by_email="system",
            created_at=now,
        )
        db.add(default)
        db.flush()
        db.add(
            BotConfigurationStateV2(
                id=1,
                active_version_id=default.id,
                updated_at=now,
            )
        )
        db.commit()
        return default

    version = db.get(BotConfigurationVersionV2, state.active_version_id)
    if version is None:
        raise HTTPException(status_code=500, detail="Active bot configuration is missing")
    return version


def _default_version(db: Session) -> BotConfigurationVersionV2:
    _active_version(db)
    version = (
        db.query(BotConfigurationVersionV2)
        .filter(BotConfigurationVersionV2.is_default.is_(True))
        .order_by(BotConfigurationVersionV2.version.asc())
        .first()
    )
    if version is None:
        raise HTTPException(status_code=500, detail="Bot default configuration is missing")
    return version


def _next_version_number(db: Session) -> int:
    latest = (
        db.query(BotConfigurationVersionV2)
        .order_by(BotConfigurationVersionV2.version.desc())
        .first()
    )
    return 1 if latest is None else latest.version + 1


def _save_version(
    db: Session,
    actor: UserInfo,
    settings: dict,
    action: str,
) -> BotConfigurationVersionV2:
    previous = _active_version(db)
    now = _now()
    version = BotConfigurationVersionV2(
        version=_next_version_number(db),
        settings=_normalized_settings(settings),
        is_default=False,
        created_by_email=(actor.email or "").strip().lower(),
        created_at=now,
    )
    db.add(version)
    db.flush()

    state = db.get(BotConfigurationStateV2, 1)
    state.active_version_id = version.id
    state.updated_at = now
    db.add(
        BotSettingsAuditLogV2(
            action=action,
            actor_email=(actor.email or "").strip().lower(),
            from_version_id=previous.id,
            to_version_id=version.id,
            created_at=now,
        )
    )
    db.commit()
    db.refresh(version)
    return version


@router.get("/models")
def list_models_for_bot_management(http_request: Request):
    """Proxy model choices through Hub authorization."""
    agent_url = (os.getenv("AGENT_API_URL") or "").rstrip("/")
    if not agent_url:
        raise HTTPException(status_code=503, detail="Agent API is not configured")
    headers: dict[str, str] = {"Accept": "application/json"}
    authorization = http_request.headers.get("authorization")
    if authorization:
        headers["Authorization"] = authorization
    try:
        response = httpx.get(f"{agent_url}/agent/models", headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Agent model list is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Agent model list could not be loaded")
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise HTTPException(status_code=502, detail="Agent returned an invalid model list")
    return payload


def _drive_runtime_status(authorization: str | None) -> dict:
    """Read the safe Drive readiness state from the Agent."""
    agent_url = (os.getenv("AGENT_API_URL") or "").rstrip("/")
    if not agent_url:
        raise HTTPException(status_code=503, detail="Agent runtime status is unavailable")
    headers: dict[str, str] = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    try:
        response = httpx.get(f"{agent_url}/agent/runtime-status", headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Agent runtime status is unavailable") from exc

    drive = payload.get("drive_knowledge") if isinstance(payload, dict) else None
    if not isinstance(drive, dict) or not isinstance(drive.get("available"), bool) or not isinstance(drive.get("reason"), str):
        raise HTTPException(status_code=502, detail="Agent returned an invalid runtime status")
    return {"drive_knowledge": {"available": drive["available"], "reason": drive["reason"]}}


@router.get("/runtime-status")
def get_runtime_status_for_bot_management(http_request: Request):
    """Expose safe runtime readiness information through Bot Management only."""
    return _drive_runtime_status(http_request.headers.get("authorization"))

@router.get("/settings")
def get_settings(db: Session = Depends(get_db_v2)):
    return {
        "active": _version_payload(_active_version(db)),
        "default": _version_payload(_default_version(db)),
    }


@router.put("/settings")
def update_settings(
    request: UpdateBotSettingsRequest,
    http_request: Request,
    current_user: UserInfo = Depends(require_bot_management_admin),
    db: Session = Depends(get_db_v2),
):
    active_model_key = request.active_model_key.strip().lower()
    if not active_model_key:
        raise HTTPException(status_code=422, detail="active_model_key is required")

    _validate_active_model_key(
        active_model_key,
        http_request.headers.get("Authorization"),
    )

    settings = _normalized_settings(_active_version(db).settings)
    settings["active_model_key"] = active_model_key
    if request.general_assistant_mode_enabled is not None:
        settings["general_assistant_mode_enabled"] = request.general_assistant_mode_enabled
    if request.answer_cache_enabled is not None:
        settings["answer_cache_enabled"] = request.answer_cache_enabled
    if request.google_drive_knowledge_enabled is not None:
        if request.google_drive_knowledge_enabled and not _drive_runtime_status(
            http_request.headers.get("authorization")
        )["drive_knowledge"]["available"]:
            raise HTTPException(
                status_code=409,
                detail="Google Drive Knowledge cannot be enabled until its approved folder and service account are ready.",
            )
        settings["google_drive_knowledge_enabled"] = request.google_drive_knowledge_enabled

    version = _save_version(
        db,
        current_user,
        settings,
        "settings_updated",
    )
    return {"active": _version_payload(version)}


@router.post("/settings/reset")
def reset_settings(
    current_user: UserInfo = Depends(require_bot_management_admin),
    db: Session = Depends(get_db_v2),
):
    version = _save_version(
        db,
        current_user,
        _normalized_settings(_default_version(db).settings),
        "settings_reset_to_default",
    )
    return {"active": _version_payload(version)}


@router.get("/settings/audit")
def list_settings_audit(
    limit: int = 100,
    db: Session = Depends(get_db_v2),
):
    rows = (
        db.query(BotSettingsAuditLogV2)
        .order_by(
            BotSettingsAuditLogV2.created_at.desc(),
            BotSettingsAuditLogV2.id.desc(),
        )
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return {
        "data": [
            {
                "id": row.id,
                "action": row.action,
                "actor_email": row.actor_email,
                "from_version_id": row.from_version_id,
                "to_version_id": row.to_version_id,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.get("/feedback")
async def list_feedback_for_bot_management(
    limit: int = 500,
    force_refresh: bool = False,
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    """Return the existing Airtable Feedbacks-table rows to All-Scopes admins."""
    return {"data": await _feedback_payload(
        airtable_service,
        limit=limit,
        force_refresh=force_refresh,
    )}


@router.get("/analytics/summary")
def get_bot_analytics_summary(
    date_from: date | None = None,
    date_to: date | None = None,
    actor_email: str | None = None,
    model_key: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    tool_name: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db_v2),
):
    where_sql, params = _analytics_filters(
        date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
    )
    try:
        totals = db.execute(text(f"""
            SELECT COUNT(*) AS run_count,
                   SUM(CASE WHEN runs.kind = 'request' THEN 1 ELSE 0 END) AS request_count,
                   SUM(CASE WHEN runs.kind = 'job' THEN 1 ELSE 0 END) AS background_job_count,
                   SUM(CASE WHEN runs.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded_count,
                   SUM(CASE WHEN runs.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                   SUM(CASE WHEN runs.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_count,
                   COALESCE(SUM(runs.elapsed_ms), 0) AS total_elapsed_ms,
                   AVG(runs.elapsed_ms) AS average_elapsed_ms
            FROM bot_telemetry_runs AS runs
            WHERE {where_sql}
        """), params).mappings().one()
        token_totals = db.execute(text(f"""
            SELECT COALESCE(SUM(CASE WHEN measurements.direction = 'input' THEN measurements.quantity ELSE 0 END), 0) AS input_tokens,
                   COALESCE(SUM(CASE WHEN measurements.direction = 'output' THEN measurements.quantity ELSE 0 END), 0) AS output_tokens,
                   COALESCE(SUM(CASE WHEN measurements.direction = 'total' THEN measurements.quantity ELSE 0 END), 0) AS total_tokens
            FROM bot_telemetry_runs AS runs
            JOIN bot_telemetry_spans AS spans ON spans.run_id = runs.id
            JOIN bot_token_measurements AS measurements ON measurements.span_id = spans.id
            WHERE {where_sql}
              AND measurements.metric = 'provider_usage'
              AND measurements.is_detail = FALSE
              AND measurements.category = 'base'
        """), params).mappings().one()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Bot analytics is temporarily unavailable.") from exc
    return {
        "totals": {key: int(value or 0) for key, value in totals.items() if key != "average_elapsed_ms"} | {
            "average_elapsed_ms": round(float(totals["average_elapsed_ms"] or 0), 2),
        },
        "tokens": {key: int(value or 0) for key, value in token_totals.items()},
    }


@router.get("/analytics/runs")
def list_bot_analytics_runs(
    page: int = 1,
    page_size: int = 50,
    date_from: date | None = None,
    date_to: date | None = None,
    actor_email: str | None = None,
    model_key: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    tool_name: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db_v2),
):
    bounded_page_size = min(max(page_size, 1), 200)
    bounded_page = max(page, 1)
    data, total = _analytics_rows(
        db, date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
        limit=bounded_page_size, offset=(bounded_page - 1) * bounded_page_size,
    )
    return {"data": data, "page": bounded_page, "page_size": bounded_page_size, "total": total}


@router.get("/analytics/chart-data")
def get_bot_analytics_chart_data(
    date_from: date | None = None,
    date_to: date | None = None,
    actor_email: str | None = None,
    model_key: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    tool_name: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db_v2),
):
    """Chart points only; no prompts, answers, provider payloads, or trace data."""
    return {"data": _analytics_chart_rows(
        db, date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
    )}


@router.get("/analytics/export")
def export_bot_analytics_csv(
    date_from: date | None = None,
    date_to: date | None = None,
    actor_email: str | None = None,
    model_key: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    tool_name: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db_v2),
):
    data, _ = _analytics_rows(
        db, date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
        limit=10_000, offset=0,
    )
    output = StringIO()
    columns = ["id", "actor_email", "kind", "job_name", "model_key", "configuration_version_id", "started_at", "finished_at", "elapsed_ms", "status", "error_code", "span_count"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(data)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bot-analytics.csv"},
    )


@router.get("/analytics/export/xlsx")
async def export_bot_analytics_workbook(
    report: Literal["all", "usage_by_user", "time_of_day", "response_time", "negative_feedback"] = "all",
    date_from: date | None = None,
    date_to: date | None = None,
    actor_email: str | None = None,
    model_key: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    tool_name: str | None = None,
    search: str | None = None,
    time_zone: str | None = None,
    db: Session = Depends(get_db_v2),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    """Export native Excel charts with the non-content rows that support them."""
    runs, _ = _analytics_rows(
        db, date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
        limit=10_000, offset=0,
    )
    points = _analytics_chart_rows(
        db, date_from=date_from, date_to=date_to, actor_email=actor_email,
        model_key=model_key, status=status, kind=kind, tool_name=tool_name, search=search,
    )
    feedback_records = await airtable_service.list_feedbacks(limit=500) if report in ("all", "negative_feedback") else []
    workbook = _build_bot_analytics_workbook(
        report=report, points=points, runs=runs, feedback_records=feedback_records,
        viewer_time_zone=time_zone,
    )
    filename = "bot-analytics.xlsx" if report == "all" else f"bot-analytics-{report.replace('_', '-')}.xlsx"
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
