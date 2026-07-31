#!/usr/bin/env python3
"""Create durable many-turn and decoy sessions with real Goose and a mock provider."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sse_text(content: str) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"delta": {"role": "assistant", "content": content}, "index": 0}],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    return (
        "".join(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


class _FixtureServer(ThreadingHTTPServer):
    response_ordinal: int
    lock: threading.Lock


class _FixtureHandler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        with self.server.lock:
            self.server.response_ordinal += 1
            ordinal = self.server.response_ordinal
        response = _sse_text(f"FIXTURE_ASSISTANT_RESPONSE_{ordinal:03d}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a durable Goose session fixture without a live inference provider."
    )
    parser.add_argument("--goose-bin", required=True, type=Path)
    parser.add_argument("--goose-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--implementation", choices=("mcp-sdk", "fastmcp"), default="mcp-sdk")
    args = parser.parse_args(argv)
    if not 10 <= args.turns <= 200:
        parser.error("--turns must be between 10 and 200")
    return args


def _run_goose(
    goose_bin: Path,
    goose_root: Path,
    implementation: str,
    base_url: str,
    arguments: list[str],
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "GOOSE_BIN": str(goose_bin),
            "GOOSE_PATH_ROOT": str(goose_root),
            "GOOSE_PROVIDER": "openai",
            "GOOSE_MODEL": "gpt-4o",
            "GOOSE_DISABLE_KEYRING": "true",
            "GOOSE_TELEMETRY_ENABLED": "false",
            "OPENAI_API_KEY": "fixture-only",
            "OPENAI_BASE_URL": base_url,
            "OPENAI_TIMEOUT": "10",
            "SANDBOXED_GOOSE_MCP_IMPLEMENTATION": implementation,
            "SANDBOXED_GOOSE_SESSION_CONTEXT_TRANSPORT": "direct",
        }
    )
    result = subprocess.run(
        [str(PROJECT_ROOT / "scripts" / "goose.sh"), "run", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = result.stderr[-4000:]
        raise RuntimeError(f"Goose fixture turn failed ({result.returncode}):\n{diagnostic}")


def _session_id(database: Path, name: str) -> str:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id FROM sessions WHERE name = ?", (name,)).fetchall()
    finally:
        connection.close()
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise RuntimeError(f"expected exactly one Goose session named {name!r}")
    return rows[0][0]


def _message_count(database: Path, session_id: str) -> int:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], int):
        raise RuntimeError("cannot count Goose fixture messages")
    return row[0]


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    goose_bin = args.goose_bin.resolve(strict=True)
    if not os.access(goose_bin, os.X_OK):
        raise SystemExit("--goose-bin is not executable")
    if args.goose_root.exists() and any(args.goose_root.iterdir()):
        raise SystemExit("--goose-root must not exist or must be empty")
    args.goose_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    goose_root = args.goose_root.resolve(strict=True)
    token = secrets.token_hex(8)
    primary_name = f"ContextFS many-turn fixture {token}"
    decoy_name = f"ContextFS decoy fixture {token}"
    primary_prefix = f"PRIMARY_CONTEXTFS_{token}"
    decoy_marker = f"DECOY_CONTEXTFS_{token}"

    server = _FixtureServer(("127.0.0.1", 0), _FixtureHandler)
    server.response_ordinal = 0
    server.lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}/v1"
    database = goose_root / "data" / "sessions" / "sessions.db"
    try:
        _run_goose(
            goose_bin,
            goose_root,
            args.implementation,
            base_url,
            [
                "--quiet",
                "--name",
                primary_name,
                "--max-turns",
                "1",
                "--text",
                f"{primary_prefix}_TURN_001",
            ],
        )
        primary_session_id = _session_id(database, primary_name)
        for ordinal in range(2, args.turns + 1):
            _run_goose(
                goose_bin,
                goose_root,
                args.implementation,
                base_url,
                [
                    "--quiet",
                    "--resume",
                    "--session-id",
                    primary_session_id,
                    "--max-turns",
                    "1",
                    "--text",
                    f"{primary_prefix}_TURN_{ordinal:03d}",
                ],
            )
        _run_goose(
            goose_bin,
            goose_root,
            args.implementation,
            base_url,
            [
                "--quiet",
                "--name",
                decoy_name,
                "--max-turns",
                "1",
                "--text",
                decoy_marker,
            ],
        )
        decoy_session_id = _session_id(database, decoy_name)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    primary_messages = _message_count(database, primary_session_id)
    if primary_messages < args.turns * 2:
        raise RuntimeError(
            f"Goose created only {primary_messages} primary messages for {args.turns} turns"
        )
    manifest = {
        "schema_version": 1,
        "goose_root": str(goose_root),
        "database": str(database.resolve()),
        "goose_bin": str(goose_bin),
        "primary_session_id": primary_session_id,
        "primary_name": primary_name,
        "primary_marker_prefix": primary_prefix,
        "primary_turns": args.turns,
        "primary_messages": primary_messages,
        "decoy_session_id": decoy_session_id,
        "decoy_marker": decoy_marker,
    }
    _write_manifest(args.output, manifest)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
