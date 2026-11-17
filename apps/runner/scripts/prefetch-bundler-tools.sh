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

# Puts every tool the runner bundling would download at bundle time where it is
# looked for, fetched with retries and checked against pinned SHA-256 digests.
# Nothing the bundling runs is downloaded unverified.
#
# tauri-bundler 2.10.0 (in @tauri-apps/cli 2.12.0, the version release.yml of
# XTM-One-Platform/release and runner-ci.yml pin) fetches a missing tool in ONE
# attempt: a single HTTP 500 from GitHub failed the linux/x64 leg of deploy run
# 36593808406, and with it the platform deploy. It skips a tool already in its
# cache, so fetching them here takes the downloads out of the bundle:
#
#   Linux    ~/.cache/tauri/AppRun-<arch>, linuxdeploy-07333c6-<arch>.AppImage,
#            linuxdeploy-plugin-appimage.AppImage, appimage-runtime-<arch>
#   Windows  %LOCALAPPDATA%\tauri\NSIS (the nsis-3.11 zip + nsis_tauri_utils.dll)
#   macOS    nothing: the app and DMG bundlers download nothing
#
# The AppImage plugin: without a file named linuxdeploy-plugin-appimage.AppImage
# in its cache the bundler downloads one from the plugin's moving `continuous`
# release, and when that fails linuxdeploy falls back to an older built-in
# plugin - either way an unverified tool shapes the AppImage we sign. It is
# pinned here to a tagged release instead. That plugin's appimagetool in turn
# downloads the AppImage runtime (the ELF head of every AppImage) from the
# type2-runtime `continuous` release unless LDAI_RUNTIME_FILE names one, so the
# runtime is pinned too and LDAI_RUNTIME_FILE is exported to the later steps
# through $GITHUB_ENV (bundle-with-retry.sh fails a bundle that still
# downloads either, under BUNDLE_PREFETCH_REQUIRED=true).
#
# A digest that no longer matches fails the leg: the asset was replaced
# upstream, and what it becomes is packaged into the signed installers - check
# the release, then update the pin.
#
# Usage: prefetch-bundler-tools.sh [<rust target triple>]   (default: the host's)
set -euo pipefail

# The pins: <tool> <arch> <version / tag> <sha256> <url>, one asset per line.
#
# To update a pin: read the bundler of the new CLI version
# (tauri-bundler's bundle/linux/appimage/linuxdeploy.rs and
# bundle/windows/nsis/mod.rs, at the version tauri-cli pins) for the names,
# URLs and linuxdeploy commit it uses; take a TAGGED release for the plugin and
# the runtime, never `continuous`; take the digest GitHub shows for the asset
# (`gh release view <tag> -R <repo> --json assets`), or `sha256sum` a download
# where GitHub has none, and check it against a second download. The NSIS
# digests must also match the SHA-1 the bundler pins. Where the bundler looks
# for its cached files (the case below) follows the same source.
PINS='
AppRun            x86_64  apprun-old          f30140a43a0a59e46db21bdefdf749b9e9f2c6946e92afabbacf98b8ae73fb4f  https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64
AppRun            aarch64 apprun-old          072f17c0895a85c490282fe5395c5007e5fc75da727e553b3b8fb680feb11578  https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-aarch64
linuxdeploy       x86_64  07333c6             36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62  https://github.com/tauri-apps/binary-releases/releases/download/linuxdeploy-07333c6/linuxdeploy-x86_64.AppImage
linuxdeploy       aarch64 07333c6             556ab80baa98e600aa80f0dcedfb70bca0e1ce7e9f147fb345be3fcc3e91b2b1  https://github.com/tauri-apps/binary-releases/releases/download/linuxdeploy-07333c6/linuxdeploy-aarch64.AppImage
appimage-plugin   x86_64  1-alpha-20250213-1  992d502a248e14ab185448ddf6f6e7d25558cb84d4623c354c3af350c25fccb3  https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/1-alpha-20250213-1/linuxdeploy-plugin-appimage-x86_64.AppImage
appimage-plugin   aarch64 1-alpha-20250213-1  83c292149274965a865dcd44c135cfca8ba28c6b7de3eb628d4b8b5f248af17c  https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/1-alpha-20250213-1/linuxdeploy-plugin-appimage-aarch64.AppImage
appimage-runtime  x86_64  20251108            2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d  https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64
appimage-runtime  aarch64 20251108            00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444  https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-aarch64
nsis              windows 3.11                c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1  https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip
nsis-tauri-utils  windows v0.5.3              5ba143b5db4a87d32d6e7802e033330aae56cbceabe0d1e3ba41948385ad4709  https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll
'
LINUXDEPLOY_COMMIT="07333c6"
NSIS_DIR="nsis-3.11"
NSIS_UTILS_PATH="Plugins/x86-unicode/additional/nsis_tauri_utils.dll"
# The bundler's NSIS_REQUIRED_FILES: one of them missing and it downloads the
# whole toolset again.
NSIS_REQUIRED_FILES=(
  makensis.exe
  Bin/makensis.exe
  Stubs/lzma-x86-unicode
  Stubs/lzma_solid-x86-unicode
  "$NSIS_UTILS_PATH"
  Include/MUI2.nsh
  Include/FileFunc.nsh
  Include/x64.nsh
  Include/nsDialogs.nsh
  Include/WinMessages.nsh
  Include/Win/COM.nsh
  Include/Win/Propkey.nsh
  Include/Win/RestartManager.nsh
)

pin() { # <tool> <arch> -> "<sha256> <url>"
  awk -v tool="$1" -v arch="$2" '$1 == tool && $2 == arch { print $4, $5; found = 1 }
    END { exit !found }' <<< "$PINS" || {
    echo "::error::no pin for $1 ($2) in scripts/prefetch-bundler-tools.sh" >&2
    return 1
  }
}

sha256_of() { # <file>
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d ' ' -f 1
  else
    shasum -a 256 "$1" | cut -d ' ' -f 1
  fi
}

# Downloads <url> to <destination> unless it is already there with <sha256>.
# curl retries the transient answers itself (5xx, 408, 429, timeouts, refused
# connections); the outer loop also covers a connection cut mid-transfer. A 4xx
# means the pinned asset is gone and fails at once.
fetch() { # <url> <sha256> <destination>
  local url="$1" want="$2" dest="$3" part="$3.part" attempt code rc got
  if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$want" ]; then
    echo "Already present: $dest"
    return 0
  fi
  for attempt in 1 2 3; do
    rc=0
    code="$(curl --location --silent --show-error --connect-timeout 30 --max-time 600 \
      --retry 5 --retry-connrefused --write-out '%{http_code}' --output "$part" "$url")" || rc=$?
    if [ "$rc" -eq 0 ] && [ "$code" = "200" ]; then
      break
    fi
    case "$code" in
      408|429) ;;
      4??)
        echo "::error::$url answered HTTP $code: the pinned asset is gone"
        rm -f "$part"
        return 1
        ;;
    esac
    if [ "$attempt" -eq 3 ]; then
      echo "::error::could not download $url (HTTP ${code:-none}, curl exit $rc) after 3 attempts"
      rm -f "$part"
      return 1
    fi
    echo "::warning::downloading $url failed (HTTP ${code:-none}, curl exit $rc), attempt $attempt of 3"
    sleep $((attempt * 30))
  done
  got="$(sha256_of "$part")"
  if [ "$got" != "$want" ]; then
    echo "::error::$url has SHA-256 $got, pinned $want: the asset changed upstream - check the release, then update the pin in scripts/prefetch-bundler-tools.sh"
    rm -f "$part"
    return 1
  fi
  mv -f "$part" "$dest"
  echo "Fetched $url -> $dest"
}

fetch_pinned() { # <tool> <arch> <destination>
  local sha url
  read -r sha url <<< "$(pin "$1" "$2")"
  fetch "$url" "$sha" "$3"
}

prefetch_appimage_tools() { # <arch>
  local arch="$1" tools runtime
  tools="${XDG_CACHE_HOME:-$HOME/.cache}/tauri"
  runtime="$tools/appimage-runtime-$arch"
  mkdir -p "$tools"
  fetch_pinned AppRun "$arch" "$tools/AppRun-$arch"
  fetch_pinned linuxdeploy "$arch" "$tools/linuxdeploy-$LINUXDEPLOY_COMMIT-$arch.AppImage"
  # One arch-less name, as the bundler looks it up: a cache shared by two
  # architectures would hand one leg the other's plugin.
  fetch_pinned appimage-plugin "$arch" "$tools/linuxdeploy-plugin-appimage.AppImage"
  fetch_pinned appimage-runtime "$arch" "$runtime"
  chmod 755 "$tools/AppRun-$arch" "$tools/linuxdeploy-$LINUXDEPLOY_COMMIT-$arch.AppImage" \
    "$tools/linuxdeploy-plugin-appimage.AppImage"
  if [ -n "${GITHUB_ENV:-}" ]; then
    echo "LDAI_RUNTIME_FILE=$runtime" >> "$GITHUB_ENV"
    echo "LDAI_RUNTIME_FILE=$runtime exported to the next steps"
  else
    echo "Bundle with LDAI_RUNTIME_FILE=$runtime, or appimagetool downloads its runtime"
  fi
}

prefetch_nsis() {
  local tools nsis dll work file missing=()
  tools="$(cygpath -u "$LOCALAPPDATA")/tauri"
  nsis="$tools/NSIS"
  dll="$nsis/$NSIS_UTILS_PATH"
  for file in "${NSIS_REQUIRED_FILES[@]}"; do
    [ -e "$nsis/$file" ] || missing+=("$file")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    echo "Already present: $nsis"
  else
    work="$(mktemp -d)"
    fetch_pinned nsis windows "$work/nsis.zip"
    rm -rf "$nsis" "${tools:?}/$NSIS_DIR"
    mkdir -p "$tools"
    7z x -y "-o$(cygpath -w "$tools")" "$(cygpath -w "$work/nsis.zip")" >/dev/null
    mv "$tools/$NSIS_DIR" "$nsis"
    rm -rf "$work"
  fi
  mkdir -p "$(dirname "$dll")"
  fetch_pinned nsis-tauri-utils windows "$dll"
  missing=()
  for file in "${NSIS_REQUIRED_FILES[@]}"; do
    [ -e "$nsis/$file" ] || missing+=("$file")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "::error::the NSIS toolset in $nsis lacks ${missing[*]}: the bundler would download it again"
    return 1
  fi
}

target="${1:-$(rustc -vV | sed -n 's/^host: //p')}"
case "$target" in
  x86_64-unknown-linux-gnu) prefetch_appimage_tools x86_64 ;;
  aarch64-unknown-linux-gnu) prefetch_appimage_tools aarch64 ;;
  *-pc-windows-msvc) prefetch_nsis ;;
  *-apple-darwin) echo "$target: the macOS bundlers download nothing" ;;
  *)
    echo "::error::no bundler tool pins for target '$target'"
    exit 1
    ;;
esac
