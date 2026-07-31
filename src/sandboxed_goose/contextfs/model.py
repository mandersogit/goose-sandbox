"""Framework-neutral model for an immutable projected filesystem snapshot."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

ROOT_INODE: Final = 1
MAX_DEPTH: Final = 16
MAX_FILE_BYTES: Final = 1024 * 1024
MAX_READ_BYTES: Final = 256 * 1024
MAX_NODES: Final = 1024
MAX_NAME_BYTES: Final = 255


class ProjectionError(ValueError):
    """Raised when a proposed snapshot violates the projection contract."""


@dataclass(frozen=True, slots=True)
class Node:
    """One immutable regular file or directory in a snapshot."""

    inode: int
    parent_inode: int
    name: bytes
    mode: int
    content: bytes | None
    children: tuple[tuple[bytes, int], ...]

    @property
    def is_directory(self) -> bool:
        """Return whether this node is a directory."""

        return stat.S_ISDIR(self.mode)

    @property
    def size(self) -> int:
        """Return the logical file size; directories have size zero."""

        return len(self.content) if self.content is not None else 0


@dataclass(slots=True)
class _MutableNode:
    name: bytes
    parent: _MutableNode | None
    content: bytes | None
    children: dict[bytes, _MutableNode]


class Snapshot:
    """A bounded, deterministic inode tree built entirely in memory."""

    def __init__(self, nodes: tuple[Node, ...]) -> None:
        if not nodes or nodes[0].inode != ROOT_INODE:
            raise ProjectionError("snapshot must begin with root inode 1")
        self._nodes = {node.inode: node for node in nodes}
        self._children = {node.inode: dict(node.children) for node in nodes if node.is_directory}

    @classmethod
    def from_files(cls, files: Mapping[str, bytes]) -> Snapshot:
        """Build a snapshot from bounded relative POSIX paths and byte contents."""

        root = _MutableNode(name=b"", parent=None, content=None, children={})
        node_count = 1

        for path, content in sorted(files.items(), key=lambda item: item[0].encode("utf-8")):
            if not isinstance(content, bytes):
                raise ProjectionError(f"content for {path!r} must be bytes")
            if len(content) > MAX_FILE_BYTES:
                raise ProjectionError(f"content for {path!r} exceeds {MAX_FILE_BYTES} bytes")

            parts = _validate_path(path)
            current = root
            for index, name in enumerate(parts):
                is_file = index == len(parts) - 1
                child = current.children.get(name)
                if child is None:
                    node_count += 1
                    if node_count > MAX_NODES:
                        raise ProjectionError(f"snapshot exceeds {MAX_NODES} nodes")
                    child = _MutableNode(
                        name=name,
                        parent=current,
                        content=content if is_file else None,
                        children={},
                    )
                    current.children[name] = child
                elif is_file:
                    if child.children:
                        raise ProjectionError(f"path {path!r} replaces an existing directory")
                    if child.content is not None:
                        raise ProjectionError(f"duplicate path {path!r}")
                    child.content = content
                elif child.content is not None:
                    raise ProjectionError(f"path {path!r} traverses a regular file")
                current = child

        ordered: list[_MutableNode] = []

        def visit(node: _MutableNode) -> None:
            ordered.append(node)
            for name in sorted(node.children):
                visit(node.children[name])

        visit(root)
        inode_by_identity = {id(node): index + ROOT_INODE for index, node in enumerate(ordered)}
        immutable_nodes: list[Node] = []
        for mutable in ordered:
            inode = inode_by_identity[id(mutable)]
            parent_inode = (
                inode if mutable.parent is None else inode_by_identity[id(mutable.parent)]
            )
            is_directory = mutable.content is None
            immutable_nodes.append(
                Node(
                    inode=inode,
                    parent_inode=parent_inode,
                    name=mutable.name,
                    mode=(stat.S_IFDIR | 0o555) if is_directory else (stat.S_IFREG | 0o444),
                    content=mutable.content,
                    children=tuple(
                        (name, inode_by_identity[id(child)])
                        for name, child in sorted(mutable.children.items())
                    ),
                )
            )
        return cls(tuple(immutable_nodes))

    @property
    def node_count(self) -> int:
        """Return the number of inodes in this snapshot."""

        return len(self._nodes)

    @property
    def total_file_bytes(self) -> int:
        """Return the aggregate logical size of all regular files."""

        return sum(node.size for node in self._nodes.values())

    def get(self, inode: int) -> Node:
        """Return an inode or raise ``KeyError``."""

        return self._nodes[inode]

    def lookup(self, parent_inode: int, name: bytes) -> Node:
        """Look up a direct child using the kernel-supplied byte name."""

        parent = self.get(parent_inode)
        if not parent.is_directory:
            raise NotADirectoryError(parent_inode)
        if name == b".":
            return parent
        if name == b"..":
            return self.get(parent.parent_inode)
        return self.get(self._children[parent_inode][name])

    def list_directory(self, inode: int) -> tuple[tuple[bytes, Node], ...]:
        """Return a stable, name-sorted directory listing."""

        node = self.get(inode)
        if not node.is_directory:
            raise NotADirectoryError(inode)
        return tuple((name, self.get(child_inode)) for name, child_inode in node.children)

    def read(self, inode: int, offset: int, size: int) -> bytes:
        """Return one bounded slice of a regular file."""

        if offset < 0:
            raise ProjectionError("read offset must be non-negative")
        if size < 0 or size > MAX_READ_BYTES:
            raise ProjectionError(f"read size must be between 0 and {MAX_READ_BYTES} bytes")
        node = self.get(inode)
        if node.is_directory or node.content is None:
            raise IsADirectoryError(inode)
        return node.content[offset : offset + size]


def _validate_path(path: str) -> tuple[bytes, ...]:
    if not isinstance(path, str):
        raise ProjectionError("snapshot paths must be strings")
    if not path or path.startswith("/") or path.endswith("/"):
        raise ProjectionError(f"path {path!r} must be a non-empty relative file path")
    raw_parts = path.split("/")
    if len(raw_parts) > MAX_DEPTH:
        raise ProjectionError(f"path {path!r} exceeds depth {MAX_DEPTH}")

    encoded: list[bytes] = []
    for part in raw_parts:
        if part in {"", ".", ".."} or "\x00" in part:
            raise ProjectionError(f"path {path!r} contains an invalid component")
        name = part.encode("utf-8")
        if len(name) > MAX_NAME_BYTES:
            raise ProjectionError(f"path component in {path!r} exceeds {MAX_NAME_BYTES} bytes")
        encoded.append(name)
    return tuple(encoded)


def toy_snapshot() -> Snapshot:
    """Create the deterministic, programmatically generated proof snapshot."""

    primes = [number for number in range(2, 50) if _is_prime(number)]
    squares = {str(number): number * number for number in range(13)}
    payloads = {
        "README.md": (
            b"# Projected context\n\n"
            b"These files are synthesized in memory by ContextFS. There is no backing "
            b"directory containing them.\n"
        ),
        "generated/primes.json": _json_bytes({"limit": 50, "values": primes}),
        "generated/squares.json": _json_bytes({"values": squares}),
        "objects/answer/content.txt": b"The computed answer is 6 * 7 = 42.\n",
        "objects/answer/metadata.json": _json_bytes(
            {"id": "answer", "media_type": "text/plain", "provenance": "toy-generator"}
        ),
    }
    manifest = {
        "schema_version": 1,
        "snapshot_id": "toy-v1",
        "storage": "generated-in-memory",
        "read_only": True,
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(payloads.items())
        ],
    }
    return Snapshot.from_files({"manifest.json": _json_bytes(manifest), **payloads})


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _is_prime(number: int) -> bool:
    return number >= 2 and all(number % divisor for divisor in range(2, int(number**0.5) + 1))
