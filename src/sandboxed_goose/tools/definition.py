"""Framework-neutral tool metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Metadata shared by every framework adapter."""

    name: str
    description: str
