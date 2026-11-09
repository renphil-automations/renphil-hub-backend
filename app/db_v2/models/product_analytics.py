"""PostgreSQL persistence for Hub product-usage (click) analytics.

Deliberately separate from the Agent's `bot_telemetry_*` tables: these rows
describe what a person clicked in the Hub UI, never an Agent or MCP run, so
the two are stored, counted and exported independently.
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, DateTime, Index, Integer, String

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
    # Assigned by the server from a verified component placement or tab target.
    # Historical rows and interactions with no reliable page remain NULL.
    page_id = Column(String(128), nullable=True)
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


class HubProductAnalyticsArchiveV2(BaseV2):
    """The archive registry: one row per archive object the retention job
    has **committed** (see `product_analytics_archive`).

    An object in the bucket is an archive only if its key is here. The row is
    inserted in the same transaction that verified the object's bytes, held
    the Hub rows its records depend on, and deleted those records from the
    live tables; an upload that the job abandoned (deadline, connection loss,
    crash, rollback) may still produce an object, but never a row here, and
    anything that reads archives must start from this table, not from a
    bucket listing. Contains no personal data: keys, counts, id ranges,
    timestamps and digests.
    """

    __tablename__ = "hub_product_analytics_archives"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    key = Column(String(512), nullable=False, unique=True)
    environment = Column(String(16), nullable=False)
    table_name = Column(String(32), nullable=False)
    sha256 = Column(String(64), nullable=False)
    format_version = Column(Integer, nullable=False)
    records = Column(Integer, nullable=False)
    first_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    last_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    time_min = Column(DateTime(timezone=True), nullable=False)
    time_max = Column(DateTime(timezone=True), nullable=False)
    omitted_columns = Column(JSON, nullable=False, default=list)
    registered_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_hub_product_analytics_archives_env_table", "environment", "table_name", "registered_at"),
    )


class HubProductAnalyticsArchiveIdentityV2(BaseV2):
    """Exactly one row: the identity of this registry database, stamped as
    metadata (`registry-id`) on every object the archive job uploads.

    The singleton is enforced by the database: the primary key is the
    constant 1 (`CHECK (singleton = 1)`), so concurrent first-time
    initialisers conflict on the key and exactly one id wins; every caller
    then reads that one row. `bucket`/`prefix` record the storage this
    identity was first used with, so a cloned or restored database pointed at
    other storage fails closed instead of silently claiming objects there.
    The id never rotates on its own; see the runbook ("Registry identity")."""

    __tablename__ = "hub_product_analytics_archive_identity"

    singleton = Column(Integer, primary_key=True, default=1)
    id = Column(String(36), nullable=False, unique=True)
    bucket = Column(String(63), nullable=True)
    prefix = Column(String(256), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("singleton = 1", name="ck_hub_product_analytics_archive_identity_singleton"),)
