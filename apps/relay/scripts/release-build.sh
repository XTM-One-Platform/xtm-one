#!/usr/bin/env bash
# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

# Builds the on-premises relay for one OS and stages its bare archive, under the
# STABLE name backend/app/api/v1/relays.py serves (_RELAY_ARCHIVES, whose tests
# read this script), into relay-out/ with its .sha256 and the VERSION marker.
# release-sign.sh then signs it, in a step of its own: the build step never has
# the signing key (the same machine, so no boundary against what cargo runs).
#
#   release-build.sh <os: windows|macos|linux> <arch: x64|arm64|universal> <rust targets, comma-separated> <relay version>
#
# Run from the repository root by the release workflow (XTM-One-Platform/release,
# release.yml's relay-build).
set -euo pipefail

os_name="$1"
arch="$2"
targets="$3"
version="$4"

# The self-updater trusts what the install scripts trust: the keys and the
# signature floor of release-signing.json, compiled in.
XTM_RELAY_MINISIGN_PUBKEY="$(jq -r '.keys | join(",")' apps/runner/release-signing.json)"
XTM_RELAY_SIGNATURE_FLOOR="$(jq -r '.timestamp_floor' apps/runner/release-signing.json)"
export XTM_RELAY_MINISIGN_PUBKEY XTM_RELAY_SIGNATURE_FLOOR
XTM_RELAY_VERSION="$version"
export XTM_RELAY_VERSION
IFS=, read -r -a target_list <<< "$targets"
for t in "${target_list[@]}"; do
  cargo build --manifest-path apps/relay/Cargo.toml --locked --release --target "$t"
done

# The STABLE names backend/app/api/v1/relays.py serves (_RELAY_ARCHIVES).
out="relay-out"; work="relay-work"; mkdir -p "$out" "$work"
rel() { echo "apps/relay/target/$1/release"; }
case "$os_name" in
  windows)
    archive="$out/xtm-one-relay-windows-$arch.zip"
    7z a -tzip "$archive" "./$(rel "$targets")/xtm-one-relay.exe" >/dev/null
    ;;
  macos)
    lipo -create -output "$work/xtm-one-relay" \
      "$(rel x86_64-apple-darwin)/xtm-one-relay" "$(rel aarch64-apple-darwin)/xtm-one-relay"
    archive="$out/xtm-one-relay-macos-universal.tar.gz"
    tar -C "$work" -czf "$archive" xtm-one-relay
    ;;
  linux)
    archive="$out/xtm-one-relay-linux-$arch.tar.gz"
    tar -C "$(rel "$targets")" -czf "$archive" xtm-one-relay
    # One VERSION for the whole dist, from one leg only.
    if [ "$arch" = "x64" ]; then printf '%s' "$version" > "$out/VERSION"; fi
    ;;
esac
(cd "$out" && if command -v sha256sum >/dev/null 2>&1; then sha256sum "$(basename "$archive")"; else shasum -a 256 "$(basename "$archive")"; fi > "$(basename "$archive").sha256")
ls -1 "$out"
