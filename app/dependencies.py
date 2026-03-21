"""
FastAPI dependency injection helpers.

Provides:
  - get_current_user  → authenticates the Bearer JWT and returns UserInfo
  - Service factories → instantiate services with settings
"""

from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.models.auth import UserInfo
from app.services.auth_service import AuthService
from app.services.dify_service import DifyService
from app.services.drive_service import DriveService

_bearer_scheme = HTTPBearer()

# ── Service singletons (simple module-level cache) ─────────────────────
_auth_service: AuthService | None = None
_drive_service: DriveService | None = None
_dify_service: DifyService | None = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService(get_settings())
    return _auth_service


def get_drive_service() -> DriveService:
    global _drive_service
    if _drive_service is None:
        _drive_service = DriveService(get_settings())
    return _drive_service


def get_dify_service() -> DifyService:
    global _dify_service
    if _dify_service is None:
        _dify_service = DifyService(get_settings())
    return _dify_service


# ── Current-user dependency ────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> UserInfo:
    """Extract and validate the JWT from the Authorization header."""
    return auth_service.decode_access_token(credentials.credentials)
