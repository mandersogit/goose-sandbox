# 2026-07-30: rootless Apptainer bring-up

## Outcome

After the host package installation, the official Ubuntu PPA package was verified and
the project image was built successfully as an ordinary user:

```text
Apptainer: 1.5.3 (1.5.3-1~noble)
installation mode: non-setuid
image: .sandbox/apptainer/images/sandbox-python-arm64.sif
size: 199,905,280 bytes
mode: 0444
SHA-256: 601e3fa4c39f0c3c4d7c995e29b5e4cfde6ff75dddef7bbb53f7574a6d6a51a4
```

The SIF and checksum sidecar are local, ignored build artifacts. The recipe, build
helper, and security rationale are tracked; the built image is not.

## Host verification

The installed runtime reports `APPTAINER_SUID_INSTALL=0`. The starter is root-owned
mode `0755`, with no setuid bit or file capabilities, and `apptainer-suid` is absent.

Rootless prerequisites were present:

- unprivileged user namespaces are enabled;
- Ubuntu's AppArmor restriction on unprivileged user namespaces remains enabled;
- the package's AppArmor profile permits the required user namespace operation;
- setuid `newuidmap` and `newgidmap` helpers are installed;
- the invoking account has subordinate UID and GID ranges; and
- `/dev/fuse` is present and accessible.

A disposable Alpine run as the ordinary user confirmed UID 1000, zero effective
capabilities, `NoNewPrivs: 1`, and a network namespace with no external route.

Hard byte and inode quotas were not part of this build validation. They are not needed
for trusted image construction, but remain mandatory for adversarial writable session
storage.

## Problems found while building the real image

The first successful build required several changes that were not obvious from a
minimal definition-file recipe:

1. The arm64 Ubuntu base uses `ports.ubuntu.com`. APT's snapshot mapping did not
   translate that host, so enabling `APT::Snapshot` fetched live indices and then
   filtered out every package. The recipe now bootstraps CA certificates from the
   signed live archive, replaces the sources with a direct signed
   `snapshot.ubuntu.com` URL, and explicitly normalizes `ca-certificates`, `openssl`,
   and `libssl3t64` to snapshot versions before installing the remaining packages.
2. The system Apptainer configuration bind-mounted `/etc/localtime` during the build.
   `tzdata` could not replace the mount and failed with `Device or resource busy`.
   System binds also interfered with sanitizing `/etc/hosts`. A separate trusted
   build-only configuration now omits those bind paths, while identity and resolver
   sanitization happens in `%setup` before build-time mounts appear.
3. The pinned Ubuntu base already contains the `ubuntu` user and group at UID/GID 1000.
   The definition now recognizes and removes only that expected identity, rejects an
   unexpected occupant, and creates the fixed `sandbox:1000:1000` identity.
4. Apptainer exposed host `/tmp` and `/var/tmp` during the build even with the initial
   mount settings. The final setid scan therefore traversed unrelated host temporary
   files and failed. The helper creates private temporary directories and explicitly
   binds them over both paths for the trusted build invocation.
5. A broad APT `--reinstall` operation would have refreshed all requested packages from
   the bootstrap source. The CA bootstrap and snapshot normalization are now separate,
   limiting the live-archive step and leaving the final image on snapshot versions.

These fixes are encoded in:

- `containers/apptainer/sandbox-python-arm64.def`;
- `containers/apptainer/apptainer-build.conf`; and
- `scripts/build-apptainer-image.sh`.

## Final image checks

`make apptainer-image` completed the definition's embedded tests and the helper's
offline runtime checks. The final runtime observation included:

```text
uid=1000(sandbox) gid=1000(sandbox)
hostname=sandbox
Python 3.12.3
openssl 3.0.13-0ubuntu3.11
APT snapshot=https://snapshot.ubuntu.com/ubuntu/20260730T000000Z/
CapEff=0000000000000000
NoNewPrivs=1
root filesystem=read-only
/sys/kernel=absent
external network=unreachable
```

The checksum sidecar was verified after atomic publication.

## What this does not prove

The image build is a content and basic-containment result, not authorization to expose a
Bash tool to a hostile model. The remaining gates include a tested default-deny arm64
seccomp profile, hard byte/inode quotas, full namespace and mount inspection, race-safe
bind setup, cgroup/process-tree supervision, bounded I/O, cancellation cleanup, and an
adversarial escape suite.

The current hardened-runtime design and complete acceptance list are in
[`docs/APPTAINER.md`](../docs/APPTAINER.md). No runtime path falls back to sudo, setuid
mode, host networking, direct host execution, or a writable original checkout.
