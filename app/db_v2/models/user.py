from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    or_,
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property

from app.db_v2.database import BaseV2


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_name(context) -> str:
    """A row must have a display name; fall back to its login email, the same
    rule scripts/migrate_merge_hub_users_into_users.py applied to logins that
    had none."""
    params = context.get_current_parameters()
    return params.get("work_email") or params.get("alias_email") or ""


# Partial: the 30-odd HR rows with no email at all must not collide on "".
_HAS_WORK_EMAIL = "work_email IS NOT NULL AND work_email <> ''"
_HAS_ALIAS_EMAIL = "alias_email IS NOT NULL AND alias_email <> ''"


class UserV2(BaseV2):
    """The ``users`` table (Postgres): the RenPhil Hub Airtable Users mirror
    AND, since the 2026-10 merge, the hub's sign-in identity (formerly the
    separate ``hub_users`` table).

    IDENTITY. ``id`` is the primary key, and it is the id
    ``role_assignments``, ``resource_grants`` and ``bookmarks`` point at —
    ``CurrentHubUser.hub_user_id`` is a ``users.id``. ``name`` used to be the
    primary key (mirroring the Airtable 'Name' field) and is no longer unique:
    a login-only row can share a staff member's name.

    LOGIN EMAIL. ``work_email``, falling back to ``alias_email`` (owner
    decision, 2026-10-09). Both are unique case-insensitively (partial indexes
    below), and the merge migration verified no address is one row's work
    email and another's alias. Look people up with ``matches_email`` — it
    checks both columns — and use the ``email`` hybrid only to display or sort.

    ``has_signed_in`` (owner decision, 2026-10-09; renamed from ``is_active``
    2026-10-18): HR rows and admin-added people start False, and the first
    sign-in flips it (``app.dependencies._ensure_hub_user``). It never meant
    "deactivated".

    Rows are either HR rows (the Airtable mirror, refreshed by a sync this
    repo does not contain) or login-only rows (test personas, external
    people an admin added, anyone who signed in with no HR row) whose HR
    columns are all empty.

    Airtable checkbox fields map to real ``Boolean`` columns here, and the two
    Airtable multi-select fields ('Employment Type', 'Tech Stack Selections')
    are stored as ``, ``-joined text.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, default=_default_name)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    employment_type = Column(Text, nullable=True)
    for_website = Column(String, nullable=True)
    status = Column(String, nullable=True)
    is_fellow = Column(Boolean, nullable=True)
    department = Column(String, nullable=True)
    program = Column(Text, nullable=True)
    start_date = Column(Date, nullable=True)
    work_email = Column(String, nullable=True)
    personal_email = Column(String, nullable=True)
    position = Column(Text, nullable=True)
    dob = Column(Date, nullable=True)
    birthday_shoutout_opt_out = Column(Boolean, nullable=True)
    post_on_website = Column(Boolean, nullable=True)
    headshot = Column(Text, nullable=True)
    office_location = Column(String, nullable=True)
    home_address = Column(Text, nullable=True)
    bio = Column(Text, nullable=True)
    scope_of_work = Column(Text, nullable=True)
    new_headshot_bio_to_website = Column(
        "New Headshot/Bio to Website", String, nullable=True
    )
    end_date = Column("EndDate", Date, nullable=True)
    manager_tech_stack_selections = Column(Text, nullable=True)
    add_to_website_date = Column(DateTime(timezone=True), nullable=True)
    alias_email = Column(String, nullable=True)

    has_signed_in = Column(Boolean, nullable=False, default=False, server_default=false())
    # Soft delete (AI Docs/plan_admin_soft_delete_user_2026-10-19.md): the row
    # and everything pointing at it are KEPT; `not_deleted()` is the filter
    # every "count / list / audience" query uses. `deleted_by` is the deleting
    # admin's email, a snapshot (no FK), like `granted_by_email`.
    is_deleted = Column(Boolean, nullable=False, default=False, server_default=false())
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(String(320), nullable=True)
    # The Airtable Users "Record ID" COLUMN (never the API id), set by the
    # Airtable sync (AI Docs/plan_airtable_sync_v2_2026-10-21.md). NULL on
    # login-only rows and on rows the sync has not linked yet.
    airtable_record_id = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "uq_users_work_email_lower",
            func.lower(work_email),
            unique=True,
            postgresql_where=text(_HAS_WORK_EMAIL),
            sqlite_where=text(_HAS_WORK_EMAIL),
        ),
        Index(
            "uq_users_alias_email_lower",
            func.lower(alias_email),
            unique=True,
            postgresql_where=text(_HAS_ALIAS_EMAIL),
            sqlite_where=text(_HAS_ALIAS_EMAIL),
        ),
        Index(
            "uq_users_airtable_record_id",
            "airtable_record_id",
            unique=True,
            postgresql_where=text("airtable_record_id IS NOT NULL"),
            sqlite_where=text("airtable_record_id IS NOT NULL"),
        ),
    )

    @hybrid_property
    def email(self) -> str | None:
        """The login email, normalized: work_email, else alias_email. For
        display and ordering — to FIND someone by an email use
        ``matches_email``, which also matches an alias behind a work email."""
        return (self.work_email or "").strip().lower() or (self.alias_email or "").strip().lower() or None

    @email.inplace.setter
    def _email_setter(self, value: str | None) -> None:
        """Setting the login email sets ``work_email`` — what sign-in
        provisioning and the admin "Add user" create."""
        self.work_email = value

    @email.inplace.expression
    @classmethod
    def _email_expression(cls):
        return func.lower(func.coalesce(func.nullif(cls.work_email, ""), cls.alias_email))

    @classmethod
    def matches_email(cls, email: str):
        """Filter clause: this row signs in as ``email`` (already normalized)."""
        return or_(func.lower(cls.work_email) == email, func.lower(cls.alias_email) == email)

    @classmethod
    def not_deleted(cls):
        """Filter clause: the row is not soft-deleted. Use it wherever a
        user's rows would COUNT toward access, an audience, the admin set, a
        picker or a roster."""
        return cls.is_deleted.is_(False)

    @classmethod
    def matches_any_email(cls, emails):
        """Filter clause: this row signs in as any of ``emails`` (normalized)."""
        emails = list(emails)
        return or_(func.lower(cls.work_email).in_(emails), func.lower(cls.alias_email).in_(emails))
