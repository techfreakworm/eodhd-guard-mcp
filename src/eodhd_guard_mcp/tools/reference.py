"""Tools: exchanges, symbol lists, search and symbol health.

``check_symbol`` answers EODHD's worst silent failure: a code that was renamed or delisted keeps
answering HTTP 200 with frozen data. The other three tools find codes, and mark the ones that look dead.

The listing endpoints return whole lists, so filtering and paging happen here, on one cached answer:
asking for a second page costs nothing.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from pydantic import Field

from eodhd_guard_mcp.costs import cost_for
from eodhd_guard_mcp.coverage import GAPS, CoverageGap, ensure_covered, ensure_exchange_covered, find_gap
from eodhd_guard_mcp.envelope import ToolResponse, ToolWarning, WarningCode, build_response
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.freshness import Freshness, assess, stale_warning
from eodhd_guard_mcp.markets import expected_latest_session
from eodhd_guard_mcp.redact import clip, redact
from eodhd_guard_mcp.semantics import is_filler_bar, normalize_eod_rows
from eodhd_guard_mcp.tools import READ_ONLY, guarded
from eodhd_guard_mcp.tools.prices import recent_bars_ttl_s
from eodhd_guard_mcp.validation import Symbol, iso_date_or_none, parse_exchange, parse_symbol

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.client import ApiResponse
    from eodhd_guard_mcp.server import Deps

EXCHANGES_TTL_S = 7 * 24 * 3600.0
SYMBOLS_TTL_S = 24 * 3600.0
SEARCH_TTL_S = 24 * 3600.0
CHECK_BARS_TTL_S = 3600.0
"""A health check should not trust a stale verdict for long, so its bars are cached for an hour."""
QUOTE_TTL_S = 60.0

CHECK_WINDOW_DAYS = 14
"""How far back ``check_symbol`` looks for bars."""
SEARCH_POOL = 50
"""Rows asked for when ``check_symbol`` searches. They are filtered here, and a call costs the same."""
MAX_CANDIDATES = 5
MAX_TYPES_SHOWN = 8
MAX_SYMBOLS_IN_WARNING = 10
IDENTIFIER_CHARS = 24
"""A symbol the vendor gave is cut to this length where a sentence names it; the data holds it whole."""
TYPE_CHARS = 30
"""The same for an instrument type named in a sentence."""

SearchType = Literal["all", "stock", "etf", "fund", "bonds", "index", "crypto"]

LIST_EXCHANGES_DESCRIPTION = (
    "List the exchanges EODHD knows, with code, country, currency and a coverage note. Costs 1 API call, "
    "or 0 when cached (the list is kept for 7 days). Tokyo and the UAE have no usable prices and are "
    "missing or empty, so being listed is not proof of coverage; the result carries PARTIAL_COVERAGE. An "
    "exchange code is the suffix of a symbol, such as the SW in NESN.SW."
)
LIST_EXCHANGE_SYMBOLS_DESCRIPTION = (
    "List the symbols on one exchange, active or delisted, filtered and paged here. Costs 1 API call for "
    "the whole list (cached 24 hours), so more pages and other filters on the same exchange and "
    "delisted flag cost 0. Narrow it with query (words from the code, name or ISIN) and type "
    "(common_stock, etf, fund, preferred_stock...), and page with max_rows and offset. The only way to "
    "find a delisted line is delisted=true; the US delisted list has about 60,000 rows."
)
SEARCH_SYMBOLS_DESCRIPTION = (
    "Find symbols by company name or code. Costs 1 API call (cached 24 hours). Each row has the previous "
    "close and its date, and possibly_inactive marks a line whose previous close is old: it may be "
    "delisted or renamed, so run check_symbol before relying on it. Delisted lines are not searchable; "
    "use list_exchange_symbols with delisted=true."
)
CHECK_SYMBOL_DESCRIPTION = (
    "Check whether a symbol is alive, and look for its successor if it is not. EODHD keeps answering "
    "HTTP 200 with frozen data for delisted or renamed codes, so this compares the newest of the last 14 "
    "days of bars with the latest session expected. Costs 1 API call, plus 1 for a quote with "
    "include_quote, plus up to 3 for the successor search when the answer is not ok and find_successor "
    "is on (a search by code, the exchange's delisted list if search no longer returns the line, a "
    "search by name): 5 at most, and 0 for Tokyo and the UAE, which EODHD does not cover. Candidates "
    "are leads: compare the ISIN and name before switching."
)

T = TypeVar("T")


def _text(value: object) -> str | None:
    """A non-empty string from an upstream field, cut to ``TEXT_LIMIT`` characters; else None."""
    if isinstance(value, str):
        return clip(value.strip()) or None
    return None


def _number(value: object) -> float | None:
    """A finite number from a JSON number or a numeric string; ``"NA"``, booleans and null give None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _first(row: dict[str, Any], *names: str) -> object:
    """The value of the first of ``names`` present in ``row``; EODHD spells ISIN two ways."""
    return next((row[name] for name in names if name in row), None)


def _given(value: str | None) -> str | None:
    """None for a missing or blank optional text: models often send an empty string for 'not set'."""
    return (value or "").strip() or None


def _words(text: str | None) -> list[str]:
    return text.casefold().split() if text else []


def _matches(tokens: Sequence[str], *fields: str | None) -> bool:
    """True when every token is part of the code, name or other field (so word order does not matter)."""
    haystack = " ".join(value.casefold() for value in fields if value)
    return all(token in haystack for token in tokens)


def _type_key(text: str | None) -> str:
    """``Common Stock``, ``common-stock`` and ``common_stock`` all become ``common_stock``."""
    return "_".join((text or "").casefold().replace("-", " ").replace("_", " ").split())


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _clean_query(raw: str) -> str:
    """Make a search text fit one path segment: a ``/`` becomes a space and runs of blanks collapse."""
    return " ".join(raw.replace("/", " ").split())


def _searchable(text: str) -> bool:
    """False for a text with nothing to search for; ``.`` and ``..`` are dot segments the client refuses."""
    return bool(text) and text not in {".", ".."}


def _invalid_query(raw: str) -> GuardError:
    return GuardError(
        ErrorCode.INVALID_PARAMS,
        f"query: received {raw[:60]!r}; it holds nothing to search for. "
        "Accepted: a company name or a code, 1 to 100 characters.",
        details={"field": "query"},
    )


def _bad_answer(endpoint: str, problem: str) -> GuardError:
    return GuardError(
        ErrorCode.BAD_RESPONSE,
        f"EODHD's {endpoint} answer {problem}.",
        hint="This is usually temporary; retry later.",
        details={"endpoint": endpoint},
    )


def _read_rows(body: object, endpoint: str, reader: Callable[[object], T | None]) -> list[T]:
    """Read every usable row of a list answer.

    An answer that is not a list, or a non-empty list without one readable row, is ``BAD_RESPONSE``: it
    must not pass for an empty result.
    """
    if not isinstance(body, list):
        raise _bad_answer(endpoint, f"is a {type(body).__name__}, not a list")
    rows = [row for row in map(reader, body) if row is not None]
    if body and not rows:
        raise _bad_answer(endpoint, f"has {len(body)} rows, none of them readable")
    return rows


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


def _now_utc(deps: Deps) -> datetime:
    return deps.clock().astimezone(timezone.utc)


# --- list_exchanges ---------------------------------------------------------------------------------


def _coverage_note(gap: CoverageGap) -> str:
    what = "only earnings dates" if gap.calendar_dates else "no data"
    return f"EODHD has {what} for the {gap.market} (checked October 2026). Alternatives: {gap.alternatives}"


def _read_exchange(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    code = _text(raw.get("Code"))
    if code is None:
        return None
    gap = find_gap(code)
    return {
        "code": code,
        "name": _text(raw.get("Name")),
        "country": _text(raw.get("Country")),
        "currency": _text(raw.get("Currency")),
        "operating_mic": _text(raw.get("OperatingMIC")),
        "country_iso2": _text(raw.get("CountryISO2")),
        "country_iso3": _text(raw.get("CountryISO3")),
        "coverage_note": _coverage_note(gap) if gap is not None else None,
    }


def _exchanges_warning(rows: Sequence[dict[str, Any]]) -> ToolWarning:
    """Always present: Tokyo and the UAE are absent from the list, which is how they would go unseen."""
    markets = "; ".join(gap.market for gap in GAPS)
    listed = [row["code"] for row in rows if row["coverage_note"] is not None]
    return ToolWarning.of(
        WarningCode.PARTIAL_COVERAGE,
        f"In checks in October 2026 EODHD had no usable prices or fundamentals for: {markets}. They are "
        "missing from this list or listed without data (AE is listed but its symbol list is empty), so "
        "being on this list does not prove an exchange is covered.",
        listed_without_data=listed,
    )


async def list_exchanges(deps: Deps, query: str | None) -> ToolResponse:
    """The exchange list with coverage notes, optionally filtered by words in the code, name or country."""
    response = await deps.client.get(
        "exchanges-list",
        endpoint="exchanges-list",
        cost=cost_for("exchanges-list"),
        ttl_s=EXCHANGES_TTL_S,
    )
    rows = _read_rows(response.data, "exchanges-list", _read_exchange)
    if not rows:
        raise GuardError(
            ErrorCode.NO_DATA,
            "EODHD answered HTTP 200 with an empty list of exchanges.",
            hint="This is not normal; retry later.",
        )
    query = _given(query)
    tokens = _words(query)
    shown = [row for row in rows if _matches(tokens, row["code"], row["name"], row["country"])]
    warnings = [_exchanges_warning(shown)]
    if not shown:
        warnings.append(
            ToolWarning.of(
                WarningCode.EMPTY_RESULT,
                f"No exchange matches {query!r} among the {len(rows)} listed. Try a country name, an "
                "exchange name or a code.",
                list_size=len(rows),
            )
        )
    data = {"query": query, "list_size": len(rows), "count": len(shown), "exchanges": shown}
    return build_response(data, [response], warnings=warnings)


# --- list_exchange_symbols --------------------------------------------------------------------------


def _read_listed(raw: object, exchange: str, only: str | None = None) -> dict[str, Any] | None:
    """One row of an exchange symbol list; ``symbol`` uses the requested exchange, not the row's venue.

    With ``only``, rows of other codes are skipped before anything is built.
    """
    if not isinstance(raw, dict):
        return None
    code = _text(raw.get("Code"))
    if code is None or (only is not None and code.upper() != only):
        return None
    code = code.upper()
    return {
        "symbol": f"{code}.{exchange}",
        "code": code,
        "name": _text(raw.get("Name")),
        "type": _text(raw.get("Type")),
        "currency": _text(raw.get("Currency")),
        "country": _text(raw.get("Country")),
        "isin": _text(_first(raw, "Isin", "ISIN")),
        "venue": _text(raw.get("Exchange")),
    }


def _empty_list_error(exchange: str, delisted: bool) -> GuardError:
    which = "delisted" if delisted else "symbol"
    return GuardError(
        ErrorCode.NO_DATA,
        f"EODHD lists {exchange} but answered HTTP 200 with an empty {which} list.",
        hint=(
            "Some exchanges are listed without data (AE is one). list_exchanges shows what EODHD "
            "lists; search_symbols can still find individual lines."
        ),
        details={"exchange": exchange, "delisted": delisted},
    )


def _types_present(rows: Sequence[dict[str, Any]]) -> str:
    counts = Counter(_type_key(row["type"]) or "unknown" for row in rows)
    return ", ".join(
        f"{clip(name, TYPE_CHARS)} ({count})" for name, count in counts.most_common(MAX_TYPES_SHOWN)
    )


def _no_match_warning(
    exchange: str, query: str | None, kind: str | None, rows: Sequence[dict[str, Any]]
) -> ToolWarning:
    filters = " and ".join(
        part for part in (f"query {query!r}" if query else "", f"type {kind!r}" if kind else "") if part
    )
    message = f"No symbol on {exchange} matches {filters}; the list holds {len(rows)} symbols."
    if kind:
        message += f" Types on this list: {_types_present(rows)}."
    return ToolWarning.of(WarningCode.EMPTY_RESULT, message, list_size=len(rows))


def _page_warning(total: int, count: int, offset: int) -> ToolWarning:
    after = offset + count
    return ToolWarning.of(
        WarningCode.TRUNCATED,
        f"{total} symbols match; {count} are shown, from position {offset}. Pass offset={after} for the "
        "next page, or narrow the list with query and type.",
        total=total,
        returned=count,
        offset=offset,
        next_offset=after,
    )


async def _fetch_symbol_list(deps: Deps, exchange: str, *, delisted: bool, tag: str | None) -> ApiResponse:
    """The whole symbol list of an exchange. One request shape, so every caller shares one cache entry."""
    return await deps.client.get(
        f"exchange-symbol-list/{exchange}",
        {"delisted": 1 if delisted else None},
        endpoint="exchange-symbol-list",
        cost=cost_for("exchange-symbol-list"),
        ttl_s=SYMBOLS_TTL_S,
        symbol=tag,
    )


async def list_exchange_symbols(
    deps: Deps,
    exchange: str,
    query: str | None,
    kind: str | None,
    delisted: bool,
    max_rows: int,
    offset: int,
) -> ToolResponse:
    """One exchange's symbol list, filtered by query and type and cut into pages here."""
    code = parse_exchange(exchange)
    query, kind = _given(query), _given(kind)
    ensure_exchange_covered(code, "symbols", allow_uncovered=deps.settings.allow_uncovered)
    try:
        response = await _fetch_symbol_list(deps, code, delisted=delisted, tag=None)
    except GuardError as exc:
        if exc.code is not ErrorCode.NOT_FOUND:
            raise
        raise GuardError(
            ErrorCode.NOT_FOUND,
            exc.message,
            hint="list_exchanges shows the exchange codes EODHD knows.",
            details=exc.details,
        ) from None
    rows = _read_rows(response.data, "exchange-symbol-list", lambda raw: _read_listed(raw, code))
    if not rows:
        raise _empty_list_error(code, delisted)

    tokens = _words(query)
    wanted = _type_key(kind)
    matched = [
        row
        for row in rows
        if (not wanted or _type_key(row["type"]) == wanted)
        and _matches(tokens, row["code"], row["name"], row["isin"])
    ]
    page = matched[offset : offset + max_rows]
    warnings: list[ToolWarning] = []
    if not matched:
        warnings.append(_no_match_warning(code, query, kind, rows))
    elif not page:
        warnings.append(
            ToolWarning.of(
                WarningCode.EMPTY_RESULT,
                f"offset {offset} is past the last of the {len(matched)} matching symbols.",
                total=len(matched),
            )
        )
    elif offset + len(page) < len(matched):
        warnings.append(_page_warning(len(matched), len(page), offset))
    next_offset = offset + len(page) if offset + len(page) < len(matched) else None
    data = {
        "exchange": code,
        "delisted": delisted,
        "query": query,
        "type": kind,
        "list_size": len(rows),
        "total": len(matched),
        "offset": offset,
        "count": len(page),
        "next_offset": next_offset,
        "symbols": page,
    }
    if deps.settings.cache_enabled:
        note = (
            "Filtering and paging run here on one cached list: more pages and other filters for this "
            "exchange cost 0 until the cache expires (24 hours)."
        )
    else:
        note = (
            "Filtering and paging run here on the whole list. The cache is off, so every page and filter "
            f"fetches it again and costs {cost_for('exchange-symbol-list')} API call."
        )
    return build_response(data, [response], warnings=warnings, notes=[note])


# --- search_symbols ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Hit:
    """One row of a search answer."""

    code: str
    exchange: str
    name: str | None
    kind: str | None
    country: str | None
    currency: str | None
    isin: str | None
    is_primary: bool | None
    previous_close: float | None
    previous_close_date: date | None

    @property
    def symbol(self) -> str:
        return f"{self.code}.{self.exchange}"


def _read_hit(raw: object) -> _Hit | None:
    if not isinstance(raw, dict):
        return None
    code, exchange = _text(raw.get("Code")), _text(raw.get("Exchange"))
    if code is None or exchange is None:
        return None
    primary = raw.get("isPrimary")
    return _Hit(
        code=code.upper(),
        exchange=exchange.upper(),
        name=_text(raw.get("Name")),
        kind=_text(raw.get("Type")),
        country=_text(raw.get("Country")),
        currency=_text(raw.get("Currency")),
        isin=_text(_first(raw, "ISIN", "Isin")),
        is_primary=primary if isinstance(primary, bool) else None,
        previous_close=_number(raw.get("previousClose")),
        previous_close_date=iso_date_or_none(raw.get("previousCloseDate")),
    )


async def _search(
    deps: Deps,
    query: str,
    *,
    exchange: str | None,
    kind: str | None,
    limit: int,
    tag: str | None,
    responses: list[ApiResponse],
) -> list[_Hit]:
    """One ``search/{query}`` call. The answer is added to ``responses``; its readable rows are returned."""
    response = await deps.client.get(
        f"search/{query}",
        {"limit": limit, "exchange": exchange, "type": kind},
        endpoint="search",
        cost=cost_for("search"),
        ttl_s=SEARCH_TTL_S,
        symbol=tag,
    )
    responses.append(response)
    return _read_rows(response.data, "search", _read_hit)


def _is_inactive(deps: Deps, hit: _Hit, now: datetime) -> bool:
    """No previous close, or one that is ``stale_sessions`` or more sessions old: no sign the line trades."""
    return assess(hit.exchange, hit.previous_close_date, now, threshold=deps.settings.stale_sessions).stale


def _hit_row(hit: _Hit, inactive: bool) -> dict[str, Any]:
    return {
        "symbol": hit.symbol,
        "code": hit.code,
        "exchange": hit.exchange,
        "name": hit.name,
        "type": hit.kind,
        "country": hit.country,
        "currency": hit.currency,
        "isin": hit.isin,
        "is_primary": hit.is_primary,
        "previous_close": hit.previous_close,
        "previous_close_date": _iso(hit.previous_close_date),
        "possibly_inactive": inactive,
    }


def _listing(symbols: Sequence[str]) -> str:
    shown = ", ".join(clip(symbol, IDENTIFIER_CHARS) for symbol in symbols[:MAX_SYMBOLS_IN_WARNING])
    return shown + (
        f" and {len(symbols) - MAX_SYMBOLS_IN_WARNING} more" if len(symbols) > MAX_SYMBOLS_IN_WARNING else ""
    )


def _inactive_warning(threshold: int, symbols: Sequence[str]) -> ToolWarning:
    return ToolWarning.of(
        WarningCode.POSSIBLY_INACTIVE,
        f"No recent previous close for {_listing(symbols)}: the date is missing or {threshold} or more "
        "sessions behind the latest session expected for the exchange. A line like that may be delisted, "
        "renamed or suspended; run check_symbol on the one you need.",
        symbols=list(symbols),
        threshold=threshold,
    )


def _partial_coverage_warning(hits: Sequence[_Hit]) -> ToolWarning:
    markets = sorted({gap.market for hit in hits if (gap := find_gap(hit.exchange)) is not None})
    symbols = [hit.symbol for hit in hits]
    return ToolWarning.of(
        WarningCode.PARTIAL_COVERAGE,
        f"Found on exchanges where EODHD has no usable prices ({'; '.join(markets)}): "
        f"{_listing(symbols)}. A search hit does not mean data exists; check_symbol names the alternatives.",
        symbols=symbols,
        markets=markets,
    )


async def search_symbols(
    deps: Deps, query: str, exchange: str | None, kind: SearchType, max_results: int
) -> ToolResponse:
    """Search by name or code; each row says whether the line still seems to trade."""
    text = _clean_query(query)
    if not _searchable(text):
        raise _invalid_query(query)
    given = _given(exchange)
    venue = parse_exchange(given) if given is not None else None
    responses: list[ApiResponse] = []
    hits = await _search(
        deps,
        text,
        exchange=venue,
        kind=None if kind == "all" else kind,
        limit=max_results,
        tag=None,
        responses=responses,
    )
    warnings: list[ToolWarning] = []
    if len(hits) > max_results:
        warnings.append(
            ToolWarning.of(
                WarningCode.TRUNCATED,
                f"EODHD returned {len(hits)} rows for a limit of {max_results}; the first {max_results} "
                "are shown.",
                returned=len(hits),
                limit=max_results,
            )
        )
        hits = hits[:max_results]
    if not hits:
        where = f" on {venue}" if venue else ""
        warnings.append(
            ToolWarning.of(
                WarningCode.EMPTY_RESULT,
                f"No listed symbol matches {text!r}{where}. Delisted lines are not searchable: try "
                "list_exchange_symbols with delisted=true, or check the spelling and try a shorter name.",
            )
        )
    now = _now_utc(deps)
    checked = [(hit, _is_inactive(deps, hit, now)) for hit in hits]
    flagged = [hit.symbol for hit, dead in checked if dead]
    if flagged:
        warnings.append(_inactive_warning(deps.settings.stale_sessions, flagged))
    uncovered = [hit for hit in hits if find_gap(hit.exchange) is not None]
    if uncovered:
        warnings.append(_partial_coverage_warning(uncovered))
    data = {
        "query": text,
        "exchange": venue,
        "type": kind,
        "count": len(hits),
        "results": [_hit_row(hit, dead) for hit, dead in checked],
    }
    notes = ["Delisted lines are not searchable; list_exchange_symbols with delisted=true lists them."]
    return build_response(data, responses, warnings=warnings, notes=notes)


# --- check_symbol -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Bars:
    """What the end-of-day probe found. ``found`` is False after a 404.

    ``last_bar`` is the newest real bar. ``fillers`` counts the holiday filler bars dated after it, which
    EODHD sends for days with no trading and which are never read as signs of life.
    """

    found: bool
    last_bar: date | None = None
    fillers: int = 0
    response: ApiResponse | None = None


@dataclass(frozen=True)
class _QuoteProbe:
    """``status`` is ok, no_quote (``"NA"`` fields), missing (404) or error (the call failed)."""

    status: str
    response: ApiResponse | None = None
    error: str | None = None


@dataclass
class _Successors:
    """What the lookup found. ``searched`` is False when it was not run."""

    searched: bool = False
    name: str | None = None
    isin: str | None = None
    delisted: bool = False
    """True when the line was found in the exchange's delisted list: EODHD itself calls it delisted."""
    clashing_names: list[str] = field(default_factory=list)
    """Different companies the delisted list holds under this code. The name can not be settled."""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class _Case:
    """Everything the advice is written from."""

    symbol: Symbol
    status: str
    freshness: Freshness
    window: tuple[date, date]
    quote: _QuoteProbe | None
    successors: _Successors
    fillers: int = 0


def _brief(exc: GuardError, deps: Deps) -> str:
    """An error as one line for the advice, with the key masked."""
    return redact(f"{exc.code.value}: {exc.message}", [deps.settings.api_key])


async def _probe_bars(deps: Deps, symbol: Symbol, window: tuple[date, date], now: datetime) -> _Bars:
    """The last 14 days of daily bars. A 404 is a finding; any other failure is raised.

    Rows are read as ``get_eod_prices`` reads them, and holiday filler bars are set aside: they repeat the
    last close with no volume, so they are not a sign that the line still trades.
    """
    start, end = window
    try:
        response = await deps.client.get(
            f"eod/{symbol.full}",
            {"from": start.isoformat(), "to": end.isoformat(), "period": "d", "order": "a"},
            endpoint="eod",
            cost=cost_for("eod"),
            # The window reaches today, so the answer goes out of date when the next bar is due.
            ttl_s=recent_bars_ttl_s(symbol.exchange, now, cap_s=CHECK_BARS_TTL_S),
            max_age_s=CHECK_BARS_TTL_S,
            symbol=symbol.full,
        )
    except GuardError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            return _Bars(found=False)
        raise
    bars = normalize_eod_rows(response.data)
    last_bar = max((bar.day for bar in bars if not is_filler_bar(bar)), default=None)
    fillers = sum(1 for bar in bars if is_filler_bar(bar) and (last_bar is None or bar.day > last_bar))
    return _Bars(found=True, last_bar=last_bar, fillers=fillers, response=response)


async def _probe_quote(deps: Deps, symbol: Symbol) -> _QuoteProbe:
    """A real-time quote counts only with a price and a timestamp: ``"NA"`` is not a quote."""
    try:
        response = await deps.client.get(
            f"real-time/{symbol.full}",
            endpoint="real-time",
            cost=cost_for("real-time"),
            ttl_s=QUOTE_TTL_S,
            symbol=symbol.full,
        )
    except GuardError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            return _QuoteProbe("missing")
        return _QuoteProbe("error", error=_brief(exc, deps))
    body = response.data[0] if isinstance(response.data, list) and response.data else response.data
    stamp = _number(body.get("timestamp")) if isinstance(body, dict) else None
    usable = stamp is not None and stamp > 0 and _number(body.get("close")) is not None
    return _QuoteProbe("ok" if usable else "no_quote", response)


def _usable_name(name: str | None) -> bool:
    return name is not None and _searchable(_clean_query(name))


def _exact_hit(hits: Sequence[_Hit], symbol: Symbol) -> _Hit | None:
    """The search row for this very line. A row of the same code on another exchange is another security."""
    return next((h for h in hits if h.code == symbol.code and h.exchange == symbol.exchange), None)


async def _delisted_rows(deps: Deps, symbol: Symbol, responses: list[ApiResponse]) -> list[dict[str, Any]]:
    """The rows of the exchange's delisted list that carry this very code.

    It is the list ``list_exchange_symbols`` pages, so a call made by either one is free for the other.
    An exchange the list endpoint does not know has no rows.
    """
    try:
        response = await _fetch_symbol_list(deps, symbol.exchange, delisted=True, tag=symbol.full)
    except GuardError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            return []
        raise
    responses.append(response)
    if not isinstance(response.data, list):
        raise _bad_answer("exchange-symbol-list", f"is a {type(response.data).__name__}, not a list")
    rows = (_read_listed(raw, symbol.exchange, only=symbol.code) for raw in response.data)
    return [row for row in rows if row is not None]


def _settle_listed_line(rows: Sequence[dict[str, Any]], found: _Successors) -> None:
    """Take the name from the delisted rows of one code, unless they belong to different companies.

    A delisted list can hold a code more than once (codes get reused), and a name from the wrong company
    would send the successor search after the wrong company.
    """
    companies: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _clean_query(row["name"] or "").casefold()
        if _searchable(key):
            companies.setdefault(key, []).append(row)
    if len(companies) > 1:
        found.clashing_names = [group[0]["name"] for group in companies.values()]
    elif companies:
        (group,) = companies.values()
        found.name = group[0]["name"]
        found.isin = next((row["isin"] for row in group if row["isin"]), None)
        found.delisted = True


async def _identify_line(
    deps: Deps, symbol: Symbol, responses: list[ApiResponse], found: _Successors, *, delisted_too: bool
) -> None:
    """Find the line's name and ISIN: in search first, then, if allowed, in the exchange's delisted list.

    Only this very line counts. EODHD's search no longer returns a frozen line, and a row with the same
    code on another exchange can be another company, so such a row is never used.
    """
    hits = await _search(
        deps,
        symbol.code,
        exchange=symbol.exchange,
        kind=None,
        limit=SEARCH_POOL,
        tag=symbol.full,
        responses=responses,
    )
    line = _exact_hit(hits, symbol)
    if line is not None and _usable_name(line.name):
        found.name, found.isin = line.name, line.isin
    elif delisted_too:
        _settle_listed_line(await _delisted_rows(deps, symbol, responses), found)


def _isin_country(isin: str | None) -> str | None:
    """The two letters an ISIN starts with: the country of issue (XS for international issues)."""
    return isin[:2].upper() if isin and isin[:2].isalpha() else None


def _candidates(
    deps: Deps, hits: Sequence[_Hit], symbol: Symbol, now: datetime, isin: str | None
) -> list[dict[str, Any]]:
    """Other codes on the same exchange with a recent previous close: lines the company may continue as.

    Those whose ISIN starts with the same country letters as this line's come first. The others stay,
    because a re-keyed line can get a new ISIN.
    """
    seen: set[str] = set()
    alive: list[_Hit] = []
    for hit in hits:
        if hit.exchange != symbol.exchange or hit.code == symbol.code or hit.symbol in seen:
            continue
        if _is_inactive(deps, hit, now):
            continue
        seen.add(hit.symbol)
        alive.append(hit)
    country = _isin_country(isin)
    if country is not None:
        alive.sort(key=lambda hit: _isin_country(hit.isin) != country)
    return [
        {
            "symbol": hit.symbol,
            "name": hit.name,
            "isin": hit.isin,
            "previous_close_date": _iso(hit.previous_close_date),
        }
        for hit in alive[:MAX_CANDIDATES]
    ]


async def _find_successors(
    deps: Deps, symbol: Symbol, now: datetime, responses: list[ApiResponse], *, delisted_too: bool
) -> _Successors:
    """Name the line, then search that name on the same exchange for codes the company may continue as.

    A failure ends the lookup but keeps what was found, so the verdict already paid for is not lost.
    """
    found = _Successors(searched=True)
    try:
        await _identify_line(deps, symbol, responses, found, delisted_too=delisted_too)
        if found.name is None:
            return found
        by_name = await _search(
            deps,
            _clean_query(found.name),
            exchange=symbol.exchange,
            kind=None,
            limit=SEARCH_POOL,
            tag=symbol.full,
            responses=responses,
        )
    except GuardError as exc:
        found.error = _brief(exc, deps)
        return found
    found.candidates = _candidates(deps, by_name, symbol, now, found.isin)
    return found


def _status(bars: _Bars, freshness: Freshness, quote: _QuoteProbe | None) -> str:
    """Precedence: not_found, stale, no_quote, ok. ``quote_status`` keeps what the status hides."""
    if not bars.found:
        return "not_found"
    if freshness.stale:
        return "stale"
    if quote is not None and quote.status in {"no_quote", "missing"}:
        return "no_quote"
    return "ok"


def _finding(case: _Case) -> str:
    name, fr = case.symbol.full, case.freshness
    if case.status == "not_found":
        return f"EODHD does not know {name}: the end-of-day endpoint answered 404."
    if case.status == "stale" and fr.last_bar is None:
        start, end = case.window
        only = f" (only {_plural(case.fillers, 'holiday filler bar')})" if case.fillers else ""
        seen = f"has no {'real ' if case.fillers else ''}end-of-day bars from {start} to {end}{only}"
        return f"{name} {seen}, although a bar for {fr.expected_latest} should exist: the code looks dead."
    behind = _plural(fr.sessions_behind, "session")
    if case.status == "stale":
        ignored = (
            f" EODHD also sent {_plural(case.fillers, 'filler bar')} dated after it (zero volume, open = "
            "high = low = close), which do not count as trading."
            if case.fillers
            else ""
        )
        return (
            f"{name} looks frozen: its newest bar is {fr.last_bar}, {behind} behind the latest "
            f"expected ({fr.expected_latest}).{ignored}"
        )
    if case.status == "no_quote":
        return (
            f"{name} has current end-of-day bars (newest {fr.last_bar}) but no real-time quote: EODHD "
            "has none for it, which is normal for some funds and indices. Use get_eod_prices."
        )
    if fr.sessions_behind == 0:
        return f"{name} looks alive: the newest bar ({fr.last_bar}) is the latest session expected by now."
    return (
        f"{name} looks alive: the newest bar is {fr.last_bar}, {behind} behind the latest expected "
        f"({fr.expected_latest}), within the normal delay."
    )


def _identity_sentence(case: _Case) -> str:
    """What the delisted list said about the line, when it was consulted and had something to say.

    The sentences name no company: what the vendor calls one is text of its own, and it is in the data.
    """
    found, symbol = case.successors, case.symbol
    if found.clashing_names:
        return (
            f"The delisted list for {symbol.exchange} holds {len(found.clashing_names)} different "
            f"companies under the code {symbol.code}, so its name could not be settled; they are named "
            "in clashing_names."
        )
    if found.delisted:
        return (
            f"EODHD's delisted list for {symbol.exchange} has {symbol.code}; its name and ISIN are in "
            "name and isin."
        )
    return ""


def _next_step(case: _Case) -> str:
    found = case.successors
    if found.candidates:
        symbols = ", ".join(clip(str(c["symbol"]), IDENTIFIER_CHARS) for c in found.candidates)
        many = len(found.candidates) > 1
        return (
            f"{'Possible successors' if many else 'Possible successor'}: {symbols}. Compare "
            f"{'their' if many else 'its'} ISIN and name (see successor_candidates) with this line's "
            "before switching."
        )
    if case.status in {"ok", "no_quote"}:
        return ""
    if found.error:
        return (
            f"The successor search stopped early ({found.error}). Use search_symbols with the company name."
        )
    if not found.searched:
        return (
            "To look for the right code, run check_symbol with find_successor=true, or use "
            "search_symbols with the company name."
        )
    if found.clashing_names:
        return "Use search_symbols with the company name."
    if found.name is None:
        return (
            "Could not find the line's name in search or in the delisted list for "
            f"{case.symbol.exchange}, so no successor search was possible. Use search_symbols with the "
            "company name."
        )
    return (
        f"No other code on {case.symbol.exchange} with a recent price matches the name found for this line "
        "(see name). The company may have changed its name as well as its code: try search_symbols with "
        "the new name."
    )


def _advice(case: _Case) -> str:
    parts = [_finding(case), _identity_sentence(case)]
    if case.quote is not None and case.quote.status == "error":
        parts.append(f"The quote check failed ({case.quote.error}).")
    parts.append(_next_step(case))
    return " ".join(part for part in parts if part)


def _stale_warning(symbol: Symbol, freshness: Freshness) -> ToolWarning:
    """The shared STALE_DATA warning without its advice to run check_symbol: this already is check_symbol."""
    shared = stale_warning(symbol, freshness)
    return shared.model_copy(update={"message": shared.message.split(" Run check_symbol", 1)[0]})


def _quote_warning(symbol: Symbol, quote: _QuoteProbe) -> ToolWarning:
    answer = "answered NA" if quote.status == "no_quote" else "answered HTTP 404"
    return ToolWarning.of(
        WarningCode.NO_QUOTE,
        f"EODHD has no usable real-time quote for {symbol.full} (it {answer}). The code may be unknown, "
        "delisted or renamed; funds and some indices have no real-time feed at all.",
        symbol=symbol.full,
        quote_status=quote.status,
    )


def _not_covered(symbol: Symbol, now: datetime, refusal: GuardError) -> ToolResponse:
    """The verdict for a market EODHD does not cover. No call is made, so nothing is spent."""
    market = refusal.details.get("market", "this market")
    data: dict[str, Any] = {
        "symbol": symbol.full,
        "status": "not_covered",
        "last_bar_date": None,
        "expected_latest_session": expected_latest_session(symbol.exchange, now).isoformat(),
        "sessions_behind": None,
        "quote_status": None,
        "name": None,
        "isin": None,
        "clashing_names": [],
        "successor_candidates": [],
        "advice": (
            f"{symbol.full} is on the {market}, which EODHD does not cover, so nothing was looked up and "
            f"nothing was spent. {refusal.hint or ''}"
        ).strip(),
    }
    warning = ToolWarning.of(
        WarningCode.NOT_COVERED,
        f"{refusal.message} {refusal.hint or ''}".strip(),
        **refusal.details,
    )
    return build_response(data, [], warnings=[warning])


async def check_symbol(
    deps: Deps, raw_symbol: str, include_quote: bool, find_successor: bool
) -> ToolResponse:
    """Is this code alive? Compare its newest bar with the expected session, and look for a successor."""
    symbol = parse_symbol(raw_symbol)
    now = _now_utc(deps)
    try:
        ensure_covered(symbol, "prices", allow_uncovered=deps.settings.allow_uncovered)
    except GuardError as refusal:
        return _not_covered(symbol, now, refusal)

    today = now.date()
    window = (today - timedelta(days=CHECK_WINDOW_DAYS), today)
    bars = await _probe_bars(deps, symbol, window, now)
    responses: list[ApiResponse] = [bars.response] if bars.response is not None else []
    quote = await _probe_quote(deps, symbol) if include_quote else None
    if quote is not None and quote.response is not None:
        responses.append(quote.response)

    freshness = assess(symbol.exchange, bars.last_bar, now, threshold=deps.settings.stale_sessions)
    status = _status(bars, freshness, quote)
    successors = _Successors()
    if status != "ok" and find_successor:
        # A line with current bars is not delisted, so only a dead one is looked up in the delisted list.
        successors = await _find_successors(deps, symbol, now, responses, delisted_too=status != "no_quote")

    case = _Case(symbol, status, freshness, window, quote, successors, bars.fillers)
    data = {
        "symbol": symbol.full,
        "status": status,
        "last_bar_date": _iso(bars.last_bar),
        "expected_latest_session": freshness.expected_latest.isoformat(),
        "sessions_behind": freshness.sessions_behind if bars.last_bar is not None else None,
        "quote_status": quote.status if quote is not None else None,
        "name": successors.name,
        "isin": successors.isin,
        "clashing_names": successors.clashing_names,
        "successor_candidates": successors.candidates,
        "advice": _advice(case),
    }
    warnings: list[ToolWarning] = []
    notes: list[str] = []
    if status == "stale":
        warnings.append(_stale_warning(symbol, freshness))
        notes.append(
            "Freshness is judged on the exchange's weekday calendar. Holidays are not modelled, so a "
            "closure of several days can look stale."
        )
    if quote is not None and quote.status in {"no_quote", "missing"}:
        warnings.append(_quote_warning(symbol, quote))
    return build_response(data, responses, warnings=warnings, notes=notes)


# --- registration -----------------------------------------------------------------------------------


def register(server: MCPServer, deps: Deps) -> None:
    """Register list_exchanges, list_exchange_symbols, search_symbols and check_symbol."""

    @server.tool(
        name="list_exchanges",
        title="List exchanges",
        description=LIST_EXCHANGES_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def list_exchanges_tool(
        query: Annotated[
            str | None,
            Field(
                max_length=100,
                description=(
                    "Words to look for in the exchange code, name or country, for example 'india' or "
                    "'swiss'. Every word must match. Leave out to list all exchanges."
                ),
            ),
        ] = None,
    ) -> ToolResponse:
        return await list_exchanges(deps, query)

    @server.tool(
        name="list_exchange_symbols",
        title="List exchange symbols",
        description=LIST_EXCHANGE_SYMBOLS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def list_exchange_symbols_tool(
        exchange: Annotated[
            str,
            Field(
                max_length=10,
                description="Exchange code as in a symbol's suffix, for example US, LSE, SW or NSE.",
            ),
        ],
        query: Annotated[
            str | None,
            Field(
                max_length=100,
                description=(
                    "Words to look for in the code, name or ISIN, in any order, for example 'nestle'. "
                    "Leave out for no filter."
                ),
            ),
        ] = None,
        type: Annotated[
            str | None,
            Field(
                max_length=30,
                description=(
                    "Only this type of instrument, matched ignoring case with spaces written as "
                    "underscores: common_stock, etf, fund, preferred_stock, mutual_fund, index and so on. "
                    "Leave out for all types."
                ),
            ),
        ] = None,
        delisted: Annotated[
            bool,
            Field(description="True lists delisted lines instead of active ones. Search never finds them."),
        ] = False,
        max_rows: Annotated[
            int,
            Field(ge=1, le=500, description="Rows per page, 1 to 500. TRUNCATED reports the total."),
        ] = 100,
        offset: Annotated[
            int,
            Field(ge=0, le=1_000_000, description="Position of the first row of the page, to page on."),
        ] = 0,
    ) -> ToolResponse:
        return await list_exchange_symbols(deps, exchange, query, type, delisted, max_rows, offset)

    @server.tool(
        name="search_symbols",
        title="Search symbols",
        description=SEARCH_SYMBOLS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def search_symbols_tool(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                description=(
                    "A company name or a code, such as 'Acme Corp' or 'ACME'. A '/' is read as a space."
                ),
            ),
        ],
        exchange: Annotated[
            str | None,
            Field(
                max_length=10,
                description="Only lines on this exchange, for example SW or LSE. Leave out to search all.",
            ),
        ] = None,
        type: Annotated[
            SearchType,
            Field(description="Kind of instrument: all, stock, etf, fund, bonds, index or crypto."),
        ] = "all",
        max_results: Annotated[int, Field(ge=1, le=50, description="Most rows to return, 1 to 50.")] = 15,
    ) -> ToolResponse:
        return await search_symbols(deps, query, exchange, type, max_results)

    @server.tool(
        name="check_symbol",
        title="Check symbol",
        description=CHECK_SYMBOL_DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def check_symbol_tool(
        symbol: Annotated[
            str,
            Field(
                max_length=40,
                description="The symbol to check, as CODE.EXCHANGE, for example ROG.SW.",
            ),
        ],
        include_quote: Annotated[
            bool,
            Field(description="Also ask for a real-time quote (1 more API call); 'NA' fields mean no_quote."),
        ] = False,
        find_successor: Annotated[
            bool,
            Field(
                description=(
                    "When the code looks dead, find the line's name (in search, else in the exchange's "
                    "delisted list) and search that name for a successor (up to 3 more API calls)."
                )
            ),
        ] = True,
    ) -> ToolResponse:
        return await check_symbol(deps, symbol, include_quote, find_successor)
