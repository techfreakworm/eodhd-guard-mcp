"""Tool: get_usage, what this server has spent and what EODHD says the account has used.

The ledger and the budget are local, so the tool works without a key. The account counters come from
EODHD's free ``user`` endpoint; the answer there also holds the account holder's name, email, payment
method and invite token, and none of it is read here: the six counters are picked out by name.
"""

from __future__ import annotations

from datetime import timezone
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from eodhd_guard_mcp.client import ApiResponse
from eodhd_guard_mcp.costs import COSTS
from eodhd_guard_mcp.envelope import ToolResponse, build_response
from eodhd_guard_mcp.errors import GuardError
from eodhd_guard_mcp.redact import redact
from eodhd_guard_mcp.tools import READ_ONLY, guarded
from eodhd_guard_mcp.validation import iso_date_or_none

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.server import Deps

GET_USAGE_DESCRIPTION = (
    "Show what this server has spent: the local call ledger (per day, and today per endpoint), the daily "
    "budget, EODHD's own account counters and the price of each endpoint. Costs 0 API calls. EODHD and "
    "this ledger both count per GMT day, but the account counter includes other apps that share the key "
    "and the ledger does not. The result never includes the account holder's name, email or payment "
    "details."
)

ACCOUNT_FIELDS = {
    "api_requests_today": "apiRequests",
    "requests_date": "apiRequestsDate",
    "daily_rate_limit": "dailyRateLimit",
    "extra_limit": "extraLimit",
    "subscription_type": "subscriptionType",
    "subscription_mode": "subscriptionMode",
}
"""Output name -> name in EODHD's ``user`` answer. Nothing outside this table is passed on."""


def _account(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {name: raw.get(source) for name, source in ACCOUNT_FIELDS.items()}


async def get_usage(deps: Deps, days: int, include_account: bool) -> ToolResponse:
    """The ledger, the budget, the account counters (if asked for) and the cost table."""
    # Read the ledger before the account call: that call writes a (free) row of its own, which would
    # otherwise show up in today's requests.
    ledger = deps.ledger.summary(days)
    budget = deps.budget.status()
    now = deps.clock().astimezone(timezone.utc)
    notes = [
        "EODHD counts API calls per GMT day; its counter restarts at 00:00 GMT.",
        "The account counter includes other apps that share the key; this ledger counts only this server.",
        "Cached answers cost 0. Real-time quotes are charged per symbol.",
    ]
    if budget["daily_limit"] == 0:
        notes.append("The daily budget is 0, which means no limit: nothing is blocked and remaining is null.")

    account: dict[str, Any] | None = None
    account_error: str | None = None
    responses: list[ApiResponse] = []
    if include_account:
        try:
            account = _account(await deps.client.get_account())
        except GuardError as exc:
            account_error = redact(str(exc), [deps.settings.api_key])
            notes.append(
                "The account counters could not be read (see account_error); the ledger and budget are "
                "local and still correct."
            )
        else:
            responses.append(
                ApiResponse(
                    endpoint="user",
                    status=200,
                    data=None,
                    cached=False,
                    cost=0,
                    fetched_at=now,
                    duration_ms=0.0,
                )
            )
            counted = iso_date_or_none(account["requests_date"])
            if counted is not None and counted != now.date():
                notes.append(
                    f"EODHD's request counter is dated {counted.isoformat()}, not today "
                    f"({now.date().isoformat()}): api_requests_today is the count for that day."
                )

    data: dict[str, Any] = {"ledger": ledger, "budget": budget, "account": account}
    if account_error is not None:
        data["account_error"] = account_error
    data["cost_table"] = {key: entry.units for key, entry in COSTS.items()}
    return build_response(data, responses, notes=notes)


def register(server: MCPServer, deps: Deps) -> None:
    """Register get_usage."""

    @server.tool(
        name="get_usage",
        title="Get usage",
        description=GET_USAGE_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_usage_tool(
        days: Annotated[
            int,
            Field(
                ge=1,
                le=90,
                description="How many GMT days of the ledger to total, today included, 1 to 90.",
            ),
        ] = 7,
        include_account: Annotated[
            bool,
            Field(
                description=(
                    "Also read EODHD's account counters (free, no API call is charged). "
                    "False makes no network request."
                )
            ),
        ] = True,
    ) -> ToolResponse:
        return await get_usage(deps, days, include_account)
