"""Trading calendars: when a session ended, and when EODHD's end-of-day bar for it should exist.

Holidays are not modelled. A holiday therefore looks like a session that produced no bar, which is why
the stale-data threshold is several sessions and not one.

Time zones come from the standard library's ``zoneinfo``. Comparisons are made between UTC instants, never
between two wall-clock times of the same zone: Python compares those without looking at the UTC offset,
which gives wrong answers around a daylight-saving change.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

SessionState = Literal["closed_day", "pre_open", "open", "post_close"]


@dataclass(frozen=True)
class MarketInfo:
    """Trading hours and publishing delay of one EODHD exchange code."""

    code: str
    name: str
    tz: str
    open: time
    close: time
    publish_lag: timedelta
    """Typical delay between the close and the appearance of EODHD's end-of-day bar."""
    lag_measured: bool
    """True only where the lag was measured against the live API (October 2026)."""
    weekend: tuple[int, ...] = (5, 6)
    """Weekday numbers (Monday is 0) on which the market does not trade."""
    currency_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.open < self.close:
            raise ValueError(f"{self.code}: the market must open before it closes")
        if self.publish_lag < timedelta(0):
            raise ValueError(f"{self.code}: publish_lag cannot be negative")
        days = set(self.weekend)
        if not days <= set(range(7)) or len(days) == 7:
            raise ValueError(f"{self.code}: weekend must be weekday numbers 0-6 and leave a trading day")


def _market(
    code: str,
    name: str,
    tz: str,
    hours: tuple[tuple[int, int], tuple[int, int]],
    lag: timedelta,
    *,
    measured: bool = False,
    weekend: tuple[int, ...] = (5, 6),
    currency_hint: str | None = None,
) -> MarketInfo:
    (open_h, open_m), (close_h, close_m) = hours
    return MarketInfo(
        code=code,
        name=name,
        tz=tz,
        open=time(open_h, open_m),
        close=time(close_h, close_m),
        publish_lag=lag,
        lag_measured=measured,
        weekend=weekend,
        currency_hint=currency_hint,
    )


_EUROPE_DAY = ((9, 0), (17, 30))
_LAG_4H = timedelta(hours=4)

_TABLE = (
    _market(
        "US", "US exchanges", "America/New_York", ((9, 30), (16, 0)), timedelta(minutes=30), measured=True
    ),
    _market(
        "LSE",
        "London Stock Exchange",
        "Europe/London",
        ((8, 0), (16, 30)),
        timedelta(hours=2),
        measured=True,
        currency_hint="GBX",
    ),
    _market("SW", "SIX Swiss Exchange", "Europe/Zurich", _EUROPE_DAY, _LAG_4H, measured=True),
    _market("NSE", "NSE India", "Asia/Kolkata", ((9, 15), (15, 30)), timedelta(hours=5), measured=True),
    _market("BSE", "BSE India", "Asia/Kolkata", ((9, 15), (15, 30)), timedelta(hours=5)),
    _market("XETRA", "Xetra (Germany)", "Europe/Berlin", _EUROPE_DAY, _LAG_4H),
    _market("F", "Frankfurt (Germany)", "Europe/Berlin", _EUROPE_DAY, _LAG_4H),
    _market("PA", "Euronext Paris", "Europe/Paris", _EUROPE_DAY, _LAG_4H),
    _market("AS", "Euronext Amsterdam", "Europe/Amsterdam", _EUROPE_DAY, _LAG_4H),
    _market("BR", "Euronext Brussels", "Europe/Brussels", _EUROPE_DAY, _LAG_4H),
    # Euronext Lisbon trades the same hours as the rest of Euronext, which is one hour earlier on Lisbon time.
    _market("LS", "Euronext Lisbon", "Europe/Lisbon", ((8, 0), (16, 30)), _LAG_4H),
    _market("MC", "Bolsa de Madrid", "Europe/Madrid", _EUROPE_DAY, _LAG_4H),
    _market("MI", "Borsa Italiana (Milan)", "Europe/Rome", _EUROPE_DAY, _LAG_4H),
    _market("TO", "Toronto Stock Exchange", "America/Toronto", ((9, 30), (16, 0)), timedelta(hours=2)),
    _market("V", "TSX Venture Exchange", "America/Toronto", ((9, 30), (16, 0)), timedelta(hours=2)),
    _market("HK", "Hong Kong Exchange", "Asia/Hong_Kong", ((9, 30), (16, 0)), _LAG_4H),
    _market("AU", "ASX (Australia)", "Australia/Sydney", ((10, 0), (16, 0)), _LAG_4H),
    _market("CC", "Crypto (24/7)", "UTC", ((0, 0), (23, 59)), timedelta(hours=1), weekend=()),
    _market("FOREX", "Forex", "UTC", ((0, 0), (23, 59)), timedelta(hours=2)),
)

MARKETS: Mapping[str, MarketInfo] = MappingProxyType({market.code: market for market in _TABLE})

DEFAULT_MARKET = _market(
    "DEFAULT",
    "Unknown exchange (UTC assumed)",
    "UTC",
    ((0, 0), (23, 59)),
    timedelta(hours=6),
)
"""Stands in for any exchange not in ``MARKETS``: a Monday-to-Friday UTC day with a generous delay."""


def market_for(exchange: str) -> MarketInfo:
    """Look up an exchange code, case-insensitively. Unknown codes get ``DEFAULT_MARKET``."""
    return MARKETS.get(exchange.strip().upper(), DEFAULT_MARKET)


def _utc(moment: datetime) -> datetime:
    """Return ``moment`` in UTC. A naive value is taken to be UTC, not the machine's local time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _ready_at(market: MarketInfo, day: date) -> datetime:
    """The UTC instant from which the end-of-day bar for ``day`` is expected to exist."""
    close = datetime.combine(day, market.close, tzinfo=ZoneInfo(market.tz))
    return close.astimezone(timezone.utc) + market.publish_lag


def expected_latest_session(exchange: str, now: datetime) -> date:
    """The most recent trading day whose bar should exist by ``now``.

    That is the latest non-weekend day whose close plus the publishing delay is at or before ``now``,
    counted on the exchange's own calendar. The date is local to the exchange, so it can differ from the
    UTC date.
    """
    market = market_for(exchange)
    moment = _utc(now)
    day = moment.astimezone(ZoneInfo(market.tz)).date()
    # Ends because a weekend never covers all seven days and _ready_at falls as the day goes back.
    while day.weekday() in market.weekend or _ready_at(market, day) > moment:
        day -= timedelta(days=1)
    return day


def _count_sessions(earlier: date, later: date, weekend: Collection[int]) -> int:
    span = (later - earlier).days
    if span <= 0:
        return 0
    off_days = frozenset(weekend)
    weeks, rest = divmod(span, 7)
    # Any seven consecutive days hold the same number of trading days, so only the leftover days need a look.
    leftover = sum(1 for step in range(1, rest + 1) if (earlier.weekday() + step) % 7 not in off_days)
    return weeks * (7 - len(off_days)) + leftover


def sessions_between(earlier: date, later: date, exchange: str) -> int:
    """Trading days in the interval after ``earlier`` up to and including ``later``; 0 if not ordered."""
    return _count_sessions(earlier, later, market_for(exchange).weekend)


def session_state(exchange: str, now: datetime) -> SessionState:
    """Where ``now`` falls in the exchange's trading day. The open is inclusive and the close exclusive."""
    market = market_for(exchange)
    local = _utc(now).astimezone(ZoneInfo(market.tz))
    if local.weekday() in market.weekend:
        return "closed_day"
    clock = local.time()
    if clock < market.open:
        return "pre_open"
    if clock < market.close:
        return "open"
    return "post_close"
