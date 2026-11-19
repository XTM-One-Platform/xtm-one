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

"""Sandbox commands as durable jobs of the workspace, keyed by the call that started them.

A worker stop (a rollout, a scale-in) cuts a turn while a ``sandbox_run_command``
call waits for its command. The command goes on in its container (it runs
detached, :mod:`commands`), but the resumed turn only knows the call it repeats,
not the ``run_id`` the cut call never answered. Without more, the loop's in-flight
repeat guard held that repeat back as "may have taken effect", and the repeat
after it ran the whole check again, even when it had ended meanwhile.

Each call that starts a command binds its job key (:func:`bound`): a digest of
the arguments that define the command (checkout, check, args, argv, cwd,
affected), never the ones that only say how to wait (background). The run the
call starts for the check it asked is recorded under that key in
``kernel_state["jobs"]`` (:func:`record`) the moment it starts, so the record
outlives the worker. The tool's ``landed_probe`` (:func:`landed`) then tells a
repeat of that call:

- the job's run ended and its result was saved, and no file of its checkout changed
  since: that result, answered in place of the repeat (one execution); a tree edited
  after that run ended makes the repeat run, on the tree as it is now;
- the job's run is still held by its container (running, or ended and not
  collected yet): ``False``, the repeat runs and :func:`commands.run_command`
  reattaches to that run (the same check asked again is the held run, waited for);
- no job, or its run was lost with a container restart: ``False``, the repeat runs.

The workspace is the run's (or the conversation's), so the ledger is per run by
construction. Generic on purpose: any long sandbox operation keyed by its call
records itself here the same way.

A checkout rebase (``sandbox_checkout_rebase``) runs in the worker itself, not in a
container: its job (:func:`rebase_key`, :func:`rebase_running`) carries a status, a
heartbeat and, once ended, its answer. A poll by ``run_id`` (:func:`rebase_answer`)
waits for the task when this worker holds it, else answers from the ledger: ended
(the saved answer), running elsewhere (a fresh heartbeat), or lost with its worker
(a stale heartbeat). The rebase writes its files and the moved checkout record in
one change set, so a lost job either landed whole or wrote nothing, and the call
repeated after it meets the rebase's own exactly-once guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

#: ``kernel_state`` key of the ledger.
JOBS = "jobs"
#: Jobs a workspace keeps (the oldest go first).
MAX_JOBS = 64
#: The arguments that define a ``sandbox_run_command`` job.
_DEFINING = ("checkout", "check", "args", "argv", "cwd", "affected")

#: The tool name a checkout rebase job carries in the ledger.
REBASE = "checkout_rebase"
#: Seconds between two heartbeats of a running rebase job.
HEARTBEAT_EVERY = 20.0
#: Seconds without a heartbeat after which a running rebase job is lost with its worker.
STALE_AFTER = 90.0
#: Seconds a poll waits for a rebase this worker runs before answering it is still running.
POLL_WAIT = 60.0
#: Characters of a rebase answer the ledger keeps.
MAX_SAVED_ANSWER = 12_000
#: The rebase tasks this worker runs, by run id (a poll in the same worker waits for them).
_LOCAL: dict[str, asyncio.Task[tuple[str, bool]]] = {}

#: The job key and the label of the check the running tool call asked for.
_bound: ContextVar[tuple[str, str] | None] = ContextVar("sandbox_job", default=None)


def job_key(arguments: Mapping[str, Any] | None) -> str | None:
    """The job key of a ``sandbox_run_command`` call, ``None`` for a call that starts no job of its own.

    A call with a ``run_id`` follows a run it already knows; a call with a ``service_id`` runs in a lab and
    answers when it ends: neither is keyed.
    """
    if not isinstance(arguments, Mapping) or arguments.get("run_id") or arguments.get("service_id"):
        return None
    if not arguments.get("check") and not arguments.get("argv"):
        return None
    defining = {name: arguments.get(name) for name in _DEFINING}
    defining["args"] = list(defining["args"] or [])
    defining["argv"] = list(defining["argv"] or [])
    defining["affected"] = bool(defining["affected"])
    for name in ("checkout", "check", "cwd"):
        defining[name] = str(defining[name] or "").strip()
    text = json.dumps(defining, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def label_of(arguments: Mapping[str, Any]) -> str:
    """The label of the command a call asks for: its check, ``argv`` for a bare argv (``ResolvedCommand.label``)."""
    return str(arguments.get("check") or "").strip() or "argv"


@contextlib.contextmanager
def bound(arguments: Mapping[str, Any] | None) -> Iterator[str | None]:
    """Bind the job of the call with *arguments* to the current context for the call's duration; its key."""
    key = job_key(arguments)
    token = _bound.set((key, label_of(arguments or {})) if key else None)
    try:
        yield key
    finally:
        _bound.reset(token)


def current() -> tuple[str, str] | None:
    """The (key, label) of the job the running call bound, ``None`` outside one."""
    return _bound.get()


async def record(workspace: Any, run_id: str, label: str) -> None:
    """Record *run_id* as the job of the running call when it runs the check that call asked for (*label*).

    An install the call runs first, or a run a call reaps on its way, is not the job; a rerun or a fallback
    of the same check is, and replaces the earlier run. Best effort: a failed write leaves the probe
    answering ``False`` (the repeat runs, as before).
    """
    held = current()
    if held is None or held[1] != label:
        return
    key = held[0]

    def change(state: dict) -> dict | None:
        jobs = dict(state.get(JOBS) or {})
        jobs[key] = {"run_id": run_id, "check": label, "at": time.time()}
        if len(jobs) > MAX_JOBS:
            for old in sorted(jobs, key=lambda k: float((jobs[k] or {}).get("at") or 0.0))[: len(jobs) - MAX_JOBS]:
                jobs.pop(old, None)
        return {**state, JOBS: jobs}

    with contextlib.suppress(Exception):
        await workspace.update_kernel_state(change)


def job_of(state: Mapping[str, Any] | None, key: str) -> dict[str, Any] | None:
    """The job recorded under *key* in a ``kernel_state``."""
    job = ((state or {}).get(JOBS) or {}).get(key)
    return dict(job) if isinstance(job, dict) and job.get("run_id") else None


def _held(state: Mapping[str, Any] | None, run_id: str) -> bool:
    """Whether a container still holds *run_id* (running, or ended and not collected yet)."""
    for held in ((state or {}).get("commands") or {}).values():
        if isinstance(held, dict) and (held.get("run") or {}).get("run_id") == run_id:
            return True
    return False


async def landed(workspace: Any, arguments: Mapping[str, Any] | None) -> str | bool | None:
    """The ``landed_probe`` of ``sandbox_run_command``: the saved result of the job a repeated call started.

    A ``str`` (the result, answered in place of the repeat) when the job's run ended and was saved; ``False``
    when the repeat must run (no job, a run still held, which the repeat reattaches to, or a lost run);
    ``None`` for a call that is no job (a ``stop``: the guard holds it back once, as before).
    """
    from app.services.sandbox_workspace import commands

    key = job_key(arguments)
    if key is None:
        return None
    state = await workspace.latest_kernel_state()
    job = job_of(state, key)
    if job is None:
        return False
    run_id = str(job["run_id"])
    if _held(state, run_id):
        return False
    if await _edited_since(workspace, run_id, str(arguments.get("checkout") or "") if arguments else ""):
        # The tree changed after that run ended (the resumed attempt edited it): its result is not this tree's.
        return False
    outcome = await commands.saved_outcome(workspace, run_id)
    if outcome is None:
        return False
    return (
        f"{job.get('check') or 'the check'} already ran as run {run_id} before the worker restart; its saved "
        f"result follows.\n{outcome.text}"
    )


#: Seconds a file may postdate a run's end and still count as that run's own output (container and worker clocks).
CLOCK_SLACK = 5.0


async def _edited_since(workspace: Any, run_id: str, checkout: str) -> bool:
    """Whether a file of *checkout* (the whole workspace when empty) changed after run *run_id* was saved.

    A saved result answers a repeat only for the tree it ran on: a resumed attempt that edited the files
    since gets the check run again. ``False`` when the run's end time or the manifest cannot be read.
    """
    from datetime import datetime

    from app.services.sandbox_workspace.checks import RUNS_DIR

    try:
        _entry, data = await workspace.get_bytes(f"{RUNS_DIR}/{run_id}/run.json")
        finished = datetime.fromisoformat(str(json.loads(data)["finished_at"])).timestamp()
        entries = await workspace.entries(refresh=True)
    except Exception:
        return False
    prefix = checkout.strip().strip("/")
    prefix = f"{prefix}/" if prefix else ""
    for path, entry in entries.items():
        if path.startswith(".xtm/") or (prefix and not path.startswith(prefix)):
            continue
        if float(getattr(entry, "mtime", 0.0) or 0.0) > finished + CLOCK_SLACK:
            return True
    return False


# -- checkout rebase jobs ----------------------------------------------------


def rebase_key(checkout: str, ref: str, on_conflict: str) -> str:
    """The job key of a background ``sandbox_checkout_rebase`` call: its checkout, ref and conflict mode."""
    text = json.dumps(
        {"tool": REBASE, "checkout": str(checkout or "").strip().strip("/"), "ref": str(ref or "").strip(),
         "on_conflict": str(on_conflict or "").strip()},
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _trimmed(jobs: dict[str, Any]) -> dict[str, Any]:
    if len(jobs) > MAX_JOBS:
        for old in sorted(jobs, key=lambda k: float((jobs[k] or {}).get("at") or 0.0))[: len(jobs) - MAX_JOBS]:
            jobs.pop(old, None)
    return jobs


async def _put_job(workspace: Any, key: str, fields: Mapping[str, Any], *, run_id: str | None = None) -> None:
    """Write *fields* on the job under *key*; with *run_id*, only while that job is still that run."""

    def change(state: dict[str, Any]) -> dict[str, Any] | None:
        jobs = dict(state.get(JOBS) or {})
        held = jobs.get(key)
        if run_id is not None and not (isinstance(held, dict) and held.get("run_id") == run_id):
            return None
        jobs[key] = {**(held if run_id is not None and isinstance(held, dict) else {}), **fields}
        return {**state, JOBS: _trimmed(jobs)}

    await workspace.update_kernel_state(change)


def rebase_job(state: Mapping[str, Any] | None, *, key: str | None = None, run_id: str | None = None) -> dict[str, Any] | None:
    """The rebase job under *key*, or the one whose run is *run_id*, in a ``kernel_state``."""
    ledger = (state or {}).get(JOBS) or {}
    if key is not None:
        job = ledger.get(key)
        return dict(job) if isinstance(job, dict) and job.get("tool") == REBASE else None
    for job in ledger.values():
        if isinstance(job, dict) and job.get("tool") == REBASE and job.get("run_id") == run_id:
            return dict(job)
    return None


def rebase_alive(job: Mapping[str, Any], now: float | None = None) -> bool:
    """Whether a running rebase job still beats (its worker holds it)."""
    if job.get("status") != "running":
        return False
    if str(job.get("run_id") or "") in _LOCAL:
        return not _LOCAL[str(job["run_id"])].done()
    beat = float(job.get("heartbeat") or job.get("at") or 0.0)
    return ((now if now is not None else time.time()) - beat) < STALE_AFTER


@contextlib.asynccontextmanager
async def _beating(workspace: Any, key: str, run_id: str) -> AsyncIterator[None]:
    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY)
            with contextlib.suppress(Exception):
                await _put_job(workspace, key, {"heartbeat": time.time()}, run_id=run_id)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def rebase_running(
    workspace: Any, key: str, run_id: str, label: str, work: Callable[[], Awaitable[tuple[str, bool]]]
) -> asyncio.Task[tuple[str, bool]]:
    """Record the rebase job *run_id* under *key* and run *work* in the background; the task.

    *work* answers ``(text, is_error)``; the job ends with that answer saved (cut to
    :data:`MAX_SAVED_ANSWER`), or ``failed`` with the error when *work* raises.
    """
    now = time.time()
    await _put_job(
        workspace, key, {"tool": REBASE, "run_id": run_id, "label": label, "status": "running", "at": now, "heartbeat": now}
    )

    async def run() -> tuple[str, bool]:
        try:
            async with _beating(workspace, key, run_id):
                text, is_error = await work()
        except Exception as exc:  # the job's answer, never a lost task
            text, is_error = f"the rebase failed: {type(exc).__name__}: {exc}", True
        ended = {"status": "ended", "answer": text[:MAX_SAVED_ANSWER], "is_error": bool(is_error), "ended_at": time.time()}
        with contextlib.suppress(Exception):
            await _put_job(workspace, key, ended, run_id=run_id)
        return text, is_error

    task = asyncio.create_task(run())
    _LOCAL[run_id] = task
    task.add_done_callback(lambda _t: _LOCAL.pop(run_id, None))
    return task


async def rebase_answer(workspace: Any, run_id: str, *, wait: float | None = None) -> tuple[str, str, bool]:
    """The state of the rebase job *run_id*: ``(status, text, is_error)``.

    *status* is ``ended`` (text = its answer), ``running`` (still running after *wait*
    seconds, :data:`POLL_WAIT` by default, when this worker holds it, or beating in another
    worker), ``lost`` (no heartbeat for :data:`STALE_AFTER` seconds: its worker stopped) or ``unknown``.
    """
    # Read at the call, not bound at the definition: a test that sets POLL_WAIT waits that long.
    wait = POLL_WAIT if wait is None else wait
    task = _LOCAL.get(run_id)
    if task is not None and not task.done():
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=wait)
    if task is not None and task.done() and not task.cancelled() and task.exception() is None:
        text, is_error = task.result()
        return "ended", text, is_error
    job = rebase_job(await workspace.latest_kernel_state(), run_id=run_id)
    if job is None:
        return "unknown", "", True
    if job.get("status") == "ended":
        return "ended", str(job.get("answer") or ""), bool(job.get("is_error"))
    if rebase_alive(job):
        return "running", str(job.get("label") or ""), False
    return "lost", str(job.get("label") or ""), True


async def rebase_landed(workspace: Any, arguments: Mapping[str, Any] | None) -> str | bool | None:
    """The ``landed_probe`` of ``sandbox_checkout_rebase``: the saved answer of the job a repeated call started.

    A ``str`` when that rebase job ended (its answer, in place of the repeat); ``False`` otherwise: the
    repeat runs, and finds the running job (answered as that job) or the rebase's exactly-once guard (a
    synchronous rebase writes in one change set: landed whole, the repeat writes nothing).
    """
    args = arguments if isinstance(arguments, Mapping) else {}
    if str(args.get("run_id") or "").strip() or not str(args.get("ref") or "").strip():
        return False
    key = rebase_key(str(args.get("checkout") or ""), str(args.get("ref") or ""), str(args.get("on_conflict") or "markers"))
    job = rebase_job(await workspace.latest_kernel_state(), key=key)
    if job is None or job.get("status") != "ended" or job.get("is_error"):
        return False
    return (
        f"The rebase {job.get('label') or ''} already ran as run_id {job.get('run_id')} before the worker restart; "
        f"its saved answer follows.\n{job.get('answer') or ''}"
    )
