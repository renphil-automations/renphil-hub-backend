from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class PageContent(Base):
    __tablename__ = "page_contents"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    content = Column(JSONB, nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)

    created_by_id = Column(Integer, nullable=True)
    updated_by_id = Column(Integer, nullable=True)
    locale = Column(String(255), nullable=True)