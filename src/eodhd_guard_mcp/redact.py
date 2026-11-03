"""Keep the API key out of messages and logs, and cap the text that comes from the data vendor.

EODHD takes the key as the ``api_token`` query parameter, so every request URL carries it, and
``httpx2`` logs request URLs at INFO. Everything that can reach the model or a log passes through here.
``clip`` is the other half: a name, a holder or a currency in an answer is the vendor's text, and it is
cut to a set length so that it cannot grow into a message of its own.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable
from functools import lru_cache
from urllib.parse import quote, quote_plus

MASK = "***"
MIN_SECRET_LENGTH = 6
"""Shorter secrets are ignored: replacing a 3-letter string would garble ordinary text."""
TEXT_LIMIT = 200
"""The longest free-text value from the vendor (a name, a holder, a currency) that is passed on whole."""
_ELLIPSIS = "..."

_KEY_PARAMS = "api_token|apikey|api_key|token"
_URL_PARAM = re.compile(rf"(?<![\w-])((?:{_KEY_PARAMS})=)[^&\s\"'<>,;)\]}}]+", re.IGNORECASE)
_QUOTED_PARAM = re.compile(rf"""(["'](?:{_KEY_PARAMS})["']\s*:\s*)(["']).*?\2""", re.IGNORECASE)

_QUIET_LOGGERS = ("httpx2", "httpcore2")
_FILTERED_LOGGERS = ("httpx2", "httpcore2", "mcp")


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    """``text`` cut to at most ``limit`` characters, ending in ``...`` when something was cut off."""
    if len(text) <= limit:
        return text
    return text[: max(limit - len(_ELLIPSIS), 0)].rstrip() + _ELLIPSIS


def _needles(secrets: Iterable[str | None]) -> tuple[str, ...]:
    """Every spelling of every usable secret, longest first so a longer one is never cut by a shorter."""
    found: set[str] = set()
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            found.update((secret, quote(secret, safe=""), quote_plus(secret)))
    return tuple(sorted(found, key=lambda needle: (-len(needle), needle)))


@lru_cache(maxsize=64)
def _needle_pattern(needles: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(needle) for needle in needles))


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Mask each secret, and the value of any ``api_token``-style parameter, in ``text``.

    Secrets under ``MIN_SECRET_LENGTH`` characters are ignored. The percent-encoded forms of a secret are
    masked too, because a URL may carry it that way.
    """
    needles = _needles(secrets)
    if needles:
        text = _needle_pattern(needles).sub(MASK, text)
    text = _URL_PARAM.sub(lambda match: f"{match.group(1)}{MASK}", text)
    return _QUOTED_PARAM.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}{match.group(2)}", text)


class RedactingFilter(logging.Filter):
    """Scrub secrets from a log record before any handler formats it.

    The message is rendered here, so arguments that are not strings (a ``URL`` object in an ``httpx2``
    INFO line) are masked too. A traceback is rendered and masked as well, since the text of a network
    error carries the request URL, and ``exc_info`` is cleared so that no handler can render it again.
    """

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            self._secrets.add(secret)

    def _scrub(self, text: str) -> str:
        return redact(text, self._secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not bring the raw arguments to stderr
            message = str(record.msg)
        record.msg = self._scrub(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        # Rich's handler builds its traceback from exc_info and ignores exc_text, which would print the
        # exception as it is. Without exc_info every handler falls back on the masked exc_text.
        record.exc_info = None
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        return True


def install_log_redaction(secret: str | None) -> None:
    """Redact ``secret`` in the root handlers and in the ``httpx2``, ``httpcore2`` and ``mcp`` loggers.

    Call it after the handlers are configured. It is safe to call twice: a target that already has a
    ``RedactingFilter`` just learns the new secret. ``httpx2`` and ``httpcore2`` are set to WARNING so
    request URLs are not logged at all.
    """
    targets: list[logging.Filterer] = list(logging.getLogger().handlers)
    for name in _FILTERED_LOGGERS:
        named = logging.getLogger(name)
        targets.append(named)
        targets.extend(named.handlers)
    shared = RedactingFilter([secret])
    for target in targets:
        existing = next((f for f in target.filters if isinstance(f, RedactingFilter)), None)
        if existing is None:
            target.addFilter(shared)
        else:
            existing.add_secret(secret)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
