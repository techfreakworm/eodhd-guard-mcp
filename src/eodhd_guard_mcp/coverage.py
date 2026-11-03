"""Markets EODHD does not cover, caught before a call is made and billed.

For these markets EODHD answers a bare 404, an empty list, or HTTP 200 with zero rows, and none of
those says "no coverage". The registry records what the October 2026 checks found, so the model gets a
clear refusal with alternatives instead of an empty result it might read as "no trading".
"""

from __future__ import annotations

from dataclasses import dataclass

from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.validation import Symbol

ESCAPE_HATCH = "EODHD_GUARD_ALLOW_UNCOVERED=1"

_CAPABILITIES = {
    "prices": "price data",
    "quotes": "quotes",
    "fundamentals": "fundamentals",
    "dividends": "dividend data",
    "splits": "split data",
    "symbols": "symbol lists",
}


@dataclass(frozen=True)
class CoverageGap:
    """A market EODHD returned nothing useful for when it was checked."""

    key: str
    market: str
    suffixes: frozenset[str]
    """Exchange codes and symbol suffixes that all mean this market."""
    calendar_dates: bool
    """True when the whole-market earnings calendar still has rows for this market."""
    message: str
    """What is missing, when it was checked, and what still works."""
    alternatives: str
    """Where the data can come from instead."""


GAPS: tuple[CoverageGap, ...] = (
    CoverageGap(
        key="tokyo",
        market="Tokyo Stock Exchange",
        suffixes=frozenset({"TSE", "T", "JP", "JPX", "XTKS", "TYO"}),
        calendar_dates=True,
        message=(
            "In checks in October 2026, EODHD returned no prices, fundamentals, dividends or symbol lists "
            "for Tokyo: every suffix tried (TSE, T, JP, JPX, XTKS, TYO) answered 404. What still works: the "
            "whole-market earnings calendar (get_earnings_calendar) lists Tokyo report dates, without prices."
        ),
        alternatives=(
            "J-Quants (the Japan Exchange Group's own data API), or the US-listed ADR of the company where "
            "one exists, such as TM.US for Toyota."
        ),
    ),
    CoverageGap(
        key="uae",
        market="UAE exchanges (DFM and ADX)",
        suffixes=frozenset({"AE", "DFM", "ADX", "AD", "XDFM", "XADS"}),
        calendar_dates=False,
        message=(
            "In checks in October 2026, EODHD returned no prices, fundamentals or earnings dates for the UAE "
            "(Dubai Financial Market and Abu Dhabi Securities Exchange). The AE exchange is listed but its "
            "symbol list is empty, and EMAAR.AE answers HTTP 200 with zero rows."
        ),
        alternatives=(
            "the Dubai Financial Market's own historical data export, or a data vendor that covers "
            "DFM and ADX."
        ),
    ),
)


def find_gap(exchange: str) -> CoverageGap | None:
    """The gap an exchange code or symbol suffix belongs to, or None when EODHD covers it.

    Toronto (``TO``) and Tel Aviv (``TA``) are not gaps; only the exact suffixes above match.
    """
    code = exchange.strip().upper()
    return next((gap for gap in GAPS if code in gap.suffixes), None)


def _check_capability(capability: str) -> str:
    try:
        return _CAPABILITIES[capability]
    except KeyError:
        raise ValueError(
            f"unknown capability {capability!r}; expected one of {', '.join(_CAPABILITIES)}"
        ) from None


def _not_covered(subject: str, gap: CoverageGap, capability: str, label: str) -> GuardError:
    return GuardError(
        ErrorCode.NOT_COVERED,
        f"{subject}: EODHD has no {label} for the {gap.market}. {gap.message}",
        hint=(
            f"Alternatives: {gap.alternatives} If EODHD has added coverage since, set {ESCAPE_HATCH} "
            "to skip this check."
        ),
        details={"subject": subject, "market": gap.market, "gap": gap.key, "capability": capability},
    )


def ensure_exchange_covered(exchange: str, capability: str, *, allow_uncovered: bool) -> None:
    """Raise ``NOT_COVERED`` when the exchange is a known gap, unless ``allow_uncovered`` is set.

    For calls that have an exchange but no symbol, such as listing an exchange's symbols.
    """
    label = _check_capability(capability)
    gap = find_gap(exchange)
    if gap is not None and not allow_uncovered:
        raise _not_covered(exchange.strip().upper(), gap, capability, label)


def ensure_covered(symbol: Symbol, capability: str, *, allow_uncovered: bool) -> None:
    """Raise ``NOT_COVERED`` when the symbol's market is a known gap, unless ``allow_uncovered`` is set.

    ``capability`` is one of prices, quotes, fundamentals, dividends, splits or symbols; anything else
    is a programming error and raises ``ValueError``.
    """
    label = _check_capability(capability)
    gap = find_gap(symbol.exchange)
    if gap is not None and not allow_uncovered:
        raise _not_covered(symbol.full, gap, capability, label)
