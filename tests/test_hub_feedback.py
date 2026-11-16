"""Hub feedback: live RBAC, private context, identity, retries and environment."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_v2.models.hub import HubV2
from app.db_v2.models.nav_tab import NavTabV2
from app.db_v2.models.tab import TabV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.page_content import PageContentV2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.hub_feedback import HubFeedbackV2, BotFeedbackV2
from app.db_v2.database import get_db_v2
from app.dependencies import CurrentHubUser, get_current_hub_user
from app.routers import hub_feedback as router
from app.routers import airtable, bot_management
from app.models.airtable import FeedbackRecord
from app.services import access_visibility_service as access_service
from app.services.resource_grant_service import GrantMatch


@compiles(JSONB, "sqlite")
def jsonb_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


def payload(scope="hub", **kwargs):
    return {"submission_id": str(uuid4()), "scope": scope, "message": "Useful feedback", **kwargs}


@pytest.fixture
def hub():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (HubV2, NavTabV2, TabV2, GridstackV2, PageContentV2, ComponentV2, HubFeedbackV2, BotFeedbackV2):
        model.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(HubV2(id=1))
        db.add(NavTabV2(id=1, document_id="nav", title="Navigation", slug="navigation"))
        db.flush()
        db.add_all([TabV2(id=1, document_id="page-a", nav_tab_id=1, title="Page A"),
                    TabV2(id=2, document_id="page-b", nav_tab_id=1, title="Private page B")])
        db.flush()
        db.add_all([GridstackV2(id=1, document_id="page-a", parent_tab_id=1),
                    GridstackV2(id=2, document_id="page-b", parent_tab_id=2)])
        db.flush()
        db.add(GridstackV2(id=3, document_id="sub-page", name="Sub page", parent_tab_id=1, parent_id=1))
        db.flush()
        db.add_all([ComponentV2(id=1, link="component-a", type="links", title="Links A", gridstack_id=1),
                    ComponentV2(id=2, link="component-b", type="button", title="Private button B", gridstack_id=2),
                    ComponentV2(id=3, link="mirror-a", type="mirror", title="Mirror A", gridstack_id=1,
                                props={"target_link": "component-b"}),
                    ComponentV2(id=4, link="notebook", type="super_block_note", title="Notebook", gridstack_id=1)])
        db.flush()
        db.add(ComponentV2(id=5, link="notebook-child", type="block_note", title="Shared notebook tab",
                           gridstack_id=1, super_blocknote_id=4))
        db.add(ComponentV2(id=6, link="sub-representation", type="gridstack", gridstack_id=1, current_grid_id=3))
        db.add(ComponentV2(id=7, link="sub-button", type="button", title="Sub button", gridstack_id=3))
        db.commit()
    state = SimpleNamespace(admin=True, seeds=[], user_id=1, email="admin@renphil.org", environment="sandbox")
    def caller():
        return CurrentHubUser(info=SimpleNamespace(email=state.email, roles=[]), hub_user_id=state.user_id, email=state.email)
    def access(db, _current):
        if state.admin:
            return access_service.ViewerAccess(None, True)
        return access_service.ViewerAccess(access_service.fold(access_service.build_node_tree(db), state.seeds), False)
    app = FastAPI()
    app.include_router(router.router)
    app.include_router(bot_management.router)
    app.post("/data/feedbacks", response_model=FeedbackRecord, status_code=201)(airtable.create_feedback)
    app.dependency_overrides[get_current_hub_user] = caller
    def db_dependency():
        with sessions() as db:
            yield db
    def admin_only():
        if not state.admin:
            raise HTTPException(403, "Admin required")
        return caller().info
    app.dependency_overrides[get_db_v2] = db_dependency
    app.dependency_overrides[bot_management.require_bot_management_admin] = admin_only
    limiter = SimpleNamespace(check=AsyncMock(return_value=None), consume=AsyncMock())
    with patch.object(router, "SessionLocalV2", sessions), patch.object(router, "_viewer_access", access), \
         patch.object(router, "get_settings", lambda: SimpleNamespace(hub_environment=state.environment)), \
         patch.object(bot_management, "get_app_settings", lambda: SimpleNamespace(hub_environment=state.environment)), \
         patch.object(router, "get_rate_limit_service", lambda: limiter):
        state.client = TestClient(app)
        state.sessions = sessions
        state.limiter = limiter
        yield state
    engine.dispose()


def as_editor(hub, kind="tab", node_id=1, level="edit"):
    hub.admin = False
    hub.seeds = [GrantMatch(grant_id=1, node_kind=kind, node_id=node_id, level=level)]


def test_global_context_is_hidden_and_actor_and_environment_are_trusted(hub):
    result = hub.client.post("/v2/hub-feedback", json=payload(page_id="page-a", page_route="/private/jane?token=secret"))
    assert result.status_code == 201, result.text
    assert result.json()["from_email"] == "admin@renphil.org"
    assert result.json()["page_id"] is None
    assert "diagnostic_page_id" not in result.json()
    assert "Page A" not in result.text
    row = hub.client.get("/v2/hub-feedback").json()["data"][0]
    assert row["page_name"] is None
    with hub.sessions() as db:
        saved = db.scalar(select(HubFeedbackV2))
        assert saved.diagnostic_page_id == "page-a"
        assert saved.page_id is None
        assert saved.diagnostic_route == "/:nav_tab/:tab"
        assert saved.environment == "sandbox"


@pytest.mark.parametrize("field,value", [("from_email", "spoof@example.org"), ("environment", "production"),
                                         ("source", "Bot"), ("diagnostic_page_id", "page-b")])
def test_client_cannot_supply_trusted_or_private_fields(hub, field, value):
    assert hub.client.post("/v2/hub-feedback", json=payload(**{field: value})).status_code == 422


@pytest.mark.parametrize("change", [{"message": "   "}, {"message": "x" * 2001}, {"message": "x\x00"},
                                    {"submission_id": "bad"}, {"scope": "page"},
                                    {"component_link": "component-a"}, {"page_id": "https://evil"}])
def test_invalid_inputs(hub, change):
    data = payload()
    data.update(change)
    result = hub.client.post("/v2/hub-feedback", json=data)
    assert result.status_code == 422
    assert result.headers["cache-control"] == "no-store"


def test_viewer_can_submit_but_cannot_read_feedback(hub):
    as_editor(hub, level="view")
    assert hub.client.post("/v2/hub-feedback", json=payload("page", page_id="page-a")).status_code == 201
    assert hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="component-a")).status_code == 201
    assert hub.client.get("/v2/hub-feedback").status_code == 403


def test_invalid_and_inaccessible_targets_have_identical_responses(hub):
    as_editor(hub)
    real = hub.client.post("/v2/hub-feedback", json=payload("page", page_id="page-b"))
    fake = hub.client.post("/v2/hub-feedback", json=payload("page", page_id="does-not-exist"))
    assert real.status_code == fake.status_code == 404
    assert real.content == fake.content


def test_component_binding_cannot_be_forged(hub):
    result = hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-b", component_link="component-a"))
    assert result.status_code == 404


def test_idempotent_retry_and_conflicting_reuse(hub):
    data = payload()
    first = hub.client.post("/v2/hub-feedback", json=data)
    second = hub.client.post("/v2/hub-feedback", json=data)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert hub.client.get("/v2/hub-feedback").json()["total"] == 1
    data["message"] = "Different message"
    assert hub.client.post("/v2/hub-feedback", json=data).status_code == 409
    assert hub.limiter.consume.await_count == 1


def test_same_submission_key_is_independent_for_different_users(hub):
    data = payload()
    first = hub.client.post("/v2/hub-feedback", json=data)
    hub.user_id, hub.email = 2, "member@renphil.org"
    second = hub.client.post("/v2/hub-feedback", json=data)
    assert first.json()["id"] != second.json()["id"]


def test_editor_sees_only_current_editable_targets_and_global_with_identity(hub):
    for data in (payload(), payload("page", page_id="page-a"), payload("page", page_id="page-b"),
                 payload("component", page_id="page-b", component_link="component-b")):
        assert hub.client.post("/v2/hub-feedback", json=data).status_code == 201
    as_editor(hub)
    result = hub.client.get("/v2/hub-feedback").json()
    assert result["total"] == 2
    assert {row["scope"] for row in result["data"]} == {"hub", "page"}
    assert all(row["from_email"] == "admin@renphil.org" for row in result["data"])
    assert "Private" not in json.dumps(result)
    assert hub.client.get("/v2/hub-feedback?page_id=page-b").json()["total"] == 0
    assert hub.client.get("/v2/hub-feedback?page_id=random").json()["total"] == 0


def test_revocation_applies_to_next_list_and_detail(hub):
    created = hub.client.post("/v2/hub-feedback", json=payload("page", page_id="page-a")).json()
    as_editor(hub)
    assert hub.client.get("/v2/hub-feedback").json()["total"] == 1
    as_editor(hub, node_id=2)
    assert hub.client.get("/v2/hub-feedback").json()["total"] == 0
    assert hub.client.get("/v2/hub-feedback/" + created["id"]).status_code == 404
    hub.seeds = []
    assert hub.client.get("/v2/hub-feedback").status_code == 403


def test_component_only_editor_cannot_read_page_feedback(hub):
    hub.client.post("/v2/hub-feedback", json=payload("page", page_id="page-a"))
    hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="component-a"))
    as_editor(hub, "component", 1)
    result = hub.client.get("/v2/hub-feedback").json()
    assert result["total"] == 1
    assert result["data"][0]["scope"] == "component"


def test_viewer_of_only_a_nested_notebook_tab_can_report_that_tab(hub):
    as_editor(hub, "component", 5, level="view")
    assert hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="notebook")).status_code == 404
    assert hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="notebook-child")).status_code == 201
    assert hub.client.get("/v2/hub-feedback").status_code == 403


def test_nested_canvas_feedback_uses_its_own_permission_node(hub):
    as_editor(hub, "component", 6)
    assert hub.client.post("/v2/hub-feedback", json=payload("page", page_id="sub-page")).status_code == 201
    assert hub.client.post("/v2/hub-feedback", json=payload("component", page_id="sub-page", component_link="sub-button")).status_code == 201
    result = hub.client.get("/v2/hub-feedback").json()
    assert result["total"] == 2
    assert all(row["page_name"] == "Sub page" for row in result["data"])


def test_mirror_requires_access_to_its_original(hub):
    as_editor(hub)
    result = hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="mirror-a"))
    assert result.status_code == 404
    hub.seeds.append(GrantMatch(grant_id=2, node_kind="component", node_id=2, level="edit"))
    assert hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="mirror-a")).status_code == 201


def test_deleted_page_retains_storage_but_has_no_current_name(hub):
    created = hub.client.post("/v2/hub-feedback", json=payload("page", page_id="page-a")).json()
    with hub.sessions() as db:
        db.query(TabV2).filter(TabV2.id == 1).delete()
        db.commit()
    result = hub.client.get("/v2/hub-feedback/" + created["id"])
    assert result.status_code == 200
    assert result.json()["page_name"] is None
    assert result.json()["target_available"] is False
    as_editor(hub, node_id=2)
    assert hub.client.get("/v2/hub-feedback/" + created["id"]).status_code == 404


def test_orphan_is_not_valid_even_for_admin(hub):
    with hub.sessions() as db:
        db.query(NavTabV2).delete()
        db.commit()
    assert hub.client.post("/v2/hub-feedback", json=payload("page", page_id="page-a")).status_code == 404


def test_moved_component_does_not_reveal_its_previous_page_via_filters(hub):
    hub.client.post("/v2/hub-feedback", json=payload("component", page_id="page-a", component_link="component-a"))
    with hub.sessions() as db:
        db.query(ComponentV2).filter(ComponentV2.id == 1).update({"gridstack_id": 2})
        db.commit()
    as_editor(hub, "component", 1)
    result = hub.client.get("/v2/hub-feedback").json()
    assert result["total"] == 1
    assert result["data"][0]["page_id"] is None
    assert result["data"][0]["page_name"] is None
    assert hub.client.get("/v2/hub-feedback?page_id=page-a").json()["total"] == 0
    assert hub.client.get("/v2/hub-feedback?page_id=missing").json()["total"] == 0


def test_streamed_oversized_body_is_stopped_before_submission(hub):
    result = hub.client.post("/v2/hub-feedback", content=(b"x" * 1024 for _ in range(20)), headers={"Content-Type": "application/json"})
    assert result.status_code == 413
    assert result.json()["detail"] == "Feedback submission is too large."
    hub.limiter.check.assert_not_called()


def test_database_failure_is_safe_and_logs_no_feedback_content(hub, caplog):
    from sqlalchemy.exc import OperationalError
    with patch.object(router, "_resources", side_effect=OperationalError("secret-sql", {}, Exception("secret-body"))):
        result = hub.client.post("/v2/hub-feedback", json=payload(message="private-personal-feedback"))
    assert result.status_code == 503
    assert "secret" not in result.text and "private-personal-feedback" not in caplog.text
    assert "secret" not in caplog.text


def test_environment_lock_and_local_comparison(hub):
    sandbox = hub.client.post("/v2/hub-feedback", json=payload()).json()
    hub.environment = "production"
    hub.client.post("/v2/hub-feedback", json=payload())
    result = hub.client.get("/v2/hub-feedback?environment=sandbox").json()
    assert result["environment"] == "production"
    assert result["total"] == 1
    assert hub.client.get(f"/v2/hub-feedback/{sandbox['id']}?environment=sandbox").status_code == 404
    hub.environment = "local"
    assert hub.client.get("/v2/hub-feedback").json()["total"] == 0
    assert hub.client.get("/v2/hub-feedback?environment=all").json()["total"] == 2
    assert hub.client.get(f"/v2/hub-feedback/{sandbox['id']}?environment=sandbox").status_code == 200


@pytest.mark.parametrize("query", ["view_as=role:scope", "view_as_user=someone@example.org", "anonymous=true"])
def test_view_as_neither_submits_nor_reads_as_real_user(hub, query):
    assert hub.client.post("/v2/hub-feedback?" + query, json=payload()).status_code == 403
    assert hub.client.get("/v2/hub-feedback?" + query).status_code == 403
    assert hub.client.get("/v2/bot-management/feedback?" + query).status_code == 403
    assert hub.client.post("/data/feedbacks?" + query, json={"source": "Bot", "message": "Test"}).status_code == 403


def test_body_limit_content_type_and_rate_limit(hub):
    large = hub.client.post("/v2/hub-feedback", content=b"x" * 17000, headers={"Content-Type": "application/json"})
    assert large.status_code == 413
    assert large.headers["cache-control"] == "no-store"
    assert hub.client.post("/v2/hub-feedback", content=b"x").status_code == 415
    hub.limiter.check.return_value = 12
    limited = hub.client.post("/v2/hub-feedback", json=payload())
    assert limited.status_code == 429 and limited.headers["retry-after"] == "12"


def test_unconfigured_environment_refuses_to_record_or_read(hub):
    hub.environment = "unconfigured"
    assert hub.client.post("/v2/hub-feedback", json=payload()).status_code == 503
    assert hub.client.get("/v2/hub-feedback").status_code == 503


def test_legacy_bot_submission_is_separate_authenticated_and_repeatable(hub):
    data = {"source": "Bot", "message": "Check this answer", "message_id": "m1", "impression": "Dislike",
            "query": "Question", "response": "Answer", "from_email": "spoof@example.org"}
    first = hub.client.post("/data/feedbacks", json=data)
    assert first.status_code == 201, first.text
    second = hub.client.post("/data/feedbacks", json=data)
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["From"] == "admin@renphil.org"
    assert first.json()["environment"] == "sandbox"
    assert hub.client.get("/v2/hub-feedback").json()["total"] == 0
    hub.client.post("/v2/hub-feedback", json=payload())
    rows = hub.client.get("/v2/bot-management/feedback").json()["data"]
    assert len(rows) == 1 and rows[0]["source"] == "Bot"
    as_editor(hub)
    assert hub.client.get("/v2/bot-management/feedback").status_code == 403


def test_legacy_global_submissions_move_to_hub_feedback(hub):
    result = hub.client.post("/data/feedbacks", json={"message": "Legacy global form", "from_email": "spoof@example.org"})
    assert result.status_code == 201, result.text
    assert result.json()["From"] == "admin@renphil.org"
    assert hub.client.get("/v2/hub-feedback").json()["total"] == 1
    assert hub.client.get("/v2/bot-management/feedback").json()["data"] == []


def test_bot_feedback_environment_lock(hub):
    hub.client.post("/data/feedbacks", json={"source": "Bot", "message": "Sandbox"})
    hub.environment = "production"
    hub.client.post("/data/feedbacks", json={"source": "Bot", "message": "Production"})
    rows = hub.client.get("/v2/bot-management/feedback?environment=sandbox").json()["data"]
    assert len(rows) == 1 and rows[0]["environment"] == "production"


def test_bot_feedback_workbook_does_not_execute_user_text_as_formula():
    from io import BytesIO
    from zipfile import ZipFile
    from xml.etree import ElementTree as ET
    data = FeedbackRecord.model_validate({"id": "f", "From": "member@renphil.org", "Message": '=HYPERLINK("https://evil","x")',
        "Query": "=1+1", "Response": "https://evil", "Impression": "Dislike"})
    workbook = bot_management._build_bot_analytics_workbook(report="negative_feedback", points=[], runs=[],
        feedback_records=[data], viewer_time_zone="UTC")
    with ZipFile(BytesIO(workbook)) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                root = ET.fromstring(archive.read(name))
                assert not root.findall(".//{*}f")
                assert not root.findall(".//{*}hyperlink")
def test_component_label_uses_current_title_or_widget_type():
    from types import SimpleNamespace
    from app.services.hub_feedback_service import component_label

    assert component_label(SimpleNamespace(title=None, type="quick_actions")) == "Quick Actions"
    assert component_label(SimpleNamespace(title="  Custom  ", type="quick_actions")) == "Custom"
    assert component_label(SimpleNamespace(title=" ", type="unknown")) == "Untitled component"
