"""Server wiring: the objects every tool shares.

``build_deps`` creates the ledger, cache, budget and client from the settings. ``Deps`` is what the
tools receive.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx2

from eodhd_guard_mcp.cache import ResponseCache
from eodhd_guard_mcp.client import EODHDClient
from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.ledger import Budget, UsageLedger

# build_server: added in the tools phase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    """The shared objects behind every tool. ``clock`` returns an aware UTC datetime."""

    settings: Settings
    client: EODHDClient
    ledger: UsageLedger
    cache: ResponseCache
    budget: Budget
    clock: Callable[[], datetime]

    async def aclose(self) -> None:
        """Close the client, then the cache and the ledger, even if one of them fails."""
        try:
            await self.client.aclose()
        finally:
            try:
                self.cache.close()
            finally:
                self.ledger.close()


def build_deps(
    settings: Settings,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> Deps:
    """Create the ledger, cache, budget and client. ``transport`` and ``sleep`` exist for tests."""
    now = clock or _utc_now
    ledger = UsageLedger(settings.state_dir / "usage.sqlite", clock=now)
    try:
        cache = ResponseCache(settings.state_dir / "cache.sqlite", enabled=settings.cache_enabled, clock=now)
    except Exception:
        ledger.close()
        raise
    budget = Budget(ledger, settings.daily_budget, clock=now)
    client = EODHDClient(
        settings, ledger=ledger, cache=cache, budget=budget, clock=now, transport=transport, sleep=sleep
    )
    return Deps(settings=settings, client=client, ledger=ledger, cache=cache, budget=budget, clock=now)
