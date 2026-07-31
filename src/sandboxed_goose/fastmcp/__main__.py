"""Console entry point for the standalone FastMCP implementation."""

from sandboxed_goose.fastmcp.server import build_server
from sandboxed_goose.stdio_startup import prepare_stdio_server


def main() -> None:
    """Run the standalone FastMCP server over stdio."""
    prepared = prepare_stdio_server()
    build_server(prepared.settings).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
