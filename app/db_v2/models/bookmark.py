"""The ``bookmarks`` table — one row per (user, saved page).

Physical table and column names are env-driven (see ``app.config`` and the
``BOOKMARKS_*`` settings), so a database-side rename never touches this
source. The names are resolved once at import time, the same style
``app.db_v2.database`` already uses for its connection config.

No ORM ``relationship()`` — every model in ``db_v2`` follows the same rule:
traversal is a plain query. The FK to ``hub_users`` and the
(user_id, bookmark_link) uniqueness are enforced by the database, declared
here only so ``create_all`` in tests reproduces the real constraints.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)

from app.config import get_settings
from app.db_v2.database import BaseV2

_settings = get_settings()


class BookmarkV2(BaseV2):
    """A single bookmark saved by a hub user."""

    __tablename__ = _settings.BOOKMARKS_TABLE

    id = Column(_settings.BOOKMARKS_ID_FIELD, Integer, primary_key=True, index=True)

    user_id = Column(
        _settings.BOOKMARKS_USER_ID_FIELD,
        Integer,
        ForeignKey("hub_users.id"),
        nullable=False,
        index=True,
    )

    bookmark_link = Column(_settings.BOOKMARKS_LINK_FIELD, Text, nullable=False)

    saved_at = Column(
        _settings.BOOKMARKS_SAVED_AT_FIELD,
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    type = Column(_settings.BOOKMARKS_TYPE_FIELD, Text, nullable=True)

    bookmark_title = Column(_settings.BOOKMARKS_TITLE_FIELD, Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            _settings.BOOKMARKS_USER_ID_FIELD,
            _settings.BOOKMARKS_LINK_FIELD,
            name="unique_user_bookmark_constraint",
        ),
    )
