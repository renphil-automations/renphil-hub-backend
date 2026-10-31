"""Pydantic schemas for authentication / authorization."""

from pydantic import BaseModel, EmailStr, Field


class TokenResponse(BaseModel):
    """Returned to the frontend after successful Google OAuth."""
    access_token: str
    token_type: str = "bearer"
    email: str
    name: str
    picture: str | None = None
    roles: list[str] = Field(default_factory=list)


class UserInfo(BaseModel):
    """Decoded JWT payload / current user representation."""
    email: EmailStr
    name: str
    picture: str | None = None
    roles: list[str] = Field(default_factory=list)


class MeResponse(BaseModel):
    """Response payload of ``GET /auth/me``."""

    email: EmailStr
    name: str
    picture: str | None = None
    roles: list[str] = Field(default_factory=list)
    is_hub_admin: bool = Field(
        default=False,
        description=(
            "The real is_hub_admin(db, current) answer (app/dependencies.py) "
            "— BOOTSTRAP_ADMIN_EMAILS OR a live `role_assignments` row — not "
            "whether 'Hub Admin' is in `roles`. Lets the frontend repair a "
            "cached user object whose is_hub_admin has gone stale (e.g. an "
            "admin grant/revoke since the last login) without forcing a "
            "fresh login. See types/index.ts's User.is_hub_admin."
        ),
    )
