from __future__ import annotations

import hashlib
import logging
import sqlite3
import stat
import sys
import threading
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from eodhd_guard_mcp.cache import ResponseCache, key_fingerprint, normalize_params
from eodhd_guard_mcp.errors import ErrorCode, GuardError
from tests.helpers import TEST_KEY, FixedClock

BODY = b'[{"date": "2026-11-02", "close": 41.8}]'


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "cache.sqlite"


@pytest.fixture
def cache(db_path: Path, clock: FixedClock) -> Iterator[ResponseCache]:
    opened = ResponseCache(db_path, enabled=True, clock=clock)
    yield opened
    opened.close()


def store(
    cache: ResponseCache, key: str = "k", *, status: int = 200, body: bytes = BODY, ttl_s: float = 60
) -> None:
    cache.set(key, endpoint="eod", status=status, body=body, ttl_s=ttl_s)


class TestMakeKey:
    def test_it_is_a_sha256_hex_digest(self) -> None:
        key = ResponseCache.make_key("eod/ACME.US", {"from": "2026-10-01"})
        assert len(key) == 64
        assert int(key, 16) >= 0

    def test_it_is_deterministic(self) -> None:
        params = {"from": "2026-10-01", "to": "2026-11-01", "order": "a"}
        assert ResponseCache.make_key("eod/ACME.US", params) == ResponseCache.make_key("eod/ACME.US", params)

    def test_parameter_order_does_not_matter(self) -> None:
        a = ResponseCache.make_key("eod/ACME.US", {"from": "2026-10-01", "to": "2026-11-01"})
        b = ResponseCache.make_key("eod/ACME.US", {"to": "2026-11-01", "from": "2026-10-01"})
        assert a == b

    @pytest.mark.parametrize("name", ["api_token", "API_TOKEN", "Api_Token"])
    def test_the_api_token_is_ignored(self, name: str) -> None:
        plain = ResponseCache.make_key("eod/ACME.US", {"period": "d"})
        with_token = ResponseCache.make_key("eod/ACME.US", {"period": "d", name: TEST_KEY})
        assert with_token == plain

    def test_two_keys_give_the_same_cache_key(self) -> None:
        a = ResponseCache.make_key("eod/ACME.US", {"api_token": "first-key-0123456789"})
        b = ResponseCache.make_key("eod/ACME.US", {"api_token": "second-key-9876543210"})
        assert a == b

    def test_the_path_and_the_parameters_both_matter(self) -> None:
        base = ResponseCache.make_key("eod/ACME.US", {"period": "d"})
        assert ResponseCache.make_key("eod/GLOBEX.LSE", {"period": "d"}) != base
        assert ResponseCache.make_key("eod/ACME.US", {"period": "w"}) != base
        assert ResponseCache.make_key("eod/ACME.US", {"period": "d", "order": "a"}) != base
        assert ResponseCache.make_key("eod/ACME.US", {}) != base

    def test_surrounding_slashes_are_ignored(self) -> None:
        assert ResponseCache.make_key("/eod/ACME.US/", {}) == ResponseCache.make_key("eod/ACME.US", {})

    def test_values_that_are_not_strings_are_normalised(self) -> None:
        a = ResponseCache.make_key("p", {"n": 5, "flag": True, "s": ["A.US", "B.US"], "skip": None})
        b = ResponseCache.make_key("p", {"n": "5", "flag": "true", "s": "A.US,B.US"})
        assert a == b

    def test_a_separator_inside_a_value_does_not_collide(self) -> None:
        a = ResponseCache.make_key("p", {"a": "1&b=2"})
        b = ResponseCache.make_key("p", {"a": "1", "b": "2"})
        assert a != b


class TestKeyScope:
    def test_without_a_scope_the_key_is_the_same_as_with_an_empty_one(self) -> None:
        plain = ResponseCache.make_key("eod/ACME.US", {"period": "d"})
        assert ResponseCache.make_key("eod/ACME.US", {"period": "d"}, base_url="", fingerprint="") == plain

    def test_another_server_is_another_question(self) -> None:
        eodhd = ResponseCache.make_key("eod/ACME.US", {}, base_url="https://eodhd.com/api")
        proxy = ResponseCache.make_key("eod/ACME.US", {}, base_url="https://proxy.example/api")
        assert eodhd != proxy
        assert eodhd == ResponseCache.make_key("eod/ACME.US", {}, base_url="https://eodhd.com/api")

    def test_another_key_is_another_question(self) -> None:
        one = ResponseCache.make_key("eod/ACME.US", {}, fingerprint=key_fingerprint("first-key-0123456789"))
        two = ResponseCache.make_key("eod/ACME.US", {}, fingerprint=key_fingerprint("second-key-9876543210"))
        assert one != two

    def test_the_server_and_the_key_cannot_trade_places(self) -> None:
        a = ResponseCache.make_key("p", {}, base_url="aaa", fingerprint="bbb")
        b = ResponseCache.make_key("p", {}, base_url="bbb", fingerprint="aaa")
        assert a != b

    def test_the_server_cannot_be_confused_with_the_path(self) -> None:
        assert ResponseCache.make_key("b/c", {}, base_url="a") != ResponseCache.make_key(
            "c", {}, base_url="a/b"
        )

    def test_the_key_is_still_a_digest_that_holds_neither_the_server_nor_the_api_key(self) -> None:
        key = ResponseCache.make_key(
            "eod/ACME.US", {}, base_url="https://eodhd.test/api", fingerprint=key_fingerprint(TEST_KEY)
        )
        assert len(key) == 64
        assert TEST_KEY not in key
        assert key_fingerprint(TEST_KEY) not in key
        assert "eodhd" not in key


class TestKeyFingerprint:
    def test_it_is_the_first_twelve_hex_digits_of_the_sha256(self) -> None:
        assert (
            key_fingerprint("test-key-0123456789") == hashlib.sha256(b"test-key-0123456789").hexdigest()[:12]
        )
        assert len(key_fingerprint("a")) == 12

    def test_different_keys_give_different_fingerprints_and_none_shows_the_key(self) -> None:
        assert key_fingerprint("first-key-0123456789") != key_fingerprint("second-key-9876543210")
        assert key_fingerprint(TEST_KEY) == key_fingerprint(TEST_KEY)
        assert TEST_KEY not in key_fingerprint(TEST_KEY)


class TestNormalizeParams:
    def test_none_gives_an_empty_dict(self) -> None:
        assert normalize_params(None) == {}

    def test_values_become_strings_and_none_is_dropped(self) -> None:
        params = {"a": 1, "b": 2.5, "c": False, "d": ("x", "y"), "e": None, "api_token": TEST_KEY}
        assert normalize_params(params) == {"a": "1", "b": "2.5", "c": "false", "d": "x,y"}


class TestStore:
    def test_a_stored_answer_comes_back(self, cache: ResponseCache, clock: FixedClock) -> None:
        store(cache)
        hit = cache.get("k")
        assert hit is not None
        assert hit.status == 200
        assert hit.body == BODY
        assert hit.created == clock()

    def test_a_missing_key_is_a_miss(self, cache: ResponseCache) -> None:
        assert cache.get("absent") is None

    def test_storing_again_replaces_the_answer(self, cache: ResponseCache, clock: FixedClock) -> None:
        store(cache, body=b"[1]")
        clock.advance(seconds=30)
        store(cache, body=b"[2]")
        hit = cache.get("k")
        assert hit is not None
        assert hit.body == b"[2]"
        assert hit.created == clock()

    def test_an_answer_expires(self, cache: ResponseCache, clock: FixedClock) -> None:
        store(cache, ttl_s=60)
        clock.advance(seconds=59)
        assert cache.get("k") is not None
        clock.advance(seconds=1)
        assert cache.get("k") is None

    def test_an_expired_answer_is_deleted_when_it_is_read(
        self, cache: ResponseCache, clock: FixedClock
    ) -> None:
        store(cache, ttl_s=60)
        clock.advance(seconds=61)
        cache.get("k")
        assert cache.purge_expired() == 0

    def test_a_404_is_cached(self, cache: ResponseCache) -> None:
        store(cache, status=404, body=b"Ticker Not Found.")
        hit = cache.get("k")
        assert hit is not None
        assert (hit.status, hit.body) == (404, b"Ticker Not Found.")

    @pytest.mark.parametrize("status", [201, 301, 400, 401, 402, 403, 429, 500, 503])
    def test_other_statuses_are_never_cached(self, cache: ResponseCache, status: int) -> None:
        store(cache, status=status)
        assert cache.get("k") is None

    @pytest.mark.parametrize("ttl_s", [0, -1])
    def test_a_ttl_that_is_not_positive_stores_nothing(self, cache: ResponseCache, ttl_s: float) -> None:
        store(cache, ttl_s=ttl_s)
        assert cache.get("k") is None

    def test_an_empty_body_is_fine(self, cache: ResponseCache) -> None:
        store(cache, body=b"")
        hit = cache.get("k")
        assert hit is not None
        assert hit.body == b""

    def test_the_answer_survives_reopening(self, db_path: Path, clock: FixedClock) -> None:
        first = ResponseCache(db_path, enabled=True, clock=clock)
        store(first)
        first.close()
        second = ResponseCache(db_path, enabled=True, clock=clock)
        hit = second.get("k")
        second.close()
        assert hit is not None
        assert hit.body == BODY

    def test_the_body_is_compressed_on_disk(self, cache: ResponseCache, db_path: Path) -> None:
        big = b'{"x": "' + b"a" * 20_000 + b'"}'
        store(cache, body=big)
        with sqlite3.connect(db_path) as conn:
            (blob,) = conn.execute("SELECT body FROM responses").fetchone()
        assert len(blob) < 1_000
        assert zlib.decompress(blob) == big

    def test_a_blob_that_is_not_compressed_data_is_a_miss_and_is_removed(
        self, cache: ResponseCache, db_path: Path, clock: FixedClock
    ) -> None:
        now = clock().timestamp()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO responses (key, endpoint, created, expires, status, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("bad", "eod", now, now + 60, 200, b"not zlib"),
            )
        assert cache.get("bad") is None
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM responses").fetchone() == (0,)


class TestMaxAge:
    def test_an_answer_older_than_the_age_a_reader_accepts_is_a_miss(
        self, cache: ResponseCache, clock: FixedClock
    ) -> None:
        store(cache, ttl_s=6 * 3600)
        clock.advance(hours=2)
        assert cache.get("k", max_age_s=3600) is None

    def test_an_answer_exactly_as_old_as_the_age_is_still_a_hit(
        self, cache: ResponseCache, clock: FixedClock
    ) -> None:
        store(cache, ttl_s=6 * 3600)
        clock.advance(seconds=3600)
        assert cache.get("k", max_age_s=3600) is not None
        clock.advance(seconds=1)
        assert cache.get("k", max_age_s=3600) is None

    def test_a_miss_by_age_leaves_the_answer_for_readers_that_accept_older_ones(
        self, cache: ResponseCache, clock: FixedClock
    ) -> None:
        store(cache, ttl_s=6 * 3600)
        clock.advance(hours=2)
        assert cache.get("k", max_age_s=3600) is None
        kept = cache.get("k", max_age_s=6 * 3600)
        assert kept is not None
        assert kept.body == BODY
        assert cache.get("k") is not None

    def test_without_an_age_only_the_expiry_counts(self, cache: ResponseCache, clock: FixedClock) -> None:
        store(cache, ttl_s=6 * 3600)
        clock.advance(hours=5)
        assert cache.get("k", max_age_s=None) is not None

    def test_the_age_applies_to_a_stored_404_too(self, cache: ResponseCache, clock: FixedClock) -> None:
        store(cache, status=404, body=b"Ticker Not Found.", ttl_s=24 * 3600)
        clock.advance(minutes=10)
        assert cache.get("k", max_age_s=60) is None
        assert cache.get("k", max_age_s=3600) is not None


class TestEnabled:
    def test_it_is_enabled_while_open(self, cache: ResponseCache) -> None:
        assert cache.enabled is True

    def test_it_is_not_enabled_when_off_or_closed(
        self, db_path: Path, clock: FixedClock, cache: ResponseCache
    ) -> None:
        assert ResponseCache(db_path.parent / "other.sqlite", enabled=False, clock=clock).enabled is False
        cache.close()
        assert cache.enabled is False


class TestMaintenance:
    def test_purge_expired_counts_what_it_removes(self, cache: ResponseCache, clock: FixedClock) -> None:
        store(cache, "short-1", ttl_s=10)
        store(cache, "short-2", ttl_s=20)
        store(cache, "long", ttl_s=3600)
        clock.advance(seconds=30)
        assert cache.purge_expired() == 2
        assert cache.get("long") is not None
        assert cache.purge_expired() == 0

    def test_expired_rows_are_dropped_when_the_cache_opens(self, db_path: Path, clock: FixedClock) -> None:
        first = ResponseCache(db_path, enabled=True, clock=clock)
        store(first, "short", ttl_s=10)
        store(first, "long", ttl_s=3600)
        first.close()
        clock.advance(seconds=60)
        second = ResponseCache(db_path, enabled=True, clock=clock)
        second.close()
        with sqlite3.connect(db_path) as conn:
            keys = [row[0] for row in conn.execute("SELECT key FROM responses")]
        assert keys == ["long"]

    def test_a_write_deletes_expired_rows_once_an_hour_has_passed(
        self, cache: ResponseCache, db_path: Path, clock: FixedClock
    ) -> None:
        def stored_keys() -> list[str]:
            with sqlite3.connect(db_path) as conn:
                return sorted(row[0] for row in conn.execute("SELECT key FROM responses"))

        store(cache, "short", ttl_s=60)
        clock.advance(minutes=30)
        store(cache, "mid", ttl_s=60)
        assert stored_keys() == ["mid", "short"]
        clock.advance(minutes=31)
        store(cache, "new", ttl_s=60)
        assert stored_keys() == ["new"]

    def test_clear_removes_everything(self, cache: ResponseCache) -> None:
        store(cache, "a")
        store(cache, "b")
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_close_twice_is_harmless(self, cache: ResponseCache) -> None:
        cache.close()
        cache.close()

    def test_a_closed_cache_acts_as_a_cache_that_never_hits(
        self, db_path: Path, clock: FixedClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        closed = ResponseCache(db_path, enabled=True, clock=clock)
        store(closed)
        closed.close()
        with caplog.at_level(logging.WARNING, logger="eodhd_guard_mcp.cache"):
            assert closed.get("k") is None
            store(closed, "other")
            assert closed.purge_expired() == 0
        assert "continuing without it" not in caplog.text

    def test_a_disk_error_after_start_up_is_logged_and_treated_as_a_miss(
        self, cache: ResponseCache, db_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store(cache)
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE responses")
        with caplog.at_level(logging.WARNING, logger="eodhd_guard_mcp.cache"):
            assert cache.get("k") is None
            store(cache, "other")
        assert "OperationalError" in caplog.text
        assert "continuing without it" in caplog.text

    def test_it_is_safe_to_use_from_several_threads(self, cache: ResponseCache) -> None:
        def worker(n: int) -> None:
            for i in range(20):
                store(cache, f"{n}-{i}", body=str(i).encode())
                assert cache.get(f"{n}-{i}") is not None

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert cache.get("5-19") is not None


class TestDisabled:
    def test_it_never_hits_and_never_touches_the_disk(self, db_path: Path, clock: FixedClock) -> None:
        off = ResponseCache(db_path, enabled=False, clock=clock)
        store(off)
        assert off.get("k") is None
        assert off.purge_expired() == 0
        off.clear()
        off.close()
        assert not db_path.exists()
        assert not db_path.parent.exists()


class TestOpen:
    def test_the_folder_and_file_are_private(self, cache: ResponseCache, db_path: Path) -> None:
        store(cache)
        if sys.platform != "win32":
            assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
            for path in db_path.parent.iterdir():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_a_folder_and_database_that_were_open_to_others_are_closed_when_it_opens(
        self, cache: ResponseCache, db_path: Path, clock: FixedClock
    ) -> None:
        store(cache)
        db_path.parent.chmod(0o777)
        for path in db_path.parent.iterdir():
            path.chmod(0o666)
        second = ResponseCache(db_path, enabled=True, clock=clock)
        store(second, "again")
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in db_path.parent.iterdir()}
        assert set(modes) >= {"cache.sqlite", "cache.sqlite-wal", "cache.sqlite-shm"}
        assert set(modes.values()) == {0o600}
        second.close()

    def test_the_table_has_the_documented_columns(self, cache: ResponseCache, db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(responses)")]
        assert columns == ["key", "endpoint", "created", "expires", "status", "body"]

    def test_an_unusable_folder_is_a_config_error(self, tmp_path: Path, clock: FixedClock) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where a folder should be", encoding="utf-8")
        with pytest.raises(GuardError) as caught:
            ResponseCache(blocker / "cache.sqlite", enabled=True, clock=clock)
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert "EODHD_GUARD_CACHE=off" in (caught.value.hint or "")

    def test_a_file_that_is_not_a_database_is_a_config_error(self, db_path: Path, clock: FixedClock) -> None:
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"this is not a sqlite file" * 100)
        with pytest.raises(GuardError, match="Cannot use the response cache") as caught:
            ResponseCache(db_path, enabled=True, clock=clock)
        assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_the_database_never_holds_the_key(db_path: Path, clock: FixedClock) -> None:
    cache = ResponseCache(db_path, enabled=True, clock=clock)
    key = ResponseCache.make_key("eod/ACME.US", {"api_token": TEST_KEY, "from": "2026-10-01"})
    cache.set(key, endpoint="eod", status=200, body=BODY, ttl_s=60)
    cache.close()
    assert all(TEST_KEY.encode() not in path.read_bytes() for path in db_path.parent.iterdir())
