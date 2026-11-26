# SPDX-License-Identifier: Apache-2.0

"""Purge every CTEM signal, cascading to the cases they opened or joined (#2528).

When the scope threshold was set wrong, the Signals list fills with thousands of
signals and too many cases are opened. An administrator starts over with one job,
run in a strict order, one job at a time on the platform:

1. **Pause intake.** :func:`pause_intake` sets one flag that
   ``ctem_signals.record_signal`` reads: while it is set every source (the OpenCTI
   live trigger and PIR playbook, the OpenAEV, Aikido, GitHub and GitLab
   deliveries, webhook sources, every scheduled pass, the API and the agent tools)
   is refused with :class:`ctem_signals.SignalIntakePausedError`, so nothing records a
   signal; the scheduled recheck and case sweep skip their tick.
2. **Purge.** The cases a signal opened or joined (a signal names it, or its
   history or members say a signal did) are deleted :data:`CASE_BATCH` at a time
   through ``ctem_case_delete.delete_cases``: members, history, open work items,
   armed timers, coverage, one tombstone audit row per case, no orphan. Then every
   signal, :data:`SIGNAL_BATCH` per transaction. Then the "already raised" state
   kept outside ``ctem_signals``: the attack surface pass's
   ``ctem_surface_observations.signalled`` keys (the baseline hosts stay), so the
   next pass raises the current outside-lens differences again (paths 5, 6, 7).
   No other source keeps raised state outside the signal row: each dedupes
   through the signal upsert, which the purge empties.
3. **Resync**, only when the administrator asked for it (``ctem_signal_resync``).
4. **Resume intake**, always, also when step 2 or 3 failed; the failure is
   reported in the job status and the audit row.

Sources and their configuration (trigger paths, webhook sources, exclusions) and
the case-opening switch are never touched. One audit row for the job: the actor,
the resync choice and the counts per step. ``dev-docs/ctem-signals.md`` section
"Purge every signal" is the contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.ctem_surface_observation import CtemSurfaceObservation

logger = logging.getLogger(__name__)

#: The word the administrator types to start the purge.
CONFIRM_WORD = "PURGE"
#: Cases one delete transaction removes (``ctem_case_delete.MAX_CASES``).
CASE_BATCH = 200
#: Signals one delete transaction removes.
SIGNAL_BATCH = 1000
#: How often one case batch is tried again when a recompute held its signal rows.
LOCK_RETRIES = 3
#: The Redis key of the intake pause; its TTL ends a pause a lost pod never lifted.
PAUSE_KEY = "ctem:signals:intake-paused"
PAUSE_TTL_SECONDS = 2 * 3600
#: The Redis key that keeps a second job out, and the one that holds the job status.
JOB_LOCK_KEY = "ctem:signals:purge-job"
STATUS_KEY = "ctem:signals:purge-status"
STATUS_TTL_SECONDS = 7 * 24 * 3600
#: The steps of a job, in order.
STEPS: tuple[str, ...] = ("pause", "purge", "resync", "resume")
#: What a second job is answered while one runs.
BUSY_DETAIL = "A purge of the signals is already running: wait for it to end."
#: The platform event of a finished job (live refresh of the Signals and Cases pages).
PURGE_FINISHED = "ctem_signal.purge_finished"

#: The pause and the job of this process, read first (a Redis outage never lets intake through mid-purge here).
_LOCAL: dict[str, Any] = {"paused": False, "status": None}
_JOBS: set[asyncio.Task[Any]] = set()
#: Set by the job for its own resync step: ``record_signal`` records through the pause there only.
RESYNC_BYPASS: ContextVar[bool] = ContextVar("ctem_signal_purge_resync", default=False)


class PurgeBusyError(RuntimeError):
    """A purge job already runs."""


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


# ── the intake pause ────────────────────────────────────────────────────


async def intake_paused() -> bool:
    """True while a purge job holds signal intake paused (this process, or any pod through Redis).

    Fails open on a Redis error: an unreachable store never stops intake for good.
    """
    if _LOCAL["paused"]:
        return True
    try:
        client = await _redis()
        return bool(await client.exists(PAUSE_KEY))
    except Exception:
        return False


async def pause_intake() -> None:
    _LOCAL["paused"] = True
    try:
        client = await _redis()
        await client.set(PAUSE_KEY, "1", ex=PAUSE_TTL_SECONDS)
    except Exception:
        logger.warning("CTEM signal purge: intake pause not shared through Redis", exc_info=True)


async def resume_intake() -> None:
    _LOCAL["paused"] = False
    try:
        client = await _redis()
        await client.delete(PAUSE_KEY)
    except Exception:
        logger.warning("CTEM signal purge: intake pause not lifted in Redis (its TTL ends it)", exc_info=True)


# ── what the purge removes ──────────────────────────────────────────────


def _signal_cases() -> Any:
    """The cases a signal opened or joined: a signal names it, a ``signal`` member, or its history says so."""
    named = select(CtemSignal.case_id).where(CtemSignal.case_id.is_not(None))
    member = exists().where(CtemCaseIssue.case_id == CtemCase.id, CtemCaseIssue.source == "signal")
    history = exists().where(
        CtemCaseEvent.case_id == CtemCase.id,
        or_(CtemCaseEvent.kind == "signal", (CtemCaseEvent.kind == "created") & CtemCaseEvent.reason.like("signal %")),
    )
    return or_(CtemCase.id.in_(named), member, history)


async def counts(db: AsyncSession) -> dict[str, int]:
    """``{signals, cases}``: what a purge would delete now (the dialog's live counts)."""
    signals = await db.scalar(select(func.count()).select_from(CtemSignal))
    cases = await db.scalar(select(func.count()).select_from(CtemCase).where(_signal_cases()))
    return {"signals": int(signals or 0), "cases": int(cases or 0)}


async def _case_batch(db: AsyncSession, actor: Any, *, reason: str) -> int:
    """Delete the next :data:`CASE_BATCH` signal cases; how many. The caller commits."""
    from app.services import ctem_case_delete as case_delete

    rows = list(
        (
            await db.execute(select(CtemCase).where(_signal_cases()).order_by(CtemCase.created_at).limit(CASE_BATCH))
        ).scalars()
    )
    if not rows:
        return 0
    # The released signals are deleted next: never handed to the scope gate (no hand_off_released).
    await case_delete.delete_cases(db, rows, actor=actor, reason=reason, dry_run=False)
    return len(rows)


async def purge_cases(sessions: Any, actor_id: uuid.UUID, *, progress: Callable[[int], Awaitable[None]]) -> int:
    """Every signal case deleted, one transaction per batch; the total."""
    from sqlalchemy.exc import DBAPIError

    from app.models.user import User
    from app.services import ctem_case_delete as case_delete

    total = 0
    tries = 0
    while True:
        try:
            async with sessions() as db:
                actor = await db.get(User, actor_id)
                done = await _case_batch(db, actor, reason="Purge of every CTEM signal (#2528)")
                await db.commit()
        except DBAPIError as exc:
            if case_delete.is_lock_wait(exc) and tries < LOCK_RETRIES:
                tries += 1
                await asyncio.sleep(2 * tries)
                continue
            raise
        tries = 0
        if done == 0:
            return total
        total += done
        await progress(total)


async def purge_signals(sessions: Any, *, progress: Callable[[int], Awaitable[None]]) -> int:
    """Every signal deleted, :data:`SIGNAL_BATCH` per transaction; the total."""
    total = 0
    while True:
        async with sessions() as db:
            ids = list((await db.execute(select(CtemSignal.id).limit(SIGNAL_BATCH))).scalars())
            if not ids:
                return total
            await db.execute(
                delete(CtemSignal).where(CtemSignal.id.in_(ids)).execution_options(synchronize_session=False)
            )
            await db.commit()
        total += len(ids)
        await progress(total)


async def clear_raised_state(db: AsyncSession) -> dict[str, int]:
    """The "already raised" state kept outside ``ctem_signals``, cleared; the caller commits.

    The attack surface pass keeps per host the signal keys it raised and still
    holding (``ctem_surface_observations.signalled``): left as is, two identical
    passes after a purge would record nothing and paths 5, 6, 7 would never come
    back. Each held key is undone so the next pass finds its difference again:
    a new port leaves the host's baseline ports, a new host leaves the baseline
    (the next pass decides it new again, with its complete-read check); the keys
    the diff recomputes every pass (answers, dangling, unowned, unseen) only need
    the key gone. Every other host stays as it is.
    """
    from app.services.ctem_attack_surface import SOURCE, host_key

    rows = list(
        (
            await db.execute(
                select(CtemSurfaceObservation).where(func.json_array_length(CtemSurfaceObservation.signalled) > 0)
            )
        ).scalars()
    )
    dropped = 0
    for row in rows:
        marker = f":{SOURCE}:{host_key(row.host)}:"
        details = [key.split(marker, 1)[1] for key in row.signalled or [] if marker in key]
        if "new_host" in details:
            await db.delete(row)
            dropped += 1
            continue
        raised_ports = {d.removeprefix("port:") for d in details if d.startswith("port:")}
        if raised_ports:
            row.ports = [
                p
                for p in row.ports or []
                if not isinstance(p, dict) or f"{p.get('port')}/{p.get('transport') or 'tcp'}" not in raised_ports
            ]
        row.signalled = []
    await db.flush()
    return {"surface_hosts": len(rows), "surface_hosts_dropped": dropped}


# ── the job ─────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def read_status() -> dict[str, Any] | None:
    """The running or last job: ``{id, state, resync, step, steps: {name: {state, counts}}, error, ...}``."""
    try:
        client = await _redis()
        raw = await client.get(STATUS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        logger.info("CTEM signal purge: status read from this process only", exc_info=True)
    return _LOCAL["status"]


async def _write_status(status: dict[str, Any]) -> None:
    _LOCAL["status"] = status
    try:
        client = await _redis()
        await client.set(STATUS_KEY, json.dumps(status), ex=STATUS_TTL_SECONDS)
    except Exception:
        logger.info("CTEM signal purge: status kept in this process only", exc_info=True)


async def _take_job() -> bool:
    if any(not t.done() for t in _JOBS):
        return False
    try:
        client = await _redis()
        return bool(await client.set(JOB_LOCK_KEY, "1", nx=True, ex=PAUSE_TTL_SECONDS))
    except Exception:
        return True  # no Redis: this process's own guard above


async def _release_job() -> None:
    try:
        client = await _redis()
        await client.delete(JOB_LOCK_KEY)
    except Exception:
        logger.info("CTEM signal purge: job lock not released in Redis (its TTL ends it)", exc_info=True)


def new_status(job_id: str, actor: Any, *, resync: bool) -> dict[str, Any]:
    return {
        "id": job_id,
        "state": "running",
        "resync": resync,
        "started_by": getattr(actor, "email", None) or str(getattr(actor, "id", "")),
        "started_at": _now(),
        "finished_at": None,
        "step": "pause",
        "steps": {
            name: {"state": "skipped" if name == "resync" and not resync else "pending", "counts": {}} for name in STEPS
        },
        "error": None,
    }


async def run_job(
    status: dict[str, Any],
    actor_id: uuid.UUID,
    *,
    sessions: Any = None,
    resync: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run one purge job to its end; the final status. Never raises: a failure is the status's ``error``.

    *resync* runs step 3 when the job asked for it (``ctem_signal_resync.resync``);
    it reports its counts per path through the progress callback it is given.
    """
    if sessions is None:
        from app.core.database import async_session as sessions

    async def mark(step: str, state: str, **counts_: Any) -> None:
        status["step"] = step
        status["steps"][step]["state"] = state
        status["steps"][step]["counts"].update(counts_)
        await _write_status(status)

    try:
        await mark("pause", "running")
        await pause_intake()
        await mark("pause", "done")

        await mark("purge", "running")

        async def cases_done(n: int) -> None:
            await mark("purge", "running", cases=n)

        async def signals_done(n: int) -> None:
            await mark("purge", "running", signals=n)

        cases = await purge_cases(sessions, actor_id, progress=cases_done)
        signals = await purge_signals(sessions, progress=signals_done)
        async with sessions() as db:
            raised = await clear_raised_state(db)
            await db.commit()
        await mark("purge", "done", cases=cases, signals=signals, **raised)

        if status["resync"]:
            await mark("resync", "running")

            async def path_done(path: str, recorded: int) -> None:
                status["steps"]["resync"]["counts"].setdefault("paths", {})[path] = recorded
                await _write_status(status)

            if resync is None:
                from app.services.ctem_signal_resync import resync as resync

            token = RESYNC_BYPASS.set(True)
            try:
                result = await resync(sessions=sessions, actor_id=actor_id, progress=path_done)
            finally:
                RESYNC_BYPASS.reset(token)
            await mark("resync", "done", **result)
    except Exception as exc:
        from app.core.utils import format_exception_message

        logger.error("CTEM signal purge job %s failed", status["id"], exc_info=True)
        failed = status["step"]
        status["steps"][failed]["state"] = "failed"
        status["error"] = f"{failed}: {format_exception_message(exc)}"[:1000]
    finally:
        await resume_intake()
        status["steps"]["resume"]["state"] = "done"
        status["step"] = "resume"
        status["state"] = "failed" if status["error"] else "done"
        status["finished_at"] = _now()
        await _write_status(status)
        await _audit(sessions, actor_id, status)
        await _release_job()
        await _announce(status)
    return status


async def _audit(sessions: Any, actor_id: uuid.UUID, status: dict[str, Any]) -> None:
    from app.models.user import User
    from app.services.audit import log_audit

    try:
        async with sessions() as db:
            actor = await db.get(User, actor_id)
            if actor is None:
                return
            purge = status["steps"]["purge"]["counts"]
            await log_audit(
                db,
                user=actor,
                action="delete",
                entity_type="ctem_signal_purge",
                entity_id=status["id"],
                entity_name="Purge of every CTEM signal",
                summary=(
                    f"Purged {purge.get('signals', 0)} CTEM signals and {purge.get('cases', 0)} cases"
                    + (" with a resync" if status["resync"] else "")
                    + (f"; failed at {status['error']}" if status["error"] else "")
                ),
                details={
                    "resync": status["resync"],
                    "steps": status["steps"],
                    "error": status["error"],
                    "started_at": status["started_at"],
                    "finished_at": status["finished_at"],
                },
            )
            await db.commit()
    except Exception:
        logger.error("CTEM signal purge job %s: audit row not written", status["id"], exc_info=True)


async def _announce(status: dict[str, Any]) -> None:
    try:
        from app.services.ctem_cases_view import forget_all_views
        from app.services.ctem_verticals import CTEM_VERTICALS

        for vertical in CTEM_VERTICALS:
            await forget_all_views(vertical)
    except Exception:
        logger.info("CTEM signal purge: case views not dropped", exc_info=True)


async def start(actor: Any, *, resync: bool) -> dict[str, Any]:
    """Start a job in the background as *actor* (an administrator); its first status. :class:`PurgeBusyError` when one runs."""
    if not await _take_job():
        raise PurgeBusyError(BUSY_DETAIL)
    status = new_status(str(uuid.uuid4()), actor, resync=resync)
    await _write_status(status)
    task = asyncio.create_task(run_job(status, actor.id), name=f"ctem-signal-purge:{status['id']}")
    _JOBS.add(task)
    task.add_done_callback(_JOBS.discard)
    return status
