"""The HTTP client for EODHD, and the one place that holds the API key.

``EODHDClient.get`` looks in the cache, checks the budget, sends the request with retries, records the
call in the ledger and turns every failure into a ``GuardError``. Exceptions from ``httpx2`` never leave
this module: their text can carry the request URL, and the URL carries the key.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx2

from eodhd_guard_mcp import __version__
from eodhd_guard_mcp.cache import CachedResponse, ResponseCache, normalize_params
from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.envelope import iso_utc
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.ledger import Budget, UsageLedger, next_gmt_reset
from eodhd_guard_mcp.redact import MASK, MIN_SECRET_LENGTH, redact

logger = logging.getLogger(__name__)

BACKOFF_BASE_S = 0.5
RETRY_AFTER_CAP_S = 5.0
NOT_FOUND_TTL_S = 24 * 3600.0
UPSTREAM_TEXT_LIMIT = 200

ACCOUNT_FIELDS = (
    "apiRequests",
    "apiRequestsDate",
    "dailyRateLimit",
    "extraLimit",
    "subscriptionType",
    "subscriptionMode",
)
"""The only fields of EODHD's ``user`` answer that leave this module. The rest is personal data."""

_PLANS = {
    "intraday": "EOD+Intraday or All-in-One",
    "fundamentals": "Fundamentals Data Feed or All-in-One",
}
_HTTPX_ERRORS = (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError)


@dataclass(frozen=True)
class ApiResponse:
    """A successful (HTTP 200) answer from EODHD, fresh or from the cache."""

    endpoint: str
    """The cost key the call was made under, for example ``eod``."""
    status: int
    data: Any
    """The parsed JSON body."""
    cached: bool
    cost: int
    """Units charged by this call: 0 for a cache hit."""
    fetched_at: datetime
    """When EODHD produced the answer; for a cache hit, when it was first fetched."""
    duration_ms: float


@dataclass(frozen=True)
class _Answer:
    """What came back from one try: an HTTP answer, or ``status == 0`` with the class of the failure."""

    status: int
    content: bytes
    received_at: datetime
    duration_ms: float
    attempts: int
    failure: str | None = None
    retry_after: str | None = None

    @property
    def retryable(self) -> bool:
        return self.status in (0, 429) or self.status >= 500


def _parse_json(content: bytes) -> tuple[bool, Any]:
    """``(True, data)`` for valid JSON, else ``(False, None)``.

    Failing here, rather than in an ``except`` block of the caller, keeps the decode error (which holds
    the whole body) from being chained to the ``GuardError`` raised next.
    """
    try:
        return True, json.loads(content)
    except ValueError:
        return False, None


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """What a ``Retry-After`` header asks for, as seconds from ``now``; None if absent or unreadable."""
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - now).total_seconds()
    return None if math.isnan(seconds) else max(seconds, 0.0)


def _retry_delay(attempt: int, retry_after: str | None, now: datetime) -> float:
    """Seconds to wait after failed try number ``attempt``: 0.5, 1, 2 ... or Retry-After, capped at 5."""
    backoff = BACKOFF_BASE_S * (1 << (attempt - 1))
    asked = _retry_after_seconds(retry_after, now)
    return backoff if asked is None else max(backoff, min(asked, RETRY_AFTER_CAP_S))


class EODHDClient:
    """Async client for the EODHD REST API.

    ``sleep`` replaces ``asyncio.sleep`` for the retry delays, which lets tests run without waiting.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        ledger: UsageLedger,
        cache: ResponseCache,
        budget: Budget,
        clock: Callable[[], datetime],
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._cache = cache
        self._budget = budget
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._base_url = settings.base_url.rstrip("/")
        # Cost of requests that passed the budget check but are not in the ledger yet.
        self._pending = 0
        self._http = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(settings.timeout_s),
            headers={"User-Agent": f"eodhd-guard-mcp/{__version__}"},
            follow_redirects=False,
        )

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        endpoint: str,
        cost: int,
        ttl_s: float,
        symbol: str | None = None,
    ) -> ApiResponse:
        """GET ``path`` (segments joined by ``/``; each is percent-encoded) and return the parsed JSON.

        ``endpoint`` is the cost key recorded in the ledger, ``cost`` what a successful answer is
        charged, and ``ttl_s`` how long a 200 answer is cached. The key and ``fmt=json`` are added here.
        A ``/`` inside a query can not be expressed in ``path``; replace it before calling.
        """
        api_key = self._settings.require_key()
        segments = self._segments(path)
        clean = normalize_params(params)
        clean.pop("fmt", None)
        cache_key = ResponseCache.make_key("/".join(segments), clean)

        started = time.perf_counter()
        cached = self._cache.get(cache_key)
        if cached is not None:
            served = self._serve_cached(cached, endpoint, symbol, started)
            if served is not None:
                return served

        self._budget.check(cost, pending=self._pending)
        self._pending += cost
        try:
            answer = await self._send(segments, clean, api_key, endpoint=endpoint, cost=cost, symbol=symbol)
        finally:
            self._pending -= cost

        if answer.status == 404:
            text = self._upstream_text(answer.content)
            self._cache.set(
                cache_key, endpoint=endpoint, status=404, body=text.encode(), ttl_s=NOT_FOUND_TTL_S
            )
            raise self._not_found(endpoint, text)
        if answer.status != 200:
            raise self._status_error(answer, endpoint)
        is_json, data = _parse_json(answer.content)
        if not is_json:
            raise self._error(
                ErrorCode.BAD_RESPONSE,
                "EODHD answered HTTP 200 but the body is not valid JSON.",
                hint="This is usually temporary, or a proxy answering in place of EODHD.",
                details={"endpoint": endpoint, "bytes": len(answer.content)},
            )
        self._cache.set(cache_key, endpoint=endpoint, status=200, body=answer.content, ttl_s=ttl_s)
        return ApiResponse(
            endpoint=endpoint,
            status=200,
            data=data,
            cached=False,
            cost=cost,
            fetched_at=answer.received_at,
            duration_ms=answer.duration_ms,
        )

    async def get_account(self) -> dict[str, Any]:
        """The account counters from the free ``user`` endpoint; it is never cached or budget-checked.

        Only the fields in ``ACCOUNT_FIELDS`` are returned (None when EODHD leaves one out). The name,
        email, payment method and invite token in the same answer are dropped.
        """
        api_key = self._settings.require_key()
        answer = await self._send(["user"], {}, api_key, endpoint="user", cost=0, symbol=None)
        if answer.status != 200:
            raise self._status_error(answer, "user")
        _, body = _parse_json(answer.content)
        if not isinstance(body, dict):
            raise self._error(
                ErrorCode.BAD_RESPONSE,
                "EODHD's account answer is not a JSON object.",
                details={"endpoint": "user", "bytes": len(answer.content)},
            )
        return {name: self._plain(body.get(name)) for name in ACCOUNT_FIELDS}

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _segments(path: str) -> list[str]:
        segments = path.strip("/").split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise GuardError(
                ErrorCode.INVALID_PARAMS, f"The request path {path!r} has an empty segment or a dot segment."
            )
        return segments

    def _serve_cached(
        self, cached: CachedResponse, endpoint: str, symbol: str | None, started: float
    ) -> ApiResponse | None:
        """Answer from a cache hit. A stored 404 raises; an unusable entry returns None (a miss)."""
        data: Any = None
        if cached.status == 200:
            is_json, data = _parse_json(cached.body)
            if not is_json:
                return None
        elif cached.status != 404:
            return None
        duration_ms = (time.perf_counter() - started) * 1000
        self._ledger.record(
            endpoint=endpoint,
            cost=0,
            status=cached.status,
            cached=True,
            duration_ms=duration_ms,
            bytes_=len(cached.body),
            symbol=symbol,
        )
        if cached.status == 404:
            raise self._not_found(endpoint, cached.body.decode("utf-8", errors="replace"))
        return ApiResponse(
            endpoint=endpoint,
            status=200,
            data=data,
            cached=True,
            cost=0,
            fetched_at=cached.created,
            duration_ms=duration_ms,
        )

    async def _send(
        self,
        segments: list[str],
        params: Mapping[str, str],
        api_key: str,
        *,
        endpoint: str,
        cost: int,
        symbol: str | None,
    ) -> _Answer:
        """Send the request, retrying 429, 5xx and network errors; return the last HTTP answer.

        Raises ``NETWORK_ERROR`` when the last try never got an answer.
        """
        url = f"{self._base_url}/" + "/".join(quote(segment, safe="") for segment in segments)
        query = {**params, "fmt": "json", "api_token": api_key}
        retries = max(self._settings.max_retries, 0)
        answer = await self._try(url, query, endpoint=endpoint, cost=cost, symbol=symbol, attempt=1)
        for retry in range(1, retries + 1):
            if not answer.retryable:
                break
            delay = _retry_delay(retry, answer.retry_after, self._clock())
            logger.warning(
                "%s: %s; retry %d of %d in %.1f s",
                endpoint,
                answer.failure or f"HTTP {answer.status}",
                retry,
                retries,
                delay,
            )
            await self._sleep(delay)
            answer = await self._try(
                url, query, endpoint=endpoint, cost=cost, symbol=symbol, attempt=retry + 1
            )
        if answer.failure is not None:
            raise self._error(
                ErrorCode.NETWORK_ERROR,
                f"Could not reach EODHD after {answer.attempts} attempt(s): {answer.failure}.",
                hint=(
                    "Check the network connection. If the error is a timeout, EODHD_GUARD_TIMEOUT "
                    f"(now {self._settings.timeout_s:g} s) can be raised."
                ),
                details={"endpoint": endpoint, "attempts": answer.attempts, "error": answer.failure},
            )
        return answer

    async def _try(
        self,
        url: str,
        query: Mapping[str, str],
        *,
        endpoint: str,
        cost: int,
        symbol: str | None,
        attempt: int,
    ) -> _Answer:
        """One request. Only the exception class survives, and every HTTP answer is recorded."""
        started = time.perf_counter()
        try:
            response = await self._http.get(url, params=query)
        except _HTTPX_ERRORS as exc:
            return _Answer(
                status=0,
                content=b"",
                received_at=self._clock(),
                duration_ms=(time.perf_counter() - started) * 1000,
                attempts=attempt,
                failure=type(exc).__name__,
            )
        duration_ms = (time.perf_counter() - started) * 1000
        status = response.status_code
        content = self._without_key(response.content)
        self._ledger.record(
            endpoint=endpoint,
            cost=cost if status == 200 else 0,
            status=status,
            cached=False,
            duration_ms=duration_ms,
            bytes_=len(content),
            symbol=symbol,
        )
        logger.debug("%s answered HTTP %d in %.0f ms", endpoint, status, duration_ms)
        return _Answer(
            status=status,
            content=content,
            received_at=self._clock(),
            duration_ms=duration_ms,
            attempts=attempt,
            retry_after=response.headers.get("Retry-After"),
        )

    def _without_key(self, content: bytes) -> bytes:
        """Mask the key if EODHD ever echoes it in a body, so it can reach neither the model nor the cache."""
        key = self._settings.api_key
        if key and len(key) >= MIN_SECRET_LENGTH and key.encode() in content:
            return content.replace(key.encode(), MASK.encode())
        return content

    def _upstream_text(self, content: bytes) -> str:
        """An error body as one line of at most 200 characters. The key is masked before the cut."""
        text = " ".join(content.decode("utf-8", errors="replace").split())
        return redact(text, [self._settings.api_key])[:UPSTREAM_TEXT_LIMIT]

    def _plain(self, value: Any) -> str | int | float | bool | None:
        """Keep a scalar from an answer; anything else becomes None."""
        if isinstance(value, str):
            return redact(value, [self._settings.api_key])
        if isinstance(value, (bool, int, float)):
            return value
        return None

    def _error(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> GuardError:
        """A ``GuardError`` whose text, hint and details are all masked."""
        secrets = [self._settings.api_key]
        return GuardError(
            code,
            redact(message, secrets),
            hint=redact(hint, secrets) if hint else None,
            details={
                name: redact(value, secrets) if isinstance(value, str) else value
                for name, value in (details or {}).items()
            },
        )

    def _not_found(self, endpoint: str, text: str) -> GuardError:
        message = f"EODHD answered HTTP 404: {text}" if text else "EODHD answered HTTP 404 with no text."
        return self._error(
            ErrorCode.NOT_FOUND, message, details={"endpoint": endpoint, "status": 404, "upstream": text}
        )

    def _status_error(self, answer: _Answer, endpoint: str) -> GuardError:
        """Map a non-200 HTTP answer to its error."""
        status = answer.status
        details: dict[str, Any] = {"endpoint": endpoint, "status": status}
        if status == 401:
            return self._error(
                ErrorCode.AUTH_ERROR,
                "EODHD rejected the API key (HTTP 401): it is missing, wrong or expired.",
                hint=(
                    "Check EODHD_API_KEY (or the file named by EODHD_API_KEY_FILE) "
                    "against the key on your EODHD dashboard."
                ),
                details=details,
            )
        if status == 402:
            resets_at = iso_utc(next_gmt_reset(self._clock()))
            return self._error(
                ErrorCode.QUOTA_EXCEEDED,
                "EODHD says this account's daily API limit is used up (HTTP 402). "
                f"It resets at 00:00 GMT (next: {resets_at}).",
                hint=(
                    "This server's own budget is separate; get_usage shows both. Other apps that share "
                    "the key count against the account limit too."
                ),
                details=details,
            )
        if status == 403:
            plans = _PLANS.get(endpoint)
            plans_text = (
                f"Plans that include it: {plans}."
                if plans
                else "A higher plan, such as All-in-One, may include it."
            )
            return self._error(
                ErrorCode.PLAN_ERROR,
                f"The EODHD subscription does not include the '{endpoint}' endpoint (HTTP 403). {plans_text}",
                hint="Compare the plans at https://eodhd.com/pricing.",
                details=details,
            )
        if status == 404:
            return self._not_found(endpoint, self._upstream_text(answer.content))
        if status == 429:
            return self._error(
                ErrorCode.RATE_LIMITED,
                f"EODHD kept answering HTTP 429 (too many requests) after {answer.attempts} attempt(s).",
                hint="Wait a few seconds and retry, or make fewer calls in parallel.",
                details={**details, "attempts": answer.attempts},
            )
        if status >= 500:
            return self._error(
                ErrorCode.UPSTREAM_ERROR,
                f"EODHD answered HTTP {status} after {answer.attempts} attempt(s).",
                hint="This is usually temporary; retry in a minute.",
                details={**details, "attempts": answer.attempts},
            )
        text = self._upstream_text(answer.content) if 400 <= status < 500 else ""
        return self._error(
            ErrorCode.UPSTREAM_ERROR,
            f"EODHD answered an unexpected HTTP {status}" + (f": {text}" if text else "."),
            details={**details, "upstream": text},
        )
