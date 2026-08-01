from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import NoReturn

import pytest

import sandboxed_goose.contextfs.operation_projection as operation_projection_module
from sandboxed_goose.contextfs.disclosure_ledger import (
    DisclosureLedgerUnavailable,
    LedgerLimits,
    bootstrap_disclosure_ledger,
    verify_disclosure_ledger,
)
from sandboxed_goose.contextfs.goose_session import (
    project_goose_session,
    render_stable_message_artifact,
)
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.contextfs.operation_projection import (
    OPERATION_PROJECTION_SCHEMA_VERSION,
    query_session_operation_descriptors,
)
from sandboxed_goose.contextfs.view_store import (
    MAX_VIEW_FILE_BYTES,
    LedgerCoverageIdentity,
    SessionOperation,
    SessionOperationRequest,
    SessionOperationResult,
    SessionViewStore,
    ViewExpiredError,
    ViewTooLargeError,
)
from tests.support.stock_goose import (
    StockGooseDatabase,
    canonical_json,
    user_only_metadata,
    visible_metadata,
)


@pytest.fixture
def stock_database(tmp_path: Path) -> StockGooseDatabase:
    return StockGooseDatabase.create(tmp_path / "sessions.db")


def _request(
    operation: SessionOperation = SessionOperation.MANIFEST,
    path: str = "manifest.json",
) -> SessionOperationRequest:
    return SessionOperationRequest(
        session_id="primary",
        operation=operation,
        path=path,
        projection_schema_version=OPERATION_PROJECTION_SCHEMA_VERSION,
    )


def _document(result: SessionOperationResult) -> dict[str, object]:
    value = json.loads(result.descriptor_data)
    assert isinstance(value, dict)
    return value


def _physical_path(source_row_id: int) -> str:
    return f"session/messages/by-source-row/{source_row_id:020d}.json"


def _coverage(database: Path) -> LedgerCoverageIdentity:
    status = verify_disclosure_ledger(database, "primary")
    return LedgerCoverageIdentity(
        schema_version=status.schema_version,
        schema_fingerprint=status.schema_fingerprint,
        coverage_epoch=status.coverage_epoch,
        database_identity=status.database_identity,
        session_incarnation=status.session_incarnation,
    )


def test_descriptor_query_is_exact_session_bounded_and_fully_fingerprinted(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    current_id = stock_database.add_message(
        "primary",
        message_id="current",
        role="user",
        content=[{"type": "text", "text": "CURRENT_CONTENT_NOT_IN_DESCRIPTOR"}],
        created_timestamp=10,
        metadata=visible_metadata(),
    )
    stock_database.add_message(
        "primary",
        message_id="hidden",
        role="user",
        content=[{"type": "text", "text": "NEVER_VISIBLE_SECRET"}],
        created_timestamp=20,
        metadata=user_only_metadata(),
    )
    stock_database.add_message(
        "primary",
        message_id="historical",
        role="assistant",
        content=[{"type": "text", "text": "EXPLICIT_HISTORY_NOT_IN_DESCRIPTOR"}],
        created_timestamp=30,
        metadata={
            "userVisible": False,
            "agentVisible": False,
            "historicallyAgentVisible": True,
        },
    )
    stock_database.add_message(
        "decoy",
        message_id="decoy",
        role="user",
        content=[{"type": "text", "text": "OTHER_SESSION_SECRET"}],
        created_timestamp=40,
        metadata=visible_metadata(),
    )

    result = query_session_operation_descriptors(stock_database.path, _request())
    document = _document(result)
    serialized = result.descriptor_data.decode()

    assert result.snapshot_id == hashlib.sha256(result.descriptor_data).hexdigest()
    assert len(result.snapshot_id) == 64
    assert result.descriptor_count == 1
    assert [file.path for file in result.files] == ["manifest.json"]
    manifest = json.loads(result.file_bytes("manifest.json"))
    assert manifest["snapshot_id"] == result.snapshot_id
    assert manifest["descriptor_sha256"] == hashlib.sha256(result.descriptor_data).hexdigest()
    assert manifest["ledger_history_merged"] is True
    assert document["session_id"] == "primary"
    assert document["counts"] == {
        "source_message_rows": 3,
        "current_eligible_rows": 1,
        "ledger_captured_rows": 0,
        "projectable_rows": 1,
    }
    assert document["ledger_history_merged"] is True
    assert document["history_source"] == "current-stock-and-same-epoch-ledger-captures"
    messages = document["messages"]
    assert isinstance(messages, list)
    assert [message["source_row_id"] for message in messages] == [current_id]
    assert [message["context_visibility"] for message in messages] == ["current"]
    assert all(message["logical_identity_status"] == "deferred" for message in messages)
    for secret in (
        "CURRENT_CONTENT_NOT_IN_DESCRIPTOR",
        "EXPLICIT_HISTORY_NOT_IN_DESCRIPTOR",
        "NEVER_VISIBLE_SECRET",
        "OTHER_SESSION_SECRET",
    ):
        assert secret not in serialized


def test_snapshot_changes_for_database_generation_and_operation_binding(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="one",
        role="user",
        content=[{"type": "text", "text": "one"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    first = query_session_operation_descriptors(stock_database.path, _request())
    transcript = query_session_operation_descriptors(
        stock_database.path,
        _request(SessionOperation.TRANSCRIPT, "session/transcript.md"),
    )
    stock_database.add_message(
        "primary",
        message_id="two",
        role="assistant",
        content=[{"type": "text", "text": "two"}],
        created_timestamp=2,
        metadata=visible_metadata(),
    )
    second = query_session_operation_descriptors(stock_database.path, _request())

    assert first.snapshot_id != transcript.snapshot_id
    assert first.snapshot_id != second.snapshot_id
    assert first.descriptor_count == 1
    assert second.descriptor_count == 2


def test_descriptor_query_rejects_any_other_projection_schema(
    stock_database: StockGooseDatabase,
) -> None:
    request = SessionOperationRequest(
        session_id="primary",
        operation=SessionOperation.MANIFEST,
        path="manifest.json",
        projection_schema_version=4,
    )
    with pytest.raises(ProjectionError, match="schema must be 3"):
        query_session_operation_descriptors(stock_database.path, request)


def test_materialized_operations_require_canonical_supported_paths(
    stock_database: StockGooseDatabase,
) -> None:
    with pytest.raises(ProjectionError, match="manifest operation"):
        query_session_operation_descriptors(
            stock_database.path,
            _request(SessionOperation.MANIFEST, "other.json"),
        )
    with pytest.raises(ProjectionError, match="physical message path"):
        query_session_operation_descriptors(
            stock_database.path,
            _request(SessionOperation.EXACT_OBJECT, "session/transcript.md"),
        )
    out_of_range = _physical_path(1 << 63)
    with pytest.raises(ProjectionError, match="outside SQLite's supported range"):
        query_session_operation_descriptors(
            stock_database.path,
            _request(SessionOperation.EXACT_OBJECT, out_of_range),
        )


def test_exact_object_materializes_one_stable_sanitized_physical_file(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    source_row_id = stock_database.add_message(
        "primary",
        message_id="exact",
        role="assistant",
        content=[
            {"type": "text", "text": "VISIBLE_EXACT_CONTENT"},
            {"type": "thinking", "thinking": "EXACT_THINKING_SECRET"},
        ],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    path = _physical_path(source_row_id)
    result = query_session_operation_descriptors(
        stock_database.path,
        _request(SessionOperation.EXACT_OBJECT, path),
    )
    payload = json.loads(result.file_bytes(path))
    descriptor = _document(result)["messages"][0]

    assert [file.path for file in result.files] == [path]
    assert payload["sourceRowId"] == source_row_id
    assert payload["messageId"] == "exact"
    assert payload["messageIdStatus"] == "available"
    assert "ordinal" not in payload
    assert "contextVisibility" not in payload
    assert "VISIBLE_EXACT_CONTENT" in result.file_bytes(path).decode()
    assert "EXACT_THINKING_SECRET" not in result.file_bytes(path).decode()
    assert descriptor["stable_file_sha256"] == hashlib.sha256(result.file_bytes(path)).hexdigest()


def test_exact_object_never_crosses_session_or_visibility_boundaries(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    hidden_id = stock_database.add_message(
        "primary",
        message_id="hidden",
        role="user",
        content=[{"type": "text", "text": "HIDDEN"}],
        created_timestamp=1,
        metadata=user_only_metadata(),
    )
    decoy_id = stock_database.add_message(
        "decoy",
        message_id="decoy",
        role="user",
        content=[{"type": "text", "text": "DECOY"}],
        created_timestamp=2,
        metadata=visible_metadata(),
    )
    for source_row_id in (hidden_id, decoy_id):
        with pytest.raises(ProjectionError, match="does not exist"):
            query_session_operation_descriptors(
                stock_database.path,
                _request(SessionOperation.EXACT_OBJECT, _physical_path(source_row_id)),
            )


def test_pinned_exact_view_survives_archive_but_epoch_change_revokes_it(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    source_row_id = stock_database.add_message(
        "primary",
        message_id="archive-me",
        role="user",
        content=[{"type": "text", "text": "PINNED_ORIGINAL"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    request = _request(SessionOperation.EXACT_OBJECT, _physical_path(source_row_id))
    result = query_session_operation_descriptors(stock_database.path, request)
    store = SessionViewStore()
    view = store.create(request, result)

    stock_database.archive_message("primary", "archive-me")
    fresh = query_session_operation_descriptors(stock_database.path, request)
    fresh_descriptor = _document(fresh)["messages"][0]
    assert fresh_descriptor["context_visibility"] == "ledger-captured"
    assert fresh_descriptor["eligibility_evidence"] == "same-epoch-project-ledger-capture"
    assert b"PINNED_ORIGINAL" in fresh.file_bytes(request.path)
    retained = store.get(
        view.view_id,
        request,
        current_ledger_coverage=_coverage(stock_database.path),
    )
    assert b"PINNED_ORIGINAL" in retained.result.file_bytes(request.path)
    after_archive = _coverage(stock_database.path)
    assert after_archive.database_identity == result.ledger_coverage.database_identity
    assert after_archive.session_incarnation == result.ledger_coverage.session_incarnation

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM messages WHERE id = ?", (source_row_id,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ProjectionError, match="does not exist"):
        query_session_operation_descriptors(stock_database.path, request)
    with pytest.raises(ViewExpiredError, match="revoked"):
        store.get(
            view.view_id,
            request,
            current_ledger_coverage=_coverage(stock_database.path),
        )


def test_three_archive_batches_merge_ledger_history_and_keep_exact_access(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    first_request_id = 0
    for ordinal in range(1, 4):
        request_id, _response_id = stock_database.add_tool_pair("primary", ordinal)
        if ordinal == 1:
            first_request_id = request_id
        stock_database.archive_message("primary", f"request-{ordinal:03d}")
        stock_database.archive_message("primary", f"response-{ordinal:03d}")
        stock_database.add_summary(
            "primary",
            summary_id=f"summary-{ordinal:03d}",
            tool_id=f"tool-{ordinal:03d}",
            response_created_timestamp=ordinal * 10 + 1,
        )

        document = _document(query_session_operation_descriptors(stock_database.path, _request()))
        assert document["counts"]["current_eligible_rows"] == ordinal
        assert document["counts"]["ledger_captured_rows"] == ordinal * 2
        assert document["counts"]["projectable_rows"] == ordinal * 3
        assert (
            sum(
                message["context_visibility"] == "ledger-captured"
                for message in document["messages"]
            )
            == ordinal * 2
        )

    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_DESCRIPTORS", 2)
    recent = query_session_operation_descriptors(stock_database.path, _request())
    assert recent.descriptor_count == 2
    assert _document(recent)["recent_window_truncated"] is True

    first_path = _physical_path(first_request_id)
    exact = query_session_operation_descriptors(
        stock_database.path,
        _request(SessionOperation.EXACT_OBJECT, first_path),
    )
    exact_descriptor = _document(exact)["messages"][0]
    assert exact_descriptor["context_visibility"] == "ledger-captured"
    assert b'"name": "calculate"' in exact.file_bytes(first_path)


def test_view_from_another_database_incarnation_is_revoked(tmp_path: Path) -> None:
    first_database = StockGooseDatabase.create(tmp_path / "first.db")
    second_database = StockGooseDatabase.create(tmp_path / "second.db")
    for database, marker in ((first_database, "first"), (second_database, "second")):
        bootstrap_disclosure_ledger(database.path, "primary")
        database.add_message(
            "primary",
            message_id=marker,
            role="user",
            content=[{"type": "text", "text": marker}],
            created_timestamp=1,
            metadata=visible_metadata(),
        )

    request = _request()
    result = query_session_operation_descriptors(first_database.path, request)
    store = SessionViewStore()
    view = store.create(request, result)
    second_coverage = _coverage(second_database.path)

    assert second_coverage.schema_fingerprint == result.ledger_coverage.schema_fingerprint
    assert second_coverage.coverage_epoch == result.ledger_coverage.coverage_epoch
    assert second_coverage.database_identity != result.ledger_coverage.database_identity
    with pytest.raises(ViewExpiredError, match="revoked"):
        store.get(
            view.view_id,
            request,
            current_ledger_coverage=second_coverage,
        )


def test_same_inode_database_replacement_revokes_a_pinned_view(tmp_path: Path) -> None:
    destination = StockGooseDatabase.create(tmp_path / "destination.db")
    replacement = StockGooseDatabase.create(tmp_path / "replacement.db")
    for database, marker in ((destination, "DESTINATION"), (replacement, "REPLACEMENT")):
        bootstrap_disclosure_ledger(database.path, "primary")
        database.add_message(
            "primary",
            message_id=marker,
            role="user",
            content=[{"type": "text", "text": marker}],
            created_timestamp=1,
            metadata=visible_metadata(),
        )

    request = _request()
    result = query_session_operation_descriptors(destination.path, request)
    store = SessionViewStore()
    view = store.create(request, result)
    original_inode = destination.path.stat().st_ino

    source_connection = replacement.connect()
    destination_connection = destination.connect()
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()

    assert destination.path.stat().st_ino == original_inode
    replacement_coverage = _coverage(destination.path)
    assert replacement_coverage.database_identity != result.ledger_coverage.database_identity
    with pytest.raises(ViewExpiredError, match="revoked"):
        store.get(
            view.view_id,
            request,
            current_ledger_coverage=replacement_coverage,
        )
    assert b"REPLACEMENT" in query_session_operation_descriptors(
        destination.path, request
    ).descriptor_data


def test_stable_message_artifact_filters_content_and_has_no_generation_fields() -> None:
    content = [
        {"type": "text", "text": "VISIBLE"},
        {"type": "thinking", "thinking": "THINKING_SECRET"},
        {"type": "actionRequired", "data": "ACTION_REQUIRED_SECRET"},
        {"type": "systemNotification", "data": "SYSTEM_NOTIFICATION_SECRET"},
        {
            "type": "toolResponse",
            "toolResult": {
                "status": "success",
                "value": {
                    "content": [{"type": "text", "text": "RESULT"}],
                    "structuredContent": {"secret": "STRUCTURED_SECRET"},
                    "_meta": {"secret": "META_SECRET"},
                },
            },
        },
    ]
    content_json = canonical_json(content)
    artifact = render_stable_message_artifact(
        projection_schema_version=3,
        source_row_id=7,
        message_id="message-7",
        message_id_status="available",
        role="assistant",
        created=100,
        source_content_bytes=len(content_json.encode()),
        content_json=content_json,
    )
    payload = json.loads(artifact.file_bytes)
    serialized = artifact.file_bytes.decode()

    assert payload["sourceRowId"] == 7
    assert payload["messageId"] == "message-7"
    assert payload["messageIdStatus"] == "available"
    assert "ordinal" not in payload
    assert "contextVisibility" not in payload
    assert "VISIBLE" in serialized
    assert "RESULT" in serialized
    for secret in (
        "THINKING_SECRET",
        "ACTION_REQUIRED_SECRET",
        "SYSTEM_NOTIFICATION_SECRET",
        "STRUCTURED_SECRET",
        "META_SECRET",
    ):
        assert secret not in serialized
    assert artifact.omissions.count("control-content-not-projected") == 2
    assert artifact.file_sha256 == hashlib.sha256(artifact.file_bytes).hexdigest()
    assert len(artifact.normalized_content_sha256) == 64


def test_per_row_limits_omit_values_without_fetching_them_into_descriptors(
    stock_database: StockGooseDatabase,
) -> None:
    limits = LedgerLimits(max_content_bytes=32, max_message_id_bytes=8)
    bootstrap_disclosure_ledger(stock_database.path, "primary", limits=limits)
    stock_database.add_message(
        "primary",
        message_id="oversized-message-id",
        role="user",
        content=[{"type": "text", "text": "OVERSIZED_CONTENT_SECRET" * 4}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )

    result = query_session_operation_descriptors(stock_database.path, _request())
    message = _document(result)["messages"][0]
    serialized = result.descriptor_data.decode()

    assert message["message_id"] is None
    assert message["message_id_status"] == "oversized"
    assert message["omissions"] == ["source-content-byte-limit"]
    assert message["source_content_bytes"] > limits.max_content_bytes
    assert "oversized-message-id" not in serialized
    assert "OVERSIZED_CONTENT_SECRET" not in serialized

    source_row_id = message["source_row_id"]
    exact = query_session_operation_descriptors(
        stock_database.path,
        _request(SessionOperation.EXACT_OBJECT, _physical_path(source_row_id)),
    )
    exact_payload = json.loads(exact.file_bytes(_physical_path(source_row_id)))
    assert exact_payload["messageId"] is None
    assert exact_payload["messageIdStatus"] == "oversized"
    assert exact_payload["omissions"] == ["source-content-byte-limit"]
    assert (
        "OVERSIZED_CONTENT_SECRET" not in exact.file_bytes(_physical_path(source_row_id)).decode()
    )


def test_missing_empty_and_oversized_message_ids_have_explicit_states(
    stock_database: StockGooseDatabase,
) -> None:
    limits = LedgerLimits(max_message_id_bytes=4)
    bootstrap_disclosure_ledger(stock_database.path, "primary", limits=limits)
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """
            INSERT INTO messages
                (message_id, session_id, role, content_json, created_timestamp, metadata_json)
            VALUES (?, 'primary', 'user', '[]', ?, ?)
            """,
            [
                (None, 1, canonical_json(visible_metadata())),
                ("", 2, canonical_json(visible_metadata())),
                ("too-long", 3, canonical_json(visible_metadata())),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    messages = _document(query_session_operation_descriptors(stock_database.path, _request()))[
        "messages"
    ]
    assert [(message["message_id"], message["message_id_status"]) for message in messages] == [
        (None, "missing"),
        ("", "empty"),
        (None, "oversized"),
    ]


def test_descriptor_and_content_windows_truncate_as_contiguous_bounded_suffixes(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    for ordinal in range(3):
        stock_database.add_message(
            "primary",
            message_id=f"message-{ordinal}",
            role="user",
            content=[{"type": "text", "text": "bounded"}],
            created_timestamp=ordinal,
            metadata=visible_metadata(),
        )

    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_DESCRIPTORS", 2)
    capped = query_session_operation_descriptors(stock_database.path, _request())
    capped_document = _document(capped)
    assert capped.descriptor_count == 2
    assert capped_document["recent_window_truncated"] is True
    assert capped_document["count_lower_bounds"] == [
        "source_message_rows",
        "current_eligible_rows",
        "projectable_rows",
    ]

    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_DESCRIPTORS", 8_192)
    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_SOURCE_CONTENT_BYTES", 8)
    byte_capped = query_session_operation_descriptors(stock_database.path, _request())
    byte_document = _document(byte_capped)
    assert byte_capped.descriptor_count == 1
    assert byte_document["content_window_truncated"] is True


def test_nonprojectable_row_counts_are_capped_without_rejecting_bounded_descriptors(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    for ordinal in range(3):
        stock_database.add_message(
            "primary",
            message_id=f"hidden-{ordinal}",
            role="user",
            content=[],
            created_timestamp=ordinal,
            metadata=user_only_metadata(),
        )
    stock_database.add_message(
        "primary",
        message_id="current",
        role="user",
        content=[],
        created_timestamp=4,
        metadata=visible_metadata(),
    )
    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_DESCRIPTORS", 2)

    document = _document(query_session_operation_descriptors(stock_database.path, _request()))

    assert document["counts"]["source_message_rows"] == 3
    assert document["count_lower_bounds"] == ["source_message_rows"]
    assert document["counts"]["current_eligible_rows"] == 1
    assert len(document["messages"]) == 1


def test_descriptor_byte_limit_is_checked_after_normalization(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="one",
        role="user",
        content=[{"type": "text", "text": "one"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_DESCRIPTOR_BYTES", 128)

    with pytest.raises(ViewTooLargeError, match="descriptor data"):
        query_session_operation_descriptors(stock_database.path, _request())


def test_identity_bytes_are_bounded_before_content_load(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="identity",
        role="user",
        content=[{"type": "text", "text": "content"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )

    monkeypatch.setattr(operation_projection_module, "MAX_OPERATION_DESCRIPTOR_BYTES", 4)
    with pytest.raises(ViewTooLargeError, match="descriptor data"):
        query_session_operation_descriptors(stock_database.path, _request())


def test_stable_file_limit_is_checked_before_result_creation(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="one",
        role="user",
        content=[{"type": "text", "text": "content"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    monkeypatch.setattr(operation_projection_module, "MAX_VIEW_FILE_BYTES", 16)

    with pytest.raises(ViewTooLargeError, match="stable message file"):
        query_session_operation_descriptors(stock_database.path, _request())


def test_normalized_file_amplification_degrades_only_the_offending_content(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    arguments = {"values": [[0] * 256 for _ in range(256)]}
    content = [
        {
            "type": "toolRequest",
            "toolCall": {
                "status": "success",
                "value": {"name": "calculate", "arguments": arguments},
            },
        }
    ]
    content_json = canonical_json(content)
    assert len(content_json.encode()) < 512 * 1024
    amplified = render_stable_message_artifact(
        projection_schema_version=3,
        source_row_id=1,
        message_id="amplified",
        message_id_status="available",
        role="assistant",
        created=1,
        source_content_bytes=len(content_json.encode()),
        content_json=content_json,
    )
    assert len(amplified.file_bytes) > MAX_VIEW_FILE_BYTES

    source_row_id = stock_database.add_message(
        "primary",
        message_id="amplified",
        role="assistant",
        content=content,
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    result = query_session_operation_descriptors(stock_database.path, _request())
    descriptor = _document(result)["messages"][0]
    assert descriptor["omissions"] == ["normalized-content-byte-limit"]
    assert descriptor["stable_file_size"] <= MAX_VIEW_FILE_BYTES

    path = _physical_path(source_row_id)
    exact = query_session_operation_descriptors(
        stock_database.path,
        _request(SessionOperation.EXACT_OBJECT, path),
    )
    payload = json.loads(exact.file_bytes(path))
    assert payload["content"] == [
        {
            "type": "omitted",
            "originalType": "message-content",
            "reason": "normalized-content-byte-limit",
            "sourceBytes": len(content_json.encode()),
        }
    ]

    legacy = project_goose_session(stock_database.path, "primary")
    legacy_payload = json.loads(legacy.files[path])
    assert legacy_payload["omissions"] == ["normalized-content-byte-limit"]


def test_invalid_utf8_text_degrades_stably_before_and_after_archive(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        source_row_id = int(
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, role, content_json,
                     created_timestamp, metadata_json)
                VALUES (CAST(X'FF' AS TEXT), 'primary', 'user',
                        CAST(X'FF5B5D' AS TEXT), 1, ?)
                RETURNING id
                """,
                (canonical_json(visible_metadata()),),
            ).fetchone()[0]
        )
        connection.commit()
    finally:
        connection.close()

    path = _physical_path(source_row_id)
    request = _request(SessionOperation.EXACT_OBJECT, path)
    current = query_session_operation_descriptors(stock_database.path, request)
    current_descriptor = _document(current)["messages"][0]
    assert current_descriptor["message_id_status"] == "invalid"
    assert current_descriptor["omissions"] == ["invalid-content-encoding"]
    legacy = project_goose_session(stock_database.path, "primary")
    legacy_payload = json.loads(legacy.files[path])
    assert legacy_payload["messageId"] is None
    assert legacy_payload["omissions"] == ["invalid-content-encoding"]

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE messages SET metadata_json = ? WHERE id = ?",
            (canonical_json(user_only_metadata()), source_row_id),
        )
        connection.commit()
    finally:
        connection.close()
    archived = query_session_operation_descriptors(stock_database.path, request)
    assert _document(archived)["messages"][0]["context_visibility"] == "ledger-captured"
    assert archived.file_bytes(path) == current.file_bytes(path)


@pytest.mark.parametrize(
    ("message_id", "expected_status"),
    [
        ("oversized", "oversized"),
        (sqlite3.Binary(b"x"), "invalid"),
    ],
)
def test_message_id_status_and_artifact_remain_stable_after_archive(
    stock_database: StockGooseDatabase,
    message_id: object,
    expected_status: str,
) -> None:
    limits = LedgerLimits(max_message_id_bytes=4)
    bootstrap_disclosure_ledger(stock_database.path, "primary", limits=limits)
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        source_row_id = int(
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, role, content_json,
                     created_timestamp, metadata_json)
                VALUES (?, 'primary', 'user', '[]', 1, ?)
                RETURNING id
                """,
                (message_id, canonical_json(visible_metadata())),
            ).fetchone()[0]
        )
        connection.commit()
    finally:
        connection.close()

    path = _physical_path(source_row_id)
    request = _request(SessionOperation.EXACT_OBJECT, path)
    current = query_session_operation_descriptors(stock_database.path, request)
    assert _document(current)["messages"][0]["message_id_status"] == expected_status

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE messages SET metadata_json = ? WHERE id = ?",
            (canonical_json(user_only_metadata()), source_row_id),
        )
        connection.commit()
    finally:
        connection.close()
    archived = query_session_operation_descriptors(stock_database.path, request)
    assert _document(archived)["messages"][0]["message_id_status"] == expected_status
    assert archived.file_bytes(path) == current.file_bytes(path)


def test_oversized_message_id_does_not_consume_the_retained_identity_budget(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="ordinary",
        role="user",
        content=[],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    stock_database.add_message(
        "primary",
        message_id="x" * (4 * 1024 * 1024 + 1),
        role="assistant",
        content=[],
        created_timestamp=2,
        metadata=visible_metadata(),
    )

    document = _document(query_session_operation_descriptors(stock_database.path, _request()))
    assert document["content_window_truncated"] is False
    assert len(document["messages"]) == 2
    assert document["messages"][1]["message_id_status"] == "oversized"


@pytest.mark.parametrize(
    ("message_id", "content_json", "created", "expected_outcome"),
    [
        (sqlite3.Binary(b"binary-id"), "[]", 1, "invalid"),
        ("message", sqlite3.Binary(b"[]"), 1, "ledger-content-unavailable"),
        ("message", "[]", "not-an-integer", None),
    ],
)
def test_dynamic_sqlite_types_are_omitted_or_degraded_without_failing_the_operation(
    stock_database: StockGooseDatabase,
    message_id: object,
    content_json: object,
    created: object,
    expected_outcome: str | None,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO messages
                (message_id, session_id, role, content_json, created_timestamp, metadata_json)
            VALUES (?, 'primary', 'user', ?, ?, ?)
            """,
            (message_id, content_json, created, canonical_json(visible_metadata())),
        )
        connection.commit()
    finally:
        connection.close()

    document = _document(query_session_operation_descriptors(stock_database.path, _request()))
    messages = document["messages"]
    assert isinstance(messages, list)
    if expected_outcome is None:
        assert messages == []
    elif expected_outcome == "invalid":
        assert len(messages) == 1
        assert messages[0]["message_id"] is None
        assert messages[0]["message_id_status"] == expected_outcome
    else:
        assert len(messages) == 1
        assert messages[0]["context_visibility"] == "ledger-captured"
        assert messages[0]["omissions"] == [expected_outcome]


def test_oversized_text_timestamp_never_enters_operation_memory_or_output(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    oversized_timestamp = "8" * (2 * 1024 * 1024)
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO messages
                (message_id, session_id, role, content_json,
                 created_timestamp, metadata_json)
            VALUES ('bad-time', 'primary', 'user', ?, ?, ?)
            """,
            (
                canonical_json([{"type": "text", "text": "TIMESTAMP_ROW_CONTENT"}]),
                oversized_timestamp,
                canonical_json(visible_metadata()),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = query_session_operation_descriptors(stock_database.path, _request())
    document = _document(result)
    assert document["messages"] == []
    assert document["counts"]["source_message_rows"] == 1
    assert document["counts"]["current_eligible_rows"] == 0
    assert "TIMESTAMP_ROW_CONTENT" not in result.descriptor_data.decode()
    assert len(result.descriptor_data) < 4096


def test_oversized_metadata_is_not_parsed_or_accepted_as_eligibility_evidence(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    oversized_metadata = json.dumps({"agentVisible": True, "padding": "M" * (2 * 1024 * 1024)})
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO messages
                (message_id, session_id, role, content_json,
                 created_timestamp, metadata_json)
            VALUES ('bad-metadata', 'primary', 'user', ?, 1, ?)
            """,
            (
                canonical_json([{"type": "text", "text": "OVERSIZED_METADATA_ROW"}]),
                oversized_metadata,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = query_session_operation_descriptors(stock_database.path, _request())
    document = _document(result)
    assert document["messages"] == []
    assert document["counts"] == {
        "source_message_rows": 1,
        "current_eligible_rows": 0,
        "ledger_captured_rows": 0,
        "projectable_rows": 0,
    }
    assert "OVERSIZED_METADATA_ROW" not in result.descriptor_data.decode()
    assert len(result.descriptor_data) < 4096


def test_internal_load_mismatch_and_sqlite_failure_are_bounded_projection_errors(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="one",
        role="user",
        content=[],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    monkeypatch.setattr(
        operation_projection_module,
        "_load_descriptor_rows",
        lambda *_args: ([], []),
    )
    with pytest.raises(ProjectionError, match="load count"):
        query_session_operation_descriptors(stock_database.path, _request())

    def fail_sql(*_args: object) -> NoReturn:
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(operation_projection_module, "_read_counts", fail_sql)
    with pytest.raises(ProjectionError, match="cannot query"):
        query_session_operation_descriptors(stock_database.path, _request())


def test_concurrent_writer_cannot_mix_preflight_and_loaded_generations(
    stock_database: StockGooseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="before",
        role="user",
        content=[{"type": "text", "text": "before"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    original_load = operation_projection_module._load_descriptor_rows
    wrote = False

    def write_then_load(
        connection: sqlite3.Connection,
        session_id: str,
        ledger_status: object,
        preflight: object,
    ) -> object:
        nonlocal wrote
        if not wrote:
            wrote = True
            stock_database.add_message(
                "primary",
                message_id="concurrent",
                role="assistant",
                content=[{"type": "text", "text": "concurrent"}],
                created_timestamp=2,
                metadata=visible_metadata(),
            )
        return original_load(  # type: ignore[arg-type]
            connection,
            session_id,
            ledger_status,
            preflight,
        )

    monkeypatch.setattr(operation_projection_module, "_load_descriptor_rows", write_then_load)
    pinned = query_session_operation_descriptors(stock_database.path, _request())
    monkeypatch.setattr(operation_projection_module, "_load_descriptor_rows", original_load)
    fresh = query_session_operation_descriptors(stock_database.path, _request())

    assert wrote is True
    assert pinned.descriptor_count == 1
    assert _document(pinned)["counts"]["source_message_rows"] == 1
    assert fresh.descriptor_count == 2
    assert _document(fresh)["counts"]["source_message_rows"] == 2


def test_missing_or_altered_ledger_fails_before_descriptor_projection(
    stock_database: StockGooseDatabase,
) -> None:
    with pytest.raises(DisclosureLedgerUnavailable, match="object set mismatch"):
        query_session_operation_descriptors(stock_database.path, _request())

    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        trigger = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger' AND name GLOB 'sandboxed_goose_disclosure_*'
            ORDER BY name LIMIT 1
            """
        ).fetchone()[0]
        connection.execute(f'DROP TRIGGER "{trigger}"')
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DisclosureLedgerUnavailable, match="object set mismatch"):
        query_session_operation_descriptors(stock_database.path, _request())
