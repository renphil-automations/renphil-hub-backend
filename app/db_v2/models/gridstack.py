from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class GridstackV2(BaseV2):
    """A canvas. parent_id NULL = the root canvas of its tab; non-null =
    a sub-tab's canvas nested under another gridstack (this is how every
    non-root old Tab — nav-tree child, Super BlockNote sub-tab, or Super
    GridStack sub-tab — is represented in the new schema). parent_tab_id is
    denormalized to the ultimate root tab at every depth, so "all gridstacks
    under tab X" is a flat `parent_tab_id = X` query regardless of nesting depth.

    settings (JSONB) is a catch-all bucket holding:
      - the Super GridStack tab-bar config, e.g. {"sgs": {"tabBarPosition": ...}}
      - the per-sub-tab access_control (AccessControl shape), since a
        sub-tab is no longer its own Tab row and has nowhere else to keep an
        independent viewer restriction, e.g. {"access_control": {...}}
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
