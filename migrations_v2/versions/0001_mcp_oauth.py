"""Hub MCP OAuth: durable authorization-request and one-time code tables.

Creates ``mcp_oauth_authorization_requests`` and
``mcp_oauth_authorization_codes`` with their indexes, and adds the
``user_roles_json`` column to code tables created by an earlier manual
(pre-Alembic) sandbox setup that predates that column.

Every step is idempotent, so the revision is safe on:

* a new database;
* a sandbox database prepared by an earlier manual MCP OAuth setup;
* a database where only the tables, but not the role column, exist.

Lifecycle: rows are short-lived (``MCP_OAUTH_CODE_TTL_SECONDS``, default
120 s). Requests are deleted when converted into a code; codes are deleted
atomically when exchanged; and every authorization/exchange purges expired
rows (``PersistentMcpOAuthService._purge``). No separate cleanup job is needed.

This repository did not previously manage the V2 schema with Alembic, so this
is the initial migration chain and it creates only the MCP OAuth tables. A
future migration must extend this revision rather than introducing a parallel
head.

Revision ID: 0001_mcp_oauth
Revises:
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0001_mcp_oauth"
down_revision = None
branch_labels = None
depends_on = None

REQUESTS_TABLE = "mcp_oauth_authorization_requests"
CODES_TABLE = "mcp_oauth_authorization_codes"

REQUEST_INDEXES: tuple[tuple[str, list[str], bool], ...] = (
    ("ix_mcp_oauth_authorization_requests_request_hash", ["request_hash"], True),
    ("ix_mcp_oauth_authorization_requests_client_id", ["client_id"], False),
    ("ix_mcp_oauth_authorization_requests_created_at", ["created_at"], False),
)
CODE_INDEXES: tuple[tuple[str, list[str], bool], ...] = (
    ("ix_mcp_oauth_authorization_codes_code_hash", ["code_hash"], True),
    ("ix_mcp_oauth_authorization_codes_client_id", ["client_id"], False),
    ("ix_mcp_oauth_authorization_codes_user_email", ["user_email"], False),
    ("ix_mcp_oauth_authorization_codes_created_at", ["created_at"], False),
    ("ix_mcp_oauth_authorization_codes_active", ["client_id", "created_at"], False),
)


def _inspector():
    return inspect(op.get_bind())


def _existing_indexes(table: str) -> set[str]:
    return {index["name"] for index in _inspector().get_indexes(table) if index.get("name")}


def _existing_columns(table: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table)}


def _ensure_indexes(table: str, indexes: tuple[tuple[str, list[str], bool], ...]) -> None:
    present = _existing_indexes(table)
    for name, columns, unique in indexes:
        if name not in present:
            op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    inspector = _inspector()

    if not inspector.has_table(REQUESTS_TABLE):
        op.create_table(
            REQUESTS_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("client_id", sa.String(length=255), nullable=False),
            sa.Column("redirect_uri", sa.Text(), nullable=False),
            sa.Column("client_state", sa.Text(), nullable=False),
            sa.Column("code_challenge", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _ensure_indexes(REQUESTS_TABLE, REQUEST_INDEXES)

    if not inspector.has_table(CODES_TABLE):
        op.create_table(
            CODES_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("client_id", sa.String(length=255), nullable=False),
            sa.Column("redirect_uri", sa.Text(), nullable=False),
            sa.Column("code_challenge", sa.String(length=128), nullable=False),
            sa.Column("user_email", sa.String(length=320), nullable=False),
            sa.Column("user_subject", sa.String(length=128), nullable=False),
            sa.Column("user_roles_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    elif "user_roles_json" not in _existing_columns(CODES_TABLE):
        # Tables created by an earlier manual sandbox setup before the
        # column existed.
        op.add_column(CODES_TABLE, sa.Column("user_roles_json", sa.Text(), nullable=False, server_default="[]"))
    _ensure_indexes(CODES_TABLE, CODE_INDEXES)


def downgrade() -> None:
    # Both tables hold only short-lived, hashed OAuth state; dropping them
    # invalidates in-flight authorizations (clients simply re-authorize).
    inspector = _inspector()
    if inspector.has_table(CODES_TABLE):
        op.drop_table(CODES_TABLE)
    if inspector.has_table(REQUESTS_TABLE):
        op.drop_table(REQUESTS_TABLE)
