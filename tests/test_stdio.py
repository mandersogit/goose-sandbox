import sys

import anyio
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from sandboxed_goose.tools import TOOL_DEFINITIONS

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
