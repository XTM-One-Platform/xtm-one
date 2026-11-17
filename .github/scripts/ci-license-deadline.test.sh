#!/usr/bin/env bash
# Tests ci-license-deadline.sh on certificates made here: the deadline is
# notBefore + 365 days, whatever the certificate's notAfter.
set -euo pipefail

script="$(dirname "$0")/ci-license-deadline.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
failures=0

cert() { # cert <file> <validity in days>
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$tmp/key.pem" -out "$1" \
    -days "$2" -subj "/CN=ci-license-deadline-test" 2>/dev/null
}

expect() { # expect <name> <pem> <today> <deadline> <days_left>
  local got
  got="$(CI_LICENSE_NOW="$3" bash "$script" "$2" | tr '\n' ' ')"
  if [ "$got" = "deadline=$4 days_left=$5 " ]; then
    echo "ok   $1"
  else
    echo "FAIL $1: got '$got', expected 'deadline=$4 days_left=$5'"
    failures=$((failures + 1))
  fi
}

cert "$tmp/two-years.pem" 730
cert "$tmp/ninety-days.pem" 90
start="$(python3 - "$tmp/two-years.pem" <<'PY'
import subprocess, sys
from datetime import datetime
out = subprocess.check_output(["openssl", "x509", "-in", sys.argv[1], "-noout", "-startdate"], text=True)
print(datetime.strptime(" ".join(out.split("=", 1)[1].split()), "%b %d %H:%M:%S %Y %Z").date().isoformat())
PY
)"
day() { python3 -c "from datetime import date, timedelta; print((date.fromisoformat('$start') + timedelta(days=$1)).isoformat())"; }

expect "a one-year window caps a longer certificate" "$tmp/two-years.pem" "$start" "$(day 365)" 365
expect "29 days left" "$tmp/two-years.pem" "$(day 336)" "$(day 365)" 29
expect "the deadline day" "$tmp/two-years.pem" "$(day 365)" "$(day 365)" 0
expect "expired" "$tmp/two-years.pem" "$(day 366)" "$(day 365)" -1
expect "a shorter notAfter does not shorten it: the platform ignores it" "$tmp/ninety-days.pem" "$start" "$(day 365)" 365

if ! bash "$script" "$tmp/missing.pem" >/dev/null 2>&1; then
  echo "ok   a missing file fails"
else
  echo "FAIL a missing file did not fail"
  failures=$((failures + 1))
fi

[ "$failures" -eq 0 ]
