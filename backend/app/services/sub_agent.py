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

"""Sub-agent service – spawn, execute, and manage background agent tasks.

Enables agents to delegate work to other agents asynchronously.  The parent
agent continues its conversation while the target agent works in the
background.  Results are persisted in PostgreSQL.

Delegation tracing — chat vs assignment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The tracing behaviour depends on WHERE the delegation originates:

**Assignment context** (``parent_run_id`` is set):
  Spawned tasks create linked child ``AssignmentRun`` records so the full
  delegation chain (parent → child → grandchild…) is visible in the
  Activity tab as a tree of runs.  This chain is propagated through nested
  delegations: ``assignment_engine`` → ``create_sub_agent_task`` →
  ``_execute_sub_agent_task`` → ``run_agent_chat(parent_run_id=...)`` →
  ``build_sub_agent_tools(parent_run_id=...)`` → (next level).

**Chat / channel / bot context** (``parent_run_id`` resolved lazily):
  A parent ``AssignmentRun`` is lazily created on the first handover via
  ``_ensure_chat_parent_run()`` in ``agent_chat.py``.  Subsequent handovers
  chain child runs to it, giving the same tree-of-runs visibility as
  assignment-originated delegations.

``delegation_depth`` is incremented at each level and capped at
``_MAX_DELEGATION_DEPTH`` to prevent infinite delegation chains.

Usage from the agentic loop (via tools):
  1. ``spawn_background_task(agent_id, task)`` – creates a SubAgentTask,
     kicks off async execution, returns immediately with the task ID.
  2. ``check_task_status(task_ids)`` – adaptively wait for and report status.
  3. ``get_task_result(task_ids)`` / ``get_full_task_result(task_id)`` –
     retrieve completed results (summary previews / one full output).
  When a chat-originated task reaches ``completed`` or ``failed`` and the
  parent conversation is idle, ``parent_wake`` starts one continuation
  turn so the parent assesses the result instead of sitting silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.partner_scope import (
    PARTNER_PRINCIPAL_PAYLOAD_KEY,
    PARTNER_TASK_PAYLOAD_KEY,
    in_partner_task,
    partner_principal_payload,
)
from app.core.config import settings
from app.core.constants import (
    TRUNCATE_ERROR,
    TRUNCATE_LABEL,
    TRUNCATE_QUESTION,
    TRUNCATE_RESULT,
)
from app.core.redaction import bind_known_secrets, known_secrets_scoped, redact_known_secrets_for_display
from app.core.utils import chunked, format_exception_message, safe_uuid
from app.llm.base import ContextOverflowError, count_llm_retries
from app.models.agent import Agent
from app.models.sub_agent_task import SubAgentTask
from app.services.attempt_charges import credit_lost_attempts, task_receipt_token
from app.services.channel_notices import DEFAULT_LANGUAGE, notice, user_notice_language
from app.services.element_scope import ENFORCED_SCOPE_MARKER_KEY, INHERITED_SCOPE_KEY
from app.services.failure_causes import FailureCause, FailureDetermination
from app.services.health.identity import HEALTH_ALONE_KEY
from app.services.orchestration_engine import (
    bound_by_deadline,
    deadline_cut_budget,
    deadline_spent,
    deadline_spent_message,
)
from app.services.output_format import CALL_OUTPUT_SCHEMA_KEY
from app.services.run_ledger import (
    EVALUATION_CHAIN_KEY,
    EVALUATION_TOOL_APPROVAL_KEY,
    NO_CONTINUATION_KEY,
    with_task_ledger,
)
from app.services.sub_agent_files import (
    CARRIED_FILE_IDS_KEY,
    RESULT_FILE_IDS_KEY,
    RecordingFileContext,
    merge_result_file_ids,
    produced_file_ids,
    resumed_file_refs,
    task_attempt_fence,
    task_attempt_place,
    task_fence_for_deferral,
    task_result_file_ids,
    task_turn_file_refs,
    with_task_attempt_fence,
)
from app.services.sub_agent_result_text import RUN_SUMMARY_CHARS, clipped, stored_answer
from app.services.tool_scaffolding import (
    fold_tool_scaffolding,
    transcript_tool_names,
)
from app.tools.wait_policy import WAIT_TERMINAL_STATUSES

if TYPE_CHECKING:
    from app.services.agentic_gate import AgenticVerdict

logger = logging.getLogger(__name__)

# Maximum delegation depth to prevent infinite chains.
_MAX_DELEGATION_DEPTH = 5
#: Task metadata key of the payload ``trigger_type`` of a run a task created
#: for itself with no parent run (``own_run_trigger``, e.g. ``run_once``); a
#: task without it runs under ``delegation``.
RUN_TRIGGER_TYPE_KEY = "run_trigger_type"
#: The ``trigger_type`` of the run ``run_agent_once`` creates for its task.
RUN_ONCE_TRIGGER_TYPE = "run_once"
#: Task metadata flag of a ``run_agent_once(dry_run=true)`` task (#1911): its
#: turn is bound to a ``dry_run`` sandbox session, reads run live and every
#: write is logged and answered that it was not executed. On the task row, so
#: a resumed attempt keeps it.
RUN_DRY_RUN_KEY = "dry_run"
# Platform-driven continuations when a sub-agent run dies waiting on
# still-running children. A run whose ONLY remaining work is waiting on
# a genuinely-running delegation must not "time out" and abandon it — it parks
# and a continuation resumes the wait. Each continuation waits up to a full
# per-run budget (default 30 min), so this cap bounds the total wait a chain of
# continuations can cover; the chain stops the instant a continuation completes
# normally (i.e. the child finished), and the child's own hard timeout makes it
# terminal eventually regardless, so this is a runaway backstop, not the primary
# terminator. A continuation only ends incomplete (and so chains) when a child
# was STILL running at budget exhaustion.
_MAX_WAIT_CONTINUATIONS = 3
_CONTINUATION_ORIGINAL_CHARS = 4_000

# Last-resort timeout when a chat-spawned task cannot resolve the target
# agent's iteration budget (lookup failed / pre-inheritance payload with
# no agent row).  Tasks spawned from an assignment run inherit the
# assignment's effective ``timeout_seconds`` instead (see
# ``resolve_sub_agent_timeout``) so a 3600s assignment doesn't watch its
# fan-out workers die at 900s.  Chat spawns no longer use this
# as the default: they derive ``chat_loop_timeout_seconds(max_iterations)``
# from the target agent so a 100-iteration specialist is not killed at
# 15 minutes while its iteration cap is barely started.
_SUB_AGENT_TASK_TIMEOUT = 900  # 15 minutes

# What a task keeps inside its bound after the agent loop returns: the turn's
# own wrap-up (persistence, memory) runs after the loop, under the same bound.
_LOOP_END_RESERVE_SECONDS = 60.0
_LOOP_END_RESERVE_SHARE = 0.1


def loop_wall_clock_deadline(
    timeout: float | None, *, now: float | None = None
) -> float | None:
    """Monotonic deadline for the agent loop of a task bounded by *timeout*.

    Ends a reserve before the bound (60 s, a tenth of a short budget) so the
    loop's own deadline handling - the ``wait`` clamp that parks a task waiting
    on its own delegations, the forced final answer - runs before
    ``asyncio.wait_for`` cancels the turn. ``None`` without a bound.
    """
    if timeout is None:
        return None
    start = time.monotonic() if now is None else now
    reserve = min(_LOOP_END_RESERVE_SECONDS, float(timeout) * _LOOP_END_RESERVE_SHARE)
    return start + float(timeout) - reserve


def resolve_sub_agent_timeout(timeout_seconds: int | None) -> int | None:
    """Resolve the effective wall-clock budget for one sub-agent task.

    ``timeout_seconds`` is the inherited parent budget (an assignment run's
    effective ``timeout_seconds``), a chat-derived budget already resolved
    by :func:`resolve_chat_sub_agent_timeout`, or ``None`` when the spawn
    has no assignment scope and the payload predates chat derivation:

    * ``None``  -> last-resort :data:`_SUB_AGENT_TASK_TIMEOUT` (900s).
      Chat spawns should not land here: ``create_sub_agent_task`` and
      ``_execute_sub_agent_task`` derive the budget from the target
      agent's ``max_iterations`` first.
    * ``<= 0``  -> ``None`` — the parent's timeout is disabled, so the task
      gets no per-task ``wait_for`` / orchestration deadline either (the
      reconciler's ``worker_job_timeout`` fallback still bounds it, exactly
      like a disabled-timeout assignment run).
    * ``> 0``   -> that many seconds.

    Single source of truth shared by ``create_sub_agent_task`` (orchestration
    ``deadline_at``) and ``_execute_sub_agent_task`` (``asyncio.wait_for``) so
    the two bounds can never drift.
    """
    if timeout_seconds is None:
        return _SUB_AGENT_TASK_TIMEOUT
    if timeout_seconds <= 0:
        return None
    return int(timeout_seconds)


def _as_optional_int(value: object) -> int | None:
    """Narrow a DB scalar to ``int | None``.  Non-ints (including ORM
    mocks in tests) are treated as unset so the caller falls through to
    the platform default instead of raising.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _read_optional_int_scalar(row: object) -> int | None:
    """Read ``scalar_one_or_none()`` as ``int | None``.

    An ``AsyncMock`` session (unit tests) returns a coroutine here —
    close it rather than awaiting, so we fall through to the next
    lookup / the platform default without a RuntimeWarning.
    """
    getter = getattr(row, "scalar_one_or_none", None)
    if not callable(getter):
        return None
    value = getter()
    if inspect.isawaitable(value):
        closer = getattr(value, "close", None)
        if callable(closer):
            closer()
        return None
    return _as_optional_int(value)


async def resolve_chat_sub_agent_timeout(
    db: AsyncSession,
    target_agent_id: str,
    *,
    agent: Agent | None = None,
    evaluation_chain: bool = False,
) -> int:
    """Wall-clock budget for a chat-spawned sub-agent task.

    Uses the TARGET agent's ``max_iterations`` (falling back to the
    platform General Assistant default) and the same formula as
    ``run_agent_chat``: :func:`chat_loop_timeout_seconds`.  A specialist
    configured for 100 iterations therefore gets 12 000 s, not the
    historical 900 s default that killed the task first.

    ``agent`` is an already-loaded row (the execute path has it); when
    omitted the helper loads just ``max_iterations`` (the create path).
    The task's turn keeps going in blocks while it makes progress,
    so the budget is sized for the iterations it may reach. A task in an
    evaluation's chain never does: its budget is its cap's.
    """
    from app.models.general_assistant_config import (
        DEFAULT_MAX_ITERATIONS,
        GeneralAssistantConfig,
        chat_loop_max_iterations,
        chat_loop_timeout_seconds,
        iteration_reach,
    )

    agent_iters = _as_optional_int(
        getattr(agent, "max_iterations", None) if agent is not None else None
    )
    if agent_iters is None and agent is None:
        target_uuid = safe_uuid(str(target_agent_id))
        if target_uuid is not None:
            row = await db.execute(
                select(Agent.max_iterations).where(Agent.id == target_uuid)
            )
            agent_iters = _read_optional_int_scalar(row)

    if agent_iters is not None:
        iterations = chat_loop_max_iterations(agent_iters)
    else:
        ga_row = await db.execute(
            select(GeneralAssistantConfig.max_iterations).limit(1)
        )
        platform = _read_optional_int_scalar(ga_row)
        iterations = chat_loop_max_iterations(
            platform if platform is not None else DEFAULT_MAX_ITERATIONS
        )
    return chat_loop_timeout_seconds(
        iterations if evaluation_chain else iteration_reach(iterations)
    )

# Namespace for the per-run fan-out cap advisory lock (the first key of
# ``pg_advisory_xact_lock(int4, int4)``), so the scope hash can't collide with
# advisory locks taken elsewhere for unrelated features.
_SUB_AGENT_CAP_LOCK_NS = 0x53414743  # "SAGC" — Sub-Agent Group Cap

# Fallback-only: when the parent will NOT wake (no conversation / already
# woken / suppressed), a completion notice may still be posted. Chat
# completions that resume the parent skip this dump entirely:
# the parent reads the full result in the hidden wake prompt and
# summarizes. Kept small so a leftover dump cannot flood the thread.
_DELIVERY_EXTRACT_CHARS = 1200

# Max characters of the task description included in timeout/failure log lines
# — enough to identify WHAT was delegated without correlating the task UUID
# against the database, while keeping the log record compact.
_LOG_TASK_DESC_CHARS = 120

# Upper bound on one best-effort SSE publish in ``publish_task_event``.  The
# spawn-side publish sits on the ``spawn_background_task`` tool hot path, and
# the Redis client's own limits (5s connect + 5s socket response by default)
# would let a Redis hiccup stall the tool for up to ~10s — far longer than a
# fire-and-forget UI signal is worth.  A timeout is treated exactly like any
# other publish failure: logged at debug and swallowed.
_PUBLISH_EVENT_TIMEOUT = 2.0


def _should_deliver_completion_notice(
    task: SubAgentTask | None,
    *,
    disable_chat: bool,
    has_response: bool,
) -> bool:
    """True when a truncated extract may still be posted into the thread.

    Chat-originated completions that will resume the parent must NOT dump
    an extract: the parent wake prompt carries the full result and the
    parent's own answer is what the user should see.
    """
    if (
        task is None
        or not getattr(task, "conversation_id", None)
        or not has_response
        or disable_chat
        or _delivery_suppressed(task)
        or getattr(task, "status", None) == "incomplete"
    ):
        return False
    from app.services.parent_wake import should_wake_parent

    return not should_wake_parent(task)


def _fold_delivered_answer(
    task_id: str,
    agent_response: str | None,
    transcript: Iterable[object],
) -> str:
    """The text a finished background task DELIVERS.

    A model that hit a refused tool call sometimes narrates a fabricated tool
    transcript into its FINAL answer (a bare tool name, ``Request``, JSON
    arguments, a ``Tool Call Failed`` line listing tools that exist nowhere).
    Everything delivered from the run derives from ``agent_response`` - the
    task result, the run summary, the delegation record, the completion
    notice, the parent wake - so the scaffolding is folded out ONCE, here,
    before the first persisted use.  The run's own transcript supplies the
    tool vocabulary the fold checks a narrated block against
    (:func:`transcript_tool_names`), so a legitimate API write-up naming a
    tool the run never called is left alone.

    *transcript* (``chat_messages``, the raw transcript persisted on the run)
    is only READ: the run detail must keep the real tool calls and their real
    refusals.
    """
    fold = fold_tool_scaffolding(
        agent_response or "", known_tools=transcript_tool_names(transcript)
    )
    if not fold.removed_blocks:
        return agent_response or ""
    logger.info(
        "Sub-agent task %s: folded %d narrated tool-call block(s) out of the "
        "delivered answer (%s)",
        task_id,
        fold.removed_blocks,
        ", ".join(fold.tool_names) or "unnamed",
    )
    return fold.delivered()


def _format_completion_notice(
    agent_name: str, result_text: str, language: str = DEFAULT_LANGUAGE
) -> str:
    """Fallback dump when the parent will not wake. Never claims a task page.

    Chat completions that resume the parent skip this entirely.
    """
    body = (result_text or "").strip()
    header = notice("task_completed", language, agent_name=agent_name)
    if not body:
        return f"{header}\n\n{notice('task_no_output', language)}"
    if len(body) <= _DELIVERY_EXTRACT_CHARS:
        return f"{header}\n\n{body}"
    extract = body[:_DELIVERY_EXTRACT_CHARS].rstrip()
    return f"{header}\n\n{extract}..."


def _wait_continuation_prompt(original: str, pending_ids: list[str]) -> str:
    """User message for a platform-spawned wait-budget continuation run.

    The previous run parked because a delegation was genuinely still running.
    The continuation must therefore *keep waiting* for the work to finish —
    not do a one-shot status check and give up. It waits for whatever
    is still running, reads the finished results, and only then produces the
    deliverable. If it runs out of budget again while a child is STILL running,
    it parks and the platform spawns the next continuation — so a long delegation
    is waited to completion instead of being abandoned with a partial answer.
    """
    listed = ", ".join(pending_ids)
    excerpt = (original or "").strip()[:_CONTINUATION_ORIGINAL_CHARS]
    return (
        "[Platform continuation] Your previous run parked because these "
        f"background tasks were still running: {listed}.\n\n"
        "First call check_task_status for those tasks - it reports which have "
        "finished and which are still running (collect_task_results does not; "
        "it only aggregates completed outputs). For any still running, call "
        "wait with until set to their IDs (comma-separated) to "
        "wait for them to finish - this is the whole point of this run, so do "
        "NOT skip the wait or produce a partial answer while they are still in "
        "flight. Once they finish, read every result with get_task_result and "
        "produce the consolidated deliverable for the original task. Only if a "
        "task genuinely FAILED or came back incomplete should you relaunch it "
        "with spawn_background_task.\n\n"
        f"Original task:\n{excerpt}"
    )


async def _record_result_files(
    task: SubAgentTask | None,
    file_context: object | None,
    inherited_file_ids: set[str] | None,
    *,
    answer: str | None = None,
) -> list[str]:
    """Write the files a run produced onto *task* and return the recorded ids.

    What the loop ADDED to *file_context* beyond *inherited_file_ids* (the
    parent's working files the task was handed), behind the files a wait-budget
    predecessor already recorded (:data:`CARRIED_FILE_IDS_KEY` - they sit in
    the inherited set here, so they would not count as produced), bounded as
    one record. A file the run only fetched to read is part of it when
    the delivered *answer* names it. Runs on every terminal path, the
    failed ones included: a task that timed out after writing three of nine
    batches still hands those three to the parent it wakes.
    ``inherited_file_ids is None`` means the run never reached the loop -
    nothing was produced, nothing is written.
    Leaves ``metadata_json`` untouched when there is nothing to record.

    A :class:`RecordingFileContext` has been writing the same record
    incrementally (the copy a dead-lettered task keeps); its in-flight write
    is drained first so this terminal write is the last one on the row.
    """
    if task is None or inherited_file_ids is None:
        return []
    drain = getattr(file_context, "drain", None)
    if drain is not None:
        await drain()
    # A JSON column can hold any shape; a non-mapping is read as empty rather
    # than failing the terminal write (and with it the task) over metadata.
    raw_meta = task.metadata_json
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    recorded = merge_result_file_ids(
        task_result_file_ids(meta, key=CARRIED_FILE_IDS_KEY),
        produced_file_ids(file_context, inherited_file_ids, answer),
    )
    if recorded and recorded != task_result_file_ids(meta):
        task.metadata_json = {**meta, RESULT_FILE_IDS_KEY: recorded}
    return recorded


async def _spawn_wait_budget_continuation(
    *,
    user_id: str,
    parent_agent_id: str,
    target_agent_id: str,
    original_task: str,
    pending_ids: list[str],
    conversation_id: str | None,
    parent_run_id: str | None,
    visited_agents: list[str] | None,
    timeout_seconds: int | None,
    max_iterations: int | None,
    continuation_of: str,
    continuation_depth: int,
    scope_headers: dict[str, str] | None,
    file_context: object | None,
    db: AsyncSession,
    carried_file_ids: list[str] | None = None,
    element_scope: dict | None = None,
    evaluation_chain: bool = False,
    evaluation_tool_approval: bool | None = None,
    cap_scope: str | None = None,
    health_alone: bool = False,
    call_output_schema: dict[str, Any] | None = None,
) -> bool:
    """Spawn one follow-up task after a wait-budget death with live children.

    Best-effort: a cap hit or cycle rejection must not fail the incomplete
    parent task that already recorded its honest status.

    ``carried_file_ids`` are the files the incomplete task already produced.
    The continuation inherits them through ``file_context`` so it can
    read them, and records them under its own result so the parent - which is
    only woken by the task that finally completes - learns about every file.
    ``element_scope`` is the Scope(s) the incomplete task inherited:
    the continuation is the same work and stays under the same boundary.
    """
    if not pending_ids:
        return False
    extra_metadata: dict = {
        "wait_continuation_depth": continuation_depth,
        "wait_continuation_of": continuation_of,
        # The same work: still within the evaluation's chain, if it was.
        **({EVALUATION_CHAIN_KEY: True} if evaluation_chain else {}),
        # And under the same tool approval setting.
        **(
            {EVALUATION_TOOL_APPROVAL_KEY: evaluation_tool_approval}
            if evaluation_chain and evaluation_tool_approval is not None
            else {}
        ),
        "pending_task_ids": pending_ids,
        # The same work: still Platform Health's with no user, if it was.
        **({HEALTH_ALONE_KEY: True} if health_alone else {}),
        # And still answering in the shape its caller fixed.
        **({CALL_OUTPUT_SCHEMA_KEY: call_output_schema} if call_output_schema else {}),
    }
    if carried_file_ids:
        extra_metadata[CARRIED_FILE_IDS_KEY] = list(carried_file_ids)
    try:
        spawned = await create_sub_agent_task(
            db,
            user_id=user_id,
            parent_agent_id=parent_agent_id,
            target_agent_id=target_agent_id,
            # The original task can only exist if its spawn passed the cycle
            # guard, so a same-agent continuation re-asserts the self-fan-out
            # allowance the original necessarily had.
            allow_self=str(parent_agent_id) == str(target_agent_id),
            task_description=_wait_continuation_prompt(original_task, pending_ids),
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            visited_agents=visited_agents,
            timeout_seconds=timeout_seconds,
            max_iterations=max_iterations,
            extra_metadata=extra_metadata,
            scope_headers=scope_headers,
            file_context=file_context,
            element_scope=element_scope,
            cap_scope=cap_scope,
        )
        if spawned is None:
            logger.warning(
                "Wait-budget continuation not spawned for task %s "
                "(fan-out cap or create returned None); pending=%s",
                continuation_of,
                pending_ids,
            )
            return False
        logger.info(
            "Spawned wait-budget continuation %s for incomplete task %s "
            "(pending=%s)",
            spawned.id,
            continuation_of,
            pending_ids,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Failed to spawn wait-budget continuation for task %s: %s",
            continuation_of,
            format_exception_message(exc),
        )
        return False


async def publish_task_event(
    user_id: str,
    event: str,
    task_id: str,
    *,
    conversation_id: str | None = None,
    status: str | None = None,
) -> None:
    """Publish a background-task lifecycle event to the user's SSE channel.

    The frontend activity stream (``useActivityStream``) invalidates the
    background-task caches (chat banner, Background Tasks screen, Flows live
    view) on every SSE message — this publisher is what makes those surfaces
    react in real time at spawn and at every terminal state (completed,
    failed, cancelled) instead of waiting for the 30s idle poll.
    ``background_task_update`` events additionally refresh
    the open conversation so a proactively delivered completion notice
    appears live in the thread.

    Best-effort by contract: a Redis hiccup must never fail the task itself,
    and the publish is time-bounded by :data:`_PUBLISH_EVENT_TIMEOUT` so a
    slow/unreachable Redis can't hold up the spawning tool call or a worker
    terminal transition for the client library's full connect+response
    timeouts either.
    """
    if event in ("completed", "failed"):
        await _publish_platform_task_event(task_id, event)
    if not user_id:
        return
    try:
        import json as _json

        from app.core.redis import redis_client

        redis = await redis_client()
        payload = _json.dumps(
            {
                "type": "background_task_update",
                "event": event,
                "task_id": task_id,
                "conversation_id": conversation_id,
                "status": status,
            }
        )
        await asyncio.wait_for(
            redis.publish(f"activity:{user_id}", payload),
            timeout=_PUBLISH_EVENT_TIMEOUT,
        )
    except Exception as exc:
        logger.debug(
            "Failed to publish background-task SSE event for task %s: %s",
            task_id,
            format_exception_message(exc),
        )


async def _spawning_work_item(parent_run_id: object) -> str | None:
    """The work item the inbox dispatched run *parent_run_id* for, else ``None``; never raises (#2215)."""
    run_uuid = safe_uuid(str(parent_run_id or ""))
    if run_uuid is None:
        return None
    try:
        from app.core.database import async_session
        from app.models.assignment import AssignmentRun
        from app.services.assignment_engine import dispatched_work_item_id

        async with async_session() as db:
            row = (
                await db.execute(
                    select(AssignmentRun.trigger_context, AssignmentRun.trigger_payload).where(
                        AssignmentRun.id == run_uuid
                    )
                )
            ).first()
        return dispatched_work_item_id(row[0], row[1]) if row is not None else None
    except Exception as exc:
        logger.debug("Work item of run %s not read: %s", parent_run_id, format_exception_message(exc))
        return None


async def _publish_platform_task_event(task_id: str, event: str) -> None:
    """Raise ``background_task.completed`` / ``background_task.failed``; never raises."""
    try:
        from app.core.database import async_session
        from app.services import platform_events

        async with async_session() as db:
            tid = safe_uuid(str(task_id))
            task = await db.get(SubAgentTask, tid) if tid is not None else None
        if task is None:
            return
        metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
        parent_run_id = metadata.get("parent_run_id") or metadata.get("assignment_run_id")
        work_item_id = await _spawning_work_item(parent_run_id)
        await platform_events.publish(
            f"background_task.{event}",
            platform_events.background_task_data(
                task_id=task.id,
                agent_id=task.target_agent_id,
                parent_agent_id=task.parent_agent_id,
                parent_run_id=parent_run_id,
                outcome=task.result,
                error=task.error,
                work_item_id=work_item_id,
            ),
        )
        if work_item_id:
            from app.services.work_item import wake_on_background_task

            await wake_on_background_task(work_item_id, task_id=task.id, outcome=event)
    except Exception as exc:
        logger.debug("Background task %s platform event not published: %s", task_id, format_exception_message(exc))


@dataclass(frozen=True)
class SubAgentTaskOutcome:
    """Terminal outcome of ``_execute_sub_agent_task``.

    Returned to the ``SubAgentAdapter`` so the orchestration engine records
    failed delegations as failed executions instead of silently completing
    them (the task body handles its own error persistence and never raises).
    """

    # "completed" | "failed" | "cancelled" | "deferred" | "held" - "deferred":
    # the task was left pending, untouched, for the engine to retry;
    # "held": left pending because agentic AI is unavailable, for the engine
    # to hold, ``refusal`` naming why.
    status: str
    error: str | None = None
    refusal: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    @property
    def deferred(self) -> bool:
        return self.status == "deferred"

    @property
    def held(self) -> bool:
        return self.status == "held"

    @property
    def handed_back(self) -> bool:
        """Wound down by its stopping worker, checkpointed, to be resumed by the next one."""
        return self.status == "handed_back"


class HandoverCycleError(Exception):
    """Raised when creating a sub-agent task would create a delegation cycle.

    Defense in depth: callers (``spawn_background_task``) pre-check the chain
    and return a friendly tool-result error before ever reaching
    ``create_sub_agent_task`` — this exception only fires if a new call site
    forgets the pre-check.  The message is already user/LLM friendly.
    """



# ── Public API ───────────────────────────────────────────────────────────────


async def create_sub_agent_task(
    db: AsyncSession,  # kept for API compat; uses own session internally
    *,
    user_id: str,
    parent_agent_id: str,
    target_agent_id: str,
    task_description: str,
    conversation_id: str | None = None,
    parent_run_id: str | None = None,
    delegation_depth: int = 0,
    scope_headers: dict[str, str] | None = None,
    file_context: object | None = None,
    run_limit: int | None = None,
    visited_agents: list[str] | None = None,
    allow_self: bool = False,
    timeout_seconds: int | None = None,
    max_iterations: int | None = None,
    extra_metadata: dict | None = None,
    element_scope: dict | None = None,
    cap_scope: str | None = None,
    own_run_trigger: str | None = None,
) -> SubAgentTask | None:
    """Create a sub-agent task record and schedule its background execution.

    Returns the task immediately (status='pending').  Execution is durable:
    a ``sub_agent`` orchestration execution is created in the same transaction
    as the task, and the reconciler runs it on the worker via the
    ``SubAgentAdapter`` — so a delegation survives a platform restart instead of
    being lost with the in-process task.

    Uses an independent DB session so multiple parallel spawn calls from
    ``asyncio.gather`` don't conflict on the parent chat session.

    When ``run_limit`` is set (and a ``parent_run_id`` / ``conversation_id`` /
    ``cap_scope`` scope exists), the per-run fan-out cap is enforced **atomically**: a
    transaction-scoped advisory lock serialises count+create for the scope, so
    two concurrent spawns can't both observe ``active < limit`` and over-spawn.
    Returns ``None`` (without creating anything) when the scope is already at or
    over the cap — the caller surfaces the limit message. The pre-check in
    ``spawn_background_task`` stays as a fast path; this is the authoritative
    gate.

    When ``parent_run_id`` is provided, a child AssignmentRun is also created
    so the delegation is visible in the Activity tab as part of the run tree.
    ``own_run_trigger`` (e.g. ``"run_once"``) creates that run even with no
    parent run, under that payload ``trigger_type``: the task is then a run of
    the target agent of its own, listed with its runs and given the run's
    sandbox workspace (``WorkspaceScope.build`` keys a workspace on a run).

    ``visited_agents`` is the ordered chain of agent ids already involved in
    the current handover chain.  A target already in the chain — or equal to
    the parent agent — raises ``HandoverCycleError`` (cycle guard — callers
    normally pre-check and return a friendly tool result instead).  The
    chain, extended with the parent agent, is stored on the execution
    payload so nested delegations inherit it.

    ``allow_self`` opts the *direct self-fan-out* case out of the cycle guard:
    when ``target_agent_id == parent_agent_id`` (an agent spawning a parallel
    copy of itself), the guard is bypassed.  This is safe because self-fan-out
    is only offered at the top delegation level (the spawned copy can't recurse
    again) and is bounded by ``run_limit``.  It does NOT relax the guard for any
    OTHER agent already in the chain.

    ``timeout_seconds`` / ``max_iterations`` are the parent execution budget
    inherited by the task (an assignment run's effective ``timeout_seconds``
    and resolved ``max_iterations``).  ``timeout_seconds=None`` is a chat
    spawn: the wall-clock is derived from the target agent's
    ``max_iterations`` via :func:`resolve_chat_sub_agent_timeout` (the same
    ``max(1800, chat_loop_max_iterations(n) * 120)`` formula as
    ``run_agent_chat``) so a 5-iteration specialist is not stripped at
    round 3 and a 100-iteration specialist is not killed at 900s.  ``<= 0``
    disables the per-task deadline (mirrors a disabled assignment timeout).
    ``max_iterations=None`` keeps the target agent's own resolution (then
    floored on the chat path).  Both are stored in the execution payload so
    the worker applies them.

    ``element_scope`` is the delegating run's enforced Scope(s)
    #(4449), stored on the payload with its marker so the worker hands it to
    the target agent's run, which can only narrow it
    (``services.element_scope``).

    ``cap_scope`` is the fan-out cap's scope for a run that has neither a
    parent run nor a conversation (an evaluated turn): stored on the
    task and its payload, and counted only when the other two are absent.
    """
    # ── Cycle guard (before any DB work) ─────────────────────────────────
    # Extend the chain with the parent FIRST so self-delegation and
    # delegate-back-to-parent are caught even when a call site passes no
    # chain at all (``visited_agents=None``) or a chain omitting the parent.
    # ``normalize_visited_agents`` drops non-sequence values (a bare string
    # would otherwise become a bogus per-character chain stored on the
    # execution payload and inherited by nested delegations).
    from app.services.builtin_tools import normalize_visited_agents

    _chain: list[str] = normalize_visited_agents(visited_agents)
    if parent_agent_id and str(parent_agent_id) not in _chain:
        _chain.append(str(parent_agent_id))
    # Deliberate self-fan-out (target == parent) is allowed through; every
    # other in-chain target is a real cycle and still rejected.
    _is_self_fanout = allow_self and str(target_agent_id) == str(parent_agent_id)
    if str(target_agent_id) in _chain and not _is_self_fanout:
        raise HandoverCycleError(
            f"Handover rejected: agent {target_agent_id} is already in this "
            f"delegation chain ({' -> '.join(_chain)} -> {target_agent_id}). "
            f"Complete the work yourself or choose a different agent."
        )

    from app.core.database import async_session
    from app.services.health.identity import health_acts_alone

    # Delegated by Platform Health acting alone (read on the server's own
    # context, never on an argument): the task's run and turn act with no
    # user, and ``user_id`` is the row's owner of record only.
    _health_alone = health_acts_alone() or bool((extra_metadata or {}).get(HEALTH_ALONE_KEY))

    async with async_session() as own_db:
        # Atomic per-run cap: take a transaction-scoped advisory lock on the
        # scope, then re-count under it. The lock auto-releases when this
        # transaction commits/rolls back, so a concurrent spawn for the same
        # scope blocks until we've created (or declined) ours.
        _lock_scope = parent_run_id or conversation_id or cap_scope
        if run_limit is not None and _lock_scope:
            # Cast the namespace to int4 explicitly so it resolves to the
            # pg_advisory_xact_lock(int4, int4) overload alongside hashtext()
            # (which returns int4) — an untyped bind param could otherwise bind
            # as int8 and fail overload resolution.
            await own_db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "CAST(:ns AS integer), hashtext(:scope))"
                ),
                {"ns": _SUB_AGENT_CAP_LOCK_NS, "scope": f"sub_agent_cap:{_lock_scope}"},
            )
            active = await count_active_sub_agent_executions(
                own_db,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
                cap_scope=cap_scope,
            )
            if active >= run_limit:
                return None

        task = SubAgentTask(
            user_id=uuid.UUID(user_id),
            parent_agent_id=uuid.UUID(parent_agent_id),
            target_agent_id=uuid.UUID(target_agent_id),
            task_description=task_description,
            conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
            status="pending",
        )
        own_db.add(task)
        await own_db.flush()

        assignment_run_id: str | None = None
        if parent_run_id or own_run_trigger:
            try:
                from app.api.v1.helpers import (
                    resolve_child_run_is_company_managed,
                )
                from app.models.assignment import AssignmentRun

                # Mirror the parent run's shared-visibility flag. Company-managed
                # parent runs now carry an owner/run-as user_id, so deriving
                # visibility from user_id alone would mark this delegation child
                # private and hide it from shared run/status views. Fall back to
                # the legacy ownerless signal only if the parent can't be loaded.
                child_company_managed = await resolve_child_run_is_company_managed(
                    own_db, parent_run_id, fallback=not user_id
                )

                from app.services.health.cycles import inherited_cycle_context

                child_run = AssignmentRun(
                    assignment_id=None,
                    agent_id=uuid.UUID(target_agent_id),
                    # A Platform Health cycle's delegated work charges the same cycle (#8261).
                    trigger_context=await inherited_cycle_context(own_db, parent_run_id),
                    user_id=uuid.UUID(user_id) if user_id and not _health_alone else None,
                    is_company_managed=child_company_managed,
                    parent_run_id=uuid.UUID(parent_run_id) if parent_run_id else None,
                    trigger_payload={
                        "trigger_type": own_run_trigger or "delegation",
                        "parent_agent_id": parent_agent_id,
                        "parent_run_id": parent_run_id,
                        "task": task_description[:TRUNCATE_ERROR],
                    },
                    status="pending",
                    # The task as given, in at creation rather than when the
                    # worker finishes: a delegation waiting on a worker is
                    # exactly the run whose prompt someone needs to read.
                    # Replaced by the full conversation when it ends.
                    messages=[{"role": "user", "content": task_description}],
                )
                own_db.add(child_run)
                await own_db.flush()
                assignment_run_id = str(child_run.id)
            except Exception as run_err:
                logger.warning(
                    "Failed to create child AssignmentRun for delegation: %s", format_exception_message(run_err)
                )

        task.metadata_json = {
            **(task.metadata_json or {}),
            "sub_agent_task_id": str(task.id),
            "assignment_run_id": assignment_run_id,
            "parent_run_id": parent_run_id,
            **({RUN_TRIGGER_TYPE_KEY: own_run_trigger} if own_run_trigger and assignment_run_id else {}),
            **({"cap_scope": cap_scope} if cap_scope else {}),
            **(extra_metadata or {}),
            **({HEALTH_ALONE_KEY: True} if _health_alone else {}),
        }

        try:
            from app.services.delegation import log_delegation

            await log_delegation(
                own_db,
                delegation_type="background_task",
                user_id=user_id,
                parent_agent_id=parent_agent_id,
                target_agent_id=target_agent_id,
                description=task_description[:TRUNCATE_QUESTION],
                status="pending",
                conversation_id=conversation_id,
                metadata={
                    "sub_agent_task_id": str(task.id),
                    "assignment_run_id": assignment_run_id,
                },
            )
        except Exception as deleg_err:
            logger.warning("Failed to log background_task delegation: %s", format_exception_message(deleg_err))

        # Serialize scope_headers + working-file refs into the execution
        # payload so the reconciler can rebuild them on the worker (payload
        # must be JSON-safe — no in-process objects or callables).
        import json as _json

        _scope_headers_json = _json.dumps(scope_headers) if scope_headers else None
        file_ids: list[str] = []
        if file_context is not None and hasattr(file_context, "list_files"):
            file_ids = [ref.file_id for ref in file_context.list_files()]

        # Capture the parent's trace context (Langfuse trace_id + root span
        # id, and the local trace id) in a JSON-safe form.  The execution
        # runs in a separate worker asyncio task, so the parent's
        # ContextVar-based trace context is not inherited — without this the
        # delegation appears as a disconnected root trace instead of nesting
        # under the orchestrator.
        parent_trace_context: dict | None = None
        try:
            from app.services.langfuse_tracing import capture_trace_context

            parent_trace_context = capture_trace_context()
        except Exception:
            logger.debug(
                "Failed to capture parent trace context for sub-agent task",
                exc_info=True,
            )

        from app.services import background_checkpoint
        from app.services.orchestration_engine import create_execution, queued_deadline

        # Effective wall-clock budget: inherited from the parent assignment
        # run when provided; chat spawns (``timeout_seconds is None``) derive
        # it from the target agent's iteration cap so a 100-iteration
        # specialist gets the same budget as chatting with that agent
        # directly, not the historical 900 s default.  ``<= 0`` (parent
        # timeout disabled) sets no orchestration deadline — otherwise
        # ``sweep_deadlines`` would dead-letter a task whose budget the
        # operator deliberately turned off (same contract as assignment runs).
        if timeout_seconds is None:
            timeout_seconds = await resolve_chat_sub_agent_timeout(
                own_db,
                target_agent_id,
                evaluation_chain=bool((extra_metadata or {}).get(EVALUATION_CHAIN_KEY)),
            )
        _effective_timeout = resolve_sub_agent_timeout(timeout_seconds)
        # Counted from the task's START (``queued_deadline``): until a worker
        # claims it, ``deadline_at`` is the queue-wait bound; the claim sets it
        # to start + budget, so a task queued behind a saturated pool keeps its
        # whole budget. None (timeout disabled): no deadline at all.
        deadline_at, start_budget = queued_deadline(
            datetime.now(UTC),
            None
            if _effective_timeout is None
            else _effective_timeout + settings.orchestration_grace_seconds,
        )
        await create_execution(
            own_db,
            adapter="sub_agent",
            payload={
                "task_id": str(task.id),
                "user_id": user_id,
                "target_agent_id": target_agent_id,
                "task_description": task_description,
                "assignment_run_id": assignment_run_id,
                "delegation_depth": str(delegation_depth),
                "scope_headers_json": _scope_headers_json,
                "file_ids": file_ids,
                "parent_run_id": parent_run_id,
                "conversation_id": conversation_id,
                # The fan-out cap's scope when neither key above is set.
                "cap_scope": cap_scope,
                "parent_trace_context": parent_trace_context,
                "visited_agents": _chain,
                # Inherited parent budget. Assignment spawns store
                # the RAW inherited value (not the wait_for-resolved one)
                # so the worker re-resolves with the same helper. Chat
                # spawns store the already-derived
                # ``chat_loop_timeout_seconds`` int so deadline_at and
                # wait_for share one number.
                "timeout_seconds": timeout_seconds,
                "max_iterations": max_iterations,
                # The delegating run's enforced Scope(s): JSON
                # element bindings, ``None`` when that run enforces none. The
                # worker reads it only with the marker (``queued_task_scope``).
                INHERITED_SCOPE_KEY: element_scope,
                ENFORCED_SCOPE_MARKER_KEY: True,
                # A delegation made while answering an A2A partner's task
                # stays one: the worker reopens the scope.
                PARTNER_TASK_PAYLOAD_KEY: in_partner_task(),
                # And the partner it answers: the run history the task may
                # read is that partner's own (``a2a.partner_runs``).
                PARTNER_PRINCIPAL_PAYLOAD_KEY: partner_principal_payload(),
                **start_budget,
            },
            deadline_at=deadline_at,
            dedup_key=f"sub_agent_task:{task.id}",
            # Its first run and every resume it may make after a loss without a
            # word: the streak of losses with no progress is the
            # task's own refusal, the engine's attempts the hard bound. An
            # operator's bound above the old floor is kept as it is.
            max_attempts=background_checkpoint.task_max_attempts(settings.orchestration_default_max_attempts),
        )

        await own_db.commit()
        await own_db.refresh(task)

    from app.services.orchestration_engine import notify_orchestration_wake

    await notify_orchestration_wake()

    # Real-time UI signal: without this, the chat banner and the Background
    # Tasks screen only learn about the new task on their 30s idle poll.
    await publish_task_event(
        user_id,
        "spawned",
        str(task.id),
        conversation_id=conversation_id,
        status="pending",
    )

    logger.info(
        "Created sub-agent task %s: %s → %s (parent_run=%s, depth=%d)",
        task.id,
        parent_agent_id,
        target_agent_id,
        parent_run_id,
        delegation_depth,
    )
    return task


def _serialize_task_status(task: SubAgentTask) -> dict:
    """Shape a ``SubAgentTask`` into the status dict the tools/LLM consume.

    Single source of truth for the status payload used by the batched
    ``get_task_statuses`` (and everything built on it).
    """
    return {
        "task_id": str(task.id),
        "status": task.status,
        "target_agent_id": str(task.target_agent_id),
        "task_description": task.task_description[:TRUNCATE_LABEL],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "has_result": task.result is not None,
        # The files the task produced - what the parent's tools put
        # into its own FileContext so it can read them back.
        "result_file_ids": task_result_file_ids(task),
    }


async def get_task_statuses(
    task_ids: Iterable[str],
    user_id: str,
) -> dict[str, dict | None]:
    """Fetch statuses for several tasks in a SINGLE session / query.

    Returns ``{task_id: status_dict_or_None}`` keyed by the *input* id strings
    (a malformed id, or one not owned by ``user_id``, maps to ``None``). It
    replaces N per-id lookups (each opening its own session and query) with one
    ``IN (...)`` lookup, so a poll over a batch costs a single round-trip
    instead of one per task. In
    typical use the id set is small (a poll is bounded by the per-run fan-out
    cap), so it stays well under the driver bind-parameter ceiling without
    chunking; the function itself does not enforce that bound, so a caller
    passing an unusually large id set is responsible for keeping it sane.
    """
    from app.core.database import async_session

    ids = [str(t).strip() for t in task_ids if str(t).strip()]
    statuses: dict[str, dict | None] = dict.fromkeys(ids)
    if not ids:
        return statuses

    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return statuses

    uuid_by_str: dict[str, uuid.UUID] = {}
    for tid in ids:
        parsed = safe_uuid(tid)
        if parsed is not None:
            uuid_by_str[tid] = parsed
    if not uuid_by_str:
        return statuses

    async with async_session() as own_db:
        rows = (
            (
                await own_db.execute(
                    select(SubAgentTask).where(
                        SubAgentTask.id.in_(set(uuid_by_str.values())),
                        SubAgentTask.user_id == user_uuid,
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {task.id: task for task in rows}
    for tid, parsed in uuid_by_str.items():
        task = by_id.get(parsed)
        if task is not None:
            statuses[tid] = _serialize_task_status(task)
    return statuses


async def get_task_labels(
    task_ids: Iterable[str],
    user_id: str,
) -> dict[str, dict[str, str | None]]:
    """Human-readable identity of several tasks, in a SINGLE session.

    Returns ``{task_id: {"agent_name": ..., "task_description": ...}}`` keyed
    by the *input* id strings, for the tasks owned by ``user_id`` that exist;
    a malformed, unknown or foreign id is simply absent. This is what the
    agentic loop names a still-running delegation by in the user-facing park
    message — the same ``Agent.name`` + ``task_description`` pair the
    Background Tasks banner shows — so the two surfaces describe one task in
    one vocabulary instead of the chat quoting a raw ``sub_agent_tasks`` UUID.

    Two queries (tasks, then the target agents' names) on one session: the id
    set is bounded by the per-run fan-out cap, and a lookup failure is the
    caller's to absorb — the label is cosmetic, the park itself must not fail
    because a name could not be read.
    """
    from app.core.database import async_session

    ids = [str(t).strip() for t in task_ids if str(t).strip()]
    labels: dict[str, dict[str, str | None]] = {}
    if not ids:
        return labels

    try:
        user_uuid = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return labels

    uuid_by_str: dict[str, uuid.UUID] = {}
    for tid in ids:
        parsed = safe_uuid(tid)
        if parsed is not None:
            uuid_by_str[tid] = parsed
    if not uuid_by_str:
        return labels

    async with async_session() as own_db:
        rows = (
            (
                await own_db.execute(
                    select(
                        SubAgentTask.id,
                        SubAgentTask.target_agent_id,
                        SubAgentTask.task_description,
                    ).where(
                        SubAgentTask.id.in_(set(uuid_by_str.values())),
                        SubAgentTask.user_id == user_uuid,
                    )
                )
            )
            .all()
        )
        if not rows:
            return labels
        agent_ids = {row[1] for row in rows if row[1] is not None}
        agent_names: dict[uuid.UUID, str] = {}
        if agent_ids:
            agent_rows = (
                await own_db.execute(
                    select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
                )
            ).all()
            agent_names = {row[0]: row[1] for row in agent_rows}

    by_id = {row[0]: row for row in rows}
    for tid, parsed in uuid_by_str.items():
        row = by_id.get(parsed)
        if row is None:
            continue
        description = (row[2] or "").strip() or None
        labels[tid] = {
            "agent_name": agent_names.get(row[1]) or None,
            "task_description": (
                description[:TRUNCATE_LABEL] if description else None
            ),
        }
    return labels


# Adaptive status-wait tuning for the ``check_task_status`` tool. The tool
# blocks until every queried task reaches a terminal state (so the LLM gets the
# outcome the moment it is ready instead of blind-sleeping a fixed interval),
# capped by a per-call budget so a long-running task still yields control back
# to the agent for re-evaluation.
# Terminal-state set is single-sourced in ``app.tools.wait_policy`` (the
# dependency-free leaf) so the ``wait`` primitive's partial-wake detection and
# this poll gate can never diverge on what "finished" means.
_STATUS_TERMINAL: frozenset[str] = WAIT_TERMINAL_STATUSES
_STATUS_POLL_INTERVAL = 2.0  # seconds between polls
_STATUS_WAIT_BUDGET = 20.0  # max seconds to block within one check call


async def wait_for_tasks(
    task_ids: Iterable[str],
    user_id: str,
    *,
    budget: float = _STATUS_WAIT_BUDGET,
    poll_interval: float = _STATUS_POLL_INTERVAL,
) -> dict[str, dict | None]:
    """Poll task statuses, returning as soon as ALL reach a terminal state.

    Returns ``{task_id: status_dict_or_None}``. Returns **immediately** once
    every task is terminal (completed / failed / cancelled / incomplete).
    Unknown / not-found ids stay pending so a typo cannot look like "all
    done". A parent agent never blind-sleeps when its background tasks have
    already finished (the previous fixed 15s sleep meant a task that
    terminated seconds ago still cost the agent the full delay).

    ``budget`` bounds the *polling loop*, not an individual status read: no new
    poll iteration starts once it has elapsed (the in-flight batch read always
    completes first), so the wall-clock wait is at most ``budget`` plus one
    status read. A task still running when the budget elapses is reported with
    its live status; the agent can call again to keep waiting.

    Each poll fetches every status in one batched query (``get_task_statuses``)
    rather than one session/query per task, so a multi-task wait stays a single
    round-trip per poll.
    """
    ids = [str(t).strip() for t in task_ids if str(t).strip()]
    if not ids:
        return {}

    # Module-level ``time`` (not a local import) so a test can drive the
    # budget with a virtual clock instead of racing the runner's wall clock.
    deadline = time.monotonic() + max(0.0, budget)
    while True:
        statuses = await get_task_statuses(ids, user_id)
        pending = [
            tid
            for tid, s in statuses.items()
            if s is None or s.get("status") not in _STATUS_TERMINAL
        ]
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            return statuses
        await asyncio.sleep(min(poll_interval, remaining))


async def get_task_result(
    db: AsyncSession,  # kept for API compat; uses own session internally
    task_id: str,
    user_id: str,
) -> dict | None:
    """Get the full result of a completed sub-agent task."""
    from app.core.database import async_session

    try:
        task_uuid = uuid.UUID(task_id)
        user_uuid = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return None

    async with async_session() as own_db:
        result = await own_db.execute(
            select(SubAgentTask).where(
                SubAgentTask.id == task_uuid,
                SubAgentTask.user_id == user_uuid,
            )
        )
        task = result.scalar_one_or_none()
    if not task:
        return None
    return {
        "task_id": str(task.id),
        # The task's own run (``get_run`` reads it), not the task id.
        "run_id": (task.metadata_json or {}).get("assignment_run_id"),
        "status": task.status,
        "target_agent_id": str(task.target_agent_id),
        "task_description": task.task_description,
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        # Files the task produced: the text result only carries their
        # ``[[FILE:id]]`` markers; the parent needs the ids to load the refs.
        "result_file_ids": task_result_file_ids(task),
    }


async def get_task_result_by_id(task_id: str) -> str | None:
    """Get the raw result string of a task by ID only (no user filter).

    Used internally by integration tools (e.g. ``import_opencti_dashboard``)
    that run within an authenticated agent loop but don't have the user_id
    in scope.  Returns the result text or *None*.
    """
    from app.core.database import async_session

    try:
        tid = uuid.UUID(task_id)
    except (ValueError, AttributeError, TypeError):
        return None
    async with async_session() as own_db:
        row = await own_db.execute(
            select(SubAgentTask).where(
                SubAgentTask.id == tid,
                SubAgentTask.status == "completed",
            )
        )
        task = row.scalar_one_or_none()
    return task.result if task else None


async def _execution_liveness(db: AsyncSession, tasks: list) -> dict[str, dict]:
    """For each pending / running task of *tasks*, its execution's state, id and last move.

    Keyed by task id: ``execution_state`` (``none`` when the task has no
    execution row), ``execution_id`` and ``last_progress_at`` (the execution's
    ``updated_at``), so a listing shows whether a running task still has a live
    execution. Best effort: an unreadable read leaves the listing as before.
    """
    from app.models.orchestration import OrchestrationExecution

    open_ids = [str(t.id) for t in tasks if getattr(t, "status", None) in ("pending", "running")]
    if not open_ids:
        return {}
    found: dict[str, dict] = {}
    try:
        for batch in chunked([f"sub_agent_task:{i}" for i in open_ids], _RUN_ID_QUERY_CHUNK):
            rows = (
                await db.execute(
                    select(
                        OrchestrationExecution.dedup_key,
                        OrchestrationExecution.state,
                        OrchestrationExecution.id,
                        OrchestrationExecution.updated_at,
                    ).where(OrchestrationExecution.dedup_key.in_(batch))
                )
            ).all()
            for key, state, execution_id, moved_at in rows:
                found[str(key).removeprefix("sub_agent_task:")] = {
                    "execution_state": state,
                    "execution_id": str(execution_id) if execution_id else None,
                    "last_progress_at": moved_at.isoformat() if moved_at else None,
                }
    except Exception:
        logger.debug("Background task execution liveness not read", exc_info=True)
        return {}
    none = {"execution_state": "none", "execution_id": None, "last_progress_at": None}
    return {i: found.get(i, none) for i in open_ids}


def task_liveness_suffix(entry: dict) -> str:
    """The run and execution a listed pending / running task line names, or ``""``."""
    state = entry.get("execution_state")
    if not state:
        return ""
    run_id = entry.get("assignment_run_id") or "none"
    text = f" | run {run_id[:8] if run_id != 'none' else run_id} | execution {state}"
    if entry.get("last_progress_at"):
        text += f", last progress {entry['last_progress_at']}"
    if state not in ("pending", "running"):
        text += " (no live execution: the sweep settles it)"
    return text


async def list_user_tasks(
    db: AsyncSession,  # kept for API compat; uses own session internally
    user_id: str,
    *,
    parent_agent_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List recent sub-agent tasks for a user.

    Opens an independent session (like ``get_task_statuses`` / ``get_task_result``):
    the ``list_background_tasks`` tool can run concurrently with other DB-touching
    tools under ``asyncio.gather``, and a SQLAlchemy ``AsyncSession`` forbids
    concurrent operations, so the shared chat/assignment session must not be used.
    """
    from app.core.database import async_session

    query = select(SubAgentTask).where(
        SubAgentTask.user_id == uuid.UUID(user_id),
    )
    if parent_agent_id:
        query = query.where(SubAgentTask.parent_agent_id == uuid.UUID(parent_agent_id))
    if status:
        query = query.where(SubAgentTask.status == status)
    query = query.order_by(SubAgentTask.created_at.desc()).limit(limit)

    async with async_session() as own_db:
        result = await own_db.execute(query)
        tasks = result.scalars().all()
        liveness = await _execution_liveness(own_db, list(tasks))

    from app.services.ai_hold import held_reason_of

    listed: list[dict] = []
    for t in tasks:
        live = liveness.get(str(t.id))
        entry = {
            "task_id": str(t.id),
            "parent_agent_id": str(t.parent_agent_id),
            "target_agent_id": str(t.target_agent_id),
            "task_description": t.task_description,
            "status": t.status,
            "result": t.result,
            "error": t.error,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "has_result": t.result is not None,
        }
        if live is not None:
            # A pending / running task: its run and whether its execution is
            # still live, so a zombie is visible in the listing.
            entry["assignment_run_id"] = (getattr(t, "metadata_json", None) or {}).get("assignment_run_id")
            entry.update(live)
        held = (
            held_reason_of(getattr(t, "metadata_json", None))
            if t.status == "pending"
            else None
        )
        if held:
            entry["held_reason"] = held
        listed.append(entry)
    return listed


def _schedule_parent_wake_if_needed(
    task: SubAgentTask | None,
    *,
    agent_name: str,
    terminal_status: str,
) -> None:
    """Resume the idle parent chat when a child becomes terminal.

    In-turn parking only covers tasks spawned in the *current*
    parent loop. Once that turn has ended, completion notices sit in the
    thread with nobody reading them. This schedules a separate parent
    continuation (own DB session, no sub-agent lease) unless the task
    was cancelled, delivery-suppressed, incomplete, or already woken.
    """
    from app.services.parent_wake import schedule_parent_wake, should_wake_parent

    if not should_wake_parent(task) or task is None:
        return
    schedule_parent_wake(
        task_id=str(task.id),
        conversation_id=str(task.conversation_id),
        user_id=str(task.user_id),
        agent_name=agent_name,
        terminal_status=terminal_status,
    )


def _delivery_suppressed(task: SubAgentTask | None) -> bool:
    """True when *task*'s proactive result delivery must be skipped.

    A task is suppressed when it was cancelled outright, or when
    :func:`cancel_tasks_for_conversation` flagged it while it was already
    running (the task is left to finish, but its result must not land in the
    conversation the user aborted).  A missing task is treated as suppressed —
    there is nothing meaningful to deliver.
    """
    if task is None:
        return True
    if task.status == "cancelled":
        return True
    return bool((task.metadata_json or {}).get("delivery_suppressed"))


def _already_cancelled(task: SubAgentTask | None) -> bool:
    """True when *task* was killed and a worker must NOT overwrite its status.

    ``cancel_sub_agent_task`` (the Background Tasks "Kill" action) flips a running
    task to ``cancelled`` from a separate session while the worker is mid-flight.
    The reconciler does not abort an in-flight adapter when its lease is lost, so
    the worker keeps running and would otherwise resurrect the task by writing
    ``completed`` (with a result) / ``failed`` at the end. Re-checking the
    freshly-loaded row lets the worker discard its now-orphaned outcome and keep
    the cancellation authoritative.

    Distinct from :func:`_delivery_suppressed`: that also covers chat-aborted
    *running* tasks (``delivery_suppressed`` set, status still ``running``) which
    must still complete normally — only their delivery is skipped. Here ONLY a
    ``cancelled`` row is preserved.
    """
    return task is not None and task.status == "cancelled"


REPLACED_ATTEMPT_ERROR = (
    "This attempt was replaced by a later one after its lease was lost."
)


def _replaced_by(task: SubAgentTask | None, place: tuple[int, int] | None) -> bool:
    if task is None or place is None:
        return False
    return task_attempt_fence(task.metadata_json) > place


def _ended_elsewhere(
    task: SubAgentTask | None, place: tuple[int, int] | None = None
) -> SubAgentTaskOutcome | None:
    """Why this attempt must not write its ending on *task*, or ``None``:
    the outcome the task already ended with, or that a later attempt
    replaced this one.

    A worker whose lease was lost keeps running while the engine re-runs the
    task, and the dead-letter can fail it meanwhile: an ending already on the
    row stands, and an attempt the re-run replaced (its *place*,
    ``task_attempt_place``, below ``TASK_ATTEMPT_KEY``) writes none - the task ends as the attempt the
    engine runs it with ends. This attempt then skips what follows an ending
    too (delivery, wake, continuation); the engine discards its result, the
    lease being another worker's. Read on the row this attempt re-read under
    its lock; a ``cancelled`` row is :func:`_already_cancelled`'s.
    """
    if task is None:
        return None
    if task.status not in WAIT_TERMINAL_STATUSES:
        if _replaced_by(task, place):
            return SubAgentTaskOutcome(status="failed", error=REPLACED_ATTEMPT_ERROR)
        return None
    if task.status == "failed":
        return SubAgentTaskOutcome(status="failed", error=task.error)
    if task.status == "cancelled":
        return SubAgentTaskOutcome(status="cancelled")
    return SubAgentTaskOutcome(status="completed")


async def _locked_task_for_ending(
    db, task_id: str, place: tuple[int, int] | None
) -> tuple[SubAgentTask | None, SubAgentTaskOutcome | None]:
    """*task_id* re-read under its lock, right before this attempt writes an
    ending once its first commit released the row, and why it must not
    (:func:`_ended_elsewhere`)."""
    task = (
        await db.execute(
            select(SubAgentTask)
            .where(SubAgentTask.id == uuid.UUID(task_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return task, _ended_elsewhere(task, place)


async def _cancel_partner_tasks(
    db, conv_uuid, conversation_id: str, stopped_at: datetime | None = None
) -> None:
    """Cancel partner tasks this conversation left outstanding (spec 011 FR-005).

    A chat turn ending must not cancel — a delegation handed back is what ends
    that turn — so the user abandoning the conversation is the only boundary an
    outstanding partner task has.

    Best-effort: a partner that cannot be told must not turn a cancel cascade
    into a failure of its own.

    The obligation is committed before any partner is called, in a session of
    its own: this cascade runs detached from the request that asked, and the
    caller's session commits only once every partner call has returned, so a
    hard kill in between would otherwise lose it. The client sweep re-sends
    what is still owed. The Stop itself is acknowledged before this
    cascade starts - ``chat._spawn_cancel_cascade`` keeps database work off
    the acknowledgement - so a kill in the cascade's first round trips loses
    the partner cancel with the local half, which is not durable yet either.

    The same commit records the Stop itself (``Conversation.stopped_at``): a
    partner answer recorded just before it has no outstanding row to stamp,
    and the partner wake reads this before delivering. A stopped
    wake turn reads it before starting a follow-up, which is at least one
    bridge poll after the Stop and so, in practice, after this commit.

    ``stopped_at`` is when the user pressed Stop, taken on the acknowledgement
    path (``chat._spawn_cancel_cascade``), not when this cascade reached the
    database: a turn the user starts in between delegates work the Stop never
    covered, so the record, the stamp and the cancel all stop at that moment
    (``submitted_before``). The record only ever moves forward, so two
    cascades finishing out of order keep the latest Stop.

    If recording it fails, the partners are still told. Skipping the cancel
    would guarantee the work keeps running; telling them without the record
    only leaves the cancel exposed to a crash, and a database that refused
    the record is one the conclusion below cannot commit to either.
    """
    from sqlalchemy import DateTime, func, literal, update

    from app.a2a.client.delegation import cancel_outstanding_for, mark_cancel_owed
    from app.core.database import async_session
    from app.models.chat import Conversation

    stop = stopped_at or datetime.now(UTC)
    try:
        async with async_session() as mark_db:
            await mark_db.execute(
                update(Conversation)
                .where(Conversation.id == conv_uuid)
                # ``updated_at`` pinned: a Stop is not activity, and bumping
                # it would reorder the conversation list. GREATEST ignores
                # the NULL of a conversation never stopped.
                .values(
                    stopped_at=func.greatest(
                        Conversation.stopped_at,
                        literal(stop, DateTime(timezone=True)),
                    ),
                    updated_at=Conversation.updated_at,
                )
            )
            await mark_cancel_owed(
                mark_db, conversation_ref=conv_uuid, submitted_before=stop
            )
            await mark_db.commit()
    except Exception:
        logger.warning(
            "Could not record the stop and the partner cancels owed for "
            "conversation %s",
            conversation_id,
            exc_info=True,
        )
    try:
        await cancel_outstanding_for(
            db, conversation_ref=conv_uuid, submitted_before=stop
        )
    except Exception:
        logger.warning(
            "Could not cancel partner tasks for conversation %s",
            conversation_id,
            exc_info=True,
        )


async def cancel_tasks_for_conversation(
    db: AsyncSession | None,  # kept for API compat; uses own session internally
    conversation_id: str,
    *,
    stopped_at: datetime | None = None,
) -> int:
    """Cancel in-flight sub-agent tasks spawned from *conversation_id*.

    Cascade for a user-cancelled chat turn:

    * **Pending** tasks (not yet claimed by a worker) are cancelled outright —
      the ``sub_agent`` orchestration execution is transitioned to the FSM's
      terminal ``cancelled`` state via the engine API and the ``SubAgentTask``
      row is marked ``cancelled``.
    * **Running** tasks are left to finish (interrupting a worker mid-flight
      is not safe), but their proactive delivery is suppressed via a
      ``delivery_suppressed`` metadata flag so the result never lands in the
      conversation the user just aborted.

    Best-effort by contract: returns the number of tasks transitioned to
    ``cancelled``; an invalid conversation id is a no-op.
    """
    from app.core.database import async_session

    try:
        conv_uuid = uuid.UUID(conversation_id)
    except (ValueError, AttributeError, TypeError):
        return 0

    from app.models.orchestration import TERMINAL_EXECUTION_STATES
    from app.services.orchestration_engine import (
        cancel_execution,
        get_execution_by_dedup_key,
    )

    cancelled_count = 0
    # (owner_user_id, task_id) pairs flipped to ``cancelled`` — the real-time
    # UI events are published only AFTER the commit below so a rollback can't
    # have broadcast a state that never became true.
    _cancelled_events: list[tuple[str, str]] = []
    async with async_session() as own_db:
        result = await own_db.execute(
            select(SubAgentTask).where(
                SubAgentTask.conversation_id == conv_uuid,
                SubAgentTask.status.in_(["pending", "running"]),
            )
        )
        tasks = result.scalars().all()

        # Before the early return below, and deliberately: a conversation that
        # delegated to a partner and spawned no LOCAL background work has no
        # sub-agent tasks at all, which is the common case for an A2A
        # delegation — and returning here skipped the partner entirely, so
        # abandoning the conversation left its work running.
        await _cancel_partner_tasks(
            own_db, conv_uuid, conversation_id, stopped_at=stopped_at
        )

        if not tasks:
            await own_db.commit()
            return 0

        now = datetime.now(UTC)
        for task in tasks:
            # Suppress proactive delivery for every in-flight task — including
            # ones already running, which are left to finish.
            task.metadata_json = {
                **(task.metadata_json or {}),
                "delivery_suppressed": True,
            }
            if task.status != "pending":
                continue
            execution = await get_execution_by_dedup_key(
                own_db, f"sub_agent_task:{task.id}"
            )
            if (
                execution is not None
                and execution.state not in TERMINAL_EXECUTION_STATES
            ):
                if execution.state == "running":
                    # A worker claimed the execution between our snapshot and
                    # now — treat it as running: leave it to finish with
                    # delivery suppressed.
                    continue
                await cancel_execution(own_db, execution_id=execution.id)
            task.status = "cancelled"
            task.error = "Cancelled by user."
            task.completed_at = now
            cancelled_count += 1
            _cancelled_events.append((str(task.user_id), str(task.id)))

        await own_db.commit()

    # Real-time UI signal: the aborting client refreshes its own
    # caches, but other tabs / the Background Tasks screen would otherwise
    # keep showing the killed task(s) until the 30s idle poll.
    for _owner_id, _task_id in _cancelled_events:
        await publish_task_event(
            _owner_id,
            "cancelled",
            _task_id,
            conversation_id=conversation_id,
            status="cancelled",
        )

    logger.info(
        "Cancel cascade for conversation %s: %d task(s) cancelled, %d task(s) "
        "left running with delivery suppressed",
        conversation_id,
        cancelled_count,
        len(tasks) - cancelled_count,
    )
    return cancelled_count


#: Who stopped a background task, as its ``error`` and its child run's read (#2721).
CANCELLED_BY_USER_REASON = "Cancelled by user."
CANCELLED_BY_ADMIN_REASON = "Cancelled by an administrator."


def cancelled_by_run_reason(run_id: str | uuid.UUID | None) -> str:
    """The ``error`` of a task an agent stopped from one of its runs (#2721)."""
    if run_id:
        return f"Cancelled by parent run {run_id}."
    return "Cancelled by the agent that spawned it."


#: The statuses of a child run its task's cancel still ends.
_CHILD_RUN_LIVE_STATUSES = ("pending", "delayed", "running", "awaiting_human")


async def _cancel_child_run(db: AsyncSession, task: SubAgentTask, *, now: datetime, error: str) -> bool:
    """End the child run of a cancelled *task* as ``cancelled``, never ``failed`` (#2721).

    A cancel the parent, the user or an administrator chose is no failure of
    the member: written ``failed``, it raised ``member_failing`` on a healthy
    member and counted in its failure rate. ``cancelled`` stays out of the
    member verdict (``run_outcome`` reads it as ``other``). A delegated child
    run (no assignment) then raises ``run.failed`` with ``reason: "cancelled"``
    and ``cancelled: true`` once *db* commits, so a listener sees the ending
    and can tell it from a failure. Locks the run after the task (the
    worker's order). Returns whether a run was ended here.
    """
    run_id = (task.metadata_json or {}).get("assignment_run_id")
    if not run_id:
        return False
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (ValueError, TypeError):
        return False
    from app.models.assignment import AssignmentRun

    child_run = (
        await db.execute(select(AssignmentRun).where(AssignmentRun.id == run_uuid).with_for_update())
    ).scalar_one_or_none()
    if child_run is None or child_run.status not in _CHILD_RUN_LIVE_STATUSES:
        return False
    child_run.status = "cancelled"
    child_run.error = error
    child_run.completed_at = now
    if child_run.assignment_id is None:
        _publish_delegated_run_ended_after_commit(db, child_run, "cancelled", error)
    return True


async def cancel_sub_agent_task(
    task_id: str,
    requester_id: str,
    *,
    is_admin: bool = False,
    spawned_by: str | None = None,
    cancelled_by_run_id: str | None = None,
) -> dict | None:
    """Cancel a single background sub-agent task (owner or admin).

    ``spawned_by`` (an agent id) narrows the gate to the tasks that agent
    spawned: the agent-facing ``cancel_background_task`` tool passes its own
    id, so an agent stops only its own tasks of the acting user (#2217), and
    ``cancelled_by_run_id``, the run it cancels from, which the error names.

    Best-effort, durable cancellation:
      * The ``sub_agent`` orchestration execution is transitioned to the FSM
        terminal ``cancelled`` state, releasing any lease — a worker already
        running it bails cleanly on its next transition (``LeaseLostError``);
        we never hard-kill an in-flight asyncio task mid-step.
      * The ``SubAgentTask`` row is marked ``cancelled`` with delivery
        suppressed so a late result never lands in the conversation.
      * The linked child ``AssignmentRun`` (if still active) is ended
        ``cancelled``, never ``failed``, its error naming who cancelled
        (:func:`_cancel_child_run`, #2721).
      * The ``background_task`` delegation audit row is closed.

    Returns a small status dict, or ``None`` when the task does not exist or the
    requester (non-admin) does not own it.
    """
    from app.core.database import async_session
    from app.models.orchestration import TERMINAL_EXECUTION_STATES
    from app.services.orchestration_engine import (
        cancel_execution,
        get_execution_by_dedup_key,
    )

    try:
        task_uuid = uuid.UUID(task_id)
        requester_uuid = uuid.UUID(requester_id)
    except (ValueError, AttributeError, TypeError):
        return None

    now = datetime.now(UTC)
    async with async_session() as own_db:
        # Lock the task row FOR UPDATE so the terminal-status check below is
        # atomic against the worker's terminal write (``_execute_sub_agent_task``
        # re-reads FOR UPDATE before writing completed/failed). Without the lock
        # there is a TOCTOU window: we could read ``running`` here, the worker
        # could commit ``completed``/``failed``, and our blind UPDATE below would
        # then clobber that legitimate terminal state back to ``cancelled`` (and
        # lose an already-delivered result). With the lock the two serialize: we
        # either win it, set ``cancelled``, and the worker observes it and bails,
        # or the worker wins, commits its terminal state, and we then read it and
        # return ``already_terminal`` without overwriting. Lock order is
        # task -> run (the child AssignmentRun is locked further down), matching
        # the worker, so the two can't deadlock.
        task = (
            await own_db.execute(
                select(SubAgentTask)
                .where(SubAgentTask.id == task_uuid)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        # Hide existence from non-owners (owner-or-admin gate).
        is_owner = task.user_id == requester_uuid
        if not (is_owner or is_admin):
            return None
        if spawned_by is not None and str(task.parent_agent_id) != str(spawned_by):
            return None

        if task.status in ("completed", "failed", "cancelled", "incomplete"):
            return {
                "task_id": str(task.id),
                "status": task.status,
                "already_terminal": True,
            }

        was_running = task.status == "running"

        # 1. Cancel the durable execution (releases the lease; a running worker
        #    bails on its next transition rather than clobbering the row).
        execution = await get_execution_by_dedup_key(
            own_db, f"sub_agent_task:{task.id}"
        )
        if (
            execution is not None
            and execution.state not in TERMINAL_EXECUTION_STATES
        ):
            await cancel_execution(own_db, execution_id=execution.id)

        # 2. Mark the task cancelled + suppress proactive delivery.
        if is_admin and not is_owner:
            reason = CANCELLED_BY_ADMIN_REASON
        elif spawned_by is not None:
            reason = cancelled_by_run_reason(cancelled_by_run_id or (task.metadata_json or {}).get("parent_run_id"))
        else:
            reason = CANCELLED_BY_USER_REASON
        task.status = "cancelled"
        task.error = reason
        task.completed_at = now
        task.metadata_json = {
            **(task.metadata_json or {}),
            "delivery_suppressed": True,
        }
        if not was_running and task.started_at is None:
            # Killed before any attempt ran it: nothing was spent. A
            # running task's worker keeps the ledger its run recorded.
            _record_no_turn_ledger(task)

        # 3. End the linked child AssignmentRun as cancelled if still active.
        await _cancel_child_run(own_db, task, now=now, error=reason)

        # 4. Close the delegation audit row.
        await _update_task_delegation(
            own_db, str(task.id), "cancelled", error="Cancelled."
        )

        # Snapshot the owner/conversation before the commit — used for the
        # post-commit SSE publish below.
        _owner_id = str(task.user_id)
        _conv_id = str(task.conversation_id) if task.conversation_id else None

        await own_db.commit()

    # Real-time UI signal: the killing client refreshes its own
    # caches via the mutation, but other tabs / the chat banner would
    # otherwise keep showing the task as running until the 30s idle poll.
    await publish_task_event(
        _owner_id,
        "cancelled",
        str(task_uuid),
        conversation_id=_conv_id,
        status="cancelled",
    )

    logger.info(
        "Sub-agent task %s cancelled (was_running=%s, by_admin=%s)",
        task_id,
        was_running,
        is_admin and not is_owner,
    )
    return {
        # Echo the canonical/validated UUID (not the raw request string), so the
        # response matches the already-terminal path above (``str(task.id)``).
        "task_id": str(task_uuid),
        "status": "cancelled",
        "was_running": was_running,
    }


# Cap how many run ids go into one matching query. Each batch expands to two
# JSONB ``IN (...)`` predicates, so a very large delete (admin purge-all, agent
# deletion with many runs) would otherwise blow past the driver bind-parameter
# ceiling (asyncpg caps a statement at 32767 params); matched tasks are unioned
# in Python (keyed by id, since one task can match on two of the deleted runs).
_RUN_ID_QUERY_CHUNK = 1000


def _mark_task_cancelled(task: SubAgentTask, *, now: datetime, error: str) -> None:
    """Flip one task row to ``cancelled`` with its delivery suppressed.

    The in-session half every cancel path shares: a late result from a worker
    that has not noticed yet must never land in the conversation, so the flag
    travels with the status in the same write.
    """
    task.status = "cancelled"
    task.error = error
    task.completed_at = now
    task.metadata_json = {
        **(task.metadata_json or {}),
        "delivery_suppressed": True,
    }


async def cancel_sub_agent_tasks_for_scope(
    db: AsyncSession, *, user_id: uuid.UUID | None, error: str
) -> tuple[int, int]:
    """Cancel every pending / running background task in scope (emergency stop).

    ``user_id`` narrows the sweep to one owner; ``None`` is the whole platform.
    Unlike :func:`cancel_tasks_for_conversation`, RUNNING tasks are stopped
    too: their durable execution is cancelled (the lease is released, so the
    worker bails with ``LeaseLostError`` or on its fast cancel poll) and the
    row is flipped so the result is discarded — an emergency stop is the one
    caller for which "let it finish" is the wrong answer.

    Flushes but does not commit — the caller owns the transaction. Returns
    ``(tasks_cancelled, executions_cancelled)``.
    """
    from app.models.orchestration import (
        TERMINAL_EXECUTION_STATES,
        OrchestrationExecution,
    )
    from app.services.orchestration_engine import cancel_execution

    query = select(SubAgentTask).where(SubAgentTask.status.in_(("pending", "running")))
    if user_id is not None:
        query = query.where(SubAgentTask.user_id == user_id)
    tasks = (await db.execute(query)).scalars().all()
    now = datetime.now(UTC)
    executions_cancelled = 0
    # One lookup per batch of tasks, not one per task: a platform-wide stop
    # over a large fleet must stay a matter of a few statements. The cancel
    # itself stays per execution — ``cancel_execution`` takes the row lock
    # that makes a racing worker's terminal write safe.
    execution_ids: list[uuid.UUID] = []
    for batch in chunked([str(t.id) for t in tasks], _RUN_ID_QUERY_CHUNK):
        execution_ids.extend(
            (
                await db.execute(
                    select(OrchestrationExecution.id).where(
                        OrchestrationExecution.dedup_key.in_(
                            [f"sub_agent_task:{tid}" for tid in batch]
                        ),
                        OrchestrationExecution.state.notin_(
                            tuple(TERMINAL_EXECUTION_STATES)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    for execution_id in execution_ids:
        cancelled = await cancel_execution(db, execution_id=execution_id)
        if cancelled is not None and cancelled.state == "cancelled":
            executions_cancelled += 1
    for task in tasks:
        _mark_task_cancelled(task, now=now, error=error)
        await _update_task_delegation(db, str(task.id), "cancelled", error=error)
    if tasks:
        await db.flush()
        # Each task's own run stops with it, cancelled, never failed (#2721):
        # after the flush wrote the task rows, so the run lock is taken after
        # the task's, the worker's order.
        for task in tasks:
            await _cancel_child_run(db, task, now=now, error=error)
        await db.flush()
        logger.warning(
            "Emergency stop cancelled %d background task(s) (%d execution(s)) "
            "for scope %s",
            len(tasks),
            executions_cancelled,
            user_id or "platform",
        )
    return len(tasks), executions_cancelled


#: Why a background task that never started stopped with the run that spawned it.
PARENT_ENDED_TASK_REASON = "Cancelled: the run that spawned it ended before it started."


async def cancel_unstarted_tasks_of_ended_run(db: AsyncSession, run_id: str | uuid.UUID) -> list[str]:
    """Cancel the background tasks *run_id* spawned that no worker ever started.

    A run that ends (its final answer, its deadline) leaves nobody to collect a
    task still waiting in the queue: started later, its result would have no
    consumer. Each such task ends ``cancelled`` with
    :data:`PARENT_ENDED_TASK_REASON`, its delivery suppressed, its execution
    cancelled and its delegation row concluded. A task already running keeps
    running and delivers as it always did. Commits; returns the cancelled ids.
    """
    from app.models.orchestration import TERMINAL_EXECUTION_STATES
    from app.services.orchestration_engine import cancel_execution, get_execution_by_dedup_key

    tasks = (
        (
            await db.execute(
                select(SubAgentTask)
                .where(
                    SubAgentTask.status == "pending",
                    SubAgentTask.started_at.is_(None),
                    SubAgentTask.metadata_json["parent_run_id"].astext == str(run_id),
                )
                .order_by(SubAgentTask.id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not tasks:
        return []
    now = datetime.now(UTC)
    for task in tasks:
        execution = await get_execution_by_dedup_key(db, f"sub_agent_task:{task.id}")
        if execution is not None and execution.state not in TERMINAL_EXECUTION_STATES:
            await cancel_execution(db, execution_id=execution.id)
        _mark_task_cancelled(task, now=now, error=PARENT_ENDED_TASK_REASON)
        _record_no_turn_ledger(task)
        await _update_task_delegation(db, str(task.id), "cancelled", error=PARENT_ENDED_TASK_REASON)
    await db.commit()
    ids = [str(task.id) for task in tasks]
    logger.info("Run %s ended: cancelled %d background task(s) it spawned that never started: %s", run_id, len(ids), ids)
    return ids


async def lock_sub_agent_tasks_for_runs(
    db: AsyncSession, run_ids: Iterable[str | uuid.UUID]
) -> None:
    """Lock the live background tasks linked to *run_ids*, in primary-key order.

    The executor starting a task locks its ``SubAgentTask`` row, then marks the
    task's ``AssignmentRun`` running. A cancel cascade that wrote the runs first
    and only then reached ``mark_sub_agent_tasks_cancelled_for_runs`` took the
    same pair in the other order, and PostgreSQL aborted one of the two: the
    partner's ``tasks/cancel`` answered a bare 500. Called before the
    cascade's first run write, it makes the cancel queue behind a starting task
    instead. Same linkage as ``mark_sub_agent_tasks_cancelled_for_runs``; reads
    nothing back and writes nothing.
    """
    ids = [str(rid) for rid in run_ids if rid is not None]
    for batch_ids in chunked(ids, _RUN_ID_QUERY_CHUNK):
        await db.execute(
            select(SubAgentTask.id)
            .where(
                SubAgentTask.status.in_(("pending", "running")),
                or_(
                    SubAgentTask.metadata_json["assignment_run_id"].astext.in_(batch_ids),
                    SubAgentTask.metadata_json["parent_run_id"].astext.in_(batch_ids),
                ),
            )
            .order_by(SubAgentTask.id)
            .with_for_update()
        )


#: Why a background task stopped with its run, as the task's ``error`` reads.
#: Each cascade says its own cause: a partner's cancel read as a deletion sent
#: operators looking for a run that was never deleted.
RUN_DELETED_TASK_REASON = "Cancelled because its run was deleted."
#: The same for a run stopped rather than deleted, when the stop names no cause.
RUN_CANCELLED_TASK_REASON = "Cancelled because its run was cancelled."


async def mark_sub_agent_tasks_cancelled_for_runs(
    db: AsyncSession,
    run_ids: Iterable[str | uuid.UUID],
    *,
    reason: str = RUN_DELETED_TASK_REASON,
) -> int:
    """Cancel background tasks whose linked run is being deleted or stopped.

    ``reason`` is the task's ``error`` and its delegation row's: a deletion by
    default, the cancel's own cause when a cascade stops the run instead
    (``a2a.tasks.cascade_cancel``).

    Companion to ``orchestration_engine.cancel_executions_for_runs`` (which
    cancels the durable executions): this flips the decoupled ``SubAgentTask``
    rows and closes their delegation audit rows so deleting a run never leaves a
    background task stranded on ``pending`` / ``running`` in the tasks table
    (the tasks live in their own table and are not otherwise correlated to runs).

    Linkage is the run id stored on the task's ``metadata_json`` as either
    ``assignment_run_id`` (the task's own child run) or ``parent_run_id`` (the
    orchestrator run it was spawned from). Runs in the caller's transaction
    (flushes via the ORM unit of work; does not commit). Returns the count
    cancelled.
    """
    ids = [str(rid) for rid in run_ids if rid is not None]
    if not ids:
        return 0
    now = datetime.now(UTC)
    # Match in bounded batches; dedupe by task id because a single task can link
    # to two of the deleted runs (its own ``assignment_run_id`` and its
    # orchestrator ``parent_run_id``) and would otherwise be cancelled twice.
    tasks_by_id: dict[uuid.UUID, SubAgentTask] = {}
    for batch_ids in chunked(ids, _RUN_ID_QUERY_CHUNK):
        rows = (
            await db.execute(
                select(SubAgentTask).where(
                    SubAgentTask.status.in_(("pending", "running")),
                    or_(
                        SubAgentTask.metadata_json["assignment_run_id"].astext.in_(
                            batch_ids
                        ),
                        SubAgentTask.metadata_json["parent_run_id"].astext.in_(
                            batch_ids
                        ),
                    ),
                )
            )
        ).scalars().all()
        for task in rows:
            tasks_by_id[task.id] = task
    cancelled = 0
    for task in tasks_by_id.values():
        _mark_task_cancelled(task, now=now, error=reason)
        await _update_task_delegation(
            db,
            str(task.id),
            "cancelled",
            error="Run deleted." if reason == RUN_DELETED_TASK_REASON else reason,
        )
        cancelled += 1
    if cancelled:
        # A task stopped with its PARENT run: its own run, outside the runs
        # being stopped, ends cancelled too, never failed (#2721); a run the
        # caller deletes or cancels itself is left to the caller. The run read
        # autoflushes the task rows first: task lock first, the worker's order.
        stopped = set(ids)
        for task in tasks_by_id.values():
            if str((task.metadata_json or {}).get("assignment_run_id")) not in stopped:
                await _cancel_child_run(db, task, now=now, error=reason)
        logger.info(
            "Cancelled %d background task(s) for %d deleted run(s)",
            cancelled,
            len(ids),
        )
    return cancelled


def _task_execution_key():
    """The ``dedup_key`` of a task's orchestration execution, as SQL (``sub_agent_task:<id>``)."""
    from sqlalchemy import String, cast, literal

    return literal("sub_agent_task:") + cast(SubAgentTask.id, String)


#: Every status a background task takes (``SubAgentTask.status``), in the order of its life.
BACKGROUND_TASK_STATUSES = ("pending", "running", "completed", "incomplete", "failed", "cancelled")

#: The server orders of the Background Tasks list (#9378 slice 8e): ``created``
#: newest first (the default), ``last_progress`` the execution's last move first,
#: ``status`` and ``from`` / ``to`` (the delegating and the target agent's name);
#: each with its ``_asc`` or ``_desc`` reverse.
BACKGROUND_TASK_SORTS = (
    "created",
    "created_asc",
    "last_progress",
    "last_progress_asc",
    "status",
    "status_desc",
    "from",
    "from_desc",
    "to",
    "to_desc",
)


def background_task_statuses(status: str | None) -> list[str]:
    """The statuses a ``status`` filter names: one value, or several comma-separated.

    Blanks are dropped and duplicates removed, the order kept. Raises
    ``ValueError`` on a value that names no status (``","``) or an unknown one,
    which the endpoints answer as a 422.
    """
    if status is None:
        return []
    values = list(dict.fromkeys(part.strip() for part in status.split(",") if part.strip()))
    if not values:
        raise ValueError("The status filter names no status.")
    unknown = [value for value in values if value not in BACKGROUND_TASK_STATUSES]
    if unknown:
        raise ValueError(f"Unknown background task status: {', '.join(unknown)}.")
    return values


def _background_tasks_query(
    *,
    requester_id: str,
    is_admin: bool,
    scope: str,
    status: str | None,
    agent_id: str | None,
    search: str | None = None,
    fleet_id: uuid.UUID | None = None,
    liveness: str | None = None,
):
    """Build the filtered ``SELECT`` shared by list / count / bulk-delete.

    Honours the scope (``all`` is admin-only; otherwise the requester's own
    tasks), an optional status, an optional ``agent_id`` that matches the
    parent OR target agent, and an optional free-text ``search`` over the task
    description and the parent/target agent names. Returns the query, or
    ``None`` when the requester id is malformed (callers treat that as an empty
    result). Keeping a single builder means the list, its total count, and
    "select all matching" deletes can never drift apart.

    ``fleet_id`` keeps the tasks whose parent or target agent is a current
    home member of the fleet, resolved when the query runs.

    ``shared`` and ``company`` (admin only, the caller gates them) split every
    user's tasks that are not the requester's by the agent that delegated them:
    ``company`` when that parent agent is company-managed, ``shared``
    otherwise. With ``mine`` they partition ``all`` exactly (``user_id`` and
    ``parent_agent_id`` are NOT NULL, ``agents.is_company_managed`` read with
    IS TRUE), so the four counts of the Background Tasks filter reconcile.

    ``liveness='no_live_execution'`` keeps the pending / running tasks whose
    orchestration execution is missing or no longer pending / running,
    the rule of ``task_liveness_suffix``: the zombies the sweep settles.
    """
    query = select(SubAgentTask)
    if not (is_admin and scope == "all"):
        try:
            requester_uuid = uuid.UUID(requester_id)
        except (ValueError, TypeError):
            return None
    if scope in ("shared", "company"):
        if not is_admin:
            return None
        company_agents = select(Agent.id).where(Agent.is_company_managed.is_(True))
        query = query.where(SubAgentTask.user_id != requester_uuid)
        if scope == "company":
            query = query.where(SubAgentTask.parent_agent_id.in_(company_agents))
        else:
            query = query.where(SubAgentTask.parent_agent_id.not_in(company_agents))
    elif not (is_admin and scope == "all"):
        query = query.where(SubAgentTask.user_id == requester_uuid)
    if status:
        # One status or several (``running,pending``, the toolbar's Status pill).
        # A malformed value is refused by the endpoints before it gets here; the
        # service keeps the plain split so a direct caller is never a 500.
        statuses = [part.strip() for part in status.split(",") if part.strip()]
        if len(statuses) == 1:
            query = query.where(SubAgentTask.status == statuses[0])
        elif statuses:
            query = query.where(SubAgentTask.status.in_(statuses))
    if agent_id:
        try:
            aid = uuid.UUID(agent_id)
            query = query.where(
                or_(
                    SubAgentTask.parent_agent_id == aid,
                    SubAgentTask.target_agent_id == aid,
                )
            )
        except (ValueError, TypeError):
            # Optional filter: a malformed agent_id is ignored (fail open, no
            # 500) rather than narrowing the list — log for diagnosability.
            logger.debug(
                "Ignoring invalid agent_id filter %r in background-task query",
                agent_id,
            )
    if fleet_id is not None:
        from app.services.fleet_views import fleet_scope_clause

        query = query.where(
            or_(
                fleet_scope_clause(SubAgentTask.parent_agent_id, fleet_id),
                fleet_scope_clause(SubAgentTask.target_agent_id, fleet_id),
            )
        )
    if search and search.strip():
        term = f"%{search.strip()}%"
        # Match the description or either agent's name (name match via an Agent
        # id subquery so it works without a join in the count/delete paths too).
        name_ids = select(Agent.id).where(Agent.name.ilike(term))
        query = query.where(
            or_(
                SubAgentTask.task_description.ilike(term),
                SubAgentTask.parent_agent_id.in_(name_ids),
                SubAgentTask.target_agent_id.in_(name_ids),
            )
        )
    if liveness == "no_live_execution":
        from app.models.orchestration import (
            NON_TERMINAL_EXECUTION_STATES,
            OrchestrationExecution,
        )

        # Correlated on the task alone: the last-progress sort joins
        # ``orchestration_executions`` onto the outer query, and auto-correlation
        # would then strip this subquery's only FROM (a 500 on
        # ``?liveness=no_live_execution&sort=last_progress``).
        live = (
            select(OrchestrationExecution.id)
            .where(
                OrchestrationExecution.dedup_key == _task_execution_key(),
                OrchestrationExecution.state.in_(NON_TERMINAL_EXECUTION_STATES),
            )
            .correlate(SubAgentTask)
            .exists()
        )
        query = query.where(SubAgentTask.status.in_(("pending", "running")), ~live)
    return query


async def list_background_tasks(
    db: AsyncSession,
    *,
    requester_id: str,
    is_admin: bool = False,
    scope: str = "mine",
    status: str | None = None,
    agent_id: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    fleet_id: uuid.UUID | None = None,
    liveness: str | None = None,
    sort: str | None = None,
) -> list[dict]:
    """List background sub-agent tasks enriched with agent names + run linkage.

    ``scope='all'`` (admin only — callers must gate) lists every user's tasks;
    otherwise only the requester's. ``agent_id`` matches the parent OR target
    agent; ``search`` matches the description or agent names. ``offset`` enables
    server-side pagination (paired with ``count_background_tasks``). The result
    is a superset of ``list_user_tasks`` (adds agent names/icons, conversation +
    run linkage, and ``user_id``) so it can back both the chat banner and the
    Background Tasks control screen.

    ``liveness`` narrows as ``_background_tasks_query`` does;
    ``sort='last_progress'`` orders by the time the Updated cell shows (an open
    task's execution move, else its end, start or creation), most recent first,
    ``last_progress_asc`` its exact reverse; the default stays newest created
    first.
    """
    from app.models.agent import Agent
    from app.services.ai_hold import held_code_of

    query = _background_tasks_query(
        requester_id=requester_id,
        is_admin=is_admin,
        scope=scope,
        status=status,
        agent_id=agent_id,
        search=search,
        fleet_id=fleet_id,
        liveness=liveness,
    )
    if query is None:
        return []
    query = _order_background_tasks(query, sort)
    if offset:
        query = query.offset(offset)
    query = query.limit(limit)
    tasks = (await db.execute(query)).scalars().all()
    if not tasks:
        return []

    agent_ids = {t.parent_agent_id for t in tasks} | {
        t.target_agent_id for t in tasks
    }
    agent_rows = (
        await db.execute(
            select(Agent.id, Agent.name, Agent.icon).where(Agent.id.in_(agent_ids))
        )
    ).all()
    agents = {row[0]: (row[1], row[2]) for row in agent_rows}
    live_by_task = await _execution_liveness(db, list(tasks))

    out: list[dict] = []
    for t in tasks:
        meta = t.metadata_json or {}
        parent = agents.get(t.parent_agent_id)
        target = agents.get(t.target_agent_id)
        out.append(
            {
                "task_id": str(t.id),
                "user_id": str(t.user_id),
                "parent_agent_id": str(t.parent_agent_id),
                "parent_agent_name": parent[0] if parent else None,
                "parent_agent_icon": parent[1] if parent else None,
                "target_agent_id": str(t.target_agent_id),
                "target_agent_name": target[0] if target else None,
                "target_agent_icon": target[1] if target else None,
                "task_description": t.task_description,
                "status": t.status,
                "result": t.result,
                "error": t.error,
                "conversation_id": (
                    str(t.conversation_id) if t.conversation_id else None
                ),
                "assignment_run_id": meta.get("assignment_run_id"),
                "parent_run_id": meta.get("parent_run_id"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": (
                    t.completed_at.isoformat() if t.completed_at else None
                ),
                "has_result": t.result is not None,
            }
        )
        # A pending / running task: its execution's state, id and last move,
        # so Operations > Background Tasks shows a zombie as one.
        live = live_by_task.get(str(t.id))
        if live is not None:
            out[-1].update(live)
        # Waiting while agentic AI is unavailable: the refusal code the
        # Background Tasks screen's Held badge takes. Absent when not held.
        held = held_code_of(meta) if t.status == "pending" else None
        if held:
            out[-1]["held_reason"] = held
    return out


def _task_moved_at(progress: Any) -> Any:
    """When a task last moved, as SQL: the time the Updated cell of the list shows.

    The listing reads an execution's ``updated_at`` for an open task only
    (``_execution_liveness``), and the cell falls back to the task's end, start
    then creation (``taskUpdatedAt`` of the Background Tasks page). The sort
    takes the same value, never null since ``created_at`` always holds one.
    *progress* is the aliased execution the query is outer-joined on.
    """
    from sqlalchemy import case, func

    open_progress = case(
        (SubAgentTask.status.in_(("pending", "running")), progress.updated_at),
        else_=None,
    )
    return func.coalesce(
        open_progress,
        SubAgentTask.completed_at,
        SubAgentTask.started_at,
        SubAgentTask.created_at,
    )


def _order_background_tasks(query, sort: str | None):
    """ORDER BY of the Background Tasks list for its ``sort`` (``BACKGROUND_TASK_SORTS``).

    The creation time then the id break ties, so paging never skips or repeats
    a task. ``last_progress`` orders by the time the Updated cell shows
    (``_task_moved_at``), the id breaking ties in the same direction, so
    ``last_progress_asc`` is its exact reverse; ``from`` and ``to`` by the
    delegating and the target agent's name, a deleted agent last. An unknown
    value keeps the default, newest created first.
    """
    from sqlalchemy.orm import aliased

    newest = (SubAgentTask.created_at.desc(), SubAgentTask.id.desc())
    if sort == "created_asc":
        return query.order_by(SubAgentTask.created_at.asc(), SubAgentTask.id.asc())
    if sort in ("last_progress", "last_progress_asc"):
        from app.models.orchestration import OrchestrationExecution

        # ``dedup_key`` is unique: the outer join adds no row. An alias, so the
        # liveness filter's ``NOT EXISTS`` on the same table stays its own.
        progress = aliased(OrchestrationExecution, name="task_execution")
        moved_at = _task_moved_at(progress)
        if sort == "last_progress_asc":
            order = (moved_at.asc(), SubAgentTask.id.asc())
        else:
            order = (moved_at.desc(), SubAgentTask.id.desc())
        return query.outerjoin(
            progress,
            progress.dedup_key == _task_execution_key(),
        ).order_by(*order)
    if sort in ("status", "status_desc"):
        order = SubAgentTask.status.desc() if sort == "status_desc" else SubAgentTask.status.asc()
        return query.order_by(order, *newest)
    if sort in ("from", "from_desc", "to", "to_desc"):
        # Outer join on an alias: a task whose agent was deleted stays listed, last.
        side = aliased(Agent, name="from_agent" if sort.startswith("from") else "to_agent")
        column = SubAgentTask.parent_agent_id if sort.startswith("from") else SubAgentTask.target_agent_id
        name = side.name.desc() if sort.endswith("_desc") else side.name.asc()
        return query.outerjoin(side, side.id == column).order_by(name.nulls_last(), *newest)
    return query.order_by(*newest)


async def background_task_status_counts(
    db: AsyncSession,
    *,
    requester_id: str,
    is_admin: bool = False,
    scope: str = "mine",
    agent_id: str | None = None,
    search: str | None = None,
    fleet_id: uuid.UUID | None = None,
    liveness: str | None = None,
) -> dict[str, int]:
    """How many background tasks each status holds under the list's other filters.

    The counts of the Status pill of Operations > Background Tasks (#9378 slice
    8e): the same builder as the list and its count, never the ``status`` filter
    itself, so each number says what one click on that status would show.
    """
    from sqlalchemy import func

    query = _background_tasks_query(
        requester_id=requester_id,
        is_admin=is_admin,
        scope=scope,
        status=None,
        agent_id=agent_id,
        search=search,
        fleet_id=fleet_id,
        liveness=liveness,
    )
    if query is None:
        return {}
    base = query.subquery()
    rows = (await db.execute(select(base.c.status, func.count()).group_by(base.c.status))).all()
    return {str(value): int(n) for value, n in rows if value}


async def count_background_tasks(
    db: AsyncSession,
    *,
    requester_id: str,
    is_admin: bool = False,
    scope: str = "mine",
    status: str | None = None,
    agent_id: str | None = None,
    search: str | None = None,
    fleet_id: uuid.UUID | None = None,
    liveness: str | None = None,
) -> int:
    """Count background tasks matching the same filters as ``list_background_tasks``.

    Powers the "Showing X-Y of Z" pagination total and the "select all N
    matching" bulk-selection affordance on the Background Tasks screen.
    """
    from sqlalchemy import func

    query = _background_tasks_query(
        requester_id=requester_id,
        is_admin=is_admin,
        scope=scope,
        status=status,
        agent_id=agent_id,
        search=search,
        fleet_id=fleet_id,
        liveness=liveness,
    )
    if query is None:
        return 0
    result = await db.execute(select(func.count()).select_from(query.subquery()))
    return int(result.scalar_one())


async def delete_background_tasks(
    *,
    requester_id: str,
    is_admin: bool = False,
    task_ids: list[str] | None = None,
    all_matching_filters: bool = False,
    scope: str = "mine",
    status: str | None = None,
    agent_id: str | None = None,
    search: str | None = None,
) -> int:
    """Delete background sub-agent tasks (owner or admin).

    Two modes mirror the assignment-run bulk delete:
      * ``task_ids`` – delete the given tasks. Non-admins are scoped to their
        own tasks (ids they don't own are silently skipped).
      * ``all_matching_filters`` – delete every task matching the scope / status
        / agent filters (same semantics as the list endpoint).

    Any task still ``pending`` / ``running`` first has its durable orchestration
    execution cancelled (lease released — a running worker bails cleanly on its
    next transition rather than resurrecting a now-deleted row) and its
    delegation audit row closed, so a delete never strands an execution or
    wedges the per-run fan-out counter. Returns the number of tasks deleted.
    """
    from sqlalchemy import delete as sql_delete

    from app.core.database import async_session
    from app.models.orchestration import TERMINAL_EXECUTION_STATES
    from app.services.orchestration_engine import (
        cancel_execution,
        get_execution_by_dedup_key,
    )

    try:
        requester_uuid = uuid.UUID(requester_id)
    except (ValueError, TypeError):
        return 0

    async with async_session() as db:
        if task_ids:
            valid_ids = [tid for tid in (safe_uuid(t) for t in task_ids) if tid]
            if not valid_ids:
                return 0
            query = select(SubAgentTask).where(SubAgentTask.id.in_(valid_ids))
            # Owner-or-admin gate: non-admins can only delete their own tasks.
            if not is_admin:
                query = query.where(SubAgentTask.user_id == requester_uuid)
        elif all_matching_filters:
            query = _background_tasks_query(
                requester_id=requester_id,
                is_admin=is_admin,
                scope=scope,
                status=status,
                agent_id=agent_id,
                search=search,
            )
            if query is None:
                return 0
        else:
            return 0

        tasks = (await db.execute(query)).scalars().all()
        if not tasks:
            return 0

        # Release any in-flight executions before the rows disappear so a worker
        # can't resurrect a deleted task and no execution is left orphaned.
        for task in tasks:
            if task.status not in ("pending", "running"):
                continue
            execution = await get_execution_by_dedup_key(
                db, f"sub_agent_task:{task.id}"
            )
            if (
                execution is not None
                and execution.state not in TERMINAL_EXECUTION_STATES
            ):
                await cancel_execution(db, execution_id=execution.id)
            await _update_task_delegation(
                db, str(task.id), "cancelled", error="Task deleted."
            )

        ids_to_delete = [task.id for task in tasks]
        # Chunk the delete so a very large admin purge can't blow past the
        # asyncpg bind-parameter ceiling on the ``IN (...)`` predicate.
        for batch in chunked(ids_to_delete, _RUN_ID_QUERY_CHUNK):
            await db.execute(
                sql_delete(SubAgentTask).where(SubAgentTask.id.in_(batch))
            )

        await db.commit()

    logger.info(
        "Deleted %d background task(s) (by_admin=%s, mode=%s)",
        len(ids_to_delete),
        is_admin,
        "ids" if task_ids else "filters",
    )
    return len(ids_to_delete)


# ── Background execution ────────────────────────────────────────────────────


async def count_active_sub_agent_executions(
    db: AsyncSession | None = None,
    *,
    parent_run_id: str | None = None,
    conversation_id: str | None = None,
    cap_scope: str | None = None,
) -> int:
    """Count in-flight (pending/running) sub-agent executions for a scope.

    Durably enforces the per-run concurrency cap in ``build_sub_agent_tools``
    (replaces the old in-process counter + ``on_complete_cb``). Scoped by
    ``parent_run_id`` when available, else ``conversation_id``, else
    ``cap_scope``; returns 0 when none is set (no scope to bound).

    When ``db`` is ``None`` an independent session is opened internally. The
    ``spawn_background_task`` fast-path pre-check passes no session because the
    sub-agent tools execute concurrently under ``asyncio.gather`` (one LLM turn
    can request several spawns at once) and a SQLAlchemy ``AsyncSession`` forbids
    concurrent operations — sharing the chat/assignment session raises "This
    session is provisioning a new connection; concurrent operations are not
    permitted". The atomic gate in ``create_sub_agent_task`` instead passes its
    own lock-holding session explicitly, so the count runs inside the same
    advisory-lock transaction (the race-free authoritative check).
    """
    if db is None:
        from app.core.database import async_session

        async with async_session() as own_db:
            return await count_active_sub_agent_executions(
                own_db,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
                cap_scope=cap_scope,
            )

    from sqlalchemy import func

    from app.models.orchestration import OrchestrationExecution

    conditions = [
        OrchestrationExecution.adapter == "sub_agent",
        OrchestrationExecution.state.in_(["pending", "running"]),
    ]
    if parent_run_id:
        conditions.append(
            OrchestrationExecution.payload["parent_run_id"].astext == str(parent_run_id)
        )
    elif conversation_id:
        conditions.append(
            OrchestrationExecution.payload["conversation_id"].astext
            == str(conversation_id)
        )
    elif cap_scope:
        conditions.append(OrchestrationExecution.payload["cap_scope"].astext == cap_scope)
    else:
        return 0
    return (
        await db.execute(
            select(func.count())
            .select_from(OrchestrationExecution)
            .where(*conditions)
        )
    ).scalar() or 0


# How long a background task may sit pending/running with NO execution row before
# the orphan sweep fails it. Compared against ``created_at`` so a task whose
# execution row is momentarily mid-creation is never failed prematurely. A task
# whose execution is already terminal is failed regardless of age (the execution
# is definitively done). Generous on purpose (>= one full task timeout + slack).
_ORPHAN_TASK_GRACE_SECONDS = _SUB_AGENT_TASK_TIMEOUT + 300

# Execution states that mean the worker is gone but did NOT cleanly finish the
# task — the task must be reconciled to ``failed``. ``completed`` is deliberately
# excluded: a completed execution means the body already wrote the result, so the
# task must never be overwritten. ``pending`` / ``running`` mean the engine still
# owns it.
_ORPHAN_RECOVERABLE_EXEC_STATES = ("failed", "dead_letter", "cancelled")


def _completed_quiet_seconds() -> int:
    """How long a ``completed`` execution may leave its task unsettled before the sweep settles it.

    The execution's body writes the task's ending before the engine completes
    the execution, so a task still ``pending`` / ``running`` this long after its
    execution last moved had its ending lost (a worker stop between the two
    writes, a terminal write that raised): the stale-run threshold, as for an
    orphaned worker (:func:`orphan_wait_seconds`).
    """
    return orphan_wait_seconds()


def _execution_names(execution_id: Any, state: str, task: SubAgentTask) -> str:
    """The execution and run a sweep reason names."""
    run_id = (getattr(task, "metadata_json", None) or {}).get("assignment_run_id") or "none"
    return f"execution {execution_id or 'unknown'} ({state}), run {run_id}"


#: ``metadata_json`` key of a running task whose execution lost its lease with
#: no attempt left while its worker may still be running: when, what
#: the engine last heard of the attempt, and until when the task is left to it.
ORPHANED_KEY = "orphaned"


def orphan_wait_seconds() -> int:
    """How long a task whose lease lapsed is left to its worker before it is failed.

    The stale-run threshold (``STALE_IN_PROCESS_RUN_SECONDS``, 900 s): the
    orphan sweep runs on every scheduler tick (60 s), so a task nobody works on
    is failed, and its work item claims released, within it.
    """
    raw = settings.stale_in_process_run_seconds
    return max(60, int(raw)) if isinstance(raw, (int, float)) and raw > 0 else 900


def mark_task_orphaned(task: SubAgentTask, execution, expiry, *, now: datetime | None = None) -> dict:
    """Record on the running *task* that the engine gave up on its execution
    while its worker may still be running, and return the record.

    The worker runs under ``asyncio.timeout`` to the execution's deadline
    (``orchestration_reconciler._execution_timeout``; ``worker_job_timeout``
    from its start when there is none), and writes its own ending when that
    fires: a task not ended by then, plus the engine's grace, has no worker
    left, and ``recover_orphaned_sub_agent_tasks`` fails it at ``fail_after``.
    """
    now = now or datetime.now(UTC)
    deadline = getattr(execution, "deadline_at", None)
    if deadline is None:
        started = task.started_at or now
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        deadline = started + timedelta(seconds=settings.worker_job_timeout)
    elif deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    # Bounded by the orphan threshold: a worker silent for a whole
    # lease and this long after it is gone, whatever the task's own deadline,
    # so its claims go back to the queue within it.
    fail_after = min(
        deadline + timedelta(seconds=settings.orchestration_grace_seconds),
        now + timedelta(seconds=orphan_wait_seconds()),
    )
    record = {
        "at": now.isoformat(),
        "fail_after": fail_after.isoformat(),
        "attempt": int(getattr(expiry, "attempt", 0) or 0),
        "owner": getattr(expiry, "owner", None),
        "last_heartbeat_at": expiry.last_heartbeat_at.isoformat(),
        "silent_seconds": int(expiry.silent_seconds),
        "execution_error": getattr(execution, "last_error", None),
    }
    task.metadata_json = {**(task.metadata_json or {}), ORPHANED_KEY: record}
    from app.services.rollout_counters import count_orphaned

    count_orphaned()
    return record


def orphan_record(task: SubAgentTask | None) -> dict | None:
    """The :data:`ORPHANED_KEY` record of *task*, or ``None``."""
    record = (getattr(task, "metadata_json", None) or {}).get(ORPHANED_KEY)
    return record if isinstance(record, dict) else None


def orphan_fail_after(record: dict) -> datetime | None:
    """When the orphan sweep may fail the task the *record* is on, or ``None`` when unreadable."""
    try:
        when = datetime.fromisoformat(str(record["fail_after"]))
    except (KeyError, TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def orphan_failure_reason(record: dict) -> str:
    """What the sweep says of a task whose orphaned worker never wrote an ending."""
    attempt = record.get("attempt")
    silent = record.get("silent_seconds")
    heard = record.get("last_heartbeat_at")
    return (
        "Background task abandoned: its worker lost its lease"
        + (f" on attempt {attempt}" if attempt else "")
        + (f" (last heartbeat {silent} s before, at {heard})" if heard is not None else "")
        + " and never reported an ending within the task's own time"
        + f" (given until {record.get('fail_after')})."
    )


async def recover_orphaned_sub_agent_tasks(
    db: AsyncSession, *, limit: int = 500
) -> int:
    """Fail background tasks stranded in pending/running with no live execution.

    Time-based safety net for the ``sub_agent_tasks`` table (the analogue of
    ``autonomous._cleanup_stale_assignment_runs`` for ``AssignmentRun``). It
    closes the desync where an execution reaches a terminal state via
    ``engine.fail()`` — a reconciler timeout, an adapter crash, or a
    non-retryable failure — WITHOUT the adapter's ``on_dead_letter`` cleanup
    running, so the ``SubAgentTask`` (and its linked run + delegation) is left
    stuck and keeps inflating the per-scope fan-out counter. Also recovers legacy
    tasks that never got an execution row at all.

    A task is orphaned when it is ``pending`` / ``running`` AND either:
      * its execution (dedup key ``sub_agent_task:<id>``) is in a non-completed
        terminal state (``failed`` / ``dead_letter`` / ``cancelled``), or
      * it has no execution row and has been waiting past
        ``_ORPHAN_TASK_GRACE_SECONDS`` (so a task whose execution is still being
        created is never failed prematurely).

    Tasks whose execution is still ``pending`` / ``running`` are left to the
    engine/reconciler; a ``completed`` execution is never touched (its body
    already wrote the result). Flushes but does not commit — the caller owns the
    transaction. Returns the number of tasks failed.
    """
    from app.models.orchestration import OrchestrationExecution

    candidates = (
        (
            await db.execute(
                select(SubAgentTask)
                .where(SubAgentTask.status.in_(("pending", "running")))
                .order_by(SubAgentTask.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return 0

    # Resolve each candidate's execution state in bounded batches (one task ->
    # one dedup key), unioned into a map. Bounded to stay well under the driver
    # bind-parameter ceiling even if a backlog of orphans accumulates.
    key_to_task = {f"sub_agent_task:{t.id}": t for t in candidates}
    state_by_key: dict[str, str] = {}
    # The execution id and when it last moved, for the reason and the
    # completed-but-unsettled case.
    exec_by_key: dict[str, tuple[Any, datetime | None]] = {}
    for batch in chunked(list(key_to_task), _RUN_ID_QUERY_CHUNK):
        rows = (
            await db.execute(
                select(
                    OrchestrationExecution.dedup_key,
                    OrchestrationExecution.state,
                    OrchestrationExecution.id,
                    OrchestrationExecution.updated_at,
                ).where(OrchestrationExecution.dedup_key.in_(batch))
            )
        ).all()
        for row in rows:
            key, state, *rest = tuple(row)
            state_by_key[key] = state
            exec_by_key[key] = (
                rest[0] if rest else None,
                rest[1] if len(rest) > 1 else None,
            )

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=_ORPHAN_TASK_GRACE_SECONDS)
    quiet_cutoff = now - timedelta(seconds=_completed_quiet_seconds())
    recovered = 0
    for key, task in key_to_task.items():
        state = state_by_key.get(key)
        execution_id, moved_at = exec_by_key.get(key, (None, None))
        credit_attempts = 0
        if state == "completed":
            # Its execution completed and the task never got its ending:
            # left alone while the body may still be writing it, settled once
            # the execution has been quiet past the threshold. An execution
            # whose last move is unknown is left alone.
            if moved_at is not None and moved_at.tzinfo is None:
                moved_at = moved_at.replace(tzinfo=UTC)
            if moved_at is None or moved_at > quiet_cutoff:
                continue
            await _settle_unsettled_task(
                db, task, names=_execution_names(execution_id, state, task), now=now
            )
            recovered += 1
            continue
        if state is None:
            # No execution row: only an aged task is a true orphan — a young one
            # may simply be mid-creation, so leave it for a later tick.
            created = task.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created is not None and created > cutoff:
                continue
            reason = (
                "Background task abandoned: no orchestration execution and stuck "
                "beyond the recovery grace period."
            )
        elif state in _ORPHAN_RECOVERABLE_EXEC_STATES:
            orphaned = orphan_record(task)
            fail_after = orphan_fail_after(orphaned) if orphaned is not None else None
            if fail_after is not None and now < fail_after:
                # Its lease lapsed, its worker may still be running:
                # the worker writes its own ending by ``fail_after``, or none.
                continue
            if orphaned is not None:
                reason = orphan_failure_reason(orphaned)
                credit_attempts = int(orphaned.get("attempt") or 0)
            else:
                reason = (
                    "Background task reconciled to failed: its orchestration execution "
                    f"ended in '{state}' without finishing the task "
                    f"({_execution_names(execution_id, state, task)})."
                )
        else:
            # pending / running: the engine owns it — never touch.
            continue
        await _fail_orphaned_task(db, task, error=reason, now=now)
        if credit_attempts:
            # The dead letter left the charge to a worker that never ended:
            # what its attempts were charged goes back, free.
            from app.services.attempt_charges import credit_abandoned_task

            await credit_abandoned_task(str(task.id), attempts=credit_attempts, db=db)
        recovered += 1

    if recovered:
        logger.info(
            "Recovered %d orphaned background task(s) with no live execution",
            recovered,
        )
    return recovered


#: What a background task whose turn was cut with no stop of its worker ends
#: with: failed, never ``completed`` with the cut's apology as result.
TURN_CUT_ERROR = (
    "Its turn was cut while it ran (its execution was cancelled, or taken back by another "
    "worker) and stopped without an answer."
)


class TurnCutError(RuntimeError):
    """A background task's turn ended on a cancellation it did not answer."""


#: What a background task its stopping worker handed back says.
HANDED_BACK_ERROR = (
    "Its worker was asked to stop (a deployment, a scale-in, a node drain): the turn stopped at an "
    "iteration boundary, checkpointed, and runs again at once from that checkpoint."
)


async def _hand_back_wound_down_task(
    task_id: str, *, checkpoint, attempt: int | None, place: tuple[int, int] | None
) -> SubAgentTaskOutcome:
    """Hand a task its stopping worker wound down back to the queue.

    Its checkpoint is flushed first, every completed round in it; the task
    stays ``running`` with the hand-back recorded (signal, attempt), so the
    next attempt resumes it (``background_checkpoint.note_resume``) and its
    work item claims stay held by a live task meanwhile. The adapter re-queues
    it due now (``SubAgentAdapter.execute``). A task that ended, or that a
    later attempt replaced, meanwhile keeps that ending.
    """
    from app.core.database import async_session
    from app.services import background_checkpoint
    from app.services.orchestration_reconciler import stop_signal

    await checkpoint.aclose(flush=True)
    signal = stop_signal()
    async with async_session() as db:
        task, ended = await _locked_task_for_ending(db, task_id, place)
        if ended is not None:
            return ended
        if task is None:
            return SubAgentTaskOutcome(status="failed", error=f"Sub-agent task {task_id} not found")
        task.metadata_json = background_checkpoint.handed_back(task.metadata_json, attempt=attempt, signal=signal)
        await db.commit()
    logger.warning(
        "Background task %s (run %s) handed back at attempt %s: its worker was asked to stop by %s; "
        "it is resumed from its checkpoint by the next worker, its work item claims carried",
        task_id,
        (task.metadata_json or {}).get("assignment_run_id") or "none",
        attempt,
        signal,
    )
    return SubAgentTaskOutcome(status="handed_back", error=HANDED_BACK_ERROR)


async def release_failed_task_claims(task_id: Any, run_id: Any = None) -> int:
    """Release the work items a background task (or its run) held, right after it was failed.

    Called once the failure is committed, so the holder reads as ended and the
    item goes back to ``open`` in the same pass, with the release note, instead
    of staying held by a dead holder until the next scheduler tick. Best effort:
    the tick's :func:`release_ended_run_claims` stays the backstop.
    """
    from app.services.work_item import release_ended_run_claims

    try:
        released = await release_ended_run_claims(run_ids=[task_id, run_id])
    except Exception as exc:
        logger.warning(
            "Work item claims of failed background task %s not released now (the next tick does): %s",
            task_id,
            format_exception_message(exc),
        )
        return 0
    if released:
        logger.info("Released %d work item(s) held by failed background task %s", released, task_id)
    return released


async def _settle_unsettled_task(
    db: AsyncSession, task: SubAgentTask, *, names: str, now: datetime
) -> None:
    """Settle a task its completed execution left ``pending`` / ``running``.

    A task holding a result ends ``completed`` (the body wrote the answer and
    lost only the status); one without ends ``failed``, the reason naming the
    execution and the run. Operates in the caller's transaction.
    """
    if task.result is not None:
        task.status = "completed"
        task.completed_at = now
        task.error = None
        await _update_task_delegation(db, str(task.id), "completed", result=task.result)
        logger.warning(
            "Background task %s settled to completed: its %s ended without settling it",
            task.id,
            names,
        )
        return
    await _fail_orphaned_task(
        db,
        task,
        error=(
            "Background task reconciled to failed: its orchestration execution completed "
            f"without the task writing an ending ({names})."
        ),
        now=now,
    )


async def _fail_orphaned_task(
    db: AsyncSession, task: SubAgentTask, *, error: str, now: datetime
) -> None:
    """Flip an orphaned task to ``failed``, close its delegation, fail linked run.

    Mirrors the adapter ``on_dead_letter`` cleanup but driven from the task side
    (the orphan sweep has no execution object). Operates in the caller's
    transaction.
    """
    task.status = "failed"
    task.error = error
    task.completed_at = now
    await _update_task_delegation(db, str(task.id), "failed", error=error)
    # The claims it held go back to the queue on the next tick, each
    # release logged; this line names the cause.
    logger.error(
        "Background task %s (run %s) failed: %s Its work item claims are released once this failure is committed, "
        "the next scheduler tick as the backstop",
        task.id,
        (task.metadata_json or {}).get("assignment_run_id") or "none",
        error,
    )
    # Its turn never ended, so it never wrote its own trace: close it with the
    # spans its turn checkpointed and what ended it, found by the task id.
    from app.services.tracing.background_run import record_lost_background_run

    orphaned = orphan_record(task) or {}
    record_lost_background_run(
        task,
        reason=error,
        last_heartbeat_at=orphaned.get("last_heartbeat_at"),
        attempt=orphaned.get("attempt"),
        now=now,
    )

    run_id = (task.metadata_json or {}).get("assignment_run_id")
    if not run_id:
        return
    run_uuid = safe_uuid(str(run_id))
    if run_uuid is None:
        return
    from app.models.assignment import AssignmentRun

    run = (
        await db.execute(
            select(AssignmentRun).where(AssignmentRun.id == run_uuid).with_for_update()
        )
    ).scalar_one_or_none()
    if run is not None and run.status in (
        "pending",
        "delayed",
        "running",
        "awaiting_human",
    ):
        run.status = "failed"
        run.error = error
        run.completed_at = now


def _with_watch_event_latency(metadata: dict | None, started_at: datetime) -> dict | None:
    """*metadata* with the ``event_latency`` of a watch-started task, fed to the histogram.

    Any other task, or one already measured, is returned unchanged. Never raises.
    """
    try:
        from app.services.tracing import event_latency

        if not isinstance(metadata, dict) or "event_latency" in metadata:
            return metadata
        figures = event_latency.task_event_latency(metadata, started_at=started_at)
        if figures is None:
            return metadata
        event_latency.observe(figures)
        return {**metadata, "event_latency": figures}
    except Exception:
        logger.debug("Watch task latency not recorded", exc_info=True)
        return metadata


async def _update_task_delegation(
    db: AsyncSession,
    task_id: str,
    status: str,
    *,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort update of the ``background_task`` delegation audit row.

    Delegation tracking must never fail the task itself, mirroring how
    ``create_sub_agent_task`` swallows ``log_delegation`` errors.
    """
    try:
        from app.services.delegation import update_delegation

        await update_delegation(
            db,
            sub_agent_task_id=task_id,
            status=status,
            result=result,
            error=error,
        )
    except Exception as deleg_err:
        logger.warning(
            "Failed to update background_task delegation for task %s: %s",
            task_id,
            format_exception_message(deleg_err),
        )

    # Flow signalling: completion-mode "On delegate" attachments
    # stamped emission specs on the task at spawn — fire them exactly once,
    # on the terminal transition. Same best-effort contract as above: the
    # task's own outcome is already decided, a signalling error must not
    # touch it. This is the single choke point every terminal path
    # (completed / failed / cancelled / incomplete) already goes through.
    if status in ("completed", "failed", "cancelled", "incomplete"):
        try:
            task_row = (
                await db.execute(
                    select(SubAgentTask).where(
                        SubAgentTask.id == uuid.UUID(task_id)
                    )
                )
            ).scalar_one_or_none()
            if task_row is not None and (task_row.metadata_json or {}).get(
                "flow_completion_emissions"
            ):
                from app.services.flow_events import (
                    emit_delegation_completion_events,
                )

                await emit_delegation_completion_events(db, task_row, status)
        except Exception as emit_err:
            logger.warning(
                "Delegation completion event emission failed for task %s: %s",
                task_id,
                format_exception_message(emit_err),
            )


def _outcome_error_redacted(fn: Callable[..., Awaitable[SubAgentTaskOutcome]]) -> Callable[..., Awaitable[SubAgentTaskOutcome]]:
    """The error a task's outcome carries out, redacted while the task's secret scope is still open.

    The orchestration engine records it as the execution's ``last_error``
    after the scope closed, through a bounded column the engine's content
    redaction does not cover.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> SubAgentTaskOutcome:
        outcome = await fn(*args, **kwargs)
        if isinstance(outcome, SubAgentTaskOutcome) and outcome.error:
            return replace(outcome, error=redact_known_secrets_for_display(outcome.error))
        return outcome

    return wrapper


def task_turn_user_id(metadata: dict | None, user_id: str) -> str:
    """Whom a task's turn acts as and sees with: its user, or no one (``""``)
    for Platform Health's delegated work, never the row's owner of record
    (``create_sub_agent_task``)."""
    return "" if (metadata or {}).get(HEALTH_ALONE_KEY) else user_id


@known_secrets_scoped()
@_outcome_error_redacted
async def _execute_sub_agent_task(
    task_id: str,
    user_id: str,
    target_agent_id: str,
    task_description: str,
    assignment_run_id: str | None = None,
    delegation_depth: str = "0",
    scope_headers_json: str | None = None,
    file_context: object | None = None,
    parent_trace_context: dict | None = None,
    visited_agents: list[str] | None = None,
    timeout_seconds: int | None = None,
    max_iterations: int | None = None,
    element_scope: dict | None = None,
    hold: bool = False,
    verdict: AgenticVerdict | None = None,
    cancel_event: asyncio.Event | None = None,
    deadline_at: datetime | None = None,
    attempt: int | None = None,
    taken_back_on_its_word: bool = False,
    taken_back_reason: str | None = None,
    killed_by_rollout: bool = False,
) -> SubAgentTaskOutcome:
    """Execute a sub-agent task in the background.

    Opens a fresh DB session, loads the target agent, runs the agentic loop,
    and persists the result.  When ``assignment_run_id`` is provided, also
    updates the linked AssignmentRun record for Activity tab tracing.

    ``element_scope`` is the delegating run's Scope(s) from the payload,
    handed to ``run_agent_chat`` in its ``handover_context`` so the
    target agent narrows it with its own selection.

    ``parent_trace_context`` is the serialized trace context
    (``{"trace_id": ..., "parent_span_id": ..., "local_trace_id": ...}``)
    captured at ``create_sub_agent_task`` time.  It is passed to
    ``run_agent_chat`` as ``parent_trace_ctx`` so the sub-agent's Langfuse
    root span nests inside the orchestrator's trace and its ``llm_traces``
    row records the orchestrator's as its ``parent_trace_id``, instead of
    starting a disconnected root trace.

    Invoked by the ``SubAgentAdapter`` from the orchestration reconciler; it
    manages the ``SubAgentTask`` lifecycle and never raises on a normal failure.
    The terminal ``SubAgentTaskOutcome`` is returned so the adapter can map
    failures onto the orchestration execution instead of always completing.

    ``timeout_seconds`` / ``max_iterations`` are the inherited parent budget
    from the execution payload (see ``create_sub_agent_task``): the wall-clock
    bound around ``run_agent_chat`` and the agentic-loop cap forwarded as
    ``max_iterations_override``.  ``timeout_seconds is None`` is a chat spawn
    (or a pre-fix in-flight payload): the wall-clock is derived from the
    target agent's ``max_iterations`` via :func:`resolve_chat_sub_agent_timeout`
    so it matches ``run_agent_chat``, not the historical 900 s default.

    ``deadline_at`` is the execution's orchestration deadline: the
    bound never runs past it (a re-run after a worker restart keeps the
    deadline it was created with), and the agent loop is handed a wall clock
    that ends inside the bound, so the wait clamp, the forced answer and the
    ``waiting_on_tasks`` hand-off all see the task's real budget
    instead of the chat formula's.

    ``scope_headers_json`` is the serialized per-request platform scope headers
    of the DELEGATING request, forwarded so the sub-agent's platform tools
    inherit the operator's scope.  It is a sub-agent-delegation-only field:
    only ``create_sub_agent_task`` ever sets it.  Autonomous-orchestrator runs
    never carry it - they are server-initiated through the durable FSM and
    authenticate to OpenAEV with a per-user cross-platform JWT, so there are no
    request scope headers to thread to their advisory consult specialists.  The
    deserialize branch below is therefore inert on the autonomous path by
    design, not a dead bug to prune.

    ``hold``: while agentic AI is unavailable the task is left pending,
    carrying the hold marker (and so is its linked run), and a ``held``
    outcome is returned for the adapter to hold its execution. Without it (the
    hold is over) the task is failed with the reason and its parent woken.
    ``verdict`` is the background gate's reading the adapter already acted on
    (a held execution's re-claim): the task decides on it rather than on a
    second reading.

    ``attempt`` is the orchestration attempt running the task: its
    agent turn is charged under a receipt of that attempt's own, and a re-run
    after a lost lease credits back what the attempts before it were charged
    (``attempt_charges``). ``None`` outside the engine: the task-wide receipt.

    ``taken_back_on_its_word``: the engine took the execution back from a
    worker that said it was stopping (``orchestration_engine.stopped_on_its_word``),
    so the resume that follows is graceful and not counted against
    ``background_checkpoint.MAX_RESUMES``, like a hand-back.
    """
    from app.core.database import async_session

    depth = int(delegation_depth)
    chat_messages: list = []
    agent_name = "Agent"
    # The parent's working files the task was handed, snapshotted right before
    # the loop runs. ``None`` until then: a failure before the loop
    # produced nothing, and the failure paths below must not record anything.
    inherited_file_ids: set[str] | None = None
    # Same resolution as the orchestration deadline in create_sub_agent_task —
    # shared helper so the wait_for bound and deadline_at can never drift.
    # Chat / pre-fix payloads (``timeout_seconds is None``) are re-derived
    # from the target agent after it is loaded, below — calling the sync
    # helper here would pin the historical 900 s default before we know
    # the iteration cap.
    effective_timeout: float | None = (
        None
        if timeout_seconds is None
        else resolve_sub_agent_timeout(timeout_seconds)
    )
    budget_timeout: float | None = effective_timeout
    deadline_cut = False
    loop_deadline: float | None = None
    # The turn stopped on its wall clock before any model output: the
    # deadline's timeout, whatever bound fired.
    deadline_stop_no_output = False

    # Read by the timeout and exception handlers below, which can run before
    # the task was read.
    attempt_place: tuple[int, int] | None = None
    # A task within an evaluation's chain records its ledger: the run's
    # outcome and retry count, read by every terminal write below.
    _evaluation_chain = False
    # The evaluation's tool approval setting, when this task is in its chain.
    _evaluation_tool_approval: bool | None = None
    # The answer shape the spawning call fixed (spawn_background_task output_schema).
    _call_output_schema: dict[str, Any] | None = None
    # Whom the task's turn acts as: its user, or no one for Platform Health's work.
    _turn_user_id = user_id
    loop_outcome: dict = {}
    _task_retry_meter: dict = {"retries": 0}
    # An earlier attempt the engine lost ran this task too: its calls are in no
    # ledger, so this attempt's is marked partial.
    _earlier_attempt_ran = False
    # What that attempt checkpointed, replayed by this one.
    _task_replay = None
    try:
        async with async_session() as db:
            # Mark sub-agent task as running. Locked until this session's first
            # commit - an ending before the turn, a hold, or "running" - so no
            # other attempt's ending lands under those writes.
            result = await db.execute(
                select(SubAgentTask)
                .where(SubAgentTask.id == uuid.UUID(task_id))
                .with_for_update()
            )
            task = result.scalar_one_or_none()
            if task is not None:
                # The delegating turn's or run's id: the task it wrote and the
                # result read back by it share its secret markers.
                bind_known_secrets(task.conversation_id or (task.metadata_json or {}).get("parent_run_id"))
            # This attempt's place, in the epoch of the row it just locked.
            attempt_place = (
                task_attempt_place(task.metadata_json, attempt) if task is not None else None
            )
            ended = _ended_elsewhere(task, attempt_place) if task is not None else None
            if ended is not None:
                logger.warning(
                    "Sub-agent task %s had already ended (%s) or was replaced "
                    "before this attempt started; it does nothing",
                    task_id,
                    task.status,
                )
                return ended
            if not task:
                logger.error("Sub-agent task %s not found", task_id)
                error_msg = f"Sub-agent task {task_id} not found"
                # The adapter maps this outcome to a NON-retryable failure,
                # which never reaches ``on_dead_letter`` — so the linked
                # AssignmentRun and the delegation audit row must be closed
                # here or they'd stay pending forever.
                if assignment_run_id:
                    await update_assignment_run(
                        db, assignment_run_id, "failed", error=error_msg
                    )
                await _update_task_delegation(db, task_id, "failed", error=error_msg)
                await db.commit()
                return SubAgentTaskOutcome(status="failed", error=error_msg)

            # Started by an attempt the engine lost (a worker restart): the
            # files it recorded stay on the record this attempt writes.
            reclaimed = task.status == "running"
            if reclaimed:
                _carry_recorded_files(task)
            _earlier_attempt_ran = reclaimed or (attempt is not None and attempt > 1)

            # A re-run after the engine lost this task's execution:
            # this attempt is charged for the task, so what the attempts before
            # it were charged goes back - free, as the dead-letter would have
            # credited it - whatever this attempt then does. An earlier attempt
            # still running can no longer credit or charge it again.
            if attempt is not None and attempt > 1:
                await credit_lost_attempts(task_id, attempt=attempt, db=db)
                # And that earlier attempt writes no ending once this one is on
                # the row: committed with this attempt's first write.
                if attempt_place > task_attempt_fence(task.metadata_json):
                    task.metadata_json = with_task_attempt_fence(
                        task.metadata_json, attempt_place
                    )

            # A delegation queued before AI was switched off or the deployment
            # dropped to Community Edition has not started: it never reaches
            # the agent turn that would charge the quota. It is HELD,
            # pending, and runs once agentic AI is available again - its parent
            # is woken then, when it ends. Once the hold is over it is
            # failed like any other failure before the loop, so the parent
            # learns it ended. A refusal nothing was read for (a database blip
            # on a cold worker) leaves the task pending, untouched, for the
            # engine to retry.
            from app.services import ai_hold
            from app.services.agentic_gate import (
                UNKNOWN_STATE_DETAIL,
                agentic_verdict,
                group_verdict,
            )

            if verdict is None:
                verdict = await agentic_verdict(db)
                if verdict.refusal is None:
                    # For the user who delegated it (``group_ai_access``).
                    verdict = await group_verdict(db, user_id)
            if verdict.transient:
                logger.info(
                    "Sub-agent task %s not started: could not confirm that "
                    "agentic AI may run (%s); the orchestration engine retries it",
                    task_id,
                    verdict.refusal,
                )
                return SubAgentTaskOutcome(status="deferred", error=UNKNOWN_STATE_DETAIL)
            refusal = verdict.refusal
            if refusal is None:
                from app.services.quota import quota_fail_closed, quota_store_out

                # The agent turn charges the task, and while quotas fail closed
                # an unreachable store would refuse it: the task is held the
                # same way before it starts, charged once it runs.
                if quota_fail_closed() and await quota_store_out():
                    refusal = ai_hold.QUOTA_STORE_UNAVAILABLE_CODE
            was_held = (
                ai_hold.held_reason_of(getattr(task, "metadata_json", None)) is not None
            )
            if refusal is not None:
                if not hold:
                    agent_name = await _target_agent_name(db, target_agent_id, agent_name)
                return await _refuse_task_before_turn(
                    db,
                    task,
                    task_id=task_id,
                    user_id=user_id,
                    assignment_run_id=assignment_run_id,
                    refusal=refusal,
                    hold=hold,
                    was_held=was_held,
                    agent_name=agent_name,
                    # A reclaimed task waits as it was before its lost attempt
                    # started it, with its linked run and delegation.
                    started=reclaimed,
                )

            if deadline_spent(deadline_at):
                return await _fail_task_out_of_time(
                    db,
                    task,
                    task_id=task_id,
                    user_id=user_id,
                    assignment_run_id=assignment_run_id,
                    deadline_at=deadline_at,
                    agent_name=await _target_agent_name(db, target_agent_id, agent_name),
                )

            # A task on a work item another run already finished, or another
            # live run holds, ends here before its turn is charged, with what a
            # claim would answer. The task, its child run and
            # the run that spawned it are its own: their claim lets it through.
            from app.services.work_item_preflight import finished_work_item_answer

            finished_answer = await finished_work_item_answer(
                task_description,
                user_id=user_id,
                own_run_ids=(
                    task_id,
                    assignment_run_id,
                    (task.metadata_json or {}).get("parent_run_id"),
                ),
            )
            if finished_answer is not None:
                return await _complete_task_before_turn(
                    db,
                    task,
                    task_id=task_id,
                    user_id=user_id,
                    assignment_run_id=assignment_run_id,
                    answer=finished_answer,
                    agent_name=await _target_agent_name(db, target_agent_id, agent_name),
                )

            # A re-run after the engine lost an attempt of this task (a
            # rollout, a node drain, a lost lease): it resumes from what that
            # attempt checkpointed. After a loss without a word, at most
            # ``MAX_RESUMES`` times in a row with no tool call completed between
            # the losses (the replay read first says whether the task
            # progressed) and ``MAX_LOST_RESUMES`` in all; ``MAX_GRACEFUL_RESUMES``
            # after a graceful stop. Past them it is failed with its
            # reason. Its work item claims stay held by the task (or its
            # run), which is alive again.
            if _earlier_attempt_ran:
                from app.services import background_checkpoint

                _task_replay = await background_checkpoint.load_task_replay(
                    task_id, conversation_id=task.conversation_id, user_id=user_id
                )
                refused_resume = background_checkpoint.resume_refused(
                    task.metadata_json,
                    attempt=attempt,
                    taken_back_on_its_word=taken_back_on_its_word,
                    completed_calls=_task_replay.completed_calls,
                    reason=taken_back_reason,
                    killed_by_rollout=killed_by_rollout,
                )
                if refused_resume is not None:
                    await _fail_orphaned_task(db, task, error=refused_resume, now=datetime.now(UTC))
                    await db.commit()
                    # Its claims go back to the queue now, not at the next tick.
                    await release_failed_task_claims(task_id, (task.metadata_json or {}).get("assignment_run_id"))
                    # Its checkpoint is kept (it expires on its own, CHECKPOINT_TTL_SECONDS): the progress trail of
                    # the work done stays readable for a relaunch (#12386).
                    return SubAgentTaskOutcome(status="failed", error=refused_resume)
                task.metadata_json = background_checkpoint.note_resume(
                    task,
                    replay=_task_replay,
                    attempt=attempt,
                    taken_back_on_its_word=taken_back_on_its_word,
                    reason=taken_back_reason,
                    killed_by_rollout=killed_by_rollout,
                )

            task.status = "running"
            task.started_at = datetime.now(UTC)
            if not _earlier_attempt_ran:
                # A task a platform event watch started: its event-to-task
                # latency feeds xtmone_event_to_run_seconds once.
                task.metadata_json = _with_watch_event_latency(task.metadata_json, task.started_at)
            # Within an evaluation's chain: this run records its ledger and
            # marks the tasks it spawns. Read here, where the row is held.
            _evaluation_chain = bool((task.metadata_json or {}).get(EVALUATION_CHAIN_KEY))
            _turn_user_id = task_turn_user_id(task.metadata_json, user_id)
            _stored_approval = (task.metadata_json or {}).get(EVALUATION_TOOL_APPROVAL_KEY)
            _evaluation_tool_approval = (
                _stored_approval if _evaluation_chain and isinstance(_stored_approval, bool) else None
            )
            _stored_schema = (task.metadata_json or {}).get(CALL_OUTPUT_SCHEMA_KEY)
            _call_output_schema = _stored_schema if isinstance(_stored_schema, dict) else None
            if was_held:
                # Held until now: AI is available again, the task goes ahead.
                task.metadata_json = ai_hold.without_hold_marker(task.metadata_json)
            await _update_task_delegation(db, task_id, "running")

            # Mark linked AssignmentRun as running (if exists)
            if assignment_run_id:
                try:
                    from app.models.assignment import AssignmentRun

                    _wr_result = await db.execute(
                        select(AssignmentRun).where(
                            AssignmentRun.id == uuid.UUID(assignment_run_id)
                        )
                    )
                    _wr = _wr_result.scalar_one_or_none()
                    if _wr:
                        _wr.status = "running"
                        _wr.started_at = datetime.now(UTC)
                        cleared = ai_hold.without_hold_marker(_wr.trigger_context)
                        if cleared is not None:
                            _wr.trigger_context = cleared
                except Exception as wr_err:
                    logger.warning(
                        "Failed to update AssignmentRun %s to running: %s",
                        assignment_run_id,
                        format_exception_message(wr_err),
                    )

            await db.commit()

            # Load the target agent with full three-tier visibility (own +
            # group-shared + company-managed). The spawn-time handover list is
            # already resolved with `user_visible_filter`, so re-loading here with
            # a narrower two-tier `or_()` structurally broke delegation to any
            # group-shared peer agent (every run failed "not accessible").
            from app.api.v1.helpers import get_user_group_ids

            # Platform Health's delegated work sees what its run with no user saw
            # at spawn, never what the row's owner of record sees.
            _target_user_uuid = uuid.UUID(_turn_user_id) if _turn_user_id else None
            _target_group_ids = (
                await get_user_group_ids(db, _target_user_uuid) if _target_user_uuid else []
            )
            # The spawning agent's configured handover is the grant, as at
            # spawn (services/fleets.py configured_handover_reach): a delegate
            # peer the person may not open still starts.
            from app.services.fleets import configured_handover_reach, handover_peer_clause

            _parent_id = safe_uuid(getattr(task, "parent_agent_id", None))
            _granted: set[uuid.UUID] = set()
            if _target_user_uuid is not None and _parent_id is not None:
                _parent_handovers = (
                    await db.execute(select(Agent.handover_agent_ids).where(Agent.id == _parent_id))
                ).scalar_one_or_none()
                _granted = await configured_handover_reach(db, _parent_id, _parent_handovers)
            agent_result = await db.execute(
                select(Agent).where(
                    Agent.id == uuid.UUID(target_agent_id),
                    Agent.enabled.is_(True),
                    handover_peer_clause(_target_user_uuid, _target_group_ids, _granted),
                )
            )
            agent = agent_result.scalar_one_or_none()
            if not agent:
                error_msg = (
                    f"Target agent {target_agent_id} not found or not accessible."
                )
                task, ended = await _locked_task_for_ending(db, task_id, attempt_place)
                if ended is not None:
                    return ended
                if task is None:
                    return SubAgentTaskOutcome(status="failed", error=error_msg)
                task.status = "failed"
                task.error = error_msg
                task.completed_at = datetime.now(UTC)
                _record_no_turn_ledger(task)
                if assignment_run_id:
                    await update_assignment_run(
                        db, assignment_run_id, "failed", error=error_msg
                    )
                await _update_task_delegation(db, task_id, "failed", error=error_msg)
                await db.commit()
                await publish_task_event(
                    user_id,
                    "failed",
                    task_id,
                    conversation_id=(
                        str(task.conversation_id) if task.conversation_id else None
                    ),
                    status="failed",
                )
                _schedule_parent_wake_if_needed(
                    task, agent_name=agent_name, terminal_status="failed"
                )
                return SubAgentTaskOutcome(status="failed", error=error_msg)
            agent_name = agent.name or "Agent"

            # Chat / pre-fix payloads carried ``timeout_seconds=None``.
            # Derive the wall-clock from this agent's iteration cap so a
            # 100-iteration specialist is not killed at 900 s.  Assignment-
            # inherited budgets (a positive int, or 0/negative = disabled)
            # were already resolved above and must not be overwritten.
            if timeout_seconds is None:
                effective_timeout = await resolve_chat_sub_agent_timeout(
                    db, target_agent_id, agent=agent, evaluation_chain=_evaluation_chain
                )
            if deadline_spent(deadline_at):
                task, ended = await _locked_task_for_ending(db, task_id, attempt_place)
                if ended is not None:
                    return ended
                if task is None:
                    return SubAgentTaskOutcome(
                        status="failed", error=f"Sub-agent task {task_id} not found"
                    )
                return await _fail_task_out_of_time(
                    db,
                    task,
                    task_id=task_id,
                    user_id=user_id,
                    assignment_run_id=assignment_run_id,
                    deadline_at=deadline_at,
                    agent_name=agent_name,
                )
            budget_timeout = effective_timeout
            effective_timeout = bound_by_deadline(effective_timeout, deadline_at)
            deadline_cut = deadline_cut_budget(budget_timeout, effective_timeout)
            if deadline_cut:
                logger.info(
                    "Sub-agent task %s runs on the %ds its deadline leaves "
                    "(budget %ds): it started late or re-runs a lost attempt, "
                    "and keeps the deadline it was created with",
                    task_id,
                    int(effective_timeout or 0),
                    int(budget_timeout or 0),
                )

            # Allow handover if we haven't exceeded the depth limit
            allow_handover = depth < _MAX_DELEGATION_DEPTH and bool(
                agent.handover_agent_ids
            )

            # Run the agent chat with the task as the user message.
            #
            # ``parent_run_id`` propagation:
            #   When this task was spawned from a assignment context,
            #   ``assignment_run_id`` is the child AssignmentRun created for THIS
            #   delegation.  We pass it as ``parent_run_id`` so that any
            #   further delegations by this sub-agent also create AssignmentRun
            #   records chained to it — keeping the full tree visible in the
            #   Activity tab.
            #   When spawned from a chat context, ``assignment_run_id`` is None
            #   and no AssignmentRun records are created.
            # Collect the full conversation messages (including tool calls
            # and results) so we can persist them in the AssignmentRun for
            # the run detail view to display agentic steps / reasoning.
            # Deserialize scope_headers from JSON string
            import json as _json

            from app.services.agent_chat import QUOTA_STORE_OUTAGE_KEY, run_agent_chat
            from app.services.agentic_executions import (
                SOURCE_AGENT_CHAT,
                refund_agentic_execution,
            )
            from app.services.builtin_tools import normalize_visited_agents
            from app.services.builtin_tools.handover import DELEGATING_AGENT_KEY
            from app.services.integration_tools import (
                INHERITED_TEMPLATE_PICKS_KEY,
                inherited_template_picks,
            )

            _scope_headers: dict[str, str] | None = None
            if scope_headers_json:
                with contextlib.suppress(_json.JSONDecodeError, TypeError):
                    _scope_headers = _json.loads(scope_headers_json)

            # Rebuild the parent's trace context (only when the payload
            # carries a usable serialized form) so the sub-agent's trace
            # nests under the orchestrator's instead of orphaning: in
            # Langfuse through ``trace_id``, and in the durable traces
            # through ``local_trace_id``, which Langfuse being off leaves
            # as the only key.
            _parent_trace_ctx: dict | None = None
            if isinstance(parent_trace_context, dict) and (
                parent_trace_context.get("trace_id")
                or parent_trace_context.get("local_trace_id")
            ):
                _parent_trace_ctx = parent_trace_context

            # The files this task produces must outlive this worker.
            # ``run_agent_chat`` builds a private, invisible FileContext when
            # handed ``None``, so the run ALWAYS gets a concrete one here: a
            # RECORDING one, which writes every file the loop adds beyond the
            # parent's inherited working files to the task row as it lands.
            # The terminal write below repeats the record on the in-process
            # ends; the incremental one is what survives a worker killed (or
            # a lease lost) mid-loop, when ``on_dead_letter`` fails the task
            # from another process and wakes the parent with what the row
            # says - and the row must already say which batches were written.
            _carried_ids = task_result_file_ids(task, key=CARRIED_FILE_IDS_KEY)
            # A resumed attempt (a graceful hand-back or a lost lease) reads
            # again, with the same ids, the files the attempts before it
            # produced: recorded, or named by a replayed result.
            _resumed_refs = (
                await resumed_file_refs(_carried_ids, _task_replay.history, user_id=user_id)
                if _task_replay is not None
                else []
            )
            file_context = RecordingFileContext.from_parent(
                file_context,
                task_id=task_id,
                carried_ids=_carried_ids,
                place=attempt_place,
                resumed=_resumed_refs,
            )
            inherited_file_ids = set(file_context.inherited_ids)
            # Files the prompt came with (run_agent_once with file_ids): on
            # the run's user message, as a chat message's attachments are.
            _turn_files = task_turn_file_refs(file_context, task)

            loop_outcome: dict = {}
            turn_errors: dict = {}
            loop_deadline = loop_wall_clock_deadline(effective_timeout)
            # A delegated task runs with no conversation of its own, but in the
            # workspace of the conversation that delegated it.
            from app.services.workspace_knowledge import load_conversation_workspace_id

            task_workspace_id = await load_conversation_workspace_id(
                db, str(task.conversation_id) if task.conversation_id else None
            )
            # A work item this task claims is held by its child run, else by
            # the task itself: released when the task ends. Its trace
            # is checkpointed from its open, span by span, so a turn the engine
            # loses still leaves what it did.
            from app.services.background_checkpoint import TaskCheckpoint, discard_task_checkpoint
            from app.services.tracing.background_run import recording_spans
            from app.services.work_item import claims_held_by_background_task

            # Its completed tool rounds are checkpointed at every iteration
            # boundary, so an attempt the engine loses is resumed from them by
            # the next one instead of starting again.
            _task_checkpoint = TaskCheckpoint(
                uuid.UUID(task_id),
                resumed=_task_replay.resumed if _task_replay is not None else None,
                attempt=attempt,
                completed_calls=_task_replay.completed_calls if _task_replay is not None else 0,
                # A hand-back keeps an oversized result whole as a file of the
                # acting user in the delegating conversation, where the resumed
                # attempt reads it again (``resumed_file_refs``).
                user_id=user_id,
                files_conversation_id=task.conversation_id,
                # Fenced by this attempt's place: a superseded attempt's last write never lands over
                # the snapshot of the attempt that replaced it.
                place=attempt_place,
            )

            # Its own retry meter: the entry's does not reach another task.
            from app.core.turn_writer import TurnSuperseded
            from app.core.worker_shutdown import TurnWoundDown
            from app.services.background_checkpoint import resumed_trace_meta
            from app.services.langfuse_tracing import next_root_trace
            from app.services.orchestration_reconciler import worker_stopping
            from app.services.run_context import format_run_context

            # A resumed attempt's first trace says so: a trace search
            # on ``resumed_from`` finds it, with the task, run, cause and count.
            _resumed_trace = (
                resumed_trace_meta(task_id, task.metadata_json) if _task_replay is not None else None
            )
            # A stop on a work item this task holds (taken over, reset or
            # finished by another writer) reaches its loop, a pause included (#11751).
            # Closed in the ``finally`` of the turn below.
            from app.services import work_item_note_steering

            _held_item_stack = contextlib.AsyncExitStack()
            _held_item_steering = await _held_item_stack.enter_async_context(
                work_item_note_steering.session_for_runs((task_id, assignment_run_id))
            )
            if (task.metadata_json or {}).get(RUN_DRY_RUN_KEY) is True:
                # run_agent_once(dry_run=true) (#1911): no write of this turn,
                # its nested consults included, is executed.
                from app.services.health.sandbox.session import SandboxSession, sandbox_session

                _held_item_stack.enter_context(sandbox_session(SandboxSession(mode="dry_run")))
            try:
                with (
                    count_llm_retries()
                    if _evaluation_chain
                    else contextlib.nullcontext(_task_retry_meter)
                ) as _task_retry_meter, claims_held_by_background_task(task_id), recording_spans(task_id), (
                    next_root_trace(_resumed_trace) if _resumed_trace else contextlib.nullcontext()
                ):
                    agent_response, _ = await asyncio.wait_for(
                        run_agent_chat(
                            db,
                            _turn_user_id,
                            agent,
                            # No history - a standalone task - but what an attempt
                            # the engine lost had done, replayed.
                            list(_task_replay.history) if _task_replay is not None else [],
                            task_description,
                            restore_user_message=True,
                            allow_handover=allow_handover,
                            # The caller's per-call answer shape: JSON valid against it.
                            session_output_schema=_call_output_schema,
                            # Inherited parent budget: a task spawned from an
                            # assignment run uses the parent run's resolved cap (the
                            # assignment scope wins over the target agent's override).
                            max_iterations_override=max_iterations,
                            handover_context={
                                "type": "sub_agent_task",
                                "source_agent": "background task",
                                "original_question": task_description,
                                # Handover chain inheritance: the spawned agent picks
                                # this up in run_agent_chat so nested delegations /
                                # transfers cannot loop back to an ancestor.  The
                                # payload value is normalized because it crosses a
                                # JSON serialization boundary — a corrupted string
                                # would otherwise be iterated character-by-character
                                # into a bogus chain that looks like a valid list.
                                "visited_agents": normalize_visited_agents(visited_agents),
                                # The agent that spawned this task: the one agent
                                # of the chain it may still consult, with a
                                # question, a bounded number of times.
                                DELEGATING_AGENT_KEY: (
                                    str(_delegating)
                                    if (_delegating := safe_uuid(getattr(task, "parent_agent_id", None)))
                                    else None
                                ),
                                # The delegating run's Scope(s): the target
                                # agent can only narrow it, never escape it.
                                "element_scope": element_scope,
                                # The task's own identity (its run, the run
                                # that spawned it, its task id), told to it.
                                "run_context": format_run_context(
                                    run_id=assignment_run_id,
                                    trigger_type=(task.metadata_json or {}).get(RUN_TRIGGER_TYPE_KEY) or "delegation",
                                    parent_run_id=(task.metadata_json or {}).get("parent_run_id"),
                                    task_id=task_id,
                                ),
                            },
                            parent_run_id=assignment_run_id,
                            delegation_depth=depth + 1,
                            # A background task is exactly one metered action, and it
                            # can be abandoned by the orchestration engine from another
                            # process (worker killed mid-turn, lease expired) — the
                            # handlers inside this turn then never run again. The charge
                            # leaves a receipt under this token so
                            # ``SubAgentAdapter.on_dead_letter`` can credit the action
                            # back, and only when it was really charged. One token per
                            # attempt: a re-run is charged under its own.
                            quota_receipt_token=task_receipt_token(task_id, attempt=attempt),
                            messages_out=chat_messages,
                            scope_headers=_scope_headers,
                            files=_turn_files or None,
                            file_context=file_context,
                            parent_trace_ctx=_parent_trace_ctx,
                            loop_outcome_out=loop_outcome,
                            cancel_event=cancel_event,
                            evaluation_chain=_evaluation_chain,
                            evaluation_tool_approval=_evaluation_tool_approval,
                            error_out=turn_errors,
                            wall_clock_deadline=loop_deadline,
                            session_workspace_id=task_workspace_id,
                            on_checkpoint=_task_checkpoint.offer,
                            steering_queue=_held_item_steering,
                            # The spawning run's per-user picks: this task acts
                            # as the same accounts when it may (#1792).
                            inherited_template_picks=inherited_template_picks(
                                (task.metadata_json or {}).get(INHERITED_TEMPLATE_PICKS_KEY)
                            ),
                        ),
                        # ``None`` = parent timeout disabled — no per-task bound (the
                        # reconciler's worker_job_timeout fallback still applies).
                        timeout=effective_timeout,
                    )
            except TurnWoundDown:
                # Its worker was asked to stop (a rollout, a scale-in, a node
                # drain): the turn stopped at an iteration boundary, the call in
                # flight answered. Its checkpoint is flushed and the task handed
                # back to the queue at once, resumed by the next worker.
                return await _hand_back_wound_down_task(
                    task_id, checkpoint=_task_checkpoint, attempt=attempt, place=attempt_place
                )
            except TurnSuperseded:
                # Another attempt took the task over while this turn ran:
                # its checkpoint writer stops without a last snapshot, so the
                # attempt that goes on keeps its own checkpoint, and nothing else
                # is written. The reconciler absorbs the stop.
                await _task_checkpoint.aclose(flush=False)
                raise
            finally:
                await _held_item_stack.aclose()
            turn_cut = loop_outcome.get("stop_reason") == "cancelled"
            if turn_cut and worker_stopping():
                # Cut mid call by its stopping worker: the engine took
                # the execution back from the draining worker, the lost lease
                # fired ``cancel_event`` and the loop answered with its
                # cancellation (a tool call or a sandbox start in flight). The
                # same graceful stop as a wind-down: handed back, resumed.
                logger.warning(
                    "Background task %s: its turn was cut mid call while its worker was stopping; "
                    "handing it back instead of ending it",
                    task_id,
                )
                return await _hand_back_wound_down_task(
                    task_id, checkpoint=_task_checkpoint, attempt=attempt, place=attempt_place
                )
            # The turn ended in this process: nothing is left to resume, whatever
            # its outcome.
            await _task_checkpoint.aclose(flush=False)
            await discard_task_checkpoint(task_id)
            if turn_errors.get(QUOTA_STORE_OUTAGE_KEY):
                # The store answered the start's probe but not the turn's quota
                # check (a flapping store, a failover): nothing was charged, and
                # the task is refused as its start would have refused it.
                # The execution the turn counted is given back - the
                # task is counted when it runs.
                await refund_agentic_execution(SOURCE_AGENT_CHAT)
                refused = (
                    await db.execute(
                        select(SubAgentTask)
                        .where(SubAgentTask.id == uuid.UUID(task_id))
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if refused is None:
                    return SubAgentTaskOutcome(
                        status="failed", error=f"Sub-agent task {task_id} not found"
                    )
                if _already_cancelled(refused):
                    await _record_ledger_on_killed_task(
                        db,
                        refused,
                        evaluation_chain=_evaluation_chain,
                        loop_outcome=loop_outcome,
                        retries=_task_retry_meter["retries"],
                        partial=_earlier_attempt_ran,
                    )
                    return SubAgentTaskOutcome(status="cancelled")
                ended = _ended_elsewhere(refused, attempt_place)
                if ended is not None:
                    return ended
                return await _refuse_task_before_turn(
                    db,
                    refused,
                    task_id=task_id,
                    user_id=user_id,
                    assignment_run_id=assignment_run_id,
                    refusal=ai_hold.QUOTA_STORE_UNAVAILABLE_CODE,
                    hold=hold,
                    was_held=was_held,
                    agent_name=agent_name,
                    started=True,
                )
            if turn_errors.get("error_code") == "loop_timeout":
                # The turn's own ``wait_for`` ended the loop before it wound
                # down, and handed back an apology written for a chat reader:
                # the task ran out of time, whatever that text says.
                raise TimeoutError(turn_errors.get("error") or "loop timeout")
            if loop_outcome.get("deadline_stop_without_output"):
                # The deadline left no time for a model call: the answer is
                # the forced-answer notice, not work. The task fails as the
                # deadline's timeout (free, ``engine_deadline_exceeded``) and
                # its parent is woken on a failure.
                deadline_stop_no_output = True
                raise TimeoutError("deadline stop without model output")
            if turn_cut:
                # Cancelled with no stop of this worker (an operator cancel, a
                # lease lost): its text is the cut's apology, not an answer, so
                # the task fails with the cut's reason. A task the user killed
                # stays cancelled, an attempt replaced writes nothing.
                raise TurnCutError(TURN_CUT_ERROR)

            # Fold narrated tool-call scaffolding out of the DELIVERED
            # answer, once, before its first persisted use.  ``chat_messages``
            # is only read here (the run's tool vocabulary) and never
            # rewritten - see ``_fold_delivered_answer``.
            agent_response = _fold_delivered_answer(
                task_id, agent_response, chat_messages
            )

            # Persist result on SubAgentTask. Re-read the row FOR UPDATE before the
            # terminal write so a concurrent "Kill" (``cancel_sub_agent_task``, a
            # separate session) can't be overwritten: the lock serializes the two
            # writers, so we either observe ``cancelled`` here and bail, or hold the
            # row until our commit and the kill's UPDATE then wins. ``populate_existing``
            # is still required because this worker's long-lived session
            # (``expire_on_commit=False``) caches the row as "running" from the start,
            # so even a locked re-select would otherwise hand back the stale copy.
            result2 = await db.execute(
                select(SubAgentTask)
                .where(SubAgentTask.id == uuid.UUID(task_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            task2 = result2.scalar_one_or_none()
            # Terminal guard: if the task was killed while this worker ran, leave
            # the cancelled status (and the failed linked run + closed delegation
            # the kill set) authoritative and discard this orphaned result rather
            # than resurrecting the task as completed.
            if _already_cancelled(task2):
                logger.info(
                    "Sub-agent task %s was cancelled while running; discarding "
                    "result instead of overwriting the cancelled status",
                    task_id,
                )
                await _record_ledger_on_killed_task(
                    db,
                    task2,
                    evaluation_chain=_evaluation_chain,
                    loop_outcome=loop_outcome,
                    retries=_task_retry_meter["retries"],
                    partial=_earlier_attempt_ran,
                )
                return SubAgentTaskOutcome(status="cancelled")
            ended = _ended_elsewhere(task2, attempt_place)
            if ended is not None:
                logger.warning(
                    "Sub-agent task %s had already ended %s; discarding this "
                    "attempt's result",
                    task_id,
                    task2.status,
                )
                return ended
            recorded_file_ids: list[str] = []
            if task2:
                stop_reason = loop_outcome.get("stop_reason")
                pending_ids = [
                    str(i)
                    for i in (loop_outcome.get("pending_task_ids") or [])
                    if i
                ]
                is_incomplete = stop_reason == "waiting_on_tasks"
                meta = dict(task2.metadata_json or {})
                continuation_depth = int(meta.get("wait_continuation_depth") or 0)
                terminal_status = "incomplete" if is_incomplete else "completed"
                task2.status = terminal_status
                # Only unwrap a fenced payload when the WHOLE response is a
                # single code block. The previous first-to-last-fence splice
                # silently discarded any prose around a code block (mangling
                # ordinary research/report results) and raised ValueError on a
                # fence without a following newline, flipping a completed task
                # to "failed". `_strip_code_fences` alone is not enough: its
                # open/close regexes match independently, so a response that
                # merely STARTS with a code block (prose after) would lose its
                # opening fence and keep a dangling closer. Gate on the
                # response both starting AND ending with a fence, with exactly
                # one fence pair — i.e. the response IS a single fenced block.
                from app.services.persistent_memory import _strip_code_fences

                _resp = (agent_response or "").strip()
                # First line must be a bare/json fence — `_strip_code_fences`
                # only consumes ``` or ```json, so unwrapping e.g. a
                # ```python block would leak the language word into the result.
                _first_line = _resp.split("\n", 1)[0].strip().lower()
                if (
                    _first_line in ("```", "```json")
                    and _resp.endswith("```")
                    and _resp.count("```") == 2
                ):
                    _resp = _strip_code_fences(_resp)
                # The whole answer: a cut here was silent and the parent could
                # never read the rest (``sub_agent_result_text``).
                task2.result = stored_answer(_resp)
                task2.completed_at = datetime.now(UTC)
                # Record the files the loop produced. A wait-budget
                # continuation was handed the files its predecessor wrote (they
                # are in ``inherited_file_ids`` here, so they would not count as
                # produced) - ``CARRIED_FILE_IDS_KEY`` carries them forward so
                # the task the parent finally wakes on names ALL the files the
                # delegation produced, not just the last attempt's.
                recorded_file_ids = await _record_result_files(
                    task2, file_context, inherited_file_ids, answer=task2.result
                )
                if is_incomplete:
                    task2.metadata_json = {
                        **(task2.metadata_json or {}),
                        "stop_reason": stop_reason,
                        "pending_task_ids": pending_ids,
                        "outcome": "wait_budget_exhausted",
                    }

            # Persist result + full agentic conversation on linked AssignmentRun
            if assignment_run_id:
                await update_assignment_run(
                    db,
                    assignment_run_id,
                    "completed",
                    result_summary=clipped(
                        agent_response,
                        RUN_SUMMARY_CHARS,
                        read_call=f'get_full_task_result(task_id="{task_id}")',
                    ),
                    messages=chat_messages,
                )

            # An incomplete (wait-budget) death must not close the delegation
            # as completed — downstream completion-mode flow emissions would
            # then run against unfinished work. Propagate the honest terminal
            # status; ``{{delegation.status}}`` mappings can distinguish it.
            await _update_task_delegation(
                db,
                task_id,
                task2.status if task2 is not None else "completed",
                result=agent_response,
            )
            if _evaluation_chain and task2 is not None:
                task2.metadata_json = with_task_ledger(
                    task2.metadata_json,
                    loop_outcome,
                    _task_retry_meter["retries"],
                    partial=_earlier_attempt_ran,
                )
            await db.commit()
            if task2 and task2.status == "incomplete":
                logger.info(
                    "Sub-agent task %s ended incomplete (waiting_on_tasks, "
                    "pending=%s)",
                    task_id,
                    (task2.metadata_json or {}).get("pending_task_ids"),
                )
            else:
                logger.info("Sub-agent task %s completed successfully", task_id)

            # Proactive delivery: only dump a notice when the parent will
            # NOT wake. Chat completions resume the parent with the full
            # result in a hidden prompt; posting a truncated extract first
            # is what the user sees as "Extract — full result available on
            # the task" with no link. Skip disable_chat /
            # cancelled / incomplete as before.
            if _should_deliver_completion_notice(
                task2,
                disable_chat=bool(getattr(agent, "disable_chat", False)),
                has_response=bool(agent_response),
            ):
                await _deliver_task_result(
                    task_id=task_id,
                    conversation_id=str(task2.conversation_id),
                    agent_name=agent.name if agent else "Agent",
                    result_text=agent_response,
                )

            terminal_event = (
                "incomplete"
                if task2 is not None and task2.status == "incomplete"
                else "completed"
            )
            # Real-time UI signal for the terminal state — published
            # AFTER delivery so the frontend's conversation refetch picks up
            # the delivered completion notice in the same invalidation.
            await publish_task_event(
                user_id,
                terminal_event,
                task_id,
                conversation_id=(
                    str(task2.conversation_id)
                    if task2 and task2.conversation_id
                    else None
                ),
                status=terminal_event,
            )

            if terminal_event == "completed":
                _schedule_parent_wake_if_needed(
                    task2,
                    agent_name=agent_name,
                    terminal_status="completed",
                )

            if (
                task2 is not None
                and task2.status == "incomplete"
                and continuation_depth < _MAX_WAIT_CONTINUATIONS
            ):
                continued = await _spawn_wait_budget_continuation(
                    db=db,
                    user_id=user_id,
                    parent_agent_id=str(task2.parent_agent_id),
                    target_agent_id=target_agent_id,
                    original_task=task_description,
                    pending_ids=pending_ids,
                    conversation_id=(
                        str(task2.conversation_id)
                        if task2.conversation_id
                        else None
                    ),
                    # Reuse the ORIGINAL spawn's parent run so the
                    # continuation counts against (and is fanned out under)
                    # the same cap scope as the task it continues —
                    # ``assignment_run_id`` here is this task's own child
                    # run, not the parent scope the spawn was charged to.
                    parent_run_id=meta.get("parent_run_id") or assignment_run_id,
                    # And under the same cap scope when it had neither.
                    cap_scope=meta.get("cap_scope"),
                    visited_agents=visited_agents,
                    timeout_seconds=timeout_seconds,
                    max_iterations=max_iterations,
                    continuation_of=task_id,
                    continuation_depth=continuation_depth + 1,
                    scope_headers=_scope_headers,
                    file_context=file_context,
                    carried_file_ids=recorded_file_ids,
                    element_scope=element_scope,
                    evaluation_chain=bool(meta.get(EVALUATION_CHAIN_KEY)),
                    evaluation_tool_approval=_evaluation_tool_approval,
                    health_alone=bool(meta.get(HEALTH_ALONE_KEY)),
                    call_output_schema=_call_output_schema,
                )
                if not continued and _evaluation_chain:
                    await _mark_no_continuation(db, task_id)

        return SubAgentTaskOutcome(status="completed")

    except TimeoutError:
        # ``effective_timeout is None`` means the per-task bound was disabled,
        # so the TimeoutError necessarily bubbled up from an inner operation
        # (LLM call, tool execution, ...) — don't cite a bound that wasn't set.
        error_msg = (
            f"Task timed out after {int(effective_timeout)}s"
            if effective_timeout is not None
            else "Task aborted by an inner timeout (per-task timeout disabled)"
        )
        # The deadline's timeout only once the loop reached the bound the
        # deadline set: an inner timeout, or a chat-formula limit shorter than
        # that bound, is the task's own. The turn's own ``wait_for`` runs on
        # whole seconds (``chat_loop_timeout_within``), so it may fire up to a
        # second before that bound.
        deadline_timeout = deadline_stop_no_output or (
            deadline_cut
            and loop_deadline is not None
            and time.monotonic() >= loop_deadline - 1.0
        )
        if deadline_stop_no_output:
            error_msg = (
                "Task stopped by its deadline before any model output: the "
                f"deadline left too little of its {int(budget_timeout or 0)}s "
                "budget for a model call"
            )
        elif deadline_timeout:
            error_msg += (
                f", what its deadline left of its {int(budget_timeout or 0)}s budget"
            )
        logger.error(
            "Sub-agent task %s timed out (target_agent=%s, timeout=%s, "
            "assignment_run=%s, depth=%d, task=%r)",
            task_id,
            target_agent_id,
            effective_timeout,
            assignment_run_id,
            depth,
            task_description[:_LOG_TASK_DESC_CHARS],
        )
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(SubAgentTask)
                    .where(SubAgentTask.id == uuid.UUID(task_id))
                    .with_for_update()
                )
                task = result.scalar_one_or_none()
                # Don't downgrade a task killed mid-run to ``failed`` — keep the
                # cancellation authoritative. The FOR UPDATE lock serializes against
                # a concurrent ``cancel_sub_agent_task`` so a kill landing between
                # this read and the commit can't be overwritten back to ``failed``.
                if _already_cancelled(task):
                    logger.info(
                        "Sub-agent task %s was cancelled; leaving it cancelled "
                        "rather than marking timed-out",
                        task_id,
                    )
                    await _record_ledger_on_killed_task(
                        db,
                        task,
                        evaluation_chain=_evaluation_chain,
                        loop_outcome=loop_outcome,
                        retries=_task_retry_meter["retries"],
                        partial=_earlier_attempt_ran,
                    )
                    return SubAgentTaskOutcome(status="cancelled")
                ended = _ended_elsewhere(task, attempt_place)
                if ended is not None:
                    logger.warning(
                        "Sub-agent task %s had already ended %s; not marking it "
                        "timed-out",
                        task_id,
                        task.status,
                    )
                    return ended
                if task:
                    task.status = "failed"
                    task.error = error_msg
                    task.completed_at = datetime.now(UTC)
                    # The batches written before the deadline still reach the
                    # parent the failure wakes.
                    await _record_result_files(task, file_context, inherited_file_ids)
                    if _evaluation_chain:
                        task.metadata_json = with_task_ledger(
                            task.metadata_json,
                            loop_outcome,
                            _task_retry_meter["retries"],
                            partial=_earlier_attempt_ran,
                        )
                if assignment_run_id:
                    await update_assignment_run(
                        db,
                        assignment_run_id,
                        "failed",
                        error=error_msg,
                        messages=chat_messages or None,
                        failure_cause=(
                            FailureCause.ENGINE_DEADLINE_EXCEEDED
                            if deadline_timeout
                            else None
                        ),
                    )
                await _update_task_delegation(db, task_id, "failed", error=error_msg)
                await db.commit()
                if deadline_timeout:
                    # The engine's timeout, not the agent's: free, exactly as
                    # the dead-letter this bound replaces would have credited.
                    from app.services.quota import refund_agentic_usage_by_receipt

                    await refund_agentic_usage_by_receipt(
                        task_receipt_token(task_id, attempt=attempt),
                        reason="sub-agent task ended by its deadline",
                        db=db,
                    )
                await publish_task_event(
                    user_id,
                    "failed",
                    task_id,
                    conversation_id=(
                        str(task.conversation_id)
                        if task and task.conversation_id
                        else None
                    ),
                    status="failed",
                )
                _schedule_parent_wake_if_needed(
                    task, agent_name=agent_name, terminal_status="failed"
                )
        except Exception:
            logger.error(
                "Failed to mark timed-out sub-agent task %s as failed",
                task_id,
                exc_info=True,
            )
        return SubAgentTaskOutcome(status="failed", error=error_msg)

    except Exception as e:
        is_overflow = isinstance(e, ContextOverflowError)
        if is_overflow:
            logger.warning(
                "Sub-agent task %s hit context overflow (target_agent=%s, "
                "assignment_run=%s, depth=%d, task=%r): %s",
                task_id,
                target_agent_id,
                assignment_run_id,
                depth,
                task_description[:_LOG_TASK_DESC_CHARS],
                format_exception_message(e),
            )
        else:
            logger.error(
                "Sub-agent task %s failed (target_agent=%s, assignment_run=%s, "
                "depth=%d, task=%r): %s",
                task_id,
                target_agent_id,
                assignment_run_id,
                depth,
                task_description[:_LOG_TASK_DESC_CHARS],
                format_exception_message(e),
                exc_info=True,
            )
        # Persisted SubAgentTask.error / AssignmentRun.error feed the UI and
        # notifications - keep the stored reason non-empty too.
        error_msg = format_exception_message(e)[:TRUNCATE_RESULT]
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(SubAgentTask)
                    .where(SubAgentTask.id == uuid.UUID(task_id))
                    .with_for_update()
                )
                task = result.scalar_one_or_none()
                # Don't downgrade a task killed mid-run to ``failed`` — keep the
                # cancellation authoritative. The FOR UPDATE lock serializes against
                # a concurrent ``cancel_sub_agent_task`` so a kill landing between
                # this read and the commit can't be overwritten back to ``failed``.
                if _already_cancelled(task):
                    logger.info(
                        "Sub-agent task %s was cancelled; leaving it cancelled "
                        "rather than marking failed",
                        task_id,
                    )
                    await _record_ledger_on_killed_task(
                        db,
                        task,
                        evaluation_chain=_evaluation_chain,
                        loop_outcome=loop_outcome,
                        retries=_task_retry_meter["retries"],
                        partial=_earlier_attempt_ran,
                    )
                    return SubAgentTaskOutcome(status="cancelled")
                ended = _ended_elsewhere(task, attempt_place)
                if ended is not None:
                    logger.warning(
                        "Sub-agent task %s had already ended %s; not marking it "
                        "failed",
                        task_id,
                        task.status,
                    )
                    return ended
                if task:
                    task.status = "failed"
                    task.error = error_msg
                    task.completed_at = datetime.now(UTC)
                    # Whatever the loop wrote before it raised still reaches
                    # the parent the failure wakes.
                    await _record_result_files(task, file_context, inherited_file_ids)
                    if _evaluation_chain:
                        task.metadata_json = with_task_ledger(
                            task.metadata_json,
                            loop_outcome,
                            _task_retry_meter["retries"],
                            partial=_earlier_attempt_ran,
                        )
                if assignment_run_id:
                    await update_assignment_run(
                        db,
                        assignment_run_id,
                        "failed",
                        error=error_msg,
                        messages=chat_messages or None,
                    )
                await _update_task_delegation(db, task_id, "failed", error=error_msg)
                await db.commit()
                await publish_task_event(
                    user_id,
                    "failed",
                    task_id,
                    conversation_id=(
                        str(task.conversation_id)
                        if task and task.conversation_id
                        else None
                    ),
                    status="failed",
                )
                _schedule_parent_wake_if_needed(
                    task, agent_name=agent_name, terminal_status="failed"
                )
        except Exception:
            logger.error(
                "Failed to mark sub-agent task %s as failed",
                task_id,
                exc_info=True,
            )
        return SubAgentTaskOutcome(status="failed", error=error_msg)


async def _deliver_task_result(
    *,
    task_id: str,
    conversation_id: str,
    agent_name: str,
    result_text: str,
) -> None:
    """Proactively deliver a completed background task result to the user.

    Stores the result as a new assistant message in the originating
    conversation and — for external channels (Teams, Slack, etc.) — sends
    a proactive message via the channel adapter so the user sees the output
    even if they're offline.
    """
    from app.core.database import async_session

    try:
        async with async_session() as db:
            # Defense-in-depth re-check: the cancel cascade may have flagged
            # the task between the caller's delivery decision and now —
            # reload it fresh and skip if delivery was suppressed meanwhile.
            task_result = await db.execute(
                select(SubAgentTask).where(SubAgentTask.id == uuid.UUID(task_id))
            )
            task_row = task_result.scalar_one_or_none()
            if _delivery_suppressed(task_row):
                logger.info(
                    "Skipping delivery of task %s result — parent request "
                    "was cancelled",
                    task_id,
                )
                return

            from app.models.chat import Conversation

            conv_result = await db.execute(
                select(Conversation).where(
                    Conversation.id == uuid.UUID(conversation_id)
                )
            )
            conv = conv_result.scalar_one_or_none()
            if not conv:
                logger.debug(
                    "Cannot deliver task %s result — conversation %s not found",
                    task_id,
                    conversation_id,
                )
                return

            # Lean delivery only when the parent will not wake. Chat
            # completions resume the parent with the full result instead
            # of flooding the thread with an extract.
            # Posted into the Slack / Teams thread too: in its owner's language.
            delivery_text = _format_completion_notice(
                agent_name,
                result_text,
                await user_notice_language(db, conv.user_id)
                if conv.channel in ("slack", "teams")
                else DEFAULT_LANGUAGE,
            )

            # The files the task produced ride on the notice as attachments:
            # the chat renders a ``[[FILE:id]]`` marker only when the
            # message carries the file, and a later turn rehydrates its
            # FileContext from the messages' attachments. The rows are bound to
            # the conversation + this message so the chip's download URL
            # resolves (a sub-agent's file is born with neither). The
            # admission read runs on its own session so a failed read cannot
            # poison the one the notice is persisted on; the bind is atomic
            # and the ``attachments`` block names only what it bound.
            from app.services.sub_agent_files import (
                attach_rows_to_message,
                deliverable_rows,
                load_attachment_rows,
            )

            attachment_rows: list = []
            try:
                # The task's deliverables, and a working file (a check's
                # picture) only when the notice names it.
                attachment_rows = deliverable_rows(
                    await load_attachment_rows(
                        task_result_file_ids(task_row),
                        conversation_id=conv.id,
                    ),
                    delivery_text,
                )
            except Exception:
                logger.warning(
                    "Could not load the files of task %s for its completion "
                    "notice; delivering the text alone",
                    task_id,
                    exc_info=True,
                )

            from app.channels.router import store_message

            notice_msg = await store_message(
                db,
                conv.id,
                role="assistant",
                content=delivery_text,
                channel=conv.channel or "web",
            )
            if attachment_rows:
                await attach_rows_to_message(
                    db, notice_msg, attachment_rows, conversation_id=conv.id
                )
            await db.commit()

            logger.info(
                "Delivered task %s result to conversation %s (channel=%s)",
                task_id,
                conversation_id,
                conv.channel,
            )

            # For external channels, send a proactive message via the
            # platform API so the user gets a notification.
            if conv.channel and conv.channel not in ("web", "web_embed"):
                await _send_proactive_channel_message(
                    channel=conv.channel,
                    channel_thread_id=conv.channel_thread_id,
                    text=delivery_text,
                    agent_name=agent_name,
                )

    except Exception as exc:
        logger.warning(
            "Failed to deliver task %s result to conversation %s: %s",
            task_id,
            conversation_id,
            format_exception_message(exc),
        )


async def _send_proactive_channel_message(
    *,
    channel: str,
    channel_thread_id: str | None,
    text: str,
    agent_name: str,
) -> None:
    """Send a proactive message to an external channel.

    Uses the channel adapter's send_message() method with credentials
    loaded from the channel bot configuration.
    """
    if not channel_thread_id:
        logger.debug("No channel_thread_id — cannot send proactive message")
        return

    try:
        from app.channels.base import OutboundMessage

        # A channel is a person's surface: a secret variable value
        # the turn echoed, or its stored marker, reaches it as the display text.
        outbound = OutboundMessage(
            text=redact_known_secrets_for_display(text),
            channel_thread_id=channel_thread_id,
        )

        if channel == "teams":
            from app.channels.teams import TeamsChannel, _get_teams_creds

            creds = await _get_teams_creds()
            if not creds:
                logger.warning(
                    "Cannot send proactive Teams message — no bot credentials"
                )
                return
            token, bot_id = creds

            service_url = None
            # The channel_thread_id for Teams IS the conversation ID — use
            # it directly as the Bot Framework conversation reference.
            # Try to get the service URL from Redis (cached per tenant).
            from app.core.redis import redis_client

            redis = await redis_client()
            cursor: int | bytes = 0
            while True:
                cursor, keys = await redis.scan(
                    cursor, match="teams:service_url:*", count=10
                )
                if keys:
                    service_url = await redis.get(keys[0])
                    if isinstance(service_url, bytes):
                        service_url = service_url.decode()
                    break
                if not cursor:
                    break

            if not service_url:
                service_url = "https://smba.trafficmanager.net/amer/"

            adapter = TeamsChannel(
                service_url=service_url,
                conversation_id=channel_thread_id,
                access_token=token,
                bot_id=bot_id,
            )
            await adapter.send_message(outbound)
            logger.info("Sent proactive Teams message for background task")

        elif channel == "slack":
            from app.channels.base import get_channel_bot
            from app.channels.slack import SlackChannel
            from app.services.vault import reveal_field

            bot = await get_channel_bot("slack_bot")
            if not bot or not bot.api_key_enc:
                logger.warning(
                    "Cannot send proactive Slack message — no bot credentials"
                )
                return
            slack_token = await reveal_field(bot, "api_key_enc") or ""
            # Slack thread ids are stored composite ("{channel_id}:{thread_ts}").
            # SlackChannel.send_message posts to ``channel_user_id or
            # channel_thread_id`` — passing the composite id as the channel
            # produced ``channel_not_found`` on every delivery.  Split it into
            # the channel argument and the thread reference so the notice
            # lands in the original thread.
            slack_channel_id, _, slack_thread_ts = channel_thread_id.partition(":")
            outbound.channel_user_id = slack_channel_id
            if slack_thread_ts:
                outbound.metadata["thread_ts"] = slack_thread_ts
            adapter = SlackChannel(slack_token)
            await adapter.send_message(outbound)
            logger.info("Sent proactive Slack message for background task")

        else:
            logger.debug(
                "Proactive messaging not implemented for channel %s — "
                "result stored in conversation only",
                channel,
            )

    except Exception as exc:
        logger.warning(
            "Failed to send proactive %s message: %s",
            channel,
            format_exception_message(exc),
        )


async def _mark_run_held(
    db: AsyncSession, assignment_run_id: str, refusal: str, *, back_to_pending: bool = False
) -> bool:
    """Carry a held delegation's hold marker on its linked run, where the
    Activity views show it, and say whether it wrote it - nothing is
    written when the run already carries it. Best effort: the task itself
    carries it. ``back_to_pending``: the run was marked running with its task,
    and waits again with it."""
    from app.services import ai_hold

    run_uuid = safe_uuid(assignment_run_id)
    if run_uuid is None:
        return False
    try:
        from app.models.assignment import AssignmentRun

        # A savepoint: a failed statement aborts the whole PostgreSQL
        # transaction, and the caller commits the task's own marker on it.
        async with db.begin_nested():
            run = (
                await db.execute(select(AssignmentRun).where(AssignmentRun.id == run_uuid))
            ).scalar_one_or_none()
            marker = (
                (run.trigger_context or {}).get(ai_hold.HOLD_MARKER_KEY) if run is not None else None
            )
            if run is None:
                return False
            if back_to_pending:
                run.status = "pending"
                run.started_at = None
            elif isinstance(marker, dict) and marker.get("reason") == refusal:
                return False
            run.trigger_context = ai_hold.with_hold_marker(run.trigger_context, refusal)
        return True
    except Exception as exc:
        logger.warning(
            "Could not mark AssignmentRun %s held: %s",
            assignment_run_id,
            format_exception_message(exc),
        )
        return False


async def _refuse_task_before_turn(
    db: AsyncSession,
    task: SubAgentTask,
    *,
    task_id: str,
    user_id: str,
    assignment_run_id: str | None,
    refusal: str,
    hold: bool,
    was_held: bool,
    agent_name: str,
    started: bool = False,
) -> SubAgentTaskOutcome:
    """Hold a task whose agent turn cannot run yet or, once the hold
    is over, fail it and wake its parent.

    ``started``: the task was marked running - by this attempt, whose turn's
    quota check met the outage its start's probe missed, or by an
    attempt the engine lost. Held, it goes back to pending with its
    linked run and delegation, as if the start had found it.
    """
    from app.services import ai_hold

    if hold:
        changed = started
        marker = (task.metadata_json or {}).get(ai_hold.HOLD_MARKER_KEY)
        if not isinstance(marker, dict) or marker.get("reason") != refusal:
            task.metadata_json = ai_hold.with_hold_marker(task.metadata_json, refusal)
            changed = True
        # The deferral restores the attempt count.
        deferred = task_fence_for_deferral(task.metadata_json)
        if deferred is not None:
            task.metadata_json = deferred
            changed = True
        if started:
            task.status = "pending"
            task.started_at = None
            await _update_task_delegation(db, task_id, "pending")
        # Asked on every re-check: a linked-run write that failed once
        # (best effort) is written at the next one.
        if assignment_run_id and await _mark_run_held(
            db, assignment_run_id, refusal, back_to_pending=started
        ):
            changed = True
        if changed:
            await db.commit()
        logger.info("Sub-agent task %s held (%s)", task_id, refusal)
        return SubAgentTaskOutcome(
            status="held", error=ai_hold.held_message(refusal), refusal=refusal
        )
    logger.info("Sub-agent task %s refused before its turn ran (%s)", task_id, refusal)
    return await _fail_task_before_turn(
        db,
        task,
        task_id=task_id,
        user_id=user_id,
        assignment_run_id=assignment_run_id,
        error_msg=ai_hold.expired_message(refusal, was_held=was_held),
        agent_name=agent_name,
        failure_cause=ai_hold.failure_cause_of(refusal),
    )


async def _mark_no_continuation(db: AsyncSession, task_id: str) -> None:
    """Tell an evaluation's wait that this ``incomplete`` task has no continuation.

    The continuation is spawned after the task's terminal commit, and may not be
    (no pending task, the fan-out cap, an error): without the mark, the wait
    looks for one until its bound. A failure here only costs the early stop.
    """
    try:
        task = (
            await db.execute(
                select(SubAgentTask)
                .where(SubAgentTask.id == uuid.UUID(task_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            return
        task.metadata_json = {**(task.metadata_json or {}), NO_CONTINUATION_KEY: True}
        await db.commit()
    except Exception as exc:
        logger.warning(
            "Could not mark task %s as having no continuation: %s",
            task_id,
            format_exception_message(exc),
        )
        with contextlib.suppress(Exception):
            await db.rollback()


def _record_no_turn_ledger(task: SubAgentTask) -> None:
    """Give a task of an evaluation's chain that ends before its turn an empty ledger.

    Nothing was spent, and the evaluation reads the empty ledger as a measured
    zero; with none at all, it would report the entry's totals unavailable.
    """
    if (task.metadata_json or {}).get(EVALUATION_CHAIN_KEY):
        task.metadata_json = with_task_ledger(task.metadata_json, {}, 0)


async def _record_ledger_on_killed_task(
    db: AsyncSession,
    task: SubAgentTask | None,
    *,
    evaluation_chain: bool,
    loop_outcome: dict,
    retries: int,
    partial: bool,
) -> None:
    """Keep the ledger of a task of an evaluation's chain killed while its turn ran.

    The worker runs on to its terminal write, which keeps ``cancelled``; the
    run's calls were still made, so the ledger is written beside that status.
    The caller holds the row FOR UPDATE.
    """
    if not evaluation_chain or task is None:
        return
    task.metadata_json = with_task_ledger(task.metadata_json, loop_outcome, retries, partial=partial)
    await db.commit()


async def _fail_task_before_turn(
    db: AsyncSession,
    task: SubAgentTask,
    *,
    task_id: str,
    user_id: str,
    assignment_run_id: str | None,
    error_msg: str,
    agent_name: str,
    failure_cause: FailureCause | None,
) -> SubAgentTaskOutcome:
    """Fail a task whose agent turn never ran, and wake its parent.

    Nothing of the task's own is written: the files an earlier attempt
    recorded stay on the row the parent's wake reads. A task of an
    evaluation's chain gets an empty ledger (``_record_no_turn_ledger``).
    """
    task.status = "failed"
    task.error = error_msg
    task.completed_at = datetime.now(UTC)
    _record_no_turn_ledger(task)
    if assignment_run_id:
        await update_assignment_run(
            db,
            assignment_run_id,
            "failed",
            error=error_msg,
            failure_cause=failure_cause,
        )
    await _update_task_delegation(db, task_id, "failed", error=error_msg)
    await db.commit()
    await publish_task_event(
        user_id,
        "failed",
        task_id,
        conversation_id=str(task.conversation_id) if task.conversation_id else None,
        status="failed",
    )
    _schedule_parent_wake_if_needed(task, agent_name=agent_name, terminal_status="failed")
    return SubAgentTaskOutcome(status="failed", error=error_msg)


async def _complete_task_before_turn(
    db: AsyncSession,
    task: SubAgentTask,
    *,
    task_id: str,
    user_id: str,
    assignment_run_id: str | None,
    answer: str,
    agent_name: str,
) -> SubAgentTaskOutcome:
    """Complete a task whose answer is known before its turn, and wake its parent.

    No model call is made and nothing is charged: the task's result is
    *answer*, its linked run and delegation are completed with it. A task of
    an evaluation's chain gets an empty ledger (``_record_no_turn_ledger``).
    """
    task.status = "completed"
    task.result = stored_answer(answer)
    task.completed_at = datetime.now(UTC)
    _record_no_turn_ledger(task)
    if assignment_run_id:
        await update_assignment_run(
            db,
            assignment_run_id,
            "completed",
            result_summary=clipped(
                answer,
                RUN_SUMMARY_CHARS,
                read_call=f'get_full_task_result(task_id="{task_id}")',
            ),
        )
    await _update_task_delegation(db, task_id, "completed", result=answer)
    await db.commit()
    logger.info("Sub-agent task %s completed before its turn: its work item is finished", task_id)
    await publish_task_event(
        user_id,
        "completed",
        task_id,
        conversation_id=str(task.conversation_id) if task.conversation_id else None,
        status="completed",
    )
    _schedule_parent_wake_if_needed(task, agent_name=agent_name, terminal_status="completed")
    return SubAgentTaskOutcome(status="completed")


async def _fail_task_out_of_time(
    db: AsyncSession,
    task: SubAgentTask,
    *,
    task_id: str,
    user_id: str,
    assignment_run_id: str | None,
    deadline_at: datetime | None,
    agent_name: str,
) -> SubAgentTaskOutcome:
    """Fail a task its deadline left no time to run, before its turn is charged."""
    logger.warning(
        "Sub-agent task %s not run: its deadline %s left none of its budget",
        task_id,
        deadline_at,
    )
    return await _fail_task_before_turn(
        db,
        task,
        task_id=task_id,
        user_id=user_id,
        assignment_run_id=assignment_run_id,
        error_msg=deadline_spent_message("Task", deadline_at),
        agent_name=agent_name,
        failure_cause=FailureCause.ENGINE_DEADLINE_EXCEEDED,
    )


async def _target_agent_name(db: AsyncSession, target_agent_id: str, default: str) -> str:
    """The name the parent's wake gives the target agent, before it is loaded."""
    target_uuid = safe_uuid(target_agent_id)
    if target_uuid is None:
        return default
    name = (
        await db.execute(select(Agent.name).where(Agent.id == target_uuid))
    ).scalar_one_or_none()
    return name or default


def _carry_recorded_files(task: SubAgentTask) -> None:
    """Keep the files a lost attempt recorded on the record the re-run writes.

    The re-run's record is its carried ids and what its own loop produces; the
    lost attempt's batches are neither, and its first write would drop them
    from the row the parent is woken with.
    """
    carried = task_result_file_ids(task, key=CARRIED_FILE_IDS_KEY)
    merged = merge_result_file_ids(carried, task_result_file_ids(task))
    if merged != carried:
        raw_meta = task.metadata_json
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        task.metadata_json = {**meta, CARRIED_FILE_IDS_KEY: merged}


async def update_assignment_run(
    db: AsyncSession,
    assignment_run_id: str,
    status: str,
    *,
    result_summary: str | None = None,
    error: str | None = None,
    messages: list | None = None,
    failure_cause: FailureCause | None = None,
) -> None:
    """Update a linked AssignmentRun's status, result/error, and messages.

    When ``messages`` is provided (the full agentic conversation from
    ``run_agent_chat``), they are serialized and stored in the AssignmentRun
    so the run detail page can display the full tool call traces / reasoning.
    ``failure_cause`` stamps a cause the caller knows structurally.
    """
    try:
        from app.models.assignment import AssignmentRun

        wr_result = await db.execute(
            select(AssignmentRun).where(
                AssignmentRun.id == uuid.UUID(assignment_run_id)
            )
        )
        wr = wr_result.scalar_one_or_none()
        if wr:
            previous_status = wr.status
            wr.status = status
            wr.completed_at = datetime.now(UTC)
            if status in ("completed", "failed", "cancelled"):
                # Registry hygiene: the run reached a terminal state under
                # this process's control, so the shutdown sweep no longer
                # needs it.  Untracked only AFTER the terminal status is
                # applied to the ORM row — untracking before the lookup
                # would drop a run whose SELECT/update then failed, leaving
                # it active in the DB but invisible to the shutdown sweep
                # (back to the ~1 h stale fallback).  The remaining window
                # (caller's commit rolls back after this point) is far
                # narrower and still covered by the periodic
                # ``_cleanup_stale_assignment_runs`` sweep.
                from app.services.in_process_runs import untrack_in_process_run

                untrack_in_process_run(assignment_run_id)
            if result_summary is not None:
                wr.result_summary = result_summary
            if error is not None:
                wr.error = error
            if failure_cause is not None:
                wr.failure_cause = failure_cause.value
                wr.failure_determination = FailureDetermination.STRUCTURAL.value
            if messages:
                try:
                    from app.services.builtin_tools import serialize_messages

                    wr.messages = serialize_messages(messages)
                except Exception as ser_err:
                    logger.warning(
                        "Failed to serialize messages for AssignmentRun %s: %s",
                        assignment_run_id,
                        format_exception_message(ser_err),
                    )
            # A delegated child run (no assignment) ends here, never through the
            # engine: it raises run.completed / run.failed itself. An assignment
            # run is published by the engine at its own ending.
            if wr.assignment_id is None and status in ("completed", "failed") and previous_status != status:
                _publish_delegated_run_ended_after_commit(db, wr, status, error)
    except Exception as e:
        logger.warning("Failed to update AssignmentRun %s: %s", assignment_run_id, format_exception_message(e))


def _publish_delegated_run_ended_after_commit(db: AsyncSession, run: Any, status: str, error: str | None) -> None:
    """Raise ``run.completed`` / ``run.failed`` for a delegated child run once *db* commits.

    The event carries ``parent_run_id`` (and the delegating run's agent in
    scope), as an assignment run's does. A snapshot of the row is taken now:
    the ORM object is expired by the commit. A rolled-back ending publishes
    nothing. Never raises.

    A run ended ``cancelled`` (its task cancelled by the parent run, the user
    or an administrator, #2721) raises ``run.failed`` with ``reason:
    "cancelled"`` and ``cancelled: true``: no event of its own, so a
    ``run.failed`` listener sees every ending that is not a completion and
    filters ``cancelled`` to tell a chosen stop from a defect.
    """
    try:
        from types import SimpleNamespace

        from sqlalchemy import event as sa_event

        snapshot = SimpleNamespace(
            id=run.id,
            agent_id=run.agent_id,
            assignment_id=run.assignment_id,
            parent_run_id=run.parent_run_id,
            trigger_context=run.trigger_context,
            user_id=run.user_id,
            is_company_managed=run.is_company_managed,
            stop_reason=getattr(run, "stop_reason", None),
        )
        name = "run.failed" if status == "cancelled" else f"run.{status}"
        reason = status

        def _after_commit(_session: Any) -> None:
            try:
                from app.services.assignment_engine import publish_run_event

                task = asyncio.get_running_loop().create_task(
                    publish_run_event(snapshot, name, reason=reason, error=error)
                )
                _DELEGATED_RUN_EVENT_TASKS.add(task)
                task.add_done_callback(_DELEGATED_RUN_EVENT_TASKS.discard)
            except RuntimeError:
                logger.debug("%s of delegated run %s not published: no event loop", name, snapshot.id)

        sa_event.listen(db.sync_session, "after_commit", _after_commit, once=True)
    except Exception as exc:
        logger.warning(
            "run.%s of delegated run %s not scheduled: %s",
            status,
            getattr(run, "id", None),
            format_exception_message(exc),
        )


# Strong references to the in-flight publishes, so a pending task is never collected.
_DELEGATED_RUN_EVENT_TASKS: set[asyncio.Task[None]] = set()
