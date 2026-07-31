#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
definition="$repo_root/containers/apptainer/sandbox-python-arm64.def"
build_config="$repo_root/containers/apptainer/apptainer-build.conf"
runtime_config="$repo_root/containers/apptainer/apptainer-hostile.conf"
state_root="${SANDBOXED_GOOSE_APPTAINER_STATE:-$repo_root/.sandbox/apptainer}"
cache_dir="$state_root/cache"
tmp_dir="$state_root/tmp"
image_dir="$state_root/images"
runtime_home="$state_root/home"
output_image="$image_dir/sandbox-python-arm64.sif"
apptainer_bin="${APPTAINER:-apptainer}"

if [[ "$(uname -m)" != "aarch64" ]]; then
    echo "This definition is pinned to the arm64 Ubuntu image." >&2
    exit 1
fi

if [[ "$(id -u)" != "1000" || "$(id -g)" != "1000" ]]; then
    echo "This first host-specific image expects UID:GID 1000:1000." >&2
    exit 1
fi

if ! command -v "$apptainer_bin" >/dev/null 2>&1; then
    echo "Apptainer is not installed; follow docs/APPTAINER.md first." >&2
    exit 1
fi

install -d -m 0700 \
    "$state_root" \
    "$cache_dir" \
    "$tmp_dir" \
    "$image_dir" \
    "$runtime_home"
build_dir="$(mktemp -d --tmpdir="$image_dir" build.XXXXXXXX)"
build_tmp="$build_dir/container-tmp"
build_var_tmp="$build_dir/container-var-tmp"
temp_image="$build_dir/sandbox-python-arm64.sif"
temp_hash="$build_dir/sandbox-python-arm64.sif.sha256"
install -d -m 1777 "$build_tmp" "$build_var_tmp"

cleanup() {
    chmod -R u+w -- "$build_dir" 2>/dev/null || true
    rm -rf -- "$build_dir"
}
trap cleanup EXIT

host_user="$(id -un)"
host_runtime_dir="/run/user/$(id -u)"

# Apptainer control variables are security-sensitive. Start it with a small
# fixed host-side environment for builds and checks, while retaining only the
# standard user-systemd bus needed for rootless cgroup delegation.
run_apptainer() {
    env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        HOME="$runtime_home" \
        USER="$host_user" \
        LOGNAME="$host_user" \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        XDG_RUNTIME_DIR="$host_runtime_dir" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$host_runtime_dir/bus" \
        APPTAINER_CACHEDIR="$cache_dir" \
        APPTAINER_TMPDIR="$tmp_dir" \
        "$apptainer_bin" "$@"
}

if ! run_apptainer buildcfg | grep -qx 'APPTAINER_SUID_INSTALL=0'; then
    echo "Refusing to build with a setuid Apptainer installation." >&2
    exit 1
fi

(
    cd -- "$build_dir"
    run_apptainer --config "$build_config" build \
        --fakeroot \
        --userns \
        --bind "$build_tmp:/tmp:rw" \
        --bind "$build_var_tmp:/var/tmp:rw" \
        --reproducible \
        "$temp_image" \
        "$definition"
)

# Re-run the embedded content tests under the intended rootless containment
# and cgroup flags. This is a build check, not the full hostile-code suite.
run_apptainer --config "$runtime_config" test \
    --userns \
    --containall \
    --cleanenv \
    --no-eval \
    --no-privs \
    --drop-caps all \
    --no-umask \
    --hostname sandbox \
    --net \
    --network none \
    --no-mount home,cwd,hostfs,bind-paths,sys \
    --no-mount /etc/hosts,/etc/localtime,/etc/resolv.conf \
    --memory 1G \
    --memory-swap 1G \
    --cpus 1 \
    --pids-limit 64 \
    "$temp_image"

host_netns="$(readlink /proc/self/ns/net)"
container_netns="$(
    run_apptainer --config "$runtime_config" exec \
        --userns \
        --containall \
        --cleanenv \
        --no-eval \
        --no-privs \
        --drop-caps all \
        --no-umask \
        --hostname sandbox \
        --net \
        --network none \
        --no-mount home,cwd,hostfs,bind-paths,sys \
        --no-mount /etc/hosts,/etc/localtime,/etc/resolv.conf \
        --memory 256M \
        --memory-swap 256M \
        --cpus 1 \
        --pids-limit 32 \
        "$temp_image" \
        /bin/bash --noprofile --norc -c '
            set -eu
            test "$(id -u)" = 1000
            test ! -e /sys/kernel
            test ! -s /etc/resolv.conf
            grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
            grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
            if touch /usr/.sandboxed-goose-write-test 2>/dev/null; then
                echo "immutable root filesystem was writable" >&2
                exit 1
            fi
            python3 -c "
import socket
s = socket.socket()
s.settimeout(0.25)
if s.connect_ex((\"1.1.1.1\", 53)) == 0:
    raise SystemExit(\"external network unexpectedly reachable\")
"
            readlink /proc/self/ns/net
        '
)"

if [[ "$host_netns" == "$container_netns" ]]; then
    echo "Container did not enter a distinct network namespace." >&2
    exit 1
fi

image_hash="$(sha256sum "$temp_image" | cut -d' ' -f1)"
printf '%s  %s\n' "$image_hash" "$(basename -- "$output_image")" > "$temp_hash"
chmod 0444 "$temp_image" "$temp_hash"
mv -f -- "$temp_image" "$output_image"
mv -f -- "$temp_hash" "$output_image.sha256"

echo "Built: $output_image"
echo "SHA-256: $image_hash"
