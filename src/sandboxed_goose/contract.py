"""Public MCP contract shared by every framework adapter."""

SERVER_NAME = "sandboxed-goose"
SERVER_INSTRUCTIONS = (
    "Use calculate for basic arithmetic, sandbox_status to inspect the scaffold, and "
    "session_context to list or read the current session's read-only projected context. "
    "General filesystem and shell execution are unavailable."
)
