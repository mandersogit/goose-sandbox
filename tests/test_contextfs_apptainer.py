from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import sandboxed_goose.contextfs.apptainer as apptainer_module
from sandboxed_goose.config import (
    APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    Settings,
)
from sandboxed_goose.contextfs.bundle import load_bundle
from sandboxed_goose.contextfs.goose_session import SessionProjection
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.contextfs.reader import render_mounted_path


def _settings(tmp_path: Path) -> Settings:
    executable = tmp_path / "apptainer"
    executable.write_text("test executable", encoding="utf-8")
    executable.chmod(0o755)
    image = tmp_path / "context.sif"
    image.write_bytes(b"SIF")
    runtime_config = tmp_path / "apptainer.conf"
    runtime_config.write_text("enable fusemount = yes\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return Settings(
        session_context_transport=APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
        context_image=image,
        apptainer_runtime_config=runtime_config,
        apptainer_state=state,
        apptainer_executable=str(executable),
    )


def _projection() -> SessionProjection:
    return SessionProjection(
        session_id="many-turn-session",
        snapshot_id="snapshot",
        files={"manifest.json": b'{"session_id":"many-turn-session"}\n'},
    )


def test_launcher_uses_a_private_fresh_bundle_and_fixed_fuse_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    observed_bundle: Path | None = None

    def fake_run(
        arguments: list[str],
        environment: dict[str, str],
        timeout: float,
    ) -> SimpleNamespace:
        nonlocal observed_bundle
        assert timeout == 30.0
        assert environment["APPTAINER_CACHEDIR"].startswith(str(settings.apptainer_state))
        bind_value = arguments[arguments.index("--bind") + 1]
        bundle_name, separator, destination = bind_value.partition(":")
        assert separator == ":"
        assert destination == "/run/sandboxed-goose/session-context.json:ro"
        observed_bundle = Path(bundle_name)
        assert observed_bundle.stat().st_mode & 0o777 == 0o600
        assert load_bundle(observed_bundle).node_count == 2
        fuse_command = arguments[arguments.index("--fusemount") + 1]
        assert fuse_command == apptainer_module.APPTAINER_CONTEXTFS_COMMAND
        reader_index = arguments.index(apptainer_module.APPTAINER_CONTEXT_IMAGE_READER)
        assert arguments[reader_index + 1 :] == [
            "--path",
            "manifest.json",
            "--offset",
            "0",
            "--limit",
            "1024",
        ]
        response = {
            "content": '{"session_id":"many-turn-session"}\n',
            "next_offset": None,
            "offset": 0,
            "path": "/context/manifest.json",
            "read_only": True,
            "size": 35,
            "type": "file",
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b"")

    monkeypatch.setattr(apptainer_module, "_run_process", fake_run)
    rendered = apptainer_module.render_projection_via_apptainer(
        settings,
        _projection(),
        "manifest.json",
        offset=0,
        limit=1024,
    )

    assert json.loads(rendered)["path"] == "/context/manifest.json"
    assert observed_bundle is not None
    assert not observed_bundle.exists()
    assert list((settings.apptainer_state / "session-context-runs").iterdir()) == []


def test_launcher_removes_bundle_after_reader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    observed_bundle: Path | None = None

    def fake_run(
        arguments: list[str],
        environment: dict[str, str],
        timeout: float,
    ) -> SimpleNamespace:
        del environment, timeout
        nonlocal observed_bundle
        observed_bundle = Path(arguments[arguments.index("--bind") + 1].partition(":")[0])
        return SimpleNamespace(returncode=17, stdout=b"", stderr=b"reader failed")

    monkeypatch.setattr(apptainer_module, "_run_process", fake_run)
    with pytest.raises(ProjectionError, match="exit status 17"):
        apptainer_module.render_projection_via_apptainer(
            settings,
            _projection(),
            "manifest.json",
            offset=0,
            limit=1024,
        )

    assert observed_bundle is not None
    assert not observed_bundle.exists()


def test_launcher_rejects_unconfigured_or_invalid_output(tmp_path: Path) -> None:
    with pytest.raises(ProjectionError, match="runtime configuration is not configured"):
        apptainer_module.render_projection_via_apptainer(
            Settings(
                session_context_transport=APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
                apptainer_state=tmp_path,
            ),
            _projection(),
            "manifest.json",
            offset=0,
            limit=1024,
        )


@pytest.mark.parametrize("offset", [3, 99])
def test_fixed_reader_and_host_validator_accept_empty_reads_at_or_beyond_eof(
    tmp_path: Path,
    offset: int,
) -> None:
    root = tmp_path / "context"
    root.mkdir()
    (root / "value.txt").write_text("abc", encoding="utf-8")
    encoded = render_mounted_path(root, "value.txt", offset=offset, limit=16).encode()

    rendered = apptainer_module._validate_response(encoded, "value.txt", offset, 16)

    envelope = json.loads(rendered)
    assert envelope["content"] == ""
    assert envelope["next_offset"] is None


def test_host_validator_rejects_content_extending_past_declared_eof() -> None:
    encoded = json.dumps(
        {
            "content": "abcdef",
            "next_offset": None,
            "offset": 0,
            "path": "/context/value.txt",
            "read_only": True,
            "size": 3,
            "type": "file",
        }
    ).encode()

    with pytest.raises(ProjectionError, match="invalid file bounds"):
        apptainer_module._validate_response(encoded, "value.txt", 0, 16)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires Linux process inspection")
def test_process_timeout_terminates_the_reader_process_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    program = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )

    with pytest.raises(ProjectionError, match="timed out"):
        apptainer_module._run_process(
            [sys.executable, "-c", program],
            os.environ.copy(),
            timeout=0.2,
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    child_proc = Path(f"/proc/{child_pid}")
    deadline = time.monotonic() + 1.0
    while child_proc.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not child_proc.exists()
