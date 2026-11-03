"""Settings read from the environment: the API key, the budget and the knobs around them."""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from eodhd_guard_mcp.errors import ErrorCode, GuardError
from eodhd_guard_mcp.redact import MASK, redact

DEFAULT_BASE_URL = "https://eodhd.com/api"
STATE_DIR_NAME = "eodhd-guard-mcp"

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_BOOL_ACCEPTED = "on or off (also 1/0, true/false, yes/no)"


def default_state_dir(env: Mapping[str, str], *, platform: str | None = None) -> Path:
    """Where the ledger and cache live when ``EODHD_GUARD_STATE_DIR`` is not set."""
    if (platform or sys.platform) == "win32":
        local = env.get("LOCALAPPDATA", "").strip()
        return (Path(local) if local else Path.home() / "AppData" / "Local") / STATE_DIR_NAME
    xdg = env.get("XDG_STATE_HOME", "").strip()
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / STATE_DIR_NAME
    return Path.home() / ".local" / "state" / STATE_DIR_NAME


def _to_int(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _to_float(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def _split_url(raw: str) -> SplitResult | None:
    try:
        return urlsplit(raw)
    except ValueError:
        return None


def _read_text(path: Path) -> tuple[str | None, str]:
    """``(reason, text)``. The reason is the OS message or the exception class, never file content.

    Errors are turned into values here so that no exception, which may hold the file's bytes, is
    chained to the ``GuardError`` the caller raises.
    """
    try:
        return None, path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return exc.strerror or type(exc).__name__, ""
    except UnicodeError as exc:
        return type(exc).__name__, ""


class _Env:
    """Typed reads from an environment mapping. Error messages never repeat the API key."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self._secrets: list[str] = []

    def hide(self, secret: str) -> None:
        self._secrets.append(secret)

    def text(self, name: str) -> str | None:
        """The stripped value, or None when the variable is unset or blank."""
        return (self._env.get(name) or "").strip() or None

    def reject(self, name: str, accepted: str, got: str | None = None) -> GuardError:
        message = f"{name} must be {accepted}"
        if got is not None:
            message += f"; got {redact(got, self._secrets)!r}"
        return GuardError(ErrorCode.CONFIG_ERROR, message + ".")

    def integer(self, name: str, default: int, *, lo: int, hi: int | None = None) -> int:
        raw = self.text(name)
        if raw is None:
            return default
        accepted = f"an integer from {lo} to {hi}" if hi is not None else f"an integer of {lo} or more"
        value = _to_int(raw)
        if value is None or value < lo or (hi is not None and value > hi):
            raise self.reject(name, accepted, raw)
        return value

    def seconds(self, name: str, default: float) -> float:
        raw = self.text(name)
        if raw is None:
            return default
        accepted = "a number of seconds greater than 0"
        value = _to_float(raw)
        if value is None or not math.isfinite(value) or value <= 0:
            raise self.reject(name, accepted, raw)
        return value

    def boolean(self, name: str, default: bool) -> bool:
        raw = self.text(name)
        if raw is None:
            return default
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise self.reject(name, _BOOL_ACCEPTED, raw)

    def log_level(self, name: str, default: str) -> str:
        raw = self.text(name)
        if raw is None:
            return default
        if raw.upper() not in _LOG_LEVELS:
            raise self.reject(name, "one of " + ", ".join(_LOG_LEVELS), raw)
        return raw.upper()

    def base_url(self, name: str, default: str) -> str:
        raw = self.text(name)
        if raw is None:
            return default
        accepted = (
            "an https URL such as https://eodhd.com/api (http is allowed only for localhost), "
            "without credentials, query or fragment"
        )
        parts = _split_url(raw)
        if parts is None:
            raise self.reject(name, accepted)
        host = parts.hostname
        secure = parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS)
        if not (secure and host) or parts.username or parts.password or parts.query or parts.fragment:
            raise self.reject(name, accepted)
        return raw.rstrip("/")

    def api_key(self) -> str | None:
        """``EODHD_API_KEY``, else the content of the file named by ``EODHD_API_KEY_FILE``."""
        key = self.text("EODHD_API_KEY")
        if key is None:
            file_name = self.text("EODHD_API_KEY_FILE")
            if file_name is not None:
                key = self._read_key_file(Path(file_name).expanduser())
        if key is not None:
            self.hide(key)
        return key

    @staticmethod
    def _read_key_file(path: Path) -> str:
        reason, text = _read_text(path)
        if reason is not None:
            raise GuardError(ErrorCode.CONFIG_ERROR, f"EODHD_API_KEY_FILE ({path}) cannot be read: {reason}.")
        content = text.strip()
        if not content:
            raise GuardError(ErrorCode.CONFIG_ERROR, f"EODHD_API_KEY_FILE ({path}) is empty.")
        return content


@dataclass(frozen=True)
class Settings:
    """Everything the server is configured with. ``repr`` masks the API key."""

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    state_dir: Path = field(default_factory=lambda: default_state_dir(os.environ))
    daily_budget: int = 1000
    """Cost units per GMT day; 0 means no limit."""
    cache_enabled: bool = True
    stale_sessions: int = 3
    """Sessions behind the expected latest one before data counts as stale."""
    timeout_s: float = 30.0
    max_retries: int = 2
    """Retries for 429, 5xx and network errors."""
    allow_uncovered: bool = False
    log_level: str = "WARNING"

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "api_key":
                value = MASK if value else None
            parts.append(f"{f.name}={value!r}")
        return f"Settings({', '.join(parts)})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read the settings from ``env`` (the process environment by default).

        Raises ``GuardError(CONFIG_ERROR)`` naming the variable when a value is invalid. A missing key
        is not an error here; ``require_key`` raises when a tool needs it.
        """
        source = os.environ if env is None else env
        reader = _Env(source)
        api_key = reader.api_key()

        state_text = reader.text("EODHD_GUARD_STATE_DIR")
        if state_text is not None:
            state_dir = Path(state_text).expanduser().absolute()
        else:
            try:
                state_dir = default_state_dir(source)
            except RuntimeError:
                raise GuardError(
                    ErrorCode.CONFIG_ERROR,
                    "No home directory was found for the ledger and cache.",
                    hint="Set EODHD_GUARD_STATE_DIR to a writable folder.",
                ) from None

        return cls(
            api_key=api_key,
            base_url=reader.base_url("EODHD_GUARD_BASE_URL", DEFAULT_BASE_URL),
            state_dir=state_dir,
            daily_budget=reader.integer("EODHD_GUARD_DAILY_BUDGET", 1000, lo=0),
            cache_enabled=reader.boolean("EODHD_GUARD_CACHE", True),
            stale_sessions=reader.integer("EODHD_GUARD_STALE_SESSIONS", 3, lo=1),
            timeout_s=reader.seconds("EODHD_GUARD_TIMEOUT", 30.0),
            max_retries=reader.integer("EODHD_GUARD_MAX_RETRIES", 2, lo=0, hi=5),
            allow_uncovered=reader.boolean("EODHD_GUARD_ALLOW_UNCOVERED", False),
            log_level=reader.log_level("EODHD_GUARD_LOG_LEVEL", "WARNING"),
        )

    def require_key(self) -> str:
        """The API key, or ``GuardError(CONFIG_ERROR)`` when none is configured."""
        if not self.api_key:
            raise GuardError(
                ErrorCode.CONFIG_ERROR,
                "No EODHD API key is configured.",
                hint=(
                    "Set EODHD_API_KEY, or EODHD_API_KEY_FILE to a file that holds the key, "
                    "in the environment of the MCP server."
                ),
            )
        return self.api_key
