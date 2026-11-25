"""Hub MCP OAuth: rotating refresh tokens (``mcp_oauth_refresh_tokens``).

Additive and idempotent: creates the table and its indexes only when they
are missing, so it is safe on a database prepared by
``scripts/sql/mcp_oauth_create.sql``. Only SHA-256 digests of refresh tokens
are stored. The table is read and written only while
``MCP_OAUTH_REFRESH_TOKENS_ENABLED`` is true; apply this revision before
enabling that setting.

Revision ID: 0002_mcp_oauth_refresh_tokens
Revises: 0001_mcp_oauth
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002_mcp_oauth_refresh_tokens"
down_revision = "0001_mcp_oauth"
branch_labels = None
depends_on = None

REFRESH_TABLE = "mcp_oauth_refresh_tokens"

REFRESH_INDEXES: tuple[tuple[str, list[str], bool], ...] = (
    ("ix_mcp_oauth_refresh_tokens_token_hash", ["token_hash"], True),
    ("ix_mcp_oauth_refresh_tokens_family_id", ["family_id"], False),
    ("ix_mcp_oauth_refresh_tokens_client_id", ["client_id"], False),
    ("ix_mcp_oauth_refresh_tokens_user_email", ["user_email"], False),
    ("ix_mcp_oauth_refresh_tokens_expires_at", ["expires_at"], False),
)


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(REFRESH_TABLE):
        op.create_table(
            REFRESH_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("family_id", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("client_id", sa.String(length=255), nullable=False),
            sa.Column("user_email", sa.String(length=320), nullable=False),
            sa.Column("user_subject", sa.String(length=128), nullable=False),
            sa.Column("jkt", sa.String(length=128), nullable=True),
            sa.Column("scope", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("family_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
    present = {index["name"] for index in inspect(op.get_bind()).get_indexes(REFRESH_TABLE) if index.get("name")}
    for name, columns, unique in REFRESH_INDEXES:
        if name not in present:
            op.create_index(name, REFRESH_TABLE, columns, unique=unique)


def downgrade() -> None:
    # Dropping the table invalidates every refresh token; clients re-authorize.
    if inspect(op.get_bind()).has_table(REFRESH_TABLE):
        op.drop_table(REFRESH_TABLE)
