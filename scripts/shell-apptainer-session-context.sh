#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin="$repo_root/local.venv/bin/python"
exporter="$repo_root/local.venv/bin/sandboxed-goose-export-session"

usage() {
    cat <<'EOF'
Usage:
  scripts/shell-apptainer-session-context.sh --fixture FIXTURE.json
  scripts/shell-apptainer-session-context.sh --database sessions.db --session-id ID

Open an interactive Bash shell in the context-enabled Apptainer image with one
exact Goose session projected read-only at /context.

Options:
  --fixture PATH     Fixture manifest created by create-goose-session-fixture.py
  --database PATH    Exact Goose sessions.db path
  --session-id ID    Exact Goose session ID to project
  -h, --help         Show this help

Environment overrides:
  APPTAINER
  SANDBOXED_GOOSE_CONTEXT_IMAGE
  SANDBOXED_GOOSE_APPTAINER_CONFIG
  SANDBOXED_GOOSE_APPTAINER_STATE
EOF
}

fixture=""
database=""
session_id=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fixture)
            [[ $# -ge 2 ]] || { echo "error: --fixture requires a path" >&2; exit 2; }
            fixture="$2"
            shift 2
            ;;
        --database)
            [[ $# -ge 2 ]] || { echo "error: --database requires a path" >&2; exit 2; }
            database="$2"
            shift 2
            ;;
        --session-id)
            [[ $# -ge 2 ]] || { echo "error: --session-id requires a value" >&2; exit 2; }
            session_id="$2"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -n "$fixture" ]]; then
    if [[ -n "$database" || -n "$session_id" ]]; then
        echo "error: --fixture cannot be combined with --database or --session-id" >&2
        exit 2
    fi
    if [[ ! -x "$python_bin" ]]; then
        echo "error: project Python is missing; run 'make install'" >&2
        exit 1
    fi
    mapfile -d '' -t fixture_fields < <(
        "$python_bin" -c '
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    database = value["database"]
    session_id = value["primary_session_id"]
except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
    raise SystemExit(f"invalid fixture manifest: {error}") from error
if not isinstance(database, str) or not isinstance(session_id, str):
    raise SystemExit("invalid fixture manifest: database and primary_session_id must be strings")
os.write(1, database.encode("utf-8") + b"\0" + session_id.encode("utf-8") + b"\0")
' "$fixture"
    )
    if [[ "${#fixture_fields[@]}" -ne 2 ]]; then
        echo "error: fixture manifest did not yield a database and session ID" >&2
        exit 1
    fi
    database="${fixture_fields[0]}"
    session_id="${fixture_fields[1]}"
elif [[ -z "$database" || -z "$session_id" ]]; then
    echo "error: supply --fixture or both --database and --session-id" >&2
    usage >&2
    exit 2
fi

if [[ ! -x "$exporter" ]]; then
    echo "error: session exporter is missing; run 'make install'" >&2
    exit 1
fi
if [[ ! -r "$database" || ! -f "$database" ]]; then
    echo "error: session database is not a readable regular file: $database" >&2
    exit 1
fi
if [[ -z "$session_id" || "$session_id" != "${session_id//$'\n'/}" ]]; then
    echo "error: session ID must be non-empty and contain no newline" >&2
    exit 2
fi

state_root="${SANDBOXED_GOOSE_APPTAINER_STATE:-$repo_root/.sandbox/apptainer}"
image="${SANDBOXED_GOOSE_CONTEXT_IMAGE:-$state_root/images/sandbox-python-context-arm64.sif}"
runtime_config="${SANDBOXED_GOOSE_APPTAINER_CONFIG:-$repo_root/containers/apptainer/apptainer-hostile-context.conf}"
apptainer_bin="${APPTAINER:-apptainer}"

if [[ ! -r "$image" || ! -f "$image" ]]; then
    echo "error: context image is missing: $image" >&2
    echo "run 'make apptainer-context-image' first" >&2
    exit 1
fi
if [[ ! -r "$runtime_config" || ! -f "$runtime_config" ]]; then
    echo "error: Apptainer context policy is missing: $runtime_config" >&2
    exit 1
fi
if [[ "$apptainer_bin" == */* ]]; then
    [[ -x "$apptainer_bin" ]] || { echo "error: Apptainer is not executable" >&2; exit 1; }
elif ! command -v "$apptainer_bin" >/dev/null 2>&1; then
    echo "error: Apptainer is not available on PATH" >&2
    exit 1
fi

cache_dir="$state_root/cache"
temporary_dir="$state_root/tmp"
runtime_home="$state_root/home"
install -d -m 0700 "$state_root" "$cache_dir" "$temporary_dir" "$runtime_home"
run_dir="$(mktemp -d --tmpdir="$state_root" manual-context.XXXXXXXX)"
bundle="$run_dir/session-context.json"
cleanup() {
    rm -f -- "$bundle" 2>/dev/null || true
    rmdir -- "$run_dir" 2>/dev/null || true
}
trap cleanup EXIT

"$exporter" \
    --database "$database" \
    --session-id "$session_id" \
    --output "$bundle"

host_user="$(id -un)"
host_runtime_dir="/run/user/$(id -u)"
session_bundle_container="/run/sandboxed-goose/session-context.json"
fuse_spec="container:/usr/local/bin/sandboxed-goose-contextfs --bundle $session_bundle_container /context"

echo "Opening offline sandbox shell for Goose session: $session_id"
echo "Projected session: /context"
echo "Exit the shell to remove the temporary projection bundle."

shell_status=0
env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME="$runtime_home" \
    USER="$host_user" \
    LOGNAME="$host_user" \
    TERM=xterm-256color \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    XDG_RUNTIME_DIR="$host_runtime_dir" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$host_runtime_dir/bus" \
    APPTAINER_CACHEDIR="$cache_dir" \
    APPTAINER_TMPDIR="$temporary_dir" \
    "$apptainer_bin" --config "$runtime_config" \
    exec \
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
    --memory 512M \
    --memory-swap 512M \
    --cpus 1 \
    --pids-limit 32 \
    --cwd / \
    --env TERM=xterm-256color \
    --bind "$bundle:$session_bundle_container:ro" \
    --fusemount "$fuse_spec" \
    "$image" \
    /bin/bash --noprofile --norc -i || shell_status=$?

cleanup
trap - EXIT
exit "$shell_status"
