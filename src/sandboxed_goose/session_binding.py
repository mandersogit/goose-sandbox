"""Resolve the Goose session attached to an MCP request."""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping

GOOSE_SESSION_META_KEY = "agent-session-id"
GOOSE_SESSION_ENV_KEY = "AGENT_SESSION_ID"
MAX_SESSION_ID_BYTES = 256


class SessionBindingError(ValueError):
    """Raised when a tool call cannot be bound to exactly one Goose session."""


def resolve_session_id(
    request_meta: Mapping[str, object] | None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve one session ID, preferring request metadata and rejecting ambiguity."""

    source = os.environ if environ is None else environ
    metadata_value = _metadata_session_id(request_meta)
    environment_value = _optional_session_id(source.get(GOOSE_SESSION_ENV_KEY))

    if (
        metadata_value is not None
        and environment_value is not None
        and metadata_value != environment_value
    ):
        raise SessionBindingError(
            "MCP request session does not match the session-bound extension process"
        )

    session_id = metadata_value or environment_value
    if session_id is None:
        raise SessionBindingError(
            f"Goose did not supply {GOOSE_SESSION_META_KEY!r} request metadata or "
            f"{GOOSE_SESSION_ENV_KEY}"
        )
    return session_id


def _metadata_session_id(request_meta: Mapping[str, object] | None) -> str | None:
    if request_meta is None:
        return None
    matches = [
        value
        for key, value in request_meta.items()
        if key.casefold() == GOOSE_SESSION_META_KEY.casefold()
    ]
    if not matches:
        return None
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise SessionBindingError(f"invalid {GOOSE_SESSION_META_KEY!r} request metadata")
    return _optional_session_id(matches[0])


def _optional_session_id(value: str | None) -> str | None:
    if value is None:
        return None
    session_id = value.strip()
    if not session_id:
        return None
    encoded = session_id.encode("utf-8")
    if len(encoded) > MAX_SESSION_ID_BYTES:
        raise SessionBindingError(f"Goose session ID exceeds {MAX_SESSION_ID_BYTES} UTF-8 bytes")
    if any(unicodedata.category(character).startswith("C") for character in session_id):
        raise SessionBindingError("Goose session ID contains a control character")
    return session_id
