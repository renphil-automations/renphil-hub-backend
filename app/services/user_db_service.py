"""User read/update service backed by the Postgres ``users`` table.

Replaces the Airtable-backed ``get_user_by_work_email`` /
``update_user_by_work_email`` methods on ``AirtableService`` as the source of
truth for the ``/users`` endpoints. The public shape (``UserRecord`` /
``UserUpdate``) is unchanged so the API contract is preserved; only storage
moves to Postgres.

Airtable multi-select fields ('Employment Type', 'Tech Stack Selections') are
stored as ``, ``-joined text in Postgres and split back into lists here.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db_v2.models.user import UserV2
from app.models.airtable import UserRecord, UserUpdate

logger = logging.getLogger(__name__)

_LIST_SEP = ", "

# Pydantic field name -> ORM attribute on UserV2 (differs only where the
# Postgres column name is not a valid identifier or was renamed).
_COLUMN_BY_FIELD: dict[str, str] = {
    "name": "name",
    "first_name": "first_name",
    "last_name": "last_name",
    "employment_type": "employment_type",
    "for_website": "for_website",
    "status": "status",
    "is_fellow": "is_fellow",
    "department": "department",
    "program": "program",
    "start_date": "start_date",
    "work_email": "work_email",
    "personal_email": "personal_email",
    "position": "position",
    "dob": "dob",
    "birthday_shoutout_opt_out": "birthday_shoutout_opt_out",
    "post_on_website": "post_on_website",
    "headshot": "headshot",
    "office_location": "office_location",
    "home_address": "home_address",
    "bio": "bio",
    "scope_of_work": "scope_of_work",
    "new_headshot_bio_to_website": "new_headshot_bio_to_website",
    "end_date": "end_date",
    "tech_stack_selections": "manager_tech_stack_selections",
    "add_to_website_date": "add_to_website_date",
}

# Multi-select fields stored as ", "-joined text.
_LIST_FIELDS = {"employment_type", "tech_stack_selections"}

# Date-only fields sent as ISO ``YYYY-MM-DD`` strings.
_DATE_FIELDS = {"start_date", "dob", "end_date"}


def _split_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part for part in value.split(_LIST_SEP) if part]


def _join_list(value: list[str] | None) -> str | None:
    if not value:
        return None
    return _LIST_SEP.join(value)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _to_record(row: UserV2) -> UserRecord:
    return UserRecord.model_validate(
        {
            "id": row.name,
            "name": row.name,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "employment_type": _split_list(row.employment_type),
            "for_website": row.for_website,
            "status": row.status,
            "is_fellow": row.is_fellow,
            "department": row.department,
            "program": row.program,
            "start_date": _iso(row.start_date),
            "work_email": row.work_email,
            "personal_email": row.personal_email,
            "position": row.position,
            "dob": _iso(row.dob),
            "birthday_shoutout_opt_out": row.birthday_shoutout_opt_out,
            "post_on_website": row.post_on_website,
            "headshot": row.headshot,
            "office_location": row.office_location,
            "home_address": row.home_address,
            "bio": row.bio,
            "scope_of_work": row.scope_of_work,
            "new_headshot_bio_to_website": row.new_headshot_bio_to_website,
            "end_date": _iso(row.end_date),
            "tech_stack_selections": _split_list(
                row.manager_tech_stack_selections
            ),
            "add_to_website_date": row.add_to_website_date,
        }
    )


def _find_user_by_work_email(db: Session, work_email: str) -> UserV2 | None:
    """Find a user row by exact (case-insensitive) Work Email."""
    normalized = (work_email or "").strip().lower()
    if not normalized:
        return None
    return (
        db.query(UserV2)
        .filter(func.lower(UserV2.work_email) == normalized)
        .first()
    )


def get_user_by_work_email(db: Session, work_email: str) -> UserRecord:
    """Return the user record matching the given Work Email."""
    row = _find_user_by_work_email(db, work_email)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with Work Email '{work_email}' not found.",
        )
    return _to_record(row)


def _coerce_value(field: str, value: Any) -> Any:
    if field in _LIST_FIELDS:
        return _join_list(value)
    if field in _DATE_FIELDS:
        return date.fromisoformat(value) if value else None
    return value


def update_user_by_work_email(
    db: Session, work_email: str, payload: UserUpdate
) -> UserRecord:
    """Update the user row identified by Work Email with the provided fields."""
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided to update.",
        )

    row = _find_user_by_work_email(db, work_email)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with Work Email '{work_email}' not found.",
        )

    for field, value in data.items():
        column = _COLUMN_BY_FIELD[field]
        setattr(row, column, _coerce_value(field, value))

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Postgres update user failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error updating user: {exc}",
        ) from exc

    db.refresh(row)
    return _to_record(row)
