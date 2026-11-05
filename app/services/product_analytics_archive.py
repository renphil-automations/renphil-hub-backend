"""Archive-before-delete for age-expired Hub product analytics.

Records older than the retention period are written to a private Cloudflare
R2 bucket (through its S3-compatible API) as ZIP objects and deleted from the
database in the same transaction that **registers** the object in
`hub_product_analytics_archives`. WHAT AN ARCHIVE IS: a registered object.
An object in the bucket without a registry row is an abandoned upload, not an
archive — the job cannot cancel an HTTP request it has given up on (a
deadline, a lost connection or a killed process leaves the server-side
outcome unknown), so the registry, not the bucket, is the authority. Anything
that reads archives starts from the registry; leftovers are reconciled by an
operator (runbook, "Leftover objects").

Records that reliably belong to a deleted Hub resource are deleted, not
archived: the deleted-resource cleanup removes them first, the archive
selection excludes them again at query time, and the Hub rows a batch depends
on are row-locked against deletion (and, where the join column has no unique
index, against any change) from before the upload until the batch commits.

Guarantees (operational runbook: PRODUCT_ANALYTICS_ARCHIVE.md):

* A row is deleted only in the transaction that registers the archive
  containing it, after the object was uploaded (or recognised) and read back
  byte for byte, and only if the row still matches the selection
  (environment and cutoff). Any failure before the commit — upload, readback,
  re-check, timeout, lost connection, failed commit — deletes nothing and
  registers nothing; the run stops.
* Snapshot: the batch's analytics rows are selected `FOR UPDATE`, so the
  encoded bytes and the deleted rows are the same row versions.
* No record enters a REGISTERED archive after its resource was deleted or
  re-keyed (PostgreSQL): the nav tabs, tabs, sub-gridstacks and components
  the deleted-resource rule consults for the batch are row-locked from
  before the upload until the commit — `FOR KEY SHARE` where the live
  catalog has a single-column unique index on the join column, `FOR SHARE`
  otherwise (`resource_lock_modes`); a resource deleted between selection
  and lock is detected and the batch is re-selected without any upload; the
  rule is re-checked after the readback, inside the locks, before the
  registry row and the deletes are written. An abandoned upload that lands
  later can contain records of a resource deleted in the meantime — it is
  not registered and therefore not an archive, but it is bytes in the bucket
  until `reconcile_leftovers` (a separate process) removes it: after a
  grace period, under the job's lock, after re-checking the registry, only
  if it carries this database's `registry-id` marker and — automatically —
  only if every record in it is still live, otherwise only once an operator
  has reviewed it. R2's DeleteObject is unconditional (it ignores
  `If-Match`; verified 2026-11-04), so nothing here claims compare-and-
  delete semantics.
* Archive bytes are deterministic for a given ordered set of rows (fixed ZIP
  member timestamps, sorted JSON keys, no run-specific data inside the ZIP),
  and the object key embeds the environment, table, first day, row-id range
  and a digest of the bytes. A batch interrupted after its upload therefore
  re-creates the same key on the next run, finds the object, verifies it and
  registers it without uploading twice — and only if its rows are still the
  same rows (a changed selection means different bytes and a different key).
  A key that already exists with different content is refused, never
  overwritten.
* One run per database at a time: a transaction-level advisory lock is taken
  inside every batch transaction (safe behind a transaction-pooling proxy,
  which the managed database's pooled endpoint is); a second run stops with
  exit 3 at its first batch.
* Dry run performs no storage call and no database write.

Bounds on how long a batch holds its locks are enforced per step (storage
call deadline, statement timeout, lock timeout) and are NOT an end-to-end
guarantee: the runbook's "maximum wait" table says which steps are bounded by
the job, which by the server, and which by nothing. On a dialect without row
locks (SQLite, tests only) the resource guarantee degrades to the re-check
after the readback, which refuses to register and delete the batch.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO, StringIO
from typing import Any, Callable, Iterable, Mapping, Protocol
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from sqlalchemy import DateTime, func, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.product_analytics import (
    HubProductAnalyticsArchiveIdentityV2, HubProductAnalyticsArchiveV2, HubProductEngagementV2, HubProductEventV2,
)
from app.db_v2.models.tab import TabV2
from app.services import product_analytics_service as service

logger = logging.getLogger(__name__)

FORMAT_VERSION = 3
LEGACY_FORMAT_VERSION = 2
ENVIRONMENTS = frozenset({"local", "sandbox", "production"})
TABLES = {"events": HubProductEventV2, "engagements": HubProductEngagementV2}
TIMESTAMP_COLUMN = {"events": "occurred_at", "engagements": "ended_at"}
RECORD_ID_COLUMN = {"events": "event_id", "engagements": "engagement_id"}
MEMBER_NAMES = {"events": "events.jsonl", "engagements": "engagements.jsonl"}
CSV_MEMBER_NAMES = {"events": "events.csv", "engagements": "engagements.csv"}
MANIFEST_NAME = "manifest.json"
# Fixed member timestamp so that identical rows always produce identical bytes.
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
# One advisory lock per database, taken per batch transaction
# (`pg_try_advisory_xact_lock`): two runs never archive at the same time.
JOB_LOCK_KEY = 7_206_234_501
REGISTRY_TABLE = HubProductAnalyticsArchiveV2.__tablename__
# How long one batch may wait for the Hub rows it depends on (an in-flight
# page deletion holds them). The job gives up the batch, never the lock
# holder; `LOCK_RETRIES` bounds how often a batch is retried before the run
# stops with exit 1 and nothing deleted.
LOCK_TIMEOUT_MS = 5_000
LOCK_RETRIES = 3
# Enforced bounds on how long one batch can hold its locks: every storage
# call is abandoned after this many seconds (`call_with_deadline`), and every
# statement inside the batch transaction runs under this `statement_timeout`.
# The arithmetic is in the runbook ("maximum wait").
STORAGE_CALL_DEADLINE_S: float | None = 60.0
STATEMENT_TIMEOUT_MS = 15_000
# The column the deleted-resource rule joins on, per Hub table, and the lock
# that keeps it from changing. `FOR KEY SHARE` blocks DELETE and any UPDATE of
# a column covered by a unique index — and nothing else — so it is enough only
# where the catalog really has such an index on that column (the models
# declare one for `components.link`; the dev database does not have it, and
# `document_id` is never unique). Otherwise `FOR SHARE` is used, which blocks
# every UPDATE and DELETE of the referenced rows. Decided from `pg_index` at
# run time, never from the models.
RESOURCE_LOCK_COLUMNS = {"nav_tabs": "document_id", "tabs": "document_id", "gridstacks": "document_id", "components": "link"}
LOCK_MODES = ("key_share", "share")
# The selected rows of this event name are tied to a page (tab or
# sub-gridstack) by `target_id`.
_PAGE_SELECTION_EVENTS = frozenset({"root_tab.selected", "view_tab.selected", "sub_tab.selected"})


class ArchiveError(RuntimeError):
    """A problem that stops the archive job; nothing further is deleted."""


class ArchiveConfigError(ArchiveError):
    """Storage configuration is missing or malformed. Messages name variables, never values."""


class ArchiveIntegrityError(ArchiveError):
    """An archive could not be verified; its rows must not be deleted."""


class ArchiveObjectExistsError(ArchiveError):
    """A conditional upload (`If-None-Match: *`) found the key already taken."""


class ArchiveTimeoutError(ArchiveError):
    """A storage call did not return within `STORAGE_CALL_DEADLINE_S`; the
    batch is abandoned (nothing registered, nothing deleted) so its locks are
    released. The call itself is NOT cancelled: its server-side outcome is
    unknown and a late object may land, unregistered."""


class ArchiveLockedError(ArchiveError):
    """Another run holds the batch lock; this run stops without touching the batch."""


class ArchiveRestoreError(ArchiveError):
    """A restore was refused before any write (unregistered key, mismatch, missing object)."""


def call_with_deadline(fn: Callable[[], Any], seconds: float | None, *, what: str) -> Any:
    """Run `fn` and give up after `seconds`, whatever the underlying client
    does. This is NOT a cancellation: the call keeps running in a daemon
    thread and its server-side outcome is unknown (a late upload may land,
    unregistered — see the module docstring); the caller regains control,
    rolls back and releases its locks. It is the enforced bound on how long a
    batch can hold its locks per storage call: client timeouts are per
    connect/read and cannot bound a slow trickle."""
    if seconds is None:
        return fn()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, name=f"archive-{what}", daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        raise ArchiveTimeoutError(f"storage {what} did not complete within {seconds:g} s; the batch was not deleted")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ── Object storage ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObjectInfo:
    """What a listing or a HEAD says about one object: enough to recognise
    it again (`etag` + `size`), to age it (`last_modified`) and — from a
    HEAD only — the user metadata the job stamped on it."""

    key: str
    size: int
    etag: str
    last_modified: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)


class ArchiveStore(Protocol):
    def put(self, key: str, data: bytes, metadata: Mapping[str, str] | None = None, *, if_none_match: bool = False) -> None:
        """Store the object. With `if_none_match`, refuse (ArchiveObjectExistsError)
        if the key already exists, atomically on the storage side."""
        ...

    def get(self, key: str) -> bytes: ...

    def head(self, key: str) -> int | None:
        """The stored object's size in bytes, or None when the key is absent."""
        ...


class ReconcileStore(Protocol):
    """What the reconciliation needs on top of reads: list, identify, delete.
    The scheduled archive writer's store class has none of these.

    `delete` is UNCONDITIONAL. Cloudflare R2 accepts `DeleteObject` with a
    wrong `If-Match` and deletes anyway (verified against the real service
    2026-11-04; its S3 compatibility page lists no precondition for
    DeleteObject), so a HEAD followed by a DELETE is never a compare-and-
    delete, and nothing here pretends it is."""

    def get(self, key: str) -> bytes: ...

    def stat(self, key: str) -> ObjectInfo | None: ...

    def list(self, prefix: str) -> list[ObjectInfo]: ...

    def delete(self, key: str) -> None:
        """Delete unconditionally; succeeds for an absent key too (S3 semantics)."""
        ...


class MemoryArchiveStore:
    """In-memory store for tests and for proving a dry run touches nothing.
    Thread-safe so concurrent-writer tests exercise the conditional put.
    Implements both the writer's and the reconciler's interface; `clock`
    stamps `last_modified` so grace periods can be tested."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        import threading

        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.modified: dict[str, datetime] = {}
        self.calls: list[tuple[str, str]] = []
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    @staticmethod
    def _etag(data: bytes) -> str:
        return '"' + hashlib.md5(data).hexdigest() + '"'  # S3's single-part ETag shape

    def put(self, key: str, data: bytes, metadata: Mapping[str, str] | None = None, *, if_none_match: bool = False) -> None:
        with self._lock:
            self.calls.append(("put", key))
            if if_none_match and key in self.objects:
                raise ArchiveObjectExistsError(f"object already exists at {key}")
            self.objects[key] = bytes(data)
            self.metadata[key] = dict(metadata or {})
            self.modified[key] = self.clock()

    def get(self, key: str) -> bytes:
        with self._lock:
            self.calls.append(("get", key))
            return self.objects[key]

    def head(self, key: str) -> int | None:
        with self._lock:
            self.calls.append(("head", key))
            data = self.objects.get(key)
            return None if data is None else len(data)

    def stat(self, key: str) -> ObjectInfo | None:
        with self._lock:
            self.calls.append(("stat", key))
            data = self.objects.get(key)
            if data is None:
                return None
            return ObjectInfo(key, len(data), self._etag(data), self.modified[key], dict(self.metadata.get(key, {})))

    def list(self, prefix: str) -> list[ObjectInfo]:
        with self._lock:
            self.calls.append(("list", prefix))
            return [ObjectInfo(key, len(data), self._etag(data), self.modified[key])
                    for key, data in sorted(self.objects.items()) if key.startswith(prefix)]

    def delete(self, key: str) -> None:
        """Unconditional, like R2's DeleteObject: whatever is at the key now
        is removed, and an absent key is not an error."""
        with self._lock:
            self.calls.append(("delete", key))
            self.objects.pop(key, None)
            self.metadata.pop(key, None)
            self.modified.pop(key, None)


_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
# Up to four path segments; a segment must start with a letter, digit, `_` or
# `-`, so `.`/`..` segments (and anything starting with a dot) are refused.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}(/[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}){0,3}$")
_REQUIRED_SETTINGS = ("R2_ACCOUNT_ID", "R2_BUCKET_NAME")
# Two identities, two tokens: the scheduled writer (PutObject, GetObject,
# HeadObject, HeadBucket — never list, never delete) and the reconciler
# (ListBucket, GetObject, HeadObject, DeleteObject — never put).
WRITER_CREDENTIALS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
RECONCILE_CREDENTIALS = ("R2_RECONCILE_ACCESS_KEY_ID", "R2_RECONCILE_SECRET_ACCESS_KEY")


@dataclass(frozen=True)
class R2Config:
    """Validated R2 settings. Secrets are excluded from `repr` so a traceback,
    a log line or a debugger dump never prints them."""

    account_id: str = field(repr=False)
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket: str
    prefix: str = "hub-product-analytics"
    role: str = "writer"

    @classmethod
    def from_settings(cls, settings: Any, *, credentials: tuple[str, str] = WRITER_CREDENTIALS) -> "R2Config":
        key_name, secret_name = credentials
        names = ("R2_ACCOUNT_ID", key_name, secret_name, "R2_BUCKET_NAME")
        values = {name: str(getattr(settings, name, "") or "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ArchiveConfigError("missing archive storage configuration: " + ", ".join(missing))
        if not _ACCOUNT_RE.match(values["R2_ACCOUNT_ID"]):
            raise ArchiveConfigError("R2_ACCOUNT_ID is not a valid account identifier")
        if not _BUCKET_RE.match(values["R2_BUCKET_NAME"]):
            raise ArchiveConfigError("R2_BUCKET_NAME is not a valid bucket name")
        if any(ch.isspace() for ch in values[key_name] + values[secret_name]) or len(values[secret_name]) < 16:
            raise ArchiveConfigError(f"{key_name} / {secret_name} are not usable credentials")
        prefix = str(getattr(settings, "PRODUCT_ANALYTICS_ARCHIVE_PREFIX", "") or "").strip().strip("/")
        if not _PREFIX_RE.match(prefix):
            raise ArchiveConfigError("PRODUCT_ANALYTICS_ARCHIVE_PREFIX is not a valid key prefix")
        return cls(
            account_id=values["R2_ACCOUNT_ID"],
            access_key_id=values[key_name],
            secret_access_key=values[secret_name],
            bucket=values["R2_BUCKET_NAME"],
            prefix=prefix,
            role="reconcile" if credentials == RECONCILE_CREDENTIALS else "writer",
        )

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def describe(self) -> str:
        """Safe for logs: the bucket and prefix only."""
        return f"bucket={self.bucket} prefix={self.prefix}"


def _client_error_code(exc: Exception) -> tuple[str, int | None]:
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code, status


def _is_not_found(exc: Exception) -> bool:
    code, status = _client_error_code(exc)
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _is_precondition_failed(exc: Exception) -> bool:
    code, status = _client_error_code(exc)
    return code in {"PreconditionFailed", "412"} or status == 412


class R2ArchiveStore:
    """Cloudflare R2 through boto3's S3 client.

    Integrity is established by reading the object back and comparing its
    SHA-256 (`ensure_archived`), not by trusting a checksum header: that is
    what proves the bytes are durably stored under the key. boto3's newer
    automatic request checksums are switched to `when_required` because R2
    does not accept them on every operation; `Content-MD5` is still sent on
    upload so a corrupted transfer is rejected by the service itself.

    Timeouts are deliberately short (`CLIENT_TIMEOUTS`): while a batch
    uploads, its Hub rows are locked and the database transaction is idle.
    These client limits are per connect / per read and cannot bound a slow
    trickle, so the enforced bound is `STORAGE_CALL_DEADLINE_S` applied by
    `call_with_deadline` around every call; the client limits only keep the
    common failures (unreachable host, dead socket) from using up that
    deadline. A failed call fails the batch closed: nothing is deleted and
    the next run retries.
    """

    CLIENT_TIMEOUTS = {"connect_timeout": 10, "read_timeout": 45, "max_attempts": 2}

    def __init__(self, config: R2Config, client: Any | None = None) -> None:
        self._bucket = config.bucket
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: R2Config) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name="auto",
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": R2ArchiveStore.CLIENT_TIMEOUTS["max_attempts"], "mode": "standard"},
                connect_timeout=R2ArchiveStore.CLIENT_TIMEOUTS["connect_timeout"],
                read_timeout=R2ArchiveStore.CLIENT_TIMEOUTS["read_timeout"],
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def put(self, key: str, data: bytes, metadata: Mapping[str, str] | None = None, *, if_none_match: bool = False) -> None:
        """Upload; with `if_none_match`, send `If-None-Match: *` so the service
        itself refuses to overwrite an existing key (HTTP 412), closing the
        window between a HEAD and a PUT from another writer. S3 and R2 both
        document this conditional write; the real-R2 probe in the runbook
        confirms it before the job is scheduled."""
        from botocore.exceptions import ClientError

        arguments: dict[str, Any] = dict(
            Bucket=self._bucket,
            Key=key,
            Body=bytes(data),
            ContentType="application/zip",
            ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode("ascii"),
            Metadata=dict(metadata or {}),
        )
        if if_none_match:
            arguments["IfNoneMatch"] = "*"
        try:
            self._client.put_object(**arguments)
        except ClientError as exc:
            if if_none_match and _is_precondition_failed(exc):
                raise ArchiveObjectExistsError(f"object already exists at {key}") from exc
            raise

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def head(self, key: str) -> int | None:
        from botocore.exceptions import ClientError

        try:
            return int(self._client.head_object(Bucket=self._bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise

    def probe(self) -> None:
        """Read-only: confirm the bucket exists and the credentials may see it."""
        self._client.head_bucket(Bucket=self._bucket)


class R2ReconcileStore(R2ArchiveStore):
    """The reconciliation's view of the bucket: everything the writer has,
    plus list, HEAD-with-metadata and an unconditional delete. Built only by
    `scripts/reconcile_product_analytics_archives.py`, with the reconciliation
    credentials; the archive job never constructs this class, so the code
    that can delete and the code that uploads are different objects even
    while Cloudflare's token model cannot separate the two permissions."""

    def stat(self, key: str) -> ObjectInfo | None:
        from botocore.exceptions import ClientError

        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise
        return ObjectInfo(key, int(head["ContentLength"]), str(head["ETag"]), _utc(head["LastModified"]), dict(head.get("Metadata") or {}))

    def list(self, prefix: str) -> list[ObjectInfo]:
        """Every object under `prefix` (paginated). Listings carry no user
        metadata and may lag; the reconciler re-inspects each object (`stat`)
        before acting on it."""
        found: list[ObjectInfo] = []
        for page in self._client.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                found.append(ObjectInfo(str(item["Key"]), int(item["Size"]), str(item["ETag"]), _utc(item["LastModified"])))
        return found

    def delete(self, key: str) -> None:
        """Unconditional. R2 ignores `If-Match` on DeleteObject (verified
        2026-11-04), so no precondition is sent and none is assumed."""
        self._client.delete_object(Bucket=self._bucket, Key=key)


# ── Archive format ───────────────────────────────────────────────────────


def _utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; the models
    # store UTC, so naive values from the database are UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("archive timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported archive value type: {type(value).__name__}")


def _encode_rows(rows: list[Mapping[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(dict(row), default=_json_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        for row in rows
    )


# Characters a spreadsheet may interpret as the start of a formula or a DDE
# command when a cell begins with them (OWASP CSV injection list plus `|`).
_CSV_FORMULA_STARTS = frozenset("=+-@|")
_CONTROL_CHARACTERS = "".join(chr(c) for c in range(32)) + "\x7f"


def _csv_cell(value: Any) -> str:
    """One CSV cell, neutralised for spreadsheets. The CSV is an inspection
    copy: a cell that starts with a formula character — after any leading
    whitespace (Unicode included) or control characters — or with a control
    character is prefixed with `'`, which spreadsheets show as text. That
    also prefixes an honest negative number; the JSONL member is the lossless
    restore source, never the CSV."""
    if isinstance(value, (dict, list)):
        text_value = json.dumps(value, default=_json_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    elif isinstance(value, (datetime, date)):
        text_value = _json_value(value)
    elif value is None:
        return ""
    else:
        text_value = str(value)
    if not text_value:
        return text_value
    # Strip whitespace and control characters until nothing changes: they can
    # alternate (" \x00 =1+1"), and one pass of each would miss that.
    leading = text_value
    while True:
        stripped = leading.lstrip().lstrip(_CONTROL_CHARACTERS)
        if stripped == leading:
            break
        leading = stripped
    if (leading and leading[0] in _CSV_FORMULA_STARTS) or ord(text_value[0]) < 32:
        return "'" + text_value
    return text_value


def _encode_csv(rows: list[Mapping[str, Any]], columns: list[str]) -> bytes:
    """Human-readable companion; JSONL is the lossless restore source.
    Deterministic: fixed column order, `\\n` line ends, minimal quoting."""
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return output.getvalue().encode("utf-8")


def _time_range(rows: list[Mapping[str, Any]], column: str) -> dict[str, str] | None:
    stamps = [_utc(row[column]) for row in rows if isinstance(row.get(column), datetime)]
    if not stamps:
        return None
    return {"min": min(stamps).isoformat(), "max": max(stamps).isoformat()}


def _id_range(rows: list[Mapping[str, Any]]) -> dict[str, int] | None:
    ids = [int(row["id"]) for row in rows if isinstance(row.get("id"), int)]
    if not ids:
        return None
    return {"min": min(ids), "max": max(ids)}


# The internal View As diagnostics. They are never returned by any read and
# no feature restores or analyses them; whether they belong in an archive is
# an open policy question (see PRODUCT_ANALYTICS_ARCHIVE.md, "Privacy").
VIEW_AS_COLUMNS = frozenset({"view_as_role_key", "view_as_scope_key", "view_as_context"})
VIEW_AS_POLICIES = ("include", "omit")


def archive_columns(table: str, *, exclude: Iterable[str] = ()) -> list[str]:
    excluded = set(exclude)
    return [column.name for column in TABLES[table].__table__.columns if column.name not in excluded]


def archive_row(row: Any, table: str, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    """The archived columns of one analytics row, timestamps normalised to UTC."""
    out: dict[str, Any] = {}
    for name in archive_columns(table, exclude=exclude):
        value = getattr(row, name)
        out[name] = _utc(value) if isinstance(value, datetime) else value
    return out


def make_archive(
    *,
    environment: str,
    events: Iterable[Mapping[str, Any]] = (),
    engagements: Iterable[Mapping[str, Any]] = (),
    metadata: Mapping[str, Any] | None = None,
    exclude_columns: Iterable[str] = (),
) -> bytes:
    """A deterministic ZIP with JSONL restore data and CSV inspection copies.

    `metadata` must not carry run-specific values (such as a cutoff computed
    from "now"); it is part of the bytes, and identical rows must always
    produce identical bytes for crash recovery to recognise a prior upload.
    `exclude_columns` names columns deliberately left out of the rows; the
    manifest's `columns` lists exactly what each record carries.
    """
    if environment not in ENVIRONMENTS:
        raise ValueError("archive environment must be configured")
    excluded = set(exclude_columns)
    # Omitted columns are omitted everywhere: manifest, CSV and JSONL agree.
    event_rows = [{k: v for k, v in dict(row).items() if k not in excluded} for row in events]
    engagement_rows = [{k: v for k, v in dict(row).items() if k not in excluded} for row in engagements]
    for row in event_rows + engagement_rows:
        if row.get("environment") != environment:
            raise ValueError("archive rows must belong to the configured environment")
    encoded = {"events.jsonl": _encode_rows(event_rows), "engagements.jsonl": _encode_rows(engagement_rows)}
    columns = {table: archive_columns(table, exclude=exclude_columns) for table in TABLES}
    encoded.update({"events.csv": _encode_csv(event_rows, columns["events"]), "engagements.csv": _encode_csv(engagement_rows, columns["engagements"])})
    record_ids = sorted(
        [str(row.get("event_id")) for row in event_rows] + [str(row.get("engagement_id")) for row in engagement_rows]
    )
    manifest = {
        "format_version": FORMAT_VERSION,
        "environment": environment,
        "events": len(event_rows),
        "engagements": len(engagement_rows),
        "record_types": [name for name, rows in (("events", event_rows), ("engagements", engagement_rows)) if rows],
        "time_range": {
            "events": _time_range(event_rows, "occurred_at"),
            "engagements": _time_range(engagement_rows, "ended_at"),
        },
        "id_range": {"events": _id_range(event_rows), "engagements": _id_range(engagement_rows)},
        "columns": {
            "events": columns["events"],
            "engagements": columns["engagements"],
        },
        "omitted_columns": sorted(set(exclude_columns)),
        "members": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "rows": (len(event_rows) if name.startswith("events.") else len(engagement_rows))}
            for name, data in encoded.items()
        },
        "record_digest": hashlib.sha256("\n".join(record_ids).encode("utf-8")).hexdigest(),
        "metadata": dict(metadata or {}),
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, data in [(MANIFEST_NAME, json.dumps(manifest, sort_keys=True).encode("utf-8")), *encoded.items()]:
            info = ZipInfo(name, date_time=_FIXED_ZIP_TIME)
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def validate_archive(payload: bytes) -> dict[str, Any]:
    """Open and check an archive end to end; return its manifest.

    Verifies the ZIP structure, the manifest version and environment, each
    member's SHA-256, size and row count, that every line is a JSON object
    with the expected record id and the manifest's environment, and that the
    manifest counts match the members. Raises ArchiveIntegrityError.
    """
    try:
        archive = ZipFile(BytesIO(payload))
    except BadZipFile as exc:
        raise ArchiveIntegrityError("archive is not a valid ZIP file") from exc
    with archive:
        if archive.testzip() is not None:
            raise ArchiveIntegrityError("archive has a corrupt member")
        names = set(archive.namelist())
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except ValueError as exc:
            raise ArchiveIntegrityError("archive manifest is not valid JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("format_version") not in (LEGACY_FORMAT_VERSION, FORMAT_VERSION):
            raise ArchiveIntegrityError("archive format version is not supported")
        expected = {MANIFEST_NAME, *MEMBER_NAMES.values()}
        if manifest["format_version"] == FORMAT_VERSION:
            expected.update(CSV_MEMBER_NAMES.values())
        if names != expected or len(archive.namelist()) != len(expected):
            raise ArchiveIntegrityError("archive members do not match the format")
        environment = manifest.get("environment")
        if environment not in ENVIRONMENTS:
            raise ArchiveIntegrityError("archive environment is not valid")
        members = manifest.get("members") if isinstance(manifest.get("members"), dict) else {}
        omitted = manifest.get("omitted_columns") or []
        if not isinstance(omitted, list) or any(not isinstance(c, str) for c in omitted):
            raise ArchiveIntegrityError("archive omitted_columns are invalid")
        omitted_set = set(omitted)
        for table, name in MEMBER_NAMES.items():
            data = archive.read(name)
            info = members.get(name) if isinstance(members.get(name), dict) else {}
            if info.get("sha256") != hashlib.sha256(data).hexdigest() or info.get("bytes") != len(data):
                raise ArchiveIntegrityError(f"{name} does not match its manifest digest")
            lines = data.split(b"\n")
            if lines and lines[-1] == b"":
                lines.pop()
            if info.get("rows") != len(lines) or manifest.get(table) != len(lines):
                raise ArchiveIntegrityError(f"{name} row count does not match its manifest")
            records = []
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise ArchiveIntegrityError(f"{name} contains an invalid record") from exc
                if not isinstance(record, dict) or not record.get(RECORD_ID_COLUMN[table]) or record.get("environment") != environment:
                    raise ArchiveIntegrityError(f"{name} contains a record that does not belong to this archive")
                if omitted_set & set(record):
                    raise ArchiveIntegrityError(f"{name} contains a column the manifest declares omitted")
                records.append(record)
            if manifest["format_version"] == FORMAT_VERSION:
                csv_name = CSV_MEMBER_NAMES[table]
                csv_data = archive.read(csv_name)
                csv_info = members.get(csv_name) if isinstance(members.get(csv_name), dict) else {}
                columns = (manifest.get("columns") or {}).get(table)
                if not isinstance(columns, list) or any(not isinstance(c, str) for c in columns):
                    raise ArchiveIntegrityError("archive columns are invalid")
                if (csv_info.get("sha256") != hashlib.sha256(csv_data).hexdigest()
                    or csv_info.get("bytes") != len(csv_data) or csv_info.get("rows") != len(records)
                    or csv_data != _encode_csv(records, columns)):
                    raise ArchiveIntegrityError(f"{csv_name} does not match its JSONL records")
        return manifest


def ensure_archived(
    store: ArchiveStore, *, key: str, payload: bytes, heartbeat: Callable[[], Any] | None = None,
    deadline: float | None = None, metadata: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """Make sure `payload` is durably stored under `key`; return (sha256, uploaded).

    An existing object is read back and must match byte for byte (crash
    recovery after an earlier upload); a different object under the same
    key is refused and never overwritten. The upload itself is conditional
    (`If-None-Match: *`): if another writer took the key between the HEAD and
    the PUT, the service refuses the PUT and the other writer's object is
    read back and compared instead. A fresh upload is read back and verified
    the same way before it counts.

    Keys also embed the first 16 hex digits of the payload's SHA-256, so two
    writers can only collide on a key when they hold the same bytes; a
    different object under the same key is therefore a corruption signal.

    `heartbeat` is called between consecutive storage calls; the job passes a
    trivial database statement so an open batch transaction is never idle for
    longer than one storage call. `deadline` bounds each storage call
    (`call_with_deadline`): at most three calls are made — HEAD, then either
    GET (object already present) or PUT and GET (fresh upload).
    """
    if not key or not payload:
        raise ValueError("archive key and payload are required")
    expected = hashlib.sha256(payload).hexdigest()
    beat = heartbeat or (lambda: None)

    def matches_existing() -> bool:
        beat()
        return hashlib.sha256(call_with_deadline(lambda: store.get(key), deadline, what="readback (GET)")).hexdigest() == expected

    existing = call_with_deadline(lambda: store.head(key), deadline, what="HEAD")
    if existing is not None:
        if existing == len(payload) and matches_existing():
            return expected, False
        raise ArchiveIntegrityError(f"an object already exists at {key} with different content")
    beat()
    try:
        call_with_deadline(
            lambda: store.put(key, payload, {**dict(metadata or {}), "sha256": expected, "format-version": str(FORMAT_VERSION)}, if_none_match=True),
            deadline, what="upload (PUT)",
        )
    except ArchiveObjectExistsError:
        if matches_existing():
            return expected, False
        raise ArchiveIntegrityError(f"an object already exists at {key} with different content") from None
    if not matches_existing():
        raise ArchiveIntegrityError("archive readback verification failed")
    return expected, True


def put_and_verify(store: ArchiveStore, *, key: str, payload: bytes) -> str:
    """Return the SHA-256 only after reading back and verifying exact bytes."""
    if not key or not payload:
        raise ValueError("archive key and payload are required")
    expected = hashlib.sha256(payload).hexdigest()
    store.put(key, payload)
    if hashlib.sha256(store.get(key)).hexdigest() != expected:
        raise ArchiveIntegrityError("archive readback verification failed")
    return expected


def archive_key(*, prefix: str, environment: str, table: str, first_day: date, first_id: int, last_id: int, digest: str) -> str:
    """`<prefix>/<env>/<table>/v<format>/<year>/<day>_<first id>-<last id>_<digest16>.zip`.

    Nothing personal: an environment, a table name, a calendar day, database
    ids and a digest. The day is that of the oldest record in the batch.
    """
    day = first_day.isoformat()
    return f"{prefix}/{environment}/{table}/v{FORMAT_VERSION}/{day[:4]}/{day}_{first_id}-{last_id}_{digest[:16]}.zip"


# ── Selection and orchestration ──────────────────────────────────────────


def _check_environment(environment: str) -> None:
    if environment not in ENVIRONMENTS:
        raise ValueError("a valid archive environment is required")


def _expired_live_statement(table: str, *, environment: str, cutoff: datetime):
    """Age-expired rows of this environment that are NOT reliably tied to a
    deleted resource. The deleted-resource rule is the cleanup's own
    predicate, so the two can never disagree about a row."""
    model = TABLES[table]
    stamp = getattr(model, TIMESTAMP_COLUMN[table])
    not_deleted = text("NOT (" + service._deleted_page_predicate(table).text + ")")
    return select(model).where(model.environment == environment, stamp < cutoff, not_deleted)


def plan_archive(db: Session, *, table: str, environment: str, cutoff: datetime, batch_size: int) -> dict[str, Any]:
    """Read-only: how many rows a live run would archive, and in how many batches."""
    _check_environment(environment)
    model = TABLES[table]
    stamp = getattr(model, TIMESTAMP_COLUMN[table])
    statement = _expired_live_statement(table, environment=environment, cutoff=cutoff)
    count = int(db.execute(statement.with_only_columns(func.count())).scalar_one())
    oldest = db.execute(statement.with_only_columns(func.min(stamp))).scalar_one() if count else None
    return {
        "table": table,
        "rows": count,
        "batches": math.ceil(count / batch_size) if count else 0,
        "oldest": _utc(oldest).isoformat() if isinstance(oldest, datetime) else None,
    }


# ── Registry ─────────────────────────────────────────────────────────────


def require_archive_registry(db: Session) -> None:
    """Refuse to run without the registry table (fail closed, before any
    storage call): without it nothing could be registered, so nothing may be
    deleted either."""
    try:
        db.execute(select(HubProductAnalyticsArchiveV2.id).limit(0)).all()
    except (ProgrammingError, OperationalError):
        db.rollback()
        raise ArchiveConfigError(
            f"the archive registry table {REGISTRY_TABLE} does not exist; "
            "run python -m scripts.create_product_analytics_archive_tables first"
        ) from None


def _insert_ignoring_conflicts(db: Session, table, values: Mapping[str, Any], *, conflict_columns: list[str], returning_id: bool = False):
    """INSERT … ON CONFLICT (…) DO NOTHING on PostgreSQL and SQLite. Returns
    the rows inserted (0 or 1), or, with `returning_id`, the list of new ids
    (empty on conflict)."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        statement = pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
    else:  # pragma: no cover - no other dialect is supported
        statement = table.insert().values(**values)
    if returning_id:
        return [int(value) for value in db.execute(statement.returning(table.c.id)).scalars().all()]
    return int(db.execute(statement).rowcount or 0)


REGISTRY_ID_METADATA = "registry-id"


def lookup_registry_identity(db: Session) -> tuple[str | None, tuple[str, str] | None]:
    """READ-ONLY: (identity, (bucket, prefix)) — identity None when the
    singleton row does not exist yet, binding None while unbound. Never
    inserts, updates, binds or commits; dry runs and reports use this."""
    table = HubProductAnalyticsArchiveIdentityV2
    try:
        row = db.execute(select(table.id, table.bucket, table.prefix).where(table.singleton == 1)).first()
    except (ProgrammingError, OperationalError):
        db.rollback()
        raise ArchiveConfigError(
            f"the registry identity table {table.__tablename__} does not exist (or is outdated); "
            "run python -m scripts.create_product_analytics_archive_tables first"
        ) from None
    if row is None:
        return None, None
    binding = (str(row[1]), str(row[2])) if row[1] is not None else None
    return str(row[0]), binding


def registry_storage_binding(db: Session) -> tuple[str, str] | None:
    """(bucket, prefix) the identity is bound to, or None before first use. Read-only."""
    return lookup_registry_identity(db)[1]


def check_registry_storage_binding(binding: tuple[str, str] | None, storage: tuple[str, str] | None) -> None:
    """Refuse (ArchiveConfigError) when the identity is bound to storage other
    than the caller's. Pure; used by every live path before it writes."""
    if storage is not None and binding is not None and tuple(binding) != tuple(storage):
        raise ArchiveConfigError(
            "the registry identity is bound to a different bucket/prefix than this run is configured for "
            "(a cloned or restored database must not claim another deployment's objects); "
            "re-bind deliberately with python -m scripts.create_product_analytics_archive_tables --rebind-storage "
            "after confirming no other deployment uses that bucket and prefix"
        )


def registry_identity(db: Session, *, storage: tuple[str, str] | None = None, bind_storage: bool = True) -> str:
    """INITIALISING lookup, for the live archive job and the create script
    only: the single row of `hub_product_analytics_archive_identity`,
    created atomically on first use — one `INSERT … ON CONFLICT (singleton)
    DO NOTHING` per caller, then every caller reads the row that won. Commits
    its own tiny transaction. Dry runs, reports and the reconciliation must
    use `lookup_registry_identity` instead, which writes nothing.

    `storage` = (bucket, prefix) the caller is configured for. On first use
    the identity is bound to it; later, a different bucket or prefix is
    refused (`ArchiveConfigError`) — the sign of a cloned or restored
    database pointed at other storage, which must not claim or delete
    objects there until an operator re-binds it deliberately
    (`scripts.create_product_analytics_archive_tables --rebind-storage`).
    `bind_storage=False` initialises the id but neither binds nor refuses.
    """
    import uuid

    table = HubProductAnalyticsArchiveIdentityV2
    try:
        _insert_ignoring_conflicts(
            db, table.__table__, {"singleton": 1, "id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc)},
            conflict_columns=["singleton"],
        )
        db.commit()
        row = db.execute(select(table.id, table.bucket, table.prefix).where(table.singleton == 1)).one()
    except (ProgrammingError, OperationalError):
        db.rollback()
        raise ArchiveConfigError(
            f"the registry identity table {table.__tablename__} does not exist (or is outdated); "
            "run python -m scripts.create_product_analytics_archive_tables first"
        ) from None
    identity, bucket, prefix = str(row[0]), row[1], row[2]
    if storage is not None and bind_storage:
        wanted_bucket, wanted_prefix = storage
        if bucket is None and prefix is None:
            db.execute(
                table.__table__.update().where(table.singleton == 1, table.bucket.is_(None))
                .values(bucket=wanted_bucket, prefix=wanted_prefix)
            )
            db.commit()
            row = db.execute(select(table.bucket, table.prefix).where(table.singleton == 1)).one()
            bucket, prefix = row[0], row[1]
        if bucket is not None:
            db.rollback()
            check_registry_storage_binding((str(bucket), str(prefix)), (wanted_bucket, wanted_prefix))
    return identity


def rebind_registry_identity_storage(db: Session, *, bucket: str, prefix: str) -> str:
    """Operator action: bind the identity to the configured storage even if
    it was bound elsewhere. Never rotates the id (objects already uploaded
    keep their marker; restore does not depend on the marker)."""
    identity = registry_identity(db, storage=None)
    table = HubProductAnalyticsArchiveIdentityV2
    db.execute(table.__table__.update().where(table.singleton == 1).values(bucket=bucket, prefix=prefix))
    db.commit()
    return identity


def register_archive(db: Session, *, key: str, environment: str, table: str, digest: str, manifest: Mapping[str, Any],
                     registered_at: datetime) -> bool:
    """Insert the registry row for `key` in the CURRENT transaction (the one
    holding the batch's locks); False if the key is already registered.

    The insert is `ON CONFLICT (key) DO NOTHING`: on PostgreSQL the batch
    lock and the `FOR UPDATE` rows make two registrations of one key
    impossible, but a database without those locks (SQLite, tests) can race
    two identical batches, and a unique violation must then be "already
    registered", not an aborted transaction."""
    time_range = (manifest.get("time_range") or {}).get(table) or {}
    id_range = (manifest.get("id_range") or {}).get(table) or {}
    inserted = _insert_ignoring_conflicts(
        db, HubProductAnalyticsArchiveV2.__table__,
        dict(key=key, environment=environment, table_name=table, sha256=digest, format_version=int(manifest["format_version"]),
             records=int(manifest[table]), first_id=int(id_range["min"]), last_id=int(id_range["max"]),
             time_min=datetime.fromisoformat(time_range["min"]), time_max=datetime.fromisoformat(time_range["max"]),
             omitted_columns=list(manifest.get("omitted_columns") or []), registered_at=registered_at),
        conflict_columns=["key"],
    )
    return inserted == 1


def _insert_trusted_archive_rows(db: Session, payload: bytes, *, include_deleted_resources: bool = False) -> dict[str, Any]:
    """INTERNAL. Insert a payload's JSONL records into the live tables.

    Trust requirements: `payload` must already be a registered archive whose
    bytes were verified against the registry (`restore_registered_archive`
    does that); this helper only validates the archive's own consistency and
    environment, so handing it an unregistered leftover or any bytes of
    unknown origin would write their records. The `id` column is not
    restored (new ids); a record already present (same `event_id` /
    `engagement_id`) is left untouched; columns the archive omitted come back
    NULL. Records that match the deleted-resource rule NOW are removed again
    within the same transaction unless `include_deleted_resources` (an
    explicit recovery mode). The caller commits or rolls back.
    """
    manifest = validate_archive(payload)
    result: dict[str, Any] = {"environment": manifest["environment"], "inserted": {}, "already_present": {}, "deleted_resource_skipped": {}}
    with ZipFile(BytesIO(payload)) as zipped:
        for table, member in MEMBER_NAMES.items():
            model = TABLES[table]
            datetime_columns = {column.name for column in model.__table__.columns if isinstance(column.type, DateTime)}
            allowed = {column.name for column in model.__table__.columns} - {"id"}
            new_ids: list[int] = []
            total = 0
            for line in zipped.read(member).decode("utf-8").splitlines():
                total += 1
                record = json.loads(line)
                if record.get("environment") != manifest["environment"]:
                    raise ArchiveIntegrityError(f"{member} contains a record of another environment")
                values = {name: value for name, value in record.items() if name in allowed}
                for name in datetime_columns & set(values):
                    if isinstance(values[name], str):
                        values[name] = datetime.fromisoformat(values[name])
                new_ids.extend(_insert_ignoring_conflicts(db, model.__table__, values, conflict_columns=[RECORD_ID_COLUMN[table]], returning_id=True))
            skipped = 0
            if new_ids and not include_deleted_resources:
                predicate = text("(" + service._deleted_page_predicate(table).text + ")")
                skipped = int(db.execute(model.__table__.delete().where(model.id.in_(new_ids), predicate)).rowcount or 0)
            result["inserted"][table] = len(new_ids) - skipped
            result["already_present"][table] = total - len(new_ids)
            result["deleted_resource_skipped"][table] = skipped
    return result


def restore_registered_archive(
    db: Session, store: ArchiveStore, *, key: str, environment: str, include_deleted_resources: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """The operator restore entry point. Starts from the registry, never from
    the bucket: the key must be a registered archive of `environment`; the
    object is downloaded and its SHA-256 must equal the registered digest;
    the archive must validate, carry a supported format version and the
    registered environment, which must be the database's own. Only then are
    its records inserted (`_insert_trusted_archive_rows`); a refusal writes
    nothing. An unregistered leftover, a tampered object or an object of
    another environment cannot pass. `dry_run` performs the checks and the
    download and writes nothing.
    """
    _check_environment(environment)
    row = db.execute(select(HubProductAnalyticsArchiveV2).where(HubProductAnalyticsArchiveV2.key == key)).scalar_one_or_none()
    if row is None:
        raise ArchiveRestoreError("the key is not a registered archive; leftovers and unknown objects are never restored")
    if row.environment != environment:
        raise ArchiveRestoreError(f"the archive is registered for environment {row.environment}, not {environment}")
    if int(row.format_version) not in (LEGACY_FORMAT_VERSION, FORMAT_VERSION):
        raise ArchiveRestoreError("the registered format version is not supported by this code")
    size = store.head(key)
    if size is None:
        raise ArchiveRestoreError("the registered object is missing from storage; nothing was written")
    payload = store.get(key)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != row.sha256:
        raise ArchiveRestoreError("the object's SHA-256 does not match the registry; nothing was written")
    manifest = validate_archive(payload)
    if manifest.get("environment") != environment or int(manifest.get("format_version", 0)) != int(row.format_version):
        raise ArchiveRestoreError("the archive's manifest does not match the registry; nothing was written")
    if int(manifest.get(row.table_name, -1)) != int(row.records):
        raise ArchiveRestoreError("the archive's record count does not match the registry; nothing was written")
    summary = {"key": key, "table": row.table_name, "records": int(row.records), "sha256": digest, "format_version": int(row.format_version)}
    if dry_run:
        return {**summary, "dry_run": True}
    return {**summary, **_insert_trusted_archive_rows(db, payload, include_deleted_resources=include_deleted_resources)}


def registered_keys(db: Session, keys: Iterable[str]) -> set[str]:
    wanted = {key for key in keys if key}
    if not wanted:
        return set()
    return set(db.execute(select(HubProductAnalyticsArchiveV2.key).where(HubProductAnalyticsArchiveV2.key.in_(wanted))).scalars())


def unregistered_keys(db: Session, keys: Iterable[str]) -> list[str]:
    """Of the object keys an operator listed from the bucket, those that are
    NOT archives (abandoned uploads to reconcile). Read-only."""
    wanted = [key for key in keys if key]
    known = registered_keys(db, wanted)
    return [key for key in wanted if key not in known]


def registry_summary(db: Session, environment: str | None = None) -> list[dict[str, Any]]:
    """Per environment and table: registered archives, records, latest registration. Read-only."""
    statement = select(
        HubProductAnalyticsArchiveV2.environment, HubProductAnalyticsArchiveV2.table_name,
        func.count(), func.sum(HubProductAnalyticsArchiveV2.records), func.max(HubProductAnalyticsArchiveV2.registered_at),
    ).group_by(HubProductAnalyticsArchiveV2.environment, HubProductAnalyticsArchiveV2.table_name)
    if environment is not None:
        statement = statement.where(HubProductAnalyticsArchiveV2.environment == environment)
    return [
        {"environment": env, "table": table, "archives": int(archives), "records": int(records or 0),
         "latest": _utc(latest).isoformat() if isinstance(latest, datetime) else None}
        for env, table, archives, records, latest in db.execute(statement.order_by(HubProductAnalyticsArchiveV2.environment, HubProductAnalyticsArchiveV2.table_name)).all()
    ]


@dataclass
class ArchiveOutcome:
    table: str
    archived: int = 0
    deleted: int = 0
    batches: int = 0
    reused: int = 0
    registered: int = 0
    # Batches abandoned BEFORE any upload because a resource some of their
    # rows depend on was deleted between the selection and the resource lock
    # (or a deletion was in flight and committed while the lock waited). The
    # batch is selected again without those rows.
    reselected: int = 0
    keys: list[str] = field(default_factory=list)


def _deleted_resource_count(db: Session, *, table: str, ids: list[int]) -> int:
    model = TABLES[table]
    predicate = text("(" + service._deleted_page_predicate(table).text + ")")
    return int(db.execute(
        select(func.count()).select_from(model).where(model.id.in_(ids), predicate)
    ).scalar_one())


def referenced_resources(table: str, records: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """The Hub rows `_deleted_page_predicate` consults for these records, by
    kind: `nav_tabs` (document ids), `pages` (document ids looked up in both
    `tabs` and `gridstacks`) and `components` (links). Exactly the predicate's
    joins, nothing more: a mirror's target, a page's nav tab or a component's
    canvas are not referenced and are never locked."""
    refs: dict[str, set[str]] = {"nav_tabs": set(), "pages": set(), "components": set()}
    for record in records:
        if table == "events":
            name = record.get("event_name")
            if record.get("component_id"):
                refs["components"].add(str(record["component_id"]))
            elif record.get("page_id"):
                refs["pages"].add(str(record["page_id"]))
            target = record.get("target_id")
            if not target:
                continue
            if name == "navigation_tab.selected":
                refs["nav_tabs"].add(str(target))
            elif name in _PAGE_SELECTION_EVENTS:
                refs["pages"].add(str(target))
            elif name == "blocknote_tab.selected":
                refs["components"].add(str(target))
        else:
            if record.get("nav_tab_id"):
                refs["nav_tabs"].add(str(record["nav_tab_id"]))
            if record.get("tab_id"):
                refs["pages"].add(str(record["tab_id"]))
    return refs


def _row_locks_available(db: Session) -> bool:
    bind = db.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


_UNIQUE_COLUMNS_SQL = text("""
    SELECT a.attname
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
    WHERE c.relname = :table AND pg_table_is_visible(c.oid)
      AND i.indisunique AND i.indisvalid AND i.indpred IS NULL AND i.indnkeyatts = 1
""")


def resource_lock_modes(db: Session) -> dict[str, str]:
    """Per Hub table, the lock that keeps its join column from changing while
    a batch uploads: `key_share` when the live catalog has a valid, complete,
    single-column unique index on that column (then `FOR KEY SHARE` blocks
    both DELETE and a change of the column), else `share` (`FOR SHARE`,
    which blocks every UPDATE and DELETE of the referenced rows). On a
    dialect without row locks every table maps to `share` but nothing is
    locked (`lock_referenced_resources` returns False)."""
    modes: dict[str, str] = {}
    for table, column in RESOURCE_LOCK_COLUMNS.items():
        unique = set()
        if _row_locks_available(db):
            unique = set(db.execute(_UNIQUE_COLUMNS_SQL, {"table": table}).scalars())
        modes[table] = "key_share" if column in unique else "share"
    return modes


def describe_lock_modes(modes: Mapping[str, str]) -> str:
    """One safe line for the job output: which lock each table gets and why."""
    parts = []
    for table, column in RESOURCE_LOCK_COLUMNS.items():
        mode = modes.get(table, "share")
        parts.append(f"{table} FOR {'KEY SHARE' if mode == 'key_share' else 'SHARE'} ({table}.{column} {'has' if mode == 'key_share' else 'has no'} unique index)")
    return "resource locks while a batch uploads: " + "; ".join(parts) + "."


def lock_referenced_resources(
    db: Session, table: str, records: Iterable[Mapping[str, Any]], modes: Mapping[str, str] | None = None,
) -> bool:
    """Row-lock the Hub rows the batch depends on until this transaction ends.

    `FOR KEY SHARE` (unique join column) blocks `DELETE` and any change of
    that column; ordinary updates (title, settings, a component's position
    or canvas) proceed. `FOR SHARE` (non-unique join column) blocks every
    UPDATE and DELETE of those rows. Rows not referenced by the batch are
    untouched either way. The deleted-resource rule can therefore not start
    matching any of the batch's rows while the archive uploads. Returns
    False on a dialect without row locks (nothing locked).
    """
    if not _row_locks_available(db):
        return False
    modes = dict(modes) if modes is not None else resource_lock_modes(db)
    refs = referenced_resources(table, records)

    def locked(statement, hub_table: str):
        mode = modes.get(hub_table, "share")
        if mode not in LOCK_MODES:
            raise ValueError(f"unknown lock mode for {hub_table}")
        return statement.with_for_update(read=True, key_share=(mode == "key_share"))

    if refs["nav_tabs"]:
        db.execute(locked(select(NavTabV2.id).where(NavTabV2.document_id.in_(refs["nav_tabs"])), "nav_tabs"))
    if refs["pages"]:
        db.execute(locked(select(TabV2.id).where(TabV2.document_id.in_(refs["pages"])), "tabs"))
        db.execute(locked(select(GridstackV2.id).where(GridstackV2.document_id.in_(refs["pages"])), "gridstacks"))
    if refs["components"]:
        db.execute(locked(select(ComponentV2.id).where(ComponentV2.link.in_(refs["components"])), "components"))
    return True


def _is_lock_timeout(exc: OperationalError) -> bool:
    origin = getattr(exc, "orig", None)
    sqlstate = getattr(origin, "pgcode", None) or getattr(getattr(origin, "diag", None), "sqlstate", None)
    return sqlstate == "55P03" or "lock timeout" in str(exc).lower() or "lock_not_available" in str(exc).lower()


def archive_expired(
    db: Session,
    store: ArchiveStore,
    *,
    table: str,
    environment: str,
    cutoff: datetime,
    prefix: str,
    batch_size: int = 5_000,
    max_batches: int | None = None,
    exclude_columns: Iterable[str] = (),
    out: Any = None,
    storage: tuple[str, str] | None = None,
    outcome: ArchiveOutcome | None = None,
    _after_select: Callable[[list[int]], None] | None = None,
) -> ArchiveOutcome:
    """Archive-before-delete, one bounded batch (one object) per transaction.

    For each batch, inside ONE database transaction (PostgreSQL):

    1. `SET LOCAL lock_timeout` / `statement_timeout`, then
       `pg_try_advisory_xact_lock(JOB_LOCK_KEY)`: another run holding it
       stops this run (`ArchiveLockedError`, nothing touched). A lock
       timeout later in the batch abandons it (nothing uploaded, nothing
       deleted) and it is retried up to `LOCK_RETRIES` times.
    2. Select the batch `FOR UPDATE`: the encoded bytes and the deleted rows
       are the same row versions, and a concurrent writer of those rows
       waits for the batch.
    3. Lock the Hub rows the deleted-resource rule consults for the batch
       (`lock_referenced_resources`, `FOR KEY SHARE` or `FOR SHARE` per the
       live catalog). From here until the commit none of them can be deleted
       or re-keyed; a deletion already in flight either commits first (then
       step 4 sees it) or waits.
    4. Re-count the batch against the deleted-resource rule. A non-zero
       count means a resource was deleted between steps 2 and 3: roll back
       and select again — nothing was uploaded.
    5. Encode, upload (or recognise an identical earlier upload), read back
       and verify, each storage call under `STORAGE_CALL_DEADLINE_S`. A
       heartbeat statement between storage calls keeps the transaction from
       idling past the server's idle-in-transaction limit.
    6. Re-count once more. On PostgreSQL this is zero by construction; a
       non-zero count (no row locks, or a path that bypassed them) is an
       integrity failure: nothing is registered or deleted and the run
       stops, naming the object for review.
    7. Register the object (`register_archive`), delete exactly those ids
       (still restricted to the environment and the cutoff) and commit. Only
       this commit makes the object an archive.

    Any exception propagates before the registry row and the delete of the
    current batch are committed; the caller rolls back (releasing every
    lock) and the run ends; rows of earlier batches were deleted only in
    their own registering transaction. `_after_select` is a test seam that
    runs between steps 2 and 3. Emits counts and keys only, never row
    contents.
    """
    _check_environment(environment)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    if not prefix or not _PREFIX_RE.match(prefix):
        raise ValueError("a valid key prefix is required")
    exclude = frozenset(exclude_columns)
    model = TABLES[table]
    stamp_name = TIMESTAMP_COLUMN[table]
    stamp = getattr(model, stamp_name)
    emit = out or (lambda line: logger.info("%s", line))
    # A caller-owned outcome keeps the counts of batches that committed even
    # if a later batch raises, so the caller can report them accurately.
    outcome = outcome if outcome is not None else ArchiveOutcome(table)
    statement = _expired_live_statement(table, environment=environment, cutoff=cutoff)
    row_locks = _row_locks_available(db)
    require_archive_registry(db)
    identity = registry_identity(db, storage=storage)  # binds on first use; refuses other storage
    modes = resource_lock_modes(db)
    if row_locks:
        emit(describe_lock_modes(modes))
    db.rollback()  # the catalog reads must not start the first batch's transaction
    object_metadata = {REGISTRY_ID_METADATA: identity, "environment": environment, "table": table}
    heartbeat = lambda: db.execute(text("SELECT 1"))  # noqa: E731 - a statement, not a function worth naming
    lock_failures = 0
    while max_batches is None or outcome.batches < max_batches:
        try:
            if row_locks:
                db.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))
                db.execute(text(f"SET LOCAL statement_timeout = '{int(STATEMENT_TIMEOUT_MS)}ms'"))
            if not try_acquire_batch_lock(db):
                db.rollback()
                raise ArchiveLockedError("another archive run holds the batch lock; this run stops without touching the batch")
            # The lock is taken by the same statement that reads the rows, so
            # the versions encoded below are the versions that will be deleted.
            rows = db.execute(statement.order_by(model.id).limit(batch_size).with_for_update()).scalars().all()
            if not rows:
                db.rollback()
                break
            records = [archive_row(row, table, exclude=exclude) for row in rows]
            ids = [int(record["id"]) for record in records]
            db.expunge_all()
            if _after_select is not None:
                _after_select(list(ids))
            lock_referenced_resources(db, table, records, modes)
        except OperationalError as exc:
            db.rollback()
            if not _is_lock_timeout(exc):
                raise
            lock_failures += 1
            emit(f"note: a {table} batch waited more than {LOCK_TIMEOUT_MS} ms for Hub rows held by another transaction; retrying ({lock_failures}/{LOCK_RETRIES}). Nothing was uploaded or deleted.")
            if lock_failures >= LOCK_RETRIES:
                raise ArchiveError(
                    f"a {table} batch could not lock the Hub rows it depends on after {LOCK_RETRIES} attempts; "
                    "the batch was not uploaded and nothing was deleted"
                ) from None
            continue
        stale = _deleted_resource_count(db, table=table, ids=ids)
        if stale:
            # A resource was deleted after the selection but before the lock
            # (or the lock waited for its deletion to commit). Those rows are
            # deleted-resource rows now; select again without them.
            db.rollback()
            outcome.reselected += 1
            emit(f"note: {stale} selected {table} row(s) came to reference a deleted resource before their batch was locked; the batch was re-selected without them (nothing uploaded).")
            if outcome.reselected > LOCK_RETRIES * 10:
                raise ArchiveError(f"the {table} selection keeps changing before it can be locked; giving up with nothing deleted")
            continue
        payload = make_archive(
            environment=environment,
            events=records if table == "events" else (),
            engagements=records if table == "engagements" else (),
            metadata={"table": table, "selection": "age_expired_not_deleted_resource"},
            exclude_columns=exclude,
        )
        key = archive_key(
            prefix=prefix, environment=environment, table=table,
            first_day=min(_utc(record[stamp_name]) for record in records).date(),
            first_id=ids[0], last_id=ids[-1], digest=hashlib.sha256(payload).hexdigest(),
        )
        digest, uploaded = ensure_archived(store, key=key, payload=payload, heartbeat=heartbeat, deadline=STORAGE_CALL_DEADLINE_S,
                                           metadata=object_metadata)
        stale = _deleted_resource_count(db, table=table, ids=ids)
        if stale:
            raise ArchiveIntegrityError(
                f"{stale} {table} row(s) of the batch came to reference a deleted resource while {key} was uploaded; "
                "nothing was registered or deleted. That object is not an archive; reconcile it."
            )
        # Only now: register the object, then delete exactly these ids while
        # they still match, in the one commit that makes the object an archive.
        registered = register_archive(
            db, key=key, environment=environment, table=table, digest=digest, manifest=validate_archive(payload),
            registered_at=datetime.now(timezone.utc),
        )
        result = db.execute(
            model.__table__.delete().where(model.id.in_(ids), model.environment == environment, stamp < cutoff)
        )
        db.commit()
        removed = int(result.rowcount or 0)
        lock_failures = 0
        outcome.archived += len(ids)
        outcome.deleted += removed
        outcome.batches += 1
        outcome.reused += 0 if uploaded else 1
        outcome.registered += 1 if registered else 0
        outcome.keys.append(key)
        emit(
            f"archived {len(ids)} {table} row(s) (ids {ids[0]}-{ids[-1]}) to {key} "
            f"[{'uploaded' if uploaded else 'already present'}, verified sha256 {digest[:16]}, "
            f"{'registered' if registered else 'already registered'}]; deleted {removed}."
        )
        if removed == 0 or len(rows) < batch_size:
            break
    return outcome


# ── Reconciliation of leftover objects ───────────────────────────────────

RECONCILE_GRACE_HOURS = 24.0


@dataclass
class ReconcileOutcome:
    environment: str
    prefix: str
    listed: int = 0
    registered: int = 0
    too_recent: int = 0
    candidates: int = 0
    deleted: int = 0
    skipped_registered_late: int = 0
    skipped_changed: int = 0
    skipped_absent: int = 0
    retained_foreign: int = 0
    retained_invalid: int = 0
    retained_not_redundant: int = 0
    failed: int = 0
    locked: bool = False
    registered_missing: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)
    # (key, reason) for every candidate that was inspected and NOT deleted.
    retained: list[tuple[str, str]] = field(default_factory=list)
    deleted_keys: list[str] = field(default_factory=list)


def archive_records(payload: bytes) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The manifest and the parsed JSONL records per table of a validated archive."""
    manifest = validate_archive(payload)
    records: dict[str, list[dict[str, Any]]] = {}
    with ZipFile(BytesIO(payload)) as zipped:
        for table, member in MEMBER_NAMES.items():
            records[table] = [json.loads(line) for line in zipped.read(member).decode("utf-8").splitlines()]
    return manifest, records


def _canonical(record: Mapping[str, Any]) -> str:
    """The one JSON rendering both sides are compared in: the same encoder
    the archive member was written with (sorted keys, UTC ISO timestamps,
    nested JSON as is)."""
    return json.dumps(dict(record), default=_json_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def live_rows_identical_to_archive(
    db: Session, *, table: str, environment: str, records: list[Mapping[str, Any]], omitted_columns: Iterable[str],
) -> tuple[int, int]:
    """(identical, total): how many archived records have a live row that is
    byte-for-byte the same once rendered the way the archive was written —
    every archived column, `id` included, with the manifest's omitted columns
    left out of the comparison (they are absent from the archive and may be
    NULL or anything live), timestamps normalised to UTC and JSON columns
    canonicalised. A record is identical only if the live row has the same
    record id in this environment and renders to the same text.

    On PostgreSQL the live rows are locked `FOR SHARE` for the rest of the
    caller's transaction, so no update or deletion of them can land between
    this comparison and the caller's decision; a concurrent writer waits
    until the caller commits or rolls back.
    """
    model = TABLES[table]
    id_column = getattr(model, RECORD_ID_COLUMN[table])
    omitted = frozenset(omitted_columns)
    wanted = {str(record.get(RECORD_ID_COLUMN[table])) for record in records if record.get(RECORD_ID_COLUMN[table])}
    live: dict[str, Any] = {}
    ids = sorted(wanted)
    for start in range(0, len(ids), 1000):
        statement = select(model).where(model.environment == environment, id_column.in_(ids[start:start + 1000]))
        if _row_locks_available(db):
            statement = statement.with_for_update(read=True)
        for row in db.execute(statement).scalars():
            live[str(getattr(row, RECORD_ID_COLUMN[table]))] = row
    identical = 0
    for record in records:
        row = live.get(str(record.get(RECORD_ID_COLUMN[table])))
        if row is None:
            continue
        rendered = archive_row(row, table, exclude=omitted)
        if _canonical(rendered) == _canonical({k: v for k, v in record.items() if k not in omitted}):
            identical += 1
    return identical, len(records)


def _acquire_job_lock_blocking(db: Session, *, timeout_ms: int) -> bool:
    """Wait up to `timeout_ms` for the job's batch lock (transaction-level);
    False on timeout. On a dialect without advisory locks, always True."""
    if not _row_locks_available(db):
        return True
    db.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'"))
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": JOB_LOCK_KEY})
    except OperationalError as exc:
        db.rollback()
        if _is_lock_timeout(exc):
            return False
        raise
    return True


def reconcile_leftovers(
    db: Session,
    store: ReconcileStore,
    *,
    environment: str,
    prefix: str,
    now: datetime | None = None,
    grace_hours: float = RECONCILE_GRACE_HOURS,
    delete: bool = False,
    reviewed_keys: Iterable[str] | None = None,
    lock_timeout_ms: int = LOCK_TIMEOUT_MS,
    out: Any = None,
    storage: tuple[str, str] | None = None,
) -> ReconcileOutcome:
    """Find — and, within strict limits, remove — objects under this
    environment's prefix that are not registered archives. Report-only by
    default. What `delete=True` may remove automatically is deliberately
    narrow; everything else is retained with a reason, and an operator can
    remove a reviewed key with `reviewed_keys`.

    Rules, in the order they are applied to each listed object:

    1. Scope: only keys under `<prefix>/<environment>/`.
    2. Registered keys are never candidates; a registered key with no object
       is reported (`registered_missing`), never acted on.
    3. Grace: an object younger than `grace_hours` (by `last_modified`) is
       never a candidate. This does not assume an abandoned upload "has
       finished": a late object simply becomes a candidate on a later run.
    4. Coordination: each deletion runs inside a transaction holding the
       archive job's advisory lock (`pg_advisory_xact_lock`, waiting at most
       `lock_timeout_ms`; on timeout the run stops, `locked=True`). A batch
       between its upload and its commit holds that lock, so the reconciler
       waits for it to commit or roll back; while the reconciler holds it no
       batch can start or register. Two reconcilers serialise on it.
    5. Registry re-check under the lock: a key registered since the listing
       is skipped.
    6. Identity, best effort: a HEAD must still show the listed ETag and
       size, otherwise the object is skipped as changed. This is NOT a
       compare-and-delete — the delete that follows is unconditional (R2
       ignores `If-Match` on DeleteObject) — so an object replaced between
       this HEAD and the DELETE is deleted. Who could replace it: the archive
       job never overwrites (every PUT is `If-None-Match: *`, refused while
       the key exists, and the job holds this same lock), so only a writer
       that bypasses both — a third-party tool with the bucket token — can.
       That is the trust boundary; it is not enforced by this code.
    7. Ownership: the HEAD's `registry-id` metadata must equal this
       database's `registry_identity`; anything else (no marker, another
       deployment's marker) is retained as foreign. A bucket shared by
       mistake never loses another registry's objects to this one.
    8. Automatic deletion (`delete=True`) only when the object is **provably
       redundant**: it downloads and validates as an archive of this
       environment and every one of its records has a live row (same record
       id, same environment) that renders to the identical archived record —
       every column including `id`, omitted columns excluded, timestamps and
       JSON canonicalised (`live_rows_identical_to_archive`); those live rows
       are locked `FOR SHARE` until the object is deleted, so they can be
       neither changed nor removed in between. Deleting the object then
       loses nothing — even if the registry were lost and restored, the data
       is live and identical. Anything else (a record missing, changed, or of
       another environment; an object that does not validate) is retained
       with its reason for the operator.
    9. Operator deletion (`reviewed_keys`): the listed keys only, after rules
       1–7 (registered and foreign objects are still refused, the grace
       period still applies), without rule 8. The operator has reviewed the
       retained reason and decided.

    Every key is independent: a failure is counted and the run continues; a
    re-run is idempotent; an interrupted run is completed by the next one.
    Emits keys, counts and reasons only.
    """
    _check_environment(environment)
    if not prefix or not _PREFIX_RE.match(prefix):
        raise ValueError("a valid key prefix is required")
    if grace_hours < 0:
        raise ValueError("grace_hours must not be negative")
    reviewed = None if reviewed_keys is None else {key for key in reviewed_keys if key}
    emit = out or (lambda line: logger.info("%s", line))
    moment = now or datetime.now(timezone.utc)
    scope = f"{prefix}/{environment}/"
    outcome = ReconcileOutcome(environment, prefix)
    require_archive_registry(db)
    # The reconciliation never initialises or binds the identity (that is the
    # archive job's and the create script's doing). A report only looks it
    # up and warns; a deleting run refuses when it is absent, unbound or
    # bound to other storage than this run is configured for.
    identity, bound = lookup_registry_identity(db)
    db.rollback()
    deleting = delete or reviewed is not None
    if identity is None:
        if deleting:
            raise ArchiveConfigError(
                "the registry identity is not initialised, so ownership of objects cannot be established; "
                "run the archive job (or python -m scripts.create_product_analytics_archive_tables) first"
            )
        emit("registry identity not initialised: ownership of objects cannot be established; a deleting run would refuse.")
    elif storage is not None and bound is None:
        if deleting:
            raise ArchiveConfigError(
                "the registry identity is not yet bound to storage (the archive job binds it on its first live run); "
                "a deleting reconciliation refuses until then"
            )
        emit("registry identity not yet bound to storage: a deleting run would refuse until the archive job has run.")
    elif storage is not None and bound is not None and bound != tuple(storage):
        if deleting:
            check_registry_storage_binding(bound, storage)
        emit("WARNING: the registry identity is bound to a different bucket/prefix than this run is configured for; "
             "this report describes storage this database does not own (a cloned or restored database?). A deleting run would refuse.")
    if reviewed is not None:
        outside = sorted(key for key in reviewed if not key.startswith(scope))
        if outside:
            raise ValueError(f"reviewed keys must lie under {scope}: {', '.join(outside)}")
    listed = store.list(scope)
    outcome.listed = len(listed)
    known = registered_keys(db, [item.key for item in listed])
    db.rollback()
    listed_keys = {item.key for item in listed}
    for row in db.execute(
        select(HubProductAnalyticsArchiveV2.key).where(
            HubProductAnalyticsArchiveV2.environment == environment, HubProductAnalyticsArchiveV2.key.like(scope + "%")
        )
    ).scalars():
        if row not in listed_keys:
            outcome.registered_missing.append(row)
    db.rollback()
    for item in outcome.registered_missing:
        emit(f"ALERT: registered archive {item} was not in the listing (missing object or listing lag); never deleted, investigate.")
    candidates: list[ObjectInfo] = []
    for item in listed:
        if item.key in known:
            outcome.registered += 1
            continue
        age_hours = (moment - _utc(item.last_modified)).total_seconds() / 3600
        if age_hours < grace_hours:
            outcome.too_recent += 1
            emit(f"recent unregistered object (age {age_hours:.1f} h < grace {grace_hours:g} h), left alone: {item.key}")
            continue
        candidates.append(item)
        outcome.leftovers.append(item.key)
        emit(f"leftover (unregistered, age {age_hours:.1f} h): {item.key}")
    outcome.candidates = len(candidates)
    if not delete and reviewed is None:
        return outcome
    if reviewed is not None:
        candidate_keys = {item.key for item in candidates}
        for key in sorted(reviewed - candidate_keys):
            emit(f"reviewed key not deleted: not a leftover candidate (registered, too recent, or not listed): {key}")
        candidates = [item for item in candidates if item.key in reviewed]

    def retain(key: str, counter: str, reason: str) -> None:
        setattr(outcome, counter, getattr(outcome, counter) + 1)
        outcome.retained.append((key, reason))
        emit(f"retained ({reason}): {key}")

    for item in candidates:
        try:
            if not _acquire_job_lock_blocking(db, timeout_ms=lock_timeout_ms):
                outcome.locked = True
                emit("an archive run holds the job lock; deletion stopped for this run, nothing further touched.")
                break
            if registered_keys(db, [item.key]):
                outcome.skipped_registered_late += 1
                emit(f"skipped: registered since the listing: {item.key}")
                db.rollback()
                continue
            current = store.stat(item.key)
            if current is None:
                outcome.skipped_absent += 1
                db.rollback()
                continue
            if current.etag != item.etag or current.size != item.size:
                outcome.skipped_changed += 1
                emit(f"skipped: object changed since the listing: {item.key}")
                db.rollback()
                continue
            marker = (current.metadata or {}).get(REGISTRY_ID_METADATA)
            if marker != identity:
                retain(item.key, "retained_foreign", "not written by this registry database (no or different registry-id metadata)")
                db.rollback()
                continue
            if reviewed is None:
                try:
                    payload = call_with_deadline(lambda: store.get(item.key), STORAGE_CALL_DEADLINE_S, what="readback (GET)")
                    manifest, records = archive_records(payload)
                except ArchiveIntegrityError as exc:
                    retain(item.key, "retained_invalid", f"does not validate as an archive: {exc}")
                    db.rollback()
                    continue
                if manifest.get("environment") != environment:
                    retain(item.key, "retained_foreign", "archive of another environment")
                    db.rollback()
                    continue
                omitted = manifest.get("omitted_columns") or []
                identical = total = 0
                for table, table_records in records.items():
                    same, count = live_rows_identical_to_archive(db, table=table, environment=environment, records=table_records, omitted_columns=omitted)
                    identical += same
                    total += count
                if total == 0 or identical != total:
                    retain(item.key, "retained_not_redundant",
                           f"{total - identical} of {total} record(s) have no identical live row (missing, changed, or another environment: "
                           "a cleanup-deleted leftover, a registration that is missing, or data that differs); operator review, then --delete-reviewed")
                    db.rollback()
                    continue
            store.delete(item.key)
            db.rollback()  # releases the job lock; nothing was written to the database
            outcome.deleted += 1
            outcome.deleted_keys.append(item.key)
            emit(f"deleted {'reviewed' if reviewed is not None else 'redundant'} leftover: {item.key}")
        except Exception as exc:  # noqa: BLE001 - one key must not stop the others; reported by type
            db.rollback()
            outcome.failed += 1
            emit(f"failed on {item.key}: {type(exc).__name__}")
    return outcome


# ── Job lock ─────────────────────────────────────────────────────────────


def try_acquire_batch_lock(db: Session) -> bool:
    """Transaction-level PostgreSQL advisory lock, taken inside the batch
    transaction and released with it; always granted on other dialects.

    Transaction-level on purpose: the managed database's pooled endpoint is
    a transaction-pooling proxy, where a SESSION-level advisory lock (and any
    other session state: `SET`, temp tables, prepared statements) belongs to
    whichever server connection the proxy happened to hand out and is not
    reliable across transactions. Everything this job does is scoped to one
    transaction (`SET LOCAL`, this lock, row locks), so it is correct on both
    the pooled and the direct endpoint.
    """
    if not _row_locks_available(db):
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": JOB_LOCK_KEY}).scalar())
