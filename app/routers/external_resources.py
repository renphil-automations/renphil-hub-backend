"""All Scopes administration for the External Resources worker."""

import csv
import re
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.external_resource_policy import resource_policy
from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.routers.bot_management import require_bot_management_admin

Db = Annotated[Session, Depends(get_db_v2)]


OCR_NOTE_CODES = frozenset({"ocr_date_corrected", "ocr_implausible_date", "ocr_unreadable_text", "ocr_page_rotated"})


def _environment() -> str:
    environment = get_settings().hub_environment
    if environment not in {"local", "sandbox", "production"}:
        raise HTTPException(503, "External Resources environment is not configured.")
    return environment


def _real_session(request: Request) -> None:
    if "anonymous" in request.query_params or any(
        key.lower().replace("-", "_").removeprefix("x_").startswith("view_as")
        for key in (*request.query_params, *request.headers)
    ):
        raise HTTPException(403, "External Resources is unavailable in View As.")


# A schema that was found complete is remembered for a short time per database engine, so a request does not
# repeat the information_schema reflection queries. Only a positive result is cached: a missing schema is
# re-checked every time, and the setup script's changes are picked up within the window.
_SCHEMA_OK_SECONDS = 60.0
_schema_ok: dict[int, float] = {}


def _schema_ready(db: Session) -> bool:
    key = id(db.get_bind())
    checked = _schema_ok.get(key)
    if checked is not None and time.monotonic() - checked < _SCHEMA_OK_SECONDS:
        return True
    schema = inspect(db.connection())
    if not schema.has_table("external_worker_control") or not schema.has_table("external_resource_component_map"):
        return False
    columns = {column["name"] for column in schema.get_columns("content_links")}
    if not {"ingestion_supported", "ingestion_block_reason"}.issubset(columns):
        return False
    _schema_ok[key] = time.monotonic()
    return True


def _control(db: Session, *, lock: bool = False):
    if not _schema_ready(db):
        return None
    query = """SELECT c.environment, v.id AS version_id, v.paused, v.created_at
               FROM external_worker_control c
               JOIN external_worker_setting_versions v ON v.id = c.version_id
               WHERE c.singleton"""
    return db.execute(text(query + (" FOR UPDATE OF c" if lock else ""))).mappings().one_or_none()


def _ready(db: Db) -> str:
    environment = _environment()
    control = _control(db)
    if control is None:
        raise HTTPException(503, "External Resources schema setup is required.")
    if control["environment"] != environment:
        raise HTTPException(503, "External Resources database belongs to a different environment.")
    return environment


router = APIRouter(
    prefix="/v2/external-resources",
    tags=["External Resources"],
    dependencies=[Depends(require_bot_management_admin), Depends(_real_session)],
)
Environment = Annotated[str, Depends(_ready)]
Actor = Annotated[CurrentHubUser, Depends(get_current_hub_user)]


def _safe_link(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme not in {"https", "http"} or not host:
            return "Unavailable"
        netloc = f"[{host}]" if ":" in host else host
        if parts.port is not None:
            netloc += f":{parts.port}"
        query = ""
        if host in {"drive.google.com", "www.drive.google.com", "docs.google.com"}:
            file_id = parse_qs(parts.query).get("id", [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
                query = urlencode({"id": file_id})
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    except ValueError:
        return "Unavailable"


class PauseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool
    expected_version_id: int = Field(gt=0)
    confirm: bool


class IngestionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ingestion_enabled: bool
    expected_updated_at: datetime


@router.get("/context")
def context(db: Db):
    environment = _environment()
    control = _control(db)
    return {
        "environment": environment,
        "migration_ready": bool(control and control["environment"] == environment),
        "misconfigured": bool(control and control["environment"] != environment),
    }


@router.get("/worker/retrieval")
def worker_retrieval(environment: Environment):
    settings = get_settings()
    url = (settings.AGENT_API_URL or "").strip().rstrip("/")
    token = (settings.AGENT_SYNC_TOKEN or "").strip()
    if not url or not token:
        return {"state": "unknown"}
    try:
        response = httpx.get(
            f"{url}/agent/external-documents/runtime-status",
            headers={"X-Sync-Token": token, "Accept": "application/json"},
            timeout=2.0,
        )
        response.raise_for_status()
        status = response.json()
    except (httpx.HTTPError, ValueError):
        return {"state": "unknown"}
    if not isinstance(status, dict):
        return {"state": "unknown"}
    if status.get("environment") != environment:
        return {"state": "misconfigured"}
    state = status.get("state")
    return {"state": state if state in ("on", "off", "misconfigured") else "unknown"}


def _indexed_or_reason(db: Session, link_id: int) -> str | None:
    """None when the registry says the document is indexed and active; otherwise the reason to show nothing."""
    row = db.execute(text("""
        SELECT ingested, ingestion_enabled, deleted, archived, ingestion_supported FROM content_links WHERE id = :id
    """), {"id": link_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Resource not found.")
    if not (row["ingested"] and row["ingestion_enabled"]) or row["deleted"] or row["archived"] or row["ingestion_supported"] is False:
        return "not_indexed"
    return None


def _agent_document_get(path: str, params: dict[str, object] | None = None) -> dict | None:
    """GET an Agent document endpoint with the shared service token. None when the Agent is not configured or
    cannot answer; details are never passed on."""
    settings = get_settings()
    url = (settings.AGENT_API_URL or "").strip().rstrip("/")
    token = (settings.AGENT_SYNC_TOKEN or "").strip()
    if not url or not token:
        return None
    try:
        reply = httpx.get(f"{url}/internal/external-documents/{path}", params=params,
                          headers={"X-Sync-Token": token, "Accept": "application/json"}, timeout=8.0)
        reply.raise_for_status()
        body = reply.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


@router.get("/resources/{link_id}/preview")
def resource_preview(link_id: int, response: Response, db: Db, environment: Environment):
    """What a document says, in a few lines, like a link preview. Only for a document the worker has indexed."""
    response.headers["Cache-Control"] = "private, no-store"
    reason = _indexed_or_reason(db, link_id)
    if reason:
        return {"available": False, "reason": reason}
    body = _agent_document_get(f"{link_id}/preview")
    if body is None:
        return {"available": False, "reason": "unavailable"}
    if not isinstance(body, dict) or body.get("available") is not True or not isinstance(body.get("excerpt"), str) or not body["excerpt"].strip():
        return {"available": False, "reason": "not_indexed"}
    title, sections = body.get("title"), body.get("sections")
    return {
        "available": True,
        "title": title.strip()[:200] if isinstance(title, str) and title.strip() else None,
        "excerpt": body["excerpt"].strip()[:1000],
        "sections": sections if isinstance(sections, int) and not isinstance(sections, bool) and sections > 0 else None,
    }


_CONTENT_TEXT_LIMIT = 20_000


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@router.get("/resources/{link_id}/content")
def resource_content(
    link_id: int, response: Response, db: Db, environment: Environment,
    start: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    version: Annotated[str | None, Query(pattern=r"^[0-9a-f]{16}$")] = None,
):
    """The rest of an indexed document's text, in order, one "Show more" step at a time. Same access and registry
    checks as the preview; the Agent re-checks the registry and returns only this document's current version."""
    response.headers["Cache-Control"] = "private, no-store"
    reason = _indexed_or_reason(db, link_id)
    if reason:
        return {"available": False, "reason": reason}
    body = _agent_document_get(f"{link_id}/content", {"start": start, **({"version": version} if version else {})})
    if body is None:
        return {"available": False, "reason": "unavailable"}
    current = body.get("version") if isinstance(body.get("version"), str) and re.fullmatch(r"[0-9a-f]{16}", body["version"]) else None
    if body.get("available") is True and body.get("changed") is True and current:
        return {"available": True, "changed": True, "version": current}
    text_value, sections, next_start = body.get("text"), _whole(body.get("sections")), body.get("next")
    if body.get("available") is not True or not current or not isinstance(text_value, str) or sections is None:
        return {"available": False, "reason": "not_indexed"}
    next_start = _whole(next_start)
    return {
        "available": True,
        "version": current,
        "start": start,
        "next": next_start if next_start is not None and start < next_start <= sections else None,
        "sections": sections,
        "text": text_value.strip()[:_CONTENT_TEXT_LIMIT],
    }


@router.get("/settings")
def settings(db: Db, environment: Environment):
    control = _control(db)
    return {**control, "environment": environment}


@router.put("/settings")
def update_settings(body: PauseUpdate, db: Db, environment: Environment, actor: Actor):
    if not body.confirm:
        raise HTTPException(422, "Confirm the Worker processing change.")
    control = _control(db, lock=True)
    if control["version_id"] != body.expected_version_id:
        raise HTTPException(409, "Worker settings changed. Refresh and try again.")
    if control["paused"] == body.paused:
        return control
    version = db.execute(text("""
        INSERT INTO external_worker_setting_versions (paused, actor_email)
        VALUES (:paused, :actor_email) RETURNING id AS version_id, paused, created_at
    """), {"paused": body.paused, "actor_email": actor.email}).mappings().one()
    db.execute(text("UPDATE external_worker_control SET version_id = :version_id WHERE singleton"), {"version_id": version["version_id"]})
    db.commit()
    return {**version, "environment": environment}


@router.get("/settings/audit")
def settings_audit(db: Db, environment: Environment, before_id: int | None = None):
    rows = db.execute(text("""
        SELECT id AS version_id, paused, actor_email, created_at
        FROM external_worker_setting_versions
        WHERE (CAST(:before_id AS BIGINT) IS NULL OR id < :before_id)
        ORDER BY id DESC LIMIT 50
    """), {"before_id": before_id}).mappings().all()
    return {"environment": environment, "data": rows}


_RESOURCE_FIELDS = """
    id, url, provider, resource_type, folder_parent_id, updated_at, ingestion_enabled, reviewed_at,
    ingested, visited, archived, deleted, acl_refresh_required, revisit,
    is_public, service_account_accessible, access_checked_at, failure_count, next_attempt_at,
    reason, ingestion_supported, ingestion_block_reason,
    (SELECT COUNT(*) FROM content_links child WHERE child.folder_parent_id = content_links.id AND NOT child.deleted) AS folder_children,
    (SELECT COUNT(*) FROM external_resource_component_map m WHERE m.link_id = content_links.id) AS component_count
"""

_OCCURRENCE_FIELDS = """
    c.id, c.title, c.type, c.link AS component_link, t.title AS page_title
"""
_OCCURRENCE_JOINS = """
    FROM external_resource_component_map m JOIN components c ON c.id = m.component_id
    LEFT JOIN gridstacks g ON g.id = c.gridstack_id
    LEFT JOIN tabs t ON t.id = g.parent_tab_id
"""
_FIRST_OCCURRENCE = f"""
    (SELECT row_to_json(occurrence) FROM (
        SELECT {_OCCURRENCE_FIELDS} {_OCCURRENCE_JOINS}
        WHERE m.link_id = content_links.id
        ORDER BY (NULLIF(BTRIM(c.link), '') IS NULL), c.id LIMIT 1
    ) occurrence) AS first_occurrence
"""


def _resource(row):
    policy = resource_policy(row["url"], row.get("resource_type"), row.get("reason"))
    supported = row.get("ingestion_supported")
    if policy.supported is False:
        supported = False
    elif supported is None:
        supported = policy.supported
    return {**row, "url": _safe_link(row["url"]), "file_type": policy.label,
            "ingestion_supported": supported, "file_type_confirmed": policy.confirmed,
            "ingestion_block_reason": (row.get("ingestion_block_reason") or policy.reason) if supported is False else None}


@router.get("/resources")
def resources(
    db: Db, environment: Environment,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    search: Annotated[str, Query(max_length=200)] = "",
    provider: Annotated[str, Query(max_length=64)] = "",
    decision: Annotated[str, Query(pattern="^(all|on|off|pending)$")] = "all",
):
    where = """WHERE (:provider = '' OR provider = :provider)
      AND (:search = '' OR split_part(split_part(url, '?', 1), '#', 1) ILIKE :pattern ESCAPE '\\'
           OR provider ILIKE :pattern ESCAPE '\\' OR resource_type ILIKE :pattern ESCAPE '\\'
           OR CAST(id AS TEXT) = :search OR CAST(folder_parent_id AS TEXT) = :search)
      AND (:decision = 'all' OR (:decision = 'on' AND ingestion_enabled)
           OR (:decision = 'off' AND reviewed_at IS NOT NULL AND NOT ingestion_enabled)
           OR (:decision = 'pending' AND reviewed_at IS NULL))"""
    params = {"provider": provider, "search": search, "pattern": "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
              "decision": decision, "limit": page_size, "offset": (page - 1) * page_size}
    rows = db.execute(text(f"""
        SELECT {_RESOURCE_FIELDS}, {_FIRST_OCCURRENCE} FROM content_links {where}
        ORDER BY updated_at DESC, id DESC LIMIT :limit OFFSET :offset
    """), params).mappings().all()
    total = db.execute(text(f"SELECT COUNT(*) FROM content_links {where}"), params).scalar_one()
    return {"environment": environment, "data": [_resource(row) for row in rows], "total": total}


@router.get("/resources/{link_id}")
def resource(link_id: int, db: Db, environment: Environment):
    row = db.execute(text(f"SELECT {_RESOURCE_FIELDS}, {_FIRST_OCCURRENCE} FROM content_links WHERE id = :id"),
                     {"id": link_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "External resource was not found.")
    return _resource(row)


@router.get("/resources/{link_id}/occurrences")
def occurrences(link_id: int, db: Db, environment: Environment):
    rows = db.execute(text(f"""
        SELECT {_OCCURRENCE_FIELDS} {_OCCURRENCE_JOINS}
        WHERE m.link_id = :id ORDER BY (NULLIF(BTRIM(c.link), '') IS NULL), c.id LIMIT 100
    """), {"id": link_id}).mappings().all()
    return {"data": rows}


@router.get("/resources/{link_id}/history")
def history(link_id: int, db: Db, environment: Environment, before_id: int | None = None):
    rows = db.execute(text("""
        SELECT id, created_at, source, action, outcome, failure_code, duration_ms, worker_run_id, actor_email
        FROM content_link_events
        WHERE link_id = :id AND (environment = :environment OR environment IS NULL)
          AND (CAST(:before_id AS BIGINT) IS NULL OR id < :before_id)
        ORDER BY id DESC LIMIT 50
    """), {"id": link_id, "environment": environment, "before_id": before_id}).mappings().all()
    return {"data": rows}


def _audit(db: Session, link_id: int, environment: str, action: str, actor: CurrentHubUser):
    db.execute(text("""
        INSERT INTO content_link_events (link_id, source, action, actor_email, environment)
        VALUES (:id, 'admin', :action, :email, :environment)
    """), {"id": link_id, "action": action, "email": actor.email, "environment": environment})


def _require_supported(db: Session, link_id: int) -> None:
    row = db.execute(text(
        "SELECT url, resource_type, reason, ingestion_supported, ingestion_block_reason "
        "FROM content_links WHERE id = :id FOR UPDATE"
    ), {"id": link_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "External resource was not found.")
    policy = resource_policy(row["url"], row["resource_type"], row["reason"])
    if row["ingestion_supported"] is False or policy.supported is False:
        raise HTTPException(422, row["ingestion_block_reason"] or policy.reason or "This file format cannot be ingested.")


@router.put("/resources/{link_id}/ingestion-decision")
def decide(link_id: int, body: IngestionDecision, db: Db, environment: Environment, actor: Actor):
    if body.ingestion_enabled:
        _require_supported(db, link_id)
    row = db.execute(text("""
        UPDATE content_links SET ingestion_enabled = :enabled, reviewed_at = NOW(), updated_at = clock_timestamp(),
           archived = CASE WHEN :enabled THEN FALSE ELSE archived END,
           archived_at = CASE WHEN :enabled THEN NULL ELSE archived_at END,
           revisit = :enabled, acl_refresh_required = acl_refresh_required OR :enabled,
           failure_count = CASE WHEN :enabled THEN 0 ELSE failure_count END,
           next_attempt_at = CASE WHEN :enabled THEN NULL ELSE next_attempt_at END
        WHERE id = :id AND NOT deleted AND updated_at = :expected_updated_at
        RETURNING id
    """), {"id": link_id, "enabled": body.ingestion_enabled,
           "expected_updated_at": body.expected_updated_at}).scalar_one_or_none()
    if row is None:
        raise HTTPException(409, "Resource changed or is pending removal. Refresh and try again.")
    _audit(db, link_id, environment, "ingestion_enabled" if body.ingestion_enabled else "ingestion_disabled", actor)
    db.commit()
    return {"id": link_id}


def _queue_check(link_id: int, db: Session, environment: str, actor: CurrentHubUser, *, rebuild: bool):
    _require_supported(db, link_id)
    row = db.execute(text("""
        UPDATE content_links SET revisit = TRUE, failure_count = 0, next_attempt_at = NULL, updated_at = clock_timestamp(),
          source_etag = CASE WHEN :rebuild THEN NULL ELSE source_etag END,
          source_modified = CASE WHEN :rebuild THEN NULL ELSE source_modified END,
          content_fingerprint = CASE WHEN :rebuild THEN NULL ELSE content_fingerprint END
        WHERE id = :id AND ingestion_enabled AND reviewed_at IS NOT NULL AND NOT archived AND NOT deleted
          AND EXISTS (SELECT 1 FROM external_resource_component_map WHERE link_id = :id)
        RETURNING id
    """), {"id": link_id, "rebuild": rebuild}).scalar_one_or_none()
    if row is None:
        raise HTTPException(409, "Only enabled resources with a live Hub occurrence can be checked.")
    _audit(db, link_id, environment, "rebuild_requested" if rebuild else "check_requested", actor)
    db.commit()
    return {"id": link_id}


@router.post("/resources/{link_id}/check")
def check(link_id: int, db: Db, environment: Environment, actor: Actor):
    return _queue_check(link_id, db, environment, actor, rebuild=False)


@router.post("/resources/{link_id}/rebuild")
def rebuild(link_id: int, db: Db, environment: Environment, actor: Actor):
    return _queue_check(link_id, db, environment, actor, rebuild=True)


@router.get("/worker/runs")
def runs(db: Db, environment: Environment, before_id: int | None = None):
    rows = db.execute(text("""
        SELECT id, started_at, finished_at, status, mode, selected, queue_depth, outcomes,
               CASE WHEN status = 'failed' THEN 'Run failed' END AS message
        FROM external_worker_runs
        WHERE environment = :environment AND (CAST(:before_id AS BIGINT) IS NULL OR id < :before_id)
        ORDER BY id DESC LIMIT 50
    """), {"environment": environment, "before_id": before_id}).mappings().all()
    return {"data": rows}


@router.get("/worker/status")
def worker_status(db: Db, environment: Environment):
    rows = runs(db, environment)["data"]
    latest = rows[0] if rows else None
    state = "never" if latest is None else latest["status"]
    if latest:
        age = datetime.now(timezone.utc) - (latest["finished_at"] or latest["started_at"])
        if state == "running" and age > timedelta(hours=6):
            state = "interrupted"
        elif state == "succeeded" and age > timedelta(hours=3):
            state = "stale"
    return {"environment": environment, "state": state, "latest": latest, "paused": _control(db)["paused"]}


@router.get("/analytics/trends")
def trends(db: Db, environment: Environment, date_from: date | None = None, date_to: date | None = None):
    end = date_to or datetime.now(timezone.utc).date()
    start = date_from or end - timedelta(days=29)
    if not 0 <= (end - start).days < 366:
        raise HTTPException(422, "Choose an analytics range of up to 366 days.")
    rows = db.execute(text("""
        SELECT day, outcome, SUM(event_count)::BIGINT AS event_count,
               CASE WHEN SUM(duration_count) > 0 THEN
                   SUM(duration_ms_sum)::DOUBLE PRECISION / SUM(duration_count) END AS average_duration_ms
        FROM external_worker_daily_metrics
        WHERE environment = :environment AND day BETWEEN :start AND :end
        GROUP BY day, outcome ORDER BY day, outcome
    """), {"environment": environment, "start": start, "end": end}).mappings().all()
    return {"environment": environment, "data": rows}


@router.get("/worker/runs/{run_id}")
def run_detail(run_id: int, db: Db, environment: Environment):
    row = db.execute(text("""
        SELECT id, started_at, finished_at, status, mode, selected, queue_depth, outcomes, config_snapshot
        FROM external_worker_runs WHERE id = :id AND environment = :environment
    """), {"id": run_id, "environment": environment}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Worker run was not found in this environment.")
    allowed = {"paused", "max_pdf_bytes", "pdf_max_pages", "ocr_max_pages", "ocr_timeout_seconds",
               "recheck_interval_hours", "retry_max_attempts", "settings_version_id"}
    config = row["config_snapshot"] if isinstance(row["config_snapshot"], dict) else {}
    return {**row, "config_snapshot": {
        key: config[key] for key in allowed if key in config and isinstance(config[key], (int, bool))
    }}


@router.get("/worker/runs/{run_id}/items")
def run_items(run_id: int, db: Db, environment: Environment, after_id: int = 0):
    run_detail(run_id, db, environment)
    rows = db.execute(text("""
        SELECT item.link_id, item.initial_status, item.action, item.outcome, item.updated_at,
               to_jsonb(item) ->> 'ocr_note' AS ocr_note,
               links.url AS source_url
        FROM external_worker_run_items AS item
        LEFT JOIN content_links AS links ON links.id = item.link_id
        WHERE item.run_id = :id AND item.link_id > :after_id
        ORDER BY item.link_id LIMIT 100
    """), {"id": run_id, "after_id": max(0, after_id)}).mappings().all()
    return {"data": [{
        **row,
        "source_url": _safe_link(row["source_url"]) if row["source_url"] else None,
        # Only known codes leave the API; anything else is dropped.
        "ocr_note": row["ocr_note"] if row["ocr_note"] in OCR_NOTE_CODES else None,
    } for row in rows]}


@router.get("/worker/runs/{run_id}/events")
def run_events(run_id: int, db: Db, environment: Environment, before_id: int | None = None):
    run_detail(run_id, db, environment)
    rows = db.execute(text("""
        SELECT events.id, events.link_id, events.created_at, events.action, events.outcome,
               events.failure_code, events.duration_ms, events.initial_status, links.url AS source_url
        FROM content_link_events AS events
        LEFT JOIN content_links AS links ON links.id = events.link_id
        WHERE events.worker_run_id = :id AND (events.environment = :environment OR events.environment IS NULL) AND events.source = 'worker'
          AND (CAST(:before_id AS BIGINT) IS NULL OR events.id < :before_id)
        ORDER BY events.id DESC LIMIT 100
    """), {"id": run_id, "environment": environment, "before_id": before_id}).mappings().all()
    return {"data": [
        {**row, "source_url": _safe_link(row["source_url"]) if row["source_url"] else None}
        for row in rows
    ]}


@router.get("/analytics/summary")
def analytics_summary(db: Db, environment: Environment):
    queue = db.execute(text("""
        SELECT COUNT(*) AS resources,
               COUNT(*) FILTER (WHERE NOT deleted AND NOT archived AND reviewed_at IS NULL AND ingestion_supported IS DISTINCT FROM FALSE) AS awaiting_decision,
               COUNT(*) FILTER (WHERE NOT deleted AND NOT archived AND ingestion_enabled AND NOT visited AND ingestion_supported IS DISTINCT FROM FALSE) AS awaiting_check,
               COUNT(*) FILTER (WHERE NOT deleted AND ingestion_enabled AND next_attempt_at > NOW() AND ingestion_supported IS DISTINCT FROM FALSE) AS waiting_retry,
               COUNT(*) FILTER (WHERE NOT deleted AND NOT archived AND reviewed_at IS NOT NULL AND NOT ingestion_enabled) AS archive_pending,
               COUNT(*) FILTER (WHERE deleted) AS removal_pending,
               COUNT(*) FILTER (WHERE NOT deleted AND NOT archived AND ingestion_enabled AND ingested AND acl_refresh_required AND ingestion_supported IS DISTINCT FROM FALSE) AS acl_pending
        FROM content_links
    """)).mappings().one()
    return {"environment": environment, **queue}


@router.get("/analytics/failures")
def failures(db: Db, environment: Environment):
    rows = db.execute(text("""
        SELECT failure_code, SUM(event_count)::BIGINT AS event_count
        FROM external_worker_daily_metrics
        WHERE environment = :environment AND day >= (NOW() AT TIME ZONE 'UTC')::date - 29
          AND failure_code <> ''
        GROUP BY failure_code ORDER BY event_count DESC, failure_code LIMIT 100
    """), {"environment": environment}).mappings().all()
    return {"environment": environment, "data": rows}


def _csv_cell(value):
    if isinstance(value, str) and (value[:1] in {"\t", "\r", "\n"} or value.lstrip()[:1] in {"=", "+", "-", "@"}):
        return "'" + value
    return value


_EXPORT_PAGE = 2000
_EXPORT_MAX_ROWS = 500_000
_EXPORT_COLUMNS = {
    "runs": ["id", "environment", "started_at", "finished_at", "status", "mode", "selected", "queue_depth"],
    "events": ["id", "environment", "link_id", "created_at", "action", "outcome", "failure_code", "duration_ms", "worker_run_id"],
    "trends": ["day", "outcome", "event_count", "average_duration_ms", "environment"],
}


def _export_rows(db: Session, dataset: str, environment: str, query: str, params: dict):
    """Every row of a dataset, read in id-ordered pages so no single query is unbounded."""
    rows: list = []
    last_id = None
    while True:
        page = db.execute(text(query), {**params, "last_id": last_id, "limit": _EXPORT_PAGE}).mappings().all()
        rows.extend(page)
        if len(rows) > _EXPORT_MAX_ROWS:
            raise HTTPException(422, f"The {dataset} export is larger than {_EXPORT_MAX_ROWS:,} rows. Narrow it by date or contact the Hub team.")
        if len(page) < _EXPORT_PAGE:
            return rows
        last_id = page[-1]["id"]


@router.get("/analytics/export")
def export(db: Db, environment: Environment, dataset: Literal["resources", "runs", "events", "trends"] = "trends"):
    if dataset == "resources":
        raw = _export_rows(db, dataset, environment, f"""
            SELECT {_RESOURCE_FIELDS} FROM content_links WHERE (CAST(:last_id AS BIGINT) IS NULL OR id < :last_id)
            ORDER BY id DESC LIMIT :limit""", {})
        rows = [{**_resource(row), "environment": environment} for row in raw]
        columns = list(rows[0]) if rows else None
    elif dataset == "runs":
        rows = _export_rows(db, dataset, environment, """
            SELECT id, environment, started_at, finished_at, status, mode, selected, queue_depth
            FROM external_worker_runs WHERE environment = :environment AND (CAST(:last_id AS BIGINT) IS NULL OR id < :last_id)
            ORDER BY id DESC LIMIT :limit""", {"environment": environment})
        columns = _EXPORT_COLUMNS["runs"]
    elif dataset == "events":
        rows = _export_rows(db, dataset, environment, """
            SELECT id, environment, link_id, created_at, action, outcome, failure_code, duration_ms, worker_run_id
            FROM content_link_events WHERE (environment = :environment OR environment IS NULL) AND source = 'worker'
              AND (CAST(:last_id AS BIGINT) IS NULL OR id < :last_id)
            ORDER BY id DESC LIMIT :limit""", {"environment": environment})
        columns = _EXPORT_COLUMNS["events"]
    else:
        rows = [{**row, "environment": environment} for row in trends(db, environment)["data"]]
        columns = _EXPORT_COLUMNS["trends"]
    buffer = StringIO()
    writer = csv.writer(buffer)
    if columns is None:  # no resources yet: still give the header so the file is a valid, empty table
        columns = ["id", "url", "environment"]
    writer.writerow(columns)
    writer.writerows([_csv_cell(row.get(key)) for key in columns] for row in rows)
    return Response(
        buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="external-resources-{environment}-{dataset}.csv"',
                 "Cache-Control": "private, no-store"},
    )
