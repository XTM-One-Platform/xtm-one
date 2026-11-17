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

# The retry rules of bundle-with-retry.sh: which bundling failures are
# retried and which fail at once, and that a retry starts from the compiled
# binary. The two transient fixtures are the verbatim output of the deploy legs
# they come from; no network, no Rust, no Tauri CLI.
#
# Usage: bash scripts/test-bundle-with-retry.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bundle-with-retry.sh
. "$here/bundle-with-retry.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0

fixture() { # <name>, text on stdin
  cat > "$work/$1.log"
}

# Deploy run 36593808406, Runner (linux/x64): GitHub answered 500 to the AppRun
# download, after an 8-minute compile.
fixture linux-apprun-500 <<'EOF'
       Built [tauri_cli::build] application at: /home/runner/work/xtm-one/xtm-one/apps/runner/target/x86_64-unknown-linux-gnu/release/xtm-one-runner
        Info [tauri_bundler::bundle] Patching /home/runner/work/xtm-one/xtm-one/apps/runner/target/x86_64-unknown-linux-gnu/release/xtm-one-runner with bundle type information: appimage
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64
       Debug [ureq::run] GET https://github.com/******
       Debug [ureq::unversioned::transport::tcp] Connected TcpStream to 140.82.112.4:443
       Debug [ureq::run] Request { method: GET, uri: https://github.com/******, version: HTTP/1.1, headers: {"accept-encoding": "gzip", "user-agent": "tauri-bundler/2.10.0", "accept": "*/*", "host": "github.com"} }
       Debug [ureq::run] Response { status: 500, version: HTTP/1.1, headers: {"date": "Tue, 29 Sep 2026 16:03:43 GMT", "content-type": "text/html; charset=utf-8", "server": "github.com", "content-length": "160949", "<NOTICE>": "10 HEADERS ARE REDACTED"} }
failed to bundle project: `http status: 500`
       Error [tauri_cli_node] failed to bundle project: `http status: 500`
EOF

# Deploy run 36590064043, Runner (macos/universal).
fixture macos-hdiutil-busy <<'EOF'
       Built [tauri_cli::build] application at: /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/xtm-one-runner
    Bundling [tauri_bundler::bundle::macos::app] XTM One Desktop.app (/Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/macos/XTM One Desktop.app)
    Bundling [tauri_bundler::bundle::macos::dmg] XTM One Desktop_1.260929.2559_universal.dmg (/Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/XTM One Desktop_1.260929.2559_universal.dmg)
     Running [tauri_bundler::bundle::macos::dmg] bundle_dmg.sh
     Running [tauri_bundler::utils] Command `/Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/bundle_dmg.sh  --volname XTM One Desktop --icon XTM One Desktop.app 180 170 --app-drop-link 480 170 --window-size 660 400 --hide-extension XTM One Desktop.app --volicon /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/icon.icns --skip-jenkins XTM One Desktop_1.260929.2559_universal.dmg XTM One Desktop.app`
Creating disk image...
hdiutil: create failed - Resource busy
failed to bundle project: error running bundle_dmg.sh: `failed to run /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/bundle_dmg.sh`
       Error [tauri_cli_node] failed to bundle project: error running bundle_dmg.sh: `failed to run /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/bundle_dmg.sh`
EOF

# The other transient shapes of the same two causes.
fixture linuxdeploy-reset <<'EOF'
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/linuxdeploy-07333c6/linuxdeploy-x86_64.AppImage
failed to bundle project: `io: Connection reset by peer (os error 104)`
       Error [tauri_cli_node] failed to bundle project: `io: Connection reset by peer (os error 104)`
EOF
fixture nsis-timeout <<'EOF'
        Info [tauri_bundler::bundle::windows::nsis] Verifying NSIS package
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip
failed to bundle project: `timeout: global`
       Error [tauri_cli_node] failed to bundle project: `timeout: global`
EOF
fixture apprun-503-colored <<EOF
 $(printf '\033[1;32m')Downloading$(printf '\033[0m') [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-aarch64
$(printf '\033[31m')failed to bundle project: \`http status: 503\`$(printf '\033[0m')
EOF
fixture dmg-detach-busy <<'EOF'
Unmounting disk image...
hdiutil: detach failed - Resource busy
failed to bundle project: error running bundle_dmg.sh: `failed to run /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/bundle_dmg.sh`
EOF

# Genuine failures: retrying cannot change their outcome.
fixture compile-error <<'EOF'
   Compiling xtm-one-runner v0.0.0-dev (/home/runner/work/xtm-one/xtm-one/apps/runner)
error[E0425]: cannot find value `runner_id` in this scope
   --> src/gui.rs:412:9
    |
412 |         runner_id,
    |         ^^^^^^^^^ not found in this scope

error: could not compile `xtm-one-runner` (bin "xtm-one-runner") due to 1 previous error
       Error [tauri_cli_node] failed to build app
EOF
fixture asset-gone-404 <<'EOF'
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64
failed to bundle project: `http status: 404`
EOF
fixture nsis-hash-mismatch <<'EOF'
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip
        Info [tauri_bundler::utils::http_utils] validating hash
failed to bundle project: hash mismatch of downloaded file
EOF
fixture windows-signing <<'EOF'
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip
        Info [tauri_bundler::utils::http_utils] validating hash
     Running [tauri_bundler::bundle::windows::sign] signtool.exe sign /sha1 0123456789ABCDEF /fd sha256 /tr https://timestamp.sectigo.com /td sha256
SignTool Error: No certificates were found that met all the given criteria.
failed to bundle project: `failed to run signtool.exe`
EOF
fixture macos-notarization <<'EOF'
    Signing [tauri_macos_sign] with identity "Developer ID Application: Filigran (ABCDE12345)"
  Notarizing [tauri_macos_sign] /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/macos/XTM One Desktop.app
failed to bundle project: `failed to notarize app: Error: HTTP status code: 401. Invalid credentials.`
EOF
fixture dmg-no-space <<'EOF'
Creating disk image...
hdiutil: create failed - No space left on device
failed to bundle project: error running bundle_dmg.sh: `failed to run /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/bundle_dmg.sh`
EOF
fixture status-500-no-download <<'EOF'
failed to bundle project: `http status: 500`
EOF

# An attempt that downloaded a tool the prefetch missed, then failed on a
# transient error: the tool stays in the bundler's cache, so the attempt after
# it downloads nothing. A tool a later CLI could fetch on macOS, then
# the busy disk image of the same leg:
fixture drift-then-hdiutil-busy <<'EOF'
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/example-tool/example-tool-universal
     Running [tauri_bundler::bundle::macos::dmg] bundle_dmg.sh
Creating disk image...
hdiutil: create failed - Resource busy
failed to bundle project: error running bundle_dmg.sh: `failed to run /Users/runner/work/xtm-one/xtm-one/apps/runner/target/universal-apple-darwin/release/bundle/dmg/bundle_dmg.sh`
EOF
# Two AppImage tools the prefetch missed: the first fetched, the second answered 502.
fixture apprun-fetched-linuxdeploy-502 <<'EOF'
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64
 Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/linuxdeploy-07333c6/linuxdeploy-x86_64.AppImage
failed to bundle project: `http status: 502`
EOF

expect_class() { # <fixture> <expected class, "" = not retried>
  local got
  got="$(classify_bundle_failure "$work/$1.log")"
  if [ "$got" = "$2" ]; then
    printf 'ok    %-24s %s\n' "$1" "${got:-not retried}"
  else
    printf 'FAIL  %-24s expected "%s", got "%s"\n' "$1" "${2:-not retried}" "${got:-not retried}"
    failures=$((failures + 1))
  fi
}

echo "== classification"
expect_class linux-apprun-500 tool-download
expect_class macos-hdiutil-busy hdiutil-busy
expect_class linuxdeploy-reset tool-download
expect_class nsis-timeout tool-download
expect_class apprun-503-colored tool-download
expect_class dmg-detach-busy hdiutil-busy
expect_class compile-error ""
expect_class asset-gone-404 ""
expect_class nsis-hash-mismatch ""
expect_class windows-signing ""
expect_class macos-notarization ""
expect_class dmg-no-space ""
expect_class status-500-no-download ""

# The retry loop, end to end, against a stand-in for the Tauri CLI. The
# stand-in patches the binary like the bundler does and refuses to start from a
# patched one, so a retry that did not restore the compiled binary fails.
project="$work/project"
mkdir -p "$project/target/release"
cat > "$work/fake-tauri" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
binary=target/release/xtm-one-runner
if ! grep -q '__TAURI_BUNDLE_TYPE_VAR_UNK' "$binary"; then
  echo "fake-tauri: started from a patched binary"
  exit 3
fi
printf 'compiled __TAURI_BUNDLE_TYPE_VAR_APP\n' > "$binary"
mkdir -p target/release/bundle/appimage
touch target/release/bundle/appimage/leftover
n=$(( $(cat "$FAKE_STATE/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_STATE/count"
step="$(sed -n "${n}p" "$FAKE_STATE/plan")"
case "$step" in
  ok) echo "Finished 1 bundle"; exit 0 ;;
  ok:*) cat "$FAKE_STATE/${step#ok:}.log"; echo "Finished 1 bundle"; exit 0 ;;
  *) cat "$FAKE_STATE/$step.log"; exit 1 ;;
esac
EOF
chmod +x "$work/fake-tauri"
printf ' Downloading [tauri_bundler::utils::http_utils] https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/continuous/linuxdeploy-plugin-appimage-x86_64.AppImage\n' > "$work/plugin-download.log"
# appimagetool fetching its runtime when LDAI_RUNTIME_FILE names none, verbatim
# from the bundler's --verbose log (linuxdeploy relays the plugin's stderr).
printf '[appimage/stderr] Downloading runtime file from https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64\n' > "$work/runtime-download.log"
printf ' Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64\n' > "$work/stale-download.log"
printf ' Downloading [tauri_bundler::utils::http_utils] https://github.com/tauri-apps/binary-releases/releases/download/linuxdeploy-07333c6/linuxdeploy-x86_64.AppImage\n' > "$work/linuxdeploy-download.log"

expect_run() { # <case> <expected exit> <expected attempts> <plan...>
  local name="$1" want_rc="$2" want_attempts="$3" rc=0 attempts
  shift 3
  printf '%s\n' "$@" > "$work/plan"
  rm -f "$work/count" "$work/run.out"
  printf 'compiled __TAURI_BUNDLE_TYPE_VAR_UNK\n' > "$project/target/release/xtm-one-runner"
  (cd "$project" && FAKE_STATE="$work" TAURI_BIN="$work/fake-tauri" BUNDLE_RETRY_DELAYS="0 0" \
    BUNDLE_PREFETCH_REQUIRED="${PREFETCH_REQUIRED:-}" \
    bash "$here/bundle-with-retry.sh" --bundles appimage) > "$work/run.out" 2>&1 || rc=$?
  attempts="$(cat "$work/count" 2>/dev/null || echo 0)"
  if { [ "$want_rc" = 0 ] && [ "$rc" -eq 0 ]; } || { [ "$want_rc" != 0 ] && [ "$rc" -ne 0 ]; }; then
    if [ "$attempts" -eq "$want_attempts" ]; then
      printf 'ok    %-34s exit %s after %s attempt(s)\n' "$name" "$rc" "$attempts"
      return 0
    fi
  fi
  printf 'FAIL  %-34s exit %s after %s attempt(s), expected exit %s after %s\n' \
    "$name" "$rc" "$attempts" "$want_rc" "$want_attempts"
  sed 's/^/      | /' "$work/run.out"
  failures=$((failures + 1))
}

expect_output() { # <case> <pattern that must appear in the last run's output>
  if grep -q -- "$2" "$work/run.out"; then
    printf 'ok    %-34s says "%s"\n' "$1" "$2"
  else
    printf 'FAIL  %-34s does not say "%s"\n' "$1" "$2"
    sed 's/^/      | /' "$work/run.out"
    failures=$((failures + 1))
  fi
}

expect_no_output() { # <case> <pattern that must not appear in the last run's output>
  if grep -q -- "$2" "$work/run.out"; then
    printf 'FAIL  %-34s says "%s"\n' "$1" "$2"
    sed 's/^/      | /' "$work/run.out"
    failures=$((failures + 1))
  else
    printf 'ok    %-34s does not say "%s"\n' "$1" "$2"
  fi
}

echo "== retry loop"
expect_run "clean bundle" 0 1 ok
expect_run "download 500, then bundled" 0 2 linux-apprun-500 ok
expect_output "download 500, then bundled" "attempt 1 of 3 failed on a transient tool-download error"
expect_run "hdiutil busy twice, then bundled" 0 3 macos-hdiutil-busy macos-hdiutil-busy ok
expect_run "download 500 on every attempt" 1 3 linux-apprun-500 linux-apprun-500 linux-apprun-500
expect_output "download 500 on every attempt" "on each of 3 attempts"
expect_run "signing failure" 1 1 windows-signing ok
expect_output "signing failure" "not on a known transient signature"
expect_run "asset gone (404)" 1 1 asset-gone-404 ok
expect_run "transient, then genuine" 1 2 nsis-timeout nsis-hash-mismatch ok
expect_run "tool the prefetch missed" 0 1 ok:stale-download
expect_output "tool the prefetch missed" "downloaded tools itself: https://github.com/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-x86_64"
PREFETCH_REQUIRED=true expect_run "tool missed, prefetch required" 1 1 ok:stale-download
# The AppImage plugin and its runtime are pinned like every other tool.
expect_run "AppImage plugin download" 0 1 ok:plugin-download
expect_output "AppImage plugin download" "downloaded tools itself: https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/continuous/"
PREFETCH_REQUIRED=true expect_run "plugin, prefetch required" 1 1 ok:plugin-download
expect_run "AppImage runtime download" 0 1 ok:runtime-download
expect_output "AppImage runtime download" "downloaded tools itself: https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64 - "
PREFETCH_REQUIRED=true expect_run "runtime, prefetch required" 1 1 ok:runtime-download

echo "== downloads of every attempt"
releases="https://github.com/tauri-apps/binary-releases/releases/download"
expect_run "tool fetched, disk busy, bundled" 0 2 drift-then-hdiutil-busy ok
expect_output "tool fetched, disk busy, bundled" "downloaded tools itself: $releases/example-tool/example-tool-universal - "
PREFETCH_REQUIRED=true expect_run "same, prefetch required" 1 2 drift-then-hdiutil-busy ok
expect_output "same, prefetch required" "::error::the bundling downloaded tools itself: $releases/example-tool/example-tool-universal"
expect_run "one tool fetched, one 502, bundled" 0 2 apprun-fetched-linuxdeploy-502 ok:linuxdeploy-download
expect_output "one tool fetched, one 502, bundled" "itself: $releases/apprun-old/AppRun-x86_64 $releases/linuxdeploy-07333c6/linuxdeploy-x86_64.AppImage - "
expect_run "tool fetched, disk busy, notarization" 1 2 drift-then-hdiutil-busy macos-notarization
expect_output "tool fetched, disk busy, notarization" "downloaded tools itself: $releases/example-tool/example-tool-universal - "
expect_run "download 500 on every attempt" 1 3 linux-apprun-500 linux-apprun-500 linux-apprun-500
expect_output "download 500 on every attempt" "downloaded tools itself: $releases/apprun-old/AppRun-x86_64 - "
expect_no_output "download 500 on every attempt" "AppRun-x86_64 https"
expect_run "disk busy, then download 500 twice" 1 3 macos-hdiutil-busy linux-apprun-500 linux-apprun-500
expect_output "disk busy, then download 500 twice" "on each of 3 attempts (hdiutil-busy, tool-download, tool-download)"

missing_rc=0
(cd "$work" && bash "$here/bundle-with-retry.sh" --bundles appimage) > "$work/run.out" 2>&1 || missing_rc=$?
if [ "$missing_rc" -ne 0 ] && grep -q "no compiled runner" "$work/run.out"; then
  echo "ok    no compiled binary                 refused before bundling"
else
  echo "FAIL  no compiled binary                 exit $missing_rc"
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
