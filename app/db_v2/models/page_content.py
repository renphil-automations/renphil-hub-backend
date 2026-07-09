from sqlalchemy import Column, Integer
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class PageContentV2(BaseV2):
    """Legacy BlockNote content, referenced from ComponentV2.page_content_id.
    Purely a referenced-by table — no FK columns of its own."""

    __tablename__ = "page_contents"

    id = Column(Integer, primary_key=True, index=True)

    content = Column(JSONB, nullable=True)
