from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


def _utc_now():
    return datetime.now(timezone.utc)


class TabVersion(Base):
    __tablename__ = "tab_versions"

    id = Column(Integer, primary_key=True, index=True)

    tab_id = Column(Integer, nullable=True, index=True)
    tab_document_id = Column(String(255), nullable=True, index=True)

    action = Column(String(50), nullable=False, default="update")
    edited_by = Column(String(255), nullable=True)

    snapshot = Column(JSONB, nullable=False)

    created_at = Column(DateTime, nullable=False, default=_utc_now)


class PageContentVersion(Base):
    __tablename__ = "page_content_versions"

    id = Column(Integer, primary_key=True, index=True)

    page_content_id = Column(Integer, nullable=True, index=True)
    page_content_document_id = Column(String(255), nullable=True, index=True)

    tab_id = Column(Integer, nullable=True, index=True)
    tab_document_id = Column(String(255), nullable=True, index=True)

    action = Column(String(50), nullable=False, default="update")
    edited_by = Column(String(255), nullable=True)

    snapshot = Column(JSONB, nullable=False)

    created_at = Column(DateTime, nullable=False, default=_utc_now)