"""The command line: --version, --check with and without the network, and how it starts the server."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from eodhd_guard_mcp import __version__, cli
from eodhd_guard_mcp import server as server_module
from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.server import Deps
from tests.helpers import TEST_KEY, MockEODHD

ENV_NAMES = (
    "EODHD_API_KEY",
    "EODHD_API_KEY_FILE",
    "EODHD_GUARD_DAILY_BUDGET",
    "EODHD_GUARD_CACHE",
    "EODHD_GUARD_STALE_SESSIONS",
    "EODHD_GUARD_TIMEOUT",
    "EODHD_GUARD_MAX_RETRIES",
    "EODHD_GUARD_ALLOW_UNCOVERED",
    "EODHD_GUARD_BASE_URL",
    "EODHD_GUARD_LOG_LEVEL",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_logging: None) -> pytest.MonkeyPatch:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EODHD_GUARD_STATE_DIR", str(tmp_path / "state"))
    return monkeypatch


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record MCPServer.run instead of serving."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(self: MCPServer, transport: str = "stdio", **kwargs: Any) -> None:
        calls.append((transport, kwargs))

    monkeypatch.setattr(MCPServer, "run", fake_run)
    return calls


def with_mock(monkeypatch: pytest.MonkeyPatch, mock: MockEODHD) -> None:
    real: Callable[..., Deps] = server_module.build_deps

    def build(settings: Settings, **kwargs: Any) -> Deps:
        return real(settings, transport=mock.transport())

    monkeypatch.setattr(server_module, "build_deps", build)


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_check_without_a_key_fails_and_says_so(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--check"]) == 1
    report = json.loads(capsys.readouterr().err)
    assert report["api_key"] == "NOT SET"
    assert report["daily_budget"] == 1000


def test_check_with_a_key_shows_only_that_it_is_set(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.setenv("EODHD_API_KEY", TEST_KEY)
    env.setenv("EODHD_GUARD_DAILY_BUDGET", "0")
    assert cli.main(["--check"]) == 0
    err = capsys.readouterr().err
    assert TEST_KEY not in err
    report = json.loads(err)
    assert report["api_key"] == "set"
    assert report["daily_budget"] == "unlimited"


def test_a_bad_setting_stops_the_cli_and_names_the_variable(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.setenv("EODHD_GUARD_DAILY_BUDGET", "lots")
    assert cli.main(["--check"]) == 2
    assert "EODHD_GUARD_DAILY_BUDGET" in capsys.readouterr().err


def test_check_online_prints_the_counters_and_nothing_personal(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.setenv("EODHD_API_KEY", TEST_KEY)
    env.setenv("EODHD_GUARD_BASE_URL", MockEODHD.base_url)
    mock = MockEODHD()
    mock.add(
        "user",
        {
            "name": "Jane Example",
            "email": "jane@example.com",
            "paymentMethod": "Card",
            "inviteToken": "invite-0000",
            "apiRequests": 3,
            "apiRequestsDate": "2026-11-03",
            "dailyRateLimit": 100000,
            "extraLimit": 0,
            "subscriptionType": "monthly",
            "subscriptionMode": "paid",
        },
    )
    with_mock(monkeypatch, mock)
    assert cli.main(["--check", "--online"]) == 0
    err = capsys.readouterr().err
    assert json.loads(err)["account"]["apiRequests"] == 3
    for private in ("Jane Example", "jane@example.com", "invite-0000", "Card", TEST_KEY):
        assert private not in err


def test_check_online_reports_a_rejected_key(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.setenv("EODHD_API_KEY", TEST_KEY)
    env.setenv("EODHD_GUARD_BASE_URL", MockEODHD.base_url)
    mock = MockEODHD()
    mock.add("user", status=401, text="Unauthenticated")
    with_mock(monkeypatch, mock)
    assert cli.main(["--check", "--online"]) == 1
    err = capsys.readouterr().err
    assert "AUTH_ERROR" in json.loads(err)["account_error"]
    assert TEST_KEY not in err


def test_the_server_runs_on_stdio_by_default(env: pytest.MonkeyPatch, runs: list[Any]) -> None:
    env.setenv("EODHD_API_KEY", TEST_KEY)
    assert cli.main([]) == 0
    assert runs == [("stdio", {})]


def test_a_missing_key_still_starts_the_server_with_a_warning(
    env: pytest.MonkeyPatch, runs: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="eodhd_guard_mcp"):
        assert cli.main([]) == 0
    assert runs == [("stdio", {})]
    assert "EODHD_API_KEY is not set" in caplog.text


def test_streamable_http_on_localhost_warns_that_it_has_no_authentication(
    env: pytest.MonkeyPatch, runs: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    env.setenv("EODHD_API_KEY", TEST_KEY)
    with caplog.at_level(logging.WARNING, logger="eodhd_guard_mcp"):
        assert cli.main(["--transport", "streamable-http", "--port", "9001"]) == 0
    assert runs == [("streamable-http", {"host": "127.0.0.1", "port": 9001})]
    assert "no authentication" in caplog.text


def test_streamable_http_on_a_public_address_warns_about_the_quota(
    env: pytest.MonkeyPatch, runs: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    env.setenv("EODHD_API_KEY", TEST_KEY)
    with caplog.at_level(logging.WARNING, logger="eodhd_guard_mcp"):
        assert cli.main(["--transport", "streamable-http", "--host", "0.0.0.0"]) == 0  # noqa: S104
    assert runs[0][1]["host"] == "0.0.0.0"  # noqa: S104
    assert "spend your EODHD quota" in caplog.text
