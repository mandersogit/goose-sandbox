# 2026-07-31: real-session ContextFS test

## Outcome

`session_context` now has an opt-in path that reads its approved session projection
through the actual ContextFS mount inside rootless Apptainer. The default remains the
in-process `direct` renderer so normal unit and stdio tests do not require Linux FUSE
or an installed container runtime.

The planned live-inference sequence was deliberately postponed. Instead, a dedicated
fixture was created with the real Goose CLI and a deterministic loopback OpenAI-format
provider. Its primary session was resumed for 12 user/assistant turns, producing 24
persisted messages before the tool tests. A separate Goose session in the same database
contains a decoy canary.

The fixture's host paths, generated identifiers, and canaries are stored only in an
ignored private manifest. The reusable generator and test contract contain no host or
provider-specific details.

## Tool-call path

One `apptainer-fuse` read performs this sequence:

```text
Goose or MCP client
  -> authoritative agent-session-id binding
  -> exact read-only SQLite projection on the trusted host
  -> exclusive mode-0600 bundle in a mode-0700 per-read directory
  -> fixed rootless, offline Apptainer argv
  -> fixed container: ContextFS frontend and /context mount
  -> fixed sandboxed-goose-read-context process
  -> ordinary open/list/pread operations against /context
  -> bounded validated JSON envelope
  -> Apptainer exit and private bundle removal
```

The model controls only `path`, `offset`, and `limit`. It does not select the database,
session, bundle location, image, runtime policy, bind destination, FUSE command,
mountpoint, or payload executable.

The in-image reader refuses to operate unless `/proc/self/mountinfo` identifies the
exact `/context` path as a FUSE filesystem. It opens every path component relative to
an already-open directory descriptor with `O_NOFOLLOW`, accepts only regular files and
directories, and implements the same path, UTF-8 boundary, offset, and 65,536-byte
limit contract as the direct renderer.

## Tests

Pure tests cover:

- parity between mounted-directory reads and the in-memory projection renderer;
- traversal, symlink, non-UTF-8-boundary, and non-FUSE rejection;
- fixed Apptainer/FUSE arguments and separation of model-controlled reader arguments;
- mode-`0600` bundle creation and cleanup after success or reader failure;
- strict validation of the reader's output envelope; and
- configuration parsing with fail-closed rejection of unknown transports.

The existing Apptainer integration still tests the generated toy tree and a synthetic
two-session projection, mutation rejection, namespace containment, and normal/signal
cleanup.

The opt-in real-session suite adds four cases:

1. official MCP SDK adapter reads the real root, manifest, full transcript in 1 KiB
   slices, and messages directory through fresh FUSE mounts;
2. FastMCP performs the same checks;
3. real Goose resumes the selected session, a deterministic provider requests
   `session_context`, and the official adapter returns the exact-session manifest
   through FUSE; and
4. the same real-Goose sequence runs through FastMCP.

The suite compares FUSE results with a direct trusted-host oracle, requires both the
first and last primary-turn canaries, rejects the decoy session canary, and verifies
that the host FUSE connection set and per-read run directory are unchanged afterward.

The focused result was `4 passed`; the final complete project run was `83 passed`.
The rebuilt mode-`0444` context SIF has SHA-256:

```text
23f56c3ca325e3eef1d1415b6cfbc4d3999ea6ebd580459b43ba8b92ad21b1e4
```

## Reproduction

Create a new private fixture rather than reusing “latest” session state:

```bash
local.venv/bin/python scripts/create-goose-session-fixture.py \
  --goose-bin /path/to/goose \
  --goose-root /private/test/goose \
  --output /private/test/fixture.json \
  --turns 12
```

Then run:

```bash
SANDBOXED_GOOSE_REAL_SESSION_FIXTURE=/private/test/fixture.json \
  make test-real-session-contextfs
```

## Remaining boundary

This proves the combined session binding, bundle, Apptainer, FUSE, reader, and cleanup
path under normal completion. It is not authorization for hostile Bash. The timeout
path owns and signals a process group, but a final hostile-command supervisor still
needs whole-cgroup ownership, forced descendant cleanup, quotas, seccomp, and the full
adversarial filesystem suite. Image signature/policy verification and performance
work for repeated fresh mounts also remain. No live model was used in this test.
