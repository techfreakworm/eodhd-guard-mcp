"""Unit tests for eodhd_guard_mcp.coverage."""

from __future__ import annotations

import dataclasses
from itertools import product

import pytest

from eodhd_guard_mcp.coverage import (
    ESCAPE_HATCH,
    GAPS,
    CoverageGap,
    ensure_covered,
    ensure_exchange_covered,
    find_gap,
)
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.markets import MARKETS
from eodhd_guard_mcp.validation import Symbol, parse_symbol

TOKYO_SUFFIXES = ["TSE", "T", "JP", "JPX", "XTKS", "TYO"]
UAE_SUFFIXES = ["AE", "DFM", "ADX", "AD", "XDFM", "XADS"]
GAP_SUFFIXES = TOKYO_SUFFIXES + UAE_SUFFIXES
CAPABILITIES = ["prices", "quotes", "fundamentals", "dividends", "splits", "symbols"]
COVERED = [
    "ACME.US",
    "GLOBEX.LSE",
    "INITECH.SW",
    "UMBRELLA.NSE",
    "ACME.TO",  # Toronto
    "GLOBEX.TA",  # Tel Aviv
    "EURUSD.FOREX",
    "BTC-USD.CC",
]


def refusal(symbol: Symbol, capability: str) -> GuardError:
    with pytest.raises(GuardError) as info:
        ensure_covered(symbol, capability, allow_uncovered=False)
    assert info.value.code is ErrorCode.NOT_COVERED
    return info.value


# ---------------------------------------------------------------------------------------------- registry


def test_the_registry_holds_tokyo_and_the_uae() -> None:
    assert [gap.key for gap in GAPS] == ["tokyo", "uae"]


def test_tokyo_entry() -> None:
    gap = next(g for g in GAPS if g.key == "tokyo")
    assert gap.market == "Tokyo Stock Exchange"
    assert gap.suffixes == frozenset(TOKYO_SUFFIXES)
    assert gap.calendar_dates is True


def test_uae_entry() -> None:
    gap = next(g for g in GAPS if g.key == "uae")
    assert "UAE" in gap.market
    assert gap.suffixes == frozenset(UAE_SUFFIXES)
    assert gap.calendar_dates is False


def test_entries_are_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        GAPS[0].key = "other"  # type: ignore[misc]
    assert isinstance(GAPS[0].suffixes, frozenset)


def test_suffixes_do_not_overlap_each_other_or_any_known_market() -> None:
    all_suffixes = [suffix for gap in GAPS for suffix in gap.suffixes]
    assert len(all_suffixes) == len(set(all_suffixes))
    assert set(all_suffixes).isdisjoint(MARKETS)


@pytest.mark.parametrize("gap", GAPS, ids=lambda gap: gap.key)
def test_every_entry_carries_the_text_a_refusal_needs(gap: CoverageGap) -> None:
    assert "October 2026" in gap.message
    assert gap.alternatives.strip()
    assert gap.market.strip()


# ------------------------------------------------------------------------------------------- find_gap


@pytest.mark.parametrize("suffix", TOKYO_SUFFIXES)
def test_every_tokyo_suffix_finds_tokyo(suffix: str) -> None:
    gap = find_gap(suffix)
    assert gap is not None
    assert gap.key == "tokyo"


@pytest.mark.parametrize("suffix", UAE_SUFFIXES)
def test_every_uae_suffix_finds_the_uae(suffix: str) -> None:
    gap = find_gap(suffix)
    assert gap is not None
    assert gap.key == "uae"


@pytest.mark.parametrize("raw", ["tse", " Tse ", "xtks", "dfm\n", "Ae"])
def test_find_gap_ignores_case_and_surrounding_space(raw: str) -> None:
    assert find_gap(raw) is not None


def test_toronto_is_not_tokyo() -> None:
    """TO is Toronto and T is Tokyo; a prefix match would confuse them."""
    assert find_gap("TO") is None
    assert find_gap("to") is None
    assert find_gap("V") is None


def test_tel_aviv_is_not_the_uae() -> None:
    """TA is Tel Aviv and AE is the UAE; they must never be mixed up."""
    assert find_gap("TA") is None
    assert find_gap("ta") is None


@pytest.mark.parametrize(
    "exchange",
    ["", "TOKYO", "JAPAN", "UAE", "DUBAI", "AEX", "AU", "J", "TS", "JPY", "ADXX", "US", "LSE", "SW", "NSE"],
)
def test_other_codes_are_not_gaps(exchange: str) -> None:
    assert find_gap(exchange) is None


@pytest.mark.parametrize("code", sorted(MARKETS))
def test_no_market_in_the_trading_calendar_table_is_a_gap(code: str) -> None:
    assert find_gap(code) is None


# ---------------------------------------------------------------------------------------- ensure_covered


@pytest.mark.parametrize(("suffix", "capability"), list(product(GAP_SUFFIXES, CAPABILITIES)))
def test_a_gap_symbol_is_refused_for_every_capability(suffix: str, capability: str) -> None:
    error = refusal(Symbol(code="ACME", exchange=suffix), capability)
    assert error.message.startswith(f"ACME.{suffix}: EODHD has no ")


@pytest.mark.parametrize(
    ("capability", "phrase"),
    [
        ("prices", "no price data for the"),
        ("quotes", "no quotes for the"),
        ("fundamentals", "no fundamentals for the"),
        ("dividends", "no dividend data for the"),
        ("splits", "no split data for the"),
        ("symbols", "no symbol lists for the"),
    ],
)
def test_the_refusal_says_which_capability_is_missing(capability: str, phrase: str) -> None:
    assert phrase in refusal(parse_symbol("7203.TSE"), capability).message


def test_tokyo_refusal_says_what_is_missing_what_still_works_and_what_to_do() -> None:
    error = refusal(parse_symbol("7203.TSE"), "prices")
    assert error.message.startswith("7203.TSE: EODHD has no price data for the Tokyo Stock Exchange.")
    assert "In checks in October 2026" in error.message
    assert "fundamentals, dividends or symbol lists" in error.message
    assert "answered 404" in error.message
    assert "What still works" in error.message
    assert "earnings calendar" in error.message
    assert "get_earnings_calendar" in error.message
    assert error.hint is not None
    assert "J-Quants" in error.hint
    assert "TM.US" in error.hint
    assert f"set {ESCAPE_HATCH}" in error.hint
    assert ESCAPE_HATCH == "EODHD_GUARD_ALLOW_UNCOVERED=1"


def test_uae_refusal_says_what_is_missing_and_what_to_do() -> None:
    error = refusal(parse_symbol("EMAAR.AE"), "prices")
    assert error.message.startswith("EMAAR.AE: EODHD has no price data for the UAE")
    assert "In checks in October 2026" in error.message
    assert "earnings dates" in error.message
    assert "AE exchange is listed but its symbol list is empty" in error.message
    assert "HTTP 200 with zero rows" in error.message
    assert "What still works" not in error.message
    assert error.hint is not None
    assert "DFM" in error.hint
    assert "ADX" in error.hint
    assert f"set {ESCAPE_HATCH}" in error.hint


def test_the_refusal_renders_as_one_readable_string() -> None:
    text = str(refusal(parse_symbol("7203.TSE"), "prices"))
    assert text.startswith("NOT_COVERED: 7203.TSE: EODHD has no price data")
    assert " Hint: Alternatives: " in text
    assert text.endswith("to skip this check.")


def test_the_refusal_carries_structured_details() -> None:
    error = refusal(parse_symbol("EMAAR.DFM"), "fundamentals")
    assert error.details == {
        "subject": "EMAAR.DFM",
        "market": "UAE exchanges (DFM and ADX)",
        "gap": "uae",
        "capability": "fundamentals",
    }


@pytest.mark.parametrize(("suffix", "capability"), list(product(GAP_SUFFIXES, CAPABILITIES)))
def test_the_escape_hatch_lets_a_gap_symbol_through(suffix: str, capability: str) -> None:
    ensure_covered(Symbol(code="ACME", exchange=suffix), capability, allow_uncovered=True)


@pytest.mark.parametrize(("raw", "capability"), list(product(COVERED, CAPABILITIES)))
def test_covered_markets_pass_silently(raw: str, capability: str) -> None:
    ensure_covered(parse_symbol(raw), capability, allow_uncovered=False)


def test_toronto_and_tel_aviv_symbols_are_not_refused() -> None:
    ensure_covered(parse_symbol("ACME.TO"), "prices", allow_uncovered=False)
    ensure_covered(parse_symbol("GLOBEX.TA"), "prices", allow_uncovered=False)


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("raw", ["ACME.US", "7203.TSE"])
@pytest.mark.parametrize("capability", ["price", "PRICES", "", "calendar", "intraday"])
def test_an_unknown_capability_is_a_programming_error_even_when_nothing_would_be_refused(
    capability: str, raw: str, allow: bool
) -> None:
    with pytest.raises(ValueError, match="unknown capability"):
        ensure_covered(parse_symbol(raw), capability, allow_uncovered=allow)


def test_the_unknown_capability_error_lists_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="prices, quotes, fundamentals, dividends, splits, symbols"):
        ensure_covered(parse_symbol("ACME.US"), "bonds", allow_uncovered=False)


# --------------------------------------------------------------------------------- ensure_exchange_covered


@pytest.mark.parametrize("exchange", GAP_SUFFIXES)
def test_a_gap_exchange_is_refused_for_symbol_lists(exchange: str) -> None:
    with pytest.raises(GuardError) as info:
        ensure_exchange_covered(exchange, "symbols", allow_uncovered=False)
    assert info.value.code is ErrorCode.NOT_COVERED
    assert info.value.message.startswith(f"{exchange}: EODHD has no symbol lists for the ")
    assert info.value.hint is not None
    assert ESCAPE_HATCH in info.value.hint


def test_the_exchange_is_shown_upper_case_and_trimmed() -> None:
    with pytest.raises(GuardError) as info:
        ensure_exchange_covered(" tse ", "symbols", allow_uncovered=False)
    assert info.value.message.startswith("TSE: EODHD has no symbol lists for the Tokyo Stock Exchange.")
    assert info.value.details["subject"] == "TSE"


@pytest.mark.parametrize("exchange", ["AE", "TSE"])
def test_the_escape_hatch_lets_a_gap_exchange_through(exchange: str) -> None:
    ensure_exchange_covered(exchange, "symbols", allow_uncovered=True)


@pytest.mark.parametrize("exchange", ["US", "LSE", "TO", "TA", "INDX"])
def test_covered_exchanges_pass_silently(exchange: str) -> None:
    ensure_exchange_covered(exchange, "symbols", allow_uncovered=False)


def test_an_unknown_capability_is_refused_for_exchanges_too() -> None:
    with pytest.raises(ValueError, match="unknown capability"):
        ensure_exchange_covered("US", "bonds", allow_uncovered=False)
