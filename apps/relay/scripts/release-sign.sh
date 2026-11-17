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

# Signs the relay archive release-build.sh staged into relay-out/ with the
# runner's release key (sign-release-archive.mjs: the archive name and the relay
# version in the trusted comment), next to it as <archive>.minisig.
#
#   release-sign.sh <relay version>
#
# Run from the repository root by the release workflow (XTM-One-Platform/release,
# release.yml's relay-build), in a step of its own after the build: only this
# step gets TAURI_SIGNING_PRIVATE_KEY (+ _PASSWORD). Unset, the archive ships
# unsigned and relays refuse to update to it.
set -euo pipefail

version="$1"
out="relay-out"

if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "::warning::RUNNER_TAURI_SIGNING_PRIVATE_KEY is not set; the relay archives ship unsigned and relays will not update to them"
  exit 0
fi

shopt -s nullglob
archives=("$out"/xtm-one-relay-*.zip "$out"/xtm-one-relay-*.tar.gz)
if [ "${#archives[@]}" -eq 0 ]; then
  echo "::error::no relay archive in $out/ to sign: run release-build.sh first"
  exit 1
fi
for archive in "${archives[@]}"; do
  node apps/runner/scripts/sign-release-archive.mjs "$archive" "$version"
  if [ "$(grep -c '' "$archive.minisig")" -ne 4 ] || ! sed -n '3p' "$archive.minisig" | grep -q "file:$(basename "$archive")"; then
    echo "::error::$archive.minisig is not a minisign signature for $(basename "$archive")"
    exit 1
  fi
  echo "Signed $archive"
done
