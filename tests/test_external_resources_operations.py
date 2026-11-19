"""Exercise the external worker operations against an isolated PostgreSQL schema."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.db_v2.external_worker_operations_schema import OPERATIONS_SCHEMA
from app.db_v2.external_worker_schema import CONTENT_LINK_SCHEMA_STATEMENTS
from app.dependencies import get_current_hub_user
from app.routers import bot_management, external_resources


@pytest.fixture()
def database():
    dsn = os.getenv("EXTERNAL_RESOURCES_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set EXTERNAL_RESOURCES_TEST_DATABASE_URL to a disposable PostgreSQL.")
    engine = create_engine(dsn)
    with engine.connect() as connection, connection.begin() as transaction:
        schema = f"external_resources_test_{uuid4().hex}"
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        connection.execute(text("""
            CREATE TABLE content_links (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, provider TEXT, resource_type TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), ingested BOOLEAN NOT NULL DEFAULT FALSE,
                visited BOOLEAN NOT NULL DEFAULT FALSE, deleted BOOLEAN NOT NULL DEFAULT FALSE,
                acl_refresh_required BOOLEAN NOT NULL DEFAULT FALSE, revisit BOOLEAN NOT NULL DEFAULT FALSE,
                content_fingerprint TEXT, reason TEXT
            )"""))
        connection.execute(text("CREATE TABLE component_link_map (link_id INTEGER, component_id INTEGER NOT NULL DEFAULT 8)"))
        connection.execute(text("CREATE TABLE components (id INTEGER PRIMARY KEY, title TEXT, type TEXT, link TEXT, gridstack_id INTEGER)"))
        connection.execute(text("CREATE TABLE gridstacks (id INTEGER PRIMARY KEY, parent_tab_id INTEGER)"))
        connection.execute(text("CREATE TABLE tabs (id INTEGER PRIMARY KEY, title TEXT)"))
        for statement in (*CONTENT_LINK_SCHEMA_STATEMENTS, *OPERATIONS_SCHEMA, *OPERATIONS_SCHEMA):
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO external_worker_setting_versions (paused) VALUES (FALSE)"))
        connection.execute(text("INSERT INTO external_worker_control (environment, version_id) VALUES ('local', 1)"))
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()
    engine.dispose()


@pytest.fixture()
def client(database, monkeypatch):
    app = FastAPI()
    app.include_router(external_resources.router)
    actor = SimpleNamespace(hub_user_id=11, email="admin@example.test", info=object())
    app.dependency_overrides[get_current_hub_user] = lambda: actor
    app.dependency_overrides[get_db_v2] = lambda: database
    monkeypatch.setattr(external_resources, "get_settings", lambda: SimpleNamespace(hub_environment="local"))
    monkeypatch.setattr(bot_management, "hub_admin_target_ids", lambda _: (1, {2}))
    monkeypatch.setattr(bot_management, "effective_pairs", lambda *_: {(1, 2)})
    return TestClient(app)


def test_pause_is_audited_and_rejects_a_stale_version(client):
    base = "/v2/external-resources"
    assert client.get(base + "/context").json()["migration_ready"]
    assert client.get(base + "/settings").json()["paused"] is False
    change = {"paused": True, "expected_version_id": 1, "confirm": True}
    assert client.put(base + "/settings", json={**change, "confirm": False}).status_code == 422
    result = client.put(base + "/settings", json=change)
    assert result.status_code == 200, result.text
    assert result.json()["version_id"] == 2
    assert client.put(base + "/settings", json=change).status_code == 409
    audit = client.get(base + "/settings/audit")
    assert audit.status_code == 200, audit.text
    assert [row["actor_email"] for row in audit.json()["data"]] == ["admin@example.test", None]


def test_scope_view_as_and_environment_are_enforced(client, monkeypatch):
    base = "/v2/external-resources"
    assert client.get(base + "/context?view_as=admin:all").status_code == 403
    assert client.get(base + "/context?anonymous=true").status_code == 403
    monkeypatch.setattr(external_resources, "get_settings", lambda: SimpleNamespace(hub_environment="sandbox"))
    assert client.get(base + "/context").json()["misconfigured"]
    assert client.get(base + "/worker/runs").status_code == 503
    monkeypatch.setattr(bot_management, "effective_pairs", lambda *_: {(1, 99)})
    assert client.get(base + "/context").status_code == 403


def test_daily_metrics_survive_detail_pruning_and_exclude_admin_rows(database, client):
    database.execute(text("""
        INSERT INTO content_link_events
            (link_id, source, action, outcome, environment, duration_ms, created_at)
        VALUES (1, 'worker', 'inspection', 'ingested', 'local', 100, '2026-11-09 23:59:59+00'),
               (1, 'worker', 'inspection', 'ingested', 'local', 300, '2026-11-10 00:00:00+00'),
               (1, 'admin', 'check_requested', NULL, 'local', NULL, '2026-11-10 00:00:00+00')
    """))
    database.execute(text("DELETE FROM content_link_events WHERE source = 'worker'"))
    database.commit()
    result = client.get("/v2/external-resources/analytics/trends?date_from=2026-11-09&date_to=2026-11-10")
    assert result.status_code == 200, result.text
    assert result.json()["data"] == [
        {"day": "2026-11-09", "outcome": "ingested", "event_count": 1, "average_duration_ms": 100.0},
        {"day": "2026-11-10", "outcome": "ingested", "event_count": 1, "average_duration_ms": 300.0},
    ]


def test_resource_decision_rebuild_and_exports(database, client):
    base = "/v2/external-resources"
    database.execute(text("""
        INSERT INTO content_links (id, url, provider, resource_type, ingested, source_etag, content_fingerprint)
        VALUES (1, 'https://demo:secret@example.test/test.pdf?private=token#ignored', '=formula', 'PDF', TRUE, 'old', 'fingerprint')
    """))
    database.execute(text("INSERT INTO components (id, title, link) VALUES (8, 'Linked component', 'component-8')"))
    database.execute(text("INSERT INTO component_link_map (link_id) VALUES (1)"))
    database.commit()
    listed = client.get(base + "/resources")
    assert listed.status_code == 200, listed.text
    row = listed.json()["data"][0]
    assert row["url"] == "https://example.test/test.pdf"
    assert row["component_count"] == 1
    assert row["first_occurrence"]["component_link"] == "component-8"
    assert client.get(base + "/resources/1/occurrences").json()["data"] == [row["first_occurrence"]]
    decision = {"ingestion_enabled": True, "expected_updated_at": row["updated_at"]}
    result = client.put(base + "/resources/1/ingestion-decision", json=decision)
    assert result.status_code == 200, result.text
    assert client.put(base + "/resources/1/ingestion-decision", json=decision).status_code == 409
    assert client.post(base + "/resources/1/check").status_code == 200
    assert database.execute(text("SELECT source_etag FROM content_links")).scalar_one() == "old"
    assert client.post(base + "/resources/1/rebuild").status_code == 200
    assert database.execute(text("SELECT source_etag FROM content_links")).scalar_one() is None
    export = client.get(base + "/analytics/export?dataset=resources")
    assert export.status_code == 200, export.text
    assert "'=formula" in export.text
    assert "secret" not in export.text and "private=token" not in export.text
    history = client.get(base + "/resources/1/history").json()["data"]
    assert [entry["action"] for entry in history] == ["rebuild_requested", "check_requested", "ingestion_enabled"]


def test_retrieval_status_checks_environment_without_returning_agent_payload(client, monkeypatch):
    settings = SimpleNamespace(
        hub_environment="local", AGENT_API_URL="https://agent.example.test", AGENT_SYNC_TOKEN="test-only-token",
    )
    monkeypatch.setattr(external_resources, "get_settings", lambda: settings)
    received = []

    def response(url, **kwargs):
        received.append((url, kwargs["headers"]))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "environment": "sandbox", "state": "on", "internal": "do-not-expose",
        })

    monkeypatch.setattr(external_resources.httpx, "get", response)
    result = client.get("/v2/external-resources/worker/retrieval")
    assert result.status_code == 200
    assert result.json() == {"state": "misconfigured"}
    assert received == [("https://agent.example.test/agent/external-documents/runtime-status",
                         {"X-Sync-Token": "test-only-token", "Accept": "application/json"})]



def test_unsupported_formats_are_labelled_and_rejected_on_all_processing_routes(database, client):
    base = "/v2/external-resources"
    database.execute(text(
        "INSERT INTO content_links (id, url, provider, resource_type) VALUES "
        "(10, 'https://example.test/report.xlsx', 'example.test', 'Pending detection')"
    ))
    database.commit()
    row = client.get(base + "/resources/10").json()
    assert row["file_type"] == "Excel"
    assert row["ingestion_supported"] is False
    assert "not supported" in row["ingestion_block_reason"]
    response = client.put(base + "/resources/10/ingestion-decision",
                          json={"ingestion_enabled": True, "expected_updated_at": row["updated_at"]})
    assert response.status_code == 422
    assert client.post(base + "/resources/10/check").status_code == 422
    assert client.post(base + "/resources/10/rebuild").status_code == 422
    assert database.execute(text("SELECT COUNT(*) FROM content_link_events")).scalar_one() == 0


def test_unknown_format_can_be_approved_for_its_first_inspection(database, client):
    base = "/v2/external-resources"
    database.execute(text(
        "INSERT INTO content_links (id, url, provider, resource_type) VALUES "
        "(11, 'https://example.test/download', 'example.test', 'Pending detection')"
    ))
    database.commit()
    row = client.get(base + "/resources/11").json()
    assert row["file_type"] == "Type not checked"
    assert row["ingestion_supported"] is None
    assert client.put(base + "/resources/11/ingestion-decision",
                      json={"ingestion_enabled": True, "expected_updated_at": row["updated_at"]}).status_code == 200



def test_source_navigation_preserves_drive_identity_and_valid_ports_without_secrets():
    assert external_resources._safe_link(
        "https://drive.google.com/open?id=qa-document-123456&private=secret"
    ) == "https://drive.google.com/open?id=qa-document-123456"
    assert external_resources._safe_link(
        "https://user:password@example.test:8443/doc.pdf?private=secret"
    ) == "https://example.test:8443/doc.pdf"
    assert external_resources._safe_link("https://example.test:bad/doc.pdf") == "Unavailable"


def _run_with_items(database, notes):
    database.execute(text("""
        INSERT INTO external_worker_runs (id, status, mode, environment) VALUES (90, 'completed', 'live', 'local')
    """))
    for link_id, note in notes.items():
        database.execute(text("""
            INSERT INTO external_worker_run_items (run_id, link_id, outcome, ocr_note)
            VALUES (90, :link_id, 'ingested', :note)
        """), {"link_id": link_id, "note": note})
    database.commit()


def test_run_items_return_only_known_ocr_notes(database, client):
    _run_with_items(database, {1: "ocr_date_corrected", 2: "ocr_implausible_date", 3: "something_internal", 4: None, 5: "ocr_unreadable_text", 6: "ocr_page_rotated"})
    result = client.get("/v2/external-resources/worker/runs/90/items")
    assert result.status_code == 200, result.text
    assert {row["link_id"]: row["ocr_note"] for row in result.json()["data"]} == {
        1: "ocr_date_corrected", 2: "ocr_implausible_date", 3: None, 4: None, 5: "ocr_unreadable_text", 6: "ocr_page_rotated",
    }


def test_run_items_still_load_before_the_ocr_note_migration(database, client):
    _run_with_items(database, {1: "ocr_date_corrected"})
    database.execute(text("ALTER TABLE external_worker_run_items DROP COLUMN ocr_note"))
    database.commit()
    result = client.get("/v2/external-resources/worker/runs/90/items")
    assert result.status_code == 200, result.text
    assert [(row["link_id"], row["outcome"], row["ocr_note"]) for row in result.json()["data"]] == [(1, "ingested", None)]


def test_search_treats_percent_and_underscore_as_plain_characters(database, client):
    database.execute(text("""
        INSERT INTO content_links (id, url, provider, resource_type) VALUES
            (1, 'https://example.test/a_b.pdf', 'p', 'PDF'), (2, 'https://example.test/axb.pdf', 'p', 'PDF'),
            (3, 'https://example.test/100%.pdf', 'p', 'PDF')
    """))
    database.commit()
    ids = lambda term: sorted(row["id"] for row in client.get("/v2/external-resources/resources", params={"search": term}).json()["data"])
    assert ids("a_b") == [1]       # `_` is not "any one character": axb is not a match
    assert ids("%") == [3]         # `%` is not "anything"
    assert ids("") == [1, 2, 3]


def test_run_events_match_the_resource_history_for_events_written_before_environments(database, client):
    database.execute(text("INSERT INTO content_links (id, url, provider, resource_type) VALUES (1, 'https://example.test/a.pdf', 'p', 'PDF')"))
    _run_with_items(database, {1: None})
    database.execute(text("""
        INSERT INTO content_link_events (link_id, source, action, outcome, environment, worker_run_id)
        VALUES (1, 'worker', 'inspection', 'ingested', 'local', 90), (1, 'worker', 'inspection', 'ingested', NULL, 90)
    """))
    database.commit()
    base = "/v2/external-resources"
    assert len(client.get(base + "/worker/runs/90/events").json()["data"]) == 2
    assert len(client.get(base + "/resources/1/history").json()["data"]) == 2
    exported = client.get(base + "/analytics/export?dataset=events").text.strip().splitlines()
    assert len(exported) == 3  # header + both events; the pre-environment one keeps an empty environment


def test_exports_have_no_row_ceiling_and_always_have_a_header(database, client, monkeypatch):
    monkeypatch.setattr(external_resources, "_EXPORT_PAGE", 3)
    empty = client.get("/v2/external-resources/analytics/export?dataset=runs")
    assert empty.status_code == 200 and empty.text.strip() == ",".join(external_resources._EXPORT_COLUMNS["runs"])
    assert client.get("/v2/external-resources/analytics/export?dataset=resources").text.strip() == "id,url,environment"
    database.execute(text("INSERT INTO content_links (id, url, provider, resource_type) SELECT g, 'https://example.test/' || g, 'p', 'PDF' FROM generate_series(1, 8) g"))
    database.commit()
    lines = client.get("/v2/external-resources/analytics/export?dataset=resources").text.strip().splitlines()
    assert len(lines) == 9  # header + all 8, read three at a time
    monkeypatch.setattr(external_resources, "_EXPORT_MAX_ROWS", 5)
    assert client.get("/v2/external-resources/analytics/export?dataset=resources").status_code == 422


def test_preview_shows_only_indexed_documents_and_only_what_the_agent_returns(database, client, monkeypatch):
    database.execute(text("""
        INSERT INTO content_links (id, url, provider, resource_type, ingested, ingestion_enabled) VALUES
            (1, 'https://example.test/a.pdf', 'p', 'PDF', TRUE, TRUE), (2, 'https://example.test/b.pdf', 'p', 'PDF', FALSE, TRUE),
            (3, 'https://example.test/c.pdf', 'p', 'PDF', TRUE, FALSE), (4, 'https://example.test/d.pdf', 'p', 'PDF', TRUE, TRUE)
    """))
    database.execute(text("UPDATE content_links SET archived = TRUE WHERE id = 4"))
    database.commit()
    settings = SimpleNamespace(hub_environment="local", AGENT_API_URL="https://agent.example.test", AGENT_SYNC_TOKEN="test-only-token")
    monkeypatch.setattr(external_resources, "get_settings", lambda: settings)
    calls = []

    def agent(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "available": True, "title": " Determination letter ", "excerpt": " Dear applicant, you are exempt. ", "sections": 3,
            "internal": "do-not-expose", "collection": "secret"})

    monkeypatch.setattr(external_resources.httpx, "get", agent)
    base = "/v2/external-resources/resources"
    ok = client.get(base + "/1/preview")
    assert ok.status_code == 200 and ok.headers["cache-control"] == "private, no-store"
    assert ok.json() == {"available": True, "title": "Determination letter", "excerpt": "Dear applicant, you are exempt.", "sections": 3}
    assert calls == [("https://agent.example.test/internal/external-documents/1/preview", {"X-Sync-Token": "test-only-token", "Accept": "application/json"})]
    for link_id in (2, 3, 4):            # not indexed, ingestion off, archived: the Agent is not even asked
        assert client.get(f"{base}/{link_id}/preview").json() == {"available": False, "reason": "not_indexed"}
    assert len(calls) == 1
    assert client.get(base + "/99/preview").status_code == 404

    def down(*_a, **_k):
        raise external_resources.httpx.ConnectError("agent down")
    monkeypatch.setattr(external_resources.httpx, "get", down)
    assert client.get(base + "/1/preview").json() == {"available": False, "reason": "unavailable"}
    monkeypatch.setattr(external_resources, "get_settings", lambda: SimpleNamespace(hub_environment="local", AGENT_API_URL="", AGENT_SYNC_TOKEN=""))
    assert client.get(base + "/1/preview").json() == {"available": False, "reason": "unavailable"}


def test_preview_is_admin_only_and_unavailable_in_view_as(client, monkeypatch):
    base = "/v2/external-resources/resources/1/preview"
    assert client.get(base + "?view_as=role:scope").status_code == 403
    monkeypatch.setattr(bot_management, "effective_pairs", lambda *_: set())
    assert client.get(base).status_code == 403
