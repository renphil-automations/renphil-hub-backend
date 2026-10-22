"""Read-only Airtable access for the sync.

The sync is one-way (Airtable → Hub): nothing in this module, or anywhere in
the sync, may write to Airtable. Reads go through `pyairtable` with the
backend's `AIRTABLE_API_KEY`; the key is never logged.

Two readers:
  - `designation_choices()`: the mapping endpoint's live Designation list.
  - `read_snapshot()`: a run's read of all four tables (plan v2 §5, R2; v1
    plan §4). Every field is read BY NAME from settings; the schema is checked
    first, so a renamed Airtable field fails the run loudly, naming it, before
    a single record is fetched or anything is written.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests
from pyairtable import Api
from pyairtable import retry_strategy as _pyairtable_retry_strategy

from app.config import get_settings
from app.services.airtable_sync import normalize

logger = logging.getLogger(__name__)

# Same widened forcelist as the widget path (airtable_service.py
# _WIDGET_RETRY_STRATEGY): pyairtable's default retries only 429, so one
# transient 5xx would fail a whole run.
_SYNC_RETRY_STRATEGY = _pyairtable_retry_strategy(status_forcelist=(429, 500, 502, 503, 504))
# (connect, read) seconds per request, so a stalled Airtable can't hang the
# run's thread forever. A page of 100 records is well under this.
_REQUEST_TIMEOUT = (10, 60)


class AirtableSyncConfigError(Exception):
    """A setting the sync needs is missing. The message names it."""


class AirtableReadError(Exception):
    """Airtable could not be read as configured: a required field is missing
    from its table, or the API answered with an error. The message is short,
    names the field or HTTP status, and never contains the access token."""


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


# ---------------------------------------------------------
# The run's snapshot: what is read, by which setting
# ---------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One Airtable field the sync reads. `name` is the logical name the
    steps use; `setting` the config key holding the Airtable field name;
    `kind` picks the normalizer (normalize.NORMALIZERS)."""

    name: str
    setting: str
    kind: str = "text"


@dataclass(frozen=True)
class TableSpec:
    """One table. `required` fields must be configured AND present in the
    Airtable schema, or the run fails before any write. `optional` fields
    (the HR fields, until phase 6 confirms them with the colleague, E-1) are
    read when present; the ones missing are reported in the run summary."""

    entity: str  # the snapshot attribute: clusters / programs / users / program_roles
    label: str  # for messages: the table's Airtable name
    table_setting: str
    required: tuple[FieldSpec, ...]
    optional: tuple[FieldSpec, ...] = ()


# Every table has a `record_id` field: the copied "Record ID"/"Record Id"
# COLUMN, which is the sync's key (v1 K1). Never the API id, which differs
# between synced copies of a table.
CLUSTERS = TableSpec(
    entity="clusters",
    label="Cluster",
    table_setting="AIRTABLE_SYNC_CLUSTERS_TABLE",
    required=(
        FieldSpec("record_id", "AIRTABLE_SYNC_CLUSTERS_RECORD_ID_FIELD"),
        FieldSpec("name", "AIRTABLE_SYNC_CLUSTERS_NAME_FIELD"),
    ),
)
PROGRAMS = TableSpec(
    entity="programs",
    label="Public incl. Finance Dashboard",
    table_setting="AIRTABLE_SYNC_PROGRAMS_TABLE",
    required=(
        FieldSpec("record_id", "AIRTABLE_SYNC_PROGRAMS_RECORD_ID_FIELD"),
        FieldSpec("name", "AIRTABLE_SYNC_PROGRAMS_NAME_FIELD"),
        # Text, comma-separated cluster "Record Id" values: what the sync
        # reads (plan v2 §2). The link is read only for the mismatch log.
        FieldSpec("cluster_record_ids", "AIRTABLE_SYNC_PROGRAMS_CLUSTER_RECORD_ID_FIELD", "id_list"),
        FieldSpec("cluster_links", "AIRTABLE_SYNC_PROGRAMS_CLUSTER_LINK_FIELD", "links"),
    ),
)
USERS = TableSpec(
    entity="users",
    label="Users",
    table_setting="USERS_TABLE",
    required=(
        FieldSpec("record_id", "AIRTABLE_SYNC_USERS_RECORD_ID_FIELD"),
        FieldSpec("name", "USERS_NAME_FIELD"),
        FieldSpec("work_email", "USERS_WORK_EMAIL_FIELD", "email"),
        FieldSpec("alias_email", "USERS_ALIAS_EMAIL_FIELD", "email"),
    ),
    # v1 plan §6.1 (a DRAFT until E-1). Department and Program are HR fields
    # only since plan v2 §3.2.
    optional=(
        FieldSpec("first_name", "USERS_FIRST_NAME_FIELD"),
        FieldSpec("last_name", "USERS_LAST_NAME_FIELD"),
        FieldSpec("employment_type", "USERS_EMPLOYMENT_TYPE_FIELD", "multi"),
        FieldSpec("for_website", "USERS_FOR_WEBSITE_FIELD"),
        FieldSpec("status", "USERS_STATUS_FIELD"),
        FieldSpec("is_fellow", "AIRTABLE_SYNC_USERS_IS_FELLOW_FIELD", "checkbox"),
        FieldSpec("department", "USERS_DEPARTMENT_FIELD"),
        FieldSpec("program", "USERS_PROGRAM_FIELD"),
        FieldSpec("start_date", "USERS_START_DATE_FIELD", "date"),
        FieldSpec("personal_email", "USERS_PERSONAL_EMAIL_FIELD", "email"),
        FieldSpec("position", "USERS_POSITION_FIELD"),
        FieldSpec("dob", "USERS_DOB_FIELD", "date"),
        FieldSpec("birthday_shoutout_opt_out", "AIRTABLE_SYNC_USERS_BIRTHDAY_OPT_OUT_FIELD", "checkbox"),
        FieldSpec("post_on_website", "AIRTABLE_SYNC_USERS_POST_ON_WEBSITE_FIELD", "checkbox"),
        FieldSpec("headshot", "USERS_HEADSHOT_FIELD", "attachments"),
        FieldSpec("office_location", "USERS_OFFICE_LOCATION_FIELD"),
        FieldSpec("home_address", "USERS_HOME_ADDRESS_FIELD"),
        FieldSpec("bio", "USERS_BIO_FIELD"),
        FieldSpec("scope_of_work", "USERS_SCOPE_OF_WORK_FIELD"),
        FieldSpec("new_headshot_bio_to_website", "AIRTABLE_SYNC_USERS_NEW_HEADSHOT_BIO_FIELD"),
        FieldSpec("end_date", "USERS_END_DATE_FIELD", "date"),
        FieldSpec("manager_tech_stack_selections", "USERS_TECH_STACK_SELECTIONS_FIELD", "multi"),
        FieldSpec("add_to_website_date", "AIRTABLE_SYNC_USERS_ADD_TO_WEBSITE_DATE_FIELD", "datetime"),
    ),
)
PROGRAM_ROLES = TableSpec(
    entity="program_roles",
    label="Program Roles",
    table_setting="AIRTABLE_SYNC_PROGRAM_ROLES_TABLE",
    required=(
        # Program Roles has no Record ID column of its own: a row is
        # identified by its (employee, program) pair (plan v2 A1).
        FieldSpec("employee_record_id", "AIRTABLE_SYNC_PROGRAM_ROLES_EMPLOYEE_RECORD_ID_FIELD"),
        FieldSpec("program_record_id", "AIRTABLE_SYNC_PROGRAM_ROLES_PROGRAM_RECORD_ID_FIELD"),
        FieldSpec("designation", "AIRTABLE_SYNC_PROGRAM_ROLES_DESIGNATION_FIELD"),
    ),
)

# In the run order (plan v2 R1).
TABLE_SPECS = (CLUSTERS, PROGRAMS, USERS, PROGRAM_ROLES)


@dataclass(frozen=True)
class ResolvedTable:
    """A TableSpec with its settings looked up: the Airtable table id and,
    per logical field, the Airtable field name."""

    spec: TableSpec
    table_id: str
    required: dict[str, str]  # logical name -> Airtable field name
    optional: dict[str, str]
    unconfigured: tuple[str, ...]  # optional fields whose setting is blank


@dataclass(frozen=True)
class SyncConfig:
    """Everything a run reads from settings, taken ONCE at its start so the
    whole run sees one configuration (`get_settings()` is not cached)."""

    base_id: str
    api_key: str = field(repr=False)
    tables: tuple[ResolvedTable, ...]

    def table(self, entity: str) -> ResolvedTable:
        return next(t for t in self.tables if t.spec.entity == entity)


def _setting(settings: Any, name: str) -> str | None:
    value = getattr(settings, name, None)
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def build_config(settings: Any) -> SyncConfig:
    """Resolve every TableSpec against `settings`. Raises
    AirtableSyncConfigError naming EVERY missing required setting."""
    missing: list[str] = []

    def need(name: str) -> str:
        value = _setting(settings, name)
        if value is None:
            missing.append(name)
        return value or ""

    base_id = need("RENPHIL_HUB_BASE_ID")
    api_key = need("AIRTABLE_API_KEY")
    tables = []
    for spec in TABLE_SPECS:
        table_id = need(spec.table_setting)
        required = {f.name: need(f.setting) for f in spec.required}
        optional = {f.name: v for f in spec.optional if (v := _setting(settings, f.setting))}
        unconfigured = tuple(f.setting for f in spec.optional if f.name not in optional)
        tables.append(ResolvedTable(spec, table_id, required, optional, unconfigured))
    if missing:
        raise AirtableSyncConfigError(f"Missing setting(s): {', '.join(missing)}")
    return SyncConfig(base_id=base_id, api_key=api_key, tables=tuple(tables))


class AirtableSource(Protocol):
    """What a run needs from Airtable. Production uses PyairtableSource;
    tests pass a fake (Airtable is never called from the test suite)."""

    def field_names(self, table_id: str) -> set[str]:
        """The names of every field in the table's schema."""

    def records(self, table_id: str, field_names: list[str]) -> list[dict]:
        """Every record, as pyairtable returns them: `{"id", "fields"}`, with
        only the named fields (Airtable omits empty cells)."""


class PyairtableSource:
    """Read-only: one schema call for the base, then `table.all()` per
    table. Nothing here writes to Airtable."""

    def __init__(self, config: SyncConfig) -> None:
        self._api = Api(config.api_key, timeout=_REQUEST_TIMEOUT, retry_strategy=_SYNC_RETRY_STRATEGY)
        self._base_id = config.base_id
        self._schema = None

    def field_names(self, table_id: str) -> set[str]:
        if self._schema is None:
            self._schema = self._api.base(self._base_id).schema()
        return {f.name for f in self._schema.table(table_id).fields}

    def records(self, table_id: str, field_names: list[str]) -> list[dict]:
        return self._api.table(self._base_id, table_id).all(fields=field_names)


@dataclass(frozen=True)
class SnapshotRecord:
    """One Airtable record. `fields` maps the spec's logical names to
    normalized values; every configured field is present (absent cells read
    as blank), so steps never need `.get` defaults."""

    api_id: str
    fields: dict[str, Any]

    @property
    def record_id(self) -> str | None:
        return self.fields.get("record_id")


@dataclass
class Snapshot:
    """All four tables, fully read before anything is written (v1 plan §4
    "snapshot first", v2 R2: every run reads all four, whatever it writes)."""

    clusters: list[SnapshotRecord]
    programs: list[SnapshotRecord]
    users: list[SnapshotRecord]
    program_roles: list[SnapshotRecord]
    # Optional (HR) fields that could not be read: their setting is blank, or
    # the field is not in the table. "<SETTING> (<reason>)".
    optional_fields_missing: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "clusters": len(self.clusters),
            "programs": len(self.programs),
            "users": len(self.users),
            "program_roles": len(self.program_roles),
            "optional_fields_missing": list(self.optional_fields_missing),
        }


def _scrub(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret else message


def _call(config: SyncConfig, table: ResolvedTable, what: str, fn: Callable[[], Any]) -> Any:
    """Run one Airtable call; turn a failure into a short AirtableReadError.
    The full exception goes to the server log; neither carries the token
    (it travels in a header, and is scrubbed anyway)."""
    try:
        return fn()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        logger.warning(
            "Airtable sync: %s of %s (%s) failed: %s",
            what, table.spec.label, table.table_id, _scrub(str(exc), config.api_key),
        )
        raise AirtableReadError(
            f"Airtable answered HTTP {status} while reading the {what} of the {table.spec.label} table "
            f"({table.table_id})."
        ) from None
    except requests.RequestException as exc:
        logger.warning(
            "Airtable sync: %s of %s (%s) failed: %s",
            what, table.spec.label, table.table_id, _scrub(repr(exc), config.api_key),
        )
        raise AirtableReadError(
            f"Could not reach Airtable ({type(exc).__name__}) while reading the {what} of the "
            f"{table.spec.label} table ({table.table_id})."
        ) from None
    except KeyError:
        # pyairtable's schema lookup raises KeyError for an unknown table id.
        if what != "schema":
            raise
        raise AirtableReadError(
            f"The {table.spec.label} table ({table.table_id}, {table.spec.table_setting}) is not in the "
            f"Airtable base {config.base_id}."
        ) from None


def read_snapshot(source: AirtableSource, config: SyncConfig) -> Snapshot:
    """Read all four tables. First every table's schema is checked: a
    required field missing anywhere fails the whole read (AirtableReadError
    naming every missing field and its setting) before one record is
    fetched. Then every table is read in full."""
    present: dict[str, set[str]] = {}
    problems: list[str] = []
    for table in config.tables:
        names = _call(config, table, "schema", lambda t=table: source.field_names(t.table_id))
        present[table.spec.entity] = names
        for logical, airtable_name in table.required.items():
            if airtable_name not in names:
                setting = next(f.setting for f in table.spec.required if f.name == logical)
                problems.append(f"{airtable_name!r} ({setting}) in {table.spec.label}")
    if problems:
        raise AirtableReadError(
            "Airtable field(s) missing: " + "; ".join(problems) + ". Rename the setting(s) to match Airtable."
        )

    optional_missing: list[str] = []
    tables: dict[str, list[SnapshotRecord]] = {}
    for table in config.tables:
        names = present[table.spec.entity]
        optional_missing.extend(f"{s} (not set)" for s in table.unconfigured)
        readable_optional = {}
        for spec in table.spec.optional:
            airtable_name = table.optional.get(spec.name)
            if airtable_name is None:
                continue
            if airtable_name in names:
                readable_optional[spec.name] = airtable_name
            else:
                optional_missing.append(f"{spec.setting} ({airtable_name!r} not in {table.spec.label})")
        columns = {**table.required, **readable_optional}
        kinds = {f.name: f.kind for f in (*table.spec.required, *table.spec.optional)}
        raw = _call(config, table, "records", lambda t=table, c=columns: source.records(t.table_id, list(c.values())))
        tables[table.spec.entity] = [
            SnapshotRecord(
                api_id=rec["id"],
                fields={
                    logical: normalize.normalize(kinds[logical], (rec.get("fields") or {}).get(airtable_name))
                    for logical, airtable_name in columns.items()
                },
            )
            for rec in raw
        ]
    return Snapshot(optional_fields_missing=optional_missing, **tables)
