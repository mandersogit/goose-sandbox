# 2026-07-31: Apptainer research and project recommendations

## Scope

This note records the architectural conclusions from a research pass over Apptainer
1.5, the current `goose-sandbox` design, and complementary sandboxing mechanisms. It is
a development record, not a replacement for the normative runtime contract in
[`docs/APPTAINER.md`](../docs/APPTAINER.md).

The project already completed considerably more Apptainer work than a typical proof of
concept: a rootless non-setuid installation was verified, a digest-pinned arm64 SIF was
built, a custom hostile-runtime configuration was created, and basic namespace,
capability, network, and read-only-root checks passed. The remaining work is primarily
the trusted supervisor and adversarial validation, not proving that Apptainer can launch
the image.

## Executive conclusion

Apptainer is appropriate for this project as:

- the builder and carrier of curated, immutable toolchain images;
- a rootless launcher for those images on Linux and especially HPC-style hosts;
- a consistent mount, environment, namespace, and cgroup implementation behind the
  backend-neutral MCP tool contract; and
- an integrity boundary through digest pinning and, later, SIF signature verification.

Apptainer is not, by itself, a complete hostile-model sandbox. Its documented default
is host integration rather than maximal isolation. The security boundary therefore
remains the trusted `goose-sandbox` supervisor plus an explicit runtime policy. The
supervisor must select the image, config, mounts, environment, network, limits, and
command lifecycle; the model must never be allowed to construct an Apptainer argument
vector.

The recommended direction is:

1. Retain the current non-setuid, rootless installation and immutable SIF model.
2. Keep a fresh Apptainer execution environment per shell invocation while persisting
   only the independent, quota-backed session workspace.
3. Finish the default-deny seccomp, hard storage quota, cgroup placement, output, and
   process-tree supervision gates before registering a shell tool.
4. Use Landlock or a small inner namespace sandbox when a trusted in-container helper
   must be protected from model code.
5. Use a microVM when the threat model requires a kernel boundary.

Apptainer itself uses the BSD 3-Clause license. That is permissive and is not a
network-copyleft license. Any FUSE library, broker, or additional runtime component
still needs an independent license check.

## What Apptainer contributes

### Immutable runtime distribution

SIF is a strong match for curated agent toolchains. A single image can contain Bash,
Git, Python, compilers, test utilities, and future language-specific environments while
remaining read-only at runtime. The launcher can authorize the final SIF digest rather
than trusting a mutable directory tree or resolving an OCI tag during a model request.

Apptainer can sign and verify SIF metadata and content. A verified signature establishes
that the local SIF is a bit-for-bit copy of the signed artifact; it does not establish
that the contents are safe. For this project, signature verification would complement,
not replace, a reviewed recipe, pinned inputs, a recorded digest, and runtime tests.

Recommended production sequence:

1. Build from the reviewed definition and pinned base/package inputs.
2. Run image and hostile-runtime acceptance tests.
3. Record the final digest and software bill of materials.
4. Sign the complete SIF object group with an offline or CI-controlled key.
5. Verify locally and compare the approved digest immediately before execution.
6. Store the image and policy files outside model-writable storage.

### Rootless operation

The non-setuid path is the right baseline for `goose-sandbox`. It avoids adding a
setuid-root starter merely to execute model-controlled native code. User namespaces let
Apptainer perform the necessary container setup while the process remains an ordinary
host user. This still shares the host kernel, and enabling unprivileged user namespaces
adds kernel attack surface, so patched kernels and defense in depth remain required.

Rootless mode is not equivalent to `--fakeroot`. The proposed hostile invocation needs
neither namespace-root behavior nor a wide subordinate-ID mapping. Fakeroot is useful
for trusted image construction; it should not be granted to the hostile payload merely
to make in-container service separation more convenient.

### HPC and heterogeneous-host fit

Apptainer is particularly useful if this backend eventually runs on shared compute
hosts where Docker-style privileged daemons are unavailable or undesirable. SIF images
are easy to distribute and cache, and Apptainer's operating model is familiar on HPC
systems. A backend can preserve the same MCP operation contract while selecting an
architecture-appropriate SIF and host-specific security profile.

Images should remain explicit by architecture and purpose. An arm64 Python image and an
x86-64 Python image should have independent definitions, package snapshots, digests,
seccomp profiles, and acceptance records. A convenient multi-architecture OCI tag must
not silently choose an unreviewed runtime.

### Foreground commands and instances

Normal `exec` is the safer default for hostile commands. It naturally fits the current
design in which each tool call gets fresh namespaces and all descendant processes must
die at completion.

Apptainer instances provide a persistent background environment and are useful for a
session-scoped service such as a language server, database, or context projection.
They also expand the security problem:

- state and compromised processes persist between model calls;
- cleanup must survive MCP server crashes and host restarts;
- per-command process and output accounting becomes less direct; and
- the model gains a longer window in which to attack services in the instance.

Do not move the shell itself to an instance solely for startup speed until cold-start
cost has been measured. A persistent instance should be introduced only for a service
whose value justifies an explicit session lifecycle and service-to-model trust model.

## Project-specific fit

The current repository has made the important non-default decisions correctly:

- `apptainer-hostile.conf` disables setuid operation, host-derived identity and
  resolver files, the host home, `/sys`, host filesystems, overlay/underlay, and FUSE;
- the proposed invocation uses `--userns`, `--containall`, `--cleanenv`,
  `--no-privs`, `--drop-caps all`, and an isolated network namespace;
- only trusted workspace, cache, home, and command sources are bound, with the
  `nonested` option;
- the root image is immutable and digest-checked;
- the original checkout is not directly writable; and
- missing security controls are intended to fail closed.

These choices matter because an ordinary Apptainer invocation would otherwise expose
host-integrating behavior such as default binds and host configuration. `--cleanenv`
also sanitizes the payload environment only; it does not remove variables that affect
Apptainer while the runtime is constructing the container. The trusted parent must
start from an empty environment and explicitly reject all `APPTAINER_*`, legacy
`SINGULARITY_*`, loader, interpreter, proxy, agent, credential, and socket variables.

The principal Apptainer-specific weakness relative to the proposed Bubblewrap backend
is path-based bind setup. Bubblewrap can receive already-opened directory and data file
descriptors, which pins the objects selected by the supervisor. Apptainer's bind
interface takes paths. Private parent directories, no-follow validation, command
serialization, post-start mount inspection, and protection against rename/replacement
races are therefore part of the Apptainer security contract.

## Recommended execution model

```text
Goose / model
    |
    | validated MCP operation
    v
trusted Python supervisor
    |- selects approved SIF + config + seccomp profile
    |- snapshots workspace onto quota-backed storage
    |- constructs a fixed Apptainer argv and empty environment
    |- creates the command directly in its final cgroup
    |- supervises pidfds/process tree, timeout, stdin, stdout, stderr
    `- verifies namespaces, mounts, limits, and cleanup
             |
             v
      rootless Apptainer exec
             |
             v
       hostile command in fresh namespaces
             |
             v
       persistent session workspace copy
```

The model should control only:

- command bytes;
- a validated workspace-relative working directory;
- bounded stdin;
- and an optional timeout no larger than policy.

Everything else is trusted policy. In particular, the model must not select the SIF,
definition file, config, bind source or destination, environment, FUSE command,
network, capabilities, security options, cgroup, instance name, or nested runtime.

## Layered security controls

Apptainer should be one layer in a control stack rather than the name of the complete
security claim.

| Layer | Required role |
| --- | --- |
| Trusted supervisor | Validate requests, construct fixed argv, manage capabilities and lifecycle |
| SIF integrity | Approved digest, read-only storage, optional complete-image signature |
| Namespaces | User, mount, PID, IPC, UTS, and network isolation |
| Mount policy | Only immutable runtime and explicit session paths; no ambient host binds |
| Seccomp | Architecture-specific default deny; remove namespace, mount, kernel, cross-process, and device attack classes |
| Landlock | Monotonic child-only filesystem and, where supported, signal/socket restrictions |
| cgroup v2 | Memory, swap, PID, CPU, and optionally I/O ceilings before payload release |
| Storage backend | Hard byte and inode quotas for every writable persistent or scratch path |
| I/O supervisor | Bounded stdin and retained output, streaming backpressure, truncation metadata |
| Process supervisor | Wall timeout, pidfds, cgroup kill, descendant verification, no surviving background jobs |
| Network policy | No network initially; later access only through a separate policy broker |
| Export boundary | Descriptor-relative validation and human approval before touching the original checkout |

### Seccomp

Namespaces hide objects; seccomp reduces reachable kernel functionality. The model can
compile and execute arbitrary native code, so a permissive seccomp profile is not a
meaningful hardening layer. The existing design's architecture-specific default-deny
filter remains a launch gate.

The filter should deny namespace and mount mutation, cross-process memory access,
kernel keyrings, BPF/perf/module/kexec interfaces, filesystem-handle bypasses,
`userfaultfd`, `io_uring` until specifically reviewed, unsupported device IOCTLs, and
unneeded network families. It must be tested against every supported SIF because
language runtimes and libc versions have different syscall needs.

### Landlock

Landlock is complementary because an unprivileged process can restrict itself and its
children, and the restrictions cannot later be removed. A trusted in-container init can
install a Landlock ruleset immediately before launching model code, permitting only the
runtime, workspace, private home/cache/tmp, and intentionally exposed context path.

Landlock does not replace mount namespaces or seccomp, and support varies by kernel ABI.
The launcher must probe the exact rights it relies on and fail closed rather than
silently accepting an older ABI. Newer ABIs can also scope signals and Unix-domain
socket resolution, which is potentially useful for protecting an in-container context
daemon, but the Ubuntu 24.04 deployment must be tested rather than assumed to have
those newer rights.

### Bubblewrap as an inner boundary

Bubblewrap is useful in two distinct ways:

- as the alternative Linux backend already designed in `docs/BUBBLEWRAP.md`; and
- as a small child sandbox inside an outer Apptainer when the container must also run a
  trusted helper that the model should not see or signal.

In the second form, a trusted init first starts the helper and prepares mounts, then
launches model code in child user/PID/mount namespaces with capabilities dropped,
Landlock applied, and seccomp installed. This provides intra-container separation with
far less machinery than a fully nested Apptainer runtime. Bubblewrap is still a policy
mechanism, not a prepackaged policy; the parent must construct all arguments.

### cgroups and hard quotas

Rootless Apptainer can apply cgroup v2 limits when systemd delegation is configured.
The supervisor must prove that limits apply before payload code runs. Moving an already
running hostile process into a cgroup leaves a fork-before-limit race and is not
acceptable.

Cgroups do not impose persistent filesystem byte or inode quotas. A separate storage
mechanism is mandatory. Possibilities include project quotas, a deliberately bounded
filesystem image, or another per-session quota facility provided by the deployment.
Merely checking directory size after each command is too late, and tmpfs alone does not
cover a persistent workspace.

### MicroVM boundary

Apptainer, Bubblewrap, seccomp, and Landlock all share the host kernel. If a successful
kernel exploit must not expose the host or neighboring secrets, run the entire agent
sandbox inside a microVM. Apptainer may still be useful inside the guest as the
toolchain/image format, though a second container layer is optional there.

A microVM is especially attractive for:

- long autonomous runs;
- untrusted native build artifacts and parser-heavy workloads;
- network-enabled tasks;
- high-value hosts containing model-provider, Git, cloud, or enterprise credentials;
- and experimental FUSE filesystems that substantially expand kernel-facing behavior.

The tradeoff is image boot and lifecycle complexity, guest patching, workspace transfer,
and a need for virtiofs/9p/vsock or explicit copy-in/copy-out channels.

## Risk summary

| Risk | Apptainer contribution | Residual requirement |
| --- | --- | --- |
| Host file disclosure | Mount namespace and custom bind policy | Empty environment, no default binds, snapshot filtering, mount verification |
| Original workspace modification | Independent session bind | Safe snapshot/export implementation and hard quota |
| Credential leakage | Clean payload environment and curated image | Pre-runtime environment scrub, FD allowlist, no host sockets |
| Network exfiltration | New network namespace with `none` | Verify routes/interfaces; broker any future egress |
| Privilege gain | Rootless userns, no-new-privileges, dropped capabilities | Seccomp, patched kernel, no setuid/capability files |
| Host resource exhaustion | Rootless cgroup support | Pre-exec placement, disk/inode quotas, bounded I/O, timeout |
| Background persistence | PID namespace and foreground exec | Cgroup-wide kill and descendant verification |
| Runtime tampering | Read-only SIF and digest | Trusted storage, signature verification, supply-chain review |
| Kernel compromise | Some attack-surface reduction | No shared-kernel mechanism eliminates this; use a microVM |

## Suggested implementation order

### Gate 1: trusted command supervisor

- Represent the invocation as an argv list, never a host shell string.
- Start with an empty environment and a descriptor allowlist.
- Create sealed/private command input and bounded stdio pipes.
- Validate the workspace source, snapshot, relative CWD, and all bind sources.
- Put the process directly into its final cgroup and verify it before release.
- Inspect namespace IDs, mounts, capabilities, `NoNewPrivs`, routes, and cgroup files.
- Kill and reap the entire cgroup on exit, cancellation, timeout, or MCP disconnect.

### Gate 2: mandatory policies

- Generate and test arm64 and x86-64 default-deny seccomp filters separately.
- Provision hard byte and inode quotas for workspace, home, cache, and command scratch.
- Implement output backpressure and explicit truncation accounting.
- Implement descriptor-relative snapshot and reviewed export.
- Add SIF signature verification after the digest-based flow is stable.

### Gate 3: adversarial suite

The acceptance suite should attempt at least:

- absolute, relative, symlink, magic-link, mount, and rename-based path escapes;
- reading host home, `/sys`, host `/proc`, host identity/resolver state, and common
  credential/socket paths;
- inherited-FD discovery and use;
- namespace creation and mount API variants;
- ptrace, process-VM, pidfd, keyring, BPF, perf, `io_uring`, device, and handle attacks;
- fork bombs, thread bombs, memory/swap pressure, large sparse files, inode floods, and
  output floods;
- daemonization, double-fork, orphan, timeout, cancellation, and supervisor-crash cases;
- network access to host, loopback host services, LAN, internet, DNS, and metadata IPs;
- SIF/config/seccomp replacement races and bind-source rename races; and
- malicious workspace trees during snapshot, execution, and export.

### Gate 4: optional context service

Only after the base hostile profile passes should a second, explicit
`hostile-offline-context` profile enable an in-container FUSE projection. Keeping the
ordinary profile's `enable fusemount = no` preserves a smaller default attack surface.
The context design and its separate threat model are recorded in
[`2026-07-31-dynamic-context-fuse-and-nesting.md`](2026-07-31-dynamic-context-fuse-and-nesting.md).

## Sources consulted

- [Apptainer project overview and license](https://github.com/apptainer/apptainer)
- [Apptainer bind paths, nested bind behavior, and FUSE mount modes](https://apptainer.org/docs/user/latest/bind_paths_and_mounts.html)
- [Apptainer user namespaces and fakeroot](https://apptainer.org/docs/admin/latest/user_namespace.html)
- [Apptainer security configuration and rootless cgroups](https://apptainer.org/docs/admin/latest/security.html)
- [Apptainer SIF signing and verification](https://apptainer.org/docs/user/latest/signNverify.html)
- [Apptainer instances and service lifecycle](https://apptainer.org/docs/user/latest/running_services.html)
- [Linux Landlock documentation](https://docs.kernel.org/userspace-api/landlock.html)
- [Bubblewrap security-model documentation](https://github.com/containers/bubblewrap)
