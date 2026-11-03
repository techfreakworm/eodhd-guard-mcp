"""A small on-disk cache of EODHD answers, so repeating a question costs nothing.

The cache key is a hash of the server, a fingerprint of the API key, the request path and its parameters
without the key, and the stored body is the response as EODHD sent it. The API key is never written here:
the fingerprint is a few digits of its SHA-256, and it only goes into the hash that makes the cache key.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.ledger import make_private

logger = logging.getLogger(__name__)

CACHEABLE_STATUSES = frozenset({200, 404})
PURGE_INTERVAL_S = 3600.0
"""How often a write also deletes expired rows, so a long-running server does not collect them."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    status INTEGER NOT NULL,
    body BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS responses_expires ON responses (expires);
"""


def _connect(path: Path) -> sqlite3.Connection:
    """Open the database with a private folder and file, in autocommit mode and WAL where available."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        make_private(path)  # before SQLite makes its own files, which copy the mode of the database
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    except (OSError, sqlite3.Error) as exc:
        raise GuardError(
            ErrorCode.CONFIG_ERROR,
            f"Cannot open the response cache at {path}: {type(exc).__name__}.",
            hint="Check that EODHD_GUARD_STATE_DIR is a writable folder, or set EODHD_GUARD_CACHE=off.",
        ) from None
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    make_private(path)  # write-ahead files left by an earlier run keep the mode they had
    return conn


def key_fingerprint(secret: str) -> str:
    """The first 12 hex digits of the SHA-256 of ``secret``, to tell two API keys apart in a cache key.

    A one-way hash of a long random token, kept short: it identifies a key among a few, and gives no way
    back to it. It is stored only inside the hashed cache key.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify(item) for item in value)
    return str(value)


def normalize_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Query parameters as strings, without ``None`` values and without ``api_token``.

    The client sends exactly what this returns (plus its own ``fmt`` and key), so the cache key and the
    request can not disagree. Lists and tuples become comma-separated strings and booleans become
    ``true`` or ``false``.
    """
    return {
        str(name): _stringify(value)
        for name, value in (params or {}).items()
        if value is not None and str(name).lower() != "api_token"
    }


@dataclass(frozen=True)
class CachedResponse:
    status: int
    body: bytes
    created: datetime
    """When the answer was stored: the moment it was fetched from EODHD."""


class ResponseCache:
    """Responses in ``cache.sqlite``, compressed, with an expiry time each.

    When ``enabled`` is false nothing is read or written and no file is created. Disk errors after
    start-up are logged and treated as a miss, because the cache is only an optimisation.
    """

    def __init__(self, path: Path, *, enabled: bool, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._last_purge = clock().timestamp()
        if not enabled:
            return
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM responses WHERE expires <= ?", (clock().timestamp(),))
        except sqlite3.Error as exc:
            conn.close()
            raise GuardError(
                ErrorCode.CONFIG_ERROR,
                f"Cannot use the response cache at {path}: {type(exc).__name__}.",
                hint="Delete that file, or set EODHD_GUARD_CACHE=off.",
            ) from None
        self._conn = conn

    @property
    def enabled(self) -> bool:
        """True while the cache reads and writes; false when it was switched off or has been closed."""
        return self._conn is not None

    @staticmethod
    def make_key(path: str, params: Mapping[str, Any], *, base_url: str = "", fingerprint: str = "") -> str:
        """SHA-256 of the server, the key fingerprint, the path and the sorted parameters.

        ``api_token`` and the parameter order are ignored. ``base_url`` and ``fingerprint`` (from
        ``key_fingerprint``) keep answers apart when the server (a proxy, say) or the API key changes, so
        an answer fetched for one is not served to the other.
        """
        pairs = sorted(normalize_params(params).items())
        payload = json.dumps([base_url, fingerprint, path.strip("/"), pairs], separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[list[tuple[Any, ...]], int] | None:
        """Run one statement; return its rows and row count, or None when the cache is off or fails."""
        try:
            with self._lock:
                if self._conn is None:
                    return None
                cursor = self._conn.execute(sql, params)
                return cursor.fetchall(), cursor.rowcount
        except sqlite3.Error as exc:
            logger.warning("Response cache operation failed (%s); continuing without it.", type(exc).__name__)
            return None

    def get(self, key: str, *, max_age_s: float | None = None) -> CachedResponse | None:
        """The stored answer, or None when it is missing, expired, unreadable or the cache is off.

        An answer is stored for the lifetime its writer chose, and a reader may want fresher data than
        that: with ``max_age_s``, an answer stored more than that many seconds ago is a miss for this
        reader only. It stays in the cache for readers that accept older ones.
        """
        result = self._execute("SELECT status, body, created, expires FROM responses WHERE key = ?", (key,))
        if result is None or not result[0]:
            return None
        status, blob, created, expires = result[0][0]
        now = self._clock().timestamp()
        body: bytes | None = None
        if expires > now:
            with contextlib.suppress(zlib.error):
                body = zlib.decompress(blob)
        if body is None:
            self._execute("DELETE FROM responses WHERE key = ?", (key,))
            return None
        if max_age_s is not None and now - created > max_age_s:
            return None
        return CachedResponse(status=status, body=body, created=datetime.fromtimestamp(created, timezone.utc))

    def set(self, key: str, *, endpoint: str, status: int, body: bytes, ttl_s: float) -> None:
        """Store an answer for ``ttl_s`` seconds. Only 200 and 404 answers are kept."""
        if self._conn is None or status not in CACHEABLE_STATUSES or ttl_s <= 0:
            return
        created = self._clock().timestamp()
        self._execute(
            "INSERT OR REPLACE INTO responses (key, endpoint, created, expires, status, body)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (key, endpoint, created, created + ttl_s, status, zlib.compress(body, 3)),
        )
        if created - self._last_purge >= PURGE_INTERVAL_S:
            self._last_purge = created
            self.purge_expired()

    def purge_expired(self) -> int:
        """Delete expired rows and return how many there were."""
        result = self._execute("DELETE FROM responses WHERE expires <= ?", (self._clock().timestamp(),))
        return max(result[1], 0) if result else 0

    def clear(self) -> None:
        self._execute("DELETE FROM responses")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
