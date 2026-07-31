"""Configuration shared by the MCP server and future sandbox backends."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BACKEND_ENV = "SANDBOXED_GOOSE_BACKEND"
WORKSPACE_ENV = "SANDBOXED_GOOSE_WORKSPACE"


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

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Load settings without validating a backend that is not implemented yet."""
        source = os.environ if environ is None else environ
        backend = _optional_value(source.get(BACKEND_ENV))
        workspace_value = _optional_value(source.get(WORKSPACE_ENV))
        workspace = Path(workspace_value) if workspace_value is not None else None
        return cls(requested_backend=backend, workspace=workspace)
