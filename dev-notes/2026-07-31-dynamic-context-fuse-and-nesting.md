# 2026-07-31: dynamic context filesystems, in-container FUSE, and nesting

> Follow-up: the first Goose session projection uses an immutable, host-generated
> bundle rather than a live socket broker. See
> [Goose session context projection](2026-07-31-goose-session-context-projection.md).
> This is sufficient for a fresh coherent generation per future sandbox invocation;
> the broker design below remains the direction for truly lazy or live data sources.

## Question and conclusion

The motivating idea is a proc-like filesystem that gives the model a familiar,
programmatic view of information without materializing a complete directory tree in
advance. Candidate sources include the agent harness's session history, tool-call
records, task-domain objects, documentation indexes, code intelligence, issue/CI state,
and constrained search results.

FUSE is the normal userspace mechanism for this. It can synthesize directory entries,
metadata, and file contents on lookup or read. It can be mounted entirely within an
Apptainer mount namespace; no host-visible FUSE mountpoint is required. Apptainer
directly supports a FUSE executable located inside the SIF with the `container:` and
`container-daemon:` modes.

The recommended design is not a monolithic in-container daemon holding broad
credentials. It is:

```text
hostile model
    |
    v
/context (read-only FUSE mount inside Apptainer)
    |
    v
small ContextFS frontend inside the approved SIF
    |
    v
session-scoped, typed RPC over a private Unix socket
    |
    v
trusted broker holding narrowly scoped data-source authority
```

The broker may run outside Apptainer without creating an outside FUSE mount. This keeps
the container's offline network property and prevents durable service credentials from
entering the hostile process domain. The broker must assume the model can speak its
protocol directly; FUSE is a convenient interface, not an authorization boundary.

If every component must live inside a single outer Apptainer, start ContextFS under a
trusted init and then place model code in a child PID/user/mount sandbox. A small
Bubblewrap or purpose-built namespace launcher is preferable to nesting a complete
second Apptainer.

## Why a filesystem projection is interesting

Agents already know how to explore files with `ls`, `find`, `rg`, `sed`, `jq`, Python,
and ordinary libraries. A projection can make a structured data source usable without
adding a new MCP schema and specialized client logic for every domain.

Useful examples include:

```text
/context/
  manifest.json
  README.md
  session/
    transcript.md
    messages/
      by-source-row/
        00000000000000000001.json
        00000000000000000002.json
    tool-calls/
      000001.json
    artifacts/
  task/
    brief.md
    constraints.json
    objects/<stable-id>/metadata.json
  code/
    symbols/<language>/<qualified-name>.json
    references/<symbol-id>.jsonl
    dependency-graph.json
  docs/
    by-id/<document-id>/metadata.json
    by-id/<document-id>/content.md
  ci/
    runs/<run-id>/summary.json
    runs/<run-id>/logs/<job-id>.txt
```

This is strongest for stable, hierarchical, mostly read-only data. FUSE is a poor
substitute for every RPC. Arbitrary search expressions, SQL, URLs, or expensive
computations encoded into pathnames create awkward quoting, caching, authorization,
and denial-of-service behavior. A narrow tool or CLI can complement the filesystem for
parameterized searches, while the results and referenced objects appear under stable
read-only paths.

Other projection approaches remain useful in narrower cases:

| Mechanism | Best use | Limitation |
| --- | --- | --- |
| Prebuilt files or tmpfs materialization | Small immutable snapshots | Up-front cost and stale copies |
| FUSE | Lazy hierarchical read-only projection | Daemon lifecycle, kernel/FUSE surface, unusual I/O semantics |
| Narrow MCP/CLI query tool | Parameterized search or mutation | Less composable with ordinary file tools |
| Unix socket API | Efficient structured internal transport | Model can call it directly if reachable |
| SIF/SquashFS data partition | Signed immutable bulk corpus | Must be constructed before launch |
| virtiofs or 9p | Projection across a VM boundary | Requires a VM and host-side service |

`procfs` and `sysfs` are kernel filesystems; an ordinary userspace program cannot add
its own arbitrary entries to them. FUSE supplies the proc-like behavior without a
custom kernel module.

## Session-history projection

The agent harness, rather than ChatGPT or Apptainer, is the authoritative owner of the
session history it can expose. For Goose this can include normalized messages, tool
requests/results, user-visible artifacts, timestamps, and session metadata captured by
the harness or MCP integration.

A history projection must define an explicit disclosure contract. It should not expose:

- hidden system or developer instructions not already intended as model input;
- chain-of-thought or internal reasoning traces;
- provider credentials, request headers, connector tokens, or unrelated environment;
- raw telemetry containing host paths, identities, or secrets;
- other sessions merely because the harness can read them; or
- unbounded tool output that was previously truncated for the model.

Recommended properties:

- one immutable or append-only snapshot generation per command;
- monotonically numbered messages and tool calls;
- both human-readable Markdown and lossless bounded JSON where useful;
- an explicit schema/version and snapshot identifier in `manifest.json`;
- provenance and original truncation markers on every derived object;
- deterministic ordering, stable inode/path identity within a generation;
- redaction performed by the trusted broker before data reaches ContextFS; and
- no write path from the model back into authoritative history.

Historical messages and task-domain documents are untrusted content and may contain
prompt injection. Files should carry provenance metadata, and prompts/tools should
describe them as data rather than policy. Filesystem placement does not make their
instructions trustworthy.

## Mounting FUSE entirely inside Apptainer

Apptainer's `--fusemount` definition has three components:

```text
--fusemount <type>:<fuse-command-and-arguments> <container-mountpoint>
```

Relevant modes are:

- `container`: executable inside the SIF, attached to the foreground container;
- `container-daemon`: executable inside the SIF, detached from the foreground
  process;
- `host` and `host-daemon`: executable on the host, which are not needed for the
  all-inside mount design.

The driver must use libfuse3 3.3 or newer because Apptainer passes it a pre-opened FUSE
file descriptor. A conceptual trusted invocation is:

```text
apptainer
  --config <trusted-context-enabled-config>
  exec
  <all ordinary hostile-offline flags and fixed mounts>
  --fusemount
    "container:/usr/libexec/contextfs --socket /run/context-broker.sock /context"
  <approved.sif>
  /usr/libexec/sandbox-init <sealed-command>
```

This is only illustrative. The supervisor must construct an argv vector directly; it
must not paste this representation into a host shell. The exact FUSE option parsing,
socket placement, startup readiness, error handling, and cleanup require an integration
test against the installed Apptainer version.

For the current per-command model, use attached `container:` mode. It ties the daemon's
lifetime to the foreground execution and makes cleanup easier. Use
`container-daemon:` only with an explicit session instance whose start, health,
authorization rotation, and stop behavior are supervised.

The current `apptainer-hostile.conf` intentionally has:

```text
enable fusemount = no
```

Do not weaken that default profile globally. Add a second trusted configuration, for
example `apptainer-hostile-context.conf`, that differs only by enabling FUSE and any
strictly necessary device setup. The trusted launcher selects the context profile only
when it also supplies the immutable ContextFS executable, fixed mountpoint, fixed
broker endpoint, quotas, and tests. The model still receives no control over
`--fusemount` arguments.

The SIF should contain the ContextFS executable and an existing `/context` mountpoint.
The hostile payload should not contain the Apptainer runtime itself, a general
`fusermount` workflow exposed as a tool, or alternative FUSE drivers that the model can
select.

## What an inside mount does and does not isolate

An inside mount provides useful mount-namespace containment:

- no host path is occupied by the ContextFS mount;
- ordinary host processes do not traverse it accidentally;
- cleanup is tied more closely to the container lifecycle;
- the frontend binary is supplied by the immutable SIF; and
- only the intended container namespace sees `/context`.

It does not move FUSE off the host kernel. The kernel FUSE client and `/dev/fuse` remain
host-kernel interfaces. Model code can issue arbitrary path operations and therefore
drive both the kernel path and the userspace daemon with adversarial request sequences.

It also does not separate two same-UID processes inside the container. If ContextFS and
the model share a user namespace, PID namespace, cgroup, and numeric UID, assume the
model can at least:

- kill, stop, or resource-starve ContextFS;
- inspect its process metadata and open descriptors through the visible parts of
  `/proc`;
- attempt ptrace or process-memory operations unless separately blocked;
- fuzz every implemented filesystem operation and sequence;
- exploit a frontend bug and inherit its available files, sockets, or credentials;
- keep references and operations outstanding during shutdown; and
- induce hangs, request-queue congestion, reentrancy bugs, or deadlocks.

Same-UID ptrace is affected by `NoNewPrivs`, dumpability, Yama, seccomp, Landlock, and
PID relationships, so a particular primitive may fail on a particular host. The design
must not treat that incidental failure as the service boundary. Separate the processes
architecturally.

The Linux FUSE documentation identifies unresponsive daemons, accidental and malicious
deadlocks, and resource-exhaustion behavior as explicit concerns. It provides a FUSE
control filesystem with an `abort` operation for hung connections. The supervisor
should retain a trusted way to abort the connection and kill the whole affected
process/cgroup tree.

## Authority and risk profile

The decisive question is not where the daemon binary runs. It is what authority becomes
available if the model compromises or impersonates it.

| ContextFS authority | Consequence of frontend compromise | Assessment |
| --- | --- | --- |
| Only data already approved for this model/session | Disclosure already in scope plus denial of service | Reasonable initial experiment |
| Narrow read capability to one redacted session | Remaining authorized session data can be enumerated | Usually acceptable with byte/object limits |
| Broad harness history store | Other sessions and hidden metadata may leak | Not acceptable |
| Host filesystem access | All files readable by the host account may leak | Not acceptable |
| Database/cloud/provider credential | Full credential scope may be exercised or exfiltrated | Not acceptable |
| Mutation authority | Model may corrupt authoritative task or history data | Exclude from initial design |

For a same-domain in-container daemon, treat all daemon-readable secrets and all daemon
authority as model-readable. `chmod`, process names, and an undocumented socket protocol
do not change that conclusion.

The host-risk delta from enabling FUSE is bounded but nonzero:

- an additional kernel interface and filesystem implementation become reachable;
- malformed or high-volume operations can consume kernel and daemon memory/CPU;
- hung requests complicate timeout and unmount behavior; and
- filesystem semantics can surprise trusted code that accidentally traverses the
  mount.

Keeping the mount in the private namespace limits which processes can encounter those
semantics. It does not provide a new kernel boundary. For especially high-value hosts,
put the whole experiment in a microVM.

## Recommended frontend/broker split

The in-container frontend should be deliberately boring. It translates bounded file
operations into a small typed protocol and holds no durable credentials. The broker
owns policy and data-source access.

Example protocol operations:

```text
GetManifest(session_capability, snapshot_id)
List(session_capability, snapshot_id, object_id, cursor, limit)
Stat(session_capability, snapshot_id, object_id)
Read(session_capability, snapshot_id, object_id, offset, length)
Search(session_capability, snapshot_id, approved_index, query, cursor, limit)
```

The protocol should use opaque object IDs rather than host paths. The broker must:

- authenticate a random, unguessable, session-specific capability;
- bind it to one user/session/task, projection policy, expiry, and byte budget;
- enforce authorization on every request rather than trusting frontend path checks;
- expose no arbitrary host path, SQL, shell, URL, template, or plugin dispatch;
- cap request size, response size, directory entries, pagination, concurrency, total
  bytes, backend time, and cache size;
- canonicalize and validate identifiers independently of FUSE;
- redact before returning data;
- avoid following data-source symlinks or aliases outside the authorized object set;
- log metadata sufficient for accounting without copying sensitive contents; and
- revoke the capability when the command/session ends.

A private pathname Unix socket can be bound into a private `/run` for the frontend. The
model may still open it, so the protocol must withstand an arbitrary hostile client. A
pre-opened socket pair would reduce namespace-visible endpoints, but Apptainer's exact
file-descriptor preservation behavior must be verified before relying on it. Do not
weaken the inherited-FD allowlist speculatively.

If the broker fails, ContextFS should return a bounded, ordinary error such as `EIO` or
`ETIMEDOUT`; it must not retry forever. A broker outage must not disable command
timeouts or cgroup cleanup.

## Filesystem contract and defenses

The initial projection should be read-only and non-executable. Where supported, mount
with or enforce semantics equivalent to:

```text
ro,nodev,nosuid,noexec
```

Unprivileged FUSE mounts receive `nodev` and `nosuid` protections from the standard
mount helper, but the design should still request and verify all intended options.
`allow_other` should remain disabled. No generated file should carry meaningful setid,
device, security-xattr, ownership, ACL, or executable semantics.

Bound every externally influenced dimension:

- pathname bytes, components, and depth;
- number of directory entries and pagination size;
- file logical size and bytes returned per read;
- symlink count and target length, or omit symlinks entirely;
- xattrs, if supported at all;
- outstanding and background requests;
- daemon threads/tasks, memory, CPU, descriptors, and cache;
- data-source latency, retries, and total operation time;
- snapshot lifetime and aggregate bytes read; and
- retained logs and error detail.

Prefer simple regular files and directories. Initially return errors for mutation,
hard links, special files, mmap, locking, writable open modes, xattrs, and unusual
IOCTLs unless a concrete consumer requires them. Never make daemon code or its backing
database depend on paths inside its own FUSE mount; that is a common route to
reentrancy and deadlock.

Use snapshot semantics. A directory listing followed by `stat` and `read` should refer
to one coherent generation even if the underlying harness history grows. New data can
appear in a later command or explicit snapshot generation. Stable object IDs should
drive inode assignment; inode reuse and changing file contents under the same inode can
confuse caches and tools.

## Stronger all-inside separation

When the frontend must remain inside the outer Apptainer, the preferred process tree is:

```text
outer rootless Apptainer namespace
  trusted sandbox-init
    |- ContextFS frontend (parent PID domain, no credentials)
    `- inner minimal sandbox
         `- model-controlled shell and descendants
```

Sequence:

1. The outer Apptainer establishes the immutable root, private mounts, network
   namespace, cgroup ceiling, and FUSE connection.
2. Trusted `sandbox-init` validates broker connectivity and ContextFS readiness.
3. It creates child PID/user/mount namespaces for the hostile command, inheriting or
   explicitly exposing `/context` read-only.
4. It applies a child-only Landlock policy and drops all capabilities.
5. It installs the hostile seccomp filter only after the trusted namespace setup is
   complete.
6. It launches the sealed command and supervises it from outside the child PID
   namespace.

This requires a two-stage syscall policy. An Apptainer-level seccomp profile applies to
the trusted init as well as its descendants; if that profile already denies `unshare`
and mount-namespace setup, the init cannot create the child boundary. The outer payload
profile must permit only the setup operations required by the trusted init, after which
the init installs the stricter hostile filter in the child before executing model code.
That larger setup-phase syscall surface is part of the trusted computing base and must
be tested explicitly.

Because a process in a child PID namespace cannot see or signal processes in its parent
PID namespace by ordinary PID operations, this materially improves ContextFS
availability and process secrecy. Seccomp should additionally block ptrace,
process-VM, pidfd-FD extraction, namespace creation, mount mutation, and relevant
IOCTLs. Landlock can restrict filesystem access and, on sufficiently new ABIs, signal
and Unix-socket scope.

Running the two processes under distinct inner UIDs is possible only with a suitable
UID/GID map. The current non-fakeroot hostile runtime should not be expanded to a broad
subordinate-ID/fakeroot mapping solely for this. PID namespaces, child-only seccomp and
Landlock, absence of daemon credentials, and a broker boundary provide a clearer first
design.

The outer Apptainer cgroup limits both services together. If the frontend needs an
availability guarantee while the model is CPU- or memory-hostile, create delegated
child cgroups from the trusted supervisor so the frontend retains a bounded reserve.
This must be arranged before releasing model code and verified on the deployment host.

## Can Apptainer be nested?

Technically, yes, when the outer environment permits the inner runtime's prerequisites.
An inner rootless Apptainer generally needs:

- an inner `apptainer` executable, helpers, config, cache/tmp, and an image;
- permission to call `unshare` and create another user and mount namespace;
- the system paths needed for PID namespace setup;
- access to `/dev/fuse` for unprivileged SIF/FUSE mounts;
- seccomp rules that permit the inner runtime's setup syscalls;
- compatible user-namespace and UID/GID mapping behavior; and
- explicit control over which outer mounts and environment reach the inner container.

Apptainer's documentation for running inside Docker illustrates these requirements:
rootless nesting needs `unshare` to work, system-path restrictions relaxed for PID
namespaces, and `/dev/fuse` exposed. A nested Apptainer inside Apptainer faces the same
kernel primitives even though the outer runtime differs.

The present hostile profile intentionally removes or blocks much of this. Making inner
Apptainer work would require loosening exactly the namespace, mount, FUSE, executable,
and configuration controls that the model sandbox is trying to minimize.

Nesting also has an Apptainer-specific mount hazard. Paths bound into an outer
container are recorded in `$APPTAINER_BIND` and are automatically rebound by a nested
invocation. Every sensitive outer bind must use the `nonested` option, and a trusted
inner launcher must clear `APPTAINER_*` and `SINGULARITY_*` variables. The current
launch contract already uses `nonested`; that remains defense in depth even though the
approved SIF contains no inner Apptainer binary.

### Nesting risk profile

Nesting creates more namespaces, but it does not create another kernel. Its net value
depends on who controls the inner launch:

| Inner launch | Effect |
| --- | --- |
| Model can invoke inner Apptainer and choose arguments | Unacceptable; exposes an image/mount/namespace/config gadget |
| Trusted init invokes fixed inner Apptainer argv | Can isolate child processes, but with substantial runtime complexity |
| Trusted init invokes a minimal namespace helper | Similar useful child isolation with a smaller trusted computing base |
| Inner Apptainer runs in a microVM | Kernel boundary comes from the VM, not from nesting |

Costs of full nesting include:

- a second runtime and image parser in the trusted computing base;
- more permissive outer seccomp and device exposure during setup;
- more user/mount/PID namespaces and FUSE connections;
- automatic bind/environment propagation hazards;
- nested cache, temporary directory, instance, and cleanup state;
- harder cgroup and process ownership accounting; and
- operational fragility across kernel, Apptainer, and host configuration versions.

Nesting is reasonable for CI that tests Apptainer workflows, or when running a portable
workflow whose nested runtime behavior is itself required. It is not the preferred way
to protect ContextFS from a hostile model. A purpose-built child launcher or Bubblewrap
inside one outer Apptainer is smaller and easier to audit.

Never solve nested-runtime failures by launching the outer container privileged. That
would reverse the security objective of this project.

## Recommended implementation experiments

### Experiment 1: minimal read-only ContextFS

- Add a small libfuse3 frontend to a test-only SIF.
- Project a fixed in-memory manifest and a bounded read-only object tree.
- Launch it with attached `container:` mode and no external broker.
- Verify the mount is visible inside the container and absent from the host mount
  namespace.
- Verify mount options, no `allow_other`, process/PID relationships, `/dev/fuse`
  visibility, and cleanup after normal exit, timeout, SIGKILL, and supervisor crash.

This experiment validates Apptainer mechanics only; it does not authorize live data
access.

### Experiment 2: hostile filesystem semantics

Run adversarial clients that:

- issue concurrent lookup/stat/open/read/close sequences;
- request invalid offsets, giant reads, deep paths, and nonexistent objects;
- abandon file descriptors and processes while I/O is pending;
- send signals during reads and force timeouts/cancellation;
- attempt writes, mmap, locks, xattrs, links, renames, devices, and execution;
- flood directory enumeration and repeated misses;
- crash or hang the daemon and broker; and
- attempt to keep the mount or daemon alive after command exit.

Assert bounded memory/CPU/descriptors, timely errors, abortability, and complete cleanup.

### Experiment 3: scoped broker

- Bind a private Unix socket into `/run`.
- Give the broker one synthetic session capability and a small object corpus.
- Call the broker both through FUSE and directly with malformed protocol requests.
- Verify authorization, redaction, pagination, aggregate byte budgets, expiration,
  revocation, logging, and failure behavior.
- Confirm that compromising the frontend yields no host paths or durable credentials.

### Experiment 4: parent/child isolation

- Start ContextFS under `sandbox-init` in the outer PID namespace.
- Launch the model test process through a minimal child PID/user/mount namespace.
- Apply child-only Landlock and the intended hostile seccomp filter.
- Demonstrate that the child can read `/context` but cannot enumerate, signal, ptrace,
  inspect descriptors of, or join namespaces belonging to ContextFS/init.
- Stress resource contention and decide whether separate delegated cgroups are needed.

Only after these pass should the context-enabled profile be considered for an MCP tool.

## Decision record

For this project:

- **Yes:** keep a FUSE mount entirely inside Apptainer using `container:` mode.
- **Yes:** keep the data-access broker outside while leaving the mount inside.
- **Yes:** use FUSE for stable, lazy, read-only projections and ordinary file-tool
  compatibility.
- **Yes:** complement it with a narrow query tool for parameterized or expensive
  operations.
- **Yes:** use a trusted init plus a small child namespace/Landlock/seccomp boundary if
  ContextFS must be protected from model processes.
- **Maybe later:** use an Apptainer instance for a genuinely session-scoped service,
  with explicit lifecycle supervision.
- **No by default:** give an in-container ContextFS broad credentials or host access.
- **No:** allow the model to invoke Apptainer or select FUSE drivers/arguments.
- **No by default:** nest a full Apptainer merely to isolate ContextFS.
- **Use a microVM:** when compromise of the shared host kernel is outside the accepted
  risk profile.

## Sources consulted

- [Apptainer FUSE mount modes and libfuse requirements](https://apptainer.org/docs/user/latest/bind_paths_and_mounts.html#fuse-mounts)
- [Apptainer bind behavior, including nested `$APPTAINER_BIND`](https://apptainer.org/docs/user/latest/bind_paths_and_mounts.html#user-defined-bind-paths)
- [Apptainer `exec` options, including `nonested`](https://apptainer.org/docs/user/latest/cli/apptainer_exec.html)
- [Apptainer instances and persistent services](https://apptainer.org/docs/user/latest/running_services.html)
- [Apptainer requirements when nested inside Docker](https://apptainer.org/docs/admin/latest/installation.html#running-inside-docker)
- [Apptainer user namespaces and rootless fakeroot constraints](https://apptainer.org/docs/admin/latest/user_namespace.html)
- [Linux kernel FUSE overview, control interface, DoS, and deadlock discussion](https://docs.kernel.org/filesystems/fuse/fuse.html)
- [Linux Landlock documentation](https://docs.kernel.org/userspace-api/landlock.html)
- [Bubblewrap security-model documentation](https://github.com/containers/bubblewrap)
