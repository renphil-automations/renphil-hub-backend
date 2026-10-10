from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class HubV2(BaseV2):
    """DORMANT. Exactly one row, ever — it was the top of the access-control
    propagation tree (AI Docs/plan_nav_tabs_phase2_2026-07-30.md), the node
    whose `access_control` cascaded down through every nav tab, tab, variant
    and sub-tab gridstack.

    That engine has been removed ahead of a new access control algorithm, so
    nothing in the application reads or writes this table any more. The
    mapping and the row are deliberately kept rather than dropped: the stored
    JSON is the record of what the old model granted, and a new algorithm may
    well want a root node of its own.

    No relationship() here, matching every other model in db_v2/ — traversal
    is always a plain query."""

    __tablename__ = "hub"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    access_control = Column(JSONB, nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
