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

    source_link = Column(String(255), nullable=True)

    parent_tab_id = Column(Integer, ForeignKey("tabs.id"), nullable=True, index=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
