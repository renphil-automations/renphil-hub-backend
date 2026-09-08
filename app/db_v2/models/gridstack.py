from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class GridstackV2(BaseV2):
    """A canvas. parent_id NULL = the root canvas of its tab; non-null =
    a sub-tab's canvas nested under another gridstack (this is how every
    non-root old Tab — nav-tree child, Super BlockNote sub-tab, or Super
    GridStack sub-tab — is represented in the new schema). parent_tab_id is
    denormalized to the ultimate root tab at every depth, so "all gridstacks
    under tab X" is a flat `parent_tab_id = X` query regardless of nesting depth.

    settings (JSONB) is a catch-all bucket holding the Super GridStack
    tab-bar config, e.g. {"sgs": {"tabBarPosition": ...}}. A sub-tab's own
    access_control used to live here too (a sub-tab is no longer its own Tab
    row and had nowhere else to keep an independent viewer restriction), but
    has moved onto its representation component's own access_control column
    instead — see ComponentV2.current_grid_id and
    migrate_subtab_access_control_to_components.py.
    """

    __tablename__ = "gridstacks"

    id = Column(Integer, primary_key=True, index=True)

    # Stable public address for this node (root or nested sub-tab) — every
    # addressable "tab" the frontend talks to needs one, same as Tab.document_id.
    # For a root gridstack this is set equal to its TabV2.document_id.
    document_id = Column(String(255), nullable=True, index=True)

    name = Column(String(255), nullable=True)
    settings = Column(JSONB, nullable=True)
    position = Column(Integer, nullable=True)

    parent_id = Column(Integer, ForeignKey("gridstacks.id"), nullable=True, index=True)
    parent_tab_id = Column(Integer, ForeignKey("tabs.id"), nullable=False, index=True)

    # Added 2026-09-07 (edit-mode-gap follow-up): independent locking for a
    # NON-root gridstack (parent_id IS NOT NULL — a sub-gridstack; live data
    # never nests one inside another, so this is always exactly one level
    # below a root/variant's own canvas). A root gridstack's OWN top-level
    # canvas (parent_id IS NULL) keeps using its owning TabV2 row's
    # locked/locked_by/locked_at instead — see gridstack_service._is_root /
    # _safe_locked_triple. Same three-column shape as TabV2's own lock
    # columns (mirrored deliberately, not independently designed) and the
    # same `is_lock_stale` TTL helper, so the two "flavors" of lock (whole
    # TabV2 row vs. one sub-gridstack) cannot drift onto different rules.
    # Deliberately real columns, not a components.props JSONB key (unlike
    # Super Block Note's own node-level lock) — every sub-gridstack already
    # has its own row here, so there is no JSONB-blob workaround to reach
    # for, and a typed/indexable column matches the TabV2 precedent instead
    # of inventing a third lock shape.
    locked = Column(Boolean, nullable=True, default=False)
    locked_by = Column(String(255), nullable=True, default="")
    locked_at = Column(DateTime, nullable=True)
