"""Console entry point for the official MCP SDK implementation."""

from sandboxed_goose.mcp_sdk.server import build_server
from sandboxed_goose.stdio_startup import prepare_stdio_server


def main() -> None:
    """Run the official MCP SDK server over stdio."""
    prepared = prepare_stdio_server()
    build_server(prepared.settings).run()


if __name__ == "__main__":
    main()
