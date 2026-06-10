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
    ALLOWED_ORIGINS: list[str] = ["http://localhost:5000", "http://localhost:3000", "https://renphil-hub.web.app", "https://renphil-hub.firebaseapp.com"]

    # ── Google OAuth ───────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/callback"
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
    # RenPhil Hub base
    ADMINS_TABLE: str
    ANNOUNCEMENTS_TABLE: str
    TICKETS_TABLE: str
    PARTNERSHIPS_FUNDRAISING_TABLE: str
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
    GENERAL_FUNDRAISING_RESOURCES_TABLE: str

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

    # ── Partnerships Fundraising fields ───────────────────────────────
    AT_F_PF_ID: str
    AT_F_PF_DOCUMENT: str
    AT_F_PF_DOCUMENT_URL: str
    AT_F_PF_NOTES: str

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
    AT_F_QA_ACTION: str

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


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()  # type: ignore[call-arg]
