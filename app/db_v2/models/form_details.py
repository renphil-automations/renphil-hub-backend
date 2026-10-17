"""The ``form_details`` table — one row per embedded-form component.

Holds the form-builder spec for a ``form`` component (1:1 via the unique
``component_id``). Deleted with its component by the database
(``ON DELETE CASCADE``).

No ORM ``relationship()`` — same rule as every other ``db_v2`` model.
Constraints are declared here so ``create_all`` in tests reproduces them.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)

from app.db_v2.database import BaseV2


class FormDetailsV2(BaseV2):
    __tablename__ = "form_details"

    id = Column(Integer, primary_key=True)
    component_id = Column(
        Integer,
        ForeignKey("components.id", ondelete="CASCADE"),
        nullable=False,
    )
    form_id = Column(Uuid, nullable=False)
    embed_url = Column(Text, nullable=False)
    # The form-builder PAT, needed server-side for runtime form-builder calls
    # (so stored retrievable, not hashed — same as the Airtable widget PAT).
    # Write-only: never returned by any endpoint.
    pat = Column(Text, nullable=False)
    requires_authentication = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("component_id", name="form_details_component_id_key"),
    )
