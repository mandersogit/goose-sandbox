# Apptainer host and sandbox-image recipe

**Status:** The rootless arm64 image was built and validated with Apptainer `1.5.3` on
Ubuntu 24.04 on 2026-07-30. No execution MCP tool is enabled.

The validated development artifact is mode `0444`, 199,905,280 bytes, with SHA-256:

```text
601e3fa4c39f0c3c4d7c995e29b5e4cfde6ff75dddef7bbb53f7574a6d6a51a4
```

It is stored under `.sandbox/apptainer/images/`, which is intentionally ignored by Git.
Rebuilding after any recipe or dependency refresh must produce and review a new digest.

## Recommendation

On Ubuntu 24.04 (`noble`) on arm64, install the official, non-setuid Apptainer package
from the Apptainer Ubuntu PPA. As checked on 2026-07-30, the PPA package is Apptainer
`1.5.3-1~noble`, matching the upstream `1.5.3` release.

Do **not** install `apptainer-suid` when unprivileged user namespaces, FUSE, subordinate
UID/GID ranges, and rootless cgroup v2 delegation are available. Adding a setuid-root
starter is unnecessary for this use case and would enlarge the trusted attack surface.

Do not install Ubuntu's `singularity-container` package as a substitute. The package
currently offered by the Ubuntu archive is SingularityCE, a different project and
release line.

## Host prerequisites

The reference build used the following rootless prerequisites. A deployment must verify
them independently and fail closed if a required control is unavailable:

| Item | Required state |
| --- | --- |
| OS / architecture | Ubuntu 24.04 `noble`, `aarch64`, for the current image recipe |
| Unprivileged user namespaces | enabled |
| Ubuntu AppArmor userns restriction | enabled |
| `/dev/fuse` | present and usable by ordinary users |
| SquashFS / overlay / FUSE | kernel and user-space support installed |
| UID/GID mapping helpers | setuid `newuidmap` and `newgidmap` installed |
| Subordinate IDs | non-empty UID and GID ranges assigned to the invoking account |
| cgroups | unified cgroup v2 with CPU, memory, swap, and PID controls |
| User systemd manager | transient resource-limited scopes must work |
| Workspace storage quotas | hard byte and inode quotas required for hostile runtime |

The PPA package is preferable to a source install on Ubuntu 24.04 because it also
installs and reloads an AppArmor profile for its starter with the required `userns`
rule. This integrates with Ubuntu 24.04's
`kernel.apparmor_restrict_unprivileged_userns=1` setting without disabling the host-wide
restriction.

Storage quotas do not block installing Apptainer or building and inspecting the image.
Ordinary directories under `.sandbox/` are suitable only for trusted development
artifacts. Runtime session storage needs a hard byte-and-inode quota backend before the
Bash tool can be enabled.

## Install

On Ubuntu, the required commands are:

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:apptainer/ppa
sudo apt update
sudo apt install -y apptainer
```

This is a host-level package install and requires the user's sudo password. It is the
only recommended privileged installation step. Do not add `apptainer-suid`.

## Verify the installation

First confirm package identity and that the installation is non-setuid:

```bash
apptainer version
apptainer buildcfg
dpkg-query -W apptainer
dpkg-query -W apptainer-suid
```

Expected:

- `apptainer version` prints `1.5.3`;
- `apptainer buildcfg` includes `APPTAINER_SUID_INSTALL=0`;
- `apptainer` is installed; and
- the `apptainer-suid` query reports that no package is installed.

Inspect the starter itself:

```bash
stat -c '%A %a %U:%G %n' /usr/libexec/apptainer/bin/starter
test ! -u /usr/libexec/apptainer/bin/starter
getcap /usr/libexec/apptainer/bin/starter
```

The starter should be root-owned mode `0755`, with neither a setuid bit nor file
capabilities.

Confirm the rootless prerequisites:

```bash
sysctl kernel.unprivileged_userns_clone
sysctl user.max_user_namespaces
sysctl kernel.apparmor_restrict_unprivileged_userns
command -v newuidmap newgidmap
account_name="$(id -un)"
grep "^${account_name}:" /etc/subuid /etc/subgid
test -c /dev/fuse
```

Finally run a disposable rootless smoke test. This download is for installation
verification only, not the final sandbox image:

```bash
mkdir -p .sandbox/apptainer/cache .sandbox/apptainer/tmp
chmod 0700 .sandbox/apptainer/cache .sandbox/apptainer/tmp

APPTAINER_CACHEDIR="$PWD/.sandbox/apptainer/cache" \
APPTAINER_TMPDIR="$PWD/.sandbox/apptainer/tmp" \
apptainer exec \
  --userns \
  --containall \
  --cleanenv \
  --no-eval \
  --no-privs \
  --drop-caps all \
  --no-umask \
  --net \
  --network none \
  docker://alpine:3.22 \
  sh -c 'id; grep -E "^(CapEff|NoNewPrivs):" /proc/self/status; cat /proc/net/route'
```

This must succeed as the ordinary user. `NoNewPrivs` must be `1`, `CapEff` must be zero,
and there must be no external route. `--network none` is the only CNI configuration
Apptainer allows to non-privileged users by default; it provides an isolated loopback
interface. A failure is a blocker, not a reason to retry with sudo or host networking.

## Image recipe

The first image definition is
[`containers/apptainer/sandbox-python-arm64.def`](../containers/apptainer/sandbox-python-arm64.def).
It is intentionally host-specific:

- arm64 Ubuntu 24.04 is pinned by OCI manifest digest;
- APT is pinned to the Ubuntu archive snapshot at `20260730T000000Z`;
- the image contains Bash, Git, Python, C/C++ build tools, CMake, Ninja, and common
  offline text/archive utilities;
- the base image's known `ubuntu` identity is replaced by the static unprivileged
  `sandbox` identity at UID/GID `1000:1000`;
- `/workspace`, `/cache`, `/home/sandbox`, and the command-file mountpoint exist;
- host identity, resolver, hostname, and timezone state are not embedded;
- Git credential helpers and prompts are disabled by default;
- pip defaults to offline, non-interactive operation;
- setuid/setgid bits and file capabilities are removed; and
- the embedded test checks the identity, toolchain, and prohibited image metadata.

The trusted build uses
[`containers/apptainer/apptainer-build.conf`](../containers/apptainer/apptainer-build.conf)
instead of the system runtime configuration. This keeps the system configuration's
`/etc/hosts`, `/etc/localtime`, `/tmp`, and `/var/tmp` bind paths out of the build while
retaining only the features needed to construct the project-owned image. In particular,
a bind-mounted `/etc/localtime` cannot be replaced by Ubuntu's `tzdata` package, and a
setid scan must never traverse host temporary directories.

Apptainer 1.5.3's build engine may still supply temporary-directory mounts even when
`mount tmp = no`. The build helper therefore replaces `/tmp` and `/var/tmp` explicitly
with fresh mode-`1777` directories inside its private build directory and invokes
Apptainer from that directory. The trusted build configuration permits user binds only
for this controlled invocation; the hostile runtime configuration remains separate.

The minimal Ubuntu OCI base does not contain a CA bundle. The definition bootstraps
`ca-certificates` from the base's signed Ubuntu archive, switches to the direct,
timestamped Ubuntu snapshot URL, and explicitly reinstalls the snapshot's
`ca-certificates`, `openssl`, and `libssl3t64` versions before installing the remaining
packages. This is necessary on arm64 because the base uses `ports.ubuntu.com`, which
APT 2.8 does not translate through its built-in snapshot host map.

The base image digest currently resolves to the official `arm64v8/ubuntu:24.04` manifest:

```text
sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827dc4b8c707de5e78a6da7ab
```

The APT snapshot makes package selection repeatable, but it also freezes security
updates. Refresh the timestamp, review the resulting package/image changes, run the
escape suite, and issue a new final SIF digest on a regular patch cadence. Apptainer's
`--reproducible` option normalizes SIF creation from the source image; it does not make
an unpinned package repository reproducible, which is why the Ubuntu snapshot is also
used.

### Build

After installing and verifying Apptainer:

```bash
make apptainer-image
```

The helper:

1. refuses non-arm64 and non-`1000:1000` hosts for this first recipe;
2. refuses a setuid Apptainer installation;
3. uses a fixed, credential-free host environment and private project-local home, cache,
   build, `/tmp`, and `/var/tmp` directories;
4. builds with rootless fakeroot and `--reproducible`;
5. re-runs image tests with the hardened config, namespaces, offline network, and
   cgroup limits;
6. verifies a distinct network namespace, no external connection, zero capabilities,
   `NoNewPrivs`, an absent `/sys`, and a read-only root filesystem; and
7. atomically publishes a mode-`0444` SIF and SHA-256 sidecar under
   `.sandbox/apptainer/images/`.

Use the helper rather than copying a bare `apptainer build` command: creation of private
mount sources, the sanitized environment, validation, atomic publication, permissions,
and digest generation are all part of the build contract. The exact invocation is in
[`scripts/build-apptainer-image.sh`](../scripts/build-apptainer-image.sh).

The image's final SHA-256, not only its Ubuntu base digest, must be placed in trusted
runtime policy. Production deployment should put the SIF in a directory the sandbox
process cannot modify, ideally administrator-owned, and may additionally require an
Apptainer signature.

## Hardened runtime configuration

[`containers/apptainer/apptainer-hostile.conf`](../containers/apptainer/apptainer-hostile.conf)
is a separate runtime-only configuration. It:

- disables setuid operation;
- disables host-derived passwd, group, resolver, home, `/sys`, host filesystem, and
  configured bind mounts;
- requests a minimal `/dev`, private mount propagation, and private `/proc`;
- accepts only SIF/SquashFS containers;
- disables extfs, directory, encrypted, writable overlay/underlay, user FUSE, GPU, and
  monitoring paths;
- drops root's default capability set;
- uses a fixed helper search path; and
- retains systemd cgroups for rootless limits.

The non-setuid installation is important: Apptainer permits an ordinary user to provide
an alternate `--config` only in an unprivileged installation. The trusted launcher must
pass this exact file before the subcommand:

```text
apptainer --config /trusted/path/apptainer-hostile.conf exec ...
```

The launcher must verify the config's digest and ownership. It must not take a config
path from the model or use a copy in the model-writable session snapshot.

## Proposed launch contract

An Apptainer image is **not** a sandbox by itself. Apptainer deliberately favors host
integration, and its defaults bind the user's home, current directory, `/sys`, `/tmp`,
host configuration files, and other administrator-configured paths. The trusted
supervisor must construct the complete argument vector.

Conceptually, the `hostile-offline` invocation is:

```text
apptainer
  --config <trusted-apptainer-hostile.conf>
  exec
  --userns
  --containall
  --cleanenv
  --no-eval
  --no-privs
  --drop-caps all
  --no-umask
  --hostname sandbox

  --net
  --network none

  --no-mount home,cwd,hostfs,bind-paths,sys
  --no-mount /etc/hosts,/etc/localtime,/etc/resolv.conf
  --workdir <private-quota-backed-command-scratch>

  --memory 2G
  --memory-swap 2G
  --cpus 2
  --pids-limit 128

  --security seccomp:<trusted-architecture-specific-profile>

  --mount type=bind,src=<session-snapshot>,dst=/workspace,rw,nonested
  --mount type=bind,src=<session-cache>,dst=/cache,rw,nonested
  --mount type=bind,src=<session-home>,dst=/home/sandbox,rw,nonested
  --mount type=bind,src=<private-command-file>,dst=/run/sandboxed-goose/command.sh,ro,nonested

  --cwd /workspace/<validated-relative-directory>
  --env HOME=/home/sandbox
  --env PATH=/usr/local/bin:/usr/bin:/bin
  --env TMPDIR=/tmp
  --env XDG_CACHE_HOME=/cache
  --env XDG_CONFIG_HOME=/home/sandbox/.config
  --env XDG_DATA_HOME=/home/sandbox/.local/share
  --env BASH_ENV=/dev/null
  --env ENV=/dev/null

  <digest-verified-sandbox-python-arm64.sif>
  /bin/bash --noprofile --norc /run/sandboxed-goose/command.sh
```

This is an argument vector, not a host shell command. The supervisor must call Apptainer
directly and never interpolate model text into a host shell.

The model controls only command bytes, a workspace-relative CWD, bounded stdin, and a
timeout no larger than policy. It cannot control the image, config, mounts, environment,
network, seccomp profile, resource ceilings, or Apptainer arguments.

Before starting Apptainer, the supervisor begins from an empty environment. It may add
only the fixed host-side variables needed to locate Apptainer and communicate with the
user's systemd manager for cgroup creation. In particular it must clear all inherited:

- `APPTAINER_*` and legacy `SINGULARITY_*` control variables;
- `LD_*`, interpreter startup, shell startup, and dynamic-loader variables;
- bind, mount, FUSE, overlay, image, plugin, and config selectors;
- proxy settings and network configuration;
- SSH/GPG agents, D-Bus/display/container-engine sockets; and
- cloud, package registry, source-control, CI, and model-provider credentials.

`--cleanenv` controls the payload environment but does not replace sanitizing variables
that affect Apptainer itself before the payload starts.

Every bind source is created and selected by the trusted parent. `nonested` prevents
Apptainer from advertising these mounts through `APPTAINER_BIND` to a nested invocation.
The image contains no Apptainer binary, and no host runtime binary or socket is mounted.

The command is written to a mode-`0400` file in a private mode-`0700` command directory,
held unchanged for the whole invocation, and bound read-only. Bash reads that file, so
stdin remains a separate bounded pipe. Apptainer's bind interface is path-based, unlike
Bubblewrap's FD-based bind/data options; private directories, serialization, no-follow
validation, and mount inspection are therefore mandatory.

## Resource, storage, and lifecycle requirements

The proposed initial limits match the Bubblewrap profile:

| Resource | Initial ceiling |
| --- | --- |
| Wall time | 120 seconds |
| CPU | 2 CPUs equivalent |
| Memory | 2 GiB |
| Swap | 0 (`--memory-swap` equals `--memory`) |
| Processes/threads | 128 |
| Open descriptors | 256 |
| Core dump | 0 bytes |
| Single file | 256 MiB |
| Persistent session storage | 2 GiB and 200,000 inodes |
| Command scratch backing `/tmp` and `/var/tmp` | 256 MiB hard quota |
| stdin | 1 MiB |
| retained stdout / stderr | 1 MiB each |

Before enabling hostile execution, verify that an ordinary-user systemd scope can
enforce CPU, memory, zero-swap, and PID limits. The installed Apptainer must also be
tested to show that its CLI flags create the intended cgroup before the payload begins.

Cgroups do not limit persistent bytes or inodes. Workspace, cache, home, command scratch,
and Apptainer temporary storage must be on hard quota-backed storage. Polling `du` is not
a security boundary.

On timeout, cancellation, output abuse, MCP shutdown, or normal completion, the
supervisor must kill the complete cgroup / PID namespace, drain bounded output, verify
that no descendants remain, and remove command-local storage. It must never consider the
initial Bash process exiting sufficient if descendants survive.

Workspace snapshot and reviewed export semantics are identical to
[BUBBLEWRAP.md](BUBBLEWRAP.md): the original checkout is never mounted read-write in the
hostile profile.

## Seccomp status

Apptainer 1.5.3 accepts an OCI JSON seccomp profile through
`--security seccomp:<path>`. Source inspection shows the profile is loaded into the OCI
process configuration in the non-setuid path, but this must be demonstrated on the
installed arm64 package before relying on it.

A concrete default-deny arm64 profile is deliberately not included yet. It must be
derived and tested against this exact image's Bash, Git, Python, compiler, and test
workloads while denying the namespace, mount, kernel-observability, keyring, BPF,
`io_uring`, cross-process, filesystem-handle, and non-Unix socket classes described in
[BUBBLEWRAP.md](BUBBLEWRAP.md). A permissive default-allow profile is not accepted as the
hostile profile merely to improve compatibility.

Missing or rejected seccomp is a hard failure for hostile execution.

## Acceptance gates before an MCP Bash tool

The image build helper is only a content and basic-containment check. Enabling hostile
model commands additionally requires automated proof that:

- the exact SIF and runtime-config digests match trusted policy;
- the runtime is non-setuid and no prohibited helper/package has appeared;
- user, mount, PID, IPC, UTS, and network namespaces are distinct;
- only the intended mounts and descriptors are visible;
- host home, CWD, `/sys`, credentials, sockets, devices, and sentinels are absent;
- the root filesystem is immutable and writes affect only quota-backed session mounts;
- offline networking has no route to host, LAN, internet, DNS, or metadata services;
- the default-deny seccomp profile loads and known-denied probes fail;
- effective/permitted/ambient capabilities are empty and `NoNewPrivs` is active;
- memory, swap, CPU, PID, output, time, byte, and inode limits work under attack;
- fork bombs, detached descendants, server death, and cancellation leave no process;
- bind-source path replacement and symlink races fail safely; and
- the positive Bash, Git, Python, build, and test workloads remain useful.

There is no fallback to host networking, a writable image, a directory image, sudo,
setuid mode, the original checkout, missing limits, missing seccomp, or direct host
execution.

## Assessment

Apptainer is promising for this backend:

- SIF gives a convenient immutable, portable, hashable runtime artifact.
- Rootless definition builds are straightforward when subordinate IDs are configured.
- The official package integrates with Ubuntu's AppArmor policy.
- `--containall`, `--network none`, custom config, seccomp, and cgroup flags expose the
  mechanisms we need.

It is less naturally suited than Bubblewrap to constructing a minimal hostile-command
boundary:

- secure isolation is opt-in rather than the default;
- bind and security-policy inputs are paths rather than sealed file descriptors;
- behavior depends on both CLI arguments and Apptainer configuration;
- the special `--network none` path and resulting namespace state still require
  package-level verification; and
- the project's documented emphasis is application portability and host integration.

The recommended role is therefore an experimental backend using the same fail-closed
contract and escape suite as Bubblewrap, not a reason to weaken that contract. If the
installed-package tests cannot meet it, Apptainer remains useful for reproducible
payload images but is rejected as the hostile execution boundary.

## Primary references

- [Official Apptainer installation guide](https://apptainer.org/docs/admin/latest/installation.html)
- [Official Apptainer Ubuntu PPA](https://launchpad.net/~apptainer/+archive/ubuntu/ppa)
- [Apptainer 1.5.3 release](https://github.com/apptainer/apptainer/releases/tag/v1.5.3)
- [User namespaces and fakeroot requirements](https://apptainer.org/docs/admin/main/user_namespace.html)
- [Default binds and containment flags](https://apptainer.org/docs/user/latest/bind_paths_and_mounts.html)
- [Apptainer exec options](https://apptainer.org/docs/user/main/cli/apptainer_exec.html)
- [Network namespace configuration](https://apptainer.org/docs/user/latest/networking.html)
- [Rootless cgroup resource limits](https://apptainer.org/user-docs/master/cgroups.html)
- [Definition files](https://apptainer.org/docs/user/latest/definition_files.html)
- [Build options](https://apptainer.org/docs/user/latest/cli/apptainer_build.html)
- [Ubuntu snapshot service](https://documentation.ubuntu.com/server/how-to/software/snapshot-service/)
