"""What an EODHD end-of-day row is, and which prices are safe to read as what.

Three things the raw rows hide: holiday "filler" bars that repeat the last close with zero volume, the
fact that ``close`` is not adjusted for splits or dividends yet is back-adjusted for some spin-offs, and
rows that cannot be read at all.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Literal

from eodhd_guard_mcp.envelope import ToolWarning, WarningCode
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.validation import Symbol, iso_date_or_none

_PRICE_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Bar:
    """One daily bar. ``close`` is as reported; ``adjusted_close`` is adjusted for splits and dividends."""

    day: date
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float | None
    volume: float


def _number(value: object) -> float | None:
    """A finite number from a JSON value. Strings (including "NA"), booleans and null are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _read_bar(row: object) -> Bar | None:
    if not isinstance(row, dict):
        return None
    day = iso_date_or_none(row.get("date"))
    open_, high, low, close, volume = (_number(row.get(field)) for field in _PRICE_FIELDS)
    if day is None or open_ is None or high is None or low is None or close is None or volume is None:
        return None
    return Bar(
        day=day,
        open=open_,
        high=high,
        low=low,
        close=close,
        adjusted_close=_number(row.get("adjusted_close")),
        volume=volume,
    )


def _describe(body: object) -> str:
    if isinstance(body, dict):
        names = ", ".join(str(key)[:30] for key in list(body)[:5])
        return f"an object with the keys {names}" if names else "an empty object"
    if body is None:
        return "null"
    return f"a {type(body).__name__} value"


def normalize_eod_rows(rows: Any) -> list[Bar]:
    """Read EODHD's end-of-day rows into bars, sorted by date with one bar per day.

    Rows that cannot be read are skipped, and when a date repeats the later row wins. An empty list is
    returned as it is (the caller decides what an empty answer means); anything that is not a list, or a
    non-empty list in which no row can be read, is ``BAD_RESPONSE``.
    """
    if not isinstance(rows, list):
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            f"EODHD answered with {_describe(rows)} where a list of daily bars was expected.",
            hint="The API may have changed its response shape; the data cannot be trusted as it is.",
        )
    by_day: dict[date, Bar] = {}
    for row in rows:
        bar = _read_bar(row)
        if bar is not None:
            by_day[bar.day] = bar
    if rows and not by_day:
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            f"None of the {len(rows)} rows EODHD returned could be read as a daily bar. Each needs a "
            "YYYY-MM-DD date and numeric open, high, low, close and volume.",
            hint="The API may have changed its response shape; the data cannot be trusted as it is.",
        )
    return [by_day[day] for day in sorted(by_day)]


def is_filler_bar(bar: Bar) -> bool:
    """EODHD fills holidays with a bar of zero volume whose four prices all repeat the last close."""
    return bar.volume == 0 and bar.open == bar.high == bar.low == bar.close


def _volume(bar: Bar) -> int | float:
    return int(bar.volume) if bar.volume.is_integer() else bar.volume


def _factor(bar: Bar) -> float | None:
    """The scale from reported prices to adjusted ones, or None when it cannot be known."""
    adjusted = bar.adjusted_close
    if adjusted is None:
        return None
    if bar.close == 0:
        return 1.0 if adjusted == 0 else None
    return adjusted / bar.close


def _scaled(price: float, factor: float | None) -> float | None:
    """``price * factor`` without float noise: at least six decimals and six significant figures kept."""
    if factor is None:
        return None
    scaled = price * factor
    if not math.isfinite(scaled):
        return None
    if scaled == 0:
        return 0.0
    return round(scaled, max(6, 5 - math.floor(math.log10(abs(scaled)))))


def apply_basis(bars: Sequence[Bar], basis: Literal["both", "raw", "adjusted"]) -> list[dict[str, Any]]:
    """Render bars as dicts on the requested price basis.

    ``both`` and ``raw`` pass EODHD's numbers through untouched. ``adjusted`` scales open, high and low by
    ``adjusted_close / close`` and reports ``adjusted_close`` as the close; where that factor cannot be
    known (no ``adjusted_close``, or a zero ``close``) the price is None and never silently the raw one.
    Volume is always as reported.
    """
    if basis == "both":
        return [
            {
                "date": bar.day.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "adjusted_close": bar.adjusted_close,
                "volume": _volume(bar),
            }
            for bar in bars
        ]
    if basis == "raw":
        return [
            {
                "date": bar.day.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": _volume(bar),
            }
            for bar in bars
        ]
    if basis == "adjusted":
        rows: list[dict[str, Any]] = []
        for bar in bars:
            factor = _factor(bar)
            rows.append(
                {
                    "date": bar.day.isoformat(),
                    "open": _scaled(bar.open, factor),
                    "high": _scaled(bar.high, factor),
                    "low": _scaled(bar.low, factor),
                    "close": bar.adjusted_close,
                    "volume": _volume(bar),
                }
            )
        return rows
    raise ValueError(f"unknown price basis {basis!r}; expected both, raw or adjusted")


KNOWN_SPINOFF_ADJUSTMENTS: Mapping[str, tuple[tuple[date, str], ...]] = MappingProxyType(
    {
        "NOVN.SW": (
            (date(2019, 4, 9), "Alcon spin-off"),
            (date(2023, 10, 4), "Sandoz spin-off"),
        ),
        "ULVR.LSE": ((date(2025, 12, 8), "The Magnum Ice Cream Company demerger"),),
        "RELIANCE.NSE": ((date(2023, 7, 20), "Jio Financial Services demerger"),),
    }
)
"""Events EODHD folds into ``close`` for earlier dates (October 2026 checks), by full symbol, oldest first."""


def spinoff_warnings(symbol: Symbol, first_day: date) -> list[ToolWarning]:
    """One SPINOFF_ADJUSTED_CLOSE warning per known event that falls after ``first_day``.

    A range that starts on the event date holds no bar from before it, so it gets no warning.
    """
    return [
        ToolWarning.of(
            WarningCode.SPINOFF_ADJUSTED_CLOSE,
            f"{event} ({event_day.isoformat()}): the range starts before this event. EODHD back-adjusts "
            f"close for it, so open/high/low/close dated before {event_day.isoformat()} can differ from the "
            "price that actually traded. Do not use them as traded prices, for example for a cost basis.",
            symbol=symbol.full,
            event=event,
            event_date=event_day.isoformat(),
        )
        for event_day, event in KNOWN_SPINOFF_ADJUSTMENTS.get(symbol.full, ())
        if first_day < event_day
    ]


_AS_REPORTED = (
    "open/high/low/close are as EODHD reports them: not adjusted for splits or dividends, but sometimes "
    "back-adjusted for spin-offs and demergers, so they may differ from the traded price. adjusted_close "
    "is adjusted for splits and dividends; use it for returns."
)

PRICE_BASIS_NOTES: Mapping[str, str] = MappingProxyType(
    {
        "both": _AS_REPORTED,
        "raw": _AS_REPORTED,
        "adjusted": (
            "open/high/low/close are scaled by adjusted_close / close, so they are adjusted for splits and "
            "dividends and the close equals adjusted_close. They are not the prices that traded; use them "
            "for returns, not for what a trade cost. Volume is as reported by EODHD, not adjusted."
        ),
    }
)
