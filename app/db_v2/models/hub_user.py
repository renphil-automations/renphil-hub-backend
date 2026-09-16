"""Access-control identity (plan_access_control_schema_2026-08-22.md §3.1, §7).

Deliberately NOT the existing ``users`` table. That one is the Airtable HR
mirror — primary key ``name``, refreshed by a sync this repo does not
contain. Hanging ``role_assignments`` off it with ON DELETE CASCADE would
mean one bad sync, or somebody simply dropping off the HR roster, silently
revokes their access with no record of what was lost. Authorization must not
inherit an external feed's lifecycle.

``thread.py``'s ``author_email`` comment already documents the other half of
the same problem: the signed-in population is "strictly wider than that
roster", since login gates on email domain only. A row here must therefore
exist for everyone who can log in, which means auto-provisioning on first
login — not an HR import.

No relationship() here, matching every other model in db_v2/ — traversal is
always a plain query.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.db_v2.database import BaseV2


class HubUserV2(BaseV2):
    """One person who can hold role assignments."""

    __tablename__ = "hub_users"

    id = Column(Integer, primary_key=True, index=True)

    # The join key from an authenticated request: the JWT's `sub` is an
    # email, so this is the only thing that connects a request to a row.
    # Lowercased and stripped on write by the service layer — the same
    # normalization every other email comparison in this codebase applies.
    # Not citext: this codebase does not use the extension anywhere, so
    # normalization is an application invariant, not a database one.
    email = Column(String(320), nullable=False, unique=True, index=True)

    name = Column(String(255), nullable=True)

    # Switch someone off without deleting the row. Deleting the row cascades
    # their assignments away permanently AND nulls granted_by_user_id on
    # every grant they ever made (the granted_by_email snapshot on those
    # rows is what survives) — so deactivation, not deletion, is the normal
    # offboarding path.
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)
