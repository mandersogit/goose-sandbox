"""Bounded process-local storage for immutable session-context operation views.

This module owns no SQLite connection and performs no projection.  It stores only
already-sanitized descriptors and materialized files so a later MCP continuation can
remain on its original generation without retaining a long-lived database transaction.
"""

from __future__ import annotations

import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sandboxed_goose.contextfs.model import MAX_DEPTH, MAX_FILE_BYTES, MAX_NAME_BYTES

MAX_VIEW_DESCRIPTORS: Final = 8_192
MAX_VIEW_DESCRIPTOR_BYTES: Final = 4 * 1024 * 1024
MAX_VIEW_FILE_BYTES: Final = MAX_FILE_BYTES
MAX_VIEW_FILES: Final = MAX_VIEW_DESCRIPTORS
MAX_VIEW_PATH_BYTES: Final = 4 * 1024

MAX_VIEWS_PER_SESSION: Final = 4
MAX_VIEWS_PER_PROCESS: Final = 16
MAX_CACHED_BYTES: Final = 32 * 1024 * 1024
MAX_IDLE_LEASE_SECONDS: Final = 10 * 60

VIEW_ID_BYTES: Final = 32
_VIEW_ID_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_ID_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
_VIEW_ACCOUNTING_OVERHEAD: Final = 256
_FILE_ACCOUNTING_OVERHEAD: Final = 128
_TOKEN_ATTEMPTS: Final = 8


class SessionOperation(StrEnum):
    """One projection operation to which a view token can be bound."""

    RECENT_TREE = "recent-tree"
    EXACT_OBJECT = "exact-object"
    TRANSCRIPT = "transcript"
    MANIFEST = "manifest"


class SessionViewError(RuntimeError):
    """A bounded session view cannot be created or continued."""

    code: str = "view_error"
    retryable: bool = False


class ViewExpiredError(SessionViewError):
    """A token is unknown, expired, evicted, or revoked."""

    code = "view_expired"
    retryable = True


class ViewMismatchError(SessionViewError):
    """A token is being used outside its exact operation binding."""

    code = "view_mismatch"


class ViewTooLargeError(SessionViewError):
    """A proposed view cannot fit within the installed hard limits."""

    code = "view_too_large"


@dataclass(frozen=True, slots=True)
class SessionOperationRequest:
    """The immutable identity of one session-context operation."""

    session_id: str
    operation: SessionOperation
    path: str
    projection_schema_version: int

    def __post_init__(self) -> None:
        _validate_bounded_text("session_id", self.session_id, maximum_bytes=256, empty=False)
        if self.session_id != self.session_id.strip():
            raise ValueError("session_id must be trimmed")
        if not isinstance(self.operation, SessionOperation):
            raise TypeError("operation must be a SessionOperation")
        _validate_virtual_path("path", self.path, empty=True)
        _validate_positive_int("projection_schema_version", self.projection_schema_version)


@dataclass(frozen=True, slots=True)
class MaterializedViewFile:
    """One sanitized immutable file retained for an operation continuation."""

    path: str
    content: bytes

    def __post_init__(self) -> None:
        _validate_virtual_path("file path", self.path, empty=False)
        if not isinstance(self.content, bytes):
            raise TypeError("materialized file content must be bytes")
        if len(self.content) > MAX_VIEW_FILE_BYTES:
            raise ViewTooLargeError(f"materialized file exceeds {MAX_VIEW_FILE_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class LedgerCoverageIdentity:
    """The verified ledger generation against which a view was created."""

    schema_version: int
    schema_fingerprint: str
    coverage_epoch: int
    database_identity: str
    session_incarnation: str

    def __post_init__(self) -> None:
        _validate_positive_int("ledger schema_version", self.schema_version)
        if (
            not isinstance(self.schema_fingerprint, str)
            or _SNAPSHOT_ID_PATTERN.fullmatch(self.schema_fingerprint) is None
        ):
            raise ValueError(
                "ledger schema_fingerprint must be a lowercase 256-bit hexadecimal digest"
            )
        _validate_positive_int("coverage_epoch", self.coverage_epoch)
        for name, value in (
            ("database_identity", self.database_identity),
            ("session_incarnation", self.session_incarnation),
        ):
            if not isinstance(value, str) or _SNAPSHOT_ID_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase 256-bit hexadecimal digest")


@dataclass(frozen=True, slots=True)
class SessionOperationResult:
    """Sanitized immutable state retained for one pinned operation."""

    snapshot_id: str
    ledger_coverage: LedgerCoverageIdentity
    descriptor_count: int
    descriptor_data: bytes
    files: tuple[MaterializedViewFile, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot_id, str)
            or _SNAPSHOT_ID_PATTERN.fullmatch(self.snapshot_id) is None
        ):
            raise ValueError("snapshot_id must be a lowercase 256-bit hexadecimal digest")
        if not isinstance(self.ledger_coverage, LedgerCoverageIdentity):
            raise TypeError("ledger_coverage must be a LedgerCoverageIdentity")
        _validate_nonnegative_int("descriptor_count", self.descriptor_count)
        if self.descriptor_count > MAX_VIEW_DESCRIPTORS:
            raise ViewTooLargeError(f"descriptor count exceeds {MAX_VIEW_DESCRIPTORS} entries")
        if not isinstance(self.descriptor_data, bytes):
            raise TypeError("descriptor_data must be bytes")
        if len(self.descriptor_data) > MAX_VIEW_DESCRIPTOR_BYTES:
            raise ViewTooLargeError(f"descriptor data exceeds {MAX_VIEW_DESCRIPTOR_BYTES} bytes")
        if not isinstance(self.files, tuple):
            raise TypeError("files must be an immutable tuple")
        if len(self.files) > MAX_VIEW_FILES:
            raise ViewTooLargeError(f"materialized file count exceeds {MAX_VIEW_FILES}")
        seen_paths: set[str] = set()
        payload_bytes = len(self.descriptor_data)
        for file in self.files:
            if not isinstance(file, MaterializedViewFile):
                raise TypeError("files must contain MaterializedViewFile values")
            if file.path in seen_paths:
                raise ValueError("materialized file paths must be unique")
            seen_paths.add(file.path)
            payload_bytes += len(file.path.encode("utf-8")) + len(file.content)
        if payload_bytes > MAX_CACHED_BYTES:
            raise ViewTooLargeError("operation result exceeds the hard process cache byte limit")

    @classmethod
    def from_files(
        cls,
        *,
        snapshot_id: str,
        ledger_coverage: LedgerCoverageIdentity,
        descriptor_count: int,
        descriptor_data: bytes,
        files: Mapping[str, bytes],
    ) -> SessionOperationResult:
        """Freeze a caller-owned file mapping in deterministic path order."""

        if not isinstance(files, Mapping):
            raise TypeError("files must be a mapping")
        if len(files) > MAX_VIEW_FILES:
            raise ViewTooLargeError(f"materialized file count exceeds {MAX_VIEW_FILES}")
        frozen_files = tuple(
            sorted(
                (
                    MaterializedViewFile(path=path, content=content)
                    for path, content in files.items()
                ),
                key=lambda file: file.path.encode("utf-8"),
            )
        )
        return cls(
            snapshot_id=snapshot_id,
            ledger_coverage=ledger_coverage,
            descriptor_count=descriptor_count,
            descriptor_data=descriptor_data,
            files=frozen_files,
        )

    def file_bytes(self, path: str) -> bytes:
        """Return one retained file without exposing a mutable container."""

        for file in self.files:
            if file.path == path:
                return file.content
        raise KeyError(path)


@dataclass(frozen=True, slots=True)
class SessionView:
    """A public immutable copy of one live store entry."""

    view_id: str
    request: SessionOperationRequest
    result: SessionOperationResult
    created_at: float
    last_accessed_at: float
    cached_bytes: int


@dataclass(frozen=True, slots=True)
class SessionViewStoreLimits:
    """Configurable limits that may only tighten the initial hard maxima."""

    max_views_per_session: int = MAX_VIEWS_PER_SESSION
    max_views_per_process: int = MAX_VIEWS_PER_PROCESS
    max_cached_bytes: int = MAX_CACHED_BYTES
    idle_lease_seconds: int = MAX_IDLE_LEASE_SECONDS

    def __post_init__(self) -> None:
        _validate_limit("max_views_per_session", self.max_views_per_session, MAX_VIEWS_PER_SESSION)
        _validate_limit("max_views_per_process", self.max_views_per_process, MAX_VIEWS_PER_PROCESS)
        if self.max_views_per_session > self.max_views_per_process:
            raise ValueError("max_views_per_session cannot exceed max_views_per_process")
        _validate_limit("max_cached_bytes", self.max_cached_bytes, MAX_CACHED_BYTES)
        _validate_limit("idle_lease_seconds", self.idle_lease_seconds, MAX_IDLE_LEASE_SECONDS)


@dataclass(slots=True)
class _StoredView:
    view_id: str
    request: SessionOperationRequest
    result: SessionOperationResult
    created_at: float
    last_accessed_at: float
    cached_bytes: int

    def immutable(self) -> SessionView:
        return SessionView(
            view_id=self.view_id,
            request=self.request,
            result=self.result,
            created_at=self.created_at,
            last_accessed_at=self.last_accessed_at,
            cached_bytes=self.cached_bytes,
        )


class SessionViewStore:
    """Thread-safe bounded LRU store for sanitized immutable operation views."""

    def __init__(
        self,
        limits: SessionViewStoreLimits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._limits = limits if limits is not None else SessionViewStoreLimits()
        self._clock = clock
        self._token_bytes = token_bytes
        self._views: OrderedDict[str, _StoredView] = OrderedDict()
        self._cached_bytes = 0
        self._lock = threading.RLock()

    @property
    def limits(self) -> SessionViewStoreLimits:
        """Return the immutable installed limits."""

        return self._limits

    @property
    def live_views(self) -> int:
        """Return the live view count after lazy expiry."""

        with self._lock:
            self._expire_idle(self._now())
            return len(self._views)

    @property
    def cached_bytes(self) -> int:
        """Return deterministic accounted bytes after lazy expiry."""

        with self._lock:
            self._expire_idle(self._now())
            return self._cached_bytes

    def live_views_for_session(self, session_id: str) -> int:
        """Return the live view count for one exact session."""

        _validate_bounded_text("session_id", session_id, maximum_bytes=256, empty=False)
        with self._lock:
            self._expire_idle(self._now())
            return sum(entry.request.session_id == session_id for entry in self._views.values())

    def create(
        self,
        request: SessionOperationRequest,
        result: SessionOperationResult,
    ) -> SessionView:
        """Create a new view, evicting least-recently-used views when necessary."""

        if not isinstance(request, SessionOperationRequest):
            raise TypeError("request must be a SessionOperationRequest")
        if not isinstance(result, SessionOperationResult):
            raise TypeError("result must be a SessionOperationResult")
        accounted_bytes = _accounted_bytes(request, result)
        if accounted_bytes > self._limits.max_cached_bytes:
            raise ViewTooLargeError("operation view exceeds the process cache byte limit")

        with self._lock:
            now = self._now()
            self._expire_idle(now)
            while self._views_for_session(request.session_id) >= self._limits.max_views_per_session:
                self._evict_oldest_for_session(request.session_id)
            while self._views and (
                len(self._views) >= self._limits.max_views_per_process
                or self._cached_bytes + accounted_bytes > self._limits.max_cached_bytes
            ):
                self._remove(next(iter(self._views)))

            view_id = self._new_view_id()
            entry = _StoredView(
                view_id=view_id,
                request=request,
                result=result,
                created_at=now,
                last_accessed_at=now,
                cached_bytes=accounted_bytes,
            )
            self._views[view_id] = entry
            self._cached_bytes += accounted_bytes
            return entry.immutable()

    def get(
        self,
        view_id: str,
        request: SessionOperationRequest,
        *,
        current_ledger_coverage: LedgerCoverageIdentity | None,
    ) -> SessionView:
        """Return a matching live view or one of the bounded typed failures."""

        if not isinstance(request, SessionOperationRequest):
            raise TypeError("request must be a SessionOperationRequest")
        if current_ledger_coverage is not None and not isinstance(
            current_ledger_coverage, LedgerCoverageIdentity
        ):
            raise TypeError("current_ledger_coverage must be a LedgerCoverageIdentity or None")

        with self._lock:
            now = self._now()
            self._expire_idle(now)
            if (
                not isinstance(view_id, str)
                or len(view_id) != VIEW_ID_BYTES * 2
                or _VIEW_ID_PATTERN.fullmatch(view_id) is None
            ):
                raise ViewExpiredError("session view is unavailable; start a new operation")
            entry = self._views.get(view_id)
            if entry is None:
                raise ViewExpiredError("session view is unavailable; start a new operation")
            if entry.request != request:
                raise ViewMismatchError(
                    "session view does not match the requested session, operation, path, or schema"
                )
            if (
                current_ledger_coverage is None
                or current_ledger_coverage != entry.result.ledger_coverage
            ):
                self._remove(view_id)
                raise ViewExpiredError("session view was revoked; start a new operation")

            entry.last_accessed_at = now
            self._views.move_to_end(view_id)
            return entry.immutable()

    def revoke_session(self, session_id: str) -> int:
        """Remove every view for one session and return the number revoked."""

        _validate_bounded_text("session_id", session_id, maximum_bytes=256, empty=False)
        with self._lock:
            matching = [
                view_id
                for view_id, entry in self._views.items()
                if entry.request.session_id == session_id
            ]
            for view_id in matching:
                self._remove(view_id)
            return len(matching)

    def clear(self) -> None:
        """Discard every process-local view."""

        with self._lock:
            self._views.clear()
            self._cached_bytes = 0

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("view-store clock must return a number")
        result = float(value)
        if not math.isfinite(result):
            raise RuntimeError("view-store clock must return a finite number")
        return result

    def _expire_idle(self, now: float) -> None:
        expired = [
            view_id
            for view_id, entry in self._views.items()
            if now - entry.last_accessed_at >= self._limits.idle_lease_seconds
        ]
        for view_id in expired:
            self._remove(view_id)

    def _views_for_session(self, session_id: str) -> int:
        return sum(entry.request.session_id == session_id for entry in self._views.values())

    def _evict_oldest_for_session(self, session_id: str) -> None:
        for view_id, entry in self._views.items():
            if entry.request.session_id == session_id:
                self._remove(view_id)
                return
        raise RuntimeError("session view accounting is inconsistent")

    def _remove(self, view_id: str) -> None:
        entry = self._views.pop(view_id)
        self._cached_bytes -= entry.cached_bytes
        if self._cached_bytes < 0:
            raise RuntimeError("session view byte accounting is inconsistent")

    def _new_view_id(self) -> str:
        for _ in range(_TOKEN_ATTEMPTS):
            raw = self._token_bytes(VIEW_ID_BYTES)
            if not isinstance(raw, bytes) or len(raw) != VIEW_ID_BYTES:
                raise RuntimeError("view token source must return exactly 32 bytes")
            view_id = raw.hex()
            if view_id not in self._views:
                return view_id
        raise RuntimeError("view token source repeatedly returned an existing token")


def _accounted_bytes(
    request: SessionOperationRequest,
    result: SessionOperationResult,
) -> int:
    return (
        _VIEW_ACCOUNTING_OVERHEAD
        + len(request.session_id.encode("utf-8"))
        + len(request.operation.value.encode("ascii"))
        + len(request.path.encode("utf-8"))
        + len(result.snapshot_id)
        + len(result.descriptor_data)
        + sum(
            _FILE_ACCOUNTING_OVERHEAD + len(file.path.encode("utf-8")) + len(file.content)
            for file in result.files
        )
    )


def _validate_bounded_text(
    name: str,
    value: str,
    *,
    maximum_bytes: int,
    empty: bool,
) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not empty and not value:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains a control character")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} is not valid UTF-8") from error
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")


def _validate_virtual_path(name: str, value: str, *, empty: bool) -> None:
    _validate_bounded_text(name, value, maximum_bytes=MAX_VIEW_PATH_BYTES, empty=empty)
    if not value:
        return
    if value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{name} must be a normalized relative path")
    parts = value.split("/")
    if len(parts) > MAX_DEPTH:
        raise ValueError(f"{name} exceeds depth {MAX_DEPTH}")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{name} contains an invalid component")
    if any(len(part.encode("utf-8")) > MAX_NAME_BYTES for part in parts):
        raise ValueError(f"{name} component exceeds {MAX_NAME_BYTES} UTF-8 bytes")


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _validate_nonnegative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validate_limit(name: str, value: int, maximum: int) -> None:
    _validate_positive_int(name, value)
    if value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
