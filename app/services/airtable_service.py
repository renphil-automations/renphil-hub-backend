"""
Airtable service.

Connects to Airtable bases via the ``pyairtable`` library and exposes
aggregation helpers used by the analytics router.  ``pyairtable`` is
synchronous (built on ``requests``), so calls are dispatched to a
worker thread to remain compatible with FastAPI's async event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from pyairtable import Api
from pyairtable import retry_strategy as _pyairtable_retry_strategy
from requests.exceptions import RequestException
from fastapi import HTTPException, status as _http_status

from app.config import Settings, get_settings
from app.helpers import airtable_formulas as af
from app.helpers import airtable_personalize as ap
from app.helpers.exceptions import AirtableError
from app.services.cache_service import CacheService, encoded_length, get_cache_service
from app.services.gridstack_service import (
    AIRTABLE_CHART_WIDGET_TYPE,
    AIRTABLE_METRIC_WIDGET_TYPE,
    AIRTABLE_WIDGET_TYPE,
)
from app.models.airtable import (
    AccessControlAssign,
    AccessControlRecord,
    AccessControlRevoke,
    ActiveProgramItem,
    AirtableUserIdResponse,
    AmountSumResponse,
    AnnouncementCreate,
    AnnouncementRecord,
    AnnouncementUpdate,
    TicketCreate,
    TicketRecord,
    TicketUpdate,
    FeedbackCreate,
    FeedbackRecord,
    SlackTicketWebhookPayload,
    EmailTicketWebhookPayload,
    CheckinReportingPeriodRecord,
    ClusterRecord,
    AwardedOpportunityRecord,
    CountResponse,
    DateRangeFilter,
    DistributionItem,
    DistributionResponse,
    DocTitleRecord,
    FundersRecord,
    GlossaryRecord,
    IdNameItem,
    MasterListFundsAndSubprogramsRecord,
    MonthlyCheckinRecord,
    OnboardingChecklistRecord,
    OrgFriendsRecord,
    GrantAppResourceRecord,
    GrantAppResourceCreate,
    GrantAppResourceUpdate,
    PersonContactItem,
    FinanceLinkRecord,
    FinanceLinkUpdate,
    GoogleDocsTabRecord,
    OfficeSpaceCreate,
    OfficeSpaceRecord,
    OfficeSpaceUpdate,
    Permission,
    Role,
    RoleUpdate,
    RoleCreate,
    ShareableDocsRecord,
    OppRecTypeAmountItem,
    OppRecTypeAmountResponse,
    UniqueAccountsResponse,
    UserRecord,
    UserUpdate,
    YearlyAmountItem,
    YearlyAmountResponse,
    MeetingCadenceRecord,
    UsefulLinkRecord,
    HrAndBenefitsRecord,
    OnboardingLinkRecord,
    OnboardingCallRecord,
    QuickLinkRecord,
    QuickLinkCreate,
    QuickLinkUpdate,
    QuickActionRecord,
    QuickActionCreate,
    QuickActionUpdate,
    GeneralFundraisingResourceRecord,
    PartnershipsLinkRecord,
    PartnershipsLinkUpdate,
    PartnershipsLinkCreate,
    PolicyLinkRecord,
    PolicyLinkCreate,
    PolicyLinkUpdate,
    EventsQuickLinkRecord,
    EventsQuickLinkCreate,
    EventsQuickLinkUpdate,
    FinanceQuickLinkRecord,
    FinanceQuickLinkCreate,
    FinanceQuickLinkUpdate,
    CommsQuickLinkRecord,
    CommsQuickLinkCreate,
    CommsQuickLinkUpdate,
    HrQuickLinkRecord,
    HrQuickLinkCreate,
    HrQuickLinkUpdate,
    RenphilDueDiligenceLinkRecord,
    RenphilDueDiligenceLinkCreate,
    RenphilDueDiligenceLinkUpdate,
    BoardMemberRecord,
    BoardMemberCreate,
    BoardMemberUpdate,
    OrganizationInfoRecord,
    OrganizationInfoCreate,
    OrganizationInfoUpdate,
)

logger = logging.getLogger(__name__)

# All Airtable field names are loaded strictly from the environment via
# ``app.config.Settings``. The module-level constants below are simple
# aliases over the loaded settings so that downstream code can keep
# referring to them by their short identifier.
_S = get_settings()

# Field name constants for the Total Moved & Deployed table
_F_AMOUNT = _S.AT_F_AMOUNT
_F_FISCAL_YEAR = _S.AT_F_FISCAL_YEAR
_F_OPP_REC_TYPE = _S.AT_F_OPP_REC_TYPE
_F_ACCOUNT_NAME = _S.AT_F_ACCOUNT_NAME

# Field name constants for the Fund & Program Tracker base
_F_EXCLUDE_FROM_LISTS = _S.AT_F_EXCLUDE_FROM_LISTS
_F_EXCLUDE_FROM_REPORTING = _S.AT_F_EXCLUDE_FROM_REPORTING
_F_STATUS = _S.AT_F_STATUS
_F_SUB_TRACK_OF = _S.AT_F_SUB_TRACK_OF
_F_SHARE_PUBLICLY = _S.AT_F_SHARE_PUBLICLY
_F_ONBOARDING_STATUS = _S.AT_F_ONBOARDING_STATUS
_ONBOARDING_STATUS_VETTING = "Vetting"
_F_ADD_TO_SHAREABLE_DOC = _S.AT_F_ADD_TO_SHAREABLE_DOC
_F_NAME = _S.AT_F_NAME
_F_SCOPING_PROP_OVERVIEW = _S.AT_F_SCOPING_PROP_OVERVIEW
_F_INITIATIVE_TYPE = _S.AT_F_INITIATIVE_TYPE
_F_FOCUS_AREAS = _S.AT_F_FOCUS_AREAS
_F_PROGRAM_LEAD_FELLOW = _S.AT_F_PROGRAM_LEAD_FELLOW
_STATUS_ACTIVE_PROGRAM = "Active Program"
_STATUS_PUBLICLY_LAUNCHED = "Publicly Launched"
_STATUS_FELLOWSHIP_SCOPING = "Fellowship (Scoping)"
_ACTIVE_PROGRAM_STATUSES = (_STATUS_ACTIVE_PROGRAM, _STATUS_PUBLICLY_LAUNCHED)

_F_DAYS_UNTIL_DEADLINE = _S.AT_F_DAYS_UNTIL_DEADLINE
_F_SUBMISSION_EXTENSION = _S.AT_F_SUBMISSION_EXTENSION
_F_REPORTING_LEAD = _S.AT_F_REPORTING_LEAD
_F_REPORT_COMPLETE = _S.AT_F_REPORT_COMPLETE
_F_FLAG_FOR_DISCUSSION = _S.AT_F_FLAG_FOR_DISCUSSION
_F_PROGRAM_NAME = _S.AT_F_PROGRAM_NAME
_F_CHECKIN_HISTORY = _S.AT_F_CHECKIN_HISTORY
_F_CHECKIN_REPORTING_PERIOD = _S.AT_F_CHECKIN_REPORTING_PERIOD
_F_CLUSTER = _S.AT_F_CLUSTER
_F_CLUSTER_NAME = _S.AT_F_CLUSTER_NAME
_F_DASHBOARD_DISPLAY = _S.AT_F_DASHBOARD_DISPLAY
_F_FOLLOWUP_INDICATED = _S.AT_F_FOLLOWUP_INDICATED
_F_DEADLINE = _S.AT_F_DEADLINE
_F_REVIEW_UNTIL = _S.AT_F_REVIEW_UNTIL
_F_PERIOD = _S.AT_F_PERIOD
_F_OC_MASTER_LIST_FUNDS_SUBPROGRAMS = _S.AT_F_OC_MASTER_LIST_FUNDS_SUBPROGRAMS

# Awarded Opportunities → Master List lookup enrichment
_ML_LOOKUP_FIELD = "Master List of Funds & Sub-Programs (from Linked Gift Designation)"
_ML_LOOKUP_PROJECT_FIELDS = [
    "Name",
    "Initiative Type",
    "Focus Area(s)",
    "Program Lead/Fellow",
    "Status",
    "Program Summary",
    "Internal Notes",
    "Can we talk about it publicly",
    "Last Updated",
    "Scoping Proposal / Fund Overview",
    "Summary Document / Concept Note",
    "Website",
    "Check-In History",
    "Technical Document / Deep Dive",
]

# Master List → Deliverables lookup enrichment. The "Upcoming Deliverables"
# lookup field carries raw linked Deliverables record ids (its source is a
# linked-record field on the Awarded Opportunities table). They are resolved
# server-side into the full Deliverables records.
_UPCOMING_DELIVERABLES_FIELD = "Upcoming Deliverables"

# `Api(api_key)` with no `retry_strategy=` already wraps its session in a
# urllib3 Retry (pyairtable's own default — `retrying.retry_strategy()`,
# confirmed by reading pyairtable/api/retrying.py, not assumed): up to 5
# retries with exponential backoff, honoring a `Retry-After` header, for
# BOTH connection-level errors (timeout/reset — urllib3's `connect`/`read`
# sub-budgets fall back to `total` when left unset) AND status 429. So the
# "429 retry-with-backoff" finding in
# plan_airtable_cache_scaling_2026-08-08.md was already half-solved by the
# library's own default. The one real gap: `status_forcelist` defaults to
# `(429,)` only — a single transient Airtable 5xx aborts the whole walk and
# discards every page fetched so far, with zero retry. Widening the
# forcelist closes that gap without hand-rolling a retry loop.
#
# A `Retry` instance is an immutable template (`.increment()` returns a NEW
# instance rather than mutating `self`), so one module-level object is safe
# to share across every `Api(...)` construction and concurrent request.
_WIDGET_RETRY_STRATEGY = _pyairtable_retry_strategy(
    status_forcelist=(429, 500, 502, 503, 504)
)

# ── Field-type hints for the viewer Filter/Sort/Group/Search toolbar ───────
# (plan_airtable_widget_viewer_controls_2026-08-12.md §2.2). Deliberately
# narrow: a field type not in this map renders as plain text, same as today.
#
# `multipleRecordLinks` and `multipleLookupValues` are EXCLUDED on purpose. A
# linked record's raw row value is an array of unresolved `recXXXXXXXXXXXXXX`
# ids — bubbling those looks more broken than today's plain text, and this
# codebase's own `Tabs.tsx` already hides raw record ids outright
# (`isAirtableRecordId`). A lookup field's resolved type is unpredictable
# (text, number, or another link). Both keep plain-text rendering.
_FIELD_TYPE_HINTS: dict[str, str] = {
    "url": "url",
    "button": "url",
    "singleSelect": "select",
    "multipleSelects": "select",
    "multipleCollaborators": "select",
}


class AirtableService:
    """Async-friendly wrapper around ``pyairtable``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api = Api(settings.AIRTABLE_API_KEY)

    # ── low-level helpers ──────────────────────────────────────────────
    def _fundraising_table(self):
        return self._api.table(
            self._settings.AIRTABLE_FUNDRAISING_BASE_ID,
            self._settings.TOTAL_MOVED_AND_DEPLOYED_TABLE_NAME,
        )

    def _admins_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ADMINS_TABLE,
        )

    # ── Generic read-only preview (dashboard Airtable widget) ───────────
    _PREVIEW_MAX_RECORDS = 100

    @staticmethod
    def _parse_airtable_share_url(url: str) -> tuple[str, str, str | None]:
        """Extract (base_id, table_id, view_id) from an Airtable share URL.

        Accepts URLs like:
          https://airtable.com/appXXXXXXXXXXXXXX/tblXXXXXXXXXXXXXX/viwXXXXXXXXXXXXXX
          https://airtable.com/appXXXXXXXXXXXXXX/tblXXXXXXXXXXXXXX
        Raises AirtableError (400) if a base id and table id can't both be found.
        """
        base_match = re.search(r"app[A-Za-z0-9]{14,}", url or "")
        table_match = re.search(r"tbl[A-Za-z0-9]{14,}", url or "")
        view_match = re.search(r"viw[A-Za-z0-9]{14,}", url or "")

        if not base_match or not table_match:
            raise AirtableError(
                "Could not find an Airtable base id (app...) and table id "
                "(tbl...) in the provided URL."
            )

        return base_match.group(0), table_match.group(0), (
            view_match.group(0) if view_match else None
        )

    _WIDGET_PAGE_SIZE = 100

    async def fetch_widget_rows(
        self,
        *,
        url: str,
        api_key: str,
        caller_email: str,
        selected_columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
        cursor: str | None = None,
    ):
        """One page of rows for a dashboard Airtable widget, filtered
        server-side under the app's own identity.

        The caller supplies only a page cursor — never a formula, columns,
        URL or token. Everything else comes from the widget's stored config
        and the caller's authenticated email, so a viewer cannot widen what
        they are shown.

        FAILS CLOSED: if personalization is enabled but could not be applied
        (no email, no column, unusable column name), this returns an EMPTY
        page rather than the unfiltered table. See
        `airtable_formulas.widget_formula`'s `allowed` flag.
        """
        from app.models.airtable import AirtableWidgetRowsResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        formula, allowed = af.widget_formula(
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        )

        if not allowed:
            logger.warning(
                "Airtable widget fetch refused: personalization enabled but not "
                "applicable (base=%s table=%s) — returning no rows",
                base_id,
                table_id,
            )
            return AirtableWidgetRowsResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                fields=list(selected_columns or []),
                rows=[],
                next_cursor=None,
                personalize_blocked=True,
            )

        options: dict[str, Any] = {"page_size": self._WIDGET_PAGE_SIZE}
        if view_id:
            options["view"] = view_id
        if selected_columns:
            options["fields"] = list(selected_columns)
        if formula:
            options["formula"] = formula
        if cursor:
            options["offset"] = cursor

        # `table.all()` / `.iterate()` both swallow the response's `offset`,
        # so neither can hand back a next-page cursor. Drop to the raw
        # request, which returns `records` and `offset` together.
        api = Api(api_key.strip(), retry_strategy=_WIDGET_RETRY_STRATEGY)
        table = api.table(base_id, table_id)
        try:
            payload = await asyncio.to_thread(
                api.request, "get", table.urls.records, options=options
            )
        except RequestException as exc:
            logger.error("Airtable widget row fetch failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during widget row fetch")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        records = payload.get("records", []) or []
        rows: list[dict[str, Any]] = []
        seen_fields: list[str] = []
        seen: set[str] = set()
        for record in records:
            record_fields = record.get("fields", {}) or {}
            for key in record_fields:
                if key not in seen:
                    seen.add(key)
                    seen_fields.append(key)
            rows.append({"id": record.get("id"), **record_fields})

        # Prefer the admin's stored column selection over what this page
        # happened to contain: deriving `fields` from the returned records
        # makes headers appear and disappear between pages whenever a column
        # is empty on one page and populated on another.
        fields = list(selected_columns) if selected_columns else seen_fields

        return AirtableWidgetRowsResponse(
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            fields=fields,
            rows=rows,
            next_cursor=payload.get("offset"),
            personalize_blocked=False,
        )

    # ── Widget row cache (plan_airtable_widget_caching_2026-08-06.md) ───
    #
    # `fetch_widget_rows` above stays completely unmodified — it is both the
    # oversized-table fallback (§5.4) and the implementation the preview
    # endpoint keeps using. Everything below is additive.

    # ~4 req/sec per base — headroom under Airtable's documented 5/sec limit.
    # Pages are inherently sequential (each offset is only known after the
    # previous response), so this is a plain minimum-interval throttle.
    _WALK_THROTTLE_SECONDS = 0.25

    # Once the walk's cheap RAW running total first crosses AIRTABLE_CACHE_
    # MAX_BYTES, a real gzip measurement (finding #4) is re-checked every
    # this many additional RAW bytes of growth — not on every single page.
    # Re-compressing the WHOLE accumulated row list is the accurate check,
    # but doing it every page would reintroduce the exact quadratic cost
    # §4.1 removed (gzip is more CPU-expensive per byte than json.dumps).
    # Bounded instead: for the benchmarked 8-column shape, a full 50,000-row
    # walk (~13 MB raw) only ever ESCALATES past the original 5 MB raw cap,
    # so this interval is paid at most roughly (13MB-5MB)/interval times —
    # about a dozen checks, a few hundred ms total, against a walk that
    # already spends 100+ s in throttled Airtable I/O either way.
    _COMPRESSED_SIZE_CHECK_RAW_INTERVAL_BYTES = 500_000
    _IDX_CURSOR_RE = re.compile(r"^idx:(\d+)$")

    @classmethod
    def _parse_synthetic_cursor(cls, cursor: str | None) -> int:
        """Parse an `idx:<offset>` cursor into a start index. Anything that
        doesn't match that exact shape — no cursor, or a real Airtable
        offset a browser held across the deploy that introduced this cache —
        restarts at index 0 rather than erroring (plan §5.3)."""
        if not cursor:
            return 0
        match = cls._IDX_CURSOR_RE.fullmatch(cursor)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _make_synthetic_cursor(next_index: int) -> str:
        return f"idx:{next_index}"

    @classmethod
    def _live_cursor(cls, cursor: str | None) -> str | None:
        """Translate a cursor for the LIVE (uncached) path. A synthetic
        `idx:` cursor means nothing to the real Airtable API, so it is
        dropped to `None` (restart at page 1); any other value — including a
        genuine Airtable offset this very fallback returned on a previous
        page — is passed through untouched."""
        return None if (cursor and cls._IDX_CURSOR_RE.fullmatch(cursor)) else cursor

    async def _walk_full_table(
        self,
        *,
        api: Api,
        table: Any,
        view_id: str | None,
        fetch_fields: list[str] | None,
        formula: str | None,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """Walk every page of `table` matching `formula`, feeding each
        response's `offset` back as the next page's request, throttled to
        `_WALK_THROTTLE_SECONDS`. Reuses the given `api`/`table` across every
        page rather than rebuilding per page (plan landmine L7).

        Returns `(rows, seen_fields, oversized)`. `oversized=True` means the
        walk was aborted after crossing `AIRTABLE_CACHE_MAX_ROWS` or
        `AIRTABLE_CACHE_MAX_BYTES` — the caller must NOT cache a partial
        result in that case (plan §5.4: never silently truncate).

        `AIRTABLE_CACHE_MAX_BYTES` is checked against the REAL, compressed
        on-the-wire size (finding #4) — not the raw JSON size, which is
        ~4.8x larger and made the guard ~9x more conservative than the
        Upstash limit it protects (a 15,000-row table was rejected purely
        by this counter while sitting inside every real limit). See
        `_oversized_by_compressed_size` below for how that's kept cheap.
        """
        max_rows = self._settings.AIRTABLE_CACHE_MAX_ROWS
        max_bytes = self._settings.AIRTABLE_CACHE_MAX_BYTES

        options: dict[str, Any] = {"page_size": self._WIDGET_PAGE_SIZE}
        if view_id:
            options["view"] = view_id
        if fetch_fields:
            options["fields"] = list(fetch_fields)
        if formula:
            options["formula"] = formula

        rows: list[dict[str, Any]] = []
        seen_fields: list[str] = []
        seen: set[str] = set()
        last_request_at: float | None = None
        # Running length of the encoded row list's BODY — everything between
        # the outer `[` and `]`. Kept incrementally because re-encoding the
        # whole accumulated list once per page is quadratic: ~1.8 s of pure
        # waste at 150 pages, ~22 s at 500 (plan §4.1). Still the byte-for-
        # byte RAW json.dumps length — used as the cheap trigger for WHEN to
        # pay for a real compressed measurement below, not as the guard's
        # threshold itself anymore.
        body_len = 0
        raw_total = 0
        # RAW total at the last REAL (gzip) size check, or None before the
        # first one. Compression can only shrink further (compressed <=
        # raw), so below max_bytes on the raw total alone already proves
        # the compressed size is fine too — this stays None for every
        # table that never crosses the raw cap, which is the common case
        # (both live widgets today are 3 rows) and costs nothing extra.
        last_compressed_check_raw: int | None = None

        def _oversized_by_compressed_size(*, force: bool) -> bool:
            """True if `rows`' REAL encoded size exceeds `max_bytes`. Only
            actually re-compresses when `force` is set or the raw total has
            grown by `_COMPRESSED_SIZE_CHECK_RAW_INTERVAL_BYTES` since the
            last real check — re-compressing the WHOLE accumulated list on
            every single page (gzip is more CPU-expensive per byte than
            json.dumps) would reintroduce the exact quadratic cost §4.1
            removed. `force=True` is used once, after the walk's last page,
            to guarantee the FINAL verdict is always a real measurement —
            never a stale one left over from an earlier checkpoint."""
            nonlocal last_compressed_check_raw
            due = (
                last_compressed_check_raw is None
                or raw_total - last_compressed_check_raw
                >= self._COMPRESSED_SIZE_CHECK_RAW_INTERVAL_BYTES
            )
            if not (force or due):
                return False
            last_compressed_check_raw = raw_total
            return encoded_length(rows, compress=True) > max_bytes

        while True:
            if last_request_at is not None:
                remaining = self._WALK_THROTTLE_SECONDS - (
                    time.monotonic() - last_request_at
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
            last_request_at = time.monotonic()

            try:
                payload = await asyncio.to_thread(
                    api.request, "get", table.urls.records, options=options
                )
            except RequestException as exc:
                logger.error("Airtable widget cache walk failed: %s", exc)
                raise AirtableError(f"Airtable API error: {exc}") from exc
            except Exception as exc:
                logger.exception("Unexpected Airtable error during widget cache walk")
                raise AirtableError(f"Airtable API error: {exc}") from exc

            page_rows: list[dict[str, Any]] = []
            for record in payload.get("records", []) or []:
                record_fields = record.get("fields", {}) or {}
                for key in record_fields:
                    if key not in seen:
                        seen.add(key)
                        seen_fields.append(key)
                page_rows.append({"id": record.get("id"), **record_fields})

            rows.extend(page_rows)

            if len(rows) > max_rows:
                return rows, seen_fields, True

            if page_rows:
                # Encode ONLY the page just appended. `json.dumps` of a list
                # is `[` + body + `]`, so dropping 2 chars leaves this page's
                # body, and joining it to a non-empty accumulation costs
                # exactly one comma.
                page_body_len = (
                    len(json.dumps(page_rows, separators=(",", ":"), default=str)) - 2
                )
                body_len += page_body_len if body_len == 0 else 1 + page_body_len
            raw_total = body_len + 2

            if raw_total > max_bytes and _oversized_by_compressed_size(force=False):
                return rows, seen_fields, True

            next_offset = payload.get("offset")
            if not next_offset:
                break
            options["offset"] = next_offset

        # The walk finished normally. If it ever escalated past the raw
        # cap, the last page's growth may not have landed on a checkpoint —
        # force one final real measurement so the returned `oversized=False`
        # is never a stale verdict from an earlier, smaller checkpoint.
        if raw_total > max_bytes and _oversized_by_compressed_size(force=True):
            return rows, seen_fields, True

        return rows, seen_fields, False

    async def _build_widget_cache_envelope(
        self,
        *,
        cache: CacheService,
        url: str,
        api_key: str,
        selected_columns: list[str] | None,
        filters: list[dict[str, Any]] | None,
        personalize_enabled: bool,
        personalize_column: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Walk the widget's FULL, unpersonalized (filters-only) row set and
        shape it into the envelope stored in the cache. Returns
        `(envelope, status)`, `status` one of:

          * ``"ok"``       — `envelope` is the built payload;
          * ``"oversized"`` — `envelope` is None, table crossed the cap;
          * ``"locked"``   — `envelope` is None, the walk was never even
            attempted because another widget on the SAME base is already
            walking (see the base-level lock below). Distinct from
            `"oversized"` so callers don't mistake contention for a
            confirmed bad table and negative-cache it.
        """
        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        # Personalize is NEVER baked into the cached formula — only the
        # stored filters are (plan §6.1). Same call shape as
        # `preview_widget_config`'s own filters-only formula build.
        formula, _ = af.widget_formula(filters=filters)

        fetch_fields = list(selected_columns) if selected_columns else None
        if (
            personalize_enabled
            and personalize_column
            and fetch_fields is not None
            and personalize_column not in fetch_fields
        ):
            # The projection trap (plan §6.3): Python needs the personalize
            # column's VALUE to match against even when the admin never
            # selected it for display. Appended to the Airtable request
            # only — `fields` below stays exactly what was fetched, and the
            # serving step (`fetch_widget_rows_cached`) is what actually
            # keeps it out of what a client sees.
            fetch_fields = fetch_fields + [personalize_column]

        api = Api(api_key.strip(), retry_strategy=_WIDGET_RETRY_STRATEGY)
        table = api.table(base_id, table_id)

        # Second, base-scoped lock (finding #6,
        # plan_airtable_cache_scaling_2026-08-08.md §3.4.2/handoff
        # 2026-08-10 §3): the per-fingerprint lock the CALLER already holds
        # only stops the SAME widget from being walked twice at once. Two
        # DIFFERENT widgets sharing a base — e.g. a reader's cold-cache warm
        # on widget A racing the cron's refresh of widget B — each hold
        # their OWN fingerprint lock and would happily walk concurrently,
        # together exceeding Airtable's 5 req/s/base limit (each walk alone
        # already uses ~4 req/s by design, `_WALK_THROTTLE_SECONDS`).
        # Scoped tightly around just the walk, not formula-building — the
        # only part that actually calls Airtable. Non-blocking: unlike the
        # fingerprint lock's read-path caller, there is nothing to poll for
        # here (a different widget's walk will never populate THIS
        # fingerprint's cache key), so a loser returns immediately.
        base_lock_key = f"airtable:{cache.version}:widget_base_lock:{base_id}"
        base_lock_token = await cache.acquire_lock(
            base_lock_key, ttl_seconds=self._settings.AIRTABLE_CACHE_REFRESH_LOCK_SECONDS
        )
        if not base_lock_token:
            return None, "locked"

        try:
            rows, seen_fields, oversized = await self._walk_full_table(
                api=api, table=table, view_id=view_id, fetch_fields=fetch_fields, formula=formula,
            )
        finally:
            await cache.release_lock(base_lock_key, base_lock_token)

        if oversized:
            return None, "oversized"

        envelope = {
            "endpoint": "widget_rows",
            "base_id": base_id,
            "table_id": table_id,
            "view_id": view_id,
            # Exactly what was fetched — may still include an
            # auto-appended personalize column. NOT the client-facing field
            # list; `fetch_widget_rows_cached` derives that from the
            # caller's own `selected_columns`, which is never wider than
            # this.
            "fields": fetch_fields if fetch_fields is not None else seen_fields,
            "rows": rows,
            "row_count": len(rows),
            "last_updated_date": datetime.now(timezone.utc).isoformat(),
        }
        return envelope, "ok"

    # Suffix for the negative-cache marker written when a warm attempt
    # confirms a widget is oversized or its walk fails outright. Lives at a
    # DIFFERENT key from the envelope itself — never overwrites a good
    # envelope, and a stale marker left behind after a later success is
    # simply never consulted again (the envelope check above it always
    # short-circuits first).
    _NEGATIVE_CACHE_SUFFIX = ":negative"

    async def _mark_walk_unwarmable(
        self,
        cache: CacheService,
        cache_key: str,
        *,
        reason: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Records that `cache_key` was just confirmed oversized or failing,
        so the NEXT request in the next `ttl_seconds` skips straight to the
        live fallback instead of repeating the same full walk only to
        rediscover the same outcome (finding #3).

        `ttl_seconds` defaults to `AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS` — the
        read path's short, "recover within a request or two" window. The
        scheduled refresh (`warm_widget_cache`) passes a much longer TTL
        explicitly: it ticks on its own fixed cadence regardless, so a short
        marker would always have lapsed by the next tick and never actually
        stop a persistently-bad widget from being re-walked to the cap
        every single time (see `AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS`
        in config.py). Both paths write the SAME key, so whichever's TTL is
        currently active covers both — a reader arriving while the cron's
        longer marker is live benefits from it too, for free.
        """
        await cache.set(
            f"{cache_key}{self._NEGATIVE_CACHE_SUFFIX}",
            {"reason": reason, "checked_at": datetime.now(timezone.utc).isoformat()},
            ttl_seconds=(
                ttl_seconds
                if ttl_seconds is not None
                else self._settings.AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS
            ),
        )

    # Suffix for the field-type schema cache's negative marker. Deliberately
    # DISTINCT from `_NEGATIVE_CACHE_SUFFIX` above: that suffix means "this
    # widget's ROW WALK is known-bad" — what `warm_widget_cache` consults to
    # damp the cron, what `_get_or_warm_widget_cache` consults to skip a
    # walk, and what several tests assert on by name
    # (`k.endswith(":negative")`). A schema-scope failure says nothing about
    # the row walk — conflating them would make one 403 on the Metadata API
    # suppress row caching entirely.
    _SCHEMA_NEGATIVE_CACHE_SUFFIX = ":schema_negative"

    async def fetch_table_field_hints(
        self, *, base_id: str, table_id: str, api_key: str
    ) -> dict[str, str]:
        """Best-effort Airtable field-type → render-hint map, cached separately
        from the row cache (schema churns far less than rows).

        Requires the PAT to hold Airtable's `schema.bases:read` scope. ANY
        failure degrades to {} — today's plain-text rendering — never raises,
        never blocks the widget. The admin is told separately
        (`fetch_table_field_hints_with_status`); this method stays silent by
        design.

        No lock: one GET, not a multi-page walk. Deliberately does NOT reuse
        the row cache's stampede lock — a schema fetch is not worth
        serializing, and taking that lock here would let a schema call block
        a row walk.
        """
        hints, _available = await self._fetch_table_field_hints_impl(
            base_id=base_id, table_id=table_id, api_key=api_key
        )
        return hints

    # Known, accepted: the schema cache key is {base_id, table_id} only — not
    # the PAT. Two widgets on the same table share one entry, which is
    # correct (schema is schema). Edge case: if widget A's PAT lacks the
    # scope, the negative marker briefly blocks widget B whose PAT has it.
    # Self-heals in AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS (60s). Not worth
    # keying on the token, which would multiply the entry per widget. Largely
    # moot once the PAT is rotated to hold `schema.bases:read`.
    async def _mark_schema_unavailable(
        self, cache: CacheService, cache_key: str, *, reason: str
    ) -> None:
        await cache.set(
            f"{cache_key}{self._SCHEMA_NEGATIVE_CACHE_SUFFIX}",
            {"reason": reason, "checked_at": datetime.now(timezone.utc).isoformat()},
            ttl_seconds=self._settings.AIRTABLE_CACHE_NEGATIVE_TTL_SECONDS,
        )

    async def fetch_table_field_hints_with_status(
        self, *, base_id: str, table_id: str, api_key: str
    ) -> tuple[dict[str, str], bool]:
        """`fetch_table_field_hints` plus an `available` flag, for the admin
        preview only. A table with no URL/select columns legitimately returns
        ({}, True); a PAT without `schema.bases:read` returns ({}, False). The
        two are indistinguishable from the hints alone, and the Property
        Panel needs to say something different for each.

        Not used by the viewer-facing `/rows` and `/rows/full` paths — a
        viewer can do nothing about a missing scope, and carrying the flag
        there would leak configuration state into the read path for no
        benefit.

        Always checks live against Airtable with THIS `api_key` — never trusts
        the shared `{base_id, table_id}` cache to answer `available`. That
        cache is deliberately PAT-agnostic for the read paths (schema is a
        fact about the table, correctly shared across every widget on it,
        same principle as the row cache) — but `available` answers a
        different, requester-specific question: does THIS token hold
        `schema.bases:read`. Trusting the shared cache for it would let a
        token that has `data.records:read` but NOT `schema.bases:read` ride a
        different, more-privileged token's cached success for up to
        `AIRTABLE_SCHEMA_CACHE_TTL_SECONDS` (6h) — confirmed in review: an
        admin testing a `data.records:read`-only PAT was able to enable the
        Property Panel's viewer-controls toggle this way. Still WRITES the
        shared cache on both success and failure exactly as before, so
        `/rows` and `/rows/full` keep benefiting from it — only the READ side
        (and the `cache.enabled` bypass, low-stakes for a debounced,
        human-triggered preview) is skipped here.
        """
        return await self._fetch_table_field_hints_impl(
            base_id=base_id, table_id=table_id, api_key=api_key, force_live=True,
        )

    async def _fetch_table_field_hints_impl(
        self, *, base_id: str, table_id: str, api_key: str, force_live: bool = False,
    ) -> tuple[dict[str, str], bool]:
        """Shared worker `fetch_table_field_hints` and
        `fetch_table_field_hints_with_status` both project from — one code
        path, so `available` is derived directly from the control flow that
        actually decided it rather than reconstructed afterward from a
        second, redundant cache read.

        `force_live=True` (only `fetch_table_field_hints_with_status` passes
        it) skips every READ shortcut below — the `cache.enabled` bypass, the
        positive-cache hit, and the negative-marker hit — so the function
        always falls through to a live Airtable call using THIS `api_key`.
        The WRITES at the bottom stay unconditional either way.
        """
        cache = get_cache_service()
        cache_key = cache.build_key(
            "widget_schema", {"base_id": base_id, "table_id": table_id}
        )

        if not force_live:
            # Same reasoning as `fetch_widget_rows_cached`'s own guard (:872):
            # with no cache, `get` returns None and `set` is a no-op —
            # INCLUDING the negative marker below. Without this line every
            # /rows request fires a live, undampened schema request, which
            # doubles Airtable calls on exactly the degraded path that can
            # least afford it. A disabled cache is a global infra condition,
            # not something rotating the PAT would fix — `available=True`
            # avoids blaming the token for it. (The admin-preview path
            # doesn't get this bypass — see `force_live`'s own docstring —
            # but that path is one debounced call per human keystroke, not
            # per-viewer traffic, so paying for a live call while the cache
            # is down is cheap here.)
            if not cache.enabled:
                return {}, True

            cached = await cache.get(cache_key)
            if isinstance(cached, dict):
                return cached, True
            if await cache.get(f"{cache_key}{self._SCHEMA_NEGATIVE_CACHE_SUFFIX}") is not None:
                return {}, False

        try:
            api = Api(api_key.strip(), retry_strategy=_WIDGET_RETRY_STRATEGY)
            table_schema = await asyncio.to_thread(
                lambda: api.base(base_id).schema().table(table_id)
            )
        except RequestException as exc:
            # PAT lacks `schema.bases:read` → 403, until the token is rotated.
            logger.warning(
                "Airtable schema fetch failed (base=%s table=%s): %s — no type hints",
                base_id, table_id, exc,
            )
            await self._mark_schema_unavailable(cache, cache_key, reason="fetch_failed")
            return {}, False
        except Exception:
            # `BaseSchema.table(id)` raises a bare KeyError (NOT
            # RequestException) for a stale/renamed table_id — `_find` ends
            # in a dict subscript, pyairtable/models/schema.py:115. Must be
            # caught separately or one bad widget 500s every viewer.
            logger.exception(
                "Unexpected error building Airtable field hints (base=%s table=%s)",
                base_id, table_id,
            )
            await self._mark_schema_unavailable(cache, cache_key, reason="unexpected_error")
            return {}, False

        hints = {
            f.name: hint
            for f in table_schema.fields
            if (hint := _FIELD_TYPE_HINTS.get(f.type))
        }
        await cache.set(
            cache_key,
            hints,
            ttl_seconds=self._settings.AIRTABLE_SCHEMA_CACHE_TTL_SECONDS,
        )
        return hints, True

    async def _get_or_warm_widget_cache(
        self,
        *,
        cache_key: str,
        url: str,
        api_key: str,
        selected_columns: list[str] | None,
        filters: list[dict[str, Any]] | None,
        personalize_enabled: bool,
        personalize_column: str | None,
        allow_warm: bool = True,
    ) -> dict[str, Any] | None:
        """Returns the cached envelope, warming it on a miss. Returns None
        when the table turned out to be oversized (never cached), the walk
        itself failed, or this call gave up waiting on another request's
        warm with nothing to show for it — either way the caller falls back
        to the live, uncached path (`fetch_widget_rows`).

        `allow_warm=False` (plan_airtable_chart_widget_2026-08-13.md §3.4,
        L3) makes this call READ-ONLY: a hit still returns normally, but a
        miss returns None immediately — no negative-marker read, no lock
        acquisition, no walk, no `cache.set`. Used by the Chart widget's
        editor preview, whose fingerprint includes `filters`: an admin
        typing into the filter box would otherwise start a full table walk
        (plus a whole-table Upstash entry) per keystroke, for cache entries
        no viewer will ever read. Keyword-only and defaulted to `True` (L11)
        so every existing caller — `fetch_widget_rows_cached`,
        `fetch_widget_full_rows_cached`, `fetch_widget_metric_cached` — is
        unaffected.
        """
        cache = get_cache_service()

        cached = await cache.get(cache_key)
        if isinstance(cached, dict) and "rows" in cached:
            return cached

        if not allow_warm:
            return None

        # A widget recently confirmed oversized or failing — skip the walk
        # (and the lock contention around it) entirely rather than paying
        # for the same negative result again (finding #3).
        if await cache.get(f"{cache_key}{self._NEGATIVE_CACHE_SUFFIX}") is not None:
            return None

        lock_key = f"{cache_key}:lock"
        # Shares the refresh path's TTL: this call does the exact same
        # full-table walk, so it needs a lock that can outlive the walk too
        # (finding #1 — the old hardcoded 120s could not, once
        # AIRTABLE_CACHE_MAX_ROWS was raised).
        lock_token = await cache.acquire_lock(
            lock_key, ttl_seconds=self._settings.AIRTABLE_CACHE_REFRESH_LOCK_SECONDS
        )
        if not lock_token:
            # Someone else is warming this exact key. Wait briefly rather
            # than starting a second concurrent full-table walk — the whole
            # point of the lock (plan §4.4): without it, a cold key plus a
            # burst of viewers would each start their own walk and breach
            # Airtable's per-base rate limit.
            for _ in range(4):
                await asyncio.sleep(1.5)
                cached = await cache.get(cache_key)
                if isinstance(cached, dict) and "rows" in cached:
                    return cached
            return None

        try:
            try:
                envelope, status = await self._build_widget_cache_envelope(
                    cache=cache,
                    url=url,
                    api_key=api_key,
                    selected_columns=selected_columns,
                    filters=filters,
                    personalize_enabled=personalize_enabled,
                    personalize_column=personalize_column,
                )
            except AirtableError:
                # Every OTHER miss-path exit degrades to the live fallback;
                # a walk failure (e.g. an Airtable 429 mid-walk) must too,
                # rather than propagating as a 502 to the caller (finding
                # #2) — the failure rate scales with table size now that a
                # miss makes N Airtable requests instead of 1.
                logger.warning(
                    "Airtable widget cache: walk failed (url=%s) — "
                    "serving live instead",
                    url,
                    exc_info=True,
                )
                await self._mark_walk_unwarmable(cache, cache_key, reason="walk_failed")
                return None
            if status == "locked":
                # A DIFFERENT widget on the same base is walking right now
                # (finding #6) — not a confirmed bad table, just contention,
                # so no negative marker. And unlike the fingerprint-lock
                # wait above, there is nothing to poll for: another widget's
                # walk will never populate THIS cache key. Degrade to live
                # immediately.
                return None
            if status == "oversized":
                logger.warning(
                    "Airtable widget cache: table too large to cache "
                    "(url=%s) — serving live instead, never truncated",
                    url,
                )
                await self._mark_walk_unwarmable(cache, cache_key, reason="oversized")
                return None
            await cache.set(
                cache_key,
                envelope,
                ttl_seconds=self._settings.AIRTABLE_CACHE_TTL_SECONDS,
                # ~4.8x smaller stored AND transferred, on a payload that is
                # by far the largest thing in this cache (§4.3). Opt-in, so
                # the decorator-cached endpoints are untouched.
                compress=True,
            )
            return envelope
        finally:
            await cache.release_lock(lock_key, lock_token)

    @staticmethod
    def _widget_cache_fingerprint(
        *,
        widget_type: str,
        link: str,
        url: str,
        selected_columns: list[str] | None,
        filters: list[dict[str, Any]] | None,
        personalize_enabled: bool,
        personalize_column: str | None,
    ) -> dict[str, Any]:
        """The ONE place a `widget_rows` cache fingerprint is shaped, for
        every widget type that caches one (Table, Metric, Chart alike).
        plan_warm_key_divergence_2026-08-14.md §3.1.

        A Table widget caches a row set projected AND personalized to the
        admin's own settings (`fetch_widget_rows_cached`,
        `fetch_widget_full_rows_cached`), so its fingerprint reflects
        `selected_columns`/`personalize_enabled`/`personalize_column`
        directly — they describe what's actually IN the cached entry.

        A Metric or Chart widget instead caches ONE unprojected,
        unpersonalized row set per widget and applies personalization
        per-viewer in Python afterward (`fetch_widget_metric_cached`,
        `fetch_widget_chart_cached`) — so for those two types this pins
        `selectedColumns: []` / `personalizeEnabled: False` /
        `personalizeColumn: None` regardless of what's passed in,
        because THAT is what actually describes the cached bytes, not
        the widget's own settings.

        Before this method existed, `warm_widget_cache` built its
        fingerprint straight from the widget's stored config with no
        knowledge of widget type — so a Metric/Chart widget with
        personalization on (or merely a stored `personalizeColumn` left
        over from a prior save) warmed a key its own read path never
        looks up: a wasted whole-table Upstash entry, and the widget
        stayed cold until a real viewer's request warmed the RIGHT key
        via `_get_or_warm_widget_cache`. This is the fix.

        A caller passing a Metric/Chart `widget_type` must ALSO
        normalize the values it feeds to the actual walk
        (`_build_widget_cache_envelope`) the same way — seeing
        `warm_widget_cache`'s own normalization is not optional: a
        pinned key over a WALK that still projects columns would cache
        rows missing a field a reader expects (the L2 projection trap,
        in reverse).
        """
        if widget_type in (AIRTABLE_METRIC_WIDGET_TYPE, AIRTABLE_CHART_WIDGET_TYPE):
            selected_columns = None
            personalize_enabled = False
            personalize_column = None
        return {
            "link": link,
            "sourceUrl": url,
            "selectedColumns": list(selected_columns or []),
            "filters": filters or [],
            "personalizeEnabled": bool(personalize_enabled),
            "personalizeColumn": personalize_column or None,
        }

    async def fetch_widget_rows_cached(
        self,
        *,
        link: str,
        url: str,
        api_key: str,
        caller_email: str,
        selected_columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
        cursor: str | None = None,
    ):
        """Cache-aware sibling of `fetch_widget_rows` — same contract, same
        response shape, backing `GET /airtable/component/{link}/rows` once
        the cache is wired in below the endpoint's access-control check
        (plan §3.1, §4).

        Personalization is applied in Python against an unpersonalized,
        filters-only cached row set (§6) rather than baked into the
        Airtable formula, and pagination is served from synthetic
        `idx:<offset>` cursors over that cached set (§5.3) instead of real
        Airtable offsets.

        FAILS CLOSED exactly like `fetch_widget_rows`: if personalization is
        on but not applicable, this returns empty with
        `personalize_blocked=True` and — critically — never even reads the
        cache (§6.2), let alone writes to it.
        """
        from app.models.airtable import AirtableWidgetRowsResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        if ap.resolve_personalize_gate(
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        ):
            logger.warning(
                "Airtable widget cache fetch refused: personalization enabled but not "
                "applicable (base=%s table=%s) — returning no rows",
                base_id,
                table_id,
            )
            return AirtableWidgetRowsResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                fields=list(selected_columns or []),
                rows=[],
                next_cursor=None,
                personalize_blocked=True,
            )

        cache = get_cache_service()

        # No cache configured ⇒ behave exactly as this endpoint did before the
        # cache existed. Without this, a disabled cache still costs a FULL
        # table walk on every single request (`get`→None, `acquire_lock`→True
        # by fail-open design, walk, `set` no-op) — strictly slower than the
        # single-page live path it replaced
        # (plan_airtable_cache_scaling_2026-08-08.md §4.5.2). Placed BELOW the
        # fail-closed personalize gate above, which stays above everything
        # (landmine L6).
        if not cache.enabled:
            return await self.fetch_widget_rows(
                url=url,
                api_key=api_key,
                caller_email=caller_email,
                selected_columns=selected_columns,
                filters=filters,
                personalize_enabled=personalize_enabled,
                personalize_column=personalize_column,
                cursor=self._live_cursor(cursor),
            )

        fingerprint = self._widget_cache_fingerprint(
            widget_type=AIRTABLE_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=selected_columns,
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
        )
        cache_key = cache.build_key("widget_rows", fingerprint)

        envelope = await self._get_or_warm_widget_cache(
            cache_key=cache_key,
            url=url,
            api_key=api_key,
            selected_columns=selected_columns,
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
        )

        if envelope is None:
            # Oversized table, or nothing to show after waiting on another
            # warmer — degrade to today's live, uncached, single-page path,
            # exactly as before this feature existed (plan §5.4). Cursor
            # translation is `_live_cursor`'s job.
            return await self.fetch_widget_rows(
                url=url,
                api_key=api_key,
                caller_email=caller_email,
                selected_columns=selected_columns,
                filters=filters,
                personalize_enabled=personalize_enabled,
                personalize_column=personalize_column,
                cursor=self._live_cursor(cursor),
            )

        rows = envelope["rows"]
        if personalize_enabled:
            rows = [
                row
                for row in rows
                if ap.personalize_match(row.get(personalize_column), caller_email)
            ]

        # Client-facing field list: exactly the admin's own selection when
        # set (never wider than the envelope's own `fields`, which may
        # additionally carry an auto-appended personalize column — see
        # `_build_widget_cache_envelope`), else the envelope's discovery
        # order. Deliberately NOT recomputed from `rows` per page — same
        # "stable across pages" reasoning as `fetch_widget_rows`.
        response_fields = list(selected_columns) if selected_columns else envelope["fields"]
        field_set = set(response_fields)

        start = self._parse_synthetic_cursor(cursor)
        page = rows[start : start + self._WIDGET_PAGE_SIZE]
        projected_rows = [
            {k: v for k, v in row.items() if k == "id" or k in field_set} for row in page
        ]
        next_index = start + len(page)
        next_cursor = (
            self._make_synthetic_cursor(next_index) if next_index < len(rows) else None
        )

        # Typed-cell hints apply to the ordinary paginated widget too — asks
        # #2/#3 (URL buttons, select bubbles) were never scoped as opt-in
        # (plan_airtable_widget_viewer_controls_2026-08-12.md §2.5).
        field_types = await self.fetch_table_field_hints(
            base_id=envelope["base_id"], table_id=envelope["table_id"], api_key=api_key
        )

        return AirtableWidgetRowsResponse(
            base_id=envelope["base_id"],
            table_id=envelope["table_id"],
            view_id=envelope.get("view_id"),
            fields=response_fields,
            rows=projected_rows,
            next_cursor=next_cursor,
            personalize_blocked=False,
            field_types=field_types,
        )

    async def fetch_widget_full_rows_cached(
        self,
        *,
        link: str,
        url: str,
        api_key: str,
        caller_email: str,
        selected_columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
    ):
        """Whole-table view backing the viewer Filter/Sort/Group/Search
        toolbar (plan_airtable_widget_viewer_controls_2026-08-12.md §2.4).

        Reuses the EXACT cache entry `fetch_widget_rows_cached` warms — same
        fingerprint — so no second walk and no new caching mechanism.

        `available=False` when the table was not cacheable at all, when the
        cache is disabled, or when the personalize-filtered result exceeds
        `AIRTABLE_WIDGET_FULL_VIEW_MAX_ROWS`. The caller falls back to the
        paginated `/rows` view rather than erroring: some browsable data
        beats none. (Deliberately UNLIKE `fetch_widget_metric_cached`'s "no
        partial number" stance — a partial aggregate is silently WRONG, a
        partial row list is merely incomplete.)

        The `viewerControlsEnabled` toggle gate is the ROUTER's job (product
        control, not a security boundary — see the router docstring), not
        this method's: everything below is exactly as safe to call as
        `fetch_widget_rows_cached` is.
        """
        from app.models.airtable import AirtableWidgetFullRowsResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)
        page_size = self._settings.AIRTABLE_WIDGET_FULL_VIEW_PAGE_SIZE

        # 1. Fail-closed personalize gate FIRST, before the cache is touched
        # at all — identical to fetch_widget_rows_cached. `available=True`
        # here: the gate is about WHO may see rows, not about cacheability.
        if ap.resolve_personalize_gate(
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        ):
            logger.warning(
                "Airtable widget full-rows fetch refused: personalization enabled "
                "but not applicable (base=%s table=%s) — returning no rows",
                base_id,
                table_id,
            )
            return AirtableWidgetFullRowsResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                fields=list(selected_columns or []),
                field_types={},
                rows=[],
                personalize_blocked=True,
                available=True,
                page_size=page_size,
            )

        cache = get_cache_service()

        # 2. No cache configured ⇒ available=False (L3, rev-2 defect #10).
        # Without this, a disabled cache walks the ENTIRE table live on
        # every single request before shipping up to
        # AIRTABLE_WIDGET_FULL_VIEW_MAX_ROWS rows — the exact defect
        # `fetch_widget_rows_cached`'s own guard (:872 area) fixed for the
        # paginated path, reintroduced here on a path that ships far more
        # rows per request.
        if not cache.enabled:
            return AirtableWidgetFullRowsResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                fields=[],
                field_types={},
                rows=[],
                personalize_blocked=False,
                available=False,
                page_size=page_size,
            )

        # 3. Fingerprint built through the SAME shared helper
        # fetch_widget_rows_cached uses, with the same AIRTABLE_WIDGET_TYPE
        # (L1) — a mismatch silently doubles the Airtable walk and the
        # cache storage, with no visible symptom. Do NOT pass
        # AIRTABLE_METRIC_WIDGET_TYPE/AIRTABLE_CHART_WIDGET_TYPE here —
        # those pin a deliberately different, unprojected fingerprint
        # specific to the Metric/Chart widgets' shared-cache-entry design.
        fingerprint = self._widget_cache_fingerprint(
            widget_type=AIRTABLE_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=selected_columns,
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
        )
        cache_key = cache.build_key("widget_rows", fingerprint)

        envelope = await self._get_or_warm_widget_cache(
            cache_key=cache_key,
            url=url,
            api_key=api_key,
            selected_columns=selected_columns,
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
        )

        if envelope is None:
            # Oversized / locked / walk failed — no live fallback here
            # (unlike the paginated path): shipping up to 10,000 rows live,
            # uncached, on every request would be far worse than the
            # paginated live fallback's single page. The caller degrades to
            # the paginated `/rows` view instead.
            return AirtableWidgetFullRowsResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                fields=[],
                field_types={},
                rows=[],
                personalize_blocked=False,
                available=False,
                page_size=page_size,
            )

        # 5. Personalize-filter in Python, THEN project to response_fields,
        # THEN check the cap — order matters twice over: a 50,000-row table
        # personalized down to 12 rows for one viewer must not be reported
        # unavailable, and the projection is what stops the auto-appended
        # personalize column from reaching the client (L2).
        rows = envelope["rows"]
        if personalize_enabled:
            rows = [
                row
                for row in rows
                if ap.personalize_match(row.get(personalize_column), caller_email)
            ]

        # Exact same projection expression as fetch_widget_rows_cached
        # (:934-941 area) — reused verbatim so the auto-appended
        # personalize column (when not in the admin's own selectedColumns)
        # never leaks to a viewer here either (L2).
        response_fields = list(selected_columns) if selected_columns else envelope["fields"]
        field_set = set(response_fields)
        projected_rows = [
            {k: v for k, v in row.items() if k == "id" or k in field_set} for row in rows
        ]

        if len(projected_rows) > self._settings.AIRTABLE_WIDGET_FULL_VIEW_MAX_ROWS:
            return AirtableWidgetFullRowsResponse(
                base_id=envelope["base_id"],
                table_id=envelope["table_id"],
                view_id=envelope.get("view_id"),
                fields=response_fields,
                field_types={},
                rows=[],
                personalize_blocked=False,
                available=False,
                page_size=page_size,
            )

        field_types = await self.fetch_table_field_hints(
            base_id=envelope["base_id"], table_id=envelope["table_id"], api_key=api_key
        )

        return AirtableWidgetFullRowsResponse(
            base_id=envelope["base_id"],
            table_id=envelope["table_id"],
            view_id=envelope.get("view_id"),
            fields=response_fields,
            field_types=field_types,
            rows=projected_rows,
            personalize_blocked=False,
            available=True,
            page_size=page_size,
        )

    @staticmethod
    def _coerce_numeric(value: Any) -> float | None:
        """Best-effort numeric coercion for one Airtable field value, used by
        the Metric widget's Sum aggregation. Returns None (skip this value
        entirely, rather than treating it as 0) for anything that isn't
        sensibly a number — missing/blank, a checkbox bool (counting
        True/False as 1/0 would be a silent surprise, not a real sum), or
        free text that doesn't parse.

        A rollup/lookup field can return a LIST of values (e.g. summing a
        linked record's own numeric field) — its own numeric entries are
        added together; a list with no numeric entries contributes nothing.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "").replace("$", "")
            if not cleaned:
                return None
            try:
                return float(cleaned)
            except ValueError:
                return None
        if isinstance(value, list):
            total = 0.0
            found_any = False
            for item in value:
                coerced = AirtableService._coerce_numeric(item)
                if coerced is not None:
                    total += coerced
                    found_any = True
            return total if found_any else None
        return None

    @staticmethod
    def _chart_group_key(value: Any) -> str:
        """One Airtable field value -> one chart group label, for the Chart
        widget's group-by (decisions 5 and 6, plan_airtable_chart_widget_
        2026-08-13.md §3.2).

        None / "" / [] / {} all fold into a single "(Empty)" group so a row
        with a blank group-by value never silently disappears — the chart's
        row total always matches the record count.

        A list (multi-select, linked records, a rollup/lookup of many
        values) becomes ONE combined group: its non-empty elements, coerced
        individually and joined with ", ", in the ORDER Airtable returned
        them — deliberately not sorted, so `['A','B']` and `['B','A']` are
        two distinct groups (assumption §9.2). Every row still lands in
        exactly one bucket, so Count/Sum and percentages stay exact.

        Never raises: a group label is a display string, and a chart that
        500s because one cell held an unexpected shape is worse than one odd
        label.
        """
        if value is None:
            return "(Empty)"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (int, float)):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
        if isinstance(value, list):
            # Each element goes through this same coercion (recursively
            # handling nested dict/collaborator shapes), then any element
            # that came out blank is dropped rather than contributing a
            # literal "(Empty)" into the joined label.
            parts = [
                part
                for item in value
                if (part := AirtableService._chart_group_key(item)) != "(Empty)"
            ]
            return ", ".join(parts) if parts else "(Empty)"
        if isinstance(value, dict):
            if not value:
                return "(Empty)"
            for key in ("name", "email"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            try:
                text = str(value)
            except Exception:
                return "(Empty)"
            return text.strip() or "(Empty)"
        try:
            text = str(value).strip()
        except Exception:
            return "(Empty)"
        return text if text else "(Empty)"

    async def fetch_widget_metric_cached(
        self,
        *,
        link: str,
        url: str,
        api_key: str,
        caller_email: str,
        aggregation: str,
        sum_field: str | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
    ):
        """Count/Sum aggregation for a dashboard Airtable Metric widget.

        Shares the EXACT SAME `widget_rows` cache envelope
        `fetch_widget_rows_cached` uses — same fingerprint shape, same TTL,
        same cron refresh, same negative-cache/base-lock protections. No new
        caching mechanism: this widget's own `link` already makes the cache
        key unique, so a Metric widget never collides with (or shares stale
        data from) a Table widget's entry even against the same base/table.
        `selected_columns` is always omitted here (a Metric widget has no
        column-display picker), so the walk fetches every field — which is
        exactly what's needed to sum an arbitrary `sum_field` without a
        second, differently-projected cache entry per widget.

        Deliberately does NOT fall back to a live, uncached walk when the
        table is too large to cache (`available=False` instead). Unlike the
        row-list endpoint, there is no "first page" equivalent for an
        aggregate — a count/sum over a partial fetch would be silently
        WRONG, not just incomplete, which is worse than reporting nothing.

        Personalize is applied in Python against the shared, unpersonalized
        envelope, exactly like `fetch_widget_rows_cached` — same fail-closed
        gate, checked before the cache is even touched, so Count/Sum can be
        computed per-viewer without a second cache entry per viewer.
        """
        from app.models.airtable import AirtableWidgetMetricResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        if ap.resolve_personalize_gate(
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        ):
            logger.warning(
                "Airtable widget metric refused: personalization enabled but "
                "not applicable (base=%s table=%s) — no value computed",
                base_id,
                table_id,
            )
            return AirtableWidgetMetricResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                value=None,
                available=True,
                personalize_blocked=True,
            )

        if aggregation == "sum" and not sum_field:
            # Not yet configured — nothing to compute. The frontend already
            # knows `sumField` locally (it's part of the widget's own
            # unprotected data blob) and should avoid calling this endpoint
            # in this state at all; this is a defensive fallback, not the
            # primary path.
            return AirtableWidgetMetricResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                value=None,
                available=False,
                personalize_blocked=False,
            )

        cache = get_cache_service()
        fingerprint = self._widget_cache_fingerprint(
            widget_type=AIRTABLE_METRIC_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=None,
            filters=filters,
            personalize_enabled=False,
            personalize_column=None,
        )
        cache_key = cache.build_key("widget_rows", fingerprint)

        envelope = await self._get_or_warm_widget_cache(
            cache_key=cache_key,
            url=url,
            api_key=api_key,
            selected_columns=None,
            filters=filters,
            personalize_enabled=False,
            personalize_column=None,
        )

        if envelope is None:
            return AirtableWidgetMetricResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                value=None,
                available=False,
                personalize_blocked=False,
            )

        rows = envelope["rows"]
        if personalize_enabled:
            rows = [
                row
                for row in rows
                if ap.personalize_match(row.get(personalize_column), caller_email)
            ]

        if aggregation == "sum":
            total = 0.0
            for row in rows:
                coerced = self._coerce_numeric(row.get(sum_field))
                if coerced is not None:
                    total += coerced
            value: float | int = total
        else:
            value = len(rows)

        return AirtableWidgetMetricResponse(
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            aggregation=aggregation,
            value=value,
            available=True,
            personalize_blocked=False,
        )

    @staticmethod
    def _chart_cache_fingerprint(
        *, link: str, url: str, filters: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """The `widget_rows` cache fingerprint a Chart widget's aggregation
        reads — a thin, Chart-specific alias over the shared
        `_widget_cache_fingerprint(widget_type=AIRTABLE_CHART_WIDGET_TYPE)`,
        which is BYTE-IDENTICAL to `fetch_widget_metric_cached`'s own
        (`selectedColumns: []`, `personalizeEnabled: False`,
        `personalizeColumn: None`), so the walk fetches every field and a
        chart's own `link` keeps its entry from colliding with a Table or
        Metric widget's differently-projected one on the same base/table.

        Kept as its own named method (rather than inlining the shared call
        at both use sites) so `preview_widget_chart`'s read-only lookup can
        never drift from `fetch_widget_chart_cached`'s own (L1) — a drift
        has no visible symptom, it just means the preview always misses the
        warmed entry and always reports `partial=True`.
        """
        return AirtableService._widget_cache_fingerprint(
            widget_type=AIRTABLE_CHART_WIDGET_TYPE,
            link=link,
            url=url,
            selected_columns=None,
            filters=filters,
            personalize_enabled=False,
            personalize_column=None,
        )

    def _aggregate_chart_groups(
        self,
        *,
        rows: list[dict[str, Any]],
        base_id: str,
        table_id: str,
        view_id: str | None,
        group_field: str,
        aggregation: str,
        sum_field: str | None,
        max_groups: int | None,
        group_sort: str,
        partial: bool,
    ):
        """Steps 7-9 of the Chart widget's aggregation (plan §3.3): group,
        truncate-then-sort (L6), and total (L7), over an already-resolved
        `rows` list — personalization (step 6) is the CALLER's job, and
        deliberately not done here, because the two callers apply it
        differently: `fetch_widget_chart_cached` and the preview's cache-hit
        branch post-filter an unpersonalized envelope in Python
        (`ap.personalize_match`), while the preview's live-capped branch
        bakes personalization into the Airtable formula itself
        (`af.widget_formula`) before the rows ever reach here. Sharing THIS
        half keeps a widget and its own preview computing groups identically
        (L1) without coupling the two different personalize mechanisms.
        """
        from app.models.airtable import AirtableChartGroup, AirtableWidgetChartResponse

        totals: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        order: list[str] = []
        seen: set[str] = set()
        for row in rows:
            key = self._chart_group_key(row.get(group_field))
            if key not in seen:
                seen.add(key)
                order.append(key)
            counts[key] += 1
            if aggregation == "sum":
                coerced = self._coerce_numeric(row.get(sum_field))
                if coerced is not None:
                    totals[key] += coerced

        def _value_for(key: str) -> float | int:
            return totals.get(key, 0.0) if aggregation == "sum" else counts.get(key, 0)

        effective_max = max(
            2,
            min(
                max_groups or self._settings.AIRTABLE_CHART_DEFAULT_MAX_GROUPS,
                self._settings.AIRTABLE_CHART_MAX_GROUPS,
            ),
        )

        # L6 — truncate BEFORE sorting for display: rank every group by
        # value first, keep the top (effective_max - 1), fold the rest into
        # one 'Other'. Sorting under group_sort first (e.g. label_asc) would
        # fold the alphabetically-LAST groups into 'Other' instead of the
        # smallest ones.
        ranked = sorted(order, key=_value_for, reverse=True)
        kept_keys = ranked[: effective_max - 1]
        other_keys = ranked[effective_max - 1 :]

        kept = [
            AirtableChartGroup(label=key, value=_value_for(key), is_other=False)
            for key in kept_keys
        ]

        other_group_count = len(other_keys)
        if other_keys:
            other_value: float | int = (
                sum(totals.get(k, 0.0) for k in other_keys)
                if aggregation == "sum"
                else sum(counts.get(k, 0) for k in other_keys)
            )
            kept.append(AirtableChartGroup(label="Other", value=other_value, is_other=True))

        # Now apply the admin's display sort to the KEPT groups only —
        # 'Other' is pinned last regardless of mode (decision 7).
        real_groups = [g for g in kept if not g.is_other]
        other_group = [g for g in kept if g.is_other]
        if group_sort == "label_asc":
            real_groups.sort(key=lambda g: g.label.lower())
        else:
            real_groups.sort(key=lambda g: g.value, reverse=True)
        groups = real_groups + other_group

        # L7 — the % denominator includes 'Other', so the caller's displayed
        # labels sum to 100%. Equal by construction to the pre-truncation
        # total minus nothing: every row landed in exactly one kept-or-Other
        # bucket.
        total = sum(g.value for g in groups)

        return AirtableWidgetChartResponse(
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            aggregation=aggregation,
            group_field=group_field,
            groups=groups,
            total=total,
            other_group_count=other_group_count,
            row_count=len(rows),
            available=True,
            personalize_blocked=False,
            partial=partial,
        )

    async def fetch_widget_chart_cached(
        self,
        *,
        link: str,
        url: str,
        api_key: str,
        caller_email: str,
        group_field: str | None,
        aggregation: str,
        sum_field: str | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
        max_groups: int | None = None,
        group_sort: str = "value_desc",
    ):
        """Grouped Count/Sum aggregation for a dashboard Airtable Chart
        widget — one aggregate PER GROUP, over the SAME cached `widget_rows`
        envelope `fetch_widget_metric_cached` uses (see
        `_chart_cache_fingerprint`). No new caching mechanism.

        Same "no partial aggregate" stance as the Metric widget:
        `available=False` on an oversized/uncacheable table rather than a
        live, partial fetch — a count/sum (per group) over only some rows
        would be silently WRONG, not just incomplete (L4). Same fail-closed
        personalize gate, checked before the cache is touched (L5).
        """
        from app.models.airtable import AirtableWidgetChartResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        if ap.resolve_personalize_gate(
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        ):
            logger.warning(
                "Airtable widget chart refused: personalization enabled but "
                "not applicable (base=%s table=%s) — no groups computed",
                base_id,
                table_id,
            )
            return AirtableWidgetChartResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                group_field=group_field or "",
                groups=[],
                available=True,
                personalize_blocked=True,
            )

        if not group_field or (aggregation == "sum" and not sum_field):
            # Not yet configured — nothing to compute. The frontend already
            # knows `groupField`/`sumField` locally and shouldn't call in
            # this state; defensive fallback, mirroring
            # fetch_widget_metric_cached's own (:1422-1436).
            return AirtableWidgetChartResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                group_field=group_field or "",
                groups=[],
                available=False,
                personalize_blocked=False,
            )

        cache = get_cache_service()
        fingerprint = self._chart_cache_fingerprint(link=link, url=url, filters=filters)
        cache_key = cache.build_key("widget_rows", fingerprint)

        envelope = await self._get_or_warm_widget_cache(
            cache_key=cache_key,
            url=url,
            api_key=api_key,
            selected_columns=None,
            filters=filters,
            personalize_enabled=False,
            personalize_column=None,
        )

        if envelope is None:
            return AirtableWidgetChartResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                group_field=group_field,
                groups=[],
                available=False,
                personalize_blocked=False,
            )

        # Step 6 — personalize-filter the envelope's (unpersonalized) rows in
        # Python, identical to fetch_widget_metric_cached's own (:1470-1476).
        rows = envelope["rows"]
        if personalize_enabled:
            rows = [
                row
                for row in rows
                if ap.personalize_match(row.get(personalize_column), caller_email)
            ]

        return self._aggregate_chart_groups(
            rows=rows,
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            group_field=group_field,
            aggregation=aggregation,
            sum_field=sum_field,
            max_groups=max_groups,
            group_sort=group_sort,
            partial=False,
        )

    async def warm_widget_cache(
        self,
        *,
        widget_type: str,
        link: str,
        url: str,
        api_key: str,
        selected_columns: list[str] | None,
        filters: list[dict[str, Any]] | None,
        personalize_enabled: bool,
        personalize_column: str | None,
    ) -> str:
        """Used by `POST /airtable/cache/refresh` (the scheduled-refresh
        endpoint) and by `_warm_after_config_save` (warm-on-save). Builds
        the same fingerprint/key a real viewer's request would use and
        re-warms it, ignoring whatever TTL remains.

        `widget_type` (required, not defaulted —
        plan_warm_key_divergence_2026-08-14.md §3.2) decides whether
        `selected_columns`/`personalize_enabled`/`personalize_column` are
        honored as given (Table) or normalized away to the Metric/Chart
        widgets' shared "nothing projected, nothing personalized" shape —
        see `_widget_cache_fingerprint`'s docstring for why. That
        normalization happens ONCE, below, before either the fingerprint is
        built or the walk is dispatched, deliberately: pinning the key
        alone while still walking with the widget's real projection would
        cache rows missing a field a Metric/Chart reader expects (the L2
        projection trap, in reverse). Before `widget_type` existed, this
        method built its fingerprint from the widget's raw stored config
        with no notion of widget type at all — for a Metric/Chart widget
        with personalization on (or merely a stored `personalizeColumn`
        left over from a prior save), that fingerprint never matched what
        `fetch_widget_metric_cached`/`fetch_widget_chart_cached` look up:
        the warm wrote a whole-table entry nobody ever read, and the widget
        stayed cold until a real viewer's own request warmed the RIGHT key.

        Takes the SAME single-flight lock the read path uses
        (plan_airtable_cache_scaling_2026-08-08.md §4.4). The original
        no-lock design assumed cron runs never overlap; one 40-second table
        makes that false, and two concurrent walks of the same base means
        Airtable 429s. A run that cannot get the lock skips — that is an
        expected outcome, NOT a failure (landmine L9).

        Also skips a widget whose cached entry is younger than
        `AIRTABLE_CACHE_MIN_REFRESH_SECONDS`, so a duplicate run costs one
        GET rather than a full walk. Same treatment for a widget already
        confirmed oversized/failing by EITHER this method or the read path
        (handoff 2026-08-10 §3, finding "cron re-walks an oversized widget
        every tick"): without this, the cron previously never consulted the
        negative marker at all, so a persistently oversized table paid a
        full throttled walk to the cap on every 5-minute tick, forever,
        discarding it every time.

        Returns one of:
          * ``"refreshed"``   — walked and written;
          * ``"oversized"``   — just confirmed too large to cache this call;
            served live, and (unlike before this fix) a negative marker is
            now written so the NEXT tick doesn't repeat the discovery;
          * ``"walk_failed"`` — the walk itself raised (e.g. an Airtable
            429/5xx that outlasted the transport-level retry); same
            negative-marker treatment as oversized;
          * ``"damped"``      — a negative marker from a PRIOR oversized or
            walk_failed confirmation (by this method or the read path) is
            still fresh — skipped without attempting a walk at all;
          * ``"fresh"``       — cached entry is younger than the guard;
          * ``"locked"``      — another run/reader holds the per-fingerprint
            lock, OR a DIFFERENT widget on the same base holds the
            base-level lock (finding #6) — either way, contention, not a
            confirmed bad table;
          * ``"disabled"``    — no cache configured, so there is nothing to
            warm and a walk would be pure waste.
        Every outcome except ``"refreshed"`` is a *skip*, and
        `_REFRESH_OUTCOME_COUNTER` in routers/airtable.py is the one place
        that maps each to its summary counter.
        """
        cache = get_cache_service()
        if not cache.enabled:
            return "disabled"

        # Normalize BEFORE building the fingerprint, so the same (now
        # possibly-overridden) values flow into the walk below via
        # `_build_widget_cache_envelope` — one normalization, not two, so
        # the key and the walked bytes can never disagree (see this
        # method's own docstring, and `_widget_cache_fingerprint`'s).
        if widget_type in (AIRTABLE_METRIC_WIDGET_TYPE, AIRTABLE_CHART_WIDGET_TYPE):
            selected_columns = None
            personalize_enabled = False
            personalize_column = None

        fingerprint = self._widget_cache_fingerprint(
            widget_type=widget_type,
            link=link,
            url=url,
            selected_columns=selected_columns,
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
        )
        cache_key = cache.build_key("widget_rows", fingerprint)

        # Checked before both the freshness lookup and lock acquisition, so
        # a confirmed-bad widget skips ALL of that too, not just the walk
        # (same "before lock acquisition" reasoning finding #3 already
        # applied on the read path).
        if await cache.get(f"{cache_key}{self._NEGATIVE_CACHE_SUFFIX}") is not None:
            logger.info(
                "Airtable cache refresh: widget link=%s is negative-cached "
                "(oversized or recently failing) — skipping re-walk",
                link,
            )
            return "damped"

        min_age = self._settings.AIRTABLE_CACHE_MIN_REFRESH_SECONDS
        if min_age > 0:
            existing = await cache.get(cache_key)
            if isinstance(existing, dict):
                age = self._envelope_age_seconds(existing)
                if age is not None and age < min_age:
                    logger.info(
                        "Airtable cache refresh: widget link=%s is %.0fs old "
                        "(< %ds) — skipping re-walk",
                        link,
                        age,
                        min_age,
                    )
                    return "fresh"

        lock_key = f"{cache_key}:lock"
        lock_token = await cache.acquire_lock(
            lock_key, ttl_seconds=self._settings.AIRTABLE_CACHE_REFRESH_LOCK_SECONDS
        )
        if not lock_token:
            logger.info(
                "Airtable cache refresh: widget link=%s is already being "
                "warmed elsewhere — skipping",
                link,
            )
            return "locked"

        try:
            try:
                envelope, status = await self._build_widget_cache_envelope(
                    cache=cache,
                    url=url,
                    api_key=api_key,
                    selected_columns=selected_columns,
                    filters=filters,
                    personalize_enabled=personalize_enabled,
                    personalize_column=personalize_column,
                )
            except AirtableError:
                # Mirrors the read path's finding #2 handling: a walk
                # failure must not blow up the whole sweep for every OTHER
                # widget (the router's own except-Exception around
                # `_refresh_one` already caught this before, but silently,
                # with no negative marker written — this is the fix for
                # that gap, finding "C" above).
                logger.warning(
                    "Airtable cache refresh: widget link=%s walk failed — skipped",
                    link,
                    exc_info=True,
                )
                await self._mark_walk_unwarmable(
                    cache,
                    cache_key,
                    reason="walk_failed",
                    ttl_seconds=self._settings.AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS,
                )
                return "walk_failed"

            if status == "locked":
                # A different widget on the same base is walking right now
                # (finding #6) — contention, not a confirmed bad table, so
                # no negative marker. Same "locked" outcome the per-
                # fingerprint lock already uses above; the router's counter
                # mapping doesn't need to distinguish the two causes.
                logger.info(
                    "Airtable cache refresh: widget link=%s's base is "
                    "already being walked by another widget — skipping",
                    link,
                )
                return "locked"

            if status == "oversized":
                logger.warning(
                    "Airtable cache refresh: widget link=%s is too large to cache — skipped",
                    link,
                )
                # Previously NOT written from this path — an oversized
                # widget was re-walked to the cap on every single tick,
                # forever, discarding the result each time (finding "C").
                # Long TTL: see AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS.
                await self._mark_walk_unwarmable(
                    cache,
                    cache_key,
                    reason="oversized",
                    ttl_seconds=self._settings.AIRTABLE_CACHE_REFRESH_NEGATIVE_TTL_SECONDS,
                )
                return "oversized"

            await cache.set(
                cache_key,
                envelope,
                ttl_seconds=self._settings.AIRTABLE_CACHE_TTL_SECONDS,
                # Same entry the read path warms — must be written the same
                # way (§4.3).
                compress=True,
            )
            return "refreshed"
        finally:
            await cache.release_lock(lock_key, lock_token)

    @staticmethod
    def _envelope_age_seconds(envelope: dict[str, Any]) -> float | None:
        """Seconds since `envelope` was written, or None if its timestamp is
        missing or unparseable — in which case the caller must treat the
        entry as stale and refresh it, never as fresh."""
        stamp = envelope.get("last_updated_date")
        if not isinstance(stamp, str):
            return None
        try:
            written = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - written).total_seconds()

    async def preview_widget_config(
        self,
        *,
        url: str,
        api_key: str,
        caller_email: str,
        selected_columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
    ):
        """Editor preview for the Property Panel.

        Returns `fields` computed WITHOUT personalization and `rows` computed
        WITH it. The split matters: an admin configuring a widget must be
        able to populate the column dropdowns even when their OWN row set is
        empty — otherwise picking a personalize column becomes impossible for
        anyone whose email does not appear in the table.

        Also returns `unpersonalized_row_count`, so the panel can tell
        "personalize is working and you have no rows" apart from "the
        personalize column is the wrong type and matches nothing" — which are
        otherwise indistinguishable and both look like a broken widget.

        Costs up to two Airtable calls, which is acceptable at edit
        frequency (and one when personalization is off).
        """
        from app.models.airtable import AirtableEditorPreviewResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        filters_formula, _ = af.widget_formula(filters=filters)

        base_options: dict[str, Any] = {"max_records": self._PREVIEW_MAX_RECORDS}
        if view_id:
            base_options["view"] = view_id
        if selected_columns:
            base_options["fields"] = list(selected_columns)

        api = Api(api_key.strip())
        table = api.table(base_id, table_id)

        async def run(formula: str | None) -> list[dict[str, Any]]:
            options = dict(base_options)
            if formula:
                options["formula"] = formula
            try:
                payload = await asyncio.to_thread(
                    api.request, "get", table.urls.records, options=options
                )
            except RequestException as exc:
                logger.error("Airtable editor preview failed: %s", exc)
                raise AirtableError(f"Airtable API error: {exc}") from exc
            except Exception as exc:
                logger.exception("Unexpected Airtable error during editor preview")
                raise AirtableError(f"Airtable API error: {exc}") from exc
            return payload.get("records", []) or []

        unpersonalized = await run(filters_formula)

        seen_fields: list[str] = []
        seen: set[str] = set()
        for record in unpersonalized:
            for key in (record.get("fields", {}) or {}):
                if key not in seen:
                    seen.add(key)
                    seen_fields.append(key)

        personalize_blocked = False
        if personalize_enabled:
            formula, allowed = af.widget_formula(
                filters=filters,
                personalize_enabled=True,
                personalize_column=personalize_column,
                email=caller_email,
            )
            if allowed:
                records = await run(formula)
            else:
                records, personalize_blocked = [], True
        else:
            records = unpersonalized

        rows = [
            {"id": r.get("id"), **(r.get("fields", {}) or {})} for r in records
        ]

        # Prefer the admin's explicit column order over discovery order — see
        # the matching comment in fetch_widget_rows. Without this, the editor
        # preview would silently ignore the admin's custom column ordering
        # even though the saved widget honors it once persisted.
        fields = list(selected_columns) if selected_columns else seen_fields

        field_types, field_types_available = await self.fetch_table_field_hints_with_status(
            base_id=base_id, table_id=table_id, api_key=api_key
        )

        return AirtableEditorPreviewResponse(
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            fields=fields,
            rows=rows,
            personalize_blocked=personalize_blocked,
            unpersonalized_row_count=len(unpersonalized),
            field_types=field_types,
            field_types_available=field_types_available,
        )

    async def preview_widget_chart(
        self,
        *,
        link: str | None,
        url: str,
        api_key: str,
        caller_email: str,
        group_field: str | None,
        aggregation: str,
        sum_field: str | None = None,
        filters: list[dict[str, Any]] | None = None,
        personalize_enabled: bool = False,
        personalize_column: str | None = None,
        max_groups: int | None = None,
        group_sort: str = "value_desc",
    ):
        """Editor preview for a Chart widget's in-progress settings (decision
        3, plan §3.4). Two paths, chosen by what the caller could supply:

        * **Cached path** — `link` was resolved (so the widget's own stored
          token/URL are in play) AND the cache is enabled. A READ-ONLY
          lookup (`allow_warm=False`, L3) against the exact same fingerprint
          `fetch_widget_chart_cached` would build from these filters. On a
          hit, `partial=False` — an admin changing group-by/aggregation/
          labels on a saved widget never touches `filters`, so the warmed
          entry answers exactly.
        * **Live capped path** — own-token shape (no `link`), cache
          disabled, or a read-only miss (filters edited since the last warm;
          table never cacheable). One Airtable read capped at
          `_PREVIEW_MAX_RECORDS`, reusing `preview_widget_config`'s `run()`
          shape — personalization is baked into the Airtable formula itself
          here (`af.widget_formula`), not applied in Python afterward.
          `partial=True`.

        Never warms the cache (L3): an admin typing into the filter box
        produces a new fingerprint per keystroke, and warming each one would
        be a full table walk (plus a whole-table Upstash entry) per
        keystroke burst, for cache entries no viewer will ever read.
        """
        from app.models.airtable import AirtableWidgetChartResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        # Same fail-closed gate, same ordering (before any cache/Airtable
        # access) as fetch_widget_chart_cached (L5).
        if ap.resolve_personalize_gate(
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        ):
            return AirtableWidgetChartResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                group_field=group_field or "",
                groups=[],
                available=True,
                personalize_blocked=True,
            )

        if not group_field or (aggregation == "sum" and not sum_field):
            return AirtableWidgetChartResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                group_field=group_field or "",
                groups=[],
                available=False,
                personalize_blocked=False,
            )

        cache = get_cache_service()

        if link and cache.enabled:
            fingerprint = self._chart_cache_fingerprint(link=link, url=url, filters=filters)
            cache_key = cache.build_key("widget_rows", fingerprint)
            envelope = await self._get_or_warm_widget_cache(
                cache_key=cache_key,
                url=url,
                api_key=api_key,
                selected_columns=None,
                filters=filters,
                personalize_enabled=False,
                personalize_column=None,
                allow_warm=False,
            )
            if envelope is not None:
                rows = envelope["rows"]
                if personalize_enabled:
                    rows = [
                        row
                        for row in rows
                        if ap.personalize_match(row.get(personalize_column), caller_email)
                    ]
                return self._aggregate_chart_groups(
                    rows=rows,
                    base_id=base_id,
                    table_id=table_id,
                    view_id=view_id,
                    group_field=group_field,
                    aggregation=aggregation,
                    sum_field=sum_field,
                    max_groups=max_groups,
                    group_sort=group_sort,
                    partial=False,
                )

        # Live capped path — no warmed entry to read. One Airtable call,
        # capped, personalization applied server-side via the formula
        # (same shape as preview_widget_config's `run()`).
        base_options: dict[str, Any] = {"max_records": self._PREVIEW_MAX_RECORDS}
        if view_id:
            base_options["view"] = view_id

        api = Api(api_key.strip())
        table = api.table(base_id, table_id)

        async def run(formula: str | None) -> list[dict[str, Any]]:
            options = dict(base_options)
            if formula:
                options["formula"] = formula
            try:
                payload = await asyncio.to_thread(
                    api.request, "get", table.urls.records, options=options
                )
            except RequestException as exc:
                logger.error("Airtable chart preview failed: %s", exc)
                raise AirtableError(f"Airtable API error: {exc}") from exc
            except Exception as exc:
                logger.exception("Unexpected Airtable error during chart preview")
                raise AirtableError(f"Airtable API error: {exc}") from exc
            return payload.get("records", []) or []

        formula, allowed = af.widget_formula(
            filters=filters,
            personalize_enabled=personalize_enabled,
            personalize_column=personalize_column,
            email=caller_email,
        )
        if not allowed:
            return AirtableWidgetChartResponse(
                base_id=base_id,
                table_id=table_id,
                view_id=view_id,
                aggregation=aggregation,
                group_field=group_field,
                groups=[],
                available=True,
                personalize_blocked=True,
                partial=True,
            )

        records = await run(formula)
        rows = [{"id": r.get("id"), **(r.get("fields", {}) or {})} for r in records]

        return self._aggregate_chart_groups(
            rows=rows,
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            group_field=group_field,
            aggregation=aggregation,
            sum_field=sum_field,
            max_groups=max_groups,
            group_sort=group_sort,
            partial=True,
        )

    async def preview_from_url(
        self,
        url: str,
        fields: list[str] | None = None,
        formula: str | None = None,
        api_key: str = "",
    ):
        """Fetch a capped, read-only preview of an arbitrary Airtable
        table/view referenced by a pasted share URL. Used by the dashboard's
        Airtable widget — intentionally generic (no fixed field schema).

        ``fields`` limits which columns Airtable returns (more efficient than
        fetching all and discarding). ``formula`` is passed verbatim to
        Airtable's ``filterByFormula`` parameter and is applied server-side
        before the row cap, so the cap applies to already-filtered results.
        """
        from app.models.airtable import AirtablePreviewResponse

        base_id, table_id, view_id = self._parse_airtable_share_url(url)

        table = Api(api_key.strip()).table(base_id, table_id)
        kwargs: dict[str, Any] = {"max_records": self._PREVIEW_MAX_RECORDS}
        if view_id:
            kwargs["view"] = view_id
        if fields:
            kwargs["fields"] = fields
        if formula and formula.strip():
            kwargs["formula"] = formula.strip()

        try:
            records = await asyncio.to_thread(table.all, **kwargs)
        except RequestException as exc:
            logger.error("Airtable preview request failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during preview fetch")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        seen_fields: list[str] = []
        seen_set: set[str] = set()
        rows: list[dict[str, Any]] = []
        for record in records:
            fields = record.get("fields", {}) or {}
            for key in fields:
                if key not in seen_set:
                    seen_set.add(key)
                    seen_fields.append(key)
            rows.append({"id": record.get("id"), **fields})

        return AirtablePreviewResponse(
            base_id=base_id,
            table_id=table_id,
            view_id=view_id,
            fields=seen_fields,
            rows=rows,
        )

    async def is_admin(self, email: str) -> bool:
        """Return True if the given email has an entry in the Admins table."""
        if not email:
            logger.info("Admin lookup skipped: empty email → classifying as 'user'")
            return False
        email_field = self._settings.ADMINS_EMAIL_FIELD
        normalized = email.strip().lower()
        formula = f"LOWER({{{email_field}}}) = '{self._escape(normalized)}'"
        table = self._admins_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1, fields=[email_field]
            )
        except RequestException as exc:
            logger.error("Airtable admin lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during admin lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        is_admin = bool(records)
        logger.info(
            "Admin lookup for email=%s → classified as '%s'",
            normalized,
            "admin" if is_admin else "user",
        )
        return is_admin

    async def get_user_roles(self, email: str) -> list[str]:
        """Return the list of role names assigned to ``email`` in the
        Access Control table.

        Reads the ``Role Name`` lookup field from every Access Control
        record matching the user's email, flattens them, de-duplicates
        while preserving order, and returns the resulting list. Returns
        an empty list when the email is empty, no record is found, or
        the lookup field is missing.
        """
        if not email:
            logger.info("Role lookup skipped: empty email → returning []")
            return []

        # if self._is_dev_admin_override(email):
        #     logger.warning("DEV_ADMIN_OVERRIDE_EMAILS granting Hub Admin to %s", email)
        #     return [self.HUB_ADMIN_ROLE]

        s = self._settings
        email_field = s.ACCESS_CONTROL_USER_EMAIL_FIELD
        role_name_field = s.ACCESS_CONTROL_ROLE_NAME_LOOKUP_FIELD
        normalized = email.strip().lower()
        formula = f"LOWER({{{email_field}}}) = '{self._escape(normalized)}'"

        table = self._access_control_table()
        try:
            records = await asyncio.to_thread(
                table.all,
                formula=formula,
                fields=[email_field, role_name_field],
            )
        except RequestException as exc:
            logger.error("Airtable role lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during role lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        roles: list[str] = []
        seen: set[str] = set()
        for rec in records:
            fields = rec.get("fields", {}) or {}
            value = fields.get(role_name_field)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if item is None:
                    continue
                name = str(item).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                roles.append(name)

        logger.info("Role lookup for email=%s → roles=%s", normalized, roles)
        return roles

    # def _is_dev_admin_override(self, email: str) -> bool:
    #     """True when ``email`` is a configured local-testing admin override.
    #
    #     Gated on DEBUG so this can never silently activate in production.
    #     """
    #     s = self._settings
    #     if not s.DEBUG or not s.DEV_ADMIN_OVERRIDE_EMAILS:
    #         return False
    #     overrides = {
    #         e.strip().lower()
    #         for e in s.DEV_ADMIN_OVERRIDE_EMAILS.split(",")
    #         if e.strip()
    #     }
    #     return email.strip().lower() in overrides

    # Role name that grants RenPhil Hub administrator privileges.
    HUB_ADMIN_ROLE = "Hub Admin"

    async def is_hub_admin(self, email: str) -> bool:
        """Return True if ``email`` has the ``Hub Admin`` role in the
        Access Control table.
        """
        roles = await self.get_user_roles(email)
        return self.HUB_ADMIN_ROLE in roles

    # Role scope value that represents a global (non-scoped) role.
    HUB_SCOPE = "Hub"

    async def get_user_scoped_roles(self, email: str):
        """Return the per-assignment scoped roles for ``email``.

        Each entry contains the role name, its scope, and the fund or
        program name from the matching Access Control record. The
        fund/program is set to ``None`` when the role's scope is
        ``Hub`` (global).
        """
        # Imported here to avoid a circular import with app.models.auth.
        from app.models.auth import ScopedRole

        if not email:
            return []

        # if self._is_dev_admin_override(email):
        #     logger.warning("DEV_ADMIN_OVERRIDE_EMAILS granting Hub Admin to %s", email)
        #     return [
        #         ScopedRole(
        #             role_name=self.HUB_ADMIN_ROLE,
        #             scope=self.HUB_SCOPE,
        #             fund_or_program_name=None,
        #         )
        #     ]

        s = self._settings
        email_field = s.ACCESS_CONTROL_USER_EMAIL_FIELD
        normalized = email.strip().lower()
        formula = f"LOWER({{{email_field}}}) = '{self._escape(normalized)}'"
        table = self._access_control_table()
        try:
            records = await asyncio.to_thread(table.all, formula=formula)
        except RequestException as exc:
            logger.error("Airtable scoped-role lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during scoped-role lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        if not records:
            return []

        # Build role_id → (name, scope) map from the Roles catalog.
        roles_catalog = await self.get_unique_roles()
        role_by_id = {r.id: r for r in roles_catalog}

        out: list = []
        seen: set[tuple[str, str, str]] = set()
        for rec in records:
            ac = self._build_access_control_record(rec)
            for role in ac.roles:
                catalog_role = role_by_id.get(role.id)
                scope = catalog_role.scope if catalog_role else None
                name = role.name or (catalog_role.name if catalog_role else None)
                if scope and scope.strip().lower() == self.HUB_SCOPE.lower():
                    fund_or_program = None
                else:
                    fund_or_program = ac.fund_or_program_name
                key = (name or "", scope or "", fund_or_program or "")
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ScopedRole(
                        role_name=name,
                        scope=scope,
                        fund_or_program_name=fund_or_program,
                    )
                )
        return out

    @staticmethod
    def _escape(value: str) -> str:
        """Escape backslashes and single quotes for Airtable formula literals."""
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @classmethod
    def _build_formula(
        cls,
        *,
        eq_year: int | None,
        lt_year: int | None,
        gt_year: int | None,
        opportunity_rec_type: str | list[str] | None = None,
    ) -> str | None:
        """
        Build a ``filterByFormula`` string from the query filters.

        Year filters (``eq_year``, ``lt_year``, ``gt_year``) are combined
        with **OR** — i.e. a record matches when its Fiscal Year satisfies
        *any* of the provided year clauses (union semantics).

        The Opportunity Record Type filter, when provided, is combined
        with the year condition using **AND**.
        """
        # ── year clauses (OR-combined) ────────────────────────────────
        year_clauses: list[str] = []
        if eq_year is not None:
            year_clauses.append(f"{{{_F_FISCAL_YEAR}}} = {int(eq_year)}")
        if lt_year is not None:
            year_clauses.append(f"{{{_F_FISCAL_YEAR}}} < {int(lt_year)}")
        if gt_year is not None:
            year_clauses.append(f"{{{_F_FISCAL_YEAR}}} > {int(gt_year)}")

        if not year_clauses:
            year_expr: str | None = None
        elif len(year_clauses) == 1:
            year_expr = year_clauses[0]
        else:
            year_expr = f"OR({', '.join(year_clauses)})"

        # ── opportunity record type clause ────────────────────────────
        opp_expr: str | None = None
        if opportunity_rec_type is not None:
            if isinstance(opportunity_rec_type, str):
                opp_expr = (
                    f"{{{_F_OPP_REC_TYPE}}} = '{cls._escape(opportunity_rec_type)}'"
                )
            else:
                values = [v for v in opportunity_rec_type if v is not None]
                if len(values) == 1:
                    opp_expr = (
                        f"{{{_F_OPP_REC_TYPE}}} = '{cls._escape(values[0])}'"
                    )
                elif len(values) > 1:
                    or_parts = ", ".join(
                        f"{{{_F_OPP_REC_TYPE}}} = '{cls._escape(v)}'" for v in values
                    )
                    opp_expr = f"OR({or_parts})"

        # ── combine (AND between year-condition and opp-condition) ────
        parts = [p for p in (year_expr, opp_expr) if p]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return f"AND({', '.join(parts)})"

    async def _list_fundraising_records(
        self,
        *,
        formula: str | None,
        fields: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run the (sync) pyairtable call in a thread to keep FastAPI async."""
        table = self._fundraising_table()
        kwargs: dict[str, Any] = {}
        if formula:
            kwargs["formula"] = formula
        if fields:
            kwargs["fields"] = list(fields)

        try:
            return await asyncio.to_thread(table.all, **kwargs)
        except RequestException as exc:
            logger.error("Airtable request failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error")
            raise AirtableError(f"Airtable API error: {exc}") from exc

    # ── helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _amount_of(record: dict[str, Any]) -> float:
        raw = record.get("fields", {}).get(_F_AMOUNT)
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _str_field(record: dict[str, Any], name: str) -> str | None:
        value = record.get("fields", {}).get(name)
        if value is None:
            return None
        if isinstance(value, list):
            # Linked-record / multi-select fields come back as lists.
            return ", ".join(str(v) for v in value) if value else None
        return str(value)

    @staticmethod
    def _to_typed(records: list[dict[str, Any]], model_cls):
        """Convert raw Airtable records to instances of *model_cls*."""
        return [
            model_cls.model_validate({"id": r["id"], **r.get("fields", {})})
            for r in records
        ]

    async def _update_typed_record(
        self,
        table,
        record_id: str,
        fields: dict[str, Any],
        model_cls,
        *,
        id_key: str = "id",
    ):
        """Update a record by Airtable record id and return the typed result.

        Uses ``typecast=True`` so single/multi-select values can be sent
        as plain strings. Raises ``HTTPException(404)`` when the record
        does not exist.
        """
        if not record_id:
            raise HTTPException(
                status_code=_http_status.HTTP_400_BAD_REQUEST,
                detail="record_id is required.",
            )
        if not fields:
            raise HTTPException(
                status_code=_http_status.HTTP_400_BAD_REQUEST,
                detail="No fields provided to update.",
            )
        try:
            updated = await asyncio.to_thread(
                table.update, record_id, fields, typecast=True
            )
        except RequestException as exc:
            # pyairtable surfaces 404 as a RequestException with an HTTP
            # response attached; map it to a proper 404 for the client.
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 404:
                raise HTTPException(
                    status_code=_http_status.HTTP_404_NOT_FOUND,
                    detail=f"Record '{record_id}' not found.",
                ) from exc
            logger.error("Airtable update failed for %s: %s", record_id, exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during update")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return model_cls.model_validate(
            {id_key: updated["id"], **updated.get("fields", {})}
        )

    # ── public endpoints ───────────────────────────────────────────────

    async def get_total_amount_sum(
        self,
        *,
        opportunity_rec_type: str | list[str] | None,
        eq_year: int | None,
        lt_year: int | None = None,
        gt_year: int | None = None,
    ) -> AmountSumResponse:
        formula = self._build_formula(
            eq_year=eq_year,
            lt_year=lt_year,
            gt_year=gt_year,
            opportunity_rec_type=opportunity_rec_type,
        )
        records = await self._list_fundraising_records(
            formula=formula, fields=[_F_AMOUNT]
        )
        total = sum(self._amount_of(r) for r in records)
        return AmountSumResponse(total=total, record_count=len(records))

    async def get_nb_unique_accounts(
        self,
        *,
        opportunity_rec_type: str | list[str] | None,
        eq_year: int | None,
        lt_year: int | None = None,
        gt_year: int | None = None,
    ) -> UniqueAccountsResponse:
        formula = self._build_formula(
            eq_year=eq_year,
            lt_year=lt_year,
            gt_year=gt_year,
            opportunity_rec_type=opportunity_rec_type,
        )
        records = await self._list_fundraising_records(
            formula=formula, fields=[_F_ACCOUNT_NAME]
        )
        unique = {
            self._str_field(r, _F_ACCOUNT_NAME)
            for r in records
            if self._str_field(r, _F_ACCOUNT_NAME)
        }
        return UniqueAccountsResponse(
            unique_accounts=len(unique), record_count=len(records)
        )

    async def get_opportunity_rec_type_distribution(
        self,
        *,
        eq_year: int | None,
        lt_year: int | None = None,
        gt_year: int | None = None,
    ) -> DistributionResponse:
        formula = self._build_formula(
            eq_year=eq_year, lt_year=lt_year, gt_year=gt_year
        )
        records = await self._list_fundraising_records(
            formula=formula, fields=[_F_OPP_REC_TYPE]
        )

        counter: Counter[str] = Counter()
        for r in records:
            value = self._str_field(r, _F_OPP_REC_TYPE) or ""
            counter[value] += 1

        total = sum(counter.values())
        distribution = [
            DistributionItem(
                value=value,
                count=count,
                percentage=(count / total * 100.0) if total else 0.0,
            )
            for value, count in sorted(
                counter.items(), key=lambda kv: kv[1], reverse=True
            )
        ]
        return DistributionResponse(total_records=total, distribution=distribution)

    async def get_sum_amount_over_years(
        self,
        *,
        opportunity_rec_type: str | list[str] | None,
    ) -> YearlyAmountResponse:
        formula = self._build_formula(
            eq_year=None,
            lt_year=None,
            gt_year=None,
            opportunity_rec_type=opportunity_rec_type,
        )
        records = await self._list_fundraising_records(
            formula=formula, fields=[_F_AMOUNT, _F_FISCAL_YEAR]
        )

        per_year: dict[str, float] = defaultdict(float)
        for r in records:
            year = self._str_field(r, _F_FISCAL_YEAR)
            if year is None:
                continue
            per_year[year] += self._amount_of(r)

        grand_total = sum(per_year.values())
        years = [
            YearlyAmountItem(
                fiscal_year=year,
                total=amount,
                percentage=(amount / grand_total * 100.0) if grand_total else 0.0,
            )
            for year, amount in sorted(per_year.items())
        ]
        return YearlyAmountResponse(grand_total=grand_total, years=years)

    async def get_sum_amount_by_opp_rec_type(
        self,
        *,
        eq_year: int | None,
        lt_year: int | None = None,
        gt_year: int | None = None,
    ) -> OppRecTypeAmountResponse:
        formula = self._build_formula(
            eq_year=eq_year, lt_year=lt_year, gt_year=gt_year
        )
        records = await self._list_fundraising_records(
            formula=formula, fields=[_F_AMOUNT, _F_OPP_REC_TYPE]
        )

        per_type: dict[str, float] = defaultdict(float)
        for r in records:
            opp_type = self._str_field(r, _F_OPP_REC_TYPE) or ""
            per_type[opp_type] += self._amount_of(r)

        grand_total = sum(per_type.values())
        items = [
            OppRecTypeAmountItem(
                opportunity_rec_type=opp_type,
                total=amount,
                percentage=(amount / grand_total * 100.0) if grand_total else 0.0,
            )
            for opp_type, amount in sorted(
                per_type.items(), key=lambda kv: kv[1], reverse=True
            )
        ]
        return OppRecTypeAmountResponse(
            grand_total=grand_total, opportunity_rec_types=items
        )

    # ══════════════════════════════════════════════════════════════════
    # Fund & Program Tracker base
    # ══════════════════════════════════════════════════════════════════

    # ── table accessors ────────────────────────────────────────────────
    def _fp_base_id(self) -> str:
        return self._settings.AIRTABLE_FUND_PROGRAM_BASE_ID

    def _fp_table(self, table_name_or_id: str):
        return self._api.table(self._fp_base_id(), table_name_or_id)

    def _master_list_table(self):
        return self._fp_table(
            self._settings.MASTER_LIST_FUNDS_AND_SUBPROGRAMS_TABLE
        )

    def _glossary_table(self):
        return self._fp_table(self._settings.GLOSSARY_TABLE)

    def _org_friends_table(self):
        return self._fp_table(self._settings.ORG_FRIENDS_TABLE)

    def _funders_table(self):
        return self._fp_table(self._settings.FUNDERS_TABLE)

    def _awarded_opportunities_table(self):
        return self._fp_table(self._settings.AWARDED_OPPORTUNITIES_TABLE)

    def _deliverables_table(self):
        return self._fp_table(self._settings.DELIVERABLES_TABLE)

    def _monthly_checkin_table(self):
        return self._fp_table(
            self._settings.FUNDS_AND_PROGRAMS_MONTHLY_CHECKIN_TABLE
        )

    def _checkin_periods_table(self):
        return self._fp_table(self._settings.CHECKIN_REPORTING_PERIODS_TABLE)

    def _doc_titles_table(self):
        return self._fp_table(self._settings.DOC_TITLES_TABLE)

    def _shareable_docs_table(self):
        return self._fp_table(self._settings.SHAREABLE_DOCS_TABLE)

    def _clusters_table(self):
        return self._fp_table(self._settings.CLUSTERS_TABLE)

    def _onboarding_checklist_table(self):
        return self._fp_table(self._settings.ONBOARDING_CHECKLIST_TABLE)

    # ── generic record fetch (async wrapper) ───────────────────────────
    async def _list_records(
        self,
        table,
        *,
        formula: str | None = None,
        fields: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if formula:
            kwargs["formula"] = formula
        if fields:
            kwargs["fields"] = list(fields)
        try:
            return await asyncio.to_thread(table.all, **kwargs)
        except RequestException as exc:
            logger.error("Airtable request failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error")
            raise AirtableError(f"Airtable API error: {exc}") from exc

    async def _get_records_by_ids(
        self, table, ids: Iterable[str], *, fields: Iterable[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Fetch a set of records by ID and return them keyed by record id."""
        unique_ids = [i for i in {*ids} if i]
        if not unique_ids:
            return {}
        clauses = [f"RECORD_ID() = '{af.escape(i)}'" for i in unique_ids]
        formula = af.OR(*clauses)
        recs = await self._list_records(table, formula=formula, fields=fields)
        return {r["id"]: r for r in recs}

    # ── post-filter helpers ────────────────────────────────────────────
    @staticmethod
    def _linked_ids(record: dict[str, Any], field: str) -> list[str]:
        value = record.get("fields", {}).get(field)
        return value if isinstance(value, list) else []

    # ── #1 /get_funds_and_subprograms ──────────────────────────────────
    async def get_funds_and_subprograms(
        self,
        *,
        exclude_from_lists: bool | None = None,
        exclude_from_reporting: bool | None = None,
        status_list: list[str] | None = None,
        not_status_list: list[str] | None = None,
        status_empty: bool | None = None,
        sub_track_of: list[str] | None = None,
        sub_track_empty: bool | None = None,
        share_publicly: bool | None = None,
        vetting: bool | None = None,
        add_to_shareable_doc: bool | None = None,
        restricted_names: list[str] | None = None,
        scoping_prop_overview_empty: bool | None = None,
        initiative_types: list[str] | None = None,
        focus_areas: list[str] | None = None,
        excluded_clusters: list[str] | None = None,
        included_clusters: list[str] | None = None,
        fields: list[str] | None = None,
    ) -> list[MasterListFundsAndSubprogramsRecord]:
        clauses: list[str | None] = [
            af.checkbox_clause(_F_EXCLUDE_FROM_LISTS, exclude_from_lists),
            af.checkbox_clause(_F_EXCLUDE_FROM_REPORTING, exclude_from_reporting),
            af.checkbox_clause(_F_SHARE_PUBLICLY, share_publicly),
            (
                af.eq_str(_F_ONBOARDING_STATUS, _ONBOARDING_STATUS_VETTING)
                if vetting is True
                else af.neq_str(_F_ONBOARDING_STATUS, _ONBOARDING_STATUS_VETTING)
                if vetting is False
                else None
            ),
            af.checkbox_clause(_F_ADD_TO_SHAREABLE_DOC, add_to_shareable_doc),
            af.empty_clause(_F_SCOPING_PROP_OVERVIEW, scoping_prop_overview_empty),
        ]

        # Status filtering:
        #   * (status_list AND not_status_list) — combined with AND
        #   * status_empty / not-empty
        # The two groups above are unioned (OR).
        status_membership_parts: list[str | None] = []
        if status_list:
            status_membership_parts.append(af.in_str(_F_STATUS, status_list))
        if not_status_list:
            status_membership_parts.append(af.not_in_str(_F_STATUS, not_status_list))
        membership_clause = af.AND(*status_membership_parts)

        status_clauses: list[str | None] = []
        if membership_clause:
            status_clauses.append(membership_clause)
        status_clauses.append(af.empty_clause(_F_STATUS, status_empty))
        status_combined = af.OR(*status_clauses)
        if status_combined:
            clauses.append(status_combined)

        # Sub-Track Of: only the empty/not-empty condition can go to the formula.
        # Membership in sub_track_of (record IDs) must be applied in Python.
        sub_track_empty_clause = af.empty_clause(_F_SUB_TRACK_OF, sub_track_empty)
        # If both are provided, the union is enforced post-fetch.  We push the
        # empty/not-empty clause to the formula only when sub_track_of is not
        # given (otherwise we'd narrow records too aggressively).
        if sub_track_empty_clause and not sub_track_of:
            clauses.append(sub_track_empty_clause)

        if restricted_names:
            clauses.append(af.not_in_str(_F_NAME, restricted_names))

        if initiative_types:
            clauses.append(af.in_str(_F_INITIATIVE_TYPE, initiative_types))

        if focus_areas:
            clauses.append(af.multiselect_contains_any(_F_FOCUS_AREAS, focus_areas))

        # Cluster Name filtering (lookup of the linked Cluster's 'Name').
        #   * included_clusters — record must carry at least one of these names.
        #   * excluded_clusters — record must carry NONE of these names.
        # Exclusion takes precedence: any name present in both lists is
        # treated as excluded (dropped from the inclusion set).
        excluded_cluster_set = {c for c in (excluded_clusters or []) if c}
        effective_included = [
            c for c in (included_clusters or []) if c and c not in excluded_cluster_set
        ]
        if effective_included:
            clauses.append(
                af.multiselect_contains_any(_F_CLUSTER_NAME, effective_included)
            )
        if excluded_cluster_set:
            excluded_clause = af.multiselect_contains_any(
                _F_CLUSTER_NAME, sorted(excluded_cluster_set)
            )
            if excluded_clause:
                clauses.append(f"NOT({excluded_clause})")

        formula = af.AND(*clauses)
        records = await self._list_records(
            self._master_list_table(), formula=formula, fields=fields
        )

        # Post-filter: Sub-Track Of membership / empty union.
        if sub_track_of or sub_track_empty is not None:
            allowed_ids = set(sub_track_of or [])
            def _passes(rec: dict[str, Any]) -> bool:
                linked = self._linked_ids(rec, _F_SUB_TRACK_OF)
                in_list = bool(allowed_ids.intersection(linked)) if sub_track_of else False
                empty_match = (
                    (sub_track_empty is True and not linked)
                    or (sub_track_empty is False and bool(linked))
                ) if sub_track_empty is not None else False
                if sub_track_of and sub_track_empty is not None:
                    return in_list or empty_match
                if sub_track_of:
                    return in_list
                return empty_match

            records = [r for r in records if _passes(r)]

        # Enrich the "Upcoming Deliverables" lookup whenever it is part of the
        # response. The lookup carries raw linked Deliverables record ids
        # (its source is a linked-record field), which are resolved here into
        # the full Deliverables records (all fields).
        enrich_deliverables = fields is None or _UPCOMING_DELIVERABLES_FIELD in fields
        if enrich_deliverables:
            id_set: set[str] = set()
            for r in records:
                for rid in (
                    r.get("fields", {}).get(_UPCOMING_DELIVERABLES_FIELD) or []
                ):
                    if isinstance(rid, str):
                        id_set.add(rid)

            if id_set:
                deliverable_recs = await self._get_records_by_ids(
                    self._deliverables_table(), id_set
                )
                for r in records:
                    ids = r.get("fields", {}).get(_UPCOMING_DELIVERABLES_FIELD)
                    if not ids:
                        continue
                    r["fields"][_UPCOMING_DELIVERABLES_FIELD] = [
                        {
                            "id": rid,
                            **(deliverable_recs.get(rid, {}).get("fields", {})),
                        }
                        for rid in ids
                        if isinstance(rid, str)
                    ]

        return self._to_typed(records, MasterListFundsAndSubprogramsRecord)

    # ── #2 /get_glossary_data ──────────────────────────────────────────
    async def get_glossary_data(
        self, *, fields: list[str] | None = None
    ) -> list[GlossaryRecord]:
        records = await self._list_records(self._glossary_table(), fields=fields)
        return self._to_typed(records, GlossaryRecord)

    # ── #3 /get_org_friends ────────────────────────────────────────────
    async def get_org_friends(
        self, *, fields: list[str] | None = None
    ) -> list[OrgFriendsRecord]:
        records = await self._list_records(self._org_friends_table(), fields=fields)
        return self._to_typed(records, OrgFriendsRecord)

    # ── #4 /get_funders ────────────────────────────────────────────────
    async def get_funders(
        self, *, fields: list[str] | None = None
    ) -> list[FundersRecord]:
        records = await self._list_records(self._funders_table(), fields=fields)
        return self._to_typed(records, FundersRecord)

    # ── /get_awarded_opportunities ────────────────────────────────────
    async def get_awarded_opportunities(
        self, *, fields: list[str] | None = None
    ) -> list[AwardedOpportunityRecord]:
        records = await self._list_records(
            self._awarded_opportunities_table(), fields=fields
        )

        # Enrich the Master List lookup field whenever it is part of the
        # response. The lookup carries raw linked-record IDs (the source is
        # a linked-record field), which would otherwise be unreadable.
        enrich = fields is None or _ML_LOOKUP_FIELD in fields
        if enrich:
            id_set: set[str] = set()
            for r in records:
                for rid in (r.get("fields", {}).get(_ML_LOOKUP_FIELD) or []):
                    if isinstance(rid, str):
                        id_set.add(rid)

            if id_set:
                ml_recs = await self._get_records_by_ids(
                    self._master_list_table(),
                    id_set,
                    fields=_ML_LOOKUP_PROJECT_FIELDS,
                )
                for r in records:
                    ids = r.get("fields", {}).get(_ML_LOOKUP_FIELD)
                    if not ids:
                        continue
                    r["fields"][_ML_LOOKUP_FIELD] = [
                        {
                            "id": rid,
                            **(ml_recs.get(rid, {}).get("fields", {})),
                        }
                        for rid in ids
                        if isinstance(rid, str)
                    ]

        return self._to_typed(records, AwardedOpportunityRecord)

    # ── shared base filters for monthly check-in endpoints ────────────
    @staticmethod
    def _user_filter_clause(user_id: str | None) -> str | None:
        """Reporting Lead user-id filter (uses FIND on the user-id within the
        formula representation of the user field)."""
        if not user_id:
            return None
        # User fields render as the user's display info; we filter by id via
        # the raw id appearing in the field's serialization. The most robust
        # approach is post-filtering, but for simple cases ``FIND`` works when
        # the field is configured with id collaborator info exposed.  To stay
        # safe we fall back to post-filtering — return ``None`` here so the
        # caller knows to apply the filter in Python.
        return None

    @classmethod
    def _filter_by_user_id(
        cls,
        records: list[dict[str, Any]],
        *,
        field: str,
        user_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        if not user_ids:
            return records
        target = set(user_ids)
        out: list[dict[str, Any]] = []
        for r in records:
            value = r.get("fields", {}).get(field)
            ids = cls._collect_user_ids(value)
            if ids & target:
                out.append(r)
        return out

    @staticmethod
    def _collect_user_ids(value: Any) -> set[str]:
        """Extract Airtable user ids from a User / multi-User field value."""
        if value is None:
            return set()
        items = value if isinstance(value, list) else [value]
        ids: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                _id = item.get("id")
                if _id:
                    ids.add(_id)
            elif isinstance(item, str):
                ids.add(item)
        return ids

    @staticmethod
    def _filter_by_linked_id(
        records: list[dict[str, Any]],
        *,
        field: str,
        target_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        if not target_ids:
            return records
        wanted = set(target_ids)
        return [
            r for r in records
            if wanted & set(r.get("fields", {}).get(field) or [])
        ]

    @staticmethod
    def _filter_by_lookup_contains(
        records: list[dict[str, Any]],
        *,
        field: str,
        targets: list[str] | None,
    ) -> list[dict[str, Any]]:
        """Filter records whose lookup field array contains any of ``targets``."""
        if not targets:
            return records
        wanted = set(targets)
        out: list[dict[str, Any]] = []
        for r in records:
            value = r.get("fields", {}).get(field)
            if isinstance(value, list):
                if wanted & set(value):
                    out.append(r)
            elif value in wanted:
                out.append(r)
        return out

    async def _filter_by_program_attr(
        self,
        records: list[dict[str, Any]],
        *,
        program_field: str = _F_PROGRAM_NAME,
        checkin_user_id: str | None = None,
        not_program_status: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Filter records whose linked program (in MASTER_LIST_FUNDS_AND_SUBPROGRAMS_TABLE)
        satisfies optional constraints on Check-In History (collaborator id) and Status.
        """
        excluded_statuses = set(not_program_status or [])
        if not checkin_user_id and not excluded_statuses:
            return records

        program_ids: set[str] = set()
        for r in records:
            program_ids.update(self._linked_ids(r, program_field))

        program_fields_needed = []
        if checkin_user_id:
            program_fields_needed.append(_F_CHECKIN_HISTORY)
        if excluded_statuses:
            program_fields_needed.append(_F_STATUS)

        programs = await self._get_records_by_ids(
            self._master_list_table(),
            program_ids,
            fields=program_fields_needed or None,
        )

        def _program_passes(prog_rec: dict[str, Any]) -> bool:
            pf = prog_rec.get("fields", {})
            if checkin_user_id:
                history = pf.get(_F_CHECKIN_HISTORY)
                user_ids = self._collect_user_ids(history)
                if checkin_user_id not in user_ids:
                    return False
            if excluded_statuses:
                status_val = pf.get(_F_STATUS)
                if isinstance(status_val, list):
                    status_val = status_val[0] if status_val else None
                if status_val in excluded_statuses:
                    return False
            return True

        out: list[dict[str, Any]] = []
        for r in records:
            ids = self._linked_ids(r, program_field)
            if not ids:
                if excluded_statuses and not checkin_user_id:
                    # No program linked → its status can't equal an excluded
                    # value, so it passes.
                    out.append(r)
                continue
            # Record passes when *any* linked program passes.
            if any(_program_passes(programs[pid]) for pid in ids if pid in programs):
                out.append(r)
        return out

    # ── #5 /get_funds_progs_monthly_checkin ────────────────────────────
    async def get_funds_progs_monthly_checkin(
        self,
        *,
        eq_days_until_deadline: int | None = None,
        lt_days_until_deadline: int | None = None,
        gt_days_until_deadline: int | None = None,
        submission_extension: bool | None = None,
        user_id: str | None = None,
        checkin_user_id: str | None = None,
        not_program_status: list[str] | None = None,
        report_complete: bool | None = None,
        flag_for_discussion: bool | None = None,
        followup_indicated_not_empty: bool | None = None,
        fields: list[str] | None = None,
    ) -> list[MonthlyCheckinRecord]:
        # Base AND clauses (excluding fields requiring program lookup or user id).
        base_clauses: list[str | None] = [
            af.checkbox_clause(_F_REPORT_COMPLETE, report_complete),
            af.checkbox_clause(_F_FLAG_FOR_DISCUSSION, flag_for_discussion),
        ]

        # Followup Indicated filter:
        #   True  → field is not empty
        #   False → field is unchecked
        #   None  → no filter
        if followup_indicated_not_empty is True:
            base_clauses.append(af.is_not_empty(_F_FOLLOWUP_INDICATED))
        elif followup_indicated_not_empty is False:
            base_clauses.append(af.is_unchecked(_F_FOLLOWUP_INDICATED))

        # Block 1 — OR of days_until_deadline conditions
        block1 = af.year_clauses(
            eq=eq_days_until_deadline,
            lt=lt_days_until_deadline,
            gt=gt_days_until_deadline,
            field=_F_DAYS_UNTIL_DEADLINE,
        )
        # Block 2 — submission_extension checkbox
        block2 = af.checkbox_clause(_F_SUBMISSION_EXTENSION, submission_extension)
        union_block = af.OR(block1, block2)
        if union_block:
            base_clauses.append(union_block)

        formula = af.AND(*base_clauses)

        # When the caller projects a subset of fields, make sure the columns
        # required by post-filters and by the Program Name enrichment are
        # always fetched so they cannot be silently dropped.
        effective_fields: list[str] | None = None
        if fields is not None:
            required = [_F_PROGRAM_NAME]
            if user_id:
                required.append(_F_REPORTING_LEAD)
            effective_fields = list(dict.fromkeys([*fields, *required]))

        records = await self._list_records(
            self._monthly_checkin_table(),
            formula=formula,
            fields=effective_fields,
        )

        # Post-filters
        records = self._filter_by_user_id(
            records, field=_F_REPORTING_LEAD,
            user_ids=[user_id] if user_id else None,
        )
        records = await self._filter_by_program_attr(
            records,
            checkin_user_id=checkin_user_id,
            not_program_status=not_program_status,
        )

        # Enrich the linked Program Name field with the full Master List
        # of Funds & Sub-Programs rows (same pattern as get_shareable_docs).
        linked_ids: set[str] = set()
        for r in records:
            for rid in self._linked_ids(r, _F_PROGRAM_NAME):
                if isinstance(rid, str):
                    linked_ids.add(rid)

        if linked_ids:
            lookup = await self._get_records_by_ids(
                self._master_list_table(), linked_ids
            )
            for r in records:
                ids = r.get("fields", {}).get(_F_PROGRAM_NAME)
                if not ids:
                    continue
                r["fields"][_F_PROGRAM_NAME] = [
                    {
                        "id": rid,
                        **(lookup.get(rid, {}).get("fields", {})),
                    }
                    for rid in ids
                    if isinstance(rid, str)
                ]

        return self._to_typed(records, MonthlyCheckinRecord)

    # ── shared base filters for the count / distribution endpoints ────
    async def _filter_monthly_checkin_common(
        self,
        records: list[dict[str, Any]],
        *,
        clusters: list[str] | None,
        user_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        records = self._filter_by_lookup_contains(
            records, field=_F_CLUSTER, targets=clusters
        )
        records = self._filter_by_user_id(
            records, field=_F_REPORTING_LEAD, user_ids=user_ids
        )
        return records

    @staticmethod
    def _build_monthly_checkin_base_formula(
        *,
        flag_for_discussion: bool | None = None,
        report_complete: bool | None = None,
        followup_indicated_empty: bool | None = None,
        checkin_in_reporting_period: str | None = None,
        program_name: str | None = None,
        status_list: list[str] | None = None,
    ) -> str | None:
        """Build the ``filterByFormula`` for filters expressible directly."""
        clauses: list[str | None] = [
            af.checkbox_clause(_F_FLAG_FOR_DISCUSSION, flag_for_discussion),
            af.checkbox_clause(_F_REPORT_COMPLETE, report_complete),
            af.empty_clause(_F_FOLLOWUP_INDICATED, followup_indicated_empty),
        ]
        # Linked record filters (Program Name / Check-In Reporting Period)
        # cannot be reliably filtered via formula on record IDs; they are
        # post-filtered in Python.  Status is a single select, so an IN
        # match is fine.
        if status_list:
            clauses.append(af.in_str(_F_STATUS, status_list))
        return af.AND(*clauses)

    # ── #6 /get_funds_progs_monthly_checkin_count ─────────────────────
    async def get_funds_progs_monthly_checkin_count(
        self,
        *,
        flag_for_discussion: bool | None = None,
        checkin_in_reporting_periods: list[str] | None = None,
        clusters: list[str] | None = None,
        program_names: list[str] | None = None,
        status_list: list[str] | None = None,
        user_ids: list[str] | None = None,
    ) -> CountResponse:
        formula = self._build_monthly_checkin_base_formula(
            flag_for_discussion=flag_for_discussion,
            status_list=status_list,
        )
        records = await self._list_records(
            self._monthly_checkin_table(),
            formula=formula,
            fields=[
                _F_PROGRAM_NAME,
                _F_CHECKIN_REPORTING_PERIOD,
                _F_CLUSTER,
                _F_REPORTING_LEAD,
            ],
        )
        records = self._filter_by_linked_id(
            records, field=_F_CHECKIN_REPORTING_PERIOD,
            target_ids=checkin_in_reporting_periods,
        )
        records = self._filter_by_linked_id(
            records, field=_F_PROGRAM_NAME, target_ids=program_names
        )
        records = await self._filter_monthly_checkin_common(
            records, clusters=clusters, user_ids=user_ids
        )
        return CountResponse(count=len(records))

    # ── #7 /get_funds_progs_status_distribution ───────────────────────
    async def get_funds_progs_status_distribution(
        self,
        *,
        checkin_in_reporting_period: str | None = None,
        cluster: str | None = None,
        program_name: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
    ) -> DistributionResponse:
        formula = self._build_monthly_checkin_base_formula(
            status_list=[status] if status else None,
        )
        records = await self._list_records(
            self._monthly_checkin_table(),
            formula=formula,
            fields=[
                _F_DASHBOARD_DISPLAY,
                _F_PROGRAM_NAME,
                _F_CHECKIN_REPORTING_PERIOD,
                _F_CLUSTER,
                _F_REPORTING_LEAD,
            ],
        )
        records = self._filter_by_linked_id(
            records, field=_F_CHECKIN_REPORTING_PERIOD,
            target_ids=[checkin_in_reporting_period] if checkin_in_reporting_period else None,
        )
        records = self._filter_by_linked_id(
            records, field=_F_PROGRAM_NAME,
            target_ids=[program_name] if program_name else None,
        )
        records = await self._filter_monthly_checkin_common(
            records,
            clusters=[cluster] if cluster else None,
            user_ids=[user_id] if user_id else None,
        )

        counter: Counter[str] = Counter()
        for r in records:
            value = self._str_field(r, _F_DASHBOARD_DISPLAY) or ""
            counter[value] += 1

        total = sum(counter.values())
        distribution = [
            DistributionItem(
                value=value,
                count=count,
                percentage=(count / total * 100.0) if total else 0.0,
            )
            for value, count in sorted(
                counter.items(), key=lambda kv: kv[1], reverse=True
            )
        ]
        return DistributionResponse(total_records=total, distribution=distribution)

    # ── #8 /get_reports_with_followups ────────────────────────────────
    async def get_reports_with_followups(
        self,
        *,
        follow_indicated_empty: bool | None = None,
        report_complete: bool | None = None,
        flag_for_discussion: bool | None = None,
        checkin_in_reporting_period: str | None = None,
        cluster: str | None = None,
        program_name: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
        fields: list[str] | None = None,
    ) -> list[MonthlyCheckinRecord]:
        formula = self._build_monthly_checkin_base_formula(
            flag_for_discussion=flag_for_discussion,
            report_complete=report_complete,
            followup_indicated_empty=follow_indicated_empty,
            status_list=[status] if status else None,
        )
        records = await self._list_records(
            self._monthly_checkin_table(),
            formula=formula,
            fields=fields,
        )
        records = self._filter_by_linked_id(
            records, field=_F_CHECKIN_REPORTING_PERIOD,
            target_ids=[checkin_in_reporting_period] if checkin_in_reporting_period else None,
        )
        records = self._filter_by_linked_id(
            records, field=_F_PROGRAM_NAME,
            target_ids=[program_name] if program_name else None,
        )
        records = await self._filter_monthly_checkin_common(
            records,
            clusters=[cluster] if cluster else None,
            user_ids=[user_id] if user_id else None,
        )
        return self._to_typed(records, MonthlyCheckinRecord)

    # ── #9 /get_checkin_reporting_periods ─────────────────────────────
    async def get_checkin_reporting_periods(
        self,
        *,
        date_filters: list[DateRangeFilter] | None = None,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # OR together every clause from every filter object.  Within each
        # object, eq/lt/gt are also OR-combined; across objects they are
        # likewise OR-combined — so the result is a flat OR of all
        # provided predicates.
        parts: list[str] = []
        for f in date_filters or []:
            clause = af.date_clauses(
                eq=f.eq_date, lt=f.lt_date, gt=f.gt_date, field=_F_DEADLINE
            )
            if clause:
                parts.append(clause)
        formula = af.OR(*parts)
        return await self._list_records(
            self._checkin_periods_table(), formula=formula, fields=fields
        )

    # ── #10 /get_recent_complete_reports ──────────────────────────────
    async def get_recent_complete_reports(
        self,
        *,
        report_complete: bool | None = None,
        eq_days_until_deadline: int | None = None,
        lt_days_until_deadline: int | None = None,
        gt_days_until_deadline: int | None = None,
        eq_review_until: str | None = None,
        lt_review_until: str | None = None,
        gt_review_until: str | None = None,
        fields: list[str] | None = None,
    ) -> list[MonthlyCheckinRecord]:
        report_clause = af.checkbox_clause(_F_REPORT_COMPLETE, report_complete)

        review_union = af.date_clauses(
            eq=eq_review_until,
            lt=lt_review_until,
            gt=gt_review_until,
            field=_F_REVIEW_UNTIL,
        )
        days_union = af.year_clauses(
            eq=eq_days_until_deadline,
            lt=lt_days_until_deadline,
            gt=gt_days_until_deadline,
            field=_F_DAYS_UNTIL_DEADLINE,
        )

        block1 = af.AND(report_clause, review_union) if review_union else None
        block2 = af.AND(report_clause, days_union) if days_union else None

        if block1 is None and block2 is None:
            # No date / days filter provided — fall back to just report_complete
            formula = report_clause
        else:
            formula = af.OR(block1, block2)

        records = await self._list_records(
            self._monthly_checkin_table(), formula=formula, fields=fields
        )
        return self._to_typed(records, MonthlyCheckinRecord)

    # ── #11 /get_archived_reports_by_program ──────────────────────────
    async def get_archived_reports_by_program(
        self,
        *,
        report_complete: bool | None = None,
        not_program_status: str | None = None,
        fields: list[str] | None = None,
    ) -> list[MonthlyCheckinRecord]:
        formula = af.checkbox_clause(_F_REPORT_COMPLETE, report_complete)
        records = await self._list_records(
            self._monthly_checkin_table(), formula=formula, fields=fields
        )
        records = await self._filter_by_program_attr(
            records,
            not_program_status=[not_program_status] if not_program_status else None,
        )
        return self._to_typed(records, MonthlyCheckinRecord)

    # ── #12 /get_doc_titles ───────────────────────────────────────────
    async def get_doc_titles(
        self, *, fields: list[str] | None = None
    ) -> list[DocTitleRecord]:
        records = await self._list_records(self._doc_titles_table(), fields=fields)
        return self._to_typed(records, DocTitleRecord)

    # ── #20 /get_shareable_docs ───────────────────────────────────────
    async def get_shareable_docs(
        self, *, fields: list[str] | None = None
    ) -> list[ShareableDocsRecord]:
        records = await self._list_records(
            self._shareable_docs_table(), fields=fields
        )

        # Expand the "Programs" linked-record field into full Master List
        # of Funds & Sub-Programs rows, mirroring the onboarding-checklist
        # enrichment pattern.
        linked_ids: set[str] = set()
        for r in records:
            for rid in self._linked_ids(r, "Programs"):
                if isinstance(rid, str):
                    linked_ids.add(rid)

        if linked_ids:
            lookup = await self._get_records_by_ids(
                self._master_list_table(), linked_ids
            )
            for r in records:
                ids = r.get("fields", {}).get("Programs")
                if not ids:
                    continue
                r["fields"]["Programs"] = [
                    {
                        "id": rid,
                        **(lookup.get(rid, {}).get("fields", {})),
                    }
                    for rid in ids
                    if isinstance(rid, str)
                ]

        return self._to_typed(records, ShareableDocsRecord)

    # ── /get_onboarding_checklist ─────────────────────────────────────
    async def get_onboarding_checklist(
        self, *, fields: list[str] | None = None
    ) -> list[OnboardingChecklistRecord]:
        records = await self._list_records(
            self._onboarding_checklist_table(), fields=fields
        )

        linked_ids: set[str] = set()
        for r in records:
            for rid in self._linked_ids(r, _F_OC_MASTER_LIST_FUNDS_SUBPROGRAMS):
                if isinstance(rid, str):
                    linked_ids.add(rid)

        if linked_ids:
            lookup = await self._get_records_by_ids(
                self._master_list_table(), linked_ids
            )
            for r in records:
                ids = r.get("fields", {}).get(_F_OC_MASTER_LIST_FUNDS_SUBPROGRAMS)
                if not ids:
                    continue
                r["fields"][_F_OC_MASTER_LIST_FUNDS_SUBPROGRAMS] = [
                    {
                        "id": rid,
                        **(lookup.get(rid, {}).get("fields", {})),
                    }
                    for rid in ids
                    if isinstance(rid, str)
                ]

        return self._to_typed(records, OnboardingChecklistRecord)

    # ── #14 /get_unique_checkin_reporting_periods ─────────────────────
    async def get_unique_checkin_reporting_periods(
        self,
    ) -> list[CheckinReportingPeriodRecord]:
        records = await self._list_records(
            self._monthly_checkin_table(),
            fields=[_F_CHECKIN_REPORTING_PERIOD],
        )
        ids: set[str] = set()
        for r in records:
            ids.update(self._linked_ids(r, _F_CHECKIN_REPORTING_PERIOD))

        if not ids:
            return []

        periods = await self._get_records_by_ids(
            self._checkin_periods_table(), ids, fields=[_F_PERIOD]
        )
        return [
            CheckinReportingPeriodRecord(
                record_id=pid,
                period=self._str_field(periods[pid], _F_PERIOD)
                if pid in periods else None,
            )
            for pid in sorted(ids)
        ]

    # ── #15 /get_clusters ─────────────────────────────────────────────
    async def get_clusters(self) -> list[ClusterRecord]:
        records = await self._list_records(
            self._monthly_checkin_table(), fields=[_F_CLUSTER]
        )
        ids: set[str] = set()
        for r in records:
            value = r.get("fields", {}).get(_F_CLUSTER)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith("rec"):
                        ids.add(item)

        if not ids:
            return []

        clusters = await self._get_records_by_ids(
            self._clusters_table(), ids, fields=[_F_NAME]
        )
        return [
            ClusterRecord(
                record_id=cid,
                name=self._str_field(clusters[cid], _F_NAME)
                if cid in clusters else None,
            )
            for cid in sorted(ids)
        ]

    # ── #16 /get_program_names ────────────────────────────────────────
    async def get_program_names(
        self,
        *,
        add_to_sharable_doc: bool | None = None,
    ) -> list[IdNameItem]:
        records = await self._list_records(
            self._monthly_checkin_table(), fields=[_F_PROGRAM_NAME]
        )
        ids: set[str] = set()
        for r in records:
            ids.update(self._linked_ids(r, _F_PROGRAM_NAME))

        if not ids:
            return []

        program_fields = [_F_NAME]
        if add_to_sharable_doc is not None:
            program_fields.append(_F_ADD_TO_SHAREABLE_DOC)

        programs = await self._get_records_by_ids(
            self._master_list_table(), ids, fields=program_fields
        )

        if add_to_sharable_doc is not None:
            ids = {
                pid for pid in ids
                if pid in programs
                and bool(
                    programs[pid].get("fields", {}).get(_F_ADD_TO_SHAREABLE_DOC)
                ) is add_to_sharable_doc
            }

        return [
            IdNameItem(
                id=pid,
                name=self._str_field(programs[pid], _F_NAME)
                if pid in programs else None,
            )
            for pid in sorted(ids)
        ]

    # ── #17 /get_status_values ────────────────────────────────────────
    async def get_status_values(self) -> list[str]:
        records = await self._list_records(
            self._monthly_checkin_table(), fields=[_F_STATUS]
        )
        unique: set[str] = set()
        for r in records:
            value = self._str_field(r, _F_STATUS)
            if value:
                unique.add(value)
        return sorted(unique)

    # ── #18 /get_reporting_leads ──────────────────────────────────────
    async def get_reporting_leads(self) -> list[dict[str, Any]]:
        records = await self._list_records(
            self._monthly_checkin_table(), fields=[_F_REPORTING_LEAD]
        )
        seen: dict[str, dict[str, Any]] = {}
        for r in records:
            value = r.get("fields", {}).get(_F_REPORTING_LEAD)
            items = value if isinstance(value, list) else [value] if value else []
            for item in items:
                if isinstance(item, dict):
                    _id = item.get("id")
                    if _id and _id not in seen:
                        seen[_id] = {
                            "id": _id,
                            "email": item.get("email"),
                            "name": item.get("name"),
                        }
        return sorted(seen.values(), key=lambda v: (v.get("name") or "").lower())

    # ── #19 /get_airtable_user_id ─────────────────────────────────────
    async def get_airtable_user_id(self, email: str) -> AirtableUserIdResponse:
        leads = await self.get_reporting_leads()
        target = email.strip().lower()
        for lead in leads:
            lead_email = (lead.get("email") or "").lower()
            if lead_email == target:
                return AirtableUserIdResponse(
                    id=lead.get("id"),
                    email=lead.get("email"),
                    name=lead.get("name"),
                )
        return AirtableUserIdResponse(id=None, email=email, name=None)

    # ── #20 /get_active_programs_count ────────────────────────────────
    async def get_active_programs_count(self) -> CountResponse:
        """Count records in MASTER_LIST where Status is an active-program status.

        A program is considered active when its Status equals either
        'Active Program' or 'Publicly Launched'. Records are additionally
        required to have an empty 'Sub-Track Of' and an unchecked
        'Exclude from lists'.
        """
        formula = af.AND(
            af.in_str(_F_STATUS, list(_ACTIVE_PROGRAM_STATUSES)),
            af.is_empty(_F_SUB_TRACK_OF),
            af.is_unchecked(_F_EXCLUDE_FROM_LISTS),
        )
        records = await self._list_records(
            self._master_list_table(), formula=formula, fields=[_F_STATUS]
        )
        return CountResponse(count=len(records))

    # ── /get_active_programs ──────────────────────────────────────────
    async def get_active_programs(self) -> list[ActiveProgramItem]:
        """List active programs with their lead/fellow assignment.

        Reads records from MASTER_LIST whose Status equals
        'Active Program' or 'Publicly Launched', 'Sub-Track Of' is empty,
        and 'Exclude from lists' is unchecked, returning the 'Name' and
        'Program Lead/Fellow' fields.
        """
        formula = af.AND(
            af.in_str(_F_STATUS, list(_ACTIVE_PROGRAM_STATUSES)),
            af.is_empty(_F_SUB_TRACK_OF),
            af.is_unchecked(_F_EXCLUDE_FROM_LISTS),
        )
        records = await self._list_records(
            self._master_list_table(),
            formula=formula,
            fields=[_F_NAME, _F_PROGRAM_LEAD_FELLOW],
        )
        items: list[ActiveProgramItem] = []
        for r in records:
            fields = r.get("fields", {}) or {}
            items.append(
                ActiveProgramItem(
                    id=r["id"],
                    name=fields.get(_F_NAME),
                    program_lead_fellow=fields.get(_F_PROGRAM_LEAD_FELLOW),
                )
            )
        items.sort(key=lambda x: (x.name or "").lower())
        return items

    # ── #21 /get_distinct_fellows_count ───────────────────────────────
    async def get_distinct_fellows_count(self) -> CountResponse:
        """Count distinct fellows sourced from the Master List.

        Fellows are derived from the Master List: records whose Status
        equals 'Fellowship (Scoping)' contribute their 'Program Lead/Fellow'
        name(s). Names are resolved to Users by matching the 'Name' field;
        each matched user contributes its (lower-cased) Work Email to the
        distinct set, and each unmatched name contributes its (lower-cased)
        name to the same set. The count is the size of that set.
        """
        entries = await self._fetch_fellow_entries()
        s = self._settings
        unique: set[str] = set()
        for name, user in entries:
            if user is not None:
                email = (user.get("fields", {}) or {}).get(s.USERS_WORK_EMAIL_FIELD)
                key = str(email).strip().lower() if email else ""
                if key:
                    unique.add(key)
                    continue
            # Unmatched (or matched user with no Work Email) → count by name.
            name_key = name.strip().lower()
            if name_key:
                unique.add(name_key)
        return CountResponse(count=len(unique))

    # ── /get_distinct_fellows ─────────────────────────────────────────
    async def get_distinct_fellows(self) -> list[PersonContactItem]:
        """List unique fellows with their First Name, Last Name and Work Email.

        Uses the same source as :meth:`get_distinct_fellows_count`.
        Unmatched Program Lead/Fellow names are returned with an empty
        ``work_email``; the raw name is split heuristically on the first
        whitespace to populate ``first_name`` / ``last_name``.
        """
        entries = await self._fetch_fellow_entries(include_names=True)
        s = self._settings
        seen_emails: set[str] = set()
        seen_names: set[str] = set()
        items: list[PersonContactItem] = []
        for name, user in entries:
            if user is not None:
                fields = user.get("fields", {}) or {}
                email = fields.get(s.USERS_WORK_EMAIL_FIELD)
                email_key = str(email).strip().lower() if email else ""
                if email_key:
                    if email_key in seen_emails:
                        continue
                    seen_emails.add(email_key)
                items.append(
                    PersonContactItem(
                        first_name=fields.get(s.USERS_FIRST_NAME_FIELD),
                        last_name=fields.get(s.USERS_LAST_NAME_FIELD),
                        work_email=email,
                        office_location=fields.get(s.USERS_OFFICE_LOCATION_FIELD),
                        programs=self._normalize_program_names(
                            fields.get(s.USERS_PROGRAM_NAMES_FIELD)
                        ),
                    )
                )
                continue

            # Unmatched name: emit with no email.
            name_key = name.strip().lower()
            if not name_key or name_key in seen_names:
                continue
            seen_names.add(name_key)
            first, last = self._split_full_name(name)
            items.append(
                PersonContactItem(
                    first_name=first,
                    last_name=last,
                    work_email=None,
                )
            )
        items.sort(
            key=lambda x: (
                (x.last_name or "").lower(),
                (x.first_name or "").lower(),
            )
        )
        return items

    @staticmethod
    def _split_full_name(full_name: str) -> tuple[str | None, str | None]:
        """Split a full name on the first whitespace.

        Returns ``(first_name, last_name)``. If the input contains no
        whitespace, the whole string is returned as ``last_name`` and
        ``first_name`` is ``None``.
        """
        parts = (full_name or "").strip().split(None, 1)
        if not parts:
            return None, None
        if len(parts) == 1:
            return None, parts[0]
        return parts[0], parts[1]

    @staticmethod
    def _extract_lead_fellow_names(value: Any) -> list[str]:
        """Normalize a 'Program Lead/Fellow' field value into a list of names.

        Handles the common shapes the field may take:
          * ``None`` / empty → ``[]``
          * ``str`` → split on commas, trimmed
          * ``list`` → each item may be a ``str`` or a collaborator-like
            ``dict`` with a ``"name"`` key
        Blank names are dropped; order is preserved (first occurrence wins).
        """
        if not value:
            return []

        candidates: list[str] = []

        if isinstance(value, str):
            candidates = [part.strip() for part in value.split(",")]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    candidates.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        candidates.append(name.strip())
        # Any other shape is ignored.

        seen: set[str] = set()
        names: list[str] = []
        for name in candidates:
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    @staticmethod
    def _normalize_program_names(value: Any) -> list[str]:
        """Normalize a 'Program Names' lookup value into a list of names.

        The lookup may return a list (one entry per linked record, each of
        which may itself hold comma-separated values) or a single string.
        Values are split on commas, trimmed and de-duplicated (first wins).
        """
        if not value:
            return []

        raw: list[str] = []
        if isinstance(value, str):
            raw = [value]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    raw.append(item)
                elif isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        raw.append(name)

        seen: set[str] = set()
        names: list[str] = []
        for chunk in raw:
            for part in chunk.split(","):
                part = part.strip()
                if not part:
                    continue
                key = part.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(part)
        return names

    async def _fetch_fellow_entries(
        self, *, include_names: bool = False
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Return one entry per Program Lead/Fellow, matched to a User when possible.

        1. Query MASTER_LIST for records with Status = 'Fellowship (Scoping)',
           projecting the 'Program Lead/Fellow' field.
        2. Extract the list of unique lead/fellow names.
        3. Query USERS matching those names against the 'Name' field,
           projecting Work Email (and First/Last Name when requested).
        4. Emit ``(name, user_record)`` for matched names and
           ``(name, None)`` for names with no matching user.
        """
        s = self._settings

        # Step 1: pull Fellowship (Scoping) programs from the Master List.
        program_formula = af.eq_str(_F_STATUS, _STATUS_FELLOWSHIP_SCOPING)
        program_records = await self._list_records(
            self._master_list_table(),
            formula=program_formula,
            fields=[_F_PROGRAM_LEAD_FELLOW],
        )

        # Step 2: collect unique lead/fellow names (preserving first-seen order).
        seen: set[str] = set()
        names: list[str] = []
        for r in program_records:
            value = (r.get("fields", {}) or {}).get(_F_PROGRAM_LEAD_FELLOW)
            for name in self._extract_lead_fellow_names(value):
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(name)

        if not names:
            return []

        # Step 3: look those names up in the Users table by Name.
        user_formula = af.in_str(s.USERS_NAME_FIELD, names)
        fields = [s.USERS_NAME_FIELD, s.USERS_WORK_EMAIL_FIELD]
        if include_names:
            fields = [
                s.USERS_NAME_FIELD,
                s.USERS_FIRST_NAME_FIELD,
                s.USERS_LAST_NAME_FIELD,
                s.USERS_WORK_EMAIL_FIELD,
                s.USERS_OFFICE_LOCATION_FIELD,
                s.USERS_PROGRAM_NAMES_FIELD,
            ]
        user_records = await self._list_records(
            self._users_table(), formula=user_formula, fields=fields
        )

        # Step 4: build a lower-cased Name → user record index, then
        # emit one entry per requested name (matched or not).
        by_name: dict[str, dict[str, Any]] = {}
        for r in user_records:
            user_name = (r.get("fields", {}) or {}).get(s.USERS_NAME_FIELD)
            if not isinstance(user_name, str):
                continue
            key = user_name.strip().lower()
            if key and key not in by_name:
                by_name[key] = r

        return [(name, by_name.get(name.lower())) for name in names]

    # ══════════════════════════════════════════════════════════════════
    # Announcements (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════════
    def _announcements_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ANNOUNCEMENTS_TABLE,
        )

    # Announcement field name constants (loaded from settings/.env)
    _F_ANN_ID = _S.AT_F_ANN_ID
    _F_ANN_TITLE = _S.AT_F_ANN_TITLE
    _F_ANN_CONTENT = _S.AT_F_ANN_CONTENT
    _F_ANN_AUTHOR_EMAIL = _S.AT_F_ANN_AUTHOR_EMAIL
    _F_ANN_CATEGORY = _S.AT_F_ANN_CATEGORY
    _F_ANN_ATTACHMENTS = _S.AT_F_ANN_ATTACHMENTS
    _F_ANN_REVIEWER_COMMENTS = _S.AT_F_ANN_REVIEWER_COMMENTS
    _F_ANN_PRIORITY = _S.AT_F_ANN_PRIORITY
    _F_ANN_APPROVED = _S.AT_F_ANN_APPROVED
    _F_ANN_STATUS = _S.AT_F_ANN_STATUS
    _F_ANN_PUBLISH_TIME = _S.AT_F_ANN_PUBLISH_TIME
    _F_ANN_EXPIRATION_TIME = _S.AT_F_ANN_EXPIRATION_TIME
    _F_ANN_APPROVED_BY = _S.AT_F_ANN_APPROVED_BY

    @staticmethod
    def _attachments_payload(urls: list[str] | None) -> list[dict[str, str]] | None:
        if urls is None:
            return None
        return [{"url": u} for u in urls]

    @staticmethod
    def _iso(value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return value.isoformat()

    async def create_announcement(
        self, payload: AnnouncementCreate
    ) -> AnnouncementRecord:
        """Create a new announcement record with Status='Drafted'."""
        fields: dict[str, Any] = {
            self._F_ANN_TITLE: payload.title,
            self._F_ANN_CONTENT: payload.content,
            self._F_ANN_AUTHOR_EMAIL: payload.author_email,
            self._F_ANN_CATEGORY: list(payload.category),
            self._F_ANN_PRIORITY: payload.priority,
            self._F_ANN_PUBLISH_TIME: self._iso(payload.publish_time),
            self._F_ANN_EXPIRATION_TIME: self._iso(payload.expiration_time),
            self._F_ANN_STATUS: "Drafted",
        }
        attachments = self._attachments_payload(payload.attachments)
        if attachments is not None:
            fields[self._F_ANN_ATTACHMENTS] = attachments

        table = self._announcements_table()
        try:
            created = await asyncio.to_thread(table.create, fields, typecast=True)
        except RequestException as exc:
            logger.error("Airtable create announcement failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during announcement create")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return AnnouncementRecord.model_validate(
            {"id": created["id"], **created.get("fields", {})}
        )

    _UPDATE_FIELD_MAP = {
        "title": _F_ANN_TITLE,
        "content": _F_ANN_CONTENT,
        "author_email": _F_ANN_AUTHOR_EMAIL,
        "category": _F_ANN_CATEGORY,
        "reviewer_comments": _F_ANN_REVIEWER_COMMENTS,
        "priority": _F_ANN_PRIORITY,
        "approved": _F_ANN_APPROVED,
        "status": _F_ANN_STATUS,
    }

    async def _find_announcement_by_id(
        self, announcement_id: int | str
    ) -> dict[str, Any] | None:
        """Find an announcement record by its 'Announcement Id' value."""
        # Autonumber renders as a number; compare numerically when possible,
        # otherwise fall back to string equality.
        try:
            numeric = int(announcement_id)
            formula = af.eq_num(self._F_ANN_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_ANN_ID, str(announcement_id))

        table = self._announcements_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable announcement lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during announcement lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def update_announcement(
        self, announcement_id: int | str, payload: AnnouncementUpdate
    ) -> AnnouncementRecord:
        """Update fields on an announcement identified by Announcement Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {}
        for key, value in data.items():
            if key == "attachments":
                # Allow explicit None or empty list to clear attachments.
                update_fields[self._F_ANN_ATTACHMENTS] = (
                    self._attachments_payload(value) or []
                )
            elif key in ("publish_time", "expiration_time"):
                target = (
                    self._F_ANN_PUBLISH_TIME
                    if key == "publish_time"
                    else self._F_ANN_EXPIRATION_TIME
                )
                update_fields[target] = self._iso(value)
            elif key == "category":
                update_fields[self._F_ANN_CATEGORY] = list(value) if value else []
            elif key == "approved_by":
                update_fields[self._F_ANN_APPROVED_BY] = value
            else:
                update_fields[self._UPDATE_FIELD_MAP[key]] = value

        record = await self._find_announcement_by_id(announcement_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Announcement with id '{announcement_id}' not found.",
            )

        table = self._announcements_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update announcement failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during announcement update")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return AnnouncementRecord.model_validate(
            {"id": updated["id"], **updated.get("fields", {})}
        )

    async def list_announcements(
        self, *, published_only: bool = True
    ) -> list[AnnouncementRecord]:
        """Return announcements from the Announcements table.

        When ``published_only`` is True (default), only records whose
        ``Status`` equals ``"Published"`` are returned.
        """
        formula = (
            af.eq_str(self._F_ANN_STATUS, "Published") if published_only else None
        )
        records = await self._list_records(
            self._announcements_table(), formula=formula
        )
        return self._to_typed(records, AnnouncementRecord)

    async def list_announcements_by_author(
        self, author_email: str
    ) -> list[AnnouncementRecord]:
        """Return announcements whose Author Email equals ``author_email``."""
        normalized = (author_email or "").strip()
        if not normalized:
            return []
        formula = (
            f"LOWER({{{self._F_ANN_AUTHOR_EMAIL}}}) = "
            f"'{self._escape(normalized.lower())}'"
        )
        records = await self._list_records(
            self._announcements_table(), formula=formula
        )
        return self._to_typed(records, AnnouncementRecord)

    async def delete_announcement(self, announcement_id: int | str) -> dict[str, Any]:
        """Delete an announcement identified by its Id field."""
        record = await self._find_announcement_by_id(announcement_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Announcement with id '{announcement_id}' not found.",
            )
        table = self._announcements_table()
        try:
            result = await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable delete announcement failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during announcement delete")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "announcement_id": announcement_id,
            "deleted": bool(result.get("deleted", True)) if isinstance(result, dict) else True,
        }

    async def get_announcement_categories(self) -> list[str]:
        """Return the sorted unique Category values used across announcements."""
        records = await self._list_records(
            self._announcements_table(), fields=[self._F_ANN_CATEGORY]
        )
        unique: set[str] = set()
        for r in records:
            value = r.get("fields", {}).get(self._F_ANN_CATEGORY)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, str) and item:
                    unique.add(item)
        return sorted(unique)

    # ══════════════════════════════════════════════════════════════════
    # Access Control (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════════
    def _access_control_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ACCESS_CONTROL_TABLE,
        )

    def _teams_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.TEAMS_TABLE,
        )

    def _roles_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ROLES_TABLE,
        )

    def _permissions_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.PERMISSIONS_TABLE,
        )

    async def list_access_control_records(self) -> list[AccessControlRecord]:
        """Return all Access Control records as typed objects."""
        records = await self._list_records(self._access_control_table())
        return [self._build_access_control_record(r) for r in records]

    def _build_access_control_record(
        self, record: dict[str, Any]
    ) -> AccessControlRecord:
        """Build an :class:`AccessControlRecord` from a raw Airtable record.

        Resolves the role and permission objects from the parallel lookup
        arrays exposed on the Access Control table.
        """
        s = self._settings
        fields = record.get("fields", {}) or {}

        def _as_list(value: Any) -> list[Any]:
            if value is None:
                return []
            return list(value) if isinstance(value, list) else [value]

        role_ids = _as_list(fields.get(s.ACCESS_CONTROL_ROLES_FIELD))
        role_names = _as_list(
            fields.get(s.ACCESS_CONTROL_ROLE_NAME_LOOKUP_FIELD)
        )
        perm_ids = _as_list(fields.get(s.ACCESS_CONTROL_PERMISSIONS_FIELD))
        perm_names = _as_list(
            fields.get(s.ACCESS_CONTROL_PERMISSION_NAME_LOOKUP_FIELD)
        )
        perm_descriptions = _as_list(
            fields.get(s.ACCESS_CONTROL_PERMISSION_DESCRIPTION_LOOKUP_FIELD)
        )

        def _str_or_none(value: Any) -> str | None:
            if value is None:
                return None
            text = str(value).strip()
            return text or None

        roles: list[Role] = []
        for idx, rid in enumerate(role_ids):
            if not isinstance(rid, str):
                continue
            name = _str_or_none(role_names[idx]) if idx < len(role_names) else None
            roles.append(Role(id=rid, name=name, permissions=[]))

        permissions: list[Permission] = []
        for idx, pid in enumerate(perm_ids):
            if not isinstance(pid, str):
                continue
            name = _str_or_none(perm_names[idx]) if idx < len(perm_names) else None
            desc = (
                _str_or_none(perm_descriptions[idx])
                if idx < len(perm_descriptions)
                else None
            )
            permissions.append(Permission(id=pid, name=name, description=desc))

        # Fund or Program Name is a lookup → list; flatten to a single string.
        fund_or_program_raw = fields.get(
            s.ACCESS_CONTROL_FUND_OR_PROGRAM_NAME_FIELD
        )
        if isinstance(fund_or_program_raw, list):
            items = [
                str(v).strip()
                for v in fund_or_program_raw
                if v is not None and str(v).strip()
            ]
            fund_or_program_name = ", ".join(items) if items else None
        else:
            fund_or_program_name = _str_or_none(fund_or_program_raw)

        return AccessControlRecord(
            id=record["id"],
            user_email=_str_or_none(
                fields.get(s.ACCESS_CONTROL_USER_EMAIL_FIELD)
            ),
            roles=roles,
            permissions=permissions,
            fund_or_program_name=fund_or_program_name,
        )

    async def _find_access_control_by_email(
        self, email: str
    ) -> dict[str, Any] | None:
        """Find an Access Control record by exact (case-insensitive) email."""
        email_field = self._settings.ACCESS_CONTROL_USER_EMAIL_FIELD
        normalized = (email or "").strip().lower()
        if not normalized:
            return None
        formula = (
            f"LOWER({{{email_field}}}) = '{self._escape(normalized)}'"
        )
        table = self._access_control_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable access-control lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during access-control lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def access_control_email_exists(self, email: str) -> bool:
        """Return True when ``email`` already has an Access Control record."""
        return await self._find_access_control_by_email(email) is not None

    async def _find_role_id_by_name(self, role_name: str) -> str | None:
        """Return the Airtable record id of the Role matching ``role_name``."""
        name_field = self._settings.ROLES_NAME_FIELD
        normalized = (role_name or "").strip().lower()
        if not normalized:
            return None
        formula = f"LOWER({{{name_field}}}) = '{self._escape(normalized)}'"
        table = self._roles_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable role lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during role lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0]["id"] if records else None

    async def ensure_access_control_member(self, email: str) -> None:
        """Ensure ``email`` has an Access Control record.

        No-op when the user already exists in the Access Control table.
        Otherwise creates a record with the user's email and the linked
        ``Hub Member`` role resolved from the Roles table.
        """
        if not email:
            return

        existing = await self._find_access_control_by_email(email)
        if existing is not None:
            return

        default_role = self._settings.DEFAULT_MEMBER_ROLE
        role_id = await self._find_role_id_by_name(default_role)
        if role_id is None:
            logger.error(
                "Cannot auto-provision access control for %s: role '%s' not found",
                email,
                default_role,
            )
            return

        s = self._settings
        fields = {
            s.ACCESS_CONTROL_USER_EMAIL_FIELD: email.strip(),
            s.ACCESS_CONTROL_ROLES_FIELD: [role_id],
        }
        table = self._access_control_table()
        try:
            await asyncio.to_thread(table.create, fields)
            logger.info(
                "Auto-provisioned access control for %s with role '%s'",
                email,
                default_role,
            )
        except RequestException as exc:
            logger.error("Airtable access-control auto-provision failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during access-control auto-provision"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

    async def upsert_access_control(
        self, payload: AccessControlAssign
    ) -> AccessControlRecord:
        """Assign role(s) and/or permission(s) to an email (upsert + merge)."""
        email_field = self._settings.ACCESS_CONTROL_USER_EMAIL_FIELD
        roles_field = self._settings.ACCESS_CONTROL_ROLES_FIELD
        permissions_field = self._settings.ACCESS_CONTROL_PERMISSIONS_FIELD

        roles_in = list(payload.roles or [])
        permissions_in = list(payload.permissions or [])
        table = self._access_control_table()

        existing = await self._find_access_control_by_email(payload.user_email)
        try:
            if existing is None:
                fields: dict[str, Any] = {email_field: payload.user_email.strip()}
                if roles_in:
                    fields[roles_field] = roles_in
                if permissions_in:
                    fields[permissions_field] = permissions_in
                result = await asyncio.to_thread(table.create, fields)
            else:
                fields_existing = existing.get("fields", {}) or {}
                current_roles = fields_existing.get(roles_field) or []
                current_perms = fields_existing.get(permissions_field) or []

                merged_roles = list(dict.fromkeys([*current_roles, *roles_in]))
                merged_perms = list(dict.fromkeys([*current_perms, *permissions_in]))

                update_fields: dict[str, Any] = {}
                if roles_in and merged_roles != current_roles:
                    update_fields[roles_field] = merged_roles
                if permissions_in and merged_perms != current_perms:
                    update_fields[permissions_field] = merged_perms

                if not update_fields:
                    result = existing
                else:
                    result = await asyncio.to_thread(
                        table.update, existing["id"], update_fields
                    )
        except RequestException as exc:
            logger.error("Airtable access-control upsert failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during access-control upsert")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return self._build_access_control_record(result)

    async def revoke_access_control(
        self, payload: AccessControlRevoke
    ) -> AccessControlRecord:
        """Remove role(s) and/or permission(s) from the record matching the email."""
        email_field = self._settings.ACCESS_CONTROL_USER_EMAIL_FIELD
        roles_field = self._settings.ACCESS_CONTROL_ROLES_FIELD
        permissions_field = self._settings.ACCESS_CONTROL_PERMISSIONS_FIELD

        existing = await self._find_access_control_by_email(payload.user_email)
        if existing is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Access Control record for email '{payload.user_email}' "
                    "not found."
                ),
            )

        fields_existing = existing.get("fields", {}) or {}
        current_roles = list(fields_existing.get(roles_field) or [])
        current_perms = list(fields_existing.get(permissions_field) or [])

        roles_to_remove = set(payload.roles or [])
        perms_to_remove = set(payload.permissions or [])

        new_roles = [r for r in current_roles if r not in roles_to_remove]
        new_perms = [p for p in current_perms if p not in perms_to_remove]

        update_fields: dict[str, Any] = {}
        if payload.roles is not None and new_roles != current_roles:
            update_fields[roles_field] = new_roles
        if payload.permissions is not None and new_perms != current_perms:
            update_fields[permissions_field] = new_perms

        if not update_fields:
            return self._build_access_control_record(existing)

        table = self._access_control_table()
        try:
            result = await asyncio.to_thread(
                table.update, existing["id"], update_fields
            )
        except RequestException as exc:
            logger.error("Airtable access-control revoke failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during access-control revoke")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return self._build_access_control_record(result)

    async def get_unique_team_emails(self) -> list[str]:
        """Return the sorted unique non-empty Work Email values from Teams."""
        field = self._settings.TEAMS_WORK_EMAIL_FIELD
        records = await self._list_records(
            self._teams_table(), fields=[field]
        )
        unique: set[str] = set()
        for r in records:
            value = r.get("fields", {}).get(field)
            if isinstance(value, str) and value.strip():
                unique.add(value.strip())
        return sorted(unique)

    @staticmethod
    def _normalize_employment_type(value: str) -> str:
        """Normalize an employment-type token for robust matching.

        Lower-cases and strips every non-alphanumeric character so that
        differences in spaces, dots, hyphens, etc. are ignored (e.g.
        'Full-Time', 'full time' and 'FullTime' all normalize equally)."""
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    def _filter_by_employment_type(
        self,
        records: list[dict[str, Any]],
        included: list[str] | None,
        excluded: list[str] | None,
    ) -> list[dict[str, Any]]:
        """Filter Users records by the 'Employment Type' field.

        * ``included`` — keep a record only if any of its Employment Type
          values *contains* (normalized substring) any included token.
        * ``excluded`` — drop a record if any of its Employment Type values
          *contains* any excluded token.
        * Both — apply both; exclusion takes precedence on conflict.
        * Neither / empty — no filtering.

        Matching is robust against spaces, dots, hyphens and case (see
        :meth:`_normalize_employment_type`)."""
        included_norm = [
            n for n in (self._normalize_employment_type(v) for v in (included or [])) if n
        ]
        excluded_norm = [
            n for n in (self._normalize_employment_type(v) for v in (excluded or [])) if n
        ]
        if not included_norm and not excluded_norm:
            return records

        emp_field = self._settings.USERS_EMPLOYMENT_TYPE_FIELD

        def _entries(rec: dict[str, Any]) -> list[str]:
            raw = rec.get("fields", {}).get(emp_field)
            if raw is None:
                return []
            values = raw if isinstance(raw, list) else [raw]
            return [self._normalize_employment_type(str(v)) for v in values]

        out: list[dict[str, Any]] = []
        for rec in records:
            entries = _entries(rec)
            if excluded_norm and any(
                token in entry for entry in entries for token in excluded_norm
            ):
                continue  # exclusion wins
            if included_norm and not any(
                token in entry for entry in entries for token in included_norm
            ):
                continue
            out.append(rec)
        return out

    async def get_team_size(
        self,
        *,
        included_employment_types: list[str] | None = None,
        excluded_employment_types: list[str] | None = None,
    ) -> CountResponse:
        """Return the number of distinct non-empty 'Name' values in the Users
        table where the 'Status' single-select equals 'Active'.

        Optionally filtered by the 'Employment Type' field (see
        :meth:`_filter_by_employment_type`)."""
        s = self._settings
        name_field = s.USERS_NAME_FIELD
        formula = af.eq_str(s.USERS_STATUS_FIELD, "Active")
        records = await self._list_records(
            self._users_table(),
            formula=formula,
            fields=[name_field, s.USERS_EMPLOYMENT_TYPE_FIELD],
        )
        records = self._filter_by_employment_type(
            records, included_employment_types, excluded_employment_types
        )
        unique: set[str] = set()
        for r in records:
            value = r.get("fields", {}).get(name_field)
            if isinstance(value, str) and value.strip():
                unique.add(value.strip())
        return CountResponse(count=len(unique))

    async def get_team_members(
        self,
        *,
        included_employment_types: list[str] | None = None,
        excluded_employment_types: list[str] | None = None,
    ) -> list[PersonContactItem]:
        """Return the team members (First Name, Last Name, Work Email)
        from the Users table where the 'Status' single-select equals
        'Active'. Uses the same table and filter as :meth:`get_team_size`;
        de-duplicated by 'Name'.

        Optionally filtered by the 'Employment Type' field (see
        :meth:`_filter_by_employment_type`)."""
        s = self._settings
        formula = af.eq_str(s.USERS_STATUS_FIELD, "Active")
        records = await self._list_records(
            self._users_table(),
            formula=formula,
            fields=[
                s.USERS_NAME_FIELD,
                s.USERS_FIRST_NAME_FIELD,
                s.USERS_LAST_NAME_FIELD,
                s.USERS_WORK_EMAIL_FIELD,
                s.USERS_EMPLOYMENT_TYPE_FIELD,
            ],
        )
        records = self._filter_by_employment_type(
            records, included_employment_types, excluded_employment_types
        )
        seen: set[str] = set()
        items: list[PersonContactItem] = []
        for r in records:
            fields = r.get("fields", {}) or {}
            name = fields.get(s.USERS_NAME_FIELD)
            if isinstance(name, str) and name.strip():
                key = name.strip().lower()
                if key in seen:
                    continue
                seen.add(key)
            items.append(
                PersonContactItem(
                    first_name=fields.get(s.USERS_FIRST_NAME_FIELD),
                    last_name=fields.get(s.USERS_LAST_NAME_FIELD),
                    work_email=fields.get(s.USERS_WORK_EMAIL_FIELD),
                )
            )
        items.sort(
            key=lambda x: (
                (x.last_name or "").lower(),
                (x.first_name or "").lower(),
            )
        )
        return items

    # ══════════════════════════════════════════════════════════════════
    # Grant Application Resources (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════════
    _F_GAR_ID = _S.AT_F_GAR_ID
    _F_GAR_DOCUMENT = _S.AT_F_GAR_DOCUMENT
    _F_GAR_DOCUMENT_URL = _S.AT_F_GAR_DOCUMENT_URL
    _F_GAR_NOTES = _S.AT_F_GAR_NOTES
    _F_GAR_ENTITY = _S.AT_F_GAR_ENTITY
    _F_GAR_TABS = _S.AT_F_GAR_TABS

    _GAR_UPDATE_FIELD_MAP = {
        "document": _F_GAR_DOCUMENT,
        "document_url": _F_GAR_DOCUMENT_URL,
        "notes": _F_GAR_NOTES,
        "entity": _F_GAR_ENTITY,
        "tabs": _F_GAR_TABS,
    }

    def _grant_app_resources_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.GRANT_APPLICATION_RESOURCES_TABLE,
        )

    @staticmethod
    def _gar_to_typed(
        records: list[dict[str, Any]],
    ) -> list[GrantAppResourceRecord]:
        """Convert raw records to GrantAppResourceRecord instances.

        Maps the Airtable record id to ``record_id`` (instead of ``id``)
        so the table's autonumber ``Id`` field can be exposed as ``id``.
        """
        return [
            GrantAppResourceRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_grant_app_resources(
        self, *, fields: list[str] | None = None
    ) -> list[GrantAppResourceRecord]:
        """Return all rows from the Grant Application Resources table.

        The 'Document URL' field may be empty when 'Document' does not
        refer to an actual document.
        """
        if not self._settings.GRANT_APPLICATION_RESOURCES_TABLE:
            return []
        records = await self._list_records(
            self._grant_app_resources_table(), fields=fields
        )
        return self._gar_to_typed(records)

    async def _find_grant_app_resource_by_id(
        self, gar_id: int | str
    ) -> dict[str, Any] | None:
        """Find a Grant Application Resources record by its 'Id' value."""
        try:
            numeric = int(gar_id)
            formula = af.eq_num(self._F_GAR_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_GAR_ID, str(gar_id))

        table = self._grant_app_resources_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable grant app resource lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during grant app resource lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_grant_app_resource(
        self, payload: GrantAppResourceCreate
    ) -> GrantAppResourceRecord:
        """Create a new Grant Application Resources row."""
        body: dict[str, Any] = {self._F_GAR_DOCUMENT: payload.document}
        if payload.document_url is not None:
            body[self._F_GAR_DOCUMENT_URL] = payload.document_url
        if payload.notes is not None:
            body[self._F_GAR_NOTES] = payload.notes
        if payload.entity is not None:
            body[self._F_GAR_ENTITY] = payload.entity
        if payload.tabs is not None:
            body[self._F_GAR_TABS] = payload.tabs

        table = self._grant_app_resources_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable grant app resource create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during grant app resource create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._gar_to_typed([created])[0]

    async def update_grant_app_resource(
        self, gar_id: int | str, payload: GrantAppResourceUpdate
    ) -> GrantAppResourceRecord:
        """Update a Grant Application Resources record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._GAR_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_grant_app_resource_by_id(gar_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Grant Application Resources record with id '{gar_id}' not found."
                ),
            )

        table = self._grant_app_resources_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update grant app resource failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during grant app resource update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._gar_to_typed([updated])[0]

    async def delete_grant_app_resource(
        self, gar_id: int | str
    ) -> dict[str, Any]:
        """Delete a Grant Application Resources row by its autonumber Id."""
        record = await self._find_grant_app_resource_by_id(gar_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Grant Application Resources record with id '{gar_id}' not found."
                ),
            )
        table = self._grant_app_resources_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable grant app resource delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during grant app resource delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "grant_app_resource_id": gar_id,
            "deleted": True,
        }

    # ═══════════════════════════════════════════════════════════════
    # Finance Links (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    _F_FL_ID = _S.AT_F_FL_ID
    _F_FL_DOCUMENT = _S.AT_F_FL_DOCUMENT
    _F_FL_DOCUMENT_URL = _S.AT_F_FL_DOCUMENT_URL

    _FL_UPDATE_FIELD_MAP = {
        "document": _F_FL_DOCUMENT,
        "document_url": _F_FL_DOCUMENT_URL,
    }

    def _finance_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.FINANCE_LINKS_TABLE,
        )

    @staticmethod
    def _fl_to_typed(
        records: list[dict[str, Any]],
    ) -> list[FinanceLinkRecord]:
        """Convert raw records to FinanceLinkRecord instances.

        Maps the Airtable record id to ``record_id`` (instead of ``id``)
        so the table's autonumber ``Id`` field can be exposed as ``id``.
        """
        return [
            FinanceLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_finance_links(
        self, *, fields: list[str] | None = None
    ) -> list[FinanceLinkRecord]:
        """Return all rows from the Finance Links table."""
        records = await self._list_records(
            self._finance_links_table(), fields=fields
        )
        return self._fl_to_typed(records)

    async def _find_finance_link_by_url(
        self, document_url: str
    ) -> dict[str, Any] | None:
        """Find a Finance Links record by its 'Document URL' value."""
        formula = af.eq_str(self._F_FL_DOCUMENT_URL, document_url)
        table = self._finance_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable finance links lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during finance links lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def update_finance_link_by_url(
        self, document_url: str, payload: FinanceLinkUpdate
    ) -> FinanceLinkRecord:
        """Update a Finance Links record identified by its 'Document URL'."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._FL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_finance_link_by_url(document_url)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Finance Links record with Document URL '{document_url}' not found."
                ),
            )

        table = self._finance_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update finance link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during finance link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return FinanceLinkRecord.model_validate(
            {"record_id": updated["id"], **updated.get("fields", {})}
        )

    # ═══════════════════════════════════════════════════════════════
    # Office Spaces (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    _F_OS_BRANCH = _S.AT_F_OS_BRANCH
    _F_OS_ADDRESS = _S.AT_F_OS_ADDRESS
    _F_OS_DETAILS = _S.AT_F_OS_DETAILS

    _OS_FIELD_MAP = {
        "branch": _F_OS_BRANCH,
        "address": _F_OS_ADDRESS,
        "details": _F_OS_DETAILS,
    }

    def _office_spaces_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.OFFICE_SPACES_TABLE,
        )

    @staticmethod
    def _os_to_typed(
        records: list[dict[str, Any]],
    ) -> list[OfficeSpaceRecord]:
        return [
            OfficeSpaceRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_office_spaces(
        self, *, fields: list[str] | None = None
    ) -> list[OfficeSpaceRecord]:
        """Return all rows from the Office Spaces table."""
        records = await self._list_records(
            self._office_spaces_table(), fields=fields
        )
        return self._os_to_typed(records)

    async def _find_office_space_by_branch(
        self, branch: str
    ) -> dict[str, Any] | None:
        """Find an Office Spaces record by its 'Branch' value."""
        formula = af.eq_str(self._F_OS_BRANCH, branch)
        table = self._office_spaces_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable office spaces lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during office spaces lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_office_space(
        self, payload: OfficeSpaceCreate
    ) -> OfficeSpaceRecord:
        """Create a new Office Spaces record."""
        data = payload.model_dump(exclude_none=True)
        create_fields: dict[str, Any] = {
            self._OS_FIELD_MAP[key]: value for key, value in data.items()
        }

        table = self._office_spaces_table()
        try:
            created = await asyncio.to_thread(
                table.create, create_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable create office space failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during office space creation"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return OfficeSpaceRecord.model_validate(
            {"record_id": created["id"], **created.get("fields", {})}
        )

    async def update_office_space_by_branch(
        self, branch: str, payload: OfficeSpaceUpdate
    ) -> OfficeSpaceRecord:
        """Update an Office Spaces record identified by its 'Branch' value."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._OS_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_office_space_by_branch(branch)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Office Spaces record with Branch '{branch}' not found."
                ),
            )

        table = self._office_spaces_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update office space failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during office space update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return OfficeSpaceRecord.model_validate(
            {"record_id": updated["id"], **updated.get("fields", {})}
        )

    # ═══════════════════════════════════════════════════════════════
    # Google Docs Tabs (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    _F_GDT_UI_PAGE = _S.AT_F_GDT_UI_PAGE

    def _google_docs_tabs_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.GOOGLE_DOCS_TABS_TABLE,
        )

    @staticmethod
    def _gdt_to_typed(
        records: list[dict[str, Any]],
    ) -> list[GoogleDocsTabRecord]:
        return [
            GoogleDocsTabRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_google_docs_tabs(
        self,
        *,
        ui_page: str | None = None,
        fields: list[str] | None = None,
    ) -> list[GoogleDocsTabRecord]:
        """Return rows from the Google Docs Tabs table.

        If ``ui_page`` is provided, filter to records where the 'UI Page'
        field equals that value.
        """
        formula = af.eq_str(self._F_GDT_UI_PAGE, ui_page) if ui_page else None
        records = await self._list_records(
            self._google_docs_tabs_table(), formula=formula, fields=fields
        )
        return self._gdt_to_typed(records)

    # ═══════════════════════════════════════════════════════════════
    # Meeting Cadence (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    def _meeting_cadence_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.MEETING_CADENCE_TABLE,
        )

    async def get_meeting_cadence(
        self, *, fields: list[str] | None = None
    ) -> list[MeetingCadenceRecord]:
        """Return all rows from the Meeting Cadence table."""
        records = await self._list_records(
            self._meeting_cadence_table(), fields=fields
        )
        return self._to_typed(records, MeetingCadenceRecord)

    # ═══════════════════════════════════════════════════════════════
    # Useful Links (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    def _useful_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.USEFUL_LINKS_TABLE,
        )

    async def get_useful_links(
        self, *, fields: list[str] | None = None
    ) -> list[UsefulLinkRecord]:
        """Return all rows from the Useful Links table."""
        records = await self._list_records(
            self._useful_links_table(), fields=fields
        )
        return self._to_typed(records, UsefulLinkRecord)

    # ═══════════════════════════════════════════════════════════════
    # HR & Benefits (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    def _hr_and_benefits_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.HR_AND_BENEFITS_TABLE,
        )

    async def get_hr_and_benefits(
        self, *, fields: list[str] | None = None
    ) -> list[HrAndBenefitsRecord]:
        """Return all rows from the HR & Benefits table."""
        records = await self._list_records(
            self._hr_and_benefits_table(), fields=fields
        )
        return self._to_typed(records, HrAndBenefitsRecord)

    # ═══════════════════════════════════════════════════════════════
    # General Fundraising Resources (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    def _general_fundraising_resources_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.GENERAL_FUNDRAISING_RESOURCES_TABLE,
        )

    async def get_general_fundraising_resources(
        self, *, fields: list[str] | None = None
    ) -> list[GeneralFundraisingResourceRecord]:
        """Return all rows from the General Fundraising Resources table."""
        records = await self._list_records(
            self._general_fundraising_resources_table(), fields=fields
        )
        return self._to_typed(records, GeneralFundraisingResourceRecord)

    # ═══════════════════════════════════════════════════════════════
    # Partnerships Links (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    _F_PL_ID = _S.AT_F_PL_ID
    _F_PL_TEXT = _S.AT_F_PL_TEXT
    _F_PL_LINK = _S.AT_F_PL_LINK
    _F_PL_CATEGORY = _S.AT_F_PL_CATEGORY
    _F_PL_TYPE = _S.AT_F_PL_TYPE

    _PL_UPDATE_FIELD_MAP = {
        "text": _F_PL_TEXT,
        "link": _F_PL_LINK,
        "category": _F_PL_CATEGORY,
        "type": _F_PL_TYPE,
    }

    def _partnerships_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.PARTNERSHIPS_LINKS_TABLE,
        )

    @staticmethod
    def _pl_to_typed(
        records: list[dict[str, Any]],
    ) -> list[PartnershipsLinkRecord]:
        return [
            PartnershipsLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_partnerships_links(
        self,
        *,
        category: str | None = None,
        fields: list[str] | None = None,
    ) -> list[PartnershipsLinkRecord]:
        """Return rows from the Partnerships Links table, optionally
        filtered by ``Category``."""
        formula = af.eq_str(self._F_PL_CATEGORY, category) if category else None
        records = await self._list_records(
            self._partnerships_links_table(), formula=formula, fields=fields
        )
        return self._pl_to_typed(records)

    async def _find_partnerships_link_by_id(
        self, pl_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(pl_id)
            formula = af.eq_num(self._F_PL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_PL_ID, str(pl_id))

        table = self._partnerships_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable partnerships link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partnerships link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_partnerships_link(
        self, payload: PartnershipsLinkCreate
    ) -> PartnershipsLinkRecord:
        """Create a new Partnerships Links row."""
        body: dict[str, Any] = {
            self._F_PL_TEXT: payload.text,
            self._F_PL_LINK: payload.link,
        }
        if payload.category is not None:
            body[self._F_PL_CATEGORY] = payload.category
        if payload.type is not None:
            body[self._F_PL_TYPE] = payload.type

        table = self._partnerships_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable partnerships link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partnerships link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._pl_to_typed([created])[0]

    async def update_partnerships_link(
        self, pl_id: int | str, payload: PartnershipsLinkUpdate
    ) -> PartnershipsLinkRecord:
        """Update a Partnerships Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._PL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_partnerships_link_by_id(pl_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Partnerships Links record with id '{pl_id}' not found.",
            )

        table = self._partnerships_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update partnerships link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partnerships link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return self._pl_to_typed([updated])[0]

    async def delete_partnerships_link(
        self, pl_id: int | str | None = None
    ) -> dict[str, Any]:
        """Delete a Partnerships Links row by its autonumber Id, or
        all rows when ``pl_id`` is ``None``."""
        table = self._partnerships_links_table()

        if pl_id is None:
            try:
                records = await asyncio.to_thread(table.all)
            except RequestException as exc:
                logger.error("Airtable partnerships links list failed: %s", exc)
                raise AirtableError(f"Airtable API error: {exc}") from exc
            except Exception as exc:
                logger.exception(
                    "Unexpected Airtable error during partnerships links list"
                )
                raise AirtableError(f"Airtable API error: {exc}") from exc

            record_ids = [r["id"] for r in records]
            if not record_ids:
                return {"deleted": True, "deleted_count": 0, "record_ids": []}

            try:
                await asyncio.to_thread(table.batch_delete, record_ids)
            except RequestException as exc:
                logger.error("Airtable partnerships links bulk delete failed: %s", exc)
                raise AirtableError(f"Airtable API error: {exc}") from exc
            except Exception as exc:
                logger.exception(
                    "Unexpected Airtable error during partnerships links bulk delete"
                )
                raise AirtableError(f"Airtable API error: {exc}") from exc

            return {
                "deleted": True,
                "deleted_count": len(record_ids),
                "record_ids": record_ids,
            }

        record = await self._find_partnerships_link_by_id(pl_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Partnerships Links record with id '{pl_id}' not found.",
            )
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable partnerships link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partnerships link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "partnerships_link_id": pl_id,
            "deleted": True,
        }

    # ══════════════════════════════════════════════════════════════
    # Policy Links (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════
    _F_POL_ID = _S.AT_F_POL_ID
    _F_POL_TEXT = _S.AT_F_POL_TEXT
    _F_POL_URL = _S.AT_F_POL_URL

    _POL_UPDATE_FIELD_MAP = {
        "text": _F_POL_TEXT,
        "url": _F_POL_URL,
    }

    def _policy_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.POLICY_LINKS_TABLE,
        )

    @staticmethod
    def _pol_to_typed(
        records: list[dict[str, Any]],
    ) -> list[PolicyLinkRecord]:
        return [
            PolicyLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_policy_links(
        self, *, fields: list[str] | None = None
    ) -> list[PolicyLinkRecord]:
        """Return all rows from the Policy Links table."""
        if not self._settings.POLICY_LINKS_TABLE:
            return []
        records = await self._list_records(
            self._policy_links_table(), fields=fields
        )
        return self._pol_to_typed(records)

    async def _find_policy_link_by_id(
        self, pol_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(pol_id)
            formula = af.eq_num(self._F_POL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_POL_ID, str(pol_id))

        table = self._policy_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable policy link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during policy link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_policy_link(
        self, payload: PolicyLinkCreate
    ) -> PolicyLinkRecord:
        """Create a new Policy Links row."""
        body: dict[str, Any] = {
            self._F_POL_TEXT: payload.text,
            self._F_POL_URL: payload.url,
        }
        table = self._policy_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable policy link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during policy link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._pol_to_typed([created])[0]

    async def update_policy_link(
        self, pol_id: int | str, payload: PolicyLinkUpdate
    ) -> PolicyLinkRecord:
        """Update a Policy Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._POL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_policy_link_by_id(pol_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Policy Links record with id '{pol_id}' not found.",
            )

        table = self._policy_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update policy link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during policy link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._pol_to_typed([updated])[0]

    async def delete_policy_link(
        self, pol_id: int | str
    ) -> dict[str, Any]:
        """Delete a Policy Links row by its autonumber Id."""
        record = await self._find_policy_link_by_id(pol_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Policy Links record with id '{pol_id}' not found.",
            )
        table = self._policy_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable policy link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during policy link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "policy_link_id": pol_id,
            "deleted": True,
        }

    # ══════════════════════════════════════════════════════════════
    # Events Quick Links (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════
    _F_EQL_ID = _S.AT_F_EQL_ID
    _F_EQL_TITLE = _S.AT_F_EQL_TITLE
    _F_EQL_ANCHOR_TEXT = _S.AT_F_EQL_ANCHOR_TEXT
    _F_EQL_TYPE = _S.AT_F_EQL_TYPE
    _F_EQL_URL = _S.AT_F_EQL_URL
    _F_EQL_EMAIL = _S.AT_F_EQL_EMAIL

    _EQL_UPDATE_FIELD_MAP = {
        "title": _F_EQL_TITLE,
        "anchor_text": _F_EQL_ANCHOR_TEXT,
        "type": _F_EQL_TYPE,
        "url": _F_EQL_URL,
        "email": _F_EQL_EMAIL,
    }

    def _events_quick_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.EVENTS_QUICK_LINKS_TABLE,
        )

    @staticmethod
    def _eql_to_typed(
        records: list[dict[str, Any]],
    ) -> list[EventsQuickLinkRecord]:
        return [
            EventsQuickLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_events_quick_links(
        self, *, fields: list[str] | None = None
    ) -> list[EventsQuickLinkRecord]:
        """Return all rows from the Events Quick Links table."""
        if not self._settings.EVENTS_QUICK_LINKS_TABLE:
            return []
        records = await self._list_records(
            self._events_quick_links_table(), fields=fields
        )
        return self._eql_to_typed(records)

    async def create_events_quick_link(
        self, payload: EventsQuickLinkCreate
    ) -> EventsQuickLinkRecord:
        """Create a new Events Quick Links row."""
        body: dict[str, Any] = {
            self._F_EQL_TITLE: payload.title,
            self._F_EQL_ANCHOR_TEXT: payload.anchor_text,
            self._F_EQL_TYPE: payload.type,
        }
        if payload.url is not None:
            body[self._F_EQL_URL] = payload.url
        if payload.email is not None:
            body[self._F_EQL_EMAIL] = payload.email

        table = self._events_quick_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable events quick link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during events quick link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._eql_to_typed([created])[0]

    async def _find_events_quick_link_by_id(
        self, eql_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(eql_id)
            formula = af.eq_num(self._F_EQL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_EQL_ID, str(eql_id))

        table = self._events_quick_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable events quick link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during events quick link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def update_events_quick_link(
        self, eql_id: int | str, payload: EventsQuickLinkUpdate
    ) -> EventsQuickLinkRecord:
        """Update an Events Quick Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._EQL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_events_quick_link_by_id(eql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Events Quick Links record with id '{eql_id}' not found.",
            )

        table = self._events_quick_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update events quick link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during events quick link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._eql_to_typed([updated])[0]

    async def delete_events_quick_link(
        self, eql_id: int | str
    ) -> dict[str, Any]:
        """Delete an Events Quick Links row by its autonumber Id."""
        record = await self._find_events_quick_link_by_id(eql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Events Quick Links record with id '{eql_id}' not found.",
            )
        table = self._events_quick_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable events quick link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during events quick link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "events_quick_link_id": eql_id,
            "deleted": True,
        }

    # ══════════════════════════════════════════════════════════════
    # Finance Quick Links (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════
    _F_FQL_ID = _S.AT_F_FQL_ID
    _F_FQL_ANCHOR_TEXT = _S.AT_F_FQL_ANCHOR_TEXT
    _F_FQL_URL = _S.AT_F_FQL_URL
    _F_FQL_ENTITY = _S.AT_F_FQL_ENTITY
    _F_FQL_TABS = _S.AT_F_FQL_TABS

    _FQL_UPDATE_FIELD_MAP = {
        "anchor_text": _F_FQL_ANCHOR_TEXT,
        "url": _F_FQL_URL,
        "entity": _F_FQL_ENTITY,
        "tabs": _F_FQL_TABS,
    }

    def _finance_quick_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.FINANCE_QUICK_LINKS_TABLE,
        )

    @staticmethod
    def _fql_to_typed(
        records: list[dict[str, Any]],
    ) -> list[FinanceQuickLinkRecord]:
        return [
            FinanceQuickLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_finance_quick_links(
        self, *, fields: list[str] | None = None
    ) -> list[FinanceQuickLinkRecord]:
        """Return all rows from the Finance Quick Links table."""
        if not self._settings.FINANCE_QUICK_LINKS_TABLE:
            return []
        records = await self._list_records(
            self._finance_quick_links_table(), fields=fields
        )
        return self._fql_to_typed(records)

    async def _find_finance_quick_link_by_id(
        self, fql_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(fql_id)
            formula = af.eq_num(self._F_FQL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_FQL_ID, str(fql_id))

        table = self._finance_quick_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable finance quick link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during finance quick link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_finance_quick_link(
        self, payload: FinanceQuickLinkCreate
    ) -> FinanceQuickLinkRecord:
        """Create a new Finance Quick Links row."""
        body: dict[str, Any] = {
            self._F_FQL_ANCHOR_TEXT: payload.anchor_text,
            self._F_FQL_URL: payload.url,
        }
        if payload.entity is not None:
            body[self._F_FQL_ENTITY] = payload.entity
        if payload.tabs is not None:
            body[self._F_FQL_TABS] = payload.tabs
        table = self._finance_quick_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable finance quick link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during finance quick link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._fql_to_typed([created])[0]

    async def update_finance_quick_link(
        self, fql_id: int | str, payload: FinanceQuickLinkUpdate
    ) -> FinanceQuickLinkRecord:
        """Update a Finance Quick Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._FQL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_finance_quick_link_by_id(fql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Finance Quick Links record with id '{fql_id}' not found.",
            )

        table = self._finance_quick_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update finance quick link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during finance quick link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._fql_to_typed([updated])[0]

    async def delete_finance_quick_link(
        self, fql_id: int | str
    ) -> dict[str, Any]:
        """Delete a Finance Quick Links row by its autonumber Id."""
        record = await self._find_finance_quick_link_by_id(fql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Finance Quick Links record with id '{fql_id}' not found.",
            )
        table = self._finance_quick_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable finance quick link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during finance quick link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "finance_quick_link_id": fql_id,
            "deleted": True,
        }

    # ══════════════════════════════════════════════════════════════
    # RenPhil Due Diligence Links (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════
    _F_DDL_ID = _S.AT_F_DDL_ID
    _F_DDL_ANCHOR_TEXT = _S.AT_F_DDL_ANCHOR_TEXT
    _F_DDL_URL = _S.AT_F_DDL_URL
    _F_DDL_ENTITY = _S.AT_F_DDL_ENTITY
    _F_DDL_TABS = _S.AT_F_DDL_TABS

    _DDL_UPDATE_FIELD_MAP = {
        "anchor_text": _F_DDL_ANCHOR_TEXT,
        "url": _F_DDL_URL,
        "entity": _F_DDL_ENTITY,
        "tabs": _F_DDL_TABS,
    }

    def _renphil_due_diligence_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.RENPHIL_DUE_DILIGENCE_LINKS_TABLE,
        )

    @staticmethod
    def _ddl_to_typed(
        records: list[dict[str, Any]],
    ) -> list[RenphilDueDiligenceLinkRecord]:
        return [
            RenphilDueDiligenceLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_renphil_due_diligence_links(
        self, *, fields: list[str] | None = None
    ) -> list[RenphilDueDiligenceLinkRecord]:
        """Return all rows from the RenPhil Due Diligence Links table."""
        if not self._settings.RENPHIL_DUE_DILIGENCE_LINKS_TABLE:
            return []
        records = await self._list_records(
            self._renphil_due_diligence_links_table(), fields=fields
        )
        return self._ddl_to_typed(records)

    async def _find_renphil_due_diligence_link_by_id(
        self, ddl_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(ddl_id)
            formula = af.eq_num(self._F_DDL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_DDL_ID, str(ddl_id))

        table = self._renphil_due_diligence_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error(
                "Airtable RenPhil due diligence link lookup failed: %s", exc
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during RenPhil due diligence link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_renphil_due_diligence_link(
        self, payload: RenphilDueDiligenceLinkCreate
    ) -> RenphilDueDiligenceLinkRecord:
        """Create a new RenPhil Due Diligence Links row."""
        body: dict[str, Any] = {
            self._F_DDL_ANCHOR_TEXT: payload.anchor_text,
            self._F_DDL_URL: payload.url,
        }
        if payload.entity is not None:
            body[self._F_DDL_ENTITY] = payload.entity
        if payload.tabs is not None:
            body[self._F_DDL_TABS] = payload.tabs
        table = self._renphil_due_diligence_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error(
                "Airtable RenPhil due diligence link create failed: %s", exc
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during RenPhil due diligence link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._ddl_to_typed([created])[0]

    async def update_renphil_due_diligence_link(
        self, ddl_id: int | str, payload: RenphilDueDiligenceLinkUpdate
    ) -> RenphilDueDiligenceLinkRecord:
        """Update a RenPhil Due Diligence Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._DDL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_renphil_due_diligence_link_by_id(ddl_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"RenPhil Due Diligence Links record with id '{ddl_id}' not found."
                ),
            )

        table = self._renphil_due_diligence_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error(
                "Airtable update RenPhil due diligence link failed: %s", exc
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during RenPhil due diligence link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._ddl_to_typed([updated])[0]

    async def delete_renphil_due_diligence_link(
        self, ddl_id: int | str
    ) -> dict[str, Any]:
        """Delete a RenPhil Due Diligence Links row by its autonumber Id."""
        record = await self._find_renphil_due_diligence_link_by_id(ddl_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"RenPhil Due Diligence Links record with id '{ddl_id}' not found."
                ),
            )
        table = self._renphil_due_diligence_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error(
                "Airtable RenPhil due diligence link delete failed: %s", exc
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during RenPhil due diligence link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "renphil_due_diligence_link_id": ddl_id,
            "deleted": True,
        }

    # ══════════════════════════════════════════════════════════════
    # Board Member List (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════
    _F_BM_ID = _S.AT_F_BM_ID
    _F_BM_TITLE = _S.AT_F_BM_TITLE
    _F_BM_FULL_NAME = _S.AT_F_BM_FULL_NAME
    _F_BM_ROLE = _S.AT_F_BM_ROLE
    _F_BM_ORGANIZATION = _S.AT_F_BM_ORGANIZATION
    _F_BM_CONTACT = _S.AT_F_BM_CONTACT
    _F_BM_ENTITY = _S.AT_F_BM_ENTITY
    _F_BM_TABS = _S.AT_F_BM_TABS

    _BM_UPDATE_FIELD_MAP = {
        "title": _F_BM_TITLE,
        "full_name": _F_BM_FULL_NAME,
        "role": _F_BM_ROLE,
        "organization": _F_BM_ORGANIZATION,
        "contact": _F_BM_CONTACT,
        "entity": _F_BM_ENTITY,
        "tabs": _F_BM_TABS,
    }

    def _board_member_list_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.BOARD_MEMBER_LIST_TABLE,
        )

    @staticmethod
    def _bm_to_typed(
        records: list[dict[str, Any]],
    ) -> list[BoardMemberRecord]:
        return [
            BoardMemberRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_board_members(
        self, *, fields: list[str] | None = None
    ) -> list[BoardMemberRecord]:
        """Return all rows from the Board Member List table."""
        if not self._settings.BOARD_MEMBER_LIST_TABLE:
            return []
        records = await self._list_records(
            self._board_member_list_table(), fields=fields
        )
        return self._bm_to_typed(records)

    async def _find_board_member_by_id(
        self, bm_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(bm_id)
            formula = af.eq_num(self._F_BM_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_BM_ID, str(bm_id))

        table = self._board_member_list_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable board member lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during board member lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_board_member(
        self, payload: BoardMemberCreate
    ) -> BoardMemberRecord:
        """Create a new Board Member List row."""
        body: dict[str, Any] = {
            self._F_BM_FULL_NAME: payload.full_name,
            self._F_BM_CONTACT: payload.contact,
        }
        if payload.title is not None:
            body[self._F_BM_TITLE] = payload.title
        if payload.role is not None:
            body[self._F_BM_ROLE] = payload.role
        if payload.organization is not None:
            body[self._F_BM_ORGANIZATION] = payload.organization
        if payload.entity is not None:
            body[self._F_BM_ENTITY] = payload.entity
        if payload.tabs is not None:
            body[self._F_BM_TABS] = payload.tabs

        table = self._board_member_list_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable board member create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during board member create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._bm_to_typed([created])[0]

    async def update_board_member(
        self, bm_id: int | str, payload: BoardMemberUpdate
    ) -> BoardMemberRecord:
        """Update a Board Member List record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._BM_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_board_member_by_id(bm_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Board Member List record with id '{bm_id}' not found.",
            )

        table = self._board_member_list_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update board member failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during board member update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._bm_to_typed([updated])[0]

    async def delete_board_member(
        self, bm_id: int | str
    ) -> dict[str, Any]:
        """Delete a Board Member List row by its autonumber Id."""
        record = await self._find_board_member_by_id(bm_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Board Member List record with id '{bm_id}' not found.",
            )
        table = self._board_member_list_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable board member delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during board member delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "board_member_id": bm_id,
            "deleted": True,
        }

    # ══════════════════════════════════════════════════════════════
    # Organization Info (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════
    _F_OI_ID = _S.AT_F_OI_ID
    _F_OI_TITLE = _S.AT_F_OI_TITLE
    _F_OI_CONTENT = _S.AT_F_OI_CONTENT
    _F_OI_ENTITY = _S.AT_F_OI_ENTITY
    _F_OI_TABS = _S.AT_F_OI_TABS

    _OI_UPDATE_FIELD_MAP = {
        "title": _F_OI_TITLE,
        "content": _F_OI_CONTENT,
        "entity": _F_OI_ENTITY,
        "tabs": _F_OI_TABS,
    }

    def _organization_info_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ORGANIZATION_INFO_TABLE,
        )

    @staticmethod
    def _oi_to_typed(
        records: list[dict[str, Any]],
    ) -> list[OrganizationInfoRecord]:
        return [
            OrganizationInfoRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_organization_info(
        self, *, fields: list[str] | None = None
    ) -> list[OrganizationInfoRecord]:
        """Return all rows from the Organization Info table."""
        if not self._settings.ORGANIZATION_INFO_TABLE:
            return []
        records = await self._list_records(
            self._organization_info_table(), fields=fields
        )
        return self._oi_to_typed(records)

    async def _find_organization_info_by_id(
        self, oi_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(oi_id)
            formula = af.eq_num(self._F_OI_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_OI_ID, str(oi_id))

        table = self._organization_info_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable organization info lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during organization info lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_organization_info(
        self, payload: OrganizationInfoCreate
    ) -> OrganizationInfoRecord:
        """Create a new Organization Info row."""
        body: dict[str, Any] = {
            self._F_OI_TITLE: payload.title,
            self._F_OI_CONTENT: payload.content,
        }
        if payload.entity is not None:
            body[self._F_OI_ENTITY] = payload.entity
        if payload.tabs is not None:
            body[self._F_OI_TABS] = payload.tabs

        table = self._organization_info_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable organization info create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during organization info create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._oi_to_typed([created])[0]

    async def update_organization_info(
        self, oi_id: int | str, payload: OrganizationInfoUpdate
    ) -> OrganizationInfoRecord:
        """Update an Organization Info record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._OI_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_organization_info_by_id(oi_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Organization Info record with id '{oi_id}' not found.",
            )

        table = self._organization_info_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update organization info failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during organization info update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._oi_to_typed([updated])[0]

    async def delete_organization_info(
        self, oi_id: int | str
    ) -> dict[str, Any]:
        """Delete an Organization Info row by its autonumber Id."""
        record = await self._find_organization_info_by_id(oi_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Organization Info record with id '{oi_id}' not found.",
            )
        table = self._organization_info_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable organization info delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during organization info delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "organization_info_id": oi_id,
            "deleted": True,
        }

    # ═══════════════════════════════════════════════════════════════
    # Onboarding (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    def _onboarding_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ONBOARDING_TABLE,
        )

    async def get_onboarding_links(
        self, *, fields: list[str] | None = None
    ) -> list[OnboardingLinkRecord]:
        """Return all rows from the Onboarding table."""
        records = await self._list_records(
            self._onboarding_table(), fields=fields
        )
        return self._to_typed(records, OnboardingLinkRecord)

    # ═══════════════════════════════════════════════════════════════
    # Onboarding Calls (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    def _onboarding_calls_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.ONBOARDING_CALLS_TABLE,
        )

    async def get_onboarding_calls(
        self, *, fields: list[str] | None = None
    ) -> list[OnboardingCallRecord]:
        """Return all rows from the Onboarding Calls table."""
        records = await self._list_records(
            self._onboarding_calls_table(), fields=fields
        )
        return self._to_typed(records, OnboardingCallRecord)

    # ═══════════════════════════════════════════════════════════════
    # Quick Links (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    # Quick Links field name constants (loaded from settings/.env)
    _F_QL_ID = _S.AT_F_QL_ID
    _F_QL_ANCHOR_TEXT = _S.AT_F_QL_ANCHOR_TEXT
    _F_QL_URL = _S.AT_F_QL_URL
    _F_QL_EMAIL = _S.AT_F_QL_EMAIL
    _F_QL_ACTION = _S.AT_F_QL_ACTION
    _F_QL_QA_LINK = _S.AT_F_QL_QUICK_ACTIONS_LINK
    _F_QA_ID = _S.AT_F_QA_ID
    _F_QA_ACTION = _S.AT_F_QA_ACTION

    def _quick_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.QUICK_LINKS_TABLE,
        )

    def _quick_actions_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.QUICK_ACTIONS_TABLE,
        )

    def _ql_to_typed(
        self, records: list[dict[str, Any]]
    ) -> list[QuickLinkRecord]:
        out: list[QuickLinkRecord] = []
        for r in records:
            fields = dict(r.get("fields", {}) or {})
            fields["record_id"] = r["id"]
            out.append(QuickLinkRecord.model_validate(fields))
        return out

    async def get_quick_links(
        self, *, fields: list[str] | None = None
    ) -> list[QuickLinkRecord]:
        """Return all rows from the Quick Links table."""
        records = await self._list_records(
            self._quick_links_table(), fields=fields
        )
        return self._ql_to_typed(records)

    async def _find_quick_link_by_id(
        self, quick_link_id: int | str
    ) -> dict[str, Any] | None:
        """Find a Quick Links record by its autonumber 'Id' value."""
        try:
            numeric = int(quick_link_id)
            formula = af.eq_num(self._F_QL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_QL_ID, str(quick_link_id))

        table = self._quick_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable quick link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick link lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def _find_or_create_quick_action(self, action_text: str) -> str:
        """Return the Airtable record id of the Quick Action with the given
        text, creating it first if no such record exists."""
        text = (action_text or "").strip()
        if not text:
            raise AirtableError("Action text must be a non-empty string.")

        table = self._quick_actions_table()
        formula = af.eq_str(self._F_QA_ACTION, text)
        try:
            existing = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
            if existing:
                return existing[0]["id"]
            created = await asyncio.to_thread(
                table.create, {self._F_QA_ACTION: text}
            )
        except RequestException as exc:
            logger.error("Airtable quick action upsert failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick action upsert")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return created["id"]

    async def create_quick_link(
        self, payload: QuickLinkCreate
    ) -> QuickLinkRecord:
        """Create a Quick Links row linked to a Quick Action.

        The Quick Action is resolved from either ``quick_action_id`` (an
        existing record) or ``action`` (upserted by text).
        """
        if payload.quick_action_id is not None:
            qa_record = await self._find_quick_action_by_id(payload.quick_action_id)
            if qa_record is None:
                raise HTTPException(
                    status_code=_http_status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"Quick Action with Id={payload.quick_action_id} not found."
                    ),
                )
            qa_record_id = qa_record["id"]
        else:
            qa_record_id = await self._find_or_create_quick_action(payload.action)

        body: dict[str, Any] = {
            self._F_QL_ANCHOR_TEXT: payload.anchor_text,
            self._F_QL_QA_LINK: [qa_record_id],
        }
        if payload.url is not None:
            body[self._F_QL_URL] = payload.url
        if payload.email is not None:
            body[self._F_QL_EMAIL] = payload.email

        table = self._quick_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable quick link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick link create")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return self._ql_to_typed([created])[0]

    async def update_quick_link(
        self, quick_link_id: int | str, payload: QuickLinkUpdate
    ) -> QuickLinkRecord:
        """Update fields on a Quick Links row identified by its autonumber Id.

        Providing ``action`` upserts the value into the Quick Actions table
        and replaces the linked record.
        """
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        record = await self._find_quick_link_by_id(quick_link_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Quick Link with Id={quick_link_id} not found.",
            )

        body: dict[str, Any] = {}
        if "anchor_text" in data:
            body[self._F_QL_ANCHOR_TEXT] = data["anchor_text"]
        if "url" in data:
            body[self._F_QL_URL] = data["url"]
        if "email" in data:
            body[self._F_QL_EMAIL] = data["email"]
        if "action" in data and data["action"] is not None:
            qa_record_id = await self._find_or_create_quick_action(data["action"])
            body[self._F_QL_QA_LINK] = [qa_record_id]

        if not body:
            # Nothing changed (e.g. only ``action: None`` was sent).
            return self._ql_to_typed([record])[0]

        table = self._quick_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], body, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable quick link update failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick link update")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return self._ql_to_typed([updated])[0]

    async def delete_quick_link(
        self, quick_link_id: int | str
    ) -> dict[str, Any]:
        """Delete a Quick Links row by its autonumber Id.

        The linked Quick Action row is left intact (not cascade-deleted).
        """
        record = await self._find_quick_link_by_id(quick_link_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Quick Link with Id={quick_link_id} not found.",
            )
        table = self._quick_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable quick link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick link delete")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "quick_link_id": quick_link_id,
            "deleted": True,
        }

    # ═══════════════════════════════════════════════════════════════
    # Quick Actions (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _qa_to_typed(
        records: list[dict[str, Any]],
    ) -> list[QuickActionRecord]:
        return [
            QuickActionRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_quick_actions(
        self, *, fields: list[str] | None = None
    ) -> list[QuickActionRecord]:
        """Return all rows from the Quick Actions table."""
        records = await self._list_records(
            self._quick_actions_table(), fields=fields
        )
        return self._qa_to_typed(records)

    async def _find_quick_action_by_id(
        self, quick_action_id: int | str
    ) -> dict[str, Any] | None:
        """Find a Quick Actions record by its autonumber 'Id' value."""
        try:
            numeric = int(quick_action_id)
            formula = af.eq_num(self._F_QA_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_QA_ID, str(quick_action_id))

        table = self._quick_actions_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable quick action lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick action lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_quick_action(
        self, payload: QuickActionCreate
    ) -> QuickActionRecord:
        """Create a new Quick Actions row from the given 'Action' text."""
        body = {self._F_QA_ACTION: payload.action}
        table = self._quick_actions_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable quick action create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick action create")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._qa_to_typed([created])[0]

    async def update_quick_action(
        self, quick_action_id: int | str, payload: QuickActionUpdate
    ) -> QuickActionRecord:
        """Update the 'Action' text of a Quick Actions row by its Id."""
        record = await self._find_quick_action_by_id(quick_action_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Quick Action with Id={quick_action_id} not found.",
            )
        body = {self._F_QA_ACTION: payload.action}
        table = self._quick_actions_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], body, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable quick action update failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick action update")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._qa_to_typed([updated])[0]

    async def delete_quick_action(
        self, quick_action_id: int | str
    ) -> dict[str, Any]:
        """Delete a Quick Actions row by its autonumber Id."""
        record = await self._find_quick_action_by_id(quick_action_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Quick Action with Id={quick_action_id} not found.",
            )
        table = self._quick_actions_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable quick action delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during quick action delete")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "quick_action_id": quick_action_id,
            "deleted": True,
        }

    # ═══════════════════════════════════════════════════════════════
    # Comms Quick Links (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    _F_CQL_ID = _S.AT_F_CQL_ID
    _F_CQL_ANCHOR_TEXT = _S.AT_F_CQL_ANCHOR_TEXT
    _F_CQL_TYPE = _S.AT_F_CQL_TYPE
    _F_CQL_URL = _S.AT_F_CQL_URL
    _F_CQL_EMAIL = _S.AT_F_CQL_EMAIL

    _CQL_UPDATE_FIELD_MAP = {
        "anchor_text": _F_CQL_ANCHOR_TEXT,
        "type": _F_CQL_TYPE,
        "url": _F_CQL_URL,
        "email": _F_CQL_EMAIL,
    }

    def _comms_quick_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.COMMS_QUICK_LINKS_TABLE,
        )

    @staticmethod
    def _cql_to_typed(
        records: list[dict[str, Any]],
    ) -> list[CommsQuickLinkRecord]:
        return [
            CommsQuickLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_comms_quick_links(
        self, *, fields: list[str] | None = None
    ) -> list[CommsQuickLinkRecord]:
        """Return all rows from the Comms Quick Links table."""
        records = await self._list_records(
            self._comms_quick_links_table(), fields=fields
        )
        return self._cql_to_typed(records)

    async def _find_comms_quick_link_by_id(
        self, cql_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(cql_id)
            formula = af.eq_num(self._F_CQL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_CQL_ID, str(cql_id))

        table = self._comms_quick_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable comms quick link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during comms quick link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_comms_quick_link(
        self, payload: CommsQuickLinkCreate
    ) -> CommsQuickLinkRecord:
        """Create a new Comms Quick Links row."""
        body: dict[str, Any] = {self._F_CQL_ANCHOR_TEXT: payload.anchor_text}
        if payload.type is not None:
            body[self._F_CQL_TYPE] = payload.type
        if payload.url is not None:
            body[self._F_CQL_URL] = payload.url
        if payload.email is not None:
            body[self._F_CQL_EMAIL] = payload.email
        table = self._comms_quick_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable comms quick link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during comms quick link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._cql_to_typed([created])[0]

    async def update_comms_quick_link(
        self, cql_id: int | str, payload: CommsQuickLinkUpdate
    ) -> CommsQuickLinkRecord:
        """Update a Comms Quick Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._CQL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_comms_quick_link_by_id(cql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Comms Quick Links record with id '{cql_id}' not found.",
            )

        table = self._comms_quick_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update comms quick link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during comms quick link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._cql_to_typed([updated])[0]

    async def delete_comms_quick_link(
        self, cql_id: int | str
    ) -> dict[str, Any]:
        """Delete a Comms Quick Links row by its autonumber Id."""
        record = await self._find_comms_quick_link_by_id(cql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Comms Quick Links record with id '{cql_id}' not found.",
            )
        table = self._comms_quick_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable comms quick link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during comms quick link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "comms_quick_link_id": cql_id,
            "deleted": True,
        }

    # ═══════════════════════════════════════════════════════════════
    # HR Quick Links (RenPhil Hub base)
    # ═══════════════════════════════════════════════════════════════
    _F_HRQL_ID = _S.AT_F_HRQL_ID
    _F_HRQL_ANCHOR_TEXT = _S.AT_F_HRQL_ANCHOR_TEXT
    _F_HRQL_TYPE = _S.AT_F_HRQL_TYPE
    _F_HRQL_URL = _S.AT_F_HRQL_URL
    _F_HRQL_EMAIL = _S.AT_F_HRQL_EMAIL

    _HRQL_UPDATE_FIELD_MAP = {
        "anchor_text": _F_HRQL_ANCHOR_TEXT,
        "type": _F_HRQL_TYPE,
        "url": _F_HRQL_URL,
        "email": _F_HRQL_EMAIL,
    }

    def _hr_quick_links_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.HR_QUICK_LINKS_TABLE,
        )

    @staticmethod
    def _hrql_to_typed(
        records: list[dict[str, Any]],
    ) -> list[HrQuickLinkRecord]:
        return [
            HrQuickLinkRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_hr_quick_links(
        self, *, fields: list[str] | None = None
    ) -> list[HrQuickLinkRecord]:
        """Return all rows from the HR Quick Links table."""
        records = await self._list_records(
            self._hr_quick_links_table(), fields=fields
        )
        return self._hrql_to_typed(records)

    async def _find_hr_quick_link_by_id(
        self, hrql_id: int | str
    ) -> dict[str, Any] | None:
        try:
            numeric = int(hrql_id)
            formula = af.eq_num(self._F_HRQL_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_HRQL_ID, str(hrql_id))

        table = self._hr_quick_links_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable hr quick link lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during hr quick link lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def create_hr_quick_link(
        self, payload: HrQuickLinkCreate
    ) -> HrQuickLinkRecord:
        """Create a new HR Quick Links row."""
        body: dict[str, Any] = {self._F_HRQL_ANCHOR_TEXT: payload.anchor_text}
        if payload.type is not None:
            body[self._F_HRQL_TYPE] = payload.type
        if payload.url is not None:
            body[self._F_HRQL_URL] = payload.url
        if payload.email is not None:
            body[self._F_HRQL_EMAIL] = payload.email
        table = self._hr_quick_links_table()
        try:
            created = await asyncio.to_thread(table.create, body, typecast=True)
        except RequestException as exc:
            logger.error("Airtable hr quick link create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during hr quick link create"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._hrql_to_typed([created])[0]

    async def update_hr_quick_link(
        self, hrql_id: int | str, payload: HrQuickLinkUpdate
    ) -> HrQuickLinkRecord:
        """Update an HR Quick Links record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._HRQL_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_hr_quick_link_by_id(hrql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"HR Quick Links record with id '{hrql_id}' not found.",
            )

        table = self._hr_quick_links_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update hr quick link failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during hr quick link update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return self._hrql_to_typed([updated])[0]

    async def delete_hr_quick_link(
        self, hrql_id: int | str
    ) -> dict[str, Any]:
        """Delete an HR Quick Links row by its autonumber Id."""
        record = await self._find_hr_quick_link_by_id(hrql_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"HR Quick Links record with id '{hrql_id}' not found.",
            )
        table = self._hr_quick_links_table()
        try:
            await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable hr quick link delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during hr quick link delete"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "hr_quick_link_id": hrql_id,
            "deleted": True,
        }

    async def get_unique_roles(self) -> list[Role]:
        """Return all Roles (id + Role Name + Scope) with their linked Permissions resolved."""
        name_field = self._settings.ROLES_NAME_FIELD
        perms_field = self._settings.ROLES_PERMISSIONS_FIELD
        scope_field = self._settings.ROLES_SCOPE_FIELD

        # Fetch roles and the permissions catalog in parallel.
        roles_records, permissions = await asyncio.gather(
            self._list_records(
                self._roles_table(),
                fields=[name_field, perms_field, scope_field],
            ),
            self.get_unique_permissions(),
        )

        perm_by_id: dict[str, Permission] = {p.id: p for p in permissions}

        seen: dict[str, Role] = {}
        for r in roles_records:
            fields = r.get("fields", {}) or {}
            name = fields.get(name_field)
            name_str = name.strip() if isinstance(name, str) else None
            scope = fields.get(scope_field)
            scope_str = scope.strip() if isinstance(scope, str) else None

            linked_perm_ids = fields.get(perms_field) or []
            if not isinstance(linked_perm_ids, list):
                linked_perm_ids = [linked_perm_ids]

            role_permissions: list[Permission] = []
            for pid in linked_perm_ids:
                if not isinstance(pid, str):
                    continue
                perm = perm_by_id.get(pid)
                if perm is not None:
                    role_permissions.append(perm)
                else:
                    # Fallback: linked record not found in catalog.
                    role_permissions.append(
                        Permission(id=pid, name=None, description=None)
                    )

            seen[r["id"]] = Role(
                id=r["id"],
                name=name_str or None,
                scope=scope_str or None,
                permissions=role_permissions,
            )
        return sorted(seen.values(), key=lambda x: (x.name or "").lower())

    async def get_unique_permissions(self) -> list[Permission]:
        """Return all Permissions with id + Permission Name + Description."""
        name_field = self._settings.PERMISSIONS_NAME_FIELD
        desc_field = self._settings.PERMISSIONS_DESCRIPTION_FIELD
        records = await self._list_records(
            self._permissions_table(), fields=[name_field, desc_field]
        )
        seen: dict[str, Permission] = {}
        for r in records:
            fields = r.get("fields", {}) or {}
            name = fields.get(name_field)
            name_str = name.strip() if isinstance(name, str) else None
            desc = fields.get(desc_field)
            desc_str = desc.strip() if isinstance(desc, str) else None
            seen[r["id"]] = Permission(
                id=r["id"], name=name_str or None, description=desc_str or None
            )
        return sorted(seen.values(), key=lambda x: (x.name or "").lower())

    async def create_role(self, payload: RoleCreate) -> Role:
        """Create a new Role record."""
        s = self._settings
        name_field = s.ROLES_NAME_FIELD
        perms_field = s.ROLES_PERMISSIONS_FIELD
        scope_field = s.ROLES_SCOPE_FIELD
        table = self._roles_table()

        fields: dict[str, Any] = {name_field: payload.name}
        if payload.scope is not None:
            fields[scope_field] = payload.scope.strip()
        if payload.permissions:
            fields[perms_field] = list(dict.fromkeys(payload.permissions))

        try:
            result = await asyncio.to_thread(table.create, fields)
        except RequestException as exc:
            logger.error("Airtable role create failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during role create")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        permissions_catalog = await self.get_unique_permissions()
        perm_by_id = {p.id: p for p in permissions_catalog}
        result_fields = result.get("fields", {}) or {}
        linked_perm_ids = result_fields.get(perms_field) or []
        if not isinstance(linked_perm_ids, list):
            linked_perm_ids = [linked_perm_ids]
        role_permissions: list[Permission] = []
        for pid in linked_perm_ids:
            if not isinstance(pid, str):
                continue
            perm = perm_by_id.get(pid)
            role_permissions.append(
                perm if perm is not None
                else Permission(id=pid, name=None, description=None)
            )

        result_name = result_fields.get(name_field)
        result_scope = result_fields.get(scope_field)
        return Role(
            id=result["id"],
            name=result_name.strip() if isinstance(result_name, str) and result_name.strip() else None,
            scope=result_scope.strip() if isinstance(result_scope, str) and result_scope.strip() else None,
            permissions=role_permissions,
        )

    async def update_role(self, role_id: str, payload: RoleUpdate) -> Role:
        """Update a Role record: name, scope, and/or linked Permissions.

        When ``payload.permissions`` is provided it replaces the linked
        list; otherwise the linked list is incrementally edited using
        ``add_permissions`` / ``remove_permissions``.
        """
        s = self._settings
        name_field = s.ROLES_NAME_FIELD
        perms_field = s.ROLES_PERMISSIONS_FIELD
        scope_field = s.ROLES_SCOPE_FIELD
        table = self._roles_table()

        try:
            existing = await asyncio.to_thread(table.get, role_id)
        except RequestException as exc:
            logger.error("Airtable role fetch failed: %s", exc)
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Role '{role_id}' not found.",
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error fetching role")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        fields_existing = existing.get("fields", {}) or {}
        update_fields: dict[str, Any] = {}

        if payload.name is not None:
            update_fields[name_field] = payload.name.strip()
        if payload.scope is not None:
            update_fields[scope_field] = payload.scope.strip()

        if payload.permissions is not None:
            update_fields[perms_field] = list(
                dict.fromkeys(payload.permissions)
            )
        elif payload.add_permissions or payload.remove_permissions:
            current = list(fields_existing.get(perms_field) or [])
            to_remove = set(payload.remove_permissions or [])
            to_add = list(payload.add_permissions or [])
            new_perms = [p for p in current if p not in to_remove]
            for pid in to_add:
                if pid not in new_perms:
                    new_perms.append(pid)
            if new_perms != current:
                update_fields[perms_field] = new_perms

        if not update_fields:
            result = existing
        else:
            try:
                result = await asyncio.to_thread(
                    table.update, role_id, update_fields
                )
            except RequestException as exc:
                logger.error("Airtable role update failed: %s", exc)
                raise AirtableError(f"Airtable API error: {exc}") from exc
            except Exception as exc:
                logger.exception("Unexpected Airtable error during role update")
                raise AirtableError(f"Airtable API error: {exc}") from exc

        # Build the resolved Role with Permission objects from the catalog.
        permissions_catalog = await self.get_unique_permissions()
        perm_by_id = {p.id: p for p in permissions_catalog}
        result_fields = result.get("fields", {}) or {}
        linked_perm_ids = result_fields.get(perms_field) or []
        if not isinstance(linked_perm_ids, list):
            linked_perm_ids = [linked_perm_ids]
        role_permissions: list[Permission] = []
        for pid in linked_perm_ids:
            if not isinstance(pid, str):
                continue
            perm = perm_by_id.get(pid)
            role_permissions.append(
                perm if perm is not None
                else Permission(id=pid, name=None, description=None)
            )

        result_name = result_fields.get(name_field)
        result_scope = result_fields.get(scope_field)
        return Role(
            id=result["id"],
            name=result_name.strip() if isinstance(result_name, str) and result_name.strip() else None,
            scope=result_scope.strip() if isinstance(result_scope, str) and result_scope.strip() else None,
            permissions=role_permissions,
        )

    async def delete_role(self, role_id: str) -> None:
        """Delete a Role record from the Roles table by record id."""
        table = self._roles_table()
        try:
            await asyncio.to_thread(table.delete, role_id)
        except RequestException as exc:
            msg = str(exc)
            if "404" in msg or "NOT_FOUND" in msg.upper():
                raise HTTPException(
                    status_code=_http_status.HTTP_404_NOT_FOUND,
                    detail=f"Role '{role_id}' not found.",
                ) from exc
            logger.error("Airtable role delete failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during role delete")
            raise AirtableError(f"Airtable API error: {exc}") from exc

    # ══════════════════════════════════════════════════════════════════
    # Tickets (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════════
    def _feedbacks_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.FEEDBACKS_TABLE,
        )

    # Feedback field name constants (loaded from settings/.env)
    _F_FEEDBACK_FROM = _S.AT_F_FEEDBACK_FROM
    _F_FEEDBACK_MESSAGE = _S.AT_F_FEEDBACK_MESSAGE
    _F_FEEDBACK_SOURCE = _S.AT_F_FEEDBACK_SOURCE
    _F_FEEDBACK_IMPRESSION = _S.AT_F_FEEDBACK_IMPRESSION
    _F_FEEDBACK_MESSAGE_ID = _S.AT_F_FEEDBACK_MESSAGE_ID
    _F_FEEDBACK_QUERY = _S.AT_F_FEEDBACK_QUERY
    _F_FEEDBACK_RESPONSE = _S.AT_F_FEEDBACK_RESPONSE

    async def create_feedback(
        self,
        payload: FeedbackCreate,
        *,
        from_email: str,
    ) -> FeedbackRecord:
        """Create a feedback record with authenticated identity.

        ``Date & Time`` is a computed Airtable field and is not written here.
        V1 message-level fields are optional so the existing global feedback
        widget remains backward-compatible.
        """
        fields: dict[str, Any] = {
            self._F_FEEDBACK_FROM: from_email,
            self._F_FEEDBACK_MESSAGE: payload.message,
        }
        optional_fields = (
            (self._F_FEEDBACK_SOURCE, payload.source),
            (self._F_FEEDBACK_IMPRESSION, payload.impression),
            (self._F_FEEDBACK_MESSAGE_ID, payload.message_id),
            (self._F_FEEDBACK_QUERY, payload.query),
            (self._F_FEEDBACK_RESPONSE, payload.response),
        )
        for field_name, value in optional_fields:
            if value is not None and (not isinstance(value, str) or value.strip()):
                fields[field_name] = value

        table = self._feedbacks_table()
        try:
            created = await asyncio.to_thread(table.create, fields, typecast=True)
        except RequestException as exc:
            logger.error("Airtable create feedback failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during feedback create")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return FeedbackRecord.model_validate(
            {"id": created["id"], **created.get("fields", {})}
        )

    def _tickets_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.TICKETS_TABLE,
        )

    # Ticket field name constants (loaded from settings/.env)
    _F_TICKET_ID = _S.AT_F_TICKET_ID
    _F_TICKET_TITLE = _S.AT_F_TICKET_TITLE
    _F_TICKET_DESCRIPTION = _S.AT_F_TICKET_DESCRIPTION
    _F_TICKET_STATUS = _S.AT_F_TICKET_STATUS
    _F_TICKET_ASSIGNEE = _S.AT_F_TICKET_ASSIGNEE
    _F_TICKET_ASSIGNED_BY = _S.AT_F_TICKET_ASSIGNED_BY
    _F_TICKET_SOURCE = _S.AT_F_TICKET_SOURCE
    _F_TICKET_CREATED_DATE = _S.AT_F_TICKET_CREATED_DATE
    _F_TICKET_DUE_DATE = _S.AT_F_TICKET_DUE_DATE
    _F_TICKET_LAST_UPDATED = _S.AT_F_TICKET_LAST_UPDATED
    _F_TICKET_LAST_UPDATED_BY = _S.AT_F_TICKET_LAST_UPDATED_BY
    _F_TICKET_COMMENTS = _S.AT_F_TICKET_COMMENTS
    # Linked-record field on the Tickets table used to write the parent
    # relationship. The corresponding lookup field ("Parent Ticket Id")
    # is read-only and only used when returning tickets to clients.
    _F_TICKET_PARENT_LINK = _S.AT_F_TICKET_PARENT_LINK

    async def _resolve_parent_ticket_record_id(
        self, parent_ticket_id: int | str
    ) -> str:
        """Resolve a parent ticket's Airtable record id from its 'Id' value."""
        parent = await self._find_ticket_by_id(parent_ticket_id)
        if parent is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Parent ticket with id '{parent_ticket_id}' not found."
                ),
            )
        return parent["id"]

    async def list_tickets(self) -> list[TicketRecord]:
        """Return all tickets from the Tickets table."""
        records = await self._list_records(self._tickets_table())
        return self._to_typed(records, TicketRecord)

    async def list_tickets_by_assignee(self, assignee_email: str) -> list[TicketRecord]:
        """Return tickets whose 'Assignee' field matches the given email (case-insensitive)."""
        target = (assignee_email or "").strip().lower()
        if not target:
            return []
        # Airtable string equality is case-sensitive; use LOWER() for case-insensitive match.
        formula = f"LOWER({af.field_ref(self._F_TICKET_ASSIGNEE)})='{af.escape(target)}'"
        records = await self._list_records(self._tickets_table(), formula=formula)
        return self._to_typed(records, TicketRecord)

    async def create_ticket_from_slack(
        self, payload: SlackTicketWebhookPayload
    ) -> TicketRecord:
        """Create a ticket from a Slack webhook event.

        - ``Source`` is forced to ``"Slack"``.
        - ``Created Date`` is set to the current UTC time.
        - ``Status`` is left empty so Airtable applies its default ("Open").
        """
        fields: dict[str, Any] = {
            self._F_TICKET_TITLE: payload.title,
            self._F_TICKET_ASSIGNEE: payload.assignee,
            self._F_TICKET_ASSIGNED_BY: payload.assigned_by,
            self._F_TICKET_SOURCE: "Slack",
            self._F_TICKET_CREATED_DATE: self._iso(datetime.utcnow()),
        }
        if payload.due_date is not None:
            fields[self._F_TICKET_DUE_DATE] = self._iso(payload.due_date)
        if payload.description is not None:
            fields[self._F_TICKET_DESCRIPTION] = payload.description

        table = self._tickets_table()
        try:
            created = await asyncio.to_thread(table.create, fields, typecast=True)
        except RequestException as exc:
            logger.error("Airtable create slack ticket failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during slack ticket create")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return TicketRecord.model_validate(
            {"id": created["id"], **created.get("fields", {})}
        )

    async def create_ticket_partial(
        self,
        *,
        source: str,
        assigned_by: str,
        title: str | None = None,
        description: str | None = None,
        assignee: str | None = None,
        due_date: datetime | None = None,
    ) -> TicketRecord:
        """Create a ticket from a webhook source (Slack, Email, …).

        - ``Source`` is forced to the provided ``source`` value.
        - ``Created Date`` is set to the current UTC time.
        - ``Status`` is left empty so Airtable applies its default ("Open").
        - At least one of ``title`` or ``description`` must be provided;
          any other missing field is simply omitted from the create call.
        """
        if not title and not description:
            raise ValueError(
                "create_ticket_partial requires at least a title or a description."
            )

        fields: dict[str, Any] = {
            self._F_TICKET_ASSIGNED_BY: assigned_by,
            self._F_TICKET_SOURCE: source,
            self._F_TICKET_CREATED_DATE: self._iso(datetime.utcnow()),
        }
        if title is not None:
            fields[self._F_TICKET_TITLE] = title
        if description is not None:
            fields[self._F_TICKET_DESCRIPTION] = description
        if assignee is not None:
            fields[self._F_TICKET_ASSIGNEE] = assignee
        if due_date is not None:
            fields[self._F_TICKET_DUE_DATE] = self._iso(due_date)

        table = self._tickets_table()
        try:
            created = await asyncio.to_thread(table.create, fields, typecast=True)
        except RequestException as exc:
            logger.error("Airtable create partial ticket failed (source=%s): %s", source, exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partial ticket create (source=%s)",
                source,
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return TicketRecord.model_validate(
            {"id": created["id"], **created.get("fields", {})}
        )

    async def create_ticket_from_email(
        self,
        *,
        assigned_by: str,
        title: str | None = None,
        description: str | None = None,
        assignee: str | None = None,
        due_date: datetime | None = None,
    ) -> TicketRecord:
        """Create a ticket from the email-based assignment webhook.

        Thin wrapper around :meth:`create_ticket_partial` with ``source="Email"``.
        """
        return await self.create_ticket_partial(
            source="Email",
            assigned_by=assigned_by,
            title=title,
            description=description,
            assignee=assignee,
            due_date=due_date,
        )

    async def create_ticket(self, payload: TicketCreate) -> TicketRecord:
        """Create a new ticket record."""
        fields: dict[str, Any] = {
            self._F_TICKET_TITLE: payload.title,
            self._F_TICKET_ASSIGNEE: payload.assignee,
            self._F_TICKET_ASSIGNED_BY: payload.assigned_by,
            self._F_TICKET_STATUS: payload.status,
            self._F_TICKET_SOURCE: payload.source,
            self._F_TICKET_CREATED_DATE: self._iso(payload.created_date),
            self._F_TICKET_DUE_DATE: self._iso(payload.due_date),
        }
        if payload.description is not None:
            fields[self._F_TICKET_DESCRIPTION] = payload.description
        if payload.parent_ticket_id is not None:
            parent_record_id = await self._resolve_parent_ticket_record_id(
                payload.parent_ticket_id
            )
            fields[self._F_TICKET_PARENT_LINK] = [parent_record_id]

        table = self._tickets_table()
        try:
            created = await asyncio.to_thread(table.create, fields, typecast=True)
        except RequestException as exc:
            logger.error("Airtable create ticket failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during ticket create")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return TicketRecord.model_validate(
            {"id": created["id"], **created.get("fields", {})}
        )

    async def _find_ticket_by_id(
        self, ticket_id: int | str
    ) -> dict[str, Any] | None:
        """Find a ticket record by its 'Id' value."""
        try:
            numeric = int(ticket_id)
            formula = af.eq_num(self._F_TICKET_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_TICKET_ID, str(ticket_id))

        table = self._tickets_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable ticket lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during ticket lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def get_ticket_by_id(self, ticket_id: int | str) -> dict[str, Any]:
        """Return the raw ticket record by Id, or raise 404 if not found."""
        record = await self._find_ticket_by_id(ticket_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket with id '{ticket_id}' not found.",
            )
        return record

    _TICKET_UPDATE_FIELD_MAP = {
        "title": _F_TICKET_TITLE,
        "description": _F_TICKET_DESCRIPTION,
        "status": _F_TICKET_STATUS,
        "assignee": _F_TICKET_ASSIGNEE,
        "comments": _F_TICKET_COMMENTS,
    }

    async def update_ticket(
        self,
        ticket_id: int | str,
        payload: TicketUpdate,
        *,
        updated_by_email: str,
        existing: dict[str, Any] | None = None,
    ) -> TicketRecord:
        """Update fields on a ticket identified by Id.

        ``Last Updated`` and ``Last Updated By`` are set automatically.
        """
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {}
        for key, value in data.items():
            if key == "due_date":
                update_fields[self._F_TICKET_DUE_DATE] = self._iso(value)
            elif key == "parent_ticket_id":
                if value is None:
                    update_fields[self._F_TICKET_PARENT_LINK] = []
                else:
                    parent_record_id = (
                        await self._resolve_parent_ticket_record_id(value)
                    )
                    update_fields[self._F_TICKET_PARENT_LINK] = [
                        parent_record_id
                    ]
            else:
                update_fields[self._TICKET_UPDATE_FIELD_MAP[key]] = value

        update_fields[self._F_TICKET_LAST_UPDATED_BY] = updated_by_email

        record = existing or await self._find_ticket_by_id(ticket_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket with id '{ticket_id}' not found.",
            )

        table = self._tickets_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update ticket failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during ticket update")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return TicketRecord.model_validate(
            {"id": updated["id"], **updated.get("fields", {})}
        )

    async def delete_ticket(
        self,
        ticket_id: int | str,
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Delete a ticket identified by its Id field."""
        record = existing or await self._find_ticket_by_id(ticket_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket with id '{ticket_id}' not found.",
            )
        table = self._tickets_table()
        try:
            result = await asyncio.to_thread(table.delete, record["id"])
        except RequestException as exc:
            logger.error("Airtable delete ticket failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during ticket delete")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return {
            "id": record["id"],
            "ticket_id": ticket_id,
            "deleted": bool(result.get("deleted", True))
            if isinstance(result, dict)
            else True,
        }

    # ══════════════════════════════════════════════════════════════════
    # Users (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════════
    _USER_UPDATE_FIELD_MAP = {
        "name": _S.USERS_NAME_FIELD,
        "first_name": _S.USERS_FIRST_NAME_FIELD,
        "last_name": _S.USERS_LAST_NAME_FIELD,
        "employment_type": _S.USERS_EMPLOYMENT_TYPE_FIELD,
        "status": _S.USERS_STATUS_FIELD,
        "department": _S.USERS_DEPARTMENT_FIELD,
        "program": _S.USERS_PROGRAM_FIELD,
        "start_date": _S.USERS_START_DATE_FIELD,
        "work_email": _S.USERS_WORK_EMAIL_FIELD,
        "personal_email": _S.USERS_PERSONAL_EMAIL_FIELD,
        "position": _S.USERS_POSITION_FIELD,
        "dob": _S.USERS_DOB_FIELD,
        "office_location": _S.USERS_OFFICE_LOCATION_FIELD,
        "home_address": _S.USERS_HOME_ADDRESS_FIELD,
        "bio": _S.USERS_BIO_FIELD,
        "scope_of_work": _S.USERS_SCOPE_OF_WORK_FIELD,
        "end_date": _S.USERS_END_DATE_FIELD,
        "manager": _S.USERS_MANAGER_FIELD,
        "tech_stack_selections": _S.USERS_TECH_STACK_SELECTIONS_FIELD,
    }

    def _users_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.USERS_TABLE,
        )

    async def _find_user_by_work_email(
        self, work_email: str
    ) -> dict[str, Any] | None:
        """Find a user record by exact (case-insensitive) Work Email."""
        normalized = (work_email or "").strip().lower()
        if not normalized:
            return None
        email_field = self._settings.USERS_WORK_EMAIL_FIELD
        formula = (
            f"LOWER({{{email_field}}}) = '{self._escape(normalized)}'"
        )
        table = self._users_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable user lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during user lookup")
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def get_user_by_work_email(self, work_email: str) -> UserRecord:
        """Return the user record matching the given Work Email."""
        record = await self._find_user_by_work_email(work_email)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"User with Work Email '{work_email}' not found.",
            )
        return UserRecord.model_validate(
            {"id": record["id"], **record.get("fields", {})}
        )

    async def update_user_by_work_email(
        self,
        work_email: str,
        payload: UserUpdate,
        *,
        existing: dict[str, Any] | None = None,
    ) -> UserRecord:
        """Update the user record identified by Work Email with the provided fields."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {}
        for key, value in data.items():
            if key == "headshot":
                update_fields[self._settings.USERS_HEADSHOT_FIELD] = (
                    self._attachments_payload(value) or []
                )
            elif key in ("employment_type", "tech_stack_selections"):
                update_fields[self._USER_UPDATE_FIELD_MAP[key]] = (
                    list(value) if value else []
                )
            else:
                update_fields[self._USER_UPDATE_FIELD_MAP[key]] = value

        record = existing or await self._find_user_by_work_email(work_email)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=f"User with Work Email '{work_email}' not found.",
            )

        table = self._users_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update user failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Airtable error during user update")
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return UserRecord.model_validate(
            {"id": updated["id"], **updated.get("fields", {})}
        )


