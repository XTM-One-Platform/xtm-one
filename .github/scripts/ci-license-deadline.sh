#!/usr/bin/env bash
# Prints when the `ci` certificate in the PEM file $1 stops working, as
# `deadline=YYYY-MM-DD` and `days_left=N` lines (for $GITHUB_OUTPUT).
#
# The platform ends a `ci` license at min(platform created_at + 45 min,
# notBefore + 365 days) (backend/app/enterprise/licensing.py): it replaces the
# certificate's notAfter, which a `ci` license never reads (OpenCTI's own CI
# certificate has a notAfter one day after its notBefore). The deadline is
# therefore notBefore + 365 days.
# days_left is negative once it has passed. CI_LICENSE_NOW (YYYY-MM-DD) replaces
# today, for the tests (ci-license-deadline.test.sh).
set -euo pipefail

pem="${1:?usage: ci-license-deadline.sh <certificate.pem>}"
not_before="$(openssl x509 -in "$pem" -noout -startdate | cut -d= -f2)"

NOT_BEFORE="$not_before" python3 - <<'PY'
import os
from datetime import date, datetime, timedelta, timezone


def parse(value: str) -> datetime:
    # openssl prints "Oct 26 12:00:00 2026 GMT" (day padded with a space)
    return datetime.strptime(" ".join(value.split()), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


deadline = (parse(os.environ["NOT_BEFORE"]) + timedelta(days=365)).date()
today = date.fromisoformat(os.environ["CI_LICENSE_NOW"]) if os.environ.get("CI_LICENSE_NOW") else datetime.now(timezone.utc).date()
print(f"deadline={deadline.isoformat()}")
print(f"days_left={(deadline - today).days}")
PY
