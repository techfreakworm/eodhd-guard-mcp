"""The guarded decorator and the budget warning every tool result can carry."""

from __future__ import annotations

from dataclasses import replace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from eodhd_guard_mcp.envelope import ToolResponse, build_response
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.ledger import Budget
from eodhd_guard_mcp.server import Deps
from eodhd_guard_mcp.tools import READ_ONLY, budget_low_warning, guarded
from tests.helpers import TEST_KEY

pytestmark = pytest.mark.anyio


def spend(deps: Deps, cost: int) -> None:
    deps.ledger.record(endpoint="eod", cost=cost, status=200, cached=False, duration_ms=1.0, bytes_=10)


async def test_a_guard_error_reaches_the_model_as_a_coded_tool_error_with_the_key_masked(deps: Deps) -> None:
    @guarded(deps)
    async def tool() -> ToolResponse:
        raise GuardError(ErrorCode.NOT_FOUND, f"Ticker Not Found for token {TEST_KEY}.", hint="Search first.")

    with pytest.raises(ToolError) as caught:
        await tool()
    text = str(caught.value)
    assert text.startswith("NOT_FOUND: ")
    assert "Hint: Search first." in text
    assert TEST_KEY not in text
    assert caught.value.__cause__ is None


async def test_other_exceptions_are_not_disguised_as_tool_errors(deps: Deps) -> None:
    @guarded(deps)
    async def tool() -> ToolResponse:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await tool()


async def test_results_gain_a_budget_warning_from_80_percent_of_the_budget(deps: Deps) -> None:
    @guarded(deps)
    async def tool() -> ToolResponse:
        return build_response({"ok": True}, [])

    assert (await tool()).warnings == []
    spend(deps, 799)
    assert (await tool()).warnings == []
    spend(deps, 1)
    warnings = (await tool()).warnings
    assert [w.code for w in warnings] == ["BUDGET_LOW"]
    assert "800 of this server's 1000-call daily budget" in warnings[0].message
    assert "EODHD_GUARD_DAILY_BUDGET" in warnings[0].message


async def test_an_unlimited_budget_never_warns(deps: Deps) -> None:
    unlimited = replace(deps, budget=Budget(deps.ledger, 0, clock=deps.clock))
    spend(unlimited, 10_000)
    assert budget_low_warning(unlimited) is None


def test_every_tool_is_annotated_read_only() -> None:
    assert READ_ONLY.read_only_hint is True
    assert READ_ONLY.destructive_hint is False
    assert READ_ONLY.open_world_hint is True
