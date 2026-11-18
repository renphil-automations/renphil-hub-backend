from __future__ import annotations

import os
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Importing the v2 models creates the application's normal Postgres engine,
# but this focused test never connects to it. These defaults only make the
# module importable when a developer runs the test without a local .env.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-nav-tabs-tests-only")
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
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.main import create_app  # noqa: E402
from tests._real_admin import seed_real_admin  # noqa: E402
from app.services.gridstack_service import (  # noqa: E402
    MIRROR_WIDGET_TYPE,
    create_tab_v2,
    create_tab_variant_v2,
    get_gridstack_by_document_id,
    get_root_tabs_v2,
    get_tab_children_v2,
    resolve_component_location_v2,
    update_tab_by_document_id_v2,
)
from app.services.nav_tab_service import (  # noqa: E402
    RESERVED_NAV_SLUGS,
    create_nav_tab_v2,
    delete_nav_tab_v2,
    get_nav_tab_by_document_id,
    move_tab_to_nav_tab_v2,
    reorder_nav_tabs_v2,
    update_nav_tab_v2,
)
from app.services.super_blocknote_service import SBN_ROOT_TYPE, create_sbn_node  # noqa: E402

def _treat_example_com_as_org_domain(test: unittest.TestCase) -> None:
    """This module's @example.com personas stand in for signed-in staff.
    Since the delete-user re-creation fix (plan_admin_delete_user_2026-10-18.md
    §4) the lazy sign-in path only provisions ORG-DOMAIN emails, so
    example.com is made one here. Provisioning itself is tested in
    test_admin_delete_user.py."""
    # `get_settings()` builds a fresh Settings each call, so the env var is what to patch.
    patcher = mock.patch.dict(os.environ, {"ALLOWED_EMAIL_DOMAIN": "renphil.org,example.com"})
    patcher.start()
    test.addCleanup(patcher.stop)


class NavTabServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _make_nav_tab(self, title: str, protected: bool = False) -> dict:
        nav_tab = create_nav_tab_v2(self.db, title=title)
        if protected:
            row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav_tab["documentId"]).first()
            row.protected = True
            self.db.commit()
            nav_tab["protected"] = True
        return nav_tab

    # ------------------------------------------------------------------
    # Case 1: slug generation + _resolve_nav_slug rejections + rename
    # ------------------------------------------------------------------

    def test_slug_generated_and_resolve_nav_slug_rejections(self) -> None:
        nav_tab = create_nav_tab_v2(self.db, title="My Programs")
        self.assertEqual(nav_tab["slug"], "my-programs")

        # Duplicate slug (same title again).
        with self.assertRaises(ValueError):
            create_nav_tab_v2(self.db, title="My Programs")

        # Reserved slug.
        self.assertIn("tracking", RESERVED_NAV_SLUGS)
        with self.assertRaises(ValueError):
            create_nav_tab_v2(self.db, title="Tracking")

        # Title that slugifies to empty.
        with self.assertRaises(ValueError):
            create_nav_tab_v2(self.db, title="!!!")

        # Rename re-runs all three checks: renaming to a reserved word fails.
        other = create_nav_tab_v2(self.db, title="Other Tab")
        with self.assertRaises(ValueError):
            update_nav_tab_v2(self.db, other["documentId"], title="Tracking")

        # Renaming to another nav tab's existing title fails (duplicate).
        with self.assertRaises(ValueError):
            update_nav_tab_v2(self.db, other["documentId"], title="My Programs")

        # Renaming a nav tab to its OWN current title is not a self-collision.
        unchanged = update_nav_tab_v2(self.db, other["documentId"], title="Other Tab")
        self.assertEqual(unchanged["slug"], "other-tab")

    # ------------------------------------------------------------------
    # Case 2: root created with navTabDocumentId lands in that nav tab
    # ------------------------------------------------------------------

    def test_root_scoped_to_nav_tab(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_b = self._make_nav_tab("Nav B")

        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])

        create_tab_v2(self.db, title="Root In A", nav_tab_id=nav_a_row.id)
        create_tab_v2(self.db, title="Root In B", nav_tab_id=nav_b_row.id)

        roots_a = get_root_tabs_v2(self.db, nav_tab_id=nav_a_row.id)
        roots_b = get_root_tabs_v2(self.db, nav_tab_id=nav_b_row.id)

        self.assertEqual([r["title"] for r in roots_a], ["Root In A"])
        self.assertEqual([r["title"] for r in roots_b], ["Root In B"])
        self.assertEqual(roots_a[0]["navTabDocumentId"], nav_a["documentId"])
        self.assertEqual(roots_a[0]["navTabTitle"], "Nav A")

    # ------------------------------------------------------------------
    # Case 3: title uniqueness is scoped per nav tab
    # ------------------------------------------------------------------

    def test_title_uniqueness_scoped_per_nav_tab(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_b = self._make_nav_tab("Nav B")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])

        create_tab_v2(self.db, title="Home", nav_tab_id=nav_a_row.id)
        # Same title in a DIFFERENT nav tab: allowed.
        create_tab_v2(self.db, title="Home", nav_tab_id=nav_b_row.id)

        # Same title in the SAME nav tab: rejected.
        with self.assertRaises(ValueError):
            create_tab_v2(self.db, title="Home", nav_tab_id=nav_a_row.id)

    # ------------------------------------------------------------------
    # Case 3b: uniqueness is by SLUG, not by exact title.
    #
    # A root tab's URL segment is derived from its title (slugifyTitle), so
    # the slug is what has to be unique. An exact title match let "home" sit
    # beside "Home": both address /<nav>/home, and resolveTabByPath takes the
    # first match — silently orphaning the other.
    # ------------------------------------------------------------------

    def test_root_title_uniqueness_is_by_slug(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_b = self._make_nav_tab("Nav B")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])

        create_tab_v2(self.db, title="Home", nav_tab_id=nav_a_row.id)

        # Differing only in case, punctuation, or spacing — all slugify to
        # "home", so all are rejected.
        for colliding in ("home", "HOME", "  Home  ", "Home!"):
            with self.assertRaises(ValueError, msg=f"{colliding!r} should collide with 'Home'"):
                create_tab_v2(self.db, title=colliding, nav_tab_id=nav_a_row.id)

        # Genuinely different titles are unaffected.
        create_tab_v2(self.db, title="Home Base", nav_tab_id=nav_a_row.id)

        # Still scoped per nav tab: "home" is free in a different one.
        create_tab_v2(self.db, title="home", nav_tab_id=nav_b_row.id)

        # A VARIANT's title still doesn't block a root's — variants are
        # selected by a pill, never addressed by their own URL segment.
        root = create_tab_v2(self.db, title="Reports", nav_tab_id=nav_b_row.id)
        create_tab_variant_v2(self.db, parent_document_id=root["documentId"], title="Summary")
        create_tab_v2(self.db, title="summary", nav_tab_id=nav_b_row.id)

        # Titles that slugify to empty fall back to the exact-title rule, so
        # two different punctuation-only titles don't report as duplicates.
        create_tab_v2(self.db, title="!!!", nav_tab_id=nav_a_row.id)
        create_tab_v2(self.db, title="???", nav_tab_id=nav_a_row.id)
        with self.assertRaises(ValueError):
            create_tab_v2(self.db, title="!!!", nav_tab_id=nav_a_row.id)

    def test_rename_cannot_collide_with_another_roots_slug(self) -> None:
        """Rename had no uniqueness check at all, so it bypassed the
        create-time one entirely — create "A", create "B", rename B to "a"."""
        nav_a = self._make_nav_tab("Nav A")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])

        create_tab_v2(self.db, title="Home", nav_tab_id=nav_a_row.id)
        other = create_tab_v2(self.db, title="Reports", nav_tab_id=nav_a_row.id)

        with self.assertRaises(ValueError):
            update_tab_by_document_id_v2(self.db, other["documentId"], title="home")

        # Rejected before anything was written.
        other_row = self.db.query(TabV2).filter(TabV2.document_id == other["documentId"]).first()
        self.assertEqual(other_row.title, "Reports")

        # Renaming a tab to its own current title is not a self-collision.
        renamed = update_tab_by_document_id_v2(self.db, other["documentId"], title="Reports")
        self.assertIsNotNone(renamed)

        # A genuinely new title still works.
        update_tab_by_document_id_v2(self.db, other["documentId"], title="Quarterly Reports")
        self.db.refresh(other_row)
        self.assertEqual(other_row.title, "Quarterly Reports")

    def test_move_rejects_slug_collision_in_destination(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_b = self._make_nav_tab("Nav B")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])

        movable = create_tab_v2(self.db, title="Home", nav_tab_id=nav_a_row.id)
        create_tab_v2(self.db, title="home", nav_tab_id=nav_b_row.id)

        with self.assertRaises(ValueError):
            move_tab_to_nav_tab_v2(
                self.db,
                tab_document_id=movable["documentId"],
                nav_tab_document_id=nav_b["documentId"],
            )

        # Rejected BEFORE any mutation — the tab is untouched.
        moved_row = self.db.query(TabV2).filter(TabV2.document_id == movable["documentId"]).first()
        self.assertEqual(moved_row.nav_tab_id, nav_a_row.id)

    # ------------------------------------------------------------------
    # Case 4: a variant inherits its root's nav_tab_id
    # ------------------------------------------------------------------

    def test_variant_inherits_nav_tab_id(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])

        root = create_tab_v2(self.db, title="Root", nav_tab_id=nav_a_row.id)
        create_tab_variant_v2(self.db, parent_document_id=root["documentId"], title="Variant One")

        variant_row = self.db.query(TabV2).filter(TabV2.title == "Variant One").first()
        self.assertEqual(variant_row.nav_tab_id, nav_a_row.id)

    # ------------------------------------------------------------------
    # Case 5: move_tab_to_nav_tab_v2
    # ------------------------------------------------------------------

    def test_move_tab_to_nav_tab(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_b = self._make_nav_tab("Nav B")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])

        root = create_tab_v2(self.db, title="Movable", nav_tab_id=nav_a_row.id)
        create_tab_variant_v2(self.db, parent_document_id=root["documentId"], title="Movable Variant")

        result = move_tab_to_nav_tab_v2(
            self.db, tab_document_id=root["documentId"], nav_tab_document_id=nav_b["documentId"]
        )
        self.assertIsNotNone(result)

        root_row = self.db.query(TabV2).filter(TabV2.document_id == root["documentId"]).first()
        self.assertEqual(root_row.nav_tab_id, nav_b_row.id)

        variant_row = self.db.query(TabV2).filter(TabV2.title == "Movable Variant").first()
        self.assertEqual(variant_row.nav_tab_id, nav_b_row.id)

        # Rejects moving a sub-tab (gridstack, not a root TabV2).
        children_before = get_tab_children_v2(self.db, root["documentId"])
        self.assertEqual(children_before, [])
        sub_tab = create_tab_v2(self.db, title="Sub", parent_document_id=root["documentId"])
        with self.assertRaises(ValueError):
            move_tab_to_nav_tab_v2(
                self.db, tab_document_id=sub_tab["documentId"], nav_tab_document_id=nav_a["documentId"]
            )

        # Rejects a title collision in the destination.
        create_tab_v2(self.db, title="Already Here", nav_tab_id=nav_a_row.id)
        other_root = create_tab_v2(self.db, title="Already Here", nav_tab_id=nav_b_row.id)
        with self.assertRaises(ValueError):
            move_tab_to_nav_tab_v2(
                self.db,
                tab_document_id=other_root["documentId"],
                nav_tab_document_id=nav_a["documentId"],
            )

    # ------------------------------------------------------------------
    # Case 6: delete_nav_tab_v2 cascades and leaves no orphans
    # ------------------------------------------------------------------

    def test_delete_nav_tab_cascades(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])

        root_one = create_tab_v2(self.db, title="Root One", nav_tab_id=nav_a_row.id)
        create_tab_v2(self.db, title="Root Two", nav_tab_id=nav_a_row.id)
        create_tab_variant_v2(self.db, parent_document_id=root_one["documentId"], title="Root One Variant")

        gridstack_count_before = self.db.query(GridstackV2).count()
        self.assertGreater(gridstack_count_before, 0)

        result = delete_nav_tab_v2(self.db, nav_a["documentId"])
        self.assertIsNotNone(result)
        # Root One's own gridstack + its variant's gridstack + Root Two's
        # own gridstack = 3 deleted gridstack nodes across both subtrees.
        self.assertEqual(result["deleted_count"], 3)

        # No orphan tabs, gridstacks, or components remain under this nav tab.
        self.assertEqual(
            self.db.query(TabV2).filter(TabV2.nav_tab_id == nav_a_row.id).count(), 0
        )
        self.assertEqual(self.db.query(GridstackV2).count(), 0)
        self.assertEqual(self.db.query(ComponentV2).count(), 0)
        self.assertIsNone(get_nav_tab_by_document_id(self.db, nav_a["documentId"]))

    # ------------------------------------------------------------------
    # Case 7: protected nav tab refuses rename/delete, permits reorder
    # ------------------------------------------------------------------

    def test_protected_nav_tab_refuses_rename_and_delete_permits_reorder(self) -> None:
        dashboard = self._make_nav_tab("Dashboard", protected=True)
        other = self._make_nav_tab("Other")

        with self.assertRaises(ValueError):
            update_nav_tab_v2(self.db, dashboard["documentId"], title="Renamed Dashboard")

        with self.assertRaises(ValueError):
            delete_nav_tab_v2(self.db, dashboard["documentId"])

        # Reorder is permitted (order is not gated by `protected`).
        reordered = reorder_nav_tabs_v2(
            self.db, [other["documentId"], dashboard["documentId"]]
        )
        orders_by_doc = {r["documentId"]: r["order"] for r in reordered}
        self.assertEqual(orders_by_doc[other["documentId"]], 0)
        self.assertEqual(orders_by_doc[dashboard["documentId"]], 1)

        # Updating order alone (no title) on a protected nav tab is fine too.
        updated = update_nav_tab_v2(self.db, dashboard["documentId"], order=5)
        self.assertEqual(updated["order"], 5)

    # ------------------------------------------------------------------
    # Case 8: get_root_tabs_v2() with no argument returns every root
    # ------------------------------------------------------------------

    def test_get_root_tabs_v2_unscoped_returns_every_root(self) -> None:
        nav_a = self._make_nav_tab("Nav A")
        nav_b = self._make_nav_tab("Nav B")
        nav_a_row = get_nav_tab_by_document_id(self.db, nav_a["documentId"])
        nav_b_row = get_nav_tab_by_document_id(self.db, nav_b["documentId"])

        create_tab_v2(self.db, title="Root A", nav_tab_id=nav_a_row.id)
        create_tab_v2(self.db, title="Root B", nav_tab_id=nav_b_row.id)

        all_roots = get_root_tabs_v2(self.db)
        titles = {r["title"] for r in all_roots}
        self.assertEqual(titles, {"Root A", "Root B"})

    # ------------------------------------------------------------------
    # Case 9: resolve_component_location_v2 separates a tab VARIANT from
    # the root tab that owns it.
    #
    # A variant is a TabV2 with parent_tab_id set and it owns its own root
    # gridstack, so the naive lookup returns the VARIANT as "the root tab".
    # The frontend then looks that id up in its root-tab list (which
    # excludes variants), finds nothing, and leaves the user on whichever
    # variant is active by default — the first one. That reads as "works"
    # whenever the target happens to be in the first variant, which is why
    # this went unnoticed.
    # ------------------------------------------------------------------

    def _add_component(self, gridstack_id: int, link: str) -> ComponentV2:
        component = ComponentV2(link=link, title=link, type="calendar", gridstack_id=gridstack_id)
        self.db.add(component)
        self.db.commit()
        return component

    def test_resolve_component_location_splits_variant_from_root_tab(self) -> None:
        nav = self._make_nav_tab("Nav Test One")
        nav_row = get_nav_tab_by_document_id(self.db, nav["documentId"])

        root = create_tab_v2(self.db, title="Home", nav_tab_id=nav_row.id)
        variant_one = create_tab_variant_v2(
            self.db, parent_document_id=root["documentId"], title="Var 1"
        )
        variant_two = create_tab_variant_v2(
            self.db, parent_document_id=root["documentId"], title="Var 2"
        )

        def root_gridstack_of(tab_document_id: str) -> GridstackV2:
            tab_row = self.db.query(TabV2).filter(TabV2.document_id == tab_document_id).first()
            return (
                self.db.query(GridstackV2)
                .filter(GridstackV2.parent_tab_id == tab_row.id, GridstackV2.parent_id.is_(None))
                .first()
            )

        # A component on the SECOND variant's canvas — the case that used to
        # silently resolve to the first variant.
        self._add_component(root_gridstack_of(variant_two["documentId"]).id, "link-in-var-2")
        located = resolve_component_location_v2(self.db, "link-in-var-2")
        self.assertIsNotNone(located)
        self.assertEqual(located["rootTabDocumentId"], root["documentId"])
        self.assertEqual(located["variantDocumentId"], variant_two["documentId"])
        self.assertEqual(located["navTabDocumentId"], nav["documentId"])

        # ...and the first variant resolves to itself, not to "no variant" —
        # otherwise the fix would only paper over the second-variant case.
        self._add_component(root_gridstack_of(variant_one["documentId"]).id, "link-in-var-1")
        located_one = resolve_component_location_v2(self.db, "link-in-var-1")
        self.assertEqual(located_one["rootTabDocumentId"], root["documentId"])
        self.assertEqual(located_one["variantDocumentId"], variant_one["documentId"])

        # A component on a plain root tab's own canvas still reports no
        # variant, so the frontend leaves variant selection alone.
        plain_root = create_tab_v2(self.db, title="Test", nav_tab_id=nav_row.id)
        self._add_component(root_gridstack_of(plain_root["documentId"]).id, "link-on-root")
        located_root = resolve_component_location_v2(self.db, "link-on-root")
        self.assertEqual(located_root["rootTabDocumentId"], plain_root["documentId"])
        self.assertIsNone(located_root["variantDocumentId"])


class NavTabAuthSurfaceTests(unittest.TestCase):
    """Every mutating nav-tab endpoint used to be gated by `require_hub_admin`,
    a pure-JWT role check.

    UPDATED for project_ac_enforcement_gap.md's §6.8 nav-tab/hub split
    (2026-09-04): that gate is gone from every route in this file, replaced
    by `edit(hub)` / `edit(nav_tab)` — the same node-scoped `require_edit`
    primitive `tabs_v2.py`'s write routes already use
    (project_ac_enforcement_gap.md item 2). The per-node split's exhaustive
    coverage (which node each route checks, the "reveals but doesn't grant"
    subtlety, the Hub Admin bypass) lives in `test_tab_visibility_wiring.py`
    — this class keeps only the surface-level claims a router file's own
    test suite should still make: auth is required at all, GET stays
    ungated, method-not-allowed still 405s, and the Hub Admin bypass — by
    JWT role, the only path this fixture's `_build_org`-less setUp can
    reach — still passes every route end to end.

    Still exercised end to end against a real in-memory SQLite DB rather
    than a raising stub session, since the endpoints behind the gate do
    real work."""

    def setUp(self) -> None:
        _treat_example_com_as_org_domain(self)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        # StaticPool means every session from this factory shares the same
        # single connection/in-memory DB, so writes made via the TestClient
        # (its own session, via the dependency override below) are visible
        # to `self.db` and vice versa — same convention as every other
        # harness in this test suite.
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        # access_visibility_service.resolve_hub_node needs EXACTLY ONE hub
        # row to resolve to anything at all (§3.3, fail-closed otherwise) —
        # without it every non-admin test below would 404 regardless of
        # grants, which would prove nothing about the real gate. Same fixture
        # gap `test_nav_tabs.py`'s own `ComponentLocationAccessTests` and
        # `test_tab_locking.py` were found to have during the read-wiring
        # session; fixed the same way here.
        self.db.add(HubV2(document_id="hub-nav-auth"))
        self.db.commit()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _token(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": "Test User", "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> dict:
        # Phase 3: the JWT "Hub Admin" claim alone admits nobody. Every
        # "Hub Admin" token in this module is the admin BYPASS, never a
        # test about a JWT-only admin, so it also gets the real
        # (hub_admin, universal) assignment (tests/_real_admin.py).
        if roles and "Hub Admin" in roles:
            seed_real_admin(self._session_factory, email)
        return {"Authorization": f"Bearer {self._token(roles, email)}"}

    def test_no_token_is_401_on_every_nav_tab_endpoint(self) -> None:
        for method, path, body in [
            ("GET", "/v2/nav-tabs", None),
            ("GET", "/v2/nav-tabs/doc-1/tabs", None),
            ("POST", "/v2/nav-tabs", {"title": "X"}),
            ("PUT", "/v2/nav-tabs/reorder", {"orderedDocumentIds": ["a"]}),
            ("PUT", "/v2/nav-tabs/doc-1", {"title": "X"}),
            ("DELETE", "/v2/nav-tabs/doc-1", None),
            ("PUT", "/v2/tabs/doc-1/nav-tab", {"navTabDocumentId": "nav-1"}),
        ]:
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, json=body)
                self.assertEqual(resp.status_code, 401)

    def test_get_endpoints_require_only_authentication(self) -> None:
        # GET endpoints carry no gate at all — 200 (list) or 404 (unknown
        # nav tab in the /tabs lookup), never 401/403, regardless of role.
        headers = self._auth(roles=["Hub Member"])
        for path in ["/v2/nav-tabs", "/v2/nav-tabs/doc-1/tabs"]:
            with self.subTest(path=path):
                resp = self.client.get(path, headers=headers)
                self.assertIn(resp.status_code, (200, 404))

    def test_reorder_declared_before_document_id_route(self) -> None:
        resp = self.client.patch("/v2/nav-tabs/reorder", headers=self._auth(roles=["Hub Admin"]))
        self.assertEqual(resp.status_code, 405)

    def test_hub_admin_role_passes_every_mutating_endpoint(self) -> None:
        # plan_lock_propagation_2026-09-08.md §5.2 added a live-session gate
        # to rename/delete (orthogonal to the permission bypass this test is
        # actually about — admin does not bypass it, owner-confirmed
        # 2026-09-08). `reorder` stays session-exempt (decision 9).
        from app.services.edit_lock_service import acquire

        admin_headers = self._auth(roles=["Hub Admin"])

        created = self.client.post("/v2/nav-tabs", json={"title": "Made It"}, headers=admin_headers)
        self.assertEqual(created.status_code, 200, created.text)
        doc_id = created.json()["documentId"]
        row = self.db.query(NavTabV2).filter(NavTabV2.document_id == doc_id).first()
        token = acquire(self.db, ("nav_tab", row.id), "nobody@example.com").token

        resp = self.client.put(
            f"/v2/nav-tabs/{doc_id}",
            json={"title": "Renamed"},
            headers={**admin_headers, "X-Edit-Tokens": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.put(
            "/v2/nav-tabs/reorder", json={"orderedDocumentIds": [doc_id]}, headers=admin_headers
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.delete(
            f"/v2/nav-tabs/{doc_id}", headers={**admin_headers, "X-Edit-Tokens": token}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_the_real_lock_endpoints_own_token_authorizes_the_rename(self) -> None:
        """findings_dev_login_live_testing_2026-09-12.md addendum: every
        other test in this module (including the one right above) gets its
        token from calling `acquire()` DIRECTLY against the service layer —
        which always carries `expires_at` correctly, since `LockGrant`
        itself was never the bug. That means none of them would have caught
        `lock_nav_tab_by_document_id_v2`'s response never actually setting
        `lock_expires_at` (`_format_nav_tab` bare never does) — the
        frontend's `if (locked.lock_token && locked.lock_expires_at)` guard
        (Sidebar.tsx) was always false as a result, so `setEditSession`
        never ran and no real browser session ever sent `X-Edit-Tokens` at
        all. This test goes through the REAL `PUT .../lock` HTTP door, the
        same one the browser calls, and uses exactly the `lock_token` its
        JSON response hands back — the one path that actually exercises the
        bug."""
        admin_headers = self._auth(roles=["Hub Admin"])

        created = self.client.post("/v2/nav-tabs", json={"title": "Made It"}, headers=admin_headers)
        self.assertEqual(created.status_code, 200, created.text)
        doc_id = created.json()["documentId"]

        lock_resp = self.client.put(f"/v2/nav-tabs/{doc_id}/lock", json={}, headers=admin_headers)
        self.assertEqual(lock_resp.status_code, 200, lock_resp.text)
        self.assertIsNotNone(lock_resp.json()["lock_expires_at"])
        token = lock_resp.json()["lock_token"]

        resp = self.client.put(
            f"/v2/nav-tabs/{doc_id}",
            json={"title": "Renamed"},
            headers={**admin_headers, "X-Edit-Tokens": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_hub_edit_field_is_true_for_a_hub_admin(self) -> None:
        """findings_dev_login_live_testing_2026-09-12.md #4 — the new
        signal the frontend needs to gate nav-tab Delete/reorder correctly
        (edit(hub), NOT a nav tab's own edit(n))."""
        resp = self.client.get("/v2/nav-tabs", headers=self._auth(roles=["Hub Admin"]))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["hub_edit"])

    def test_hub_edit_field_is_false_for_a_nav_tab_only_editor(self) -> None:
        """The exact live scenario #4 traced: `test1@gmail.com` held `edit`
        on "Test 1" directly (a nav-tab-level resource_grants row) but
        nothing on the Hub. `hub_edit` must read False for that identity
        even though their OWN nav tab's `edit` correctly reads True — the
        two must not be conflated, which is the whole bug this field
        fixes."""
        admin_headers = self._auth(roles=["Hub Admin"])
        created = self.client.post(
            "/v2/nav-tabs", json={"title": "Editor Only"}, headers=admin_headers
        )
        self.assertEqual(created.status_code, 200, created.text)
        doc_id = created.json()["documentId"]
        row = self.db.query(NavTabV2).filter(NavTabV2.document_id == doc_id).first()

        editor = UserV2(
            email="editor@example.com", has_signed_in=True, created_at=datetime.now(timezone.utc)
        )
        self.db.add(editor)
        self.db.commit()
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["nav_tab"]: row.id},
                user_id=editor.id,
                level="edit",
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        resp = self.client.get(
            "/v2/nav-tabs", headers=self._auth(roles=[], email="editor@example.com")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["hub_edit"])
        own_row = next(r for r in resp.json()["data"] if r["documentId"] == doc_id)
        self.assertTrue(own_row["edit"])
        self.assertTrue(own_row["edit_within"])

    def test_edit_within_is_true_for_a_hub_admin(self) -> None:
        admin_headers = self._auth(roles=["Hub Admin"])
        created = self.client.post("/v2/nav-tabs", json={"title": "Admin's"}, headers=admin_headers)
        self.assertEqual(created.status_code, 200, created.text)
        resp = self.client.get("/v2/nav-tabs", headers=admin_headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["data"])
        for row in resp.json()["data"]:
            self.assertTrue(row["edit_within"], row)

    def test_edit_within_sees_a_grant_on_a_root_tab_beneath_the_nav_tab(self) -> None:
        """2026-10-07 — the Edit Mode toggle gate. An edit grant on a root
        tab only: the nav tab's own `edit` is False (and it is merely
        revealed), but `edit_within` must read True so the frontend offers
        the toggle."""
        admin_headers = self._auth(roles=["Hub Admin"])
        created = self.client.post("/v2/nav-tabs", json={"title": "Deep"}, headers=admin_headers)
        self.assertEqual(created.status_code, 200, created.text)
        doc_id = created.json()["documentId"]
        nav_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == doc_id).first()
        root = TabV2(document_id="deep-root-0001", title="Root", nav_tab_id=nav_row.id)
        self.db.add(root)
        self.db.commit()

        editor = UserV2(
            email="deep@example.com", has_signed_in=True, created_at=datetime.now(timezone.utc)
        )
        self.db.add(editor)
        self.db.commit()
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["tab"]: root.id},
                user_id=editor.id,
                level="edit",
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        resp = self.client.get("/v2/nav-tabs", headers=self._auth(roles=[], email="deep@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        row = next(r for r in resp.json()["data"] if r["documentId"] == doc_id)
        self.assertFalse(row["edit"])
        self.assertTrue(row["revealed"])
        self.assertTrue(row["edit_within"])

    def test_edit_within_is_false_for_a_view_only_grant(self) -> None:
        admin_headers = self._auth(roles=["Hub Admin"])
        created = self.client.post("/v2/nav-tabs", json={"title": "Viewed"}, headers=admin_headers)
        self.assertEqual(created.status_code, 200, created.text)
        doc_id = created.json()["documentId"]
        nav_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == doc_id).first()

        viewer = UserV2(
            email="viewer@example.com", has_signed_in=True, created_at=datetime.now(timezone.utc)
        )
        self.db.add(viewer)
        self.db.commit()
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["nav_tab"]: nav_row.id},
                user_id=viewer.id,
                level="view",
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        resp = self.client.get("/v2/nav-tabs", headers=self._auth(roles=[], email="viewer@example.com"))
        self.assertEqual(resp.status_code, 200, resp.text)
        row = next(r for r in resp.json()["data"] if r["documentId"] == doc_id)
        self.assertFalse(row["edit_within"])

    def test_every_mutating_endpoint_404s_without_any_grant(self) -> None:
        """UPDATED for the §6.8 split: the old gate read the JWT's role
        names and nothing else, so an outsider always got a blanket 403. The
        new gate is node-scoped (`edit(hub)`/`edit(nav_tab)`), and D2 (plan
        §2) says unset access control matches no one — an outsider with ZERO
        `resource_grants` rows cannot even VIEW the hub or the nav tab, so
        this is now 404 (§9's fail-closed convention), not 403. That
        difference IS this session's change, not a regression:
        `test_tab_visibility_wiring.py` pins the 403 case separately, for a
        caller who can view but not edit.

        The one thing that DIDN'T change: a stored `access_control` naming
        this caller as an admin is still not consulted — that was the
        propagation engine's job, and it is gone; the new gate reads
        `resource_grants`, not this legacy JSONB, either."""
        admin_headers = self._auth(roles=["Hub Admin"])
        existing = self.client.post(
            "/v2/nav-tabs", json={"title": "Existing"}, headers=admin_headers
        ).json()

        # Name the outsider as an admin on the nav tab itself via the OLD,
        # unread JSONB field. Under the pre-redesign per-node gate this
        # would have admitted them; under both the old JWT-only gate and
        # this new resource_grants-based one, it changes nothing.
        row = self.db.query(NavTabV2).filter(NavTabV2.document_id == existing["documentId"]).first()
        row.access_control = {
            "admins": {"users": [{"email": "outsider@example.com"}], "roles": []},
            "viewers": {"users": [{"email": "outsider@example.com"}], "roles": []},
        }
        self.db.commit()

        outsider = self._auth(roles=["Hub Member"], email="outsider@example.com")
        for method, path, body in [
            ("POST", "/v2/nav-tabs", {"title": "Blocked"}),
            # A non-empty, real documentId — an empty list now fails input
            # validation (400) BEFORE the AC gate even runs, which would
            # test the wrong thing here.
            ("PUT", "/v2/nav-tabs/reorder", {"orderedDocumentIds": [existing["documentId"]]}),
            ("PUT", f"/v2/nav-tabs/{existing['documentId']}", {"title": "Blocked"}),
            ("DELETE", f"/v2/nav-tabs/{existing['documentId']}", None),
        ]:
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, json=body, headers=outsider)
                self.assertEqual(resp.status_code, 404, resp.text)

    def test_move_between_nav_tabs_404s_without_any_grant(self) -> None:
        """Moving a root tab between nav tabs is a nav-tab-level operation,
        so it carries the same gate — now `edit` on BOTH nav tabs
        (`test_tab_visibility_wiring.py`'s `TestMoveTabToNavTabRequiresEditOnBothNavTabs`
        owns that split; this is the zero-grant case, 404 for the same D2
        reason the class above is)."""
        from app.services.edit_lock_service import acquire

        admin_headers = self._auth(roles=["Hub Admin"])
        nav_a = self.client.post("/v2/nav-tabs", json={"title": "Nav A"}, headers=admin_headers).json()
        nav_b = self.client.post("/v2/nav-tabs", json={"title": "Nav B"}, headers=admin_headers).json()
        nav_a_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav_a["documentId"]).first()
        nav_a_token = acquire(self.db, ("nav_tab", nav_a_row.id), "nobody@example.com").token
        root = self.client.post(
            "/v2/tabs/",
            json={"title": "Root", "navTabDocumentId": nav_a["documentId"]},
            headers={**admin_headers, "X-Edit-Tokens": nav_a_token},
        ).json()

        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/nav-tab",
            json={"navTabDocumentId": nav_b["documentId"]},
            headers=self._auth(roles=["Hub Member"], email="outsider@example.com"),
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_hub_admin_can_move_a_tab_end_to_end(self) -> None:
        from app.services.edit_lock_service import acquire

        admin_headers = self._auth(roles=["Hub Admin"])
        nav_a = self.client.post("/v2/nav-tabs", json={"title": "Nav A"}, headers=admin_headers).json()
        nav_b = self.client.post("/v2/nav-tabs", json={"title": "Nav B"}, headers=admin_headers).json()
        nav_a_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav_a["documentId"]).first()
        nav_b_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav_b["documentId"]).first()
        nav_a_token = acquire(self.db, ("nav_tab", nav_a_row.id), "nobody@example.com").token
        root = self.client.post(
            "/v2/tabs/",
            json={"title": "Root", "navTabDocumentId": nav_a["documentId"]},
            headers={**admin_headers, "X-Edit-Tokens": nav_a_token},
        ).json()

        nav_b_token = acquire(self.db, ("nav_tab", nav_b_row.id), "nobody@example.com").token
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}/nav-tab",
            json={"navTabDocumentId": nav_b["documentId"]},
            headers={**admin_headers, "X-Edit-Tokens": f"{nav_a_token},{nav_b_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class RemovedAccessSurfaceTests(unittest.TestCase):
    """The access-control propagation surface is gone.

    Plan §10 commit 8a built the hub router (GET/PUT/preview/purge), the
    preview/purge/reset trio on the nav-tab and tab routers, and the
    `resolved_access_control` / `resolved_parent_access_control` read-side
    fields. All of it existed to drive, preview or repair the propagation
    engine — the hub node in particular had no purpose beyond being the top
    of the cascade — so all of it was removed with the engine, ahead of a
    new access control algorithm.

    These tests pin the removal: the endpoints must genuinely be unrouted
    (404) rather than half-present, and the read-side fields must be absent
    from the response rather than serialized as null.
    """

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _token(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": "Test User", "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> dict:
        # Phase 3: the JWT "Hub Admin" claim alone admits nobody. Every
        # "Hub Admin" token in this module is the admin BYPASS, never a
        # test about a JWT-only admin, so it also gets the real
        # (hub_admin, universal) assignment (tests/_real_admin.py).
        if roles and "Hub Admin" in roles:
            seed_real_admin(self._session_factory, email)
        return {"Authorization": f"Bearer {self._token(roles, email)}"}

    def _seed_nav_and_root(self) -> tuple[dict, dict]:
        # plan_lock_propagation_2026-09-08.md §5.2 — creating the root now
        # needs a live session on its nav tab too (admin's permission
        # bypass does not extend to it, owner-confirmed 2026-09-08).
        from app.services.edit_lock_service import acquire

        admin_headers = self._auth(roles=["Hub Admin"])
        nav = self.client.post("/v2/nav-tabs", json={"title": "Nav A"}, headers=admin_headers).json()
        nav_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav["documentId"]).first()
        token = acquire(self.db, ("nav_tab", nav_row.id), "nobody@example.com").token
        root = self.client.post(
            "/v2/tabs/",
            json={"title": "Root", "navTabDocumentId": nav["documentId"]},
            headers={**admin_headers, "X-Edit-Tokens": token},
        ).json()
        return nav, root

    # -- The endpoints are unrouted ---------------------------------------

    def test_hub_router_is_gone(self) -> None:
        admin_headers = self._auth(roles=["Hub Admin"])
        ac_body = {
            "access_control": {
                "admins": {"users": [], "roles": []},
                "viewers": {"users": [], "roles": []},
            }
        }
        for method, path, body in [
            ("GET", "/v2/hub", None),
            ("PUT", "/v2/hub", ac_body),
            ("POST", "/v2/hub/preview", ac_body),
            ("POST", "/v2/hub/access/purge", {"principal": {"type": "user", "email": "a@b.com"}}),
        ]:
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, json=body, headers=admin_headers)
                self.assertEqual(resp.status_code, 404, resp.text)

    def test_preview_purge_reset_are_gone_from_both_routers(self) -> None:
        nav, root = self._seed_nav_and_root()
        admin_headers = self._auth(roles=["Hub Admin"])
        ac_body = {
            "access_control": {
                "admins": {"users": [], "roles": []},
                "viewers": {"users": [], "roles": []},
            }
        }
        purge_body = {"principal": {"type": "user", "email": "a@b.com"}}
        for prefix, doc_id in (("/v2/nav-tabs", nav["documentId"]), ("/v2/tabs", root["documentId"])):
            for suffix, body in (
                ("access/preview", ac_body),
                ("access/purge", purge_body),
                ("access/reset", None),
            ):
                path = f"{prefix}/{doc_id}/{suffix}"
                with self.subTest(path=path):
                    resp = self.client.post(path, json=body, headers=admin_headers)
                    self.assertEqual(resp.status_code, 404, resp.text)

    # -- The resolved read-side fields are gone ----------------------------

    def test_tab_workspace_no_longer_exposes_resolved_access_control(self) -> None:
        _, root = self._seed_nav_and_root()
        resp = self.client.get(
            f"/v2/tabs/{root['documentId']}/workspace", headers=self._auth(roles=["Hub Admin"])
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("resolved_access_control", resp.json()["data"])

    def test_sbn_workspace_no_longer_exposes_resolved_parent_access_control(self) -> None:
        _, root = self._seed_nav_and_root()
        gridstack = get_gridstack_by_document_id(self.db, root["documentId"])
        sbn_root = ComponentV2(
            link="sbn-root-1", type=SBN_ROOT_TYPE, gridstack_id=gridstack.id, access_control=None
        )
        self.db.add(sbn_root)
        self.db.commit()

        resp = self.client.get(
            f"/v2/sbn/{sbn_root.link}/workspace", headers=self._auth(roles=["Hub Admin"])
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("resolved_parent_access_control", resp.json()["data"])

    # -- An access_control write is now a plain per-node write -------------

    def test_writing_a_tabs_ac_does_not_touch_its_nav_tab_or_variants(self) -> None:
        """The single most load-bearing behaviour change: rule 1 used to
        push a newly added admin up into every ancestor's viewers and down
        into every descendant. Nothing outside the written node moves now."""
        from app.services.edit_lock_service import acquire

        admin_headers = self._auth(roles=["Hub Admin"])
        nav, root = self._seed_nav_and_root()
        root_row = self.db.query(TabV2).filter(TabV2.document_id == root["documentId"]).first()
        root_token = acquire(self.db, ("tab", root_row.id), "nobody@example.com").token
        variant = self.client.post(
            f"/v2/tabs/{root['documentId']}/variants",
            json={"title": "Variant A"},
            headers={**admin_headers, "X-Edit-Tokens": root_token},
        )
        self.assertEqual(variant.status_code, 200, variant.text)

        nav_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav["documentId"]).first()
        nav_ac_before = nav_row.access_control
        variant_row = (
            self.db.query(TabV2)
            .filter(TabV2.document_id == variant.json()["documentId"])
            .first()
        )
        variant_ac_before = variant_row.access_control

        new_ac = {
            "admins": {"users": [{"email": "solo@example.com"}], "roles": []},
            "viewers": {"users": [{"email": "solo@example.com"}], "roles": []},
        }
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}",
            json={"access_control": new_ac},
            headers={**admin_headers, "X-Edit-Tokens": root_token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        root_row = self.db.query(TabV2).filter(TabV2.document_id == root["documentId"]).first()
        self.db.refresh(root_row)
        self.assertEqual(root_row.access_control, new_ac)

        # Neither the ancestor nav tab nor the descendant variant moved.
        self.db.refresh(nav_row)
        self.db.refresh(variant_row)
        self.assertEqual(nav_row.access_control, nav_ac_before)
        self.assertEqual(variant_row.access_control, variant_ac_before)

    def test_a_new_node_no_longer_inherits_its_parents_ac(self) -> None:
        """Creation used to seed the new node from its parent's effective
        AC. It now stores exactly what the caller sent, or NULL."""
        from app.services.edit_lock_service import acquire

        admin_headers = self._auth(roles=["Hub Admin"])
        nav = self.client.post("/v2/nav-tabs", json={"title": "Nav A"}, headers=admin_headers).json()
        nav_row = self.db.query(NavTabV2).filter(NavTabV2.document_id == nav["documentId"]).first()
        nav_row.access_control = {
            "admins": {"users": [{"email": "parent-only@example.com"}], "roles": []},
            "viewers": {"users": [{"email": "parent-only@example.com"}], "roles": []},
        }
        self.db.commit()
        token = acquire(self.db, ("nav_tab", nav_row.id), "nobody@example.com").token

        root = self.client.post(
            "/v2/tabs/",
            json={"title": "Root", "navTabDocumentId": nav["documentId"]},
            headers={**admin_headers, "X-Edit-Tokens": token},
        ).json()
        root_row = self.db.query(TabV2).filter(TabV2.document_id == root["documentId"]).first()
        self.assertIsNone(root_row.access_control)

    # -- Removed wire-compat field is really gone --------------------------

    def test_cascade_confirmed_no_longer_accepted(self) -> None:
        admin_headers = self._auth(roles=["Hub Admin"])
        _, root = self._seed_nav_and_root()
        resp = self.client.put(
            f"/v2/tabs/{root['documentId']}",
            json={"title": "Root 2", "cascade_confirmed": True},
            headers=admin_headers,
        )
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------
# Nav tab icons (plan_ui_features_2026-08-04.md §3) — the nullable `icon`
# column plus the three-way (omit/set/clear) PUT semantics.
# ---------------------------------------------------------


class NavTabIconTests(unittest.TestCase):
    def setUp(self) -> None:
        _treat_example_com_as_org_domain(self)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)
        self.admin_headers = self._auth(roles=["Hub Admin"])

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _token(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": "Test User", "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> dict:
        # Phase 3: the JWT "Hub Admin" claim alone admits nobody. Every
        # "Hub Admin" token in this module is the admin BYPASS, never a
        # test about a JWT-only admin, so it also gets the real
        # (hub_admin, universal) assignment (tests/_real_admin.py).
        if roles and "Hub Admin" in roles:
            seed_real_admin(self._session_factory, email)
        return {"Authorization": f"Bearer {self._token(roles, email)}"}

    def test_create_with_icon_round_trips(self) -> None:
        resp = self.client.post(
            "/v2/nav-tabs",
            json={"title": "Programs", "icon": "briefcase"},
            headers=self.admin_headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["icon"], "briefcase")

        # Persisted, not just echoed back.
        listed = self.client.get("/v2/nav-tabs", headers=self.admin_headers).json()["data"]
        created = next(t for t in listed if t["title"] == "Programs")
        self.assertEqual(created["icon"], "briefcase")

    def test_create_with_no_icon_defaults_to_null(self) -> None:
        resp = self.client.post("/v2/nav-tabs", json={"title": "No Icon"}, headers=self.admin_headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["icon"])

    def test_update_icon_omitted_leaves_it_unchanged_explicit_null_clears_it(self) -> None:
        from app.services.edit_lock_service import acquire

        created = self.client.post(
            "/v2/nav-tabs",
            json={"title": "Programs", "icon": "briefcase"},
            headers=self.admin_headers,
        ).json()
        doc_id = created["documentId"]
        row = self.db.query(NavTabV2).filter(NavTabV2.document_id == doc_id).first()
        token = acquire(self.db, ("nav_tab", row.id), "nobody@example.com").token
        headers = {**self.admin_headers, "X-Edit-Tokens": token}

        # Omitted entirely -- a plain rename must not disturb the icon.
        resp = self.client.put(f"/v2/nav-tabs/{doc_id}", json={"title": "Programs 2"}, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["icon"], "briefcase")

        # A different icon replaces it.
        resp = self.client.put(f"/v2/nav-tabs/{doc_id}", json={"icon": "calendar"}, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["icon"], "calendar")

        # Explicit null -- §3.4's "Use default" -- clears it back to NULL.
        resp = self.client.put(f"/v2/nav-tabs/{doc_id}", json={"icon": None}, headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["icon"])

    def test_icon_only_update_on_protected_dashboard_row_succeeds(self) -> None:
        from app.services.edit_lock_service import acquire

        # No dedicated "create the Dashboard row" helper in this harness --
        # promote a plain nav tab the same way migrate_nav_tabs.py does.
        dashboard = self.client.post(
            "/v2/nav-tabs", json={"title": "Dashboard"}, headers=self.admin_headers
        ).json()
        row = self.db.query(NavTabV2).filter(NavTabV2.document_id == dashboard["documentId"]).first()
        row.protected = True
        self.db.commit()
        token = acquire(self.db, ("nav_tab", row.id), "nobody@example.com").token
        headers = {**self.admin_headers, "X-Edit-Tokens": token}

        # A rename would be refused (protected) -- confirm the icon-only path
        # is NOT, per §3.3 ("no backend guard change needed").
        resp = self.client.put(
            f"/v2/nav-tabs/{dashboard['documentId']}",
            json={"title": "Renamed"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 400)

        resp = self.client.put(
            f"/v2/nav-tabs/{dashboard['documentId']}",
            json={"icon": "layout-dashboard"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["icon"], "layout-dashboard")

    def test_malformed_icon_name_422s_on_create_and_update(self) -> None:
        resp = self.client.post(
            "/v2/nav-tabs",
            json={"title": "Bad Icon", "icon": "Not_Kebab Case!"},
            headers=self.admin_headers,
        )
        self.assertEqual(resp.status_code, 422)

        created = self.client.post(
            "/v2/nav-tabs", json={"title": "Fine"}, headers=self.admin_headers
        ).json()
        resp = self.client.put(
            f"/v2/nav-tabs/{created['documentId']}",
            json={"icon": "Not_Kebab Case!"},
            headers=self.admin_headers,
        )
        self.assertEqual(resp.status_code, 422)


class ComponentLocationAccessTests(unittest.TestCase):
    """The `.../location` endpoint backs both the mirror widget's 'jump to
    original' affordance and the external component deep-link feature
    (plan_component_deep_links_2026-08-05.md §6) -- unlike the mirror
    case, a deep link is reachable by anyone who receives it outside the
    app (email, chat), so this endpoint must fail closed for a viewer who
    can't see the target rather than resolving it unconditionally.

    UPDATED for project_ac_enforcement_gap.md item 1 (2026-09-03): this
    endpoint used to check a component's own OLD per-widget
    `access_control` JSONB (Airtable role names) directly, independent of
    the resource_grants system. It now runs the same
    `ViewerAccess.is_granted` check every other tab-serving endpoint uses,
    for the reason recorded on the endpoint itself: leaving the deep-link
    check on the old model while workspace/content moved to the new one
    would let the two silently disagree (a component with no old-style AC
    but also no new-model grant is now `restricted` in a workspace fetch —
    D2, unset access control matches no one — and must be equally
    unreachable here). The old 403 is gone with it: `is_granted` gives one
    boolean, so every denial reason is now the same 404 (plan §9), not a
    403 for "explicitly restricted" and a 404 for "does not exist"."""

    def setUp(self) -> None:
        _treat_example_com_as_org_domain(self)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

        # access_visibility_service.build_node_tree requires EXACTLY ONE hub
        # row to have a root at all — without it every node is an orphan
        # (§3.3, fail-closed) and nothing but the Hub Admin bypass can ever
        # resolve, which is not what most of this class's tests are about.
        self.db.add(HubV2(document_id="hub-loc"))
        self.db.flush()

        nav = create_nav_tab_v2(self.db, title="Nav Loc")
        nav_row = get_nav_tab_by_document_id(self.db, nav["documentId"])
        self.root = create_tab_v2(self.db, title="Root Loc", nav_tab_id=nav_row.id)
        self.root_row = self.db.query(TabV2).filter(TabV2.document_id == self.root["documentId"]).first()
        self.root_gridstack = (
            self.db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == self.root_row.id, GridstackV2.parent_id.is_(None))
            .first()
        )

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _token(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": "Test User", "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, roles: list[str] | None = None, email: str = "nobody@example.com") -> dict:
        # Phase 3: the JWT "Hub Admin" claim alone admits nobody. Every
        # "Hub Admin" token in this module is the admin BYPASS, never a
        # test about a JWT-only admin, so it also gets the real
        # (hub_admin, universal) assignment (tests/_real_admin.py).
        if roles and "Hub Admin" in roles:
            seed_real_admin(self._session_factory, email)
        return {"Authorization": f"Bearer {self._token(roles, email)}"}

    def _add_component(self, link: str, *, gridstack_id: int | None = None) -> ComponentV2:
        component = ComponentV2(
            link=link,
            title=link,
            type="calendar",
            gridstack_id=gridstack_id or self.root_gridstack.id,
        )
        self.db.add(component)
        self.db.commit()
        return component

    def _hub_user(self, email: str) -> UserV2:
        row = UserV2(email=email, has_signed_in=True, created_at=datetime.now(timezone.utc))
        self.db.add(row)
        self.db.commit()
        return row

    def _grant(self, *, node: tuple[str, int], user_id: int, level: str = "view") -> None:
        node_kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[node_kind]: node_id},
                user_id=user_id,
                level=level,
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

    def _location(self, link: str, roles: list[str] | None = None, email: str = "nobody@example.com"):
        return self.client.get(
            f"/v2/tabs/components/by-link/{link}/location",
            headers=self._auth(roles, email),
        )

    def test_a_component_with_no_grant_is_404_even_for_an_ordinary_role(self) -> None:
        """D2: unset access control matches no one — an authenticated,
        role-bearing user with no resource_grants row anywhere near this
        component cannot resolve it, same as an unknown link."""
        self._add_component("loc-open")
        resp = self._location("loc-open", roles=["Hub Member"])
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_viewer_with_a_direct_grant_gets_200(self) -> None:
        component = self._add_component("loc-granted")
        allowed = self._hub_user("allowed@example.com")
        self._grant(node=("component", component.id), user_id=allowed.id)

        resp = self._location("loc-granted", roles=[], email="outsider@example.com")
        self.assertEqual(resp.status_code, 404, resp.text)

        resp = self._location("loc-granted", roles=[], email="allowed@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["componentLink"], "loc-granted")

    def test_a_grant_on_the_owning_tab_reaches_a_component_with_no_grant_of_its_own(self) -> None:
        """D3: a grant covers everything beneath it, unconditionally — the
        replacement for the old per-widget-AC test of the same name. Where
        the old model's inheritance ran through `resolved_parent_ac`'s walk
        up `access_control` JSONB, this is the resource_grants fold doing
        the identical job through a different mechanism."""
        self._add_component("loc-tab-covered")
        allowed = self._hub_user("allowed@example.com")
        self._grant(node=("tab", self.root_row.id), user_id=allowed.id)

        resp = self._location("loc-tab-covered", roles=[], email="outsider@example.com")
        self.assertEqual(resp.status_code, 404, resp.text)

        resp = self._location("loc-tab-covered", roles=[], email="allowed@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_hub_admin_bypasses_any_restriction(self) -> None:
        # No grant anywhere — the JWT Hub Admin bypass is the only thing
        # that can let this resolve.
        self._add_component("loc-admin-bypass")
        resp = self._location("loc-admin-bypass", roles=["Hub Admin"], email="admin@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_unknown_link_is_404(self) -> None:
        resp = self._location("does-not-exist", roles=["Hub Member"])
        self.assertEqual(resp.status_code, 404)

    def test_mirror_link_is_404_not_403(self) -> None:
        # A mirror is never a navigable "original" -- the access-check
        # helper rejects it the same way resolve_component_location_v2
        # itself already does (both share the same MIRROR_WIDGET_TYPE
        # guard), so this is "not found/locatable", never a permission
        # question — unaffected by which model gates viewing.
        self._add_component("loc-mirror")
        mirror = self.db.query(ComponentV2).filter(ComponentV2.link == "loc-mirror").first()
        mirror.type = MIRROR_WIDGET_TYPE
        self.db.commit()
        resp = self._location("loc-mirror", roles=["Hub Member"])
        self.assertEqual(resp.status_code, 404)

    def test_sbn_descendant_resolves_and_is_checked_against_its_own_grant(self) -> None:
        """Path resolution down an SBN chain is unchanged. The access check
        is not: a grant on the SBN ROOT does not reach its leaves (D3 runs
        the other way — a grant covers what's BELOW it, and the leaf is
        below the root, so this is actually the ordinary case, re-proven
        for the SBN parent axis specifically, since `_component_parent`
        resolves it through `super_blocknote_id` rather than a gridstack)."""
        sbn_root = ComponentV2(
            link="loc-sbn-root", title="Loc SBN Root", type=SBN_ROOT_TYPE, gridstack_id=self.root_gridstack.id
        )
        self.db.add(sbn_root)
        self.db.commit()
        leaf = create_sbn_node(self.db, "loc-sbn-root", "Leaf", content=[{"type": "paragraph"}])

        allowed = self._hub_user("allowed@example.com")
        self._grant(node=("component", sbn_root.id), user_id=allowed.id)

        # The leaf carries no grant of its own, but the root's covers it —
        # D3, applied down the super_blocknote_id axis.
        resp = self._location(leaf["documentId"], roles=[], email="allowed@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["sbnPath"], ["loc-sbn-root", leaf["documentId"]])

        resp = self._location(leaf["documentId"], roles=[], email="outsider@example.com")
        self.assertEqual(resp.status_code, 404, resp.text)

        # The root itself is reachable too — it IS the seed.
        resp = self._location("loc-sbn-root", roles=[], email="allowed@example.com")
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self._location("loc-sbn-root", roles=[], email="outsider@example.com")
        self.assertEqual(resp.status_code, 404, resp.text)


if __name__ == "__main__":
    unittest.main()
