"""pyfuse3 adapter for immutable context snapshots.

This module intentionally has image-only runtime dependencies. The normal MCP
server and pure snapshot tests do not import it.
"""

from __future__ import annotations

import errno
import os
from typing import Any

import pyfuse3  # type: ignore[import-not-found]
import trio  # type: ignore[import-not-found]

from sandboxed_goose.contextfs.model import Node, ProjectionError, Snapshot

ATTR_TIMEOUT_SECONDS = 300.0
FIXED_TIMESTAMP_NS = 1_700_000_000_000_000_000


class ContextOperations(pyfuse3.Operations):  # type: ignore[misc]
    """Expose one immutable snapshot through the pyfuse3 low-level API."""

    enable_writeback_cache = False
    enable_acl = False
    supports_dot_lookup = False

    def __init__(self, snapshot: Snapshot) -> None:
        super().__init__()
        self._snapshot = snapshot

    def _node(self, inode: int) -> Node:
        try:
            return self._snapshot.get(inode)
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT) from None

    def _attributes(self, node: Node) -> Any:
        entry = pyfuse3.EntryAttributes()
        entry.st_ino = node.inode
        entry.generation = 0
        entry.entry_timeout = ATTR_TIMEOUT_SECONDS
        entry.attr_timeout = ATTR_TIMEOUT_SECONDS
        entry.st_mode = node.mode
        entry.st_nlink = (
            2 + sum(child.is_directory for _, child in self._snapshot.list_directory(node.inode))
            if node.is_directory
            else 1
        )
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_rdev = 0
        entry.st_size = node.size
        entry.st_blksize = 4096
        entry.st_blocks = (node.size + 511) // 512
        entry.st_atime_ns = FIXED_TIMESTAMP_NS
        entry.st_mtime_ns = FIXED_TIMESTAMP_NS
        entry.st_ctime_ns = FIXED_TIMESTAMP_NS
        return entry

    async def getattr(self, inode: int, ctx: Any | None = None) -> Any:
        del ctx
        return self._attributes(self._node(inode))

    async def lookup(self, parent_inode: int, name: bytes, ctx: Any) -> Any:
        del ctx
        try:
            return self._attributes(self._snapshot.lookup(parent_inode, name))
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT) from None
        except NotADirectoryError:
            raise pyfuse3.FUSEError(errno.ENOTDIR) from None

    async def opendir(self, inode: int, ctx: Any) -> int:
        del ctx
        node = self._node(inode)
        if not node.is_directory:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh: int, start_id: int, token: Any) -> None:
        try:
            entries = self._snapshot.list_directory(fh)
        except (KeyError, NotADirectoryError):
            raise pyfuse3.FUSEError(errno.ENOTDIR) from None
        for next_id, (name, node) in enumerate(entries, start=1):
            if next_id <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, name, self._attributes(node), next_id):
                break

    async def open(self, inode: int, flags: int, ctx: Any) -> Any:
        del ctx
        node = self._node(inode)
        if node.is_directory:
            raise pyfuse3.FUSEError(errno.EISDIR)
        if flags & os.O_ACCMODE != os.O_RDONLY or flags & (
            os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_TRUNC
        ):
            raise pyfuse3.FUSEError(errno.EROFS)
        return pyfuse3.FileInfo(fh=inode)

    async def read(self, fh: int, off: int, size: int) -> bytes:
        try:
            return self._snapshot.read(fh, off, size)
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT) from None
        except IsADirectoryError:
            raise pyfuse3.FUSEError(errno.EISDIR) from None
        except ProjectionError:
            raise pyfuse3.FUSEError(errno.EINVAL) from None

    async def access(self, inode: int, mode: int, ctx: Any) -> bool:
        del ctx
        self._node(inode)
        return not bool(mode & os.W_OK)

    async def statfs(self, ctx: Any) -> Any:
        del ctx
        result = pyfuse3.StatvfsData()
        result.f_bsize = 4096
        result.f_frsize = 4096
        result.f_blocks = (self._snapshot.total_file_bytes + 4095) // 4096
        result.f_bfree = 0
        result.f_bavail = 0
        result.f_files = self._snapshot.node_count
        result.f_ffree = 0
        result.f_favail = 0
        return result

    async def setattr(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def mknod(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def mkdir(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def unlink(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def rmdir(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def symlink(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def rename(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def link(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def write(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def setxattr(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def removexattr(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        self._read_only(args, kwargs)

    @staticmethod
    def _read_only(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        del args, kwargs
        raise pyfuse3.FUSEError(errno.EROFS)


def serve(snapshot: Snapshot, mountpoint: str) -> None:
    """Serve a snapshot over an Apptainer-provided FUSE descriptor."""

    options = set(pyfuse3.default_options)
    options.update(
        {
            "fsname=sandboxed-goose-context",
            "subtype=contextfs",
        }
    )
    pyfuse3.init(ContextOperations(snapshot), mountpoint, options)
    try:
        trio.run(pyfuse3.main, 1, 16)
    finally:
        # Apptainer owns the mount. For a pre-opened /dev/fd/N channel libfuse
        # has no mountpoint to unmount, so only destroy our session state.
        pyfuse3.close(unmount=False)
