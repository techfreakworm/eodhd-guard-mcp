"""Tool: get_fundamentals.

EODHD answers a fundamentals request with the whole company document (about a megabyte for a large stock)
and bills 10 calls for it, whichever sections were asked for. So the tool fetches the document once, with
no filter, which gives every request for a symbol the same cache entry for 24 hours, and cuts the sections
out of it here. Asking again for other sections, statements or periods then costs nothing.

The cutting is the point: officers, addresses and long histories are dropped, statements keep their newest
periods without the empty fields, and the result stops at about 150 KB. The guards look at what is
returned: a statement in another currency than the quote, a ``filing_date`` that is only the period end,
a dividend yield of 0 next to a forward dividend, and EPS on the analyst basis.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from eodhd_guard_mcp.costs import cost_for
from eodhd_guard_mcp.coverage import ensure_covered
from eodhd_guard_mcp.envelope import ToolResponse, ToolWarning, WarningCode, build_response
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.tools import READ_ONLY, guarded
from eodhd_guard_mcp.validation import Symbol, check_int, parse_symbol

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.client import ApiResponse
    from eodhd_guard_mcp.server import Deps


def _fold(name: str) -> str:
    """A name without case, spaces, underscores or hyphens, so ``Shares Stats`` finds ``SharesStats``."""
    return re.sub(r"[\s_\-]+", "", name).lower()


CACHE_TTL_S = 24 * 3600.0
OUTPUT_CAP_BYTES = 150_000
DESCRIPTION_CHARS = 500
TOP_HOLDERS = 10
RECENT_INSIDER_TRANSACTIONS = 10
DIVIDEND_YEARS = 5
MAX_PERIODS = 40

Kind = Literal["stock", "etf", "fund"]

SECTIONS = (
    "General",
    "Highlights",
    "Valuation",
    "SharesStats",
    "Technicals",
    "SplitsDividends",
    "AnalystRatings",
    "Holders",
    "InsiderTransactions",
    "ESGScores",
    "outstandingShares",
    "Earnings",
    "ShortPositionDisclosure",
    "ETF_Data",
    "MutualFund_Data",
)
DEFAULT_SECTIONS: Mapping[Kind, tuple[str, ...]] = {
    "stock": ("General", "Highlights", "Valuation", "SharesStats", "SplitsDividends", "Earnings"),
    "etf": ("General", "Technicals", "ETF_Data"),
    "fund": ("General", "MutualFund_Data"),
}
STATEMENTS = {"income": "Income_Statement", "balance": "Balance_Sheet", "cashflow": "Cash_Flow"}
_STATEMENT_ALIASES = {
    "income": "income",
    "incomestatement": "income",
    "balance": "balance",
    "balancesheet": "balance",
    "cashflow": "cashflow",
}
_GENERAL_DROPPED = ("Officers", "AddressData", "Phone", "LogoURL")
_BLANK_STRINGS = frozenset({"", "NA"})
_DATE_LIKE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SECTION_NAMES = {_fold(name): name for name in SECTIONS}
_SECTIONS_ACCEPTED = ", ".join(SECTIONS)
_STATEMENTS_ACCEPTED = "income, balance, cashflow"

DESCRIPTION = (
    "Company fundamentals for a stock, ETF or fund, cut down to a small answer. The first read of a symbol "
    "costs 10 API calls and fetches EODHD's whole document, which is cached for 24 hours, so reading other "
    "sections, statements, periods or the yearly view of the same symbol afterwards costs 0. Stocks return "
    "General, Highlights, Valuation, SharesStats, SplitsDividends and Earnings by default (ETFs: General, "
    "Technicals, ETF_Data; funds: General, MutualFund_Data); add income, balance and cashflow statements "
    "with `statements`. It warns when statements are in another currency than the quote (London lines are "
    "quoted in pence), when filing_date is only the period end, when a dividend yield of 0 sits next to a "
    "forward dividend, and that EPS actuals use the analyst basis."
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


def _names(
    raw: object,
    field_name: str,
    lookup: Mapping[str, str],
    accepted: str,
    *,
    refused: Mapping[str, str] | None = None,
) -> list[str] | None:
    """Names from a list or a comma-separated string, as the canonical spellings, without repeats.

    None stays None (the caller's default); an empty list stays empty. ``refused`` maps a folded name to
    the hint for a name that is real but belongs to another parameter.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        items: list[object] = list(raw.split(","))
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise _invalid(field_name, raw, f"it is {type(raw).__name__}, not a list of names", accepted)
    chosen: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise _invalid(field_name, item, f"a name is text, not {type(item).__name__}", accepted)
        if not item.strip():
            continue
        folded = _fold(item)
        canonical = lookup.get(folded)
        if canonical is None:
            raise _invalid(
                field_name,
                item,
                "it is not a name this tool knows",
                accepted,
                hint=(refused or {}).get(folded),
            )
        if canonical not in chosen:
            chosen.append(canonical)
    return chosen


def parse_sections(raw: object) -> list[str] | None:
    """The sections to return, or None for the defaults of the instrument."""
    return _names(
        raw,
        "sections",
        _SECTION_NAMES,
        _SECTIONS_ACCEPTED,
        refused={
            "financials": (
                "Financial statements are chosen with the statements parameter "
                f"({_STATEMENTS_ACCEPTED}), not with sections."
            )
        },
    )


def parse_statements(raw: object) -> list[str]:
    """The statements to return, as keys of ``STATEMENTS``; none by default."""
    return _names(raw, "statements", _STATEMENT_ALIASES, _STATEMENTS_ACCEPTED) or []


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _blank(value: object) -> bool:
    """EODHD's empty values: null, an empty string and the placeholder ``"NA"``."""
    return value is None or (isinstance(value, str) and value.strip() in _BLANK_STRINGS)


def _text(value: object) -> str | None:
    """A non-blank string, stripped; anything else is None."""
    if isinstance(value, str) and not _blank(value):
        return value.strip()
    return None


def _number(value: object) -> float | None:
    """A finite number from a JSON value; strings and booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_size(value: object) -> int:
    """Bytes of the compact UTF-8 JSON for ``value``, which is what the model is charged for."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _index_ordered(value: Any) -> list[Any]:
    """The values of an index-keyed dict (``"0"``, ``"1"``, ...) in index order; a list passes through."""
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, dict):
        return []

    def position(key: object) -> tuple[bool, int, str]:
        text = str(key)
        return (not text.isdigit(), int(text) if text.isdigit() else 0, text)

    return [value[key] for key in sorted(value, key=position)]


def _newest_first(value: Any, date_fields: Sequence[str]) -> list[tuple[str | None, dict[str, Any]]]:
    """Rows of a date-keyed dict (or of a list) as ``(key, row)``, newest first.

    The date comes from the first non-blank field of ``date_fields`` in the row, else from the key. EODHD
    lists these newest first today, but nothing promises it, so the order is made here.
    """
    if isinstance(value, dict):
        pairs: list[tuple[str | None, Any]] = [(str(key), row) for key, row in value.items()]
    elif isinstance(value, list):
        pairs = [(None, row) for row in value]
    else:
        return []
    items = [(key, row) for key, row in pairs if isinstance(row, dict)]

    def stamp(item: tuple[str | None, dict[str, Any]]) -> str:
        key, row = item
        for name in date_fields:
            text = _text(row.get(name))
            if text:
                return text
        return key if key is not None and _DATE_LIKE.fullmatch(key) else ""

    return sorted(items, key=stamp, reverse=True)


def _dated(key: str | None, row: dict[str, Any]) -> dict[str, Any]:
    """The row, with its key as ``date`` when the row does not carry one."""
    if "date" in row or key is None or not _DATE_LIKE.fullmatch(key):
        return dict(row)
    return {"date": key, **row}


# --- trimming ---------------------------------------------------------------------------------------


@dataclass
class _Context:
    """What the trimmers need to know, and where they report what they cut."""

    periods: int
    trims: list[str] = field(default_factory=list)


def _trim_general(value: Any, ctx: _Context) -> dict[str, Any]:
    general = _mapping(value)
    out = {name: item for name, item in general.items() if name not in _GENERAL_DROPPED}
    cuts = []
    dropped = [name for name in _GENERAL_DROPPED if name in general]
    if dropped:
        cuts.append(f"{', '.join(dropped)} dropped")
    if "Listings" in out:
        out["Listings"] = _index_ordered(out["Listings"])
    description = out.get("Description")
    if isinstance(description, str) and len(description) > DESCRIPTION_CHARS:
        out["Description"] = description[: DESCRIPTION_CHARS - 3].rstrip() + "..."
        cuts.append(f"Description cut to {DESCRIPTION_CHARS} characters")
    if cuts:
        ctx.trims.append(f"General: {', '.join(cuts)}")
    return out


def _trim_splits_dividends(value: Any, ctx: _Context) -> dict[str, Any]:
    section = dict(_mapping(value))
    if "NumberDividendsByYear" in section:
        rows = [row for row in _index_ordered(section["NumberDividendsByYear"]) if isinstance(row, dict)]
        rows.sort(key=lambda row: _number(row.get("Year")) or 0, reverse=True)
        if len(rows) > DIVIDEND_YEARS:
            ctx.trims.append(f"SplitsDividends.NumberDividendsByYear: newest {DIVIDEND_YEARS} years")
        section["NumberDividendsByYear"] = rows[:DIVIDEND_YEARS]
    return section


def _trim_holders(value: Any, ctx: _Context) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for group, rows in _mapping(value).items():
        ordered = _index_ordered(rows)
        if len(ordered) > TOP_HOLDERS:
            ctx.trims.append(f"Holders.{group}: top {TOP_HOLDERS}")
        out[group] = ordered[:TOP_HOLDERS]
    return out


def _trim_insiders(value: Any, ctx: _Context) -> list[Any]:
    rows = [row for row in _index_ordered(value) if isinstance(row, dict)]
    rows.sort(key=lambda row: _text(row.get("date")) or _text(row.get("transactionDate")) or "", reverse=True)
    if len(rows) > RECENT_INSIDER_TRANSACTIONS:
        ctx.trims.append(f"InsiderTransactions: newest {RECENT_INSIDER_TRANSACTIONS}")
    return rows[:RECENT_INSIDER_TRANSACTIONS]


def _trim_earnings(value: Any, ctx: _Context) -> dict[str, Any]:
    out = dict(_mapping(value))
    for name in ("History", "Trend", "Annual"):
        if name in out:
            items = _newest_first(out[name], ("date",))[: ctx.periods]
            out[name] = [_dated(key, row) for key, row in items]
    return out


def _trim_outstanding_shares(value: Any, ctx: _Context) -> dict[str, Any]:
    out = dict(_mapping(value))
    for name in ("annual", "quarterly"):
        if name in out:
            items = _newest_first(out[name], ("dateFormatted", "date"))[: ctx.periods]
            out[name] = [row for _, row in items]
    return out


def _trim_holdings(section: str, value: Any, ctx: _Context) -> dict[str, Any]:
    """``ETF_Data`` and ``MutualFund_Data`` without ``Holdings``: a full position list can be thousands."""
    out = dict(_mapping(value))
    if "Holdings" in out:
        count = len(out.pop("Holdings") or ())
        ctx.trims.append(f"{section}.Holdings ({count} positions) dropped, the top holdings stay")
    return out


_TRIMMERS: Mapping[str, Callable[[Any, _Context], Any]] = {
    "General": _trim_general,
    "SplitsDividends": _trim_splits_dividends,
    "Holders": _trim_holders,
    "InsiderTransactions": _trim_insiders,
    "Earnings": _trim_earnings,
    "outstandingShares": _trim_outstanding_shares,
    "ETF_Data": lambda value, ctx: _trim_holdings("ETF_Data", value, ctx),
    "MutualFund_Data": lambda value, ctx: _trim_holdings("MutualFund_Data", value, ctx),
}


def _period_row(key: str | None, record: Mapping[str, Any], currency: str | None) -> dict[str, Any]:
    """One statement period: its dates, then every field that has a value.

    ``currency_symbol`` is kept, as ``currency``, only when the period differs from the statement's.
    """
    row: dict[str, Any] = {"date": _text(record.get("date")) or key}
    filing = _text(record.get("filing_date"))
    if filing:
        row["filing_date"] = filing
    own = _text(record.get("currency_symbol"))
    if own and own != currency:
        row["currency"] = own
    for name, value in record.items():
        if name not in ("date", "filing_date", "currency_symbol") and not _blank(value):
            row[name] = value
    return row


def _statement(financials: Mapping[str, Any], name: str, period: str, periods: int) -> dict[str, Any] | None:
    """``{currency, periods: [...]}`` for one statement, or None when the document has no such period."""
    block = _mapping(financials.get(name))
    items = _newest_first(block.get(period), ("date",))[:periods]
    if not items:
        return None
    currency = _text(block.get("currency_symbol")) or next(
        (own for _, row in items if (own := _text(row.get("currency_symbol")))), None
    )
    return {"currency": currency, "periods": [_period_row(key, row, currency) for key, row in items]}


# --- the cap ----------------------------------------------------------------------------------------


def _series(data: Mapping[str, Any]) -> list[tuple[str, list[Any]]]:
    """The newest-first lists that a too large answer can lose its oldest entries from, one label each."""
    found: list[tuple[str, list[Any]]] = []
    sections = _mapping(data.get("sections"))
    for section, names in (
        ("Earnings", ("History", "Trend", "Annual")),
        ("outstandingShares", ("annual", "quarterly")),
    ):
        block = _mapping(sections.get(section))
        found.extend(
            (f"{section}.{name}", block[name]) for name in names if isinstance(block.get(name), list)
        )
    for short, statement in _mapping(data.get("statements")).items():
        found.append((f"statements.{short}", statement["periods"]))
    return found


def _omit_sections(data: dict[str, Any], cap: int) -> list[str]:
    """Drop the largest section or statement, never General, until ``data`` fits. The last resort."""
    omitted: list[str] = []
    while _json_size(data) > cap:
        options = [
            ("sections", name, _json_size(value))
            for name, value in _mapping(data.get("sections")).items()
            if name != "General"
        ]
        options += [
            ("statements", name, _json_size(value))
            for name, value in _mapping(data.get("statements")).items()
        ]
        if not options:
            break
        bucket, name, _ = max(options, key=lambda option: option[2])
        del data[bucket][name]
        omitted.append(name if bucket == "sections" else f"statements.{name}")
    return omitted


def _enforce_cap(data: dict[str, Any], cap: int) -> ToolWarning | None:
    """Make ``data`` fit in ``cap`` bytes: oldest periods go first, whole sections only as a last resort.

    Each round drops the oldest entry of the largest list, so no series is emptied while a larger one can
    still give. Entry sizes are measured once and then subtracted, which is exact because a list never
    drops to empty, so a large document is not serialised over and over. Returns the TRUNCATED warning,
    or None when nothing had to go.
    """
    before = _json_size(data)
    if before <= cap:
        return None
    series = _series(data)
    original = {label: len(rows) for label, rows in series}
    weights = {label: [_json_size(row) + 1 for row in rows] for label, rows in series}
    total = before
    while total > cap:
        candidates = [(label, rows) for label, rows in series if len(rows) > 1]
        if not candidates:
            break
        label, rows = max(candidates, key=lambda candidate: sum(weights[candidate[0]]))
        rows.pop()
        total -= weights[label].pop()
    omitted = _omit_sections(data, cap) if total > cap else []
    alive = {label for label, _ in _series(data)}
    cut = {
        label: f"newest {len(rows)} of {original[label]}"
        for label, rows in series
        if label in alive and len(rows) < original[label]
    }
    parts = []
    if cut:
        parts.append(
            "Older periods were cut (" + ", ".join(f"{label}: {kept}" for label, kept in cut.items()) + ")."
        )
    if omitted:
        parts.append("Whole sections were dropped (" + ", ".join(omitted) + ").")
    return ToolWarning.of(
        WarningCode.TRUNCATED,
        f"The answer was {before // 1000} KB, over the {cap // 1000} KB cap. "
        + (" ".join(parts) if parts else "Nothing more could be cut.")
        + " Ask again for fewer `statements` or `sections`, or a smaller `periods`, to keep more of each: "
        "the document stays cached for 24 hours, so that costs nothing.",
        limit_bytes=cap,
        size_before=before,
        size_after=_json_size(data),
        cut=cut,
        omitted=omitted,
    )


# --- guards -----------------------------------------------------------------------------------------


def _currency_warning(quote: str | None, statements: Mapping[str, Any]) -> ToolWarning | None:
    """CURRENCY_MISMATCH when a returned statement period is not in the currency the line is quoted in."""
    if quote is None:
        return None
    reported: dict[str, list[str]] = {}
    for short, statement in statements.items():
        used: list[str] = []
        for row in statement["periods"]:
            currency = row.get("currency") or statement["currency"]
            if currency and currency not in used:
                used.append(currency)
        reported[short] = used
    differing = {
        short: used for short, used in reported.items() if any(c.upper() != quote.upper() for c in used)
    }
    if not differing:
        return None
    listing = "; ".join(f"{short}: {', '.join(used)}" for short, used in differing.items())
    pence = (
        " GBX is pence, so a figure in GBP differs from a price in GBX by a factor of 100."
        if quote.upper() == "GBX"
        else ""
    )
    mixed = any(len(used) > 1 for used in differing.values())
    return ToolWarning.of(
        WarningCode.CURRENCY_MISMATCH,
        f"The line is quoted in {quote}, but the statements are reported in a different currency "
        f"({listing}){' and one statement mixes currencies across periods' if mixed else ''}. "
        "Do not combine a price with statement figures (P/E, yield, market value) without converting "
        f"first.{pence}",
        quote_currency=quote,
        statement_currencies=differing,
    )


def _filing_date_warning(statements: Mapping[str, Any]) -> ToolWarning | None:
    """FILING_DATE_UNRELIABLE when a returned period has ``filing_date`` equal to its period end."""
    total = 0
    affected: dict[str, int] = {}
    for short, statement in statements.items():
        for row in statement["periods"]:
            total += 1
            if row.get("filing_date") is not None and row.get("filing_date") == row.get("date"):
                affected[short] = affected.get(short, 0) + 1
    if not affected:
        return None
    count = sum(affected.values())
    return ToolWarning.of(
        WarningCode.FILING_DATE_UNRELIABLE,
        f"In {count} of {total} returned statement periods filing_date equals the period end date, so it "
        "is not the date the figures were published (EODHD fills in the period end for many markets). Do "
        "not use filing_date for point-in-time work; assume a reporting lag of weeks instead.",
        periods_affected=count,
        periods_returned=total,
        statements=sorted(affected),
    )


def _dividend_yield_warning(doc: Mapping[str, Any], sections: Mapping[str, Any]) -> ToolWarning | None:
    """DIVIDEND_YIELD_INCONSISTENT when the yield shown is 0 but a forward dividend rate is set."""
    if "Highlights" not in sections:
        return None
    dividend_yield = _number(_mapping(doc.get("Highlights")).get("DividendYield"))
    rate = _number(_mapping(doc.get("SplitsDividends")).get("ForwardAnnualDividendRate"))
    if dividend_yield != 0 or rate is None or rate <= 0:
        return None
    return ToolWarning.of(
        WarningCode.DIVIDEND_YIELD_INCONSISTENT,
        f"Highlights.DividendYield is 0, but SplitsDividends.ForwardAnnualDividendRate is {rate:g}. The "
        "yield field is not reliable here: do not conclude that the stock pays no dividend. Work out the "
        "yield from the forward rate and the price, or look at get_dividends.",
        dividend_yield=0,
        forward_annual_dividend_rate=rate,
    )


def _eps_warning() -> ToolWarning:
    return ToolWarning.of(
        WarningCode.EPS_BASIS,
        "EPS actuals in Earnings follow the analyst (adjusted) basis, which can differ from the EPS in the "
        "reported statements (GAAP or IFRS). Do not mix the two without checking.",
    )


# --- the document -----------------------------------------------------------------------------------


def _kind(doc: Mapping[str, Any]) -> Kind:
    """Stock, ETF or fund: from ``General.Type``, else from which instrument section the document has."""
    kind = str(_mapping(doc.get("General")).get("Type") or "").strip().lower()
    if kind == "etf":
        return "etf"
    if kind in ("fund", "mutual fund"):
        return "fund"
    if "ETF_Data" in doc:
        return "etf"
    return "fund" if "MutualFund_Data" in doc else "stock"


def _document(data: Any, symbol: Symbol, cost: int) -> dict[str, Any]:
    """The company document, or the error for an answer that is empty or not a document."""
    if isinstance(data, dict) and data:
        return data
    if isinstance(data, (dict, list)) and not data:
        billing = (
            f"The request was billed ({cost} calls) and the empty answer is cached for 24 hours."
            if cost
            else "This empty answer comes from the cache, where an earlier billed request left it; it is "
            "fetched again 24 hours after that."
        )
        raise GuardError(
            ErrorCode.NO_DATA,
            f"{symbol.full}: EODHD answered HTTP 200 with an empty document, not an error. The code may be "
            f"delisted, renamed, or not covered by the plan. {billing}",
            hint="Run check_symbol to see if the code is live, or search_symbols to find its current code.",
            details={"symbol": symbol.full},
        )
    raise GuardError(
        ErrorCode.BAD_RESPONSE,
        f"{symbol.full}: EODHD's fundamentals answer is a {type(data).__name__}, not a company document.",
        hint="This is usually temporary; retry in a minute.",
        details={"symbol": symbol.full},
    )


@dataclass(frozen=True)
class Built:
    """The trimmed answer, with the warnings and notes that describe it."""

    data: dict[str, Any]
    warnings: list[ToolWarning]
    notes: list[str]


def build_fundamentals(
    doc: Mapping[str, Any],
    symbol: Symbol,
    sections: Sequence[str] | None,
    statements: Sequence[str],
    period: str,
    periods: int,
) -> Built:
    """Cut the requested sections and statements out of ``doc`` and run the guards on what is returned."""
    kind = _kind(doc)
    general = _mapping(doc.get("General"))
    ctx = _Context(periods=periods)

    if sections is None:
        chosen, missing_sections = [name for name in DEFAULT_SECTIONS[kind] if name in doc], []
    else:
        chosen = [name for name in sections if name in doc]
        missing_sections = [name for name in sections if name not in doc]
    returned: dict[str, Any] = {}
    for name in chosen:
        trimmer = _TRIMMERS.get(name)
        returned[name] = trimmer(doc[name], ctx) if trimmer is not None else doc[name]

    financials = _mapping(doc.get("Financials"))
    built_statements: dict[str, Any] = {}
    missing_statements: list[str] = []
    for short in statements:
        statement = _statement(financials, STATEMENTS[short], period, periods)
        if statement is None:
            missing_statements.append(short)
        else:
            built_statements[short] = statement

    data: dict[str, Any] = {
        "symbol": symbol.full,
        "name": general.get("Name"),
        "type": general.get("Type"),
        "quote_currency": _text(general.get("CurrencyCode")),
        "updated_at": _text(general.get("UpdatedAt")),
        "sections": returned,
    }
    if statements:
        data["period"] = period
        data["statements"] = built_statements
    if missing_sections or missing_statements:
        data["not_in_document"] = {
            name: items
            for name, items in (("sections", missing_sections), ("statements", missing_statements))
            if items
        }

    warnings: list[ToolWarning] = []
    truncated = _enforce_cap(data, OUTPUT_CAP_BYTES)
    if truncated is not None:
        warnings.append(truncated)
    final_statements = _mapping(data.get("statements"))
    final_sections = _mapping(data.get("sections"))
    for guard in (
        _currency_warning(data["quote_currency"], final_statements),
        _filing_date_warning(final_statements),
        _dividend_yield_warning(doc, final_sections),
        _eps_warning() if "Earnings" in final_sections else None,
    ):
        if guard is not None:
            warnings.append(guard)

    return Built(data=data, warnings=warnings, notes=_notes(kind, data, ctx))


def _notes(kind: Kind, data: Mapping[str, Any], ctx: _Context) -> list[str]:
    notes: list[str] = []
    updated = data["updated_at"]
    if kind == "stock":
        notes.append(
            "New quarters and years usually reach EODHD 1 to 4 days after the company's release, so a "
            "missing latest period just after results is normal."
            + (f" This document was last updated {updated} (General.UpdatedAt)." if updated else "")
        )
    elif updated:
        notes.append(f"This document was last updated {updated} (General.UpdatedAt).")
    history = _mapping(data["sections"].get("Earnings")).get("History")
    if isinstance(history, list) and any(
        isinstance(row, dict) and row.get("epsActual") is None for row in history
    ):
        notes.append(
            "Earnings.History also lists quarters that have not reported yet (epsActual is null); they count "
            "towards `periods`."
        )
    if ctx.trims:
        notes.append("Cut to keep the answer small: " + "; ".join(ctx.trims) + ".")
    absent = data.get("not_in_document")
    if absent:
        listing = "; ".join(f"{kind_name}: {', '.join(items)}" for kind_name, items in absent.items())
        notes.append(f"Not in EODHD's document for this symbol, so not returned ({listing}).")
    return notes


async def _fetch(deps: Deps, symbol: Symbol) -> ApiResponse:
    """The whole document. No ``filter`` is sent, so every request for the symbol shares one cache entry."""
    try:
        return await deps.client.get(
            f"fundamentals/{symbol.full}",
            None,
            endpoint="fundamentals",
            cost=cost_for("fundamentals"),
            ttl_s=CACHE_TTL_S,
            symbol=symbol.full,
        )
    except GuardError as exc:
        if exc.code is not ErrorCode.NOT_FOUND:
            raise
        raise GuardError(
            ErrorCode.NOT_FOUND,
            f"{symbol.full}: {exc.message}",
            hint=(
                "Check the code with search_symbols. Delisted lines are listed by "
                "list_exchange_symbols(delisted=true)."
            ),
            details=exc.details,
        ) from None


async def fundamentals(
    deps: Deps,
    symbol_text: str,
    sections: Sequence[str] | str | None,
    statements: Sequence[str] | str | None,
    period: str,
    periods: int,
) -> ToolResponse:
    """Validate, fetch the document (or read it from the cache) and answer from it."""
    symbol = parse_symbol(symbol_text)
    ensure_covered(symbol, "fundamentals", allow_uncovered=deps.settings.allow_uncovered)
    wanted_sections = parse_sections(sections)
    wanted_statements = parse_statements(statements)
    count = check_int(periods, lo=1, hi=MAX_PERIODS, field="periods")
    if wanted_sections == [] and not wanted_statements:
        raise _invalid(
            "sections/statements",
            "no sections and no statements",
            "nothing is requested, so there is nothing to return",
            f"sections from {_SECTIONS_ACCEPTED}; or statements from {_STATEMENTS_ACCEPTED}",
            hint="Leave sections out to get the defaults for the instrument.",
        )
    response = await _fetch(deps, symbol)
    doc = _document(response.data, symbol, response.cost)
    built = build_fundamentals(doc, symbol, wanted_sections, wanted_statements, period, count)
    if response.cached:
        cache_note = (
            "Served from the local cache. Other sections, statements, periods or the yearly view of this "
            "symbol cost 0 until the cached document expires, 24 hours after the first fetch (see "
            "meta.fetched_at)."
        )
    else:
        cache_note = (
            f"The whole document was fetched once ({response.cost} calls) and is cached for 24 hours. Other "
            "sections, statements, periods or the yearly view of this symbol cost 0 until then."
        )
    notes = [cache_note, *built.notes]
    return build_response(built.data, [response], warnings=built.warnings, notes=notes)


def register(server: MCPServer, deps: Deps) -> None:
    """Register ``get_fundamentals``."""

    @server.tool(
        name="get_fundamentals",
        title="Company fundamentals",
        description=DESCRIPTION,
        annotations=READ_ONLY,
    )
    @guarded(deps)
    async def get_fundamentals(
        symbol: Annotated[
            str,
            Field(description="The line as CODE.EXCHANGE, for example AAPL.US, NESN.SW or VOD.LSE."),
        ],
        sections: Annotated[
            list[str] | str | None,
            Field(
                description=(
                    "Sections to return, as a list or a comma-separated string, from "
                    f"{_SECTIONS_ACCEPTED}. Leave out for the defaults of the instrument (stock, ETF or "
                    "fund). Pass [] for no sections when only statements are wanted. Statements are not "
                    "a section: use `statements`."
                )
            ),
        ] = None,
        statements: Annotated[
            list[str] | str | None,
            Field(
                description=(
                    "Financial statements to add: any of income, balance, cashflow, as a list or a "
                    "comma-separated string. None by default. Each returns the newest `periods` periods "
                    "without empty fields."
                )
            ),
        ] = None,
        period: Annotated[
            Literal["quarterly", "yearly"],
            Field(description="Whether the statements use quarterly or yearly periods."),
        ] = "quarterly",
        periods: Annotated[
            int,
            Field(
                description=(
                    "How many of the newest periods to keep in each statement and in each Earnings list "
                    "(History, Trend, Annual), from 1 to 40. Default 4."
                )
            ),
        ] = 4,
    ) -> ToolResponse:
        return await fundamentals(deps, symbol, sections, statements, period, periods)
