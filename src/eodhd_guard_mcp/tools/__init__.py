"""Tool registration, and the decorator every tool goes through.

Each tools module exposes ``register(server, deps)``. ``guarded`` turns an anticipated ``GuardError``
into an MCP tool error the model can read, with the key redacted, and adds the budget warning to
successful results.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ParamSpec

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from eodhd_guard_mcp.envelope import ToolResponse, ToolWarning, WarningCode
from eodhd_guard_mcp.errors import GuardError
from eodhd_guard_mcp.redact import redact

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.server import Deps

P = ParamSpec("P")

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
"""Every tool only reads from EODHD."""

BUDGET_LOW_SHARE = 0.8


def guarded(
    deps: Deps,
) -> Callable[[Callable[P, Awaitable[ToolResponse]]], Callable[P, Awaitable[ToolResponse]]]:
    """Wrap a tool so a GuardError reaches the model as a readable error, never as a crash."""

    def decorate(fn: Callable[P, Awaitable[ToolResponse]]) -> Callable[P, Awaitable[ToolResponse]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResponse:
            try:
                result = await fn(*args, **kwargs)
            except GuardError as exc:
                raise ToolError(redact(str(exc), [deps.settings.api_key])) from None
            budget_warning = budget_low_warning(deps)
            if budget_warning is not None:
                result.warnings.append(budget_warning)
            return result

        return wrapper

    return decorate


def budget_low_warning(deps: Deps) -> ToolWarning | None:
    """BUDGET_LOW once 80% of the daily budget is spent; None without a budget."""
    status = deps.budget.status()
    limit = int(status.get("daily_limit") or 0)
    spent = int(status.get("spent_today") or 0)
    if limit <= 0 or spent < BUDGET_LOW_SHARE * limit:
        return None
    return ToolWarning.of(
        WarningCode.BUDGET_LOW,
        f"{spent} of this server's {limit}-call daily budget is spent; it resets at "
        f"{status.get('resets_at')}. Raise EODHD_GUARD_DAILY_BUDGET to allow more.",
        spent_today=spent,
        daily_limit=limit,
    )


def register_all(server: MCPServer, deps: Deps) -> None:
    """Register every tool on the server."""
    from eodhd_guard_mcp.tools import calendar, fundamentals, news, prices, quotes, reference, usage

    for module in (prices, quotes, fundamentals, calendar, news, reference, usage):
        module.register(server, deps)
