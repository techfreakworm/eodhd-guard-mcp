"""The shape of every successful tool result: data, warnings, and what the answer cost."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from eodhd_guard_mcp.client import ApiResponse


class WarningCode(str, Enum):
    """Machine-readable reason for a warning. See docs/DESIGN.md for when each one is raised."""

    STALE_DATA = "STALE_DATA"
    FILLER_BARS_REMOVED = "FILLER_BARS_REMOVED"
    RANGE_TRIMMED = "RANGE_TRIMMED"
    RANGE_CLAMPED = "RANGE_CLAMPED"
    TRUNCATED = "TRUNCATED"
    SPINOFF_ADJUSTED_CLOSE = "SPINOFF_ADJUSTED_CLOSE"
    CURRENCY_NOTE = "CURRENCY_NOTE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    DELAYED_QUOTE = "DELAYED_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    NO_QUOTE = "NO_QUOTE"
    INTRADAY_NOT_SAME_DAY = "INTRADAY_NOT_SAME_DAY"
    NO_INTRADAY_DATA = "NO_INTRADAY_DATA"
    FILING_DATE_UNRELIABLE = "FILING_DATE_UNRELIABLE"
    DIVIDEND_YIELD_INCONSISTENT = "DIVIDEND_YIELD_INCONSISTENT"
    EPS_BASIS = "EPS_BASIS"
    CALENDAR_TIMING_UNRELIABLE = "CALENDAR_TIMING_UNRELIABLE"
    NO_CALENDAR_ROWS = "NO_CALENDAR_ROWS"
    SENTIMENT_UNRELIABLE = "SENTIMENT_UNRELIABLE"
    LOW_ARTICLE_COUNT = "LOW_ARTICLE_COUNT"
    SCORES_DROPPED = "SCORES_DROPPED"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    NOT_COVERED = "NOT_COVERED"
    POSSIBLY_INACTIVE = "POSSIBLY_INACTIVE"
    ODD_SPLIT_RATIO = "ODD_SPLIT_RATIO"
    EMPTY_RESULT = "EMPTY_RESULT"
    BUDGET_LOW = "BUDGET_LOW"


class ToolWarning(BaseModel):
    """Something the user should know about the data, even though the call succeeded."""

    code: str = Field(description="Machine-readable warning code, e.g. STALE_DATA.")
    message: str = Field(description="What it means, in a sentence or two. Pass it on to the user.")
    details: dict[str, Any] | None = Field(default=None, description="Optional structured detail.")

    @classmethod
    def of(cls, code: WarningCode, message: str, **details: Any) -> ToolWarning:
        return cls(code=code.value, message=message, details=details or None)


class Meta(BaseModel):
    """Where the answer came from and what it cost."""

    source: str = "EODHD"
    endpoints: list[str] = Field(default_factory=list, description="EODHD endpoints used for this answer.")
    cost: int = Field(description="API calls charged for this tool call. Cached answers cost 0.")
    cached: bool = Field(description="True when every upstream answer came from the local cache.")
    fetched_at: str | None = Field(
        default=None, description="When EODHD produced the oldest answer used (ISO 8601, UTC)."
    )
    notes: list[str] = Field(default_factory=list, description="How to read the data.")


class ToolResponse(BaseModel):
    """Every successful tool result."""

    data: Any
    warnings: list[ToolWarning] = Field(default_factory=list)
    meta: Meta


def iso_utc(moment: datetime) -> str:
    """Format an aware datetime as ISO 8601 in UTC with a trailing Z, to the second."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_response(
    data: Any,
    responses: Sequence[ApiResponse],
    *,
    warnings: Sequence[ToolWarning] = (),
    notes: Sequence[str] = (),
) -> ToolResponse:
    """Wrap data with the warnings and the cost of the upstream answers it was built from."""
    endpoints: list[str] = []
    for response in responses:
        if response.endpoint not in endpoints:
            endpoints.append(response.endpoint)
    fetched = min((r.fetched_at for r in responses), default=None)
    meta = Meta(
        endpoints=endpoints,
        cost=sum(r.cost for r in responses),
        cached=all(r.cached for r in responses),
        fetched_at=iso_utc(fetched) if fetched is not None else None,
        notes=list(notes),
    )
    return ToolResponse(data=data, warnings=list(warnings), meta=meta)
