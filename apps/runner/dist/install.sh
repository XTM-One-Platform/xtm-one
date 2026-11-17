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

# Install (and optionally enroll + autostart) the XTM One runner (the daemon inside XTM One Desktop, headless here) on
# macOS and Linux.
#
# Every platform release image embeds this script and serves it at
#   <instance>/api/v1/runners/dist/xtm-one-runner-installer.sh
# alongside the version-matched runner archives built by the same release
# pipeline (there is no external download location). It:
#
#   1. Detects the OS (macos/linux), CPU architecture (x64/arm64) and, on
#      Linux, the FLAVOR: a box with no graphical session gets the headless
#      build, which links no desktop stack and therefore actually starts on a
#      server (the desktop build fails in the loader on a missing libgdk /
#      libxcb, before any code of ours runs). Override with --headless or
#      --desktop.
#   2. Resolves the GENUINE, version-matched signed archive through the
#      platform (--server, the build embedded in that instance's release), or
#      from an explicit mirror (--download-base) for air-gapped setups.
#   3. Downloads the .tar.gz, verifies its SHA-256 and its release signature
#      (the .minisig the release publishes next to it), installs the binary.
#   4. If --server and --key are given: enrolls once (durable credential in the
#      OS keychain, or a 0600 file where the host has no keychain) and installs
#      an autostart unit - tied to the graphical session for the desktop
#      flavor, which is what full-desktop screenshots require, or a plain
#      always-on service for the headless one.
#
# Pull-only by design: the runner dials OUT to XTM One. No inbound port is ever
# opened on this machine.
#
# Usage:
#   ./install.sh [--server URL] [--key renr-...] [--name NAME]
#                [--download-base URL] [--install-dir DIR] [--no-autostart]
#                [--headless | --desktop] [--require-checksum]
#                [--require-signature]
#   curl -fsSL https://.../xtm-one-runner-installer.sh | bash -s -- --server URL --key renr-...
#
# --require-checksum (or XTM_RUNNER_REQUIRE_CHECKSUM=1) refuses to install a
# binary whose SHA-256 could not be verified - no sha256sum / shasum on the
# host, or no (or an empty) .sha256 next to the archive - instead of installing
# it with a warning. Use it for MDM and other unattended rollouts. A mismatch,
# or a .sha256 that holds no digest, is refused either way.
#
# The SHA-256 comes from the same server as the archive, so it proves the
# download is intact, not who built it. The release signature does: the
# archive's .minisig is checked against the release public key it names, among
# those carried in this script, with minisign when it is installed, otherwise
# with OpenSSL 3, otherwise - on an upgrade - with the runner already installed
# (xtm-one-runner verify-signature). The file its signed trusted comment names
# must be this very archive, the runner version it names (when it names one)
# the version the platform ships, and it must not be older than the release
# signature floor. A signature that does not verify, or was made for another
# archive or another version, is always refused.
# --require-signature (or XTM_RUNNER_REQUIRE_SIGNATURE=1) also refuses a binary
# whose signature could not be checked - no .minisig next to the archive (a
# release built without the signing key ships none), one that is not a
# signature or names no file, or neither verifier on the host - instead of
# installing it with a warning. XTM_RUNNER_MINISIGN_PUBKEY
# replaces the release keys, for runners built and signed with your own key:
# the key line, or the base64 of the whole .pub file as tauri.conf.json has it.
set -euo pipefail

SERVER="${XTM_ONE_SERVER:-}"
KEY="${XTM_ONE_ENROLL_KEY:-}"
NAME=""
DOWNLOAD_BASE="${XTM_RUNNER_DOWNLOAD_BASE:-}"
INSTALL_DIR="${XTM_RUNNER_INSTALL_DIR:-$HOME/.local/bin}"
NO_AUTOSTART=0
# desktop | headless | "" (decide from the environment). XTM_RUNNER_FLAVOR is
# the non-interactive form, for MDM and cloud-init.
FLAVOR="${XTM_RUNNER_FLAVOR:-}"
case "${XTM_RUNNER_REQUIRE_CHECKSUM:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]) REQUIRE_CHECKSUM=1 ;;
    *)                               REQUIRE_CHECKSUM=0 ;;
esac
case "${XTM_RUNNER_REQUIRE_SIGNATURE:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]) REQUIRE_SIGNATURE=1 ;;
    *)                               REQUIRE_SIGNATURE=0 ;;
esac
# The release signing keys: the keys of apps/runner/release-signing.json, the
# keys every self-updating runner is built with
# (backend/tests/test_runner_install_scripts.py keeps them equal, and the image
# build writes in its own). The key id a signature names picks one, so the next
# key is trusted before the release signs with it. A key given in
# XTM_RUNNER_MINISIGN_PUBKEY replaces them (see above).
RELEASE_PUBKEYS="RWST68eWcapzC8LRtFi7bOSPsJYBBeav3f0GL5AYvdrYKlKjDQ9sISAq"
# Their signatures whose trusted timestamp is older than this Unix time are
# refused (0: none): the timestamp_floor of release-signing.json.
RELEASE_TIMESTAMP_FLOOR=0

while [ $# -gt 0 ]; do
    case "$1" in
        --server)        SERVER="$2"; shift 2 ;;
        --key)           KEY="$2"; shift 2 ;;
        --name)          NAME="$2"; shift 2 ;;
        --download-base) DOWNLOAD_BASE="$2"; shift 2 ;;
        --install-dir)   INSTALL_DIR="$2"; shift 2 ;;
        --no-autostart)  NO_AUTOSTART=1; shift ;;
        --headless)      FLAVOR="headless"; shift ;;
        --desktop)       FLAVOR="desktop"; shift ;;
        --require-checksum) REQUIRE_CHECKSUM=1; shift ;;
        --require-signature) REQUIRE_SIGNATURE=1; shift ;;
        -h|--help)
            sed -n '2,/^set -euo/{ /^set -euo/d; p; }' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$1"; }
note() { printf '\033[90m    %s\033[0m\n' "$1"; }
warn() { printf '\033[33mWarning: %s\033[0m\n' "$1" >&2; }
die()  { printf '\033[31mError: %s\033[0m\n' "$1" >&2; exit 1; }

# ── 1. Target detection ──────────────────────────────────────────────────────
case "$(uname -s)" in
    Darwin) OS="macos" ;;
    Linux)  OS="linux" ;;
    *)      die "Unsupported OS: $(uname -s) (this runner ships for macOS and Linux)";;
esac
case "$(uname -m)" in
    x86_64|amd64)   ARCH="x64" ;;
    arm64|aarch64)  ARCH="arm64" ;;
    *)              die "Unsupported architecture: $(uname -m)";;
esac
# Flavor. Only Linux ships two: macOS always has a window server, so its build
# is the desktop one. On Linux, no DISPLAY and no WAYLAND_DISPLAY means no
# graphical session to screenshot or type into, which is exactly the host the
# desktop build cannot even load on - so that is where headless belongs.
if [ -z "$FLAVOR" ]; then
    FLAVOR="desktop"
    if [ "$OS" = "linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        # No display in THIS shell - but that is also exactly what SSH-ing into
        # your own Linux desktop looks like, and silently taking screenshots
        # away from that machine would be worse than the problem we are solving.
        # So ask the system whether it has a graphical session at all.
        if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet graphical.target 2>/dev/null; then
            note "No display in this shell, but this machine runs a graphical session - installing the DESKTOP build. Pass --headless to override."
        else
            FLAVOR="headless"
        fi
    fi
fi
if [ "$FLAVOR" = "headless" ] && [ "$OS" != "linux" ]; then
    die "The headless flavor is Linux-only; on ${OS} the standard build runs as a service."
fi

# Stable archive names, matched to what the release embeds (the macOS build is
# a universal binary, so one archive covers both Mac architectures).
case "$OS/$ARCH" in
    macos/*)     ARCHIVE="xtm-one-runner-macos-universal.tar.gz" ;;
    linux/x64)   ARCHIVE="xtm-one-runner-linux-x64.tar.gz" ;;
    linux/arm64) ARCHIVE="xtm-one-runner-linux-arm64.tar.gz" ;;
esac
TARGET="${OS}/${ARCH}"
if [ "$FLAVOR" = "headless" ]; then
    ARCHIVE="xtm-one-runner-linux-${ARCH}-headless.tar.gz"
    TARGET="$TARGET headless"
fi
step "Installing the XTM One runner (${OS}/${ARCH}, ${FLAVOR})"
if [ "$FLAVOR" = "headless" ]; then
    note "No graphical session detected: installing the headless build (shell, files and browser control; no screen capture or synthetic input)."
fi

command -v curl >/dev/null 2>&1 || die "curl is required."
# Hash stdin: given a path, sha256sum escapes its output line when the name
# holds a backslash.
if command -v sha256sum >/dev/null 2>&1; then
    sha_of() { sha256sum < "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha_of() { shasum -a 256 < "$1" | awk '{print $1}'; }
else
    sha_of() { echo ""; }
    # Nothing to verify with: refuse before downloading anything when asked to.
    if [ "$REQUIRE_CHECKSUM" -eq 1 ]; then
        die "Refusing to install an unverified binary: no sha256sum or shasum on this host (--require-checksum)."
    fi
fi

# Release signature (minisign). A .minisig is minisign's four-line text:
# untrusted comment, signature (algorithm "ED", key id, Ed25519 over the
# BLAKE2b-512 of the file), trusted comment, and a global signature over
# signature || trusted comment. Older releases served base64 of that
# text instead; both forms are read, like the self-updater does
# (apps/runner/src/update.rs), which also refuses the legacy "Ed" algorithm
# (Ed25519 over the file itself): the release never uses it.
# The text is decoded with the unused bits of its last character cleared:
# OpenSSL and GNU base64 ignore them, the uutils coreutils of Ubuntu 25.10 and
# later refuse them ("invalid input").
b64d() {
    local text
    IFS= read -r -d '' text || true
    text="$(b64_canonical "$text")"
    if command -v openssl >/dev/null 2>&1; then
        printf '%s' "$text" | openssl base64 -d -A 2>/dev/null
    elif printf '' | base64 -d >/dev/null 2>&1; then
        printf '%s' "$text" | base64 -d 2>/dev/null
    else
        printf '%s' "$text" | base64 -D 2>/dev/null
    fi
}
# b64_canonical <base64>: the same text with the bits its last character does
# not encode set to zero - 2 of them before "=", 4 before "==". Anything else
# is printed as it is.
b64_canonical() {
    local text="$1" data last index
    local alphabet=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/
    data="${text%%=*}"
    last="${data: -1}"
    case "${text:${#data}}:$last" in
        "=:"?) index="${alphabet%%"$last"*}"; index=$(( ${#index} & ~3 )) ;;
        "==:"?) index="${alphabet%%"$last"*}"; index=$(( ${#index} & ~15 )) ;;
        *) printf '%s' "$text"; return ;;
    esac
    [ "$index" -lt 64 ] || { printf '%s' "$text"; return; }
    printf '%s%s%s' "${data%?}" "${alphabet:$index:1}" "${text:${#data}}"
}
b64e() {
    if command -v openssl >/dev/null 2>&1; then openssl base64 -A; else base64; fi | tr -d '\n'
}
# -v: without it od prints a line equal to the one before as "*", so 32 zero
# bytes would read as 16.
hex_of() { od -An -v -tx1 | tr -d ' \n'; }
size_of() { wc -c < "$1" | tr -d ' '; }
# is_b64 <text>: base64 that every decoder reads as the same bytes - nothing but
# its alphabet, whole groups of four, "=" only as the last one or two. Decoders
# differ on anything else: OpenSSL 3.0 decodes past a stray character, 3.5 drops
# an incomplete group, GNU base64 decodes it and fails. Spelled out: a range
# such as A-Z depends on the locale.
is_b64() {
    case "$1" in
        "" | *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=]* | *=*[!=]* | *===) return 1 ;;
    esac
    [ $((${#1} % 4)) -eq 0 ]
}

# minisig_text <.minisig> <out>: the four-line text, or fail when it is not one.
# A byte order mark or blanks a mirror may add sit before the untrusted comment,
# which nothing signs.
minisig_text() {
    local wrapped
    if [ "$(head -c 3 "$1" | hex_of)" = "efbbbf" ]; then tail -c +4 "$1"; else cat "$1"; fi |
        tr -d '\r' | awk 'NF' | sed '1s/^[[:blank:]]*//' > "$2.raw"
    case "$(sed -n 1p "$2.raw")" in
        "untrusted comment:"*) cp "$2.raw" "$2" ;;
        *)
            wrapped="$(tr -d ' \t\n' < "$2.raw")"
            is_b64 "$wrapped" || return 1
            printf '%s' "$wrapped" | b64d | tr -d '\r' | awk 'NF' > "$2" || return 1
            ;;
    esac
    [ "$(wc -l < "$2" | tr -d ' ')" -eq 4 ] || return 1
    case "$(sed -n 1p "$2")" in "untrusted comment:"*) ;; *) return 1 ;; esac
    case "$(sed -n 3p "$2")" in "trusted comment: "*) ;; *) return 1 ;; esac
}

# key_line <value> <scratch path>: print the key line of a minisign public key
# given as that line or, as tauri.conf.json and the release build carry it, as
# base64 of the whole .pub file (parse_minisign_pubkey in
# apps/runner/src/update.rs reads both). Fails when it is neither.
key_line() {
    local line wrapped
    wrapped="$(printf '%s' "$1" | tr -d ' \t\r\n')"
    { printf '%s\n' "$1"; if is_b64 "$wrapped"; then printf '%s' "$wrapped" | b64d; fi; printf '\n'; } 2>/dev/null |
        tr -d '\r' > "$2.txt" || true
    while IFS= read -r line; do
        line="$(printf '%s' "$line" | tr -d ' \t')"
        is_b64 "$line" || continue
        printf '%s' "$line" | b64d > "$2.bin" 2>/dev/null || continue
        if [ "$(size_of "$2.bin")" = 42 ] && [ "$(head -c 2 "$2.bin" | hex_of)" = "4564" ]; then
            printf '%s' "$line"
            return 0
        fi
    done < "$2.txt"
    return 1
}

# key_id_of <key line>: its key ID as minisign and the .pub file print it (the
# little-endian bytes as one hex number, no leading zeros).
key_id_of() {
    local id="" b
    for b in $(printf '%s' "$1" | b64d | tail -c +3 | head -c 8 | od -An -v -tx1); do id="$b$id"; done
    id="$(printf '%s' "$id" | tr 'abcdef' 'ABCDEF' | sed 's/^0*//')"
    printf '%s' "${id:-0}"
}

# verify_openssl <file> <minisig text> <scratch dir holding pk, sig and gsig>
verify_openssl() {
    local file="$1" sig="$2" d="$3"
    openssl dgst -blake2b512 -binary "$file" > "$d/msg" 2>/dev/null || return 1
    # SubjectPublicKeyInfo DER prefix of an Ed25519 key, then the 32 key bytes.
    printf '\060\052\060\005\006\003\053\145\160\003\041\000' > "$d/pk.der"
    tail -c 32 "$d/pk" >> "$d/pk.der"
    tail -c 64 "$d/sig" > "$d/sig64"
    openssl pkeyutl -verify -pubin -keyform DER -inkey "$d/pk.der" -rawin \
        -in "$d/msg" -sigfile "$d/sig64" >/dev/null 2>&1 || return 1
    SIG_REASON="its trusted comment does not verify"
    { cat "$d/sig64"; sed -n 3p "$sig" | sed 's/^trusted comment: //' | tr -d '\n'; } > "$d/gmsg"
    openssl pkeyutl -verify -pubin -keyform DER -inkey "$d/pk.der" -rawin \
        -in "$d/gmsg" -sigfile "$d/gsig" >/dev/null 2>&1
}

verify_minisign() { minisign -V -q -m "$1" -x "$2" -P "$3" >/dev/null 2>&1; }

# checked_minisig <minisig text> <scratch dir holding sig and gsig>: the four
# lines as verify_signature read them, for minisign and the runner. Both parse
# the lines as served and refuse what OpenSSL and install.ps1 accept: whitespace
# inside the base64, an untrusted comment without its space (minisign), unused
# bits set in the last base64 character (the runner's minisign-verify).
checked_minisig() {
    { printf 'untrusted comment: signature\n'; b64e < "$2/sig"; printf '\n'; sed -n 3p "$1"; b64e < "$2/gsig"; printf '\n'; } > "$2/minisig"
}

# small_order <64 hex digits>: true for an encoding of a point of small order
# (libsodium's list, sign bit ignored), and for anything that is not 32 bytes of
# hex. OpenSSL verifies against such a key, and a key of small order accepts
# forgeries, so the key and the R of both signatures are checked here.
small_order() {
    local h="$1"
    case "$h" in
        *[!0123456789abcdef]*) return 0 ;;
    esac
    [ "${#h}" -eq 64 ] || return 0
    h="${h:0:62}$(printf '%02x' $((0x${h:62:2} & 0x7f)))"
    case "$h" in
        0000000000000000000000000000000000000000000000000000000000000000 | \
        0100000000000000000000000000000000000000000000000000000000000000 | \
        26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05 | \
        c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a | \
        ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f | \
        edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f | \
        eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f) return 0 ;;
    esac
    return 1
}

# verify_signature <file> <minisig text> <key lines> <scratch dir>: 0 when the
# signature verifies against the key of the list whose id it names (SIG_KEY),
# else 1 with SIG_REASON saying why not.
verify_signature() {
    local file="$1" sig="$2" keys="$3" d="$4" key line2 line4 id
    mkdir -p "$d"
    SIG_KEY=""
    SIG_REASON="the release key is not a minisign public key"
    [ -n "$keys" ] || return 1
    for key in $keys; do
        is_b64 "$key" || return 1
        printf '%s' "$key" | b64d > "$d/pk" 2>/dev/null || return 1
        [ "$(size_of "$d/pk")" = 42 ] && [ "$(head -c 2 "$d/pk" | hex_of)" = "4564" ] || return 1
    done
    SIG_REASON="not a minisign signature"
    line2="$(sed -n 2p "$sig" | tr -d ' \t')"
    line4="$(sed -n 4p "$sig" | tr -d ' \t')"
    is_b64 "$line2" && is_b64 "$line4" || return 1
    printf '%s' "$line2" | b64d > "$d/sig" 2>/dev/null || return 1
    printf '%s' "$line4" | b64d > "$d/gsig" 2>/dev/null || return 1
    [ "$(size_of "$d/sig")" = 74 ] && [ "$(size_of "$d/gsig")" = 64 ] || return 1
    case "$(head -c 2 "$d/sig" | hex_of)" in 4544|4564) ;; *) return 1 ;; esac
    SIG_REASON="signed with another key"
    id="$(tail -c +3 "$d/sig" | head -c 8 | hex_of)"
    for key in $keys; do
        printf '%s' "$key" | b64d > "$d/pk"
        if [ "$(tail -c +3 "$d/pk" | head -c 8 | hex_of)" = "$id" ]; then
            SIG_KEY="$key"
            break
        fi
    done
    [ -n "$SIG_KEY" ] || return 1
    SIG_REASON="legacy"
    [ "$(head -c 2 "$d/sig" | hex_of)" = "4544" ] || return 1
    SIG_REASON="does not verify"
    ! small_order "$(tail -c 32 "$d/pk" | hex_of)" || return 1
    ! small_order "$(tail -c 64 "$d/sig" | head -c 32 | hex_of)" || return 1
    # Neither OpenSSL nor the runner's minisign-verify refuses it.
    SIG_REASON="its trusted comment does not verify"
    ! small_order "$(head -c 32 "$d/gsig" | hex_of)" || return 1
    SIG_REASON="does not verify"
    case "$SIG_VERIFIER" in
        minisign) checked_minisig "$sig" "$d" && verify_minisign "$file" "$d/minisig" "$SIG_KEY" ;;
        openssl)  verify_openssl "$file" "$sig" "$d" ;;
        runner)   checked_minisig "$sig" "$d" &&
                      "$INSTALLED_RUNNER" verify-signature --pubkey "$SIG_KEY" --signature "$d/minisig" "$file" >/dev/null 2>&1 ;;
        *)        false ;;
    esac || return 1
    SIG_REASON=""
}

# signed_field <name> <minisig text>: the value of the first <name>: field of
# its trusted comment, empty when it has none. tauri signer writes
# "timestamp:<t>\tfile:<name>"; the release adds "version:<v>".
signed_field() {
    sed -n 3p "$2" | sed 's/^trusted comment: //' |
        awk -v name="$1:" 'BEGIN { FS = "\t" } { for (i = 1; i <= NF; i++) if (substr($i, 1, length(name)) == name) { print substr($i, length(name) + 1); exit } }'
}

# signed_since <trusted timestamp> <floor>: true when the timestamp is a Unix
# time no older than the floor; one that is not a number is refused.
signed_since() {
    case "$1" in "" | *[!0123456789]*) return 1 ;; esac
    [ "${#1}" -gt 18 ] || [ "$1" -ge "$2" ]
}

# A verifier is trusted only once it accepts a known-good signature AND rejects
# the same signature over other bytes: a missing algorithm (OpenSSL 1.1, or
# LibreSSL on macOS) then reads as "cannot check", never as "bad signature".
# The vector is the self-updater's test fixture (throwaway key). On an
# upgrade the runner already installed is the last candidate, for a host with
# neither minisign nor OpenSSL 3 (a stock Mac): every runner routes `help` to
# its CLI, never to the GUI, and only one that has `verify-signature` answers 0.
select_verifier() {
    local d="$1/selftest" candidate
    mkdir -p "$d"
    printf 'xtm-one-runner self-update fixture payload (#3743)\n' > "$d/good"
    printf 'Xtm-one-runner self-update fixture payload (#3743)\n' > "$d/bad"
    printf '%s\n' \
        'untrusted comment: signature from xtm-one-runner test key' \
        'RUSRNhxpZP3VePJ9Gt9CA/TxR2O4oPCtO+vp0gPyMUuTHLwleciB4JQoQFkyfAEAqKvv/4uCXCk1p6SLNm+S9YkD2H3+J9DKiw8=' \
        "$(printf 'trusted comment: timestamp:1790000000\tfile:fixture.tar.gz')" \
        'TEKrt719BBzN00ESdP6yW2DruNkJT1VDUmEae/USsn3IZgs0fPyo8EMEjIbyfPxzudZZA0pwtIK9UD8mT6wyCA==' \
        > "$d/sig"
    for candidate in minisign openssl runner; do
        if [ "$candidate" = runner ]; then
            [ -x "$INSTALLED_RUNNER" ] && "$INSTALLED_RUNNER" help verify-signature >/dev/null 2>&1 || continue
        else
            command -v "$candidate" >/dev/null 2>&1 || continue
        fi
        SIG_VERIFIER="$candidate"
        if verify_signature "$d/good" "$d/sig" 'RWSRNhxpZP3VeNdBcMrZ70t+ks6N46DKBb15Q9yCGtYii+76LXWRTd7T' "$d/good.chk" \
            && ! verify_signature "$d/bad" "$d/sig" 'RWSRNhxpZP3VeNdBcMrZ70t+ks6N46DKBb15Q9yCGtYii+76LXWRTd7T' "$d/bad.chk"; then
            return 0
        fi
    done
    SIG_VERIFIER=""
    return 1
}

# ── 2. Resolve the version-matched artifact + integrity URLs ──────────────────
# The platform redirects /download/{os}/{arch} to the exact archive embedded in
# its own release; -L follows it and %{url_effective} gives us the resolved URL
# so we can find the sibling .sha256. --download-base is the explicit override
# for air-gapped mirrors of the /dist directory.
ARCHIVE_URL=""
SHA_URL=""
SIG_URL=""
PROBE_CODE=""
# The runner version the platform ships, when the archive comes from it.
EXPECTED_VERSION=""
if [ -n "$SERVER" ]; then
    base="${SERVER%/}"
    # HEAD probe: 200 after following the redirect proves the artifact is
    # embedded; the effective URL is the /dist asset whose sibling .sha256 we
    # verify against. (curl emits -w output even on failures, so check the
    # status code rather than the URL.)
    if [ "$FLAVOR" = "headless" ]; then
        dl="$base/api/v1/runners/download/linux/headless?arch=$ARCH"
    else
        dl="$base/api/v1/runners/download/$OS/$ARCH"
    fi
    probe="$(curl -sIL -o /dev/null -w '%{http_code} %{url_effective}' "$dl" 2>/dev/null || true)"
    code="${probe%% *}"
    eff="${probe#* }"
    PROBE_CODE="${code:-000}"
    # Only a 404 says anything about how the platform was built; every other
    # failure is about the path to it, and names its status so the operator
    # looks at the proxy or the backend rather than the build pipeline.
    case "$PROBE_CODE" in
        200)
            if [ -n "$eff" ]; then
                ARCHIVE_URL="$dl"
                SHA_URL="${eff}.sha256"
                SIG_URL="${eff}.minisig"
                # The platform redirects to /dist/<archive>: a 200 anywhere else
                # is an auth proxy or a sign-in page answering in its place.
                case "$eff" in
                    */"$ARCHIVE") note "Version-matched to the platform via $base" ;;
                    *) warn "The platform at $base resolved the download to $eff, which is not the runner archive: an auth proxy or a sign-in page may be answering for /api/v1/runners/download." ;;
                esac
                # The archive names are the same in every release: the version
                # the signature names is what tells an older build from this one.
                EXPECTED_VERSION="$(curl -fsSL --max-filesize 1048576 "$base/api/v1/runners/download/manifest" 2>/dev/null |
                    sed -n 's/.*"runner_version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' | head -n 1)" ||
                    EXPECTED_VERSION=""
            fi
            ;;
        404)     warn "The platform at $base has no embedded runner download for ${TARGET} (HTTP 404: a source/dev build, runners disabled on this instance, or a release that does not ship this build)." ;;
        000)     warn "Could not reach $base (DNS, TLS or connection refused). Check the URL and the network path." ;;
        401|403) warn "The platform at $base refused the download request (HTTP $PROBE_CODE): an auth proxy or WAF may be blocking /api/v1/runners/download." ;;
        5*)      warn "The platform at $base answered HTTP $PROBE_CODE: it is reachable but not serving downloads right now. This is usually transient - retry, or check the backend and the proxy." ;;
        *)       warn "Unexpected HTTP $PROBE_CODE from $base while resolving the runner archive." ;;
    esac
fi
if [ -z "$ARCHIVE_URL" ] && [ -n "$DOWNLOAD_BASE" ]; then
    b="${DOWNLOAD_BASE%/}"
    ARCHIVE_URL="$b/$ARCHIVE"
    SHA_URL="$b/$ARCHIVE.sha256"
    SIG_URL="$b/$ARCHIVE.minisig"
fi
if [ -z "$ARCHIVE_URL" ]; then
    if [ -n "$SERVER" ]; then
        die "No download source (last probe: HTTP $PROBE_CODE from $SERVER). Retry once the platform serves downloads, or pass --download-base <mirror URL>."
    fi
    die "No download source: pass --server <your XTM One URL> (release images embed the runner) or --download-base <mirror URL>."
fi
# Over plain HTTP the .sha256 rides the same unauthenticated connection as the
# binary it vouches for, so it cannot catch a swap.
for url in "$ARCHIVE_URL" "$SHA_URL"; do
    case "$url" in
        [Hh][Tt][Tt][Pp]://*)
            warn "Plain HTTP download ($url): the binary and its .sha256 travel unencrypted, so whoever can intercept the connection can replace both and the SHA-256 check still passes. Use an https:// URL."
            break
            ;;
    esac
done

# ── 3. Download + verify ─────────────────────────────────────────────────────
WORK="$(mktemp -d "${TMPDIR:-/tmp}/xtm-one-runner.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
INSTALLED_RUNNER="$INSTALL_DIR/xtm-one-runner"
if [ -n "${XTM_RUNNER_MINISIGN_PUBKEY:-}" ]; then
    RELEASE_PUBKEYS="$(key_line "$XTM_RUNNER_MINISIGN_PUBKEY" "$WORK/key")" ||
        die "Refusing to install: XTM_RUNNER_MINISIGN_PUBKEY is not a minisign public key (give the key line of the .pub file, or the base64 of the whole file as tauri.conf.json has it)."
    # The floor is the release keys' own.
    RELEASE_TIMESTAMP_FLOOR=0
    warn "XTM_RUNNER_MINISIGN_PUBKEY replaces the release keys this script carries: signatures are checked against key ID $(key_id_of "$RELEASE_PUBKEYS")."
fi
SIG_VERIFIER=""
if ! select_verifier "$WORK" && [ "$REQUIRE_SIGNATURE" -eq 1 ]; then
    # Nothing to verify with: refuse before downloading anything when asked to.
    die "Refusing to install an unverified binary: neither minisign nor OpenSSL 3 (Ed25519 + BLAKE2b) on this host can check the release signature (--require-signature)."
fi
TARBALL="$WORK/$ARCHIVE"
step "Downloading $ARCHIVE_URL"
curl -fSL "$ARCHIVE_URL" -o "$TARBALL"

actual="$(sha_of "$TARBALL")"
# The causes are told apart: a host without a hashing tool is a host problem, a
# missing .sha256 means the mirror is incomplete or the resolved URL is not the
# artifact we think it is (every release archive ships one). Its status says
# which: curl -f fails on a 404 and on a 5xx alike.
UNVERIFIED=""
expected=""
if [ -z "$actual" ]; then
    UNVERIFIED="no sha256sum or shasum on this host"
else
    SHA_FILE="$WORK/$ARCHIVE.sha256"
    sha_code="$(curl -sSL -o "$SHA_FILE" -w '%{http_code}' "$SHA_URL" 2>/dev/null || true)"
    if [ "$sha_code" != "200" ]; then
        case "$sha_code" in
            ""|000) sha_why="no response" ;;
            *)      sha_why="HTTP $sha_code" ;;
        esac
        UNVERIFIED="no .sha256 could be fetched from $SHA_URL ($sha_why)"
    else
        # Read like the self-updater (apps/runner/src/update.rs): the first
        # token - a bare digest or the sha256sum form - in any case, CRLF or not.
        if [ -f "$SHA_FILE" ]; then
            expected="$(awk '{ gsub(/\r/, "") } NF { print tolower($1); exit }' "$SHA_FILE")"
        fi
        if [ -z "$expected" ]; then
            UNVERIFIED="the .sha256 at $SHA_URL is empty"
        else
            case "$expected" in
                *[!0-9a-f]*) digest_ok=0 ;;
                *)           digest_ok=1 ;;
            esac
            if [ "$digest_ok" -ne 1 ] || [ "${#expected}" -ne 64 ]; then
                die "Refusing to install: the .sha256 at $SHA_URL does not hold a SHA-256 digest."
            fi
        fi
    fi
fi
if [ -z "$UNVERIFIED" ]; then
    [ "$expected" = "$actual" ] || die "SHA-256 mismatch for $ARCHIVE (expected $expected, got $actual)."
    ok "SHA-256 verified."
elif [ "$REQUIRE_CHECKSUM" -eq 1 ]; then
    die "Refusing to install an unverified binary: $UNVERIFIED (--require-checksum)."
else
    warn "INTEGRITY NOT VERIFIED: $UNVERIFIED. The binary is installed without a checksum check; pass --require-checksum (or set XTM_RUNNER_REQUIRE_CHECKSUM=1) to refuse instead."
fi

# The signature is what ties the archive to the release: a mirror or a proxy
# able to change the archive can change its .sha256 too, not its .minisig.
UNSIGNED=""
if [ -z "$SIG_VERIFIER" ]; then
    UNSIGNED="neither minisign nor OpenSSL 3 (Ed25519 + BLAKE2b) on this host can check the release signature"
else
    SIG_FILE="$WORK/$ARCHIVE.minisig"
    # A signature is well under 64 KB: never read more from the server. Before
    # 8.4.0, curl only stops a body whose size the server announces, hence the
    # size check below.
    sig_rc=0
    sig_code="$(curl -sSL --max-filesize 65536 -o "$SIG_FILE" -w '%{http_code}' "$SIG_URL" 2>/dev/null)" || sig_rc=$?
    if [ "$sig_code" != "200" ]; then
        case "$sig_code" in
            ""|000) sig_why="no response" ;;
            *)      sig_why="HTTP $sig_code" ;;
        esac
        UNSIGNED="no .minisig could be fetched from $SIG_URL ($sig_why)"
    elif [ "$sig_rc" -eq 63 ]; then
        UNSIGNED="the .minisig at $SIG_URL is larger than a minisign signature can be"
    elif [ "$sig_rc" -ne 0 ]; then
        UNSIGNED="the .minisig at $SIG_URL could not be read in full (curl exit $sig_rc)"
    elif [ ! -s "$SIG_FILE" ]; then
        UNSIGNED="the .minisig at $SIG_URL is empty"
    elif [ "$(size_of "$SIG_FILE")" -gt 65536 ]; then
        UNSIGNED="the .minisig at $SIG_URL is larger than a minisign signature can be"
    # A body that is not a minisign signature at all (a mirror's or a proxy's
    # error page, an SPA catch-all) counts as a missing one: whoever can swap
    # files can delete the signature instead, and a 200 with an error page must
    # not end an install that works without the signature check.
    elif ! minisig_text "$SIG_FILE" "$WORK/signature.txt"; then
        UNSIGNED="the .minisig at $SIG_URL is not a minisign signature"
    elif ! verify_signature "$TARBALL" "$WORK/signature.txt" "$RELEASE_PUBKEYS" "$WORK/verify"; then
        case "$SIG_REASON" in
            "not a minisign signature")
                UNSIGNED="the .minisig at $SIG_URL is not a minisign signature" ;;
            legacy)
                die "Refusing to install: the .minisig at $SIG_URL is a legacy (not prehashed) minisign signature, which the release never makes." ;;
            *)
                die "Refusing to install: the signature at $SIG_URL does not verify against the release key ($SIG_REASON) - $ARCHIVE was changed after it was signed, or signed with another key." ;;
        esac
    elif ! SIGNED_FILE="$(signed_field file "$WORK/signature.txt")" ||
        ! SIGNED_VERSION="$(signed_field version "$WORK/signature.txt")" ||
        ! SIGNED_AT="$(signed_field timestamp "$WORK/signature.txt")"; then
        die "Refusing to install: the trusted comment of the signature at $SIG_URL could not be read, so nothing says it was made for $ARCHIVE."
    # A trusted comment of the signer's own (minisign -t) may name no file: it
    # is bound to no archive, not to another one.
    elif [ -z "$SIGNED_FILE" ]; then
        UNSIGNED="the trusted comment of the signature at $SIG_URL names no file, so the signature cannot be tied to $ARCHIVE"
    elif [ "$SIGNED_FILE" != "$ARCHIVE" ]; then
        die "Refusing to install: the signature at $SIG_URL is not for $ARCHIVE (its trusted comment names $SIGNED_FILE) - a signed build of another platform or flavor was served in its place."
    # Archives signed before versions were name none, and keep installing.
    elif [ -n "$SIGNED_VERSION" ] && [ -n "$EXPECTED_VERSION" ] && [ "$SIGNED_VERSION" != "$EXPECTED_VERSION" ]; then
        die "Refusing to install: the signature at $SIG_URL is for runner $SIGNED_VERSION, not $EXPECTED_VERSION, the version $SERVER ships - another signed build of $ARCHIVE was served in its place."
    elif [ "$RELEASE_TIMESTAMP_FLOOR" -gt 0 ] && [ -z "$SIGNED_AT" ]; then
        die "Refusing to install: the signature at $SIG_URL names no signing time, so nothing says it is newer than $RELEASE_TIMESTAMP_FLOOR, the oldest release signature this script accepts."
    elif [ "$RELEASE_TIMESTAMP_FLOOR" -gt 0 ] && ! signed_since "$SIGNED_AT" "$RELEASE_TIMESTAMP_FLOOR"; then
        die "Refusing to install: the signature at $SIG_URL was made at ${SIGNED_AT}, before $RELEASE_TIMESTAMP_FLOOR, the oldest release signature this script accepts - an older signed build was served in its place."
    else
        ok "Release signature verified ($SIG_VERIFIER)."
        if [ -n "$SIGNED_VERSION" ]; then
            note "Signed with key ID $(key_id_of "$SIG_KEY") for runner ${SIGNED_VERSION}."
        else
            note "Signed with key ID $(key_id_of "$SIG_KEY"). The signature names no runner version (signed before release signatures carried one), so it cannot tell this build from an older one."
        fi
    fi
fi
if [ -n "$UNSIGNED" ]; then
    if [ "$REQUIRE_SIGNATURE" -eq 1 ]; then
        die "Refusing to install an unverified binary: $UNSIGNED (--require-signature)."
    fi
    warn "SIGNATURE NOT VERIFIED: $UNSIGNED. Nothing proves the binary was built by the release; pass --require-signature (or set XTM_RUNNER_REQUIRE_SIGNATURE=1) to refuse instead."
fi

# ── 4. Install ───────────────────────────────────────────────────────────────
tar -xzf "$TARBALL" -C "$WORK"
[ -f "$WORK/xtm-one-runner" ] || die "Archive did not contain the xtm-one-runner binary."
mkdir -p "$INSTALL_DIR"
BIN="$INSTALL_DIR/xtm-one-runner"
# Replace atomically so a running instance keeps its open file handle.
mv -f "$WORK/xtm-one-runner" "$BIN.new"
chmod +x "$BIN.new"
mv -f "$BIN.new" "$BIN"
ok "Installed to $BIN"
case ":$PATH:" in
    *":$INSTALL_DIR:"*) : ;;
    *) note "Add $INSTALL_DIR to your PATH to run 'xtm-one-runner' directly." ;;
esac

# ── 5. Enroll (optional) ─────────────────────────────────────────────────────
ENROLLED=0
if [ -n "$SERVER" ] && [ -n "$KEY" ]; then
    step "Enrolling with $SERVER"
    set -- enroll --server "$SERVER" --key "$KEY"
    [ -n "$NAME" ] && set -- "$@" --name "$NAME"
    "$BIN" "$@"
    ENROLLED=1
    ok "Enrolled. Credential stored in the OS keychain, or in a 0600 file when the host has none (headless servers usually do not run a Secret Service)."
fi

# ── 6. Autostart (graphical session for desktop, plain service for headless) ─
if [ "$ENROLLED" -eq 1 ] && [ "$NO_AUTOSTART" -eq 0 ]; then
    if [ "$OS" = "macos" ]; then
        step "Installing LaunchAgent"
        PLIST="$HOME/Library/LaunchAgents/io.filigran.xtm-one-runner.plist"
        mkdir -p "$HOME/Library/LaunchAgents"
        cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>io.filigran.xtm-one-runner</string>
    <key>ProgramArguments</key><array><string>$BIN</string><string>run</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLISTEOF
        launchctl unload "$PLIST" >/dev/null 2>&1 || true
        launchctl load -w "$PLIST" >/dev/null 2>&1 || note "Run: launchctl load -w $PLIST"
        ok "LaunchAgent installed; runner starts at login."
        note "macOS will prompt for Screen Recording (and Accessibility) permission on first capture - approve it in System Settings > Privacy & Security."
    else
        if command -v systemctl >/dev/null 2>&1; then
            step "Installing systemd --user service"
            UNIT_DIR="$HOME/.config/systemd/user"
            mkdir -p "$UNIT_DIR"
            # The desktop flavor must come up WITH the graphical session - it is
            # what it captures and types into. The headless flavor has no such
            # dependency, and ordering it after a target that never activates on
            # a server is how you get a service that is enabled and never runs.
            if [ "$FLAVOR" = "headless" ]; then
                UNIT_DESC="XTM One runner (headless)"
                UNIT_AFTER=""
            else
                UNIT_DESC="XTM One runner"
                UNIT_AFTER="After=graphical-session.target"
            fi
            cat > "$UNIT_DIR/xtm-one-runner.service" <<UNITEOF
[Unit]
Description=$UNIT_DESC
$UNIT_AFTER

[Service]
ExecStart="$BIN" run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNITEOF
            systemctl --user daemon-reload
            systemctl --user enable --now xtm-one-runner.service
            ok "systemd --user service enabled and started."
            # Without lingering, a --user service dies with the SSH session that
            # started it - which on a server means the runner is online exactly
            # as long as someone is logged in.
            run_user="${USER:-$(id -un)}"
            if [ "$FLAVOR" = "headless" ]; then
                if loginctl enable-linger "$run_user" >/dev/null 2>&1; then
                    ok "Lingering enabled: the runner keeps running after you log out."
                else
                    note "Enable lingering so the runner survives logout: sudo loginctl enable-linger $run_user"
                fi
            else
                note "For capture on a headless/SSH login, enable lingering: loginctl enable-linger $run_user"
            fi
        else
            note "systemd not found. Start manually with: $BIN run"
        fi
    fi
fi

echo ""
if [ -n "$UNVERIFIED" ]; then
    printf '\033[33mXTM One runner installed WITHOUT integrity verification (%s).\033[0m\n' "$UNVERIFIED"
fi
if [ -n "$UNSIGNED" ]; then
    printf '\033[33mXTM One runner installed WITHOUT signature verification (%s).\033[0m\n' "$UNSIGNED"
fi
if [ -z "$UNVERIFIED" ] && [ -z "$UNSIGNED" ]; then
    printf '\033[32mXTM One runner installed.\033[0m\n'
fi
if [ "$ENROLLED" -eq 1 ]; then
    note "Check status any time with:  $BIN status"
else
    printf '\033[33mNext step - enroll this machine (copy the command from the XTM One Desktop page in XTM One):\033[0m\n'
    echo   "  xtm-one-runner enroll --server <YOUR_XTM_ONE_URL> --key <renr-...>"
    printf '\033[33mThen start it with:\033[0m\n'
    echo   "  xtm-one-runner run"
fi
