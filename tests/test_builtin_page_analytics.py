"""Built-in (hard-coded) Hub pages: their openings, controls and active time are recorded as fixed codes and shown
to All Scopes admins only. An editor sees analytics only about data they can currently edit, and a built-in page
is not such data, so none of it reaches an editor's summary, events or active time."""

from __future__ import annotations

import unittest

from app.db_v2.models.tab import TabV2
from app.services import product_analytics_service as service
from tests import test_product_analytics as analytics  # module import: its own tests are not collected again here
from tests.test_product_analytics import ADMIN, MEMBER, _engagement, _event, grant

_Harness = analytics.ProductAnalyticsApiTests


def opened(surface: str, route: str) -> dict:
    return _event("builtin_page.opened", target_id=None, page_route=route, properties={"surface": surface})


def control(surface: str, name: str, route: str, view: str | None = None) -> dict:
    properties = {"surface": surface, "control": name, **({"view": view} if view else {})}
    return _event("builtin_control.used", target_id=None, page_route=route, properties=properties)


class BuiltinPageAnalyticsTests(unittest.TestCase):
    setUp = _Harness.setUp
    tearDown = _Harness.tearDown
    _closures = _Harness._closures
    _is_hub_admin = _Harness._is_hub_admin
    _worker_session = _Harness._worker_session
    _ingest = _Harness._ingest
    _ingest_engagements = _Harness._ingest_engagements
    _rows = _Harness._rows

    def _record_admin_page_activity(self) -> None:
        self.current = ADMIN
        accepted = self._ingest(
            opened("bot_management", "/admin/bot-management"),
            control("external_resources", "view_selected", "/admin/bot-management", view="worker"),
            _event("thread_review.tab_selected", target_id="history", page_route="/threads/review",
                   properties={"view": "history", "kind": "new"}),
            _event("my_revisions.tab_selected", target_id="awaiting", page_route="/threads/my-revisions",
                   properties={"view": "awaiting"}),
        )
        self.assertEqual(accepted.json()["accepted"], 4, accepted.json())
        self._ingest_engagements(_engagement(page_route="/admin/bot-management", nav_tab_id=None, tab_id=None))

    def test_built_in_page_events_are_accepted_as_fixed_codes_only(self) -> None:
        self._record_admin_page_activity()
        rows = {row.event_name: row for row in self._rows()}
        self.assertEqual(rows["builtin_page.opened"].properties, {"surface": "bot_management"})
        self.assertEqual(rows["builtin_control.used"].properties, {"surface": "external_resources", "control": "view_selected", "view": "worker"})
        self.assertEqual(rows["thread_review.tab_selected"].page_route, "/threads/review")
        self.assertEqual(rows["thread_review.tab_selected"].target_id, "history")
        self.assertEqual(rows["thread_review.tab_selected"].properties, {"view": "history", "kind": "new"})
        self.assertEqual(rows["my_revisions.tab_selected"].page_route, "/threads/my-revisions")

    def test_unknown_codes_and_free_text_are_dropped(self) -> None:
        self.current = ADMIN
        self._ingest(_event("builtin_control.used", target_id="Jane Doe", page_route="/admin/bot-management",
                            properties={"surface": "Jane Doe salary", "control": "typed: secret", "view": "worker", "note": "x"}))
        (row,) = self._rows()
        self.assertEqual(row.properties, {"view": "worker"})
        self.assertIsNone(row.target_id)

    def test_built_in_routes_keep_their_names(self) -> None:
        for path, template in (
            ("/threads/review", "/threads/review"), ("/threads/my-revisions", "/threads/my-revisions"),
            ("/admin/external-resources", "/admin/external-resources"), ("/admin/hub-analytics", "/admin/hub-analytics"),
            ("/admin/bot-management", "/admin/bot-management"), ("/threads/12345", "/threads/:segment"),
        ):
            with self.subTest(path=path):
                self.assertEqual(service.route_template(path), template)

    def test_all_scopes_admin_sees_built_in_page_activity(self) -> None:
        self._record_admin_page_activity()
        events = self.client.get("/v2/product-analytics/events").json()["data"]
        self.assertEqual({row["event_name"] for row in events},
                         {"builtin_page.opened", "builtin_control.used", "thread_review.tab_selected", "my_revisions.tab_selected"})
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(summary["totals"]["events"], 4)
        self.assertEqual(summary["engagement"]["active_seconds"], 3)

    def test_an_editor_sees_none_of_it_even_with_edit_access_everywhere_in_the_hub(self) -> None:
        self._record_admin_page_activity()
        with self.Session() as db:
            tab_ids = [tab_id for (tab_id,) in db.query(TabV2.id).all()]
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("hub", 1, "edit"), *[grant("tab", tab_id, "edit") for tab_id in tab_ids]]}
        events = self.client.get("/v2/product-analytics/events")
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["data"], [])
        summary = self.client.get("/v2/product-analytics/summary").json()
        self.assertEqual(summary["totals"]["events"], 0)
        self.assertEqual(summary["engagement"]["active_seconds"], 0)
        # Their own activity on a built-in page is not shown back to them either.
        self._ingest(opened("feedback", "/feedbacks"))
        self.assertEqual(self.client.get("/v2/product-analytics/events").json()["data"], [])


del _Harness  # keep pytest from collecting the borrowed harness's own tests a second time


if __name__ == "__main__":
    unittest.main()


def test_built_in_events_name_their_page_and_link_to_it():
    from app.services.product_analytics_refs import builtin_destination

    assert builtin_destination({"event_name": "builtin_page.opened", "properties": {"surface": "bot_management"}}) == (
        "builtin:bot_management", "Bot Management", "/admin/bot-management")
    assert builtin_destination({"event_name": "builtin_control.used", "properties": {"surface": "external_resources", "control": "exported"}})[1:] == (
        "External Resources", "/admin/external-resources")
    assert builtin_destination({"event_name": "thread_review.tab_selected", "properties": {"view": "history"}})[1] == "Thread Review"
    # Grouped rows have no properties: the stored route identifies the page.
    assert builtin_destination({"event_name": "builtin_page.opened", "properties": {}, "page_route": "/admin/access-control/root-access"})[1] == "Root Access"
    assert builtin_destination({"event_name": "builtin_page.opened", "properties": {}, "page_route": "/dashboard/announcements/:segment"})[1] == "Announcements"
    # Unknown codes and ordinary Hub events are left to the normal name resolution.
    assert builtin_destination({"event_name": "builtin_page.opened", "properties": {"surface": "x"}, "page_route": "/:nav_tab"}) is None
    assert builtin_destination({"event_name": "link.clicked", "properties": {}, "page_route": "/admin/bot-management"}) is None
