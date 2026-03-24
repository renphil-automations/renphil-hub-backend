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
from app.models.drive import DriveFile, DriveFileList, DriveItem, DriveItemList

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"

# Fields we request from Google Drive
_FILE_FIELDS = (
    "id, name, mimeType, parents, webViewLink, webContentLink, "
    "createdTime, modifiedTime, size"
)
_FOLDER_FIELDS = "id, name"


class DriveService:
    """Thin wrapper around the Google Drive v3 API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service = build_drive_service(settings)

    # ── Recursive nested tree of ALL files / sub-folders ───────────────
    def list_all_items(self, folder_id: str | None = None) -> DriveItemList:
        """
        Recursively list every file and sub-folder under *folder_id*
        (defaults to the configured root folder).

        Returns a nested :class:`DriveItemList` where each folder item
        contains its own ``children`` list, building a full tree.
        """
        root_id = folder_id or self._settings.GOOGLE_DRIVE_FOLDER_ID
        items = self._build_tree(root_id)
        root_name = self._get_folder_name(root_id)
        return DriveItemList(
            root_folder_id=root_id,
            root_folder_name=root_name,
            items=items,
        )

    def _build_tree(self, parent_id: str) -> list[DriveItem]:
        """Return the direct children of *parent_id*, recursing into sub-folders."""
        children: list[DriveItem] = []
        page_token: str | None = None
        while True:
            try:
                response: dict[str, Any] = (
                    self._service.files()
                    .list(
                        q=f"'{parent_id}' in parents and trashed = false",
                        pageSize=100,
                        pageToken=page_token,
                        fields=f"nextPageToken, files({_FILE_FIELDS})",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
            except HttpError as exc:
                logger.error("Drive API error while listing %s: %s", parent_id, exc)
                raise GoogleDriveError(f"Drive API error: {exc}") from exc

            for f in response.get("files", []):
                is_folder = f["mimeType"] == _FOLDER_MIME
                item = DriveItem(
                    id=f["id"],
                    name=f["name"],
                    mime_type=f["mimeType"],
                    is_folder=is_folder,
                    parent_id=parent_id,
                    web_view_link=f.get("webViewLink"),
                    web_content_link=f.get("webContentLink"),
                    created_time=f.get("createdTime"),
                    modified_time=f.get("modifiedTime"),
                    size=f.get("size"),
                    children=self._build_tree(f["id"]) if is_folder else [],
                )
                children.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return children

    # ── Legacy paginated list (kept for backwards compat) ──────────────
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
