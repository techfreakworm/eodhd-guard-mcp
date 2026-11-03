"""Input validation: turn what the model sent into values the API cannot misread.

EODHD ignores bad parameters instead of rejecting them (an invalid ``from`` date returns the whole
history, a symbol without a suffix means the US exchange), so every parameter is checked here, before a
call is made and paid for. Every failure is ``INVALID_PARAMS``, and its message names the field, the
value received and what is accepted.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from eodhd_guard_mcp.errors import ErrorCode, GuardError

# [0-9] and fullmatch on purpose: \d also matches non-ASCII digits and $ matches before a trailing newline.
_CODE_RE = re.compile(r"[A-Z0-9.\-_&^=+]+")
_EXCHANGE_RE = re.compile(r"[A-Z0-9]{1,10}")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DATETIME_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?(Z|[+-][0-9]{2}:?[0-9]{2})?)?"
)

_SYMBOL_EXAMPLES = "AAPL.US, VOD.LSE, NESN.SW, RELIANCE.NSE"
_SYMBOL_ACCEPTED = f"CODE.EXCHANGE, for example {_SYMBOL_EXAMPLES}"
_EXCHANGE_ACCEPTED = "an exchange code of 1 to 10 letters or digits, for example US, LSE, SW, NSE"
_DATE_ACCEPTED = "a calendar date written YYYY-MM-DD, for example 2026-01-15"
_DATETIME_ACCEPTED = (
    "YYYY-MM-DD, or YYYY-MM-DDTHH:MM[:SS] with an optional Z or +HH:MM offset "
    "(no offset means UTC), for example 2026-03-05T14:30:00Z"
)
_SHOW_LIMIT = 60


def _show(value: object) -> str:
    """Quote a received value for an error message, capped so a huge input is not echoed back whole."""
    text = repr(value)
    return text if len(text) <= _SHOW_LIMIT else text[: _SHOW_LIMIT - 3] + "..."


def _invalid(
    field: str, received: object, problem: str, accepted: str, *, hint: str | None = None
) -> GuardError:
    shown = _show(received)
    return GuardError(
        ErrorCode.INVALID_PARAMS,
        f"{field}: received {shown}; {problem}. Accepted: {accepted}.",
        hint=hint,
        details={"field": field, "received": shown},
    )


@dataclass(frozen=True)
class Symbol:
    """A ticker as EODHD addresses it: ``CODE.EXCHANGE``, both parts upper-case."""

    code: str
    exchange: str

    @property
    def full(self) -> str:
        return f"{self.code}.{self.exchange}"


def _parse_one(raw: object, field: str) -> Symbol:
    if not isinstance(raw, str):
        raise _invalid(field, raw, f"a symbol is text, not {type(raw).__name__}", _SYMBOL_ACCEPTED)
    text = raw.strip()
    # str.upper() folds some non-ASCII letters into ASCII ones (the German sharp s becomes 'SS'), which
    # would let a different ticker through, so non-ASCII input is refused before it is upper-cased.
    if not text.isascii():
        raise _invalid(field, raw, "it contains characters outside ASCII", _SYMBOL_ACCEPTED)
    text = text.upper()
    if not text:
        raise _invalid(field, raw, "it is empty", _SYMBOL_ACCEPTED)
    if "." not in text or text.endswith("."):
        raise _invalid(
            field,
            raw,
            "it has no exchange suffix, so EODHD would silently use the US exchange, which can return the "
            "wrong company or nothing useful",
            _SYMBOL_ACCEPTED,
            hint="Add the exchange after a dot (.US for a US stock), or find the code with search_symbols.",
        )
    code, _, exchange = text.rpartition(".")
    if not code:
        raise _invalid(field, raw, "the code before the dot is missing", _SYMBOL_ACCEPTED)
    if _CODE_RE.fullmatch(code) is None:
        hint = "Separate several symbols with commas." if any(ch.isspace() for ch in text) else None
        raise _invalid(
            field,
            raw,
            "the code has characters other than letters, digits and . - _ & ^ = +",
            _SYMBOL_ACCEPTED,
            hint=hint,
        )
    if _EXCHANGE_RE.fullmatch(exchange) is None:
        raise _invalid(
            field,
            raw,
            "the exchange suffix after the last dot must be 1 to 10 letters or digits",
            _SYMBOL_ACCEPTED,
        )
    return Symbol(code=code, exchange=exchange)


def parse_symbol(raw: str) -> Symbol:
    """Parse ``CODE.EXCHANGE``. The exchange is what follows the last dot, so ``A.B.C`` is code ``A.B``."""
    return _parse_one(raw, "symbol")


def parse_symbols(raw: Sequence[str] | str, *, max_n: int) -> list[Symbol]:
    """Parse a list, or a comma-separated string, of symbols. Blank entries are ignored, repeats dropped."""
    value: object = raw
    if isinstance(value, str):
        items: list[object] = list(value.split(","))
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise _invalid(
            "symbols",
            value,
            f"symbols are a list or a comma-separated string, not {type(value).__name__}",
            f"1 to {max_n} symbols, each {_SYMBOL_ACCEPTED}",
        )
    symbols: dict[str, Symbol] = {}
    for item in items:
        if isinstance(item, str) and not item.strip():
            continue
        symbol = _parse_one(item, "symbols")
        symbols.setdefault(symbol.full, symbol)
    if not symbols:
        raise _invalid("symbols", raw, "it names no symbol", f"1 to {max_n} symbols, each {_SYMBOL_ACCEPTED}")
    if len(symbols) > max_n:
        raise _invalid(
            "symbols",
            raw,
            f"it names {len(symbols)} different symbols, more than the {max_n} allowed per call",
            f"1 to {max_n} symbols, each {_SYMBOL_ACCEPTED}",
            hint="Split the request over several calls.",
        )
    return list(symbols.values())


def parse_exchange(raw: str) -> str:
    """Parse an exchange code such as ``US`` or ``LSE`` (upper-cased)."""
    value: object = raw
    if not isinstance(value, str):
        raise _invalid(
            "exchange", value, f"an exchange is text, not {type(value).__name__}", _EXCHANGE_ACCEPTED
        )
    text = value.strip()
    if not text.isascii() or _EXCHANGE_RE.fullmatch(text.upper()) is None:
        raise _invalid("exchange", value, "it is not an exchange code", _EXCHANGE_ACCEPTED)
    return text.upper()


def iso_date_or_none(value: object) -> date | None:
    """Read a strict ``YYYY-MM-DD`` date from an upstream row.

    Anything else gives None: EODHD uses null, ``"NA"`` and ``"0000-00-00"`` for dates it does not have.
    """
    if not isinstance(value, str):
        return None
    match = _DATE_RE.fullmatch(value)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def parse_date(value: str | None, field: str) -> date | None:
    """Parse a strict ``YYYY-MM-DD`` date; None stays None.

    Compact forms such as ``20240115`` and numbers are refused: EODHD would ignore a bad ``from`` or ``to``
    and answer with the whole history, and digits must never be read as Unix seconds.
    """
    raw: object = value
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _invalid(
            field,
            raw,
            f"a date is text, not {type(raw).__name__}; numbers are never read as dates or Unix seconds",
            _DATE_ACCEPTED,
        )
    text = raw.strip()
    parsed = iso_date_or_none(text)
    if parsed is not None:
        return parsed
    problem = "it is not a real calendar date" if _DATE_RE.fullmatch(text) else "it is not written YYYY-MM-DD"
    raise _invalid(field, raw, problem, _DATE_ACCEPTED, hint="Leave the field out to use the default.")


def _utc_offset(text: str | None) -> timezone:
    if text is None or text == "Z":
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:])
    if hours > 23 or minutes > 59:
        raise ValueError(f"offset {text} is out of range")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_datetime_utc(value: str | None, field: str) -> datetime | None:
    """Parse a date or an ISO 8601 date-time into an aware UTC ``datetime``; None stays None.

    A bare date means 00:00 UTC, and a time without an offset is read as UTC.
    """
    raw: object = value
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _invalid(
            field,
            raw,
            f"a date-time is text, not {type(raw).__name__}; numbers are never read as Unix seconds",
            _DATETIME_ACCEPTED,
        )
    match = _DATETIME_RE.fullmatch(raw.strip())
    if match is None:
        raise _invalid(field, raw, "it is not an ISO 8601 date or date-time", _DATETIME_ACCEPTED)
    year, month, day, hour, minute, second, offset = match.groups()
    try:
        moment = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=_utc_offset(offset),
        )
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _invalid(field, raw, "it is not a real date and time", _DATETIME_ACCEPTED) from None


@dataclass(frozen=True)
class DateRange:
    """A resolved window. ``clamped`` is True when a future end date was pulled back to today."""

    start: date
    end: date
    clamped: bool


def _days_before(day: date, days: int) -> date:
    try:
        return day - timedelta(days=days)
    except OverflowError:
        return date.min


def resolve_range(
    start: date | None,
    end: date | None,
    *,
    today: date,
    default_days: int,
    max_days: int | None = None,
) -> DateRange:
    """Fill in and check a date window.

    ``end`` defaults to today and is clamped to it, because there is no data after today. ``start`` defaults
    to ``default_days`` before ``end``. A start after the end, or a span over ``max_days``, is refused.
    """
    clamped = end is not None and end > today
    last = today if end is None or clamped else end
    first = _days_before(last, default_days) if start is None else start
    if first > today:
        raise _invalid(
            "start",
            first.isoformat(),
            "it is in the future, and no data exists after today",
            f"a date on or before {today.isoformat()}",
        )
    if first > last:
        raise _invalid(
            "start",
            first.isoformat(),
            f"it is after the end date {last.isoformat()}",
            "a start date on or before the end date",
        )
    span = (last - first).days
    if max_days is not None and span > max_days:
        raise _invalid(
            "start/end",
            f"{first.isoformat()} to {last.isoformat()}",
            f"the range spans {span} days, more than the {max_days} allowed",
            f"a range of at most {max_days} days",
            hint="Ask for a shorter window, or make several calls.",
        )
    return DateRange(start=first, end=last, clamped=clamped)


def check_int(value: int, *, lo: int, hi: int, field: str) -> int:
    """Check a whole number is within ``lo..hi``. ``True`` and ``False`` are not numbers here."""
    number: object = value
    accepted = f"a whole number from {lo} to {hi}"
    if isinstance(number, bool) or not isinstance(number, int):
        raise _invalid(field, number, f"it is {type(number).__name__}, not a whole number", accepted)
    if not lo <= number <= hi:
        raise _invalid(field, number, "it is out of range", accepted)
    return number
