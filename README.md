# sandboxed-goose

A Python stdio MCP extension intended to replace Goose's built-in developer tools with a deliberately sandboxed tool surface.

**Status:** pre-alpha scaffold. Two synchronized MCP adapters expose the same three host-safe tools:

- `sandboxed_goose.mcp_sdk` uses the official MCP Python SDK.
- `sandboxed_goose.fastmcp` uses the standalone FastMCP framework.

The shared tool surface is:

- `sandbox_status()`: reports the requested sandbox configuration and confirms that execution remains disabled.
- `calculate(expression)`: evaluates bounded basic arithmetic with parentheses and `+`, `-`, `*`, `/`, `//`, `%`, and `**`.
- `session_context(path="", offset=0, limit=65536, tail=False)`: lists or reads
  bounded, read-only virtual files projected from the current Goose session. A tail
  read resolves the final `limit` bytes inside the trusted projection.

The calculator parses expressions with Python's `ast` module and recursively evaluates only explicitly supported numeric nodes. It never evaluates the source or compiles the accepted tree into executable bytecode. Expression length, AST size, exponent magnitude, finite numbers, and intermediate result magnitude are bounded.

General workspace read/write and shell tools are intentionally not registered until a
sandbox backend and its security contract are implemented. Framework-neutral behavior
and metadata live under `sandboxed_goose.tools`; adapter parity is enforced by tests.

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
sandboxed-goose-contextfs  # image-only dependencies; not an MCP tool
sandboxed-goose-read-context  # fixed image-only reader; not an MCP tool
sandboxed-goose-export-session  # trusted host-side snapshot exporter
sandboxed-goose-live-test  # sustained model/FUSE verification driver
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
three namespaced tools. A second deterministic run makes Goose call
`session_context` and verifies that the returned manifest names the exact active
session from Goose's SQLite store. It requires neither provider credentials nor
internet access.

The wrapper uses a project-local Goose root at `.sandbox/goose` by setting
`GOOSE_PATH_ROOT`. The isolated root contains separate `config`, `data`, and `state`
directories and is ignored by Git. Override it only with another absolute path:

```bash
GOOSE_PATH_ROOT=/tmp/sandboxed-goose-test make goose-fastmcp ARGS='session'
```

Every project-managed Goose invocation force-disables Goose's background tool-pair
summarizer with `GOOSE_TOOL_PAIR_SUMMARIZATION=false`. That feature asynchronously
archives tool request/result rows and inserts summaries while a turn is running, which
is incompatible with exact accumulating-session tests. The wrapper deliberately
overrides an inherited `true` value; ordinary token-threshold compaction remains
available.

For a Goose executable outside `PATH`:

```bash
GOOSE_BIN=/path/to/goose make goose-mcp-sdk ARGS='session'
```

You can also select an adapter when calling the wrapper directly:

```bash
SANDBOXED_GOOSE_MCP_IMPLEMENTATION=fastmcp ./scripts/goose.sh session
```

## Configuration

These environment variables are parsed now so the eventual backend contract has a stable configuration seam:

- `SANDBOXED_GOOSE_BACKEND`: requested backend name, such as `bubblewrap`, `apptainer`, or `seatbelt`
- `SANDBOXED_GOOSE_WORKSPACE`: workspace path that would be exposed inside the sandbox
- `SANDBOXED_GOOSE_SESSION_DATABASE`: optional explicit Goose `sessions.db` path;
  otherwise it is derived from `GOOSE_PATH_ROOT`
- `SANDBOXED_GOOSE_SESSION_CONTEXT_TRANSPORT`: `direct` (the default) or the
  opt-in `apptainer-fuse` transport
- `SANDBOXED_GOOSE_CONTEXT_IMAGE`: immutable context-enabled SIF used by the
  FUSE transport
- `SANDBOXED_GOOSE_APPTAINER_CONFIG`: context-enabled Apptainer runtime policy
- `SANDBOXED_GOOSE_APPTAINER_STATE`: private mode-`0700` cache, temporary, and
  per-read state root
- `APPTAINER`: optional Apptainer executable override

The context transport enables only bounded reads of an already approved projection.
It does not enable general filesystem access or command execution. The Goose wrapper
supplies project-local image, policy, and state defaults when `apptainer-fuse` is
selected.

## Security invariant

There is no unsandboxed fallback. If a supported backend cannot be initialized with the requested policy, execution tools must remain unavailable or return a clear error.

See [docs/DESIGN.md](docs/DESIGN.md) for the current boundary and open design decisions.
The proposed hostile-command Bubblewrap policy is in
[docs/BUBBLEWRAP.md](docs/BUBBLEWRAP.md); it is not implemented or exposed as a tool.
The host recommendation, hardened runtime configuration, and first arm64 Apptainer
image recipe are in [docs/APPTAINER.md](docs/APPTAINER.md). The rootless image has been
built and validated locally, but the Apptainer backend is not yet wired to an execution
tool and does not enable shell execution.

ContextFS now has two proven inputs. Its original deterministic toy tree exercises the
FUSE mechanics. The session projection reads exactly the session selected by Goose's
`agent-session-id` MCP request metadata, normalizes current and explicitly preserved
historically agent-visible messages, and exposes a manifest, Markdown transcript,
per-message JSON, and per-content-event JSON. The trusted host exports a mode-`0600`
bounded bundle; only that bundle—not the Goose database—is bound read-only into
Apptainer. A fresh projection is built for each read or sandbox launch.

The projection deliberately excludes rows without agent-disclosure provenance,
audience-scoped user content, thinking blocks, binary payloads, provider metadata, MCP
`_meta`, usage, cost, configuration, and all other sessions. `session_context` lets the
agent list or read the same virtual files before the general sandboxed read/Bash tools
exist. Its default `direct` transport renders in the MCP process. The opt-in
`apptainer-fuse` transport exports a fresh bundle per call, starts the fixed reader in
the hardened context image, requires that `/context` is a real FUSE mount, and returns
only the bounded reader envelope. Build the image with `make apptainer-context-image`
and rerun the toy and synthetic-session checks with `make test-apptainer-contextfs`.

An opt-in real-session suite can use a durable fixture created by the real Goose CLI
against a deterministic loopback provider:

```bash
local.venv/bin/python scripts/create-goose-session-fixture.py \
  --goose-bin /path/to/goose \
  --goose-root /private/test/goose \
  --output /private/test/fixture.json \
  --turns 12
SANDBOXED_GOOSE_REAL_SESSION_FIXTURE=/private/test/fixture.json \
  make test-real-session-contextfs
```

The suite checks both MCP adapters and then resumes the selected Goose session through
each adapter so Goose itself supplies the session binding. It uses no live inference
service.

To inspect an exact fixture session manually through the same Apptainer/FUSE mount:

```bash
scripts/shell-apptainer-session-context.sh \
  --fixture /private/test/fixture.json
```

The script opens an offline interactive Bash shell with the session at `/context` and
removes its private projection bundle when the shell exits. It also accepts an explicit
`--database PATH --session-id ID` pair.

### Sustained live test

The live-test driver creates a new private Goose root, a same-database decoy session,
and a primary session that it resumes for 10–200 sequential turns. Every primary turn
must make two bounded `session_context` reads through the `apptainer-fuse` transport.
The driver verifies the canonical SQLite rows, request/result IDs, exact tool names and
arguments, fresh projection snapshot, current-turn canary, decoy isolation, and final
assistant sentinel after every invocation. It preserves prompts, outputs, reports,
configuration, and the complete Goose root under `.sandbox/live-tests/`.

Supply the provider endpoint, model, and Goose binary explicitly (or via the matching
environment variables); none are embedded in the project:

```bash
local.venv/bin/python -m sandboxed_goose.live_test initial \
  --ollama-host http://127.0.0.1:11434 \
  --model your-tool-capable-model \
  --goose-bin /path/to/goose \
  --turns 10 \
  --adapter mcp-sdk
```

After that phase passes, the same session can perform projection-dependent work:

```bash
local.venv/bin/python -m sandboxed_goose.live_test audit \
  --run /absolute/path/to/.sandbox/live-tests/RUN_ID \
  --tasks 1
```

Each audit task lists `session/messages/by-source-row`, reads a host-selected message
from prior work, and must recover `sourceRowId`, `messageId`, `createdAt`, and disclosure
visibility—data that is present in the projected JSON but not in ordinary conversation
messages. It returns those values in a strict plain-text record so provider tool-call
parsers cannot mistake a requested JSON object for another tool call. Task one targets
the last initial reply; each later task targets the preceding audit reply, proving that
the projected filesystem refreshes as work accumulates. Message paths use immutable
SQLite source-row IDs, so Goose history compaction cannot make a path refer to a
different message. The `ordinal` and `contextVisibility` fields remain
snapshot-relative, and the oracle validates the exact values returned by the tool.

The supported runtime contract is stock, unmodified Goose. The current projector
exposes only currently visible rows from stock Goose; once stock Goose archives a row,
the row is ambiguous and remains excluded. The launcher therefore forces tool-pair
summarization off. The tracked provenance patch is retained only as a development
reference, not as an installation requirement. The
[hardening plan](dev-notes/2026-07-31-tool-pair-summarization-hardening-plan.md)
replaces that dependency with a project-owned atomic disclosure ledger.

Chronological implementation findings and local verification are recorded in
[dev-notes](dev-notes/README.md).

## License

This project is available under the [MIT License](LICENSE).
