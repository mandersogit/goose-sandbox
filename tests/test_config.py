from pathlib import Path

from sandboxed_goose.config import BACKEND_ENV, WORKSPACE_ENV, Settings


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
