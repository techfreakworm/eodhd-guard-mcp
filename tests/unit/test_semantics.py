"""Unit tests for eodhd_guard_mcp.semantics."""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from eodhd_guard_mcp.envelope import ToolWarning, WarningCode
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.semantics import (
    KNOWN_SPINOFF_ADJUSTMENTS,
    PRICE_BASIS_NOTES,
    Bar,
    apply_basis,
    is_filler_bar,
    normalize_eod_rows,
    spinoff_warnings,
)
from eodhd_guard_mcp.validation import parse_symbol

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "guards_eod_acme_us.json"
D1, D2, D3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)


def row(day: str = "2026-01-05", **fields: Any) -> dict[str, Any]:
    """A valid EODHD end-of-day row; pass a field as ``...`` to leave it out."""
    base: dict[str, Any] = {
        "date": day,
        "open": 100,
        "high": 102.5,
        "low": 99.5,
        "close": 101,
        "adjusted_close": 100.5,
        "volume": 12345,
    }
    base.update(fields)
    return {key: value for key, value in base.items() if value is not ...}


def bar(
    day: date = D1,
    *,
    open_: float = 100.0,
    high: float = 102.5,
    low: float = 99.5,
    close: float = 101.0,
    adjusted_close: float | None = 100.5,
    volume: float = 12345.0,
) -> Bar:
    return Bar(
        day=day, open=open_, high=high, low=low, close=close, adjusted_close=adjusted_close, volume=volume
    )


def rejected(rows: Any) -> GuardError:
    with pytest.raises(GuardError) as info:
        normalize_eod_rows(rows)
    assert info.value.code is ErrorCode.BAD_RESPONSE
    return info.value


# --------------------------------------------------------------------------------- normalize_eod_rows


def test_the_synthetic_fixture_is_read_in_full_and_in_date_order() -> None:
    bars = normalize_eod_rows(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert len(bars) == 9
    assert [b.day for b in bars] == sorted(b.day for b in bars)
    assert bars[0] == Bar(
        day=date(2025, 11, 20),
        open=100.0,
        high=103.5,
        low=99.25,
        close=102.0,
        adjusted_close=99.96,
        volume=1200000.0,
    )
    assert [b.day.isoformat() for b in bars if is_filler_bar(b)] == ["2025-11-27"]


def test_integers_and_floats_both_become_floats() -> None:
    [only] = normalize_eod_rows([row(open=100, high=102, low=99, close=101, adjusted_close=100, volume=7)])
    for value in (only.open, only.high, only.low, only.close, only.adjusted_close, only.volume):
        assert type(value) is float


def test_rows_are_sorted_by_date() -> None:
    rows = [row("2026-01-07"), row("2026-01-05"), row("2026-01-06")]
    assert [b.day for b in normalize_eod_rows(rows)] == [D1, D2, D3]


def test_a_repeated_date_keeps_the_later_row() -> None:
    bars = normalize_eod_rows(
        [row("2026-01-05", close=1), row("2026-01-06", close=5), row("2026-01-05", close=2)]
    )
    assert [(b.day, b.close) for b in bars] == [(D1, 2.0), (D2, 5.0)]


def test_an_unreadable_repeat_does_not_displace_a_good_row() -> None:
    bars = normalize_eod_rows([row("2026-01-05", close=7), row("2026-01-05", close="NA")])
    assert [(b.day, b.close) for b in bars] == [(D1, 7.0)]


def test_an_empty_list_is_returned_empty_not_refused() -> None:
    assert normalize_eod_rows([]) == []


def test_the_input_is_not_modified() -> None:
    rows = [row("2026-01-06"), row("2026-01-05")]
    snapshot = json.loads(json.dumps(rows))
    normalize_eod_rows(rows)
    assert rows == snapshot


UNREADABLE_ROWS = [
    pytest.param(row(date=...), id="no-date"),
    pytest.param(row(open=...), id="no-open"),
    pytest.param(row(high=...), id="no-high"),
    pytest.param(row(low=...), id="no-low"),
    pytest.param(row(close=...), id="no-close"),
    pytest.param(row(volume=...), id="no-volume"),
    pytest.param(row(open="NA"), id="open-NA"),
    pytest.param(row(close="NA"), id="close-NA"),
    pytest.param(row(volume="NA"), id="volume-NA"),
    pytest.param(row(close="101.5"), id="close-numeric-string"),
    pytest.param(row(volume="12345"), id="volume-numeric-string"),
    pytest.param(row(open=None), id="open-null"),
    pytest.param(row(high=None), id="high-null"),
    pytest.param(row(low=None), id="low-null"),
    pytest.param(row(close=None), id="close-null"),
    pytest.param(row(volume=None), id="volume-null"),
    pytest.param(row(close=True), id="close-bool"),
    pytest.param(row(volume=False), id="volume-bool"),
    pytest.param(row(close=float("nan")), id="close-nan"),
    pytest.param(row(high=float("inf")), id="high-inf"),
    pytest.param(row(low=float("-inf")), id="low-minus-inf"),
    pytest.param(row(volume=10**400), id="volume-too-big-for-a-float"),
    pytest.param(row(close=[101]), id="close-list"),
    pytest.param(row(close={"v": 1}), id="close-object"),
    pytest.param(row("2026-02-30"), id="date-not-real"),
    pytest.param(row("20260105"), id="date-compact"),
    pytest.param(row("2026-1-5"), id="date-unpadded"),
    pytest.param(row("0000-00-00"), id="date-zeros"),
    pytest.param(row(""), id="date-empty"),
    pytest.param(row(date=None), id="date-null"),
    pytest.param(row(date=20260105), id="date-int"),
    pytest.param(row("2026-01-05T00:00:00"), id="date-with-time"),
    pytest.param(None, id="row-null"),
    pytest.param("2026-01-05", id="row-string"),
    pytest.param(5, id="row-int"),
    pytest.param([1, 2, 3], id="row-list"),
    pytest.param({}, id="row-empty-object"),
]


@pytest.mark.parametrize("bad", UNREADABLE_ROWS)
def test_an_unreadable_row_is_skipped_and_the_good_ones_are_kept(bad: Any) -> None:
    bars = normalize_eod_rows([row("2026-01-06"), bad, row("2026-01-07")])
    assert [b.day for b in bars] == [D2, D3]


@pytest.mark.parametrize("bad", UNREADABLE_ROWS)
def test_a_list_of_only_unreadable_rows_is_a_bad_response(bad: Any) -> None:
    error = rejected([bad])
    assert "None of the 1 rows" in error.message
    assert "could be read as a daily bar" in error.message


@pytest.mark.parametrize("adjusted", ["NA", None, True, float("nan"), float("inf"), "99.5", [99.5]], ids=repr)
def test_an_unreadable_adjusted_close_leaves_the_bar_with_none(adjusted: Any) -> None:
    [only] = normalize_eod_rows([row(adjusted_close=adjusted)])
    assert only.adjusted_close is None
    assert only.close == 101.0


def test_a_missing_adjusted_close_is_allowed() -> None:
    [only] = normalize_eod_rows([row(adjusted_close=...)])
    assert only.adjusted_close is None


@pytest.mark.parametrize(("given", "expected"), [(100, 100.0), (99.25, 99.25), (0, 0.0), (-1.5, -1.5)])
def test_a_numeric_adjusted_close_is_kept(given: float, expected: float) -> None:
    [only] = normalize_eod_rows([row(adjusted_close=given)])
    assert only.adjusted_close == expected


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"code": "ERR", "message": "boom"}, id="object"),
        pytest.param({}, id="empty-object"),
        pytest.param("Ticker Not Found.", id="string"),
        pytest.param(None, id="null"),
        pytest.param(0, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(("2026-01-05",), id="tuple"),
    ],
)
def test_anything_that_is_not_a_list_is_a_bad_response(body: Any) -> None:
    error = rejected(body)
    assert "where a list of daily bars was expected" in error.message
    assert error.hint is not None
    assert "cannot be trusted" in error.hint


def test_a_bad_response_names_the_keys_of_an_object_but_not_its_values() -> None:
    error = rejected({"code": "ERR", "message": "a confidential detail"})
    assert "an object with the keys code, message" in error.message
    assert "confidential" not in error.message


def test_a_bad_response_shows_only_a_few_short_keys() -> None:
    error = rejected({f"key{n}": n for n in range(20)} | {"x" * 100: 1})
    assert "key0, key1, key2, key3, key4" in error.message
    assert "key5" not in error.message
    assert "x" * 31 not in rejected({"x" * 100: 1}).message


def test_a_bad_response_for_a_scalar_names_only_its_type() -> None:
    assert "a str value" in rejected("Ticker Not Found.").message
    assert "null" in rejected(None).message
    assert "an empty object" in rejected({}).message


def test_the_count_of_unreadable_rows_is_reported() -> None:
    error = rejected(["NA", None, {}, {"date": "2026-01-05"}])
    assert "None of the 4 rows" in error.message


# ---------------------------------------------------------------------------------------- is_filler_bar


@pytest.mark.parametrize(
    ("open_", "high", "low", "close", "volume", "expected"),
    [
        (50.0, 50.0, 50.0, 50.0, 0.0, True),
        (50.25, 50.25, 50.25, 50.25, 0.0, True),
        (0.0, 0.0, 0.0, 0.0, 0.0, True),
        (50.0, 50.0, 50.0, 50.0, 1.0, False),
        (50.0, 50.0, 50.0, 50.0, 1200.0, False),
        (50.0, 50.5, 50.0, 50.0, 0.0, False),
        (50.0, 50.0, 49.5, 50.0, 0.0, False),
        (49.5, 50.0, 50.0, 50.0, 0.0, False),
        (50.0, 50.0, 50.0, 50.5, 0.0, False),
        (50.0, 51.0, 49.0, 50.5, 800.0, False),
    ],
)
def test_filler_bars_have_zero_volume_and_four_equal_prices(
    open_: float, high: float, low: float, close: float, volume: float, expected: bool
) -> None:
    assert is_filler_bar(bar(open_=open_, high=high, low=low, close=close, volume=volume)) is expected


def test_the_adjusted_close_plays_no_part_in_spotting_filler() -> None:
    flat = bar(open_=9.0, high=9.0, low=9.0, close=9.0, volume=0.0)
    assert is_filler_bar(dataclasses.replace(flat, adjusted_close=None))
    assert is_filler_bar(dataclasses.replace(flat, adjusted_close=4.5))


# ----------------------------------------------------------------------------------------- apply_basis


def test_both_gives_every_price_and_the_adjusted_close_in_order() -> None:
    [out] = apply_basis([bar()], "both")
    assert out == {
        "date": "2026-01-05",
        "open": 100.0,
        "high": 102.5,
        "low": 99.5,
        "close": 101.0,
        "adjusted_close": 100.5,
        "volume": 12345,
    }
    assert list(out) == ["date", "open", "high", "low", "close", "adjusted_close", "volume"]


def test_both_shows_a_missing_adjusted_close_as_none() -> None:
    [out] = apply_basis([bar(adjusted_close=None)], "both")
    assert out["adjusted_close"] is None


def test_raw_leaves_out_the_adjusted_close() -> None:
    [out] = apply_basis([bar()], "raw")
    assert list(out) == ["date", "open", "high", "low", "close", "volume"]
    assert out["close"] == 101.0


def test_raw_and_both_pass_the_reported_numbers_through_untouched() -> None:
    odd = bar(open_=100.1234567891, high=100.2, low=100.0000001, close=100.1, adjusted_close=0.1 + 0.2)
    [both] = apply_basis([odd], "both")
    [raw] = apply_basis([odd], "raw")
    assert (both["open"], both["low"], both["adjusted_close"]) == (100.1234567891, 100.0000001, 0.1 + 0.2)
    assert (raw["open"], raw["low"]) == (100.1234567891, 100.0000001)


def test_adjusted_scales_open_high_low_and_reports_the_adjusted_close_as_close() -> None:
    [out] = apply_basis(
        [bar(open_=100.0, high=110.0, low=90.0, close=105.0, adjusted_close=52.5)], "adjusted"
    )
    assert out == {
        "date": "2026-01-05",
        "open": 50.0,
        "high": 55.0,
        "low": 45.0,
        "close": 52.5,
        "volume": 12345,
    }
    assert list(out) == ["date", "open", "high", "low", "close", "volume"]


def test_adjusted_reads_the_fixture_without_float_noise() -> None:
    bars = normalize_eod_rows(json.loads(FIXTURE.read_text(encoding="utf-8")))
    out = apply_basis(bars, "adjusted")
    assert out[0] == {
        "date": "2025-11-20",
        "open": 98.0,
        "high": 101.43,
        "low": 97.265,
        "close": 99.96,
        "volume": 1200000,
    }
    for source, rendered in zip(bars, out, strict=True):
        assert rendered["close"] == source.adjusted_close
        for name in ("open", "high", "low"):
            assert rendered[name] == round(rendered[name], 6)


def test_adjusted_rounds_away_float_noise_to_six_decimals() -> None:
    [out] = apply_basis([bar(open_=1.1, high=1.3, low=0.9, close=3.0, adjusted_close=1.0)], "adjusted")
    assert (out["open"], out["high"], out["low"]) == (0.366667, 0.433333, 0.3)
    assert out["close"] == 1.0


def test_adjusted_keeps_six_significant_figures_for_tiny_prices() -> None:
    tiny = bar(
        open_=0.0000089, high=0.00000912345678, low=0.0000085, close=0.0000088, adjusted_close=0.0000044
    )
    [out] = apply_basis([tiny], "adjusted")
    assert out["open"] == pytest.approx(0.0000089 * 0.5, rel=1e-5)
    assert out["high"] == pytest.approx(0.00000912345678 * 0.5, rel=1e-5)
    assert out["open"] > 0
    assert out["low"] > 0


def test_adjusted_keeps_the_decimals_of_large_prices() -> None:
    big = bar(open_=700000.123456, high=700001.0, low=699999.5, close=700000.0, adjusted_close=630000.0)
    [out] = apply_basis([big], "adjusted")
    assert out["open"] == pytest.approx(700000.123456 * 0.9, abs=1e-5)
    assert out["close"] == 630000.0


def test_adjusted_with_nothing_to_adjust_gives_the_reported_prices() -> None:
    [out] = apply_basis([bar(close=101.0, adjusted_close=101.0)], "adjusted")
    assert (out["open"], out["high"], out["low"], out["close"]) == (100.0, 102.5, 99.5, 101.0)


def test_adjusted_without_an_adjusted_close_gives_none_not_the_raw_price() -> None:
    [out] = apply_basis([bar(adjusted_close=None)], "adjusted")
    assert (out["open"], out["high"], out["low"], out["close"]) == (None, None, None, None)
    assert out["volume"] == 12345


def test_adjusted_with_a_zero_close_and_a_zero_adjusted_close_stays_zero() -> None:
    dead = bar(open_=0.0, high=0.0, low=0.0, close=0.0, adjusted_close=0.0, volume=0.0)
    [out] = apply_basis([dead], "adjusted")
    assert (out["open"], out["high"], out["low"], out["close"]) == (0.0, 0.0, 0.0, 0.0)


def test_adjusted_with_a_zero_close_but_a_real_adjusted_close_cannot_scale() -> None:
    odd = bar(open_=10.0, high=11.0, low=9.0, close=0.0, adjusted_close=5.0)
    [out] = apply_basis([odd], "adjusted")
    assert (out["open"], out["high"], out["low"]) == (None, None, None)
    assert out["close"] == 5.0


def test_adjusted_gives_none_where_scaling_overflows() -> None:
    [out] = apply_basis([bar(open_=1e300, high=1e300, low=1.0, close=1.0, adjusted_close=1e300)], "adjusted")
    assert out["open"] is None
    assert out["high"] is None
    assert out["low"] == 1e300


def test_adjusted_zero_prices_stay_zero() -> None:
    [out] = apply_basis([bar(open_=0.0, high=2.0, low=0.0, close=2.0, adjusted_close=1.0)], "adjusted")
    assert (out["open"], out["high"], out["low"]) == (0.0, 1.0, 0.0)


@pytest.mark.parametrize("basis", ["both", "raw", "adjusted"])
def test_volume_is_a_whole_number_when_it_is_one(basis: Any) -> None:
    [out] = apply_basis([bar(volume=1200.0)], basis)
    assert out["volume"] == 1200
    assert isinstance(out["volume"], int)


@pytest.mark.parametrize("basis", ["both", "raw", "adjusted"])
def test_volume_keeps_its_fraction_when_it_has_one(basis: Any) -> None:
    [out] = apply_basis([bar(volume=12.5)], basis)
    assert out["volume"] == 12.5


@pytest.mark.parametrize("basis", ["both", "raw", "adjusted"])
def test_volume_is_never_adjusted(basis: Any) -> None:
    [out] = apply_basis([bar(close=100.0, adjusted_close=25.0, volume=777.0)], basis)
    assert out["volume"] == 777


@pytest.mark.parametrize("basis", ["both", "raw", "adjusted"])
def test_no_bars_give_no_rows(basis: Any) -> None:
    assert apply_basis([], basis) == []


@pytest.mark.parametrize("basis", ["both", "raw", "adjusted"])
def test_every_bar_becomes_one_row_in_order(basis: Any) -> None:
    rows = apply_basis([bar(D1), bar(D2), bar(D3)], basis)
    assert [r["date"] for r in rows] == ["2026-01-05", "2026-01-06", "2026-01-07"]


@pytest.mark.parametrize("basis", ["adjust", "ADJUSTED", "", "both ", None])
def test_an_unknown_basis_is_a_programming_error(basis: Any) -> None:
    with pytest.raises(ValueError, match="unknown price basis"):
        apply_basis([bar()], basis)


def test_a_bar_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        bar().close = 1.0  # type: ignore[misc]


# -------------------------------------------------------------------------------- spin-off registry


def test_the_registry_lists_the_known_events() -> None:
    assert dict(KNOWN_SPINOFF_ADJUSTMENTS) == {
        "NOVN.SW": ((date(2019, 4, 9), "Alcon spin-off"), (date(2023, 10, 4), "Sandoz spin-off")),
        "ULVR.LSE": ((date(2025, 12, 8), "The Magnum Ice Cream Company demerger"),),
        "RELIANCE.NSE": ((date(2023, 7, 20), "Jio Financial Services demerger"),),
    }


def test_registry_keys_are_full_upper_case_symbols_and_events_run_oldest_first() -> None:
    for key, events in KNOWN_SPINOFF_ADJUSTMENTS.items():
        assert parse_symbol(key).full == key
        assert [day for day, _ in events] == sorted(day for day, _ in events)
        assert all(text for _, text in events)


def test_the_registry_is_read_only() -> None:
    with pytest.raises(TypeError):
        KNOWN_SPINOFF_ADJUSTMENTS["ACME.US"] = ()  # type: ignore[index]


def test_a_range_from_before_every_event_warns_once_per_event_oldest_first() -> None:
    warnings = spinoff_warnings(parse_symbol("NOVN.SW"), date(2015, 1, 1))
    assert [w.details["event"] for w in warnings if w.details] == ["Alcon spin-off", "Sandoz spin-off"]
    assert all(isinstance(w, ToolWarning) for w in warnings)
    assert {w.code for w in warnings} == {WarningCode.SPINOFF_ADJUSTED_CLOSE.value}


def test_the_warning_names_the_event_and_says_what_it_does_to_prices() -> None:
    [warning] = spinoff_warnings(parse_symbol("ULVR.LSE"), date(2025, 1, 1))
    assert warning.message.startswith("The Magnum Ice Cream Company demerger (2025-12-08): ")
    assert "the range starts before this event" in warning.message
    assert "back-adjusts close" in warning.message
    assert "traded" in warning.message
    assert warning.details == {
        "symbol": "ULVR.LSE",
        "event": "The Magnum Ice Cream Company demerger",
        "event_date": "2025-12-08",
    }


@pytest.mark.parametrize(
    ("first_day", "events"),
    [
        (date(2019, 4, 8), ["Alcon spin-off", "Sandoz spin-off"]),
        (date(2019, 4, 9), ["Sandoz spin-off"]),
        (date(2019, 4, 10), ["Sandoz spin-off"]),
        (date(2023, 10, 3), ["Sandoz spin-off"]),
        (date(2023, 10, 4), []),
        (date(2023, 10, 5), []),
        (date(2026, 11, 3), []),
    ],
)
def test_a_warning_needs_the_range_to_start_before_the_event_date(first_day: date, events: list[str]) -> None:
    warnings = spinoff_warnings(parse_symbol("NOVN.SW"), first_day)
    assert [w.details["event"] for w in warnings if w.details] == events


@pytest.mark.parametrize(
    ("raw", "event_day"),
    [("ULVR.LSE", date(2025, 12, 8)), ("RELIANCE.NSE", date(2023, 7, 20)), ("NOVN.SW", date(2023, 10, 4))],
)
def test_the_day_before_an_event_warns_and_the_day_itself_does_not(raw: str, event_day: date) -> None:
    symbol = parse_symbol(raw)

    def event_dates(first_day: date) -> list[str]:
        return [w.details["event_date"] for w in spinoff_warnings(symbol, first_day) if w.details]

    assert event_day.isoformat() in event_dates(date.fromordinal(event_day.toordinal() - 1))
    assert event_day.isoformat() not in event_dates(event_day)
    assert event_dates(date.fromordinal(event_day.toordinal() + 1)) == []


@pytest.mark.parametrize("raw", ["ACME.US", "GLOBEX.LSE", "INITECH.SW", "UMBRELLA.NSE"])
def test_symbols_without_a_known_event_get_no_warning(raw: str) -> None:
    assert spinoff_warnings(parse_symbol(raw), date(1990, 1, 1)) == []


# ------------------------------------------------------------------------------- PRICE_BASIS_NOTES

AS_REPORTED = (
    "open/high/low/close are as EODHD reports them: not adjusted for splits or dividends, but sometimes "
    "back-adjusted for spin-offs and demergers, so they may differ from the traded price. adjusted_close "
    "is adjusted for splits and dividends; use it for returns."
)


def test_the_notes_cover_every_basis() -> None:
    assert set(PRICE_BASIS_NOTES) == {"both", "raw", "adjusted"}


@pytest.mark.parametrize("basis", ["both", "raw"])
def test_the_note_for_reported_prices_is_the_design_sentence(basis: str) -> None:
    assert PRICE_BASIS_NOTES[basis] == AS_REPORTED


def test_the_note_for_adjusted_prices_explains_the_scaling_and_the_volume() -> None:
    note = PRICE_BASIS_NOTES["adjusted"]
    assert note != AS_REPORTED
    assert "adjusted_close / close" in note
    assert "Volume is as reported" in note
    assert "returns" in note


def test_the_notes_are_read_only() -> None:
    with pytest.raises(TypeError):
        PRICE_BASIS_NOTES["both"] = "x"  # type: ignore[index]


def test_every_note_is_one_plain_paragraph() -> None:
    for note in PRICE_BASIS_NOTES.values():
        assert "\n" not in note
        assert note.endswith(".")
