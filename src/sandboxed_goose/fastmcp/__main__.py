"""Console entry point for the standalone FastMCP implementation."""

from sandboxed_goose.fastmcp.server import mcp


def main() -> None:
    """Run the standalone FastMCP server over stdio."""
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
