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

"""Bounds on a consulted turn, and the record of a parent that keeps re-asking.

``consult_agent`` runs the consulted peer's turn in-process, inside the
caller's tool call: its child ``AssignmentRun`` (``assignment_id IS NULL``)
never has an orchestration execution, so nothing but the caller can tell a
live consult from a dead one. Before this module the caller waited for the
agent-tool timeout (900 s) whatever happened, and a consult cancelled at that
timeout left its child run ``running`` until the stale-run sweep failed it
another 900 s later.

* :class:`ConsultWatchdog` follows the events the consulted turn emits. A turn
  that emits nothing within :data:`CONSULT_START_TIMEOUT_SECONDS` never
  started; one silent for :data:`CONSULT_IDLE_TIMEOUT_SECONDS` stopped. The
  agent loop emits a ``tool_heartbeat`` every 10 s while a tool runs and an
  event at every iteration, so only a model call longer than the idle bound
  can look silent, and the bound sits well above a normal one.
* :func:`run_watched` awaits the turn under the watchdog and raises
  :class:`ConsultStalledError`, naming the child run, as soon as a bound is
  crossed; the caller fails the child run with the same reason and answers
  at once.
* :class:`ConsultRetryLedger` counts the consecutive failed consults of one
  turn per peer: past :data:`CONSULT_RETRY_WARN_AFTER` the caller logs a
  warning and records ``consult_retries`` on its trace, so a parent looping
  on a failing peer is visible from outside.
* A rollout: a consulted turn has no checkpoint and no execution of
  its own, so it cannot be handed back and resumed like a background task.
  Once its server process is about to stop (:func:`rollout_cut_reason`, the
  moment a checkpointed turn of the same process winds down),
  :func:`run_watched` stops it and raises :class:`ConsultCutError`: the caller
  fails the child run with "cut by a rollout" and answers its tool call at
  once, so the answer is a completed round of the caller's own checkpoint and
  the caller, resumed on the next process, asks again there with no bound to
  wait out. A consult cancelled by the drain itself fails its child with
  :data:`CONSULT_ROLLOUT_ERROR` (:func:`consult_cancel_error`).
* Rollouts closer together than the consulted turn: every re-ask is
  cut again and the question lives only in the caller's context. The caller
  itself is taken back at the same rollout and resumed on another process,
  so no in-memory count of cuts survives: from the FIRST cut
  (:data:`CONSULT_DURABLE_AFTER_CUTS`), :func:`durable_consult_after_cut`
  turns the consult into a durable ask: a
  work item owned by the consulted agent holding the question, and, when the
  caller works a dispatched work item, a follow-up item for the caller blocked
  on it, which the scheduler reopens once the peer completes the ask with its
  answer as the result. The tool call answers with both ids, so the caller
  ends cleanly and the answer reaches whoever holds the work next.
* A peer with no inbox: the durable ask only works when a ``work_item``
  inbox (an enabled assignment or an active flow trigger) takes it from the
  peer's queue. When none does (:func:`peer_inbox_takes`), nothing is written
  and the tool call answers :func:`no_inbox_cut_answer`: cut by a rollout, the
  peer has no inbox, ask again now or go on without the answer. No orphan
  item sits in a queue nobody drains, and the caller is never told to wait
  for an answer that cannot come.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: A consulted turn emits its first event (``preparing``) before it loads
#: memory; none within this bound means it never started.
CONSULT_START_TIMEOUT_SECONDS = 90.0
#: No event at all for this long: the consulted turn stopped. Above a slow
#: model call, below the 900 s agent-tool timeout.
CONSULT_IDLE_TIMEOUT_SECONDS = 600.0
#: How often :func:`run_watched` reads the watchdog.
CONSULT_WATCH_POLL_SECONDS = 5.0
#: Consecutive failed consults of one peer after which the next is reported.
CONSULT_RETRY_WARN_AFTER = 2
#: Rollout cuts after which the consult becomes a durable work item on the
#: peer. One: the caller is taken back at the same rollout, so a
#: per-turn count would restart at zero on every resumed process.
CONSULT_DURABLE_AFTER_CUTS = 1
#: What a consult child run records when its caller is cancelled first (the
#: agent-tool timeout, or the caller's own turn cut or stopped).
CONSULT_CANCELLED_ERROR = (
    "Consult cancelled before the consulted agent answered: the caller's tool "
    "call timed out or its turn was cut or stopped."
)
#: What a consult child run records when a rollout (its server process
#: stopping) cut it.
CONSULT_ROLLOUT_ERROR = (
    "Consult cut by a rollout: its server process was stopping before the "
    "consulted agent answered; the caller asks again on the next one."
)


class ConsultStalledError(Exception):
    """A consulted turn crossed a watchdog bound; the message names its child run."""

    def __init__(self, child_run_id: str | None, reason: str) -> None:
        self.child_run_id = child_run_id
        self.reason = reason
        run = f"child run {child_run_id}" if child_run_id else "no child run recorded"
        super().__init__(
            f"the consulted turn ({run}) {reason}; it was stopped and its run failed. "
            "Retry once, or go on without this answer."
        )


class ConsultCutError(ConsultStalledError):
    """The consulted turn was stopped because its server process is stopping."""

    def __init__(self, child_run_id: str | None, reason: str) -> None:
        self.child_run_id = child_run_id
        self.reason = reason
        run = f"child run {child_run_id}" if child_run_id else "no child run recorded"
        Exception.__init__(
            self,
            f"the consulted turn ({run}) was cut by a rollout: {reason}; its run failed. "
            "Ask the same question again now: it runs on the next server process.",
        )


def rollout_cut_reason() -> str | None:
    """Why a consulted turn must stop now because its process is stopping, else ``None``.

    Holds from the moment a checkpointed turn of the same process winds down
    (``worker_shutdown.wind_down_due``): before it, the consult may still
    answer within the grace. Never raises.
    """
    try:
        from app.core.worker_shutdown import shutdown_request, wind_down_due

        if not wind_down_due():
            return None
        request = shutdown_request()
        cause = f"{request.reason}, {request.detail}" if request is not None else "stop"
        return f"its server process is stopping ({cause})"
    except Exception as exc:
        logger.debug("rollout_cut_reason unavailable: %s", format_exception_message(exc))
        return None


def consult_cancel_error(exc: BaseException) -> str:
    """The error a consult child cancelled by *exc* fails with.

    :data:`CONSULT_ROLLOUT_ERROR` when its process is stopping (the drain's own
    cancellation, or a stop recorded), else :data:`CONSULT_CANCELLED_ERROR`.
    Never raises.
    """
    try:
        from app.core.utils import is_graceful_shutdown_cancellation
        from app.core.worker_shutdown import shutdown_request

        if is_graceful_shutdown_cancellation(exc) or shutdown_request() is not None:
            return CONSULT_ROLLOUT_ERROR
    except Exception as err:
        logger.debug("consult_cancel_error fallback: %s", format_exception_message(err))
    return CONSULT_CANCELLED_ERROR


class ConsultWatchdog:
    """The liveness of one consulted turn, read from the events it emits."""

    def __init__(
        self,
        *,
        start_seconds: float = CONSULT_START_TIMEOUT_SECONDS,
        idle_seconds: float = CONSULT_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._start_seconds = start_seconds
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._opened = clock()
        self._last_event: float | None = None
        self.events = 0

    def observe(self, _event: Any = None) -> None:
        """Record that the consulted turn emitted an event."""
        self._last_event = self._clock()
        self.events += 1

    def stalled(self) -> str | None:
        """Why the turn counts as stalled now, or ``None`` while it is alive."""
        now = self._clock()
        if self._last_event is None:
            waited = now - self._opened
            if waited >= self._start_seconds:
                return f"emitted no event in {int(waited)}s: it never started"
            return None
        silent = now - self._last_event
        if silent >= self._idle_seconds:
            return f"emitted no event for {int(silent)}s after {self.events} event(s): it stopped"
        return None


async def run_watched[T](
    awaitable: Awaitable[T],
    watchdog: ConsultWatchdog,
    *,
    child_run_id: str | None,
    poll_seconds: float = CONSULT_WATCH_POLL_SECONDS,
    cut_check: Callable[[], str | None] | None = None,
) -> T:
    """Await *awaitable* while *watchdog* says it is alive.

    Returns its result or re-raises its exception unchanged. Raises
    :class:`ConsultStalledError` once a bound is crossed, after cancelling it and
    awaiting the cancellation. With *cut_check* (:func:`rollout_cut_reason`),
    raises :class:`ConsultCutError` the same way once it gives a reason, read
    before the first wait too, so no consult starts on a stopping process. An
    outer cancellation cancels it too.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            cut = cut_check() if cut_check is not None else None
            if cut is not None:
                await _cancel_and_wait(task)
                raise ConsultCutError(child_run_id, cut)
            done, _pending = await asyncio.wait({task}, timeout=poll_seconds)
            if done:
                return task.result()
            reason = watchdog.stalled()
            if reason is not None:
                await _cancel_and_wait(task)
                raise ConsultStalledError(child_run_id, reason)
    except asyncio.CancelledError:
        await _cancel_and_wait(task)
        raise


async def _cancel_and_wait(task: asyncio.Future[Any]) -> None:
    if task.done():
        return
    task.cancel()
    # Retrieve the inner outcome so no "exception never retrieved" leaks.
    await asyncio.gather(task, return_exceptions=True)


class ConsultRetryLedger:
    """Consecutive failed consults of one turn, per peer."""

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}

    def failures(self, target_id: str) -> int:
        return self._failures.get(target_id, 0)

    def failed(self, target_id: str) -> int:
        count = self._failures.get(target_id, 0) + 1
        self._failures[target_id] = count
        return count

    def succeeded(self, target_id: str) -> None:
        self._failures.pop(target_id, None)

    def snapshot(self) -> dict[str, int]:
        return dict(self._failures)


async def finalize_consult_child(child_run_id: str | None, error: str) -> None:
    """Fail a consult child run in a session of its own. Never raises.

    Used when the consult's own session cannot be trusted: the consulted turn
    was cancelled mid-flight, possibly inside a query.
    """
    if not child_run_id:
        return
    try:
        from app.core.database import async_session
        from app.services.sub_agent import update_assignment_run

        async with async_session() as db:
            await update_assignment_run(db, child_run_id, "failed", error=error[:2000])
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Consult child run %s not finalized (the stale-run sweep will fail it): %s",
            child_run_id,
            format_exception_message(exc),
        )


#: The priority of the durable ask (and of its follow-up on the caller).
DURABLE_CONSULT_PRIORITY = "high"


def peer_inbox_takes(
    inboxes: Any,
    *,
    owner_agent_id: str,
    correlation_key: str,
    priority: str,
    is_company_managed: bool,
) -> bool:
    """Whether one of *inboxes* takes an item of *owner_agent_id*'s queue.

    The owner, correlation-prefix, priority and company-managed parts of the
    pickup rule (``work_item._inbox_clauses``), read on an item that does not
    exist yet. The narrower rules (a dispatcher draining another agent's queue,
    the inbox user's tier) are left out: they can only make this answer
    ``True`` where the dispatch would not, which keeps today's behaviour.
    """
    from app.core.utils import normalize_correlation_key
    from app.services.work_item import _priorities_at_or_above

    owner = str(owner_agent_id).lower()
    key = (correlation_key or "").lower()
    for inbox in inboxes or ():
        if str(inbox.owner_agent_id).lower() != owner:
            continue
        if is_company_managed and not inbox.writer.may_write:
            continue
        prefix = normalize_correlation_key(inbox.correlation_prefix) if inbox.correlation_prefix else None
        if prefix and not key.startswith(prefix.lower()):
            continue
        allowed = _priorities_at_or_above(inbox.priority_min)
        if allowed is not None and priority not in allowed:
            continue
        return True
    return False


async def _peer_has_inbox(*, target_id: str, correlation_key: str, is_company_managed: bool) -> bool:
    """Whether a ``work_item`` inbox would pick the durable ask up. Never raises.

    Reads every assignment and flow inbox; a failed read answers ``True`` so
    the ask is written as before.
    """
    try:
        from app.core.database import async_session
        from app.services.flow_triggers import load_flow_work_item_inboxes
        from app.services.work_item import load_assignment_inboxes

        async with async_session() as db:
            inboxes = [*await load_assignment_inboxes(db), *await load_flow_work_item_inboxes(db)]
    except Exception as exc:
        logger.debug("Inboxes of %s not read: %s", target_id, format_exception_message(exc))
        return True
    return peer_inbox_takes(
        inboxes,
        owner_agent_id=target_id,
        correlation_key=correlation_key,
        priority=DURABLE_CONSULT_PRIORITY,
        is_company_managed=is_company_managed,
    )


def no_inbox_cut_answer(target_name: str, target_id: str, cuts: int) -> str:
    """What a cut consult answers when no inbox of the peer would take the ask."""
    return (
        f"The consult with {target_name} (agent {target_id}) was cut by a rollout {cuts} time(s), "
        f"and {target_name} has no work-item inbox that would take the question (no enabled "
        "work_item assignment or flow trigger drains its queue), so no work item was created: it "
        "would never be answered. Ask the same question again now (it runs on the next server "
        "process), or go on without this answer."
    )


def durable_consult_key(target_id: str, question: str) -> str:
    """The correlation key of the durable ask of *question* to *target_id*."""
    digest = hashlib.sha256((question or "").strip().encode("utf-8")).hexdigest()[:12]
    return f"consult:{str(target_id)[:8]}-{digest}"


#: How far back an open durable ask of the same caller to the same target is
#: reused instead of filing a twin (#2218): a caller that re-relays a question
#: in new words after a cut lands on the ask already open.
TWIN_ASK_WINDOW = timedelta(minutes=30)


async def open_twin_ask(*, user_id: str | None, caller_agent_id: str, target_id: str) -> tuple[str, str] | None:
    """The newest open durable ask of *caller_agent_id* to *target_id* (#2218).

    ``(id, correlation_key)`` of a ``consult:<target8>-*`` item (never its
    ``-answer`` follow-up) the same caller filed for the same person in the
    last :data:`TWIN_ASK_WINDOW`, still open; ``None`` otherwise or when the
    read fails. Never raises.
    """
    try:
        import uuid

        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

        since = datetime.now(UTC) - TWIN_ASK_WINDOW
        query = (
            select(WorkItem.id, WorkItem.correlation_key)
            .where(
                WorkItem.owner_agent_id == uuid.UUID(str(target_id)),
                WorkItem.created_by_agent_id == uuid.UUID(str(caller_agent_id)),
                WorkItem.correlation_key.like(f"consult:{str(target_id)[:8]}-%"),
                WorkItem.correlation_key.not_like("%-answer"),
                WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
                WorkItem.created_at >= since,
            )
            .order_by(WorkItem.created_at.desc())
            .limit(1)
        )
        query = query.where(
            WorkItem.user_id.is_(None) if user_id is None else WorkItem.user_id == uuid.UUID(str(user_id))
        )
        async with async_session() as db:
            row = (await db.execute(query)).first()
    except Exception as exc:
        logger.debug("Open twin ask of %s not read: %s", caller_agent_id, format_exception_message(exc))
        return None
    return (str(row[0]), str(row[1])) if row else None


async def _create_ask(
    create_work_item: Callable[..., Awaitable[dict[str, Any]]],
    *,
    user_id: str | None,
    caller_name: str,
    caller_agent_id: str,
    caller_is_company_managed: bool,
    target_id: str,
    headline: str,
    question: str,
    cuts: int,
    key: str,
    trail: str,
    writer: Any,
) -> dict[str, Any]:
    """Create (dedupe on *key*) the durable ask owned by the consulted agent."""
    return await create_work_item(
        user_id=user_id,
        title=f"Consult from {caller_name}: {headline}",
        description=(
            f"{caller_name} (agent {caller_agent_id}) asked you this question with "
            f"consult_agent; the consult was cut by a rollout {cuts} time(s), so it is "
            "asked here instead, where a rollout cannot lose it.\n\n"
            "Answer it and complete this item with the WHOLE answer as its result: "
            "the caller reads the result, nothing else."
            f"{trail}\n\n## Question\n\n{(question or '')[:20000]}"
        ),
        owner_agent_id=target_id,
        created_by_agent_id=caller_agent_id,
        priority=DURABLE_CONSULT_PRIORITY,
        correlation_key=key,
        is_company_managed=caller_is_company_managed,
        dedupe=True,
        writer=writer,
    )


async def durable_consult_after_cut(
    *,
    user_id: str | None,
    caller_agent_id: str,
    caller_name: str,
    caller_is_company_managed: bool,
    target_id: str,
    target_name: str,
    question: str,
    cuts: int,
    source_work_item_id: str | None = None,
    parent_run_id: str | None = None,
) -> str | None:
    """Turn a consult cut by rollouts into a durable ask. Never raises.

    Creates (dedupe) a work item owned by the consulted agent holding the
    question; its inbox answers it and completes it with the answer as the
    result. With *source_work_item_id* (the caller's dispatched item), also
    creates (dedupe) a follow-up item owned by the caller and blocked on the
    ask: the scheduler reopens it once the ask is done, so the caller's inbox
    resumes the work with the answer even though this run has ended.

    When no inbox of the consulted agent would take the ask, nothing is written
    and the text is :func:`no_inbox_cut_answer`.

    Returns the text the tool call answers with, or ``None`` when nothing could
    be written (the caller then gets the plain cut message).
    """
    key = durable_consult_key(target_id, question)
    if not await _peer_has_inbox(
        target_id=target_id, correlation_key=key, is_company_managed=caller_is_company_managed
    ):
        logger.info(
            "consult_agent: cut %d time(s), %s has no work-item inbox: no durable ask written",
            cuts,
            target_id,
        )
        return no_inbox_cut_answer(target_name, target_id, cuts)
    try:
        from app.services.company_managed_writes import agent_writer
        from app.services.work_item import create_work_item

        writer = agent_writer(is_company_managed=caller_is_company_managed, agent_id=caller_agent_id)
        headline = " ".join((question or "").split())[:80]
        source = f"\n- Caller's work item: {source_work_item_id}" if source_work_item_id else ""
        run = f"\n- Caller's run: {parent_run_id}" if parent_run_id else ""
        twin = await open_twin_ask(user_id=user_id, caller_agent_id=caller_agent_id, target_id=target_id)
        if twin is not None and twin[1] != key:
            # The caller asked again in other words: one open ask, the new
            # wording as a note on it, never a second run on one question.
            from app.services.work_item import update_work_item

            key = twin[1]
            await update_work_item(
                twin[0],
                user_id=user_id,
                note=(
                    f"{caller_name} asked again after another cut (rollout {cuts} time(s)), "
                    f"in these words; answer both in one result.{source}{run}\n\n"
                    f"{(question or '')[:20000]}"
                ),
                writer=writer,
                stamp_note=True,
            )
            ask: dict[str, Any] = {"id": twin[0]}
        else:
            ask = await _create_ask(
                create_work_item,
                user_id=user_id,
                caller_name=caller_name,
                caller_agent_id=caller_agent_id,
                caller_is_company_managed=caller_is_company_managed,
                target_id=target_id,
                headline=headline,
                question=question,
                cuts=cuts,
                key=key,
                trail=f"{source}{run}",
                writer=writer,
            )
        ask_id = str(ask["id"])
        follow_id: str | None = None
        if source_work_item_id:
            follow = await create_work_item(
                user_id=user_id,
                title=f"Consult answer from {target_name} for work item {source_work_item_id[:8]}",
                description=(
                    f"Resume work item {source_work_item_id} with the answer of {target_name}: "
                    f"get_work_item({ask_id}) and read its result. The consult was cut by a "
                    f"rollout {cuts} time(s) and handed to {target_name} as that item; this one "
                    "reopened by itself when it was done."
                    f"{run}\n\n## Question asked\n\n{(question or '')[:4000]}"
                ),
                owner_agent_id=caller_agent_id,
                created_by_agent_id=caller_agent_id,
                priority=DURABLE_CONSULT_PRIORITY,
                correlation_key=f"{key}-answer",
                is_company_managed=caller_is_company_managed,
                blocked_on=[ask_id],
                dedupe=True,
                writer=writer,
            )
            follow_id = str(follow["id"])
    except Exception as exc:
        logger.warning(
            "Durable consult of %s by %s not written: %s",
            target_id,
            caller_agent_id,
            format_exception_message(exc),
        )
        return None
    logger.info(
        "consult_agent: cut %d time(s), asked %s as work item %s (follow-up %s)",
        cuts,
        target_id,
        ask_id,
        follow_id,
    )
    text = (
        f"The consult with {target_name} was cut by a rollout {cuts} time(s). It is now the "
        f"durable work item {ask_id} owned by {target_name} (key {key}): its inbox answers it "
        "and completes it with the answer as its result, whatever rollouts happen. "
    )
    if follow_id:
        text += (
            f"Work item {follow_id}, owned by you and blocked on it, reopens by itself with the "
            "answer: your inbox resumes the work there. Do not ask again; end this run with a "
            "note naming both items."
        )
    else:
        text += f"Do not ask again: read its result later with get_work_item({ask_id})."
    return text


def report_consult_retry(
    ledger: ConsultRetryLedger,
    *,
    target_id: str,
    target_name: str,
    parent_run_id: str | None,
) -> int:
    """Log and trace a consult made after repeated failures of the same peer.

    Returns the consecutive failures before this call. Past
    :data:`CONSULT_RETRY_WARN_AFTER` a warning names the parent run and the
    peer, and the caller's root trace carries ``consult_retries`` (failures
    per peer id). Never raises.
    """
    prior = ledger.failures(target_id)
    if prior < CONSULT_RETRY_WARN_AFTER:
        return prior
    logger.warning(
        "consult_agent: re-asking %s (%s) after %d consecutive failed consult(s) "
        "in the same turn (parent run %s)",
        target_name,
        target_id,
        prior,
        parent_run_id,
    )
    try:
        from app.services.langfuse_tracing import (
            enrich_trace_metadata,
            get_current_trace_ctx,
            trace_root_ctx,
        )

        enrich_trace_metadata(
            trace_root_ctx(get_current_trace_ctx()),
            {"consult_retries": ledger.snapshot()},
        )
    except Exception as exc:
        logger.debug("consult_retries not recorded on the trace: %s", format_exception_message(exc))
    return prior
