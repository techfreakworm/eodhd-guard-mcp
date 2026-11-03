"""Tools: get_eod_prices and get_intraday_prices.

Both check every parameter before a call is made (EODHD ignores bad ones), refuse markets EODHD does not
cover, and turn answers that look like data but are not into errors or warnings. The logic lives in
``fetch_eod_prices`` and ``fetch_intraday_prices``; the registered tools only forward their arguments.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from eodhd_guard_mcp.costs import cost_for
from eodhd_guard_mcp.coverage import ensure_covered
from eodhd_guard_mcp.envelope import ToolResponse, ToolWarning, WarningCode, build_response, iso_utc
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.freshness import Freshness, stale_warning
from eodhd_guard_mcp.markets import (
    expected_latest_session,
    market_for,
    next_ready_at,
    session_on_or_before,
    session_state,
    sessions_between,
)
from eodhd_guard_mcp.semantics import (
    PRICE_BASIS_NOTES,
    Bar,
    apply_basis,
    is_filler_bar,
    normalize_eod_rows,
    spinoff_warnings,
)
from eodhd_guard_mcp.tools import READ_ONLY, guarded
from eodhd_guard_mcp.validation import (
    Symbol,
    check_int,
    iso_date_or_none,
    parse_date,
    parse_datetime_utc,
    parse_symbol,
    resolve_range,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.client import ApiResponse
    from eodhd_guard_mcp.server import Deps

Period = Literal["d", "w", "m"]
PriceBasis = Literal["both", "raw", "adjusted"]
Interval = Literal["1m", "5m", "1h"]

DEFAULT_MAX_ROWS = 500
MAX_ROWS = 5000

EOD_DEFAULT_DAYS = 30
EOD_RECENT_TTL_S = 6 * 3600.0
"""The longest a range that reaches the newest sessions is kept. It is cut short when a bar is due sooner."""
EOD_RECENT_MIN_TTL_S = 300.0
"""The shortest it is kept, so a bar that is almost due does not turn into a call on every read."""
EOD_HISTORIC_TTL_S = 24 * 3600.0
FRESHNESS_WINDOW_DAYS = 7
"""The stale-data check runs on daily bars whose range ends this close to today."""
FILLER_DATES_SHOWN = 10

INTRADAY_RECENT_TTL_S = 15 * 60.0
INTRADAY_HISTORIC_TTL_S = 24 * 3600.0
INTRADAY_STEP_S = 15 * 60
"""A default window end is rounded up to this step, so calls in the same quarter hour share a cache key."""


@dataclass(frozen=True)
class _IntradaySpan:
    default: timedelta
    longest: timedelta


INTRADAY_SPANS: dict[str, _IntradaySpan] = {
    "1m": _IntradaySpan(default=timedelta(days=1), longest=timedelta(days=120)),
    "5m": _IntradaySpan(default=timedelta(days=3), longest=timedelta(days=600)),
    "1h": _IntradaySpan(default=timedelta(days=14), longest=timedelta(days=7200)),
}

SYMBOL_HELP = (
    "The symbol as CODE.EXCHANGE, for example AAPL.US, VOD.LSE, NESN.SW or RELIANCE.NSE. A code without an "
    "exchange is refused. US share classes use a hyphen, as in BRK-B.US."
)
MAX_ROWS_HELP = "Most bars to return, 1 to 5000. When there are more, the most recent are kept (TRUNCATED)."

EOD_DESCRIPTION = (
    "End-of-day bars (daily, weekly or monthly) for one symbol. Costs 1 API call; cached for 6 hours when "
    "the range ends within a day of today, otherwise 24 hours. close is as EODHD reports it: not adjusted "
    "for splits or dividends, and sometimes back-adjusted for spin-offs, so use adjusted_close for returns. "
    "EODHD answers HTTP 200 for dead codes, so a newest bar that is several sessions old gives STALE_DATA, "
    "and an empty answer for a range with trading days is an error."
)
INTRADAY_DESCRIPTION = (
    "Intraday bars (1m, 5m or 1h) for one symbol, timestamped in UTC. Costs 5 API calls; cached for 15 "
    "minutes when the window includes today, otherwise 24 hours. EODHD publishes intraday bars after the "
    "session, never during it, so today's bars are usually missing (INTRADAY_NOT_SAME_DAY): use get_quote "
    "for a delayed current price. A window with no bars is an empty result with NO_INTRADAY_DATA, not an "
    "error, and the endpoint needs a plan with intraday data."
)


def _invalid(
    field: str, received: str, problem: str, accepted: str, *, hint: str | None = None
) -> GuardError:
    """An ``INVALID_PARAMS`` error worded like the ones in ``validation``."""
    return GuardError(
        ErrorCode.INVALID_PARAMS,
        f"{field}: received {received!r}; {problem}. Accepted: {accepted}.",
        hint=hint,
        details={"field": field, "received": received},
    )


def share_class_hint(symbol: Symbol) -> str:
    """For ``BRK.B``, which parses as code BRK on exchange B: EODHD spells US share classes with a hyphen."""
    if len(symbol.exchange) != 1:
        return ""
    return (
        f" {symbol.full} was read as code {symbol.code} on exchange {symbol.exchange}. EODHD writes US share "
        f"classes with a hyphen, as in {symbol.code}-{symbol.exchange}.US."
    )


def not_found_with_hint(error: GuardError, symbol: Symbol, what: str) -> GuardError:
    """Re-raise EODHD's 404 with the tools that find the right code."""
    return GuardError(
        ErrorCode.NOT_FOUND,
        f"No {what} for {symbol.full}. {error.message}",
        hint=(
            "Find the right code with search_symbols (by company name or ticker). For a delisted line, "
            "list_exchange_symbols(delisted=true) lists the codes EODHD still holds for the exchange."
            + share_class_hint(symbol)
        ),
        details=error.details,
    )


def currency_note(symbols: Sequence[Symbol]) -> ToolWarning | None:
    """CURRENCY_NOTE for lines quoted in pence (London), or None when none of the symbols is."""
    lines = [symbol.full for symbol in symbols if market_for(symbol.exchange).currency_hint == "GBX"]
    if not lines:
        return None
    return ToolWarning.of(
        WarningCode.CURRENCY_NOTE,
        f"{', '.join(lines)}: London lines are usually quoted in GBX (pence), not GBP (pounds). A price of "
        "1250 means 1250 pence, which is 12.50 pounds. EODHD's answer carries no currency field, so check "
        "the scale before comparing with figures in pounds or dollars.",
        symbols=lines,
        currency="GBX",
    )


def recent_bars_ttl_s(exchange: str, now: datetime, *, cap_s: float) -> float:
    """How long to keep an answer whose range reaches the newest sessions: until the next bar is due.

    The answer gains a bar when ``next_ready_at`` arrives, so keeping it past that moment would hide the
    bar. The time is at least ``EOD_RECENT_MIN_TTL_S`` and at most ``cap_s``.
    """
    until_due = (next_ready_at(exchange, now) - now).total_seconds()
    return max(EOD_RECENT_MIN_TTL_S, min(cap_s, until_due))


def _truncated_warning(total: int, shown: int, limit: int, oldest: str, noun: str) -> ToolWarning:
    return ToolWarning.of(
        WarningCode.TRUNCATED,
        f"{total} {noun} matched, but max_rows is {limit}: only the most recent {shown} are returned, "
        f"starting at {oldest}. Raise max_rows (up to {MAX_ROWS}) or narrow the range to see the rest.",
        total=total,
        returned=shown,
        max_rows=limit,
        kept="most_recent",
    )


# --- get_eod_prices -----------------------------------------------------------------------------------


def _day_before(day: date) -> date:
    return day - timedelta(days=1) if day > date.min else day


def _bar_span(day: date, period: str) -> tuple[date, date]:
    """The first and last calendar day that a bar labelled ``day`` can cover.

    EODHD's label for a weekly or monthly bar may be the first or the last day of the period, so a
    weekly bar covers its whole week and a monthly bar its whole month.
    """
    if period == "w":
        monday = day - timedelta(days=day.weekday())
        return monday, monday + timedelta(days=6)
    if period == "m":
        first = day.replace(day=1)
        return first, first.replace(day=calendar.monthrange(day.year, day.month)[1])
    return day, day


def _within(bar: Bar, period: str, start: date, end: date) -> bool:
    """True when the bar's period overlaps the requested range."""
    try:
        first, last = _bar_span(bar.day, period)
    except OverflowError:  # a label at the edge of the calendar: no real bar looks like this
        return False
    return first <= end and last >= start


def _clamped_eod_warning(requested: date, used: date) -> ToolWarning:
    return ToolWarning.of(
        WarningCode.RANGE_CLAMPED,
        f"The end date {requested.isoformat()} is in the future and no data exists after today, so the "
        f"range ends on {used.isoformat()}.",
        requested_end=requested.isoformat(),
        end=used.isoformat(),
    )


def _trimmed_eod_warning(outside: Sequence[Bar], start: date, end: date) -> ToolWarning:
    return ToolWarning.of(
        WarningCode.RANGE_TRIMMED,
        f"EODHD returned {len(outside)} bar(s) dated outside the requested range {start.isoformat()} to "
        f"{end.isoformat()} (from {outside[0].day.isoformat()} to {outside[-1].day.isoformat()}); they were "
        "dropped.",
        dropped=len(outside),
        first=outside[0].day.isoformat(),
        last=outside[-1].day.isoformat(),
    )


def _filler_warning(removed: Sequence[Bar], *, nothing_left: bool) -> ToolWarning:
    shown = ", ".join(bar.day.isoformat() for bar in removed[:FILLER_DATES_SHOWN])
    more = f" and {len(removed) - FILLER_DATES_SHOWN} more" if len(removed) > FILLER_DATES_SHOWN else ""
    message = (
        f"Removed {len(removed)} holiday filler bar(s) (zero volume, open = high = low = close): "
        f"{shown}{more}. Pass include_filler_bars=true to keep them."
    )
    if nothing_left:
        message += (
            " Every bar in the range was filler, so no real trading day remains. If the range is not a "
            "holiday period, the code may be dead: run check_symbol."
        )
    return ToolWarning.of(
        WarningCode.FILLER_BARS_REMOVED,
        message,
        removed=len(removed),
        dates=[bar.day.isoformat() for bar in removed[:FILLER_DATES_SHOWN]],
    )


def _no_data_error(
    symbol: Symbol, start: date, end: date, response: ApiResponse, outside: Sequence[Bar]
) -> GuardError:
    dropped = (
        f" EODHD did send {len(outside)} bar(s) dated outside the range; they were dropped."
        if outside
        else ""
    )
    billing = f"counted as {response.cost} API call(s)" if response.cost else "came from the cache (cost 0)"
    return GuardError(
        ErrorCode.NO_DATA,
        f"EODHD answered HTTP 200 with no bars for {symbol.full} from {start.isoformat()} to "
        f"{end.isoformat()}, a range that contains trading days.{dropped} The code may be delisted, renamed "
        "or not covered by EODHD; a range made only of market holidays gives the same answer. The call "
        f"{billing}.",
        hint=(
            "Run check_symbol to test the code and look for a successor; search_symbols finds codes by "
            "company name."
        ),
        details={"symbol": symbol.full, "start": start.isoformat(), "end": end.isoformat()},
    )


def _not_due_warning(symbol: Symbol, start: date, end: date, latest_due: date, now: datetime) -> ToolWarning:
    """EMPTY_RESULT for a range of sessions whose end-of-day bar EODHD has not had time to publish."""
    due = iso_utc(next_ready_at(symbol.exchange, now))
    return ToolWarning.of(
        WarningCode.EMPTY_RESULT,
        f"The range {start.isoformat()} to {end.isoformat()} holds no session whose end-of-day bar is due "
        f"yet: the latest bar expected by now is for {latest_due.isoformat()}, and the next one is expected "
        f"from {due}. An empty list is the correct answer until then, and no data is missing.",
        symbol=symbol.full,
        latest_due=latest_due.isoformat(),
        next_bar_due=due,
    )


def _freshness_warning(
    symbol: Symbol,
    period: str,
    end: date,
    real_days: Sequence[date],
    *,
    now: datetime,
    threshold: int,
) -> ToolWarning | None:
    """STALE_DATA for daily bars, when the range ends close to today. Filler bars never count as fresh.

    The newest bar is judged against the last session the range can hold: the latest one due by now, or
    the range's own last session when the range stops earlier. A complete range that ended days ago is
    therefore not "behind" just because time has passed since it ended.
    """
    if (
        period != "d"
        or not real_days
        or (now.astimezone(timezone.utc).date() - end).days > FRESHNESS_WINDOW_DAYS
    ):
        return None
    due = expected_latest_session(symbol.exchange, now)
    reached = session_on_or_before(symbol.exchange, end)
    expected = min(due, reached)
    behind = sessions_between(real_days[-1], expected, symbol.exchange)
    if behind < threshold:
        return None
    warning = stale_warning(
        symbol,
        Freshness(last_bar=real_days[-1], expected_latest=expected, sessions_behind=behind, stale=True),
    )
    if expected == due:
        return warning
    ends = f"the range ends on {end.isoformat()}, whose last session is"
    return warning.model_copy(
        update={"message": warning.message.replace("the latest session expected by now is", ends, 1)}
    )


async def fetch_eod_prices(
    deps: Deps,
    *,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    period: Period = "d",
    price_basis: PriceBasis = "both",
    include_filler_bars: bool = False,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ToolResponse:
    """The logic of get_eod_prices: check the input, call EODHD once, then run the guards in order."""
    sym = parse_symbol(symbol)
    now = deps.clock()
    today = now.astimezone(timezone.utc).date()
    requested_end = parse_date(end, "end")
    window = resolve_range(
        parse_date(start, "start"), requested_end, today=today, default_days=EOD_DEFAULT_DAYS
    )
    limit = check_int(max_rows, lo=1, hi=MAX_ROWS, field="max_rows")

    ensure_covered(sym, "prices", allow_uncovered=deps.settings.allow_uncovered)  # guard 1
    recent = (today - window.end).days <= 1
    try:
        response = await deps.client.get(
            f"eod/{sym.full}",
            {"from": window.start.isoformat(), "to": window.end.isoformat(), "period": period, "order": "a"},
            endpoint="eod",
            cost=cost_for("eod"),
            ttl_s=(
                recent_bars_ttl_s(sym.exchange, now, cap_s=EOD_RECENT_TTL_S) if recent else EOD_HISTORIC_TTL_S
            ),
            max_age_s=EOD_RECENT_TTL_S if recent else EOD_HISTORIC_TTL_S,
            symbol=sym.full,
        )
    except GuardError as error:  # guard 2
        if error.code is ErrorCode.NOT_FOUND:
            raise not_found_with_hint(error, sym, "end-of-day prices") from None
        raise

    warnings: list[ToolWarning] = []
    if window.clamped and requested_end is not None:
        warnings.append(_clamped_eod_warning(requested_end, window.end))

    bars = normalize_eod_rows(response.data)  # guard 3
    inside = [bar for bar in bars if _within(bar, period, window.start, window.end)]  # guard 4
    outside = [bar for bar in bars if not _within(bar, period, window.start, window.end)]
    if outside:
        warnings.append(_trimmed_eod_warning(outside, window.start, window.end))

    if not inside:  # guard 5
        before_start = _day_before(window.start)
        latest_due = expected_latest_session(sym.exchange, now)
        # Only a session whose bar is already due can be missing: today's, before it is published, is not.
        if sessions_between(before_start, min(window.end, latest_due), sym.exchange) >= 1:
            raise _no_data_error(sym, window.start, window.end, response, outside)
        if sessions_between(before_start, window.end, sym.exchange) >= 1:
            warnings.append(_not_due_warning(sym, window.start, window.end, latest_due, now))
        else:
            warnings.append(
                ToolWarning.of(
                    WarningCode.EMPTY_RESULT,
                    f"The range {window.start.isoformat()} to {window.end.isoformat()} holds no trading day "
                    "(weekend days only), so an empty list is the correct answer and no data is missing.",
                    symbol=sym.full,
                )
            )

    fillers = [bar for bar in inside if is_filler_bar(bar)]  # guard 6
    kept = inside
    if fillers and not include_filler_bars:
        kept = [bar for bar in inside if not is_filler_bar(bar)]
        warnings.append(_filler_warning(fillers, nothing_left=not kept))

    real_days = [bar.day for bar in inside if not is_filler_bar(bar)]  # guard 7
    stale = _freshness_warning(
        sym, period, window.end, real_days, now=now, threshold=deps.settings.stale_sessions
    )
    if stale is not None:
        warnings.append(stale)

    # Guard 10 cuts the bars first, so that guards 8 and 9 judge the bars that are returned.
    shown = kept[-limit:]
    omitted = kept[:-limit]
    if shown:
        warnings.extend(spinoff_warnings(sym, shown[0].day))  # guard 8
        note = currency_note([sym])  # guard 9
        if note is not None:
            warnings.append(note)
    if omitted:
        warnings.append(_truncated_warning(len(kept), len(shown), limit, shown[0].day.isoformat(), "bars"))

    rows = apply_basis(shown, price_basis)
    notes = [PRICE_BASIS_NOTES[price_basis]]
    if price_basis == "adjusted":
        unadjusted = sum(1 for row in rows if row["close"] is None)
        if unadjusted:
            notes.append(f"{unadjusted} bar(s) have no adjusted_close, so their adjusted prices are null.")
    data = {
        "symbol": sym.full,
        "period": period,
        "price_basis": price_basis,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "first_date": shown[0].day.isoformat() if shown else None,
        "last_date": shown[-1].day.isoformat() if shown else None,
        "count": len(shown),
        "bars": rows,
    }
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- get_intraday_prices ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime
    requested_end: datetime | None
    """Set when the end asked for was in the future and was pulled back."""


@dataclass(frozen=True)
class _IntradayBar:
    """One intraday bar; ``moment`` is the instant EODHD stamps it with."""

    moment: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | float | None


def _next_step(moment: datetime) -> datetime:
    """The first quarter-hour boundary at or after ``moment``."""
    seconds = math.ceil(moment.timestamp() / INTRADAY_STEP_S) * INTRADAY_STEP_S
    return datetime.fromtimestamp(seconds, timezone.utc)


def _end_of_day(midnight: datetime) -> datetime:
    """The last second of the UTC day that begins at ``midnight``."""
    return midnight + timedelta(days=1, seconds=-1)


def _resolve_window(
    start: datetime | None,
    end: datetime | None,
    *,
    end_is_date: bool,
    now: datetime,
    interval: str,
) -> _Window:
    """Fill in and check an intraday window.

    ``end`` defaults to the next quarter hour after ``now``. A bare-date ``end`` (``end_is_date``) means the
    last second of that UTC day, so ``end="2026-10-30"`` includes the 30th; an ``end`` with a time is exact.
    A bare-date start is 00:00 UTC.
    """
    span = INTRADAY_SPANS[interval]
    limit = _next_step(now)
    asked, day_asked = end, None
    if end is not None and end_is_date:
        asked, day_asked = _end_of_day(end), end.date()
    pulled_back = asked is not None and asked > limit
    last = limit if asked is None or pulled_back else asked
    # Asking for data "through today" is fair, so only a date after today is announced as pulled back.
    announced = pulled_back and not (
        day_asked is not None and day_asked <= now.astimezone(timezone.utc).date()
    )
    if start is not None and start > now:
        raise _invalid(
            "start",
            iso_utc(start),
            "it is in the future, and no data exists after now",
            f"a time on or before {iso_utc(now)}",
        )
    # With no start the default span counts back from the end; a whole-day end counts from the next midnight.
    if start is None:
        whole_day = day_asked is not None and not pulled_back
        first = last + timedelta(seconds=1 if whole_day else 0) - span.default
    else:
        first = start
    if first >= last:
        raise _invalid(
            "start",
            iso_utc(first),
            f"it is not before the end {iso_utc(last)}",
            "a start time before the end time",
        )
    length = last - first
    if length > span.longest:
        raise _invalid(
            "start/end",
            f"{iso_utc(first)} to {iso_utc(last)}",
            f"the window spans {length.total_seconds() / 86400:.1f} days, more than the "
            f"{span.longest.days} allowed for {interval} bars",
            f"a window of at most {span.longest.days} days",
            hint="Ask for a shorter window, or make several calls.",
        )
    return _Window(start=first, end=last, requested_end=asked if announced else None)


def finite_number(value: object) -> int | float | None:
    """A finite number from a JSON value. Strings (including "NA"), booleans and null are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:  # an integer too large for a float
        return None


def _moment(row: object) -> datetime | None:
    """The instant of a row from its Unix ``timestamp``, or None when it has none that makes sense."""
    stamp = finite_number(row.get("timestamp")) if isinstance(row, dict) else None
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _read_intraday(rows: Any) -> tuple[list[_IntradayBar], int]:
    """Read EODHD's intraday rows into bars sorted by time, plus how many rows had to be skipped.

    A row needs a Unix ``timestamp`` and numeric open, high, low and close; a null ``volume`` is kept as
    None. EODHD sends rows with null prices for intervals without a trade, and those are skipped. When a
    timestamp repeats, the later row wins. Anything that is not a list, or a non-empty list in which no
    row has a readable timestamp, is BAD_RESPONSE.
    """
    if not isinstance(rows, list):
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            f"EODHD answered with a {type(rows).__name__} value where a list of intraday bars was expected.",
            hint="The API may have changed its response shape; the data cannot be trusted as it is.",
        )
    by_moment: dict[datetime, _IntradayBar] = {}
    stamped = skipped = 0
    for row in rows:
        moment = _moment(row)
        if moment is None:
            skipped += 1
            continue
        stamped += 1
        open_, high, low, close = (finite_number(row.get(name)) for name in ("open", "high", "low", "close"))
        if open_ is None or high is None or low is None or close is None:
            skipped += 1
            continue
        volume = finite_number(row.get("volume"))
        if isinstance(volume, float) and volume.is_integer():
            volume = int(volume)
        by_moment[moment] = _IntradayBar(moment, float(open_), float(high), float(low), float(close), volume)
    if rows and not stamped:
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            f"None of the {len(rows)} rows EODHD returned has a readable Unix timestamp.",
            hint="The API may have changed its response shape; the data cannot be trusted as it is.",
        )
    return [by_moment[moment] for moment in sorted(by_moment)], skipped


def _intraday_row(bar: _IntradayBar) -> dict[str, Any]:
    return {
        "datetime_utc": iso_utc(bar.moment),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _not_same_day_warning(
    symbol: Symbol, interval: str, today: date, tz_name: str, state: str, newest: _IntradayBar | None
) -> ToolWarning:
    status = "is open" if state == "open" else "has closed"
    last = (
        f"The newest bar returned is {iso_utc(newest.moment)}." if newest else "No bar was returned at all."
    )
    return ToolWarning.of(
        WarningCode.INTRADAY_NOT_SAME_DAY,
        f"The window includes today ({today.isoformat()}, {tz_name} time), the {symbol.exchange} session "
        f"{status}, and EODHD has no {interval} bar dated today yet. EODHD publishes intraday bars after "
        f"the session, never during it, and today's bars can lag the close by hours. {last} For a current "
        "price use get_quote (delayed 15 to 20 minutes), or end the window at the previous session. Market "
        "holidays are not modelled, so a holiday looks the same.",
        symbol=symbol.full,
        local_date=today.isoformat(),
        session=state,
        newest_bar=iso_utc(newest.moment) if newest else None,
    )


async def fetch_intraday_prices(
    deps: Deps,
    *,
    symbol: str,
    interval: Interval = "5m",
    start: str | None = None,
    end: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ToolResponse:
    """The logic of get_intraday_prices: check the input, call EODHD once, then run the guards."""
    sym = parse_symbol(symbol)
    now = deps.clock()
    window = _resolve_window(
        parse_datetime_utc(start, "start"),
        parse_datetime_utc(end, "end"),
        end_is_date=end is not None and iso_date_or_none(end.strip()) is not None,
        now=now,
        interval=interval,
    )
    limit = check_int(max_rows, lo=1, hi=MAX_ROWS, field="max_rows")

    ensure_covered(sym, "prices", allow_uncovered=deps.settings.allow_uncovered)
    market = market_for(sym.exchange)
    market_tz = ZoneInfo(market.tz)
    today = now.astimezone(market_tz).date()
    # "Today" matters only when the window reaches today's session: a window that ends before it opens (or
    # starts after it closes) holds no bar of today, however its UTC dates fall, and its answer is final.
    opens = datetime.combine(today, market.open, tzinfo=market_tz).astimezone(timezone.utc)
    closes = datetime.combine(today, market.close, tzinfo=market_tz).astimezone(timezone.utc)
    reaches_today = window.end >= opens and window.start <= closes
    try:
        response = await deps.client.get(
            f"intraday/{sym.full}",
            {
                "interval": interval,
                "from": int(window.start.timestamp()),
                "to": int(window.end.timestamp()),
            },
            endpoint="intraday",
            cost=cost_for("intraday"),
            ttl_s=INTRADAY_RECENT_TTL_S if reaches_today else INTRADAY_HISTORIC_TTL_S,
            symbol=sym.full,
        )
    except GuardError as error:
        if error.code is ErrorCode.NOT_FOUND:
            raise not_found_with_hint(error, sym, "intraday prices") from None
        raise

    warnings: list[ToolWarning] = []
    if window.requested_end is not None:
        warnings.append(
            ToolWarning.of(
                WarningCode.RANGE_CLAMPED,
                f"The end time {iso_utc(window.requested_end)} is in the future and no data exists after "
                f"now, so the window ends at {iso_utc(window.end)}: now, rounded up to the next quarter "
                "hour.",
                requested_end=iso_utc(window.requested_end),
                end=iso_utc(window.end),
            )
        )

    bars, skipped = _read_intraday(response.data)
    inside = [bar for bar in bars if window.start <= bar.moment <= window.end]
    if len(inside) < len(bars):
        outside = len(bars) - len(inside)
        warnings.append(
            ToolWarning.of(
                WarningCode.RANGE_TRIMMED,
                f"EODHD returned {outside} bar(s) outside the requested window {iso_utc(window.start)} to "
                f"{iso_utc(window.end)}; they were dropped.",
                dropped=outside,
            )
        )

    state = session_state(sym.exchange, now)
    if (
        reaches_today
        and state in ("open", "post_close")
        and not any(bar.moment.astimezone(market_tz).date() == today for bar in inside)
    ):
        newest = inside[-1] if inside else None
        warnings.append(_not_same_day_warning(sym, interval, today, market_tz.key, state, newest))

    if not inside:
        warnings.append(
            ToolWarning.of(
                WarningCode.NO_INTRADAY_DATA,
                f"EODHD returned no {interval} bars for {sym.full} between {iso_utc(window.start)} and "
                f"{iso_utc(window.end)}. This is not an error: intraday history depth varies by exchange "
                "and plan, and weekends and holidays have no bars.",
                symbol=sym.full,
                interval=interval,
                rows_returned=len(response.data),
            )
        )

    shown = inside[-limit:]
    if len(inside) > limit:
        warnings.append(_truncated_warning(len(inside), len(shown), limit, iso_utc(shown[0].moment), "bars"))

    notes = ["datetime_utc is the timestamp EODHD gives each bar, in UTC. Volume is as reported."]
    if skipped:
        notes.append(f"{skipped} row(s) without prices or a readable timestamp were skipped.")
    data = {
        "symbol": sym.full,
        "interval": interval,
        "start": iso_utc(window.start),
        "end": iso_utc(window.end),
        "first_datetime_utc": iso_utc(shown[0].moment) if shown else None,
        "last_datetime_utc": iso_utc(shown[-1].moment) if shown else None,
        "count": len(shown),
        "bars": [_intraday_row(bar) for bar in shown],
    }
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- registration -------------------------------------------------------------------------------------


def register(server: MCPServer, deps: Deps) -> None:
    """Register get_eod_prices and get_intraday_prices."""

    @server.tool(
        name="get_eod_prices",
        title="End-of-day prices",
        description=EOD_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_eod_prices(
        symbol: Annotated[str, Field(description=SYMBOL_HELP)],
        start: Annotated[
            str | None,
            Field(description="First day of the range, as YYYY-MM-DD. Defaults to 30 days before end."),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description=(
                    "Last day of the range, as YYYY-MM-DD. Defaults to today (UTC); a later date is clamped "
                    "to today."
                )
            ),
        ] = None,
        period: Annotated[
            Period, Field(description="Bar length: d (daily), w (weekly) or m (monthly).")
        ] = "d",
        price_basis: Annotated[
            PriceBasis,
            Field(
                description=(
                    "both returns close and adjusted_close; raw returns only the reported open, high, low "
                    "and close; adjusted scales open, high, low and close by adjusted_close / close, for "
                    "returns."
                )
            ),
        ] = "both",
        include_filler_bars: Annotated[
            bool,
            Field(
                description=(
                    "Keep holiday filler bars (zero volume, open = high = low = close). They are dropped "
                    "by default."
                )
            ),
        ] = False,
        max_rows: Annotated[int, Field(ge=1, le=MAX_ROWS, description=MAX_ROWS_HELP)] = DEFAULT_MAX_ROWS,
    ) -> ToolResponse:
        return await fetch_eod_prices(
            deps,
            symbol=symbol,
            start=start,
            end=end,
            period=period,
            price_basis=price_basis,
            include_filler_bars=include_filler_bars,
            max_rows=max_rows,
        )

    @server.tool(
        name="get_intraday_prices",
        title="Intraday prices",
        description=INTRADAY_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_intraday_prices(
        symbol: Annotated[str, Field(description=SYMBOL_HELP)],
        interval: Annotated[
            Interval,
            Field(
                description=(
                    "Bar length: 1m, 5m or 1h. The default window is 1 day for 1m, 3 days for 5m and 14 days "
                    "for 1h; the longest window is 120, 600 and 7200 days."
                )
            ),
        ] = "5m",
        start: Annotated[
            str | None,
            Field(
                description=(
                    "Start of the window: YYYY-MM-DD (00:00 UTC) or an ISO 8601 time such as "
                    "2026-10-28T13:30:00Z; a time without an offset is UTC. Defaults to the end minus the "
                    "default window for the interval."
                )
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description=(
                    "End of the window, in the same formats. A bare date means the end of that UTC day "
                    "(every bar up to 23:59:59), so end=2026-10-30 includes the 30th; a time is used "
                    "exactly. Defaults to now, rounded up to the next quarter hour so that repeated calls "
                    "share the cache; a later time is clamped to that."
                )
            ),
        ] = None,
        max_rows: Annotated[int, Field(ge=1, le=MAX_ROWS, description=MAX_ROWS_HELP)] = DEFAULT_MAX_ROWS,
    ) -> ToolResponse:
        return await fetch_intraday_prices(
            deps, symbol=symbol, interval=interval, start=start, end=end, max_rows=max_rows
        )
