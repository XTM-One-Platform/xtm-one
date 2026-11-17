#!/usr/bin/env bash
# Runs tests/test_migration_integrity.py against the tree a merge of HEAD into
# the live tip of main would produce right now. Two pull requests cut from the
# same main are each green on their own and fork the Alembic history once both
# land: once a migration lands on main, every open migration PR that forked
# from it goes red on its next run instead of at merge time.
# Needs the full history and `pytest` on the PATH; run from the repository root.
set -euo pipefail

# Explicit refspec: a bare `git fetch origin main` would leave origin/main at
# whatever the checkout happened to fetch. Offline, or in a clone without an
# origin, a local run checks what it has rather than stopping ci-local.sh.
if ! git fetch --no-tags --quiet origin +refs/heads/main:refs/remotes/origin/main 2>/dev/null; then
  echo "Could not reach origin - checking against the local origin/main, which may be stale."
fi
if ! MAIN=$(git rev-parse --verify --quiet refs/remotes/origin/main); then
  echo "No origin/main ref - skipping."
  exit 0
fi
echo "Live tip of main: $MAIN"

if git merge-base --is-ancestor "$MAIN" HEAD; then
  echo "Branch already contains origin/main - nothing further to check."
  exit 0
fi

# A conflict leaves the post-merge state undefined, and GitHub blocks the
# merge until it is resolved: the push that resolves it runs this again.
if ! MERGED_TREE=$(git merge-tree --write-tree HEAD "$MAIN" 2>/dev/null); then
  echo "::warning::Branch conflicts with origin/main - cannot evaluate the post-merge migration history. Rebase; this check re-runs on the next push."
  exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git archive "$MERGED_TREE" backend | tar -x -C "$WORK"
cd "$WORK/backend"
if pytest tests/test_migration_integrity.py --noconftest -v --tb=short; then
  echo "Migration history stays consistent once merged into main ($MAIN)."
  exit 0
fi

echo ""
echo "The migration history is valid on this branch but BREAKS once merged into the current main."
echo "  Rebase onto main, re-point your earliest migration's down_revision to main's new head,"
echo "  and re-verify with: cd backend && uv run python -m pytest tests/test_migration_integrity.py"
exit 1
