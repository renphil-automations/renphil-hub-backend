"""
Custom exception classes used across the application.
Keeps HTTP concerns out of service / helper layers.
"""

from fastapi import HTTPException, status


class DomainNotAllowedError(HTTPException):
    """Raised when the authenticated email domain is not in the allow-list."""

    def __init__(self, email: str, allowed_domain: str) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied. The email '{email}' does not belong to the "
                f"'{allowed_domain}' domain."
            ),
        )


class UserNotAuthorizedError(HTTPException):
    """Raised when an email outside the org domain, and not pre-registered in
    the Access Control table, attempts to log in."""

    def __init__(self, email: str) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied. The email '{email}' is not authorized to "
                f"access this application."
            ),
        )


class UserRemovedError(HTTPException):
    """Raised at sign-in (OAuth callback, dev-login) when the email belongs
    to a soft-deleted `users` row, whatever its domain
    (plan_admin_soft_delete_user_2026-10-19.md §3.3)."""

    def __init__(self, email: str) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied. The account '{email}' was removed from "
                f"this application. Ask a Hub Admin to restore it."
            ),
        )


class GoogleOAuthError(HTTPException):
    """Raised on any failure during the Google OAuth flow."""

    def __init__(self, detail: str = "Google authentication failed.") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
        )


class GoogleDriveError(HTTPException):
    """Raised when interacting with Google Drive fails."""

    def __init__(self, detail: str = "Failed to fetch data from Google Drive.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )


class GoogleCalendarError(HTTPException):
    """Raised when interacting with the Google Calendar API fails."""

    def __init__(self, detail: str = "Failed to reach Google Calendar.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )


class DifyError(HTTPException):
    """Raised when the Dify.ai API call fails."""

    def __init__(self, detail: str = "Dify.ai request failed.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )


class AirtableError(HTTPException):
    """Raised when interacting with the Airtable API fails."""

    def __init__(self, detail: str = "Failed to fetch data from Airtable.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )
