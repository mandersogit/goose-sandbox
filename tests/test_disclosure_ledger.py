from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sandboxed_goose.contextfs.disclosure_ledger import (
    ACCOUNTING_TABLE,
    DEFAULT_MAX_CONTENT_BYTES,
    DEFAULT_MAX_LEDGER_BYTES,
    DEFAULT_MAX_LEDGER_ENTRIES,
    DEFAULT_MAX_MESSAGE_ID_BYTES,
    DEFAULT_MAX_METADATA_BYTES,
    DEFAULT_MAX_ROLE_BYTES,
    ENTRY_TABLE,
    LEDGER_SCHEMA_VERSION,
    OBJECT_PREFIX,
    OMITTED_CONTENT,
    OMITTED_CREATED_TIMESTAMP,
    OMITTED_MESSAGE_ID,
    OMITTED_METADATA,
    OMITTED_ROLE,
    SCHEMA_FINGERPRINT,
    DisclosureLedgerUnavailable,
    LedgerLimits,
    bootstrap_disclosure_ledger,
    open_verified_disclosure_snapshot,
    verify_disclosure_ledger,
)
from sandboxed_goose.contextfs.goose_session import CURRENT_MESSAGE_SQL
from tests.support.stock_goose import (
    StockGooseDatabase,
    agent_only_metadata,
    canonical_json,
    user_only_metadata,
    visible_metadata,
)


@pytest.fixture
def stock_database(tmp_path: Path) -> StockGooseDatabase:
    return StockGooseDatabase.create(tmp_path / "sessions.db")


def _ledger_rows(database: StockGooseDatabase, session_id: str) -> list[sqlite3.Row]:
    connection = database.connect()
    try:
        return connection.execute(
            f"SELECT * FROM {ENTRY_TABLE} WHERE session_id = ? ORDER BY source_row_id",
            (session_id,),
        ).fetchall()
    finally:
        connection.close()


def test_verified_snapshot_owns_one_read_transaction_and_always_closes(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    captured: sqlite3.Connection | None = None
    with open_verified_disclosure_snapshot(stock_database.path, "primary") as (
        connection,
        status,
    ):
        captured = connection
        assert connection.in_transaction is True
        assert status.session_id == "primary"
        assert (
            connection.execute(
                "SELECT count(*) FROM messages WHERE session_id = ?", ("primary",)
            ).fetchone()[0]
            == 0
        )

    assert captured is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured.execute("SELECT 1")

    failed: sqlite3.Connection | None = None
    with (
        pytest.raises(RuntimeError, match="consumer failure"),
        open_verified_disclosure_snapshot(stock_database.path, "primary") as (
            connection,
            _status,
        ),
    ):
        failed = connection
        raise RuntimeError("consumer failure")
    assert failed is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")


def test_canonical_ledger_artifact_pins_schema_objects_limits_and_flags(
    stock_database: StockGooseDatabase,
) -> None:
    artifact_path = Path(__file__).parent / "fixtures" / "disclosure-ledger-v2.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        object_names = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name GLOB ?", (f"{OBJECT_PREFIX}*",)
            )
        )
    finally:
        connection.close()

    assert artifact["artifact_schema_version"] == 2
    assert artifact["ledger_schema_version"] == LEDGER_SCHEMA_VERSION
    assert artifact["schema_fingerprint"] == SCHEMA_FINGERPRINT
    assert artifact["object_names"] == object_names
    assert artifact["default_limits"] == {
        "max_entries": DEFAULT_MAX_LEDGER_ENTRIES,
        "max_stored_bytes": DEFAULT_MAX_LEDGER_BYTES,
        "max_content_bytes": DEFAULT_MAX_CONTENT_BYTES,
        "max_metadata_bytes": DEFAULT_MAX_METADATA_BYTES,
        "max_message_id_bytes": DEFAULT_MAX_MESSAGE_ID_BYTES,
        "max_role_bytes": DEFAULT_MAX_ROLE_BYTES,
    }
    assert artifact["omission_flags"] == {
        "message_id": OMITTED_MESSAGE_ID,
        "role": OMITTED_ROLE,
        "content": OMITTED_CONTENT,
        "metadata": OMITTED_METADATA,
        "created_timestamp": OMITTED_CREATED_TIMESTAMP,
    }


def test_bootstrap_seeds_only_currently_visible_bound_session_rows(
    stock_database: StockGooseDatabase,
) -> None:
    first_id = stock_database.add_message(
        "primary",
        message_id="visible",
        role="user",
        content=[{"type": "text", "text": "CURRENT_VISIBLE"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    second_id = stock_database.add_message(
        "primary",
        message_id="agent-only",
        role="assistant",
        content=[{"type": "text", "text": "CURRENT_AGENT_ONLY"}],
        created_timestamp=2,
        metadata=agent_only_metadata(),
    )
    stock_database.add_message(
        "primary",
        message_id="secret",
        role="user",
        content=[{"type": "text", "text": "PREEXISTING_INVISIBLE_SECRET"}],
        created_timestamp=3,
        metadata=user_only_metadata(),
    )
    stock_database.add_message(
        "decoy",
        message_id="decoy",
        role="user",
        content=[{"type": "text", "text": "OTHER_SESSION_SECRET"}],
        created_timestamp=4,
        metadata=visible_metadata(),
    )

    status = bootstrap_disclosure_ledger(stock_database.path, "primary")
    rows = _ledger_rows(stock_database, "primary")

    assert status.coverage_epoch == 1
    assert status.coverage_complete is False
    assert status.coverage_reason == "preexisting-ambiguous-rows"
    assert status.ambiguous_rows_at_bootstrap == 1
    assert status.ledger_entries == 2
    assert [row["source_row_id"] for row in rows] == [first_id, second_id]
    assert {row["capture_reason"] for row in rows} == {"bootstrap"}
    serialized = json.dumps([dict(row) for row in rows])
    assert "CURRENT_VISIBLE" in serialized
    assert "CURRENT_AGENT_ONLY" in serialized
    assert "PREEXISTING_INVISIBLE_SECRET" not in serialized
    assert "OTHER_SESSION_SECRET" not in serialized

    assert bootstrap_disclosure_ledger(stock_database.path, "primary") == status
    assert verify_disclosure_ledger(stock_database.path, "primary") == status


def test_only_json_boolean_true_is_eligible_and_current_queries_use_owned_indexes(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        values: list[object] = [True, 1, 1.0, "1", None, [], {}]
        for ordinal, value in enumerate(values, start=1):
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, role, content_json,
                     created_timestamp, metadata_json)
                VALUES (?, 'primary', 'user', '[]', ?, ?)
                """,
                (
                    f"visibility-{ordinal}",
                    ordinal,
                    canonical_json({"agentVisible": value}),
                ),
            )
        connection.commit()

        captured = connection.execute(
            f"SELECT message_id FROM {ENTRY_TABLE} WHERE session_id = 'primary'"
        ).fetchall()
        assert [row[0] for row in captured] == ["visibility-1"]
        eligible = connection.execute(
            f"SELECT message_id FROM messages WHERE session_id = ? AND {CURRENT_MESSAGE_SQL}",
            ("primary",),
        ).fetchall()
        assert [row[0] for row in eligible] == ["visibility-1"]

        plan = connection.execute(
            f"""
            EXPLAIN QUERY PLAN
            SELECT id FROM messages
            WHERE session_id = ? AND {CURRENT_MESSAGE_SQL}
            ORDER BY created_timestamp DESC, id DESC
            LIMIT 257
            """,
            ("primary",),
        ).fetchall()
        assert any(
            "sandboxed_goose_disclosure_current_messages" in str(row[3]) for row in plan
        )
    finally:
        connection.close()


def test_bootstrap_reports_a_bounded_ambiguous_row_lower_bound(
    stock_database: StockGooseDatabase,
) -> None:
    for ordinal in range(3):
        stock_database.add_message(
            "primary",
            message_id=f"hidden-{ordinal}",
            role="user",
            content=[],
            created_timestamp=ordinal,
            metadata=user_only_metadata(),
        )

    status = bootstrap_disclosure_ledger(
        stock_database.path,
        "primary",
        limits=LedgerLimits(max_entries=2),
    )

    assert status.ambiguous_rows_at_bootstrap == 3
    assert status.ambiguous_rows_at_bootstrap_is_lower_bound is True
    assert status.coverage_complete is False
    assert status.coverage_reason == "preexisting-ambiguous-rows"
    assert status.capture_enabled is True


def test_persistent_triggers_capture_stock_insert_update_and_archive(
    stock_database: StockGooseDatabase,
) -> None:
    initial = bootstrap_disclosure_ledger(stock_database.path, "primary")
    assert initial.coverage_complete is True
    assert initial.ledger_entries == 0

    request_id, response_id = stock_database.add_tool_pair("primary", 1)
    stock_database.add_message(
        "primary",
        message_id="never-visible",
        role="user",
        content=[{"type": "text", "text": "NEVER_VISIBLE"}],
        created_timestamp=12,
        metadata=user_only_metadata(),
    )
    stock_database.add_message(
        "decoy",
        message_id="decoy-after-bootstrap",
        role="user",
        content=[{"type": "text", "text": "DECOY_AFTER_BOOTSTRAP"}],
        created_timestamp=13,
        metadata=visible_metadata(),
    )
    stock_database.archive_message("primary", "request-001")
    stock_database.archive_message("primary", "response-001")
    summary_id = stock_database.add_summary(
        "primary",
        summary_id="summary-001",
        tool_id="tool-001",
        response_created_timestamp=11,
    )

    rows = _ledger_rows(stock_database, "primary")
    status = verify_disclosure_ledger(stock_database.path, "primary")

    assert status.coverage_complete is True
    assert status.ledger_entries == 3
    assert [row["source_row_id"] for row in rows] == [request_id, response_id, summary_id]
    assert [row["capture_reason"] for row in rows] == [
        "pre-archive",
        "pre-archive",
        "visible-insert",
    ]
    serialized = json.dumps([dict(row) for row in rows])
    assert "NEVER_VISIBLE" not in serialized
    assert "DECOY_AFTER_BOOTSTRAP" not in serialized
    assert all(json.loads(row["metadata_json"])["agentVisible"] is True for row in rows)


def test_one_static_trigger_set_supports_multiple_explicitly_managed_sessions(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    bootstrap_disclosure_ledger(stock_database.path, "decoy")
    primary_id = stock_database.add_message(
        "primary",
        message_id="primary-visible",
        role="user",
        content=[{"type": "text", "text": "PRIMARY"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    decoy_id = stock_database.add_message(
        "decoy",
        message_id="decoy-invisible",
        role="user",
        content=[{"type": "text", "text": "NOT_YET_VISIBLE"}],
        created_timestamp=2,
        metadata=user_only_metadata(),
    )

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE messages SET metadata_json = ? WHERE id = ?",
            (canonical_json(visible_metadata()), decoy_id),
        )
        connection.commit()
    finally:
        connection.close()

    primary_rows = _ledger_rows(stock_database, "primary")
    decoy_rows = _ledger_rows(stock_database, "decoy")
    assert [row["source_row_id"] for row in primary_rows] == [primary_id]
    assert [row["source_row_id"] for row in decoy_rows] == [decoy_id]
    assert decoy_rows[0]["capture_reason"] == "visible-update"
    assert verify_disclosure_ledger(stock_database.path, "primary").ledger_entries == 1
    assert verify_disclosure_ledger(stock_database.path, "decoy").ledger_entries == 1


def test_visible_content_updates_refresh_the_same_physical_ledger_entry(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    row_id = stock_database.add_message(
        "primary",
        message_id="mutable",
        role="assistant",
        content=[{"type": "text", "text": "BEFORE"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    before = verify_disclosure_ledger(stock_database.path, "primary")

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE messages SET content_json = ? WHERE id = ?",
            (
                canonical_json([{"type": "text", "text": "AFTER", "_meta": {"private": True}}]),
                row_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    after = verify_disclosure_ledger(stock_database.path, "primary")
    rows = _ledger_rows(stock_database, "primary")
    assert len(rows) == 1
    assert rows[0]["capture_reason"] == "visible-update"
    assert "AFTER" in rows[0]["content_json"]
    assert after.ledger_entries == before.ledger_entries == 1
    assert after.stored_bytes > before.stored_bytes


def test_runtime_entry_quota_degrades_capture_without_blocking_goose(
    stock_database: StockGooseDatabase,
) -> None:
    limits = LedgerLimits(max_entries=1)
    bootstrap_disclosure_ledger(stock_database.path, "primary", limits=limits)
    stock_database.add_message(
        "primary",
        message_id="first",
        role="user",
        content=[{"type": "text", "text": "FIRST"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )

    stock_database.add_message(
        "primary",
        message_id="second",
        role="user",
        content=[{"type": "text", "text": "SECOND"}],
        created_timestamp=2,
        metadata=visible_metadata(),
    )

    assert [row.message_id for row in stock_database.rows("primary")] == ["first", "second"]
    degraded = verify_disclosure_ledger(stock_database.path, "primary")
    assert degraded.capture_enabled is False
    assert degraded.coverage_complete is False
    assert degraded.coverage_reason == "capture-overflow"
    assert degraded.coverage_epoch == 2
    assert degraded.ledger_entries == 1
    stock_database.archive_message("primary", "first")
    assert stock_database.rows("primary")[0].metadata["agentVisible"] is False
    assert verify_disclosure_ledger(stock_database.path, "primary") == degraded
    assert _ledger_rows(stock_database, "primary")[0]["capture_reason"] == "visible-insert"


def test_runtime_byte_quota_degrades_capture_without_blocking_visible_update(
    stock_database: StockGooseDatabase,
) -> None:
    limits = LedgerLimits(max_stored_bytes=128, max_content_bytes=128)
    bootstrap_disclosure_ledger(stock_database.path, "primary", limits=limits)
    row_id = stock_database.add_message(
        "primary",
        message_id="bounded",
        role="user",
        content=[{"type": "text", "text": "small"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    before = verify_disclosure_ledger(stock_database.path, "primary")

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE messages SET content_json = ? WHERE id = ?",
            (canonical_json([{"type": "text", "text": "x" * 90}]), row_id),
        )
        connection.commit()
    finally:
        connection.close()

    source = stock_database.rows("primary")[0]
    ledger = _ledger_rows(stock_database, "primary")[0]
    assert source.content == [{"type": "text", "text": "x" * 90}]
    assert json.loads(ledger["content_json"]) == [{"type": "text", "text": "small"}]
    degraded = verify_disclosure_ledger(stock_database.path, "primary")
    assert degraded.capture_enabled is False
    assert degraded.coverage_reason == "capture-overflow"
    assert degraded.coverage_epoch == before.coverage_epoch + 1


def test_oversized_content_creates_a_bounded_omission_record(
    stock_database: StockGooseDatabase,
) -> None:
    limits = LedgerLimits(max_stored_bytes=256, max_content_bytes=32)
    bootstrap_disclosure_ledger(stock_database.path, "primary", limits=limits)
    stock_database.add_message(
        "primary",
        message_id="oversized",
        role="user",
        content=[{"type": "text", "text": "SENSITIVE" * 100}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )

    row = _ledger_rows(stock_database, "primary")[0]
    status = verify_disclosure_ledger(stock_database.path, "primary")
    assert row["content_json"] is None
    assert row["source_content_bytes"] > limits.max_content_bytes
    assert row["omission_flags"] & OMITTED_CONTENT
    assert status.omitted_entries == 1
    assert status.stored_bytes <= limits.max_stored_bytes


def test_invalid_created_timestamp_is_not_copied_into_the_ledger(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    oversized_timestamp = "9" * (2 * 1024 * 1024)
    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO messages
                (message_id, session_id, role, content_json,
                 created_timestamp, metadata_json)
            VALUES ('bad-time', 'primary', 'user', '[]', ?, ?)
            """,
            (oversized_timestamp, canonical_json(visible_metadata())),
        )
        connection.commit()
    finally:
        connection.close()

    ledger = _ledger_rows(stock_database, "primary")[0]
    status = verify_disclosure_ledger(stock_database.path, "primary")
    assert ledger["created_timestamp"] is None
    assert ledger["omission_flags"] & OMITTED_CREATED_TIMESTAMP
    assert status.omitted_entries == 1
    assert status.stored_bytes < 1024


def test_bootstrap_overflow_commits_a_bounded_degraded_ledger(
    stock_database: StockGooseDatabase,
) -> None:
    for ordinal in range(2):
        stock_database.add_message(
            "primary",
            message_id=f"visible-{ordinal}",
            role="user",
            content=[{"type": "text", "text": str(ordinal)}],
            created_timestamp=ordinal,
            metadata=visible_metadata(),
        )

    status = bootstrap_disclosure_ledger(
        stock_database.path,
        "primary",
        limits=LedgerLimits(max_entries=1),
    )

    connection = stock_database.connect()
    try:
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name GLOB ?", (f"{OBJECT_PREFIX}*",)
        ).fetchall()
    finally:
        connection.close()
    assert objects
    assert status.capture_enabled is False
    assert status.coverage_complete is False
    assert status.coverage_reason == "capture-overflow"
    assert status.coverage_epoch == 2
    assert status.ledger_entries == 1


def test_message_deletion_advances_and_transactionally_rolls_back_coverage_epoch(
    stock_database: StockGooseDatabase,
) -> None:
    first_id = stock_database.add_message(
        "primary",
        message_id="first",
        role="user",
        content=[{"type": "text", "text": "first"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    stock_database.add_message(
        "primary",
        message_id="second",
        role="assistant",
        content=[{"type": "text", "text": "second"}],
        created_timestamp=2,
        metadata=visible_metadata(),
    )
    initial = bootstrap_disclosure_ledger(stock_database.path, "primary")

    connection = stock_database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM messages WHERE id = ?", (first_id,))
        assert (
            connection.execute(
                "SELECT coverage_epoch FROM sandboxed_goose_disclosure_managed_sessions "
                "WHERE session_id = 'primary'"
            ).fetchone()[0]
            == 2
        )
        connection.rollback()
    finally:
        connection.close()
    assert verify_disclosure_ledger(stock_database.path, "primary") == initial

    connection = stock_database.connect()
    try:
        connection.execute("DELETE FROM messages WHERE id = ?", (first_id,))
        connection.commit()
    finally:
        connection.close()
    deleted = verify_disclosure_ledger(stock_database.path, "primary")
    assert deleted.coverage_epoch == 2
    assert deleted.coverage_complete is False
    assert deleted.coverage_reason == "message-delete"
    assert deleted.deletion_events == 1

    new_id = stock_database.add_message(
        "primary",
        message_id="third",
        role="user",
        content=[{"type": "text", "text": "third"}],
        created_timestamp=3,
        metadata=visible_metadata(),
    )
    rows = _ledger_rows(stock_database, "primary")
    assert [(row["source_row_id"], row["coverage_epoch"]) for row in rows] == [
        (first_id, 1),
        (first_id + 1, 1),
        (new_id, 2),
    ]


def test_stock_session_deletion_is_not_blocked_and_reuse_remains_fail_closed(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM sessions WHERE id = 'primary'")
        connection.commit()
        managed = connection.execute(
            """
            SELECT coverage_epoch, coverage_complete, coverage_reason, deletion_events
            FROM sandboxed_goose_disclosure_managed_sessions
            WHERE session_id = 'primary'
            """
        ).fetchone()
    finally:
        connection.close()

    assert tuple(managed) == (2, 0, "session-delete", 1)
    with pytest.raises(DisclosureLedgerUnavailable, match="bound Goose session does not exist"):
        verify_disclosure_ledger(stock_database.path, "primary")

    connection = stock_database.connect()
    try:
        connection.execute("INSERT INTO sessions (id, name) VALUES ('primary', 'replacement')")
        connection.commit()
    finally:
        connection.close()
    status = bootstrap_disclosure_ledger(stock_database.path, "primary")
    assert status.coverage_epoch == 2
    assert status.coverage_complete is False
    assert status.coverage_reason == "session-delete"


def test_verification_rejects_missing_objects_and_accounting_tampering(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    connection = stock_database.connect()
    try:
        connection.execute("DROP TRIGGER sandboxed_goose_disclosure_pre_archive")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DisclosureLedgerUnavailable, match="object set mismatch"):
        verify_disclosure_ledger(stock_database.path, "primary")
    with pytest.raises(DisclosureLedgerUnavailable, match="object set mismatch"):
        bootstrap_disclosure_ledger(stock_database.path, "primary")

    other = StockGooseDatabase.create(stock_database.path.parent / "accounting.db")
    bootstrap_disclosure_ledger(other.path, "primary")
    connection = other.connect()
    try:
        connection.execute(
            f"UPDATE {ACCOUNTING_TABLE} SET entry_count = 1 WHERE session_id = 'primary'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DisclosureLedgerUnavailable, match="accounting mismatch"):
        verify_disclosure_ledger(other.path, "primary")

    altered = StockGooseDatabase.create(stock_database.path.parent / "altered.db")
    bootstrap_disclosure_ledger(altered.path, "primary")
    connection = altered.connect()
    try:
        connection.executescript(
            """
            DROP TRIGGER sandboxed_goose_disclosure_pre_archive;
            CREATE TRIGGER sandboxed_goose_disclosure_pre_archive
            AFTER INSERT ON messages BEGIN SELECT 1; END;
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DisclosureLedgerUnavailable, match="object was altered"):
        verify_disclosure_ledger(altered.path, "primary")


def test_missing_accounting_degrades_capture_without_blocking_archival(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="must-remain-visible",
        role="assistant",
        content=[{"type": "text", "text": "LAST_DISCLOSED_FORM"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    connection = stock_database.connect()
    try:
        connection.execute(f"DELETE FROM {ACCOUNTING_TABLE} WHERE session_id = 'primary'")
        connection.commit()
    finally:
        connection.close()

    stock_database.archive_message("primary", "must-remain-visible")

    source = stock_database.rows("primary")[0]
    assert source.metadata["agentVisible"] is False
    assert source.content == [{"type": "text", "text": "LAST_DISCLOSED_FORM"}]
    connection = stock_database.connect()
    try:
        managed = connection.execute(
            """
            SELECT coverage_epoch, coverage_complete, coverage_reason
            FROM sandboxed_goose_disclosure_managed_sessions
            WHERE session_id = 'primary'
            """
        ).fetchone()
    finally:
        connection.close()
    assert tuple(managed) == (2, 0, "capture-unavailable")
    with pytest.raises(DisclosureLedgerUnavailable, match="bound Goose session"):
        verify_disclosure_ledger(stock_database.path, "primary")


def test_existing_session_limits_cannot_be_silently_reconfigured(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(
        stock_database.path,
        "primary",
        limits=LedgerLimits(max_entries=10),
    )

    with pytest.raises(DisclosureLedgerUnavailable, match="limits do not match"):
        bootstrap_disclosure_ledger(
            stock_database.path,
            "primary",
            limits=LedgerLimits(max_entries=11),
        )


def test_ledger_entries_cannot_be_deleted(
    stock_database: StockGooseDatabase,
) -> None:
    bootstrap_disclosure_ledger(stock_database.path, "primary")
    stock_database.add_message(
        "primary",
        message_id="retained",
        role="user",
        content=[{"type": "text", "text": "retained"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )

    connection = stock_database.connect()
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="sandboxed_goose_ledger_entry_delete_forbidden",
        ):
            connection.execute(f"DELETE FROM {ENTRY_TABLE} WHERE session_id = 'primary'")
        connection.rollback()
    finally:
        connection.close()
    assert verify_disclosure_ledger(stock_database.path, "primary").ledger_entries == 1
