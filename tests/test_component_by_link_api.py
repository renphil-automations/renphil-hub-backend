"""GET /v2/tabs/components/by-link/{link} — the mirror picker's resolver.

Handoff §4.2 (session_handoff_2026-09-02-grant-editor.md): this was the only
client-facing way to resolve a `link` (a string) and its response carried no
`id` (the integer `components.id`), so nothing could turn a pasted link into
the numeric id a component-kind resource grant addresses
(plan_access_control_algorithm_2026-08-27.md §7). This file pins that `id`
is now on the response — additively: every field the mirror picker already
reads (`type`, `title`, `data`, `sbn`) is unchanged.

AC GAP FIXED (mirror picker's "paste a link" bypass, found via live manual
testing): this endpoint used to have NO `ViewerAccess` dependency at all —
every other v2 read path (`.../workspace`, `.../children`, `.../variants`,
and its own sibling `.../location`) already gated on it, but this one
resolved and returned a component's live `type`/`title`/`data` to ANY
authenticated caller regardless of grants, because a pasted link never
passes through a server-gated listing endpoint the way browse mode's picks
do. Fixed by threading `ViewerAccess` through and checking
`access.is_granted(("component", id))` — the same check, same 404-not-403
fail-closed convention, as `.../location` — before resolving. `setUp` below
now gives the default `_auth()` identity a real VIEW grant so the original
"does the payload look right" tests above keep exercising that, unaffected;
`TestPasteLinkAccessControl` is the new class actually about the gate.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-component-by-link-tests-only")
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
from app.services.gridstack_service import MIRROR_WIDGET_TYPE  # noqa: E402

URL = "/v2/tabs/components/by-link/{link}"
NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


class ComponentByLinkFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        hub = HubV2(document_id="hub-1")
        self.db.add(hub)
        self.db.flush()
        nav = NavTabV2(document_id="nav-1", slug="dash", title="Dashboard")
        self.db.add(nav)
        self.db.flush()
        tab = TabV2(document_id="tab-1", title="Root", nav_tab_id=nav.id)
        self.db.add(tab)
        self.db.flush()
        self.tab = tab
        gs = GridstackV2(document_id="tab-1", parent_tab_id=tab.id)
        self.db.add(gs)
        self.db.flush()

        self.widget = ComponentV2(
            link="widget-1", type="markdown", title="Widget", gridstack_id=gs.id
        )
        self.mirror = ComponentV2(
            link="mirror-1",
            type=MIRROR_WIDGET_TYPE,
            title="Mirror",
            gridstack_id=gs.id,
            props={"target_link": "widget-1"},
        )
        self.db.add_all([self.widget, self.mirror])
        self.db.flush()

        # The default `_auth()` identity needs a real grant now that this
        # endpoint gates on ViewerAccess — VIEW is deliberately enough
        # (matches `access.is_granted`'s "view-or-edit, edit implies view"
        # contract), so this stays a payload-shape fixture, not an
        # editor-only one. `TestPasteLinkAccessControl` below is the class
        # that varies this grant to test the gate itself.
        self.editor_user = UserV2(email="editor@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(self.editor_user)
        self.db.flush()
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["tab"]: tab.id},
                user_id=self.editor_user.id,
                level="view",
                created_at=NOW,
            )
        )
        self.db.commit()

        app = create_app()

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

    def _grant(self, *, user: UserV2, level: str) -> None:
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS["tab"]: self.tab.id},
                user_id=user.id,
                level=level,
                created_at=NOW,
            )
        )
        self.db.commit()


class TestComponentByLinkIncludesId(ComponentByLinkFixture):
    """Handoff §4.2."""

    def test_the_response_carries_the_numeric_id(self):
        res = self.client.get(URL.format(link="widget-1"), headers=self._auth())
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["data"]
        self.assertEqual(data["id"], self.widget.id)
        self.assertEqual(data["type"], "markdown")

    def test_existing_fields_are_unchanged(self):
        """Additive only — every field the mirror picker already reads is
        still there, unaffected."""
        res = self.client.get(URL.format(link="widget-1"), headers=self._auth())
        data = res.json()["data"]
        self.assertEqual(set(data), {"id", "type", "title", "data", "sbn"})

    def test_a_mirror_still_cannot_be_resolved(self):
        """Cycle prevention, unchanged — a mirror is never a pickable
        target, and this endpoint refuses to resolve one directly."""
        res = self.client.get(URL.format(link="mirror-1"), headers=self._auth())
        self.assertEqual(res.status_code, 404)

    def test_an_unknown_link_is_404(self):
        res = self.client.get(URL.format(link="nope"), headers=self._auth())
        self.assertEqual(res.status_code, 404)

    def test_authentication_is_required(self):
        res = self.client.get(URL.format(link="widget-1"))
        self.assertEqual(res.status_code, 401)


class TestPasteLinkAccessControl(ComponentByLinkFixture):
    """The fix: this endpoint backs the mirror picker's 'paste a link' mode,
    which — unlike browse mode — never passes through a server-gated listing
    endpoint on its way here. Before this fix, ANY authenticated caller could
    resolve (and read the live `data` of) any component in the hub by
    guessing/pasting its `link`, regardless of access_control, because this
    was the one v2 read path with no `ViewerAccess` check at all."""

    def test_an_ungranted_user_cannot_resolve_a_link_they_cannot_view(self):
        nobody = UserV2(email="nobody@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(nobody)
        self.db.commit()

        res = self.client.get(
            URL.format(link="widget-1"), headers=self._auth(email="nobody@renphil.org")
        )
        # Same code AND same body as test_an_unknown_link_is_404 above — a
        # caller who cannot view this link must not be able to tell it apart
        # from one that does not exist (§9's fail-closed convention).
        self.assertEqual(res.status_code, 404)
        self.assertEqual(
            res.json()["detail"], "Component not found, or cannot be mirrored"
        )

    def test_a_view_only_grant_is_sufficient(self):
        """`is_granted` is "view or edit" (edit implies view by construction
        — access_visibility_service.py) — matches the requirement as
        originally reported: blocked only when the caller has NEITHER edit
        NOR read access."""
        viewer = UserV2(email="viewer@renphil.org", has_signed_in=True, created_at=NOW)
        self.db.add(viewer)
        self.db.flush()
        self._grant(user=viewer, level="view")

        res = self.client.get(
            URL.format(link="widget-1"), headers=self._auth(email="viewer@renphil.org")
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["data"]["id"], self.widget.id)

    def test_a_hub_admin_may_resolve_with_no_grant_at_all(self):
        # Phase 3: the JWT claim alone admits nobody; a real (hub_admin,
        # universal) assignment is what makes this caller an admin.
        seed_real_admin(self._session_factory, "admin@renphil.org")
        res = self.client.get(
            URL.format(link="widget-1"),
            headers=self._auth(email="admin@renphil.org", roles=["Hub Admin"]),
        )
        self.assertEqual(res.status_code, 200, res.text)


if __name__ == "__main__":
    unittest.main()
