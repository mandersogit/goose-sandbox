"""Read-only access to the current Goose session's projected files."""

from __future__ import annotations

from sandboxed_goose.config import (
    APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    DIRECT_SESSION_CONTEXT_TRANSPORT,
    Settings,
)
from sandboxed_goose.contextfs.apptainer import render_projection_via_apptainer
from sandboxed_goose.contextfs.goose_session import (
    project_goose_session,
    render_projection_path,
)
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.tools.definition import ToolDefinition

SESSION_CONTEXT = ToolDefinition(
    name="session_context",
    description=(
        "List or read bounded, read-only files projected from this Goose session's "
        "current and preserved historically agent-visible messages. Paths are relative "
        "to /context."
    ),
)


def render_session_context(
    settings: Settings,
    session_id: str,
    path: str = "",
    offset: int = 0,
    limit: int = 64 * 1024,
) -> str:
    """Build a fresh generation and render one virtual path from it."""

    if settings.session_database is None:
        raise ProjectionError(
            "Goose session database is unavailable; set GOOSE_PATH_ROOT or "
            "SANDBOXED_GOOSE_SESSION_DATABASE"
        )
    projection = project_goose_session(settings.session_database, session_id)
    if settings.session_context_transport == DIRECT_SESSION_CONTEXT_TRANSPORT:
        return render_projection_path(projection, path, offset=offset, limit=limit)
    if settings.session_context_transport == APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT:
        return render_projection_via_apptainer(
            settings,
            projection,
            path,
            offset=offset,
            limit=limit,
        )
    raise ProjectionError(
        f"unsupported session context transport: {settings.session_context_transport!r}"
    )
