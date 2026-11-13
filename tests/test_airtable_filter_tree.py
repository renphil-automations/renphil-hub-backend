"""
Advanced admin filters, phase 1 (plan_airtable_advanced_filters_2026-09-26.md §3).

A widget's stored `filters` may now be the legacy flat list OR a root group
of nested AND/OR groups. This module pins:

* **byte identity** — every legacy-list formula below was captured from the
  PRE-change compiler (`05a0daa`) and pasted verbatim. A legacy list must
  compile to exactly these strings forever: they are the cache-key and
  behaviour contract for every widget saved before advanced filters;
* the tree compiler's semantics — parenthesization, single-child collapse,
  empty/incomplete groups ignored (never "match everything"), depth and
  condition caps, malformed nodes dropped rather than raised;
* `FormulaFieldError` still propagating, at the top level AND nested;
* personalization still AND-ed OUTSIDE the tree;
* the index-snapshot route passing a tree through to its walk (§1.3).

Run: `PYTHONPATH=. ./.venv/Scripts/python.exe -m unittest tests.test_airtable_filter_tree`
"""

from __future__ import annotations

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.routers.airtable as airtable_router
from app.helpers import airtable_formulas as af
from app.models.airtable import (
    AirtableChartPreviewRequest,
    AirtableEditorPreviewRequest,
    AirtableWidgetIndexSnapshotResponse,
)
from app.services.airtable_service import AirtableService


# ── byte-identity contract, captured from 05a0daa BEFORE any edit ──────────
LEGACY_CLAUSE_FIXTURES = [
    ('empty_list', [],
     None),
    ('none', None,
     None),
    ('op_eq', [{'field': 'Status', 'operator': 'eq', 'value': 'Active'}],
     "{Status} = 'Active'"),
    ('op_neq', [{'field': 'Status', 'operator': 'neq', 'value': 'Closed'}],
     "NOT({Status} = 'Closed')"),
    ('op_contains', [{'field': 'Name', 'operator': 'contains', 'value': "O'Brien"}],
     "FIND('O\\'Brien', {Name} & '') > 0"),
    ('op_not_contains', [{'field': 'Name', 'operator': 'not_contains', 'value': 'test'}],
     "NOT(FIND('test', {Name} & '') > 0)"),
    ('op_is_empty', [{'field': 'Notes', 'operator': 'is_empty', 'value': ''}],
     '{Notes} = BLANK()'),
    ('op_is_not_empty', [{'field': 'Notes', 'operator': 'is_not_empty', 'value': ''}],
     'NOT({Notes} = BLANK())'),
    ('op_gt', [{'field': 'Amount', 'operator': 'gt', 'value': '5000'}],
     '{Amount} > 5000.0'),
    ('op_lt', [{'field': 'Amount', 'operator': 'lt', 'value': 12.5}],
     '{Amount} < 12.5'),
    ('eq_one_item_list', [{'field': 'Status', 'operator': 'eq', 'value': ['Active']}],
     "{Status} = 'Active'"),
    ('eq_multi_list', [{'field': 'Status', 'operator': 'eq', 'value': ['Active', 'Pending', 'On hold']}],
     "OR({Status} = 'Active', {Status} = 'Pending', {Status} = 'On hold')"),
    ('neq_one_item_list', [{'field': 'Status', 'operator': 'neq', 'value': ['Closed']}],
     "NOT({Status} = 'Closed')"),
    ('neq_multi_list', [{'field': 'Status', 'operator': 'neq', 'value': ['Closed', 'Lost']}],
     "NOT(OR({Status} = 'Closed', {Status} = 'Lost'))"),
    ('blank_field_skipped', [{'field': '  ', 'operator': 'eq', 'value': 'x'}, {'field': 'Status', 'operator': 'eq', 'value': 'Active'}],
     "{Status} = 'Active'"),
    ('blank_value_dropped', [{'field': 'Status', 'operator': 'eq', 'value': ''}, {'field': 'Owner', 'operator': 'eq', 'value': []}],
     None),
    ('blank_value_dropped_mixed', [{'field': 'Status', 'operator': 'eq', 'value': ''}, {'field': 'Owner', 'operator': 'contains', 'value': 'amy'}],
     "FIND('amy', {Owner} & '') > 0"),
    ('non_dict_item', ['garbage', 42, None, {'field': 'Status', 'operator': 'eq', 'value': 'Active'}],
     "{Status} = 'Active'"),
    ('unknown_operator', [{'field': 'Status', 'operator': 'xor', 'value': 'Active'}],
     None),
    ('unknown_operator_mixed', [{'field': 'Status', 'operator': 'xor', 'value': 'Active'}, {'field': 'Amount', 'operator': 'gt', 'value': '1'}],
     '{Amount} > 1.0'),
    ('three_mixed', [{'field': 'Status', 'operator': 'eq', 'value': ['Active', 'Pending']}, {'field': 'Owner', 'operator': 'contains', 'value': 'amy'}, {'field': 'Amount', 'operator': 'gt', 'value': '5000'}],
     "AND(OR({Status} = 'Active', {Status} = 'Pending'), FIND('amy', {Owner} & '') > 0, {Amount} > 5000.0)"),
]

LEGACY_FORMULA_FIXTURES = [
    ('personalize_on', {'filters': [{'field': 'Status', 'operator': 'eq', 'value': 'Active'}, {'field': 'Amount', 'operator': 'gt', 'value': '5'}], 'personalize_enabled': True, 'personalize_column': 'Owner Email', 'email': 'Amy@RenPhil.org'},
     ("AND(AND({Status} = 'Active', {Amount} > 5.0), FIND(',amy@renphil.org,', ',' & SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(LOWER({Owner Email} & ''), ';', ','), '|', ','), ' ', '') & ',') > 0)", True)),
    ('personalize_on_no_filters', {'filters': [], 'personalize_enabled': True, 'personalize_column': 'Owner Email', 'email': 'amy@renphil.org'},
     ("FIND(',amy@renphil.org,', ',' & SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(LOWER({Owner Email} & ''), ';', ','), '|', ','), ' ', '') & ',') > 0", True)),
    ('personalize_missing_column', {'filters': [{'field': 'Status', 'operator': 'eq', 'value': 'Active'}], 'personalize_enabled': True, 'personalize_column': None, 'email': 'amy@renphil.org'},
     (None, False)),
    ('personalize_missing_email', {'filters': [{'field': 'Status', 'operator': 'eq', 'value': 'Active'}], 'personalize_enabled': True, 'personalize_column': 'Owner Email', 'email': '  '},
     (None, False)),
    ('personalize_off', {'filters': [{'field': 'Status', 'operator': 'eq', 'value': 'Active'}, {'field': 'Amount', 'operator': 'gt', 'value': '5'}], 'personalize_enabled': False, 'personalize_column': 'Owner Email', 'email': 'amy@renphil.org'},
     ("AND({Status} = 'Active', {Amount} > 5.0)", True)),
    ('personalize_off_no_filters', {'filters': None, 'personalize_enabled': False},
     (None, True)),
]

# Captured from 05a0daa too: each of these RAISED FormulaFieldError, from
# both widget_filters_clause and widget_formula (personalization on). Note
# the last: an unknown operator does NOT save a bad field name, because the
# field is validated before the operator is looked at.
LEGACY_RAISING_FIXTURES = [
    ('bad_field_brace', [{'field': 'X} , TRUE()) , OR((TRUE', 'operator': 'eq', 'value': 'a'}]),
    ('bad_field_backslash', [{'field': 'A\\B', 'operator': 'eq', 'value': 'a'}]),
    ('gt_non_numeric', [{'field': 'Amount', 'operator': 'gt', 'value': 'lots'}]),
    ('lt_non_numeric_mixed', [{'field': 'Status', 'operator': 'eq', 'value': 'A'}, {'field': 'Amount', 'operator': 'lt', 'value': 'abc'}]),
    ('bad_field_unknown_op', [{'field': 'A{B', 'operator': 'xor', 'value': 'a'}]),
]

PERSONALIZE_OWNER = (
    "FIND(',amy@renphil.org,', ',' & SUBSTITUTE(SUBSTITUTE(SUBSTITUTE("
    "LOWER({Owner Email} & ''), ';', ','), '|', ','), ' ', '') & ',') > 0"
)


# ── tree builders ──────────────────────────────────────────────────────────
def cond(field, operator="eq", value="", **extra):
    return {"type": "condition", "id": f"c-{field}", "field": field,
            "operator": operator, "value": value, **extra}


def group(conjunction, *items, **extra):
    return {"type": "group", "id": f"g-{conjunction}", "conjunction": conjunction,
            "items": list(items), **extra}


A = cond("Status", "eq", "Active")
B = cond("Owner", "contains", "amy")
C = cond("Amount", "gt", "5000")
A_SQL = "{Status} = 'Active'"
B_SQL = "FIND('amy', {Owner} & '') > 0"
C_SQL = "{Amount} > 5000.0"
INCOMPLETE = cond("Region", "eq", "")  # no value → ignored
NO_FIELD = cond("  ", "eq", "x")  # no field → ignored


class LegacyListByteIdentityTests(unittest.TestCase):
    """A legacy flat list compiles EXACTLY as before the tree compiler."""

    def test_widget_filters_clause_is_byte_identical(self):
        for name, filters, expected in LEGACY_CLAUSE_FIXTURES:
            with self.subTest(name):
                self.assertEqual(af.widget_filters_clause(filters), expected)

    def test_well_formed_legacy_lists_log_nothing(self):
        """Only a non-dict item warns; every other legacy case is silent,
        so the new warnings don't fire on every read of an old widget."""
        for name, filters, _ in LEGACY_CLAUSE_FIXTURES:
            if name == "non_dict_item":
                continue
            with self.subTest(name), self.assertNoLogs(af.logger, "WARNING"):
                af.widget_filters_clause(filters)

    def test_widget_formula_is_byte_identical(self):
        for name, kwargs, expected in LEGACY_FORMULA_FIXTURES:
            with self.subTest(name):
                self.assertEqual(af.widget_formula(**kwargs), expected)

    def test_legacy_raising_cases_still_raise(self):
        for name, filters in LEGACY_RAISING_FIXTURES:
            with self.subTest(name):
                with self.assertRaises(af.FormulaFieldError):
                    af.widget_filters_clause(filters)
                with self.assertRaises(af.FormulaFieldError):
                    af.widget_formula(
                        filters=filters, personalize_enabled=True,
                        personalize_column="Owner", email="a@b.c",
                    )

    def test_equivalent_root_and_group_matches_legacy_list(self):
        """A root AND group holding the same conditions compiles to the same
        string as the legacy list: the frontend's first normalize-and-save
        of an old widget must not change its results."""
        for name, filters, expected in LEGACY_CLAUSE_FIXTURES:
            items = [
                {**item, "type": "condition", "id": f"n{i}"}
                for i, item in enumerate(filters or [])
                if isinstance(item, dict)
            ]
            with self.subTest(name):
                self.assertEqual(
                    af.widget_filters_clause(group("and", *items)), expected
                )


class TreeCompileTests(unittest.TestCase):
    def test_nested_and_or_and(self):
        tree = group("and", A, group("or", B, group("and", C, cond("Notes", "is_empty"))))
        self.assertEqual(
            af.widget_filters_clause(tree),
            f"AND({A_SQL}, OR({B_SQL}, AND({C_SQL}, {{Notes}} = BLANK())))",
        )

    def test_root_or(self):
        self.assertEqual(
            af.widget_filters_clause(group("or", A, B, C)),
            f"OR({A_SQL}, {B_SQL}, {C_SQL})",
        )

    def test_single_child_groups_collapse_without_wrapping(self):
        self.assertEqual(af.widget_filters_clause(group("or", A)), A_SQL)
        self.assertEqual(
            af.widget_filters_clause(group("and", group("or", group("and", A)))),
            A_SQL,
        )
        self.assertEqual(
            af.widget_filters_clause(group("and", A, group("or", B))),
            f"AND({A_SQL}, {B_SQL})",
        )

    def test_or_with_empty_group_is_just_the_other_child(self):
        """`A OR (empty group)` is A — an empty group is ignored, never
        "match everything" (plan §2.1)."""
        self.assertEqual(af.widget_filters_clause(group("or", A, group("and"))), A_SQL)
        self.assertEqual(
            af.widget_filters_clause(group("or", A, group("and", INCOMPLETE, NO_FIELD))),
            A_SQL,
        )
        self.assertNotIn("TRUE", af.widget_filters_clause(group("or", A, group("or"))))

    def test_and_with_empty_group_is_just_the_other_child(self):
        self.assertEqual(af.widget_filters_clause(group("and", A, group("or"))), A_SQL)

    def test_root_with_no_complete_condition_is_none(self):
        self.assertIsNone(af.widget_filters_clause(group("and")))
        self.assertIsNone(af.widget_filters_clause(group("or", INCOMPLETE, NO_FIELD)))
        self.assertIsNone(af.widget_filters_clause(group("and", group("or", group("and")))))

    def test_incomplete_conditions_ignored_inside_groups(self):
        self.assertEqual(
            af.widget_filters_clause(group("or", INCOMPLETE, A, NO_FIELD, B)),
            f"OR({A_SQL}, {B_SQL})",
        )

    def test_order_is_preserved(self):
        self.assertEqual(
            af.widget_filters_clause(group("and", C, A)), f"AND({C_SQL}, {A_SQL})"
        )

    def test_ids_are_ignored(self):
        with_ids = group("or", A, B)
        without = {"type": "group", "conjunction": "or", "items": [
            {k: v for k, v in n.items() if k != "id"} for n in (A, B)
        ]}
        self.assertEqual(
            af.widget_filters_clause(with_ids), af.widget_filters_clause(without)
        )

    def test_conjunction_is_case_and_space_insensitive(self):
        self.assertEqual(
            af.widget_filters_clause(group(" OR ", A, B)), f"OR({A_SQL}, {B_SQL})"
        )
        self.assertEqual(
            af.widget_filters_clause(group("And", A, B)), f"AND({A_SQL}, {B_SQL})"
        )

    def test_items_without_type_are_conditions(self):
        legacy_item = {"field": "Status", "operator": "eq", "value": "Active"}
        self.assertEqual(
            af.widget_filters_clause(group("or", legacy_item, B)),
            f"OR({A_SQL}, {B_SQL})",
        )

    def test_multi_value_eq_inside_or_group(self):
        self.assertEqual(
            af.widget_filters_clause(
                group("or", cond("Status", "eq", ["Active", "Pending"]), B)
            ),
            f"OR(OR({{Status}} = 'Active', {{Status}} = 'Pending'), {B_SQL})",
        )

    def test_input_is_not_mutated(self):
        tree = group("OR", A, "junk", group("xor", B), {"type": "weird"})
        before = copy.deepcopy(tree)
        with self.assertLogs(af.logger, "WARNING"):
            af.widget_filters_clause(tree)
        self.assertEqual(tree, before)
        legacy = [dict(A), "junk"]
        legacy_before = copy.deepcopy(legacy)
        with self.assertLogs(af.logger, "WARNING"):
            af.widget_filters_clause(legacy)
        self.assertEqual(legacy, legacy_before)


class NormalizeFilterTreeTests(unittest.TestCase):
    def test_list_becomes_root_and_group(self):
        legacy = [{"field": "Status", "operator": "eq", "value": "Active"}]
        self.assertEqual(
            af.normalize_filter_tree(legacy),
            {"type": "group", "conjunction": "and", "items": [
                {"field": "Status", "operator": "eq", "value": "Active",
                 "type": "condition"},
            ]},
        )
        self.assertEqual(
            af.normalize_filter_tree([]),
            {"type": "group", "conjunction": "and", "items": []},
        )

    def test_non_group_values_are_none(self):
        for raw in (None, {}, "Status", 42, cond("Status", "eq", "Active"),
                    {"conjunction": "and", "items": [A]}):
            with self.subTest(raw=raw):
                self.assertIsNone(af.normalize_filter_tree(raw))
                self.assertIsNone(af.widget_filters_clause(raw))

    def test_group_is_normalized(self):
        tree = group(" Or ", A, group("and", B))
        normalized = af.normalize_filter_tree(tree)
        self.assertEqual(normalized["conjunction"], "or")
        self.assertEqual(normalized["items"][1]["conjunction"], "and")


class MalformedNodeTests(unittest.TestCase):
    """Malformed nodes are dropped with a warning, never raised."""

    def test_string_items_dropped(self):
        with self.assertLogs(af.logger, "WARNING") as logs:
            self.assertEqual(
                af.widget_filters_clause(group("or", "junk", A, 7, None, B)),
                f"OR({A_SQL}, {B_SQL})",
            )
        self.assertTrue(any("not an object" in m for m in logs.output))

    def test_unknown_type_dropped(self):
        with self.assertLogs(af.logger, "WARNING") as logs:
            self.assertEqual(
                af.widget_filters_clause(group("or", {**B, "type": "rule"}, A)),
                A_SQL,
            )
        self.assertTrue(any("unknown type" in m for m in logs.output))

    def test_bad_conjunction_drops_that_group(self):
        with self.assertLogs(af.logger, "WARNING") as logs:
            self.assertEqual(
                af.widget_filters_clause(group("and", A, group("xor", B, C))), A_SQL
            )
        self.assertTrue(any("unknown conjunction" in m for m in logs.output))

    def test_missing_or_non_string_conjunction_drops_that_group(self):
        missing = {"type": "group", "items": [B]}
        numeric = {"type": "group", "conjunction": 1, "items": [B]}
        with self.assertLogs(af.logger, "WARNING"):
            self.assertEqual(af.widget_filters_clause(group("or", A, missing)), A_SQL)
            self.assertEqual(af.widget_filters_clause(group("or", A, numeric)), A_SQL)

    def test_bad_root_conjunction_means_no_filter(self):
        with self.assertLogs(af.logger, "WARNING"):
            self.assertIsNone(af.widget_filters_clause(group("xor", A, B)))

    def test_items_not_a_list(self):
        with self.assertLogs(af.logger, "WARNING") as logs:
            self.assertIsNone(
                af.widget_filters_clause({"type": "group", "conjunction": "and",
                                          "items": {"0": A}})
            )
        self.assertTrue(any("not a list" in m for m in logs.output))
        # Missing `items` is simply an empty group — no warning needed.
        with self.assertNoLogs(af.logger, "WARNING"):
            self.assertIsNone(
                af.widget_filters_clause({"type": "group", "conjunction": "and"})
            )

    def test_warnings_never_echo_values(self):
        secret = "SECRET-VALUE-xyz"
        tree = group("and", A, group(secret, cond(secret, "eq", secret)),
                     {"type": secret, "field": secret}, secret)
        with self.assertLogs(af.logger, "WARNING") as logs:
            af.widget_filters_clause(tree)
        for message in logs.output:
            self.assertNotIn(secret, message)


class CapTests(unittest.TestCase):
    def test_constants(self):
        # Mirrored by the frontend (plan §2) — change both sides together.
        self.assertEqual(af.MAX_FILTER_DEPTH, 3)
        self.assertEqual(af.MAX_FILTER_CONDITIONS, 50)

    def test_depth_three_is_allowed(self):
        tree = group("and", A, group("or", B, group("and", C)))
        self.assertEqual(
            af.widget_filters_clause(tree), f"AND({A_SQL}, OR({B_SQL}, {C_SQL}))"
        )

    def test_group_deeper_than_max_is_dropped(self):
        tree = group("and", A, group("or", B, group("and", C, group("or", cond("Deep", "eq", "x")))))
        with self.assertLogs(af.logger, "WARNING") as logs:
            self.assertEqual(
                af.widget_filters_clause(tree),
                f"AND({A_SQL}, OR({B_SQL}, {C_SQL}))",
            )
        self.assertTrue(any("deeper than 3" in m for m in logs.output))

    def test_compiler_depth_guard_without_normalize(self):
        """`_filter_node_clause` defends its own depth even when handed an
        un-normalized tree."""
        deep = group("and", group("and", group("and", group("and", A))))
        with self.assertLogs(af.logger, "WARNING"):
            self.assertIsNone(af._filter_node_clause(deep, 1))

    def test_conditions_past_cap_dropped_in_document_order(self):
        conds = [cond(f"F{i}", "eq", str(i)) for i in range(55)]
        # 20 at the root, then 35 in a nested OR: the cap bites inside it.
        tree = group("and", *conds[:20], group("or", *conds[20:]))
        with self.assertLogs(af.logger, "WARNING") as logs:
            formula = af.widget_filters_clause(tree)
        for i in range(50):
            self.assertIn(f"{{F{i}}} = '{i}'", formula)
        for i in range(50, 55):
            self.assertNotIn(f"{{F{i}}}", formula)
        overflow = [m for m in logs.output if "past 50" in m]
        self.assertEqual(len(overflow), 1)

    def test_incomplete_conditions_count_toward_cap(self):
        """The cap counts condition NODES (what the modal counts), complete
        or not."""
        tree = group("and", *[INCOMPLETE] * 50, A)
        with self.assertLogs(af.logger, "WARNING"):
            self.assertIsNone(af.widget_filters_clause(tree))

    def test_cap_resets_per_compile(self):
        tree = group("and", *[cond(f"F{i}", "eq", "x") for i in range(50)])
        first = af.widget_filters_clause(tree)
        self.assertEqual(af.widget_filters_clause(tree), first)
        self.assertIn("{F49}", first)


class FormulaFieldErrorPropagationTests(unittest.TestCase):
    """The recursion must NOT swallow FormulaFieldError: dropping a bad
    condition would silently widen the result set."""

    def test_bad_field_nested_two_groups_deep_raises(self):
        for name, filters in LEGACY_RAISING_FIXTURES:
            bad = [dict(item, type="condition") for item in filters]
            tree = group("and", A, group("or", B, group("and", C, *bad)))
            with self.subTest(name):
                with self.assertRaises(af.FormulaFieldError):
                    af.widget_filters_clause(tree)
                with self.assertRaises(af.FormulaFieldError):
                    af.widget_formula(
                        filters=tree, personalize_enabled=True,
                        personalize_column="Owner Email", email="amy@renphil.org",
                    )
                with self.assertRaises(af.FormulaFieldError):
                    af.widget_formula(filters=tree)

    def test_bad_field_in_or_with_good_sibling_raises(self):
        tree = group("or", A, cond("X}", "eq", "a"))
        with self.assertRaises(af.FormulaFieldError):
            af.widget_filters_clause(tree)

    def test_bad_field_in_dropped_node_is_never_compiled(self):
        """A node dropped by normalization (over-depth / bad conjunction) is
        never compiled, so its bad field can't raise. Documented, not a
        loophole: the node contributes no clause either way."""
        over_depth = group("and", A, group("and", group("and", group("and", cond("X}")))))
        bad_conj = group("and", A, group("xor", cond("X}", "eq", "a")))
        with self.assertLogs(af.logger, "WARNING"):
            self.assertEqual(af.widget_filters_clause(over_depth), A_SQL)
            self.assertEqual(af.widget_filters_clause(bad_conj), A_SQL)


class PersonalizeOutsideTreeTests(unittest.TestCase):
    def test_personalize_is_anded_outside_an_or_tree(self):
        formula, allowed = af.widget_formula(
            filters=group("or", A, B), personalize_enabled=True,
            personalize_column="Owner Email", email="Amy@RenPhil.org",
        )
        self.assertTrue(allowed)
        self.assertEqual(formula, f"AND(OR({A_SQL}, {B_SQL}), {PERSONALIZE_OWNER})")

    def test_personalize_outside_a_nested_tree(self):
        formula, allowed = af.widget_formula(
            filters=group("and", A, group("or", B, C)), personalize_enabled=True,
            personalize_column="Owner Email", email="amy@renphil.org",
        )
        self.assertTrue(allowed)
        self.assertEqual(
            formula,
            f"AND(AND({A_SQL}, OR({B_SQL}, {C_SQL})), {PERSONALIZE_OWNER})",
        )

    def test_personalize_with_empty_tree_is_personalize_alone(self):
        self.assertEqual(
            af.widget_formula(
                filters=group("or"), personalize_enabled=True,
                personalize_column="Owner Email", email="amy@renphil.org",
            ),
            (PERSONALIZE_OWNER, True),
        )

    def test_personalize_unavailable_still_blocks_with_a_tree(self):
        tree = group("or", A, B)
        for kwargs in (
            dict(personalize_column=None, email="amy@renphil.org"),
            dict(personalize_column="Owner Email", email=""),
            dict(personalize_column="Bad}Col", email="amy@renphil.org"),
        ):
            with self.subTest(**kwargs):
                self.assertEqual(
                    af.widget_formula(filters=tree, personalize_enabled=True, **kwargs),
                    (None, False),
                )

    def test_personalize_off_returns_tree_formula(self):
        self.assertEqual(
            af.widget_formula(filters=group("or", A, B)),
            (f"OR({A_SQL}, {B_SQL})", True),
        )


class CacheFingerprintTests(unittest.TestCase):
    """The fingerprint hashes the RAW stored value: a legacy widget's cache
    key is unchanged, and a tree is keyed as stored (no normalization)."""

    def _fp(self, filters):
        return AirtableService._widget_cache_fingerprint(
            widget_type="airtable", link="l", url="u", selected_columns=None,
            filters=filters, personalize_enabled=False, personalize_column=None,
        )

    def test_legacy_and_tree_are_stored_raw(self):
        legacy = [{"field": "Status", "operator": "eq", "value": "Active"}]
        self.assertEqual(self._fp(legacy)["filters"], legacy)
        self.assertEqual(self._fp(None)["filters"], [])
        tree = group(" OR ", A, "junk")
        self.assertEqual(self._fp(tree)["filters"], tree)


class ModelWideningTests(unittest.TestCase):
    TREE = group("or", A, group("and", B, C))
    LEGACY = [{"field": "Status", "operator": "eq", "value": "Active"}]

    def test_request_models_accept_both_shapes(self):
        for model in (AirtableEditorPreviewRequest, AirtableChartPreviewRequest):
            with self.subTest(model=model.__name__):
                self.assertEqual(model(filters=self.TREE).filters, self.TREE)
                self.assertEqual(model(filters=self.LEGACY).filters, self.LEGACY)
                self.assertIsNone(model(filters=None).filters)
                self.assertIsNone(model().filters)

    def test_request_models_reject_other_shapes(self):
        for model in (AirtableEditorPreviewRequest, AirtableChartPreviewRequest):
            with self.subTest(model=model.__name__):
                with self.assertRaises(Exception):
                    model(filters="Status = 1")
                with self.assertRaises(Exception):
                    model(filters=["junk"])

    def test_snapshot_response_accepts_both_shapes(self):
        base = dict(widget_type="airtable", base_id="app", table_id="tbl",
                    reason="shared_rows")
        self.assertEqual(
            AirtableWidgetIndexSnapshotResponse(**base, filters=self.TREE)
            .model_dump()["filters"],
            self.TREE,
        )
        self.assertEqual(
            AirtableWidgetIndexSnapshotResponse(**base, filters=self.LEGACY).filters,
            self.LEGACY,
        )
        self.assertEqual(AirtableWidgetIndexSnapshotResponse(**base).filters, [])


# ── index snapshot route (plan §1.3) ───────────────────────────────────────
SOURCE_URL = "https://airtable.com/appP9ziO5nV1LY7eS/tblj34ByiDh40US4x/viw6Ckhb93CGwRzQS"


def _bundle(data):
    return SimpleNamespace(
        config={"sourceUrl": SOURCE_URL, "personalizeEnabled": False,
                "personalizeColumn": None},
        pat="SAFE_TEST_PAT",
        data=dict(data),
        widget_type="airtable",
    )


def _service():
    return SimpleNamespace(
        fetch_widget_index_rows=AsyncMock(
            return_value=SimpleNamespace(
                fields=["Status"], rows=[{"id": "rec1", "Status": "Active"}],
                available=True,
            )
        )
    )


def _run_snapshot(bundle, service):
    """Same shape as tests/test_airtable_index_ingestion.py's pytest helper
    (which never runs here — no pytest installed)."""
    with patch.object(airtable_router, "get_airtable_component_bundle",
                      return_value=bundle):
        return asyncio.run(
            airtable_router.get_airtable_component_index_snapshot(
                link="component-link", _service_auth=None, db=object(),
                airtable_service=service,
            )
        )


class IndexSnapshotFilterPassThroughTests(unittest.TestCase):
    def test_tree_reaches_the_walk_and_the_response(self):
        tree = group("or", A, group("and", B, C))
        service = _service()
        result = _run_snapshot(_bundle({"filters": tree}), service)
        kwargs = service.fetch_widget_index_rows.await_args.kwargs
        self.assertEqual(kwargs["filters"], tree)
        self.assertEqual(result.filters, tree)
        self.assertTrue(result.available)
        # And the walk would compile the real tree, not an unfiltered []:
        self.assertEqual(
            af.widget_formula(filters=kwargs["filters"])[0],
            f"OR({A_SQL}, AND({B_SQL}, {C_SQL}))",
        )

    def test_legacy_list_unchanged(self):
        legacy = [{"field": "Status", "operator": "eq", "value": ["Active"]}]
        service = _service()
        result = _run_snapshot(_bundle({"filters": legacy}), service)
        self.assertEqual(service.fetch_widget_index_rows.await_args.kwargs["filters"], legacy)
        self.assertEqual(result.filters, legacy)

    def test_legacy_list_keeps_dict_only_cleanup(self):
        legacy = ["junk", {"field": "Status", "operator": "eq", "value": "Active"}]
        service = _service()
        result = _run_snapshot(_bundle({"filters": legacy}), service)
        self.assertEqual(result.filters, [legacy[1]])
        self.assertEqual(
            service.fetch_widget_index_rows.await_args.kwargs["filters"], [legacy[1]]
        )

    def test_other_shapes_become_empty_list(self):
        for raw in (None, "Status = 1", 42):
            service = _service()
            with self.subTest(raw=raw):
                result = _run_snapshot(_bundle({"filters": raw}), service)
                self.assertEqual(result.filters, [])
                self.assertIsNone(
                    service.fetch_widget_index_rows.await_args.kwargs["filters"]
                )

    def test_stored_tree_is_not_mutated(self):
        tree = group("or", A, B)
        before = copy.deepcopy(tree)
        _run_snapshot(_bundle({"filters": tree}), _service())
        self.assertEqual(tree, before)


if __name__ == "__main__":
    unittest.main()
