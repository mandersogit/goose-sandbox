from pathlib import Path

from sandboxed_goose.config import (
    APPTAINER_EXECUTABLE_ENV,
    APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    APPTAINER_RUNTIME_CONFIG_ENV,
    APPTAINER_STATE_ENV,
    BACKEND_ENV,
    CONTEXT_IMAGE_ENV,
    GOOSE_PATH_ROOT_ENV,
    SESSION_CONTEXT_TRANSPORT_ENV,
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


def test_apptainer_fuse_session_context_settings_are_explicit() -> None:
    settings = Settings.from_environment(
        {
            SESSION_CONTEXT_TRANSPORT_ENV: APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
            CONTEXT_IMAGE_ENV: "/test/context.sif",
            APPTAINER_RUNTIME_CONFIG_ENV: "/test/apptainer.conf",
            APPTAINER_STATE_ENV: "/test/state",
            APPTAINER_EXECUTABLE_ENV: "/test/apptainer",
        }
    )

    assert settings.session_context_transport == APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT
    assert settings.context_image == Path("/test/context.sif")
    assert settings.apptainer_runtime_config == Path("/test/apptainer.conf")
    assert settings.apptainer_state == Path("/test/state")
    assert settings.apptainer_executable == "/test/apptainer"


def test_unknown_session_context_transport_is_rejected() -> None:
    try:
        Settings.from_environment({SESSION_CONTEXT_TRANSPORT_ENV: "unknown"})
    except ValueError as error:
        assert "unsupported session context transport" in str(error)
    else:
        raise AssertionError("unknown session context transport was accepted")
