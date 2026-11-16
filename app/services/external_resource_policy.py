"""Shared file-type and ingestion eligibility contract for Hub and worker."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class ResourcePolicy:
    label: str
    supported: bool | None
    reason: str | None
    confirmed: bool = False


_SUPPORTED = {
    "application/pdf": "PDF", "pdf": "PDF",
    "application/vnd.google-apps.document": "Google Docs", "google docs": "Google Docs",
    "application/vnd.google-apps.spreadsheet": "Google Sheets", "google sheets": "Google Sheets",
    "application/vnd.google-apps.presentation": "Google Slides", "google slides": "Google Slides",
    "google drive folder": "Google Drive folder", "application/vnd.google-apps.folder": "Google Drive folder",
}
_OFFICE_TYPES = {
    "application/msword": "Word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.ms-excel": "Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "application/vnd.ms-powerpoint": "PowerPoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
}
_UNSUPPORTED_TYPES = {
    "text/csv": "CSV", "text/plain": "Text file",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.shortcut": "Drive shortcut",
}
_SUFFIXES = {
    ".pdf": "PDF", ".doc": "Word", ".docx": "Word", ".xls": "Excel", ".xlsx": "Excel",
    ".xlsm": "Excel", ".ppt": "PowerPoint", ".pptx": "PowerPoint", ".csv": "CSV",
    ".zip": "Archive", ".rar": "Archive", ".7z": "Archive",
    ".png": "Image", ".jpg": "Image", ".jpeg": "Image", ".gif": "Image", ".svg": "Image",
    ".mp4": "Video", ".mov": "Video", ".mp3": "Audio", ".wav": "Audio",
    ".html": "Web page", ".htm": "Web page", ".txt": "Text file",
}


def _unsupported(label: str, *, confirmed: bool = False) -> ResourcePolicy:
    reason = f"{label} ingestion is not supported in this release."
    if label in {"Word", "Excel", "PowerPoint"}:
        reason += " Export the file as PDF or use a supported Google document."
    if label == "Google Form":
        reason = "Google Forms are for embedding and cannot be ingested."
    if label == "Airtable":
        reason = "Airtable links use the Airtable component and its dedicated data connection."
    return ResourcePolicy(label, False, reason, confirmed)


def resource_policy(url: str, resource_type: str | None = None, reason: str | None = None) -> ResourcePolicy:
    detected = (resource_type or "").strip().lower()
    if detected in _SUPPORTED:
        return ResourcePolicy(_SUPPORTED[detected], True, None, True)
    if detected in _OFFICE_TYPES:
        return _unsupported(_OFFICE_TYPES[detected], confirmed=True)
    if detected in _UNSUPPORTED_TYPES:
        return _unsupported(_UNSUPPORTED_TYPES[detected], confirmed=True)
    if detected.startswith(("image/", "video/", "audio/")):
        return _unsupported(detected.split("/", 1)[0].title(), confirmed=True)
    if detected in {"text/html", "application/xhtml+xml"}:
        return _unsupported("Web page", confirmed=True)
    if detected in {"application/zip", "application/x-zip-compressed"}:
        return _unsupported("Archive", confirmed=True)
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = unquote(parsed.path).lower()
        parsed.port
    except (TypeError, ValueError):
        return _unsupported("Invalid link")
    if parsed.scheme not in {"http", "https"} or not host:
        return _unsupported("Unsupported link")
    if host == "airtable.com" or host.endswith(".airtable.com"):
        return _unsupported("Airtable")
    if host in {"docs.google.com", "drive.google.com", "www.drive.google.com"}:
        if path.startswith("/forms/"):
            return _unsupported("Google Form")
        if path.startswith("/drive/") and "folders" in path.split("/"):
            return ResourcePolicy("Google Drive folder", True, None)
        for prefix, label in (("/document/", "Google Docs"), ("/spreadsheets/", "Google Sheets"),
                              ("/presentation/", "Google Slides"), ("/drive/folders/", "Google Drive folder")):
            if path.startswith(prefix):
                return ResourcePolicy(label, True, None)
    if detected not in {"", "pending", "pending detection", "unknown"}:
        # Worker-detected MIME takes precedence over an optimistic URL suffix.
        return _unsupported("Unsupported file type", confirmed=True)
    if (reason or "").startswith("unsupported_resource_type:"):
        return _unsupported("Unsupported file type", confirmed=True)
    label = _SUFFIXES.get(PurePosixPath(path).suffix)
    if label == "PDF":
        return ResourcePolicy(label, True, None)
    if label:
        return _unsupported(label)
    return ResourcePolicy("Type not checked", None, "The file type will be identified during its first approved check.")
