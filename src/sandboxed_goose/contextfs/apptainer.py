"""Trusted host launcher for reading a session projection through Apptainer FUSE."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sandboxed_goose.config import Settings
from sandboxed_goose.contextfs.bundle import write_bundle
from sandboxed_goose.contextfs.goose_session import (
    SessionProjection,
    normalize_requested_path,
)
from sandboxed_goose.contextfs.model import ProjectionError

APPTAINER_SESSION_BUNDLE: Final = "/run/sandboxed-goose/session-context.json"
APPTAINER_CONTEXT_IMAGE_READER: Final = "/usr/local/bin/sandboxed-goose-read-context"
APPTAINER_CONTEXTFS_COMMAND: Final = (
    "container:/usr/local/bin/sandboxed-goose-contextfs "
    f"--bundle {APPTAINER_SESSION_BUNDLE} /context"
)
DEFAULT_TIMEOUT_SECONDS: Final = 30.0
MAX_STDOUT_BYTES: Final = 80 * 1024
MAX_STDERR_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _require_regular_file(path: Path | None, description: str) -> Path:
    if path is None:
        raise ProjectionError(f"{description} is not configured")
    try:
        resolved = path.resolve(strict=True)
        details = resolved.stat()
    except OSError as error:
        raise ProjectionError(f"{description} is unavailable") from error
    if not stat.S_ISREG(details.st_mode):
        raise ProjectionError(f"{description} is not a regular file")
    return resolved


def _resolve_executable(value: str) -> Path:
    candidate = shutil.which(value) if "/" not in value else value
    if candidate is None:
        raise ProjectionError("Apptainer executable is unavailable")
    executable = _require_regular_file(Path(candidate), "Apptainer executable")
    if not os.access(executable, os.X_OK):
        raise ProjectionError("Apptainer executable is not executable")
    return executable


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
        details = resolved.stat()
    except OSError as error:
        raise ProjectionError("Apptainer state directory is unavailable") from error
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise ProjectionError("Apptainer state directory is not owned by the current user")
    if details.st_mode & 0o077:
        raise ProjectionError("Apptainer state directory must have mode 0700")
    return resolved


def _runtime_environment(state: Path) -> dict[str, str]:
    runtime_home = _private_directory(state / "home")
    cache = _private_directory(state / "cache")
    temporary = _private_directory(state / "tmp")
    runtime_directory = f"/run/user/{os.getuid()}"
    user = pwd.getpwuid(os.getuid()).pw_name
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(runtime_home),
        "USER": user,
        "LOGNAME": user,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": runtime_directory,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_directory}/bus",
        "APPTAINER_CACHEDIR": str(cache),
        "APPTAINER_TMPDIR": str(temporary),
    }


def _run_process(
    arguments: list[str],
    environment: dict[str, str],
    timeout: float,
) -> _ProcessResult:
    try:
        process = subprocess.Popen(
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProjectionError("cannot start the Apptainer ContextFS reader") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(OSError):
                process.kill()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        raise ProjectionError("Apptainer ContextFS reader timed out") from error
    return _ProcessResult(process.returncode, stdout, stderr)


def _arguments(
    executable: Path,
    runtime_config: Path,
    image: Path,
    bundle: Path,
    path: str,
    offset: int,
    limit: int,
) -> list[str]:
    bind_source = str(bundle)
    if any(character in bind_source for character in (":", ",", "\n", "\r")):
        raise ProjectionError("generated bundle path cannot be represented as an Apptainer bind")
    return [
        str(executable),
        "--config",
        str(runtime_config),
        "exec",
        "--userns",
        "--containall",
        "--cleanenv",
        "--no-eval",
        "--no-privs",
        "--drop-caps",
        "all",
        "--no-umask",
        "--hostname",
        "sandbox",
        "--net",
        "--network",
        "none",
        "--no-mount",
        "home,cwd,hostfs,bind-paths,sys",
        "--no-mount",
        "/etc/hosts,/etc/localtime,/etc/resolv.conf",
        "--memory",
        "512M",
        "--memory-swap",
        "512M",
        "--cpus",
        "1",
        "--pids-limit",
        "32",
        "--bind",
        f"{bind_source}:{APPTAINER_SESSION_BUNDLE}:ro",
        "--fusemount",
        APPTAINER_CONTEXTFS_COMMAND,
        str(image),
        APPTAINER_CONTEXT_IMAGE_READER,
        "--path",
        path,
        "--offset",
        str(offset),
        "--limit",
        str(limit),
    ]


def _validate_response(encoded: bytes, path: str, offset: int, limit: int) -> str:
    if len(encoded) > MAX_STDOUT_BYTES:
        raise ProjectionError("Apptainer ContextFS reader returned too much data")
    try:
        response: Any = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectionError("Apptainer ContextFS reader returned invalid JSON") from error
    if not isinstance(response, dict) or response.get("read_only") is not True:
        raise ProjectionError("Apptainer ContextFS reader returned an invalid envelope")
    normalized = normalize_requested_path(path)
    expected_path = "/context" + (f"/{normalized}" if normalized else "")
    if response.get("path") != expected_path or response.get("type") not in {
        "file",
        "directory",
    }:
        raise ProjectionError("Apptainer ContextFS reader returned the wrong path")
    if response["type"] == "file":
        required = {
            "content",
            "next_offset",
            "offset",
            "path",
            "read_only",
            "size",
            "type",
        }
        if set(response) != required:
            raise ProjectionError("Apptainer ContextFS reader returned an invalid file envelope")
        content = response["content"]
        size = response["size"]
        next_offset = response["next_offset"]
        content_bytes = content.encode("utf-8") if isinstance(content, str) else b""
        content_end = offset + len(content_bytes)
        if (
            not isinstance(content, str)
            or len(content_bytes) > limit
            or not isinstance(response["offset"], int)
            or isinstance(response["offset"], bool)
            or response["offset"] != offset
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or offset > size
            or (
                next_offset is not None
                and (
                    not isinstance(next_offset, int)
                    or isinstance(next_offset, bool)
                    or next_offset <= offset
                    or next_offset > size
                    or next_offset != content_end
                )
            )
            or next_offset is None
            and content_end < size
        ):
            raise ProjectionError("Apptainer ContextFS reader returned invalid file bounds")
    else:
        required = {"entries", "path", "read_only", "type"}
        entries = response.get("entries")
        if set(response) != required or not isinstance(entries, list) or len(entries) > 1024:
            raise ProjectionError(
                "Apptainer ContextFS reader returned an invalid directory envelope"
            )
        previous: str | None = None
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "type"}
                or not isinstance(entry["name"], str)
                or not entry["name"]
                or entry["type"] not in {"file", "directory"}
                or previous is not None
                and entry["name"] <= previous
            ):
                raise ProjectionError(
                    "Apptainer ContextFS reader returned invalid directory entries"
                )
            previous = entry["name"]
    return json.dumps(response, ensure_ascii=False, sort_keys=True)


def render_projection_via_apptainer(
    settings: Settings,
    projection: SessionProjection,
    path: str,
    *,
    offset: int,
    limit: int,
) -> str:
    """Export a fresh bundle and read it only through the in-container FUSE mount."""

    normalize_requested_path(path)
    if offset < 0:
        raise ProjectionError("offset must be non-negative")
    if not 1 <= limit <= 64 * 1024:
        raise ProjectionError("limit must be between 1 and 65536 bytes")
    executable = _resolve_executable(settings.apptainer_executable)
    runtime_config = _require_regular_file(
        settings.apptainer_runtime_config,
        "Apptainer ContextFS runtime configuration",
    )
    image = _require_regular_file(settings.context_image, "Apptainer ContextFS image")
    if settings.apptainer_state is None:
        raise ProjectionError("Apptainer state directory is not configured")
    state = _private_directory(settings.apptainer_state)
    runs = _private_directory(state / "session-context-runs")
    environment = _runtime_environment(state)

    with tempfile.TemporaryDirectory(prefix="read-", dir=runs) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        bundle = temporary / "session-context.json"
        write_bundle(bundle, projection.files)
        arguments = _arguments(
            executable,
            runtime_config,
            image,
            bundle,
            path,
            offset,
            limit,
        )
        result = _run_process(arguments, environment, DEFAULT_TIMEOUT_SECONDS)
        if len(result.stderr) > MAX_STDERR_BYTES:
            raise ProjectionError("Apptainer ContextFS reader emitted too much diagnostic data")
        if result.returncode != 0:
            raise ProjectionError(
                f"Apptainer ContextFS reader failed with exit status {result.returncode}"
            )
        return _validate_response(result.stdout, path, offset, limit)
