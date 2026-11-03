"""Unit tests for eodhd_guard_mcp.freshness."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone

import pytest

from eodhd_guard_mcp.envelope import ToolWarning, WarningCode
from eodhd_guard_mcp.freshness import Freshness, assess, stale_warning
from eodhd_guard_mcp.validation import Symbol, parse_symbol

# Tuesday 2026-11-03, 10:00 in New York and 16:00 in Zurich: neither market has published today's bar yet.
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
MONDAY = date(2026, 11, 2)


# --------------------------------------------------------------------------------------------- assess


def test_a_bar_from_march_in_november_is_stale_by_a_large_gap() -> None:
    """The Roche-like case: HTTP 200 and plausible-looking rows, but the data stopped in March."""
    result = assess("SW", date(2026, 3, 25), NOW, threshold=3)
    assert result == Freshness(
        last_bar=date(2026, 3, 25), expected_latest=MONDAY, sessions_behind=158, stale=True
    )


def test_a_normal_one_session_lag_is_not_stale() -> None:
    result = assess("US", date(2026, 10, 30), NOW, threshold=3)
    assert result == Freshness(
        last_bar=date(2026, 10, 30), expected_latest=MONDAY, sessions_behind=1, stale=False
    )


def test_a_bar_for_the_latest_expected_session_is_fresh() -> None:
    result = assess("US", MONDAY, NOW, threshold=3)
    assert (result.sessions_behind, result.stale) == (0, False)


def test_a_bar_newer_than_expected_counts_as_zero_behind() -> None:
    """EODHD sometimes publishes sooner than the usual delay; that must never read as stale or negative."""
    result = assess("US", date(2026, 11, 3), NOW, threshold=3)
    assert result.expected_latest == MONDAY
    assert (result.sessions_behind, result.stale) == (0, False)


def test_no_bar_at_all_is_stale() -> None:
    result = assess("US", None, NOW, threshold=3)
    assert result == Freshness(last_bar=None, expected_latest=MONDAY, sessions_behind=0, stale=True)


@pytest.mark.parametrize(
    ("last_bar", "behind", "stale"),
    [
        (date(2026, 11, 2), 0, False),
        (date(2026, 10, 30), 1, False),
        (date(2026, 10, 29), 2, False),
        (date(2026, 10, 28), 3, True),
        (date(2026, 10, 27), 4, True),
        (date(2026, 10, 26), 5, True),
    ],
)
def test_stale_starts_at_the_threshold(last_bar: date, behind: int, stale: bool) -> None:
    result = assess("US", last_bar, NOW, threshold=3)
    assert (result.sessions_behind, result.stale) == (behind, stale)


@pytest.mark.parametrize(
    ("threshold", "last_bar", "stale"),
    [
        (1, date(2026, 11, 2), False),
        (1, date(2026, 10, 30), True),
        (2, date(2026, 10, 30), False),
        (2, date(2026, 10, 29), True),
        (5, date(2026, 10, 27), False),
        (5, date(2026, 10, 26), True),
    ],
)
def test_the_threshold_is_set_by_the_caller(threshold: int, last_bar: date, stale: bool) -> None:
    assert assess("US", last_bar, NOW, threshold=threshold).stale is stale


@pytest.mark.parametrize("threshold", [0, -1, -10])
def test_a_threshold_below_one_is_a_programming_error(threshold: int) -> None:
    with pytest.raises(ValueError, match="threshold must be at least 1"):
        assess("US", MONDAY, NOW, threshold=threshold)


def test_weekends_do_not_count_as_lag() -> None:
    friday = date(2026, 10, 30)
    sunday = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    monday_morning = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    assert assess("US", friday, sunday, threshold=3) == Freshness(friday, friday, 0, False)
    assert assess("US", friday, monday_morning, threshold=3) == Freshness(friday, friday, 0, False)


def test_a_single_market_holiday_does_not_trip_the_default_threshold() -> None:
    # Good Friday 2026-04-03: no bar for it. On Monday evening Thursday's bar is two sessions old.
    monday_evening = datetime(2026, 4, 6, 22, 0, tzinfo=timezone.utc)
    result = assess("US", date(2026, 4, 2), monday_evening, threshold=3)
    assert result.expected_latest == date(2026, 4, 6)
    assert (result.sessions_behind, result.stale) == (2, False)


def test_each_exchange_is_judged_on_its_own_calendar() -> None:
    # At 20:45 UTC Zurich's bar for Tuesday is due (20:30 UTC) and New York's (21:30 UTC) is not.
    moment = datetime(2026, 11, 3, 20, 45, tzinfo=timezone.utc)
    swiss = assess("SW", MONDAY, moment, threshold=3)
    american = assess("US", MONDAY, moment, threshold=3)
    assert (swiss.expected_latest, swiss.sessions_behind) == (date(2026, 11, 3), 1)
    assert (american.expected_latest, american.sessions_behind) == (MONDAY, 0)


def test_an_unknown_exchange_is_judged_on_the_default_calendar() -> None:
    result = assess("ZZ", date(2026, 10, 30), NOW, threshold=3)
    assert result.expected_latest == MONDAY
    assert result.sessions_behind == 1


def test_crypto_counts_the_weekend() -> None:
    saturday_noon = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    result = assess("CC", date(2026, 11, 3), saturday_noon, threshold=3)
    assert result.expected_latest == date(2026, 11, 6)
    assert (result.sessions_behind, result.stale) == (3, True)


def test_freshness_is_immutable() -> None:
    result = assess("US", MONDAY, NOW, threshold=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.stale = True  # type: ignore[misc]


# ------------------------------------------------------------------------------------------ stale_warning


def roche_like_warning() -> ToolWarning:
    symbol = parse_symbol("ROG.SW")
    return stale_warning(symbol, assess("SW", date(2026, 3, 25), NOW, threshold=3))


def test_the_warning_is_a_stale_data_warning() -> None:
    warning = roche_like_warning()
    assert isinstance(warning, ToolWarning)
    assert warning.code == WarningCode.STALE_DATA.value


def test_the_warning_names_the_symbol_the_dates_and_the_gap() -> None:
    message = roche_like_warning().message
    assert "ROG.SW" in message
    assert "2026-03-25" in message
    assert "2026-11-02" in message
    assert "158 sessions behind" in message


def test_the_warning_says_a_200_proves_nothing() -> None:
    message = roche_like_warning().message
    assert "HTTP 200" in message
    assert "frozen" in message
    assert "delisted" in message
    assert "renamed" in message


def test_the_warning_points_to_check_symbol_and_a_name_search() -> None:
    message = roche_like_warning().message
    assert "check_symbol" in message
    assert "search_symbols" in message
    assert "company name" in message


def test_the_warning_details_are_structured() -> None:
    assert roche_like_warning().details == {
        "symbol": "ROG.SW",
        "last_bar": "2026-03-25",
        "expected_latest": "2026-11-02",
        "sessions_behind": 158,
    }


def test_one_session_is_not_pluralised() -> None:
    message = stale_warning(Symbol("ACME", "US"), assess("US", date(2026, 10, 30), NOW, threshold=1)).message
    assert "1 session behind" in message
    assert "1 sessions" not in message


def test_the_warning_for_no_bar_says_so() -> None:
    warning = stale_warning(Symbol("ACME", "US"), assess("US", None, NOW, threshold=3))
    assert "no bar" in warning.message
    assert "ACME.US" in warning.message
    assert "2026-11-02" in warning.message
    assert "behind" not in warning.message
    assert "HTTP 200" in warning.message
    assert "check_symbol" in warning.message
    assert "search_symbols" in warning.message
    assert warning.details == {
        "symbol": "ACME.US",
        "last_bar": None,
        "expected_latest": "2026-11-02",
        "sessions_behind": 0,
    }


def test_the_warning_is_built_from_the_assessment_alone() -> None:
    fresh = assess("US", MONDAY, NOW, threshold=3)
    warning = stale_warning(Symbol("ACME", "US"), fresh)
    assert warning.code == WarningCode.STALE_DATA.value
    assert "0 sessions behind" in warning.message
