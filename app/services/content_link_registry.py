"""Stage 1 registry maintenance for external URLs authored in Hub components.

This module never downloads a URL or writes to Qdrant. It only makes the
PostgreSQL work queue accurately reflect committed Hub component content.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.content_link import ComponentLinkMapV2, ContentLinkV2
from app.db_v2.models.page_content import PageContentV2


_URL_PATTERN = re.compile(r"https?://[^\s<>\]})\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?"


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
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    # A fragment only points to a location inside the same resource; it must
    # not create a second external-document work item.
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _component_content(db: Session, component: ComponentV2) -> object:
    if component.page_content_id is None:
        return None
    row = db.query(PageContentV2).filter(PageContentV2.id == component.page_content_id).first()
    return row.content if row is not None else None


def sync_component_links(db: Session, component: ComponentV2) -> int:
    """Upsert current external links and soft-delete links no longer authored.

    The caller owns the transaction. This must run before the component write
    commits so a successful save and its registry state are atomic.
    """
    authored = {
        "description": component.description,
        "props": component.props,
        "content": _component_content(db, component),
    }
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
    now = _now()
    link_ids = [row.link_id for row in db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.component_id == component_id).all()]
    deleted = db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.component_id == component_id).delete(synchronize_session=False)
    db.flush()
    for link_id in link_ids:
        if db.query(ComponentLinkMapV2).filter(ComponentLinkMapV2.link_id == link_id).first() is None:
            link = db.query(ContentLinkV2).filter(ContentLinkV2.id == link_id).first()
            if link is not None:
                link.deleted = True; link.deleted_at = now; link.acl_refresh_required = True; link.updated_at = now
    return deleted


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
