from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests import test_view_as as base  # also sets the env / SQLite JSONB shim

from app.db_v2.database import get_db_v2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.hub import HubV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.resource_grant import ResourceGrantV2
from app.db_v2.models.tab import TabV2
from app.dependencies import get_current_user
from app.main import app
from app.models.auth import UserInfo
from app.services.component_access_service import list_users_with_component_access

NOW = base.NOW


class ComponentAccessUsersTests(base._ViewAsDb):
    """hub -> nav -> tab -> grid -> component, with:
    * nav:       view  for (member, progA)  -> alice (lead/org), carol (lead/progA)
    * tab:       edit  for carol's email
    * component: edit  for bob's email
    * admin holds (hub_admin, All Scopes); nobody holds nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.db.add(HubV2(document_id="hub"))
        nav = NavTabV2(document_id="nav-1", slug="n1", title="N1", order=0, protected=False)
        self.db.add(nav)
        self.db.flush()
        tab = TabV2(document_id="tab-1", title="T1", order=0, access_control={}, locked=False, locked_by="", nav_tab_id=nav.id)
        self.db.add(tab)
        self.db.flush()
        grid = GridstackV2(document_id="tab-1", name="T1", settings={}, position=0, parent_id=None, parent_tab_id=tab.id)
        self.db.add(grid)
        self.db.flush()
        self.component = ComponentV2(link="tm", type="task_manager", props={}, gridstack_id=grid.id)
        self.db.add(self.component)
        self.db.flush()
        self.alice.first_name, self.alice.last_name, self.alice.personal_email = "Alice", "A", "alice@home.org"
        for grant in (
            dict(nav_tab_id=nav.id, role_id=self.role["member"].id, scope_id=self.scope["progA"].id, level="view"),
            dict(tab_id=tab.id, user_id=self.carol.id, level="edit"),
            dict(component_id=self.component.id, user_id=self.bob.id, level="edit"),
        ):
            self.db.add(ResourceGrantV2(created_at=NOW, **grant))
        self.db.commit()

    def _levels(self):
        return {u["name"]: u["access_level"] for u in list_users_with_component_access(self.db, self.component)}

    def test_levels_through_roles_scopes_direct_grants_and_ancestors(self) -> None:
        self.assertEqual(
            self._levels(),
            {"Alice": "view", "Carol": "edit", "Bob": "edit", "Admin": "edit"},
        )

    def test_returned_fields(self) -> None:
        alice = next(u for u in list_users_with_component_access(self.db, self.component) if u["name"] == "Alice")
        self.assertEqual(
            alice,
            {
                "id": self.alice.id,
                "first_name": "Alice",
                "last_name": "A",
                "name": "Alice",
                "work_email": "alice@renphil.org",
                "alias_email": None,
                "personal_email": "alice@home.org",
                "access_level": "view",
            },
        )

    def test_agrees_with_the_forward_fold(self) -> None:
        from app.services.access_visibility_service import compute_visibility

        levels = {u["id"]: u["access_level"] for u in list_users_with_component_access(self.db, self.component)}
        for user in (self.alice, self.bob, self.carol, self.nobody):
            result = compute_visibility(self.db, user.id)
            granted = result.is_granted("component", self.component.id)
            edit = result.verdict("component", self.component.id).edit
            expected = "edit" if edit else ("view" if granted else None)
            with self.subTest(user=user.name):
                self.assertEqual(levels.get(user.id), expected)

    def test_edit_fast_path_agrees_with_the_forward_fold(self) -> None:
        from app.services.access_visibility_service import compute_visibility, granted_single_node

        node = ("component", self.component.id)
        for user in (self.alice, self.bob, self.carol, self.nobody):
            result = compute_visibility(self.db, user.id)
            with self.subTest(user=user.name):
                self.assertEqual(
                    granted_single_node(self.db, user.id, node, is_admin=False, edit_only=True),
                    result.verdict(*node).edit,
                )
                self.assertEqual(
                    granted_single_node(self.db, user.id, node, is_admin=False),
                    result.is_granted(*node),
                )

    def test_deleted_users_are_excluded(self) -> None:
        self.bob.is_deleted = True
        self.db.commit()
        self.assertNotIn("Bob", self._levels())

    def test_router(self) -> None:
        app.dependency_overrides[get_db_v2] = lambda: self.db
        email = {"value": "alice@renphil.org"}
        app.dependency_overrides[get_current_user] = lambda: UserInfo(email=email["value"], name="x")
        try:
            client = TestClient(app)
            r = client.get(f"/v2/tabs/components/{self.component.id}/users")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["component_id"], self.component.id)
            self.assertEqual([u["name"] for u in body["users"]], ["Admin", "Alice", "Bob", "Carol"])

            self.assertEqual(client.get("/v2/tabs/components/99999/users").status_code, 404)
            email["value"] = "nobody@renphil.org"
            self.assertEqual(client.get(f"/v2/tabs/components/{self.component.id}/users").status_code, 404)
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
