"""Tool: get_quote.

EODHD answers HTTP 200 with every field set to ``"NA"`` for a code it does not know, so each requested
symbol is read on its own and gets a status: ``ok``, ``no_quote``, ``missing`` or ``not_covered``. The logic
lives in ``fetch_quotes``; the registered tool only forwards its argument.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from eodhd_guard_mcp.costs import cost_for
from eodhd_guard_mcp.coverage import ensure_covered
from eodhd_guard_mcp.envelope import ToolResponse, ToolWarning, WarningCode, build_response, iso_utc
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.freshness import Freshness, assess
from eodhd_guard_mcp.markets import market_for
from eodhd_guard_mcp.tools import READ_ONLY, guarded
from eodhd_guard_mcp.tools.prices import currency_note, finite_number, share_class_hint
from eodhd_guard_mcp.validation import Symbol, parse_symbols

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.client import ApiResponse
    from eodhd_guard_mcp.server import Deps

MAX_SYMBOLS = 20
QUOTE_TTL_S = 60.0
MAX_SHOWN_NUMBER = 20
"""A number longer than this is not written into a message."""

QUOTE_DESCRIPTION = (
    "Delayed quotes for 1 to 20 symbols in one request. Costs 1 API call per symbol sent; cached for 60 "
    "seconds. Quotes are 15 to 20 minutes behind the market: as_of and age_seconds say how old each one is. "
    'For an unknown, delisted or renamed code EODHD answers HTTP 200 with every field set to "NA"; such '
    "symbols come back as no_quote, and the call fails with NO_DATA when no symbol has a quote."
)
SYMBOLS_HELP = (
    'The symbols as CODE.EXCHANGE, for example ["AAPL.US", "VOD.LSE", "NESN.SW"], or one comma-separated '
    "string. 1 to 20 symbols; a repeated symbol is sent once. A code without an exchange is refused."
)
QUOTE_NOTE = (
    "price is the close field of EODHD's real-time answer: the last price at as_of (UTC). change and "
    "change_pct are measured against previous_close."
)


def _seen(value: object) -> str:
    """Name what EODHD sent without echoing arbitrary text: "NA" and null by name, short numbers as is."""
    if value == "NA":
        return '"NA"'
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        shown = repr(value)
        return shown if len(shown) <= MAX_SHOWN_NUMBER else "an out-of-range number"
    return "an unreadable value"


def _instant(value: int | float | None) -> datetime | None:
    """A quote's Unix timestamp as an instant; None for a missing, non-positive or impossible one."""
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _whole(value: int | float | None) -> int | float | None:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _delayed_warning() -> ToolWarning:
    return ToolWarning.of(
        WarningCode.DELAYED_QUOTE,
        "Quotes are delayed 15 to 20 minutes, so they are not the live price. as_of is each quote's own "
        "timestamp (UTC) and age_seconds says how old it is now.",
        delay_minutes=[15, 20],
    )


def _stale_quote_warning(
    symbol: Symbol, moment: datetime, quote_day: date, freshness: Freshness
) -> ToolWarning:
    unit = "session" if freshness.sessions_behind == 1 else "sessions"
    return ToolWarning.of(
        WarningCode.STALE_QUOTE,
        f"The quote for {symbol.full} is stamped {iso_utc(moment)} (local date {quote_day.isoformat()}), but "
        f"the latest session expected by now is {freshness.expected_latest.isoformat()}: "
        f"{freshness.sessions_behind} {unit} behind. EODHD keeps answering for codes that are frozen, "
        "delisted or renamed, so this may be a dead line and not a quiet market. Run check_symbol to test "
        "the code, and use search_symbols with the company name to look for a successor code.",
        symbol=symbol.full,
        as_of=iso_utc(moment),
        quote_date=quote_day.isoformat(),
        expected_latest=freshness.expected_latest.isoformat(),
        sessions_behind=freshness.sessions_behind,
    )


def _refused(symbol: Symbol, error: GuardError) -> tuple[dict[str, Any], ToolWarning]:
    """A symbol on a market EODHD does not cover: it is never sent, and its entry says why."""
    market = error.details.get("market", "this market")
    entry = {
        "symbol": symbol.full,
        "status": "not_covered",
        "message": f"EODHD has no quotes for the {market}; the symbol was not sent and cost nothing.",
    }
    warning = ToolWarning.of(
        WarningCode.NOT_COVERED,
        " ".join(part for part in (error.message, error.hint) if part),
        symbol=symbol.full,
        market=market,
        gap=error.details.get("gap"),
    )
    return entry, warning


def _no_quote(symbol: Symbol, row: Mapping[str, Any]) -> tuple[dict[str, Any], ToolWarning]:
    seen = f"timestamp {_seen(row.get('timestamp'))}, close {_seen(row.get('close'))}"
    entry = {
        "symbol": symbol.full,
        "status": "no_quote",
        "message": (
            f"EODHD returned no usable quote ({seen}): an unknown, delisted or renamed code; "
            "run check_symbol."
        ),
    }
    warning = ToolWarning.of(
        WarningCode.NO_QUOTE,
        f"EODHD returned no usable quote for {symbol.full} ({seen}). That is how it answers for an unknown, "
        "delisted or renamed code. Run check_symbol to test the code, and use search_symbols with the "
        "company name to look for a successor code.",
        symbol=symbol.full,
        timestamp=_seen(row.get("timestamp")),
        close=_seen(row.get("close")),
    )
    return entry, warning


def _read_quote(
    symbol: Symbol, row: Mapping[str, Any], *, now: datetime, threshold: int
) -> tuple[dict[str, Any], list[ToolWarning]]:
    """One symbol's entry, and the warnings it earns. A quote needs a real timestamp and a close."""
    moment = _instant(finite_number(row.get("timestamp")))
    price = finite_number(row.get("close"))
    if moment is None or price is None:
        entry, warning = _no_quote(symbol, row)
        return entry, [warning]
    entry = {
        "symbol": symbol.full,
        "status": "ok",
        "price": price,
        "open": finite_number(row.get("open")),
        "high": finite_number(row.get("high")),
        "low": finite_number(row.get("low")),
        "previous_close": finite_number(row.get("previousClose")),
        "change": finite_number(row.get("change")),
        "change_pct": finite_number(row.get("change_p")),
        "volume": _whole(finite_number(row.get("volume"))),
        "as_of": iso_utc(moment),
        "age_seconds": max(0, int((now - moment).total_seconds())),
        "delayed": True,
    }
    try:
        quote_day = moment.astimezone(ZoneInfo(market_for(symbol.exchange).tz)).date()
    except OverflowError:  # the last instant a datetime holds is past year 9999 on a market ahead of UTC
        entry, warning = _no_quote(symbol, row)
        return entry, [warning]
    freshness = assess(symbol.exchange, quote_day, now, threshold=threshold)
    warnings = [_stale_quote_warning(symbol, moment, quote_day, freshness)] if freshness.stale else []
    return entry, warnings


def _answers_by_code(data: Any, sent: Sequence[Symbol]) -> dict[str, dict[str, Any]]:
    """EODHD's answer as a dict per symbol: a dict for one symbol, a list for several, matched by ``code``."""
    rows = [data] if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            f"EODHD answered with a {type(data).__name__} value where quotes were expected.",
            hint="The API may have changed its response shape; the data cannot be trusted as it is.",
        )
    items = [item for item in rows if isinstance(item, dict)]
    by_code: dict[str, dict[str, Any]] = {}
    for item in items:
        code = item.get("code")
        if isinstance(code, str) and code.strip():
            by_code.setdefault(code.strip().upper(), item)
    if not by_code and len(sent) == 1 and len(items) == 1:
        # A lone answer without a code can only be about the one symbol that was asked for.
        by_code[sent[0].full] = items[0]
    if rows and not by_code:
        raise GuardError(
            ErrorCode.BAD_RESPONSE,
            "EODHD's real-time answer has no usable code field, so its quotes cannot be matched to the "
            "symbols asked for.",
            hint="The API may have changed its response shape; the data cannot be trusted as it is.",
        )
    return by_code


def _quote_not_found(error: GuardError, first: Symbol, count: int) -> GuardError:
    lead = (
        f" The request carried {count} symbols and {first.full} led it, so the 404 may be about it alone."
        if count > 1
        else ""
    )
    return GuardError(
        ErrorCode.NOT_FOUND,
        f"No quote for {first.full}. {error.message}{lead}",
        hint=(
            "Run check_symbol on the code, or find it with search_symbols (by company name or ticker). "
            + ("Drop it or fix its suffix and ask again." if count > 1 else "")
            + share_class_hint(first)
        ).strip(),
        details=error.details,
    )


async def _fetch(deps: Deps, symbols: Sequence[Symbol]) -> ApiResponse:
    """One ``real-time`` request: the first symbol in the path and the others in ``s=``."""
    first, rest = symbols[0], symbols[1:]
    try:
        return await deps.client.get(
            f"real-time/{first.full}",
            {"s": ",".join(symbol.full for symbol in rest)} if rest else None,
            endpoint="real-time",
            cost=cost_for("real-time", len(symbols)),
            ttl_s=QUOTE_TTL_S,
            symbol=",".join(symbol.full for symbol in symbols),
        )
    except GuardError as error:
        if error.code is ErrorCode.NOT_FOUND:
            raise _quote_not_found(error, first, len(symbols)) from None
        raise


def _no_data_error(
    entries: Sequence[Mapping[str, Any]], refusals: Mapping[str, GuardError], responses: Sequence[ApiResponse]
) -> GuardError:
    listing = "; ".join(f"{entry['symbol']}: {entry['status']} ({entry['message']})" for entry in entries)
    spent = (
        f" The request cost {sum(response.cost for response in responses)} API call(s)."
        if responses
        else " Nothing was sent, so nothing was charged."
    )
    hints: list[str] = []
    if any(entry["status"] in ("no_quote", "missing") for entry in entries):
        hints.append(
            "Run check_symbol on a no_quote or missing code to see whether it was renamed or delisted; "
            "search_symbols with the company name finds the new code."
        )
    for error in refusals.values():
        if error.hint and error.hint not in hints:
            hints.append(error.hint)
    return GuardError(
        ErrorCode.NO_DATA,
        f"None of the {len(entries)} requested symbol(s) has a quote. {listing}.{spent}",
        hint=" ".join(hints) or None,
        details={"statuses": {entry["symbol"]: entry["status"] for entry in entries}},
    )


async def fetch_quotes(deps: Deps, *, symbols: Sequence[str] | str) -> ToolResponse:
    """The logic of get_quote: check the input, send the covered symbols in one request, read each answer."""
    requested = parse_symbols(symbols, max_n=MAX_SYMBOLS)
    now = deps.clock()

    refusals: dict[str, GuardError] = {}
    for symbol in requested:
        try:
            ensure_covered(symbol, "quotes", allow_uncovered=deps.settings.allow_uncovered)
        except GuardError as error:
            refusals[symbol.full] = error
    sendable = [symbol for symbol in requested if symbol.full not in refusals]

    responses: list[ApiResponse] = []
    answers: dict[str, dict[str, Any]] = {}
    if sendable:
        response = await _fetch(deps, sendable)
        responses.append(response)
        answers = _answers_by_code(response.data, sendable)

    entries: list[dict[str, Any]] = []
    problems: list[ToolWarning] = []
    quoted: list[Symbol] = []
    for symbol in requested:
        if symbol.full in refusals:
            entry, warning = _refused(symbol, refusals[symbol.full])
            problems.append(warning)
        elif symbol.full in answers:
            entry, found = _read_quote(
                symbol, answers[symbol.full], now=now, threshold=deps.settings.stale_sessions
            )
            problems.extend(found)
            if entry["status"] == "ok":
                quoted.append(symbol)
        else:
            entry = {
                "symbol": symbol.full,
                "status": "missing",
                "message": "EODHD's answer did not include this symbol.",
            }
        entries.append(entry)

    if not quoted:
        raise _no_data_error(entries, refusals, responses)
    warnings = [_delayed_warning(), *problems]
    currency = currency_note(quoted)
    if currency is not None:
        warnings.append(currency)
    return build_response(entries, responses, warnings=warnings, notes=[QUOTE_NOTE])


def register(server: MCPServer, deps: Deps) -> None:
    """Register get_quote."""

    @server.tool(
        name="get_quote", title="Delayed quotes", description=QUOTE_DESCRIPTION, annotations=READ_ONLY
    )
    @guarded(deps)
    async def get_quote(symbols: Annotated[list[str] | str, Field(description=SYMBOLS_HELP)]) -> ToolResponse:
        return await fetch_quotes(deps, symbols=symbols)
