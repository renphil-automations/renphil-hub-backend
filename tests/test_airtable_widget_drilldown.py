"""
Tests for the Metric/Chart click-to-open drill-down
(`AirtableService.fetch_widget_drilldown_cached` and
GET /airtable/component/{link}/drilldown), 2026-10-02.

Reuses tests/test_airtable_widget_cache.py's in-memory Airtable + cache
fakes, so the drill-down reads the SAME cached envelope the real aggregates
do. The property under test throughout: the modal lists exactly the rows the
widget's own number / bar was computed from, projected to the configured
fields only.
"""

from __future__ import annotations

import unittest
from unittest import mock

# Module import, not `from ... import WidgetCacheServiceTests`: a TestCase
# bound at module level here would have its tests collected and run twice.
import tests.test_airtable_widget_cache as cache_tests
from tests.test_airtable_widget_cache import (
    FAKE_URL,
    AIRTABLE_CHART_WIDGET_TYPE,
    AIRTABLE_METRIC_WIDGET_TYPE,
    AIRTABLE_WIDGET_TYPE,
    AirtableService,
    FakeCacheService,
    _AirtableCacheFixture,
    make_fake_api_class,
)


REGION_RECORDS = [
    {"id": "r1", "fields": {"Name": "Bravo", "Region": "East", "Amount": 10, "Secret": "x"}},
    {"id": "r2", "fields": {"Name": "alpha", "Region": "East", "Amount": 5, "Secret": "x"}},
    {"id": "r3", "fields": {"Name": "Charlie", "Region": "West", "Amount": 1, "Secret": "x"}},
    {"id": "r4", "fields": {"Region": "North", "Amount": 2}},
    {"id": "r5", "fields": {"Name": "Delta", "Region": "South", "Amount": 3}},
]


class DrilldownServiceTests(unittest.IsolatedAsyncioTestCase):
    _patched = cache_tests.WidgetCacheServiceTests._patched

    async def _drill(self, records, *, patch_kwargs=None, **kwargs):
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, **(patch_kwargs or {})
        )
        params = {
            "widget_type": AIRTABLE_METRIC_WIDGET_TYPE,
            "link": "d1",
            "url": FAKE_URL,
            "api_key": "pat",
            "caller_email": "anyone@example.com",
            "title_field": "Name",
            "detail_fields": [],
            "aggregation": "count",
            **kwargs,
        }
        with api_patch, cache_patch:
            return await AirtableService(settings).fetch_widget_drilldown_cached(**params)

    async def test_metric_lists_every_row_sorted_with_empty_title_last(self) -> None:
        resp = await self._drill(REGION_RECORDS)
        self.assertTrue(resp.available)
        self.assertEqual(resp.total_rows, 5)
        self.assertEqual([r.id for r in resp.rows], ["r2", "r1", "r3", "r5", "r4"])

    async def test_projection_returns_only_configured_fields(self) -> None:
        resp = await self._drill(REGION_RECORDS, detail_fields=["Region"])
        self.assertEqual(resp.fields, ["Region"])
        for row in resp.rows:
            self.assertNotIn("Secret", row.fields)
            self.assertNotIn("Amount", row.fields)

    async def test_sum_appends_the_summed_field(self) -> None:
        resp = await self._drill(
            REGION_RECORDS, aggregation="sum", sum_field="Amount", detail_fields=["Region"]
        )
        self.assertEqual(resp.fields, ["Region", "Amount"])
        self.assertEqual(resp.total_rows, 5)

    async def test_count_ignore_empty_drops_uncounted_rows(self) -> None:
        resp = await self._drill(REGION_RECORDS, count_field="Name", count_ignore_empty=True)
        self.assertEqual(resp.total_rows, 4)
        self.assertNotIn("r4", [r.id for r in resp.rows])

    async def test_count_distinct_alone_still_lists_every_record(self) -> None:
        # Owner decision 2026-10-02: "every record", not one row per value.
        records = [
            {"id": "a", "fields": {"Name": "Same"}},
            {"id": "b", "fields": {"Name": "Same"}},
        ]
        resp = await self._drill(records, count_field="Name")
        self.assertEqual(resp.total_rows, 2)

    async def test_personalize_applies_per_viewer(self) -> None:
        records = [
            {"id": "a", "fields": {"Name": "Mine", "Owner": "me@example.com"}},
            {"id": "b", "fields": {"Name": "Theirs", "Owner": "them@example.com"}},
        ]
        resp = await self._drill(
            records,
            caller_email="me@example.com",
            personalize_enabled=True,
            personalize_column="Owner",
        )
        self.assertEqual([r.id for r in resp.rows], ["a"])

    async def test_personalize_fail_closed(self) -> None:
        resp = await self._drill(
            REGION_RECORDS, personalize_enabled=True, personalize_column=None
        )
        self.assertTrue(resp.personalize_blocked)
        self.assertEqual(resp.rows, [])

    async def test_chart_group_lists_only_that_group(self) -> None:
        resp = await self._drill(
            REGION_RECORDS,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Region",
            group="East",
        )
        self.assertEqual(sorted(r.id for r in resp.rows), ["r1", "r2"])
        self.assertTrue(all(r.group is None for r in resp.rows))

    async def test_chart_empty_group(self) -> None:
        records = [
            {"id": "a", "fields": {"Name": "A"}},
            {"id": "b", "fields": {"Name": "B", "Region": "East"}},
        ]
        resp = await self._drill(
            records,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Region",
            group="(Empty)",
        )
        self.assertEqual([r.id for r in resp.rows], ["a"])

    async def test_chart_other_matches_the_charts_own_folding(self) -> None:
        # East=2 is kept; with max_groups=2 every other group folds into Other.
        settings, fake_cache, api_patch, cache_patch = self._patched(REGION_RECORDS)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            chart = await service.fetch_widget_chart_cached(
                link="d-other",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
                max_groups=2,
            )
            other_bar = next(g for g in chart.groups if g.is_other)
            resp = await service.fetch_widget_drilldown_cached(
                widget_type=AIRTABLE_CHART_WIDGET_TYPE,
                link="d-other",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                title_field="Name",
                detail_fields=[],
                aggregation="count",
                group_field="Region",
                max_groups=2,
                other=True,
            )
        self.assertEqual(resp.total_rows, other_bar.value)
        self.assertEqual({r.group for r in resp.rows}, {"West", "North", "South"})

    async def test_real_group_named_other_is_not_the_other_bucket(self) -> None:
        records = [
            {"id": "a", "fields": {"Name": "A", "Region": "Other"}},
            {"id": "b", "fields": {"Name": "B", "Region": "East"}},
        ]
        resp = await self._drill(
            records,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Region",
            group="Other",
        )
        self.assertEqual([r.id for r in resp.rows], ["a"])

    async def test_row_cap_truncates_and_reports_total(self) -> None:
        resp = await self._drill(REGION_RECORDS, patch_kwargs={"full_view_max_rows": 2})
        self.assertTrue(resp.truncated)
        self.assertEqual(resp.total_rows, 5)
        self.assertEqual(len(resp.rows), 2)

    async def test_oversized_table_is_unavailable(self) -> None:
        records = [{"id": f"r{i}", "fields": {"Name": str(i)}} for i in range(12)]
        resp = await self._drill(records, patch_kwargs={"max_rows": 5})
        self.assertFalse(resp.available)
        self.assertEqual(resp.rows, [])


# Chart Count dedupe (2026-10-03). Plain counts: Big=3, Dupes=3, Pair=2.
# Distinct by Program: Big=3, Dupes=1, Pair=2 — so with max_groups=3 the
# plain chart folds Pair into Other, the deduped chart folds Dupes.
DEDUPE_RECORDS = [
    {"id": "b1", "fields": {"Name": "B1", "Status": "Big", "Program": "X"}},
    {"id": "b2", "fields": {"Name": "B2", "Status": "Big", "Program": "Y"}},
    {"id": "b3", "fields": {"Name": "B3", "Status": "Big", "Program": "Z"}},
    {"id": "d1", "fields": {"Name": "D1", "Status": "Dupes", "Program": "Same"}},
    {"id": "d2", "fields": {"Name": "D2", "Status": "Dupes", "Program": "same"}},
    {"id": "d3", "fields": {"Name": "D3", "Status": "Dupes", "Program": "SAME."}},
    {"id": "p1", "fields": {"Name": "P1", "Status": "Pair", "Program": "Q"}},
    {"id": "p2", "fields": {"Name": "P2", "Status": "Pair"}},
]


class ChartDedupeDrilldownServiceTests(unittest.IsolatedAsyncioTestCase):
    _patched = cache_tests.WidgetCacheServiceTests._patched

    async def _chart_and_other(self, **dedupe):
        settings, fake_cache, api_patch, cache_patch = self._patched(DEDUPE_RECORDS)
        common = {
            "link": "dd-dedupe",
            "url": FAKE_URL,
            "api_key": "pat",
            "caller_email": "anyone@example.com",
            "group_field": "Status",
            "aggregation": "count",
            "max_groups": 3,
            **dedupe,
        }
        with api_patch, cache_patch:
            service = AirtableService(settings)
            chart = await service.fetch_widget_chart_cached(**common)
            drill = await service.fetch_widget_drilldown_cached(
                widget_type=AIRTABLE_CHART_WIDGET_TYPE,
                title_field="Name",
                detail_fields=[],
                other=True,
                **common,
            )
        return chart, drill

    async def test_other_group_set_matches_the_deduped_chart(self) -> None:
        plain_chart, plain_drill = await self._chart_and_other(count_field="Program")
        self.assertEqual({r.group for r in plain_drill.rows}, {"Pair"})

        chart, drill = await self._chart_and_other(count_field="Program", count_distinct=True)
        self.assertEqual(
            [(g.label, g.value) for g in chart.groups],
            [("Big", 3), ("Pair", 2), ("Other", 1)],
        )
        self.assertEqual({r.group for r in drill.rows}, {"Dupes"})
        # count_distinct alone still lists every record (2026-10-02).
        self.assertEqual(sorted(r.id for r in drill.rows), ["d1", "d2", "d3"])

    async def test_ignore_empty_lists_only_counted_records(self) -> None:
        resp = await self._drill(
            DEDUPE_RECORDS,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Status",
            group="Pair",
            count_field="Program",
            count_ignore_empty=True,
        )
        self.assertEqual([r.id for r in resp.rows], ["p1"])

    async def test_zero_value_group_opens_an_empty_list(self) -> None:
        records = [
            {"id": "a", "fields": {"Name": "A", "Status": "Done"}},
            {"id": "b", "fields": {"Name": "B", "Status": "Done", "Program": "—"}},
        ]
        resp = await self._drill(
            records,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Status",
            group="Done",
            count_field="Program",
            count_ignore_empty=True,
        )
        self.assertTrue(resp.available)
        self.assertEqual(resp.rows, [])
        self.assertEqual(resp.total_rows, 0)

    async def test_ignore_empty_applies_to_other_too(self) -> None:
        _chart, drill = await self._chart_and_other(
            count_field="Program", count_ignore_empty=True, max_groups=2
        )
        # Kept: Big (3). Folded: Dupes (3) + Pair (1) — p2 has no Program.
        self.assertEqual(sorted(r.id for r in drill.rows), ["d1", "d2", "d3", "p1"])

    async def test_dedupe_field_equal_to_group_field_is_ignored(self) -> None:
        resp = await self._drill(
            DEDUPE_RECORDS,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Status",
            group="Pair",
            count_field="Status",
            count_ignore_empty=True,
        )
        self.assertEqual(sorted(r.id for r in resp.rows), ["p1", "p2"])

    async def test_sum_chart_ignores_count_ignore_empty(self) -> None:
        resp = await self._drill(
            DEDUPE_RECORDS,
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            group_field="Status",
            group="Pair",
            aggregation="sum",
            sum_field="Program",
            count_field="Program",
            count_ignore_empty=True,
        )
        self.assertEqual(sorted(r.id for r in resp.rows), ["p1", "p2"])

    _drill = DrilldownServiceTests._drill


class DrilldownHttpTests(_AirtableCacheFixture):
    def _get(self, link: str, *, params=None, headers=None):
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(REGION_RECORDS)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            return self.client.get(
                f"/data/airtable/component/{link}/drilldown",
                params=params or {},
                headers=headers or self._member_auth("someone@example.com"),
            )

    def test_metric_drilldown_returns_rows(self) -> None:
        self._seed_widget(
            "dd-metric",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={
                "aggregation": "count",
                "clickAction": "modal",
                "drilldownTitleField": "Name",
                "drilldownFields": ["Region"],
            },
        )
        resp = self._get("dd-metric")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total_rows"], 5)
        self.assertEqual(body["fields"], ["Region"])
        self.assertNotIn("Secret", body["rows"][0]["fields"])

    def test_chart_drilldown_by_group(self) -> None:
        self._seed_widget(
            "dd-chart",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "aggregation": "count",
                "clickAction": "modal",
                "drilldownTitleField": "Name",
            },
        )
        resp = self._get("dd-chart", params={"group": "West"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([r["id"] for r in resp.json()["rows"]], ["r3"])

    def test_chart_drilldown_reads_stored_dedupe_settings(self) -> None:
        # North's only record (r4) has no Name: with ignore-empty its bar is
        # 0 and its drill-down lists nothing ("only counted records").
        self._seed_widget(
            "dd-chart-dedupe",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "aggregation": "count",
                "countField": "Name",
                "countIgnoreEmpty": True,
                "clickAction": "modal",
                "drilldownTitleField": "Name",
            },
        )
        resp = self._get("dd-chart-dedupe", params={"group": "North"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["rows"], [])
        east = self._get("dd-chart-dedupe", params={"group": "East"})
        self.assertEqual(sorted(r["id"] for r in east.json()["rows"]), ["r1", "r2"])

    def test_chart_drilldown_requires_a_group(self) -> None:
        self._seed_widget(
            "dd-chart-nogroup",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "clickAction": "modal",
                "drilldownTitleField": "Name",
            },
        )
        self.assertEqual(self._get("dd-chart-nogroup").status_code, 400)

    def test_refused_when_modal_not_enabled(self) -> None:
        self._seed_widget(
            "dd-off",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count", "drilldownTitleField": "Name"},
        )
        self.assertEqual(self._get("dd-off").status_code, 400)

    def test_refused_without_title_field(self) -> None:
        self._seed_widget(
            "dd-notitle",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count", "clickAction": "modal"},
        )
        self.assertEqual(self._get("dd-notitle").status_code, 400)

    def test_refused_for_table_widget(self) -> None:
        self._seed_widget(
            "dd-table",
            widget_type=AIRTABLE_WIDGET_TYPE,
            extra_data={"clickAction": "modal", "drilldownTitleField": "Name"},
        )
        self.assertEqual(self._get("dd-table").status_code, 400)

    def test_enforces_access_control(self) -> None:
        widget = self._seed_widget(
            "dd-ac",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={
                "aggregation": "count",
                "clickAction": "modal",
                "drilldownTitleField": "Name",
            },
        )
        self._grant(email="allowed@example.com", node=("component", widget.id))
        denied = self._get(
            "dd-ac", headers=self._auth("outsider@example.com", roles=["Hub Member"])
        )
        self.assertEqual(denied.status_code, 404)
        allowed = self._get("dd-ac", headers=self._auth("allowed@example.com"))
        self.assertEqual(allowed.status_code, 200, allowed.text)


if __name__ == "__main__":
    unittest.main()
