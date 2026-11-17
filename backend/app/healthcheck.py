# SPDX-License-Identifier: Apache-2.0

"""Process health checks runnable as ``python -m app.healthcheck <check>``.

The orchestration reconciler runs inside the SAQ worker, which has no HTTP
server, so an httpGet liveness probe is impossible. Instead the reconciler
touches a per-pod heartbeat file every cycle (see
``orchestration_reconciler._write_heartbeat``) and the worker Deployment's
``livenessProbe`` runs ``python -m app.healthcheck reconciler``: this exits
non-zero when the heartbeat is missing or stale, so Kubernetes restarts a
dead/hung worker instead of leaving runs and background tasks stuck on
``pending`` with no recovery.

Kept dependency-light on purpose — it imports only ``app.core.config`` (no DB,
Redis, or network), so the probe is fast and cannot itself wedge.
"""

from __future__ import annotations

import os
import sys
import time

from app.core.config import settings


def _out(msg: str) -> None:
    sys.stdout.write(msg + "\n")


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def check_reconciler() -> int:
    """Exit 0 when the reconciler heartbeat is fresh, 1 when stale/missing."""
    path = settings.reconciler_heartbeat_path
    max_age = settings.reconciler_heartbeat_max_age_seconds
    if not path:
        # Heartbeat disabled — treat as healthy (operator opted out).
        _out("reconciler heartbeat disabled; skipping check")
        return 0
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _err(f"reconciler heartbeat missing at {path}")
        return 1
    age = time.time() - mtime
    if age > max_age:
        _err(f"reconciler heartbeat stale: {age:.0f}s old (> {max_age}s)")
        return 1
    _out(f"reconciler heartbeat ok ({age:.0f}s old)")
    return 0


_CHECKS = {"reconciler": check_reconciler}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = argv[0] if argv else "reconciler"
    fn = _CHECKS.get(check)
    if fn is None:
        _err(f"unknown check '{check}'; known checks: {', '.join(sorted(_CHECKS))}")
        return 2
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
