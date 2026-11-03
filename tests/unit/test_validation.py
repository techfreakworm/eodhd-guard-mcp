"""Unit tests for eodhd_guard_mcp.validation."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

import pytest

from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.validation import (
    DateRange,
    Symbol,
    check_int,
    iso_date_or_none,
    parse_date,
    parse_datetime_utc,
    parse_exchange,
    parse_symbol,
    parse_symbols,
    resolve_range,
)

TODAY = date(2026, 11, 3)

# Look-alike characters are built with chr() so this file stays pure ASCII.
SHARP_S = chr(0x00DF)  # str.upper() turns it into "SS"
DOTLESS_I = chr(0x0131)  # str.upper() turns it into a plain "I"
A_GRAVE = chr(0x00C0)
U_UMLAUT = chr(0x00DC)


def fullwidth(text: str) -> str:
    """Re-spell ASCII text with the fullwidth look-alikes U+FF01 to U+FF5E."""
    return "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text)


def arabic_indic(text: str) -> str:
    """Re-spell the digits of ``text`` as Arabic-Indic digits, which ``int()`` and ``\\d`` accept."""
    return "".join(chr(0x0660 + int(ch)) if ch.isdigit() else ch for ch in text)


def invalid(call: Callable[..., Any], *args: Any, **kwargs: Any) -> GuardError:
    """Run a call that must be refused with INVALID_PARAMS and return the error for inspection."""
    with pytest.raises(GuardError) as info:
        call(*args, **kwargs)
    assert info.value.code is ErrorCode.INVALID_PARAMS
    return info.value


# --------------------------------------------------------------------------------------------- symbols


def test_symbol_full_joins_code_and_exchange() -> None:
    assert Symbol(code="AAPL", exchange="US").full == "AAPL.US"


@pytest.mark.parametrize(
    ("raw", "code", "exchange"),
    [
        ("AAPL.US", "AAPL", "US"),
        ("BRK-B.US", "BRK-B", "US"),
        ("0R2V.LSE", "0R2V", "LSE"),
        ("M&M.NSE", "M&M", "NSE"),
        ("EURUSD.FOREX", "EURUSD", "FOREX"),
        ("BTC-USD.CC", "BTC-USD", "CC"),
        ("GSPC.INDX", "GSPC", "INDX"),
        ("7203.TSE", "7203", "TSE"),
        ("aapl.us", "AAPL", "US"),
        ("  vod.lse\n", "VOD", "LSE"),
        ("A.B.C", "A.B", "C"),
        ("BRK.B.US", "BRK.B", "US"),
        ("A^B=C+D_E.US", "A^B=C+D_E", "US"),
    ],
)
def test_parse_symbol_accepts_real_shapes(raw: str, code: str, exchange: str) -> None:
    assert parse_symbol(raw) == Symbol(code=code, exchange=exchange)


def test_parse_symbol_takes_the_exchange_from_the_last_dot() -> None:
    symbol = parse_symbol("BRK.B.US")
    assert symbol.exchange == "US"
    assert symbol.full == "BRK.B.US"


@pytest.mark.parametrize("raw", ["AAPL", "BRK-B", "aapl", "AAPL.", " vod "])
def test_bare_code_is_refused_with_the_us_default_explained(raw: str) -> None:
    error = invalid(parse_symbol, raw)
    assert "no exchange suffix" in error.message
    assert "silently use the US exchange" in error.message
    for example in ("AAPL.US", "VOD.LSE", "NESN.SW", "RELIANCE.NSE"):
        assert example in error.message
    assert error.hint is not None
    assert "search_symbols" in error.hint


def test_invalid_symbol_message_names_field_and_received_value() -> None:
    error = invalid(parse_symbol, "AAPL")
    assert error.message.startswith("symbol: received 'AAPL'")
    assert "Accepted: CODE.EXCHANGE" in error.message
    assert error.details == {"field": "symbol", "received": "'AAPL'"}


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "empty"),
        ("   ", "empty"),
        (".US", "code before the dot is missing"),
        (".", "no exchange suffix"),
        ("AAPL.TOOLONGEXCH", "1 to 10 letters or digits"),
        ("AAPL.U-S", "1 to 10 letters or digits"),
        ("AAPL.U S", "1 to 10 letters or digits"),
        ("AAPL.US/X", "1 to 10 letters or digits"),
        ("AAPL.US/../X", "characters other than"),
        ("AAP L.US", "characters other than"),
        ("AAPL;.US", "characters other than"),
        ("AAPL$.US", "characters other than"),
        ("AAPL/.US", "characters other than"),
        ("../X.US", "characters other than"),
        ("AAPL\n.US", "characters other than"),
        ("AAPL\x00.US", "characters other than"),
        ("AAPL,MSFT.US", "characters other than"),
    ],
)
def test_parse_symbol_refuses_malformed_input(raw: str, fragment: str) -> None:
    error = invalid(parse_symbol, raw)
    assert fragment in error.message


@pytest.mark.parametrize(
    "raw",
    [
        f"STRA{SHARP_S}E.XETRA",
        fullwidth("AAPL") + ".US",
        f"{A_GRAVE}APL.US",
        f"AAPL.{U_UMLAUT}S",
        f"APPLE.{DOTLESS_I}S",
    ],
)
def test_parse_symbol_refuses_non_ascii_before_upper_casing(raw: str) -> None:
    error = invalid(parse_symbol, raw)
    assert "outside ASCII" in error.message


@pytest.mark.parametrize("raw", [None, 123, ["AAPL.US"], b"AAPL.US"])
def test_parse_symbol_refuses_non_text(raw: Any) -> None:
    error = invalid(parse_symbol, raw)
    assert "is text" in error.message


def test_space_separated_symbols_get_a_comma_hint() -> None:
    error = invalid(parse_symbol, "AAPL.US MSFT.US")
    assert error.hint is not None
    assert "commas" in error.hint


def test_huge_input_is_not_echoed_back_whole() -> None:
    error = invalid(parse_symbol, "X" * 5000)
    assert len(error.message) < 400
    assert "..." in error.message


# ------------------------------------------------------------------------------------------ symbol lists


def test_parse_symbols_accepts_a_comma_string() -> None:
    assert parse_symbols("AAPL.US, msft.us ,VOD.LSE", max_n=5) == [
        Symbol("AAPL", "US"),
        Symbol("MSFT", "US"),
        Symbol("VOD", "LSE"),
    ]


def test_parse_symbols_accepts_a_list_and_a_tuple() -> None:
    expected = [Symbol("AAPL", "US"), Symbol("NESN", "SW")]
    assert parse_symbols(["AAPL.US", "nesn.sw"], max_n=5) == expected
    assert parse_symbols(("AAPL.US", "nesn.sw"), max_n=5) == expected


def test_parse_symbols_drops_repeats_and_keeps_first_order() -> None:
    result = parse_symbols(["B.US", "a.us", "A.US", "B.US", "b.US"], max_n=5)
    assert [s.full for s in result] == ["B.US", "A.US"]


def test_repeats_do_not_count_against_the_limit() -> None:
    assert len(parse_symbols(["A.US"] * 50, max_n=2)) == 1


@pytest.mark.parametrize("raw", ["AAPL.US,", ",AAPL.US", "AAPL.US,,", ["AAPL.US", "", "  "]])
def test_blank_entries_are_ignored(raw: Any) -> None:
    assert parse_symbols(raw, max_n=5) == [Symbol("AAPL", "US")]


@pytest.mark.parametrize("raw", ["", ",", "  ,  ", [], [""], ()])
def test_no_symbol_at_all_is_refused(raw: Any) -> None:
    error = invalid(parse_symbols, raw, max_n=5)
    assert error.message.startswith("symbols: received")
    assert "names no symbol" in error.message
    assert "1 to 5 symbols" in error.message


def test_too_many_symbols_is_refused_with_count_and_limit() -> None:
    error = invalid(parse_symbols, ["A.US", "B.US", "C.US"], max_n=2)
    assert "3 different symbols" in error.message
    assert "more than the 2 allowed" in error.message
    assert error.hint is not None


def test_one_bad_symbol_in_a_list_names_the_bad_one_and_the_field() -> None:
    error = invalid(parse_symbols, ["AAPL.US", "MSFT"], max_n=5)
    assert error.message.startswith("symbols: received 'MSFT'")
    assert "no exchange suffix" in error.message


def test_a_non_text_entry_is_refused() -> None:
    error = invalid(parse_symbols, ["AAPL.US", 7], max_n=5)
    assert "not int" in error.message


@pytest.mark.parametrize("raw", [None, 5, {"AAPL.US": 1}, 1.5])
def test_symbols_must_be_a_list_or_a_string(raw: Any) -> None:
    error = invalid(parse_symbols, raw, max_n=5)
    assert "list or a comma-separated string" in error.message


def test_space_separated_symbols_in_a_list_entry_get_a_comma_hint() -> None:
    error = invalid(parse_symbols, "AAPL.US MSFT.US", max_n=5)
    assert error.hint is not None
    assert "commas" in error.hint


# ------------------------------------------------------------------------------------------- exchanges


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("us", "US"),
        (" lse ", "LSE"),
        ("XETRA", "XETRA"),
        ("INDX", "INDX"),
        ("0", "0"),
        ("ABCDEFGHIJ", "ABCDEFGHIJ"),
    ],
)
def test_parse_exchange_accepts_codes(raw: str, expected: str) -> None:
    assert parse_exchange(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "  ", "U S", "US.", ".US", "U-S", "ABCDEFGHIJK", U_UMLAUT + "S", SHARP_S, "US\n.X"]
)
def test_parse_exchange_refuses_malformed_codes(raw: str) -> None:
    error = invalid(parse_exchange, raw)
    assert error.message.startswith("exchange: received")
    assert "1 to 10 letters or digits" in error.message


@pytest.mark.parametrize("raw", [None, 5, ["US"]])
def test_parse_exchange_refuses_non_text(raw: Any) -> None:
    error = invalid(parse_exchange, raw)
    assert "is text" in error.message


# ------------------------------------------------------------------------------------------------ dates


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-15", date(2026, 1, 15)),
        ("2024-02-29", date(2024, 2, 29)),
        ("0001-01-01", date(1, 1, 1)),
        ("9999-12-31", date(9999, 12, 31)),
    ],
)
def test_iso_date_or_none_reads_strict_dates(value: str, expected: date) -> None:
    assert iso_date_or_none(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        20240115,
        1705276800,
        date(2024, 1, 15),
        "",
        "NA",
        "0000-00-00",
        "20240115",
        "2024-1-5",
        "2026-02-30",
        "2025-02-29",
        " 2024-01-15",
        "2024-01-15\n",
        "2024-01-15T00:00:00",
        arabic_indic("2024-01-15"),
        fullwidth("2024-01-15"),
    ],
)
def test_iso_date_or_none_gives_none_for_anything_else(value: Any) -> None:
    assert iso_date_or_none(value) is None


def test_parse_date_passes_none_through() -> None:
    assert parse_date(None, "start") is None


@pytest.mark.parametrize(
    ("value", "expected"), [("2026-01-15", date(2026, 1, 15)), (" 2026-01-15 ", date(2026, 1, 15))]
)
def test_parse_date_reads_iso_dates(value: str, expected: date) -> None:
    assert parse_date(value, "start") == expected


@pytest.mark.parametrize(
    "value", ["2026-02-30", "2025-02-29", "2026-13-01", "2026-00-10", "0000-01-01", "2026-04-31"]
)
def test_parse_date_refuses_dates_that_do_not_exist(value: str) -> None:
    error = invalid(parse_date, value, "start")
    assert "not a real calendar date" in error.message
    assert f"received {value!r}" in error.message


@pytest.mark.parametrize(
    "value",
    [
        "20240115",
        "2024/01/15",
        "15-01-2024",
        "Jan 15 2024",
        "2024-1-5",
        "2024-01-15T10:00:00Z",
        "1705276800",
        "1705276800000",
        "",
        "   ",
        "tomorrow",
        fullwidth("2024-01-15"),
    ],
)
def test_parse_date_refuses_other_formats(value: str) -> None:
    error = invalid(parse_date, value, "end")
    assert error.message.startswith("end: received")
    assert "not written YYYY-MM-DD" in error.message
    assert "Accepted: a calendar date written YYYY-MM-DD" in error.message
    assert error.hint is not None
    assert "Leave the field out" in error.hint


@pytest.mark.parametrize("value", [20240115, 1705276800, 1705276800.0, True, date(2024, 1, 15)])
def test_parse_date_never_reads_numbers_as_dates_or_unix_seconds(value: Any) -> None:
    error = invalid(parse_date, value, "start")
    assert "Unix seconds" in error.message
    assert "Accepted:" in error.message


@pytest.mark.parametrize("field", ["start", "end", "date"])
def test_parse_date_error_names_the_field(field: str) -> None:
    error = invalid(parse_date, "nonsense", field)
    assert error.message.startswith(f"{field}: received 'nonsense'")
    assert error.details["field"] == field


# ------------------------------------------------------------------------------------------- date-times


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-05", datetime(2026, 3, 5, 0, 0)),
        ("2026-03-05T14:30", datetime(2026, 3, 5, 14, 30)),
        ("2026-03-05T14:30:15", datetime(2026, 3, 5, 14, 30, 15)),
        ("2026-03-05 14:30", datetime(2026, 3, 5, 14, 30)),
        ("2026-03-05T14:30:00Z", datetime(2026, 3, 5, 14, 30)),
        ("2026-03-05T14:30:00+01:00", datetime(2026, 3, 5, 13, 30)),
        ("2026-03-05T14:30:00-0500", datetime(2026, 3, 5, 19, 30)),
        ("2026-03-05T14:30+05:30", datetime(2026, 3, 5, 9, 0)),
        ("2026-03-05T00:30:00+01:00", datetime(2026, 3, 4, 23, 30)),
        ("2026-03-05T23:30:00-02:00", datetime(2026, 3, 6, 1, 30)),
        ("  2026-03-05T14:30:00Z ", datetime(2026, 3, 5, 14, 30)),
    ],
)
def test_parse_datetime_utc_returns_aware_utc(value: str, expected: datetime) -> None:
    result = parse_datetime_utc(value, "start")
    assert result == expected.replace(tzinfo=timezone.utc)
    assert result is not None
    assert result.tzinfo is not None
    assert result.utcoffset() == timezone.utc.utcoffset(None)


def test_parse_datetime_utc_passes_none_through() -> None:
    assert parse_datetime_utc(None, "start") is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-03-05T24:00",
        "2026-03-05T14:60",
        "2026-03-05T14:30:60",
        "2026-02-30T10:00",
        "2026-03-05T14",
        "2026-03-05T14:30:00.123Z",
        "2026-03-05Z",
        "2026-03-05+01:00",
        "20260305T143000Z",
        "2026-03-05T14:30:00+24:00",
        "2026-03-05T14:30:00+23:60",
        "yesterday",
        "",
        "1705276800",
        "0001-01-01T00:00:00+05:00",
        "9999-12-31T23:59:00-05:00",
    ],
)
def test_parse_datetime_utc_refuses_malformed_values(value: str) -> None:
    error = invalid(parse_datetime_utc, value, "end")
    assert error.message.startswith("end: received")
    assert "Accepted: YYYY-MM-DD" in error.message


@pytest.mark.parametrize("value", [1705276800, 1705276800.5, True])
def test_parse_datetime_utc_never_reads_numbers_as_unix_seconds(value: Any) -> None:
    error = invalid(parse_datetime_utc, value, "start")
    assert "Unix seconds" in error.message


# ----------------------------------------------------------------------------------------- date ranges


def test_resolve_range_defaults_to_the_last_default_days() -> None:
    result = resolve_range(None, None, today=TODAY, default_days=30)
    assert result == DateRange(start=date(2026, 10, 4), end=TODAY, clamped=False)


def test_resolve_range_counts_the_default_back_from_an_explicit_end() -> None:
    result = resolve_range(None, date(2026, 10, 15), today=TODAY, default_days=30)
    assert result == DateRange(start=date(2026, 9, 15), end=date(2026, 10, 15), clamped=False)


def test_resolve_range_defaults_the_end_to_today() -> None:
    result = resolve_range(date(2026, 10, 1), None, today=TODAY, default_days=30)
    assert result == DateRange(start=date(2026, 10, 1), end=TODAY, clamped=False)


def test_resolve_range_keeps_an_explicit_window() -> None:
    result = resolve_range(date(2025, 1, 1), date(2025, 12, 31), today=TODAY, default_days=30)
    assert result == DateRange(start=date(2025, 1, 1), end=date(2025, 12, 31), clamped=False)


def test_resolve_range_clamps_a_future_end_and_says_so() -> None:
    result = resolve_range(date(2026, 10, 20), date(2027, 1, 1), today=TODAY, default_days=30)
    assert result == DateRange(start=date(2026, 10, 20), end=TODAY, clamped=True)


def test_resolve_range_counts_the_default_start_from_the_clamped_end() -> None:
    result = resolve_range(None, date(2027, 1, 1), today=TODAY, default_days=10)
    assert result == DateRange(start=date(2026, 10, 24), end=TODAY, clamped=True)


def test_an_end_of_exactly_today_is_not_a_clamp() -> None:
    assert resolve_range(None, TODAY, today=TODAY, default_days=5).clamped is False


def test_one_day_window_is_allowed() -> None:
    day = date(2026, 10, 1)
    assert resolve_range(day, day, today=TODAY, default_days=30) == DateRange(day, day, False)


def test_zero_default_days_gives_a_one_day_window() -> None:
    assert resolve_range(None, None, today=TODAY, default_days=0) == DateRange(TODAY, TODAY, False)


def test_start_after_end_is_refused_naming_both_dates() -> None:
    error = invalid(resolve_range, date(2026, 5, 1), date(2026, 3, 1), today=TODAY, default_days=30)
    assert error.message.startswith("start: received '2026-05-01'")
    assert "after the end date 2026-03-01" in error.message
    assert "Accepted:" in error.message


def test_start_in_the_future_is_refused_even_when_the_end_gets_clamped() -> None:
    error = invalid(resolve_range, date(2026, 12, 1), date(2026, 12, 31), today=TODAY, default_days=30)
    assert error.message.startswith("start: received '2026-12-01'")
    assert "future" in error.message
    assert "on or before 2026-11-03" in error.message


def test_start_in_the_future_without_an_end_is_refused() -> None:
    error = invalid(resolve_range, date(2026, 11, 4), None, today=TODAY, default_days=30)
    assert "future" in error.message


def test_a_span_of_exactly_max_days_is_allowed() -> None:
    result = resolve_range(date(2026, 10, 3), TODAY, today=TODAY, default_days=30, max_days=31)
    assert result.start == date(2026, 10, 3)


def test_a_span_over_max_days_is_refused_with_span_and_limit() -> None:
    error = invalid(resolve_range, date(2024, 11, 3), TODAY, today=TODAY, default_days=30, max_days=600)
    assert error.message.startswith("start/end: received '2024-11-03 to 2026-11-03'")
    assert "spans 730 days" in error.message
    assert "more than the 600 allowed" in error.message
    assert "at most 600 days" in error.message


def test_max_days_one_past_the_limit_is_refused() -> None:
    error = invalid(resolve_range, date(2026, 10, 3), TODAY, today=TODAY, default_days=30, max_days=30)
    assert "spans 31 days" in error.message


def test_no_max_days_means_no_limit() -> None:
    result = resolve_range(date(1970, 1, 1), TODAY, today=TODAY, default_days=30)
    assert result.start == date(1970, 1, 1)


def test_default_start_cannot_run_off_the_calendar() -> None:
    result = resolve_range(None, date(1, 1, 5), today=TODAY, default_days=30)
    assert result.start == date.min
    assert result.end == date(1, 1, 5)


def test_date_range_is_immutable() -> None:
    result = resolve_range(None, None, today=TODAY, default_days=30)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.start = TODAY  # type: ignore[misc]


# -------------------------------------------------------------------------------------------- integers


@pytest.mark.parametrize("value", [1, 7, 500, 5000])
def test_check_int_returns_values_in_range_including_the_bounds(value: int) -> None:
    assert check_int(value, lo=1, hi=5000, field="max_rows") == value


@pytest.mark.parametrize("value", [0, -1, 5001, 10**9])
def test_check_int_refuses_out_of_range_values(value: int) -> None:
    error = invalid(check_int, value, lo=1, hi=5000, field="max_rows")
    assert error.message.startswith(f"max_rows: received {value}")
    assert "a whole number from 1 to 5000" in error.message
    assert error.details["field"] == "max_rows"


@pytest.mark.parametrize("value", [True, False, 5.0, 5.5, "5", None, [5]])
def test_check_int_refuses_things_that_are_not_whole_numbers(value: Any) -> None:
    error = invalid(check_int, value, lo=0, hi=10, field="periods")
    assert "not a whole number" in error.message
    assert error.message.startswith("periods: received")
