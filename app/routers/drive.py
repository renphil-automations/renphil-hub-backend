"""
Drive router — Google Drive file listing.

Endpoints:
  GET /files       → list all files & sub-folders recursively
  GET /files/{id}  → get metadata for a single file
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user, get_drive_service
from app.models.auth import UserInfo
from app.models.drive import DriveFile, DriveItemList
from app.services.drive_service import DriveService

router = APIRouter(prefix="/drive", tags=["Google Drive"])


@router.get("/files", response_model=DriveItemList, summary="List all Drive folder contents recursively")
async def list_files(
    folder_id: str | None = Query(default=None, description="Optional folder ID to start from (defaults to root)"),
    _user: UserInfo = Depends(get_current_user),
    drive_service: DriveService = Depends(get_drive_service),
):
    """
    Return a nested tree of every file and sub-folder found recursively
    under the given folder (or the configured root folder).
    Each item includes ``is_folder``, ``parent_id``, and ``children``.
    Requires authentication.
    """
    return drive_service.list_all_items(folder_id=folder_id)


@router.get("/files/{file_id}", response_model=DriveFile, summary="Get file metadata")
async def get_file(
    file_id: str,
    _user: UserInfo = Depends(get_current_user),
    drive_service: DriveService = Depends(get_drive_service),
):
    """Return metadata for a single file by ID.  Requires authentication."""
    return drive_service.get_file(file_id)
