"""Server adapter implemented with the official MCP Python SDK."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from sandboxed_goose import __version__
from sandboxed_goose.config import Settings
from sandboxed_goose.contextfs.view_store import SessionViewStore
from sandboxed_goose.contract import SERVER_INSTRUCTIONS, SERVER_NAME
from sandboxed_goose.session_binding import resolve_session_id
from sandboxed_goose.tools import (
    CALCULATE,
    SANDBOX_STATUS,
    SESSION_CONTEXT,
    render_calculation,
    render_sandbox_status,
    render_session_context,
)


def build_server(settings: Settings | None = None) -> MCPServer[dict[str, Any]]:
    """Build the fail-closed server using the official MCP SDK."""
    active_settings = settings if settings is not None else Settings.from_environment()
    view_store = SessionViewStore()
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

    @server.tool(
        name=SESSION_CONTEXT.name,
        description=SESSION_CONTEXT.description,
    )
    async def session_context(
        ctx: Context[dict[str, Any], Any],
        path: str = "",
        offset: int = 0,
        limit: int = 64 * 1024,
        tail: bool = False,
        view_id: str = "",
    ) -> str:
        params = ctx.request_context.params
        raw_meta = params.get("_meta") if isinstance(params, Mapping) else None
        meta = (
            {key: value for key, value in raw_meta.items() if isinstance(key, str)}
            if isinstance(raw_meta, Mapping)
            else None
        )
        session_id = resolve_session_id(meta)
        return render_session_context(
            active_settings,
            session_id,
            path,
            offset,
            limit,
            tail,
            view_id or None,
            view_store,
        )

    return server
