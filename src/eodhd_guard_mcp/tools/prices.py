"""Tools: get_eod_prices, get_intraday_prices. Placeholder until the tools phase lands."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eodhd_guard_mcp.server import Deps


def register(server: MCPServer, deps: Deps) -> None:
    """Register this module's tools. Nothing yet."""
