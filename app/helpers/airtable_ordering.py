"""
Admin-configured default sort / grouping for the Airtable Table widget
(plan_airtable_advanced_filters_2026-09-26.md, Part B, §11).

The widget stores two optional, unprotected display choices:

    defaultSort:  {"field": str, "direction": "asc" | "desc"} | null
    defaultGroup: {"field": str, "direction": "asc" | "desc"} | null

The backend never groups (group headers are the frontend's job). It only
ORDERS rows, by the group field first and the sort field second, so that:

* the paginated `/rows` view (cached path) is served in that order, sorted in
  Python after personalization and before slicing;
* the live paths (`/rows` uncached/oversized, the editor preview) hand the
  same order to Airtable's native `sort` option.

`/rows/full` stays unsorted: the client sorts and groups the whole table.

A default whose field isn't a displayed column (or whose shape is malformed)
is IGNORED, never an error (Part B decision 3): a stale default must not take
the widget down. That's why nothing here raises `FormulaFieldError`, which
`app/main.py` turns into a 400.

The comparator mirrors the frontend's `compareCellValues` /  `formatCell`
(`AirtableWidget.tsx`): numeric when both sides convert with JS `Number()`
(and neither is the empty string), otherwise a string compare of the
formatted cells; null/missing sorts after every value (so first when
descending, since the frontend multiplies the whole comparison by -1). The
string compare approximates `localeCompare(…, undefined, {numeric: true})`
(ICU root collation with numeric ordering) and is NOT byte-exact with it —
accepted by the owner (Part B decision 6).
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from decimal import Decimal
from functools import cmp_to_key
from typing import Any, Iterable

logger = logging.getLogger(__name__)

ORDER_DIRECTIONS = ("asc", "desc")


# ── Stored-config parsing & validation ─────────────────────────────────────


def parse_default_order(raw: Any) -> dict[str, str] | None:
    """One stored `defaultSort`/`defaultGroup` value → `{field, direction}`,
    or `None` when it's unset or malformed (ignored, not an error)."""
    if not isinstance(raw, dict):
        return None
    field = raw.get("field")
    direction = raw.get("direction")
    if not isinstance(field, str) or not field.strip():
        return None
    if direction not in ORDER_DIRECTIONS:
        return None
    return {"field": field, "direction": direction}


def widget_default_order(*, default_group: Any, default_sort: Any) -> list[dict[str, str]]:
    """The effective order spec: group level first, then sort level.

    A sort on the group's own field adds nothing (rows sharing a group value
    are tied on that field anyway), so it's dropped rather than sent to
    Airtable as a duplicate sort key."""
    order: list[dict[str, str]] = []
    for level in (parse_default_order(default_group), parse_default_order(default_sort)):
        if level is None:
            continue
        if any(existing["field"] == level["field"] for existing in order):
            continue
        order.append(level)
    return order


def restrict_order_to_fields(
    order: list[dict[str, str]] | None, fields: Iterable[str]
) -> list[dict[str, str]]:
    """Drop every level whose field isn't displayed (Part B decision 3).

    Grouping or sorting by a hidden column would order by values the viewer
    can't see, and the cached rows don't even carry hidden columns (the
    projection), so a stale default is ignored rather than applied."""
    if not order:
        return []
    allowed = set(fields)
    kept = [level for level in order if level["field"] in allowed]
    if len(kept) != len(order):
        logger.info(
            "Airtable default order: ignoring %d level(s) on a column that isn't displayed",
            len(order) - len(kept),
        )
    return kept


def default_group_is_valid(stored: dict[str, Any] | None) -> bool:
    """Whether the stored `defaultGroup` is usable, for the `/rows/full` gate.

    With `selectedColumns` set, the field must be one of them. Without it,
    every discovered column is displayed and the router can't know the
    column list before reading the table, so a well-formed value is enough
    here; the frontend ignores a group field missing from `/rows/full`'s
    `fields` (B2)."""
    stored = stored if isinstance(stored, dict) else {}
    group = parse_default_order(stored.get("defaultGroup"))
    if group is None:
        return False
    selected = stored.get("selectedColumns")
    if isinstance(selected, list) and selected:
        return group["field"] in selected
    return True


def airtable_sort_option(order: list[dict[str, str]] | None) -> list[str]:
    """The order spec as pyairtable's `sort` option: field names, `-`-prefixed
    for descending. pyairtable turns this into `sort[i][field]/[direction]`
    query params on a GET and a `[{field, direction}]` list in the JSON body
    when `_list_records_page` falls back to `POST …/listRecords`.

    pyairtable reads a leading `-` as "descending", so an ASCENDING sort on a
    field whose own name starts with `-` can't be expressed; that level is
    skipped (logged). A descending one is fine: `--x` strips one `-`."""
    sort: list[str] = []
    for level in order or []:
        field = level["field"]
        if level["direction"] == "desc":
            sort.append("-" + field)
        elif field.startswith("-"):
            logger.warning(
                "Airtable default order: can't send an ascending sort on a "
                "field named with a leading '-' through pyairtable; skipped"
            )
        else:
            sort.append(field)
    return sort


# ── JS `Number()` / `String()` parity ──────────────────────────────────────

# ECMAScript StrWhiteSpaceChar: WhiteSpace + LineTerminator. Python's own
# `str.strip()` differs (it strips \x1c-\x1f, and keeps ﻿).
_JS_WHITESPACE = (
    "\t\n\v\f\r          "
    "        　﻿"
)
# StrDecimalLiteral, ASCII digits only (Python's `\d` and `float()` would
# also accept other scripts' digits and `1_000`, which JS rejects).
_JS_DECIMAL_RE = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_JS_RADIX_RE = re.compile(r"0([xXoObB])([0-9a-zA-Z]+)")
_RADIX = {"x": 16, "o": 8, "b": 2}


def _js_number_to_string(value: float | int) -> str:
    """JS `String(number)` (Number::toString, radix 10)."""
    if isinstance(value, int):
        if abs(value) < 10**21:
            return str(value)
        value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() is the shortest round-tripping decimal, like JS; only the
    # layout (where the point goes, when to use an exponent) differs. Per
    # the spec: `digits` is s (k digits, no trailing zeros), value = s×10^(n−k).
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, digit_tuple)).lstrip("0")
    trimmed = digits.rstrip("0")
    exponent += len(digits) - len(trimmed)
    digits = trimmed
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp = ("+" if e >= 0 else "-") + str(abs(e))
    if k == 1:
        return sign + digits + "e" + exp
    return sign + digits[0] + "." + digits[1:] + "e" + exp


def _js_to_string(value: Any) -> str:
    """JS `String(value)` for JSON-shaped values (used by `Number(array)`)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _js_number_to_string(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Array.prototype.toString → join(","), null/undefined as "".
        return ",".join("" if v is None else _js_to_string(v) for v in value)
    return "[object Object]"


def _js_string_to_number(text: str) -> float:
    body = text.strip(_JS_WHITESPACE)
    if body == "":
        return 0.0
    if _JS_DECIMAL_RE.fullmatch(body):
        return float(body)
    radix = _JS_RADIX_RE.fullmatch(body)
    if radix:
        try:
            return float(int(radix.group(2), _RADIX[radix.group(1).lower()]))
        except ValueError:
            return math.nan
    return math.nan


def js_number(value: Any) -> float:
    """JS `Number(value)` for JSON-shaped values; NaN when it doesn't convert."""
    if value is None:
        return 0.0
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _js_string_to_number(value)
    if isinstance(value, list):
        return _js_string_to_number(_js_to_string(value))
    return math.nan


def format_cell(value: Any) -> str:
    """The frontend's `formatCell`: '' for null, arrays joined with ', ',
    objects as JSON, everything else `String()`."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(format_cell(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return _js_to_string(value)


# ── Collation (approximates localeCompare with {numeric: true}) ────────────

_CHUNK_RE = re.compile(r"\d+|.", re.DOTALL)

# ICU root (CLDR/DUCET) primary order of the ASCII non-alphanumerics, after
# whitespace (rank 0). Other symbols follow, by code point.
_PUNCTUATION_RANK = {
    ch: rank for rank, ch in enumerate("_-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$", start=1)
}
# The control characters ICU sorts as whitespace. Every other control or
# format character (a BOM, \x1c) is completely ignorable there.
_COLLATION_SPACE_CONTROLS = "\t\n\v\f\r\x85"


def _symbol_rank(ch: str) -> int:
    if ch in _COLLATION_SPACE_CONTROLS or unicodedata.category(ch) in ("Zs", "Zl", "Zp"):
        return 0
    return _PUNCTUATION_RANK.get(ch, 1000 + ord(ch))


def collation_key(text: str) -> tuple:
    """A sort key approximating ICU root collation with numeric ordering.

    * primary: case- and accent-insensitive; digit runs compare by numeric
      value (so "item 2" < "item 10"); whitespace < punctuation/symbols <
      digits < letters, as in ICU; control and format characters (a BOM,
      \\x1c) are ignored, as ICU treats them as completely ignorable;
    * secondary: accents; tertiary: case, lowercase first (ICU's default).
    """
    if text.isascii():
        return _ascii_collation_key(text)
    return _general_collation_key(text)


def _general_collation_key(text: str) -> tuple:
    primary: list[tuple] = []
    secondary: list[str] = []
    tertiary: list[int] = []
    for match in _CHUNK_RE.finditer(text):
        chunk = match.group(0)
        if chunk[0].isdecimal():
            primary.append((1, int(chunk)))
            secondary.append("")
            tertiary.append(0)
            continue
        if unicodedata.category(chunk) in ("Cc", "Cf") and chunk not in _COLLATION_SPACE_CONTROLS:
            continue
        decomposed = unicodedata.normalize("NFD", chunk)
        base = "".join(c for c in decomposed if not unicodedata.combining(c))
        marks = "".join(c for c in decomposed if unicodedata.combining(c))
        folded = base.casefold()
        if folded.isalpha():
            primary.append((2, folded))
        else:
            primary.append((0, _symbol_rank(base[:1]) if base else 0))
        secondary.append(marks)
        tertiary.append(1 if chunk != chunk.lower() else 0)
    return (tuple(primary), tuple(secondary), tuple(tertiary))


_ASCII_DIGIT_RUN_RE = re.compile(r"[0-9]+|.", re.DOTALL)


def _ascii_collation_key(text: str) -> tuple:
    """`collation_key` for pure-ASCII text, without the per-character Unicode
    normalization (ASCII has no accents). Produces the SAME key shape as the
    general path, so ASCII and non-ASCII values still compare correctly."""
    primary: list[tuple] = []
    tertiary: list[int] = []
    for chunk in _ASCII_DIGIT_RUN_RE.findall(text):
        if chunk[0] <= "9" and chunk[0] >= "0":
            primary.append((1, int(chunk)))
            tertiary.append(0)
        elif chunk.isalpha():
            primary.append((2, chunk.lower()))
            tertiary.append(1 if chunk.isupper() else 0)
        elif chunk < " " or chunk == "\x7f":
            if chunk not in _COLLATION_SPACE_CONTROLS:
                continue
            primary.append((0, 0))
            tertiary.append(0)
        else:
            primary.append((0, 0 if chunk == " " else _PUNCTUATION_RANK.get(chunk, 1000 + ord(chunk))))
            tertiary.append(0)
    return (tuple(primary), ("",) * len(primary), tuple(tertiary))


def _cmp(a: Any, b: Any) -> int:
    return (a > b) - (a < b)


# ── The comparator ─────────────────────────────────────────────────────────


class _Cell:
    """One cell's precomputed comparison data."""

    __slots__ = ("null", "num", "_value", "_coll", "_memo")

    def __init__(self, value: Any, memo: dict[str, tuple] | None = None) -> None:
        self.null = value is None
        self._value = value
        self._coll: tuple | None = None
        # Shared by every cell of one sort, so a repeated value (a Status
        # column) is collated once. Per call on purpose: a module-wide cache
        # would keep long-text keys alive across requests.
        self._memo = memo if memo is not None else {}
        number = math.nan if self.null or value == "" else js_number(value)
        # "Numeric" means `Number()` converts AND the value isn't exactly ''
        # (the frontend's `a !== ''` guard). NaN (incl. Infinity - Infinity
        # later) is handled in the compare.
        self.num: float | None = None if math.isnan(number) else number

    @property
    def coll(self) -> tuple:
        if self._coll is None:
            text = format_cell(self._value)
            key = self._memo.get(text)
            if key is None:
                key = self._memo[text] = collation_key(text)
            self._coll = key
        return self._coll


def _compare_cells(x: _Cell, y: _Cell) -> int:
    if x.null and y.null:
        return 0
    if x.null:
        return 1
    if y.null:
        return -1
    if x.num is not None and y.num is not None:
        diff = x.num - y.num
        # JS: a NaN comparator result (Infinity - Infinity) counts as 0.
        return 0 if math.isnan(diff) else _cmp(diff, 0)
    return _cmp(x.coll, y.coll)


def compare_cell_values(a: Any, b: Any) -> int:
    """Python twin of the frontend's `compareCellValues`, ascending.
    Descending is the negation (null then sorts FIRST, as in the frontend)."""
    return _compare_cells(_Cell(a), _Cell(b))


def _level_keys(cells: list[_Cell]) -> list[Any]:
    """Per-row sort keys for one level.

    `compareCellValues` is numeric only when BOTH sides are numeric, so in
    general it isn't a key function. But when every non-null cell is numeric,
    or none is, it reduces to one — which is the common case, and far cheaper
    than `cmp_to_key` over a 50,000-row cached table on every page request.
    A column mixing numeric and non-numeric values falls back to the
    comparator itself."""
    non_null = [c for c in cells if not c.null]
    if all(c.num is not None for c in non_null):
        return [(1, 0.0) if c.null else (0, c.num) for c in cells]
    if all(c.num is None for c in non_null):
        return [(1, ()) if c.null else (0, c.coll) for c in cells]
    key = cmp_to_key(_compare_cells)
    return [key(c) for c in cells]


def sort_rows(rows: list[dict[str, Any]], order: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    """Return a NEW list of `rows` ordered by `order` (group level first).

    Sorted one level at a time, least significant first, with Python's
    stable sort, which equals one composite comparator. `reverse=True` keeps
    ties in their original order, exactly like the frontend's
    `dir * compareCellValues(...)` returning 0 under a stable sort, so rows
    tied on every level keep the cached (view) order. Never mutates `rows`:
    it's the cache envelope's own list."""
    ordered = list(rows)
    for level in reversed(order or []):
        field = level["field"]
        memo: dict[str, tuple] = {}
        cells = [_Cell(row.get(field), memo) for row in ordered]
        keys = _level_keys(cells)
        index = sorted(
            range(len(ordered)), key=keys.__getitem__, reverse=level["direction"] == "desc"
        )
        ordered = [ordered[i] for i in index]
    return ordered
