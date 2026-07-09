from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class TabV2(BaseV2):
    """Root-level tabs only in the new schema — flat, no parent_id. Any tab
    that had a parent in the old schema (nav-tree child, Super BlockNote
    sub-tab, or Super GridStack sub-tab) becomes a GridstackV2 row instead,
    see gridstack.py."""

    __tablename__ = "tabs"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    title = Column(String(255), nullable=True)
    order = Column(Integer, nullable=True)

    access_control = Column(JSONB, nullable=True)

    locked = Column(Boolean, nullable=True, default=False)
    locked_by = Column(String(255), nullable=True, default="")

    source_link = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
