"""
Airtable ``filterByFormula`` builder helpers.

Each helper returns a snippet (or ``None`` when the input is empty / not
provided).  The :func:`combine` helpers wrap clauses with ``AND``/``OR``
or return the single clause / ``None`` as appropriate.
"""

from __future__ import annotations

from typing import Any, Iterable


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


def contains_str(field: str, value: str) -> str:
    """Case-sensitive substring match: the field's text contains *value*."""
    return f"FIND('{escape(value)}', {field_ref(field)} & '') > 0"


def contains_any_str(field: str, values: Iterable[str]) -> str | None:
    """OR of substring-contains predicates for each value."""
    items = [v for v in values if v is not None]
    if not items:
        return None
    if len(items) == 1:
        return contains_str(field, items[0])
    return OR(*(contains_str(field, v) for v in items))


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


# ══════════════════════════════════════════════════════════════════════
#   Dashboard Airtable widget
#
#   Server-side replacement for the formula the frontend used to build in
#   AirtableWidget.tsx. That version is NOT ported verbatim — it had three
#   injection defects, documented at each fix below. Everything here goes
#   through `escape()` and single-quoted literals, matching the rest of this
#   module; the frontend's double-quoted dialect is deliberately not
#   reproduced.
# ══════════════════════════════════════════════════════════════════════


class FormulaFieldError(ValueError):
    """A field name or value cannot be safely embedded in a formula."""


# Airtable field references are `{Name}` with no escape mechanism, so a name
# containing a brace terminates the reference early and everything after it
# is parsed as formula code. A backslash can escape the closing quote of an
# adjacent literal. There is no safe way to embed any of these — reject.
_FIELD_NAME_FORBIDDEN = ("{", "}", "\\")


def validate_field_name(field: str) -> str:
    """Return `field` stripped, or raise if it can't be safely embedded.

    The frontend interpolated field names raw (`{${f.field}}`), so a column
    called `X} , TRUE()) , OR((TRUE` would have escaped the field reference
    and rewritten the surrounding formula — including neutralising the
    personalize filter. Field names reach the server from stored widget
    config, which is why this is enforced here rather than trusted.
    """
    name = (field or "").strip()
    if not name:
        raise FormulaFieldError("Field name is empty.")
    for bad in _FIELD_NAME_FORBIDDEN:
        if bad in name:
            raise FormulaFieldError(
                f"Field name may not contain {bad!r}: {field!r}"
            )
    return name


def _as_number(value: Any) -> float:
    """Coerce a comparison operand to a number, or raise.

    The frontend emitted `{Field} > ${value}` with the value interpolated
    RAW and unquoted — no escaping at all — so any text landed directly in
    the formula. Numeric comparisons must therefore be validated, not
    escaped: there is nothing to escape in a bare number.
    """
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise FormulaFieldError(
            f"Numeric comparison needs a number, got {value!r}"
        ) from None


def widget_filter_clause(field: str, operator: str, value: Any = "") -> str | None:
    """One clause of the widget's Filters section.

    Mirrors the operator set the Property Panel offers. Returns None for an
    unknown operator rather than raising, so one stale filter row cannot
    take down the whole widget — but note that a dropped clause makes the
    result set WIDER, so callers that treat filters as a security boundary
    must not use this (personalize is applied separately and never dropped).
    """
    name = validate_field_name(field)
    text = "" if value is None else str(value)

    if operator == "eq":
        return eq_str(name, text)
    if operator == "neq":
        return neq_str(name, text)
    if operator == "contains":
        return contains_str(name, text)
    if operator == "not_contains":
        return f"NOT({contains_str(name, text)})"
    if operator == "is_empty":
        # The frontend used `{Field} = ""`, which only matches empty TEXT.
        # BLANK() is the canonical empty test and also catches empty
        # numeric/date/lookup cells — a deliberate (narrow) behaviour change.
        return is_empty(name)
    if operator == "is_not_empty":
        return is_not_empty(name)
    if operator == "gt":
        return gt_num(name, _as_number(text))
    if operator == "lt":
        return lt_num(name, _as_number(text))
    return None


def widget_filters_clause(filters: Iterable[dict[str, Any]] | None) -> str | None:
    """AND-combine the widget's stored filter rows. Rows with no field are
    skipped, matching the frontend's `filter((f) => f.field.trim())`."""
    clauses: list[str] = []
    for row in filters or []:
        if not isinstance(row, dict):
            continue
        if not str(row.get("field") or "").strip():
            continue
        clause = widget_filter_clause(
            row.get("field", ""), str(row.get("operator") or ""), row.get("value", "")
        )
        if clause:
            clauses.append(clause)
    return AND(*clauses)


# Separators an admin's "who owns this row" column might realistically use
# between addresses. Normalised to a single comma before matching so every
# entry ends up delimiter-bounded regardless of the source shape.
#
# ONLY plain printable literals belong here. Airtable's formula language has
# no CHAR() function — an earlier version of this list used CHAR(10)/CHAR(13)
# for newlines and every query failed with
# `INVALID_FILTER_BY_FORMULA: Unknown function names: CHAR`. Nor is it
# established that a raw control character survives the trip inside a quoted
# literal. So newline-separated lists are NOT normalised: a column holding
# one address per LINE will match nothing (the Property Panel's
# "matched none of your N rows" warning surfaces that at configuration
# time). Add newline support only after verifying the syntax against a real
# base — do not infer it.
_PERSONALIZE_SEPARATOR_LITERALS = (";", "|")


def personalize_clause(column: str, email: str) -> str | None:
    """Match `email` as a WHOLE entry of `column`, case-insensitively.

    The shipped client-side version was
    `FIND(LOWER("<email>"), LOWER({Col})) > 0` — an UNANCHORED substring
    match. Because one address can be a suffix of another, that silently
    leaked rows across users: FIND("amy@renphil.org", "tamy@renphil.org")
    returns 2, so amy@ saw every row belonging to tamy@. Over the financial
    and medical data this widget carries, that is the failure that matters
    most, so the match is delimiter-bounded here instead.

    The column may hold a single address, a delimited list, or an Airtable
    array (multi-select / linked records / collaborators). `{Col} & ''`
    coerces all three to text — arrays render comma-separated — then
    separators are normalised to commas, spaces removed, and BOTH sides
    padded with commas so a match can only ever be a complete entry.

    Supported separators: comma, semicolon, pipe (and any surrounding
    spaces). NEWLINE-separated lists are NOT supported — see
    `_PERSONALIZE_SEPARATOR_LITERALS` for why.

    Returns None when either input is empty: the caller must treat that as
    "return no rows", never as "no filter".
    """
    name = validate_field_name(column)
    address = (email or "").strip().lower()
    if not address:
        return None

    # Coerce to text (handles scalars and arrays alike), lowercase, then
    # collapse every plausible separator to a comma and drop spaces, so
    # "A@x.com; B@x.com" and ["A@x.com", "B@x.com"] normalise identically.
    haystack = f"LOWER({field_ref(name)} & '')"
    for separator in _PERSONALIZE_SEPARATOR_LITERALS:
        haystack = f"SUBSTITUTE({haystack}, '{escape(separator)}', ',')"
    haystack = f"SUBSTITUTE({haystack}, ' ', '')"

    needle = f"',{escape(address)},'"
    return f"FIND({needle}, ',' & {haystack} & ',') > 0"


def widget_formula(
    *,
    filters: Iterable[dict[str, Any]] | None = None,
    personalize_enabled: bool = False,
    personalize_column: str | None = None,
    email: str | None = None,
) -> tuple[str | None, bool]:
    """Build the widget's complete `filterByFormula`.

    Returns `(formula, allowed)`. **`allowed` is False when personalization
    is on but could not be applied** — no email, no column, or an unusable
    column name. The caller MUST return zero rows in that case: falling back
    to `formula` alone would serve the whole table to someone who should
    only ever see their own rows. This is why the failure is a separate flag
    and not an exception or a None formula, both of which are easy to
    mistake for "no filtering needed".
    """
    filter_clause = widget_filters_clause(filters)

    if not personalize_enabled:
        return filter_clause, True

    if not personalize_column or not (email or "").strip():
        return None, False

    try:
        personalize = personalize_clause(personalize_column, email or "")
    except FormulaFieldError:
        return None, False

    if not personalize:
        return None, False

    return AND(filter_clause, personalize), True


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
