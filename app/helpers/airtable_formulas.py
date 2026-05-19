"""
Airtable ``filterByFormula`` builder helpers.

Each helper returns a snippet (or ``None`` when the input is empty / not
provided).  The :func:`combine` helpers wrap clauses with ``AND``/``OR``
or return the single clause / ``None`` as appropriate.
"""

from __future__ import annotations

from typing import Iterable


def escape(value: str) -> str:
    """Escape backslashes and single quotes for Airtable formula literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def field_ref(field: str) -> str:
    """Wrap a field name in ``{...}`` for use in Airtable formulas."""
    return "{" + field + "}"


def AND(*clauses: str | None) -> str | None:
    parts = [c for c in clauses if c]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"AND({', '.join(parts)})"


def OR(*clauses: str | None) -> str | None:
    parts = [c for c in clauses if c]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"OR({', '.join(parts)})"


# ── basic predicates ───────────────────────────────────────────────────
def eq_str(field: str, value: str) -> str:
    return f"{field_ref(field)} = '{escape(value)}'"


def neq_str(field: str, value: str) -> str:
    return f"{field_ref(field)} != '{escape(value)}'"


def eq_num(field: str, value: float | int) -> str:
    return f"{field_ref(field)} = {value}"


def lt_num(field: str, value: float | int) -> str:
    return f"{field_ref(field)} < {value}"


def gt_num(field: str, value: float | int) -> str:
    return f"{field_ref(field)} > {value}"


def is_checked(field: str) -> str:
    return f"{field_ref(field)} = 1"


def is_unchecked(field: str) -> str:
    return f"NOT({field_ref(field)} = 1)"


def checkbox_clause(field: str, value: bool | None) -> str | None:
    if value is None:
        return None
    return is_checked(field) if value else is_unchecked(field)


def is_empty(field: str) -> str:
    return f"{field_ref(field)} = BLANK()"


def is_not_empty(field: str) -> str:
    return f"NOT({field_ref(field)} = BLANK())"


def empty_clause(field: str, value: bool | None) -> str | None:
    if value is None:
        return None
    return is_empty(field) if value else is_not_empty(field)


def in_str(field: str, values: Iterable[str]) -> str | None:
    """OR of equality clauses for the given values."""
    items = [v for v in values if v is not None]
    if not items:
        return None
    if len(items) == 1:
        return eq_str(field, items[0])
    return OR(*(eq_str(field, v) for v in items))


def not_in_str(field: str, values: Iterable[str]) -> str | None:
    """Field is NOT equal to any of the provided values."""
    items = [v for v in values if v is not None]
    if not items:
        return None
    inner = OR(*(eq_str(field, v) for v in items))
    return f"NOT({inner})" if inner else None


# Multiselect contains any: use ARRAYJOIN with a delimiter unlikely to appear.
_SEP = "\u001f"  # ASCII unit separator


def multiselect_contains_any(field: str, values: Iterable[str]) -> str | None:
    items = [v for v in values if v is not None]
    if not items:
        return None
    joined = f"'{_SEP}' & ARRAYJOIN({field_ref(field)}, '{_SEP}') & '{_SEP}'"
    parts = [
        f"FIND('{_SEP}{escape(v)}{_SEP}', {joined}) > 0" for v in items
    ]
    return OR(*parts)


# ── date predicates ────────────────────────────────────────────────────
def _parse(iso: str) -> str:
    return f"DATETIME_PARSE('{escape(iso)}')"


def eq_date(field: str, iso: str) -> str:
    return f"IS_SAME({field_ref(field)}, {_parse(iso)}, 'day')"


def lt_date(field: str, iso: str) -> str:
    return f"IS_BEFORE({field_ref(field)}, {_parse(iso)})"


def gt_date(field: str, iso: str) -> str:
    return f"IS_AFTER({field_ref(field)}, {_parse(iso)})"


def year_clauses(
    *, eq: int | None, lt: int | None, gt: int | None, field: str
) -> str | None:
    """OR-combined integer comparisons for a numeric year-like field."""
    parts = []
    if eq is not None:
        parts.append(eq_num(field, int(eq)))
    if lt is not None:
        parts.append(lt_num(field, int(lt)))
    if gt is not None:
        parts.append(gt_num(field, int(gt)))
    return OR(*parts)


def date_clauses(
    *, eq: str | None, lt: str | None, gt: str | None, field: str
) -> str | None:
    """OR-combined date comparisons for a date field."""
    parts = []
    if eq is not None:
        parts.append(eq_date(field, eq))
    if lt is not None:
        parts.append(lt_date(field, lt))
    if gt is not None:
        parts.append(gt_date(field, gt))
    return OR(*parts)
