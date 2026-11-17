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

# Stamps the runner version into tauri.conf.json and compiles a release runner,
# with the release keys and the signature floor baked in. Run from apps/runner by
# the release workflow (XTM-One-Platform/release, release.yml's runner-build).
#
#   release-compile.sh desktop  <tauri target> <runner version>
#       the GUI binary, `tauri build --no-bundle`: bundle-with-retry.sh then
#       bundles it. RELEASE_SIGNED other than "true": no updater artifacts are
#       asked for, the installers ship unsigned.
#   release-compile.sh headless <tauri target> <runner version>
#       the headless flavor: same source, built without `desktop`, so it
#       links no xcap/enigo and therefore no libxcb / libxkbcommon and starts on
#       a server where the desktop build dies in the loader before main(). Its
#       own --target-dir on purpose - building different features into the same
#       path would clobber the GUI binary the bundling step produced, and the
#       bare archive would then ship the wrong one. Linux only: it exists to
#       unblock servers, and Windows/macOS service installs already run the
#       desktop build fine.
#
# The compile step never has the signing key, which only the bundling and
# staging steps that sign are given. They run on the same machine, so this is
# no boundary against a malicious build script.
# RELEASE_SIGNED says whether those steps will have it.
set -euo pipefail

flavor="$1"
target="$2"
version="$3"

if [ "$flavor" = desktop ]; then
  tmp="$(mktemp)"
  # jq is preinstalled on all GitHub runners (incl. Windows).
  jq --arg v "$version" '.version = $v' tauri.conf.json > "$tmp"
  mv "$tmp" tauri.conf.json
  echo "tauri.conf.json version -> $version"
  if [ "${RELEASE_SIGNED:-}" != true ]; then
    echo "::warning::RUNNER_TAURI_SIGNING_PRIVATE_KEY is not set; building unsigned installers without self-update artifacts"
    tmp="$(mktemp)"
    jq 'del(.bundle.createUpdaterArtifacts)' tauri.conf.json > "$tmp"
    mv "$tmp" tauri.conf.json
  fi
fi

# Bake the release keys and the signature floor into the binary so the
# bare-binary self-updater (headless installs) and `verify-signature` trust
# what the install scripts trust (release-signing.json). The GUI bundle
# updater keeps the one key of tauri.conf.json, which is among them.
XTM_RUNNER_MINISIGN_PUBKEY="$(jq -r '.keys | join(",")' release-signing.json)"
XTM_RUNNER_SIGNATURE_FLOOR="$(jq -r '.timestamp_floor' release-signing.json)"
XTM_RUNNER_VERSION="$version"
export XTM_RUNNER_MINISIGN_PUBKEY XTM_RUNNER_SIGNATURE_FLOOR XTM_RUNNER_VERSION

case "$flavor" in
  desktop)
    tauri build --target "$target" --no-bundle --verbose
    ;;
  headless)
    cargo build --locked --release \
      --target "$target" \
      --no-default-features --features browser,self-update \
      --target-dir target-headless
    ;;
  *)
    echo "::error::flavor must be desktop or headless, got '$flavor'"
    exit 1
    ;;
esac
