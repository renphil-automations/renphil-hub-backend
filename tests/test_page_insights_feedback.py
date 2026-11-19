"""Page Insights feedback: Hub feedback about one page and its components, under both the Page Insights scope
(current edit access on that page) and the Feedback page's own read rule, rechecked on every call."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.db_v2.models.hub_feedback import HubFeedbackV2
from app.db_v2.models.tab import TabV2
from tests import test_product_analytics as analytics  # module import: its own tests are not collected again here
from tests.test_product_analytics import ADMIN, MEMBER, grant

URL = "/v2/product-analytics/pages/{}/feedback"
_Harness = analytics.ProductAnalyticsApiTests


class PageInsightsFeedbackTests(unittest.TestCase):
    # The analytics API harness: the real routers, the real live-access fold over a seeded Hub, faked grants.
    setUp_harness = _Harness.setUp
    tearDown = _Harness.tearDown
    _closures = _Harness._closures
    _is_hub_admin = _Harness._is_hub_admin
    _worker_session = _Harness._worker_session
    _add_variant = _Harness._add_variant

    def setUp(self) -> None:
        self.setUp_harness()
        from app.db_v2.database import BaseV2
        BaseV2.metadata.create_all(bind=self.engine, tables=[HubFeedbackV2.__table__])
        self.minutes = 0

    def _feedback(self, scope: str, page_id: str | None = None, component_link: str | None = None, *,
                  environment: str = "sandbox", status: str = "unresolved", message: str = "Feedback") -> None:
        self.minutes += 1
        with self.Session() as db:
            db.add(HubFeedbackV2(
                public_id=str(uuid.uuid4()), submission_id=str(uuid.uuid4()), actor_user_id=2, actor_email=MEMBER.email,
                environment=environment, scope=scope, message=message, page_id=page_id, component_link=component_link,
                created_at=datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(minutes=self.minutes), status=status,
            ))
            db.commit()

    def _seed(self) -> None:
        self._add_variant("variant-doc-1")
        self._feedback("page", "root-doc-1", message="The page is slow")
        self._feedback("component", "root-doc-1", "cmp-links", message="A link is broken", status="resolved")
        self._feedback("page", "variant-doc-1", message="Variant wording")
        self._feedback("hub", None, message="Hub-wide idea")                          # not about a page
        self._feedback("page", "root-doc-2", message="Another page")                  # another page
        self._feedback("page", "root-doc-1", environment="production", message="Other environment")

    def _root_tab_id(self) -> int:
        with self.Session() as db:
            return db.query(TabV2.id).filter(TabV2.document_id == "root-doc-1").scalar()

    def test_counts_and_lists_feedback_on_the_page_its_components_and_variants_only(self) -> None:
        self._seed()
        self.current = ADMIN
        response = self.client.get(URL.format("root-doc-1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual((body["total"], body["on_page"], body["on_components"], body["unresolved"]), (3, 2, 1, 2))
        self.assertEqual([item["message"] for item in body["latest"]], ["Variant wording", "A link is broken", "The page is slow"])
        component = body["latest"][1]
        self.assertEqual((component["scope"], component["component_name"], component["status"]), ("component", "Jane Doe links", "resolved"))
        self.assertTrue(all("diagnostic_route" not in item for item in body["latest"]))
        # A variant reports only itself.
        variant = self.client.get(URL.format("variant-doc-1")).json()
        self.assertEqual([item["message"] for item in variant["latest"]], ["Variant wording"])

    def test_latest_is_capped_at_five_but_the_count_is_complete(self) -> None:
        for index in range(8):
            self._feedback("page", "root-doc-1", message=f"Note {index}")
        self.current = ADMIN
        body = self.client.get(URL.format("root-doc-1")).json()
        self.assertEqual(body["total"], 8)
        self.assertEqual([item["message"] for item in body["latest"]], [f"Note {index}" for index in (7, 6, 5, 4, 3)])

    def test_an_editor_of_the_page_sees_it_and_loses_it_the_moment_edit_access_is_revoked(self) -> None:
        self._seed()
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", self._root_tab_id(), "edit")]}
        body = self.client.get(URL.format("root-doc-1")).json()
        self.assertEqual(body["total"], 3)
        self.grants = {MEMBER.hub_user_id: [grant("tab", self._root_tab_id(), "view")]}
        refused = self.client.get(URL.format("root-doc-1"))
        self.assertEqual((refused.status_code, refused.json()), (403, {"detail": "Hub Analytics requires current edit access."}))

    def test_another_page_is_the_same_private_404_as_an_unknown_one(self) -> None:
        self._seed()
        self.current = MEMBER
        self.grants = {MEMBER.hub_user_id: [grant("tab", self._root_tab_id(), "edit")]}
        for page_id in ("root-doc-2", "unknown-doc", "not a valid id"):
            with self.subTest(page_id=page_id):
                response = self.client.get(URL.format(page_id))
                self.assertEqual((response.status_code, response.json()), (404, {"detail": "Page insights are unavailable."}))

    def test_only_the_configured_environment_is_counted(self) -> None:
        self._seed()
        self.current = ADMIN
        self.environment = "production"
        body = self.client.get(URL.format("root-doc-1")).json()
        self.assertEqual([item["message"] for item in body["latest"]], ["Other environment"])
        self.environment = "unconfigured"
        self.assertEqual(self.client.get(URL.format("root-doc-1")).json(), {"available": False, "reason": "unconfigured"})


del _Harness  # keep pytest from collecting the borrowed harness's own tests a second time


if __name__ == "__main__":
    unittest.main()
