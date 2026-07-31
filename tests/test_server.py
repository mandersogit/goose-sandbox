import json
from collections.abc import Callable
from typing import Any

import pytest

from sandboxed_goose.config import Settings
from sandboxed_goose.fastmcp.server import build_server as build_fastmcp_server
from sandboxed_goose.mcp_sdk.server import build_server as build_mcp_sdk_server
from sandboxed_goose.tools import TOOL_DEFINITIONS

pytestmark = pytest.mark.anyio

ServerBuilder = Callable[[Settings | None], Any]
SERVER_BUILDERS = [
    pytest.param(build_mcp_sdk_server, id="mcp-sdk"),
    pytest.param(build_fastmcp_server, id="fastmcp"),
]


@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_scaffold_registers_the_shared_tool_catalog(
    build_server: ServerBuilder,
) -> None:
    server = build_server(None)
    tools = await server.list_tools()

    assert [tool.name for tool in tools] == [tool.name for tool in TOOL_DEFINITIONS]
    assert [tool.description for tool in tools] == [tool.description for tool in TOOL_DEFINITIONS]


@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_status_tool_is_explicitly_fail_closed(
    build_server: ServerBuilder,
) -> None:
    server = build_server(
        Settings(
            requested_backend="bubblewrap",
        )
    )

    result = await server.call_tool("sandbox_status", {})
    content = result.content[0]
    payload = json.loads(content.text)

    assert result.is_error is not True
    assert payload["execution_enabled"] is False
    assert payload["requested_backend"] == "bubblewrap"
    assert "disabled" in payload["reason"].lower()


@pytest.mark.parametrize("build_server", SERVER_BUILDERS)
async def test_calculator_tool_evaluates_arithmetic(
    build_server: ServerBuilder,
) -> None:
    result = await build_server(None).call_tool(
        "calculate",
        {"expression": "(12 + 8) * 3 / 4"},
    )
    payload = json.loads(result.content[0].text)

    assert result.is_error is not True
    assert payload == {
        "expression": "(12 + 8) * 3 / 4",
        "result": 15.0,
    }


async def test_frameworks_expose_equivalent_public_contract() -> None:
    settings = Settings(requested_backend="bubblewrap")
    mcp_sdk = build_mcp_sdk_server(settings)
    fastmcp = build_fastmcp_server(settings)

    mcp_sdk_tools = await mcp_sdk.list_tools()
    fastmcp_tools = await fastmcp.list_tools()
    assert [(tool.name, tool.description) for tool in mcp_sdk_tools] == [
        (tool.name, tool.description) for tool in fastmcp_tools
    ]

    for mcp_sdk_tool, fastmcp_tool in zip(mcp_sdk_tools, fastmcp_tools, strict=True):
        mcp_sdk_schema = mcp_sdk_tool.input_schema
        fastmcp_schema = fastmcp_tool.parameters
        assert mcp_sdk_schema["type"] == fastmcp_schema["type"] == "object"
        assert set(mcp_sdk_schema["properties"]) == set(fastmcp_schema["properties"])
        assert mcp_sdk_schema.get("required", []) == fastmcp_schema.get("required", [])
        for property_name in mcp_sdk_schema["properties"]:
            assert (
                mcp_sdk_schema["properties"][property_name]["type"]
                == fastmcp_schema["properties"][property_name]["type"]
            )

    mcp_sdk_result = await mcp_sdk.call_tool("sandbox_status", {})
    fastmcp_result = await fastmcp.call_tool("sandbox_status", {})
    assert mcp_sdk_result.content[0].text == fastmcp_result.content[0].text
    assert mcp_sdk_result.structured_content == fastmcp_result.structured_content

    arguments = {"expression": "2**10 + 7"}
    mcp_sdk_result = await mcp_sdk.call_tool("calculate", arguments)
    fastmcp_result = await fastmcp.call_tool("calculate", arguments)
    assert mcp_sdk_result.content[0].text == fastmcp_result.content[0].text
    assert mcp_sdk_result.structured_content == fastmcp_result.structured_content
