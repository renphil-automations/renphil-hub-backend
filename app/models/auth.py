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
