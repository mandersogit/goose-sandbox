"""Configuration shared by the MCP server and future sandbox backends."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BACKEND_ENV = "SANDBOXED_GOOSE_BACKEND"
WORKSPACE_ENV = "SANDBOXED_GOOSE_WORKSPACE"
SESSION_DATABASE_ENV = "SANDBOXED_GOOSE_SESSION_DATABASE"
GOOSE_PATH_ROOT_ENV = "GOOSE_PATH_ROOT"
SESSION_CONTEXT_TRANSPORT_ENV = "SANDBOXED_GOOSE_SESSION_CONTEXT_TRANSPORT"
CONTEXT_IMAGE_ENV = "SANDBOXED_GOOSE_CONTEXT_IMAGE"
APPTAINER_RUNTIME_CONFIG_ENV = "SANDBOXED_GOOSE_APPTAINER_CONFIG"
APPTAINER_STATE_ENV = "SANDBOXED_GOOSE_APPTAINER_STATE"
APPTAINER_EXECUTABLE_ENV = "APPTAINER"

DIRECT_SESSION_CONTEXT_TRANSPORT = "direct"
APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT = "apptainer-fuse"
SESSION_CONTEXT_TRANSPORTS = frozenset(
    {
        DIRECT_SESSION_CONTEXT_TRANSPORT,
        APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    }
)


def _optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True, slots=True)
class Settings:
    """Requested sandbox settings.

    These values describe intent only. They do not make execution available.
    """

    requested_backend: str | None = None
    workspace: Path | None = None
    session_database: Path | None = None
    session_context_transport: str = DIRECT_SESSION_CONTEXT_TRANSPORT
    context_image: Path | None = None
    apptainer_runtime_config: Path | None = None
    apptainer_state: Path | None = None
    apptainer_executable: str = "apptainer"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Load settings without validating a backend that is not implemented yet."""
        source = os.environ if environ is None else environ
        backend = _optional_value(source.get(BACKEND_ENV))
        workspace_value = _optional_value(source.get(WORKSPACE_ENV))
        workspace = Path(workspace_value) if workspace_value is not None else None
        database_value = _optional_value(source.get(SESSION_DATABASE_ENV))
        goose_root_value = _optional_value(source.get(GOOSE_PATH_ROOT_ENV))
        if database_value is not None:
            session_database = Path(database_value)
        elif goose_root_value is not None:
            session_database = Path(goose_root_value) / "data" / "sessions" / "sessions.db"
        else:
            session_database = None
        session_context_transport = (
            _optional_value(source.get(SESSION_CONTEXT_TRANSPORT_ENV))
            or DIRECT_SESSION_CONTEXT_TRANSPORT
        )
        if session_context_transport not in SESSION_CONTEXT_TRANSPORTS:
            supported = ", ".join(sorted(SESSION_CONTEXT_TRANSPORTS))
            raise ValueError(
                f"unsupported session context transport {session_context_transport!r}; "
                f"expected one of: {supported}"
            )
        context_image_value = _optional_value(source.get(CONTEXT_IMAGE_ENV))
        runtime_config_value = _optional_value(source.get(APPTAINER_RUNTIME_CONFIG_ENV))
        apptainer_state_value = _optional_value(source.get(APPTAINER_STATE_ENV))
        apptainer_executable = _optional_value(source.get(APPTAINER_EXECUTABLE_ENV)) or "apptainer"
        return cls(
            requested_backend=backend,
            workspace=workspace,
            session_database=session_database,
            session_context_transport=session_context_transport,
            context_image=Path(context_image_value) if context_image_value is not None else None,
            apptainer_runtime_config=(
                Path(runtime_config_value) if runtime_config_value is not None else None
            ),
            apptainer_state=(
                Path(apptainer_state_value) if apptainer_state_value is not None else None
            ),
            apptainer_executable=apptainer_executable,
        )
