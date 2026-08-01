from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import sandboxed_goose.contextfs.goose_session as goose_session_module
import sandboxed_goose.tools.session_context as session_context_module
from sandboxed_goose.config import (
    APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    Settings,
)
from sandboxed_goose.contextfs.bundle import decode_bundle, encode_bundle, write_bundle
from sandboxed_goose.contextfs.disclosure_ledger import bootstrap_disclosure_ledger
from sandboxed_goose.contextfs.goose_session import (
    EVENT_PATH_PREFIX,
    MESSAGE_PATH_PREFIX,
    SessionProjection,
    project_goose_session,
    render_projection_path,
)
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.contextfs.view_store import SessionViewStore
from sandboxed_goose.fastmcp.server import build_server as build_fastmcp_server
from sandboxed_goose.mcp_sdk.server import build_server as build_mcp_sdk_server
from sandboxed_goose.session_binding import GOOSE_SESSION_META_KEY
from sandboxed_goose.tools.session_context import render_session_context
from tests.support.stock_goose import StockGooseDatabase, visible_metadata

ServerBuilder = Callable[[Settings | None], Any]
SERVER_BUILDERS = [
    pytest.param(build_mcp_sdk_server, id="mcp-sdk"),
    pytest.param(build_fastmcp_server, id="fastmcp"),
]


@pytest.fixture
def goose_database(tmp_path: Path) -> Path:
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL,
            metadata_json TEXT
        );
        """
    )
    connection.executemany("INSERT INTO sessions (id) VALUES (?)", [("current",), ("other",)])

    visible = json.dumps({"userVisible": True, "agentVisible": True})
    agent_only = json.dumps({"userVisible": False, "agentVisible": True})
    user_only = json.dumps({"userVisible": True, "agentVisible": False})
    historical = json.dumps(
        {
            "userVisible": True,
            "agentVisible": False,
            "historicallyAgentVisible": True,
        }
    )
    rows = [
        (
            "m0",
            "current",
            "user",
            json.dumps([{"type": "text", "text": "PRECOMPACTION_HISTORY_MARKER"}]),
            0,
            historical,
        ),
        (
            "m1",
            "current",
            "user",
            json.dumps(
                [
                    {"type": "text", "text": "VISIBLE_USER_MARKER"},
                    {
                        "type": "text",
                        "text": "ANNOTATED_USER_ONLY_SECRET",
                        "annotations": {"audience": ["user"]},
                    },
                ]
            ),
            1,
            visible,
        ),
        (
            "m2",
            "current",
            "assistant",
            json.dumps(
                [
                    {
                        "type": "toolRequest",
                        "id": "tool-1",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "calculate",
                                "arguments": {"expression": "6*7"},
                                "providerInternal": "TOOL_CALL_INTERNAL_SECRET",
                            },
                        },
                        "metadata": {"providerSecret": "PROVIDER_METADATA_SECRET"},
                        "_meta": {"private": "REQUEST_META_SECRET"},
                    }
                ]
            ),
            2,
            visible,
        ),
        (
            "m3",
            "current",
            "user",
            json.dumps(
                [
                    {
                        "type": "toolResponse",
                        "id": "tool-1",
                        "toolResult": {
                            "status": "success",
                            "value": {
                                "content": [
                                    {"type": "text", "text": "VISIBLE_TOOL_RESULT"},
                                    {
                                        "type": "text",
                                        "text": "NESTED_USER_ONLY_SECRET",
                                        "annotations": {"audience": ["user"]},
                                    },
                                ],
                                "structuredContent": {"secret": "STRUCTURED_SECRET"},
                                "_meta": {"secret": "TOOL_RESULT_META_SECRET"},
                                "providerInternal": "TOOL_RESULT_INTERNAL_SECRET",
                            },
                        },
                    }
                ]
            ),
            3,
            visible,
        ),
        (
            "m7",
            "current",
            "system",
            json.dumps([{"type": "text", "text": "SYSTEM_ROW_SECRET"}]),
            7,
            visible,
        ),
        (
            "m4",
            "current",
            "assistant",
            json.dumps([{"type": "text", "text": "COMPACTION_SUMMARY_MARKER"}]),
            4,
            agent_only,
        ),
        (
            "m5",
            "current",
            "user",
            json.dumps([{"type": "text", "text": "USER_ONLY_ROW_SECRET"}]),
            5,
            user_only,
        ),
        (
            "m6",
            "current",
            "assistant",
            json.dumps(
                [
                    {"type": "thinking", "thinking": "THINKING_SECRET", "signature": "sig"},
                    {"type": "redactedThinking", "data": "REDACTED_THINKING_SECRET"},
                ]
            ),
            6,
            visible,
        ),
        (
            "other-m1",
            "other",
            "user",
            json.dumps([{"type": "text", "text": "OTHER_SESSION_SECRET"}]),
            1,
            visible,
        ),
    ]
    connection.executemany(
        """
        INSERT INTO messages
            (message_id, session_id, role, content_json, created_timestamp, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.commit()
    connection.close()
    bootstrap_disclosure_ledger(database, "current")
    return database


def test_projection_is_exact_session_bounded_and_agent_visible(goose_database: Path) -> None:
    projection = project_goose_session(goose_database, "current")
    manifest = json.loads(projection.files["manifest.json"])
    all_content = b"\n".join(projection.files.values()).decode("utf-8")

    assert manifest["session_id"] == "current"
    assert manifest["source_message_rows"] == 8
    assert manifest["current_agent_visible_rows"] == 5
    assert manifest["historical_agent_visible_rows"] == 0
    assert manifest["projectable_rows"] == 5
    assert manifest["projected_messages"] == 5
    assert manifest["omitted_unprojected_rows"] == 3
    assert manifest["audience_filtered_blocks"] == 2
    assert manifest["content_omissions"]["internal-reasoning-not-projected"] == 2
    assert manifest["read_only"] is True
    assert manifest["schema_version"] == 2

    message_files = {
        path: json.loads(content)
        for path, content in projection.files.items()
        if path.startswith(f"{MESSAGE_PATH_PREFIX}/")
    }
    assert len(message_files) == manifest["projected_messages"]
    for path, payload in message_files.items():
        assert path == f"{MESSAGE_PATH_PREFIX}/{payload['sourceRowId']:020d}.json"

    event_files = {
        path: json.loads(content)
        for path, content in projection.files.items()
        if path.startswith(f"{EVENT_PATH_PREFIX}/")
    }
    assert len(event_files) == manifest["projected_events"]
    for path, payload in event_files.items():
        assert path == (
            f"{EVENT_PATH_PREFIX}/{payload['sourceRowId']:020d}-{payload['contentIndex']:06d}.json"
        )

    assert "VISIBLE_USER_MARKER" in all_content
    assert "VISIBLE_TOOL_RESULT" in all_content
    assert "COMPACTION_SUMMARY_MARKER" in all_content
    assert "calculate" in all_content
    for secret in (
        "ANNOTATED_USER_ONLY_SECRET",
        "PRECOMPACTION_HISTORY_MARKER",
        "NESTED_USER_ONLY_SECRET",
        "OTHER_SESSION_SECRET",
        "PROVIDER_METADATA_SECRET",
        "REQUEST_META_SECRET",
        "STRUCTURED_SECRET",
        "TOOL_RESULT_META_SECRET",
        "TOOL_CALL_INTERNAL_SECRET",
        "TOOL_RESULT_INTERNAL_SECRET",
        "USER_ONLY_ROW_SECRET",
        "THINKING_SECRET",
        "REDACTED_THINKING_SECRET",
        "SYSTEM_ROW_SECRET",
    ):
        assert secret not in all_content

    assert projection.snapshot().node_count > len(projection.files)


def test_projection_path_lists_and_reads_bounded_slices(goose_database: Path) -> None:
    projection = project_goose_session(goose_database, "current")

    listing = json.loads(
        render_projection_path(projection, "/context/session", offset=0, limit=1024)
    )
    assert listing == {
        "entries": [
            {"name": "events", "type": "directory"},
            {"name": "messages", "type": "directory"},
            {"name": "transcript.md", "type": "file"},
        ],
        "path": "/context/session",
        "read_only": True,
        "type": "directory",
    }

    first = json.loads(
        render_projection_path(projection, "session/transcript.md", offset=0, limit=32)
    )
    assert first["content"].startswith("# Goose session eligible")
    assert first["next_offset"] == 32

    with pytest.raises(ProjectionError, match="invalid component"):
        render_projection_path(projection, "../manifest.json", offset=0, limit=32)

    unicode_projection = SessionProjection(
        session_id="current",
        snapshot_id="unicode",
        files={"unicode.txt": "é".encode()},
    )
    with pytest.raises(ProjectionError, match="UTF-8 boundary"):
        render_projection_path(unicode_projection, "unicode.txt", offset=0, limit=1)


def test_bundle_round_trip_and_exclusive_write(
    goose_database: Path,
    tmp_path: Path,
) -> None:
    projection = project_goose_session(goose_database, "current")
    encoded = encode_bundle(projection.files)
    decoded = decode_bundle(encoded)

    assert decoded.node_count == projection.snapshot().node_count
    output = tmp_path / "session-context.json"
    write_bundle(output, projection.files)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_bundle(output, projection.files)


def test_projection_rejects_missing_or_other_session_database(
    goose_database: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectionError, match="does not exist"):
        project_goose_session(goose_database, "missing")
    with pytest.raises(ProjectionError, match="cannot inspect"):
        project_goose_session(tmp_path / "missing.db", "current")


def test_projection_caps_event_file_fanout(
    goose_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goose_session_module, "MAX_PROJECTED_EVENTS", 2)

    projection = project_goose_session(goose_database, "current")
    manifest = json.loads(projection.files["manifest.json"])

    assert manifest["projected_events"] == 2
    assert manifest["omitted_event_files"] == 4
    assert manifest["truncated"] is True
    assert len([path for path in projection.files if path.startswith("session/events/")]) == 2


def test_projection_uses_explicit_lower_bounds_instead_of_full_count_scans(
    goose_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goose_session_module, "MAX_PROJECTED_MESSAGES", 2)

    projection = project_goose_session(goose_database, "current", max_messages=2)
    manifest = json.loads(projection.files["manifest.json"])

    assert manifest["source_message_rows"] == 3
    assert manifest["current_agent_visible_rows"] == 3
    assert manifest["count_lower_bounds"] == [
        "source_message_rows",
        "current_agent_visible_rows",
        "projectable_rows",
    ]
    assert manifest["omitted_unprojected_rows"] is None
    assert manifest["truncated"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_both_adapters_bind_projection_to_mcp_request_metadata(
    build_server: ServerBuilder,
    goose_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_SESSION_ID", raising=False)
    settings = Settings(session_database=goose_database)

    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "session_context",
            {"path": "manifest.json"},
            meta={GOOSE_SESSION_META_KEY: "current"},
        )

    envelope = json.loads(result.content[0].text)
    manifest = json.loads(envelope["content"])
    assert manifest["session_id"] == "current"
    assert manifest["projection"] == "goose-session-operation-view"


@pytest.mark.anyio
@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_both_adapters_resolve_bounded_tail_reads_on_the_trusted_host(
    build_server: ServerBuilder,
    goose_database: Path,
) -> None:
    settings = Settings(session_database=goose_database)
    projection = project_goose_session(goose_database, "current")
    transcript = projection.files["session/transcript.md"]

    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "session_context",
            {"path": "session/transcript.md", "limit": 64, "tail": True},
            meta={GOOSE_SESSION_META_KEY: "current"},
        )

    assert result.is_error is not True
    envelope = json.loads(result.content[0].text)
    offset = envelope["offset"]
    assert max(len(transcript) - 64, 0) <= offset <= max(len(transcript) - 64, 0) + 3
    assert envelope["content"].encode() == transcript[offset:]
    assert envelope["next_offset"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_both_adapters_pin_multi_chunk_reads_while_fresh_views_advance(
    build_server: ServerBuilder,
    goose_database: Path,
) -> None:
    settings = Settings(session_database=goose_database)
    async with Client(build_server(settings)) as client:
        first_result = await client.call_tool(
            "session_context",
            {"path": "session/transcript.md", "limit": 64},
            meta={GOOSE_SESSION_META_KEY: "current"},
        )
        first = json.loads(first_result.content[0].text)
        pinned_view_id = first["view_id"]
        pinned_snapshot_id = first["snapshot_id"]

        connection = sqlite3.connect(goose_database)
        try:
            connection.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, role, content_json,
                     created_timestamp, metadata_json)
                VALUES ('later', 'current', 'assistant', ?, 100, ?)
                """,
                (
                    json.dumps([{"type": "text", "text": "FRESH_VIEW_ONLY_MARKER"}]),
                    json.dumps({"userVisible": True, "agentVisible": True}),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        pinned_content = bytearray(first["content"].encode())
        next_offset = first["next_offset"]
        while next_offset is not None:
            continuation_result = await client.call_tool(
                "session_context",
                {
                    "path": "session/transcript.md",
                    "offset": next_offset,
                    "limit": 64,
                    "view_id": pinned_view_id,
                },
                meta={GOOSE_SESSION_META_KEY: "current"},
            )
            continuation = json.loads(continuation_result.content[0].text)
            assert continuation["view_id"] == pinned_view_id
            assert continuation["snapshot_id"] == pinned_snapshot_id
            assert continuation["view_reused"] is True
            pinned_content.extend(continuation["content"].encode())
            next_offset = continuation["next_offset"]

        fresh_result = await client.call_tool(
            "session_context",
            {"path": "session/transcript.md", "limit": 64, "tail": True},
            meta={GOOSE_SESSION_META_KEY: "current"},
        )
        fresh = json.loads(fresh_result.content[0].text)

    assert b"FRESH_VIEW_ONLY_MARKER" not in pinned_content
    assert "FRESH_VIEW_ONLY_MARKER" in fresh["content"]
    assert fresh["view_id"] != pinned_view_id
    assert fresh["snapshot_id"] != pinned_snapshot_id
    assert fresh["view_reused"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_both_adapters_reject_ambiguous_or_directory_tail_reads(
    build_server: ServerBuilder,
    goose_database: Path,
) -> None:
    settings = Settings(session_database=goose_database)
    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="nonzero offset"):
            await client.call_tool(
                "session_context",
                {"path": "session/transcript.md", "offset": 1, "limit": 64, "tail": True},
                meta={GOOSE_SESSION_META_KEY: "current"},
            )
        with pytest.raises(ToolError, match="file path"):
            await client.call_tool(
                "session_context",
                {"path": "session", "limit": 64, "tail": True},
                meta={GOOSE_SESSION_META_KEY: "current"},
            )


@pytest.mark.anyio
@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_both_adapters_verify_exact_ledger_preparation_at_request_time(
    build_server: ServerBuilder,
    tmp_path: Path,
) -> None:
    database = StockGooseDatabase.create(tmp_path / "unprepared.db")
    database.add_message(
        "primary",
        message_id="current",
        role="user",
        content=[{"type": "text", "text": "current"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    settings = Settings(session_database=database.path)

    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="object set mismatch"):
            await client.call_tool(
                "session_context",
                {"path": "manifest.json"},
                meta={GOOSE_SESSION_META_KEY: "primary"},
            )

        bootstrap_disclosure_ledger(database.path, "primary")
        result = await client.call_tool(
            "session_context",
            {"path": "manifest.json"},
            meta={GOOSE_SESSION_META_KEY: "primary"},
        )
        assert result.is_error is not True

        connection = database.connect()
        try:
            connection.execute("DROP TRIGGER sandboxed_goose_disclosure_pre_archive")
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ToolError, match="object set mismatch"):
            await client.call_tool(
                "session_context",
                {"path": "manifest.json"},
                meta={GOOSE_SESSION_META_KEY: "primary"},
            )


def test_public_operation_tree_lists_and_reads_same_epoch_ledger_history(
    tmp_path: Path,
) -> None:
    database = StockGooseDatabase.create(tmp_path / "public-history.db")
    bootstrap_disclosure_ledger(database.path, "primary")
    request_id, response_id = database.add_tool_pair("primary", 1)
    database.archive_message("primary", "request-001")
    database.archive_message("primary", "response-001")
    summary_id = database.add_summary(
        "primary",
        summary_id="summary-001",
        tool_id="tool-001",
        response_created_timestamp=12,
    )
    settings = Settings(session_database=database.path)
    store = SessionViewStore()

    root = json.loads(render_session_context(settings, "primary", view_store=store))
    listing = json.loads(
        render_session_context(
            settings,
            "primary",
            path=MESSAGE_PATH_PREFIX,
            view_store=store,
        )
    )
    listed_names = {entry["name"] for entry in listing["entries"]}
    expected_names = {
        f"{source_row_id:020d}.json" for source_row_id in (request_id, response_id, summary_id)
    }

    assert root["operation"] == "recent-tree"
    assert {entry["name"] for entry in root["entries"]} == {
        "README.md",
        "manifest.json",
        "session",
    }
    assert listed_names == expected_names
    assert listing["operation"] == "recent-tree"

    archived_path = f"{MESSAGE_PATH_PREFIX}/{request_id:020d}.json"
    exact = json.loads(
        render_session_context(
            settings,
            "primary",
            path=archived_path,
            view_store=store,
        )
    )
    payload = json.loads(exact["content"])
    assert exact["operation"] == "exact-object"
    assert payload["sourceRowId"] == request_id
    assert payload["messageId"] == "request-001"
    assert '"name": "calculate"' in exact["content"]

    manifest_envelope = json.loads(
        render_session_context(
            settings,
            "primary",
            path="manifest.json",
            view_store=store,
        )
    )
    manifest = json.loads(manifest_envelope["content"])
    assert manifest["counts"] == {
        "source_message_rows": 3,
        "current_eligible_rows": 1,
        "ledger_captured_rows": 2,
        "projectable_rows": 3,
    }
    assert manifest["ledger_history_merged"] is True


def test_direct_callers_can_continue_the_process_default_view_store(tmp_path: Path) -> None:
    database = StockGooseDatabase.create(tmp_path / "default-view-store.db")
    bootstrap_disclosure_ledger(database.path, "primary")
    database.add_message(
        "primary",
        message_id="one",
        role="user",
        content=[{"type": "text", "text": "one"}],
        created_timestamp=1,
        metadata=visible_metadata(),
    )
    settings = Settings(session_database=database.path)

    first = json.loads(
        render_session_context(settings, "primary", path="manifest.json", limit=1)
    )
    continuation = json.loads(
        render_session_context(
            settings,
            "primary",
            path="manifest.json",
            offset=first["next_offset"],
            limit=64 * 1024,
            view_id=first["view_id"],
        )
    )

    assert continuation["view_id"] == first["view_id"]
    assert continuation["view_reused"] is True


def test_public_direct_and_apptainer_dispatch_have_matching_results(
    goose_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apptainer_calls: list[tuple[str, int, int]] = []

    def fake_apptainer_render(
        _settings: Settings,
        projection: SessionProjection,
        path: str,
        *,
        offset: int,
        limit: int,
    ) -> str:
        apptainer_calls.append((path, offset, limit))
        return render_projection_path(projection, path, offset=offset, limit=limit)

    monkeypatch.setattr(
        session_context_module,
        "render_projection_via_apptainer",
        fake_apptainer_render,
    )
    direct_settings = Settings(session_database=goose_database)
    apptainer_settings = Settings(
        session_database=goose_database,
        session_context_transport=APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
        context_image=tmp_path / "context.sif",
        apptainer_runtime_config=tmp_path / "apptainer.conf",
        apptainer_state=tmp_path / "state",
    )
    store = SessionViewStore()

    direct_root = json.loads(render_session_context(direct_settings, "current", view_store=store))
    apptainer_root = json.loads(
        render_session_context(
            apptainer_settings,
            "current",
            view_id=direct_root["view_id"],
            view_store=store,
        )
    )
    direct_root["view_reused"] = True
    assert apptainer_root == direct_root

    exact_path = f"{MESSAGE_PATH_PREFIX}/{2:020d}.json"
    direct_file = json.loads(
        render_session_context(
            direct_settings,
            "current",
            path=exact_path,
            offset=3,
            limit=41,
            view_store=store,
        )
    )
    apptainer_file = json.loads(
        render_session_context(
            apptainer_settings,
            "current",
            path=exact_path,
            offset=3,
            limit=41,
            view_id=direct_file["view_id"],
            view_store=store,
        )
    )
    direct_file["view_reused"] = True
    assert apptainer_file == direct_file

    with pytest.raises(ProjectionError) as direct_error:
        render_session_context(
            direct_settings,
            "current",
            path="missing.txt",
            view_store=store,
        )
    with pytest.raises(ProjectionError) as apptainer_error:
        render_session_context(
            apptainer_settings,
            "current",
            path="missing.txt",
            view_store=store,
        )
    assert str(apptainer_error.value) == str(direct_error.value)
    assert apptainer_calls == [
        ("", 0, 64 * 1024),
        (exact_path, 3, 41),
        ("missing.txt", 0, 64 * 1024),
    ]
