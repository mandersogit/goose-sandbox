# Bubblewrap sandbox for hostile commands

**Status:** proposed design for review; not implemented and not exposed as an MCP tool.

## Decision

The first Linux backend will provide one security profile named `hostile-offline`.
It assumes that every command and every byte written to the workspace is controlled
by an actively hostile language model.

The profile is intended to be genuinely useful for offline development work:

- run Bash, Git, Python, build tools, and test suites from a curated runtime;
- inspect and modify a persistent working copy over multiple tool calls;
- capture bounded stdout, stderr, exit status, and termination details; and
- review a file-level change set before exporting anything to the real checkout.

It will not give the command direct access to the original workspace, host network,
credentials, user home, host services, or arbitrary host paths.

Bubblewrap is a low-level mechanism, not a complete policy. Its maintainers explicitly
place responsibility for the security model on the program constructing its arguments.
This document is that policy for `sandboxed-goose`.

## Security claim

Assuming a supported, fully patched Linux kernel, a trusted runtime image, a correctly
loaded seccomp policy, and working resource and storage limits, a hostile command should:

1. read only the immutable runtime and the session workspace snapshot;
2. write only to quota-limited session storage and bounded temporary filesystems;
3. see no host processes, IPC objects, devices beyond Bubblewrap's private `/dev`,
   host Unix sockets, or external network;
4. receive no ambient credentials or inherited host file descriptors;
5. be unable to create nested user or mount namespaces;
6. be unable to exhaust host memory, process IDs, CPU indefinitely, output buffers,
   or disk space beyond configured limits;
7. lose all descendant and background processes when a command ends or times out; and
8. leave the original host workspace unchanged until a separate reviewed export.

This is a containment boundary against hostile user-space code. It is not a boundary
against a Linux kernel vulnerability reachable through an allowed syscall. Workloads
that require protection from kernel exploits, or that run beside high-value secrets,
need a VM or microVM boundary instead.

## Assets and adversary

Protected assets:

- all host files outside the explicitly prepared session snapshot;
- the original checkout, including uncommitted work;
- SSH, cloud, Git, package-manager, and model-provider credentials;
- host processes, agents, D-Bus, Docker/Podman, display servers, and other sockets;
- host and local-network services; and
- host availability.

The adversary may submit arbitrary shell text, generate native executables, exploit
parsers in the runtime, create symlinks and unusual filenames, fork, allocate memory,
fill filesystems, flood output, leave background processes, and deliberately attempt
namespace or kernel attacks.

Everything copied into `/workspace`, and all command output, is considered disclosed to
the language model and its inference provider. Filesystem isolation cannot keep a secret
from an adversary that is intentionally allowed to read that file.

## Boundary and lifecycle

```text
Goose / hostile model
        │ command, relative cwd, bounded timeout
        ▼
trusted MCP server
        │
        ├── trusted policy validator
        ├── workspace snapshot + export manager
        ├── cgroup/quota/output supervisor
        └── sealed command and seccomp file descriptors
                    │
                    ▼
                bubblewrap
                    │ fresh namespaces per command
                    ▼
          bash inside immutable runtime
                    │
                    ▼
        persistent session workspace copy
```

The filesystem snapshot persists for one Goose/MCP session. Each Bash invocation gets
fresh user, mount, PID, IPC, network, UTS, and cgroup namespaces. This preserves useful
file changes while ensuring that daemons and background processes do not persist between
calls.

The model controls only:

- command text;
- a working directory expressed relative to `/workspace`;
- optional opaque stdin bytes up to the configured limit; and
- a requested timeout no larger than the policy maximum.

The model cannot select mounts, the runtime image, environment variables, network mode,
resource ceilings, seccomp policy, workspace source, or export destination.

The working directory is resolved by the trusted parent while no sandbox command is
running. It rejects NUL bytes, absolute paths, `..` components, excessive path length
or depth, and anything that does not resolve to an existing directory beneath the
workspace FD using descriptor-relative, no-magic-link resolution. Commands for one
session are serialized so the validated directory cannot be replaced by another
sandbox process before launch.

## Runtime filesystem

The sandbox must use a purpose-built root filesystem, not `--ro-bind / /` and not a
collection of the user's host directories. Binding the host root read-only would still
disclose readable home files, configuration, and machine state.

The runtime is selected by trusted configuration and identified by a cryptographic
digest. It is not writable by the sandbox or by the source workspace. A useful initial
runtime should contain:

- Bash, coreutils, findutils, grep/ripgrep, sed, awk, diff, patch, tar, and compression;
- Git with a sanitized system configuration;
- Python and common build/test tooling; and
- CA certificates for package verification, even though the default profile has no
  network.

Additional language runtimes can be separate digest-pinned images selected before the
session starts. Runtime images must contain no setuid/setgid files, file capabilities,
credentials, service sockets, package-manager credentials, or unnecessary device nodes.

The mount layout is:

| Sandbox path | Source | Access | Lifetime |
| --- | --- | --- | --- |
| `/` | digest-verified runtime root | read-only, nodev | deployment |
| `/workspace` | independent session snapshot | read-write, hard quota | MCP session |
| `/cache` | empty session cache | read-write, hard quota | MCP session |
| `/home/sandbox` | empty session home | read-write, hard quota | MCP session |
| `/tmp` | private tmpfs | read-write, size-limited | command |
| `/run` | private tmpfs plus sealed command file | read-write except command | command |
| `/proc` | new procfs in the PID namespace | kernel-provided | command |
| `/dev` | Bubblewrap private minimal device filesystem | minimal | command |
| `/sys` | absent | none | — |

No host home, `/run/user`, D-Bus socket, SSH agent, container-engine socket, display
socket, GPU, FUSE device, host `/tmp`, or host package cache is mounted.

Source directories are opened by the trusted supervisor with `O_PATH`, `O_DIRECTORY`,
and no-follow semantics, then passed to Bubblewrap with `--ro-bind-fd` or `--bind-fd`.
This pins the objects being mounted and avoids a path replacement race.

## Workspace snapshot and export

### Snapshot

The default workspace mode is `snapshot`:

1. Validate that the configured source is an absolute directory selected by the user,
   not by the model.
2. Create a private session directory with mode `0700` on quota-enforced storage.
3. Load the trusted inclusion, exclusion, and protected-path policy.
4. Traverse from a pinned source-directory FD without following directory symlinks,
   enforcing file-count, depth, per-file, and total-byte limits while copying.
5. Copy or reflink regular files without creating hard links back to host files.
6. Do not cross source filesystem or mount boundaries; reject nested mount points,
   device nodes, sockets, and other special files.
7. Preserve symlinks as symlinks; an absolute or escaping target remains harmless
   because the target is not mounted inside the sandbox.
8. Clear setuid/setgid bits, file capabilities, and security-sensitive extended
   attributes from copied workspace content.
9. Create a synthetic Git baseline, or sanitize explicitly included original metadata.
10. Record an immutable baseline manifest for later comparison.

The trusted snapshot policy also applies source exclusions outside model control. It
defaults to excluding original VCS metadata and known credential artifacts, supports
user-configured protected paths, and reports what will be copied before the model gains
access. Filename rules and secret scanning are warnings, not proof that a tree is
secret-free; selecting a workspace authorizes disclosure of every included byte.

To retain useful Git ergonomics without exposing original history, the default creates a
new session-local repository containing one synthetic baseline commit. It has a dummy
identity, no remotes, no hooks, no external filters, and no credential helpers. The
original `.git` may be copied only by explicit trusted configuration; all included
history, reflogs, objects, and metadata must then be treated as disclosed. Either form of
`.git` remains session-local and is never exportable by default.

Two other modes may exist, but only one shares the security claim:

| Mode | Behavior | Hostile-safe |
| --- | --- | --- |
| `read-only` | Mount a prepared snapshot read-only | Yes |
| `snapshot` | Mount an independent, quota-backed copy read-write | Yes; default |
| `direct-write` | Bind the original checkout read-write | **No** |

`direct-write` must be named and documented as unsafe, require an explicit user-level
configuration outside model control, and never be selected as a fallback.

### Export

No command automatically changes the original workspace. A later export operation must:

- compare the session tree to the recorded baseline from the trusted host side;
- use descriptor-relative traversal and `openat2`-style `RESOLVE_BENEATH`,
  `RESOLVE_NO_MAGICLINKS`, and no-follow checks for untrusted paths;
- never follow sandbox-created symlinks while reading file contents;
- reject devices, FIFOs, sockets, mount points, path traversal, and unsupported hard
  link relationships;
- exclude `.git` and configured protected paths;
- enforce file-count, per-file, and total-byte limits;
- present additions, modifications, deletions, modes, and symlink targets for review;
  and
- require explicit approval and verify that each host preimage still matches the
  baseline before atomically updating host files.

Export should copy validated content by file descriptor. It must not run a
sandbox-generated patch, Git hook, merge driver, or executable on the host.

## Bubblewrap launch profile

The following is a conceptual argument vector, not an implementation script:

```text
bwrap
  --unshare-all
  --unshare-user
  --unshare-cgroup
  --disable-userns
  --assert-userns-disabled
  --uid 1000
  --gid 1000
  --hostname sandbox
  --cap-drop ALL
  --new-session
  --die-with-parent
  --clearenv

  --ro-bind-fd <runtime-fd> /
  --bind-fd <workspace-fd> /workspace
  --bind-fd <cache-fd> /cache
  --bind-fd <home-fd> /home/sandbox

  --perms 0700 --size <tmp-bytes> --tmpfs /tmp
  --perms 0700 --size <run-bytes> --tmpfs /run
  --proc /proc
  --dev /dev

  --perms 0400 --ro-bind-data <command-fd> /run/command.sh
  --add-seccomp-fd <seccomp-bpf-fd>
  --block-fd <release-fd>
  --json-status-fd <status-fd>

  --setenv HOME /home/sandbox
  --setenv PATH /usr/local/bin:/usr/bin:/bin
  --setenv TMPDIR /tmp
  --setenv XDG_CACHE_HOME /cache
  --setenv XDG_CONFIG_HOME /home/sandbox/.config
  --setenv XDG_DATA_HOME /home/sandbox/.local/share
  --setenv LANG C.UTF-8
  --setenv LC_ALL C.UTF-8
  --setenv USER sandbox
  --setenv LOGNAME sandbox
  --setenv BASH_ENV /dev/null
  --setenv ENV /dev/null
  --setenv GIT_CONFIG_NOSYSTEM 1
  --setenv GIT_CONFIG_GLOBAL /dev/null

  --chdir /workspace/<validated-relative-cwd>
  --
  /bin/bash --noprofile --norc /run/command.sh
```

`--unshare-all` uses best-effort forms for the user and cgroup namespaces in Bubblewrap
0.9, so both are also supplied explicitly and failure is fatal. No `*-try` option is
accepted as satisfying a required boundary.

The trusted parent places the exact model-supplied bytes in a sealed memory-backed file
and passes its descriptor through `--ro-bind-data`. It never interpolates the command
into a host shell or Bubblewrap argument.

The parent starts Bubblewrap with `close_fds=True` and an explicit descriptor allowlist.
Only stdin, stdout, stderr, the command FD, mount FDs, seccomp FD, and status/synchronizer
FDs may survive startup. The MCP transport, configuration files, logs, sockets, and
secrets must not be inherited.

The child remains blocked on `--block-fd` while the trusted supervisor confirms from
the status channel that the process is in the intended cgroup, every limit is active,
and the observed namespace IDs match the launch. Only then does it release the child.
The supervisor must create Bubblewrap directly in the already-configured target cgroup
(for example, with a trusted systemd transient unit or an equivalent of
`CLONE_INTO_CGROUP`). Moving a running process into a cgroup is not accepted because it
leaves a fork-before-limit race. The release gate is an additional verification step,
not a way to repair late cgroup placement.

`--new-session` is mandatory even when seccomp also blocks `TIOCSTI`; Bubblewrap calls
this out as protection against terminal-input injection. `--die-with-parent` is a backup
for supervisor failure, not the only process cleanup mechanism.

Stdin is a dedicated pipe, never Goose's stdio MCP stream or the host terminal. With no
stdin argument it is closed immediately, producing EOF. Otherwise the supervisor writes
at most the policy limit asynchronously, closes the pipe, and remains able to enforce
timeouts if the command does not read it.

## Environment

The environment begins empty. The fixed values above are sufficient for ordinary CLI
tools. Optional environment values require a policy allowlist and length limits.

Model-supplied values in these classes are always rejected, regardless of the configured
allowlist:

- `LD_*`, `BASH_ENV`, `ENV`, `PYTHONPATH`, and interpreter startup injection;
- `SSH_*`, `GPG_*`, `DBUS_*`, `DISPLAY`, `WAYLAND_DISPLAY`, and `XDG_RUNTIME_DIR`;
- cloud, CI, package-registry, source-control, and model-provider credentials;
- proxy settings; and
- any value that names a host path or socket.

The model may set ordinary application variables only through a separate validated map;
it cannot ask the shell to inherit the MCP server's environment.

## Network

`hostile-offline` never uses `--share-net`. The new network namespace has only its
isolated loopback interface, so host, LAN, internet, and metadata services are
unreachable. Seccomp additionally rejects internet, packet, and netlink socket domains
while retaining Unix-domain sockets for communication among processes in the same
command.

Network access is deferred to a separate design. A safe future option would require a
dedicated network namespace plus a policy-enforcing egress broker that blocks host,
loopback, private, link-local, and metadata ranges; handles DNS rebinding; and authorizes
destinations independently of model input. Binding a host proxy socket or enabling
`--share-net` would invalidate this design's security claim.

Offline dependency installation remains possible from packages or caches deliberately
included in the runtime or copied into the session. Host package caches are never bound
directly.

## Seccomp

Namespaces limit object visibility; seccomp reduces the kernel attack surface. A
versioned, architecture-specific, default-deny filter is mandatory for hostile commands.
It is generated with libseccomp, exported as cBPF, passed by sealed FD, and tested against
every supported runtime image.

The allowlist should cover ordinary file I/O, memory management, signals, clocks,
futexes, polling, pipes, Unix-domain sockets, process creation, `execve`, and waiting.
Process creation must allow `clone` only without namespace flags. `clone3` should return
`ENOSYS` so compatible runtimes fall back to filterable interfaces.

At minimum, the filter rejects these attack-surface classes:

- namespace and mount mutation: `unshare`, `setns`, `mount`, `umount2`, `pivot_root`,
  `chroot`, `fsopen`, `fsmount`, `fspick`, `move_mount`, `open_tree`, and
  `mount_setattr`;
- cross-process access: `ptrace`, `process_vm_readv`, `process_vm_writev`, and
  `pidfd_getfd`;
- kernel extensibility and observability: `bpf`, `perf_event_open`, module loading,
  kexec, `userfaultfd`, and `io_uring_*`;
- kernel keyrings: `add_key`, `request_key`, and `keyctl`;
- filesystem-handle bypasses: `name_to_handle_at` and `open_by_handle_at`;
- system administration: reboot, swap, accounting, quota, clock-setting, and hostname
  mutation;
- network socket domains other than `AF_UNIX`; and
- dangerous terminal operations, including `TIOCSTI`.

This list is defense in depth, not a substitute for a default-deny allowlist. A
compatibility-driven default-allow filter is not sufficient for the hostile profile.
Filter-generation code and the concrete syscall list require their own review before
execution tools can be enabled.

## Resource and output supervisor

Bubblewrap namespaces do not impose resource ceilings. Every command must enter a
dedicated cgroup v2 subtree before untrusted code executes. A trusted supervisor or
system service owns the cgroup and enforces:

| Resource | Initial ceiling |
| --- | --- |
| Wall-clock time | 120 seconds |
| CPU bandwidth | 2 CPUs equivalent |
| Memory | 2 GiB hard limit |
| Swap | 0 |
| Processes/threads | 128 |
| Open descriptors | 256 |
| Core dump | 0 bytes |
| Single file | 256 MiB |
| Session workspace + cache + home | 2 GiB hard quota |
| Session files/inodes | 200,000 hard quota |
| `/tmp` | 256 MiB tmpfs |
| `/run` | 16 MiB tmpfs |
| stdin | 1 MiB |
| stdout | 1 MiB retained |
| stderr | 1 MiB retained |

Exact values are policy configuration, but the model may only request lower values.

The supervisor uses `memory.max`, `memory.swap.max`, `pids.max`, and `cpu.max`, with
`memory.oom.group=1`. On timeout, cancellation, output abuse, or server shutdown, it
writes to `cgroup.kill`, waits for the subtree to become empty, and only then returns.
POSIX rlimits provide additional per-process ceilings but do not replace cgroups.

Output pipes are always drained. After the retained-byte limit, the supervisor discards
additional bytes while counting them, preventing a pipe deadlock, and returns
`stdout_truncated` or `stderr_truncated`. Output is decoded with explicit replacement
rules while preserving byte counts. Terminal control characters, ANSI/OSC sequences,
hyperlinks, and bidirectional-text controls are escaped or stripped before display.
The result includes exit code or signal, duration, timeout/OOM/limit reason, and
resource-usage summaries.

The persistent session directories must live on storage with hard byte and inode
quotas, such as a dedicated project-quota, subvolume-quota, dataset, or equivalent
backend. Polling disk usage and `RLIMIT_FSIZE` are not sufficient against deliberate
block- or inode-exhaustion attacks.

## Command completion

A command call completes only after:

1. the initial Bash process exits or a limit fires;
2. the entire command cgroup is killed;
3. all output pipes reach EOF;
4. Bubblewrap reports its terminal status;
5. the cgroup is empty and removed; and
6. the supervisor records the result without reading sandbox-controlled paths.

Background processes are therefore killed at the end of every call. A future
long-running-server feature would need a separate explicit lifecycle and is not part of
the Bash tool.

## Policy shape

The eventual trusted configuration could have this shape:

```yaml
version: 1
profile: hostile-offline

runtime:
  rootfs: /var/lib/sandboxed-goose/runtimes/python-dev/rootfs
  digest: sha256:<pinned-digest>
  seccomp_profile: python-dev-aarch64-v1

workspace:
  source: /absolute/user-selected/project
  mode: snapshot
  max_bytes: 2147483648
  max_files: 200000
  export:
    approval_required: true
    protected:
      - .git

resources:
  wall_seconds: 120
  cpu_count: 2
  memory_bytes: 2147483648
  swap_bytes: 0
  pids: 128
  stdout_bytes: 1048576
  stderr_bytes: 1048576
  tmp_bytes: 268435456

network:
  mode: none

environment:
  fixed:
    CI: "true"
  allowed_names: []
```

The file must be owned and writable only by the trusted user/administrator. The model
cannot provide a configuration path or inline policy.

## Mandatory preflight and fail-closed behavior

Execution remains unavailable unless every check succeeds:

- Bubblewrap is a vendor-supported, security-patched build with all required options.
- An actual probe confirms user, mount, PID, IPC, network, UTS, and cgroup namespace
  creation; `--unshare-user-try` is never accepted as success.
- `--disable-userns` and `--assert-userns-disabled` succeed.
- The seccomp filter loads, and known-denied probe syscalls fail as expected.
- The cgroup supervisor can atomically place the child and enforce memory, PID, CPU, and
  whole-tree kill controls.
- The session storage backend demonstrates hard byte and inode quotas.
- Runtime digest, ownership, permissions, mountpoints, and prohibited file metadata pass
  validation.
- Workspace and runtime directory FDs refer to the validated objects.
- No unexpected file descriptors, mounts, environment variables, sockets, or
  capabilities appear in a probe sandbox; `NoNewPrivs` is active; and the effective
  UID/GID/group set is exactly the reviewed identity.

There is no degraded mode that drops seccomp, shares the network, binds the host root,
uses the original workspace, skips quotas, or executes directly on the host. Failure
leaves the Bash tool unregistered or returns a backend-unavailable error.

## Adversarial acceptance tests

Before enabling a Bash MCP tool, automated tests must demonstrate:

- `/home`, host configuration, MCP logs, and a host sentinel are absent;
- `/proc` exposes only sandbox processes and `/proc/*/root` cannot escape;
- scanning `/proc/self/fd` finds only the documented descriptors;
- D-Bus, SSH agent, Docker/Podman, display, and host Unix sockets are unreachable;
- internet, LAN, loopback-host services, DNS, netlink, and raw sockets are unavailable;
- mount, namespace, ptrace, BPF, keyring, `io_uring`, and filesystem-handle probes fail;
- nested user namespaces cannot be created;
- rootfs writes fail and snapshot writes do not affect the original checkout;
- absolute, relative, dangling, cyclic, and racing symlinks cannot escape mounts or
  export validation;
- malicious filenames, special files, hard links, and `.git` mutations cannot escape
  export policy;
- fork bombs hit `pids.max` without affecting unrelated processes;
- memory and swap exhaustion terminate the command cgroup;
- disk filling stops at the hard storage/tmpfs quotas;
- infinite execution, detached descendants, and signal-handling tricks are killed;
- stdout/stderr flooding is bounded without deadlock;
- malformed UTF-8 and binary output are reported safely;
- terminal escapes, OSC hyperlinks, and bidirectional controls cannot manipulate the
  host terminal or review UI;
- cancellation and MCP-server death kill every descendant; and
- all positive tests for Bash, Git, Python, builds, and test runners still work.

Escape tests run on every supported architecture, kernel family, Bubblewrap package, and
runtime image. The suite must include a control case proving each test would detect the
corresponding missing boundary.

## Residual risks

- The sandbox shares the host kernel. Seccomp reduces but cannot eliminate kernel attack
  surface.
- Runtime utilities can contain vulnerabilities. Compromising one should still leave the
  process inside the same boundary, but may improve an attacker's ability to reach the
  kernel.
- CPU topology, kernel version, timing, and other low-value host characteristics remain
  observable.
- Workspace contents and command output are visible to the hostile model. Network
  isolation does not prevent exfiltration through the model conversation.
- Export approval can be socially engineered. The review UI must make destructive and
  suspicious changes legible and should never execute proposed content.
- Exported source and artifacts remain adversary-controlled. They should not be built,
  imported, rendered, or executed on the host merely because export was approved.
- Resource limits protect the host only if cgroup delegation and hard storage quotas are
  actually available.

## Deployment feasibility checks

The installed Bubblewrap build must expose the required namespace,
nested-user-namespace, seccomp-FD, private-device, PID-reaping, status-FD, and
parent-death options. A minimal namespace probe must succeed in the actual launch
context before the backend is enabled.

The UID/GID probe must produce only the intended identities. If an overflow or
supplementary group appears, preflight must explicitly validate it and prove that it
grants no additional access to mounted content.

The launch context must also demonstrate delegated cgroup v2 controls or access to a
trusted resource-supervisor service. Read-only cgroup visibility and unrelated outer
limits are not substitutes for a per-command cgroup.

## Review decisions

These choices need agreement before implementation:

1. Is a shared-kernel boundary sufficient, or does “hostile” require a microVM?
2. Which tools belong in the first immutable runtime: Python only, or also a compiler,
   Node, Rust, and other larger attack surfaces?
3. Is a 2 GiB persistent snapshot and 2 GiB memory ceiling useful enough?
4. Is a synthetic one-commit Git repository the right default, with original `.git`
   history available only as an explicit disclosure opt-in?
5. Should `direct-write` exist at all, even with an explicit unsafe label?
6. Is every export manually approved, or may a separately trusted automation policy
   approve limited paths?
7. Is offline-only sufficient for the first useful release?

## Primary references

- [Bubblewrap security model and limitations](https://github.com/containers/bubblewrap#sandbox-security)
- [Bubblewrap option reference](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)
- [Linux `no_new_privs`](https://www.kernel.org/doc/html/latest/userspace-api/no_new_privs.html)
- [Linux seccomp filter documentation](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
- [Linux cgroup v2 resource controls](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [Linux `openat2` constrained path resolution](https://www.man7.org/linux/man-pages/man2/openat2.2.html)
