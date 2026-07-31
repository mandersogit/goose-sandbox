import pytest

from sandboxed_goose.contextfs.__main__ import APPTAINER_SESSION_BUNDLE, parse_args


def test_cli_accepts_only_apptainer_attached_fuse_arguments() -> None:
    args = parse_args(["/dev/fd/3", "-f"])

    assert args.mountpoint == "/dev/fd/3"
    assert args.foreground is True


def test_cli_accepts_only_the_fixed_session_bundle_path() -> None:
    args = parse_args(["--bundle", APPTAINER_SESSION_BUNDLE, "/dev/fd/3", "-f"])

    assert args.bundle == APPTAINER_SESSION_BUNDLE

    with pytest.raises(SystemExit):
        parse_args(["--bundle", "/tmp/model-selected.json", "/dev/fd/3", "-f"])


@pytest.mark.parametrize(
    "arguments",
    [[], ["/context", "-f"], ["/dev/fd/3"], ["/dev/fd/4", "-f"], ["/dev/fd/3", "-f", "extra"]],
)
def test_cli_rejects_non_apptainer_or_extra_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(arguments)
