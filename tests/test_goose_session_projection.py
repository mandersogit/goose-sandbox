from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import sandboxed_goose.contextfs.goose_session as goose_session_module
from sandboxed_goose.config import Settings
from sandboxed_goose.contextfs.bundle import decode_bundle, encode_bundle, write_bundle
from sandboxed_goose.contextfs.goose_session import (
    SessionProjection,
    project_goose_session,
    render_projection_path,
)
from sandboxed_goose.contextfs.model import ProjectionError
from sandboxed_goose.fastmcp.server import build_server as build_fastmcp_server
from sandboxed_goose.mcp_sdk.server import build_server as build_mcp_sdk_server
from sandboxed_goose.session_binding import GOOSE_SESSION_META_KEY

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
            session_id TEXT NOT NULL,
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
    return database


def test_projection_is_exact_session_bounded_and_agent_visible(goose_database: Path) -> None:
    projection = project_goose_session(goose_database, "current")
    manifest = json.loads(projection.files["manifest.json"])
    all_content = b"\n".join(projection.files.values()).decode("utf-8")

    assert manifest["session_id"] == "current"
    assert manifest["source_message_rows"] == 8
    assert manifest["current_agent_visible_rows"] == 5
    assert manifest["historical_agent_visible_rows"] == 1
    assert manifest["projectable_rows"] == 6
    assert manifest["projected_messages"] == 6
    assert manifest["omitted_unprojected_rows"] == 2
    assert manifest["audience_filtered_blocks"] == 2
    assert manifest["content_omissions"]["internal-reasoning-not-projected"] == 2
    assert manifest["read_only"] is True

    assert "VISIBLE_USER_MARKER" in all_content
    assert "PRECOMPACTION_HISTORY_MARKER" in all_content
    assert "VISIBLE_TOOL_RESULT" in all_content
    assert "COMPACTION_SUMMARY_MARKER" in all_content
    assert "calculate" in all_content
    for secret in (
        "ANNOTATED_USER_ONLY_SECRET",
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
    assert first["content"].startswith("# Goose session disclosed")
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
    assert manifest["omitted_event_files"] == 5
    assert manifest["truncated"] is True
    assert len([path for path in projection.files if path.startswith("session/events/")]) == 2


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
    assert manifest["projection"] == "goose-session-disclosed-history"
