"""A local record of every call, and the daily budget built on it.

The ledger never holds the API key, a URL or a response body: only the endpoint, the symbol, the status,
the cost and the timing of each call.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from eodhd_guard_mcp.envelope import iso_utc
from eodhd_guard_mcp.errors import ErrorCode, GuardError

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    cost INTEGER NOT NULL,
    status INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    bytes INTEGER NOT NULL,
    symbol TEXT
);
CREATE INDEX IF NOT EXISTS calls_day ON calls (day);
"""

_STATE_HINT = "Check that EODHD_GUARD_STATE_DIR is a writable folder."


def _connect(path: Path) -> sqlite3.Connection:
    """Open the database with a private folder and file, in autocommit mode and WAL where available."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    except (OSError, sqlite3.Error) as exc:
        raise GuardError(
            ErrorCode.CONFIG_ERROR,
            f"Cannot open the usage ledger at {path}: {type(exc).__name__}.",
            hint=_STATE_HINT,
        ) from None
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _gmt_day(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).date().isoformat()


def next_gmt_reset(moment: datetime) -> datetime:
    """The next 00:00 GMT after ``moment``, when EODHD's daily counter starts again."""
    tomorrow = moment.astimezone(timezone.utc).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(), tzinfo=timezone.utc)


class UsageLedger:
    """Append-only log of calls in ``usage.sqlite``, grouped by GMT day.

    ``clock`` must return an aware datetime. Rows from days more than ``RETENTION_DAYS`` back are pruned
    when the ledger opens.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = _connect(path)
        self._started = clock()
        try:
            self._conn.executescript(_SCHEMA)
            cutoff = _gmt_day(self._started - timedelta(days=RETENTION_DAYS))
            self._conn.execute("DELETE FROM calls WHERE day < ?", (cutoff,))
        except sqlite3.Error as exc:
            self._conn.close()
            raise GuardError(
                ErrorCode.CONFIG_ERROR,
                f"Cannot use the usage ledger at {path}: {type(exc).__name__}.",
                hint=_STATE_HINT,
            ) from None

    def record(
        self,
        *,
        endpoint: str,
        cost: int,
        status: int,
        cached: bool,
        duration_ms: float,
        bytes_: int,
        symbol: str | None = None,
    ) -> None:
        """Add one row. A failed write is logged, not raised, so a paid answer is never thrown away."""
        now = self._clock()
        row = (
            now.timestamp(),
            _gmt_day(now),
            endpoint,
            cost,
            status,
            int(cached),
            duration_ms,
            bytes_,
            symbol,
        )
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO calls (ts, day, endpoint, cost, status, cached, duration_ms, bytes, symbol)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
        except sqlite3.Error as exc:
            logger.warning("Usage ledger write failed (%s); this call is not counted.", type(exc).__name__)

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise GuardError(
                ErrorCode.CONFIG_ERROR,
                f"The usage ledger at {self._path} cannot be read: {type(exc).__name__}.",
                hint=_STATE_HINT,
            ) from None

    def spent_today(self) -> int:
        """Cost units charged so far on the current GMT day."""
        rows = self._rows(
            "SELECT COALESCE(SUM(cost), 0) FROM calls WHERE day = ?", (_gmt_day(self._clock()),)
        )
        return int(rows[0][0])

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Totals for today (by endpoint), for each of the last ``days`` GMT days, and for this session.

        ``requests`` counts answers from EODHD, failed attempts included; ``cache_hits`` counts answers
        served locally. ``days`` lists only days that have rows, newest first, and is capped at
        ``RETENTION_DAYS``.
        """
        now = self._clock()
        today = _gmt_day(now)
        first_day = _gmt_day(now - timedelta(days=min(max(days, 1), RETENTION_DAYS) - 1))
        counts = "SUM(cost), SUM(cached = 0), SUM(cached)"
        by_endpoint = self._rows(
            f"SELECT endpoint, {counts} FROM calls WHERE day = ? GROUP BY endpoint ORDER BY endpoint",
            (today,),
        )
        by_day = self._rows(
            f"SELECT day, {counts} FROM calls WHERE day >= ? GROUP BY day ORDER BY day DESC", (first_day,)
        )
        session = self._rows(
            "SELECT COALESCE(SUM(cost), 0), COALESCE(SUM(cached = 0), 0), COALESCE(SUM(cached), 0)"
            " FROM calls WHERE ts >= ?",
            (self._started.timestamp(),),
        )[0]
        return {
            "today": {
                "day": today,
                "cost": sum(row[1] for row in by_endpoint),
                "requests": sum(row[2] for row in by_endpoint),
                "cache_hits": sum(row[3] for row in by_endpoint),
                "by_endpoint": {
                    row[0]: {"cost": row[1], "requests": row[2], "cache_hits": row[3]} for row in by_endpoint
                },
            },
            "days": [
                {"day": row[0], "cost": row[1], "requests": row[2], "cache_hits": row[3]} for row in by_day
            ],
            "session": {
                "since": iso_utc(self._started),
                "cost": session[0],
                "requests": session[1],
                "cache_hits": session[2],
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Budget:
    """A cap on the cost units spent per GMT day. A limit of 0 means no cap."""

    def __init__(self, ledger: UsageLedger, daily_limit: int, *, clock: Callable[[], datetime]) -> None:
        self._ledger = ledger
        self._limit = max(daily_limit, 0)
        self._clock = clock

    def check(self, cost: int, *, pending: int = 0) -> None:
        """Raise ``GuardError(BUDGET_EXCEEDED)`` if spending ``cost`` more would pass the limit.

        ``pending`` is the cost of calls already in flight that the ledger has not seen yet.
        """
        if self._limit == 0 or cost <= 0:
            return
        spent = self._ledger.spent_today()
        if spent + pending + cost <= self._limit:
            return
        resets_at = iso_utc(next_gmt_reset(self._clock()))
        in_flight = f" (plus {pending} in flight)" if pending else ""
        raise GuardError(
            ErrorCode.BUDGET_EXCEEDED,
            f"The daily budget of {self._limit} API calls would be exceeded: {spent} spent today{in_flight} "
            f"and this call costs {cost}. Raise EODHD_GUARD_DAILY_BUDGET to allow more "
            f"(0 removes the limit). The count restarts at {resets_at} (00:00 GMT).",
            details={
                "daily_limit": self._limit,
                "spent_today": spent,
                "pending": pending,
                "cost": cost,
                "resets_at": resets_at,
            },
        )

    def status(self) -> dict[str, Any]:
        """The limit (0 = none), what is spent today, what remains (None when unlimited) and the reset."""
        spent = self._ledger.spent_today()
        return {
            "daily_limit": self._limit,
            "spent_today": spent,
            "remaining": max(self._limit - spent, 0) if self._limit else None,
            "resets_at": iso_utc(next_gmt_reset(self._clock())),
        }
