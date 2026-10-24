"""Headshots on Cloudflare R2 (plan v2 §7, A-5; v1 plan §6.2).

Airtable attachment URLs expire within hours, so the Users step copies a
person's headshot to R2 and stores the public URL:

  key  <R2_KEY_PREFIX><attachment id>/<file name>     e.g. headshots/attX/a b.jpg
  URL  <R2_PUBLIC_BASE_URL>/<key>                     the file name unencoded,
                                                      like the 107 URLs on Dev

A headshot is compared by its attachment id, the first segment of the key.

R2 speaks the S3 API; `BotoHeadshotStore` uses boto3, imported only when an
upload or delete really happens. Tests replace `STORE_FACTORY` and `FETCH`.
The two keys are secrets: they're `repr=False` here, and no message built
in this module carries them (errors are reported by class and code only).
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

logger = logging.getLogger(__name__)

# (connect, read) seconds for the download from Airtable.
_FETCH_TIMEOUT = (10, 60)
_SETTINGS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_ENDPOINT_URL", "R2_KEY_PREFIX", "R2_PUBLIC_BASE_URL")


class HeadshotError(Exception):
    """An upload, download or delete failed. The message is short and
    secret-free: what failed, and the error's class or code."""


@dataclass(frozen=True)
class R2Config:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket: str
    endpoint_url: str
    key_prefix: str  # with its trailing slash, e.g. "headshots/"
    public_base_url: str  # no trailing slash

    @classmethod
    def from_settings(cls, settings: Any) -> R2Config | None:
        """None when any R2 setting is blank: the sync then skips the
        Headshot field (one warning per run)."""
        values = {}
        for name in _SETTINGS:
            value = getattr(settings, name, None)
            if value is None or not str(value).strip():
                return None
            values[name] = str(value).strip()
        prefix = values["R2_KEY_PREFIX"].lstrip("/")
        return cls(
            access_key_id=values["R2_ACCESS_KEY_ID"],
            secret_access_key=values["R2_SECRET_ACCESS_KEY"],
            bucket=values["R2_BUCKET"],
            endpoint_url=values["R2_ENDPOINT_URL"],
            key_prefix=prefix if prefix.endswith("/") else prefix + "/",
            public_base_url=values["R2_PUBLIC_BASE_URL"].rstrip("/"),
        )

    def key_for(self, attachment_id: str, filename: str | None) -> str:
        name = (filename or "").replace("/", "_").strip() or "headshot"
        return f"{self.key_prefix}{attachment_id}/{name}"

    def url_for(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"

    def key_of(self, url: str | None) -> str | None:
        """The object key of a URL inside OUR area (public base + prefix),
        else None: anything else (an external URL, a hand-typed link) is
        never touched (v2 §7 guard)."""
        area = f"{self.public_base_url}/{self.key_prefix}"
        if not url or not url.startswith(area) or len(url) <= len(area):
            return None
        return url[len(self.public_base_url) + 1 :]

    def attachment_id_of(self, url: str | None) -> str | None:
        """What a stored headshot URL is compared by: the attachment id in
        our area's key; for a URL outside it, the URL itself (it can only
        differ from an attachment id); None when blank."""
        if not url or not url.strip():
            return None
        key = self.key_of(url.strip())
        if key is None:
            return url.strip()
        return key[len(self.key_prefix) :].split("/", 1)[0] or url.strip()


class HeadshotStore(Protocol):
    def put(self, key: str, body: bytes, content_type: str | None) -> None: ...

    def delete(self, key: str) -> None: ...


class BotoHeadshotStore:
    def __init__(self, config: R2Config) -> None:
        import boto3  # only when R2 is really used
        from botocore.config import Config

        self._bucket = config.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name="auto",
            config=Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3}),
        )

    def put(self, key: str, body: bytes, content_type: str | None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body, **extra)
        except Exception as exc:
            raise HeadshotError(f"R2 upload failed ({_describe(exc)})") from None

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise HeadshotError(f"R2 delete failed ({_describe(exc)})") from None


def _describe(exc: Exception) -> str:
    """The error's class, plus the S3 error code when there is one. Never
    the message: some carry the access key id."""
    response = getattr(exc, "response", None)
    code = (response.get("Error") or {}).get("Code") if isinstance(response, dict) else None
    return f"{type(exc).__name__}: {code}" if code else type(exc).__name__


def fetch_attachment(url: str) -> tuple[bytes, str | None]:
    """Download an Airtable attachment from its temporary URL."""
    try:
        resp = requests.get(url, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HeadshotError(f"Airtable attachment download answered HTTP {status}") from None
    except requests.RequestException as exc:
        raise HeadshotError(f"Airtable attachment download failed ({type(exc).__name__})") from None
    return resp.content, resp.headers.get("Content-Type")


def content_type_for(filename: str | None, fetched: str | None) -> str | None:
    if fetched and fetched.split(";")[0].strip().startswith("image/"):
        return fetched.split(";")[0].strip()
    return mimetypes.guess_type(filename or "")[0] or fetched


# Injection points: tests swap these for fakes.
STORE_FACTORY: Callable[[R2Config], HeadshotStore] = BotoHeadshotStore
FETCH: Callable[[str], tuple[bytes, str | None]] = fetch_attachment


def make_store(config: R2Config | None) -> HeadshotStore | None:
    return STORE_FACTORY(config) if config is not None else None
