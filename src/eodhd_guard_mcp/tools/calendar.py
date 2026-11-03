"""Tools: the earnings, dividend and split calendars, and the dividend and split histories.

The calendars are the cost trap in this part of the API. EODHD charges 1 call per symbol when an earnings
request names symbols, and returns nothing for Tokyo codes when it does. A whole-market request for a date
window costs 1 call whatever the window, so the tools ask for the window alone and filter by symbol and
exchange here. Windows look forward, so none of them is clamped to today.

``import datetime as dt`` is deliberate: ``get_dividend_calendar`` has a parameter named ``date``.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from eodhd_guard_mcp.costs import cost_for
from eodhd_guard_mcp.coverage import GAPS, CoverageGap, ensure_covered, find_gap
from eodhd_guard_mcp.envelope import ToolResponse, ToolWarning, WarningCode, build_response
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.markets import market_for
from eodhd_guard_mcp.tools import READ_ONLY, guarded
from eodhd_guard_mcp.validation import (
    Symbol,
    check_int,
    iso_date_or_none,
    parse_date,
    parse_exchange,
    parse_symbol,
    parse_symbols,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.client import ApiResponse
    from eodhd_guard_mcp.server import Deps

CALENDAR_TTL_S = 6 * 3600.0
HISTORY_TTL_S = 24 * 3600.0
MAX_WINDOW_DAYS = 90
EARNINGS_WINDOW_DAYS = 14
SPLITS_WINDOW_DAYS = 30
MAX_SYMBOLS = 100
MAX_ROWS = 1000
MAX_OFFSET = 1_000_000
CALENDAR_PLANS = "Fundamentals Data Feed or All-in-One"
HISTORY_PLANS = "EOD Historical Data or higher"
PLANS_BY_ENDPOINT = {
    "calendar/earnings": ("the calendar", CALENDAR_PLANS),
    "calendar/dividends": ("the calendar", CALENDAR_PLANS),
    "calendar/splits": ("the calendar", CALENDAR_PLANS),
    "div": ("dividend history", HISTORY_PLANS),
    "splits": ("split history", HISTORY_PLANS),
}
SPLIT_FACTOR_BAND = (0.8, 1.25)

_TIMING = {"BeforeMarket": "before_market", "AfterMarket": "after_market"}
_EPSILON = 1e-9

EARNINGS_DESCRIPTION = (
    "Earnings report dates in a window, with EPS actual, estimate and surprise once a company has "
    "reported. Costs 1 API call whatever the window or the number of symbols: the tool fetches the "
    "whole-market calendar once (cached for 6 hours) and filters it by `symbols` and `exchange` itself, "
    "because EODHD charges 1 call per symbol otherwise and returns nothing for Tokyo codes. The window "
    "defaults to the next 14 days and spans at most 90. Before/after-market flags were often wrong in "
    "checks, a missing row does not prove that a company does not report, Tokyo has dates only and the "
    "UAE has none."
)
DIVIDENDS_DESCRIPTION = (
    "Dividend history for one symbol: ex-date, declaration, record and payment dates and the amount per "
    "share, oldest first, with future ex-dates flagged as upcoming. Costs 1 API call, cached for 24 "
    "hours. `value` is adjusted for splits and `unadjusted_value` is the amount as declared; London lines "
    "are quoted in pence (GBX) while their dividends are often in pounds (GBP), which the tool flags. "
    "No dividends is a normal empty result, not an error."
)
DIVIDEND_CALENDAR_DESCRIPTION = (
    "The symbols that have a dividend dated on one day. Costs 1 API call, cached for 6 hours. The calendar "
    "carries only the symbol and the date, with no amounts: call `get_dividends` for a symbol to get "
    "them. Results are paged with `max_rows` and `offset`."
)
SPLITS_DESCRIPTION = (
    "Split history for one symbol, each ratio parsed into new shares, old shares and a factor (new "
    "divided by old: 4 is a 4-for-1 split, 0.1 a 1-for-10 reverse split). Costs 1 API call, cached for 24 "
    "hours. EODHD also lists spin-off and rights adjustments among the splits; ratios that are not whole "
    "numbers, or whose factor is close to 1 without being 1, are flagged ODD_SPLIT_RATIO."
)
SPLIT_CALENDAR_DESCRIPTION = (
    "Splits announced or executed in a window, for the whole market. Costs 1 API call whatever the window "
    "(cached for 6 hours); `symbols` and `exchange` are filtered here and cost nothing extra. The window "
    "defaults to the next 30 days and spans at most 90. Each row has old and new shares, the factor (new "
    "divided by old) and whether options exist."
)


def _invalid(
    field_name: str, received: object, problem: str, accepted: str, *, hint: str | None = None
) -> GuardError:
    """An ``INVALID_PARAMS`` error in the style of ``validation.py``: field, value, problem, accepted."""
    text = repr(received)
    shown = text if len(text) <= 60 else text[:57] + "..."
    return GuardError(
        ErrorCode.INVALID_PARAMS,
        f"{field_name}: received {shown}; {problem}. Accepted: {accepted}.",
        hint=hint,
        details={"field": field_name, "received": shown},
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    """A non-blank string, stripped; null, ``"NA"`` and anything that is not text are None."""
    if isinstance(value, str) and value.strip() not in ("", "NA"):
        return value.strip()
    return None


def _to_float(value: object) -> float | None:
    """A finite number from a JSON number or a numeric string; everything else, ``"NA"`` included, is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _whole_or_float(number: float) -> int | float:
    """4.0 as 4, so share counts read as counts."""
    return int(number) if number.is_integer() else number


def _days_after(day: dt.date, days: int) -> dt.date:
    try:
        return day + dt.timedelta(days=days)
    except OverflowError:
        return dt.date.max


def _symbol_of(code: str) -> str:
    return code.strip().upper()


def _suffix(code: str) -> str:
    return code.rpartition(".")[2]


# --- errors -----------------------------------------------------------------------------------------


def _explain(exc: GuardError, endpoint: str, symbol: Symbol | None) -> GuardError | None:
    """A clearer error for the two cases the client cannot word well, else None (re-raise as it is).

    A 404 for a symbol gets a way forward; a 403 names the plans that include the endpoint, which the
    client knows only for intraday and fundamentals.
    """
    if exc.code is ErrorCode.NOT_FOUND and symbol is not None:
        return GuardError(
            ErrorCode.NOT_FOUND,
            f"{symbol.full}: {exc.message}",
            hint=(
                "Check the code with search_symbols. Delisted lines are listed by "
                "list_exchange_symbols(delisted=true)."
            ),
            details=exc.details,
        )
    if exc.code is ErrorCode.PLAN_ERROR and endpoint in PLANS_BY_ENDPOINT:
        what, plans = PLANS_BY_ENDPOINT[endpoint]
        return GuardError(
            ErrorCode.PLAN_ERROR,
            f"The EODHD subscription does not include the '{endpoint}' endpoint (HTTP 403). "
            f"Plans that include {what}: {plans}.",
            hint=exc.hint,
            details=exc.details,
        )
    return None


async def _get(
    deps: Deps,
    path: str,
    params: Mapping[str, Any] | None,
    *,
    endpoint: str,
    ttl_s: float,
    symbol: Symbol | None = None,
) -> ApiResponse:
    try:
        return await deps.client.get(
            path,
            params,
            endpoint=endpoint,
            cost=cost_for(endpoint),
            ttl_s=ttl_s,
            symbol=symbol.full if symbol is not None else None,
        )
    except GuardError as exc:
        replacement = _explain(exc, endpoint, symbol)
        if replacement is None:
            raise
        raise replacement from None


def _bad_response(what: str, body: object) -> GuardError:
    return GuardError(
        ErrorCode.BAD_RESPONSE,
        f"EODHD's {what} answer is a {type(body).__name__} without the expected rows.",
        hint="This is usually temporary; retry in a minute.",
    )


# --- windows, filters and pages ---------------------------------------------------------------------


def _window(
    start: dt.date | None, end: dt.date | None, *, today: dt.date, default_days: int
) -> tuple[dt.date, dt.date]:
    """The first and last day to ask for. Unlike prices, a window may lie in the future or the past."""
    first = today if start is None else start
    last = _days_after(first, default_days) if end is None else end
    if last < first:
        raise _invalid(
            "start/end",
            f"{first.isoformat()} to {last.isoformat()}",
            "the end date is before the start date",
            "an end date on or after the start date",
        )
    span = (last - first).days
    if span > MAX_WINDOW_DAYS:
        raise _invalid(
            "start/end",
            f"{first.isoformat()} to {last.isoformat()}",
            f"the window spans {span} days, more than the {MAX_WINDOW_DAYS} allowed",
            f"a window of at most {MAX_WINDOW_DAYS} days",
            hint="Ask for a shorter window, or make several calls.",
        )
    return first, last


@dataclass(frozen=True)
class _Row:
    """A calendar row that can be filtered and sorted: the symbol, the day, and what the model sees."""

    symbol: str
    day: dt.date
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Selection:
    rows: list[_Row]
    """The rows that match, sorted by day and symbol."""
    found: frozenset[str]
    """Requested symbols that have at least one row in the window, whatever the exchange filter says."""
    outside: int
    """Rows EODHD returned outside the window we asked for."""


def _select(
    rows: Sequence[_Row],
    *,
    start: dt.date,
    end: dt.date,
    symbols: Sequence[Symbol],
    exchange: str | None,
    allow_uncovered: bool,
) -> _Selection:
    """Keep the rows in the window that match the symbols and the exchange (both, when both are given).

    A Tokyo symbol or exchange matches the calendar's ``.TSE`` rows under any of Tokyo's aliases.
    """
    in_window = [row for row in rows if start <= row.day <= end]
    # Row code -> the requested symbols it answers. Tokyo's aliases share a row, so one row can answer
    # several symbols.
    keys: dict[str, set[str]] = {}
    for symbol in symbols:
        keys.setdefault(symbol.full, set()).add(symbol.full)
        gap = None if allow_uncovered else find_gap(symbol.exchange)
        if gap is not None and gap.calendar_dates:
            for alias in gap.suffixes:
                keys.setdefault(f"{symbol.code}.{alias}", set()).add(symbol.full)
    by_symbol = [row for row in in_window if row.symbol in keys] if symbols else in_window
    found = frozenset(name for row in by_symbol for name in keys[row.symbol]) if symbols else frozenset()
    suffixes: frozenset[str] | None = None
    if exchange is not None:
        gap = None if allow_uncovered else find_gap(exchange)
        suffixes = gap.suffixes if gap is not None and gap.calendar_dates else frozenset({exchange})
    matched = [row for row in by_symbol if suffixes is None or _suffix(row.symbol) in suffixes]
    matched.sort(key=lambda row: (row.day, row.symbol))
    return _Selection(rows=matched, found=found, outside=len(rows) - len(in_window))


def _coverage_warnings(
    symbols: Sequence[Symbol],
    exchange: str | None,
    matched: Sequence[_Row],
    *,
    allow_uncovered: bool,
    dates_only: bool,
) -> tuple[list[ToolWarning], frozenset[str]]:
    """Warnings for markets EODHD does not cover, and the requested symbols that can have no row.

    With ``dates_only`` a market whose calendar still has rows (Tokyo) is PARTIAL_COVERAGE; every other
    gap, and Tokyo for calendars that have no Tokyo rows, is NOT_COVERED. The escape hatch
    ``EODHD_GUARD_ALLOW_UNCOVERED`` turns all of it off.
    """
    if allow_uncovered:
        return [], frozenset()
    gaps = {gap.key: gap for gap in GAPS}
    missing: dict[str, list[str]] = {}
    partial: dict[str, list[str]] = {}
    silent: set[str] = set()
    rows_by_gap: dict[str, int] = {}

    def record(gap: CoverageGap, subject: str, *, requested: bool) -> None:
        if gap.calendar_dates and dates_only:
            partial.setdefault(gap.key, [])
            if requested:
                partial[gap.key].append(subject)
        else:
            missing.setdefault(gap.key, []).append(subject)
            if requested:
                silent.add(subject)

    for symbol in symbols:
        gap = find_gap(symbol.exchange)
        if gap is not None:
            record(gap, symbol.full, requested=True)
    exchange_gap = find_gap(exchange) if exchange is not None else None
    if exchange is not None and exchange_gap is not None:
        record(exchange_gap, f"exchange {exchange}", requested=False)
    for row in matched:
        gap = find_gap(_suffix(row.symbol))
        if gap is not None and gap.calendar_dates and dates_only:
            partial.setdefault(gap.key, [])
            rows_by_gap[gap.key] = rows_by_gap.get(gap.key, 0) + 1

    warnings: list[ToolWarning] = []
    for key, subjects in missing.items():
        gap = gaps[key]
        warnings.append(
            ToolWarning.of(
                WarningCode.NOT_COVERED,
                f"{', '.join(subjects)}: {gap.message} An empty result for this market says nothing about "
                f"its calendar. Alternatives: {gap.alternatives}",
                market=gap.market,
                subjects=subjects,
            )
        )
    for key, subjects in partial.items():
        gap = gaps[key]
        warnings.append(
            ToolWarning.of(
                WarningCode.PARTIAL_COVERAGE,
                f"{gap.market} rows are report dates only. {gap.message}",
                market=gap.market,
                symbols=subjects,
                rows=rows_by_gap.get(key, 0),
            )
        )
    return warnings, frozenset(silent)


def _page_warning(total: int, offset: int, shown: int) -> ToolWarning | None:
    """TRUNCATED when rows remain after the page; EMPTY_RESULT when the page has none to show."""
    if shown == 0:
        problem = (
            "No rows match the window and filters."
            if total == 0
            else f"offset {offset} is past the last of the {total} matching rows."
        )
        return ToolWarning.of(WarningCode.EMPTY_RESULT, problem, total=total, offset=offset)
    if offset + shown >= total:
        return None
    return ToolWarning.of(
        WarningCode.TRUNCATED,
        f"{total} rows match; rows {offset + 1} to {offset + shown} are returned. Pass "
        f"offset={offset + shown} for the next page, or narrow the window, symbols or exchange.",
        total=total,
        offset=offset,
        returned=shown,
        next_offset=offset + shown,
    )


def _outside_warning(count: int, start: dt.date, end: dt.date) -> ToolWarning:
    return ToolWarning.of(
        WarningCode.RANGE_TRIMMED,
        f"EODHD returned {count} rows outside {start.isoformat()} to {end.isoformat()}; they were dropped.",
        dropped=count,
    )


def _symbols(raw: Sequence[str] | str | None) -> list[Symbol]:
    """The symbols to filter by; None and an empty list both mean no filter."""
    if raw is None or (not isinstance(raw, str) and not raw):
        return []
    return parse_symbols(raw, max_n=MAX_SYMBOLS)


def _skipped_note(count: int, what: str = "symbol or date") -> list[str]:
    """A note when rows could not be read and were left out; nothing when all were."""
    if count == 0:
        return []
    if count == 1:
        return [f"1 row without a readable {what} was skipped."]
    return [f"{count} rows without a readable {what} were skipped."]


# --- earnings calendar ------------------------------------------------------------------------------


def _read_earning(raw: object) -> _Row | None:
    if not isinstance(raw, dict):
        return None
    code = _text(raw.get("code"))
    day = iso_date_or_none(raw.get("report_date"))
    if code is None or day is None:
        return None
    symbol = _symbol_of(code)
    actual = _to_float(raw.get("actual"))
    estimate = _to_float(raw.get("estimate"))
    # EODHD sends difference 0 when it has no actual or no estimate, which would read as "in line".
    has_both = actual is not None and estimate is not None
    period_end = iso_date_or_none(raw.get("date"))
    timing = _text(raw.get("before_after_market"))
    return _Row(
        symbol=symbol,
        day=day,
        payload={
            "symbol": symbol,
            "report_date": day.isoformat(),
            "period_end": period_end.isoformat() if period_end else None,
            "timing": _TIMING.get(timing, timing) if timing else None,
            "currency": _text(raw.get("currency")),
            "eps_actual": actual,
            "eps_estimate": estimate,
            "surprise": _to_float(raw.get("difference")) if has_both else None,
            "surprise_pct": _to_float(raw.get("percent")) if has_both else None,
        },
    )


def _earnings_rows(body: Any) -> tuple[list[_Row], int]:
    if not isinstance(body, dict) or not isinstance(body.get("earnings"), list):
        raise _bad_response("earnings calendar", body)
    rows = [_read_earning(raw) for raw in body["earnings"]]
    return [row for row in rows if row is not None], rows.count(None)


async def earnings_calendar(
    deps: Deps,
    start_text: str | None,
    end_text: str | None,
    symbols_raw: Sequence[str] | str | None,
    exchange_text: str | None,
    max_rows: int,
    offset: int,
) -> ToolResponse:
    """Validate, fetch the whole-market window once, then filter and page it here."""
    start = parse_date(start_text, "start")
    end = parse_date(end_text, "end")
    first, last = _window(start, end, today=deps.clock().date(), default_days=EARNINGS_WINDOW_DAYS)
    symbols = _symbols(symbols_raw)
    exchange = parse_exchange(exchange_text) if exchange_text is not None else None
    limit = check_int(max_rows, lo=1, hi=MAX_ROWS, field="max_rows")
    skip = check_int(offset, lo=0, hi=MAX_OFFSET, field="offset")

    # Never `symbols`: that is billed per symbol, and answers nothing for Tokyo codes.
    response = await _get(
        deps,
        "calendar/earnings",
        {"from": first.isoformat(), "to": last.isoformat()},
        endpoint="calendar/earnings",
        ttl_s=CALENDAR_TTL_S,
    )
    rows, unreadable = _earnings_rows(response.data)
    allow = deps.settings.allow_uncovered
    chosen = _select(rows, start=first, end=last, symbols=symbols, exchange=exchange, allow_uncovered=allow)
    total = len(chosen.rows)
    page = chosen.rows[skip : skip + limit]

    warnings, silent = _coverage_warnings(
        symbols, exchange, chosen.rows, allow_uncovered=allow, dates_only=True
    )
    if chosen.outside:
        warnings.append(_outside_warning(chosen.outside, first, last))
    absent = [s.full for s in symbols if s.full not in silent and s.full not in chosen.found]
    if absent:
        warnings.append(
            ToolWarning.of(
                WarningCode.NO_CALENDAR_ROWS,
                f"No calendar row for {', '.join(absent)} between {first.isoformat()} and "
                f"{last.isoformat()}. A missing row is not evidence that the company does not report: the "
                "calendar leaves some issuers out. Check the company's investor-relations page.",
                symbols=absent,
                start=first.isoformat(),
                end=last.isoformat(),
            )
        )
    page_warning = _page_warning(total, skip, len(page))
    if page_warning is not None:
        warnings.append(page_warning)
    warnings.append(
        ToolWarning.of(
            WarningCode.CALENDAR_TIMING_UNRELIABLE,
            "The before/after-market flags were often wrong in checks (timing may be null or wrong), and "
            "report dates move. Treat both as a lead and confirm with the company before relying on them.",
        )
    )

    data: dict[str, Any] = {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "total": total,
        "offset": skip,
        "count": len(page),
        "next_offset": skip + len(page) if skip + len(page) < total else None,
        "earnings": [row.payload for row in page],
    }
    if symbols:
        data["symbols"] = [s.full for s in symbols]
    if exchange is not None:
        data["exchange"] = exchange
    notes = [
        f"One whole-market request (1 call for any window) was filtered here: {len(rows)} rows in the "
        f"window, {total} match. A request naming symbols would cost 1 call per symbol and returns "
        "nothing for Tokyo codes.",
        "surprise and surprise_pct are given only when both eps_actual and eps_estimate exist; EODHD sends "
        "a difference of 0 otherwise.",
        *_skipped_note(unreadable),
    ]
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- dividends --------------------------------------------------------------------------------------


def _read_dividend(raw: object, today: dt.date) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    ex_date = iso_date_or_none(raw.get("date"))
    if ex_date is None:
        return None

    def iso(name: str) -> str | None:
        day = iso_date_or_none(raw.get(name))
        return day.isoformat() if day else None

    return {
        "ex_date": ex_date.isoformat(),
        "declaration_date": iso("declarationDate"),
        "record_date": iso("recordDate"),
        "payment_date": iso("paymentDate"),
        "period": _text(raw.get("period")),
        "value": _to_float(raw.get("value")),
        "unadjusted_value": _to_float(raw.get("unadjustedValue")),
        "currency": _text(raw.get("currency")),
        "upcoming": ex_date > today,
    }


def _dividend_currency_warning(symbol: Symbol, rows: Sequence[Mapping[str, Any]]) -> ToolWarning | None:
    """CURRENCY_MISMATCH when a London line's dividends are not in pence; CURRENCY_NOTE when they are."""
    quote = market_for(symbol.exchange).currency_hint
    if quote is None or not rows:
        return None
    paid = sorted({row["currency"] for row in rows if row["currency"]})
    differing = [currency for currency in paid if currency.upper() != quote]
    if not differing:
        return ToolWarning.of(
            WarningCode.CURRENCY_NOTE,
            f"{symbol.full} is quoted in {quote} (pence), and its dividends are in {quote} too.",
            quote_currency=quote,
        )
    factor = " For GBP against GBX the factor is 100." if "GBP" in differing else ""
    return ToolWarning.of(
        WarningCode.CURRENCY_MISMATCH,
        f"{symbol.full} is quoted in {quote} (pence), but its dividends are in {', '.join(differing)}. "
        f"Convert one side before comparing a dividend with the share price (yield, payout).{factor}",
        quote_currency=quote,
        dividend_currencies=paid,
    )


async def dividends(
    deps: Deps, symbol_text: str, start_text: str | None, end_text: str | None, max_rows: int
) -> ToolResponse:
    """Validate, fetch the dividend history, and answer from the newest ``max_rows`` rows in the window."""
    symbol = parse_symbol(symbol_text)
    ensure_covered(symbol, "dividends", allow_uncovered=deps.settings.allow_uncovered)
    start = parse_date(start_text, "start")
    end = parse_date(end_text, "end")
    if start is not None and end is not None and start > end:
        raise _invalid(
            "start/end",
            f"{start.isoformat()} to {end.isoformat()}",
            "the start date is after the end date",
            "a start date on or before the end date",
        )
    limit = check_int(max_rows, lo=1, hi=MAX_ROWS, field="max_rows")

    params = {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None}
    response = await _get(
        deps, f"div/{symbol.full}", params, endpoint="div", ttl_s=HISTORY_TTL_S, symbol=symbol
    )
    if not isinstance(response.data, list):
        raise _bad_response("dividends", response.data)
    today = deps.clock().date()
    parsed = [_read_dividend(raw, today) for raw in response.data]
    readable = [row for row in parsed if row is not None]
    inside = [
        row
        for row in readable
        if (start is None or row["ex_date"] >= start.isoformat())
        and (end is None or row["ex_date"] <= end.isoformat())
    ]
    inside.sort(key=lambda row: row["ex_date"])
    shown = inside[-limit:]

    warnings: list[ToolWarning] = []
    if len(readable) > len(inside):
        warnings.append(
            ToolWarning.of(
                WarningCode.RANGE_TRIMMED,
                f"EODHD returned {len(readable) - len(inside)} dividends outside the requested dates; "
                "they were dropped.",
                dropped=len(readable) - len(inside),
            )
        )
    if not inside:
        warnings.append(
            ToolWarning.of(
                WarningCode.EMPTY_RESULT,
                f"No dividends for {symbol.full}{' in the window' if start or end else ''}. That is a normal "
                "answer for a company that does not pay one, or did not in this period.",
            )
        )
    elif len(inside) > len(shown):
        warnings.append(
            ToolWarning.of(
                WarningCode.TRUNCATED,
                f"{len(inside)} dividends match; the newest {len(shown)} are returned and the oldest "
                f"{len(inside) - len(shown)} are left out. Use start and end to see another period.",
                total=len(inside),
                returned=len(shown),
            )
        )
    currency_warning = _dividend_currency_warning(symbol, shown)
    if currency_warning is not None:
        warnings.append(currency_warning)

    data = {
        "symbol": symbol.full,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "total": len(inside),
        "count": len(shown),
        "dividends": shown,
    }
    notes = [
        "value is adjusted for splits; unadjusted_value is the amount as declared at the time. ex_date is "
        "the first day the shares trade without the dividend. A date EODHD does not have is null.",
        *_skipped_note(len(parsed) - len(readable), "ex-date"),
    ]
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- dividend calendar ------------------------------------------------------------------------------


def _read_dividend_day(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    symbol = _text(raw.get("symbol"))
    when = iso_date_or_none(raw.get("date"))
    if symbol is None or when is None:
        return None
    return {"symbol": _symbol_of(symbol), "date": when.isoformat()}


async def dividend_calendar(deps: Deps, date_text: str, max_rows: int, offset: int) -> ToolResponse:
    """Validate, fetch one page of the symbols with a dividend dated on one day."""
    day = parse_date(date_text, "date")
    if day is None:
        raise _invalid("date", date_text, "a date is required", "a calendar date written YYYY-MM-DD")
    limit = check_int(max_rows, lo=1, hi=MAX_ROWS, field="max_rows")
    skip = check_int(offset, lo=0, hi=MAX_OFFSET, field="offset")

    response = await _get(
        deps,
        "calendar/dividends",
        {"filter[date_eq]": day.isoformat(), "page[limit]": limit, "page[offset]": skip},
        endpoint="calendar/dividends",
        ttl_s=CALENDAR_TTL_S,
    )
    body = response.data
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise _bad_response("dividend calendar", body)
    fetched = body["data"]
    parsed = [_read_dividend_day(raw) for raw in fetched]
    rows = [row for row in parsed if row is not None]
    on_day = [row for row in rows if row["date"] == day.isoformat()]

    # The page we got decides whether more follow; EODHD's own total is used when it gives one.
    reported = _mapping(body.get("meta")).get("total")
    total = reported if isinstance(reported, int) and not isinstance(reported, bool) else None
    next_offset = skip + len(fetched)
    more = bool(_mapping(body.get("links")).get("next")) or (total is not None and next_offset < total)
    if total is None and not more:
        total = next_offset

    warnings: list[ToolWarning] = []
    if len(on_day) < len(rows):
        warnings.append(_outside_warning(len(rows) - len(on_day), day, day))
    if not fetched:
        problem = (
            f"No dividend is dated {day.isoformat()}."
            if skip == 0
            else f"offset {skip} is past the last dividend dated {day.isoformat()}."
        )
        warnings.append(ToolWarning.of(WarningCode.EMPTY_RESULT, problem, total=total, offset=skip))
    elif more:
        how_many = f"{total} dividends are" if total is not None else "More dividends are"
        warnings.append(
            ToolWarning.of(
                WarningCode.TRUNCATED,
                f"{how_many} dated {day.isoformat()}; rows {skip + 1} to {next_offset} are returned. Pass "
                f"offset={next_offset} for the next page.",
                total=total,
                offset=skip,
                returned=len(fetched),
                next_offset=next_offset,
            )
        )

    data = {
        "date": day.isoformat(),
        "total": total,
        "offset": skip,
        "count": len(on_day),
        "next_offset": next_offset if more else None,
        "dividends": on_day,
    }
    notes = [
        "The dividend calendar carries only the symbol and the date, with no amounts. Call get_dividends "
        "for a symbol to get the amount, currency and payment dates.",
        *_skipped_note(len(parsed) - len(rows)),
    ]
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- splits -----------------------------------------------------------------------------------------


def _format_shares(number: float) -> str:
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _is_odd_ratio(new: float, old: float) -> bool:
    """A ratio that looks like a spin-off or rights adjustment: fractional, or a factor near 1 but not 1."""
    factor = new / old

    def whole(number: float) -> bool:
        return abs(number - round(number)) < _EPSILON

    low, high = SPLIT_FACTOR_BAND
    near_one = low - _EPSILON <= factor <= high + _EPSILON and abs(factor - 1) > _EPSILON
    return not (whole(new) and whole(old)) or near_one


def _read_split(raw: object) -> tuple[dt.date, float, float] | None:
    """``(day, new, old)`` from ``{"date": ..., "split": "4.000000/1.000000"}``; None when unreadable."""
    if not isinstance(raw, dict):
        return None
    day = iso_date_or_none(raw.get("date"))
    text = raw.get("split")
    if day is None or not isinstance(text, str):
        return None
    new_text, separator, old_text = text.partition("/")
    new, old = _to_float(new_text), _to_float(old_text)
    if not separator or new is None or old is None or new <= 0 or old <= 0:
        return None
    return day, new, old


async def splits(deps: Deps, symbol_text: str, start_text: str | None, end_text: str | None) -> ToolResponse:
    """Validate, fetch the whole split history (a few rows) and filter it by date here."""
    symbol = parse_symbol(symbol_text)
    ensure_covered(symbol, "splits", allow_uncovered=deps.settings.allow_uncovered)
    start = parse_date(start_text, "start")
    end = parse_date(end_text, "end")
    if start is not None and end is not None and start > end:
        raise _invalid(
            "start/end",
            f"{start.isoformat()} to {end.isoformat()}",
            "the start date is after the end date",
            "a start date on or before the end date",
        )

    # No dates are sent: the list is short, so one cache entry per symbol serves every window.
    response = await _get(
        deps, f"splits/{symbol.full}", None, endpoint="splits", ttl_s=HISTORY_TTL_S, symbol=symbol
    )
    if not isinstance(response.data, list):
        raise _bad_response("splits", response.data)
    parsed = [_read_split(raw) for raw in response.data]
    readable = [item for item in parsed if item is not None]
    if response.data and not readable:
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            f"{symbol.full}: none of the {len(response.data)} split rows has a readable date and "
            '"new/old" ratio.',
            hint="This is usually temporary; retry in a minute.",
        )
    selected = sorted(
        (
            item
            for item in readable
            if (start is None or item[0] >= start) and (end is None or item[0] <= end)
        ),
        key=lambda item: item[0],
    )
    rows = [
        {
            "date": day.isoformat(),
            "ratio": f"{_format_shares(new)}:{_format_shares(old)}",
            "new_shares": _whole_or_float(new),
            "old_shares": _whole_or_float(old),
            "factor": round(new / old, 6),
        }
        for day, new, old in selected
    ]
    odd = [row for row, (_, new, old) in zip(rows, selected, strict=True) if _is_odd_ratio(new, old)]

    warnings: list[ToolWarning] = []
    if odd:
        warnings.append(
            ToolWarning.of(
                WarningCode.ODD_SPLIT_RATIO,
                f"{len(odd)} of these ratios look like a spin-off or rights adjustment, not a split: "
                + ", ".join(f"{row['date']} ({row['ratio']})" for row in odd)
                + ". EODHD lists some such corporate actions among the splits, so check before treating "
                "them as a change in share count.",
                ratios=[{"date": row["date"], "ratio": row["ratio"], "factor": row["factor"]} for row in odd],
            )
        )
    if not rows:
        warnings.append(
            ToolWarning.of(
                WarningCode.EMPTY_RESULT,
                f"No splits for {symbol.full} in the window. That is a normal answer for most companies.",
            )
        )
    data = {
        "symbol": symbol.full,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "count": len(rows),
        "splits": rows,
    }
    notes = [
        "factor is new shares divided by old shares: 4 means a 4-for-1 split, 0.1 a 1-for-10 reverse split.",
        *_skipped_note(len(parsed) - len(readable), 'date or "new/old" ratio'),
    ]
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- split calendar ---------------------------------------------------------------------------------


def _optionable(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _text(value)
    if text is None:
        return None
    return {"Y": True, "YES": True, "TRUE": True, "N": False, "NO": False, "FALSE": False}.get(text.upper())


def _read_split_row(raw: object) -> _Row | None:
    if not isinstance(raw, dict):
        return None
    code = _text(raw.get("code"))
    day = iso_date_or_none(raw.get("split_date"))
    if code is None or day is None:
        return None
    symbol = _symbol_of(code)
    old, new = _to_float(raw.get("old_shares")), _to_float(raw.get("new_shares"))
    factor = round(new / old, 6) if old is not None and new is not None and old > 0 else None
    return _Row(
        symbol=symbol,
        day=day,
        payload={
            "symbol": symbol,
            "split_date": day.isoformat(),
            "old_shares": _whole_or_float(old) if old is not None else None,
            "new_shares": _whole_or_float(new) if new is not None else None,
            "factor": factor,
            "optionable": _optionable(raw.get("optionable")),
        },
    )


async def split_calendar(
    deps: Deps,
    start_text: str | None,
    end_text: str | None,
    symbols_raw: Sequence[str] | str | None,
    exchange_text: str | None,
    max_rows: int,
    offset: int,
) -> ToolResponse:
    """Validate, fetch the whole-market split window once, then filter and page it here."""
    start = parse_date(start_text, "start")
    end = parse_date(end_text, "end")
    first, last = _window(start, end, today=deps.clock().date(), default_days=SPLITS_WINDOW_DAYS)
    symbols = _symbols(symbols_raw)
    exchange = parse_exchange(exchange_text) if exchange_text is not None else None
    limit = check_int(max_rows, lo=1, hi=MAX_ROWS, field="max_rows")
    skip = check_int(offset, lo=0, hi=MAX_OFFSET, field="offset")

    response = await _get(
        deps,
        "calendar/splits",
        {"from": first.isoformat(), "to": last.isoformat()},
        endpoint="calendar/splits",
        ttl_s=CALENDAR_TTL_S,
    )
    body = response.data
    if not isinstance(body, dict) or not isinstance(body.get("splits"), list):
        raise _bad_response("split calendar", body)
    parsed = [_read_split_row(raw) for raw in body["splits"]]
    rows = [row for row in parsed if row is not None]
    allow = deps.settings.allow_uncovered
    chosen = _select(rows, start=first, end=last, symbols=symbols, exchange=exchange, allow_uncovered=allow)
    total = len(chosen.rows)
    page = chosen.rows[skip : skip + limit]

    warnings, _ = _coverage_warnings(symbols, exchange, [], allow_uncovered=allow, dates_only=False)
    if chosen.outside:
        warnings.append(_outside_warning(chosen.outside, first, last))
    page_warning = _page_warning(total, skip, len(page))
    if page_warning is not None:
        warnings.append(page_warning)

    data: dict[str, Any] = {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "total": total,
        "offset": skip,
        "count": len(page),
        "next_offset": skip + len(page) if skip + len(page) < total else None,
        "splits": [row.payload for row in page],
    }
    if symbols:
        data["symbols"] = [s.full for s in symbols]
    if exchange is not None:
        data["exchange"] = exchange
    notes = [
        f"One whole-market request (any window) was filtered here: {len(rows)} rows in the window, "
        f"{total} match.",
        "factor is new shares divided by old shares: 4 means a 4-for-1 split, 0.1 a 1-for-10 reverse split.",
        *_skipped_note(len(parsed) - len(rows)),
    ]
    return build_response(data, [response], warnings=warnings, notes=notes)


# --- registration -----------------------------------------------------------------------------------


def register(server: MCPServer, deps: Deps) -> None:
    """Register the five calendar and corporate-action tools."""

    @server.tool(
        name="get_earnings_calendar",
        title="Earnings calendar",
        description=EARNINGS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_earnings_calendar(
        start: Annotated[
            str | None,
            Field(
                description=(
                    "First report date, YYYY-MM-DD. Default: today (UTC). May be in the past, to see "
                    "reported results."
                )
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description=(
                    "Last report date, YYYY-MM-DD. Default: start plus 14 days. The window spans at most "
                    "90 days."
                )
            ),
        ] = None,
        symbols: Annotated[
            list[str] | str | None,
            Field(
                description=(
                    "Only these symbols, CODE.EXCHANGE, at most 100, as a list or a comma-separated "
                    "string. Filtered here: it does not change the cost of 1 call."
                )
            ),
        ] = None,
        exchange: Annotated[
            str | None,
            Field(
                description=(
                    "Only rows on this exchange code, for example US, LSE, SW or NSE. With `symbols`, a "
                    "row must match both."
                )
            ),
        ] = None,
        max_rows: Annotated[int, Field(description="Rows per page, 1 to 1000. Default 200.")] = 200,
        offset: Annotated[int, Field(description="Rows to skip, to read the next page. Default 0.")] = 0,
    ) -> ToolResponse:
        return await earnings_calendar(deps, start, end, symbols, exchange, max_rows, offset)

    @server.tool(
        name="get_dividends",
        title="Dividend history",
        description=DIVIDENDS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_dividends(
        symbol: Annotated[
            str, Field(description="The line as CODE.EXCHANGE, for example AAPL.US, VOD.LSE or NESN.SW.")
        ],
        start: Annotated[
            str | None,
            Field(description="First ex-date to include, YYYY-MM-DD. Default: the full history."),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description="Last ex-date to include, YYYY-MM-DD. Default: no limit, so declared future "
                "dividends are included."
            ),
        ] = None,
        max_rows: Annotated[
            int, Field(description="Most rows to return, 1 to 1000; the newest are kept. Default 100.")
        ] = 100,
    ) -> ToolResponse:
        return await dividends(deps, symbol, start, end, max_rows)

    @server.tool(
        name="get_dividend_calendar",
        title="Dividend calendar",
        description=DIVIDEND_CALENDAR_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_dividend_calendar(
        date: Annotated[str, Field(description="The day to list, YYYY-MM-DD.")],
        max_rows: Annotated[int, Field(description="Rows per page, 1 to 1000. Default 200.")] = 200,
        offset: Annotated[int, Field(description="Rows to skip, to read the next page. Default 0.")] = 0,
    ) -> ToolResponse:
        return await dividend_calendar(deps, date, max_rows, offset)

    @server.tool(
        name="get_splits",
        title="Split history",
        description=SPLITS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_splits(
        symbol: Annotated[
            str, Field(description="The line as CODE.EXCHANGE, for example AAPL.US, NVDA.US or NESN.SW.")
        ],
        start: Annotated[
            str | None, Field(description="First split date to include, YYYY-MM-DD. Default: all.")
        ] = None,
        end: Annotated[
            str | None, Field(description="Last split date to include, YYYY-MM-DD. Default: all.")
        ] = None,
    ) -> ToolResponse:
        return await splits(deps, symbol, start, end)

    @server.tool(
        name="get_split_calendar",
        title="Split calendar",
        description=SPLIT_CALENDAR_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_split_calendar(
        start: Annotated[
            str | None, Field(description="First split date, YYYY-MM-DD. Default: today (UTC).")
        ] = None,
        end: Annotated[
            str | None,
            Field(description="Last split date, YYYY-MM-DD. Default: start plus 30 days. At most 90 days."),
        ] = None,
        symbols: Annotated[
            list[str] | str | None,
            Field(
                description="Only these symbols, CODE.EXCHANGE, at most 100, as a list or a comma-separated "
                "string. Filtered here, at no extra cost."
            ),
        ] = None,
        exchange: Annotated[
            str | None,
            Field(description="Only rows on this exchange code, for example US, LSE or NSE."),
        ] = None,
        max_rows: Annotated[int, Field(description="Rows per page, 1 to 1000. Default 200.")] = 200,
        offset: Annotated[int, Field(description="Rows to skip, to read the next page. Default 0.")] = 0,
    ) -> ToolResponse:
        return await split_calendar(deps, start, end, symbols, exchange, max_rows, offset)
