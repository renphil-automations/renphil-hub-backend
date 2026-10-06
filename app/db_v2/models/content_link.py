"""Durable external-link registry for Hub component content.

The component/link mapping uses the requested component foreign key. Services
remove mappings before component deletion so that orphaned resources can be
soft-deleted and later cleaned up by the scheduled external-resource worker.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.db_v2.database import BaseV2


class ContentLinkV2(BaseV2):
    """One distinct external resource, shared by any number of components."""

    __tablename__ = "content_links"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False, unique=True)
    provider = Column(String(255), nullable=False)
    resource_type = Column(String(128), nullable=False)

    # Stage 1 state. Later scheduled workers own visited/ingested/revisit and
    # may fill content_fingerprint/reason; component writes never fetch URLs.
    visited = Column(Boolean, nullable=False, default=False, index=True)
    ingested = Column(Boolean, nullable=False, default=False, index=True)
    revisit = Column(Boolean, nullable=False, default=False, index=True)
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


class ComponentLinkMapV2(BaseV2):
    """Many-to-many map from live Hub components to external resources."""

    __tablename__ = "component_link_map"

    id = Column(Integer, primary_key=True, index=True)
    component_id = Column(Integer, ForeignKey("components.id", ondelete="CASCADE"), nullable=False, index=True)
    link_id = Column(Integer, ForeignKey("content_links.id", ondelete="CASCADE"), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("component_id", "link_id", name="uq_component_link_map_component_link"),
    )
