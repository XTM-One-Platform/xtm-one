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

# Bundles the runner `tauri build --no-bundle` compiled, and retries a failed
# attempt only when its output carries a known TRANSIENT signature:
#
#   tool-download  tauri-bundler fetches a missing tool (AppImage, NSIS) in ONE
#                  attempt and reports a failed download as
#                  failed to bundle project: `<ureq error>` - an HTTP 5xx, 408 or
#                  429, or a connection that failed, timed out or was cut. Deploy
#                  run 36593808406: `http status: 500` on AppRun-x86_64.
#   hdiutil-busy   the DMG step of the GitHub macOS images: `hdiutil: create
#                  failed - Resource busy`, then `error running bundle_dmg.sh`.
#                  Deploy run 36590064043.
#
# Anything else - signing, notarization, a packaging error, a download answered
# 404 or with the wrong hash - fails on the first attempt: retrying a
# deterministic failure only delays the red.
#
# Each retry starts from the binary the compile produced. The bundler patches
# the binary in place with each bundle type (__TAURI_BUNDLE_TYPE_VAR_*) and puts
# it back only once that type succeeded: over a failed attempt, a second one
# would ship a .deb / .rpm marked as an AppImage, and cargo, whose
# output looks up to date, never rebuilds it.
#
# Usage (from apps/runner): bundle-with-retry.sh --bundles <list> [--target <triple>] [--debug]
# BUNDLE_ATTEMPTS (3), BUNDLE_RETRY_DELAYS ("30 90": seconds before attempt 2,
# 3, ...) and TAURI_BIN (tauri) are read from the environment.
# BUNDLE_PREFETCH_REQUIRED=true fails a bundle during which the bundling still
# downloaded, in any attempt, a tool prefetch-bundler-tools.sh should have
# provided.
set -euo pipefail

BINARY_NAME="xtm-one-runner"

strip_ansi() { # <file> -> stdout
  sed "s/"$'\033'"\[[0-9;]*[A-Za-z]//g" "$1"
}

# Prints the transient class of a failed attempt's output, or nothing.
classify_bundle_failure() { # <log file>
  local text
  text="$(mktemp)"
  strip_ansi "$1" > "$text"
  if grep -q 'Downloading \[tauri_bundler::utils::http_utils\]' "$text" \
    && grep -Eq 'failed to bundle project: `(http status: (5[0-9][0-9]|408|429)|io: |timeout: |host not found|connection failed|body data reading stalled)' "$text"; then
    echo "tool-download"
  elif grep -Eq 'hdiutil: [a-z]+ failed - Resource busy' "$text" \
    && grep -q 'error running bundle_dmg.sh' "$text"; then
    echo "hdiutil-busy"
  fi
  rm -f "$text"
}

# The line that names the failure, for the annotations.
failure_line() { # <log file>
  local text
  text="$(mktemp)"
  strip_ansi "$1" > "$text"
  { grep -E -m 1 'hdiutil: [a-z]+ failed - ' "$text" \
    || grep -m 1 'failed to bundle project' "$text" \
    || grep -E 'error(\[E[0-9]+\])?: ' "$text" | tail -n 1 \
    || true; } | sed 's/^[[:space:]]*//'
  rm -f "$text"
}

# What the bundling downloaded: the bundler's own downloads, and the AppImage
# runtime appimagetool fetches when LDAI_RUNTIME_FILE names none (its stderr,
# relayed by linuxdeploy into the bundler's --verbose log).
downloaded_urls() { # <log file>
  strip_ansi "$1" | sed -n \
    -e 's/.*Downloading \[tauri_bundler::utils::http_utils\] \([^ ]*\).*/\1/p' \
    -e 's/.*Downloading runtime file from \([^ ]*\).*/\1/p'
}

# Reports the tools the bundling downloaded itself over EVERY attempt: an
# attempt that fetched one and then failed left it in the bundler's cache, so
# the attempt after it downloads nothing and its own log says nothing.
# A warning, or with BUNDLE_PREFETCH_REQUIRED=true an error and a non-zero
# return.
report_unprefetched_downloads() { # <log file>...
  local log url stale=""
  while read -r url; do
    if [ -n "$url" ]; then
      stale="$stale $url"
    fi
  done <<< "$(for log in "$@"; do downloaded_urls "$log"; done | awk '!seen[$0]++')"
  [ -n "$stale" ] || return 0
  stale="the bundling downloaded tools itself:$stale - scripts/prefetch-bundler-tools.sh no longer matches this CLI version, update its pins"
  if [ "${BUNDLE_PREFETCH_REQUIRED:-}" = "true" ]; then
    echo "::error::$stale"
    return 1
  fi
  echo "::warning::$stale"
}

# A failed DMG attempt can leave its read-write image attached; the next
# `hdiutil create` would then meet it. Only images built under our bundle
# directory are detached - the macOS images keep simulator runtimes attached.
detach_leftover_images() { # <absolute bundle dir>
  command -v hdiutil >/dev/null 2>&1 || return 0
  local dev
  hdiutil info 2>/dev/null | awk -v dir="$1/" '
    /^image-path/ { keep = index($0, dir) > 0 }
    keep && /^\/dev\/disk[0-9]+[ \t]/ { print $1; keep = 0 }' |
    while read -r dev; do
      echo "Detaching $dev, left attached by the failed attempt"
      hdiutil detach -force "$dev" || true
    done || true
}

main() {
  local bundles="" target="" profile="release"
  local args=(bundle --verbose)
  while [ $# -gt 0 ]; do
    case "$1" in
      --bundles) bundles="$2"; shift 2 ;;
      --target) target="$2"; args+=(--target "$2"); shift 2 ;;
      --debug) profile="debug"; args+=(--debug); shift ;;
      *) echo "::error::bundle-with-retry.sh: unknown argument '$1'"; return 2 ;;
    esac
  done
  if [ -z "$bundles" ]; then
    echo "::error::bundle-with-retry.sh: --bundles is required"
    return 2
  fi
  # `tauri build` bundles with the features it compiled with; the bundler
  # settings read them, so the bundle-only run is handed the same.
  args+=(--bundles "$bundles" --features tauri/custom-protocol)

  local tauri="${TAURI_BIN:-tauri}"
  local attempts="${BUNDLE_ATTEMPTS:-3}"
  local delays
  read -r -a delays <<< "${BUNDLE_RETRY_DELAYS:-30 90}"

  local out_dir="target/${target:+$target/}$profile"
  local binary="$out_dir/$BINARY_NAME"
  [ -f "$binary.exe" ] && binary="$binary.exe"
  if [ ! -f "$binary" ]; then
    echo "::error::no compiled runner at $binary: run tauri build --no-bundle first"
    return 1
  fi
  local bundle_dir
  bundle_dir="$(cd "$out_dir" && pwd)/bundle"

  local work
  work="$(mktemp -d)"
  # shellcheck disable=SC2064 # expand now: $work is local to main
  trap "rm -rf '$work'" EXIT
  cp -p "$binary" "$work/pristine"

  local attempt=1 rc log class classes="" line delay index
  local logs=()
  while :; do
    if [ "$attempt" -gt 1 ]; then
      cp -p "$work/pristine" "$binary"
      detach_leftover_images "$bundle_dir"
      rm -rf "$bundle_dir"
    fi
    log="$work/attempt-$attempt.log"
    logs+=("$log")
    echo "::group::tauri ${args[*]} (attempt $attempt of $attempts)"
    rc=0
    "$tauri" "${args[@]}" 2>&1 | tee "$log" || rc=$?
    echo "::endgroup::"

    if [ "$rc" -eq 0 ]; then
      report_unprefetched_downloads "${logs[@]}" || return 1
      [ "$attempt" -gt 1 ] && echo "Bundled on attempt $attempt of $attempts"
      return 0
    fi

    class="$(classify_bundle_failure "$log")"
    line="$(failure_line "$log")"
    if [ -z "$class" ]; then
      echo "::error::bundling failed (exit $rc) and not on a known transient signature, so it is not retried: ${line:-see the log above}"
      report_unprefetched_downloads "${logs[@]}" || true
      return "$rc"
    fi
    classes="${classes:+$classes, }$class"
    if [ "$attempt" -ge "$attempts" ]; then
      echo "::error::bundling failed on a transient error on each of $attempts attempts ($classes), the last one: $line"
      report_unprefetched_downloads "${logs[@]}" || true
      return "$rc"
    fi
    # The last delay repeats when BUNDLE_ATTEMPTS outnumbers the delays.
    delay=0
    if [ "${#delays[@]}" -gt 0 ]; then
      index=$((attempt - 1))
      [ "$index" -lt "${#delays[@]}" ] || index=$((${#delays[@]} - 1))
      delay="${delays[$index]}"
    fi
    echo "::warning::bundling attempt $attempt of $attempts failed on a transient $class error ($line); retrying in ${delay}s from the compiled binary"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
