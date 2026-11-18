"""
Tests for the Airtable widget row cache
(plan_airtable_widget_caching_2026-08-06.md).

Covers, per the plan's own §9:
  * Cache miss warms + serves a subsequent hit without re-walking Airtable.
  * The projection trap, both directions (L2).
  * Fail-closed personalization never reads OR writes the cache (§6.2).
  * Oversized tables are never cached and are still served correctly (§5.4).
  * Synthetic cursor defensive parsing restarts at page 1 on a foreign
    cursor rather than erroring (§5.3).
  * A disabled cache (no Upstash configured) is a pure pass-through.
  * Authorization is NOT bypassed by a warm cache — the regression test for
    the entire reason `@airtable_cache` was rejected for this endpoint (§3.1).
  * The refresh endpoint's shared-secret auth and summary shape.

Plus, per plan_airtable_cache_scaling_2026-08-08.md §9 (Tier 1):
  * §4.1 — the walk's incremental byte accounting decides oversize at
    byte-for-byte the same threshold the old whole-list re-encode did.
  * §4.2 — the row cap is a sanity ceiling at 50,000, not the thing deciding
    what gets cached.
  * §4.3 — compression round-trips, is opt-in, and UNMARKED legacy entries
    (the 123 `@airtable_cache` endpoints' existing values) still decode (L2).
  * §4.4 — a scheduled refresh skips a fresh entry and skips when the
    single-flight lock is held; both are skips, never failures (L9).
  * §4.5 — a disabled cache does not walk the table; an Upstash outage
    degrades to live rather than 500ing.
  * §4.6 — oversized is counted separately from failed.
  * §4.7 — the rows endpoint issues 2 queries, not 6, and the bundle never
    exposes the raw blob or leaks/clears the PAT (L12).

Airtable itself is never called — `pyairtable.Api` is replaced with a fake
that serves an in-memory record list with REAL pagination (page_size +
offset) and REAL field projection (`options["fields"]`), so these tests
exercise the actual walk/projection code, not a canned response. Similarly
`CacheService` is replaced with a small in-memory fake implementing the same
surface `AirtableService` actually calls.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest import mock

import jwt
from fastapi.testclient import TestClient
from requests.exceptions import RequestException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Same fallback-only env defaults as tests/test_nav_tabs.py, so this file is
# independently runnable regardless of import order.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-airtable-cache-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.resource_grant import NODE_COLUMNS, ResourceGrantV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.main import create_app  # noqa: E402
from tests._real_admin import seed_real_admin  # noqa: E402
from app.services.airtable_service import AirtableService  # noqa: E402
from app.services.gridstack_service import (  # noqa: E402
    AIRTABLE_CHART_WIDGET_TYPE,
    AIRTABLE_METRIC_WIDGET_TYPE,
    AIRTABLE_WIDGET_TYPE,
    _write_component_data,
    create_tab_v2,
    update_airtable_component_config,
    update_tab_content_v2,
)
from app.services.nav_tab_service import create_nav_tab_v2, get_nav_tab_by_document_id  # noqa: E402

FAKE_URL = "https://airtable.com/appFAKE00000000AA/tblFAKE00000000BB/viwFAKE00000000CC"


def _make_records(n: int, *, owner_field: str = "OwnerEmail") -> list[dict]:
    return [
        {
            "id": f"rec{i:04d}",
            "fields": {"Name": f"Row {i}", owner_field: f"user{i % 3}@example.com"},
        }
        for i in range(n)
    ]

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


class _FakeAirtableTableHandle:
    class _Urls:
        records = "fake://records"
        records_post = "fake://records/listRecords"

    urls = _Urls()


class _FakeSchemaTable:
    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = [SimpleNamespace(name=k, type=v) for k, v in fields.items()]


class _FakeBaseSchema:
    def __init__(self, fields: dict[str, str] | None, *, unknown_table: bool) -> None:
        self._fields = fields or {}
        self._unknown_table = unknown_table

    def table(self, table_id: str) -> _FakeSchemaTable:
        if self._unknown_table:
            # Mirrors BaseSchema.table()'s real behavior for a stale/renamed
            # table_id: a bare KeyError from `_find`'s dict subscript
            # (pyairtable/models/schema.py:115), not a RequestException.
            raise KeyError(table_id)
        return _FakeSchemaTable(self._fields)


class _FakeBase:
    def __init__(self, fields: dict[str, str] | None, *, unknown_table: bool) -> None:
        self._fields = fields
        self._unknown_table = unknown_table

    def schema(self) -> _FakeBaseSchema:
        return _FakeBaseSchema(self._fields, unknown_table=self._unknown_table)


def make_fake_api_class(
    records: list[dict],
    call_log: list | None = None,
    *,
    constructed: list | None = None,
    schema_fields: dict[str, str] | None = None,
    schema_unknown_table: bool = False,
):
    """A drop-in for `pyairtable.Api`, patched over
    `app.services.airtable_service.Api`. Serves `records` with real
    pagination (`page_size`/`offset`) and real field projection
    (`options["fields"]`) so tests exercise the real walk/projection code.

    `constructed`, if given, collects the `retry_strategy=` kwarg from every
    construction — lets a test prove the service actually passes
    `_WIDGET_RETRY_STRATEGY` through, not just that the walk still works.

    `schema_fields`/`schema_unknown_table` model `.base(id).schema().table(id)`
    for `fetch_table_field_hints` (plan_airtable_widget_viewer_controls_
    2026-08-12.md §5.2):
      * `schema_unknown_table=True` — `.table(table_id)` raises `KeyError`,
        a stale/renamed table_id (`_find`'s dict subscript).
      * `schema_fields` is a dict — `.table(table_id)` succeeds, returning
        that field-name -> Airtable-type map as the schema.
      * `schema_fields is None` (default) and not `schema_unknown_table` —
        `.base()` itself raises `RequestException`, modeling a PAT without
        the `schema.bases:read` scope. This is also what every test NOT
        passing `schema_fields` gets for free: `.base` didn't exist on this
        fake at all before the viewer-controls feature, so any pre-existing
        test that now incidentally reaches `fetch_table_field_hints` (via
        `fetch_widget_rows_cached`/`preview_widget_config` attaching
        `field_types`) degrades the same way a real unscoped PAT would.
    """

    class _FakeApi:
        def __init__(self, api_key: str, *, retry_strategy: object = None) -> None:
            self.api_key = api_key
            self.retry_strategy = retry_strategy
            if constructed is not None:
                constructed.append(retry_strategy)

        def table(self, base_id: str, table_id: str) -> _FakeAirtableTableHandle:
            return _FakeAirtableTableHandle()

        def base(self, base_id: str) -> _FakeBase:
            if schema_fields is None and not schema_unknown_table:
                raise RequestException("PAT lacks schema.bases:read scope")
            return _FakeBase(schema_fields, unknown_table=schema_unknown_table)

        def request(
            self, method: str, url: str, fallback: tuple | None = None, options: dict | None = None
        ) -> dict:
            if call_log is not None:
                call_log.append(dict(options or {}))
            options = options or {}
            page_size = options.get("page_size", 100)
            offset = options.get("offset")
            start = int(offset) if offset else 0
            fields_filter = options.get("fields")

            page = records[start : start + page_size]
            out = []
            for r in page:
                fields = dict(r.get("fields", {}))
                if fields_filter:
                    fields = {k: v for k, v in fields.items() if k in fields_filter}
                out.append({"id": r["id"], "fields": fields})

            next_offset = (
                str(start + page_size) if start + page_size < len(records) else None
            )
            return {"records": out, "offset": next_offset}

    return _FakeApi


class FakeCacheService:
    """In-memory stand-in for `CacheService`, implementing only the surface
    `AirtableService` actually calls."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.store: dict[str, Any] = {}
        #: key -> the ownership token currently holding it, mirroring real
        #: `CacheService`'s token-checked lock (lock-ownership fix): a
        #: `release_lock` call with the wrong token must not clear someone
        #: else's lock.
        self._locks: dict[str, str] = {}
        # Mirrors `CacheService.enabled` — False models Upstash being
        # unconfigured, which `fetch_widget_rows_cached` must treat as "go
        # straight to the live path" rather than walking the whole table
        # (plan_airtable_cache_scaling_2026-08-08.md §4.5.2).
        self.enabled = enabled
        #: `compress=` as passed to each `set()` call, in order.
        self.compress_flags: list[bool] = []
        #: key -> the `compress` passed to the most recent `set()` for it —
        #: lets a test pin the compress flag to a SPECIFIC key (e.g. the row
        #: cache entry) now that a second, independent key (the schema-hints
        #: negative marker) can also land in the same store.
        self.compress_by_key: dict[str, bool] = {}
        #: Mirrors `CacheService.version` — the real service's key-building
        #: uses this for the base-lock key (finding #6); hardcoded "v1" here
        #: matches `build_key`'s own hardcoded `version="v1"` below.
        self.version = "v1"
        #: key -> the `ttl_seconds` passed to the most recent `set()` for
        #: it — lets a test tell the read path's short negative TTL apart
        #: from the scheduled refresh's longer one (finding "C").
        self.ttl_by_key: dict[str, int | None] = {}

    def build_key(self, endpoint_name: str, params: dict) -> str:
        from app.services.cache_service import build_key

        return build_key(endpoint_name, params, version="v1")

    async def get(self, key: str):
        return self.store.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: int | None = None,
        compress: bool = False,
    ) -> None:
        # Stores the live object rather than an encoded string — the
        # encode/decode round trip is `CacheService`'s own concern and is
        # tested directly in `CacheServiceEncodingTests`. Recorded so a test
        # can assert the widget cache actually opts in.
        self.store[key] = value
        self.compress_flags.append(compress)
        self.compress_by_key[key] = compress
        self.ttl_by_key[key] = ttl_seconds

    async def acquire_lock(self, key: str, *, ttl_seconds: int = 120) -> str | None:
        if key in self._locks:
            return None
        token = f"token-{len(self._locks)}-{key}"
        self._locks[key] = token
        return token

    async def release_lock(self, key: str, token: str) -> None:
        # Only clears the lock if `token` still matches — a stale release
        # (the exact bug the lock-ownership fix closes) must be a no-op,
        # not a delete of whatever is currently there.
        if self._locks.get(key) == token:
            del self._locks[key]


class WidgetCacheServiceTests(unittest.IsolatedAsyncioTestCase):
    """Direct AirtableService tests — no HTTP layer, no DB."""

    def _patched(
        self,
        records: list[dict],
        *,
        max_rows: int = 10_000,
        max_bytes: int = 5_000_000,
        call_log=None,
        cache_enabled: bool = True,
        min_refresh_seconds: int = 240,
        negative_ttl_seconds: int = 60,
        refresh_negative_ttl_seconds: int = 900,
        full_view_max_rows: int = 10_000,
        full_view_page_size: int = 100,
        chart_default_max_groups: int = 10,
        chart_max_groups: int = 50,
    ):
        settings = SimpleNamespace(
            AIRTABLE_API_KEY="fake-master-key",
            AIRTABLE_CACHE_TTL_SECONDS=1800,
            AIRTABLE_CACHE_MAX_ROWS=max_rows,
            AIRTABLE_CACHE_MAX_BYTES=max_bytes,
            AIRTABLE_CACHE_MIN_REFRESH_SECONDS=min_refresh_seconds,
            AIRTABLE_CACHE_REFRESH_LOCK_SECONDS=300,
            AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS=negative_ttl_seconds,
            AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS=refresh_negative_ttl_seconds,
            AIRTABLE_SCHEMA_CACHE_TTL_SECONDS=21_600,
            AIRTABLE_WIDGET_FULL_VIEW_MAX_ROWS=full_view_max_rows,
            AIRTABLE_WIDGET_FULL_VIEW_PAGE_SIZE=full_view_page_size,
            AIRTABLE_CHART_DEFAULT_MAX_GROUPS=chart_default_max_groups,
            AIRTABLE_CHART_MAX_GROUPS=chart_max_groups,
            # Lock-contention retry settings added by 0432aca (2026-09-28)
            # without updating this fixture. Real attempt count, zero delays
            # so a contended `patient` caller never actually sleeps here.
            AIRTABLE_CACHE_LOCK_RETRY_MAX_ATTEMPTS=4,
            AIRTABLE_CACHE_LOCK_RETRY_BASE_INTERVAL_SECONDS=0.0,
            AIRTABLE_CACHE_BASE_LOCK_JITTER_MIN_SECONDS=0.0,
            AIRTABLE_CACHE_BASE_LOCK_JITTER_MAX_SECONDS=0.0,
            AIRTABLE_CACHE_FINGERPRINT_LOCK_JITTER_GAP_SECONDS=0.0,
            AIRTABLE_CACHE_FINGERPRINT_LOCK_JITTER_RANGE_SECONDS=0.0,
        )
        fake_cache = FakeCacheService(enabled=cache_enabled)
        api_patch = mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records, call_log)
        )
        cache_patch = mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        )
        return settings, fake_cache, api_patch, cache_patch

    async def test_cache_warms_on_miss_and_serves_hit_without_rewalk(self) -> None:
        records = _make_records(250)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)

            page1 = await service.fetch_widget_rows_cached(
                link="w1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
                cursor=None,
            )
            self.assertEqual(len(page1.rows), 100)
            self.assertEqual(page1.next_cursor, "idx:100")
            self.assertEqual(len(call_log), 3)  # 250 rows / 100 page_size, walked once
            self.assertTrue(fake_cache.store)

            calls_before = len(call_log)

            page2 = await service.fetch_widget_rows_cached(
                link="w1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
                cursor="idx:100",
            )
            self.assertEqual(len(page2.rows), 100)
            self.assertEqual(page2.next_cursor, "idx:200")
            self.assertEqual(len(call_log), calls_before, "cache hit must not re-walk Airtable")

            page3 = await service.fetch_widget_rows_cached(
                link="w1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
                cursor="idx:200",
            )
            self.assertEqual(len(page3.rows), 50)
            self.assertIsNone(page3.next_cursor)
            self.assertEqual(len(call_log), calls_before)

    async def test_projection_trap_auto_appended_column_is_stripped(self) -> None:
        # L2, direction 1: personalize column NOT in selectedColumns must be
        # fetched (so Python can match against it) but never returned.
        records = _make_records(6)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_rows_cached(
                link="w2",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                selected_columns=["Name"],
                filters=None,
                personalize_enabled=True,
                personalize_column="OwnerEmail",
                cursor=None,
            )
            self.assertEqual(resp.fields, ["Name"])
            self.assertTrue(resp.rows, "user0@example.com should match some rows")
            for row in resp.rows:
                self.assertIn("Name", row)
                self.assertNotIn("OwnerEmail", row)

    async def test_projection_trap_explicit_column_stays_visible(self) -> None:
        # L2, direction 2 (symmetric): if the admin DID select the
        # personalize column, it must still show up.
        records = _make_records(6)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_rows_cached(
                link="w3",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                selected_columns=["Name", "OwnerEmail"],
                filters=None,
                personalize_enabled=True,
                personalize_column="OwnerEmail",
                cursor=None,
            )
            self.assertEqual(resp.fields, ["Name", "OwnerEmail"])
            self.assertTrue(resp.rows)
            for row in resp.rows:
                self.assertIn("OwnerEmail", row)

    async def test_personalize_fail_closed_never_touches_cache(self) -> None:
        records = _make_records(6)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_rows_cached(
                link="w4",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=True,
                personalize_column=None,  # not configured
                cursor=None,
            )
            self.assertEqual(resp.rows, [])
            self.assertTrue(resp.personalize_blocked)
            self.assertEqual(call_log, [], "a blocked request must never call Airtable")
            self.assertEqual(fake_cache.store, {}, "a blocked request must never touch the cache")

    async def test_oversized_table_falls_back_live_and_is_not_cached(self) -> None:
        records = _make_records(12)
        settings, fake_cache, api_patch, cache_patch = self._patched(records, max_rows=5)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_rows_cached(
                link="w5",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
                cursor=None,
            )
            # Served live and complete — never truncated to the row cap.
            self.assertEqual(len(resp.rows), 12)
            self.assertIsNone(resp.next_cursor)
            # No entry holds `rows` — the real envelope is never cached. A
            # short-TTL negative marker IS expected here (finding #3, see
            # the dedicated re-walk test below) so the store is not empty.
            self.assertFalse(
                any("rows" in v for v in fake_cache.store.values() if isinstance(v, dict)),
                "an oversized table must never be cached as real data",
            )

    async def test_oversized_widget_is_negative_cached_and_not_rewalked(self) -> None:
        # Finding #3: without a negative-cache marker, an oversized widget
        # repeats its FULL throttled walk to the row cap on every single
        # request, discards it, then serves one live page — forever. The
        # marker must make the SECOND request skip the walk entirely.
        records = _make_records(12)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, max_rows=5, call_log=call_log
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            kwargs = dict(
                link="w-oversized",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )

            first = await service.fetch_widget_rows_cached(cursor=None, **kwargs)
            self.assertEqual(len(first.rows), 12)
            # 1 call for the aborted oversized walk (all 12 rows fit in one
            # page, already over max_rows=5) + 1 for the live single-page
            # fallback that actually serves the response.
            self.assertEqual(len(call_log), 2)

            second = await service.fetch_widget_rows_cached(cursor=None, **kwargs)
            self.assertEqual(
                len(second.rows), 12, "still served live while the marker is fresh"
            )
            # Only the live fallback's 1 call — the fresh negative marker
            # must skip repeating the already-known-oversized full walk.
            self.assertEqual(len(call_log), 3)

    async def test_read_path_degrades_to_live_when_the_walk_fails(self) -> None:
        # Finding #2: every OTHER miss-path exit (oversized, lock-wait
        # timeout, disabled cache) degrades to the live single-page path.
        # A walk failure (e.g. an Airtable 429 mid-walk) must too, instead
        # of propagating as a bare 502 to the caller.
        from app.helpers.exceptions import AirtableError

        records = _make_records(5)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, call_log=call_log
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            real_walk = service._build_widget_cache_envelope
            kwargs = dict(
                link="w-flaky",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )

            flaky = mock.AsyncMock(side_effect=AirtableError("simulated Airtable 429"))
            with mock.patch.object(service, "_build_widget_cache_envelope", flaky):
                first = await service.fetch_widget_rows_cached(cursor=None, **kwargs)
            self.assertEqual(len(first.rows), 5, "must degrade to live, not raise")
            self.assertEqual(flaky.await_count, 1)

            negative_entries = {
                k: v for k, v in fake_cache.store.items() if k.endswith(":negative")
            }
            self.assertEqual(len(negative_entries), 1)
            self.assertEqual(next(iter(negative_entries.values()))["reason"], "walk_failed")
            self.assertFalse(
                any("rows" in v for v in fake_cache.store.values() if isinstance(v, dict)),
                "a failed walk must never be cached as real data",
            )

            # A second request — even with a HEALTHY walk available this
            # time — must not attempt it at all: the fresh negative marker
            # alone must short-circuit straight to the live path (finding
            # #3's generalization to walk failures, not just oversize).
            spy = mock.AsyncMock(side_effect=real_walk)
            with mock.patch.object(service, "_build_widget_cache_envelope", spy):
                second = await service.fetch_widget_rows_cached(cursor=None, **kwargs)
            self.assertEqual(len(second.rows), 5, "still degrades to live while marker is fresh")
            spy.assert_not_awaited()

    async def test_foreign_cursor_restarts_at_page_one(self) -> None:
        records = _make_records(150)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            kwargs = dict(
                link="w6",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )
            first = await service.fetch_widget_rows_cached(cursor=None, **kwargs)
            # A legacy real-Airtable-style offset, or anything else that
            # isn't our own `idx:<n>` shape.
            foreign = await service.fetch_widget_rows_cached(cursor="recABCDEF1234567", **kwargs)
            self.assertEqual([r["id"] for r in first.rows], [r["id"] for r in foreign.rows])


class WidgetSchemaHintsServiceTests(unittest.IsolatedAsyncioTestCase):
    """`AirtableService.fetch_table_field_hints` /
    `fetch_table_field_hints_with_status`
    (plan_airtable_widget_viewer_controls_2026-08-12.md §2.2, §5.3).
    """

    _patched = WidgetCacheServiceTests._patched

    def _api_patch(self, **schema_kwargs):
        return mock.patch(
            "app.services.airtable_service.Api",
            make_fake_api_class([], **schema_kwargs),
        )

    async def test_happy_path_maps_correctly_and_excludes_record_links(self) -> None:
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        schema = {
            "Website": "url",
            "Apply": "button",
            "Status": "singleSelect",
            "Tags": "multipleSelects",
            "Owners": "multipleCollaborators",
            "Notes": "multilineText",
            # Deliberately excluded types — must NOT appear in the hints.
            "Linked Grants": "multipleRecordLinks",
            "Rollup Total": "multipleLookupValues",
        }
        with self._api_patch(schema_fields=schema), cache_patch:
            service = AirtableService(settings)
            hints = await service.fetch_table_field_hints(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
        self.assertEqual(
            hints,
            {
                "Website": "url",
                "Apply": "url",
                "Status": "select",
                "Tags": "select",
                "Owners": "select",
            },
        )
        self.assertNotIn("Linked Grants", hints)
        self.assertNotIn("Rollup Total", hints)
        self.assertNotIn("Notes", hints)

    async def test_scope_failure_degrades_to_empty_and_negative_caches(self) -> None:
        settings, fake_cache, _api_patch, cache_patch = self._patched([])

        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class([], schema_fields=None)
        ), cache_patch:
            service = AirtableService(settings)
            first = await service.fetch_table_field_hints(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
            self.assertEqual(first, {})
            negative_entries = {
                k: v for k, v in fake_cache.store.items() if k.endswith(":schema_negative")
            }
            self.assertEqual(len(negative_entries), 1)
            self.assertEqual(next(iter(negative_entries.values()))["reason"], "fetch_failed")

            # A second call inside the TTL must not re-hit the fake API at
            # all — proven by making the API construction itself raise.
            def _boom(*args, **kwargs):
                raise AssertionError("must not re-fetch schema while negative-cached")

            with mock.patch("app.services.airtable_service.Api", _boom):
                second = await service.fetch_table_field_hints(
                    base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
                )
            self.assertEqual(second, {})

    async def test_unknown_table_id_degrades_to_empty_rather_than_raising(self) -> None:
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        with self._api_patch(schema_unknown_table=True), cache_patch:
            service = AirtableService(settings)
            hints = await service.fetch_table_field_hints(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
        self.assertEqual(hints, {})
        negative_entries = {
            k: v for k, v in fake_cache.store.items() if k.endswith(":schema_negative")
        }
        self.assertEqual(len(negative_entries), 1)
        self.assertEqual(
            next(iter(negative_entries.values()))["reason"], "unexpected_error"
        )

    async def test_disabled_cache_returns_empty_with_zero_airtable_calls(self) -> None:
        settings, fake_cache, _api_patch, cache_patch = self._patched(
            [], cache_enabled=False
        )

        class _NoCallsApi:
            """Construction (`AirtableService.__init__`'s own `Api(...)`)
            must still succeed — only a REAL schema/row call is disallowed."""

            def __init__(self, api_key: str, *, retry_strategy: object = None) -> None:
                pass

            def base(self, base_id: str):
                raise AssertionError("a disabled cache must never call Airtable for schema")

            def table(self, base_id: str, table_id: str):
                raise AssertionError("a disabled cache must never call Airtable for rows")

            def request(self, *args, **kwargs):
                raise AssertionError("a disabled cache must never call Airtable for rows")

        with mock.patch("app.services.airtable_service.Api", _NoCallsApi), cache_patch:
            service = AirtableService(settings)
            hints = await service.fetch_table_field_hints(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
        self.assertEqual(hints, {})
        self.assertEqual(fake_cache.store, {})

    async def test_with_status_reports_available_true_on_success(self) -> None:
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        with self._api_patch(schema_fields={"Website": "url"}), cache_patch:
            service = AirtableService(settings)
            hints, available = await service.fetch_table_field_hints_with_status(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
        self.assertEqual(hints, {"Website": "url"})
        self.assertTrue(available)

    async def test_with_status_reports_available_true_when_schema_has_no_typed_columns(
        self,
    ) -> None:
        # A table with no URL/select columns is a legitimate {} — must NOT
        # be confused with a missing scope.
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        with self._api_patch(schema_fields={"Notes": "multilineText"}), cache_patch:
            service = AirtableService(settings)
            hints, available = await service.fetch_table_field_hints_with_status(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
        self.assertEqual(hints, {})
        self.assertTrue(available)

    async def test_with_status_reports_available_false_on_scope_failure(self) -> None:
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class([], schema_fields=None)
        ), cache_patch:
            service = AirtableService(settings)
            hints, available = await service.fetch_table_field_hints_with_status(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
        self.assertEqual(hints, {})
        self.assertFalse(available)

    async def test_with_status_does_not_trust_a_cached_positive_from_a_different_token(
        self,
    ) -> None:
        """The security-relevant regression test: `available` must reflect
        THIS `api_key`, never a different, more-privileged token's cached
        success. Pre-seeds the shared {base_id, table_id} cache with a
        positive entry (as a stronger token would have warmed it), then
        calls `fetch_table_field_hints_with_status` with a `pat` the fake API
        is configured to REJECT (schema_fields=None -> `.base()` raises,
        modeling a PAT without `schema.bases:read`).

        Before the `force_live` fix, this would have returned the STALE
        cached hints with `available=True` without ever touching the API at
        all — exactly the gap a `data.records:read`-only token exploited to
        enable the Property Panel's viewer-controls toggle by riding a
        different widget's warm schema cache.
        """
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        cache_key = fake_cache.build_key(
            "widget_schema",
            {"base_id": "appFAKE00000000AA", "table_id": "tblFAKE00000000BB"},
        )
        fake_cache.store[cache_key] = {"Website": "url"}  # warmed by a good token

        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class([], schema_fields=None)
        ), cache_patch:
            service = AirtableService(settings)
            hints, available = await service.fetch_table_field_hints_with_status(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="weak-pat"
            )
        self.assertEqual(hints, {})
        self.assertFalse(available)
        # The live failure must also correctly refresh the negative marker —
        # not leave the stale positive entry from the other token in place.
        self.assertIn(f"{cache_key}:schema_negative", fake_cache.store)

    async def test_with_status_ignores_the_disabled_cache_bypass_and_still_calls_live(
        self,
    ) -> None:
        """`fetch_table_field_hints` (bare) treats a disabled cache as
        `available=True` with zero Airtable calls (see the disabled-cache
        test above) — a deliberate dampener for per-viewer-request traffic.
        `_with_status` is a rare, human-triggered admin action, so it must
        NOT get that bypass: skipping it would report every token as
        `available=True` for the entire outage, which is wrong in exactly
        the direction that matters (a scope-less token would sail through).
        """
        settings, fake_cache, _api_patch, cache_patch = self._patched(
            [], cache_enabled=False
        )
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class([], schema_fields=None)
        ), cache_patch:
            service = AirtableService(settings)
            hints, available = await service.fetch_table_field_hints_with_status(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="weak-pat"
            )
        self.assertEqual(hints, {})
        self.assertFalse(available)

    async def test_with_status_still_warms_the_shared_cache_for_rows_and_full_rows(
        self,
    ) -> None:
        """The `force_live` fix must not lose the existing efficiency win —
        a SUCCESSFUL admin-preview check should still warm the shared cache,
        so `/rows` and `/rows/full` (via the bare `fetch_table_field_hints`)
        keep serving from it without their own live Airtable call."""
        settings, fake_cache, _api_patch, cache_patch = self._patched([])
        with self._api_patch(schema_fields={"Website": "url"}), cache_patch:
            service = AirtableService(settings)
            hints, available = await service.fetch_table_field_hints_with_status(
                base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
            )
            self.assertEqual(hints, {"Website": "url"})
            self.assertTrue(available)

            # A second, unrelated call through the bare (viewer-facing)
            # method must be served from what the preview call just warmed —
            # proven by making the API construction itself raise.
            def _boom(*args, **kwargs):
                raise AssertionError("must not re-fetch schema once warmed")

            with mock.patch("app.services.airtable_service.Api", _boom):
                served = await service.fetch_table_field_hints(
                    base_id="appFAKE00000000AA", table_id="tblFAKE00000000BB", api_key="pat"
                )
        self.assertEqual(served, {"Website": "url"})


class CacheServiceDisabledTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_cache_is_a_pure_pass_through(self) -> None:
        from app.services.cache_service import CacheService

        settings = SimpleNamespace(
            UPSTASH_REDIS_REST_URL=None, UPSTASH_REDIS_REST_TOKEN=None, CACHE_VERSION="v1"
        )
        cache = CacheService(settings)
        self.assertFalse(cache.enabled)
        self.assertIsNone(await cache.get("some-key"))
        await cache.set("some-key", {"a": 1}, ttl_seconds=30)  # must not raise
        token = await cache.acquire_lock("some-key:lock")  # fail-open: always "wins"
        self.assertTrue(token)
        await cache.release_lock("some-key:lock", token)  # must not raise


class _FakeRedisLockClient:
    """Minimal in-memory Redis stand-in exercising the exact primitives
    `CacheService`'s lock methods actually send: `SET ... NX EX` and
    `EVAL` of the compare-and-delete script. Enough to prove the
    lock-ownership fix without a real Upstash connection."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(
        self,
        key,
        value,
        nx=None,
        xx=None,
        get=None,
        ex=None,
        px=None,
        exat=None,
        pxat=None,
        keepttl=None,
    ):
        if nx and key in self.store:
            return None  # Upstash returns nil on a refused NX
        self.store[key] = value
        return "OK"

    async def eval(self, script, keys=None, args=None):
        from app.services.cache_service import _RELEASE_IF_OWNER_SCRIPT

        assert script == _RELEASE_IF_OWNER_SCRIPT, "unexpected script — CAS logic changed?"
        key, token = keys[0], args[0]
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed


class CacheServiceLockOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the lock-ownership fix: `release_lock` must
    never delete a lock it does not currently hold
    (plan_airtable_cache_scaling_2026-08-08.md, lock-ownership finding)."""

    def _service(self):
        from app.services.cache_service import CacheService

        return CacheService(
            SimpleNamespace(
                UPSTASH_REDIS_REST_URL="https://fake",
                UPSTASH_REDIS_REST_TOKEN="fake",
                CACHE_VERSION="v1",
            )
        )

    async def test_second_acquire_fails_while_the_first_holds_the_lock(self) -> None:
        service = self._service()
        client = _FakeRedisLockClient()
        with mock.patch.object(service, "_redis", return_value=client):
            first = await service.acquire_lock("k:lock", ttl_seconds=120)
            second = await service.acquire_lock("k:lock", ttl_seconds=120)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_release_deletes_the_lock_it_actually_holds(self) -> None:
        service = self._service()
        client = _FakeRedisLockClient()
        with mock.patch.object(service, "_redis", return_value=client):
            token = await service.acquire_lock("k:lock", ttl_seconds=120)
            await service.release_lock("k:lock", token)
        self.assertNotIn("k:lock", client.store)

    async def test_release_does_not_delete_a_lock_it_no_longer_owns(self) -> None:
        # The exact race the finding describes: holder A's TTL lapses
        # (modeled here as the key having expired out of Redis already),
        # holder B acquires the same key, and only THEN does A's `finally`
        # get around to releasing. A bare `DEL` would delete B's lock; the
        # token-checked release must leave it alone.
        service = self._service()
        client = _FakeRedisLockClient()
        with mock.patch.object(service, "_redis", return_value=client):
            token_a = await service.acquire_lock("k:lock", ttl_seconds=120)
            del client.store["k:lock"]  # simulate A's TTL expiring
            token_b = await service.acquire_lock("k:lock", ttl_seconds=120)
            self.assertIsNotNone(token_b)
            self.assertNotEqual(token_a, token_b)

            await service.release_lock("k:lock", token_a)  # A's late release

        self.assertIn("k:lock", client.store, "B's still-active lock must survive")
        self.assertEqual(client.store["k:lock"], token_b)


class Tier1ServiceTests(unittest.IsolatedAsyncioTestCase):
    """plan_airtable_cache_scaling_2026-08-08.md Tier 1, at the service level."""

    _patched = WidgetCacheServiceTests._patched

    async def _rows(self, service, **overrides):
        kwargs = dict(
            link="tier1-widget",
            url=FAKE_URL,
            api_key="fake-pat",
            caller_email="user0@example.com",
        )
        kwargs.update(overrides)
        return await service.fetch_widget_rows_cached(**kwargs)

    async def test_disabled_cache_does_not_walk_the_table(self) -> None:
        # §4.5.2: with Upstash unconfigured the old code still did
        # get→None, acquire_lock→True (fail-open), a FULL walk, set→no-op —
        # strictly slower than the single-page live path it replaced. The
        # pre-existing `test_disabled_cache_is_a_pure_pass_through` only
        # exercises CacheService in isolation and never asserted this.
        records = _make_records(250)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, call_log=call_log, cache_enabled=False
        )
        with api_patch, cache_patch:
            page = await self._rows(AirtableService(settings))

        self.assertEqual(len(page.rows), 100)
        self.assertEqual(
            len(call_log),
            1,
            f"a disabled cache must serve ONE live page, not walk the table "
            f"({len(call_log)} Airtable requests made)",
        )
        self.assertEqual(fake_cache.store, {})
        # The live path returns a real Airtable offset, not a synthetic one.
        self.assertIsNotNone(page.next_cursor)
        self.assertFalse(str(page.next_cursor).startswith("idx:"))

    async def test_widget_cache_writes_opt_into_compression(self) -> None:
        # §4.3: compression is opt-in per call site; the widget row cache is
        # the only caller that opts in. Asserted on the row-cache key
        # SPECIFICALLY (not `compress_flags` list identity) — since
        # `fetch_widget_rows_cached` now also attaches `field_types`
        # (plan_airtable_widget_viewer_controls_2026-08-12.md §2.5), a second,
        # independent write (the schema-hints negative marker, uncompressed
        # by default — this fixture's fake PAT has no `.base` scope) lands in
        # the same store.
        records = _make_records(20)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            await self._rows(AirtableService(settings))

        row_cache_keys = [
            k for k, v in fake_cache.store.items() if isinstance(v, dict) and "rows" in v
        ]
        self.assertEqual(len(row_cache_keys), 1, fake_cache.store)
        self.assertTrue(fake_cache.compress_by_key[row_cache_keys[0]])

    async def test_incremental_byte_accounting_matches_whole_list_encoding(
        self,
    ) -> None:
        # §4.1: the walk now keeps a running RAW total instead of re-encoding
        # the whole accumulated list every page (quadratic: ~22 s at 500
        # pages). That running total is no longer the oversize guard's OWN
        # threshold (see finding #4 below) — it's the trigger for WHEN to
        # pay for a real compressed measurement — but it still has to be
        # exactly right, or that trigger fires at the wrong point. Proven
        # here by asserting NO real compression check ever happens while
        # the raw total sits below max_bytes: if the running total were
        # wrong (e.g. drifted from what `json.dumps(rows)` actually
        # produces), it would either escalate early (wasting CPU on a
        # perfectly cacheable table) or late (letting a genuinely oversized
        # table slip past the trigger — finding #4 depends on this being
        # exact just as much as the old raw-only guard did).
        import json

        records = _make_records(250)

        async def _walk(max_bytes: int, *, encoded_length_spy=None):
            settings, _cache, _api_patch, _cache_patch = self._patched(
                records, max_bytes=max_bytes
            )
            service = AirtableService(settings)
            patches = []
            if encoded_length_spy is not None:
                patches.append(
                    mock.patch(
                        "app.services.airtable_service.encoded_length", encoded_length_spy
                    )
                )
            for p in patches:
                p.start()
            try:
                # `_walk_full_table` takes the api handle as a parameter, so
                # the fake goes straight in — no patching needed.
                return await service._walk_full_table(
                    api=make_fake_api_class(records)("fake-key"),
                    table=_FakeAirtableTableHandle(),
                    view_id=None,
                    fetch_fields=None,
                    formula=None,
                )
            finally:
                for p in patches:
                    p.stop()

        rows, _fields, oversized = await _walk(5_000_000)
        self.assertFalse(oversized)
        self.assertEqual(len(rows), 250)

        exact = len(json.dumps(rows, separators=(",", ":"), default=str))

        # A cap at (or above) the exact raw size must never even ATTEMPT a
        # real compression measurement — proves the running total is
        # accurate to the byte, since being off by even 1 in either
        # direction would flip whether this spy gets called at all.
        never_called = mock.Mock(side_effect=AssertionError("must not be called"))
        _r, _f, over = await _walk(exact, encoded_length_spy=never_called)
        self.assertFalse(over)
        never_called.assert_not_called()

        # A cap exactly 1 byte below the exact raw size must escalate —
        # i.e. must call the real check at least once — even though the
        # REAL compressed size (checked separately below) sits well under
        # it, so the eventual verdict is still "not oversized".
        from app.services.cache_service import encoded_length as real_encoded_length

        call_count = 0

        def _counting_spy(value, *, compress):
            nonlocal call_count
            call_count += 1
            return real_encoded_length(value, compress=compress)

        _r2, _f2, over2 = await _walk(exact - 1, encoded_length_spy=_counting_spy)
        self.assertGreaterEqual(call_count, 1, "must escalate one byte below the raw cap")
        self.assertFalse(over2, "the REAL compressed size is still well under the cap")

    async def test_oversized_guard_measures_the_real_compressed_size(self) -> None:
        # Finding #4: AIRTABLE_CACHE_MAX_BYTES was checked against the walk's
        # UNCOMPRESSED size even though the entry is stored compressed
        # (§4.3) — ~4.8x more conservative than necessary, and the plan's
        # own numbers show it rejected a 15,000-row table that fit inside
        # every real limit. Proven here at an exact boundary of the REAL
        # compressed size, which a cap between the compressed and raw sizes
        # would have failed under the OLD (raw-only) guard.
        import json

        from app.services.cache_service import encoded_length

        records = _make_records(250)
        settings, _cache, _api_patch, _cache_patch = self._patched(
            records, max_bytes=5_000_000
        )
        service = AirtableService(settings)
        rows, _fields, oversized = await service._walk_full_table(
            api=make_fake_api_class(records)("fake-key"),
            table=_FakeAirtableTableHandle(),
            view_id=None,
            fetch_fields=None,
            formula=None,
        )
        self.assertFalse(oversized)

        raw_len = len(json.dumps(rows, separators=(",", ":"), default=str))
        compressed_len = encoded_length(rows, compress=True)
        self.assertLess(
            compressed_len,
            raw_len,
            "the fixture data must actually compress for this test to be meaningful",
        )

        async def _walk(max_bytes: int):
            settings, _c, _a, _cp = self._patched(records, max_bytes=max_bytes)
            svc = AirtableService(settings)
            return await svc._walk_full_table(
                api=make_fake_api_class(records)("fake-key"),
                table=_FakeAirtableTableHandle(),
                view_id=None,
                fetch_fields=None,
                formula=None,
            )

        # A cap strictly between the two sizes: the OLD raw-based guard
        # would have rejected this table; the fix must not.
        midpoint = (compressed_len + raw_len) // 2
        self.assertGreater(midpoint, compressed_len)
        self.assertLess(midpoint, raw_len)
        _r, _f, over_mid = await _walk(midpoint)
        self.assertFalse(
            over_mid, "a cap above the REAL compressed size must not be oversized"
        )

        # A cap below the real compressed size must still trip it — the
        # guard isn't just permissive now, it's accurate in both directions.
        _r2, _f2, over_low = await _walk(compressed_len - 1)
        self.assertTrue(over_low)

    async def test_small_table_never_pays_for_a_compression_check(self) -> None:
        # The common case (both live widgets today are 3 rows): if the RAW
        # total never crosses max_bytes, compression is guaranteed smaller
        # too, so the real (CPU-costing) check must never even run.
        records = _make_records(5)
        settings, _cache, _api_patch, _cache_patch = self._patched(
            records, max_bytes=5_000_000
        )
        service = AirtableService(settings)
        never_called = mock.Mock(side_effect=AssertionError("must not be called"))
        with mock.patch("app.services.airtable_service.encoded_length", never_called):
            rows, _fields, oversized = await service._walk_full_table(
                api=make_fake_api_class(records)("fake-key"),
                table=_FakeAirtableTableHandle(),
                view_id=None,
                fetch_fields=None,
                formula=None,
            )
        self.assertFalse(oversized)
        self.assertEqual(len(rows), 5)
        never_called.assert_not_called()

    async def test_escalated_walk_does_not_recompress_every_single_page(self) -> None:
        # Once escalated past max_bytes, re-compressing the WHOLE
        # accumulated list on every page would reintroduce the exact
        # quadratic cost §4.1 removed. The real check must only repeat
        # every `_COMPRESSED_SIZE_CHECK_RAW_INTERVAL_BYTES` of RAW growth.
        #
        # max_bytes=7500 sits ABOVE the full 1000-row REAL compressed size
        # (measured ~7.4 KB for this fixture shape) so the walk completes
        # without tripping oversized, but well BELOW the raw size of just a
        # couple of pages (measured ~6.6 KB/page) — escalating from early
        # in the walk and staying escalated, giving the interval logic 9
        # pages' worth of room to skip checks. Values are measured against
        # this fixture, not guessed — a shape change would need re-tuning.
        records = _make_records(1000)  # 10 pages of 100
        settings, _cache, _api_patch, _cache_patch = self._patched(records, max_bytes=7500)
        service = AirtableService(settings)

        from app.services.cache_service import encoded_length as real_encoded_length

        call_count = 0

        def _counting_spy(value, *, compress):
            nonlocal call_count
            call_count += 1
            return real_encoded_length(value, compress=compress)

        with mock.patch(
            "app.services.airtable_service.encoded_length", _counting_spy
        ), mock.patch.object(
            AirtableService, "_COMPRESSED_SIZE_CHECK_RAW_INTERVAL_BYTES", 15_000
        ):
            rows, _fields, oversized = await service._walk_full_table(
                api=make_fake_api_class(records)("fake-key"),
                table=_FakeAirtableTableHandle(),
                view_id=None,
                fetch_fields=None,
                formula=None,
            )
        self.assertFalse(oversized, "the real compressed size fits comfortably under 7500")
        self.assertEqual(len(rows), 1000)
        # ~9 pages run escalated once raw crosses 7500: every-page
        # re-checking would be ~9 calls. The interval must cut that down
        # meaningfully, not just by one — measured at exactly 4 for these
        # constants.
        self.assertLess(
            call_count,
            8,
            f"expected the interval to skip most pages, got {call_count} real checks",
        )
        self.assertGreaterEqual(call_count, 2, "must still check more than once")

    def test_row_cap_is_a_sanity_ceiling_not_the_deciding_guard(self) -> None:
        # §4.2: at 10,000 the row counter — which has no relationship to any
        # Upstash limit — rejected a 15,000-row table that sat inside every
        # real limit. The byte cap stays where it is; §2.1 shows it is a
        # real guard (Upstash hard-rejects over 10 MiB).
        from app.config import Settings

        self.assertEqual(Settings.model_fields["AIRTABLE_CACHE_MAX_ROWS"].default, 50_000)
        self.assertEqual(
            Settings.model_fields["AIRTABLE_CACHE_MAX_BYTES"].default, 5_000_000
        )

    def test_refresh_negative_ttl_setting_exists_and_is_longer_than_the_read_paths(
        self,
    ) -> None:
        # The whole point of the separate setting (2026-08-10 handoff
        # finding "C"): the cron ticks on a fixed ~5-minute cadence
        # regardless, so a negative marker with the read path's short TTL
        # would always have lapsed by the next tick and never actually stop
        # a persistently oversized/failing widget from being re-walked to
        # the cap on EVERY tick, forever.
        from app.config import Settings

        read_ttl = Settings.model_fields["AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS"].default
        refresh_ttl = Settings.model_fields[
            "AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS"
        ].default
        self.assertGreater(refresh_ttl, read_ttl)


class WidgetWalkRetryTests(unittest.IsolatedAsyncioTestCase):
    """Finding D (2026-08-10 handoff): pyairtable's own default retry
    (`Api(key)` with no `retry_strategy=`) already retries 429s AND
    connection-level errors up to 5x with backoff — confirmed by reading
    `pyairtable/api/retrying.py`, not assumed. The real gap is that its
    `status_forcelist` defaults to `(429,)` only, so a single transient
    Airtable 5xx aborts the whole walk with zero retry. These tests pin the
    fix: a widened, shared retry strategy, actually wired into both
    Api-construction sites the widget cache walk uses.
    """

    _patched = WidgetCacheServiceTests._patched

    def test_widget_retry_strategy_widens_status_forcelist_beyond_429(self) -> None:
        # Unit-tests the CONSTANT itself against the real pyairtable/urllib3
        # wiring (no fakes) — proves the understanding of the library's
        # default is right, and guards against a future pyairtable upgrade
        # silently changing what `retry_strategy=True` means.
        from pyairtable import Api

        from app.services.airtable_service import _WIDGET_RETRY_STRATEGY

        api = Api("fake-key", retry_strategy=_WIDGET_RETRY_STRATEGY)
        adapter = api.session.get_adapter("https://api.airtable.com")
        self.assertEqual(
            set(adapter.max_retries.status_forcelist),
            {429, 500, 502, 503, 504},
        )
        # Connection-level retries (timeout/reset) are NOT status-based —
        # confirm they're still covered (urllib3 defaults `connect`/`read`
        # to `total` when left unset, which pyairtable's factory does).
        self.assertGreater(adapter.max_retries.total, 0)
        self.assertTrue(adapter.max_retries.respect_retry_after_header)

    async def test_cache_walk_path_constructs_api_with_the_widened_strategy(self) -> None:
        from app.services.airtable_service import _WIDGET_RETRY_STRATEGY

        records = _make_records(5)
        constructed: list = []
        settings, fake_cache, _api_patch, cache_patch = self._patched(records)
        with mock.patch(
            "app.services.airtable_service.Api",
            make_fake_api_class(records, constructed=constructed),
        ), cache_patch:
            # AirtableService.__init__ builds its OWN unrelated `self._api`
            # for the fixed-schema accessors (`Api(settings.AIRTABLE_API_KEY)`,
            # no retry_strategy= — correctly out of scope for this fix), so
            # only what the WIDGET call itself constructs is captured below.
            service = AirtableService(settings)
            constructed.clear()
            await service.fetch_widget_rows_cached(
                link="retry-widget",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
                cursor=None,
            )
        self.assertTrue(constructed, "Api should have been constructed at least once")
        self.assertTrue(
            all(rs is _WIDGET_RETRY_STRATEGY for rs in constructed),
            "the cache-walk Api() construction must use the widened retry strategy",
        )

    async def test_live_fallback_path_constructs_api_with_the_widened_strategy(self) -> None:
        # `fetch_widget_rows` (the oversized/disabled-cache fallback) has
        # its OWN separate Api() construction — a request that degrades to
        # it deserves the same 5xx resilience, not just the cache-walk path.
        from app.services.airtable_service import _WIDGET_RETRY_STRATEGY

        records = _make_records(5)
        constructed: list = []
        settings, fake_cache, _api_patch, cache_patch = self._patched(
            records, cache_enabled=False
        )
        with mock.patch(
            "app.services.airtable_service.Api",
            make_fake_api_class(records, constructed=constructed),
        ), cache_patch:
            service = AirtableService(settings)  # builds its own unrelated Api — see above
            constructed.clear()
            await service.fetch_widget_rows_cached(
                link="retry-widget-live",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
                cursor=None,
            )
        self.assertTrue(constructed, "Api should have been constructed at least once")
        self.assertTrue(all(rs is _WIDGET_RETRY_STRATEGY for rs in constructed))


class ScheduledRefreshNegativeCacheTests(unittest.IsolatedAsyncioTestCase):
    """Finding C (2026-08-10 handoff): `warm_widget_cache` previously never
    consulted OR wrote the negative-cache marker at all — only the read
    path did. A persistently oversized (or failing) widget was re-walked to
    the cap on every single ~5-minute tick, forever, discarding the result
    every time. These tests prove the fix: the scheduled refresh now checks
    the marker before doing any work, and writes one (with its own longer
    TTL) when it confirms a widget oversized or failing itself.
    """

    _patched = WidgetCacheServiceTests._patched

    async def test_walk_failure_is_negative_cached_and_reported_as_walk_failed(
        self,
    ) -> None:
        from app.helpers.exceptions import AirtableError

        records = _make_records(5)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            flaky = mock.AsyncMock(side_effect=AirtableError("simulated Airtable 5xx"))
            with mock.patch.object(service, "_build_widget_cache_envelope", flaky):
                # Must NOT raise — previously this exception propagated
                # uncaught out of warm_widget_cache entirely.
                outcome = await service.warm_widget_cache(
                    widget_type=AIRTABLE_WIDGET_TYPE,
                    link="w-cron-flaky",
                    url=FAKE_URL,
                    api_key="pat",
                    selected_columns=None,
                    filters=None,
                    personalize_enabled=False,
                    personalize_column=None,
                )
        self.assertEqual(outcome, "walk_failed")
        negative_entries = {
            k: v for k, v in fake_cache.store.items() if k.endswith(":negative")
        }
        self.assertEqual(len(negative_entries), 1)
        neg_key, neg_value = next(iter(negative_entries.items()))
        self.assertEqual(neg_value["reason"], "walk_failed")
        self.assertEqual(
            fake_cache.ttl_by_key[neg_key],
            settings.AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS,
            "the scheduled refresh must write its OWN longer TTL, not the "
            "read path's short one",
        )

    async def test_oversized_widget_is_negative_cached_with_the_longer_refresh_ttl(
        self,
    ) -> None:
        records = _make_records(12)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, max_rows=5, call_log=call_log
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            kwargs = dict(
                widget_type=AIRTABLE_WIDGET_TYPE,
                link="w-cron-oversized",
                url=FAKE_URL,
                api_key="pat",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )

            first = await service.warm_widget_cache(**kwargs)
            self.assertEqual(first, "oversized")
            self.assertEqual(len(call_log), 1, "one aborted oversized walk")

            negative_entries = {
                k: v for k, v in fake_cache.store.items() if k.endswith(":negative")
            }
            self.assertEqual(len(negative_entries), 1)
            neg_key, neg_value = next(iter(negative_entries.items()))
            self.assertEqual(neg_value["reason"], "oversized")
            self.assertEqual(
                fake_cache.ttl_by_key[neg_key],
                settings.AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS,
            )

            # The very next tick must be a pure skip — no second walk, even
            # though AIRTABLE_CACHE_MIN_REFRESH_SECONDS's freshness check
            # (which only ever looks at the REAL envelope key, never
            # written for an oversized table) could not have caught this on
            # its own.
            second = await service.warm_widget_cache(**kwargs)
            self.assertEqual(second, "damped")
            self.assertEqual(
                len(call_log), 1, "a fresh negative marker must skip the walk entirely"
            )

    async def test_healthy_widget_is_unaffected(self) -> None:
        # Guard against a too-broad negative check accidentally damping
        # widgets that never failed.
        records = _make_records(5)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            outcome = await service.warm_widget_cache(
                widget_type=AIRTABLE_WIDGET_TYPE,
                link="w-cron-healthy",
                url=FAKE_URL,
                api_key="pat",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )
        self.assertEqual(outcome, "refreshed")
        self.assertFalse(
            any(k.endswith(":negative") for k in fake_cache.store),
            "a healthy widget must never get a negative marker",
        )


class WidgetBaseLockTests(unittest.IsolatedAsyncioTestCase):
    """Finding #6 (plan_airtable_cache_scaling_2026-08-08.md §3.4.2,
    2026-08-10 handoff §3): the per-fingerprint lock only stops the SAME
    widget from being walked twice at once. Two DIFFERENT widgets sharing a
    base — e.g. a reader's cold warm of widget A racing the cron's refresh
    of widget B — each hold their own fingerprint lock and would happily
    walk concurrently, together exceeding Airtable's 5 req/s/base limit.
    """

    _patched = WidgetCacheServiceTests._patched

    async def test_envelope_builder_reports_locked_and_never_calls_airtable(self) -> None:
        records = _make_records(5)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, call_log=call_log
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            base_id, _table_id, _view_id = service._parse_airtable_share_url(FAKE_URL)
            base_lock_key = f"airtable:{fake_cache.version}:widget_base_lock:{base_id}"
            # Simulate a DIFFERENT widget on the same base already walking.
            fake_cache._locks[base_lock_key] = "someone-elses-token"

            envelope, status = await service._build_widget_cache_envelope(
                cache=fake_cache,
                url=FAKE_URL,
                api_key="pat",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )
        self.assertIsNone(envelope)
        self.assertEqual(status, "locked")
        self.assertEqual(call_log, [], "a base-lock loser must never call Airtable")

    async def test_read_path_degrades_to_live_immediately_with_no_poll(self) -> None:
        # Design note from the handoff: unlike per-fingerprint contention
        # (which polls, since the OTHER holder IS building this exact
        # entry), base-lock contention must return immediately — a
        # different widget's walk will never populate THIS cache key.
        records = _make_records(5)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            base_id, _table_id, _view_id = service._parse_airtable_share_url(FAKE_URL)
            base_lock_key = f"airtable:{fake_cache.version}:widget_base_lock:{base_id}"
            fake_cache._locks[base_lock_key] = "someone-elses-token"

            sleep_spy = mock.AsyncMock()
            with mock.patch("app.services.airtable_service.asyncio.sleep", sleep_spy):
                resp = await service.fetch_widget_rows_cached(
                    link="w-base-locked-reader",
                    url=FAKE_URL,
                    api_key="pat",
                    caller_email="anyone@example.com",
                    selected_columns=None,
                    filters=None,
                    personalize_enabled=False,
                    personalize_column=None,
                    cursor=None,
                )
        self.assertEqual(len(resp.rows), 5, "must still degrade to live")
        sleep_spy.assert_not_awaited()
        self.assertFalse(
            any(k.endswith(":negative") for k in fake_cache.store),
            "lock contention is not a confirmed bad table — no negative marker",
        )

    async def test_scheduled_refresh_reports_locked_not_failed(self) -> None:
        records = _make_records(5)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, call_log=call_log
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            base_id, _table_id, _view_id = service._parse_airtable_share_url(FAKE_URL)
            base_lock_key = f"airtable:{fake_cache.version}:widget_base_lock:{base_id}"
            fake_cache._locks[base_lock_key] = "someone-elses-token"

            outcome = await service.warm_widget_cache(
                widget_type=AIRTABLE_WIDGET_TYPE,
                link="w-base-locked-cron",
                url=FAKE_URL,
                api_key="pat",
                selected_columns=None,
                filters=None,
                personalize_enabled=False,
                personalize_column=None,
            )
        self.assertEqual(outcome, "locked")
        self.assertEqual(call_log, [])
        self.assertFalse(any(k.endswith(":negative") for k in fake_cache.store))


class _AirtableCacheFixture(unittest.TestCase):
    """Shared router-level fixture: an in-memory SQLite v2 schema, a
    TestClient, JWT minting and a widget seeder.

    Holds NO test methods — subclasses supply those. Split out so a second
    test class can reuse the fixture without also re-running the first
    class's tests.
    """

    def setUp(self) -> None:
        _treat_example_com_as_org_domain(self)
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        self.hub = HubV2(
            document_id="hub-doc",
            access_control={
                "admins": {
                    "users": [],
                    "roles": [{"name": "Hub Admin", "scope": "", "program": "", "function": ""}],
                },
                "viewers": {"users": [], "roles": []},
            },
        )
        self.db.add(self.hub)
        self.db.commit()

        app = create_app()
        app.dependency_overrides[get_db_v2] = lambda: (yield self._session_factory())
        self.client = TestClient(app, raise_server_exceptions=False)

        nav = create_nav_tab_v2(self.db, title="Nav Cache")
        nav_row = get_nav_tab_by_document_id(self.db, nav["documentId"])
        self.root = create_tab_v2(self.db, title="Root Cache", nav_tab_id=nav_row.id)
        root_row = self.db.query(TabV2).filter(TabV2.document_id == self.root["documentId"]).first()
        self.root_gridstack = (
            self.db.query(GridstackV2)
            .filter(GridstackV2.parent_tab_id == root_row.id, GridstackV2.parent_id.is_(None))
            .first()
        )

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _token(self, email: str, roles: list[str] | None = None) -> str:
        expire = datetime.now(timezone.utc) + timedelta(minutes=30)
        return jwt.encode(
            {"sub": email, "name": "Test User", "roles": roles or [], "exp": expire},
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        """Phase 3: the JWT "Hub Admin" claim alone admits nobody. Every
        "Hub Admin" token built on this fixture (`_member_auth`, the
        lock-then-save helpers, and `test_airtable_widget_drilldown`) is the
        admin BYPASS, never a test about a JWT-only admin, so asking for
        that role also seeds the real `(hub_admin, universal)` assignment
        (`tests/_real_admin.py`, own session)."""
        if roles and "Hub Admin" in roles:
            seed_real_admin(self._session_factory, email)
        return {"Authorization": f"Bearer {self._token(email, roles)}"}

    def _member_auth(self, email: str) -> dict:
        """plan_ac_enforcement_closeout_2026-09-09.md §5 re-gated the six
        component-data routes below onto the read-time visibility fold
        (`granted_single_node`/`ViewerAccess.is_granted`) instead of the
        component's own legacy `access_control` blob. Most of this file's
        cache/warm/query-count tests are not ABOUT that gate — they seed
        widgets with no `resource_grants` at all, the same way
        `test_threads.py`'s `_member_auth` (step 2 of this arc) re-pointed
        its own file's pre-existing functional tests at a Hub Admin bypass
        rather than teaching every one of them to hold a real grant. A Hub
        Admin token trips `ViewerAccess.full_access`, which bypasses the
        fold entirely, so these tests keep exercising what they were
        written to exercise. Tests that are actually ABOUT the gate build a
        real `ResourceGrantV2` row via `_grant` and call `_auth` directly —
        see `AirtableAccessControlTests` below."""
        return self._auth(email, roles=["Hub Admin"])

    # -- resource grants (plan_access_control_algorithm_2026-08-27.md §7,
    # D5 — a direct per-user grant, no role/scope/assignment needed) -------

    def _seed_hub_user(self, email: str) -> UserV2:
        """`get_current_hub_user` auto-provisions a `hub_users` row on first
        request, keyed by normalized email — pre-seeded here with the same
        normalization so a grant written against this row is the SAME row
        the caller's own request resolves to. Same helper as
        `test_threads.py`'s (step 2 of this arc)."""
        normalized = email.strip().lower()
        existing = self.db.query(UserV2).filter(UserV2.email == normalized).first()
        if existing is not None:
            return existing
        row = UserV2(email=normalized, has_signed_in=True, created_at=datetime.now(timezone.utc))
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _grant(self, *, email: str, node: tuple[str, int], level: str = "view") -> None:
        """A direct per-user grant (D5) on `node = (node_kind, node_id)` —
        same shape `test_threads.py`'s own `_grant` and
        `test_sbn_access_control.py`'s `grant()` use."""
        user_row = self._seed_hub_user(email)
        node_kind, node_id = node
        self.db.add(
            ResourceGrantV2(
                **{NODE_COLUMNS[node_kind]: node_id},
                user_id=user_row.id,
                level=level,
                created_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

    def _seed_widget(
        self,
        link: str,
        *,
        access_control: dict | None = None,
        widget_type: str = AIRTABLE_WIDGET_TYPE,
        extra_data: dict | None = None,
    ) -> ComponentV2:
        component = ComponentV2(
            link=link,
            title=link,
            type=widget_type,
            gridstack_id=self.root_gridstack.id,
            access_control=access_control,
        )
        self.db.add(component)
        self.db.commit()
        self.db.refresh(component)
        _write_component_data(
            self.db,
            component,
            {
                "sourceUrl": FAKE_URL,
                "pat": "fake-pat",
                "selectedColumns": [],
                "filters": [],
                "personalizeEnabled": False,
                "personalizeColumn": None,
                **(extra_data or {}),
            },
        )
        self.db.commit()
        self.db.refresh(component)
        return component


class AirtableCacheHttpTests(_AirtableCacheFixture):
    """Router-level tests: the endpoint's access-control check, the §4.7
    bundle, and the refresh endpoint's auth + summary shape."""

    def test_authorization_is_not_bypassed_by_a_warm_cache(self) -> None:
        # The regression test for the entire reason @airtable_cache was
        # rejected for this endpoint (plan §3.1) — updated for
        # plan_ac_enforcement_closeout_2026-09-09.md §5: the legacy
        # `access_control` blob is no longer read here at all, so "granted"
        # now means a real `ResourceGrantV2` row, and a refusal is 404, not
        # 403 (§5.4 — a deep link to a hidden node must not confirm the node
        # exists). The property under test is unchanged: warming the cache
        # as an authorized viewer must not let a DIFFERENT, unauthorized
        # viewer read it.
        widget = self._seed_widget("cache-auth-widget")
        self._grant(email="allowed@example.com", node=("component", widget.id))
        records = _make_records(5)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            first = self.client.get(
                "/data/airtable/component/cache-auth-widget/rows",
                headers=self._auth("allowed@example.com"),
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(fake_cache.store, "first request should have warmed the cache")

            second = self.client.get(
                "/data/airtable/component/cache-auth-widget/rows",
                headers=self._auth("outsider@example.com", roles=["Hub Member"]),
            )
            self.assertEqual(
                second.status_code,
                404,
                "an unauthorized viewer must be rejected even though the cache is warm",
            )

    def test_refresh_endpoint_requires_configured_token(self) -> None:
        with mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN=None),
        ):
            resp = self.client.post(
                "/data/airtable/cache/refresh", headers={"X-Sync-Token": "anything"}
            )
        self.assertEqual(resp.status_code, 503)

    def test_refresh_endpoint_rejects_wrong_token(self) -> None:
        with mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            resp = self.client.post(
                "/data/airtable/cache/refresh", headers={"X-Sync-Token": "wrong-token"}
            )
        self.assertEqual(resp.status_code, 401)

        with mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            resp = self.client.post("/data/airtable/cache/refresh")  # header omitted entirely
        self.assertEqual(resp.status_code, 401)

    def test_refresh_endpoint_warms_widgets_and_reports_a_summary(self) -> None:
        self._seed_widget("cache-refresh-widget-1")
        # A second widget with no source URL configured — must be counted
        # as skipped, not failed, and must not abort the run.
        unconfigured = ComponentV2(
            link="cache-refresh-widget-2",
            title="cache-refresh-widget-2",
            type=AIRTABLE_WIDGET_TYPE,
            gridstack_id=self.root_gridstack.id,
        )
        self.db.add(unconfigured)
        self.db.commit()

        records = _make_records(5)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            resp = self.client.post(
                "/data/airtable/cache/refresh", headers={"X-Sync-Token": "correct-token"}
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["refreshed"], 1)
        self.assertEqual(body["skipped"], 1)
        self.assertEqual(body["failed"], 0)
        self.assertIn("elapsed_seconds", body)
        self.assertTrue(fake_cache.store)

    # ── §4.7: the component bundle ─────────────────────────────────────
    def _count_row_reads(self) -> tuple[dict, Any]:
        """Install a cursor-level listener that tallies SELECTs per table.
        Returns `(counts, detach)`; call `detach()` when finished."""
        from sqlalchemy import event

        counts: dict[str, int] = {"components": 0, "page_contents": 0}

        def _on_execute(_conn, _cursor, statement, _params, _ctx, _many):
            head = statement.lstrip()[:6].upper()
            if head != "SELECT":
                return
            for table in counts:
                # The FROM clause quotes table names on SQLite; match on the
                # bare name, which cannot collide between these two.
                if f" {table}" in statement or f'"{table}"' in statement:
                    counts[table] += 1

        event.listen(self.engine, "before_cursor_execute", _on_execute)
        return counts, lambda: event.remove(
            self.engine, "before_cursor_execute", _on_execute
        )

    def test_rows_endpoint_issues_two_queries_not_six(self) -> None:
        # Before §4.7 this endpoint called get_airtable_component_config,
        # get_airtable_pat_for_component and get_airtable_component_data
        # independently. Each ran `_airtable_component_by_link` (a
        # ComponentV2 query) AND `_raw_component_data` (a PageContentV2
        # fetch) — SIX queries to read the SAME TWO ROWS, on every request,
        # cache hit and miss alike. Asserted via a real cursor listener so
        # the fix cannot silently regress.
        #
        # `_member_auth`, not `_auth`: plan_ac_enforcement_closeout_2026-09-
        # 09.md §5 added an access-control check ahead of the cache, but
        # `_resolve_airtable_bundle_and_access` checks `is_hub_admin`'s
        # JWT branch BEFORE building an `RbacClosures` snapshot (see that
        # function's own comment), so a Hub Admin caller — this test's only
        # concern is the bundle's own query count, not the gate — adds
        # ZERO queries beyond the two this test already pins.
        self._seed_widget("cache-querycount-widget")
        records = _make_records(3)
        fake_cache = FakeCacheService()

        counts, detach = self._count_row_reads()
        try:
            with mock.patch(
                "app.services.airtable_service.Api", make_fake_api_class(records)
            ), mock.patch(
                "app.services.airtable_service.get_cache_service",
                return_value=fake_cache,
            ):
                resp = self.client.get(
                    "/data/airtable/component/cache-querycount-widget/rows",
                    headers=self._member_auth("someone@example.com"),
                )
        finally:
            detach()

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            counts,
            {"components": 1, "page_contents": 1},
            f"expected one read of each row, got {counts} (was 3 each before §4.7)",
        )

    def test_bundle_never_exposes_the_raw_blob_or_leaks_the_pat(self) -> None:
        # Landmine L12: `_raw_component_data` returns the PAT in clear, and a
        # bundle carrying BOTH the raw and sanitised views is exactly the
        # shape where a later caller returns the wrong one.
        from app.services.gridstack_service import (
            AirtableComponentBundle,
            get_airtable_component_bundle,
            update_airtable_component_config,
        )

        self._seed_widget("cache-bundle-widget")
        bundle = get_airtable_component_bundle(self.db, "cache-bundle-widget")
        self.assertIsNotNone(bundle)

        # The type exposes exactly five fields — no raw blob, under any name.
        # `widget_type` is `ComponentV2.type`, not a view of the raw data
        # blob, so it doesn't weaken this guarantee (plan_warm_key_
        # divergence_2026-08-14.md §3.3) — asserted directly below.
        # `component_id` (plan_ac_enforcement_closeout_2026-09-09.md §5.4)
        # is likewise the component ROW's own `.id`, not a raw-blob field —
        # added so the six gated routes can ask `access.is_granted(...)`
        # without a second `_airtable_component_by_link` lookup.
        self.assertEqual(
            set(AirtableComponentBundle.__dataclass_fields__),
            {"config", "pat", "data", "widget_type", "component_id"},
        )
        self.assertEqual(bundle.widget_type, AIRTABLE_WIDGET_TYPE)
        self.assertIsInstance(bundle.component_id, int)

        # `.data` is client-safe: reports the PAT's existence, never its value.
        self.assertNotIn("pat", bundle.data)
        self.assertTrue(bundle.data["hasPat"])
        self.assertEqual(bundle.config["hasPat"], True)
        self.assertNotIn("pat", bundle.config)

        # `.pat` is the server-internal view, and it is the real token.
        self.assertEqual(bundle.pat, "fake-pat")

        # Each view matches the single-view accessor it derives from.
        from app.services.gridstack_service import (
            get_airtable_component_config,
            get_airtable_component_data,
            get_airtable_pat_for_component,
        )

        self.assertEqual(
            bundle.config, get_airtable_component_config(self.db, "cache-bundle-widget")
        )
        self.assertEqual(
            bundle.data, get_airtable_component_data(self.db, "cache-bundle-widget")
        )
        self.assertEqual(
            bundle.pat, get_airtable_pat_for_component(self.db, "cache-bundle-widget")
        )

        # The failure mode L12 warns about: writing back a view that had the
        # PAT stripped silently DELETES the stored token. Updating an
        # unrelated field must not do that.
        update_airtable_component_config(
            self.db, "cache-bundle-widget", personalize_enabled=True
        )
        after = get_airtable_component_bundle(self.db, "cache-bundle-widget")
        self.assertEqual(after.pat, "fake-pat", "config round-trip cleared the PAT")
        self.assertTrue(after.config["personalizeEnabled"])
        self.assertNotIn("pat", after.data)

    def test_config_save_warms_the_cache_in_the_background(self) -> None:
        # Warm-on-save: an admin saving a widget's config should trigger the
        # cache warm right then, so the first VIEWER after the save doesn't
        # have to pay for the cold, throttled full-table walk themselves.
        self._seed_widget("warm-on-save-widget")
        records = _make_records(5)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            # `_warm_after_config_save` deliberately opens its OWN db
            # session rather than reusing the request's — this is what
            # lets it be re-pointed at the test's in-memory SQLite engine
            # (same StaticPool the fixture's own `self.db` uses).
            "app.routers.airtable.SessionLocalV2",
            self._session_factory,
        ):
            # Hub Admin + a live session — plan_lock_propagation_2026-09-08.md
            # §5.3 gave this endpoint its first-ever require_edit +
            # require_live_session gate; see `_save_config`'s own comment
            # in WarmKeyAlignmentTests below for the full rationale.
            auth = self._auth("someone@example.com", roles=["Hub Admin"])
            lock_resp = self.client.put(f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=auth)
            token = lock_resp.json()["data"]["lock_token"]
            resp = self.client.put(
                "/data/airtable/component/warm-on-save-widget/config",
                json={"personalizeEnabled": False},
                headers={**auth, "X-Edit-Tokens": token},
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            # TestClient runs background tasks to completion before the
            # call above returns, so the warm has already happened here —
            # no polling or sleeping needed.
            warmed = [
                v for v in fake_cache.store.values() if isinstance(v, dict) and "rows" in v
            ]
            self.assertEqual(len(warmed), 1, fake_cache.store)
            self.assertEqual(len(warmed[0]["rows"]), 5)

            # Not just "an entry with rows exists" — the warmed entry must
            # sit at the EXACT key a reader's GET /rows would look up.
            # Absent this, a fingerprint drift between warm_widget_cache and
            # fetch_widget_rows_cached (or fetch_widget_metric_cached /
            # fetch_widget_chart_cached, for those widget types — see
            # WarmKeyAlignmentTests) has no failing assertion anywhere in
            # this file (plan_warm_key_divergence_2026-08-14.md §5).
            from app.services.airtable_service import AirtableService
            from app.services.cache_service import build_key

            expected_key = build_key(
                "widget_rows",
                AirtableService._widget_cache_fingerprint(
                    widget_type=AIRTABLE_WIDGET_TYPE,
                    link="warm-on-save-widget",
                    url=FAKE_URL,
                    selected_columns=[],
                    filters=[],
                    personalize_enabled=False,
                    personalize_column=None,
                ),
            )
            self.assertIn(expected_key, fake_cache.store, fake_cache.store)

    def test_config_save_does_not_warm_when_nothing_is_fetchable_yet(self) -> None:
        # A brand-new widget saved without a sourceUrl/pat yet has nothing
        # to warm — must not attempt (and fail) a walk against an empty URL.
        component = ComponentV2(
            link="warm-on-save-empty",
            title="warm-on-save-empty",
            type=AIRTABLE_WIDGET_TYPE,
            gridstack_id=self.root_gridstack.id,
        )
        self.db.add(component)
        self.db.commit()
        self.db.refresh(component)
        _write_component_data(self.db, component, {})
        self.db.commit()

        call_log: list = []
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class([], call_log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.SessionLocalV2", self._session_factory
        ):
            # Hub Admin + a live session — see the previous test's own
            # comment on why (plan_lock_propagation_2026-09-08.md §5.3).
            auth = self._auth("someone@example.com", roles=["Hub Admin"])
            lock_resp = self.client.put(f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=auth)
            token = lock_resp.json()["data"]["lock_token"]
            resp = self.client.put(
                "/data/airtable/component/warm-on-save-empty/config",
                json={"personalizeEnabled": False},
                headers={**auth, "X-Edit-Tokens": token},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(call_log, [], "must not attempt a walk with no sourceUrl/pat")
            self.assertEqual(fake_cache.store, {})

    def test_unknown_link_still_404s_through_the_bundle(self) -> None:
        resp = self.client.get(
            "/data/airtable/component/no-such-widget/rows",
            headers=self._auth("someone@example.com"),
        )
        self.assertEqual(resp.status_code, 404)

    # ── §4.4 / §4.6: refresh outcomes ──────────────────────────────────
    def _refresh(self, fake_cache, records):
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            return self.client.post(
                "/data/airtable/cache/refresh",
                headers={"X-Sync-Token": "correct-token"},
            )

    def test_refresh_skips_a_fresh_entry_instead_of_rewalking(self) -> None:
        # §4.4: today every widget is re-walked every tick regardless of
        # change. A second run moments later must cost a GET, not a walk.
        self._seed_widget("cache-fresh-widget")
        records = _make_records(5)
        fake_cache = FakeCacheService()

        first = self._refresh(fake_cache, records)
        self.assertEqual(first.json()["refreshed"], 1, first.text)

        second = self._refresh(fake_cache, records)
        body = second.json()
        self.assertEqual(body["refreshed"], 0, body)
        self.assertEqual(body["skipped"], 1, body)
        self.assertEqual(body["failed"], 0, body)

    def test_refresh_skips_when_the_lock_is_held_and_does_not_fail(self) -> None:
        # §4.4 + landmine L9: a lock-skip is an expected outcome. Counting it
        # as `failed` would make the cron alarm on correct behavior.
        self._seed_widget("cache-locked-widget")
        records = _make_records(5)
        fake_cache = FakeCacheService()

        # Pre-take every lock this run could ask for.
        original_acquire = fake_cache.acquire_lock

        async def _always_taken(key: str, *, ttl_seconds: int = 120) -> str | None:
            return None

        fake_cache.acquire_lock = _always_taken
        try:
            resp = self._refresh(fake_cache, records)
        finally:
            fake_cache.acquire_lock = original_acquire

        body = resp.json()
        self.assertEqual(body["refreshed"], 0, body)
        self.assertEqual(body["skipped"], 1, body)
        self.assertEqual(body["failed"], 0, body)
        self.assertEqual(fake_cache.store, {}, "a locked run must not write")

    def test_refresh_counts_oversized_separately_from_failed(self) -> None:
        # §4.6: an oversized table is expected behavior (served live), not a
        # failure — and the cron reads non-zero `failed` as a warning.
        self._seed_widget("cache-oversized-widget")
        records = _make_records(12)
        fake_cache = FakeCacheService()

        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ), mock.patch.object(
            AirtableService, "_build_widget_cache_envelope", return_value=(None, "oversized")
        ):
            resp = self.client.post(
                "/data/airtable/cache/refresh",
                headers={"X-Sync-Token": "correct-token"},
            )

        body = resp.json()
        self.assertEqual(body["oversized"], 1, body)
        self.assertEqual(body["failed"], 0, body)
        self.assertEqual(body["refreshed"], 0, body)
        # No entry holds `rows` — the real envelope is never cached. A
        # negative marker IS now expected here (unlike before this
        # session's fix — see the 2026-08-10 handoff finding "C": the
        # scheduled refresh previously never wrote one, so an oversized
        # widget was re-walked to the cap on every single tick, forever),
        # so the store is not empty.
        self.assertFalse(
            any("rows" in v for v in fake_cache.store.values() if isinstance(v, dict)),
            "oversized must never be cached as real data",
        )


class PerWidgetRefreshTests(_AirtableCacheFixture):
    """Per-widget refresh targeting + the discovery endpoint
    (scope_per_widget_refresh_2026-08-08.md §5). Reuses the shared fixture
    only — none of `AirtableCacheHttpTests`' cases re-run here.
    """

    def _post(self, fake_cache, records, *, link=None):
        url = "/data/airtable/cache/refresh"
        if link is not None:
            url += f"?link={link}"
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            return self.client.post(url, headers={"X-Sync-Token": "correct-token"})

    # ── targeting ──────────────────────────────────────────────────────
    def test_link_param_refreshes_only_that_widget(self) -> None:
        self._seed_widget("per-widget-a")
        self._seed_widget("per-widget-b")
        fake_cache = FakeCacheService()

        resp = self._post(fake_cache, _make_records(5), link="per-widget-a")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["refreshed"], 1, body)
        self.assertEqual(len(fake_cache.store), 1, "exactly one widget cached")

        # And it is A's entry, not B's — compare against the key the read
        # path would build for each.
        from app.services.cache_service import build_key

        def key_for(link):
            return build_key(
                "widget_rows",
                {
                    "link": link,
                    "sourceUrl": FAKE_URL,
                    "selectedColumns": [],
                    "filters": [],
                    "personalizeEnabled": False,
                    "personalizeColumn": None,
                },
                version="v1",
            )

        self.assertIn(key_for("per-widget-a"), fake_cache.store)
        self.assertNotIn(key_for("per-widget-b"), fake_cache.store)

    def test_sweep_still_refreshes_everything_when_link_is_omitted(self) -> None:
        # The regression that matters most: the deployed cron uses the
        # no-param form and must keep behaving exactly as before.
        self._seed_widget("sweep-a")
        self._seed_widget("sweep-b")
        fake_cache = FakeCacheService()

        body = self._post(fake_cache, _make_records(5)).json()
        self.assertEqual(body["refreshed"], 2, body)
        self.assertEqual(body["skipped"], 0, body)
        self.assertEqual(body["failed"], 0, body)
        self.assertEqual(len(fake_cache.store), 2)

    def test_summary_shape_is_identical_for_both_forms(self) -> None:
        self._seed_widget("shape-a")
        sweep = self._post(FakeCacheService(), _make_records(5)).json()
        single = self._post(
            FakeCacheService(), _make_records(5), link="shape-a"
        ).json()
        self.assertEqual(
            sorted(sweep), sorted(single), "cron sums these dicts — keys must match"
        )

    def test_unknown_link_is_skipped_not_an_error(self) -> None:
        # A widget deleted between discovery and refresh must not fail the
        # run (§3.1a). 200 + skipped, never 404, never failed.
        self._seed_widget("present-widget")
        resp = self._post(FakeCacheService(), _make_records(5), link="no-such-widget")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["skipped"], 1, body)
        self.assertEqual(body["failed"], 0, body)
        self.assertEqual(body["refreshed"], 0, body)

    def test_per_widget_call_still_requires_the_token(self) -> None:
        self._seed_widget("auth-widget")
        with mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            resp = self.client.post(
                "/data/airtable/cache/refresh?link=auth-widget",
                headers={"X-Sync-Token": "wrong"},
            )
        self.assertEqual(resp.status_code, 401)

    def test_locked_widget_is_skipped_by_a_concurrent_per_widget_call(self) -> None:
        # Overlap handling (§3.3): sequential per-widget calls take longer in
        # wall-clock, so runs can overlap a tick. The Tier 1 single-flight
        # lock must make that cheap rather than a double walk.
        self._seed_widget("locked-widget")
        fake_cache = FakeCacheService()

        async def _always_taken(key, *, ttl_seconds=120):
            return None

        fake_cache.acquire_lock = _always_taken
        body = self._post(fake_cache, _make_records(5), link="locked-widget").json()
        self.assertEqual(body["skipped"], 1, body)
        self.assertEqual(body["failed"], 0, body)
        self.assertEqual(fake_cache.store, {})

    def test_two_widgets_on_the_same_base_do_not_walk_concurrently(self) -> None:
        # Finding #6: unlike per-fingerprint contention (one widget refreshed
        # twice at once), THIS is two DIFFERENT widgets sharing a base — the
        # per-fingerprint lock alone would let both walk at the same time,
        # together exceeding Airtable's 5 req/s/base limit. Both widgets
        # come from `_seed_widget`, which always uses the same FAKE_URL, so
        # they share a base_id for free.
        self._seed_widget("base-lock-a")
        self._seed_widget("base-lock-b")
        fake_cache = FakeCacheService()

        base_id, _table_id, _view_id = AirtableService._parse_airtable_share_url(FAKE_URL)
        base_lock_key = f"airtable:{fake_cache.version}:widget_base_lock:{base_id}"
        # Simulate widget A already walking (e.g. a reader's cold warm)
        # while the cron refreshes widget B.
        fake_cache._locks[base_lock_key] = "widget-a-is-walking"

        call_log: list = []
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(_make_records(5), call_log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="correct-token"),
        ):
            resp = self.client.post(
                "/data/airtable/cache/refresh?link=base-lock-b",
                headers={"X-Sync-Token": "correct-token"},
            )

        body = resp.json()
        self.assertEqual(body["skipped"], 1, body)
        self.assertEqual(body["failed"], 0, body)
        self.assertEqual(call_log, [], "B must not call Airtable while A holds the base lock")
        self.assertFalse(
            any(k.endswith(":negative") for k in fake_cache.store),
            "lock contention is not a confirmed bad table",
        )

    # ── outcome mapping ────────────────────────────────────────────────
    def test_every_warm_outcome_maps_to_a_real_counter(self) -> None:
        # The mapping is the one place the sweep and the per-widget path
        # could drift, so pin it against what warm_widget_cache can return.
        from app.routers.airtable import (
            _REFRESH_COUNTERS,
            _REFRESH_OUTCOME_COUNTER,
        )

        self.assertEqual(
            set(_REFRESH_OUTCOME_COUNTER),
            {
                "refreshed",
                "oversized",
                "walk_failed",
                "damped",
                "fresh",
                "locked",
                "disabled",
            },
            "warm_widget_cache's documented outcomes and the mapping disagree",
        )
        for outcome, counter in _REFRESH_OUTCOME_COUNTER.items():
            self.assertIn(counter, _REFRESH_COUNTERS, outcome)

    def test_unrecognised_outcome_counts_as_failed(self) -> None:
        self._seed_widget("weird-widget")
        with mock.patch.object(
            AirtableService, "warm_widget_cache", return_value="something-new"
        ):
            body = self._post(
                FakeCacheService(), _make_records(5), link="weird-widget"
            ).json()
        self.assertEqual(body["failed"], 1, body)

    # ── discovery endpoint ─────────────────────────────────────────────
    def _get_widgets(self, token="correct-token", configured="correct-token"):
        with mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN=configured),
        ):
            headers = {"X-Sync-Token": token} if token is not None else {}
            return self.client.get("/data/airtable/cache/widgets", headers=headers)

    def test_widgets_endpoint_lists_every_airtable_widget(self) -> None:
        self._seed_widget("listing-a")
        self._seed_widget("listing-b")
        resp = self._get_widgets()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(sorted(body["links"]), ["listing-a", "listing-b"])
        self.assertEqual(body["count"], 2)

    def test_widgets_endpoint_rejects_a_bad_or_missing_token(self) -> None:
        self.assertEqual(self._get_widgets(token="wrong").status_code, 401)
        self.assertEqual(self._get_widgets(token=None).status_code, 401)

    def test_widgets_endpoint_503s_when_unconfigured(self) -> None:
        self.assertEqual(self._get_widgets(configured=None).status_code, 503)

    def test_discovery_then_per_widget_covers_the_same_set_as_a_sweep(self) -> None:
        # The end-to-end property the cron relies on: discovering links and
        # refreshing each one individually must cache exactly what a single
        # sweep would have cached.
        self._seed_widget("parity-a")
        self._seed_widget("parity-b")

        swept = FakeCacheService()
        self._post(swept, _make_records(5))

        per_widget = FakeCacheService()
        for link in self._get_widgets().json()["links"]:
            self._post(per_widget, _make_records(5), link=link)

        self.assertEqual(sorted(swept.store), sorted(per_widget.store))
        self.assertTrue(swept.store, "fixture should have cached something")


class CacheServiceEncodingTests(unittest.IsolatedAsyncioTestCase):
    """`CacheService`'s own encode/decode and fail-open behavior (§4.3, §4.5.1).

    These bypass `AirtableService` entirely — `FakeCacheService` deliberately
    stores live objects, so the real encoding is only exercised here.
    """

    def test_compressed_payload_is_marked_and_round_trips(self) -> None:
        from app.services.cache_service import _GZIP_MARKER, _decode, _encode

        value = {"rows": [{"id": f"rec{i}", "Name": f"Row {i}"} for i in range(500)]}
        encoded = _encode(value, compress=True)

        self.assertTrue(encoded.startswith(_GZIP_MARKER))
        self.assertEqual(_decode(encoded), value)
        self.assertLess(
            len(encoded),
            len(_encode(value, compress=False)),
            "compression should actually shrink a realistic row set",
        )

    def test_uncompressed_encoding_is_unchanged_plain_json(self) -> None:
        # The 123 `@airtable_cache` endpoints share this service and never
        # opt in — their stored format must be byte-identical to before.
        import json

        from app.services.cache_service import _encode

        value = {"b": 2, "a": [1, None, True]}
        self.assertEqual(
            _encode(value, compress=False),
            json.dumps(value, separators=(",", ":"), default=str),
        )

    def test_legacy_unmarked_entries_still_decode(self) -> None:
        # Landmine L2: entries written before compression existed carry no
        # marker and MUST keep decoding. No JSON payload can begin with the
        # marker (JSON always starts with {, [, ", -, a digit, or t/f/n), so
        # "starts with the marker" is a sound discriminator, not a guess.
        import json

        from app.services.cache_service import _decode

        for legacy in [
            {"endpoint": "x", "response": {"a": 1}},
            [1, 2, 3],
            "gzip: a string that merely looks like the marker",
            "gz:not-really-compressed",
            42,
            -1.5,
            None,
            True,
            {},
        ]:
            with self.subTest(legacy=legacy):
                self.assertEqual(
                    _decode(json.dumps(legacy, separators=(",", ":"))), legacy
                )

    async def test_get_degrades_to_none_when_upstash_raises(self) -> None:
        # §4.5.1: only `json.loads` was guarded, so an Upstash outage 500'd
        # every Airtable widget instead of degrading to live.
        from app.services.cache_service import CacheService

        service = CacheService(
            SimpleNamespace(
                UPSTASH_REDIS_REST_URL="https://fake",
                UPSTASH_REDIS_REST_TOKEN="fake",
                CACHE_VERSION="v1",
            )
        )

        class _BoomClient:
            async def get(self, key):
                raise RuntimeError("upstash is down")

            async def set(self, key, payload, **kwargs):
                raise RuntimeError("upstash is down")

        with mock.patch.object(service, "_redis", return_value=_BoomClient()):
            self.assertIsNone(await service.get("any-key"))
            # A failed write is a miss next time, not an exception now.
            await service.set("any-key", {"a": 1})


class WidgetMetricServiceTests(unittest.IsolatedAsyncioTestCase):
    """Direct AirtableService.fetch_widget_metric_cached tests — no HTTP
    layer, no DB. Covers the Airtable Metric widget (2026-08-11): Count/Sum
    aggregation computed over the SAME `widget_rows` cache envelope the
    Table widget uses (no new caching mechanism), the fail-closed
    personalize gate applied per-viewer, and the deliberate refusal to fall
    back to a live partial fetch for an oversized table.
    """

    _patched = WidgetCacheServiceTests._patched

    async def test_count_matches_the_filtered_row_count(self) -> None:
        records = _make_records(37)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                aggregation="count",
            )
            self.assertTrue(resp.available)
            self.assertEqual(resp.value, 37)
            self.assertFalse(resp.personalize_blocked)
            self.assertTrue(fake_cache.store, "the row envelope must be cached, same as /rows")

    async def test_sum_totals_the_field_skipping_non_numeric_values(self) -> None:
        records = [
            {"id": "r1", "fields": {"Amount": 100}},
            {"id": "r2", "fields": {"Amount": "2,500.50"}},
            {"id": "r3", "fields": {"Amount": None}},
            {"id": "r4", "fields": {"Amount": "not a number"}},
            {"id": "r5", "fields": {}},  # field missing entirely
        ]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m2",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                aggregation="sum",
                sum_field="Amount",
            )
            self.assertTrue(resp.available)
            self.assertEqual(resp.value, 2600.50)

    async def test_second_call_does_not_rewalk_airtable(self) -> None:
        records = _make_records(9)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            await service.fetch_widget_metric_cached(
                link="m3", url=FAKE_URL, api_key="pat",
                caller_email="anyone@example.com", aggregation="count",
            )
            calls_after_first = len(call_log)
            self.assertGreater(calls_after_first, 0)

            resp2 = await service.fetch_widget_metric_cached(
                link="m3", url=FAKE_URL, api_key="pat",
                caller_email="anyone@example.com", aggregation="count",
            )
            self.assertEqual(resp2.value, 9)
            self.assertEqual(len(call_log), calls_after_first, "a cache hit must not re-walk Airtable")

    async def test_personalize_fail_closed_never_computes_a_value(self) -> None:
        records = _make_records(6)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m4",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                aggregation="count",
                personalize_enabled=True,
                personalize_column=None,  # not configured -> gate refuses
            )
            self.assertIsNone(resp.value)
            self.assertTrue(resp.personalize_blocked)
            self.assertTrue(resp.available)
            self.assertEqual(call_log, [], "a blocked request must never call Airtable")
            self.assertEqual(fake_cache.store, {}, "a blocked request must never touch the cache")

    async def test_personalize_applied_computes_a_per_viewer_value(self) -> None:
        # _make_records cycles OwnerEmail across user0/user1/user2@example.com.
        records = _make_records(9)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m5",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                aggregation="count",
                personalize_enabled=True,
                personalize_column="OwnerEmail",
            )
            self.assertTrue(resp.available)
            self.assertEqual(resp.value, 3, "only user0's own 3 of 9 rows should count")

            other = await service.fetch_widget_metric_cached(
                link="m5",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user1@example.com",
                aggregation="count",
                personalize_enabled=True,
                personalize_column="OwnerEmail",
            )
            self.assertEqual(other.value, 3, "a different viewer gets their OWN count, same cache entry")

    async def test_oversized_table_reports_unavailable_not_a_partial_value(self) -> None:
        records = _make_records(12)
        settings, fake_cache, api_patch, cache_patch = self._patched(records, max_rows=5)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m6",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                aggregation="count",
            )
            # Unlike fetch_widget_rows_cached, this must NOT fall back to a
            # live, partial fetch — a count over only some rows would be
            # silently wrong, not just incomplete.
            self.assertFalse(resp.available)
            self.assertIsNone(resp.value)

    async def test_sum_with_no_field_configured_is_unavailable_not_zero(self) -> None:
        records = _make_records(4)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m7",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                aggregation="sum",
                sum_field=None,
            )
            self.assertFalse(resp.available)
            self.assertIsNone(resp.value)

    # Rows for the dedupe tests below. user0 owns Acme / acme! / Beta / an
    # empty Org; user1 owns Gamma / Delta, which must never leak into
    # user0's personalised distinct count.
    _DEDUPE_RECORDS = [
        {"id": "r1", "fields": {"OwnerEmail": "user0@example.com", "Org": "Acme", "Amount": 1}},
        {"id": "r2", "fields": {"OwnerEmail": "user0@example.com", "Org": "acme!", "Amount": 2}},
        {"id": "r3", "fields": {"OwnerEmail": "user0@example.com", "Org": "Beta", "Amount": 3}},
        {"id": "r4", "fields": {"OwnerEmail": "user0@example.com", "Amount": 4}},
        {"id": "r5", "fields": {"OwnerEmail": "user1@example.com", "Org": "Gamma", "Amount": 5}},
        {"id": "r6", "fields": {"OwnerEmail": "user1@example.com", "Org": "Delta", "Amount": 6}},
    ]

    async def test_count_dedupe_is_applied_after_personalisation(self) -> None:
        settings, fake_cache, api_patch, cache_patch = self._patched(self._DEDUPE_RECORDS)
        with api_patch, cache_patch:
            service = AirtableService(settings)

            async def count(**opts: Any):
                return await service.fetch_widget_metric_cached(
                    link="m-dedupe",
                    url=FAKE_URL,
                    api_key="pat",
                    caller_email="user0@example.com",
                    aggregation="count",
                    personalize_enabled=True,
                    personalize_column="OwnerEmail",
                    count_field="Org",
                    **opts,
                )

            plain = await count()
            self.assertEqual(plain.value, 4, "no switches -> user0's plain record count")
            both = await count(count_distinct=True, count_ignore_empty=True)
            self.assertEqual(both.value, 2, "{acme, beta} — user1's orgs filtered out first")
            distinct = await count(count_distinct=True)
            self.assertEqual(distinct.value, 3, "{acme, beta} + one empty bucket")
            non_empty = await count(count_ignore_empty=True)
            self.assertEqual(non_empty.value, 3)

    async def test_sum_ignores_the_count_settings(self) -> None:
        settings, fake_cache, api_patch, cache_patch = self._patched(self._DEDUPE_RECORDS)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_metric_cached(
                link="m-dedupe-sum",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                aggregation="sum",
                sum_field="Amount",
                count_field="Org",
                count_distinct=True,
                count_ignore_empty=True,
            )
            self.assertEqual(resp.value, 21)


class WidgetMetricHttpTests(_AirtableCacheFixture):
    """Router-level tests for GET /airtable/component/{link}/metric and the
    widened protected-field gate (AIRTABLE_LIKE_WIDGET_TYPES) covering the
    airtable_metric component type."""

    def test_metric_endpoint_returns_computed_count(self) -> None:
        self._seed_widget(
            "metric-count-widget",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count"},
        )
        records = _make_records(8)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/metric-count-widget/metric",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["value"], 8)
        self.assertTrue(body["available"])
        self.assertFalse(body["personalize_blocked"])

    def test_metric_endpoint_returns_computed_sum(self) -> None:
        self._seed_widget(
            "metric-sum-widget",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "sum", "sumField": "Amount"},
        )
        records = [
            {"id": "r1", "fields": {"Amount": 10}},
            {"id": "r2", "fields": {"Amount": 20}},
            {"id": "r3", "fields": {"Amount": 5}},
        ]
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/metric-sum-widget/metric",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["value"], 35)

    _DEDUPE_RECORDS = WidgetMetricServiceTests._DEDUPE_RECORDS

    def _get_metric(self, link: str, *, params: dict | None = None) -> Any:
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(self._DEDUPE_RECORDS)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            return self.client.get(
                f"/data/airtable/component/{link}/metric",
                params=params,
                headers=self._member_auth("someone@example.com"),
            )

    def test_metric_endpoint_reads_count_options_from_stored_data(self) -> None:
        self._seed_widget(
            "metric-dedupe-widget",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={
                "aggregation": "count",
                "countField": " Org ",
                "countDistinct": True,
                "countIgnoreEmpty": True,
            },
        )
        resp = self._get_metric("metric-dedupe-widget")
        self.assertEqual(resp.status_code, 200, resp.text)
        # {acme, beta, gamma, delta}; r4's empty Org is ignored.
        self.assertEqual(resp.json()["value"], 4)

    def test_caller_cannot_supply_count_options(self) -> None:
        # Stored data has NO count options; request params naming them must
        # be ignored — the endpoint has no request-side way in.
        self._seed_widget(
            "metric-no-dedupe-widget",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count"},
        )
        resp = self._get_metric(
            "metric-no-dedupe-widget",
            params={
                "countField": "Org",
                "count_field": "Org",
                "countDistinct": "true",
                "count_distinct": "true",
                "countIgnoreEmpty": "true",
                "count_ignore_empty": "true",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["value"], 6)

    def test_personal_context_passes_count_options_through(self) -> None:
        # The Agent path (get_airtable_personal_context) is the SECOND
        # fetch_widget_metric_cached caller — it must dedupe exactly like the
        # dashboard's /metric route, or the Agent reports a different number.
        from app.routers.airtable import _require_airtable_personal_identity

        widget = self._seed_widget(
            "metric-personal-dedupe",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={
                "aggregation": "count",
                "personalizeEnabled": True,
                "personalizeColumn": "OwnerEmail",
                "countField": "Org",
                "countDistinct": True,
                "countIgnoreEmpty": True,
            },
        )
        self._grant(email="user0@example.com", node=("component", widget.id))
        self.client.app.dependency_overrides[_require_airtable_personal_identity] = (
            lambda: {"email": "user0@example.com", "roles": []}
        )
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(self._DEDUPE_RECORDS)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get("/data/airtable/personal-context")
        self.assertEqual(resp.status_code, 200, resp.text)
        sources = [
            s for s in resp.json()["sources"]
            if s.get("component_link") == "metric-personal-dedupe"
        ]
        self.assertEqual(len(sources), 1, resp.text)
        self.assertTrue(sources[0]["available"], sources[0])
        # user0's 4 records -> {acme, beta}; without passthrough this is 4.
        self.assertEqual(sources[0]["value"], 2)

    def test_metric_endpoint_enforces_access_control(self) -> None:
        # plan_ac_enforcement_closeout_2026-09-09.md §5 — a real grant, not
        # the legacy blob, and a 404 refusal, not 403 (§5.4).
        widget = self._seed_widget(
            "metric-ac-widget",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={"aggregation": "count"},
        )
        self._grant(email="allowed@example.com", node=("component", widget.id))
        records = _make_records(3)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            denied = self.client.get(
                "/data/airtable/component/metric-ac-widget/metric",
                headers=self._auth("outsider@example.com", roles=["Hub Member"]),
            )
            self.assertEqual(denied.status_code, 404)

            allowed = self.client.get(
                "/data/airtable/component/metric-ac-widget/metric",
                headers=self._auth("allowed@example.com"),
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_metric_endpoint_404s_for_a_non_metric_link(self) -> None:
        resp = self.client.get(
            "/data/airtable/component/does-not-exist/metric",
            headers=self._auth("someone@example.com"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_metric_widget_link_is_discoverable_for_cache_refresh(self) -> None:
        # list_airtable_component_links (the cache-refresh sweep's own
        # discovery query) must include airtable_metric widgets, not just
        # the table type — otherwise the cron would never refresh them.
        from app.services.gridstack_service import list_airtable_component_links

        self._seed_widget("metric-discovery-widget", widget_type=AIRTABLE_METRIC_WIDGET_TYPE)
        self._seed_widget("table-discovery-widget", widget_type=AIRTABLE_WIDGET_TYPE)
        links = list_airtable_component_links(self.db)
        self.assertIn("metric-discovery-widget", links)
        self.assertIn("table-discovery-widget", links)

    def test_metric_widget_pat_and_sourceurl_survive_canvas_save_protection(self) -> None:
        # The canvas save (PUT /v2/tabs/{id}/content, exercised here via
        # update_tab_content_v2 directly) has no auth dependency — this is
        # the regression test proving AIRTABLE_LIKE_WIDGET_TYPES actually
        # widened the protected-field gate to the metric type too, not just
        # the table type it was written for originally.
        self._seed_widget("metric-protect-widget", widget_type=AIRTABLE_METRIC_WIDGET_TYPE)
        component = (
            self.db.query(ComponentV2)
            .filter(ComponentV2.link == "metric-protect-widget")
            .first()
        )
        self.assertIsNotNone(component)

        malicious_content = {
            "schemaVersion": 2,
            "layout": [
                {
                    "id": str(component.id),
                    "x": 0, "y": 0, "w": 3, "h": 3,
                    "type": AIRTABLE_METRIC_WIDGET_TYPE,
                }
            ],
            "widgets": {
                str(component.id): {
                    "type": AIRTABLE_METRIC_WIDGET_TYPE,
                    "data": {
                        "sourceUrl": "https://airtable.com/appATTACKER0000/tblATTACKER0000",
                        "pat": "stolen-pat-value",
                        "aggregation": "count",
                    },
                }
            },
        }
        result = update_tab_content_v2(self.db, self.root["documentId"], malicious_content)
        self.assertIsNotNone(result)

        from app.services.gridstack_service import (
            get_airtable_component_config,
            get_airtable_pat_for_component,
        )

        self.assertEqual(
            get_airtable_pat_for_component(self.db, "metric-protect-widget"),
            "fake-pat",
            "the unauthenticated canvas save must not be able to overwrite the PAT",
        )
        self.assertEqual(
            get_airtable_component_config(self.db, "metric-protect-widget")["sourceUrl"],
            FAKE_URL,
            "the unauthenticated canvas save must not be able to overwrite sourceUrl",
        )


class WidgetFullRowsServiceTests(unittest.IsolatedAsyncioTestCase):
    """Direct AirtableService.fetch_widget_full_rows_cached tests — no HTTP
    layer, no DB. Covers the viewer toolbar's whole-table read
    (plan_airtable_widget_viewer_controls_2026-08-12.md §2.4, §5.3).
    """

    _patched = WidgetCacheServiceTests._patched

    async def test_happy_path_returns_every_row_and_field_types(self) -> None:
        records = _make_records(37)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
            )
        self.assertTrue(resp.available)
        self.assertEqual(len(resp.rows), 37)
        self.assertFalse(resp.personalize_blocked)
        self.assertEqual(resp.page_size, settings.AIRTABLE_WIDGET_FULL_VIEW_PAGE_SIZE)
        # No schema.bases:read scope in this fixture -> {} field_types, but
        # the call must still happen (proven by other schema tests) and
        # must not blow up the response.
        self.assertEqual(resp.field_types, {})

    async def test_personalize_fail_closed_before_any_cache_read(self) -> None:
        records = _make_records(6)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr2",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                personalize_enabled=True,
                personalize_column=None,  # not configured -> gate refuses
            )
        self.assertEqual(resp.rows, [])
        self.assertTrue(resp.personalize_blocked)
        self.assertTrue(resp.available, "the gate itself is not a cacheability signal")
        self.assertEqual(call_log, [], "a blocked request must never call Airtable")
        self.assertEqual(fake_cache.store, {}, "a blocked request must never touch the cache")

    async def test_auto_appended_personalize_column_is_not_leaked(self) -> None:
        # L2 — the owner's own stated requirement: a viewer must not be able
        # to surface a column the admin hid, even the one the personalize
        # gate itself needed to READ to do its filtering.
        records = [
            {"id": "r1", "fields": {"Name": "Alpha", "OwnerEmail": "user0@example.com"}},
            {"id": "r2", "fields": {"Name": "Beta", "OwnerEmail": "user1@example.com"}},
        ]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr3",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                selected_columns=["Name"],  # admin never selected OwnerEmail
                personalize_enabled=True,
                personalize_column="OwnerEmail",
            )
        self.assertTrue(resp.available)
        self.assertEqual(len(resp.rows), 1)
        self.assertNotIn("OwnerEmail", resp.fields)
        self.assertNotIn("OwnerEmail", resp.rows[0])
        self.assertEqual(resp.rows[0]["Name"], "Alpha")

    async def test_oversized_table_is_unavailable(self) -> None:
        records = _make_records(12)
        settings, fake_cache, api_patch, cache_patch = self._patched(records, max_rows=5)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr4", url=FAKE_URL, api_key="pat", caller_email="anyone@example.com",
            )
        self.assertFalse(resp.available)
        self.assertEqual(resp.rows, [])

    async def test_viewer_controls_full_view_cap_reports_unavailable(self) -> None:
        # Distinct from the row-WALK cap above: the table caches fine, but
        # the /rows/full RESPONSE cap (AIRTABLE_WIDGET_FULL_VIEW_MAX_ROWS) is
        # smaller than the row count.
        records = _make_records(20)
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, full_view_max_rows=10
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr5", url=FAKE_URL, api_key="pat", caller_email="anyone@example.com",
            )
        self.assertFalse(resp.available)
        self.assertEqual(resp.rows, [])

    async def test_disabled_cache_is_unavailable_and_makes_no_airtable_call(self) -> None:
        records = _make_records(5)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, call_log=call_log, cache_enabled=False
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr6", url=FAKE_URL, api_key="pat", caller_email="anyone@example.com",
            )
        self.assertFalse(resp.available)
        self.assertEqual(resp.rows, [])
        self.assertEqual(
            call_log, [], "a disabled cache must never walk the whole table live (L3)"
        )

    async def test_personalize_then_cap_ordering_available_when_filtered_result_fits(
        self,
    ) -> None:
        # A table ABOVE the full-view cap that personalizes down BELOW it
        # must report available=True — the cap is checked AFTER
        # personalization, not before.
        records = _make_records(20, owner_field="OwnerEmail")
        settings, fake_cache, api_patch, cache_patch = self._patched(
            records, full_view_max_rows=10
        )
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_full_rows_cached(
                link="fr7",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                personalize_enabled=True,
                personalize_column="OwnerEmail",
            )
        # _make_records cycles OwnerEmail across user0/user1/user2 -> ~7 of
        # 20 rows are user0's, comfortably under the cap of 10.
        self.assertTrue(resp.available)
        self.assertGreater(len(resp.rows), 0)
        self.assertLessEqual(len(resp.rows), 10)

    async def test_full_rows_and_paginated_rows_share_one_cache_entry(self) -> None:
        # L1 — the regression test for the fingerprint being byte-identical
        # to fetch_widget_rows_cached's own. A mismatch silently doubles the
        # Airtable walk and the cache storage.
        records = _make_records(50)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            kwargs = dict(
                link="fr8",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
            )
            await service.fetch_widget_rows_cached(cursor=None, **kwargs)
            calls_after_paginated = len(call_log)
            self.assertGreater(calls_after_paginated, 0, "the first call must warm the cache")

            resp = await service.fetch_widget_full_rows_cached(**kwargs)
            self.assertTrue(resp.available)
            self.assertEqual(len(resp.rows), 50)
            self.assertEqual(
                len(call_log),
                calls_after_paginated,
                "a shared cache entry means /rows/full must not trigger a second walk",
            )
            # Same reasoning, the other direction.
            row_cache_entries = [
                v for v in fake_cache.store.values() if isinstance(v, dict) and "rows" in v
            ]
            self.assertEqual(
                len(row_cache_entries), 1, "one shared envelope, not two independent caches"
            )


class WidgetFullRowsHttpTests(_AirtableCacheFixture):
    """Router-level tests for GET /airtable/component/{link}/rows/full —
    the viewerControlsEnabled product-control gate (L4) and access control.
    """

    def test_full_rows_endpoint_returns_data_when_toggle_is_on(self) -> None:
        self._seed_widget(
            "full-rows-on-widget",
            extra_data={"viewerControlsEnabled": True},
        )
        records = _make_records(15)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/full-rows-on-widget/rows/full",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(len(body["rows"]), 15)
        self.assertIn("page_size", body)

    def test_full_rows_endpoint_unavailable_when_toggle_absent(self) -> None:
        self._seed_widget("full-rows-default-widget")  # no viewerControlsEnabled key at all
        records = _make_records(5)
        call_log: list = []
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records, call_log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/full-rows-default-widget/rows/full",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["rows"], [])
        self.assertEqual(call_log, [], "the toggle-off gate must short-circuit before Airtable")

    def test_full_rows_endpoint_unavailable_when_toggle_explicitly_false(self) -> None:
        self._seed_widget(
            "full-rows-off-widget", extra_data={"viewerControlsEnabled": False}
        )
        records = _make_records(5)
        call_log: list = []
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records, call_log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/full-rows-off-widget/rows/full",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["available"])
        self.assertEqual(call_log, [])

    def test_full_rows_endpoint_enforces_access_control(self) -> None:
        # plan_ac_enforcement_closeout_2026-09-09.md §5 — a real grant, not
        # the legacy blob, and a 404 refusal, not 403 (§5.4).
        widget = self._seed_widget(
            "full-rows-ac-widget",
            extra_data={"viewerControlsEnabled": True},
        )
        self._grant(email="allowed@example.com", node=("component", widget.id))
        records = _make_records(3)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            denied = self.client.get(
                "/data/airtable/component/full-rows-ac-widget/rows/full",
                headers=self._auth("outsider@example.com", roles=["Hub Member"]),
            )
            self.assertEqual(denied.status_code, 404)

            allowed = self.client.get(
                "/data/airtable/component/full-rows-ac-widget/rows/full",
                headers=self._auth("allowed@example.com"),
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_full_rows_endpoint_404s_for_unknown_link(self) -> None:
        resp = self.client.get(
            "/data/airtable/component/does-not-exist/rows/full",
            headers=self._auth("someone@example.com"),
        )
        self.assertEqual(resp.status_code, 404)


class ChartGroupKeyTests(unittest.TestCase):
    """AirtableService._chart_group_key — one row's raw field value -> one
    Chart widget group label (plan_airtable_chart_widget_2026-08-13.md
    §3.2, decisions 5 and 6). No HTTP layer, no DB, no Airtable."""

    def test_blank_values_all_become_the_empty_group(self) -> None:
        for value in (None, "", "   ", [], {}):
            self.assertEqual(
                AirtableService._chart_group_key(value), "(Empty)", repr(value)
            )

    def test_multi_value_list_is_one_combined_group_preserving_order(self) -> None:
        self.assertEqual(AirtableService._chart_group_key(["A", "B"]), "A, B")
        # Element order is preserved, NOT sorted (assumption §9.2) — a
        # DIFFERENT group from ["A", "B"].
        self.assertEqual(AirtableService._chart_group_key(["B", "A"]), "B, A")

    def test_list_with_some_blank_elements_drops_only_those(self) -> None:
        self.assertEqual(AirtableService._chart_group_key(["A", "", None, "B"]), "A, B")

    def test_list_of_entirely_blank_elements_is_the_empty_group(self) -> None:
        self.assertEqual(AirtableService._chart_group_key(["", None, []]), "(Empty)")

    def test_bool_renders_as_yes_no_not_true_false(self) -> None:
        self.assertEqual(AirtableService._chart_group_key(True), "Yes")
        self.assertEqual(AirtableService._chart_group_key(False), "No")

    def test_integral_float_renders_without_trailing_zero(self) -> None:
        self.assertEqual(AirtableService._chart_group_key(3.0), "3")
        self.assertEqual(AirtableService._chart_group_key(3.5), "3.5")
        self.assertEqual(AirtableService._chart_group_key(7), "7")

    def test_collaborator_dict_prefers_name_then_email(self) -> None:
        self.assertEqual(
            AirtableService._chart_group_key(
                {"id": "usr1", "email": "a@example.com", "name": "Ada"}
            ),
            "Ada",
        )
        self.assertEqual(
            AirtableService._chart_group_key({"id": "usr2", "email": "b@example.com"}),
            "b@example.com",
        )

    def test_plain_string_is_stripped(self) -> None:
        self.assertEqual(AirtableService._chart_group_key("  Grants  "), "Grants")

    def test_a_value_that_cannot_be_stringified_never_raises(self) -> None:
        class Unstringifiable:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        # A chart that 500s because one cell held an unexpected shape is
        # worse than one odd label — must degrade to "(Empty)", not raise.
        self.assertEqual(AirtableService._chart_group_key(Unstringifiable()), "(Empty)")


class DedupeKeyTests(unittest.TestCase):
    """AirtableService._dedupe_key — the Metric widget's Count-dedupe
    equivalence (plan_metric_chart_feedback_2026-09-30.md §1, D-M4a/b/c).
    Deliberately separate from `_chart_group_key` (ChartGroupKeyTests above
    is unedited)."""

    def key(self, value: Any):
        return AirtableService._dedupe_key(value)

    def assertSame(self, *values: Any) -> None:
        first = self.key(values[0])
        self.assertIsNotNone(first, repr(values[0]))
        for value in values[1:]:
            self.assertEqual(self.key(value), first, f"{values[0]!r} vs {value!r}")

    def assertDifferent(self, a: Any, b: Any) -> None:
        self.assertNotEqual(self.key(a), self.key(b), f"{a!r} vs {b!r}")

    # -- D-M4a normalisation -------------------------------------------------

    def test_case_trim_and_punctuation_fold_together(self) -> None:
        self.assertSame("Acme", " ACME ", "Acme.", "acme!")

    def test_inner_punctuation_removed_and_whitespace_collapsed(self) -> None:
        self.assertSame("Acme, Inc.", "acme   inc")
        self.assertSame("O'Brien", "obrien")

    def test_nfkc_composed_and_decomposed_accents_are_equal(self) -> None:
        self.assertSame("Café", "Café")

    def test_underscore_is_removed(self) -> None:
        self.assertSame("a_b", "ab")

    def test_normalize_text_steps(self) -> None:
        self.assertEqual(AirtableService._normalize_dedupe_text("  Acme,\tInc. "), "acme inc")

    # -- numbers (text-only sub-decision) ------------------------------------

    def test_real_numbers_are_not_punctuation_stripped(self) -> None:
        self.assertDifferent(1.5, 15)
        self.assertSame(1, 1.0)

    def test_text_numbers_are_normalised_like_any_text(self) -> None:
        # Pins the documented flip side of the text-only rule.
        self.assertSame("1.5", "15")

    # -- D-M4b Rule B ----------------------------------------------------------

    def test_multi_value_order_is_ignored(self) -> None:
        self.assertSame(["Health", "Education"], ["Education", "Health"])

    def test_subset_is_a_different_value(self) -> None:
        self.assertDifferent(["Health"], ["Health", "Education"])

    def test_in_record_duplicates_collapse_and_scalar_equals_singleton(self) -> None:
        self.assertSame(["Health", "health"], "Health", ["Health"])

    def test_nested_lists_flatten(self) -> None:
        self.assertSame([["Health"], ["Education", ["Arts"]]], ["Arts", "Education", "Health"])

    # -- D-M4c empty -------------------------------------------------------------

    def test_empty_values(self) -> None:
        for value in (None, "", "  ", "—", "...", [], {}, [None, "", "!!"]):
            self.assertIsNone(self.key(value), repr(value))
        # A missing key reads as None through row.get().
        self.assertIsNone(self.key({"Other": "x"}.get("Org")))

    # -- collaborators -------------------------------------------------------------

    def test_collaborator_dict_matches_by_name_then_email(self) -> None:
        self.assertSame(
            {"id": "usr1", "email": "a@example.com", "name": "Ada"},
            "ada",
            {"id": "usr9", "name": " ADA "},
        )
        self.assertSame({"id": "usr2", "email": "b@example.com"}, "b@example.com")
        self.assertDifferent(
            {"id": "usr1", "email": "a@example.com", "name": "Ada"},
            {"id": "usr2", "email": "a@example.com", "name": "Bea"},
        )

    def test_bool_is_yes_no(self) -> None:
        self.assertSame(True, "yes")
        self.assertDifferent(True, False)

    def test_a_value_that_cannot_be_stringified_never_raises(self) -> None:
        class Unstringifiable:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        self.assertIsNone(self.key(Unstringifiable()))


class CountRowsTests(unittest.TestCase):
    """AirtableService._count_rows — the four switch combinations of plan
    §1 "M4 semantics"."""

    ROWS = [
        {"id": "r1", "Org": "Acme"},
        {"id": "r2", "Org": "acme!"},
        {"id": "r3", "Org": "Beta"},
        {"id": "r4", "Org": None},
        {"id": "r5"},  # field missing (Airtable omits empty fields)
        {"id": "r6", "Org": "  "},
    ]

    def count(self, *, field: str | None = "Org", distinct: bool, ignore_empty: bool) -> int:
        return AirtableService._count_rows(
            self.ROWS,
            count_field=field,
            count_distinct=distinct,
            count_ignore_empty=ignore_empty,
        )

    def test_both_off_counts_every_record(self) -> None:
        self.assertEqual(self.count(distinct=False, ignore_empty=False), 6)

    def test_ignore_empty_only_counts_non_empty_records(self) -> None:
        self.assertEqual(self.count(distinct=False, ignore_empty=True), 3)

    def test_distinct_only_counts_all_empties_as_one_value(self) -> None:
        # {acme, beta} + 1 for the empty bucket.
        self.assertEqual(self.count(distinct=True, ignore_empty=False), 3)

    def test_distinct_without_any_empty_adds_nothing(self) -> None:
        rows = [{"Org": "Acme"}, {"Org": "ACME"}, {"Org": "Beta"}]
        self.assertEqual(
            AirtableService._count_rows(
                rows, count_field="Org", count_distinct=True, count_ignore_empty=False
            ),
            2,
        )

    def test_both_on_counts_distinct_non_empty_values(self) -> None:
        self.assertEqual(self.count(distinct=True, ignore_empty=True), 2)

    def test_no_count_field_is_a_plain_record_count(self) -> None:
        for field in (None, ""):
            self.assertEqual(self.count(field=field, distinct=True, ignore_empty=True), 6)

    def test_empty_row_list(self) -> None:
        self.assertEqual(
            AirtableService._count_rows(
                [], count_field="Org", count_distinct=True, count_ignore_empty=False
            ),
            0,
        )


class WidgetChartServiceTests(unittest.IsolatedAsyncioTestCase):
    """Direct AirtableService.fetch_widget_chart_cached tests — no HTTP
    layer, no DB. Covers the Airtable Chart widget
    (plan_airtable_chart_widget_2026-08-13.md): grouped Count/Sum computed
    over the SAME `widget_rows` cache envelope the Metric widget uses, the
    fail-closed personalize gate (L5), the refusal to fall back to a live
    partial fetch for an oversized table (L4), truncate-then-sort (L6), the
    percent denominator (L7), and one warmed entry serving both the viewer
    endpoint and the preview endpoint (L1).
    """

    _patched = WidgetCacheServiceTests._patched

    async def test_count_per_group(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East"}},
            {"id": "r2", "fields": {"Region": "East"}},
            {"id": "r3", "fields": {"Region": "West"}},
        ]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
            )
            self.assertTrue(resp.available)
            self.assertEqual({g.label: g.value for g in resp.groups}, {"East": 2, "West": 1})
            self.assertEqual(resp.row_count, 3)
            self.assertEqual(resp.total, 3)
            self.assertEqual(resp.other_group_count, 0)

    async def test_sum_per_group_skips_non_numeric_values(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East", "Amount": 100}},
            {"id": "r2", "fields": {"Region": "East", "Amount": "not a number"}},
            {"id": "r3", "fields": {"Region": "West", "Amount": 50}},
        ]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c2",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="sum",
                sum_field="Amount",
            )
            self.assertEqual({g.label: g.value for g in resp.groups}, {"East": 100, "West": 50})

    async def test_group_with_no_numeric_values_still_exists_at_zero(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East", "Amount": "n/a"}},
            {"id": "r2", "fields": {"Region": "West", "Amount": 10}},
        ]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c3",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="sum",
                sum_field="Amount",
            )
            by_label = {g.label: g.value for g in resp.groups}
            self.assertIn("East", by_label, "a group with no summable rows must still exist")
            self.assertEqual(by_label["East"], 0)

    async def test_blank_group_value_becomes_its_own_empty_group(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East"}},
            {"id": "r2", "fields": {}},  # Region missing entirely
        ]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c4",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
            )
            self.assertEqual({g.label: g.value for g in resp.groups}, {"East": 1, "(Empty)": 1})
            self.assertEqual(resp.row_count, 2, "a blank group value must not disappear from the total")

    async def test_personalize_fail_closed_computes_no_groups(self) -> None:
        records = [{"id": "r1", "fields": {"Region": "East"}}]
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c5",
                url=FAKE_URL,
                api_key="pat",
                caller_email="user0@example.com",
                group_field="Region",
                aggregation="count",
                personalize_enabled=True,
                personalize_column=None,  # not configured -> gate refuses
            )
            self.assertEqual(resp.groups, [])
            self.assertTrue(resp.personalize_blocked)
            self.assertTrue(resp.available)
            self.assertEqual(fake_cache.store, {}, "a blocked request must never touch the cache")

    async def test_oversized_table_reports_unavailable_not_a_partial_chart(self) -> None:
        records = _make_records(12)
        settings, fake_cache, api_patch, cache_patch = self._patched(records, max_rows=5)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c6",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Name",
                aggregation="count",
            )
            self.assertFalse(resp.available)
            self.assertEqual(resp.groups, [])

    async def test_unconfigured_group_field_is_unavailable(self) -> None:
        records = _make_records(3)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c7",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field=None,
                aggregation="count",
            )
            self.assertFalse(resp.available)

    async def test_sum_with_no_field_configured_is_unavailable(self) -> None:
        records = _make_records(3)
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c8",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Name",
                aggregation="sum",
                sum_field=None,
            )
            self.assertFalse(resp.available)

    async def test_truncate_by_value_then_display_sort_l6(self) -> None:
        # Labels chosen so alphabetical order does NOT coincide with value
        # order (Zebra=10 is the biggest but alphabetically last) — a test
        # where the two orders happened to agree would pass even with L6's
        # guard removed. maxGroups=3 -> top 2 BY VALUE kept (Zebra=10,
        # Apple=8), the rest folded into 'Other' (Mango+Banana+Yak=6+4+2=12).
        # A sort-before-truncate bug would instead keep the alphabetically-
        # first two (Apple, Banana) and fold Zebra itself into Other.
        records = []
        sizes = {"Zebra": 10, "Apple": 8, "Mango": 6, "Banana": 4, "Yak": 2}
        for label, n in sizes.items():
            for i in range(n):
                records.append({"id": f"{label}{i}", "fields": {"Group": label}})
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c9",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Group",
                aggregation="count",
                max_groups=3,
                group_sort="label_asc",
            )
            labels = [g.label for g in resp.groups]
            self.assertEqual(
                labels, ["Apple", "Zebra", "Other"], "top-2 BY VALUE, then label_asc, Other last"
            )
            other = next(g for g in resp.groups if g.is_other)
            self.assertEqual(other.value, 12)
            self.assertEqual(resp.other_group_count, 3)

    async def test_percent_denominator_includes_other_l7(self) -> None:
        records = []
        sizes = {"A": 5, "B": 3, "C": 2}
        for label, n in sizes.items():
            for i in range(n):
                records.append({"id": f"{label}{i}", "fields": {"Group": label}})
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c10",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Group",
                aggregation="count",
                max_groups=2,  # top-1 kept (A=5), B+C folded into Other (5)
            )
            self.assertEqual(resp.total, sum(g.value for g in resp.groups))
            self.assertEqual(resp.total, 10, "the % denominator must include the Other bucket")

    async def test_max_groups_is_clamped_to_the_settings_ceiling(self) -> None:
        records = [{"id": f"r{i}", "fields": {"Group": f"G{i}"}} for i in range(20)]
        settings, fake_cache, api_patch, cache_patch = self._patched(records, chart_max_groups=5)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            resp = await service.fetch_widget_chart_cached(
                link="c11",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Group",
                aggregation="count",
                max_groups=20,  # a widget preference far past the ceiling
            )
            # 20 distinct groups, AIRTABLE_CHART_MAX_GROUPS=5 clamps
            # effective_max to 5: top 4 real groups + one Other.
            self.assertEqual(len(resp.groups), 5)
            self.assertEqual(resp.other_group_count, 16)

    async def test_one_warmed_entry_serves_both_chart_and_preview_l1(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East"}},
            {"id": "r2", "fields": {"Region": "West"}},
        ]
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            viewer_resp = await service.fetch_widget_chart_cached(
                link="c12",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
            )
            calls_after_warm = len(call_log)
            self.assertGreater(calls_after_warm, 0)

            preview_resp = await service.preview_widget_chart(
                link="c12",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
                filters=None,
            )
            self.assertEqual(
                len(call_log), calls_after_warm, "the preview must hit the SAME warmed entry, not re-walk"
            )
            self.assertFalse(preview_resp.partial)
            self.assertEqual(
                {g.label: g.value for g in preview_resp.groups},
                {g.label: g.value for g in viewer_resp.groups},
            )


class WidgetChartPreviewServiceTests(unittest.IsolatedAsyncioTestCase):
    """AirtableService.preview_widget_chart — the Chart widget's editor
    preview (decision 3, plan §3.4). Covers L3 (never warms the cache) and
    that a warm hit is exact and matches the viewer endpoint."""

    _patched = WidgetCacheServiceTests._patched

    async def test_cold_key_never_warms_the_cache_and_reports_partial_l3(self) -> None:
        records = _make_records(5)
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch, mock.patch.object(
            AirtableService, "_build_widget_cache_envelope"
        ) as envelope_builder:
            service = AirtableService(settings)
            resp = await service.preview_widget_chart(
                link="cp1",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Name",
                aggregation="count",
                filters=None,
            )
            self.assertTrue(resp.partial)
            # The live capped read itself legitimately calls Airtable once —
            # what must NOT happen is the row-cache WARMING machinery: no
            # walk, no lock, no cache entry written.
            envelope_builder.assert_not_called()
            self.assertEqual(fake_cache.store, {}, "the preview must never write a cache entry")
            self.assertEqual(fake_cache._locks, {}, "the preview must never acquire the row-cache lock")

    async def test_warm_key_is_exact_and_matches_the_viewer_endpoint(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East"}},
            {"id": "r2", "fields": {"Region": "East"}},
            {"id": "r3", "fields": {"Region": "West"}},
        ]
        call_log: list = []
        settings, fake_cache, api_patch, cache_patch = self._patched(records, call_log=call_log)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            viewer_resp = await service.fetch_widget_chart_cached(
                link="cp2",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
            )
            calls_after_warm = len(call_log)

            preview_resp = await service.preview_widget_chart(
                link="cp2",
                url=FAKE_URL,
                api_key="pat",
                caller_email="anyone@example.com",
                group_field="Region",
                aggregation="count",
                filters=None,
            )
            self.assertFalse(preview_resp.partial)
            self.assertEqual(len(call_log), calls_after_warm, "a warm hit must not call Airtable again")
            self.assertEqual(
                {g.label: g.value for g in preview_resp.groups},
                {g.label: g.value for g in viewer_resp.groups},
            )
            self.assertEqual(preview_resp.total, viewer_resp.total)


class WidgetChartHttpTests(_AirtableCacheFixture):
    """Router-level tests for GET /airtable/component/{link}/chart and POST
    /airtable/component/chart/preview."""

    def test_chart_endpoint_returns_computed_groups(self) -> None:
        self._seed_widget(
            "chart-count-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Region", "aggregation": "count"},
        )
        records = [
            {"id": "r1", "fields": {"Region": "East"}},
            {"id": "r2", "fields": {"Region": "West"}},
            {"id": "r3", "fields": {"Region": "East"}},
        ]
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/chart-count-widget/chart",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(
            {g["label"]: g["value"] for g in body["groups"]}, {"East": 2, "West": 1}
        )

    def test_chart_endpoint_enforces_access_control(self) -> None:
        # plan_ac_enforcement_closeout_2026-09-09.md §5 — a real grant, not
        # the legacy blob, and a 404 refusal, not 403 (§5.4).
        widget = self._seed_widget(
            "chart-ac-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Region", "aggregation": "count"},
        )
        self._grant(email="allowed@example.com", node=("component", widget.id))
        records = _make_records(3)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            denied = self.client.get(
                "/data/airtable/component/chart-ac-widget/chart",
                headers=self._auth("outsider@example.com", roles=["Hub Member"]),
            )
            self.assertEqual(denied.status_code, 404)

            allowed = self.client.get(
                "/data/airtable/component/chart-ac-widget/chart",
                headers=self._auth("allowed@example.com"),
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_chart_endpoint_404s_for_a_non_chart_link(self) -> None:
        resp = self.client.get(
            "/data/airtable/component/does-not-exist/chart",
            headers=self._auth("someone@example.com"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_chart_endpoint_unconfigured_group_field_is_unavailable(self) -> None:
        self._seed_widget("chart-unconfigured-widget", widget_type=AIRTABLE_CHART_WIDGET_TYPE)
        records = _make_records(3)
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(
                "/data/airtable/component/chart-unconfigured-widget/chart",
                headers=self._member_auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["available"])

    def test_chart_preview_own_token_shape(self) -> None:
        records = [
            {"id": "r1", "fields": {"Region": "East"}},
            {"id": "r2", "fields": {"Region": "West"}},
        ]
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(records)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.post(
                "/data/airtable/component/chart/preview",
                json={
                    "sourceUrl": FAKE_URL,
                    "pat": "own-pat",
                    "groupField": "Region",
                    "aggregation": "count",
                },
                headers=self._auth("someone@example.com"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["partial"], "own-token shape has no link to check a warmed cache against")
        self.assertEqual({g["label"]: g["value"] for g in body["groups"]}, {"East": 1, "West": 1})

    def test_chart_preview_rejects_stored_token_with_a_different_source_url(self) -> None:
        # This test is not ABOUT the §5 access-control gate — it is about
        # the URL/token-mismatch check one step further into the `link`
        # branch. Without a grant (or the Hub Admin bypass) the new gate
        # would 404 before ever reaching that check, so the caller uses
        # `_member_auth` here (see that helper's own docstring).
        self._seed_widget(
            "chart-preview-mismatch-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Region", "aggregation": "count"},
        )
        resp = self.client.post(
            "/data/airtable/component/chart/preview",
            json={
                "link": "chart-preview-mismatch-widget",
                "sourceUrl": "https://airtable.com/appOTHER00000AA/tblOTHER00000BB",
                "groupField": "Region",
                "aggregation": "count",
            },
            headers=self._member_auth("someone@example.com"),
        )
        self.assertEqual(resp.status_code, 400, resp.text)


def _status_program_records(spec: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """{status: [program value per record]} -> records; a `_MISSING` value
    leaves the Program field out entirely (how Airtable reports empty)."""
    records: list[dict[str, Any]] = []
    for status, programs in spec.items():
        for i, program in enumerate(programs):
            fields: dict[str, Any] = {"Status": status}
            if program is not _MISSING:
                fields["Program"] = program
            records.append({"id": f"{status}-{i}", "fields": fields})
    return records


_MISSING = object()

# In progress: P1 = p1. (normalised), P2, one missing  -> 4 records
# Todo:        P1, ""                                  -> 2 records
# Done:        three records whose Program is empty    -> 3 records
_CHART_DEDUPE_SPEC: dict[str, list[Any]] = {
    "In progress": ["P1", "p1.", "P2", _MISSING],
    "Todo": ["P1", ""],
    "Done": ["—", _MISSING, None],
}


class ChartCountDedupeServiceTests(unittest.IsolatedAsyncioTestCase):
    """Chart Count "dedupe by another field" (2026-10-03): each group's
    Count becomes the Metric's `_count_rows` over that group's records.
    Grouping itself is untouched (ChartGroupKeyTests pass unedited)."""

    _patched = WidgetCacheServiceTests._patched

    async def _chart(self, records, **kwargs):
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        params = {
            "link": "cd",
            "url": FAKE_URL,
            "api_key": "pat",
            "caller_email": "anyone@example.com",
            "group_field": "Status",
            "aggregation": "count",
            **kwargs,
        }
        with api_patch, cache_patch:
            return await AirtableService(settings).fetch_widget_chart_cached(**params)

    async def test_all_four_switch_combinations_per_group(self) -> None:
        records = _status_program_records(_CHART_DEDUPE_SPEC)
        expected = {
            (False, False): {"In progress": 4, "Todo": 2, "Done": 3},
            (False, True): {"In progress": 3, "Todo": 1, "Done": 0},
            (True, False): {"In progress": 3, "Todo": 2, "Done": 1},
            (True, True): {"In progress": 2, "Todo": 1, "Done": 0},
        }
        for (distinct, ignore_empty), want in expected.items():
            with self.subTest(distinct=distinct, ignore_empty=ignore_empty):
                resp = await self._chart(
                    records,
                    count_field="Program",
                    count_distinct=distinct,
                    count_ignore_empty=ignore_empty,
                )
                self.assertEqual({g.label: g.value for g in resp.groups}, want)
                self.assertEqual(resp.total, sum(want.values()))
                self.assertEqual(resp.row_count, 9, "row_count stays the record count")

    async def test_dedupe_field_unset_is_todays_record_count(self) -> None:
        records = _status_program_records(_CHART_DEDUPE_SPEC)
        resp = await self._chart(records, count_distinct=True, count_ignore_empty=True)
        self.assertEqual(
            {g.label: g.value for g in resp.groups}, {"In progress": 4, "Todo": 2, "Done": 3}
        )

    async def test_zero_group_stays_and_ranks_last(self) -> None:
        # Plain: Done=3 outranks Todo=2. Deduped (both on): Done=0 — still
        # drawn, ranked last.
        records = _status_program_records(_CHART_DEDUPE_SPEC)
        resp = await self._chart(
            records, count_field="Program", count_distinct=True, count_ignore_empty=True
        )
        self.assertEqual(
            [(g.label, g.value) for g in resp.groups],
            [("In progress", 2), ("Todo", 1), ("Done", 0)],
        )

    async def test_zero_group_competes_for_a_max_groups_slot(self) -> None:
        records = _status_program_records(_CHART_DEDUPE_SPEC)
        plain = await self._chart(records, count_field="Program", max_groups=3)
        self.assertEqual(
            [g.label for g in plain.groups], ["In progress", "Done", "Other"],
            "plain: Done (3) beats Todo (2) for the second slot",
        )
        deduped = await self._chart(
            records, count_field="Program", count_ignore_empty=True, max_groups=3
        )
        self.assertEqual(
            [(g.label, g.value) for g in deduped.groups],
            [("In progress", 3), ("Todo", 1), ("Other", 0)],
            "deduped: Done (0) loses the slot and folds into Other, not dropped",
        )
        self.assertEqual(deduped.other_group_count, 1)

    async def test_other_is_deduped_as_one_group(self) -> None:
        # A is kept; B, C, D fold into Other. Program P is in all three
        # folded groups -> counts once in Other: {P, Q} = 2, not 2+1+1.
        records = _status_program_records(
            {
                "A": ["X1", "X2", "X3", "X4", "X5"],
                "B": ["P", "Q"],
                "C": ["P"],
                "D": ["p "],
            }
        )
        resp = await self._chart(
            records, count_field="Program", count_distinct=True, max_groups=2
        )
        self.assertEqual(
            [(g.label, g.value, g.is_other) for g in resp.groups],
            [("A", 5, False), ("Other", 2, True)],
        )
        self.assertEqual(resp.other_group_count, 3)
        self.assertEqual(resp.total, 7, "% denominator = sum of the drawn values")

        plain = await self._chart(records, count_field="Program", max_groups=2)
        self.assertEqual(plain.groups[-1].value, 4, "dedupe off: Other = sum of record counts")

    async def test_sum_ignores_the_count_settings(self) -> None:
        records = [
            {"id": "r1", "fields": {"Status": "A", "Program": "P", "Amount": 10}},
            {"id": "r2", "fields": {"Status": "A", "Program": "P", "Amount": 5}},
            {"id": "r3", "fields": {"Status": "B", "Amount": 1}},
        ]
        resp = await self._chart(
            records,
            aggregation="sum",
            sum_field="Amount",
            count_field="Program",
            count_distinct=True,
            count_ignore_empty=True,
        )
        self.assertEqual({g.label: g.value for g in resp.groups}, {"A": 15, "B": 1})

    async def test_label_asc_truncates_by_deduped_value(self) -> None:
        # Plain: Zeta=4, Mid=3 kept, Alpha=2 folded. Distinct: Zeta has ONE
        # program (1) -> Mid=3, Alpha=2 kept, Zeta folded; then label_asc.
        records = _status_program_records(
            {
                "Zeta": ["Same", "same", "SAME!", "Same"],
                "Mid": ["M1", "M2", "M3"],
                "Alpha": ["A1", "A2"],
            }
        )
        resp = await self._chart(
            records,
            count_field="Program",
            count_distinct=True,
            max_groups=3,
            group_sort="label_asc",
        )
        self.assertEqual(
            [(g.label, g.value) for g in resp.groups],
            [("Alpha", 2), ("Mid", 3), ("Other", 1)],
        )

    async def test_dedupe_applies_after_personalisation(self) -> None:
        records = [
            {"id": "a", "fields": {"Status": "East", "Program": "A", "Owner": "me@example.com"}},
            {"id": "b", "fields": {"Status": "East", "Program": "a", "Owner": "me@example.com"}},
            {"id": "c", "fields": {"Status": "East", "Program": "B", "Owner": "them@example.com"}},
            {"id": "d", "fields": {"Status": "West", "Program": "C", "Owner": "them@example.com"}},
        ]
        resp = await self._chart(
            records,
            caller_email="me@example.com",
            personalize_enabled=True,
            personalize_column="Owner",
            count_field="Program",
            count_distinct=True,
        )
        self.assertEqual(
            {g.label: g.value for g in resp.groups}, {"East": 1},
            "only the viewer's rows, then deduped: {a} — not {a, b}",
        )

    async def test_dedupe_field_equal_to_group_field_is_ignored(self) -> None:
        # Owner decision 2026-10-03: D = G in stored data is ignored.
        records = _status_program_records(_CHART_DEDUPE_SPEC)
        resp = await self._chart(
            records, count_field="Status", count_distinct=True, count_ignore_empty=True
        )
        self.assertEqual(
            {g.label: g.value for g in resp.groups}, {"In progress": 4, "Todo": 2, "Done": 3}
        )

    async def test_preview_honours_dedupe_on_both_branches(self) -> None:
        records = _status_program_records(_CHART_DEDUPE_SPEC)
        want = {"In progress": 2, "Todo": 1, "Done": 0}
        opts = {"count_field": "Program", "count_distinct": True, "count_ignore_empty": True}
        settings, fake_cache, api_patch, cache_patch = self._patched(records)
        with api_patch, cache_patch:
            service = AirtableService(settings)
            common = {
                "url": FAKE_URL,
                "api_key": "pat",
                "caller_email": "anyone@example.com",
                "group_field": "Status",
                "aggregation": "count",
                "filters": None,
            }
            live = await service.preview_widget_chart(link="cold-link", **common, **opts)
            self.assertTrue(live.partial, "cold key -> live capped branch")
            self.assertEqual({g.label: g.value for g in live.groups}, want)

            # Warm the entry WITHOUT dedupe (the saved settings), then
            # preview the unsaved dedupe settings against it.
            await service.fetch_widget_chart_cached(link="warm-link", **{
                k: v for k, v in common.items() if k != "filters"
            })
            cached = await service.preview_widget_chart(link="warm-link", **common, **opts)
            self.assertFalse(cached.partial, "warm key -> cached branch")
            self.assertEqual({g.label: g.value for g in cached.groups}, want)


class ChartCountDedupeHttpTests(_AirtableCacheFixture):
    """The saved /chart route reads the dedupe settings from stored data
    only; the preview route takes them from the body."""

    def _call(self, fn):
        fake_cache = FakeCacheService()
        with mock.patch(
            "app.services.airtable_service.Api",
            make_fake_api_class(_status_program_records(_CHART_DEDUPE_SPEC)),
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            return fn()

    def test_chart_endpoint_reads_dedupe_from_stored_data(self) -> None:
        self._seed_widget(
            "chart-dedupe-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Status",
                "aggregation": "count",
                "countField": "Program",
                "countDistinct": True,
                "countIgnoreEmpty": True,
            },
        )
        resp = self._call(lambda: self.client.get(
            "/data/airtable/component/chart-dedupe-widget/chart",
            headers=self._member_auth("someone@example.com"),
        ))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            {g["label"]: g["value"] for g in resp.json()["groups"]},
            {"In progress": 2, "Todo": 1, "Done": 0},
        )

    def test_caller_cannot_supply_dedupe_on_the_saved_endpoint(self) -> None:
        self._seed_widget(
            "chart-no-dedupe-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Status", "aggregation": "count"},
        )
        resp = self._call(lambda: self.client.get(
            "/data/airtable/component/chart-no-dedupe-widget/chart",
            params={
                "countField": "Program",
                "count_field": "Program",
                "countDistinct": "true",
                "count_distinct": "true",
                "countIgnoreEmpty": "true",
                "count_ignore_empty": "true",
            },
            headers=self._member_auth("someone@example.com"),
        ))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            {g["label"]: g["value"] for g in resp.json()["groups"]},
            {"In progress": 4, "Todo": 2, "Done": 3},
        )

    def test_preview_honours_body_dedupe_on_both_branches(self) -> None:
        self._seed_widget(
            "chart-preview-dedupe-widget",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={"groupField": "Status", "aggregation": "count"},
        )
        dedupe_body = {
            "groupField": "Status",
            "aggregation": "count",
            "countField": "Program",
            "countDistinct": True,
            "countIgnoreEmpty": True,
        }
        want = {"In progress": 2, "Todo": 1, "Done": 0}

        def run():
            live = self.client.post(
                "/data/airtable/component/chart/preview",
                json={"sourceUrl": FAKE_URL, "pat": "own-pat", **dedupe_body},
                headers=self._auth("someone@example.com"),
            )
            # Warm the widget's own entry through the saved route, then
            # preview the unsaved dedupe against it.
            warm = self.client.get(
                "/data/airtable/component/chart-preview-dedupe-widget/chart",
                headers=self._member_auth("someone@example.com"),
            )
            cached = self.client.post(
                "/data/airtable/component/chart/preview",
                json={"link": "chart-preview-dedupe-widget", **dedupe_body},
                headers=self._member_auth("someone@example.com"),
            )
            return live, warm, cached

        live, warm, cached = self._call(run)
        self.assertEqual(live.status_code, 200, live.text)
        self.assertTrue(live.json()["partial"])
        self.assertEqual({g["label"]: g["value"] for g in live.json()["groups"]}, want)
        self.assertEqual(warm.status_code, 200, warm.text)
        self.assertEqual(cached.status_code, 200, cached.text)
        self.assertFalse(cached.json()["partial"])
        self.assertEqual({g["label"]: g["value"] for g in cached.json()["groups"]}, want)


class WarmKeyAlignmentTests(_AirtableCacheFixture):
    """plan_warm_key_divergence_2026-08-14.md — `warm_widget_cache` used to
    build its fingerprint straight from the widget's stored config with no
    notion of widget type, so a Metric/Chart widget with personalization on
    (or merely a stored `personalizeColumn`) warmed a `widget_rows` key its
    own read path never looked up: a wasted whole-table Upstash entry, and
    the widget stayed cold until a real viewer's own request warmed the
    RIGHT key.

    Proven here by counting the ACTUAL Airtable calls each step makes — an
    empty log after the read means it was served from the warmed entry; a
    non-empty one means it missed and walked Airtable a second time. Every
    `test_*_is_served_from_the_warmed_entry` case below fails at
    `81ea8f5` (pre-fix) and passes after it.
    """

    _RECORDS = [
        {"id": "r1", "fields": {"Region": "East", "OwnerEmail": "user0@example.com"}},
        {"id": "r2", "fields": {"Region": "West", "OwnerEmail": "user0@example.com"}},
        {"id": "r3", "fields": {"Region": "East", "OwnerEmail": "other@example.com"}},
    ]
    _CALLER = "user0@example.com"

    def _save_config(self, link: str, fake_cache: FakeCacheService, body: dict) -> list:
        """PUT the widget's config — fires `_warm_after_config_save` as a
        background task, which `TestClient` runs to completion before this
        returns (no polling needed). Returns the Airtable call log the warm
        itself made.

        Hub Admin role AND a live edit session, both added for
        plan_lock_propagation_2026-09-08.md §5.3: this endpoint gained its
        first-ever `require_edit` + `require_live_session` gate under that
        plan (it had NEITHER before). Hub Admin bypasses the permission
        half only — locking is a separate axis with no admin exception —
        so this also acquires a real lock on the widget's owning canvas
        (every widget in this class lives on `self.root_gridstack`, per
        `_seed_widget`'s own default) and sends its token. Both are
        orthogonal to this class's own cache/warm assertions."""
        auth = self._auth(self._CALLER, roles=["Hub Admin"])
        lock_resp = self.client.put(f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=auth)
        token = lock_resp.json()["data"]["lock_token"]

        log: list = []
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(self._RECORDS, log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.SessionLocalV2", self._session_factory
        ):
            resp = self.client.put(
                f"/data/airtable/component/{link}/config",
                json=body,
                headers={**auth, "X-Edit-Tokens": token},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
        return log

    def _cron_refresh(self, link: str, fake_cache: FakeCacheService) -> list:
        log: list = []
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(self._RECORDS, log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ), mock.patch(
            "app.routers.airtable.get_settings",
            return_value=SimpleNamespace(CACHE_REFRESH_TOKEN="tok"),
        ):
            resp = self.client.post(
                f"/data/airtable/cache/refresh?link={link}",
                headers={"X-Sync-Token": "tok"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["refreshed"], 1, resp.text)
        return log

    def _read(self, path: str, fake_cache: FakeCacheService):
        """GET the widget's own read endpoint. Returns `(body, call_log)` —
        an empty log means the read was served from the warmed entry.

        `_member_auth`, not `_auth` — this class's tests are about the
        cache fingerprint, not the §5 access-control gate, and a Hub Admin
        token keeps `self._CALLER`'s own email (personalization still
        matches on it) while bypassing the fold, matching `_save_config`'s
        own reasoning just above."""
        log: list = []
        with mock.patch(
            "app.services.airtable_service.Api", make_fake_api_class(self._RECORDS, log)
        ), mock.patch(
            "app.services.airtable_service.get_cache_service", return_value=fake_cache
        ):
            resp = self.client.get(path, headers=self._member_auth(self._CALLER))
            self.assertEqual(resp.status_code, 200, resp.text)
            return resp.json(), log

    def test_chart_personalize_on_is_served_from_the_warmed_entry(self) -> None:
        self._seed_widget(
            "warmkey-chart-on",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "aggregation": "count",
                "personalizeColumn": "OwnerEmail",
                "personalizeEnabled": True,
            },
        )
        cache = FakeCacheService()
        warm_log = self._save_config("warmkey-chart-on", cache, {"personalizeEnabled": True})
        self.assertTrue(warm_log, "warm-on-save should have walked Airtable")
        self.assertEqual(len(cache.store), 1, "warm must land on ONE key, not a wasted duplicate")
        _body, read_log = self._read("/data/airtable/component/warmkey-chart-on/chart", cache)
        self.assertEqual(read_log, [], "reader re-walked Airtable — warm landed on the wrong key")

    def test_chart_personalize_off_with_a_stored_column_is_served_from_the_warmed_entry(
        self,
    ) -> None:
        # The narrower trigger (§1.3 point 2): personalize is OFF, but a
        # column is still stored from an earlier save. Independently
        # sufficient to cause the mismatch — must be covered on its own.
        self._seed_widget(
            "warmkey-chart-col",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "aggregation": "count",
                "personalizeColumn": "OwnerEmail",
                "personalizeEnabled": False,
            },
        )
        cache = FakeCacheService()
        warm_log = self._save_config("warmkey-chart-col", cache, {"personalizeEnabled": False})
        self.assertTrue(warm_log, "warm-on-save should have walked Airtable")
        _body, read_log = self._read("/data/airtable/component/warmkey-chart-col/chart", cache)
        self.assertEqual(read_log, [], "reader re-walked Airtable — warm landed on the wrong key")

    def test_metric_personalize_on_is_served_from_the_warmed_entry(self) -> None:
        self._seed_widget(
            "warmkey-metric-on",
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            extra_data={
                "aggregation": "count",
                "personalizeColumn": "OwnerEmail",
                "personalizeEnabled": True,
            },
        )
        cache = FakeCacheService()
        warm_log = self._save_config("warmkey-metric-on", cache, {"personalizeEnabled": True})
        self.assertTrue(warm_log, "warm-on-save should have walked Airtable")
        _body, read_log = self._read("/data/airtable/component/warmkey-metric-on/metric", cache)
        self.assertEqual(read_log, [], "reader re-walked Airtable — warm landed on the wrong key")

    def test_cron_refresh_of_a_personalize_on_chart_is_served_from_the_warmed_entry(
        self,
    ) -> None:
        # The cron sweep does NOT rescue this on its own tick — it builds
        # the identical (pre-fix, wrong) fingerprint _refresh_one always
        # has, so it re-warms the same unread key every time.
        self._seed_widget(
            "warmkey-chart-cron",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "aggregation": "count",
                "personalizeColumn": "OwnerEmail",
                "personalizeEnabled": True,
            },
        )
        cache = FakeCacheService()
        cron_log = self._cron_refresh("warmkey-chart-cron", cache)
        self.assertTrue(cron_log, "cron refresh should have walked Airtable")
        _body, read_log = self._read("/data/airtable/component/warmkey-chart-cron/chart", cache)
        self.assertEqual(read_log, [], "reader re-walked Airtable — the sweep warmed the wrong key")

    def test_table_personalize_on_still_served_from_the_warmed_entry(self) -> None:
        # Regression guard (W4): the Table fingerprint is UNCHANGED by this
        # fix — its personalize fields genuinely describe its cached bytes,
        # and it must keep matching exactly as before.
        self._seed_widget(
            "warmkey-table-on",
            extra_data={"personalizeColumn": "OwnerEmail", "personalizeEnabled": True},
        )
        cache = FakeCacheService()
        warm_log = self._save_config("warmkey-table-on", cache, {"personalizeEnabled": True})
        self.assertTrue(warm_log, "warm-on-save should have walked Airtable")
        _body, read_log = self._read("/data/airtable/component/warmkey-table-on/rows", cache)
        self.assertEqual(read_log, [], "reader re-walked Airtable — the Table fingerprint regressed")

    def test_shared_fingerprint_builder_matches_every_read_sites_shape(self) -> None:
        # L1, now spanning five callers: unit-level proof that
        # _widget_cache_fingerprint(widget_type=...) produces the same
        # shape each read site built independently before this refactor,
        # AND that a Metric/Chart widget's real personalize settings are
        # ignored (that ignoring IS the fix) while a Table widget's are
        # honored directly.
        from app.services.airtable_service import AirtableService

        link, url = "fp-widget", FAKE_URL
        filters = [{"field": "Status", "operator": "is", "value": "Active"}]

        chart_via_shared = AirtableService._widget_cache_fingerprint(
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=None,
            filters=filters,
            personalize_enabled=False,
            personalize_column=None,
        )
        self.assertEqual(
            chart_via_shared,
            AirtableService._chart_cache_fingerprint(link=link, url=url, filters=filters),
        )

        metric_via_shared = AirtableService._widget_cache_fingerprint(
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=None,
            filters=filters,
            personalize_enabled=False,
            personalize_column=None,
        )
        self.assertEqual(chart_via_shared, metric_via_shared, "Metric and Chart share one pinned shape")

        # A Metric/Chart widget's real (non-empty) personalize settings and
        # projection must be ignored — this is the substance of the fix.
        personalized = AirtableService._widget_cache_fingerprint(
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=["Name"],
            filters=filters,
            personalize_enabled=True,
            personalize_column="OwnerEmail",
        )
        self.assertEqual(personalized, chart_via_shared)

        # A Table widget's real settings, in contrast, are honored as-is.
        table_fp = AirtableService._widget_cache_fingerprint(
            widget_type=AIRTABLE_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=["Name"],
            filters=filters,
            personalize_enabled=True,
            personalize_column="OwnerEmail",
        )
        self.assertEqual(
            table_fp,
            {
                "link": link,
                "sourceUrl": url,
                "selectedColumns": ["Name"],
                "filters": filters,
                "personalizeEnabled": True,
                "personalizeColumn": "OwnerEmail",
            },
        )

    def test_warm_never_projects_the_walk_for_a_chart_even_with_a_stored_selectedColumns(
        self,
    ) -> None:
        # W1: the key and the WALK must be normalized together. A Chart
        # widget's data blob has no admin-facing column picker today, but
        # this proves the service doesn't silently trust one if present —
        # if only the fingerprint were pinned while the walk still
        # projected to `selectedColumns`, the cached rows would be missing
        # the group field entirely, and every row would group into
        # "(Empty)" instead of "East"/"West".
        self._seed_widget(
            "warmkey-chart-projected",
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            extra_data={
                "groupField": "Region",
                "aggregation": "count",
                "selectedColumns": ["OwnerEmail"],
            },
        )
        cache = FakeCacheService()
        self._save_config("warmkey-chart-projected", cache, {"personalizeEnabled": False})
        body, _read_log = self._read(
            "/data/airtable/component/warmkey-chart-projected/chart", cache
        )
        self.assertEqual(
            {g["label"]: g["value"] for g in body["groups"]},
            {"East": 2, "West": 1},
            "Region was stripped from the warmed rows — the walk was projected while the key was not",
        )


class AirtableConfigAccessControlTests(_AirtableCacheFixture):
    """update_airtable_component_config is the only place an airtable-like
    widget's access_control can be written (the canvas save protects the
    field — §0.2's protected-field widening to the chart widget type, L2).

    It used to validate the incoming value against the gridstack's resolved
    AC ceiling (§4.2, plan_airtable_chart_widget_2026-08-13.md). That
    ceiling came from the access-control propagation engine, which has been
    removed ahead of a new algorithm — there is no inherited value to
    compare against any more, so the two tests below pin the replacement
    behaviour: whatever is sent is stored verbatim."""

    def test_access_control_is_stored_as_given(self) -> None:
        self._seed_widget("ac-ok-widget", widget_type=AIRTABLE_CHART_WIDGET_TYPE)
        narrower_ac = {
            "admins": {
                "roles": [{"name": "Hub Admin", "scope": "", "program": "", "function": ""}],
                "users": [],
            },
            "viewers": {"roles": [], "users": [{"email": "allowed@example.com"}]},
        }
        result = update_airtable_component_config(self.db, "ac-ok-widget", access_control=narrower_ac)
        self.assertIsNotNone(result)
        self.assertEqual(result["access_control"], narrower_ac)

    def test_a_value_the_old_ceiling_would_have_rejected_is_now_accepted(self) -> None:
        """Explicitly pins the behaviour change: this exact payload used to
        400 (it named a principal the gridstack's resolved ceiling did not
        permit). With no ceiling, it is an ordinary write."""
        self._seed_widget("ac-http-widget", widget_type=AIRTABLE_CHART_WIDGET_TYPE)
        wider_ac = {
            "admins": {"roles": [], "users": [{"email": "attacker@example.com"}]},
            "viewers": {"roles": [], "users": []},
        }
        # Hub Admin + a live session — plan_lock_propagation_2026-09-08.md
        # §5.3 (see WarmKeyAlignmentTests._save_config's own comment).
        auth = self._auth("someone@example.com", roles=["Hub Admin"])
        lock_resp = self.client.put(f"/v2/tabs/{self.root['documentId']}/lock", json={}, headers=auth)
        token = lock_resp.json()["data"]["lock_token"]
        resp = self.client.put(
            "/data/airtable/component/ac-http-widget/config",
            json={"access_control": wider_ac},
            headers={**auth, "X-Edit-Tokens": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["access_control"], wider_ac)

    def test_chart_widget_pat_sourceurl_and_ac_survive_canvas_save_protection(self) -> None:
        self._seed_widget("chart-protect-widget", widget_type=AIRTABLE_CHART_WIDGET_TYPE)
        component = (
            self.db.query(ComponentV2).filter(ComponentV2.link == "chart-protect-widget").first()
        )
        self.assertIsNotNone(component)

        malicious_content = {
            "schemaVersion": 2,
            "layout": [
                {
                    "id": str(component.id),
                    "x": 0, "y": 0, "w": 3, "h": 3,
                    "type": AIRTABLE_CHART_WIDGET_TYPE,
                }
            ],
            "widgets": {
                str(component.id): {
                    "type": AIRTABLE_CHART_WIDGET_TYPE,
                    "access_control": {
                        "admins": {"roles": [], "users": [{"email": "attacker@example.com"}]},
                        "viewers": {"roles": [], "users": []},
                    },
                    "data": {
                        "sourceUrl": "https://airtable.com/appATTACKER0000/tblATTACKER0000",
                        "pat": "stolen-pat-value",
                        "groupField": "Region",
                        "aggregation": "count",
                    },
                }
            },
        }
        result = update_tab_content_v2(self.db, self.root["documentId"], malicious_content)
        self.assertIsNotNone(result)

        from app.services.gridstack_service import (
            get_airtable_component_config,
            get_airtable_pat_for_component,
        )

        self.assertEqual(
            get_airtable_pat_for_component(self.db, "chart-protect-widget"),
            "fake-pat",
            "the unauthenticated canvas save must not be able to overwrite the PAT",
        )
        config = get_airtable_component_config(self.db, "chart-protect-widget")
        self.assertEqual(config["sourceUrl"], FAKE_URL)
        self.assertIsNone(
            config["access_control"],
            "the unauthenticated canvas save must not be able to set access_control either",
        )

    def test_chart_widget_link_is_discoverable_for_cache_refresh(self) -> None:
        from app.services.gridstack_service import list_airtable_component_links

        self._seed_widget("chart-discovery-widget", widget_type=AIRTABLE_CHART_WIDGET_TYPE)
        links = list_airtable_component_links(self.db)
        self.assertIn("chart-discovery-widget", links)


if __name__ == "__main__":
    unittest.main()
