# Design

**Status:** draft scaffold

## Goal

Expose a small developer-tool surface to Goose over stdio MCP while ensuring that filesystem access and process execution occur inside an explicit operating-system sandbox.

The initial tool set is expected to include read, write, and shell execution. Exact schemas and any additional tools remain a design decision.

The proposed hostile-command Linux policy is documented in
[BUBBLEWRAP.md](BUBBLEWRAP.md). It is a design only; no execution tool is registered.
The rootless Apptainer host assessment, runtime policy, and initial SIF recipe are
documented in [APPTAINER.md](APPTAINER.md), also without an execution tool.

## Process boundary

```text
Goose
  └─ stdio MCP client
       └─ sandboxed-goose Python server
            ├─ official MCP SDK or FastMCP adapter
            ├─ shared tool contract and behavior
            ├─ exact-session context projector
            ├─ tool schema and request validation
            ├─ sandbox policy and lifecycle
            └─ platform backend
                 ├─ bubblewrap on Linux
                 ├─ Apptainer where required
                 └─ Seatbelt/sandbox-exec on macOS, if viable
```

Goose is launched with `--no-profile` and exactly one `--with-extension` argument. The Python process owns sandbox policy; Goose's own tool permission UI may add an approval layer but is not the security boundary.

## Decisions represented by the scaffold

- Python 3.11+ with a `src/` package layout.
- Parallel official MCP SDK v2 and FastMCP adapters over stdio.
- One shared, framework-neutral tool catalog; parity tests prevent adapter drift.
- A bounded arithmetic tool exercises arguments and non-static results without requiring sandbox access.
- End-to-end tests inspect the exact tool array Goose sends to a mock model for each adapter.
- Exact dependency pins while MCP SDK v2 and FastMCP v4 are newly released.
- One MCP server process per Goose session.
- Goose supplies the active session as `agent-session-id` MCP request metadata and as
  `AGENT_SESSION_ID` for the session-bound stdio process. Request metadata is
  authoritative; disagreement fails closed.
- The context projector opens the isolated Goose SQLite database read-only, selects
  one parameterized session, and emits only normalized content with current or
  historical agent-disclosure provenance. Both MCP adapters expose the same bounded
  `session_context` list/read tool.
- Stock, unmodified Goose is the supported runtime. The current projector excludes
  agent-invisible stock rows because it cannot distinguish archived former context from
  never-disclosed content; the launcher forces tool-pair summarization off.
- The planned defensive path uses project-owned atomic SQLite disclosure-ledger
  triggers rather than a Goose source patch. Until that lands, archived-history
  recovery is incomplete but remains fail-closed.
- No read, write, or shell tools until a real backend is selected and tested.
- Hostile Linux commands use an independent workspace snapshot, immutable runtime,
  offline network namespace, seccomp, cgroup limits, hard storage quotas, and reviewed
  export; missing controls fail closed.
- No automatic fallback to execution on the host.
- The Apptainer experiment uses the official non-setuid runtime, a custom fail-closed
  configuration, a digest-pinned arm64 SIF recipe, and the same hostile-offline
  acceptance gates as Bubblewrap.
- A separate context-enabled Apptainer profile and derivative SIF prove that an
  immutable in-image Python frontend can project a bounded, read-only in-memory tree
  at `/context`; the ordinary profile continues to reject FUSE mounts.
- A trusted host exporter materializes one mode-`0600` session-scoped bundle for each
  generation. Apptainer binds only that bundle read-only at a fixed in-image path; the
  Goose database and unrelated sessions never enter the container.
- The opt-in `apptainer-fuse` implementation of `session_context` regenerates that
  bundle for every bounded read and invokes only a fixed in-image reader. The reader
  verifies `/context` is FUSE-backed before using ordinary filesystem operations.
- Backend and workspace selection enter through environment variables so the stdio command stays simple.
- The Goose CLI remains an independently installed integration dependency rather than
  vendored project content.

## Proposed layers

1. A framework-neutral tool catalog defines public names, descriptions, and behavior.
2. Thin MCP SDK and FastMCP adapters register that same catalog.
3. MCP tools translate validated requests into backend-neutral operations.
4. A session-context broker resolves Goose request metadata and produces a bounded,
   immutable projection generation.
5. A session policy defines workspace mounts, writable paths, network access, environment variables, resource limits, and lifetime.
6. A platform backend turns that policy into a concrete sandbox process.
7. Contract tests run the same tool, escape, and persistence cases against every adapter and backend.

## Open decisions

- Whether the shared Linux kernel is an adequate boundary for the deployment or a
  microVM is required.
- Exact immutable runtime contents and supported language toolchains.
- Resource ceilings and the host storage/cgroup mechanisms used to enforce them.
- Whether the proposed synthetic one-commit Git baseline is sufficient, with original
  history available only as an explicit disclosure opt-in.
- Whether an explicitly unsafe direct-write profile should exist at all.
- Export approval policy and protected paths.
- Network remains disabled in the first design; filtered egress requires a separate
  broker design.
- Sandbox lifetime: one persistent environment per Goose session or a fresh environment per tool call?
- Tool compatibility: mirror Goose developer-tool schemas and names, or expose a smaller purpose-built API?
- Shell contract: a shell command string or argv, maximum runtime/output, process-group cancellation, and background-process policy?
- Filesystem contract: text-only or binary support, symlink handling, maximum file size, and atomic-write semantics?
- Backend priority: bubblewrap-first on Linux, Apptainer-first for HPC, and whether modern macOS can rely on Seatbelt?
- Distribution: library/CLI only, or also a packaged sandbox image/root filesystem?
