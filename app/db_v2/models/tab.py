from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class TabV2(BaseV2):
    """Root-level tabs in the new schema. Any tab that had a parent in the
    old schema (nav-tree child, Super BlockNote sub-tab, or Super GridStack
    sub-tab) becomes a GridstackV2 row instead, see gridstack.py — that is
    a wholly separate nesting axis from parent_tab_id below.

    parent_tab_id (self-referential FK, nullable) is a distinct, one-level
    nesting axis: "tab variants" — a full sibling TabV2 (own document_id,
    own root GridstackV2/canvas, own access_control) selected via a pill
    row rather than shown in the main tab bar. A tab whose own
    parent_tab_id is set may never itself be used as a variant's parent
    (enforced in gridstack_service.py, not by a DB constraint) — depth is
    strictly one level. No ORM relationship() here, matching
    GridstackV2.parent_id / ComponentV2.super_blocknote_id's existing
    precedent — traversal is always a plain query, and same-table delete
    ordering is handled explicitly wherever deletes happen."""

    __tablename__ = "tabs"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    title = Column(String(255), nullable=True)
    order = Column(Integer, nullable=True)

    access_control = Column(JSONB, nullable=True)

    locked = Column(Boolean, nullable=True, default=False)
    locked_by = Column(String(255), nullable=True, default="")

    # Added by scripts/migrate_tab_locked_at.py (plan §6.6 Fix 2). NULL on
    # every row locked before this column existed — including the live
    # tab 42, locked since 2026-08-07 — and NULL is treated as "already
    # stale" (see gridstack_service.is_lock_stale), not backfilled from
    # updated_at. That is a deliberate simplicity choice, not an oversight:
    # a NULL lock predates the whole TTL concept, so there is no real
    # acquisition time to recover, and treating it as fresh would leave
    # exactly the stranded locks this fix exists to release.
    locked_at = Column(DateTime, nullable=True)

    # Added by scripts/migrate_lock_propagation_columns.py
    # (plan_lock_propagation_2026-09-08.md §3.1). Opaque uuid4().hex, minted
    # on acquire, rotated on force-takeover, cleared on release. Stored
    # rather than derived (holder + locked_at) because it must be
    # INVALIDATABLE: a derived token would revive after a takeover-then-
    # re-lock, since holder/timestamp alone can't distinguish "this session"
    # from "a later session with the same holder". NULL on every
    # pre-existing locked row (no backfill) — validates as "no live
    # session", the same fail-open-to-stale posture `locked_at IS NULL`
    # already has. See app/services/edit_lock_service.py.
    lock_token = Column(String(64), nullable=True)

    source_link = Column(String(255), nullable=True)

    parent_tab_id = Column(Integer, ForeignKey("tabs.id"), nullable=True, index=True)

    # The nav tab (parent layer above root tabs, see NavTabV2) this tab
    # belongs to. Nullable so the migration can add the column before
    # backfilling with no NOT NULL window; after the backfill a NULL is an
    # anomaly the service layer never produces. A variant carries the same
    # nav_tab_id as its parent tab (create_tab_variant_v2) — it is not an
    # independent placement.
    nav_tab_id = Column(Integer, ForeignKey("nav_tabs.id"), nullable=True, index=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
