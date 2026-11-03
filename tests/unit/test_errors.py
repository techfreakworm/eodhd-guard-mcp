from __future__ import annotations

import pytest

from eodhd_guard_mcp.errors import ErrorCode, GuardError

DESIGN_CODES = {
    "CONFIG_ERROR",
    "INVALID_PARAMS",
    "NOT_COVERED",
    "NOT_FOUND",
    "NO_DATA",
    "AUTH_ERROR",
    "PLAN_ERROR",
    "QUOTA_EXCEEDED",
    "RATE_LIMITED",
    "BUDGET_EXCEEDED",
    "UPSTREAM_ERROR",
    "NETWORK_ERROR",
    "BAD_RESPONSE",
}


def test_error_codes_match_the_design() -> None:
    assert {code.value for code in ErrorCode} == DESIGN_CODES


def test_error_codes_are_strings() -> None:
    assert ErrorCode.NOT_FOUND == "NOT_FOUND"
    assert ErrorCode("NO_DATA") is ErrorCode.NO_DATA


def test_str_starts_with_the_code() -> None:
    error = GuardError(ErrorCode.NOT_COVERED, "Tokyo has no prices.")
    assert str(error) == "NOT_COVERED: Tokyo has no prices."


def test_str_appends_the_hint() -> None:
    error = GuardError(ErrorCode.NOT_COVERED, "Tokyo has no prices.", hint="Try J-Quants.")
    assert str(error) == "NOT_COVERED: Tokyo has no prices. Hint: Try J-Quants."


def test_attributes() -> None:
    error = GuardError(ErrorCode.NO_DATA, "Empty.", hint="Run check_symbol.", details={"symbol": "ACME.US"})
    assert error.code is ErrorCode.NO_DATA
    assert error.message == "Empty."
    assert error.hint == "Run check_symbol."
    assert error.details == {"symbol": "ACME.US"}


def test_details_default_to_an_empty_dict_and_are_copied() -> None:
    assert GuardError(ErrorCode.NO_DATA, "Empty.").details == {}
    original = {"a": 1}
    error = GuardError(ErrorCode.NO_DATA, "Empty.", details=original)
    original["a"] = 2
    assert error.details == {"a": 1}


def test_repr_shows_the_code_and_message() -> None:
    assert repr(GuardError(ErrorCode.AUTH_ERROR, "Bad key.")) == "GuardError(AUTH_ERROR, 'Bad key.')"


def test_it_can_be_raised_and_caught() -> None:
    with pytest.raises(GuardError, match="BUDGET_EXCEEDED") as caught:
        raise GuardError(ErrorCode.BUDGET_EXCEEDED, "Spent.")
    assert caught.value.code is ErrorCode.BUDGET_EXCEEDED
