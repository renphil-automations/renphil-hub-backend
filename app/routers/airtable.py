"""
Airtable router — fundraising analytics endpoints.

All endpoints are read-only and require authentication.  Filters
follow the same pattern across endpoints:

  * ``eq_year``    — Fiscal Year exact match (optional)
  * ``lt_year``    — Fiscal Year strict upper bound (optional)
  * ``gt_year``    — Fiscal Year strict lower bound (optional)
  * ``opportunity_rec_type`` — single value or list (optional)

When no year filter is provided, no Fiscal Year filter is applied.
When ``opportunity_rec_type`` is a list, an ``OR`` (IN) match is used.
"""

from __future__ import annotations

import asyncio
import logging
from email.utils import parseaddr
from urllib.parse import parse_qs

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)

from app.config import get_settings
from app.dependencies import (
    get_airtable_service,
    get_current_user,
    get_gemini_service,
)
from app.helpers.slack import (
    post_to_response_url,
    verify_slack_signature,
)
from app.models.airtable import (
    AirtableRecord,
    AirtableUserIdResponse,
    ActiveProgramItem,
    AmountSumResponse,
    AnnouncementCreate,
    AnnouncementRecord,
    AnnouncementUpdate,
    AccessControlAssign,
    AccessControlRecord,
    AccessControlRevoke,
    CheckinReportingPeriodRecord,
    ClusterRecord,
    CountResponse,
    DateRangeFilter,
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
    OppRecTypeAmountResponse,
    Permission,
    Role,
    RoleUpdate,
    RoleCreate,
    ShareableDocsRecord,
    SlackTicketWebhookPayload,
    EmailTicketWebhookPayload,
    TicketCreate,
    TicketRecord,
    TicketUpdate,
    UniqueAccountsResponse,
    UserRecord,
    UserUpdate,
    YearlyAmountResponse,
    MeetingCadenceRecord,
    UsefulLinkRecord,
    HrAndBenefitsRecord,
    OnboardingLinkRecord,
    OnboardingCallRecord,
    OnboardingChecklistRecord,
    QuickLinkRecord,
    QuickLinkCreate,
    QuickLinkUpdate,
    RecordFieldsUpdate,
)
from app.models.auth import UserInfo
from app.services.airtable_service import AirtableService
from app.services.gemini_service import GeminiService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["Data"])


@router.get(
    "/get_total_amount_sum",
    response_model=AmountSumResponse,
    summary="Sum of Amount filtered by Opportunity Record Type and Fiscal Year",
)
async def get_total_amount_sum(
    opportunity_rec_type: list[str] | None = Query(
        default=None,
        description=(
            "Opportunity Record Type filter. Pass once for an equality match "
            "or repeat the parameter for an OR / IN match. Optional."
        ),
    ),
    eq_year: int | None = Query(default=None, description="Fiscal Year equals."),
    lt_year: int | None = Query(default=None, description="Fiscal Year strictly less than."),
    gt_year: int | None = Query(default=None, description="Fiscal Year strictly greater than."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_total_amount_sum(
        opportunity_rec_type=opportunity_rec_type,
        eq_year=eq_year,
        lt_year=lt_year,
        gt_year=gt_year,
    )


@router.get(
    "/get_nb_unique_accounts",
    response_model=UniqueAccountsResponse,
    summary="Number of unique Account Name values matching the filters",
)
async def get_nb_unique_accounts(
    opportunity_rec_type: list[str] | None = Query(
        default=None,
        description=(
            "Opportunity Record Type filter. Pass once for an equality match "
            "or repeat the parameter for an OR / IN match. Optional."
        ),
    ),
    eq_year: int | None = Query(default=None, description="Fiscal Year equals."),
    lt_year: int | None = Query(default=None, description="Fiscal Year strictly less than."),
    gt_year: int | None = Query(default=None, description="Fiscal Year strictly greater than."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_nb_unique_accounts(
        opportunity_rec_type=opportunity_rec_type,
        eq_year=eq_year,
        lt_year=lt_year,
        gt_year=gt_year,
    )


@router.get(
    "/get_opportunity_rec_type_distribution",
    response_model=DistributionResponse,
    summary="Percentage distribution of Opportunity Record Type values",
)
async def get_opportunity_rec_type_distribution(
    eq_year: int | None = Query(default=None, description="Fiscal Year equals."),
    lt_year: int | None = Query(default=None, description="Fiscal Year strictly less than."),
    gt_year: int | None = Query(default=None, description="Fiscal Year strictly greater than."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_opportunity_rec_type_distribution(
        eq_year=eq_year, lt_year=lt_year, gt_year=gt_year
    )


@router.get(
    "/get_sum_amount_over_years",
    response_model=YearlyAmountResponse,
    summary="Sum of Amount per Fiscal Year for the given Opportunity Record Type(s)",
)
async def get_sum_amount_over_years(
    opportunity_rec_type: list[str] | None = Query(
        default=None,
        description=(
            "Opportunity Record Type filter. Pass once for an equality match "
            "or repeat the parameter for an OR / IN match. Optional."
        ),
    ),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_sum_amount_over_years(
        opportunity_rec_type=opportunity_rec_type,
    )


@router.get(
    "/get_sum_amount_by_opp_rec_type",
    response_model=OppRecTypeAmountResponse,
    summary="Sum of Amount per Opportunity Record Type within a Fiscal Year range",
)
async def get_sum_amount_by_opp_rec_type(
    eq_year: int | None = Query(default=None, description="Fiscal Year equals."),
    lt_year: int | None = Query(default=None, description="Fiscal Year strictly less than."),
    gt_year: int | None = Query(default=None, description="Fiscal Year strictly greater than."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_sum_amount_by_opp_rec_type(
        eq_year=eq_year, lt_year=lt_year, gt_year=gt_year
    )


# ══════════════════════════════════════════════════════════════════════
#   Fund & Program Tracker endpoints
# ══════════════════════════════════════════════════════════════════════

_FIELDS_DESC = (
    "Optional list of field names to project. If omitted, all fields are "
    "returned. Repeat the query parameter to pass multiple values."
)


# ── #1 /get_funds_and_subprograms ─────────────────────────────────────
@router.get(
    "/get_funds_and_subprograms",
    response_model=list[MasterListFundsAndSubprogramsRecord],
    summary="List funds & subprograms with rich filtering",
)
async def get_funds_and_subprograms(
    exclude_from_lists: bool | None = Query(default=None),
    exclude_from_reporting: bool | None = Query(default=None),
    status_list: list[str] | None = Query(default=None),
    status_empty: bool | None = Query(default=None),
    not_status_list: list[str] | None = Query(
        default=None,
        description="Exclude records whose Status is in this list.",
    ),
    sub_track_of: list[str] | None = Query(
        default=None,
        description="List of record IDs the Sub-Track Of must reference.",
    ),
    sub_track_empty: bool | None = Query(default=None),
    share_publicly: bool | None = Query(default=None),
    vetting: bool | None = Query(default=None),
    add_to_shareable_doc: bool | None = Query(default=None),
    restricted_names: list[str] | None = Query(default=None),
    scoping_prop_overview_empty: bool | None = Query(default=None),
    initiative_types: list[str] | None = Query(default=None),
    focus_areas: list[str] | None = Query(default=None),
    onboarding_empty: bool | None = Query(
        default=None,
        description=(
            "Filter on the 'Onboarding status' field: "
            "true → empty, false → not empty, omitted → no filter."
        ),
    ),
    vetting_status_list: list[str] | None = Query(
        default=None,
        description="Keep records whose 'Vetting Status' is in this list.",
    ),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_funds_and_subprograms(
        exclude_from_lists=exclude_from_lists,
        exclude_from_reporting=exclude_from_reporting,
        status_list=status_list,
        not_status_list=not_status_list,
        status_empty=status_empty,
        sub_track_of=sub_track_of,
        sub_track_empty=sub_track_empty,
        share_publicly=share_publicly,
        vetting=vetting,
        add_to_shareable_doc=add_to_shareable_doc,
        restricted_names=restricted_names,
        scoping_prop_overview_empty=scoping_prop_overview_empty,
        initiative_types=initiative_types,
        focus_areas=focus_areas,
        onboarding_empty=onboarding_empty,
        vetting_status_list=vetting_status_list,
        fields=fields,
    )


# ── #2 /get_glossary_data ─────────────────────────────────────────────
@router.get(
    "/get_glossary_data",
    response_model=list[GlossaryRecord],
    summary="List glossary entries",
)
async def get_glossary_data(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_glossary_data(fields=fields)


# ── #3 /get_org_friends ───────────────────────────────────────────────
@router.get(
    "/get_org_friends",
    response_model=list[OrgFriendsRecord],
    summary="List org friends",
)
async def get_org_friends(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_org_friends(fields=fields)


# ── #4 /get_funders ───────────────────────────────────────────────────
@router.get(
    "/get_funders",
    response_model=list[FundersRecord],
    summary="List funders",
)
async def get_funders(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_funders(fields=fields)


# ── #5 /get_funds_progs_monthly_checkin ───────────────────────────────
@router.get(
    "/get_funds_progs_monthly_checkin",
    response_model=list[MonthlyCheckinRecord],
    summary="Funds & programs monthly check-in records",
)
async def get_funds_progs_monthly_checkin(
    eq_days_until_deadline: int | None = Query(default=None),
    lt_days_until_deadline: int | None = Query(default=None),
    gt_days_until_deadline: int | None = Query(default=None),
    submission_extension: bool | None = Query(default=None),
    user_id: str | None = Query(
        default=None,
        description="Reporting Lead Airtable user id.",
    ),
    checkin_user_id: str | None = Query(
        default=None,
        description=(
            "Filter by Airtable user id present in the linked program's "
            "Check-In History collaborator field."
        ),
    ),
    not_program_status: list[str] | None = Query(
        default=None,
        description="Exclude records whose linked program Status is in this list.",
    ),
    report_complete: bool | None = Query(default=None),
    flag_for_discussion: bool | None = Query(default=None),
    followup_indicated_not_empty: bool | None = Query(
        default=None,
        description=(
            "Filter on 'Followup Indicated': true → not empty, "
            "false → unchecked, omitted → no filter."
        ),
    ),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_funds_progs_monthly_checkin(
        eq_days_until_deadline=eq_days_until_deadline,
        lt_days_until_deadline=lt_days_until_deadline,
        gt_days_until_deadline=gt_days_until_deadline,
        submission_extension=submission_extension,
        user_id=user_id,
        checkin_user_id=checkin_user_id,
        not_program_status=not_program_status,
        report_complete=report_complete,
        flag_for_discussion=flag_for_discussion,
        followup_indicated_not_empty=followup_indicated_not_empty,
        fields=fields,
    )


# ── #6 /get_funds_progs_monthly_checkin_count ─────────────────────────
@router.get(
    "/get_funds_progs_monthly_checkin_count",
    response_model=CountResponse,
    summary="Count of monthly check-in records matching filters",
)
async def get_funds_progs_monthly_checkin_count(
    flag_for_discussion: bool | None = Query(default=None),
    checkin_in_reporting_periods: list[str] | None = Query(default=None),
    clusters: list[str] | None = Query(default=None),
    program_names: list[str] | None = Query(default=None),
    status_list: list[str] | None = Query(default=None),
    user_ids: list[str] | None = Query(default=None),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_funds_progs_monthly_checkin_count(
        flag_for_discussion=flag_for_discussion,
        checkin_in_reporting_periods=checkin_in_reporting_periods,
        clusters=clusters,
        program_names=program_names,
        status_list=status_list,
        user_ids=user_ids,
    )


# ── #7 /get_funds_progs_status_distribution ───────────────────────────
@router.get(
    "/get_funds_progs_status_distribution",
    response_model=DistributionResponse,
    summary="Distribution of Dashboard Display values for monthly check-ins",
)
async def get_funds_progs_status_distribution(
    checkin_in_reporting_period: str | None = Query(default=None),
    cluster: str | None = Query(default=None),
    program_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_funds_progs_status_distribution(
        checkin_in_reporting_period=checkin_in_reporting_period,
        cluster=cluster,
        program_name=program_name,
        status=status,
        user_id=user_id,
    )


# ── #8 /get_reports_with_followups ────────────────────────────────────
@router.get(
    "/get_reports_with_followups",
    response_model=list[MonthlyCheckinRecord],
    summary="Reports with optional follow-up filters",
)
async def get_reports_with_followups(
    follow_indicated_empty: bool | None = Query(default=None),
    report_complete: bool | None = Query(default=None),
    flag_for_discussion: bool | None = Query(default=None),
    checkin_in_reporting_period: str | None = Query(default=None),
    cluster: str | None = Query(default=None),
    program_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_reports_with_followups(
        follow_indicated_empty=follow_indicated_empty,
        report_complete=report_complete,
        flag_for_discussion=flag_for_discussion,
        checkin_in_reporting_period=checkin_in_reporting_period,
        cluster=cluster,
        program_name=program_name,
        status=status,
        user_id=user_id,
        fields=fields,
    )


# ── #9 /get_checkin_reporting_periods ─────────────────────────────────
@router.post(
    "/get_checkin_reporting_periods",
    response_model=list[AirtableRecord],
    summary="Check-in reporting periods filtered by Deadline (OR of OR groups)",
)
async def get_checkin_reporting_periods(
    date_filters: list[DateRangeFilter] = Body(
        default_factory=list,
        description=(
            "List of date-filter objects. Each object may set any subset "
            "of eq_date/lt_date/gt_date (ISO YYYY-MM-DD); predicates "
            "inside an object and across objects are all OR-combined."
        ),
    ),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_checkin_reporting_periods(
        date_filters=date_filters, fields=fields
    )


# ── #10 /get_recent_complete_reports ──────────────────────────────────
@router.get(
    "/get_recent_complete_reports",
    response_model=list[MonthlyCheckinRecord],
    summary="Recent complete reports filtered by review window or days-until-deadline",
)
async def get_recent_complete_reports(
    report_complete: bool | None = Query(default=None),
    eq_days_until_deadline: int | None = Query(default=None),
    lt_days_until_deadline: int | None = Query(default=None),
    gt_days_until_deadline: int | None = Query(default=None),
    eq_review_until: str | None = Query(default=None),
    lt_review_until: str | None = Query(default=None),
    gt_review_until: str | None = Query(default=None),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_recent_complete_reports(
        report_complete=report_complete,
        eq_days_until_deadline=eq_days_until_deadline,
        lt_days_until_deadline=lt_days_until_deadline,
        gt_days_until_deadline=gt_days_until_deadline,
        eq_review_until=eq_review_until,
        lt_review_until=lt_review_until,
        gt_review_until=gt_review_until,
        fields=fields,
    )


# ── #11 /get_archived_reports_by_program ──────────────────────────────
@router.get(
    "/get_archived_reports_by_program",
    response_model=list[MonthlyCheckinRecord],
    summary="Reports filtered by Report Complete and program Status exclusion",
)
async def get_archived_reports_by_program(
    report_complete: bool | None = Query(default=None),
    not_program_status: str | None = Query(default=None),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_archived_reports_by_program(
        report_complete=report_complete,
        not_program_status=not_program_status,
        fields=fields,
    )


# ── #12 /get_doc_titles ───────────────────────────────────────────────
@router.get(
    "/get_doc_titles",
    response_model=list[DocTitleRecord],
    summary="List doc titles",
)
async def get_doc_titles(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_doc_titles(fields=fields)


# ── #20 /get_shareable_docs ───────────────────────────────────────────
@router.get(
    "/get_shareable_docs",
    response_model=list[ShareableDocsRecord],
    summary="List shareable docs",
)
async def get_shareable_docs(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_shareable_docs(fields=fields)


# ── /get_onboarding_checklist ─────────────────────────────────────────
@router.get(
    "/get_onboarding_checklist",
    response_model=list[OnboardingChecklistRecord],
    summary="List onboarding checklist rows with linked Master List expanded",
)
async def get_onboarding_checklist(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_onboarding_checklist(fields=fields)

# ── #14 /get_unique_checkin_reporting_periods ─────────────────────────
@router.get(
    "/get_unique_checkin_reporting_periods",
    response_model=list[CheckinReportingPeriodRecord],
    summary="Unique Check-In Reporting Period values (record_id + Period)",
)
async def get_unique_checkin_reporting_periods(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_unique_checkin_reporting_periods()


# ── #15 /get_clusters ─────────────────────────────────────────────────
@router.get(
    "/get_clusters",
    response_model=list[ClusterRecord],
    summary="Unique Cluster values (record_id + Name)",
)
async def get_clusters(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_clusters()


# ── #16 /get_program_names ────────────────────────────────────────────
@router.get(
    "/get_program_names",
    response_model=list[IdNameItem],
    summary="Unique Program Name values (id + Name)",
)
async def get_program_names(
    add_to_sharable_doc: bool | None = Query(
        default=None,
        description=(
            "Filter by the 'Add to New Shareable Doc' checkbox on the "
            "linked program: true → ticked only, false → unticked only, "
            "omitted → no filter."
        ),
    ),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_program_names(
        add_to_sharable_doc=add_to_sharable_doc,
    )


# ── #17 /get_status_values ────────────────────────────────────────────
@router.get(
    "/get_status_values",
    response_model=list[str],
    summary="Unique Status values from monthly check-in",
)
async def get_status_values(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_status_values()


# ── #18 /get_reporting_leads ──────────────────────────────────────────
@router.get(
    "/get_reporting_leads",
    summary="Unique Reporting Lead users (id, email, name)",
)
async def get_reporting_leads(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_reporting_leads()


# ── #19 /get_airtable_user_id ─────────────────────────────────────────
@router.get(
    "/get_airtable_user_id",
    response_model=AirtableUserIdResponse,
    summary="Resolve an Airtable user id from an email",
)
async def get_airtable_user_id(
    email: str = Query(..., description="Email of the user to resolve."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_airtable_user_id(email)


# ── #20 /get_active_programs_count ────────────────────────────────────
@router.get(
    "/get_active_programs_count",
    response_model=CountResponse,
    summary=(
        "Number of records in the Master List whose Status is "
        "'3. Active Program' or '4. Publicly Launched'"
    ),
)
async def get_active_programs_count(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_active_programs_count()


# ── /get_active_programs ──────────────────────────────────────────────
@router.get(
    "/get_active_programs",
    response_model=list[ActiveProgramItem],
    summary=(
        "List active programs (Name + Program Lead/Fellow) from the "
        "Master List filtered to Status '3. Active Program' or "
        "'4. Publicly Launched'"
    ),
)
async def get_active_programs(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_active_programs()


# ── #21 /get_distinct_fellows_count ───────────────────────────────────
@router.get(
    "/get_distinct_fellows_count",
    response_model=CountResponse,
    summary=(
        "Number of fellows: distinct Work Email values in the Users table "
        "where Employment Type includes 'Fellow (Unpaid)' OR For Website == 'Fellow'"
    ),
)
async def get_distinct_fellows_count(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_distinct_fellows_count()


# ── /get_distinct_fellows ─────────────────────────────────────────────
@router.get(
    "/get_distinct_fellows",
    response_model=list[PersonContactItem],
    summary=(
        "Unique fellows (First Name, Last Name, Work Email) from the Users "
        "table where Employment Type includes 'Fellow (Unpaid)' OR "
        "For Website == 'Fellow'"
    ),
)
async def get_distinct_fellows(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_distinct_fellows()


# ── #22 /get_team_size ───────────────────────────────────────
@router.get(
    "/get_team_size",
    response_model=CountResponse,
    summary="Number of distinct 'Name' values in the Users table where Status == 'Active'",
)
async def get_team_size(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_team_size()


# ── /get_team_members ─────────────────────────────────────────────────
@router.get(
    "/get_team_members",
    response_model=list[PersonContactItem],
    summary=(
        "Team members (First Name, Last Name, Work Email) from the Users "
        "table where Status == 'Active'"
    ),
)
async def get_team_members(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_team_members()


# ── /get_partnerships_fundraising ─────────────────────────────────────
@router.get(
    "/get_partnerships_fundraising",
    response_model=list[PartnershipsFundraisingRecord],
    summary=(
        "List rows from the Partnerships Fundraising table "
        "(Document, Document URL, Notes). Document URL may be empty."
    ),
)
async def get_partnerships_fundraising(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_partnerships_fundraising(fields=fields)


# ── PATCH /partnerships_fundraising/{id} ──────────────────────────────
@router.patch(
    "/partnerships_fundraising/{pf_id}",
    response_model=PartnershipsFundraisingRecord,
    summary=(
        "Update a Partnerships Fundraising record by its 'Id' "
        "(admin only). Any subset of fields may be provided."
    ),
)
async def update_partnerships_fundraising(
    pf_id: int,
    payload: PartnershipsFundraisingUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to edit Partnerships Fundraising records.",
        )
    return await airtable_service.update_partnerships_fundraising(pf_id, payload)


# ── /get_finance_links ────────────────────────────────────────────────
@router.get(
    "/get_finance_links",
    response_model=list[FinanceLinkRecord],
    summary="List rows from the Finance Links table (Id, Document, Document URL).",
)
async def get_finance_links(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_finance_links(fields=fields)


# ── PATCH /finance_links ──────────────────────────────────────────────────────
@router.patch(
    "/finance_links",
    response_model=FinanceLinkRecord,
    summary=(
        "Update a Finance Links record identified by its 'Document URL' "
        "(admin only). Any subset of fields may be provided."
    ),
)
async def update_finance_link(
    document_url: str = Query(
        ...,
        description="The current 'Document URL' of the record to update.",
    ),
    payload: FinanceLinkUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to edit Finance Links records.",
        )
    return await airtable_service.update_finance_link_by_url(document_url, payload)


# ══════════════════════════════════════════════════════════════════════
#   Office Spaces endpoints
# ══════════════════════════════════════════════════════════════════════
@router.get(
    "/office_spaces",
    response_model=list[OfficeSpaceRecord],
    summary="List rows from the Office Spaces table (Id, Branch, Address, Details).",
)
async def get_office_spaces(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_office_spaces(fields=fields)


@router.post(
    "/office_spaces",
    response_model=OfficeSpaceRecord,
    status_code=status.HTTP_201_CREATED,
    summary=(
        "Create a new Office Spaces record (admin only). "
        "'branch' and 'address' are required; 'details' is optional."
    ),
)
async def create_office_space(
    payload: OfficeSpaceCreate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to create Office Spaces records.",
        )
    return await airtable_service.create_office_space(payload)


@router.patch(
    "/office_spaces",
    response_model=OfficeSpaceRecord,
    summary=(
        "Update an Office Spaces record identified by its 'Branch' value "
        "(admin only). Any subset of fields may be provided."
    ),
)
async def update_office_space(
    branch: str = Query(
        ...,
        description="The current 'Branch' value of the record to update.",
    ),
    payload: OfficeSpaceUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to edit Office Spaces records.",
        )
    return await airtable_service.update_office_space_by_branch(branch, payload)


# ── /get_google_docs_tabs ──────────────────────────────────────────────
@router.get(
    "/get_google_docs_tabs",
    response_model=list[GoogleDocsTabRecord],
    summary=(
        "List rows from the Google Docs Tabs table "
        "(Id, UI Page, Document Id, Tab Id). "
        "Optionally filter by 'UI Page'."
    ),
)
async def get_google_docs_tabs(
    ui_page: str | None = Query(
        default=None,
        description="If provided, return only records whose 'UI Page' equals this value.",
    ),
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_google_docs_tabs(
        ui_page=ui_page, fields=fields
    )


# ══════════════════════════════════════════════════════════════════════
#   Meeting Cadence / Useful Links / HR & Benefits / Onboarding endpoints
# ══════════════════════════════════════════════════════════════════════
@router.get(
    "/meeting_cadence",
    response_model=list[MeetingCadenceRecord],
    summary=(
        "List rows from the Meeting Cadence table "
        "(Meeting Title, Description, Attachment URL)."
    ),
)
async def get_meeting_cadence(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_meeting_cadence(fields=fields)


@router.get(
    "/useful_links",
    response_model=list[UsefulLinkRecord],
    summary=(
        "List rows from the Useful Links table "
        "(Document, Document URL, Description)."
    ),
)
async def get_useful_links(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_useful_links(fields=fields)


@router.get(
    "/hr_and_benefits",
    response_model=list[HrAndBenefitsRecord],
    summary=(
        "List rows from the HR & Benefits table "
        "(Document, Document URL, Description)."
    ),
)
async def get_hr_and_benefits(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_hr_and_benefits(fields=fields)


@router.get(
    "/onboarding",
    response_model=list[OnboardingLinkRecord],
    summary=(
        "List rows from the Onboarding table "
        "(Document, Document URL, Notes)."
    ),
)
async def get_onboarding_links(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_onboarding_links(fields=fields)


@router.get(
    "/onboarding_calls",
    response_model=list[OnboardingCallRecord],
    summary="List rows from the Onboarding Calls table (Date, Notes).",
)
async def get_onboarding_calls(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_onboarding_calls(fields=fields)


# ══════════════════════════════════════════════════════════════════════
#   Quick Links endpoints
# ══════════════════════════════════════════════════════════════════════
@router.get(
    "/quick_links",
    response_model=list[QuickLinkRecord],
    summary="List rows from the Quick Links table.",
)
async def get_quick_links(
    fields: list[str] | None = Query(default=None, description=_FIELDS_DESC),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_quick_links(fields=fields)


@router.post(
    "/quick_links",
    response_model=QuickLinkRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new Quick Link (admin only)",
)
async def create_quick_link(
    payload: QuickLinkCreate,
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to create a quick link.",
        )
    return await airtable_service.create_quick_link(payload)


@router.patch(
    "/quick_links/{quick_link_id}",
    response_model=QuickLinkRecord,
    summary="Update a Quick Link by its Id (admin only)",
)
async def update_quick_link(
    payload: QuickLinkUpdate,
    quick_link_id: int = Path(..., description="Value of the 'Id' (Autonumber) field."),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to update a quick link.",
        )
    return await airtable_service.update_quick_link(quick_link_id, payload)


@router.delete(
    "/quick_links/{quick_link_id}",
    summary="Delete a Quick Link by its Id (admin only)",
)
async def delete_quick_link(
    quick_link_id: int = Path(..., description="Value of the 'Id' (Autonumber) field."),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to delete a quick link.",
        )
    return await airtable_service.delete_quick_link(quick_link_id)


# ══════════════════════════════════════════════════════════════════════
#   Announcements endpoints
# ══════════════════════════════════════════════════════════════════════
@router.post(
    "/announcements",
    response_model=AnnouncementRecord,
    summary="Create a new announcement (Status defaults to 'Drafted')",
)
async def create_announcement(
    payload: AnnouncementCreate = Body(...),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.create_announcement(payload)


@router.patch(
    "/announcements/{announcement_id}",
    response_model=AnnouncementRecord,
    summary="Update one or more fields on an announcement by its Announcement Id",
)
async def update_announcement(
    announcement_id: int = Path(..., description="Value of the 'Announcement Id' (Autonumber) field."),
    payload: AnnouncementUpdate = Body(...),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.update_announcement(announcement_id, payload)


@router.get(
    "/announcements",
    response_model=list[AnnouncementRecord],
    summary="List announcements (published only by default; admins may list all)",
)
async def list_announcements(
    all: bool = Query(
        default=False,
        description=(
            "If true, return ALL announcements (any Status). Requires the "
            "authenticated user to be an admin. If false (default), only "
            "announcements with Status='Published' are returned."
        ),
    ),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if all:
        if not await airtable_service.is_hub_admin(user.email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required to list all announcements.",
            )
        return await airtable_service.list_announcements(published_only=False)
    return await airtable_service.list_announcements(published_only=True)


@router.get(
    "/announcements/get_categories",
    response_model=list[str],
    summary="Unique Category values used across announcements",
)
async def get_announcement_categories(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_announcement_categories()


@router.get(
    "/announcements/by-author/{author_email}",
    response_model=list[AnnouncementRecord],
    summary="List announcements authored by a specific email",
)
async def list_announcements_by_author(
    author_email: str = Path(..., description="Author email to filter by."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.list_announcements_by_author(author_email)


@router.delete(
    "/announcements/{announcement_id}",
    summary="Delete an announcement by its Id (admin only)",
)
async def delete_announcement(
    announcement_id: int = Path(..., description="Value of the 'Id' (Autonumber) field."),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to delete an announcement.",
        )
    return await airtable_service.delete_announcement(announcement_id)


# ══════════════════════════════════════════════════════════════════════
#   Access Control endpoints
# ══════════════════════════════════════════════════════════════════════
_ADMIN_REQUIRED_DETAIL = "Admin privileges required to manage access control."


async def _require_admin(
    user: UserInfo, airtable_service: AirtableService
) -> None:
    if not await airtable_service.is_hub_admin(user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_ADMIN_REQUIRED_DETAIL,
        )


@router.get(
    "/access-control",
    response_model=list[AccessControlRecord],
    summary="List all Access Control records (admin only)",
)
async def list_access_control(
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service.list_access_control_records()


@router.post(
    "/access-control/assign",
    response_model=AccessControlRecord,
    summary="Upsert role(s) and/or permission(s) for a user email (admin only)",
)
async def assign_access_control(
    payload: AccessControlAssign = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service.upsert_access_control(payload)


@router.post(
    "/access-control/revoke",
    response_model=AccessControlRecord,
    summary="Remove role(s) and/or permission(s) for a user email (admin only)",
)
async def revoke_access_control(
    payload: AccessControlRevoke = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service.revoke_access_control(payload)


@router.get(
    "/access-control/team-emails",
    response_model=list[str],
    summary="Unique Work Email values from the Teams table",
)
async def get_team_emails(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_unique_team_emails()


@router.get(
    "/access-control/roles",
    response_model=list[Role],
    summary="Roles from the Roles table with their resolved Permissions",
)
async def get_roles(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_unique_roles()


@router.get(
    "/access-control/permissions",
    response_model=list[Permission],
    summary="Permissions from the Permissions table (id + Permission Name + Description)",
)
async def get_permissions(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_unique_permissions()


@router.post(
    "/access-control/roles",
    response_model=Role,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new Role (admin only)",
)
async def create_role(
    payload: RoleCreate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service.create_role(payload)


@router.patch(
    "/access-control/roles/{role_id}",
    response_model=Role,
    summary="Update a Role: name, scope, and/or permissions (admin only)",
)
async def update_role(
    role_id: str = Path(..., description="Role record id."),
    payload: RoleUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service.update_role(role_id, payload)


@router.delete(
    "/access-control/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a Role (admin only)",
)
async def delete_role(
    role_id: str = Path(..., description="Role record id."),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    await airtable_service.delete_role(role_id)
    return None


# ═══════════════════════════════════════════════════════════════════
#   Admin record updates for typed data tables
# ═══════════════════════════════════════════════════════════════════
#
# Each endpoint takes:
#   * Path  : the Airtable record id (e.g. "rec...")
#   * Body  : { "fields": { "<Airtable Field Name>": value, ... } }
#
# Keys in ``fields`` are the exact Airtable column names (which match the
# alias of the corresponding attribute on the typed record model).
# Values follow Airtable's own JSON shape (string, number, bool, list of
# strings for multi-select / linked-record fields, etc.). ``typecast``
# is enabled server-side so single/multi-select values can be sent as
# plain strings.
#
# Returns the refreshed record using the same model as the matching GET.

@router.patch(
    "/funds_and_subprograms/{record_id}",
    response_model=MasterListFundsAndSubprogramsRecord,
    summary="Update a Master List fund/subprogram record (admin only)",
)
async def update_funds_and_subprograms_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._master_list_table(),
        record_id,
        payload.fields,
        MasterListFundsAndSubprogramsRecord,
    )


@router.patch(
    "/glossary/{record_id}",
    response_model=GlossaryRecord,
    summary="Update a Glossary record (admin only)",
)
async def update_glossary_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._glossary_table(),
        record_id,
        payload.fields,
        GlossaryRecord,
    )


@router.patch(
    "/org_friends/{record_id}",
    response_model=OrgFriendsRecord,
    summary="Update an Org Friends record (admin only)",
)
async def update_org_friends_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._org_friends_table(),
        record_id,
        payload.fields,
        OrgFriendsRecord,
    )


@router.patch(
    "/funders/{record_id}",
    response_model=FundersRecord,
    summary="Update a Funders record (admin only)",
)
async def update_funders_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._funders_table(),
        record_id,
        payload.fields,
        FundersRecord,
    )


@router.patch(
    "/monthly_checkin/{record_id}",
    response_model=MonthlyCheckinRecord,
    summary="Update a Funds & Programs Monthly Check-In record (admin only)",
)
async def update_monthly_checkin_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._monthly_checkin_table(),
        record_id,
        payload.fields,
        MonthlyCheckinRecord,
    )


@router.patch(
    "/checkin_reporting_periods/{record_id}",
    response_model=CheckinReportingPeriodRecord,
    summary="Update a Check-In Reporting Period record (admin only)",
)
async def update_checkin_reporting_period_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._checkin_periods_table(),
        record_id,
        payload.fields,
        CheckinReportingPeriodRecord,
        id_key="record_id",
    )


@router.patch(
    "/shareable_docs/{record_id}",
    response_model=ShareableDocsRecord,
    summary="Update a Shareable Docs record (admin only)",
)
async def update_shareable_docs_record(
    record_id: str = Path(..., description="Airtable record id."),
    payload: RecordFieldsUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    await _require_admin(user, airtable_service)
    return await airtable_service._update_typed_record(
        airtable_service._shareable_docs_table(),
        record_id,
        payload.fields,
        ShareableDocsRecord,
    )


# ═══════════════════════════════════════════════════════════════════
#   Tickets endpoints
# ═══════════════════════════════════════════════════════════════════
@router.get(
    "/tickets",
    response_model=list[TicketRecord],
    summary="List all tickets",
)
async def list_tickets(
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.list_tickets()


@router.get(
    "/tickets/by-assignee/{assignee_email}",
    response_model=list[TicketRecord],
    summary="List tickets assigned to a specific assignee email",
)
async def list_tickets_by_assignee(
    assignee_email: str = Path(..., description="Assignee email to filter by."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.list_tickets_by_assignee(assignee_email)


@router.post(
    "/tickets",
    response_model=TicketRecord,
    summary="Create a new ticket",
)
async def create_ticket(
    payload: TicketCreate = Body(...),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.create_ticket(payload)


# @router.post(
#     "/tickets/webhook/slack",
#     response_model=TicketRecord,
#     summary=(
#         "Webhook: create a ticket from Slack. Source is forced to 'Slack' "
#         "and Created Date is set server-side; Status is left empty so "
#         "Airtable applies its default ('Open')."
#     ),
# )
# async def slack_ticket_webhook(
#     payload: SlackTicketWebhookPayload = Body(...),
#     x_webhook_secret: str | None = Header(
#         default=None,
#         alias="X-Webhook-Secret",
#         description=(
#             "Shared secret. Required when SLACK_WEBHOOK_SECRET is "
#             "configured on the server."
#         ),
#     ),
#     airtable_service: AirtableService = Depends(get_airtable_service),
# ):
#     expected = get_settings().SLACK_WEBHOOK_SECRET
#     if expected:
#         if not x_webhook_secret or x_webhook_secret != expected:
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Invalid or missing webhook secret.",
#             )
#     return await airtable_service.create_ticket_from_slack(payload)


# ── Slack ``/tickets`` slash-command webhook ───────────────────────────


async def _llm_extract_ticket_fields(
    text: str, gemini_service: GeminiService
) -> dict[str, str | None]:
    """Run Gemini against the ticket-parsing prompt.

    Returns a dict shaped like:
        {
          "title": str | None,
          "description": str | None,
          "assignee":  str | None,    # mapped from prompt's ``assignee_email``
          "due_date":  str | None,    # ISO YYYY-MM-DD when present
        }
    """
    try:
        parsed = await gemini_service.extract_ticket_fields(text)
    except HTTPException:
        # Re-raise so the background task surfaces a useful error to the
        # Slack user via the failure branch in ``_process_slack_ticket``.
        raise
    except Exception:
        logger.exception("Gemini ticket extraction failed unexpectedly.")
        raise

    return {
        "title": parsed.get("title"),
        "description": parsed.get("description"),
        "assignee": parsed.get("assignee_email"),
        "assignee_full_name": parsed.get("assignee_full_name"),
        "due_date": parsed.get("due_date"),
    }


def _resolve_assignee_email(
    extracted: dict[str, str | None], org_domain: str | None
) -> str | None:
    """Resolve the assignee email from the LLM extraction result.

    Preference order:
      1. ``assignee`` (from ``assignee_email`` in the prompt) used as-is.
      2. ``assignee_full_name`` converted to ``first.last.…@<ORG_DOMAIN>``
         (lower-cased, whitespace collapsed). Requires ``org_domain``.
      3. ``None`` if neither is available (or domain missing when needed).
    """
    email = (extracted.get("assignee") or "").strip()
    if email:
        return email

    full_name = (extracted.get("assignee_full_name") or "").strip()
    if not full_name or not org_domain:
        return None

    parts = [p for p in full_name.split() if p]
    if not parts:
        return None
    local = ".".join(parts).lower()
    return f"{local}@{org_domain}"


async def _process_slack_ticket(
    text: str,
    user_id: str,
    user_name: str,
    response_url: str,
    airtable_service: AirtableService,
    gemini_service: GeminiService,
) -> None:
    """Background task triggered by the Slack ``/tickets`` slash command.

    1. Build ``assigned_by`` from ``user_name`` + ``ORG_DOMAIN``.
    2. Run the LLM to extract ticket fields from ``text``.
    3. If both title AND description are missing, abort with a hint to the user.
    4. Otherwise, resolve the assignee email (from the LLM's ``assignee_email``
       or from the ``assignee_full_name`` joined with dots + ORG_DOMAIN) and
       create the ticket with whatever fields are available.
    """
    try:
        # ── Step 1: Build ``assigned_by`` from user_name + ORG_DOMAIN ────────
        settings = get_settings()
        if not settings.ORG_DOMAIN:
            logger.error(
                "Slack /tickets: ORG_DOMAIN is not configured; cannot build assigned_by email."
            )
            await post_to_response_url(
                response_url,
                "❌ Ticket creation is misconfigured (missing organization domain). "
                "Please contact an administrator.",
            )
            return

        if not user_name:
            logger.warning(
                "Slack /tickets: missing user_name in Slack payload (slack_user=%s).",
                user_id,
            )
            await post_to_response_url(
                response_url,
                "❌ Could not determine your Slack username. Please try again.",
            )
            return

        assigned_by_email = f"{user_name}@{settings.ORG_DOMAIN}"
        logger.info(
            "Slack /tickets: built assigned_by (slack_user=%s, user_name=%s, email=%s)",
            user_id,
            user_name,
            assigned_by_email,
        )

        # ── Step 2: LLM extraction ───────────────────────────────────
        logger.info(
            "Slack /tickets: starting LLM extraction (slack_user=%s, text_len=%d)",
            user_id,
            len(text or ""),
        )
        extracted = await _llm_extract_ticket_fields(text, gemini_service)
        logger.debug(
            "Slack /tickets: LLM extraction result (slack_user=%s): %s",
            user_id,
            {k: extracted.get(k) for k in (
                "title",
                "description",
                "assignee",
                "assignee_full_name",
                "due_date",
            )},
        )

        # ── Step 3: Require at least a title or a description ───────────
        title = extracted.get("title")
        description = extracted.get("description")
        if not title and not description:
            logger.warning(
                "Slack /tickets: LLM produced neither title nor description "
                "(slack_user=%s) — prompting retry.",
                user_id,
            )
            await post_to_response_url(
                response_url,
                (
                    "⚠️ I could not find a *title* or *description* for the ticket.\n\n"
                    "Please try again with at least one of them, for example:\n"
                    "```/tickets Fix login bug — users can't log in on mobile, "
                    "assign to John Doe, due 2026-06-01```"
                ),
            )
            return

        # ── Step 4: Resolve the assignee email ──────────────────────────
        assignee_email = _resolve_assignee_email(extracted, settings.ORG_DOMAIN)
        logger.info(
            "Slack /tickets: resolved assignee (slack_user=%s, assignee=%s)",
            user_id,
            assignee_email,
        )

        # ── Step 5: Create the ticket with the available fields ─────────
        logger.info(
            "Slack /tickets: creating ticket (slack_user=%s, assignee=%s, "
            "assigned_by=%s, has_title=%s, has_description=%s)",
            user_id,
            assignee_email,
            assigned_by_email,
            bool(title),
            bool(description),
        )
        ticket = await airtable_service.create_ticket_partial(
            source="Slack",
            assigned_by=assigned_by_email,
            title=title,
            description=description,
            assignee=assignee_email,
            due_date=extracted.get("due_date"),
        )
        logger.info(
            "Slack /tickets: ticket created (slack_user=%s, airtable_id=%s, ticket_id=%s)",
            user_id,
            getattr(ticket, "id", None),
            getattr(ticket, "ticket_id", None),
        )

        # ── Step 6: Confirm to the user ─────────────────────────────────
        if assignee_email:
            confirmation = f"✅ Ticket assigned to *{assignee_email}* successfully."
        else:
            confirmation = "✅ Ticket created successfully (no assignee detected)."
        await post_to_response_url(response_url, confirmation)

    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Slack /tickets: failed to process command (slack_user=%s)", user_id
        )
        await post_to_response_url(
            response_url,
            (
                "❌ Something went wrong while creating your ticket. "
                f"Please try again.\n_Error: {exc}_"
            ),
        )


@router.post(
    "/tickets/webhook/slack",
    summary="Slash-command webhook: receives the /tickets command from Slack.",
)
async def slack_ticket_webhook(
    request: Request,
    airtable_service: AirtableService = Depends(get_airtable_service),
    gemini_service: GeminiService = Depends(get_gemini_service),
):
    """Entry point for the Slack ``/tickets`` slash command.

    Verifies the Slack signature, enforces the allowed-channel restriction,
    schedules a background task to run the LLM extraction and create the
    ticket, and immediately returns an ephemeral acknowledgement so Slack
    does not time out (the 3-second limit).
    """
    settings = get_settings()

    # ── Step 1: Read the raw body and verify the Slack signature ────────
    # We must read the raw body BEFORE parsing the form, because the
    # signature is computed over the exact bytes Slack sent. We then parse
    # the form ourselves from those bytes so the stream is only consumed once.
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not settings.SLACK_SIGNING_SECRET:
        logger.error(
            "Slack /tickets: SLACK_SIGNING_SECRET is not configured; rejecting request."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack signature.",
        )

    if not verify_slack_signature(
        signing_secret=settings.SLACK_SIGNING_SECRET,
        request_body=body,
        timestamp=timestamp,
        signature=signature,
    ):
        logger.warning("Slack /tickets: signature verification failed.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack signature.",
        )

    # ── Step 2: Parse the urlencoded form payload from the raw body ─────
    form = parse_qs(body.decode("utf-8"))

    def _field(name: str) -> str:
        values = form.get(name) or []
        return values[0] if values else ""

    text = _field("text")
    user_id = _field("user_id")
    user_name = _field("user_name")
    channel_id = _field("channel_id")
    response_url = _field("response_url")

    logger.info(
        "Slack /tickets webhook received (slack_user=%s, user_name=%s, channel_id=%s)",
        user_id,
        user_name,
        channel_id,
    )

    # ── Step 3: Enforce channel restriction ─────────────────────────────
    allowed_channel = settings.SLACK_TICKETS_ALLOWED_CHANNEL_ID
    if allowed_channel and channel_id != allowed_channel:
        # Silently drop the request: do NOT post anything back to Slack.
        # We only record it server-side for observability.
        logger.warning(
            "Slack /tickets: command invoked from disallowed channel "
            "(slack_user=%s, channel_id=%s, allowed_channel=%s) — ignored.",
            user_id,
            channel_id,
            allowed_channel,
        )
        return None

    # ── Step 4: Respond immediately to avoid the 3-second timeout ──────────
    logger.info(
        "Slack /tickets: scheduling background processing (slack_user=%s, channel_id=%s)",
        user_id,
        channel_id,
    )
    asyncio.create_task(
        _process_slack_ticket(
            text=text,
            user_id=user_id,
            user_name=user_name,
            response_url=response_url,
            airtable_service=airtable_service,
            gemini_service=gemini_service,
        )
    )

    return {
        "response_type": "ephemeral",
        "text": "⏳ Creating the ticket...",
    }


async def _process_email_ticket(
    assigned_by_raw: str,
    email_content: str,
    airtable_service: AirtableService,
    gemini_service: GeminiService,
) -> None:
    """Background task triggered by the email-based ticket-assignment webhook.

    1. Normalize ``assigned_by`` (strip "Name <email>" wrapper if present).
    2. Run the LLM to extract ticket fields from ``email_content``.
    3. If both ``title`` AND ``description`` are missing, drop the ticket
       (we need at least one of them). Otherwise create with whatever the
       LLM returned plus the resolved ``assigned_by``.
    """
    try:
        # ── Step 1: Normalize assigned_by ───────────────────────────────
        _, parsed_email = parseaddr(assigned_by_raw or "")
        assigned_by_email = (parsed_email or assigned_by_raw or "").strip()
        if not assigned_by_email:
            logger.warning(
                "Email /tickets: missing assigned_by in payload; aborting."
            )
            return
        logger.info(
            "Email /tickets: resolved assigned_by (raw=%r, email=%s)",
            assigned_by_raw,
            assigned_by_email,
        )

        # ── Step 2: LLM extraction ──────────────────────────────────────
        logger.info(
            "Email /tickets: starting LLM extraction (assigned_by=%s, content_len=%d)",
            assigned_by_email,
            len(email_content or ""),
        )
        extracted = await _llm_extract_ticket_fields(
            email_content or "", gemini_service
        )
        logger.debug(
            "Email /tickets: LLM extraction result (assigned_by=%s): %s",
            assigned_by_email,
            {k: extracted.get(k) for k in (
                "title",
                "description",
                "assignee",
                "assignee_full_name",
                "due_date",
            )},
        )

        # ── Step 3: Require at least a title or a description ───────────
        title = extracted.get("title")
        description = extracted.get("description")
        if not title and not description:
            logger.warning(
                "Email /tickets: LLM produced neither title nor description "
                "(assigned_by=%s) — skipping ticket creation.",
                assigned_by_email,
            )
            return

        # ── Step 4: Resolve the assignee email ──────────────────────────
        settings = get_settings()
        assignee_email = _resolve_assignee_email(extracted, settings.ORG_DOMAIN)
        logger.info(
            "Email /tickets: resolved assignee (assigned_by=%s, assignee=%s)",
            assigned_by_email,
            assignee_email,
        )

        # ── Step 5: Create the ticket ───────────────────────────────────
        logger.info(
            "Email /tickets: creating ticket (assigned_by=%s, assignee=%s, "
            "has_title=%s, has_description=%s)",
            assigned_by_email,
            assignee_email,
            bool(title),
            bool(description),
        )
        ticket = await airtable_service.create_ticket_from_email(
            assigned_by=assigned_by_email,
            title=title,
            description=description,
            assignee=assignee_email,
            due_date=extracted.get("due_date"),
        )
        logger.info(
            "Email /tickets: ticket created (assigned_by=%s, airtable_id=%s, ticket_id=%s)",
            assigned_by_email,
            getattr(ticket, "id", None),
            getattr(ticket, "ticket_id", None),
        )

    except Exception:  # noqa: BLE001
        logger.exception(
            "Email /tickets: failed to process email ticket (assigned_by=%r)",
            assigned_by_raw,
        )


@router.post(
    "/tickets/webhook/email",
    summary="Webhook: create a ticket from an email-based assignment.",
)
async def email_ticket_webhook(
    payload: EmailTicketWebhookPayload = Body(...),
    airtable_service: AirtableService = Depends(get_airtable_service),
    gemini_service: GeminiService = Depends(get_gemini_service),
):
    """Entry point for the email-based ticket-assignment integration.

    The caller (e.g. a Google Apps Script Gmail forwarder) posts the raw
    email payload here. The handler immediately schedules a background
    task to run the LLM extraction and create the ticket, and returns
    202 so the caller does not block on Airtable/LLM latency.

    ``payload.source`` is informational and intentionally unused.
    """
    logger.info(
        "Email /tickets webhook received (assigned_by=%r, received_by=%r, source=%r, content_len=%d)",
        payload.assigned_by,
        payload.received_by,
        payload.source,
        len(payload.email_content or ""),
    )

    asyncio.create_task(
        _process_email_ticket(
            assigned_by_raw=payload.assigned_by,
            email_content=payload.email_content,
            airtable_service=airtable_service,
            gemini_service=gemini_service,
        )
    )

    return {"status": "accepted"}


def _ticket_field_email(record: dict, field: str) -> str | None:
    value = (record.get("fields", {}) or {}).get(field)
    if not value:
        return None
    return str(value).strip().lower()


def _is_ticket_owner(record: dict, email: str) -> bool:
    """Return True if ``email`` matches the ticket's 'Assigned By' field."""
    if not email:
        return False
    owner = _ticket_field_email(record, "Assigned By")
    return owner is not None and owner == email.strip().lower()


def _is_ticket_assignee(record: dict, email: str) -> bool:
    """Return True if ``email`` matches the ticket's 'Assignee' field."""
    if not email:
        return False
    assignee = _ticket_field_email(record, "Assignee")
    return assignee is not None and assignee == email.strip().lower()


@router.patch(
    "/tickets/{ticket_id}",
    response_model=TicketRecord,
    summary=(
        "Update a ticket (admins or the original creator may edit any field; "
        "the assignee may edit only the Status)"
    ),
)
async def update_ticket(
    ticket_id: int = Path(..., description="Value of the 'Id' (Autonumber) field."),
    payload: TicketUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    existing = await airtable_service.get_ticket_by_id(ticket_id)
    is_admin = await airtable_service.is_hub_admin(user.email)
    is_owner = _is_ticket_owner(existing, user.email)
    is_assignee = _is_ticket_assignee(existing, user.email)

    if not (is_admin or is_owner or is_assignee):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only admins, the user who created the ticket (Assigned By), "
                "or the assignee may update it."
            ),
        )

    # Assignee-only callers (not admin and not the creator) may edit
    # only 'status' and/or 'comments'.
    if not (is_admin or is_owner):
        provided = set(payload.model_fields_set)
        assignee_editable = {"status", "comments"}
        if provided - assignee_editable:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "The assignee may only update the 'status' and/or "
                    "'comments' fields."
                ),
            )
        if not (provided & assignee_editable):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "The assignee must provide at least one of 'status' "
                    "or 'comments' to update."
                ),
            )

    return await airtable_service.update_ticket(
        ticket_id,
        payload,
        updated_by_email=user.email,
        existing=existing,
    )


@router.delete(
    "/tickets/{ticket_id}",
    summary="Delete a ticket (admins or the original creator only)",
)
async def delete_ticket(
    ticket_id: int = Path(..., description="Value of the 'Id' (Autonumber) field."),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    existing = await airtable_service.get_ticket_by_id(ticket_id)
    is_admin = await airtable_service.is_hub_admin(user.email)
    if not (is_admin or _is_ticket_owner(existing, user.email)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only admins or the user who created the ticket "
                "(Assigned By) may delete it."
            ),
        )
    return await airtable_service.delete_ticket(ticket_id, existing=existing)


# ══════════════════════════════════════════════════════════════════════
#   Users endpoints
# ══════════════════════════════════════════════════════════════════════
# Fields a non-admin user is permitted to update on their own record.
_USER_SELF_EDITABLE_FIELDS = {"dob", "home_address", "personal_email"}


@router.get(
    "/users/by-email/{work_email}",
    response_model=UserRecord,
    summary="Get a user by Work Email",
)
async def get_user_by_email(
    work_email: str = Path(..., description="Work Email of the user to fetch."),
    _user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    return await airtable_service.get_user_by_work_email(work_email)


@router.patch(
    "/users/{work_email}",
    response_model=UserRecord,
    summary=(
        "Update a user by Work Email. Admins may update any field; "
        "the user themselves may only update DOB, Home Address, and "
        "Personal Email."
    ),
)
async def update_user_by_email(
    work_email: str = Path(..., description="Work Email of the user to update."),
    payload: UserUpdate = Body(...),
    user: UserInfo = Depends(get_current_user),
    airtable_service: AirtableService = Depends(get_airtable_service),
):
    is_admin = await airtable_service.is_hub_admin(user.email)
    is_self = (user.email or "").strip().lower() == (work_email or "").strip().lower()

    if not (is_admin or is_self):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins or the user themselves may update this record.",
        )

    provided = set(payload.model_fields_set)
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided to update.",
        )

    if not is_admin:
        disallowed = provided - _USER_SELF_EDITABLE_FIELDS
        if disallowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "You may only update the following fields: "
                    f"{sorted(_USER_SELF_EDITABLE_FIELDS)}. "
                    f"Disallowed fields: {sorted(disallowed)}."
                ),
            )

    return await airtable_service.update_user_by_work_email(work_email, payload)


