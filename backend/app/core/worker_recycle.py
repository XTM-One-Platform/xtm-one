# SPDX-License-Identifier: Apache-2.0

"""The web worker's ``MAX_REQUESTS`` recycle, never in the middle of an agent turn.

uvicorn's own recycle (``--limit-max-requests``) stops a worker the moment it
has served its quota: it closes the worker's WebSockets, waits
``GRACEFUL_TIMEOUT`` and cancels every request still running. A web chat turn
runs inside its send's SSE request, so a turn with more than that left would be
cut while the pod stayed up, and say "a server restart" nobody else saw.

``start-uvicorn.sh`` therefore hands the recycle to the app
(:data:`RECYCLE_REQUESTS_ENV` and its two siblings) instead of passing the flag.
The worker counts its HTTP requests as uvicorn did, the outermost middleware
seeing every one, probes included (:meth:`WorkerRecycler.record_request`).
Past its quota (``MAX_REQUESTS`` plus a jitter drawn once per worker, up to
``MAX_REQUESTS_JITTER``) it keeps serving until no agent turn runs in it
(``worker_shutdown.agent_work_in_flight``), at most ``MAX_REQUESTS_DEFER_SECONDS``,
then sends itself SIGTERM: uvicorn's own shutdown, after which the supervisor
starts a replacement exactly as it did after uvicorn's recycle. A worker
started any other way (``--reload``, a bare ``uvicorn``) recycles nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.core import worker_shutdown

logger = logging.getLogger("app.worker_recycle")

#: The validated ``MAX_REQUESTS`` the launcher hands the app; unset or 0: no recycle.
RECYCLE_REQUESTS_ENV = "XTM_ONE_RECYCLE_REQUESTS"
#: The validated ``MAX_REQUESTS_JITTER``.
RECYCLE_JITTER_ENV = "XTM_ONE_RECYCLE_JITTER"
#: The validated ``MAX_REQUESTS_DEFER_SECONDS``.
RECYCLE_DEFER_ENV = "XTM_ONE_RECYCLE_DEFER_SECONDS"

#: How long a worker past its quota waits for its agent turns by default: the
#: launcher's ``MAX_REQUESTS_DEFER_SECONDS`` default.
DEFAULT_DEFER_SECONDS = 14400
#: How often the watcher looks at a worker past its quota.
WATCH_INTERVAL_SECONDS = 1.0

#: The worker recycles: nothing runs in it.
IDLE = "idle"
#: The worker recycles: its turns outlived ``MAX_REQUESTS_DEFER_SECONDS``.
OVERDUE = "overdue"

_worker_started_at = time.monotonic()


def _count(raw: str | None) -> int | None:
    """A non-negative integer as the launcher writes it, ``None`` for anything else."""
    value = (raw or "").strip()
    if not value.isascii() or not value.isdigit():
        return None
    return int(value)


@dataclass(frozen=True)
class RecyclePolicy:
    requests: int
    jitter: int
    defer_seconds: int


def policy_from_env(environ: Mapping[str, str] | None = None) -> RecyclePolicy | None:
    """The recycle the launcher handed over, or ``None`` when it handed none."""
    env = os.environ if environ is None else environ
    requests = _count(env.get(RECYCLE_REQUESTS_ENV))
    if not requests:
        return None
    jitter = _count(env.get(RECYCLE_JITTER_ENV))
    defer = _count(env.get(RECYCLE_DEFER_ENV))
    return RecyclePolicy(
        requests=requests,
        jitter=jitter if jitter is not None else requests // 2,
        defer_seconds=defer if defer is not None else DEFAULT_DEFER_SECONDS,
    )


class WorkerRecycler:
    """Counts this worker's requests and recycles it once it is due and idle."""

    def __init__(
        self,
        policy: RecyclePolicy | None,
        *,
        draw: Callable[[int, int], int] = random.randint,
    ) -> None:
        self.policy = policy
        self.seen = 0
        #: This worker's quota: the base plus its own jitter, ``0`` = never.
        self.limit = policy.requests + draw(0, policy.jitter) if policy else 0
        #: ``time.monotonic()`` when the quota was reached.
        self.due_at: float | None = None

    def record_request(self) -> None:
        self.seen += 1
        policy = self.policy
        if policy is None or self.due_at is not None or self.seen < self.limit:
            return
        self.due_at = time.monotonic()
        busy = worker_shutdown.agent_work_in_flight()
        logger.warning(
            "Web worker (pid %d) served its %d requests (MAX_REQUESTS %d plus its "
            "jitter) after %.0f s: it recycles %s. The pod stays up; the supervisor "
            "starts a replacement",
            os.getpid(),
            self.seen,
            policy.requests,
            time.monotonic() - _worker_started_at,
            "now, no agent turn runs in it"
            if not busy
            else f"once its {busy} running agent turn(s) end, at most "
            f"{policy.defer_seconds} s from now (MAX_REQUESTS_DEFER_SECONDS)",
        )

    def decision(self, now: float, busy: int) -> str | None:
        """:data:`IDLE` or :data:`OVERDUE` when the worker recycles now, else ``None``."""
        if self.due_at is None or self.policy is None:
            return None
        if busy == 0:
            return IDLE
        # Against the deadline, not the elapsed time: ``(due_at + d) - due_at``
        # can round below ``d`` and miss the deadline it is called at.
        if now >= self.due_at + self.policy.defer_seconds:
            return OVERDUE
        return None

    async def watch(self, interval: float = WATCH_INTERVAL_SECONDS) -> None:
        """Recycle the worker once it is due; returns after it signalled itself."""
        while True:
            await asyncio.sleep(interval)
            verdict = self.decision(time.monotonic(), worker_shutdown.agent_work_in_flight())
            if verdict is not None:
                self.recycle(verdict)
                return

    def recycle(self, verdict: str) -> None:
        defer_seconds = self.policy.defer_seconds if self.policy is not None else 0
        worker_shutdown.note_shutdown(worker_shutdown.RECYCLE, f"{self.seen} requests")
        if verdict == IDLE:
            logger.warning(
                "Web worker (pid %d) recycles now: %d requests served, no agent turn "
                "runs in it",
                os.getpid(),
                self.seen,
            )
        else:
            turns = worker_shutdown.running_turns()
            logger.warning(
                "Web worker (pid %d) recycles with agent work still running: its "
                "turns outlived MAX_REQUESTS_DEFER_SECONDS (%d s) after its %d "
                "requests; the drain cuts the chat turn(s) [%s] and %d background "
                "turn(s) that do not end within GRACEFUL_TIMEOUT",
                os.getpid(),
                defer_seconds,
                self.seen,
                worker_shutdown.describe_turns(turns),
                worker_shutdown.agent_work_in_flight() - len(turns),
            )
        os.kill(os.getpid(), signal.SIGTERM)


recycler = WorkerRecycler(policy_from_env())


def start_recycle_watch() -> asyncio.Task | None:
    """Start the watcher of a worker the launcher handed its recycle to."""
    if recycler.policy is None:
        return None
    return asyncio.create_task(recycler.watch(), name="worker-recycle-watch")


def log_worker_shutdown_summary() -> None:
    """The WARNING every web worker exit leaves, with what stopped it (lifespan teardown)."""
    request = worker_shutdown.shutdown_request()
    if request is None:
        reason = "no recycle or stop signal recorded"
    elif request.reason == worker_shutdown.RECYCLE:
        reason = f"recycled after {request.detail} (MAX_REQUESTS)"
    else:
        reason = f"asked to stop by {request.detail}"
    logger.warning(
        "Web worker (pid %d) shutting down after %.0f s uptime, %d HTTP requests "
        "received: %s; %d agent turn(s) still running",
        os.getpid(),
        time.monotonic() - _worker_started_at,
        recycler.seen,
        reason,
        worker_shutdown.agent_work_in_flight(),
    )
