"""
Airtable service.

Connects to Airtable bases via the ``pyairtable`` library and exposes
aggregation helpers used by the analytics router.  ``pyairtable`` is
synchronous (built on ``requests``), so calls are dispatched to a
worker thread to remain compatible with FastAPI's async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable

from pyairtable import Api
from requests.exceptions import RequestException
from fastapi import HTTPException, status as _http_status

from app.config import Settings, get_settings
from app.helpers import airtable_formulas as af
from app.helpers.exceptions import AirtableError
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
    SlackTicketWebhookPayload,
    EmailTicketWebhookPayload,
    CheckinReportingPeriodRecord,
    ClusterRecord,
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
    OrgFriendsRecord,
    PartnershipsFundraisingRecord,
    PartnershipsFundraisingUpdate,
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
_STATUS_ACTIVE_PROGRAM = "3. Active Program"
_STATUS_PUBLICLY_LAUNCHED = "4. Publicly Launched"
_STATUS_FELLOWSHIP_SCOPING = "2. Fellowship (Scoping)"
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
_F_DASHBOARD_DISPLAY = _S.AT_F_DASHBOARD_DISPLAY
_F_FOLLOWUP_INDICATED = _S.AT_F_FOLLOWUP_INDICATED
_F_DEADLINE = _S.AT_F_DEADLINE
_F_REVIEW_UNTIL = _S.AT_F_REVIEW_UNTIL
_F_PERIOD = _S.AT_F_PERIOD


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
    # Role scope value that targets the 'Function' single-select field.
    FUNCTION_SCOPE = "Function"

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
        seen: set[tuple[str, str, str, str]] = set()
        for rec in records:
            ac = self._build_access_control_record(rec)
            for role in ac.roles:
                catalog_role = role_by_id.get(role.id)
                scope = catalog_role.scope if catalog_role else None
                name = role.name or (catalog_role.name if catalog_role else None)
                scope_norm = (scope or "").strip().lower()
                if scope_norm == self.HUB_SCOPE.lower():
                    fund_or_program = None
                    function = None
                elif scope_norm == self.FUNCTION_SCOPE.lower():
                    fund_or_program = None
                    function = ac.function
                else:
                    fund_or_program = ac.fund_or_program_name
                    function = None
                key = (
                    name or "",
                    scope or "",
                    fund_or_program or "",
                    function or "",
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ScopedRole(
                        role_name=name,
                        scope=scope,
                        fund_or_program_name=fund_or_program,
                        function=function,
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
        onboarding_empty: bool | None = None,
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
            af.empty_clause(_F_ONBOARDING_STATUS, onboarding_empty),
        ]

        # Status filtering:
        #   * status_list: substring match — the Status field contains
        #     any of the provided values (OR across values).
        #   * not_status_list: exact-value exclusion.
        #   * status_empty / not-empty.
        # The membership clause and empty clause are unioned (OR).
        status_membership_parts: list[str | None] = []
        if status_list:
            status_membership_parts.append(
                af.contains_any_str(_F_STATUS, status_list)
            )
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

        records = await self._list_records(
            self._monthly_checkin_table(), formula=formula, fields=fields
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
        return self._to_typed(records, ShareableDocsRecord)

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
        '3. Active Program' or '4. Publicly Launched'.
        """
        formula = af.in_str(_F_STATUS, list(_ACTIVE_PROGRAM_STATUSES))
        records = await self._list_records(
            self._master_list_table(), formula=formula, fields=[_F_STATUS]
        )
        return CountResponse(count=len(records))

    # ── /get_active_programs ──────────────────────────────────────────
    async def get_active_programs(self) -> list[ActiveProgramItem]:
        """List active programs with their lead/fellow assignment.

        Reads records from MASTER_LIST whose Status equals
        '3. Active Program' or '4. Publicly Launched', returning the
        'Name' and 'Program Lead/Fellow' fields.
        """
        formula = af.in_str(_F_STATUS, list(_ACTIVE_PROGRAM_STATUSES))
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
        """Count distinct Work Email values of fellows in the Users table.

        A user is counted as a fellow when EITHER:
          * the 'Employment Type' multi-select contains 'Fellow (Unpaid)', OR
          * the 'For Website' single-select equals 'Fellow'.
        """
        records = await self._fetch_fellow_records()
        unique: set[str] = set()
        for r in records:
            value = r.get("fields", {}).get(self._settings.USERS_WORK_EMAIL_FIELD)
            if not value:
                continue
            text = str(value).strip().lower()
            if text:
                unique.add(text)
        return CountResponse(count=len(unique))

    # ── /get_distinct_fellows ─────────────────────────────────────────
    async def get_distinct_fellows(self) -> list[PersonContactItem]:
        """List unique fellows with their First Name, Last Name and Work Email.

        Uses the same table and filters as :meth:`get_distinct_fellows_count`;
        de-duplicated by lower-cased Work Email. Records without a Work
        Email are returned as-is (not de-duplicated).
        """
        records = await self._fetch_fellow_records(include_names=True)
        s = self._settings
        seen: set[str] = set()
        items: list[PersonContactItem] = []
        for r in records:
            fields = r.get("fields", {}) or {}
            email = fields.get(s.USERS_WORK_EMAIL_FIELD)
            email_key = str(email).strip().lower() if email else ""
            if email_key:
                if email_key in seen:
                    continue
                seen.add(email_key)
            items.append(
                PersonContactItem(
                    first_name=fields.get(s.USERS_FIRST_NAME_FIELD),
                    last_name=fields.get(s.USERS_LAST_NAME_FIELD),
                    work_email=email,
                )
            )
        items.sort(
            key=lambda x: (
                (x.last_name or "").lower(),
                (x.first_name or "").lower(),
            )
        )
        return items

    async def _fetch_fellow_records(
        self, *, include_names: bool = False
    ) -> list[dict[str, Any]]:
        """Return raw Users records matching the fellow criteria."""
        s = self._settings
        formula = af.OR(
            af.multiselect_contains_any(
                s.USERS_EMPLOYMENT_TYPE_FIELD, ["Fellow (Unpaid)"]
            ),
            af.eq_str(s.USERS_FOR_WEBSITE_FIELD, "Fellow"),
        )
        fields = [s.USERS_WORK_EMAIL_FIELD]
        if include_names:
            fields = [
                s.USERS_FIRST_NAME_FIELD,
                s.USERS_LAST_NAME_FIELD,
                s.USERS_WORK_EMAIL_FIELD,
            ]
        return await self._list_records(
            self._users_table(), formula=formula, fields=fields
        )

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

        # Function is a single-select; may also come back as a list if the
        # Airtable column is later changed — handle both shapes defensively.
        function_raw = fields.get(s.ACCESS_CONTROL_FUNCTION_FIELD)
        if isinstance(function_raw, list):
            fn_items = [
                str(v).strip()
                for v in function_raw
                if v is not None and str(v).strip()
            ]
            function_value = ", ".join(fn_items) if fn_items else None
        else:
            function_value = _str_or_none(function_raw)

        return AccessControlRecord(
            id=record["id"],
            user_email=_str_or_none(
                fields.get(s.ACCESS_CONTROL_USER_EMAIL_FIELD)
            ),
            roles=roles,
            permissions=permissions,
            fund_or_program_name=fund_or_program_name,
            function=function_value,
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

    async def upsert_access_control(
        self, payload: AccessControlAssign
    ) -> AccessControlRecord:
        """Assign role(s) and/or permission(s) to an email (upsert + merge)."""
        email_field = self._settings.ACCESS_CONTROL_USER_EMAIL_FIELD
        roles_field = self._settings.ACCESS_CONTROL_ROLES_FIELD
        permissions_field = self._settings.ACCESS_CONTROL_PERMISSIONS_FIELD
        fund_field = self._settings.ACCESS_CONTROL_FUND_OR_PROGRAM_NAME_FIELD
        function_field = self._settings.ACCESS_CONTROL_FUNCTION_FIELD

        roles_in = list(payload.roles or [])
        permissions_in = list(payload.permissions or [])
        fund_in = payload.fund_or_program_name
        function_in = payload.function
        table = self._access_control_table()

        existing = await self._find_access_control_by_email(payload.user_email)
        try:
            if existing is None:
                fields: dict[str, Any] = {email_field: payload.user_email.strip()}
                if roles_in:
                    fields[roles_field] = roles_in
                if permissions_in:
                    fields[permissions_field] = permissions_in
                if fund_in is not None:
                    fields[fund_field] = [fund_in] if fund_in else None
                if function_in is not None:
                    fields[function_field] = function_in or None
                result = await asyncio.to_thread(
                    table.create, fields, typecast=True
                )
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
                if fund_in is not None:
                    update_fields[fund_field] = [fund_in] if fund_in else None
                if function_in is not None:
                    update_fields[function_field] = function_in or None

                if not update_fields:
                    result = existing
                else:
                    result = await asyncio.to_thread(
                        table.update,
                        existing["id"],
                        update_fields,
                        typecast=True,
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
        fund_field = self._settings.ACCESS_CONTROL_FUND_OR_PROGRAM_NAME_FIELD
        function_field = self._settings.ACCESS_CONTROL_FUNCTION_FIELD

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
        if payload.clear_fund_or_program_name:
            update_fields[fund_field] = None
        if payload.clear_function:
            update_fields[function_field] = None

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

    async def get_team_size(self) -> CountResponse:
        """Return the number of distinct non-empty 'Name' values in the Users
        table where the 'Status' single-select equals 'Active'."""
        name_field = self._settings.USERS_NAME_FIELD
        formula = af.eq_str(self._settings.USERS_STATUS_FIELD, "Active")
        records = await self._list_records(
            self._users_table(), formula=formula, fields=[name_field]
        )
        unique: set[str] = set()
        for r in records:
            value = r.get("fields", {}).get(name_field)
            if isinstance(value, str) and value.strip():
                unique.add(value.strip())
        return CountResponse(count=len(unique))

    async def get_team_members(self) -> list[PersonContactItem]:
        """Return the team members (First Name, Last Name, Work Email)
        from the Users table where the 'Status' single-select equals
        'Active'. Uses the same table and filter as :meth:`get_team_size`;
        de-duplicated by 'Name'."""
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
            ],
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
    # Partnerships Fundraising (RenPhil Hub base)
    # ══════════════════════════════════════════════════════════════════
    _F_PF_ID = _S.AT_F_PF_ID
    _F_PF_DOCUMENT = _S.AT_F_PF_DOCUMENT
    _F_PF_DOCUMENT_URL = _S.AT_F_PF_DOCUMENT_URL
    _F_PF_NOTES = _S.AT_F_PF_NOTES

    _PF_UPDATE_FIELD_MAP = {
        "document": _F_PF_DOCUMENT,
        "document_url": _F_PF_DOCUMENT_URL,
        "notes": _F_PF_NOTES,
    }

    def _partnerships_fundraising_table(self):
        return self._api.table(
            self._settings.RENPHIL_HUB_BASE_ID,
            self._settings.PARTNERSHIPS_FUNDRAISING_TABLE,
        )

    @staticmethod
    def _pf_to_typed(
        records: list[dict[str, Any]],
    ) -> list[PartnershipsFundraisingRecord]:
        """Convert raw records to PartnershipsFundraisingRecord instances.

        Maps the Airtable record id to ``record_id`` (instead of ``id``)
        so the table's autonumber ``Id`` field can be exposed as ``id``.
        """
        return [
            PartnershipsFundraisingRecord.model_validate(
                {"record_id": r["id"], **r.get("fields", {})}
            )
            for r in records
        ]

    async def get_partnerships_fundraising(
        self, *, fields: list[str] | None = None
    ) -> list[PartnershipsFundraisingRecord]:
        """Return all rows from the Partnerships Fundraising table.

        The 'Document URL' field may be empty when 'Document' does not
        refer to an actual document.
        """
        records = await self._list_records(
            self._partnerships_fundraising_table(), fields=fields
        )
        return self._pf_to_typed(records)

    async def _find_partnerships_fundraising_by_id(
        self, pf_id: int | str
    ) -> dict[str, Any] | None:
        """Find a Partnerships Fundraising record by its 'Id' value."""
        try:
            numeric = int(pf_id)
            formula = af.eq_num(self._F_PF_ID, numeric)
        except (TypeError, ValueError):
            formula = af.eq_str(self._F_PF_ID, str(pf_id))

        table = self._partnerships_fundraising_table()
        try:
            records = await asyncio.to_thread(
                table.all, formula=formula, max_records=1
            )
        except RequestException as exc:
            logger.error("Airtable partnerships fundraising lookup failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partnerships fundraising lookup"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc
        return records[0] if records else None

    async def update_partnerships_fundraising(
        self, pf_id: int | str, payload: PartnershipsFundraisingUpdate
    ) -> PartnershipsFundraisingRecord:
        """Update a Partnerships Fundraising record identified by its Id."""
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AirtableError("No fields provided to update.")

        update_fields: dict[str, Any] = {
            self._PF_UPDATE_FIELD_MAP[key]: value for key, value in data.items()
        }

        record = await self._find_partnerships_fundraising_by_id(pf_id)
        if record is None:
            raise HTTPException(
                status_code=_http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Partnerships Fundraising record with id '{pf_id}' not found."
                ),
            )

        table = self._partnerships_fundraising_table()
        try:
            updated = await asyncio.to_thread(
                table.update, record["id"], update_fields, typecast=True
            )
        except RequestException as exc:
            logger.error("Airtable update partnerships fundraising failed: %s", exc)
            raise AirtableError(f"Airtable API error: {exc}") from exc
        except Exception as exc:
            logger.exception(
                "Unexpected Airtable error during partnerships fundraising update"
            )
            raise AirtableError(f"Airtable API error: {exc}") from exc

        return PartnershipsFundraisingRecord.model_validate(
            {"record_id": updated["id"], **updated.get("fields", {})}
        )

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
        """Create a Quick Links row, upserting the linked Quick Action."""
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


