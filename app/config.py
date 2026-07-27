"""
Centralised application settings loaded from environment variables.
Uses pydantic-settings so every value is validated at startup.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────────────
    APP_NAME: str = "RenPhil Hub API"
    DEBUG: bool = False
    # Loaded strictly from the ALLOWED_ORIGINS environment variable (JSON list)
    ALLOWED_ORIGINS: list[str]
    # Comma-separated emails granted "Hub Admin" without an Access Control
    # record. Only takes effect when DEBUG=true — local testing convenience,
    # never honored in production.
    # DEV_ADMIN_OVERRIDE_EMAILS: str = ""

    # ── Google OAuth ───────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/callback"
    ALLOWED_EMAIL_DOMAIN: str = "renphil.org"

    # ── JWT Tokens (issued after OAuth) ────────────────────────────────
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Google Drive ───────────────────────────────────────────────────
    GOOGLE_DRIVE_FOLDER_ID: str
    # Production: store the entire service-account JSON as a string in this var.
    # Local dev fallback: path to the JSON key file.
    GOOGLE_SERVICE_ACCOUNT_JSON: str | None = None
    GOOGLE_SERVICE_ACCOUNT_FILE: str = "service_account.json"

    # ── Google Calendar (shared events account) ────────────────────────
    # The calendar widget reads events from, and RSVPs viewers onto, a single
    # shared calendar owned by GOOGLE_CALENDAR_ID (e.g. cal@renphil.org). The
    # backend acts *as* that account using a refresh token it granted once via
    # scripts/get_calendar_refresh_token.py — deliberately NOT domain-wide
    # delegation, so a leaked token can touch only this one account's events.
    GOOGLE_CALENDAR_ID: str | None = None
    GOOGLE_CALENDAR_REFRESH_TOKEN: str | None = None
    # Passed as `sendUpdates` on attendee changes. "all" emails guests, "none"
    # adds/removes silently (the event still lands on the user's calendar).
    # Flip to "none" if "all" proves noisy on large events.
    GOOGLE_CALENDAR_SEND_UPDATES: str = "all"

    # ── Dify.ai ────────────────────────────────────────────────────────
    DIFY_API_BASE_URL: str = "https://api.dify.ai/v1"
    DIFY_API_KEY: str

    # ══════════════════════════════════════════════════════════════════
    # Airtable
    # ══════════════════════════════════════════════════════════════════
    # All Airtable base ids, table ids, personal access tokens and
    # field names are loaded strictly from the environment — no
    # in-code defaults are provided. Update the ``.env`` file to
    # change any of them.
    # ------------------------------------------------------------------

    # ── Personal access token ─────────────────────────────────────────
    AIRTABLE_API_KEY: str

    # ── Base ids ──────────────────────────────────────────────────────
    AIRTABLE_FUNDRAISING_BASE_ID: str
    AIRTABLE_FUND_PROGRAM_BASE_ID: str
    RENPHIL_HUB_BASE_ID: str

    # ── Table ids / names ─────────────────────────────────────────────
    # Fundraising base
    TOTAL_MOVED_AND_DEPLOYED_TABLE_NAME: str
    # Fund & Program Tracker base
    MASTER_LIST_FUNDS_AND_SUBPROGRAMS_TABLE: str
    GLOSSARY_TABLE: str
    ORG_FRIENDS_TABLE: str
    FUNDERS_TABLE: str
    FUNDS_AND_PROGRAMS_MONTHLY_CHECKIN_TABLE: str
    CHECKIN_REPORTING_PERIODS_TABLE: str
    DOC_TITLES_TABLE: str
    SHAREABLE_DOCS_TABLE: str
    CLUSTERS_TABLE: str
    AWARDED_OPPORTUNITIES_TABLE: str
    DELIVERABLES_TABLE: str
    # RenPhil Hub base
    ADMINS_TABLE: str
    ANNOUNCEMENTS_TABLE: str
    TICKETS_TABLE: str
    GRANT_APPLICATION_RESOURCES_TABLE: str | None = None
    FINANCE_LINKS_TABLE: str
    GOOGLE_DOCS_TABS_TABLE: str
    OFFICE_SPACES_TABLE: str
    ACCESS_CONTROL_TABLE: str
    TEAMS_TABLE: str
    USERS_TABLE: str
    ROLES_TABLE: str
    PERMISSIONS_TABLE: str
    MEETING_CADENCE_TABLE: str
    USEFUL_LINKS_TABLE: str
    HR_AND_BENEFITS_TABLE: str
    ONBOARDING_TABLE: str
    ONBOARDING_CALLS_TABLE: str
    QUICK_LINKS_TABLE: str
    QUICK_ACTIONS_TABLE: str
    ONBOARDING_CHECKLIST_TABLE: str
    GENERAL_FUNDRAISING_RESOURCES_TABLE: str
    PARTNERSHIPS_LINKS_TABLE: str
    POLICY_LINKS_TABLE: str
    EVENTS_QUICK_LINKS_TABLE: str
    FINANCE_QUICK_LINKS_TABLE: str
    COMMS_QUICK_LINKS_TABLE: str
    HR_QUICK_LINKS_TABLE: str
    RENPHIL_DUE_DILIGENCE_LINKS_TABLE: str
    BOARD_MEMBER_LIST_TABLE: str
    ORGANIZATION_INFO_TABLE: str

    # ── Field names ───────────────────────────────────────────────────
    # Admins / Access Control
    ADMINS_EMAIL_FIELD: str
    ACCESS_CONTROL_USER_EMAIL_FIELD: str
    ACCESS_CONTROL_ROLES_FIELD: str
    ACCESS_CONTROL_PERMISSIONS_FIELD: str
    ACCESS_CONTROL_FUND_OR_PROGRAM_NAME_FIELD: str
    ACCESS_CONTROL_ROLE_NAME_LOOKUP_FIELD: str
    ACCESS_CONTROL_PERMISSION_NAME_LOOKUP_FIELD: str
    ACCESS_CONTROL_PERMISSION_DESCRIPTION_LOOKUP_FIELD: str
    # Teams / Users
    TEAMS_WORK_EMAIL_FIELD: str
    TEAMS_NAME_FIELD: str
    USERS_WORK_EMAIL_FIELD: str
    USERS_NAME_FIELD: str
    USERS_FIRST_NAME_FIELD: str
    USERS_LAST_NAME_FIELD: str
    USERS_EMPLOYMENT_TYPE_FIELD: str
    USERS_STATUS_FIELD: str
    USERS_DEPARTMENT_FIELD: str
    USERS_PROGRAM_FIELD: str
    USERS_START_DATE_FIELD: str
    USERS_PERSONAL_EMAIL_FIELD: str
    USERS_POSITION_FIELD: str
    USERS_DOB_FIELD: str
    USERS_OFFICE_LOCATION_FIELD: str
    USERS_HOME_ADDRESS_FIELD: str
    USERS_BIO_FIELD: str
    USERS_SCOPE_OF_WORK_FIELD: str
    USERS_END_DATE_FIELD: str
    USERS_MANAGER_FIELD: str
    USERS_TECH_STACK_SELECTIONS_FIELD: str
    USERS_HEADSHOT_FIELD: str
    USERS_FOR_WEBSITE_FIELD: str
    # Roles / Permissions
    ROLES_NAME_FIELD: str
    ROLES_PERMISSIONS_FIELD: str
    ROLES_SCOPE_FIELD: str
    PERMISSIONS_NAME_FIELD: str
    PERMISSIONS_DESCRIPTION_FIELD: str

    # ── Fundraising (Total Moved & Deployed) fields ───────────────────
    AT_F_AMOUNT: str
    AT_F_FISCAL_YEAR: str
    AT_F_OPP_REC_TYPE: str
    AT_F_ACCOUNT_NAME: str

    # ── Fund & Program Tracker fields ─────────────────────────────────
    AT_F_EXCLUDE_FROM_LISTS: str
    AT_F_EXCLUDE_FROM_REPORTING: str
    AT_F_STATUS: str
    AT_F_SUB_TRACK_OF: str
    AT_F_SHARE_PUBLICLY: str
    AT_F_ONBOARDING_STATUS: str
    AT_F_ADD_TO_SHAREABLE_DOC: str
    AT_F_NAME: str
    AT_F_SCOPING_PROP_OVERVIEW: str
    AT_F_INITIATIVE_TYPE: str
    AT_F_FOCUS_AREAS: str
    AT_F_PROGRAM_LEAD_FELLOW: str
    AT_F_DAYS_UNTIL_DEADLINE: str
    AT_F_SUBMISSION_EXTENSION: str
    AT_F_REPORTING_LEAD: str
    AT_F_REPORT_COMPLETE: str
    AT_F_FLAG_FOR_DISCUSSION: str
    AT_F_PROGRAM_NAME: str
    AT_F_CHECKIN_HISTORY: str
    AT_F_CHECKIN_REPORTING_PERIOD: str
    AT_F_CLUSTER: str
    AT_F_CLUSTER_NAME: str
    AT_F_DASHBOARD_DISPLAY: str
    AT_F_FOLLOWUP_INDICATED: str
    AT_F_DEADLINE: str
    AT_F_REVIEW_UNTIL: str
    AT_F_PERIOD: str

    # ── Announcements fields ──────────────────────────────────────────
    AT_F_ANN_ID: str
    AT_F_ANN_TITLE: str
    AT_F_ANN_CONTENT: str
    AT_F_ANN_AUTHOR_EMAIL: str
    AT_F_ANN_CATEGORY: str
    AT_F_ANN_ATTACHMENTS: str
    AT_F_ANN_REVIEWER_COMMENTS: str
    AT_F_ANN_PRIORITY: str
    AT_F_ANN_APPROVED: str
    AT_F_ANN_STATUS: str
    AT_F_ANN_PUBLISH_TIME: str
    AT_F_ANN_EXPIRATION_TIME: str
    AT_F_ANN_APPROVED_BY: str

    # ── Tickets fields ────────────────────────────────────────────────
    AT_F_TICKET_ID: str
    AT_F_TICKET_TITLE: str
    AT_F_TICKET_DESCRIPTION: str
    AT_F_TICKET_STATUS: str
    AT_F_TICKET_ASSIGNEE: str
    AT_F_TICKET_ASSIGNED_BY: str
    AT_F_TICKET_SOURCE: str
    AT_F_TICKET_CREATED_DATE: str
    AT_F_TICKET_DUE_DATE: str
    AT_F_TICKET_LAST_UPDATED: str
    AT_F_TICKET_LAST_UPDATED_BY: str
    AT_F_TICKET_COMMENTS: str
    AT_F_TICKET_PARENT_LINK: str

    # ── Grant Application Resources fields ────────────────────────────
    AT_F_GAR_ID: str | None = None
    AT_F_GAR_DOCUMENT: str | None = None
    AT_F_GAR_DOCUMENT_URL: str | None = None
    AT_F_GAR_NOTES: str | None = None
    AT_F_GAR_ENTITY: str | None = None
    AT_F_GAR_TABS: str | None = None

    # ── Finance Links fields ──────────────────────────────────────────
    AT_F_FL_ID: str
    AT_F_FL_DOCUMENT: str
    AT_F_FL_DOCUMENT_URL: str

    # ── Office Spaces fields ──────────────────────────────────────────
    AT_F_OS_BRANCH: str
    AT_F_OS_ADDRESS: str
    AT_F_OS_DETAILS: str

    # ── Google Docs Tabs fields ───────────────────────────────────────
    AT_F_GDT_UI_PAGE: str

    # ── Meeting Cadence fields ────────────────────────────────────────
    AT_F_MC_MEETING_TITLE: str
    AT_F_MC_DESCRIPTION: str
    AT_F_MC_ATTACHMENT_URL: str

    # ── Useful Links fields ───────────────────────────────────────────
    AT_F_UL_DOCUMENT: str
    AT_F_UL_DOCUMENT_URL: str
    AT_F_UL_DESCRIPTION: str

    # ── HR & Benefits fields ──────────────────────────────────────────
    AT_F_HR_DOCUMENT: str
    AT_F_HR_DOCUMENT_URL: str
    AT_F_HR_DESCRIPTION: str

    # ── Onboarding fields ─────────────────────────────────────────────
    AT_F_OB_DOCUMENT: str
    AT_F_OB_DOCUMENT_URL: str
    AT_F_OB_NOTES: str

    # ── Onboarding Calls fields ───────────────────────────────────────
    AT_F_OBC_DATE: str
    AT_F_OBC_NOTES: str

    # ── Quick Links fields ────────────────────────────────────────────
    AT_F_QL_ID: str
    AT_F_QL_ANCHOR_TEXT: str
    AT_F_QL_URL: str
    AT_F_QL_EMAIL: str
    AT_F_QL_ACTION: str
    AT_F_QL_QUICK_ACTIONS_LINK: str

    # ── Quick Actions fields ──────────────────────────────────────────
    AT_F_QA_ID: str
    AT_F_QA_ACTION: str

    # ── Partnerships Links fields ─────────────────────────────────────
    AT_F_PL_ID: str
    AT_F_PL_TEXT: str
    AT_F_PL_LINK: str
    AT_F_PL_CATEGORY: str
    AT_F_PL_TYPE: str

    # ── Policy Links fields ───────────────────────────────────────────
    AT_F_POL_ID: str | None = None
    AT_F_POL_TEXT: str | None = None
    AT_F_POL_URL: str | None = None

    # ── Events Quick Links fields ───────────────────────────────────
    AT_F_EQL_ID: str | None = None
    AT_F_EQL_TITLE: str | None = None
    AT_F_EQL_ANCHOR_TEXT: str | None = None
    AT_F_EQL_TYPE: str | None = None
    AT_F_EQL_URL: str | None = None
    AT_F_EQL_EMAIL: str | None = None

    # ── Finance Quick Links fields ──────────────────────────────────
    AT_F_FQL_ID: str | None = None
    AT_F_FQL_ANCHOR_TEXT: str | None = None
    AT_F_FQL_URL: str | None = None
    AT_F_FQL_ENTITY: str | None = None
    AT_F_FQL_TABS: str | None = None

    # ── Comms Quick Links fields ────────────────────────────────────
    AT_F_CQL_ID: str
    AT_F_CQL_ANCHOR_TEXT: str
    AT_F_CQL_TYPE: str
    AT_F_CQL_URL: str
    AT_F_CQL_EMAIL: str

    # ── HR Quick Links fields ───────────────────────────────────────
    AT_F_HRQL_ID: str
    AT_F_HRQL_ANCHOR_TEXT: str
    AT_F_HRQL_TYPE: str
    AT_F_HRQL_URL: str
    AT_F_HRQL_EMAIL: str

    # ── RenPhil Due Diligence Links fields ────────────────────────
    AT_F_DDL_ID: str | None = None
    AT_F_DDL_ANCHOR_TEXT: str | None = None
    AT_F_DDL_URL: str | None = None
    AT_F_DDL_ENTITY: str | None = None
    AT_F_DDL_TABS: str | None = None

    # ── Board Member List fields ─────────────────────────────────────
    AT_F_BM_ID: str | None = None
    AT_F_BM_TITLE: str | None = None
    AT_F_BM_FULL_NAME: str | None = None
    AT_F_BM_ROLE: str | None = None
    AT_F_BM_ORGANIZATION: str | None = None
    AT_F_BM_CONTACT: str | None = None
    AT_F_BM_ENTITY: str | None = None
    AT_F_BM_TABS: str | None = None
    # ── Organization Info fields ────────────────────────────────
    AT_F_OI_ID: str | None = None
    AT_F_OI_TITLE: str | None = None
    AT_F_OI_CONTENT: str | None = None
    AT_F_OI_ENTITY: str | None = None
    AT_F_OI_TABS: str | None = None    # ── Onboarding Checklist fields ──────────────────────────────────
    AT_F_OC_MASTER_LIST_FUNDS_SUBPROGRAMS: str
    # ══════════════════════════════════════════════════════════════════
    # Slack webhook
    # ══════════════════════════════════════════════════════════════════
    # Signing secret used to verify Slack request signatures
    # (X-Slack-Signature + X-Slack-Request-Timestamp).
    SLACK_SIGNING_SECRET: str | None = None
    # Slack channel id where the /tickets slash command is allowed.
    SLACK_TICKETS_ALLOWED_CHANNEL_ID: str | None = None
    # Organization email domain (e.g. ``renphil.org``) used to construct
    # the ``assigned_by`` email from the Slack ``user_name`` field.
    ORG_DOMAIN: str | None = None

    # ══════════════════════════════════════════════════════════════════
    # Gemini (Google Generative AI)
    # ══════════════════════════════════════════════════════════════════
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ══════════════════════════════════════════════════════════════════
    # Cache (Upstash Redis over REST)
    # ══════════════════════════════════════════════════════════════════
    # When either URL or TOKEN is missing, the endpoint cache is disabled
    # and every GET goes straight to Airtable (no error is raised).
    UPSTASH_REDIS_REST_URL: str | None = None
    UPSTASH_REDIS_REST_TOKEN: str | None = None
    # Bumping this rotates the cache namespace and effectively wipes it.
    CACHE_VERSION: str = "v1"


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()  # type: ignore[call-arg]
