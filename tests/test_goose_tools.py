import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from sandboxed_goose.contextfs.disclosure_ledger import verify_disclosure_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOOSE_BIN = Path(os.environ.get("GOOSE_BIN") or shutil.which("goose") or "__goose_not_found__")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not GOOSE_BIN.is_file(), reason="Goose CLI is not available"),
]

IMPLEMENTATIONS = [
    pytest.param(
        "mcp-sdk",
        {
            "sandboxed-goose-mcp-sdk__calculate",
            "sandboxed-goose-mcp-sdk__sandbox_status",
            "sandboxed-goose-mcp-sdk__session_context",
        },
        id="mcp-sdk",
    ),
    pytest.param(
        "fastmcp",
        {
            "sandboxed-goose-fastmcp__calculate",
            "sandboxed-goose-fastmcp__sandbox_status",
            "sandboxed-goose-fastmcp__session_context",
        },
        id="fastmcp",
    ),
]

CHAT_STREAM = "".join(
    (
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
        '"model":"gpt-4o","choices":[{"delta":{"role":"assistant","content":"OK"},'
        '"index":0}]}\n\n',
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
        '"model":"gpt-4o","choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n',
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
        '"model":"gpt-4o","choices":[],"usage":{"prompt_tokens":1,'
        '"completion_tokens":1,"total_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    )
).encode()


def _sse_stream(chunks: list[dict[str, Any]]) -> bytes:
    return (
        "".join(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def _tool_call_stream(tool_name: str) -> bytes:
    return _sse_stream(
        [
            {
                "id": "chatcmpl-context-tool",
                "object": "chat.completion.chunk",
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_session_context",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": '{"path":"manifest.json"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ]
    )


CONTEXT_FINAL_STREAM = _sse_stream(
    [
        {
            "id": "chatcmpl-context-final",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "delta": {"role": "assistant", "content": "CONTEXT_OK"},
                    "index": 0,
                }
            ],
        },
        {
            "id": "chatcmpl-context-final",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
        },
        {
            "id": "chatcmpl-context-final",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
)


class _OpenAIHandler(BaseHTTPRequestHandler):
    server: "_CaptureServer"

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length))
        self.server.requests.put((self.path, payload))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(CHAT_STREAM)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(CHAT_STREAM)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _CaptureServer(ThreadingHTTPServer):
    requests: queue.Queue[tuple[str, dict[str, Any]]]


class _SessionContextHandler(_OpenAIHandler):
    server: "_CaptureServer"

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length))
        self.server.requests.put((self.path, payload))

        has_tool_result = any(message.get("role") == "tool" for message in payload["messages"])
        if "tools" not in payload:
            response = CHAT_STREAM
        elif has_tool_result:
            response = CONTEXT_FINAL_STREAM
        else:
            tool_name = next(
                tool["function"]["name"]
                for tool in payload["tools"]
                if tool["function"]["name"].endswith("__session_context")
            )
            response = _tool_call_stream(tool_name)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)


@contextmanager
def mock_openai() -> Iterator[tuple[str, queue.Queue[tuple[str, dict[str, Any]]]]]:
    server = _CaptureServer(("127.0.0.1", 0), _OpenAIHandler)
    server.requests = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    try:
        yield f"http://{host}:{port}/v1", server.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def mock_openai_session_context() -> Iterator[tuple[str, queue.Queue[tuple[str, dict[str, Any]]]]]:
    server = _CaptureServer(("127.0.0.1", 0), _SessionContextHandler)
    server.requests = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    try:
        yield f"http://{host}:{port}/v1", server.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(("implementation", "expected_tool_names"), IMPLEMENTATIONS)
def test_goose_sends_only_adapter_tools_to_provider(
    implementation: str,
    expected_tool_names: set[str],
    tmp_path: Path,
) -> None:
    goose_root = tmp_path / "goose"

    with mock_openai() as (base_url, requests):
        env = os.environ.copy()
        env.update(
            {
                "GOOSE_BIN": str(GOOSE_BIN),
                "GOOSE_PATH_ROOT": str(goose_root),
                "GOOSE_PROVIDER": "openai",
                "GOOSE_MODEL": "gpt-4o",
                "GOOSE_DISABLE_KEYRING": "true",
                "GOOSE_TELEMETRY_ENABLED": "false",
                "OPENAI_API_KEY": "integration-test-only",
                "OPENAI_BASE_URL": base_url,
                "OPENAI_TIMEOUT": "10",
                "SANDBOXED_GOOSE_MCP_IMPLEMENTATION": implementation,
            }
        )
        result = subprocess.run(
            [
                str(PROJECT_ROOT / "scripts" / "goose.sh"),
                "run",
                "--no-session",
                "--quiet",
                "--text",
                "Reply with OK.",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        captured = [requests.get(timeout=2)]
        while not requests.empty():
            captured.append(requests.get_nowait())

    assert all(path == "/v1/chat/completions" for path, _payload in captured)
    tool_requests = [payload for _path, payload in captured if "tools" in payload]
    assert len(tool_requests) == 1, captured
    payload = tool_requests[0]
    assert payload["stream"] is True
    tools = payload["tools"]
    assert len(tools) == len(expected_tool_names)
    assert all(tool["type"] == "function" for tool in tools)
    assert {tool["function"]["name"] for tool in tools} == expected_tool_names


@pytest.mark.parametrize(("implementation", "_expected_tool_names"), IMPLEMENTATIONS)
def test_goose_binds_session_context_tool_to_the_active_session(
    implementation: str,
    _expected_tool_names: set[str],
    tmp_path: Path,
) -> None:
    goose_root = tmp_path / "goose"

    with mock_openai_session_context() as (base_url, requests):
        env = os.environ.copy()
        env.update(
            {
                "GOOSE_BIN": str(GOOSE_BIN),
                "GOOSE_PATH_ROOT": str(goose_root),
                "GOOSE_PROVIDER": "openai",
                "GOOSE_MODEL": "gpt-4o",
                "GOOSE_DISABLE_KEYRING": "true",
                "GOOSE_TELEMETRY_ENABLED": "false",
                "OPENAI_API_KEY": "integration-test-only",
                "OPENAI_BASE_URL": base_url,
                "OPENAI_TIMEOUT": "10",
                "SANDBOXED_GOOSE_MCP_IMPLEMENTATION": implementation,
            }
        )
        result = subprocess.run(
            [
                str(PROJECT_ROOT / "scripts" / "goose.sh"),
                "run",
                "--no-session",
                "--quiet",
                "--text",
                "Read your session context manifest, then reply CONTEXT_OK.",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "CONTEXT_OK" in result.stdout
        captured = [requests.get(timeout=2)]
        while not requests.empty():
            captured.append(requests.get_nowait())

    assert all(path == "/v1/chat/completions" for path, _payload in captured)
    database = goose_root / "data" / "sessions" / "sessions.db"
    connection = sqlite3.connect(database)
    session_ids = [row[0] for row in connection.execute("SELECT id FROM sessions")]
    content_rows = [
        row[0]
        for row in connection.execute(
            "SELECT content_json FROM messages WHERE session_id = ? ORDER BY id",
            (session_ids[0],),
        )
    ]
    connection.close()

    assert len(session_ids) == 1
    manifest: dict[str, Any] | None = None
    for content_json in content_rows:
        for block in json.loads(content_json):
            if block.get("type") != "toolResponse":
                continue
            text = block["toolResult"]["value"]["content"][0]["text"]
            envelope = json.loads(text)
            candidate = json.loads(envelope["content"])
            if candidate.get("projection") == "goose-session-operation-view":
                manifest = candidate

    assert manifest is not None
    assert manifest["session_id"] == session_ids[0]
    assert manifest["descriptor_count"] >= 1
    assert manifest["counts"]["current_eligible_rows"] >= 1
    ledger = verify_disclosure_ledger(database, session_ids[0])
    assert ledger.session_id == session_ids[0]
    assert ledger.coverage_complete is True
