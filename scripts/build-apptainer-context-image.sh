#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
definition="$repo_root/containers/apptainer/sandbox-python-context-arm64.def"
build_config="$repo_root/containers/apptainer/apptainer-build.conf"
state_root="${SANDBOXED_GOOSE_APPTAINER_STATE:-$repo_root/.sandbox/apptainer}"
cache_dir="$state_root/cache"
tmp_dir="$state_root/tmp"
image_dir="$state_root/images"
runtime_home="$state_root/home"
base_image="$image_dir/sandbox-python-arm64.sif"
output_image="$image_dir/sandbox-python-context-arm64.sif"
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

if [[ ! -r "$base_image" ]]; then
    echo "Base image is missing: $base_image" >&2
    echo "Run scripts/build-apptainer-image.sh first." >&2
    exit 1
fi

install -d -m 0700 \
    "$state_root" \
    "$cache_dir" \
    "$tmp_dir" \
    "$image_dir" \
    "$runtime_home"
build_dir="$(mktemp -d --tmpdir="$image_dir" context-build.XXXXXXXX)"
build_tmp="$build_dir/container-tmp"
build_var_tmp="$build_dir/container-var-tmp"
temp_image="$build_dir/sandbox-python-context-arm64.sif"
temp_hash="$build_dir/sandbox-python-context-arm64.sif.sha256"
install -d -m 1777 "$build_tmp" "$build_var_tmp"

cleanup() {
    chmod -R u+w -- "$build_dir" 2>/dev/null || true
    rm -rf -- "$build_dir"
}
trap cleanup EXIT

host_user="$(id -un)"
host_runtime_dir="/run/user/$(id -u)"

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

python3 -m pip wheel \
    --disable-pip-version-check \
    --no-build-isolation \
    --no-deps \
    --wheel-dir "$build_dir" \
    "$repo_root"

mapfile -t project_wheels < <(
    find "$build_dir" -maxdepth 1 -type f -name 'sandboxed_goose-*.whl' -print
)
if [[ "${#project_wheels[@]}" != "1" ]]; then
    echo "Expected exactly one sandboxed-goose wheel, found ${#project_wheels[@]}." >&2
    exit 1
fi
project_wheel="${project_wheels[0]}"

(
    cd -- "$build_dir"
    run_apptainer --config "$build_config" build \
        --fakeroot \
        --userns \
        --bind "$build_tmp:/tmp:rw" \
        --bind "$build_var_tmp:/var/tmp:rw" \
        --build-arg "base_image=$base_image" \
        --build-arg "project_wheel=$project_wheel" \
        --reproducible \
        "$temp_image" \
        "$definition"
)

SANDBOXED_GOOSE_CONTEXT_IMAGE="$temp_image" \
    "$repo_root/scripts/test-apptainer-contextfs.sh"

image_hash="$(sha256sum "$temp_image" | cut -d' ' -f1)"
printf '%s  %s\n' "$image_hash" "$(basename -- "$output_image")" > "$temp_hash"
chmod 0444 "$temp_image" "$temp_hash"
mv -f -- "$temp_image" "$output_image"
mv -f -- "$temp_hash" "$output_image.sha256"

echo "Built: $output_image"
echo "SHA-256: $image_hash"
