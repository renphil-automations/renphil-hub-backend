"""The raw list-records GETs switch to `POST …/listRecords` past Airtable's
URL-length limit (advanced-filters arc, phase 3a).

Every raw `api.request("get", table.urls.records, …)` in
`airtable_service.py` goes through `_list_records_page`, which passes the same
`fallback=("post", table.urls.records_post)` pyairtable's own `Table.iterate`
uses. These tests run pyairtable's REAL switch-over: `Api` is a real
`pyairtable.Api` whose `session.request` (the very last hop, after
`Api.request` has already built and measured the prepared GET URL) is
replaced with a recorder that serves pages from memory. Nothing reaches the
network.

Run with:
    TAB_LOCK_TTL_SECONDS=14400 PYTHONPATH=. ./.venv/Scripts/python.exe -m unittest tests.test_airtable_list_records_fallback
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

import requests
from pyairtable import Api

from app.helpers import airtable_formulas as af
from app.services import airtable_service as svc_module
from app.services.airtable_service import AirtableService, _list_records_page

FAKE_URL = "https://airtable.com/appFAKE00000000AA/tblFAKE00000000BB/viwFAKE00000000CC"
BASE_ID = "appFAKE00000000AA"
TABLE_ID = "tblFAKE00000000BB"
VIEW_ID = "viwFAKE00000000CC"


def _records(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"rec{i:014d}", "fields": {"Name": f"row {i}", "Status": "Active", "Amount": i}}
        for i in range(n)
    ]


def _long_filters(tags: int = 400) -> list[dict[str, Any]]:
    """One multi-value `eq` condition whose compiled formula alone is far past
    16,000 URL characters — the shape the condition caps don't bound."""
    return [
        {
            "field": "Status",
            "operator": "eq",
            "value": [f"value-{i:04d}-padding-padding-pad" for i in range(tags)],
        }
    ]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.text = "x"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _ForbiddenResponse:
    text = ""

    def raise_for_status(self) -> None:
        raise requests.HTTPError("403 Forbidden (fake: PAT lacks schema.bases:read)")

    def json(self) -> dict[str, Any]:
        return {}


def make_recording_api_class(records: list[dict[str, Any]], calls: list[dict[str, Any]]):
    """A REAL `pyairtable.Api` subclass. Only `session.request` is replaced,
    so `Api.request`'s own URL measurement and GET→POST switch run as
    shipped. Serves `records` with real paging: `offset`/`pageSize` are read
    from the query string on a GET and from the JSON body on a POST."""

    class _RecordingApi(Api):
        def __init__(self, api_key: str, **kwargs: Any) -> None:
            super().__init__(api_key, **kwargs)

            def fake_request(method: str, url: str, params=None, json=None, **_: Any):
                calls.append(
                    {"method": method.lower(), "url": str(url), "params": dict(params or {}), "json": json}
                )
                if "/meta/" in str(url):
                    # The field-type-hint schema read: answer like a PAT
                    # without `schema.bases:read`, the degrade path the other
                    # Airtable test fakes model too.
                    return _ForbiddenResponse()
                source = json if method.lower() == "post" else (params or {})
                source = source or {}
                page_size = int(source.get("pageSize") or source.get("maxRecords") or 100)
                start = int(source.get("offset") or 0)
                page = records[start : start + page_size]
                next_offset = str(start + page_size) if start + page_size < len(records) else None
                payload: dict[str, Any] = {"records": page}
                if next_offset:
                    payload["offset"] = next_offset
                return _FakeResponse(payload)

            self.session.request = fake_request  # type: ignore[method-assign]

    return _RecordingApi


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        AIRTABLE_API_KEY="fake-master-key",
        AIRTABLE_CACHE_TTL_SECONDS=1800,
        AIRTABLE_CACHE_MAX_ROWS=50_000,
        AIRTABLE_CACHE_MAX_BYTES=50_000_000,
        AIRTABLE_CACHE_MIN_REFRESH_SECONDS=240,
        AIRTABLE_CACHE_REFRESH_LOCK_SECONDS=300,
        AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS=60,
        AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS=900,
        AIRTABLE_SCHEMA_CACHE_TTL_SECONDS=21_600,
        AIRTABLE_WIDGET_FULL_VIEW_MAX_ROWS=10_000,
        AIRTABLE_WIDGET_FULL_VIEW_PAGE_SIZE=100,
        AIRTABLE_CHART_DEFAULT_MAX_GROUPS=10,
        AIRTABLE_CHART_MAX_GROUPS=50,
    )


def _prepared_get_url_length(options: dict[str, Any]) -> int:
    """What `Api.request` measures, computed independently — the precondition
    that a "long" fixture really is past the limit."""
    from pyairtable.api.params import options_to_params

    api = Api("fake")
    table = api.table(BASE_ID, TABLE_ID)
    prepared = requests.Session().prepare_request(
        requests.Request("GET", url=str(table.urls.records), params=options_to_params(options))
    )
    return len(str(prepared.url))


class ListRecordsPageHelperTests(unittest.TestCase):
    """`_list_records_page` directly, against a real `Api`."""

    def _api(self, records, calls):
        api = make_recording_api_class(records, calls)("fake-key")
        return api, api.table(BASE_ID, TABLE_ID)

    def test_under_limit_is_an_unchanged_get(self) -> None:
        calls: list = []
        api, table = self._api(_records(3), calls)
        formula = "{Status} = 'Active'"
        options = {"page_size": 100, "view": VIEW_ID, "fields": ["Name"], "formula": formula}

        payload = _list_records_page(api, table, options)

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "get")
        self.assertEqual(call["url"], str(table.urls.records))
        self.assertIsNone(call["json"])
        self.assertEqual(call["params"]["filterByFormula"], formula)
        self.assertEqual(call["params"]["pageSize"], 100)
        self.assertEqual(call["params"]["view"], VIEW_ID)
        self.assertEqual(call["params"]["fields[]"], ["Name"])
        self.assertEqual(len(payload["records"]), 3)

    def test_over_limit_switches_to_post_list_records_with_options_in_body(self) -> None:
        formula = af.widget_filters_clause(_long_filters())
        options = {
            "page_size": 100,
            "view": VIEW_ID,
            "fields": ["Name", "Status"],
            "formula": formula,
            "offset": "100",
        }
        self.assertGreaterEqual(_prepared_get_url_length(options), Api.MAX_URL_LENGTH)

        calls: list = []
        api, table = self._api(_records(3), calls)
        _list_records_page(api, table, options)

        self.assertEqual(len(calls), 1, "one POST, no GET attempted first")
        call = calls[0]
        self.assertEqual(call["method"], "post")
        self.assertEqual(call["url"], str(table.urls.records_post))
        self.assertTrue(call["url"].endswith("/listRecords"))
        self.assertEqual(call["params"], {})
        self.assertEqual(
            call["json"],
            {
                "filterByFormula": formula,
                "offset": "100",
                "pageSize": 100,
                "fields": ["Name", "Status"],
                "view": VIEW_ID,
            },
        )

    def test_over_limit_by_selected_columns_alone_also_switches(self) -> None:
        """The limit isn't just a filter concern — many long selected columns
        push the URL over too."""
        fields = [f"A fairly long column name number {i:03d} with padding" for i in range(400)]
        options = {"page_size": 100, "fields": fields}
        self.assertGreaterEqual(_prepared_get_url_length(options), Api.MAX_URL_LENGTH)

        calls: list = []
        api, table = self._api(_records(1), calls)
        _list_records_page(api, table, options)
        self.assertEqual(calls[0]["method"], "post")
        self.assertEqual(calls[0]["json"]["fields"], fields)


class ServicePathsUseFallbackTests(unittest.IsolatedAsyncioTestCase):
    """All four raw list-records call sites, driven through the real service
    methods with a long compiled filter. Each must come back with rows (not an
    error) via POST."""

    def setUp(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.records = _records(250)
        self.api_patch = mock.patch(
            "app.services.airtable_service.Api", make_recording_api_class(self.records, self.calls)
        )
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        # No throttle between walk pages — keeps the walk test fast.
        throttle = mock.patch.object(AirtableService, "_WALK_THROTTLE_SECONDS", 0)
        throttle.start()
        self.addCleanup(throttle.stop)
        self.service = AirtableService(_settings())

    def _list_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "/meta/" not in c["url"]]

    async def test_fetch_widget_rows_pages_over_post(self) -> None:
        page1 = await self.service.fetch_widget_rows(
            url=FAKE_URL,
            api_key="pat",
            caller_email="anyone@example.com",
            selected_columns=["Name", "Status"],
            filters=_long_filters(),
        )
        self.assertEqual(len(page1.rows), 100)
        self.assertEqual(page1.next_cursor, "100")

        page2 = await self.service.fetch_widget_rows(
            url=FAKE_URL,
            api_key="pat",
            caller_email="anyone@example.com",
            selected_columns=["Name", "Status"],
            filters=_long_filters(),
            cursor=page1.next_cursor,
        )
        self.assertEqual(len(page2.rows), 100)
        self.assertEqual(page2.next_cursor, "200")

        calls = self._list_calls()
        self.assertEqual([c["method"] for c in calls], ["post", "post"])
        self.assertNotIn("offset", calls[0]["json"])
        self.assertEqual(calls[1]["json"]["offset"], "100")
        self.assertEqual(calls[1]["json"]["filterByFormula"], af.widget_filters_clause(_long_filters()))

    async def test_fetch_widget_rows_short_filter_stays_get(self) -> None:
        await self.service.fetch_widget_rows(
            url=FAKE_URL,
            api_key="pat",
            caller_email="anyone@example.com",
            filters=[{"field": "Status", "operator": "eq", "value": "Active"}],
        )
        calls = self._list_calls()
        self.assertEqual([c["method"] for c in calls], ["get"])
        self.assertEqual(calls[0]["params"]["filterByFormula"], "{Status} = 'Active'")

    async def test_cache_walk_follows_offset_across_post_pages(self) -> None:
        api = svc_module.Api("pat")
        table = api.table(BASE_ID, TABLE_ID)
        formula = af.widget_filters_clause(_long_filters())

        rows, _fields, oversized = await self.service._walk_full_table(
            api=api, table=table, view_id=VIEW_ID, fetch_fields=None, formula=formula
        )

        self.assertFalse(oversized)
        self.assertEqual(len(rows), 250, "every page reached — offset carried in the POST body")
        calls = self._list_calls()
        self.assertEqual([c["method"] for c in calls], ["post", "post", "post"])
        self.assertEqual(
            [c["json"].get("offset") for c in calls], [None, "100", "200"]
        )
        for c in calls:
            self.assertEqual(c["json"]["filterByFormula"], formula)
            self.assertEqual(c["json"]["view"], VIEW_ID)

    async def test_preview_widget_config_uses_post(self) -> None:
        preview = await self.service.preview_widget_config(
            url=FAKE_URL,
            api_key="pat",
            caller_email="anyone@example.com",
            filters=_long_filters(),
        )
        self.assertGreater(len(preview.rows), 0)
        calls = self._list_calls()
        self.assertTrue(calls)
        self.assertTrue(all(c["method"] == "post" for c in calls))
        self.assertIn("maxRecords", calls[0]["json"])

    async def test_preview_widget_chart_live_path_uses_post(self) -> None:
        chart = await self.service.preview_widget_chart(
            link=None,
            url=FAKE_URL,
            api_key="pat",
            caller_email="anyone@example.com",
            group_field="Status",
            aggregation="count",
            filters=_long_filters(),
        )
        self.assertTrue(chart.groups)
        calls = self._list_calls()
        self.assertTrue(calls)
        self.assertTrue(all(c["method"] == "post" for c in calls))


if __name__ == "__main__":
    unittest.main()
