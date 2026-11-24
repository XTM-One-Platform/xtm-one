#!/usr/bin/env bash
#
# Build a single sandbox image. The one `docker build` invocation shared
# by create-register-images.sh, the CI smoke-test matrix
# (sandbox-images.yml), and the on-demand scan workflow
# (sandbox-image-scan.yml), so a new build-arg or Dockerfile target only
# needs to be added here once instead of in three hand-kept-in-sync places.
# Usage: build-image.sh <image>:<tag>
#   e.g. build-image.sh sandbox-python-network:<version>
#        build-image.sh sandbox-nodejs:<version>
# The image name picks the entry in sandbox-images.json; the tag is yours (the
# version the app's OPENSANDBOX_IMAGES points at, or a throwaway name in CI).
#
# A plain `docker build`: it reuses Docker's own local layer cache across runs,
# and sandbox-images.yml builds cold (its layer caches filled the repository's
# cache quota).
set -uo pipefail

cd "$(dirname "$0")" || exit 1

TAG="${1:-}"
if [ -z "$TAG" ]; then
    echo "Usage: $0 <image>:<tag>" >&2
    exit 1
fi
IMAGE="${TAG##*/}"   # drop a registry or namespace prefix
IMAGE="${IMAGE%%:*}"  # drop the tag

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: 'docker' is not installed or not in PATH." >&2
    exit 1
fi

_build() {
    docker build "$@"
}

# nodejs has no entry in sandbox-images.json (single fixed build, no
# per-domain apt/req/target matrix).
if [ "$IMAGE" = "sandbox-nodejs" ]; then
    _build -f Dockerfile.sandbox.nodejs -t "$TAG" .
    exit $?
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: 'jq' is not installed or not in PATH (required to read sandbox-images.json)." >&2
    exit 1
fi

ENTRY=$(jq -e --arg image "$IMAGE" '.[] | select(.image == $image)' sandbox-images.json) || {
    echo "Error: '$IMAGE' is not an image in sandbox-images.json (and is not sandbox-nodejs)." >&2
    exit 1
}

_build -f Dockerfile.sandbox.python \
  --target "$(echo "$ENTRY" | jq -r .final_target)" \
  --build-arg APT_FILE="$(echo "$ENTRY" | jq -r .apt_file)" \
  --build-arg REQ_FILE="$(echo "$ENTRY" | jq -r .req_file)" \
  --build-arg RUNTIME_APT_FILE="$(echo "$ENTRY" | jq -r .runtime_apt_file)" \
  -t "$TAG" .
