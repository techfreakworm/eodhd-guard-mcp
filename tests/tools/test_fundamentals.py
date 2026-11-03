"""get_fundamentals through the MCP client: one cached document, cut down, with its guards."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.types import CallToolResult

from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.redact import clip
from eodhd_guard_mcp.server import Deps, build_deps, build_server
from eodhd_guard_mcp.tools.fundamentals import (
    _enforce_cap,
    _entry_weight,
    _json_size,
    parse_sections,
    parse_statements,
)
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, RecordingSleep, load_fixture

pytestmark = pytest.mark.anyio

ACME = "fundamentals/ACME.US"
CAP_BYTES = 150_000


async def fundamentals(client: Client, **arguments: Any) -> CallToolResult:
    arguments.setdefault("symbol", "ACME.US")
    return await client.call_tool("get_fundamentals", arguments)


def acme() -> dict[str, Any]:
    return copy.deepcopy(load_fixture("tools_fund_acme_stock"))  # type: ignore[no-any-return]


def body_of(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return str(result.content[0].text)  # type: ignore[union-attr]


def codes(body: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in body["warnings"]]


def warning_of(body: dict[str, Any], code: str) -> dict[str, Any]:
    return next(w for w in body["warnings"] if w["code"] == code)


def sent_size(value: Any) -> int:
    """Bytes of the text the SDK sends for ``value``: JSON indented by two spaces, not compact."""
    return len(json.dumps(value, indent=2, ensure_ascii=False, default=str).encode("utf-8"))


def everything(result: CallToolResult) -> str:
    """All the text a model would see for a result, content and structured content alike."""
    parts = [json.dumps(item.model_dump()) for item in result.content]
    parts.append(json.dumps(result.structured_content))
    return "\n".join(parts)


def quarter_ends(count: int) -> list[str]:
    ends, year, quarter = [], 2026, 3
    for _ in range(count):
        month = quarter * 3
        ends.append(f"{year}-{month:02d}-{ {3: 31, 6: 30, 9: 30, 12: 31}[month] }")
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
    return ends


def wide_statement(periods: int, fields: int) -> dict[str, Any]:
    rows = {
        day: {
            "date": day,
            "filing_date": day,
            "currency_symbol": "USD",
            **{f"field{j:03d}": f"{1_000_000 + j * 7 + i:.2f}" for j in range(fields)},
        }
        for i, day in enumerate(quarter_ends(periods))
    }
    return {"currency_symbol": "USD", "quarterly": rows, "yearly": {}}


def with_statements(doc: dict[str, Any], **widths: int) -> dict[str, Any]:
    names = {"income": "Income_Statement", "balance": "Balance_Sheet", "cashflow": "Cash_Flow"}
    doc["Financials"] = {names[short]: wide_statement(40, fields) for short, fields in widths.items()}
    return doc


@pytest.fixture
def tight_budget(settings: Settings) -> Settings:
    """A budget that exactly covers one fundamentals read."""
    return replace(settings, daily_budget=10)


@pytest.fixture
async def budgeted_client(
    tight_budget: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> AsyncIterator[tuple[Client, Deps]]:
    deps = build_deps(tight_budget, transport=mock_api.transport(), clock=clock, sleep=sleeps)
    async with Client(build_server(deps=deps)) as client:
        yield client, deps
    await deps.aclose()


@pytest.fixture
async def slow_client(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> AsyncIterator[tuple[Client, Deps]]:
    """A server whose EODHD takes a moment to answer, as a real one does, with room for two reads only."""
    answers = mock_api.transport()

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.01)  # MockEODHD answers at once; real I/O gives other calls a turn
        return await answers.handle_async_request(request)

    deps = build_deps(
        replace(settings, daily_budget=25), transport=httpx2.MockTransport(slow), clock=clock, sleep=sleeps
    )
    async with Client(build_server(deps=deps)) as client:
        yield client, deps
    await deps.aclose()


@pytest.fixture
async def uncached_client(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> AsyncIterator[tuple[Client, Deps]]:
    deps = build_deps(
        replace(settings, cache_enabled=False), transport=mock_api.transport(), clock=clock, sleep=sleeps
    )
    async with Client(build_server(deps=deps)) as client:
        yield client, deps
    await deps.aclose()


# --- one request, many reads ------------------------------------------------------------------------


async def test_one_request_serves_every_later_read_of_the_symbol(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))

    first = body_of(await fundamentals(mcp_client, statements=["income"], periods=2))
    second = body_of(
        await fundamentals(
            mcp_client,
            sections=["Holders", "InsiderTransactions"],
            statements=["balance", "cashflow"],
            period="yearly",
            periods=5,
        )
    )

    assert first["meta"]["cost"] == 10
    assert first["meta"]["cached"] is False
    assert second["meta"]["cost"] == 0
    assert second["meta"]["cached"] is True
    assert mock_api.count("fundamentals") == 1
    assert deps.ledger.spent_today() == 10
    # No `filter` is sent, which is what makes every read share one cache entry.
    assert mock_api.requests[0].params == {"fmt": "json"}
    assert first["meta"]["endpoints"] == ["fundamentals"]


async def test_parallel_reads_of_one_document_send_one_request_and_pay_for_it_once(
    slow_client: tuple[Client, Deps], mock_api: MockEODHD
) -> None:
    client, deps = slow_client
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    # A model that wants three views of one company asks for them in parallel. The budget (25) has room for
    # two reads of 10 but not three, and three requests would have been paid for and refused alike.
    results = await asyncio.gather(
        fundamentals(client),
        fundamentals(client, sections=["Highlights"]),
        fundamentals(client, sections=["General"], statements=["income"]),
    )
    bodies = [body_of(result) for result in results]
    assert mock_api.count("fundamentals") == 1
    assert sorted(body["meta"]["cost"] for body in bodies) == [0, 0, 10]
    assert sorted(body["meta"]["cached"] for body in bodies) == [False, True, True]
    assert deps.ledger.spent_today() == 10
    assert deps.budget.status()["remaining"] == 15
    assert not any("BUDGET_EXCEEDED" in str(result.content) for result in results)
    today = deps.ledger.summary()["today"]
    assert (today["requests"], today["cache_hits"]) == (1, 2)
    for body in bodies:
        assert body["meta"]["fetched_at"] == bodies[0]["meta"]["fetched_at"]
    # The reads that waited were told, like any other cache hit, where their answer came from.
    waited = [body for body in bodies if body["meta"]["cached"]]
    assert all(body["meta"]["notes"][0].startswith("Served from the local cache.") for body in waited)


async def test_the_budget_refuses_a_read_it_cannot_afford_before_any_request(
    budgeted_client: tuple[Client, Deps], mock_api: MockEODHD
) -> None:
    client, deps = budgeted_client
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    # With 1 of the 10 spent, the 10-call read no longer fits.
    deps.ledger.record(endpoint="eod", cost=1, status=200, cached=False, duration_ms=1.0, bytes_=1)
    text = error_text(await fundamentals(client))
    assert "BUDGET_EXCEEDED" in text
    assert mock_api.requests == []


async def test_a_spent_budget_still_serves_the_cached_document_for_free(
    budgeted_client: tuple[Client, Deps], mock_api: MockEODHD
) -> None:
    client, deps = budgeted_client
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    first = body_of(await fundamentals(client))
    assert first["meta"]["cost"] == 10
    assert deps.ledger.spent_today() == 10
    assert "BUDGET_LOW" in codes(first)
    # The budget is used up, but the document is cached: every further read costs nothing.
    again = body_of(await fundamentals(client, statements=["income"], period="yearly"))
    assert again["meta"]["cost"] == 0
    assert mock_api.count("fundamentals") == 1
    # Another symbol would need a new 10-call read, which the spent budget now refuses.
    mock_api.add("fundamentals/ACMX.US", load_fixture("tools_fund_etf"))
    assert "BUDGET_EXCEEDED" in error_text(await fundamentals(client, symbol="ACMX.US"))
    assert mock_api.count("fundamentals/ACMX") == 0


async def test_the_document_is_cached_for_exactly_a_day(
    mcp_client: Client, mock_api: MockEODHD, clock: FixedClock
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    assert body_of(await fundamentals(mcp_client))["meta"]["cost"] == 10
    clock.advance(hours=23, minutes=59)
    assert body_of(await fundamentals(mcp_client, statements=["income"]))["meta"]["cost"] == 0
    clock.advance(minutes=2)
    after_a_day = body_of(await fundamentals(mcp_client))
    assert after_a_day["meta"]["cost"] == 10
    assert after_a_day["meta"]["cached"] is False
    assert mock_api.count("fundamentals") == 2


async def test_the_note_says_what_the_first_read_bought(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    first = body_of(await fundamentals(mcp_client))
    second = body_of(await fundamentals(mcp_client))
    assert "fetched once (10 calls) and is cached for 24 hours" in first["meta"]["notes"][0]
    assert "cost 0" in first["meta"]["notes"][0]
    assert second["meta"]["notes"][0].startswith("Served from the local cache.")
    assert "meta.fetched_at" in second["meta"]["notes"][0]


async def test_with_the_cache_off_the_note_says_that_every_read_costs_ten(
    uncached_client: tuple[Client, Deps], mock_api: MockEODHD
) -> None:
    client, deps = uncached_client
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    first = body_of(await fundamentals(client))
    second = body_of(await fundamentals(client, statements=["income"]))
    assert (first["meta"]["cost"], second["meta"]["cost"]) == (10, 10)
    assert (first["meta"]["cached"], second["meta"]["cached"]) == (False, False)
    assert mock_api.count("fundamentals") == 2
    assert deps.ledger.spent_today() == 20
    for body in (first, second):
        note = body["meta"]["notes"][0]
        assert "is not cached" in note
        assert "every read of this symbol costs 10 calls" in note
        assert "cost 0" not in note
        assert "cached for 24 hours" not in note


async def test_with_the_cache_off_the_description_says_what_a_read_costs(
    uncached_client: tuple[Client, Deps],
) -> None:
    client, _ = uncached_client
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    description = tools["get_fundamentals"].description
    assert description is not None
    assert "Every read of a symbol costs 10 API calls" in description
    assert "the cache is off" in description
    assert "cached for 24 hours" not in description
    assert "afterwards costs 0" not in description


async def test_with_the_cache_on_the_description_still_promises_free_follow_up_reads(
    mcp_client: Client,
) -> None:
    tools = {tool.name: tool for tool in (await mcp_client.list_tools()).tools}
    description = tools["get_fundamentals"].description
    assert description is not None
    assert "cached for 24 hours" in description
    assert "afterwards costs 0" in description


async def test_with_the_cache_off_a_cut_answer_does_not_say_that_asking_again_is_free(
    uncached_client: tuple[Client, Deps], mock_api: MockEODHD
) -> None:
    client, _ = uncached_client
    mock_api.add(ACME, with_statements(acme(), income=60, balance=60, cashflow=60))
    body = body_of(await fundamentals(client, statements=["income", "balance", "cashflow"], periods=40))
    message = warning_of(body, "TRUNCATED")["message"]
    assert "Ask again for fewer `statements` or `sections`" in message
    assert "the cache is off, so another read costs 10 API calls" in message
    assert "that costs nothing" not in message
    assert "stays cached" not in message


async def test_with_the_cache_off_an_empty_document_is_not_said_to_be_cached(
    uncached_client: tuple[Client, Deps], mock_api: MockEODHD
) -> None:
    client, _ = uncached_client
    mock_api.add(ACME, {})
    text = error_text(await fundamentals(client))
    assert "billed (10 calls) and the empty answer is not cached" in text
    assert "cached for 24 hours" not in text
    again = error_text(await fundamentals(client))
    assert "billed (10 calls)" in again
    assert mock_api.count("fundamentals") == 2


# --- sections and defaults --------------------------------------------------------------------------


async def test_a_stock_gets_the_default_sections_and_no_statements(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    data = body_of(await fundamentals(mcp_client))["data"]
    assert list(data["sections"]) == [
        "General",
        "Highlights",
        "Valuation",
        "SharesStats",
        "SplitsDividends",
        "Earnings",
    ]
    assert "statements" not in data
    assert "period" not in data
    assert data["symbol"] == "ACME.US"
    assert data["name"] == "Acme Industrial Holdings"
    assert data["type"] == "Common Stock"
    assert data["quote_currency"] == "USD"
    assert data["updated_at"] == "2026-10-30"


async def test_section_names_are_forgiving_and_a_comma_string_works(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    listed = body_of(await fundamentals(mcp_client, sections=["general", "Shares Stats", "technicals"]))
    assert list(listed["data"]["sections"]) == ["General", "SharesStats", "Technicals"]
    text = body_of(await fundamentals(mcp_client, sections="highlights, Valuation"))
    assert list(text["data"]["sections"]) == ["Highlights", "Valuation"]


async def test_an_empty_sections_list_means_statements_only(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    data = body_of(await fundamentals(mcp_client, sections=[], statements="income", periods=1))["data"]
    assert data["sections"] == {}
    assert list(data["statements"]) == ["income"]


async def test_the_etf_defaults_drop_the_holdings_but_keep_the_summaries(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/ACMX.US", load_fixture("tools_fund_etf"))
    body = body_of(await fundamentals(mcp_client, symbol="ACMX.US"))
    sections = body["data"]["sections"]
    assert list(sections) == ["General", "Technicals", "ETF_Data"]
    etf = sections["ETF_Data"]
    assert "Holdings" not in etf
    assert len(etf["Top_10_Holdings"]) == 10
    assert {"Sector_Weights", "World_Regions", "Asset_Allocation", "Ongoing_Charge"} <= set(etf)
    assert any("ETF_Data.Holdings (60 positions) dropped" in note for note in body["meta"]["notes"])
    # No statement-style guards on an ETF.
    assert codes(body) == []


async def test_the_fund_defaults_drop_the_full_holdings_list(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("fundamentals/GLBLX.US", load_fixture("tools_fund_fund"))
    body = body_of(await fundamentals(mcp_client, symbol="GLBLX.US"))
    sections = body["data"]["sections"]
    assert list(sections) == ["General", "MutualFund_Data"]
    assert "Holdings" not in sections["MutualFund_Data"]
    assert len(sections["MutualFund_Data"]["Top_Holdings"]) == 5
    assert any("MutualFund_Data.Holdings (30 positions) dropped" in note for note in body["meta"]["notes"])


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            {"General": {"Type": "ETF"}, "Technicals": {"Beta": 1}, "ETF_Data": {}},
            ["General", "Technicals", "ETF_Data"],
        ),
        (
            {"General": {"Type": "Mutual Fund"}, "MutualFund_Data": {"Yield": "1"}},
            ["General", "MutualFund_Data"],
        ),
        ({"General": {"Name": "No type"}, "ETF_Data": {"Yield": "1"}}, ["General", "ETF_Data"]),
        ({"General": {"Name": "No type"}, "MutualFund_Data": {"Yield": "1"}}, ["General", "MutualFund_Data"]),
        ({"General": {"Name": "Plain"}, "Highlights": {"PERatio": 1}}, ["General", "Highlights"]),
    ],
    ids=["etf-type", "fund-type", "etf-by-section", "fund-by-section", "stock-fallback"],
)
async def test_the_instrument_is_told_by_its_type_or_its_sections(
    mcp_client: Client, mock_api: MockEODHD, document: dict[str, Any], expected: list[str]
) -> None:
    mock_api.add(ACME, document)
    data = body_of(await fundamentals(mcp_client))["data"]
    assert list(data["sections"]) == expected


async def test_items_the_document_lacks_are_listed_not_failed(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/ACMX.US", load_fixture("tools_fund_etf"))
    body = body_of(
        await fundamentals(
            mcp_client, symbol="ACMX.US", sections=["General", "Highlights"], statements=["income"]
        )
    )
    assert body["data"]["not_in_document"] == {"sections": ["Highlights"], "statements": ["income"]}
    assert list(body["data"]["sections"]) == ["General"]
    assert body["data"]["statements"] == {}
    assert any("Not in EODHD's document" in note for note in body["meta"]["notes"])


async def test_a_period_kind_the_document_lacks_is_listed_too(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    for statement in doc["Financials"].values():
        statement["yearly"] = {}
    mock_api.add(ACME, doc)
    data = body_of(await fundamentals(mcp_client, statements=["income"], period="yearly"))["data"]
    assert data["not_in_document"] == {"statements": ["income"]}


async def test_a_small_document_comes_back_whole_without_a_trim_note(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = {"General": {"Type": "Common Stock", "Name": "Tiny", "Description": "Short."}}
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=["General"]))
    assert body["data"]["sections"]["General"] == {
        "Type": "Common Stock",
        "Name": "Tiny",
        "Description": "Short.",
    }
    assert not any(note.startswith("Cut to keep") for note in body["meta"]["notes"])


# --- trimming ---------------------------------------------------------------------------------------


async def test_general_loses_officers_address_phone_and_logo_and_cuts_the_description(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    body = body_of(await fundamentals(mcp_client, sections=["General"]))
    general = body["data"]["sections"]["General"]
    for dropped in ("Officers", "AddressData", "Phone", "LogoURL"):
        assert dropped not in general
    assert len(general["Description"]) <= 500
    assert general["Description"].endswith("...")
    assert general["Description"].startswith("Acme Industrial Holdings designs")
    assert general["WebURL"] == "https://www.example.invalid"
    assert general["Address"].startswith("1 Example Way")
    assert [listing["Exchange"] for listing in general["Listings"]] == ["NYSE", "XETRA"]
    note = next(n for n in body["meta"]["notes"] if n.startswith("Cut to keep"))
    assert "Officers, AddressData, Phone, LogoURL dropped" in note
    assert "Description cut to 500 characters" in note


async def test_dividends_by_year_keep_the_five_newest_years(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    body = body_of(await fundamentals(mcp_client, sections=["SplitsDividends"]))
    years = body["data"]["sections"]["SplitsDividends"]["NumberDividendsByYear"]
    assert [row["Year"] for row in years] == [2026, 2025, 2024, 2023, 2022]
    assert body["data"]["sections"]["SplitsDividends"]["ForwardAnnualDividendRate"] == 1.2


async def test_holders_keep_the_top_ten_and_insiders_the_newest_ten(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    body = body_of(await fundamentals(mcp_client, sections=["Holders", "InsiderTransactions"]))
    holders = body["data"]["sections"]["Holders"]
    assert [len(holders["Institutions"]), len(holders["Funds"])] == [10, 10]
    assert holders["Institutions"][0]["name"] == "Institution Holder 00"
    assert holders["Institutions"][-1]["name"] == "Institution Holder 09"
    insiders = body["data"]["sections"]["InsiderTransactions"]
    assert len(insiders) == 10
    dates = [row["date"] for row in insiders]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == "2026-10-20"
    note = next(n for n in body["meta"]["notes"] if n.startswith("Cut to keep"))
    assert "Holders.Institutions: top 10" in note
    assert "InsiderTransactions: newest 10" in note


async def test_earnings_lists_keep_the_newest_periods_newest_first(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    earnings = body_of(await fundamentals(mcp_client, sections=["Earnings"], periods=3))["data"]["sections"][
        "Earnings"
    ]
    assert [row["date"] for row in earnings["History"]] == ["2026-12-31", "2026-09-30", "2026-06-30"]
    assert [row["date"] for row in earnings["Trend"]] == ["2027-12-31", "2026-12-31", "2026-12-30"]
    assert [row["date"] for row in earnings["Annual"]] == ["2025-12-31", "2024-12-31", "2023-12-31"]
    # The upcoming quarter keeps its null actual: it has not been reported yet, and a note says so.
    assert earnings["History"][0]["epsActual"] is None
    assert earnings["History"][1]["epsActual"] == 1.17
    notes = body_of(await fundamentals(mcp_client, sections=["Earnings"], periods=3))["meta"]["notes"]
    assert any("have not reported yet (epsActual is null)" in note for note in notes)
    reported_only = body_of(await fundamentals(mcp_client, sections=["Earnings"], periods=1))
    assert [row["date"] for row in reported_only["data"]["sections"]["Earnings"]["History"]] == ["2026-12-31"]


async def test_no_upcoming_quarter_means_no_note_about_one(mcp_client: Client, mock_api: MockEODHD) -> None:
    doc = acme()
    del doc["Earnings"]["History"]["2026-12-31"]
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=["Earnings"], periods=3))
    assert body["data"]["sections"]["Earnings"]["History"][0]["date"] == "2026-09-30"
    assert not any("have not reported yet" in note for note in body["meta"]["notes"])


async def test_outstanding_shares_are_cut_like_the_other_series(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    shares = body_of(await fundamentals(mcp_client, sections=["outstandingShares"], periods=2))["data"][
        "sections"
    ]["outstandingShares"]
    assert [row["dateFormatted"] for row in shares["annual"]] == ["2025-12-31", "2024-12-31"]
    assert [row["dateFormatted"] for row in shares["quarterly"]] == ["2026-09-30", "2026-06-30"]


async def test_other_sections_pass_through_as_given(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    sections = body_of(
        await fundamentals(mcp_client, sections=["Technicals", "AnalystRatings", "ESGScores"])
    )["data"]["sections"]
    assert sections["Technicals"]["Beta"] == 1.05
    assert sections["AnalystRatings"]["StrongBuy"] == 8
    assert len(sections["ESGScores"]["ActivitiesInvolvement"]) == 2


async def test_short_lists_and_missing_parts_are_kept_as_they_are(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        ACME,
        {
            "General": {"Type": "Common Stock", "Description": "Short."},
            "SplitsDividends": {
                "ForwardAnnualDividendRate": 1.0,
                "NumberDividendsByYear": {"0": {"Year": 2025, "Count": 4}, "1": {"Year": 2026, "Count": 2}},
            },
            "Holders": {"Institutions": {"0": {"name": "A"}}, "Funds": []},
            "InsiderTransactions": {"0": {"date": "2026-10-01"}},
            "Earnings": {"Annual": {"2025-12-31": {"epsActual": 1.0}}},
            "outstandingShares": {"annual": [{"date": "2025", "shares": 5}]},
        },
    )
    body = body_of(
        await fundamentals(
            mcp_client,
            sections=[
                "General",
                "SplitsDividends",
                "Holders",
                "InsiderTransactions",
                "Earnings",
                "outstandingShares",
            ],
        )
    )
    sections = body["data"]["sections"]
    assert sections["SplitsDividends"]["NumberDividendsByYear"] == [
        {"Year": 2026, "Count": 2},
        {"Year": 2025, "Count": 4},
    ]
    assert sections["Holders"] == {"Institutions": [{"name": "A"}], "Funds": []}
    assert sections["InsiderTransactions"] == [{"date": "2026-10-01"}]
    # A row without its own date gets its key as the date.
    assert sections["Earnings"] == {"Annual": [{"date": "2025-12-31", "epsActual": 1.0}]}
    assert sections["outstandingShares"] == {"annual": [{"date": "2025", "shares": 5}]}
    assert not any(note.startswith("Cut to keep") for note in body["meta"]["notes"])
    # SplitsDividends without a dividend count list is passed through as it is.
    mock_api.add("fundamentals/NOLIST.US", {"SplitsDividends": {"ForwardAnnualDividendRate": 1.0}})
    bare = body_of(await fundamentals(mcp_client, symbol="NOLIST.US", sections=["SplitsDividends"]))
    assert bare["data"]["sections"]["SplitsDividends"] == {"ForwardAnnualDividendRate": 1.0}


async def test_lists_arriving_as_arrays_or_as_nonsense_are_handled(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(
        ACME,
        {
            "General": {"Type": "Common Stock", "Listings": [{"Code": "A", "Exchange": "NYSE"}]},
            "SplitsDividends": {
                "NumberDividendsByYear": [{"Year": y, "Count": 4} for y in range(2018, 2027)]
            },
            "Holders": {"Institutions": [{"name": f"I{i}"} for i in range(12)], "Funds": "none"},
            "InsiderTransactions": [{"date": f"2026-10-{i + 1:02d}"} for i in range(12)] + ["junk"],
            "Earnings": {
                "History": [
                    {"date": "2026-06-30", "epsActual": 1},
                    {"date": "2026-09-30", "epsActual": 2},
                    "junk",
                ],
                "Trend": 5,
                "Annual": {"latest": {"epsActual": 3}, "2025-12-31": {"epsActual": 2}},
            },
        },
    )
    sections = body_of(
        await fundamentals(
            mcp_client,
            sections=["General", "SplitsDividends", "Holders", "InsiderTransactions", "Earnings"],
        )
    )["data"]["sections"]
    assert sections["General"]["Listings"] == [{"Code": "A", "Exchange": "NYSE"}]
    assert [row["Year"] for row in sections["SplitsDividends"]["NumberDividendsByYear"]] == [
        2026,
        2025,
        2024,
        2023,
        2022,
    ]
    assert [row["name"] for row in sections["Holders"]["Institutions"]] == [f"I{i}" for i in range(10)]
    assert sections["Holders"]["Funds"] == []
    assert [row["date"] for row in sections["InsiderTransactions"]][:2] == ["2026-10-12", "2026-10-11"]
    assert len(sections["InsiderTransactions"]) == 10
    history = sections["Earnings"]["History"]
    assert [row["date"] for row in history] == ["2026-09-30", "2026-06-30"]
    assert sections["Earnings"]["Trend"] == []
    # Keys that are not dates sort last and are kept as given.
    assert sections["Earnings"]["Annual"] == [{"date": "2025-12-31", "epsActual": 2}, {"epsActual": 3}]


def test_name_lists_must_be_lists_or_comma_strings_of_known_names() -> None:
    assert parse_sections(None) is None
    assert parse_sections([]) == []
    assert parse_sections(" general , GENERAL,, Earnings ") == ["General", "Earnings"]
    assert parse_statements(None) == []
    assert parse_statements(("Income", "cash_flow")) == ["income", "cashflow"]
    for bad in (5, {"General": 1}, 1.5):
        with pytest.raises(GuardError) as caught:
            parse_sections(bad)
        assert caught.value.code is ErrorCode.INVALID_PARAMS
        assert "not a list of names" in str(caught.value)
    with pytest.raises(GuardError) as caught:
        parse_statements(["income", 5])
    assert "a name is text, not int" in str(caught.value)
    with pytest.raises(GuardError) as caught:
        parse_sections(["x" * 100])
    assert "x" * 50 in str(caught.value)
    assert "..." in str(caught.value)


# --- statements -------------------------------------------------------------------------------------


async def test_a_statement_is_its_currency_and_the_newest_periods_without_empty_fields(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    data = body_of(await fundamentals(mcp_client, sections=[], statements=["income"], periods=2))["data"]
    assert data["period"] == "quarterly"
    income = data["statements"]["income"]
    assert income["currency"] == "USD"
    assert [row["date"] for row in income["periods"]] == ["2026-09-30", "2026-06-30"]
    newest = income["periods"][0]
    assert newest["filing_date"] == "2026-11-04"
    assert newest["totalRevenue"] == "1180000000.00"
    # null, "NA" and "" are all gone, and so is the per-period currency that repeats the statement's.
    for empty in (
        "interestExpense",
        "nonRecurring",
        "otherItems",
        "discontinuedOperations",
        "currency_symbol",
    ):
        assert empty not in newest
    assert "currency" not in newest
    assert list(newest)[:2] == ["date", "filing_date"]


async def test_all_three_yearly_statements_by_comma_string(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    data = body_of(
        await fundamentals(
            mcp_client, sections=[], statements="income, balance, cashflow", period="yearly", periods=5
        )
    )["data"]
    assert data["period"] == "yearly"
    assert list(data["statements"]) == ["income", "balance", "cashflow"]
    for statement in data["statements"].values():
        assert [row["date"] for row in statement["periods"]] == [
            "2025-12-31",
            "2024-12-31",
            "2023-12-31",
            "2022-12-31",
            "2021-12-31",
        ]
    assert "totalAssets" in data["statements"]["balance"]["periods"][0]
    assert "freeCashFlow" in data["statements"]["cashflow"]["periods"][0]


async def test_statement_names_are_forgiving(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    data = body_of(
        await fundamentals(
            mcp_client, sections=[], statements=["Income Statement", "balance_sheet", "CashFlow", "income"]
        )
    )["data"]
    assert list(data["statements"]) == ["income", "balance", "cashflow"]


async def test_statements_listed_oldest_first_come_back_newest_first(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/GLOBEX.LSE", load_fixture("tools_fund_globex_lse"))
    data = body_of(await fundamentals(mcp_client, symbol="GLOBEX.LSE", sections=[], statements=["income"]))[
        "data"
    ]
    assert [row["date"] for row in data["statements"]["income"]["periods"]] == [
        "2026-09-30",
        "2026-06-30",
        "2026-03-31",
        "2025-12-31",
    ]


# --- guards -----------------------------------------------------------------------------------------


async def test_a_london_line_reporting_in_dollars_is_a_currency_mismatch(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/GLOBEX.LSE", load_fixture("tools_fund_globex_lse"))
    body = body_of(
        await fundamentals(
            mcp_client, symbol="GLOBEX.LSE", sections=["General"], statements=["income", "balance"]
        )
    )
    mismatch = warning_of(body, "CURRENCY_MISMATCH")
    assert mismatch["details"]["quote_currency"] == "GBX"
    assert mismatch["details"]["statement_currencies"] == {"income": ["USD"], "balance": ["USD"]}
    assert "quoted in GBX" in mismatch["message"]
    assert "pence" in mismatch["message"]
    assert "factor of 100" in mismatch["message"]
    assert "mixes currencies" not in mismatch["message"]


async def test_matching_currencies_raise_no_currency_warning(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    body = body_of(await fundamentals(mcp_client, statements=["income", "balance", "cashflow"]))
    assert "CURRENCY_MISMATCH" not in codes(body)


async def test_the_currency_check_follows_the_periods_that_are_returned(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/INITECH.SW", load_fixture("tools_fund_initech_mixed"))
    # The income statement is labelled USD, but only its two newest periods are: the older ones are CHF.
    two = body_of(
        await fundamentals(
            mcp_client,
            symbol="INITECH.SW",
            sections=[],
            statements=["income", "balance"],
            periods=2,
        )
    )
    detail = warning_of(two, "CURRENCY_MISMATCH")["details"]
    assert detail["quote_currency"] == "CHF"
    assert detail["statement_currencies"] == {"income": ["USD"]}

    four = body_of(
        await fundamentals(mcp_client, symbol="INITECH.SW", sections=[], statements=["income"], periods=4)
    )
    mixed = warning_of(four, "CURRENCY_MISMATCH")
    assert mixed["details"]["statement_currencies"] == {"income": ["USD", "CHF"]}
    assert "mixes currencies across periods" in mixed["message"]
    rows = four["data"]["statements"]["income"]["periods"]
    # A period keeps its own currency when it differs from the statement's, so nothing is hidden.
    assert ["currency" in row for row in rows] == [False, False, True, True]
    assert rows[2]["currency"] == "CHF"

    only_older = body_of(
        await fundamentals(mcp_client, symbol="INITECH.SW", sections=[], statements=["balance"], periods=4)
    )
    assert "CURRENCY_MISMATCH" not in codes(only_older)


async def test_without_a_quote_currency_there_is_nothing_to_compare(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    del doc["General"]["CurrencyCode"]
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, statements=["income"]))
    assert body["data"]["quote_currency"] is None
    assert "CURRENCY_MISMATCH" not in codes(body)


async def test_a_filing_date_equal_to_the_period_end_is_flagged(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/GLOBEX.LSE", load_fixture("tools_fund_globex_lse"))
    body = body_of(
        await fundamentals(mcp_client, symbol="GLOBEX.LSE", sections=[], statements=["income", "cashflow"])
    )
    warning = warning_of(body, "FILING_DATE_UNRELIABLE")
    assert warning["details"] == {
        "periods_affected": 8,
        "periods_returned": 8,
        "statements": ["cashflow", "income"],
    }
    assert "8 of 8" in warning["message"]
    assert "not the date the figures were published" in warning["message"]


async def test_a_real_filing_date_raises_no_warning_and_no_statements_means_no_check(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    with_statements_body = body_of(await fundamentals(mcp_client, statements=["income"]))
    assert "FILING_DATE_UNRELIABLE" not in codes(with_statements_body)
    mock_api.add("fundamentals/GLOBEX.LSE", load_fixture("tools_fund_globex_lse"))
    none_returned = body_of(await fundamentals(mcp_client, symbol="GLOBEX.LSE"))
    assert "FILING_DATE_UNRELIABLE" not in codes(none_returned)


async def test_rows_without_any_date_are_not_mistaken_for_equal_dates(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    # A list of rows with no dates at all: None equals None, which must not count as filing == period end.
    doc["Financials"]["Income_Statement"]["quarterly"] = [{"totalRevenue": "1.00"}, {"totalRevenue": "2.00"}]
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=[], statements=["income"]))
    assert body["data"]["statements"]["income"]["periods"] == [
        {"date": None, "totalRevenue": "1.00"},
        {"date": None, "totalRevenue": "2.00"},
    ]
    assert "FILING_DATE_UNRELIABLE" not in codes(body)


async def test_only_some_equal_filing_dates_are_counted(mcp_client: Client, mock_api: MockEODHD) -> None:
    doc = acme()
    quarterly = doc["Financials"]["Income_Statement"]["quarterly"]
    quarterly["2026-09-30"]["filing_date"] = "2026-09-30"
    quarterly["2026-06-30"]["filing_date"] = None
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=[], statements=["income"], periods=3))
    assert warning_of(body, "FILING_DATE_UNRELIABLE")["details"]["periods_affected"] == 1
    assert "filing_date" not in body["data"]["statements"]["income"]["periods"][1]


async def test_a_zero_yield_next_to_a_forward_dividend_is_flagged(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("fundamentals/GLOBEX.LSE", load_fixture("tools_fund_globex_lse"))
    body = body_of(await fundamentals(mcp_client, symbol="GLOBEX.LSE"))
    warning = warning_of(body, "DIVIDEND_YIELD_INCONSISTENT")
    assert warning["details"] == {"dividend_yield": 0, "forward_annual_dividend_rate": 0.62}
    assert "do not conclude that the stock pays no dividend" in warning["message"]


async def test_the_dividend_yield_warning_needs_all_three_conditions(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    # A healthy yield.
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    assert "DIVIDEND_YIELD_INCONSISTENT" not in codes(body_of(await fundamentals(mcp_client)))
    # Highlights not returned: the yield is not in front of the model.
    mock_api.add("fundamentals/GLOBEX.LSE", load_fixture("tools_fund_globex_lse"))
    general_only = body_of(await fundamentals(mcp_client, symbol="GLOBEX.LSE", sections=["General"]))
    assert "DIVIDEND_YIELD_INCONSISTENT" not in codes(general_only)
    # Zero yield and no forward rate: a company that pays nothing.
    for forward in (0, None):
        doc = acme()
        doc["Highlights"]["DividendYield"] = 0
        doc["SplitsDividends"]["ForwardAnnualDividendRate"] = forward
        mock_api.add("fundamentals/NODIV.US", doc)
    silent = body_of(await fundamentals(mcp_client, symbol="NODIV.US"))
    assert "DIVIDEND_YIELD_INCONSISTENT" not in codes(silent)
    # A missing yield is not a zero yield.
    doc = acme()
    doc["Highlights"]["DividendYield"] = None
    mock_api.add("fundamentals/NULLY.US", doc)
    assert "DIVIDEND_YIELD_INCONSISTENT" not in codes(
        body_of(await fundamentals(mcp_client, symbol="NULLY.US"))
    )


async def test_eps_basis_is_stated_whenever_earnings_is_returned(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    with_earnings = body_of(await fundamentals(mcp_client))
    assert "analyst (adjusted) basis" in warning_of(with_earnings, "EPS_BASIS")["message"]
    without = body_of(await fundamentals(mcp_client, sections=["General", "Highlights"]))
    assert "EPS_BASIS" not in codes(without)


async def test_the_arrival_lag_note_carries_the_update_date(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    mock_api.add("fundamentals/ACMX.US", load_fixture("tools_fund_etf"))
    stock = body_of(await fundamentals(mcp_client))
    lag = next(n for n in stock["meta"]["notes"] if "1 to 4 days" in n)
    assert "2026-10-30 (General.UpdatedAt)" in lag
    etf = body_of(await fundamentals(mcp_client, symbol="ACMX.US"))
    assert not any("1 to 4 days" in n for n in etf["meta"]["notes"])
    assert any("last updated 2026-11-01" in n for n in etf["meta"]["notes"])
    doc = acme()
    del doc["General"]["UpdatedAt"]
    mock_api.add("fundamentals/NODATE.US", doc)
    undated = body_of(await fundamentals(mcp_client, symbol="NODATE.US"))
    assert any("1 to 4 days" in n and "last updated" not in n for n in undated["meta"]["notes"])


# --- the cap ----------------------------------------------------------------------------------------


async def test_an_answer_under_the_cap_is_not_flagged(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    body = body_of(
        await fundamentals(
            mcp_client,
            sections=["General", "Highlights", "Earnings", "Holders", "InsiderTransactions"],
            statements=["income", "balance", "cashflow"],
            periods=40,
        )
    )
    assert "TRUNCATED" not in codes(body)
    assert sent_size(body["data"]) < CAP_BYTES


def statement_data(periods: int) -> dict[str, Any]:
    rows = [{"date": f"2026-{12 - i:02d}-01", "field": "x" * 30} for i in range(periods)]
    return {
        "symbol": "ACME.US",
        "sections": {},
        "statements": {"income": {"currency": "USD", "periods": rows}},
    }


def kept_dates(data: dict[str, Any]) -> list[str]:
    return [row["date"] for row in data["statements"]["income"]["periods"]]


def test_the_cap_is_inclusive_so_an_answer_of_exactly_the_cap_is_not_cut() -> None:
    data = statement_data(6)
    size = sent_size(data)
    assert _enforce_cap(data, size) is None
    assert len(kept_dates(data)) == 6
    warning = _enforce_cap(data, size - 1)
    assert warning is not None
    assert len(kept_dates(data)) == 5


def test_the_cap_keeps_as_many_of_the_newest_periods_as_fit() -> None:
    data = statement_data(10)
    # The size of four periods, to the byte: dropping a period also drops its comma, line break and indent.
    fits_four = sent_size(statement_data(4))
    warning = _enforce_cap(data, fits_four)
    assert warning is not None
    assert kept_dates(data) == ["2026-12-01", "2026-11-01", "2026-10-01", "2026-09-01"]
    assert sent_size(data) == fits_four
    assert warning.details is not None
    assert warning.details["size_after"] == fits_four
    assert warning.details["cut"] == {"statements.income": "newest 4 of 10"}


def test_the_size_is_the_indented_json_the_sdk_sends() -> None:
    value = {"a": [{"b": "é日本"}, {"c": [1, 2]}], "d": {}}
    assert _json_size(value) == sent_size(value)
    assert _json_size(value) > len(json.dumps(value, separators=(",", ":")))
    assert _json_size({"x": "é"}) == len(json.dumps({"x": "é"}, indent=2, ensure_ascii=False)) + 1


def test_a_value_json_cannot_write_is_measured_as_text() -> None:
    assert _json_size({"day": date(2026, 11, 2)}) == sent_size({"day": date(2026, 11, 2)})


ENTRIES = [
    {"date": "2026-09-30", "revenue": "1250000.00"},
    {
        "date": "2026-06-30",
        "note": "é日本 \U0001f600",
        "parts": [1, 2, {"deep": [3, {}]}],
        "empty": {},
        "none": [],
    },
    {"date": "2026-03-31", "currency": "USD", "text": "line one\nline two", "nested": {"a": {"b": {"c": 1}}}},
    {"date": "2025-12-31"},
]


@pytest.mark.parametrize("entry", range(len(ENTRIES)))
@pytest.mark.parametrize("series", ["Earnings.History", "statements.income", "outstandingShares.annual"])
def test_the_weight_of_an_entry_is_what_dropping_it_takes_off_the_size(series: str, entry: int) -> None:
    # Every list the cap cuts sits three levels down. Dropping its last entry takes off the entry itself, the
    # comma and line break before it, and the indent of each of its lines.
    def build(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if series.startswith("statements"):
            return {
                "symbol": "ACME.US",
                "sections": {},
                "statements": {"income": {"currency": "USD", "periods": rows}},
            }
        section, name = series.split(".")
        return {"symbol": "ACME.US", "sections": {section: {name: rows}}}

    rows = [ENTRIES[entry], ENTRIES[(entry + 1) % len(ENTRIES)]]
    before = sent_size(build(rows))
    after = sent_size(build(rows[:-1]))
    assert before - after == _entry_weight(rows[-1], 3)


@pytest.mark.parametrize("count", range(2, 9))
def test_the_cap_leaves_exactly_the_newest_entries_that_fit(count: int) -> None:
    rows = [{**ENTRIES[i % len(ENTRIES)], "date": f"2026-{12 - i:02d}-01"} for i in range(8)]
    full = {
        "symbol": "ACME.US",
        "sections": {},
        "statements": {"income": {"currency": "USD", "periods": rows}},
    }
    fits = copy.deepcopy(full)
    fits["statements"]["income"]["periods"] = rows[:count]
    cap = sent_size(fits)
    data = copy.deepcopy(full)
    warning = _enforce_cap(data, cap)
    assert data["statements"]["income"]["periods"] == rows[:count]
    assert sent_size(data) <= cap
    if count < 8:
        assert warning is not None
        assert warning.details is not None
        assert warning.details["size_after"] == sent_size(data)
        assert warning.details["cut"] == {"statements.income": f"newest {count} of 8"}
        # One byte less and the last entry that is left must go too.
        again = copy.deepcopy(full)
        _enforce_cap(again, cap - 1)
        assert len(again["statements"]["income"]["periods"]) == max(count - 1, 1)


def test_the_cap_cuts_several_series_and_reports_the_size_it_reached() -> None:
    def rows(n: int, width: int) -> list[dict[str, Any]]:
        return [
            {"date": f"2026-{12 - i:02d}-01", "text": "é" * width, "list": [i, {"k": "v"}]} for i in range(n)
        ]

    data = {
        "symbol": "ACME.US",
        "sections": {"Earnings": {"History": rows(30, 40), "Trend": rows(20, 10)}},
        "statements": {"income": {"currency": "USD", "periods": rows(25, 25)}},
    }
    total = sent_size(data)
    warning = _enforce_cap(data, total // 2)
    assert warning is not None
    assert warning.details is not None
    assert sent_size(data) <= total // 2
    assert warning.details["size_before"] == total
    assert warning.details["size_after"] == sent_size(data)
    kept = [
        len(data["sections"]["Earnings"]["History"]),
        len(data["sections"]["Earnings"]["Trend"]),
        len(data["statements"]["income"]["periods"]),
    ]
    assert all(n >= 1 for n in kept)
    assert sum(kept) < 75


async def test_a_huge_answer_is_cut_to_the_cap_keeping_the_newest_periods(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add(ACME, with_statements(acme(), income=60, balance=60, cashflow=60))
    body = body_of(await fundamentals(mcp_client, statements=["income", "balance", "cashflow"], periods=40))
    data = body["data"]
    truncated = warning_of(body, "TRUNCATED")
    assert sent_size(data) <= CAP_BYTES
    # No more was cut than it took: one more period of any series would have gone over the cap.
    assert sent_size(data) > CAP_BYTES - 6_000
    assert truncated["details"]["limit_bytes"] == CAP_BYTES
    assert truncated["details"]["size_before"] > CAP_BYTES
    assert truncated["details"]["size_after"] == sent_size(data)
    assert truncated["details"]["omitted"] == []
    cut = truncated["details"]["cut"]
    assert set(cut) <= {"statements.income", "statements.balance", "statements.cashflow", "Earnings.History"}
    assert {"statements.income", "statements.balance", "statements.cashflow"} <= set(cut)
    assert "Ask again for fewer `statements` or `sections`" in truncated["message"]
    assert "that costs nothing" in truncated["message"]
    for short in ("income", "balance", "cashflow"):
        dates = [row["date"] for row in data["statements"][short]["periods"]]
        assert 1 <= len(dates) < 40
        # The oldest went first: what is left is the newest run of quarters, unbroken.
        assert dates == quarter_ends(40)[: len(dates)]
        assert cut[f"statements.{short}"] == f"newest {len(dates)} of 40"
    # The big read was paid for once, and asking for less afterwards is free.
    assert body["meta"]["cost"] == 10
    smaller = body_of(await fundamentals(mcp_client, statements=["income"], periods=4))
    assert smaller["meta"]["cost"] == 0
    assert "TRUNCATED" not in codes(smaller)
    assert mock_api.count("fundamentals") == 1
    assert deps.ledger.spent_today() == 10


async def test_the_cap_takes_from_the_largest_series_first(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add(ACME, with_statements(acme(), income=170, balance=4, cashflow=4))
    body = body_of(
        await fundamentals(
            mcp_client, sections=["General"], statements=["income", "balance", "cashflow"], periods=40
        )
    )
    assert sent_size(body["data"]) <= CAP_BYTES
    assert list(warning_of(body, "TRUNCATED")["details"]["cut"]) == ["statements.income"]
    assert len(body["data"]["statements"]["balance"]["periods"]) == 40
    assert len(body["data"]["statements"]["cashflow"]["periods"]) == 40
    assert len(body["data"]["statements"]["income"]["periods"]) < 40


async def test_whole_sections_go_only_when_cutting_periods_is_not_enough(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    doc["ESGScores"]["ActivitiesInvolvement"] = [
        {"Activity": f"Activity {i}", "Involvement": "No" * 20} for i in range(5000)
    ]
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=["General", "ESGScores", "Earnings"], periods=4))
    truncated = warning_of(body, "TRUNCATED")
    assert truncated["details"]["omitted"] == ["ESGScores"]
    assert "Whole sections were dropped (ESGScores)" in truncated["message"]
    assert list(body["data"]["sections"]) == ["General", "Earnings"]
    assert sent_size(body["data"]) <= CAP_BYTES


async def test_a_statement_can_be_dropped_whole_as_a_last_resort(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    # One period so big that it cannot be cut to fit; the other statement is small. A long text would not do:
    # texts are cut to 200 characters, so the size has to come from the number of fields.
    big = doc["Financials"]["Income_Statement"]["quarterly"]["2026-09-30"]
    big.update({f"field{i:05d}": "1234567.00" for i in range(12_000)})
    mock_api.add(ACME, doc)
    body = body_of(
        await fundamentals(mcp_client, sections=["General"], statements=["income", "balance"], periods=1)
    )
    truncated = warning_of(body, "TRUNCATED")
    assert truncated["details"]["omitted"] == ["statements.income"]
    assert list(body["data"]["statements"]) == ["balance"]
    assert sent_size(body["data"]) <= CAP_BYTES


async def test_a_general_section_that_alone_exceeds_the_cap_is_reported_not_hidden(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    doc["General"]["Extra"] = {f"key{i:05d}": "x" * 30 for i in range(6_000)}
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=["General"]))
    truncated = warning_of(body, "TRUNCATED")
    assert "Nothing more could be cut." in truncated["message"]
    assert truncated["details"]["size_after"] > CAP_BYTES
    assert list(body["data"]["sections"]) == ["General"]


# --- text from the vendor ---------------------------------------------------------------------------

LONG = "Alexander Hamilton Holdings " * 30
CUT = clip(LONG.strip())


async def test_long_names_and_other_texts_are_cut_to_200_characters_with_an_ellipsis(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    assert 190 < len(CUT) <= 200
    assert CUT.endswith("...")
    doc = acme()
    doc["General"].update({"Name": LONG, "Sector": LONG, "Industry": LONG, "CountryName": LONG})
    doc["Holders"]["Institutions"]["0"]["name"] = LONG
    doc["InsiderTransactions"]["0"]["ownerName"] = LONG
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=["General", "Holders", "InsiderTransactions"]))
    data = body["data"]
    general = data["sections"]["General"]
    assert data["name"] == CUT
    assert (general["Name"], general["Sector"], general["Industry"], general["CountryName"]) == (CUT,) * 4
    assert data["sections"]["Holders"]["Institutions"][0]["name"] == CUT
    assert data["sections"]["InsiderTransactions"][0]["ownerName"] == CUT
    assert LONG.strip() not in json.dumps(body)


async def test_the_description_keeps_its_own_limit_of_500_characters(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    doc["General"]["Description"] = "d" * 450
    mock_api.add(ACME, doc)
    kept = body_of(await fundamentals(mcp_client, sections=["General"]))
    assert kept["data"]["sections"]["General"]["Description"] == "d" * 450
    doc["General"]["Description"] = "d" * 900
    mock_api.add("fundamentals/LONGD.US", doc)
    cut = body_of(await fundamentals(mcp_client, symbol="LONGD.US", sections=["General"]))
    description = cut["data"]["sections"]["General"]["Description"]
    assert len(description) == 500
    assert description.endswith("...")


async def test_the_text_fields_of_an_etf_are_cut_too(mcp_client: Client, mock_api: MockEODHD) -> None:
    doc = copy.deepcopy(load_fixture("tools_fund_etf"))
    doc["ETF_Data"]["Company_Name"] = LONG
    doc["ETF_Data"]["Index_Name"] = LONG
    doc["ETF_Data"]["Top_10_Holdings"]["HLD00.US"]["Name"] = LONG
    mock_api.add("fundamentals/ACMX.US", doc)
    etf = body_of(await fundamentals(mcp_client, symbol="ACMX.US"))["data"]["sections"]["ETF_Data"]
    assert (etf["Company_Name"], etf["Index_Name"]) == (CUT, CUT)
    assert etf["Top_10_Holdings"]["HLD00.US"]["Name"] == CUT


async def test_short_texts_and_numbers_pass_through_untouched(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, load_fixture("tools_fund_acme_stock"))
    data = body_of(await fundamentals(mcp_client, sections=["General", "Highlights"]))["data"]
    assert data["name"] == "Acme Industrial Holdings"
    assert data["sections"]["General"]["Sector"] == "Industrials"
    assert data["sections"]["Highlights"]["DividendYield"] == acme()["Highlights"]["DividendYield"]


async def test_a_long_currency_stays_out_of_the_sentence_and_is_cut_in_the_details(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    doc["Financials"]["Income_Statement"]["currency_symbol"] = LONG
    for row in doc["Financials"]["Income_Statement"]["quarterly"].values():
        row["currency_symbol"] = LONG
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, statements=["income"], sections=["General"]))
    mismatch = warning_of(body, "CURRENCY_MISMATCH")
    assert mismatch["details"]["statement_currencies"] == {"income": [CUT]}
    assert f"income: {clip(LONG.strip(), 12)}" in mismatch["message"]
    assert "Hamilton" not in mismatch["message"]


# --- documents that are not what they should be -----------------------------------------------------


@pytest.mark.parametrize("holdings", [5, 2.5, True, "many", None, []], ids=repr)
async def test_holdings_that_are_not_a_list_do_not_crash_the_etf_trimmer(
    mcp_client: Client, mock_api: MockEODHD, holdings: Any
) -> None:
    doc = copy.deepcopy(load_fixture("tools_fund_etf"))
    doc["ETF_Data"]["Holdings"] = holdings
    mock_api.add("fundamentals/ACMX.US", doc)
    body = body_of(await fundamentals(mcp_client, symbol="ACMX.US"))
    assert "Holdings" not in body["data"]["sections"]["ETF_Data"]
    assert any("ETF_Data.Holdings (0 positions) dropped" in note for note in body["meta"]["notes"])


async def test_holdings_that_are_a_list_or_a_dict_are_counted(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = copy.deepcopy(load_fixture("tools_fund_etf"))
    doc["ETF_Data"]["Holdings"] = {"a": {}, "b": {}, "c": {}}
    mock_api.add("fundamentals/ACMX.US", doc)
    body = body_of(await fundamentals(mcp_client, symbol="ACMX.US"))
    assert any("ETF_Data.Holdings (3 positions) dropped" in note for note in body["meta"]["notes"])


async def test_a_number_too_large_for_a_float_is_not_a_number(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    doc["SplitsDividends"]["NumberDividendsByYear"] = {
        "0": {"Year": 10**400, "Count": 1},
        "1": {"Year": 2025, "Count": 4},
        "2": {"Year": 2024, "Count": 4},
    }
    doc["Highlights"]["DividendYield"] = 10**400
    doc["SplitsDividends"]["ForwardAnnualDividendRate"] = 0.8
    mock_api.add(ACME, doc)
    body = body_of(await fundamentals(mcp_client, sections=["Highlights", "SplitsDividends"]))
    years = body["data"]["sections"]["SplitsDividends"]["NumberDividendsByYear"]
    # The row whose year is no number sorts as year 0, after the real ones.
    assert [row["Year"] for row in years] == [2025, 2024, 10**400]
    # A yield that is not a number is not a yield of 0 either.
    assert "DIVIDEND_YIELD_INCONSISTENT" not in codes(body)


# --- errors -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [{}, []], ids=["empty-object", "empty-array"])
async def test_an_empty_document_is_no_data_not_a_quiet_success(
    mcp_client: Client, mock_api: MockEODHD, empty: Any
) -> None:
    mock_api.add(ACME, empty)
    text = error_text(await fundamentals(mcp_client))
    assert text.startswith("Error executing tool get_fundamentals: NO_DATA: ACME.US")
    assert "empty document" in text
    assert "billed (10 calls)" in text
    assert "check_symbol" in text
    # The empty answer is cached, so asking again is free and the message says why it is not billed.
    again = error_text(await fundamentals(mcp_client))
    assert "NO_DATA" in again
    assert "comes from the cache" in again
    assert mock_api.count("fundamentals") == 1


@pytest.mark.parametrize("odd", ["just text", [1, 2], 7], ids=["string", "list", "number"])
async def test_a_body_that_is_not_a_document_is_a_bad_response(
    mcp_client: Client, mock_api: MockEODHD, odd: Any
) -> None:
    mock_api.add(ACME, odd)
    text = error_text(await fundamentals(mcp_client))
    assert "BAD_RESPONSE" in text
    assert "not a company document" in text


async def test_a_404_is_not_found_with_a_way_forward(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("fundamentals/NOSUCH.US", status=404, text="Symbol not found")
    text = error_text(await fundamentals(mcp_client, symbol="NOSUCH.US"))
    assert "NOT_FOUND: NOSUCH.US" in text
    assert "Symbol not found" in text
    assert "search_symbols" in text
    assert "list_exchange_symbols(delisted=true)" in text


async def test_a_403_names_the_plans_that_include_fundamentals(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add(ACME, status=403, text="Forbidden")
    text = error_text(await fundamentals(mcp_client))
    assert "PLAN_ERROR" in text
    assert "Fundamentals Data Feed or All-in-One" in text
    assert mock_api.count("fundamentals") == 1


async def test_tokyo_is_refused_before_any_request(mcp_client: Client, mock_api: MockEODHD) -> None:
    text = error_text(await fundamentals(mcp_client, symbol="7203.TSE"))
    assert "NOT_COVERED" in text
    assert "Tokyo" in text
    assert mock_api.requests == []


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"symbol": "ACME"}, "no exchange suffix"),
        ({"sections": ["Nonsense"]}, "sections: received 'Nonsense'"),
        ({"sections": "General,Foo"}, "Accepted: General, Highlights"),
        ({"sections": ["Financials"]}, "Financial statements are chosen with the statements parameter"),
        ({"statements": ["ebitda"]}, "statements: received 'ebitda'"),
        ({"statements": ["cash"]}, "Accepted: income, balance, cashflow"),
        ({"periods": 0}, "periods: received 0"),
        ({"periods": 41}, "a whole number from 1 to 40"),
        ({"sections": [], "statements": []}, "nothing is requested"),
        ({"sections": ""}, "nothing is requested"),
    ],
    ids=[
        "bare-symbol",
        "unknown-section",
        "unknown-in-string",
        "financials-section",
        "unknown-statement",
        "ambiguous-statement",
        "zero-periods",
        "too-many-periods",
        "nothing-requested",
        "blank-sections",
    ],
)
async def test_bad_input_is_refused_before_any_request(
    mcp_client: Client, mock_api: MockEODHD, arguments: dict[str, Any], expected: str
) -> None:
    text = error_text(await fundamentals(mcp_client, **arguments))
    assert "INVALID_PARAMS" in text
    assert expected in text
    assert mock_api.requests == []


async def test_a_wrong_period_is_stopped_by_the_schema(mcp_client: Client, mock_api: MockEODHD) -> None:
    text = error_text(await fundamentals(mcp_client, period="daily"))
    assert "quarterly" in text
    assert "yearly" in text
    assert mock_api.requests == []


# --- the key ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (401, "Unauthorized"),
        (402, "Payment Required"),
        (403, f"Forbidden for token {TEST_KEY}"),
        (404, f"Symbol not found for {TEST_KEY}"),
        (500, f"internal error, api_token={TEST_KEY}"),
    ],
)
async def test_the_key_is_in_no_error_result(
    mcp_client: Client, mock_api: MockEODHD, status: int, text: str
) -> None:
    mock_api.add(ACME, status=status, text=text)
    result = await fundamentals(mcp_client)
    assert result.is_error
    assert TEST_KEY not in everything(result)
    # The key really was sent, so the absence above is not an artefact of a request that never left.
    assert mock_api.requests[0].api_token == TEST_KEY


async def test_the_key_is_in_no_successful_result_even_if_the_document_echoes_it(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    doc = acme()
    doc["General"]["Name"] = f"Acme {TEST_KEY} Holdings"
    mock_api.add(ACME, doc)
    result = await fundamentals(mcp_client, statements=["income"])
    assert not result.is_error
    assert TEST_KEY not in everything(result)
    assert mock_api.requests[0].api_token == TEST_KEY


# --- the tool as the model sees it ------------------------------------------------------------------


async def test_the_tool_is_described_with_its_cost_and_every_parameter(mcp_client: Client) -> None:
    tools = {tool.name: tool for tool in (await mcp_client.list_tools()).tools}
    tool = tools["get_fundamentals"]
    assert tool.description is not None
    assert "10 API calls" in tool.description
    assert "cached for 24 hours" in tool.description
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    properties = tool.input_schema["properties"]
    assert set(properties) == {"symbol", "sections", "statements", "period", "periods"}
    for name, schema in properties.items():
        assert schema.get("description"), name
    assert properties["period"]["enum"] == ["quarterly", "yearly"]
    assert tool.input_schema["required"] == ["symbol"]
