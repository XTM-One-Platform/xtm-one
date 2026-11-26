#!/bin/sh
# `make setup-hooks`: point git at the hooks of .githooks (the Enterprise Edition
# license header check, and the commit subject rule of commit-msg). core.hooksPath is shared by every worktree of the clone
# and replaces .git/hooks, so an existing setting is left alone and hooks of
# your own in .git/hooks are named before they stop running.
set -eu

current=$(git config --get core.hooksPath || true)
if [ -n "$current" ] && [ "$current" != ".githooks" ]; then
    echo "core.hooksPath is already '$current': left as it is. Call .githooks/pre-commit and .githooks/commit-msg from your own hooks." >&2
    exit 1
fi

hooks_dir="$(git rev-parse --git-common-dir)/hooks"
if [ -d "$hooks_dir" ]; then
    # A hook manager often installs links, not files: name those too.
    own=$(find "$hooks_dir" -maxdepth 1 \( -type f -o -type l \) ! -name '*.sample' -exec basename {} \; | tr '\n' ' ')
    if [ -n "$own" ]; then
        echo "Note: your hooks in $hooks_dir stop running ($own). Call them from .githooks if you need them." >&2
    fi
fi

git config core.hooksPath .githooks
echo "pre-commit and commit-msg hooks enabled for every worktree of this clone (.githooks)."
