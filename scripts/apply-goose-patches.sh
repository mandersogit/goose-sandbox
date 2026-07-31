#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
goose_tree="${GOOSE_SOURCE_DIR:-$repo_root/goose-dev}"
patch_file="$repo_root/patches/goose/0001-preserve-agent-visible-history-provenance.patch"

if [[ ! -d "$goose_tree/.git" && ! -f "$goose_tree/.git" ]]; then
    echo "Goose source is not a Git worktree: $goose_tree" >&2
    exit 1
fi

if git -C "$goose_tree" apply --reverse --check "$patch_file" 2>/dev/null; then
    echo "Goose history-provenance patch is already applied."
    exit 0
fi

git -C "$goose_tree" apply --check "$patch_file"
git -C "$goose_tree" apply "$patch_file"
echo "Applied Goose history-provenance patch to: $goose_tree"
