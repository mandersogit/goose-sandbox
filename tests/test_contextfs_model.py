import hashlib
import json
import stat

import pytest

from sandboxed_goose.contextfs import MAX_FILE_BYTES, MAX_READ_BYTES
from sandboxed_goose.contextfs.model import Node, ProjectionError, Snapshot, toy_snapshot


def test_toy_snapshot_is_generated_and_internally_described() -> None:
    snapshot = toy_snapshot()
    root_entries = dict(snapshot.list_directory(1))

    assert tuple(root_entries) == (b"README.md", b"generated", b"manifest.json", b"objects")
    assert all(
        stat.S_IMODE(node.mode) == 0o555 for node in root_entries.values() if node.is_directory
    )

    manifest_node = root_entries[b"manifest.json"]
    manifest = json.loads(snapshot.read(manifest_node.inode, 0, MAX_READ_BYTES))
    assert manifest["snapshot_id"] == "toy-v1"
    assert manifest["storage"] == "generated-in-memory"
    assert manifest["read_only"] is True

    for described_file in manifest["files"]:
        node = _resolve(snapshot, described_file["path"])
        content = snapshot.read(node.inode, 0, MAX_READ_BYTES)
        assert len(content) == described_file["size"]
        assert hashlib.sha256(content).hexdigest() == described_file["sha256"]
        assert stat.S_IMODE(node.mode) == 0o444


def test_snapshot_assigns_stable_inodes_independent_of_input_order() -> None:
    first = Snapshot.from_files({"z/file": b"z", "a/file": b"a"})
    second = Snapshot.from_files({"a/file": b"a", "z/file": b"z"})

    assert _resolve(first, "a/file").inode == _resolve(second, "a/file").inode
    assert _resolve(first, "z/file").inode == _resolve(second, "z/file").inode


@pytest.mark.parametrize(
    "path",
    ["", "/absolute", "trailing/", "a//b", "a/./b", "a/../b"],
)
def test_snapshot_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ProjectionError):
        Snapshot.from_files({path: b"content"})


def test_snapshot_rejects_file_directory_conflicts_and_oversized_content() -> None:
    with pytest.raises(ProjectionError):
        Snapshot.from_files({"object": b"file", "object/child": b"child"})
    with pytest.raises(ProjectionError):
        Snapshot.from_files({"large": b"x" * (MAX_FILE_BYTES + 1)})


def test_reads_are_bounded_and_slice_without_materialization() -> None:
    snapshot = Snapshot.from_files({"value": b"0123456789"})
    inode = _resolve(snapshot, "value").inode

    assert snapshot.read(inode, 3, 4) == b"3456"
    assert snapshot.read(inode, 50, 4) == b""
    with pytest.raises(ProjectionError):
        snapshot.read(inode, -1, 1)
    with pytest.raises(ProjectionError):
        snapshot.read(inode, 0, MAX_READ_BYTES + 1)


def _resolve(snapshot: Snapshot, path: str) -> Node:
    node = snapshot.get(1)
    for part in path.split("/"):
        node = snapshot.lookup(node.inode, part.encode())
    return node
