"""Shared fixtures.

``server`` and ``mcp_client`` import ``build_server`` inside the fixture: it is written in the tools
phase, and the other fixtures must work without it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.redact import RedactingFilter
from eodhd_guard_mcp.server import Deps, build_deps
from tests.helpers import TEST_KEY, FixedClock, MockEODHD, RecordingSleep

if TYPE_CHECKING:
    from mcp import Client
    from mcp.server.mcpserver import MCPServer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> FixedClock:
    """Tuesday 2026-11-03 15:00 UTC."""
    return FixedClock(datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc))


@pytest.fixture
def mock_api() -> MockEODHD:
    return MockEODHD()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key=TEST_KEY,
        base_url=MockEODHD.base_url,
        state_dir=tmp_path / "state",
        daily_budget=1000,
    )


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    """Undo what ``install_log_redaction`` does to the process-wide loggers."""
    names = ("", "httpx2", "httpcore2", "mcp")
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    for name in names:
        target_logger = logging.getLogger(name)
        target_logger.setLevel(levels[name])
        for target in (target_logger, *target_logger.handlers):
            for added in [f for f in target.filters if isinstance(f, RedactingFilter)]:
                target.removeFilter(added)


@pytest.fixture
def sleeps() -> RecordingSleep:
    """Retry delays are recorded, not waited for."""
    return RecordingSleep()


@pytest.fixture
async def deps(
    settings: Settings, mock_api: MockEODHD, clock: FixedClock, sleeps: RecordingSleep
) -> AsyncIterator[Deps]:
    built = build_deps(settings, transport=mock_api.transport(), clock=clock, sleep=sleeps)
    yield built
    await built.aclose()


@pytest.fixture
def server(deps: Deps, isolated_logging: None) -> MCPServer:
    """``build_server`` installs log redaction process-wide; ``isolated_logging`` undoes it after."""
    from eodhd_guard_mcp.server import build_server

    return build_server(deps=deps)


@pytest.fixture
async def mcp_client(server: MCPServer) -> AsyncIterator[Client]:
    from mcp import Client

    async with Client(server) as client:
        yield client
