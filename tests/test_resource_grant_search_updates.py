"""Focused coverage for Qdrant refresh receipts after an ACL mutation."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import resource_grant_search_updates


class _Tree:
    def __init__(self) -> None:
        self.roots: list[tuple[str, int]] = []

    def descendants(self, root: tuple[str, int]) -> set[tuple[str, int]]:
        self.roots.append(root)
        return {("component", 3), ("component", 8), ("tab", 11)}


class ResourceGrantSearchUpdatesTests(unittest.TestCase):
    def test_component_grant_reindexes_itself_and_descendant_components(self) -> None:
        tree = _Tree()
        with patch.object(resource_grant_search_updates, "build_node_tree", return_value=tree):
            updates = resource_grant_search_updates.affected_component_search_updates(
                object(), node_kind="component", node_id=42
            )

        self.assertEqual(tree.roots, [("component", 42)])
        self.assertEqual(
            updates,
            [
                {"component_id": 3, "action": "upsert"},
                {"component_id": 8, "action": "upsert"},
                {"component_id": 42, "action": "upsert"},
            ],
        )

    def test_parent_grant_reindexes_only_descendant_components(self) -> None:
        tree = _Tree()
        with patch.object(resource_grant_search_updates, "build_node_tree", return_value=tree):
            updates = resource_grant_search_updates.affected_component_search_updates(
                object(), node_kind="tab", node_id=11
            )

        self.assertEqual(tree.roots, [("tab", 11)])
        self.assertEqual(
            updates,
            [
                {"component_id": 3, "action": "upsert"},
                {"component_id": 8, "action": "upsert"},
            ],
        )