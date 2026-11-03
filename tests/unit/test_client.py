from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
import traceback
import zlib
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx2
import pytest

from eodhd_guard_mcp.cache import ResponseCache
from eodhd_guard_mcp.client import (
    ACCOUNT_FIELDS,
    ApiResponse,
    _retry_after_seconds,
    _retry_delay,
)
from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.ledger import UsageLedger
from eodhd_guard_mcp.redact import install_log_redaction
from eodhd_guard_mcp.server import Deps, build_deps
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, RecordingSleep, load_fixture

pytestmark = pytest.mark.anyio

EOD = "eod/ACME.US"
DATA = load_fixture("core_eod_acme")
SECRET_URL = f"https://eodhd.test/api/eod/ACME.US?api_token={TEST_KEY}&fmt=json"


async def fetch(
    deps: Deps,
    path: str = EOD,
    params: dict[str, Any] | None = None,
    *,
    endpoint: str = "eod",
    cost: int = 1,
    ttl_s: float = 3600,
    symbol: str | None = "ACME.US",
) -> ApiResponse:
    return await deps.client.get(path, params, endpoint=endpoint, cost=cost, ttl_s=ttl_s, symbol=symbol)


def ledger_rows(settings: Settings) -> list[tuple[Any, ...]]:
    """(endpoint, cost, status, cached, symbol) for every row in the ledger, oldest first."""
    with sqlite3.connect(settings.state_dir / "usage.sqlite") as conn:
        return conn.execute("SELECT endpoint, cost, status, cached, symbol FROM calls ORDER BY id").fetchall()


def assert_no_key(error: GuardError) -> None:
    """The key is in no text a GuardError can show, and no exception is chained to it."""
    texts = [
        str(error),
        repr(error),
        error.message,
        error.hint or "",
        repr(error.details),
        "".join(traceback.format_exception(error)),
    ]
    assert all(TEST_KEY not in text for text in texts)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.fixture
async def make_deps(
    settings: Settings, mock_api: MockEODHD, clock: FixedClock, sleeps: RecordingSleep
) -> AsyncIterator[Callable[..., Deps]]:
    """Build a Deps with some settings changed; everything is closed afterwards."""
    built: list[Deps] = []

    def factory(**changes: Any) -> Deps:
        new = build_deps(
            dataclasses.replace(settings, **changes),
            transport=mock_api.transport(),
            clock=clock,
            sleep=sleeps,
        )
        built.append(new)
        return new

    yield factory
    for deps in built:
        await deps.aclose()


class TestGet:
    async def test_it_returns_the_parsed_json_and_what_it_cost(
        self, deps: Deps, mock_api: MockEODHD, clock: FixedClock
    ) -> None:
        mock_api.add(EOD, DATA)
        result = await fetch(deps)
        assert result.data == DATA
        assert (result.endpoint, result.status, result.cached, result.cost) == ("eod", 200, False, 1)
        assert result.fetched_at == clock()
        assert result.duration_ms >= 0

    async def test_it_sends_the_key_and_fmt_json(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, [])
        await fetch(deps, params={"from": "2026-10-01", "period": "d"})
        (request,) = mock_api.requests
        assert request.path == EOD
        assert request.params == {"from": "2026-10-01", "period": "d", "fmt": "json"}
        assert request.api_token == TEST_KEY

    async def test_the_caller_cannot_override_the_format_or_the_key(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        mock_api.add(EOD, [])
        await fetch(deps, params={"fmt": "csv", "api_token": "attacker-token-123456"})
        (request,) = mock_api.requests
        assert request.params == {"fmt": "json"}
        assert request.api_token == TEST_KEY

    async def test_parameter_values_are_normalised_before_sending(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        mock_api.add(EOD, [])
        await fetch(deps, params={"s": ["A.US", "B.US"], "flag": True, "skip": None, "n": 5})
        assert mock_api.requests[0].params == {"s": "A.US,B.US", "flag": "true", "n": "5", "fmt": "json"}

    @pytest.mark.parametrize(
        ("path", "encoded"),
        [
            ("search/M&M.NSE", "search/M%26M.NSE"),
            ("search/S&P 500", "search/S%26P%20500"),
            ("eod/^GSPC.INDX", "eod/%5EGSPC.INDX"),
            ("search/Nestlé", "search/Nestl%C3%A9"),
            ("search/a?b=c#d", "search/a%3Fb%3Dc%23d"),
            ("search/50%", "search/50%25"),
        ],
    )
    async def test_every_path_segment_is_percent_encoded(
        self, deps: Deps, mock_api: MockEODHD, path: str, encoded: str
    ) -> None:
        mock_api.add(path, [])
        await fetch(deps, path)
        assert mock_api.requests[0].raw_path.startswith(f"/api/{encoded}?")

    async def test_slashes_around_the_path_are_ignored(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, [])
        await fetch(deps, f"/{EOD}/")
        assert mock_api.requests[0].path == EOD

    @pytest.mark.parametrize(
        "path", ["", "/", "eod//ACME.US", "eod/./ACME.US", "eod/../user", "../user", ".."]
    )
    async def test_an_empty_or_dot_segment_is_refused_before_anything_is_sent(
        self, deps: Deps, mock_api: MockEODHD, path: str
    ) -> None:
        with pytest.raises(GuardError) as caught:
            await fetch(deps, path)
        assert caught.value.code is ErrorCode.INVALID_PARAMS
        assert mock_api.requests == []

    async def test_a_base_url_with_a_trailing_slash_works(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(base_url=MockEODHD.base_url + "/")
        mock_api.add(EOD, [])
        await fetch(deps)
        assert mock_api.requests[0].raw_path.startswith("/api/eod/ACME.US?")

    async def test_without_a_key_nothing_is_sent(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(api_key=None)
        with pytest.raises(GuardError) as get_error:
            await fetch(deps)
        with pytest.raises(GuardError) as account_error:
            await deps.client.get_account()
        assert get_error.value.code is account_error.value.code is ErrorCode.CONFIG_ERROR
        assert mock_api.requests == []

    async def test_the_call_is_recorded_in_the_ledger(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings
    ) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, cost=1, symbol="ACME.US")
        assert ledger_rows(settings) == [("eod", 1, 200, 0, "ACME.US")]
        assert deps.ledger.spent_today() == 1

    async def test_json_that_is_empty_or_null_is_still_a_success(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        for name, body in (("empty-list", "[]"), ("empty-dict", "{}"), ("null", "null")):
            mock_api.add(f"eod/{name}", text=body)
        assert (await fetch(deps, "eod/empty-list")).data == []
        assert (await fetch(deps, "eod/empty-dict")).data == {}
        assert (await fetch(deps, "eod/null")).data is None

    async def test_the_client_can_be_closed_more_than_once(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps)
        await deps.client.aclose()
        await deps.client.aclose()


class TestCache:
    async def test_the_second_identical_call_is_served_locally(
        self, deps: Deps, mock_api: MockEODHD, clock: FixedClock, settings: Settings
    ) -> None:
        mock_api.add(EOD, DATA)
        first = await fetch(deps)
        fetched = clock()
        clock.advance(minutes=5)
        second = await fetch(deps)
        assert second.cached is True
        assert second.cost == 0
        assert second.data == first.data
        assert second.fetched_at == fetched
        assert mock_api.count() == 1
        assert ledger_rows(settings) == [("eod", 1, 200, 0, "ACME.US"), ("eod", 0, 200, 1, "ACME.US")]
        today = deps.ledger.summary()["today"]
        assert (today["cost"], today["requests"], today["cache_hits"]) == (1, 1, 1)

    async def test_parameter_order_does_not_matter(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, params={"from": "2026-10-01", "to": "2026-11-01"})
        again = await fetch(deps, params={"to": "2026-11-01", "from": "2026-10-01"})
        assert again.cached is True
        assert mock_api.count() == 1

    async def test_different_parameters_are_different_questions(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, params={"from": "2026-10-01"})
        other = await fetch(deps, params={"from": "2026-10-15"})
        assert other.cached is False
        assert mock_api.count() == 2

    async def test_an_answer_expires_after_its_ttl(
        self, deps: Deps, mock_api: MockEODHD, clock: FixedClock
    ) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, ttl_s=60)
        clock.advance(seconds=59)
        assert (await fetch(deps, ttl_s=60)).cached is True
        clock.advance(seconds=2)
        assert (await fetch(deps, ttl_s=60)).cached is False
        assert mock_api.count() == 2

    async def test_a_zero_ttl_means_the_answer_is_not_kept(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, ttl_s=0)
        await fetch(deps, ttl_s=0)
        assert mock_api.count() == 2

    async def test_with_the_cache_off_every_call_goes_to_the_network(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD, settings: Settings
    ) -> None:
        deps = make_deps(cache_enabled=False)
        mock_api.add(EOD, DATA)
        await fetch(deps)
        assert (await fetch(deps)).cached is False
        assert mock_api.count() == 2
        assert not (settings.state_dir / "cache.sqlite").exists()

    async def test_the_key_used_for_the_cache_does_not_depend_on_the_api_token(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, params={"api_token": "someone-elses-key-123456"})
        assert (await fetch(deps)).cached is True

    async def test_a_cached_body_that_is_not_json_is_a_miss(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings, clock: FixedClock
    ) -> None:
        mock_api.add(EOD, DATA)
        key = ResponseCache.make_key(EOD, {})
        deps.cache.set(key, endpoint="eod", status=200, body=b"<html>", ttl_s=60)
        result = await fetch(deps)
        assert result.cached is False
        assert result.data == DATA

    async def test_a_cached_entry_with_an_unexpected_status_is_a_miss(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings, clock: FixedClock
    ) -> None:
        mock_api.add(EOD, DATA)
        key = ResponseCache.make_key(EOD, {})
        with sqlite3.connect(settings.state_dir / "cache.sqlite") as conn:
            conn.execute(
                "INSERT INTO responses (key, endpoint, created, expires, status, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, "eod", clock().timestamp(), clock().timestamp() + 60, 500, zlib.compress(b"[]")),
            )
        assert (await fetch(deps)).cached is False


class TestNotFound:
    async def test_the_upstream_text_is_in_the_message(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, status=404, text="Ticker Not Found.")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert caught.value.message == "EODHD answered HTTP 404: Ticker Not Found."
        assert caught.value.details == {"endpoint": "eod", "status": 404, "upstream": "Ticker Not Found."}

    async def test_the_text_is_cut_to_200_characters_on_one_line(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        mock_api.add(EOD, status=404, text="Exchange\n  Not   Found\n" + "x" * 500)
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        upstream = caught.value.details["upstream"]
        assert len(upstream) == 200
        assert upstream.startswith("Exchange Not Found xxx")

    async def test_an_empty_body_still_gives_a_message(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, status=404)
        with pytest.raises(GuardError, match="404 with no text"):
            await fetch(deps)

    async def test_a_key_in_the_text_is_masked_before_the_cut(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, status=404, text="x" * 195 + TEST_KEY + "tail")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.details["upstream"] == "x" * 195 + "***ta"
        assert "test-k" not in caught.value.message

    async def test_a_404_is_cached_and_raises_again_at_no_cost(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings
    ) -> None:
        mock_api.add(EOD, status=404, text="Ticker Not Found.")
        with pytest.raises(GuardError) as first:
            await fetch(deps)
        with pytest.raises(GuardError) as second:
            await fetch(deps)
        assert second.value.code is ErrorCode.NOT_FOUND
        assert second.value.message == first.value.message
        assert mock_api.count() == 1
        assert ledger_rows(settings) == [("eod", 0, 404, 0, "ACME.US"), ("eod", 0, 404, 1, "ACME.US")]

    async def test_a_cached_404_is_forgotten_after_a_day(
        self, deps: Deps, mock_api: MockEODHD, clock: FixedClock
    ) -> None:
        mock_api.add(EOD, status=404, text="Ticker Not Found.", times=1)
        mock_api.add(EOD, DATA)
        with pytest.raises(GuardError):
            await fetch(deps)
        clock.advance(hours=23)
        with pytest.raises(GuardError):
            await fetch(deps)
        assert mock_api.count() == 1
        clock.advance(hours=2)
        assert (await fetch(deps)).data == DATA

    async def test_a_404_is_not_billed(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, status=404, text="Ticker Not Found.")
        with pytest.raises(GuardError):
            await fetch(deps, cost=10)
        assert deps.ledger.spent_today() == 0


class TestBudget:
    async def test_a_call_over_budget_is_refused_before_anything_is_sent(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD, settings: Settings
    ) -> None:
        deps = make_deps(daily_budget=3)
        with pytest.raises(GuardError) as caught:
            await fetch(deps, cost=5)
        assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
        assert mock_api.requests == []
        assert ledger_rows(settings) == []

    async def test_the_message_says_how_to_fix_it(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(daily_budget=3)
        with pytest.raises(GuardError) as caught:
            await fetch(deps, cost=5)
        message = caught.value.message
        assert "daily budget of 3 API calls" in message
        assert "0 spent today" in message
        assert "EODHD_GUARD_DAILY_BUDGET" in message
        assert "2026-11-04T00:00:00Z" in message

    async def test_the_budget_can_be_spent_to_the_last_unit(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(daily_budget=5)
        mock_api.add("eod/A.US", [])
        mock_api.add("eod/B.US", [])
        await fetch(deps, "eod/A.US", cost=5)
        with pytest.raises(GuardError) as caught:
            await fetch(deps, "eod/B.US", cost=1)
        assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
        assert "5 spent today" in caught.value.message

    async def test_cache_hits_are_free_even_when_the_budget_is_used_up(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(daily_budget=1)
        mock_api.add(EOD, DATA)
        await fetch(deps)
        again = await fetch(deps)
        assert again.cached is True
        assert deps.budget.status()["spent_today"] == 1

    async def test_the_budget_starts_again_at_midnight_gmt(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD, clock: FixedClock
    ) -> None:
        deps = make_deps(daily_budget=1)
        mock_api.add("eod/A.US", [])
        mock_api.add("eod/B.US", [])
        clock.advance(hours=8, minutes=59)  # 23:59 GMT
        await fetch(deps, "eod/A.US")
        with pytest.raises(GuardError, match="BUDGET_EXCEEDED"):
            await fetch(deps, "eod/B.US")
        clock.advance(minutes=1)  # 00:00 GMT
        assert (await fetch(deps, "eod/B.US")).cost == 1
        assert deps.budget.status()["spent_today"] == 1

    async def test_failed_calls_do_not_use_up_the_budget(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(daily_budget=1)
        mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
        mock_api.add("eod/DOWN.US", status=500)
        mock_api.add("eod/OK.US", [])
        with pytest.raises(GuardError):
            await fetch(deps, "eod/GONE.US")
        with pytest.raises(GuardError):
            await fetch(deps, "eod/DOWN.US")
        assert (await fetch(deps, "eod/OK.US")).cost == 1

    async def test_a_free_call_is_allowed_when_the_budget_is_used_up(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(daily_budget=1)
        mock_api.add("eod/A.US", [])
        mock_api.add("exchanges-list", [])
        await fetch(deps, "eod/A.US")
        result = await fetch(deps, "exchanges-list", endpoint="exchanges-list", cost=0)
        assert result.cost == 0

    async def test_no_limit_means_no_check(self, make_deps: Callable[..., Deps], mock_api: MockEODHD) -> None:
        deps = make_deps(daily_budget=0)
        mock_api.add(EOD, [])
        assert (await fetch(deps, cost=10**6)).cost == 10**6

    async def test_calls_in_flight_count_against_the_budget(
        self, settings: Settings, clock: FixedClock, sleeps: RecordingSleep
    ) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(request: httpx2.Request) -> httpx2.Response:
            started.set()
            await gate.wait()
            return httpx2.Response(200, json=[])

        deps = build_deps(
            dataclasses.replace(settings, daily_budget=1),
            transport=httpx2.MockTransport(slow),
            clock=clock,
            sleep=sleeps,
        )
        try:
            first = asyncio.create_task(fetch(deps, "eod/A.US"))
            await started.wait()
            with pytest.raises(GuardError) as caught:
                await fetch(deps, "eod/B.US")
            assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
            assert "in flight" in caught.value.message
            gate.set()
            assert (await first).cost == 1
            assert deps.budget.status()["spent_today"] == 1
        finally:
            await deps.aclose()

    async def test_a_failed_call_gives_its_reservation_back(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(daily_budget=1, max_retries=0)
        mock_api.add("eod/DOWN.US", raises=httpx2.ConnectError("down"))
        mock_api.add("eod/OK.US", [])
        with pytest.raises(GuardError, match="NETWORK_ERROR"):
            await fetch(deps, "eod/DOWN.US")
        assert (await fetch(deps, "eod/OK.US")).cost == 1


class TestRetries:
    async def test_a_429_is_retried_with_backoff(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, settings: Settings
    ) -> None:
        mock_api.add(EOD, status=429, text="Too Many Requests", times=2)
        mock_api.add(EOD, DATA)
        result = await fetch(deps)
        assert result.data == DATA
        assert result.cost == 1
        assert mock_api.count(EOD) == 3
        assert sleeps.delays == [0.5, 1.0]
        assert [row[1:3] for row in ledger_rows(settings)] == [(0, 429), (0, 429), (1, 200)]

    async def test_a_429_that_never_stops_is_rate_limited(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, settings: Settings
    ) -> None:
        mock_api.add(EOD, status=429, text="Too Many Requests")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is ErrorCode.RATE_LIMITED
        assert "3 attempt(s)" in caught.value.message
        assert mock_api.count(EOD) == 3
        assert sleeps.delays == [0.5, 1.0]
        assert deps.ledger.spent_today() == 0
        assert [row[1:3] for row in ledger_rows(settings)] == [(0, 429)] * 3

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    async def test_a_5xx_that_never_stops_is_an_upstream_error(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, status: int
    ) -> None:
        mock_api.add(EOD, status=status, text="<html>Bad gateway</html>")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is ErrorCode.UPSTREAM_ERROR
        assert f"HTTP {status} after 3 attempt(s)" in caught.value.message
        assert "Bad gateway" not in str(caught.value)
        assert mock_api.count(EOD) == 3
        assert len(sleeps.delays) == 2

    async def test_a_5xx_followed_by_success(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep
    ) -> None:
        mock_api.add(EOD, status=503, times=1)
        mock_api.add(EOD, DATA)
        assert (await fetch(deps)).data == DATA
        assert sleeps.delays == [0.5]

    @pytest.mark.parametrize(
        "error",
        [
            httpx2.ConnectError("refused"),
            httpx2.ReadTimeout("slow"),
            httpx2.RemoteProtocolError("garbled"),
            httpx2.ProxyError("proxy"),
            httpx2.InvalidURL("odd"),
            httpx2.StreamClosed(),
        ],
        ids=lambda error: type(error).__name__,
    )
    async def test_a_network_error_that_never_stops(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, settings: Settings, error: Exception
    ) -> None:
        mock_api.add(EOD, raises=error)
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is ErrorCode.NETWORK_ERROR
        assert f"after 3 attempt(s): {type(error).__name__}." in caught.value.message
        assert caught.value.details["error"] == type(error).__name__
        assert mock_api.count(EOD) == 3
        assert sleeps.delays == [0.5, 1.0]
        assert ledger_rows(settings) == []

    async def test_a_network_error_followed_by_success(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, settings: Settings
    ) -> None:
        mock_api.add(EOD, raises=httpx2.ConnectTimeout("slow"), times=1)
        mock_api.add(EOD, DATA)
        assert (await fetch(deps)).data == DATA
        assert sleeps.delays == [0.5]
        assert ledger_rows(settings) == [("eod", 1, 200, 0, "ACME.US")]

    async def test_the_last_try_decides_the_error(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, status=429, times=2)
        mock_api.add(EOD, raises=httpx2.ReadTimeout("slow"))
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is ErrorCode.NETWORK_ERROR
        assert "ReadTimeout" in caught.value.message

    async def test_no_retries_means_one_attempt(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD, sleeps: RecordingSleep
    ) -> None:
        deps = make_deps(max_retries=0)
        mock_api.add(EOD, status=500)
        with pytest.raises(GuardError, match="after 1 attempt"):
            await fetch(deps)
        assert mock_api.count(EOD) == 1
        assert sleeps.delays == []

    async def test_a_negative_retry_count_still_makes_one_attempt(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(max_retries=-3)
        mock_api.add(EOD, DATA)
        assert (await fetch(deps)).data == DATA

    async def test_the_backoff_doubles(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD, sleeps: RecordingSleep
    ) -> None:
        deps = make_deps(max_retries=5)
        mock_api.add(EOD, status=502)
        with pytest.raises(GuardError):
            await fetch(deps)
        assert mock_api.count(EOD) == 6
        assert sleeps.delays == [0.5, 1.0, 2.0, 4.0, 8.0]

    @pytest.mark.parametrize(
        ("header", "first_delay"),
        [
            ("2", 2.0),
            ("1.5", 1.5),
            ("120", 5.0),
            ("inf", 5.0),
            ("0", 0.5),
            ("-4", 0.5),
            ("0.1", 0.5),
            ("soon", 0.5),
            ("nan", 0.5),
            ("", 0.5),
            ("Tue, 03 Nov 2026 15:00:03 GMT", 3.0),
            ("Tue, 03 Nov 2026 14:00:00 GMT", 0.5),
        ],
    )
    async def test_retry_after_is_honoured_up_to_five_seconds(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, header: str, first_delay: float
    ) -> None:
        mock_api.add(EOD, status=429, headers={"Retry-After": header}, times=1)
        mock_api.add(EOD, DATA)
        await fetch(deps)
        assert sleeps.delays == [first_delay]

    async def test_retry_after_also_applies_to_a_503(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep
    ) -> None:
        mock_api.add(EOD, status=503, headers={"Retry-After": "3"}, times=1)
        mock_api.add(EOD, DATA)
        await fetch(deps)
        assert sleeps.delays == [3.0]

    async def test_retry_after_never_shortens_the_backoff(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep
    ) -> None:
        mock_api.add(EOD, status=429, headers={"Retry-After": "0.2"}, times=2)
        mock_api.add(EOD, DATA)
        await fetch(deps)
        assert sleeps.delays == [0.5, 1.0]

    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 410, 418, 302])
    async def test_other_statuses_are_not_retried(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep, status: int
    ) -> None:
        mock_api.add(EOD, status=status, text="no")
        with pytest.raises(GuardError):
            await fetch(deps)
        assert mock_api.count(EOD) == 1
        assert sleeps.delays == []

    async def test_a_real_sleep_is_used_when_none_is_injected(
        self, settings: Settings, mock_api: MockEODHD, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waited: list[float] = []

        real_sleep = asyncio.sleep

        async def fast_sleep(seconds: float, *args: object) -> None:
            waited.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        deps = build_deps(settings, transport=mock_api.transport(), clock=clock)
        mock_api.add(EOD, status=500, times=1)
        mock_api.add(EOD, DATA)
        try:
            await fetch(deps)
        finally:
            await deps.aclose()
        assert waited == [0.5]

    async def test_a_mistake_in_the_test_setup_is_not_swallowed(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        with pytest.raises(AssertionError, match="is not mocked"):
            await fetch(deps, "eod/UNMOCKED.US")

    async def test_a_used_up_route_fails_loudly(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, DATA, times=1)
        await fetch(deps, ttl_s=0)
        with pytest.raises(AssertionError, match="has no uses left"):
            await fetch(deps, ttl_s=0)


class TestRetryHelpers:
    NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", None),
            ("  ", None),
            ("3", 3.0),
            (" 2.5 ", 2.5),
            ("-1", 0.0),
            ("inf", float("inf")),
            ("nan", None),
            ("never", None),
            ("Tue, 03 Nov 2026 15:00:10 GMT", 10.0),
            ("Tue, 03 Nov 2026 14:59:00 GMT", 0.0),
            ("Tue, 03 Nov 2026 15:00:10 -0000", 10.0),
        ],
    )
    def test_retry_after_seconds(self, value: str | None, expected: float | None) -> None:
        assert _retry_after_seconds(value, self.NOW) == expected

    @pytest.mark.parametrize(
        ("attempt", "header", "expected"),
        [(1, None, 0.5), (2, None, 1.0), (3, None, 2.0), (1, "4", 4.0), (3, "1", 2.0), (1, "99", 5.0)],
    )
    def test_retry_delay(self, attempt: int, header: str | None, expected: float) -> None:
        assert _retry_delay(attempt, header, self.NOW) == expected


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "code", "fragments"),
        [
            (401, ErrorCode.AUTH_ERROR, ["HTTP 401", "missing, wrong or expired"]),
            (
                402,
                ErrorCode.QUOTA_EXCEEDED,
                ["HTTP 402", "daily API limit", "00:00 GMT", "2026-11-04T00:00:00Z"],
            ),
            (403, ErrorCode.PLAN_ERROR, ["HTTP 403", "'eod' endpoint", "All-in-One"]),
            (400, ErrorCode.UPSTREAM_ERROR, ["unexpected HTTP 400: Wrong parameters"]),
            (410, ErrorCode.UPSTREAM_ERROR, ["unexpected HTTP 410: Wrong parameters"]),
            (302, ErrorCode.UPSTREAM_ERROR, ["unexpected HTTP 302."]),
            (204, ErrorCode.UPSTREAM_ERROR, ["unexpected HTTP 204."]),
        ],
    )
    async def test_each_status_maps_to_its_error(
        self, deps: Deps, mock_api: MockEODHD, status: int, code: ErrorCode, fragments: list[str]
    ) -> None:
        mock_api.add(EOD, status=status, text="Wrong parameters" if 400 <= status < 500 else "")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is code
        assert caught.value.details["status"] == status
        assert caught.value.details["endpoint"] == "eod"
        for fragment in fragments:
            assert fragment in caught.value.message

    @pytest.mark.parametrize(
        ("status", "tries"), [(401, 1), (402, 1), (403, 1), (400, 1), (429, 3), (500, 3)]
    )
    async def test_errors_are_not_billed_and_not_cached(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings, status: int, tries: int
    ) -> None:
        mock_api.add(EOD, status=status, text="no", times=tries)
        mock_api.add(EOD, DATA)
        with pytest.raises(GuardError):
            await fetch(deps, cost=10)
        assert deps.ledger.spent_today() == 0
        assert all(row[1] == 0 for row in ledger_rows(settings))
        assert (await fetch(deps, cost=10)).cached is False
        assert deps.ledger.spent_today() == 10

    @pytest.mark.parametrize(
        ("endpoint", "plans"),
        [
            ("intraday", "EOD+Intraday or All-in-One"),
            ("fundamentals", "Fundamentals Data Feed or All-in-One"),
            ("calendar/earnings", "A higher plan, such as All-in-One, may include it"),
        ],
    )
    async def test_a_403_names_the_plans_that_include_the_endpoint(
        self, deps: Deps, mock_api: MockEODHD, endpoint: str, plans: str
    ) -> None:
        mock_api.add(EOD, status=403, text="Forbidden")
        with pytest.raises(GuardError) as caught:
            await fetch(deps, endpoint=endpoint)
        assert f"'{endpoint}' endpoint" in caught.value.message
        assert plans in caught.value.message
        assert caught.value.code is ErrorCode.PLAN_ERROR

    @pytest.mark.parametrize("body", ["<html>maintenance</html>", "", "not json", "{broken"])
    async def test_a_200_that_is_not_json_is_a_bad_response_and_is_billed(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings, body: str
    ) -> None:
        mock_api.add(EOD, status=200, text=body)
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert caught.value.code is ErrorCode.BAD_RESPONSE
        assert "not valid JSON" in caught.value.message
        assert caught.value.details == {"endpoint": "eod", "bytes": len(body)}
        assert body not in str(caught.value) or not body
        assert deps.ledger.spent_today() == 1
        assert ledger_rows(settings) == [("eod", 1, 200, 0, "ACME.US")]

    async def test_a_bad_response_is_not_cached(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, text="oops", times=1)
        mock_api.add(EOD, DATA)
        with pytest.raises(GuardError):
            await fetch(deps)
        assert (await fetch(deps)).data == DATA
        assert mock_api.count() == 2


class TestKeyHygiene:
    @pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 500])
    async def test_an_http_error_never_carries_the_key(
        self, deps: Deps, mock_api: MockEODHD, status: int
    ) -> None:
        mock_api.add(EOD, status=status, text=f"upstream said {TEST_KEY} at {SECRET_URL}")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert_no_key(caught.value)

    @pytest.mark.parametrize(
        "make_error",
        [
            lambda: httpx2.ConnectError(f"connect failed: {SECRET_URL}"),
            lambda: httpx2.ReadTimeout(f"timed out reading {SECRET_URL}"),
            lambda: httpx2.RemoteProtocolError(f"bad response from {SECRET_URL}"),
            lambda: httpx2.InvalidURL(f"invalid URL {SECRET_URL}"),
            lambda: httpx2.StreamClosed(),
        ],
        ids=["connect", "timeout", "protocol", "invalid-url", "stream"],
    )
    async def test_a_network_error_reports_only_its_class_name(
        self, deps: Deps, mock_api: MockEODHD, make_error: Callable[[], Exception]
    ) -> None:
        error = make_error()
        mock_api.add(EOD, raises=error)
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert type(error).__name__ in caught.value.message
        assert "eodhd.test" not in str(caught.value)
        assert_no_key(caught.value)

    async def test_a_key_echoed_in_a_body_reaches_neither_the_caller_nor_the_cache(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings
    ) -> None:
        mock_api.add(EOD, {"echo": f"https://eodhd.com/api/eod?api_token={TEST_KEY}", "rows": []})
        fresh = await fetch(deps)
        cached = await fetch(deps)
        assert fresh.data == cached.data == {"echo": "https://eodhd.com/api/eod?api_token=***", "rows": []}
        with sqlite3.connect(settings.state_dir / "cache.sqlite") as conn:
            bodies = [zlib.decompress(row[0]) for row in conn.execute("SELECT body FROM responses")]
        assert bodies
        assert all(TEST_KEY.encode() not in body for body in bodies)

    async def test_a_short_key_is_not_masked_in_bodies(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD
    ) -> None:
        deps = make_deps(api_key="demo")
        mock_api.add(EOD, [{"name": "Demo Holdings", "tag": "demo"}])
        assert (await fetch(deps)).data == [{"name": "Demo Holdings", "tag": "demo"}]

    async def test_a_bad_response_never_carries_the_key(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, text=f"<html>{TEST_KEY}</html>")
        with pytest.raises(GuardError) as caught:
            await fetch(deps)
        assert_no_key(caught.value)

    async def test_a_budget_error_never_carries_the_key(self, make_deps: Callable[..., Deps]) -> None:
        deps = make_deps(daily_budget=1)
        with pytest.raises(GuardError) as caught:
            await fetch(deps, cost=2)
        assert_no_key(caught.value)

    async def test_the_settings_and_deps_do_not_show_the_key(self, deps: Deps) -> None:
        assert TEST_KEY not in repr(deps.settings)
        assert TEST_KEY not in str(deps.settings)
        assert TEST_KEY not in repr(deps)

    async def test_a_failure_never_leaves_the_key_in_the_log(
        self, deps: Deps, mock_api: MockEODHD, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        logging.getLogger("httpx2").setLevel(logging.WARNING)
        mock_api.add("eod/RATE.US", status=429, text=TEST_KEY, times=1)
        mock_api.add("eod/RATE.US", [])
        mock_api.add("eod/DOWN.US", raises=httpx2.ConnectError(SECRET_URL), times=1)
        mock_api.add("eod/DOWN.US", [])
        mock_api.add("eod/GONE.US", status=404, text=f"{TEST_KEY}")
        await fetch(deps, "eod/RATE.US")
        await fetch(deps, "eod/DOWN.US")
        with pytest.raises(GuardError):
            await fetch(deps, "eod/GONE.US")
        assert "retry 1 of 2" in caplog.text
        assert TEST_KEY not in caplog.text
        assert "api_token" not in caplog.text

    @pytest.mark.usefixtures("isolated_logging")
    async def test_after_install_the_debug_log_is_clean_even_with_the_httpx2_url_line_on(
        self, deps: Deps, mock_api: MockEODHD, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_log_redaction(TEST_KEY)
        caplog.set_level(logging.DEBUG)
        logging.getLogger("httpx2").setLevel(logging.INFO)
        logging.getLogger("httpcore2").setLevel(logging.DEBUG)
        mock_api.add("eod/OK.US", DATA)
        mock_api.add("eod/RATE.US", status=429, times=1)
        mock_api.add("eod/RATE.US", DATA)
        mock_api.add("eod/DOWN.US", raises=httpx2.ConnectError(SECRET_URL))
        await fetch(deps, "eod/OK.US")
        await fetch(deps, "eod/RATE.US")
        with pytest.raises(GuardError):
            await fetch(deps, "eod/DOWN.US")
        assert "HTTP Request: GET" in caplog.text
        assert "api_token=***" in caplog.text
        assert TEST_KEY not in caplog.text
        assert all(TEST_KEY not in record.getMessage() for record in caplog.records)

    async def test_the_state_files_never_hold_the_key(
        self, deps: Deps, mock_api: MockEODHD, settings: Settings
    ) -> None:
        mock_api.add(EOD, DATA)
        mock_api.add("eod/GONE.US", status=404, text="Ticker Not Found.")
        mock_api.add("eod/DOWN.US", status=500, text=SECRET_URL)
        mock_api.add("user", load_fixture("core_account"))
        await fetch(deps)
        await fetch(deps)
        with pytest.raises(GuardError):
            await fetch(deps, "eod/GONE.US")
        with pytest.raises(GuardError):
            await fetch(deps, "eod/DOWN.US")
        await deps.client.get_account()

        files = sorted(settings.state_dir.iterdir())
        assert {"usage.sqlite", "cache.sqlite"} <= {path.name for path in files}
        assert all(TEST_KEY.encode() not in path.read_bytes() for path in files)
        with sqlite3.connect(settings.state_dir / "cache.sqlite") as conn:
            bodies = [zlib.decompress(row[0]) for row in conn.execute("SELECT body FROM responses")]
        assert bodies
        assert all(TEST_KEY.encode() not in body for body in bodies)
        with sqlite3.connect(settings.state_dir / "cache.sqlite") as conn:
            keys = [row[0] for row in conn.execute("SELECT key FROM responses")]
        assert all(len(key) == 64 for key in keys)


class TestAccount:
    async def test_only_the_six_counters_are_returned(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add("user", load_fixture("core_account"))
        account = await deps.client.get_account()
        assert account == {
            "apiRequests": 37,
            "apiRequestsDate": "2026-11-03",
            "dailyRateLimit": 5000,
            "extraLimit": 0,
            "subscriptionType": "synthetic-plan",
            "subscriptionMode": "synthetic",
        }
        assert tuple(account) == ACCOUNT_FIELDS

    async def test_personal_data_never_survives(self, deps: Deps, mock_api: MockEODHD) -> None:
        body = load_fixture("core_account")
        mock_api.add("user", body)
        dumped = json.dumps(await deps.client.get_account())
        for private in ("name", "email", "paymentMethod", "inviteToken", "inviteTokenClicked"):
            assert private not in dumped
        for value in (body["name"], body["email"], body["paymentMethod"], body["inviteToken"]):
            assert value not in dumped
        assert TEST_KEY not in dumped

    async def test_the_request_asks_for_json_with_the_key(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add("user", load_fixture("core_account"))
        await deps.client.get_account()
        (request,) = mock_api.requests
        assert request.path == "user"
        assert request.params == {"fmt": "json"}
        assert request.api_token == TEST_KEY

    async def test_a_missing_field_is_none_and_a_non_scalar_is_dropped(
        self, deps: Deps, mock_api: MockEODHD
    ) -> None:
        mock_api.add(
            "user", {"apiRequests": 5, "dailyRateLimit": {"nested": 1}, "email": "x@example.invalid"}
        )
        account = await deps.client.get_account()
        assert account["apiRequests"] == 5
        assert account["dailyRateLimit"] is None
        assert account["subscriptionType"] is None
        assert set(account) == set(ACCOUNT_FIELDS)

    async def test_a_value_that_contains_the_key_is_masked(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add("user", {"subscriptionType": f"plan {TEST_KEY}", "extraLimit": 2.5, "apiRequests": True})
        account = await deps.client.get_account()
        assert account["subscriptionType"] == "plan ***"
        assert account["extraLimit"] == 2.5
        assert account["apiRequests"] is True

    @pytest.mark.parametrize("body", ["[]", '"text"', "7", "not json", ""])
    async def test_an_answer_that_is_not_an_object_is_a_bad_response(
        self, deps: Deps, mock_api: MockEODHD, body: str
    ) -> None:
        mock_api.add("user", text=body)
        with pytest.raises(GuardError) as caught:
            await deps.client.get_account()
        assert caught.value.code is ErrorCode.BAD_RESPONSE

    async def test_an_unknown_account_endpoint_is_not_found(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add("user", status=404, text="Not Found")
        with pytest.raises(GuardError) as caught:
            await deps.client.get_account()
        assert caught.value.code is ErrorCode.NOT_FOUND
        assert caught.value.details["endpoint"] == "user"

    async def test_a_rejected_key_is_an_auth_error(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add("user", status=401, text="Unauthenticated")
        with pytest.raises(GuardError) as caught:
            await deps.client.get_account()
        assert caught.value.code is ErrorCode.AUTH_ERROR
        assert_no_key(caught.value)

    async def test_it_is_free_never_cached_and_never_budget_checked(
        self, make_deps: Callable[..., Deps], mock_api: MockEODHD, settings: Settings
    ) -> None:
        deps = make_deps(daily_budget=1)
        mock_api.add("eod/A.US", [])
        mock_api.add("user", load_fixture("core_account"))
        await fetch(deps, "eod/A.US")
        await deps.client.get_account()
        await deps.client.get_account()
        assert mock_api.count("user") == 2
        assert deps.ledger.spent_today() == 1
        assert ledger_rows(settings)[1:] == [("user", 0, 200, 0, None)] * 2

    async def test_it_retries_like_any_other_call(
        self, deps: Deps, mock_api: MockEODHD, sleeps: RecordingSleep
    ) -> None:
        mock_api.add("user", status=503, times=1)
        mock_api.add("user", load_fixture("core_account"))
        assert (await deps.client.get_account())["apiRequests"] == 37
        assert sleeps.delays == [0.5]


class TestBuildDeps:
    async def test_it_wires_the_pieces_together(
        self, deps: Deps, settings: Settings, clock: FixedClock
    ) -> None:
        assert deps.settings is settings
        assert deps.clock is clock
        assert (settings.state_dir / "usage.sqlite").exists()
        assert (settings.state_dir / "cache.sqlite").exists()
        assert deps.budget.status()["daily_limit"] == 1000
        assert isinstance(deps.ledger, UsageLedger)
        assert isinstance(deps.cache, ResponseCache)

    async def test_the_budget_and_the_client_share_the_ledger(self, deps: Deps, mock_api: MockEODHD) -> None:
        mock_api.add(EOD, DATA)
        await fetch(deps, cost=4)
        assert deps.ledger.spent_today() == 4
        assert deps.budget.status()["spent_today"] == 4

    def test_the_default_clock_is_utc_now(self, settings: Settings) -> None:
        deps = build_deps(settings)
        try:
            now = deps.clock()
            assert now.utcoffset() == timedelta(0)
            assert abs(now - datetime.now(timezone.utc)) < timedelta(seconds=5)
        finally:
            deps.cache.close()
            deps.ledger.close()

    def test_with_the_cache_off_no_cache_file_is_made(self, settings: Settings) -> None:
        deps = build_deps(dataclasses.replace(settings, cache_enabled=False))
        deps.ledger.close()
        assert not (settings.state_dir / "cache.sqlite").exists()

    async def test_aclose_closes_everything_and_can_be_repeated(self, deps: Deps) -> None:
        await deps.aclose()
        assert deps.cache.get("anything") is None
        with pytest.raises(GuardError, match="cannot be read"):
            deps.ledger.spent_today()
        await deps.aclose()

    def test_a_cache_that_cannot_open_closes_the_ledger_again(
        self, settings: Settings, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []
        real_close = UsageLedger.close

        def spy(self: UsageLedger) -> None:
            closed.append(True)
            real_close(self)

        monkeypatch.setattr(UsageLedger, "close", spy)
        (settings.state_dir / "cache.sqlite").mkdir(parents=True)
        with pytest.raises(GuardError, match="response cache"):
            build_deps(settings, clock=clock)
        assert closed == [True]

    def test_a_ledger_that_cannot_open_is_a_config_error(self, settings: Settings, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        with pytest.raises(GuardError, match="usage ledger") as caught:
            build_deps(dataclasses.replace(settings, state_dir=blocker / "state"))
        assert caught.value.code is ErrorCode.CONFIG_ERROR
