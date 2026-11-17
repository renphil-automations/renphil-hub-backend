"""
A widget config that can't be compiled into a formula answers 400, not 500.

Found in the advanced-filters phase-5 live pass (2026-09-28): an admin filter
`Name > abc` (or a blank `gt`/`lt`) makes `airtable_formulas._as_number` raise
`FormulaFieldError`. Decision 7 of that arc wants the request to error, and it
did — but as an UNHANDLED exception, i.e. a bare 500 raised past
`CORSMiddleware`, so the browser saw only "Failed to fetch" with no reason.
`app/main.py` now maps `FormulaFieldError` to a 400 carrying its message.

Covers all six component-data sites (the same six
`test_airtable_personalize.AirtableComponentAccessControlTests` gates), that
the 400 carries CORS headers, that a numeric value still compiles (control),
and that the handler is narrow — any other `ValueError` is still a 500.

Reuses `test_airtable_personalize`'s router fixture (in-memory SQLite v2, a
real TestClient, fake Airtable API) rather than rebuilding it.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe -m unittest tests.test_airtable_formula_error_response
"""

from __future__ import annotations

import unittest

from tests.test_airtable_personalize import (
    _AirtableAccessControlFixture,
)
from app.config import get_settings
from app.services.gridstack_service import (
    AIRTABLE_CHART_WIDGET_TYPE,
    AIRTABLE_METRIC_WIDGET_TYPE,
)

GRANTED = "allowed@example.com"
BAD_GT = [{"field": "Name", "operator": "gt", "value": "abc"}]
BLANK_GT = [{"field": "Name", "operator": "gt", "value": ""}]
GOOD_GT = [{"field": "Name", "operator": "gt", "value": "5"}]
BAD_TREE = {
    "type": "group",
    "id": "root",
    "conjunction": "or",
    "items": [
        {"type": "condition", "id": "a", "field": "Name", "operator": "eq", "value": ["x"]},
        {"type": "condition", "id": "b", "field": "Name", "operator": "lt", "value": "abc"},
    ],
}


class FormulaFieldErrorResponseTests(_AirtableAccessControlFixture):
    def _origin(self) -> str:
        return get_settings().ALLOWED_ORIGINS[0]

    def _seed_all(self, filters) -> None:
        """One widget per kind, all carrying `filters`, all granted."""
        widgets = [
            self._seed_widget("fe-rows", extra_data={"filters": filters, "viewerControlsEnabled": True}),
            self._seed_widget(
                "fe-metric",
                widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
                extra_data={"filters": filters, "aggregation": "count"},
            ),
            self._seed_widget(
                "fe-chart",
                widget_type=AIRTABLE_CHART_WIDGET_TYPE,
                extra_data={"filters": filters, "groupField": "Name", "aggregation": "count"},
            ),
        ]
        for widget in widgets:
            self._grant(email=GRANTED, node=("component", widget.id))

    def _six_requests(self):
        headers = {**self._auth(GRANTED), "Origin": self._origin()}
        yield "rows", lambda: self.client.get("/data/airtable/component/fe-rows/rows", headers=headers)
        yield "rows/full", lambda: self.client.get(
            "/data/airtable/component/fe-rows/rows/full", headers=headers
        )
        yield "metric", lambda: self.client.get(
            "/data/airtable/component/fe-metric/metric", headers=headers
        )
        yield "chart", lambda: self.client.get(
            "/data/airtable/component/fe-chart/chart", headers=headers
        )
        # The editor previews take the unsaved filters from the body.
        yield "preview", lambda: self.client.post(
            "/data/airtable/component/preview",
            json={"link": "fe-rows", "filters": self._filters},
            headers=headers,
        )
        yield "chart/preview", lambda: self.client.post(
            "/data/airtable/component/chart/preview",
            json={
                "link": "fe-chart",
                "filters": self._filters,
                "groupField": "Name",
                "aggregation": "count",
            },
            headers=headers,
        )

    def _assert_all(self, filters, expected_status: int, detail_fragment: str | None = None) -> None:
        self._filters = filters
        self._seed_all(filters)
        for name, call in self._six_requests():
            with self.subTest(endpoint=name), self._patched():
                resp = call()
                self.assertEqual(resp.status_code, expected_status, f"{name}: {resp.text}")
                if detail_fragment is not None:
                    self.assertIn(detail_fragment, resp.json().get("detail", ""), name)
                    # The whole point: the error reaches the browser, so it
                    # must carry CORS headers (a bare 500 didn't).
                    self.assertEqual(
                        resp.headers.get("access-control-allow-origin"), self._origin(), name
                    )

    def test_non_numeric_gt_is_a_400_with_the_reason_on_every_site(self) -> None:
        self._assert_all(BAD_GT, 400, "Numeric comparison needs a number, got 'abc'")

    def test_blank_gt_is_a_400_on_every_site(self) -> None:
        self._assert_all(BLANK_GT, 400, "Numeric comparison needs a number")

    def test_bad_lt_inside_an_or_tree_is_a_400(self) -> None:
        self._assert_all(BAD_TREE, 400, "got 'abc'")

    def test_numeric_gt_still_compiles(self) -> None:
        self._assert_all(GOOD_GT, 200)


class FormulaFieldErrorHandlerIsNarrowTests(_AirtableAccessControlFixture):
    def test_other_value_errors_are_still_500s(self) -> None:
        app = self.client.app

        @app.get("/__phase5_plain_value_error")
        async def _boom():
            raise ValueError("not a formula problem")

        self.assertEqual(self.client.get("/__phase5_plain_value_error").status_code, 500)


if __name__ == "__main__":
    unittest.main()
