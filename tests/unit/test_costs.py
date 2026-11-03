from __future__ import annotations

import pytest

from eodhd_guard_mcp.costs import COSTS, cost_for

DESIGN_TABLE = {
    "eod": 1,
    "intraday": 5,
    "real-time": 1,
    "fundamentals": 10,
    "calendar/earnings": 1,
    "calendar/splits": 1,
    "calendar/dividends": 1,
    "div": 1,
    "splits": 1,
    "exchanges-list": 1,
    "exchange-symbol-list": 1,
    "search": 1,
    "news": 5,
    "sentiments": 5,
    "economic-events": 1,
    "user": 0,
}


def test_table_matches_the_design() -> None:
    assert {key: entry.units for key, entry in COSTS.items()} == DESIGN_TABLE


def test_each_entry_knows_its_own_key() -> None:
    assert all(key == entry.key for key, entry in COSTS.items())


def test_only_real_time_news_and_sentiments_are_priced_per_symbol() -> None:
    assert [key for key, entry in COSTS.items() if entry.per_symbol] == ["real-time", "news", "sentiments"]


def test_only_the_dividend_calendar_is_unmeasured() -> None:
    assert [key for key, entry in COSTS.items() if not entry.measured] == ["calendar/dividends"]


def test_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        COSTS["eod"] = COSTS["intraday"]  # type: ignore[index]


@pytest.mark.parametrize(("key", "units"), sorted(DESIGN_TABLE.items()))
def test_cost_for_a_single_symbol(key: str, units: int) -> None:
    assert cost_for(key) == units


def test_real_time_costs_one_per_symbol() -> None:
    assert cost_for("real-time", 4) == 4
    assert cost_for("real-time", 0) == 0


def test_other_endpoints_ignore_the_symbol_count() -> None:
    assert cost_for("eod", 9) == 1
    assert cost_for("fundamentals", 3) == 10


def test_unknown_key_raises_key_error() -> None:
    with pytest.raises(KeyError):
        cost_for("technical")


def test_negative_symbol_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        cost_for("real-time", -1)


def test_news_and_sentiments_cost_five_calls_per_ticker() -> None:
    assert cost_for("news", 1) == 5
    assert cost_for("news", 5) == 25
    assert cost_for("sentiments", 1) == 5
    assert cost_for("sentiments", 10) == 50


def test_a_topic_is_one_news_unit_because_the_tool_asks_for_at_least_one() -> None:
    # `cost_for` multiplies by the symbol count, so a topic (no symbols) must be priced with max(1, n).
    assert cost_for("news", max(1, 0)) == 5
    assert cost_for("news", 0) == 0


def test_economic_events_cost_one_call_whatever_the_window() -> None:
    assert cost_for("economic-events") == 1
    assert cost_for("economic-events", 30) == 1


def test_the_notes_say_what_was_measured() -> None:
    assert "measured for one ticker or a topic" in COSTS["news"].note
    assert "measured for one ticker" in COSTS["sentiments"].note
    assert all(COSTS[key].measured for key in ("news", "sentiments", "economic-events"))
