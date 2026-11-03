"""get_eod_prices and get_intraday_prices, through the MCP client against a mocked EODHD.

The clock is Tuesday 2026-11-03 15:00 UTC: the US session is open (10:00 in New York, on standard time),
and the latest US session whose bar should exist is Monday 2026-11-02. Every fixture is synthetic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.semantics import PRICE_BASIS_NOTES
from eodhd_guard_mcp.server import Deps
from eodhd_guard_mcp.tools.prices import fetch_eod_prices, fetch_intraday_prices
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, load_fixture

pytestmark = pytest.mark.anyio

UTC = timezone.utc


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    """The way a model writes a time in an argument: ISO 8601 with a Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def unix(moment: datetime) -> str:
    return str(int(moment.timestamp()))


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return str(getattr(result.content[0], "text", ""))


def codes(body: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in body["warnings"]]


def warning_of(body: dict[str, Any], code: str) -> dict[str, Any]:
    found = [warning for warning in body["warnings"] if warning["code"] == code]
    assert len(found) == 1, f"expected one {code}, warnings were {codes(body)}"
    return found[0]


def between(rows: list[dict[str, Any]], first: str, last: str) -> list[dict[str, Any]]:
    return [row for row in rows if first <= row["date"] <= last]


def bar(day: str, close: float = 50.0, *, volume: int = 1000, flat: bool = False) -> dict[str, Any]:
    """One daily row with invented prices. ``flat`` makes open = high = low = close, like a filler bar."""
    spread = 0.0 if flat else 0.5
    return {
        "date": day,
        "open": close,
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "adjusted_close": close,
        "volume": volume,
    }


@pytest.fixture
def acme() -> list[dict[str, Any]]:
    """ACME.US daily bars, 2026-10-01 to 2026-11-02 (23 sessions)."""
    return load_fixture("tools_prices_eod_acme")


async def call_eod(client: Client, **arguments: Any) -> CallToolResult:
    return await client.call_tool("get_eod_prices", {"symbol": "ACME.US", **arguments})


# --- get_eod_prices: the answer ---------------------------------------------------------------------


async def test_a_range_comes_back_as_bars_with_the_cost_and_the_price_basis_note(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-01", "2026-10-30"))
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    assert not result.is_error
    body = result.structured_content
    assert body is not None
    data = body["data"]
    assert {key: data[key] for key in data if key != "bars"} == {
        "symbol": "ACME.US",
        "period": "d",
        "price_basis": "both",
        "start": "2026-10-01",
        "end": "2026-10-30",
        "first_date": "2026-10-01",
        "last_date": "2026-10-30",
        "count": 22,
    }
    assert len(data["bars"]) == 22
    assert set(data["bars"][0]) == {"date", "open", "high", "low", "close", "adjusted_close", "volume"}
    assert body["warnings"] == []
    assert body["meta"]["cost"] == 1
    assert body["meta"]["endpoints"] == ["eod"]
    assert body["meta"]["cached"] is False
    assert body["meta"]["notes"] == [PRICE_BASIS_NOTES["both"]]
    request = mock_api.requests[0]
    assert request.params == {
        "from": "2026-10-01",
        "to": "2026-10-30",
        "period": "d",
        "order": "a",
        "fmt": "json",
    }
    assert request.api_token == TEST_KEY


async def test_a_symbol_with_an_ampersand_is_encoded_in_the_path(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/M&M.NSE", [bar("2026-10-29")])
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "m&m.nse", "start": "2026-10-26", "end": "2026-10-30"}
    )
    assert not result.is_error
    assert "/eod/M%26M.NSE?" in mock_api.requests[0].raw_path


async def test_without_dates_the_range_is_the_last_30_days_ending_today(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-04", "2026-11-03"))
    result = await call_eod(mcp_client)
    body = result.structured_content
    assert body is not None
    assert (mock_api.requests[0].params["from"], mock_api.requests[0].params["to"]) == (
        "2026-10-04",
        "2026-11-03",
    )
    assert (body["data"]["start"], body["data"]["end"]) == ("2026-10-04", "2026-11-03")
    assert body["data"]["last_date"] == "2026-11-02"
    assert body["warnings"] == []


async def test_an_end_date_in_the_future_is_clamped_to_today_with_a_warning(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-20", "2026-11-03"))
    result = await call_eod(mcp_client, start="2026-10-20", end="2026-12-31")
    body = result.structured_content
    assert body is not None
    assert mock_api.requests[0].params["to"] == "2026-11-03"
    assert body["data"]["end"] == "2026-11-03"
    clamped = warning_of(body, "RANGE_CLAMPED")
    assert clamped["details"] == {"requested_end": "2026-12-31", "end": "2026-11-03"}


async def test_a_repeated_call_is_a_cache_hit_at_no_cost(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-01", "2026-10-30"))
    first = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    second = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    assert first.structured_content is not None
    assert second.structured_content is not None
    assert first.structured_content["meta"]["cost"] == 1
    assert second.structured_content["meta"]["cost"] == 0
    assert second.structured_content["meta"]["cached"] is True
    assert second.structured_content["data"] == first.structured_content["data"]
    assert mock_api.count("eod/") == 1
    assert deps.ledger.spent_today() == 1


@pytest.mark.parametrize(
    ("end", "cached_until_hours"),
    [("2026-11-03", 6), ("2026-11-02", 6), ("2026-11-01", 24), ("2026-10-30", 24)],
)
async def test_the_cache_lifetime_is_6_hours_near_today_and_a_day_for_older_ranges(
    mcp_client: Client,
    mock_api: MockEODHD,
    clock: FixedClock,
    acme: list[dict[str, Any]],
    end: str,
    cached_until_hours: int,
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-01", end))
    await call_eod(mcp_client, start="2026-10-01", end=end)
    clock.advance(hours=cached_until_hours - 0.1)
    await call_eod(mcp_client, start="2026-10-01", end=end)
    assert mock_api.count("eod/") == 1
    clock.advance(hours=0.2)
    await call_eod(mcp_client, start="2026-10-01", end=end)
    assert mock_api.count("eod/") == 2


# --- get_eod_prices: price basis ----------------------------------------------------------------------


async def test_the_raw_basis_leaves_out_adjusted_close(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-01", "2026-10-30"))
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30", price_basis="raw")
    body = result.structured_content
    assert body is not None
    assert "adjusted_close" not in body["data"]["bars"][0]
    assert body["data"]["price_basis"] == "raw"
    assert body["meta"]["notes"] == [PRICE_BASIS_NOTES["raw"]]


async def test_the_adjusted_basis_scales_the_prices_and_says_so(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    rows = between(acme, "2026-10-01", "2026-10-30")
    mock_api.add("eod/ACME.US", rows)
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30", price_basis="adjusted")
    body = result.structured_content
    assert body is not None
    first = body["data"]["bars"][0]
    assert first["close"] == rows[0]["adjusted_close"]
    assert first["open"] == pytest.approx(rows[0]["open"] * rows[0]["adjusted_close"] / rows[0]["close"])
    assert body["meta"]["notes"] == [PRICE_BASIS_NOTES["adjusted"]]


async def test_the_adjusted_basis_reports_bars_without_an_adjusted_close_as_null(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    missing = bar("2026-10-29")
    del missing["adjusted_close"]
    mock_api.add("eod/ACME.US", [missing, bar("2026-10-30")])
    result = await call_eod(mcp_client, start="2026-10-29", end="2026-10-30", price_basis="adjusted")
    body = result.structured_content
    assert body is not None
    assert body["data"]["bars"][0]["close"] is None
    assert body["data"]["bars"][1]["close"] == 50.0
    assert any("1 bar(s) have no adjusted_close" in note for note in body["meta"]["notes"])


# --- get_eod_prices: input and coverage ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"symbol": "ACME"}, "no exchange suffix"),
        ({"symbol": "ACME.US", "start": "2026-13-01"}, "start: received '2026-13-01'"),
        ({"symbol": "ACME.US", "end": "03/11/2026"}, "end: received '03/11/2026'"),
        ({"symbol": "ACME.US", "start": "2026-10-30", "end": "2026-10-01"}, "after the end date"),
        ({"symbol": "ACME.US", "start": "2026-12-01"}, "in the future"),
    ],
)
async def test_bad_input_is_refused_before_any_call_is_made(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any], expected: str
) -> None:
    result = await mcp_client.call_tool("get_eod_prices", arguments)
    text = error_text(result)
    assert "INVALID_PARAMS" in text
    assert expected in text
    assert mock_api.requests == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"period": "x"},
        {"price_basis": "x"},
        {"max_rows": 0},
        {"max_rows": 5001},
        {"include_filler_bars": "maybe"},
    ],
)
async def test_values_outside_the_declared_choices_never_reach_the_api(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any]
) -> None:
    result = await call_eod(mcp_client, **arguments)
    assert result.is_error
    assert mock_api.requests == []


async def test_the_row_cap_is_checked_again_when_the_logic_is_called_directly(deps: Deps) -> None:
    with pytest.raises(GuardError, match="max_rows") as caught:
        await fetch_eod_prices(deps, symbol="ACME.US", max_rows=0)
    assert caught.value.code is ErrorCode.INVALID_PARAMS


@pytest.mark.parametrize("symbol", ["7203.TSE", "EMAAR.AE"])
async def test_markets_without_coverage_are_refused_without_a_call(
    mcp_client: Client, mock_api: MockEODHD, symbol: str
) -> None:
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": symbol})
    text = error_text(result)
    assert "NOT_COVERED" in text
    assert "EODHD_GUARD_ALLOW_UNCOVERED=1" in text
    assert mock_api.requests == []


async def test_the_coverage_check_can_be_switched_off(deps: Deps, mock_api: MockEODHD) -> None:
    mock_api.add("eod/7203.TSE", status=404, text="Ticker Not Found.")
    allowing = replace(deps, settings=replace(deps.settings, allow_uncovered=True))
    with pytest.raises(GuardError) as caught:
        await fetch_eod_prices(allowing, symbol="7203.TSE")
    assert caught.value.code is ErrorCode.NOT_FOUND
    assert mock_api.count("eod/7203.TSE") == 1


# --- get_eod_prices: the guards -----------------------------------------------------------------------


async def test_a_404_becomes_not_found_with_the_tools_that_find_the_right_code(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/NOPE.US", status=404, text="Ticker Not Found.")
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "NOPE.US"})
    text = error_text(result)
    assert "NOT_FOUND" in text
    assert "Ticker Not Found." in text
    assert "search_symbols" in text
    assert "list_exchange_symbols(delisted=true)" in text
    assert "hyphen" not in text


async def test_a_dotted_share_class_gets_the_hyphen_hint(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/BRK.B", status=404, text="Ticker Not Found.")
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "BRK.B"})
    text = error_text(result)
    assert "NOT_FOUND" in text
    assert "read as code BRK on exchange B" in text
    assert "hyphen, as in BRK-B.US" in text
    assert "search_symbols" in text


@pytest.mark.parametrize("body", [{"code": "ACME.US", "note": "no bars"}, "oops", 7, None])
async def test_an_answer_that_is_not_a_list_of_bars_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, body: Any
) -> None:
    mock_api.add("eod/ACME.US", body, text=None if body is not None else "null")
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    assert "BAD_RESPONSE" in error_text(result)


async def test_rows_outside_the_range_are_dropped_with_a_warning(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", acme)  # asked for October, EODHD also sends 2026-11-02
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    body = result.structured_content
    assert body is not None
    assert body["data"]["count"] == 22
    assert body["data"]["last_date"] == "2026-10-30"
    trimmed = warning_of(body, "RANGE_TRIMMED")
    assert trimmed["details"] == {"dropped": 1, "first": "2026-11-02", "last": "2026-11-02"}


async def test_weekly_bars_are_kept_when_their_week_overlaps_the_range(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    # EODHD may label a weekly bar with its Monday or its Friday; the week of 2026-10-05 overlaps the range.
    rows = [bar("2026-09-28"), bar("2026-10-05"), bar("2026-10-16"), bar("2026-10-19"), bar("2026-10-26")]
    mock_api.add("eod/ACME.US", rows)
    result = await call_eod(mcp_client, start="2026-10-07", end="2026-10-21", period="w")
    body = result.structured_content
    assert body is not None
    assert [row["date"] for row in body["data"]["bars"]] == ["2026-10-05", "2026-10-16", "2026-10-19"]
    assert warning_of(body, "RANGE_TRIMMED")["details"]["dropped"] == 2
    assert mock_api.requests[0].params["period"] == "w"


async def test_monthly_bars_are_kept_when_their_month_overlaps_the_range(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    rows = [bar("2026-08-01"), bar("2026-09-01"), bar("2026-10-31"), bar("2026-11-01")]
    mock_api.add("eod/ACME.US", rows)
    result = await call_eod(mcp_client, start="2026-09-15", end="2026-10-20", period="m")
    body = result.structured_content
    assert body is not None
    assert [row["date"] for row in body["data"]["bars"]] == ["2026-09-01", "2026-10-31"]
    assert warning_of(body, "RANGE_TRIMMED")["details"]["dropped"] == 2


async def test_a_label_at_the_edge_of_the_calendar_is_dropped_without_crashing(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-10-29"), bar("9999-12-31")])
    result = await call_eod(mcp_client, start="2026-10-26", end="2026-10-30", period="w")
    body = result.structured_content
    assert body is not None
    assert [row["date"] for row in body["data"]["bars"]] == ["2026-10-29"]
    assert warning_of(body, "RANGE_TRIMMED")["details"]["dropped"] == 1


async def test_an_empty_answer_for_a_range_with_trading_days_is_no_data(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add("eod/ACME.US", [])
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    text = error_text(result)
    assert "NO_DATA" in text
    assert "HTTP 200 with no bars" in text
    assert "delisted, renamed or not covered" in text
    assert "check_symbol" in text
    assert "counted as 1 API call(s)" in text
    assert deps.ledger.spent_today() == 1


async def test_no_data_also_covers_an_answer_whose_rows_are_all_outside_the_range(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", [bar("2025-01-02"), bar("2025-01-03")])
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    text = error_text(result)
    assert "NO_DATA" in text
    assert "2 bar(s) dated outside the range" in text


async def test_a_cached_empty_answer_is_still_no_data_and_costs_nothing(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add("eod/ACME.US", [])
    await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    assert "came from the cache (cost 0)" in error_text(result)
    assert deps.ledger.spent_today() == 1


async def test_an_empty_range_without_trading_days_is_an_explicit_empty_result(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", [])
    result = await call_eod(mcp_client, start="2026-10-31", end="2026-11-01")  # a Saturday and a Sunday
    body = result.structured_content
    assert body is not None
    assert body["data"]["bars"] == []
    assert (body["data"]["count"], body["data"]["first_date"], body["data"]["last_date"]) == (0, None, None)
    assert codes(body) == ["EMPTY_RESULT"]
    assert body["meta"]["cost"] == 1


async def test_holiday_filler_bars_are_dropped_and_reported(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_prices_eod_filler_acme"))
    result = await call_eod(mcp_client, start="2026-09-03", end="2026-09-09")
    body = result.structured_content
    assert body is not None
    assert [row["date"] for row in body["data"]["bars"]] == [
        "2026-09-03",
        "2026-09-04",
        "2026-09-08",
        "2026-09-09",
    ]
    filler = warning_of(body, "FILLER_BARS_REMOVED")
    assert filler["details"] == {"removed": 1, "dates": ["2026-09-07"]}
    assert "include_filler_bars=true" in filler["message"]
    assert codes(body) == ["FILLER_BARS_REMOVED"]


async def test_filler_bars_stay_when_asked_for(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_prices_eod_filler_acme"))
    result = await call_eod(mcp_client, start="2026-09-03", end="2026-09-09", include_filler_bars=True)
    body = result.structured_content
    assert body is not None
    assert body["data"]["count"] == 5
    assert body["warnings"] == []


async def test_a_range_made_only_of_filler_says_no_real_day_remains(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-09-07", volume=0, flat=True)])
    result = await call_eod(mcp_client, start="2026-09-07", end="2026-09-07")
    body = result.structured_content
    assert body is not None
    assert body["data"]["bars"] == []
    assert codes(body) == ["FILLER_BARS_REMOVED"]
    assert "Every bar in the range was filler" in body["warnings"][0]["message"]
    assert "check_symbol" in body["warnings"][0]["message"]


async def test_many_filler_bars_are_listed_with_a_count_of_the_rest(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    days = [f"2026-09-{day:02d}" for day in range(1, 13)]
    mock_api.add("eod/ACME.US", [bar(day, volume=0, flat=True) for day in days] + [bar("2026-09-14")])
    result = await call_eod(mcp_client, start="2026-09-01", end="2026-09-14")
    body = result.structured_content
    assert body is not None
    filler = warning_of(body, "FILLER_BARS_REMOVED")
    assert filler["details"]["removed"] == 12
    assert len(filler["details"]["dates"]) == 10
    assert "and 2 more" in filler["message"]
    assert [row["date"] for row in body["data"]["bars"]] == ["2026-09-14"]


# --- get_eod_prices: stale data ----------------------------------------------------------------------


async def test_a_frozen_code_returns_its_bars_with_a_stale_data_warning(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    # ROG.SW: HTTP 200, plausible bars, but the newest is 2026-03-25 and the code is dead.
    mock_api.add("eod/ROG.SW", load_fixture("tools_prices_eod_frozen_rog"))
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "ROG.SW", "start": "2026-03-01"})
    assert not result.is_error
    body = result.structured_content
    assert body is not None
    assert body["data"]["count"] == 18
    assert body["data"]["last_date"] == "2026-03-25"
    assert body["data"]["end"] == "2026-11-03"
    stale = warning_of(body, "STALE_DATA")
    assert stale["details"] == {
        "symbol": "ROG.SW",
        "last_bar": "2026-03-25",
        "expected_latest": "2026-11-02",
        "sessions_behind": 158,
    }
    assert "check_symbol" in stale["message"]
    assert "search_symbols" in stale["message"]
    assert "HTTP 200" in stale["message"]


async def test_the_default_window_of_a_frozen_code_is_no_data(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ROG.SW", [])  # nothing in the last 30 days: the series stopped months ago
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "ROG.SW"})
    assert "NO_DATA" in error_text(result)


async def test_fresh_bars_carry_no_stale_data_warning(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-20", "2026-11-03"))
    result = await call_eod(mcp_client, start="2026-10-20")
    body = result.structured_content
    assert body is not None
    assert body["data"]["last_date"] == "2026-11-02"
    assert body["warnings"] == []


@pytest.mark.parametrize(
    ("last_bar", "sessions_behind", "stale"),
    [("2026-10-29", 2, False), ("2026-10-28", 3, True), ("2026-10-20", 9, True)],
)
async def test_bars_are_stale_from_three_sessions_behind(
    mcp_client: Client,
    mock_api: MockEODHD,
    last_bar: str,
    sessions_behind: int,
    stale: bool,
) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-10-14"), bar(last_bar)])
    result = await call_eod(mcp_client, start="2026-10-14")
    body = result.structured_content
    assert body is not None
    assert ("STALE_DATA" in codes(body)) is stale
    if stale:
        assert warning_of(body, "STALE_DATA")["details"]["sessions_behind"] == sessions_behind


async def test_the_stale_threshold_comes_from_the_settings(deps: Deps, mock_api: MockEODHD) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-10-14"), bar("2026-10-29")])  # 2 sessions behind
    loose = await fetch_eod_prices(deps, symbol="ACME.US", start="2026-10-14")
    strict_deps = replace(deps, settings=replace(deps.settings, stale_sessions=2))
    strict = await fetch_eod_prices(strict_deps, symbol="ACME.US", start="2026-10-14")
    assert [w.code for w in loose.warnings] == []
    assert [w.code for w in strict.warnings] == ["STALE_DATA"]


@pytest.mark.parametrize(
    ("end", "period", "checked"),
    [
        ("2026-10-27", "d", True),  # 7 days before today: the check still runs
        ("2026-10-26", "d", False),  # 8 days before today: a historical query
        ("2026-10-27", "w", False),  # weekly and monthly bars lag by design
        ("2026-10-27", "m", False),
    ],
)
async def test_the_stale_check_runs_only_on_recent_daily_ranges(
    mcp_client: Client, mock_api: MockEODHD, end: str, period: str, checked: bool
) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-10-14"), bar("2026-10-15")])
    result = await call_eod(mcp_client, start="2026-10-10", end=end, period=period)
    body = result.structured_content
    assert body is not None
    assert ("STALE_DATA" in codes(body)) is checked


async def test_filler_bars_never_make_a_dead_line_look_fresh(mcp_client: Client, mock_api: MockEODHD) -> None:
    fillers = [
        bar(day, volume=0, flat=True) for day in ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")
    ]
    mock_api.add("eod/ACME.US", [bar("2026-10-20"), *fillers])
    dropped = await call_eod(mcp_client, start="2026-10-14")
    kept = await call_eod(mcp_client, start="2026-10-14", include_filler_bars=True)
    dropped_body, kept_body = dropped.structured_content, kept.structured_content
    assert dropped_body is not None
    assert kept_body is not None
    assert "STALE_DATA" in codes(dropped_body)
    assert "FILLER_BARS_REMOVED" in codes(dropped_body)
    assert kept_body["data"]["last_date"] == "2026-10-30"
    assert warning_of(kept_body, "STALE_DATA")["details"]["last_bar"] == "2026-10-20"


async def test_the_session_calendar_of_the_symbols_market_is_used(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    # At 15:00 UTC it is 20:30 in India, five hours after the NSE close, so today's bar is already expected:
    # a bar from 2026-10-29 is 3 NSE sessions behind, but only 2 behind on the US calendar.
    mock_api.add("eod/UMBRELLA.NSE", [bar("2026-10-29")])
    mock_api.add("eod/ACME.US", [bar("2026-10-29")])
    india = await mcp_client.call_tool("get_eod_prices", {"symbol": "UMBRELLA.NSE", "start": "2026-10-26"})
    america = await call_eod(mcp_client, start="2026-10-26")
    assert india.structured_content is not None
    assert america.structured_content is not None
    stale = warning_of(india.structured_content, "STALE_DATA")
    assert (stale["details"]["expected_latest"], stale["details"]["sessions_behind"]) == ("2026-11-03", 3)
    assert "STALE_DATA" not in codes(america.structured_content)


# --- get_eod_prices: spin-offs, currency, truncation ---------------------------------------------------


async def test_a_range_starting_before_a_known_spin_off_is_flagged(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    rows = [
        bar(day)
        for day in ("2023-09-28", "2023-09-29", "2023-10-02", "2023-10-03", "2023-10-04", "2023-10-05")
    ]
    mock_api.add("eod/NOVN.SW", rows)
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "NOVN.SW", "start": "2023-09-28", "end": "2023-10-05"}
    )
    body = result.structured_content
    assert body is not None
    spinoff = warning_of(body, "SPINOFF_ADJUSTED_CLOSE")
    assert spinoff["details"]["event"] == "Sandoz spin-off"
    assert spinoff["details"]["event_date"] == "2023-10-04"


async def test_a_range_starting_on_the_spin_off_date_is_not_flagged(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/NOVN.SW", [bar("2023-10-04"), bar("2023-10-05")])
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "NOVN.SW", "start": "2023-10-04", "end": "2023-10-05"}
    )
    body = result.structured_content
    assert body is not None
    assert body["warnings"] == []


async def test_spin_off_warnings_look_at_the_bars_that_are_returned(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    rows = [
        bar(day)
        for day in ("2023-09-28", "2023-09-29", "2023-10-02", "2023-10-03", "2023-10-04", "2023-10-05")
    ]
    mock_api.add("eod/NOVN.SW", rows)
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "NOVN.SW", "start": "2023-09-28", "end": "2023-10-05", "max_rows": 2}
    )
    body = result.structured_content
    assert body is not None
    assert codes(body) == ["TRUNCATED"]  # the two newest bars start on the event date


async def test_london_lines_carry_the_pence_note(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/GLOBEX.LSE", load_fixture("tools_prices_eod_globex_lse"))
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "GLOBEX.LSE", "start": "2026-10-26", "end": "2026-11-02"}
    )
    body = result.structured_content
    assert body is not None
    note = warning_of(body, "CURRENCY_NOTE")
    assert note["details"] == {"symbols": ["GLOBEX.LSE"], "currency": "GBX"}
    assert "pence" in note["message"]


async def test_other_markets_and_empty_london_ranges_carry_no_pence_note(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-01", "2026-10-30"))
    mock_api.add("eod/GLOBEX.LSE", [])
    us = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30")
    london = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "GLOBEX.LSE", "start": "2026-10-31", "end": "2026-11-01"}
    )
    assert us.structured_content is not None
    assert london.structured_content is not None
    assert "CURRENCY_NOTE" not in codes(us.structured_content)
    assert "CURRENCY_NOTE" not in codes(london.structured_content)


async def test_more_rows_than_max_rows_keep_the_most_recent_ones(
    mcp_client: Client, mock_api: MockEODHD, acme: list[dict[str, Any]]
) -> None:
    mock_api.add("eod/ACME.US", between(acme, "2026-10-01", "2026-10-30"))
    result = await call_eod(mcp_client, start="2026-10-01", end="2026-10-30", max_rows=5)
    body = result.structured_content
    assert body is not None
    assert (body["data"]["count"], body["data"]["first_date"], body["data"]["last_date"]) == (
        5,
        "2026-10-26",
        "2026-10-30",
    )
    truncated = warning_of(body, "TRUNCATED")
    assert truncated["details"] == {"total": 22, "returned": 5, "max_rows": 5, "kept": "most_recent"}
    assert "starting at 2026-10-26" in truncated["message"]


# --- get_intraday_prices ------------------------------------------------------------------------------


@pytest.fixture
def intraday() -> list[dict[str, Any]]:
    """ACME.US 5-minute bars on Fri 2026-10-30 (13:30-20:00 UTC) and Mon 2026-11-02 (14:30-21:00 UTC)."""
    return load_fixture("tools_prices_intraday_acme")


def on(rows: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    return [row for row in rows if row["datetime"].startswith(day)]


def minute_bar(moment: datetime, close: float = 51.0) -> dict[str, Any]:
    """One intraday row with invented prices, stamped ``moment``."""
    return {
        "timestamp": int(moment.timestamp()),
        "gmtoffset": 0,
        "datetime": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "open": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": 100,
    }


async def call_intraday(client: Client, **arguments: Any) -> CallToolResult:
    return await client.call_tool("get_intraday_prices", {"symbol": "ACME.US", **arguments})


async def test_intraday_bars_come_back_in_utc_for_five_calls(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    opening, closing = utc(2026, 10, 30, 13, 30), utc(2026, 10, 30, 20, 0)
    mock_api.add("intraday/ACME.US", on(intraday, "2026-10-30"))
    result = await call_intraday(mcp_client, start=stamp(opening), end=stamp(closing))
    assert not result.is_error
    body = result.structured_content
    assert body is not None
    data = body["data"]
    assert {key: data[key] for key in data if key != "bars"} == {
        "symbol": "ACME.US",
        "interval": "5m",
        "start": "2026-10-30T13:30:00Z",
        "end": "2026-10-30T20:00:00Z",
        "first_datetime_utc": "2026-10-30T13:30:00Z",
        "last_datetime_utc": "2026-10-30T20:00:00Z",
        "count": 79,
    }
    assert set(data["bars"][0]) == {"datetime_utc", "open", "high", "low", "close", "volume"}
    assert data["bars"][0]["datetime_utc"] == "2026-10-30T13:30:00Z"
    assert body["warnings"] == []
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (5, ["intraday"])
    assert any("UTC" in note for note in body["meta"]["notes"])
    assert mock_api.requests[0].params == {
        "interval": "5m",
        "from": unix(opening),
        "to": unix(closing),
        "fmt": "json",
    }


@pytest.mark.parametrize(("interval", "days"), [("1m", 1), ("5m", 3), ("1h", 14)])
async def test_the_default_window_is_one_three_or_fourteen_days_back_from_now(
    mcp_client: Client, mock_api: MockEODHD, interval: str, days: int
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, interval=interval)
    body = result.structured_content
    assert body is not None
    now = utc(2026, 11, 3, 15, 0)  # the clock stands on a quarter hour, so the end is exactly now
    assert mock_api.requests[0].params["interval"] == interval
    assert mock_api.requests[0].params["to"] == unix(now)
    assert mock_api.requests[0].params["from"] == unix(now - timedelta(days=days))
    assert body["data"]["end"] == "2026-11-03T15:00:00Z"
    assert body["data"]["start"] == stamp(now - timedelta(days=days))


async def test_a_default_end_rounds_up_to_the_quarter_hour_so_repeated_calls_share_the_cache(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock, intraday: list[dict[str, Any]]
) -> None:
    mock_api.add("intraday/ACME.US", on(intraday, "2026-11-02"))
    clock.advance(minutes=7, seconds=30)  # 15:07:30
    first = await call_intraday(mcp_client)
    assert mock_api.requests[0].params["to"] == unix(utc(2026, 11, 3, 15, 15))
    clock.advance(minutes=5)  # 15:12:30, the same quarter hour
    second = await call_intraday(mcp_client)
    assert mock_api.count("intraday/") == 1
    assert second.structured_content is not None
    assert second.structured_content["meta"]["cost"] == 0
    assert first.structured_content is not None
    assert second.structured_content["data"] == first.structured_content["data"]
    clock.advance(minutes=4)  # 15:16:30, the next quarter hour
    await call_intraday(mcp_client)
    assert mock_api.count("intraday/") == 2
    assert mock_api.requests[1].params["to"] == unix(utc(2026, 11, 3, 15, 30))


@pytest.mark.parametrize(("interval", "longest"), [("1m", 120), ("5m", 600), ("1h", 7200)])
async def test_the_longest_window_depends_on_the_interval(
    mcp_client: Client, mock_api: MockEODHD, interval: str, longest: int
) -> None:
    mock_api.add("intraday/ACME.US", [])
    end = utc(2026, 10, 30)
    ok = await call_intraday(
        mcp_client, interval=interval, start=stamp(end - timedelta(days=longest)), end=stamp(end)
    )
    assert not ok.is_error
    too_long = await call_intraday(
        mcp_client, interval=interval, start=stamp(end - timedelta(days=longest, hours=1)), end=stamp(end)
    )
    text = error_text(too_long)
    assert "INVALID_PARAMS" in text
    assert f"more than the {longest} allowed for {interval} bars" in text
    assert mock_api.count("intraday/") == 1


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"start": "2026-10-30T20:00:00Z", "end": "2026-10-30T13:30:00Z"}, "is not before the end"),
        ({"start": "2026-10-30T13:30:00Z", "end": "2026-10-30T13:30:00Z"}, "is not before the end"),
        ({"start": "2026-10-31", "end": "2026-10-30"}, "is not before the end 2026-10-30T23:59:59Z"),
        ({"start": "2026-11-03T15:00:01Z"}, "it is in the future"),
        ({"start": "yesterday"}, "start: received 'yesterday'"),
        ({"end": "2026-02-30"}, "end: received '2026-02-30'"),
    ],
)
async def test_bad_windows_are_refused_before_any_call_is_made(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any], expected: str
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, **arguments)
    text = error_text(result)
    assert "INVALID_PARAMS" in text
    assert expected in text
    assert mock_api.requests == []


async def test_a_time_without_an_offset_is_read_as_utc(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, start="2026-10-30 13:30", end="2026-10-30T14:00")
    assert not result.is_error
    assert mock_api.requests[0].params["from"] == unix(utc(2026, 10, 30, 13, 30))
    assert mock_api.requests[0].params["to"] == unix(utc(2026, 10, 30, 14, 0))


@pytest.mark.parametrize(
    "arguments", [{"interval": "2m"}, {"interval": "1d"}, {"max_rows": 0}, {"max_rows": 5001}]
)
async def test_intraday_choices_outside_the_declared_values_never_reach_the_api(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any]
) -> None:
    result = await call_intraday(mcp_client, **arguments)
    assert result.is_error
    assert mock_api.requests == []


async def test_the_row_cap_is_checked_again_when_the_intraday_logic_is_called_directly(deps: Deps) -> None:
    with pytest.raises(GuardError, match="max_rows"):
        await fetch_intraday_prices(deps, symbol="ACME.US", max_rows=5001)


async def test_an_end_in_the_future_is_pulled_back_with_a_warning(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, end="2026-11-10T00:00:00Z")
    body = result.structured_content
    assert body is not None
    assert body["data"]["end"] == "2026-11-03T15:00:00Z"
    clamped = warning_of(body, "RANGE_CLAMPED")
    assert clamped["details"] == {"requested_end": "2026-11-10T00:00:00Z", "end": "2026-11-03T15:00:00Z"}
    assert mock_api.requests[0].params["to"] == unix(utc(2026, 11, 3, 15, 0))


async def test_a_bare_end_date_means_the_end_of_that_utc_day(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    mock_api.add("intraday/ACME.US", on(intraday, "2026-10-30"))
    result = await call_intraday(mcp_client, start="2026-10-29", end="2026-10-30")
    body = result.structured_content
    assert body is not None
    assert (body["data"]["start"], body["data"]["end"]) == ("2026-10-29T00:00:00Z", "2026-10-30T23:59:59Z")
    assert body["data"]["count"] == 79
    assert body["warnings"] == []
    assert mock_api.requests[0].params["from"] == unix(utc(2026, 10, 29))
    assert mock_api.requests[0].params["to"] == unix(utc(2026, 10, 30, 23, 59, 59))


async def test_the_same_bare_date_as_start_and_end_returns_that_days_bars(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    mock_api.add("intraday/ACME.US", on(intraday, "2026-10-30"))
    result = await call_intraday(mcp_client, start="2026-10-30", end="2026-10-30")
    body = result.structured_content
    assert body is not None
    data = body["data"]
    assert (data["start"], data["end"]) == ("2026-10-30T00:00:00Z", "2026-10-30T23:59:59Z")
    assert (data["count"], data["first_datetime_utc"], data["last_datetime_utc"]) == (
        79,
        "2026-10-30T13:30:00Z",
        "2026-10-30T20:00:00Z",
    )
    assert body["warnings"] == []


async def test_a_bare_end_date_keeps_the_last_minute_of_the_day_and_drops_the_next_midnight(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    rows = [
        minute_bar(utc(2026, 10, 30, 23, 58)),
        minute_bar(utc(2026, 10, 30, 23, 59)),
        minute_bar(utc(2026, 10, 30, 23, 59, 59)),
        minute_bar(utc(2026, 10, 31, 0, 0)),
    ]
    mock_api.add("intraday/ACME.US", rows)
    result = await call_intraday(mcp_client, interval="1m", start="2026-10-30", end="2026-10-30")
    body = result.structured_content
    assert body is not None
    assert [row["datetime_utc"] for row in body["data"]["bars"]] == [
        "2026-10-30T23:58:00Z",
        "2026-10-30T23:59:00Z",
        "2026-10-30T23:59:59Z",
    ]
    assert warning_of(body, "RANGE_TRIMMED")["details"] == {"dropped": 1}


async def test_a_bare_start_date_is_midnight_utc(mcp_client: Client, mock_api: MockEODHD) -> None:
    rows = [minute_bar(utc(2026, 10, 29, 23, 59)), minute_bar(utc(2026, 10, 30, 0, 0))]
    mock_api.add("intraday/ACME.US", rows)
    result = await call_intraday(mcp_client, interval="1m", start="2026-10-30", end="2026-10-30")
    body = result.structured_content
    assert body is not None
    assert [row["datetime_utc"] for row in body["data"]["bars"]] == ["2026-10-30T00:00:00Z"]
    assert warning_of(body, "RANGE_TRIMMED")["details"] == {"dropped": 1}


async def test_an_end_with_a_time_stays_exact(mcp_client: Client, mock_api: MockEODHD) -> None:
    rows = [
        minute_bar(utc(2026, 10, 30, 23, 57)),
        minute_bar(utc(2026, 10, 30, 23, 58)),
        minute_bar(utc(2026, 10, 30, 23, 59)),
    ]
    mock_api.add("intraday/ACME.US", rows)
    result = await call_intraday(
        mcp_client, interval="1m", start="2026-10-30T23:00:00Z", end="2026-10-30T23:58:00Z"
    )
    body = result.structured_content
    assert body is not None
    assert body["data"]["end"] == "2026-10-30T23:58:00Z"
    assert [row["datetime_utc"] for row in body["data"]["bars"]] == [
        "2026-10-30T23:57:00Z",
        "2026-10-30T23:58:00Z",
    ]
    assert mock_api.requests[0].params["to"] == unix(utc(2026, 10, 30, 23, 58))
    assert warning_of(body, "RANGE_TRIMMED")["details"] == {"dropped": 1}


@pytest.mark.parametrize("end", ["2026-10-30T00:00:00Z", "2026-10-30T00:00", "2026-10-30 00:00"])
async def test_midnight_written_with_a_time_is_not_taken_for_a_bare_date(
    mcp_client: Client, mock_api: MockEODHD, end: str
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, start="2026-10-29", end=end)
    body = result.structured_content
    assert body is not None
    assert body["data"]["end"] == "2026-10-30T00:00:00Z"
    assert mock_api.requests[0].params["to"] == unix(utc(2026, 10, 30))


@pytest.mark.parametrize(
    ("interval", "first"),
    [("1m", utc(2026, 10, 30)), ("5m", utc(2026, 10, 28)), ("1h", utc(2026, 10, 17))],
)
async def test_without_a_start_a_bare_end_date_gets_whole_days_back_from_the_next_midnight(
    mcp_client: Client, mock_api: MockEODHD, interval: str, first: datetime
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, interval=interval, end="2026-10-30")
    body = result.structured_content
    assert body is not None
    assert (body["data"]["start"], body["data"]["end"]) == (stamp(first), "2026-10-30T23:59:59Z")
    assert mock_api.requests[0].params["from"] == unix(first)


async def test_the_longest_window_counts_the_whole_last_day_of_a_bare_end_date(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", [])
    ok = await call_intraday(mcp_client, interval="1m", start="2026-06-01", end="2026-09-28")
    assert not ok.is_error  # 119 days and 23:59:59
    too_long = await call_intraday(mcp_client, interval="1m", start="2026-06-01", end="2026-09-29")
    text = error_text(too_long)
    assert "the window spans 121.0 days, more than the 120 allowed for 1m bars" in text
    assert mock_api.count("intraday/") == 1


@pytest.mark.parametrize(
    ("end", "announced"),
    [("2026-11-03", False), ("2026-11-04", True)],
)
async def test_a_bare_end_date_of_today_is_pulled_back_quietly_and_a_later_one_with_a_warning(
    mcp_client: Client, mock_api: MockEODHD, end: str, announced: bool
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, end=end)
    body = result.structured_content
    assert body is not None
    assert body["data"]["end"] == "2026-11-03T15:00:00Z"
    assert ("RANGE_CLAMPED" in codes(body)) is announced
    if announced:
        assert warning_of(body, "RANGE_CLAMPED")["details"]["requested_end"] == "2026-11-04T23:59:59Z"


async def test_a_bare_end_date_on_the_last_day_of_the_calendar_is_pulled_back(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, end="9999-12-31")
    body = result.structured_content
    assert body is not None
    assert body["data"]["end"] == "2026-11-03T15:00:00Z"
    assert warning_of(body, "RANGE_CLAMPED")["details"]["requested_end"] == "9999-12-31T23:59:59Z"


async def test_rows_outside_the_window_are_dropped_with_a_warning(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    mock_api.add("intraday/ACME.US", intraday)  # both days, for a 30-minute window
    result = await call_intraday(mcp_client, start="2026-10-30T13:30:00Z", end="2026-10-30T14:00:00Z")
    body = result.structured_content
    assert body is not None
    assert body["data"]["count"] == 7
    assert body["data"]["last_datetime_utc"] == "2026-10-30T14:00:00Z"
    assert warning_of(body, "RANGE_TRIMMED")["details"] == {"dropped": 151}


async def test_a_window_with_no_bars_is_an_empty_result_not_an_error(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, start="2026-01-05T00:00:00Z", end="2026-01-06T00:00:00Z")
    assert not result.is_error
    body = result.structured_content
    assert body is not None
    assert body["data"]["bars"] == []
    assert (body["data"]["count"], body["data"]["first_datetime_utc"]) == (0, None)
    assert codes(body) == ["NO_INTRADAY_DATA"]
    assert "history depth varies by exchange and plan" in body["warnings"][0]["message"]
    assert body["meta"]["cost"] == 5


async def test_rows_without_prices_are_skipped_and_a_null_volume_is_kept(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    rows = on(intraday, "2026-10-30")[:3]
    no_trade = {**rows[1], "timestamp": rows[1]["timestamp"] + 60, "open": None, "high": None, "low": None}
    no_trade.update(close=None, volume=None)
    quiet = {**rows[2], "timestamp": rows[2]["timestamp"] + 60, "volume": None}
    text_price = {**rows[0], "timestamp": rows[0]["timestamp"] + 120, "close": "NA"}
    absurd = [{**rows[0], "timestamp": 10**400}, {**rows[0], "timestamp": 1e20}]
    unreadable = [{"datetime": "2026-10-30 13:31:00"}, "x", *absurd]
    mock_api.add("intraday/ACME.US", [*rows, no_trade, quiet, text_price, *unreadable])
    result = await call_intraday(
        mcp_client, interval="1m", start="2026-10-30T13:30:00Z", end="2026-10-30T14:00:00Z"
    )
    body = result.structured_content
    assert body is not None
    assert body["data"]["count"] == 4
    assert body["data"]["bars"][-1]["volume"] is None
    assert body["data"]["bars"][-1]["datetime_utc"] == "2026-10-30T13:41:00Z"
    assert any(
        "6 row(s) without prices or a readable timestamp were skipped" in n for n in body["meta"]["notes"]
    )


async def test_whole_number_floats_become_integers_and_duplicates_keep_the_later_row(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    first = on(intraday, "2026-10-30")[0]
    mock_api.add(
        "intraday/ACME.US",
        [
            {**first, "volume": 10},
            {**first, "volume": 2500.0},
            {**first, "timestamp": first["timestamp"] + 300, "volume": 12.5},
        ],
    )
    result = await call_intraday(mcp_client, start="2026-10-30T13:30:00Z", end="2026-10-30T14:00:00Z")
    body = result.structured_content
    assert body is not None
    assert [row["volume"] for row in body["data"]["bars"]] == [2500, 12.5]
    assert isinstance(body["data"]["bars"][0]["volume"], int)


async def test_a_list_of_rows_with_no_readable_timestamp_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", [{"datetime": "2026-10-30 13:30:00", "open": 1}, 5])
    result = await call_intraday(mcp_client, start="2026-10-30T13:30:00Z", end="2026-10-30T14:00:00Z")
    text = error_text(result)
    assert "BAD_RESPONSE" in text
    assert "None of the 2 rows" in text


async def test_an_intraday_answer_that_is_not_a_list_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", {"message": "no data"})
    result = await call_intraday(mcp_client, start="2026-10-30T13:30:00Z", end="2026-10-30T14:00:00Z")
    assert "BAD_RESPONSE" in error_text(result)


async def test_more_bars_than_max_rows_keep_the_most_recent_ones(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    mock_api.add("intraday/ACME.US", on(intraday, "2026-10-30"))
    result = await call_intraday(
        mcp_client, start="2026-10-30T13:30:00Z", end="2026-10-30T20:00:00Z", max_rows=10
    )
    body = result.structured_content
    assert body is not None
    assert (body["data"]["count"], body["data"]["first_datetime_utc"]) == (10, "2026-10-30T19:15:00Z")
    assert warning_of(body, "TRUNCATED")["details"] == {
        "total": 79,
        "returned": 10,
        "max_rows": 10,
        "kept": "most_recent",
    }


async def test_a_403_says_the_plan_lacks_intraday_data(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("intraday/ACME.US", status=403, text="Forbidden")
    result = await call_intraday(mcp_client)
    text = error_text(result)
    assert "PLAN_ERROR" in text
    assert "EOD+Intraday or All-in-One" in text


async def test_an_intraday_404_gets_the_same_search_hint(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("intraday/NOPE.US", status=404, text="Ticker Not Found.")
    result = await mcp_client.call_tool("get_intraday_prices", {"symbol": "NOPE.US"})
    text = error_text(result)
    assert "NOT_FOUND" in text
    assert "No intraday prices for NOPE.US" in text
    assert "search_symbols" in text


@pytest.mark.parametrize("symbol", ["7203.TSE", "EMAAR.AE"])
async def test_intraday_for_markets_without_coverage_is_refused_without_a_call(
    mcp_client: Client, mock_api: MockEODHD, symbol: str
) -> None:
    result = await mcp_client.call_tool("get_intraday_prices", {"symbol": symbol})
    assert "NOT_COVERED" in error_text(result)
    assert mock_api.requests == []


@pytest.mark.parametrize(
    ("end", "cached_until_minutes"),
    [
        ("2026-11-03T14:45:00Z", 15),  # past today's open at 14:30 UTC: bars may still arrive
        ("2026-11-03T14:00:00Z", 24 * 60),  # before the open: nothing of today can be in the window
        ("2026-11-02T22:00:00Z", 24 * 60),
    ],
)
async def test_intraday_answers_are_cached_for_15_minutes_when_the_window_reaches_todays_session_else_a_day(
    mcp_client: Client,
    mock_api: MockEODHD,
    clock: FixedClock,
    end: str,
    cached_until_minutes: int,
) -> None:
    mock_api.add("intraday/ACME.US", [])
    await call_intraday(mcp_client, start="2026-10-31T00:00:00Z", end=end)
    clock.advance(minutes=cached_until_minutes - 1)
    await call_intraday(mcp_client, start="2026-10-31T00:00:00Z", end=end)
    assert mock_api.count("intraday/") == 1
    clock.advance(minutes=2)
    await call_intraday(mcp_client, start="2026-10-31T00:00:00Z", end=end)
    assert mock_api.count("intraday/") == 2


# --- get_intraday_prices: bars for today ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "state", "warned"),
    [
        ({}, "open", True),  # 10:00 in New York
        ({"hours": 7}, "post_close", True),  # 17:00
        ({"hours": -5}, "pre_open", False),  # 05:00
        ({"days": 4}, "closed_day", False),  # Saturday
    ],
)
async def test_same_day_bars_are_flagged_when_the_session_has_started_and_none_exist(
    mcp_client: Client,
    mock_api: MockEODHD,
    clock: FixedClock,
    intraday: list[dict[str, Any]],
    moment: dict[str, float],
    state: str,
    warned: bool,
) -> None:
    clock.advance(**moment)
    mock_api.add("intraday/ACME.US", on(intraday, "2026-11-02"))
    result = await call_intraday(mcp_client, start="2026-11-02T00:00:00Z")
    body = result.structured_content
    assert body is not None
    assert ("INTRADAY_NOT_SAME_DAY" in codes(body)) is warned
    if warned:
        same_day = warning_of(body, "INTRADAY_NOT_SAME_DAY")
        assert same_day["details"]["session"] == state
        assert same_day["details"]["newest_bar"] == "2026-11-02T21:00:00Z"
        assert same_day["details"]["local_date"] == "2026-11-03"
        assert "get_quote" in same_day["message"]
        assert "after the session, never during it" in same_day["message"]


async def test_a_bar_dated_today_clears_the_same_day_warning(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock, intraday: list[dict[str, Any]]
) -> None:
    clock.advance(hours=7)  # 17:00 in New York, an hour after the close
    today_bar = {
        **intraday[0],
        "timestamp": int(utc(2026, 11, 3, 14, 30).timestamp()),
        "datetime": "2026-11-03 14:30:00",
    }
    mock_api.add("intraday/ACME.US", [*on(intraday, "2026-11-02"), today_bar])
    result = await call_intraday(mcp_client, start="2026-11-02T00:00:00Z")
    body = result.structured_content
    assert body is not None
    assert "INTRADAY_NOT_SAME_DAY" not in codes(body)
    assert body["data"]["last_datetime_utc"] == "2026-11-03T14:30:00Z"


async def test_a_window_that_ends_before_today_is_never_flagged(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    mock_api.add("intraday/ACME.US", on(intraday, "2026-11-02"))
    result = await call_intraday(mcp_client, start="2026-11-02T00:00:00Z", end="2026-11-02T22:00:00Z")
    body = result.structured_content
    assert body is not None
    assert body["warnings"] == []


async def test_a_window_ending_before_todays_session_opens_is_not_flagged_whatever_its_utc_date(
    mcp_client: Client, mock_api: MockEODHD, intraday: list[dict[str, Any]]
) -> None:
    # 03:00 UTC on 2026-11-03 is still the evening of the 2nd in New York: the window ends on a past day.
    mock_api.add("intraday/ACME.US", on(intraday, "2026-11-02"))
    result = await call_intraday(mcp_client, start="2026-11-02T00:00:00Z", end="2026-11-03T03:00:00Z")
    body = result.structured_content
    assert body is not None
    assert "INTRADAY_NOT_SAME_DAY" not in codes(body)


async def test_a_window_ending_in_the_early_hours_of_a_markets_today_does_not_reach_its_session(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    # The end of 2026-11-02 UTC is 00:59:59 on the 3rd in Zurich, where the session opens at 09:00 and it is
    # 16:00 now. Asking for "through yesterday" must not be told that today's bars are missing.
    mock_api.add("intraday/INITECH.SW", [])
    arguments = {"symbol": "INITECH.SW", "start": "2026-11-02", "end": "2026-11-02"}
    result = await mcp_client.call_tool("get_intraday_prices", arguments)
    body = result.structured_content
    assert body is not None
    assert codes(body) == ["NO_INTRADAY_DATA"]
    clock.advance(hours=23)  # the answer is final, so it is cached for a day and not for 15 minutes
    await mcp_client.call_tool("get_intraday_prices", arguments)
    assert mock_api.count("intraday/") == 1


@pytest.mark.parametrize(
    ("end", "reaches"),
    [("2026-11-03T14:29:59Z", False), ("2026-11-03T14:30:00Z", True)],
)
async def test_a_window_reaches_todays_session_from_the_moment_it_opens(
    mcp_client: Client,
    mock_api: MockEODHD,
    intraday: list[dict[str, Any]],
    end: str,
    reaches: bool,
) -> None:
    mock_api.add("intraday/ACME.US", on(intraday, "2026-11-02"))  # New York opens at 14:30 UTC on 2026-11-03
    result = await call_intraday(mcp_client, start="2026-11-02T00:00:00Z", end=end)
    body = result.structured_content
    assert body is not None
    assert ("INTRADAY_NOT_SAME_DAY" in codes(body)) is reaches


@pytest.mark.parametrize(
    ("start", "reaches"),
    [("2026-11-03T21:00:01Z", False), ("2026-11-03T21:00:00Z", True)],
)
async def test_a_window_starting_after_todays_close_does_not_reach_its_session(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock, start: str, reaches: bool
) -> None:
    clock.advance(hours=7)  # 22:00 UTC, an hour after the New York close
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client, start=start)
    body = result.structured_content
    assert body is not None
    assert ("INTRADAY_NOT_SAME_DAY" in codes(body)) is reaches
    assert "NO_INTRADAY_DATA" in codes(body)


async def test_an_empty_window_that_includes_today_gets_both_explanations(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("intraday/ACME.US", [])
    result = await call_intraday(mcp_client)
    body = result.structured_content
    assert body is not None
    assert codes(body) == ["INTRADAY_NOT_SAME_DAY", "NO_INTRADAY_DATA"]
    assert "No bar was returned at all" in warning_of(body, "INTRADAY_NOT_SAME_DAY")["message"]


# --- both tools: the key never comes back ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "text", "code"),
    [
        (200, None, None),
        (401, f"Unauthorized api_token={TEST_KEY}", "AUTH_ERROR"),
        (402, f"Payment Required {TEST_KEY}", "QUOTA_EXCEEDED"),
        (403, f"Forbidden for key {TEST_KEY}", "PLAN_ERROR"),
        (404, f"Ticker Not Found. https://eodhd.test/api/eod/ACME.US?api_token={TEST_KEY}", "NOT_FOUND"),
        (500, f"Server error, token={TEST_KEY}", "UPSTREAM_ERROR"),
    ],
)
@pytest.mark.parametrize("tool", ["get_eod_prices", "get_intraday_prices"])
async def test_the_api_key_appears_in_no_result_text_or_structured_content(
    mcp_client: Client,
    mock_api: MockEODHD,
    acme: list[dict[str, Any]],
    tool: str,
    status: int,
    text: str | None,
    code: str | None,
) -> None:
    arguments = {"symbol": "ACME.US", "start": "2026-10-26", "end": "2026-10-30"}
    path = "eod/ACME.US" if tool == "get_eod_prices" else "intraday/ACME.US"
    if text is None:
        mock_api.add(path, between(acme, "2026-10-26", "2026-10-30") if tool == "get_eod_prices" else [])
    else:
        mock_api.add(path, status=status, text=text)
    result = await mcp_client.call_tool(tool, arguments)
    everything = json.dumps(result.structured_content) + "".join(
        str(getattr(part, "text", "")) for part in result.content
    )
    assert TEST_KEY not in everything
    assert mock_api.requests[0].api_token == TEST_KEY  # the key was sent to EODHD, and only there
    if code is None:
        assert not result.is_error
    else:
        assert code in error_text(result)
