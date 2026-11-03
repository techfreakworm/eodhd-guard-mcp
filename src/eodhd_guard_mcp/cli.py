"""Command line entry point: ``eodhd-guard-mcp``.

stdout carries the MCP protocol on stdio, so everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any

from eodhd_guard_mcp import __version__
from eodhd_guard_mcp.config import Settings
from eodhd_guard_mcp.errors import GuardError
from eodhd_guard_mcp.redact import install_log_redaction, redact

logger = logging.getLogger("eodhd_guard_mcp")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eodhd-guard-mcp",
        description="A guard-first MCP server for the EODHD market-data API.",
        epilog="Configuration comes from environment variables; see the README. The API key is read "
        "from EODHD_API_KEY or EODHD_API_KEY_FILE.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="streamable-http bind address (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=8000, help="streamable-http port (default: 8000)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the configuration (the key only as set or not set) and exit",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="with --check, also call EODHD's free account endpoint to test the key",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def configure_logging(settings: Settings) -> None:
    """Send logs to stderr at the configured level, with the key redacted everywhere."""
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_log_redaction(settings.api_key)


def run_check(settings: Settings, *, online: bool) -> int:
    """Print what the server would run with. Returns the process exit code."""
    report: dict[str, Any] = {
        "version": __version__,
        "api_key": "set" if settings.api_key else "NOT SET",
        "base_url": settings.base_url,
        "state_dir": str(settings.state_dir),
        "daily_budget": settings.daily_budget or "unlimited",
        "cache": "on" if settings.cache_enabled else "off",
        "stale_sessions": settings.stale_sessions,
        "allow_uncovered": settings.allow_uncovered,
    }
    status = 0 if settings.api_key else 1
    if online and settings.api_key:
        try:
            report["account"] = asyncio.run(_account_counters(settings))
        except GuardError as exc:
            report["account_error"] = redact(str(exc), [settings.api_key])
            status = 1
    print(json.dumps(report, indent=2), file=sys.stderr)
    return status


async def _account_counters(settings: Settings) -> dict[str, Any]:
    from eodhd_guard_mcp.server import build_deps

    deps = build_deps(settings)
    try:
        return await deps.client.get_account()
    finally:
        await deps.client.aclose()
        deps.ledger.close()
        deps.cache.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except GuardError as exc:
        print(f"eodhd-guard-mcp: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings)
    if args.check:
        return run_check(settings, online=args.online)
    if not settings.api_key:
        logger.warning("EODHD_API_KEY is not set: tools will be listed, but every call will fail.")

    from eodhd_guard_mcp.server import build_deps, build_server

    deps = build_deps(settings)
    server = build_server(deps=deps)
    try:
        _serve(server, args)
    finally:
        deps.cache.close()
        deps.ledger.close()
    return 0


def _serve(server: Any, args: argparse.Namespace) -> None:
    if args.transport == "streamable-http":
        if args.host not in LOCAL_HOSTS:
            logger.warning(
                "Binding to %s exposes this server beyond this machine. It has no authentication, "
                "and anyone who can reach it can spend your EODHD quota.",
                args.host,
            )
        else:
            logger.warning("streamable-http has no authentication; keep it on localhost.")
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")
