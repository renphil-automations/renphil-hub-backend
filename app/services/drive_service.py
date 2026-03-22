"""
Google Drive service.

Uses a GCP service account to list and retrieve files from a
pre-configured Drive folder shared with that service account.
"""

from __future__ import annotations

import logging
from typing import Any

from googleapiclient.errors import HttpError

from app.config import Settings
from app.helpers.exceptions import GoogleDriveError
from app.helpers.google_client import build_drive_service
from app.models.drive import DriveFile, DriveFileList

logger = logging.getLogger(__name__)

# Fields we request from Google Drive
_FILE_FIELDS = (
    "id, name, mimeType, webViewLink, webContentLink, createdTime, modifiedTime, size"
)
_FOLDER_FIELDS = "id, name"


class DriveService:
    """Thin wrapper around the Google Drive v3 API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service = build_drive_service(settings)

    # ── List files in the configured folder ────────────────────────────
    def list_files(
        self,
        page_size: int = 50,
        page_token: str | None = None,
        order_by: str = "modifiedTime desc",
    ) -> DriveFileList:
        """Return a paginated list of files inside the target folder."""
        folder_id = self._settings.GOOGLE_DRIVE_FOLDER_ID
        query = f"'{folder_id}' in parents and trashed = false"

        try:
            response: dict[str, Any] = (
                self._service.files()
                .list(
                    q=query,
                    pageSize=page_size,
                    pageToken=page_token,
                    orderBy=order_by,
                    fields=f"nextPageToken, files({_FILE_FIELDS})",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Drive API error: %s", exc)
            raise GoogleDriveError(f"Drive API error: {exc}") from exc

        files = [
            DriveFile(
                id=f["id"],
                name=f["name"],
                mime_type=f["mimeType"],
                web_view_link=f.get("webViewLink"),
                web_content_link=f.get("webContentLink"),
                created_time=f.get("createdTime"),
                modified_time=f.get("modifiedTime"),
                size=f.get("size"),
            )
            for f in response.get("files", [])
        ]

        folder_name = self._get_folder_name(folder_id)

        return DriveFileList(
            folder_id=folder_id,
            folder_name=folder_name,
            files=files,
            next_page_token=response.get("nextPageToken"),
        )

    # ── Folder name helper ─────────────────────────────────────────────
    def _get_folder_name(self, folder_id: str) -> str:
        """Fetch just the name of the folder itself."""
        try:
            folder: dict[str, Any] = (
                self._service.files()
                .get(
                    fileId=folder_id,
                    fields=_FOLDER_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
            return folder.get("name", folder_id)
        except HttpError as exc:
            logger.warning("Could not fetch folder name for %s: %s", folder_id, exc)
            return folder_id  # Graceful fallback: return the ID instead of failing

    # ── Get metadata for a single file ─────────────────────────────────
    def get_file(self, file_id: str) -> DriveFile:
        """Return metadata for a single file by its ID."""
        try:
            f: dict[str, Any] = (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields=_FILE_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Drive API error (get_file): %s", exc)
            raise GoogleDriveError(f"Drive API error: {exc}") from exc

        return DriveFile(
            id=f["id"],
            name=f["name"],
            mime_type=f["mimeType"],
            web_view_link=f.get("webViewLink"),
            web_content_link=f.get("webContentLink"),
            created_time=f.get("createdTime"),
            modified_time=f.get("modifiedTime"),
            size=f.get("size"),
        )
