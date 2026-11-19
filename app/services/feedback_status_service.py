"""Resolved / Unresolved status of Hub and Bot feedback.

Every feedback starts Unresolved. The status columns are additive and DEFERRED, so a database that has not run
scripts/add_feedback_status_columns.py yet keeps reading and recording feedback exactly as before; until then
status is reported as unavailable and cannot be changed. Who may change a status is decided by the callers with
the same rules as reading that feedback; this module only stores the value and who set it.
"""
import time
from datetime import datetime, timezone

from sqlalchemy import inspect

STATUSES = ("unresolved", "resolved")
REQUIRED_COLUMNS = frozenset({"status", "status_updated_at", "status_updated_by_user_id"})
_OK_SECONDS = 60.0
_ready: dict[tuple[int, str], float] = {}


class FeedbackStatusUnavailable(Exception):
    """The status columns have not been added to this database yet."""


def status_ready(db, table: str) -> bool:
    """True once the status columns exist. Only a positive answer is remembered, briefly, per database engine."""
    key = (id(db.get_bind()), table)
    checked = _ready.get(key)
    if checked is not None and time.monotonic() - checked < _OK_SECONDS:
        return True
    columns = {column["name"] for column in inspect(db.get_bind()).get_columns(table)}
    if not REQUIRED_COLUMNS.issubset(columns):
        return False
    _ready[key] = time.monotonic()
    return True


def status_fields(row, ready: bool) -> dict:
    if not ready:
        return {"status": "unresolved", "status_updated_at": None}
    changed = row.status_updated_at
    return {"status": row.status or "unresolved", "status_updated_at": changed.isoformat() if changed else None}


def set_status(db, row, status: str, *, user_id: int, ready: bool) -> None:
    if not ready:
        raise FeedbackStatusUnavailable()
    if status not in STATUSES:
        raise ValueError("Unknown feedback status")
    if row.status == status:
        return
    row.status = status
    row.status_updated_at = datetime.now(timezone.utc)
    row.status_updated_by_user_id = user_id
    db.commit()
