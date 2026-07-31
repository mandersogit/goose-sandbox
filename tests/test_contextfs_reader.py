from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandboxed_goose.contextfs.goose_session import (
    SessionProjection,
    render_projection_path,
)
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.contextfs.reader import (
    is_fuse_mount,
    render_mounted_path,
    require_contextfs_mount,
)


@pytest.fixture
def mounted_tree(tmp_path: Path) -> tuple[Path, SessionProjection]:
    files = {
        "manifest.json": b'{"session_id":"many-turn-session"}\n',
        "session/transcript.md": "first\nsecond\nthird é\n".encode(),
        "session/messages/000001.json": b'{"ordinal":1}\n',
    }
    root = tmp_path / "context"
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return root, SessionProjection("many-turn-session", "snapshot", files)


@pytest.mark.parametrize(
    ("path", "offset", "limit"),
    [
        ("", 0, 65536),
        ("/context/session", 0, 1024),
        ("manifest.json", 0, 8),
        ("session/transcript.md", 6, 9),
        ("session/messages/000001.json", 100, 32),
    ],
)
def test_mounted_reader_matches_in_memory_projection(
    mounted_tree: tuple[Path, SessionProjection],
    path: str,
    offset: int,
    limit: int,
) -> None:
    root, projection = mounted_tree

    mounted = render_mounted_path(root, path, offset=offset, limit=limit)
    direct = render_projection_path(projection, path, offset=offset, limit=limit)

    assert json.loads(mounted) == json.loads(direct)


def test_mounted_reader_rejects_traversal_symlinks_and_non_utf8_boundaries(
    mounted_tree: tuple[Path, SessionProjection],
) -> None:
    root, _projection = mounted_tree
    (root / "escape").symlink_to(root.parent)

    with pytest.raises(ProjectionError, match="invalid component"):
        render_mounted_path(root, "../manifest.json", offset=0, limit=32)
    with pytest.raises(ProjectionError, match="does not exist or is not traversable"):
        render_mounted_path(root, "escape", offset=0, limit=32)
    with pytest.raises(ProjectionError, match="exceeds depth 16"):
        render_mounted_path(root, "/".join(["nested"] * 17), offset=0, limit=32)
    with pytest.raises(ProjectionError, match="exceeds 255 bytes"):
        render_mounted_path(root, "x" * 256, offset=0, limit=32)
    transcript = (root / "session" / "transcript.md").read_bytes()
    unicode_offset = transcript.index("é".encode()) + 1
    with pytest.raises(ProjectionError, match="UTF-8 boundary"):
        render_mounted_path(
            root,
            "session/transcript.md",
            offset=unicode_offset,
            limit=1,
        )


def test_reader_requires_the_exact_context_path_to_be_a_fuse_mount(tmp_path: Path) -> None:
    mountinfo = "36 29 0:42 / /context rw,nosuid,nodev - fuse fuse rw,user_id=1000\n"
    assert is_fuse_mount(Path("/context"), mountinfo)
    assert not is_fuse_mount(Path("/context"), mountinfo.replace(" fuse fuse ", " ext4 /dev/vda "))
    assert not is_fuse_mount(Path("/other"), mountinfo)

    mountinfo_path = tmp_path / "mountinfo"
    mountinfo_path.write_text(mountinfo, encoding="utf-8")
    require_contextfs_mount(Path("/context"), mountinfo_path)
    with pytest.raises(ProjectionError, match="not a FUSE mount"):
        require_contextfs_mount(Path("/other"), mountinfo_path)
