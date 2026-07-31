"""The fail-closed status tool shared by both MCP adapters."""

from __future__ import annotations

import json

from sandboxed_goose.config import Settings
from sandboxed_goose.tools.definition import ToolDefinition

_SCAFFOLD_REASON = (
    "Execution tools are disabled until a sandbox backend and policy are implemented."
)


SANDBOX_STATUS = ToolDefinition(
    name="sandbox_status",
    description="Report sandbox configuration without reading files or running commands.",
)


def render_sandbox_status(settings: Settings) -> str:
    """Render the status payload returned by both MCP implementations."""
    return json.dumps(
        {
            "execution_enabled": False,
            "reason": _SCAFFOLD_REASON,
            "requested_backend": settings.requested_backend,
            "workspace": str(settings.workspace) if settings.workspace is not None else None,
        },
        sort_keys=True,
    )
