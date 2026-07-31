# 2026-07-31: Goose session context projection

> **Historical prototype note:** This document records the first projection and its
> live verification, including an experimental Goose provenance patch. The supported
> runtime contract is now stock, unmodified Goose. Stock Goose safely exposes only
> currently agent-visible rows; durable recovery of rows later hidden by summarization
> or compaction awaits the project-owned disclosure ledger described in
> `2026-07-31-tool-pair-summarization-hardening-plan.md`. The patch remains optional
> development evidence, not an installation, runtime, or acceptance prerequisite.

## Outcome

The toy ContextFS generator now has a session-scoped input that projects the current
Goose conversation and safely identified pre-compaction history as bounded, read-only
files. The same virtual tree can be explored through the `session_context` MCP tool in
either framework adapter and mounted at `/context` inside the context-enabled
Apptainer image.

No Goose patch was needed for exact-session binding. Inspection through the
repository's `goose-dev` source and binary showed that Goose already supplies the exact
active session in two places:

- `agent-session-id` in the MCP request `_meta`; and
- `AGENT_SESSION_ID` in the environment of a session-bound stdio extension.

Request metadata is authoritative. If both values exist and disagree, the tool fails
closed. There is no “latest session” fallback, database scan for a plausible session,
or model-supplied session selector.

Historical recovery did require a small Goose provenance change. The standalone patch
at `patches/goose/0001-preserve-agent-visible-history-provenance.patch` adds
`historicallyAgentVisible` and records it only when compaction or tool-pair
summarization archives a row that is currently agent-visible. `make goose-patches`
applies the patch idempotently to `GOOSE_SOURCE_DIR` or the repository's `goose-dev`
checkout. It was verified against Goose commit
`ee61c7c499dbf08786a75948d949639cbab14150` (version `1.45.0`).

## Projected shape

One immutable generation contains:

```text
/context/
  README.md
  manifest.json
  session/
    transcript.md
    messages/
      by-source-row/
        00000000000000000001.json
        ...
    events/
      by-source-row/
        00000000000000000001-000001.json
        ...
```

Messages are ordered chronologically. An event is one normalized content block from a
projected message. Filenames use immutable source-row IDs, while ordinal fields describe
the current snapshot and may change after compaction. The manifest records the exact
session ID, deterministic snapshot ID, source and visible row counts, truncation state,
omission counters, limits, file sizes, and SHA-256 digests.

`session_context` accepts a virtual path, byte offset, and maximum read size. A
directory path returns a listing; a file returns at most 65,536 bytes and a continuation
offset. This provides useful read-only access before a general sandboxed filesystem or
Bash tool is enabled.

## Disclosure policy

The trusted projector opens Goose's `sessions.db` with SQLite `mode=ro`,
`PRAGMA query_only`, a short busy timeout, and an exact parameterized session query.
It never copies or binds the database into the hostile container.

The projection includes only rows whose valid Goose metadata says either
`agentVisible: true` or `historicallyAgentVisible: true`. Each message and event records
whether its disclosure is `current` or `historical`. Within those rows the projector
keeps content with no audience restriction or an assistant audience. It normalizes
text, tool requests, tool results, action requests, notifications, and resource
references while excluding:

- rows with neither current nor historical agent-disclosure provenance;
- content scoped only to the user;
- thinking, reasoning, and redacted-thinking payloads;
- provider metadata, MCP `_meta`, signatures, and structured tool output not present
  in the model-visible content list;
- binary image, audio, and resource payloads;
- session configuration, provider/model settings, token usage, cost, and telemetry;
- every other Goose session; and
- malformed, oversized, over-deep, or excess content beyond fixed limits.

Session files are untrusted data and the projected README says so explicitly. Their
placement under `/context` does not grant them policy authority.

## Compaction behavior

Goose compaction makes original rows agent-invisible and adds an agent-only summary and
continuation. The patch replaces the two compaction-time uses of
`with_agent_invisible()` with `with_agent_archived()`. That helper first records whether
the row was actually agent-visible and then clears current visibility. Rows already
hidden from the model therefore do not acquire disclosure authority.

The projector can now return both the archived originals and the current summary after
compaction. This is intentionally not retroactive: legacy rows compacted before the
patch have no trustworthy discriminator and remain excluded. An unpatched Goose is
therefore safe but provides only its current inference view.

## Apptainer transfer

The trusted host exporter serializes only approved UTF-8 files into an exclusive
mode-`0600` bundle. The format is size-bounded and strictly validated before ContextFS
constructs its immutable inode tree. A trusted launcher binds that single file
read-only at `/run/sandboxed-goose/session-context.json` and selects a fixed frontend
argument. The model does not choose the database, session ID, bundle path, FUSE command,
or mountpoint.

Because overlay and underlay are disabled, Apptainer cannot synthesize a file-bind
destination. The immutable SIF must contain an empty
`/run/sandboxed-goose/session-context.json` mountpoint in advance; creating only its
parent directory is insufficient.

The bundle is readable in the hostile domain and contains exactly the same authority
as the FUSE tree. FUSE supplies filesystem ergonomics, not an authorization boundary.
A future per-command launcher should export a fresh bundle immediately before each
sandbox invocation and remove it during whole-cgroup cleanup.

## Verification

Automated tests prove that:

- both FastMCP and the official MCP SDK receive and enforce the same session binding;
- a real local Goose binary calls `session_context` through each adapter and the
  returned manifest matches the sole active session created by that run;
- a two-session SQLite fixture cannot disclose the non-selected session;
- a historical row with explicit prior-disclosure provenance remains available while
  a legacy/user-only invisible row does not;
- user-audience blocks, thinking, provider metadata, `_meta`, and structured internal
  results are absent;
- normalized user/assistant text, tool requests/results, and an agent-only compaction
  summary remain available;
- bundle writes are exclusive and mode `0600`, and decoding revalidates the complete
  tree;
- both the static toy tree and session tree mount inside Apptainer and reject mutation;
- the ordinary hostile profile still rejects FUSE;
- the payload still lacks `/dev/fuse`, `fusermount3`, and Apptainer; and
- both mount modes restore the original host FUSE connection and mount sets.

Focused Goose tests also prove that ordinary invisibility does not create historical
authority, full compaction preserves provenance, and all archived tool-pair messages
retain it. The patched portable Goose CLI builds successfully.

The rebuilt context-enabled SIF is mode `0444` with SHA-256:

```text
23f56c3ca325e3eef1d1415b6cfbc4d3999ea6ebd580459b43ba8b92ad21b1e4
```

The opt-in `apptainer-fuse` transport is now also covered with a durable 12-turn
session created and resumed by the real Goose CLI. A local mock-provider test exercises
the real Goose session manager, database, stdio MCP transport, metadata injection,
both Python adapters, fresh Apptainer launches, the actual `/context` FUSE mount, and
tool response persistence without sending session material to an external model
endpoint. The separate live-inference test remains postponed.

## Remaining boundary

This remains a projection and transport proof, not authorization to register hostile
Bash. The missing gates include the final sandbox supervisor, whole-cgroup lifetime,
hard workspace/scratch quotas, default-deny seccomp, comprehensive hostile filesystem
tests, and protected broker/supervisor lifecycle integration. Pre-patch compacted rows
remain intentionally unrecoverable because they lack safe disclosure provenance.
