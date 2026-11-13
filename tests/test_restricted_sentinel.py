"""The `restricted` sentinel must never be written back
(plan_access_control_algorithm_2026-08-27.md §6.7, §10 item 7).

THE DEFECT, which was traced by READING rather than by running — which is
exactly why §10 says it "needs a test":

``filter_widget_content_for_user`` replaces a widget the caller may not see
with ``{type: 'restricted', data: None}`` and **keeps its key**
(``tab_service.py:100``). The frontend's only guard is at render —
``isRestrictedWidgetType`` is checked "at the boundary where entries are
rendered" (``gridContent.ts:753-760``) — so the sentinel stays in
``GridCanvasContent.widgets`` and is posted back on the next save, where
``update_tab_content_v2`` used to treat it as an ordinary edit and write it
straight through. The widget the caller was not allowed to SEE is destroyed
by the act of saving the canvas around it.

Rare today because it needs someone who is both redacted *and* able to enter
edit mode, and every tab's ``admins`` was narrowed to ``{Hub Admin}``, who
bypass redaction entirely. Fine-grained edit grants make that combination
ordinary, which is why §10 orders this fix BEFORE they exist.

``TestTheSentinelReallyIsProducedAndKeepsItsKey`` is the half that stops the
rest of this file passing vacuously: if redaction ever stopped emitting the
sentinel, or started dropping the key, every "the widget survived" assertion
below would go green while testing nothing. Same documentation-by-pairing
shape as ``test_the_wide_sql_filter_really_does_admit_it`` in
``test_resource_grants.py``.
"""

from __future__ import annotations

import os
import unittest
from copy import deepcopy

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


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.page_content import PageContentV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.services.gridstack_service import (  # noqa: E402
    RESTRICTED_WIDGET_TYPE,
    _create_gridstack_component,
    get_tab_content_v2,
    update_tab_content_v2,
)
from app.services.tab_service import filter_widget_content_for_user  # noqa: E402

# A viewer ACL naming somebody else. `_user_can_view_widget` treats an empty
# viewers group as open to everyone, so the widget needs a NON-empty one to
# be redacted at all.
SECRET_AC = {"viewers": {"users": [{"email": "owner@renphil.org"}], "roles": []}}

INTRUDER_EMAIL = "editor@renphil.org"
# Deliberately NOT "Hub Admin" — a hub admin bypasses per-widget filtering
# outright (tab_service.py:76) and would never see a sentinel.
INTRUDER_ROLES = ["Program Member"]


class RestrictedSentinelTestCase(unittest.TestCase):
    """Two widgets on one canvas: one the caller may see, one they may not."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        BaseV2.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        tab = TabV2(document_id="root-doc", title="Root", order=0, access_control={})
        self.db.add(tab)
        self.db.flush()
        gridstack = GridstackV2(
            document_id="root-doc", name="Root", position=0, parent_tab_id=tab.id
        )
        self.db.add(gridstack)
        self.db.flush()
        _create_gridstack_component(self.db, gridstack)
        self.gridstack = gridstack

        self.open_widget = self._widget("open", "Open Widget", {}, "open content")
        self.secret_widget = self._widget("secret", "Secret Widget", SECRET_AC, "secret content")
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _widget(self, link, title, access_control, markdown) -> ComponentV2:
        page_content = PageContentV2(content={"markdown": markdown})
        self.db.add(page_content)
        self.db.flush()
        component = ComponentV2(
            link=link,
            type="markdown",
            title=title,
            description=f"{title} description",
            x=0,
            y=0,
            width=3,
            height=3,
            props={},
            access_control=access_control,
            gridstack_id=self.gridstack.id,
            page_content_id=page_content.id,
        )
        self.db.add(component)
        self.db.flush()
        return component

    # -- helpers ------------------------------------------------------

    def _redacted_payload(self) -> dict:
        """Exactly what a redacted caller's browser holds and posts back."""
        served = get_tab_content_v2(self.db, "root-doc")
        return deepcopy(
            filter_widget_content_for_user(served["content"], INTRUDER_EMAIL, INTRUDER_ROLES)
        )

    def _reload(self, component: ComponentV2) -> ComponentV2:
        self.db.expire_all()
        return self.db.query(ComponentV2).filter(ComponentV2.id == component.id).first()

    def _markdown_of(self, component: ComponentV2) -> str | None:
        row = (
            self.db.query(PageContentV2)
            .filter(PageContentV2.id == component.page_content_id)
            .first()
        )
        return (row.content or {}).get("markdown") if row else None


class TestTheSentinelReallyIsProducedAndKeepsItsKey(RestrictedSentinelTestCase):
    """Without this class the rest of the file could pass vacuously."""

    def test_the_secret_widget_is_redacted_to_the_sentinel(self):
        payload = self._redacted_payload()
        entry = payload["widgets"][str(self.secret_widget.id)]
        self.assertEqual(entry["type"], RESTRICTED_WIDGET_TYPE)
        self.assertIsNone(entry["data"])

    def test_the_sentinel_KEEPS_the_component_key(self):
        """The key is what stops the delete pass removing the component, and
        it is also what carries the sentinel back to the save."""
        payload = self._redacted_payload()
        self.assertIn(str(self.secret_widget.id), payload["widgets"])

    def test_the_sentinel_carries_no_type_title_description_or_data(self):
        """The shape of the damage: every one of these would be written over
        the real component by an unguarded save."""
        entry = self._redacted_payload()["widgets"][str(self.secret_widget.id)]
        self.assertNotIn("title", entry)
        self.assertNotIn("description", entry)
        self.assertNotIn("link", entry)
        self.assertNotIn("access_control", entry)

    def test_the_open_widget_is_not_redacted(self):
        entry = self._redacted_payload()["widgets"][str(self.open_widget.id)]
        self.assertEqual(entry["type"], "markdown")


class TestSavingARedactedCanvasDestroysNothing(RestrictedSentinelTestCase):
    def test_the_redacted_component_survives_a_save_intact(self):
        """The defect, end to end: redact, save, and the widget the caller
        could not see is still exactly what it was."""
        update_tab_content_v2(self.db, "root-doc", self._redacted_payload())

        survivor = self._reload(self.secret_widget)
        self.assertIsNotNone(survivor, "the redacted component was deleted")
        self.assertEqual(survivor.type, "markdown")
        self.assertNotEqual(survivor.type, RESTRICTED_WIDGET_TYPE)
        self.assertEqual(survivor.title, "Secret Widget")
        self.assertEqual(survivor.description, "Secret Widget description")
        self.assertEqual(survivor.link, "secret")
        self.assertEqual(survivor.access_control, SECRET_AC)
        self.assertEqual(self._markdown_of(survivor), "secret content")

    def test_no_component_anywhere_ends_up_typed_restricted(self):
        update_tab_content_v2(self.db, "root-doc", self._redacted_payload())
        self.assertEqual(
            self.db.query(ComponentV2)
            .filter(ComponentV2.type == RESTRICTED_WIDGET_TYPE)
            .count(),
            0,
        )

    def test_repeated_saves_stay_non_destructive(self):
        """A redacted client saves on every edit, not once."""
        for _ in range(3):
            update_tab_content_v2(self.db, "root-doc", self._redacted_payload())
        survivor = self._reload(self.secret_widget)
        self.assertEqual(survivor.type, "markdown")
        self.assertEqual(self._markdown_of(survivor), "secret content")

    def test_a_real_edit_beside_the_sentinel_still_lands(self):
        """The guard must skip the sentinel WITHOUT blocking the save it
        arrived in — otherwise the fix trades data loss for a dead canvas."""
        payload = self._redacted_payload()
        payload["widgets"][str(self.open_widget.id)]["data"]["markdown"] = "edited"
        payload["widgets"][str(self.open_widget.id)]["title"] = "Renamed"
        update_tab_content_v2(self.db, "root-doc", payload)

        edited = self._reload(self.open_widget)
        self.assertEqual(edited.title, "Renamed")
        self.assertEqual(self._markdown_of(edited), "edited")
        # ...and the redacted one still untouched beside it.
        self.assertEqual(self._markdown_of(self._reload(self.secret_widget)), "secret content")

    def test_the_skipped_component_is_not_reported_as_changed(self):
        """Search receipts drive reindexing. A component nothing wrote must
        not claim an upsert."""
        result = update_tab_content_v2(self.db, "root-doc", self._redacted_payload())
        touched = {u["component_id"] for u in result["search_updates"]}
        self.assertNotIn(self.secret_widget.id, touched)

    def test_a_sentinel_for_an_unknown_key_creates_nothing(self):
        """The insert branch is the other half — a sentinel with no matching
        component must not write a junk 'restricted' row."""
        before = self.db.query(ComponentV2).count()
        payload = self._redacted_payload()
        payload["widgets"]["999999"] = {"type": RESTRICTED_WIDGET_TYPE, "data": None}
        update_tab_content_v2(self.db, "root-doc", payload)
        self.assertEqual(self.db.query(ComponentV2).count(), before)

    def test_a_sentinel_typed_widget_from_any_client_is_ignored(self):
        """The guard keys on the TYPE, not on where the request came from, so
        a hand-rolled request cannot use the sentinel to blank a widget it
        cannot see either."""
        served = get_tab_content_v2(self.db, "root-doc")
        payload = deepcopy(served["content"])
        payload["widgets"][str(self.open_widget.id)] = {
            "type": RESTRICTED_WIDGET_TYPE,
            "data": None,
        }
        update_tab_content_v2(self.db, "root-doc", payload)

        survivor = self._reload(self.open_widget)
        self.assertEqual(survivor.type, "markdown")
        self.assertEqual(self._markdown_of(survivor), "open content")


class TestDeletionStillWorksAndIsWhyKeysMustSurvive(RestrictedSentinelTestCase):
    def test_dropping_a_key_still_deletes_the_component(self):
        """The delete pass is deliberately unchanged: absence still means
        delete. This is the behaviour the guard must not break."""
        payload = self._redacted_payload()
        del payload["widgets"][str(self.open_widget.id)]
        update_tab_content_v2(self.db, "root-doc", payload)
        self.assertIsNone(self._reload(self.open_widget))

    def test_stripping_sentinels_from_the_payload_would_DELETE_the_widget(self):
        """WHY THE CLIENT-SIDE HALF MUST NOT BE "FILTER THEM OUT".

        Removing sentinel ENTRIES before posting is the obvious client-side
        fix and it is worse than the bug: the delete pass keys on the ABSENCE
        of a component's key, so a stripped sentinel does not preserve the
        widget, it destroys the whole row. Pinned here so nobody implements
        that in the frontend without meeting this test first — the server
        cannot defend against it, because a deleted key is indistinguishable
        from a genuine deletion.
        """
        payload = self._redacted_payload()
        del payload["widgets"][str(self.secret_widget.id)]
        update_tab_content_v2(self.db, "root-doc", payload)
        self.assertIsNone(
            self._reload(self.secret_widget),
            "if this now survives, the delete semantics changed — re-read this test",
        )


if __name__ == "__main__":
    unittest.main()
