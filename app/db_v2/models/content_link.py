"""Durable external-link registry for Hub component content.

The component/link mapping uses the requested component foreign key. Services
remove mappings before component deletion so that orphaned resources can be
soft-deleted and later cleaned up by the scheduled external-resource worker.
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import deferred

from app.db_v2.database import BaseV2


class ContentLinkV2(BaseV2):
    """One distinct external resource, shared by any number of components."""

    __tablename__ = "content_links"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False, unique=True)
    provider = Column(String(255), nullable=False)
    resource_type = Column(String(128), nullable=False)

    # Component saves register URLs only. The scheduled worker owns provider
    # and file-type detection, visitation, ingestion, and content metadata.
    visited = Column(Boolean, nullable=False, default=False, index=True)
    ingested = Column(Boolean, nullable=False, default=False, index=True)
    # A new link waits for an explicit administrator decision. The scheduled
    # worker may ingest only rows explicitly enabled here; selecting off is
    # recorded as reviewed and later archived by that worker.
    ingestion_enabled = Column(Boolean, nullable=False, default=False, index=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    archived = Column(Boolean, nullable=False, default=False, index=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # Worker-owned access assessment. ``is_public`` refers only to an
    # unauthenticated/public provider permission; service-account access is
    # recorded independently because private shared files may still be usable.
    is_public = Column(Boolean, nullable=True, index=True)
    service_account_accessible = Column(Boolean, nullable=True, index=True)
    access_checked_at = Column(DateTime(timezone=True), nullable=True)
    revisit = Column(Boolean, nullable=False, default=False, index=True)
    # Worker-owned retry backoff for retryable failures. The worker skips
    # inspection until next_attempt_at; failure_count counts only failures
    # caused by the resource. An administrator retry or re-enable resets both.
    # Deferred, with a server-side default only, so component saves and other
    # registry writes keep working on a database that has not yet received
    # scripts/create_content_link_tables.py; Bot Management checks for them.
    ingestion_supported = deferred(Column(Boolean, nullable=True, server_default=text("NULL")))
    ingestion_block_reason = deferred(Column(Text, nullable=True, server_default=text("NULL")))
    failure_count = deferred(Column(Integer, nullable=False, server_default=text("0")))
    next_attempt_at = deferred(Column(DateTime(timezone=True), nullable=True, index=True, server_default=text("NULL")))
    # Worker-owned change validators from the last successful check (HTTP
    # ETag/Last-Modified, or the Drive md5Checksum/version and modifiedTime),
    # used to skip downloading and re-indexing unchanged documents.
    source_etag = deferred(Column(Text, nullable=True, server_default=text("NULL")))
    source_modified = deferred(Column(Text, nullable=True, server_default=text("NULL")))
    deleted = Column(Boolean, nullable=False, default=False, index=True)
    acl_refresh_required = Column(Boolean, nullable=False, default=True, index=True)
    reason = Column(Text, nullable=True)
    content_fingerprint = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_content_links_worker_queue", "deleted", "acl_refresh_required", "visited", "revisit"),
    )
    # Never add RETURNING for server defaults (the deferred backoff columns may
    # not exist yet on a database awaiting its migration).
    __mapper_args__ = {"eager_defaults": False}


class ComponentLinkMapV2(BaseV2):
    """Many-to-many map from live Hub components to external resources."""

    __tablename__ = "component_link_map"

    id = Column(Integer, primary_key=True, index=True)
    component_id = Column(Integer, ForeignKey("components.id", ondelete="CASCADE"), nullable=False, index=True)
    link_id = Column(Integer, ForeignKey("content_links.id", ondelete="CASCADE"), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("component_id", "link_id", name="uq_component_link_map_component_link"),
    )


class ContentLinkEventV2(BaseV2):
    """Bounded per-resource history: worker outcomes and administrator actions.

    No foreign key, so the audit trail outlives a cleaned-up registry row; the
    worker prunes it (latest 50 per resource, 180 days for removed resources).
    """

    __tablename__ = "content_link_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    link_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    source = Column(String(16), nullable=False)
    action = Column(String(64), nullable=False)
    actor_email = Column(String(320), nullable=True)
    outcome = Column(String(64), nullable=True)
    reason = Column(Text, nullable=True)
    worker_run_id = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_content_link_events_link_created", "link_id", "created_at"),
    )


class ExternalWorkerRunV2(BaseV2):
    """One live run of the scheduled external-resource worker."""

    __tablename__ = "external_worker_runs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    finished_at = Column(DateTime(timezone=True), nullable=True)
    mode = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    selected = Column(Integer, nullable=True)
    outcomes = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    stopped_reason = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    host = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_external_worker_runs_started_at", "started_at"),
    )
