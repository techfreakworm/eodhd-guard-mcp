"""Error codes and the one exception type the server raises on purpose.

A ``GuardError`` is an anticipated failure: bad input, a market EODHD does not cover, an answer that
looks like data but is not, an exhausted budget. The tools turn it into an MCP error result whose text
starts with the code, so the model can act on it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Machine-readable reason for a failed tool call."""

    CONFIG_ERROR = "CONFIG_ERROR"
    """The server is misconfigured, for example the API key is missing."""
    INVALID_PARAMS = "INVALID_PARAMS"
    """A parameter is malformed or out of range. EODHD would have ignored it silently."""
    NOT_COVERED = "NOT_COVERED"
    """EODHD has no data for this market (Tokyo, the UAE)."""
    NOT_FOUND = "NOT_FOUND"
    """EODHD answered 404: unknown ticker or exchange."""
    NO_DATA = "NO_DATA"
    """EODHD answered 200 but with nothing usable (an empty list, "NA" fields)."""
    AUTH_ERROR = "AUTH_ERROR"
    """EODHD answered 401: the key is missing, wrong or expired."""
    PLAN_ERROR = "PLAN_ERROR"
    """EODHD answered 403: the subscription does not include this endpoint."""
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    """EODHD answered 402: the account's daily API limit is used up."""
    RATE_LIMITED = "RATE_LIMITED"
    """EODHD answered 429 and kept doing so after retries."""
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    """This server's own daily budget would be exceeded by the call."""
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    """EODHD failed (5xx or an unexpected status)."""
    NETWORK_ERROR = "NETWORK_ERROR"
    """EODHD could not be reached."""
    BAD_RESPONSE = "BAD_RESPONSE"
    """EODHD answered 200 with a body of an unexpected shape."""


class GuardError(Exception):
    """An anticipated failure with a code, a message for the model, and an optional hint."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:
        text = f"{self.code.value}: {self.message}"
        if self.hint:
            text += f" Hint: {self.hint}"
        return text

    def __repr__(self) -> str:
        return f"GuardError({self.code.value!s}, {self.message!r})"
