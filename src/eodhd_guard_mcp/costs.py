"""What each EODHD endpoint costs, in the API calls EODHD itself counts.

The prices are EODHD's documented ones, checked against measurements in October 2026. Only HTTP 200
answers are billed; 401, 402, 403 and 404 were measured as free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class EndpointCost:
    key: str
    units: int
    per_symbol: bool = False
    """True when the price is per symbol requested, as for ``real-time``."""
    measured: bool = True
    """False when the price comes from EODHD's documentation and was not confirmed by a call."""
    note: str = ""


_ENDPOINTS = (
    EndpointCost("eod", 1),
    EndpointCost("intraday", 5),
    EndpointCost("real-time", 1, per_symbol=True, note="`s=` adds symbols, 1 each"),
    EndpointCost("fundamentals", 10, note="one request for the whole document"),
    EndpointCost(
        "calendar/earnings", 1, note="1 for any window without `symbols`; 1 per symbol with `symbols`"
    ),
    EndpointCost("calendar/splits", 1),
    EndpointCost("calendar/dividends", 1, measured=False, note="assumed from the Calendar API price"),
    EndpointCost("div", 1),
    EndpointCost("splits", 1),
    EndpointCost("exchanges-list", 1),
    EndpointCost("exchange-symbol-list", 1),
    EndpointCost("search", 1),
    EndpointCost(
        "news",
        5,
        per_symbol=True,
        note=(
            "measured for one ticker or a topic; several tickers are modelled as 5 each from EODHD's "
            "documentation, and a topic counts as one"
        ),
    ),
    EndpointCost(
        "sentiments",
        5,
        per_symbol=True,
        note="measured for one ticker; several tickers are modelled as 5 each",
    ),
    EndpointCost("economic-events", 1, note="1 per request whatever the window, country or limit"),
    EndpointCost("user", 0, note="account counters"),
)

COSTS: Mapping[str, EndpointCost] = MappingProxyType({entry.key: entry for entry in _ENDPOINTS})


def cost_for(key: str, n_symbols: int = 1) -> int:
    """Units charged for one call to ``key``; ``n_symbols`` only matters for per-symbol endpoints.

    Raises ``KeyError`` for an unknown endpoint and ``ValueError`` for a negative symbol count.
    """
    entry = COSTS[key]
    if n_symbols < 0:
        raise ValueError(f"n_symbols must not be negative, got {n_symbols}")
    return entry.units * n_symbols if entry.per_symbol else entry.units
