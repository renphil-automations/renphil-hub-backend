from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class Tab(Base):
    __tablename__ = "tabs"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    title = Column(String(255), nullable=True)
    order = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)

    created_by_id = Column(Integer, nullable=True)
    updated_by_id = Column(Integer, nullable=True)
    locale = Column(String(255), nullable=True)

    google_source_id = Column(String(255), nullable=True)

    # Exists in the real RenPhil cloned database.
    source_link = Column(String(255), nullable=True)

    access_control = Column(JSONB, nullable=True)

    locked = Column(Boolean, nullable=True, default=False)
    locked_by = Column(String(255), nullable=True, default="")