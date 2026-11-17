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

# Stages what one runner build ships, under the STABLE names the backend serves
# from /api/v1/runners/dist/ (kept in lock-step with backend/app/api/v1/runners.py,
# whose tests read this script), into apps/runner/dist-out/: installers, the
# Tauri updater artifacts and their signatures, the bare-binary archives with
# their .sha256 and .minisig, and the VERSION marker.
#
#   release-stage.sh <os: windows|macos|linux> <arch: x64|arm64|universal> <tauri target> <runner version>
#
# Run from apps/runner by the release workflow (XTM-One-Platform/release,
# release.yml's runner-build), after release-compile.sh and bundle-with-retry.sh.
# TAURI_SIGNING_PRIVATE_KEY (+ _PASSWORD) set: the archives are signed and every
# signature is checked with the runner this build compiled.
set -euo pipefail

os_name="$1"
arch="$2"
target="$3"
version="$4"
b="target/$target/release/bundle"
bin_dir="target/$target/release"
# Every stable name is "<os>-<arch>", with macOS's single universal
# binary carrying arch=universal. Kept byte-identical to the tables in
# backend/app/api/v1/runners.py - a rename here 404s the downloads.
out="dist-out"; mkdir -p "$out"
signed=1; [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ] && signed=0
# The runner this leg built, which checks every signature it stages.
case "$os_name" in
  windows) verifier="$bin_dir/xtm-one-runner.exe" ;;
  linux)   verifier="target-headless/$target/release/xtm-one-runner" ;;
  *)       verifier="$bin_dir/xtm-one-runner" ;;
esac

sha256_of() { # <file> -> sibling <file>.sha256 (bare-hex + name form)
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$(dirname "$1")" && sha256sum "$(basename "$1")" > "$(basename "$1").sha256")
  else
    (cd "$(dirname "$1")" && shasum -a 256 "$(basename "$1")" > "$(basename "$1").sha256")
  fi
}
minisig_of() { # <file> -> sibling <file>.minisig: minisign's OWN text, never Tauri's wrapper
  # `tauri signer sign` writes <file>.sig as BASE64 OF the minisign
  # signature text - the form latest.json carries and the Tauri bundle
  # updater decodes itself before verifying. The bare-binary
  # self-updater (apps/runner/src/update.rs) hands <file>.minisig to
  # minisign_verify VERBATIM, so it needs the raw four-line text.
  # Renaming .sig to .minisig shipped the wrapped form under the raw
  # name in every older release: the update was found, the
  # download and the checksum passed, and every headless / service
  # runner failed signature verification every 6 hours, forever.
  # The trusted comment also names the runner version, which
  # tauri signer cannot write: sign-release-archive.mjs signs the way
  # it does, with the same key, and writes minisign's text itself.
  node scripts/sign-release-archive.mjs "$1" "$version"
  assert_raw_minisig "$1.minisig"
  # What the fleet would refuse fails the BUILD: the runner checks it
  # with its compiled-in keys, for this archive and this version.
  # Without the key in its environment (every crate linked into it runs
  # here; it can still read the parent's).
  env -u TAURI_SIGNING_PRIVATE_KEY -u TAURI_SIGNING_PRIVATE_KEY_PASSWORD \
    "$verifier" verify-signature --signature "$1.minisig" \
    --expect-file "$(basename "$1")" --expect-version "$version" "$1"
}
assert_raw_minisig() { # a .minisig the runner cannot parse fails the BUILD, not the fleet
  # minisign text is exactly four lines: untrusted comment, signature,
  # trusted comment, global signature. A single base64 line (the Tauri
  # form) or an empty decode is refused here.
  if [ "$(grep -c '' "$1")" -ne 4 ] \
    || ! sed -n '1p' "$1" | grep -q '^untrusted comment: ' \
    || ! sed -n '3p' "$1" | grep -q '^trusted comment: '; then
    echo "::error::$1 is not a raw minisign signature (expected the four-line minisign text, got: $(head -c 120 "$1" | tr '\n' '|'))"
    exit 1
  fi
}

case "$os_name" in
  windows)
    cp "$b"/nsis/*-setup.exe "$out/xtm-one-runner-windows-$arch-setup.exe"
    [ "$signed" = 1 ] && cp "$b"/nsis/*-setup.exe.sig "$out/xtm-one-runner-windows-$arch-setup.exe.sig"
    archive="$out/xtm-one-runner-windows-$arch.zip"
    7z a -tzip "$archive" "./$bin_dir/xtm-one-runner.exe" >/dev/null
    ;;
  macos)
    cp "$b"/dmg/*.dmg "$out/xtm-one-runner-macos-$arch.dmg"
    if [ "$signed" = 1 ]; then
      cp "$b"/macos/*.app.tar.gz "$out/xtm-one-runner-macos-$arch.app.tar.gz"
      cp "$b"/macos/*.app.tar.gz.sig "$out/xtm-one-runner-macos-$arch.app.tar.gz.sig"
    fi
    archive="$out/xtm-one-runner-macos-$arch.tar.gz"
    tar -C "$bin_dir" -czf "$archive" xtm-one-runner
    ;;
  linux)
    cp "$b"/appimage/*.AppImage "$out/xtm-one-runner-linux-$arch.AppImage"
    # Tauri v2 form (createUpdaterArtifacts: true): the Linux updater
    # artifact IS the AppImage itself with a detached .AppImage.sig.
    # The v1-era .AppImage.tar.gz pair is only produced by the
    # "v1Compatible" mode and never exists here - staging it killed
    # the first signed deploys.
    [ "$signed" = 1 ] && cp "$b"/appimage/*.AppImage.sig "$out/xtm-one-runner-linux-$arch.AppImage.sig"
    cp "$b"/deb/*.deb "$out/xtm-one-runner-linux-$arch.deb"
    cp "$b"/rpm/*.rpm "$out/xtm-one-runner-linux-$arch.rpm"
    # A .deb / .rpm install updates from its OWN package: the Tauri
    # updater looks up linux-<arch>-deb / -rpm before linux-<arch>
    # and refuses any other format, so without these the package
    # installs never update. `tauri build` signs both next to
    # the package whenever it signs the AppImage; a missing one fails
    # the leg here rather than shipping a fleet that silently stalls.
    [ "$signed" = 1 ] && cp "$b"/deb/*.deb.sig "$out/xtm-one-runner-linux-$arch.deb.sig"
    [ "$signed" = 1 ] && cp "$b"/rpm/*.rpm.sig "$out/xtm-one-runner-linux-$arch.rpm.sig"
    archive="$out/xtm-one-runner-linux-$arch.tar.gz"
    tar -C "$bin_dir" -czf "$archive" xtm-one-runner
    # The headless flavor ships as its own bare archive: the install
    # script and the self-updater pick between the two by flavor, and
    # a headless host must never be handed the desktop binary.
    headless="$out/xtm-one-runner-linux-$arch-headless.tar.gz"
    tar -C "target-headless/$target/release" \
      -czf "$headless" xtm-one-runner
    # One VERSION marker for the whole dist, written by exactly ONE
    # leg (linux/x64) so the download-artifact merge can never
    # collide on it - every leg builds the same version anyway.
    if [ "$arch" = "x64" ]; then
      printf '%s' "$version" > "$out/VERSION"
    fi
    ;;
esac

# Integrity + authenticity for every bare archive this leg produced
# (the desktop one, plus the headless one on Linux): the install
# script verifies the .sha256; the daemon self-updater requires BOTH
# and refuses to apply anything unsigned.
for a in "$archive" ${headless:+"$headless"}; do
  sha256_of "$a"
  if [ "$signed" = 1 ]; then minisig_of "$a"; fi
done
ls -1 "$out"
