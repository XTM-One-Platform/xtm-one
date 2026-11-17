#!/usr/bin/env bash
set -euo pipefail

# End-to-end release helper: create the date-based tag, push it, and open a
# draft GitHub Release whose notes follow the established release template.
#
# Each phase can be toggled independently, so the same command can create a
# tag only, push an existing tag, (re)draft release notes, or preview
# everything with --dry-run. Tag creation and push are delegated to
# scripts/create-release-tag.sh so the versioning logic lives in one place.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# REPO is needed here for the gh calls; the Docker Hub / docs URLs used only in
# the notes footer live in generate-release-notes.sh (their single source).
REPO="XTM-One-Platform/xtm-one"

MAJOR="1"
REMOTE="origin"
TARGET_REF="HEAD"
EXPLICIT_TAG=""
MODEL=""

DRY_RUN="false"
NOTES_ONLY="false"
DO_TAG="true"
DO_PUSH="true"
DO_RELEASE="true"

usage() {
  cat <<'EOF'
Create a release: tag -> push -> draft GitHub Release (notes from template).

Release notes are written by an LLM (GitHub Copilot CLI, `copilot -p`, using
your local Copilot auth) from the commit history, following the established
release template. Note generation lives in scripts/generate-release-notes.sh,
which this script calls.

Usage:
  scripts/release.sh [options]

Phase toggles (all phases run by default):
  --notes-only     Only generate and print the notes to stdout; do nothing else.
  --dry-run        Compute the version and print the plan + notes; change nothing.
  --no-tag         Do not create a local tag (use an existing one).
  --no-push        Do not push the tag to the remote.
  --no-release     Do not create/update the draft GitHub Release.

Options:
  --model NAME     LLM model for the Copilot CLI (default: Copilot's default).
  --major N        Major version to use (default: 1).
  --remote NAME    Git remote (default: origin).
  --ref REF        Tag this commit/ref instead of HEAD.
  --tag TAG        Operate on an explicit existing tag (implies --no-tag).
  -h, --help       Show this help.

Examples:
  scripts/release.sh --notes-only             # just print the notes to check
  scripts/release.sh --dry-run                # preview version + plan + notes
  scripts/release.sh                          # tag, push, draft release
  scripts/release.sh --no-release             # tag + push only
  scripts/release.sh --no-tag --tag 1.260731.0 --no-push  # (re)draft notes

Requires: git; copilot (for LLM notes); gh (authenticated) for the release phase.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --major)   MAJOR="${2:?Missing value for --major}"; shift 2 ;;
    --remote)  REMOTE="${2:?Missing value for --remote}"; shift 2 ;;
    --ref)     TARGET_REF="${2:?Missing value for --ref}"; shift 2 ;;
    --tag)     EXPLICIT_TAG="${2:?Missing value for --tag}"; DO_TAG="false"; shift 2 ;;
    --model)   MODEL="${2:?Missing value for --model}"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --notes-only) NOTES_ONLY="true"; shift ;;
    --no-tag)  DO_TAG="false"; shift ;;
    --no-push) DO_PUSH="false"; shift ;;
    --no-release) DO_RELEASE="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if ! [[ "$MAJOR" =~ ^[0-9]+$ ]] || [[ "$MAJOR" -lt 1 ]]; then
  echo "Invalid major version: $MAJOR" >&2
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This script must be run from inside the git repository." >&2
  exit 1
fi

if ! git fetch "$REMOTE" --tags --force >/dev/null 2>&1; then
  echo "Warning: failed to fetch tags from ${REMOTE}; using local tags only." >&2
fi
# ---------------------------------------------------------------------------
# Resolve the target version.
# ---------------------------------------------------------------------------
if [[ -n "$EXPLICIT_TAG" ]]; then
  if ! [[ "$EXPLICIT_TAG" =~ ^[0-9]+\.[0-9]{6}\.[0-9]+(-lts\.[0-9]+)?$ ]]; then
    echo "Invalid release tag: ${EXPLICIT_TAG}" >&2
    echo "Expected format: MAJOR.YYMMDD.SEQ, for example 1.260618.0" >&2
    exit 1
  fi
  VERSION="$EXPLICIT_TAG"
  # Take the major from the tag so PREV_TAG/RANGE look at the right series
  # (e.g. --tag 2.260731.0 must not compare against the 1.x line).
  MAJOR="${EXPLICIT_TAG%%.*}"
else
  # Delegate version computation to create-release-tag.sh in dry-run mode so
  # the two scripts can never disagree on the next tag.
  DRY_OUT="$(bash "$SCRIPT_DIR/create-release-tag.sh" \
    --major "$MAJOR" --remote "$REMOTE" --ref "$TARGET_REF" --dry-run)"
  VERSION="$(printf '%s\n' "$DRY_OUT" \
    | awk -F': *' '/^Release tag:/ {print $2; exit}' \
    | tr -d '[:space:]')"
fi

if [[ -z "${VERSION:-}" ]]; then
  echo "Could not determine the release version." >&2
  exit 1
fi

# When operating on an explicit existing tag, notes should describe that tag,
# not whatever HEAD happens to be.
if [[ -n "$EXPLICIT_TAG" && "$TARGET_REF" == "HEAD" ]]; then
  TARGET_REF="$EXPLICIT_TAG"
fi

TARGET_COMMIT="$(git rev-parse --verify "${TARGET_REF}^{commit}")"

# Previous release tag = latest matching tag that is not the new version.
PREV_TAG="$(
  git tag -l "${MAJOR}.*" \
    | grep -E "^${MAJOR}\.[0-9]{6}\.[0-9]+(-lts\.[0-9]+)?$" \
    | grep -vxF "$VERSION" \
    | sort -V \
    | tail -1 \
    || true
)"

# Commit range for the notes.
if [[ -n "$PREV_TAG" ]]; then
  RANGE="${PREV_TAG}..${TARGET_COMMIT}"
else
  RANGE="$TARGET_COMMIT"
fi

CHANGE_COUNT="$(git rev-list --no-merges --count "$RANGE")"

# ---------------------------------------------------------------------------
# Generate the release notes (delegated to generate-release-notes.sh, which
# uses the Copilot CLI + the release template).
# ---------------------------------------------------------------------------
# Pass the change count so the notes and this script's plan can never disagree
# on it (single source of truth).
NOTES_ARGS=(--version "$VERSION" --range "$RANGE" --repo "$REPO"
  --change-count "$CHANGE_COUNT")
[[ -n "$PREV_TAG" ]] && NOTES_ARGS+=(--prev "$PREV_TAG")
[[ -n "$MODEL" ]] && NOTES_ARGS+=(--model "$MODEL")
NOTES="$(bash "$SCRIPT_DIR/generate-release-notes.sh" "${NOTES_ARGS[@]}")"

# ---------------------------------------------------------------------------
# --notes-only: just print the notes and exit (no plan, no side effects).
# ---------------------------------------------------------------------------
if [[ "$NOTES_ONLY" == "true" ]]; then
  printf '%s\n' "$NOTES"
  exit 0
fi

# ---------------------------------------------------------------------------
# Report the plan.
# ---------------------------------------------------------------------------
cat <<EOF >&2
Release version: ${VERSION}
Previous tag:    ${PREV_TAG:-<none>}
Remote:          ${REMOTE}
Target ref:      ${TARGET_REF}
Target commit:   ${TARGET_COMMIT}
Changes:         ${CHANGE_COUNT}
Dry run:         ${DRY_RUN}
Create tag:      ${DO_TAG}
Push tag:        ${DO_PUSH}
Draft release:   ${DO_RELEASE}
EOF

if [[ "$DRY_RUN" == "true" ]]; then
  echo "--- release notes (dry run) ---" >&2
  printf '%s\n' "$NOTES"
  exit 0
fi

# ---------------------------------------------------------------------------
# Phase 1 + 2: create (and optionally push) the tag via create-release-tag.sh.
# ---------------------------------------------------------------------------
if [[ "$DO_TAG" == "true" ]]; then
  # Pin the tag to the exact VERSION the notes were built for, so a UTC-date
  # rollover (or a concurrent tag) between resolving the version and creating
  # it can never leave the release describing a different tag than was pushed.
  CREATE_ARGS=(--tag "$VERSION" --remote "$REMOTE" --ref "$TARGET_REF")
  [[ "$DO_PUSH" == "true" ]] && CREATE_ARGS+=(--push)
  bash "$SCRIPT_DIR/create-release-tag.sh" "${CREATE_ARGS[@]}"
elif [[ "$DO_PUSH" == "true" ]]; then
  if ! git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null; then
    echo "Tag ${VERSION} does not exist locally; cannot push (use --tag or drop --no-tag)." >&2
    exit 1
  fi
  git push "$REMOTE" "$VERSION"
  echo "Pushed ${VERSION} to ${REMOTE}."
fi

# ---------------------------------------------------------------------------
# Phase 3: create or update the draft GitHub Release.
# ---------------------------------------------------------------------------
if [[ "$DO_RELEASE" == "true" ]]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "gh CLI is required for the release phase but was not found." >&2
    exit 1
  fi

  NOTES_FILE="$(mktemp)"
  trap 'rm -f "$NOTES_FILE"' EXIT
  printf '%s\n' "$NOTES" > "$NOTES_FILE"

  RELEASE_ARGS=(--title "XTM One ${VERSION}" --notes-file "$NOTES_FILE" --draft --latest=false)
  # The annotated (signed) tag must already be on the remote. Letting gh create
  # the release against a not-yet-pushed tag makes it publish its own lightweight
  # tag, which then collides with our signed tag and blocks the real push.
  remote_tag_line="$(git ls-remote --tags "$REMOTE" "refs/tags/${VERSION}" 2>/dev/null)" || {
    echo "Failed to query remote tags from ${REMOTE}." >&2
    exit 1
  }
  if [[ -z "$remote_tag_line" ]]; then
    echo "Tag ${VERSION} is not on ${REMOTE}; push it before drafting the release" >&2
    echo "(drop --no-push, or run: git push ${REMOTE} ${VERSION})." >&2
    exit 1
  fi

  if gh release view "$VERSION" --repo "$REPO" >/dev/null 2>&1; then
    gh release edit "$VERSION" --repo "$REPO" \
      --title "XTM One ${VERSION}" --notes-file "$NOTES_FILE" \
      --draft=true --latest=false
  else
    gh release create "$VERSION" --repo "$REPO" "${RELEASE_ARGS[@]}"
  fi

  URL="$(gh release view "$VERSION" --repo "$REPO" --json url --jq .url 2>/dev/null || true)"
  echo "Draft GitHub Release ready: ${URL:-<see GitHub releases>}"
fi
