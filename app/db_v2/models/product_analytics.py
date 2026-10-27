"""PostgreSQL persistence for Hub product-usage (click) analytics.

Deliberately separate from the Agent's `bot_telemetry_*` tables: these rows
describe what a person clicked in the Hub UI, never an Agent or MCP run, so
the two are stored, counted and exported independently.
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, Integer, String

from app.db_v2.database import BaseV2


class HubProductEventV2(BaseV2):
    """One successful, user-initiated Hub interaction (see PRODUCT_ANALYTICS_EVENTS.md)."""

    __tablename__ = "hub_product_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    # Client-generated UUID. Unique, so a retried batch is ingested once.
    event_id = Column(String(36), nullable=False, unique=True)
    event_name = Column(String(64), nullable=False)
    # Derived server-side from `event_name` via the catalog, never sent by the client.
    component_type = Column(String(48), nullable=False)
    action = Column(String(48), nullable=False)
    # `click` (a deliberate user click) or `default` (a selection that click opened).
    trigger = Column(String(16), nullable=False)
    # Links the default selections a click opened back to that click's event_id.
    cascade_id = Column(String(36), nullable=True)
    hub_user_id = Column(Integer, nullable=False)
    actor_email = Column(String(320), nullable=False)
    environment = Column(String(16), nullable=False)
    # The route TEMPLATE (`/:nav_tab/:tab`, `/admin/bot-management`), never
    # the path itself: Hub URLs carry slugs derived from tab titles.
    page_route = Column(String(256), nullable=False)
    component_id = Column(String(128), nullable=True)
    # An opaque stable id (tab/card/thread id) or a catalog code such as
    # `pending`/`history`. Never a title or label: those are admin- or
    # user-authored text and can contain personal information.
    target_id = Column(String(128), nullable=True)
    properties = Column(JSON, nullable=False, default=dict)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_hub_product_events_occurred_at", "occurred_at"),
        Index("ix_hub_product_events_name_occurred", "event_name", "occurred_at"),
        Index("ix_hub_product_events_actor_occurred", "actor_email", "occurred_at"),
        Index("ix_hub_product_events_component_occurred", "component_type", "occurred_at"),
    )
