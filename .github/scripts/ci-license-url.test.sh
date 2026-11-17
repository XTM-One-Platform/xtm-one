#!/usr/bin/env bash
# Tests ci-license-url.sh: comments and blanks are ignored, a missing file or
# one without a URL prints nothing.
set -euo pipefail

script="$(dirname "$0")/ci-license-url.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

expect() { # expect <name> <file> <expected output>
  local got
  got="$(CI_LICENSE_URL_FILE="$2" bash "$script")"
  if [ "$got" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1: got '$got', expected '$3'"
    failures=$((failures + 1))
  fi
}

printf '# comment\n\n' > "$tmp/empty"
printf '# comment\n  https://example.com/ci.crt  \n' > "$tmp/url"
expect "comments only" "$tmp/empty" ""
expect "a URL after comments, trimmed" "$tmp/url" "https://example.com/ci.crt"
expect "a missing file" "$tmp/missing" ""

[ "$failures" -eq 0 ]
