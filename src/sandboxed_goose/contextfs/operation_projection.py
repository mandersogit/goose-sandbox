"""Public bounded operation views over current rows and same-epoch ledger captures.

Every operation verifies the exact managed session and reads one SQLite snapshot.
Discovery is a capped recent window; exact physical-row lookup is independent of that
window.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from sandboxed_goose.contextfs.disclosure_ledger import (
    ENTRY_TABLE,
    LEDGER_SCHEMA_VERSION,
    OMITTED_CONTENT,
    OMITTED_CREATED_TIMESTAMP,
    OMITTED_MESSAGE_ID,
    OMITTED_ROLE,
    LedgerStatus,
    open_verified_disclosure_snapshot,
)
from sandboxed_goose.contextfs.goose_session import (
    CURRENT_MESSAGE_SQL,
    MAX_SOURCE_CONTENT_BYTES,
    MAX_TRANSCRIPT_BYTES,
    MESSAGE_PATH_PREFIX,
    SOURCE_ROW_ID_WIDTH,
    StableMessageArtifact,
    decode_sqlite_utf8_blob,
    render_stable_message_artifact,
)
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.contextfs.view_store import (
    MAX_VIEW_DESCRIPTOR_BYTES,
    MAX_VIEW_FILE_BYTES,
    LedgerCoverageIdentity,
    SessionOperation,
    SessionOperationRequest,
    SessionOperationResult,
    ViewTooLargeError,
)

OPERATION_PROJECTION_SCHEMA_VERSION: Final = 3
MAX_OPERATION_DESCRIPTORS: Final = 256
MAX_OPERATION_DESCRIPTOR_BYTES: Final = MAX_VIEW_DESCRIPTOR_BYTES
MAX_OPERATION_SOURCE_CONTENT_BYTES: Final = 64 * 1024 * 1024
MAX_SQLITE_ROW_ID: Final = (1 << 63) - 1

_CURRENT_ORIGIN: Final = "current"
_LEDGER_ORIGIN: Final = "ledger-captured"
_Origin = Literal["current", "ledger-captured"]


@dataclass(frozen=True, slots=True)
class _PreflightRow:
    origin: _Origin
    source_row_id: int
    created: int
    message_id_is_null: bool
    message_id_is_text: bool
    message_id_bytes: int
    source_content_bytes: int
    retained_content_bytes: int
    omission_flags: int


@dataclass(frozen=True, slots=True)
class _LoadedRow:
    preflight: _PreflightRow
    message_id: str | None
    message_id_is_utf8: bool
    role: str
    content_json: str | None
    content_is_utf8: bool


def query_session_operation_descriptors(
    database: Path,
    request: SessionOperationRequest,
) -> SessionOperationResult:
    """Read, merge, normalize, and fingerprint one bounded operation atomically."""

    if request.projection_schema_version != OPERATION_PROJECTION_SCHEMA_VERSION:
        raise ProjectionError(
            f"operation projection schema must be {OPERATION_PROJECTION_SCHEMA_VERSION}"
        )
    exact_source_row_id = _validate_materialized_operation(request)

    with open_verified_disclosure_snapshot(database, request.session_id) as (
        connection,
        ledger_status,
    ):
        try:
            counts, count_lower_bounds = _read_counts(
                connection,
                request.session_id,
                ledger_status,
            )
            if exact_source_row_id is None:
                preflight, window_truncated, content_window_truncated = (
                    _preflight_recent_descriptors(
                        connection,
                        request.session_id,
                        ledger_status,
                    )
                )
            else:
                exact = _preflight_exact_descriptor(
                    connection,
                    request.session_id,
                    ledger_status,
                    exact_source_row_id,
                )
                if exact is None:
                    raise ProjectionError("exact projected object does not exist")
                preflight = [exact]
                window_truncated = False
                content_window_truncated = False
            descriptors, artifacts = _load_descriptor_rows(
                connection,
                request.session_id,
                ledger_status,
                preflight,
            )
            if len(descriptors) != len(preflight) or len(artifacts) != len(preflight):
                raise ProjectionError("operation descriptor load count changed within one snapshot")
        except ViewTooLargeError:
            raise
        except ProjectionError:
            raise
        except sqlite3.Error as error:
            raise ProjectionError(f"cannot query Goose operation descriptors: {error}") from error

        descriptor_document = {
            "projection_schema_version": OPERATION_PROJECTION_SCHEMA_VERSION,
            "session_id": request.session_id,
            "operation": {
                "kind": request.operation.value,
                "path": request.path,
            },
            "ledger": _ledger_document(ledger_status),
            "counts": counts,
            "count_lower_bounds": list(count_lower_bounds),
            "history_source": "current-stock-and-same-epoch-ledger-captures",
            "ledger_history_merged": True,
            "recent_window_truncated": window_truncated,
            "content_window_truncated": content_window_truncated,
            "messages": descriptors,
        }
        descriptor_data = _canonical_json_bytes(descriptor_document)
        if len(descriptor_data) > MAX_OPERATION_DESCRIPTOR_BYTES:
            raise ViewTooLargeError(
                f"operation descriptor data exceeds {MAX_OPERATION_DESCRIPTOR_BYTES} bytes"
            )

        snapshot_id = hashlib.sha256(descriptor_data).hexdigest()
        materialized_files = _materialize_operation(
            request,
            snapshot_id=snapshot_id,
            ledger_status=ledger_status,
            counts=counts,
            count_lower_bounds=count_lower_bounds,
            descriptors=descriptors,
            artifacts=artifacts,
            window_truncated=window_truncated,
            content_window_truncated=content_window_truncated,
            descriptor_data=descriptor_data,
        )
        coverage = LedgerCoverageIdentity(
            schema_version=ledger_status.schema_version,
            schema_fingerprint=ledger_status.schema_fingerprint,
            coverage_epoch=ledger_status.coverage_epoch,
            database_identity=ledger_status.database_identity,
            session_incarnation=ledger_status.session_incarnation,
        )
        return SessionOperationResult.from_files(
            snapshot_id=snapshot_id,
            ledger_coverage=coverage,
            descriptor_count=len(descriptors),
            descriptor_data=descriptor_data,
            files=materialized_files,
        )


def _validate_materialized_operation(request: SessionOperationRequest) -> int | None:
    if request.operation is SessionOperation.MANIFEST:
        if request.path != "manifest.json":
            raise ProjectionError("manifest operation requires path 'manifest.json'")
        return None
    if request.operation is SessionOperation.TRANSCRIPT:
        if request.path != "session/transcript.md":
            raise ProjectionError("transcript operation requires path 'session/transcript.md'")
        return None
    if request.operation is not SessionOperation.EXACT_OBJECT:
        return None
    pattern = rf"{re.escape(MESSAGE_PATH_PREFIX)}/([0-9]{{{SOURCE_ROW_ID_WIDTH}}})\.json"
    match = re.fullmatch(pattern, request.path)
    if match is None:
        raise ProjectionError("exact-object requires a physical message path")
    source_row_id = int(match.group(1))
    if not 1 <= source_row_id <= MAX_SQLITE_ROW_ID:
        raise ProjectionError("exact-object source row ID is outside SQLite's supported range")
    return source_row_id


def _ledger_document(status: LedgerStatus) -> dict[str, object]:
    return {
        "schema_version": status.schema_version,
        "schema_fingerprint": status.schema_fingerprint,
        "coverage_epoch": status.coverage_epoch,
        "coverage_complete": status.coverage_complete,
        "coverage_reason": status.coverage_reason,
        "capture_enabled": status.capture_enabled,
        "ambiguous_rows_at_bootstrap": status.ambiguous_rows_at_bootstrap,
        "ambiguous_rows_at_bootstrap_is_lower_bound": (
            status.ambiguous_rows_at_bootstrap_is_lower_bound
        ),
        "database_identity": status.database_identity,
        "session_incarnation": status.session_incarnation,
    }


def _read_counts(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
) -> tuple[dict[str, int], tuple[str, ...]]:
    total, total_is_lower_bound = _bounded_stock_count(
        connection,
        "session_id = ?",
        session_id,
    )
    current, current_is_lower_bound = _bounded_stock_count(
        connection,
        f"session_id = ? AND {CURRENT_MESSAGE_SQL}",
        session_id,
    )
    captured, captured_is_lower_bound = _bounded_ledger_count(
        connection,
        session_id,
        ledger_status,
    )
    values = {
        "source_message_rows": total,
        "current_eligible_rows": current,
        "ledger_captured_rows": captured,
        "projectable_rows": current + captured,
    }
    lower_bounds = tuple(
        name
        for name, truncated in (
            ("source_message_rows", total_is_lower_bound),
            ("current_eligible_rows", current_is_lower_bound),
            ("ledger_captured_rows", captured_is_lower_bound),
            ("projectable_rows", current_is_lower_bound or captured_is_lower_bound),
        )
        if truncated
    )
    return values, lower_bounds


def _bounded_stock_count(
    connection: sqlite3.Connection,
    predicate: str,
    session_id: str,
) -> tuple[int, bool]:
    rows = connection.execute(
        f"SELECT 1 FROM messages WHERE {predicate} LIMIT ?",  # noqa: S608
        (session_id, MAX_OPERATION_DESCRIPTORS + 1),
    ).fetchall()
    return len(rows), len(rows) > MAX_OPERATION_DESCRIPTORS


def _bounded_ledger_count(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
) -> tuple[int, bool]:
    rows = connection.execute(
        f"""
        SELECT 1 FROM {ENTRY_TABLE} AS entry
        WHERE entry.session_id = ?
          AND entry.coverage_epoch = ?
          AND {_ledger_candidate_sql()}
        LIMIT ?
        """,
        (
            session_id,
            ledger_status.coverage_epoch,
            MAX_OPERATION_DESCRIPTORS + 1,
        ),
    ).fetchall()
    return len(rows), len(rows) > MAX_OPERATION_DESCRIPTORS


def _preflight_recent_descriptors(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
) -> tuple[list[_PreflightRow], bool, bool]:
    current = _preflight_current_rows(connection, session_id, ledger_status)
    captured = _preflight_ledger_rows(connection, session_id, ledger_status)
    window_truncated = (
        len(current) > MAX_OPERATION_DESCRIPTORS or len(captured) > MAX_OPERATION_DESCRIPTORS
    )
    combined = sorted(
        (*current[: MAX_OPERATION_DESCRIPTORS + 1], *captured[: MAX_OPERATION_DESCRIPTORS + 1]),
        key=lambda row: (row.created, row.source_row_id, row.origin),
        reverse=True,
    )
    if len(combined) > MAX_OPERATION_DESCRIPTORS:
        window_truncated = True
    newest = combined[:MAX_OPERATION_DESCRIPTORS]

    retained_bytes = 0
    identity_bytes = 0
    bounded_newest: list[_PreflightRow] = []
    content_window_truncated = False
    for row in newest:
        next_retained_bytes = retained_bytes + row.retained_content_bytes
        next_identity_bytes = identity_bytes + (
            row.message_id_bytes
            if row.message_id_is_text
            and row.message_id_bytes <= ledger_status.limits.max_message_id_bytes
            else 0
        )
        if bounded_newest and (
            next_retained_bytes > MAX_OPERATION_SOURCE_CONTENT_BYTES
            or next_identity_bytes > MAX_OPERATION_DESCRIPTOR_BYTES
        ):
            content_window_truncated = True
            break
        bounded_newest.append(row)
        retained_bytes = next_retained_bytes
        identity_bytes = next_identity_bytes

    bounded_newest.reverse()
    return bounded_newest, window_truncated, content_window_truncated


def _preflight_current_rows(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
) -> list[_PreflightRow]:
    content_limit = min(MAX_SOURCE_CONTENT_BYTES, ledger_status.limits.max_content_bytes)
    rows = connection.execute(
        f"""
        SELECT id AS source_row_id,
               created_timestamp,
               message_id IS NULL AS message_id_is_null,
               typeof(message_id) = 'text' AS message_id_is_text,
               length(CAST(COALESCE(message_id, '') AS BLOB)) AS message_id_bytes,
               length(CAST(content_json AS BLOB)) AS source_content_bytes,
               CASE WHEN length(CAST(content_json AS BLOB)) <= ?
                    THEN length(CAST(content_json AS BLOB)) ELSE 0 END
                    AS retained_content_bytes,
               0 AS omission_flags
        FROM messages
        WHERE session_id = ? AND {CURRENT_MESSAGE_SQL}
        ORDER BY created_timestamp DESC, id DESC
        LIMIT ?
        """,
        (content_limit, session_id, MAX_OPERATION_DESCRIPTORS + 1),
    ).fetchall()
    return [_preflight_from_row(row, _CURRENT_ORIGIN) for row in rows]


def _preflight_ledger_rows(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
) -> list[_PreflightRow]:
    rows = connection.execute(
        f"""
        SELECT source_row_id,
               created_timestamp,
               message_id IS NULL AS message_id_is_null,
               typeof(message_id) = 'text' AS message_id_is_text,
               source_message_id_bytes AS message_id_bytes,
               source_content_bytes,
               length(CAST(COALESCE(content_json, '') AS BLOB)) AS retained_content_bytes,
               omission_flags
        FROM {ENTRY_TABLE} AS entry
        WHERE entry.session_id = ?
          AND entry.coverage_epoch = ?
          AND {_ledger_candidate_sql()}
        ORDER BY created_timestamp DESC, source_row_id DESC
        LIMIT ?
        """,
        (
            session_id,
            ledger_status.coverage_epoch,
            MAX_OPERATION_DESCRIPTORS + 1,
        ),
    ).fetchall()
    return [_preflight_from_row(row, _LEDGER_ORIGIN) for row in rows]


def _preflight_exact_descriptor(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
    source_row_id: int,
) -> _PreflightRow | None:
    content_limit = min(MAX_SOURCE_CONTENT_BYTES, ledger_status.limits.max_content_bytes)
    row = connection.execute(
        f"""
        SELECT id AS source_row_id,
               created_timestamp,
               message_id IS NULL AS message_id_is_null,
               typeof(message_id) = 'text' AS message_id_is_text,
               length(CAST(COALESCE(message_id, '') AS BLOB)) AS message_id_bytes,
               length(CAST(content_json AS BLOB)) AS source_content_bytes,
               CASE WHEN length(CAST(content_json AS BLOB)) <= ?
                    THEN length(CAST(content_json AS BLOB)) ELSE 0 END
                    AS retained_content_bytes,
               0 AS omission_flags
        FROM messages
        WHERE session_id = ? AND id = ? AND {CURRENT_MESSAGE_SQL}
        LIMIT 1
        """,
        (content_limit, session_id, source_row_id),
    ).fetchone()
    if row is not None:
        return _preflight_from_row(row, _CURRENT_ORIGIN)
    row = connection.execute(
        f"""
        SELECT source_row_id,
               created_timestamp,
               message_id IS NULL AS message_id_is_null,
               typeof(message_id) = 'text' AS message_id_is_text,
               source_message_id_bytes AS message_id_bytes,
               source_content_bytes,
               length(CAST(COALESCE(content_json, '') AS BLOB)) AS retained_content_bytes,
               omission_flags
        FROM {ENTRY_TABLE} AS entry
        WHERE entry.session_id = ?
          AND entry.coverage_epoch = ?
          AND entry.source_row_id = ?
          AND {_ledger_candidate_sql()}
        LIMIT 1
        """,
        (session_id, ledger_status.coverage_epoch, source_row_id),
    ).fetchone()
    return None if row is None else _preflight_from_row(row, _LEDGER_ORIGIN)


def _ledger_candidate_sql() -> str:
    return f"""
        typeof(entry.source_row_id) = 'integer'
        AND entry.source_row_id >= 1
        AND typeof(entry.created_timestamp) = 'integer'
        AND (entry.omission_flags & {OMITTED_CREATED_TIMESTAMP}) = 0
        AND typeof(entry.role) = 'text'
        AND entry.role IN ('user', 'assistant')
        AND (entry.omission_flags & {OMITTED_ROLE}) = 0
        AND typeof(entry.source_message_id_bytes) = 'integer'
        AND entry.source_message_id_bytes >= 0
        AND typeof(entry.source_content_bytes) = 'integer'
        AND entry.source_content_bytes >= 0
        AND typeof(entry.omission_flags) = 'integer'
        AND entry.omission_flags BETWEEN 0 AND 31
        AND (entry.message_id IS NULL OR typeof(entry.message_id) = 'text')
        AND (entry.content_json IS NULL OR typeof(entry.content_json) = 'text')
        AND (
            entry.content_json IS NOT NULL
            OR (entry.omission_flags & {OMITTED_CONTENT}) != 0
        )
        AND entry.ledger_schema_version = {LEDGER_SCHEMA_VERSION}
        AND NOT EXISTS (
            SELECT 1 FROM messages
            WHERE id = entry.source_row_id
              AND session_id = entry.session_id
              AND {CURRENT_MESSAGE_SQL}
        )
    """


def _preflight_from_row(row: sqlite3.Row, origin: _Origin) -> _PreflightRow:
    return _PreflightRow(
        origin=origin,
        source_row_id=_positive_sqlite_int(row["source_row_id"], "source row ID"),
        created=_sqlite_int(row["created_timestamp"], "created timestamp"),
        message_id_is_null=_sqlite_bool(row["message_id_is_null"], "message ID null state"),
        message_id_is_text=_sqlite_bool(row["message_id_is_text"], "message ID type state"),
        message_id_bytes=_nonnegative_sqlite_int(
            row["message_id_bytes"],
            "message ID byte length",
        ),
        source_content_bytes=_nonnegative_sqlite_int(
            row["source_content_bytes"],
            "source content byte length",
        ),
        retained_content_bytes=_nonnegative_sqlite_int(
            row["retained_content_bytes"],
            "retained content byte length",
        ),
        omission_flags=_nonnegative_sqlite_int(row["omission_flags"], "omission flags"),
    )


def _load_descriptor_rows(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
    preflight: list[_PreflightRow],
) -> tuple[list[dict[str, object]], list[tuple[_PreflightRow, StableMessageArtifact]]]:
    descriptors: list[dict[str, object]] = []
    artifacts: list[tuple[_PreflightRow, StableMessageArtifact]] = []
    for expected in preflight:
        loaded = _load_one_descriptor(connection, session_id, ledger_status, expected)
        artifact, message_id_status = _normalize_loaded_descriptor(loaded, ledger_status)
        if len(artifact.file_bytes) > MAX_VIEW_FILE_BYTES:
            artifact, fallback_status = _normalize_loaded_descriptor(
                loaded,
                ledger_status,
                force_content_omission_reason="normalized-content-byte-limit",
            )
            if fallback_status != message_id_status:  # pragma: no cover - local invariant
                raise AssertionError("content omission changed message identity status")
        if len(artifact.file_bytes) > MAX_VIEW_FILE_BYTES:
            raise ViewTooLargeError(f"stable message file exceeds {MAX_VIEW_FILE_BYTES} bytes")
        physical_path = (
            f"{MESSAGE_PATH_PREFIX}/{expected.source_row_id:0{SOURCE_ROW_ID_WIDTH}d}.json"
        )
        descriptors.append(
            {
                "source_row_id": expected.source_row_id,
                "physical_path": physical_path,
                "message_id": loaded.message_id,
                "message_id_status": message_id_status,
                "logical_identity_status": "deferred",
                "role": loaded.role,
                "created": expected.created,
                "context_visibility": expected.origin,
                "eligibility_evidence": (
                    "current-goose-metadata"
                    if expected.origin == _CURRENT_ORIGIN
                    else "same-epoch-project-ledger-capture"
                ),
                "source_content_bytes": expected.source_content_bytes,
                "stable_file_size": len(artifact.file_bytes),
                "stable_file_sha256": artifact.file_sha256,
                "normalized_content_sha256": artifact.normalized_content_sha256,
                "content_blocks": artifact.content_blocks,
                "omissions": list(artifact.omissions),
                "malformed": artifact.malformed,
            }
        )
        artifacts.append((expected, artifact))
    return descriptors, artifacts


def _load_one_descriptor(
    connection: sqlite3.Connection,
    session_id: str,
    ledger_status: LedgerStatus,
    expected: _PreflightRow,
) -> _LoadedRow:
    content_limit = min(MAX_SOURCE_CONTENT_BYTES, ledger_status.limits.max_content_bytes)
    if expected.origin == _CURRENT_ORIGIN:
        row = connection.execute(
            f"""
            SELECT id AS source_row_id,
                   created_timestamp,
                   message_id IS NULL AS message_id_is_null,
                   typeof(message_id) = 'text' AS message_id_is_text,
                   length(CAST(COALESCE(message_id, '') AS BLOB)) AS message_id_bytes,
                   length(CAST(content_json AS BLOB)) AS source_content_bytes,
                   CASE WHEN length(CAST(content_json AS BLOB)) <= ?
                        THEN length(CAST(content_json AS BLOB)) ELSE 0 END
                        AS retained_content_bytes,
                   0 AS omission_flags,
                   CASE WHEN typeof(message_id) = 'text'
                          AND length(CAST(message_id AS BLOB)) <= ?
                        THEN CAST(message_id AS BLOB) ELSE NULL END
                        AS bounded_message_id_blob,
                   CAST(role AS BLOB) AS role_blob,
                   CASE WHEN length(CAST(content_json AS BLOB)) <= ?
                        THEN CAST(content_json AS BLOB) ELSE NULL END
                        AS bounded_content_blob
            FROM messages
            WHERE session_id = ? AND id = ? AND {CURRENT_MESSAGE_SQL}
            LIMIT 1
            """,
            (
                content_limit,
                ledger_status.limits.max_message_id_bytes,
                content_limit,
                session_id,
                expected.source_row_id,
            ),
        ).fetchone()
    else:
        row = connection.execute(
            f"""
            SELECT source_row_id,
                   created_timestamp,
                   message_id IS NULL AS message_id_is_null,
                   typeof(message_id) = 'text' AS message_id_is_text,
                   source_message_id_bytes AS message_id_bytes,
                   source_content_bytes,
                   length(CAST(COALESCE(content_json, '') AS BLOB))
                       AS retained_content_bytes,
                   omission_flags,
                   CASE WHEN typeof(message_id) = 'text'
                          AND source_message_id_bytes <= ?
                          AND (omission_flags & {OMITTED_MESSAGE_ID}) = 0
                        THEN CAST(message_id AS BLOB) ELSE NULL END
                        AS bounded_message_id_blob,
                   CAST(role AS BLOB) AS role_blob,
                   CASE WHEN typeof(content_json) = 'text'
                          AND length(CAST(content_json AS BLOB)) <= ?
                        THEN CAST(content_json AS BLOB) ELSE NULL END
                        AS bounded_content_blob
            FROM {ENTRY_TABLE} AS entry
            WHERE entry.session_id = ?
              AND entry.coverage_epoch = ?
              AND entry.source_row_id = ?
              AND {_ledger_candidate_sql()}
            LIMIT 1
            """,
            (
                ledger_status.limits.max_message_id_bytes,
                content_limit,
                session_id,
                ledger_status.coverage_epoch,
                expected.source_row_id,
            ),
        ).fetchone()
    if row is None:
        raise ProjectionError("operation descriptor changed within one snapshot")
    observed = _preflight_from_row(row, expected.origin)
    if observed != expected:
        raise ProjectionError("operation descriptor row changed within one snapshot")
    message_id, message_id_is_utf8 = decode_sqlite_utf8_blob(
        row["bounded_message_id_blob"], "Goose message ID"
    )
    role, role_is_utf8 = decode_sqlite_utf8_blob(row["role_blob"], "Goose message role")
    content_json, content_is_utf8 = decode_sqlite_utf8_blob(
        row["bounded_content_blob"], "Goose message content"
    )
    if role is None or not role_is_utf8:
        raise ProjectionError("Goose message role has an invalid SQLite type")
    return _LoadedRow(
        preflight=expected,
        message_id=message_id,
        message_id_is_utf8=message_id_is_utf8,
        role=role,
        content_json=content_json,
        content_is_utf8=content_is_utf8,
    )


def _normalize_loaded_descriptor(
    loaded: _LoadedRow,
    ledger_status: LedgerStatus,
    *,
    force_content_omission_reason: str | None = None,
) -> tuple[StableMessageArtifact, str]:
    row = loaded.preflight
    message_id_was_omitted = bool(row.omission_flags & OMITTED_MESSAGE_ID)
    if row.message_id_is_null and not message_id_was_omitted:
        message_id_status = "missing"
    elif row.message_id_is_text and loaded.message_id_is_utf8 and row.message_id_bytes == 0:
        message_id_status = "empty"
    elif row.message_id_bytes > ledger_status.limits.max_message_id_bytes:
        message_id_status = "oversized"
    elif not row.message_id_is_text or not loaded.message_id_is_utf8:
        message_id_status = "invalid"
    elif message_id_was_omitted:
        message_id_status = "omitted"
    elif loaded.message_id is not None:
        message_id_status = "available"
    else:
        raise ProjectionError("bounded message ID state is inconsistent")
    message_id = loaded.message_id if message_id_status in {"available", "empty"} else None
    if force_content_omission_reason is not None:
        content_json = None
        content_omission_reason = force_content_omission_reason
    elif not loaded.content_is_utf8:
        content_json = None
        content_omission_reason = "invalid-content-encoding"
    else:
        content_json = loaded.content_json
        content_omission_reason = (
            "ledger-content-unavailable"
            if row.origin == _LEDGER_ORIGIN
            and row.source_content_bytes <= ledger_status.limits.max_content_bytes
            else "source-content-byte-limit"
        )
    artifact = render_stable_message_artifact(
        projection_schema_version=OPERATION_PROJECTION_SCHEMA_VERSION,
        source_row_id=row.source_row_id,
        message_id=message_id,
        message_id_status=message_id_status,
        role=loaded.role,
        created=row.created,
        source_content_bytes=row.source_content_bytes,
        content_json=content_json,
        content_omission_reason=content_omission_reason,
    )
    return artifact, message_id_status


def _materialize_operation(
    request: SessionOperationRequest,
    *,
    snapshot_id: str,
    ledger_status: LedgerStatus,
    counts: dict[str, int],
    count_lower_bounds: tuple[str, ...],
    descriptors: list[dict[str, object]],
    artifacts: list[tuple[_PreflightRow, StableMessageArtifact]],
    window_truncated: bool,
    content_window_truncated: bool,
    descriptor_data: bytes,
) -> dict[str, bytes]:
    if request.operation is SessionOperation.EXACT_OBJECT:
        if len(artifacts) != 1:
            raise ProjectionError("exact projected object does not exist")
        return {request.path: artifacts[0][1].file_bytes}
    if request.operation is SessionOperation.TRANSCRIPT:
        return {request.path: _render_transcript(artifacts)}
    if request.operation is not SessionOperation.MANIFEST:
        return {}
    return {
        request.path: _pretty_json_bytes(
            {
                "schema_version": OPERATION_PROJECTION_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "projection": "goose-session-operation-view",
                "session_id": request.session_id,
                "operation": {"kind": request.operation.value, "path": request.path},
                "ledger": _ledger_document(ledger_status),
                "counts": counts,
                "count_lower_bounds": list(count_lower_bounds),
                "descriptor_count": len(descriptors),
                "descriptor_sha256": hashlib.sha256(descriptor_data).hexdigest(),
                "ledger_history_merged": True,
                "recent_window_truncated": window_truncated,
                "content_window_truncated": content_window_truncated,
                "read_only": True,
            }
        )
    }


def _render_transcript(
    artifacts: list[tuple[_PreflightRow, StableMessageArtifact]],
) -> bytes:
    sections = ["# Goose session eligible context"]
    truncated = False
    for ordinal, (source, artifact) in enumerate(artifacts, start=1):
        value = json.loads(artifact.file_bytes)
        if not isinstance(value, dict):
            raise AssertionError("stable message artifact must be a JSON object")
        content = value.get("content")
        if not isinstance(content, list):
            raise AssertionError("stable message content must be a list")
        parts = [f"## {ordinal:06d} · {value['role']} · {source.origin} · {value['createdAt']}"]
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(
                    "```json\n" + json.dumps(block, ensure_ascii=False, sort_keys=True) + "\n```"
                )
        candidate = "\n\n".join((*sections, "\n\n".join(parts))).rstrip() + "\n"
        if len(candidate.encode()) > MAX_TRANSCRIPT_BYTES:
            truncated = True
            break
        sections.append("\n\n".join(parts))
    transcript = "\n\n".join(sections).rstrip() + "\n"
    if truncated:
        marker = "\n[transcript truncated by projection byte limit]\n"
        encoded = transcript.encode()
        marker_bytes = marker.encode()
        prefix = encoded[: MAX_TRANSCRIPT_BYTES - len(marker_bytes)]
        transcript = prefix.decode(errors="ignore") + marker
    return transcript.encode()


def _sqlite_bool(value: object, name: str) -> bool:
    result = _sqlite_int(value, name)
    if result not in {0, 1}:
        raise ProjectionError(f"{name} must be zero or one")
    return bool(result)


def _sqlite_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError(f"{name} has an invalid SQLite type")
    return value


def _positive_sqlite_int(value: object, name: str) -> int:
    result = _sqlite_int(value, name)
    if result < 1:
        raise ProjectionError(f"{name} must be positive")
    return result


def _nonnegative_sqlite_int(value: object, name: str) -> int:
    result = _sqlite_int(value, name)
    if result < 0:
        raise ProjectionError(f"{name} must be non-negative")
    return result


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _pretty_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
