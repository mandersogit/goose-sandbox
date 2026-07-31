from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_wrapper_force_disables_goose_tool_pair_summarization(tmp_path: Path) -> None:
    fake_goose = tmp_path / "goose"
    fake_goose.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "summarization=%s\\n" "$GOOSE_TOOL_PAIR_SUMMARIZATION"\n',
        encoding="utf-8",
    )
    fake_goose.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "GOOSE_BIN": str(fake_goose),
            "GOOSE_PATH_ROOT": str(tmp_path / "goose-root"),
            "GOOSE_TOOL_PAIR_SUMMARIZATION": "true",
            "SANDBOXED_GOOSE_MCP_IMPLEMENTATION": "mcp-sdk",
        }
    )

    result = subprocess.run(
        [str(PROJECT_ROOT / "scripts" / "goose.sh"), "run", "--text", "probe"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "summarization=false\n"
