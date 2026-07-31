from __future__ import annotations

from pathlib import Path

import pytest

from sandboxed_goose.config import GOOSE_PATH_ROOT_ENV, Settings
from sandboxed_goose.contextfs.disclosure_ledger import (
    DisclosureLedgerUnavailable,
    verify_disclosure_ledger,
)
from sandboxed_goose.session_binding import GOOSE_SESSION_ENV_KEY
from sandboxed_goose.stdio_startup import prepare_stdio_server
from tests.support.stock_goose import StockGooseDatabase


def test_manual_stdio_launch_without_a_goose_session_does_not_mutate_state() -> None:
    prepared = prepare_stdio_server(Settings(), {})

    assert prepared.settings == Settings()
    assert prepared.ledger is None


def test_bound_stdio_launch_bootstraps_the_exact_session_before_returning(
    tmp_path: Path,
) -> None:
    goose_root = tmp_path / "goose"
    database_path = goose_root / "data" / "sessions" / "sessions.db"
    database_path.parent.mkdir(parents=True)
    database = StockGooseDatabase.create(database_path)
    environment = {
        GOOSE_PATH_ROOT_ENV: str(goose_root),
        GOOSE_SESSION_ENV_KEY: "primary",
    }

    prepared = prepare_stdio_server(environ=environment)

    assert prepared.settings.session_database == database.path
    assert prepared.ledger is not None
    assert prepared.ledger.session_id == "primary"
    assert prepared.ledger.ledger_entries == 0
    assert verify_disclosure_ledger(database.path, "primary") == prepared.ledger
    with pytest.raises(DisclosureLedgerUnavailable, match="not ledger-managed"):
        verify_disclosure_ledger(database.path, "decoy")


def test_bound_stdio_launch_fails_when_the_database_is_not_configured() -> None:
    with pytest.raises(DisclosureLedgerUnavailable, match="database is unavailable"):
        prepare_stdio_server(
            Settings(),
            {GOOSE_SESSION_ENV_KEY: "primary"},
        )


def test_present_but_invalid_goose_session_environment_fails_closed() -> None:
    with pytest.raises(ValueError, match="did not supply"):
        prepare_stdio_server(
            Settings(session_database=Path("/unused/sessions.db")),
            {GOOSE_SESSION_ENV_KEY: " "},
        )
