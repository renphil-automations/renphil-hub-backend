"""Pydantic schemas for Google Drive related payloads."""

from pydantic import BaseModel


class DriveFile(BaseModel):
    """Single file / folder item from Google Drive."""
    id: str
    name: str
    mime_type: str
    web_view_link: str | None = None
    web_content_link: str | None = None
    created_time: str | None = None
    modified_time: str | None = None
    size: str | None = None


class DriveFileList(BaseModel):
    """Paginated list of files from a Drive folder."""
    files: list[DriveFile]
    next_page_token: str | None = None
