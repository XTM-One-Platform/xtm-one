#!/usr/bin/env bash
#
# `env bash` (not a hardcoded /bin/bash path) so this also resolves correctly
# on macOS, where /bin/bash is a fixed old build and a newer bash from
# Homebrew is typically installed elsewhere on PATH. Without an explicit bash
# shebang at all, running this via `sh create-register-images.sh` (or a shell
# that treats it as POSIX sh) breaks on bash-only syntax below (process
# substitution, `IFS=$'\t'`).
#
# `-u`/`pipefail`, not `-e`: a failed `docker build` (e.g. sandbox-python-data
# — see sandbox-images.json's ci_skip note) is expected to be reported and
# skipped, not to abort the whole script before the remaining images build.
#
# Usage: create-register-images.sh <version>   e.g. create-register-images.sh 0.3
# Tags every image `<image>:<version>`: the version OPENSANDBOX_IMAGES uses.
set -uo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "Usage: $0 <version>" >&2
    exit 1
fi

pushd "$(dirname "$0")" > /dev/null || exit 1

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: 'docker' is not installed or not in PATH." >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: 'jq' is not installed or not in PATH (required to read sandbox-images.json)." >&2
    exit 1
fi

# Build every Python image from the single source of truth in sandbox-images.json,
# via build-image.sh, so this script, the CI smoke-test job, and the on-demand
# scan workflow never drift out of sync on the actual `docker build` invocation.
while IFS=$'\t' read -r image; do
    echo "Building ${image}:${VERSION}..."
    ./build-image.sh "${image}:${VERSION}" \
      || echo "  WARNING: build failed for ${image}:${VERSION} — continuing with the remaining images." >&2
done < <(jq -r '.[] | .image' sandbox-images.json)

# Build nodejs with the same tag
./build-image.sh "sandbox-nodejs:${VERSION}"

# Import images into k3s (skipped when k3s is not available)
# Checking the containerd socket avoids needing sudo just for the probe.
if ! command -v k3s >/dev/null 2>&1 || ! [ -S /run/k3s/containerd/containerd.sock ]; then
    echo "  k3s not available — skipping image import. Build complete."
    popd > /dev/null
    exit 0
fi

while IFS=$'\t' read -r image; do
    docker save "${image}:${VERSION}" | sudo k3s ctr images import - \
      || echo "  WARNING: k3s import failed for ${image}:${VERSION} (image may not have built) — continuing." >&2
done < <(jq -r '.[] | .image' sandbox-images.json)

docker save "sandbox-nodejs:${VERSION}" | sudo k3s ctr images import -

popd > /dev/null

# Use the same version in OPENSANDBOX_IMAGES
