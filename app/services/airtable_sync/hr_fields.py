"""HR fields: the three-way merge per field (v1 plan §5.3.6, M1–M3), Work
Email changes for signed-in people (E1, v1 §5.3.7) and the headshot's
comparison (plan v2 §7). Pure: no I/O, no writes; `users_step.UserWriter`
applies what `plan_fields` decides.

Which fields, and how (v1 §6.1's draft, confirmed by the owner 2026-10-23,
E-1): `HR_FIELDS`. A field the run could not read (setting blank, or not in
the Airtable table) is not merged at all, and its base key is left alone.
`is_fellow` is DERIVED: true when Employment Type includes "Fellow" (owner,
2026-10-23; every record holds one value today).

Values are compared in a canonical, JSON-able form — the form the base
stores:
  text / email   trimmed (emails lower-cased); blank → None
  checkbox       true / false (absent = false)
  date           "YYYY-MM-DD";  datetime "YYYY-MM-DDTHH:MM:SSZ" (UTC)
  multi          the sorted set of values (compared as sets; the column is
                 written ", "-joined in Airtable's order)
  headshot       the attachment id (the first attachment); the Hub side is
                 parsed from the stored R2 URL (r2.R2Config.attachment_id_of)

Per field, A = Airtable, B = the base (missing key = not agreed yet), H = Hub:
  first link (no B)   A = H → B := A;  A ≠ H → `field_conflict` (M2)
  A = B               H = B → nothing;  H ≠ B → keep the Hub (`hub_differs`)
  A ≠ B, H = B        update the Hub (`field_updated`), B := A
  A ≠ B, H = A        B := A, no item (both changed the same way)
  A ≠ B, H ≠ B, H ≠ A `field_conflict` (Accept Airtable / Keep current; M3:
                      both move B to A)
A blank Airtable Work Email is never merged (`no_work_email` holds the
person instead). Work Email of a person with `has_signed_in` (E1): every case that would
write a new address (update, conflict, first-link difference) becomes ONE
`work_email_change_signed_in` instead. A new Work or Alias Email that another
`users` row already holds (either column, deleted rows too: the unique
indexes cover them) → `email_taken` with `field` set, instead of anything
that would write it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from app.services.airtable_sync import normalize

if TYPE_CHECKING:
    from app.db_v2.models.user import UserV2
    from app.services.airtable_sync.r2 import R2Config


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()  # the run did not read this field
FELLOW = "fellow"
EMAIL_FIELDS = ("work_email", "alias_email")


@dataclass(frozen=True)
class HrField:
    """`name` is both the `users` attribute and the base key; `source` the
    snapshot field it is read from (default: `name`)."""

    name: str
    kind: str  # text / email / checkbox / date / datetime / multi / fellow / headshot
    source: str | None = None

    @property
    def read_from(self) -> str:
        return self.source or self.name


HR_FIELDS: tuple[HrField, ...] = (
    HrField("name", "text"),
    HrField("work_email", "email"),
    HrField("alias_email", "email"),
    HrField("first_name", "text"),
    HrField("last_name", "text"),
    HrField("employment_type", "multi"),
    HrField("is_fellow", "fellow", source="employment_type"),
    HrField("for_website", "text"),
    HrField("status", "text"),
    HrField("department", "text"),
    HrField("program", "text"),
    HrField("start_date", "date"),
    HrField("personal_email", "email"),
    HrField("position", "text"),
    HrField("dob", "date"),
    HrField("birthday_shoutout_opt_out", "checkbox"),
    HrField("post_on_website", "checkbox"),
    HrField("headshot", "headshot"),
    HrField("office_location", "text"),
    HrField("home_address", "text"),
    HrField("bio", "text"),
    HrField("scope_of_work", "text"),
    HrField("new_headshot_bio_to_website", "text"),
    HrField("end_date", "date"),
    HrField("manager_tech_stack_selections", "multi"),
    HrField("add_to_website_date", "datetime"),
)
BY_NAME = {f.name: f for f in HR_FIELDS}


# ---------------------------------------------------------
# Canonical values
# ---------------------------------------------------------


def _set(values: Any) -> list[str]:
    return sorted(set(normalize.multi(values)))


def airtable_value(f: HrField, fields: dict) -> Any:
    """A's canonical value, or MISSING when the run didn't read the field.
    The reader already normalized text, emails, dates and datetimes."""
    if f.read_from not in fields:
        return MISSING
    raw = fields[f.read_from]
    if f.kind == "multi":
        return _set(raw)
    if f.kind == "fellow":
        return any(v.casefold() == FELLOW for v in normalize.multi(raw))
    if f.kind == "headshot":
        return raw[0]["id"] if raw else None
    if f.kind == "checkbox":
        return bool(raw)
    return raw


def hub_value(f: HrField, row: UserV2, r2: R2Config | None) -> Any:
    value = getattr(row, f.name)
    if f.kind == "email":
        return normalize.email(value)
    if f.kind in ("checkbox", "fellow"):
        return bool(value)
    if f.kind == "date":
        return value.isoformat() if isinstance(value, date) and not isinstance(value, datetime) else normalize.iso_date(value)
    if f.kind == "datetime":
        return normalize.iso_datetime(value)
    if f.kind == "multi":
        return _set(value.split(",")) if value else []
    if f.kind == "headshot":
        return r2.attachment_id_of(value) if r2 is not None else normalize.text(value)
    return normalize.text(value)


def column_value(f: HrField, fields: dict) -> Any:
    """What to store in the `users` column for Airtable's value (not for the
    headshot, which the writer uploads). Raises ValueError for a date the
    reader could not parse (`valid` checks first)."""
    a = airtable_value(f, fields)
    if f.kind == "multi":
        return normalize.join_multi(dict.fromkeys(normalize.multi(fields[f.read_from])))
    if f.kind == "date":
        return date.fromisoformat(a) if a else None
    if f.kind == "datetime":
        return datetime.fromisoformat(a.replace("Z", "+00:00")) if a else None
    return a


def valid(f: HrField, a: Any) -> bool:
    if a is None or f.kind not in ("date", "datetime"):
        return True
    try:
        if f.kind == "date":
            date.fromisoformat(a)
        else:
            datetime.fromisoformat(a.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------
# The plan for one person
# ---------------------------------------------------------


@dataclass
class Change:
    field: str
    base: Any  # None on a first link
    airtable: Any
    hub: Any
    first_link: bool = False

    def describe(self) -> dict:
        return {"field": self.field, "base": self.base, "airtable": self.airtable, "hub": self.hub, "first_link": self.first_link}


@dataclass
class Plan:
    """What the merge found for one person. `base` holds every agreement the
    plan made by itself (equal values; both sides changed the same way); the
    writer adds each change it applies."""

    base: dict
    updates: list[Change] = field(default_factory=list)
    conflicts: list[Change] = field(default_factory=list)
    hub_differs: list[Change] = field(default_factory=list)
    email_change: Change | None = None  # E1
    alias_offer: bool = False  # E1's "Apply + keep the old address as Alias Email"
    taken: list[tuple[Change, dict]] = field(default_factory=list)  # (change, the other row)
    invalid: list[Change] = field(default_factory=list)
    headshot_skipped: bool = False  # read, but R2 isn't configured


EmailOwner = Callable[[str], "dict | None"]


def plan_fields(
    row: UserV2 | None,
    base: dict,
    fields: dict,
    *,
    signed_in: bool,
    r2: R2Config | None,
    email_owner: EmailOwner,
) -> Plan:
    """The three-way merge for every HR field the run read. `row` None = the
    person is being created: every field is an update. `email_owner(email)`
    describes ANOTHER row holding that address, or None."""
    plan = Plan(base=dict(base))
    for f in HR_FIELDS:
        a = airtable_value(f, fields)
        if a is MISSING:
            continue
        if f.kind == "headshot" and r2 is None:
            plan.headshot_skipped = True
            continue
        if f.name == "work_email" and a is None:
            # A blank Work Email is `no_work_email` (it holds the person);
            # the merge never clears the address someone signs in with.
            continue
        if row is None:
            change = Change(f.name, None, a, None, first_link=True)
            if valid(f, a):
                plan.updates.append(change)
            else:
                plan.invalid.append(change)
            continue

        h = hub_value(f, row, r2)
        first = f.name not in base
        b = base.get(f.name)
        change = Change(f.name, None if first else b, a, h, first_link=first)
        if first:
            if a == h:
                plan.base[f.name] = a
                continue
            outcome = "conflict"
        elif a == b:
            if h != b:
                plan.hub_differs.append(change)
            continue
        elif h == b:
            outcome = "update"
        elif h == a:
            plan.base[f.name] = a
            continue
        else:
            outcome = "conflict"

        if not valid(f, a):
            plan.invalid.append(change)
            continue
        if f.name in EMAIL_FIELDS and a is not None:
            other = email_owner(a)
            if other is not None:
                plan.taken.append((change, other))
                continue
        if f.name == "work_email" and signed_in:
            plan.email_change = change
            continue
        (plan.updates if outcome == "update" else plan.conflicts).append(change)

    if plan.email_change is not None:
        alias = BY_NAME["alias_email"]
        a_alias = airtable_value(alias, fields)
        # Offered only when the Hub has no alias and Airtable none either, so
        # the alias field itself isn't being changed by this merge (the base
        # then records Airtable's empty alias, v1 §5.3.7).
        plan.alias_offer = (
            row is not None
            and hub_value(alias, row, r2) is None
            and a_alias in (None, MISSING)
            and plan.email_change.hub is not None
        )
    return plan
