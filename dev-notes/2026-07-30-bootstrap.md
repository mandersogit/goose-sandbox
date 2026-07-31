# 2026-07-30: project bootstrap and Goose integration

## Objective

The initial experiment establishes a Python stdio MCP extension that can eventually
replace Goose's developer tools with read, write, and shell operations backed by a real
OS sandbox. No filesystem or process-execution tool is enabled at this stage.

The package uses a `src/` tree, project-local `local.venv`, setuptools metadata, Make
targets, pytest, Ruff, mypy, and pyright.

## Two MCP framework implementations

The project deliberately carries both candidate frameworks:

- `mcp==2.0.0`, the official MCP Python SDK; and
- `fastmcp==4.0.0b1`, the standalone FastMCP framework.

FastMCP 4 is a prerelease, but it is the line compatible with MCP SDK 2. FastMCP 3
requires `mcp<2`, so the stable FastMCP release and MCP SDK 2 cannot be tested in one
environment.

Framework-neutral names, descriptions, settings, and behavior live outside both
adapters. The adapters are intentionally thin, and parametrized contract tests require
them to expose equivalent tools, argument schemas, structured content, and text
responses. This lets later work compare ergonomics without allowing the implementations
to acquire different capabilities.

The current shared surface is:

- `sandbox_status`, which explicitly reports that execution is disabled; and
- `calculate`, a bounded arithmetic tool useful for exercising arguments, tool calls,
  and non-static results.

## Calculator evaluation decision

The calculator parses in `eval` mode with the standard-library `ast` module, accepts
only numeric constants and an explicit arithmetic operator set, and applies limits to
source length, tree size, exponents, finite values, and result magnitude.

We considered validating the tree and then using Python `eval`. That is not a
fundamentally different language boundary: in either design, the AST validator defines
the accepted mini-language. The current implementation retains a small recursive
evaluator because it can enforce magnitude limits at intermediate nodes and normalize
arithmetic failures without compiling the accepted expression. This is an engineering
tradeoff, not a claim that recursive interpretation removes the need for a complete
allowlist.

## Isolated Goose setup

The test wrapper uses a Goose CLI found on `PATH` or selected through `GOOSE_BIN`. It
invokes Goose with `--no-profile` and exactly one absolute `--with-extension` path,
selecting either the official-SDK or FastMCP executable. Goose remains an independently
installed integration dependency rather than vendored project content.

The default test root is `.sandbox/goose`, selected through `GOOSE_PATH_ROOT`, with
separate `config`, `data`, and `state` directories. This leaves the user's ordinary
Goose configuration and keyring untouched. Tool-isolation tests always start a new
session because resuming a session can restore extensions associated with that
session. The wrapper does not embed a provider, model, endpoint, or credential.

## Verification performed

Tests cover three distinct boundaries:

1. in-process contract parity between the two framework adapters;
2. real MCP list-tools round trips over stdio; and
3. a real Goose process pointed at a local mock OpenAI-compatible endpoint.

The third test captures the provider request and asserts that Goose sends exactly the
two namespaced tools from the selected adapter. This is stronger than inferring
isolation from the Goose command line.

Live runs against a separately configured Ollama service also succeeded for both
adapters. For the calculator prompt, the model issued exactly one call with:

```text
expression = ((17 ** 2) + 31) / 8
tool result = 40.0
assistant reply = CALC_OK=40
```

The final project test run after the Apptainer work completed with `34 passed`. An
earlier run inside an outer development sandbox produced four false failures because
that outer sandbox prohibited local sockets/process behavior; the same suite passed
when run directly on the host.

## Deliberately deferred

There is still no read, write, or Bash MCP tool. `SANDBOXED_GOOSE_BACKEND` and
`SANDBOXED_GOOSE_WORKSPACE` reserve a configuration seam but do not activate anything.
An unavailable or incomplete sandbox backend must fail closed rather than falling back
to host execution.

The Bubblewrap and Apptainer work currently defines and probes possible security
boundaries. Framework ergonomics will be compared again once both adapters expose a
real backend-neutral operation.
