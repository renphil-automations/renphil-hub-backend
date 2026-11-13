"""Wiring the read-time visibility triple into the tab-serving endpoints
(plan_access_control_algorithm_2026-08-27.md §8.1-8.4, §9) —
project_ac_enforcement_gap.md item 1.

`test_access_visibility.py` exhaustively tests the FOLD itself (`fold`,
`_fold_down`, `_reveal_ancestors`, `_apply_mirror_substitution`) as pure
functions over hand-built seed sets. This file does not repeat that — it
tests the WIRING: does `GET /v2/nav-tabs`, `GET /v2/tabs/root`,
`.../children`, `.../variants`, `.../{id}/workspace`, `.../{id}/content` and
`.../components/by-link/{link}/location` actually call the fold, resolve the
right node per gridstack (root vs. sub-grid), 404 a hidden node the same way
they 404 a missing one, filter canvas widgets, and merge the §5.2 triple —
through the REAL router, with a REAL JWT, exactly as a browser would hit it.

THE FIXTURE BUILDS NO ROLES, SCOPES OR ASSIGNMENTS. Every grant below is a
direct per-user grant (D5) — `resource_grant_service.grants_matching_pairs`
finds these independent of `effective_pairs`, confirmed by reading it before
writing a single test here — because what is under test is the wiring, not
role/scope matching (`test_resource_grants.py`,
`test_rbac_graph.py` already own that). The one exception is the Hub Admin
JWT bypass (`TestHubAdminBypassesTheFold`), which needs no DB row at all —
`is_hub_admin`'s branch 1 short-circuits on the JWT alone.

THE HUB ADMIN BYPASS IS THE REGRESSION THIS FILE EXISTS TO PIN. Without
`ViewerAccess.full_access`, wiring `compute_visibility` into these endpoints
would make every JWT-recognized Hub Admin who is not
`role_assignments`'s one populated row (roy.abdelnour@renphil.org, live
Neon, 2026-09-03) see a fully dark hub the moment this ships — found while
designing this change, not by a failing test, which is exactly why it gets
one now.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-tab-visibility-wiring-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.main import create_app  # noqa: E402
from tests._real_admin import seed_real_admin  # noqa: E402
from app.services.access_visibility_service import (  # noqa: E402
    GRIDSTACK_WIDGET_TYPE,
    MIRROR_WIDGET_TYPE,
)

HUB_ADMIN = "Hub Admin"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TabVisibilityWiringFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        app = create_app()

        # test_resource_grants_api.py's override, INCLUDING its
        # `finally: db.close()` — see that module's docstring for why the
        # one-liner other fixtures use would make a "nothing was created"
        # assertion pass vacuously under a StaticPool.
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

        self._build_tree()
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- node tree ------------------------------------------------------
    #
    #  hub -> nav1 -> tabA (+ variantA)         -> gA -> g_sub -> widget2
    #               -> tabB                     -> gB -> widget_b1, mirror1
    #
    # mirror1 targets widget1 (on tabA/gA), matching §3.4's own worked case.

    def _gridstack(self, tab: TabV2, *, parent: GridstackV2 | None = None) -> GridstackV2:
        # A ROOT gridstack shares its owning tab's document_id (create_tab_v2
        # does the same) — every tabs_v2.py route resolves `document_id`
        # straight to a GridstackV2 row, so this is load-bearing for the
        # router-level GETs this file exercises, not cosmetic.
        document_id = tab.document_id if parent is None else f"grid-sub-{tab.id}-{parent.id}"
        grid = GridstackV2(
            document_id=document_id,
            parent_id=parent.id if parent else None,
            parent_tab_id=tab.id,
        )
        self.db.add(grid)
        self.db.flush()
        rep = ComponentV2(
            link=f"rep-{grid.id}", type=GRIDSTACK_WIDGET_TYPE, gridstack_id=grid.id, current_grid_id=grid.id
        )
        self.db.add(rep)
        self.db.flush()
        return grid

    def _build_tree(self) -> None:
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(
            document_id="nav-1", slug="dashboard", title="Dashboard", protected=False
        )
        self.db.add(self.nav1)
        # A second, empty nav tab — project_ac_enforcement_gap.md's §6.8
        # nav-tab/hub write-gate classes below need a destination for
        # `move_tab_to_nav_tab` and a clean node for `delete_nav_tab` that
        # doesn't cascade into tabA/tabB. Harmless to every EXISTING test in
        # this file: nothing asserts the nav-tabs list exactly, only
        # membership (`assertIn`) or an explicit empty list for a caller
        # with zero grants (nav2, like nav1, stays invisible without one).
        self.nav2 = NavTabV2(
            document_id="nav-2", slug="programs", title="Programs", protected=False
        )
        self.db.add(self.nav2)
        self.db.flush()

        self.tabA = TabV2(document_id="tab-a", title="Root A", nav_tab_id=self.nav1.id)
        self.tabB = TabV2(document_id="tab-b", title="Root B", nav_tab_id=self.nav1.id)
        self.db.add_all([self.tabA, self.tabB])
        self.db.flush()

        self.variantA = TabV2(
            document_id="tab-a-v1", title="Variant 1", nav_tab_id=self.nav1.id, parent_tab_id=self.tabA.id
        )
        self.db.add(self.variantA)
        self.db.flush()

        self.gA = self._gridstack(self.tabA)
        self.gB = self._gridstack(self.tabB)
        self.gV = self._gridstack(self.variantA)
        self.g_sub = self._gridstack(self.tabA, parent=self.gA)

        self.widget1 = ComponentV2(link="widget-1", type="markdown", gridstack_id=self.gA.id)
        self.widget2 = ComponentV2(link="widget-2", type="markdown", gridstack_id=self.g_sub.id)
        self.widget_b1 = ComponentV2(link="widget-b1", type="markdown", gridstack_id=self.gB.id)
        self.mirror1 = ComponentV2(
            link="mirror-1",
            type=MIRROR_WIDGET_TYPE,
            gridstack_id=self.gB.id,
            props={"target_link": "widget-1"},
        )
        self.db.add_all([self.widget1, self.widget2, self.widget_b1, self.mirror1])
        self.db.flush()

        # The sub-grid's own representation component — resolve_gridstack_node's
        # non-root branch resolves g_sub's OWN identity to this row.
        self.subgrid_rep = (
            self.db.query(ComponentV2).filter(ComponentV2.current_grid_id == self.g_sub.id).first()
        )

        self.alice = UserV2(email="alice@renphil.org", has_signed_in=True, created_at=_now())
        self.bob = UserV2(email="bob@renphil.org", has_signed_in=True, created_at=_now())
        self.carol = UserV2(email="carol@renphil.org", has_signed_in=True, created_at=_now())
        self.db.add_all([self.alice, self.bob, self.carol])
        self.db.flush()

    # -- grants ----------------------------------------------------------

    def grant(self, *, user: UserV2, node: tuple[str, int], level: str = "view") -> None:
        """A direct per-user grant (D5) — no role, no scope, no assignment."""
        node_kind, node_id = node
        row = ResourceGrantV2(
            **{NODE_COLUMNS[node_kind]: node_id}, user_id=user.id, level=level, created_at=_now()
        )
        self.db.add(row)
        self.db.commit()

    # -- auth --------------------------------------------------------------

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": roles or [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def admin(self) -> dict:
        """A REAL Hub Admin. Phase 3 (2026-10-12) removed the JWT branch the
        module docstring describes: `is_hub_admin` is now the (disabled)
        bootstrap list OR a `(hub_admin, universal)` assignment. So the
        first use seeds that assignment (`tests/_real_admin.py`, own
        session). Lazy, so the "no roles, scopes or assignments" fixture
        still holds for every test that never acts as admin. What
        `TestHubAdminBypassesTheFold` pins is unchanged: `full_access`
        bypasses the fold with ZERO `resource_grants` rows."""
        seed_real_admin(self._session_factory, "admin@renphil.org")
        return self._auth("admin@renphil.org", [HUB_ADMIN])

    def user(self, hub_user: UserV2) -> dict:
        return self._auth(hub_user.email, [])

    # -- calls ------------------------------------------------------------

    def get(self, path: str, headers: dict):
        return self.client.get(path, headers=headers)

    def put(self, path: str, headers: dict, json: dict | None = None):
        return self.client.put(path, json=json or {}, headers=headers)

    def post(self, path: str, headers: dict, json: dict | None = None):
        return self.client.post(path, json=json or {}, headers=headers)

    def delete(self, path: str, headers: dict):
        return self.client.delete(path, headers=headers)

    # -- edit-session tokens ------------------------------------------------
    #
    # plan_lock_propagation_2026-09-08.md §5.2 added `require_live_session`
    # to every endpoint this file's write-side classes already exercised, so
    # every pre-existing "succeeds" test below now ALSO needs a live lock
    # token on the node `require_live_session` actually checks — which is
    # not always the same node the AC `edit(...)` grant above it targets
    # (e.g. deleting a nav tab gates PERMISSION on `edit(hub)` but SESSION on
    # the nav tab itself — the hub has no lock node at all, §9 item 1).
    # Service-level, not HTTP: nav tabs have no `/lock` route yet (phase 5),
    # so this is the only way to get a token for one — same pattern
    # `test_tab_locking.py`'s `TestEditSessionTokens` already uses.

    def _edit_token(self, node: tuple[str, int], holder: str) -> str:
        from app.services.edit_lock_service import acquire

        return acquire(self.db, node, holder).token

    def _edit_tokens(self, *pairs: tuple[tuple[str, int], str]) -> str:
        return ",".join(self._edit_token(node, holder) for node, holder in pairs)


class TestHubAdminBypassesTheFold(TabVisibilityWiringFixture):
    """The regression this whole file exists to pin — see module docstring."""

    def test_admin_sees_every_nav_tab_and_root_tab_fully_granted(self) -> None:
        resp = self.get("/v2/nav-tabs", headers=self.admin)
        self.assertEqual(resp.status_code, 200)
        nav_ids = {row["id"] for row in resp.json()["data"]}
        self.assertIn(self.nav1.id, nav_ids)
        nav_row = next(r for r in resp.json()["data"] if r["id"] == self.nav1.id)
        self.assertEqual((nav_row["view"], nav_row["edit"], nav_row["revealed"]), (True, True, False))

        resp = self.get("/v2/tabs/root", headers=self.admin)
        ids = {row["id"] for row in resp.json()["data"]}
        self.assertEqual(ids, {self.tabA.id, self.tabB.id})
        for row in resp.json()["data"]:
            self.assertEqual((row["view"], row["edit"], row["revealed"]), (True, True, False))

    def test_admin_workspace_content_is_never_redacted(self) -> None:
        resp = self.get("/v2/tabs/tab-a/workspace", headers=self.admin)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"], body["revealed"]), (True, True, False))
        widgets = body["page_content"]["content"]["widgets"]
        self.assertEqual(widgets[str(self.widget1.id)]["type"], "markdown")

    def test_admin_sees_hidden_component_via_deep_link(self) -> None:
        resp = self.get("/v2/tabs/components/by-link/widget-1/location", headers=self.admin)
        self.assertEqual(resp.status_code, 200)


class TestDarkByDefault(TabVisibilityWiringFixture):
    """D2: unset access control matches no one — the hub's default state."""

    def test_ordinary_user_with_no_grants_sees_nothing(self) -> None:
        resp = self.get("/v2/nav-tabs", headers=self.user(self.bob))
        self.assertEqual(resp.json()["data"], [])

        resp = self.get("/v2/tabs/root", headers=self.user(self.bob))
        self.assertEqual(resp.json()["data"], [])

    def test_workspace_404s_not_403s_for_an_invisible_tab(self) -> None:
        resp = self.get("/v2/tabs/tab-a/workspace", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 404)

    def test_children_of_an_invisible_tab_404_rather_than_leak_a_count(self) -> None:
        resp = self.get("/v2/tabs/tab-a/children", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 404)

    def test_deep_link_to_an_unreachable_component_is_404_not_403(self) -> None:
        resp = self.get("/v2/tabs/components/by-link/widget-1/location", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 404)
        self.assertNotEqual(resp.status_code, 403)


class TestASingleGrantRevealsTheRootPath(TabVisibilityWiringFixture):
    def setUp(self) -> None:
        super().setUp()
        self.grant(user=self.alice, node=("tab", self.tabA.id), level="view")

    def test_nav_tab_is_revealed_not_granted(self) -> None:
        resp = self.get("/v2/nav-tabs", headers=self.user(self.alice))
        row = next(r for r in resp.json()["data"] if r["id"] == self.nav1.id)
        self.assertEqual((row["view"], row["edit"], row["revealed"]), (True, False, True))

    def test_granted_tab_appears_but_the_sibling_tab_does_not(self) -> None:
        resp = self.get("/v2/tabs/root", headers=self.user(self.alice))
        rows = {r["id"]: r for r in resp.json()["data"]}
        self.assertIn(self.tabA.id, rows)
        self.assertNotIn(self.tabB.id, rows)
        self.assertEqual(
            (rows[self.tabA.id]["view"], rows[self.tabA.id]["edit"], rows[self.tabA.id]["revealed"]),
            (True, False, False),
        )

    def test_granted_tab_content_is_not_redacted(self) -> None:
        resp = self.get("/v2/tabs/tab-a/workspace", headers=self.user(self.alice))
        self.assertEqual(resp.status_code, 200)
        widgets = resp.json()["data"]["page_content"]["content"]["widgets"]
        self.assertEqual(widgets[str(self.widget1.id)]["type"], "markdown")

    def test_subgrid_inherits_the_grant_and_appears_as_a_child(self) -> None:
        resp = self.get("/v2/tabs/tab-a/children", headers=self.user(self.alice))
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["view"], rows[0]["revealed"]), (True, False))

    def test_edit_was_not_implied_by_a_view_grant(self) -> None:
        resp = self.get("/v2/tabs/tab-a/workspace", headers=self.user(self.alice))
        self.assertFalse(resp.json()["data"]["edit"])


class TestEditImpliesView(TabVisibilityWiringFixture):
    def test_an_edit_only_grant_yields_view_true_too(self) -> None:
        self.grant(user=self.carol, node=("tab", self.tabA.id), level="edit")
        resp = self.get("/v2/tabs/tab-a/workspace", headers=self.user(self.carol))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"]), (True, True))


class TestAComponentGrantRevealsItsTabAsAShell(TabVisibilityWiringFixture):
    """§5.2's worked case: revealed but not granted — content filtered to
    nothing but the one component that earned the reveal."""

    def setUp(self) -> None:
        super().setUp()
        self.grant(user=self.bob, node=("component", self.widget_b1.id), level="view")

    def test_tab_b_is_revealed_not_granted(self) -> None:
        resp = self.get("/v2/tabs/tab-b/workspace", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"], body["revealed"]), (True, False, True))

    def test_the_granted_widget_shows_and_its_sibling_is_redacted(self) -> None:
        resp = self.get("/v2/tabs/tab-b/workspace", headers=self.user(self.bob))
        widgets = resp.json()["data"]["page_content"]["content"]["widgets"]
        self.assertEqual(widgets[str(self.widget_b1.id)]["type"], "markdown")
        # The mirror is the OTHER top-level widget on tab B and was not
        # separately granted — redacted, same sentinel shape as today's
        # tab_service.filter_widget_content_for_user.
        self.assertEqual(widgets[str(self.mirror1.id)], {"type": "restricted", "data": None})

    def test_root_tabs_list_shows_tab_b_as_a_shell(self) -> None:
        resp = self.get("/v2/tabs/root", headers=self.user(self.bob))
        row = next(r for r in resp.json()["data"] if r["id"] == self.tabB.id)
        self.assertEqual((row["view"], row["revealed"]), (True, True))
        self.assertNotIn(self.tabA.id, {r["id"] for r in resp.json()["data"]})


class TestMirrorConjunction(TabVisibilityWiringFixture):
    """§3.4: a mirror is granted only if BOTH its own position and its
    target are — tested through the real endpoint, not the pure fold."""

    def test_position_granted_but_target_not_redacts_the_mirror(self) -> None:
        # Whole of tab B granted (so the mirror's OWN position is granted by
        # the ordinary fold) — but nothing on tab A/widget-1, the mirror's
        # target, is granted at all.
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="view")
        resp = self.get("/v2/tabs/tab-b/workspace", headers=self.user(self.bob))
        widgets = resp.json()["data"]["page_content"]["content"]["widgets"]
        # An ordinary widget on the same, fully-granted tab shows fine —
        # proves the redaction below is the mirror rule, not a blanket one.
        self.assertEqual(widgets[str(self.widget_b1.id)]["type"], "markdown")
        self.assertEqual(widgets[str(self.mirror1.id)], {"type": "restricted", "data": None})

    def test_both_halves_granted_shows_the_mirror(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="view")
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="view")
        resp = self.get("/v2/tabs/tab-b/workspace", headers=self.user(self.bob))
        widgets = resp.json()["data"]["page_content"]["content"]["widgets"]
        self.assertEqual(widgets[str(self.mirror1.id)]["type"], MIRROR_WIDGET_TYPE)


class TestARevealedSbnRootRendersAsAShell(TabVisibilityWiringFixture):
    """plan_component_locking_and_sbn_2026-09-17.md §5.5 (finding 9). A
    Super Block Note root is the one widget type that is itself a container
    of further AC nodes: a viewer whose only grant is sub-tab `s1` makes the
    root REVEALED (an ancestor of a seed) but never GRANTED, and
    `_apply_visibility_to_content` — keyed on `is_granted` — used to redact
    the whole widget to the `restricted` sentinel, so the widget never
    mounted and `s1` was unreachable from the canvas. Now that one type
    keeps its entry as a shell (`edit: False`, `revealed: True`, `node_id`
    set) and the SBN service's own §5.2 reveal logic takes over.

    Tree added on top of the shared fixture: tab B's canvas gains
    `sbn_root` (type `super_block_note`) with sub-tab `s1` beneath it, and
    `mirror_s1` — a mirror on tab B whose target is `s1`."""

    def setUp(self) -> None:
        super().setUp()
        self.sbn_root = ComponentV2(
            link="sbn-root", title="Notes", type="super_block_note", gridstack_id=self.gB.id
        )
        self.db.add(self.sbn_root)
        self.db.flush()
        self.s1 = ComponentV2(
            link="sbn-s1",
            title="s1",
            type="block_note",
            gridstack_id=self.gB.id,
            super_blocknote_id=self.sbn_root.id,
        )
        self.mirror_s1 = ComponentV2(
            link="mirror-s1",
            type=MIRROR_WIDGET_TYPE,
            gridstack_id=self.gB.id,
            props={"target_link": "sbn-s1"},
        )
        self.db.add_all([self.s1, self.mirror_s1])
        self.db.commit()

    def _widgets_for(self, user: UserV2) -> dict:
        resp = self.get("/v2/tabs/tab-b/workspace", headers=self.user(user))
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["data"]["page_content"]["content"]["widgets"]

    def test_a_viewer_granted_only_a_sub_tab_receives_the_root_as_a_shell_not_the_sentinel(self) -> None:
        self.grant(user=self.bob, node=("component", self.s1.id), level="view")
        widgets = self._widgets_for(self.bob)
        root_entry = widgets[str(self.sbn_root.id)]
        self.assertEqual(root_entry["type"], "super_block_note")
        self.assertEqual(root_entry["link"], "sbn-root")
        self.assertIs(root_entry["revealed"], True)
        self.assertIs(root_entry["edit"], False)
        self.assertEqual(root_entry["node_kind"], "component")
        self.assertEqual(root_entry["node_id"], self.sbn_root.id)
        # The ungranted ORDINARY sibling on the same canvas is still the
        # sentinel — the shell is for the container type only.
        self.assertEqual(widgets[str(self.widget_b1.id)], {"type": "restricted", "data": None})

    def test_a_viewer_granted_nothing_under_the_root_still_receives_the_sentinel(self) -> None:
        """Revealed is what earns the shell — a root with no granted
        descendant is not revealed, and stays redacted exactly as before."""
        self.grant(user=self.bob, node=("component", self.widget_b1.id), level="view")
        widgets = self._widgets_for(self.bob)
        self.assertEqual(widgets[str(self.sbn_root.id)], {"type": "restricted", "data": None})

    def test_a_viewer_granted_the_root_itself_receives_it_granted_not_as_a_shell(self) -> None:
        self.grant(user=self.bob, node=("component", self.sbn_root.id), level="view")
        widgets = self._widgets_for(self.bob)
        root_entry = widgets[str(self.sbn_root.id)]
        self.assertEqual(root_entry["type"], "super_block_note")
        self.assertNotIn("revealed", root_entry)
        self.assertEqual(root_entry["node_id"], self.sbn_root.id)

    def test_a_conjunction_failing_mirror_of_a_sub_tab_still_receives_the_sentinel(self) -> None:
        """The shell is `super_block_note`-only on purpose: a mirror's
        `visible` is deliberately left untouched by
        `_apply_mirror_substitution` when its conjunction fails, so reading
        `.view` for it would show it in full. Bob is granted tab B (the
        mirror's POSITION, and the root and s1 with it) — but the mirror's
        TARGET is... also s1, which IS granted here, so make the target
        fail instead: grant only the mirror's own row. Position granted,
        target (s1) not → sentinel, same as `TestMirrorConjunction`."""
        self.grant(user=self.bob, node=("component", self.mirror_s1.id), level="view")
        widgets = self._widgets_for(self.bob)
        self.assertEqual(widgets[str(self.mirror_s1.id)], {"type": "restricted", "data": None})
        # And the SBN root, with nothing granted beneath it, is the sentinel
        # too — a mirror of a sub-tab does not reveal the sub-tab's root.
        self.assertEqual(widgets[str(self.sbn_root.id)], {"type": "restricted", "data": None})

    def test_the_shell_reaches_the_sbn_workspace_the_viewer_was_actually_granted(self) -> None:
        """End to end, the reason for the fix: the shell is what lets
        `SuperBlockNoteWidget` mount and fetch its workspace, where the
        SBN service's own reveal logic serves `s1`."""
        self.grant(user=self.bob, node=("component", self.s1.id), level="view")
        resp = self.get("/v2/sbn/sbn-root/workspace", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"], body["revealed"]), (True, False, True))
        self.assertEqual([c["documentId"] for c in body["children"]], ["sbn-s1"])


class TestNoContentEnforcementRegressedForInternalCallers(TabVisibilityWiringFixture):
    """gridstack_service functions called WITHOUT `access` (mutation-response
    echoes — `get_tab_workspace_v2` called internally by `update_tab_by_
    document_id_v2` to build ITS OWN response) must behave exactly as
    before: no triple, no filtering. This claim is orthogonal to
    project_ac_enforcement_gap.md item 2's write-side `edit(n)` gate (below,
    and in the classes that follow this one) — bob needs a real edit grant
    now to reach a 200 at all, but the response shape assertion is
    unchanged from what this class always tested."""

    def test_update_tab_metadata_response_carries_no_triple(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.put(
            "/v2/tabs/tab-a",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "Root A renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertIsNone(body.get("view"))
        self.assertIsNone(body.get("edit"))
        self.assertIsNone(body.get("revealed"))


# ---------------------------------------------------------------------------
# project_ac_enforcement_gap.md item 2 — the WRITE-side gate. Each class
# below exercises one row of plan §6.3's table through the real router,
# mirroring how the classes above exercise the READ side: an ungranted
# caller gets the same 404 a missing document_id would (§9's fail-closed
# convention, carried to writes), a caller who can VIEW but not EDIT gets
# 403 (their own request already proves the node exists), and a properly
# granted caller succeeds. `edit(parent(n))`-gated operations additionally
# pin that edit on the CHILD alone is not enough — the fold's upward reveal
# makes the parent VISIBLE (403), never invisible (404), which is itself
# worth distinguishing from the zero-grant case.
# ---------------------------------------------------------------------------


class TestCreateTabRequiresEditOnParent(TabVisibilityWiringFixture):
    """§6.3 "create a child of n → n", both create shapes: a ROOT tab under
    a nav tab, and a SUB-TAB under an existing gridstack."""

    def test_root_create_is_refused_with_no_grant(self) -> None:
        resp = self.post(
            "/v2/tabs/",
            headers=self.user(self.bob),
            json={"title": "New Root", "navTabDocumentId": "nav-1"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_root_create_is_forbidden_with_view_only_on_the_nav_tab(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="view")
        resp = self.post(
            "/v2/tabs/",
            headers=self.user(self.bob),
            json={"title": "New Root", "navTabDocumentId": "nav-1"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_root_create_succeeds_with_edit_on_the_nav_tab(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        token = self._edit_token(("nav_tab", self.nav1.id), self.bob.email)
        resp = self.post(
            "/v2/tabs/",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "New Root", "navTabDocumentId": "nav-1"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assert_echo_is_editable(resp)

    def assert_echo_is_editable(self, resp) -> None:
        """plan_login_airtable_removal_2026-10-31.md Step 3: a create echo
        carries the triple, computed AFTER the insert — the request's own
        access snapshot predates the new node and would call it invisible."""
        body = resp.json()["data"] if "data" in resp.json() else resp.json()
        self.assertIs(body.get("view"), True, body)
        self.assertIs(body.get("edit"), True, body)

    def test_sub_tab_create_is_refused_with_no_grant(self) -> None:
        resp = self.post(
            "/v2/tabs/",
            headers=self.user(self.bob),
            json={"title": "New Sub", "parentDocumentId": "tab-a"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_sub_tab_create_succeeds_with_edit_on_the_parent_tab(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.post(
            "/v2/tabs/",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "New Sub", "parentDocumentId": "tab-a"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assert_echo_is_editable(resp)


class TestCreateVariantAndReorderVariantsGateOnTheParentTab(TabVisibilityWiringFixture):
    """project_ac_enforcement_gap.md item 2 §2's resolved reading, worked
    out from the tree structure rather than asked: variant CREATION is
    "create a child of n" (n = the parent root tab); variant REORDER is
    "reorder n → parent(n)" where each variant's parent is that SAME root
    tab. Both operations therefore gate on the identical node — pinned here
    together rather than in two separate classes."""

    def test_create_variant_is_refused_with_no_grant(self) -> None:
        resp = self.post(
            "/v2/tabs/tab-a/variants", headers=self.user(self.bob), json={"title": "Variant 2"}
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_create_variant_succeeds_with_edit_on_the_parent_tab(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.post(
            "/v2/tabs/tab-a/variants",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "Variant 2"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"] if "data" in resp.json() else resp.json()
        # Same post-insert triple as TestCreateTabRequiresEditOnParent's echoes.
        self.assertIs(body.get("view"), True, body)
        self.assertIs(body.get("edit"), True, body)

    def test_reorder_variants_is_refused_with_no_grant(self) -> None:
        resp = self.put(
            "/v2/tabs/tab-a/variants/reorder",
            headers=self.user(self.bob),
            json={"orderedDocumentIds": ["tab-a-v1"]},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_reorder_variants_succeeds_with_edit_on_the_parent_tab(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        resp = self.put(
            "/v2/tabs/tab-a/variants/reorder",
            headers=self.user(self.bob),
            json={"orderedDocumentIds": ["tab-a-v1"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestUpdateContentRequiresEditOnN(TabVisibilityWiringFixture):
    """§6.3 "change a canvas, widget config, or text → n" — the whole-canvas
    save, gated the same way `update_component_content` (tested exhaustively
    in test_component_content_api.py) now is; the two "gain it together"
    per that function's own docstring."""

    def test_refused_with_no_grant(self) -> None:
        resp = self.put(
            "/v2/tabs/tab-a/content", headers=self.user(self.bob), json={"content": None}
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_forbidden_with_view_only(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="view")
        resp = self.put(
            "/v2/tabs/tab-a/content", headers=self.user(self.bob), json={"content": None}
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_n(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.put(
            "/v2/tabs/tab-a/content",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"content": None},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_response_content_carries_the_triple_for_saved_widgets(self) -> None:
        """Regression pin: `update_tab_content_v2` used to echo the raw
        write back, never running it through `_apply_visibility_to_content`
        the way every GET path does — so a saved widget's `node_id` came
        back `None`, silently disabling the frontend's per-widget "Manage
        access"/"Enter edit mode" affordances (both gated on it,
        TabGridCanvas.tsx) until the next reload. `_apply_visibility_to_
        content` requires a full `schemaVersion: 2` shape, so this save
        (re-saving the fixture's own widget1) exercises the exact same
        request shape the frontend's canvas Save sends."""
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        widget_key = str(self.widget1.id)
        resp = self.put(
            "/v2/tabs/tab-a/content",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={
                "content": {
                    "schemaVersion": 2,
                    "layout": [
                        {"id": widget_key, "x": 0, "y": 0, "w": 4, "h": 2, "type": "markdown"}
                    ],
                    "widgets": {
                        widget_key: {"type": "markdown", "data": {"markdown": "hello"}},
                    },
                }
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        widget = resp.json()["data"]["content"]["widgets"][widget_key]
        self.assertEqual(widget["edit"], True)
        self.assertEqual(widget["node_kind"], "component")
        self.assertEqual(widget["node_id"], self.widget1.id)


class TestMirrorTargetLinkRequiresViewOnTheTarget(TabVisibilityWiringFixture):
    """Defense in depth alongside the paste-link resolver's own gate
    (test_component_by_link_api.py's `TestPasteLinkAccessControl`): even a
    hand-crafted canvas-save payload that never went through
    `GET .../components/by-link/{link}` at all must not be able to wire a
    NEW mirror to a target the acting editor cannot view. `edit(n)` on the
    canvas being saved (checked once, up front, same as every other content
    write) is a DIFFERENT permission from being able to view the mirror's
    TARGET — matches this codebase's existing client-hides/server-re-checks
    pattern for mirror-of-mirror cycle prevention (`test_a_mirror_still_
    cannot_be_resolved`), just for a different rule.

    Bob holds `edit` on tab B (the mirror's own position, where the save is
    happening) throughout every test below — only his access to tab A /
    widget-1 (the mirror's TARGET) varies."""

    def _save_new_mirror(self, headers: dict, *, target_link: str):
        token = self._edit_token(("tab", self.tabB.id), self.bob.email)
        return self.put(
            "/v2/tabs/tab-b/content",
            headers={**headers, "X-Edit-Tokens": token},
            json={
                "content": {
                    "schemaVersion": 2,
                    "layout": [{"id": "new-mirror", "x": 0, "y": 0, "w": 6, "h": 6}],
                    "widgets": {
                        "new-mirror": {
                            "type": MIRROR_WIDGET_TYPE,
                            "data": {"targetLink": target_link},
                        }
                    },
                }
            },
        )

    def test_refused_when_the_target_is_not_viewable(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        resp = self._save_new_mirror(self.user(self.bob), target_link="widget-1")
        self.assertEqual(resp.status_code, 404, resp.text)

        # Fail-closed on the WHOLE save, not just the offending widget — and
        # nothing from the attempt persists (the function's own
        # `except Exception: db.rollback(); raise` at the end covers this
        # raise too, since NodeNotViewableError is an Exception subclass).
        self.db.expire_all()
        self.assertIsNotNone(
            self.db.query(ComponentV2).filter(ComponentV2.link == "widget-b1").first(),
            "a refused save must not have deleted the pre-existing sibling",
        )

    def test_forbidden_is_not_used_here_either(self) -> None:
        """Same §9 reasoning as the paste-link resolver: a target the caller
        cannot even VIEW must 404, not 403 — 403 would confirm it exists."""
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        resp = self._save_new_mirror(self.user(self.bob), target_link="widget-1")
        self.assertNotEqual(resp.status_code, 403)

    def test_succeeds_once_the_target_is_at_least_viewable(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="view")
        resp = self._save_new_mirror(self.user(self.bob), target_link="widget-1")
        self.assertEqual(resp.status_code, 200, resp.text)

        self.db.expire_all()
        new_mirror = (
            self.db.query(ComponentV2)
            .filter(ComponentV2.gridstack_id == self.gB.id, ComponentV2.type == MIRROR_WIDGET_TYPE)
            .first()
        )
        self.assertIsNotNone(new_mirror)
        self.assertEqual((new_mirror.props or {}).get("target_link"), "widget-1")

    def test_a_dangling_target_link_is_unaffected(self) -> None:
        """Not this guard's job — an unresolvable link is left to the
        existing dangling-target handling, unchanged."""
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        resp = self._save_new_mirror(self.user(self.bob), target_link="no-such-link")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_hub_admin_may_target_anything(self) -> None:
        token = self._edit_token(("tab", self.tabB.id), "admin@renphil.org")
        resp = self.put(
            "/v2/tabs/tab-b/content",
            headers={**self.admin, "X-Edit-Tokens": token},
            json={
                "content": {
                    "schemaVersion": 2,
                    "layout": [{"id": "new-mirror", "x": 0, "y": 0, "w": 6, "h": 6}],
                    "widgets": {
                        "new-mirror": {
                            "type": MIRROR_WIDGET_TYPE,
                            "data": {"targetLink": "widget-1"},
                        }
                    },
                }
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestUpdateTabMetadataSplitsRenameFromReorder(TabVisibilityWiringFixture):
    """§11.2 (owner decision, 2026-09-04): rename is `edit(n)`, matching
    §6.3's table literally. The `order` field is a reorder in disguise
    (`reorder_tabs_by_document_id_v2`'s own comment: root order is actually
    persisted through THIS endpoint) and stays `edit(parent(n))` — the two
    fields on the same PUT do NOT require the same grant, which is the
    point of every test below."""

    def test_rename_succeeds_with_edit_on_n_alone(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.put(
            "/v2/tabs/tab-a",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "Renamed A"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_order_change_is_forbidden_with_edit_on_n_alone(self) -> None:
        """Edit on tabA reveals nav1 (the fold's upward reveal, §5.1) but
        does not GRANT edit on it — 403, not 404: this caller can see nav1
        exists, they simply may not rearrange its children."""
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        resp = self.put("/v2/tabs/tab-a", headers=self.user(self.bob), json={"order": 5})
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_order_change_succeeds_with_edit_on_the_nav_tab(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        resp = self.put("/v2/tabs/tab-a", headers=self.user(self.bob), json={"order": 5})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_a_variants_order_change_gates_on_its_parent_tab_not_the_nav_tab(self) -> None:
        """Same resolved reading as the create/reorder-variants class above,
        pinned again here since `update_tab_by_document_id_v2` is a THIRD
        code path that touches a variant's order: edit on tabA (variantA's
        true parent) is sufficient on its own — no nav-tab-level grant
        needed."""
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        resp = self.put("/v2/tabs/tab-a-v1", headers=self.user(self.bob), json={"order": 1})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestMoveTabRequiresEditOnBothParents(TabVisibilityWiringFixture):
    """project_ac_enforcement_gap.md item 2's judgement call: §6.3's table
    states ONE parent for delete/reorder, but a move has two — this session
    requires edit on both the OLD parent (content is leaving it) and the
    NEW one (content is arriving in it), the same bar a file-system move
    would set for source and destination directories. `g_sub` (a sub-grid
    under tabA) moves to become a sub-grid under tabB."""

    def test_refused_with_no_grants_at_all(self) -> None:
        resp = self.put(
            f"/v2/tabs/{self.g_sub.document_id}/move",
            headers=self.user(self.bob),
            json={"newParentDocumentId": "tab-b"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_refused_when_only_the_old_parent_is_editable(self) -> None:
        """Tab B (the new parent) has no grant at all here — fully
        invisible to bob — so this is 404 (cannot even see the
        destination), not 403."""
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        resp = self.put(
            f"/v2/tabs/{self.g_sub.document_id}/move",
            headers=self.user(self.bob),
            json={"newParentDocumentId": "tab-b"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_succeeds_with_edit_on_both_parents(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        tokens = self._edit_tokens(
            (("tab", self.tabA.id), self.bob.email), (("tab", self.tabB.id), self.bob.email)
        )
        resp = self.put(
            f"/v2/tabs/{self.g_sub.document_id}/move",
            headers={**self.user(self.bob), "X-Edit-Tokens": tokens},
            json={"newParentDocumentId": "tab-b"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestDeleteTabRequiresEditOnParent(TabVisibilityWiringFixture):
    """§6.3 "delete n → parent(n)", exercised for all three shapes a
    document_id can address: a root tab (parent = nav tab), a variant
    (parent = the tab it varies — a separate axis from the gridstack tree,
    §3.1), and a sub-grid (parent = whatever node owns its own parent
    gridstack)."""

    def test_deleting_a_root_tab_is_forbidden_with_edit_on_itself_alone(self) -> None:
        """Edit on tabB reveals nav1 but does not grant edit on it — 403."""
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        resp = self.delete("/v2/tabs/tab-b", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_deleting_a_root_tab_succeeds_with_edit_on_its_nav_tab(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        token = self._edit_token(("nav_tab", self.nav1.id), self.bob.email)
        resp = self.delete("/v2/tabs/tab-b", headers={**self.user(self.bob), "X-Edit-Tokens": token})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_deleting_a_variant_gates_on_its_parent_tab(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.delete("/v2/tabs/tab-a-v1", headers={**self.user(self.bob), "X-Edit-Tokens": token})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_deleting_a_sub_grid_is_forbidden_with_edit_on_itself_alone(self) -> None:
        """g_sub's OWN node is its representation component — edit there
        satisfies `edit(n)`, but delete needs `edit(parent(n))`, i.e. tabA.
        Edit on g_sub reveals tabA (its parent) but does not grant edit on
        it, so this is 403."""
        self.grant(user=self.bob, node=("component", self.subgrid_rep.id), level="edit")
        resp = self.delete(f"/v2/tabs/{self.g_sub.document_id}", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_deleting_a_sub_grid_succeeds_with_edit_on_the_owning_tab(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        token = self._edit_token(("tab", self.tabA.id), self.bob.email)
        resp = self.delete(
            f"/v2/tabs/{self.g_sub.document_id}", headers={**self.user(self.bob), "X-Edit-Tokens": token}
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestReorderTabsRequiresEditOnTheSharedParent(TabVisibilityWiringFixture):
    """§6.3 "reorder n → parent(n)", for the root-level batch shape (the
    sub-grid-level shape shares `resolve_gridstack_parent_node` with
    `move`/`delete` above, already covered there)."""

    def test_refused_with_edit_on_the_tabs_themselves_but_not_the_nav_tab(self) -> None:
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        self.grant(user=self.bob, node=("tab", self.tabB.id), level="edit")
        resp = self.put(
            "/v2/tabs/reorder",
            headers=self.user(self.bob),
            json={
                "items": [
                    {"documentId": "tab-a", "order": 1},
                    {"documentId": "tab-b", "order": 0},
                ]
            },
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_the_shared_nav_tab(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        resp = self.put(
            "/v2/tabs/reorder",
            headers=self.user(self.bob),
            json={
                "items": [
                    {"documentId": "tab-a", "order": 1},
                    {"documentId": "tab-b", "order": 0},
                ]
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestHubAdminBypassesEveryWriteGate(TabVisibilityWiringFixture):
    """The same regression `TestHubAdminBypassesTheFold` pins for reads,
    pinned once more for writes: JWT admin, zero `resource_grants` rows,
    every mutating endpoint this session gated still succeeds."""

    def test_admin_may_rename_reorder_and_delete_with_no_grants_at_all(self) -> None:
        # Hub Admin's PERMISSION bypass (full_access) does NOT extend to the
        # SESSION check — locking is concurrent-edit safety, a different
        # axis from permission, and decision 4 lists no admin exception
        # (owner-confirmed 2026-09-08, session_handoff's §11 open item). An
        # admin needs a real lock/token here exactly like anyone else.
        admin_email = "admin@renphil.org"
        tab_a_token = self._edit_token(("tab", self.tabA.id), admin_email)
        resp = self.put(
            "/v2/tabs/tab-a",
            headers={**self.admin, "X-Edit-Tokens": tab_a_token},
            json={"title": "Admin renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # `order` is the session-exempt reorder shape (§5.4) — no token.
        resp = self.put("/v2/tabs/tab-a", headers=self.admin, json={"order": 9})
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.post(
            "/v2/tabs/tab-a/variants",
            headers={**self.admin, "X-Edit-Tokens": tab_a_token},
            json={"title": "Admin's Variant"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        nav1_token = self._edit_token(("nav_tab", self.nav1.id), admin_email)
        resp = self.delete("/v2/tabs/tab-b", headers={**self.admin, "X-Edit-Tokens": nav1_token})
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# project_ac_enforcement_gap.md's §6.8 nav-tab/hub split (2026-09-04): the
# same WRITE-side pattern as the classes above, one node kind up — `n` is
# now `hub` or `nav_tab` instead of `tab`/`component`. `nav_tabs.py`'s four
# mutating routes and `tabs_v2.py`'s `move_tab_to_nav_tab` used to sit behind
# the identity-only `require_hub_admin`; they now gate on `edit(hub)` /
# `edit(nav_tab)` through the exact same `require_edit` primitive.
# ---------------------------------------------------------------------------


class TestCreateNavTabRequiresEditOnHub(TabVisibilityWiringFixture):
    """§6.3 "create a child of n → n", one level up from
    `TestCreateTabRequiresEditOnParent`: a new nav tab's parent is the
    single hub row, not anything the caller names."""

    def test_refused_with_no_grant(self) -> None:
        resp = self.post("/v2/nav-tabs", headers=self.user(self.bob), json={"title": "New Nav"})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_forbidden_with_view_only_on_the_hub(self) -> None:
        self.grant(user=self.bob, node=("hub", self.hub.id), level="view")
        resp = self.post("/v2/nav-tabs", headers=self.user(self.bob), json={"title": "New Nav"})
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_the_hub(self) -> None:
        self.grant(user=self.bob, node=("hub", self.hub.id), level="edit")
        resp = self.post("/v2/nav-tabs", headers=self.user(self.bob), json={"title": "New Nav"})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestReorderNavTabsRequiresEditOnHub(TabVisibilityWiringFixture):
    """§6.3 "reorder n → parent(n)": every nav tab shares the SAME parent —
    the single hub row — so, unlike root-tab reordering, this is always one
    check, never a per-item resolution."""

    def test_refused_with_no_grant(self) -> None:
        resp = self.put(
            "/v2/nav-tabs/reorder",
            headers=self.user(self.bob),
            json={"orderedDocumentIds": ["nav-1", "nav-2"]},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_forbidden_with_edit_on_a_nav_tab_but_not_the_hub(self) -> None:
        """Edit on nav1 reveals the hub (the fold's upward reveal, §5.1) but
        does not GRANT edit on it — 403, not 404: bob can see the hub
        exists, he simply may not rearrange its children. The exact
        "reveals but doesn't grant" subtlety the §6.8 task asked to be
        pinned one level up from step 2's tab/nav-tab pin
        (`TestDeleteTabRequiresEditOnParent`)."""
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        resp = self.put(
            "/v2/nav-tabs/reorder",
            headers=self.user(self.bob),
            json={"orderedDocumentIds": ["nav-1", "nav-2"]},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_the_hub(self) -> None:
        self.grant(user=self.bob, node=("hub", self.hub.id), level="edit")
        resp = self.put(
            "/v2/nav-tabs/reorder",
            headers=self.user(self.bob),
            json={"orderedDocumentIds": ["nav-1", "nav-2"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestUpdateNavTabSplitsRenameFromReorder(TabVisibilityWiringFixture):
    """§6.3's table, one level up from
    `TestUpdateTabMetadataSplitsRenameFromReorder`: rename and
    access_control-edit are `edit(n)`, n = this nav tab; the `order` field
    is `edit(parent(n))` = `edit(hub)` — the two fields on the same PUT do
    NOT require the same grant."""

    def test_rename_succeeds_with_edit_on_the_nav_tab_alone(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        token = self._edit_token(("nav_tab", self.nav1.id), self.bob.email)
        resp = self.put(
            "/v2/nav-tabs/nav-1",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "Renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_rename_is_forbidden_with_edit_on_a_child_tab_alone(self) -> None:
        """Edit on tabA — a CHILD of nav1 — reveals nav1 (upward fold) but
        does not GRANT edit on it: 403, not 404. This is the exact case the
        §6.8 task named explicitly: edit on a nav tab's child reveals the
        nav tab without granting it."""
        self.grant(user=self.bob, node=("tab", self.tabA.id), level="edit")
        resp = self.put("/v2/nav-tabs/nav-1", headers=self.user(self.bob), json={"title": "Renamed"})
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_order_change_is_forbidden_with_edit_on_the_nav_tab_alone(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        resp = self.put("/v2/nav-tabs/nav-1", headers=self.user(self.bob), json={"order": 5})
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_order_change_succeeds_with_edit_on_the_hub(self) -> None:
        self.grant(user=self.bob, node=("hub", self.hub.id), level="edit")
        resp = self.put("/v2/nav-tabs/nav-1", headers=self.user(self.bob), json={"order": 5})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestDeleteNavTabRequiresEditOnHub(TabVisibilityWiringFixture):
    """§6.3 "delete n → parent(n)" = `edit(hub)`. Exercised against nav2
    (empty, no root tabs under it) to isolate the gate from the cascade
    machinery, which `test_nav_tabs.py`'s service-level tests already
    cover."""

    def test_refused_with_no_grant(self) -> None:
        resp = self.delete("/v2/nav-tabs/nav-2", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_forbidden_with_edit_on_the_nav_tab_itself_alone(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav2.id), level="edit")
        resp = self.delete("/v2/nav-tabs/nav-2", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_the_hub(self) -> None:
        self.grant(user=self.bob, node=("hub", self.hub.id), level="edit")
        # The SESSION target diverges from the PERMISSION gate here: delete
        # is edit(hub) for AC, but the hub has no lock node at all (§9 item
        # 1) — the live session `require_live_session` actually checks is on
        # the nav tab being deleted itself (§4.2's documented divergence).
        token = self._edit_token(("nav_tab", self.nav2.id), self.bob.email)
        resp = self.delete("/v2/nav-tabs/nav-2", headers={**self.user(self.bob), "X-Edit-Tokens": token})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestProtectedNavTabRuleLayersAlongsideTheACGate(TabVisibilityWiringFixture):
    """The pre-existing `protected` business rule (Dashboard cannot be
    renamed/deleted — `NavTabIconTests`, `test_nav_tabs.py`) must keep
    firing ALONGSIDE the new AC gate, not be replaced by it — the task's
    own instruction, checked rather than assumed."""

    def setUp(self) -> None:
        super().setUp()
        self.nav1.protected = True
        self.db.commit()

    def test_no_grant_404s_before_the_protected_rule_is_ever_reached(self) -> None:
        """The AC gate runs FIRST: an ungranted caller gets the ordinary
        fail-closed 404, not the protected-row 400 — they never even learn
        the row is protected."""
        resp = self.put("/v2/nav-tabs/nav-1", headers=self.user(self.bob), json={"title": "Renamed"})
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_edit_grant_still_hits_the_protected_refusal(self) -> None:
        """A caller who CAN edit nav1 clears the AC gate but still hits the
        protected-row business rule — the gate does not bypass it."""
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        token = self._edit_token(("nav_tab", self.nav1.id), self.bob.email)
        resp = self.put(
            "/v2/nav-tabs/nav-1",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"title": "Renamed"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_hub_admin_also_still_hits_the_protected_refusal(self) -> None:
        token = self._edit_token(("nav_tab", self.nav1.id), "admin@renphil.org")
        resp = self.put(
            "/v2/nav-tabs/nav-1",
            headers={**self.admin, "X-Edit-Tokens": token},
            json={"title": "Renamed"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_order_change_on_a_protected_nav_tab_is_unaffected_by_the_rule(self) -> None:
        """`protected` only refuses rename/delete — reordering (`edit(hub)`)
        stays a plain AC question, exactly as
        `test_protected_nav_tab_refuses_rename_and_delete_permits_reorder`
        already pins at the service layer."""
        self.grant(user=self.bob, node=("hub", self.hub.id), level="edit")
        resp = self.put("/v2/nav-tabs/nav-1", headers=self.user(self.bob), json={"order": 3})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestMoveTabToNavTabRequiresEditOnBothNavTabs(TabVisibilityWiringFixture):
    """Does step 2's `move_tab` two-parent judgement call (edit on BOTH the
    old and new parent) apply the same way one level up? Yes — a root tab's
    true AC parent is its nav tab, so moving it between nav tabs is edit on
    both. tabB moves from nav1 to nav2."""

    def test_refused_with_no_grants_at_all(self) -> None:
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers=self.user(self.bob),
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_refused_when_only_the_old_nav_tab_is_editable(self) -> None:
        """nav2 (the new parent) has no grant at all here — fully invisible
        to bob — so this is 404 (cannot even see the destination), not
        403."""
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers=self.user(self.bob),
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_forbidden_when_the_old_nav_tab_is_only_visible_not_editable(self) -> None:
        """View-only on nav1 plus edit on nav2 reveals BOTH nodes but only
        grants one of them — 403, not 404: bob can see the tab is leaving
        nav1, he simply may not authorize that departure."""
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="view")
        self.grant(user=self.bob, node=("nav_tab", self.nav2.id), level="edit")
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers=self.user(self.bob),
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_both_nav_tabs(self) -> None:
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        self.grant(user=self.bob, node=("nav_tab", self.nav2.id), level="edit")
        tokens = self._edit_tokens(
            (("nav_tab", self.nav1.id), self.bob.email), (("nav_tab", self.nav2.id), self.bob.email)
        )
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers={**self.user(self.bob), "X-Edit-Tokens": tokens},
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_forbidden_when_the_destination_is_only_visible_even_with_a_live_source_session(self) -> None:
        """New 2026-09-30 (plan_node_session_gaps §3, O3): the destination
        needs no SESSION any more, but still needs EDIT — dropping the
        destination session check must not have dropped the permission
        check with it."""
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        self.grant(user=self.bob, node=("nav_tab", self.nav2.id), level="view")
        token = self._edit_token(("nav_tab", self.nav1.id), self.bob.email)
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_succeeds_with_edit_on_both_and_only_the_source_nav_tabs_session(self) -> None:
        """New 2026-09-30 (O3): the pen model holds one nav-tab session at a
        time, so a move carries only the source's token."""
        self.grant(user=self.bob, node=("nav_tab", self.nav1.id), level="edit")
        self.grant(user=self.bob, node=("nav_tab", self.nav2.id), level="edit")
        token = self._edit_token(("nav_tab", self.nav1.id), self.bob.email)
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers={**self.user(self.bob), "X-Edit-Tokens": token},
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestHubAdminBypassesEveryNavTabWriteGate(TabVisibilityWiringFixture):
    """The same regression `TestHubAdminBypassesEveryWriteGate` pins for
    tabs, pinned once more for the nav-tab/hub surface: JWT admin, zero
    `resource_grants` rows, every mutating endpoint this session gated
    still succeeds."""

    def test_admin_may_create_rename_reorder_move_and_delete_with_no_grants_at_all(self) -> None:
        # Same divergence as the single-write classes above: admin needs a
        # real session/token on nav1/nav2 for rename/move/delete. `create`
        # and `reorder` stay fully exempt (hub has no lock node; reorder is
        # session-exempt by decision 9), so those two calls carry no token.
        admin_email = "admin@renphil.org"
        resp = self.post("/v2/nav-tabs", headers=self.admin, json={"title": "Admin's Nav"})
        self.assertEqual(resp.status_code, 200, resp.text)

        nav1_token = self._edit_token(("nav_tab", self.nav1.id), admin_email)
        resp = self.put(
            "/v2/nav-tabs/nav-1",
            headers={**self.admin, "X-Edit-Tokens": nav1_token},
            json={"title": "Admin renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.put(
            "/v2/nav-tabs/reorder", headers=self.admin, json={"orderedDocumentIds": ["nav-2", "nav-1"]}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        nav2_token = self._edit_token(("nav_tab", self.nav2.id), admin_email)
        resp = self.put(
            "/v2/tabs/tab-b/nav-tab",
            headers={**self.admin, "X-Edit-Tokens": f"{nav1_token},{nav2_token}"},
            json={"navTabDocumentId": "nav-2"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.delete("/v2/nav-tabs/nav-2", headers={**self.admin, "X-Edit-Tokens": nav2_token})
        self.assertEqual(resp.status_code, 200, resp.text)


if __name__ == "__main__":
    unittest.main()
