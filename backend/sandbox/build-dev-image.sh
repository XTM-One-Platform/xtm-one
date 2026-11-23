#!/usr/bin/env bash
#
# Build one development sandbox image, from the repository root (not in
# sandbox-images.json, whose entries build from this directory):
#   python-dev, nodejs-dev  Dockerfile.sandbox.dev: THIS repository's locked
#       dependencies for sandbox_run_command (.xtm-one/checks.toml):
#       backend/uv.lock (dev group), frontend/yarn.lock in the yarn cache.
#   toolchain-python, toolchain-node, toolchain-jvm, toolchain-rust,
#   toolchain-dbt  Dockerfile.sandbox.toolchain: generic build environments
#       of a stack, no lock baked; a checkout installs its own dependencies.
#
# Usage: bash build-dev-image.sh <target> <tag>
#   e.g. bash build-dev-image.sh python-dev sandbox-python-dev:0.1
#        bash build-dev-image.sh toolchain-jvm sandbox-toolchain-jvm:0.1
set -uo pipefail

TARGET="${1:-}"
TAG="${2:-}"
case "$TARGET" in
    python-dev|nodejs-dev)
        DOCKERFILE=backend/sandbox/Dockerfile.sandbox.dev ;;
    toolchain-python|toolchain-node|toolchain-jvm|toolchain-rust|toolchain-dbt)
        DOCKERFILE=backend/sandbox/Dockerfile.sandbox.toolchain ;;
    *)
        DOCKERFILE= ;;
esac
if [ -z "$DOCKERFILE" ] || [ -z "$TAG" ]; then
    echo "Usage: $0 <python-dev|nodejs-dev|toolchain-python|toolchain-node|toolchain-jvm|toolchain-rust|toolchain-dbt> <image>:<tag>" >&2
    exit 1
fi

cd "$(dirname "$0")/../.." || exit 1

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: 'docker' is not installed or not in PATH." >&2
    exit 1
fi

docker build -f "$DOCKERFILE" --target "$TARGET" -t "$TAG" .
