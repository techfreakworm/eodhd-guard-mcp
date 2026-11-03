"""list_exchanges, list_exchange_symbols and search_symbols, through the real MCP path."""

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
from eodhd_guard_mcp.redact import clip
from eodhd_guard_mcp.server import build_deps, build_server
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, RecordingSleep, load_fixture

pytestmark = pytest.mark.anyio

US_SYMBOLS = "exchange-symbol-list/US"


async def call(client: Client, tool: str, **arguments: Any) -> CallToolResult:
    return await client.call_tool(tool, arguments)


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


def symbols(body: dict[str, Any]) -> list[str]:
    return [row["symbol"] for row in body["data"]["symbols"]]


def hits(body: dict[str, Any]) -> list[str]:
    return [row["symbol"] for row in body["data"]["results"]]


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


def add_us_list(mock_api: MockEODHD) -> None:
    mock_api.add(US_SYMBOLS, load_fixture("tools_ref_symbols_us"))


LONG = "Acme Industrial Holdings " * 30
CUT = clip(LONG.strip())


# --- list_exchanges -----------------------------------------------------------------------------------


async def test_exchanges_are_listed_with_a_coverage_note_only_for_the_gaps(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("exchanges-list", load_fixture("tools_ref_exchanges"))
    body = ok(await call(mcp_client, "list_exchanges"))
    rows = {row["code"]: row for row in body["data"]["exchanges"]}
    assert list(rows) == ["ACX", "UMB", "GLX", "INI", "TSE", "AE"]
    assert rows["ACX"] == {
        "code": "ACX",
        "name": "Acme Exchange",
        "country": "Freedonia",
        "currency": "FDD",
        "operating_mic": "XACX",
        "country_iso2": "FD",
        "country_iso3": "FDN",
        "coverage_note": None,
    }
    assert [code for code, row in rows.items() if row["coverage_note"]] == ["TSE", "AE"]
    assert "only earnings dates" in rows["TSE"]["coverage_note"]
    assert "J-Quants" in rows["TSE"]["coverage_note"]
    assert rows["AE"]["coverage_note"].startswith("EODHD has no data for the UAE exchanges")
    assert body["data"]["list_size"] == body["data"]["count"] == 6
    assert body["meta"]["cost"] == 1
    assert body["meta"]["endpoints"] == ["exchanges-list"]


async def test_the_partial_coverage_warning_is_present_whatever_the_query(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("exchanges-list", load_fixture("tools_ref_exchanges"))
    everything = ok(await call(mcp_client, "list_exchanges"))
    only_covered = ok(await call(mcp_client, "list_exchanges", query="acme"))
    for body in (everything, only_covered):
        message = warning(body, "PARTIAL_COVERAGE")["message"]
        assert "Tokyo Stock Exchange" in message
        assert "UAE exchanges (DFM and ADX)" in message
        assert "AE is listed but its symbol list is empty" in message
    assert warning(everything, "PARTIAL_COVERAGE")["details"] == {"listed_without_data": ["TSE", "AE"]}
    assert warning(only_covered, "PARTIAL_COVERAGE")["details"] == {"listed_without_data": []}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("acx", ["ACX"]),
        ("GLOBEX", ["GLX"]),
        ("freedonia", ["ACX", "UMB"]),
        ("freedonia securities", ["UMB"]),
        ("bourse sylvania", ["INI"]),
        ("", ["ACX", "UMB", "GLX", "INI", "TSE", "AE"]),
        ("   ", ["ACX", "UMB", "GLX", "INI", "TSE", "AE"]),
    ],
)
async def test_the_query_needs_every_word_in_the_code_name_or_country(
    mcp_client: Client, mock_api: MockEODHD, query: str, expected: list[str]
) -> None:
    mock_api.add("exchanges-list", load_fixture("tools_ref_exchanges"))
    body = ok(await call(mcp_client, "list_exchanges", query=query))
    assert [row["code"] for row in body["data"]["exchanges"]] == expected
    assert body["data"]["count"] == len(expected)


async def test_a_query_without_a_match_is_an_empty_result_not_an_error(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("exchanges-list", load_fixture("tools_ref_exchanges"))
    body = ok(await call(mcp_client, "list_exchanges", query="narnia"))
    assert body["data"]["exchanges"] == []
    assert warning_codes(body) == ["PARTIAL_COVERAGE", "EMPTY_RESULT"]
    assert "'narnia'" in warning(body, "EMPTY_RESULT")["message"]


async def test_long_texts_from_the_vendor_are_cut_to_200_characters_in_the_exchange_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    assert len(CUT) == 200
    assert CUT.endswith("...")
    mock_api.add(
        "exchanges-list",
        [{"Code": "ACX", "Name": LONG, "Country": LONG, "Currency": LONG, "OperatingMIC": LONG}],
    )
    (row,) = ok(await call(mcp_client, "list_exchanges"))["data"]["exchanges"]
    assert (row["name"], row["country"], row["currency"], row["operating_mic"]) == (CUT,) * 4


async def test_the_exchange_list_is_cached_for_a_week(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    mock_api.add("exchanges-list", load_fixture("tools_ref_exchanges"))
    first = ok(await call(mcp_client, "list_exchanges"))
    clock.advance(days=6)
    second = ok(await call(mcp_client, "list_exchanges", query="acx"))
    assert (first["meta"]["cost"], first["meta"]["cached"]) == (1, False)
    assert (second["meta"]["cost"], second["meta"]["cached"]) == (0, True)
    assert mock_api.count("exchanges-list") == 1
    clock.advance(days=2)
    third = ok(await call(mcp_client, "list_exchanges"))
    assert third["meta"]["cost"] == 1
    assert mock_api.count("exchanges-list") == 2


async def test_an_empty_exchange_list_is_no_data(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("exchanges-list", [])
    text = failure(await call(mcp_client, "list_exchanges"))
    assert text.startswith("Error executing tool list_exchanges: NO_DATA: ")


@pytest.mark.parametrize("answer", [{"error": "unexpected"}, [1, 2], [{"Name": "no code"}]])
async def test_an_exchange_list_of_the_wrong_shape_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, answer: Any
) -> None:
    mock_api.add("exchanges-list", answer)
    assert "BAD_RESPONSE" in failure(await call(mcp_client, "list_exchanges"))


# --- list_exchange_symbols ----------------------------------------------------------------------------


async def test_a_symbol_is_built_from_the_requested_exchange_and_keeps_the_venue(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="us", query="acme", max_rows=2))
    assert body["data"]["symbols"][0] == {
        "symbol": "ACME.US",
        "code": "ACME",
        "name": "Acme Corp",
        "type": "Common Stock",
        "currency": "USD",
        "country": "USA",
        "isin": "ZZ0000000001",
        "venue": "NASDAQ",
    }
    assert body["data"]["exchange"] == "US"
    assert body["data"]["delisted"] is False
    request = mock_api.requests[0]
    assert request.path == US_SYMBOLS
    assert "delisted" not in request.params
    assert body["meta"]["endpoints"] == ["exchange-symbol-list"]


async def test_long_texts_from_the_vendor_are_cut_to_200_characters_in_the_symbol_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        US_SYMBOLS,
        [{"Code": "ACME", "Name": LONG, "Country": LONG, "Currency": LONG, "Type": LONG, "Isin": LONG}],
    )
    (row,) = ok(await call(mcp_client, "list_exchange_symbols", exchange="US"))["data"]["symbols"]
    assert (row["name"], row["country"], row["currency"], row["type"], row["isin"]) == (CUT,) * 5


@pytest.mark.parametrize("wanted", ["Common Stock", "common_stock", "COMMON-STOCK", "  common   stock "])
async def test_the_type_filter_ignores_case_and_separators(
    mcp_client: Client, mock_api: MockEODHD, wanted: str
) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", type=wanted, max_rows=500))
    assert body["data"]["total"] == 21
    assert {row["type"] for row in body["data"]["symbols"]} == {"Common Stock"}


@pytest.mark.parametrize(
    ("wanted", "total"), [("etf", 4), ("fund", 2), ("mutual_fund", 1), ("preferred_stock", 2)]
)
async def test_each_type_matches_only_itself(
    mcp_client: Client, mock_api: MockEODHD, wanted: str, total: int
) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", type=wanted))
    assert body["data"]["total"] == total


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("nimbus", ["NIMB.US"]),
        ("ETF acme", ["ACMX.US"]),
        ("ZZ0000000002", ["GLOBEX.US"]),
        ("acme", ["ACME.US", "ACMEP.US", "ACMX.US", "ACMFX.US"]),
    ],
)
async def test_the_query_matches_words_of_the_code_name_or_isin_in_the_order_of_the_list(
    mcp_client: Client, mock_api: MockEODHD, query: str, expected: list[str]
) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", query=query))
    assert symbols(body) == expected


async def test_pages_of_one_list_cost_one_call_in_total(mcp_client: Client, mock_api: MockEODHD) -> None:
    add_us_list(mock_api)
    first = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", max_rows=12))
    assert (first["data"]["total"], first["data"]["count"], first["data"]["next_offset"]) == (30, 12, 12)
    assert first["data"]["list_size"] == 30
    truncated = warning(first, "TRUNCATED")
    assert truncated["details"] == {"total": 30, "returned": 12, "offset": 0, "next_offset": 12}
    assert "30 symbols match" in truncated["message"]
    assert (first["meta"]["cost"], first["meta"]["cached"]) == (1, False)

    second = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", max_rows=12, offset=12))
    assert (second["meta"]["cost"], second["meta"]["cached"]) == (0, True)
    assert second["data"]["next_offset"] == 24
    assert "TRUNCATED" in warning_codes(second)

    last = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", max_rows=12, offset=24))
    assert (last["data"]["count"], last["data"]["next_offset"]) == (6, None)
    assert "TRUNCATED" not in warning_codes(last)
    assert last["meta"]["cost"] == 0

    pages = symbols(first) + symbols(second) + symbols(last)
    assert len(pages) == len(set(pages)) == 30
    assert mock_api.count(US_SYMBOLS) == 1


async def test_with_the_cache_off_the_note_does_not_promise_free_pages(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    add_us_list(mock_api)
    async with client_with(replace(settings, cache_enabled=False), mock_api, clock, sleeps) as client:
        first = ok(await call(client, "list_exchange_symbols", exchange="US", max_rows=12))
        second = ok(await call(client, "list_exchange_symbols", exchange="US", max_rows=12, offset=12))
    assert (first["meta"]["cost"], second["meta"]["cost"]) == (1, 1)
    note = first["meta"]["notes"][0]
    assert "cache is off" in note
    assert "1 API call" in note
    assert "cost 0" not in note
    assert mock_api.count(US_SYMBOLS) == 2


async def test_filters_on_a_cached_list_cost_nothing_either(mcp_client: Client, mock_api: MockEODHD) -> None:
    add_us_list(mock_api)
    ok(await call(mcp_client, "list_exchange_symbols", exchange="US"))
    again = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", type="etf", query="globex"))
    assert symbols(again) == ["GLBE.US"]
    assert (again["meta"]["cost"], again["meta"]["cached"]) == (0, True)
    assert mock_api.count(US_SYMBOLS) == 1


async def test_an_offset_past_the_last_match_is_an_empty_page(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", offset=100))
    assert body["data"]["symbols"] == []
    assert body["data"]["total"] == 30
    assert body["data"]["next_offset"] is None
    assert (
        "offset 100 is past the last of the 30 matching symbols" in warning(body, "EMPTY_RESULT")["message"]
    )


async def test_a_wrong_type_word_gets_the_types_the_list_has(mcp_client: Client, mock_api: MockEODHD) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", type="stock"))
    assert body["data"]["total"] == 0
    message = warning(body, "EMPTY_RESULT")["message"]
    assert "type 'stock'" in message
    assert "common_stock (21)" in message
    assert "etf (4)" in message


async def test_the_types_named_in_a_warning_are_cut_short_but_kept_whole_in_the_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    long_type = "weird " * 20
    mock_api.add(
        US_SYMBOLS,
        [
            {"Code": "ACME", "Name": "Acme", "Type": long_type},
            {"Code": "GLBE", "Name": "Globex", "Type": "ETF"},
        ],
    )
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", type="stock"))
    message = warning(body, "EMPTY_RESULT")["message"]
    assert "etf (1)" in message
    assert f"{clip('_'.join(long_type.split()), 30)} (1)" in message
    assert "_".join(long_type.split()) not in message
    whole = ok(await call(mcp_client, "list_exchange_symbols", exchange="US"))["data"]["symbols"]
    assert whole[0]["type"] == long_type.strip()


async def test_a_query_without_a_match_says_how_big_the_list_is(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", query="zzzz nothing"))
    message = warning(body, "EMPTY_RESULT")["message"]
    assert "query 'zzzz nothing'" in message
    assert "holds 30 symbols" in message
    assert "Types on this list" not in message


async def test_blank_optional_texts_mean_no_filter(mcp_client: Client, mock_api: MockEODHD) -> None:
    add_us_list(mock_api)
    body = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", query="  ", type=""))
    assert body["data"]["total"] == 30
    assert (body["data"]["query"], body["data"]["type"]) == (None, None)


async def test_delisted_lines_come_from_a_list_of_their_own(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(US_SYMBOLS, load_fixture("tools_ref_symbols_us"), times=1)
    mock_api.add(US_SYMBOLS, load_fixture("tools_ref_symbols_delisted_us"))
    active = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", query="oldco"))
    gone = ok(await call(mcp_client, "list_exchange_symbols", exchange="US", delisted=True))
    assert symbols(active) == []
    assert symbols(gone) == ["OLDCO.US", "BYGN.US", "VNSH.US"]
    assert gone["data"]["delisted"] is True
    assert gone["data"]["symbols"][2]["isin"] is None
    assert [request.params.get("delisted") for request in mock_api.requests] == [None, "1"]
    assert gone["meta"]["cost"] == 1


@pytest.mark.parametrize("exchange", ["TSE", "tse", "JP", "AE", "DFM", "ADX"])
async def test_exchanges_without_data_are_refused_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, exchange: str
) -> None:
    text = failure(await call(mcp_client, "list_exchange_symbols", exchange=exchange))
    assert text.startswith("Error executing tool list_exchange_symbols: NOT_COVERED: ")
    assert "EODHD_GUARD_ALLOW_UNCOVERED=1" in text
    assert mock_api.requests == []


async def test_the_escape_hatch_lets_a_gap_exchange_through(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    mock_api.add("exchange-symbol-list/TSE", load_fixture("tools_ref_symbols_delisted_us"))
    lenient = replace(settings, allow_uncovered=True)
    async with client_with(lenient, mock_api, clock, sleeps) as client:
        body = ok(await call(client, "list_exchange_symbols", exchange="TSE"))
    assert symbols(body) == ["OLDCO.TSE", "BYGN.TSE", "VNSH.TSE"]


async def test_an_unknown_exchange_is_not_found_and_points_to_the_exchange_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("exchange-symbol-list/ZZZ", status=404, text="Exchange Not Found.")
    text = failure(await call(mcp_client, "list_exchange_symbols", exchange="zzz"))
    assert "NOT_FOUND: EODHD answered HTTP 404: Exchange Not Found." in text
    assert "Hint: list_exchanges shows the exchange codes EODHD knows." in text


@pytest.mark.parametrize(
    ("status", "code"), [(401, "AUTH_ERROR"), (403, "PLAN_ERROR"), (500, "UPSTREAM_ERROR")]
)
async def test_other_errors_of_the_symbol_list_keep_their_own_code(
    mcp_client: Client, mock_api: MockEODHD, status: int, code: str
) -> None:
    mock_api.add(US_SYMBOLS, status=status, text="no")
    text = failure(await call(mcp_client, "list_exchange_symbols", exchange="US"))
    assert f"{code}: " in text
    assert "list_exchanges shows" not in text


@pytest.mark.parametrize(("delisted", "word"), [(False, "symbol"), (True, "delisted")])
async def test_a_listed_but_empty_exchange_is_no_data(
    mcp_client: Client, mock_api: MockEODHD, delisted: bool, word: str
) -> None:
    mock_api.add("exchange-symbol-list/ZZZ", [])
    text = failure(await call(mcp_client, "list_exchange_symbols", exchange="ZZZ", delisted=delisted))
    assert "NO_DATA: EODHD lists ZZZ but answered HTTP 200 with an empty " + word + " list." in text
    assert "AE is one" in text


@pytest.mark.parametrize("answer", [{"error": "unexpected"}, "text", [5, {"Name": "no code"}]])
async def test_a_symbol_list_of_the_wrong_shape_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, answer: Any
) -> None:
    mock_api.add(US_SYMBOLS, answer)
    assert "BAD_RESPONSE" in failure(await call(mcp_client, "list_exchange_symbols", exchange="US"))


@pytest.mark.parametrize("exchange", ["not valid!", "", "TOOLONGEXCHANGE", "É"])
async def test_a_malformed_exchange_is_refused_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, exchange: str
) -> None:
    text = failure(await call(mcp_client, "list_exchange_symbols", exchange=exchange))
    assert "exchange" in text
    assert mock_api.requests == []


@pytest.mark.parametrize(
    "arguments",
    [{"max_rows": 0}, {"max_rows": 501}, {"offset": -1}, {"offset": 1_000_001}, {"delisted": "maybe"}],
)
async def test_the_paging_bounds_are_enforced_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any]
) -> None:
    text = failure(await call(mcp_client, "list_exchange_symbols", exchange="US", **arguments))
    assert next(iter(arguments)) in text
    assert mock_api.requests == []


# --- search_symbols -----------------------------------------------------------------------------------


async def test_search_rows_carry_the_symbol_the_previous_close_and_a_liveness_flag(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    assert hits(body) == ["ACME.US", "ACME.XETRA", "ACMEOLD.US", "ACME.LSE", "ACMEJP.TSE"]
    assert body["data"]["results"][0] == {
        "symbol": "ACME.US",
        "code": "ACME",
        "exchange": "US",
        "name": "Acme Corp",
        "type": "Common Stock",
        "country": "USA",
        "currency": "USD",
        "isin": "ZZ0000000001",
        "is_primary": True,
        "previous_close": 41.8,
        "previous_close_date": "2026-11-02",
        "possibly_inactive": False,
    }
    assert body["data"]["count"] == 5
    assert body["data"]["query"] == "Acme Corp"
    assert body["meta"]["cost"] == 1
    assert body["meta"]["endpoints"] == ["search"]


async def test_long_texts_from_the_vendor_are_cut_to_200_characters_in_search_results(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        "search/Acme Corp",
        [
            {
                "Code": "ACME",
                "Exchange": "US",
                "Name": LONG,
                "Type": LONG,
                "Country": LONG,
                "Currency": LONG,
                "ISIN": LONG,
                "isPrimary": True,
                "previousClose": 41.8,
                "previousCloseDate": "2026-11-02",
            }
        ],
    )
    (row,) = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))["data"]["results"]
    assert (row["name"], row["type"], row["country"], row["currency"], row["isin"]) == (CUT,) * 5


async def test_symbols_named_in_a_warning_are_cut_short_but_kept_whole_in_its_details(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    code = "OLD" + "Z" * 100
    mock_api.add(
        "search/Acme Corp",
        [
            {
                "Code": code,
                "Exchange": "US",
                "Name": "Acme",
                "previousClose": 1.0,
                "previousCloseDate": "2026-01-02",
            }
        ],
    )
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    inactive = warning(body, "POSSIBLY_INACTIVE")
    assert inactive["details"]["symbols"] == [f"{code}.US"[:200]]
    assert f"No recent previous close for {clip(f'{code}.US', 24)}:" in inactive["message"]
    assert code not in inactive["message"]


async def test_a_missing_or_old_previous_close_marks_the_line_as_possibly_inactive(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    flagged = [row["symbol"] for row in body["data"]["results"] if row["possibly_inactive"]]
    assert flagged == ["ACMEOLD.US", "ACME.LSE"]
    inactive = warning(body, "POSSIBLY_INACTIVE")
    assert inactive["details"] == {"symbols": ["ACMEOLD.US", "ACME.LSE"], "threshold": 3}
    assert inactive["message"].startswith("No recent previous close for ACMEOLD.US, ACME.LSE: ")
    assert "check_symbol" in inactive["message"]
    missing = next(row for row in body["data"]["results"] if row["symbol"] == "ACME.LSE")
    assert missing["previous_close_date"] is None


@pytest.mark.parametrize(
    ("previous_close_date", "inactive"),
    [("2026-11-02", False), ("2026-10-30", False), ("2026-10-29", False), ("2026-10-28", True)],
)
async def test_a_previous_close_is_old_from_the_stale_sessions_threshold(
    mcp_client: Client, mock_api: MockEODHD, previous_close_date: str, inactive: bool
) -> None:
    row = {"Code": "ACME", "Exchange": "US", "Name": "Acme Corp", "previousCloseDate": previous_close_date}
    mock_api.add("search/Acme", [row])
    body = ok(await call(mcp_client, "search_symbols", query="Acme"))
    assert body["data"]["results"][0]["possibly_inactive"] is inactive
    assert ("POSSIBLY_INACTIVE" in warning_codes(body)) is inactive


async def test_rows_on_exchanges_without_data_get_a_partial_coverage_warning(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    coverage = warning(body, "PARTIAL_COVERAGE")
    assert coverage["details"] == {"symbols": ["ACMEJP.TSE"], "markets": ["Tokyo Stock Exchange"]}
    assert coverage["message"].startswith("Found on exchanges where EODHD has no usable prices")
    assert "(Tokyo Stock Exchange): ACMEJP.TSE." in coverage["message"]


async def test_rows_that_are_not_objects_are_skipped_too(mcp_client: Client, mock_api: MockEODHD) -> None:
    good = {"Code": "ACME", "Exchange": "US", "Name": "Acme Corp", "previousCloseDate": "2026-11-02"}
    mock_api.add("search/Acme", [5, "text", None, ["ACME"], good])
    body = ok(await call(mcp_client, "search_symbols", query="Acme"))
    assert hits(body) == ["ACME.US"]


async def test_rows_without_a_code_or_an_exchange_are_skipped(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    assert all(row["code"] and row["exchange"] for row in body["data"]["results"])
    assert len(body["data"]["results"]) == 5


async def test_the_filters_are_passed_on_and_type_all_is_left_out(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", [])
    ok(
        await call(
            mcp_client, "search_symbols", query="Acme Corp", exchange="us", type="stock", max_results=7
        )
    )
    ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    assert mock_api.requests[0].params == {"limit": "7", "exchange": "US", "type": "stock", "fmt": "json"}
    assert mock_api.requests[1].params == {"limit": "15", "fmt": "json"}


async def test_a_slash_or_extra_blanks_in_the_query_become_single_spaces(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", [])
    body = ok(await call(mcp_client, "search_symbols", query="  Acme  /  Corp\t"))
    assert body["data"]["query"] == "Acme Corp"
    assert mock_api.requests[0].path == "search/Acme Corp"


@pytest.mark.parametrize("query", ["///", " ", ".", ".."])
async def test_a_query_with_nothing_to_search_for_is_refused_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, query: str
) -> None:
    text = failure(await call(mcp_client, "search_symbols", query=query))
    assert "INVALID_PARAMS: query: received" in text
    assert "holds nothing to search for" in text
    assert mock_api.requests == []


async def test_no_match_is_an_empty_result_that_points_to_the_delisted_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Nothing Here", [])
    body = ok(await call(mcp_client, "search_symbols", query="Nothing Here", exchange="SW"))
    assert body["data"]["results"] == []
    assert warning_codes(body) == ["EMPTY_RESULT"]
    message = warning(body, "EMPTY_RESULT")["message"]
    assert "'Nothing Here' on SW" in message
    assert "list_exchange_symbols with delisted=true" in message


async def test_every_result_says_that_delisted_lines_are_not_searchable(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    assert any("Delisted lines are not searchable" in note for note in body["meta"]["notes"])


async def test_more_rows_than_the_limit_are_cut_and_reported(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    body = ok(await call(mcp_client, "search_symbols", query="Acme Corp", max_results=2))
    assert hits(body) == ["ACME.US", "ACME.XETRA"]
    assert warning(body, "TRUNCATED")["details"] == {"returned": 5, "limit": 2}


@pytest.mark.parametrize("answer", [{"error": "unexpected"}, "text", [{"Name": "no code"}]])
async def test_a_search_answer_of_the_wrong_shape_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, answer: Any
) -> None:
    mock_api.add("search/Acme Corp", answer)
    assert "BAD_RESPONSE" in failure(await call(mcp_client, "search_symbols", query="Acme Corp"))


async def test_a_search_is_cached_for_a_day(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    clock.advance(hours=23)
    again = ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    assert (again["meta"]["cost"], again["meta"]["cached"]) == (0, True)
    clock.advance(hours=2)
    ok(await call(mcp_client, "search_symbols", query="Acme Corp"))
    assert mock_api.count("search/") == 2


@pytest.mark.parametrize(
    "arguments",
    [{"type": "bond"}, {"max_results": 0}, {"max_results": 51}, {"query": ""}, {"query": "x" * 101}],
)
async def test_the_schema_rejects_out_of_range_arguments_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any]
) -> None:
    arguments = {"query": "Acme", **arguments}
    failure(await call(mcp_client, "search_symbols", **arguments))
    assert mock_api.requests == []


# --- keys ---------------------------------------------------------------------------------------------


async def test_the_api_key_is_in_no_result_not_even_an_error_that_echoes_it(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("exchanges-list", load_fixture("tools_ref_exchanges"))
    add_us_list(mock_api)
    mock_api.add("search/Acme Corp", load_fixture("tools_ref_search_acme"))
    mock_api.add("exchange-symbol-list/ZZZ", status=404, text=f"Exchange Not Found for token {TEST_KEY}")
    mock_api.add("search/Teapot", status=418, text=f"api_token={TEST_KEY} is a teapot")
    results = [
        await call(mcp_client, "list_exchanges"),
        await call(mcp_client, "list_exchange_symbols", exchange="US"),
        await call(mcp_client, "search_symbols", query="Acme Corp"),
        await call(mcp_client, "list_exchange_symbols", exchange="ZZZ"),
        await call(mcp_client, "list_exchange_symbols", exchange="TSE"),
        await call(mcp_client, "search_symbols", query="Teapot"),
        await call(mcp_client, "search_symbols", query="///"),
    ]
    assert [result.is_error for result in results] == [False, False, False, True, True, True, True]
    for result in results:
        everything = json.dumps(result.structured_content) + "\n".join(
            getattr(block, "text", "") for block in result.content
        )
        assert TEST_KEY not in everything
    assert "EODHD answered an unexpected HTTP 418" in failure(results[5])
