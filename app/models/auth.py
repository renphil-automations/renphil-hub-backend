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


class ScopedRole(BaseModel):
    """A role assignment with its scope and (when scoped) fund/program name."""

    role_name: str | None = None
    scope: str | None = Field(
        default=None,
        description="Scope of the role (e.g. 'Hub', 'Program', 'Fund').",
    )
    fund_or_program_name: str | None = Field(
        default=None,
        description=(
            "Name of the fund or program the role is scoped to. Null when "
            "the role's scope is 'Hub' (i.e. global)."
        ),
    )
    function: str | None = Field(
        default=None,
        description=(
            "Function value the role is scoped to. Populated only when "
            "the role's scope is 'Function'; null otherwise."
        ),
    )


class MeResponse(BaseModel):
    """Response payload of ``GET /auth/me``."""

    email: EmailStr
    name: str
    picture: str | None = None
    roles: list[str] = Field(default_factory=list)
    scoped_roles: list[ScopedRole] = Field(
        default_factory=list,
        description=(
            "Per-assignment role info from the Access Control table. "
            "Each entry carries the role name, its scope, and the "
            "fund/program it is scoped to (null when scope is 'Hub')."
        ),
    )
    is_hub_admin: bool = Field(
        default=False,
        description=(
            "The real is_hub_admin(db, current) answer (app/dependencies.py) "
            "— JWT `roles` OR a live `role_assignments` row — not just "
            "whether 'Hub Admin' is in `roles`. Lets the frontend repair a "
            "cached user object whose is_hub_admin has gone stale (e.g. an "
            "admin grant/revoke since the last login) without forcing a "
            "fresh login. See types/index.ts's User.is_hub_admin."
        ),
    )
