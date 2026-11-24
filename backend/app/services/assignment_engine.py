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

"""Assignment execution engine.

Handles the full lifecycle of an assignment run:
  1. Receive trigger (schedule tick, webhook payload, integration event, manual)
  2. Apply optional delay (schedule delayed execution via Redis)
  3. Build execution context (agent persona, integrations, MCP tools, knowledge bases)
  4. Run the agentic loop (LLM + tools) with the assignment prompt
  5. Record results in AssignmentRun

The AI handles orchestration: given the trigger payload and prompt, it uses
available tools to analyze and act. No hard-coded step graphs.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import html
import json
import logging
import random
import re
import time
import uuid as _uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

import structlog
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only
from sqlalchemy.orm.exc import StaleDataError

from app.api.v1.helpers import (
    ContinuedRunNotOpenableError,
    can_open_continued_run,
    company_managed_run_scoped,
    continuing_run_is_company_managed,
    mark_company_managed_run,
)
from app.core.config import settings
from app.core.constants import (
    TRUNCATE_ERROR,
    TRUNCATE_LABEL,
    TRUNCATE_QUESTION,
    TRUNCATE_RESULT,
)
from app.core.database import async_session, release_db_connection_before_loop
from app.core.feature_flags import is_feature_enabled
from app.core.hot_imports import ensure_hot_path_imports
from app.core.redaction import known_secrets_scoped
from app.core.security_options import security_option
from app.core.turn_writer import SUPERSEDED_MESSAGE, TurnSuperseded, ensure_single_writer
from app.core.utils import format_exception_message, parse_delay
from app.core.worker_shutdown import TurnWoundDown
from app.integrations.stale_references import (
    StaleIntegrationReferenceError,
    StaleReferenceCollector,
    collect_stale_references,
    describe_reference,
)
from app.llm.attribution import provider_config_metadata
from app.llm.base import ContextOverflowError, overflow_error_type
from app.llm.provider_access import bind_acting_user
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun, AssignmentRunConversationNotice
from app.services import (
    assignment_dispatch,
    assignment_run_resume,
    chat_reply_guard,
    chat_turn_coalescing,
    event_batching,
    event_coalescing,
    event_run_dedup,
)
from app.services.agent_posts import set_posting_assignment
from app.services.agentic_executions import (
    SOURCE_ASSIGNMENT,
    record_agentic_execution,
)
from app.services.assignment_helpers import (
    _build_elements_context,
    _build_system_message,
    _build_trigger_information,
    _load_memory_context,
    _publish_run_status,
    _render_prompt,
    _warn_unresolved_placeholders,
    assignment_memory_scope,
    resolve_assignment_kb_access,
    select_best_result_summary,
    store_assignment_agent_memory,
)
from app.services.assignment_pause import (
    OWNER_DEACTIVATED_RESUME_ERROR,
    OWNER_DEACTIVATED_RUN_ERROR,
    AssignmentOwnerDeactivatedError,
    is_paused_by_deactivated_owner,
    refuse_if_owner_deactivated,
)
from app.services.attempt_charges import (
    attempt_fence,
    claim_charge_record,
    credit_recorded_charge,
    fence_attempt,
    fence_for_deferral,
    new_charge_id,
    open_pause_epoch,
    release_charge_record,
    replaceable_charge_id,
    settle_lost_charge,
    take_lost_charge,
    without_charge_state,
)
from app.services.builtin_tools import (
    filter_integration_tools,
    make_task_condition,
    merge_tool_allow_lists,
    offered_handover_types,
)
from app.services.decision_waits import holds_assignment
from app.services.element_scope import assignment_run_scope
from app.services.event_bus import (
    ASSIGNMENT_RUN_COMPLETED,
    ASSIGNMENT_RUN_FAILED,
    ASSIGNMENT_RUN_STARTED,
    event_bus,
)
from app.services.failure_causes import (
    FailureCause,
    FailureClassification,
    FailureContext,
    FailureDetermination,
    classify_run_failure,
)
from app.services.frictions import binding_friction_scope
from app.services.frictions import make_scope as make_friction_scope
from app.services.health.identity import health_delegation_owner_id, is_health_agent, mark_health_run
from app.services.integration_user_credentials import connected_for_clause
from app.services.orchestration_engine import (
    bound_by_deadline,
    create_execution,
    deadline_cut_budget,
    deadline_spent,
    deadline_spent_message,
    notify_orchestration_wake,
    queued_deadline,
)
from app.tools.base import FileContext, HumanInputRequestedError, ScopedToolExecutor
from app.tools.wait_policy import WaitPolicy

if TYPE_CHECKING:
    from app.services.agentic_gate import AgenticVerdict
    from app.services.assignment_activation_check import ActivationCheckOutcome

logger = logging.getLogger(__name__)


# AssignmentRun status vocabulary (see ``AssignmentRun.status``):
#   pending → delayed → running → awaiting_human → completed / failed
#
# ACTIVE statuses are the non-terminal ones — a run in any of these states
# still owns the assignment and blocks overlapping executions. This is the
# single source of truth for the overlap guard used by the scheduler, the
# manual-trigger API, and ``trigger_assignment(skip_if_active=True)``.
ACTIVE_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "delayed",
    "running",
    "awaiting_human",
)

# A delivery that was ACCEPTED but cannot start yet because the assignment is
# busy (see ``trigger_or_queue_assignment``). Deliberately NOT in
# ``ACTIVE_RUN_STATUSES``: a queued run owns no execution row and no lease, so
# it must not make ``has_active_run`` true (that would make the queue block
# itself and never drain), and it must not be swept by the stale-run cleanup —
# waiting in the queue is not being stuck. It is not terminal either: the
# reconciler promotes it to ``pending`` / ``delayed`` via
# ``drain_assignment_queues`` as soon as the assignment goes idle.
QUEUED_RUN_STATUS = "queued"

# TERMINAL statuses can never be left — ``execute_assignment_run`` refuses to
# re-execute a run in one of these states. "completed" and "failed" are the
# statuses the engine writes today; "skipped" and "cancelled" are included
# defensively ("skipped" is treated as terminal by the follow-up endpoint,
# "cancelled" is reserved for a future explicit-cancellation feature).
# "awaiting_human" is deliberately NOT terminal: HITL resume re-enters
# ``execute_assignment_run`` on a paused run.
TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "skipped",
    "cancelled",
)

# What ``execute_assignment_run`` returns when it left the run exactly as it
# was for the orchestration engine to retry: whether agentic AI may run could
# not be read. The assignment adapter maps it to a retryable failure.
RUN_DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class RunHeld:
    """What ``execute_assignment_run(..., hold=True)`` returns when agentic AI
    is unavailable: the run was left as it was - pending, delayed, or
    paused with its human answer recorded - carrying the hold marker, and the
    assignment adapter holds its execution. ``refusal`` is the gate's code."""

    refusal: str


class AssignmentTimeoutError(TimeoutError):
    """The run's own wall-clock bound fired around the agent loop.

    ``run_stop_reason`` is what the failed run records as its ``stop_reason``
    (#1695): without it a run the timeout stopped read like any other failure.
    """

    run_stop_reason = "timeout"


class DeadlineCutTimeoutError(AssignmentTimeoutError):
    """A run's loop timed out on a bound its orchestration deadline cut short
    of its budget by more than the grace (``deadline_cut_budget``): the
    engine's timeout, credited back free and recorded as
    ``engine_deadline_exceeded``, as the dead-letter it replaces was."""

    run_stop_reason = "deadline"


def _unstart_reclaimed_run(run: AssignmentRun, reclaimed: bool) -> bool:
    """Put a reclaimed run back to ``pending`` while its execution is held.

    A lost attempt left it ``running``: held, it would read as running for the
    whole hold. ``started_at`` stays: that attempt did start part of the work.
    Returns whether the row changed.
    """
    if not reclaimed or run.status != "running":
        return False
    run.status = "pending"
    return True


# Strong references to internal fire-and-forget tasks (e.g. post-run memory
# persistence) so they aren't garbage-collected before completion.
_background_tasks: set[asyncio.Task] = set()

# Namespace for the per-assignment trigger-overlap advisory lock (the first
# key of ``pg_advisory_xact_lock(int4, int4)``), so the assignment hash can't
# collide with advisory locks taken elsewhere for unrelated features (e.g.
# the sub-agent fan-out cap in ``sub_agent.py`` or the KB sync lock in
# ``kb_sync.py``).
_TRIGGER_OVERLAP_LOCK_NS = 0x41545247  # "ATRG" — Assignment TRigger Guard


async def _acquire_trigger_overlap_lock(
    db: AsyncSession, assignment_id: _uuid.UUID
) -> None:
    """Serialize concurrent ``skip_if_active`` triggers for one assignment.

    The check-then-insert in ``trigger_assignment`` is not atomic on its own:
    two concurrent triggers (across workers/processes) could both pass
    ``has_active_run`` before either commits its new run, defeating the
    overlap guard. A transaction-scoped Postgres advisory lock (same pattern
    as the sub-agent fan-out cap in ``sub_agent.py``) makes the guard atomic:
    the second trigger blocks here until the first transaction commits its
    run, then observes it via ``has_active_run`` and skips.

    The lock auto-releases when the surrounding transaction commits or rolls
    back. ``trigger_assignment`` commits on BOTH outcomes — after inserting
    the run on the create path and immediately before returning ``None`` on
    the skip path — so the lock is only ever held for the duration of the
    guarded check-then-insert itself, never across a caller's fan-out loop.
    (Should an unexpected error escape between this lock and that commit,
    the caller's session rollback/close still releases it.)
    """
    # Cast the namespace to int4 explicitly so it resolves to the
    # pg_advisory_xact_lock(int4, int4) overload alongside hashtext()
    # (which returns int4) — an untyped bind param could otherwise bind
    # as int8 and fail overload resolution.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), hashtext(:scope))"),
        {
            "ns": _TRIGGER_OVERLAP_LOCK_NS,
            "scope": f"assignment_trigger:{assignment_id}",
        },
    )


def _dedup_agent_id(assignment: Assignment) -> _uuid.UUID | None:
    """The agent a run's dedup key is scoped to, or ``None`` when the assignment names none.

    ``event_run_dedup.run_dedup_key`` sits on every dispatch path before the
    slot and queue decision: an exception there drops the event it was about
    to key, so the agent is read tolerantly. An assignment object without an
    ``agent_id`` keys the event as if no agent were known (``agent_id=None``);
    the agent-wide lock and the duplicate check only run on a keyed event.
    """
    return getattr(assignment, "agent_id", None)


async def _acquire_agent_dedup_lock(db: AsyncSession, agent_id: _uuid.UUID) -> None:
    """Serialize the dedup check of event-triggered runs across one agent's assignments.

    The per-assignment overlap lock cannot close the race between two
    assignments of the same agent starting a run for the same inbound event
    (two mailboxes the mail reached, a notification and the poll behind it):
    each holds its own lock, both read no duplicate, both insert. A second
    transaction-scoped advisory lock keyed by the agent makes
    ``duplicate_event_run`` + insert atomic across them. Taken AFTER the
    assignment lock, always in that order, so two triggers never wait on each
    other; released by the commit every trigger path makes.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), hashtext(:scope))"),
        {
            "ns": _TRIGGER_OVERLAP_LOCK_NS,
            "scope": f"agent_event_dedup:{agent_id}",
        },
    )


async def duplicate_event_run(
    db: AsyncSession,
    agent_id: _uuid.UUID,
    dedup_key: str,
    *,
    window_seconds: int,
) -> _uuid.UUID | None:
    """The run of *agent_id* that already carries *dedup_key* within the window, or ``None``.

    For a key rendered from ``trigger_config.dedup`` (#10622) a run its gate
    skipped does not count either: it stood down on the facts of its own
    instant, so a later delivery of the same key still gets its run.

    Across the agent's assignments, whatever their delivery path. A run that
    was itself refused as a duplicate does not count: its original does.
    Queued runs count: a delivery waiting for a slot is a run of that event.
    A run that failed or was cancelled does not count either: the event got
    no answer from it, so the next copy may run.
    """
    if window_seconds <= 0 or not dedup_key:
        return None
    since = datetime.now(UTC) - timedelta(seconds=window_seconds)
    ended_without_answer = ["failed", "cancelled"]
    if event_run_dedup.is_configured_key(dedup_key):
        ended_without_answer.append("skipped")
    result = await db.execute(
        select(AssignmentRun.id)
        .where(
            AssignmentRun.agent_id == agent_id,
            AssignmentRun.created_at >= since,
            AssignmentRun.status.not_in(ended_without_answer),
            AssignmentRun.trigger_context[event_run_dedup.DEDUP_KEY_CONTEXT].as_string() == dedup_key,
            AssignmentRun.trigger_context[event_run_dedup.DUPLICATE_OF_CONTEXT].as_string().is_(None),
        )
        .order_by(AssignmentRun.created_at, AssignmentRun.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _skip_duplicate_event_run(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    payload: dict | None,
    trigger_context: dict,
    *,
    original_run_id: _uuid.UUID | str,
    window_seconds: int,
    triggered_by_user_id: _uuid.UUID | None = None,
    claimed_by: str | None = None,
) -> str:
    """Record a run refused as a duplicate of *original_run_id* and commit.

    The refusal is a ``skipped`` run (visible in the run list, never executed:
    no orchestration execution is created) whose ``result_summary`` names the
    original, counted in the assignment's filtered events. The commit releases
    the advisory locks the caller took. *claimed_by* is set when the original
    is not a run but another pipeline of the agent that claimed the event
    (``services/inbound_claims.py``): *original_run_id* is then that
    claim's holder.
    """
    from app.services.assignment_helpers import resolve_hide_runs
    from app.services.integration_event_filters import record_filter_reasons, record_filtered_events

    key = trigger_context.get(event_run_dedup.DEDUP_KEY_CONTEXT, "")
    now = datetime.now(UTC)
    run = AssignmentRun(
        assignment_id=assignment.id,
        agent_id=assignment.agent_id,
        user_id=_execution_identity(assignment, triggered_by_user_id, agent),
        triggered_by_user_id=triggered_by_user_id,
        is_company_managed=assignment.is_company_managed and not resolve_hide_runs(agent, assignment),
        trigger_payload=payload or {},
        trigger_context={**trigger_context, event_run_dedup.DUPLICATE_OF_CONTEXT: str(original_run_id)},
        status="skipped",
        result_summary=(
            event_run_dedup.claimed_skip_summary(claimed_by, key, window_seconds)
            if claimed_by
            else event_run_dedup.duplicate_skip_summary(original_run_id, key, window_seconds)
        ),
        started_at=now,
        completed_at=now,
    )
    db.add(run)
    await db.flush()
    run_id = str(run.id)
    await db.commit()
    logger.info(
        "Assignment '%s' (%s): run %s skipped as a duplicate of run %s (dedup key %s, window %d s)",
        assignment.name,
        assignment.id,
        run_id,
        original_run_id,
        key,
        window_seconds,
    )
    await record_filtered_events(assignment.id, 1)
    # Readable on the assignment (``Dropped by reason``), never a payload value.
    await record_filter_reasons(assignment.id, {event_run_dedup.drop_reason(key): 1})
    return run_id


async def _event_claimed_elsewhere(agent_id: _uuid.UUID, dedup_key: str, window_seconds: int) -> str | None:
    """Claim *dedup_key* for the agent's assignments; the other holder when one has it.

    An agent account of the same agent may hear the same mail and answer it
    (``services/agent_accounts/pipeline.py``): the run database the engine
    dedups on never sees it, the claim both take does
    (``services/inbound_claims.py``). Fail open: Redis down answers ``None``.
    """
    from app.services import inbound_claims

    return await inbound_claims.claim(
        agent_id, dedup_key, inbound_claims.ASSIGNMENT_HOLDER, window_seconds=window_seconds
    )


async def _release_event_claim(agent_id: _uuid.UUID, dedup_key: str | None) -> None:
    """Give back the claim of a delivery that created no run, so another pipeline may answer it."""
    from app.services import inbound_claims

    await inbound_claims.release(agent_id, dedup_key, inbound_claims.ASSIGNMENT_HOLDER)


async def _red_head_carried(db: AsyncSession, agent_id: _uuid.UUID, dedup_key: str) -> _uuid.UUID | None:
    """The live run of the agent already diagnosing the red head *dedup_key* names, else ``None`` (#11809).

    Only a configured key naming ``owner/name@sha`` asks (``services/ci_red_claims.py``):
    a work item's CI-completion dispatch of a failure on that head, or an
    event run of the same key older than the window, is the one diagnosis.
    """
    from app.services import ci_red_claims

    head = ci_red_claims.red_head_of_key(dedup_key)
    if head is None:
        return None
    return await ci_red_claims.live_red_carrier(db, agent_id, head[0], head[1])


async def _skip_claimed_event_run(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    payload: dict | None,
    trigger_context: dict,
    *,
    holder: str,
    window_seconds: int,
    triggered_by_user_id: _uuid.UUID | None = None,
) -> str:
    """Record a run refused because *holder*, another pipeline of the agent, took the event."""
    from app.services import inbound_claims

    return await _skip_duplicate_event_run(
        db,
        assignment,
        agent,
        payload,
        trigger_context,
        original_run_id=holder,
        window_seconds=window_seconds,
        triggered_by_user_id=triggered_by_user_id,
        claimed_by=inbound_claims.describe_holder(holder),
    )


async def _publish_run_failed(run: AssignmentRun, error: str | None) -> None:
    """Raise the ``run.failed`` platform event for *run*; never raises.

    Only for a run that executed: a run refused before it started (the
    quota, the AI switch or licence, an assignment or agent gone or disabled,
    a quota store failing closed) publishes nothing, so an assignment watching
    ``run.failed`` is not handed one run per refusal.
    """
    await _publish_run_ended(run, "run.failed", reason="failed", error=error)


async def _publish_run_ended(run: AssignmentRun, name: str, *, reason: str, error: str | None = None) -> None:
    """Raise run event *name* (``run.failed``, ``run.completed``) for *run*; never raises.

    Every caller is a run's terminal ending, after its commit: the run's
    sandbox workspace is collected here too, in a task of its own.
    """
    _collect_run_workspace(run)
    await publish_run_event(run, name, reason=reason, error=error)


async def publish_run_event(run: Any, name: str, *, reason: str, error: str | None = None) -> None:
    """Publish run event *name* for *run*, scoped as every ``run.*`` event; never raises.

    *run* is an ``AssignmentRun`` or a snapshot carrying the same attributes
    (id, agent_id, assignment_id, parent_run_id, trigger_context, user_id,
    is_company_managed, stop_reason): a delegated child run ends outside the
    engine (``sub_agent.update_assignment_run``) and publishes through here.
    """
    from app.services import platform_events

    try:
        context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
        item_id = context.get("work_item_id")
        # The item's workspace and agents, the scope every other run.* event
        # carries: dispatch checks workspace visibility on them. A failed read
        # leaves the event unpublished (the except below), never published
        # without its workspace scope.
        workspace_ids: list[object] = []
        participant_agent_ids: list[object] = []
        personal_owner_ids: list[object] = []
        if item_id:
            from app.models.work_item import WorkItem

            async with async_session() as _db:
                held = (
                    await _db.execute(
                        select(
                            WorkItem.workspace_id,
                            WorkItem.created_by_agent_id,
                            WorkItem.owner_agent_id,
                            WorkItem.user_id,
                            WorkItem.is_company_managed,
                        ).where(WorkItem.id == _uuid.UUID(str(item_id)))
                    )
                ).one_or_none()
            if held is not None:
                workspace_ids = [held.workspace_id]
                participant_agent_ids = [held.created_by_agent_id, held.owner_agent_id]
                # A personal item: only its owner's assignments see the event.
                if not held.is_company_managed and held.workspace_id is None:
                    personal_owner_ids = [held.user_id]
        # A run not visible to everyone (a personal run, a hidden one) reaches only
        # its owner's assignments, as run_visible_filter scopes it everywhere else.
        if not run.is_company_managed and run.user_id is not None:
            personal_owner_ids = [*personal_owner_ids, run.user_id]
        parent_agent_id = None
        if run.parent_run_id is not None:
            # A delegated run: the agent of the run that delegated it is in scope.
            # A failed lookup narrows the scope, it never drops the event.
            try:
                async with async_session() as _db:
                    parent_agent_id = (
                        await _db.execute(
                            select(AssignmentRun.agent_id).where(AssignmentRun.id == run.parent_run_id)
                        )
                    ).scalar_one_or_none()
            except Exception as exc:
                logger.warning("%s: parent of run %s unreadable: %s", name, run.id, format_exception_message(exc))
        await platform_events.publish(
            name,
            platform_events.run_data(
                run_id=run.id,
                agent_id=run.agent_id,
                assignment_id=run.assignment_id,
                parent_run_id=run.parent_run_id,
                parent_agent_id=parent_agent_id,
                work_item_ids=[item_id] if item_id else [],
                reason=reason,
                error=error,
                # A run that completed partial (deadline, budget, iteration
                # cap) says so: a listener tells it from a finished task.
                stop_reason=getattr(run, "stop_reason", None) if reason == "completed" else None,
                participant_agent_ids=participant_agent_ids,
                workspace_ids=workspace_ids,
                personal_owner_ids=personal_owner_ids,
            ),
        )
    except Exception as exc:
        logger.warning(
            "%s platform event not published for run %s: %s",
            name,
            getattr(run, "id", None),
            format_exception_message(exc),
        )


def _collect_run_workspace(run: AssignmentRun) -> None:
    """Delete the sandbox workspace of *run*, which just ended; never raises.

    ``collect_runs`` reads the status again and removes nothing for a run
    that is not terminal; whatever it could not remove, the hourly sweep does.
    """
    try:
        from app.services.sandbox_workspace.gc import collect_runs_soon
        from app.services.sandbox_workspace.session import workspace_enabled

        if workspace_enabled():
            collect_runs_soon([getattr(run, "id", None)])
    except Exception as exc:
        logger.debug("Sandbox workspace of run %s not collected: %s", getattr(run, "id", None), format_exception_message(exc))


async def has_active_run(
    db: AsyncSession,
    assignment_id: _uuid.UUID,
    *,
    user_id: _uuid.UUID | None = None,
) -> bool:
    """Return True if the assignment has a run in an active (non-terminal) state.

    Active means ``pending``, ``delayed``, ``running``, or ``awaiting_human``
    (see ``ACTIVE_RUN_STATUSES``). Used as the shared overlap guard so an
    assignment cannot accumulate parallel runs racing the same tools and DB
    writes.

    *user_id* narrows the guard to that user's runs: a per-user assignment
    (``resolve_hide_runs``) runs once per user, so only the caller's own run in
    flight overlaps with the one they are starting.
    """
    conditions = [
        AssignmentRun.assignment_id == assignment_id,
        holds_assignment(AssignmentRun, ACTIVE_RUN_STATUSES),
    ]
    if user_id is not None:
        conditions.append(AssignmentRun.user_id == user_id)
    result = await db.execute(select(AssignmentRun.id).where(*conditions).limit(1))
    return result.scalar_one_or_none() is not None


async def count_active_runs(db: AsyncSession, assignment_id: _uuid.UUID) -> int:
    """How many runs of the assignment are active (``ACTIVE_RUN_STATUSES``).

    The overlap guard of an assignment allowed more than one run at once
    (a ``work_item`` inbox with ``max_concurrent_runs``).
    """
    result = await db.execute(
        select(func.count(AssignmentRun.id)).where(
            AssignmentRun.assignment_id == assignment_id,
            holds_assignment(AssignmentRun, ACTIVE_RUN_STATUSES),
        )
    )
    return int(result.scalar_one() or 0)


async def find_active_run(
    db: AsyncSession,
    assignment_id: _uuid.UUID,
    *,
    user_id: _uuid.UUID | None = None,
) -> AssignmentRun | None:
    """The newest run of the assignment in an active state, scoped like :func:`has_active_run`."""
    conditions = [
        AssignmentRun.assignment_id == assignment_id,
        holds_assignment(AssignmentRun, ACTIVE_RUN_STATUSES),
    ]
    if user_id is not None:
        conditions.append(AssignmentRun.user_id == user_id)
    result = await db.execute(
        select(AssignmentRun)
        .options(
            load_only(
                AssignmentRun.id,
                AssignmentRun.status,
                AssignmentRun.created_at,
                AssignmentRun.started_at,
            )
        )
        .where(*conditions)
        .order_by(AssignmentRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _format_assignment_timeout_error(
    timeout: float | None,
    elapsed_seconds: float,
    max_iterations: int,
) -> str:
    """Build the user-facing message for a TimeoutError around the agent loop.

    ``timeout`` is the *effective* value passed to ``asyncio.wait_for`` —
    ``None`` when the assignment-level timeout is disabled
    (``timeout_seconds <= 0``).  In that case ``wait_for`` can never fire, so
    the TimeoutError necessarily bubbled up from an inner operation (LLM call,
    tool execution, ...) and citing ``assignment.timeout_seconds`` (0/None)
    would be misleading.  The enabled case reports the effective
    timeout, which also covers the ``_UNSET_TIMEOUT_FALLBACK_SECONDS``
    fallback for unset rows.
    """
    if timeout is None:
        return (
            "Assignment run aborted by an inner timeout after "
            f"~{int(elapsed_seconds)}s (the assignment-level timeout is "
            f"disabled; max_iterations={max_iterations})"
        )
    return (
        f"Assignment timed out after {int(timeout)}s "
        f"(max_iterations={max_iterations})"
    )


def _loop_timeout_error(
    run_timeout: float | None,
    budget: float | None,
    elapsed_seconds: float,
    max_iterations: int,
    *,
    deadline_cut: bool,
) -> TimeoutError:
    """What a ``TimeoutError`` around the agent loop is recorded as.

    The deadline's (:class:`DeadlineCutTimeoutError`) when the ``wait_for``
    fired on a bound the deadline cut; the run's own
    (:class:`AssignmentTimeoutError`) when it fired on the run's budget; a
    plain ``TimeoutError`` for a timeout raised before the bound elapsed (a
    setup call), which did not stop the run on its timeout.
    """
    message = _format_assignment_timeout_error(
        run_timeout, elapsed_seconds, max_iterations
    )
    bound_fired = run_timeout is not None and elapsed_seconds >= run_timeout
    if deadline_cut and bound_fired:
        return DeadlineCutTimeoutError(
            f"{message}: what its deadline left of its {int(budget or 0)}s budget"
        )
    if bound_fired:
        return AssignmentTimeoutError(message)
    return TimeoutError(message)


def _sanitize_binding_label(value: str) -> str:
    """Collapse a binding-derived token to a single printable line.

    Binding keys (→ provider labels) come from user-editable JSON columns,
    so newlines / control characters must not be able to distort the notice
    or smuggle extra lines into the deliverable (same contract as
    ``format_mcp_unavailable_prompt`` for MCP server names).
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(cleaned.split())[:100]


def _format_missing_integration_notice(
    missing_bindings: list[dict[str, str]], output_format: str
) -> str:
    """Build a user-visible warning for unresolved integration bindings."""
    if not missing_bindings:
        return ""

    seen: set[tuple[str, str]] = set()
    labels: list[str] = []
    for entry in missing_bindings:
        provider = (
            _sanitize_binding_label((entry.get("provider") or "").strip())
            or "unknown"
        )
        integration_id = _sanitize_binding_label(
            (entry.get("integration_id") or "").strip()
        )
        key = (provider, integration_id)
        if key in seen:
            continue
        seen.add(key)
        labels.append(
            f"{provider} ({integration_id})" if integration_id else provider
        )

    if not labels:
        return ""

    shown = labels[:3]
    extra_count = len(labels) - len(shown)
    if extra_count > 0:
        shown.append(f"+{extra_count} more")
    joined = ", ".join(shown)
    heading = "Tool Availability Notice"
    body = (
        "Some configured integration bindings could not be resolved and were "
        f"skipped for this run: {joined}."
    )
    if output_format == "html":
        # Provider labels are user-controlled; escape so an html-format
        # agent's result_summary cannot carry injected markup.
        return f"<p><strong>{heading}:</strong> {html.escape(body)}</p>"
    return f"⚠️ {heading}: {body}"


def _format_missing_integration_prompt_note(
    missing_bindings: list[dict[str, str]],
) -> str:
    """Build the system-prompt Tool Availability note for unresolved bindings.

    Mirrors ``format_mcp_unavailable_prompt`` (loaders.py): injected ONLY on
    runs where bindings actually failed to resolve — net-zero prompt inflation
    on healthy runs — so the LLM acknowledges the missing tools instead of
    hallucinating integration results.  Names are json.dumps-escaped for the
    same reason as MCP server names (user-controlled values must not smuggle
    extra prompt lines).
    """
    from app.services.integration_credentials import (
        OWNER_LOST_ACCESS_REASON,
        format_orphaned_binding_prompt_note,
    )

    providers = sorted(
        {
            _sanitize_binding_label(
                (entry.get("provider") or "").strip()
            )
            or "unknown"
            for entry in missing_bindings
            if isinstance(entry, dict) and entry.get("reason") != OWNER_LOST_ACCESS_REASON
        }
    )
    orphaned = format_orphaned_binding_prompt_note(missing_bindings)
    if not providers:
        return orphaned
    names = ", ".join(json.dumps(p) for p in providers)
    return (
        f"\n\n**Tool Availability Notice**: The following integration "
        f"binding(s) could not be resolved and their tools are unavailable "
        f"for this run: {names}. If the task requires those tools, "
        f"acknowledge this limitation clearly in your response."
    ) + orphaned


# Minimum seconds guaranteed per iteration when auto-flooring the effective
# timeout (mirrors ``chat_loop_timeout_seconds`` / 120s/iter
# for interactive sessions; assignments use a more conservative 20s/iter since
# they run in the background and many iterations are fast tool dispatches).
# Applied via _apply_iteration_timeout_floor() only when the operator's
# configured timeout_seconds would otherwise be less than this floor —
# it never reduces an explicitly generous timeout.
_MIN_SECS_PER_ITERATION: int = 20
# Global floor for assignment wall-clock budgets. Low-iteration assignments
# (e.g. max_iterations=20) previously auto-floored to 400s (20×20s), which
# proved too tight for periodic repo-wide review jobs in production.
_MIN_ASSIGNMENT_TIMEOUT_SECONDS: int = 600

# Fallback when ``assignment.timeout_seconds`` is ``None`` (only possible on
# an unflushed in-memory row — the column is non-nullable with default=300).
# ONE constant shared by the agentic loop's ``asyncio.wait_for`` bound and
# both orchestration ``deadline_at`` sites (trigger + retry): the loop used
# to fall back to 900 s while the deadlines fell back to 300 s, so a None
# row could be dead-lettered by the reconciler at ~300 s + grace while the
# inner loop still believed it had 900 s — the exact bound drift this module
# is meant to eliminate. Matches the model's column default.
_UNSET_TIMEOUT_FALLBACK_SECONDS: int = 300


def max_queue_wait_seconds(assignment: Assignment) -> float | None:
    """How long a run of *assignment* may wait for a free worker before it fails unstarted.

    ``trigger_config.max_queue_wait_seconds`` when the assignment sets a number (0 or less: no
    queue bound); ``None`` otherwise, which means the platform's
    ``orchestration_max_queue_wait_seconds``. Distinct from ``timeout_seconds``, which is the
    run's execution time and counts from its start.
    """
    value = (getattr(assignment, "trigger_config", None) or {}).get("max_queue_wait_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _apply_iteration_timeout_floor(
    timeout: float | None,
    effective_max_iterations: int,
    run_id: str,
    stored_timeout_seconds: int | None,
) -> float | None:
    """Return the effective wall-clock timeout for the agentic loop.

    When ``timeout`` is ``None`` (assignment-level timeout disabled) it is
    returned unchanged.  Otherwise the returned value is:

        max(
            timeout,
            _MIN_ASSIGNMENT_TIMEOUT_SECONDS,
            effective_max_iterations × _MIN_SECS_PER_ITERATION,
        )

    This prevents the wall-clock budget from expiring before the iteration
    budget is exhausted when an operator sets a high ``max_iterations`` (e.g.
    50) but leaves ``timeout_seconds`` at its 300 s default — a pattern that
    caused repeated premature timeouts across different assignments.  The chat loop applies an identical principle via
    ``max(CHAT_MIN_LOOP_TIMEOUT_SECONDS, effective_max_iterations * 120)`` via
    ``chat_loop_timeout_seconds`` in ``general_assistant_config.py``;
    assignments
    use a lower per-iteration floor (20 s vs 120 s) because they run
    autonomously without an interactive user waiting. A static 600 s minimum
    also applies so low-iteration weekly review jobs do not keep timing out at
    400 s (20 × 20 s) before they can finish.

    An INFO-level log is emitted when the floor raises the stored value so the
    auto-adjustment is visible in observability tooling (Loki / Grafana).
    """
    if timeout is None:
        return None
    # Callers pass ints (e.g. ``_ts or _UNSET_TIMEOUT_FALLBACK_SECONDS``) —
    # coerce so the enabled case always returns a float, matching the
    # annotation.
    timeout = float(timeout)
    iteration_floor = effective_max_iterations * _MIN_SECS_PER_ITERATION
    floor = float(max(_MIN_ASSIGNMENT_TIMEOUT_SECONDS, iteration_floor))
    if timeout < floor:
        logger.info(
            "Assignment run %s: effective timeout raised from %ds to %ds "
            "(floor=max(%ds static, max_iterations=%d × %ds/iter=%ds); "
            "stored timeout_seconds=%s) — "
            "set timeout_seconds>=%d explicitly to suppress this adjustment",
            run_id,
            int(timeout),
            int(floor),
            _MIN_ASSIGNMENT_TIMEOUT_SECONDS,
            effective_max_iterations,
            _MIN_SECS_PER_ITERATION,
            iteration_floor,
            stored_timeout_seconds,
            int(floor),
        )
        return floor
    return timeout


def describe_effective_timeout(
    stored_timeout_seconds: int | None,
    effective_max_iterations: int,
    grace_seconds: float,
) -> str:
    """The run budget an assignment's runs get, and why, in one line (#1695).

    The same arithmetic as :func:`_apply_iteration_timeout_floor` (without its
    log line): the stored ``timeout_seconds`` raised to the 600 s static floor
    or to ``max_iterations`` x 20 s, plus the orchestration grace, counted from
    the run's start (queue time is not charged).
    """
    stored = _UNSET_TIMEOUT_FALLBACK_SECONDS if stored_timeout_seconds is None else stored_timeout_seconds
    if stored <= 0:
        return "no wall-clock bound (timeout disabled)"
    iteration_floor = effective_max_iterations * _MIN_SECS_PER_ITERATION
    effective = max(stored, _MIN_ASSIGNMENT_TIMEOUT_SECONDS, iteration_floor)
    if effective == stored:
        why = "the stored value, above the floor"
    elif effective == iteration_floor:
        why = f"raised to the iteration floor, max_iterations {effective_max_iterations} x {_MIN_SECS_PER_ITERATION}s"
    else:
        why = f"raised to the {_MIN_ASSIGNMENT_TIMEOUT_SECONDS}s static floor"
    return (
        f"{int(effective)}s ({why}) plus {int(grace_seconds)}s grace, counted from "
        f"the run's start; a run stopped by it ends failed with stop_reason timeout"
    )


def resolve_effective_max_iterations(
    assignment_max_iterations: int | None,
    agent_max_iterations: int | None,
    platform_default: int,
) -> int:
    """Resolve the agentic-loop cap for an assignment run.

    Precedence (most specific scope wins): the assignment's own override, then
    the agent's override, then the platform default
    (``GeneralAssistantConfig.max_iterations``). A set assignment value
    therefore OVERRIDES the agent — the assignment is the most specific scope,
    mirroring how the assignment refines the agent's tool / KB scope. ``None``
    at a level means "inherit from the next-broader scope".

    The resolved value is clamped to ``MAX_ITERATIONS_CEILING`` so the agentic
    loop always honours the safety ceiling at runtime, even for legacy rows /
    direct DB edits that stored an out-of-range value before the save-path
    clamp existed. Only the transient runtime cap is clamped — the stored
    value is left untouched (it is corrected on its next save), so the
    "existing installs keep their configured value" contract still holds.
    """
    from app.models.general_assistant_config import clamp_max_iterations

    if assignment_max_iterations is not None:
        resolved = assignment_max_iterations
    elif agent_max_iterations is not None:
        resolved = agent_max_iterations
    else:
        resolved = platform_default
    return clamp_max_iterations(resolved)


# High-iteration gate for the per-tool budget cap: runs at or below this many
# iterations keep the agent-loop default timeout untouched. A run at the
# platform default max_iterations is above it, yet never capped tighter than
# the loop default: from 75 iterations the iteration floor (x 20 s) leaves a
# fifth of at least 1500 s to one tool call. A tighter cap on every standard
# run was evaluated and rejected (the diagnosed root cause there
# was system-prompt bloat from unbounded agent memory, not hanging tools).
_TOOL_CAP_MIN_ITERATIONS = 30

# Floor for the derived per-tool timeout so data-heavy tools (large KB
# searches, MCP calls, code execution) keep enough room to finish even under
# very tight run budgets.
_TOOL_CAP_FLOOR_SECONDS = 60

# One tool call may take at most 1 / N of the run's wall-clock budget: the
# smallest budget a capped run can have (31 iterations x 20 s = 620 s) gives
# 124 s, and from 1500 s on it gets the full loop default.
_TOOL_CAP_BUDGET_DIVISOR = 5

# ---------------------------------------------------------------------------
# Cross-run integration auth-failure quarantine
#
# Scheduled assignments whose integration tool calls consistently return
# authentication / permission failures (e.g. an expired Notion OAuth token)
# keep burning cycles indefinitely until a human notices.  To avoid that, we
# track consecutive auth-failure RUNS in Redis.  When the counter reaches
# ``settings.assignment_auth_fail_quarantine_threshold``:
#   1. The assignment is auto-disabled (``enabled = False``).
#   2. A durable Activity notification is persisted for the owner.
#   3. An SSE notification is published — scoped like every other assignment
#      notification (``_publish_run_status`` / ``notify._notify_web``):
#      company-managed (or owner-less) assignments broadcast to the shared
#      ``activity:company`` channel, personal assignments publish only to
#      the owner's channel so private assignment/agent names never leak.
#
# A SCHEDULER run (``schedule`` / ``schedule_fanout`` trigger) that contains
# *at least one* integration tool result classified as an auth/permission
# error increments the counter — manual retries / webhook bursts never
# advance it, so a user diagnosing a broken credential cannot quarantine
# their own assignment by re-running it.  A run with no auth failures (ANY
# trigger type) resets the counter so a rotated credential clears the state
# on the very next run.  All Redis operations fail-open — a Redis outage
# degrades to no tracking, never to a blocked assignment.
# ---------------------------------------------------------------------------
_ASSIGNMENT_AUTH_FAIL_KEY_PREFIX = "assignment:auth_failures:"
# Consecutive auth-failure runs of the report-only paths - see
# ``_record_assignment_auth_report_failure``.
_ASSIGNMENT_AUTH_REPORT_FAIL_KEY_PREFIX = "assignment:auth_report_failures:"
_ASSIGNMENT_AUTH_FAIL_TTL_SECS = 7 * 24 * 3600  # 7-day auto-expiry
# Assignments that are exempt from the quarantine are NOT exempt from being
# reported: they still tell their owner the credential is broken, once
# per window rather than once per run.  A webhook-driven assignment is the
# reason this exists — a SOAR playbook can fire it hundreds of times a day, and
# an alert storm on a dead credential must not become an alert storm of
# notifications.  One day is deliberately coarse: the message is "this is still
# broken", and nothing about it becomes more actionable by repeating it sooner.
# Keyed by notice KIND as well as assignment: the report-only notice ("still
# broken, still running") and the quarantine notice ("this has been paused")
# are different events, and one must never swallow the other — an assignment
# triggered both manually and on a schedule would otherwise announce the manual
# failure and then pause silently.
_ASSIGNMENT_AUTH_NOTICE_KEY_PREFIX = "assignment:auth_notice:"
_ASSIGNMENT_AUTH_NOTICE_KINDS = ("report", "quarantine")
_ASSIGNMENT_AUTH_NOTICE_COOLDOWN_SECS = 24 * 3600


#: ``paused_by_label`` of a pause the engine decided on its own.
_ENGINE_PAUSE_ACTOR_LABEL = "XTM One — assignment engine"


def _auth_notice_key(assignment_id: str, kind: str) -> str:
    return f"{_ASSIGNMENT_AUTH_NOTICE_KEY_PREFIX}{kind}:{assignment_id}"
# Trigger types whose auth-failure runs advance the quarantine counter.
# Only autonomous scheduler runs count (the failure mode this protects
# against is unattended scheduled assignments burning cycles); clean runs
# of ANY trigger type still reset the counter.
_QUARANTINE_TRIGGER_TYPES = frozenset({"schedule", "schedule_fanout"})
# Lua script: atomic INCR + conditional EXPIRE (set TTL on first write or when
# the key survived a crash without an expiry).  Identical logic to
# ``_MCP_FAILURE_INCR_LUA`` in ``mcp_client.py`` — shared so they're easy to
# audit together.
_ASSIGNMENT_AUTH_FAIL_INCR_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
else
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
end
return current
"""
# The report-only streak refreshes its expiry on every failing run: it is held
# only while the runs of its identity come less than a notice window apart
# (``_identity_ran_recently``), and a fixed expiry from the first failure would
# drop a streak that a high ``assignment_auth_fail_quarantine_threshold`` needs
# more than the TTL to complete.
_ASSIGNMENT_AUTH_REPORT_INCR_LUA = """
local current = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return current
"""


def _auth_report_key(assignment_id: str, user_id: object | None = None) -> str:
    """The report-only streak of one execution identity.

    A per-user fan-out passes the run's user: each user's run uses that user's
    integrations, so another user's clean run is not the end of this user's
    streak, and several users failing once each are not N failures of one
    credential.
    """
    key = f"{_ASSIGNMENT_AUTH_REPORT_FAIL_KEY_PREFIX}{assignment_id}"
    return f"{key}:{user_id}" if user_id is not None else key


def _is_per_user_fanout(assignment: Assignment) -> bool:
    return bool(assignment.run_as_all_users and assignment.is_company_managed)


async def _record_assignment_auth_failure(assignment_id: str) -> int:
    """Increment the consecutive auth-failure run counter for *assignment_id*.

    Returns the new consecutive count, or ``0`` on Redis unavailability
    (fail-open — a Redis outage degrades to no tracking, never blocks a run).
    """
    return await _incr_auth_failure_counter(
        f"{_ASSIGNMENT_AUTH_FAIL_KEY_PREFIX}{assignment_id}", assignment_id
    )


async def _record_assignment_auth_report_failure(
    assignment_id: str, user_id: object | None = None
) -> int:
    """Increment the consecutive auth-failure count of REPORT-ONLY runs.

    Webhook, manual and quarantine-exempt runs are never paused, but their
    notice waits for the same N-consecutive evidence the pause does: one
    failing run on a missing capability is not a dead credential. Deliberately
    a counter of its own - the pause counter only ever counts scheduled runs,
    so a user re-running an assignment by hand while diagnosing it can never
    bring its next scheduled failure closer to a pause.
    """
    return await _incr_auth_failure_counter(
        _auth_report_key(assignment_id, user_id),
        assignment_id,
        script=_ASSIGNMENT_AUTH_REPORT_INCR_LUA,
    )


async def _incr_auth_failure_counter(
    redis_key: str, assignment_id: str, *, script: str = _ASSIGNMENT_AUTH_FAIL_INCR_LUA
) -> int:
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        count = await redis.eval(script, 1, redis_key, _ASSIGNMENT_AUTH_FAIL_TTL_SECS)
        return int(count)
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot track auth-failure count for assignment '%s': %s",
            assignment_id,
            format_exception_message(exc),
        )
        return 0


async def _reset_assignment_auth_failures(
    assignment_id: str, report_user_id: object | None = None
) -> None:
    """Reset the consecutive auth-failure counter after a clean run.

    Called when the run completed without any auth-error tool results, so a
    rotated / re-authorized credential clears the quarantine trigger immediately.
    Fail-open: a Redis outage at most leaves a stale counter that auto-expires
    after ``_ASSIGNMENT_AUTH_FAIL_TTL_SECS``.
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        # The notice claims go with the counter: a recovered credential ENDS the
        # episode, and the rule is one alert per episode — not one per day.
        # Leaving them behind means a credential that breaks again an hour later
        # (OAuth refresh flapping is the common shape) is never reported, which
        # is the same silence this feature exists to kill.
        await redis.delete(
            f"{_ASSIGNMENT_AUTH_FAIL_KEY_PREFIX}{assignment_id}",
            _auth_report_key(assignment_id, report_user_id),
            *(
                _auth_notice_key(assignment_id, kind)
                for kind in _ASSIGNMENT_AUTH_NOTICE_KINDS
            ),
        )
    except Exception as exc:
        logger.debug(
            "Redis unavailable — cannot reset auth-failure count for assignment '%s': %s",
            assignment_id,
            format_exception_message(exc),
        )


async def _release_auth_failure_notice(assignment_id: str, kind: str) -> None:
    """Give the claim back so the next run can retry an undelivered notice."""
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        await redis.delete(_auth_notice_key(assignment_id, kind))
    except Exception as exc:
        logger.debug(
            "Redis unavailable — could not release the auth-failure notice "
            "claim for assignment '%s': %s",
            assignment_id,
            format_exception_message(exc),
        )


async def _claim_auth_failure_notice(assignment_id: str, kind: str) -> bool | None:
    """Claim the right to emit one *kind* of auth-failure notice.

    ``SET NX EX`` — True for the first caller in the window, False afterwards,
    ``None`` when Redis could not be asked.

    The two outcomes are kept apart because the callers need opposite
    failure directions.  The report-only path drops the notice on ``None``:
    an un-deduplicated notice would put one notification per webhook delivery
    in front of the owner during exactly the incident it reports, and the
    assignment keeps running, so the next failing run tries again.  The
    quarantine path notifies anyway: it has just paused the assignment, there
    is no next run to retry on, and a pause read twice beats one never read.
    """
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        claimed = await redis.set(
            _auth_notice_key(assignment_id, kind),
            "1",
            nx=True,
            ex=_ASSIGNMENT_AUTH_NOTICE_COOLDOWN_SECS,
        )
        return bool(claimed)
    except Exception as exc:
        logger.warning(
            "Redis unavailable - cannot deduplicate the %s auth-failure notice "
            "for assignment '%s': %s",
            kind,
            assignment_id,
            format_exception_message(exc),
        )
        return None


# A 401 where it reads as the call's own HTTP status, after a status word or
# before its reason phrase ("Request failed with status code 401", "Jira API
# returned 401 Unauthorized for GET ..."). Not the parenthesised form: on an
# unwrapped result, "(401)" is as often an echoed title ("Login failed (401)
# for SSO users").
_HTTP_401_STATUS_RE = re.compile(
    r"\b(?:https?(?:/\d(?:\.\d)?)?|status(?:[ _-]?code)?|(?:error|response|return)[ _-]?code)"
    r"[\"']?\s*[:=]?\s*[\"']?401(?!\d)"
    r"|(?<![\w#/.-])401\s+(?:unauthori[sz]ed|client error)"
)
_AUTH_STATUS_LINE_MAX_CHARS = 300
_JSON_ERROR_BODY_MAX_CHARS = 4000
_JSON_ERROR_STATUS_KEYS = ("status", "statusCode", "status_code", "status-code", "code", "httpStatus")


def _is_json_error_body(stripped: str) -> bool:
    """A small JSON object that reports a failure at its top level: an
    ``error`` / ``errors`` member (Google, GraphQL), ``"object": "error"``,
    ``"ok": false`` or a 401 status field (Notion, Jira)."""
    if len(stripped) > _JSON_ERROR_BODY_MAX_CHARS:
        return False
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get("error")
        or payload.get("errors")
        or payload.get("object") == "error"
        or payload.get("ok") is False
        or any(str(payload.get(key)) == "401" for key in _JSON_ERROR_STATUS_KEYS)
    )


def _is_auth_failure_message(content: str) -> bool:
    """Whether one rendered tool result IS an auth failure.

    :func:`app.tools.base.is_auth_failure_result` decides for the
    shapes it proves, and a SUCCESSFUL result that merely mentions
    "unauthorized" or "#401" (a threat report, an issue list) does not count.
    It is built for the ``is_error`` stamp, where a false positive is the
    costly direction, so two failure shapes it leaves out still count here, as
    they do for the raw classifier: an MCP server's error result reaches the
    transcript as the server wrote it (the MCP client neither wraps nor flags
    it), so a one-line HTTP status message and a JSON error body are what a
    dead credential looks like there.
    """
    from app.tools.base import is_auth_failure_result, is_auth_or_permission_error

    if is_auth_failure_result(content):
        return True
    if not is_auth_or_permission_error(content):
        return False
    stripped = content.strip()
    if stripped.startswith("{"):
        return _is_json_error_body(stripped)
    return (
        len(stripped) <= _AUTH_STATUS_LINE_MAX_CHARS
        and "\n" not in stripped
        and bool(_HTTP_401_STATUS_RE.search(stripped.lower()))
    )


def _auth_failure_contents(messages: list) -> list[str]:
    """The tool-result messages of *messages* that ARE an auth failure
    (:func:`_is_auth_failure_message`): the messages carry no error flag, only
    the rendered text, and three successes in a row that merely mention one
    would pause a healthy scheduled digest.

    Only ``role == "tool"`` messages are checked: assistant messages and system
    messages are never auth-failure responses.
    """
    found: list[str] = []
    for msg in messages:
        if getattr(msg, "role", None) == "tool":
            content = getattr(msg, "content", None) or ""
            if _is_auth_failure_message(content):
                found.append(content)
    return found


def _messages_contain_auth_failure(messages: list) -> bool:
    """Return True if any tool-result message in *messages* is an auth failure."""
    return bool(_auth_failure_contents(messages))


# A rejected credential, as opposed to a credential that works but lacks a
# permission / capability. Re-authorizing fixes the first and cannot
# fix the second - an OpenCTI ``FORBIDDEN_ACCESS`` needs a capability granted
# to the account, so telling its owner to re-authorize sends them to the wrong
# screen.
_CREDENTIAL_FAILURE_RE = re.compile(
    r"\b401\b|unauthori[sz]ed|bad credentials|invalid (?:token|credential)"
    r"|token (?:may be |is )?(?:expired|revoked)|requires authentication"
    r"|authentication (?:failed|required)|must be logged in|re-authori[sz]e",
    re.IGNORECASE,
)


def _auth_failure_kind(contents: list[str]) -> str:
    """``"credential"`` when every failure rejects the credential,
    ``"permission"`` when none does, and ``"mixed"`` when a run has both - its
    notice then names both remedies, since re-authorizing alone leaves the
    missing permission in place."""
    rejected = [bool(_CREDENTIAL_FAILURE_RE.search(content)) for content in contents]
    if rejected and all(rejected):
        return "credential"
    if any(rejected):
        return "mixed"
    return "permission"


def _auth_failure_remedy(kind: str) -> str:
    """The sentence a notice uses to say what to do, per failure kind."""
    if kind == "credential":
        return (
            "The connected integration's OAuth token or API key may be "
            "expired or revoked - re-authorize the integration to fix it."
        )
    if kind == "mixed":
        return (
            "Some calls were rejected because the connected integration's "
            "OAuth token or API key may be expired or revoked, and others "
            "because its account was refused a permission or capability the "
            "assignment needs - re-authorize the integration AND grant the "
            "missing permission to that account (or narrow what the assignment "
            "asks for)."
        )
    return (
        "The connected integration's account was refused a permission or "
        "capability the assignment needs - grant it to that account (or "
        "narrow what the assignment asks for). Re-authorizing alone will not "
        "fix it."
    )


async def _emit_assignment_notice(
    db: AsyncSession,
    *,
    assignment: Assignment,
    agent: Agent,
    activity_type: str,
    title: str,
    summary: str,
    extra_data: dict,
    company_recipient_id: _uuid.UUID | str | None = None,
    only_to: _uuid.UUID | str | None = None,
) -> bool:
    """Persist the durable Activity and publish the SSE toast. Best-effort.

    Shared by the auth quarantine, the auth report-only path and the
    stale-reference path so they can never drift on the thing that
    matters most here: WHO is allowed to see a private assignment's name.

    Returns whether ANYTHING landed — a stored Activity or a published SSE
    event.  Both halves swallow their own failure, so without this the caller
    cannot tell a delivered notice from one that vanished into two DEBUG lines,
    and would keep a dedup claim it never earned.  An owner whose user row is
    gone is the realistic way to reach ``False``: the insert fails on the
    foreign key and the SSE goes to a channel nobody is subscribed to.

    That case is NOT rescued by falling back to the company channel: an
    owner-less broadcast is exactly the leak the scoping below prevents, and a
    private assignment's name must not reach every connected user because its
    owner left.  It is logged at WARNING instead, and the caller retries.

    *company_recipient_id* names who gets the notice for a COMPANY-MANAGED
    assignment, which has no owner: the identity its runs execute as, the
    fallback the KB sync alerts use for a company-managed source (its
    configuring user). The notice is then theirs alone, live event included:
    it can name that user's own integration (a company-managed run resolves
    its run user's integrations), which the company channel must not carry.
    Ignored for a personal assignment.

    *only_to* makes the notice that user's alone whatever the assignment is:
    stored for them and published to their channel only, never the company
    one - for a notice about something not every reader of the assignment may
    know of (a refused continuation of a private run).
    """
    import json as _json
    import uuid as _uuid_mod

    owner_user_id: str | None = None
    recipient_only = False
    if only_to is not None:
        owner_user_id = str(only_to)
        recipient_only = True
    elif assignment.user_id is not None:
        owner_user_id = str(assignment.user_id)
    elif assignment.is_company_managed and company_recipient_id is not None:
        owner_user_id = str(company_recipient_id)
        recipient_only = True
    activity_stored = False
    sse_published = False

    # Persist a durable Activity notification for the owner.
    if owner_user_id:
        try:
            from app.models.activity import Activity

            # Flush the caller's pending state (e.g. ``enabled = False``)
            # into the outer transaction FIRST, then wrap the Activity
            # insert — including its own flush — in a SAVEPOINT so a
            # constraint error rolls back only this row instead of
            # surfacing later during the caller's commit (which would
            # undo the quarantine and violate the best-effort contract).
            # Mirrors ``kb_sync_scheduler._emit_sync_notification``.
            await db.flush()
            async with db.begin_nested():
                notice = Activity(
                    user_id=_uuid_mod.UUID(owner_user_id),
                    activity_type=activity_type,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    title=title,
                    summary=summary,
                    extra_data=extra_data,
                )
                db.add(notice)
                await db.flush()
            activity_stored = True
            # To the owner's linked phones too, once the caller commits.
            from app.services.mobile_push import queue_push

            queue_push(db, user_id=notice.user_id, ref_kind="activity", ref_id=notice.id)
        except Exception:
            logger.warning(
                "Could not persist %s Activity for assignment '%s'",
                activity_type,
                assignment.name,
                exc_info=True,
            )

    # SSE notification — scoped exactly like ``_publish_run_status`` /
    # ``notify._notify_web``: company-managed assignments broadcast to the
    # shared ``activity:company`` channel (every user may act on them) unless
    # the notice has a recipient (see the docstring); personal assignments
    # publish ONLY to the owner's channel so private assignment/agent names
    # never leak to unrelated users.  An owner-less personal assignment
    # (shouldn't normally happen) falls back to the company channel rather
    # than dropping the alert.
    try:
        from app.core.redis import redis_client as _redis_client

        redis = await _redis_client()
        payload = _json.dumps(
            {
                "type": "notification",
                "agent_name": agent.name,
                "assignment_name": assignment.name,
                "summary": summary,
            }
        )
        if not recipient_only and (assignment.is_company_managed or not owner_user_id):
            await redis.publish("activity:company", payload)
        if owner_user_id:
            await redis.publish(f"activity:{owner_user_id}", payload)
        sse_published = True
    except Exception:
        logger.warning(
            "SSE broadcast failed for %s notification of assignment '%s'",
            activity_type,
            assignment.name,
            exc_info=True,
        )
    return activity_stored or sse_published


async def _handle_assignment_auth_failure(
    db: AsyncSession,
    *,
    assignment: Assignment,
    agent: Agent,
    run_id: str,
    consecutive: int,
    kind: str = "credential",
) -> None:
    """Auto-disable the assignment and notify the owner after crossing the threshold.

    Called when the consecutive auth-failure count for an assignment reaches
    ``settings.assignment_auth_fail_quarantine_threshold``.  Strictly
    best-effort: any failure inside is caught so the quarantine action (the
    ``enabled = False`` write committed by the caller) can never be rolled back
    by a notification error.
    """
    claimed = await _claim_auth_failure_notice(str(assignment.id), "quarantine")
    if claimed is False:
        # Two workers can cross the threshold on the same assignment before
        # either commits ``enabled = False`` — the overlap guard only covers
        # ``skip_if_active`` triggers — and the owner should not read the same
        # pause twice.
        return
    if claimed is None:
        # Redis could not deduplicate. The caller pauses the
        # assignment whatever happens here and there is no later run to retry
        # the notice on, so a possible duplicate beats a silent pause.
        logger.warning(
            "Sending the pause notice for assignment '%s' without "
            "deduplication - Redis is unavailable",
            assignment.name,
        )
    threshold = settings.assignment_auth_fail_quarantine_threshold
    summary = (
        f"Assignment '{assignment.name}' has been automatically paused after "
        f"{consecutive} consecutive run(s) with integration authentication "
        f"failures. {_auth_failure_remedy(kind)} Then re-enable the assignment "
        f"to resume."
    )
    delivered = await _emit_assignment_notice(
        db,
        assignment=assignment,
        agent=agent,
        activity_type="assignment_auth_quarantine",
        title=(
            f"Assignment '{assignment.name}' paused — "
            "integration authentication failed"
        ),
        summary=summary,
        extra_data={
            "assignment_id": str(assignment.id),
            "run_id": run_id,
            "consecutive_auth_failures": consecutive,
            "threshold": threshold,
            "auth_failure_kind": kind,
        },
    )
    if not delivered and claimed:
        logger.warning(
            "Quarantine notice for assignment '%s' could not be delivered — "
            "releasing the claim so the next run retries",
            assignment.name,
        )
        await _release_auth_failure_notice(str(assignment.id), "quarantine")


async def _report_unquarantined_auth_failure(
    db: AsyncSession,
    *,
    assignment: Assignment,
    agent: Agent,
    run_id: str,
    reason: str,
    consecutive: int = 1,
    kind: str = "credential",
    recipient_id: _uuid.UUID | str | None = None,
) -> None:
    """Report an auth failure on an assignment the quarantine will never pause.

    *recipient_id* is the run's user: a company-managed assignment has no
    owner, and without it the notice was a live toast only, stored for nobody
    (#1454). It is passed as ``company_recipient_id``, so a personal
    assignment still notifies its owner.

    The quarantine is deliberately narrow — it only pauses unattended scheduler
    runs, and never touches built-in or per-user fan-out assignments.  Those
    exemptions do not silence the *reporting*, which is a different
    decision: silenced, a webhook-driven assignment whose integration
    credential is dead keeps accepting deliveries, keeps producing runs with
    no usable result, and tells nobody.

    Not pausing stays the right call for these; saying nothing does not.  The
    notice is rate-limited to one per assignment per
    ``_ASSIGNMENT_AUTH_NOTICE_COOLDOWN_SECS``, so a SOAR firing an assignment
    hundreds of times against a dead credential still produces one notification
    a day.  Best-effort throughout — this must never affect the run's outcome.

    Only called once ``consecutive`` report-only runs failed, and a
    claim Redis could not check (``None``) drops the notice: the assignment
    keeps running, so the next failing run tries again.
    """
    if not await _claim_auth_failure_notice(str(assignment.id), "report"):
        return
    summary = (
        f"Assignment '{assignment.name}' ran {consecutive} time(s) in a row "
        f"with integration authentication failures, so its results are "
        f"incomplete or empty. It has NOT been paused ({reason}) and will keep "
        f"running. {_auth_failure_remedy(kind)} This notice repeats at most "
        f"once a day while the failures continue."
    )
    delivered = await _emit_assignment_notice(
        db,
        assignment=assignment,
        agent=agent,
        activity_type="assignment_auth_failure",
        title=(
            f"Assignment '{assignment.name}' — integration authentication "
            "is failing"
        ),
        summary=summary,
        extra_data={
            "assignment_id": str(assignment.id),
            "run_id": run_id,
            "quarantined": False,
            "not_quarantined_reason": reason,
            "consecutive_auth_failures": consecutive,
            "auth_failure_kind": kind,
        },
        company_recipient_id=recipient_id,
    )
    if not delivered:
        # The claim was taken before the notice was known to land. Keeping it
        # would trade one failed delivery for a day of silence — the outcome
        # this whole path exists to prevent — so give it back and let the next
        # failing run try again.
        logger.warning(
            "Auth-failure notice for assignment '%s' could not be delivered — "
            "releasing the cooldown so the next run retries",
            assignment.name,
        )
        await _release_auth_failure_notice(str(assignment.id), "report")


async def _identity_ran_recently(db: AsyncSession, run: AssignmentRun, *, per_user: bool) -> bool:
    """Whether the identity of *run* ran the assignment within the notice
    window before it - that is, whether more evidence is coming soon.

    Best-effort in a SAVEPOINT: a failed read aborts the PostgreSQL
    transaction the run is committed in next. On any doubt, False.
    """
    created_at = getattr(run, "created_at", None)
    if not isinstance(created_at, datetime):
        return False
    query = select(AssignmentRun.id).where(
        AssignmentRun.assignment_id == run.assignment_id,
        AssignmentRun.id != run.id,
        AssignmentRun.created_at < created_at,
        AssignmentRun.created_at
        >= created_at - timedelta(seconds=_ASSIGNMENT_AUTH_NOTICE_COOLDOWN_SECS),
    )
    if per_user:
        query = query.where(AssignmentRun.user_id == run.user_id)
    try:
        async with db.begin_nested():
            return (await db.execute(query.limit(1))).first() is not None
    except Exception:
        logger.debug(
            "Could not read the recent runs of assignment run %s", run.id, exc_info=True
        )
        return False


async def _report_after_consecutive_failures(
    db: AsyncSession,
    *,
    run: AssignmentRun,
    assignment: Assignment,
    agent: Agent,
    run_id: str,
    reason: str,
    kind: str,
    threshold: int,
) -> None:
    """Report-only path: notify once the SAME evidence the pause needs is in.

    The notice used to fire on the first failing run, so a single
    webhook run against a tool missing one capability told its owner to
    re-authorize a credential that works. It now waits for ``threshold``
    consecutive failing runs of the same identity; a clean run in between
    resets it (``_reset_assignment_auth_failures``), and a Redis outage
    (count 0) drops it like the notice claim does.

    Only while that evidence can come within the notice window: an identity
    that has not run in the day before this failure (a weekly or monthly
    schedule, a rare webhook) is reported at its first failure, as it always
    was - waiting would delay the notice by weeks, or forever for a cadence
    slower than the counter's expiry.
    """
    per_user = _is_per_user_fanout(assignment)
    consecutive = await _record_assignment_auth_report_failure(
        str(assignment.id), run.user_id if per_user else None
    )
    if consecutive == 0:
        return
    if consecutive < threshold and await _identity_ran_recently(db, run, per_user=per_user):
        return
    await _report_unquarantined_auth_failure(
        db,
        assignment=assignment,
        agent=agent,
        run_id=run_id,
        reason=reason,
        consecutive=consecutive,
        kind=kind,
        recipient_id=run.user_id,
    )


def _quarantine_exempt_reason(assignment: Assignment, agent: Agent) -> str | None:
    """Return why *assignment* is exempt from auth-failure quarantine, or None.

    Two classes of assignments must never be auto-disabled:

    * **Built-in (seeded) assignments** — startup seeding force-re-enables them
      on every boot, so a quarantine only creates a disable/resurrect loop that
      masks the underlying credential problem while the queued fan-out runs
      fail with "Assignment is disabled".
    * **Company-managed per-user fan-out assignments** (``run_as_all_users``) —
      the Redis failure counter is per ASSIGNMENT, not per user, so a single
      user with an expired credential would cross the threshold and disable the
      shared automation for every other user.
    """
    if agent.is_builtin:
        return "built-in seeded assignments are never auto-disabled"
    if assignment.run_as_all_users and assignment.is_company_managed:
        return (
            "per-user fan-out assignments are never auto-disabled — one "
            "user's broken credential must not disable the automation for "
            "every user"
        )
    return None


async def _track_assignment_auth_failures(
    db: AsyncSession,
    *,
    run: AssignmentRun,
    assignment: Assignment,
    agent: Agent,
    run_id: str,
    messages: list,
) -> None:
    """Post-run auth-failure tracking + quarantine gate.

    Detects whether any integration tool call in *messages* returned an
    auth/permission failure and tracks consecutive auth-failure runs across
    the assignment's lifetime.  When the consecutive count crosses
    ``assignment_auth_fail_quarantine_threshold``:
      1. The assignment is auto-disabled (``enabled = False`` — committed by
         the caller together with the run).
      2. A durable Activity notification is written for the owner.
      3. An SSE notification is published (owner channel; company channel
         only for company-managed / owner-less assignments).

    Only autonomous scheduler runs (``schedule`` / ``schedule_fanout``)
    advance the counter — a manual retry while diagnosing a broken credential
    must never push the assignment over the threshold.  A clean run of ANY
    trigger type resets the counter immediately so a re-authorized credential
    takes effect on the very next run.

    Built-in (seeded) assignments and company-managed per-user fan-out
    assignments are EXEMPT from the quarantine entirely — see
    ``_quarantine_exempt_reason`` — an actionable warning is logged instead
    of disabling, and clean runs still reset the counter.

    Strictly best-effort: all Redis + notification calls fail-open and this
    helper never re-raises, so it can never corrupt the caller's final
    ``db.commit()``.
    """
    threshold = settings.assignment_auth_fail_quarantine_threshold
    if (
        threshold <= 0  # feature disabled
        or not assignment.enabled  # only act on still-enabled assignments
        or not messages  # guard: empty when failure happened before the loop
    ):
        return
    try:
        failures = _auth_failure_contents(messages)
        if not failures:
            # Clean run — reset the counter so a re-authorized credential
            # clears the quarantine trigger immediately.
            await _reset_assignment_auth_failures(
                str(assignment.id),
                report_user_id=run.user_id if _is_per_user_fanout(assignment) else None,
            )
            return
        kind = _auth_failure_kind(failures)
        quarantine_exempt_reason = _quarantine_exempt_reason(assignment, agent)
        if quarantine_exempt_reason:
            # Never quarantine these assignments. Log an actionable
            # warning instead — the per-run failure stays visible in the run
            # history, and the credential owner still sees the failed run.
            logger.warning(
                "Assignment '%s' run %s contained integration auth failures — "
                "not counted toward auto-quarantine (%s). %s",
                assignment.name,
                run_id,
                quarantine_exempt_reason,
                _auth_failure_remedy(kind),
            )
            await _report_after_consecutive_failures(
                db,
                run=run,
                assignment=assignment,
                agent=agent,
                run_id=run_id,
                reason=quarantine_exempt_reason,
                kind=kind,
                threshold=threshold,
            )
            return
        trigger_type = (run.trigger_context or {}).get("type")
        if trigger_type not in _QUARANTINE_TRIGGER_TYPES:
            # Non-scheduler run with auth failures: don't advance the counter
            # (manual/webhook/work-item retries must not quarantine), but
            # don't reset it either — the credential is still demonstrably
            # broken.
            logger.warning(
                "Assignment '%s' run %s (trigger '%s') contained integration "
                "auth failures — not counted toward auto-quarantine "
                "(scheduler runs only)",
                assignment.name,
                run_id,
                trigger_type or "unknown",
            )
            await _report_after_consecutive_failures(
                db,
                run=run,
                assignment=assignment,
                agent=agent,
                run_id=run_id,
                reason=(
                    f"only scheduled runs are auto-paused; this one was "
                    f"triggered by '{trigger_type or 'unknown'}'"
                ),
                kind=kind,
                threshold=threshold,
            )
            return
        consecutive = await _record_assignment_auth_failure(str(assignment.id))
        logger.warning(
            "Assignment '%s' run %s contained integration auth failures "
            "(%d consecutive run(s) with auth failures across runs)",
            assignment.name,
            run_id,
            consecutive,
        )
        if consecutive >= threshold:
            # Pause provenance: stamp the reason on the assignment
            # itself, not only in this log line and the Activity feed. The
            # Activity row is easy to miss (and is scoped to the owner); the
            # stamp is what the assignment page renders next to the "Paused"
            # badge, so the answer to "why did this stop firing?" is on screen.
            from app.services.assignment_pause import (
                pause_assignment as _pause_assignment,
            )

            _pause_assignment(
                assignment,
                reason="auth_quarantine",
                details=(
                    f"Auto-paused after {consecutive} consecutive scheduled "
                    "run(s) whose integration tool calls failed "
                    f"authentication. {_auth_failure_remedy(kind)} Then "
                    "re-enable this assignment."
                ),
                actor_user_id=None,
                actor_label=_ENGINE_PAUSE_ACTOR_LABEL,
            )
            logger.error(
                "Assignment '%s' auto-disabled after %d consecutive run(s) "
                "with integration authentication failures. %s Then re-enable "
                "the assignment to resume.",
                assignment.name,
                consecutive,
                _auth_failure_remedy(kind),
            )
            await _handle_assignment_auth_failure(
                db,
                assignment=assignment,
                agent=agent,
                run_id=run_id,
                consecutive=consecutive,
                kind=kind,
            )
    except Exception as exc:
        logger.debug(
            "Auth-failure tracking failed for assignment '%s' run %s: %s",
            assignment.name,
            run_id,
            format_exception_message(exc),
        )


# ---------------------------------------------------------------------------
# Stale integration references: fail the run, tell the owner, pause
#
# A provider reports a SUSTAINED stale reference - a resource the run uses
# that its integration reports missing in separate runs, such as a Notion
# database deleted or no longer shared - through
# ``app.integrations.stale_references``; a first-time miss (possibly the
# agent's own typo) is never reported. The engine collects them around the
# agent loop, and a run that finished with one is recorded as FAILED
# (``stale_integration_reference``), its summary kept.
#
# After that run is committed, ``_track_stale_reference_failure`` reads the
# assignment's recent runs under a row lock on the assignment - runs of one
# assignment may overlap, and the lock serializes their decisions:
#   * the FIRST failure of an episode gets a durable notice to the owner,
#     never the ones after it;
#   * after ``assignment_stale_reference_pause_after`` consecutive SCHEDULED
#     runs failed this way, the scheduled run completing the streak pauses
#     the assignment (``stale_reference``), the pause is audited under a
#     system actor and the owner told why.
# The streak is read off the run rows, not kept in a counter. Only runs
# created after ``Assignment.last_paused_at`` count (resuming starts a fresh
# streak); a scheduled run that succeeds or fails another way ends it; manual,
# webhook and event runs neither extend nor end it, whatever their outcome;
# a run the activation gate skipped never ran the agent and is ignored.
# Built-in and per-user fan-out assignments are reported, never paused, and a
# per-user fan-out reads each user's runs apart.
# ---------------------------------------------------------------------------
_ACTIVITY_STALE_REFERENCE = "assignment_stale_reference"
_ACTIVITY_STALE_REFERENCE_PAUSE = "assignment_stale_reference_pause"
_STALE_REFERENCE_AUDIT_ACTOR = "assignment-engine"
# Fewest recent terminal runs read to derive a streak; a streak longer than
# the threshold is never needed, the margin is for the runs it skips.
_STALE_STREAK_MIN_SCAN = 50


@dataclass(frozen=True, slots=True)
class _StreakRun:
    """What the stale-reference streak reads of one terminal run."""

    id: _uuid.UUID
    status: str
    failure_cause: str | None
    trigger_type: str | None
    gate_skipped: bool
    created_at: datetime | None


def _is_stale_reference_failure(run: _StreakRun) -> bool:
    return (
        run.status == "failed"
        and run.failure_cause == FailureCause.STALE_INTEGRATION_REFERENCE.value
    )


def _before_streak_boundary(run: _StreakRun, since: datetime | None) -> bool:
    return since is not None and run.created_at is not None and run.created_at <= since


def _stale_reference_streak(runs: list[_StreakRun], *, since: datetime | None) -> int:
    """Consecutive scheduled stale-reference failures at the newest end of *runs*.

    *runs* are the assignment's terminal runs, newest first; *since* is the
    assignment's last pause, before which nothing counts. A gate-skipped run
    and a manual / webhook / event run are neutral whatever their outcome:
    they neither extend the streak nor end it.
    """
    streak = 0
    for run in runs:
        if _before_streak_boundary(run, since):
            break
        if run.gate_skipped or run.trigger_type not in _QUARANTINE_TRIGGER_TYPES:
            continue
        if not _is_stale_reference_failure(run):
            break
        streak += 1
    return streak


def _starts_stale_reference_episode(
    runs: list[_StreakRun], run_id: _uuid.UUID, *, since: datetime | None
) -> bool:
    """True when no stale-reference failure precedes *run_id* in its episode.

    Walking back from *run_id* (newest first): a stale-reference failure of
    any trigger means the episode is already running - its owner was told;
    a scheduled run that did not fail this way ended the previous one; a
    gate-skipped run and a manual / webhook / event run that did not fail
    this way are neutral, as they are for the streak.
    """
    seen = False
    for run in runs:
        if not seen:
            seen = run.id == run_id
            continue
        if _before_streak_boundary(run, since):
            return True
        if run.gate_skipped:
            continue
        if _is_stale_reference_failure(run):
            return False
        if run.trigger_type in _QUARANTINE_TRIGGER_TYPES:
            return True
    return True


async def _load_streak_runs(
    db: AsyncSession,
    assignment_id: _uuid.UUID,
    *,
    limit: int,
    user_id: _uuid.UUID | None = None,
) -> list[_StreakRun]:
    """The assignment's recent terminal runs that matter to the streak, newest first.

    One user's only if *user_id*. A neutral run - a gate skip, a manual /
    webhook / event run that did not fail on a stale reference - is filtered
    out HERE, not only in Python: a burst of webhook runs must not crowd the
    scheduled history out of the bounded scan.
    """
    trigger_type = AssignmentRun.trigger_context["type"].as_string()
    gate_skipped = AssignmentRun.result_summary.startswith(
        _ACTIVATION_SKIP_SUMMARY_PREFIX, autoescape=True
    )
    query = select(
        AssignmentRun.id,
        AssignmentRun.status,
        AssignmentRun.failure_cause,
        trigger_type,
        gate_skipped,
        AssignmentRun.created_at,
    ).where(
        AssignmentRun.assignment_id == assignment_id,
        AssignmentRun.status.in_(("completed", "failed")),
        or_(
            and_(
                AssignmentRun.status == "failed",
                AssignmentRun.failure_cause
                == FailureCause.STALE_INTEGRATION_REFERENCE.value,
            ),
            and_(
                trigger_type.in_(sorted(_QUARANTINE_TRIGGER_TYPES)),
                or_(
                    AssignmentRun.status == "failed",
                    AssignmentRun.result_summary.is_(None),
                    ~gate_skipped,
                ),
            ),
        ),
    )
    if user_id is not None:
        query = query.where(AssignmentRun.user_id == user_id)
    rows = (
        await db.execute(
            query.order_by(AssignmentRun.created_at.desc(), AssignmentRun.id.desc()).limit(
                limit
            )
        )
    ).all()
    return [
        _StreakRun(
            id=row[0],
            status=row[1],
            failure_cause=row[2],
            trigger_type=row[3],
            gate_skipped=bool(row[4]),
            created_at=row[5],
        )
        for row in rows
    ]


async def _stale_reference_error(
    db: AsyncSession, collector: StaleReferenceCollector
) -> StaleIntegrationReferenceError:
    """The error a run records for the stale references it hit, integrations named.

    The name lookup is best-effort and runs in a SAVEPOINT: a failed read
    aborts the PostgreSQL transaction the run's failure is written in next.
    """
    references = collector.references
    names: dict[str, str] = {}
    try:
        by_id: dict[_uuid.UUID, str] = {}
        for ref in references:
            with contextlib.suppress(ValueError):
                by_id[_uuid.UUID(ref.integration_id)] = ref.integration_id
        if by_id:
            from app.models.integration import Integration

            async with db.begin_nested():
                rows = (
                    await db.execute(
                        select(Integration.id, Integration.name).where(
                            Integration.id.in_(list(by_id))
                        )
                    )
                ).all()
            names = {by_id[row[0]]: row[1] for row in rows if row[1]}
    except Exception:
        logger.debug(
            "Could not resolve the integrations of %d stale reference(s)",
            len(references),
            exc_info=True,
        )
    return StaleIntegrationReferenceError(references, integration_names=names)


async def _shown_result_summary(
    db: AsyncSession,
    run: AssignmentRun,
    bindings: dict | None,
    *,
    guard: bool,
) -> str | None:
    """``run.result_summary`` as the Result card and the notifications show it.

    The links on a bound platform's internal URL are moved onto its
    registered URL and, with *guard*, the ``/dashboard`` links are
    validated. *bindings* are the run's own, the ones its tools were
    built from. Applied once, to the summary the run keeps.
    """
    if not run.result_summary:
        return run.result_summary
    from app.services.url_guard import collect_platform_link_bases, finalize_platform_links

    return await finalize_platform_links(
        run.result_summary,
        lambda: collect_platform_link_bases(db, bindings),
        run_id=str(run.id),
        guard=guard,
    )


async def _link_run_output_files(
    db: AsyncSession,
    run: AssignmentRun,
    file_context: FileContext,
    trigger_file_ids: list[str],
) -> None:
    """Attach the files a tool produced during *run* to it (its trigger files excluded).

    Shared by the success path and a run that finished on a stale integration
    reference: what the agent produced stays with the run either way.
    A file the run only fetched to read (``FileContext.fetched_ids``) is linked
    only when no conversation and no other run holds it: one another scope
    lent it - a work item's attachment - keeps its binding, and with
    it who may read it.
    Each link runs in a SAVEPOINT: a failed statement aborts the PostgreSQL
    transaction, and the run's own outcome is written on this session after.
    """
    from sqlalchemy import or_ as _sql_or
    from sqlalchemy import update as _sql_update

    from app.models.file_attachment import FileAttachment

    trigger_file_id_set = set(trigger_file_ids)
    fetched_ids = getattr(file_context, "fetched_ids", None) or set()
    for file_ref in file_context.list_files():
        if file_ref.file_id in trigger_file_id_set:
            continue
        try:
            file_id = _uuid.UUID(file_ref.file_id)
            link = _sql_update(FileAttachment).where(FileAttachment.id == file_id)
            if str(file_ref.file_id).removeprefix("file-") in fetched_ids:
                link = link.where(
                    FileAttachment.conversation_id.is_(None),
                    _sql_or(
                        FileAttachment.assignment_run_id.is_(None),
                        FileAttachment.assignment_run_id == run.id,
                    ),
                )
            async with db.begin_nested():
                await db.execute(link.values(assignment_run_id=run.id))
        except Exception:
            logger.warning(
                "Failed to associate file attachment %s with assignment run %s",
                file_ref.file_id,
                run.id,
                exc_info=True,
            )


def _stale_references_payload(error: StaleIntegrationReferenceError) -> list[dict]:
    return [
        {
            "provider": ref.provider,
            "integration_id": ref.integration_id,
            "integration_name": error.integration_names.get(ref.integration_id),
            "resource_type": ref.resource_type,
            "resource_id": ref.resource_id,
            "failing_runs": ref.failing_runs,
        }
        for ref in error.references
    ]


def _stale_pause_details(error: StaleIntegrationReferenceError, streak: int) -> str:
    """What the assignment page says about the pause - the resources and the remedy."""
    if len(error.references) == 1:
        ref = error.references[0]
        missing = describe_reference(ref, error.integration_names)
        remedy = (
            f"Share it with that integration in {ref.provider_label}, or update "
            "the assignment so it no longer uses it, then re-enable the assignment."
        )
    else:
        missing = f"{len(error.references)} integration resources (" + "; ".join(
            describe_reference(ref, error.integration_names) for ref in error.references
        ) + ")"
        remedy = (
            "Share each one with its integration, or update the assignment so it "
            "no longer uses them, then re-enable the assignment."
        )
    return (
        f"Auto-paused after {streak} consecutive scheduled runs that could not "
        f"find {missing}. {remedy}"
    )


def _stale_failure_summary(
    assignment: Assignment,
    error: StaleIntegrationReferenceError,
    *,
    threshold: int,
    exempt_reason: str | None,
) -> str:
    if exempt_reason:
        outlook = f" It will not be paused automatically: {exempt_reason}."
    elif threshold > 0:
        outlook = (
            f" It is paused automatically after {threshold} consecutive "
            "scheduled runs like this one."
        )
    else:
        outlook = ""
    return f"Assignment '{assignment.name}' failed. {error}{outlook}"


async def _track_stale_reference_failure(
    db: AsyncSession,
    *,
    run: AssignmentRun,
    assignment: Assignment,
    agent: Agent,
    error: StaleIntegrationReferenceError,
) -> None:
    """Tell the owner once, and pause after a streak of stale-reference failures.

    Runs once the failed run is committed, so a concurrent run of the same
    assignment sees it, and on a session of its own (*db* is not the run's):
    its writes are rolled back if anything in it fails, and a rollback expires
    every instance of its session. The row lock serializes the decision, and
    ``enabled`` re-read under it is what keeps two runs reaching the threshold
    together from pausing - and notifying - twice. A per-user fan-out reads
    only the history of this run's user: each user's run uses that user's
    integrations, and another user's success in between is not the end of
    this user's episode. Best-effort: never raises.
    """
    threshold = settings.assignment_stale_reference_pause_after
    try:
        from app.services.assignment_pause import (
            pause_assignment,
            record_system_pause_audit,
        )

        locked = (
            await db.execute(
                select(Assignment)
                .where(Assignment.id == assignment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked is None:
            return
        since = locked.last_paused_at
        per_user_fanout = bool(locked.run_as_all_users and locked.is_company_managed)
        runs = await _load_streak_runs(
            db,
            locked.id,
            limit=max(_STALE_STREAK_MIN_SCAN, 3 * threshold),
            user_id=run.user_id if per_user_fanout else None,
        )
        streak = _stale_reference_streak(runs, since=since)
        exempt_reason = _quarantine_exempt_reason(locked, agent)
        extra_data = {
            "assignment_id": str(locked.id),
            "run_id": str(run.id),
            "references": _stale_references_payload(error),
            "consecutive_failures": streak,
            "threshold": threshold,
        }
        # Only a scheduled run pauses: a person re-running a broken assignment
        # to look at it must not be the one that switches it off.
        run_is_scheduled = (run.trigger_context or {}).get("type") in _QUARANTINE_TRIGGER_TYPES
        if (
            threshold > 0
            and run_is_scheduled
            and locked.enabled
            and exempt_reason is None
            and streak >= threshold
        ):
            details = _stale_pause_details(error, streak)
            pause_assignment(
                locked,
                reason="stale_reference",
                details=details,
                actor_user_id=None,
                actor_label=_ENGINE_PAUSE_ACTOR_LABEL,
            )
            logger.error(
                "Assignment '%s' auto-paused after %d consecutive scheduled "
                "run(s) that failed on a stale integration reference: %s",
                locked.name,
                streak,
                error,
            )
            anchor_user_id = locked.user_id or (
                run.user_id if locked.is_company_managed else None
            )
            if anchor_user_id is not None:
                await record_system_pause_audit(
                    db,
                    assignment=locked,
                    actor=_STALE_REFERENCE_AUDIT_ACTOR,
                    anchor_user_id=anchor_user_id,
                    reason="stale_reference",
                    details=details,
                    extra={
                        "run_id": str(run.id),
                        "consecutive_failures": streak,
                        "references": extra_data["references"],
                    },
                )
            else:
                logger.warning(
                    "Assignment '%s' auto-paused with no user to anchor its "
                    "audit entry on; the pause is recorded on the assignment",
                    locked.name,
                )
            await _emit_assignment_notice(
                db,
                assignment=locked,
                agent=agent,
                activity_type=_ACTIVITY_STALE_REFERENCE_PAUSE,
                title=(
                    f"Assignment '{locked.name}' paused - an integration "
                    "resource it uses is missing"
                ),
                summary=details,
                extra_data={**extra_data, "paused": True},
                company_recipient_id=run.user_id,
            )
        elif locked.paused_reason != "stale_reference" and _starts_stale_reference_episode(
            runs, run.id, since=since
        ):
            await _emit_assignment_notice(
                db,
                assignment=locked,
                agent=agent,
                activity_type=_ACTIVITY_STALE_REFERENCE,
                title=(
                    f"Assignment '{locked.name}' is failing - an integration "
                    "resource it uses is missing"
                ),
                summary=_stale_failure_summary(
                    locked, error, threshold=threshold, exempt_reason=exempt_reason
                ),
                extra_data={**extra_data, "paused": False},
                company_recipient_id=run.user_id,
            )
        await db.commit()
    except Exception:
        logger.warning(
            "Stale-reference tracking failed for assignment '%s' run %s",
            assignment.name,
            run.id,
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await db.rollback()


def resolve_assignment_tool_timeout(
    timeout_seconds: float | None,
    max_iterations: int,
) -> int | None:
    """Resolve a per-tool timeout cap for high-iteration assignment runs.

    High-iteration assignments (e.g. 40 iterations with a 1800s wall clock
    budget) can otherwise spend too much of the whole run budget inside a few
    long tool calls. For those runs one tool call may take at most a fixed
    share of the run budget (``timeout_seconds / _TOOL_CAP_BUDGET_DIVISOR``,
    clamped to ``[_TOOL_CAP_FLOOR_SECONDS, TOOL_EXECUTION_TIMEOUT_SECONDS]``).

    The share is deliberately NOT ``timeout_seconds / max_iterations``:
    ``_apply_iteration_timeout_floor`` guarantees at least 20 s per iteration,
    so that quotient always landed on the floor - raising an assignment from 30
    to 31 iterations cut every tool call from 300 s to 30 s, and at 100
    iterations only a timeout above 8 h gave the 300 s back. Above the gate a
    bigger iteration budget never shrinks the time a single tool call gets,
    since the floored budget only grows with it; crossing the gate is the one
    deliberate step, from the loop default to that share (124 s at the default
    timeout).

    ``timeout_seconds`` is the EFFECTIVE run budget computed by the engine —
    the ``_UNSET_TIMEOUT_FALLBACK_SECONDS``-normalized, iteration-floored
    value fed to ``asyncio.wait_for`` (see ``_apply_iteration_timeout_floor``)
    — never ``assignment.timeout_seconds`` directly, whose ``None`` (an unset
    in-memory attribute; the column is non-nullable) means "fallback applies"
    rather than "disabled".

    Returns ``None`` when no extra cap should be applied (the agent-loop
    default remains in effect): the run has no wall-clock budget
    (``timeout_seconds`` is ``None``, i.e. the per-run timeout is disabled)
    or its iteration budget is at or below ``_TOOL_CAP_MIN_ITERATIONS``.
    The ``<= 0`` guard is defensive only — a normalized budget is always
    positive.
    """
    # The loop's built-in default is the ceiling — the derived cap can only
    # ever tighten it, never widen it. Imported lazily to mirror the
    # existing deferred ``run_agent_loop`` import in this module.
    from app.tools.agent_loop import TOOL_EXECUTION_TIMEOUT_SECONDS

    if timeout_seconds is None or timeout_seconds <= 0:
        return None
    # Keep existing behaviour for normal iteration budgets.
    if max_iterations <= _TOOL_CAP_MIN_ITERATIONS:
        return None
    derived = int(timeout_seconds / _TOOL_CAP_BUDGET_DIVISOR)
    return max(
        _TOOL_CAP_FLOOR_SECONDS, min(TOOL_EXECUTION_TIMEOUT_SECONDS, derived)
    )


async def resolve_run_max_iterations(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
) -> int:
    """Resolve the effective agentic-loop cap for a run of ``assignment``.

    Wraps :func:`resolve_effective_max_iterations` with the platform-default
    lookup (``GeneralAssistantConfig.max_iterations``) so every call site —
    the agentic-loop setup in ``execute_assignment_run``, the orchestration
    ``deadline_at`` computations in ``trigger_assignment`` and the
    retry-on-failure path — shares one resolution and the wall-clock floor
    can never drift between the inner ``asyncio.wait_for`` bound and the
    engine deadline.
    """
    from app.models.general_assistant_config import (
        DEFAULT_MAX_ITERATIONS,
        GeneralAssistantConfig,
    )

    # Explicit ``is None`` (not ``or``) so only a MISSING GA row falls back
    # to the Python default: a stored 0 (legacy / direct DB edit) must stay
    # 0 here and be floored to 1 by clamp_max_iterations, not silently
    # become the default.
    platform_row_max_iterations = (
        await db.execute(select(GeneralAssistantConfig.max_iterations).limit(1))
    ).scalar_one_or_none()
    platform_max_iterations = (
        platform_row_max_iterations
        if platform_row_max_iterations is not None
        else DEFAULT_MAX_ITERATIONS
    )
    # The agent's home fleet default sits between the agent and the platform;
    # read only when neither the assignment nor the agent pins one.
    agent_max_iterations = agent.max_iterations
    if assignment.max_iterations is None and agent_max_iterations is None:
        from app.services.fleet_runs import fleet_run_defaults, member_max_iterations

        agent_max_iterations = member_max_iterations(None, await fleet_run_defaults(db, agent))
    return resolve_effective_max_iterations(
        assignment.max_iterations,
        agent_max_iterations,
        platform_max_iterations,
    )


async def resolve_run_wait_policy(
    db: AsyncSession,
    assignment: object | None = None,
    agent: object | None = None,
) -> WaitPolicy:
    """Resolve the wait bounds for an assignment run.

    Assignment > agent > platform (Settings > Agents and automations > Agent runs), the platform
    values being the ceilings.  A missing platform row resolves to the
    defaults: an assignment must not lose its timer because the settings were
    never saved.
    """
    from app.services.wait_settings import resolve_run_wait_policy as _resolve

    return await _resolve(db, agent=agent, assignment=assignment)


async def _run_activation_gate(
    db: AsyncSession,
    *,
    agent,
    user_id: str | None,
    rendered_activation: str,
    parent_trace_ctx: dict | None,
) -> str:
    """Run the assignment's pre-flight activation-gate LLM call.

    Opens a child ``activation-gate`` generation under ``parent_trace_ctx``
    (the workflow root span) so the gate appears nested under the
    assignment run in Langfuse — sibling to the iteration spans of the
    eventual agent loop.

    Returns the upper-cased model answer (typically ``"YES"`` / ``"NO"`` /
    ``"SKIP..."``).  The caller inspects the prefix to decide whether to
    skip the run.  Re-raises any LLM exception after recording the error
    on the generation.

    Prompt / caller contract
    ------------------------
    The system prompt advertises ``YES``, ``NO``, and ``SKIP`` as the
    accepted answers (matching ``execute_assignment_run``'s
    ``answer.startswith("NO") or answer.startswith("SKIP")`` check
    around line 602). ``SKIP`` lets the model decline assignments it
    judges out-of-scope or irrelevant with a brief explanation
    (``SKIP: <reason>``) instead of forcing it into the binary ``NO``
    answer for cases where ``NO`` would imply a falsifying judgement
    rather than non-applicability — useful for assignments whose
    activation prompt is itself ambiguous on edge-case inputs.
    """
    from app.llm.base import ChatMessage
    from app.services.langfuse_tracing import (
        end_generation,
        start_activation_gate_generation,
    )

    await ensure_hot_path_imports("app.llm.router")
    from app.llm.agent_model import agent_model_selection
    from app.llm.router import get_llm_for_user

    # The agent's whole selection, as for its run: the provider alone
    # is None for a routing agent, which then ran its gate on the default
    # model, and it dropped a provider config pin.
    selection = await agent_model_selection(db, agent)
    activation_llm, activation_model = await get_llm_for_user(
        db,
        user_id,
        provider_override=selection.provider,
        model_override=selection.model,
        provider_id_override=selection.provider_id,
    )
    activation_messages = [
        ChatMessage(
            role="system",
            content=(
                "You are a filter. Answer with exactly one of: YES, NO, "
                "or SKIP. Use YES when the assignment should run, NO when "
                "it should not, and SKIP (optionally followed by a colon "
                "and a brief reason) when the assignment is not "
                "applicable to this input."
            ),
        ),
        ChatMessage(role="user", content=rendered_activation),
    ]
    gate_ctx = start_activation_gate_generation(
        parent_trace_ctx,
        model=activation_model,
        messages=activation_messages,
    )
    # Two narrower handlers (asyncio ``CancelledError`` + ``Exception``)
    # instead of one ``except BaseException``: both still record the
    # cancellation or failure reason on the Langfuse generation before
    # propagating, but ``SystemExit`` / ``KeyboardInterrupt`` now pass
    # through uninstrumented — same convention as the surrounding agent-
    # loop iteration / continuation / empty-retry / tool-execution
    # sites.  Process-terminating exceptions must never be caught by
    # tracing instrumentation (even when the catcher re-raises), both
    # for CodeQL ``py/catch-base-exception`` cleanliness and to keep an
    # operator-initiated SIGINT visible to higher layers without trace
    # noise.
    activation_error: BaseException | None = None
    content: str | None = None
    activation_usage = None
    # Hand the pooled connection back before the gate LLM call: the setup
    # reads (agent/assignment/group lookups + the provider resolution just
    # above) opened a transaction that would otherwise pin the engine
    # session's connection for the whole generation — multiplied by the
    # reconciler's concurrent execution slots. Setup is
    # read-only here (run status was committed earlier).
    await release_db_connection_before_loop(db)
    try:
        activation_response = await activation_llm.chat(
            messages=activation_messages,
            model=activation_model,
            max_tokens=10,
        )
        content = activation_response.content
        activation_usage = getattr(activation_response, "usage", None)
    except asyncio.CancelledError as exc:
        activation_error = exc
        raise
    except Exception as exc:
        activation_error = exc
        raise
    finally:
        if gate_ctx is not None:
            end_generation(
                gate_ctx,
                output=content,
                usage=activation_usage,
                model=activation_model,
                error=activation_error,
            )
    return (content or "").strip().upper()


async def _activation_check_executor(
    db: AsyncSession,
    *,
    tool: str,
    run: AssignmentRun,
    agent,
    assignment: Assignment,
    effective_user_id,
):
    """The agent's tools an activation check may call, as the run would build them.

    The built-in coordination tools the agent holds (the ``coordination``
    group or its former ids ``work_items`` / ``shared_state``), then - only when the
    check's tool is not among them - the integration tools of the bindings the
    run reads (the assignment's, else the agent's), read-only bindings and the
    enforced Scope(s) applied.     MCP servers and custom tools are not connected
    for a check.
    """
    from app.services.builtin_tool_aliases import runtime_builtin_tool_ids
    from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY
    from app.services.integration_live_writes import LIVE_WRITES_CONTEXT_KEY, live_writes_overrides
    from app.services.integration_read_only import (
        READ_ONLY_CONTEXT_KEY,
        read_only_integration_ids,
    )
    from app.services.integration_tools import build_integration_tools, flatten_bindings
    from app.tools.base import CompositeToolExecutor

    composite = CompositeToolExecutor()
    file_owner_id = _resolve_run_file_owner_id(run) or ""
    # The ids the run wires from: a merged group (``coordination``) expands
    # into the former ids its allow list keeps, as the run's own tools do.
    builtin_ids = set(
        runtime_builtin_tool_ids(agent.builtin_tool_ids or [], getattr(agent, "builtin_allowed_tools", None))
    )
    if "work_items" in builtin_ids:
        from app.services.builtin_tools import build_work_item_tools

        composite.add_tools(
            *build_work_item_tools(
                db,
                file_owner_id,
                str(agent.id),
                is_company_managed=run.is_company_managed,
                company_managed_agent=agent.is_company_managed,
            )
        )
    if "shared_state" in builtin_ids:
        from app.services.builtin_tools import build_shared_state_tools

        composite.add_tools(
            *build_shared_state_tools(
                db,
                file_owner_id,
                str(agent.id),
                is_company_managed=run.is_company_managed,
                company_managed_agent=agent.is_company_managed,
                run_id=run.id,
            )
        )
    if composite.resolve_canonical_name(tool) is not None:
        return composite
    # The fleet reads (get_fleet_health, #1400), as the run wires them: the
    # agent holds them through its platform group, an on-demand one included.
    if "fleets" in builtin_ids and effective_user_id:
        from app.services.builtin_tools import build_fleet_tools

        composite.add_tools(
            *build_fleet_tools(str(effective_user_id), assignment_run_id=str(run.id), agent_id=str(agent.id))
        )
        if composite.resolve_canonical_name(tool) is not None:
            return composite
    bindings = flatten_bindings(
        agent.integration_bindings or {}
        if assignment.integration_bindings is None
        else assignment.integration_bindings
    )
    if bindings:
        scope = assignment_run_scope(
            agent.integration_element_bindings,
            agent.integration_element_enforcement,
            assignment.integration_element_bindings,
            assignment.integration_element_enforcement,
        )
        context = {
            "source": "assignment_activation_check",
            "agent": agent.name,
            "agent_id": str(agent.id),
            "assignment": assignment.name,
            "run_id": str(run.id),
            "autonomous": True,
            "hitl_enabled": False,
            "user_id": str(effective_user_id) if effective_user_id else None,
            "element_scope": scope.enforced,
            READ_ONLY_CONTEXT_KEY: read_only_integration_ids(agent, bindings),
            SIGNATURE_CONTEXT_KEY: getattr(agent, "integration_signatures", None),
            LIVE_WRITES_CONTEXT_KEY: live_writes_overrides(agent, bindings),
        }
        composite.add_tools(*await build_integration_tools(db, bindings, context=context))
    return composite


def _gate_check_due(run: AssignmentRun, *, resuming: bool) -> bool:
    """Whether this attempt evaluates the assignment's gate check.

    Once per run, at dispatch: a HITL resume (``awaiting_human``) and a resume
    from the checkpoint after a worker stop or a rollout (*resuming*) are past
    the gate and continue the run's loop (#8831).
    """
    return run.status != "awaiting_human" and not resuming


async def _run_holds_a_lease(db: AsyncSession, run_id) -> bool:
    """Whether *run_id* holds the lease of any shared-state entry.

    Read before a gate check ends a run skipped: a run holding a lease has
    started its work. A read that fails answers False (the gate decides).
    """
    from app.models.shared_state import SharedState

    try:
        held = await db.execute(select(SharedState.id).where(SharedState.held_by_run_id == run_id).limit(1))
        return held.first() is not None
    except Exception as exc:
        logger.warning("Lease read for run %s failed (%s)", run_id, format_exception_message(exc))
        return False


async def _activation_check_zone(db: AsyncSession, assignment: Assignment, agent):
    """The zone a gate check's date placeholders resolve in (#10319).

    The trigger's ``timezone`` (a schedule's cron zone), else the agent
    owner's profile timezone, else UTC. Never raises.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from app.core.utils import schedule_timezone
    from app.models.user import User

    config = getattr(assignment, "trigger_config", None)
    if isinstance(config, dict) and str(config.get("timezone") or "").strip():
        return schedule_timezone(config)[0]
    owner_id = getattr(agent, "user_id", None)
    if owner_id is None:
        return UTC
    try:
        name = (await db.execute(select(User.timezone).where(User.id == owner_id))).scalar_one_or_none()
    except Exception as exc:
        logger.warning("Owner timezone read for agent %s failed (%s)", getattr(agent, "id", None), exc)
        return UTC
    if not isinstance(name, str) or not name.strip():
        return UTC
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


async def _evaluate_one_gate_check(
    db: AsyncSession,
    check: dict,
    *,
    run: AssignmentRun,
    agent,
    assignment: Assignment,
    effective_user_id,
):
    """One check of the gate, ``None`` when it could not run (the run proceeds past it)."""
    from app.services.assignment_activation_check import (
        check_tool_label,
        run_activation_check,
        uses_date_placeholders,
    )

    arguments = check.get("arguments") or {}
    # One writer per engine-run turn: an attempt
    # taken over before its gate runs neither calls the gate tool nor ends the run.
    await ensure_single_writer()
    try:
        executor = await _activation_check_executor(
            db,
            # ``tool@<integration id>`` never resolves among the built-ins,
            # so an integration tool check always reaches the integration tools.
            tool=check_tool_label(check),
            run=run,
            agent=agent,
            assignment=assignment,
            effective_user_id=effective_user_id,
        )
        # The executor's setup awaits integrations and the database: re-checked right before the gate tool runs.
        # TurnSuperseded is a cancellation: the ``except Exception`` lets it through.
        await ensure_single_writer()
        outcome = await run_activation_check(
            check,
            executor,
            placeholders={"agent.id": str(agent.id), "assignment.id": str(assignment.id)},
            payload=getattr(run, "trigger_payload", None),
            run_id=str(run.id),
            zone=(await _activation_check_zone(db, assignment, agent)) if uses_date_placeholders(arguments) else None,
        )
    except Exception as exc:
        logger.warning(
            "Assignment '%s': gate check %s could not run (%s) - the run proceeds",
            assignment.name,
            check["tool"],
            format_exception_message(exc),
            exc_info=True,
        )
        return None
    if outcome.error and check.get("integration_id"):
        outcome = await _with_integration_state(db, check, outcome)
    if outcome.passed and outcome.error:
        logger.warning(
            "Assignment '%s': %s: %s - the run proceeds",
            assignment.name,
            outcome.summary,
            outcome.error,
        )
    return outcome


async def _with_integration_state(
    db: AsyncSession, check: dict[str, Any], outcome: ActivationCheckOutcome
) -> ActivationCheckOutcome:
    """*outcome* with its error naming the check's integration when it is not usable (#1665).

    A gate tool of an integration in ``error`` or ``reauthorize`` fails, or is
    not built at all; the reason recorded on the integration is what the owner
    needs on the run, not only the tool's own failure.
    """
    import dataclasses

    from app.services.integration_status import unusable_integrations

    integration_id = str(check["integration_id"])
    state = (await unusable_integrations(db, [integration_id])).get(integration_id)
    if state is None:
        return outcome
    return dataclasses.replace(outcome, error=f"integration {integration_id} {state}; {outcome.error}")


async def _skip_by_activation_check(
    db: AsyncSession,
    *,
    run: AssignmentRun,
    agent,
    assignment: Assignment,
    effective_user_id,
) -> bool:
    """Run the assignment's tool check; ``True`` when it ended the run as skipped.

    Evaluated before the run is charged, before a model is resolved and before
    the LLM gate: a skipped run spends no token. A check that cannot be
    evaluated lets the run go ahead, the reason logged and kept on the run.
    """
    from app.services.assignment_activation_check import (
        chain_position,
        chained_checks,
        check_tool_label,
        describe_condition,
        is_any_of,
        stored_activation_check,
    )

    stored = stored_activation_check(getattr(assignment, "activation_check", None))
    if stored is None:
        return False
    if is_any_of(stored):
        return await _skip_by_any_of_checks(
            db, stored, run=run, agent=agent, assignment=assignment, effective_user_id=effective_user_id
        )
    # The first check, then those its ``also`` chains (#10597): the run is
    # skipped at the first that fails, still with no model call.
    checks = chained_checks(stored)
    failed = None
    passed_over: list[dict] = []
    for position, check in enumerate(checks, start=1):
        outcome = await _evaluate_one_gate_check(
            db, check, run=run, agent=agent, assignment=assignment, effective_user_id=effective_user_id
        )
        if outcome is not None and outcome.misconfigured:
            await _record_misconfigured_gate_check(run, assignment, check, outcome)
        if outcome is None or (outcome.passed and outcome.error):
            # Passed over: named on the run and counted on the assignment (#10724).
            passed_over.append(
                {
                    "position": position,
                    "tool": check_tool_label(check),
                    "reason": (outcome.error if outcome is not None else "the check could not run")[:300],
                }
            )
            continue
        if not outcome.passed:
            failed = (position, check, outcome)
            break
    if passed_over:
        await _record_unevaluated_gate_checks(run, assignment, passed_over)
    if failed is None:
        return False
    position, check, outcome = failed
    arguments = check.get("arguments") or {}
    summary = chain_position(
        outcome.summary,
        position=position,
        total=len(checks),
        tool=check_tool_label(check),
        arguments=outcome.arguments,
    )
    asked = (
        f"Gate check: {check['tool']}({json.dumps(arguments)}), condition: {describe_condition(check.get('condition'))}"
    )
    return await _end_run_skipped_by_gate_check(
        db,
        run=run,
        agent=agent,
        assignment=assignment,
        summary=summary,
        asked=asked,
        preview=outcome.preview,
        live_holder=outcome.live_holder,
    )


async def _skip_by_any_of_checks(
    db: AsyncSession,
    stored: dict,
    *,
    run: AssignmentRun,
    agent,
    assignment: Assignment,
    effective_user_id,
) -> bool:
    """The ``or`` form of the tool check: skipped only when EVERY check fails.

    The checks run in order and the first that passes lets the run go ahead;
    one that cannot be evaluated lets it go ahead too, named on the run as a
    check passed over. ``True`` when the run ended as skipped.
    """
    from app.services.assignment_activation_check import (
        any_of_skip_summary,
        chained_checks,
        check_tool_label,
        describe_check,
    )

    failures: list[tuple[str, str, dict | None]] = []
    previews: list[str] = []
    live_holder: str | None = None
    for position, check in enumerate(chained_checks(stored), start=1):
        outcome = await _evaluate_one_gate_check(
            db, check, run=run, agent=agent, assignment=assignment, effective_user_id=effective_user_id
        )
        if outcome is not None and outcome.misconfigured:
            await _record_misconfigured_gate_check(run, assignment, check, outcome)
        if outcome is None or (outcome.passed and outcome.error):
            reason = outcome.error if outcome is not None else "the check could not run"
            await _record_unevaluated_gate_checks(
                run, assignment, [{"position": position, "tool": check_tool_label(check), "reason": reason[:300]}]
            )
            return False
        if outcome.passed:
            return False
        failures.append((check_tool_label(check), outcome.summary, outcome.arguments))
        if outcome.preview:
            previews.append(outcome.preview)
        live_holder = live_holder or outcome.live_holder
    return await _end_run_skipped_by_gate_check(
        db,
        run=run,
        agent=agent,
        assignment=assignment,
        summary=any_of_skip_summary(failures),
        asked=f"Gate check, {describe_check(stored)}",
        preview="\n\n".join(previews),
        live_holder=live_holder,
    )


async def _end_run_skipped_by_gate_check(
    db: AsyncSession,
    *,
    run: AssignmentRun,
    agent,
    assignment: Assignment,
    summary: str,
    asked: str,
    preview: str,
    live_holder: str | None,
) -> bool:
    """End *run* as skipped by its tool check, at zero tokens; ``False`` when it must proceed."""
    if await _run_holds_a_lease(db, run.id):
        # A run that already took a shared-state lease has started its work:
        # ending it skipped would release the hold with the work undone (#8831).
        logger.warning(
            "Assignment '%s': %s, but run %s holds a shared-state lease - the run proceeds",
            assignment.name,
            summary,
            run.id,
        )
        return False
    # Taken over while the gate tool ran: the attempt that goes on owns the run's ending.
    await ensure_single_writer()
    if live_holder and await _hold_event_for_lease(run, assignment, live_holder):
        summary = f"{summary}; the event is delivered again once run {live_holder} ends"
    now = datetime.now(UTC)
    run.status = "completed"
    run.result_summary = summary
    # As the LLM gate records its exchange: the run panel shows what was asked
    # and what came back, though no model was called.
    run.messages = [
        {"role": "user", "content": asked},
        {"role": "assistant", "content": f"{summary}\n\n{preview}".strip()},
    ]
    run.total_input_tokens = 0
    run.total_output_tokens = 0
    run.started_at = run.started_at or now
    run.completed_at = now
    await db.commit()
    await _release_run_work_item(run, agent, failed=False)
    await _publish_run_status(
        str(run.user_id),
        str(run.id),
        "skipped",
        assignment.name,
        run.result_summary,
        is_company=run.is_company_managed,
    )
    logger.info("Assignment '%s' skipped by its gate check: %s", assignment.name, summary)
    return True


async def _hold_event_for_lease(run: AssignmentRun, assignment: Assignment, holder_run_id: str) -> bool:
    """Keep the CI event a gate-skipped run stood down on, until the live lease holder ends (#11988).

    Only an integration-event run on a watched CI head (``event_enrichment.watch_subject``)
    is kept; ``False`` for any other run, or when the store is unavailable. Never raises.
    """
    try:
        if str(holder_run_id) == str(run.id) or getattr(assignment, "trigger_type", None) != "integration_event":
            return False
        context = getattr(run, "trigger_context", None) or {}
        provider = context.get("provider")
        event_key = context.get("event_key")
        payload = getattr(run, "trigger_payload", None)
        if not isinstance(provider, str) or not isinstance(event_key, str) or not isinstance(payload, dict):
            return False
        from app.services import event_enrichment, event_enrichment_retry

        subject = event_enrichment.watch_subject(provider, event_key, payload)
        if subject is None:
            return False
        return await event_enrichment_retry.hold_for_lease(
            assignment_id=assignment.id,
            provider=provider,
            event_key=event_key,
            payload=payload,
            subject=subject,
            holder_run_id=str(holder_run_id),
            trigger_context=context,
        )
    except Exception as exc:
        logger.info("Assignment '%s': event not held for the lease: %s", assignment.name, format_exception_message(exc))
        return False


async def _record_misconfigured_gate_check(
    run: AssignmentRun, assignment: Assignment, check: dict[str, Any], outcome: ActivationCheckOutcome
) -> None:
    """Name a gate check that can never pass as configured (#1400): a friction and a drop reason.

    The run is skipped by it (no model call); the friction under the run's
    scope and the reason counted on the assignment make the configuration
    error visible to the owner and to the fleet's health read.
    """
    from app.services.assignment_activation_check import check_tool_label
    from app.services.frictions import note_friction, run_scope
    from app.services.integration_event_filters import record_filter_reasons

    tool = check_tool_label(check)
    note_friction(
        "tool_error",
        tool,
        f"gate check misconfigured on assignment '{assignment.name}' ({assignment.id}): {outcome.error}",
        scope=run_scope(run, assignment),
    )
    context = dict(getattr(run, "trigger_context", None) or {})
    context["gate_check_misconfigured"] = {"tool": tool, "reason": (outcome.error or "")[:300]}
    run.trigger_context = context
    await record_filter_reasons(assignment.id, {f"gate check misconfigured: {tool}": 1})


async def _record_unevaluated_gate_checks(run: AssignmentRun, assignment: Assignment, passed_over: list[dict]) -> None:
    """Name the gate checks passed over on the run and count them on the assignment (#10724).

    A check that cannot be evaluated still lets the run go ahead; this only
    makes it visible: ``trigger_context.gate_checks_not_evaluated`` on the run,
    one drop reason per check on the assignment (``get_assignment`` lists them).
    """
    from app.services.assignment_activation_check import unevaluated_reason
    from app.services.integration_event_filters import record_filter_reasons

    context = dict(getattr(run, "trigger_context", None) or {})
    context["gate_checks_not_evaluated"] = passed_over
    # A new dict: the JSON column sees the change.
    run.trigger_context = context
    await record_filter_reasons(
        assignment.id, {unevaluated_reason(item["position"], item["tool"]): 1 for item in passed_over}
    )


#: How every gate-skipped run's ``result_summary`` starts - what tells one
#: apart from a run that did its work.
_ACTIVATION_SKIP_SUMMARY_PREFIX = "Skipped: activation check returned"


def _format_skip_summary(answer: str) -> str:
    """Build a clean, single-line ``result_summary`` for a gate-skipped run.

    The activation gate is instructed to reply with exactly ``YES`` / ``NO`` /
    ``SKIP[: reason]``, but models frequently append a free-text explanation.
    Because the gate call runs on a tiny ``max_tokens`` budget, that trailing
    text is truncated mid-sentence and carries embedded newlines — e.g.
    ``"NO\nTHIS IS AN ISSUE CLOSURE EVENT (ACTION"``. Embedding it verbatim
    produced an ugly multi-line summary like::

        Skipped: activation check returned 'NO'
        THIS IS AN ISSUE CLOSURE EVENT (ACTION

    Collapse all whitespace and keep only the decision token. A documented
    ``SKIP: <reason>`` input still surfaces its reason — extracted and rendered
    as ``'SKIP' (<reason>)`` in the summary — so a genuine skip explanation
    isn't lost (the trailing junk on a bare ``NO`` is dropped entirely).
    """
    collapsed = " ".join(answer.split())
    if collapsed.startswith("SKIP"):
        reason = collapsed[len("SKIP") :].lstrip(" :-").strip()
        if reason:
            return f"{_ACTIVATION_SKIP_SUMMARY_PREFIX} 'SKIP' ({reason})"
        return f"{_ACTIVATION_SKIP_SUMMARY_PREFIX} 'SKIP'"
    return f"{_ACTIVATION_SKIP_SUMMARY_PREFIX} 'NO'"


def _schedule_spread_delay_seconds(
    assignment_id: _uuid.UUID, trigger_context: dict | None
) -> int:
    """Return deterministic delay to spread schedule-triggered assignment bursts.

    The returned offset is in the **inclusive** range ``[0, spread_window]``
    (so a ``spread_window`` of 30 produces 31 distinct delays, 0 through 30).
    The offset is derived from the assignment UUID, so the same assignment
    gets the same offset on every trigger — this turns ":00" thundering herds
    into a deterministic per-assignment spread.

    The assignment's FIRST scheduled run (the auto-run the scheduler fires
    right after creation, flagged ``_first_run`` in the trigger context) is
    exempt and returns 0: the spread exists to smear *recurring* top-of-hour
    bursts across the peak window, and a single just-created assignment is not
    part of a herd. Applying it to the initial run only made a freshly created
    assignment look stuck ("delayed / waiting…") for up to the spread window.
    """
    if not trigger_context or trigger_context.get("type") != "schedule":
        return 0
    if trigger_context.get("_first_run"):
        return 0
    spread_window = max(0, settings.scheduled_assignment_spread_seconds)
    if spread_window == 0:
        return 0
    # `+ 1` is intentional: produces an inclusive range so a spread_window
    # of 30s gives offsets in `[0, 30]` (31 distinct buckets). This is more
    # intuitive for operators than `[0, 29]` and mirrors the docstring.
    offset = assignment_id.int % (spread_window + 1)
    logger.debug(
        "Schedule spread: assignment=%s window=%ds offset=%ds",
        assignment_id,
        spread_window,
        offset,
    )
    return offset


# ── Transient model-failure retry ────────────────────────────────────
# When a run fails because the LLM PROVIDER was unhealthy (rate limit / quota,
# overload, outage, connection failure) — i.e. after ``retry_llm_call`` and the
# failover chain are both exhausted — the failure is about the provider's
# minute-scale weather, not about the assignment. Those runs are retried
# automatically (no ``retry_on_failure`` opt-in needed), a bounded number of
# times, with an exponential + jittered delay so the retry doesn't land on the
# same rate-limit window that killed the original run.
#
# Status codes as standalone numbers (429/5xx/529). The lookarounds reject
# digit-grouped numbers so token counts in non-transient errors can't
# false-positive (e.g. "~12,429 tokens" in a context-overflow message, or
# "429,000"). Keywords mirror the provider retryable predicates
# (``is_retryable_openai``, ``_is_retryable_anthropic``,
# ``_is_retryable_gemini``, ``_is_retryable_bedrock``).
_TRANSIENT_LLM_STATUS_RE = re.compile(r"(?<![\d,])\b(429|500|502|503|529)\b(?!,?\d)")
_TRANSIENT_LLM_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "rate-limit",
    "too many requests",
    "overloaded",
    "quota",
    "resource exhausted",
    "resource_exhausted",
    "throttl",  # AWS Bedrock "throttling" / "throttled"
    "service unavailable",
    "temporarily unavailable",
    "internal server error",
    "connection error",
    "api connection",
)


def _is_transient_llm_failure(error: str | None) -> bool:
    """True when a run's error string looks like a transient LLM provider
    failure (rate limit, quota, overload, outage, connection loss).

    Deliberately keyword-based: by the time the failure reaches the run
    record it is a plain string (``run.error``), the original exception is
    gone. Platform-side quota exhaustion (``QuotaExceededError``) never
    reaches the retry path — it commits and returns before the agentic loop.

    ``run.failure_cause`` now records the same judgement structurally,
    and is where this predicate should eventually move. It is deliberately NOT
    consulted yet: the two do not agree exactly (a failover chain that
    exhausted every provider is ``all_providers_failed`` structurally but
    matches no keyword here), and phase 1 changes nothing about how a failure
    is *acted on* — it only measures. Switching the retry trigger over is a
    behaviour change that belongs in its own commit, once the recorded
    distribution says what it would actually do.

    Tool/integration HTTP failures (a 429/5xx from GitHub, Slack, OpenCTI,
    an MCP server, ...) can NOT false-positive here: the agent loop catches
    every tool exception and feeds it back to the LLM as an error
    tool-result message (``tools/agent_loop.py``), and integration tools
    additionally wrap failures in ``ToolResult(is_error=True)``
    (``services/integration_tools.py``) — so a tool error never propagates
    into ``run.error``. An exception that fails the RUN with an HTTP status
    can only come from the LLM provider call itself.
    """
    if not error:
        return False
    lowered = error.lower()
    if _TRANSIENT_LLM_STATUS_RE.search(lowered):
        return True
    return any(kw in lowered for kw in _TRANSIENT_LLM_KEYWORDS)


def _record_failure_cause(
    run: AssignmentRun,
    cause: FailureCause,
    detail: str | None = None,
) -> None:
    """Stamp a structurally-known cause on a failed run.

    The convenience form for the sites that already KNOW why they are failing
    the run — an assignment that is gone, an agent that is disabled, a quota
    that is spent — and so have no exception to classify. Everything reached
    through an exception goes through ``classify_run_failure`` instead.
    """
    run.failure_cause = cause.value
    run.failure_determination = FailureDetermination.STRUCTURAL.value
    if detail:
        logger.debug("Run %s failure cause: %s (%s)", run.id, cause.value, detail)


def _deadline_failure_never_started(run: AssignmentRun, last_usage: Any) -> bool:
    """A run failed on its deadline before any model call returned.

    Such a run never worked: it waited in the queue until its deadline was
    spent. ``retry_on_failure`` is for a run that failed while working; a
    retry of this one only re-queues it into the same saturation.
    """
    return run.failure_cause == FailureCause.ENGINE_DEADLINE_EXCEEDED.value and last_usage is None


def _apply_failure_classification(
    run: AssignmentRun, classification: FailureClassification
) -> None:
    """Persist a classification produced from an exception."""
    run.failure_cause = classification.cause.value
    run.failure_determination = classification.determination.value


REPLACED_ATTEMPT = "replaced"


async def _ended_elsewhere(
    db: AsyncSession,
    run: AssignmentRun,
    *,
    place: tuple[int, int] | None = None,
    starting: bool = False,
) -> str | None:
    """Why this attempt must not write its ending on *run*, or ``None``:
    the ending another writer gave it, or ``REPLACED_ATTEMPT``.

    Every attempt commits ``running`` before its loop and writes an ending -
    a terminal status, or the ``awaiting_human`` of a pause - only when it
    ends, so an ending already on the row was written by someone else: the
    re-run that replaced this attempt after its lease was lost, the
    dead-letter, a cancel. That ending stands. And an attempt a later one
    replaced (*place*, ``attempt_charges.fence_attempt``, below the row's
    fence) writes no ending at all: the run ends as the attempt the engine
    runs it with ends, whichever of the two finishes first. Either way this
    attempt writes neither its ending nor what follows it (notification,
    chain, retry).

    Asked as the attempt starts (*starting*: a paused run is where a resume
    starts, not an ending), which covers every ending written before its loop,
    and where it is about to write its ending after the loop. The row is read
    without flushing this attempt's copy of the run, and stays locked until
    the attempt's next commit, so no other ending lands in between. Never
    asked after this attempt's own ending was set - a later flush would make
    the read return it. A read that fails raises: a failed statement leaves
    the transaction unusable, so the attempt could not commit its ending
    anyway.
    """
    rows = (
        await db.execute(
            select(AssignmentRun.status, AssignmentRun.trigger_context)
            .where(AssignmentRun.id == run.id)
            .with_for_update()
            .execution_options(autoflush=False)
        )
    ).all()
    if not rows:
        return None
    status, context = rows[0][0], rows[0][1]
    if status in TERMINAL_RUN_STATUSES or (status == "awaiting_human" and not starting):
        logger.warning(
            "Assignment run %s was already %s by another attempt or the "
            "orchestration engine; this attempt's ending is discarded",
            run.id,
            status,
        )
        return status
    if place is not None and attempt_fence(context) > place:
        logger.warning(
            "Assignment run %s: this attempt was replaced after its lease was "
            "lost; its ending is discarded, the attempt that replaced it ends "
            "the run",
            run.id,
        )
        return REPLACED_ATTEMPT
    return None


async def _teardown_partner_work(
    db: AsyncSession,
    run: AssignmentRun,
    integ_exec: Any,
    *,
    handed_back: bool,
    replaced: bool,
) -> None:
    """Cancel the partner work of an attempt whose run is over (FR-005, FR-025).

    Only an attempt that still owns its run tears its partners down: one
    handed back to the queue or replaced by a later attempt after its
    lease was lost leaves its outstanding delegations and partner
    tasks to the attempt that goes on with the run, which collects their
    answers. A terminal outcome, this attempt's own or another writer's,
    still cancels them. Suppressed so teardown survives a partner that cannot
    be told.
    """
    if handed_back or replaced:
        logger.info(
            "Assignment run %s: this attempt was %s; its partner tasks stay outstanding "
            "for the attempt that goes on with the run",
            run.id,
            "handed back" if handed_back else "replaced",
        )
        return
    from app.services.integration_tools import (
        cancel_outstanding_delegations,
        cancel_run_partner_tasks,
    )

    # A run that ends must not leave a partner working on an
    # ``input-required`` task nobody will answer.
    if integ_exec is not None:
        with contextlib.suppress(Exception):
            await cancel_outstanding_delegations(integ_exec)
    # And the tasks handed back as still running, which outlive the executor.
    # A run that COMPLETED keeps them: the agent was told the answer would
    # reach it, and it does - ``_resume_caller`` raises
    # ``a2a.delegation.completed`` for an ended run, and a later run of the
    # same agent and user may collect the task (#12079). A run that failed or
    # was cancelled still cancels them (FR-005); a person's cancel also goes
    # through ``mark_cancel_owed`` and ``spawn_run_cancel``.
    if await _run_completed(db, run):
        logger.info(
            "Assignment run %s completed: its still-running partner tasks are kept; "
            "their answers arrive as a2a.delegation.completed",
            run.id,
        )
        return
    with contextlib.suppress(Exception):
        await cancel_run_partner_tasks(db, run.id)


async def _run_completed(db: AsyncSession, run: AssignmentRun) -> bool:
    """Whether *run* ended ``completed``, read from the row as stored now.

    Read rather than taken from the instance: an out-of-band cancel writes
    ``cancelled`` on the row while this attempt still holds the old status.
    Unreadable counts as not completed, so the teardown keeps cancelling.
    """
    try:
        status = (await db.execute(select(AssignmentRun.status).where(AssignmentRun.id == run.id))).scalar_one_or_none()
    except Exception:
        return False
    return status == "completed"


def _transient_retry_delay_seconds(retry_count: int) -> int:
    """Backoff for automatic transient-failure retries: exponential on the
    retry ordinal, capped, with jitter (uniform in [0.5, 1.0] of the slot) so
    a burst of simultaneously-failed fan-out runs doesn't re-fire as the same
    thundering herd that tripped the provider in the first place.
    """
    base = max(1, settings.assignment_transient_retry_base_seconds)
    cap = max(base, settings.assignment_transient_retry_max_delay_seconds)
    slot = min(cap, base * (2**max(0, retry_count)))
    return max(1, int(random.uniform(0.5, 1.0) * slot))  # noqa: S311 — jitter, not crypto


def _resolve_run_file_owner_id(run: AssignmentRun) -> str | None:
    """Owner to stamp on files this run generates (``FileAttachment.user_id``).

    Returns ``None`` for company-managed runs — generated artifacts get no
    personal owner so file access is governed solely by the run's
    ``is_company_managed`` admin gate. ``_authorize_file_access`` consults the
    file owner BEFORE the linked run, so stamping the always-set run-as /
    triggering user (typically a non-admin) would let them download a
    company-managed run's files despite company-run access being admin-gated.

    Personal runs keep their execution identity as the owner so the chat user
    can still download their own files. This is ONLY the file owner — it never
    changes the tool execution identity, which keeps using ``run.user_id``.
    """
    if run.is_company_managed:
        return None
    return str(run.user_id) if run.user_id else None


def _inbox_release_reason(loop_result: Any, messages: Any, offered_tools: Any) -> str | None:
    """Why an inbox run's end must not complete its item, or ``None``.

    A run wound down before its work was done (the tool failure brake, the
    iteration backstop, the budget): whatever its status, its item is not done.
    A run that was offered ``complete_work_item`` and ended without completing
    the item (the settle step sees an item still not terminal): it is released
    too, the run had its own way to close it. Only a run that had no such tool
    and ended cleanly lets its end complete the item.
    """
    from app.tools.agent_loop import turn_wound_down

    wound_down = turn_wound_down(loop_result, messages or [])
    if wound_down:
        return f"the run was wound down: {wound_down}"
    names = {getattr(tool, "name", None) for tool in offered_tools or []}
    if "complete_work_item" in names:
        return "it did not call complete_work_item"
    return None


def dispatched_work_item_id(trigger_context: object, trigger_payload: object) -> str | None:
    """The work item a run's trigger names, only when the server-built context agrees with the payload.

    The context is the inbox dispatcher's own record (retries copy it); a
    payload alone (a manual trigger, a webhook body) is never enough.
    """
    context = trigger_context if isinstance(trigger_context, dict) else {}
    payload = trigger_payload if isinstance(trigger_payload, dict) else {}
    item_id = context.get("work_item_id")
    if (
        context.get("type") != "work_item"
        or payload.get("trigger_type") != "work_item"
        or not item_id
        or str(payload.get("work_item_id")) != str(item_id)
    ):
        return None
    return str(item_id)


def _inbox_dispatched_work_item(run: AssignmentRun) -> tuple[str, str | None] | None:
    """The work item the inbox dispatcher handed this run, with its user.

    ``(work_item_id, user_id)`` only when the server-built trigger context - the
    dispatcher's own record, which retries copy - names the same item as the
    payload; ``None`` otherwise. The payload alone is never enough: a manual
    trigger or a webhook body can put any ``work_item_id`` there, and the run
    finalizer settles the item as the run's agent. The user scopes the
    visibility of the item: its own owning user, which the dispatcher wrote
    into the payload, else the run's.
    """
    payload = run.trigger_payload or {}
    item_id = dispatched_work_item_id(run.trigger_context, payload)
    if item_id is None:
        return None
    user_id = (payload.get("work_item") or {}).get("user_id") or (
        str(run.user_id) if run.user_id else None
    )
    return str(item_id), user_id


def _run_lineage_ids(run: AssignmentRun) -> set[_uuid.UUID]:
    """This run's id and its parent's: the runs whose own files it may read."""
    lineage = {run.id}
    parent_run_id = getattr(run, "parent_run_id", None)
    if parent_run_id is not None:
        lineage.add(parent_run_id)
    return lineage


async def load_authorized_trigger_files(
    db: AsyncSession,
    run: AssignmentRun,
    file_ids: object,
) -> list:
    """The ``trigger_payload["file_ids"]`` rows this run may legitimately read.

    The list is CALLER-SUPPLIED - a manual trigger body, a flow, or a generic
    webhook body copied verbatim into ``trigger_payload`` - so an id in it is
    a claim, not a grant: loaded by bare primary key and attached to
    the user message, any ``FileAttachment`` whose UUID a webhook caller knew
    or guessed had its bytes inlined into the prompt. A row is admitted when
    EITHER holds:

    * :func:`authorize_file_access` admits it for the run's execution
      identity (``run.user_id`` - the run-as / triggering user, the same
      identity the run's tools act as) - the ONE answer to "may this user
      read these bytes?", so owner, admin, shared conversation, run scope,
      template asset and readable-deck grants all apply unchanged;
    * the row is bound to THIS run or its parent run
      (``FileAttachment.assignment_run_id``) - the run lineage's own output,
      which a company-managed automation with no user of its own could not
      otherwise re-read.

    Everything else is DROPPED and logged at debug; a foreign id never fails
    the run, or naming one would be a way to deny it. Malformed ids and rows
    that no longer exist are skipped the same way. Returns the admitted rows
    in payload order, de-duplicated.
    """
    if not isinstance(file_ids, list | tuple) or not file_ids:
        return []
    from fastapi import HTTPException

    from app.models.file_attachment import FileAttachment
    from app.models.user import User
    from app.services.file_access import authorize_file_access

    run_user: User | None = None
    if run.user_id is not None:
        try:
            run_user = (
                await db.execute(select(User).where(User.id == run.user_id))
            ).scalar_one_or_none()
        except Exception:
            logger.warning(
                "Run %s: could not resolve its user for trigger file authorization",
                run.id,
                exc_info=True,
            )
    lineage = _run_lineage_ids(run)
    admitted: list = []
    seen: set[_uuid.UUID] = set()
    for raw in file_ids:
        try:
            file_uuid = _uuid.UUID(str(raw))
        except (TypeError, ValueError, AttributeError):
            logger.debug("Run %s: ignoring malformed trigger file id %r", run.id, raw)
            continue
        if file_uuid in seen:
            continue
        seen.add(file_uuid)
        try:
            fa = (
                await db.execute(
                    select(FileAttachment).where(FileAttachment.id == file_uuid)
                )
            ).scalar_one_or_none()
        except Exception:
            logger.warning(
                "Run %s: failed to load trigger file %s", run.id, file_uuid, exc_info=True
            )
            continue
        if fa is None:
            logger.debug("Run %s: trigger file %s does not exist", run.id, file_uuid)
            continue
        allowed = getattr(fa, "assignment_run_id", None) in lineage
        if not allowed and run_user is not None:
            try:
                await authorize_file_access(fa, run_user, db)
                allowed = True
            except HTTPException:
                allowed = False
            except Exception:
                logger.warning(
                    "Run %s: trigger file %s authorization errored; dropping it",
                    run.id,
                    file_uuid,
                    exc_info=True,
                )
                allowed = False
        if not allowed:
            logger.debug(
                "Run %s: dropping trigger file %s - not readable by user %s "
                "and not bound to this run",
                run.id,
                file_uuid,
                run.user_id,
            )
            continue
        admitted.append(fa)
    return admitted


async def load_dispatched_work_item_files(run: AssignmentRun) -> list:
    """The ``FileRef`` of each file attached to the work item the inbox dispatched to *run*.

    Only for the item the server-built trigger context names
    (:func:`_inbox_dispatched_work_item`), re-read through the work-item
    visibility of its user - never the ids a payload carries. The item is
    the run's trigger, so its files are the run's trigger files: on its user
    message, and never re-bound to the run. Never raises: a lookup that fails
    costs the files (logged), not the run.
    """
    dispatched = _inbox_dispatched_work_item(run)
    if dispatched is None:
        return []
    item_id, user_id = dispatched
    from app.services.work_item import get_work_item
    from app.services.work_item_files import MAX_DISPATCH_INLINED_ATTACHMENTS, work_item_file_refs

    try:
        item = await get_work_item(item_id, user_id=user_id)
    except Exception:
        logger.warning(
            "Run %s: could not read its work item %s for its files", run.id, item_id, exc_info=True
        )
        return []
    refs, _gone = await work_item_file_refs(item)
    # The newest round only: an item worked over several rounds carries more
    # files than a first message should inline; get_work_item registers the
    # older ones for the reader by reference.
    return refs[-MAX_DISPATCH_INLINED_ATTACHMENTS:]


#: Files of earlier runs of the same work item registered on a run: the newest
#: ones, by reference. A file list, not a file store: the prompt names each.
MAX_EARLIER_ITEM_RUN_FILES = 30


async def load_earlier_item_run_files(db: AsyncSession, run: AssignmentRun) -> list:
    """The ``FileAttachment`` rows earlier runs of this run's work item produced.

    A work item is often worked over several runs (a run cut by a rollout and
    redispatched, an item reopened by a wake or a verdict): each run is a new
    conversation-less scope, so without this a later run cannot open by id the
    generator or the metrics an earlier run of the SAME item wrote. Only for
    the item the server-built trigger context names
    (:func:`_inbox_dispatched_work_item`), never a payload id; only the rows
    bound to runs of that item by the SAME agent for the SAME user, never a
    template asset; the newest :data:`MAX_EARLIER_ITEM_RUN_FILES`. Another
    item's run, another agent's or another user's reads none of them. The
    caller registers them by reference: they are listed, not inlined. Never
    raises: a failed lookup costs the files (logged), not the run.
    """
    dispatched = _inbox_dispatched_work_item(run)
    if dispatched is None:
        return []
    item_id, _user_id = dispatched
    from app.models.file_attachment import FileAttachment

    context = AssignmentRun.trigger_context
    try:
        rows = (
            (
                await db.execute(
                    select(FileAttachment)
                    .join(AssignmentRun, AssignmentRun.id == FileAttachment.assignment_run_id)
                    .where(
                        context["type"].as_string() == "work_item",
                        context["work_item_id"].as_string() == str(item_id),
                        AssignmentRun.agent_id == run.agent_id,
                        AssignmentRun.user_id.is_not_distinct_from(run.user_id),
                        AssignmentRun.id != run.id,
                        FileAttachment.template_container_id.is_(None),
                    )
                    .order_by(FileAttachment.created_at.desc())
                    .limit(MAX_EARLIER_ITEM_RUN_FILES)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.warning(
            "Run %s: could not load the files of earlier runs of work item %s", run.id, item_id, exc_info=True
        )
        return []
    return list(reversed(rows))


async def load_run_generated_files(
    db: AsyncSession, run: AssignmentRun, *, exclude_ids: set[str]
) -> list:
    """The ``FileAttachment`` rows THIS run produced, minus *exclude_ids*.

    Every file a tool writes during a run is stamped ``assignment_run_id`` at
    creation (``generate_file``, the code interpreter, custom-tool
    ``$output_files``), so a run that pauses for human input and resumes in a
    fresh process finds what it produced before the pause here - the
    persisted transcript carries the ids in the tool results but never the
    refs, so without this the read / edit tools could not find them.
    """
    from app.models.file_attachment import FileAttachment

    try:
        rows = (
            (
                await db.execute(
                    select(FileAttachment)
                    .where(FileAttachment.assignment_run_id == run.id)
                    .order_by(FileAttachment.created_at)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.warning(
            "Run %s: could not load its generated files for resume", run.id, exc_info=True
        )
        return []
    return [fa for fa in rows if str(fa.id) not in exclude_ids]


def apply_run_usage_accounting(
    run: AssignmentRun,
    usage: dict | None,
    trace_ctx: dict | None,
) -> None:
    """Best-effort: persist token totals and the Langfuse trace id on a run.

    Accounting must never fail a run — any error is logged and swallowed
    (OBS-5 / OBS-6).
    """
    try:
        from app.services.langfuse_tracing import (
            get_local_trace_id,
            get_trace_id,
            usage_token_totals,
        )

        # Trace id FIRST: it is independent of the usage payload, and
        # deep-linking matters most when something else went wrong — a
        # malformed provider usage dict must not lose the trace link.
        trace_id = get_trace_id(trace_ctx)
        if trace_id:
            run.langfuse_trace_id = trace_id
        local_trace_id = get_local_trace_id(trace_ctx)
        if local_trace_id:
            run.llm_trace_id = local_trace_id
        input_tokens, output_tokens = usage_token_totals(usage)
        if input_tokens or output_tokens:
            run.total_input_tokens = input_tokens
            run.total_output_tokens = output_tokens
    except Exception:
        logger.warning(
            "Run usage accounting failed for run %s",
            getattr(run, "id", "?"),
            exc_info=True,
        )


# ── Seeded-automation agentic-quota exemption ───────────────────────────────
# Built-in CTEM agents (the five stage analysts + the exposure briefing analyst)
# run a per-user WEEKLY schedule that fans out to every active user. Metering
# those automatic runs against each user's agentic quota would burn the quota on
# work the user never initiated. Such runs are therefore exempt from BOTH the
# quota check and the usage increment.
#
# SECURITY: the exemption is gated FIRST on ``agent.is_builtin`` — a flag that is
# only ever set by the startup seeder and is NEVER writable through any create /
# update API (not even by admins), so a user can never mark their own agent's
# runs as exempt. Within built-in agents, only NON-user-initiated runs qualify,
# identified by the server-controlled ``trigger_context``:
#   * the scheduler / per-user fan-out set ``type`` to "schedule"/"schedule_fanout";
#   * the seeded-dashboard FIRST auto-population sets ``quota_exempt`` (ctem_metrics).
# Deliberate user actions — the manual "Trigger" API, follow-ups, the
# "Regenerate" buttons — carry their own ``type`` ("manual"/"followup"/
# "briefing_refresh"/"stage_refresh") and never set ``quota_exempt``. The manual
# API hard-codes that context and sandboxes any caller-supplied payload under a
# nested ``["payload"]`` key, so neither exemption token can be forged from user
# input. Both gates must hold, so even a forged context is inert on a non-builtin
# agent.
_QUOTA_EXEMPT_TRIGGER_TYPES: frozenset[str] = frozenset({"schedule", "schedule_fanout"})


def _run_is_quota_exempt(agent: Agent, run: AssignmentRun) -> bool:
    """True when *run* is a seeded platform automation exempt from the agentic quota."""
    if not getattr(agent, "is_builtin", False):
        return False
    ctx = run.trigger_context or {}
    return ctx.get("type") in _QUOTA_EXEMPT_TRIGGER_TYPES or ctx.get("quota_exempt") is True


async def _run_unless_stopped(executor, call, cancel_event) -> str:
    """Run one call a resume owes a result, unless its turn was stopped.

    A stopped turn (the user's cancel, an emergency stop) runs nothing more: a
    call not yet started is answered as not run, and one running is cancelled
    like any mid-tool cancel, what it already did standing. Either way it gets
    a result, so the transcript stays valid.
    """
    if cancel_event is not None and cancel_event.is_set():
        return f"The {call.name} call was not run: the turn was stopped."
    # One writer per engine-run turn: an attempt taken over while it
    # waited for its reviewer, or before its resume prelude ran, runs none of
    # the approved or cleared calls; each is checked right before it runs.
    await ensure_single_writer()
    from app.tools.agent_loop import _CancelledMidCallError, _race_cancellation

    try:
        result = await _race_cancellation(executor.execute_tool(call), cancel_event)
    except _CancelledMidCallError:
        return f"The {call.name} call was stopped while it ran: the turn was cancelled."
    return result.content


def _refusal_without_run(decision: dict[str, Any], *, decided: bool) -> tuple[str | None, str]:
    """(gate outcome, first sentence of the tool result) of a call not run.

    Only a verdict a person gave reads "Rejected by human reviewer": a call
    the deadline refused, one the decision set carried no verdict for and one
    whose tool left the run's surface each say what refused it, so neither the
    agent nor an auditor credits a person with a refusal nobody made. The
    outcome is ``None`` for a person's refusal (the caller keeps
    ``human_<verdict>``).
    """
    from app.tools.approval import (
        DECISION_SOURCE_DEADLINE,
        DECISION_SOURCE_KEY,
        DECISION_SOURCE_NOT_OFFERED,
        OUTCOME_EXPIRED,
        OUTCOME_NOT_OFFERED,
        OUTCOME_UNDECIDED,
    )

    source = decision.get(DECISION_SOURCE_KEY)
    if source == DECISION_SOURCE_DEADLINE:
        return OUTCOME_EXPIRED, (
            "Not run: nobody answered the approval request before its deadline, "
            "so the platform refused it. No person rejected it."
        )
    if source == DECISION_SOURCE_NOT_OFFERED:
        reason = decision.get("rejection_reason") or "This tool is no longer available."
        return OUTCOME_NOT_OFFERED, f"Not run: {reason}"
    if not decided:
        return OUTCOME_UNDECIDED, (
            "Not run: the approval answer carried no verdict for this call, so it "
            "was refused by default. No person rejected it."
        )
    reason = decision.get("rejection_reason") or "No reason given."
    return None, f"Rejected by human reviewer: {reason}"


async def _apply_approval_decisions(
    *,
    executor,
    messages: list,
    proposals: list,
    decisions: dict,
    outcomes: dict[str, str] | None = None,
    on_approve_always=None,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Turn a reviewer's decisions into tool results, resuming a gated turn.

    Appends exactly one tool message per proposal, in the order the model
    emitted the calls. That completeness is not cosmetic: the conversation holds
    an assistant message with N ``tool_use`` blocks, and strict-mode
    Anthropic/OpenAI reject it on resume unless every one has a matching
    ``tool_result``. Skipping an undecided call would reproduce the exact
    orphaning the batch pre-screen exists to prevent.

    Approved calls are executed here rather than being handed back to the model
    to re-issue. The model already chose them and a human already approved
    *those* arguments; asking it again would risk it proposing something
    different, which nobody approved.
    """
    from app.llm.base import ChatMessage
    from app.tools.approval import is_approval
    from app.tools.base import ToolCall

    for proposal in proposals:
        call_id = proposal.get("tool_call_id")
        tool_name = proposal.get("tool_name", "")
        decided = isinstance(decisions.get(call_id), dict)
        decision = decisions.get(call_id) or {}
        verdict = decision.get("decision", "reject")
        refusal = None if is_approval(verdict) else _refusal_without_run(decision, decided=decided)

        if outcomes is not None:
            # The human's verdict IS this call's gate outcome (FR-020); a
            # refusal no person gave is named for what refused it.
            if refusal and refusal[0]:
                outcomes[call_id] = refusal[0]
            else:
                outcomes[call_id] = f"human_{verdict}"

        # An allow-list, so anything unrecognised refuses. This is the last
        # point before execution: a verdict the transports let through — a typo,
        # a stale client's value — must not become consent here.
        if refusal is not None:
            # A structured refusal, not an exception: the agent should be able
            # to read this, understand the action was declined, and adapt —
            # rather than treat it as a tool malfunction and retry blindly.
            messages.append(
                ChatMessage(
                    role="tool",
                    content=(
                        f"{refusal[1]} "
                        f"The {tool_name} call was not executed. Do not retry it "
                        f"unchanged; propose an alternative or ask how to proceed."
                    ),
                    tool_call_id=call_id,
                    name=tool_name,
                )
            )
            continue

        # approve | approve_always
        #
        # "Always" is two things: run this call, and remember the answer. The
        # run path persists the second half in its own endpoint; a chat has no
        # such endpoint, so it passes a sink here instead. Without one, "Yes,
        # always" quietly degraded to a plain approve while the prompt above it
        # promised the preference would cover scheduled runs too (FR-015,
        # FR-002b). Failure to remember must not block the call the reviewer
        # just approved.
        # A consent is given call by call: "always" approves this one.
        if verdict == "approve_always" and on_approve_always is not None and not proposal.get("requires_consent"):
            try:
                # With its decision: that names who answered (FR-017b).
                await on_approve_always(proposal, decision)
            except Exception:
                logger.warning(
                    "Could not persist the standing approval for %s",
                    tool_name,
                    exc_info=True,
                )

        # Always the arguments the agent proposed and the reviewer saw. There is
        # no edited variant: a decision answers the call as put, so what runs is
        # what was shown.
        arguments = proposal.get("arguments") or {}

        try:
            content = await _run_unless_stopped(
                executor,
                ToolCall(id=call_id, name=tool_name, arguments=arguments),
                cancel_event,
            )
        except Exception as exc:  # broad by design — surface as a tool result
            # An approved call that then fails is a normal tool error, not a
            # reason to abandon the resume: the remaining proposals still need
            # their results or the conversation is left invalid.
            logger.exception("Approved tool %s failed on resume", tool_name)
            content = f"Tool execution failed: {exc}"

        messages.append(
            ChatMessage(
                role="tool",
                content=content,
                tool_call_id=call_id,
                name=tool_name,
            )
        )



async def _dispatch_calls_the_pause_left_behind(
    *,
    executor,
    messages: list,
    outcomes: dict[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Run the batch calls a pause suspended but never proposed.

    Screening pauses the whole turn but proposes only the calls that FAIL the
    gate. A batch can mix them with calls that passed — whitelisted, or covered
    by a standing approval — and those were suspended along with everything else
    without ever being asked about. The applier gives them no result because
    nobody decided them, so resuming left their ``tool_use`` blocks orphaned and
    every provider rejected the conversation outright.

    They are not re-gated: their ids are added to the executor's bypass first.
    The gate cleared them before the pause and the reviewer was never asked
    about them because there was nothing to ask, so re-running it would invite a
    *second* pause inside one turn — the thing screening the batch up front
    exists to prevent. Worse, that pause would carry no proposals (the gate
    raises without them here), so it would render as a free-text box for a
    decision that is not free text. An exemption that lapsed while the reviewer
    was thinking is caught on the next turn, where it can be asked properly.

    Mirrors what the chat loop does by keeping unproposed calls in its dispatch
    list; the run path cannot, because a pause unwinds it entirely and it comes
    back from a serialized conversation rather than from live state.
    """
    from app.llm.base import ChatMessage
    from app.tools.approval import OUTCOME_CLEARED_PRE_PAUSE, OUTCOME_NOT_OFFERED
    from app.tools.base import ToolCall

    answered = {
        getattr(m, "tool_call_id", None)
        for m in messages
        if getattr(m, "tool_call_id", None)
    }
    # Only the batch that was paused — the last assistant message carrying tool
    # calls. Scanning the whole restored conversation would also pick up any
    # call left unanswered by some earlier turn and run it now, long after the
    # reasoning that proposed it.
    last_batch = next(
        (m for m in reversed(messages) if getattr(m, "tool_calls", None)), None
    )
    pending: list = [
        call
        for call in (getattr(last_batch, "tool_calls", None) or [])
        if getattr(call, "id", None) and call.id not in answered
    ]
    if not pending:
        return

    if executor is None:
        # The offered surface is empty, so there is nothing to run these
        # through. They still need a result each or the resumed conversation is
        # invalid, which is what this helper exists for.
        for call in pending:
            if outcomes is not None:
                outcomes.setdefault(call.id, OUTCOME_NOT_OFFERED)
            messages.append(
                ChatMessage(
                    role="tool",
                    content=(
                        f"The {call.name} call was not run: this assignment no "
                        f"longer offers that tool."
                    ),
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
        return

    # Cleared before the pause, so they must not gate on the way through.
    _bypass = getattr(executor, "bypass_approval_for", None)
    if _bypass is not None:
        _bypass({call.id for call in pending})

    for call in pending:
        try:
            content = await _run_unless_stopped(
                executor,
                ToolCall(id=call.id, name=call.name, arguments=call.arguments or {}),
                cancel_event,
            )
        except Exception as exc:  # broad by design — surface as a tool result
            # Same trade as an approved call that fails: a tool error is a
            # result, and abandoning the resume over one would strand every
            # other call in the batch without one.
            logger.exception("Cleared tool %s failed on resume", call.name)
            content = f"Tool execution failed: {exc}"

        if outcomes is not None:
            outcomes.setdefault(call.id, OUTCOME_CLEARED_PRE_PAUSE)

        messages.append(
            ChatMessage(
                role="tool",
                content=content,
                tool_call_id=call.id,
                name=call.name,
            )
        )


async def _release_run_work_item(
    run: AssignmentRun, agent: Agent, *, failed: bool, error: str | None = None
) -> None:
    """Hand an inbox run's work item back to the queue.

    Only the item the inbox dispatcher named (``_inbox_dispatched_work_item``),
    released as the run's agent, the writer that claimed it. ``failed``
    counts a bounded attempt against the item; a run that never started does
    not. Best effort: releasing must never mask the run's own outcome.
    """
    dispatched = _inbox_dispatched_work_item(run)
    if dispatched is None:
        return
    item_id, item_user = dispatched
    try:
        from app.services.company_managed_writes import agent_writer
        from app.services.work_item import release_work_item_claim

        await release_work_item_claim(
            work_item_id=item_id,
            user_id=item_user,
            failed=failed,
            error=error,
            writer=agent_writer(
                is_company_managed=agent.is_company_managed, agent_id=agent.id
            ),
        )
    except Exception as exc:
        logger.warning(
            "Failed to release work item %s of run %s: %s",
            item_id,
            run.id,
            format_exception_message(exc),
        )


def _actions_taken(
    messages: list,
    composite: Any,
    *,
    outcomes: dict | None = None,
    hitl_enabled: bool = False,
    hitl_off_outcome: str | None = None,
) -> list[dict]:
    """The tool calls a run's transcript made, for ``AssignmentRun.actions_taken``.

    Each carries the ``source`` label from the tool definition so downstream
    analytics can attribute it to the originating entity (custom tool, MCP
    server, integration, built-in, ...) without name heuristics; the lookup is
    sanitised-name-aware because the messages carry the names the LLM saw. A
    call the tool-call budget cut in strict mode never ran and is not
    an action: its answer is tagged ``not_run="budget"``, read per turn.

    ``gate_outcome`` says why each call ran without asking, or what the human
    decided (FR-020), shared with the chat path via ``resolve_log_outcome``.
    """
    from app.services.tool_tracking import _budget_cut_call_ids, _build_tool_lookup
    from app.tools.approval import resolve_log_outcome

    source_lookup = _build_tool_lookup(composite)
    actions: list[dict] = []
    for index, m in enumerate(messages):
        if m.role != "assistant" or not m.tool_calls:
            continue
        cut = _budget_cut_call_ids(messages, index)
        for tc in m.tool_calls:
            if tc.id in cut:
                continue
            entry = source_lookup.get(tc.name)
            actions.append(
                {
                    "tool": tc.name,
                    "arguments": tc.arguments or {},
                    "source": entry[0] if entry else composite.get_tool_source(tc.name),
                    "gate_outcome": resolve_log_outcome(
                        tc.name,
                        tc.id,
                        outcomes=outcomes or {},
                        hitl_enabled=hitl_enabled,
                        **({"hitl_off_outcome": hitl_off_outcome} if hitl_off_outcome else {}),
                    ),
                }
            )
    return actions


async def _resolve_chain_agent(db: AsyncSession, agent: Agent, next_assign: Assignment) -> Agent | None:
    """The agent the next assignment of a chain runs as, or ``None`` to skip it.

    ``trigger_assignment`` decides the run's enabled gate, visibility and
    deadline from the agent it is given, so a chain into another agent's
    assignment must hand it that agent, not the one whose run just finished.
    When the next assignment is on another agent and that agent is
    missing or disabled, the chain is skipped like a disabled next assignment.
    A same-agent chain gets the finished run's own agent object untouched, so
    it behaves exactly as before this fix.
    """
    if next_assign.agent_id == agent.id:
        return agent
    next_agent = (await db.execute(select(Agent).where(Agent.id == next_assign.agent_id))).scalar_one_or_none()
    if next_agent is None or not next_agent.enabled:
        logger.info(
            "Chain to '%s' not dispatched: its agent is %s",
            next_assign.name,
            "missing" if next_agent is None else "disabled",
        )
        return None
    return next_agent


_ACTIVITY_CONTINUATION_REFUSED = "assignment_continuation_refused"


async def _report_refused_continuation(
    *,
    kind: Literal["chain", "retry"],
    assignment: Assignment,
    agent: Agent,
    identity: _uuid.UUID | None,
    next_assign: Assignment | None = None,
) -> None:
    """Say that a continuation of a run of *assignment* was not started because
    the user it would run as (*identity*) cannot open that run: a chain
    into *next_assign*, or a retry. It never stops silently: a WARNING, a system
    audit entry and a notice to the assignment's owner alone (the one who can
    fix it), company-managed or not. None of them names the run or carries its
    result: the owner may not open the run. Best-effort, on a session of its
    own: the caller's still carries the finished run."""
    from app.api.v1.helpers import CONTINUED_RUN_NOT_OPENABLE_CODE
    from app.services.audit import log_system_audit

    details = {"code": CONTINUED_RUN_NOT_OPENABLE_CODE, "kind": kind, "assignment_id": str(assignment.id)}
    if kind == "chain":
        details["next_assignment_id"] = str(next_assign.id)
        logger.warning(
            "Chain from assignment %s to assignment %s not dispatched: user %s, whom the next one runs as, "
            "cannot open the run whose result it would carry",
            assignment.id,
            next_assign.id,
            identity,
        )
        title = f"Chain from '{assignment.name}' not started"
        summary = (
            f"The chain from '{assignment.name}' to '{next_assign.name}' was not "
            f"started: '{next_assign.name}' runs as a user who cannot open the run "
            "it would continue, and the chained run would carry that run's result. "
            "Make it run as an active user who can open that run (for a private "
            "run: its own user or an administrator) to chain again."
        )
    else:
        logger.warning(
            "Retry of assignment %s not started: user %s, whom its failed run ran as, is deactivated or gone",
            assignment.id,
            identity,
        )
        title = f"Retry of '{assignment.name}' not started"
        summary = (
            f"A failed run of '{assignment.name}' was not retried: the user it "
            "ran as is deactivated or no longer exists (or it ran as no one "
            "who can open it), and the retry would run as that user. Make the "
            "assignment run as an active user who can open its runs, or "
            "reactivate that user."
        )
    if assignment.user_id is None:
        logger.warning("Refused %s of assignment %s: no owner to tell", kind, assignment.id)
        return
    try:
        async with async_session() as own_db:
            await log_system_audit(
                own_db,
                actor=_STALE_REFERENCE_AUDIT_ACTOR,
                anchor_user_id=assignment.user_id,
                action="continuation_refused",
                entity_type="assignment",
                entity_id=assignment.id,
                entity_name=assignment.name,
                summary=summary,
                details=details,
            )
            await _emit_assignment_notice(
                own_db,
                assignment=assignment,
                agent=agent,
                activity_type=_ACTIVITY_CONTINUATION_REFUSED,
                title=title,
                summary=summary,
                extra_data=details,
                only_to=assignment.user_id,
            )
            await own_db.commit()
    except Exception:
        logger.warning("Could not report the refused %s of assignment %s", kind, assignment.id, exc_info=True)


@company_managed_run_scoped
@known_secrets_scoped("run_id")
async def execute_assignment_run(
    run_id: str,
    *,
    hold: bool = False,
    verdict: AgenticVerdict | None = None,
    cancel_event: asyncio.Event | None = None,
    deadline_at: datetime | None = None,
    attempt: int | None = None,
    taken_back_on_its_word: bool = False,
) -> str | RunHeld | None:
    """Execute an assignment run (called directly or after delay).

    Returns :data:`RUN_DEFERRED` when the run was left untouched for the
    orchestration engine to retry, a :class:`RunHeld` when ``hold`` is set and
    agentic AI is unavailable (the orchestration engine holds it),
    ``None`` otherwise. Without ``hold`` (no engine to hold it, or its hold is
    over) such a run is failed with the reason. ``verdict`` is the background
    gate's reading the caller already acted on (a held execution's re-claim):
    the run decides on it rather than on a second reading.

    ``cancel_event`` is the reconciler's fast cancel signal: fired when the
    run's execution is cancelled out of band (emergency stop, operator cancel)
    so the agent loop aborts between iterations instead of at the lease
    heartbeat or the end of the run. ``None`` keeps the legacy behaviour.

    ``deadline_at`` is the execution's orchestration deadline: the
    loop's bound never runs past it, so a run re-executed after a worker
    restart winds down inside what is left instead of being dead-lettered
    mid-work. The per-tool cap and the budget spawned workers inherit stay
    those of the whole run.

    ``attempt`` is the orchestration attempt running the run: its
    charge is recorded with it, and it credits back the charge of an attempt
    before it that the engine lost (``attempt_charges``).

    ``taken_back_on_its_word``: the execution was taken back from a worker
    that said it was stopping or that a deploy replaced, so a resume is
    graceful (read by the adapter).
    """
    async with async_session() as db:
        # Load the run
        result = await db.execute(
            select(AssignmentRun).where(AssignmentRun.id == _uuid.UUID(run_id))
        )
        run = result.scalar_one_or_none()
        if not run:
            logger.error("AssignmentRun %s not found", run_id)
            return

        # ── Terminal-state guard ─────────────────────────────────────────
        # A finished run must never be re-executed: a lease reclaim, a
        # mistaken requeue, or a duplicate orchestration dispatch would
        # otherwise flip the run back to "running" and replay its side
        # effects (emails sent twice, tickets created twice). Return early
        # WITHOUT touching the run. "awaiting_human" is intentionally not
        # terminal — HITL resume re-enters this function on a paused run —
        # and retry-on-failure always creates a NEW run, so neither path is
        # affected by this guard.
        if run.status in TERMINAL_RUN_STATUSES:
            logger.warning(
                "AssignmentRun %s is already in terminal state '%s' — "
                "refusing to re-execute (duplicate dispatch or stale requeue)",
                run_id,
                run.status,
            )
            return
        # Left running by an attempt the engine lost.
        reclaimed = run.status == "running"

        # ── The charge of an attempt the engine lost ─────────────
        # A run left recording an earlier attempt's charge was re-dispatched
        # after the engine lost that attempt's lease. This attempt is charged
        # for the run, so that charge goes back - free, as the dead-letter it
        # replaces would have credited it, and before anything can end this
        # attempt. Taken off the row first, so neither that attempt (it may
        # still be running) nor the dead-letter credits it again. This
        # attempt's place is recorded before that, so the attempt it replaces
        # cannot record a charge of its own afterwards.
        charge_id = new_charge_id()
        attempt_place = await fence_attempt(db, run, attempt=attempt)
        charge_epoch = attempt_place[0] if attempt_place is not None else 0
        # A run handed back by its stopping worker is resumed at the attempt
        # it was handed back at (the deferral gives it back): the charge
        # that attempt left recorded is the lost one.
        lost_charge = await take_lost_charge(
            db,
            run,
            attempt=attempt,
            epoch=charge_epoch,
            handed_back_at=assignment_run_resume.handed_back_attempt(run.trigger_context),
        )
        # The record stays on the run, marked settling, until the credit
        # landed: a credit that fails, or a worker that dies here,
        # leaves it to the dead letter. While it stays, this attempt runs
        # uncharged (``claim_charge_record`` finds it), so the run is
        # charged once either way. The credit and the charge's receipt move
        # in one Redis script, so a worker that dies after it landed and
        # before the record is settled is never credited again by the dead
        # letter, and a credit that did not land moved nothing.
        if (
            lost_charge is not None
            and lost_charge.charge_id is not None
            and await credit_recorded_charge(
                run,
                lost_charge,
                db=db,
                reason=f"assignment run {run.id}: attempt lost by the orchestration engine",
                cause=FailureCause.ENGINE_ABANDONED.value,
            )
        ):
            await settle_lost_charge(db, run, charge_id=lost_charge.charge_id)
        replaceable_charge = replaceable_charge_id(run)

        # If another writer ended the run since it was loaded, or a later
        # attempt replaced this one, this attempt does nothing. Every
        # write before the loop below - an ending, a hold with its trigger
        # context and status, the charge record, or "running" - is made under
        # the lock this read takes, held until this attempt's first commit: a
        # first attempt too, since a re-run can replace it while it is still
        # before that commit.
        if attempt is not None and await _ended_elsewhere(
            db, run, place=attempt_place, starting=True
        ):
            return

        # Load the assignment
        assign_result = await db.execute(
            select(Assignment).where(Assignment.id == run.assignment_id)
        )
        assignment = assign_result.scalar_one_or_none()
        if not assignment:
            run.status = "failed"
            run.error = "Assignment not found"
            _record_failure_cause(run, FailureCause.ASSIGNMENT_UNAVAILABLE, "not_found")
            await db.commit()
            # Refused before it executed: no ``run.failed``.
            return
        if not assignment.enabled:
            run.status = "failed"
            run.error = "Assignment is disabled"
            _record_failure_cause(run, FailureCause.ASSIGNMENT_UNAVAILABLE, "disabled")
            await db.commit()
            # Refused before it executed: no ``run.failed``.
            return
        set_posting_assignment(getattr(assignment, "id", None))
        # The inbound chat messages this run answers: its replies in their
        # conversation claim them, a second answer is refused.
        chat_reply_guard.bind_turn(
            getattr(run, "trigger_payload", None),
            scope=getattr(assignment, "id", None),
            run_id=getattr(run, "id", None),
        )

        # Load the agent
        agent_result = await db.execute(select(Agent).where(Agent.id == run.agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent or not agent.enabled:
            run.status = "failed"
            run.error = "Agent not found or disabled"
            _record_failure_cause(
                run,
                FailureCause.AGENT_UNAVAILABLE,
                "not_found" if not agent else "disabled",
            )
            await db.commit()
            # Refused before it executed: no ``run.failed``.
            return

        # ── Resume of an attempt lost to a rollout ───────────────
        # A run left running, or handed back by its stopping worker, is
        # resumed from its checkpoint: after a loss without a word at most
        # ``MAX_RESUMES`` times in a row with no tool call completed between
        # them and ``MAX_LOST_RESUMES`` times in all, after a
        # hand-back or a take-back on its worker's word ``MAX_GRACEFUL_STREAK``
        # times in a row with no progress and ``MAX_GRACEFUL_RESUMES`` in all:
        # the next loss past its bound fails it with its
        # reason, before it is charged, and its work item goes back to the
        # queue. The checkpoint is read first: its count of completed calls
        # is the progress the streak reads.
        _resuming = run.status != "awaiting_human" and assignment_run_resume.resuming(
            run.trigger_context, reclaimed=reclaimed
        )
        _run_replay = None
        if _resuming:
            from app.services import schedule_overlap

            _run_replay = await assignment_run_resume.load_replay(run.id, user_id=getattr(run, "user_id", None))
            # The timeout bounds the run's wall time across its resumes, from
            # its first ``started_at``; a run a fire of its schedule
            # waits behind gets a shorter graceful streak. A run that never
            # started has no wall time to bound: nothing is resolved for it.
            _wall_refused = None
            if run.started_at is not None:
                _wall_ts = assignment.timeout_seconds
                _wall_timeout = (
                    None
                    if (_wall_ts is not None and _wall_ts <= 0)
                    else _apply_iteration_timeout_floor(
                        float(_wall_ts or _UNSET_TIMEOUT_FALLBACK_SECONDS),
                        await resolve_run_max_iterations(db, assignment, agent),
                        run_id,
                        _wall_ts,
                    )
                )
                _wall_refused = schedule_overlap.wall_time_refusal(run, effective_timeout=_wall_timeout)
            _cap_reached = _wall_refused or assignment_run_resume.resume_refused(
                run.trigger_context,
                attempt=attempt,
                completed_calls=_run_replay.completed_calls,
                taken_back_on_its_word=taken_back_on_its_word,
                graceful_streak_bound=schedule_overlap.graceful_streak_bound(assignment, run),
            )
            if _cap_reached:
                run.status = "failed"
                run.error = _cap_reached
                run.completed_at = datetime.now(UTC)
                _record_failure_cause(
                    run, FailureCause.ENGINE_ABANDONED, "wall_time" if _wall_refused else "resume_cap"
                )
                await db.commit()
                logger.warning("Assignment run %s failed at attempt %s: %s", run_id, attempt, _cap_reached)
                await _release_run_work_item(run, agent, failed=True, error=_cap_reached)
                # No attempt will collect what its partners still work on: the
                # delegation rows are concluded and committed.
                await assignment_run_resume.cancel_partner_tasks(db, run.id)
                await db.commit()
                await assignment_run_resume.discard(run.id, place=attempt_place)
                return

        # A company-managed assignment run with no user keeps the tier its
        # automation always had in every read that has no account to go by.
        mark_company_managed_run(assignment, run)
        # A run of a Platform Health fleet agent acts as the Platform Health identity (#8254).
        await mark_health_run(db, agent, run.user_id)

        # ── Owner deactivated ────────────────────────────────────
        # A run that existed before its owner's account was deactivated (a
        # delayed run, a promoted webhook delivery, a scheduled retry or chain
        # link, a resume) is ended here, before anything runs or is charged.
        # Skipped, not failed: nothing broke, and a failure would notify, feed
        # the failure pool and count toward failure streaks. The assignment is
        # left exactly as it is.
        if await is_paused_by_deactivated_owner(db, assignment):
            run.status = "skipped"
            # ``started_at`` is only ever set below: a run carrying it already
            # did part of its work before this re-entry.
            run.error = (
                OWNER_DEACTIVATED_RESUME_ERROR
                if run.started_at is not None
                else OWNER_DEACTIVATED_RUN_ERROR
            )
            run.completed_at = datetime.now(UTC)
            await db.commit()
            # An inbox run holds its item's claim, and the reaper never
            # recovers a claimed item: hand it back, no attempt counted.
            await _release_run_work_item(run, agent, failed=False)
            logger.info(
                "Assignment run %s skipped: the owner of assignment '%s' (%s) "
                "is deactivated",
                run_id,
                assignment.name,
                assignment.id,
            )
            return

        # ── May agentic AI run here at all? ───────────────
        # Defence in depth behind the trigger-time gate: a run queued or
        # delayed before AI was switched off, or before the deployment dropped
        # to Community Edition, has not started - it is a new execution, not
        # work in flight (FR-013). It never runs a model and is never charged
        # while agentic AI is unavailable: it is HELD - left as it is, carrying
        # the hold marker - and runs by itself once AI is available again. A
        # HITL resume re-enters here too: the answer stays recorded and the
        # resumed run is held the same way. Without ``hold`` (its hold is over,
        # or no engine to hold it) it is failed with the reason and its cause.
        # A refusal nothing was read for (a database blip on a cold worker) is
        # not a reason to fail the run either: it is left exactly as it is for
        # the engine to retry. A re-check of a run held for the quota store
        # hands in that refusal while the store is still out.
        from app.services.agentic_gate import agentic_verdict, group_verdict

        if verdict is None:
            verdict = await agentic_verdict(db)
            if verdict.refusal is None:
                # For the person the run acts as: a group of theirs may switch
                # AI off (``group_ai_access``).
                verdict = await group_verdict(db, run.user_id)
        if verdict.transient:
            logger.info(
                "Assignment run %s not started: could not confirm that agentic "
                "AI may run (%s); the orchestration engine retries it",
                run_id,
                verdict.refusal,
            )
            return RUN_DEFERRED
        refusal = verdict.refusal
        if refusal is not None:
            from app.services import ai_hold

            was_held = ai_hold.held_reason_of(run.trigger_context) is not None
            if hold:
                changed = _unstart_reclaimed_run(run, reclaimed)
                marker = (run.trigger_context or {}).get(ai_hold.HOLD_MARKER_KEY)
                if not isinstance(marker, dict) or marker.get("reason") != refusal:
                    run.trigger_context = ai_hold.with_hold_marker(
                        run.trigger_context, refusal
                    )
                    changed = True
                # The deferral restores the attempt count.
                deferred = fence_for_deferral(run.trigger_context)
                if deferred is not None:
                    run.trigger_context = deferred
                    changed = True
                if changed:
                    await db.commit()
                logger.info("Assignment run %s held (%s)", run_id, refusal)
                return RunHeld(refusal)
            run.status = "failed"
            run.error = ai_hold.expired_message(refusal, was_held=was_held)
            run.completed_at = datetime.now(UTC)
            _record_failure_cause(run, ai_hold.failure_cause_of(refusal))
            await db.commit()
            # Refused before it executed: no ``run.failed``.
            logger.info(
                "Assignment run %s refused before it started (%s)", run_id, refusal
            )
            return
        from app.services import ai_hold

        quota_code = ai_hold.QUOTA_STORE_UNAVAILABLE_CODE
        run_context = getattr(run, "trigger_context", None)
        if ai_hold.held_code_of(run_context) != quota_code:
            cleared = ai_hold.without_hold_marker(run_context)
            if cleared is not None:
                # Held until now: AI is available again, the run goes ahead.
                run.trigger_context = cleared

        if deadline_spent(deadline_at):
            # Nothing of the budget is left: the run records its own
            # timeout before any setup and before it is charged, instead of
            # running a setup it cannot use until the sweep ends it.
            run.status = "failed"
            run.error = deadline_spent_message("Assignment run", deadline_at)
            run.completed_at = datetime.now(UTC)
            _record_failure_cause(run, FailureCause.ENGINE_DEADLINE_EXCEEDED)
            await db.commit()
            await _publish_run_failed(run, run.error)
            await _release_run_work_item(run, agent, failed=False)
            await _publish_run_status(
                str(run.user_id),
                str(run.id),
                "failed",
                assignment.name,
                run.error,
                is_company=run.is_company_managed,
            )
            logger.warning(
                "Assignment run %s not started: its deadline %s left none of "
                "its budget",
                run_id,
                deadline_at,
            )
            return

        # ── Agentic quota enforcement ────────────────────────────────────
        # Seeded platform automations (built-in agents' scheduled per-user
        # fan-out + the one-time first auto-population of a seeded dashboard)
        # are NOT user-initiated and are exempt from the agentic quota — both
        # the check and the increment are skipped. Manual "Trigger" /
        # "Regenerate" clicks stay metered. See _run_is_quota_exempt (the
        # exemption is hard-gated on the server-only agent.is_builtin flag).
        # A run with no user to attribute it to (a legacy company-managed row
        # from before an assignment always carried its owner) is metered too,
        # against the platform-wide tiers alone.
        effective_user_id = _acting_identity(run.user_id, agent)
        # Every AI call of the run is for that person, those with no user of
        # their own included (memory, compaction, embeddings): their LLM
        # provider exclusions hold. The adapter scopes the binding.
        bind_acting_user(effective_user_id)

        # ── Tool-capable gate (activation_check) ─────────────────────────
        # Before the charge, the model and the LLM gate: a check that fails
        # ends the run skipped with no token spent. Evaluated once per run, at
        # dispatch: a HITL resume and a resume from the checkpoint (a worker
        # stop, a rollout) are past it, so a run never reads the lease it took
        # itself as a live holder and skips with its work undone (#8831).
        if _gate_check_due(run, resuming=_resuming) and await _skip_by_activation_check(
            db,
            run=run,
            agent=agent,
            assignment=assignment,
            effective_user_id=effective_user_id,
        ):
            return
        # Outcome of the quota enforcement, attached to the workflow trace
        # metadata below so the Trace Explorer can show the platform-action
        # consumption of this run.
        _quota_trace_meta = {
            "quota_actions_consumed": 0,
            "quota_reason": "exempt_builtin",
        }
        charged = False
        # The Platform Health module checkers run outside the agentic quota under
        # their own ceiling (#101 S14b and S15, dev-docs/platform-health.md 12.5, 13.1):
        # decided from the seeded rows alone, never from the trigger or the payload.
        from app.services.health.module_analyst_budget import is_module_checker_run

        _module_analyst_exempt = is_module_checker_run(agent, assignment)
        if _module_analyst_exempt:
            _quota_trace_meta["quota_reason"] = "exempt_module_analyst"
        if not _module_analyst_exempt and not _run_is_quota_exempt(agent, run):
            charging = False
            try:
                from app.services.quota import (
                    QuotaExceededError,
                    check_agentic_quota,
                    increment_agentic_usage,
                    quota_fail_closed,
                    quota_store_outage,
                )

                if quota_fail_closed():
                    # A replica refusing writes or a store out of memory still
                    # answers the check's reads: asked for a write first, such
                    # an outage is met before anything is charged.
                    outage = await quota_store_outage()
                    if outage is not None:
                        raise outage
                await check_agentic_quota(db, effective_user_id)
                charging = True
                await increment_agentic_usage(
                    effective_user_id,
                    db=db,
                    agent_id=agent.id,
                )
                _quota_trace_meta = {
                    "quota_actions_consumed": 1,
                    "quota_reason": (
                        "charged" if effective_user_id else "charged_no_account"
                    ),
                }
                # Record the charge DURABLY, on the run itself. The failure
                # handler below can read ``_quota_trace_meta`` because it is
                # still in memory, but the orchestration engine can also give
                # up on this run from ANOTHER process entirely (lease expired,
                # worker killed, deadline blown mid-execution) — see
                # ``orchestration_adapters/assignment.on_dead_letter``. That
                # path has no in-memory state, and without this flag it cannot
                # tell "charged, then abandoned" (credit it back) from "never
                # started" (nothing to credit) or "let through unmetered by a
                # fail-open quota backend" (crediting would take an action
                # from some other run). A ``None`` user is a charge with no
                # account behind it: the platform-wide counters alone are
                # credited back. Recorded below, once the quota is settled.
                charged = True
            except QuotaExceededError as qe:
                # Refused before it executed: skipped, not failed. A
                # failure would publish ``run.failed`` and every subscriber of
                # that event would get a run of its own for each refusal - a
                # quota-exhausted agent fed by webhooks flooded the queue that
                # way. Nothing was charged, nothing ran.
                run.status = "skipped"
                run.error = f"{QUOTA_SKIP_PREFIX} {qe}"
                run.result_summary = run.error
                run.completed_at = datetime.now(UTC)
                _record_failure_cause(run, FailureCause.PLATFORM_QUOTA_EXCEEDED)
                await db.commit()
                await _release_run_work_item(run, agent, failed=False)
                await _publish_run_status(
                    str(run.user_id),
                    str(run.id),
                    "skipped",
                    assignment.name,
                    run.error,
                    is_company=run.is_company_managed,
                )
                await _notify_quota_exhausted(assignment, agent, str(qe), where="dequeue")
                logger.info("Assignment run %s skipped: %s", run_id, run.error)
                return
            except Exception as exc:
                # Quota backend failure must NOT silently grant unlimited
                # usage. ERROR-level so it surfaces in alerting (billing
                # / abuse concern). Failing closed (always in XTM One
                # mode, QUOTA_FAIL_MODE=closed in Copilot / dev mode)
                # fails the run, mirroring the quota-exceeded path;
                # failing open (the Copilot / dev default) keeps
                # assignments running through a transient Redis hiccup,
                # unmetered.
                from app.services.quota import (
                    QUOTA_UNAVAILABLE_MESSAGE,
                    is_quota_store_outage,
                    quota_fail_closed,
                )

                if quota_fail_closed():
                    failure_error = QUOTA_UNAVAILABLE_MESSAGE
                    # An unreachable store met by the check, which only reads:
                    # nothing was charged, so the run is held like a run
                    # waiting for agentic AI and charged once it runs.
                    # Met while charging, part of the charge may have landed
                    # and holding would charge it again.
                    if not charging and is_quota_store_outage(exc):
                        run_context = getattr(run, "trigger_context", None)
                        was_held = ai_hold.held_code_of(run_context) == quota_code
                        if hold:
                            changed = _unstart_reclaimed_run(run, reclaimed)
                            if not was_held:
                                run.trigger_context = ai_hold.with_hold_marker(
                                    run_context, quota_code
                                )
                                changed = True
                            # The deferral restores the attempt count.
                            deferred = fence_for_deferral(run.trigger_context)
                            if deferred is not None:
                                run.trigger_context = deferred
                                changed = True
                            if changed:
                                await db.commit()
                            logger.warning(
                                "Assignment run %s held: the quota store cannot "
                                "be reached (%s)",
                                run_id,
                                format_exception_message(exc),
                            )
                            return RunHeld(quota_code)
                        failure_error = ai_hold.expired_message(
                            quota_code, was_held=was_held
                        )
                    logger.error(
                        "Quota check failed for assignment run %s — failing "
                        "run (quotas fail closed)",
                        run_id,
                        exc_info=True,
                    )
                    run.status = "failed"
                    run.error = failure_error
                    _record_failure_cause(
                        run, FailureCause.QUOTA_BACKEND_UNAVAILABLE, "fail_closed"
                    )
                    await db.commit()
                    # Refused before it executed: no ``run.failed``.
                    return
                logger.error(
                    "Quota check failed for assignment run %s — run "
                    "proceeds without quota accounting (unmetered)",
                    run_id,
                    exc_info=True,
                )
                _quota_trace_meta = {
                    "quota_actions_consumed": 0,
                    "quota_reason": "unmetered_error",
                }

        # The record names this attempt's charge, and is not written
        # when a later attempt recorded its own since this one started: the
        # engine replaced this attempt before it was charged, so its charge
        # goes back and it runs uncharged. Either way the run's context is read
        # back from the row, locked until the "running" commit below.
        if charged and not await claim_charge_record(
            db,
            run,
            charge_id=charge_id,
            attempt=attempt,
            user_id=effective_user_id,
            expected=replaceable_charge,
            epoch=charge_epoch,
        ):
            from app.services.quota import refund_agentic_usage

            logger.info(
                "Assignment run %s: charge given back, the orchestration engine "
                "replaced this attempt before it was charged",
                run_id,
            )
            try:
                await refund_agentic_usage(effective_user_id, db=db, agent_id=agent.id)
            except Exception:
                logger.warning(
                    "Assignment run %s: could not give back the charge of a "
                    "replaced attempt - it stays charged",
                    run_id,
                    exc_info=True,
                )
            _quota_trace_meta = {
                "quota_actions_consumed": 0,
                "quota_reason": "attempt_replaced",
            }

        cleared = ai_hold.without_hold_marker(getattr(run, "trigger_context", None))
        if cleared is not None:
            # Held until now for the quota store: it answered, the run goes ahead.
            run.trigger_context = cleared

        # ── Execution telemetry ──────────────────────────────────────────
        # Counted for EVERY run that reaches execution — including seeded
        # platform automations and runs the quota let through unmetered:
        # this measures how much agentic work the install performs, not what
        # is billable. A HITL resume re-enters this function on the same run
        # to continue the paused execution, so it must not be counted twice.
        if run.status != "awaiting_human":
            await record_agentic_execution(SOURCE_ASSIGNMENT)

        # The checkpointed rounds of the attempt this one goes on from,
        # its resume recorded with the "running" commit below.
        if _resuming:
            await assignment_run_resume.prepare_resume(
                run,
                attempt=attempt,
                replay=_run_replay,
                taken_back_on_its_word=taken_back_on_its_word,
            )

        # Save original status before overwriting (needed for HITL resume)
        original_status = run.status

        # Mark as running. A resume keeps the first ``started_at``: the
        # run's wall time is read across its resumes. A HITL resume keeps it
        # too, and its pause is counted apart (#1645), never as queue time.
        from app.services import hitl_resume_clock

        _hitl_resume = original_status == "awaiting_human"
        if _hitl_resume:
            hitl_resume_clock.close_pause(run)
        run.status = "running"
        run.started_at = (run.started_at if (_resuming or _hitl_resume) else None) or datetime.now(UTC)
        await db.commit()
        await _publish_run_status(
            str(run.user_id),
            str(run.id),
            "running",
            assignment.name,
            is_company=run.is_company_managed,
        )
        await event_bus.emit(
            ASSIGNMENT_RUN_STARTED,
            {
                "run_id": run_id,
                "assignment_id": str(assignment.id),
                "assignment_name": assignment.name,
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "user_id": str(run.user_id) if run.user_id else None,
            },
        )

        # Bind structured context for all downstream log records
        structlog.contextvars.bind_contextvars(
            source="assignment",
            agent=agent.name,
            agent_id=str(agent.id),
            assignment=assignment.name,
            assignment_id=str(assignment.id),
            run_id=str(run.id),
        )

        wf_trace_ctx = None  # Initialise before try so except handler always has it
        _wf_trace_ctx_token = None  # Langfuse contextvar reset token
        mcp_manager = None  # Initialise before try so finally can always clean up
        # This attempt checked that no other ending landed and set its own:
        # an ending read back from the row after that is its own.
        own_ending = False
        # Its completed tool rounds, checkpointed at every iteration boundary
        # for the attempt that resumes it after a rollout.
        _run_checkpoint = None
        # Handed back by its stopping worker: not over, resumed elsewhere.
        _handed_back = False
        # Replaced by a later attempt after its lease was lost: the run
        # is that attempt's, partner work included.
        _replaced = False
        messages: list = (
            []
        )  # Initialise early so the except handler can always access it
        # ── Failure-attribution signals ──────────────────
        # Did this run get as far as having a model to call? Everything that
        # fails at or before LLM resolution — no provider configured, a
        # credential that cannot be decrypted, a broken integration binding,
        # variable resolution — happened without a single model call, so the
        # action goes back FREE rather than out of the agent's failure pool.
        # A flag beats classifying the exception: "we never reached a model" is
        # a fact about where we are in the run, not a string in an error message.
        #
        # ``_llm`` keeps the provider itself, not just the boolean, because
        # ``LLMProvider.last_usage`` — proxied through the failover wrapper —
        # is the platform's only honest answer to the NEXT question, "did any
        # model call in this run actually return". The except handler is the
        # one place that can still ask. Recorded by the classifier, which
        # changes no outcome.
        _llm = None
        _llm_resolved = False
        # Read in the ``finally`` below, so bound before anything can raise.
        integ_exec = None
        # A stale integration reference fails a run whose loop RETURNED:
        # its usage is still the run's, and the streak is read once
        # the failure is committed.
        _stale_failure: StaleIntegrationReferenceError | None = None
        _finished_loop = None

        try:

            # ── Resolve platform variables ────────────────────────────
            from app.api.v1.helpers import get_user_group_ids as _get_groups
            from app.services.variable_resolver import (
                load_variables,
                substitute_variables,
            )

            _var_groups = (
                await _get_groups(db, effective_user_id) if effective_user_id else []
            )
            _variables = await load_variables(db, effective_user_id, _var_groups)

            # Build prompt context
            prompt_context = {
                "trigger": run.trigger_payload or {},
                "agent": {
                    "name": agent.name,
                    "persona": agent.persona or "",
                    "config": agent.config or {},
                },
            }

            # Build element binding descriptions for the prompt context.
            # Merge agent-level and assignment-level element bindings:
            #  - Agent defines the pool of available elements
            #  - Assignment refines (narrows) from the agent's selection
            #  - If assignment has no bindings → use agent bindings (all agent elements)
            #  - If agent has no bindings → use assignment bindings (all integration elements)
            # A type the agent OR the assignment enforces is also a boundary on
            # the tools and on every handover: the stricter wins.
            _run_scope = assignment_run_scope(
                agent.integration_element_bindings,
                agent.integration_element_enforcement,
                assignment.integration_element_bindings,
                assignment.integration_element_enforcement,
            )
            element_bindings = _run_scope.prompt
            enforced_scope = _run_scope.enforced
            prompt_context["elements"] = _build_elements_context(element_bindings)

            # ── Resolve the workflow LLM up-front ────────────────────────
            # Done before the trace is opened so the resolved model can be
            # passed to ``trace_llm_call`` (which uses it to set the
            # ``model:<x>`` Langfuse tag — tags are immutable post-creation).
            await ensure_hot_path_imports(
                "app.llm.router", "app.services.integration_tools"
            )
            from app.llm.agent_model import agent_model_selection
            from app.llm.router import get_llm_for_user

            _agent_model = await agent_model_selection(db, agent)
            llm, wf_resolved_model = await get_llm_for_user(
                db,
                str(run.user_id) if run.user_id else None,
                provider_override=_agent_model.provider,
                model_override=_agent_model.model,
                provider_id_override=_agent_model.provider_id,
            )
            # From here on a model exists to call, so a failure can no longer
            # be attributed to "there was nothing to call".
            _llm = llm
            _llm_resolved = True

            # ── Langfuse: open the workflow root trace early ─────────────
            # Created BEFORE the activation gate so the gate (and any
            # consult/transfer it might trigger) nest under the same root.
            # Input is a minimal placeholder here; enriched with the full
            # messages/system_prompt/tools payload once those are assembled
            # below via ``enrich_trace_input`` and ``enrich_trace_metadata``.
            from app.services.langfuse_tracing import (
                build_trace_input,
                end_trace,
                enrich_trace_input,
                enrich_trace_metadata,
                serialize_file_refs,
                set_current_trace_ctx,
                trace_llm_call,
            )

            _event_latency = _run_event_latency(
                run, original_status, trigger_type=getattr(assignment, "trigger_type", None)
            )
            wf_trace_ctx = await trace_llm_call(
                db,
                name=f"assignment-run:{agent.slug or agent.name}",
                user_id=str(run.user_id) if run.user_id else "system",
                source=agent.slug or agent.name,
                model=wf_resolved_model,
                input_data=build_trace_input(
                    extra={
                        "assignment": assignment.name,
                        "agent": agent.name,
                        "trigger_payload": run.trigger_payload or {},
                    }
                ),
                agent_id=str(agent.id),
                agent_name=agent.name,
                agent_slug=agent.slug,
                assignment_id=str(assignment.id),
                assignment_name=assignment.name,
                run_id=run_id,
                trigger_type=assignment.trigger_type,
                metadata={
                    "provider": getattr(llm, "provider_name", None),
                    "llm_provider": getattr(llm, "provider_name", None),
                    **provider_config_metadata(llm),
                    "agent_template": agent.template_slug,
                    "is_company_managed": agent.is_company_managed,
                    "dry_run": assignment.dry_run,
                    # The link from the attempt lost to this one.
                    **(assignment_run_resume.resumed_trace_meta(run.id, run.trigger_context) or {}),
                    "delay": assignment.delay,
                    "human_in_the_loop": assignment.human_in_the_loop,
                    "tool_deny_list": agent.tool_deny_list,
                    "use_knowledge_bases": assignment.use_knowledge_bases,
                    **_quota_trace_meta,
                    # Event origin to run start: timestamps only.
                    **({"event_latency": _event_latency} if _event_latency else {}),
                },
            )
            _wf_trace_ctx_token = (
                set_current_trace_ctx(wf_trace_ctx) if wf_trace_ctx else None
            )

            # Trigger context + payload block shared by the activation gate and
            # the main agentic prompt. Built once from the run so both paths see
            # the same incoming-event data even when the prompt templates don't
            # reference {{trigger.*}}.
            trigger_information = _build_trigger_information(run)

            # When the trigger is externally-sourced (webhook / integration_event)
            # any {{trigger.*}} substituted into a prompt must be quarantined so it
            # can't inject instructions into the trusted prompt text.
            from app.services.assignment_helpers import _UNTRUSTED_TRIGGER_TYPES

            _trigger_type_for_render = (run.trigger_context or {}).get("type")
            _untrusted_trigger_source = (
                f"{_trigger_type_for_render} trigger"
                if _trigger_type_for_render in _UNTRUSTED_TRIGGER_TYPES
                else None
            )

            # --- Activation check (optional gate) ---
            # Whitespace-only gates count as "no gate": legacy rows written
            # before the API normalized blank input would otherwise trigger a
            # real gate LLM call with a blank question, whose answer decides
            # whether the assignment runs at all.
            if (assignment.activation_prompt or "").strip():
                await _warn_unresolved_placeholders(
                    db,
                    substitute_variables(assignment.activation_prompt, _variables),
                    where="activation prompt",
                    agent_name=agent.name,
                    assignment_name=assignment.name,
                    assignment_id=assignment.id,
                    resolved_as=effective_user_id,
                    rendered_roots=prompt_context.keys(),
                )
                rendered_activation = _render_prompt(
                    assignment.activation_prompt,
                    prompt_context,
                    _variables,
                    untrusted_trigger_source=_untrusted_trigger_source,
                )
                # Feed the gate the same trigger context/payload the run would
                # see. Without it, an activation prompt that doesn't explicitly
                # template {{trigger.*}} leaves the gate blind to the event it
                # is gating — it then skips every run with answers like
                # "SKIP: NO ISSUE CONTENT PROVIDED".
                if trigger_information:
                    rendered_activation += trigger_information
                answer = await _run_activation_gate(
                    db,
                    agent=agent,
                    user_id=str(run.user_id) if run.user_id else None,
                    rendered_activation=rendered_activation,
                    parent_trace_ctx=wf_trace_ctx,
                )
                if answer.startswith("NO") or answer.startswith("SKIP"):
                    # Taken over while the gate ran: no ending from this attempt.
                    await ensure_single_writer()
                    run.status = "completed"
                    # Persist the trace id so the gate-skipped run (which
                    # never reaches the main completion path) still deep-links
                    # to its Langfuse trace (no loop usage: the agent loop
                    # never ran).
                    apply_run_usage_accounting(run, None, wf_trace_ctx)
                    run.result_summary = _format_skip_summary(answer)
                    # Persist the activation-gate exchange so the run's "Full
                    # Rendered Prompt" panel (rebuilt from run.messages in the
                    # run-detail endpoint) shows what was actually sent to the
                    # LLM. A gated run never reaches the main agentic loop, so
                    # run.messages would otherwise stay NULL and the panel would
                    # read "No rendered prompt available for this run." even
                    # though the gate prompt WAS evaluated. Storing it here also
                    # surfaces the gate's YES/NO/SKIP answer as an agentic step.
                    run.messages = [
                        {"role": "user", "content": rendered_activation},
                        {"role": "assistant", "content": answer},
                    ]
                    run.completed_at = datetime.now(UTC)
                    await db.commit()
                    await _publish_run_status(
                        str(run.user_id),
                        str(run.id),
                        "skipped",
                        assignment.name,
                        run.result_summary,
                        is_company=run.is_company_managed,
                    )
                    # Close the workflow root trace explicitly — the early
                    # ``return`` bypasses the outer try's normal completion
                    # path, but the ``finally`` block still resets the
                    # contextvar.
                    if wf_trace_ctx is not None:
                        end_trace(
                            wf_trace_ctx, output="Skipped by activation gate"
                        )
                    logger.info(
                        "Assignment '%s' skipped by activation prompt", assignment.name
                    )
                    return

            # Render the main prompt
            rendered_prompt = _render_prompt(
                assignment.prompt,
                prompt_context,
                _variables,
                untrusted_trigger_source=_untrusted_trigger_source,
            )
            # Scan the template, not its rendering: a ``{{...}}`` inside a
            # rendered trigger value is data, not an unresolved placeholder.
            await _warn_unresolved_placeholders(
                db,
                substitute_variables(assignment.prompt, _variables),
                where="assignment prompt",
                agent_name=agent.name,
                assignment_name=assignment.name,
                assignment_id=assignment.id,
                resolved_as=effective_user_id,
                rendered_roots=prompt_context.keys(),
            )

            # Always append trigger metadata and payload so the LLM has full
            # context about how and why this assignment was triggered, even when
            # the prompt doesn't use {{trigger.*}} template variables. Reuses the
            # same block fed to the activation gate above (built once from run).
            if trigger_information:
                rendered_prompt += trigger_information

            # ── Follow-up override ──
            # When a run is a follow-up of a previous run, replace the
            # rendered prompt entirely so the agent gets the parent result
            # and the user's follow-up instruction instead of the assignment's
            # original template (which would have empty trigger variables).
            if (run.trigger_payload or {}).get("trigger_type") == "followup":
                parent_result = run.trigger_payload.get("parent_result", "")
                followup_prompt = run.trigger_payload.get("followup_prompt", "")
                rendered_prompt = (
                    "You previously completed a task with this result:\n\n"
                    f"{parent_result}\n\n"
                    "The user has a follow-up request:\n\n"
                    f"{followup_prompt}\n\n"
                    "Use your available tools to fulfill this follow-up request."
                )

            # The run's own identity (run_id, assignment_id, trigger), so what
            # the run records about itself names it by the id get_run reads.
            from app.services.run_context import build_run_context

            rendered_prompt += build_run_context(run)

            # A fleet member's run starts from the organization profile (filled
            # fields quarantined, missing ones named; #8987 slice 3b). Never
            # on an allowlisted automation of 1.261102.0, nor on a run of a
            # pre-existing member the fleet did not start (#9188).
            from app.services.fleet_runs import assignment_fleet_context, parent_run_agent_id
            from app.services.organization_context_injection import (
                assignment_run_organization_context,
            )
            from app.services.organization_events import organization_ids_of

            _run_fleet = await assignment_fleet_context(
                db,
                agent,
                assignment,
                await parent_run_agent_id(db, getattr(run, "parent_run_id", None)),
            )
            # One cascade decides whether the run carries the organization
            # at all (#11533; assignment > agent > platform). Resolved on, a
            # fleet run keeps its fuller block and the system prompt's
            # summary is left out, so the profile appears once.
            _organization_block, _organization_summary = await assignment_run_organization_context(
                db,
                agent=agent,
                assignment=assignment,
                fleet=_run_fleet,
                organization_ids=organization_ids_of(run.trigger_payload),
            )
            rendered_prompt += _organization_block

            # The fleet's autonomy level and which of its declared gates wait
            # at that level (#9769 S1b); the gates that name tools are also
            # enforced in the tool path below, whatever the model decides.
            from app.services.fleet_autonomy import (
                fleet_autonomy_block,
                fleet_autonomy_for_run,
                fleet_enforces_at_tool_level,
                fleet_instructions_for_run,
            )

            _fleet_autonomy = await fleet_autonomy_for_run(db, getattr(_run_fleet, "fleet_id", None))
            # Tool-level pauses only on an administrator's opt-in (#1467): by
            # default the gates are instructions, stated in the block below.
            _fleet_enforced = _fleet_autonomy is not None and await fleet_enforces_at_tool_level(
                db, getattr(_run_fleet, "fleet_id", None)
            )
            if _fleet_autonomy is not None:
                rendered_prompt += fleet_autonomy_block(
                    *_fleet_autonomy,
                    await fleet_instructions_for_run(db, getattr(_run_fleet, "fleet_id", None)),
                    enforced=_fleet_enforced,
                )

            # ── Memory (load enabled tiers + user memory before building prompt) ──
            agent_memory_text, user_memory_text = await _load_memory_context(
                db,
                agent,
                run.user_id,
                query=rendered_prompt,
            )

            # ── Knowledge base access (read + write, refined from agent config) ──
            from app.services.builtin_tools import (
                format_coordination_tools_prompt,
                format_knowledge_base_prompt,
                load_kb_names,
                load_write_locked_kb_ids,
            )

            read_kb_ids, write_kb_ids = resolve_assignment_kb_access(agent, assignment)
            kb_name_map = await load_kb_names(
                db, list(dict.fromkeys(read_kb_ids + write_kb_ids))
            )
            # A content-locked knowledge base (read-only ground truth,
            # installed by a skill package, built-in) leaves the WRITE
            # scope regardless of the agent's / assignment's grant: the lock
            # lives on the knowledge base so it cannot be granted away from the
            # agent side, and filtering here means the write tools are never
            # offered for it. Read scope is untouched - locked KBs stay fully
            # searchable and readable.
            _locked_kb_ids = await load_write_locked_kb_ids(db, write_kb_ids)
            if _locked_kb_ids:
                write_kb_ids = [k for k in write_kb_ids if k not in _locked_kb_ids]
                logger.info(
                    "Assignment run %s: %d writable knowledge base(s) are "
                    "content-locked and were excluded from the write scope",
                    run_id,
                    len(_locked_kb_ids),
                )
            # Fall back to the KB id string (never a constant "knowledge base")
            # when a name can't be loaded: a constant would collapse several KBs
            # to the same label, producing ambiguous names in the capability
            # prompt and in store_knowledge's writable list (which rejects
            # ambiguous names). The id keeps every entry unambiguous.
            read_kbs = [(k, kb_name_map.get(k, k)) for k in read_kb_ids]
            write_kbs = [(k, kb_name_map.get(k, k)) for k in write_kb_ids]
            kb_prompt = format_knowledge_base_prompt(read_kbs, write_kbs)

            # Coordination-tools nudge — gated on the agent having the durable
            # work-item / shared-state tools wired (assignments use the agent's
            # builtin_tool_ids directly; there is no per-run override).
            coordination_prompt = format_coordination_tools_prompt(
                agent.builtin_tool_ids or []
            )

            # Build system message with agent persona + guardrails + execution policy
            # The quarantine-marker policy is only needed when quarantined
            # content can actually appear: an untrusted trigger payload, a
            # web_fetch, document_vision or shared_state tool, or the
            # integration tools built below.
            from app.services.assignment_helpers import (
                assignment_untrusted_policy_needed,
            )

            _quarantine_possible = assignment_untrusted_policy_needed(
                agent,
                assignment,
                untrusted_trigger=_untrusted_trigger_source is not None,
            )
            # ── Agent skills (progressive disclosure) ──────────
            # Resolved here rather than inside ``_build_system_message``
            # because the same set decides whether the ``load_skill`` tool is
            # registered further down. Visibility is checked against the
            # AGENT OWNER (the identity that attached the skills), falling
            # back to ``effective_user_id`` for an owner-less agent — see
            # ``SkillAccessContext``.
            _skill_access = None
            _resolved_skills: list = []
            if agent.skill_ids:
                from app.services.builtin_tools import (
                    build_skill_access_context,
                    resolve_agent_skills,
                )

                _skill_access = await build_skill_access_context(
                    db, agent.user_id, fallback_user_id=effective_user_id
                )
                _resolved_skills = await resolve_agent_skills(
                    db,
                    agent.skill_ids,
                    access=_skill_access,
                    always_load_ids=set(agent.always_load_skill_ids),
                )

            _templated_sections: list[tuple[str, str]] = []
            system_message = await _build_system_message(
                db,
                agent,
                assignment,
                run.user_id,
                user_memory_text,
                agent_memory_text,
                element_bindings,
                variables=_variables,
                has_memory_tools=bool(agent.memory_persistent_enabled),
                knowledge_base_prompt=kb_prompt,
                coordination_tools_prompt=coordination_prompt,
                include_untrusted_policy=_quarantine_possible,
                resolved_skills=_resolved_skills,
                templated_sections=_templated_sections,
                organization_summary=_organization_summary,
            )
            for _section, _section_text in _templated_sections:
                await _warn_unresolved_placeholders(
                    db,
                    _section_text,
                    where=_section,
                    agent_name=agent.name,
                    assignment_name=assignment.name,
                    assignment_id=assignment.id,
                    resolved_as=effective_user_id,
                )

            # Execute the agentic loop
            from app.llm.base import ChatMessage
            from app.tools.agent_loop import run_agent_loop
            from app.tools.base import CompositeToolExecutor
            from app.tools.run_budget import build_run_budget_for_run

            # ── Build FileContext from trigger payload ──────────────────
            # ``file_ids`` is caller-supplied (a webhook body lands here
            # verbatim), so each id is admitted through the file access
            # contract as the run's own identity - or dropped. Only the
            # admitted ids are ``trigger_file_ids`` from here on.
            file_context = FileContext()
            trigger_file_ids: list[str] = []
            _trigger_file_rows = await load_authorized_trigger_files(
                db, run, (run.trigger_payload or {}).get("file_ids")
            )
            if _trigger_file_rows:
                from app.llm.base import FileRef

                for fa in _trigger_file_rows:
                    file_context.add(FileRef.from_attachment(fa))
                    trigger_file_ids.append(str(fa.id))
            # The files the dispatched work item's creator attached.
            for _item_ref in await load_dispatched_work_item_files(run):
                if _item_ref.file_id not in trigger_file_ids:
                    file_context.add(_item_ref)
                    trigger_file_ids.append(_item_ref.file_id)

            # Owner stamped on files this run generates (generate_file built-in
            # + custom-tool ``$output_files``) — NULL for company-managed runs so
            # a non-admin run-as / triggering user can't bypass the admin-gated
            # company-run file access via the owner shortcut in
            # ``_authorize_file_access``. See ``_resolve_run_file_owner_id``.
            _run_file_owner_id = _resolve_run_file_owner_id(run)

            # Gather tools via MCP + custom tools
            composite = CompositeToolExecutor()
            mcp_manager = None
            integration_notice = ""
            # Auth failures of the run's integration calls, and the refusal of
            # a credential already rejected in this run (#1454).
            from app.services.integration_auth_guard import AuthGuardedExecutor, IntegrationAuthGuard

            auth_guard = IntegrationAuthGuard()

            from app.services.builtin_tools import load_custom_tools, load_mcp_tools

            mcp_ids = assignment.tool_sources or agent.mcp_server_ids or []
            mcp_scope = None
            if mcp_ids:
                # Deferred like ``load_mcp_tools`` itself: ``app.tools.mcp_client``
                # pulls in the MCP SDK transports, which a run without MCP never needs.
                from app.tools.mcp_client import MCPArtifactScope

                # Same scope as this run's ``generate_file``: a binary MCP
                # result is listed on the run and restored on a HITL resume
                # (``load_run_generated_files``).
                mcp_scope = MCPArtifactScope(
                    user_id=_run_file_owner_id,
                    assignment_run_id=str(run.id),
                )
            mcp_manager = await load_mcp_tools(
                db,
                mcp_ids,
                composite,
                allowed_tools=agent.mcp_allowed_tools or {},
                artifact_scope=mcp_scope,
                # A per_user MCP server carries the account of the person the
                # run acts as, never the agent owner's lent to someone else.
                acting_user_id=effective_user_id,
            )

            # Surface MCP tool unavailability in the system prompt so the LLM
            # can acknowledge it in its output rather than silently missing data.
            # Only injected when servers actually failed — net-zero inflation on
            # normal runs.  Inserted BEFORE the security anchor (same contract
            # as the handover / self-fan-out blocks below).  ``mcp_manager is
            # None`` with servers requested is the wrapper-level failure path
            # of ``load_mcp_tools`` (ALL MCP tools missing) — surface that too.
            if mcp_ids:
                from app.services.builtin_tools import (
                    format_mcp_auth_broken_prompt,
                    format_mcp_unavailable_prompt,
                )
                from app.services.prompt_guard import insert_before_safety_anchor

                _mcp_note = (
                    format_mcp_unavailable_prompt([], all_failed=True)
                    if mcp_manager is None
                    else format_mcp_unavailable_prompt(
                        mcp_manager.failed_servers,
                        server_durations=mcp_manager.failed_server_durations,
                    )
                )
                if _mcp_note:
                    system_message = insert_before_safety_anchor(
                        system_message, _mcp_note
                    )

                # Surface servers whose credential is persistently broken
                # (cross-run Redis auth-failure counter at or above threshold)
                # so the agent does not waste iterations on known-broken tools.
                if mcp_manager is not None:
                    _auth_broken = await mcp_manager.auth_broken_servers()
                    _auth_note = format_mcp_auth_broken_prompt(_auth_broken)
                    if _auth_note:
                        system_message = insert_before_safety_anchor(
                            system_message, _auth_note
                        )

            # Custom tools – user-defined JavaScript tools bound to the agent
            if not security_option("disable_custom_tools"):
                await load_custom_tools(
                    db,
                    agent.custom_tool_ids or [],
                    composite,
                    variables=_variables,
                    user_id=_run_file_owner_id,
                    # Company-managed runs have no personal file owner
                    # (``_run_file_owner_id`` is None, by design), but the
                    # combined agentic+sandbox quota check at sandbox-dispatch
                    # time (spec 002 FR-015) still needs a user to charge —
                    # use the run's charged/owning user so those dispatches
                    # stay metered instead of silently bypassing the check.
                    quota_user_id=(
                        str(effective_user_id) if effective_user_id else None
                    ),
                    assignment_run_id=str(run.id),
                )

            # Integration tools — resolve effective bindings.
            #   assignment.integration_bindings is NULL → inherit ALL from agent
            #   assignment.integration_bindings is {}   → explicitly no integrations
            #   assignment.integration_bindings is set  → custom subset
            from app.services.integration_tools import flatten_bindings

            if assignment.integration_bindings is None:
                effective_bindings = flatten_bindings(agent.integration_bindings or {})
                binding_origin_map = {
                    key: "agent_binding_inherited" for key in effective_bindings
                }
                logger.info(
                    "Assignment %s inheriting integration bindings from agent (%d entries)",
                    assignment.name,
                    len(effective_bindings),
                )
            else:
                effective_bindings = flatten_bindings(assignment.integration_bindings)
                binding_origin_map = {
                    key: "assignment_binding_override" for key in effective_bindings
                }
                if effective_bindings:
                    logger.info(
                        "Assignment %s using custom integration bindings (%d entries)",
                        assignment.name,
                        len(effective_bindings),
                    )
                else:
                    logger.info(
                        "Assignment %s has explicitly empty integration bindings — no integration tools",
                        assignment.name,
                    )

            # Resolve per-user "template" integration bindings.
            #
            # ``integration_template_bindings`` (e.g. ["opencti"]) means
            # "resolve the right integration for this provider at run time"
            # instead of pinning a fixed integration id.  Resolution needs a
            # *user context* to look up "this user's connected integration".
            #
            # That context is ``effective_user_id`` = ``run.user_id`` falling
            # back to ``agent.user_id`` (computed above, same pattern used for
            # model/memory/quota resolution).  This matters for
            # company-managed agents:
            #
            #   • A company-managed run now ALWAYS carries an owner in
            #     ``run.user_id`` (stamped at creation: triggering user →
            #     assignment ``run_as_user_id`` → assignment owner), and a
            #     company-managed agent KEEPS ``agent.user_id`` as the original
            #     creator — visibility is driven by ``is_company_managed``, not
            #     by nulling the owner.  So we resolve that user's connected
            #     integrations, exactly like the agent did while it was private
            #     — which is why a company-managed agent with a per-user
            #     OpenCTI integration keeps its tools instead of silently
            #     degrading to memory-only.
            #
            #   • Only when there is genuinely no owner anywhere
            #     (``effective_user_id`` is ``None`` — e.g. a company-managed
            #     agent created from scratch, with no run-as and no triggering
            #     user) do we restrict to SHARED integrations
            #     (company-managed / platform-managed), since there is no owner
            #     whose private credentials could be borrowed.
            #
            # NOTE: the gate is ``if agent.integration_template_bindings`` and
            # is intentionally NOT additionally gated on ``run.user_id``.
            # Re-introducing a ``run.user_id`` gate here would resurrect the
            # bug where company-managed runs lost their per-user
            # integration tools at execution time.
            if agent.integration_template_bindings:
                from app.models.integration import Integration as _IntegModel

                _needed_provs = [
                    p
                    for p in agent.integration_template_bindings
                    if p not in effective_bindings
                ]
                if _needed_provs:
                    from app.services.integration_tools import (
                        drop_foreign_personal_integrations,
                        pick_template_integration,
                        template_candidate_filter,
                    )

                    # Resolution context: run owner, else agent creator. With no
                    # owner anywhere (a company-managed agent created from
                    # scratch) only company-wide integrations are eligible —
                    # there is no user whose private credentials could be used.
                    _ctx_user_id = effective_user_id

                    _tmpl_r = await db.execute(
                        select(_IntegModel)
                        .where(
                            _IntegModel.provider.in_(_needed_provs),
                            await template_candidate_filter(
                                db,
                                _ctx_user_id,
                                company_automation=bool(agent.is_company_managed),
                            ),
                            connected_for_clause(_ctx_user_id),
                        )
                        .order_by(
                            _IntegModel.provider,
                            _IntegModel.created_at,
                            _IntegModel.id,
                        )
                    )
                    _by_prov: dict[str, list] = {}
                    for _tmpl_i in _tmpl_r.scalars().all():
                        _by_prov.setdefault(_tmpl_i.provider, []).append(_tmpl_i)

                    for _tmpl_prov, _tmpl_integs in list(_by_prov.items()):
                        if _tmpl_prov in effective_bindings:
                            continue
                        # Defence in depth against a colleague's personal
                        # integration reaching the pick — the query
                        # already excludes them.
                        _tmpl_integs = drop_foreign_personal_integrations(
                            _tmpl_integs, _ctx_user_id, provider=_tmpl_prov
                        )
                        _by_prov[_tmpl_prov] = _tmpl_integs
                        if not _tmpl_integs:
                            del _by_prov[_tmpl_prov]
                            continue
                        if len(_tmpl_integs) > 1 and any(
                            i.scope_config for i in _tmpl_integs
                        ):
                            # Scope is defined — do NOT auto-pick. Let intent
                            # headers or trigger context drive selection
                            # downstream.
                            continue
                        # Prefer the context user's OWN integration (continuity
                        # with how the agent behaved while private), else a
                        # company-wide one. ``pick_template_integration`` also
                        # labels which of the two happened so the run's origin
                        # map records whether shared credentials were used.
                        _picked, _origin = pick_template_integration(
                            _tmpl_integs,
                            _ctx_user_id,
                            provider=_tmpl_prov,
                            context=(
                                f"assignment={assignment.name} agent={agent.name}"
                            ),
                        )
                        if _picked is not None:
                            effective_bindings[_tmpl_prov] = str(_picked.id)
                            binding_origin_map[_tmpl_prov] = _origin

                    # Visibility: surface providers for which NO eligible
                    # integration was found at all (vs. deliberately-skipped
                    # scoped providers, which are resolved downstream).  This
                    # gives the otherwise-silent "agent only used its memory
                    # tools" failure a traceable cause in the logs.
                    _unresolved = [p for p in _needed_provs if p not in _by_prov]
                    if _unresolved and _ctx_user_id:
                        logger.warning(
                            "Assignment '%s' (agent '%s'): no connected "
                            "integration found for template binding(s) %s in "
                            "the resolution context (user %s) — these "
                            "integration tools will be UNAVAILABLE for this "
                            "run.",
                            assignment.name,
                            agent.name,
                            _unresolved,
                            _ctx_user_id,
                        )
                    elif _unresolved:
                        logger.warning(
                            "Assignment '%s' (company-managed agent '%s'): no "
                            "owner (run.user_id and agent.user_id are both "
                            "NULL) and no SHARED (company-managed / "
                            "platform-managed) integration found for template "
                            "binding(s) %s — these integration tools will be "
                            "UNAVAILABLE for this run. Either set a creator on "
                            "the agent or mark the integration company-managed "
                            "so it can be resolved without a per-user context.",
                            assignment.name,
                            agent.name,
                            _unresolved,
                        )

            # The per-user (template) rows this run resolved: the tasks it spawns
            # resolve to the same rows when eligible for them (#1792).
            from app.services.integration_tools import template_picks as _template_picks

            _run_template_picks = _template_picks(effective_bindings, binding_origin_map)

            # How the run's bound platforms name the objects they hold, filled by
            # the integration build (integration_tools.PLATFORM_NAMES_CONTEXT_KEY)
            # for the grounded STIX extraction below.
            _platform_names: list = []
            # The run's bound MITRE ATT&CK sources, for the same extraction.
            _attack_sources: list = []
            if effective_bindings:
                try:
                    from app.services.assignment_helpers import resolve_hitl
                    from app.services.integration_tools import (
                        ATTACK_SOURCES_CONTEXT_KEY,
                        PLATFORM_NAMES_CONTEXT_KEY,
                        build_integration_tools,
                    )

                    # Resolved here as well as for the built-in HITL tool below,
                    # because providers need it at tool-build time: a provider
                    # that guards an irreversible action can escalate it for
                    # human approval when HITL is on instead of refusing it.
                    _integration_hitl_on, _ = resolve_hitl(agent, assignment)

                    integration_context = {
                        "source": "assignment",
                        "agent": agent.name,
                        "agent_id": str(agent.id),
                        "assignment": assignment.name,
                        "run_id": str(run.id),
                        # Assignment runs are server-side with no interactive
                        # user, so providers must not stall waiting on one.
                        # OpenCTI uses this to auto-provision its review draft
                        # rather than asking the agent which draft to use.
                        "autonomous": True,
                        "hitl_enabled": _integration_hitl_on,
                        # Use the resolution context (run owner, else agent
                        # creator) so platform-managed integrations mint
                        # their per-user JWT for the right identity even on
                        # owner-less company-managed runs.
                        "user_id": (
                            str(effective_user_id) if effective_user_id else None
                        ),
                        "binding_origin_map": binding_origin_map,
                        # The enforced part of the merged Scope(s) is a hard
                        # boundary on the providers that enforce one,
                        # not only the prompt hint above.
                        "element_scope": enforced_scope,
                    }
                    # The work item the inbox dispatched this run for: a commit
                    # the run pushes to an open pull request names it there
                    # (#9819). The dispatcher's own record only, never the payload.
                    _dispatched_item = _inbox_dispatched_work_item(run)
                    if _dispatched_item is not None:
                        integration_context["work_item_id"] = _dispatched_item[0]
                    # An orphaned binding (the agent's owner lost access to the
                    # integration) is refused before any credential is read.
                    # Only the agent's own bindings are judged: an assignment
                    # override and a template pick are not the owner's.
                    from app.services.integration_credentials import drop_orphaned_bindings

                    await drop_orphaned_bindings(
                        db,
                        agent,
                        effective_bindings,
                        binding_origin_map,
                        withheld=integration_context.setdefault("missing_integration_bindings", []),
                        origins=("agent_binding_inherited",),
                    )
                    from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY
                    from app.services.integration_live_writes import LIVE_WRITES_CONTEXT_KEY, live_writes_overrides
                    from app.services.integration_read_only import (
                        READ_ONLY_CONTEXT_KEY,
                        read_only_integration_ids,
                    )
                    if _fleet_autonomy is not None:
                        # The tools a gate of the run's fleet decides (#1480).
                        from app.services.fleet_autonomy import FLEET_GATED_TOOLS_CONTEXT_KEY, gate_tool_names

                        integration_context[FLEET_GATED_TOOLS_CONTEXT_KEY] = gate_tool_names(_fleet_autonomy[1])

                    # Resolved bindings: a template-bound provider marked
                    # read-only narrows the integration resolved for it above.
                    integration_context[READ_ONLY_CONTEXT_KEY] = read_only_integration_ids(
                        agent, effective_bindings
                    )
                    integration_context[SIGNATURE_CONTEXT_KEY] = getattr(agent, "integration_signatures", None)
                    integration_context[LIVE_WRITES_CONTEXT_KEY] = live_writes_overrides(agent, effective_bindings)
                    integration_context[PLATFORM_NAMES_CONTEXT_KEY] = _platform_names
                    integration_context[ATTACK_SOURCES_CONTEXT_KEY] = _attack_sources
                    integ_defs, integ_exec = await build_integration_tools(
                        db,
                        effective_bindings,
                        context=integration_context,
                    )
                    missing_bindings = integration_context.get(
                        "missing_integration_bindings", []
                    )
                    if isinstance(missing_bindings, list) and missing_bindings:
                        from app.services.integration_credentials import (
                            unavailable_integration_reasons,
                        )

                        # ``load_tools`` asked for a tool of one of them says
                        # why instead of "unknown tool" (#11866).
                        composite.withhold_integrations(unavailable_integration_reasons(missing_bindings))
                        integration_notice = _format_missing_integration_notice(
                            missing_bindings,
                            agent.output_format or "markdown",
                        )
                        logger.error(
                            "Assignment run %s has %d unresolved integration "
                            "binding(s); skipped those tools",
                            run_id,
                            len(missing_bindings),
                        )
                        # Same contract as the MCP unavailability note above:
                        # tell the LLM which tools are missing so it
                        # acknowledges the gap instead of hallucinating
                        # integration results. Only injected on degraded runs.
                        _integ_note = _format_missing_integration_prompt_note(
                            missing_bindings
                        )
                        if _integ_note:
                            from app.services.prompt_guard import (
                                insert_before_safety_anchor,
                            )

                            system_message = insert_before_safety_anchor(
                                system_message, _integ_note
                            )
                        # An integration invalid for its provider says which
                        # key it lacks (#9898), on top of the generic note.
                        from app.services.integration_credentials import (
                            format_invalid_integration_prompt_note,
                        )

                        _invalid_note = format_invalid_integration_prompt_note(missing_bindings)
                        if _invalid_note:
                            from app.services.prompt_guard import (
                                insert_before_safety_anchor,
                            )

                            system_message = insert_before_safety_anchor(
                                system_message, _invalid_note
                            )
                    if integ_defs:
                        # Apply tool-level scoping: agent-level first, then assignment-level.
                        # Effective tools = intersection of both allow lists.
                        agent_allowed = agent.integration_allowed_tools or {}
                        wf_allowed = assignment.integration_allowed_tools or {}
                        merged_allowed = merge_tool_allow_lists(
                            agent_allowed, wf_allowed
                        )
                        filtered_defs = filter_integration_tools(
                            integ_defs, merged_allowed
                        )
                        composite.add_tools(
                            filtered_defs,
                            AuthGuardedExecutor(integ_exec, filtered_defs, auth_guard),
                        )
                        logger.info(
                            "Loaded %d integration tool(s) for assignment (%d filtered by scoping)",
                            len(filtered_defs),
                            len(integ_defs) - len(filtered_defs),
                        )
                except Exception as e:
                    # Without integration tools the assignment runs with a
                    # silently degraded toolbox — operators frequently see
                    # "agent didn't do anything" with no visible cause.
                    # ERROR-level so the failure surfaces in monitoring;
                    # do NOT write to ``run.error`` here. ``AssignmentRun.error``
                    # is the failure-state field consumed everywhere else in
                    # this engine (lines 445, 450, 459, 477, 1998) and rendered
                    # in red by the UI as "this run failed". Writing a
                    # degraded-but-completed message into it AND still letting
                    # the run reach ``status="completed"`` (which the surrounding
                    # control flow does) leaves the user with a green completed
                    # run that's also marked with a red error — exactly the
                    # inconsistency Copilot flagged. Until ``AssignmentRun``
                    # gains a separate non-fatal-warning field (out of scope
                    # for this PR, would need a migration), the ERROR log is
                    # the operator-visible signal, and the Tool Availability
                    # Notice below surfaces the degradation on the deliverable.
                    logger.error(
                        "Failed to load integration tools for assignment "
                        "run %s (agent=%s): %s — agent will run WITHOUT "
                        "integration tools",
                        run_id,
                        agent.name,
                        format_exception_message(e),
                        exc_info=True,
                    )
                    # Total build failure: every bound provider is affected.
                    # Reuse the missing-binding notice so the degradation is
                    # visible on the run result, not only in worker logs.
                    integration_notice = _format_missing_integration_notice(
                        [
                            {"provider": key.split("#")[0], "integration_id": ""}
                            for key in effective_bindings
                        ],
                        agent.output_format or "markdown",
                    )

            # --- Channel/Bot tools (from channel_config) ---
            # Resolve effective channel_config: assignment overrides agent if set.
            effective_channel_config = (
                assignment.channel_config
                if assignment.channel_config is not None
                else agent.channel_config
            )
            if effective_channel_config:
                try:
                    from app.services.integration_tools import build_channel_tools

                    ch_defs, ch_exec = await build_channel_tools(
                        db,
                        effective_channel_config,
                        context={
                            "source": "assignment",
                            "agent": agent.name,
                            "assignment": assignment.name,
                            "run_id": str(run.id),
                        },
                    )
                    if ch_defs:
                        composite.add_tools(ch_defs, ch_exec)
                        logger.info(
                            "Loaded %d channel tool(s) for assignment", len(ch_defs)
                        )
                except Exception as e:
                    logger.warning("Failed to load channel tools: %s", format_exception_message(e))

            # --- Knowledge base tools (read + write, refined from agent config) ---
            # read_kb_ids / write_kbs were resolved above via
            # resolve_assignment_kb_access (read = refinement of the agent's
            # bound KBs; write = refinement of the agent's writable KBs).
            from app.services.builtin_tools import (
                build_kb_document_read_tools,
                build_kb_document_update_tool,
                build_kb_search_tool,
                build_kb_store_tool,
            )

            _kb_user_id = str(run.user_id) if run.user_id else ""
            if read_kb_ids:
                search_result = build_kb_search_tool(db, _kb_user_id, read_kb_ids)
                if search_result:
                    composite.add_tools(*search_result)
                # Full-source-document reads over the same readable
                # scope — ``read_kbs`` already carries the resolved names. The
                # agent identity is what lets an oversized document be registered
                # as a work item instead of injected (matching the identity
                # ``build_work_item_tools`` uses below).
                doc_read_result = build_kb_document_read_tools(
                    db,
                    _kb_user_id,
                    read_kbs,
                    agent_id=str(agent.id),
                    is_company_managed=bool(run.is_company_managed),
                    company_managed_agent=agent.is_company_managed,
                    assignment_run_id=str(run.id),
                    file_owner_id=_run_file_owner_id or "",
                )
                if doc_read_result:
                    composite.add_tools(*doc_read_result)
            if write_kbs:
                store_result = build_kb_store_tool(
                    db, _kb_user_id, write_kbs, source_type="assignment"
                )
                if store_result:
                    composite.add_tools(*store_result)
                doc_update_result = build_kb_document_update_tool(
                    db, _kb_user_id, write_kbs
                )
                if doc_update_result:
                    composite.add_tools(*doc_update_result)

            # --- Human-in-the-loop tool (built-in) ---
            from app.services.assignment_helpers import resolve_hitl

            _hitl_on, _ = resolve_hitl(agent, assignment)
            # The per-tool gate has its own switch; ``_hitl_on`` keeps
            # main's meaning, the ``request_human_input`` tool below.
            from app.services.hitl_whitelist import resolve_approve_tool_calls

            _gate_on = await resolve_approve_tool_calls(db, agent)
            if _hitl_on:
                from app.tools.base import CallableToolExecutor, ToolDefinition

                hitl_executor = CallableToolExecutor()

                async def _request_human_input(
                    question: str,
                    context: str = "",
                    options: list[dict[str, Any]] | None = None,
                    recommendation: str = "",
                    deadline: str = "",
                    links: list[dict[str, Any]] | None = None,
                ) -> str:
                    """Ask the user for input / a decision. Pauses the assignment."""
                    from app.services.decision_ask import normalize_run_ask, run_question_text

                    try:
                        ask = normalize_run_ask(question, context, options, recommendation, deadline, links)
                    except ValueError as exc:
                        # Refused before the pause: the agent rewrites the ask in the same turn.
                        return (
                            f"Error: nothing was asked. {exc}. In request_human_input, ids, codes the person "
                            "must not type and technical details go in context; ask again now."
                        )
                    # This exception propagates up through the agentic loop
                    # The tool_call_id is set by the agent loop's exception handler
                    raise HumanInputRequestedError(
                        question=run_question_text(ask),
                        tool_call_id="",  # will be set by the loop
                        conversation=[],  # will be set by the loop
                    )

                hitl_executor.register("request_human_input", _request_human_input)
                hitl_def = ToolDefinition(
                    name="request_human_input",
                    description=(
                        "Pause the assignment and ask a person a decision, approval or clarification. "
                        "Give a one-sentence question in plain words (no ids, keys or timestamps), "
                        "options with the one you recommend, and links to the sources. The run resumes "
                        "with the option chosen or the person's own words."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The decision in one sentence, in the reader's words.",
                            },
                            "context": {
                                "type": "string",
                                "description": "What you found and why you ask, shown under the question.",
                                "default": "",
                            },
                            "options": {
                                "type": "array",
                                "description": "Up to 6 answers. effect: what choosing it does.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "key": {"type": "string"},
                                        "label": {"type": "string"},
                                        "effect": {"type": "string"},
                                    },
                                    "required": ["key", "label"],
                                },
                            },
                            "recommendation": {
                                "type": "string",
                                "description": "The key of the option you recommend.",
                            },
                            "deadline": {
                                "type": "string",
                                "description": "When the answer is needed, ISO-8601.",
                            },
                            "links": {
                                "type": "array",
                                "description": "Sources to open before deciding.",
                                "items": {
                                    "type": "object",
                                    "properties": {"label": {"type": "string"}, "url": {"type": "string"}},
                                    "required": ["url"],
                                },
                            },
                        },
                        "required": ["question"],
                    },
                    source="builtin:hitl",
                )
                composite.add_tools([hitl_def], hitl_executor)

            # --- Agent memory tools (built-in, when persistent memory enabled) ---
            if agent.memory_persistent_enabled:
                from app.services.builtin_tools import (
                    build_memory_search_tool,
                    build_memory_store_tool,
                )

                mem_search_defs, mem_search_exec = build_memory_search_tool(
                    str(agent.id),
                    str(run.user_id) if run.user_id else "",
                )
                composite.add_tools(mem_search_defs, mem_search_exec)
                mem_store_defs, mem_store_exec = build_memory_store_tool(
                    str(agent.id),
                    str(run.user_id) if run.user_id else "",
                    source="assignment",
                )
                composite.add_tools(mem_store_defs, mem_store_exec)

            # --- Code interpreter tool (built-in, opt-in) ---
            # A deck agent always carries template_search: an assignment
            # prompt names the template it wants, and the agent finds it by name.
            from app.services.builtin_tools import ensure_deck_tools

            agent_builtin_tool_ids: list[str] = ensure_deck_tools(
                list(agent.builtin_tool_ids or [])
            )
            # A merged group wires under the ids it had before the merge, and a
            # former id under its group (``builtin_tool_aliases``).
            from app.services.builtin_tool_aliases import runtime_builtin_tool_ids

            agent_builtin_tool_ids = runtime_builtin_tool_ids(
                agent_builtin_tool_ids, getattr(agent, "builtin_allowed_tools", None)
            )

            # Collects base64 matplotlib charts emitted by the code interpreter
            # so ``inject_code_images`` can persist them as durable run files
            # and append them to the result after the loop — mirroring the chat
            # paths so charts surface in assignment runs too.
            code_images: list[str] = []
            # The ``render_chart`` blocks of the run, placed in the result.
            run_charts: list = []

            if not security_option("disable_code_interpreter") and "code_interpreter" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_code_interpreter_tool

                ci_defs, ci_exec = build_code_interpreter_tool(
                    db=db,
                    user_id=_run_file_owner_id or "",
                    # See the matching comment on the load_custom_tools call
                    # above — company-managed runs still need the combined
                    # quota check charged to the run's owning user.
                    quota_user_id=str(effective_user_id) if effective_user_id else "",
                    assignment_run_id=str(run.id),
                    image_sink=code_images,
                )
                composite.add_tools(ci_defs, ci_exec)

            if "generate_file" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_generate_file_tool

                gf_defs, gf_exec = build_generate_file_tool(
                    db,
                    _run_file_owner_id or "",
                    None,
                    assignment_run_id=str(run.id),
                    agent_id=str(agent.id),
                    agent_company_managed=bool(agent.is_company_managed),
                )
                composite.add_tools(gf_defs, gf_exec)

            if "web_fetch" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_web_fetch_tool

                wf_defs, wf_exec = build_web_fetch_tool(
                    db=db,
                    user_id=_run_file_owner_id or "",
                    assignment_run_id=str(run.id),
                )
                composite.add_tools(wf_defs, wf_exec)

            # --- Deep Investigation ---
            # No `on_event`: an assignment has no client watching, so there is
            # no live block to stream and the run simply produces its report.
            # No `investigations_out` either — the block is attached to a chat
            # message, and an assignment writes none. The report is still
            # persisted on the run, which is what a flow reads it from.
            # Behind "DEEP_INVESTIGATION", like the chat build.
            if "deep_investigation" in agent_builtin_tool_ids and is_feature_enabled("DEEP_INVESTIGATION"):
                from app.services.builtin_tools import build_deep_investigation_tool

                di_defs, di_exec = build_deep_investigation_tool(
                    db,
                    effective_user_id,
                    conversation_id=None,
                )
                composite.add_tools(di_defs, di_exec)

            if "browser" in agent_builtin_tool_ids:
                from app.services.browser_profiles.agent_access import access_for_agent
                from app.services.builtin_tools import build_browser_tool

                browser_key = f"{agent.id}:assignment:{run.id}"
                br_defs, br_exec = build_browser_tool(
                    browser_key,
                    db=db,
                    user_id=_run_file_owner_id or "",
                    assignment_run_id=str(run.id),
                    # The run's execution identity, never the file owner: a
                    # company-managed run still opens profiles for its run-as user.
                    profile_access=access_for_agent(agent, user_id=run.user_id),
                )
                composite.add_tools(br_defs, br_exec)

            # --- Native OS-control Runners (``computer_use``, opt-in) ---
            # ``assignment.runner_ids`` non-destructively refines the agent's
            # runner selection (None = inherit, [] = none, subset = refine),
            # mirroring the tool / KB scoping model. Visibility runs as the
            # run's user (``is_admin=False``); a company-managed run with no
            # owner reaches company-managed runners only.
            if settings.runners_enabled and "computer_use" in agent_builtin_tool_ids:
                from app.api.v1.helpers import get_user_group_ids
                from app.services.builtin_tools import build_computer_use_tools

                _cu_group_ids: list[str] = []
                if run.user_id:
                    _cu_group_ids = await get_user_group_ids(db, run.user_id)
                cu_defs, cu_exec = build_computer_use_tools(
                    user_id=str(run.user_id) if run.user_id else None,
                    is_admin=False,
                    user_group_ids=_cu_group_ids,
                    agent_runner_ids=agent.runner_ids,
                    agent_runner_labels=agent.runner_labels,
                    assignment_runner_ids=assignment.runner_ids,
                    agent_id=str(agent.id),
                    assignment_run_id=str(run.id),
                )
                composite.add_tools(cu_defs, cu_exec)

            if "stix_bundle_validator" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_stix_bundle_validator_tool
                from app.services.builtin_tools.stix import stix_validator_strict_refs

                sv_defs, sv_exec = build_stix_bundle_validator_tool(
                    strict_refs=stix_validator_strict_refs(agent)
                )
                composite.add_tools(sv_defs, sv_exec)

            if "stix_extractor" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_stix_extraction_tool

                sx_defs, sx_exec = build_stix_extraction_tool(
                    llm, wf_resolved_model, platform_names=_platform_names, attack_sources=_attack_sources
                )
                composite.add_tools(sx_defs, sx_exec)

            # --- Skill loader (built-in, IMPLICIT when skills are attached) ---
            # Mirrors agent_chat.py: the catalog in the system prompt tells the
            # model to call ``load_skill``, so the tool must exist whenever the
            # catalog does — it is never something the user has to enable.
            if any(
                not s.always_load or s.resource_paths for s in _resolved_skills
            ):
                from app.services.builtin_tools import build_skill_tools

                _skill_tools = build_skill_tools(
                    skill_ids=agent.skill_ids,
                    access=_skill_access,
                    always_load_ids=set(agent.always_load_skill_ids),
                    variables=_variables,
                )
                if _skill_tools:
                    composite.add_tools(*_skill_tools)

            # --- Work-item queue tools (built-in, opt-in) ---
            if "work_items" in agent_builtin_tool_ids:
                from app.services.builtin_tools import (
                    build_work_item_tools,
                    load_work_item_peers,
                )
                from app.services.work_item import WorkItemOrigin

                wi_peers = await load_work_item_peers(
                    owner_agent_ids=agent.work_item_owner_agent_ids,
                    user_id=_run_file_owner_id or None,
                )
                wi_defs, wi_exec = build_work_item_tools(
                    db,
                    _run_file_owner_id or "",
                    str(agent.id),
                    is_company_managed=run.is_company_managed,
                    company_managed_agent=agent.is_company_managed,
                    peer_agents=wi_peers,
                    origin=WorkItemOrigin(run_id=run.id),
                )
                composite.add_tools(wi_defs, wi_exec)

            # --- Scheduling (core: every agent, whatever its built-in tools) ---
            # For the person the run acts for; a run acting for nobody has
            # nobody a one-off action could run as.
            if run.user_id:
                from app.services.builtin_tools import build_scheduling_tools

                _sched_defs, _sched_exec = build_scheduling_tools(
                    user_id=str(run.user_id),
                    agent_id=str(agent.id),
                    agent_name=agent.name,
                    # An agent's run: its one-offs are silent unless they escalate.
                    caller_is_person=False,
                )
                composite.add_tools(_sched_defs, _sched_exec)

            # --- Organization profile (core, read-only: every agent, #8987) ---
            # Not on a run whose agent or assignment is explicitly off (#11533).
            from app.services.builtin_tools import build_organization_context_tool
            from app.services.organization_context_injection import tools_enabled_for

            if tools_enabled_for(agent=agent, assignment=assignment):
                # The person the run acts for scopes the enterprise knowledge passages (#11543).
                composite.add_tools(*build_organization_context_tool(user_id=run.user_id, agent=agent))

            # --- CTEM case tools (#11545): the CTEM remediation agents, XTM One only ---
            from app.services.builtin_tools import build_ctem_case_tools, offers_ctem_case_tools

            if run.user_id and offers_ctem_case_tools(agent.slug, is_builtin=agent.is_builtin):
                composite.add_tools(*build_ctem_case_tools(user_id=str(run.user_id), agent_id=str(agent.id)))
            # --- CTEM signal tools (#826): the CTEM agents that see a trigger first ---
            from app.services.builtin_tools import build_ctem_signal_tools, offers_ctem_signal_tools

            if run.user_id and offers_ctem_signal_tools(agent.slug, is_builtin=agent.is_builtin):
                composite.add_tools(*build_ctem_signal_tools(user_id=str(run.user_id)))
            # --- Read-only CTEM group (#1337): bound on any agent, as the run's user ---
            from app.services.builtin_tools import ctem_read_tools_for

            _ctem_read = (
                ctem_read_tools_for(agent, agent_builtin_tool_ids, user_id=str(run.user_id)) if run.user_id else None
            )
            if _ctem_read is not None:
                composite.add_tools(*_ctem_read)
            # --- CTEM administrator repairs (#1778): the CTEM case agents, or the group bound ---
            from app.services.builtin_tools import ctem_repair_tools_for

            _ctem_repair = (
                ctem_repair_tools_for(agent, agent_builtin_tool_ids, user_id=str(run.user_id)) if run.user_id else None
            )
            if _ctem_repair is not None:
                composite.add_tools(*_ctem_repair)
            # --- Learned skill (#914): a built-in agent records durable lessons ---
            from app.services.builtin_tools import build_learned_lesson_tool, offers_learned_lesson_tool

            if offers_learned_lesson_tool(is_builtin=agent.is_builtin):
                composite.add_tools(
                    *build_learned_lesson_tool(
                        agent_id=str(agent.id),
                        user_id=str(run.user_id) if run.user_id else None,
                        run_id=str(run.id),
                    )
                )

            # --- Organization profile write group (opt-in, administrators only, #11594) ---
            # The run acts as ``effective_user_id``; the group is wired only
            # when that person is an administrator, and each executor checks again.
            if "organization_context" in agent_builtin_tool_ids and effective_user_id:
                from app.models.user import User as _OrgActingUser
                from app.services.builtin_tools import (
                    build_organization_context_write_tools,
                    offers_organization_context_write,
                )

                _org_acting_admin = bool(getattr(await db.get(_OrgActingUser, effective_user_id), "is_admin", False))
                if offers_organization_context_write(
                    agent_builtin_tool_ids,
                    acting_user_id=str(effective_user_id),
                    acting_is_admin=_org_acting_admin,
                ):
                    composite.add_tools(
                        *build_organization_context_write_tools(
                            user_id=str(effective_user_id),
                            caller_is_admin=_org_acting_admin,
                            agent_name=agent.name,
                        )
                    )

            # --- User and group administration (opt-in, administrators only, #250) ---
            if "user_admin" in agent_builtin_tool_ids and effective_user_id:
                from app.models.user import User as _AdminActingUser
                from app.services.builtin_tools import build_user_admin_tools, offers_user_admin

                _ua_acting_admin = bool(getattr(await db.get(_AdminActingUser, effective_user_id), "is_admin", False))
                if offers_user_admin(
                    agent_builtin_tool_ids, acting_user_id=str(effective_user_id), acting_is_admin=_ua_acting_admin
                ):
                    composite.add_tools(
                        *build_user_admin_tools(
                            user_id=str(effective_user_id),
                            caller_is_admin=_ua_acting_admin,
                            agent_name=agent.name,
                            run_id=str(run.id),
                        )
                    )

            # --- Cross-run shared-state tools (built-in, opt-in) ---
            if "shared_state" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_shared_state_tools

                cs_defs, cs_exec = build_shared_state_tools(
                    db,
                    _run_file_owner_id or "",
                    str(agent.id),
                    is_company_managed=run.is_company_managed,
                    company_managed_agent=agent.is_company_managed,
                    run_id=run.id,
                )
                composite.add_tools(cs_defs, cs_exec)
                from app.services.builtin_tools import build_friction_tools

                composite.add_tools(
                    *build_friction_tools(str(run.user_id or _run_file_owner_id or ""), str(agent.id), run_id=run.id)
                )
                # Per-object timers ride with shared state: a run arms a date and
                # the platform raises timer.due then (services/platform_timers.py).
                if run.user_id:
                    from app.services.builtin_tools import build_timer_tools

                    composite.add_tools(*build_timer_tools(str(run.user_id), str(agent.id), run_id=run.id))

            # --- Workset tools (built-in, opt-in) — a large working corpus ---
            # Scoped to the ASSIGNMENT, not the run: a bi-weekly report's
            # corpus has to survive between runs for a sliding window to be
            # cheaper than a cold start, which is the whole point.
            if "workset" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_workset_tools

                # The execution identity, never the file owner: a
                # company-managed run has no file owner, and a workset needs
                # an identified user. ``effective_user_id`` is the engine's own
                # resolution (``run.user_id``, else the agent's owner), so a
                # company-managed run with no "run as" user still carries the
                # owner the corpus belongs to, instead of refusing every call.
                ws_defs, ws_exec = build_workset_tools(
                    db,
                    str(effective_user_id) if effective_user_id else "",
                    str(agent.id),
                    is_company_managed=run.is_company_managed,
                    workset_key=f"assignment-{run.assignment_id}",
                    scope="global",
                    # Derived by the runtime, never named by a flow: idle
                    # expiry, rolling budget, rotation and workset_resolve.
                    implicit=True,
                )
                composite.add_tools(ws_defs, ws_exec)

            # --- Interactive presentation generation (built-in, opt-in) ---
            # Deck rows are stamped with this run so a scheduled/delegated run's
            # deck stays attributable to it. The rendered prompt is the
            # run's request, which the template choice of a new deck reads.
            if "generate_presentation" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_presentation_tools

                pres_defs, pres_exec = build_presentation_tools(
                    db,
                    _run_file_owner_id or "",
                    None,
                    assignment_run_id=str(run.id),
                    request_text=rendered_prompt,
                )
                composite.add_tools(pres_defs, pres_exec)

            # --- Structured report generation (built-in, opt-in) ---
            # Report rows are stamped with this run so a scheduled brief's
            # document stays attributable to it.
            if "generate_report" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_report_tools

                rep_defs, rep_exec = build_report_tools(
                    db,
                    _run_file_owner_id or "",
                    None,
                    assignment_run_id=str(run.id),
                )
                composite.add_tools(rep_defs, rep_exec)

            # --- Custom dashboards (built-in, opt-in): a scheduled run refreshes
            # its owner's dashboard in place, stamped with the run ---
            if "custom_dashboards" in agent_builtin_tool_ids and _run_file_owner_id and is_feature_enabled("DASHBOARD_STUDIO"):
                from app.services.builtin_tools import build_custom_dashboard_tools

                dash_defs, dash_exec = build_custom_dashboard_tools(
                    db, _run_file_owner_id, None, assignment_run_id=str(run.id)
                )
                composite.add_tools(dash_defs, dash_exec)
            # --- Phone (built-in, opt-in): the run's owner's linked phones ---
            # Behind "MOBILE_COMPANION", like the chat build.
            if "phone" in agent_builtin_tool_ids and _run_file_owner_id and is_feature_enabled("MOBILE_COMPANION"):
                from app.services.builtin_tools import build_phone_tools

                phone_defs, phone_exec = build_phone_tools(db, _run_file_owner_id, agent_name=agent.name)
                composite.add_tools(phone_defs, phone_exec)

            # --- Video Studio (built-in, opt-in): as the run's file owner ---
            # Behind "VIDEO_STUDIO", like the chat build.
            if "video_studio" in agent_builtin_tool_ids and _run_file_owner_id and is_feature_enabled("VIDEO_STUDIO"):
                from app.services.builtin_tools import build_video_studio_tools

                vs_defs, vs_exec = build_video_studio_tools(user_id=_run_file_owner_id, automation=True)
                composite.add_tools(vs_defs, vs_exec)

            # --- Structured template lookup (built-in, opt-in) ---
            # Exact metadata filters over ``template_containers``; never touches
            # the vector store or a knowledge base.
            if "template_search" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_template_search_tools

                tpl_defs, tpl_exec = build_template_search_tools(
                    db, _run_file_owner_id or ""
                )
                composite.add_tools(tpl_defs, tpl_exec)

            # --- Template authoring (built-in, opt-in) ---
            # Write half of the template surface. Same owner as the read
            # tools: a company-managed run has no ``_run_file_owner_id``, which
            # collapses to the anonymous case and authors nothing — an unattended
            # run must not create containers nobody owns.
            if "template_authoring" in agent_builtin_tool_ids:
                from app.services.builtin_tools import (
                    build_template_authoring_tools,
                )

                tpl_write_defs, tpl_write_exec = build_template_authoring_tools(
                    db, _run_file_owner_id or ""
                )
                composite.add_tools(tpl_write_defs, tpl_write_exec)

            if "document_vision" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_document_vision_tools

                doc_defs, doc_exec = build_document_vision_tools(
                    db,
                    _run_file_owner_id or "",
                    None,
                    assignment_run_id=str(run.id),
                )
                composite.add_tools(doc_defs, doc_exec)

            if "inspect_my_traces" in agent_builtin_tool_ids or "inspect_agent_traces" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_trace_tools

                trace_defs, trace_exec = build_trace_tools(
                    db,
                    str(effective_user_id) if effective_user_id else "",
                    str(agent.id),
                    supervision_target_agent_ids=agent.trace_supervision_target_agent_ids,
                    # Only the supervision grant lets an admin run identity's
                    # privilege reach another agent's payloads; self-inspection
                    # alone must not.
                    allow_cross_agent="inspect_agent_traces" in agent_builtin_tool_ids,
                )
                if "inspect_my_traces" not in agent_builtin_tool_ids:
                    trace_defs = [d for d in trace_defs if d.name != "inspect_my_traces"]
                if "inspect_agent_traces" not in agent_builtin_tool_ids:
                    trace_defs = [d for d in trace_defs if d.name != "inspect_agent_traces"]
                if trace_defs:
                    composite.add_tools(trace_defs, trace_exec)

            if "fimi_analytics" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_fimi_analytics_composite

                _defs, _exec = build_fimi_analytics_composite()
                composite.add_tools(_defs, _exec)

            # --- Agent builder tool (built-in, opt-in) ---
            # Mirrors agent_chat.py: an agent with ``agent_builder`` in its
            # builtin_tool_ids gets the platform-management tool set in
            # assignments too (list/get agents, runs, assignments, KBs, ...).
            # The acting identity is ``effective_user_id`` (run owner, falling
            # back to the agent owner — the same per-user resolution identity
            # used for integrations / model / memory), so every builder tool
            # enforces exactly that user's permissions: an admin-owned
            # scheduled run gets admin visibility, a run-as user gets theirs,
            # and an owner-less run degrades to the anonymous read-only
            # surface (company-managed entities only, writes refused).
            if "flow_builder" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_flow_builder_tool

                if effective_user_id:
                    fb_defs, fb_exec = build_flow_builder_tool(
                        db, str(effective_user_id)
                    )
                    composite.add_tools(fb_defs, fb_exec)

            if "agent_builder" in agent_builtin_tool_ids:
                from app.models.user import User as _ActingUser
                from app.services.builtin_tool_aliases import caller_may_run
                from app.services.builtin_tools import (
                    build_agent_builder_tool,
                    build_evaluation_tools,
                    build_trace_tools,
                )

                # The run acts as ``effective_user_id``: an admin-only
                # function of the platform group is wired for an
                # administrator alone (Sam's decision 2).
                _acting_is_admin = bool(
                    effective_user_id
                    and getattr(await db.get(_ActingUser, effective_user_id), "is_admin", False)
                )
                ab_defs, ab_exec = build_agent_builder_tool(
                    db,
                    str(effective_user_id) if effective_user_id else "",
                    # The parent of the run ``run_agent_once`` creates.
                    run_id=str(run.id),
                )
                ab_defs = [d for d in ab_defs if caller_may_run(d.name, is_admin=_acting_is_admin)]
                composite.add_tools(ab_defs, ab_exec)

                # Platform built-in alias: agent_builder implies the full
                # trace inspection surface. Legacy trace ids stay
                # authoritative for the tools they already registered above;
                # the alias only fills in the trace tools still missing.
                _missing_trace_tools = {
                    "inspect_my_traces",
                    "inspect_agent_traces",
                } - set(agent_builtin_tool_ids)
                if _missing_trace_tools:
                    from app.services.builtin_tools.traces import (
                        TRACE_PAYLOAD_TOOL_NAME,
                    )

                    trace_defs, trace_exec = build_trace_tools(
                        db,
                        str(effective_user_id) if effective_user_id else "",
                        str(agent.id),
                        supervision_target_agent_ids=agent.trace_supervision_target_agent_ids,
                        # This alias grants the full inspection surface,
                        # supervision included, so the admin branch applies here.
                        allow_cross_agent=True,
                    )
                    # ``get_trace_payload`` is not a selectable id — it is the
                    # fetch that makes the inspection tools' bounded previews
                    # readable in full, so it comes along with whichever of them
                    # this alias adds. ``add_tools`` is last-write-wins by name,
                    # so re-adding one already registered above is a no-op.
                    trace_defs = [
                        d
                        for d in trace_defs
                        if d.name in _missing_trace_tools
                        or d.name == TRACE_PAYLOAD_TOOL_NAME
                    ]
                    if trace_defs:
                        composite.add_tools(trace_defs, trace_exec)

                ev_defs, ev_exec = build_evaluation_tools(
                    db,
                    str(effective_user_id) if effective_user_id else "",
                )
                ev_defs = [d for d in ev_defs if caller_may_run(d.name, is_admin=_acting_is_admin)]
                composite.add_tools(ev_defs, ev_exec)

            # --- Platform insights: read as the run's acting user ---
            # An owner-less run has no account to read for and gets none.
            if "platform_insights" in agent_builtin_tool_ids and effective_user_id:
                from app.services.builtin_tools import build_platform_insights_tools

                pi_defs, pi_exec = build_platform_insights_tools(
                    str(effective_user_id),
                    chart_sink=run_charts,
                    assignment_run_id=str(run.id),
                    agent_id=str(agent.id),
                )
                composite.add_tools(pi_defs, pi_exec)

            # --- Memory Steward (#8258): refused to anyone but Platform Health ---
            if "memory_steward" in agent_builtin_tool_ids:
                from app.services.builtin_tools import build_memory_steward_tools

                # The run's Platform Health cycle pays for the pass's model calls.
                composite.add_tools(*build_memory_steward_tools(run_id=str(run.id)))
            # --- Platform Health (#8259): the run acts as its principal, user or not ---
            from app.services.builtin_tools.health_quality import add_health_quality_tools

            add_health_quality_tools(
                composite,
                str(effective_user_id) if effective_user_id else "",
                agent_builtin_tool_ids,
                agent_id=str(run.agent_id),
                # The run's Platform Health cycle books what its experiments spend.
                run_id=str(run.id),
            )
            # --- Module Analyst (#101 S14a-2b-4): the investigated module's allow-listed reads ---
            from app.services.builtin_tools.health_modules import add_module_read_tools

            try:
                await add_module_read_tools(
                    db,
                    composite,
                    agent_builtin_tool_ids,
                    trigger_context=run.trigger_context,
                    agent_id=run.agent_id,
                    user_id=str(effective_user_id) if effective_user_id else None,
                    run_id=str(run.id),
                )
            except Exception as exc:
                logger.error(
                    "Module Analyst read tools not loaded for run %s: %s",
                    run_id,
                    format_exception_message(exc),
                )
            from app.services.builtin_tools.flow_lab import add_flow_lab_tools

            add_flow_lab_tools(
                composite, str(effective_user_id) if effective_user_id else "", agent_builtin_tool_ids, agent_id=str(run.agent_id)
            )
            # --- Platform Health orchestrator (#8261): read-only in a run, so a
            # cycle's own run never starts another cycle ---
            if "health_orchestrator" in agent_builtin_tool_ids:
                from app.services.builtin_tools.health_orchestrator import build_health_orchestrator_tools

                co_defs, co_exec = build_health_orchestrator_tools(
                    str(effective_user_id) if effective_user_id else None, allow_requests=False
                )
                composite.add_tools(co_defs, co_exec)

            # --- Fleets (#7030): managed as the run's acting user ---
            # An owner-less run has no account to act for and gets none.
            if "fleets" in agent_builtin_tool_ids and effective_user_id:
                from app.services.builtin_tools import build_fleet_tools

                # The run is where export_fleet_spec keeps the full spec file.
                fl_defs, fl_exec = build_fleet_tools(
                    str(effective_user_id), assignment_run_id=str(run.id), agent_id=str(run.agent_id)
                )
                composite.add_tools(fl_defs, fl_exec)

            # --- ROI and outcomes (#10006 S2): as the run's acting user ---
            if "roi" in agent_builtin_tool_ids and effective_user_id:
                from app.services.builtin_tools.roi import build_roi_tools

                composite.add_tools(*build_roi_tools(str(effective_user_id)))

            # --- Platform Health sandbox (#8255): a Platform Health run has no user of its
            # own; the tools apply their own rule (Platform Health or an admin).
            if "health_sandbox" in agent_builtin_tool_ids:
                from app.services.health.sandbox.tools import build_sandbox_tools

                composite.add_tools(
                    *build_sandbox_tools(db, str(effective_user_id) if effective_user_id else None)
                )
            # --- Platform Health evaluation (#8257): the same rule, the tools' own.
            if "health_evaluation" in agent_builtin_tool_ids:
                from app.services.health.evaluation_tools import build_health_evaluation_tools

                composite.add_tools(
                    *build_health_evaluation_tools(
                        db, str(effective_user_id) if effective_user_id else None, run_id=str(run.id)
                    )
                )
            if "improvement_proposals" in agent_builtin_tool_ids:
                from app.services.builtin_tools.improvements import build_improvement_tools

                composite.add_tools(
                    *build_improvement_tools(
                        db, str(effective_user_id) if effective_user_id else None, str(agent.id)
                    )
                )

            # --- Handover tools (type-aware: consult / transfer / delegate) ---
            # Mirrors agent_chat.py logic: checks agent.handover_types to determine
            # which tool types to inject (consult, transfer, delegate).
            _handover_loaded = False
            _handover_agents: list[Agent] = []
            # The peers each handover tool accepts: its own type's only.
            _peers_by_type: dict[str, list[Agent]] = {}
            # Every configured peer, so a background task to a peer of another
            # type is refused naming that type.
            _configured_handover_peers: list[Agent] = []
            # Ordered chain of agent ids for handover cycle detection —
            # assignment runs are chain roots, so the chain starts with the
            # current agent.  Hoisted out of the peers-only block because the
            # background-task tools built after it (self-fan-out) need the
            # chain even when the agent has no peer handover agents.
            _visited_agents: list[str] = [str(agent.id)]
            _needs_delegate = False
            # The background-task block below needs a user to record its tasks
            # under: the acting user, or, for Platform Health acting alone, its
            # agent's owner as the row's owner only (the tasks act with no user).
            _delegation_user_id = effective_user_id or await health_delegation_owner_id(db, agent)
            _offered_handover_types = offered_handover_types(
                delegate_available=_delegation_user_id is not None,
                tool_deny_list=agent.tool_deny_list,
            )
            if agent.handover_agent_ids:
                try:
                    from app.api.v1.helpers import (
                        get_user_group_ids,
                        user_visible_filter,
                    )

                    h_uuids = [_uuid.UUID(h) for h in agent.handover_agent_ids if h]
                    if h_uuids:
                        _user_group_ids: list[str] = []
                        if run.user_id:
                            _user_group_ids = await get_user_group_ids(db, run.user_id)

                        _ho_query = (
                            select(Agent)
                            .where(Agent.id.in_(h_uuids), Agent.enabled.is_(True))
                            .where(
                                user_visible_filter(Agent, run.user_id, _user_group_ids)
                            )
                        )
                        from app.services.fleet_runs import active_handover_target_clause

                        _ho_query = _ho_query.where(active_handover_target_clause())
                        _ho_result = await db.execute(_ho_query)
                        from app.services.builtin_tools import (
                            consult_refusal,
                            consulted_peer_handover,
                            handover_cycle_error,
                            handover_peer_tag,
                            handover_peers_by_type,
                            handover_tool_types,
                            live_consult_target,
                            reachable_handover_peers,
                            resolve_handover_agent,
                        )

                        _handover_type_map: dict[str, str] = (
                            agent.handover_types or {}
                        )
                        _configured_handover_peers = list(_ho_result.scalars().all())
                        _handover_agents = reachable_handover_peers(
                            _configured_handover_peers,
                            _handover_type_map,
                            offered=_offered_handover_types,
                        )
                        _peers_by_type = handover_peers_by_type(
                            _handover_agents,
                            _handover_type_map,
                            offered=_offered_handover_types,
                        )

                        if _handover_agents:
                            from app.tools.base import (
                                CallableToolExecutor,
                                ToolDefinition,
                            )

                            _handover_names: dict[str, str] = {
                                str(ha.id): ha.name for ha in _handover_agents
                            }
                            _handover_names[str(agent.id)] = agent.name

                            _h_types = handover_tool_types(
                                _handover_agents,
                                _handover_type_map,
                                offered=_offered_handover_types,
                            )
                            _needs_consult = "consult" in _h_types
                            _needs_transfer = "transfer" in _h_types
                            _needs_delegate = "delegate" in _h_types

                            _ho_exec = CallableToolExecutor()
                            _ho_defs: list[ToolDefinition] = []

                            # ── Consult tool ──────────────────────────
                            if _needs_consult:
                                # Bound parallel consults per run — one turn can
                                # request several under asyncio.gather, each with
                                # its own DB session + nested agent setup, which
                                # would burst-consume pooled connections.
                                # Same limit as
                                # spawn_background_task's per-run cap.
                                _consult_sem = asyncio.Semaphore(
                                    max(
                                        1,
                                        settings.max_concurrent_sub_agent_tasks_per_run,
                                    )
                                )

                                async def _consult_agent(
                                    agent_id: str, question: str, output_schema: object = None
                                ) -> str:
                                    """Ask another agent a question and get its answer back.

                                    Uses an isolated DB session so multiple parallel
                                    consult calls don't interfere with each other.
                                    Bounded by a per-run semaphore so a burst of
                                    parallel consults cannot monopolise the pool.
                                    """
                                    if not question or not question.strip():
                                        return (
                                            "Consultation failed: missing required question. "
                                            "Please include a specific question for the target agent."
                                        )
                                    from app.services.output_format import parse_call_output_schema

                                    _call_schema, _schema_error = parse_call_output_schema(output_schema)
                                    if _schema_error:
                                        return _schema_error

                                    target = resolve_handover_agent(
                                        agent_id, _peers_by_type["consult"]
                                    )
                                    if not target:
                                        return consult_refusal(
                                            agent_id,
                                            _handover_type_map,
                                            _configured_handover_peers,
                                            offered=_offered_handover_types,
                                        )
                                    from app.services.output_format import caller_output_format_refusal

                                    _format_refusal = caller_output_format_refusal(target, _call_schema)
                                    if _format_refusal:
                                        return _format_refusal

                                    _cycle_msg = handover_cycle_error(
                                        str(target.id),
                                        _visited_agents,
                                        agent_names=_handover_names,
                                        target_name=target.name,
                                    )
                                    if _cycle_msg:
                                        return _cycle_msg

                                    # The run is the chain root: the consulted
                                    # peer runs one level below it.
                                    _nested_handover, _nested_types = consulted_peer_handover(1)
                                    async with _consult_sem, async_session() as consult_db:
                                        # The peer's row as it is now.
                                        target = await live_consult_target(
                                            consult_db, target
                                        )
                                        # Read now: the failure path's rollback
                                        # expires the row.
                                        _target_id = str(target.id)
                                        _target_name = target.name
                                        _start_ts = datetime.now(UTC)
                                        # Bounded like a chat consult:
                                        # no event within 90 s, or none for
                                        # 600 s, or a rollout, and the call
                                        # answers at once. This handler makes
                                        # no child run: nothing is left
                                        # "running" when it is cancelled.
                                        from app.services.consult_watchdog import (
                                            ConsultCutError,
                                            ConsultStalledError,
                                            ConsultWatchdog,
                                            durable_consult_after_cut,
                                            rollout_cut_reason,
                                            run_watched,
                                        )

                                        _watchdog = ConsultWatchdog()

                                        async def _watch_consult(
                                            inner_event: dict,
                                        ) -> None:
                                            _watchdog.observe(inner_event)

                                        try:
                                            from app.services.agent_chat import (
                                                run_agent_chat,
                                            )

                                            consulted_text, _ = await run_watched(
                                                run_agent_chat(
                                                consult_db,
                                                (
                                                    str(run.user_id)
                                                    if run.user_id
                                                    else str(agent.id)
                                                ),
                                                target,
                                                [],
                                                question,
                                                allow_handover=_nested_handover,
                                                allowed_handover_types=_nested_types,
                                                session_output_schema=_call_schema,
                                                delegation_depth=1,
                                                handover_context={
                                                    "type": "consult",
                                                    "source_agent": f'"{agent.name}" (id: {agent.id})',
                                                    "original_question": question,
                                                    "visited_agents": _visited_agents,
                                                    # The run's boundaries:
                                                    # the peer only narrows them.
                                                    "element_scope": enforced_scope,
                                                },
                                                parent_run_id=str(run.id),
                                                file_context=file_context,
                                                # The parent run was already
                                                # metered — do not double-count
                                                # the nested consult (OBS-3).
                                                meter_quota=False,
                                                # An emergency stop on the
                                                # parent reaches the consulted
                                                # agent's loop too.
                                                cancel_event=cancel_event,
                                                on_event=_watch_consult,
                                                ),
                                                _watchdog,
                                                child_run_id=None,
                                                cut_check=rollout_cut_reason,
                                            )
                                            _elapsed = int(
                                                (
                                                    datetime.now(UTC) - _start_ts
                                                ).total_seconds()
                                                * 1000
                                            )
                                            try:
                                                from app.services.delegation import (
                                                    log_delegation,
                                                )

                                                await log_delegation(
                                                    consult_db,
                                                    delegation_type="consult",
                                                    user_id=(
                                                        str(run.user_id)
                                                        if run.user_id
                                                        else None
                                                    ),
                                                    parent_agent_id=str(agent.id),
                                                    target_agent_id=str(target.id),
                                                    description=question[
                                                        :TRUNCATE_QUESTION
                                                    ],
                                                    status="completed",
                                                    result=(
                                                        consulted_text[:TRUNCATE_RESULT]
                                                        if consulted_text
                                                        else None
                                                    ),
                                                    duration_ms=_elapsed,
                                                    metadata={
                                                        "source_agent_name": agent.name,
                                                        "target_agent_name": target.name,
                                                        "context": "assignment",
                                                        "run_id": str(run.id),
                                                    },
                                                )
                                            except Exception as _deleg_err:
                                                logger.warning(
                                                    "Failed to log consult delegation: %s",
                                                    format_exception_message(_deleg_err),
                                                )
                                            await consult_db.commit()
                                            return f"[Response from {target.name}]:\n{consulted_text}"
                                        except Exception as e:
                                            logger.error(
                                                "consult_agent failed for %s: %s",
                                                agent_id,
                                                format_exception_message(e),
                                                exc_info=not isinstance(e, ConsultStalledError),
                                            )
                                            _elapsed = int(
                                                (
                                                    datetime.now(UTC) - _start_ts
                                                ).total_seconds()
                                                * 1000
                                            )
                                            if isinstance(e, ConsultStalledError):
                                                # The turn the watchdog stopped
                                                # may have left its session
                                                # inside a transaction.
                                                try:
                                                    await consult_db.rollback()
                                                except Exception as _rb_err:
                                                    logger.warning(
                                                        "Consult session rollback failed: %s",
                                                        format_exception_message(_rb_err),
                                                    )
                                            try:
                                                from app.services.delegation import (
                                                    log_delegation,
                                                )

                                                await log_delegation(
                                                    consult_db,
                                                    delegation_type="consult",
                                                    user_id=(
                                                        str(run.user_id)
                                                        if run.user_id
                                                        else None
                                                    ),
                                                    parent_agent_id=str(agent.id),
                                                    target_agent_id=_target_id,
                                                    description=question[
                                                        :TRUNCATE_QUESTION
                                                    ],
                                                    status="failed",
                                                    error=str(e)[:TRUNCATE_ERROR],
                                                    duration_ms=_elapsed,
                                                    metadata={
                                                        "source_agent_name": agent.name,
                                                        "target_agent_name": _target_name,
                                                        "context": "assignment",
                                                        "run_id": str(run.id),
                                                    },
                                                )
                                            except Exception as log_exc:
                                                logger.debug(
                                                    "Failed to log consult delegation: %s",
                                                    format_exception_message(log_exc),
                                                )
                                            await consult_db.commit()
                                            if isinstance(e, ConsultCutError):
                                                # A rollout cut it: asked again as a
                                                # durable work item on the peer, the
                                                # run's own item waiting on it.
                                                _dispatched_item = _inbox_dispatched_work_item(run)
                                                _durable = await durable_consult_after_cut(
                                                    user_id=(
                                                        _dispatched_item[1]
                                                        if _dispatched_item and _dispatched_item[1]
                                                        else (str(run.user_id) if run.user_id else None)
                                                    ),
                                                    caller_agent_id=str(agent.id),
                                                    caller_name=agent.name,
                                                    caller_is_company_managed=bool(agent.is_company_managed),
                                                    target_id=_target_id,
                                                    target_name=_target_name,
                                                    question=question,
                                                    cuts=1,
                                                    source_work_item_id=(
                                                        _dispatched_item[0] if _dispatched_item else None
                                                    ),
                                                    parent_run_id=str(run.id),
                                                )
                                                if _durable:
                                                    return _durable
                                            return f"Consultation with {_target_name} failed: {e}"

                                from app.services.output_format import CALL_OUTPUT_SCHEMA_PROPERTY

                                _ho_exec.register("consult_agent", _consult_agent)
                                _ho_defs.append(
                                    ToolDefinition(
                                        name="consult_agent",
                                        description=(
                                            "Ask another specialized agent a question and receive its response. "
                                            "You stay in control — use the answer to inform your own work. "
                                            "Use this for quality checks, reviews, or when you need "
                                            "another agent's expertise without giving up control. "
                                            "You can call this tool multiple times in a single turn for "
                                            "independent queries — they will execute in parallel."
                                        ),
                                        input_schema={
                                            "type": "object",
                                            "properties": {
                                                "agent_id": {
                                                    "type": "string",
                                                    "description": "The ID or slug of the agent to consult (from the peer agents list)",
                                                },
                                                "question": {
                                                    "type": "string",
                                                    "description": "The question to ask the other agent",
                                                },
                                                "output_schema": CALL_OUTPUT_SCHEMA_PROPERTY,
                                            },
                                            "required": ["agent_id", "question"],
                                        },
                                        source="builtin:handover",
                                    )
                                )

                            # ── Transfer tool ─────────────────────────
                            if _needs_transfer:

                                async def _transfer_to_agent(
                                    agent_id: str, message: str, reason: str = ""
                                ) -> str:
                                    """Transfer remaining work to another agent; its response becomes the output."""
                                    target = resolve_handover_agent(
                                        agent_id, _peers_by_type["transfer"]
                                    )
                                    if not target:
                                        return f"Agent {agent_id} is not available for transfer."
                                    _cycle_msg = handover_cycle_error(
                                        str(target.id),
                                        _visited_agents,
                                        agent_names=_handover_names,
                                        target_name=target.name,
                                    )
                                    if _cycle_msg:
                                        return _cycle_msg
                                    _start_ts = datetime.now(UTC)
                                    try:
                                        from app.services.agent_chat import (
                                            run_agent_chat,
                                        )

                                        transferred_text, _ = await run_agent_chat(
                                            db,
                                            (
                                                str(run.user_id)
                                                if run.user_id
                                                else str(agent.id)
                                            ),
                                            target,
                                            [],
                                            message,
                                            allow_handover=False,
                                            handover_context={
                                                "type": "transfer",
                                                "source_agent": f'"{agent.name}" (id: {agent.id})',
                                                "reason": reason,
                                                "visited_agents": _visited_agents,
                                                # The run's boundaries:
                                                # the peer only narrows them.
                                                "element_scope": enforced_scope,
                                            },
                                            parent_run_id=str(run.id),
                                            file_context=file_context,
                                            # The parent run was already
                                            # metered — do not double-count
                                            # the nested transfer (OBS-3).
                                            meter_quota=False,
                                            cancel_event=cancel_event,
                                        )
                                        _elapsed = int(
                                            (
                                                datetime.now(UTC) - _start_ts
                                            ).total_seconds()
                                            * 1000
                                        )
                                        try:
                                            from app.services.delegation import (
                                                log_delegation,
                                            )

                                            await log_delegation(
                                                db,
                                                delegation_type="transfer",
                                                user_id=(
                                                    str(run.user_id)
                                                    if run.user_id
                                                    else None
                                                ),
                                                parent_agent_id=str(agent.id),
                                                target_agent_id=str(target.id),
                                                description=(
                                                    reason[:TRUNCATE_QUESTION]
                                                    if reason
                                                    else message[:TRUNCATE_QUESTION]
                                                ),
                                                status="completed",
                                                result=(
                                                    transferred_text[:TRUNCATE_RESULT]
                                                    if transferred_text
                                                    else None
                                                ),
                                                duration_ms=_elapsed,
                                                metadata={
                                                    "source_agent_name": agent.name,
                                                    "target_agent_name": target.name,
                                                    "context": "assignment",
                                                    "run_id": str(run.id),
                                                },
                                            )
                                        except Exception as _deleg_err:
                                            logger.warning(
                                                "Failed to log transfer delegation: %s",
                                                format_exception_message(_deleg_err),
                                            )
                                        return f"[Completed by {target.name}]:\n{transferred_text}"
                                    except Exception as e:
                                        logger.error(
                                            "transfer_to_agent failed for %s: %s",
                                            agent_id,
                                            format_exception_message(e),
                                            exc_info=True,
                                        )
                                        return f"Transfer to {target.name} failed: {e}"

                                _ho_exec.register(
                                    "transfer_to_agent", _transfer_to_agent
                                )
                                _ho_defs.append(
                                    ToolDefinition(
                                        name="transfer_to_agent",
                                        description=(
                                            "Transfer the remaining work to another specialized agent. "
                                            "The target agent will process the message and its response "
                                            "becomes part of this assignment's output. Only use this when "
                                            "the remaining work is entirely within the other agent's specialty."
                                        ),
                                        input_schema={
                                            "type": "object",
                                            "properties": {
                                                "agent_id": {
                                                    "type": "string",
                                                    "description": "The ID or slug of the agent to transfer to (from the peer agents list)",
                                                },
                                                "message": {
                                                    "type": "string",
                                                    "description": "The task or message to send to the target agent",
                                                },
                                                "reason": {
                                                    "type": "string",
                                                    "description": "Brief explanation of why the work is being transferred",
                                                },
                                            },
                                            "required": ["agent_id", "message"],
                                        },
                                        source="builtin:handover",
                                    )
                                )

                            if _ho_defs:
                                composite.add_tools(_ho_defs, _ho_exec)

                            # ── Build type-aware handover system prompt ─
                            _type_instructions = {
                                "consult": "Use `consult_agent` to ask a question and get an answer back (you stay in control).",
                                "delegate": "Use `spawn_background_task` to delegate work that runs asynchronously.",
                                "transfer": "Use `transfer_to_agent` to hand over remaining work (its response becomes part of the output).",
                            }
                            _ho_parts: list[str] = [
                                "\n\n## Agent Delegation",
                                "You can delegate to the following peer agents:",
                                "\nAvailable peer agents:",
                            ]
                            for _ha in _handover_agents:
                                _tag = " [company]" if _ha.is_company_managed else ""
                                _desc = (
                                    _ha.description or _ha.persona or "No description"
                                )
                                if len(_desc) > TRUNCATE_LABEL:
                                    _desc = _desc[:TRUNCATE_LABEL] + "..."
                                _slug_part = f", slug: {_ha.slug}" if _ha.slug else ""
                                _type_label, _instr = handover_peer_tag(
                                    _handover_type_map,
                                    str(_ha.id),
                                    _type_instructions,
                                    offered=_offered_handover_types,
                                )
                                _ho_parts.append(
                                    f'- "{_ha.name}" (id: {_ha.id}{_slug_part}){_tag} [{_type_label}]: {_desc} — {_instr}'
                                )

                            if _needs_consult:
                                _ho_parts.append(
                                    "\n**Parallel consulting**: for independent "
                                    "tasks, call `consult_agent` multiple times "
                                    "in a single turn — the calls run in "
                                    "parallel."
                                )
                            # Insert the delegation section BEFORE the safety
                            # anchor: prompt_guard.py documents
                            # SAFETY_SYSTEM_PROMPT as the FINAL high-priority
                            # block, and agent_chat preserves that contract
                            # via insert-before — a plain += here would leave
                            # the delegation text dangling after it.
                            from app.services.prompt_guard import (
                                insert_before_safety_anchor,
                            )

                            system_message = insert_before_safety_anchor(
                                system_message, "\n".join(_ho_parts)
                            )

                            _handover_loaded = True
                            logger.info(
                                "Loaded %d handover agent(s) for assignment (consult=%s, transfer=%s, delegate=%s)",
                                len(_handover_agents),
                                _needs_consult,
                                _needs_transfer,
                                _needs_delegate,
                            )
                except Exception as _ho_err:
                    logger.warning(
                        "Failed to load handover tools for assignment: %s", format_exception_message(_ho_err)
                    )

            # Resolve max_iterations with assignment-over-agent precedence:
            #   assignment override → agent override → platform default.
            # The assignment is the most specific scope, so an explicit
            # assignment value WINS over the agent's override; NULL on both
            # falls back to the platform default (GeneralAssistantConfig),
            # mirroring the chat fallback in agent_chat. Bound here — BEFORE
            # the background-task tools, which hand it to spawned workers as
            # the inherited budget — so the spawn tools, the trace
            # metadata, the agentic loop, and the TimeoutError handler all
            # share one value.
            effective_max_iterations = await resolve_run_max_iterations(
                db, assignment, agent
            )
            # Wait bounds for the ``wait`` primitive, resolved alongside
            # the iteration budget: an assignment that delegates work needs the
            # timer just as much as chat does, and the run's own timeout below
            # still clamps every pause.
            effective_wait_policy = await resolve_run_wait_policy(db, assignment, agent)

            # ``timeout_seconds == 0`` (or any non-positive) disables the
            # per-run timeout entirely (see Assignment.timeout_seconds) — pass
            # None to asyncio.wait_for rather than coercing it to a default
            # and force-cancelling a run whose timeout was deliberately off.
            # ``None`` (unset / unflushed row) falls back to the SAME
            # ``_UNSET_TIMEOUT_FALLBACK_SECONDS`` used by both orchestration
            # ``deadline_at`` sites, so the inner bound can never outlive the
            # reconciler's deadline-derived budget (it used to be 900 s here
            # vs 300 s there).  Bound here — BEFORE the background-task tools
            # AND the try block — for two reasons: spawned workers inherit the
            # FLOORED effective budget below (not the raw stored value), and
            # the ``except TimeoutError`` handler can never hit an
            # UnboundLocalError when an inner await raises TimeoutError before
            # the ``asyncio.wait_for`` line.
            #
            # The floor (max of the _MIN_ASSIGNMENT_TIMEOUT_SECONDS static
            # minimum and _MIN_SECS_PER_ITERATION × max_iterations) prevents an
            # assignment with a high iteration budget (e.g. max_iterations=50)
            # from being killed by the 300 s default timeout before it can
            # exhaust its iteration budget — mirrors agent_chat.py's dynamic
            # chat_loop_timeout formula.
            _ts = assignment.timeout_seconds
            timeout = (
                None
                if (_ts is not None and _ts <= 0)
                else (_ts or _UNSET_TIMEOUT_FALLBACK_SECONDS)
            )
            timeout = _apply_iteration_timeout_floor(
                timeout, effective_max_iterations, run_id, _ts
            )
            # Per-tool cap for high-iteration runs: derived from the
            # FLOORED effective budget above — never the raw stored value —
            # so the cap and the ``asyncio.wait_for`` bound share one number.
            assignment_tool_timeout = resolve_assignment_tool_timeout(
                timeout, effective_max_iterations
            )

            # ── Background-task tools (delegate peers + self-fan-out) ─────
            # Mirrors agent_chat.py: injected when any peer is delegate-typed
            # (or types are empty for backward compat) OR when self-fan-out
            # is enabled — so an agent with no peer handover agents can still
            # spawn parallel background copies of itself in an assignment run
            # (previously chat-only).  Assignment runs are chain
            # roots (delegation depth 0); the spawned worker executes at
            # depth 1, above the self-fan-out gate, so it cannot recurse.
            # The per-run fan-out cap and the cycle guard for non-self
            # targets are enforced inside build_sub_agent_tools.  The WHOLE
            # block — delegate peers included, not just self-fan-out —
            # requires a resolvable acting user (mirrors chat's anonymous-user
            # gate on its background-task block): SubAgentTask.user_id is a
            # users FK, so an ownerless run could never actually spawn —
            # don't advertise a broken tool (nor a denied one).  Self-fan-out
            # needs both the platform and the agent to allow it.
            from app.services.builtin_tools import (
                owned_target_resolver,
                self_fanout_enabled,
            )

            _self_fanout_allowed = (
                self_fanout_enabled(agent)
                and "delegate" in _offered_handover_types
            )
            # An agent holding the agent-builder tools may also delegate to the
            # agents its user owns.
            _owned_resolver = (
                owned_target_resolver(agent_builtin_tool_ids, str(effective_user_id))
                if "delegate" in _offered_handover_types and effective_user_id is not None
                else None
            )
            if (
                _self_fanout_allowed or _needs_delegate or _owned_resolver is not None
            ) and _delegation_user_id is not None:
                try:
                    from app.services.builtin_tools import (
                        build_sub_agent_tools,
                        format_self_fanout_prompt,
                    )

                    _sa_result = build_sub_agent_tools(
                        db,
                        # The run's effective identity (run owner, falling back
                        # to the agent owner) — the same per-user resolution
                        # identity used for quota and the agent-builder tool;
                        # for Platform Health acting alone, the tasks' owner of
                        # record (``identity.health_delegation_owner_id``).
                        # Guaranteed non-None by the block gate above (the old
                        # delegate block's agent-id fallback could never
                        # actually create a task).
                        user_id=str(_delegation_user_id),
                        parent_agent_id=str(agent.id),
                        handover_agents=_peers_by_type.get("delegate", []),
                        parent_run_id=str(run.id),
                        visited_agents=_visited_agents,
                        self_agent=agent,
                        allow_self_fanout=_self_fanout_allowed,
                        # Inherited execution budget: spawned workers
                        # get the assignment's EFFECTIVE wall-clock timeout —
                        # the floored value computed above, not the raw stored
                        # one, so a max_iterations=100 / timeout_seconds=300
                        # parent (effective 2000 s) doesn't watch its workers
                        # die at 300 s. A disabled timeout
                        # (<= 0) propagates RAW as "no per-task deadline" —
                        # the floored value is None there, which the sub-agent
                        # helper would misread as "standalone 900 s default".
                        # The workers also get the run's resolved iteration
                        # cap, so their floor inputs match the parent's.
                        task_timeout_seconds=(
                            assignment.timeout_seconds
                            if timeout is None
                            else int(timeout)
                        ),
                        task_max_iterations=effective_max_iterations,
                        # Every task carries the run's boundaries.
                        element_scope=enforced_scope,
                        handover_type_map=agent.handover_types or {},
                        handover_peers=_configured_handover_peers,
                        resolve_owned_target=_owned_resolver,
                        # The run's per-user picks: its tasks act as the same
                        # accounts (#1792).
                        inherited_template_picks=_run_template_picks,
                    )
                    if _sa_result:
                        composite.add_tools(*_sa_result)

                    # Capability-gated guidance (chat parity): without this
                    # nudge the LLM under-reaches the self-parallelisation
                    # tool.  Inserted BEFORE the safety anchor, same contract
                    # as the delegation block above.  When the agent has no
                    # peers this also supplies the ## Agent Delegation header.
                    if _self_fanout_allowed and _sa_result:
                        from app.services.prompt_guard import (
                            insert_before_safety_anchor as _insert_before_anchor,
                        )

                        system_message = _insert_before_anchor(
                            system_message,
                            format_self_fanout_prompt(has_peers=_handover_loaded),
                        )
                except Exception as _sa_err:
                    logger.warning(
                        "Failed to load background-task tools for assignment: %s",
                        format_exception_message(_sa_err),
                    )

            # ── Inject file context into prompt ──────────────────────
            # The trigger files ride on the user message (``files=``) so
            # every provider inlines their content - extracted text or a
            # native PDF / image block - exactly as the flow runner and the
            # evaluation engine do; the prompt section then says what is
            # true, which is that those files are already in front of the
            # model. A section claiming "already included" over a
            # bare id list left the agent unable to read a trigger file
            # unless it happened to carry a file tool.
            trigger_file_refs = file_context.list_files()
            # What earlier runs of the same work item produced, by reference:
            # listed in the section, readable by id, never inlined.
            from app.llm.base import FileRef as _ItemRunFileRef

            for fa in await load_earlier_item_run_files(db, run):
                if file_context.get(str(fa.id)) is None:
                    file_context.add(_ItemRunFileRef.from_attachment(fa))
            if not file_context.is_empty:
                rendered_prompt += "\n\n" + file_context.to_prompt_section(
                    inlined_file_ids={ref.file_id for ref in trigger_file_refs}
                )

            # Run the agentic loop
            messages = [
                ChatMessage(role="system", content=system_message),
                ChatMessage(
                    role="user",
                    content=rendered_prompt,
                    files=trigger_file_refs or None,
                ),
            ]
            # What the attempt lost to a rollout had done, replayed with the
            # resume note, its secret values restored. The files it
            # produced are registered by reference, as on a HITL resume:
            # the replayed results name them by id.
            if _run_replay is not None:
                if _run_replay.history:
                    messages.extend(assignment_run_resume.restore_history(_run_replay.history))
                from app.llm.base import FileRef as _ResumedFileRef

                for fa in await load_run_generated_files(db, run, exclude_ids=set(trigger_file_ids)):
                    file_context.add(_ResumedFileRef.from_attachment(fa))
                # And every file a replayed result names that the run's acting
                # user stored without the run's stamp: a frame, a
                # contact sheet, a render's captions, same id as before.
                for fa in await assignment_run_resume.replayed_files(
                    db,
                    run,
                    _run_replay.history,
                    exclude_ids=set(trigger_file_ids),
                    user_id=effective_user_id,
                ):
                    if file_context.get(str(fa.id)) is None:
                        file_context.add(_ResumedFileRef.from_attachment(fa))

            # Approval decisions to apply once the tool executor exists.
            # The proposals cannot be resolved here: applying an "approve"
            # means actually running the tool, and the composite executor is
            # not built until later in this function.
            _resume_proposals: list | None = None
            _resume_decisions: dict | None = None

            # If resuming from HITL pause, restore conversation + inject human response
            if (
                original_status == "awaiting_human"
                and run.messages
                and (run.human_input_response or run.approval_decisions)
            ):
                from app.core.redaction import restore_known_secrets
                from app.services.hitl_resume_clock import restamp_prompt_clock
                from app.tools.base import ToolCall

                restored = []
                # Stored with each secret variable value as its marker:
                # the model and the tools get the values back, resolved above.
                for m in restore_known_secrets(run.messages):
                    if m.get("role") == "system" and isinstance(m.get("content"), str):
                        # The clock of the pause is stale by its length (#1645).
                        m = {**m, "content": restamp_prompt_clock(m["content"])}
                    tc_list = None
                    if m.get("tool_calls"):
                        tc_list = [
                            ToolCall(
                                id=tc["id"],
                                name=tc["name"],
                                arguments=tc.get("arguments", {}),
                            )
                            for tc in m["tool_calls"]
                        ]
                    restored.append(
                        ChatMessage(
                            role=m["role"],
                            content=m.get("content", ""),
                            tool_calls=tc_list,
                            tool_call_id=m.get("tool_call_id"),
                            name=m.get("name"),
                            not_run=m.get("not_run"),
                        )
                    )
                messages = restored

                # ``serialize_messages`` persists no ``files``, so
                # the restored user message carries the file SECTION written
                # before the pause and none of the refs it describes - a
                # prompt that claims the trigger files are inlined when they
                # are not, and a FileContext blind to everything the run
                # produced before pausing. Rebuild both honestly:
                # re-attach the (authorized) trigger refs to the first user
                # message, register this run's own generated files by
                # reference, and rewrite the section from what is REALLY on
                # the transcript being sent.
                from app.tools.base import (
                    inlined_user_message_file_ids,
                    strip_file_section,
                )

                _resumed_files = await load_run_generated_files(
                    db, run, exclude_ids=set(trigger_file_ids)
                )
                if _resumed_files:
                    from app.llm.base import FileRef

                    for fa in _resumed_files:
                        file_context.add(FileRef.from_attachment(fa))
                _first_user = next(
                    (m for m in restored if m.role == "user"), None
                )
                if _first_user is not None:
                    _first_user.files = trigger_file_refs or None
                    _first_user.content = strip_file_section(_first_user.content or "")
                    if not file_context.is_empty:
                        _first_user.content += "\n\n" + file_context.to_prompt_section(
                            inlined_file_ids=inlined_user_message_file_ids(restored)
                        )

                if run.approval_decisions:
                    # Per-tool approval resume. Deferred until the
                    # executor exists — see ``_apply_approval_decisions``.
                    #
                    # Note what is NOT done here: matching a pending call by
                    # tool NAME. The free-text path below can get away with that
                    # because only one tool ever raises it, but a turn can call
                    # the same tool several times with different arguments, so
                    # decisions are keyed on ``tool_call_id`` throughout.
                    # The approved calls run with the arguments the model proposed, values restored.
                    _resume_proposals = restore_known_secrets(list(run.pending_approvals or []))
                    _resume_decisions = dict(run.approval_decisions)
                    run.pending_approvals = None
                    run.approval_decisions = None
                else:
                    # Free-text ``request_human_input`` resume — unchanged, and
                    # deliberately still reached when no structured decisions are
                    # present, so runs paused before this feature shipped resume
                    # exactly as they always did.
                    # Find the pending request_human_input tool call id from the
                    # last assistant message.
                    hitl_tc_id = "hitl"
                    hitl_args: dict[str, Any] = {}
                    for m in reversed(run.messages):
                        if m.get("tool_calls"):
                            for tc in m["tool_calls"]:
                                if tc.get("name") == "request_human_input":
                                    hitl_tc_id = tc["id"]
                                    hitl_args = tc.get("arguments") or {}
                                    break
                            break

                    from app.core.redaction import defuse_secret_markers
                    from app.services.decision_ask import run_answer_text

                    messages.append(
                        ChatMessage(
                            role="tool",
                            # What a person wrote: a marker in it is never restored.
                            # The options are the paused call's own arguments: a reply naming
                            # one (its key or label) resumes as that option, any other is free text.
                            content=run_answer_text(
                                hitl_args if isinstance(hitl_args, dict) else {},
                                defuse_secret_markers(run.human_input_response),
                            ),
                            tool_call_id=hitl_tc_id,
                            name="request_human_input",
                        )
                    )
                # Clear HITL state
                run.human_input_question = None
                run.human_input_response = None
                run.status = "running"
                await db.commit()
                await _publish_run_status(
                    str(run.user_id),
                    str(run.id),
                    "running",
                    assignment.name,
                    is_company=run.is_company_managed,
                )

            if assignment.dry_run:
                messages.append(
                    ChatMessage(
                        role="system",
                        content="[DRY RUN] Do NOT execute actions. Describe the plan: tools, parameters, expected outcomes.",
                    )
                )

            # ── Enrich the workflow trace with the assembled payload ─────
            # The root trace was opened early (pre-activation-gate) with a
            # placeholder input.  Now that messages/system_prompt/tools are
            # fully assembled, set the trace-card input via
            # ``enrich_trace_input`` and add filterable metadata fields
            # (tool counts, max_iterations) via ``enrich_trace_metadata``.
            if wf_trace_ctx is not None:
                enrich_trace_input(
                    wf_trace_ctx,
                    build_trace_input(
                        messages=messages,
                        system_prompt=system_message,
                        tools=composite.all_tools,
                        extra={
                            "assignment": assignment.name,
                            "agent": agent.name,
                            **(
                                {"files": serialize_file_refs(file_context.list_files())}
                                if not file_context.is_empty
                                else {}
                            ),
                        },
                    ),
                )
                enrich_trace_metadata(
                    wf_trace_ctx,
                    {
                        "has_tools": bool(composite.all_tools),
                        "tool_count": len(composite.all_tools),
                        "max_iterations": effective_max_iterations,
                    },
                )
                # fleet_id, and caller / target fleet ids on a delegated run
                # whose parent's home fleet is another one.
                from app.services.fleet_runs import parent_run_agent_id, run_fleet_trace_metadata

                enrich_trace_metadata(
                    wf_trace_ctx,
                    await run_fleet_trace_metadata(
                        db,
                        agent.id,
                        await parent_run_agent_id(db, getattr(run, "parent_run_id", None)),
                    ),
                )

            # ``timeout`` (the floored effective wall-clock budget of the run)
            # and the derived per-tool cap
            # (``assignment_tool_timeout``) were computed right after
            # ``effective_max_iterations`` — before the background-task tools,
            # which hand the budget to spawned workers as the inherited
            # budget.  Bind the ``_loop_started`` default here, BEFORE the try
            # block, so the ``except TimeoutError`` handler can never hit an
            # UnboundLocalError when an inner await raises TimeoutError before
            # the ``asyncio.wait_for`` line.
            _loop_started = time.monotonic()
            _stale_refs: StaleReferenceCollector | None = None
            # The bound of the ``asyncio.wait_for`` below: that budget, never
            # past what the execution's deadline leaves - equal on a
            # first execution, shorter on a re-run after a worker restart. Read
            # after the tool setup, which the deadline counts too.
            run_timeout = bound_by_deadline(timeout, deadline_at)
            deadline_cut = deadline_cut_budget(timeout, run_timeout)
            if deadline_cut:
                logger.info(
                    "Assignment run %s runs on the %ds its deadline leaves "
                    "(budget %ds): it started late or re-runs a lost attempt, "
                    "and keeps the deadline it was created with",
                    run_id,
                    int(run_timeout or 0),
                    int(timeout or 0),
                )
            if deadline_spent(deadline_at):
                # The setup used what the deadline left: a loop started now
                # could only time out.
                raise DeadlineCutTimeoutError(
                    deadline_spent_message("Assignment run", deadline_at)
                )

            try:
                # ── Apply tool deny list (security backstop) ──────────
                effective_tools = composite.all_tools
                # Per-function built-in scoping: the agent's and the
                # assignment's lists intersect, as for integrations; applied
                # before the deny list and the progressive tool gate.
                _builtin_allowed = merge_tool_allow_lists(
                    getattr(agent, "builtin_allowed_tools", None) or {},
                    getattr(assignment, "builtin_allowed_tools", None) or {},
                )
                if _builtin_allowed:
                    from app.services.builtin_tools import filter_builtin_tools
                    from app.services.builtin_tools.filtering import (
                        builtin_tools_outside_scope,
                        describe_outside_builtin_scope,
                    )

                    # Named, never hidden: load_tools and a call say the
                    # function exists outside the allowlist (#12192).
                    for _name, _group in builtin_tools_outside_scope(effective_tools, _builtin_allowed).items():
                        composite.withhold(
                            [_name],
                            describe_outside_builtin_scope(_group, scope="this agent's or this assignment's"),
                            registered=True,
                        )
                    effective_tools = filter_builtin_tools(
                        effective_tools, _builtin_allowed
                    )
                if agent.tool_deny_list:
                    from app.services.builtin_tools import apply_tool_deny_list

                    effective_tools = apply_tool_deny_list(
                        effective_tools, agent.tool_deny_list
                    )

                # --- Per-tool approval gate ------------------------
                # Resolve everything the gate needs ONCE for this run and close
                # over it. Whitelist membership and standing approvals are
                # consulted on EVERY tool call, so querying them per call would
                # put a database round-trip on the hot path.
                #
                # Fail closed (Principle I): if either lookup fails we cannot
                # know what is exempt, so we gate everything rather than
                # treating "could not read" as "nothing is whitelisted" — the
                # latter happens to be safe here, but it would also record every
                # call as gated-by-policy when it was really gated-by-outage.
                #
                # Declared outside the branch so the pause handler can drain it
                # unconditionally, without caring whether gating was enabled.
                _stale_approvals: set[str] = set()
                # Gate outcome per tool_call_id, for the audit trail (FR-020).
                # Recorded for calls that RAN — a paused call has no outcome yet,
                # it gets one from the human's decision on resume.
                _gate_outcomes: dict[str, str] = {}

                # Calls a human just approved on this resume. They must not
                # re-gate when executed below, or the run would pause again
                # on the very action the reviewer signed off.
                # ``is_approval``, not ``!= "reject"``: this set skips the
                # gate outright, and ``run.approval_decisions`` is untyped
                # JSON, so a verdict spelled "rejected" would pass a
                # negative test and hand a refused call a free pass. Same
                # allow-list the applier and the other three call sites use.
                from app.tools.approval import fingerprint as _fingerprint
                from app.tools.approval import is_approval as _is_approval

                _approved_ids = {
                    cid
                    for cid, d in (_resume_decisions or {}).items()
                    if _is_approval((d or {}).get("decision"))
                }

                # The shape each approved call was shown as, so the bypass
                # can be checked against what the tool is at execution time
                # (FR-002c4, spec.md:207). A paused run has no clock on it,
                # so without this a run resumed later ran against whatever
                # the tool had since become.
                _approved_shapes = {
                    str(p.get("tool_call_id")): _fingerprint(p.get("input_schema"))
                    for p in (_resume_proposals or [])
                    if str(p.get("tool_call_id")) in _approved_ids
                }

                # Every gate this run installs for an agent that asks still
                # pauses a call of a tool that always asks - ``always_ask``, or
                # a set ``always_ask_argument`` - through the helper chat uses.
                # Approval is opt-in: an agent that does not ask gets the
                # ungated gate, and where an administrator turned approvals
                # off for the platform, nothing pauses.
                from app.services.hitl_whitelist import (
                    make_always_ask_gate,
                    make_recording_gate,
                    make_ungated_gate,
                    resolve_platform_kill_switch,
                )
                from app.tools.approval import OUTCOME_APPROVALS_DISABLED

                _always_ask = partial(
                    make_always_ask_gate,
                    composite,
                    approved_call_ids=_approved_ids,
                    approved_shapes=_approved_shapes,
                    outcome_sink=_gate_outcomes,
                )
                if _gate_on:
                    # The two wholesale switches — the platform-wide
                    # kill switch and this user's own allow-all mode — are
                    # resolved by the same helper chat uses, so a scheduled run
                    # and a conversation cannot disagree about whether approvals
                    # are off. When suppressed, a recording-only gate is
                    # installed instead of none at all: every call still gets a
                    # truthful audit outcome (FR-020) naming why it ran
                    # unattended.
                    from app.services.hitl_whitelist import (
                        make_recording_gate,
                        resolve_gate_suppression,
                    )

                    _suppressed_outcome = await resolve_gate_suppression(
                        db,
                        effective_user_id,
                        is_company_managed=bool(run.is_company_managed),
                    )
                else:
                    _suppressed_outcome = await resolve_platform_kill_switch(db)

                if _suppressed_outcome == OUTCOME_APPROVALS_DISABLED:
                    composite.set_approval_gate(
                        make_recording_gate(_suppressed_outcome, _gate_outcomes)
                    )
                elif _gate_on and _suppressed_outcome:
                    composite.set_approval_gate(
                        _always_ask(make_recording_gate(_suppressed_outcome, _gate_outcomes))
                    )
                elif not _gate_on:
                    composite.set_approval_gate(make_ungated_gate())
                else:
                    from app.services.hitl_whitelist import (
                        WhitelistUnavailableError,
                        gate_reach,
                        resolve_vetoed,
                        resolve_whitelist,
                    )
                    from app.services.tool_approval_service import (
                        ApprovalPreferencesUnavailableError,
                        list_active_exceptions,
                    )
                    from app.tools.approval import evaluate_gate, fingerprint

                    _reach: dict[str, set[str]] = {}
                    try:
                        _run_whitelist = await resolve_whitelist(db, reach=_reach)
                        _run_vetoed = await resolve_vetoed(db)
                    except WhitelistUnavailableError:
                        logger.error(
                            "HITL whitelist unavailable for run %s — gating all "
                            "tool calls",
                            run.id,
                        )
                        _run_whitelist = frozenset()
                        _run_vetoed = frozenset()
                    _run_reach = gate_reach(_reach)

                    _run_exception_sources: dict[str, str] = {}
                    _run_exception_entity_ids: dict[str, str] = {}
                    try:
                        _run_exceptions = await list_active_exceptions(
                            db,
                            effective_user_id,
                            sources=_run_exception_sources,
                            entity_ids=_run_exception_entity_ids,
                        )
                    except ApprovalPreferencesUnavailableError:
                        logger.error(
                            "Standing approvals unavailable for run %s — gating "
                            "all tool calls",
                            run.id,
                        )
                        _run_exceptions = {}

                    # Sources whose tools skip approval: the per-platform
                    # policy for registered Suite platforms, the coarse
                    # per-integration toggle for everything else. Resolved by the
                    # shared helper rather than re-queried here, so a run and a
                    # conversation cannot reach opposite verdicts about the same
                    # tool — an inline copy here did exactly that, honouring the
                    # integration toggle while never reading the platform policy.
                    # It fails closed internally: an unreadable toggle keeps the
                    # source gating.
                    from app.services.hitl_whitelist import resolve_exempt_sources

                    _exempt_sources = await resolve_exempt_sources(db)

                    # ``_stale_approvals`` collects tools whose standing
                    # approval no longer describes them. Collected rather than
                    # revoked on the spot: the gate is synchronous by design (it
                    # sits on the hot path of every tool call and must stay free
                    # of I/O), while revoking is an async database write. The
                    # pause handler drains it.
                    def _approval_gate(call) -> None:
                        _defn = composite.get_tool_definition(call.name)
                        _live_fp = fingerprint(
                            _defn.input_schema if _defn else None
                        )

                        if call.id in _approved_ids:
                            _shown = _approved_shapes.get(call.id)
                            if _shown is None or _shown == _live_fp:
                                return
                            from app.tools.approval import (
                                OUTCOME_PAUSED_TOOL_CHANGED,
                            )

                            _gate_outcomes[call.id] = OUTCOME_PAUSED_TOOL_CHANGED
                            raise HumanInputRequestedError(
                                question=(
                                    f"Approval required to run {call.name}: it "
                                    "changed since you were asked"
                                ),
                                tool_call_id=call.id,
                                conversation=[],
                            )

                        _decision = evaluate_gate(
                            call.name,
                            hitl_enabled=True,
                            whitelist=_run_whitelist,
                            active_exceptions=_run_exceptions,
                            current_fingerprint=_live_fp,
                            tool_source=_defn.source if _defn else "",
                            # The connection's OWN id, not just its provider.
                            # A per-connection switch emits
                            # `integration:<provider>:<id>` and the bare form
                            # means a platform-wide policy, so omitting this
                            # left an operator's switch working in chat and
                            # inert on every scheduled run.
                            tool_entity_id=_defn.entity_id if _defn else None,
                            exempt_sources=_exempt_sources,
                            vetoed=_run_vetoed,
                            whitelist_reach=_run_reach,
                            exception_sources=_run_exception_sources,
                            exception_entity_ids=_run_exception_entity_ids,
                        )
                        if _decision.invalidated_preference:
                            _stale_approvals.add(call.name)
                            # Drop it from the in-memory map too, so the rest of
                            # this run cannot match against consent we are about
                            # to revoke.
                            _run_exceptions.pop(call.name, None)
                        if _decision.log_outcome:
                            _gate_outcomes[call.id] = _decision.log_outcome
                        if _decision.action == "pause":
                            raise HumanInputRequestedError(
                                question=f"Approval required to run {call.name}",
                                tool_call_id=call.id,
                                conversation=[],
                            )

                    composite.set_approval_gate(_always_ask(_approval_gate))

                # The fleet's gates still waiting at its autonomy level pause
                # their tool calls (#9769 S1b), around whichever gate the run
                # installed, only when an administrator opted the fleet in
                # (#1467); the platform kill switch wins.
                if (
                    _fleet_autonomy is not None
                    and _fleet_enforced
                    and _suppressed_outcome != OUTCOME_APPROVALS_DISABLED
                ):
                    from app.services.fleet_autonomy import gated_tool_rules, make_fleet_autonomy_gate

                    _fleet_rules = gated_tool_rules(*_fleet_autonomy)
                    if _fleet_rules:
                        composite.set_approval_gate(
                            make_fleet_autonomy_gate(
                                composite.approval_gate, _fleet_rules, approved_call_ids=_approved_ids
                            )
                        )

                # Apply approval decisions now that tools can actually run.
                # Deferred from the resume block above, which executes
                # before the composite executor exists.
                if _resume_decisions is not None:
                    # No executor when the offered surface is empty, and never
                    # the bare composite: it keeps executors registered for
                    # denied tool names (see below), so falling back to it
                    # here ran a deny-listed call with the gate already
                    # bypassed. Same contract both chat paths carry — an
                    # approval is not a licence to run a tool this assignment no
                    # longer offers.
                    _resume_executor = (
                        ScopedToolExecutor(composite, effective_tools)
                        if effective_tools
                        else None
                    )
                    _resume_verdicts = _resume_decisions
                    if not effective_tools:
                        _resume_verdicts = {
                            _cid: {
                                **(_d or {}),
                                "decision": "reject",
                                "rejection_reason": (
                                    "This tool is no longer available to this "
                                    "assignment, so the approved call was not "
                                    "run."
                                ),
                                "source": "not_offered",
                            }
                            for _cid, _d in _resume_decisions.items()
                        }
                    await _apply_approval_decisions(
                        executor=_resume_executor,
                        messages=messages,
                        proposals=_resume_proposals or [],
                        decisions=_resume_verdicts,
                        outcomes=_gate_outcomes,
                        cancel_event=cancel_event,
                    )
                    # The pause suspended the whole batch, not just its gated
                    # calls. Anything the gate had cleared is still sitting
                    # there without a result, and resuming with an orphaned
                    # ``tool_use`` block is rejected outright.
                    await _dispatch_calls_the_pause_left_behind(
                        executor=_resume_executor,
                        messages=messages,
                        outcomes=_gate_outcomes,
                        cancel_event=cancel_event,
                    )

                # Build keyword arguments for the agentic loop.
                #
                # Execution-time enforcement (fail closed): the
                # composite keeps executors registered for denied tool names
                # and the agent loop dispatches tool calls without
                # re-checking the offered list — a prompt-injected model
                # could otherwise call a denied tool by its registered name
                # and have it execute.  Wrap the composite in a
                # ScopedToolExecutor so only the offered tool surface
                # (``effective_tools``) is executable.
                # The Module Analyst's every model call is reserved and booked on
                # its own ceiling ledger (#101 S14b-2, dev-docs 12.5), decided by
                # the same server-only predicate as its quota exemption.
                _loop_llm = llm
                if _module_analyst_exempt:
                    from app.services.health.module_analyst_ceiling import ModuleAnalystCeilingLLM

                    _loop_llm = ModuleAnalystCeilingLLM(llm, model=wf_resolved_model)
                loop_kwargs: dict = dict(
                    llm=_loop_llm,
                    messages=messages,
                    tools=effective_tools if effective_tools else None,
                    executor=(
                        None
                        if assignment.dry_run
                        else (
                            ScopedToolExecutor(composite, effective_tools)
                            if effective_tools
                            else None
                        )
                    ),
                    max_iterations=effective_max_iterations,
                    model=wf_resolved_model,
                    file_context=file_context,
                    wait_policy=effective_wait_policy,
                    cancel_event=cancel_event,
                    # No skip event: an assignment run has no live user to press
                    # "continue now".  Cancelling the run still ends a pause
                    # immediately, through the orchestration cancel path.
                    wait_condition=make_task_condition(
                        str(run.user_id) if run.user_id else None,
                        agent_id=str(agent.id),
                    ),
                )
                # A run still making progress at its cap keeps going in blocks,
                # within the assignment's own timeout: the loop winds
                # down on the wall clock below like any run.
                from app.models.general_assistant_config import iteration_reach

                _iteration_reach = iteration_reach(effective_max_iterations)
                if _iteration_reach > effective_max_iterations and not assignment.dry_run:
                    loop_kwargs["extend_iterations_to"] = _iteration_reach
                # Checkpointed: a worker asked to stop winds the run down at the
                # next iteration boundary and hands it back.
                _run_checkpoint = assignment_run_resume.checkpoint_for(
                    run.id,
                    replay=_run_replay,
                    attempt=attempt,
                    place=attempt_place,
                    user_id=effective_user_id,
                )
                loop_kwargs["on_checkpoint"] = _run_checkpoint.offer
                # Tool groups the assignment declares are on the wire from the
                # first request, so the cached prompt prefix holds all run.
                _preload_groups = assignment_dispatch.preload_tool_groups(
                    assignment.trigger_config
                )
                if _preload_groups:
                    loop_kwargs["preload_tool_groups"] = _preload_groups
                # Pass agent-level temperature if configured
                if agent.temperature is not None:
                    loop_kwargs["temperature"] = agent.temperature

                # Per-run spending budget (env-configured, every
                # dimension 0 = disabled): cost / tokens / tool calls /
                # duration in ONE object enforced with
                # reserve -> call -> debit -> block.  Pricing is resolved
                # here, before the loop, so the loop never holds a DB
                # connection for accounting (none is held across the run at all
                # — see ``release_db_connection_before_loop`` below); an
                # unconfigured deployment issues no query and gets no budget,
                # and when no price row is known the cost dimension simply is
                # not enforced.
                #
                # This supersedes the old soft token wind-down: when the budget
                # object exists, its token limit is a HARD block (the call that
                # would cross it is refused) rather than a nudge.
                # The agent's own Budget tab narrows (or, when an admin
                # authorised it, widens) the platform configuration for this
                # run. An agent with no budget configured resolves to
                # the platform values exactly as before.
                from app.services import fleet_runs as _fleet_runs

                _run_budget = await build_run_budget_for_run(
                    db,
                    model_id=wf_resolved_model or getattr(llm, "default_model", None),
                    provider=getattr(llm, "provider_name", None),
                    max_duration_seconds=run_timeout,
                    # The agent's own budget, its unset dimensions filled
                    # from its home fleet's defaults.
                    overrides=_fleet_runs.member_budget_overrides(
                        agent.budget_config, await _fleet_runs.fleet_run_defaults(db, agent)
                    ),
                    user_id=str(run.user_id) if run.user_id else None,
                )
                if _run_budget is not None:
                    loop_kwargs["budget"] = _run_budget
                if assignment_tool_timeout is not None:
                    loop_kwargs["tool_timeout_seconds"] = assignment_tool_timeout
                    # Surface the derived cap in the run logs — this exists
                    # because of a production incident and ops need
                    # to see the effective per-tool budget when triaging.
                    logger.info(
                        "High-iteration assignment run: capping per-tool "
                        "timeout at %ds (timeout=%ss, max_iterations=%d)",
                        assignment_tool_timeout,
                        timeout,
                        effective_max_iterations,
                    )

                # Fast-path for trusted validator/finalizer tools. This is
                # intentionally runtime control, not prompt text: when the
                # configured tool returns a valid final object, the loop can
                # return it directly and skip the expensive final LLM call.
                _agent_config = agent.config if isinstance(agent.config, dict) else {}
                _finalize_on_tool = _agent_config.get("finalize_on_builtin_tool")
                if isinstance(_finalize_on_tool, dict) and _finalize_on_tool:
                    loop_kwargs["finalize_on_tool"] = _finalize_on_tool
                    if isinstance(agent.output_schema, dict):
                        loop_kwargs["finalize_output_schema"] = agent.output_schema
                # ``$input``: a transformer references its input instead of
                # regenerating it (config.output_mode "patch" or input_reference).
                from app.tools.session_input import input_reference_enabled

                if input_reference_enabled(_agent_config):
                    loop_kwargs["input_reference"] = True

                # Build native structured output config when agent has a JSON schema
                if agent.output_format == "json" and agent.output_schema:
                    from app.services.agent_chat import _build_native_structured_output

                    _so = _build_native_structured_output(llm, agent.output_schema)
                    if _so:
                        loop_kwargs["structured_output"] = _so

                # The agentic loop can run for minutes. End any open transaction
                # first so this session doesn't pin a pool connection the whole time.
                await release_db_connection_before_loop(db)

                # Re-bind right before the wait_for so the elapsed time in the
                # disabled-timeout error message measures only the loop itself.
                _loop_started = time.monotonic()
                # And what the deadline leaves now, after the awaits since the
                # first reading (the budget lookup, the connection release).
                run_timeout = bound_by_deadline(timeout, deadline_at)
                deadline_cut = deadline_cut_budget(timeout, run_timeout)
                # Propagate the wall-clock deadline so the loop's
                # forced-final-answer backstop can cap its per-LLM-call
                # timeout to the remaining budget instead of using the full
                # 900 s _LLM_CALL_TIMEOUT — which exceeds the outer
                # wait_for and causes the run to fail with a hard timeout
                # error and no output when the backstop fires near the end
                # of the assignment's time budget.
                if run_timeout is not None:
                    loop_kwargs["wall_clock_deadline"] = _loop_started + run_timeout
                # Only this run's own integrations: a consulted agent's are its
                # configuration, not this assignment's. The dispatch
                # scope attributes a 429 met in the loop to this assignment,
                # which then backs off.
                # A message of the same conversation and sender arriving while
                # this chat turn runs is injected as a user steer.
                from app.services import chat_turn_steering
                from app.services.health.sandbox.turns import agent_turn_session, turn_binding

                # The loop runs here, not through ``run_agent_chat``: a Platform Health
                # run binds its dry-run session itself (#8255).
                _turn_session = await agent_turn_session(
                    db, str(run.user_id) if run.user_id else "anonymous", agent
                )
                # A note another writer adds to the work item this run holds
                # reaches the loop the same way (work_item_note_steering).
                from app.services import work_item_note_steering

                async with (
                    chat_turn_steering.session(
                        getattr(run, "trigger_payload", None),
                        assignment.trigger_config,
                        functools.partial(_requeue_unsteered, assignment.id),
                    ) as _chat_steering,
                    work_item_note_steering.session(
                        None if assignment.dry_run else run.id,
                        getattr(run, "trigger_payload", None),
                    ) as _note_steering,
                ):
                    if _chat_steering is not None and not assignment.dry_run:
                        loop_kwargs["steering_queue"] = _chat_steering
                    elif _note_steering is not None:
                        loop_kwargs["steering_queue"] = _note_steering
                    with (
                        collect_stale_references(
                            str(i) for i in effective_bindings.values() if i
                        ) as _stale_refs,
                        assignment_dispatch.dispatch_scope(assignment.id),
                        # The run's frictions (tool errors...) are recorded
                        # under it with no model call (services/frictions.py).
                        binding_friction_scope(
                            make_friction_scope(
                                agent.id,
                                user_id=run.user_id,
                                run_id=run.id,
                                assignment_id=assignment.id,
                                work_item_id=(getattr(run, "trigger_payload", None) or {}).get("work_item_id"),
                            )
                        ),
                        turn_binding(_turn_session),
                    ):
                        loop_result = await asyncio.wait_for(
                            run_agent_loop(**loop_kwargs),
                            timeout=run_timeout,
                        )
            except TimeoutError:
                raise _loop_timeout_error(
                    run_timeout,
                    timeout,
                    time.monotonic() - _loop_started,
                    effective_max_iterations,
                    deadline_cut=deadline_cut,
                ) from None
            except HumanInputRequestedError as hitl:
                end_trace(wf_trace_ctx, output="Paused for human input")

                # Revoke standing approvals the gate found stale (FR-019a).
                # Done here because this is the first async point after the
                # synchronous gate detected the mismatch.
                #
                # Without this the tool correctly re-prompts, but the user's
                # preference list still claims they always allow it — a visible
                # contradiction with no explanation. Worse, if the tool's schema
                # ever changed back, the untouched row would silently start
                # applying again, resurrecting consent that should have lapsed.
                if _stale_approvals and effective_user_id:
                    from app.services.tool_approval_service import (
                        REASON_TOOL_CHANGED,
                        invalidate_stale_approvals,
                    )

                    try:
                        await invalidate_stale_approvals(
                            db,
                            user_id=effective_user_id,
                            tool_keys=_stale_approvals,
                            reason=REASON_TOOL_CHANGED,
                        )
                    except Exception:
                        # Never let bookkeeping cost the pause itself: the run
                        # must still be persisted as awaiting_human, and the
                        # stale row is safe (it re-prompts either way).
                        logger.exception(
                            "Failed to revoke stale approvals for run %s", run.id
                        )

                # --- Assignment paused for human input ---
                run.status = "awaiting_human"
                from app.services.hitl_resume_clock import mark_paused

                mark_paused(run)
                # Persist the trace id while the run waits (possibly days)
                # for a human response — the resumed execution opens a new
                # trace and overwrites it on completion/failure.
                apply_run_usage_accounting(run, None, wf_trace_ctx)
                run.human_input_question = hitl.question
                # Structured approval proposals, when the pause came from the
                # per-tool gate rather than from ``request_human_input``.
                # Left as None for a free-text pause, which is what the resume
                # path branches on — so a run paused by request_human_input,
                # including one paused before this feature shipped, still
                # resumes down the original path.
                run.pending_approvals = getattr(hitl, "proposals", None)
                if run.pending_approvals:
                    # An approval pause is a friction even when the run later
                    # completes (services/frictions.py), one per tool asked.
                    from app.services.frictions import note_approval_pause

                    note_approval_pause(run, assignment, run.pending_approvals)
                run.notification_sent = True  # HITL always notifies
                # Persist the full conversation so we can resume later
                from app.services.builtin_tools import serialize_messages

                run.messages = serialize_messages(hitl.conversation)
                if _elsewhere := await _ended_elsewhere(db, run, place=attempt_place):
                    _replaced = _elsewhere == REPLACED_ATTEMPT
                    return
                own_ending = True
                # Its resume counts attempts from 1 again.
                await open_pause_epoch(db, run)
                try:
                    await db.commit()
                except StaleDataError:
                    logger.warning(
                        "Run %s was deleted during HITL pause — aborting gracefully",
                        run_id,
                    )
                    return
                # Cleanup MCP
                from app.services.builtin_tools import cleanup_mcp_manager

                await cleanup_mcp_manager(mcp_manager)
                # Always send notification for HITL (bypasses notify_on_complete)
                await _publish_run_status(
                    str(run.user_id),
                    str(run.id),
                    "awaiting_human",
                    assignment.name,
                    f"🖐️ {hitl.question}",
                    is_company=run.is_company_managed,
                )

                # Live approval prompt. Emitted right after the status
                # event so a reviewer already watching the run sees the request
                # appear rather than waiting for the next poll.
                #
                # Best-effort: the proposals are persisted on the run, so a
                # client that missed this still finds them. Never let it cost
                # the pause itself.
                if run.pending_approvals:
                    try:
                        from app.services.assignment_helpers import (
                            is_interactive_run,
                            publish_action_proposal,
                        )

                        await publish_action_proposal(
                            str(run.user_id),
                            str(run.id),
                            assignment.name,
                            run.pending_approvals,
                            is_company=run.is_company_managed,
                            is_autonomous=not is_interactive_run(run),
                        )
                    except Exception:
                        logger.exception(
                            "action_proposal publish failed for run %s", run.id
                        )

                try:
                    from app.services.notify import deliver_notification

                    await deliver_notification(db, run, assignment, agent)
                except Exception as notif_err:
                    # error (was warning): the user never learns the run is
                    # waiting for their input — the run stalls until timeout.
                    logger.error(
                        "HITL notification failed for run %s: %s",
                        run_id,
                        format_exception_message(notif_err),
                        exc_info=True,
                    )
                logger.info(
                    "Assignment '%s' paused for human input: %s",
                    assignment.name,
                    hitl.question[:100],
                )
                return  # Exit — do not mark completed

            # A run that ends on a terminal control-flow stop — the wall
            # clock ran out, the user cancelled, the per-run budget was
            # spent — still *completes*: the loop returns a real answer and
            # the run is recorded as a success. That is the right outcome and
            # the wrong record, so the reason rides onto the trace metadata.
            # Not a failure cause: nothing failed.
            _terminal_stop = getattr(loop_result, "stop_reason", None)
            if _terminal_stop == "cancelled":
                # An out-of-band cancel (emergency stop, operator) already
                # wrote ``cancelled``: the completion path would erase it, so
                # the run keeps the cancel's status and records its usage. The
                # guarded UPDATE only fills the status when the cancel reached
                # the execution but not the row.
                end_trace(
                    wf_trace_ctx,
                    output="Cancelled",
                    usage=loop_result.usage,
                    model=loop_result.model,
                )
                apply_run_usage_accounting(run, loop_result.usage, wf_trace_ctx)
                await db.execute(
                    update(AssignmentRun)
                    .where(
                        AssignmentRun.id == run.id,
                        AssignmentRun.status.in_(ACTIVE_RUN_STATUSES),
                    )
                    .values(status="cancelled", completed_at=datetime.now(UTC))
                )
                await db.commit()
                logger.info(
                    "Assignment '%s' run %s stopped by an out-of-band cancel",
                    assignment.name,
                    run_id,
                )
                return
            if _terminal_stop and _terminal_stop not in {"end_turn", "tool_use"}:
                with contextlib.suppress(Exception):
                    enrich_trace_metadata(
                        wf_trace_ctx, {"agent_stop_reason": _terminal_stop}
                    )
            from app.tools.agent_loop import deadline_stop_without_output

            if deadline_stop_without_output(loop_result):
                # The wall clock ran out before any model call produced a
                # token: the answer is the forced-answer notice, not work.
                # The deadline's timeout (free, ``engine_deadline_exceeded``,
                # ``run.failed``), never a completion. Its input usage is
                # still accounted by the failure path.
                _finished_loop = loop_result
                raise DeadlineCutTimeoutError(
                    "Assignment run stopped by its deadline before any model "
                    "output: " + deadline_spent_message("Assignment run", deadline_at)
                )

            # The agent finished, but an integration reported a resource this
            # run uses as missing in separate runs: the configuration is
            # broken, so the run fails - keeping what the agent produced.
            if _stale_refs is not None and _stale_refs.references:
                run.result_summary = select_best_result_summary(
                    loop_result.content, messages
                )
                run.result_summary = await _shown_result_summary(
                    db, run, effective_bindings, guard=False
                )
                await _link_run_output_files(db, run, file_context, trigger_file_ids)
                _finished_loop = loop_result
                raise await _stale_reference_error(db, _stale_refs)

            # Record result
            end_trace(
                wf_trace_ctx,
                output=loop_result.content,
                usage=loop_result.usage,
                model=loop_result.model,
            )
            if _elsewhere := await _ended_elsewhere(db, run, place=attempt_place):
                _replaced = _elsewhere == REPLACED_ATTEMPT
                return
            own_ending = True
            run.status = "completed"
            # Why the run stopped before its task was done: the
            # iteration cap, the wall clock, the per-run budget... The run
            # still completes with the answer the loop produced, but the run
            # record says it is partial instead of passing for a clean
            # success. Written on every ending, so a retried or resumed run
            # that finishes is not left flagged by an earlier attempt.
            from app.tools.agent_loop import unfinished_stop_reason

            run.stop_reason = unfinished_stop_reason(loop_result)
            # Persist accumulated token usage + Langfuse trace id on the
            # run (best-effort — never fails the run).
            apply_run_usage_accounting(run, loop_result.usage, wf_trace_ctx)
            # ``loop_result.content`` is only the FINAL assistant message.
            # When the LLM emits the actual deliverable in an earlier
            # iteration (e.g. combined with a tool call) and ends with a
            # brief recap like "Your digest is ready above!", that final
            # message references content the user never sees in the Result
            # card or notification. ``select_best_result_summary`` walks
            # the full conversation and preserves the deliverable.
            run.result_summary = select_best_result_summary(
                loop_result.content, messages
            )
            if integration_notice and agent.output_format != "json":
                run.result_summary = (
                    f"{run.result_summary}\n\n{integration_notice}"
                    if run.result_summary
                    else integration_notice
                )
            # A run whose integration calls failed authentication is degraded,
            # never a plain success: the record names the integrations, and
            # the result says which data it had (#1454).
            run.integration_auth_failures = auth_guard.failures() or None
            _coverage_notice = auth_guard.coverage_notice()
            if _coverage_notice and agent.output_format != "json":
                run.result_summary = (
                    f"{run.result_summary}\n\n{_coverage_notice}" if run.result_summary else _coverage_notice
                )
            # No called integration was readable: the run says so on its
            # status line (run list, get_run, CTEM cards) instead of passing
            # for a plain success; a limit stop keeps its own reason (#1589).
            if run.stop_reason is None and auth_guard.unreadable():
                from app.services.integration_auth_guard import UNREADABLE_STOP_REASON

                run.stop_reason = UNREADABLE_STOP_REASON

            # ── Surface matplotlib charts produced by the code interpreter ─
            # Persist each chart as a durable run file and append it to the
            # result, mirroring the chat paths so charts show up in assignment
            # runs too. For ``json`` output the structured-output
            # step below re-serialises ``result_summary`` from
            # ``loop_result.content``, so the charts stay persisted as run files
            # without corrupting the JSON payload.
            if code_images:
                from app.services.code_interpreter import inject_code_images

                run.result_summary = await inject_code_images(
                    run.result_summary or "",
                    code_images,
                    db=db,
                    user_id=_run_file_owner_id or "",
                    assignment_run_id=str(run.id),
                )
            if run_charts or "[[CHART:" in (run.result_summary or "").upper():
                from app.services.chat_charts import inject_charts

                run.result_summary = inject_charts(run.result_summary or "", run_charts)

            # ── Link output files to the assignment run ──────────────
            await _link_run_output_files(db, run, file_context, trigger_file_ids)

            # ── Structured output validation (SAP) ─────────────────────
            # ``emit_repaired_json`` re-serializes the repaired
            # payload as soon as it parses (schema validation errors are
            # log-only) and returns None on parse failure or root-type
            # mismatch, in which case ``result_summary`` keeps whatever
            # ``select_best_result_summary`` selected above.  A payload the
            # per-run budget cut short is kept as it came, ending
            # with the budget message: repairing it would re-serialize a
            # partial payload as a complete one.
            from app.tools.agent_loop import answer_cut_by_budget

            _repaired_json: str | None = None
            if (
                agent.output_format == "json"
                and agent.output_schema
                and loop_result.content
                and not answer_cut_by_budget(loop_result)
            ):
                from app.services.structured_output import (
                    emit_repaired_json_in_thread,
                )

                # In a thread: repairing a large answer is CPU-bound and would
                # block the worker's event loop (and its health probe).
                _repaired_json = await emit_repaired_json_in_thread(
                    loop_result.content,
                    agent.output_schema,
                    log_context=f"assignment {assignment.id}",
                )
                if _repaired_json is not None:
                    run.result_summary = _repaired_json

            # ── Platform URL guard: validate / auto-correct deep links ─
            # Deterministic post-processing on the deliverable
            # surface (Result card + notifications), and a link on a
            # platform's internal URL shown on its registered URL,
            # applied once to the summary the run keeps. A repaired JSON
            # payload only has its links moved: the guard never rewrote it.
            run.result_summary = await _shown_result_summary(
                db,
                run,
                effective_bindings,
                guard=_repaired_json is None,
            )
            # A JSON-output agent's degraded coverage goes in a ``_coverage``
            # key of its JSON object, where its readers look, instead of the
            # prose notice that would break the payload (#1589).
            if agent.output_format == "json" and run.integration_auth_failures:
                from app.services.integration_auth_guard import with_coverage_key

                _with_coverage = with_coverage_key(run.result_summary, auth_guard.coverage_record())
                if _with_coverage is not None:
                    run.result_summary = _with_coverage

            # ── ROI: units of work this run produced (#8990, #9285) ──
            # The declaration is resolved (assignment, agent, legacy
            # ``x-xtm-roi`` of a JSON-output agent, home fleet); ``per_run``
            # counts any output format. Best effort, own savepoint.
            from app.services.roi_records import record_run_units as _record_roi
            from app.services.roi_records import resolve_declaration as _resolve_roi

            try:
                from app.services.fleet_runs import home_fleet_defaults as _home_fleet

                _roi_fleet_id = (await _home_fleet(db, agent.id)).fleet_id
                # A savepoint: a failed read never poisons the run's transaction.
                async with db.begin_nested():
                    _roi_declaration = await _resolve_roi(
                        db,
                        agent_id=agent.id,
                        output_schema=(
                            agent.output_schema
                            if agent.output_format == "json" and isinstance(agent.output_schema, dict)
                            else None
                        ),
                        assignment_id=assignment.id,
                        fleet_id=_roi_fleet_id,
                    )
            except Exception as _roi_exc:  # accounting never fails a run
                logger.warning(
                    "ROI declaration of run %s unreadable: %s", run.id, format_exception_message(_roi_exc)
                )
                _roi_declaration = None
            if _roi_declaration is not None:
                await _record_roi(db, run=run, fleet_id=_roi_fleet_id, declaration=_roi_declaration)

            # ── Action Register -> live work items ───────────────
            # When the agent opted into the work-item system, parse the run's
            # "Action Register" table and materialise each row that names a known
            # owning agent into a durable, trackable work item so genuine,
            # un-picked-up follow-ups are surfaced and stale-flagged in the
            # dashboard instead of dying as run text. Rows with no resolvable
            # agent owner are skipped (never minted as orphan, unassigned items).
            # Best-effort: a parse/persist failure must never fail the run.
            if "work_items" in agent_builtin_tool_ids and run.result_summary:
                try:
                    from app.services.action_register import (
                        materialize_action_register,
                    )
                    from app.services.company_managed_writes import agent_writer

                    await materialize_action_register(
                        text=run.result_summary,
                        user_id=(
                            str(effective_user_id) if effective_user_id else None
                        ),
                        is_company_managed=run.is_company_managed,
                        correlation_prefix=f"actionreg:{run.id}",
                        created_by_agent_id=str(agent.id),
                        source_run_id=str(run.id),
                        writer=agent_writer(
                            is_company_managed=agent.is_company_managed,
                            agent_id=agent.id,
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "Action-register materialization failed for run %s: %s",
                        run.id,
                        format_exception_message(exc),
                    )

            # ── Work-item inbox: complete the dispatched item ────────
            # When the autonomous work-item consumer dispatched this run, close
            # out the item it was dispatched for so the queue advances on its
            # own (no human, no second sweep). Only the item the dispatcher
            # named (``_inbox_dispatched_work_item``): a payload naming another
            # one settles nothing. Best-effort: a completion failure must never
            # fail the run, but it must also never STRAND the item ``claimed`` -
            # if the completion does not take effect we release the claim
            # (bounded retries -> blocked) so the item is retried instead of
            # stuck forever. The run's end completes the item only when the run
            # left nothing pending: an item it completed itself, left
            # blocked, or that still names an open pull request is not done.
            _dispatched = _inbox_dispatched_work_item(run)
            if _dispatched is not None:
                from app.services.company_managed_writes import agent_writer
                from app.services.work_item import (
                    release_work_item_claim,
                    settle_inbox_work_item,
                )

                # Visibility from the item's OWN owning user, not run.user_id: a
                # configured ``run_as_user_id`` can differ from the item owner
                # and would make a user-scoped item invisible to complete/release.
                _wi_id, _wi_user = _dispatched
                # The run's agent settles the item: a company-managed one
                # settles a company-managed item, a personal one only reads it.
                # It claimed the item, so the creator's narrowing admits it.
                _wi_writer = agent_writer(
                    is_company_managed=agent.is_company_managed, agent_id=agent.id
                )
                # The run's end completes the item only for a run that could
                # not complete it itself and ended cleanly: a run offered
                # ``complete_work_item`` that did not call it, or a run wound
                # down by a failure brake, the iteration backstop or the
                # budget, puts it back in the queue.
                _release_reason = _inbox_release_reason(
                    loop_result, messages, effective_tools
                )
                # The run's own work-item tools could not reach the item (they
                # act with ``_run_file_owner_id``, the dispatcher selected
                # through the assignment owner's tier): every run would answer
                # "not found" and be released, one metered run per pass until
                # the release cap. Parked after this first run (#11943).
                _unreachable = False
                if "work_items" in agent_builtin_tool_ids:
                    try:
                        from app.services.work_item import reachable_by_run_tools

                        _unreachable = not await reachable_by_run_tools(
                            str(_wi_id), user_id=_run_file_owner_id or None, writer=_wi_writer
                        )
                    except Exception as reach_exc:  # fail open: settle as before
                        logger.debug(
                            "Reach of work item %s by run %s not read: %s",
                            _wi_id,
                            run.id,
                            format_exception_message(reach_exc),
                        )
                if _unreachable:
                    try:
                        from app.services.work_item import park_unreachable_work_item

                        if await park_unreachable_work_item(
                            str(_wi_id),
                            agent_id=agent.id,
                            run_id=run.id,
                            reason="the owner's run cannot read the item",
                        ):
                            _dispatched = None
                    except Exception as park_exc:
                        logger.warning(
                            "Failed to park work item %s after run %s: %s",
                            _wi_id,
                            run.id,
                            format_exception_message(park_exc),
                        )
            if _dispatched is not None:
                _wi_id, _wi_user = _dispatched
                try:
                    _completed = await settle_inbox_work_item(
                        str(_wi_id),
                        user_id=_wi_user,
                        result=(run.result_summary or "Completed by inbox run.")[
                            :4000
                        ],
                        writer=_wi_writer,
                        # A second dispatch never settles the holder's item.
                        run_id=run.id,
                        may_complete=_release_reason is None,
                        release_reason=_release_reason,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to complete work item %s for run %s: %s",
                        _wi_id,
                        run.id,
                        format_exception_message(exc),
                    )
                    _completed = None
                if _completed is None:
                    # Completion did not take effect (not found/not visible, or
                    # it errored): release the claim so the item is never
                    # stranded ``claimed``. release guards terminal items, so a
                    # partially-succeeded completion can't be reopened.
                    _released = None
                    try:
                        _released = await release_work_item_claim(
                            work_item_id=str(_wi_id),
                            user_id=_wi_user,
                            failed=True,
                            error="inbox run could not close its work item",
                            writer=_wi_writer,
                        )
                    except Exception as rel_exc:
                        logger.warning(
                            "Failed to release work item %s after run %s: %s",
                            _wi_id,
                            run.id,
                            format_exception_message(rel_exc),
                        )
                    if _released is None:
                        # Neither the settle nor the release reached the item
                        # the dispatcher handed this run: left claimed, the
                        # reaper would reopen it and the inbox dispatch it
                        # again, a metered run per pass (#11900). Parked.
                        try:
                            from app.services.work_item import park_unreachable_work_item

                            await park_unreachable_work_item(
                                str(_wi_id),
                                agent_id=agent.id,
                                run_id=run.id,
                                reason="not found or not accessible to the inbox run",
                            )
                        except Exception as park_exc:
                            logger.warning(
                                "Failed to park work item %s after run %s: %s",
                                _wi_id,
                                run.id,
                                format_exception_message(park_exc),
                            )

            # Extract tool calls from the conversation for actions_taken tracking.
            # The agentic loop modifies `messages` in-place, so it now contains
            # the full conversation: system → user → assistant (tool_calls) →
            # tool results → … → final assistant response.
            from app.services.hitl_whitelist import gate_off_outcome

            actions = _actions_taken(
                messages,
                composite,
                outcomes=_gate_outcomes,
                hitl_enabled=_gate_on,
                hitl_off_outcome=gate_off_outcome(agent),
            )
            run.actions_taken = actions

            # ── Track tool usage on Integration / MCP / Custom entities ──
            try:
                from app.services.tool_tracking import track_tool_usage

                await track_tool_usage(db, messages, composite)
            except Exception as _track_err:
                logger.warning(
                    "Tool usage tracking failed: %s", format_exception_message(_track_err), exc_info=True
                )

            # Delegation is the model's choice: no configuration requires an
            # assignment run to call a handover tool (a mandatory transfer is
            # post-loop routing, never offered to the model).
            _handover_tool_names = {
                "spawn_background_task",
                "consult_agent",
                "transfer_to_agent",
            }
            if _handover_loaded and not any(
                a["tool"] in _handover_tool_names for a in actions
            ):
                logger.debug(
                    "Assignment run %s completed without delegating: %d handover "
                    "agent(s) were available and no handover tool was called",
                    run_id,
                    len(_handover_agents),
                )

            # Persist the full agentic conversation including tool calls and
            # results so it can be displayed in the run detail view.
            from app.services.builtin_tools import serialize_messages

            run.messages = serialize_messages(messages)

            # ── Event bus: assignment run completed ────────────────────────
            await event_bus.emit(
                ASSIGNMENT_RUN_COMPLETED,
                {
                    "run_id": run_id,
                    "assignment_id": str(assignment.id),
                    "assignment_name": assignment.name,
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "user_id": str(run.user_id) if run.user_id else None,
                    "actions_count": len(actions),
                },
            )

            # ── Memory (save after successful run) ──────────────────────
            # ``_mem_task`` is declared at the outer scope (initially
            # ``None``) so the chain-dispatch path further down can
            # ``await`` it before triggering the next assignment — a
            # chained run dispatches IMMEDIATELY (worker SAQ enqueue or
            # inline ``asyncio.create_task``) and would otherwise race
            # the background memory write, causing the next assignment
            # to load memory BEFORE this run's summary was persisted.
            _mem_task: asyncio.Task | None = None
            if loop_result.content:
                # Detach memory writes so the assignment can be marked
                # completed and the user notified without waiting on
                # two serial Redis round-trips. The consolidate_* coros
                # were already background tasks; here we also background
                # the store_* calls. The strong-reference trick avoids
                # GC-mid-flight for these fire-and-forget tasks.
                async def _persist_assignment_memories(
                    _agent_id: str = str(agent.id),
                    _agent_name: str = agent.name,
                    _agent_short: bool = agent.memory_short_term_enabled,
                    _agent_medium: bool = agent.memory_medium_term_enabled,
                    _agent_company: bool = agent.is_company_managed,
                    _user_id: str | None = str(run.user_id) if run.user_id else None,
                    _agent_scope: str | None = assignment_memory_scope(run.user_id, agent.user_id),
                    _user_enabled: bool = agent.memory_user_enabled,
                    _summary: str = loop_result.content,
                    _assignment_name: str = assignment.name,
                    _llm: Any = llm,
                    _model: str | None = wf_resolved_model,
                ) -> None:
                    # The chain dispatch below awaits this write, then starts
                    # the next assignment as that assignment's own identity,
                    # not ``_user_id``: no run_as is carried from this run.
                    # The chained run must be one its identity may read, which
                    # ``trigger_assignment(continues_run=...)`` enforces.
                    if _agent_short:
                        try:
                            await store_assignment_agent_memory(
                                agent_id=_agent_id,
                                scope_id=_agent_scope,
                                source=_assignment_name,
                                summary=_summary,
                                consolidate=_agent_medium,
                                llm=_llm,
                                model=_model,
                                is_company_managed=_agent_company,
                                background_tasks=_background_tasks,
                            )
                        except Exception as mem_err:
                            logger.warning(
                                "Failed to store agent memory (agent=%s): %s",
                                _agent_name,
                                format_exception_message(mem_err),
                            )

                    if _user_id and _user_enabled:
                        try:
                            from app.services.user_memory import (
                                consolidate_user_memory,
                                store_user_memory,
                            )

                            u_counter = await store_user_memory(
                                _user_id,
                                agent_name=_agent_name,
                                summary=_summary,
                                interaction_type="assignment",
                                agent_id=_agent_id,
                            )
                            if u_counter >= 6:
                                _user_consolidate_task = asyncio.create_task(
                                    consolidate_user_memory(
                                        _user_id, _llm, model=_model
                                    )
                                )
                                _background_tasks.add(_user_consolidate_task)
                                _user_consolidate_task.add_done_callback(
                                    _background_tasks.discard
                                )
                        except Exception as mem_err:
                            logger.warning(
                                "Failed to store user memory "
                                "(user=%s, agent=%s): %s",
                                _user_id,
                                _agent_name,
                                format_exception_message(mem_err),
                            )

                _mem_task = asyncio.create_task(_persist_assignment_memories())
                _background_tasks.add(_mem_task)
                _mem_task.add_done_callback(_background_tasks.discard)

            # Update assignment stats. The count is incremented in SQL: the value
            # this session loaded when the run started is minutes old, and the
            # other runs and run deletions of the assignment write it meanwhile.
            await db.execute(
                update(Assignment)
                .where(Assignment.id == assignment.id)
                .values(execution_count=func.coalesce(Assignment.execution_count, 0) + 1)
            )
            assignment.last_triggered_at = datetime.now(UTC)

            # MCP cleanup is handled in the finally block below.

        except TurnWoundDown:
            # Its worker was asked to stop (a rollout, a scale-in, a node
            # drain): the loop stopped at an iteration boundary, every call
            # answered. The checkpoint is flushed and the run handed back, still
            # running with its work item claims, resumed by the next worker.
            # Its partner tasks stay outstanding for the resumed attempt.
            _handed_back = True
            end_trace(wf_trace_ctx, output=assignment_run_resume.HANDED_BACK_ERROR)
            if _run_checkpoint is not None:
                await assignment_run_resume.hand_back(
                    run_id, checkpoint=_run_checkpoint, attempt=attempt, place=attempt_place
                )
            return assignment_run_resume.RUN_HANDED_BACK

        except TurnSuperseded:
            # Another attempt took the run over while this turn ran:
            # the loop stopped before its next tool round. Like an attempt a
            # later one replaced, it writes no ending and leaves its
            # partner work to the attempt that goes on; its checkpoint writer
            # stops without a last snapshot in the ``finally`` below. The
            # reconciler absorbs the stop.
            _replaced = True
            end_trace(wf_trace_ctx, output=SUPERSEDED_MESSAGE)
            raise

        except Exception as e:
            # ── Classify the failure ONCE ────────────────────────
            # Every downstream decision (user-facing message, event error
            # type, persisted cause, trace attribution) reads this single
            # classification instead of re-deriving its own. The router import
            # is still forced first because the classifier consults it for the
            # 413 predicate, and on this path it may not be loaded yet — its
            # import chain can itself fail (a broken optional provider SDK),
            # which must degrade the classification rather than mask the
            # exception we are here to record.
            try:
                await ensure_hot_path_imports("app.llm.router")
            except Exception:
                logger.warning(
                    "Could not import app.llm.router to classify assignment "
                    "run %s failure; classification degrades to what the "
                    "exception itself carries",
                    run_id,
                    exc_info=True,
                )
            _failure = (
                FailureClassification(
                    cause=FailureCause.ENGINE_DEADLINE_EXCEEDED,
                    determination=FailureDetermination.STRUCTURAL,
                    detail="deadline_cut_budget",
                )
                if isinstance(e, DeadlineCutTimeoutError)
                else classify_run_failure(
                    e,
                    FailureContext(
                        llm_resolved=_llm_resolved,
                        # ``last_usage`` stays None until a call RETURNS, so
                        # this reads "did any model call in this run succeed".
                        # Recorded for measurement only — it changes no outcome
                        # today.
                        model_call_returned=(
                            getattr(_llm, "last_usage", None) is not None
                            if _llm is not None
                            else None
                        ),
                    ),
                )
            )
            is_overflow = _failure.cause is FailureCause.CONTEXT_OVERFLOW and isinstance(
                e, ContextOverflowError
            )
            is_too_large = _failure.cause is FailureCause.REQUEST_TOO_LARGE
            if is_overflow:
                logger.warning(
                    "Assignment run %s hit context overflow: %s", run_id, format_exception_message(e)
                )
                if e.is_tool_dominated:
                    error_msg = (
                        f"Tool overflow: the agent's tools (~{e.tool_tokens:,} tokens) "
                        f"exceed the model's context window "
                        f"({e.context_window:,} tokens for {e.model}). "
                        f"Reduce the number of bound tools or switch to a larger model."
                    )
                else:
                    error_msg = (
                        f"Context overflow: the input (~{e.estimated_tokens:,} tokens) "
                        f"exceeds the model's context window "
                        f"({e.context_window:,} tokens for {e.model})."
                    )
            elif is_too_large:
                # HTTP 413 / payload-too-large from the provider — the
                # accumulated conversation / tool output is too large for the
                # API gateway.  Log as a warning (not an error) since it is a
                # request-caused, not a provider-caused, failure.
                logger.warning(
                    "Assignment run %s failed with request-too-large error (HTTP 413): %s",
                    run_id,
                    format_exception_message(e),
                )
                error_msg = (
                    "Request too large: the assignment's accumulated context or tool output "
                    "exceeds the provider's payload limit. "
                    "Try reducing the assignment's scope, or use a model with a larger context window."
                )
            elif isinstance(e, StaleIntegrationReferenceError):
                logger.warning(
                    "Assignment run %s failed on a stale integration reference: %s",
                    run_id,
                    e,
                )
                error_msg = str(e)
                _stale_failure = e
            else:
                logger.error(
                    "Assignment run %s failed: %s", run_id, format_exception_message(e), exc_info=True
                )
                error_msg = str(e)

            # ── Agent failure quota pool (env-only) ──────────────────────
            # This run spent an agentic action up-front and produced
            # nothing. Credit that action back, bounded by the agent's
            # per-window failure pool, so a provider outage or a broken tool
            # doesn't eat the user's action quota while a permanently failing
            # agent keeps consuming it once its pool is spent. Only ever reverses what this run
            # actually metered (``quota_actions_consumed``), so an exempt,
            # unmetered or quota-refused run is untouched. Never raises, and
            # never touches this session (``db`` is a resolution signal
            # only) — the transaction here still has to commit the failure.
            #
            # Runs BEFORE ``end_trace`` on purpose. The trace's
            # ``quota_reason`` is written when the trace OPENS, so a run that
            # is charged and then credited back here would otherwise keep
            # ``"charged"`` in its trace forever — the Trace Explorer stating
            # the exact opposite of what happened. Langfuse's root metadata is
            # genuinely immutable after creation, but the durable local trace
            # is only assembled at ``end_trace``, so the correction just has to
            # be known by then. Ordering is the whole fix.
            #
            # A run with no user behind it (``effective_user_id`` None) was
            # charged to the platform-wide counters: the free path
            # gives them back, the pool - a per-user allowance - never does.
            #
            # Only while the run still records THIS attempt's charge:
            # taken off the row first, so the re-run that replaced this attempt
            # after a lost lease, or the dead-letter, credits it at most once.
            _refund_outcome: str | None = None
            _charged = bool(_quota_trace_meta.get("quota_actions_consumed"))
            _owns_charge = _charged and await release_charge_record(
                db, run, charge_id=charge_id
            )
            if _charged and not _owns_charge:
                # Credited by what replaced this attempt, or left on the row
                # for the engine to credit.
                _quota_trace_meta = {
                    **_quota_trace_meta,
                    "quota_refund_outcome": "settled_by_engine",
                }
                with contextlib.suppress(Exception):
                    enrich_trace_metadata(wf_trace_ctx, _quota_trace_meta)
            if _owns_charge:
                from app.llm.router import is_provider_unavailable_error
                from app.services.quota import (
                    REFUND_OUTCOME_POOL_EXHAUSTED,
                    refund_agentic_usage_for_failure,
                    refund_agentic_usage_for_provider_outage,
                )

                # A run that never reached a model is credited back FREE — the
                # platform failed to deliver, so rationing it against the
                # agent's pool would let one outage swallow the whole
                # allowance. Two ways to know: the run died before it even had
                # an LLM to call (``_llm_resolved``), or it died calling one
                # that was unreachable / down / unauthorised. Every other
                # failure spends a pool slot - a stale reference too, whatever
                # its text: the loop had returned, so a model answered, and a
                # broken configuration fails every run until someone fixes it.
                # A timeout on a bound the deadline cut is the engine's:
                # free, like the dead-letter that bound replaces.
                _free_credit = isinstance(e, DeadlineCutTimeoutError) or (
                    not isinstance(e, StaleIntegrationReferenceError)
                    and (not _llm_resolved or is_provider_unavailable_error(e))
                )
                _pool_report: dict = {}
                if _free_credit:
                    _refunded = await refund_agentic_usage_for_provider_outage(
                        effective_user_id,
                        agent.id,
                        db=db,
                        reason=f"assignment run {run_id}",
                        cause=_failure.cause.value,
                    )
                else:
                    _refunded = await refund_agentic_usage_for_failure(
                        effective_user_id,
                        agent.id,
                        db=db,
                        reason=f"assignment run {run_id}",
                        cause=_failure.cause.value,
                        outcome_out=_pool_report,
                    )
                # The difference matters to anyone reading the trace: the
                # platform absorbed it, the agent's own pool did, or the charge
                # STANDS - and when it stands, WHY is what the pool helper
                # reports through ``_pool_report`` (``REFUND_OUTCOME_*``): the
                # pool is spent, the pool is switched off, the refund path
                # itself broke, there was never anything to give back
                # because the user's agentic quota is unlimited, or
                # there is no user whose pool could give it back. Only
                # the first of those is the pool working as designed, so the
                # reported outcome is written as is rather than collapsed into
                # "pool exhausted" - which must never be said about a pool that
                # is off, a refund that failed, or a quota that does not exist.
                # "pool_exhausted" remains the fallback for a report that never
                # arrived (the helper returned False without naming a reason).
                if _refunded:
                    _refund_outcome = (
                        "credited_free" if _free_credit else "credited_from_pool"
                    )
                elif _free_credit:
                    _refund_outcome = "refund_unavailable"
                else:
                    _refund_outcome = (
                        _pool_report.get("outcome") or REFUND_OUTCOME_POOL_EXHAUSTED
                    )
                _quota_trace_meta = {
                    **_quota_trace_meta,
                    "quota_refund_outcome": _refund_outcome,
                }
                if _refunded:
                    # The charge recorded at trace-open has been reversed, so
                    # the trace must say so rather than keep asserting it.
                    _quota_trace_meta["quota_actions_consumed"] = 0
                    _quota_trace_meta["quota_reason"] = (
                        "credited_back_free"
                        if _free_credit
                        else "credited_back_from_pool"
                    )
                with contextlib.suppress(Exception):
                    enrich_trace_metadata(wf_trace_ctx, _quota_trace_meta)

            if wf_trace_ctx:
                # The classification rides along so the durable trace carries a
                # filterable cause next to its error text.
                end_trace(
                    wf_trace_ctx,
                    error=error_msg,
                    failure=_failure,
                    usage=getattr(_finished_loop, "usage", None),
                    model=getattr(_finished_loop, "model", None),
                )
            # This attempt's charge is settled above whatever the run became;
            # its ending is not written over another one.
            if not own_ending and (
                _elsewhere := await _ended_elsewhere(db, run, place=attempt_place)
            ):
                _replaced = _elsewhere == REPLACED_ATTEMPT
                return
            _apply_failure_classification(run, _failure)
            run.status = "failed"
            run.error = error_msg
            # A run its own timeout (or its deadline) stopped says so in a
            # field, not only in the error text (#1695).
            if _timeout_stop := getattr(e, "run_stop_reason", None):
                run.stop_reason = _timeout_stop
            # Persist the trace id even on failure — that's when deep-linking
            # to the Langfuse trace matters most (usage only when the loop
            # returned before the run failed).
            apply_run_usage_accounting(
                run, getattr(_finished_loop, "usage", None), wf_trace_ctx
            )
            # Persist messages so the rendered prompt and agentic steps are
            # available for debugging even when the run fails (e.g. "prompt
            # too long" errors).
            try:
                if messages:
                    from app.services.builtin_tools import serialize_messages

                    run.messages = serialize_messages(messages)
            except Exception:
                message = "Failed to serialize messages for failed assignment run %s"
                logger.warning(message, run.id, exc_info=True)
            # ── Work-item inbox: release the dispatched item ─────────
            # On failure, hand the item back to the queue (bounded retries via a
            # ``payload`` attempt counter, then parked in ``blocked``) so a
            # dispatched item is never stranded ``claimed`` by a dead run. Only
            # the item the dispatcher named (``_inbox_dispatched_work_item``),
            # so a run that merely carries a ``work_item_id`` - a manual
            # trigger, a webhook body - is never touched.
            await _release_run_work_item(run, agent, failed=True, error=error_msg)
            await _publish_run_status(
                str(run.user_id),
                str(run.id),
                "failed",
                assignment.name,
                error_msg,
                is_company=run.is_company_managed,
            )
            await event_bus.emit(
                ASSIGNMENT_RUN_FAILED,
                {
                    "run_id": run_id,
                    "assignment_id": str(assignment.id),
                    "assignment_name": assignment.name,
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "user_id": str(run.user_id) if run.user_id else None,
                    "error": error_msg,
                    # ``error_type`` is the long-standing discriminator the
                    # chat stream / notification consumers switch on — its two
                    # historic values are preserved verbatim. ``failure_cause``
                    # is the new, complete one: every failure carries
                    # it, including the ones ``error_type`` has never named.
                    "failure_cause": _failure.cause.value,
                    "failure_determination": _failure.determination.value,
                    **(
                        {"error_type": overflow_error_type(e)}
                        if is_overflow
                        else {"error_type": "request_too_large"}
                        if is_too_large
                        else {}
                    ),
                },
            )

        finally:
            from app.services.builtin_tools import cleanup_mcp_manager
            from app.services.langfuse_tracing import reset_current_trace_ctx

            # Pop the Langfuse trace context pushed earlier so nested asyncio
            # tasks don't inherit a stale parent.
            if _wf_trace_ctx_token is not None:
                with contextlib.suppress(Exception):
                    reset_current_trace_ctx(_wf_trace_ctx_token)

            # FR-025 / FR-005, as in ``run_agent_chat``: a run that ends
            # cancels its partner work. A run handed back to the queue is not
            # over, and a replaced attempt no longer owns it: the attempt that
            # goes on with the run collects its partners' answers.
            await _teardown_partner_work(
                db,
                run,
                integ_exec,
                handed_back=_handed_back,
                replaced=_replaced,
            )
            # Every attempt stops its checkpoint writer here, a replaced one
            # too (it returns before the discard below): no snapshot of it
            # lands after this point. A handed-back one flushed already.
            if _run_checkpoint is not None and not _handed_back:
                with contextlib.suppress(Exception):
                    await _run_checkpoint.aclose(flush=False)

            await cleanup_mcp_manager(mcp_manager)

        # The turn ended in this process: nothing is left to resume.
        # Its writer is stopped in the ``finally`` above; the checkpoint gives
        # way to a tombstone at this attempt's place, so a replaced attempt's
        # writer still in flight elsewhere cannot bring it back.
        # No store reached when the run neither resumed nor wrote one.
        if _run_checkpoint is not None and _run_checkpoint.may_hold_row:
            await assignment_run_resume.discard(run.id, place=attempt_place)

        run.completed_at = datetime.now(UTC)

        # ── Cross-run auth-failure quarantine ──────────────────────
        # Best-effort by contract: the helper never re-raises, so it can
        # never corrupt the final db.commit() below.
        await _track_assignment_auth_failures(
            db,
            run=run,
            assignment=assignment,
            agent=agent,
            run_id=run_id,
            messages=messages,
        )

        try:
            await db.commit()
        except StaleDataError:
            # The run was deleted while execution was in progress (user deleted
            # it from the UI).  Nothing left to update — exit gracefully.
            logger.warning(
                "Run %s was deleted during execution — skipping final update", run_id
            )
            structlog.contextvars.unbind_contextvars(
                "source",
                "agent",
                "agent_id",
                "assignment",
                "assignment_id",
                "run_id",
            )
            return

        # After the commit on purpose: a concurrent run of this assignment
        # must see this failure when it reads the streak. In a session
        # of its own: its failure path rolls back, and a rollback expires every
        # instance of the session it runs on - ``run`` and ``assignment`` are
        # read again below, and on an AsyncSession that would be a lazy load.
        if _stale_failure is not None:
            async with async_session() as _stale_db:
                await _track_stale_reference_failure(
                    _stale_db,
                    run=run,
                    assignment=assignment,
                    agent=agent,
                    error=_stale_failure,
                )

        # Unbind structured context now that the run is finished
        structlog.contextvars.unbind_contextvars(
            "source",
            "agent",
            "agent_id",
            "assignment",
            "assignment_id",
            "run_id",
        )

        # Publish final status to SSE
        await _publish_run_status(
            str(run.user_id),
            str(run.id),
            run.status,
            assignment.name,
            run.result_summary,
            is_company=run.is_company_managed,
        )
        # The background tasks the run spawned that never started have no one
        # left to collect them: cancelled with their cause, so none runs for
        # nobody. A running child keeps running and delivers as before.
        if run.status in ("failed", "completed"):
            try:
                from app.services.sub_agent import cancel_unstarted_tasks_of_ended_run

                async with async_session() as _children_db:
                    await cancel_unstarted_tasks_of_ended_run(_children_db, run.id)
            except Exception as exc:
                logger.warning(
                    "Run %s ended; its never-started background tasks were not cancelled: %s",
                    run.id,
                    format_exception_message(exc),
                )
        # After the commit: a subscriber that reads the run sees how it ended.
        if run.status in ("failed", "completed"):
            # The frictions the run left with no disposition: counted on the
            # run and announced (services/frictions.py); none, nothing written.
            from app.services.frictions import settle_run_frictions

            await settle_run_frictions(run)
        if run.status == "failed":
            await _publish_run_failed(run, run.error)
        elif run.status == "completed":
            await _publish_run_ended(run, "run.completed", reason="completed")

        # --- Retry on failure ---
        # Two retry triggers:
        #   * explicit opt-in (``assignment.retry_on_failure``) — any failure,
        #     up to ``assignment.max_retries``;
        #   * automatic transient model-failure retry — when the run died on
        #     provider weather (rate limit / quota, overload, outage) after
        #     the in-call retries and failover were already exhausted, retry
        #     a bounded number of times even WITHOUT the opt-in.
        # Transient failures always retry with an exponential + jittered
        # delay so the retry doesn't land in the same rate-limit window (and
        # a burst of failed fan-out runs doesn't re-fire as a herd).
        if run.status == "failed":
            transient_llm_failure = _is_transient_llm_failure(run.error)
            if _stale_failure is not None:
                # A retry would run the same configuration against the same
                # missing resource.
                max_retries = 0
            elif _deadline_failure_never_started(run, getattr(_llm, "last_usage", None) if _llm is not None else None):
                # Its deadline was spent before any model call returned: the
                # run waited in a saturated queue and never worked. A retry
                # only queues it again behind the same backlog.
                logger.info(
                    "Assignment run %s not retried: its deadline expired before any model call returned",
                    run_id,
                )
                max_retries = 0
            elif assignment.retry_on_failure:
                max_retries = assignment.max_retries or 3
            elif transient_llm_failure:
                max_retries = max(0, settings.assignment_transient_retry_max)
            else:
                max_retries = 0
            current_retries = run.retry_count or 0
            if (
                max_retries
                and current_retries < max_retries
                and await is_paused_by_deactivated_owner(db, assignment)
            ):
                logger.info(
                    "Assignment run %s not retried: the owner of assignment "
                    "'%s' is deactivated",
                    run_id,
                    assignment.name,
                )
                max_retries = 0
            # A retry runs as the failed run's user with its input: refused,
            # and said so, when that user can no longer open it.
            retry_identity = _acting_identity(run.user_id, agent)
            if (
                max_retries
                and current_retries < max_retries
                # A Platform Health fleet run with no user is retried as what it ran
                # as, Platform Health alone: there is no other user to check.
                and not (retry_identity is None and run.user_id is None and is_health_agent(agent))
                and not await can_open_continued_run(db, run, retry_identity)
            ):
                await _report_refused_continuation(
                    kind="retry", assignment=assignment, agent=agent, identity=retry_identity
                )
                max_retries = 0
            if max_retries and current_retries < max_retries:
                try:
                    retry_delay_seconds = (
                        _transient_retry_delay_seconds(current_retries)
                        if transient_llm_failure
                        else 0
                    )
                    retry_run = AssignmentRun(
                        assignment_id=assignment.id,
                        agent_id=assignment.agent_id,
                        user_id=run.user_id,
                        is_company_managed=continuing_run_is_company_managed(
                            run.is_company_managed, fallback=False, ceiling=True
                        ),
                        trigger_payload=run.trigger_payload,
                        # Preserve the trigger context: dropping it
                        # loses the fan-out's ``ctem_vertical`` (the retried
                        # assessment would compute for the wrong vertical) and
                        # the trigger ``type`` consumed downstream (e.g. the
                        # auth-failure quarantine's scheduler-run gate). Not
                        # the failed run's charge record: a retry the engine
                        # abandons before it charges must not be credited it.
                        trigger_context=assignment_run_resume.without_resume_state(
                            without_charge_state(run.trigger_context)
                        ),
                        status="delayed" if retry_delay_seconds > 0 else "pending",
                        retry_count=current_retries + 1,
                    )
                    db.add(retry_run)
                    await db.flush()
                    retry_run_id = str(retry_run.id)
                    # Schedule the retry through the durable orchestration
                    # engine (same path as the primary dispatch): the
                    # reconciler claims and runs it under a lease, so the
                    # retry survives a restart and is recovered like any
                    # other execution.
                    # ``timeout_seconds == 0`` disables the timeout (see the
                    # model) → the retry carries no orchestration deadline
                    # either, matching the primary-dispatch path. ``None``
                    # (unflushed row) falls back to the shared
                    # ``_UNSET_TIMEOUT_FALLBACK_SECONDS`` (same as the loop's
                    # ``asyncio.wait_for`` bound and ``trigger_assignment``).
                    # The deadline gets the same iteration floor as the
                    # primary dispatch in ``trigger_assignment`` (re-resolved
                    # here: ``effective_max_iterations`` may be unbound when
                    # the failure happened before the loop setup).
                    retry_ts = assignment.timeout_seconds
                    if retry_ts is None:
                        retry_ts = _UNSET_TIMEOUT_FALLBACK_SECONDS
                    # The delayed retry's deadline starts at its own
                    # ``run_at`` (like the primary dispatch), so the backoff
                    # wait never eats into the run's timeout budget.
                    retry_run_at = datetime.now(UTC) + timedelta(
                        seconds=retry_delay_seconds
                    )
                    if retry_ts <= 0:
                        retry_deadline, retry_budget = None, {}
                    else:
                        retry_timeout = _apply_iteration_timeout_floor(
                            float(retry_ts),
                            await resolve_run_max_iterations(db, assignment, agent),
                            retry_run_id,
                            assignment.timeout_seconds,
                        )
                        # Counted from the retry's start, as the primary
                        # dispatch: a retry into a saturated queue keeps its budget.
                        retry_deadline, retry_budget = queued_deadline(
                            retry_run_at,
                            retry_timeout + settings.orchestration_grace_seconds,
                            max_queue_wait_seconds=max_queue_wait_seconds(assignment),
                        )
                    await create_execution(
                        db,
                        adapter="assignment",
                        payload={"run_id": retry_run_id, **retry_budget},
                        run_at=retry_run_at,
                        deadline_at=retry_deadline,
                        dedup_key=f"assignment_run:{retry_run_id}",
                        max_attempts=assignment_run_resume.execution_max_attempts(),
                    )
                    await db.commit()
                    await notify_orchestration_wake()
                    logger.info(
                        "Assignment '%s' run %s failed — retrying (%d/%d) as "
                        "run %s in %ds%s",
                        assignment.name,
                        run_id,
                        current_retries + 1,
                        max_retries,
                        retry_run_id,
                        retry_delay_seconds,
                        (
                            " (transient model failure — automatic retry)"
                            if transient_llm_failure
                            else ""
                        ),
                    )
                except Exception as retry_err:
                    # error (was warning): the configured retry silently never
                    # happens — the failed run is permanently abandoned.
                    logger.error(
                        "Failed to create retry run for run %s: %s",
                        run_id,
                        format_exception_message(retry_err),
                        exc_info=True,
                    )
            elif max_retries:
                logger.info(
                    "Assignment '%s' run %s failed — max retries (%d) exhausted",
                    assignment.name,
                    run_id,
                    max_retries,
                )

        # Assignment chaining: trigger next assignment if configured
        if run.status == "completed" and assignment.next_assignment_id:
            # Await the memory background task before dispatching the
            # chain. The next assignment will load short-term + user
            # memory at startup; if we trigger it before
            # ``_persist_assignment_memories`` has finished writing this
            # run's summary, the next run sees stale memory and the
            # chain-of-thought across assignments breaks. Cost: at most
            # two Redis SETs (sub-ms typical). The consolidate_* LLM
            # calls inside the task remain detached as their own
            # ``asyncio.create_task`` so this await never blocks on the
            # slow consolidation path. Exceptions inside the task are
            # already logged inside ``_persist_assignment_memories``;
            # ``contextlib.suppress`` swallows the re-raise so a memory
            # failure can't break the chain dispatch.
            if _mem_task is not None and not _mem_task.done():
                with contextlib.suppress(Exception):
                    await _mem_task
            try:
                next_assign_result = await db.execute(
                    select(Assignment).where(
                        Assignment.id == assignment.next_assignment_id,
                        Assignment.enabled.is_(True),
                    )
                )
                next_assign = next_assign_result.scalar_one_or_none()
                # Checked here like the agentic gate below: a paused next link
                # is a policy decision, which the ``except`` would log as ERROR.
                if next_assign and await is_paused_by_deactivated_owner(
                    db, next_assign
                ):
                    logger.info(
                        "Chain '%s' to '%s' not dispatched: the next "
                        "assignment's owner is deactivated",
                        assignment.name,
                        next_assign.name,
                    )
                    next_assign = None
                if next_assign:
                    chain_payload = {
                        "chained_from": str(assignment.id),
                        "previous_result": run.result_summary or "",
                        "previous_actions": run.actions_taken or [],
                    }
                    chain_trigger_context = {
                        "type": "chain",
                        "chained_from_assignment_id": str(assignment.id),
                        "chained_from_assignment_name": assignment.name,
                        "chained_from_run_id": run_id,
                        "triggered_at": datetime.now(UTC).isoformat(),
                    }
                    # skip_if_active: if the next assignment already has an
                    # active run (e.g. it was also triggered directly), do not
                    # stack a second one — the skip is logged inside
                    # trigger_assignment and the chain link is dropped.
                    # Checked here, not left to ``trigger_assignment``'s raise:
                    # this is a worker path, and the ``except Exception`` below
                    # logs ERROR with a traceback, which would report a
                    # deliberate policy decision as a defect.
                    from app.services.agentic_gate import agentic_refusal

                    chain_refusal = await agentic_refusal(db)
                    next_agent = await _resolve_chain_agent(db, agent, next_assign) if chain_refusal is None else None
                    if chain_refusal is not None:
                        logger.info(
                            "Chain '%s' to '%s' not dispatched: agentic AI "
                            "unavailable (%s)",
                            assignment.name,
                            next_assign.name,
                            chain_refusal,
                        )
                        chained_run_id = None
                    elif next_agent is None:
                        chained_run_id = None
                    else:
                        # The chained run runs as the next assignment's own
                        # identity (its run-as user, else its owner), never
                        # this run's user: the next assignment's integrations
                        # and credentials belong to that identity, and running
                        # another agent as this run's user could hand it an
                        # agent it cannot reach. It carries this run's result,
                        # so ``trigger_assignment`` refuses it
                        # (``ContinuedRunNotOpenableError``) when that identity
                        # cannot open this run.
                        chained_run_id = await trigger_assignment(
                            assignment=next_assign,
                            agent=next_agent,
                            payload=chain_payload,
                            db=db,
                            trigger_context=chain_trigger_context,
                            skip_if_active=True,
                            continues_run=run,
                        )
                    if chained_run_id is not None:
                        logger.info(
                            "Chained assignment '%s' → '%s'",
                            assignment.name,
                            next_assign.name,
                        )
            except AssignmentOwnerDeactivatedError:
                # The next assignment's owner was deactivated since the check
                # above: the pause, not a broken chain.
                logger.info(
                    "Chain '%s' to its next assignment not dispatched: that "
                    "assignment's owner is deactivated",
                    assignment.name,
                )
            except ContinuedRunNotOpenableError:
                await _report_refused_continuation(
                    kind="chain",
                    assignment=assignment,
                    agent=agent,
                    identity=_acting_identity(_execution_identity(next_assign, None, next_agent), next_agent),
                    next_assign=next_assign,
                )
            except Exception as chain_err:
                # Chained assignment silently dropped. ERROR-level because
                # this breaks user-configured workflows in a non-obvious
                # way (the originating run looks "completed" but the
                # downstream chain never fired).
                logger.error(
                    "Failed to chain assignment '%s' → next assignment "
                    "(next_assignment_id=%s) after run %s: %s",
                    assignment.name,
                    assignment.next_assignment_id,
                    run_id,
                    format_exception_message(chain_err),
                    exc_info=True,
                )

        # Send notification if configured (both completed and failed runs).
        # Tag the run BEFORE delivering so the flag is persisted even if
        # the delivery itself fails (ephemeral SSE, external channel error, etc.).
        if assignment.notify_on_complete and run.status in ("completed", "failed"):
            run.notification_sent = True
            await db.commit()
            try:
                from app.services.notify import deliver_notification

                await deliver_notification(db, run, assignment, agent)
            except Exception as notif_err:
                # error (was warning): for channel-routed assignments the
                # notification IS the deliverable — losing it means the run's
                # output never reaches the user.
                logger.error(
                    "Notification delivery failed for run %s: %s",
                    run_id,
                    format_exception_message(notif_err),
                    exc_info=True,
                )


async def _refuse_if_ai_disabled(assignment: Assignment) -> None:
    """Refuse this trigger when AI is off here: switched off by an
    administrator, or not licensed - Community Edition. Raises; never
    returns a flag. The two refusals share the ``{code, message}`` shape
    (``ai_disabled`` / ``license_required``) so callers branch on a value.

    **Raises rather than returning a sentinel.** Returning ``None`` from
    ``trigger_assignment`` was indistinguishable from its existing "skipped
    because a run is already active" outcome, and eight call sites in
    ``webhooks.py`` read ``run_id is None`` exactly that way — so an external
    partner got ``200`` with ``"assignment already has an active run"``, a false
    reason for a delivery that did nothing. That is the "silently succeeds while
    doing nothing" SC-004 forbids, on the entrypoints FR-010 names.

    Safe to raise: the autonomous tick asks the same gate itself before
    dispatching (``_run_autonomous_tick_inner``), so the callers that reach here
    with AI off are request paths, which is precisely where a 403 belongs. Every
    receiver in ``api/v1/webhooks.py`` - the direct, per-assignment, provider
    (``/api/hooks/providers/{provider}``), integration-level and legacy agent
    endpoints alike - answers its sender that 403, which a sender retries and
    never mistakes for a delivery (the contract documented for webhook senders).
    Only the change-notification receivers in
    ``api/v1/integration_notifications.py`` (Microsoft Graph, Slack, LinkedIn),
    whose providers drop a subscription after repeated failures, catch it
    through ``agentic_gate.refusal_code`` and keep answering 200.

    Gated at the **service**, not per route: the autonomous tick, the manual
    endpoint and the assignment webhooks all arrive here, and the webhooks are
    external — a partner with a webhook secret was able to run an agent on a
    platform configured to run none (FR-001, FR-010, SC-005). Gating each
    caller separately is how the webhook path was missed in the first place.

    Opens its own session: callers reach this with and without one, and the
    answer must not depend on which.
    """
    from app.core.database import async_session
    from app.services.agentic_gate import (
        AI_DISABLED_CODE,
        agentic_refusal,
        refuse_ai_disabled,
        refuse_licence_required,
    )

    async with async_session() as _db:
        refusal = await agentic_refusal(_db)
    if refusal is None:
        return

    logger.info(
        "Agentic AI unavailable on this deployment (%s): refusing to trigger "
        "assignment %s.",
        refusal,
        assignment.id,
    )
    if refusal == AI_DISABLED_CODE:
        refuse_ai_disabled()
    refuse_licence_required()


async def _refuse_if_group_ai_disabled(user_id: object) -> None:
    """Refuse a trigger whose run would act as a person a group of theirs
    switches AI off for (``group_ai_access``): the platform switch's own
    ``ai_disabled`` code with ``scope: "group"``, so a loop over several
    people's work tells it from a platform refusal (``refusal_is_personal``).

    Asked right after :func:`_refuse_if_ai_disabled`, at the same two funnels,
    and free while no group switches AI off. A group state that could not be
    read refuses too: no model runs for a person it could not be checked for.
    """
    if user_id is None:
        return
    from app.core.database import async_session
    from app.services.agentic_gate import group_verdict, refuse_ai_disabled_for_group

    async with async_session() as _db:
        verdict = await group_verdict(_db, user_id)
    if verdict.refusal is not None:
        logger.info("AI is disabled for a group of user %s: refusing to trigger.", user_id)
        refuse_ai_disabled_for_group()


def _run_person(
    assignment: Assignment, triggered_by_user_id: _uuid.UUID | None, agent: Agent | None = None
) -> _uuid.UUID | None:
    """:func:`_execution_identity`, read tolerantly: a gate must never be what
    breaks a trigger on an assignment-like object."""
    if triggered_by_user_id is None and is_health_agent(agent):
        return None
    return (
        triggered_by_user_id
        or getattr(assignment, "run_as_user_id", None)
        or getattr(assignment, "user_id", None)
    )


def _execution_identity(
    assignment: Assignment, triggered_by_user_id: _uuid.UUID | None, agent: Agent | None
) -> _uuid.UUID | None:
    """The ``user_id`` a run of *assignment* takes (precedence in ``trigger_assignment``).

    A Platform Health fleet run nobody started has no user: it acts as Platform Health
    identity, never as the assignment's owner or run-as user (PRD 13).
    """
    if triggered_by_user_id is None and is_health_agent(agent):
        return None
    return triggered_by_user_id or assignment.run_as_user_id or assignment.user_id


def _acting_identity(user_id: _uuid.UUID | None, agent: Agent | None) -> _uuid.UUID | None:
    """Who a run with this ``user_id`` acts as: that user, else its agent's owner.

    A Platform Health fleet agent's run with no user never falls back to its owner,
    the administrator its company-managed row keeps (invariant 9): it acts
    with no user, as Platform Health alone (PRD 9, 13). Its tools, quota,
    provider exclusions and variables are then those of a run with no account.
    """
    if user_id is None and is_health_agent(agent):
        return None
    return user_id or (agent.user_id if agent else None)


async def trigger_assignment(
    assignment: Assignment,
    agent: Agent,
    payload: dict | None = None,
    db: AsyncSession | None = None,
    trigger_context: dict | None = None,
    triggered_by_user_id: _uuid.UUID | None = None,
    skip_if_active: bool = False,
    active_run_user_id: _uuid.UUID | None = None,
    extra_delay_seconds: int = 0,
    continues_run: AssignmentRun | None = None,
    owed_conversation_id: _uuid.UUID | None = None,
    max_active_runs: int = 1,
) -> str | None:
    """Trigger an assignment execution, handling delays if configured.

    ``max_active_runs`` widens the ``skip_if_active`` guard: the trigger is
    skipped once that many runs of the assignment are active, counted under
    the same advisory lock, so concurrent dispatchers never exceed it. The
    work-item inbox passes its ``max_concurrent_runs``; every other
    caller keeps the default of one run at a time.

    ``owed_conversation_id`` is the chat conversation that triggered the run:
    the run is owed to it (an ``AssignmentRunConversationNotice`` written in
    the run's own transaction), and the conversation is told how it ended
    (``services/assignment_run_wake.py``).

    ``continues_run`` is the run this one continues - the run a follow-up
    follows up, the run a chained assignment carries the result of. The new
    run is never more visible than it: its ``is_company_managed`` is
    the assignment's, capped by that run's
    (:func:`~app.api.v1.helpers.continuing_run_is_company_managed`), and
    ``ContinuedRunNotOpenableError`` (403) is raised, before anything is
    written, when the user it would run as cannot open that run
    (:func:`~app.api.v1.helpers.can_open_continued_run`).

    ``extra_delay_seconds`` is an additional caller-supplied offset added to
    the run's ``run_at`` on top of the assignment's configured delay — used by
    the per-user fan-out to spread its runs across
    ``settings.fanout_spread_window_seconds`` instead of firing every
    user's run at the same instant. Because the orchestration deadline is
    derived from ``run_at``, the offset also shifts the run's timeout budget,
    so a spread run is never dead-lettered for time it spent waiting for its
    own scheduled start.

    ``triggered_by_user_id`` is the human who initiated the run when there is
    one in the loop — the user who clicked "Trigger" in the UI, or the caller
    of the agent-builder ``trigger_assignment`` chat tool. When provided it
    becomes the run's execution identity (the run acts on behalf of that
    person). It is left unset for fully automatic triggers (schedule, webhook,
    integration event), which fall back to the assignment's "run as" user
    (company-managed agents) and then the assignment owner.

    ``skip_if_active`` is the opt-in overlap guard for automatic burst-prone
    triggers (webhooks, integration events, chaining): when True and the
    assignment already has an active run (``has_active_run``), the trigger is
    SKIPPED — logged and ``None`` returned, no run created. The check is made
    atomic under concurrency by a per-assignment transaction-scoped advisory
    lock (``_acquire_trigger_overlap_lock``), so two concurrent triggers
    cannot both pass the check before either commits. The session is
    committed on the skip path too (not just after creating a run), so the
    advisory lock never outlives this call — callers in fan-out loops must
    expect a transaction boundary here regardless of the outcome. Bursts are
    dropped, not queued: queueing needs ordering, bounds and backpressure
    semantics that this guard does not attempt to provide. Callers on a path
    where a dropped delivery is a lost event — the per-assignment webhook
    receiver, whose sender records the 200 as a success — must use
    ``trigger_or_queue_assignment`` instead, which supplies exactly those
    semantics on top of this guard. The scheduler keeps its own pre-existing
    skip-with-log guard and calls this with the default ``False``. The manual
    triggers (``POST /assignments/{id}/trigger``, the agent-builder
    ``trigger_assignment`` tool, ``delegate_to_assignment``) check first for
    their wording (``manual_trigger_overlap``) and pass ``True`` with
    ``active_run_user_id`` (``manual_trigger_run_scope``), so check and run
    creation are atomic; ``active_run_user_id`` scopes the guard to that
    user's runs for a per-user assignment.

    Returns the AssignmentRun ID, or ``None`` when the trigger was skipped.
    An event-triggered run refused as a duplicate of a run of the same agent
    (``services/event_run_dedup.py``) returns the id of the ``skipped`` run
    that records the refusal: the event was taken, nothing executes.
    Raises ``AssignmentOwnerDeactivatedError`` (409 ``owner_deactivated``) for
    an assignment that is not company-managed and whose owner's account is
    deactivated.
    """
    await _refuse_if_ai_disabled(assignment)
    await _refuse_if_group_ai_disabled(_run_person(assignment, triggered_by_user_id, agent))

    # Defense-in-depth: reject disabled assignments/agents early
    if not assignment.enabled:
        raise ValueError(f"Assignment '{assignment.name}' is disabled")
    if not agent.enabled:
        raise ValueError(f"Agent '{agent.name}' is disabled")

    should_close = False
    if db is None:
        db = async_session()
        should_close = True
        await db.__aenter__()

    try:
        # The backstop of every trigger path: the discovery queries
        # already skip a paused assignment, the rest arrive here.
        await refuse_if_owner_deactivated(db, assignment)

        if continues_run is not None and not await can_open_continued_run(
            db, continues_run, _acting_identity(_execution_identity(assignment, triggered_by_user_id, agent), agent)
        ):
            raise ContinuedRunNotOpenableError()

        # A delivery whose quota is spent creates no run: skipped, like
        # a busy assignment, and counted as a filtered event.
        if triggered_by_user_id is None and await _drop_delivery_over_quota(
            db, assignment, agent, trigger_context
        ):
            return None

        delivery = assignment_dispatch.is_delivery(trigger_context)
        if delivery:
            # A delivery started here holds its ordering key like one the queue
            # starts, so the next one of the same key waits for it.
            trigger_context = assignment_dispatch.with_ordering_key(
                assignment.trigger_config, payload, trigger_context
            )
            # A chat message's run waits out the coalescing window, so the
            # next message of the same sender joins its turn.
            if extra_delay_seconds <= 0 and chat_turn_coalescing.is_conversation_event(trigger_context, payload):
                window = chat_turn_coalescing.window_seconds(
                    assignment.trigger_config, settings.chat_event_coalesce_window_seconds
                )
                if window:
                    extra_delay_seconds = window
                    trigger_context = {**(trigger_context or {}), chat_turn_coalescing.WINDOW_CONTEXT: window}

        if skip_if_active:
            # Make the check-then-insert atomic across concurrent triggers:
            # serialize per-assignment via a transaction-scoped advisory
            # lock BEFORE consulting has_active_run (see the helper's
            # docstring for the race this closes).
            await _acquire_trigger_overlap_lock(db, assignment.id)
            # ``active_run_user_id`` scopes the guard to that user's runs: a
            # per-user assignment runs once per user (``manual_trigger_overlap``).
            if active_run_user_id is not None:
                active = await has_active_run(db, assignment.id, user_id=active_run_user_id)
            elif max_active_runs > 1:
                active = await count_active_runs(db, assignment.id) >= max_active_runs
            else:
                active = await has_active_run(db, assignment.id)
            # A delivery never starts ahead of the deliveries its assignment
            # already queued: the caller hands it to the queue instead.
            if not active and delivery:
                active = await _has_promotable_queued_run(db, assignment.id)
            if active:
                logger.info(
                    "Skipping trigger for assignment '%s' (%s) — a run is "
                    "already active or queued (overlapping trigger suppressed, not queued)",
                    assignment.name,
                    assignment.id,
                )
                # End the transaction NOW so the advisory lock releases
                # immediately. Without this, the lock would stay held until
                # the caller's next commit/rollback — in webhook fan-out
                # loops and the long-lived scheduler session that means
                # skipped triggers pile up locks for the rest of the
                # handler/tick, blocking concurrent triggers for the same
                # assignments and inviting advisory-lock deadlocks. Commit
                # (not rollback) so any pending caller work is preserved —
                # the create path below already commits this same session,
                # so trigger_assignment is a transaction boundary either way.
                await db.commit()
                return None

        # One run per inbound event on the agent (``services/event_run_dedup.py``):
        # the key names the mail or the event modification whatever mailbox or
        # delivery path brought it, and a run of this agent that already carries
        # it within the window makes this one a ``skipped`` duplicate. Checked
        # under an agent-wide advisory lock, after the assignment lock above
        # when it was taken, so two assignments of one agent cannot both insert.
        # The key is recorded on the run even when the window is 0. A context
        # that already carries the key was checked by the caller under the
        # same transaction (``trigger_or_queue_assignment``): not asked again.
        already_checked = bool((trigger_context or {}).get(event_run_dedup.DEDUP_KEY_CONTEXT))
        dedup_key = event_run_dedup.run_dedup_key(
            trigger_context,
            payload,
            agent_id=_dedup_agent_id(assignment),
            trigger_config=getattr(assignment, "trigger_config", None),
            assignment_id=getattr(assignment, "id", None),
        )
        if dedup_key:
            trigger_context = event_run_dedup.with_dedup_key(trigger_context, dedup_key)
            window = event_run_dedup.dedup_window_seconds(
                assignment.trigger_config, settings.assignment_event_dedup_window_seconds, dedup_key
            )
            if window > 0 and not already_checked:
                await _acquire_agent_dedup_lock(db, assignment.agent_id)
                original = await duplicate_event_run(
                    db, assignment.agent_id, dedup_key, window_seconds=window
                )
                if original is not None:
                    return await _skip_duplicate_event_run(
                        db,
                        assignment,
                        agent,
                        payload,
                        trigger_context or {},
                        original_run_id=original,
                        window_seconds=window,
                        triggered_by_user_id=triggered_by_user_id,
                    )
                # No run of the agent has it: an agent account of the agent
                # may (``services/inbound_claims.py``).
                carrier = await _red_head_carried(db, assignment.agent_id, dedup_key)
                if carrier is not None:
                    return await _skip_duplicate_event_run(
                        db,
                        assignment,
                        agent,
                        payload,
                        trigger_context or {},
                        original_run_id=carrier,
                        window_seconds=window,
                        triggered_by_user_id=triggered_by_user_id,
                    )
                holder = await _event_claimed_elsewhere(assignment.agent_id, dedup_key, window)
                if holder is not None:
                    return await _skip_claimed_event_run(
                        db,
                        assignment,
                        agent,
                        payload,
                        trigger_context or {},
                        holder=holder,
                        window_seconds=window,
                        triggered_by_user_id=triggered_by_user_id,
                    )

        # Create the run record.
        #
        # Execution identity (``run.user_id``) precedence:
        #   1. The human who triggered it (manual UI click / agent-builder chat
        #      tool) — the run acts on behalf of that specific person.
        #   2. Otherwise (fully automatic: schedule / webhook / integration
        #      event), the assignment's configured "run as" user
        #      (company-managed agents), then the assignment owner.
        #
        # This is what drives per-user resolution downstream (integration
        # template bindings, LLM model, memory) via
        # ``effective_user_id = run.user_id or agent.user_id``.
        #
        # ``run.is_company_managed`` is the per-run VISIBILITY flag. It mirrors
        # the assignment's company-managed visibility, EXCEPT when the agent /
        # assignment opts into hiding runs (``hide_runs``): a hidden run is born
        # ``is_company_managed=False`` so the standard visibility filters
        # (``run_visible_filter`` / ``require_run_access``) scope it to the
        # executing user ALONE (its ``user_id``) instead of everyone - admins get
        # no special access to another user's hidden run, since admin run access is
        # gated on ``is_company_managed=True``. This is independent of the
        # execution identity (``user_id``).
        from app.services.assignment_helpers import resolve_hide_runs

        run_visible_to_all = assignment.is_company_managed and not resolve_hide_runs(
            agent, assignment
        )
        if continues_run is not None:
            run_visible_to_all = continuing_run_is_company_managed(
                continues_run.is_company_managed,
                fallback=False,
                ceiling=run_visible_to_all,
            )
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=assignment.agent_id,
            user_id=_execution_identity(assignment, triggered_by_user_id, agent),
            # Recorded separately from ``user_id`` so the run remembers whether a
            # human started it. ``user_id`` falls back to run-as or the
            # owner, so it is always set and cannot carry that signal.
            triggered_by_user_id=triggered_by_user_id,
            is_company_managed=run_visible_to_all,
            trigger_payload=payload or {},
            trigger_context=trigger_context,
            status="pending",
        )
        db.add(run)
        await db.flush()
        run_id = str(run.id)
        notice_user_id = triggered_by_user_id or run.user_id
        if owed_conversation_id is not None and notice_user_id is not None:
            db.add(
                AssignmentRunConversationNotice(
                    run_id=run.id,
                    conversation_id=owed_conversation_id,
                    user_id=notice_user_id,
                )
            )

        await _schedule_run_execution(
            db,
            assignment,
            agent,
            run,
            trigger_context=trigger_context,
            extra_delay_seconds=extra_delay_seconds,
        )

        return run_id

    finally:
        if should_close:
            await db.__aexit__(None, None, None)


# Set inside ``batched_wakes``: the runs scheduled there wake the reconciler
# once, when the block ends, instead of once each.
_wake_batch: ContextVar[list[bool] | None] = ContextVar("assignment_wake_batch", default=None)


async def _wake_reconciler() -> None:
    batch = _wake_batch.get()
    if batch is not None:
        batch.append(True)
        return
    await notify_orchestration_wake()


@contextlib.asynccontextmanager
async def batched_wakes() -> AsyncIterator[None]:
    """Wake the reconciler once for every run scheduled or queued in the block.

    A drain promoting ten runs, or a poll page dispatching fifteen events, is
    one wake, not ten or fifteen. The wake never raises and is skipped while
    the task is being cancelled: the reconciler's own interval covers it.
    """
    pending: list[bool] = []
    token = _wake_batch.set(pending)
    try:
        yield
    finally:
        _wake_batch.reset(token)
        task = asyncio.current_task()
        if pending and not (task is not None and task.cancelling()):
            with contextlib.suppress(Exception):
                await notify_orchestration_wake()


async def _schedule_run_execution(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    run: AssignmentRun,
    *,
    trigger_context: dict | None = None,
    extra_delay_seconds: int = 0,
) -> None:
    """Give a freshly-created (or de-queued) run its orchestration execution.

    Computes the effective delay + deadline, flips ``run.status`` to
    ``pending`` / ``delayed``, creates the durable execution row, commits, and
    nudges the reconciler. Shared by ``trigger_assignment`` (run created now)
    and ``_drain_one_assignment`` (run created when the delivery was accepted
    and parked in ``queued``), so a de-queued delivery is scheduled through
    exactly the same path — same delay, same iteration floor, same deadline
    budget — as one that never had to wait.
    """
    run_id = str(run.id)
    configured_delay_seconds = parse_delay(assignment.delay)
    schedule_spread_seconds = _schedule_spread_delay_seconds(
        assignment.id, trigger_context
    )
    delay_seconds = (
        configured_delay_seconds
        + schedule_spread_seconds
        + max(0, extra_delay_seconds)
    )

    now = datetime.now(UTC)
    run_at = now + timedelta(seconds=delay_seconds)
    # ``timeout_seconds == 0`` (or any non-positive) means "disable the
    # timeout" per the model, so set no orchestration deadline — otherwise
    # ``sweep_deadlines`` would dead-letter a run whose timeout the operator
    # deliberately turned off. ``None`` only happens on an unflushed
    # in-memory row → fall back to the shared
    # ``_UNSET_TIMEOUT_FALLBACK_SECONDS`` (same as the agentic loop's
    # ``asyncio.wait_for`` bound and the retry path).
    #
    # The deadline applies the SAME iteration floor as the agentic loop's
    # ``asyncio.wait_for`` bound in ``execute_assignment_run``: without it,
    # the reconciler's deadline-derived task budget (``_execution_timeout``)
    # and ``sweep_deadlines`` would still kill a high-``max_iterations`` run
    # at ``timeout_seconds`` + grace, defeating the floor.
    timeout_seconds = assignment.timeout_seconds
    if timeout_seconds is None:
        timeout_seconds = _UNSET_TIMEOUT_FALLBACK_SECONDS
    #
    # The budget counts from the run's START (``queued_deadline``): until a
    # worker claims it, ``deadline_at`` is the queue-wait bound; the claim
    # sets it to start + budget, so time spent waiting for a free slot is
    # never charged to the run.
    if timeout_seconds <= 0:
        deadline_at, start_budget = None, {}
    else:
        effective_timeout = _apply_iteration_timeout_floor(
            float(timeout_seconds),
            await resolve_run_max_iterations(db, assignment, agent),
            run_id,
            assignment.timeout_seconds,
        )
        deadline_at, start_budget = queued_deadline(
            run_at,
            effective_timeout + settings.orchestration_grace_seconds,
            max_queue_wait_seconds=max_queue_wait_seconds(assignment),
        )

    # Durable scheduling via the orchestration engine: the reconciler
    # claims this execution at ``run_at`` and runs it under a lease. The
    # execution row is the single source of truth for scheduling, delay,
    # leasing, deadlines, and crash recovery — there are no ``delayed_run:*``
    # Redis keys and no SAQ scheduled set. ``delayed`` vs ``pending`` is
    # purely the user-facing AssignmentRun status.
    run.status = "delayed" if delay_seconds > 0 else "pending"
    await create_execution(
        db,
        adapter="assignment",
        payload={"run_id": run_id, **start_budget},
        run_at=run_at,
        deadline_at=deadline_at,
        dedup_key=f"assignment_run:{run_id}",
        # Its first run and every resume it may make.
        max_attempts=assignment_run_resume.execution_max_attempts(),
    )
    await db.commit()
    await _wake_reconciler()
    logger.info(
        "Assignment '%s' run %s scheduled via orchestration engine "
        "(delay=%ds: configured=%ds, spread=%ds, extra=%ds)",
        assignment.name,
        run_id,
        delay_seconds,
        configured_delay_seconds,
        schedule_spread_seconds,
        max(0, extra_delay_seconds),
    )


# ---------------------------------------------------------------------------
# Per-assignment delivery queue
# ---------------------------------------------------------------------------
# ``trigger_assignment(skip_if_active=True)`` protects an assignment from
# running twice in parallel, but it does so by DISCARDING the second trigger.
# On the per-assignment webhook receiver that discard is a lost event: the
# sender (a SIEM/SOAR firing on every alert) got HTTP 200 and recorded a
# success for a run that never happened, and the loss was visible only as an
# INFO log. "One run per alert" was unachievable by design.
#
# The queue supplies the three things the guard declines to provide:
#
#   Ordering     FIFO per assignment, serialized by the SAME per-assignment
#                advisory lock (``_acquire_trigger_overlap_lock``) the guard
#                already uses, so enqueue and drain cannot race each other.
#                Up to the assignment's concurrency cap run at once;
#                runs sharing an ordering key run one at a time, in order.
#   Bounds       ``settings.assignment_queue_max_depth`` queued runs per
#                assignment with an explicit overflow policy. Dropping is
#                still allowed — dropping SILENTLY is not: every outcome is a
#                run row, a WARNING, or a 429 the sender can act on.
#   Backpressure the default ``reject`` policy answers 429 + ``Retry-After``
#                once the queue is full. Senders retry a 429; none retry a 200.
#
# A queued delivery is a real ``AssignmentRun`` row in the ``queued`` status —
# not a Redis list — so it survives a restart, is visible in the runs UI with
# its payload, and carries the same execution identity / visibility flags the
# run would have had if it had started immediately.


@dataclass(frozen=True)
class QueuedTriggerResult:
    """Outcome of ``trigger_or_queue_assignment``.

    ``status`` is one of:


    - ``"ok"``          — the assignment had a free slot; the run is scheduled
      now and ``run_id`` is its id (identical to a plain ``trigger_assignment``).
    - ``"queued"``      — every slot was taken (or the queue was non-empty);
      ``run_id`` is the queued run's id and ``queue_depth`` its position-ish
      depth AFTER the insert.
    - ``"queue_full"``  — the queue was at ``assignment_queue_max_depth`` under
      the ``reject`` policy. Nothing was created; the caller must answer 429
      with ``retry_after_seconds`` so the sender backs off and retries.
    - ``"dropped"``     — the queue was full under ``drop_newest``, or every
      slot was taken for a ``queue=False`` call. Nothing was created; the
      caller answers 200 and the drop is logged as a WARNING.

    - ``"duplicate"``   — a run of the same agent already carries this event's
      dedup key within the window (``services/event_run_dedup.py``); ``run_id``
      is the ``skipped`` run that records the refusal. Nothing executes.
    - ``"coalesced"``   — a chat message merged into the waiting turn of a run
      of the same conversation and sender (``services/chat_turn_coalescing.py``),
      or a polled event folded into the queued run of its coalesce key
      (``services/event_coalescing.py``); ``run_id`` is that run. No run is
      created.
    - ``"steered"``     — a chat message handed to the running turn of its
      conversation and sender, injected at the loop's next iteration
      (``services/chat_turn_steering.py``); ``run_id`` is that run. No
      run is created; a message the run never takes in is queued as the next
      turn when it ends.

    ``dropped_run_id`` is set only under ``drop_oldest``: the queued run that
    was evicted (and finalized as ``skipped``) to make room for this one.

    ``reason`` says why a ``dropped`` delivery was dropped when it is not the
    queue: ``quota_exceeded`` for a delivery whose agentic quota is spent.
    """

    status: str
    run_id: str | None = None
    queue_depth: int = 0
    dropped_run_id: str | None = None
    retry_after_seconds: int | None = None
    reason: str | None = None


# Marker prefix on an evicted / abandoned queued run's ``error`` so the reason a
# delivery never ran is greppable and distinguishable from an execution failure.
QUEUE_DROP_ERROR_PREFIX = "Delivery dropped by the assignment queue:"

# Marks, in its ``trigger_context``, a queued run an integration event created
# (``INTEGRATION_EVENT_QUEUE_ENABLED``). ``ASSIGNMENT_QUEUE_ENABLED=false``
# turns the per-assignment webhook queue off: the drain then promotes these runs
# only, and leaves anything else queued as it always did.
QUEUE_ORIGIN_KEY = "queue_origin"
INTEGRATION_EVENT_QUEUE_ORIGIN = "integration_event"


def _run_event_latency(
    run: Any, original_status: str | None, *, trigger_type: str | None = None
) -> dict[str, Any] | None:
    """The ``event_latency`` trace metadata of an event-triggered run's first start.

    A HITL resume or a resumed attempt starts again long after the event: only
    a first start is measured. ``trigger_type`` is the assignment's: a provider
    webhook delivers an ``integration_event`` run with ``type: "webhook"`` in
    its context. Never raises.
    """
    if original_status == "awaiting_human":
        return None
    try:
        from app.services import background_checkpoint
        from app.services.tracing import event_latency

        context = getattr(run, "trigger_context", None)
        if background_checkpoint.resumes_of(context):
            return None
        figures = event_latency.event_latency(
            context,
            created_at=getattr(run, "created_at", None),
            started_at=getattr(run, "started_at", None),
            trigger_type=trigger_type,
        )
        event_latency.observe(figures)
        return figures
    except Exception:
        logger.debug("Event-to-run latency not recorded", exc_info=True)
        return None


def _integration_event_queued_clause():
    return AssignmentRun.trigger_context[QUEUE_ORIGIN_KEY].as_string() == INTEGRATION_EVENT_QUEUE_ORIGIN


def _queue_max_depth() -> int:
    """Effective queue depth cap (clamped so a 0/negative can't disable it)."""
    return max(1, int(settings.assignment_queue_max_depth))


def _queue_retry_after_seconds() -> int:
    """Effective ``Retry-After`` for a full queue (clamped to >= 1s)."""
    return max(1, int(settings.assignment_queue_retry_after_seconds))


async def count_queued_runs(db: AsyncSession, assignment_id: _uuid.UUID) -> int:
    """Number of accepted-but-not-yet-started deliveries for an assignment."""
    result = await db.execute(
        select(func.count())
        .select_from(AssignmentRun)
        .where(
            AssignmentRun.assignment_id == assignment_id,
            AssignmentRun.status == QUEUED_RUN_STATUS,
        )
    )
    return int(result.scalar() or 0)


async def count_queued_runs_by_assignment(
    db: AsyncSession, assignment_ids: list[_uuid.UUID]
) -> dict[_uuid.UUID, int]:
    """Batched ``count_queued_runs`` for a list view (empty entries omitted)."""
    if not assignment_ids:
        return {}
    result = await db.execute(
        select(AssignmentRun.assignment_id, func.count())
        .where(
            AssignmentRun.assignment_id.in_(assignment_ids),
            AssignmentRun.status == QUEUED_RUN_STATUS,
        )
        .group_by(AssignmentRun.assignment_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def full_queue_assignment_ids(
    db: AsyncSession, assignment_ids: list[_uuid.UUID]
) -> set[_uuid.UUID]:
    """The assignments among *assignment_ids* whose delivery queue is at its cap."""
    counts = await count_queued_runs_by_assignment(db, assignment_ids)
    max_depth = _queue_max_depth()
    return {assignment_id for assignment_id, depth in counts.items() if depth >= max_depth}


def _queue_blocking_statuses() -> list[str]:
    """Run statuses that hold one of the assignment's concurrency slots.

    ``awaiting_human`` is included while
    ``assignment_queue_hold_on_awaiting_human`` is on, and it then holds every
    slot, not one (``_SlotUsage.held``): a HITL-paused run still owns the
    assignment's tools and DB writes, so the queue waits for the answer, as it
    always did, whatever the concurrency cap. What the queue changes is that
    the hold is no longer a silent, unbounded loss of intake — deliveries
    accumulate as visible ``queued`` rows and the sender gets a 429 once the
    cap is reached.
    """
    if settings.assignment_queue_hold_on_awaiting_human:
        return list(ACTIVE_RUN_STATUSES)
    return [s for s in ACTIVE_RUN_STATUSES if s != "awaiting_human"]


def _holds_on_approval(status: str) -> bool:
    return status == "awaiting_human" and settings.assignment_queue_hold_on_awaiting_human


def _non_delivery_run_clause():
    """A run that has its assignment to itself (not in ``assignment_dispatch.SHARED_SLOT_TRIGGER_TYPES``)."""
    run_type = AssignmentRun.trigger_context["type"].as_string()
    return or_(
        AssignmentRun.trigger_context.is_(None),
        run_type.is_(None),
        run_type.not_in(sorted(assignment_dispatch.SHARED_SLOT_TRIGGER_TYPES)),
    )


@dataclass(frozen=True)
class _SlotUsage:
    """The runs holding an assignment's slots, and the ordering keys they hold.

    ``held``: a run waits for a human while ``ASSIGNMENT_QUEUE_HOLD_ON_AWAITING_HUMAN``
    is on, so nothing else starts until it is answered. ``exclusive``: a run
    that shares no slot - manual, follow-up, scheduled, chained - is in
    flight, and deliveries and platform events wait for it.
    """

    active: int
    busy_keys: frozenset[str]
    held: bool = False
    exclusive: bool = False

    def has_room(self, cap: int) -> bool:
        return not self.held and not self.exclusive and self.active < cap


async def _slot_usage(db: AsyncSession, assignment_id: _uuid.UUID) -> _SlotUsage:
    result = await db.execute(
        select(
            AssignmentRun.status,
            AssignmentRun.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string(),
            AssignmentRun.trigger_context["type"].as_string(),
        ).where(
            AssignmentRun.assignment_id == assignment_id,
            holds_assignment(AssignmentRun, _queue_blocking_statuses()),
        )
    )
    rows = result.all()
    return _SlotUsage(
        active=len(rows),
        busy_keys=frozenset(row[1] for row in rows if row[1]),
        held=any(_holds_on_approval(row[0]) for row in rows),
        exclusive=any(not assignment_dispatch.shares_slots(row[2]) for row in rows),
    )


async def _assignment_cap(assignment: Assignment) -> int:
    """The assignment's concurrency cap, halved while a provider answers it 429."""
    cap = assignment_dispatch.assignment_concurrency_cap(assignment.trigger_config)
    throttled = await assignment_dispatch.is_throttled(assignment.id)
    return assignment_dispatch.throttled_cap(cap) if throttled else cap


def _promotable_queued_conditions(assignment_id: _uuid.UUID) -> list:
    """The queued runs of an assignment the drain promotes."""
    conditions = [
        AssignmentRun.assignment_id == assignment_id,
        AssignmentRun.status == QUEUED_RUN_STATUS,
    ]
    if not settings.assignment_queue_enabled:
        conditions.append(_integration_event_queued_clause())
    return conditions


async def _ordering_key_queued(
    db: AsyncSession, assignment_id: _uuid.UUID, ordering_key: str
) -> bool:
    """Whether a queued run the drain will promote carries this ordering key."""
    result = await db.execute(
        select(AssignmentRun.id)
        .where(
            *_promotable_queued_conditions(assignment_id),
            AssignmentRun.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string()
            == ordering_key,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _has_promotable_queued_run(db: AsyncSession, assignment_id: _uuid.UUID) -> bool:
    """Whether the assignment has a queued run the drain will promote."""
    result = await db.execute(
        select(AssignmentRun.id).where(*_promotable_queued_conditions(assignment_id)).limit(1)
    )
    return result.scalar_one_or_none() is not None


#: How a run refused by the agentic quota at dequeue starts its ``error``.
QUOTA_SKIP_PREFIX = "Skipped: quota exceeded:"
#: The dropped-event reason a delivery refused by the quota is counted under.
QUOTA_DROP_REASON = "quota_exceeded"
#: Set on the caller's ``trigger_context`` once a delivery was dropped for the
#: quota, so the busy fallback (``queue_busy_pushed_event``) a receiver calls
#: after ``trigger_assignment`` answered ``None`` neither checks nor counts it twice.
QUOTA_DROPPED_CONTEXT = "quota_dropped"
_QUOTA_DROP_LOG_KEY_PREFIX = "assignment:quota_drop_logged:"
_QUOTA_DROP_LOG_SECONDS = 3600


async def _notify_quota_exhausted(assignment: Any, agent: Any, error: str, *, where: str) -> None:
    """The once-a-day notice to the agent's owner and the administrators; never raises."""
    try:
        from app.services.quota_exhausted_notice import notify_quota_exhausted_once

        await notify_quota_exhausted_once(assignment, agent, error, where=where)
    except Exception as exc:
        logger.debug("Quota exhausted notice skipped for assignment %s: %r", getattr(assignment, "id", None), exc)


async def _log_quota_drop_once(assignment: Assignment, error: str) -> None:
    """One warning per assignment per hour for its quota drops, never one per event."""
    try:
        from app.core.redis import redis_client

        redis = await asyncio.wait_for(redis_client(), 0.5)
        first = await asyncio.wait_for(
            redis.set(
                f"{_QUOTA_DROP_LOG_KEY_PREFIX}{assignment.id}", "1", nx=True, ex=_QUOTA_DROP_LOG_SECONDS
            ),
            0.5,
        )
    except Exception:
        first = True
    if first:
        logger.warning(
            "Assignment '%s' (%s): deliveries dropped without a run while its agentic "
            "quota is exhausted (%s); counted as filtered events, reason %s. Logged "
            "once per hour.",
            assignment.name,
            assignment.id,
            error,
            QUOTA_DROP_REASON,
        )


async def _drop_delivery_over_quota(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    trigger_context: dict[str, Any] | None,
) -> bool:
    """``True`` when this delivery must be dropped because its quota is spent.

    A webhook or integration event for an assignment whose acting user (or the
    platform) has exhausted the agentic quota creates no run: the run would be
    refused at dequeue anyway, after its gate had spent a model call. The drop
    is counted like a filtered event (``events_filtered_count`` and the reason
    ``quota_exceeded``), logged once per assignment per hour, and publishes
    nothing. A quota store that cannot answer drops nothing: the run's own
    check decides, under ``QUOTA_FAIL_MODE``. Any other trigger is untouched.
    """
    if trigger_context is None or not assignment_dispatch.is_delivery(trigger_context):
        return False
    if trigger_context.get(QUOTA_DROPPED_CONTEXT) is True:
        return True
    if getattr(agent, "is_builtin", False) and trigger_context.get("quota_exempt") is True:
        return False
    from app.services.integration_event_filters import record_filter_reasons, record_filtered_events
    from app.services.quota import QuotaExceededError, check_agentic_quota

    # The identity the run would take (``_execution_identity``), read tolerantly:
    # a pre-check must never be what breaks a delivery.
    user_id = (
        getattr(assignment, "run_as_user_id", None)
        or getattr(assignment, "user_id", None)
        or getattr(agent, "user_id", None)
    )
    try:
        await check_agentic_quota(db, user_id)
        return False
    except QuotaExceededError as qe:
        error = str(qe)
    except Exception as exc:
        logger.debug("Quota pre-check of assignment %s could not run: %r", assignment.id, exc)
        return False
    trigger_context[QUOTA_DROPPED_CONTEXT] = True
    await record_filtered_events(assignment.id, 1)
    await record_filter_reasons(assignment.id, {QUOTA_DROP_REASON: 1})
    await _log_quota_drop_once(assignment, error)
    await _notify_quota_exhausted(assignment, agent, error, where="delivery")
    return True


async def trigger_or_queue_assignment(
    assignment: Assignment,
    agent: Agent,
    payload: dict | None = None,
    *,
    db: AsyncSession,
    trigger_context: dict | None = None,
    overflow: str | None = None,
    queue: bool = True,
) -> QueuedTriggerResult:
    """Trigger an assignment, or queue the delivery when it has no free slot.

    The bounded, ordered, back-pressured alternative to
    ``trigger_assignment(skip_if_active=True)`` for callers whose dropped
    trigger is a lost external event.

    Everything below happens under the per-assignment advisory lock the overlap
    guard already takes, so concurrent deliveries — across workers and pods —
    are serialized: they start while the assignment has fewer runs in flight
    than its concurrency cap (``assignment_dispatch.assignment_concurrency_cap``),
    and the rest queue in arrival order. A delivery whose ordering key
    (``trigger_config.ordering_key``) a run in flight already holds queues too.

    A NON-EMPTY queue is itself a reason to queue even when a slot is free:
    starting this delivery immediately would let it overtake deliveries that
    arrived earlier and are waiting for the drain the enqueue wakes, breaking
    the FIFO guarantee for no benefit.

    The per-assignment webhook checks ``settings.assignment_queue_enabled``
    itself and falls back to the legacy drop-and-report behaviour when it is
    off (``status="skipped"``, surfaced by the caller). The integration-event
    callers (``overflow`` set) queue whatever that setting says, and their runs
    are marked so the drain still promotes them.

    ``overflow`` overrides ``ASSIGNMENT_QUEUE_OVERFLOW_POLICY`` for a caller that
    must never drop: ``"reject"`` answers ``queue_full`` so the event can
    be handed back, ``"persist"`` queues past the cap. The per-assignment webhook
    passes nothing and keeps the configured policy.

    ``queue=False`` starts the delivery when a slot is free and otherwise
    answers ``dropped`` without queuing it: a pushed integration event with
    ``INTEGRATION_EVENT_QUEUE_ENABLED`` off, whose skip stands only once every
    slot is taken. It keeps the order of its ordering key - a delivery whose
    key a queued run carries is dropped rather than run ahead of it - but an
    unordered one may start ahead of the queue: dropping it for a queue it
    could not join would lose it for nothing.

    Commits the session on every path (the advisory lock must not outlive the
    call), exactly like ``trigger_assignment``.
    """
    await _refuse_if_ai_disabled(assignment)
    await _refuse_if_group_ai_disabled(_run_person(assignment, None, agent))

    if not assignment.enabled:
        raise ValueError(f"Assignment '{assignment.name}' is disabled")
    if not agent.enabled:
        raise ValueError(f"Agent '{agent.name}' is disabled")
    await refuse_if_owner_deactivated(db, assignment)

    # A delivery whose quota is spent creates no run.
    if await _drop_delivery_over_quota(db, assignment, agent, trigger_context):
        return QueuedTriggerResult(status="dropped", reason=QUOTA_DROP_REASON)

    # Read before the lock: a Redis round trip must not hold it.
    cap = await _assignment_cap(assignment)
    trigger_context = assignment_dispatch.with_ordering_key(
        assignment.trigger_config, payload, trigger_context
    )
    ordering_key = (trigger_context or {}).get(assignment_dispatch.ORDERING_KEY_CONTEXT)

    await _acquire_trigger_overlap_lock(db, assignment.id)

    # A duplicate of a run this agent already started for the same inbound
    # event is refused before any slot or queue decision, whichever assignment
    # and delivery path started the original (``services/event_run_dedup.py``).
    dedup_key = event_run_dedup.run_dedup_key(
        trigger_context,
        payload,
        agent_id=_dedup_agent_id(assignment),
        trigger_config=getattr(assignment, "trigger_config", None),
        assignment_id=getattr(assignment, "id", None),
    )
    if dedup_key:
        trigger_context = event_run_dedup.with_dedup_key(trigger_context, dedup_key)
        window = event_run_dedup.dedup_window_seconds(
            assignment.trigger_config, settings.assignment_event_dedup_window_seconds, dedup_key
        )
        if window > 0:
            await _acquire_agent_dedup_lock(db, assignment.agent_id)
            original = await duplicate_event_run(db, assignment.agent_id, dedup_key, window_seconds=window)
            if original is not None:
                skipped_id = await _skip_duplicate_event_run(
                    db,
                    assignment,
                    agent,
                    payload,
                    trigger_context or {},
                    original_run_id=original,
                    window_seconds=window,
                )
                return QueuedTriggerResult(status="duplicate", run_id=skipped_id)
            carrier = await _red_head_carried(db, assignment.agent_id, dedup_key)
            if carrier is not None:
                skipped_id = await _skip_duplicate_event_run(
                    db,
                    assignment,
                    agent,
                    payload,
                    trigger_context or {},
                    original_run_id=carrier,
                    window_seconds=window,
                )
                return QueuedTriggerResult(status="duplicate", run_id=skipped_id)
            holder = await _event_claimed_elsewhere(assignment.agent_id, dedup_key, window)
            if holder is not None:
                skipped_id = await _skip_claimed_event_run(
                    db,
                    assignment,
                    agent,
                    payload,
                    trigger_context or {},
                    holder=holder,
                    window_seconds=window,
                )
                return QueuedTriggerResult(status="duplicate", run_id=skipped_id)

    if chat_turn_coalescing.forces_queue(trigger_context, payload):
        # A chat message is never dropped for a busy assignment nor run beside
        # the run of its conversation: it queues behind it, or joins
        # the turn of a run of the same sender still waiting to start.
        queue = True
        overflow = overflow or "persist"
        if ordering_key is not None:
            merged_into = await _merge_into_waiting_turn(db, assignment, ordering_key, payload)
            if merged_into is not None:
                await db.commit()
                logger.info(
                    "Assignment '%s' (%s): chat message %s merged into the waiting turn of run %s",
                    assignment.name,
                    assignment.id,
                    (payload or {}).get("message_id") or "?",
                    merged_into,
                )
                return QueuedTriggerResult(status="coalesced", run_id=merged_into)
            steered_into = await _steer_into_running_turn(db, assignment, ordering_key, payload, trigger_context)
            if steered_into is not None:
                await db.commit()
                logger.info(
                    "Assignment '%s' (%s): chat message %s steered into the running turn of run %s",
                    assignment.name,
                    assignment.id,
                    (payload or {}).get("message_id") or "?",
                    steered_into,
                )
                return QueuedTriggerResult(status="steered", run_id=steered_into)

    coalesce_key = event_coalescing.context_key(trigger_context)
    if coalesce_key is not None:
        # A burst of polled events that stand for one change (an OpenCRQ batch
        # recompute, #1417): folded into the run of the same key still waiting
        # in the queue, so at most one waits behind the one running.
        folded_into = await _fold_into_queued_run(db, assignment, coalesce_key, payload)
        if folded_into is not None:
            await db.commit()
            await _release_event_claim(assignment.agent_id, dedup_key)
            logger.info(
                "Assignment '%s' (%s): event %s folded into queued run %s (coalesce key %s)",
                assignment.name,
                assignment.id,
                (payload or {}).get("message_id") or "?",
                folded_into,
                coalesce_key,
            )
            return QueuedTriggerResult(status="coalesced", run_id=folded_into)

    batch_window = event_batching.window_seconds(assignment.trigger_config, trigger_context)
    if batch_window:
        # An observer with a batch window (``services/event_batching.py``): the
        # event joins the batch run still waiting, else opens a new batch run
        # that starts once the window ends.
        joined = await _join_waiting_batch(db, assignment, payload)
        if joined is not None:
            await db.commit()
            await _release_event_claim(assignment.agent_id, dedup_key)
            logger.info(
                "Assignment '%s' (%s): event %s joined batch run %s",
                assignment.name,
                assignment.id,
                (payload or {}).get("event_id") or (payload or {}).get("message_id") or "?",
                joined,
            )
            return QueuedTriggerResult(status="coalesced", run_id=joined)
        payload = event_batching.batch_payload(payload, batch_window)
        trigger_context = {**(trigger_context or {}), event_batching.WINDOW_CONTEXT: batch_window}

    usage = await _slot_usage(db, assignment.id)
    depth = await count_queued_runs(db, assignment.id)
    start_now = usage.has_room(cap) and ordering_key not in usage.busy_keys
    if start_now and depth:
        if queue:
            start_now = False
        elif ordering_key is not None:
            start_now = not await _ordering_key_queued(db, assignment.id, ordering_key)
    if start_now:
        # A free slot: behave EXACTLY like the legacy trigger. Delegating
        # (rather than re-implementing the insert) keeps run identity,
        # visibility and scheduling in one place. ``skip_if_active=False``
        # because we already did the check ourselves — under the lock this call
        # inherits, and which its commit releases.
        run_id = await trigger_assignment(
            assignment=assignment,
            agent=agent,
            payload=payload,
            db=db,
            trigger_context=trigger_context,
            extra_delay_seconds=batch_window,
        )
        return QueuedTriggerResult(status="ok", run_id=run_id, queue_depth=0)
    if not queue:
        await db.commit()
        await _release_event_claim(assignment.agent_id, dedup_key)
        return QueuedTriggerResult(status="dropped")

    max_depth = _queue_max_depth()
    dropped_run_id: str | None = None
    if depth >= max_depth and overflow == "persist":
        # A busy integration event with no one to retry it: queued past
        # the cap rather than lost, and said so.
        logger.warning(
            "Assignment '%s' (%s) delivery queue is full (%d/%d) - queuing this "
            "integration event past the cap: it has no sender that would retry it",
            assignment.name,
            assignment.id,
            depth,
            max_depth,
        )
    elif depth >= max_depth:
        policy = overflow or settings.assignment_queue_overflow_policy
        if policy == "reject":
            # Commit to release the advisory lock before answering 429 — the
            # rejected delivery must not hold the lock while the sender's
            # retry (or a concurrent delivery) waits on it.
            await db.commit()
            await _release_event_claim(assignment.agent_id, dedup_key)
            logger.warning(
                "Assignment '%s' (%s) delivery queue is full (%d/%d) — "
                "rejecting delivery with 429 so the sender retries",
                assignment.name,
                assignment.id,
                depth,
                max_depth,
            )
            return QueuedTriggerResult(
                status="queue_full",
                queue_depth=depth,
                retry_after_seconds=_queue_retry_after_seconds(),
            )
        if policy == "drop_newest":
            await db.commit()
            await _release_event_claim(assignment.agent_id, dedup_key)
            logger.warning(
                "Assignment '%s' (%s) delivery queue is full (%d/%d) — "
                "DROPPING this delivery (ASSIGNMENT_QUEUE_OVERFLOW_POLICY="
                "drop_newest); it will never produce a run",
                assignment.name,
                assignment.id,
                depth,
                max_depth,
            )
            return QueuedTriggerResult(status="dropped", queue_depth=depth)
        # drop_oldest — evict the head of the queue so the freshest deliveries
        # win. The evicted run is finalized as ``skipped`` rather than deleted
        # so the loss stays visible as a run row carrying its payload.
        dropped_run_id = await _evict_oldest_queued_run(
            db, assignment, depth=depth, max_depth=max_depth
        )
        depth = await count_queued_runs(db, assignment.id)

    if overflow is not None:
        trigger_context = {**(trigger_context or {}), QUEUE_ORIGIN_KEY: INTEGRATION_EVENT_QUEUE_ORIGIN}
    run_id = await _create_queued_run(db, assignment, agent, payload, trigger_context)
    depth += 1
    # The single commit for this path: it publishes the new queued run together
    # with any ``drop_oldest`` eviction above, and releases the advisory lock.
    await db.commit()
    logger.info(
        "Assignment '%s' (%s): %d/%d runs in flight - delivery queued as run %s "
        "(queue depth %d/%d)",
        assignment.name,
        assignment.id,
        usage.active,
        cap,
        run_id,
        depth,
        max_depth,
    )
    # The drain promotes it as soon as a slot frees; waking it here covers the
    # delivery queued behind a queue that still had free slots.
    await _wake_reconciler()
    return QueuedTriggerResult(
        status="queued",
        run_id=run_id,
        queue_depth=depth,
        dropped_run_id=dropped_run_id,
    )


async def _fold_into_queued_run(
    db: AsyncSession,
    assignment: Assignment,
    coalesce_key: str,
    payload: dict[str, Any] | None,
) -> str | None:
    """Fold a coalescing event into the queued run of the same key; its id, or ``None``.

    The oldest queued run of *assignment* whose trigger context carries
    *coalesce_key* (``services/event_coalescing.py``), locked so two events
    folding at once never lose one. Flushes, the caller commits.
    """
    result = await db.execute(
        select(AssignmentRun)
        .where(
            AssignmentRun.assignment_id == assignment.id,
            AssignmentRun.status == QUEUED_RUN_STATUS,
            AssignmentRun.trigger_context[event_coalescing.CONTEXT_KEY].as_string() == coalesce_key,
        )
        .order_by(AssignmentRun.created_at.asc())
        .limit(1)
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    run.trigger_payload = event_coalescing.merged_payload(run.trigger_payload or {}, payload or {})
    await db.flush()
    return str(run.id)


async def _join_waiting_batch(
    db: AsyncSession,
    assignment: Assignment,
    payload: dict[str, Any] | None,
) -> str | None:
    """Append an event to the batch run of *assignment* still waiting; its id, or ``None``.

    The newest batch run (``event_batching.WINDOW_CONTEXT`` in its context)
    that has not started: queued, or delayed in its window with more than
    ``chat_turn_coalescing.MERGE_MARGIN_SECONDS`` left. Locked so two events
    joining at once never lose one. A full batch answers ``None``: the event
    opens the next one. Flushes, the caller commits.
    """
    from app.models.orchestration import OrchestrationExecution

    result = await db.execute(
        select(AssignmentRun)
        .where(
            AssignmentRun.assignment_id == assignment.id,
            AssignmentRun.status.in_((QUEUED_RUN_STATUS, "delayed")),
            AssignmentRun.trigger_context[event_batching.WINDOW_CONTEXT].as_string().is_not(None),
        )
        .order_by(AssignmentRun.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    if event_batching.carries(run.trigger_payload, payload):
        return str(run.id)
    if not event_batching.can_join(run.trigger_payload):
        return None
    if run.status != QUEUED_RUN_STATUS:
        run_at = (
            await db.execute(
                select(OrchestrationExecution.run_at).where(
                    OrchestrationExecution.dedup_key == f"assignment_run:{run.id}",
                    OrchestrationExecution.state == "pending",
                )
            )
        ).scalar_one_or_none()
        if not chat_turn_coalescing.still_open(run_at, datetime.now(UTC)):
            return None
    run.trigger_payload = event_batching.merged_payload(run.trigger_payload or {}, payload or {})
    await db.flush()
    return str(run.id)


async def _merge_into_waiting_turn(
    db: AsyncSession,
    assignment: Assignment,
    ordering_key: str,
    payload: dict | None,
) -> str | None:
    """Merge a chat message into the waiting run of its conversation; its id, or ``None``.

    The newest run of the conversation (``ordering_key``) that has not started:
    queued behind a run in flight, or delayed in its coalescing window with
    more than ``MERGE_MARGIN_SECONDS`` left. Joined only when the sender is the
    same (``chat_turn_coalescing.can_merge``); the run row is locked so two
    messages merging at once never lose one. Flushes, the caller commits.
    """
    from app.models.orchestration import OrchestrationExecution

    result = await db.execute(
        select(AssignmentRun)
        .where(
            AssignmentRun.assignment_id == assignment.id,
            AssignmentRun.status.in_((QUEUED_RUN_STATUS, "delayed")),
            AssignmentRun.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string() == ordering_key,
        )
        .order_by(AssignmentRun.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    if chat_turn_coalescing.carries(run.trigger_payload, payload):
        # The same message delivered again (a notification and the poll behind
        # it): its turn already holds it.
        return str(run.id)
    if not chat_turn_coalescing.can_merge(run.trigger_payload, payload):
        return None
    if run.status != QUEUED_RUN_STATUS:
        run_at = (
            await db.execute(
                select(OrchestrationExecution.run_at).where(
                    OrchestrationExecution.dedup_key == f"assignment_run:{run.id}",
                    OrchestrationExecution.state == "pending",
                )
            )
        ).scalar_one_or_none()
        if not chat_turn_coalescing.still_open(run_at, datetime.now(UTC)):
            return None
    run.trigger_payload = chat_turn_coalescing.merged_payload(run.trigger_payload or {}, payload or {})
    await db.flush()
    return str(run.id)


async def _steer_into_running_turn(
    db: AsyncSession,
    assignment: Assignment,
    ordering_key: str,
    payload: dict | None,
    trigger_context: dict | None,
) -> str | None:
    """Hand a chat message to the running turn of its conversation; that run's id, or ``None``.

    Only when no turn of the conversation waits to start: a message never
    overtakes one that arrived before it (``chat_turn_steering.offer`` checks
    the sender and the live run). Called under the trigger lock.
    """
    from app.services import chat_turn_steering

    if not chat_turn_steering.enabled_for(assignment.trigger_config):
        return None
    conversation = (payload or {}).get(assignment_dispatch.CONVERSATION_KEY_FIELD)
    if not conversation:
        return None
    waiting = await db.execute(
        select(AssignmentRun.id)
        .where(
            AssignmentRun.assignment_id == assignment.id,
            AssignmentRun.status.in_((QUEUED_RUN_STATUS, "delayed")),
            AssignmentRun.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string() == ordering_key,
        )
        .limit(1)
    )
    if waiting.scalar_one_or_none() is not None:
        return None
    return await chat_turn_steering.offer(assignment.id, str(conversation), payload, trigger_context)


async def _requeue_unsteered(assignment_id: _uuid.UUID, entries: list[dict]) -> None:
    """Queue, as the conversation's next turn, the messages a run never took in mid-run.

    They were handed to the running turn and arrived after its last iteration
    (or the run ended first): each goes back through
    ``trigger_or_queue_assignment``, so it merges into a waiting turn of the
    same sender or queues behind the run, exactly as if steering were off.
    """
    async with async_session() as db:
        assignment = (
            await db.execute(select(Assignment).where(Assignment.id == assignment_id))
        ).scalar_one_or_none()
        agent = None
        if assignment is not None:
            agent = (await db.execute(select(Agent).where(Agent.id == assignment.agent_id))).scalar_one_or_none()
        if assignment is None or agent is None:
            logger.warning(
                "Assignment %s: %d chat message(s) its run never took in are lost: the assignment or its agent is gone",
                assignment_id,
                len(entries),
            )
            return
        for entry in entries:
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
            context = entry.get("trigger_context") if isinstance(entry.get("trigger_context"), dict) else None
            if payload is None:
                continue
            try:
                result = await trigger_or_queue_assignment(
                    assignment, agent, payload, db=db, trigger_context=context, overflow="persist"
                )
            except Exception as exc:
                await db.rollback()
                logger.warning(
                    "Assignment %s: chat message %s its run never took in could not be queued: %s",
                    assignment_id,
                    payload.get("message_id") or "?",
                    format_exception_message(exc),
                )
                continue
            logger.info(
                "Assignment %s: chat message %s its run never took in is the next turn (%s, run %s)",
                assignment_id,
                payload.get("message_id") or "?",
                result.status,
                result.run_id,
            )


async def queue_busy_integration_event(
    assignment: Assignment,
    agent: Agent,
    payload: dict | None = None,
    *,
    db: AsyncSession,
    trigger_context: dict | None = None,
    on_full: str = "hand_back",
) -> QueuedTriggerResult:
    """Start or queue an integration event its assignment was busy for.

    Called after ``trigger_assignment(skip_if_active=True)`` answered ``None``,
    i.e. a run of the assignment is in flight. The event still starts at once
    when the assignment has a free slot (``assignment_max_concurrent_runs``).
    Otherwise, with ``INTEGRATION_EVENT_QUEUE_ENABLED`` off (the
    default) ``dropped`` is returned and nothing is created: a pushed
    event is skipped, and a Slack event is left to the poller.
    On, the event becomes a ``queued`` run in the per-assignment delivery queue
    (``trigger_or_queue_assignment``) that the reconciler starts as slots free,
    and the queue's drop policies do not apply to it. An idle assignment never
    reaches this, so its events run exactly as before either way.

    When the queue is at ``ASSIGNMENT_QUEUE_MAX_DEPTH``, *on_full* says what keeps
    the event:

    - ``"hand_back"`` (a polled event): nothing is created and ``queue_full`` is
      returned; the poller gives the event and its cursor back, so a later tick
      fetches it again.
    - ``"persist"`` (a pushed event, whose sender cannot be relied on to retry):
      the event is queued past the cap, with a warning.
    """
    if on_full not in ("hand_back", "persist"):
        raise ValueError(f"on_full must be 'hand_back' or 'persist', not {on_full!r}")
    return await trigger_or_queue_assignment(
        assignment,
        agent,
        payload,
        db=db,
        trigger_context=trigger_context,
        overflow="reject" if on_full == "hand_back" else "persist",
        queue=settings.integration_event_queue_enabled,
    )


async def queue_busy_pushed_event(
    assignment: Assignment,
    agent: Agent,
    payload: dict | None = None,
    *,
    db: AsyncSession,
    trigger_context: dict | None = None,
) -> str | None:
    """The run of a pushed event its busy assignment could not take at once.

    The receiver-side form of :func:`queue_busy_integration_event` with
    ``on_full="persist"``: webhook receivers and notification endpoints call it
    after the overlap guard skipped the delivery. The run started in a free
    slot, or the queued one; ``None`` when every slot is taken and
    ``INTEGRATION_EVENT_QUEUE_ENABLED`` is off: the skip stands.
    """
    result = await queue_busy_integration_event(
        assignment, agent, payload, db=db, trigger_context=trigger_context, on_full="persist"
    )
    return result.run_id


async def _create_queued_run(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    payload: dict | None,
    trigger_context: dict | None,
) -> str:
    """Park an accepted delivery as a ``queued`` run and commit.

    No orchestration execution is created — ``drain_assignment_queues``
    promotes the run (through ``_schedule_run_execution``, the same path an
    immediate trigger takes) once the assignment goes idle.

    Execution identity and visibility are resolved HERE, at acceptance time,
    not at promotion time: they must reflect the delivery's provenance, and
    re-resolving later would silently re-attribute a waiting delivery if the
    assignment's "run as" user or visibility changed in the meantime. The
    precedence mirrors ``trigger_assignment``'s automatic-trigger arm — there
    is no ``triggered_by_user_id`` on a webhook, so the assignment's "run as"
    user, then its owner.

    Flushes but does NOT commit: ``trigger_or_queue_assignment`` owns the one
    commit per path, so an eviction and the insert that replaced it land
    atomically and the advisory lock is not released mid-decision.
    """
    from app.services.assignment_helpers import resolve_hide_runs

    run_visible_to_all = assignment.is_company_managed and not resolve_hide_runs(
        agent, assignment
    )
    run = AssignmentRun(
        assignment_id=assignment.id,
        agent_id=assignment.agent_id,
        user_id=assignment.run_as_user_id or assignment.user_id,
        is_company_managed=run_visible_to_all,
        trigger_payload=payload or {},
        trigger_context=trigger_context,
        status=QUEUED_RUN_STATUS,
    )
    db.add(run)
    await db.flush()
    return str(run.id)


async def _evict_oldest_queued_run(
    db: AsyncSession,
    assignment: Assignment,
    *,
    depth: int,
    max_depth: int,
) -> str | None:
    """Finalize the oldest queued run as ``skipped`` to free a queue slot."""
    oldest = (
        await db.execute(
            select(AssignmentRun)
            .where(
                AssignmentRun.assignment_id == assignment.id,
                AssignmentRun.status == QUEUED_RUN_STATUS,
            )
            .order_by(AssignmentRun.created_at, AssignmentRun.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if oldest is None:
        return None
    oldest.status = "skipped"
    oldest.error = (
        f"{QUEUE_DROP_ERROR_PREFIX} the queue was full ({depth}/{max_depth}) "
        "when a newer delivery arrived and ASSIGNMENT_QUEUE_OVERFLOW_POLICY is "
        "'drop_oldest'. The delivery's payload is preserved on this run."
    )
    oldest.completed_at = datetime.now(UTC)
    await db.flush()
    logger.warning(
        "Assignment '%s' (%s) delivery queue is full (%d/%d) — evicted the "
        "oldest queued delivery (run %s) to admit a newer one "
        "(ASSIGNMENT_QUEUE_OVERFLOW_POLICY=drop_oldest)",
        assignment.name,
        assignment.id,
        depth,
        max_depth,
        oldest.id,
    )
    return str(oldest.id)


async def drain_assignment_queues(limit: int | None = None) -> int:
    """Promote queued deliveries into their assignments' free slots. Returns how many.

    Called on every reconciler cycle from ``AssignmentAdapter.periodic``: the
    enqueue and the end of every assignment run wake the reconciler,
    so a freed slot is refilled within the cycle, and the reconciler's own
    interval recovers every other way a run stops holding a slot (dead-letter,
    force-fail by the stale sweep).

    What is promoted is planned by ``assignment_dispatch.plan_promotions``:
    round-robin across agents, then across each agent's assignments, oldest
    backlog first, one slot at a time, bounded by

    * each assignment's free slots (its cap, halved while a provider answers
      429, minus the runs holding a slot - ``_queue_blocking_statuses``);
    * the executions the reconciler has not claimed yet: at most
      ``assignment_dispatch_max_pending_runs`` (default ``max_concurrent_runs``)
      due runs wait for a worker slot, so a promoted run starts within one
      cycle and a busy assignment cannot fill the pool ahead of the others;
    * ``limit`` (``assignment_queue_drain_batch``) assignments per cycle.

    An assignment with no free slot takes no part in the plan: held by
    an unanswered approval or its runs in flight, it keeps its backlog without
    taking a place from another queue.

    Each assignment is drained in its OWN session so a single bad row cannot
    poison the reconciler's transaction, and ``_drain_one_assignment`` checks
    the slots again under the advisory lock before each promotion.
    """
    # With the webhook queue off, only what integration events queued is
    # promoted: those are never dropped.
    only_integration_events = not settings.assignment_queue_enabled

    batch = limit if limit is not None else int(settings.assignment_queue_drain_batch)
    batch = max(1, batch)

    async with async_session() as db:
        states = await _load_queue_states(db, only_integration_events=only_integration_events)
        if not states:
            return 0
        # While agentic AI is unavailable here (switched off, Community
        # Edition) a promoted delivery could only be refused at run
        # start. Left queued instead, order and bounds intact, and promoted
        # once AI is available again. Asked only when there is something to
        # promote, so an empty queue costs the one query it always did.
        from app.services.agentic_gate import agentic_refusal

        if await agentic_refusal(db) is not None:
            return 0
        waiting = await _unclaimed_assignment_executions(db)

    throttled = await assignment_dispatch.read_throttles([state.assignment_id for state in states])
    states = [
        replace(state, cap=assignment_dispatch.throttled_cap(state.cap))
        if state.assignment_id in throttled
        else state
        for state in states
    ]
    capacity = max(0, _dispatch_pending_cap() - waiting)
    plan = assignment_dispatch.plan_promotions(states, capacity=capacity, max_assignments=batch)

    promoted = 0
    # One wake for the whole drain, not one per promoted run.
    async with batched_wakes():
        for assignment_id, count in plan.items():
            try:
                async with async_session() as db:
                    promoted += await _drain_one_assignment(
                        db,
                        assignment_id,
                        count=count,
                        only_integration_events=only_integration_events,
                        throttled=assignment_id in throttled,
                    )
            except Exception as exc:
                # One assignment's failure must never abort the sweep — the others
                # still have deliveries waiting.
                logger.error(
                    "Failed to drain the delivery queue for assignment %s: %s",
                    assignment_id,
                    format_exception_message(exc),
                    exc_info=True,
                )
    _log_dispatch_cycle(
        states,
        promoted=promoted,
        capacity=capacity,
        waiting=waiting,
        throttled=len(throttled),
    )
    return promoted


def _dispatch_pending_cap() -> int:
    configured = int(settings.assignment_dispatch_max_pending_runs or 0)
    return max(1, configured if configured > 0 else int(settings.max_concurrent_runs))


async def _load_queue_states(
    db: AsyncSession, *, only_integration_events: bool
) -> list[assignment_dispatch.QueueState]:
    """Every assignment with a queued delivery: depth, oldest, slots held, cap.

    ``startable`` counts the queued runs that could start now (the unordered
    ones and the head of each key no run in flight holds), so a queue whose
    keys are all in flight takes no share of the drain's capacity.
    """
    conditions = [
        AssignmentRun.status == QUEUED_RUN_STATUS,
        AssignmentRun.assignment_id.is_not(None),
    ]
    if only_integration_events:
        conditions.append(_integration_event_queued_clause())
    queued_key = AssignmentRun.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string()
    queued = (
        await db.execute(
            select(
                AssignmentRun.assignment_id,
                func.count(AssignmentRun.id),
                func.min(AssignmentRun.created_at),
                func.count(AssignmentRun.id).filter(or_(queued_key.is_(None), queued_key == "")),
            )
            .where(*conditions)
            .group_by(AssignmentRun.assignment_id)
        )
    ).all()
    if not queued:
        return []
    ids = [row[0] for row in queued]
    free_keys: dict[_uuid.UUID, int] = {}
    if any(int(row[1]) > int(row[3] or 0) for row in queued):
        in_flight = aliased(AssignmentRun)
        in_flight_key = in_flight.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string()
        free_keys = {
            row[0]: int(row[1])
            for row in (
                await db.execute(
                    select(AssignmentRun.assignment_id, func.count(func.distinct(queued_key)))
                    .where(
                        *conditions,
                        AssignmentRun.assignment_id.in_(ids),
                        queued_key.is_not(None),
                        queued_key != "",
                        ~select(in_flight.id)
                        .where(
                            in_flight.assignment_id == AssignmentRun.assignment_id,
                            holds_assignment(in_flight, _queue_blocking_statuses()),
                            in_flight_key == queued_key,
                        )
                        .exists(),
                    )
                    .group_by(AssignmentRun.assignment_id)
                )
            ).all()
        }
    hold_on_approval = bool(settings.assignment_queue_hold_on_awaiting_human)
    usage = {
        row[0]: (int(row[1]), hold_on_approval and int(row[2]) > 0, int(row[3] or 0) > 0)
        for row in (
            await db.execute(
                select(
                    AssignmentRun.assignment_id,
                    func.count(AssignmentRun.id),
                    func.count(AssignmentRun.id).filter(AssignmentRun.status == "awaiting_human"),
                    func.count(AssignmentRun.id).filter(_non_delivery_run_clause()),
                )
                .where(
                    AssignmentRun.assignment_id.in_(ids),
                    holds_assignment(AssignmentRun, _queue_blocking_statuses()),
                )
                .group_by(AssignmentRun.assignment_id)
            )
        ).all()
    }
    configs = {
        row[0]: (row[1], row[2])
        for row in (
            await db.execute(
                select(Assignment.id, Assignment.agent_id, Assignment.trigger_config).where(
                    Assignment.id.in_(ids)
                )
            )
        ).all()
    }
    states: list[assignment_dispatch.QueueState] = []
    for assignment_id, depth, oldest_at, unordered in queued:
        agent_id, trigger_config = configs.get(assignment_id, (None, None))
        active, held, exclusive = usage.get(assignment_id, (0, False, False))
        states.append(
            assignment_dispatch.QueueState(
                assignment_id=assignment_id,
                agent_id=agent_id,
                depth=int(depth),
                oldest_at=oldest_at,
                active=active,
                cap=assignment_dispatch.assignment_concurrency_cap(trigger_config),
                held=held,
                exclusive=exclusive,
                startable=int(unordered or 0) + free_keys.get(assignment_id, 0),
            )
        )
    return states


async def _unclaimed_assignment_executions(db: AsyncSession) -> int:
    """Assignment executions that are due and still wait for a worker slot."""
    from app.models.orchestration import OrchestrationExecution

    return int(
        (
            await db.execute(
                select(func.count(OrchestrationExecution.id)).where(
                    OrchestrationExecution.adapter == "assignment",
                    OrchestrationExecution.state == "pending",
                    OrchestrationExecution.run_at <= datetime.now(UTC),
                )
            )
        ).scalar_one()
        or 0
    )


_last_backlog_warning = 0.0


def _log_dispatch_cycle(
    states: list[assignment_dispatch.QueueState],
    *,
    promoted: int,
    capacity: int,
    waiting: int,
    throttled: int,
) -> None:
    """One line per cycle with something queued, and a warning while it lags."""
    global _last_backlog_warning
    now = datetime.now(UTC)
    depth = sum(state.depth for state in states)
    oldest = min(state.oldest_at for state in states)
    oldest_age = max(0.0, (now - _aware(oldest)).total_seconds())
    active = sum(state.active for state in states)
    logger.info(
        "Assignment dispatch: promoted %d, %d queued across %d assignments, "
        "oldest %ds, %d runs holding a slot, %d executions waiting for a "
        "worker (room for %d), %d assignments throttled, %d waiting for a human, "
        "%d waiting for a run that is not a delivery",
        promoted,
        depth - promoted,
        len(states),
        int(oldest_age),
        active,
        waiting,
        capacity,
        throttled,
        sum(1 for state in states if state.held),
        sum(1 for state in states if state.exclusive and not state.held),
    )
    # A queue waiting for a human's answer is not dispatch lag.
    lagging = sorted((state for state in states if not state.held), key=lambda state: state.oldest_at)
    lag_age = max(0.0, (now - _aware(lagging[0].oldest_at)).total_seconds()) if lagging else 0.0
    threshold = int(settings.assignment_queue_backlog_seconds or 0)
    if threshold > 0 and lag_age > threshold and time.monotonic() - _last_backlog_warning >= 60:
        _last_backlog_warning = time.monotonic()
        logger.warning(
            "Assignment dispatch lag: the oldest queued delivery has waited %ds "
            "(threshold %ds); oldest queues: %s",
            int(lag_age),
            threshold,
            ", ".join(
                f"{state.assignment_id} ({state.depth} queued, {state.active}/{state.cap} in flight)"
                for state in lagging[:3]
            ),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# Per assignment, whether its next backlog pick takes the newest delivery.
# Process-local on purpose: losing it on a restart only restarts the alternation.
_backlog_turns: dict[_uuid.UUID, bool] = {}


async def _drain_one_assignment(
    db: AsyncSession,
    assignment_id: _uuid.UUID,
    *,
    count: int = 1,
    only_integration_events: bool = False,
    throttled: bool = False,
) -> int:
    """Promote up to ``count`` queued runs for one assignment while it has free slots.

    ``only_integration_events`` limits it to the runs integration events queued
    (the webhook queue is off). ``throttled`` halves the cap (a provider
    answered 429). Returns how many runs it promoted.

    Every promotion takes the advisory lock the enqueue path takes and checks
    the slots again under it: two workers draining the same assignment can
    never promote past its cap, nor two runs of one ordering key.
    ``_schedule_run_execution`` commits, which releases the lock, so it is
    taken again before the next promotion.
    """
    promoted = 0
    newest_turn = _backlog_turns.get(assignment_id, False)
    backlog = False
    assignment: Assignment | None = None
    for _ in range(max(1, count)):
        await _acquire_trigger_overlap_lock(db, assignment_id)
        assignment = (
            await db.execute(select(Assignment).where(Assignment.id == assignment_id))
        ).scalar_one_or_none()
        agent = (
            None
            if assignment is None
            else (
                await db.execute(select(Agent).where(Agent.id == assignment.agent_id))
            ).scalar_one_or_none()
        )

        # The assignment / agent may have been deleted or disabled while the
        # deliveries waited. Abandon them with an explicit reason instead of
        # leaving them queued forever — a queued row nothing will ever promote
        # is the same silent loss the queue was built to eliminate.
        if assignment is None or not assignment.enabled or agent is None or not agent.enabled:
            abandoned = await _abandon_queued_runs(
                db,
                assignment_id,
                f"{QUEUE_DROP_ERROR_PREFIX} the assignment or its agent was "
                "deleted or disabled while this delivery was waiting in the queue.",
                only_integration_events=only_integration_events,
            )
            await db.commit()
            if abandoned:
                logger.warning(
                    "Abandoned %d queued deliveries: assignment %s or its agent is "
                    "gone or disabled",
                    abandoned,
                    assignment_id,
                )
            break

        # Accepted before the owner's account was deactivated: ended
        # with the reason rather than promoted into runs the engine would skip.
        if await is_paused_by_deactivated_owner(db, assignment):
            abandoned = await _abandon_queued_runs(
                db,
                assignment_id,
                OWNER_DEACTIVATED_RUN_ERROR,
                only_integration_events=only_integration_events,
            )
            await db.commit()
            if abandoned:
                logger.info(
                    "Abandoned %d queued deliveries: the owner of assignment %s is "
                    "deactivated",
                    abandoned,
                    assignment_id,
                )
            break

        cap = assignment_dispatch.assignment_concurrency_cap(assignment.trigger_config)
        if throttled:
            cap = assignment_dispatch.throttled_cap(cap)
        usage = await _slot_usage(db, assignment_id)
        if not usage.has_room(cap):
            await db.commit()
            break
        candidates = await _queued_candidates(
            db, assignment_id, only_integration_events=only_integration_events
        )
        if not candidates:
            await db.commit()
            break
        backlog = _in_backlog(candidates[0].created_at)
        chosen, newest_turn = assignment_dispatch.select_runs(
            candidates,
            1,
            busy_keys=set(usage.busy_keys),
            backlog=backlog,
            newest_turn=newest_turn,
        )
        if not chosen:
            # Every queued run waits for an ordering key a run in flight holds.
            await db.commit()
            break
        run = (
            await db.execute(select(AssignmentRun).where(AssignmentRun.id == chosen[0].run_id))
        ).scalar_one()
        await _schedule_run_execution(
            db,
            assignment,
            agent,
            run,
            trigger_context=run.trigger_context,
        )
        promoted += 1

    if backlog:
        _backlog_turns[assignment_id] = newest_turn
    else:
        _backlog_turns.pop(assignment_id, None)
    if promoted and assignment is not None:
        logger.info(
            "Promoted %d queued deliveries for assignment '%s' (%s)%s - %d still queued",
            promoted,
            assignment.name,
            assignment_id,
            " (backlog: oldest and newest interleaved)" if backlog else "",
            await count_queued_runs(db, assignment_id),
        )
    return promoted


# The oldest queued deliveries the drain chooses from. Queued past the queue
# cap only by integration events with no sender to retry them, so a
# window this size always holds the next runs of every ordering key.
_CANDIDATE_WINDOW = 500


async def _queued_candidates(
    db: AsyncSession, assignment_id: _uuid.UUID, *, only_integration_events: bool
) -> list[assignment_dispatch.QueuedRun]:
    conditions = [
        AssignmentRun.assignment_id == assignment_id,
        AssignmentRun.status == QUEUED_RUN_STATUS,
    ]
    if only_integration_events:
        conditions.append(_integration_event_queued_clause())
    rows = (
        await db.execute(
            select(
                AssignmentRun.id,
                AssignmentRun.created_at,
                AssignmentRun.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT].as_string(),
            )
            .where(*conditions)
            .order_by(AssignmentRun.created_at, AssignmentRun.id)
            .limit(_CANDIDATE_WINDOW)
        )
    ).all()
    return [
        assignment_dispatch.QueuedRun(run_id=row[0], created_at=row[1], ordering_key=row[2] or None)
        for row in rows
    ]


def _in_backlog(oldest_created_at: datetime | None) -> bool:
    threshold = int(settings.assignment_queue_backlog_seconds or 0)
    if threshold <= 0 or oldest_created_at is None:
        return False
    return (datetime.now(UTC) - _aware(oldest_created_at)).total_seconds() > threshold


async def _abandon_queued_runs(
    db: AsyncSession,
    assignment_id: _uuid.UUID,
    error: str,
    *,
    only_integration_events: bool,
) -> int:
    conditions = [
        AssignmentRun.assignment_id == assignment_id,
        AssignmentRun.status == QUEUED_RUN_STATUS,
    ]
    if only_integration_events:
        conditions.append(_integration_event_queued_clause())
    runs = (await db.execute(select(AssignmentRun).where(*conditions))).scalars().all()
    now = datetime.now(UTC)
    for run in runs:
        run.status = "skipped"
        run.error = error
        run.completed_at = now
    return len(runs)


async def resume_assignment_run(db: AsyncSession, run: AssignmentRun) -> None:
    """Resume a HITL-paused assignment run via the orchestration engine.

    The run's dispatch execution is parked in ``running`` / ``awaiting_human``
    (an AWAIT) after the human-input request. Emitting a ``resume`` outbox event
    re-arms it for re-claim, so ``execute_assignment_run`` runs again under a
    fresh lease and resumes from the pause. If no live paused execution exists
    (e.g. a run created before this engine landed), a fresh execution is created
    instead. The caller must have already persisted the human response on the
    run. Commits and nudges the reconciler.

    Every answer to a pause comes through here, so this is where the open
    action requests on the run close: once answered, nothing is left
    for them to ask for.
    """
    from app.services.access_requests import close_run_action_requests
    from app.services.orchestration_engine import (
        emit_outbox_event,
        get_execution_by_dedup_key,
    )

    await close_run_action_requests(db, run.id)

    dedup_key = f"assignment_run:{run.id}"
    execution = await get_execution_by_dedup_key(db, dedup_key)
    if execution is not None and execution.state == "running":
        # Key the resume event on the pause instant (the execution's
        # ``updated_at``, set when ``await_event`` parked it), NOT on
        # ``now()``. The outbox dedups on ``(execution_id, event_type,
        # dedup_key)``, so a pause-stable key collapses repeated respond
        # clicks within the SAME pause onto one event (no churn) while a
        # later HITL round — which re-parks the execution with a fresh
        # ``updated_at`` — still gets a distinct, non-conflicting key. A
        # fully-constant key would instead be swallowed by ON CONFLICT
        # against the prior round's already-processed event, silently
        # dropping the second resume.
        pause_ms = int(execution.updated_at.timestamp() * 1000)
        await emit_outbox_event(
            db,
            execution_id=execution.id,
            event_type="resume",
            dedup_key=f"resume:{run.id}:{pause_ms}",
            payload={"run_id": str(run.id)},
        )
    else:
        # No live paused execution — dispatch a fresh one (no hard deadline:
        # a resumed HITL run is human-paced like the original wait). Reuse the
        # run's dedup_key so repeated respond calls are idempotent: the
        # ON CONFLICT in create_execution collapses retries onto the one row.
        await create_execution(
            db,
            adapter="assignment",
            payload={"run_id": str(run.id)},
            dedup_key=dedup_key,
            max_attempts=assignment_run_resume.execution_max_attempts(),
        )
    await db.commit()
    await notify_orchestration_wake()
