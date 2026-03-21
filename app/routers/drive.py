"""
Drive router — Google Drive file listing.

Endpoints:
  GET /files       → list files in the configured folder
  GET /files/{id}  → get metadata for a single file
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user, get_drive_service
from app.models.auth import UserInfo
from app.models.drive import DriveFile, DriveFileList
from app.services.drive_service import DriveService

router = APIRouter(prefix="/drive", tags=["Google Drive"])


@router.get("/files", response_model=DriveFileList, summary="List Drive folder contents")
async def list_files(
    page_size: int = Query(default=50, ge=1, le=100),
    page_token: str | None = Query(default=None),
    _user: UserInfo = Depends(get_current_user),
    drive_service: DriveService = Depends(get_drive_service),
):
    """
    Return a paginated list of files from the pre-configured
    Google Drive folder.  Requires authentication.
    """
    return drive_service.list_files(
        page_size=page_size,
        page_token=page_token,
    )


@router.get("/files/{file_id}", response_model=DriveFile, summary="Get file metadata")
async def get_file(
    file_id: str,
    _user: UserInfo = Depends(get_current_user),
    drive_service: DriveService = Depends(get_drive_service),
):
    """Return metadata for a single file by ID.  Requires authentication."""
    return drive_service.get_file(file_id)
