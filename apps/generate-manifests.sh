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

# generate-manifests.sh — Generate the Office add-in manifests from templates.
#
# The Teams app ships no package here: the Setup Guide of the Teams channel
# gives a manifest with placeholders (docs/docs/administration/channels/microsoft-teams.md).
#
# App name resolution (first wins), as the platform's branding
# (backend/tests/test_generate_manifests.py holds the two together):
#   1. PLATFORM_NAME env var, trimmed, if not blank  (e.g.  PLATFORM_NAME="Acme AI")
#   2. PLATFORM_MODE=copilot or dev   →  "Filigran Copilot"
#   3. Otherwise (xtm_one, the platform's default, or ai_disabled)  →  "XTM One"
# PLATFORM_MODE is trimmed and lowercased as the platform reads it, and a value
# the platform would refuse stops the script.
#
# Usage:
#   ./apps/generate-manifests.sh
#   PLATFORM_MODE=copilot ./apps/generate-manifests.sh
#   PLATFORM_NAME="Acme AI" ./apps/generate-manifests.sh

set -euo pipefail

# ── Resolve APP_NAME ──────────────────────────────────────────────────────────
# Leading and trailing whitespace out, a CR from a CRLF .env included.
trim() {
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    printf '%s' "$v"
}

PLATFORM_NAME_TRIMMED="$(trim "${PLATFORM_NAME:-}")"
PLATFORM_MODE_NORMALIZED="$(trim "${PLATFORM_MODE:-}" | tr '[:upper:]' '[:lower:]')"
case "${PLATFORM_MODE_NORMALIZED:-xtm_one}" in
    xtm_one | ai_disabled | copilot | dev) ;;
    *)
        echo "PLATFORM_MODE must be one of xtm_one|copilot|dev|ai_disabled, got '${PLATFORM_MODE}'" >&2
        exit 1
        ;;
esac

if [[ -n "$PLATFORM_NAME_TRIMMED" ]]; then
    APP_NAME="$PLATFORM_NAME_TRIMMED"
elif [[ "$PLATFORM_MODE_NORMALIZED" == "copilot" || "$PLATFORM_MODE_NORMALIZED" == "dev" ]]; then
    APP_NAME="Filigran Copilot"
else
    APP_NAME="XTM One"
fi

echo "Generating manifests with APP_NAME='${APP_NAME}'"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── XML escaping ─────────────────────────────────────────────────────────────
# Custom platform names can contain XML-sensitive characters (e.g.
# `Acme & Co`, `Acme "Edge"`, a backslash, or sed's `|` delimiter).  We
# escape *first* for XML, then escape the result so it is
# safe on the replacement side of a sed `s|||` command.

# XML-escape: &, <, >, ", '
xml_escape() {
    python3 -c "
import sys, xml.sax.saxutils as s
v = sys.argv[1]
sys.stdout.write(s.escape(v, {'\"': '&quot;', \"'\": '&apos;'}))
" "$1"
}

# Escape a value for the replacement side of sed's s|||.  Escapes
# backslash, the chosen delimiter (|), the special replacement char (&),
# and embedded newlines.
sed_escape_replacement() {
    python3 -c "
import sys
v = sys.argv[1]
v = v.replace('\\\\', '\\\\\\\\').replace('|', '\\\\|').replace('&', '\\\\&')
v = v.replace('\\n', '\\\\n')
sys.stdout.write(v)
" "$1"
}

# Pre-compute the escaped + sed-safe replacement once.
APP_NAME_XML_SED="$(sed_escape_replacement "$(xml_escape "$APP_NAME")")"

# ── Portable sed -i (macOS requires an empty string suffix) ───────────────────
sed_inplace() {
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "$@"
    else
        sed -i "$@"
    fi
}

# Copy a template to its output path and substitute {{APP_NAME}} with the
# XML-escaped, sed-safe replacement.
#
# $1 = source template path
# $2 = destination path
apply_template() {
    local src="$1"
    local dst="$2"
    cp "$src" "$dst"
    sed_inplace "s|{{APP_NAME}}|${APP_NAME_XML_SED}|g" "$dst"
    echo "  → $(basename "$dst")"
}

# ── Office (production — Word / Excel / PowerPoint) ───────────────────────────
apply_template \
    "$SCRIPT_DIR/microsoft-office/manifest/manifest.prod.template.xml" \
    "$SCRIPT_DIR/microsoft-office/manifest/manifest.prod.xml"

# ── Office (Outlook) ──────────────────────────────────────────────────────────
apply_template \
    "$SCRIPT_DIR/microsoft-office/manifest/manifest.outlook.template.xml" \
    "$SCRIPT_DIR/microsoft-office/manifest/manifest.outlook.xml"

# ── Office (dev — localhost) ──────────────────────────────────────────────────
apply_template \
    "$SCRIPT_DIR/microsoft-office/manifest/manifest.dev.template.xml" \
    "$SCRIPT_DIR/microsoft-office/manifest/manifest.xml"

echo ""
echo "Done."
