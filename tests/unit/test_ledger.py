from __future__ import annotations

import logging
import sqlite3
import stat
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.ledger import RETENTION_DAYS, Budget, UsageLedger, next_gmt_reset
from tests.helpers import FixedClock

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "usage.sqlite"


@pytest.fixture
def ledger(db_path: Path, clock: FixedClock) -> Iterator[UsageLedger]:
    opened = UsageLedger(db_path, clock=clock)
    yield opened
    opened.close()


def add(
    ledger: UsageLedger,
    *,
    endpoint: str = "eod",
    cost: int = 1,
    status: int = 200,
    cached: bool = False,
    symbol: str | None = "ACME.US",
) -> None:
    ledger.record(
        endpoint=endpoint,
        cost=cost,
        status=status,
        cached=cached,
        duration_ms=12.5,
        bytes_=640,
        symbol=symbol,
    )


class TestOpen:
    def test_the_folder_is_created_private(self, ledger: UsageLedger, db_path: Path) -> None:
        assert db_path.exists()
        if sys.platform != "win32":
            assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    @posix_only
    def test_the_wal_files_are_private_too(self, ledger: UsageLedger, db_path: Path) -> None:
        add(ledger)
        siblings = [p for p in db_path.parent.iterdir() if p != db_path]
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in siblings)

    def test_the_table_has_the_documented_columns(self, ledger: UsageLedger, db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(calls)")]
        assert columns == [
            "id",
            "ts",
            "day",
            "endpoint",
            "cost",
            "status",
            "cached",
            "duration_ms",
            "bytes",
            "symbol",
        ]

    def test_reopening_keeps_the_rows(self, db_path: Path, clock: FixedClock) -> None:
        first = UsageLedger(db_path, clock=clock)
        add(first, cost=5)
        first.close()
        second = UsageLedger(db_path, clock=clock)
        assert second.spent_today() == 5
        second.close()

    def test_an_unusable_folder_is_a_config_error(self, tmp_path: Path, clock: FixedClock) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where a folder should be", encoding="utf-8")
        with pytest.raises(GuardError) as caught:
            UsageLedger(blocker / "usage.sqlite", clock=clock)
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert "EODHD_GUARD_STATE_DIR" in (caught.value.hint or "")

    def test_a_file_that_is_not_a_database_is_a_config_error(self, db_path: Path, clock: FixedClock) -> None:
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"this is not a sqlite file" * 100)
        with pytest.raises(GuardError, match="Cannot use the usage ledger") as caught:
            UsageLedger(db_path, clock=clock)
        assert caught.value.code is ErrorCode.CONFIG_ERROR

    def test_old_rows_are_pruned_when_it_opens(self, db_path: Path, clock: FixedClock) -> None:
        old = UsageLedger(db_path, clock=clock)
        for age_days in (RETENTION_DAYS + 10, RETENTION_DAYS + 1, RETENTION_DAYS, RETENTION_DAYS - 1, 0):
            clock_then = FixedClock(clock() - timedelta(days=age_days))
            writer = UsageLedger(db_path, clock=clock_then)
            add(writer, cost=age_days)
            writer.close()
        old.close()
        reopened = UsageLedger(db_path, clock=clock)
        with sqlite3.connect(db_path) as conn:
            kept = sorted(row[0] for row in conn.execute("SELECT cost FROM calls"))
        reopened.close()
        assert kept == [0, RETENTION_DAYS - 1, RETENTION_DAYS]


class TestRecord:
    def test_a_row_holds_what_was_recorded(
        self, ledger: UsageLedger, db_path: Path, clock: FixedClock
    ) -> None:
        add(ledger, endpoint="intraday", cost=5, status=200, cached=False, symbol="ACME.US")
        add(ledger, endpoint="eod", cost=0, status=404, cached=True, symbol=None)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT ts, day, endpoint, cost, status, cached, duration_ms, bytes, symbol"
                " FROM calls ORDER BY id"
            ).fetchall()
        assert rows == [
            (clock().timestamp(), "2026-11-03", "intraday", 5, 200, 0, 12.5, 640, "ACME.US"),
            (clock().timestamp(), "2026-11-03", "eod", 0, 404, 1, 12.5, 640, None),
        ]

    def test_the_day_is_the_gmt_day_whatever_the_clock_zone(self, db_path: Path) -> None:
        plus_five = timezone(timedelta(hours=5))
        zoned = FixedClock(datetime(2026, 11, 4, 2, 0, tzinfo=plus_five))
        writer = UsageLedger(db_path, clock=zoned)
        add(writer, cost=3)
        assert writer.summary()["today"]["day"] == "2026-11-03"
        assert writer.spent_today() == 3
        writer.close()

    def test_a_failed_write_is_logged_not_raised(
        self, db_path: Path, clock: FixedClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        closed = UsageLedger(db_path, clock=clock)
        closed.close()
        with caplog.at_level(logging.WARNING, logger="eodhd_guard_mcp.ledger"):
            add(closed)
        assert "write failed (ProgrammingError)" in caplog.text

    def test_it_is_safe_to_record_from_several_threads(self, ledger: UsageLedger) -> None:
        def worker() -> None:
            for _ in range(25):
                add(ledger, cost=2)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert ledger.spent_today() == 8 * 25 * 2


class TestSpentToday:
    def test_it_sums_the_cost_of_the_current_day_only(self, ledger: UsageLedger, clock: FixedClock) -> None:
        add(ledger, cost=10)
        add(ledger, cost=0, status=404)
        add(ledger, cost=0, cached=True)
        clock.advance(days=1)
        add(ledger, cost=4)
        assert ledger.spent_today() == 4
        clock.advance(days=-1)
        assert ledger.spent_today() == 10

    def test_an_empty_ledger_has_spent_nothing(self, ledger: UsageLedger) -> None:
        assert ledger.spent_today() == 0

    def test_the_count_restarts_at_midnight_gmt(self, ledger: UsageLedger, clock: FixedClock) -> None:
        clock.advance(hours=8, minutes=59)  # 23:59 GMT
        add(ledger, cost=7)
        assert ledger.spent_today() == 7
        clock.advance(minutes=1)  # 00:00 GMT the next day
        assert ledger.spent_today() == 0

    def test_a_closed_ledger_cannot_be_read(self, db_path: Path, clock: FixedClock) -> None:
        closed = UsageLedger(db_path, clock=clock)
        closed.close()
        with pytest.raises(GuardError) as caught:
            closed.spent_today()
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert "cannot be read" in caught.value.message


class TestSummary:
    def test_an_empty_ledger(self, ledger: UsageLedger) -> None:
        summary = ledger.summary()
        assert summary["today"] == {
            "day": "2026-11-03",
            "cost": 0,
            "requests": 0,
            "cache_hits": 0,
            "by_endpoint": {},
        }
        assert summary["days"] == []
        assert summary["session"] == {
            "since": "2026-11-03T15:00:00Z",
            "cost": 0,
            "requests": 0,
            "cache_hits": 0,
        }

    def test_today_by_endpoint(self, ledger: UsageLedger) -> None:
        add(ledger, endpoint="eod", cost=1)
        add(ledger, endpoint="eod", cost=1)
        add(ledger, endpoint="eod", cost=0, cached=True)
        add(ledger, endpoint="intraday", cost=5)
        add(ledger, endpoint="intraday", cost=0, status=429)
        today = ledger.summary()["today"]
        assert today["cost"] == 7
        assert today["requests"] == 4
        assert today["cache_hits"] == 1
        assert today["by_endpoint"] == {
            "eod": {"cost": 2, "requests": 2, "cache_hits": 1},
            "intraday": {"cost": 5, "requests": 2, "cache_hits": 0},
        }

    def test_days_are_listed_newest_first_and_only_when_they_have_rows(
        self, ledger: UsageLedger, clock: FixedClock
    ) -> None:
        add(ledger, cost=2)  # 2026-11-03
        clock.advance(days=-1)
        add(ledger, cost=3)
        add(ledger, cost=0, cached=True)  # 2026-11-02
        clock.advance(days=-3)
        add(ledger, cost=9)  # 2026-10-30
        clock.advance(days=4)
        assert ledger.summary(days=7)["days"] == [
            {"day": "2026-11-03", "cost": 2, "requests": 1, "cache_hits": 0},
            {"day": "2026-11-02", "cost": 3, "requests": 1, "cache_hits": 1},
            {"day": "2026-10-30", "cost": 9, "requests": 1, "cache_hits": 0},
        ]

    def test_the_window_counts_today_as_the_first_day(self, ledger: UsageLedger, clock: FixedClock) -> None:
        add(ledger)
        clock.advance(days=-1)
        add(ledger)
        clock.advance(days=1)
        assert [d["day"] for d in ledger.summary(days=1)["days"]] == ["2026-11-03"]
        assert [d["day"] for d in ledger.summary(days=2)["days"]] == ["2026-11-03", "2026-11-02"]

    def test_the_window_is_clamped(self, ledger: UsageLedger, clock: FixedClock) -> None:
        add(ledger)
        assert [d["day"] for d in ledger.summary(days=0)["days"]] == ["2026-11-03"]
        assert [d["day"] for d in ledger.summary(days=-5)["days"]] == ["2026-11-03"]
        assert len(ledger.summary(days=10**9)["days"]) == 1

    def test_the_session_starts_when_the_ledger_opens(self, db_path: Path, clock: FixedClock) -> None:
        earlier = UsageLedger(db_path, clock=FixedClock(clock() - timedelta(hours=2)))
        add(earlier, cost=4)
        earlier.close()
        current = UsageLedger(db_path, clock=clock)
        add(current, cost=1)
        add(current, cost=0, cached=True)
        summary = current.summary()
        current.close()
        assert summary["today"]["cost"] == 5
        assert summary["session"] == {
            "since": "2026-11-03T15:00:00Z",
            "cost": 1,
            "requests": 1,
            "cache_hits": 1,
        }

    def test_a_closed_ledger_cannot_be_summarised(self, db_path: Path, clock: FixedClock) -> None:
        closed = UsageLedger(db_path, clock=clock)
        closed.close()
        with pytest.raises(GuardError, match="cannot be read"):
            closed.summary()


def test_close_twice_is_harmless(ledger: UsageLedger) -> None:
    ledger.close()
    ledger.close()


def test_next_reset_is_midnight_gmt_of_the_following_day() -> None:
    plus_five = timezone(timedelta(hours=5))
    assert next_gmt_reset(datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)) == datetime(
        2026, 11, 4, tzinfo=timezone.utc
    )
    assert next_gmt_reset(datetime(2026, 11, 3, 23, 59, 59, tzinfo=timezone.utc)) == datetime(
        2026, 11, 4, tzinfo=timezone.utc
    )
    assert next_gmt_reset(datetime(2026, 11, 4, 2, 0, tzinfo=plus_five)) == datetime(
        2026, 11, 4, tzinfo=timezone.utc
    )


class TestBudget:
    def test_a_call_within_the_limit_passes(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=4)
        budget.check(6)

    def test_a_call_past_the_limit_is_refused(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=8)
        with pytest.raises(GuardError) as caught:
            budget.check(5)
        assert caught.value.code is ErrorCode.BUDGET_EXCEEDED

    def test_the_message_names_the_budget_the_spend_the_variable_and_the_reset(
        self, ledger: UsageLedger, clock: FixedClock
    ) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=8)
        with pytest.raises(GuardError) as caught:
            budget.check(5)
        message = caught.value.message
        assert "daily budget of 10 API calls" in message
        assert "8 spent today" in message
        assert "this call costs 5" in message
        assert "EODHD_GUARD_DAILY_BUDGET" in message
        assert "2026-11-04T00:00:00Z" in message
        assert str(caught.value).startswith("BUDGET_EXCEEDED: ")
        assert caught.value.details == {
            "daily_limit": 10,
            "spent_today": 8,
            "pending": 0,
            "cost": 5,
            "resets_at": "2026-11-04T00:00:00Z",
        }

    def test_the_limit_itself_can_be_spent(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=9)
        budget.check(1)
        add(ledger, cost=1)
        with pytest.raises(GuardError, match="BUDGET_EXCEEDED"):
            budget.check(1)

    def test_zero_means_no_limit(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 0, clock=clock)
        add(ledger, cost=10**6)
        budget.check(10**6)

    def test_a_free_call_is_always_allowed(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=50)
        budget.check(0)

    def test_calls_in_flight_count_against_the_limit(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=8)
        budget.check(1, pending=1)
        with pytest.raises(GuardError) as caught:
            budget.check(1, pending=2)
        assert "(plus 2 in flight)" in caught.value.message
        assert caught.value.details["pending"] == 2

    def test_the_budget_starts_again_after_midnight_gmt(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        clock.advance(hours=8, minutes=59)  # 23:59 GMT
        add(ledger, cost=10)
        with pytest.raises(GuardError, match="BUDGET_EXCEEDED"):
            budget.check(1)
        clock.advance(minutes=1)
        budget.check(10)

    def test_a_negative_limit_is_treated_as_none(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, -5, clock=clock)
        add(ledger, cost=100)
        budget.check(100)
        assert budget.status()["daily_limit"] == 0

    def test_status(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 100, clock=clock)
        add(ledger, cost=30)
        assert budget.status() == {
            "daily_limit": 100,
            "spent_today": 30,
            "remaining": 70,
            "resets_at": "2026-11-04T00:00:00Z",
        }

    def test_remaining_never_goes_negative(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 10, clock=clock)
        add(ledger, cost=25)
        assert budget.status()["remaining"] == 0

    def test_status_without_a_limit(self, ledger: UsageLedger, clock: FixedClock) -> None:
        budget = Budget(ledger, 0, clock=clock)
        add(ledger, cost=25)
        assert budget.status() == {
            "daily_limit": 0,
            "spent_today": 25,
            "remaining": None,
            "resets_at": "2026-11-04T00:00:00Z",
        }
