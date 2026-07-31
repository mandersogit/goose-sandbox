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
        },
        id="mcp-sdk",
    ),
    pytest.param(
        "fastmcp",
        {
            "sandboxed-goose-fastmcp__calculate",
            "sandboxed-goose-fastmcp__sandbox_status",
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
