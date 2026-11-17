#!/usr/bin/env bash
# Prints the URL of the public `ci` certificate: the first non-comment line of
# .github/ci-license-url (or of $CI_LICENSE_URL_FILE), nothing while the file
# holds none, which makes the end-to-end suites use the secret instead.
set -euo pipefail

file="${CI_LICENSE_URL_FILE:-$(dirname "$0")/../ci-license-url}"
if [ -f "$file" ]; then
  sed -e '/^[[:space:]]*#/d' "$file" | tr -d '[:space:]'
fi
