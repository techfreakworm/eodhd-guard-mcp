"""check_symbol, through the real MCP path.

The central story is a re-keyed code: ROG.SW froze in March 2026 and kept answering HTTP 200, while the
line carries on as ROP.SW under a new ISIN. The data is synthetic; only the codes and the company name are
real, because the test is about that case.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.server import Deps, build_deps, build_server
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, RecordingSleep, load_fixture

pytestmark = pytest.mark.anyio


async def check(client: Client, symbol: str, **options: Any) -> CallToolResult:
    return await client.call_tool("check_symbol", {"symbol": symbol, **options})


def ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def failure(result: CallToolResult) -> str:
    assert result.is_error
    return "\n".join(getattr(block, "text", "") for block in result.content)


def warning_codes(body: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in body["warnings"]]


def warning(body: dict[str, Any], code: str) -> dict[str, Any]:
    (found,) = [w for w in body["warnings"] if w["code"] == code]
    return found


def hit(
    code: str, exchange: str, name: str, day: str | None = "2026-11-02", isin: str | None = None
) -> dict[str, Any]:
    """One synthetic search row."""
    return {
        "Code": code,
        "Exchange": exchange,
        "Name": name,
        "Type": "Common Stock",
        "Country": "Freedonia",
        "Currency": "FDD",
        "ISIN": isin,
        "isPrimary": True,
        "previousClose": 10.5,
        "previousCloseDate": day,
    }


def listed(code: str, name: str | None, isin: str | None = None, exchange: str = "SW") -> dict[str, Any]:
    """One synthetic row of an exchange symbol list."""
    return {
        "Code": code,
        "Name": name,
        "Country": "Freedonia",
        "Exchange": exchange,
        "Currency": "FDD",
        "Type": "Common Stock",
        "Isin": isin,
    }


def bar(day: str) -> dict[str, Any]:
    return {
        "date": day,
        "open": 40.0,
        "high": 41.0,
        "low": 39.0,
        "close": 40.5,
        "adjusted_close": 40.5,
        "volume": 1000,
    }


def add_roche(mock_api: MockEODHD, bars: Any = None) -> None:
    """Every route of the re-key story: frozen bars, an NA quote, and the two searches."""
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen") if bars is None else bars)
    mock_api.add("real-time/ROG.SW", load_fixture("tools_ref_quote_na"))
    mock_api.add("search/ROG", load_fixture("tools_ref_search_rog"))
    mock_api.add("search/Roche Holding AG", load_fixture("tools_ref_search_roche_name"))


def add_frozen_line_missing_from_search(mock_api: MockEODHD, bars: Any = None) -> None:
    """How a frozen line really behaves: search no longer returns it, and only the delisted list has it."""
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen") if bars is None else bars)
    mock_api.add("real-time/ROG.SW", load_fixture("tools_ref_quote_na"))
    mock_api.add("search/ROG", load_fixture("tools_ref_search_rog_other"))
    mock_api.add("exchange-symbol-list/SW", load_fixture("tools_ref_symbols_delisted_sw"))
    mock_api.add("search/Roche Holding AG", load_fixture("tools_ref_search_roche_name_two"))


def add_line_nobody_knows(mock_api: MockEODHD) -> None:
    """Neither search nor the delisted list has ROG.SW."""
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen"))
    mock_api.add("real-time/ROG.SW", load_fixture("tools_ref_quote_na"))
    mock_api.add("search/ROG", load_fixture("tools_ref_search_rog_other"))
    mock_api.add("exchange-symbol-list/SW", [listed("OLDSW", "Oldswiss Holdings AG")])


def add_acme(mock_api: MockEODHD) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", load_fixture("tools_ref_quote_acme"))


@asynccontextmanager
async def client_with(
    settings: Settings, mock_api: MockEODHD, clock: FixedClock, sleeps: RecordingSleep
) -> AsyncIterator[Client]:
    """A client over a server built from other settings than the default ones."""
    deps = build_deps(settings, transport=mock_api.transport(), clock=clock, sleep=sleeps)
    try:
        async with Client(build_server(deps=deps)) as client:
            yield client
    finally:
        await deps.aclose()


# --- the re-key story ---------------------------------------------------------------------------------


async def test_a_frozen_code_is_caught_and_its_successor_is_named(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_roche(mock_api)
    body = ok(await check(mcp_client, "rog.sw"))
    data = body["data"]
    assert data["symbol"] == "ROG.SW"
    assert data["status"] == "stale"
    assert data["last_bar_date"] == "2026-03-25"
    assert data["expected_latest_session"] == "2026-11-02"
    assert data["sessions_behind"] == 158
    assert data["quote_status"] is None
    assert data["name"] == "Roche Holding AG"
    assert data["isin"] == "ZZ0000001001"
    assert data["successor_candidates"] == [
        {
            "symbol": "ROP.SW",
            "name": "Roche Holding AG",
            "isin": "ZZ0000001002",
            "previous_close_date": "2026-11-02",
        }
    ]
    assert data["advice"].startswith("ROG.SW looks frozen: its newest bar is 2026-03-25")
    assert "Possible successor: ROP.SW (Roche Holding AG, ISIN ZZ0000001002)" in data["advice"]
    assert "Compare the ISIN and name" in data["advice"]

    assert warning_codes(body) == ["STALE_DATA"]
    assert warning(body, "STALE_DATA")["details"]["sessions_behind"] == 158
    assert body["meta"]["cost"] == 3
    assert body["meta"]["endpoints"] == ["eod", "search"]
    assert body["meta"]["cached"] is False
    assert any("Holidays are not modelled" in note for note in body["meta"]["notes"])


async def test_both_searches_are_limited_to_the_exchange_and_an_exact_hit_makes_no_list_call(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_roche(mock_api)
    body = ok(await check(mcp_client, "ROG.SW"))
    eod, by_code, by_name = mock_api.requests
    assert eod.path == "eod/ROG.SW"
    assert eod.params == {
        "from": "2026-10-20",
        "to": "2026-11-03",
        "period": "d",
        "order": "a",
        "fmt": "json",
    }
    assert (by_code.path, by_code.params) == (
        "search/ROG",
        {"limit": "50", "exchange": "SW", "fmt": "json"},
    )
    assert (by_name.path, by_name.params) == (
        "search/Roche Holding AG",
        {"limit": "50", "exchange": "SW", "fmt": "json"},
    )
    assert mock_api.count("exchange-symbol-list") == 0
    assert "delisted list" not in body["data"]["advice"]


async def test_a_frozen_code_with_an_empty_window_has_no_last_bar_date_and_no_session_count(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_roche(mock_api, bars=[])
    body = ok(await check(mcp_client, "ROG.SW"))
    data = body["data"]
    assert data["status"] == "stale"
    assert data["last_bar_date"] is None
    assert data["sessions_behind"] is None
    assert data["expected_latest_session"] == "2026-11-02"
    assert data["advice"].startswith(
        "ROG.SW has no end-of-day bars from 2026-10-20 to 2026-11-03, although a bar for 2026-11-02 "
        "should exist: the code looks dead."
    )
    assert "ROP.SW" in data["advice"]
    assert "returned no bar" in warning(body, "STALE_DATA")["message"]
    assert body["meta"]["cost"] == 3


async def test_the_real_time_quote_adds_its_verdict_and_costs_one_more_call(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_roche(mock_api)
    body = ok(await check(mcp_client, "ROG.SW", include_quote=True))
    assert body["data"]["status"] == "stale"
    assert body["data"]["quote_status"] == "no_quote"
    assert warning_codes(body) == ["STALE_DATA", "NO_QUOTE"]
    assert "answered NA" in warning(body, "NO_QUOTE")["message"]
    assert body["meta"]["cost"] == 4
    assert body["meta"]["endpoints"] == ["eod", "real-time", "search"]


# --- a healthy code -----------------------------------------------------------------------------------


async def test_a_fresh_code_is_ok_and_costs_one_call(mcp_client: Client, mock_api: MockEODHD) -> None:
    add_acme(mock_api)
    body = ok(await check(mcp_client, "ACME.US"))
    data = body["data"]
    assert data["status"] == "ok"
    assert data["last_bar_date"] == "2026-11-02"
    assert (data["expected_latest_session"], data["sessions_behind"]) == ("2026-11-02", 0)
    assert data["quote_status"] is None
    assert (data["name"], data["isin"], data["successor_candidates"]) == (None, None, [])
    assert (
        data["advice"]
        == "ACME.US looks alive: the newest bar (2026-11-02) is the latest session expected by now."
    )
    assert body["warnings"] == []
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (1, ["eod"])
    assert [request.path for request in mock_api.requests] == ["eod/ACME.US"]


async def test_a_healthy_code_with_a_good_quote_costs_two_calls(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_acme(mock_api)
    body = ok(await check(mcp_client, "ACME.US", include_quote=True))
    assert (body["data"]["status"], body["data"]["quote_status"]) == ("ok", "ok")
    assert body["warnings"] == []
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (2, ["eod", "real-time"])


@pytest.mark.parametrize(
    ("last_bar", "status", "behind"),
    [
        ("2026-11-02", "ok", 0),
        ("2026-10-30", "ok", 1),
        ("2026-10-29", "ok", 2),
        ("2026-10-28", "stale", 3),
        ("2026-10-27", "stale", 4),
    ],
)
async def test_three_sessions_behind_is_where_a_code_turns_stale(
    mcp_client: Client, mock_api: MockEODHD, last_bar: str, status: str, behind: int
) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-10-26"), bar(last_bar)])
    body = ok(await check(mcp_client, "ACME.US", find_successor=False))
    assert (body["data"]["status"], body["data"]["sessions_behind"]) == (status, behind)
    assert ("STALE_DATA" in warning_codes(body)) is (status == "stale")


async def test_the_stale_sessions_setting_moves_the_threshold(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    mock_api.add("eod/ACME.US", [bar("2026-10-30")])
    async with client_with(replace(settings, stale_sessions=1), mock_api, clock, sleeps) as client:
        body = ok(await check(client, "ACME.US", find_successor=False))
    assert (body["data"]["status"], body["data"]["sessions_behind"]) == ("stale", 1)
    assert "1 session behind" in body["data"]["advice"]


async def test_only_the_newest_bar_date_matters_and_unreadable_rows_are_ignored(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        "eod/ACME.US", [{"date": "2026-11-02"}, {"date": "not a date"}, "junk", {"date": "2026-10-21"}]
    )
    body = ok(await check(mcp_client, "ACME.US"))
    assert (body["data"]["status"], body["data"]["last_bar_date"]) == ("ok", "2026-11-02")


async def test_a_second_check_is_free_for_an_hour(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    add_acme(mock_api)
    first = ok(await check(mcp_client, "ACME.US"))
    again = ok(await check(mcp_client, "ACME.US"))
    assert (first["meta"]["cost"], again["meta"]["cost"], again["meta"]["cached"]) == (1, 0, True)
    clock.advance(minutes=61)
    ok(await check(mcp_client, "ACME.US"))
    assert mock_api.count("eod/ACME.US") == 2


# --- not found ----------------------------------------------------------------------------------------


async def test_a_404_is_not_found_and_free(mcp_client: Client, mock_api: MockEODHD, deps: Deps) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    body = ok(await check(mcp_client, "GONE.US", find_successor=False))
    data = body["data"]
    assert data["status"] == "not_found"
    assert (data["last_bar_date"], data["sessions_behind"]) == (None, None)
    assert data["expected_latest_session"] == "2026-11-02"
    assert data["advice"].startswith("EODHD does not know GONE.US: the end-of-day endpoint answered 404.")
    assert "find_successor=true" in data["advice"]
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (0, [])
    assert body["warnings"] == []
    assert deps.ledger.spent_today() == 0


async def test_a_not_found_code_that_neither_search_nor_the_delisted_list_knows_ends_with_a_search_hint(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [])
    mock_api.add("exchange-symbol-list/US", [listed("OTHER", "Other Inc", exchange="US")])
    body = ok(await check(mcp_client, "GONE.US"))
    assert body["data"]["status"] == "not_found"
    assert (body["data"]["name"], body["data"]["successor_candidates"]) == (None, [])
    assert (
        "Could not find the line's name in search or in the delisted list for US, so no successor "
        "search was possible. Use search_symbols with the company name." in body["data"]["advice"]
    )
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (2, ["search", "exchange-symbol-list"])


async def test_a_not_found_code_is_named_from_the_delisted_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [])
    mock_api.add(
        "exchange-symbol-list/US", [listed("GONE", "Gone Industries Inc", "ZZ00000000G1", exchange="US")]
    )
    mock_api.add(
        "search/Gone Industries Inc", [hit("GONEX", "US", "Gone Industries Inc", isin="ZZ00000000G2")]
    )
    body = ok(await check(mcp_client, "GONE.US"))
    data = body["data"]
    assert (data["status"], data["name"], data["isin"]) == (
        "not_found",
        "Gone Industries Inc",
        "ZZ00000000G1",
    )
    assert [candidate["symbol"] for candidate in data["successor_candidates"]] == ["GONEX.US"]
    assert "EODHD's delisted list for US has GONE as 'Gone Industries Inc'." in data["advice"]
    assert body["meta"]["cost"] == 3


async def test_a_row_of_the_same_code_on_another_exchange_is_never_used(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [hit("GONE", "LSE", "Gone Elsewhere plc", isin="YY00000000G9")])
    mock_api.add("exchange-symbol-list/US", [listed("OTHER", "Other Inc", exchange="US")])
    body = ok(await check(mcp_client, "GONE.US"))
    assert (body["data"]["name"], body["data"]["isin"]) == (None, None)
    assert "Gone Elsewhere" not in json.dumps(body)
    assert [request.path for request in mock_api.requests] == [
        "eod/GONE.US",
        "search/GONE",
        "exchange-symbol-list/US",
    ]


async def test_the_line_itself_is_preferred_over_the_same_code_elsewhere(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add(
        "search/GONE",
        [
            hit("GONE", "LSE", "Gone Elsewhere plc"),
            hit("GONE", "US", "Gone Industries Inc", isin="ZZ00000000G3"),
        ],
    )
    mock_api.add("search/Gone Industries Inc", [])
    body = ok(await check(mcp_client, "GONE.US"))
    assert (body["data"]["name"], body["data"]["isin"]) == ("Gone Industries Inc", "ZZ00000000G3")
    assert body["meta"]["notes"] == []
    assert "No other code on US with a recent price matches 'Gone Industries Inc'" in body["data"]["advice"]
    assert "try search_symbols with the new name" in body["data"]["advice"]
    assert mock_api.count("exchange-symbol-list") == 0


# --- the line is no longer in search: the delisted list --------------------------------------------


async def test_a_frozen_line_that_search_no_longer_returns_is_named_from_the_delisted_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_frozen_line_missing_from_search(mock_api)
    body = ok(await check(mcp_client, "ROG.SW"))
    data = body["data"]
    assert (data["status"], data["last_bar_date"]) == ("stale", "2026-03-25")
    assert (data["name"], data["isin"]) == ("Roche Holding AG", "ZZ0000001001")
    assert [candidate["symbol"] for candidate in data["successor_candidates"]] == ["ROP.SW", "RO.SW"]
    assert data["successor_candidates"][0] == {
        "symbol": "ROP.SW",
        "name": "Roche Holding AG",
        "isin": "ZZ0000001002",
        "previous_close_date": "2026-11-02",
    }
    assert "EODHD's delisted list for SW has ROG as 'Roche Holding AG'." in data["advice"]
    assert "Possible successors: ROP.SW (Roche Holding AG, ISIN ZZ0000001002); RO.SW (" in data["advice"]

    assert [(r.path, r.params.get("exchange"), r.params.get("delisted")) for r in mock_api.requests] == [
        ("eod/ROG.SW", None, None),
        ("search/ROG", "SW", None),
        ("exchange-symbol-list/SW", None, "1"),
        ("search/Roche Holding AG", "SW", None),
    ]
    assert body["meta"]["cost"] == 4
    assert body["meta"]["endpoints"] == ["eod", "search", "exchange-symbol-list"]
    assert warning_codes(body) == ["STALE_DATA"]


async def test_the_other_company_with_the_same_code_that_search_returned_is_never_used(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_frozen_line_missing_from_search(mock_api)
    body = ok(await check(mcp_client, "ROG.SW"))
    assert "Rogwell" not in json.dumps(body)
    assert mock_api.count("search/Rogwell") == 0
    assert mock_api.count("search/") == 2


async def test_with_a_quote_the_delisted_path_costs_five_calls(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_frozen_line_missing_from_search(mock_api)
    body = ok(await check(mcp_client, "ROG.SW", include_quote=True))
    assert (body["data"]["status"], body["data"]["quote_status"]) == ("stale", "no_quote")
    assert body["meta"]["cost"] == 5
    assert body["meta"]["endpoints"] == ["eod", "real-time", "search", "exchange-symbol-list"]
    assert warning_codes(body) == ["STALE_DATA", "NO_QUOTE"]


async def test_when_neither_search_nor_the_delisted_list_has_the_line_no_name_search_is_made(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_line_nobody_knows(mock_api)
    body = ok(await check(mcp_client, "ROG.SW"))
    data = body["data"]
    assert data["status"] == "stale"
    assert (data["name"], data["isin"], data["successor_candidates"]) == (None, None, [])
    assert data["advice"].endswith(
        "Could not find the line's name in search or in the delisted list for SW, so no successor "
        "search was possible. Use search_symbols with the company name."
    )
    assert [request.path for request in mock_api.requests] == [
        "eod/ROG.SW",
        "search/ROG",
        "exchange-symbol-list/SW",
    ]
    assert body["meta"]["cost"] == 3
    assert "Rogwell" not in json.dumps(body)


async def test_an_exact_search_hit_makes_no_delisted_list_call(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_roche(mock_api)
    mock_api.add("exchange-symbol-list/SW", load_fixture("tools_ref_symbols_delisted_sw"))
    body = ok(await check(mcp_client, "ROG.SW"))
    assert mock_api.count("exchange-symbol-list") == 0
    assert body["meta"]["endpoints"] == ["eod", "search"]
    assert body["meta"]["cost"] == 3


@pytest.mark.parametrize(
    ("setup", "include_quote", "cost", "endpoints"),
    [
        (add_roche, False, 3, ["eod", "search"]),
        (add_roche, True, 4, ["eod", "real-time", "search"]),
        (add_frozen_line_missing_from_search, False, 4, ["eod", "search", "exchange-symbol-list"]),
        (
            add_frozen_line_missing_from_search,
            True,
            5,
            ["eod", "real-time", "search", "exchange-symbol-list"],
        ),
        (add_line_nobody_knows, False, 3, ["eod", "search", "exchange-symbol-list"]),
        (add_line_nobody_knows, True, 4, ["eod", "real-time", "search", "exchange-symbol-list"]),
    ],
)
async def test_the_cost_of_each_lookup_path_is_exact_and_matches_the_ledger(
    mcp_client: Client,
    mock_api: MockEODHD,
    deps: Deps,
    setup: Any,
    include_quote: bool,
    cost: int,
    endpoints: list[str],
) -> None:
    setup(mock_api)
    body = ok(await check(mcp_client, "ROG.SW", include_quote=include_quote))
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (cost, endpoints)
    assert deps.ledger.spent_today() == cost


async def test_the_delisted_list_is_shared_with_list_exchange_symbols_in_both_directions(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_frozen_line_missing_from_search(mock_api)
    listing = ok(await mcp_client.call_tool("list_exchange_symbols", {"exchange": "SW", "delisted": True}))
    assert listing["meta"]["cost"] == 1
    body = ok(await check(mcp_client, "ROG.SW"))
    assert body["meta"]["cost"] == 3
    assert body["meta"]["endpoints"] == ["eod", "search", "exchange-symbol-list"]
    assert mock_api.count("exchange-symbol-list/SW") == 1


async def test_a_list_fetched_by_check_symbol_is_free_for_list_exchange_symbols(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_frozen_line_missing_from_search(mock_api)
    assert ok(await check(mcp_client, "ROG.SW"))["meta"]["cost"] == 4
    listing = ok(
        await mcp_client.call_tool(
            "list_exchange_symbols", {"exchange": "SW", "delisted": True, "query": "roche"}
        )
    )
    assert (listing["meta"]["cost"], listing["meta"]["cached"]) == (0, True)
    assert [row["symbol"] for row in listing["data"]["symbols"]] == ["ROG.SW"]
    assert mock_api.count("exchange-symbol-list/SW") == 1


async def test_a_search_hit_with_a_name_that_cannot_be_searched_falls_back_to_the_delisted_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [hit("GONE", "US", "..", isin="ZZ00000000G1")])
    mock_api.add(
        "exchange-symbol-list/US", [listed("GONE", "Gone Industries Inc", "ZZ00000000G4", exchange="US")]
    )
    mock_api.add("search/Gone Industries Inc", [])
    body = ok(await check(mcp_client, "GONE.US"))
    assert (body["data"]["name"], body["data"]["isin"]) == ("Gone Industries Inc", "ZZ00000000G4")


@pytest.mark.parametrize("name", ["..", "///", "."])
async def test_a_name_that_cannot_be_searched_for_is_treated_as_no_name(
    mcp_client: Client, mock_api: MockEODHD, name: str
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [hit("GONE", "US", name, isin="ZZ00000000G1")])
    mock_api.add("exchange-symbol-list/US", [listed("GONE", name, "ZZ00000000G1", exchange="US")])
    body = ok(await check(mcp_client, "GONE.US"))
    assert (body["data"]["name"], body["data"]["isin"]) == (None, None)
    assert "Could not find the line's name" in body["data"]["advice"]
    assert (body["meta"]["cost"], mock_api.count("search/")) == (2, 1)


@pytest.mark.parametrize("rows", [[], [listed("GONE", None, exchange="US")], "unreadable"])
async def test_a_delisted_list_without_a_usable_row_for_the_code_gives_no_name(
    mcp_client: Client, mock_api: MockEODHD, rows: Any
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [])
    mock_api.add("exchange-symbol-list/US", rows if rows != "unreadable" else [5, "text", {"Code": 7}])
    body = ok(await check(mcp_client, "GONE.US"))
    assert body["data"]["name"] is None
    assert "Could not find the line's name in search or in the delisted list for US" in body["data"]["advice"]


async def test_an_exchange_the_list_endpoint_does_not_know_is_no_name_not_an_error(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [])
    mock_api.add("exchange-symbol-list/US", status=404, text="Exchange Not Found.")
    body = ok(await check(mcp_client, "GONE.US"))
    assert "Could not find the line's name" in body["data"]["advice"]
    assert "stopped early" not in body["data"]["advice"]
    assert body["meta"]["cost"] == 1


@pytest.mark.parametrize(
    ("route", "word"),
    [({"status": 500}, "UPSTREAM_ERROR"), ({"body": {"unexpected": "object"}}, "BAD_RESPONSE")],
)
async def test_a_failing_delisted_list_keeps_the_verdict_and_says_so(
    mcp_client: Client, mock_api: MockEODHD, route: dict[str, Any], word: str
) -> None:
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen"))
    mock_api.add("search/ROG", load_fixture("tools_ref_search_rog_other"))
    mock_api.add("exchange-symbol-list/SW", **route)
    body = ok(await check(mcp_client, "ROG.SW"))
    assert body["data"]["status"] == "stale"
    assert body["data"]["name"] is None
    assert f"The successor search stopped early ({word}: " in body["data"]["advice"]
    assert "STALE_DATA" in warning_codes(body)


async def test_a_code_the_delisted_list_holds_for_two_companies_is_not_guessed(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [])
    mock_api.add(
        "exchange-symbol-list/US",
        [
            listed("GONE", "Gone Industries Inc", "ZZ00000000G1", exchange="US"),
            listed("GONE", "Gone Mining Corp", "ZZ00000000G5", exchange="US"),
            listed("GONE", "GONE INDUSTRIES INC", None, exchange="US"),
        ],
    )
    body = ok(await check(mcp_client, "GONE.US"))
    data = body["data"]
    assert (data["name"], data["isin"], data["successor_candidates"]) == (None, None, [])
    assert (
        "The delisted list for US holds 2 different companies under the code GONE "
        "('Gone Industries Inc'; 'Gone Mining Corp'), so its name could not be settled." in data["advice"]
    )
    assert data["advice"].endswith("Use search_symbols with the company name.")
    assert mock_api.count("search/") == 1


async def test_rows_of_one_company_under_the_same_code_are_one_line_and_give_the_isin_that_exists(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
    mock_api.add("search/GONE", [])
    mock_api.add(
        "exchange-symbol-list/US",
        [
            listed("GONE", "Gone Industries Inc", None, exchange="US"),
            listed("GONE", "GONE INDUSTRIES INC", "ZZ00000000G6", exchange="US"),
        ],
    )
    mock_api.add("search/Gone Industries Inc", [])
    body = ok(await check(mcp_client, "GONE.US"))
    assert (body["data"]["name"], body["data"]["isin"]) == ("Gone Industries Inc", "ZZ00000000G6")


async def test_a_line_with_current_bars_is_not_looked_up_in_the_delisted_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", load_fixture("tools_ref_quote_na"))
    mock_api.add("search/ACME", [])
    body = ok(await check(mcp_client, "ACME.US", include_quote=True))
    assert body["data"]["status"] == "no_quote"
    assert mock_api.count("exchange-symbol-list") == 0
    assert body["meta"]["cost"] == 3


# --- successor candidates: the ISIN country first --------------------------------------------------


def add_gone_sw(mock_api: MockEODHD, isin: str | None, rows: list[dict[str, Any]]) -> None:
    mock_api.add("eod/GONE.SW", [bar("2026-01-05")])
    mock_api.add("search/GONE", [])
    mock_api.add("exchange-symbol-list/SW", [listed("GONE", "Gone Industries AG", isin)])
    mock_api.add("search/Gone Industries AG", rows)


def other(code: str, isin: str | None) -> dict[str, Any]:
    return hit(code, "SW", "Gone Industries AG", isin=isin)


async def test_candidates_with_the_isin_country_of_the_line_come_first_and_the_others_stay(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_gone_sw(
        mock_api,
        "ZZ0000000G10",
        [
            other("A1", "YY0000000A01"),
            other("A2", "YY0000000A02"),
            other("A3", None),
            other("A4", "YY0000000A04"),
            other("A5", "YY0000000A05"),
            other("B1", "ZZ0000000B01"),
            other("B2", "zz0000000b02"),
            other("B3", "ZZ0000000B03"),
        ],
    )
    body = ok(await check(mcp_client, "GONE.SW"))
    symbols = [candidate["symbol"] for candidate in body["data"]["successor_candidates"]]
    assert symbols == ["B1.SW", "B2.SW", "B3.SW", "A1.SW", "A2.SW"]


@pytest.mark.parametrize("isin", [None, "", "1234", "9Z0000000G10"])
async def test_without_a_known_isin_country_the_order_is_the_one_eodhd_gave(
    mcp_client: Client, mock_api: MockEODHD, isin: str | None
) -> None:
    add_gone_sw(
        mock_api,
        isin,
        [other("A1", "YY0000000A01"), other("B1", "ZZ0000000B01"), other("A2", None)],
    )
    body = ok(await check(mcp_client, "GONE.SW"))
    symbols = [candidate["symbol"] for candidate in body["data"]["successor_candidates"]]
    assert symbols == ["A1.SW", "B1.SW", "A2.SW"]


# --- the STALE_DATA warning ------------------------------------------------------------------------


@pytest.mark.parametrize("bars", [None, []])
async def test_the_stale_warning_does_not_tell_check_symbol_to_run_check_symbol(
    mcp_client: Client, mock_api: MockEODHD, bars: Any
) -> None:
    add_roche(mock_api, bars=bars)
    body = ok(await check(mcp_client, "ROG.SW"))
    stale = warning(body, "STALE_DATA")
    assert "check_symbol" not in stale["message"]
    assert stale["message"].endswith("so this may be a dead line and not a quiet market.")
    assert "2026-11-02" in stale["message"]
    assert stale["details"]["symbol"] == "ROG.SW"
    assert stale["details"]["expected_latest"] == "2026-11-02"


# --- candidates ---------------------------------------------------------------------------------------


async def test_candidates_are_other_codes_on_the_same_exchange_with_a_recent_close_at_most_five(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/GONE.US", [bar("2026-01-05")])
    mock_api.add("search/GONE", [hit("GONE", "US", "Gone Industries", "2026-01-05", "ZZ00000000G1")])
    news = [hit(f"NEW{n}", "US", f"Gone Industries {n}", isin=f"ZZ0000000N{n}") for n in range(1, 8)]
    mock_api.add(
        "search/Gone Industries",
        [
            hit("GONE", "US", "Gone Industries", "2026-11-02"),
            hit("OLD1", "US", "Gone Industries Legacy", "2026-09-01"),
            hit("OTHER", "LSE", "Gone Industries plc", "2026-11-02"),
            news[0],
            news[0],
            hit("NOCLOSE", "US", "Gone Industries Unpriced", None),
            *news[1:],
        ],
    )
    body = ok(await check(mcp_client, "GONE.US"))
    candidates = body["data"]["successor_candidates"]
    assert [candidate["symbol"] for candidate in candidates] == [
        "NEW1.US",
        "NEW2.US",
        "NEW3.US",
        "NEW4.US",
        "NEW5.US",
    ]
    assert (
        body["data"]["advice"].count("Possible successors: NEW1.US (Gone Industries 1, ISIN ZZ0000000N1);")
        == 1
    )


async def test_a_name_search_that_finds_nothing_new_says_so_without_inventing_a_successor(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ROG.SW", [])
    mock_api.add("search/ROG", load_fixture("tools_ref_search_rog"))
    mock_api.add("search/Roche Holding AG", [hit("ROG", "SW", "Roche Holding AG", "2026-03-25")])
    body = ok(await check(mcp_client, "ROG.SW"))
    assert body["data"]["status"] == "stale"
    assert (body["data"]["name"], body["data"]["successor_candidates"]) == ("Roche Holding AG", [])
    assert "No other code on SW with a recent price matches 'Roche Holding AG'" in body["data"]["advice"]
    assert body["meta"]["cost"] == 3
    assert mock_api.count("exchange-symbol-list") == 0


# --- no quote and quote trouble -----------------------------------------------------------------------


async def test_fresh_bars_with_an_na_quote_are_no_quote_and_still_get_the_name_looked_up(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", load_fixture("tools_ref_quote_na"))
    mock_api.add("search/ACME", load_fixture("tools_ref_search_acme"))
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await check(mcp_client, "ACME.US", include_quote=True))
    data = body["data"]
    assert (data["status"], data["quote_status"]) == ("no_quote", "no_quote")
    assert data["name"] == "Acme Corp"
    assert data["successor_candidates"] == []
    assert data["advice"].startswith(
        "ACME.US has current end-of-day bars (newest 2026-11-02) but no real-time quote"
    )
    assert "Use get_eod_prices." in data["advice"]
    assert warning_codes(body) == ["NO_QUOTE"]
    assert body["meta"]["cost"] == 4


async def test_without_a_successor_search_the_na_quote_costs_two_calls(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", load_fixture("tools_ref_quote_na"))
    body = ok(await check(mcp_client, "ACME.US", include_quote=True, find_successor=False))
    assert (body["data"]["status"], body["meta"]["cost"]) == ("no_quote", 2)


@pytest.mark.parametrize(
    "quote",
    [
        {"code": "ACME.US", "timestamp": "NA", "close": 41.9},
        {"code": "ACME.US", "timestamp": 1793717100, "close": "NA"},
        {"code": "ACME.US", "timestamp": 0, "close": 41.9},
        {"code": "ACME.US", "timestamp": True, "close": 41.9},
        {"code": "ACME.US", "timestamp": None, "close": None},
        {"code": "ACME.US", "timestamp": 1793717100},
        {},
        [],
        "NA",
    ],
)
async def test_a_quote_without_a_price_or_a_timestamp_is_not_a_quote(
    mcp_client: Client, mock_api: MockEODHD, quote: Any
) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", quote)
    body = ok(await check(mcp_client, "ACME.US", include_quote=True, find_successor=False))
    assert (body["data"]["status"], body["data"]["quote_status"]) == ("no_quote", "no_quote")
    assert warning_codes(body) == ["NO_QUOTE"]


async def test_a_quote_may_come_as_a_list_of_one_and_numbers_may_be_text(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", [{"timestamp": "1793717100", "close": "41.9"}])
    body = ok(await check(mcp_client, "ACME.US", include_quote=True))
    assert (body["data"]["status"], body["data"]["quote_status"]) == ("ok", "ok")


async def test_a_404_for_the_quote_is_a_missing_quote(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", status=404, text="Ticker Not Found.")
    body = ok(await check(mcp_client, "ACME.US", include_quote=True, find_successor=False))
    assert (body["data"]["status"], body["data"]["quote_status"]) == ("no_quote", "missing")
    assert "answered HTTP 404" in warning(body, "NO_QUOTE")["message"]
    assert body["meta"]["cost"] == 1


async def test_a_failing_quote_call_keeps_the_verdict_and_says_so(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", load_fixture("tools_ref_eod_acme_fresh"))
    mock_api.add("real-time/ACME.US", status=500)
    body = ok(await check(mcp_client, "ACME.US", include_quote=True))
    assert (body["data"]["status"], body["data"]["quote_status"]) == ("ok", "error")
    assert "The quote check failed (UPSTREAM_ERROR: EODHD answered HTTP 500" in body["data"]["advice"]
    assert body["warnings"] == []
    assert body["meta"]["cost"] == 1


# --- failures during the successor search -------------------------------------------------------------


async def test_a_failing_first_search_keeps_the_verdict(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen"))
    mock_api.add("search/ROG", status=500)
    body = ok(await check(mcp_client, "ROG.SW"))
    assert body["data"]["status"] == "stale"
    assert (body["data"]["name"], body["data"]["successor_candidates"]) == (None, [])
    assert (
        "The successor search stopped early (UPSTREAM_ERROR: EODHD answered HTTP 500"
        in body["data"]["advice"]
    )
    assert "STALE_DATA" in warning_codes(body)
    assert body["meta"]["cost"] == 1


async def test_a_failing_second_search_keeps_the_name_that_was_found(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen"))
    mock_api.add("search/ROG", load_fixture("tools_ref_search_rog"))
    mock_api.add("search/Roche Holding AG", status=503)
    body = ok(await check(mcp_client, "ROG.SW"))
    assert (body["data"]["name"], body["data"]["isin"]) == ("Roche Holding AG", "ZZ0000001001")
    assert body["data"]["successor_candidates"] == []
    assert "stopped early (UPSTREAM_ERROR" in body["data"]["advice"]
    assert body["meta"]["cost"] == 2


async def test_a_budget_that_runs_out_mid_check_keeps_what_was_paid_for(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    add_roche(mock_api)
    async with client_with(replace(settings, daily_budget=2), mock_api, clock, sleeps) as client:
        body = ok(await check(client, "ROG.SW"))
    assert body["data"]["status"] == "stale"
    assert body["data"]["name"] == "Roche Holding AG"
    assert body["data"]["successor_candidates"] == []
    assert (
        "stopped early (BUDGET_EXCEEDED: The daily budget of 2 API calls would be exceeded"
        in body["data"]["advice"]
    )
    assert body["meta"]["cost"] == 2


async def test_find_successor_false_makes_no_search(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("eod/ROG.SW", load_fixture("tools_ref_eod_rog_frozen"))
    body = ok(await check(mcp_client, "ROG.SW", find_successor=False))
    assert body["data"]["status"] == "stale"
    assert (body["data"]["name"], body["data"]["successor_candidates"]) == (None, [])
    assert (
        "run check_symbol with find_successor=true, or use search_symbols with the company name"
        in body["data"]["advice"]
    )
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (1, ["eod"])
    assert [request.path for request in mock_api.requests] == ["eod/ROG.SW"]


# --- markets without data -----------------------------------------------------------------------------


async def test_a_market_without_data_is_not_covered_and_costs_nothing(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    body = ok(await check(mcp_client, "7203.TSE", include_quote=True))
    data = body["data"]
    assert data["status"] == "not_covered"
    assert data["expected_latest_session"] == "2026-11-02"
    assert (data["last_bar_date"], data["sessions_behind"], data["quote_status"]) == (None, None, None)
    assert (data["name"], data["isin"], data["successor_candidates"]) == (None, None, [])
    assert data["advice"].startswith(
        "7203.TSE is on the Tokyo Stock Exchange, which EODHD does not cover, so nothing was looked up and "
        "nothing was spent. Alternatives: J-Quants"
    )
    assert warning_codes(body) == ["NOT_COVERED"]
    assert warning(body, "NOT_COVERED")["details"]["gap"] == "tokyo"
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (0, [])
    assert mock_api.requests == []
    assert deps.ledger.spent_today() == 0


@pytest.mark.parametrize("symbol", ["7203.T", "7203.JP", "EMAAR.AE", "DEWA.DFM", "ADNOCDIST.ADX"])
async def test_every_suffix_of_a_gap_market_is_not_covered(
    mcp_client: Client, mock_api: MockEODHD, symbol: str
) -> None:
    body = ok(await check(mcp_client, symbol))
    assert body["data"]["status"] == "not_covered"
    assert mock_api.requests == []


@pytest.mark.parametrize("symbol", ["SHOP.TO", "TEVA.TA"])
async def test_look_alikes_of_the_gap_suffixes_are_checked_normally(
    mcp_client: Client, mock_api: MockEODHD, symbol: str
) -> None:
    mock_api.add(f"eod/{symbol}", [bar("2026-11-02")])
    assert ok(await check(mcp_client, symbol))["data"]["status"] == "ok"


async def test_the_escape_hatch_makes_a_gap_market_a_normal_check(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    mock_api.add("eod/7203.TSE", [bar("2026-11-02")])
    async with client_with(replace(settings, allow_uncovered=True), mock_api, clock, sleeps) as client:
        body = ok(await check(client, "7203.TSE"))
    assert body["data"]["status"] == "ok"
    assert body["meta"]["cost"] == 1


# --- cost accounting ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "include_quote", "cost", "endpoints"),
    [
        ("7203.TSE", False, 0, []),
        ("ACME.US", False, 1, ["eod"]),
        ("ACME.US", True, 2, ["eod", "real-time"]),
        ("ROG.SW", False, 3, ["eod", "search"]),
        ("ROG.SW", True, 4, ["eod", "real-time", "search"]),
    ],
)
async def test_the_cost_is_the_number_of_billed_calls_from_zero_to_four(
    mcp_client: Client,
    mock_api: MockEODHD,
    deps: Deps,
    symbol: str,
    include_quote: bool,
    cost: int,
    endpoints: list[str],
) -> None:
    add_acme(mock_api)
    add_roche(mock_api)
    body = ok(await check(mcp_client, symbol, include_quote=include_quote))
    assert body["meta"]["cost"] == cost
    assert body["meta"]["endpoints"] == endpoints
    assert deps.ledger.spent_today() == cost


# --- bad input and bad answers ------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["ROG", "", "ROG.", "R OG.SW"])
async def test_a_symbol_without_an_exchange_is_refused_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, symbol: str
) -> None:
    text = failure(await check(mcp_client, symbol))
    assert "INVALID_PARAMS: symbol: received" in text
    assert mock_api.requests == []


@pytest.mark.parametrize("answer", [{"message": "unexpected"}, "text", [{"close": 1}, 5]])
async def test_a_bar_answer_of_the_wrong_shape_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, answer: Any
) -> None:
    mock_api.add("eod/ACME.US", answer)
    assert "BAD_RESPONSE" in failure(await check(mcp_client, "ACME.US"))


async def test_other_errors_of_the_bar_call_are_raised_not_swallowed(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("eod/ACME.US", status=401, text="Unauthorized")
    text = failure(await check(mcp_client, "ACME.US"))
    assert text.startswith("Error executing tool check_symbol: AUTH_ERROR: ")


# --- keys ---------------------------------------------------------------------------------------------


async def test_the_api_key_is_in_no_result_even_when_it_is_in_the_input_or_echoed_by_an_error(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_acme(mock_api)
    add_roche(mock_api)
    mock_api.add("eod/GONE.US", status=404, text=f"Ticker Not Found for {TEST_KEY}")
    mock_api.add("eod/DENIED.US", status=401, text=f"invalid token {TEST_KEY}")
    mock_api.add("search/GONE", [])
    mock_api.add("exchange-symbol-list/US", [])
    results = [
        await check(mcp_client, "ACME.US", include_quote=True),
        await check(mcp_client, "ROG.SW", include_quote=True),
        await check(mcp_client, "GONE.US"),
        await check(mcp_client, "7203.TSE"),
        await check(mcp_client, "DENIED.US"),
        await check(mcp_client, TEST_KEY),
    ]
    assert [result.is_error for result in results] == [False, False, False, False, True, True]
    for result in results:
        everything = json.dumps(result.structured_content) + "\n".join(
            getattr(block, "text", "") for block in result.content
        )
        assert TEST_KEY not in everything
    assert "received '***'" in failure(results[5])
