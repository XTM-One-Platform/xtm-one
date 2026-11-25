"""Regression: a Langfuse host that keeps refusing the auth check never pins a thread.

Every failed init used to shut the client down. The SDK keeps one resource
manager per public key for the whole process, and a shut-down one cannot be
revived: from the third failed attempt on, ``shutdown()`` waited forever in
``flush()`` on a media queue no consumer drained. Each retry (one per cooldown)
pinned another default-executor thread, until the pool was full and every DNS
lookup, S3 call and bcrypt check queued behind them: the database pool
exhausted on the platform, the worker's reconciler unable to reconnect.

Runs the real SDK in a child process, because it installs a process-wide
OpenTelemetry tracer provider and keeps its threads until exit.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

_CHILD = textwrap.dedent(
    """
    import json, threading, time
    from app.services.langfuse_tracing import _blocking_init_and_auth

    def attempt():
        try:
            # Port 9 on loopback refuses the connection: the auth check fails fast.
            _blocking_init_and_auth("pk-lf-thread-regression", "sk-lf-x", "http://127.0.0.1:9")
        except Exception:
            pass

    outcomes = []
    for _ in range(5):
        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(15)
        outcomes.append("returned" if not worker.is_alive() else "stuck")
    print(json.dumps(outcomes))
    """
)


def test_repeated_failed_inits_all_return():
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    outcomes = json.loads(done.stdout.strip().splitlines()[-1])
    assert outcomes == ["returned"] * 5
