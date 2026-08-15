"""
Python port of `airtable_formulas.personalize_clause`, for matching a
personalize column's value against a caller's email OUTSIDE of an Airtable
formula — used by the widget row cache (plan_airtable_widget_caching_2026-08-06.md
§6), where personalization is applied at serve time against an already-fetched
(and possibly cached) row, not baked into the `filterByFormula` sent to
Airtable.

This is a **security-critical, line-for-line port** — see `personalize_match`'s
docstring for the exact algorithm and why it must not "improve" on the
formula version while porting it (plan landmine L4). Kept in its own module,
deliberately small and dependency-free, so it can land and be reviewed alone.
"""

from __future__ import annotations

from typing import Any

# MUST stay identical to `airtable_formulas._PERSONALIZE_SEPARATOR_LITERALS`.
# Newlines are deliberately NOT a separator here either — see that module's
# comment for why (no CHAR() in Airtable's formula language forced the
# omission there; porting to Python removes that constraint, which is
# precisely why it would be easy to "fix" here and silently widen what a
# user can see, per landmine L4). Treat adding newline support as its own,
# separately-reviewed change, not a fix.
_PERSONALIZE_SEPARATOR_LITERALS = (";", "|")


def _coerce_text(value: Any) -> str:
    """Mirror Airtable's `{Col} & ''` text coercion.

    A scalar becomes its string form; an array (multi-select, linked
    records, collaborators) becomes a comma-joined string, the way Airtable
    renders array fields when coerced to text. The exact join separator
    doesn't matter — every space is stripped in a later step regardless.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_coerce_text(v) for v in value if v is not None)
    if isinstance(value, bool):
        # Airtable checkboxes coerced to text render as 1/0, not True/False.
        return "1" if value else "0"
    return str(value)


def personalize_match(value: Any, email: str) -> bool:
    """Does `value` (a personalize column's raw cell value) contain `email`
    as a WHOLE entry, case-insensitively?

    Line-for-line port of `airtable_formulas.personalize_clause`'s formula:

        haystack = LOWER({Col} & '')
        haystack = SUBSTITUTE(haystack, ';', ',')
        haystack = SUBSTITUTE(haystack, '|', ',')
        haystack = SUBSTITUTE(haystack, ' ', '')
        match    = FIND(',<email>,', ',' & haystack & ',') > 0

    i.e.: coerce to text → lowercase → normalize `;`/`|` separators to `,` →
    strip spaces → delimiter-bounded substring match. Same reasoning as the
    formula version: an UNANCHORED substring match would let
    "amy@renphil.org" match inside "tamy@renphil.org" (over data this widget
    describes as financial and medical), so the match must be bounded by
    comma delimiters on both sides, never a bare substring check.

    Supported separators: comma (implicitly, via the delimiter-bounding),
    semicolon, pipe, and surrounding spaces. NEWLINE-separated lists do
    **not** match — see `_PERSONALIZE_SEPARATOR_LITERALS` above.

    Returns False when `email` is empty — callers must not treat that as
    "no filter"; the caller is responsible for the fail-closed gate before
    ever reaching this function (an empty email must produce zero rows, not
    a call to this predicate at all — see `resolve_personalize_gate`).
    """
    address = (email or "").strip().lower()
    if not address:
        return False

    haystack = _coerce_text(value).lower()
    for separator in _PERSONALIZE_SEPARATOR_LITERALS:
        haystack = haystack.replace(separator, ",")
    haystack = haystack.replace(" ", "")

    needle = f",{address},"
    return needle in f",{haystack},"


def resolve_personalize_gate(
    *, personalize_enabled: bool, personalize_column: str | None, email: str | None
) -> bool:
    """Mirrors `airtable_formulas.widget_formula`'s fail-closed `allowed`
    check, for the Python serve-time path.

    Returns True when the request is BLOCKED — personalization is enabled
    but cannot be applied (no column configured, or no authenticated
    email) — in which case the caller MUST return zero rows and must NOT
    read the cache at all (plan §6.2). Returns False when it's safe to
    proceed (either personalization is off, or it's on and applicable).

    Unlike `widget_formula`, there is no formula-field-name validation step
    here: this path never builds a formula, it does a plain dict lookup by
    column name, so there is no equivalent failure mode to mirror.
    """
    if not personalize_enabled:
        return False
    column = (personalize_column or "").strip()
    address = (email or "").strip()
    return not column or not address
