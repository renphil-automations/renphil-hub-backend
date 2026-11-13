from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.routers import product_analytics_archive_trigger as trigger
from app.db_v2.models.product_analytics import HubProductEventV2, HubProductEngagementV2, HubProductAnalyticsArchiveV2, HubProductAnalyticsArchiveIdentityV2
from app.db_v2.database import BaseV2
from app.services import product_analytics_archive as archive
from scripts import trigger_product_analytics_archive as scheduler

TOKEN = "t" * 32
PATH = "/internal/product-analytics/archive/run"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(trigger.router)
    return TestClient(app)


def test_authentication_fails_closed_without_config_or_valid_token(client):
    for expected, provided, status in (("", TOKEN, 503), ("short", TOKEN, 503), (TOKEN, "", 401), (TOKEN, "wrong", 401)):
        with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=expected)), patch.object(trigger.archive_job, "run") as run:
            response = client.post(PATH, headers={"X-Analytics-Archive-Token": provided})
        assert response.status_code == status
        run.assert_not_called()


def test_main_app_imports_and_registers_the_internal_endpoint():
    from app.main import app
    with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN="")):
        assert TestClient(app).post(PATH).status_code == 503


def test_authorized_request_runs_only_fixed_bounded_job(client):
    with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=TOKEN)), patch.object(trigger.archive_job, "run", return_value=0) as run:
        response = client.post(PATH + "?batch_size=50000", json={"environment": "production"}, headers={"X-Analytics-Archive-Token": TOKEN})
    assert response.status_code == 200
    assert response.json() == {"status": "completed", "batch_size": 100, "max_batches_per_table": 1,
                               "has_more": False, "cleanup": {}, "archived": {}}
    assert run.call_args.args[0] == ["--batch-size", "100", "--max-batches", "1"]


@pytest.mark.parametrize(("result", "status"), [(1, 503), (2, 503), (3, 409), (99, 503)])
def test_failures_and_lock_conflict_are_not_reported_as_success(client, result, status):
    with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=TOKEN)), patch.object(trigger.archive_job, "run", return_value=result):
        assert client.post(PATH, headers={"X-Analytics-Archive-Token": TOKEN}).status_code == status


def test_unexpected_exception_does_not_return_internal_details(client):
    with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=TOKEN)), patch.object(trigger.archive_job, "run", side_effect=RuntimeError("private detail")):
        response = client.post(PATH, headers={"X-Analytics-Archive-Token": TOKEN})
    assert response.status_code == 503
    assert "private detail" not in response.text


def test_disabled_retention_is_reported_separately(client):
    def disabled(args, *, out, progress_out):
        out("archive disabled: PRODUCT_ANALYTICS_RETENTION_DAYS is off. Nothing was deleted or uploaded.")
        return 0
    with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=TOKEN)), patch.object(trigger.archive_job, "run", side_effect=disabled):
        response = client.post(PATH, headers={"X-Analytics-Archive-Token": TOKEN})
    assert response.json()["status"] == "disabled"


def test_scheduler_sends_token_only_to_configured_https_endpoint():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "completed", "has_more": False})
    env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": "https://example.invalid" + PATH,
           "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert scheduler.run(env=env, client=client, out=lambda _: None) == 0
    assert len(requests) == 1
    assert requests[0].headers["X-Analytics-Archive-Token"] == TOKEN


@pytest.mark.parametrize("url", ["http://example.invalid" + PATH, "https://user:secret@example.invalid" + PATH,
                                "https://example.invalid" + PATH + "?x=1", "https://example.invalid/other"])
def test_scheduler_refuses_invalid_url_before_network(url):
    env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": url, "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN}
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        assert scheduler.run(env=env, client=client, out=lambda _: None) == 2


@pytest.mark.parametrize("status", [301, 401, 409, 503])
def test_scheduler_reports_non_success_without_following_redirects(status):
    env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": "https://example.invalid" + PATH,
           "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={"Location": "https://elsewhere.invalid"}))) as client:
        assert scheduler.run(env=env, client=client, out=lambda _: None) == 1


def test_scheduler_rejects_invalid_success_response_and_does_not_leak_exception():
    env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": "https://example.invalid" + PATH,
           "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="unexpected"))) as client:
        assert scheduler.run(env=env, client=client, out=lambda _: None) == 1
    lines = []
    def failure(request):
        raise httpx.ConnectError("private connection detail", request=request)
    with httpx.Client(transport=httpx.MockTransport(failure)) as client:
        assert scheduler.run(env=env, client=client, out=lines.append) == 1
    assert "private connection detail" not in " ".join(lines)


def test_authorized_http_request_runs_archive_end_to_end(client):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (HubProductEventV2, HubProductEngagementV2, HubProductAnalyticsArchiveV2, HubProductAnalyticsArchiveIdentityV2):
        model.__table__.create(bind=engine)
    with engine.begin() as connection:
        for table in ("nav_tabs", "tabs", "gridstacks"):
            connection.execute(text(f"CREATE TABLE {table} (document_id VARCHAR(255))"))
            connection.execute(text(f"INSERT INTO {table} VALUES ('test-page')"))
        connection.execute(text("CREATE TABLE components (link VARCHAR(255))"))
        connection.execute(text("INSERT INTO components VALUES ('test-component')"))
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
            action="select", trigger="click", hub_user_id=1, actor_email="synthetic@example.invalid", environment="local",
            page_route="/:nav_tab", target_id="test-page", page_id="test-page", properties={},
            occurred_at=now-timedelta(days=400), received_at=now-timedelta(days=400)))
        db.commit()
    store = archive.MemoryArchiveStore()
    real_run = trigger.archive_job.run
    def run_in_test_database(args, *, out, progress_out):
        return real_run(args, engine=engine, store=store, raw_retention="365", environment="local", now=now, out=out, progress_out=progress_out)
    try:
        with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=TOKEN)), patch.object(trigger.archive_job, "run", side_effect=run_in_test_database):
            def call_endpoint(request):
                response = client.post(PATH, headers={"X-Analytics-Archive-Token": request.headers["X-Analytics-Archive-Token"]})
                return httpx.Response(response.status_code, json=response.json())
            env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": "https://example.invalid" + PATH,
                   "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN}
            with httpx.Client(transport=httpx.MockTransport(call_endpoint)) as http_client:
                assert scheduler.run(env=env, client=http_client, out=lambda _: None) == 0
        with Session(engine) as db:
            assert db.execute(select(HubProductEventV2)).scalars().all() == []
            records = db.execute(select(HubProductAnalyticsArchiveV2)).scalars().all()
            assert len(records) == 1
            manifest, data = archive.archive_records(store.get(records[0].key))
            assert manifest["environment"] == "local"
            assert data["events"][0]["actor_email"] == "synthetic@example.invalid"
    finally:
        engine.dispose()


def test_scheduler_continues_bounded_batches_and_reports_a_backlog():
    env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": "https://example.invalid" + PATH,
           "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN, "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_MAX_REQUESTS": "2"}
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "has_more": len(calls) == 1})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert scheduler.run(env=env, client=client, out=lambda _: None) == 0
    assert len(calls) == 2
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "completed", "has_more": True}))) as client:
        assert scheduler.run(env=env, client=client, out=lambda _: None) == 1


@pytest.mark.skipif(not os.environ.get("PRODUCT_ANALYTICS_TEST_DATABASE_URL"), reason="requires disposable PostgreSQL")
def test_scheduler_endpoint_archive_and_deleted_cleanup_with_postgres(client):
    url = os.environ["PRODUCT_ANALYTICS_TEST_DATABASE_URL"]
    if any(word in url.lower() for word in ("prod", "neon.tech", "sandbox")):
        pytest.skip("refusing a database URL that looks shared")
    schema = "archive_trigger_" + uuid.uuid4().hex
    base = create_engine(url)
    engine = None
    with base.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
        with engine.begin() as connection:
            for table in ("nav_tabs", "tabs", "gridstacks"):
                connection.execute(text(f"CREATE TABLE {table} (id serial primary key, document_id varchar(255))"))
                connection.execute(text(f"INSERT INTO {table} (document_id) VALUES ('live-page')"))
            connection.execute(text("CREATE TABLE components (id serial primary key, link varchar(255))"))
            connection.execute(text("INSERT INTO components (link) VALUES ('live-component')"))
            BaseV2.metadata.create_all(bind=connection, tables=[HubProductEventV2.__table__, HubProductEngagementV2.__table__,
                HubProductAnalyticsArchiveV2.__table__, HubProductAnalyticsArchiveIdentityV2.__table__])
        now = datetime.now(timezone.utc)
        with Session(engine) as db:
            for target in ("live-page", "deleted-page"):
                db.add(HubProductEventV2(event_id=str(uuid.uuid4()), event_name="root_tab.selected", component_type="root_tab",
                    action="select", trigger="click", hub_user_id=1, actor_email="test@example.invalid", environment="local",
                    page_route="/:nav_tab", target_id=target, page_id=target, properties={},
                    occurred_at=now-timedelta(days=400), received_at=now-timedelta(days=400)))
            db.commit()
        store = archive.MemoryArchiveStore()
        real_run = trigger.archive_job.run
        def run_in_test_database(args, *, out, progress_out):
            return real_run(args, engine=engine, store=store, raw_retention="365", environment="local", now=now, out=out, progress_out=progress_out)
        def call_endpoint(request):
            response = client.post(PATH, headers={"X-Analytics-Archive-Token": request.headers["X-Analytics-Archive-Token"]})
            return httpx.Response(response.status_code, json=response.json())
        env = {"PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL": "https://example.invalid" + PATH,
               "PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN": TOKEN}
        with patch.object(trigger, "get_settings", return_value=SimpleNamespace(PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN=TOKEN)), patch.object(trigger.archive_job, "run", side_effect=run_in_test_database), httpx.Client(transport=httpx.MockTransport(call_endpoint)) as http_client:
            assert scheduler.run(env=env, client=http_client, out=lambda _: None) == 0
        with Session(engine) as db:
            assert db.execute(select(HubProductEventV2)).scalars().all() == []
            records = db.execute(select(HubProductAnalyticsArchiveV2)).scalars().all()
            assert len(records) == 1
            _, data = archive.archive_records(store.get(records[0].key))
            assert [row["target_id"] for row in data["events"]] == ["live-page"]
    finally:
        if engine is not None:
            engine.dispose()
        with base.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()
