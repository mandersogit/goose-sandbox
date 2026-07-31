from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import sandboxed_goose.live_test as live_test
from sandboxed_goose.contextfs.goose_session import (
    MESSAGE_PATH_PREFIX,
    project_goose_session,
)
from sandboxed_goose.live_test import (
    LiveTestError,
    _audit_expected_line,
    _audit_prompt,
    _base_environment,
    _initial_prompt,
    audit_expected_result,
    select_audit_target,
    verify_audit_turn,
    verify_initial_turn,
)

VISIBLE = json.dumps({"userVisible": True, "agentVisible": True})
TOOL_NAME = "sandboxed-goose-mcp-sdk__session_context"


def _provenance_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    goose_bin = tmp_path / "bin" / "goose"
    goose_bin.parent.mkdir()
    goose_bin.write_text("#!/bin/sh\nprintf 'goose 1.45.0\\n'\n")
    goose_bin.chmod(0o755)

    adapter = tmp_path / "local.venv" / "bin" / "sandboxed-goose-mcp-sdk"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("#!/bin/sh\nexit 0\n")
    adapter.chmod(0o755)

    goose_source = tmp_path / "stock-goose"
    goose_source.mkdir()
    return goose_bin, adapter, goose_source


def test_live_provenance_accepts_clean_stock_goose_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goose_bin, _, goose_source = _provenance_fixture(tmp_path)
    project_root = tmp_path.resolve()
    source_root = goose_source.resolve()

    def fake_git_capture(arguments: list[str], cwd: Path) -> str:
        if cwd == source_root:
            if arguments[-1] == "--is-inside-work-tree":
                return "true"
            if arguments[-1] == "--porcelain":
                return ""
            return "stock-goose-commit"
        if cwd == project_root:
            return "project-commit" if arguments[-1] == "HEAD" else " M README.md"
        raise AssertionError((arguments, cwd))

    monkeypatch.setattr(live_test, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(live_test, "_git_capture", fake_git_capture)

    provenance = live_test._provenance(
        goose_bin=goose_bin,
        goose_source=goose_source,
        adapter="mcp-sdk",
        probe_root=tmp_path / "probe",
    )

    assert provenance["goose_runtime_contract"] == "stock-unmodified"
    assert provenance["goose_commit"] == "stock-goose-commit"
    assert provenance["goose_status"] == ""
    assert provenance["goose_source_clean"] is True


def test_live_provenance_rejects_modified_goose_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goose_bin, _, goose_source = _provenance_fixture(tmp_path)
    project_root = tmp_path.resolve()
    source_root = goose_source.resolve()

    def fake_git_capture(arguments: list[str], cwd: Path) -> str:
        if cwd == source_root:
            if arguments[-1] == "--is-inside-work-tree":
                return "true"
            if arguments[-1] == "--porcelain":
                return " M crates/goose/src/context_mgmt/mod.rs"
            return "stock-goose-commit"
        if cwd == project_root:
            return "project-commit" if arguments[-1] == "HEAD" else ""
        raise AssertionError((arguments, cwd))

    monkeypatch.setattr(live_test, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(live_test, "_git_capture", fake_git_capture)

    with pytest.raises(LiveTestError, match="clean, unmodified Goose checkout"):
        live_test._provenance(
            goose_bin=goose_bin,
            goose_source=goose_source,
            adapter="mcp-sdk",
            probe_root=tmp_path / "probe",
        )


def test_live_environment_force_disables_goose_tool_pair_summarization(tmp_path: Path) -> None:
    environment = _base_environment(
        run_root=tmp_path,
        goose_root=tmp_path / "goose",
        goose_bin=tmp_path / "bin" / "goose",
        adapter="mcp-sdk",
        origin="http://127.0.0.1:11434",
        model="test-model",
        image=tmp_path / "context.sif",
        runtime_config=tmp_path / "apptainer.conf",
        apptainer=Path("/usr/bin/apptainer"),
    )

    assert environment["GOOSE_TOOL_PAIR_SUMMARIZATION"] == "false"


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL,
            metadata_json TEXT
        );
        INSERT INTO sessions (id, name) VALUES ('primary', 'primary'), ('decoy', 'decoy');
        """
    )
    connection.close()
    return database


def _insert(database: Path, rows: list[tuple[str, list[dict[str, Any]]]]) -> None:
    connection = sqlite3.connect(database)
    first_id = int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
    connection.executemany(
        """
        INSERT INTO messages
            (message_id, session_id, role, content_json, created_timestamp, metadata_json)
        VALUES (?, 'primary', ?, ?, ?, ?)
        """,
        [
            (
                f"message-{first_id + offset}",
                role,
                json.dumps(content),
                first_id + offset,
                VISIBLE,
            )
            for offset, (role, content) in enumerate(rows, start=1)
        ],
    )
    connection.commit()
    connection.close()


def _request(tool_id: str, arguments: dict[str, object], name: str = TOOL_NAME) -> dict[str, Any]:
    return {
        "type": "toolRequest",
        "id": tool_id,
        "toolCall": {
            "status": "success",
            "value": {"name": name, "arguments": arguments},
        },
    }


def _response(tool_id: str, envelope: dict[str, object]) -> dict[str, Any]:
    return {
        "type": "toolResponse",
        "id": tool_id,
        "toolResult": {
            "status": "success",
            "value": {
                "content": [{"type": "text", "text": json.dumps(envelope)}],
                "isError": False,
            },
        },
    }


def _file_envelope(path: str, size: int, offset: int, content: str) -> dict[str, object]:
    content_end = offset + len(content.encode())
    return {
        "path": f"/context/{path}",
        "type": "file",
        "read_only": True,
        "size": size,
        "offset": offset,
        "next_offset": content_end if content_end < size else None,
        "content": content,
    }


def test_initial_oracle_proves_persisted_tail_reads_and_exact_session(tmp_path: Path) -> None:
    database = _database(tmp_path)
    canary = "SGCTX_TEST_001_cafefeed"
    prompt = _initial_prompt(1, 10, canary)
    expected_final = f"SGCTX_OK 1 {canary}"
    first_size = 5000
    second_size = first_size + 400
    tail_offset = second_size - 2048
    tail_content = "x" * (2048 - len(canary)) + canary
    _insert(
        database,
        [
            ("user", [{"type": "text", "text": prompt}]),
            (
                "assistant",
                [_request("read-size", {"path": "session/transcript.md", "offset": 0, "limit": 1})],
            ),
            (
                "user",
                [
                    _response(
                        "read-size", _file_envelope("session/transcript.md", first_size, 0, "#")
                    )
                ],
            ),
            (
                "assistant",
                [
                    _request(
                        "read-tail",
                        {
                            "path": "session/transcript.md",
                            "limit": 2048,
                            "tail": True,
                        },
                    )
                ],
            ),
            (
                "user",
                [
                    _response(
                        "read-tail",
                        _file_envelope(
                            "session/transcript.md",
                            second_size,
                            tail_offset,
                            tail_content,
                        ),
                    )
                ],
            ),
            ("assistant", [{"type": "text", "text": expected_final}]),
        ],
    )

    report = verify_initial_turn(
        database,
        "primary",
        after_row_id=0,
        prompt=prompt,
        canary=canary,
        expected_final=expected_final,
        tool_name=TOOL_NAME,
        decoy_marker="DECOY_MUST_NOT_APPEAR",
        previous_snapshot_id=None,
        previous_source_rows=0,
    )

    assert report["new_rows"] == 6
    assert report["tool_calls"] == 2
    assert report["tail_offset"] == tail_offset
    assert report["source_message_rows"] == 6


def test_initial_oracle_rejects_any_other_tool(tmp_path: Path) -> None:
    database = _database(tmp_path)
    canary = "SGCTX_TEST_001_badtool"
    prompt = _initial_prompt(1, 10, canary)
    expected_final = f"SGCTX_OK 1 {canary}"
    _insert(
        database,
        [
            ("user", [{"type": "text", "text": prompt}]),
            (
                "assistant",
                [
                    _request(
                        "read-size",
                        {"path": "session/transcript.md", "offset": 0, "limit": 1},
                        name="sandboxed-goose-mcp-sdk__calculate",
                    )
                ],
            ),
            (
                "user",
                [_response("read-size", _file_envelope("session/transcript.md", 1, 0, "#"))],
            ),
            (
                "assistant",
                [
                    _request(
                        "read-tail",
                        {"path": "session/transcript.md", "limit": 2048, "tail": True},
                    )
                ],
            ),
            (
                "user",
                [
                    _response(
                        "read-tail",
                        _file_envelope("session/transcript.md", 100, 0, canary),
                    )
                ],
            ),
            ("assistant", [{"type": "text", "text": expected_final}]),
        ],
    )

    with pytest.raises(LiveTestError, match="unexpected tools"):
        verify_initial_turn(
            database,
            "primary",
            after_row_id=0,
            prompt=prompt,
            canary=canary,
            expected_final=expected_final,
            tool_name=TOOL_NAME,
            decoy_marker="DECOY_MUST_NOT_APPEAR",
            previous_snapshot_id=None,
            previous_source_rows=0,
        )


def test_audit_oracle_requires_projection_only_metadata_and_accumulation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    prior_marker = "SGCTX_OK 10 SGCTX_PRIOR_MARKER"
    _insert(
        database,
        [
            ("user", [{"type": "text", "text": f"Please later return {prior_marker}"}]),
            ("assistant", [{"type": "text", "text": prior_marker}]),
        ],
    )
    before = project_goose_session(database, "primary")
    target = select_audit_target(database, "primary", "SGCTX_PRIOR_MARKER")
    marker = "SGCTX_AUDIT_TEST_001_feedface"
    prompt = _audit_prompt(1, marker, target)
    expected = audit_expected_result(target, marker, 1)
    listing = {
        "path": f"/context/{MESSAGE_PATH_PREFIX}",
        "type": "directory",
        "read_only": True,
        "entries": [{"name": Path(target.path).name, "type": "file"}],
    }
    target_content = json.dumps(target.payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _insert(
        database,
        [
            ("user", [{"type": "text", "text": prompt}]),
            ("assistant", [_request("list-messages", {"path": MESSAGE_PATH_PREFIX})]),
            ("user", [_response("list-messages", listing)]),
            (
                "assistant",
                [
                    _request(
                        "read-target",
                        {"path": target.path, "offset": 0, "limit": 65536},
                    )
                ],
            ),
            (
                "user",
                [
                    _response(
                        "read-target",
                        _file_envelope(
                            target.path, len(target_content.encode()), 0, target_content
                        ),
                    )
                ],
            ),
            ("assistant", [{"type": "text", "text": _audit_expected_line(expected)}]),
        ],
    )

    report = verify_audit_turn(
        database,
        "primary",
        after_row_id=2,
        prompt=prompt,
        marker=marker,
        expected_result=expected,
        target=target,
        tool_name=TOOL_NAME,
        decoy_marker="DECOY_MUST_NOT_APPEAR",
        previous_snapshot_id=before.snapshot_id,
        previous_source_rows=2,
    )

    assert report["tool_calls"] == 2
    assert report["target"] == expected
    assert report["target_snapshot_fields_changed"] is False
    assert report["source_message_rows"] == 8


def test_audit_target_path_survives_compaction_ordinal_and_visibility_changes(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    prior_marker = "SGCTX_OK 10 SGCTX_COMPACTION_TARGET"
    _insert(
        database,
        [
            ("user", [{"type": "text", "text": f"Please later return {prior_marker}"}]),
            ("assistant", [{"type": "text", "text": prior_marker}]),
        ],
    )
    before = project_goose_session(database, "primary")
    target = select_audit_target(database, "primary", "SGCTX_COMPACTION_TARGET")
    assert target.ordinal == 2

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE messages SET metadata_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "userVisible": True,
                    "agentVisible": False,
                    "historicallyAgentVisible": True,
                }
            ),
            target.source_row_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO messages
            (message_id, session_id, role, content_json, created_timestamp, metadata_json)
        VALUES ('summary', 'primary', 'user', ?, 1, ?)
        """,
        (json.dumps([{"type": "text", "text": "compaction summary"}]), VISIBLE),
    )
    connection.commit()
    connection.close()

    shifted_target = select_audit_target(database, "primary", "SGCTX_COMPACTION_TARGET")
    assert shifted_target.path == target.path
    assert shifted_target.ordinal == 3
    assert shifted_target.context_visibility == "historical"

    marker = "SGCTX_AUDIT_TEST_001_compacted"
    prompt = _audit_prompt(1, marker, target)
    selected = audit_expected_result(target, marker, 1)
    observed = audit_expected_result(shifted_target, marker, 1)
    target_content = (
        json.dumps(shifted_target.payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    listing = {
        "path": f"/context/{MESSAGE_PATH_PREFIX}",
        "type": "directory",
        "read_only": True,
        "entries": [{"name": Path(target.path).name, "type": "file"}],
    }
    _insert(
        database,
        [
            ("user", [{"type": "text", "text": prompt}]),
            ("assistant", [_request("list-messages", {"path": MESSAGE_PATH_PREFIX})]),
            ("user", [_response("list-messages", listing)]),
            (
                "assistant",
                [_request("read-target", {"path": target.path, "offset": 0, "limit": 65536})],
            ),
            (
                "user",
                [
                    _response(
                        "read-target",
                        _file_envelope(
                            target.path, len(target_content.encode()), 0, target_content
                        ),
                    )
                ],
            ),
            ("assistant", [{"type": "text", "text": _audit_expected_line(observed)}]),
        ],
    )

    report = verify_audit_turn(
        database,
        "primary",
        after_row_id=2,
        prompt=prompt,
        marker=marker,
        expected_result=selected,
        target=target,
        tool_name=TOOL_NAME,
        decoy_marker="DECOY_MUST_NOT_APPEAR",
        previous_snapshot_id=before.snapshot_id,
        previous_source_rows=2,
    )

    assert report["target"] == observed
    assert report["selected_target"] == selected
    assert report["target_snapshot_fields_changed"] is True
