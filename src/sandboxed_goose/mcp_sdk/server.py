"""Server adapter implemented with the official MCP Python SDK."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from sandboxed_goose import __version__
from sandboxed_goose.config import Settings
from sandboxed_goose.contract import SERVER_INSTRUCTIONS, SERVER_NAME
from sandboxed_goose.tools import (
    CALCULATE,
    SANDBOX_STATUS,
    render_calculation,
    render_sandbox_status,
)


def build_server(settings: Settings | None = None) -> MCPServer[dict[str, Any]]:
    """Build the fail-closed server using the official MCP SDK."""
    active_settings = settings if settings is not None else Settings.from_environment()
    server: MCPServer[dict[str, Any]] = MCPServer(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(
        name=SANDBOX_STATUS.name,
        description=SANDBOX_STATUS.description,
    )
    async def sandbox_status() -> str:
        return render_sandbox_status(active_settings)

    @server.tool(
        name=CALCULATE.name,
        description=CALCULATE.description,
    )
    async def calculate(expression: str) -> str:
        return render_calculation(expression)

    return server


mcp = build_server()
