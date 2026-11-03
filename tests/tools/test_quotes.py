"""get_quote, through the MCP client against a mocked EODHD.

The clock is Tuesday 2026-11-03 15:00 UTC. The standard quote is stamped 14:40 UTC, 20 minutes old. The latest
US session whose bar should exist is Monday 2026-11-02. Every fixture is synthetic.
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
from eodhd_guard_mcp.server import Deps
from eodhd_guard_mcp.tools.quotes import fetch_quotes
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, load_fixture

pytestmark = pytest.mark.anyio

QUOTE_TIME = datetime(2026, 11, 3, 14, 40, tzinfo=timezone.utc)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def row(code: str, moment: datetime = QUOTE_TIME, **fields: Any) -> dict[str, Any]:
    """A real-time row in EODHD's shape with invented numbers."""
    return {
        "code": code,
        "timestamp": int(moment.timestamp()),
        "gmtoffset": 0,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "volume": 1000,
        "previousClose": 10.0,
        "change": 0.5,
        "change_p": 5.0,
        **fields,
    }


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return str(getattr(result.content[0], "text", ""))


def codes(body: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in body["warnings"]]


def warnings_of(body: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [warning for warning in body["warnings"] if warning["code"] == code]


async def quote(client: Client, symbols: Any) -> CallToolResult:
    return await client.call_tool("get_quote", {"symbols": symbols})


async def quoted(client: Client, symbols: Any) -> dict[str, Any]:
    """The structured content of a call that is expected to succeed."""
    result = await quote(client, symbols)
    assert not result.is_error, error_text(result)
    assert result.structured_content is not None
    return result.structured_content


@pytest.fixture
def multi() -> list[dict[str, Any]]:
    """ACME.US, GLOBEX.LSE and INITECH.SW, in that order."""
    return load_fixture("tools_prices_quote_multi")


# --- the answer ---------------------------------------------------------------------------------------


async def test_one_symbol_comes_back_as_an_ok_entry_with_its_age_and_the_delay_flag(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", load_fixture("tools_prices_quote_acme"))
    body = await quoted(mcp_client, ["ACME.US"])
    assert body["data"] == [
        {
            "symbol": "ACME.US",
            "status": "ok",
            "price": 52.9,
            "open": 52.1,
            "high": 53.4,
            "low": 51.8,
            "previous_close": 51.7,
            "change": 1.2,
            "change_pct": 2.3211,
            "volume": 412300,
            "as_of": "2026-11-03T14:40:00Z",
            "age_seconds": 1200,
            "delayed": True,
        }
    ]
    assert codes(body) == ["DELAYED_QUOTE"]
    assert "15 to 20 minutes" in body["warnings"][0]["message"]
    assert (body["meta"]["cost"], body["meta"]["endpoints"], body["meta"]["cached"]) == (
        1,
        ["real-time"],
        False,
    )
    assert any("last price at as_of" in note for note in body["meta"]["notes"])
    assert mock_api.requests[0].params == {"fmt": "json"}
    assert mock_api.requests[0].api_token == TEST_KEY


async def test_several_symbols_are_one_request_costing_one_call_each_and_keep_the_requested_order(
    mcp_client: Client, mock_api: MockEODHD, multi: list[dict[str, Any]]
) -> None:
    mock_api.add("real-time/ACME.US", list(reversed(multi)))  # EODHD need not answer in the order asked
    body = await quoted(mcp_client, ["ACME.US", "GLOBEX.LSE", "INITECH.SW"])
    assert mock_api.requests[0].params["s"] == "GLOBEX.LSE,INITECH.SW"
    assert [(e["symbol"], e["status"]) for e in body["data"]] == [
        ("ACME.US", "ok"),
        ("GLOBEX.LSE", "ok"),
        ("INITECH.SW", "ok"),
    ]
    assert body["data"][1]["price"] == 1270.5
    assert (body["data"][2]["as_of"], body["data"][2]["age_seconds"]) == ("2026-11-03T14:42:00Z", 1080)
    assert body["meta"]["cost"] == 3
    assert mock_api.count("real-time/") == 1


async def test_a_comma_separated_string_is_accepted_and_repeats_are_sent_once(
    mcp_client: Client, mock_api: MockEODHD, multi: list[dict[str, Any]]
) -> None:
    mock_api.add("real-time/ACME.US", multi[:2])
    body = await quoted(mcp_client, "acme.us, ACME.US, globex.lse")
    assert mock_api.requests[0].params["s"] == "GLOBEX.LSE"
    assert [e["symbol"] for e in body["data"]] == ["ACME.US", "GLOBEX.LSE"]
    assert body["meta"]["cost"] == 2


async def test_symbols_with_an_ampersand_survive_in_the_path_and_in_the_list(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/M&M.NSE", [row("M&M.NSE"), row("Q&A.NSE")])
    body = await quoted(mcp_client, ["M&M.NSE", "Q&A.NSE"])
    assert [e["status"] for e in body["data"]] == ["ok", "ok"]
    assert mock_api.requests[0].params["s"] == "Q&A.NSE"
    assert "M%26M.NSE" in mock_api.requests[0].raw_path


async def test_twenty_symbols_fit_in_one_request_and_twenty_one_do_not(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    symbols = [f"S{number:02d}.US" for number in range(1, 22)]
    mock_api.add("real-time/S01.US", [row(symbol) for symbol in symbols[:20]])
    body = await quoted(mcp_client, symbols[:20])
    assert body["meta"]["cost"] == 20
    assert len(mock_api.requests[0].params["s"].split(",")) == 19
    too_many = await quote(mcp_client, symbols)
    text = error_text(too_many)
    assert "INVALID_PARAMS" in text
    assert "21 different symbols, more than the 20 allowed per call" in text
    assert mock_api.count("real-time/") == 1


@pytest.mark.parametrize(
    ("symbols", "expected"),
    [
        ([], "names no symbol"),
        ("", "names no symbol"),
        (["ACME"], "no exchange suffix"),
        ("ACME.US,VOD", "VOD"),
    ],
)
async def test_bad_symbols_are_refused_before_any_call_is_made(
    mcp_client: Client, mock_api: MockEODHD, symbols: Any, expected: str
) -> None:
    text = error_text(await quote(mcp_client, symbols))
    assert "INVALID_PARAMS" in text
    assert expected in text
    assert mock_api.requests == []


@pytest.mark.parametrize("symbols", [5, [1, 2], None, {"symbol": "ACME.US"}])
async def test_symbols_of_the_wrong_type_never_reach_the_api(
    mcp_client: Client, mock_api: MockEODHD, symbols: Any
) -> None:
    assert (await quote(mcp_client, symbols)).is_error
    assert mock_api.requests == []


async def test_a_repeated_call_is_a_cache_hit_for_60_seconds(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock, deps: Deps
) -> None:
    mock_api.add("real-time/ACME.US", load_fixture("tools_prices_quote_acme"))
    first = await quoted(mcp_client, ["ACME.US"])
    clock.advance(seconds=59)
    second = await quoted(mcp_client, ["ACME.US"])
    assert (first["meta"]["cost"], second["meta"]["cost"], second["meta"]["cached"]) == (1, 0, True)
    assert second["data"][0]["age_seconds"] == 1259  # the age is recomputed from the stored timestamp
    assert mock_api.count("real-time/") == 1
    clock.advance(seconds=2)
    third = await quoted(mcp_client, ["ACME.US"])
    assert third["meta"]["cost"] == 1
    assert mock_api.count("real-time/") == 2
    assert deps.ledger.spent_today() == 2


# --- NA quotes ----------------------------------------------------------------------------------------


async def test_an_all_na_answer_for_the_only_symbol_fails_with_no_data_listing_its_status(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add("real-time/ROG.SW", load_fixture("tools_prices_quote_na_rog"))
    text = error_text(await quote(mcp_client, ["ROG.SW"]))
    assert "NO_DATA" in text
    assert "None of the 1 requested symbol(s) has a quote" in text
    assert 'ROG.SW: no_quote (EODHD returned no usable quote (timestamp "NA", close "NA")' in text
    assert "The request cost 1 API call(s)" in text
    assert "check_symbol" in text
    assert deps.ledger.spent_today() == 1


async def test_one_na_symbol_among_good_ones_is_a_no_quote_entry_with_a_warning(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        "real-time/ACME.US",
        [load_fixture("tools_prices_quote_acme"), load_fixture("tools_prices_quote_na_rog")],
    )
    body = await quoted(mcp_client, ["ACME.US", "ROG.SW"])
    assert [(e["symbol"], e["status"]) for e in body["data"]] == [("ACME.US", "ok"), ("ROG.SW", "no_quote")]
    assert set(body["data"][1]) == {"symbol", "status", "message"}
    assert "unknown, delisted or renamed code" in body["data"][1]["message"]
    assert codes(body) == ["DELAYED_QUOTE", "NO_QUOTE"]
    no_quote = body["warnings"][1]
    assert no_quote["details"] == {"symbol": "ROG.SW", "timestamp": '"NA"', "close": '"NA"'}
    assert "check_symbol" in no_quote["message"]
    assert "search_symbols" in no_quote["message"]
    assert body["meta"]["cost"] == 2


@pytest.mark.parametrize(
    ("fields", "seen"),
    [
        ({"close": None}, "timestamp 1793716800, close null"),
        ({"close": "NA"}, 'close "NA"'),
        ({"timestamp": "NA"}, 'timestamp "NA"'),
        ({"timestamp": None}, "timestamp null"),
        ({"timestamp": 0}, "timestamp 0"),
        ({"timestamp": -5}, "timestamp -5"),
        ({"timestamp": 1e20}, "timestamp 1e+20"),
        ({"timestamp": 10**400}, "timestamp an out-of-range number"),
        ({"timestamp": "1793716800"}, "timestamp an unreadable value"),
        ({"close": True}, "close an unreadable value"),
    ],
)
async def test_a_quote_needs_a_real_timestamp_and_a_close(
    mcp_client: Client, mock_api: MockEODHD, fields: dict[str, Any], seen: str
) -> None:
    mock_api.add("real-time/ACME.US", [row("ACME.US"), row("GLOBEX.US", **fields)])
    body = await quoted(mcp_client, ["ACME.US", "GLOBEX.US"])
    assert [e["status"] for e in body["data"]] == ["ok", "no_quote"]
    assert seen in body["data"][1]["message"]
    assert len(warnings_of(body, "NO_QUOTE")) == 1


END_OF_TIME = 253402300799  # 9999-12-31T23:59:59Z, the last second a datetime can hold


@pytest.mark.parametrize("exchange", ["NSE", "HK", "AU", "SW"])
async def test_a_timestamp_at_the_end_of_time_is_no_quote_on_a_market_ahead_of_utc(
    mcp_client: Client, mock_api: MockEODHD, exchange: str
) -> None:
    # The same instant in UTC+5:30 is already past year 9999, which no date can hold.
    mock_api.add("real-time/ACME.US", [row("ACME.US"), row(f"UMB.{exchange}", timestamp=END_OF_TIME)])
    body = await quoted(mcp_client, ["ACME.US", f"UMB.{exchange}"])
    assert [e["status"] for e in body["data"]] == ["ok", "no_quote"]
    assert f"timestamp {END_OF_TIME}" in body["data"][1]["message"]
    assert len(warnings_of(body, "NO_QUOTE")) == 1


async def test_a_lone_timestamp_at_the_end_of_time_fails_as_no_data_not_as_a_crash(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/UMB.NSE", row("UMB.NSE", timestamp=END_OF_TIME))
    assert "NO_DATA" in error_text(await quote(mcp_client, ["UMB.NSE"]))


async def test_fields_that_are_na_on_their_own_become_null_in_an_otherwise_good_quote(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        "real-time/ACME.US",
        row("ACME.US", open="NA", high=None, volume="NA", previousClose="NA", change="NA", change_p="NA"),
    )
    entry = (await quoted(mcp_client, ["ACME.US"]))["data"][0]
    assert entry["status"] == "ok"
    assert entry["price"] == 10.5
    assert [entry[name] for name in ("open", "high", "volume", "previous_close", "change", "change_pct")] == [
        None
    ] * 6


async def test_a_whole_number_volume_is_an_integer_and_a_fraction_is_kept(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", [row("ACME.US", volume=1000.0), row("GLOBEX.US", volume=12.5)])
    data = (await quoted(mcp_client, ["ACME.US", "GLOBEX.US"]))["data"]
    assert [(e["volume"], type(e["volume"])) for e in data] == [(1000, int), (12.5, float)]


async def test_a_timestamp_in_the_future_has_an_age_of_zero(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("real-time/ACME.US", row("ACME.US", utc(2026, 11, 3, 15, 10)))
    entry = (await quoted(mcp_client, ["ACME.US"]))["data"][0]
    assert (entry["as_of"], entry["age_seconds"]) == ("2026-11-03T15:10:00Z", 0)


# --- what EODHD answered ------------------------------------------------------------------------------


async def test_a_symbol_the_answer_leaves_out_is_missing(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("real-time/ACME.US", [row("ACME.US")])
    body = await quoted(mcp_client, ["ACME.US", "GLOBEX.LSE"])
    assert body["data"][1] == {
        "symbol": "GLOBEX.LSE",
        "status": "missing",
        "message": "EODHD's answer did not include this symbol.",
    }
    assert codes(body) == ["DELAYED_QUOTE"]
    assert body["meta"]["cost"] == 2


async def test_when_nothing_is_quoted_the_call_fails_and_lists_every_status(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", [])
    text = error_text(await quote(mcp_client, ["ACME.US", "ROG.SW"]))
    assert "NO_DATA" in text
    assert "ACME.US: missing" in text
    assert "ROG.SW: missing" in text
    assert "check_symbol" in text


async def test_codes_in_the_answer_are_matched_ignoring_case_and_spaces(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", [row(" acme.us "), row("globex.us")])
    body = await quoted(mcp_client, ["ACME.US", "GLOBEX.US"])
    assert [e["status"] for e in body["data"]] == ["ok", "ok"]


async def test_a_lone_answer_without_a_code_belongs_to_the_one_symbol_asked_for(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    lone = row("ACME.US")
    del lone["code"]
    mock_api.add("real-time/ACME.US", lone)
    assert (await quoted(mcp_client, ["ACME.US"]))["data"][0]["status"] == "ok"


async def test_a_lone_answer_for_another_code_does_not_pass_as_the_symbol_asked_for(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", row("OTHER.US"))
    text = error_text(await quote(mcp_client, ["ACME.US"]))
    assert "NO_DATA" in text
    assert "ACME.US: missing" in text


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("oops", "a str value"),
        (7, "a int value"),
        ([1, 2], "no usable code field"),
        ([{"close": 1}, {"close": 2}], "no usable code field"),
        ([{"code": "", "close": 1}, {"code": 5}], "no usable code field"),
    ],
)
async def test_an_answer_that_cannot_be_matched_to_the_symbols_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, body: Any, expected: str
) -> None:
    mock_api.add("real-time/ACME.US", body)
    text = error_text(await quote(mcp_client, ["ACME.US", "GLOBEX.LSE"]))
    assert "BAD_RESPONSE" in text
    assert expected in text


# --- stale quotes, the delay and the currency -----------------------------------------------------------


@pytest.mark.parametrize(
    ("stamped", "quote_date", "sessions_behind"),
    [
        (utc(2026, 10, 28, 20, 0), "2026-10-28", 3),
        (
            utc(2026, 10, 29, 2, 0),
            "2026-10-28",
            3,
        ),  # 22:00 in New York: the local date is the 28th, not the 29th
        (utc(2026, 3, 25, 16, 30), "2026-03-25", 158),
    ],
)
async def test_a_quote_three_or_more_sessions_behind_is_flagged_stale(
    mcp_client: Client,
    mock_api: MockEODHD,
    stamped: datetime,
    quote_date: str,
    sessions_behind: int,
) -> None:
    mock_api.add("real-time/ACME.US", row("ACME.US", stamped))
    body = await quoted(mcp_client, ["ACME.US"])
    assert body["data"][0]["status"] == "ok"
    assert body["data"][0]["as_of"] == stamped.strftime("%Y-%m-%dT%H:%M:%SZ")
    (stale,) = warnings_of(body, "STALE_QUOTE")
    assert stale["details"] == {
        "symbol": "ACME.US",
        "as_of": stamped.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_date": quote_date,
        "expected_latest": "2026-11-02",
        "sessions_behind": sessions_behind,
    }
    assert "check_symbol" in stale["message"]
    assert "search_symbols" in stale["message"]
    assert codes(body) == ["DELAYED_QUOTE", "STALE_QUOTE"]


@pytest.mark.parametrize(
    "stamped",
    [
        utc(2026, 10, 29, 20, 0),  # 2 sessions behind
        utc(2026, 10, 30, 2, 0),  # 22:00 on the 29th in New York: still 2 behind
        utc(2026, 11, 2, 21, 0),  # yesterday's close
        QUOTE_TIME,
    ],
)
async def test_a_quote_less_than_three_sessions_behind_is_not_stale(
    mcp_client: Client, mock_api: MockEODHD, stamped: datetime
) -> None:
    mock_api.add("real-time/ACME.US", row("ACME.US", stamped))
    assert codes(await quoted(mcp_client, ["ACME.US"])) == ["DELAYED_QUOTE"]


async def test_a_friday_quote_is_still_current_on_saturday(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    clock.advance(days=4)  # Saturday 2026-11-07
    mock_api.add("real-time/ACME.US", row("ACME.US", utc(2026, 11, 6, 20, 55)))
    body = await quoted(mcp_client, ["ACME.US"])
    assert codes(body) == ["DELAYED_QUOTE"]
    assert body["data"][0]["age_seconds"] == int(timedelta(hours=18, minutes=5).total_seconds())


async def test_the_stale_threshold_comes_from_the_settings_and_one_session_is_singular(
    deps: Deps, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", row("ACME.US", utc(2026, 10, 30, 20, 0)))  # Friday: 1 session behind
    relaxed = await fetch_quotes(deps, symbols=["ACME.US"])
    strict_deps = replace(deps, settings=replace(deps.settings, stale_sessions=1))
    strict = await fetch_quotes(strict_deps, symbols=["ACME.US"])
    assert [w.code for w in relaxed.warnings] == ["DELAYED_QUOTE"]
    assert [w.code for w in strict.warnings] == ["DELAYED_QUOTE", "STALE_QUOTE"]
    assert "1 session behind" in strict.warnings[1].message


async def test_the_market_of_each_symbol_sets_its_own_calendar(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    # 15:00 UTC is 20:30 in India, five hours after the NSE close, so the NSE bar for the 3rd is expected.
    stamped = utc(2026, 10, 29, 9, 0)
    mock_api.add("real-time/ACME.US", [row("ACME.US", stamped), row("UMBRELLA.NSE", stamped)])
    body = await quoted(mcp_client, ["ACME.US", "UMBRELLA.NSE"])
    (stale,) = warnings_of(body, "STALE_QUOTE")
    assert (stale["details"]["symbol"], stale["details"]["expected_latest"]) == ("UMBRELLA.NSE", "2026-11-03")


async def test_london_quotes_carry_the_pence_note_once(
    mcp_client: Client, mock_api: MockEODHD, multi: list[dict[str, Any]]
) -> None:
    mock_api.add("real-time/GLOBEX.LSE", [multi[1], row("HOOLI.LSE")])
    body = await quoted(mcp_client, ["GLOBEX.LSE", "HOOLI.LSE"])
    (note,) = warnings_of(body, "CURRENCY_NOTE")
    assert note["details"] == {"symbols": ["GLOBEX.LSE", "HOOLI.LSE"], "currency": "GBX"}
    assert "pence" in note["message"]


async def test_a_london_symbol_without_a_quote_gets_no_pence_note(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    na = {**row("GLOBEX.LSE"), "timestamp": "NA", "close": "NA"}
    mock_api.add("real-time/GLOBEX.LSE", [na, row("ACME.US")])
    body = await quoted(mcp_client, ["GLOBEX.LSE", "ACME.US"])
    assert codes(body) == ["DELAYED_QUOTE", "NO_QUOTE"]


# --- coverage -----------------------------------------------------------------------------------------


async def test_an_uncovered_symbol_is_never_sent_and_gets_its_own_entry_and_warning(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/ACME.US", load_fixture("tools_prices_quote_acme"))
    body = await quoted(mcp_client, ["7203.TSE", "ACME.US"])
    assert [(e["symbol"], e["status"]) for e in body["data"]] == [
        ("7203.TSE", "not_covered"),
        ("ACME.US", "ok"),
    ]
    assert "Tokyo Stock Exchange" in body["data"][0]["message"]
    assert set(body["data"][0]) == {"symbol", "status", "message"}
    assert codes(body) == ["DELAYED_QUOTE", "NOT_COVERED"]
    not_covered = body["warnings"][1]
    assert not_covered["details"] == {"symbol": "7203.TSE", "market": "Tokyo Stock Exchange", "gap": "tokyo"}
    assert "J-Quants" in not_covered["message"]
    assert "EODHD_GUARD_ALLOW_UNCOVERED=1" in not_covered["message"]
    assert [request.path for request in mock_api.requests] == ["real-time/ACME.US"]
    assert "s" not in mock_api.requests[0].params
    assert body["meta"]["cost"] == 1


async def test_when_every_symbol_is_uncovered_nothing_is_sent_and_the_call_fails(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    text = error_text(await quote(mcp_client, ["7203.TSE", "EMAAR.AE"]))
    assert "NO_DATA" in text
    assert "7203.TSE: not_covered" in text
    assert "EMAAR.AE: not_covered" in text
    assert "Nothing was sent, so nothing was charged" in text
    assert "J-Quants" in text
    assert "Dubai Financial Market" in text
    assert mock_api.requests == []


async def test_two_uncovered_symbols_of_one_market_share_a_single_hint(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    text = error_text(await quote(mcp_client, ["7203.TSE", "7203.T"]))
    assert "7203.TSE: not_covered" in text
    assert "7203.T: not_covered" in text
    assert text.count("J-Quants") == 1
    assert mock_api.requests == []


async def test_the_coverage_check_can_be_switched_off(deps: Deps, mock_api: MockEODHD) -> None:
    mock_api.add("real-time/7203.TSE", {**load_fixture("tools_prices_quote_na_rog"), "code": "7203.TSE"})
    allowing = replace(deps, settings=replace(deps.settings, allow_uncovered=True))
    with pytest.raises(GuardError, match=r"7203\.TSE: no_quote") as caught:
        await fetch_quotes(allowing, symbols=["7203.TSE"])
    assert caught.value.code is ErrorCode.NO_DATA
    assert mock_api.count("real-time/7203.TSE") == 1


# --- errors from EODHD ----------------------------------------------------------------------------------


async def test_a_404_names_the_symbol_and_the_tools_that_find_the_right_code(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/NOPE.US", status=404, text="Ticker Not Found.")
    text = error_text(await quote(mcp_client, ["NOPE.US"]))
    assert "NOT_FOUND" in text
    assert "No quote for NOPE.US" in text
    assert "Ticker Not Found." in text
    assert "check_symbol" in text
    assert "search_symbols" in text
    assert "led it" not in text


async def test_a_404_on_a_request_with_several_symbols_points_at_the_one_that_led_it(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("real-time/BRK.B", status=404, text="Ticker Not Found.")
    text = error_text(await quote(mcp_client, ["BRK.B", "ACME.US"]))
    assert "NOT_FOUND" in text
    assert "The request carried 2 symbols and BRK.B led it" in text
    assert "Drop it or fix its suffix and ask again." in text
    assert "hyphen, as in BRK-B.US" in text


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "AUTH_ERROR"), (402, "QUOTA_EXCEEDED"), (403, "PLAN_ERROR"), (500, "UPSTREAM_ERROR")],
)
async def test_other_http_errors_pass_through_with_their_codes(
    mcp_client: Client, mock_api: MockEODHD, status: int, code: str
) -> None:
    mock_api.add("real-time/ACME.US", status=status, text="no")
    assert code in error_text(await quote(mcp_client, ["ACME.US"]))


# --- the key never comes back -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "text", "code"),
    [
        (200, None, None),
        (401, f"Unauthorized api_token={TEST_KEY}", "AUTH_ERROR"),
        (402, f"Payment Required {TEST_KEY}", "QUOTA_EXCEEDED"),
        (403, f"Forbidden for key {TEST_KEY}", "PLAN_ERROR"),
        (
            404,
            f"Ticker Not Found. https://eodhd.test/api/real-time/ACME.US?api_token={TEST_KEY}",
            "NOT_FOUND",
        ),
        (500, f"Server error, token={TEST_KEY}", "UPSTREAM_ERROR"),
    ],
)
async def test_the_api_key_appears_in_no_result_text_or_structured_content(
    mcp_client: Client, mock_api: MockEODHD, status: int, text: str | None, code: str | None
) -> None:
    if text is None:
        mock_api.add("real-time/ACME.US", [row("ACME.US"), row("ROG.SW", close=f"NA {TEST_KEY}")])
    else:
        mock_api.add("real-time/ACME.US", status=status, text=text)
    result = await quote(mcp_client, ["ACME.US", "ROG.SW"])
    everything = json.dumps(result.structured_content) + "".join(
        str(getattr(part, "text", "")) for part in result.content
    )
    assert TEST_KEY not in everything
    assert mock_api.requests[0].api_token == TEST_KEY  # the key was sent to EODHD, and only there
    if code is None:
        assert not result.is_error
    else:
        assert code in error_text(result)
