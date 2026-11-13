"""Optional real PostgreSQL schema, migration and retry-race checks.

Only the explicitly named local disposable database is accepted. Each test
uses a separate generated schema; no existing tables are truncated/dropped.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db_v2.models.hub_feedback import HubFeedbackV2, BotFeedbackV2
from app.schemas.hub_feedback import HubFeedbackCreate
from app.services.hub_feedback_service import create_feedback
from scripts import create_hub_feedback_tables


@pytest.fixture
def database():
    value = os.getenv("FEEDBACK_TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set FEEDBACK_TEST_DATABASE_URL to the disposable local PostgreSQL database")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "hub_analytics_test":
        pytest.fail("Feedback PostgreSQL tests require the named disposable local database")
    base = create_engine(value)
    schema = "feedback_test_" + uuid4().hex
    with base.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # A real connection search path makes the production setup script's
    # unqualified table/inspection behavior run unchanged in our test schema.
    engine = create_engine(value, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with patch.object(create_hub_feedback_tables, "engine_v2", engine):
            create_hub_feedback_tables.main()
            create_hub_feedback_tables.main()
        yield engine, base, schema
    finally:
        engine.dispose()
        with base.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()


def test_feedback_schema_timezone_constraints_and_indexes(database):
    engine, base, schema = database
    inspector = inspect(base)
    for model in (HubFeedbackV2, BotFeedbackV2):
        columns = {column["name"]: column for column in inspector.get_columns(model.__tablename__, schema=schema)}
        assert columns["created_at"]["type"].timezone is True
        unique = {tuple(value["column_names"]) for value in inspector.get_unique_constraints(model.__tablename__, schema=schema)}
        assert ("actor_user_id", "submission_id") in unique
        assert ("environment", "airtable_record_id") in unique
        indexes = {value["name"] for value in inspector.get_indexes(model.__tablename__, schema=schema)}
        assert {index.name for index in model.__table__.indexes}.issubset(indexes)


def test_simultaneous_retries_store_one_postgres_row(database):
    engine, _, _ = database
    sessions = sessionmaker(bind=engine)
    body = HubFeedbackCreate(submission_id=uuid4(), scope="hub", message="Concurrent retry")
    resources = SimpleNamespace(validate=lambda _: None)
    def submit(_):
        with sessions() as db:
            row, created = create_feedback(db, body, user_id=1, email="test@example.org", environment="local", resources=resources)
            return row.public_id, created
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(6)))
    assert len({key for key, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with sessions() as db:
        assert len(db.scalars(select(HubFeedbackV2)).all()) == 1
