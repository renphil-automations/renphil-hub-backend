"""Durable, short-lived OAuth authorization-code records for Hub MCP."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from app.db_v2.database import BaseV2


class McpOAuthAuthorizationRequestV2(BaseV2):
    """Pending browser authorization; request ID itself is never persisted."""

    __tablename__ = "mcp_oauth_authorization_requests"

    id = Column(Integer, primary_key=True)
    request_hash = Column(String(64), nullable=False, unique=True, index=True)
    client_id = Column(String(255), nullable=False, index=True)
    redirect_uri = Column(Text, nullable=False)
    client_state = Column(Text, nullable=False)
    code_challenge = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)


class McpOAuthAuthorizationCodeV2(BaseV2):
    """One-time code; only its SHA-256 digest is stored."""

    __tablename__ = "mcp_oauth_authorization_codes"

    id = Column(Integer, primary_key=True)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    client_id = Column(String(255), nullable=False, index=True)
    redirect_uri = Column(Text, nullable=False)
    code_challenge = Column(String(128), nullable=False)
    user_email = Column(String(320), nullable=False, index=True)
    user_subject = Column(String(128), nullable=False)
    # Retained only while the one-time authorization code exists, so the
    # access token represents the same verified Hub login that approved it.
    user_roles_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
    __table_args__ = (
        Index("ix_mcp_oauth_authorization_codes_active", "client_id", "created_at"),
    )


class McpOAuthRefreshTokenV2(BaseV2):
    """Rotating refresh token; only its SHA-256 digest is stored.

    Every rotation adds a row to the same ``family_id`` and marks the
    presented row ``used_at``. Presenting a used or revoked row revokes the
    whole family (refresh-token reuse detection). Only written when
    ``MCP_OAUTH_REFRESH_TOKENS_ENABLED`` is true.
    """

    __tablename__ = "mcp_oauth_refresh_tokens"

    id = Column(Integer, primary_key=True)
    family_id = Column(String(64), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    client_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(320), nullable=False, index=True)
    user_subject = Column(String(128), nullable=False)
    # RFC 7638 thumbprint when the family is DPoP-bound; NULL for an
    # unbound (bearer-provider) family.
    jkt = Column(String(128), nullable=True)
    scope = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    family_expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
