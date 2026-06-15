from sqlalchemy import Column, Integer, UniqueConstraint

from app.database import Base


class PageContentTabLink(Base):
    __tablename__ = "page_contents_tab_lnk"

    id = Column(Integer, primary_key=True, index=True)

    page_content_id = Column(Integer, unique=True, nullable=True)

    tab_id = Column(Integer, unique=True, nullable=True)

    __table_args__ = (
        UniqueConstraint("page_content_id", "tab_id", name="page_contents_tab_lnk_uq"),
    )


class TabParentLink(Base):
    __tablename__ = "tabs_parent_lnk"

    id = Column(Integer, primary_key=True, index=True)

    # Child tab id.
    tab_id = Column(Integer, unique=True, nullable=True)

    # Parent tab id.
    # This must NOT be unique.
    inv_tab_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("tab_id", "inv_tab_id", name="tabs_parent_lnk_uq"),
    )