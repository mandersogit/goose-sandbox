# 2026-07-31: ContextFS Apptainer proof

## Outcome

A Python `pyfuse3` process stored inside an immutable SIF successfully serves a
programmatically generated filesystem at `/context` through Apptainer 1.5.3 attached
`container:` FUSE mode. The proof uses no backing directory, broker, host path, live
session data, or credentials.

The generated tree is:

```text
/context/
  README.md
  manifest.json
  generated/
    primes.json
    squares.json
  objects/answer/
    content.txt
    metadata.json
```

`manifest.json` identifies snapshot `toy-v1`, records `generated-in-memory` storage,
and contains the size and SHA-256 digest of every payload file. The integration test
recomputes those values through the mounted filesystem.

## Components added

- `sandboxed_goose.contextfs.model` builds bounded immutable snapshots, validates
  paths and sizes, assigns deterministic inodes, and performs bounded reads.
- `sandboxed_goose.contextfs.fuse` implements the read-side pyfuse3 operations and
  rejects mutation.
- `sandboxed-goose-contextfs` accepts only Apptainer's attached `/dev/fd/3 -f`
  argument shape.
- `sandbox-python-context-arm64.def` layers the project wheel and the snapshot-pinned
  Ubuntu `python3-pyfuse3` package over the existing base SIF.
- `apptainer-hostile-context.conf` enables FUSE without weakening the ordinary hostile
  profile.
- The build and test scripts create the derivative SIF and exercise the real mount.

## Environment and artifact

- Host architecture: arm64
- Apptainer: 1.5.3, non-setuid
- Runtime identity: UID/GID 1000:1000
- Ubuntu snapshot: `20260730T000000Z`
- `python3-pyfuse3`: 3.3.0-0.1
- `libfuse3-3`: 3.14.0-5build1
- `python3-trio`: 0.24.0-1ubuntu1
- Proof SIF SHA-256:
  `e1c5afcf53a4de0ee57d5befc6561593e1de27a77c39df7e3494816785d58b67`

The SIF and checksum are generated under `.sandbox/apptainer/images/` and remain
Git-ignored.

## Verified behavior

The test demonstrated that:

- the ordinary hostile profile rejects `--fusemount` because FUSE remains disabled;
- the context profile launches the immutable frontend as
  `/usr/local/bin/sandboxed-goose-contextfs /dev/fd/3 -f`;
- projected files can be traversed with Bash, `jq`, Python, and ordinary file reads;
- file creation, deletion, and chmod fail;
- `/dev/fuse`, `fusermount3`, and Apptainer are absent from the payload filesystem;
- `/context` is visible inside the container but no corresponding mount appears in the
  host mount namespace;
- `allow_other` is absent;
- foreground exit restores the host FUSE connection set exactly; and
- a payload terminating itself with SIGTERM also restores the connection set.

The observed mountinfo entry was structurally:

```text
... / /context rw,nosuid,nodev,relatime - fuse fuse rw,user_id=1000,group_id=1000
```

The mount is therefore not kernel-read-only or `noexec`. The frontend enforces the
current read-only contract through projected `0444`/`0555` modes and mutation errors.

The frontend PID and its `/dev/fd/3 -f` command line are visible to the payload. This
confirms the expected same-PID-namespace limitation and the need for the later trusted
init plus child sandbox design.

## Integration findings

Two failures refined the implementation:

1. A wheel copied to `/tmp` during `%files` was hidden by the private build-time `/tmp`
   bind. The derivative definition now stages it under `/opt` and removes it after
   installation.
2. Supplying `max_read=262144` from pyfuse3 failed because Apptainer had already
   mounted the FUSE channel with a different parent-side maximum. The frontend now
   accepts the parent mount negotiation and applies its own per-operation read bound.

Apptainer creates the mount before the driver initializes, so frontend `fsname` and
`subtype` options do not determine the mountinfo source/type; it appears as `fuse
fuse` on this host.

An exploratory host-side SIGTERM sent only to a background launch wrapper was also
inadequate: two connections initially remained, and an orphaned `squashfuse_ll`
continued holding the SIF connection. The exact process created by the experiment was
terminated and the original connection set was restored. This was not retained as an
automated test because intentionally reproducing a leak is unsafe. It establishes that
the real supervisor must own the whole cgroup/process tree and not equate wrapper exit
with sandbox cleanup.

## Remaining boundary

This validates FUSE mechanics, immutable packaging, the FD handoff, namespace-local
mounting, basic read-only semantics, and orderly cleanup. It does not validate a live
broker, direct hostile broker clients, frontend protection from the model, cgroup-wide
forced cleanup, kernel-level `ro,noexec`, or the complete adversarial filesystem suite.
No MCP tool should depend on ContextFS until those later gates pass.
