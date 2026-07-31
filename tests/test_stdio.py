import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from sandboxed_goose.config import GOOSE_PATH_ROOT_ENV
from sandboxed_goose.contextfs.disclosure_ledger import verify_disclosure_ledger
from sandboxed_goose.session_binding import GOOSE_SESSION_ENV_KEY
from sandboxed_goose.tools import TOOL_DEFINITIONS
from tests.support.stock_goose import StockGooseDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.integration, pytest.mark.timeout(15)]

SERVER_MODULES = [
    pytest.param("sandboxed_goose.mcp_sdk", id="mcp-sdk"),
    pytest.param("sandboxed_goose.fastmcp", id="fastmcp"),
]


@pytest.mark.parametrize("server_module", SERVER_MODULES)
async def test_server_round_trips_over_stdio(server_module: str) -> None:
    transport = stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", server_module],
        )
    )

    with anyio.fail_after(10):
        async with Client(transport) as client:
            tools = await client.list_tools()

    assert [tool.name for tool in tools.tools] == [
        definition.name for definition in TOOL_DEFINITIONS
    ]


@pytest.mark.parametrize("server_module", SERVER_MODULES)
async def test_bound_session_ledger_exists_before_stdio_advertises_tools(
    server_module: str,
    tmp_path: Path,
) -> None:
    goose_root = tmp_path / "goose"
    database_path = goose_root / "data" / "sessions" / "sessions.db"
    database_path.parent.mkdir(parents=True)
    StockGooseDatabase.create(database_path)
    environment = os.environ.copy()
    environment.update(
        {
            GOOSE_PATH_ROOT_ENV: str(goose_root),
            GOOSE_SESSION_ENV_KEY: "primary",
        }
    )
    transport = stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", server_module],
            env=environment,
        )
    )

    with anyio.fail_after(10):
        async with Client(transport) as client:
            tools = await client.list_tools()
            status = verify_disclosure_ledger(database_path, "primary")

    assert len(tools.tools) == len(TOOL_DEFINITIONS)
    assert status.session_id == "primary"
    assert status.coverage_complete is True
