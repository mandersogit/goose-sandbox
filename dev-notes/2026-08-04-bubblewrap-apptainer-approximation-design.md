# 2026-08-04: approximating Apptainer with Bubblewrap — system design

**Status:** high-level design proposal; nothing here is implemented. The normative security contracts remain [docs/BUBBLEWRAP.md](../docs/BUBBLEWRAP.md) and [docs/APPTAINER.md](../docs/APPTAINER.md); this note designs a runtime that would satisfy both at once.

## Goal

Design a small trusted runtime — provisionally `sgbox` — that reproduces the subset of Apptainer this project actually exercises, using Bubblewrap as the primary isolation mechanism and other unprivileged tools where Bubblewrap alone is insufficient. The point is not to reimplement Apptainer generally. The project's own hostile runtime policy already disables most of what makes Apptainer hard to replicate (setuid mode, overlay/underlay, encrypted and directory images, GPU integration, host binds, CNI networking, home/cwd propagation). What remains is a narrow, well-documented contract, and that narrow contract is reconstructible on Bubblewrap with a smaller trusted computing base and better sealing properties (FD-based binds instead of path-based, no ambient configuration file semantics, `--disable-userns`).

## The Apptainer surface this project actually uses

Grounded in the current tree, the use cases the design must cover are:

1. **Reproducible rootless image builds** (`scripts/build-apptainer-image.sh`, `containers/apptainer/sandbox-python-arm64.def`): OCI bootstrap pinned by digest, an APT snapshot, `%setup`/`%files`/`%post`/`%environment`/`%labels`/`%runscript`/`%test`/`%help` sections, `--build-arg` templating for the derived context image, rootless fakeroot via subordinate IDs, `--reproducible`, a trusted build-only configuration, and atomic publication of a mode-`0444` image plus SHA-256 sidecar.
2. **Embedded image self-test under runtime flags** (`apptainer test` re-run with the hostile config and cgroup limits as a build gate).
3. **Hostile-offline execution contract** (`docs/APPTAINER.md` launch contract): `--containall`-equivalent containment, clean environment, no shell evaluation of the payload, capability drop, private hostname, isolated network namespace with usable loopback, minimal `/dev`, private `/proc`, no `/sys`, explicit read-only/read-write binds selected only by the trusted parent, cgroup ceilings (memory, swap, CPU, PIDs), seccomp, and a fail-closed supervisor owning the whole process-tree lifecycle.
4. **In-container ContextFS FUSE mount** (`src/sandboxed_goose/contextfs/apptainer.py`, `--fusemount "container:… /context"`): a per-call bundle bound read-only at a fixed path, a fixed frontend from the immutable image serving a FUSE mount at `/context`, and a hard requirement that reads only succeed through a real FUSE mount.
5. **Interactive inspection shell** (`scripts/shell-apptainer-session-context.sh`): the same containment plus a usable terminal.
6. **Runtime policy as a trusted artifact** (`apptainer-hostile.conf` / `apptainer-hostile-context.conf` / `apptainer-build.conf`): image-format allowlist, FUSE opt-in as a separate profile, fixed helper search path, systemd cgroups, and launcher-side digest/ownership verification.
7. **Installation and environment verification** (`apptainer buildcfg` non-setuid gate, sanitized `env -i` launch environment, private cache/tmp/state under `.sandbox/apptainer` mode `0700`).

## Component overview

```text
                    trusted MCP server / CLI user
                                │
                                ▼
                      sgbox supervisor (Python)
                                │
        ┌───────────┬───────────┼─────────────┬──────────────┐
        ▼           ▼           ▼             ▼              ▼
  image builder  image      cgroup manager  FUSE broker   policy loader
  (build-time)   mounter    (systemd user   (fusermount3   (TOML, digest-
                 (squashfuse) transient      + frontend     verified)
                              scopes)        sandbox)
                                │
                                ▼
                            bubblewrap
                                │  fresh user/mount/PID/IPC/net/UTS/cgroup namespaces
                                ▼
                        stage-0 sandbox-init
                                │  lo up, cap drop, rlimits, seccomp, exec
                                ▼
                  payload (bash / reader) on immutable image root
```

Everything above the `bubblewrap` line is trusted host-side code owned by the supervisor. The model controls only command bytes, a validated relative cwd, bounded stdin, and a capped timeout — identical to the existing contracts.

## Image format

Apptainer's SIF is a multi-partition single file with embedded metadata and optional signatures. The approximation keeps the properties this project relies on — one immutable, hashable, mountable artifact — and drops the container-specific packaging:

- The image is a plain **SquashFS filesystem** produced by `mksquashfs`, published mode `0444` with a SHA-256 sidecar, exactly like today's SIF artifacts under `.sandbox/apptainer/images/`.
- Definition-file metadata that Apptainer stores in SIF descriptors moves **inside the root filesystem** under a fixed `/.sgbox.d/` directory: `labels.json`, `env.json` (plain key–value pairs, never sourced shell — this preserves `--no-eval` semantics by construction), `runscript.sh`, `test.sh`, `help.md`, and a `build.json` provenance record (base digest, APT snapshot, definition digest, tool versions).
- Signatures, if wanted, are a **detached minisign/`ssh-keygen -Y` signature** over the image file, verified by the launcher alongside the digest. This approximates `apptainer sign`/`verify` without parsing SIF.
- The kernel cannot mount block-backed SquashFS from an unprivileged user namespace, so runtime access goes through **squashfuse** (FUSE), which is also what Apptainer itself does in rootless mode — the "orphaned SIF reader" observed in the ContextFS proof was exactly such a process. Extraction via `unsquashfs` to a directory tree is a build/debug convenience only; like `allow container dir = no` today, a directory tree is never accepted as the hostile runtime root because it loses the single-artifact digest coupling.

## Tool inventory

| Tool | Role | Why Bubblewrap alone cannot do it |
| --- | --- | --- |
| bubblewrap ≥ 0.9 | Namespaces, sealed FD binds, seccomp-FD load, PID-1 reaping, status/handshake FDs | — (this is the core) |
| squashfuse (libfuse3) | Mount the digest-verified image as the sandbox root | bwrap only binds existing trees; kernel squashfs needs privilege |
| squashfs-tools (`mksquashfs`) | Produce reproducible image artifacts | bwrap has no image format at all |
| skopeo | Fetch the OCI base by manifest digest into a local layout | bwrap has no registry client |
| `unshare` + `newuidmap`/`newgidmap` | Multi-UID "range namespace" for builds (fakeroot equivalent) | bwrap maps one UID/GID; `%post` needs a range |
| GNU tar | OCI layer extraction with whiteout handling inside the range namespace | not a bwrap concern |
| systemd user manager (transient scopes) | Per-invocation memory/swap/CPU/PID ceilings, `memory.oom.group` | bwrap enforces no resource limits |
| libseccomp | Compile the default-deny profile to cBPF for `--add-seccomp-fd` | bwrap loads filters but does not author them |
| fuse3 (`fusermount3`, `/dev/fuse`) | Host-anchored unprivileged FUSE mounts for the image root and `/context` | bwrap cannot create FUSE mounts |
| sha256sum + minisign (optional) | Artifact digests and detached signatures | policy layer, not mechanism |

All of these run unprivileged. The only setuid binaries in the trusted computing base are the standard `newuidmap`/`newgidmap` (build path only) and `fusermount3` (runtime mounts) — both already prerequisites of the rootless Apptainer install this design replaces.

## Build pipeline (broad strokes)

`sgbox build <definition> <output>` accepts the existing definition-file dialect (a documented subset: the eight sections used by the two current `.def` files, `Bootstrap: docker|localimage`, and `{{ }}` build-args; everything else is rejected loudly rather than half-supported).

1. **Bootstrap.** For `docker`, skopeo copies the digest-pinned reference into a content-addressed local OCI layout under the state root and verifies the digest. For `localimage`, the parent image is unsquashed into the working rootfs.
2. **Range namespace.** The builder forks a holder process, creates a user namespace mapping container root to the invoking user and `1..65535` to the account's subordinate range via `newuidmap`/`newgidmap`, and keeps it alive for the whole build. Layer extraction (tar with whiteouts), `%post`, and `mksquashfs` all run inside it, so multi-UID ownership is real inside the build and canonical container IDs are what gets stored in the image.
3. **`%setup` and `%files`** run trusted on the host against the working rootfs (`SGBOX_ROOTFS`, mirroring `APPTAINER_ROOTFS`), preserving the current recipe's pre-`%post` sanitization step.
4. **`%post`** runs inside bwrap joined to the range namespace via `--userns <fd>`, with the rootfs bound read-write, private `/tmp`/`/var/tmp` (the same explicit replacement the current build helper does), a generated resolver (the `config resolv_conf = yes` analog), and — build only — shared network for the signed APT snapshot. Bubblewrap remains the mount/chroot engine even at build time; only the ID mapping comes from the range namespace.
5. **Metadata emission.** `%environment`, `%labels`, `%runscript`, `%test`, `%help` are written to `/.sgbox.d/` as described above. `%environment` is parsed into key–value JSON at build time so the runtime never evaluates shell.
6. **Reproducible pack.** `mksquashfs` (pinned version, `SOURCE_DATE_EPOCH`, fixed flags) replaces `--reproducible`; CI double-builds and compares digests, which is a stronger check than trusting the flag.
7. **Gate and publish.** The embedded `test.sh` re-runs through the full hostile runtime pipeline (the `apptainer test` equivalent), the same containment probes as the current build helper run (distinct netns, zero capabilities, `NoNewPrivs`, immutable root, no external route), and the artifact is atomically published mode `0444` with its sidecar.

## Runtime pipeline (broad strokes)

`sgbox exec --policy <file> <image> <argv>` follows the launch contract in docs/BUBBLEWRAP.md, with the image-mounting and cgroup pieces Apptainer provided now supplied by the supervisor:

1. **Policy and artifact verification.** Load the digest-verified TOML policy; hash the image through an already-open descriptor and hand squashfuse `/proc/self/fd/<n>` so the verified object and the mounted object cannot diverge (the FD-pinning discipline the Apptainer docs could only approximate with private directories).
2. **Private run state.** Mode-`0700` per-invocation directory under the state root: image mountpoint, `/context` mountpoint if enabled, sealed command file, scratch.
3. **Two transient scopes.** A *reader* scope (squashfuse, ContextFS frontend) and a *payload* scope (bwrap tree) via the systemd user manager, with the policy's memory/swap/CPU/PID ceilings on the payload scope and a small fixed reserve on the reader scope. Launching bwrap inside an already-configured scope satisfies the "no fork-before-limit race" requirement; the split fixes the lesson from the ContextFS proof, where killing a wrapper orphaned the SIF reader — teardown is payload kill → drain → unmount → reader kill → verify both scopes empty.
4. **Mount the root.** squashfuse serves the image at the private mountpoint; the supervisor opens it `O_PATH` and passes `--ro-bind-fd` to bwrap. Workspace/cache/home session directories bind exactly as in docs/BUBBLEWRAP.md.
5. **Bubblewrap.** The hostile argv is the one already specified in docs/BUBBLEWRAP.md (`--unshare-all`, `--disable-userns`, `--assert-userns-disabled`, `--clearenv`, sealed `--ro-bind-data` command FD, `--add-seccomp-fd`, `--block-fd`/`--json-status-fd` handshake). Environment is composed by the supervisor from `/.sgbox.d/env.json` plus policy overrides into explicit `--setenv` arguments — `--cleanenv` and `%environment` semantics without sourcing anything.
6. **Stage-0 init.** A tiny trusted init inside the sandbox is the one deliberate divergence from pure bwrap: Apptainer's `--network none` provides a *usable* loopback, and localhost-binding test suites depend on it, but bwrap leaves `lo` down. Init is launched with in-namespace ambient `CAP_NET_ADMIN` (+`CAP_SETPCAP`), brings `lo` up, optionally performs the in-sandbox `/context` mount (option B below), then clears ambient and bounding sets, applies rlimits, installs the hostile seccomp filter, and execs the payload. This matches the two-stage-policy structure already anticipated in the FUSE/nesting note. Whether `--cap-add` yields usable ambient capabilities on the installed bwrap is a preflight probe with fail-closed behavior, not an assumption.
7. **Handshake, wait, teardown.** The supervisor verifies namespace identity and cgroup membership over the status FD before releasing `--block-fd`, enforces wall-clock timeout, kills via `cgroup.kill`, drains bounded output, and removes run state — the completion checklist from docs/BUBBLEWRAP.md unchanged.

`sgbox shell` is the same pipeline with a supervisor-owned pty pair relayed to the user's terminal, so `--new-session` stays on and `TIOCSTI` can at worst inject into the disposable relay pty. `sgbox test` is the same pipeline running `/.sgbox.d/test.sh`. `sgbox run` execs `runscript.sh` for parity, though nothing in the project uses it today.

## ContextFS transport equivalence

The `--fusemount "container:…"` feature is the one piece of Apptainer with no bwrap analog, and it is load-bearing for the `apptainer-fuse` transport. Two designs, with A recommended:

**Option A — host-anchored mount, frontend in its own sandbox.** The supervisor performs the standard unprivileged libfuse mount handshake (`fusermount3` + `FUSE_COMMFD`) at the private `/context` mountpoint in its own mount namespace and receives the open `/dev/fuse` FD. The frontend (`sandboxed-goose-contextfs --bundle … /dev/fd/3`, unchanged — the dev-notes already established the libfuse3 ≥ 3.3 pre-opened-FD contract) runs in a *separate* bwrap sandbox built from the same digest-verified context image, with the bundle bound read-only, no network, its own scope, and the inherited FUSE FD as its only unusual descriptor. The payload sandbox simply binds the mountpoint. Inside the payload, `/context` is a real FUSE mount (`statfs` → `FUSE_SUPER_MAGIC`), so the transport's "refuse unless a real FUSE mount" invariant keeps working. This is a strict *improvement* over the current mechanics proof: docs/APPTAINER.md explicitly flags that the frontend today shares the payload's PID namespace and is killable/inspectable by the model; in option A the model cannot see, signal, ptrace, or starve the frontend at all, which is the "separate the processes architecturally" recommendation from the FUSE/nesting note realized.

**Option B — in-sandbox mount by stage-0 init.** Bind host `/dev/fuse` into the payload sandbox; init, while still holding in-namespace `CAP_SYS_ADMIN`, mounts FUSE at `/context` and spawns the frontend from the image before dropping capabilities and installing seccomp. This reproduces Apptainer's fully-in-container topology (no host-visible mountpoint) but reinstates the same-PID-namespace weakness and depends on the ambient-capability probe. Keep it specified as a fallback; do not build it first.

The trade-off in option A — the mountpoint exists in the supervisor's namespace under a `0700` directory — is the same one already accepted for the squashfuse image root, and the parity test adjusts accordingly: instead of asserting the frontend runs beside the payload with `/dev/fd/3 -f`, it asserts the frontend is *absent* from the payload's PID namespace entirely, that `/dev/fuse` and `fusermount3` are unreachable from the payload, and that mutation, cleanup, and both-mode (toy and session-bundle) content checks from `scripts/test-apptainer-contextfs.sh` behave identically.

## Flag and directive mapping

| Apptainer surface (as used here) | sgbox mechanism |
| --- | --- |
| `--config <hostile.conf>` | one TOML policy file per profile, digest/ownership-verified by the launcher |
| `--userns`, non-setuid install | inherent (bwrap is never setuid); `buildcfg` gate becomes `sgbox doctor` |
| `--containall --cleanenv --no-eval` | purpose-built root + `--clearenv` + explicit `--setenv` + direct argv exec |
| `--no-privs --drop-caps all` | `--cap-drop ALL` for payload; stage-0 clears ambient/bounding after setup |
| `--hostname sandbox` | `--unshare-uts --hostname sandbox` |
| `--net --network none` | `--unshare-net` + stage-0 loopback-up |
| `--no-mount home,cwd,hostfs,bind-paths,sys` | nothing is mounted unless the supervisor binds it (bwrap default) |
| `--memory/--memory-swap/--cpus/--pids-limit` | transient scope properties on the payload scope |
| `--bind src:dst:ro` / `--mount …,nonested` | `--ro-bind-fd`/`--bind-fd`/`--ro-bind-data` (sealed FDs; no `APPTAINER_BIND` nesting hazard exists) |
| `--fusemount container:` | option A broker (or option B init mount), policy-gated like `enable fusemount` |
| `--security seccomp:` | libseccomp-compiled cBPF via `--add-seccomp-fd` |
| `--cwd`, `--env` | `--chdir` (validated), `--setenv` |
| SIF + sha256 sidecar | SquashFS + sidecar + optional detached signature |
| `apptainer build --fakeroot --reproducible` | range namespace + bwrap `--userns FD` + pinned `mksquashfs` + CI double-build |
| `apptainer test` | `sgbox test` running `/.sgbox.d/test.sh` under the hostile pipeline |
| `allow container sif/squashfs`, `dir = no` | magic-number + digest check; only SquashFS accepted at runtime |
| `binary path = …` | absolute, policy-recorded helper paths (squashfuse, fusermount3, systemd-run), digest-checked |
| `APPTAINER_CACHEDIR/TMPDIR`, `env -i` launch | `SGBOX_STATE` private `0700` tree; identical sanitized-environment discipline |

Not carried over, deliberately: setuid mode, encrypted/extfs/dir containers, overlay/underlay, GPU paths, CNI networks beyond none, instances/`container-daemon:` (revisit only with an explicit supervised-lifecycle design), remote builds, and SIF partition tooling.

## Integration with the existing code

- `SANDBOXED_GOOSE_BACKEND=bubblewrap` is already parsed; this design is its referent.
- `Settings` gains a `bubblewrap-fuse` value for `SANDBOXED_GOOSE_SESSION_CONTEXT_TRANSPORT` plus `SGBOX`-side analogs of the image/config/state variables; `render_projection_via_apptainer` gets a sibling launcher that reuses `write_bundle`, the fixed bundle path, the reader argv, and — unchanged — `_validate_response`, so the response envelope and its bounds checking stay byte-identical across `direct`, `apptainer-fuse`, and `bubblewrap-fuse`.
- Script parity: `build-sgbox-image.sh`, `shell-sgbox-session-context.sh`, and a `test-sgbox-contextfs.sh` mirroring the existing checks with the option-A adjustments above; Make targets alongside the Apptainer ones so both backends stay exercised.
- The definition files should build under both engines during the transition; anything the sgbox dialect rejects is a build error, not a silent skip.

## Verification

`sgbox doctor` is the preflight gate (the `buildcfg` analog): bwrap version and required options (`--disable-userns`, `--ro-bind-fd`, `--add-seccomp-fd`, `--json-status-fd`, `--userns`, `--cap-add` ambient probe), user-namespace creation, `/dev/fuse` and `fusermount3`, squashfuse/mksquashfs/skopeo versions, subordinate-ID ranges and mapping helpers (build only), systemd user-manager delegation of memory/cpu/pids, and the storage-quota backend. Every check fails closed; there is no degraded mode, per the standing security invariant.

The acceptance gates are the union of the existing lists in docs/BUBBLEWRAP.md and docs/APPTAINER.md — they were written to be backend-agnostic and apply unchanged, including the control cases proving each test would catch its missing boundary. New backend-specific additions: squashfuse death mid-command must fail the command and teardown cleanly (EIO from the root, not a hang); reader-scope starvation must not deadlock payload teardown; the FUSE broker handshake must be robust against a payload that opens `/context` descriptors and holds them across exit.

## Open questions

1. Is the ambient-capability route for stage-0 (`--cap-add` semantics on installed bwrap) reliable across target hosts, or should loopback-up move to a `pidfd`-based post-launch helper if the probe fails anywhere we care about?
2. Option A leaves squashfuse and the frontend as same-UID host processes of the supervisor; is that acceptable long-term, or does the broker eventually deserve its own service account?
3. Should the sgbox dialect keep def-file syntax at all, or freeze on a small declarative build manifest (JSON/TOML) once both current definitions are ported?
4. Is minisign signing worth adding before the first hostile-execution milestone, given digests-in-policy already gate launch?
5. Does `sgbox` subsume the Apptainer backend once at parity, or do both remain maintained escape-suite targets indefinitely?

## References

- [docs/BUBBLEWRAP.md](../docs/BUBBLEWRAP.md) — hostile launch contract this design implements
- [docs/APPTAINER.md](../docs/APPTAINER.md) — the surface being approximated, launch contract, acceptance gates
- [2026-07-31: dynamic context filesystems, in-container FUSE, and nesting](2026-07-31-dynamic-context-fuse-and-nesting.md) — frontend/broker split and two-stage seccomp groundwork
- [2026-07-31: ContextFS Apptainer proof](2026-07-31-contextfs-apptainer-proof.md) — observed `container:` mount mechanics and cleanup lessons
- [Bubblewrap option reference](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)
- [squashfuse](https://github.com/vasi/squashfuse), [squashfs-tools reproducibility](https://github.com/plougher/squashfs-tools)
- [skopeo](https://github.com/containers/skopeo), [`newuidmap(1)`](https://man7.org/linux/man-pages/man1/newuidmap.1.html), [`user_namespaces(7)`](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)
- [libfuse mount protocol (`fusermount3`)](https://github.com/libfuse/libfuse), [Linux FUSE documentation](https://docs.kernel.org/filesystems/fuse/fuse.html)
- [systemd transient units and user-manager delegation](https://systemd.io/CGROUP_DELEGATION/)
