"""Strict serialization for transferring an approved snapshot into Apptainer."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from sandboxed_goose.contextfs.model import MAX_NODES, ProjectionError, Snapshot

BUNDLE_SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILE_BYTES = 8 * 1024 * 1024


def encode_bundle(files: Mapping[str, bytes]) -> bytes:
    """Encode UTF-8 projected files into the bounded transport format."""

    Snapshot.from_files(files)
    total = sum(len(content) for content in files.values())
    if total > MAX_BUNDLE_FILE_BYTES:
        raise ProjectionError(
            f"bundle file content exceeds {MAX_BUNDLE_FILE_BYTES} aggregate bytes"
        )

    text_files: dict[str, str] = {}
    for path, content in sorted(files.items()):
        try:
            text_files[path] = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProjectionError(f"bundle content for {path!r} is not UTF-8") from error

    encoded = (
        json.dumps(
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "files": text_files,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ProjectionError(f"encoded bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    return encoded


def decode_bundle(encoded: bytes) -> Snapshot:
    """Decode and fully validate a snapshot bundle."""

    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ProjectionError(f"encoded bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectionError("snapshot bundle is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "files"}:
        raise ProjectionError("snapshot bundle has an invalid top-level shape")
    if value["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise ProjectionError("snapshot bundle has an unsupported schema version")
    raw_files = value["files"]
    if not isinstance(raw_files, dict) or len(raw_files) > MAX_NODES - 1:
        raise ProjectionError("snapshot bundle has an invalid file map")

    files: dict[str, bytes] = {}
    for path, content in raw_files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ProjectionError("snapshot bundle paths and contents must be strings")
        files[path] = content.encode("utf-8")
    if sum(len(content) for content in files.values()) > MAX_BUNDLE_FILE_BYTES:
        raise ProjectionError(
            f"bundle file content exceeds {MAX_BUNDLE_FILE_BYTES} aggregate bytes"
        )
    return Snapshot.from_files(files)


def load_bundle(path: Path) -> Snapshot:
    """Load a regular, non-symlink bundle without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProjectionError(f"cannot open snapshot bundle: {error}") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ProjectionError("snapshot bundle is not a regular file")
        if details.st_size > MAX_BUNDLE_BYTES:
            raise ProjectionError(f"encoded bundle exceeds {MAX_BUNDLE_BYTES} bytes")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            encoded = stream.read(MAX_BUNDLE_BYTES + 1)
    finally:
        os.close(descriptor)
    return decode_bundle(encoded)


def write_bundle(path: Path, files: Mapping[str, bytes]) -> None:
    """Create a mode-0600 bundle without overwriting or following a path."""

    encoded = encode_bundle(files)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
