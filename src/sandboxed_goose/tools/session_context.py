"""Read-only access to the current Goose session's projected files."""

from __future__ import annotations

from sandboxed_goose.config import (
    APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    DIRECT_SESSION_CONTEXT_TRANSPORT,
    Settings,
)
from sandboxed_goose.contextfs.apptainer import render_projection_via_apptainer
from sandboxed_goose.contextfs.goose_session import (
    normalize_requested_path,
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
        "to /context. Set tail=true to read the final limit bytes of a file without "
        "calculating an offset; tail cannot be combined with a nonzero offset."
    ),
)


def render_session_context(
    settings: Settings,
    session_id: str,
    path: str = "",
    offset: int = 0,
    limit: int = 64 * 1024,
    tail: bool = False,
) -> str:
    """Build a fresh generation and render one virtual path from it."""

    if settings.session_database is None:
        raise ProjectionError(
            "Goose session database is unavailable; set GOOSE_PATH_ROOT or "
            "SANDBOXED_GOOSE_SESSION_DATABASE"
        )
    projection = project_goose_session(settings.session_database, session_id)
    resolved_offset = offset
    if tail:
        if offset != 0:
            raise ProjectionError("tail reads cannot be combined with a nonzero offset")
        if not 1 <= limit <= 64 * 1024:
            raise ProjectionError("limit must be between 1 and 65536 bytes")
        normalized_path = normalize_requested_path(path)
        content = projection.files.get(normalized_path)
        if content is None:
            raise ProjectionError("tail reads require a projected file path")
        resolved_offset = max(len(content) - limit, 0)
        while (
            resolved_offset < len(content) and content[resolved_offset] & 0b1100_0000 == 0b1000_0000
        ):
            resolved_offset += 1
    if settings.session_context_transport == DIRECT_SESSION_CONTEXT_TRANSPORT:
        return render_projection_path(projection, path, offset=resolved_offset, limit=limit)
    if settings.session_context_transport == APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT:
        return render_projection_via_apptainer(
            settings,
            projection,
            path,
            offset=resolved_offset,
            limit=limit,
        )
    raise ProjectionError(
        f"unsupported session context transport: {settings.session_context_transport!r}"
    )
