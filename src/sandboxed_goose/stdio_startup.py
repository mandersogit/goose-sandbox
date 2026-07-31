"""Shared fail-closed preparation for stdio MCP server entry points."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from sandboxed_goose.config import Settings
from sandboxed_goose.contextfs.disclosure_ledger import (
    DisclosureLedgerUnavailable,
    LedgerStatus,
    bootstrap_disclosure_ledger,
)
from sandboxed_goose.session_binding import GOOSE_SESSION_ENV_KEY, resolve_session_id


@dataclass(frozen=True, slots=True)
class PreparedStdioServer:
    """Settings and optional managed-session ledger established before stdio starts."""

    settings: Settings
    ledger: LedgerStatus | None


def prepare_stdio_server(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> PreparedStdioServer:
    """Install the exact-session ledger before either MCP framework reads stdin.

    A manual MCP launch with no Goose session environment remains useful for tool-list
    inspection. Once Goose supplies ``AGENT_SESSION_ID``, the session database and
    ledger are mandatory and startup fails closed if either cannot be verified.
    """

    source = os.environ if environ is None else environ
    active_settings = settings if settings is not None else Settings.from_environment(source)
    if GOOSE_SESSION_ENV_KEY not in source:
        return PreparedStdioServer(settings=active_settings, ledger=None)
    session_id = resolve_session_id(None, source)
    if active_settings.session_database is None:
        raise DisclosureLedgerUnavailable(
            "Goose supplied a bound session but its session database is unavailable"
        )
    ledger = bootstrap_disclosure_ledger(active_settings.session_database, session_id)
    return PreparedStdioServer(settings=active_settings, ledger=ledger)
