"""
Step 3 regression test — server-side Airtable formula construction.

Covers the three injection defects in the frontend builder that were
deliberately NOT ported, and the cross-user leak in the personalize match.

The personalize section includes a small interpreter for the subset of
Airtable formula semantics the clause uses (LOWER / SUBSTITUTE / FIND /
concatenation), so the anchoring behaviour is actually EXECUTED against
realistic cell values rather than asserted by eyeballing the string. That
simulator is not Airtable — see the caveat printed at the end.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe <this file>
"""
from __future__ import annotations

import re
import sys

from app.helpers import airtable_formulas as af

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))


# ── A tiny evaluator for the personalize clause ────────────────────────
def eval_personalize(clause: str, cell_value) -> bool:
    """Execute the generated clause against one cell value.

    Implements only what personalize_clause emits:
      FIND(<literal>, ',' & <expr> & ',') > 0
      expr := LOWER({Field} & '') | SUBSTITUTE(expr, <sep>, ',')
    """
    m = re.match(r"^FIND\('(.*?)', (.*)\) > 0$", clause, re.S)
    if not m:
        raise AssertionError(f"unexpected clause shape: {clause}")
    needle = m.group(1).replace("\\'", "'").replace("\\\\", "\\")
    haystack_expr = m.group(2)

    # Airtable renders an array cell comma-separated when coerced to text.
    if isinstance(cell_value, (list, tuple)):
        text = ", ".join(str(v) for v in cell_value)
    elif cell_value is None:
        text = ""
    else:
        text = str(cell_value)

    def evaluate(expr: str) -> str:
        expr = expr.strip()
        if expr.startswith("',' & ") and expr.endswith(" & ','"):
            return "," + evaluate(expr[len("',' & "):-len(" & ','")]) + ","
        if expr.startswith("SUBSTITUTE("):
            inner, sep, repl = split_args(expr[len("SUBSTITUTE("):-1])
            return evaluate(inner).replace(literal(sep), literal(repl))
        if expr.startswith("LOWER("):
            return evaluate(expr[len("LOWER("):-1]).lower()
        if expr.endswith(" & ''"):
            return evaluate(expr[:-len(" & ''")])
        if expr.startswith("{") and expr.endswith("}"):
            return text
        raise AssertionError(f"unhandled expr: {expr}")

    def literal(token: str) -> str:
        token = token.strip()
        if token == "CHAR(10)":
            return "\n"
        if token == "CHAR(13)":
            return "\r"
        if token.startswith("'") and token.endswith("'"):
            return token[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        raise AssertionError(f"unhandled literal: {token}")

    def split_args(s: str):
        depth, quoted, parts, buf = 0, False, [], ""
        i = 0
        while i < len(s):
            ch = s[i]
            if quoted:
                if ch == "\\":
                    buf += s[i:i + 2]
                    i += 2
                    continue
                if ch == "'":
                    quoted = False
                buf += ch
            else:
                if ch == "'":
                    quoted = True
                    buf += ch
                elif ch == "(":
                    depth += 1
                    buf += ch
                elif ch == ")":
                    depth -= 1
                    buf += ch
                elif ch == "," and depth == 0:
                    parts.append(buf)
                    buf = ""
                else:
                    buf += ch
            i += 1
        parts.append(buf)
        return parts

    return needle in evaluate(haystack_expr)


def main() -> int:
    # ── 1. THE LEAK: suffix-substring collision ────────────────────────
    print("\n1. Personalize is anchored (the tamy@/amy@ leak)")
    clause = af.personalize_clause("Owner", "amy@renphil.org")
    check("amy@ matches her own row", eval_personalize(clause, "amy@renphil.org"))
    check("amy@ does NOT match tamy@'s row",
          not eval_personalize(clause, "tamy@renphil.org"),
          "this is the exact cross-user leak in the shipped client version")
    for mine, theirs in [("an@x.org", "dan@x.org"), ("jo@x.org", "jojo@x.org"),
                         ("ben@x.org", "reuben@x.org")]:
        c = af.personalize_clause("Owner", mine)
        check(f"{mine} does not match {theirs}", not eval_personalize(c, theirs))

    # ── 2. Real-world cell shapes still match ──────────────────────────
    print("\n2. Multi-collaborator shapes still match")
    clause = af.personalize_clause("Owner", "amy@renphil.org")
    check("comma+space list", eval_personalize(clause, "bob@x.org, amy@renphil.org"))
    check("comma list, no space", eval_personalize(clause, "bob@x.org,amy@renphil.org"))
    check("semicolon list", eval_personalize(clause, "bob@x.org; amy@renphil.org"))
    check("pipe list", eval_personalize(clause, "bob@x.org|amy@renphil.org"))
    # Newline-separated lists are a KNOWN, deliberate gap: normalising them
    # would need a newline literal in the formula, and the CHAR() approach
    # that seemed obvious does not exist in Airtable. Asserted so the gap is
    # visible rather than forgotten — see _PERSONALIZE_SEPARATOR_LITERALS.
    check("newline list is a known gap (does NOT match)",
          not eval_personalize(clause, "bob@x.org\namy@renphil.org"))
    check("array cell (multi-select / linked records)",
          eval_personalize(clause, ["bob@x.org", "amy@renphil.org"]))
    check("case-insensitive", eval_personalize(clause, "AMY@RenPhil.ORG"))
    check("leading/trailing spaces", eval_personalize(clause, "  amy@renphil.org  "))
    check("empty cell does not match", not eval_personalize(clause, ""))
    check("unrelated cell does not match", not eval_personalize(clause, "zoe@x.org"))
    check("suffix collision inside a LIST still excluded",
          not eval_personalize(clause, "tamy@renphil.org, zoe@x.org"))

    # ── 3. Field-name injection ────────────────────────────────────────
    print("\n3. Field names cannot break out")
    for bad in ["X} , TRUE()) , OR((TRUE", "a}b", "a{b", "back\\slash"]:
        try:
            af.validate_field_name(bad)
            check(f"rejects {bad!r}", False, "was accepted")
        except af.FormulaFieldError:
            check(f"rejects {bad!r}", True)
    check("accepts a normal name", af.validate_field_name("  Owner Email ") == "Owner Email")
    try:
        af.personalize_clause("bad}name", "a@b.c")
        check("personalize rejects a bad column", False)
    except af.FormulaFieldError:
        check("personalize rejects a bad column", True)

    # ── 4. Value escaping ──────────────────────────────────────────────
    print("\n4. Filter values cannot break out of their literal")
    # The frontend escaped only the double quote, never the backslash, so a
    # value ending in a backslash closed the literal early.
    clause = af.widget_filter_clause("Status", "eq", "ends with backslash\\")
    check("backslash escaped", "\\\\" in clause, clause)
    check("literal still balanced", clause.count("'") % 2 == 0, clause)
    clause = af.widget_filter_clause("Status", "eq", "it's")
    check("single quote escaped", "\\'" in clause, clause)
    clause = af.widget_filter_clause("Status", "eq", "a' OR TRUE() OR '")
    check("injection attempt stays inside the literal",
          clause == "{Status} = 'a\\' OR TRUE() OR \\''", clause)

    # ── 5. Numeric operators validate instead of interpolating ─────────
    print("\n5. gt/lt reject non-numeric operands")
    check("gt with a number", af.widget_filter_clause("Amount", "gt", "100") == "{Amount} > 100.0")
    for bad in ["abc", "1) OR TRUE() OR (1", "", None]:
        try:
            af.widget_filter_clause("Amount", "gt", bad)
            check(f"gt rejects {bad!r}", False, "was accepted")
        except af.FormulaFieldError:
            check(f"gt rejects {bad!r}", True)

    # ── 6. Operator coverage ───────────────────────────────────────────
    print("\n6. Operator set")
    for op in ["eq", "neq", "contains", "not_contains", "is_empty",
               "is_not_empty"]:
        check(f"{op} produces a clause",
              bool(af.widget_filter_clause("F", op, "v")))
    check("unknown operator -> None", af.widget_filter_clause("F", "bogus", "v") is None)
    check("is_empty uses BLANK()", af.widget_filter_clause("F", "is_empty") == "{F} = BLANK()")

    # ── 7. widget_formula fail-closed contract ─────────────────────────
    print("\n7. widget_formula never falls back to unfiltered")
    f, ok = af.widget_formula(filters=[{"field": "S", "operator": "eq", "value": "A"}])
    check("no personalize -> allowed with filters", ok and f == "{S} = 'A'", f"{f!r}")

    f, ok = af.widget_formula(personalize_enabled=True, personalize_column="Owner", email="")
    check("personalize on, no email -> NOT allowed", ok is False and f is None)

    f, ok = af.widget_formula(personalize_enabled=True, personalize_column="", email="a@b.c")
    check("personalize on, no column -> NOT allowed", ok is False and f is None)

    f, ok = af.widget_formula(personalize_enabled=True, personalize_column="bad}col",
                              email="a@b.c")
    check("personalize on, unusable column -> NOT allowed", ok is False and f is None)

    f, ok = af.widget_formula(
        filters=[{"field": "S", "operator": "eq", "value": "A"}],
        personalize_enabled=True, personalize_column="Owner", email="a@b.c")
    check("personalize applied -> allowed", ok is True)
    check("filters AND personalize", f.startswith("AND({S} = 'A', FIND("), f"{f!r}")

    f, ok = af.widget_formula(personalize_enabled=True, personalize_column="Owner",
                              email="   ")
    check("whitespace-only email -> NOT allowed", ok is False and f is None)

    # ── 8. Filter rows are skipped, not fatal ──────────────────────────
    print("\n8. Malformed filter rows")
    f = af.widget_filters_clause([{"field": "", "operator": "eq", "value": "x"},
                                  {"field": "S", "operator": "eq", "value": "A"},
                                  "not-a-dict"])
    check("blank/invalid rows skipped", f == "{S} = 'A'", f"{f!r}")
    check("no filters -> None", af.widget_filters_clause([]) is None)

    # ── 9. No raw control characters in the emitted formula ────────────
    print("\n9. Emitted formula only uses functions Airtable actually has")
    # An earlier version emitted CHAR(10)/CHAR(13) for newline separators.
    # Airtable has no CHAR function, so EVERY query 4xx'd with
    # "Unknown function names: CHAR". The old test asserted `"CHAR(10)" in
    # clause` — it validated the assumption instead of checking it, which is
    # exactly why it passed while production failed. This checks the emitted
    # function names against Airtable's actual formula reference instead.
    AIRTABLE_FUNCTIONS = {
        "AND", "OR", "NOT", "IF", "BLANK", "FIND", "SEARCH", "LOWER", "UPPER",
        "SUBSTITUTE", "TRIM", "CONCATENATE", "LEN", "LEFT", "RIGHT", "MID",
        "REPLACE", "REPT", "T", "VALUE", "ARRAYJOIN", "ARRAYCOMPACT",
        "ARRAYFLATTEN", "ARRAYUNIQUE", "DATETIME_PARSE", "DATETIME_FORMAT",
        "IS_SAME", "IS_BEFORE", "IS_AFTER", "TODAY", "NOW", "ROUND", "ABS",
    }
    clauses = [
        af.personalize_clause("Owner", "a@b.c"),
        af.widget_filter_clause("F", "contains", "x"),
        af.widget_filter_clause("F", "is_empty"),
        af.widget_filter_clause("F", "not_contains", "x"),
        af.widget_formula(filters=[{"field": "F", "operator": "eq", "value": "v"}],
                          personalize_enabled=True, personalize_column="Owner",
                          email="a@b.c")[0],
    ]
    unknown: set[str] = set()
    for c in clauses:
        for name in re.findall(r"\b([A-Z][A-Z_0-9]*)\s*\(", c or ""):
            if name not in AIRTABLE_FUNCTIONS:
                unknown.add(name)
    check("no unknown function names", not unknown, f"unknown: {sorted(unknown)}")

    clause = af.personalize_clause("Owner", "a@b.c")
    ctrl = [c for c in clause if ord(c) < 32]
    check("no raw control characters", not ctrl,
          f"found {[hex(ord(c)) for c in ctrl]}")
    check("separators are plain literals",
          "SUBSTITUTE" in clause and "CHAR" not in clause, clause[:120])

    print(f"\n{'=' * 60}\n{PASS} passed, {FAIL} failed\n{'=' * 60}")
    print(
        "\nNOTE: section 1/2 run against a local simulator of Airtable's\n"
        "LOWER/SUBSTITUTE/FIND semantics, not Airtable itself. The anchoring\n"
        "LOGIC is proven; the assumption that `{Col} & ''` renders an array\n"
        "comma-separated still needs one check against a real base."
    )
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
