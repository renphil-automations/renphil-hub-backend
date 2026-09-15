from sqlalchemy import Boolean, Column, Date, DateTime, String, Text

from app.db_v2.database import BaseV2


class UserV2(BaseV2):
    """The ``users`` table (Postgres) that replaces the RenPhil Hub Airtable
    Users table as the source of truth for the ``/users`` read/update
    endpoints.

    ``name`` is the primary key (mirrors the Airtable 'Name' field). Airtable
    checkbox fields map to real ``Boolean`` columns here, and the two Airtable
    multi-select fields ('Employment Type', 'Tech Stack Selections') are stored
    as ``, ``-joined text.
    """

    __tablename__ = "users"

    name = Column(String, primary_key=True)
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
