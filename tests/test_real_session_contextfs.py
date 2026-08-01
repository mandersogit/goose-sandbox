from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from sandboxed_goose.config import (
    APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
    Settings,
)
from sandboxed_goose.contextfs.view_store import SessionViewStore
from sandboxed_goose.fastmcp.server import build_server as build_fastmcp_server
from sandboxed_goose.mcp_sdk.server import build_server as build_mcp_sdk_server
from sandboxed_goose.session_binding import GOOSE_SESSION_META_KEY
from sandboxed_goose.tools.session_context import render_session_context

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ENV = "SANDBOXED_GOOSE_REAL_SESSION_FIXTURE"
FIXTURE_PATH = os.environ.get(FIXTURE_ENV)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not FIXTURE_PATH, reason=f"{FIXTURE_ENV} is not configured"),
]


def _fixture() -> dict[str, Any]:
    assert FIXTURE_PATH is not None
    value: Any = json.loads(Path(FIXTURE_PATH).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("real-session fixture manifest must be an object")
    return value


def _settings(fixture: dict[str, Any]) -> Settings:
    return Settings(
        session_database=Path(fixture["database"]),
        session_context_transport=APPTAINER_FUSE_SESSION_CONTEXT_TRANSPORT,
        context_image=(
            PROJECT_ROOT / ".sandbox" / "apptainer" / "images" / "sandbox-python-context-arm64.sif"
        ),
        apptainer_runtime_config=(
            PROJECT_ROOT / "containers" / "apptainer" / "apptainer-hostile-context.conf"
        ),
        apptainer_state=PROJECT_ROOT / ".sandbox" / "apptainer",
        apptainer_executable=os.environ.get("APPTAINER", "apptainer"),
    )


def _fuse_connections() -> set[str]:
    root = Path("/sys/fs/fuse/connections")
    try:
        return {entry.name for entry in root.iterdir() if entry.is_dir()}
    except OSError:
        return set()


def _sse_stream(chunks: list[dict[str, Any]]) -> bytes:
    return (
        "".join(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def _context_tool_stream(tool_name: str) -> bytes:
    return _sse_stream(
        [
            {
                "id": "chatcmpl-real-context-tool",
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
                                    "id": "call_real_session_context",
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


_CONTEXT_FINAL_STREAM = _sse_stream(
    [
        {
            "id": "chatcmpl-real-context-final",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "delta": {"role": "assistant", "content": "FUSE_CONTEXT_OK"},
                    "index": 0,
                }
            ],
        },
        {
            "id": "chatcmpl-real-context-final",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
        },
        {
            "id": "chatcmpl-real-context-final",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
)


class _CaptureServer(ThreadingHTTPServer):
    requests: queue.Queue[dict[str, Any]]


class _ContextHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload: dict[str, Any] = json.loads(self.rfile.read(content_length))
        self.server.requests.put(payload)
        messages = payload["messages"]
        last_user = max(
            (index for index, message in enumerate(messages) if message.get("role") == "user"),
            default=-1,
        )
        has_current_tool_result = any(
            message.get("role") == "tool" for message in messages[last_user + 1 :]
        )
        if has_current_tool_result:
            response = _CONTEXT_FINAL_STREAM
        else:
            tool_name = next(
                tool["function"]["name"]
                for tool in payload["tools"]
                if tool["function"]["name"].endswith("__session_context")
            )
            response = _context_tool_stream(tool_name)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _mock_context_provider() -> Iterator[tuple[str, queue.Queue[dict[str, Any]]]]:
    server = _CaptureServer(("127.0.0.1", 0), _ContextHandler)
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


async def _call(client: Client[Any], session_id: str, **arguments: object) -> dict[str, Any]:
    result = await client.call_tool(
        "session_context",
        arguments,
        meta={GOOSE_SESSION_META_KEY: session_id},
    )
    value: Any = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise AssertionError("session_context response must be an object")
    return value


@pytest.mark.anyio
@pytest.mark.parametrize(
    "build_server",
    [
        pytest.param(build_mcp_sdk_server, id="mcp-sdk"),
        pytest.param(build_fastmcp_server, id="fastmcp"),
    ],
)
async def test_real_many_turn_session_is_read_through_apptainer_fuse(
    build_server: Any,
) -> None:
    fixture = _fixture()
    settings = _settings(fixture)
    session_id = fixture["primary_session_id"]
    direct_settings = Settings(session_database=settings.session_database)
    direct_store = SessionViewStore()
    direct_root = json.loads(
        render_session_context(direct_settings, session_id, view_store=direct_store)
    )
    direct_manifest_envelope = json.loads(
        render_session_context(
            direct_settings,
            session_id,
            path="manifest.json",
            view_store=direct_store,
        )
    )
    direct_manifest = json.loads(direct_manifest_envelope["content"])
    direct_messages = json.loads(
        render_session_context(
            direct_settings,
            session_id,
            path="session/messages/by-source-row",
            view_store=direct_store,
        )
    )
    direct_transcript = bytearray()
    direct_offset = 0
    direct_view_id: str | None = None
    while True:
        direct_envelope = json.loads(
            render_session_context(
                direct_settings,
                session_id,
                path="session/transcript.md",
                offset=direct_offset,
                limit=1024,
                view_id=direct_view_id,
                view_store=direct_store,
            )
        )
        direct_view_id = direct_envelope["view_id"]
        direct_transcript.extend(direct_envelope["content"].encode())
        next_offset = direct_envelope["next_offset"]
        if next_offset is None:
            break
        direct_offset = next_offset

    expected_source_rows = fixture["primary_turns"] * 2
    assert direct_manifest["counts"]["source_message_rows"] >= min(expected_source_rows, 257)
    assert direct_manifest["descriptor_count"] >= min(expected_source_rows, 256)
    if expected_source_rows <= 256:
        assert f"{fixture['primary_marker_prefix']}_TURN_001" in direct_transcript.decode()
    assert (
        f"{fixture['primary_marker_prefix']}_TURN_{fixture['primary_turns']:03d}"
        in direct_transcript.decode()
    )
    assert fixture["decoy_marker"] not in direct_transcript.decode()

    connections_before = _fuse_connections()
    async with Client(build_server(settings)) as client:
        root = await _call(client, session_id)
        assert root["snapshot_id"] == direct_root["snapshot_id"]
        assert root["entries"] == direct_root["entries"]

        manifest_envelope = await _call(
            client,
            session_id,
            path="manifest.json",
            offset=0,
            limit=65536,
        )
        assert json.loads(manifest_envelope["content"]) == direct_manifest
        assert manifest_envelope["snapshot_id"] == direct_manifest_envelope["snapshot_id"]

        transcript = bytearray()
        offset = 0
        transcript_view_id: str | None = None
        while True:
            envelope = await _call(
                client,
                session_id,
                path="session/transcript.md",
                offset=offset,
                limit=1024,
                view_id=transcript_view_id,
            )
            transcript_view_id = envelope["view_id"]
            transcript.extend(envelope["content"].encode("utf-8"))
            next_offset = envelope["next_offset"]
            if next_offset is None:
                break
            assert next_offset > offset
            offset = next_offset
        assert transcript == direct_transcript

        messages = await _call(
            client,
            session_id,
            path="session/messages/by-source-row",
        )
        assert messages["entries"] == direct_messages["entries"]

        exact_path = f"session/messages/by-source-row/{messages['entries'][0]['name']}"
        exact = await _call(client, session_id, path=exact_path)
        direct_exact = json.loads(
            render_session_context(
                direct_settings,
                session_id,
                path=exact_path,
                view_store=direct_store,
            )
        )
        assert exact["content"] == direct_exact["content"]
        assert exact["snapshot_id"] == direct_exact["snapshot_id"]

    assert _fuse_connections() == connections_before
    runs = settings.apptainer_state / "session-context-runs"
    assert not runs.exists() or list(runs.iterdir()) == []


@pytest.mark.parametrize("implementation", ["mcp-sdk", "fastmcp"])
def test_real_goose_resumes_selected_session_and_calls_fuse_context(
    implementation: str,
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    settings = _settings(fixture)
    session_id = fixture["primary_session_id"]
    copied_goose_root = tmp_path / "goose"
    shutil.copytree(fixture["goose_root"], copied_goose_root)
    connections_before = _fuse_connections()

    with _mock_context_provider() as (base_url, requests):
        environment = os.environ.copy()
        environment.update(
            {
                "GOOSE_BIN": fixture["goose_bin"],
                "GOOSE_PATH_ROOT": str(copied_goose_root),
                "GOOSE_PROVIDER": "openai",
                "GOOSE_MODEL": "gpt-4o",
                "GOOSE_DISABLE_KEYRING": "true",
                "GOOSE_TELEMETRY_ENABLED": "false",
                "OPENAI_API_KEY": "fixture-only",
                "OPENAI_BASE_URL": base_url,
                "OPENAI_TIMEOUT": "10",
                "SANDBOXED_GOOSE_MCP_IMPLEMENTATION": implementation,
                "SANDBOXED_GOOSE_SESSION_CONTEXT_TRANSPORT": "apptainer-fuse",
                "SANDBOXED_GOOSE_CONTEXT_IMAGE": str(settings.context_image),
                "SANDBOXED_GOOSE_APPTAINER_CONFIG": str(settings.apptainer_runtime_config),
                "SANDBOXED_GOOSE_APPTAINER_STATE": str(settings.apptainer_state),
            }
        )
        result = subprocess.run(
            [
                str(PROJECT_ROOT / "scripts" / "goose.sh"),
                "run",
                "--resume",
                "--session-id",
                session_id,
                "--quiet",
                "--max-turns",
                "3",
                "--text",
                "Read the current session context manifest, then reply FUSE_CONTEXT_OK.",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "FUSE_CONTEXT_OK" in result.stdout
        captured = [requests.get(timeout=2)]
        while not requests.empty():
            captured.append(requests.get_nowait())

    tool_results: list[str] = []
    for payload in captured:
        messages = payload["messages"]
        last_user = max(
            (index for index, message in enumerate(messages) if message.get("role") == "user"),
            default=-1,
        )
        tool_results.extend(
            message["content"]
            for message in messages[last_user + 1 :]
            if message.get("role") == "tool" and isinstance(message.get("content"), str)
        )
    assert len(tool_results) == 1
    envelope = json.loads(tool_results[0])
    manifest = json.loads(envelope["content"])
    assert manifest["session_id"] == session_id
    assert manifest["counts"]["source_message_rows"] >= min(fixture["primary_turns"] * 2, 257)
    assert _fuse_connections() == connections_before
    runs = settings.apptainer_state / "session-context-runs"
    assert not runs.exists() or list(runs.iterdir()) == []
