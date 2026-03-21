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


class DifyError(HTTPException):
    """Raised when the Dify.ai API call fails."""

    def __init__(self, detail: str = "Dify.ai request failed.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )
