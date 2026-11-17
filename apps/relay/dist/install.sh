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

# Install, enroll and start the XTM One Relay as a system service on Linux and
# macOS.
#
# Every platform release image embeds this script and serves it at
#   <instance>/api/v1/relays/dist/xtm-one-relay-installer.sh
# next to the version-matched relay archives built by the same release
# pipeline. It:
#
#   1. Detects the OS (linux/macos) and CPU architecture (x64/arm64).
#   2. Resolves the version-matched signed archive through the platform
#      (--server), or from an explicit mirror (--download-base) for
#      air-gapped setups.
#   3. Downloads it, verifies its SHA-256 and its release signature (the
#      .minisig published next to it) and installs the binary under
#      /opt/xtm-one-relay (linked from /usr/local/bin).
#   4. Writes relay.toml with the platform URL and the relay's local
#      allow-list (--destinations) when there is none yet.
#   5. With --server and --key: enrolls once (the credential is a 0600 file
#      next to relay.toml) and installs the system service: systemd under a
#      dedicated xtm-one-relay account on Linux, a LaunchDaemon on macOS.
#
# Pull-only by design: the relay dials OUT to XTM One over HTTPS. No inbound
# port is ever opened on this machine. It only reaches the destinations its
# local list allows.
#
# Usage (as root):
#   ./install.sh --server URL --key rlenr-... --destinations 'splunk.corp:8089,10.0.0.0/8:443'
#                [--name NAME] [--download-base URL] [--install-dir DIR]
#                [--user ACCOUNT] [--no-service] [--require-checksum]
#                [--require-signature]
#   curl -fsSL https://.../xtm-one-relay-installer.sh | sudo bash -s -- --server URL --key rlenr-...
#
# --require-checksum / --require-signature (or XTM_RELAY_REQUIRE_CHECKSUM=1 /
# XTM_RELAY_REQUIRE_SIGNATURE=1) refuse a binary whose SHA-256 / release
# signature could not be checked, instead of installing it with a warning. A
# mismatch, or a signature made for another archive or version, is always
# refused. XTM_RELAY_MINISIGN_PUBKEY replaces the release keys, for relays
# built and signed with your own key.
set -euo pipefail

SERVER="${XTM_ONE_SERVER:-}"
KEY="${XTM_RELAY_ENROLLMENT_KEY:-}"
NAME="${XTM_RELAY_NAME:-}"
DESTINATIONS="${XTM_RELAY_ALLOWED_DESTINATIONS:-}"
DOWNLOAD_BASE="${XTM_RELAY_DOWNLOAD_BASE:-}"
INSTALL_DIR="${XTM_RELAY_INSTALL_DIR:-/opt/xtm-one-relay}"
SERVICE_USER="${XTM_RELAY_SERVICE_USER:-xtm-one-relay}"
NO_SERVICE=0
case "${XTM_RELAY_REQUIRE_CHECKSUM:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]) REQUIRE_CHECKSUM=1 ;;
    *)                               REQUIRE_CHECKSUM=0 ;;
esac
case "${XTM_RELAY_REQUIRE_SIGNATURE:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]) REQUIRE_SIGNATURE=1 ;;
    *)                               REQUIRE_SIGNATURE=0 ;;
esac
# The release signing keys: those of apps/runner/release-signing.json, which
# the relay is built and signed with too
# (backend/tests/test_relay_install_scripts.py keeps them equal).
RELEASE_PUBKEYS="RWST68eWcapzC8LRtFi7bOSPsJYBBeav3f0GL5AYvdrYKlKjDQ9sISAq"
# Their signatures whose trusted timestamp is older than this Unix time are
# refused (0: none): the timestamp_floor of release-signing.json.
RELEASE_TIMESTAMP_FLOOR=0

while [ $# -gt 0 ]; do
    case "$1" in
        --server)        SERVER="$2"; shift 2 ;;
        --key)           KEY="$2"; shift 2 ;;
        --name)          NAME="$2"; shift 2 ;;
        --destinations)  DESTINATIONS="$2"; shift 2 ;;
        --download-base) DOWNLOAD_BASE="$2"; shift 2 ;;
        --install-dir)   INSTALL_DIR="$2"; shift 2 ;;
        --user)          SERVICE_USER="$2"; shift 2 ;;
        --no-service)    NO_SERVICE=1; shift ;;
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
[ "$(id -u)" -eq 0 ] || die "Run this installer as root (sudo): the relay installs a system service."
case "$(uname -s)" in
    Darwin) OS="macos"; CONFIG="/Library/Application Support/XTM One Relay/relay.toml" ;;
    Linux)  OS="linux"; CONFIG="/etc/xtm-one-relay/relay.toml" ;;
    *)      die "Unsupported OS: $(uname -s) (use install.ps1 on Windows, or the container image)";;
esac
case "$(uname -m)" in
    x86_64|amd64)   ARCH="x64" ;;
    arm64|aarch64)  ARCH="arm64" ;;
    *)              die "Unsupported architecture: $(uname -m)";;
esac
case "$OS/$ARCH" in
    macos/*)     ARCHIVE="xtm-one-relay-macos-universal.tar.gz" ;;
    linux/x64)   ARCHIVE="xtm-one-relay-linux-x64.tar.gz" ;;
    linux/arm64) ARCHIVE="xtm-one-relay-linux-arm64.tar.gz" ;;
esac
step "Installing the XTM One Relay (${OS}/${ARCH})"

command -v curl >/dev/null 2>&1 || die "curl is required."
if command -v sha256sum >/dev/null 2>&1; then
    sha_of() { sha256sum < "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha_of() { shasum -a 256 < "$1" | awk '{print $1}'; }
else
    sha_of() { echo ""; }
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
ARCHIVE_URL=""
SHA_URL=""
SIG_URL=""
PROBE_CODE=""
# The relay version the platform ships, when the archive comes from it.
EXPECTED_VERSION=""
if [ -n "$SERVER" ]; then
    base="${SERVER%/}"
    dl="$base/api/v1/relays/dist/$ARCHIVE"
    PROBE_CODE="$(curl -sIL -o /dev/null -w '%{http_code}' "$dl" 2>/dev/null || true)"
    PROBE_CODE="${PROBE_CODE:-000}"
    case "$PROBE_CODE" in
        200)
            ARCHIVE_URL="$dl"
            SHA_URL="$dl.sha256"
            SIG_URL="$dl.minisig"
            note "Version-matched to the platform via $base"
            EXPECTED_VERSION="$(curl -fsSL --max-filesize 1048576 "$base/api/v1/relays/update-manifest" 2>/dev/null |
                sed -n 's/.*"relay_version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' | head -n 1)" ||
                EXPECTED_VERSION=""
            ;;
        404)     warn "The platform at $base serves no relay build for ${OS}/${ARCH} (HTTP 404: a source/dev build, or relays turned off on this instance)." ;;
        000)     warn "Could not reach $base (DNS, TLS or connection refused). Check the URL and the network path." ;;
        401|403) warn "The platform at $base refused the download (HTTP $PROBE_CODE): an auth proxy or WAF may be blocking /api/v1/relays/dist." ;;
        5*)      warn "The platform at $base answered HTTP $PROBE_CODE: reachable but not serving downloads right now. Retry, or check the backend and the proxy." ;;
        *)       warn "Unexpected HTTP $PROBE_CODE from $base while resolving the relay archive." ;;
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
    die "No download source: pass --server <your XTM One URL> or --download-base <mirror URL>."
fi
case "$ARCHIVE_URL" in
    [Hh][Tt][Tt][Pp]://*)
        warn "Plain HTTP download ($ARCHIVE_URL): the binary and its .sha256 travel unencrypted. Use an https:// URL." ;;
esac

# ── 3. Download + verify ─────────────────────────────────────────────────────
WORK="$(mktemp -d "${TMPDIR:-/tmp}/xtm-one-relay.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
# The verifier block can also use an installed runner; a relay host has none.
INSTALLED_RUNNER="/nonexistent/xtm-one-runner"
if [ -n "${XTM_RELAY_MINISIGN_PUBKEY:-}" ]; then
    RELEASE_PUBKEYS="$(key_line "$XTM_RELAY_MINISIGN_PUBKEY" "$WORK/key")" ||
        die "Refusing to install: XTM_RELAY_MINISIGN_PUBKEY is not a minisign public key."
    RELEASE_TIMESTAMP_FLOOR=0
    warn "XTM_RELAY_MINISIGN_PUBKEY replaces the release keys this script carries: signatures are checked against key ID $(key_id_of "$RELEASE_PUBKEYS")."
fi
SIG_VERIFIER=""
if ! select_verifier "$WORK" && [ "$REQUIRE_SIGNATURE" -eq 1 ]; then
    die "Refusing to install an unverified binary: neither minisign nor OpenSSL 3 (Ed25519 + BLAKE2b) on this host can check the release signature (--require-signature)."
fi
TARBALL="$WORK/$ARCHIVE"
step "Downloading $ARCHIVE_URL"
curl -fSL "$ARCHIVE_URL" -o "$TARBALL"

actual="$(sha_of "$TARBALL")"
UNVERIFIED=""
expected=""
if [ -z "$actual" ]; then
    UNVERIFIED="no sha256sum or shasum on this host"
else
    SHA_FILE="$WORK/$ARCHIVE.sha256"
    sha_code="$(curl -sSL -o "$SHA_FILE" -w '%{http_code}' "$SHA_URL" 2>/dev/null || true)"
    if [ "$sha_code" != "200" ]; then
        UNVERIFIED="no .sha256 could be fetched from $SHA_URL (HTTP ${sha_code:-000})"
    else
        expected="$(awk '{ gsub(/\r/, "") } NF { print tolower($1); exit }' "$SHA_FILE")"
        if [ -z "$expected" ]; then
            UNVERIFIED="the .sha256 at $SHA_URL is empty"
        else
            case "$expected" in
                *[!0-9a-f]*) die "Refusing to install: the .sha256 at $SHA_URL does not hold a SHA-256 digest." ;;
            esac
            [ "${#expected}" -eq 64 ] || die "Refusing to install: the .sha256 at $SHA_URL does not hold a SHA-256 digest."
        fi
    fi
fi
if [ -z "$UNVERIFIED" ]; then
    [ "$expected" = "$actual" ] || die "SHA-256 mismatch for $ARCHIVE (expected $expected, got $actual)."
    ok "SHA-256 verified."
elif [ "$REQUIRE_CHECKSUM" -eq 1 ]; then
    die "Refusing to install an unverified binary: $UNVERIFIED (--require-checksum)."
else
    warn "INTEGRITY NOT VERIFIED: $UNVERIFIED."
fi

UNSIGNED=""
if [ -z "$SIG_VERIFIER" ]; then
    UNSIGNED="neither minisign nor OpenSSL 3 (Ed25519 + BLAKE2b) on this host can check the release signature"
else
    SIG_FILE="$WORK/$ARCHIVE.minisig"
    sig_rc=0
    sig_code="$(curl -sSL --max-filesize 65536 -o "$SIG_FILE" -w '%{http_code}' "$SIG_URL" 2>/dev/null)" || sig_rc=$?
    if [ "$sig_code" != "200" ]; then
        UNSIGNED="no .minisig could be fetched from $SIG_URL (HTTP ${sig_code:-000})"
    elif [ "$sig_rc" -ne 0 ] || [ ! -s "$SIG_FILE" ] || [ "$(size_of "$SIG_FILE")" -gt 65536 ]; then
        UNSIGNED="the .minisig at $SIG_URL could not be read as a signature"
    elif ! minisig_text "$SIG_FILE" "$WORK/signature.txt"; then
        UNSIGNED="the .minisig at $SIG_URL is not a minisign signature"
    elif ! verify_signature "$TARBALL" "$WORK/signature.txt" "$RELEASE_PUBKEYS" "$WORK/verify"; then
        case "$SIG_REASON" in
            "not a minisign signature") UNSIGNED="the .minisig at $SIG_URL is not a minisign signature" ;;
            legacy) die "Refusing to install: the .minisig at $SIG_URL is a legacy (not prehashed) minisign signature, which the release never makes." ;;
            *) die "Refusing to install: the signature at $SIG_URL does not verify against the release key ($SIG_REASON)." ;;
        esac
    elif ! SIGNED_FILE="$(signed_field file "$WORK/signature.txt")" ||
        ! SIGNED_VERSION="$(signed_field version "$WORK/signature.txt")" ||
        ! SIGNED_AT="$(signed_field timestamp "$WORK/signature.txt")"; then
        die "Refusing to install: the trusted comment of the signature at $SIG_URL could not be read."
    elif [ -z "$SIGNED_FILE" ]; then
        UNSIGNED="the trusted comment of the signature at $SIG_URL names no file"
    elif [ "$SIGNED_FILE" != "$ARCHIVE" ]; then
        die "Refusing to install: the signature at $SIG_URL is not for $ARCHIVE (its trusted comment names $SIGNED_FILE)."
    elif [ -n "$SIGNED_VERSION" ] && [ -n "$EXPECTED_VERSION" ] && [ "$SIGNED_VERSION" != "$EXPECTED_VERSION" ]; then
        die "Refusing to install: the signature at $SIG_URL is for relay $SIGNED_VERSION, not $EXPECTED_VERSION, the version $SERVER ships."
    elif [ "$RELEASE_TIMESTAMP_FLOOR" -gt 0 ] && ! signed_since "$SIGNED_AT" "$RELEASE_TIMESTAMP_FLOOR"; then
        die "Refusing to install: the signature at $SIG_URL is older than the oldest release signature this script accepts."
    else
        ok "Release signature verified ($SIG_VERIFIER, key ID $(key_id_of "$SIG_KEY"))."
    fi
fi
if [ -n "$UNSIGNED" ]; then
    if [ "$REQUIRE_SIGNATURE" -eq 1 ]; then
        die "Refusing to install an unverified binary: $UNSIGNED (--require-signature)."
    fi
    warn "SIGNATURE NOT VERIFIED: $UNSIGNED."
fi

# ── 4. Install ───────────────────────────────────────────────────────────────
tar -xzf "$TARBALL" -C "$WORK"
[ -f "$WORK/xtm-one-relay" ] || die "The archive did not contain the xtm-one-relay binary."
mkdir -p "$INSTALL_DIR"
BIN="$INSTALL_DIR/xtm-one-relay"
mv -f "$WORK/xtm-one-relay" "$BIN.new"
chmod 755 "$BIN.new"
mv -f "$BIN.new" "$BIN"
mkdir -p /usr/local/bin
ln -sf "$BIN" /usr/local/bin/xtm-one-relay
ok "Installed to $BIN"

# The service account (Linux): it owns the configuration directory and the
# install directory, so the relay can update itself and nothing else.
if [ "$OS" = "linux" ] && [ "$NO_SERVICE" -eq 0 ] && [ -n "$SERVICE_USER" ]; then
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        if command -v useradd >/dev/null 2>&1; then
            useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
        elif command -v adduser >/dev/null 2>&1; then
            adduser -S -D -H -s /sbin/nologin "$SERVICE_USER"
        else
            die "Cannot create the $SERVICE_USER account (no useradd or adduser). Create it, or pass --user root."
        fi
        ok "Created the $SERVICE_USER system account."
    fi
    chown -R "$SERVICE_USER": "$INSTALL_DIR"
fi

# ── 5. Configuration ─────────────────────────────────────────────────────────
toml_string() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }
CONFIG_DIR="$(dirname "$CONFIG")"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
    {
        echo "# XTM One Relay - see 'xtm-one-relay check-config'. Reloaded when it changes."
        [ -n "$SERVER" ] && echo "server_url = $(toml_string "${SERVER%/}")"
        [ -n "$NAME" ] && echo "name = $(toml_string "$NAME")"
        printf 'allowed_destinations = ['
        first=1
        items=()
        IFS=', ' read -r -a items <<< "$DESTINATIONS" || true
        for item in ${items[@]+"${items[@]}"}; do
            [ -n "$item" ] || continue
            [ "$first" -eq 1 ] || printf ', '
            printf '%s' "$(toml_string "$item")"
            first=0
        done
        printf ']\n'
    } > "$CONFIG"
    chmod 600 "$CONFIG"
    ok "Wrote $CONFIG"
    [ -n "$DESTINATIONS" ] || warn "The relay's allow-list is empty: it reaches nothing until you list destinations in $CONFIG."
elif [ -n "$DESTINATIONS" ]; then
    note "$CONFIG exists and was kept: edit allowed_destinations there to change what the relay reaches."
fi

# ── 6. Enroll ────────────────────────────────────────────────────────────────
ENROLLED=0
if [ -n "$SERVER" ] && [ -n "$KEY" ]; then
    step "Enrolling with $SERVER"
    set -- --config "$CONFIG" enroll --server-url "${SERVER%/}" --key "$KEY" --force
    [ -n "$NAME" ] && set -- "$@" --name "$NAME"
    "$BIN" "$@"
    ENROLLED=1
    ok "Enrolled."
fi

# ── 7. System service ────────────────────────────────────────────────────────
if [ "$NO_SERVICE" -eq 0 ]; then
    if [ "$ENROLLED" -eq 1 ] || "$BIN" --config "$CONFIG" status >/dev/null 2>&1 || [ -f "$CONFIG_DIR/state.json" ]; then
        step "Installing the system service"
        if [ "$OS" = "linux" ] && [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != "root" ]; then
            "$BIN" --config "$CONFIG" service install --user "$SERVICE_USER"
            systemctl restart xtm-one-relay >/dev/null 2>&1 || true
        else
            "$BIN" --config "$CONFIG" service install
            if [ "$OS" = "linux" ]; then systemctl restart xtm-one-relay >/dev/null 2>&1 || true; fi
        fi
        ok "Service installed and started."
    else
        note "Not enrolled yet: the service is installed once you enroll (re-run with --server and --key)."
    fi
fi

echo ""
if [ -n "$UNVERIFIED" ]; then
    printf '\033[33mXTM One Relay installed WITHOUT integrity verification (%s).\033[0m\n' "$UNVERIFIED"
fi
if [ -n "$UNSIGNED" ]; then
    printf '\033[33mXTM One Relay installed WITHOUT signature verification (%s).\033[0m\n' "$UNSIGNED"
fi
if [ -z "$UNVERIFIED" ] && [ -z "$UNSIGNED" ]; then
    printf '\033[32mXTM One Relay installed.\033[0m\n'
fi
if [ "$ENROLLED" -eq 1 ]; then
    sleep 3
    "$BIN" --config "$CONFIG" status || note "It may take a few seconds to connect: check again with 'xtm-one-relay status'."
else
    printf '\033[33mNext step - enroll it (copy the command from Relays > a group > Enroll a relay in XTM One):\033[0m\n'
    echo   "  sudo xtm-one-relay enroll --server-url <YOUR_XTM_ONE_URL> --key <rlenr-...>"
fi
