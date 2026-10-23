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
