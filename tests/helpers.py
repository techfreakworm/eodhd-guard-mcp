"""Test doubles shared by the unit, tool and integration tests.

``MockEODHD`` stands in for eodhd.com through ``httpx2.MockTransport``, so no test touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

TEST_KEY = "test-key-0123456789"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load ``tests/fixtures/<name>.json``. Fixtures are synthetic; see the fixture policy in DESIGN.md."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


class FixedClock:
    """A clock that only moves when told to."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        """Move forward by ``timedelta(**kwargs)``, for example ``advance(hours=10)``."""
        self._now += timedelta(**kwargs)


class RecordingSleep:
    """Stands in for ``asyncio.sleep``: remembers each delay and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass(frozen=True)
class RecordedRequest:
    path: str
    """The path below ``/api/``, decoded: ``eod/ACME.US``."""
    params: dict[str, str]
    """Query parameters except ``api_token``."""
    raw_path: str
    """The path and query exactly as sent, still percent-encoded."""
    api_token: str | None = field(default=None, repr=False)


@dataclass
class _Route:
    status: int
    content: bytes
    content_type: str
    headers: dict[str, str]
    times: int | None
    raises: BaseException | None
    calls: int = 0

    @property
    def exhausted(self) -> bool:
        return self.times is not None and self.calls >= self.times


class MockEODHD:
    """A fake EODHD API. Register routes with ``add``, hand ``transport()`` to the client.

    Several routes may share a path: the first one with uses left answers, so
    ``add(p, status=429, times=2)`` followed by ``add(p, body)`` gives two 429s and then the body. A
    request to a path with no route left raises ``AssertionError``, which the client does not catch.
    """

    base_url = "https://eodhd.test/api"

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._routes: dict[str, list[_Route]] = {}

    def add(
        self,
        path: str,
        body: Any = None,
        *,
        status: int = 200,
        text: str | None = None,
        times: int | None = None,
        headers: Mapping[str, str] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        """Answer ``path`` (for example ``eod/ACME.US``) with a JSON ``body`` or raw ``text``.

        ``raises`` makes the route fail the way a broken network does, with an ``httpx2`` exception.
        """
        if body is not None and text is not None:
            raise ValueError("give a JSON body or text, not both")
        if text is not None:
            content, content_type = text.encode(), "text/html; charset=utf-8"
        elif body is not None:
            content, content_type = json.dumps(body).encode(), "application/json"
        else:
            content, content_type = b"", "text/plain"
        route = _Route(status, content, content_type, dict(headers or {}), times, raises)
        self._routes.setdefault(path.strip("/"), []).append(route)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def count(self, path_prefix: str = "") -> int:
        """Requests seen so far whose path starts with ``path_prefix``."""
        return sum(1 for request in self.requests if request.path.startswith(path_prefix))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        prefix = "/api/"
        if not request.url.path.startswith(prefix):
            raise AssertionError(f"MockEODHD: request outside {prefix}: {request.url.path}")
        path = request.url.path[len(prefix) :]
        params = dict(request.url.params.items())
        self.requests.append(
            RecordedRequest(
                path=path,
                params={name: value for name, value in params.items() if name != "api_token"},
                raw_path=request.url.raw_path.decode("ascii"),
                api_token=params.get("api_token"),
            )
        )
        routes = self._routes.get(path, [])
        route = next((r for r in routes if not r.exhausted), None)
        if route is None:
            state = "has no uses left" if routes else "is not mocked"
            known = ", ".join(sorted(self._routes)) or "none"
            raise AssertionError(f"MockEODHD: path {path!r} {state} (registered: {known})")
        route.calls += 1
        if route.raises is not None:
            raise route.raises
        headers = {"content-type": route.content_type, **route.headers}
        return httpx2.Response(route.status, content=route.content, headers=headers)
