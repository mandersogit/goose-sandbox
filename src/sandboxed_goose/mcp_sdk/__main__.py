"""Console entry point for the official MCP SDK implementation."""

from sandboxed_goose.mcp_sdk.server import mcp


def main() -> None:
    """Run the official MCP SDK server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
