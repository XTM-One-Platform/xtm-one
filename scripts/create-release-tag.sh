#!/usr/bin/env bash
set -euo pipefail

MAJOR="1"
REMOTE="origin"
DRY_RUN="false"
PUSH_TAG="false"
TARGET_REF="HEAD"
EXPLICIT_TAG=""

usage() {
  cat <<'EOF'
Create the next date-based release tag.

Usage:
  scripts/create-release-tag.sh [--major N] [--remote origin] [--ref REF] [--tag TAG] [--dry-run] [--push]

Version format:
  MAJOR.YYMMDD.SEQ

Examples:
  scripts/create-release-tag.sh --dry-run
  scripts/create-release-tag.sh --major 1
  scripts/create-release-tag.sh --major 1 --ref 5bfb376
  scripts/create-release-tag.sh --major 1 --push
  scripts/create-release-tag.sh --tag 1.260731.0 --ref 5bfb376

By default the script creates a signed tag locally. Use --push to push the
tag to the remote and trigger the tag-based release workflow.
Use --ref to create the tag on a specific commit or ref instead of HEAD.
Use --tag to create a specific, pre-computed tag instead of deriving the next
date-based one (the major is taken from the tag).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --major)
      if [[ -z "${2:-}" ]]; then
        echo "Missing value for --major" >&2
        exit 1
      fi
      MAJOR="$2"
      shift 2
      ;;
    --remote)
      if [[ -z "${2:-}" ]]; then
        echo "Missing value for --remote" >&2
        exit 1
      fi
      REMOTE="$2"
      shift 2
      ;;
    --ref)
      if [[ -z "${2:-}" ]]; then
        echo "Missing value for --ref" >&2
        exit 1
      fi
      TARGET_REF="$2"
      shift 2
      ;;
    --tag)
      if [[ -z "${2:-}" ]]; then
        echo "Missing value for --tag" >&2
        exit 1
      fi
      EXPLICIT_TAG="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --push)
      PUSH_TAG="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! [[ "$MAJOR" =~ ^[0-9]+$ ]] || [[ "$MAJOR" -lt 1 ]]; then
  echo "Invalid major version: $MAJOR" >&2
  exit 1
fi

# An explicit tag pins the exact version; take the major from it so the
# latest-tag / ancestry checks below look at the right series.
if [[ -n "$EXPLICIT_TAG" ]]; then
  if ! [[ "$EXPLICIT_TAG" =~ ^[0-9]+\.[0-9]{6}\.[0-9]+(-lts\.[0-9]+)?$ ]]; then
    echo "Invalid release tag: ${EXPLICIT_TAG}" >&2
    echo "Expected format: MAJOR.YYMMDD.SEQ, for example 1.260618.0" >&2
    exit 1
  fi
  MAJOR="${EXPLICIT_TAG%%.*}"
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "This script must be run from inside the git repository." >&2
  exit 1
fi

git fetch "$REMOTE" --tags --force

TARGET_COMMIT="$(git rev-parse --verify "${TARGET_REF}^{commit}")"

LATEST_TAG="$(
  git tag -l "${MAJOR}.*" \
    | grep -E "^${MAJOR}\.[0-9]{6}\.[0-9]+(-lts\.[0-9]+)?$" \
    | sort -V \
    | tail -1 \
    || true
)"

if [[ -n "$LATEST_TAG" ]]; then
  LATEST_COMMIT="$(git rev-list -n 1 "$LATEST_TAG")"
  if [[ "$TARGET_COMMIT" == "$LATEST_COMMIT" ]] || ! git merge-base --is-ancestor "$LATEST_COMMIT" "$TARGET_COMMIT"; then
    echo "Target ref ${TARGET_REF} must be after latest release tag ${LATEST_TAG}." >&2
    exit 1
  fi
fi

if [[ "$DRY_RUN" != "true" && "$TARGET_REF" == "HEAD" && -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit or stash changes, or pass --ref to tag a specific commit." >&2
  exit 1
fi

if [[ -n "$EXPLICIT_TAG" ]]; then
  # Caller pinned the exact tag; use it verbatim rather than deriving one.
  VERSION="$EXPLICIT_TAG"
  TAG="$EXPLICIT_TAG"
else
  DATE="$(date -u +%y%m%d)"
  PREFIX="${MAJOR}.${DATE}."
  MATCHING_TAGS="$(
    git tag -l "${PREFIX}*" \
      | grep -E "^${MAJOR}\.${DATE}\.[0-9]+$" \
      || true
  )"
  MAX_SEQ="$(
    printf '%s\n' "$MATCHING_TAGS" \
      | sed -E "s/^${MAJOR}\.${DATE}\.([0-9]+)$/\1/" \
      | sort -n \
      | tail -1
  )"
  SEQ="$(( ${MAX_SEQ:--1} + 1 ))"
  VERSION="${MAJOR}.${DATE}.${SEQ}"
  TAG="${VERSION}"
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Tag already exists locally: ${TAG}" >&2
  exit 1
fi

cat <<EOF
Release version: ${VERSION}
Release tag:     ${TAG}
Remote:          ${REMOTE}
Target ref:      ${TARGET_REF}
Target commit:   ${TARGET_COMMIT}
Dry run:         ${DRY_RUN}
Push tag:        ${PUSH_TAG}
EOF

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry run enabled; not creating tag ${TAG}."
  exit 0
fi

git tag -s "$TAG" "$TARGET_COMMIT" -m "XTM One ${VERSION}"
echo "Created local signed tag ${TAG}."

if [[ "$PUSH_TAG" == "true" ]]; then
  git push "$REMOTE" "$TAG"
  echo "Pushed ${TAG} to ${REMOTE}. The tag push will trigger the release workflow."
else
  echo "Review locally, then push with: git push ${REMOTE} ${TAG}"
fi
