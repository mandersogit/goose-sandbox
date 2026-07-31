# sandboxed-goose

A Python stdio MCP extension intended to replace Goose's built-in developer tools with a deliberately sandboxed tool surface.

**Status:** pre-alpha scaffold. Two synchronized MCP adapters expose the same two host-safe tools:

- `sandboxed_goose.mcp_sdk` uses the official MCP Python SDK.
- `sandboxed_goose.fastmcp` uses the standalone FastMCP framework.

The shared tool surface is:

- `sandbox_status()`: reports the requested sandbox configuration and confirms that execution remains disabled.
- `calculate(expression)`: evaluates bounded basic arithmetic with parentheses and `+`, `-`, `*`, `/`, `//`, `%`, and `**`.

The calculator parses expressions with Python's `ast` module and recursively evaluates only explicitly supported numeric nodes. It never evaluates the source or compiles the accepted tree into executable bytecode. Expression length, AST size, exponent magnitude, finite numbers, and intermediate result magnitude are bounded.

Filesystem and shell tools are intentionally not registered until a sandbox backend and its security contract are implemented. Framework-neutral behavior and metadata live under `sandboxed_goose.tools`; adapter parity is enforced by tests.

## Development setup

The project uses a conventional `src/` package layout, a project-local `local.venv`,
Make targets, pytest, Ruff, mypy, and pyright. Python 3.11 or newer must be available as
`python3`; set `PYTHON` to select another interpreter.

```bash
make install
make all
```

The experiment pins both `mcp==2.0.0` and `fastmcp==4.0.0b1`. FastMCP 4 is a prerelease, but it is the first FastMCP line compatible with MCP SDK v2; stable FastMCP 3.x requires `mcp<2` and cannot share this environment. Both servers use stdio and must never write application output to stdout because stdout is the MCP protocol stream.

The installed entry points are:

```text
sandboxed-goose-mcp-sdk
sandboxed-goose-fastmcp
```

`sandboxed-goose-mcp` and `python -m sandboxed_goose` remain aliases for the official SDK implementation.

## Run with Goose

Install the Goose CLI separately and make `goose` available on `PATH`. To use a source
build or another executable, set `GOOSE_BIN` to its path.

```bash
make goose-mcp-sdk ARGS='session'
make goose-fastmcp ARGS='session'
```

For a headless smoke test:

```bash
GOOSE_PROVIDER=ollama \
GOOSE_MODEL=your-installed-model \
OLLAMA_HOST=http://127.0.0.1:11434 \
make goose-fastmcp ARGS='run --text "Call calculate for 6 * 7."'
```

The project does not hard-code an inference provider, model, endpoint, or credential.
Supply them through Goose's supported environment variables or configure them under
the isolated test root. The wrapper does not modify or read the normal user
configuration.

The wrapper selects exactly one adapter and always supplies:

```text
--no-profile --with-extension <absolute-path>/local.venv/bin/sandboxed-goose-mcp-sdk
# or
--no-profile --with-extension <absolute-path>/local.venv/bin/sandboxed-goose-fastmcp
```

For a new session, `--no-profile` prevents configured extensions and plugin MCP servers from loading. Do not resume a session that previously had other extensions enabled when testing tool isolation.

`make all` checks both adapters at three levels: framework-level contract parity, an
MCP stdio client round trip, and—when Goose is on `PATH` or `GOOSE_BIN` is set—a real
Goose run against a local mock model endpoint. The Goose test captures the model
request and verifies that its tool array contains exactly the selected adapter's
namespaced `calculate` and `sandbox_status` tools. It requires neither provider
credentials nor internet access.

The wrapper uses a project-local Goose root at `.sandbox/goose` by setting
`GOOSE_PATH_ROOT`. The isolated root contains separate `config`, `data`, and `state`
directories and is ignored by Git. Override it only with another absolute path:

```bash
GOOSE_PATH_ROOT=/tmp/sandboxed-goose-test make goose-fastmcp ARGS='session'
```

For a Goose executable outside `PATH`:

```bash
GOOSE_BIN=/path/to/goose make goose-mcp-sdk ARGS='session'
```

You can also select an adapter when calling the wrapper directly:

```bash
SANDBOXED_GOOSE_MCP_IMPLEMENTATION=fastmcp ./scripts/goose.sh session
```

## Reserved configuration

These environment variables are parsed now so the eventual backend contract has a stable configuration seam:

- `SANDBOXED_GOOSE_BACKEND`: requested backend name, such as `bubblewrap`, `apptainer`, or `seatbelt`
- `SANDBOXED_GOOSE_WORKSPACE`: workspace path that would be exposed inside the sandbox

Setting them does not enable execution in the scaffold.

## Security invariant

There is no unsandboxed fallback. If a supported backend cannot be initialized with the requested policy, execution tools must remain unavailable or return a clear error.

See [docs/DESIGN.md](docs/DESIGN.md) for the current boundary and open design decisions.
The proposed hostile-command Bubblewrap policy is in
[docs/BUBBLEWRAP.md](docs/BUBBLEWRAP.md); it is not implemented or exposed as a tool.
The host recommendation, hardened runtime configuration, and first arm64 Apptainer
image recipe are in [docs/APPTAINER.md](docs/APPTAINER.md). The rootless image has been
built and validated locally, but the Apptainer backend is not yet wired to an execution
tool and does not enable shell execution.

Chronological implementation findings and local verification are recorded in
[dev-notes](dev-notes/README.md).

## License

This project is available under the [MIT License](LICENSE).
