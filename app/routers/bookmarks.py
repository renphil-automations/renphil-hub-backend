"""Bookmarks router — save, list and delete a user's bookmarked pages.

All three endpoints are authenticated; the owner is resolved from the JWT
email to the caller's ``hub_users`` row via ``get_current_hub_user``, and
they operate on the Postgres ``bookmarks`` table via ``bookmark_service``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.schemas.bookmark import (
    AddBookmarkRequest,
    AddBookmarkResponse,
    BookmarkEntry,
    BookmarkListResponse,
    DeleteBookmarkResponse,
)
from app.services import bookmark_service

router = APIRouter(prefix="/bookmarks", tags=["Bookmarks"])


@router.get("/get_bookmarks", response_model=BookmarkListResponse)
def get_bookmarks(
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> BookmarkListResponse:
    """Return every bookmark saved by the authenticated caller.

    The owner is resolved from the JWT email to the caller's ``hub_users``
    row, never taken as input.
    """
    user_id = current.hub_user_id
    rows = bookmark_service.list_bookmarks(db, user_id=user_id)
    return BookmarkListResponse(
        user_id=user_id,
        bookmarks=[
            BookmarkEntry(
                bookmark_link=row.bookmark_link,
                bookmark_title=row.bookmark_title,
                type=row.type,
                saved_at=row.saved_at,
            )
            for row in rows
        ],
    )


@router.post("/add_bookmark", response_model=AddBookmarkResponse)
def add_bookmark(
    payload: AddBookmarkRequest,
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> AddBookmarkResponse:
    """Create a bookmark for the authenticated caller; idempotent on (user, link).

    The owner is resolved from the JWT email to the caller's ``hub_users``
    row, never taken as input.
    """
    row, created = bookmark_service.add_bookmark(
        db,
        user_id=current.hub_user_id,
        bookmark_link=payload.bookmark_link,
        type=payload.type,
        bookmark_title=payload.bookmark_title,
    )
    return AddBookmarkResponse(
        id=row.id,
        user_id=row.user_id,
        bookmark_link=row.bookmark_link,
        bookmark_title=row.bookmark_title,
        type=row.type,
        saved_at=row.saved_at,
        created=created,
    )


@router.delete("/delete_bookmark", response_model=DeleteBookmarkResponse)
def delete_bookmark(
    bookmark_link: str = Query(..., min_length=1, description="Page URL to remove."),
    current: CurrentHubUser = Depends(get_current_hub_user),
    db: Session = Depends(get_db_v2),
) -> DeleteBookmarkResponse:
    """Delete the caller's bookmark for ``bookmark_link`` if it exists.

    The owner is resolved from the JWT email to the caller's ``hub_users``
    row, never taken as input.
    """
    user_id = current.hub_user_id
    deleted, bookmark_title = bookmark_service.delete_bookmark(
        db, user_id=user_id, bookmark_link=bookmark_link
    )
    return DeleteBookmarkResponse(
        user_id=user_id,
        bookmark_link=bookmark_link,
        bookmark_title=bookmark_title,
        deleted=deleted,
    )
