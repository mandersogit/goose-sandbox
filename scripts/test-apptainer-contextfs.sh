#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
runtime_config="$repo_root/containers/apptainer/apptainer-hostile-context.conf"
ordinary_config="$repo_root/containers/apptainer/apptainer-hostile.conf"
state_root="${SANDBOXED_GOOSE_APPTAINER_STATE:-$repo_root/.sandbox/apptainer}"
cache_dir="$state_root/cache"
tmp_dir="$state_root/tmp"
runtime_home="$state_root/home"
image="${SANDBOXED_GOOSE_CONTEXT_IMAGE:-$state_root/images/sandbox-python-context-arm64.sif}"
apptainer_bin="${APPTAINER:-apptainer}"

if [[ ! -r "$image" ]]; then
    echo "Context image is missing: $image" >&2
    exit 1
fi

install -d -m 0700 "$state_root" "$cache_dir" "$tmp_dir" "$runtime_home"
test_dir="$(mktemp -d --tmpdir="$state_root" context-test.XXXXXXXX)"
cleanup() {
    rm -rf -- "$test_dir"
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

runtime_flags=(
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
    --memory 512M
    --memory-swap 512M
    --cpus 1
    --pids-limit 32
)
fuse_spec="container:/usr/local/bin/sandboxed-goose-contextfs /context"
session_bundle_container="/run/sandboxed-goose/session-context.json"

if run_apptainer --config "$ordinary_config" \
    "${runtime_flags[@]}" \
    --fusemount "$fuse_spec" \
    "$image" \
    /bin/true >"$test_dir/ordinary.stdout" 2>"$test_dir/ordinary.stderr"; then
    echo "Ordinary hostile profile unexpectedly allowed FUSE." >&2
    exit 1
fi
if ! grep -q "fusemount disabled" "$test_dir/ordinary.stderr"; then
    echo "Ordinary hostile profile failed for an unexpected reason." >&2
    sed -n '1,120p' "$test_dir/ordinary.stderr" >&2
    exit 1
fi

find /sys/fs/fuse/connections -mindepth 1 -maxdepth 1 -type d \
    -printf '%f\n' 2>/dev/null | sort -n >"$test_dir/connections.before"
awk '$0 ~ / - fuse(\.| )/ { print }' /proc/self/mountinfo \
    >"$test_dir/host-mounts.before"

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
    "$apptainer_bin" --config "$runtime_config" \
    "${runtime_flags[@]}" \
    --fusemount "$fuse_spec" \
    "$image" \
    /bin/bash --noprofile --norc -c '
        set -eu

        test ! -e /dev/fuse
        test ! -e /usr/bin/fusermount3
        test -r /context/manifest.json
        test -r /context/generated/primes.json
        test -r /context/objects/answer/content.txt
        test "$(jq -r .snapshot_id /context/manifest.json)" = toy-v1
        test "$(jq -r .storage /context/manifest.json)" = generated-in-memory
        test "$(jq -r .read_only /context/manifest.json)" = true
        test "$(jq -r ".values[-1]" /context/generated/primes.json)" = 47
        grep -qx "The computed answer is 6 \* 7 = 42\." /context/objects/answer/content.txt

        python3 -c "
import hashlib
import json
from pathlib import Path

root = Path(\"/context\")
manifest = json.loads((root / \"manifest.json\").read_bytes())
for item in manifest[\"files\"]:
    content = (root / item[\"path\"]).read_bytes()
    assert len(content) == item[\"size\"]
    assert hashlib.sha256(content).hexdigest() == item[\"sha256\"]
"

        if printf x 2>/dev/null > /context/new-file; then
            echo "ContextFS allowed file creation." >&2
            exit 1
        fi
        if rm /context/README.md 2>/dev/null; then
            echo "ContextFS allowed deletion." >&2
            exit 1
        fi
        if chmod u+w /context/README.md 2>/dev/null; then
            echo "ContextFS allowed chmod." >&2
            exit 1
        fi

        mount_line="$(awk '\''$5 == "/context" { print; found=1 } END { exit !found }'\'' /proc/self/mountinfo)"
        case "$mount_line" in
            *" - fuse "*|*" - fuse."*) ;;
            *)
                echo "Unexpected ContextFS mount: $mount_line" >&2
                exit 1
                ;;
        esac
        case "$mount_line" in
            *nosuid,nodev*) ;;
            *)
                echo "ContextFS lacks expected nosuid,nodev flags: $mount_line" >&2
                exit 1
                ;;
        esac
        case "$mount_line" in
            *allow_other*)
                echo "ContextFS unexpectedly enabled allow_other." >&2
                exit 1
                ;;
        esac

        frontend_needle="/usr/local/bin/sandboxed-goose-context""fs /dev/fd/3 -f"
        frontend_pid=""
        for cmdline_path in /proc/[0-9]*/cmdline; do
            cmdline="$(tr "\000" " " < "$cmdline_path" 2>/dev/null || true)"
            case "$cmdline" in
                *"$frontend_needle"*)
                    frontend_pid="${cmdline_path#/proc/}"
                    frontend_pid="${frontend_pid%/cmdline}"
                    break
                    ;;
            esac
        done
        test -n "$frontend_pid"

        jq -c '\''{
            snapshot_id,
            storage,
            read_only,
            projected_files: (.files | length)
        }'\'' /context/manifest.json
        echo "mount=$mount_line"
        echo "frontend_pid_visible=$frontend_pid"
    '

find /sys/fs/fuse/connections -mindepth 1 -maxdepth 1 -type d \
    -printf '%f\n' 2>/dev/null | sort -n >"$test_dir/connections.after"
awk '$0 ~ / - fuse(\.| )/ { print }' /proc/self/mountinfo \
    >"$test_dir/host-mounts.after"

if ! diff -u "$test_dir/connections.before" "$test_dir/connections.after"; then
    echo "A FUSE connection leaked after Apptainer exited." >&2
    exit 1
fi
if ! diff -u "$test_dir/host-mounts.before" "$test_dir/host-mounts.after"; then
    echo "A ContextFS mount leaked into the host namespace." >&2
    exit 1
fi

session_database="$test_dir/sessions.db"
session_bundle="$test_dir/session-context.json"
sqlite3 "$session_database" <<'SQL'
CREATE TABLE sessions (id TEXT PRIMARY KEY);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_timestamp INTEGER NOT NULL,
    metadata_json TEXT
);
INSERT INTO sessions (id) VALUES ('current-session'), ('other-session');
INSERT INTO messages
    (message_id, session_id, role, content_json, created_timestamp, metadata_json)
VALUES
    ('m0', 'current-session', 'user',
     '[{"type":"text","text":"PRECOMPACTION_HISTORY_MARKER"}]',
     0, '{"userVisible":true,"agentVisible":false,"historicallyAgentVisible":true}'),
    ('m1', 'current-session', 'user',
     '[{"type":"text","text":"VISIBLE_SESSION_MARKER"},{"type":"text","text":"ANNOTATED_USER_ONLY_SECRET","annotations":{"audience":["user"]}}]',
     1, '{"userVisible":true,"agentVisible":true}'),
    ('m2', 'current-session', 'assistant',
     '[{"type":"toolRequest","id":"tool-1","toolCall":{"status":"success","value":{"name":"calculate","arguments":{"expression":"6*7"}}},"_meta":{"secret":"INTERNAL_META_SECRET"}}]',
     2, '{"userVisible":true,"agentVisible":true}'),
    ('m3', 'current-session', 'user',
     '[{"type":"toolResponse","id":"tool-1","toolResult":{"status":"success","value":{"content":[{"type":"text","text":"VISIBLE_TOOL_RESULT"}],"structuredContent":{"secret":"STRUCTURED_SECRET"}}}}]',
     3, '{"userVisible":true,"agentVisible":true}'),
    ('m4', 'current-session', 'assistant',
     '[{"type":"text","text":"COMPACTION_SUMMARY_MARKER"}]',
     4, '{"userVisible":false,"agentVisible":true}'),
    ('m5', 'current-session', 'user',
     '[{"type":"text","text":"USER_ONLY_ROW_SECRET"}]',
     5, '{"userVisible":true,"agentVisible":false}'),
    ('m6', 'current-session', 'assistant',
     '[{"type":"thinking","thinking":"THINKING_SECRET","signature":"sig"}]',
     6, '{"userVisible":true,"agentVisible":true}'),
    ('other-m1', 'other-session', 'user',
     '[{"type":"text","text":"OTHER_SESSION_SECRET"}]',
     1, '{"userVisible":true,"agentVisible":true}');
SQL

PYTHONPATH="$repo_root/src" python3 -m sandboxed_goose.contextfs.goose_session \
    --database "$session_database" \
    --session-id current-session \
    --output "$session_bundle"
test "$(stat -c %a "$session_bundle")" = 600

session_fuse_spec="container:/usr/local/bin/sandboxed-goose-contextfs --bundle $session_bundle_container /context"
run_apptainer --config "$runtime_config" \
    "${runtime_flags[@]}" \
    --bind "$session_bundle:$session_bundle_container:ro" \
    --fusemount "$session_fuse_spec" \
    "$image" \
    /bin/bash --noprofile --norc -c '
        set -eu

        test "$(jq -r .projection /context/manifest.json)" = goose-session-disclosed-history
        test "$(jq -r .session_id /context/manifest.json)" = current-session
        test "$(jq -r .source_message_rows /context/manifest.json)" = 7
        test "$(jq -r .current_agent_visible_rows /context/manifest.json)" = 5
        test "$(jq -r .historical_agent_visible_rows /context/manifest.json)" = 1
        test "$(jq -r .omitted_unprojected_rows /context/manifest.json)" = 1
        test "$(jq -r .limits.max_events /context/manifest.json)" = 700
        test "$(find /context/session/messages -type f | wc -l)" = 6

        grep -q PRECOMPACTION_HISTORY_MARKER /context/session/transcript.md
        grep -q VISIBLE_SESSION_MARKER /context/session/transcript.md
        grep -q VISIBLE_TOOL_RESULT /context/session/transcript.md
        grep -q COMPACTION_SUMMARY_MARKER /context/session/transcript.md
        grep -Rq '"name": "calculate"' /context/session/events

        for secret in \
            ANNOTATED_USER_ONLY_SECRET \
            INTERNAL_META_SECRET \
            STRUCTURED_SECRET \
            USER_ONLY_ROW_SECRET \
            THINKING_SECRET \
            OTHER_SESSION_SECRET; do
            if grep -Rq "$secret" /context; then
                echo "Session projection leaked $secret." >&2
                exit 1
            fi
        done

        if printf x 2>/dev/null > /context/session/messages/new.json; then
            echo "Session ContextFS allowed file creation." >&2
            exit 1
        fi
        if printf x 2>/dev/null > /run/sandboxed-goose/session-context.json; then
            echo "Session bundle bind was writable." >&2
            exit 1
        fi

        jq -c "{
            snapshot_id,
            session_id,
            projected_messages,
            projected_events,
            omitted_unprojected_rows
        }" /context/manifest.json
    '

find /sys/fs/fuse/connections -mindepth 1 -maxdepth 1 -type d \
    -printf '%f\n' 2>/dev/null | sort -n >"$test_dir/connections.session"
awk '$0 ~ / - fuse(\.| )/ { print }' /proc/self/mountinfo \
    >"$test_dir/host-mounts.session"
if ! diff -u "$test_dir/connections.before" "$test_dir/connections.session"; then
    echo "A FUSE connection leaked after the session projection exited." >&2
    exit 1
fi
if ! diff -u "$test_dir/host-mounts.before" "$test_dir/host-mounts.session"; then
    echo "A session ContextFS mount leaked into the host namespace." >&2
    exit 1
fi

if run_apptainer --config "$runtime_config" \
    "${runtime_flags[@]}" \
    --fusemount "$fuse_spec" \
    "$image" \
    /bin/bash --noprofile --norc -c '
        set -eu
        test -r /context/manifest.json
        kill -TERM $$
    ' >"$test_dir/termination.stdout" 2>"$test_dir/termination.stderr"; then
    echo "A payload that signaled itself unexpectedly exited successfully." >&2
    exit 1
fi

find /sys/fs/fuse/connections -mindepth 1 -maxdepth 1 -type d \
    -printf '%f\n' 2>/dev/null | sort -n >"$test_dir/connections.terminated"
awk '$0 ~ / - fuse(\.| )/ { print }' /proc/self/mountinfo \
    >"$test_dir/host-mounts.terminated"

if ! diff -u "$test_dir/connections.before" "$test_dir/connections.terminated"; then
    echo "A FUSE connection leaked after signal termination of the payload." >&2
    exit 1
fi
if ! diff -u "$test_dir/host-mounts.before" "$test_dir/host-mounts.terminated"; then
    echo "A FUSE mount leaked after signal termination of the payload." >&2
    exit 1
fi

echo "ContextFS Apptainer proof passed."
