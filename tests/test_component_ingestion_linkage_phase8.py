"""Mutation receipt contract tests for deterministic Hub re-ingestion."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import hub_service, nav_tab_service
from app.services.access_control_service import NodeRef


def _fake_db():
    return SimpleNamespace(commit=MagicMock(), rollback=MagicMock())


def test_hub_access_write_returns_component_receipts(monkeypatch):
    db = _fake_db()
    hub = SimpleNamespace(
        id=1,
        document_id="hub-v2",
        access_control={"viewers": {}, "admins": {}},
        updated_at=None,
    )
    touched = [NodeRef("tab", 10)]
    receipt = {"component_id": 295, "action": "upsert"}

    monkeypatch.setattr(hub_service.access_control_service, "get_hub", lambda _db: hub)
    monkeypatch.setattr(
        hub_service.access_control_service,
        "apply_write",
        lambda _db, _ref, _ac: touched,
    )
    monkeypatch.setattr(
        hub_service,
        "_refresh_index_for_touched",
        lambda _db, refs: [receipt] if refs == touched else [],
    )

    result = hub_service.update_hub_v2(
        db,
        {"viewers": {}, "admins": {}},
    )

    assert result is not None
    assert result["search_updates"] == [receipt]
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_nav_access_write_returns_component_receipts(monkeypatch):
    db = _fake_db()
    nav = SimpleNamespace(
        id=7,
        document_id="nav-v2",
        slug="nav",
        title="Nav",
        order=0,
        access_control={"viewers": {}, "admins": {}},
        protected=False,
        icon=None,
        updated_at=None,
    )
    touched = [NodeRef("tab", 10)]
    receipt = {"component_id": 365, "action": "upsert"}

    monkeypatch.setattr(
        nav_tab_service,
        "get_nav_tab_by_document_id",
        lambda _db, _document_id: nav,
    )
    monkeypatch.setattr(
        nav_tab_service,
        "apply_write",
        lambda _db, _ref, _ac: touched,
    )
    monkeypatch.setattr(
        nav_tab_service,
        "_refresh_index_for_touched",
        lambda _db, refs: [receipt] if refs == touched else [],
    )

    result = nav_tab_service.update_nav_tab_v2(
        db,
        "nav-v2",
        access_control={"viewers": {}, "admins": {}},
    )

    assert result is not None
    assert result["search_updates"] == [receipt]
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_nav_rename_reindexes_all_descendant_component_metadata(monkeypatch):
    db = _fake_db()
    nav = SimpleNamespace(
        id=7,
        document_id="nav-v2",
        slug="old-nav",
        title="Old Nav",
        order=0,
        access_control={"viewers": {}, "admins": {}},
        protected=False,
        icon=None,
        updated_at=None,
    )
    receipt = {"component_id": 371, "action": "upsert"}

    monkeypatch.setattr(
        nav_tab_service,
        "get_nav_tab_by_document_id",
        lambda _db, _document_id: nav,
    )
    monkeypatch.setattr(
        nav_tab_service,
        "_resolve_nav_slug",
        lambda _db, title, exclude_id=None: "new-nav",
    )
    monkeypatch.setattr(
        nav_tab_service,
        "_nav_tab_component_search_updates",
        lambda _db, nav_tab_id: [receipt] if nav_tab_id == 7 else [],
    )

    result = nav_tab_service.update_nav_tab_v2(db, "nav-v2", title="New Nav")

    assert result is not None
    assert result["title"] == "New Nav"
    assert result["slug"] == "new-nav"
    assert result["search_updates"] == [receipt]


def test_search_update_dedupe_last_action_wins():
    assert nav_tab_service._dedupe_search_updates(
        [
            {"component_id": 10, "action": "upsert"},
            {"component_id": 11, "action": "upsert"},
            {"component_id": 10, "action": "delete"},
        ]
    ) == [
        {"component_id": 10, "action": "delete"},
        {"component_id": 11, "action": "upsert"},
    ]
