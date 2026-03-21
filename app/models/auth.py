"""Pydantic schemas for authentication / authorization."""

from pydantic import BaseModel, EmailStr


class TokenResponse(BaseModel):
    """Returned to the frontend after successful Google OAuth."""
    access_token: str
    token_type: str = "bearer"
    email: str
    name: str
    picture: str | None = None


class UserInfo(BaseModel):
    """Decoded JWT payload / current user representation."""
    email: EmailStr
    name: str
    picture: str | None = None
