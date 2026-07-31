from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from sandboxed_goose.contextfs.goose_session import (
    EVENT_PATH_PREFIX,
    MESSAGE_PATH_PREFIX,
    project_goose_session,
)
from tests.support.stock_goose import (
    StockGooseDatabase,
    agent_only_metadata,
    load_stock_goose_contract,
    normalize_sql,
    tool_request_content,
    tool_response_content,
    user_only_metadata,
    visible_metadata,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stock_database(tmp_path: Path) -> StockGooseDatabase:
    return StockGooseDatabase.create(tmp_path / "sessions.db")


def test_canonical_artifact_records_stock_shapes_without_patch_provenance() -> None:
    contract = load_stock_goose_contract()

    assert contract["artifact_schema_version"] == 1
    assert len(contract["goose_commit"]) == 40
    assert contract["metadata_examples"] == {
        "visible": visible_metadata(),
        "agent_only": agent_only_metadata(),
        "user_only": user_only_metadata(),
        "stock_archived": {"userVisible": True, "agentVisible": False},
    }
    assert "historicallyAgentVisible" not in json.dumps(contract)
    assert contract["content_examples"]["tool_request"] == tool_request_content("tool-000", 0)
    assert contract["content_examples"]["tool_response"] == tool_response_content("tool-000", 0)
    assert contract["tool_pair_summarization"] == {
        "batch_size": 10,
        "control_cutoff": 2,
        "eligible_pairs_for_one_batch": 13,
        "apply_commit_order": [
            "archive_request",
            "archive_response",
            "insert_summary",
        ],
        "summary_role": "user",
        "summary_created_timestamp_source": "tool_response",
    }


def test_fixture_database_has_the_pinned_stock_messages_schema(
    stock_database: StockGooseDatabase,
) -> None:
    contract = load_stock_goose_contract()
    connection = stock_database.connect()
    try:
        actual_columns = [
            [
                row["name"],
                row["type"],
                bool(row["notnull"]),
                row["dflt_value"],
                bool(row["pk"]),
            ]
            for row in connection.execute("PRAGMA table_info(messages)")
        ]
        foreign_keys = connection.execute("PRAGMA foreign_key_list(messages)").fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        sequence_table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
        ).fetchone()
    finally:
        connection.close()

    assert actual_columns == contract["messages_columns"]
    assert [(row["table"], row["from"], row["to"]) for row in foreign_keys] == [
        ("sessions", "session_id", "id")
    ]
    assert journal_mode == "wal"
    assert sequence_table is not None


def test_tool_pair_archival_has_three_separately_observable_commits(
    stock_database: StockGooseDatabase,
) -> None:
    request_row_id, response_row_id = stock_database.add_tool_pair("primary", 1)
    stock_database.add_message(
        "primary",
        message_id="never-disclosed",
        role="user",
        content=[{"type": "text", "text": "NEVER_DISCLOSED_SECRET"}],
        created_timestamp=12,
        metadata=user_only_metadata(),
    )
    original_rows = stock_database.rows("primary")

    stock_database.archive_message("primary", "request-001")
    request_archived = stock_database.rows("primary")
    assert [row.row_id for row in request_archived] == [row.row_id for row in original_rows]
    assert request_archived[0].metadata["agentVisible"] is False
    assert request_archived[1].metadata["agentVisible"] is True
    assert request_archived[0].content_json == original_rows[0].content_json

    stock_database.archive_message("primary", "response-001")
    both_archived = stock_database.rows("primary")
    assert both_archived[0].metadata["agentVisible"] is False
    assert both_archived[1].metadata["agentVisible"] is False
    assert both_archived[1].content_json == original_rows[1].content_json
    assert all("historicallyAgentVisible" not in row.metadata for row in both_archived)

    summary_row_id = stock_database.add_summary(
        "primary",
        summary_id="summary-001",
        tool_id="tool-001",
        response_created_timestamp=11,
    )
    complete = stock_database.rows("primary")
    assert [row.row_id for row in complete[:2]] == [request_row_id, response_row_id]
    assert summary_row_id > complete[-2].row_id
    summary = complete[-1]
    assert summary.role == "user"
    assert summary.created_timestamp == 11
    assert summary.metadata == agent_only_metadata()
    assert summary.content == [{"type": "text", "text": "summary for tool-001"}]
    assert complete[2].metadata == user_only_metadata()


def test_stock_duplicate_message_id_update_reads_one_and_updates_every_match(
    stock_database: StockGooseDatabase,
) -> None:
    first_id = stock_database.add_message(
        "primary",
        message_id="duplicate",
        role="assistant",
        content=[{"type": "text", "text": "first"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    second_id = stock_database.add_message(
        "primary",
        message_id="duplicate",
        role="assistant",
        content=[{"type": "text", "text": "second"}],
        created_timestamp=2,
        metadata=agent_only_metadata(),
    )

    stock_database.archive_message("primary", "duplicate")
    rows = stock_database.rows("primary")

    assert [row.row_id for row in rows] == [first_id, second_id]
    assert [row.content[0]["text"] for row in rows] == ["first", "second"]
    assert [row.metadata for row in rows] == [
        {"userVisible": True, "agentVisible": False},
        {"userVisible": True, "agentVisible": False},
    ]


def test_summary_uses_response_timestamp_but_a_new_physical_row(
    stock_database: StockGooseDatabase,
) -> None:
    stock_database.add_tool_pair("primary", 1)
    later_id = stock_database.add_message(
        "primary",
        message_id="later",
        role="assistant",
        content=[{"type": "text", "text": "later"}],
        created_timestamp=1000,
        metadata=visible_metadata(),
    )
    summary_id = stock_database.add_summary(
        "primary",
        summary_id="summary-001",
        tool_id="tool-001",
        response_created_timestamp=11,
    )

    rows_by_physical_id = stock_database.rows("primary")
    connection = stock_database.connect()
    try:
        rows_by_goose_order = connection.execute(
            """
            SELECT id FROM messages WHERE session_id = 'primary'
            ORDER BY created_timestamp, id
            """
        ).fetchall()
    finally:
        connection.close()

    assert summary_id == rows_by_physical_id[-1].row_id
    assert summary_id > later_id
    assert [row[0] for row in rows_by_goose_order] == [
        rows_by_physical_id[0].row_id,
        rows_by_physical_id[1].row_id,
        summary_id,
        later_id,
    ]


def test_replace_conversation_is_atomic_for_wal_readers(
    stock_database: StockGooseDatabase,
) -> None:
    stock_database.add_message(
        "primary",
        message_id="old",
        role="user",
        content=[{"type": "text", "text": "old generation"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    old_reader = stock_database.connect()
    old_reader.execute("BEGIN")
    before = old_reader.execute(
        "SELECT message_id FROM messages WHERE session_id = 'primary' ORDER BY id"
    ).fetchall()

    stock_database.replace_conversation(
        "primary",
        [
            (
                "new",
                "user",
                [{"type": "text", "text": "new generation"}],
                2,
                visible_metadata(),
            )
        ],
    )
    still_before = old_reader.execute(
        "SELECT message_id FROM messages WHERE session_id = 'primary' ORDER BY id"
    ).fetchall()
    old_reader.rollback()
    old_reader.close()

    new_reader = stock_database.connect()
    try:
        after = new_reader.execute(
            "SELECT message_id FROM messages WHERE session_id = 'primary' ORDER BY id"
        ).fetchall()
    finally:
        new_reader.close()

    assert [row[0] for row in before] == ["old"]
    assert [row[0] for row in still_before] == ["old"]
    assert [row[0] for row in after] == ["new"]


def test_unmanaged_stock_invisible_rows_remain_fail_closed(
    stock_database: StockGooseDatabase,
) -> None:
    stock_database.add_tool_pair("primary", 1)
    stock_database.add_message(
        "primary",
        message_id="secret",
        role="user",
        content=[{"type": "text", "text": "UNPROVEN_INVISIBLE_SECRET"}],
        created_timestamp=12,
        metadata=user_only_metadata(),
    )
    stock_database.archive_message("primary", "request-001")
    stock_database.archive_message("primary", "response-001")
    stock_database.add_summary(
        "primary",
        summary_id="summary-001",
        tool_id="tool-001",
        response_created_timestamp=11,
    )

    projection = project_goose_session(stock_database.path, "primary")
    projected = b"\n".join(projection.files.values()).decode("utf-8")
    manifest = json.loads(projection.files["manifest.json"])

    assert "summary for tool-001" in projected
    assert "UNPROVEN_INVISIBLE_SECRET" not in projected
    assert manifest["current_agent_visible_rows"] == 1
    assert manifest["historical_agent_visible_rows"] == 0
    assert manifest["projected_messages"] == 1


@pytest.mark.parametrize(
    ("source_rows", "projected_rows", "truncated"),
    [(255, 255, False), (256, 256, False), (257, 256, True)],
)
def test_recent_tree_message_boundary_is_a_contiguous_newest_suffix(
    stock_database: StockGooseDatabase,
    source_rows: int,
    projected_rows: int,
    truncated: bool,
) -> None:
    for ordinal in range(1, source_rows + 1):
        stock_database.add_message(
            "primary",
            message_id=f"boundary-{ordinal:03d}",
            role="user",
            content=[{"type": "text", "text": f"BOUNDARY_MESSAGE_{ordinal:03d}"}],
            created_timestamp=1,
            metadata=visible_metadata(),
        )

    projection = project_goose_session(stock_database.path, "primary")
    manifest = json.loads(projection.files["manifest.json"])
    message_paths = sorted(
        path for path in projection.files if path.startswith(f"{MESSAGE_PATH_PREFIX}/")
    )
    projected = b"\n".join(projection.files[path] for path in message_paths).decode("utf-8")

    assert manifest["source_message_rows"] == source_rows
    assert manifest["projected_messages"] == projected_rows
    assert manifest["truncated"] is truncated
    assert len(message_paths) == projected_rows
    assert f"BOUNDARY_MESSAGE_{source_rows:03d}" in projected
    first_included = source_rows - projected_rows + 1
    assert f"BOUNDARY_MESSAGE_{first_included:03d}" in projected
    if first_included > 1:
        assert f"BOUNDARY_MESSAGE_{first_included - 1:03d}" not in projected


@pytest.mark.parametrize(
    ("source_events", "projected_events", "omitted_events", "truncated"),
    [(699, 699, 0, False), (700, 700, 0, False), (701, 700, 1, True)],
)
def test_recent_tree_event_file_boundary_is_explicit(
    stock_database: StockGooseDatabase,
    source_events: int,
    projected_events: int,
    omitted_events: int,
    truncated: bool,
) -> None:
    events_remaining = source_events
    message_ordinal = 0
    while events_remaining:
        message_ordinal += 1
        event_count = min(events_remaining, 250)
        stock_database.add_message(
            "primary",
            message_id=f"events-{message_ordinal}",
            role="user",
            content=[
                {"type": "text", "text": f"event-{message_ordinal}-{event_ordinal}"}
                for event_ordinal in range(event_count)
            ],
            created_timestamp=message_ordinal,
            metadata=visible_metadata(),
        )
        events_remaining -= event_count

    projection = project_goose_session(stock_database.path, "primary")
    manifest = json.loads(projection.files["manifest.json"])
    event_paths = [path for path in projection.files if path.startswith(f"{EVENT_PATH_PREFIX}/")]

    assert len(event_paths) == projected_events
    assert manifest["projected_events"] == projected_events
    assert manifest["omitted_event_files"] == omitted_events
    assert manifest["truncated"] is truncated


def _source_text(source: Path, relative_path: str) -> str:
    git_result = subprocess.run(
        ["git", "-C", str(source), "show", f"HEAD:{relative_path}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if git_result.returncode == 0:
        return git_result.stdout
    return (source / relative_path).read_text(encoding="utf-8")


def _optional_goose_source() -> Path | None:
    configured = os.environ.get("GOOSE_SOURCE_DIR")
    candidate = Path(configured) if configured else PROJECT_ROOT / "goose-dev"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def test_canonical_artifact_conforms_to_pinned_stock_goose_source() -> None:
    source = _optional_goose_source()
    if source is None:
        pytest.skip("set GOOSE_SOURCE_DIR to check the optional upstream source contract")
    contract = load_stock_goose_contract()

    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if revision.returncode != 0:
        pytest.skip("GOOSE_SOURCE_DIR is not a Git checkout")
    assert revision.stdout.strip() == contract["goose_commit"]

    manager_source = _source_text(source, "crates/goose/src/session/session_manager.rs")
    ddl_match = re.search(
        r'CREATE TABLE IF NOT EXISTS messages \((.*?)\)\s*"#',
        manager_source,
        flags=re.DOTALL,
    )
    assert ddl_match is not None
    source_ddl = f"CREATE TABLE IF NOT EXISTS messages ({ddl_match.group(1)})"
    assert normalize_sql(source_ddl) == normalize_sql(contract["messages_table_sql"])

    metadata_source = _source_text(
        source, "crates/goose-provider-types/src/conversation/message.rs"
    )
    metadata_struct = re.search(
        r"pub struct MessageMetadata \{(.*?)\n\}", metadata_source, flags=re.DOTALL
    )
    assert metadata_struct is not None
    assert "historically_agent_visible" not in metadata_struct.group(1)

    agent_source = _source_text(source, "crates/goose/src/agents/agent.rs")
    assert "metadata.with_agent_invisible()" in agent_source
    assert "metadata.with_agent_archived()" not in agent_source
