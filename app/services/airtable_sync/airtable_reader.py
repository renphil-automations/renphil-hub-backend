"""Read-only Airtable access for the sync.

The sync is one-way (Airtable → Hub): nothing in this module, or anywhere in
the sync, may write to Airtable. Reads go through `pyairtable` with the
backend's `AIRTABLE_API_KEY`; the key is never logged.
"""

from __future__ import annotations

import logging

from pyairtable import Api

from app.config import get_settings

logger = logging.getLogger(__name__)


class AirtableSyncConfigError(Exception):
    """A setting the sync needs is missing. The message names it."""


def _require(name: str) -> str:
    value = getattr(get_settings(), name, None)
    if not value or not str(value).strip():
        raise AirtableSyncConfigError(f"{name} is not set")
    return str(value).strip()


def designation_choices() -> list[str]:
    """The choices of the Program Roles "Designation" single select, read live
    from the table schema, in Airtable's order. Listing the CHOICES rather than
    the values in use means a Designation can be mapped before anyone has it
    (plan v2 §3.4 A2: "Studio Designate" has 0 rows today).

    Raises AirtableSyncConfigError if a setting is missing, LookupError if the
    field is not in the table (renamed in Airtable), and lets pyairtable's
    HTTP errors propagate; the caller turns all of these into a message.
    """
    base_id = _require("RENPHIL_HUB_BASE_ID")
    table_id = _require("AIRTABLE_SYNC_PROGRAM_ROLES_TABLE")
    field_name = _require("AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD")

    api = Api(_require("AIRTABLE_API_KEY"))
    table_schema = api.base(base_id).schema().table(table_id)
    for field in table_schema.fields:
        if field.name == field_name:
            options = getattr(field, "options", None)
            choices = getattr(options, "choices", None) or []
            return [c.name.strip() for c in choices if (c.name or "").strip()]
    raise LookupError(f"Field {field_name!r} is not in the Program Roles table {table_id}")
