from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.support.stock_goose import StockGooseDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOOSE_WRAPPER = PROJECT_ROOT / "scripts" / "goose.sh"
MCP_SERVER = PROJECT_ROOT / "local.venv" / "bin" / "sandboxed-goose-mcp-sdk"
GOOSE_BIN = Path(os.environ.get("GOOSE_BIN") or shutil.which("goose") or "__missing_goose__")
SUMMARY_PROMPT = "Your task is to summarize a tool call & response pair to save tokens."
SUMMARY_MARKER = "DETERMINISTIC_TOOL_PAIR_SUMMARY"
FINAL_MARKER = "SUMMARIZATION_CONTROL_FINAL"
TOOL_EXPRESSION_PATTERN = re.compile(r'"expression"\s*:\s*"(\d+)\+(\d+)"')

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not GOOSE_BIN.is_file(), reason="set GOOSE_BIN to a Goose executable"),
]


def _sse_text(content: str) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-summarization-control",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"delta": {"role": "assistant", "content": content}, "index": 0}],
        },
        {
            "id": "chatcmpl-summarization-control",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
        },
        {
            "id": "chatcmpl-summarization-control",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
    ]
    return (
        "".join(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def _message_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text.append(block["text"])
    return "\n".join(text)


def _summary_tool_id(payload: Mapping[str, object]) -> str | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    typed_messages = [message for message in messages if isinstance(message, dict)]
    system_messages = [
        _message_text(message) for message in typed_messages if message.get("role") == "system"
    ]
    if len(system_messages) != 1 or SUMMARY_PROMPT not in system_messages[0]:
        return None
    user_messages = [
        _message_text(message) for message in typed_messages if message.get("role") == "user"
    ]
    if len(user_messages) != 1:
        raise AssertionError("tool-pair summary request must have exactly one user message")
    expressions = TOOL_EXPRESSION_PATTERN.findall(user_messages[0])
    if len(expressions) != 1 or expressions[0][0] != expressions[0][1]:
        raise AssertionError("tool-pair summary request did not identify exactly one tool pair")
    return f"tool-{int(expressions[0][0]):03d}"


class _CaptureServer(ThreadingHTTPServer):
    requests: queue.Queue[dict[str, Any]]
    classification_errors: queue.Queue[str]


class _DeterministicProviderHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload: dict[str, Any] = json.loads(self.rfile.read(content_length))
        self.server.requests.put(payload)
        try:
            tool_id = _summary_tool_id(payload)
        except AssertionError as error:
            self.server.classification_errors.put(str(error))
            tool_id = "unclassified"
        response = _sse_text(f"{SUMMARY_MARKER} {tool_id}" if tool_id is not None else FINAL_MARKER)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _deterministic_provider() -> Iterator[
    tuple[str, queue.Queue[dict[str, Any]], queue.Queue[str]]
]:
    server = _CaptureServer(("127.0.0.1", 0), _DeterministicProviderHandler)
    server.requests = queue.Queue()
    server.classification_errors = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/v1", server.requests, server.classification_errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _environment(goose_root: Path, base_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GOOSE_BIN": str(GOOSE_BIN.resolve()),
            "GOOSE_PATH_ROOT": str(goose_root),
            "GOOSE_PROVIDER": "openai",
            "GOOSE_MODEL": "gpt-4o",
            "GOOSE_DISABLE_KEYRING": "true",
            "GOOSE_TELEMETRY_ENABLED": "false",
            "GOOSE_AUTO_COMPACT_THRESHOLD": "0",
            "GOOSE_TOOL_CALL_CUTOFF": "2",
            "GOOSE_TOOL_PAIR_SUMMARIZATION": "true",
            "OPENAI_API_KEY": "deterministic-test-only",
            "OPENAI_BASE_URL": base_url,
            "OPENAI_TIMEOUT": "10",
            "SANDBOXED_GOOSE_MCP_IMPLEMENTATION": "mcp-sdk",
            "SANDBOXED_GOOSE_SESSION_CONTEXT_TRANSPORT": "direct",
        }
    )
    return environment


def _run(
    arguments: Sequence[str], environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=PROJECT_ROOT,
        env=dict(environment),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _captured_requests(requests: queue.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    captured = [requests.get(timeout=2)]
    while not requests.empty():
        captured.append(requests.get_nowait())
    return captured


def _captured_errors(errors: queue.Queue[str]) -> list[str]:
    captured: list[str] = []
    while not errors.empty():
        captured.append(errors.get_nowait())
    return captured


def _session_id(database: Path, name: str) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT id FROM sessions WHERE name = ?", (name,)).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    assert isinstance(rows[0][0], str)
    return rows[0][0]


def _initialize_seeded_session(goose_root: Path) -> tuple[str, Path]:
    session_name = "deterministic summarization control"
    with _deterministic_provider() as (base_url, requests, classification_errors):
        result = _run(
            [
                str(GOOSE_WRAPPER),
                "run",
                "--quiet",
                "--name",
                session_name,
                "--max-turns",
                "1",
                "--text",
                "Initialize the deterministic summarization control session.",
            ],
            _environment(goose_root, base_url),
        )
        assert result.returncode == 0, result.stderr
        assert FINAL_MARKER in result.stdout
        captured = _captured_requests(requests)
        assert _captured_errors(classification_errors) == []
        assert len(captured) == 1
        assert _summary_tool_id(captured[0]) is None

    database = goose_root / "data" / "sessions" / "sessions.db"
    session_id = _session_id(database, session_name)
    fixture = StockGooseDatabase(database)
    connection = fixture.connect()
    try:
        created_timestamp_base = int(
            connection.execute(
                "SELECT MAX(created_timestamp) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    for ordinal in range(1, 14):
        fixture.add_tool_pair(
            session_id,
            ordinal,
            created_timestamp_base=created_timestamp_base,
        )
    return session_id, database


def _resume(
    goose_root: Path,
    session_id: str,
    *,
    through_wrapper: bool,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]], list[str]]:
    with _deterministic_provider() as (base_url, requests, classification_errors):
        environment = _environment(goose_root, base_url)
        common = [
            "--resume",
            "--session-id",
            session_id,
            "--quiet",
            "--max-turns",
            "1",
            "--text",
            "Return the deterministic control response.",
        ]
        if through_wrapper:
            arguments = [str(GOOSE_WRAPPER), "run", *common]
        else:
            arguments = [
                str(GOOSE_BIN.resolve()),
                "run",
                "--no-profile",
                "--with-extension",
                str(MCP_SERVER),
                *common,
            ]
        result = _run(arguments, environment)
        captured = _captured_requests(requests)
        errors = _captured_errors(classification_errors)
    return result, captured, errors


def _visibility_by_message_id(database: Path, session_id: str) -> dict[str, bool]:
    connection = sqlite3.connect(database)
    try:
        records = connection.execute(
            "SELECT message_id, metadata_json FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    return {
        str(message_id): bool(json.loads(metadata_json)["agentVisible"])
        for message_id, metadata_json in records
        if message_id is not None
    }


def _summary_rows(database: Path, session_id: str) -> list[tuple[str, int, dict[str, Any]]]:
    connection = sqlite3.connect(database)
    try:
        records = connection.execute(
            """
            SELECT role, created_timestamp, content_json, metadata_json
            FROM messages WHERE session_id = ? ORDER BY id
            """,
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    summaries: list[tuple[str, int, dict[str, Any]]] = []
    for role, created_timestamp, content_json, metadata_json in records:
        content = json.loads(content_json)
        if not (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and isinstance(content[0].get("text"), str)
            and content[0]["text"].startswith(SUMMARY_MARKER)
        ):
            continue
        summaries.append((role, created_timestamp, json.loads(metadata_json)))
    return summaries


def _response_timestamps(database: Path, session_id: str) -> list[int]:
    connection = sqlite3.connect(database)
    try:
        records = connection.execute(
            """
            SELECT created_timestamp FROM messages
            WHERE session_id = ? AND message_id LIKE 'response-%'
            ORDER BY message_id LIMIT 10
            """,
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    return [int(record[0]) for record in records]


def test_wrapper_disables_real_tool_pair_summarization_with_enabled_inverse_control(
    tmp_path: Path,
) -> None:
    assert MCP_SERVER.is_file(), "run `make install` before Goose integration tests"
    baseline_root = tmp_path / "baseline"
    session_id, _baseline_database = _initialize_seeded_session(baseline_root)
    disabled_root = tmp_path / "disabled"
    enabled_root = tmp_path / "enabled"
    shutil.copytree(baseline_root, disabled_root)
    shutil.copytree(baseline_root, enabled_root)

    disabled_result, disabled_requests, disabled_errors = _resume(
        disabled_root, session_id, through_wrapper=True
    )
    assert disabled_result.returncode == 0, disabled_result.stderr
    assert FINAL_MARKER in disabled_result.stdout
    assert disabled_errors == []
    assert [_summary_tool_id(payload) for payload in disabled_requests] == [None]

    disabled_database = disabled_root / "data" / "sessions" / "sessions.db"
    disabled_visibility = _visibility_by_message_id(disabled_database, session_id)
    assert all(disabled_visibility[f"request-{ordinal:03d}"] for ordinal in range(1, 14))
    assert all(disabled_visibility[f"response-{ordinal:03d}"] for ordinal in range(1, 14))
    assert _summary_rows(disabled_database, session_id) == []

    enabled_result, enabled_requests, enabled_errors = _resume(
        enabled_root, session_id, through_wrapper=False
    )
    assert enabled_result.returncode == 0, enabled_result.stderr
    assert FINAL_MARKER in enabled_result.stdout
    assert enabled_errors == []
    summary_tool_ids = [
        tool_id
        for payload in enabled_requests
        if (tool_id := _summary_tool_id(payload)) is not None
    ]
    assert summary_tool_ids == [f"tool-{ordinal:03d}" for ordinal in range(1, 11)], enabled_requests
    assert sum(_summary_tool_id(payload) is None for payload in enabled_requests) == 1

    enabled_database = enabled_root / "data" / "sessions" / "sessions.db"
    enabled_visibility = _visibility_by_message_id(enabled_database, session_id)
    for ordinal in range(1, 11):
        assert enabled_visibility[f"request-{ordinal:03d}"] is False
        assert enabled_visibility[f"response-{ordinal:03d}"] is False
    for ordinal in range(11, 14):
        assert enabled_visibility[f"request-{ordinal:03d}"] is True
        assert enabled_visibility[f"response-{ordinal:03d}"] is True

    summaries = _summary_rows(enabled_database, session_id)
    assert len(summaries) == 10
    assert [created for _role, created, _metadata in summaries] == _response_timestamps(
        enabled_database, session_id
    )
    assert all(role == "user" for role, _created, _metadata in summaries)
    assert all(
        metadata["agentVisible"] is True and metadata["userVisible"] is False
        for _role, _created, metadata in summaries
    )
