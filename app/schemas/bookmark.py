"""Pydantic request/response models for the bookmarks endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BookmarkEntry(BaseModel):
    """One saved bookmark, as returned by ``GET /get_bookmarks``."""

    bookmark_link: str = Field(..., description="The bookmarked page URL.")
    bookmark_title: str | None = Field(None, description="Caller-supplied bookmark title.")
    type: str | None = Field(None, description="Caller-supplied bookmark type.")
    saved_at: datetime = Field(..., description="When the bookmark was saved.")


class BookmarkListResponse(BaseModel):
    """Response payload of ``GET /get_bookmarks``."""

    user_id: int
    bookmarks: list[BookmarkEntry] = Field(default_factory=list)


class AddBookmarkRequest(BaseModel):
    """Request body of ``POST /add_bookmark``.

    The owner is resolved from the JWT, so only the link and type are supplied.
    """

    bookmark_link: str = Field(..., min_length=1, description="Page URL to bookmark.")
    bookmark_title: str | None = Field(None, description="Title of the bookmark saved.")
    type: str | None = Field(None, description="Type of the bookmark saved.")


class AddBookmarkResponse(BaseModel):
    """Response payload of ``POST /add_bookmark``."""

    id: int
    user_id: int
    bookmark_link: str
    bookmark_title: str | None = None
    type: str | None = None
    saved_at: datetime
    created: bool = Field(
        ...,
        description="True if a new row was inserted, False if it already existed.",
    )


class DeleteBookmarkResponse(BaseModel):
    """Response payload of ``DELETE /delete_bookmark``."""

    user_id: int
    bookmark_link: str
    bookmark_title: str | None = Field(
        None, description="Title of the removed bookmark, if it had one."
    )
    deleted: bool = Field(
        ..., description="True if a matching bookmark was found and removed."
    )
