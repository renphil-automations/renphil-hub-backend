"""The per-component write endpoint — plan_access_control_algorithm_2026-08-27.md §6.7.

    PUT /v2/tabs/components/by-link/{link}/content

WHAT THIS FILE IS ACTUALLY FOR. The endpoint is easy to write and easy to get
subtly wrong in one specific way: by behaving like `PUT /{id}/content`. That
route is a WHOLE-GRIDSTACK DIFF — every component on the canvas is compared
against the payload and **anything absent is deleted**. So the tests that
matter here are not "does it write the title" but "does the thing it must NOT
do still not happen": siblings survive, their page content survives, SBN
descendants survive, and the redaction sentinel never reaches
`components.type`. `TestItIsNotTheCanvasSave` is the heart of the file, and
`TestTheCanvasSaveStillDeletesByOmission` sits beside it asserting the
CONTRAST directly — without it, a per-component endpoint that silently became
a canvas diff could still pass every "sibling survives" test if the fixture
happened to send every sibling.

FIXTURES, both copied deliberately rather than by habit:

  - `test_rbac_api.py`'s dependency override, INCLUDING `finally: db.close()`.
    The one-liner every other fixture in this repo uses never closes, and on
    a StaticPool every session shares one SQLite connection — so a request
    that raises leaves its uncommitted INSERTs pending, and the next
    request's commit adopts them. Under that override
    `test_a_refused_write_persists_NOTHING` cannot fail, whatever the handler
    does.
  - `test_resource_grants.py`'s `@compiles(JSONB, "sqlite")` shim (the node
    tables all carry JSONB), `PRAGMA foreign_keys=ON` (without which the
    "SBN descendants survive" assertions would pass against a database
    enforcing nothing) and a committing `setUp`.

UPDATED for project_ac_enforcement_gap.md item 2 (2026-09-04): the default
test identity, `editor@renphil.org`, now holds a direct per-user EDIT grant
(D5) on `self.tab` — added to `setUp` so every content-BEHAVIOR test above
(does it write the title, do siblings survive, is the PAT protected...)
keeps exercising exactly what it always exercised, unaffected by the new
`edit(n)` gate this session adds. `TestAuthorization` below is the one class
that was actually ABOUT the old "login only" gate; it is rewritten, not
patched, since its old assertions are now the opposite of true.
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-component-content-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.config import get_settings  # noqa: E402
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
    AIRTABLE_PROTECTED_DATA_FIELDS,
    RESTRICTED_WIDGET_TYPE,
)

URL = "/v2/tabs/components/by-link/{link}/content"
NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class ComponentContentFixture(unittest.TestCase):
    """A real canvas: one tab, one gridstack, three ordinary sibling widgets,
    an SBN root with two descendants (one of them nested two deep), a mirror,
    and an airtable widget with a stored PAT.

    Every widget gets its OWN page_content row, because the assertion that
    matters most — "the page content of untouched components survives" — is
    vacuous if two widgets share one blob."""

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

        hub = HubV2(document_id="hub-1", created_at=NOW)
        self.db.add(hub)
        self.db.flush()

        nav = NavTabV2(document_id="nav-1", slug="dash", title="Dashboard", created_at=NOW)
        self.db.add(nav)
        self.db.flush()

        tab = TabV2(document_id="tab-1", title="Root", nav_tab_id=nav.id, created_at=NOW)
        self.db.add(tab)
        self.db.flush()
        self.tab = tab

        gs = GridstackV2(document_id="tab-1", name="Root", parent_tab_id=tab.id)
        self.db.add(gs)
        self.db.flush()
        self.gridstack = gs

        # The gridstack's own self-representation row (current_grid_id) —
        # never a real widget, and one of the things the endpoint refuses.
        self.representation = self._component(
            "rep-link", "gridstack", "(representation)", None, current_grid_id=gs.id
        )

        self.alpha = self._component("alpha-link", "block_note", "Alpha", {"content": ["A"]})
        self.beta = self._component("beta-link", "block_note", "Beta", {"content": ["B"]})
        self.gamma = self._component("gamma-link", "banner", "Gamma", {"text": "G"})

        # An SBN tree: root -> child -> grandchild. Descendants share the
        # gridstack_id but are excluded from every canvas query.
        self.sbn_root = self._component(
            "sbn-root-link", "super_block_note", "SBN", {"content": []}
        )
        self.sbn_child = self._component(
            "sbn-child-link", "block_note", "Child", {"content": ["C"]},
            super_blocknote_id=self.sbn_root.id,
        )
        self.sbn_grandchild = self._component(
            "sbn-grandchild-link", "block_note", "Grandchild", {"content": ["GC"]},
            super_blocknote_id=self.sbn_child.id,
        )

        self.mirror = self._component("mirror-link", "mirror", "Mirror", None)
        self.mirror.props = {"target_link": "alpha-link"}

        self.airtable = self._component(
            "airtable-link",
            "airtable",
            "Table",
            {
                "pat": "patSECRET",
                "patUpdatedAt": "2026-01-01T00:00:00+00:00",
                "sourceUrl": "https://airtable.com/appX/tblY",
                "personalizeEnabled": True,
                "personalizeColumn": "Owner",
                "viewConfig": {"sort": "asc"},
            },
        )

        # project_ac_enforcement_gap.md item 2: `editor@renphil.org` (the
        # default `_auth()` identity every test below authenticates as)
        # holds a direct per-user EDIT grant (D5) on `self.tab` — the fold
        # extends it to every component in this fixture, so every existing
        # content-behavior test keeps writing successfully without itself
        # being about access control. `TestAuthorization` overrides this
        # with its own zero-grant identities where it is deliberately
        # testing the gate.
        self.editor_user = UserV2(email="editor@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(self.editor_user)
        self.db.flush()
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["tab"]: self.tab.id},
                user_id=self.editor_user.id,
                level="edit",
                created_at=NOW,
            )
        )

        self.db.commit()

        app = create_app()

        # Mirrors app/db_v2/database.py's real get_db_v2, INCLUDING its
        # `finally: db.close()` — see this module's docstring for why the
        # repo's usual one-liner would make the rollback assertions vacuous.
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- builders -------------------------------------------------------

    def _component(
        self,
        link: str,
        type_: str,
        title: str,
        data: dict | None,
        *,
        super_blocknote_id: int | None = None,
        current_grid_id: int | None = None,
    ) -> ComponentV2:
        page_content_id = None
        if data is not None:
            # block_note stores the bare Block[] list; every other type
            # stores its data dict as-is. Mirrors `_write_component_data`.
            stored = data.get("content", []) if type_ in ("block_note",) else data
            pc = PageContentV2(content=stored)
            self.db.add(pc)
            self.db.flush()
            page_content_id = pc.id

        component = ComponentV2(
            link=link,
            type=type_,
            title=title,
            description=f"desc of {title}",
            x=0,
            y=0,
            width=6,
            height=6,
            gridstack_id=self.gridstack.id,
            page_content_id=page_content_id,
            super_blocknote_id=super_blocknote_id,
            current_grid_id=current_grid_id,
        )
        self.db.add(component)
        self.db.flush()
        return component

    # -- auth -----------------------------------------------------------

    def _auth(self, email: str = "editor@renphil.org", roles: list[str] | None = None) -> dict:
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

    ADMIN = "admin@renphil.org"

    def _admin_auth(self) -> dict:
        """A REAL Hub Admin. Phase 3: the JWT "Hub Admin" claim alone admits
        nobody, so this also seeds `ADMIN`'s `(hub_admin, universal)`
        assignment (`tests/_real_admin.py`, own session). Plain
        `_auth(roles=["Hub Admin"])` calls are left as they were: the one
        remaining (`test_hub_admin_by_jwt_still_sees_...`) authenticates as
        `editor@`, who passes on their own edit grant."""
        seed_real_admin(self._session_factory, self.ADMIN)
        return self._auth(email=self.ADMIN, roles=["Hub Admin"])

    # -- helpers --------------------------------------------------------

    def _session_token(self, email: str = "editor@renphil.org") -> str:
        """2026-09-16 TTL fix: an ordinary (non-SBN) widget write now takes
        the same `require_live_session` gate the canvas save has, so every
        content-BEHAVIOUR test below needs the writer to hold a live session
        on `self.tab` — acquired service-level (no AC gate; `TestWriteEnforce
        mentIsNowLive` is where AC is exercised, and auth runs BEFORE the
        session check there anyway, so a token never masks a 403/404).
        Same-holder re-entry keeps the token, so calling this twice for one
        identity is harmless."""
        from app.services.edit_lock_service import acquire

        return acquire(self.db, ("tab", self.tab.id), email).token

    def _put(self, link: str, body: dict, *, session: bool = True, **kw) -> object:
        """`session=True` (default): acquire/renew a live session for the
        `email` being authenticated as and present it via `X-Edit-Tokens`,
        exactly as `ComponentContentEditModal` does. `session=False` sends
        no token — for the handful of tests ABOUT the session gate."""
        headers = self._auth(**kw)
        if session:
            headers["X-Edit-Tokens"] = self._session_token(kw.get("email", "editor@renphil.org"))
        return self.client.put(URL.format(link=link), json=body, headers=headers)

    # Both helpers address components by their stable `link`, never by
    # holding an ORM instance across a commit. `expire_on_commit` is on, so a
    # retained instance re-loads its row on first attribute access — and
    # raises ObjectDeletedError if that row is gone. That is precisely the
    # case `TestTheCanvasSaveStillDeletesByOmission` sets up, where "the row
    # is gone" is the expected ANSWER and must come back as None rather than
    # as an exception from the assertion helper itself.
    def _reload(self, link: str) -> ComponentV2 | None:
        self.db.expire_all()
        return self.db.query(ComponentV2).filter(ComponentV2.link == link).first()

    def _stored_content(self, link: str):
        fresh = self._reload(link)
        if fresh is None or fresh.page_content_id is None:
            return None
        row = (
            self.db.query(PageContentV2)
            .filter(PageContentV2.id == fresh.page_content_id)
            .first()
        )
        return row.content if row else None


class TestItWrites(ComponentContentFixture):
    def test_it_writes_the_title(self):
        r = self._put("alpha-link", {"title": "Renamed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Renamed")

    def test_it_writes_the_description(self):
        r = self._put("alpha-link", {"description": "New description"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._reload("alpha-link").description, "New description")

    def test_it_writes_the_data(self):
        r = self._put("alpha-link", {"data": {"content": ["EDITED"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored_content("alpha-link"), ["EDITED"])

    def test_an_omitted_field_is_untouched(self):
        """The `_UNSET` contract: a caller renaming a widget must not have to
        resend its whole blob, and must not blank it by not sending it."""
        r = self._put("alpha-link", {"title": "Only the title"})
        self.assertEqual(r.status_code, 200, r.text)
        fresh = self._reload("alpha-link")
        self.assertEqual(fresh.title, "Only the title")
        self.assertEqual(fresh.description, "desc of Alpha")
        self.assertEqual(self._stored_content("alpha-link"), ["A"])

    def test_an_explicit_null_title_clears_it(self):
        """`title` is a nullable column and clearing it is meaningful — this
        is the "not sent" vs "set to null" distinction the `_UNSET` sentinel
        exists to preserve."""
        r = self._put("alpha-link", {"title": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._reload("alpha-link").title)

    def test_empty_data_is_allowed_and_empties_the_widget(self):
        r = self._put("gamma-link", {"data": {}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored_content("gamma-link"), {})

    def test_it_returns_the_updated_component(self):
        r = self._put("gamma-link", {"title": "G2", "data": {"text": "new"}})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["link"], "gamma-link")
        self.assertEqual(data["type"], "banner")
        self.assertEqual(data["title"], "G2")
        self.assertEqual(data["data"], {"text": "new"})

    def test_a_real_change_emits_a_search_receipt(self):
        r = self._put("gamma-link", {"title": "Changed"})
        self.assertEqual(
            r.json()["data"]["search_updates"],
            [{"component_id": self.gamma.id, "action": "upsert"}],
        )

    def test_a_no_op_write_emits_no_search_receipt(self):
        """Same convention as `update_sbn_content` — a save that changes
        nothing must not churn the search index."""
        r = self._put("gamma-link", {"title": "Gamma"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["search_updates"], [])

    def test_it_writes_a_component_that_had_no_page_content_yet(self):
        """A widget whose `page_content_id` is still NULL gets a row created
        on first write — `_write_component_data`'s insert branch."""
        bare = self._component("bare-link", "banner", "Bare", None)
        self.db.commit()
        self.assertIsNone(bare.page_content_id)

        r = self._put("bare-link", {"data": {"text": "first"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored_content("bare-link"), {"text": "first"})


class TestItIsNotTheCanvasSave(ComponentContentFixture):
    """THE POINT OF THE ENDPOINT. `update_tab_content_v2` deletes every
    component absent from its payload; this one names a single component and
    must leave the rest of the canvas exactly as it found it."""

    def _canvas_ids(self) -> set[int]:
        self.db.expire_all()
        return {
            c.id
            for c in self.db.query(ComponentV2)
            .filter(ComponentV2.gridstack_id == self.gridstack.id)
            .all()
        }

    def test_siblings_absent_from_the_payload_survive(self):
        before = self._canvas_ids()
        r = self._put("alpha-link", {"title": "Edited", "data": {"content": ["X"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._canvas_ids(), before)
        self.assertIsNotNone(self._reload("beta-link"))
        self.assertIsNotNone(self._reload("gamma-link"))

    def test_the_page_content_of_untouched_components_survives(self):
        r = self._put("alpha-link", {"data": {"content": ["X"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored_content("beta-link"), ["B"])
        self.assertEqual(self._stored_content("gamma-link"), {"text": "G"})

    def test_the_metadata_of_untouched_components_survives(self):
        r = self._put("alpha-link", {"title": "Edited"})
        self.assertEqual(r.status_code, 200, r.text)
        beta = self._reload("beta-link")
        self.assertEqual(beta.title, "Beta")
        self.assertEqual(beta.description, "desc of Beta")

    def test_sbn_descendants_survive(self):
        """SBN descendants share the gridstack_id, so a diff-shaped
        implementation that forgot the `super_blocknote_id IS NULL` filter
        would delete this whole tree on any single-widget save."""
        r = self._put("alpha-link", {"title": "Edited"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(self._reload("sbn-root-link"))
        self.assertIsNotNone(self._reload("sbn-child-link"))
        self.assertIsNotNone(self._reload("sbn-grandchild-link"))
        self.assertEqual(self._stored_content("sbn-grandchild-link"), ["GC"])

    def test_writing_an_sbn_descendant_leaves_its_siblings_and_parents(self):
        r = self._put("sbn-grandchild-link", {"data": {"content": ["EDITED"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored_content("sbn-grandchild-link"), ["EDITED"])
        self.assertEqual(self._stored_content("sbn-child-link"), ["C"])
        self.assertIsNotNone(self._reload("sbn-root-link"))
        self.assertIsNotNone(self._reload("alpha-link"))

    def test_the_gridstack_representation_row_survives(self):
        r = self._put("alpha-link", {"title": "Edited"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(self._reload("rep-link"))

    def test_the_mirror_survives_and_still_points_at_its_target(self):
        r = self._put("alpha-link", {"title": "Edited"})
        self.assertEqual(r.status_code, 200, r.text)
        mirror = self._reload("mirror-link")
        self.assertIsNotNone(mirror)
        self.assertEqual((mirror.props or {}).get("target_link"), "alpha-link")

    def test_no_page_content_row_is_ever_orphaned_or_removed(self):
        before = {row.id for row in self.db.query(PageContentV2).all()}
        r = self._put("alpha-link", {"data": {"content": ["X"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.db.expire_all()
        after = {row.id for row in self.db.query(PageContentV2).all()}
        self.assertEqual(before, after)


class TestTheCanvasSaveStillDeletesByOmission(ComponentContentFixture):
    """The contrast, asserted rather than described.

    Without this, every "siblings survive" test above could pass against an
    implementation that had quietly become a canvas diff — they would just be
    measuring a fixture that happened to send everything. This pins that the
    OLD path really does delete by omission, so the new path's survival is a
    property of the new path.

    It also pins that this session changed nothing about `update_tab_content_v2`.
    """

    def test_the_canvas_save_deletes_a_component_absent_from_the_payload(self):
        # plan_lock_propagation_2026-09-08.md §5.2 gave the whole-canvas
        # save its own require_live_session gate — a real lock on the tab is
        # needed here, this test predates that plan. (As of 2026-09-16 the
        # per-component path below has the same gate; `_put` handles it.)
        from app.services.edit_lock_service import acquire

        token = acquire(self.db, ("tab", self.tab.id), "editor@renphil.org").token
        r = self.client.put(
            "/v2/tabs/tab-1/content",
            headers={**self._auth(), "X-Edit-Tokens": token},
            json={
                "content": {
                    "schemaVersion": 2,
                    "layout": [{"id": str(self.alpha.id), "x": 0, "y": 0, "w": 6, "h": 6}],
                    # beta and gamma deliberately omitted
                    "widgets": {
                        str(self.alpha.id): {
                            "type": "block_note",
                            "link": "alpha-link",
                            "title": "Alpha",
                            "data": {"content": ["A"]},
                        }
                    },
                }
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._reload("beta-link"), "canvas save should have deleted beta")
        self.assertIsNone(self._reload("gamma-link"), "canvas save should have deleted gamma")
        self.assertIsNotNone(self._reload("alpha-link"))

    def test_the_per_component_path_does_not(self):
        r = self._put("alpha-link", {"title": "Alpha renamed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(self._reload("beta-link"))
        self.assertIsNotNone(self._reload("gamma-link"))


class TestTheRestrictedSentinel(ComponentContentFixture):
    """§6.7 / §10 item 7. The sentinel must never reach `components.type`, and
    this path must hold ON ITS OWN rather than inheriting the canvas save's
    guard."""

    def test_type_is_not_an_accepted_field_at_all(self):
        """The structural half. `extra="forbid"` means a client that posts a
        whole serialized widget entry — which always carries `type` — gets a
        422 instead of a partial write."""
        r = self._put("alpha-link", {"type": RESTRICTED_WIDGET_TYPE, "data": None})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._reload("alpha-link").type, "block_note")

    def test_a_full_redacted_widget_entry_is_refused_and_changes_nothing(self):
        """Exactly what a stale client holds for a widget it may not see."""
        before_type = self._reload("alpha-link").type
        before_content = self._stored_content("alpha-link")
        r = self._put("alpha-link", {"type": "restricted", "data": None})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._reload("alpha-link").type, before_type)
        self.assertEqual(self._stored_content("alpha-link"), before_content)

    def test_explicit_null_data_is_refused(self):
        """The other half of the same body. Accepting `data: null` would
        overwrite the real blob with {} — the 2026-08-30 data-loss path
        rebuilt on a new door."""
        r = self._put("alpha-link", {"data": None})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._stored_content("alpha-link"), ["A"])

    def test_a_stored_sentinel_row_cannot_be_written_to(self):
        """Defense in depth: such a row should not exist, but writing content
        into one would make a redaction artifact permanent."""
        rogue = self._component("rogue-link", RESTRICTED_WIDGET_TYPE, "Rogue", {"x": 1})
        self.db.commit()
        r = self._put("rogue-link", {"title": "attempt"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._reload("rogue-link").title, "Rogue")

    def test_no_widget_type_can_be_changed_through_this_endpoint(self):
        for candidate in ("banner", "airtable", "mirror", "gridstack"):
            with self.subTest(candidate=candidate):
                r = self._put("alpha-link", {"type": candidate})
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(self._reload("alpha-link").type, "block_note")


class TestRefusals(ComponentContentFixture):
    def test_an_unknown_link_is_404(self):
        r = self._put("no-such-link", {"title": "x"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_a_mirror_is_refused(self):
        """A mirror holds no content of its own — `_write_component_data`
        documents that it is never called for one."""
        r = self._put("mirror-link", {"data": {"anything": 1}})
        self.assertEqual(r.status_code, 400, r.text)
        mirror = self._reload("mirror-link")
        self.assertIsNone(mirror.page_content_id)
        self.assertEqual((mirror.props or {}).get("target_link"), "alpha-link")

    def test_a_gridstack_representation_row_is_refused(self):
        r = self._put("rep-link", {"data": {"anything": 1}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(self._reload("rep-link").page_content_id)

    def test_access_control_is_not_an_accepted_field(self):
        """Authorization, not content — §6.3 routes grant edits elsewhere."""
        r = self._put("alpha-link", {"access_control": {"viewers": {"roles": []}}})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIsNone(self._reload("alpha-link").access_control)

    def test_layout_fields_are_not_accepted(self):
        """§11.3 — repositioning is deliberately NOT implemented. If this test
        ever needs changing, that change is a DESIGN DECISION taken with the
        team, not a bug fix."""
        for field, value in (("x", 3), ("y", 4), ("w", 5), ("h", 6),
                             ("width", 5), ("height", 6)):
            with self.subTest(field=field):
                r = self._put("alpha-link", {field: value})
                self.assertEqual(r.status_code, 422, r.text)
        fresh = self._reload("alpha-link")
        self.assertEqual((fresh.x, fresh.y, fresh.width, fresh.height), (0, 0, 6, 6))

    def test_an_unknown_field_is_refused(self):
        r = self._put("alpha-link", {"nonsense": 1})
        self.assertEqual(r.status_code, 422, r.text)

    def test_an_empty_body_is_a_no_op_not_an_error(self):
        r = self._put("alpha-link", {})
        self.assertEqual(r.status_code, 200, r.text)
        fresh = self._reload("alpha-link")
        self.assertEqual(fresh.title, "Alpha")
        self.assertEqual(self._stored_content("alpha-link"), ["A"])

    def test_a_refused_write_persists_NOTHING(self):
        """Needs the closing dependency override to be meaningful — see the
        module docstring."""
        before_titles = {
            c.id: c.title
            for c in self.db.query(ComponentV2).all()
        }
        r = self._put("mirror-link", {"title": "attempt", "data": {"x": 1}})
        self.assertEqual(r.status_code, 400, r.text)
        self.db.expire_all()
        after_titles = {
            c.id: c.title
            for c in self.db.query(ComponentV2).all()
        }
        self.assertEqual(before_titles, after_titles)

    def test_a_failure_PART_WAY_THROUGH_the_write_rolls_the_whole_thing_back(self):
        """Every refusal above raises BEFORE the try block, so none of them
        exercises the rollback — nothing has been written yet at that point.
        Found by mutation testing: deleting `db.rollback()` left the whole
        suite green until this test existed.

        This forces the other shape — a failure AFTER a partial mutation.
        `title` is assigned, then `_write_component_data` blows up. Without
        the rollback the session still holds the dirty title, and the very
        next query on that session AUTOFLUSHES it into the database: a write
        that was refused with a 500 lands anyway, out of a request that
        reported failure.

        Driven at the service level deliberately. Through the router the
        dependency's `finally: db.close()` returns the connection to the
        pool, which resets it — so the router path would mask the missing
        rollback rather than expose it, and the guard would be untested while
        looking tested.
        """
        from unittest import mock

        from app.services import gridstack_service

        with mock.patch.object(
            gridstack_service, "_write_component_data", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                gridstack_service.update_component_content(
                    self.db,
                    "alpha-link",
                    title="Half-written",
                    data={"content": ["X"]},
                )

        # Deliberately NOT self._reload(): that calls expire_all(), which
        # would DISCARD the pending change and hide exactly what is being
        # tested. A plain query autoflushes it instead.
        fresh = (
            self.db.query(ComponentV2).filter(ComponentV2.link == "alpha-link").first()
        )
        self.assertEqual(fresh.title, "Alpha")
        self.assertEqual(self._stored_content("alpha-link"), ["A"])

    def test_a_title_over_255_characters_is_refused(self):
        r = self._put("alpha-link", {"title": "x" * 256})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Alpha")


class TestAirtableProtection(ComponentContentFixture):
    """This endpoint checks login only, exactly like the canvas save — so it
    must carry the same protection the canvas save does, or it becomes a
    PAT-wiping door on a widget whose whole point is that the token is
    write-only."""

    def test_the_stored_pat_survives_a_content_write(self):
        r = self._put("airtable-link", {"data": {"viewConfig": {"sort": "desc"}}})
        self.assertEqual(r.status_code, 200, r.text)
        stored = self._stored_content("airtable-link")
        self.assertEqual(stored["pat"], "patSECRET")
        self.assertEqual(stored["patUpdatedAt"], "2026-01-01T00:00:00+00:00")

    def test_a_client_supplied_pat_cannot_overwrite_the_stored_one(self):
        r = self._put("airtable-link", {"data": {"pat": "patINJECTED"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored_content("airtable-link")["pat"], "patSECRET")

    def test_every_protected_field_is_preserved(self):
        r = self._put(
            "airtable-link",
            {
                "data": {
                    "pat": "nope",
                    "patUpdatedAt": "1999-01-01T00:00:00+00:00",
                    "sourceUrl": "https://evil.example/appZ/tblZ",
                    "personalizeEnabled": False,
                    "personalizeColumn": "Everyone",
                    "viewConfig": {"sort": "desc"},
                }
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        stored = self._stored_content("airtable-link")
        self.assertEqual(stored["pat"], "patSECRET")
        self.assertEqual(stored["sourceUrl"], "https://airtable.com/appX/tblY")
        self.assertIs(stored["personalizeEnabled"], True)
        self.assertEqual(stored["personalizeColumn"], "Owner")
        # …while the unprotected half of the same payload IS applied.
        self.assertEqual(stored["viewConfig"], {"sort": "desc"})

    def test_the_protected_field_list_is_the_real_one(self):
        """Pins the coupling: if a field is added to the frontend's widget
        data and to AIRTABLE_PROTECTED_DATA_FIELDS, this endpoint inherits
        the protection automatically — but if the tuple is ever narrowed,
        this test is where it shows up."""
        for field in AIRTABLE_PROTECTED_DATA_FIELDS:
            with self.subTest(field=field):
                r = self._put("airtable-link", {"data": {field: "TAMPERED"}})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotEqual(self._stored_content("airtable-link").get(field), "TAMPERED")

    def test_the_pat_is_never_returned_in_the_response(self):
        r = self._put("airtable-link", {"data": {"viewConfig": {}}})
        self.assertEqual(r.status_code, 200, r.text)
        returned = r.json()["data"]["data"]
        self.assertNotIn("pat", returned)
        self.assertIs(returned["hasPat"], True)


class TestWriteEnforcementIsNowLive(ComponentContentFixture):
    """UPDATED for project_ac_enforcement_gap.md item 2 (2026-09-04) — this
    class used to be `TestAuthorization`, pinning "login only, and NOTHING
    MORE: no grant check (that is enforcement, and §10 blocks it)". The grant
    check exists now, on this endpoint specifically (§6.7's per-component
    door, gated the same way `update_tab_content_v2` is — the two "must gain
    it together" per the service function's own docstring). What survives
    unchanged: the old per-widget `access_control` JSONB is still unread by
    the new gate (§7).

    UPDATED AGAIN 2026-09-16 (TTL fix): the LOCK check now ALSO exists on
    this door for an ordinary widget — the same `require_live_session` gate
    as the canvas save, replacing decision 6's tokenless foreign-hold-only
    check (which let a modal whose lock had expired keep saving forever).
    See `_put`'s `session` flag and the two lock tests at the bottom."""

    def test_authentication_is_required(self):
        r = self.client.put(URL.format(link="alpha-link"), json={"title": "x"})
        self.assertIn(r.status_code, (401, 403))
        self.assertEqual(self._reload("alpha-link").title, "Alpha")

    def test_an_ungranted_signed_in_user_is_refused_not_found(self):
        """No role, no assignment, no grant — what `editor@renphil.org` used
        to be before `setUp` started granting it edit on `self.tab`. §9's
        fail-closed convention, carried to the write side: invisible reads as
        404, the same code a genuinely unknown link returns, not 403 — this
        caller cannot even tell the component exists."""
        r = self._put("alpha-link", {"title": "By a nobody"}, email="nobody@renphil.org", roles=[])
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Alpha")

    def test_a_view_only_grant_is_refused_forbidden_not_found(self):
        """Unlike the ungranted case above, this caller CAN see the
        component — their own UI got them to this link — so the refusal is
        403, not a 404 that would pretend it doesn't exist."""
        viewer = UserV2(email="viewer@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(viewer)
        self.db.flush()
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["tab"]: self.tab.id},
                user_id=viewer.id,
                level="view",
                created_at=NOW,
            )
        )
        self.db.commit()

        r = self._put("alpha-link", {"title": "By a viewer"}, email="viewer@renphil.org", roles=[])
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Alpha")

    def test_a_hub_admin_may_write_with_no_grant_at_all(self):
        """The bypass, exercised through this endpoint specifically — every
        other AC-gated write in this codebase applies it first and
        unconditionally, and this door is no exception."""
        seed_real_admin(self._session_factory, self.ADMIN)  # phase 3: a real admin row
        r = self._put("alpha-link", {"title": "By the admin"}, email=self.ADMIN, roles=["Hub Admin"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._reload("alpha-link").title, "By the admin")

    def test_the_old_per_widget_acl_is_not_consulted_by_the_new_gate(self):
        """NOT a bug, and pinned so it cannot be "fixed" by accident. §7: the
        existing `access_control` JSONB columns stay in place, unread — the
        new gate reads `resource_grants` exclusively. `editor@renphil.org`
        keeps writing on the strength of its `resource_grants` row alone,
        regardless of what this legacy field says."""
        self.alpha.access_control = {
            "viewers": {
                "roles": [{"name": "Hub Admin", "scope": "Hub", "program": "", "function": ""}],
                "users": [],
            }
        }
        self.db.commit()

        r = self._put("alpha-link", {"title": "Still writable"}, roles=[])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Still writable")

    def test_a_stale_foreign_lock_does_not_block_the_write(self):
        """Was `test_a_locked_tab_does_not_block_the_write`, pinning "§6.6 —
        locking is advisory and unenforced on writes". No longer true
        (2026-09-16): what this now pins is that a STALE foreign lock (here
        with no `locked_at` at all — a pre-TTL row, always stale) is
        reclaimed by the writer's own session (`_put` acquires one) and the
        write proceeds. A FRESH foreign lock refuses — that half lives in
        tests/test_tab_locking.py's `TestComponentWritesRequireALiveSession`.
        Separate axis from the grant check above: `editor@renphil.org`
        still needs (and, via `setUp`, still holds) its own edit grant."""
        self.tab.locked = True
        self.tab.locked_by = "someone.else@renphil.org"
        self.db.commit()

        r = self._put("alpha-link", {"title": "Written anyway"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Written anyway")

    def test_a_write_with_no_live_session_is_refused_as_missing(self):
        """2026-09-16 TTL fix — the reversal of decision 6 for an ordinary
        widget, exercised through THIS fixture's fully-granted editor so
        the refusal is provably about the session, not about access."""
        r = self._put("alpha-link", {"title": "No session"}, session=False)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self._reload("alpha-link").title, "Alpha")

    def test_an_sbn_descendant_write_now_needs_a_session_too(self):
        """REVERSED 2026-09-17 (plan_component_locking_and_sbn §5.2) — was
        `test_an_sbn_descendant_write_still_needs_no_session` (200), pinning
        decision 6's last surviving case. Every SBN member is a lock node
        now and the SBN editor holds a real session, so this row shape
        takes the same gate as every other; the tab-session default of
        `_put` (an ancestor) still covers it, which is what keeps
        `test_writing_an_sbn_descendant_leaves_its_siblings_and_parents`
        green with no change."""
        r = self._put("sbn-child-link", {"title": "Tokenless"}, session=False)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self._reload("sbn-child-link").title, "Child")


class TestComponentLockDoors(ComponentContentFixture):
    """plan_component_locking_and_sbn_2026-09-17.md §5.1 — the three
    per-component lock routes beside the content door, and the content
    door passing on the component's OWN token (phase A could only pin that
    at the service level; this is the end-to-end pin)."""

    LOCK = "/v2/tabs/components/by-link/{link}/lock"
    RENEW = "/v2/tabs/components/by-link/{link}/lock/renew"
    UNLOCK = "/v2/tabs/components/by-link/{link}/unlock"

    def _lock(self, link: str, body: dict | None = None, **kw):
        return self.client.put(self.LOCK.format(link=link), json=body or {}, headers=self._auth(**kw))

    def _renew(self, link: str, token: str, **kw):
        headers = {**self._auth(**kw), "X-Edit-Tokens": token}
        return self.client.put(self.RENEW.format(link=link), json={}, headers=headers)

    def _unlock(self, link: str, body: dict | None = None, **kw):
        return self.client.put(self.UNLOCK.format(link=link), json=body or {}, headers=self._auth(**kw))

    def _content(self, link: str, body: dict, token: str | None, **kw):
        headers = self._auth(**kw)
        if token is not None:
            headers["X-Edit-Tokens"] = token
        return self.client.put(URL.format(link=link), json=body, headers=headers)

    # -- the doors themselves --------------------------------------------

    def test_lock_returns_the_token_and_expiry_and_writes_the_row(self):
        r = self._lock("alpha-link")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()["data"]
        self.assertEqual((body["link"], body["type"], body["title"]), ("alpha-link", "block_note", "Alpha"))
        self.assertTrue(body["locked"])
        self.assertEqual(body["locked_by"], "editor@renphil.org")
        self.assertTrue(body["lock_token"])
        self.assertIsNotNone(body["lock_expires_at"])
        row = self._reload("alpha-link")
        self.assertEqual((row.locked, row.locked_by, row.lock_token), (True, "editor@renphil.org", body["lock_token"]))
        # The canvas itself is NOT locked — the whole point of §5.1.
        self.db.expire_all()
        self.assertFalse(self.db.query(TabV2).filter(TabV2.id == self.tab.id).first().locked)

    def test_renew_returns_the_same_token_and_a_later_expiry_never_on_a_stale_session(self):
        token = self._lock("alpha-link").json()["data"]["lock_token"]
        r = self._renew("alpha-link", token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["lock_token"], token)
        self.assertIsNotNone(r.json()["data"]["lock_expires_at"])

        row = self._reload("alpha-link")
        row.locked_at = datetime.now(timezone.utc) - timedelta(seconds=get_settings().TAB_LOCK_TTL_SECONDS + 60)
        self.db.commit()
        r = self._renew("alpha-link", token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")

    def test_unlock_clears_the_row_and_returns_no_token(self):
        self._lock("alpha-link")
        r = self._unlock("alpha-link")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["data"]["locked"])
        self.assertIsNone(r.json()["data"]["lock_token"])
        row = self._reload("alpha-link")
        self.assertEqual((bool(row.locked), row.locked_by, row.lock_token), (False, "", None))

    def test_an_unknown_link_is_404_on_all_three(self):
        self.assertEqual(self._lock("nope").status_code, 404)
        self.assertEqual(self._renew("nope", "x").status_code, 404)
        self.assertEqual(self._unlock("nope").status_code, 404)

    def test_no_edit_grant_is_refused_before_anything_is_learned(self):
        """Ungranted → 404 (invisible); view-only → 403. Neither row is
        touched, and neither caller learns whether it was locked."""
        r = self._lock("alpha-link", email="nobody@renphil.org", roles=[])
        self.assertEqual(r.status_code, 404, r.text)
        viewer = UserV2(email="viewer@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(viewer)
        self.db.flush()
        self.db.add(ResourceGrantV2(**{NODE_COLUMNS["tab"]: self.tab.id}, user_id=viewer.id, level="view", created_at=NOW))
        self.db.commit()
        r = self._lock("alpha-link", email="viewer@renphil.org", roles=[])
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(self._reload("alpha-link").locked)

    def test_a_representation_row_is_refused_400_lock_the_sub_tab_through_its_own_route(self):
        r = self._lock("rep-link")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("sub-tab", r.json()["detail"])
        self.assertFalse(self._reload("rep-link").locked)

    def test_a_stored_sentinel_row_is_refused_400(self):
        self._component("sentinel-link", "restricted", "(sentinel)", None)
        self.db.commit()
        r = self._lock("sentinel-link")
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_second_editor_on_a_sibling_widget_is_not_refused(self):
        """The one behaviour that flips (plan §2.1 row 1): two narrow
        editors on different widgets of one canvas no longer exclude each
        other. Both hold `edit(tab)` here via a second grant."""
        other = UserV2(email="other@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(other)
        self.db.flush()
        self.db.add(ResourceGrantV2(**{NODE_COLUMNS["tab"]: self.tab.id}, user_id=other.id, level="edit", created_at=NOW))
        self.db.commit()
        self.assertEqual(self._lock("alpha-link").status_code, 200)
        r = self._lock("beta-link", email="other@renphil.org", roles=[])
        self.assertEqual(r.status_code, 200, r.text)
        # But the SAME widget is refused naming Alpha, and a THIRD party
        # trying the canvas above is refused naming both holders (§2.1 row
        # 4 — `other`'s own Beta would not block `other`, so use the admin).
        r = self._lock("alpha-link", email="other@renphil.org", roles=[])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "NODE_LOCKED")
        r = self.client.put("/v2/tabs/tab-1/lock", json={}, headers=self._admin_auth())
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual({b["node_label"] for b in r.json()["detail"]["blocking"]}, {"Alpha", "Beta"})

    def test_a_held_canvas_refuses_the_widget_lock(self):
        other = UserV2(email="other@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(other)
        self.db.flush()
        self.db.add(ResourceGrantV2(**{NODE_COLUMNS["tab"]: self.tab.id}, user_id=other.id, level="edit", created_at=NOW))
        self.db.commit()
        self.client.put("/v2/tabs/tab-1/lock", json={}, headers=self._auth(email="other@renphil.org", roles=[]))
        r = self._lock("alpha-link")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["blocking"][0]["relation"], "ancestor")

    # -- the content door on the component's OWN token --------------------

    def test_content_write_passes_on_the_widgets_own_token(self):
        token = self._lock("alpha-link").json()["data"]["lock_token"]
        r = self._content("alpha-link", {"title": "Own token"}, token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._reload("alpha-link").title, "Own token")

    def test_content_write_is_refused_on_a_sibling_widgets_token(self):
        token = self._lock("beta-link").json()["data"]["lock_token"]
        r = self._content("alpha-link", {"title": "Wrong widget"}, token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "EDIT_SESSION_MISSING")
        self.assertEqual(self._reload("alpha-link").title, "Alpha")

    def test_content_write_is_refused_as_expired_then_taken_over(self):
        token = self._lock("alpha-link").json()["data"]["lock_token"]
        row = self._reload("alpha-link")
        row.locked_at = datetime.now(timezone.utc) - timedelta(seconds=get_settings().TAB_LOCK_TTL_SECONDS + 60)
        self.db.commit()
        r = self._content("alpha-link", {"title": "Expired"}, token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "EDIT_SESSION_EXPIRED")

        # An admin forces the CANVAS: Alpha's row is broken with its token
        # rotated and `locked_by` kept, so the ousted editor's next write
        # reads TAKEN_OVER (the descendant case that preserves it).
        self._lock("alpha-link")  # reclaim fresh first
        r = self.client.put(
            "/v2/tabs/tab-1/lock", json={"force": True}, headers=self._admin_auth()
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self._content("alpha-link", {"title": "Ousted"}, token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "EDIT_SESSION_TAKEN_OVER")
        self.assertEqual(self._reload("alpha-link").title, "Alpha")

    def test_the_airtable_config_door_also_passes_on_the_widgets_own_token(self):
        """`update_airtable_component_config` shares `_require_live_session`
        on `("component", id)` — rerouted by `lock_node_of` with no edit of
        its own."""
        from unittest import mock

        token = self._lock("airtable-link").json()["data"]["lock_token"]
        headers = {**self._auth(), "X-Edit-Tokens": token}
        # The route schedules a warm-on-save background task that opens its
        # OWN db session against the real engine — out of scope here.
        with mock.patch("app.routers.airtable._warm_after_config_save"):
            r = self.client.put(
                "/data/airtable/component/airtable-link/config",
                json={"sourceUrl": "https://airtable.com/appX/tblZ"},
                headers=headers,
            )
        self.assertEqual(r.status_code, 200, r.text)


class TestContentVisibilityForAnUnrelatedEndpoint(ComponentContentFixture):
    """UPDATED for project_ac_enforcement_gap.md item 1 (2026-09-03): this
    class used to be `TestNoContentEnforcementWasAdded`, guarding that
    adding the by-link write endpoint changed nothing about what an
    existing read served. It changed the point of the guard, not its
    subject — this fixture never authors a resource_grants row, so a
    non-admin now correctly sees nothing (D2); the Hub Admin case is added
    below so this class still proves an ordinary read endpoint works at all,
    which was the original test's actual point.

    UPDATED AGAIN for project_ac_enforcement_gap.md item 2 (2026-09-04):
    `editor@renphil.org` — the default `_auth()` identity, and what
    "a non-admin holding nothing" used to mean here — now holds a real edit
    grant on `self.tab` (`setUp`, added so the WRITE-side tests elsewhere in
    this file keep exercising content behavior rather than access control).
    "Holding nothing" now means a different, explicitly ungranted email."""

    def test_a_non_admin_holding_nothing_gets_a_dark_hub(self):
        r = self.client.get(
            "/v2/tabs/tab-1/workspace", headers=self._auth(email="nobody@renphil.org", roles=[])
        )
        self.assertEqual(r.status_code, 404, r.text)

        r = self.client.get(
            "/v2/tabs/root", headers=self._auth(email="nobody@renphil.org", roles=[])
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"], [])

    def test_hub_admin_by_jwt_still_sees_the_workspace_and_root_list(self):
        r = self.client.get("/v2/tabs/tab-1/workspace", headers=self._auth(roles=["Hub Admin"]))
        self.assertEqual(r.status_code, 200, r.text)
        widgets = r.json()["data"]["page_content"]["content"]["widgets"]
        self.assertIn(str(self.alpha.id), widgets)

        r = self.client.get("/v2/tabs/root", headers=self._auth(roles=["Hub Admin"]))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"])


if __name__ == "__main__":
    unittest.main()
