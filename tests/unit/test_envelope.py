from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from eodhd_guard_mcp.client import ApiResponse
from eodhd_guard_mcp.envelope import Meta, ToolWarning, WarningCode, build_response, iso_utc


def api_response(
    endpoint: str = "eod",
    *,
    cost: int = 1,
    cached: bool = False,
    fetched_at: datetime | None = None,
) -> ApiResponse:
    return ApiResponse(
        endpoint=endpoint,
        status=200,
        data=[],
        cached=cached,
        cost=cost,
        fetched_at=fetched_at or datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
        duration_ms=1.0,
    )


def test_iso_utc_converts_to_utc_and_drops_microseconds() -> None:
    plus_two = timezone(timedelta(hours=2))
    moment = datetime(2026, 11, 3, 17, 30, 5, 987654, tzinfo=plus_two)
    assert iso_utc(moment) == "2026-11-03T15:30:05Z"


def test_iso_utc_treats_a_naive_datetime_as_utc() -> None:
    assert iso_utc(datetime(2026, 11, 3, 9, 0)) == "2026-11-03T09:00:00Z"


def test_every_warning_code_value_is_its_name() -> None:
    assert all(code.value == code.name for code in WarningCode)


def test_tool_warning_of_keeps_details() -> None:
    warning = ToolWarning.of(WarningCode.STALE_DATA, "Old.", last_bar="2026-10-01", behind=24)
    assert warning.code == "STALE_DATA"
    assert warning.message == "Old."
    assert warning.details == {"last_bar": "2026-10-01", "behind": 24}


def test_tool_warning_of_without_details_has_none() -> None:
    assert ToolWarning.of(WarningCode.EMPTY_RESULT, "None.").details is None


def test_meta_defaults() -> None:
    meta = Meta(cost=0, cached=True)
    assert meta.source == "EODHD"
    assert meta.endpoints == []
    assert meta.fetched_at is None
    assert meta.notes == []


def test_build_response_sums_cost_and_lists_endpoints_once_in_order() -> None:
    responses = [
        api_response("eod", cost=1),
        api_response("search", cost=1),
        api_response("eod", cost=1),
    ]
    result = build_response({"rows": 3}, responses)
    assert result.data == {"rows": 3}
    assert result.meta.cost == 3
    assert result.meta.endpoints == ["eod", "search"]


def test_build_response_is_cached_only_when_every_answer_was() -> None:
    assert build_response(None, [api_response(cached=True, cost=0)] * 2).meta.cached is True
    mixed = [api_response(cached=True, cost=0), api_response(cached=False)]
    assert build_response(None, mixed).meta.cached is False


def test_build_response_without_responses_is_free_and_cached() -> None:
    result = build_response("ok", [])
    assert result.meta.cost == 0
    assert result.meta.cached is True
    assert result.meta.fetched_at is None
    assert result.meta.endpoints == []


def test_build_response_reports_the_oldest_fetch_time() -> None:
    old = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    new = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    result = build_response(None, [api_response(fetched_at=new), api_response(fetched_at=old)])
    assert result.meta.fetched_at == "2026-11-02T08:00:00Z"


def test_build_response_passes_warnings_and_notes_through() -> None:
    warning = ToolWarning.of(WarningCode.DELAYED_QUOTE, "Delayed.")
    result = build_response(None, [api_response()], warnings=[warning], notes=["Raw prices."])
    assert result.warnings == [warning]
    assert result.meta.notes == ["Raw prices."]


def test_response_serialises_to_json() -> None:
    result = build_response({"x": 1}, [api_response()])
    payload = json.loads(result.model_dump_json())
    assert set(payload) == {"data", "warnings", "meta"}
    assert payload["meta"]["source"] == "EODHD"
