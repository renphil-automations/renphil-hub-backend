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
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]

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


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()  # type: ignore[call-arg]
