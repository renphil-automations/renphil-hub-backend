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
    # Standard View As remains attributed to the real authenticated actor.
    # These optional, server-verified keys describe the adopted role/scope
    # only for restricted operational investigation; they are never returned
    # by the product analytics APIs or exports. Anonymous View As writes no
    # analytics row at all.
    view_as_role_key = Column(String(128), nullable=True)
    view_as_scope_key = Column(String(128), nullable=True)
    view_as_context = Column(JSON, nullable=True)
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


class HubProductEngagementV2(BaseV2):
    """A bounded interval of *active* time spent on one Hub page.

    This intentionally contains no page title, URL, component label or free
    text.  The browser stops an interval as soon as its tab is hidden, so
    background, locked-screen and idle-away time is not represented as usage.
    """

    __tablename__ = "hub_product_engagements"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    # Client-generated UUID; retries of a pagehide/visibility flush are safe.
    engagement_id = Column(String(36), nullable=False, unique=True)
    hub_user_id = Column(Integer, nullable=False)
    actor_email = Column(String(320), nullable=False)
    environment = Column(String(16), nullable=False)
    # Same hidden simulation context as HubProductEventV2. It is not an
    # impersonated user: actor_email always remains the real caller.
    view_as_role_key = Column(String(128), nullable=True)
    view_as_scope_key = Column(String(128), nullable=True)
    view_as_context = Column(JSON, nullable=True)
    # Same privacy-safe route template used by HubProductEventV2.
    page_route = Column(String(256), nullable=False)
    # Verified, opaque bindings for the navigation tree and the tab that was
    # actually visible. They let an editor receive time only for pages they
    # can still edit; routes alone are deliberately not sufficient for that.
    nav_tab_id = Column(String(128), nullable=True)
    tab_id = Column(String(128), nullable=True)
    active_seconds = Column(Integer, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_hub_product_engagements_ended_at", "ended_at"),
        Index("ix_hub_product_engagements_environment_ended", "environment", "ended_at"),
        Index("ix_hub_product_engagements_actor_ended", "actor_email", "ended_at"),
        Index("ix_hub_product_engagements_route_ended", "page_route", "ended_at"),
        Index("ix_hub_product_engagements_nav_ended", "nav_tab_id", "ended_at"),
        Index("ix_hub_product_engagements_tab_ended", "tab_id", "ended_at"),
    )
