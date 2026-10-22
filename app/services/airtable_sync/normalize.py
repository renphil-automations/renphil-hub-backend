"""How the sync normalizes Airtable values before storing or comparing them
(v1 plan §5.3.6 "Normalization before comparing"; plan v2 §5).

Pure functions, no I/O. "Blank" is always None, never "", so a field Airtable
omits (it leaves empty cells out of a record) and a field holding only
whitespace compare equal.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

MULTI_SEPARATOR = ", "


def text(value: Any) -> str | None:
    """Trimmed text, or None when blank. A list (a lookup or a single-value
    multi field) is joined with ", " in Airtable's order."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [p for p in (text(v) for v in value) if p is not None]
        return MULTI_SEPARATOR.join(parts) if parts else None
    stripped = str(value).strip()
    return stripped or None


def email(value: Any) -> str | None:
    """Trimmed and lowercased; None when blank."""
    stripped = text(value)
    return stripped.lower() if stripped else None


def checkbox(value: Any) -> bool:
    """Airtable omits an unticked checkbox from the record, so absent is False."""
    return bool(value)


def iso_date(value: Any) -> str | None:
    """`YYYY-MM-DD`, or None when blank. Accepts Airtable's date strings, a
    full ISO datetime (its date part), or a date/datetime object. Anything
    unparseable is returned trimmed rather than dropped, so a comparison
    still sees the change."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return raw


def iso_datetime(value: Any) -> str | None:
    """UTC `YYYY-MM-DDTHH:MM:SSZ`, or None when blank. A naive value is taken
    as UTC (Airtable's API always sends UTC with a trailing Z)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = text(value)
        if raw is None:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def multi(value: Any) -> list[str]:
    """A multi-select as a list in Airtable's order, blanks dropped. Compare
    two of these with `multi_key`, store them with `join_multi`."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [t for t in (text(v) for v in value) if t is not None]


def multi_key(values: Iterable[str]) -> frozenset[str]:
    """Multi-selects are compared as sets: order and repeats don't count."""
    return frozenset(values)


def join_multi(values: Iterable[str]) -> str | None:
    """How a multi-select is stored in a text column: ", "-joined in
    Airtable's order; None when empty."""
    joined = MULTI_SEPARATOR.join(values)
    return joined or None


def id_list(value: Any) -> list[str]:
    """A text cell holding comma-separated record ids (e.g. Programs
    "Cluster Record Id"): trimmed, blanks and repeats dropped, order kept."""
    raw = text(value)
    if raw is None:
        return []
    seen: dict[str, None] = {}
    for part in raw.split(","):
        part = part.strip()
        if part:
            seen.setdefault(part, None)
    return list(seen)


def links(value: Any) -> list[str]:
    """A linked-record field: the linked records' API ids, in order."""
    if not value:
        return []
    return [str(v) for v in value if v]


def attachments(value: Any) -> list[dict[str, str | None]]:
    """An attachment field as `[{id, filename, url}]`. The URL is Airtable's
    temporary one (it expires within hours); the attachment `id` is what a
    headshot is compared by (v1 plan §6.2)."""
    if not value:
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            out.append({"id": item["id"], "filename": item.get("filename"), "url": item.get("url")})
    return out


# The kinds a field spec may name (airtable_reader.FieldSpec.kind).
NORMALIZERS = {
    "text": text,
    "email": email,
    "checkbox": checkbox,
    "date": iso_date,
    "datetime": iso_datetime,
    "multi": multi,
    "id_list": id_list,
    "links": links,
    "attachments": attachments,
}


def normalize(kind: str, value: Any) -> Any:
    return NORMALIZERS[kind](value)
