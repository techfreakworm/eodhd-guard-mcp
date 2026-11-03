"""Replay real EODHD answers from a local cache through the tools, and check that every guard fires.

EODHD's licence forbids redistributing its data, so real answers are never committed. To run these
tests, point ``EODHD_GUARD_EVAL_CACHE`` at a git-ignored folder laid out as
``<endpoint>/<name>__<hash>.json``, where each file is a JSON wrapper ``{"status": ..., "body": ...}``
holding one raw answer. Without it, the module is skipped.

The clock is Saturday 2026-10-31 12:00 UTC, the day after the answers were recorded. Assertions
check codes and shapes, never values.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.errors import ErrorCode
from eodhd_guard_mcp.server import build_deps, build_server
from tests.helpers import FixedClock, MockEODHD, RecordingSleep

CACHE = os.environ.get("EODHD_GUARD_EVAL_CACHE", "")

pytestmark = [
    pytest.mark.contract,
    pytest.mark.anyio,
    pytest.mark.skipif(not CACHE, reason="EODHD_GUARD_EVAL_CACHE is not set"),
]

Wrapper = dict[str, Any]
ERROR_CODES = tuple(code.value for code in ErrorCode)


def wrappers(folder: str, prefix: str) -> list[Wrapper]:
    files = sorted(Path(CACHE, folder).glob(f"{prefix}__*.json"))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def cached(folder: str, prefix: str, pick: Callable[[list[Wrapper]], Wrapper] | None = None) -> Wrapper:
    found = wrappers(folder, prefix)
    if not found:
        pytest.skip(f"no cached answer for {folder}/{prefix}")
    return pick(found) if pick else found[0]


def every(folder: str, prefix: str) -> list[tuple[str, Wrapper]]:
    """(symbol, wrapper) for each cached file named ``<prefix>_<symbol>__<hash>.json``."""
    out = []
    for file in sorted(Path(CACHE, folder).glob(f"{prefix}_*__*.json")):
        symbol = file.name.removeprefix(f"{prefix}_").split("__")[0]
        out.append((symbol, json.loads(file.read_text(encoding="utf-8"))))
    return out


def longest(found: list[Wrapper]) -> Wrapper:
    return max(found, key=lambda w: len(w["body"]))


def serve(mock_api: MockEODHD, path: str, wrapper: Wrapper) -> None:
    mock_api.add(path, status=wrapper["status"], text=wrapper["body"])


def codes(result: Any) -> list[str]:
    return [w["code"] for w in result.structured_content["warnings"]]


def error_text(result: Any) -> str:
    assert result.is_error, "expected an error result"
    return str(result.content[0].text)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
async def open_client(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
    tmp_path: Path,
) -> AsyncIterator[Client]:
    """A client whose server skips the coverage pre-check, to see what EODHD itself answers."""
    lax = replace(settings, allow_uncovered=True, state_dir=tmp_path / "lax")
    deps = build_deps(lax, transport=mock_api.transport(), clock=clock, sleep=sleeps)
    try:
        async with Client(build_server(deps=deps)) as client:
            yield client
    finally:
        await deps.aclose()


# Frozen and renamed codes


async def test_a_frozen_code_is_flagged_stale(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "eod/ROG.SW", cached("eod", "eod_ROG.SW", longest))
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "ROG.SW", "start": "2026-01-01"})
    assert not result.is_error
    assert "STALE_DATA" in codes(result)
    assert result.structured_content["data"]["last_date"] == "2026-03-25"


async def test_a_frozen_code_in_a_recent_window_is_no_data(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "eod/ROG.SW", cached("eod", "eod_ROG.SW", longest))
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "ROG.SW"})
    assert "NO_DATA" in error_text(result)


async def test_an_all_na_quote_is_not_a_quote(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "real-time/ROG.SW", cached("real-time", "real-time_ROG.SW"))
    result = await mcp_client.call_tool("get_quote", {"symbols": ["ROG.SW"]})
    text = error_text(result)
    assert "NO_DATA" in text
    assert "no_quote" in text


async def test_a_name_search_finds_the_successor_and_never_the_frozen_line(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    serve(mock_api, "search/Roche", cached("search", "search_Roche", longest))
    result = await mcp_client.call_tool("search_symbols", {"query": "Roche", "max_results": 50})
    assert not result.is_error
    rows = {row["symbol"]: row for row in result.structured_content["data"]["results"]}
    assert "ROG.SW" not in rows
    assert rows["ROP.SW"]["possibly_inactive"] is False


def delisted_row(exchange: str, code: str) -> tuple[Wrapper, dict[str, Any]]:
    for wrapper in wrappers("exchange-symbol-list", f"exchange-symbol-list_{exchange}"):
        rows = json.loads(wrapper["body"]) if wrapper["status"] == 200 else []
        row = next((r for r in rows if r.get("Code") == code), None)
        if row is not None:
            return wrapper, row
    pytest.skip(f"no cached {exchange} symbol list holds {code}")


async def test_check_symbol_finds_roches_new_line(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "eod/ROG.SW", cached("eod", "eod_ROG.SW", longest))
    mock_api.add("search/ROG", [])
    listing, row = delisted_row("SW", "ROG")
    serve(mock_api, "exchange-symbol-list/SW", listing)
    serve(mock_api, f"search/{row['Name']}", cached("search", "search_Roche", longest))
    result = await mcp_client.call_tool("check_symbol", {"symbol": "ROG.SW"})
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["status"] == "stale"
    assert "ROP.SW" in [c["symbol"] for c in data["successor_candidates"]]


# Coverage gaps


async def test_the_uae_is_refused_before_any_call(mcp_client: Client, mock_api: MockEODHD) -> None:
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": "EMAAR.AE"})
    assert "NOT_COVERED" in error_text(result)
    assert mock_api.count() == 0


async def test_an_empty_uae_answer_is_no_data_without_the_pre_check(
    open_client: Client, mock_api: MockEODHD
) -> None:
    serve(mock_api, "eod/EMAAR.AE", cached("eod", "eod_EMAAR.AE"))
    result = await open_client.call_tool("get_eod_prices", {"symbol": "EMAAR.AE"})
    assert "NO_DATA" in error_text(result)


async def test_tokyo_is_not_found_without_the_pre_check(open_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "eod/7203.TSE", cached("eod", "eod_7203.TSE"))
    result = await open_client.call_tool("get_eod_prices", {"symbol": "7203.TSE"})
    assert "NOT_FOUND" in error_text(result)


async def test_an_empty_exchange_list_is_refused(
    mcp_client: Client, open_client: Client, mock_api: MockEODHD
) -> None:
    result = await mcp_client.call_tool("list_exchange_symbols", {"exchange": "AE"})
    assert "NOT_COVERED" in error_text(result)
    serve(mock_api, "exchange-symbol-list/AE", cached("exchange-symbol-list", "exchange-symbol-list_AE"))
    result = await open_client.call_tool("list_exchange_symbols", {"exchange": "AE"})
    assert "NO_DATA" in error_text(result)


# What the prices mean


async def test_holiday_filler_bars_are_removed(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "eod/RELIANCE.NSE", cached("eod", "eod_RELIANCE.NSE", longest))
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "RELIANCE.NSE", "start": "2026-01-01", "end": "2026-10-29"}
    )
    assert not result.is_error
    assert "FILLER_BARS_REMOVED" in codes(result)


@pytest.mark.parametrize(
    ("symbol", "start", "end"),
    [("NOVN.SW", "2019-01-01", "2019-12-31"), ("ULVR.LSE", "2025-01-01", "2025-06-30")],
)
async def test_spin_off_adjusted_closes_are_flagged(
    mcp_client: Client, mock_api: MockEODHD, symbol: str, start: str, end: str
) -> None:
    serve(mock_api, f"eod/{symbol}", cached("eod", f"eod_{symbol}", longest))
    result = await mcp_client.call_tool("get_eod_prices", {"symbol": symbol, "start": start, "end": end})
    assert not result.is_error
    assert "SPINOFF_ADJUSTED_CLOSE" in codes(result)


async def test_london_prices_carry_the_pence_note(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "eod/ULVR.LSE", cached("eod", "eod_ULVR.LSE", longest))
    result = await mcp_client.call_tool(
        "get_eod_prices", {"symbol": "ULVR.LSE", "start": "2026-10-01", "end": "2026-10-29"}
    )
    assert "CURRENCY_NOTE" in codes(result)


async def test_every_cached_price_history_parses(mcp_client: Client, mock_api: MockEODHD) -> None:
    by_symbol: dict[str, Wrapper] = {}
    for symbol, wrapper in every("eod", "eod"):
        if symbol not in by_symbol or len(wrapper["body"]) > len(by_symbol[symbol]["body"]):
            by_symbol[symbol] = wrapper
    assert by_symbol
    for symbol, wrapper in by_symbol.items():
        serve(mock_api, f"eod/{symbol}", wrapper)
    for symbol in by_symbol:
        result = await mcp_client.call_tool(
            "get_eod_prices", {"symbol": symbol, "start": "1980-01-01", "end": "2026-10-31", "max_rows": 5000}
        )
        if result.is_error:
            assert any(code in error_text(result) for code in ERROR_CODES), symbol


# Fundamentals


async def test_fundamentals_flag_a_reporting_currency_that_differs_from_the_quote(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    serve(mock_api, "fundamentals/AZN.LSE", cached("fundamentals", "fundamentals_AZN.LSE"))
    result = await mcp_client.call_tool(
        "get_fundamentals", {"symbol": "AZN.LSE", "statements": ["income", "balance", "cashflow"]}
    )
    assert not result.is_error
    assert "CURRENCY_MISMATCH" in codes(result)


async def test_nse_filing_dates_are_flagged(mcp_client: Client, mock_api: MockEODHD) -> None:
    serve(mock_api, "fundamentals/RELIANCE.NSE", cached("fundamentals", "fundamentals_RELIANCE.NSE"))
    result = await mcp_client.call_tool(
        "get_fundamentals", {"symbol": "RELIANCE.NSE", "statements": ["income"]}
    )
    assert not result.is_error
    assert "FILING_DATE_UNRELIABLE" in codes(result)


async def test_every_cached_fundamentals_document_stays_small(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    documents = every("fundamentals", "fundamentals")
    assert documents
    for symbol, wrapper in documents:
        serve(mock_api, f"fundamentals/{symbol}", wrapper)
        result = await mcp_client.call_tool(
            "get_fundamentals",
            {"symbol": symbol, "statements": ["income", "balance", "cashflow"], "periods": 8},
        )
        if result.is_error:
            assert any(code in error_text(result) for code in ERROR_CODES), symbol
            continue
        size = len(json.dumps(result.structured_content))
        assert size < 160_000, f"{symbol}: {size} characters"


# Corporate actions and calendars


async def test_dividends_in_another_currency_than_the_quote_are_flagged(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    serve(mock_api, "div/AZN.LSE", cached("div", "div_AZN.LSE", longest))
    result = await mcp_client.call_tool("get_dividends", {"symbol": "AZN.LSE"})
    assert not result.is_error
    assert "CURRENCY_MISMATCH" in codes(result)


async def test_a_consolidation_after_a_demerger_is_an_odd_split(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    serve(mock_api, "splits/ULVR.LSE", cached("splits", "splits_ULVR.LSE"))
    result = await mcp_client.call_tool("get_splits", {"symbol": "ULVR.LSE"})
    assert not result.is_error
    assert "ODD_SPLIT_RATIO" in codes(result)


def whole_market(found: list[Wrapper]) -> Wrapper:
    windows = [
        w for w in found if w["status"] == 200 and '"from"' in w["body"] and '"symbols"' not in w["body"]
    ]
    if not windows:
        pytest.skip("no whole-market earnings window in the cache")
    return max(windows, key=lambda w: len(w["body"]))


async def test_the_whole_market_calendar_serves_tokyo_dates_for_one_call(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    wrapper = cached("calendar", "calendar_earnings", whole_market)
    payload = json.loads(wrapper["body"])
    tokyo = next((row["code"] for row in payload["earnings"] if row["code"].endswith(".TSE")), None)
    if tokyo is None:
        pytest.skip("the cached window has no Tokyo rows")
    start = date.fromisoformat(payload["from"])
    end = min(date.fromisoformat(payload["to"]), start + timedelta(days=89))
    serve(mock_api, "calendar/earnings", wrapper)
    result = await mcp_client.call_tool(
        "get_earnings_calendar",
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": [tokyo, "EMAAR.AE", "NOSUCHCODE.US"],
        },
    )
    assert not result.is_error
    found = codes(result)
    for expected in ("PARTIAL_COVERAGE", "NOT_COVERED", "NO_CALENDAR_ROWS", "CALENDAR_TIMING_UNRELIABLE"):
        assert expected in found
    assert result.structured_content["meta"]["cost"] == 1
    assert mock_api.count("calendar/earnings") == 1


# Intraday


async def test_no_bars_during_the_session_is_explained(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    empty = cached("intraday", "intraday_AAPL.US", lambda found: min(found, key=lambda w: len(w["body"])))
    if json.loads(empty["body"]) != []:
        pytest.skip("no empty intraday answer in the cache")
    clock.advance(days=-1, hours=2.5)  # Friday 2026-10-30 14:30 UTC: one hour into the US session
    serve(mock_api, "intraday/AAPL.US", empty)
    result = await mcp_client.call_tool("get_intraday_prices", {"symbol": "AAPL.US", "interval": "5m"})
    assert not result.is_error
    assert "INTRADAY_NOT_SAME_DAY" in codes(result)
