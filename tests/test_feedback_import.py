from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from unittest.mock import patch

from app.db_v2.models.hub_feedback import HubFeedbackV2, BotFeedbackV2
from app.services.feedback_import_service import FeedbackImportError, prepare_snapshot, import_prepared


def record(source="Hub", **fields):
    return {"id": "rec1", "createdTime": "2026-10-01T12:00:00Z", "fields": {"Source": source, "Message": "Helpful feedback", "From": "member@renphil.org", **fields}}


def test_snapshot_needs_explicit_environment_and_ambiguous_source_decision():
    with pytest.raises(FeedbackImportError):
        prepare_snapshot([record()], environment="guessed")
    with pytest.raises(FeedbackImportError):
        prepare_snapshot([record(None)], environment="sandbox")
    rows, skipped = prepare_snapshot([record(None)], environment="sandbox", skip_ambiguous=True)
    assert rows == [] and skipped == 1
    rows, _ = prepare_snapshot([record(None)], environment="sandbox", missing_source="hub")
    assert rows[0][0] == "hub"


def test_import_is_repeatable_and_separates_hub_and_bot():
    engine = create_engine("sqlite://")
    HubFeedbackV2.__table__.create(engine)
    BotFeedbackV2.__table__.create(engine)
    other = record("Bot", **{"Query": "question", "Response": "response"})
    other["id"] = "rec2"
    rows, _ = prepare_snapshot([record(), other], environment="sandbox")
    with Session(engine) as db:
        assert import_prepared(db, rows) == {"hub": 1, "bot": 1, "existing": 0}
        assert import_prepared(db, rows) == {"hub": 0, "bot": 0, "existing": 2}
        assert db.scalar(select(HubFeedbackV2)).actor_email == "member@renphil.org"
        assert db.scalar(select(BotFeedbackV2)).query == "question"


@pytest.mark.parametrize("fields", [{"Message": "   "}, {"Message": "secret" * 1000}, {"From": ["bad"]}, {"Date & Time": "not a date"}])
def test_invalid_snapshot_errors_do_not_reflect_content(fields):
    with pytest.raises(FeedbackImportError) as error:
        prepare_snapshot([record(**fields)], environment="sandbox")
    assert str(error.value) == "Snapshot record at index 0 is invalid; no rows imported."


def test_classification_change_rolls_back_whole_import():
    engine = create_engine("sqlite://")
    HubFeedbackV2.__table__.create(engine)
    BotFeedbackV2.__table__.create(engine)
    rows, _ = prepare_snapshot([record()], environment="sandbox")
    with Session(engine) as db:
        import_prepared(db, rows)
        changed = record("Bot")
        new = record()
        new["id"] = "new"
        rows, _ = prepare_snapshot([new, changed], environment="sandbox")
        with pytest.raises(FeedbackImportError):
            import_prepared(db, rows)
        assert len(db.scalars(select(HubFeedbackV2)).all()) == 1
        assert len(db.scalars(select(BotFeedbackV2)).all()) == 0


def test_duplicate_source_ids_in_one_snapshot_are_rejected():
    with pytest.raises(FeedbackImportError):
        prepare_snapshot([record(), deepcopy(record())], environment="sandbox")


def test_table_setup_is_idempotent_and_recreates_a_missing_index():
    from scripts import create_hub_feedback_tables as setup
    from sqlalchemy import inspect
    engine = create_engine("sqlite://")
    with patch.object(setup, "engine_v2", engine):
        setup.main()
        index = next(iter(HubFeedbackV2.__table__.indexes))
        index.drop(engine)
        setup.main()
        setup.main()
        assert index.name in {value["name"] for value in inspect(engine).get_indexes("hub_feedback")}


def test_changed_source_content_requires_review_instead_of_silent_skip():
    engine = create_engine("sqlite://")
    HubFeedbackV2.__table__.create(engine)
    BotFeedbackV2.__table__.create(engine)
    with Session(engine) as db:
        original, _ = prepare_snapshot([record()], environment="sandbox")
        import_prepared(db, original)
        changed, _ = prepare_snapshot([record(Message="Edited after first snapshot")], environment="sandbox")
        with pytest.raises(FeedbackImportError, match="changed"):
            import_prepared(db, changed)
        assert db.scalar(select(HubFeedbackV2)).message == "Helpful feedback"


def test_cli_default_dry_run_and_missing_target_never_open_database(tmp_path, capsys):
    import json
    from types import SimpleNamespace
    from scripts import import_feedback_snapshot as cli
    from app.services.feedback_import_service import DEFAULT_FIELDS
    snapshot = tmp_path / "fixture-feedback.json"
    snapshot.write_text(json.dumps([record(Message="private source content")]), encoding="utf-8")
    settings = SimpleNamespace(hub_environment="local", **{
        "AT_F_FEEDBACK_SOURCE": DEFAULT_FIELDS["source"], "AT_F_FEEDBACK_MESSAGE": DEFAULT_FIELDS["message"],
        "AT_F_FEEDBACK_FROM": DEFAULT_FIELDS["email"], "AT_F_FEEDBACK_IMPRESSION": DEFAULT_FIELDS["impression"],
        "AT_F_FEEDBACK_MESSAGE_ID": DEFAULT_FIELDS["message_id"], "AT_F_FEEDBACK_QUERY": DEFAULT_FIELDS["query"],
        "AT_F_FEEDBACK_RESPONSE": DEFAULT_FIELDS["response"],
    })
    with patch("app.config.get_settings", return_value=settings), patch("app.db_v2.database.SessionLocalV2") as sessions:
        assert cli.main([str(snapshot), "--environment", "sandbox"]) == 0
        assert cli.main([str(snapshot), "--environment", "sandbox", "--apply"]) == 2
        sessions.assert_not_called()
    output = capsys.readouterr().out
    assert "private source content" not in output
    assert "--expected-host" in output
