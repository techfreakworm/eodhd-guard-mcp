"""Unit tests for eodhd_guard_mcp.markets.

The boundary cases are worked out by hand from each exchange's close, publishing delay and UTC offset, so
that a wrong offset (for example a fixed EST after the clocks change) fails a test rather than hiding.
"""

from __future__ import annotations

import time as systime
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from eodhd_guard_mcp import markets
from eodhd_guard_mcp.markets import (
    DEFAULT_MARKET,
    MARKETS,
    MarketInfo,
    _count_sessions,
    expected_latest_session,
    market_for,
    session_state,
    sessions_between,
)

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ table

H = timedelta(hours=1)

# code: (tz, open, close, publish lag, lag measured against the live API)
EXPECTED_TABLE = {
    "US": ("America/New_York", time(9, 30), time(16, 0), timedelta(minutes=30), True),
    "LSE": ("Europe/London", time(8, 0), time(16, 30), 2 * H, True),
    "SW": ("Europe/Zurich", time(9, 0), time(17, 30), 4 * H, True),
    "NSE": ("Asia/Kolkata", time(9, 15), time(15, 30), 5 * H, True),
    "BSE": ("Asia/Kolkata", time(9, 15), time(15, 30), 5 * H, False),
    "XETRA": ("Europe/Berlin", time(9, 0), time(17, 30), 4 * H, False),
    "F": ("Europe/Berlin", time(9, 0), time(17, 30), 4 * H, False),
    "PA": ("Europe/Paris", time(9, 0), time(17, 30), 4 * H, False),
    "AS": ("Europe/Amsterdam", time(9, 0), time(17, 30), 4 * H, False),
    "BR": ("Europe/Brussels", time(9, 0), time(17, 30), 4 * H, False),
    "LS": ("Europe/Lisbon", time(8, 0), time(16, 30), 4 * H, False),
    "MC": ("Europe/Madrid", time(9, 0), time(17, 30), 4 * H, False),
    "MI": ("Europe/Rome", time(9, 0), time(17, 30), 4 * H, False),
    "TO": ("America/Toronto", time(9, 30), time(16, 0), 2 * H, False),
    "V": ("America/Toronto", time(9, 30), time(16, 0), 2 * H, False),
    "HK": ("Asia/Hong_Kong", time(9, 30), time(16, 0), 4 * H, False),
    "AU": ("Australia/Sydney", time(10, 0), time(16, 0), 4 * H, False),
    "CC": ("UTC", time(0, 0), time(23, 59), 1 * H, False),
    "FOREX": ("UTC", time(0, 0), time(23, 59), 2 * H, False),
}


@pytest.mark.parametrize(("code", "expected"), list(EXPECTED_TABLE.items()))
def test_table_rows_match_the_design(code: str, expected: tuple[str, time, time, timedelta, bool]) -> None:
    tz, opens, closes, lag, measured = expected
    market = MARKETS[code]
    assert market.code == code
    assert (market.tz, market.open, market.close, market.publish_lag, market.lag_measured) == (
        tz,
        opens,
        closes,
        lag,
        measured,
    )


def test_table_has_no_rows_beyond_the_documented_ones() -> None:
    assert set(MARKETS) == set(EXPECTED_TABLE)


def test_only_the_four_markets_checked_in_october_2026_have_a_measured_lag() -> None:
    assert {code for code, market in MARKETS.items() if market.lag_measured} == {"US", "LSE", "SW", "NSE"}


def test_every_time_zone_resolves_and_every_row_is_sane() -> None:
    for code, market in MARKETS.items():
        assert ZoneInfo(market.tz) is not None, code
        assert market.open < market.close, code
        assert timedelta(0) < market.publish_lag <= timedelta(hours=6), code
        assert market.name, code


def test_weekends_are_saturday_and_sunday_except_for_crypto() -> None:
    assert {code for code, market in MARKETS.items() if market.weekend != (5, 6)} == {"CC"}
    assert MARKETS["CC"].weekend == ()


def test_only_london_carries_a_currency_hint() -> None:
    assert MARKETS["LSE"].currency_hint == "GBX"
    assert {code for code, market in MARKETS.items() if market.currency_hint} == {"LSE"}


def test_the_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        MARKETS["XX"] = DEFAULT_MARKET  # type: ignore[index]


def test_default_market_is_a_utc_weekday_market_with_a_generous_lag() -> None:
    assert DEFAULT_MARKET.tz == "UTC"
    assert (DEFAULT_MARKET.open, DEFAULT_MARKET.close) == (time(0, 0), time(23, 59))
    assert DEFAULT_MARKET.publish_lag == timedelta(hours=6)
    assert DEFAULT_MARKET.weekend == (5, 6)
    assert DEFAULT_MARKET.lag_measured is False
    assert DEFAULT_MARKET.code not in MARKETS


def _market(**overrides: object) -> MarketInfo:
    fields: dict[str, object] = {
        "code": "XX",
        "name": "Test market",
        "tz": "UTC",
        "open": time(9, 0),
        "close": time(17, 0),
        "publish_lag": timedelta(hours=1),
        "lag_measured": False,
    }
    fields.update(overrides)
    return MarketInfo(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"open": time(17, 0), "close": time(9, 0)}, "open before it closes"),
        ({"open": time(9, 0), "close": time(9, 0)}, "open before it closes"),
        ({"publish_lag": timedelta(seconds=-1)}, "cannot be negative"),
        ({"weekend": (5, 7)}, "weekday numbers 0-6"),
        ({"weekend": (-1,)}, "weekday numbers 0-6"),
        ({"weekend": (0, 1, 2, 3, 4, 5, 6)}, "leave a trading day"),
    ],
)
def test_market_info_refuses_a_nonsensical_definition(overrides: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        _market(**overrides)


def test_market_info_accepts_a_six_day_weekend_and_no_weekend() -> None:
    assert _market(weekend=(0, 1, 2, 3, 4, 5)).weekend == (0, 1, 2, 3, 4, 5)
    assert _market(weekend=()).weekend == ()


# -------------------------------------------------------------------------------------------- market_for


def test_market_for_finds_known_exchanges_ignoring_case_and_spaces() -> None:
    assert market_for("US") is MARKETS["US"]
    assert market_for("lse") is MARKETS["LSE"]
    assert market_for("  Sw ") is MARKETS["SW"]


@pytest.mark.parametrize("exchange", ["ZZ", "", "TSE", "INDX", "MONEY", "AE", "TA", "NASDAQ"])
def test_market_for_falls_back_to_the_default_for_unknown_exchanges(exchange: str) -> None:
    assert market_for(exchange) is DEFAULT_MARKET


def test_toronto_is_a_known_market_and_tel_aviv_is_not() -> None:
    assert market_for("TO") is MARKETS["TO"]
    assert market_for("TA") is DEFAULT_MARKET


# ------------------------------------------------------------------------ expected_latest_session: hours


def at(exchange: str, now: datetime, expected: date, label: str) -> object:
    return pytest.param(exchange, now, expected, id=f"{exchange}-{label}")


# Tuesday 2026-11-03 is after the US clocks went back, so New York is on EST (UTC-5).
US_TUESDAY_CASES = [
    at("US", datetime(2026, 11, 3, 16, 29, tzinfo=NEW_YORK), date(2026, 11, 2), "tue-16:29-ny"),
    at("US", datetime(2026, 11, 3, 16, 30, tzinfo=NEW_YORK), date(2026, 11, 3), "tue-16:30-ny"),
    at("US", datetime(2026, 11, 3, 16, 31, tzinfo=NEW_YORK), date(2026, 11, 3), "tue-16:31-ny"),
    at("US", utc(2026, 11, 3, 21, 29), date(2026, 11, 2), "tue-21:29-utc"),
    at("US", utc(2026, 11, 3, 21, 31), date(2026, 11, 3), "tue-21:31-utc"),
    at("US", datetime(2026, 11, 3, 0, 30, tzinfo=NEW_YORK), date(2026, 11, 2), "tue-00:30-ny"),
    at("US", datetime(2026, 11, 3, 23, 59, tzinfo=NEW_YORK), date(2026, 11, 3), "tue-23:59-ny"),
]

WEEKEND_CASES = [
    at("US", datetime(2026, 11, 2, 9, 0, tzinfo=NEW_YORK), date(2026, 10, 30), "monday-morning"),
    at("US", datetime(2026, 11, 2, 16, 29, tzinfo=NEW_YORK), date(2026, 10, 30), "monday-16:29"),
    at("US", datetime(2026, 11, 2, 16, 31, tzinfo=NEW_YORK), date(2026, 11, 2), "monday-16:31"),
    at("US", datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK), date(2026, 10, 30), "saturday"),
    at("US", datetime(2026, 11, 1, 23, 0, tzinfo=NEW_YORK), date(2026, 10, 30), "sunday-night"),
    at("LSE", utc(2026, 11, 2, 8, 0), date(2026, 10, 30), "monday-morning"),
    at("SW", utc(2026, 11, 1, 12, 0), date(2026, 10, 30), "sunday"),
]

# NSE is UTC+5:30 all year. Close 15:30 plus 5 h is 20:30 IST, which is 15:00 UTC.
NSE_CASES = [
    at(
        "NSE",
        datetime(2026, 11, 3, 20, 29, tzinfo=ZoneInfo("Asia/Kolkata")),
        date(2026, 11, 2),
        "tue-20:29-ist",
    ),
    at(
        "NSE",
        datetime(2026, 11, 3, 20, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
        date(2026, 11, 3),
        "tue-20:30-ist",
    ),
    at(
        "NSE",
        datetime(2026, 11, 3, 20, 31, tzinfo=ZoneInfo("Asia/Kolkata")),
        date(2026, 11, 3),
        "tue-20:31-ist",
    ),
    at("NSE", utc(2026, 11, 3, 14, 59), date(2026, 11, 2), "tue-14:59-utc"),
    at("NSE", utc(2026, 11, 3, 15, 0), date(2026, 11, 3), "tue-15:00-utc"),
    at("NSE", utc(2026, 11, 2, 12, 0), date(2026, 10, 30), "monday-before-lag"),
]

# Other exchanges, each on its own offset in November: LSE is GMT, SW is CET, HK is UTC+8.
OTHER_HOURS_CASES = [
    at("LSE", utc(2026, 11, 3, 18, 29), date(2026, 11, 2), "tue-18:29-utc"),
    at("LSE", utc(2026, 11, 3, 18, 31), date(2026, 11, 3), "tue-18:31-utc"),
    at("SW", utc(2026, 11, 3, 15, 0), date(2026, 11, 2), "tue-15:00-utc"),
    at("SW", utc(2026, 11, 3, 20, 29), date(2026, 11, 2), "tue-20:29-utc"),
    at("SW", utc(2026, 11, 3, 20, 31), date(2026, 11, 3), "tue-20:31-utc"),
    at("HK", utc(2026, 11, 3, 11, 59), date(2026, 11, 2), "tue-11:59-utc"),
    at("HK", utc(2026, 11, 3, 12, 1), date(2026, 11, 3), "tue-12:01-utc"),
    at("TO", utc(2026, 11, 3, 22, 59), date(2026, 11, 2), "tue-22:59-utc"),
    at("TO", utc(2026, 11, 3, 23, 1), date(2026, 11, 3), "tue-23:01-utc"),
]

# Sydney is ahead of UTC: its calendar day starts while UTC is still on the day before. On
# 2026-11-03 it is on daylight time (UTC+11), so close 16:00 plus 4 h is 20:00 AEDT, which is 09:00 UTC.
DATE_LINE_CASES = [
    at("AU", utc(2026, 11, 3, 20, 0), date(2026, 11, 3), "wed-morning-in-sydney"),
    at("AU", utc(2026, 11, 4, 8, 59), date(2026, 11, 3), "wed-19:59-aedt"),
    at("AU", utc(2026, 11, 4, 9, 1), date(2026, 11, 4), "wed-20:01-aedt"),
    at("AU", utc(2026, 11, 6, 20, 0), date(2026, 11, 6), "saturday-morning-in-sydney"),
    at("AU", utc(2026, 11, 7, 20, 0), date(2026, 11, 6), "sunday-morning-in-sydney"),
]

# Crypto has no weekend and a close of 23:59 UTC, so a day's bar is expected at 00:59 UTC the next day.
CRYPTO_CASES = [
    at("CC", utc(2026, 11, 3, 12, 0), date(2026, 11, 2), "tuesday-noon"),
    at("CC", utc(2026, 11, 7, 12, 0), date(2026, 11, 6), "saturday-noon"),
    at("CC", utc(2026, 11, 8, 12, 0), date(2026, 11, 7), "sunday-noon"),
    at("CC", utc(2026, 11, 9, 0, 30), date(2026, 11, 7), "monday-00:30"),
    at("CC", utc(2026, 11, 9, 1, 0), date(2026, 11, 8), "monday-01:00"),
    at("CC", utc(2026, 11, 8, 0, 58), date(2026, 11, 6), "sunday-00:58"),
    at("CC", utc(2026, 11, 8, 0, 59), date(2026, 11, 7), "sunday-00:59"),
]

# Forex closes 23:59 UTC with a 2 h delay, and it does have a Saturday and Sunday here.
FOREX_CASES = [
    at("FOREX", utc(2026, 11, 6, 23, 0), date(2026, 11, 5), "friday-23:00"),
    at("FOREX", utc(2026, 11, 7, 1, 58), date(2026, 11, 5), "saturday-01:58"),
    at("FOREX", utc(2026, 11, 7, 2, 0), date(2026, 11, 6), "saturday-02:00"),
    at("FOREX", utc(2026, 11, 8, 12, 0), date(2026, 11, 6), "sunday"),
]

# An exchange the table does not know: UTC, close 23:59, delay 6 h, so a bar is due at 05:59 UTC the next day.
UNKNOWN_CASES = [
    at("ZZ", utc(2026, 11, 3, 5, 58), date(2026, 10, 30), "tue-05:58-skips-the-weekend"),
    at("ZZ", utc(2026, 11, 3, 5, 59), date(2026, 11, 2), "tue-05:59"),
    at("ZZ", utc(2026, 11, 3, 6, 0), date(2026, 11, 2), "tue-06:00"),
    at("ZZ", utc(2026, 11, 4, 5, 58), date(2026, 11, 2), "wed-05:58"),
    at("ZZ", utc(2026, 11, 4, 6, 0), date(2026, 11, 3), "wed-06:00"),
    at("TSE", utc(2026, 11, 4, 6, 0), date(2026, 11, 3), "tse-uses-default"),
    at("ZZ", utc(2026, 11, 8, 12, 0), date(2026, 11, 6), "sunday"),
]


@pytest.mark.parametrize(
    ("exchange", "now", "expected"),
    US_TUESDAY_CASES
    + WEEKEND_CASES
    + NSE_CASES
    + OTHER_HOURS_CASES
    + DATE_LINE_CASES
    + CRYPTO_CASES
    + FOREX_CASES
    + UNKNOWN_CASES,
)
def test_expected_latest_session(exchange: str, now: datetime, expected: date) -> None:
    assert expected_latest_session(exchange, now) == expected


def test_a_bar_ready_exactly_now_counts() -> None:
    assert expected_latest_session("US", utc(2026, 11, 3, 21, 30)) == date(2026, 11, 3)
    assert expected_latest_session("US", utc(2026, 11, 3, 21, 30) - timedelta(seconds=1)) == date(2026, 11, 2)


def test_exchange_codes_are_matched_case_insensitively() -> None:
    now = utc(2026, 11, 3, 21, 31)
    assert expected_latest_session(" us ", now) == expected_latest_session("US", now)


# ------------------------------------------------------------------------ expected_latest_session: DST

# US clocks go forward on Sunday 2026-03-08 (EST to EDT) and back on Sunday 2026-11-01 (EDT to EST).
# A Friday close before the change and a Monday close after it are an hour apart in UTC.
US_DST_CASES = [
    # spring: Friday 03-06 is EST (ready 21:30 UTC), Monday 03-09 is EDT (ready 20:30 UTC)
    at("US", utc(2026, 3, 6, 21, 29), date(2026, 3, 5), "spring-fri-21:29"),
    at("US", utc(2026, 3, 6, 21, 30), date(2026, 3, 6), "spring-fri-21:30"),
    at("US", utc(2026, 3, 9, 20, 29), date(2026, 3, 6), "spring-mon-20:29"),
    at("US", utc(2026, 3, 9, 20, 30), date(2026, 3, 9), "spring-mon-20:30"),
    at("US", utc(2026, 3, 9, 20, 31), date(2026, 3, 9), "spring-mon-20:31"),
    at("US", datetime(2026, 3, 9, 16, 29, tzinfo=NEW_YORK), date(2026, 3, 6), "spring-mon-16:29-ny"),
    at("US", datetime(2026, 3, 9, 16, 31, tzinfo=NEW_YORK), date(2026, 3, 9), "spring-mon-16:31-ny"),
    at("US", utc(2026, 3, 8, 6, 30), date(2026, 3, 6), "spring-sunday-before-gap"),
    at("US", utc(2026, 3, 8, 7, 30), date(2026, 3, 6), "spring-sunday-after-gap"),
    # autumn: Friday 10-30 is EDT (ready 20:30 UTC), Monday 11-02 is EST (ready 21:30 UTC)
    at("US", utc(2026, 10, 30, 20, 29), date(2026, 10, 29), "autumn-fri-20:29"),
    at("US", utc(2026, 10, 30, 20, 30), date(2026, 10, 30), "autumn-fri-20:30"),
    at("US", utc(2026, 11, 2, 21, 29), date(2026, 10, 30), "autumn-mon-21:29"),
    at("US", utc(2026, 11, 2, 21, 30), date(2026, 11, 2), "autumn-mon-21:30"),
    at("US", utc(2026, 11, 2, 21, 31), date(2026, 11, 2), "autumn-mon-21:31"),
    at("US", datetime(2026, 11, 2, 16, 29, tzinfo=NEW_YORK), date(2026, 10, 30), "autumn-mon-16:29-ny"),
    at("US", datetime(2026, 11, 2, 16, 31, tzinfo=NEW_YORK), date(2026, 11, 2), "autumn-mon-16:31-ny"),
    # the repeated hour on Sunday 11-01: 01:30 happens twice, 05:30 UTC (EDT) and 06:30 UTC (EST)
    at("US", utc(2026, 11, 1, 5, 30), date(2026, 10, 30), "autumn-sunday-first-01:30"),
    at("US", utc(2026, 11, 1, 6, 30), date(2026, 10, 30), "autumn-sunday-second-01:30"),
    at("US", datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK, fold=0), date(2026, 10, 30), "autumn-fold-0"),
    at("US", datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK, fold=1), date(2026, 10, 30), "autumn-fold-1"),
]

# UK clocks go forward at 01:00 UTC on Sunday 2026-03-29 and back at 01:00 UTC on Sunday 2026-10-25.
# London close 16:30 plus 2 h is 18:30 local.
UK_DST_CASES = [
    # spring: Friday 03-27 is GMT (ready 18:30 UTC), Monday 03-30 is BST (ready 17:30 UTC)
    at("LSE", utc(2026, 3, 27, 18, 29), date(2026, 3, 26), "spring-fri-18:29"),
    at("LSE", utc(2026, 3, 27, 18, 31), date(2026, 3, 27), "spring-fri-18:31"),
    at("LSE", utc(2026, 3, 30, 17, 29), date(2026, 3, 27), "spring-mon-17:29"),
    at("LSE", utc(2026, 3, 30, 17, 30), date(2026, 3, 30), "spring-mon-17:30"),
    at("LSE", utc(2026, 3, 30, 17, 31), date(2026, 3, 30), "spring-mon-17:31"),
    at("LSE", utc(2026, 3, 29, 0, 30), date(2026, 3, 27), "spring-sunday-gmt"),
    at("LSE", utc(2026, 3, 29, 1, 30), date(2026, 3, 27), "spring-sunday-bst"),
    # autumn: Friday 10-23 is BST (ready 17:30 UTC), Monday 10-26 is GMT (ready 18:30 UTC)
    at("LSE", utc(2026, 10, 23, 17, 29), date(2026, 10, 22), "autumn-fri-17:29"),
    at("LSE", utc(2026, 10, 23, 17, 30), date(2026, 10, 23), "autumn-fri-17:30"),
    at("LSE", utc(2026, 10, 26, 18, 29), date(2026, 10, 23), "autumn-mon-18:29"),
    at("LSE", utc(2026, 10, 26, 18, 30), date(2026, 10, 26), "autumn-mon-18:30"),
    at("LSE", utc(2026, 10, 26, 18, 31), date(2026, 10, 26), "autumn-mon-18:31"),
    at("LSE", utc(2026, 10, 25, 0, 30), date(2026, 10, 23), "autumn-sunday-bst"),
    at("LSE", utc(2026, 10, 25, 1, 30), date(2026, 10, 23), "autumn-sunday-gmt"),
    # Switzerland changes on the same Sundays: CEST (UTC+2) before 10-25, CET (UTC+1) after.
    at("SW", utc(2026, 10, 23, 19, 29), date(2026, 10, 22), "ch-autumn-fri-19:29"),
    at("SW", utc(2026, 10, 23, 19, 31), date(2026, 10, 23), "ch-autumn-fri-19:31"),
    at("SW", utc(2026, 10, 26, 20, 29), date(2026, 10, 23), "ch-autumn-mon-20:29"),
    at("SW", utc(2026, 10, 26, 20, 31), date(2026, 10, 26), "ch-autumn-mon-20:31"),
    # Sydney changes in the other season: AEST to AEDT on Sunday 2026-10-04 (UTC+10 to UTC+11).
    at("AU", utc(2026, 10, 2, 9, 59), date(2026, 10, 1), "au-fri-before-change-09:59"),
    at("AU", utc(2026, 10, 2, 10, 1), date(2026, 10, 2), "au-fri-before-change-10:01"),
    at("AU", utc(2026, 10, 5, 8, 59), date(2026, 10, 2), "au-mon-after-change-08:59"),
    at("AU", utc(2026, 10, 5, 9, 1), date(2026, 10, 5), "au-mon-after-change-09:01"),
]


@pytest.mark.parametrize(("exchange", "now", "expected"), US_DST_CASES + UK_DST_CASES)
def test_expected_latest_session_across_daylight_saving_changes(
    exchange: str, now: datetime, expected: date
) -> None:
    assert expected_latest_session(exchange, now) == expected


# ------------------------------------------------------------------ expected_latest_session: input forms


def test_a_naive_now_is_taken_as_utc() -> None:
    assert expected_latest_session("US", datetime(2026, 11, 3, 21, 29)) == date(2026, 11, 2)
    assert expected_latest_session("US", datetime(2026, 11, 3, 21, 31)) == date(2026, 11, 3)


def test_now_may_carry_any_time_zone() -> None:
    tokyo_evening = datetime(2026, 11, 4, 6, 31, tzinfo=ZoneInfo("Asia/Tokyo"))  # 21:31 UTC on 11-03
    assert expected_latest_session("US", tokyo_evening) == date(2026, 11, 3)
    assert expected_latest_session("US", tokyo_evening - timedelta(minutes=2)) == date(2026, 11, 2)


@pytest.mark.skipif(not hasattr(systime, "tzset"), reason="needs time.tzset, which Windows lacks")
def test_a_naive_now_is_not_read_in_the_machines_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    systime.tzset()
    try:
        assert expected_latest_session("US", datetime(2026, 11, 3, 21, 31)) == date(2026, 11, 3)
        assert expected_latest_session("US", datetime(2026, 11, 3, 21, 29)) == date(2026, 11, 2)
        assert session_state("US", datetime(2026, 11, 3, 14, 30)) == "open"
    finally:
        monkeypatch.undo()
        systime.tzset()


# ------------------------------------------------------------ markets the table does not (yet) contain


def test_the_date_is_the_exchanges_own_even_when_it_is_ahead_of_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A made-up market in UTC+14 that closes at 01:00: its Tuesday is over while UTC is still on Monday."""
    early = MarketInfo(
        code="KI",
        name="Made-up early closer",
        tz="Pacific/Kiritimati",
        open=time(0, 15),
        close=time(1, 0),
        publish_lag=timedelta(0),
        lag_measured=False,
    )
    monkeypatch.setattr(markets, "MARKETS", {"KI": early})
    assert expected_latest_session("KI", utc(2026, 11, 2, 11, 30)) == date(2026, 11, 3)
    assert expected_latest_session("KI", utc(2026, 11, 2, 10, 30)) == date(2026, 11, 2)


@pytest.fixture
def sunday_to_thursday(monkeypatch: pytest.MonkeyPatch) -> MarketInfo:
    """A made-up market that trades Sunday to Thursday, as several Gulf markets do (UTC+4, no DST)."""
    gulf = MarketInfo(
        code="GU",
        name="Made-up Sunday-Thursday market",
        tz="Asia/Dubai",
        open=time(10, 0),
        close=time(14, 0),
        publish_lag=timedelta(hours=2),
        lag_measured=False,
        weekend=(4, 5),
    )
    monkeypatch.setattr(markets, "MARKETS", {"GU": gulf})
    return gulf


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 11, 8, 11, 59), date(2026, 11, 5)),  # Sunday, bar not due until 12:00 UTC: Thursday it is
        (utc(2026, 11, 8, 12, 0), date(2026, 11, 8)),  # Sunday is a trading day
        (utc(2026, 11, 6, 12, 0), date(2026, 11, 5)),  # Friday is weekend
        (utc(2026, 11, 7, 12, 0), date(2026, 11, 5)),  # Saturday is weekend
        (utc(2026, 11, 9, 11, 59), date(2026, 11, 8)),  # Monday morning: Sunday's bar
    ],
)
def test_a_sunday_to_thursday_market_uses_its_own_weekend(
    sunday_to_thursday: MarketInfo, now: datetime, expected: date
) -> None:
    assert expected_latest_session("GU", now) == expected


def test_sessions_and_state_follow_a_sunday_to_thursday_week(sunday_to_thursday: MarketInfo) -> None:
    thursday, friday, sunday = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 8)
    assert sessions_between(thursday, sunday, "GU") == 1
    assert sessions_between(thursday, friday, "GU") == 0
    assert sessions_between(sunday, date(2026, 11, 12), "GU") == 4
    assert session_state("GU", utc(2026, 11, 8, 6, 30)) == "open"
    assert session_state("GU", utc(2026, 11, 6, 6, 30)) == "closed_day"


# ---------------------------------------------------------------- expected_latest_session: independent check


def oracle_latest_session(
    now: datetime, offset_hours: float, close: time, lag: timedelta, weekend: tuple[int, ...]
) -> date:
    """The same rule worked out on a plain fixed-offset clock, with no time-zone database involved."""
    local = now.replace(tzinfo=None) + timedelta(hours=offset_hours)
    day = local.date()
    for _ in range(14):
        if day.weekday() not in weekend and datetime.combine(day, close) + lag <= local:
            return day
        day -= timedelta(days=1)
    raise AssertionError("no session found")


def sample_instants(
    start: datetime, days: int, offset_hours: float, close: time, lag: timedelta
) -> Iterator[datetime]:
    """A coarse grid, plus one minute either side of every instant at which a bar becomes due."""
    step = timedelta(minutes=17)
    moment = start
    while moment < start + timedelta(days=days):
        yield moment
        moment += step
    for back in range(-1, days + 1):
        day = start.date() + timedelta(days=back)
        due = datetime.combine(day, close) + lag - timedelta(hours=offset_hours)
        for minutes in (-1, 0, 1):
            yield due.replace(tzinfo=UTC) + timedelta(minutes=minutes)


# Windows with no clock change in the zone: January and July 2026 begin on a Monday (5th and 6th).
JANUARY = datetime(2026, 1, 5, tzinfo=UTC)
JULY = datetime(2026, 7, 6, tzinfo=UTC)

SWEEP_CASES = [
    ("US", JANUARY, -5),
    ("US", JULY, -4),
    ("TO", JANUARY, -5),
    ("TO", JULY, -4),
    ("LSE", JANUARY, 0),
    ("LSE", JULY, 1),
    ("LS", JANUARY, 0),
    ("LS", JULY, 1),
    ("SW", JANUARY, 1),
    ("SW", JULY, 2),
    ("XETRA", JANUARY, 1),
    ("PA", JULY, 2),
    ("MI", JANUARY, 1),
    ("NSE", JANUARY, 5.5),
    ("BSE", JULY, 5.5),
    ("HK", JANUARY, 8),
    ("AU", JANUARY, 11),
    ("AU", JULY, 10),
    ("CC", JANUARY, 0),
    ("FOREX", JULY, 0),
    ("ZZ", JANUARY, 0),
]


@pytest.mark.parametrize(
    ("exchange", "start", "offset"), SWEEP_CASES, ids=[f"{c[0]}-{c[1]:%b}" for c in SWEEP_CASES]
)
def test_expected_latest_session_matches_a_fixed_offset_calculation(
    exchange: str, start: datetime, offset: float
) -> None:
    market = market_for(exchange)
    mismatches = []
    for now in sample_instants(start, 14, offset, market.close, market.publish_lag):
        want = oracle_latest_session(now, offset, market.close, market.publish_lag, market.weekend)
        got = expected_latest_session(exchange, now)
        if got != want:
            mismatches.append((now.isoformat(), got, want))
    assert mismatches == []


SWEEP_WINDOWS = [
    pytest.param(datetime(2026, 3, 1, tzinfo=UTC), id="march-us-spring"),
    pytest.param(datetime(2026, 3, 22, tzinfo=UTC), id="late-march-europe-spring"),
    pytest.param(datetime(2026, 10, 19, tzinfo=UTC), id="october-europe-and-us-autumn"),
]


@pytest.mark.parametrize("start", SWEEP_WINDOWS)
@pytest.mark.parametrize("exchange", [*EXPECTED_TABLE, "ZZ"])
def test_across_clock_changes_the_answer_never_goes_back_or_lands_on_a_weekend(
    exchange: str, start: datetime
) -> None:
    market = market_for(exchange)
    zone = ZoneInfo(market.tz)
    previous: date | None = None
    moment = start
    while moment < start + timedelta(days=21):
        latest = expected_latest_session(exchange, moment)
        assert latest.weekday() not in market.weekend
        assert latest <= moment.astimezone(zone).date()
        assert latest >= moment.astimezone(zone).date() - timedelta(days=4)
        if previous is not None:
            assert latest >= previous
        previous = latest
        moment += timedelta(minutes=41)


# ---------------------------------------------------------------------------------- sessions_between


def test_sessions_between_counts_after_the_first_date_up_to_the_second() -> None:
    friday, saturday, sunday, monday = (
        date(2026, 10, 30),
        date(2026, 10, 31),
        date(2026, 11, 1),
        date(2026, 11, 2),
    )
    assert sessions_between(friday, monday, "US") == 1
    assert sessions_between(friday, saturday, "US") == 0
    assert sessions_between(friday, sunday, "US") == 0
    assert sessions_between(saturday, monday, "US") == 1
    assert sessions_between(sunday, monday, "US") == 1
    assert sessions_between(monday, date(2026, 11, 6), "US") == 4
    assert sessions_between(monday, date(2026, 11, 9), "US") == 5
    assert sessions_between(friday, date(2026, 11, 13), "US") == 10


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        (date(2026, 11, 2), date(2026, 11, 2)),
        (date(2026, 11, 3), date(2026, 11, 2)),
        (date(2026, 11, 2), date(2026, 10, 1)),
        (date(2027, 1, 1), date(2026, 1, 1)),
    ],
)
def test_sessions_between_is_zero_when_the_dates_are_not_in_order(earlier: date, later: date) -> None:
    assert sessions_between(earlier, later, "US") == 0


def test_sessions_between_counts_every_day_for_crypto() -> None:
    assert sessions_between(date(2026, 10, 30), date(2026, 11, 2), "CC") == 3
    assert sessions_between(date(2026, 10, 30), date(2026, 11, 6), "CC") == 7


def test_sessions_between_uses_the_default_weekend_for_unknown_exchanges() -> None:
    assert sessions_between(date(2026, 10, 30), date(2026, 11, 2), "ZZ") == 1


def brute_force_count(earlier: date, later: date, weekend: tuple[int, ...]) -> int:
    count, day = 0, earlier
    while day < later:
        day += timedelta(days=1)
        if day.weekday() not in weekend:
            count += 1
    return count


@pytest.mark.parametrize("weekend", [(), (5, 6), (4, 5), (6,), (0,), (1, 3), (0, 1, 2, 3, 4, 5)])
def test_count_sessions_agrees_with_counting_day_by_day(weekend: tuple[int, ...]) -> None:
    first = date(2026, 10, 25)
    days = [first + timedelta(days=n) for n in range(-3, 45)]
    for earlier in days:
        for later in days:
            assert _count_sessions(earlier, later, weekend) == brute_force_count(earlier, later, weekend)


def test_a_long_span_is_counted_correctly() -> None:
    earlier, later = date(1970, 1, 1), date(2026, 11, 3)
    assert sessions_between(earlier, later, "US") == brute_force_count(earlier, later, (5, 6))


def test_roche_like_gap_counts_every_weekday() -> None:
    # 2026-03-25 is a Wednesday; the weekdays after it up to Monday 2026-11-02 number 158.
    assert sessions_between(date(2026, 3, 25), date(2026, 11, 2), "SW") == 158


# ---------------------------------------------------------------------------------------- session_state


@pytest.mark.parametrize(
    ("local_time", "expected"),
    [
        (time(0, 0), "pre_open"),
        (time(9, 29), "pre_open"),
        (time(9, 29, 59), "pre_open"),
        (time(9, 30), "open"),
        (time(12, 0), "open"),
        (time(15, 59, 59), "open"),
        (time(16, 0), "post_close"),
        (time(16, 1), "post_close"),
        (time(23, 59, 59), "post_close"),
    ],
)
def test_session_state_through_a_us_tuesday(local_time: time, expected: str) -> None:
    now = datetime.combine(date(2026, 11, 3), local_time, tzinfo=NEW_YORK)
    assert session_state("US", now) == expected


@pytest.mark.parametrize("day", [date(2026, 10, 31), date(2026, 11, 1)])
@pytest.mark.parametrize("hour", [0, 9, 12, 16, 23])
def test_session_state_on_a_weekend_is_closed_day(day: date, hour: int) -> None:
    assert session_state("US", datetime.combine(day, time(hour, 0), tzinfo=NEW_YORK)) == "closed_day"


def test_session_state_follows_the_clock_change() -> None:
    # Monday 2026-03-09 is on EDT: 09:30 New York is 13:30 UTC, not the 14:30 UTC it was on EST.
    assert session_state("US", utc(2026, 3, 9, 13, 29)) == "pre_open"
    assert session_state("US", utc(2026, 3, 9, 13, 30)) == "open"
    assert session_state("US", utc(2026, 3, 9, 19, 59)) == "open"
    assert session_state("US", utc(2026, 3, 9, 20, 0)) == "post_close"
    # Monday 2026-11-02 is on EST.
    assert session_state("US", utc(2026, 11, 2, 14, 29)) == "pre_open"
    assert session_state("US", utc(2026, 11, 2, 14, 30)) == "open"


def test_session_state_for_london_in_summer_time() -> None:
    assert session_state("LSE", utc(2026, 3, 30, 6, 59)) == "pre_open"
    assert session_state("LSE", utc(2026, 3, 30, 7, 0)) == "open"
    assert session_state("LSE", utc(2026, 3, 30, 15, 30)) == "post_close"


def test_session_state_uses_the_exchanges_own_date() -> None:
    # 22:59 UTC on Tuesday is 09:59 on Wednesday in Sydney, before the 10:00 open.
    assert session_state("AU", utc(2026, 11, 3, 22, 59)) == "pre_open"
    assert session_state("AU", utc(2026, 11, 3, 23, 0)) == "open"
    # 21:00 UTC on Friday is already Saturday morning in Sydney.
    assert session_state("AU", utc(2026, 11, 6, 21, 0)) == "closed_day"


def test_session_state_in_india_has_no_daylight_saving() -> None:
    assert session_state("NSE", utc(2026, 11, 3, 3, 44)) == "pre_open"
    assert session_state("NSE", utc(2026, 11, 3, 3, 45)) == "open"
    assert session_state("NSE", utc(2026, 11, 3, 10, 0)) == "post_close"


def test_crypto_is_never_closed_for_the_day() -> None:
    assert session_state("CC", utc(2026, 11, 7, 12, 0)) == "open"
    assert session_state("CC", utc(2026, 11, 8, 0, 0)) == "open"
    assert session_state("CC", utc(2026, 11, 8, 23, 58)) == "open"
    assert session_state("CC", utc(2026, 11, 8, 23, 59)) == "post_close"


def test_session_state_for_an_unknown_exchange_uses_the_default_market() -> None:
    assert session_state("ZZ", utc(2026, 11, 7, 12, 0)) == "closed_day"
    assert session_state("ZZ", utc(2026, 11, 3, 12, 0)) == "open"


def test_session_state_reads_a_naive_now_as_utc() -> None:
    assert session_state("US", datetime(2026, 11, 3, 14, 30)) == "open"
    assert session_state("US", datetime(2026, 11, 3, 14, 29)) == "pre_open"
