"""Pydantic schemas for Airtable analytics endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AirtableRecord(BaseModel):
    """A single Airtable record as returned by the REST API (raw fields)."""

    id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    createdTime: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────
class _TypedAirtableRecord(BaseModel):
    """
    Base class for typed Airtable record models.

    All declared fields are optional so endpoints can project a subset.
    Field aliases match the original Airtable field names (which often
    contain spaces and special characters).  Unknown fields are kept
    under ``extra`` to remain forward compatible.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="allow",
    )

    id: str


class AmountSumResponse(BaseModel):
    """Response model for endpoints returning a single amount sum."""
    total: float = Field(description="Sum of the Amount field for matched records.")
    record_count: int = Field(description="Number of records included in the sum.")


class UniqueAccountsResponse(BaseModel):
    """Response model for the unique-accounts endpoint."""
    unique_accounts: int = Field(
        description="Number of distinct values in the Account Name field."
    )
    record_count: int = Field(description="Number of records considered.")


class DistributionItem(BaseModel):
    """Single bucket of an Opportunity Record Type distribution."""
    value: str = Field(description="The Opportunity Record Type value.")
    count: int = Field(description="How many records carry this value.")
    percentage: float = Field(
        description="Percentage of records with this value (0-100)."
    )


class DistributionResponse(BaseModel):
    """Distribution of values for the Opportunity Record Type field."""
    total_records: int
    distribution: list[DistributionItem]


class YearlyAmountItem(BaseModel):
    """Aggregated Amount for a given Fiscal Year."""
    fiscal_year: str = Field(description="The Fiscal Year bucket (as a string).")
    total: float = Field(description="Sum of the Amount field for that year.")
    percentage: float = Field(
        description="Percentage of this year's total over the grand total (0-100)."
    )


class YearlyAmountResponse(BaseModel):
    """Response model for the sum-amount-over-years endpoint."""
    grand_total: float
    years: list[YearlyAmountItem]


class OppRecTypeAmountItem(BaseModel):
    """Aggregated Amount for a given Opportunity Record Type."""
    opportunity_rec_type: str = Field(
        description="The Opportunity Record Type bucket (as a string)."
    )
    total: float = Field(
        description="Sum of the Amount field for that Opportunity Record Type."
    )
    percentage: float = Field(
        description=(
            "Percentage of this Opportunity Record Type's total over the "
            "grand total (0-100)."
        )
    )


class OppRecTypeAmountResponse(BaseModel):
    """Response model for the sum-amount-by-opportunity-rec-type endpoint."""
    grand_total: float
    opportunity_rec_types: list[OppRecTypeAmountItem]


# ── Fund & Program Tracker schemas ─────────────────────────────────────
class CountResponse(BaseModel):
    """Generic count response."""
    count: int


class DateRangeFilter(BaseModel):
    """
    A single date filter object.

    Any subset of ``eq_date``, ``lt_date``, ``gt_date`` may be provided
    (ISO ``YYYY-MM-DD``).  Within the object the three predicates are
    combined with OR.
    """
    eq_date: str | None = Field(
        default=None, description="Field equals this ISO date (YYYY-MM-DD)."
    )
    lt_date: str | None = Field(
        default=None, description="Field strictly before this ISO date."
    )
    gt_date: str | None = Field(
        default=None, description="Field strictly after this ISO date."
    )


class IdNameItem(BaseModel):
    """A linked-record reference resolved with its display name."""
    id: str
    name: str | None = None


class AirtableUserIdResponse(BaseModel):
    """Response of the airtable-user-id lookup endpoint."""
    id: str | None = Field(
        default=None,
        description="Airtable user id matching the email, or null if unknown.",
    )
    email: str | None = None
    name: str | None = None


class ActiveProgramItem(BaseModel):
    """An active program with its lead/fellow assignment."""
    id: str
    name: str | None = Field(default=None, alias="Name")
    program_lead_fellow: Any = Field(default=None, alias="Program Lead/Fellow")

    model_config = ConfigDict(populate_by_name=True)


class PersonContactItem(BaseModel):
    """A person identified by first name, last name and work email."""
    first_name: str | None = Field(default=None, alias="First Name")
    last_name: str | None = Field(default=None, alias="Last Name")
    work_email: str | None = Field(default=None, alias="Work Email")

    model_config = ConfigDict(populate_by_name=True)


# ── Generic per-record partial update ─────────────────────────────────
class RecordFieldsUpdate(BaseModel):
    """Generic partial update payload for a typed Airtable record.

    The ``fields`` dictionary keys are the Airtable column names (which
    match the alias of the corresponding field on the table's typed
    record model — e.g. ``"Name"``, ``"Status"``,
    ``"Focus Area(s)"``). Values follow Airtable's expected types
    (string, number, bool, list of strings for multi-select / linked
    records, etc.). ``typecast=True`` is used server-side so single-
    select / multi-select string values are accepted as-is.

    Only the fields included in this dictionary are updated; omitted
    fields are left untouched. Set a key to ``null`` to clear the field.
    """

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any] = Field(
        description=(
            "Map of Airtable field name → new value. "
            "Must contain at least one entry."
        ),
        min_length=1,
    )


# ══════════════════════════════════════════════════════════════════════
#   Per-table typed record models
# ══════════════════════════════════════════════════════════════════════

# ── Master List of Funds & Subprograms ─────────────────────────────────
class MasterListFundsAndSubprogramsRecord(_TypedAirtableRecord):
    name: str | None = Field(default=None, alias="Name")
    fundraising_stage: list[str] | None = Field(default=None, alias="Fundraising Stage")
    status: str | None = Field(default=None, alias="Status")
    official_fund_or_program_name: str | None = Field(
        default=None, alias="Official Fund or Program Name"
    )
    initiative_type: str | None = Field(default=None, alias="Initiative Type")
    focus_areas: list[str] | None = Field(default=None, alias="Focus Area(s)")
    program_summary: str | None = Field(default=None, alias="Program Summary")
    internal_notes: str | None = Field(default=None, alias="Internal Notes")
    program_lead_fellow: Any = Field(default=None, alias="Program Lead/Fellow")
    program_lead_email: str | None = Field(default=None, alias="Program Lead Email")
    scoping_proposal_fund_overview: Any = Field(
        default=None, alias="Scoping Proposal / Fund Overview"
    )
    status_of_program: list[str] | None = Field(
        default=None, alias="Status of Program"
    )
    summary_of_conversation: str | None = Field(
        default=None, alias="Summary of Conversation"
    )
    amount: float | None = Field(default=None, alias="Amount")
    onboarding_notes: str | None = Field(default=None, alias="Onboarding Notes")
    internal_intake_doc: Any = Field(default=None, alias="Internal/Intake Doc")
    cluster: list[str] | None = Field(default=None, alias="Cluster Name")
    onboarding_hours: str | None = Field(default=None, alias="Onboarding hours")
    ongoing_hours: str | None = Field(default=None, alias="Ongoing hours")
    biggest_needs: str | None = Field(default=None, alias="Biggest needs?")
    add_to_new_shareable_doc: bool | None = Field(
        default=None, alias="Add to New Shareable Doc"
    )
    can_we_talk_about_it_publicly: bool | None = Field(
        default=None, alias="Can we talk about it publicly"
    )
    website: str | None = Field(default=None, alias="Website")
    has_attachments: bool | None = Field(default=None, alias="Has Attachments")
    summary_document_concept_note: Any = Field(
        default=None, alias="Summary Document / Concept Note"
    )
    deliverables: list[str] | None = Field(
        default=None,
        alias="Deliverable Name (from Awarded Opportunities with Designations)",
    )
    deliverable_due_date: list[str] | None = Field(
        default=None, alias="Deliverable Due Date"
    )


# ── Glossary ───────────────────────────────────────────────────────────
class GlossaryRecord(_TypedAirtableRecord):
    column: str | None = Field(default=None, alias="Column")
    term: str | None = Field(default=None, alias="Term")
    definition: str | None = Field(default=None, alias="Definition")


# ── Org Friends ────────────────────────────────────────────────────────
class OrgFriendsRecord(_TypedAirtableRecord):
    name_of_proposal: str | None = Field(default=None, alias="Name of Proposal")
    name_of_org: str | None = Field(default=None, alias="Name of Org (if applicable)")
    paragraph_summary: str | None = Field(default=None, alias="Paragraph Summary")
    issue_areas: list[str] | None = Field(default=None, alias="Issue Area(s)")
    submitter: Any = Field(default=None, alias="Submitter")
    unique_insight: str | None = Field(
        default=None,
        alias="What's the unique insight from this proposal? What makes it clever?*",
    )
    level_of_conviction: list[str] | None = Field(
        default=None, alias="Level of Conviction"
    )
    proposal: Any = Field(default=None, alias="Proposal")
    additional_attachments: Any = Field(default=None, alias="Additional Attachments")
    lead_contact: str | None = Field(default=None, alias="Lead Contact")
    lead_contact_email: str | None = Field(default=None, alias="Lead contact email")
    location: str | None = Field(default=None, alias="Location")
    latest_update: str | None = Field(default=None, alias="Latest update")


# ── Funders ────────────────────────────────────────────────────────────
class FundersRecord(_TypedAirtableRecord):
    opportunity_name: str | None = Field(default=None, alias="Opportunity Name")
    close_date: str | None = Field(default=None, alias="Close Date")
    gift_designation_name: str | None = Field(
        default=None, alias="Gift Designation: Name"
    )
    stage: str | None = Field(default=None, alias="Stage")
    account_name: str | None = Field(default=None, alias="Account Name: Account Name")


# ── Shareable Docs ─────────────────────────────────────────────────────
class ShareableDocsRecord(_TypedAirtableRecord):
    programs: list[str] | None = Field(
        default=None,
        alias="Programs",
        description="Linked record ids of the related programs.",
    )
    document: str | None = Field(
        default=None, alias="Document", description="URL to the document."
    )
    created: str | None = Field(
        default=None, alias="Created", description="ISO date-time."
    )


# ── Doc Titles ─────────────────────────────────────────────────────────
class DocTitleRecord(_TypedAirtableRecord):
    new_doc_title: str | None = Field(default=None, alias="NEW DOC TITLE")
    notes: str | None = Field(default=None, alias="Notes")


# ── Partnerships Fundraising ───────────────────────────────────────────
class PartnershipsFundraisingRecord(BaseModel):
    """A row from the Partnerships Fundraising table.

    Unlike most other typed Airtable records, the canonical ``id`` here is
    the table's autonumber ``Id`` field. The Airtable record id is
    returned separately as ``record_id``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    record_id: str = Field(description="Airtable record id (e.g. 'rec...').")
    id: int | None = Field(
        default=None,
        alias="Id",
        description="Autonumber 'Id' value from the Airtable table.",
    )
    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(
        default=None,
        alias="Document URL",
        description=(
            "URL to the document. May be null when 'Document' does not "
            "refer to an actual document."
        ),
    )
    notes: str | None = Field(default=None, alias="Notes")


class PartnershipsFundraisingUpdate(BaseModel):
    """Partial update payload for a Partnerships Fundraising record.

    Any subset of fields may be provided. Fields not included in the
    payload are left untouched. Set a field explicitly to ``null`` to
    clear it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(default=None, alias="Document URL")
    notes: str | None = Field(default=None, alias="Notes")


# ── Finance Links ──────────────────────────────────────────────────────
class FinanceLinkRecord(BaseModel):
    """A row from the Finance Links table.

    The canonical ``id`` here is the table's autonumber ``Id`` field. The
    Airtable record id is returned separately as ``record_id``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    record_id: str = Field(description="Airtable record id (e.g. 'rec...').")
    id: int | None = Field(
        default=None,
        alias="Id",
        description="Autonumber 'Id' value from the Airtable table.",
    )
    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(
        default=None,
        alias="Document URL",
        description="URL to the document.",
    )


class FinanceLinkUpdate(BaseModel):
    """Partial update payload for a Finance Links record.

    Any subset of fields may be provided. Fields not included in the
    payload are left untouched. Set a field explicitly to ``null`` to
    clear it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(default=None, alias="Document URL")


# ── Office Spaces ──────────────────────────────────────────────────────
class OfficeSpaceRecord(BaseModel):
    """A row from the Office Spaces table.

    The canonical ``id`` here is the table's autonumber ``Id`` field. The
    Airtable record id is returned separately as ``record_id``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    record_id: str = Field(description="Airtable record id (e.g. 'rec...').")
    id: int | None = Field(
        default=None,
        alias="Id",
        description="Autonumber 'Id' value from the Airtable table.",
    )
    branch: str | None = Field(default=None, alias="Branch")
    address: str | None = Field(default=None, alias="Address")
    details: str | None = Field(default=None, alias="Details")


class OfficeSpaceCreate(BaseModel):
    """Payload to create a new Office Spaces record."""

    model_config = ConfigDict(extra="forbid")

    branch: str = Field(description="Branch name (required).")
    address: str = Field(description="Office address (required).")
    details: str | None = Field(
        default=None, description="Optional free-form details about the office."
    )


class OfficeSpaceUpdate(BaseModel):
    """Partial update payload for an Office Spaces record.

    Any subset of fields may be provided. Fields not included in the
    payload are left untouched. Set a field explicitly to ``null`` to
    clear it.
    """

    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    address: str | None = None
    details: str | None = None


# ── Google Docs Tabs ───────────────────────────────────────────────────
class GoogleDocsTabRecord(BaseModel):
    """A row from the Google Docs Tabs table."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    record_id: str = Field(description="Airtable record id (e.g. 'rec...').")
    id: int | None = Field(
        default=None,
        alias="Id",
        description="Autonumber 'Id' value from the Airtable table.",
    )
    ui_page: str | None = Field(default=None, alias="UI Page")
    document_id: str | None = Field(default=None, alias="Document Id")
    tab_id: str | None = Field(default=None, alias="Tab Id")


# ── Meeting Cadence ────────────────────────────────────────────────────
class MeetingCadenceRecord(_TypedAirtableRecord):
    """A row from the Meeting Cadence table."""

    meeting_title: str | None = Field(default=None, alias="Meeting Title")
    description: str | None = Field(default=None, alias="Description")
    attachment_url: str | None = Field(
        default=None,
        alias="Attachment URL",
        description="Optional URL to an attachment for this meeting.",
    )


# ── Useful Links ───────────────────────────────────────────────────────
class UsefulLinkRecord(_TypedAirtableRecord):
    """A row from the Useful Links table."""

    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(default=None, alias="Document URL")
    description: str | None = Field(default=None, alias="Description")


# ── HR & Benefits ──────────────────────────────────────────────────────
class HrAndBenefitsRecord(_TypedAirtableRecord):
    """A row from the HR & Benefits table."""

    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(default=None, alias="Document URL")
    description: str | None = Field(default=None, alias="Description")


# ── Onboarding ─────────────────────────────────────────────────────────
class OnboardingLinkRecord(_TypedAirtableRecord):
    """A row from the Onboarding table."""

    document: str | None = Field(default=None, alias="Document")
    document_url: str | None = Field(default=None, alias="Document URL")
    notes: str | None = Field(default=None, alias="Notes")


# ── Onboarding Calls ───────────────────────────────────────────────────
class OnboardingCallRecord(_TypedAirtableRecord):
    """A row from the Onboarding Calls table."""

    date: str | None = Field(
        default=None, alias="Date", description="ISO date (YYYY-MM-DD)."
    )
    notes: str | None = Field(default=None, alias="Notes")


# ── Quick Links ────────────────────────────────────────────────────────
class QuickLinkRecord(BaseModel):
    """A row from the Quick Links table.

    The canonical ``id`` here is the table's autonumber ``Id`` field. The
    Airtable record id is returned separately as ``record_id``. ``action``
    is sourced from the Airtable lookup field that pulls the action text
    from the linked Quick Actions record.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    record_id: str = Field(description="Airtable record id (e.g. 'rec...').")
    id: int | None = Field(
        default=None,
        alias="Id",
        description="Autonumber 'Id' value from the Airtable table.",
    )
    anchor_text: str | None = Field(default=None, alias="Anchor Text")
    url: str | None = Field(default=None, alias="URL")
    email: str | None = Field(default=None, alias="Email")
    action: str | None = Field(
        default=None,
        alias="Action",
        description="Action text resolved from the linked Quick Actions record.",
    )

    @field_validator("action", mode="before")
    @classmethod
    def _unwrap_action(cls, value: Any) -> Any:
        # 'Action' is an Airtable lookup field, which returns its value
        # as a single-element list. Unwrap to a scalar for clients.
        if isinstance(value, list):
            return value[0] if value else None
        return value


class QuickLinkCreate(BaseModel):
    """Payload to create a new Quick Links record.

    The ``action`` string is upserted into the Quick Actions table and
    linked to this record via the 'Quick Actions' linked-record field.
    """

    model_config = ConfigDict(extra="forbid")

    anchor_text: str = Field(description="Display text for the link.")
    action: str = Field(
        description=(
            "Action text. Will be created in the Quick Actions table if it "
            "does not already exist, then linked to this Quick Links row."
        )
    )
    url: str | None = Field(
        default=None, description="Optional URL the link points to."
    )
    email: str | None = Field(
        default=None, description="Optional email address associated with the link."
    )


class QuickLinkUpdate(BaseModel):
    """Partial update payload for a Quick Links record.

    Any subset of fields may be provided. Fields not included in the
    payload are left untouched. Set ``url`` or ``email`` to ``null`` to
    clear them. If ``action`` is provided, the value is upserted into
    the Quick Actions table and the link is updated.
    """

    model_config = ConfigDict(extra="forbid")

    anchor_text: str | None = None
    url: str | None = None
    email: str | None = None
    action: str | None = None


# ── Clusters ───────────────────────────────────────────────────────────
class ClusterRecord(BaseModel):
    """A cluster reference resolved with its display name."""

    model_config = ConfigDict(populate_by_name=True)

    record_id: str = Field(description="Airtable record id of the cluster.")
    name: str | None = Field(default=None, alias="Name")


# ── Check-In Reporting Periods ─────────────────────────────────────────
class CheckinReportingPeriodRecord(BaseModel):
    """A check-in reporting period reference resolved with its Period value."""

    model_config = ConfigDict(populate_by_name=True)

    record_id: str = Field(description="Airtable record id of the period.")
    period: str | None = Field(default=None, alias="Period")


# ── Funds & Programs Monthly Check-In ──────────────────────────────────
class MonthlyCheckinRecord(_TypedAirtableRecord):
    name: str | None = Field(default=None, alias="Name")
    status: str | None = Field(default=None, alias="Status")
    big_wins_and_updates: str | None = Field(
        default=None, alias="Big Wins & Updates"
    )
    upcoming_operational_needs: str | None = Field(
        default=None, alias="Upcoming operational needs"
    )
    flag_for_discussion: bool | None = Field(
        default=None, alias="Flag for Discussion?"
    )
    report_complete: bool | None = Field(default=None, alias="Report Complete?")
    reporting_lead: Any = Field(default=None, alias="Reporting Lead")
    followup_indicated: str | None = Field(default=None, alias="Followup Indicated")
    program_name: Any = Field(default=None, alias="Program Name")
    phase_status_from_program_name: Any = Field(
        default=None, alias="Phase/Status (from Program Name)"
    )
    any_req_followup_complete: bool | None = Field(
        default=None, alias="Any Req. Followup Complete?"
    )
    followup_notes: str | None = Field(default=None, alias="Followup Notes")
    timeline: str | None = Field(default=None, alias="Timeline")
    submission_extension: bool | None = Field(
        default=None, alias="Submission Extension"
    )
    old_to_discuss_with_program_team: str | None = Field(
        default=None, alias="[OLD] To discuss with Program Team"
    )
    old_operational_needs_and_activities: str | None = Field(
        default=None, alias="[OLD] Operational Needs & Activities (6 mo. Outlook)"
    )


# ── Announcements ──────────────────────────────────────────────────────
class AnnouncementCreate(BaseModel):
    """Payload to create an announcement.

    Stored in the Announcements table with Status defaulted to ``Drafted``.
    """

    title: str = Field(description="Announcement title.")
    content: str = Field(description="Announcement body / content.")
    author_email: str = Field(description="Email of the announcement author.")
    category: list[str] = Field(
        description="One or more category values (multi-select)."
    )
    attachments: list[str] | None = Field(
        default=None,
        description="Optional list of attachment URLs.",
    )
    priority: str = Field(description="Priority single-select value.")
    publish_time: datetime = Field(description="When the announcement publishes.")
    expiration_time: datetime = Field(description="When the announcement expires.")


class AnnouncementUpdate(BaseModel):
    """Partial update payload for an announcement.

    Any subset of fields may be provided. Fields left unset are not touched.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    content: str | None = None
    author_email: str | None = None
    category: list[str] | None = None
    attachments: list[str] | None = None
    reviewer_comments: str | None = None
    priority: str | None = None
    approved: bool | None = None
    approved_by: str | None = Field(
        default=None,
        description=(
            "Email of the user approving the announcement. Required "
            "when 'approved' is provided; must be omitted otherwise."
        ),
    )
    status: str | None = None
    publish_time: datetime | None = None
    expiration_time: datetime | None = None

    @model_validator(mode="after")
    def _check_approved_by(self) -> "AnnouncementUpdate":
        ack_set = "approved" in self.model_fields_set
        ack_by_set = "approved_by" in self.model_fields_set
        if ack_set and not ack_by_set:
            raise ValueError(
                "'approved_by' is required when 'approved' is provided."
            )
        if ack_by_set and not ack_set:
            raise ValueError(
                "'approved_by' may only be set together with 'approved'."
            )
        return self


class AnnouncementRecord(_TypedAirtableRecord):
    """An announcement record as returned to clients."""

    announcement_id: Any = Field(default=None, alias="Id")
    title: str | None = Field(default=None, alias="Title")
    content: str | None = Field(default=None, alias="Content")
    author_email: str | None = Field(default=None, alias="Author Email")
    category: list[str] | None = Field(default=None, alias="Category")
    attachments: Any = Field(default=None, alias="Attachments")
    reviewer_comments: str | None = Field(default=None, alias="Reviewer Comments")
    priority: str | None = Field(default=None, alias="Priority")
    approved: bool | None = Field(default=None, alias="Approved")
    approved_by: str | None = Field(default=None, alias="Approved By")
    status: str | None = Field(default=None, alias="Status")
    publish_time: str | None = Field(default=None, alias="Publish Time")
    expiration_time: str | None = Field(default=None, alias="Expiration Time")

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value
        return [value]


# ── Access Control ────────────────────────────────────────────────────
class Permission(BaseModel):
    """A permission with its id, name and description."""

    id: str
    name: str | None = None
    description: str | None = None


class Role(BaseModel):
    """A role with its id, name, scope and the permissions it grants."""

    id: str
    name: str | None = None
    scope: str | None = None
    permissions: list[Permission] = Field(default_factory=list)


class AccessControlRecord(BaseModel):
    """An Access Control record as returned to clients.

    Roles and permissions are resolved to objects carrying the record id,
    name and (for permissions) description — sourced from the lookup
    fields on the Access Control table.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    user_email: str | None = None
    roles: list[Role] = Field(default_factory=list)
    permissions: list[Permission] = Field(default_factory=list)
    fund_or_program_name: str | None = None


class AccessControlAssign(BaseModel):
    """Payload to upsert role(s) and/or permission(s) for a user email."""

    model_config = ConfigDict(extra="forbid")

    user_email: str = Field(description="Email of the user.")
    roles: list[str] | None = Field(
        default=None, description="Role record IDs to add. Optional."
    )
    permissions: list[str] | None = Field(
        default=None, description="Permission record IDs to add. Optional."
    )

    @model_validator(mode="after")
    def _check_at_least_one(self) -> "AccessControlAssign":
        if not self.roles and not self.permissions:
            raise ValueError(
                "At least one of 'roles' or 'permissions' must be provided."
            )
        return self


class AccessControlRevoke(BaseModel):
    """Payload to revoke role(s) and/or permission(s) for a user email."""

    model_config = ConfigDict(extra="forbid")

    user_email: str = Field(description="Email of the user.")
    roles: list[str] | None = Field(
        default=None, description="Role record IDs to remove. Optional."
    )
    permissions: list[str] | None = Field(
        default=None, description="Permission record IDs to remove. Optional."
    )

    @model_validator(mode="after")
    def _check_at_least_one(self) -> "AccessControlRevoke":
        if not self.roles and not self.permissions:
            raise ValueError(
                "At least one of 'roles' or 'permissions' must be provided."
            )
        return self


class RoleCreate(BaseModel):
    """Payload to create a new Role."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Role name.")
    scope: str | None = Field(default=None, description="Optional scope value.")
    permissions: list[str] | None = Field(
        default=None,
        description="Optional list of Permission record IDs to link.",
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("name must be a non-empty string.")
        return value


class RoleUpdate(BaseModel):
    """Payload to update an existing Role.

    All fields are optional. ``name`` and ``scope`` replace the current
    values when provided. ``add_permissions`` / ``remove_permissions``
    incrementally edit the linked Permissions field by Permission record
    IDs. ``permissions`` (if provided) overrides the entire linked list
    and may not be combined with ``add_permissions``/``remove_permissions``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="New role name.")
    scope: str | None = Field(default=None, description="New scope value.")
    permissions: list[str] | None = Field(
        default=None,
        description=(
            "Replace the linked Permissions with this exact list of "
            "Permission record IDs."
        ),
    )
    add_permissions: list[str] | None = Field(
        default=None,
        description="Permission record IDs to add to the linked list.",
    )
    remove_permissions: list[str] | None = Field(
        default=None,
        description="Permission record IDs to remove from the linked list.",
    )

    @model_validator(mode="after")
    def _validate(self) -> "RoleUpdate":
        if (
            self.name is None
            and self.scope is None
            and self.permissions is None
            and not self.add_permissions
            and not self.remove_permissions
        ):
            raise ValueError("At least one field must be provided.")
        if self.permissions is not None and (
            self.add_permissions or self.remove_permissions
        ):
            raise ValueError(
                "'permissions' cannot be combined with "
                "'add_permissions' or 'remove_permissions'."
            )
        return self


# ── Tickets ───────────────────────────────────────────────────────────
TicketStatus = str  # constrained at runtime by validators below
TicketSource = str

_TICKET_STATUS_VALUES = {"Open", "Closed", "Blocked", "In Progress"}
_TICKET_SOURCE_VALUES = {"RenPhil Hub", "Slack", "Email"}


class TicketCreate(BaseModel):
    """Payload to create a new ticket."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Ticket title.")
    description: str | None = Field(default=None, description="Ticket description.")
    assignee: str = Field(description="Email of the assignee.")
    assigned_by: str = Field(description="Email of the person assigning the ticket.")
    status: str = Field(
        description="One of: Open, Closed, Blocked, In Progress."
    )
    source: str = Field(
        description="One of: RenPhil Hub, Slack, Email."
    )
    created_date: datetime = Field(description="Ticket creation date and time.")
    due_date: datetime = Field(description="Ticket deadline date and time.")
    parent_ticket_id: int | None = Field(
        default=None,
        description=(
            "Optional. The 'Id' (autonumber) of the parent ticket. "
            "Leave empty for a top-level ticket."
        ),
    )

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in _TICKET_STATUS_VALUES:
            raise ValueError(
                f"status must be one of {sorted(_TICKET_STATUS_VALUES)}."
            )
        return value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in _TICKET_SOURCE_VALUES:
            raise ValueError(
                f"source must be one of {sorted(_TICKET_SOURCE_VALUES)}."
            )
        return value


class TicketUpdate(BaseModel):
    """Partial update payload for a ticket.

    Any subset of the fields below may be provided. ``Last Updated`` and
    ``Last Updated By`` are managed automatically server-side and must not
    be supplied by the client.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    status: str | None = None
    assignee: str | None = None
    due_date: datetime | None = None
    comments: str | None = None
    parent_ticket_id: int | None = Field(
        default=None,
        description=(
            "Optional. The 'Id' (autonumber) of the parent ticket. "
            "Only admins and the ticket creator (Assigned By) may set this."
        ),
    )

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in _TICKET_STATUS_VALUES:
            raise ValueError(
                f"status must be one of {sorted(_TICKET_STATUS_VALUES)}."
            )
        return value


class TicketRecord(_TypedAirtableRecord):
    """A ticket record as returned to clients."""

    ticket_id: Any = Field(default=None, alias="Id")
    title: str | None = Field(default=None, alias="Title")
    description: str | None = Field(default=None, alias="Description")
    status: str | None = Field(default=None, alias="Status")
    assignee: str | None = Field(default=None, alias="Assignee")
    assigned_by: str | None = Field(default=None, alias="Assigned By")
    source: str | None = Field(default=None, alias="Source")
    created_date: str | None = Field(default=None, alias="Created Date")
    due_date: str | None = Field(default=None, alias="Due Date")
    last_updated: str | None = Field(default=None, alias="Last Updated")
    last_updated_by: str | None = Field(default=None, alias="Last Updated By")
    comments: str | None = Field(default=None, alias="Comments")
    parent_ticket_id: Any = Field(default=None, alias="Parent Ticket Id")

    @field_validator("parent_ticket_id", mode="before")
    @classmethod
    def _unwrap_parent_ticket_id(cls, value: Any) -> Any:
        # 'Parent Ticket Id' is an Airtable lookup field, which returns
        # values as a single-element list. Unwrap to a scalar for clients.
        if isinstance(value, list):
            return value[0] if value else None
        return value


class SlackTicketWebhookPayload(BaseModel):
    """Payload built from the Slack ``/tickets`` slash command to create a ticket.

    ``Source`` is forced to ``"Slack"`` and ``Created Date`` is set
    server-side at the moment the webhook is received.  ``Status`` is
    left empty (Airtable defaults it to ``"Open"``).
    """

    model_config = ConfigDict(extra="forbid")

    assigned_by: str = Field(description="Email of the person assigning the ticket.")
    title: str = Field(description="Ticket title.")
    description: str | None = Field(default=None, description="Ticket description.")
    assignee: str = Field(description="Email of the assignee.")
    due_date: datetime | None = Field(
        default=None, description="Ticket deadline date and time (optional)."
    )


class EmailTicketWebhookPayload(BaseModel):
    """Payload posted by the email-based ticket-assignment integration.

    ``Source`` is forced server-side to ``"Email"``. The ``source`` field
    on this payload is informational only and is not used by the endpoint.
    """

    model_config = ConfigDict(extra="ignore")

    assigned_by: str = Field(
        description="Sender of the ticket-assignment email (may be in "
        "'Name <email@host>' form)."
    )
    email_content: str = Field(
        description="Plain-text body of the email to extract ticket fields from."
    )
    received_by: str | None = Field(
        default=None,
        description="Mailbox that received the assignment email (informational).",
    )
    source: str | None = Field(
        default=None,
        description="Origin of the email integration (informational, unused).",
    )


# ── Users ─────────────────────────────────────────────────────────────
class UserRecord(_TypedAirtableRecord):
    """A user record from the Users table (RenPhil Hub base)."""

    name: str | None = Field(default=None, alias="Name")
    first_name: str | None = Field(default=None, alias="First Name")
    last_name: str | None = Field(default=None, alias="Last Name")
    employment_type: list[str] | None = Field(
        default=None, alias="Employment Type"
    )
    status: str | None = Field(default=None, alias="Status")
    department: str | None = Field(default=None, alias="Department")
    program: Any = Field(default=None, alias="Program")
    start_date: str | None = Field(default=None, alias="Start Date")
    work_email: str | None = Field(default=None, alias="Work Email")
    personal_email: str | None = Field(default=None, alias="Personal Email")
    position: str | None = Field(default=None, alias="Position")
    dob: str | None = Field(default=None, alias="DOB")
    headshot: Any = Field(
        default=None,
        alias="Headshot",
        description="Airtable attachment array for the user's headshot image.",
    )
    office_location: str | None = Field(default=None, alias="Office location")
    home_address: str | None = Field(default=None, alias="Home Address")
    bio: str | None = Field(default=None, alias="Bio")
    scope_of_work: str | None = Field(default=None, alias="ScopeofWork")
    end_date: str | None = Field(default=None, alias="End Date")
    manager: Any = Field(default=None, alias="Manager")
    tech_stack_selections: list[str] | None = Field(
        default=None, alias="Tech Stack Selections"
    )

    @field_validator("employment_type", "tech_stack_selections", mode="before")
    @classmethod
    def _coerce_multi(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value
        return [value]


class UserUpdate(BaseModel):
    """Partial update payload for a user record.

    Field permissions:
      * Admins may update any field below.
      * The user themselves may update ONLY: ``dob``, ``home_address``,
        and ``personal_email`` — this is enforced at the endpoint layer.

    Any subset of fields may be provided. Fields left unset are not touched.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    employment_type: list[str] | None = None
    status: str | None = None
    department: str | None = None
    program: Any | None = None
    start_date: str | None = Field(
        default=None, description="ISO date (YYYY-MM-DD)."
    )
    work_email: str | None = None
    personal_email: str | None = None
    position: str | None = None
    dob: str | None = Field(
        default=None, description="ISO date of birth (YYYY-MM-DD)."
    )
    headshot: list[str] | None = Field(
        default=None,
        description="List of attachment URLs for the headshot.",
    )
    office_location: str | None = None
    home_address: str | None = None
    bio: str | None = None
    scope_of_work: str | None = None
    end_date: str | None = Field(
        default=None, description="ISO date (YYYY-MM-DD)."
    )
    manager: Any | None = None
    tech_stack_selections: list[str] | None = None


