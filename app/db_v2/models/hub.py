from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class HubV2(BaseV2):
    """The top of the access-control tree (see
    AI Docs/plan_nav_tabs_phase2_2026-07-30.md). Exactly one row, ever — the
    service reads `db.query(HubV2).order_by(HubV2.id).first()`, never by id.

    A separate table rather than a `NavTabV2` or `TabV2` row: either would
    make the top of the access tree a member of a collection it needs to sit
    above — excluded from listings, reorder, slug uniqueness, and routing by
    convention rather than by construction. See the plan's §4.1 for the full
    per-collection breakdown of what would need to filter it out.

    Its editors (`access_control`) may create/rename/reorder/delete nav tabs,
    replacing the interim `Hub Admin` JWT gate (`require_hub_admin`) with a
    real, evaluated access-control node — see `access_control_service`.

    No relationship() here, matching every other model in db_v2/ — traversal
    is always a plain query."""

    __tablename__ = "hub"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    access_control = Column(JSONB, nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
