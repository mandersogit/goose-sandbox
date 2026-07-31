from pathlib import Path

from sandboxed_goose.config import (
    BACKEND_ENV,
    GOOSE_PATH_ROOT_ENV,
    SESSION_DATABASE_ENV,
    WORKSPACE_ENV,
    Settings,
)


def test_settings_are_empty_by_default() -> None:
    assert Settings.from_environment({}) == Settings()


def test_settings_parse_backend_and_workspace() -> None:
    settings = Settings.from_environment(
        {
            BACKEND_ENV: " bubblewrap ",
            WORKSPACE_ENV: " /work/project ",
        }
    )

    assert settings == Settings(
        requested_backend="bubblewrap",
        workspace=Path("/work/project"),
    )


def test_blank_values_are_not_configuration() -> None:
    settings = Settings.from_environment(
        {
            BACKEND_ENV: " ",
            WORKSPACE_ENV: "",
        }
    )

    assert settings == Settings()


def test_session_database_is_explicit_or_derived_from_isolated_goose_root() -> None:
    assert Settings.from_environment({GOOSE_PATH_ROOT_ENV: "/test/goose"}).session_database == Path(
        "/test/goose/data/sessions/sessions.db"
    )
    assert Settings.from_environment(
        {
            GOOSE_PATH_ROOT_ENV: "/ignored/goose",
            SESSION_DATABASE_ENV: "/explicit/sessions.db",
        }
    ).session_database == Path("/explicit/sessions.db")
