"""Access control on the Super Block Note surface
(plan_access_control_algorithm_2026-08-27.md §5.2, §6.3, §9) — the gap the
2026-09-09 plan audit found: `super_blocknote_v2.py` carried
`Depends(get_current_user)` and nothing else, so every SBN sub-tab in the hub
was readable, writable, creatable, deletable, reorderable and lockable by any
signed-in caller, addressed by its own `link`, independent of the fold gating
the tab it sits on.

WHY THIS IS ITS OWN FILE rather than more classes in
`test_tab_visibility_wiring.py`: the node kind under test is different in a
way that changes what can go wrong. Every node here is a `ComponentV2`, the
parent chain is `super_blocknote_id` rather than a gridstack chain, and it
nests to arbitrary depth (§3.1's branch 1 — "live data has chains four deep").
The tab file's fixture has no SBN tree at all.

SAME FIXTURE CONVENTIONS as that file, deliberately, so the two read alike:
no roles, scopes or assignments — every grant is a direct per-user grant (D5),
because what is under test is the wiring, not role/scope matching
(`test_resource_grants.py` and `test_rbac_graph.py` own that). The one
exception is the Hub Admin JWT bypass, which needs no DB row at all.

THE TWO ASYMMETRIES THIS FILE EXISTS TO PIN, both of which are easy to
implement backwards and neither of which any other test covers:

  1. **A revealed node's own text is withheld, its chrome is not** (§5.2).
     `granted` gates the payload — and §5.2 names "BlockNote text" in that
     list explicitly — while `visible` gates the chrome. An SBN node reached
     only because a descendant is granted must return its title and its
     visible children (so the caller can navigate through to that
     descendant) and `content: null`.
  2. **Delete is gated on the PARENT, not the node** (§6.3). `edit` folds
     down, so `edit(parent(n))` implies `edit(n)` but never the reverse: a
     caller granted edit on one sub-tab can rewrite it and cannot delete it
     out of a tree they hold nothing else on.
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-sbn-access-control-tests-only")
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
from app.services.access_visibility_service import GRIDSTACK_WIDGET_TYPE  # noqa: E402
from app.services.super_blocknote_service import SBN_LEAF_TYPE, SBN_ROOT_TYPE  # noqa: E402

HUB_ADMIN = "Hub Admin"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _blocks(text: str) -> list[dict]:
    """A minimal BlockNote doc — the shape `_write_component_data` stores for
    a leaf and `_resolve_component_data` hands back."""
    return [{"type": "paragraph", "content": text}]


class SbnAccessFixture(unittest.TestCase):
    # -- the tree -----------------------------------------------------------
    #
    #  hub -> nav1 -> tabA -> gA -> sbnRoot          (the widget itself)
    #                                ├── leafA
    #                                └── leafB
    #                                      └── leafB1
    #
    #  alice : view on leafA
    #  bob   : edit on leafB1   -> reveals leafB and sbnRoot as shells
    #  carol : nothing at all

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

        # The full override INCLUDING `finally: db.close()` — see
        # test_resource_grants_api.py's docstring for why the one-liner other
        # fixtures use makes "nothing was written" assertions pass vacuously
        # under a StaticPool.
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

    # -- construction -------------------------------------------------------

    def _sbn_node(
        self, link: str, *, parent: ComponentV2 | None, text: str, node_type: str = SBN_LEAF_TYPE
    ) -> ComponentV2:
        component = ComponentV2(
            link=link,
            type=node_type,
            title=link,
            props={"locked": False, "locked_by": "", "order": 0},
            gridstack_id=self.gA.id,
            super_blocknote_id=parent.id if parent is not None else None,
        )
        self.db.add(component)
        self.db.flush()
        # Written directly rather than through `_write_component_data` so the
        # fixture depends on the storage shape only, not on the write path
        # this file is also testing. That shape differs by type, per
        # `_raw_component_data`: a `block_note` leaf stores the bare
        # `Block[]` and is wrapped into `{"content": [...]}` on read, while
        # every other type's stored value already IS its `data` dict.
        stored = _blocks(text) if node_type == SBN_LEAF_TYPE else {"content": _blocks(text)}
        page_content = PageContentV2(content=stored)
        self.db.add(page_content)
        self.db.flush()
        component.page_content_id = page_content.id
        self.db.flush()
        return component

    def _build_tree(self) -> None:
        self.hub = HubV2(document_id="hub-1")
        self.db.add(self.hub)
        self.nav1 = NavTabV2(
            document_id="nav-1", slug="dashboard", title="Dashboard", protected=False
        )
        self.db.add(self.nav1)
        self.db.flush()

        self.tabA = TabV2(document_id="tab-a", title="Root A", nav_tab_id=self.nav1.id)
        self.db.add(self.tabA)
        self.db.flush()

        # A root gridstack shares its owning tab's document_id (create_tab_v2
        # does the same) — load-bearing for `_owner_of_gridstack`, which is
        # how an SBN ROOT reaches the tab above it.
        self.gA = GridstackV2(document_id="tab-a", parent_id=None, parent_tab_id=self.tabA.id)
        self.db.add(self.gA)
        self.db.flush()
        self.db.add(
            ComponentV2(
                link=f"rep-{self.gA.id}",
                type=GRIDSTACK_WIDGET_TYPE,
                gridstack_id=self.gA.id,
                current_grid_id=self.gA.id,
            )
        )
        self.db.flush()

        self.sbn_root = self._sbn_node(
            "sbn-root", parent=None, text="root text", node_type=SBN_ROOT_TYPE
        )
        self.leaf_a = self._sbn_node("sbn-a", parent=self.sbn_root, text="alice text")
        self.leaf_b = self._sbn_node("sbn-b", parent=self.sbn_root, text="b text")
        self.leaf_b1 = self._sbn_node("sbn-b1", parent=self.leaf_b, text="b1 text")

        self.alice = UserV2(email="alice@renphil.org", has_signed_in=True, created_at=_now())
        self.bob = UserV2(email="bob@renphil.org", has_signed_in=True, created_at=_now())
        self.carol = UserV2(email="carol@renphil.org", has_signed_in=True, created_at=_now())
        self.db.add_all([self.alice, self.bob, self.carol])
        self.db.flush()

        self.grant(user=self.alice, node=("component", self.leaf_a.id), level="view")
        self.grant(user=self.bob, node=("component", self.leaf_b1.id), level="edit")

    def grant(self, *, user: UserV2, node: tuple[str, int], level: str = "view") -> None:
        """A direct per-user grant (D5) — no role, no scope, no assignment."""
        node_kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[node_kind]: node_id},
                user_id=user.id,
                level=level,
                created_at=_now(),
            )
        )
        self.db.commit()

    # -- auth ---------------------------------------------------------------

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
        """A REAL Hub Admin (phase 3: the JWT claim alone no longer counts),
        seeded lazily on first use via `tests/_real_admin.py`. Same reasoning
        as `test_tab_visibility_wiring.TabVisibilityWiringFixture.admin`."""
        seed_real_admin(self._session_factory, "admin@renphil.org")
        return self._auth("admin@renphil.org", [HUB_ADMIN])

    def user(self, hub_user: UserV2) -> dict:
        return self._auth(hub_user.email, [])

    # -- edit-session tokens ------------------------------------------------
    #
    # plan_component_locking_and_sbn_2026-09-17.md §6: every SBN write now
    # ALSO needs a live edit session covering the node (own row, or any
    # ancestor's), presented via `X-Edit-Tokens` — the same pairing
    # `test_tab_visibility_wiring.py`'s write-side classes gained under
    # plan_lock_propagation §5.2. Auth runs BEFORE the session check, so a
    # token never masks a 403/404: every "refused" test below still sends
    # none. Service-level `acquire`, same as that file's `_edit_token`.

    def _edit_token(self, node: tuple[str, int], holder: str) -> str:
        from app.services.edit_lock_service import acquire

        return acquire(self.db, node, holder).token

    def with_token(self, headers: dict, node: tuple[str, int], holder: str) -> dict:
        return {**headers, "X-Edit-Tokens": self._edit_token(node, holder)}

    # -- calls --------------------------------------------------------------

    def get(self, path: str, headers: dict):
        return self.client.get(path, headers=headers)

    def put(self, path: str, headers: dict, json: dict | None = None):
        return self.client.put(path, json=json or {}, headers=headers)

    def post(self, path: str, headers: dict, json: dict | None = None):
        return self.client.post(path, json=json or {}, headers=headers)

    def delete(self, path: str, headers: dict):
        return self.client.delete(path, headers=headers)

    def reload(self, link: str) -> ComponentV2 | None:
        """Re-read a node by LINK, not by holding an ORM instance across the
        request. A delete test's instance is both expired and gone, so
        touching `component.id` on it raises `ObjectDeletedError` before any
        assertion runs — which would turn "the row is gone" (the thing being
        asserted) into an error rather than a pass."""
        self.db.expire_all()
        return self.db.query(ComponentV2).filter(ComponentV2.link == link).first()

    def stored_content(self, link: str):
        """What is actually on disk for a node — the assertion that makes a
        "refused" test mean something. `ComponentV2` has no `page_content`
        relationship (self-referential FKs without relationships, per its own
        docstring), so this is a second query by id rather than an attribute
        hop."""
        component = self.reload(link)
        page_content = (
            self.db.query(PageContentV2)
            .filter(PageContentV2.id == component.page_content_id)
            .first()
        )
        return page_content.content if page_content is not None else None


class TestUngrantedCallerSeesNothing(SbnAccessFixture):
    """D2, applied to the surface that had no gate at all: a signed-in caller
    with no grants gets the same 404 for a real SBN node as for a made-up
    link. Every one of these returned 200 with full content before this."""

    def test_workspace_children_and_content_all_404(self) -> None:
        for path in (
            "/v2/sbn/sbn-root/workspace",
            "/v2/sbn/sbn-root/children",
            "/v2/sbn/sbn-root/content",
            "/v2/sbn/sbn-a/workspace",
            "/v2/sbn/sbn-b1/content",
        ):
            with self.subTest(path=path):
                resp = self.get(path, headers=self.user(self.carol))
                self.assertEqual(resp.status_code, 404)

    def test_hidden_node_is_indistinguishable_from_a_missing_one(self) -> None:
        real = self.get("/v2/sbn/sbn-a/workspace", headers=self.user(self.carol))
        fake = self.get("/v2/sbn/sbn-nope/workspace", headers=self.user(self.carol))
        self.assertEqual(real.status_code, fake.status_code)
        self.assertEqual(real.json(), fake.json())


class TestHubAdminBypassesTheFold(SbnAccessFixture):
    """The same regression `test_tab_visibility_wiring.py` pins for tabs, on
    this surface: `role_assignments` holds one row in production, so without
    `ViewerAccess.full_access` every JWT-recognized Hub Admin would lose the
    SBN trees the moment this ships."""

    def test_admin_reads_every_node_fully_granted(self) -> None:
        resp = self.get("/v2/sbn/sbn-root/workspace", headers=self.admin)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"], body["revealed"]), (True, True, False))
        self.assertEqual(body["page_content"]["content"], _blocks("root text"))
        self.assertEqual({c["documentId"] for c in body["children"]}, {"sbn-a", "sbn-b"})

    def test_admin_may_write_and_delete(self) -> None:
        # The bypass is for AUTHORIZATION only — an admin still needs a
        # live session like anyone else (plan_lock_propagation decision 4).
        headers = self.with_token(self.admin, ("component", self.leaf_b1.id), "admin@renphil.org")
        resp = self.put(
            "/v2/sbn/sbn-b1/content", headers=headers, json={"content": _blocks("rewritten")}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.delete("/v2/sbn/sbn-b1", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(self.reload("sbn-b1"))


class TestRevealedNodeIsAShell(SbnAccessFixture):
    """Asymmetry 1 — §5.2. Bob holds edit on `leafB1` and nothing else."""

    def test_ancestors_are_visible_but_their_own_text_is_withheld(self) -> None:
        for link, text in (("sbn-root", "root text"), ("sbn-b", "b text")):
            with self.subTest(link=link):
                resp = self.get(f"/v2/sbn/{link}/workspace", headers=self.user(self.bob))
                self.assertEqual(resp.status_code, 200)
                body = resp.json()["data"]
                self.assertEqual(
                    (body["view"], body["edit"], body["revealed"]), (True, False, True)
                )
                self.assertIsNone(body["page_content"]["content"])
                # The chrome survives — this is what makes the shell
                # navigable rather than a wall.
                self.assertEqual(body["title"], link)

    def test_granted_leaf_keeps_its_text(self) -> None:
        resp = self.get("/v2/sbn/sbn-b1/workspace", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"], body["revealed"]), (True, True, False))
        self.assertEqual(body["page_content"]["content"], _blocks("b1 text"))

    def test_content_endpoint_agrees_with_the_workspace(self) -> None:
        shell = self.get("/v2/sbn/sbn-b/content", headers=self.user(self.bob))
        self.assertEqual(shell.status_code, 200)
        self.assertIsNone(shell.json()["data"]["content"])

        granted = self.get("/v2/sbn/sbn-b1/content", headers=self.user(self.bob))
        self.assertEqual(granted.status_code, 200)
        self.assertEqual(granted.json()["data"]["content"], _blocks("b1 text"))

    def test_siblings_outside_the_revealed_path_stay_hidden(self) -> None:
        resp = self.get("/v2/sbn/sbn-root/workspace", headers=self.user(self.bob))
        self.assertEqual({c["documentId"] for c in resp.json()["data"]["children"]}, {"sbn-b"})

        resp = self.get("/v2/sbn/sbn-root/children", headers=self.user(self.bob))
        self.assertEqual({c["documentId"] for c in resp.json()["data"]}, {"sbn-b"})

        # And `leafA` itself is not merely filtered out of the list — it is
        # unreachable by direct link too, which is the half a chrome-only
        # filter would miss.
        self.assertEqual(
            self.get("/v2/sbn/sbn-a/workspace", headers=self.user(self.bob)).status_code, 404
        )

    def test_alices_view_grant_reveals_her_own_path_only(self) -> None:
        resp = self.get("/v2/sbn/sbn-root/children", headers=self.user(self.alice))
        self.assertEqual({c["documentId"] for c in resp.json()["data"]}, {"sbn-a"})

        resp = self.get("/v2/sbn/sbn-a/workspace", headers=self.user(self.alice))
        body = resp.json()["data"]
        self.assertEqual((body["view"], body["edit"], body["revealed"]), (True, False, False))
        self.assertEqual(body["page_content"]["content"], _blocks("alice text"))


class TestTripleIsServedToTheClient(SbnAccessFixture):
    """The frontend needs no change to consume any of this: `canViewTab`
    prefers a non-null `view` over its legacy `access_control` fallback, and
    named this surface as one of the three cases that fallback existed for.
    That only works if these fields are actually on the wire."""

    def test_summary_and_workspace_carry_the_triple_and_the_node_ref(self) -> None:
        resp = self.get("/v2/sbn/sbn-root/children", headers=self.user(self.alice))
        row = next(r for r in resp.json()["data"] if r["documentId"] == "sbn-a")
        self.assertEqual((row["view"], row["edit"], row["revealed"]), (True, False, False))
        self.assertEqual((row["node_kind"], row["node_id"]), ("component", self.leaf_a.id))

        resp = self.get("/v2/sbn/sbn-a/workspace", headers=self.user(self.alice))
        body = resp.json()["data"]
        self.assertEqual((body["node_kind"], body["node_id"]), ("component", self.leaf_a.id))

    def test_edit_seed_marks_the_granted_node_not_its_descendants(self) -> None:
        # `leafB1` is bob's seed; `leafB` above it is not editable at all.
        resp = self.get("/v2/sbn/sbn-b1/workspace", headers=self.user(self.bob))
        self.assertTrue(resp.json()["data"]["edit_seed"])
        resp = self.get("/v2/sbn/sbn-b/workspace", headers=self.user(self.bob))
        self.assertFalse(resp.json()["data"]["edit_seed"])


class TestContentWrites(SbnAccessFixture):
    def test_view_only_caller_is_refused_403(self) -> None:
        resp = self.put(
            "/v2/sbn/sbn-a/content",
            headers=self.user(self.alice),
            json={"content": _blocks("alice was here")},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.stored_content("sbn-a"), _blocks("alice text"))

    def test_invisible_caller_is_refused_404_not_403(self) -> None:
        """§9's fail-closed convention carried to the write side: a refusal
        must not confirm the node exists to someone who cannot see it."""
        resp = self.put(
            "/v2/sbn/sbn-a/content",
            headers=self.user(self.carol),
            json={"content": _blocks("carol was here")},
        )
        self.assertEqual(resp.status_code, 404)

    def test_revealed_ancestor_cannot_be_written(self) -> None:
        resp = self.put(
            "/v2/sbn/sbn-b/content",
            headers=self.user(self.bob),
            json={"content": _blocks("bob was here")},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.stored_content("sbn-b"), _blocks("b text"))

    def test_editor_may_write_their_own_node(self) -> None:
        resp = self.put(
            "/v2/sbn/sbn-b1/content",
            headers=self.with_token(self.user(self.bob), ("component", self.leaf_b1.id), self.bob.email),
            json={"content": _blocks("bob was here")},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.stored_content("sbn-b1"), _blocks("bob was here"))


class TestCreate(SbnAccessFixture):
    """§6.3 "create a child of `n` → edit on `n`"."""

    def _payload(self, parent_link: str) -> dict:
        return {"title": "New sub-tab", "parentDocumentId": parent_link, "content": []}

    def test_editor_may_create_under_their_own_node(self) -> None:
        resp = self.post(
            "/v2/sbn/",
            headers=self.with_token(self.user(self.bob), ("component", self.leaf_b1.id), self.bob.email),
            json=self._payload("sbn-b1"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            self.db.query(ComponentV2)
            .filter(ComponentV2.super_blocknote_id == self.leaf_b1.id)
            .count(),
            1,
        )
        # plan_login_airtable_removal_2026-10-31.md Step 3: the echo carries
        # the post-insert triple, not null and not "invisible".
        body = resp.json()["data"] if "data" in resp.json() else resp.json()
        self.assertIs(body.get("view"), True, body)
        self.assertIs(body.get("edit"), True, body)

    def test_revealed_ancestor_is_not_a_create_target(self) -> None:
        resp = self.post("/v2/sbn/", headers=self.user(self.bob), json=self._payload("sbn-root"))
        self.assertEqual(resp.status_code, 403)

    def test_view_only_caller_may_not_create(self) -> None:
        resp = self.post("/v2/sbn/", headers=self.user(self.alice), json=self._payload("sbn-a"))
        self.assertEqual(resp.status_code, 403)

    def test_invisible_caller_gets_404(self) -> None:
        resp = self.post("/v2/sbn/", headers=self.user(self.carol), json=self._payload("sbn-a"))
        self.assertEqual(resp.status_code, 404)

    def test_nothing_was_created_by_any_refusal(self) -> None:
        before = self.db.query(ComponentV2).count()
        for headers, parent in (
            (self.user(self.bob), "sbn-root"),
            (self.user(self.alice), "sbn-a"),
            (self.user(self.carol), "sbn-a"),
        ):
            self.post("/v2/sbn/", headers=headers, json=self._payload(parent))
        self.db.expire_all()
        self.assertEqual(self.db.query(ComponentV2).count(), before)


class TestDeleteGatesOnTheParent(SbnAccessFixture):
    """Asymmetry 2 — §6.3. The one place `edit(n)` is deliberately not
    enough, and the easiest rule in this change to implement backwards."""

    def test_editor_of_a_node_may_not_delete_it(self) -> None:
        resp = self.delete("/v2/sbn/sbn-b1", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(self.reload("sbn-b1"))

    def test_editor_of_the_parent_may_delete_the_child(self) -> None:
        # Bob gains edit on `leafB`, the parent — the grant §6.3 says a
        # delete actually needs. Nothing else about the fixture changes.
        # Session on `n` itself (decision B) — see TestSbnWritesRequireA
        # LiveSession for the parent-vs-node session cases.
        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        resp = self.delete(
            "/v2/sbn/sbn-b1",
            headers=self.with_token(self.user(self.bob), ("component", self.leaf_b1.id), self.bob.email),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(self.reload("sbn-b1"))

    def test_invisible_caller_gets_404(self) -> None:
        resp = self.delete("/v2/sbn/sbn-a", headers=self.user(self.carol))
        self.assertEqual(resp.status_code, 404)
        self.assertIsNotNone(self.reload("sbn-a"))

    def test_deleting_the_sbn_root_is_still_refused_for_an_editor(self) -> None:
        """The root's own refusal is unchanged and still explains itself —
        the AC gate in front of it must not turn a deliberate 400 into a
        confusing 404 for someone who can edit the widget."""
        self.grant(user=self.bob, node=("component", self.sbn_root.id), level="edit")
        resp = self.delete("/v2/sbn/sbn-root", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 400)
        self.assertIsNotNone(self.reload("sbn-root"))

    def test_deleting_the_sbn_root_fails_closed_for_everyone_else(self) -> None:
        resp = self.delete("/v2/sbn/sbn-root", headers=self.user(self.carol))
        self.assertEqual(resp.status_code, 404)


class TestRenameAndReorder(SbnAccessFixture):
    def test_editor_may_rename_their_own_node(self) -> None:
        resp = self.put(
            "/v2/sbn/sbn-b1",
            headers=self.with_token(self.user(self.bob), ("component", self.leaf_b1.id), self.bob.email),
            json={"title": "Renamed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.reload("sbn-b1").title, "Renamed")

    def test_view_only_caller_may_not_rename(self) -> None:
        resp = self.put("/v2/sbn/sbn-a", headers=self.user(self.alice), json={"title": "Renamed"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.reload("sbn-a").title, "sbn-a")

    def test_rename_cannot_be_used_to_rewrite_access_control(self) -> None:
        """`update_sbn_node` also writes the legacy `access_control` blob,
        which is still what the SBN widget's own modal edits — so the same
        gate has to cover it, or the ungated door is an authorization write
        rather than a rename."""
        resp = self.put(
            "/v2/sbn/sbn-a",
            headers=self.user(self.alice),
            json={"access_control": {"viewers": {"emails": ["alice@renphil.org"]}}},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self.reload("sbn-a").access_control)

    def test_reorder_gates_on_the_shared_parent(self) -> None:
        # Bob holds edit on `leafB1` itself but not on `leafB`, its parent —
        # the node a reorder of `leafB`'s children actually changes.
        body = {"items": [{"documentId": "sbn-b1", "order": 3}]}
        resp = self.put("/v2/sbn/reorder", headers=self.user(self.bob), json=body)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual((self.reload("sbn-b1").props or {}).get("order"), 0)

        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        resp = self.put("/v2/sbn/reorder", headers=self.user(self.bob), json=body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self.reload("sbn-b1").props or {}).get("order"), 3)

    def test_reordering_sbn_roots_fails_closed(self) -> None:
        """A batch whose members have no SBN parent resolves to `{None}`.
        `require_edit(access, None)` must refuse it rather than read the
        unresolvable parent as "no gate"."""
        body = {"items": [{"documentId": "sbn-root", "order": 2}]}
        resp = self.put("/v2/sbn/reorder", headers=self.user(self.bob), json=body)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual((self.reload("sbn-root").props or {}).get("order"), 0)


class TestLocking(SbnAccessFixture):
    """§6.3's two new rows. Before this, any signed-in caller could lock any
    SBN node in the hub and deny service to the people who can actually edit
    it — and `force: true` skipped every check there was."""

    # All four moved from `props.locked*` to the real lock columns
    # 2026-09-17 (plan_component_locking_and_sbn §3/§6) and gained
    # token/expiry assertions; the AC verdicts they pin are unchanged.

    def test_view_only_caller_may_not_lock(self) -> None:
        resp = self.put("/v2/sbn/sbn-a/lock", headers=self.user(self.alice), json={})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.reload("sbn-a").locked)

    def test_invisible_caller_may_not_lock(self) -> None:
        resp = self.put("/v2/sbn/sbn-a/lock", headers=self.user(self.carol), json={})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(self.reload("sbn-a").locked)

    def test_editor_may_lock_and_unlock(self) -> None:
        resp = self.put("/v2/sbn/sbn-b1/lock", headers=self.user(self.bob), json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["data"]
        self.assertTrue(body["lock_token"])
        self.assertIsNotNone(body["lock_expires_at"])
        row = self.reload("sbn-b1")
        self.assertTrue(row.locked)
        self.assertEqual(row.locked_by, "bob@renphil.org")
        self.assertEqual(row.lock_token, body["lock_token"])
        self.assertIsNotNone(row.locked_at)
        # The retired props keys (this fixture seeds the legacy shape, as
        # live rows still carry it) are left exactly as found — no backfill,
        # no longer written (plan §3).
        self.assertEqual(row.props, {"locked": False, "locked_by": "", "order": 0})

        resp = self.put("/v2/sbn/sbn-b1/unlock", headers=self.user(self.bob), json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["data"]["lock_token"])
        row = self.reload("sbn-b1")
        self.assertFalse(row.locked)
        self.assertIsNone(row.lock_token)

    def test_force_unlock_no_longer_skips_the_grant_check(self) -> None:
        self.put("/v2/sbn/sbn-b1/lock", headers=self.user(self.bob), json={})
        resp = self.put(
            "/v2/sbn/sbn-b1/unlock", headers=self.user(self.alice), json={"force": True}
        )
        self.assertIn(resp.status_code, (403, 404))
        self.assertTrue(self.reload("sbn-b1").locked)

    def test_lock_state_is_stamped_on_summaries_and_workspaces(self) -> None:
        """§6: read routes thread `lock_view`, so an SBN row carries the
        same derived `lock_state` block a tab summary does."""
        self.put("/v2/sbn/sbn-b1/lock", headers=self.user(self.bob), json={})
        # Bob's own view: `self` on the node he holds AND on its ancestors
        # (`LockView.state_for` reads any lock in the viewer's own chain —
        # above or below — as "self", never as blocking them).
        ws = self.get("/v2/sbn/sbn-b1/workspace", headers=self.user(self.bob))
        self.assertEqual(ws.json()["data"]["lock_state"], "self")
        self.assertEqual(ws.json()["data"]["lock_holder"], self.bob.email)
        parent = self.get("/v2/sbn/sbn-b/workspace", headers=self.user(self.bob))
        self.assertEqual(parent.json()["data"]["lock_state"], "self")
        # An admin's view of the same: locked_here on b1, blocked_by_
        # descendant on b and on the root, and the children list agrees.
        ws = self.get("/v2/sbn/sbn-b1/workspace", headers=self.admin)
        self.assertEqual(ws.json()["data"]["lock_state"], "locked_here")
        parent = self.get("/v2/sbn/sbn-b/workspace", headers=self.admin)
        self.assertEqual(parent.json()["data"]["lock_state"], "blocked_by_descendant")
        self.assertEqual(parent.json()["data"]["lock_holder_node_label"], "sbn-b1")
        children = self.get("/v2/sbn/sbn-b/children", headers=self.admin).json()["data"]
        self.assertEqual(children[0]["lock_state"], "locked_here")
        self.assertIsNone(ws.json()["data"]["lock_token"])  # never on a read


class TestSbnWritesRequireALiveSession(SbnAccessFixture):
    """plan_component_locking_and_sbn_2026-09-17.md §6/§12: every SBN write
    now follows `gridstack_service`'s mechanical pair — `require_edit` then
    `require_live_session` on the same node — with the one owner-locked
    deviation (decision B: delete's session check is on `n`, not
    `parent(n)`). Bob holds `edit` on `leafB1` throughout; grants are added
    per test where the AC gate needs more."""

    def _bob_with(self, node: tuple[str, int]) -> dict:
        return self.with_token(self.user(self.bob), node, self.bob.email)

    def _stale(self, node: tuple[str, int]) -> None:
        from app.config import get_settings

        row = self.db.query(ComponentV2).filter(ComponentV2.id == node[1]).first()
        row.locked_at = _now() - timedelta(seconds=get_settings().TAB_LOCK_TTL_SECONDS + 60)
        self.db.commit()

    # -- content write: every covering session passes, none/expired refused --

    def test_content_write_with_no_token_is_refused_as_missing(self) -> None:
        resp = self.put(
            "/v2/sbn/sbn-b1/content", headers=self.user(self.bob), json={"content": _blocks("x")}
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self.stored_content("sbn-b1"), _blocks("b1 text"))

    def test_content_write_passes_on_every_covering_session(self) -> None:
        """Own node, SBN parent, SBN root, the canvas's tab — each covers
        the write on its own (ancestor sessions cover descendant writes, as
        everywhere else in the tree). The nav tab is NOT in this list any
        more: nav-tab locks are structure-only (plan_node_session_gaps §4),
        see `test_a_nav_tab_session_alone_does_not_cover_a_content_write`.
        The tab session is a service-level acquire (Bob has no `edit` there,
        but `acquire` is not the AC gate; auth passed on `leafB1`)."""
        for node in (
            ("component", self.leaf_b1.id),
            ("component", self.leaf_b.id),
            ("component", self.sbn_root.id),
            ("tab", self.tabA.id),
        ):
            with self.subTest(node=node):
                headers = self._bob_with(node)
                resp = self.put(
                    "/v2/sbn/sbn-b1/content", headers=headers, json={"content": _blocks(str(node))}
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(self.stored_content("sbn-b1"), _blocks(str(node)))
                from app.services.edit_lock_service import release

                release(self.db, node, self.bob.email)

    def test_a_nav_tab_session_alone_does_not_cover_a_content_write(self) -> None:
        """FLIPPED 2026-09-30 (was the `("nav_tab", …)` case of the test
        above): a nav-tab session covers only the nav tab (plan
        node_session_gaps §4, option 2; O2 "editing a nav tab never makes
        canvases editable"), so an SBN write under it with only that token
        is EDIT_SESSION_MISSING and writes nothing."""
        headers = self._bob_with(("nav_tab", self.nav1.id))
        resp = self.put("/v2/sbn/sbn-b1/content", headers=headers, json={"content": _blocks("nav only")})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self.stored_content("sbn-b1"), _blocks("b1 text"))

    def test_an_expired_session_is_refused_as_expired_and_writes_nothing(self) -> None:
        headers = self._bob_with(("component", self.leaf_b1.id))
        self._stale(("component", self.leaf_b1.id))
        resp = self.put("/v2/sbn/sbn-b1/content", headers=headers, json={"content": _blocks("x")})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")
        self.assertEqual(self.stored_content("sbn-b1"), _blocks("b1 text"))

    def test_auth_still_runs_before_the_session_check(self) -> None:
        """§5.2's ordering: a caller with no edit grant gets 403/404, never
        a 409 that would reveal the node's lock state."""
        resp = self.put(
            "/v2/sbn/sbn-a/content",
            headers=self.with_token(self.user(self.alice), ("component", self.leaf_a.id), self.alice.email),
            json={"content": _blocks("x")},
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.put("/v2/sbn/sbn-a/content", headers=self.user(self.carol), json={"content": _blocks("x")})
        self.assertEqual(resp.status_code, 404)

    # -- create / rename ----------------------------------------------------

    def test_create_needs_a_session_on_the_parent_or_above(self) -> None:
        payload = {"title": "New", "parentDocumentId": "sbn-b1", "content": []}
        resp = self.post("/v2/sbn/", headers=self.user(self.bob), json=payload)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        resp = self.post("/v2/sbn/", headers=self._bob_with(("component", self.sbn_root.id)), json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_rename_needs_a_session_on_the_node_or_above(self) -> None:
        resp = self.put("/v2/sbn/sbn-b1", headers=self.user(self.bob), json={"title": "Renamed"})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.reload("sbn-b1").title, "sbn-b1")
        resp = self.put(
            "/v2/sbn/sbn-b1", headers=self._bob_with(("tab", self.tabA.id)), json={"title": "Renamed"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- delete: decision B -------------------------------------------------

    def test_delete_passes_with_a_session_on_n_itself_decision_b(self) -> None:
        """DECISION B: `require_edit` is on `parent(n)` (unchanged) but the
        session check is on `n` — the node whose row carries the SBN's only
        Delete affordance. Bob's session on b1 + edit on b deletes b1."""
        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        resp = self.delete("/v2/sbn/sbn-b1", headers=self._bob_with(("component", self.leaf_b1.id)))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(self.reload("sbn-b1"))

    def test_delete_passes_with_an_ancestor_session_too(self) -> None:
        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        resp = self.delete("/v2/sbn/sbn-b1", headers=self._bob_with(("component", self.sbn_root.id)))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(self.reload("sbn-b1"))

    def test_delete_with_no_session_is_refused_and_the_node_survives(self) -> None:
        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        resp = self.delete("/v2/sbn/sbn-b1", headers=self.user(self.bob))
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertIsNotNone(self.reload("sbn-b1"))

    def test_delete_auth_on_the_parent_still_runs_first(self) -> None:
        """Bob's session on b1 does not buy him a delete he lacks the
        parent grant for — 403, not 409."""
        resp = self.delete("/v2/sbn/sbn-b1", headers=self._bob_with(("component", self.leaf_b1.id)))
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(self.reload("sbn-b1"))

    # -- reorder: decision 9, session-exempt but hold-aware ------------------

    def test_reorder_needs_no_token_but_is_refused_while_a_sibling_is_held_by_someone_else(self) -> None:
        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        body = {"items": [{"documentId": "sbn-b1", "order": 3}]}
        # No token at all: passes (session-exempt).
        resp = self.put("/v2/sbn/reorder", headers=self.user(self.bob), json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        # Alice holds b1 fresh: refused, and the order is untouched.
        self._edit_token(("component", self.leaf_b1.id), self.alice.email)
        body = {"items": [{"documentId": "sbn-b1", "order": 5}]}
        resp = self.put("/v2/sbn/reorder", headers=self.user(self.bob), json=body)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "NODE_LOCKED")
        self.assertEqual((self.reload("sbn-b1").props or {}).get("order"), 3)

    def test_reorder_passes_on_the_callers_own_hold(self) -> None:
        self.grant(user=self.bob, node=("component", self.leaf_b.id), level="edit")
        self._edit_token(("component", self.leaf_b1.id), self.bob.email)
        body = {"items": [{"documentId": "sbn-b1", "order": 7}]}
        resp = self.put("/v2/sbn/reorder", headers=self.user(self.bob), json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((self.reload("sbn-b1").props or {}).get("order"), 7)

    # -- the service directly, not through the router ------------------------

    def test_the_service_gate_holds_when_called_directly(self) -> None:
        """Not only through the HTTP door: `update_sbn_content` and
        `delete_sbn_subtree` refuse an empty-token session themselves, and
        `lock_sbn_node` hands back the grant the router echoes. `access=None`
        (internal caller) is deliberately used so this isolates the SESSION
        gate from the AC one."""
        from app.services.edit_lock_service import EditSession, EditSessionError
        from app.services.super_blocknote_service import (
            delete_sbn_subtree,
            lock_sbn_node,
            update_sbn_content,
        )

        empty = EditSession(holder=self.bob.email, tokens=frozenset())
        with self.assertRaises(EditSessionError) as ctx:
            update_sbn_content(self.db, "sbn-b1", _blocks("direct"), session=empty)
        self.assertEqual(ctx.exception.code, "EDIT_SESSION_MISSING")
        with self.assertRaises(EditSessionError):
            delete_sbn_subtree(self.db, "sbn-b1", session=empty)
        self.assertIsNotNone(self.reload("sbn-b1"))

        locked = lock_sbn_node(self.db, "sbn-b1", self.bob.email)
        self.assertTrue(locked["lock_token"])
        live = EditSession(holder=self.bob.email, tokens=frozenset({locked["lock_token"]}))
        self.assertIsNotNone(update_sbn_content(self.db, "sbn-b1", _blocks("direct"), session=live))
        self.assertEqual(self.stored_content("sbn-b1"), _blocks("direct"))
        # A leaked token is useless to another identity.
        stolen = EditSession(holder=self.alice.email, tokens=frozenset({locked["lock_token"]}))
        with self.assertRaises(EditSessionError):
            update_sbn_content(self.db, "sbn-b1", _blocks("stolen"), session=stolen)
        self.assertIsNotNone(delete_sbn_subtree(self.db, "sbn-b1", session=live))
        self.assertIsNone(self.reload("sbn-b1"))


class TestInternalCallersAreUnaffected(SbnAccessFixture):
    """`access=None` means "no check requested", never "deny" — the same
    convention every other `ViewerAccess | None` in the codebase uses. A
    service-level caller that never intended to gate keeps working."""

    def test_service_functions_are_ungated_without_access(self) -> None:
        from app.services.super_blocknote_service import (
            get_sbn_content,
            get_sbn_workspace,
            update_sbn_content,
        )

        workspace = get_sbn_workspace(self.db, "sbn-root")
        self.assertIsNotNone(workspace)
        self.assertEqual(workspace["page_content"]["content"], _blocks("root text"))
        self.assertNotIn("view", workspace)

        self.assertEqual(
            get_sbn_content(self.db, "sbn-b")["content"], _blocks("b text")
        )
        self.assertIsNotNone(update_sbn_content(self.db, "sbn-b", _blocks("internal")))


if __name__ == "__main__":
    unittest.main()
