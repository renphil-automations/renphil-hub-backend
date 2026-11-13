"""Tests for the one piece of non-trivial logic in the exposure-delta survey:
the canonical comparison that decides whether a stored `access_control` is a
DEFAULT stamped at creation or something a person authored.

WHY THIS AND NOT THE REPORT. The rest of `scripts/survey_widget_exposure_delta`
is enumeration and printing, and testing printing pins formatting rather than
behaviour. This function is different: it is the one place the report can be
QUIETLY WRONG in a way a reader cannot see. `stored == DEFAULT_ACCESS_CONTROL`
looks obviously correct and is not — the groups hold LISTS, and list equality
is order-sensitive, so whether an ACL is recognised as a default would depend
on the order Postgres happened to serialise its JSON in. The failure is silent
in both directions: an authored ACL misreported as "a default nobody chose"
(so the owner drops a restriction somebody meant), or a default misreported as
hand-set (so the owner preserves one nobody ever chose).

No database. These are pure-function tests over dicts.
"""

from __future__ import annotations

import copy
import unittest

from app.services.tab_service import DEFAULT_ACCESS_CONTROL
from scripts.survey_widget_exposure_delta import (
    canonical_access_control,
    gates_anything,
    inert_role_entries,
    is_default_access_control,
    viewer_entries,
)


class TestCanonicalComparison(unittest.TestCase):
    def test_the_default_is_recognised_as_the_default(self):
        self.assertTrue(is_default_access_control(DEFAULT_ACCESS_CONTROL))

    def test_a_reordered_default_is_still_the_default(self):
        """The whole reason this function exists. Same grants, different list
        order — must compare equal."""
        shuffled = copy.deepcopy(DEFAULT_ACCESS_CONTROL)
        shuffled["viewers"]["roles"] = list(reversed(shuffled["viewers"]["roles"]))
        shuffled["admins"]["roles"] = list(reversed(shuffled["admins"]["roles"]))

        self.assertNotEqual(shuffled, DEFAULT_ACCESS_CONTROL)  # naive == disagrees
        self.assertTrue(is_default_access_control(shuffled))  # canonical does not

    def test_a_default_with_reordered_json_keys_is_still_the_default(self):
        reordered = {
            "viewers": copy.deepcopy(DEFAULT_ACCESS_CONTROL["viewers"]),
            "admins": copy.deepcopy(DEFAULT_ACCESS_CONTROL["admins"]),
        }
        self.assertTrue(is_default_access_control(reordered))

    def test_email_case_and_whitespace_do_not_make_it_hand_set(self):
        """`_user_can_view_widget` compares emails stripped and lower-cased, so
        an ACL differing only in case grants EXACTLY what the default grants."""
        variant = copy.deepcopy(DEFAULT_ACCESS_CONTROL)
        variant["viewers"]["users"] = [{"email": "  Automations@RenPhil.ORG "}]
        self.assertTrue(is_default_access_control(variant))

    def test_removing_one_role_makes_it_hand_set(self):
        variant = copy.deepcopy(DEFAULT_ACCESS_CONTROL)
        variant["viewers"]["roles"] = variant["viewers"]["roles"][:-1]
        self.assertFalse(is_default_access_control(variant))

    def test_adding_one_role_makes_it_hand_set(self):
        variant = copy.deepcopy(DEFAULT_ACCESS_CONTROL)
        variant["viewers"]["roles"] = variant["viewers"]["roles"] + [
            {"name": "Contractor", "scope": "Hub", "program": "", "function": ""}
        ]
        self.assertFalse(is_default_access_control(variant))

    def test_same_name_different_scope_is_not_collapsed(self):
        """Sorting by `name` alone would call these two ACLs identical. They
        grant different things — one is Hub-scoped and matches, the other is
        Function-scoped and matches nobody."""
        a = {"viewers": {"roles": [{"name": "Function Lead", "scope": "Hub"}], "users": []}}
        b = {"viewers": {"roles": [{"name": "Function Lead", "scope": "Function"}], "users": []}}
        self.assertNotEqual(canonical_access_control(a), canonical_access_control(b))

    def test_same_name_and_scope_different_function_is_not_collapsed(self):
        a = {
            "viewers": {
                "roles": [{"name": "Function Lead", "scope": "Function", "function": "Legal"}],
                "users": [],
            }
        }
        b = {
            "viewers": {
                "roles": [{"name": "Function Lead", "scope": "Function", "function": "Finance"}],
                "users": [],
            }
        }
        self.assertNotEqual(canonical_access_control(a), canonical_access_control(b))

    def test_missing_optional_keys_normalise_to_empty_strings(self):
        """Live rows omit `program`/`function` sometimes and carry `""` other
        times. Those are the same ACL and must not read as hand-set."""
        terse = {"viewers": {"roles": [{"name": "Hub Member", "scope": "Hub"}], "users": []}}
        verbose = {
            "viewers": {
                "roles": [
                    {"name": "Hub Member", "scope": "Hub", "program": "", "function": ""}
                ],
                "users": [],
            }
        }
        self.assertEqual(canonical_access_control(terse), canonical_access_control(verbose))

    def test_non_dict_input_passes_through(self):
        """SQL NULL, JSON null and a stray list all reach this function from
        live data. None of them may raise."""
        self.assertIsNone(canonical_access_control(None))
        self.assertEqual(canonical_access_control([]), [])
        self.assertEqual(canonical_access_control("nonsense"), "nonsense")
        self.assertFalse(is_default_access_control(None))
        self.assertFalse(is_default_access_control([]))

    def test_garbage_entries_inside_a_group_are_dropped_not_fatal(self):
        messy = {"viewers": {"roles": ["not-a-dict", {"name": "Hub Member", "scope": "Hub"}],
                             "users": [None, {"email": "a@b.c"}]}}
        canon = canonical_access_control(messy)
        self.assertEqual([r["name"] for r in canon["viewers"]["roles"]], ["Hub Member"])
        self.assertEqual([u["email"] for u in canon["viewers"]["users"]], ["a@b.c"])


class TestTheThreeCounterintuitiveRules(unittest.TestCase):
    """These mirror `tab_service`'s real semantics. If any of them ever
    changes, the report's headline counts change with it — so they are pinned
    here rather than only described in the script's docstring."""

    def test_an_empty_viewers_group_gates_nothing(self):
        self.assertFalse(gates_anything({"viewers": {"roles": [], "users": []}}))
        self.assertFalse(gates_anything({}))

    def test_a_populated_admins_group_alone_gates_nothing(self):
        """Rule 1: only `viewers` is ever read. This is the one that makes a
        reader believe a widget is restricted when it is open."""
        ac = {
            "admins": {"roles": [{"name": "Hub Admin", "scope": "Hub"}], "users": []},
            "viewers": {"roles": [], "users": []},
        }
        self.assertFalse(gates_anything(ac))

    def test_viewer_entries_never_returns_admins(self):
        ac = {
            "admins": {"roles": [{"name": "Hub Admin", "scope": "Hub"}], "users": [{"email": "x@y.z"}]},
            "viewers": {"roles": [], "users": []},
        }
        roles, users = viewer_entries(ac)
        self.assertEqual(roles, [])
        self.assertEqual(users, [])

    def test_a_scoped_role_entry_is_reported_inert(self):
        """Rule 3: Program/Function-scoped entries match NOBODY."""
        ac = {
            "viewers": {
                "roles": [
                    {"name": "Hub Member", "scope": "Hub"},
                    {"name": "Function Lead", "scope": "Function", "function": "test function"},
                    {"name": "Program Lead", "scope": "Program", "program": "test program"},
                ],
                "users": [],
            }
        }
        inert = inert_role_entries(ac)
        self.assertEqual(len(inert), 2)
        self.assertTrue(any("Function Lead" in i for i in inert))
        self.assertTrue(any("Program Lead" in i for i in inert))
        self.assertFalse(any("Hub Member" in i for i in inert))

    def test_a_hub_scoped_entry_is_not_inert(self):
        ac = {"viewers": {"roles": [{"name": "Hub Member", "scope": "Hub"}], "users": []}}
        self.assertEqual(inert_role_entries(ac), [])


class TestItAgreesWithTheRealPredicate(unittest.TestCase):
    """The script's classification helpers and `tab_service`'s predicate must
    not disagree — a disagreement would UNDERSTATE exposure, which is the
    dangerous direction (a widget reported open that is actually gated is
    merely noisy; one reported gated that is actually open is a hole nobody
    looks at again)."""

    def test_gates_anything_false_implies_everyone_passes_the_predicate(self):
        from app.services.tab_service import _user_can_view_widget

        for ac in (
            {"viewers": {"roles": [], "users": []}},
            {"admins": {"roles": [{"name": "Hub Admin", "scope": "Hub"}]}, "viewers": {}},
            {"viewers": {}},
        ):
            self.assertFalse(gates_anything(ac))
            self.assertTrue(_user_can_view_widget(ac, "anyone@example.invalid", []))
            self.assertTrue(_user_can_view_widget(ac, "anyone@example.invalid", ["Contractor"]))

    def test_an_all_inert_acl_admits_nobody_through_the_predicate(self):
        """A gating ACL made only of Program/Function entries reads like a
        grant and behaves like a comment."""
        from app.services.tab_service import _user_can_view_widget

        ac = {
            "viewers": {
                "roles": [
                    {"name": "Function Lead", "scope": "Function", "function": "test function"}
                ],
                "users": [],
            }
        }
        self.assertTrue(gates_anything(ac))
        self.assertEqual(len(inert_role_entries(ac)), 1)
        for role in ("Function Lead", "Hub Member", "Hub Admin", "Contractor"):
            self.assertFalse(
                _user_can_view_widget(ac, "someone@example.invalid", [role]),
                f"{role} unexpectedly matched an all-inert ACL",
            )


if __name__ == "__main__":
    unittest.main()
