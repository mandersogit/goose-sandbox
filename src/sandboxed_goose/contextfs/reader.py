"""Fixed in-container reader for the mounted ContextFS tree."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from sandboxed_goose.contextfs.goose_session import normalize_requested_path
from sandboxed_goose.contextfs.model import ProjectionError

CONTEXT_ROOT = Path("/context")
MAX_TOOL_READ_BYTES = 64 * 1024
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _validate_request(path: str, offset: int, limit: int) -> str:
    if offset < 0:
        raise ProjectionError("offset must be non-negative")
    if not 1 <= limit <= MAX_TOOL_READ_BYTES:
        raise ProjectionError("limit must be between 1 and 65536 bytes")
    return normalize_requested_path(path)


def _decode_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def is_fuse_mount(mountpoint: Path, mountinfo: str) -> bool:
    """Return whether mountinfo identifies the exact path as a FUSE mount."""

    expected = str(mountpoint)
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields) or len(fields) < 6:
            continue
        mounted_at = _decode_mount_field(fields[4])
        filesystem_type = fields[separator + 1]
        if mounted_at == expected and (
            filesystem_type == "fuse" or filesystem_type.startswith("fuse.")
        ):
            return True
    return False


def require_contextfs_mount(
    mountpoint: Path = CONTEXT_ROOT,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Fail unless the fixed context root is backed by FUSE in this namespace."""

    try:
        mountinfo = mountinfo_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProjectionError("cannot inspect the container mount namespace") from error
    if not is_fuse_mount(mountpoint, mountinfo):
        raise ProjectionError(f"{mountpoint} is not a FUSE mount")


def _open_target(root: Path, normalized_path: str) -> tuple[list[int], int]:
    base_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_descriptor = os.open(root, base_flags | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise ProjectionError("cannot open the projected context root") from error

    descriptors = [root_descriptor]
    current = root_descriptor
    parts = normalized_path.split("/") if normalized_path else []
    try:
        for index, part in enumerate(parts):
            flags = base_flags
            if index < len(parts) - 1:
                flags |= getattr(os, "O_DIRECTORY", 0)
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
    except OSError as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise ProjectionError("projected path does not exist or is not traversable") from error
    return descriptors, current


def _render_file(descriptor: int, normalized_path: str, offset: int, limit: int) -> str:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise ProjectionError("projected path is not a regular file")
    content = os.pread(descriptor, limit, offset)
    chunk_end = offset + len(content)
    while content:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            content = content[:-1]
            chunk_end -= 1
        else:
            break
    else:
        if offset < details.st_size:
            raise ProjectionError(
                "offset is not a UTF-8 boundary or limit is too small for the next character"
            )
        decoded = ""
    return json.dumps(
        {
            "path": f"/context/{normalized_path}",
            "type": "file",
            "read_only": True,
            "size": details.st_size,
            "offset": offset,
            "next_offset": chunk_end if chunk_end < details.st_size else None,
            "content": decoded,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _render_directory(descriptor: int, normalized_path: str) -> str:
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        raise ProjectionError("projected path is not a directory")
    entries: list[dict[str, str]] = []
    try:
        names = os.listdir(descriptor)
        for name in sorted(names):
            child = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(child.st_mode):
                entry_type = "directory"
            elif stat.S_ISREG(child.st_mode):
                entry_type = "file"
            else:
                raise ProjectionError("projected directory contains an unsupported node type")
            entries.append({"name": name, "type": entry_type})
    except OSError as error:
        raise ProjectionError("cannot list projected directory") from error
    return json.dumps(
        {
            "path": "/context" + (f"/{normalized_path}" if normalized_path else ""),
            "type": "directory",
            "read_only": True,
            "entries": entries,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def render_mounted_path(
    root: Path,
    path: str,
    *,
    offset: int,
    limit: int,
) -> str:
    """Render one path by performing ordinary reads against a mounted tree."""

    normalized_path = _validate_request(path, offset, limit)
    descriptors, target = _open_target(root, normalized_path)
    try:
        details = os.fstat(target)
        if stat.S_ISREG(details.st_mode):
            return _render_file(target, normalized_path, offset, limit)
        if stat.S_ISDIR(details.st_mode):
            return _render_directory(target, normalized_path)
        raise ProjectionError("projected path has an unsupported node type")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the fixed reader's bounded path request."""

    parser = argparse.ArgumentParser(
        description="Read one bounded path from the mounted ContextFS tree."
    )
    parser.add_argument("--path", default="")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=MAX_TOOL_READ_BYTES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Require the real FUSE mount and emit exactly one JSON response."""

    args = parse_args(argv)
    try:
        require_contextfs_mount()
        rendered = render_mounted_path(
            CONTEXT_ROOT,
            args.path,
            offset=args.offset,
            limit=args.limit,
        )
    except ProjectionError as error:
        print(f"context reader failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(rendered)


if __name__ == "__main__":
    main()
