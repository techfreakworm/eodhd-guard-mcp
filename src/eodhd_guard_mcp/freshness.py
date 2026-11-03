"""Is the newest bar recent enough?

EODHD keeps answering HTTP 200 for a code that was delisted, renamed or frozen, with data that simply
stops. The only sign is the date of the last bar, so it is compared against the latest session the
exchange should have published by now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from eodhd_guard_mcp.envelope import ToolWarning, WarningCode
from eodhd_guard_mcp.markets import expected_latest_session, sessions_between
from eodhd_guard_mcp.validation import Symbol


@dataclass(frozen=True)
class Freshness:
    """How far behind the newest bar is. ``sessions_behind`` is 0 when there is no bar to compare."""

    last_bar: date | None
    expected_latest: date
    sessions_behind: int
    stale: bool


def assess(exchange: str, last_bar: date | None, now: datetime, *, threshold: int) -> Freshness:
    """Compare the newest bar with the latest session expected by ``now``.

    Stale means no bar at all, or ``threshold`` or more sessions behind. A bar newer than expected
    (EODHD published faster than the usual delay) counts as 0 behind.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be at least 1, got {threshold}")
    expected = expected_latest_session(exchange, now)
    if last_bar is None:
        return Freshness(last_bar=None, expected_latest=expected, sessions_behind=0, stale=True)
    behind = sessions_between(last_bar, expected, exchange)
    return Freshness(
        last_bar=last_bar, expected_latest=expected, sessions_behind=behind, stale=behind >= threshold
    )


def stale_warning(symbol: Symbol, fr: Freshness) -> ToolWarning:
    """The STALE_DATA warning: the dates, the gap, why a 200 proves nothing, and what to try next."""
    expected = fr.expected_latest.isoformat()
    if fr.last_bar is None:
        finding = (
            f"EODHD returned no bar for {symbol.full}, although a bar for {expected} should exist by now."
        )
    else:
        unit = "session" if fr.sessions_behind == 1 else "sessions"
        finding = (
            f"The newest bar for {symbol.full} is dated {fr.last_bar.isoformat()}, but the latest session "
            f"expected by now is {expected}: {fr.sessions_behind} {unit} behind."
        )
    message = (
        f"{finding} EODHD keeps answering HTTP 200 for codes that are frozen, delisted or renamed, so this "
        "may be a dead line and not a quiet market. Run check_symbol to test the code, and use "
        "search_symbols with the company name to look for a successor code."
    )
    return ToolWarning.of(
        WarningCode.STALE_DATA,
        message,
        symbol=symbol.full,
        last_bar=fr.last_bar.isoformat() if fr.last_bar is not None else None,
        expected_latest=expected,
        sessions_behind=fr.sessions_behind,
    )
