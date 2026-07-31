#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GOOSE_BIN="${GOOSE_BIN:-goose}"
MCP_IMPLEMENTATION="${SANDBOXED_GOOSE_MCP_IMPLEMENTATION:-mcp-sdk}"

case "$MCP_IMPLEMENTATION" in
  fastmcp)
    MCP_SERVER="$PROJECT_ROOT/local.venv/bin/sandboxed-goose-fastmcp"
    ;;
  mcp-sdk)
    MCP_SERVER="$PROJECT_ROOT/local.venv/bin/sandboxed-goose-mcp-sdk"
    ;;
  *)
    echo "error: unknown MCP implementation '$MCP_IMPLEMENTATION'" >&2
    echo "expected SANDBOXED_GOOSE_MCP_IMPLEMENTATION=mcp-sdk or fastmcp" >&2
    exit 2
    ;;
esac

export GOOSE_PATH_ROOT="${GOOSE_PATH_ROOT:-$PROJECT_ROOT/.sandbox/goose}"
export GOOSE_DISABLE_KEYRING="${GOOSE_DISABLE_KEYRING:-true}"
export GOOSE_TELEMETRY_ENABLED="${GOOSE_TELEMETRY_ENABLED:-false}"

if [[ "$GOOSE_PATH_ROOT" != /* ]]; then
  echo "error: GOOSE_PATH_ROOT must be absolute: $GOOSE_PATH_ROOT" >&2
  exit 2
fi

mkdir -p \
  "$GOOSE_PATH_ROOT/config" \
  "$GOOSE_PATH_ROOT/data" \
  "$GOOSE_PATH_ROOT/state"

if [[ "$GOOSE_BIN" == */* ]]; then
  if [[ ! -x "$GOOSE_BIN" ]]; then
    echo "error: Goose binary is not executable: $GOOSE_BIN" >&2
    exit 1
  fi
  GOOSE_BIN_DIR="$(cd "$(dirname "$GOOSE_BIN")" && pwd)"
  GOOSE_BIN="$GOOSE_BIN_DIR/$(basename "$GOOSE_BIN")"
else
  RESOLVED_GOOSE_BIN="$(command -v "$GOOSE_BIN" || true)"
  if [[ -z "$RESOLVED_GOOSE_BIN" ]]; then
    echo "error: Goose CLI not found on PATH" >&2
    echo "install Goose or set GOOSE_BIN to an executable path" >&2
    exit 1
  fi
  GOOSE_BIN="$RESOLVED_GOOSE_BIN"
fi

if [[ ! -x "$GOOSE_BIN" ]]; then
  echo "error: Goose binary is not executable: $GOOSE_BIN" >&2
  exit 1
fi

if [[ ! -x "$MCP_SERVER" ]]; then
  echo "error: $MCP_IMPLEMENTATION server entry point not found at $MCP_SERVER" >&2
  echo "run 'make install'" >&2
  exit 1
fi

SUBCOMMAND="${1:-session}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$SUBCOMMAND" in
  run | session)
    ;;
  *)
    echo "usage: scripts/goose.sh [session|run] [goose arguments...]" >&2
    exit 2
    ;;
esac

cd "$PROJECT_ROOT"
exec "$GOOSE_BIN" "$SUBCOMMAND" \
  --no-profile \
  --with-extension "$MCP_SERVER" \
  "$@"
