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
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "https://renphil-hub.web.app", "https://renphil-hub.firebaseapp.com"]

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

    # ── Airtable ───────────────────────────────────────────────────────
    AIRTABLE_API_KEY: str

    # Fundraising base
    AIRTABLE_FUNDRAISING_BASE_ID: str = "appgeVEhaSHWv0jQY"
    TOTAL_MOVED_AND_DEPLOYED_TABLE_NAME: str = "Total Moved and Deployed"

    # Fund & Program Tracker base
    AIRTABLE_FUND_PROGRAM_BASE_ID: str = "appP9ziO5nV1LY7eS"
    MASTER_LIST_FUNDS_AND_SUBPROGRAMS_TABLE: str = "tblj34ByiDh40US4x"
    GLOSSARY_TABLE: str = "Glossary"
    ORG_FRIENDS_TABLE: str = "Org Friends"
    FUNDERS_TABLE: str = "Funders"
    FUNDS_AND_PROGRAMS_MONTHLY_CHECKIN_TABLE: str = (
        "Funds & Programs Monthly Check-In"
    )
    CHECKIN_REPORTING_PERIODS_TABLE: str = "Check-In Reporting Periods"
    DOC_TITLES_TABLE: str = "Doc Titles"
    SHAREABLE_DOCS_TABLE: str = "Shareable Docs"
    CLUSTERS_TABLE: str = "Clusters"

    # RenPhil Hub base (admins, etc.)
    RENPHIL_HUB_BASE_ID: str = "appSh6OwO3ZMAkuVE"
    ADMINS_TABLE: str = "Admins"
    ADMINS_EMAIL_FIELD: str = "Email"
    ANNOUNCEMENTS_TABLE: str = "tbl6UrftFbn7EDSC6"
    TICKETS_TABLE: str = "Tickets"
    PARTNERSHIPS_FUNDRAISING_TABLE: str = "Partnerships Fundraising"
    FINANCE_LINKS_TABLE: str = "Finance Links"
    GOOGLE_DOCS_TABS_TABLE: str = "Google Docs Tabs"
    OFFICE_SPACES_TABLE: str = "tblcWUSSuY2ATDdce"

    # ── Slack webhook ─────────────────────────────────────────────────
    # Signing secret used to verify Slack request signatures
    # (X-Slack-Signature + X-Slack-Request-Timestamp).
    SLACK_SIGNING_SECRET: str | None = None
    # Slack channel id where the /tickets slash command is allowed.
    SLACK_TICKETS_ALLOWED_CHANNEL_ID: str | None = None
    # Organization email domain (e.g. ``renphil.org``) used to construct
    # the ``assigned_by`` email from the Slack ``user_name`` field.
    ORG_DOMAIN: str | None = None
    # ── Gemini (Google Generative AI) ──────────────────────────────────
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # Access-Control tables (defaults to the RenPhil Hub base; override per-env if needed)
    ACCESS_CONTROL_TABLE: str = "Access Control"
    ACCESS_CONTROL_USER_EMAIL_FIELD: str = "User Email"
    ACCESS_CONTROL_ROLES_FIELD: str = "Roles"
    ACCESS_CONTROL_PERMISSIONS_FIELD: str = "Permissions"
    ACCESS_CONTROL_SCOPE_FIELD: str = "Scope"
    ACCESS_CONTROL_FUND_OR_PROGRAM_NAME_FIELD: str = "Fund or Program Name"
    ACCESS_CONTROL_ROLE_NAME_LOOKUP_FIELD: str = "Role Name"
    ACCESS_CONTROL_PERMISSION_NAME_LOOKUP_FIELD: str = "Permission Name"
    ACCESS_CONTROL_PERMISSION_DESCRIPTION_LOOKUP_FIELD: str = "Permission Description"

    TEAMS_TABLE: str = "tbl537cgZO1xCBOQs"
    TEAMS_WORK_EMAIL_FIELD: str = "Work Email"
    TEAMS_NAME_FIELD: str = "Name"

    # Users table (RenPhil Hub base) — directory of all hub users.
    USERS_TABLE: str = "Users"
    USERS_WORK_EMAIL_FIELD: str = "Work Email"

    ROLES_TABLE: str = "tblBRs7WWDokHyKCy"
    ROLES_NAME_FIELD: str = "Role Name"
    ROLES_PERMISSIONS_FIELD: str = "Permissions"

    PERMISSIONS_TABLE: str = "tblprRqGJQGLCN5fq"
    PERMISSIONS_NAME_FIELD: str = "Permission Name"
    PERMISSIONS_DESCRIPTION_FIELD: str = "Description"


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()  # type: ignore[call-arg]
