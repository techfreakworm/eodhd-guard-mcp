"""A short check against the real EODHD API: about six API calls.

Run it with ``EODHD_GUARD_LIVE=1`` and a key in ``EODHD_API_KEY`` (or ``EODHD_API_KEY_FILE``):

    EODHD_GUARD_LIVE=1 pytest -m live

It uses a temporary state directory and a budget of 25 calls, so it can never spend more than that.
Assertions check statuses, codes and costs, never prices.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.server import build_deps, build_server

LIVE = os.environ.get("EODHD_GUARD_LIVE") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(not LIVE, reason="set EODHD_GUARD_LIVE=1 to call the real EODHD API"),
]


@pytest.fixture
async def live(tmp_path: Path, isolated_logging: None) -> AsyncIterator[tuple[Client, str]]:
    settings = replace(Settings.from_env(), state_dir=tmp_path / "state", daily_budget=25)
    if not settings.api_key:
        pytest.skip("no EODHD_API_KEY or EODHD_API_KEY_FILE")
    deps = build_deps(settings)
    try:
        async with Client(build_server(deps=deps)) as client:
            yield client, settings.api_key
    finally:
        await deps.aclose()


def body(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content[0].text
    return dict(result.structured_content)


async def test_a_live_round_trip_catches_the_known_failures(live: tuple[Client, str]) -> None:
    client, key = live
    seen: list[Any] = []

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        result = await client.call_tool(name, arguments)
        seen.append(result)
        return result

    account = body(await call("get_usage", {"days": 1}))["data"]["account"]
    assert account["daily_rate_limit"]

    prices = body(await call("get_eod_prices", {"symbol": "AAPL.US"}))
    assert prices["meta"]["cost"] == 1
    assert prices["data"]["count"] >= 10
    assert body(await call("get_eod_prices", {"symbol": "AAPL.US"}))["meta"]["cost"] == 0

    quotes = body(await call("get_quote", {"symbols": ["AAPL.US", "ROG.SW"]}))
    statuses = {entry["symbol"]: entry["status"] for entry in quotes["data"]}
    assert statuses == {"AAPL.US": "ok", "ROG.SW": "no_quote"}
    assert "DELAYED_QUOTE" in [w["code"] for w in quotes["warnings"]]

    health = body(await call("check_symbol", {"symbol": "ROG.SW"}))["data"]
    assert health["status"] == "stale"
    assert "ROP.SW" in [c["symbol"] for c in health["successor_candidates"]]

    tokyo = await call("get_eod_prices", {"symbol": "7203.TSE"})
    assert tokyo.is_error
    assert "NOT_COVERED" in tokyo.content[0].text

    ledger = body(await call("get_usage", {"days": 1, "include_account": False}))["data"]["ledger"]
    assert ledger["today"]["cost"] <= 10

    for result in seen:
        assert key not in json.dumps([c.model_dump() for c in result.content])
        assert key not in json.dumps(result.structured_content or {})
