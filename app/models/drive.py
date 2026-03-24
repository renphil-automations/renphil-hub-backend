"""Pydantic schemas for Google Drive related payloads."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DriveItem(BaseModel):
    """Single file or folder item from Google Drive (recursive tree node)."""
    id: str
    name: str
    mime_type: str
    is_folder: bool = Field(
        description="True when this item is a folder, False when it is a file."
    )
    parent_id: str | None = Field(
        default=None,
        description="ID of the parent folder this item lives in.",
    )
    web_view_link: str | None = None
    web_content_link: str | None = None
    created_time: str | None = None
    modified_time: str | None = None
    size: str | None = None
    children: list[DriveItem] = Field(
        default_factory=list,
        description="Nested children (files and sub-folders). Empty for files.",
    )


class DriveItemList(BaseModel):
    """Nested tree of every file and sub-folder under the root folder."""
    root_folder_id: str = Field(description="The top-level folder that was traversed.")
    root_folder_name: str
    items: list[DriveItem] = Field(
        description="First-level children of the root folder. "
        "Each folder item may contain nested children recursively."
    )


# ── Kept for backwards compat if needed elsewhere ──────────────────────
class DriveFile(BaseModel):
    """Single file / folder item from Google Drive (legacy)."""
    id: str
    name: str
    mime_type: str
    web_view_link: str | None = None
    web_content_link: str | None = None
    created_time: str | None = None
    modified_time: str | None = None
    size: str | None = None


class DriveFileList(BaseModel):
    """Paginated list of files from a Drive folder (legacy)."""
    folder_id: str
    folder_name: str
    files: list[DriveFile]
    next_page_token: str | None = None
