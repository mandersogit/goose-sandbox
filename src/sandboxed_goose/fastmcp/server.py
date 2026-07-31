"""Server adapter implemented with the standalone FastMCP framework."""

from __future__ import annotations

from collections.abc import Mapping

from fastmcp import Context, FastMCP

from sandboxed_goose import __version__
from sandboxed_goose.config import Settings
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


def build_server(settings: Settings | None = None) -> FastMCP[None]:
    """Build the fail-closed server using standalone FastMCP."""
    active_settings = settings if settings is not None else Settings.from_environment()
    server = FastMCP[None](
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
        ctx: Context,
        path: str = "",
        offset: int = 0,
        limit: int = 64 * 1024,
        tail: bool = False,
    ) -> str:
        request_context = ctx.request_context
        raw_meta = request_context.meta if request_context is not None else None
        meta: Mapping[str, object] | None = raw_meta
        session_id = resolve_session_id(meta)
        return render_session_context(active_settings, session_id, path, offset, limit, tail)

    return server


mcp = build_server()
