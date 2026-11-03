"""get_usage, through the real MCP path."""

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
from eodhd_guard_mcp.costs import COSTS
from eodhd_guard_mcp.server import Deps, build_deps, build_server
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, RecordingSleep, load_fixture

pytestmark = pytest.mark.anyio

PERSONAL_DETAILS = (
    "Zelda",
    "sentinel@example.invalid",
    "example.invalid",
    "SENTINEL-CARD-4242",
    "SENTINEL-INVITE-7781",
    "paymentMethod",
    "inviteToken",
    "email",
)

ACCOUNT = {
    "api_requests_today": 120,
    "requests_date": "2026-11-03",
    "daily_rate_limit": 100000,
    "extra_limit": 500,
    "subscription_type": "synthetic-plan",
    "subscription_mode": "synthetic",
}


async def usage(client: Client, **options: Any) -> CallToolResult:
    return await client.call_tool("get_usage", options)


def ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def failure(result: CallToolResult) -> str:
    assert result.is_error
    return "\n".join(getattr(block, "text", "") for block in result.content)


def everything(result: CallToolResult) -> str:
    """All the text a result carries: the structured content and every content block."""
    return json.dumps(result.structured_content) + "\n".join(
        getattr(block, "text", "") for block in result.content
    )


def spend(deps: Deps, cost: int, endpoint: str = "eod") -> None:
    deps.ledger.record(endpoint=endpoint, cost=cost, status=200, cached=False, duration_ms=1.0, bytes_=10)


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


async def test_usage_has_the_ledger_the_budget_the_account_and_the_price_list(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add("eod/ACME.US", [])
    mock_api.add("user", load_fixture("tools_ref_user"))
    await deps.client.get("eod/ACME.US", endpoint="eod", cost=1, ttl_s=60, symbol="ACME.US")
    await deps.client.get("eod/ACME.US", endpoint="eod", cost=1, ttl_s=60, symbol="ACME.US")

    body = ok(await usage(mcp_client))
    data = body["data"]
    assert list(data) == ["ledger", "budget", "account", "cost_table"]

    today = data["ledger"]["today"]
    assert (today["day"], today["cost"], today["requests"], today["cache_hits"]) == ("2026-11-03", 1, 1, 1)
    assert today["by_endpoint"] == {"eod": {"cost": 1, "requests": 1, "cache_hits": 1}}
    assert data["ledger"]["days"] == [{"day": "2026-11-03", "cost": 1, "requests": 1, "cache_hits": 1}]
    assert data["ledger"]["session"]["cost"] == 1

    assert data["budget"] == {
        "daily_limit": 1000,
        "spent_today": 1,
        "remaining": 999,
        "resets_at": "2026-11-04T00:00:00Z",
    }
    assert data["account"] == ACCOUNT
    assert set(data["cost_table"]) == set(COSTS)
    assert {
        key: data["cost_table"][key] for key in ("eod", "intraday", "real-time", "fundamentals", "user")
    } == {
        "eod": 1,
        "intraday": 5,
        "real-time": 1,
        "fundamentals": 10,
        "user": 0,
    }

    assert (body["meta"]["cost"], body["meta"]["endpoints"], body["meta"]["cached"]) == (0, ["user"], False)
    notes = " ".join(body["meta"]["notes"])
    assert "per GMT day" in notes
    assert "includes other apps that share the key" in notes
    assert "this ledger counts only this server" in notes
    assert "Cached answers cost 0" in notes
    assert "counter is dated" not in notes
    assert body["warnings"] == []


async def test_the_ledger_is_read_before_the_free_account_call_writes_its_own_row(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("user", load_fixture("tools_ref_user"))
    first = ok(await usage(mcp_client))["data"]["ledger"]["today"]
    assert (first["requests"], first["by_endpoint"]) == (0, {})
    second = ok(await usage(mcp_client))["data"]["ledger"]["today"]
    assert second["by_endpoint"] == {"user": {"cost": 0, "requests": 1, "cache_hits": 0}}
    assert second["cost"] == 0
    assert mock_api.count("user") == 2


@pytest.mark.parametrize("include_account", [True, False])
async def test_no_personal_detail_of_the_account_holder_is_ever_returned(
    mcp_client: Client, mock_api: MockEODHD, include_account: bool
) -> None:
    mock_api.add("user", load_fixture("tools_ref_user"))
    result = await usage(mcp_client, include_account=include_account)
    body = ok(result)
    text = everything(result)
    for detail in PERSONAL_DETAILS:
        assert detail not in text
    if include_account:
        assert set(body["data"]["account"]) == set(ACCOUNT)


async def test_without_the_account_part_no_request_is_made(mcp_client: Client, mock_api: MockEODHD) -> None:
    body = ok(await usage(mcp_client, include_account=False))
    assert body["data"]["account"] is None
    assert "account_error" not in body["data"]
    assert (body["meta"]["cost"], body["meta"]["endpoints"]) == (0, [])
    assert mock_api.requests == []
    assert body["data"]["budget"]["daily_limit"] == 1000


async def test_the_account_counters_are_never_cached(mcp_client: Client, mock_api: MockEODHD) -> None:
    mock_api.add("user", load_fixture("tools_ref_user"))
    await usage(mcp_client)
    again = ok(await usage(mcp_client))
    assert again["data"]["account"] == ACCOUNT
    assert mock_api.count("user") == 2


async def test_a_failing_account_call_leaves_the_local_part_intact(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add("user", status=401, text="Unauthorized")
    spend(deps, 7)
    body = ok(await usage(mcp_client))
    data = body["data"]
    assert data["account"] is None
    assert data["account_error"].startswith("AUTH_ERROR: EODHD rejected the API key (HTTP 401)")
    assert data["ledger"]["today"]["cost"] == 7
    assert data["budget"]["spent_today"] == 7
    assert "cost_table" in data
    assert any("account counters could not be read" in note for note in body["meta"]["notes"])
    assert body["meta"]["endpoints"] == []


async def test_a_missing_key_does_not_stop_the_local_part(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    async with client_with(replace(settings, api_key=None), mock_api, clock, sleeps) as client:
        body = ok(await usage(client))
    assert body["data"]["account"] is None
    assert body["data"]["account_error"].startswith("CONFIG_ERROR: ")
    assert body["data"]["budget"]["daily_limit"] == 1000
    assert mock_api.requests == []


async def test_an_error_that_echoes_the_key_is_masked_in_the_account_error(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("user", status=418, text=f"api_token={TEST_KEY} is a teapot")
    result = await usage(mcp_client)
    body = ok(result)
    assert body["data"]["account_error"].startswith("UPSTREAM_ERROR: EODHD answered an unexpected HTTP 418")
    assert "***" in body["data"]["account_error"]
    assert TEST_KEY not in everything(result)


async def test_an_unlimited_budget_says_so(
    settings: Settings,
    mock_api: MockEODHD,
    clock: FixedClock,
    sleeps: RecordingSleep,
    isolated_logging: None,
) -> None:
    mock_api.add("user", load_fixture("tools_ref_user"))
    async with client_with(replace(settings, daily_budget=0), mock_api, clock, sleeps) as client:
        body = ok(await usage(client))
    assert (body["data"]["budget"]["daily_limit"], body["data"]["budget"]["remaining"]) == (0, None)
    assert any("means no limit" in note for note in body["meta"]["notes"])


async def test_a_counter_dated_another_day_is_not_presented_as_todays(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("user", {**load_fixture("tools_ref_user"), "apiRequestsDate": "2026-11-02"})
    body = ok(await usage(mcp_client))
    assert body["data"]["account"]["requests_date"] == "2026-11-02"
    assert (
        "EODHD's request counter is dated 2026-11-02, not today (2026-11-03): api_requests_today is the "
        "count for that day." in body["meta"]["notes"]
    )


async def test_a_counter_without_a_readable_date_adds_no_note(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("user", {**load_fixture("tools_ref_user"), "apiRequestsDate": None})
    body = ok(await usage(mcp_client))
    assert body["data"]["account"]["requests_date"] is None
    assert not any("counter is dated" in note for note in body["meta"]["notes"])


async def test_an_account_answer_that_is_not_an_object_is_reported_in_the_account_error(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("user", ["not", "an", "object"])
    body = ok(await usage(mcp_client))
    assert body["data"]["account"] is None
    assert body["data"]["account_error"].startswith("BAD_RESPONSE: ")


async def test_days_select_how_much_of_the_ledger_is_totalled(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps, clock: FixedClock
) -> None:
    clock.advance(days=-4)
    spend(deps, 5)
    clock.advance(days=3)
    spend(deps, 3)
    clock.advance(days=1)
    spend(deps, 2)

    def listed(body: dict[str, Any]) -> list[tuple[str, int]]:
        return [(row["day"], row["cost"]) for row in body["data"]["ledger"]["days"]]

    assert listed(ok(await usage(mcp_client, days=1, include_account=False))) == [("2026-11-03", 2)]
    assert listed(ok(await usage(mcp_client, days=2, include_account=False))) == [
        ("2026-11-03", 2),
        ("2026-11-02", 3),
    ]
    assert listed(ok(await usage(mcp_client, days=7, include_account=False))) == [
        ("2026-11-03", 2),
        ("2026-11-02", 3),
        ("2026-10-30", 5),
    ]
    assert listed(ok(await usage(mcp_client, include_account=False))) == listed(
        ok(await usage(mcp_client, days=7, include_account=False))
    )
    assert listed(ok(await usage(mcp_client, days=90, include_account=False)))[-1] == ("2026-10-30", 5)


@pytest.mark.parametrize("days", [0, -1, 91])
async def test_days_outside_one_to_ninety_are_refused_before_any_call(
    mcp_client: Client, mock_api: MockEODHD, days: int
) -> None:
    assert "days" in failure(await usage(mcp_client, days=days))
    assert mock_api.requests == []


async def test_the_budget_warning_comes_with_the_numbers_and_the_account_call_is_not_blocked(
    mcp_client: Client, mock_api: MockEODHD, deps: Deps
) -> None:
    mock_api.add("user", load_fixture("tools_ref_user"))
    spend(deps, 1000)
    body = ok(await usage(mcp_client))
    assert [warning["code"] for warning in body["warnings"]] == ["BUDGET_LOW"]
    assert body["data"]["budget"]["remaining"] == 0
    assert body["data"]["account"] == ACCOUNT


async def test_the_api_key_is_in_no_usage_result_whatever_happens(
    mcp_client: Client, mock_api: MockEODHD
) -> None:
    mock_api.add("user", load_fixture("tools_ref_user"), times=1)
    mock_api.add("user", status=418, text=f"token {TEST_KEY} rejected")
    results = [
        await usage(mcp_client),
        await usage(mcp_client),
        await usage(mcp_client, include_account=False),
        await usage(mcp_client, days=0),
    ]
    assert [result.is_error for result in results] == [False, False, False, True]
    for result in results:
        assert TEST_KEY not in everything(result)
