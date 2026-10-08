"""Bookmark persistence backed by the Postgres ``bookmarks`` table.

Plain synchronous query helpers — the router runs them in FastAPI's
threadpool (sync ``def`` path operations), matching the other db_v2-backed
routers in this codebase.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.bookmark import BookmarkV2

logger = logging.getLogger(__name__)


def list_bookmarks(db: Session, *, user_id: int) -> list[BookmarkV2]:
    """Return all bookmarks owned by ``user_id``, newest first."""
    return (
        db.query(BookmarkV2)
        .filter(BookmarkV2.user_id == user_id)
        .order_by(BookmarkV2.saved_at.desc())
        .all()
    )


def add_bookmark(
    db: Session,
    *,
    user_id: int,
    bookmark_link: str,
    type: str | None,
    bookmark_title: str | None = None,
) -> tuple[BookmarkV2, bool]:
    """Create a bookmark for ``user_id``.

    Returns ``(row, created)``. Honors the (user_id, bookmark_link) unique
    constraint: if the pair already exists the existing row is returned with
    ``created=False`` rather than raising.
    """
    existing = (
        db.query(BookmarkV2)
        .filter(
            BookmarkV2.user_id == user_id,
            BookmarkV2.bookmark_link == bookmark_link,
        )
        .first()
    )
    if existing is not None:
        return existing, False

    bookmark = BookmarkV2(
        user_id=user_id,
        bookmark_link=bookmark_link,
        type=type,
        bookmark_title=bookmark_title,
    )
    db.add(bookmark)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent insert won the unique constraint — re-read and report
        # not-created instead of surfacing a 500.
        db.rollback()
        existing = (
            db.query(BookmarkV2)
            .filter(
                BookmarkV2.user_id == user_id,
                BookmarkV2.bookmark_link == bookmark_link,
            )
            .first()
        )
        if existing is not None:
            return existing, False
        raise
    db.refresh(bookmark)
    return bookmark, True


def delete_bookmark(
    db: Session, *, user_id: int, bookmark_link: str
) -> tuple[bool, str | None]:
    """Delete the (user_id, bookmark_link) bookmark if present.

    Returns ``(deleted, bookmark_title)`` — ``deleted`` is True when a row was
    removed, and ``bookmark_title`` is the removed row's title (or None when
    nothing matched or it had no title).
    """
    row = (
        db.query(BookmarkV2)
        .filter(
            BookmarkV2.user_id == user_id,
            BookmarkV2.bookmark_link == bookmark_link,
        )
        .first()
    )
    if row is None:
        return False, None

    bookmark_title = row.bookmark_title
    db.delete(row)
    db.commit()
    return True, bookmark_title
