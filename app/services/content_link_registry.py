"""Stage 1 registry maintenance for external URLs authored in Hub components.

This module never downloads a URL or writes to Qdrant. It only makes the
PostgreSQL work queue accurately reflect committed Hub component content.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.external_resource_policy import resource_policy
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.content_link import ComponentLinkMapV2, ContentLinkV2
from app.db_v2.models.page_content import PageContentV2


_URL_PATTERN = re.compile(r"https?://[^\s<>\]})\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?"
# Airtable-backed widgets already index their approved live-data content
# through the Airtable ingestion path. Their configuration URLs are not Hub
# authored external resources and must never enter this registry.
_EXCLUDED_COMPONENT_TYPES = frozenset({"airtable", "airtable_metric", "chart"})
# Schema added for the external worker by the Alembic revision
# 0002_external_worker (or scripts/create_content_link_tables.py).
RETRY_BACKOFF_COLUMNS = ("failure_count", "next_attempt_at")
SOURCE_VALIDATOR_COLUMNS = ("source_etag", "source_modified")
WORKER_HISTORY_TABLES = ("content_link_events", "external_worker_runs")
_retry_backoff_columns_confirmed = False
_worker_history_confirmed = False
_retry_backoff_warning_logged = False
logger = logging.getLogger(__name__)


def _inspect_worker_schema(db: Session) -> tuple[bool, bool]:
    inspector = sqlalchemy_inspect(db.connection())
    columns = {column["name"] for column in inspector.get_columns("content_links")}
    backoff = all(name in columns for name in RETRY_BACKOFF_COLUMNS)
    history = backoff and all(name in columns for name in SOURCE_VALIDATOR_COLUMNS) and all(
        inspector.has_table(table) for table in WORKER_HISTORY_TABLES
    )
    return backoff, history


def _check_worker_schema(db: Session) -> tuple[bool, bool]:
    """(backoff columns, full worker history schema), positive answers cached.

    A missing migration is re-checked on each call, so applying it needs no
    backend restart; the Hub keeps working meanwhile and hides these details.
    """
    global _retry_backoff_columns_confirmed, _worker_history_confirmed, _retry_backoff_warning_logged
    if _worker_history_confirmed:
        return True, True
    try:
        backoff, history = _inspect_worker_schema(db)
    except Exception:
        logger.warning("Could not inspect the external-worker registry schema.", exc_info=True)
        return _retry_backoff_columns_confirmed, False
    _retry_backoff_columns_confirmed = _retry_backoff_columns_confirmed or backoff
    _worker_history_confirmed = history
    if not history and not _retry_backoff_warning_logged:
        _retry_backoff_warning_logged = True
        logger.warning(
            "The external-worker schema is not fully applied (needs %s, %s and tables %s). Run "
            "'alembic upgrade head'; retry details and worker history are hidden until then.",
            ", ".join(RETRY_BACKOFF_COLUMNS), ", ".join(SOURCE_VALIDATOR_COLUMNS), ", ".join(WORKER_HISTORY_TABLES),
        )
    return _retry_backoff_columns_confirmed or backoff, history


def retry_backoff_columns_available(db: Session) -> bool:
    """Whether content_links has the worker backoff columns."""
    return _check_worker_schema(db)[0]


def worker_history_available(db: Session) -> bool:
    """Whether the worker run log and per-resource history tables exist."""
    return _check_worker_schema(db)[1]


_policy_schema_confirmed = False


def _policy_columns_available(db: Session) -> bool:
    global _policy_schema_confirmed
    if not _policy_schema_confirmed:
        columns = {column["name"] for column in sqlalchemy_inspect(db.connection()).get_columns("content_links")}
        _policy_schema_confirmed = {"ingestion_supported", "ingestion_block_reason"}.issubset(columns)
    return _policy_schema_confirmed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _urls(value: object, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        for match in _URL_PATTERN.finditer(value):
            yield match.group(0).rstrip(_TRAILING_PUNCTUATION), path
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _urls(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _urls(item, f"{path}.{key}")


def _internal_hosts() -> set[str]:
    try:
        origins = get_settings().ALLOWED_ORIGINS
    except Exception:
        return set()
    hosts: set[str] = set()
    for origin in origins:
        try:
            host = (urlsplit(str(origin)).hostname or "").lower()
        except ValueError:
            host = ""
        if host:
            hosts.add(host)
    return hosts


def _normalized_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    # A fragment only points to a location inside the same resource; it must
    # not create a second external-document work item.
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _component_content(db: Session, component: ComponentV2) -> object:
    if component.page_content_id is None:
        return None
    row = db.query(PageContentV2).filter(PageContentV2.id == component.page_content_id).first()
    return row.content if row is not None else None


def _remove_component_link_maps(db: Session, component_id: int) -> int:
    """Remove a component's registry associations and tombstone new orphans."""
    now = _now()
    link_ids = [
        row.link_id
        for row in db.query(ComponentLinkMapV2)
        .filter(ComponentLinkMapV2.component_id == component_id)
        .all()
    ]
    removed = (
        db.query(ComponentLinkMapV2)
        .filter(ComponentLinkMapV2.component_id == component_id)
        .delete(synchronize_session=False)
    )
    db.flush()
    for link_id in link_ids:
        if db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.link_id == link_id).first() is None:
            link = db.query(ContentLinkV2).filter(ContentLinkV2.id == link_id).first()
            if link is not None:
                link.deleted = True
                link.deleted_at = now
                link.acl_refresh_required = True
                link.updated_at = now
    return removed


def sync_component_links(db: Session, component: ComponentV2) -> int:
    """Upsert current external links and soft-delete links no longer authored.

    The caller owns the transaction. This must run before the component write
    commits so a successful save and its registry state are atomic.
    """
    if (component.type or "").strip().lower() in _EXCLUDED_COMPONENT_TYPES:
        # A previously registered widget may be converted to an Airtable
        # component, or older builds may already have captured its source URL.
        # Remove those associations on save rather than letting stale entries
        # appear for administrators or reach the external worker.
        _remove_component_link_maps(db, component.id)
        return 0

    authored = {
        "title": component.title,
        "description": component.description,
        "props": component.props,
        "content": _component_content(db, component),
    }
    if (component.type or "").strip().lower() == "form":
        # Form configuration is only used for embedding. Legacy page content
        # may still contain embed URLs or a pending form configuration.
        authored = {"title": component.title, "description": component.description}
    internal_hosts = _internal_hosts()
    found: dict[str, dict[str, Any]] = {}
    for url, path in _urls(authored):
        normalized = _normalized_url(url)
        if normalized is None:
            continue
        try:
            host = (urlsplit(normalized).hostname or "").lower()
        except ValueError:
            continue
        if host in internal_hosts:
            continue
        current = found.setdefault(normalized, {"url": url, "paths": []})
        current["paths"].append(path)

    existing_maps = db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.component_id == component.id).all()
    existing = {row.normalized_url: row for row in db.query(ContentLinkV2).join(ComponentLinkMapV2, ComponentLinkMapV2.link_id == ContentLinkV2.id).filter(ComponentLinkMapV2.component_id == component.id).all()}
    now = _now()
    policy_ready = _policy_columns_available(db)
    for normalized, item in found.items():
        row = existing.pop(normalized, None)
        if row is None:
            row = db.query(ContentLinkV2).filter(ContentLinkV2.normalized_url == normalized).first()
        if row is None:
            row = ContentLinkV2(
                normalized_url=normalized,
                url=str(item["url"]),
                provider="Pending detection",
                resource_type="Pending detection",
                created_at=now,
            )
            db.add(row)
        else:
            # The worker owns the detected provider and MIME-derived type. A
            # component edit may update the authored spelling of a URL but
            # must never erase worker results.
            row.url = str(item["url"])
        if policy_ready and (row.id is None or row.ingestion_supported is None):
            policy = resource_policy(row.url, row.resource_type, row.reason)
            row.ingestion_supported = policy.supported
            row.ingestion_block_reason = policy.reason if policy.supported is False else None
        row.deleted = False
        row.deleted_at = None
        row.acl_refresh_required = True
        row.updated_at = now
        if not any(mapping.link_id == row.id for mapping in existing_maps):
            db.flush()
            db.add(ComponentLinkMapV2(component_id=component.id, link_id=row.id))

    for row in existing.values():
        db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.component_id == component.id, ComponentLinkMapV2.link_id == row.id).delete(synchronize_session=False)
        if db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.link_id == row.id).first() is None:
            row.deleted = True; row.deleted_at = now; row.acl_refresh_required = True; row.updated_at = now
    return len(found)


def mark_component_links_deleted(db: Session, component_id: int) -> int:
    """Preserve deletion work for the future external-resource worker."""
    return _remove_component_link_maps(db, component_id)


def mark_components_acl_refresh(db: Session, component_ids: Iterable[int]) -> int:
    """Queue linked resources for metadata/ACL reconciliation after a grant change."""
    ids = sorted({int(component_id) for component_id in component_ids})
    if not ids:
        return 0
    link_ids = [
        link_id
        for (link_id,) in (
            db.query(ComponentLinkMapV2.link_id)
            .filter(ComponentLinkMapV2.component_id.in_(ids))
            .distinct()
            .all()
        )
    ]
    if not link_ids:
        return 0
    return (
        db.query(ContentLinkV2)
        .filter(ContentLinkV2.id.in_(link_ids), ContentLinkV2.deleted.is_(False))
        .update({
            ContentLinkV2.acl_refresh_required: True,
            ContentLinkV2.updated_at: _now(),
        }, synchronize_session=False)
    )
