import asyncio
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import app.routers.airtable as airtable_router


SOURCE_URL = (
    "https://airtable.com/"
    "appP9ziO5nV1LY7eS/"
    "tblj34ByiDh40US4x/"
    "viw6Ckhb93CGwRzQS"
)


def _bundle(
    *,
    widget_type="airtable",
    personalize_enabled=False,
    pat="SAFE_TEST_PAT",
    data=None,
):
    return SimpleNamespace(
        config={
            "sourceUrl": SOURCE_URL,
            "personalizeEnabled": personalize_enabled,
            "personalizeColumn": (
                "Email"
                if personalize_enabled
                else None
            ),
        },
        pat=pat,
        data=dict(data or {}),
        widget_type=widget_type,
    )


def _run_snapshot(bundle, service):
    with patch.object(
        airtable_router,
        "get_airtable_component_bundle",
        return_value=bundle,
    ):
        return asyncio.run(
            airtable_router.get_airtable_component_index_snapshot(
                link="component-link",
                _service_auth=None,
                db=object(),
                airtable_service=service,
            )
        )


def test_airtable_index_sync_auth_uses_constant_time_compare():
    real_compare = hmac.compare_digest

    settings = SimpleNamespace(
        AGENT_SYNC_TOKEN="expected-sync-token"
    )

    with patch.object(
        airtable_router,
        "get_settings",
        return_value=settings,
    ):
        with patch.object(
            airtable_router.hmac,
            "compare_digest",
            wraps=real_compare,
        ) as compare:
            airtable_router._require_airtable_index_sync_token(
                "expected-sync-token"
            )

            compare.assert_called_once_with(
                "expected-sync-token",
                "expected-sync-token",
            )

        with pytest.raises(HTTPException) as exc:
            airtable_router._require_airtable_index_sync_token(
                "wrong-token"
            )

    assert exc.value.status_code == 401


def test_unpersonalized_table_returns_only_shared_filtered_rows():
    filters = [
        {
            "field": "Status",
            "operator": "eq",
            "value": ["Active Program"],
        }
    ]

    service = SimpleNamespace(
        fetch_widget_index_rows=AsyncMock(
            return_value=SimpleNamespace(
                fields=["Name", "Status"],
                rows=[
                    {
                        "id": "rec-safe-test",
                        "Name": "Program A",
                        "Status": "Active Program",
                    }
                ],
                available=True,
            )
        )
    )

    bundle = _bundle(
        data={
            "selectedColumns": [
                "Name",
                "Status",
            ],
            "filters": filters,
        }
    )

    result = _run_snapshot(bundle, service)

    assert result.widget_type == "airtable"
    assert result.personalize_enabled is False
    assert result.row_data_included is True
    assert result.available is True
    assert result.reason == "shared_rows"
    assert result.fields == ["Name", "Status"]
    assert len(result.rows) == 1

    kwargs = (
        service
        .fetch_widget_index_rows
        .await_args
        .kwargs
    )

    assert kwargs["selected_columns"] == [
        "Name",
        "Status",
    ]
    assert kwargs["filters"] == filters

    dumped = result.model_dump()

    assert "pat" not in dumped
    assert "sourceUrl" not in dumped
    assert "SAFE_TEST_PAT" not in repr(dumped)


def test_personalized_table_never_fetches_or_returns_rows():
    service = SimpleNamespace(
        fetch_widget_index_rows=AsyncMock(
            side_effect=AssertionError(
                "Personalized ingestion must not fetch rows"
            )
        )
    )

    result = _run_snapshot(
        _bundle(
            personalize_enabled=True,
            data={
                "selectedColumns": [
                    "Name",
                    "Email",
                ],
                "filters": [],
            },
        ),
        service,
    )

    assert result.personalize_enabled is True
    assert result.personalize_column == "Email"
    assert result.rows == []
    assert result.row_data_included is False
    assert result.reason == "personalized_live_only"

    service.fetch_widget_index_rows.assert_not_awaited()

    assert "SAFE_TEST_PAT" not in repr(
        result.model_dump()
    )


def test_metric_is_config_only_and_never_returns_live_value():
    service = SimpleNamespace(
        fetch_widget_index_rows=AsyncMock(
            side_effect=AssertionError(
                "Metric ingestion must not fetch rows"
            )
        )
    )

    result = _run_snapshot(
        _bundle(
            widget_type="airtable_metric",
            personalize_enabled=True,
            data={
                "filters": [],
                "aggregation": "count",
                "description": "Active Member",
                "note": "Dashboard count",
                "url": "https://example.com",
            },
        ),
        service,
    )

    assert result.widget_type == "airtable_metric"
    assert result.aggregation == "count"
    assert result.metric_description == "Active Member"
    assert result.metric_note == "Dashboard count"
    assert result.metric_url == "https://example.com"

    assert result.rows == []
    assert result.row_data_included is False
    assert result.reason == "metric_live_only"

    dumped = result.model_dump()

    assert "value" not in dumped
    assert "SAFE_TEST_PAT" not in repr(dumped)

    service.fetch_widget_index_rows.assert_not_awaited()


def test_unpersonalized_missing_pat_is_unavailable_not_empty_authority():
    service = SimpleNamespace(
        fetch_widget_index_rows=AsyncMock(
            side_effect=AssertionError(
                "Missing PAT must not fetch rows"
            )
        )
    )

    result = _run_snapshot(
        _bundle(
            pat=None,
            data={
                "selectedColumns": ["Name"],
                "filters": [],
            },
        ),
        service,
    )

    assert result.available is False
    assert result.row_data_included is False
    assert result.rows == []
    assert result.reason == "source_credentials_unavailable"

    service.fetch_widget_index_rows.assert_not_awaited()


def test_chart_uses_complete_shared_index_rows_and_only_required_fields():
    service = SimpleNamespace(
        fetch_widget_index_rows=AsyncMock(
            return_value=SimpleNamespace(
                fields=["Location", "Earnings"],
                rows=[
                    {
                        "id": "rec-safe",
                        "Location": "Northeast",
                        "Earnings": 12,
                    }
                ],
                available=True,
            )
        )
    )

    result = _run_snapshot(
        _bundle(
            widget_type="chart",
            data={
                "title": "Earning per Region",
                "groupField": "Location",
                "aggregation": "sum",
                "sumField": "Earnings",
                "filters": [],
            },
        ),
        service,
    )

    assert result.widget_type == "chart"
    assert result.reason == "shared_rows"
    assert result.row_data_included is True
    assert result.available is True
    assert result.fields == ["Location", "Earnings"]

    kwargs = service.fetch_widget_index_rows.await_args.kwargs
    assert kwargs["selected_columns"] == ["Location", "Earnings"]
    assert kwargs["filters"] is None
    assert "SAFE_TEST_PAT" not in repr(result.model_dump())
