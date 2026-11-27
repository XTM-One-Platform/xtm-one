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

"""Autonomous scheduler – runs assignment triggers on schedule.

This background loop (called from main.py every 60s) handles trigger detection
and platform housekeeping:
  1. Schedule-based assignments: checks cron expressions, triggers overdue ones
  2. Integration event polling: checks for new events from integrations
  3. Knowledge base sync scheduling
  4. Memory maintenance: periodic pruning, cap enforcement, and fact consolidation
  5. Skill auto-refresh: re-fetches URL-based skills every 24 hours
  6. Scheduled evaluations

Durable execution, scheduling (delays), leasing, deadlines, retries, and crash
recovery for assignment runs / evaluation runs / sub-agent tasks are owned by
the orchestration engine + reconciler (``orchestration_engine.py`` /
``orchestration_reconciler.py``, running in the worker). There is no longer any
``delayed_run:*`` Redis poll or startup recovery loop in this module —
triggering simply creates an orchestration execution.

The one exception is ``_cleanup_stale_assignment_runs``: a defensive sweep for
runs the reconciler *cannot* reach because they have no live orchestration
execution (in-process chat tracing runs, legacy pre-orchestration runs, and
terminal-state sync-hook desyncs). It only ever finalizes runs the engine is
not (or no longer) managing — desyncs are reconciled to the execution's actual
outcome (a ``completed`` execution completes the run, every other terminal
state fails it) — so it never races the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from croniter import croniter
from croniter.croniter import CroniterBadCronError
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstanceState

from app.core.config import settings
from app.core.database import async_session
from app.core.egress import egress_client, egress_route
from app.core.metrics import get_or_create_counter
from app.core.utils import (
    chat_processing_key,
    format_exception_message,
    next_cron_run,
    safe_uuid,
    schedule_base,
    schedule_timezone,
    text_column_error,
)
from app.llm.provider_access import acting_for
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.knowledge_base import KBExportPackage
from app.services import wall_clock_schedule
from app.services.assignment_engine import has_active_run, trigger_assignment
from app.services.assignment_pause import (
    AssignmentOwnerDeactivatedError,
    owner_not_deactivated,
    pause_assignment,
)
from app.services.work_item_dispatch_wake import observe_lag as observe_dispatch_lag

logger = logging.getLogger(__name__)

# ── Prometheus counters: stale / orphaned run observability ───────────────────
# Increment these whenever the stale-run sweep detects or finalises orphaned
# runs so Grafana / Alertmanager can fire on recurring spikes without requiring
# manual log review.
#
#   xtmone_stale_run_warnings_total{orphan_class}
#       Incremented each time an early WARNING is emitted for a run approaching
#       its stale deadline.  Labels:
#         • no_execution  — run has no orchestration execution at all (Pattern A:
#                           crashed/restarted in-process or legacy run)
#
#   xtmone_stale_runs_finalized_total{outcome, orphan_class}
#       Incremented each time the sweep force-finalises a stale/orphaned run.
#       Labels:
#         • outcome        — "failed" | "completed"
#         • orphan_class   — "no_execution" (Pattern A) |
#                            "terminal_desync" (Pattern B: terminal execution,
#                            run never finalized by the sync hook)
#
# Registration is idempotent and per-process — see ``app.core.metrics``.
# With multiple web workers the Redis tick lock rotates which worker
# increments, so treat these series as spike/trend signals, not exact totals;
# the WARNING/INFO logs remain the authoritative per-run record.
_STALE_RUN_WARNINGS = get_or_create_counter(
    "xtmone_stale_run_warnings_total",
    "Orphaned assignment runs approaching the stale force-fail deadline",
    ["orphan_class"],
)
_STALE_RUNS_FINALIZED = get_or_create_counter(
    "xtmone_stale_runs_finalized_total",
    "Orphaned/stale assignment runs force-finalized by the stale-run sweep",
    ["outcome", "orphan_class"],
)

#   xtmone_skill_refresh_total{outcome, auth}
#       Incremented once per skill per auto-refresh attempt so operators can
#       tell "the credential expired" apart from "the URL is broken" apart from
#       "GitHub had an outage" without reading logs.
#       Labels:
#         • outcome — "success" | "auth" (credential rejected / under-scoped) |
#                     "not_found" | "client" (other permanent 4xx) |
#                     "rate_limit" (403-with-quota-spent / 429 / 408) |
#                     "transient" (5xx, network error, unexpected failure)
#         • auth    — "authenticated" (bound GitHub credential) | "anonymous"
#       Same per-process caveat as the counters above: treat as a spike/trend
#       signal, not an exact total.
_SKILL_REFRESH_OUTCOMES = get_or_create_counter(
    "xtmone_skill_refresh_total",
    "URL-based skill auto-refresh attempts by outcome and authentication mode",
    ["outcome", "auth"],
)

# ── Hourly jobs: a wall-clock schedule shared by every process (#768) ─────────
# The hourly maintenance block, the skill auto-refresh check, the AI adoption
# sync and the model prices git sync run when their last COMPLETED pass, kept
# in Redis, is older than their interval, under a cluster-wide lock
# (``services/wall_clock_schedule.py``). They used to count ticks in a module
# global that restarted at 0 on every boot: a platform redeploying more often
# than hourly never reached 60 ticks and never ran them. Individual skills are
# refreshed when their fetched_at is older than SKILL_REFRESH_MAX_AGE_SECONDS.
SKILL_REFRESH_MAX_AGE_SECONDS = 86400  # 24 hours

# ── API key stats flush scheduling ────────────────────────────────────────────
# Flush Redis-buffered API key usage stats to PostgreSQL every 5 ticks (5 min).
_APIKEY_STATS_FLUSH_INTERVAL = 5  # ticks (5 × 60s = 5 minutes)
_apikey_stats_tick_counter = 0

# ── Quota usage flush scheduling ─────────────────────────────────────────────
# Flush Redis-buffered quota counters to PostgreSQL every 5 ticks (5 min).
_QUOTA_FLUSH_INTERVAL = 5  # ticks (5 × 60s = 5 minutes)
_quota_flush_tick_counter = 0


# ── CTEM metrics: no scheduling. Collected live, per-user, on dashboard load
# (each request mints the acting user's JWT so OpenCTI/OpenAEV return only what
# that user can see). See services/ctem_metrics_collector.build_posture_for_user.

# ── Trace storage maintenance scheduling ─────────────────────────────────────
# Both counters are SEEDED AT their interval so the first tick after every
# startup runs the check. The daily-stats aggregation must never depend on an
# in-memory counter surviving long enough to fire: a frequently-restarting
# replica (deploys, autoscaling, OOM) would otherwise keep resetting the counter
# to 0, never reach the threshold, and never aggregate - so last_aggregation_at
# stays NULL and the "stats may be incomplete" banner sticks forever. Steady
# state: purge ~hourly, daily-stats backfill ~every 30 min. The backfill itself
# is gated by the persistent llm_daily_stats table, so the extra checks after a
# restart are cheap no-ops once it has caught up.
_TRACE_PURGE_INTERVAL = 60
_trace_purge_tick_counter = _TRACE_PURGE_INTERVAL
_DAILY_STATS_INTERVAL = 30
_daily_stats_tick_counter = _DAILY_STATS_INTERVAL

# ── Teams private-channel subscription maintenance ──────────────────────────
# Hourly, and SEEDED AT its interval so the first tick a process wins after a
# restart runs it: a channel whose subscription must be recreated (its record
# lost, or unreadable after a SECRET_KEY change) is repaired within minutes
# instead of after the first hour - later still with several web processes,
# each counting only the ticks it wins. Each channel is maintained by one
# worker at a time (the channel lock in ``app.channels.teams``), so passes run
# by several processes after a restart never overlap on a channel.
_TEAMS_SUBSCRIPTION_INTERVAL = 60
_teams_subscription_tick_counter = _TEAMS_SUBSCRIPTION_INTERVAL

# ── Blocked references polling fallback ─────────────────────────────
# Starts at 0: the first tick after a restart reads the references of the items
# no delivery released, then every ``work_item_blocked_refs.POLL_EVERY_TICKS``.
_blocked_refs_poll_tick_counter = 0


# ── Cron warning rate limiting ────────────────────────────────────────────────
# A misconfigured cron expression would otherwise log a WARNING every
# scheduler tick (~30–60 s) indefinitely. Since save-time validation lives
# in ``core/utils.validate_cron_expression`` and is wired into every save
# path (POST/PUT API, agent builder, agent import), the only way an invalid
# cron now ends up here is a *legacy* row created before validation landed —
# the user needs to fix it in the UI, but re-logging on every tick (or even
# every hour) only buries the rest of the log stream. We log once per
# (kind, id) per process lifetime and silently skip after that.
#
# In a multi-worker deployment each uvicorn worker has its own
# ``_cron_warned_once`` set, so the warning will fire up to N times across
# the fleet on each pod start — still bounded, no longer a flood.
_cron_warned_once: set[tuple[str, str]] = set()


def _should_warn_cron(kind: str, id_: object) -> bool:
    """Return True the *first* time a cron-error WARNING should be emitted.

    Bounded by the (small) set of evaluations / assignments that currently
    have an invalid expression, so unbounded growth is not a concern.
    """
    key = (kind, str(id_))
    if key in _cron_warned_once:
        return False
    _cron_warned_once.add(key)
    return True


# The last refusal logged per evaluation whose scheduled launch is refused:
# the check runs every tick until the cause is fixed, the warning once
# per cause. Kept to the evaluations refused at the last tick, so one launched,
# paused, deleted or unscheduled since is forgotten; one skipped for a tick (a
# run in progress) is logged again, once. Per process: a tick held by another
# process logs it again, once.
_scheduled_launch_refusals: dict[object, str] = {}


def _warn_scheduled_launch_refused(evaluation_id: object, name: str, detail: str) -> None:
    """Log why a scheduled evaluation creates no run, once per cause."""
    if _scheduled_launch_refusals.get(evaluation_id) == detail:
        return
    _scheduled_launch_refusals[evaluation_id] = detail
    logger.warning(
        "Scheduled evaluation %s ('%s') creates no run: %s", evaluation_id, name, detail
    )


async def _record_skipped_fire(
    db: AsyncSession,
    assignment: Assignment,
    cron_expr: object,
    now: datetime,
) -> str:
    """Record the due slots the overlap guard dropped as ``skipped`` runs (#2582).

    One run per slot after the schedule base, stamped on it and naming the
    active run in the way and since when (one summary run past
    ``schedule_overlap.MISSED_RECORD_CAP``); ``last_triggered_at`` moves to the
    latest slot in the caller's commit, with the rows. Answers ``"recorded"``;
    ``"vanished"`` when the active run ended since ``has_active_run`` read it
    (the caller fires as usual, nothing is written); ``"failed"`` when the rows
    could not be built (logged; the caller rolls back and the base stays, so
    the next tick records the slots).
    """
    from app.services import schedule_overlap
    from app.services.assignment_engine import find_active_run

    try:
        active = await find_active_run(db, assignment.id)
        if active is None:
            return "vanished"
        zone, _ = schedule_timezone(assignment.trigger_config)
        served, slots, truncated = schedule_overlap.due_slots(str(cron_expr), schedule_base(assignment), now, tz=zone)
        runs = schedule_overlap.skipped_fire_runs(
            assignment, active, cron=str(cron_expr), now=now, slots=slots, truncated=truncated
        )
        for fields in runs:
            db.add(AssignmentRun(**fields))
        assignment.last_triggered_at = served if served is not None and served <= now else now
        logger.info(
            "Scheduled assignment '%s' (%s): fire skipped, %s",
            assignment.name,
            assignment.id,
            runs[-1]["result_summary"] if len(runs) == 1 else f"{len(runs)} windows recorded skipped",
        )
        return "recorded"
    except Exception as exc:
        logger.warning(
            "Scheduled assignment '%s' (%s): skipped fire not recorded: %s",
            assignment.name,
            assignment.id,
            format_exception_message(exc),
        )
        return "failed"


async def _record_lost_fire(
    assignment: Assignment, cron_expr: object, slot: datetime, now: datetime, *, reason: str
) -> None:
    """Record *slot* as a ``skipped`` run when its fire's run was never created (#2582).

    The schedule base was committed past *slot* before ``trigger_assignment``;
    a creation that did not complete (the tick was stopped, a non-refusal
    error) would otherwise leave the slot consumed with no row. Written in a
    session of its own, shielded from a second cancellation, and only when no
    run of the assignment was created since the tick read *now* (a creation
    that committed before the stop needs no record). Never raises.
    """
    from app.services import schedule_overlap

    async def _write() -> None:
        async with async_session() as own:
            landed = (
                await own.execute(
                    select(func.count())
                    .select_from(AssignmentRun)
                    .where(AssignmentRun.assignment_id == assignment.id, AssignmentRun.created_at >= now)
                )
            ).scalar()
            if landed:
                return
            own.add(
                AssignmentRun(
                    **schedule_overlap.lost_fire_run(assignment, slot, cron=str(cron_expr), now=now, reason=reason)
                )
            )
            await own.commit()
        logger.warning(
            "Scheduled assignment '%s' (%s): fire of %s lost, recorded skipped (%s)",
            assignment.name,
            assignment.id,
            slot.isoformat(),
            reason,
        )

    try:
        await asyncio.shield(_write())
    except BaseException as exc:  # never masks the error being handled
        logger.warning(
            "Scheduled assignment '%s' (%s): lost fire of %s not recorded: %s",
            assignment.name,
            assignment.id,
            slot.isoformat(),
            format_exception_message(exc) if isinstance(exc, Exception) else type(exc).__name__,
        )


#: Why the last scheduler pass of this process may have missed windows: its
#: sweep failed, or its tick overran (#2562). Read by the next sweep into the
#: summary of the windows it records missed; per process, so a tick run by
#: another replica records them without a reason.
_schedule_trouble: str | None = None


async def _record_missed_windows(
    db: AsyncSession,
    assignment: Assignment,
    cron_expr: object,
    missed: list[datetime],
    *,
    truncated: bool,
    now: datetime,
) -> None:
    """Record the windows no sweep reached before *assignment*'s late fire as ``skipped`` runs (#2562).

    One run per window up to ``schedule_overlap.MISSED_RECORD_CAP``, else one
    summary run. Committed; best effort: a failure is logged and rolled back,
    never undoing the fire already created.
    """
    from app.services import schedule_overlap

    try:
        runs = schedule_overlap.missed_window_runs(
            assignment, missed, cron=str(cron_expr), now=now, truncated=truncated, reason=_schedule_trouble
        )
        for fields in runs:
            db.add(AssignmentRun(**fields))
        await db.commit()
        logger.warning(
            "Scheduled assignment '%s' (%s): %s",
            assignment.name,
            assignment.id,
            runs[-1]["result_summary"] if len(runs) == 1 else f"{len(runs)} windows missed",
        )
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "Scheduled assignment '%s' (%s): missed windows not recorded: %s",
            assignment.name,
            assignment.id,
            format_exception_message(exc),
        )


async def _supersede_stale_fires(db: AsyncSession, assignment: Assignment, now: datetime) -> bool:
    """Supersede the earlier fires of *assignment* still waiting for a worker (#2040); whether none is left active.

    Every active run of the assignment must be a scheduled fire that never
    started (:func:`schedule_overlap.supersede_never_started`): a run that
    started, an event or manual run, or one a worker claimed meanwhile keeps
    its place and the new fire is skipped as before. Committed when it
    supersedes; best effort, a failure is logged and answers ``False``.
    """
    from app.services import schedule_overlap
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES
    from app.services.decision_waits import holds_assignment

    try:
        active = list(
            (
                await db.execute(
                    select(AssignmentRun).where(
                        AssignmentRun.assignment_id == assignment.id,
                        holds_assignment(AssignmentRun, ACTIVE_RUN_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not active or any(
            (getattr(run, "trigger_context", None) or {}).get("type") != "schedule"
            or getattr(run, "status", None) not in schedule_overlap.SUPERSEDABLE_STATUSES
            or getattr(run, "started_at", None) is not None
            for run in active
        ):
            return False
        for run in active:
            if not await schedule_overlap.supersede_never_started(db, run, now=now):
                await db.rollback()
                return False
        await db.commit()
    except Exception as exc:
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.warning(
            "Scheduled assignment '%s' (%s): stale fire not superseded: %s",
            assignment.name,
            assignment.id,
            format_exception_message(exc),
        )
        return False
    for run in active:
        logger.info(
            "Scheduled assignment '%s' (%s): fire %s never started and is superseded by the fire of %s",
            assignment.name,
            assignment.id,
            run.id,
            now.isoformat(),
        )
    return True


async def _flag_assignment_cron_misconfiguration(
    db: AsyncSession,
    assignment: Assignment,
    cron_expr: object,
    cron_err: Exception,
    *,
    now: datetime | None = None,
) -> None:
    """Disable a misconfigured scheduled assignment and persist a failed run.

    The failed run mirrors the shape of a normal schedule-trigger run —
    ``trigger_type``/``scheduled_at`` in the payload and
    ``type``/``cron``/``scheduled_at``/``triggered_at`` in the context — plus a
    ``misconfigured`` flag, so the run-detail UI renders it with the same tiles
    as any other scheduled run.  The cron value is stringified: it comes
    straight from user config and may not be a plain string, so coercing it
    here keeps the JSON column serialisable and the UI consistent.
    """
    now = now or datetime.now(UTC)
    cron_str = str(cron_expr)
    timestamp = now.isoformat()
    error_message = f"Invalid cron expression {cron_str!r}: {cron_err}"
    # Pause provenance: the failed run below explains this to whoever
    # opens the run history, but the assignment page only ever showed a bare
    # "Paused" badge. Stamp the reason on the assignment so the badge explains
    # itself.
    pause_assignment(
        assignment,
        reason="invalid_schedule",
        details=(
            f"Auto-paused by the scheduler: {error_message}. "
            "Fix the schedule, then re-enable this assignment."
        ),
        actor_user_id=None,
        actor_label="XTM One — scheduler",
        now=now,
    )
    run = AssignmentRun(
        assignment_id=assignment.id,
        agent_id=assignment.agent_id,
        user_id=assignment.run_as_user_id or assignment.user_id,
        is_company_managed=assignment.is_company_managed,
        trigger_payload={"trigger_type": "schedule", "scheduled_at": timestamp},
        trigger_context={
            "type": "schedule",
            "cron": cron_str,
            "scheduled_at": timestamp,
            "triggered_at": timestamp,
            "misconfigured": True,
        },
        status="failed",
        result_summary="Assignment disabled due to invalid schedule configuration.",
        error=error_message,
        started_at=now,
        completed_at=now,
    )
    db.add(run)
    try:
        await db.commit()
    except Exception:
        # A failed commit leaves the shared session in a failed-transaction
        # state; the scheduler reuses this same session for the remaining
        # assignments in the tick, so roll back to keep it usable, then
        # re-raise.  The caller's per-assignment ``except Exception`` logs the
        # failure, and because the disable never committed the assignment stays
        # ``enabled`` and is retried on the next tick.
        await db.rollback()
        raise


__all__ = [
    "_apikey_stats_tick_counter",
    "_blocked_refs_poll_tick_counter",
    "_daily_stats_tick_counter",
    "_quota_flush_tick_counter",
    "_teams_subscription_tick_counter",
    "_trace_purge_tick_counter",
]


# Assignment-run statuses considered "active" (non-terminal). ``awaiting_human``
# is deliberately excluded — a HITL pause is an intentional indefinite wait, not
# a stuck run, and must never be force-failed by the stale sweep.
STALE_ASSIGNMENT_ACTIVE_STATUSES = ("running", "pending", "delayed")
STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX = (
    "Run marked as failed by stale-assignment-run cleanup."
)
# Result placeholder for a desync run reconciled to ``completed``: the
# execution row records no result payload (only ``last_error``), and the
# run's own finalize write — the one that would have persisted
# ``result_summary`` — is exactly what was lost in the desync, so there is
# nothing to copy over. An explicit note beats a blank Result card.
RECONCILED_COMPLETED_RESULT_SUMMARY = (
    "Run completed successfully, but the detailed result was not recorded: "
    "the run was finalized by the platform's reconciliation sweep after the "
    "engine's terminal-state sync was interrupted."
)


async def _cleanup_stale_assignment_runs(db: AsyncSession) -> int:
    """Finalize assignment runs orphaned in an active status with no live execution.

    Safety net for runs the durable orchestration engine is NOT (or no longer)
    managing. The reconciler can only recover runs that still have a
    *non-terminal* ``orchestration_executions`` row (lease reclaim + deadline
    sweep). Three orphan classes slip through that net and would otherwise stay
    ``running`` / ``pending`` forever:

      1. **In-process tracing runs** — chat-handover parents and consult /
         transfer children created in ``agent_chat`` with ``assignment_id IS
         NULL`` and *no* orchestration execution. A backend/worker restart
         mid-chat strands them.
      2. **Legacy runs** — created before the orchestration engine existed, so
         no execution row was ever created for them.
      3. **Desync** — the execution reached a terminal state but its terminal-
         state sync hook failed to flip the run (the reconciler logs+swallows
         that failure). When the execution ``completed`` successfully the run is
         reconciled to ``completed``; every other terminal execution state
         (``failed``, ``dead_letter``, ``cancelled``) maps to ``failed``.

    Runs whose execution is still ``pending`` / ``running`` are LEFT ALONE — the
    worker reconciler owns their recovery and we must never race it. HITL
    ``awaiting_human`` runs are never touched. Background-delegation child runs
    are engine-managed under the ``sub_agent_task:<task_id>`` dedup key (not
    ``assignment_run:<run_id>``), so their liveness is resolved through the
    ``sub_agent`` execution payload (``payload->>'assignment_run_id'``) — a
    live delegation is skipped exactly like a live ``assignment_run:*``
    execution. In-flight ``agent_chat`` turns are detected via the
    ``chat:processing:{conv_id}`` Redis marker (300s TTL, refreshed by the
    turn holding it) extracted from ``trigger_payload``; runs backed
    by a live turn are skipped the same way. External A2A runs
    (``trigger_context.source == "a2a"``) are skipped on the same contract while
    they hold a fresh ``a2a_tasks.lease_heartbeat_at`` lease — their own reaper
    (``a2a.tasks.reap_stale_tasks``) owns them, and force-failing a heartbeating
    run loses its result to the executor's ``status == 'running'`` finalize
    guard. A stale or missing lease falls through, so this sweep stays
    the backstop for an A2A run whose task row is gone.

    Orphaned runs that have not yet crossed the stale deadline but have been
    active longer than ``stale_assignment_warn_seconds`` emit an early WARNING
    so operators can detect them before the run is force-failed.

    Returns the number of runs finalized (completed or failed).
    """
    from app.models.orchestration import (
        TERMINAL_EXECUTION_STATES,
        OrchestrationExecution,
    )
    from app.services.ai_hold import HOLD_MARKER_KEY
    from app.services.orchestration_adapters.assignment import execution_dedup_key

    now = datetime.now(UTC)
    cleaned = 0
    stale_batch = 500

    # Oldest active runs first: the stale ones we care about sort to the front,
    # so the batch cap never starves them behind a flood of fresh runs. A run
    # held while agentic AI is unavailable stays active and old for as
    # long as the hold lasts; while its execution is live the loop below skips
    # it anyway, so it is left out here rather than taking a slot of the batch.
    live_execution = (
        select(OrchestrationExecution.id)
        .where(
            OrchestrationExecution.state.in_(("pending", "running")),
            or_(
                OrchestrationExecution.dedup_key
                == func.concat("assignment_run:", cast(AssignmentRun.id, String)),
                and_(
                    OrchestrationExecution.adapter == "sub_agent",
                    OrchestrationExecution.payload["assignment_run_id"].astext
                    == cast(AssignmentRun.id, String),
                ),
            ),
        )
        .exists()
    )
    candidates = (
        (
            await db.execute(
                select(AssignmentRun)
                .where(
                    AssignmentRun.status.in_(STALE_ASSIGNMENT_ACTIVE_STATUSES),
                    or_(
                        AssignmentRun.trigger_context[HOLD_MARKER_KEY].is_(None),
                        ~live_execution,
                    ),
                )
                .order_by(AssignmentRun.created_at)
                .limit(stale_batch)
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return 0

    # Resolve each run's orchestration execution state via the canonical dedup
    # key ``assignment_run:<run_id>`` in a single batched query.
    dedup_keys = [execution_dedup_key(run.id) for run in candidates]
    exec_rows = (
        await db.execute(
            select(
                OrchestrationExecution.dedup_key,
                OrchestrationExecution.state,
            ).where(OrchestrationExecution.dedup_key.in_(dedup_keys))
        )
    ).all()
    exec_state_by_key = {row[0]: row[1] for row in exec_rows}

    # Background-delegation child runs (created by ``create_sub_agent_task``
    # with ``assignment_id IS NULL``) are engine-managed under a DIFFERENT
    # dedup key — ``sub_agent_task:<task_id>`` — so the probe above finds no
    # execution for them and would misclassify a live delegation as an orphan
    # (repeating "orphaned by a restart or crash" warnings from warn_seconds,
    # then a force-fail at the stale deadline that kills a legitimately
    # running worker mid-flight). Resolve their liveness through
    # the execution payload: a non-terminal ``sub_agent`` execution whose
    # ``payload->>'assignment_run_id'`` matches the run means the engine owns
    # it. Dead sub-agent executions need no handling here —
    # ``recover_orphaned_sub_agent_tasks`` (step 5c-1, which runs before this
    # sweep on the same tick) already reconciles those onto their linked runs.
    probeless_run_ids = [
        str(run.id)
        for run in candidates
        if execution_dedup_key(run.id) not in exec_state_by_key
    ]
    live_delegation_run_ids: set[str] = set()
    if probeless_run_ids:
        delegation_rows = (
            await db.execute(
                select(
                    OrchestrationExecution.payload["assignment_run_id"].astext
                ).where(
                    OrchestrationExecution.adapter == "sub_agent",
                    OrchestrationExecution.state.in_(("pending", "running")),
                    OrchestrationExecution.payload["assignment_run_id"].astext.in_(
                        probeless_run_ids
                    ),
                )
            )
        ).all()
        live_delegation_run_ids = {row[0] for row in delegation_rows if row[0]}

    # In-flight ``agent_chat`` turns are tracked via the
    # ``chat:processing:{conv_id}`` Redis marker (300s TTL, refreshed every
    # 20s by the web chat turn's marker refresh in ``chat.py``, every
    # ``REST_REFRESH_INTERVAL_SECONDS`` by a REST turn).  Runs created by
    # ``agent_chat`` for handover / consult / transfer scenarios store the
    # conversation_id in ``trigger_payload`` but carry no orchestration
    # execution, so the DB probes above cannot distinguish them from genuine
    # orphans.  The result is the same flood of false "orphaned by a restart
    # or crash" warnings and eventual force-fails of legitimately running
    # long-duration turns that the delegation-run probe already avoids.
    #
    # Batch-probe Redis for all remaining probeless runs that carry a
    # ``trigger_payload.conversation_id`` field; skip any whose conversation
    # still has a live processing marker, exactly as we skip live delegation
    # runs above.  On Redis error (transient outage / cold start) conservatively
    # treat all probed runs as live — spurious warns are far preferable to
    # force-failing a turn mid-flight.  A Redis restart/flush that wipes live
    # markers is self-healing: the chat keepalive re-sets each marker within
    # ~20s, so at most one 60s sweep tick can race that window (accepted risk).
    live_chat_turn_run_ids: set[str] = set()
    probeless_ids = set(probeless_run_ids)
    still_probeless = [
        run
        for run in candidates
        if (
            str(run.id) in probeless_ids
            and str(run.id) not in live_delegation_run_ids
        )
    ]
    if still_probeless:
        conv_id_to_run_ids: dict[str, list[str]] = {}
        for run in still_probeless:
            payload = run.trigger_payload
            conv_id = payload.get("conversation_id") if isinstance(payload, dict) else None
            if conv_id:
                conv_id_to_run_ids.setdefault(str(conv_id), []).append(str(run.id))
        if conv_id_to_run_ids:
            try:
                from app.core.redis import redis_client as _redis_client

                _r = await _redis_client()
                conv_ids = list(conv_id_to_run_ids)
                chat_values = await _r.mget(
                    *(chat_processing_key(cid) for cid in conv_ids)
                )
                for cid, val in zip(conv_ids, chat_values, strict=True):
                    if val is not None:
                        live_chat_turn_run_ids.update(conv_id_to_run_ids[cid])
            except Exception:
                # Redis unavailable — conservatively skip these runs rather
                # than risk force-failing a live agent_chat turn.
                logger.warning(
                    "Failed to probe Redis for live agent_chat turn markers "
                    "(%d conversation(s)); skipping stale check for the "
                    "associated %d run(s) this tick to avoid force-failing "
                    "legitimately running turns",
                    len(conv_id_to_run_ids),
                    sum(len(v) for v in conv_id_to_run_ids.values()),
                    exc_info=True,
                )
                for run_ids in conv_id_to_run_ids.values():
                    live_chat_turn_run_ids.update(run_ids)

    # External A2A runs (``a2a.tasks.create_task``) are the third class that
    # looks identical to an orphan in the DB: ``assignment_id IS NULL``, no
    # orchestration execution, and no ``conversation_id`` for the Redis probe
    # above to find.  They are NOT orphans — ``a2a.tasks.reap_stale_tasks``
    # owns them and, unlike anything else this sweep sees, they carry a real
    # per-run heartbeat (``a2a_tasks.lease_heartbeat_at``, refreshed every
    # quarter-lease by ``_supervise_run``).  Force-failing one mid-flight
    # discards its real result: the executor's finalize is conditional on the
    # run still being ``running``.  Same liveness contract as the
    # delegation and chat turn probes above.
    #
    # Gated on a FRESH lease rather than on being an A2A run at all: if the task
    # row is gone (its exposure cascade-deleted with the agent) or the lease has
    # genuinely expired without its own reaper clearing it, this sweep stays the
    # backstop.
    live_a2a_run_ids: set[str] = set()
    a2a_candidate_ids = [
        run.id
        for run in still_probeless
        if str(run.id) not in live_chat_turn_run_ids
        and isinstance(run.trigger_context, dict)
        and run.trigger_context.get("source") == "a2a"
    ]
    if a2a_candidate_ids:
        from app.models.a2a import A2ATask

        lease_cutoff = now - timedelta(seconds=settings.a2a_run_lease_seconds)
        leased_rows = (
            await db.execute(
                select(A2ATask.run_id).where(
                    A2ATask.run_id.in_(a2a_candidate_ids),
                    A2ATask.reaped.is_(False),
                    A2ATask.lease_heartbeat_at.isnot(None),
                    A2ATask.lease_heartbeat_at >= lease_cutoff,
                )
            )
        ).all()
        live_a2a_run_ids = {str(row[0]) for row in leased_rows if row[0]}

    # Per-assignment wall-clock timeout (drives the deadline for runs that carry
    # an assignment); timeout-less tracing/legacy runs fall back to the setting.
    assignment_ids = {run.assignment_id for run in candidates if run.assignment_id}
    timeout_by_assignment: dict = {}
    if assignment_ids:
        timeout_rows = (
            await db.execute(
                select(Assignment.id, Assignment.timeout_seconds).where(
                    Assignment.id.in_(assignment_ids)
                )
            )
        ).all()
        timeout_by_assignment = {row[0]: row[1] for row in timeout_rows}

    fallback_seconds = max(60, settings.stale_assignment_run_seconds)
    warn_seconds = max(0, settings.stale_assignment_warn_seconds)
    grace = settings.orchestration_grace_seconds
    # Tighter deadline for in-process tracing runs (assignment_id IS NULL):
    # their Redis chat:processing marker provides reliable liveness detection,
    # so a shorter threshold is safe and reduces the warning-flood window.
    # Falls back to the global fallback when the setting is 0 or unset.
    _in_process_seconds_raw = settings.stale_in_process_run_seconds
    in_process_fallback = (
        max(60, _in_process_seconds_raw)
        if _in_process_seconds_raw > 0
        else fallback_seconds
    )

    # Chat-handover parents a platform stop kept for the automatic resume of
    # their cut turn: left while the cut record is queued (or Redis
    # cannot say), and for a short grace once it left the queue, the time the
    # resumed turn takes to adopt them.
    awaiting = {
        str(run.id): conv
        for run in candidates
        if (conv := _awaiting_auto_resume(run)) is not None
    }
    queued_awaiting: set[str] | None = set()
    if awaiting:
        from app.services.cut_turn_resume import queued_conversations

        queued_awaiting = await queued_conversations(awaiting.values())

    for run in candidates:
        awaited_conv = awaiting.get(str(run.id))
        if awaited_conv is not None:
            missed = _missed_auto_resume(run, awaited_conv, queued_awaiting, now)
            if missed is None:
                continue
            # A live turn holds the conversation: the resumed turn adopted it
            # before the stopping process's shutdown sweep marked it (the
            # resume was taken within seconds of the cut), or it is about to.
            # That turn finalizes it; failing it here would cut its children.
            if str(run.id) in live_chat_turn_run_ids:
                continue
            prior_status = run.status
            run.status = "failed"
            run.error = (
                f"{STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX} The run sat in "
                f"'{prior_status}' because {missed}."
            )[:2000]
            run.completed_at = now
            cleaned += 1
            _STALE_RUNS_FINALIZED.labels(outcome="failed", orphan_class="missed_auto_resume").inc()
            logger.warning(
                "Chat handover parent run %s (agent=%s) failed: %s",
                run.id,
                run.agent_id,
                missed,
            )
            continue

        exec_state = exec_state_by_key.get(execution_dedup_key(run.id))

        # The engine is still actively managing this run — defer to the worker
        # reconciler (lease reclaim / deadline sweep). Never race it.
        if exec_state is not None and exec_state not in TERMINAL_EXECUTION_STATES:
            continue

        # Same contract for delegation child runs: a live ``sub_agent``
        # execution carries this run in its payload, so the engine owns it.
        if exec_state is None and str(run.id) in live_delegation_run_ids:
            continue

        # Active agent_chat turn: the chat:processing:{conv_id} Redis marker
        # is alive, so the outer chat turn is still legitimately executing.
        # Skip to avoid false warnings / force-fails of long-running turns
        # (mirrors the fix for delegation runs).
        if exec_state is None and str(run.id) in live_chat_turn_run_ids:
            continue

        # External A2A run holding a live execution lease: its own reaper
        # (``a2a.tasks.reap_stale_tasks``) drives it terminal, and it heartbeats,
        # so this sweep must not race it.
        if exec_state is None and str(run.id) in live_a2a_run_ids:
            continue

        started = run.started_at or run.created_at

        if exec_state in TERMINAL_EXECUTION_STATES:
            # Desync: the execution already returned a terminal verdict but the
            # run was never finalized. Reconcile immediately — the engine is
            # done with it, so no age grace is needed.
            reason = (
                f"its orchestration execution is terminal ({exec_state}) but the "
                "run was never finalized (the engine's terminal-state sync hook "
                "did not update the run)"
            )
            orphan_class = "terminal_desync"
        else:
            # No execution row at all (orphaned tracing/legacy run). Require an
            # age threshold so a run that is genuinely in-flight in another
            # process is never failed prematurely.
            #
            # In-process tracing runs (assignment_id IS NULL) use the tighter
            # ``stale_in_process_run_seconds`` threshold: their Redis liveness
            # marker makes false positives much less likely, and a shorter
            # deadline limits the warning-flood window.
            timeout = timeout_by_assignment.get(run.assignment_id)
            if timeout is not None and timeout > 0:
                deadline_seconds = timeout + grace
            elif run.assignment_id is None:
                deadline_seconds = in_process_fallback
            else:
                deadline_seconds = fallback_seconds
            age_seconds = (now - started).total_seconds() if started is not None else 0
            if started is None or age_seconds < deadline_seconds:
                # Emit an early warning for runs approaching the stale threshold
                # so operators can detect orphaned runs well before they are
                # force-failed. The warning repeats every tick
                # (60 s) until the run is failed — deduplicate on run_id in
                # your alerting/monitoring system.
                if warn_seconds > 0 and age_seconds >= warn_seconds:
                    logger.warning(
                        "Orphaned assignment run %s (agent=%s, assignment=%s) "
                        "has no orchestration execution and has been active for "
                        "~%ds — will be force-failed after %ds "
                        "(orphaned by a restart or crash before it reached a "
                        "terminal state)",
                        run.id,
                        run.agent_id,
                        run.assignment_id,
                        int(age_seconds),
                        deadline_seconds,
                    )
                    _STALE_RUN_WARNINGS.labels(orphan_class="no_execution").inc()
                continue
            reason = (
                "it has no orchestration execution and exceeded the stale "
                f"threshold of {deadline_seconds}s (orphaned by a restart or "
                "crash before it reached a terminal state)"
            )
            orphan_class = "no_execution"

        prior_status = run.status
        age = int((now - started).total_seconds()) if started else 0
        # Reconcile the run status to match the execution outcome.  A
        # successfully-completed execution must never be misreported as failed
        # just because the terminal-state sync hook was skipped.
        # Orphaned runs with no execution row are always failed.
        terminal_run_status = "completed" if exec_state == "completed" else "failed"
        run.status = terminal_run_status
        if terminal_run_status == "completed":
            # A completed run must never surface an error, and the Result
            # card must not be blank — the desync lost the finalize write
            # that would have persisted the real summary (see the constant).
            run.error = None
            if not run.result_summary:
                run.result_summary = RECONCILED_COMPLETED_RESULT_SUMMARY
        else:
            run.error = (
                f"{STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX} "
                f"The run sat in '{prior_status}' for ~{age}s because {reason}. "
                "This usually means the backend or worker was restarted or "
                "interrupted before the run record was finalized."
            )[:2000]
        run.completed_at = now
        cleaned += 1
        _STALE_RUNS_FINALIZED.labels(outcome=terminal_run_status, orphan_class=orphan_class).inc()
        if terminal_run_status == "completed":
            logger.info(
                "Stale assignment run %s (agent=%s, assignment=%s) reconciled "
                "to completed after ~%ds: %s",
                run.id,
                run.agent_id,
                run.assignment_id,
                age,
                reason,
            )
        else:
            logger.warning(
                "Stale assignment run %s (agent=%s, assignment=%s) failed after "
                "~%ds: %s",
                run.id,
                run.agent_id,
                run.assignment_id,
                age,
                reason,
            )

    if cleaned:
        await db.commit()

    return cleaned


def _chat_handover_conversation(run: AssignmentRun) -> str | None:
    """The conversation of a chat-handover parent run (``agent_chat``); ``None`` for any other run."""
    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    payload = run.trigger_payload if isinstance(run.trigger_payload, dict) else {}
    if context.get("type") != "chat_handover" and payload.get("trigger_type") != "chat_handover":
        return None
    conv = payload.get("conversation_id")
    return str(conv) if conv else None


def _awaiting_auto_resume(run: AssignmentRun) -> str | None:
    """The conversation whose queued resume a parent run was kept for; ``None`` when it was not."""
    from app.services.cut_turn_resume import AWAITING_AUTO_RESUME_KEY

    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    conv = context.get(AWAITING_AUTO_RESUME_KEY)
    return str(conv) if conv else None


def _missed_auto_resume(
    run: AssignmentRun, conversation_id: str, queued: set[str] | None, now: datetime
) -> str | None:
    """Why a parent run kept for a resume is failed now; ``None`` while it still waits.

    It waits while its cut record is queued, while Redis cannot say (``queued``
    is ``None``), and for :data:`cut_turn_resume.PARENT_ADOPTION_GRACE_SECONDS`
    after it left the queue: the resumed turn is starting and adopts it.
    """
    from app.services.cut_turn_resume import AWAITING_SINCE_KEY, PARENT_ADOPTION_GRACE_SECONDS

    if queued is None or conversation_id in queued:
        return None
    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    since = None
    with contextlib.suppress(TypeError, ValueError):
        since = datetime.fromisoformat(str(context.get(AWAITING_SINCE_KEY)))
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    if since is not None and (now - since).total_seconds() < PARENT_ADOPTION_GRACE_SECONDS:
        return None
    return (
        f"a platform stop cut its chat turn and kept it for the automatic resume of "
        f"conversation {conversation_id}, but that resume never adopted it (the cut "
        "turn left the queue without resuming: the person wrote or stopped since, "
        "the message is gone, or the resume was refused)"
    )


async def fail_orphaned_in_process_runs(
    db: AsyncSession, run_ids: set[str] | None = None
) -> int:
    """Immediately finalize THIS process's interrupted in-process tracing runs.

    In-process tracing runs (``assignment_id IS NULL``) are created by
    ``agent_chat`` for chat-handover / consult / transfer scenarios.  They
    carry no orchestration execution, so the worker reconciler can never
    recover them.

    Called at process shutdown (web ``lifespan`` and worker ``shutdown``
    hooks) with the process-local registry snapshot
    (``in_process_runs.get_tracked_in_process_runs()``): any registered run
    still in an active status is being interrupted by this shutdown, so it
    is finalized immediately and reaches a truthful terminal status in the
    UI within seconds instead of waiting up to
    ``stale_assignment_run_seconds`` (default 1 800 s) for the periodic
    sweep — shutdown-interrupted work is failed, while a terminal-desync run
    whose execution actually completed is reconciled to ``completed``.  This
    eliminates the silent ~30 min hang observed during rolling deploys.

    The sweep is deliberately scoped to ``run_ids`` — the runs THIS process
    created — never a global ``assignment_id IS NULL`` query:

    * another replica's live chat turn looks identical in the DB and must
      not be failed by a neighbouring pod's restart;
    * background-delegation child runs are engine-managed via a
      ``sub_agent_task:<task_id>``-keyed execution (not
      ``assignment_run:<run_id>``), so a key-based liveness probe alone
      cannot protect them.

    Orphans from a *crashed* process (SIGKILL / OOM — no shutdown hook ran)
    remain covered by ``_cleanup_stale_assignment_runs`` on the scheduler
    tick: without a per-run heartbeat, a starting pod cannot distinguish a
    dead process's runs from another live replica's, so there is no safe
    immediate startup sweep.

    Only runs with ``assignment_id IS NULL`` *and* no live orchestration
    execution are touched.  Any run whose execution is still in a
    non-terminal state is skipped — the worker reconciler owns it and we
    must not race it.  The defensive terminal-desync branch maps the run to
    the execution's actual outcome (``completed`` execution → ``completed``
    run, every other terminal state → ``failed``), mirroring
    ``_cleanup_stale_assignment_runs``.

    A chat-handover parent run whose conversation has its cut turn queued for
    the automatic resume (``cut_turn_resume``, queued by the drain before this
    sweep) is kept, not failed: its ``trigger_context`` is marked
    ``awaiting_auto_resume`` with the conversation id, and the resumed turn
    adopts it, so the runs it delegated stay under one parent and no red run
    asks for a Resume click. A turn that is not queued is failed as before.

    Returns the number of runs finalized (completed or failed).
    """
    from app.models.orchestration import (
        TERMINAL_EXECUTION_STATES,
        OrchestrationExecution,
    )
    from app.services.orchestration_adapters.assignment import execution_dedup_key

    if not run_ids:
        return 0
    parsed_ids = [safe_uuid(rid) for rid in run_ids]
    run_uuids = [rid for rid in parsed_ids if rid is not None]
    if not run_uuids:
        return 0

    now = datetime.now(UTC)

    candidates = (
        (
            await db.execute(
                select(AssignmentRun).where(
                    AssignmentRun.id.in_(run_uuids),
                    AssignmentRun.status.in_(STALE_ASSIGNMENT_ACTIVE_STATUSES),
                    AssignmentRun.assignment_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return 0

    # Defensive: in-process tracing runs should never have an orchestration
    # execution, but check anyway so we never accidentally race the reconciler.
    dedup_keys = [execution_dedup_key(run.id) for run in candidates]
    exec_rows = (
        await db.execute(
            select(
                OrchestrationExecution.dedup_key,
                OrchestrationExecution.state,
            ).where(OrchestrationExecution.dedup_key.in_(dedup_keys))
        )
    ).all()
    exec_state_by_key = {row[0]: row[1] for row in exec_rows}

    # Chat-handover parents whose cut turn the drain queued for its automatic
    # resume: kept for the resumed turn. Redis unreadable: failed as
    # before, the one-click Resume stays.
    handover_convs = {
        str(run.id): conv
        for run in candidates
        if (conv := _chat_handover_conversation(run)) is not None
    }
    queued_convs: set[str] = set()
    if handover_convs:
        from app.services.cut_turn_resume import queued_conversations

        queued_convs = await queued_conversations(handover_convs.values()) or set()

    from app.services.cut_turn_resume import AWAITING_AUTO_RESUME_KEY, AWAITING_SINCE_KEY

    finalized = 0
    kept = 0
    for run in candidates:
        exec_state = exec_state_by_key.get(execution_dedup_key(run.id))
        # Skip any run the engine is still actively managing.
        if exec_state is not None and exec_state not in TERMINAL_EXECUTION_STATES:
            continue

        conv = handover_convs.get(str(run.id))
        if exec_state is None and conv is not None and conv in queued_convs:
            # A new dict: the JSON column only sees a reassignment.
            run.trigger_context = {
                **(run.trigger_context if isinstance(run.trigger_context, dict) else {}),
                AWAITING_AUTO_RESUME_KEY: conv,
                AWAITING_SINCE_KEY: now.isoformat(),
            }
            kept += 1
            logger.info(
                "Chat handover parent run %s (agent=%s) kept for the automatic resume "
                "of conversation %s",
                run.id,
                run.agent_id,
                conv,
            )
            continue

        # Accurate cause per branch: the common case is a run interrupted by
        # this process's shutdown, but the defensive desync branch (terminal
        # execution that was never synced back to the run) has a different
        # root cause and must not masquerade as a shutdown interruption.
        if exec_state in TERMINAL_EXECUTION_STATES:
            reason = (
                f"its orchestration execution is terminal ({exec_state}) but "
                "the run was never finalized (desync reconciled during "
                "process shutdown)"
            )
        else:
            reason = (
                "it was interrupted by a platform shutdown or pod restart "
                "before it reached a terminal state"
            )

        started = run.started_at or run.created_at
        age = int((now - started).total_seconds()) if started else 0
        prior_status = run.status
        # Same outcome mapping as ``_cleanup_stale_assignment_runs``: a
        # ``completed`` execution desync must never be misreported as failed.
        # Runs with no execution row are shutdown-interrupted work
        # and are always failed.
        terminal_run_status = "completed" if exec_state == "completed" else "failed"
        run.status = terminal_run_status
        if terminal_run_status == "completed":
            run.error = None
            if not run.result_summary:
                run.result_summary = RECONCILED_COMPLETED_RESULT_SUMMARY
        else:
            run.error = (
                f"{STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX} "
                f"The run sat in '{prior_status}' for ~{age}s because {reason}."
            )[:2000]
        run.completed_at = now
        finalized += 1
        logger.info(
            "Orphaned in-process tracing run %s (agent=%s) finalized to "
            "'%s' after ~%ds: %s",
            run.id,
            run.agent_id,
            terminal_run_status,
            age,
            reason,
        )

    if finalized or kept:
        await db.commit()

    return finalized


async def _recover_stale_orchestration_executions() -> int:
    """Web-process backstop for the worker reconciler's recovery sweeps.

    The worker reconciler sweeps deadlines + reclaims crashed leases on every
    cycle. When the worker is DOWN those never run, and because the autonomous
    safety net (:func:`_cleanup_stale_assignment_runs`) deliberately defers to
    the reconciler for runs whose execution is still ``pending`` / ``running``,
    a live execution with no worker would hang forever. Running the same
    all-adapter sweeps here — in the always-on web scheduler tick — guarantees
    past-deadline executions are dead-lettered (their backing ``AssignmentRun``
    / ``SubAgentTask`` + linked run / ``EvaluationRun`` failed via the adapter's
    ``on_dead_letter`` hook) and crashed leases reclaimed within ~60s even with
    no worker at all.

    ``FOR UPDATE SKIP LOCKED`` (inside the engine sweeps) makes this safe to run
    concurrently with a healthy reconciler — neither double-processes a row, and
    a healthy worker's in-flight executions are never touched (their deadline is
    in the future; HITL ``awaiting_human`` executions have no deadline). Uses
    its own session so a sweep error can't poison the caller's tick transaction.
    Returns the number of executions dead-lettered.
    """
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters import register_builtin_adapters
    from app.services.orchestration_reconciler import resumable_adapters, run_dead_letter

    register_builtin_adapters()
    batch = settings.orchestration_claim_batch
    dead_lettered: list = []
    async with async_session() as db:
        dead_lettered.extend(await engine.sweep_deadlines(db, limit=batch))
        dead_lettered.extend(
            await engine.reclaim_expired_leases(db, limit=batch, head_adapters=resumable_adapters())
        )
        await db.commit()
    # Drive each dead-letter cleanup through the reconciler's resilient helper
    # (own session per execution, logs+swallows failures) so one corrupt payload
    # can't abort the rest of the batch or the surrounding tick.
    for execution in dead_lettered:
        await run_dead_letter(execution, worker_id="web-backstop")
    return len(dead_lettered)


async def _cleanup_expired_kb_export_packages(db: AsyncSession) -> int:
    """Delete expired KB export package rows and backing S3 objects.

    Capped per tick (oldest-expired first), like the other cleanup sweeps, so a
    flood of simultaneously expiring packages cannot monopolise a scheduler
    cycle on S3 deletes / DB commits; the remainder is swept on later ticks.
    """
    from app.services.file_storage import file_storage

    cleanup_batch = 500
    now = datetime.now(UTC)
    result = await db.execute(
        select(KBExportPackage)
        .where(KBExportPackage.expires_at <= now)
        .order_by(KBExportPackage.expires_at.asc())
        .limit(cleanup_batch)
    )
    packages = list(result.scalars().all())
    if not packages:
        return 0

    cleaned = 0
    for pkg in packages:
        await file_storage.delete(pkg.s3_object_name)
        await db.delete(pkg)
        cleaned += 1

    await db.commit()
    return cleaned


async def _cleanup_resolved_shared_states(db: AsyncSession) -> int:
    """Purge resolved shared-state entries past the retention window.

    The platform backstop so resolved shared state never lives forever even when
    the owning agent forgets to delete it (the agent-driven resolve/purge path is
    the primary one). Capped per call and oldest-resolved first, like the other
    cleanup sweeps. ACTIVE entries are never touched. Disabled when the retention
    setting is <= 0.
    """
    del db  # purge_resolved_states opens its own session, like the rest of the service
    from app.services.coordination_retention import effective_coordination_retention

    days = (await effective_coordination_retention())["shared_state_resolved_days"]
    if not days or days <= 0:
        return 0
    from app.services.shared_state import purge_resolved_states

    return await purge_resolved_states(older_than=timedelta(days=days), limit=COORDINATION_PURGE_BATCH)


async def _cleanup_archived_shared_state_items() -> int:
    """Drop items ``archive_key`` archived past ``shared_state_archive_retention_days``.

    An archive is an active entry, so the resolved purge never reaches it:
    without this sweep archiving only relocates a list's growth. Disabled
    when the setting is <= 0.
    """
    from app.services.coordination_retention import effective_coordination_retention

    days = (await effective_coordination_retention())["shared_state_archive_days"]
    if not days or days <= 0:
        return 0
    from app.services.shared_state import purge_archived_items

    return await purge_archived_items(older_than=timedelta(days=days), limit=200)


async def _cleanup_work_items(db: AsyncSession) -> dict:
    """Reap the work-item queue: auto-cancel abandoned, purge terminal.

    The Work Item twin of :func:`_cleanup_resolved_shared_states`. Both phases
    are independently disabled by their ``<= 0`` retention settings and open
    their own session, so a no-op tick is cheap. Returns
    ``{"cancelled": N, "purged": M}``.
    """
    del db  # reap_work_items opens its own session, like the rest of the service
    from app.services.coordination_retention import effective_coordination_retention
    from app.services.work_item import reap_work_items

    windows = await effective_coordination_retention()
    return await reap_work_items(
        abandon_days=windows["work_item_abandoned_days"],
        retain_days=windows["work_item_resolved_days"],
    )


#: Rows one coordination purge call deletes; a call that deletes this many may have left more.
COORDINATION_PURGE_BATCH = 2000


async def _drain_coordination_retention() -> tuple[dict, bool]:
    """One bounded pass of the closed work-item and resolved shared-state purges (#2070).

    Each purge deletes at most :data:`COORDINATION_PURGE_BATCH` rows; a purge
    that reached it answers ``more``, which keeps
    :data:`wall_clock_schedule.RETENTION_DRAIN` set so the next ticks run this
    pass again until the backlog past retention is gone, instead of one batch an
    hour. Answers the rows deleted per table and whether any purge may have more.
    """
    from app.services.coordination_retention import effective_coordination_retention
    from app.services.shared_state import purge_resolved_states
    from app.services.work_item import purge_terminal_work_items

    windows = await effective_coordination_retention()
    work_items = await purge_terminal_work_items(
        older_than_days=windows["work_item_resolved_days"], limit=COORDINATION_PURGE_BATCH
    )
    days = windows["shared_state_resolved_days"]
    states = (
        await purge_resolved_states(older_than=timedelta(days=days), limit=COORDINATION_PURGE_BATCH)
        if days and days > 0
        else 0
    )
    more = work_items >= COORDINATION_PURGE_BATCH or states >= COORDINATION_PURGE_BATCH
    return {"work_items": work_items, "shared_states": states}, more


async def _drain_run_retention() -> tuple[int, bool]:
    """One bounded pass of the assignment run retention (Operations > Executions).

    Deletes at most :data:`run_retention.RUN_RETENTION_BATCH` finished runs past
    the window (``services/run_retention.py``) and writes one platform audit entry
    when it deleted any. Answers the runs deleted and whether more may be left,
    which keeps :data:`wall_clock_schedule.RETENTION_DRAIN` set.
    """
    from app.services import run_retention
    from app.services.coordination_retention import effective_coordination_retention

    days = (await effective_coordination_retention())["assignment_run_days"]
    deleted = await run_retention.purge_expired_runs(older_than_days=days)
    if deleted:
        await run_retention.audit_run_retention_pass(deleted, days)
    return deleted, deleted >= run_retention.RUN_RETENTION_BATCH


async def _cleanup_flow_events(db: AsyncSession) -> int:
    """Purge flow-event emissions past retention.

    ``flow_events`` is append-only and had no sweep at all until durable
    memory writes started emitting into it, which made it the fastest-growing
    coordination table on a busy platform. Dispatch happens synchronously at
    emit time, so an aged row is audit history and nothing else.

    The window's floor lives in :func:`purge_old_flow_events` rather than
    here: it is a correctness property of the deletion (a parked
    ``Wait for event`` must never be stranded), not a scheduling choice.
    Disabled when ``flow_event_retention_days`` is <= 0.
    """
    del db  # purge_old_flow_events opens its own session, like the rest
    from app.services.flow_events import purge_old_flow_events

    return await purge_old_flow_events()


async def _cleanup_video_media(db: AsyncSession) -> dict:
    """Delete the Video Studio media past retention.

    A recording is a mezzanine video and an AI shot a provider's clip; media a
    video uses are never expired. Once no video uses them they are kept
    ``video_studio_media_retention_days`` after the last video using them
    stopped, media no video used ``video_studio_unused_media_retention_days``
    after it was made. The rows stay, marked expired, so the person records or
    generates them again. A failed or cancelled one no video uses is deleted,
    row included. Returns ``{"purged": P, "expired": N, "in_use": M}``.
    """
    from app.services.video_studio import retention

    return await retention.sweep(db)


async def _cleanup_video_renders(db: AsyncSession) -> int:
    """Delete the Video Studio renders past retention, once a night.

    The administrator's days (Settings: failed or cancelled renders, scene
    previews, superseded ready renders); the newest ready render of each
    quality of a video, its current render, the renders kept by hand and those
    in progress stay. The sweep logs what it freed per video. Returns how many
    renders were deleted, with their files; 0 outside the night or once swept.
    """
    from app.services.video_studio import render_retention

    swept = await render_retention.sweep_if_due(db)
    return swept.renders if swept is not None else 0


async def _cleanup_figure_stills() -> int:
    """Delete the editor's figure stills drawn more than ``video_studio_figure_still_retention_days`` ago.

    A still a video still shows is drawn again by the next save or preview
    that finds it missing; so is a screen insert's preview build
    (``insert_builds``), swept after the same days. Returns how many were deleted.
    """
    from app.services.video_studio import figure_stills, insert_builds

    return await figure_stills.sweep() + await insert_builds.sweep()


async def _cleanup_check_files(db: AsyncSession) -> tuple[int, bool]:
    """Delete the check files made more than ``agent_check_file_retention_days`` ago.

    The pictures a tool made for the agent to look at (frames, contact sheets,
    insert sweeps, slide captures, page renders); one a video uses is kept.
    Returns how many were deleted and whether more are left past retention.
    """
    from app.services import check_files

    return await check_files.sweep_pass(db)


async def _cleanup_retained_files(db: AsyncSession, *, also: dict[str, int] | None = None) -> tuple[int, bool]:
    """Delete the stored files past their category's retention (Settings > Storage & retention).

    The agent's scratch and delivered files go 60 days after their last use by
    default; a file a live object still names is kept, and a deleted one stays
    as a tombstone (``services/file_retention.py``). *also* adds the files an
    older sweep deleted (check pictures) to the figures Settings shows. Returns
    how many files and whether a category stopped at its per-tick cap.
    """
    from app.services import file_retention

    report = await file_retention.sweep(db, also=also)
    files = sum(int(tally.get("files", 0)) for key, tally in report.items() if not also or key not in also)
    return files, any(bool(tally.get("more")) for tally in report.values())


async def _run_retention_pass(db: AsyncSession) -> None:
    """One bounded pass of the check file, file, coordination, run and CTEM signal retentions (#768, #2070, #2581).

    Each sweep is capped per tick; when one stops at its cap, the backlog flag
    of :data:`wall_clock_schedule.RETENTION_DRAIN` makes the next ticks run this
    pass again until nothing past retention is left, instead of waiting an hour.
    Each sweep catches its own failure, so one never starves the other.
    """
    swept_checks, more_checks = 0, False
    try:
        swept_checks, more_checks = await _cleanup_check_files(db)
        if swept_checks:
            logger.info(
                "Check file retention: deleted %d picture%s made for an agent to look at",
                swept_checks,
                "" if swept_checks == 1 else "s",
            )
    except Exception as e:
        logger.error("Check file retention error: %s", format_exception_message(e), exc_info=True)
        await db.rollback()
    more_files = False
    try:
        swept_files, more_files = await _cleanup_retained_files(db, also={"agent_check": swept_checks})
        if swept_files:
            logger.info(
                "File retention: deleted the bytes of %d file%s past their retention",
                swept_files,
                "" if swept_files == 1 else "s",
            )
    except Exception as e:
        logger.error("File retention error: %s", format_exception_message(e), exc_info=True)
        await db.rollback()
    more_coordination = False
    try:
        purged, more_coordination = await _drain_coordination_retention()
        if purged["work_items"] or purged["shared_states"]:
            logger.info(
                "Coordination retention: deleted %d closed work item(s) and %d resolved shared-state entr%s",
                purged["work_items"],
                purged["shared_states"],
                "y" if purged["shared_states"] == 1 else "ies",
            )
    except Exception as e:
        logger.error("Coordination retention error: %s", format_exception_message(e), exc_info=True)
    more_runs = False
    try:
        deleted_runs, more_runs = await _drain_run_retention()
        if deleted_runs:
            logger.info("Run retention: deleted %d finished assignment run(s)", deleted_runs)
    except Exception as e:
        logger.error("Run retention error: %s", format_exception_message(e), exc_info=True)
    more_signals = False
    try:
        from app.services import ctem_signal_retention

        deleted_signals, more_signals = await ctem_signal_retention.run_pass()
        if any(deleted_signals.values()):
            logger.info(
                "CTEM signal retention: deleted %d discarded and %d watchlist signal(s)",
                deleted_signals.get("discarded", 0),
                deleted_signals.get("watchlist", 0),
            )
    except Exception as e:
        logger.error("CTEM signal retention error: %s", format_exception_message(e), exc_info=True)
    await wall_clock_schedule.set_backlog(
        wall_clock_schedule.RETENTION_DRAIN,
        more_checks or more_files or more_coordination or more_runs or more_signals,
    )


async def _cleanup_kb_figure_sources(db: AsyncSession) -> int:
    """Delete the PDFs kept for a knowledge-base figure pass that no pass will read.

    A document's job deletes its PDF once the figures are described; this is
    the backstop for a document deleted (by any path) before then, and for a
    PDF kept past ``kb_figure_pass.SOURCE_RETENTION`` for a retry that never
    came. Returns how many were deleted.
    """
    from app.services import kb_figure_pass

    return await kb_figure_pass.sweep_sources(db)


async def _cleanup_stored_video_work() -> int:
    """Delete what Video Studio keeps in the store for work that is over.

    The figure drawings of videos that no longer exist (a video's deletion
    evicts them), the segments kept beside renders that ended (the attempt
    that ends a render clears them) and the scene ranges of those renders
    with what they were drawn from: the backstop for what neither
    cleared. Returns how many objects were deleted.
    """
    from app.services.video_studio import figure_cache, render_cache, render_ranges, render_segments

    return (
        await figure_cache.sweep_store()
        + await render_segments.sweep()
        + await render_ranges.sweep()
        + await render_cache.sweep()
    )


async def _cleanup_orphan_annotations(db: AsyncSession) -> int:
    """Delete ratings whose rated run / task / delegation is gone.

    An annotation points at its target without a foreign key (the key would
    lock the write-hot run and task tables in the migration), so the deletes of
    those tables leave it behind. Listings already hide such rows; this removes
    them. Bounded per call like the other sweeps.
    """
    del db  # purge_orphan_annotations opens its own session, like the rest of the service
    from app.services.annotations import purge_orphan_annotations

    return await purge_orphan_annotations()


async def _cleanup_worksets(db: AsyncSession) -> dict:
    """Purge worksets past their retention.

    The third member of the coordination family's self-cleaning backstop, and
    the one that costs the most to skip: a workset holds documents, chunks and
    vectors, so a corpus nobody purged is storage, not a stale row.

    Two reasons a workspace is due — resolved past its retention window, and an
    explicit TTL that has passed. The second is the one that carries the
    feature: nothing resolves a workset when its run fails halfway, and a
    run-scoped corpus outliving its run is exactly the leak the TTL prevents.
    An ACTIVE workset with no expiry is never touched — it may belong to a
    fortnightly job, and deleting it on inactivity would silently make the next
    run start cold. Returns ``{"resolved": N, "expired": M}``.
    """
    del db  # sweep_worksets opens its own session, like the rest of the service
    from app.services.coordination_retention import effective_coordination_retention
    from app.services.workset import sweep_worksets

    days = (await effective_coordination_retention())["workset_resolved_days"]
    return await sweep_worksets(
        resolved_after=timedelta(days=days) if days and days > 0 else None,
        limit=settings.workset_sweep_batch,
    )


_tick_lock = asyncio.Lock()

#: The window one tick is given (``main._autonomous_loop`` sleeps 60 s between
#: ticks, its Redis window lock lives 55 s): a tick longer than it overran.
_TICK_WINDOW_SECONDS = 60.0


class _TickClock:
    """How long a tick ran and its slowest step (#2562).

    ``mark(step)`` closes the step running and opens *step*; the steps are the
    numbered sections of :func:`_run_autonomous_tick_inner`.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._started = time.monotonic()
        self._step = "start"
        self._step_started = self._started
        self.slowest: tuple[str, float] = ("start", 0.0)

    def mark(self, step: str) -> None:
        now = time.monotonic()
        took = now - self._step_started
        if took > self.slowest[1]:
            self.slowest = (self._step, took)
        self._step = step
        self._step_started = now

    def finish(self) -> tuple[float, tuple[str, float]]:
        """The tick's duration and its slowest step with that step's duration."""
        self.mark("end")
        return time.monotonic() - self._started, self.slowest


_tick_clock = _TickClock()


#: The time one housekeeping step of a tick may take (#2582): a third of the
#: tick window, so schedule evaluation is never more than one window late.
_HOUSEKEEPING_BUDGET_SECONDS = 20.0

#: Past its budget, how long a step is given to commit what it released before
#: it is cancelled (a cancelled step rolls back; the next tick does it again).
_HOUSEKEEPING_GRACE_SECONDS = 10.0


async def _within_housekeeping_budget(step: str, call: Callable[[float], Awaitable[int | None]]) -> int:
    """Run one housekeeping *call* of the tick within its time budget (#2582).

    *call* takes a ``time.monotonic()`` deadline and stops its own batch there,
    committing what it did; a call still running past the deadline and the
    grace (a read stuck on the pool) is cancelled and answers ``0``: the rest
    waits for the next tick, with one WARNING naming the step.
    """
    deadline = time.monotonic() + _HOUSEKEEPING_BUDGET_SECONDS
    try:
        return (
            await asyncio.wait_for(call(deadline), timeout=_HOUSEKEEPING_BUDGET_SECONDS + _HOUSEKEEPING_GRACE_SECONDS)
            or 0
        )
    except TimeoutError:
        logger.warning(
            "Scheduler housekeeping step '%s' cancelled past its %ds budget: the rest waits for the next tick",
            step,
            int(_HOUSEKEEPING_BUDGET_SECONDS + _HOUSEKEEPING_GRACE_SECONDS),
        )
        return 0


def _warn_if_tick_overran() -> None:
    """One WARNING for a tick past its window, naming its duration and slowest step (#2562).

    The overrun is also kept as the reason the next sweep gives for the
    windows it records missed.
    """
    global _schedule_trouble
    took, (step, step_took) = _tick_clock.finish()
    if took <= _TICK_WINDOW_SECONDS:
        return
    logger.warning(
        "Scheduler tick overran its %ds window: %.1fs, slowest step '%s' (%.1fs)",
        int(_TICK_WINDOW_SECONDS),
        took,
        step,
        step_took,
    )
    if _schedule_trouble is None:
        _schedule_trouble = f"scheduler tick overran: {int(took)}s, slowest step {step} ({int(step_took)}s)"


async def run_autonomous_tick() -> int:
    """Run one tick of the autonomous scheduler.

    Returns the number of assignments triggered.
    A lock prevents overlapping ticks in this process when a previous tick
    runs longer than the 60 s interval (e.g. slow KB sync or memory
    maintenance); other replicas are not covered by it.
    """
    if _tick_lock.locked():
        logger.warning("Skipping scheduler tick — previous tick still running")
        return 0

    # A tick serves no one: a step that resolves a model for a user must not
    # leave that user bound for the next step, nor for the next tick.
    async with _tick_lock:
        _tick_clock.reset()
        try:
            with acting_for(None):
                return await _run_autonomous_tick_inner()
        finally:
            _warn_if_tick_overran()


#: The refusal the "no AI dispatch" notice was last logged for, in the current
#: off-period. The tick runs every few seconds, so an unconditional INFO line
#: would be spam — which is why this started as ``debug`` and said nothing at
#: all. Debounced instead: one line when dispatch stops (or its reason
#: changes), and it re-arms when AI comes back. Same reasoning as the flow
#: hold-back debounce.
_dispatch_refusal_logged: str | None = None

#: Heartbeat and change floor for the probe-plumbing ERROR below: first
#: occurrence at ERROR, repeats at DEBUG, one ERROR per interval while it
#: lasts, a different failure class once the floor has passed.
_EMBEDDING_PROBE_ERROR_LOG_INTERVAL_SECONDS = 3600.0
_EMBEDDING_PROBE_ERROR_FLOOR_SECONDS = 300.0

#: ``(exception class of the last ERROR line, its monotonic timestamp)``; the
#: class becomes ``None`` once the probe works again (the timestamp is kept, so
#: an intermittent failure cannot re-emit on every flip), and the whole value
#: is ``None`` until the first ERROR. Lives HERE, not in ``kb_helpers``: the
#: failure this throttles is essentially ``kb_helpers`` failing to import (the
#: resolver swallows every provider error itself), and a failed import is
#: retried - and fails - on every tick.
_embedding_probe_error_logged: tuple[str | None, float] | None = None


def _log_embedding_probe_error(e: Exception) -> None:
    """ERROR once, then DEBUG, with an hourly heartbeat (see the constants)."""
    global _embedding_probe_error_logged
    # Module + qualname: two ``TimeoutError`` / ``Error`` classes from
    # different modules are different failures.
    kind = f"{type(e).__module__}.{type(e).__qualname__}"
    now = time.monotonic()
    previous = _embedding_probe_error_logged
    if previous is None:
        due = True
    elif previous[0] == kind:
        due = now - previous[1] >= _EMBEDDING_PROBE_ERROR_LOG_INTERVAL_SECONDS
    else:
        due = now - previous[1] >= _EMBEDDING_PROBE_ERROR_FLOOR_SECONDS
    if due:
        _embedding_probe_error_logged = (kind, now)
    (logger.error if due else logger.debug)(
        "KB embedding readiness probe error: %s",
        format_exception_message(e),
        exc_info=True,
    )


async def _probe_embedding_readiness() -> bool | None:
    """The tick's single embedding-readiness verdict for the KB recovery sweeps.

    ``True`` / ``False`` come from
    :func:`~app.services.kb_helpers.resolve_embedding_recovery_state`, which
    owns its own (throttled) log line. When ``kb_helpers`` cannot even be
    imported the answer is ``False`` too: the three sweeps import from the
    same module, so running them would only fail - and log - three more
    times. ``None`` means the resolver itself raised: the sweeps still run
    best-effort rather than KB self-healing going silently dark on an
    unrelated error. Either failure is logged through the throttle, so a
    persistent breakage (bad deploy, import failure) does not re-emit a full
    traceback every tick.
    """
    global _embedding_probe_error_logged
    try:
        from app.services.kb_helpers import resolve_embedding_recovery_state
    except Exception as e:
        _log_embedding_probe_error(e)
        return False
    try:
        verdict = await resolve_embedding_recovery_state()
    except Exception as e:
        _log_embedding_probe_error(e)
        return None
    if _embedding_probe_error_logged is not None:
        _embedding_probe_error_logged = (None, _embedding_probe_error_logged[1])
    return verdict


async def _run_autonomous_tick_inner() -> int:
    triggered = 0

    async with async_session() as db:
        # Resolved once per tick, not per step and not per process: a runtime
        # flip must take effect within one interval without a restart
        # (FR-002/SC-003). The combined gate, not the AI switch alone: a
        # deployment that dropped to Community Edition must stop dispatching
        # AI work too.
        #
        # The flow schedulers below are deliberately NOT gated, because
        # deterministic flows must keep running (FR-014). The AI question is
        # answered per flow instead, inside the trigger paths themselves:
        # ``flow_triggers.hold_back_if_ai_required`` records a skipped run for a
        # graph that contains an AI node and lets every other graph through.
        from app.services.agentic_gate import AI_DISABLED_CODE, agentic_refusal

        global _dispatch_refusal_logged, _blocked_refs_poll_tick_counter, _schedule_trouble

        _refusal = await agentic_refusal(db)
        _ai_on = _refusal is None
        if not _ai_on:
            if _dispatch_refusal_logged != _refusal:
                # Named and counted, at INFO: a scheduled assignment that simply
                # stops producing runs is what "broken" looks like from outside,
                # and every assignment runs an agent, so none of them dispatch.
                # Flows say this in Operations via a skipped run; assignments
                # have no equivalent, so the log is the only place it is said.
                from app.models.assignment import Assignment

                held = (
                    await db.execute(
                        select(func.count())
                        .select_from(Assignment)
                        .where(
                            Assignment.trigger_type == "schedule",
                            Assignment.enabled.is_(True),
                        )
                    )
                ).scalar() or 0
                if _refusal == AI_DISABLED_CODE:
                    logger.info(
                        "AI is disabled: no AI scheduled work is dispatched. "
                        "%d enabled scheduled assignment(s) will not run until "
                        "AI is re-enabled in Settings > Platform. "
                        "Deterministic flows are unaffected.",
                        held,
                    )
                else:
                    from app.api.deps import licence_required_detail

                    logger.info(
                        "AI is not licensed on this deployment: no AI scheduled "
                        "work is dispatched. %d enabled scheduled assignment(s) "
                        "will not run until it is, and nothing is disabled "
                        "meanwhile. Deterministic flows are unaffected. %s",
                        held,
                        licence_required_detail(),
                    )
                _dispatch_refusal_logged = _refusal
            else:
                logger.debug(
                    "Agentic AI unavailable (%s): skipping AI scheduler "
                    "dispatch this tick",
                    _refusal,
                )
        else:
            _dispatch_refusal_logged = None

        # Crash recovery for runs WITH a live orchestration execution is handled
        # durably by the worker reconciler (lease expiry + deadline sweeps).
        # Runs WITHOUT one (in-process tracing runs, legacy runs, on_dead_letter
        # desyncs) are swept by ``_cleanup_stale_assignment_runs`` in step 5c
        # below. There is no ``delayed_run:*`` poll here anymore.

        _tick_clock.mark("1")
        # --- 1. Refresh expiring OAuth tokens ---
        # Tokens must be fresh before any assignment or event polling reads them.
        try:
            from app.services.token_refresh import refresh_expiring_tokens

            refreshed = await refresh_expiring_tokens(db)
            if refreshed:
                logger.info("Refreshed %d expiring OAuth token(s)", refreshed)
        except Exception as e:
            logger.error("Token refresh error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("3")
        # --- 3. Process schedule-based assignments ---
        try:
            if _ai_on:
                triggered += await _process_scheduled_assignments(db)
                # The windows missed meanwhile were recorded with their reason.
                _schedule_trouble = None
        except Exception as e:
            logger.error("Scheduled assignments error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
            # Said by the next sweep on the windows it records missed (#2562).
            _schedule_trouble = f"scheduler sweep failed: {format_exception_message(e)}"[:200]

        _tick_clock.mark("3-bis")
        # --- 3-bis. One admin alert per episode of assignment runs starved of workers (#2040) ---
        from app.services.run_queue_starvation import check_queue_starvation

        await check_queue_starvation(db)  # never raises

        _tick_clock.mark("3a-bis")
        # --- 3a-bis. Fire due flow schedules ---
        # Same tick, same Redis window lock as assignment crons — firing flow
        # crons from the worker's adapter.periodic too would double-trigger.
        try:
            from app.services.flow_triggers import process_scheduled_flows

            triggered += await process_scheduled_flows(db)
        except Exception as e:
            logger.error("Scheduled flows error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("3a-quater")
        # --- 3a-quater. Tracked organizations' digests of held events (#9157 M6) ---
        try:
            if _ai_on:
                from app.services.organization_run_cap import dispatch_due_digests

                triggered += await dispatch_due_digests(db)
        except Exception as e:
            logger.error("Organization digest error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("3a-ter")
        # --- 3a-ter. Release the claims of runs that ended ---
        # A run that claimed an item and ended without settling it (cancelled,
        # failed, timed out, dead-lettered after a rollout) leaves it held:
        # released here, before the inbox dispatch below, so the same tick
        # hands it out again. Not gated on AI: it only frees rows.
        # Time-budgeted (#2582): a slow pass under pool pressure stops at its
        # budget and the rest waits for the next tick, its events published off
        # the tick, so no schedule waits behind it for more than one window.
        try:
            from app.services.work_item import release_ended_run_claims

            released = await _within_housekeeping_budget(
                "work item claim release",
                lambda deadline: release_ended_run_claims(deadline=deadline, background_events=True),
            )
            if released:
                logger.info("Released %d work item(s) held by runs that ended", released)
        except Exception as e:
            logger.error("Work-item claim release error: %s", format_exception_message(e), exc_info=True)
        # The shared-state leases those runs held, released the same way:
        # a lease whose holder ended is free without any agent-side sweep.
        try:
            from app.services.shared_state import release_ended_run_holds

            freed = await _within_housekeeping_budget(
                "shared-state hold release",
                lambda deadline: release_ended_run_holds(deadline=deadline),
            )
            if freed:
                logger.info("Released %d shared-state hold(s) of runs that ended", freed)
        except Exception as e:
            logger.error("Shared-state hold release error: %s", format_exception_message(e), exc_info=True)
        # CTEM cases (#11545): an accepted risk past its expiry returns to
        # needs attention. Rules only, no model call: not gated on AI.
        try:
            from app.services.ctem_cases_store import expire_acceptances

            expired = await expire_acceptances(db)
            if expired:
                await db.commit()
                logger.info("Returned %d CTEM case(s) whose accepted risk expired", expired)
        except Exception as e:
            logger.error("CTEM case expiry error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # CTEM signals still new past the grace (a record-time gate that failed, a
        # case-delete release whose background re-decision was lost): decided on
        # their own short cadence (#2530), batch after batch within a time budget,
        # so a backlog drains in minutes and none waits "for reconcile" for good.
        # Rules only, no model call; no case is opened here.
        try:
            from app.services import ctem_scope_gate_store

            if await ctem_scope_gate_store.recheck_new_due():
                undecided = await ctem_scope_gate_store.drain_new(db)
                if undecided:
                    logger.info("CTEM signals left new decided by the scheduled recheck: %s", undecided)
                # #2542: the unmatched signals whose remote software read failed, read again with backoff.
                retried = await ctem_scope_gate_store.recheck_lookup_failed(db)
                await db.commit()
                if retried:
                    logger.info("CTEM signals decided again after a failed software read: %s", retried)
        except Exception as e:
            logger.error("CTEM new-signal recheck error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # #2570: the verified signal-source deliveries the webhook accepted, recorded off
        # the request path, batch after batch within a time budget per tick (ctem_signal_inbox, #2584).
        try:
            from app.services import ctem_signal_inbox

            drained = await ctem_signal_inbox.drain_within()
            if any(drained.values()):
                logger.info("CTEM signal inbox drained: %s", drained)
        except Exception as e:
            logger.error("CTEM signal inbox drain error: %s", format_exception_message(e), exc_info=True)
        # #2570: the paced opening pass, once a minute, within the per-minute opening budget
        # (CTEM_CASE_OPEN_PER_MINUTE): a platform restarted in a storm opens its waiting
        # cases a budget at a time, by itself, one supervising run per case.
        try:
            from app.services import ctem_case_opening
            from app.services.ctem_signal_purge import intake_paused

            if not await intake_paused() and await ctem_case_opening.paced_pass_due():
                paced = await ctem_case_opening.paced_pass("cyber")
                if paced and (paced.get("opened") or paced.get("joined")):
                    logger.info("CTEM paced case opening: %s", paced)
        except Exception as e:
            logger.error("CTEM paced case opening error: %s", format_exception_message(e), exc_info=True)
        # CTEM case loop (#857): open the cases of confirmed signals still
        # waiting, escalate SLA breaches once. Rules only, no model call;
        # its own cadence (CTEM_CASE_SWEEP_SECONDS, default hourly).
        try:
            from app.services import ctem_case_loop
            from app.services.ctem_signal_purge import intake_paused

            # #2528: no recheck and no case sweep while a purge of every signal runs.
            if not await intake_paused() and await ctem_case_loop.sweep_due():
                # #1505: the unmatched signals a newer profile version may now hold, decided
                # again first, so the sweep below opens their cases grouped by case key.
                from app.services import ctem_scope_gate_store

                rechecked = await ctem_scope_gate_store.recheck_parked(db)
                await db.commit()
                if rechecked:
                    logger.info("CTEM unmatched signals re-checked after a profile change: %s", rechecked)
                pending = ctem_case_loop.PendingEvents()
                counts = await ctem_case_loop.sweep(db, events=pending)
                await db.commit()
                await ctem_case_loop.publish_pending(pending)
                # The open cases their owning fleet never ran (#1390): handed to it once.
                from app.services.ctem_case_routing import catch_up

                counts["caught_up"] = await catch_up()
                # Per-case decisions of a groupable kind asked of one person, folded into one card (#2451).
                from app.services import ctem_decision_groups

                counts["decisions_grouped"] = await ctem_decision_groups.consolidate(db)
                await db.commit()
                if any(counts.values()):
                    logger.info("CTEM case sweep: %s", counts)
        except Exception as e:
            logger.error("CTEM case sweep error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # CTEM cases kept current (dev-docs/ctem-cases.md s.4.3): the source recompute
        # on its own interval (profile cases_recompute.interval_minutes, default 15),
        # as the CTEM loop fleet's run-as users, else the recent viewers of Cases.
        try:
            from app.services import ctem_cases_refresh

            refresh_counts = await ctem_cases_refresh.scheduled_pass(db)
            if refresh_counts and refresh_counts.get("applied"):
                logger.info("CTEM cases scheduled recompute: %s", refresh_counts)
        except Exception as e:
            logger.error("CTEM cases scheduled recompute error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # CTEM attack surface pass (CTEM-26): the outside lens, opt-in in the profile,
        # its own cadence (attack_surface.cadence_hours, default daily). Rules only, no model call.
        try:
            from app.services import ctem_attack_surface
            from app.services.ctem_signal_purge import intake_paused

            # #2528: the pass keeps its cadence slot for after a purge of every signal.
            surface_counts = None if await intake_paused() else await ctem_attack_surface.scheduled_pass(db)
            if surface_counts:
                logger.info("CTEM attack surface pass: %s", surface_counts)
        except Exception as e:
            logger.error("CTEM attack surface pass error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # CTEM evidence health pass (CTEM-28): evidence decay (path 9) and blind spots
        # (path 12) from OpenCRQ, opt-in in the profile, its own cadence
        # (evidence_health.cadence_hours, default daily). Rules only, no model call.
        try:
            from app.services import ctem_evidence_health
            from app.services.ctem_signal_purge import intake_paused

            evidence_counts = None if await intake_paused() else await ctem_evidence_health.scheduled_pass(db)
            if evidence_counts:
                logger.info("CTEM evidence health pass: %s", evidence_counts)
        except Exception as e:
            logger.error("CTEM evidence health pass error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # CTEM ticketing sync (#917): create the ticket of a mobilized case,
        # pull the ticket status (ServiceNow's inbound), push the case state.
        # Rules only, no model call; its own cadence (CTEM_TICKETING_SYNC_SECONDS, default 5 min).
        try:
            from app.core.config import settings as app_settings
            from app.services import ctem_case_loop, ctem_ticketing

            if await ctem_ticketing.sync_due():
                pending = ctem_case_loop.PendingEvents()
                counts = await ctem_ticketing.sync_cases(db, events=pending, base_url=app_settings.frontend_url)
                await db.commit()
                await ctem_case_loop.publish_pending(pending)
                if any(v for k, v in counts.items() if k != "not_connected"):
                    logger.info("CTEM ticketing sync: %s", counts)
        except Exception as e:
            logger.error("CTEM ticketing sync error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # Assignments marked delete_assignment(after_run=true) whose runs
        # have all ended: deleted now, so a one-off retires itself.
        try:
            from app.services.assignment_retire import retire_assignments_after_runs

            retired = await retire_assignments_after_runs()
            if retired:
                logger.info("Deleted %d assignment(s) retired after their last run", retired)
        except Exception as e:
            logger.error("Assignment retirement error: %s", format_exception_message(e), exc_info=True)
        # A person's decision past its deadline expires and its run resumes
        # with the default option or no answer (human-gates.md section 7, F3).
        try:
            from app.services.decision_waits import expire_overdue_decisions

            expired = await expire_overdue_decisions()
            if expired:
                logger.info("Expired %d decision(s) past their deadline", expired)
        except Exception as e:
            logger.error("Decision expiry error: %s", format_exception_message(e), exc_info=True)
        # A claim that records no run (a chat turn, a claim made by an older
        # release) and that no live run of its agent holds: released after its
        # grace, so the inbox below dispatches it in the same tick.
        try:
            from app.services.work_item import release_orphaned_legacy_claims

            released = await release_orphaned_legacy_claims()
            if released:
                logger.info("Released %d work item(s) with no live holder", released)
        except Exception as e:
            logger.error("Work-item claim release error: %s", format_exception_message(e), exc_info=True)
        # An item blocked on other items whose blockers are all done:
        # back in the queue, so the inbox below dispatches it in the same tick.
        try:
            from app.services.work_item import release_satisfied_dependencies

            reopened = await release_satisfied_dependencies()
            if reopened:
                logger.info("Reopened %d work item(s) whose blockers are done", reopened)
        except Exception as e:
            logger.error("Work-item dependency release error: %s", format_exception_message(e), exc_info=True)
        # The polling fallback of the reference release: an item
        # blocked on a pull request or issue of a repository that sends no
        # webhook, or that closed before the item was blocked on it, is read
        # every few ticks; the delivery stays the primary path.
        try:
            from app.services.work_item_blocked_refs import (
                POLL_EVERY_TICKS,
                poll_blocked_on_refs,
                release_red_self_blocks,
                release_satisfied_blocks,
            )

            _blocked_refs_poll_tick_counter -= 1
            if _blocked_refs_poll_tick_counter <= 0:
                _blocked_refs_poll_tick_counter = POLL_EVERY_TICKS
                # An item waiting on the merge of its own red pull request never ends by itself.
                await release_red_self_blocks()
                polled = await poll_blocked_on_refs()
                if polled["released"]:
                    logger.info("Reopened %d work item(s) whose references closed", polled["released"])
                # Items waiting for an outbound A2A task that ended (#2265).
                from app.services.work_item_a2a_refs import release_ended_a2a_tasks

                await release_ended_a2a_tasks()
                # The class of defect: every reference met, the item still blocked.
                await release_satisfied_blocks()
                # Items waiting on each other through items or gated pull requests (#606).
                from app.services.work_item_wait_cycles import release_wait_cycles

                await release_wait_cycles()
                # An item parked on a pull request whose CI delivery never came (#10692).
                from app.services.work_item_pull_requests import poll_pull_request_ci

                await poll_pull_request_ci()
        except Exception as e:
            logger.error("Blocked references poll error: %s", format_exception_message(e), exc_info=True)
        # The notification an ended work item asks for: sent here by
        # the platform, whatever ended it (an agent, the API, the reaper, an
        # exhausted pick-up), with a bounded retry. Not gated on AI.
        try:
            from app.services.work_item_notify import deliver_pending_notifications

            notified = await deliver_pending_notifications()
            if notified:
                logger.info("Sent %d work item notification(s)", notified)
        except Exception as e:
            logger.error("Work-item notification error: %s", format_exception_message(e), exc_info=True)
        # The decision card of a work item waiting for a person, posted in the
        # Teams chat or channel its notify ask names, as its agent (#842 S9b).
        try:
            from app.services.decision_graph_cards import deliver_work_item_cards

            carded = await deliver_work_item_cards()
            if carded:
                logger.info("Sent %d decision card(s)", carded)
        except Exception as e:
            logger.error("Decision card error: %s", format_exception_message(e), exc_info=True)
        # The timers runs armed for a date (arm_timer): each due one raises
        # timer.due once, or is skipped when its unless condition is met.
        try:
            from app.services.platform_timers import fire_due_timers

            timers_fired = await fire_due_timers()
            if timers_fired:
                logger.info("Fired %d timer(s)", timers_fired)
        except Exception as e:
            logger.error("Timer firing error: %s", format_exception_message(e), exc_info=True)
        # The open work items that crossed their due date: work_item.past_due
        # raised once each (#1662), so a fleet reacts before its weekly check.
        try:
            from app.services.work_item_past_due import raise_past_due_items

            past_due = await raise_past_due_items()
            if past_due:
                logger.info("Raised work_item.past_due for %d item(s)", past_due)
        except Exception as e:
            logger.error("Past-due raise error: %s", format_exception_message(e), exc_info=True)
        # The decision gates nobody opened in time: the administrators told once (#2390).
        try:
            from app.services.work_item_decision_reach import raise_unseen_decisions

            unseen = await raise_unseen_decisions()
            if unseen:
                logger.info("Raised work_item.needs_human_unseen for %d item(s)", unseen)
        except Exception as e:
            logger.error("Unseen decision raise error: %s", format_exception_message(e), exc_info=True)
        # The CI completions a trigger filter could not judge (their enriched
        # facts timed out or the provider failed): read again, and triggered on
        # a match, so the last check of a head is never lost to a slow read.
        try:
            if _ai_on:
                from app.services.event_enrichment_retry import process_due_retries

                retried = await process_due_retries()
                if retried:
                    triggered += retried
                    logger.info("Enrichment retry: %d deferred delivery(ies) triggered", retried)
        except Exception as e:
            logger.error("Enrichment retry error: %s", format_exception_message(e), exc_info=True)
        # The CI heads a filter dropped while checks still ran: read again, so
        # a head whose last completion was lost (a rollout) still wakes once.
        try:
            if _ai_on:
                from app.services.event_enrichment_retry import process_watched_heads

                watched = await process_watched_heads()
                if watched:
                    triggered += watched
                    logger.info("Head watch: %d settled head(s) triggered", watched)
        except Exception as e:
            logger.error("Head watch error: %s", format_exception_message(e), exc_info=True)
        # The CI events a run stood down on because a live run held the lease
        # it reads: delivered again once that holder ended (#11988).
        try:
            if _ai_on:
                from app.services.event_enrichment_retry import process_held_events, process_redeliveries

                held = await process_held_events() + await process_redeliveries()
                if held:
                    triggered += held
                    logger.info("Lease hold: %d held event(s) delivered again", held)
        except Exception as e:
            logger.error("Lease hold error: %s", format_exception_message(e), exc_info=True)
        # The platform event deliveries that errored (the DB pool exhausted, a
        # dropped connection): delivered again with back-off, never lost (#2101).
        try:
            if _ai_on:
                from app.services.platform_event_retry import process_due as process_platform_event_retries

                redelivered = await process_platform_event_retries()
                if redelivered:
                    triggered += redelivered
                    logger.info("Platform event retry: %d delivery(ies) made again", redelivered)
        except Exception as e:
            logger.error("Platform event retry error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("3b")
        # --- 3b. Process work-item inbox assignments (autonomous pickup) ---
        # The consumer half of the work-item primitive: dispatch due,
        # owned work items to their agent's ``work_item``-trigger inbox
        # assignment. Every tick so pickup is timely.
        try:
            if _ai_on:
                triggered += await _process_work_item_assignments(db)
        except Exception as e:
            logger.error("Work-item assignments error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("3b-bis")
        # --- 3b-bis. Fire work-item flow triggers ---
        # The flow twin of 3b: `trigger.work_item` nodes on active flows drain
        # their owner agent's queue; the atomic claim keeps the two consumers
        # from ever double-running one item.
        try:
            from app.services.flow_triggers import process_work_item_flows

            triggered += await process_work_item_flows(db)
        except Exception as e:
            logger.error("Work-item flows error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("3c")
        # --- 3c. Dispatch due scheduled work items ---
        # One-off actions an agent scheduled from a chat: no assignment, no
        # flow. Not gated on the AI state here - the run re-checks the edition
        # and AI gate when it starts and blocks the item with the reason, and
        # an item nobody could dispatch in time is marked missed either way.
        try:
            from app.services.scheduled_work_item_runner import (
                dispatch_due_scheduled_work_items,
            )

            triggered += await dispatch_due_scheduled_work_items(db)
        except Exception as e:
            logger.error(
                "Scheduled work items error: %s", format_exception_message(e), exc_info=True
            )
            await db.rollback()

        _tick_clock.mark("4")
        # --- 4. Graph subscriptions, then poll integration events ---
        # A subscribed assignment is skipped by the poll below; one whose
        # subscription cannot be created, or lapses, is polled.
        try:
            from app.services.graph_event_subscriptions import (
                maintain_graph_event_subscriptions,
            )

            if _ai_on:
                await maintain_graph_event_subscriptions(db)
        except Exception as e:
            logger.error("Graph subscription pass error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("4-pre")
        # --- 4-pre. Triggers provisioned in source platforms for assignments ---
        try:
            from app.services.trigger_provisioning import maintain_trigger_provisioning

            if _ai_on:
                await maintain_trigger_provisioning(db)
        except Exception as e:
            logger.error("Trigger provisioning pass error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        try:
            from app.services.event_poller import poll_integration_events

            polled = await poll_integration_events(db) if _ai_on else 0
            if polled:
                logger.info(
                    "Integration event poller triggered %d assignment(s)", polled
                )
        except Exception as e:
            logger.error("Event poller error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("4-bis")
        # --- 4-bis. Poll integration events for flows ---
        try:
            from app.services.flow_triggers import poll_flow_integration_events

            flow_polled = await poll_flow_integration_events(db)
            if flow_polled:
                logger.info(
                    "Integration event poller triggered %d flow run(s)",
                    flow_polled,
                )
        except Exception as e:
            logger.error("Flow event poller error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("5")
        # --- 5. Process scheduled knowledge base syncs ---
        try:
            from app.services.kb_sync_scheduler import (
                process_scheduled_kb_syncs,
                reset_stale_syncs,
            )

            stale = await reset_stale_syncs(db)
            if stale:
                logger.info("Reset %d stale KB sync source(s)", stale)
                await db.commit()

            synced = await process_scheduled_kb_syncs(db) if _ai_on else 0
            if synced:
                logger.info("KB sync scheduler triggered %d sync(s)", synced)
        except Exception as e:
            logger.error("KB sync scheduler error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("conversation-search")
        # --- Workspace conversation search ---
        # Reclaims the documents of conversations that left their workspace
        # whatever AI says, and queues idle conversations only while it is on.
        try:
            from app.services.workspace_conversation_index import run_tick

            queued, reclaimed = await run_tick(db, pick=_ai_on)
            if queued or reclaimed:
                logger.info(
                    "Workspace conversation index: %d queued, %d reclaimed",
                    queued,
                    reclaimed,
                )
        except Exception as e:
            logger.error(
                "Workspace conversation index error: %s", format_exception_message(e), exc_info=True
            )
            await db.rollback()

        _tick_clock.mark("rule-syncs")
        # --- Process scheduled Nova rule syncs (pull .nov rule repos) ---
        try:
            from app.services.nova_rule_sync import process_scheduled_nova_syncs

            nova_synced = await process_scheduled_nova_syncs(db) if _ai_on else 0
            if nova_synced:
                logger.info("Nova rule sync triggered %d sync(s)", nova_synced)
        except Exception as e:
            logger.error(
                "Nova rule sync error: %s", format_exception_message(e), exc_info=True
            )
            await db.rollback()

        _tick_clock.mark("custom-tool-syncs")
        # --- Scheduled custom tool repository syncs ---
        # No AI gate: a custom tool is a sandboxed script and its tests call no
        # model; the licence and the custom-tools switch are checked inside.
        try:
            from app.services.custom_tool_sources import process_scheduled_syncs

            tool_syncs = await process_scheduled_syncs(db)
            if tool_syncs:
                logger.info("Custom tool source sync ran %d source(s)", tool_syncs)
        except Exception as e:
            logger.error(
                "Custom tool source sync error: %s", format_exception_message(e), exc_info=True
            )
            await db.rollback()

        _tick_clock.mark("5b-0")
        # --- 5b-0. Embedding-readiness gate for the KB recovery sweeps ---
        # All three sweeps below exist to get documents *embedded*. With no
        # embedding-capable provider configured (a normal state for an install
        # that never enabled RAG) none of them can accomplish anything: each
        # would claim a batch with ``FOR UPDATE SKIP LOCKED``, discover the
        # missing provider, log an identical line and roll the claim back — on
        # every 60 s tick, forever.
        #
        # So probe once here, *before* any row is touched, and skip the whole
        # block when embedding is unavailable. ``resolve_embedding_recovery_state``
        # owns the (throttled) operator-facing log line; any verdict that lets
        # the sweeps run is passed down as ``embedding_ready=True`` so they
        # don't repeat the provider-config lookup three more times (see the
        # comment on the guard below for the ``None`` case).
        # A broken probe is logged, throttled, by the helper; an unimportable
        # ``kb_helpers`` reads as ``False``, since the sweeps import from it.
        embedding_ready = await _probe_embedding_readiness()

        _tick_clock.mark("5a-2")
        # --- 5a-2. Settle workset items behind retries nobody will run ---
        # Outside the gate below on purpose: a missing provider is exactly
        # when failed embeddings pile up, and an item left `pending` for a
        # retry that will never run (retries off, the cap reached, or a
        # schedule the sweeper already cleared) must still settle.
        # It calls no model - it only fails items.
        if _ai_on:
            try:
                from app.services.workset_processing import (
                    settle_items_behind_abandoned_retries,
                )

                await settle_items_behind_abandoned_retries()
            except Exception as e:
                logger.error(
                    "Workset abandoned-retry settlement error: %s",
                    format_exception_message(e),
                    exc_info=True,
                )

        if embedding_ready is not False:
            # Reaching here means the verdict was ``True`` (configured) or
            # ``None`` (the probe itself failed). Both proceed, and both are
            # forwarded to the sweeps as ``True``: the gate has already emitted
            # its single throttled line, whereas passing ``None`` down would
            # make each sweep re-run the same probe and re-log — the exact
            # cost/log amplification ``enqueue_documents`` avoids one level
            # lower by the same convention.
            #
            # --- 5b. Recover stuck KB document embeddings ---
            # Defense-in-depth for the "document sat in ``pending`` /
            # ``processing`` forever" failure modes:
            #
            # * KB sync enqueued the SAQ job but Redis hiccuped and the
            #   enqueue was silently swallowed by the bare ``except`` in
            #   ``enqueue_document_processing``.
            # * Worker picked the job up, ran ``FOR UPDATE SKIP LOCKED``,
            #   found the row locked, returned cleanly without re-enqueueing
            #   (as an older worker did).
            # * Worker process died (OOM kill, pod restart) between flipping
            #   the doc to ``processing`` and the final ``ready``/``error``
            #   commit, leaving an orphaned in-flight document.
            #
            # The sweeper detects each of those by ``updated_at`` and
            # re-enqueues, with separate generous thresholds for ``pending``
            # vs. ``processing`` so we never race a legitimately-running
            # embedding job.
            try:
                from app.services.kb_helpers import recover_stuck_documents

                recovered = await recover_stuck_documents(db, embedding_ready=True) if _ai_on else 0
                if recovered:
                    logger.info(
                        "Recovered %d stuck KB document(s) "
                        "(re-enqueued for embedding)",
                        recovered,
                    )
            except Exception as e:
                logger.error("Stuck KB document recovery error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()

            # --- 5b-2. Auto-retry failed KB document embeddings ---
            # Self-healing for ``error`` docs whose failure was retryable
            # (provider down/misconfigured, transient network error): the
            # document processor schedules ``embedding_next_retry_at`` with
            # exponential backoff + jitter, and this sweep re-enqueues the
            # doc once the retry is due.  This sweep (and 5b-3 below) was
            # implemented and documented as autonomous but never invoked
            # from the tick — failed docs sat in ``error`` forever unless
            # manually reprocessed.
            try:
                from app.services.kb_helpers import recover_failed_documents

                retried = await recover_failed_documents(db, embedding_ready=True) if _ai_on else 0
                if retried:
                    logger.info(
                        "Auto-retried %d failed KB document(s) "
                        "(re-enqueued for embedding)",
                        retried,
                    )
            except Exception as e:
                logger.error("Failed KB document retry error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()

            # --- 5b-3. Re-embed stale KB documents ---
            # Self-healing for ``ready`` docs whose stored embeddings no
            # longer match the platform embedding target (admin changed the
            # provider / model / dimensions): purge the stale vectors, reset
            # to ``pending`` and re-enqueue — what the dashboard "Re-embed
            # all" button does, but autonomous and batch-capped.
            try:
                from app.services.kb_helpers import recover_stale_documents

                remediated = await recover_stale_documents(db, embedding_ready=True) if _ai_on else 0
                if remediated:
                    logger.info(
                        "Re-embedding %d stale KB document(s) "
                        "(embedding target changed)",
                        remediated,
                    )
            except Exception as e:
                logger.error("Stale KB document remediation error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()

        _tick_clock.mark("5b-4")
        # --- 5b-4. Clear the provider reference of chunks a deleted config embedded ---
        # The provider deletion no longer cascades onto ``embeddings`` inside
        # its locked transaction; this tick nulls the id afterwards, in
        # bounded batches. Pure housekeeping: not gated on AI or embedding.
        try:
            from app.services.llm_provider_deletion import (
                clear_deleted_embedding_provider_references,
            )

            await clear_deleted_embedding_provider_references(db)
        except Exception as e:
            logger.error(
                "Deleted-provider embedding reference cleanup error: %s",
                format_exception_message(e),
                exc_info=True,
            )
            await db.rollback()

        _tick_clock.mark("5c-0")
        # --- 5c-0. Orchestration recovery backstop (worker-down safety net) ---
        # The worker reconciler sweeps deadlines + reclaims crashed leases every
        # cycle. If the worker is DOWN, those sweeps never run and every run/task
        # backed by a live execution hangs on ``pending`` / ``running`` forever
        # (5c below deliberately DEFERS to the reconciler for live executions).
        # Running the same all-adapter recovery here, in the always-on web
        # scheduler tick, guarantees past-deadline executions are dead-lettered
        # (their backing run/task failed via on_dead_letter) and crashed leases
        # reclaimed within ~60s even with no worker at all. FOR UPDATE SKIP
        # LOCKED makes it safe to run concurrently with a healthy reconciler.
        # Runs BEFORE 5c so the freshly dead-lettered executions are reconciled
        # onto their runs in the same tick.
        try:
            recovered = await _recover_stale_orchestration_executions()
            if recovered:
                logger.info(
                    "Orchestration backstop: dead-lettered %d stale execution(s) "
                    "(worker-down safety net)",
                    recovered,
                )
        except Exception as e:
            logger.error("Orchestration backstop error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("5c-0b")
        # --- 5c-0b. Video Studio renders no worker resumed ---
        # A render a stopped worker handed back (or one taken back from a worker
        # gone) waits first in the queue; one no worker took within the rolling
        # restart its deployment allows, while nothing held a slot of its lane,
        # is failed saying so - never left reading "running". Here because this
        # tick runs with no worker at all.
        try:
            from app.services.video_studio.jobs import fail_unresumed_renders

            unresumed = await fail_unresumed_renders()
            if unresumed:
                logger.warning("Video Studio: failed %d render(s) no worker resumed within their wait", unresumed)
        except Exception as e:
            logger.error("Video Studio unresumed render sweep error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("5c-0b1")
        # --- 5c-0b1. Video Studio renders left running with no lease ---
        # A relaunch that lost its lease record leaves the render's execution
        # running with no holder: no lease expires and no worker is named gone.
        # Quiet one lease period, it is taken back first in its lane (frames
        # kept), or dead-lettered with no attempt left - never orphaned.
        try:
            from app.services.video_studio.jobs import reclaim_leaseless_renders

            leaseless = await reclaim_leaseless_renders()
            if leaseless:
                logger.warning("Video Studio: took back %d render(s) left running with no lease", leaseless)
        except Exception as e:
            logger.error("Video Studio lease-less render sweep error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("5c-0b2")
        # --- 5c-0b2. Video Studio renders nothing runs any more ---
        # A render row still queued or running whose execution ended, is gone or
        # lost its lease, quiet for 15 min, is failed with the reason, so a fresh
        # render of its revision is never answered "already being rendered".
        try:
            from app.services.video_studio.jobs import fail_orphaned_renders

            orphaned = await fail_orphaned_renders()
            if orphaned:
                logger.warning("Video Studio: failed %d render(s) nothing ran any more", orphaned)
        except Exception as e:
            logger.error("Video Studio orphaned render sweep error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("5c-0c")
        # --- 5c-0c. Video Studio videos indexed by an older rule ---
        # A deletion's refusal and a shot's file cleanup read the media index as
        # stored: a video an older rule indexed is indexed again within a tick of
        # a deploy, not at the hourly media retention. One query once none is left.
        try:
            from app.services.video_studio.retention import index_stale_refs

            async with async_session() as index_db:
                reindexed = await index_stale_refs(index_db)
                await index_db.commit()
            if reindexed:
                logger.info("Video Studio: indexed the media of %d video(s) again", reindexed)
        except Exception as e:
            logger.error("Video Studio media index error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("5c-1")
        # --- 5c-1. Recover orphaned background (sub-agent) tasks ---
        # Companion to 5c for the decoupled ``sub_agent_tasks`` table. A task
        # whose execution reached a terminal state via ``engine.fail()`` (a
        # reconciler timeout / adapter crash / non-retryable failure) bypasses
        # the adapter ``on_dead_letter`` cleanup, so the task is left stuck on
        # ``pending`` / ``running`` — visibly "killed but never died" — and keeps
        # inflating the per-scope fan-out counter that gates new delegations.
        # Fail any such task (and its linked run + delegation) whose backing
        # execution is terminal or gone. Own session so a sweep error can't
        # poison the surrounding tick transaction.
        try:
            from app.services.sub_agent import recover_orphaned_sub_agent_tasks

            async with async_session() as _orphan_db:
                orphaned_tasks = await recover_orphaned_sub_agent_tasks(_orphan_db)
                await _orphan_db.commit()
            if orphaned_tasks:
                logger.info(
                    "Failed %d orphaned background task(s) with no live execution",
                    orphaned_tasks,
                )
                # The work items they held go back to the queue in this same
                # pass, not a tick later: each release names the task.
                from app.services.work_item import release_ended_run_claims

                released = await release_ended_run_claims()
                if released:
                    logger.info(
                        "Released %d work item(s) held by the orphaned background task(s) just failed",
                        released,
                    )
        except Exception as e:
            logger.error(
                "Orphaned background task recovery error: %s", format_exception_message(e), exc_info=True
            )

        _tick_clock.mark("5c-2")
        # --- 5c-2. Runner presence sweep ---
        # Declares offline (and fires "Runner offline" for) the runners whose
        # heartbeat went stale. A closed socket no longer does - every deploy
        # closes them all. Own sessions inside the sweep.
        if settings.runners_enabled:
            try:
                from app.services.runner_transport import sweep_stale_runners

                gone = await sweep_stale_runners()
                if gone:
                    logger.info("Marked %d runner(s) offline (heartbeat stale)", gone)
            except Exception as e:
                logger.error(
                    "Runner presence sweep error: %s", format_exception_message(e), exc_info=True
                )
            # Commands whose waiter gave up while a runner held them are never
            # redelivered; once their lease runs out with no result they are
            # finished here.
            try:
                from app.services.runner_transport import expire_given_up_commands

                expired = await expire_given_up_commands()
                if expired:
                    logger.info("Finished %d given-up runner command(s) past their lease", expired)
            except Exception as e:
                logger.error(
                    "Given-up runner command sweep error: %s",
                    format_exception_message(e),
                    exc_info=True,
                )

        _tick_clock.mark("5c")
        # --- 5c. Recover stale/orphaned assignment runs ---
        # Safety net for assignment runs the orchestration engine can't recover
        # because they have no live execution row: in-process chat-handover /
        # consult / transfer tracing runs (assignment_id IS NULL), legacy
        # pre-orchestration runs, and terminal-state sync-hook desyncs (which
        # are reconciled to the execution's actual outcome). Without
        # this they sit in ``running`` / ``pending`` forever after a restart or
        # crash — the symptom the orchestration migration's removal of startup
        # recovery introduced. Runs with a live execution are left to the
        # worker reconciler; HITL ``awaiting_human`` runs are never touched.
        try:
            stale_runs = await _cleanup_stale_assignment_runs(db)
            if stale_runs:
                logger.info(
                    "Finalized %d stale/orphaned assignment run(s) with no "
                    "live orchestration execution",
                    stale_runs,
                )
                # Their work item claims are released in the same pass.
                from app.services.work_item import release_ended_run_claims

                released = await release_ended_run_claims()
                if released:
                    logger.info("Released %d work item(s) held by the stale run(s) just finalized", released)
        except Exception as e:
            logger.error("Stale assignment run cleanup error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("5c-bis")
        # --- 5c-bis. Handed-back runs no worker resumed ---
        # A run handed back by a stopping worker stays ``running`` with its
        # execution live, so the sweep above skips it: nudged after
        # ``handed_back_resume_seconds``, failed after
        # ``handed_back_orphan_seconds`` and its claims released here.
        try:
            from app.services.handed_back_sweep import fail_stranded_hand_backs

            nudged, stranded = await fail_stranded_hand_backs(db)
            if nudged:
                logger.info("Made %d handed-back assignment run(s) due again", nudged)
            if stranded:
                from app.services.work_item import release_ended_run_claims

                released = await release_ended_run_claims()
                logger.info(
                    "Failed %d handed-back assignment run(s) never resumed; released %d work item(s)",
                    stranded,
                    released,
                )
        except Exception as e:
            logger.error("Handed-back run sweep error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("5d")
        # --- 5d. Delete expired KB export packages ---
        try:
            expired_exports = await _cleanup_expired_kb_export_packages(db)
            if expired_exports:
                logger.info("Deleted %d expired KB export package(s)", expired_exports)
        except Exception as e:
            logger.error("KB export package cleanup error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("5e")
        # --- 5e. Periodic model-prices git sync (optional) ---
        if settings.model_prices_git_sync_enabled:
            _prices_claim = await wall_clock_schedule.claim(
                wall_clock_schedule.model_prices_git_sync(settings.model_prices_git_sync_interval_minutes)
            )
            if _prices_claim is not None:
                try:
                    from app.services.tracing.price_loader import (
                        pull_model_prices_git_source,
                        sync_model_prices,
                    )

                    updated, payload = await pull_model_prices_git_source()
                    synced_count, skipped_custom_count = await sync_model_prices(
                        db,
                        payload=payload,
                    )
                    if updated:
                        logger.info(
                            "Model prices git sync pulled updates and synced %d row(s); "
                            "skipped %d custom override(s)",
                            synced_count,
                            skipped_custom_count,
                        )
                    else:
                        logger.debug(
                            "Model prices git sync found no upstream changes; "
                            "refreshed %d row(s)",
                            synced_count,
                        )
                except Exception as e:
                    logger.error("Model prices git sync error: %s", format_exception_message(e), exc_info=True)
                    await db.rollback()
                # A failed pull waits for the next interval, as it did before.
                await wall_clock_schedule.complete(_prices_claim)

        await _run_trace_storage_maintenance(db)

        _tick_clock.mark("6b")
        # --- 6b. Graph subscriptions of private Teams channels (hourly + first tick) ---
        await _run_teams_subscription_maintenance()

        _tick_clock.mark("6c")
        # --- 6c. Agent accounts: subscriptions, chat polls, grace periods ---
        await _run_agent_account_maintenance()

        _tick_clock.mark("6")
        # --- 6. Periodic memory maintenance and retention sweeps (once per hour) ---
        # Due when the last COMPLETED pass, in Redis, is an hour old; one
        # process runs it under a cluster-wide lock, and a pass a deploy cut
        # is run again once its lock expires (#768).
        _maintenance_claim = await wall_clock_schedule.claim(wall_clock_schedule.HOURLY_MAINTENANCE)
        if _maintenance_claim is not None:
            try:
                from app.services.persistent_memory import run_memory_maintenance

                # A ``None`` fallback here subscripted below and raised a
                # ``TypeError`` into the block's ``except``, logging a
                # traceback and rolling back once an hour on every AI-disabled
                # platform. Every other gated call in this tick returns a zero.
                maint = (
                    await run_memory_maintenance(db)
                    if _ai_on
                    else {"pruned": 0, "evicted": 0, "consolidated": 0}
                )
                total = (
                    maint["pruned"]
                    + maint["evicted"]
                    + maint["consolidated"]
                    + maint.get("reembedded", 0)
                )
                if total:
                    logger.info(
                        "Memory maintenance: %d pruned, %d evicted, "
                        "%d consolidated, %d re-embedded",
                        maint["pruned"],
                        maint["evicted"],
                        maint["consolidated"],
                        maint.get("reembedded", 0),
                    )
            except Exception as e:
                logger.error("Memory maintenance error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()
            # Not AI work: the retention of consolidated-away memory (#8258)
            # holds whether AI is on or not.
            try:
                from app.services.health.memory_history import purge_expired_consolidations

                purged = await purge_expired_consolidations(db)
                await db.commit()
                if purged["rows"]:
                    logger.info(
                        "Memory consolidation purge: %d rows, %d entries", purged["rows"], purged["entries"]
                    )
            except Exception as e:
                logger.error("Memory consolidation purge error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()

            # --- 6a. The cross-agent user memory sweep: splits free-text
            # profiles with the consolidation model and drops expired notes. A
            # Redis lease decides; it runs in the background on sessions of its own.
            if _ai_on:
                try:
                    from app.services.user_memory_maintenance import sweep_user_memories_if_due

                    await sweep_user_memories_if_due()
                except Exception as e:
                    logger.error("User memory sweep error: %s", format_exception_message(e), exc_info=True)

            # --- 6c. Purge resolved shared state past retention (once per hour) ---
            try:
                purged_states = await _cleanup_resolved_shared_states(db)
                if purged_states:
                    logger.info(
                        "Purged %d resolved shared-state entr%s past retention",
                        purged_states,
                        "y" if purged_states == 1 else "ies",
                    )
                purged_items = await _cleanup_archived_shared_state_items()
                if purged_items:
                    logger.info("Purged %d archived shared-state item(s) past retention", purged_items)
                # Shared-state hygiene (#11696): entries whose linked work item
                # ended or whose date passed are resolved, oversize ones
                # compacted. Rules only, no model call.
                from app.services.shared_state import sweep_shared_state_lifecycle

                hygiene = await sweep_shared_state_lifecycle()
                if any(hygiene.values()):
                    logger.info("Shared-state hygiene: %s", hygiene)
            except Exception as e:
                logger.error(
                    "Shared-state retention cleanup error: %s", format_exception_message(e), exc_info=True
                )
                await db.rollback()

            # --- 6d. Reap the work-item queue (once per hour) ---
            # Self-cleaning backstop: auto-cancel abandoned open items
            # nobody picked up and purge terminal items past retention, so the
            # human never has to babysit stale work items.
            try:
                reaped = await _cleanup_work_items(db)
                if reaped["cancelled"] or reaped["purged"]:
                    logger.info(
                        "Work-item retention sweep: auto-cancelled %d abandoned, "
                        "purged %d terminal",
                        reaped["cancelled"],
                        reaped["purged"],
                    )
            except Exception as e:
                logger.error(
                    "Work-item retention cleanup error: %s", format_exception_message(e), exc_info=True
                )
                await db.rollback()

            # --- 6e. Purge worksets past their retention (once per hour) ---
            try:
                swept = await _cleanup_worksets(db)
                if swept["resolved"] or swept["expired"]:
                    logger.info(
                        "Workset retention sweep: purged %d resolved, %d expired",
                        swept["resolved"],
                        swept["expired"],
                    )
            except Exception as e:
                logger.error(
                    "Workset retention cleanup error: %s", format_exception_message(e), exc_info=True
                )
                await db.rollback()

            # --- 6f. Purge flow events past retention (once per hour) ---
            try:
                purged_events = await _cleanup_flow_events(db)
                if purged_events:
                    logger.info(
                        "Purged %d flow event%s past retention",
                        purged_events,
                        "" if purged_events == 1 else "s",
                    )
            except Exception as e:
                logger.error(
                    "Flow-event retention cleanup error: %s", format_exception_message(e), exc_info=True
                )
                await db.rollback()

            # --- 6f2. Delete Video Studio media past retention (once per hour) ---
            try:
                swept_media = await _cleanup_video_media(db)
                if swept_media["expired"]:
                    logger.info(
                        "Video Studio retention: deleted the media of %d recording%s and AI shot%s",
                        swept_media["expired"],
                        "" if swept_media["expired"] == 1 else "s",
                        "" if swept_media["expired"] == 1 else "s",
                    )
                if swept_media["purged"]:
                    logger.info("Video Studio retention: deleted %d failed recordings and AI shots", swept_media["purged"])
            except Exception as e:
                logger.error("Video Studio retention error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()
            try:
                swept_renders = await _cleanup_video_renders(db)
                if swept_renders:
                    logger.info(
                        "Video Studio retention: deleted %d render%s",
                        swept_renders,
                        "" if swept_renders == 1 else "s",
                    )
            except Exception as e:
                logger.error("Video Studio render retention error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()
            try:
                swept_stills = await _cleanup_figure_stills()
                if swept_stills:
                    logger.info(
                        "Video Studio retention: deleted %d figure still%s",
                        swept_stills,
                        "" if swept_stills == 1 else "s",
                    )
            except Exception as e:
                logger.error("Video Studio figure still retention error: %s", format_exception_message(e), exc_info=True)
            try:
                swept_figure_sources = await _cleanup_kb_figure_sources(db)
                if swept_figure_sources:
                    logger.info(
                        "Knowledge base retention: deleted %d PDF%s kept for a figure pass",
                        swept_figure_sources,
                        "" if swept_figure_sources == 1 else "s",
                    )
            except Exception as e:
                logger.error("Knowledge base figure source retention error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()
            await _run_retention_pass(db)
            try:
                swept_work = await _cleanup_stored_video_work()
                if swept_work:
                    logger.info(
                        "Video Studio retention: deleted %d stored figure drawing%s and render segment%s of work that is over",
                        swept_work,
                        "" if swept_work == 1 else "s",
                        "" if swept_work == 1 else "s",
                    )
            except Exception as e:
                logger.error("Video Studio stored work retention error: %s", format_exception_message(e), exc_info=True)

            # --- 6g. Purge ratings of deleted runs / tasks (once per hour) ---
            try:
                purged_annotations = await _cleanup_orphan_annotations(db)
                if purged_annotations:
                    logger.info(
                        "Purged %d annotation%s whose target was deleted",
                        purged_annotations,
                        "" if purged_annotations == 1 else "s",
                    )
            except Exception as e:
                logger.error(
                    "Orphan annotation cleanup error: %s", format_exception_message(e), exc_info=True
                )
                await db.rollback()
            # Every step above caught its own failure: the pass is complete.
            await wall_clock_schedule.complete(_maintenance_claim)
        elif await wall_clock_schedule.backlog_pending(wall_clock_schedule.RETENTION_DRAIN):
            # --- 6h. A retention pass stopped at its cap: drain on the next ticks ---
            _drain_claim = await wall_clock_schedule.claim(wall_clock_schedule.RETENTION_DRAIN)
            if _drain_claim is not None:
                await _run_retention_pass(db)
                await wall_clock_schedule.complete(_drain_claim)

        _tick_clock.mark("7")
        # --- 7. Periodic skill auto-refresh (check once per hour) ---
        _skills_claim = await wall_clock_schedule.claim(wall_clock_schedule.SKILL_REFRESH)
        if _skills_claim is not None:
            try:
                refreshed = await _refresh_stale_skills(db)
                if refreshed:
                    logger.info("Skill auto-refresh: updated %d skill(s)", refreshed)
            except Exception as e:
                logger.error("Skill auto-refresh error: %s", format_exception_message(e), exc_info=True)
                await db.rollback()
            await wall_clock_schedule.complete(_skills_claim)

        _tick_clock.mark("8")
        # --- 8. Flush Redis-buffered API key usage stats (every 5 min) ---
        global _apikey_stats_tick_counter
        _apikey_stats_tick_counter += 1
        if _apikey_stats_tick_counter >= _APIKEY_STATS_FLUSH_INTERVAL:
            _apikey_stats_tick_counter = 0
            try:
                from app.api.v1.llm_proxy import flush_api_key_stats

                await flush_api_key_stats()
            except Exception as e:
                logger.error("API key stats flush error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("9")
        # --- 9. Process scheduled evaluations ---
        # (Evaluation episode callback timeouts are handled by the
        # EvaluationAdapter.periodic() hook in the orchestration reconciler.)
        try:
            eval_triggered = await _run_scheduled_evaluations(db, ai_on=_ai_on)
            if eval_triggered:
                logger.info(
                    "Evaluation scheduler triggered %d evaluation(s)", eval_triggered
                )
        except Exception as e:
            logger.error("Evaluation scheduler error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("9a")
        # --- 9a. Scheduled syncs of the organization profile (#11913) ---
        # No model call: platform reads mapped to profile rows, so it runs with
        # AI off too, like the import it repeats.
        try:
            from app.services.organization_context_sync import run_due_syncs

            synced = await run_due_syncs(db)
            if synced:
                logger.info("Organization context: %d scheduled sync(s) ran", synced)
        except Exception as e:
            logger.error("Organization sync scheduler error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("9a'")
        # --- 9a'. The Intelligence policy's PIRs, read from every connected OpenCTI (#2435) ---
        # No model call, no configuration: at most once an hour per process.
        try:
            from app.services.organization_pirs import refresh_if_due

            await refresh_if_due(db)
        except Exception as e:
            logger.error("Intelligence policy refresh error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

        _tick_clock.mark("9b")
        # --- 9b. Settle applied improvement proposals (#1044, #8256) ---
        # No model call: a verdict and, on a regression, a rollback. Runs with
        # AI, evaluation or Platform Health off too (#5503), so a change applied
        # before never stays unjudged.
        try:
            from app.services.improvement_proposals import settle_open_proposals

            settled_proposals = await settle_open_proposals()
            if settled_proposals:
                logger.info("Settled %d improvement proposal(s)", settled_proposals)
        except Exception as e:
            logger.error("Improvement proposal settling error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("9c")
        # --- 9c. Purge expired Platform Health sandbox copies (#8255) ---
        # No model call; one indexed read when nothing has expired.
        try:
            from app.services.health.sandbox.copies import purge_expired_sandbox_copies_tick

            await purge_expired_sandbox_copies_tick()
        except Exception as e:
            logger.error("Sandbox copy purge error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("9b9")
        # --- 9b9. Dashboard data sources refreshed on the server ---
        # No model call (a source that needs one is never refreshed on the server);
        # one indexed read when nothing is due, then a bounded batch, each source read
        # as its publisher (``dashboard_refresh``).
        try:
            from app.core.feature_flags import is_feature_enabled

            if is_feature_enabled("DASHBOARD_STUDIO"):
                from app.services.dashboard_refresh import refresh_due

                await refresh_due(async_session)
        except Exception as e:
            logger.error("Dashboard source refresh error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("9c0")
        # --- 9c0. Component versions of the connected platforms ---
        # No model call; nothing is read while no assignment subscribes to
        # integration.version.changed, one read per platform every 5 minutes.
        try:
            from app.services.integration_component_versions import component_versions_tick

            await component_versions_tick()
        except Exception as e:
            logger.error("Component versions tick error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("9c")
        # --- 9c. Platform Health cycles, schedules and signals (#8261) ---
        # Settling a cycle (and its digest) runs whatever the switches; starting
        # work needs agentic AI and Platform Health switched on (``cycles.health_tick``).
        # Signals make no model call.
        try:
            from app.services.health.cycles import health_tick

            await health_tick(ai_on=_ai_on)
        except Exception as e:
            logger.error("Platform Health tick error: %s", format_exception_message(e), exc_info=True)
        _tick_clock.mark("9c bis")
        # --- 9c bis. Platform Health checks (#101 S2, dev-docs/platform-health.md 3) ---
        # Deterministic, no model call, never metered: runs whatever the AI
        # switch, so health stays visible with AI off.
        try:
            from app.services.health.engine import health_pass_tick

            await health_pass_tick()
        except Exception as e:
            logger.error("Platform Health pass error: %s", format_exception_message(e), exc_info=True)
        _tick_clock.mark("9c ter")
        # --- 9c ter. Platform Health analysis (#101 S9, dev-docs/platform-health.md 6) ---
        # One model call per module whose findings changed, at most once a day,
        # within the analysis ceiling; nothing with AI off or analysis off.
        try:
            from app.services.health.analysis import analysis_tick

            await analysis_tick(ai_on=_ai_on)
        except Exception as e:
            logger.error("Platform Health analysis error: %s", format_exception_message(e), exc_info=True)
        _tick_clock.mark("9d")
        # --- 9d. Advance flow shadows, switch-overs and their watch (#8260) ---
        # The watch and its rollback need no model and run whatever the flags,
        # so a switch-over applied before is never left unwatched; pairing new
        # shadow runs waits on Platform Health's own gates (flow_shadow.pair_new_runs).
        try:
            from app.services.health.flow_shadow import flow_shadow_tick

            shadows = await flow_shadow_tick()
            if shadows.failed:
                logger.warning("Flow shadows: %d advanced, %d failed", shadows.advanced, shadows.failed)
        except Exception as e:
            logger.error("Flow shadow step error: %s", format_exception_message(e), exc_info=True)

        _tick_clock.mark("10")
        # --- 10. Flush Redis-buffered usage counters (every 5 min) ---
        # Quota usage and agentic-execution counts are independent sinks, so
        # each gets its own guard: a failure in one must not skip the other.
        global _quota_flush_tick_counter
        _quota_flush_tick_counter += 1
        if _quota_flush_tick_counter >= _QUOTA_FLUSH_INTERVAL:
            _quota_flush_tick_counter = 0
            try:
                from app.services.quota import flush_quota_usage

                await flush_quota_usage()
            except Exception as e:
                logger.error("Quota usage flush error: %s", format_exception_message(e), exc_info=True)
            try:
                from app.services.agentic_executions import (
                    flush_agentic_executions,
                )

                await flush_agentic_executions()
            except Exception as e:
                logger.error(
                    "Agentic execution flush error: %s",
                    format_exception_message(e),
                    exc_info=True,
                )

        _tick_clock.mark("11")
        # --- 11. AI adoption metrics sync (every hour, every agentic mode) ---
        # AI adoption is generic AI (dev-docs/mode-split.md, S2): it runs in
        # Copilot, dev and XTM One mode alike. Never in ``ai_disabled``: this
        # makes outbound calls to external AI vendors, on the one mode whose
        # purpose is that none happen, even after AI is switched back on.
        from app.core.config import settings as _app_settings
        from app.services.ai_state import AI_DISABLED_MODE

        if _app_settings.platform_mode != AI_DISABLED_MODE and _ai_on:
            _metrics_claim = await wall_clock_schedule.claim(wall_clock_schedule.AI_METRICS_SYNC)
            if _metrics_claim is not None:
                try:
                    # The platform's own agentic usage only (#11380): no
                    # external vendor is called any more.
                    from app.services.ai_metrics_collector import collect_all_metrics

                    summary = await collect_all_metrics()
                    logger.info("AI metrics sync completed: %s", summary)
                except Exception as e:
                    logger.error("AI metrics sync error: %s", format_exception_message(e), exc_info=True)
                await wall_clock_schedule.complete(_metrics_claim)

        # --- 11. CTEM metrics: intentionally NOT scheduled (lazy-by-design). CTEM
        # posture is collected on demand on dashboard load and served
        # stale-while-revalidate (cached + snapshotted per user/vertical/day, ~24h
        # soft TTL), so a user who never opens the dashboard needs no posture and
        # there is nothing to pre-collect here. This is a product choice, NOT a
        # token-minting limitation: a background job could mint each user's own
        # Ed25519 JWT via ctem_metrics._user_creds (the same per-user minting
        # assignment fan-out uses) if we ever wanted scheduled collection. See
        # services/ctem_metrics_collector.build_posture_for_user. ---

        _tick_clock.mark("12")
        # --- 12. Usage telemetry export, once per install per interval ---
        # Every tick asks; the telemetry window lease decides (one export per
        # interval install-wide, whichever process runs the tick).
        try:
            from app.services.telemetry import export_telemetry_if_due

            await export_telemetry_if_due(db)
        except Exception as e:
            logger.error("Telemetry export error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

    _tick_clock.mark("13")
    # --- 13. Purge the objects a removed built-in template left (hourly, install-wide) ---
    # Not AI content: the Filigran Brand Deck's assets must leave a non-Copilot
    # platform whatever its AI state, even when the object store was unreachable
    # at the start that removed the rows. Every tick asks and a Redis
    # lease decides, and the call runs after the tick's session closed, so no
    # transaction of this tick is open while the store is called.
    try:
        from app.services.ai_state import content_mode
        from app.services.template_asset_seed import purge_orphaned_seed_objects_if_due

        await purge_orphaned_seed_objects_if_due(platform_mode=content_mode())
    except Exception as e:
        logger.error("Removed template asset purge error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("13b")
    # --- 13b. Delete the files public widget visitors attached once their session is over (hourly, install-wide) ---
    # Not AI work: a visitor's file must go whatever the AI state.
    # Also the files of queued Slack / Teams messages that never ran.
    # Every tick asks and a Redis lease decides; the sweep opens its own
    # sessions, after the tick's closed.
    try:
        from app.services.widget_uploads import purge_expired_uploads_if_due

        await purge_expired_uploads_if_due()
    except Exception as e:
        logger.error("Widget upload purge error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("13c")
    # --- 13c. Drop relay request logs past their retention (hourly, install-wide) ---
    # Not AI work: relays carry traffic whatever the AI state. Not gated on
    # RELAYS_ENABLED either: switching relays off keeps no log past its retention.
    try:
        from app.services.relays import purge_expired_request_logs_if_due

        await purge_expired_request_logs_if_due()
    except Exception as e:
        logger.error("Relay request log purge error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("14")
    # --- 14. Notify administrators of new critical / warning security audit findings (hourly, install-wide) ---
    # Not AI work: the audit covers every edition and AI state. Every tick asks
    # and a Redis lease decides; the audit then runs in the background
    # on a session of its own, so the tick never waits for it.
    try:
        from app.services.security_audit_notifications import notify_new_findings_if_due

        await notify_new_findings_if_due()
    except Exception as e:
        logger.error("Security audit notification error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("14b")
    # --- 14b. Tell owners their API keys are about to expire (hourly, install-wide) ---
    # Not AI work: a key authenticates every surface. A Redis lease decides; the
    # scan runs on a session of its own, after the tick's closed.
    try:
        from app.services.api_key_notices import notify_expiring_api_keys_if_due

        await notify_expiring_api_keys_if_due()
    except Exception as e:
        logger.error("API key expiry notice error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("14c")
    # --- 14c. Check this platform's XTM Hub registration (hourly, install-wide) ---
    # Not AI work: the registration is the platform's, whatever the edition
    # and AI state. Every tick asks and a Redis lease decides; the Hub is
    # called in the background on a session of its own, so the tick never
    # waits for it.
    try:
        from app.services.xtm_hub_registration import heartbeat_if_due

        await heartbeat_if_due()
    except Exception as e:
        logger.error("Platform registration heartbeat error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("14d")
    # --- 14d. Keep the running build's last_seen_at current ---
    # One row upserted at most every TOUCH_INTERVAL_SECONDS per process, on a session of
    # its own: the release history tells a build still running from a replaced one.
    try:
        from app.services.platform_builds import touch_running_build_if_due

        await touch_running_build_if_due()
    except Exception as e:
        logger.error("Running build refresh error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("14e")
    # --- 14e. Tell administrators of the models traces ran on without a price (daily, install-wide) ---
    # A NULL cost leaves the analytics under-reported with nothing to say so.
    # A Redis lease decides; the check runs in the background on a
    # session of its own.
    try:
        from app.services.tracing.unpriced_models import notify_unpriced_models_if_due

        await notify_unpriced_models_if_due()
    except Exception as e:
        logger.error("Unpriced model notice error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("15")
    # --- 15. Email the users about a model catalog change ---
    # A bounded batch per tick, on sessions of its own opened after the tick's
    # closed (SMTP is slow). Only while agentic AI runs: an announcement held
    # meanwhile expires rather than going out stale.
    try:
        if _ai_on:
            from app.services.model_catalog_announcements import deliver_pending_announcements

            await deliver_pending_announcements()
    except Exception as e:
        logger.error("Model catalog announcement error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("16")
    # --- 16. Answer Slack / Teams messages a dead turn left queued ---
    # Every release of a turn lock starts its conversation's queue; a process
    # that died holding the lock released nothing, so once that lock's TTL ran
    # out this gives the waiting messages their turn. The router answers them
    # and applies the AI gates itself.
    try:
        from app.services.channel_continuation import sweep_channel_queues

        restarted = await sweep_channel_queues()
        if restarted:
            logger.info("Restarted the channel queue of %d conversation(s)", restarted)
    except Exception as e:
        logger.error("Channel queue sweep error: %s", format_exception_message(e), exc_info=True)

    _tick_clock.mark("16b")
    # --- 16b. Answer web widget messages a stopped turn left unanswered ---
    # A widget session's queue is its stored messages, answered by the turn
    # holder only: a worker stopped or killed mid-turn left the visitor's
    # message with no answer and no turn coming. Each message is resumed
    # once (a Redis claim), only while agentic AI runs.
    try:
        if _ai_on:
            from app.api.v1.public_chat import resume_unanswered_widget_sessions

            widget_resumed = await resume_unanswered_widget_sessions()
            if widget_resumed:
                logger.info("Resumed %d web widget session(s) a stopped turn left unanswered", widget_resumed)
    except Exception as e:
        logger.error("Widget session resume error: %s", format_exception_message(e), exc_info=True)

    # The chat turns a platform stop cut are resumed by every worker's
    # own sweep (``cut_turn_resume.run_sweep_loop``), not by this tick:
    # the tick runs once a minute on whichever worker wins its window, a
    # draining pod's included, where a resume was cut again within seconds.

    return triggered


async def _run_teams_subscription_maintenance() -> None:
    """Renew / repair the Graph subscriptions of private Teams channels when due."""
    global _teams_subscription_tick_counter
    _teams_subscription_tick_counter += 1
    if _teams_subscription_tick_counter < _TEAMS_SUBSCRIPTION_INTERVAL:
        return
    _teams_subscription_tick_counter = 0
    try:
        from app.channels.teams import renew_private_channel_subscriptions

        actions = await renew_private_channel_subscriptions()
        if actions:
            logger.info(
                "Teams private channel subscription maintenance: %d action(s)",
                actions,
            )
    except Exception as e:
        logger.error(
            "Teams subscription renewal error: %s",
            format_exception_message(e),
            exc_info=True,
        )


async def _run_agent_account_maintenance() -> None:
    """Keep every agent account listening, poll the ones push cannot reach, resume grace periods.

    Every tick, under a cluster-wide lease; the messages it finds go through
    the agent-account pipeline, which applies the AI gates itself.
    """
    try:
        from app.services.agent_accounts.listening import run_maintenance

        actions = await run_maintenance()
        if actions:
            logger.debug("Agent account maintenance: %d action(s)", actions)
    except Exception as e:
        logger.error("Agent account maintenance error: %s", format_exception_message(e), exc_info=True)


async def _run_trace_storage_maintenance(db: AsyncSession) -> None:
    """Aggregate first, purge second - the order is load-bearing.

    ``llm_daily_stats`` is the only thing that outlives the retention purge, so
    every day must be rolled up BEFORE its traces are deleted. Both counters are
    seeded at their interval, so on the first tick after every startup both blocks
    fire in the same call; with the purge first, the oldest day was aggregated
    from an already-truncated set of traces and the resulting rollup - which
    ``aggregate_daily_stats`` never rewrites - silently under-reported forever.
    (``purge_expired_traces`` also refuses to delete a day with no rollup, so
    this ordering is belt-and-braces rather than the only guard, but a fresh
    aggregate should always win the race.)
    """
    global _trace_purge_tick_counter, _daily_stats_tick_counter

    _daily_stats_tick_counter += 1
    if _daily_stats_tick_counter >= _DAILY_STATS_INTERVAL:
        _daily_stats_tick_counter = 0
        # Before the rollup backfill: the days it stamps are recorded stale,
        # and the backfill below rebuilds them in the same cycle.
        try:
            from app.services.tracing.attribution_backfill import run_attribution_backfill

            await run_attribution_backfill(db)
        except Exception as e:
            logger.error("Trace attribution backfill error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # The person of the ROI records written before the ledger (#12077), after the
        # traces' own stamp. Bounded, idempotent, never raises.
        try:
            from app.services.roi_ledger import run_ledger_backfill

            await run_ledger_backfill(db)
        except Exception as e:
            logger.error("ROI ledger backfill error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        try:
            from app.api.v1.platform_settings_helpers import get_platform_settings
            from app.services.trace_storage import run_daily_stats_backfill

            ps = await get_platform_settings(db)
            retention_days = int(ps.trace_retention_days or 90)
            inserted = await run_daily_stats_backfill(db, retention_days=retention_days)
            if inserted:
                logger.info(
                    "Daily trace stats backfill aggregated %d row(s)", inserted
                )
        except Exception as e:
            logger.error("Daily trace stats aggregation error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # The ROI cost facts of the days the rollup just closed (#12077, L2): after the
        # rollup, which says which days are complete and what they cost. Never raises.
        try:
            from app.services.roi_ledger import close_cost_days

            await close_cost_days(db)
        except Exception as e:
            logger.error("ROI cost facts close error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()

    _trace_purge_tick_counter += 1
    if _trace_purge_tick_counter >= _TRACE_PURGE_INTERVAL:
        _trace_purge_tick_counter = 0
        try:
            from app.api.v1.platform_settings_helpers import get_platform_settings
            from app.services.trace_storage import (
                purge_expired_clickhouse_traces,
                purge_expired_traces,
                sync_clickhouse_trace_retention,
            )

            ps = await get_platform_settings(db)
            retention_days = int(ps.trace_retention_days or 90)
            await purge_expired_traces(db, retention_days)
            # Both ClickHouse steps run in this same cycle, and both are no-ops
            # under the postgres provider. The TTL is re-declared because the
            # trace DDL runs before any session can read trace_retention_days
            # (and the tables may predate the TTL entirely); the sweep is what
            # enforces the exact window and, crucially, deletes the offloaded
            # payload objects along with the rows that point at them - the
            # PostgreSQL purge above leaves them alone under this provider,
            # because the ClickHouse rows are what still serve those payloads.
            await sync_clickhouse_trace_retention(retention_days)
            await purge_expired_clickhouse_traces(retention_days)
        except Exception as e:
            logger.error("Trace retention purge error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()
        # The audit log purge rides the same hourly cycle, on its own window
        # and its own error path: a failed trace purge never keeps audit
        # entries past their retention, nor the reverse.
        try:
            from app.api.v1.platform_settings_helpers import get_platform_settings
            from app.services.audit_retention import purge_expired_audit_logs

            ps = await get_platform_settings(db)
            await purge_expired_audit_logs(db, getattr(ps, "audit_log_retention_days", None))
        except Exception as e:
            logger.error("Audit log retention purge error: %s", format_exception_message(e), exc_info=True)
            await db.rollback()


async def _run_scheduled_evaluations(db: AsyncSession, *, ai_on: bool) -> int:
    """Trigger the overdue scheduled evaluations, none while AI or evaluation is off."""
    from app.services.evaluation.availability import evaluation_enabled

    if not (ai_on and evaluation_enabled()):
        return 0
    return await _process_scheduled_evaluations(db)


async def _process_scheduled_evaluations(db: AsyncSession) -> int:
    """Check schedule-based evaluations and trigger overdue ones."""
    from app.models.evaluation import (
        Evaluation,
        EvaluationRun,
    )

    count = 0
    now = datetime.now(UTC)

    # Ordered so dispatch is deterministic across ticks: an unordered
    # scan's row order shifts once a row is UPDATEd, e.g. by this loop's own
    # last_triggered_at write.
    result = await db.execute(
        select(Evaluation)
        .where(Evaluation.enabled.is_(True))
        .order_by(Evaluation.created_at)
    )
    evaluation_ids = [e.id for e in result.scalars().all()]

    refused_this_tick: set[object] = set()
    for evaluation_id in evaluation_ids:
        # Re-fetched by id: `db.rollback()` below expires every object loaded
        # on this shared session (`expire_on_commit=False` does not cover
        # rollback), so a failure for evaluation N would leave N+1's object
        # expired, and its next attribute read would raise ``MissingGreenlet``.
        # ``db.get()`` refreshes an expired entry.
        #
        # A deliberate N+1 (one SELECT per enabled evaluation per tick):
        # negligible at realistic counts. Don't reuse the initial query's rows.
        evaluation = await db.get(Evaluation, evaluation_id)
        if evaluation is None:
            continue
        try:
            cron_expr = (evaluation.trigger_config or {}).get("schedule")
            # Skip only when no schedule is configured at all (key absent). A
            # present-but-invalid value ("" / [] / 0 / non-string) must flow
            # into the WARNING-and-skip handler below rather than being
            # silently skipped.
            if cron_expr is None:
                continue

            # Validate the cron expression once so a misconfigured evaluation
            # produces a single concise WARNING (with the bad expression)
            # instead of an ERROR with a full traceback.  The warning is
            # rate-limited via ``_should_warn_cron`` to once per (kind, id)
            # per process lifetime so long-lived misconfigurations don't flood
            # the logs every scheduler tick.
            # A non-string ``schedule`` (e.g. a list from a legacy/malformed
            # config) makes ``croniter`` raise ``AttributeError`` rather than a
            # ``ValueError``; reject it up front so it routes through the same
            # WARNING-and-skip path instead of escaping to the generic
            # ``except Exception`` and re-introducing per-tick tracebacks.
            try:
                if not isinstance(cron_expr, str):
                    raise ValueError(
                        f"cron expression must be a string, "
                        f"got {type(cron_expr).__name__}"
                    )
                last_run = evaluation.last_triggered_at
                if last_run:
                    cron = croniter(cron_expr, last_run)
                    next_run = cron.get_next(datetime)
                    if now < next_run:
                        continue
                else:
                    cron = croniter(cron_expr, now)
                    prev_run = cron.get_prev(datetime)
                    if now < prev_run:
                        continue
            except (CroniterBadCronError, ValueError) as cron_err:
                if _should_warn_cron("evaluation", evaluation.id):
                    logger.warning(
                        "Skipping evaluation %s ('%s'): invalid cron expression %r — %s",
                        evaluation.id,
                        evaluation.name,
                        cron_expr,
                        cron_err,
                    )
                continue

            # Check active runs
            active_check = await db.execute(
                select(EvaluationRun.id).where(
                    EvaluationRun.evaluation_id == evaluation.id,
                    EvaluationRun.status.in_(("pending", "running")),
                )
            )
            if active_check.first():
                continue

            # The checks and the runs of the Run button: family, callback
            # secret, dataset families, the enabled models only, a model a
            # provider of the owner offers, the full version snapshot.
            from app.services.evaluation.launch import (
                LaunchRefusedError,
                active_model_ids,
                add_launch_runs,
                check_launchable,
                plan_launch,
            )

            try:
                await check_launchable(db, evaluation)
                plan = await plan_launch(
                    db,
                    evaluation,
                    model_ids=active_model_ids(evaluation),
                    use_default=evaluation.use_agent_default_model,
                )
            except LaunchRefusedError as refused:
                # No run, and the window stays due: it starts at the first tick
                # after the cause is fixed. Logged once per cause, not every minute.
                _warn_scheduled_launch_refused(evaluation.id, evaluation.name, refused.detail)
                refused_this_tick.add(evaluation.id)
                continue

            # Stage the execution in the SAME transaction as the runs and the
            # last_triggered_at update, then commit once: a crash
            # between two commits would strand runs with no execution. The runs
            # are owned by it from creation: a scheduled launch can
            # overlap a manual one, and ownership keeps each one's cleanup off
            # the other's runs.
            from app.services.evaluation_engine import stage_evaluation_execution
            from app.services.orchestration_engine import notify_orchestration_wake

            execution = await stage_evaluation_execution(
                db, str(evaluation.id), trigger_reason="schedule"
            )
            await add_launch_runs(
                db,
                evaluation,
                plan,
                trigger_reason="schedule",
                owner_execution_id=execution.id,
            )
            evaluation.last_triggered_at = now
            await db.commit()
            await notify_orchestration_wake()
            count += 1

        except Exception as e:
            # A plain UUID, not an ORM attribute: safe to log around the
            # rollback.
            logger.error(
                "Evaluation schedule check failed for %s: %s",
                evaluation_id,
                format_exception_message(e),
                exc_info=True,
            )
            # Roll back so one evaluation's failure can't poison the shared
            # session for the rest of the loop.
            await db.rollback()

    for stale in [eid for eid in _scheduled_launch_refusals if eid not in refused_this_tick]:
        del _scheduled_launch_refusals[stale]
    return count


# Renewed while the fan-out runs, so a fan-out of any size keeps it; a crashed
# holder's claim lapses within the TTL and the window fires late, not twice.
_FANOUT_CLAIM_TTL_SECONDS = 5 * 60
_FANOUT_CLAIM_RENEW_SECONDS = 60

# Touch the claim only while it is still ours, never a successor's.
_FANOUT_CLAIM_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_FANOUT_CLAIM_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


async def _renew_fanout_claim(redis, key: str, token: str) -> None:
    while True:
        await asyncio.sleep(_FANOUT_CLAIM_RENEW_SECONDS)
        try:
            renewed = await redis.eval(_FANOUT_CLAIM_RENEW_SCRIPT, 1, key, token, _FANOUT_CLAIM_TTL_SECONDS)
        except Exception:
            logger.debug("Fan-out claim renewal failed for %s", key, exc_info=True)
            continue
        if not renewed:
            logger.warning("Fan-out claim %s lapsed before its fan-out ended", key)
            return


class _FanoutClaim:
    def __init__(self, redis, key: str, token: str):
        self._redis, self._key, self._token = redis, key, token

    async def held(self) -> bool:
        """Still ours. Asked before each user: a lost claim (Redis restart) can be
        retaken at once, long before the next renewal would notice."""
        try:
            return await self._redis.get(self._key) == self._token
        except Exception:
            # Unreachable from this pod says nothing of the others: stop, and
            # the next tick resumes with dedup.
            logger.warning("Fan-out claim %s could not be checked", self._key, exc_info=True)
            return False


@contextlib.asynccontextmanager
async def _fanout_claim(assignment_id):
    """The claim to fan out *assignment_id*, or ``None`` when another tick holds it.

    Released on exit, raise included. Not held when Redis does not answer: the
    window stays open.
    """
    key = f"copilot:scheduler_fanout_claim:{assignment_id}"
    token = uuid.uuid4().hex
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        held = bool(await redis.set(key, token, nx=True, ex=_FANOUT_CLAIM_TTL_SECONDS))
    except Exception:
        logger.warning("Fan-out claim unavailable for %s", assignment_id, exc_info=True)
        held = False
    if not held:
        yield None
        return
    renewal = asyncio.create_task(_renew_fanout_claim(redis, key, token))
    try:
        yield _FanoutClaim(redis, key, token)
    finally:
        renewal.cancel()
        try:
            await redis.eval(_FANOUT_CLAIM_RELEASE_SCRIPT, 1, key, token)
        except Exception:
            logger.warning(
                "Fan-out claim release failed for %s; it lapses in %ds",
                assignment_id,
                _FANOUT_CLAIM_TTL_SECONDS,
                exc_info=True,
            )


# Per-user fan-out: skip users who already have a run within this window so a
# monthly fan-out doesn't re-fire across scheduler ticks / restarts.
_FANOUT_RECENT_DAYS = 25


def _fanout_dedup_days(cron_expr: str, now: datetime) -> int:
    """Per-user dedup window derived from the schedule cadence.

    Monthly fan-outs need a wide window (~25d) so a tick/restart doesn't re-fire;
    weekly fan-outs (the per-stage assessments) need a narrow one (~5d) or they
    would never re-run. Use 80% of the cron interval, floored at 1 day.
    """
    try:
        itr = croniter(cron_expr, now)
        first = itr.get_next(datetime)
        second = itr.get_next(datetime)
        interval_days = (second - first).total_seconds() / 86400.0
        return max(1, int(interval_days * 0.8))
    except Exception:
        return _FANOUT_RECENT_DAYS


#: ``trigger_config.fanout_audience`` of a fan-out that only concerns the
#: people who use the CTEM command center (the seeded stage analysts): a user
#: is in it when the command center built a posture for them within
#: ``_CTEM_AUDIENCE_DAYS`` (one ``ctem_posture_snapshots`` row per user,
#: vertical and day, written by each live build). Without the key every
#: active user is targeted, as before.
FANOUT_AUDIENCE_KEY = "fanout_audience"
FANOUT_AUDIENCE_CTEM_USERS = "ctem_users"
_CTEM_AUDIENCE_DAYS = 30


async def _fanout_audience_user_ids(db: AsyncSession, assignment, now: datetime) -> set | None:
    """The users a fan-out is limited to, or ``None`` for every active user."""
    config = getattr(assignment, "trigger_config", None)
    audience = config.get(FANOUT_AUDIENCE_KEY) if isinstance(config, dict) else None
    if audience != FANOUT_AUDIENCE_CTEM_USERS:
        return None
    from app.models.ctem_posture_snapshot import CtemPostureSnapshot

    since = now - timedelta(days=_CTEM_AUDIENCE_DAYS)
    return set(
        (
            await db.execute(
                select(CtemPostureSnapshot.user_id).where(CtemPostureSnapshot.updated_at >= since).distinct()
            )
        )
        .scalars()
        .all()
    )


def _is_expired(instance) -> bool:
    state = sa_inspect(instance, raiseerr=False)
    return isinstance(state, InstanceState) and bool(state.expired_attributes)


async def _reload_expired(db: AsyncSession, *instances) -> None:
    """Re-read the instances a rollback expired, one query per model.

    A rollback expires the whole session, and an AsyncSession cannot lazy-load
    an expired attribute: the next read would raise MissingGreenlet.
    """
    ids: dict[type, list] = {}
    for instance in instances:
        if _is_expired(instance):
            # The identity key survives the expiry; reading ``.id`` would not.
            ids.setdefault(type(instance), []).append(sa_inspect(instance).identity[0])
    for model, model_ids in ids.items():
        await db.execute(
            select(model).where(model.id.in_(model_ids)).execution_options(populate_existing=True)
        )


async def _still_due(db: AsyncSession, assignment, cron_expr: str, now: datetime) -> bool:
    """Whether the window is still open: an overlapping tick may have served it
    since this tick loaded the assignment (``expire_on_commit=False``)."""
    state = sa_inspect(assignment, raiseerr=False)
    if isinstance(state, InstanceState) and state.persistent:
        await db.refresh(assignment, ["last_triggered_at", "schedule_resumed_at"])
    last_run = schedule_base(assignment)
    if last_run is None:
        return True
    zone, _ = schedule_timezone(assignment.trigger_config)
    next_run = next_cron_run(cron_expr, last_run, tz=zone)
    return next_run is not None and now >= next_run


def _fanout_spread_delay_seconds(index: int, total: int) -> int:
    """Evenly spread *total* fan-out runs across the configured window.

    Run ``index`` (0-based) gets a delay of ``index * window / total`` seconds,
    so the first run starts immediately and the rest trickle in at a constant
    rate bounded by ``settings.fanout_spread_window_seconds`` (default 1h)
    regardless of fleet size. Returns 0 when the window is disabled (<= 0) or
    there is nothing to spread.
    """
    window = settings.fanout_spread_window_seconds
    if window <= 0 or total <= 1 or index <= 0:
        return 0
    return (index * window) // total


async def _fanout_assignment_to_users(
    db: AsyncSession,
    assignment,
    agent,
    now: datetime,
    cron_expr: str,
    claim: _FanoutClaim | None = None,
) -> int | None:
    """Fan a scheduled assignment out to ONE run per active user.

    Each run executes under that user's own identity (their integrations / JWT /
    memory), so a single company-managed automation produces per-user results
    (e.g. the exposure briefing scoped to what each user can see in
    OpenCTI/OpenAEV). Gated like every AI dispatch and per-user deduped.

    Returns the number of runs triggered, or ``None`` when agentic AI is
    unavailable here - before the fan-out or in the middle of it - or once
    *claim* is lost, so the caller leaves the cron window open.
    """
    from app.models.assignment import AssignmentRun
    from app.models.user import User
    from app.services.agentic_gate import agentic_refusal, refusal_code, refusal_is_personal
    from app.services.ai_hold import HOLD_MARKER_KEY
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, trigger_assignment

    # The same gate as the tick that brought us here - AI switched on AND a
    # licensed edition - asked again because a tick is long enough for
    # either to change. Refused means "not now", never "done for this window":
    # the caller leaves last_triggered_at unstamped, exactly as the tick does
    # for every other schedule while AI is unavailable, so the fan-out runs
    # once AI is back. A transient failure elsewhere (DB connectivity) still
    # propagates for the same reason.
    refusal = await agentic_refusal(db)
    if refusal is not None:
        logger.info(
            "Skipping fan-out of '%s' - agentic AI unavailable (%s)",
            assignment.name,
            refusal,
        )
        return None

    recent_cutoff = now - timedelta(days=_fanout_dedup_days(cron_expr, now))
    # Load each user's active CTEM vertical so the run is tagged with it (the
    # per-stage analysts compute for that vertical; the dashboard matches runs
    # to the user's current vertical).
    rows = (
        await db.execute(
            select(User.id, User.ctem_vertical).where(User.is_active.is_(True))
        )
    ).all()
    # A fan-out that concerns CTEM users only makes no run for a user who
    # never opened the command center: N active users with one CTEM user
    # is one run, not N heavy runs queued behind every other fleet.
    audience = await _fanout_audience_user_ids(db, assignment, now)
    if audience is not None:
        rows = [row for row in rows if row[0] in audience]
        if not rows:
            return 0

    # Prefetch — in ONE query — the set of user_ids that already have a recent
    # run for this assignment, so dedup is an in-memory lookup instead of a
    # SELECT per active user (this fan-out runs hourly across every active user,
    # so the old per-user query was O(N) round-trips). A run held while agentic
    # AI was unavailable counts whatever its age: waiting (the hold
    # marker) or just resumed (running), it IS that user's catch-up. Any other
    # old run - a paused one nobody answered - keeps the dedup window.
    recent_user_ids = set(
        (
            await db.execute(
                select(AssignmentRun.user_id)
                .where(
                    AssignmentRun.assignment_id == assignment.id,
                    or_(
                        AssignmentRun.created_at >= recent_cutoff,
                        and_(
                            AssignmentRun.status.in_(list(ACTIVE_RUN_STATUSES)),
                            AssignmentRun.trigger_context[HOLD_MARKER_KEY].is_not(None),
                        ),
                        AssignmentRun.status == "running",
                    ),
                    AssignmentRun.user_id.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    eligible = [
        (user_id, ctem_vertical)
        for user_id, ctem_vertical in rows
        if user_id not in recent_user_ids
    ]

    triggered = 0
    for index, (user_id, ctem_vertical) in enumerate(eligible):
        if claim is not None and not await claim.held():
            # Another tick may claim it now: going on would race its dedup.
            logger.warning(
                "Fan-out of '%s' stopped after %d run(s) - its claim was lost",
                assignment.name,
                triggered,
            )
            return None
        try:
            await trigger_assignment(
                assignment=assignment,
                agent=agent,
                payload={
                    "trigger_type": "schedule_fanout",
                    "scheduled_at": now.isoformat(),
                },
                db=db,
                triggered_by_user_id=user_id,
                trigger_context={
                    "type": "schedule_fanout",
                    "cron": cron_expr,
                    "scheduled_at": now.isoformat(),
                    "ctem_vertical": ctem_vertical,
                },
                # Spread the per-user runs evenly across the fan-out window
                # instead of making them all due at the same instant.
                # A fan-out herd is N heavy agentic runs against the same LLM
                # provider account: fired together they trip provider rate
                # limits, and — because the orchestration deadline derives
                # from ``run_at`` — runs queued behind the reconciler's
                # concurrency cap burn their timeout budget while waiting and
                # get dead-lettered as timeouts without ever executing.
                extra_delay_seconds=_fanout_spread_delay_seconds(
                    index, len(eligible)
                ),
            )
            triggered += 1
        except Exception as exc:
            # trigger_assignment is a transaction boundary (it commits each
            # run itself), so a failure here may leave the session in a
            # rolled-back-pending state. Roll back before the next user so one
            # transient failure can't poison every subsequent iteration (and
            # the caller's last_triggered_at commit) — already-committed runs
            # from prior users are durable and untouched. Matches the
            # rollback-and-continue every other handler in this module uses.
            await db.rollback()
            await _reload_expired(db, assignment, agent)
            refusal = refusal_code(exc)
            if refusal is not None and refusal_is_personal(exc):
                # AI is off for this user's group only: their run is not
                # made, the users left still get theirs.
                logger.info(
                    "Fan-out of '%s' skipped user %s - AI is disabled for their group",
                    assignment.name,
                    user_id,
                )
                continue
            if refusal is not None:
                # Agentic AI went unavailable mid fan-out: every user
                # left would be refused too. Stop, and leave the window open -
                # the users already served are deduplicated when it reruns.
                logger.info(
                    "Fan-out of '%s' stopped after %d run(s) - agentic AI "
                    "unavailable (%s)",
                    assignment.name,
                    triggered,
                    refusal,
                )
                return None
            logger.warning(
                "Fan-out trigger failed for assignment '%s' user %s: %s",
                assignment.name,
                user_id,
                format_exception_message(exc),
            )
    return triggered


async def _process_scheduled_assignments(db: AsyncSession) -> int:
    """Check schedule-based assignments and trigger overdue ones."""
    count = 0

    # Find all enabled schedule assignments. One of a deactivated owner (not
    # company-managed) is not selected: no run and no log per tick, and
    # ``last_triggered_at`` untouched, so once the account is reactivated a
    # window that came due meanwhile fires once, as after a resume.
    result = await db.execute(
        select(Assignment).where(
            Assignment.trigger_type == "schedule",
            Assignment.enabled.is_(True),
            owner_not_deactivated(),
        )
    )
    assignments = result.scalars().all()

    now = datetime.now(UTC)

    # Batch-load all agents referenced by scheduled assignments (avoid N+1).
    # With 200 users each having multiple scheduled assignments, querying
    # the agent table per assignment would be hundreds of round-trips.
    agent_ids = {wf.agent_id for wf in assignments if wf.agent_id}
    agents_map: dict = {}
    if agent_ids:
        agent_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        agents_map = {a.id: a for a in agent_result.scalars().all()}

    # Ids read before anything can roll back: the handler below logs one after
    # its rollback has expired every instance of the session.
    pending = [(a.id, a) for a in assignments]
    for index, (assignment_id, assignment) in enumerate(pending):
        try:
            if _is_expired(assignment):
                # A rollback expired them all: reload the rest of the tick at once.
                await _reload_expired(
                    db, *(a for _, a in pending[index:]), *agents_map.values()
                )
                if _is_expired(assignment):
                    continue  # deleted meanwhile: no row to reload
            cron_expr = (assignment.trigger_config or {}).get("cron")
            # Skip only when no cron is configured at all (key absent). A
            # present-but-invalid value ("" / [] / 0 / non-string) is a real
            # misconfiguration and must flow into the handler below (disable +
            # failed run), not be silently skipped — matching the save-path
            # ``core.utils.validate_cron_expression`` contract.
            if cron_expr is None:
                continue

            # Check if the agent is enabled (batch-loaded)
            agent = agents_map.get(assignment.agent_id)
            if agent is not None and _is_expired(agent):
                await _reload_expired(db, *agents_map.values())
                if _is_expired(agent):
                    continue  # deleted meanwhile
            if not agent or not agent.enabled:
                continue

            # Whether this is the assignment's first-ever scheduled run — the
            # auto-run fired right after creation. Captured BEFORE
            # ``last_triggered_at`` is stamped below so the flag survives into
            # the trigger context. The schedule-spread delay is skipped for it
            # so a freshly created assignment starts promptly instead of
            # sitting in "delayed / waiting…" for up to the spread window.
            first_run = schedule_base(assignment) is None

            # Determine if this assignment is overdue.  Invalid cron
            # expressions are caught here (instead of the generic ``except``
            # below) so a misconfiguration surfaces as an actionable failure
            # rather than an ERROR-with-traceback on every scheduler tick
            # (~30s): we disable the assignment, persist a failed
            # ``AssignmentRun`` (visible in the activity/run surfaces), and log
            # a single ERROR.  The remediation (disable + failed run) is
            # attempted on *every* tick so a transient commit failure is
            # retried next tick — the assignment stays enabled and re-selected
            # until the disable actually commits, after which the query above
            # stops selecting it.  Only the ERROR log is rate-limited to once
            # per (kind, id) per process via ``_should_warn_cron`` (and only
            # after a successful disable) so a long-lived misconfiguration
            # doesn't flood the logs.
            #
            # ``cron_expr`` comes straight from (possibly legacy) user config
            # and may not be a plain string.  ``croniter`` raises non-``ValueError``
            # types for those (e.g. ``AttributeError`` for a list), which would
            # otherwise escape the handler below and fall through to the generic
            # ``except Exception`` — re-introducing the perpetual traceback spam
            # for the non-string subset.  Reject non-strings up front as a
            # ``ValueError`` so they route through the same misconfiguration
            # flow, mirroring ``core.utils.validate_cron_expression``.
            try:
                if not isinstance(cron_expr, str):
                    raise ValueError(
                        f"cron expression must be a string, "
                        f"got {type(cron_expr).__name__}"
                    )
                # The later of the last fire and the last time it was enabled
                # again: enabling never makes a schedule due at once (#8899).
                last_run = schedule_base(assignment)
                if last_run:
                    # Single source of truth with the read-only schedule
                    # endpoint (``GET /assignments/schedule``) so the "next
                    # run" the Agentic Flow UI shows can never drift from the
                    # tick that actually fires here.
                    # The cron is read in ``trigger_config.timezone`` (an
                    # IANA zone, daylight saving included), UTC when absent or
                    # unknown; the unknown-zone warning is surfaced by the
                    # schedule endpoint and ``get_assignment``.
                    zone, _ = schedule_timezone(assignment.trigger_config)
                    next_run = next_cron_run(cron_expr, last_run, tz=zone)
                    if next_run is None:
                        # ``next_cron_run`` only returns None for an
                        # invalid / non-string cron — which the ``isinstance``
                        # guard above already rejected. Route any residual case
                        # through the same misconfiguration handler below
                        # (disable + failed run) instead of silently skipping.
                        raise ValueError(
                            f"cron expression {cron_expr!r} is not parseable"
                        )
                    if now < next_run:
                        continue  # not yet due
                else:
                    # Never ran – check if we should run now
                    cron = croniter(cron_expr, now)
                    prev_run = cron.get_prev(datetime)
                    # Only run if within the last 2 intervals
                    cron2 = croniter(cron_expr, prev_run)
                    cron2.get_next(datetime)
                    if now < prev_run:
                        continue
            except (CroniterBadCronError, ValueError) as cron_err:
                if agent.is_builtin:
                    # Built-in (seeded) assignments are never auto-disabled:
                    # their cron comes from versioned seed data (a
                    # dev error, not a user misconfiguration), and startup
                    # seeding force-re-enables them on every boot — disabling
                    # here would only create a disable/resurrect loop. Log
                    # once per process and skip (no per-tick failed-run spam).
                    if _should_warn_cron("assignment", assignment.id):
                        logger.error(
                            "Built-in assignment %s ('%s') has an invalid "
                            "cron expression %r (%s) — skipping, NOT "
                            "disabling (seed data must be fixed)",
                            assignment.id,
                            assignment.name,
                            cron_expr,
                            cron_err,
                        )
                    continue
                # Always attempt the remediation — never gate it behind
                # ``_should_warn_cron`` — so a transient commit failure is
                # retried on the next tick instead of being permanently
                # silenced once the rate-limit token is spent.  The token is
                # consumed (and the ERROR logged) only after a successful
                # disable, since the log line follows the remediation call.
                await _flag_assignment_cron_misconfiguration(
                    db=db,
                    assignment=assignment,
                    cron_expr=cron_expr,
                    cron_err=cron_err,
                    now=now,
                )
                if _should_warn_cron("assignment", assignment.id):
                    logger.error(
                        "Disabled assignment %s ('%s'): invalid cron expression %r — %s",
                        assignment.id,
                        assignment.name,
                        cron_expr,
                        cron_err,
                    )
                continue

            # Per-user fan-out: one run per active user (each as themselves),
            # instead of a single run as the owner. last_triggered_at is stamped
            # only AFTER the fan-out attempt returns, so a transient failure
            # (rolled back by the per-assignment handler below) is retried on the
            # next tick instead of being skipped for the whole cron window;
            # per-user dedup keeps a retry from re-serving users, and the claim
            # keeps a concurrent tick out. Bypasses the per-assignment has_active_run guard
            # (parallel per-user runs are intended here).
            #
            # SECURITY: fan-out is an admin-managed capability and is gated to
            # company-managed (admin-owned) assignments only. Without this gate a
            # normal user could set ``run_as_all_users`` on their OWN assignment
            # (directly via the API — the UI only exposes the toggle for
            # company-managed assignments) and make it execute under EVERY active
            # user's identity/integrations (privilege escalation). A user-owned
            # assignment with the flag set falls through to the normal single-run
            # path below (runs as its owner).
            if assignment.run_as_all_users and assignment.is_company_managed:
                structlog.contextvars.bind_contextvars(
                    agent=agent.name, assignment=assignment.name
                )
                try:
                    # Dedup sees only committed runs, so a tick overlapping one
                    # still fanning out would serve its remaining users twice. The
                    # claim is held until the stamp below is committed.
                    async with _fanout_claim(assignment.id) as claim:
                        if claim is None:
                            logger.info(
                                "Fan-out of '%s' not claimed - another tick is fanning it "
                                "out or Redis is unavailable; its window stays open",
                                assignment.name,
                            )
                            continue
                        if not await _still_due(db, assignment, cron_expr, now):
                            continue
                        fanned = await _fanout_assignment_to_users(
                            db, assignment, agent, now, cron_expr, claim
                        )
                        if fanned is None:
                            # Agentic AI unavailable or claim lost: the window stays open.
                            continue
                        # Stamp only after a clean return (0 included): a raise
                        # leaves the window open, and dedup absorbs the retry.
                        assignment.last_triggered_at = now
                        await db.commit()
                    if fanned:
                        count += 1
                        logger.info(
                            "Scheduled assignment '%s' (agent '%s') fanned out to %d user(s)",
                            assignment.name,
                            agent.name,
                            fanned,
                        )
                finally:
                    structlog.contextvars.unbind_contextvars("agent", "assignment")
                continue

            if not await _still_due(db, assignment, cron_expr, now):
                continue

            # Skip if there's already an active run for this assignment.
            # This prevents overlapping executions — an assignment cannot re-run
            # while a previous run is still pending, delayed, running, or
            # awaiting human input.
            if await has_active_run(db, assignment.id) and not await _supersede_stale_fires(db, assignment, now):
                # Never silent: each slot the dropped fire covers is a
                # ``skipped`` run stamped on it, naming the run in its way, so
                # a run stuck across resumes shows in the run list instead of
                # starving the schedule. The base moves past the slots in the
                # same commit as their rows, never without them (#2582), so no
                # backlog of "overdue" ticks fires once the active run ends.
                outcome = await _record_skipped_fire(db, assignment, cron_expr, now)
                if outcome == "recorded":
                    await db.commit()
                    continue
                if outcome == "failed":
                    # Nothing recorded: the base stays, the next tick tries again.
                    await db.rollback()
                    continue
                # The active run ended between the two reads: fire as usual.

            # The cron slot this fire serves and the earlier windows no sweep
            # reached (#2562): the late fire serves the latest due slot and is
            # stamped on it, so the next fire stays on the cron; the windows
            # before it are recorded as ``skipped`` runs once the fire exists.
            # A first run (no base) serves ``now``, as before.
            served_at = now
            missed: list[datetime] = []
            missed_truncated = False
            base = schedule_base(assignment)
            if base is not None:
                from app.services import schedule_overlap

                zone, _ = schedule_timezone(assignment.trigger_config)
                slot, missed, missed_truncated = schedule_overlap.missed_windows(cron_expr, base, now, tz=zone)
                if slot is not None and slot <= now:
                    served_at = slot

            # Mark as triggered FIRST to prevent re-triggering on next tick.
            # A run creation that does not complete past this commit (the tick
            # stopped, a non-refusal error) records the slot as a lost fire
            # below, so the base never moves past a slot without a row (#2582).
            previous_triggered_at = assignment.last_triggered_at
            assignment.last_triggered_at = served_at
            await db.commit()

            # Trigger the assignment
            structlog.contextvars.bind_contextvars(
                agent=agent.name,
                assignment=assignment.name,
            )
            try:
                run_id = await trigger_assignment(
                    assignment=assignment,
                    agent=agent,
                    payload={"trigger_type": "schedule", "scheduled_at": served_at.isoformat()},
                    db=db,
                    trigger_context={
                        "type": "schedule",
                        "cron": cron_expr,
                        "scheduled_at": served_at.isoformat(),
                        "triggered_at": now.isoformat(),
                        # Internal flag (``_`` prefix → hidden from the run-detail
                        # UI): the first post-creation run skips the spread delay
                        # and starts immediately.
                        "_first_run": first_run,
                    },
                )
            except asyncio.CancelledError:
                await _record_lost_fire(assignment, cron_expr, served_at, now, reason="the scheduler tick was stopped")
                raise
            except AssignmentOwnerDeactivatedError:
                # Its owner was deactivated since the query picked it:
                # the pause, not an error. The window stays open, as for an
                # assignment the query had skipped.
                assignment.last_triggered_at = previous_triggered_at
                await db.commit()
                structlog.contextvars.unbind_contextvars("agent", "assignment")
                continue
            except Exception as exc:
                from app.services.agentic_gate import refusal_code, refusal_is_personal

                refusal = refusal_code(exc)
                if refusal is None:
                    await _record_lost_fire(
                        assignment, cron_expr, served_at, now, reason=format_exception_message(exc)[:200]
                    )
                    raise
                if refusal_is_personal(exc):
                    # AI is off for a group of the person it runs as: this
                    # assignment's window stays open - it fires once AI is
                    # back for them - and the sweep goes on with the others.
                    assignment.last_triggered_at = previous_triggered_at
                    await db.commit()
                    structlog.contextvars.unbind_contextvars("agent", "assignment")
                    logger.info(
                        "Scheduled assignment '%s' not triggered - AI is disabled "
                        "for its owner's group; its window stays open",
                        assignment.name,
                    )
                    continue
                # Agentic AI went unavailable since the tick asked:
                # refused before any run existed, so the window goes back, as
                # the fan-out leaves its own open, and the schedule runs once
                # AI is available again. Every assignment left would be
                # refused too: the sweep stops here.
                assignment.last_triggered_at = previous_triggered_at
                await db.commit()
                structlog.contextvars.unbind_contextvars("agent", "assignment")
                logger.info(
                    "Scheduled assignment '%s' not triggered - agentic AI "
                    "unavailable (%s); its window stays open",
                    assignment.name,
                    refusal,
                )
                break
            if missed:
                await _record_missed_windows(db, assignment, cron_expr, missed, truncated=missed_truncated, now=now)
            logger.info(
                "Scheduled assignment '%s' (agent '%s') triggered: run %s",
                assignment.name,
                agent.name,
                run_id,
            )
            structlog.contextvars.unbind_contextvars("agent", "assignment")
            count += 1

        except Exception as e:
            # Roll back so a failed assignment (e.g. a propagated fan-out / EE
            # gate error) leaves a clean session for the rest of the tick
            # instead of poisoning every subsequent assignment's queries.
            # Matches the rollback every other handler in this module uses.
            await db.rollback()
            logger.error(
                "Error processing scheduled assignment %s: %s",
                assignment_id,
                format_exception_message(e),
                exc_info=True,
            )
            structlog.contextvars.unbind_contextvars("agent", "assignment")

    return count


async def _dispatch_inbox_item(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    item: dict,
    *,
    owner_user_id: str | None,
    writer: Any,
    cap: int,
) -> str | None:
    """Start the inbox run of one claimed *item*; its run id, or ``None`` when skipped.

    Every way it fails to start a run hands the claim back, so the item is
    retried, never stranded ``claimed``: a skip (every slot taken meanwhile),
    a deactivated owner (the pause, not an error), or a raise (no run
    was created, so no run finalizer would ever release it; the item is
    healthy, so no dispatch attempt is counted against it, and the error goes
    on to the caller).
    """
    from app.services.work_item import bounded_trigger_item, release_work_item_claim

    structlog.contextvars.bind_contextvars(agent=agent.name, assignment=assignment.name)
    try:
        run_id = await trigger_assignment(
            assignment=assignment,
            agent=agent,
            payload={
                "trigger_type": "work_item",
                "work_item_id": item["id"],
                "work_item": bounded_trigger_item(item),
            },
            db=db,
            trigger_context={
                "type": "work_item",
                "work_item_id": item["id"],
                "correlation_key": item.get("correlation_key"),
                "triggered_at": datetime.now(UTC).isoformat(),
            },
            skip_if_active=True,
            max_active_runs=cap,
        )
    except AssignmentOwnerDeactivatedError:
        await release_work_item_claim(work_item_id=item["id"], user_id=owner_user_id, writer=writer)
        return None
    except Exception:
        await release_work_item_claim(work_item_id=item["id"], user_id=owner_user_id, writer=writer)
        raise
    finally:
        structlog.contextvars.unbind_contextvars("agent", "assignment")
    if run_id is None:
        await release_work_item_claim(work_item_id=item["id"], user_id=owner_user_id, writer=writer)
    return run_id


async def _held_by_a_live_lease(item: dict) -> bool:
    """Whether a live run holds a lease on one of the open pull requests *item* names (#10737).

    That run is pushing the fix: a dispatched run would read the lease and
    stand down, and the item would come back for another one. The item stays
    open and unclaimed; the pass after the release (or the holder's end) takes it.
    """
    from app.services.shared_state_pull_requests import live_pull_request_lease

    lease = await live_pull_request_lease(item.get("payload"), user_id=item.get("user_id"))
    if lease is None:
        return False
    logger.info(
        "Work item %s not dispatched: run %s holds %s on %s#%s",
        item.get("id"),
        lease["run_id"],
        lease["key"],
        lease["repo"],
        lease["number"],
    )
    return True


async def _red_diagnosed_by_a_live_run(item: dict, agent_id: Any) -> bool:
    """Whether a live run of *agent_id* already diagnoses the red head of one of *item*'s pull requests (#11809).

    A red listener keyed on the head (``trigger_config.dedup`` naming
    ``owner/name@sha``) or another item's CI-completion dispatch of that head
    (``services/ci_red_claims.py``). Any open pull request's head an event run
    of the agent was woken on, green included, counts too: that run (a green
    listener holding the per-head claim) merges it, a second run on the head
    could only stand down. The item stays open and unclaimed; the pass after
    that run's end takes it. A failed read dispatches as before.
    """
    from app.services.ci_red_claims import live_red_carrier
    from app.services.work_item_pull_requests import pull_request_bindings

    heads = [
        b for b in pull_request_bindings(item.get("payload")) if b.get("head_sha") and b.get("state", "open") == "open"
    ]
    if not heads:
        return False
    try:
        async with async_session() as db:
            for binding in heads:
                carrier = await live_red_carrier(
                    db, agent_id, binding["repo"], binding["head_sha"], exclude_item_id=item.get("id")
                )
                if carrier is not None:
                    logger.info(
                        "Work item %s not dispatched: run %s already works on the head of %s#%s at %s",
                        item.get("id"),
                        carrier,
                        binding["repo"],
                        binding["number"],
                        binding["head_sha"],
                    )
                    return True
    except Exception as exc:  # fail open: dispatch as before
        logger.debug("Red carrier of work item %s not read: %s", item.get("id"), exc)
    return False


def _express_prefix(lane_prefix: str, inbox_prefix: str | None) -> str | None:
    """The prefix an express listing filters on, inside the inbox's own prefix.

    No inbox prefix: the lane's. Otherwise the longer of the two when one
    starts with the other (case-insensitively, as the inbox filter matches),
    else ``None``: no item can match both, nothing is listed.
    """
    if not inbox_prefix:
        return lane_prefix
    lane, own = lane_prefix.lower(), inbox_prefix.strip().lower()
    if lane.startswith(own):
        return lane_prefix
    if own.startswith(lane):
        return inbox_prefix.strip()
    return None


async def _with_express_candidates(
    items: list[dict], lane: Any, *, inbox_prefix: str | None, limit: int, lister: Any
) -> list[dict]:
    """*items* followed by the express items not already in it, each prefix in lane order."""
    seen = {item["id"] for item in items}
    merged = list(items)
    for lane_prefix in lane.prefixes:
        prefix = _express_prefix(lane_prefix, inbox_prefix)
        if prefix is None:
            continue
        for item in await lister(correlation_prefix=prefix, limit=limit):
            if item["id"] not in seen:
                seen.add(item["id"])
                merged.append(item)
    return merged


async def _record_inbox_dispatch(
    assignment: Assignment, agent: Agent, item: dict, run_id: str, path: str, record_claim_run: Any
) -> None:
    """Name *run_id* the holder of *item* and log and time the dispatch."""
    try:
        await record_claim_run(item["id"], agent_id=agent.id, run_id=run_id)
    except Exception as e:
        logger.warning(
            "Could not record run %s as the holder of work item %s: %s",
            run_id,
            item["id"],
            format_exception_message(e),
        )
    logger.info(
        "Work-item inbox '%s' (agent '%s') dispatched item %s: run %s",
        assignment.name,
        agent.name,
        item["id"],
        run_id,
    )
    observe_dispatch_lag(item, path)


async def _process_work_item_assignments(db: AsyncSession, *, path: str = "tick") -> int:
    """Dispatch due, owned work items to their agent's inbox assignment.

    *path* names what ran the pass for the dispatch-lag series: the scheduler
    ``tick``, or a ``wake`` when an item became dispatchable
    (``work_item_dispatch_wake``).

    The autonomous consumer half of the work-item primitive. For each enabled
    ``work_item``-trigger assignment, pick the eligible OPEN items owned by the
    assignment's agent (priority first, then due, then oldest), atomically
    claim one, and trigger the assignment with the item as the trigger payload.
    An inbox runs up to ``trigger_config.max_concurrent_runs`` items at once
    (default 1, the serial inbox): one pass fills every free slot with
    a distinct item, and the end of each run wakes the next pass
    (``work_item_dispatch_wake``), so a slot that frees is filled at once. The
    cap is enforced twice: the free slots counted here, and the
    ``max_active_runs`` guard of ``trigger_assignment`` under its advisory
    lock, so two passes racing never exceed it. A skipped dispatch releases
    the claim so the item is retried later instead of being stranded
    ``claimed``.

    Pickup flows through the SAME ``trigger_assignment`` -> orchestration-engine
    path as every other trigger, so the agentic loop and the run lifecycle are
    unchanged - this is purely a new trigger SOURCE, not a new execution path.
    """
    from app.services.assignment_dispatch import work_item_express_lane, work_item_inbox_cap
    from app.services.assignment_engine import count_active_runs
    from app.services.work_item import (
        claim_work_item,
        list_dispatchable_work_items,
        load_assignment_inboxes,
        record_claim_run,
    )

    count = 0
    # Paused while the owner is deactivated: its items stay open,
    # unclaimed, and are dispatched once the account is reactivated. The
    # selection is the one the work-item views say will pick an item up.
    inboxes = await load_assignment_inboxes(db)
    if not inboxes:
        return 0

    per_tick = max(int(getattr(settings, "work_item_dispatch_per_tick", 5) or 0), 1)

    for inbox in inboxes:
        assignment, agent = inbox.source
        try:
            # The dispatch is an automatic trigger acting on behalf of the
            # assignment owner, so scope visible items to that user +
            # company-managed (the standard work-item visibility). The inbox
            # claims as the assignment's agent: a personal agent never takes
            # over a company-managed item, nor an agent one whose creator keeps
            # it out, so its listing leaves them out.
            owner_user_id = inbox.user_id
            writer = inbox.writer

            # The inbox's slots: 1 by default (the serial inbox), up to
            # ``max_concurrent_runs``. No claim while every slot is taken; the
            # ``max_active_runs`` guard of trigger_assignment is the second
            # line of defence, atomic under its advisory lock.
            cap = work_item_inbox_cap(assignment.trigger_config)
            # The express lane (#11082): extra slots above the cap kept for
            # the items whose correlation key starts with one of its prefixes.
            lane = work_item_express_lane(assignment.trigger_config)
            if cap <= 1 and lane is None:
                if await has_active_run(db, assignment.id):
                    continue
                free = 1
            else:
                free = cap - await count_active_runs(db, assignment.id)
            express_free = (free + lane.slots) if lane is not None else 0
            if free <= 0 and express_free <= 0:
                continue

            # The candidate pool covers every free slot plus the claim-race
            # headroom of ``work_item_dispatch_per_tick``; the express items
            # are listed apart so a full queue of long items never hides them.
            items: list[dict] = []
            if free > 0:
                items = await list_dispatchable_work_items(
                    owner_agent_id=inbox.owner_agent_id,
                    user_id=owner_user_id,
                    correlation_prefix=inbox.correlation_prefix,
                    priority_min=inbox.priority_min,
                    limit=per_tick + free - 1,
                    writer=writer,
                    run_user_id=inbox.run_user_id,
                    run_reads_items=inbox.run_reads_items,
                )
            if lane is not None and express_free > max(free, 0):
                items = await _with_express_candidates(
                    items,
                    lane,
                    inbox_prefix=inbox.correlation_prefix,
                    limit=per_tick + express_free - 1,
                    lister=functools.partial(
                        list_dispatchable_work_items,
                        owner_agent_id=inbox.owner_agent_id,
                        user_id=owner_user_id,
                        priority_min=inbox.priority_min,
                        writer=writer,
                        run_user_id=inbox.run_user_id,
                        run_reads_items=inbox.run_reads_items,
                    ),
                )
            if not items:
                continue

            # Walk the pool in dispatch order (priority, due, age) and fill
            # the free slots with the items we win: losing a claim race to a
            # concurrent claimer falls through to the next candidate instead
            # of wasting the pass. Once the regular slots are taken, only an
            # express item is claimed, guarded by the cap plus the lane.
            candidates = iter(items)
            while free > 0 or express_free > 0:
                item = None
                guard = cap
                for candidate in candidates:
                    if free > 0:
                        guard = cap
                    elif lane is not None and express_free > 0 and lane.matches(candidate.get("correlation_key")):
                        guard = cap + lane.slots
                    else:
                        continue  # the regular slots are taken and it is no express item
                    if await _held_by_a_live_lease(candidate):
                        continue  # its lease holder is working on it; a pass after the release takes it
                    if await _red_diagnosed_by_a_live_run(candidate, agent.id):
                        continue  # another run of the agent diagnoses its red head; a pass after its end takes it
                    claim = await claim_work_item(
                        candidate["id"],
                        agent_id=str(agent.id),
                        user_id=owner_user_id,
                        writer=writer,
                    )
                    if claim.get("claimed"):
                        item = candidate
                        break
                if item is None:
                    break  # the pool is spent; the next pass takes the rest
                run_id = await _dispatch_inbox_item(
                    db, assignment, agent, item, owner_user_id=owner_user_id, writer=writer, cap=guard
                )
                if run_id is None:
                    break  # every slot was taken meanwhile; the claim is released
                await _record_inbox_dispatch(assignment, agent, item, run_id, path, record_claim_run)
                count += 1
                free -= 1
                express_free -= 1
        except Exception as e:
            logger.error(
                "Error processing work-item assignment %s: %s",
                assignment.id,
                format_exception_message(e),
                exc_info=True,
            )
            structlog.contextvars.unbind_contextvars("agent", "assignment")

    return count


class _SkillFetchPlan:
    """How one skill will be fetched — resolved *before* the HTTP loop starts.

    Credential lookup needs the DB, but the fetch loop runs after
    ``release_db_connection_before_loop`` has handed the pooled connection
    back, so every skill's request is fully prepared up front.  ``headers`` is
    ``None`` for a public URL (a plain anonymous GET, exactly as before);
    ``config_error`` short-circuits the request entirely when the bound
    credential cannot be used at all.
    """

    __slots__ = (
        "config_error",
        "credential_bound",
        "headers",
        "integration_name",
        "route",
        "skill",
        "source",
        "url",
    )

    def __init__(
        self,
        *,
        skill,
        url: str,
        headers: dict[str, str] | None = None,
        source=None,
        integration_name: str | None = None,
        config_error: str | None = None,
        route=None,
    ) -> None:
        self.skill = skill
        self.url = url
        self.headers = headers
        self.source = source
        self.integration_name = integration_name
        self.config_error = config_error
        # The bound integration's relay route.
        self.route = route
        # Whether the skill is *bound* to a credential — which is not the same
        # as whether this particular request carries one: a ``config_error``
        # plan sends no request at all, yet the failure is entirely about the
        # credential.  The telemetry label keys off the binding so an operator
        # filtering ``auth="authenticated"`` sees every credential-bound skill,
        # including the ones that never got as far as a request.
        self.credential_bound = headers is not None or config_error is not None

    @property
    def sends_credentials(self) -> bool:
        """Whether *this request* carries an ``Authorization`` header.

        Distinct from :attr:`credential_bound`: only a request that actually
        authenticated may have GitHub's overloaded 403 / 404 statuses
        reinterpreted (a 404 on an anonymous read means what it says).
        """
        return self.headers is not None

    @property
    def auth_label(self) -> str:
        return "authenticated" if self.credential_bound else "anonymous"


async def _build_skill_fetch_plans(db: AsyncSession, skills) -> list[_SkillFetchPlan]:
    """Resolve the fetch target (and credential) for every skill in the batch.

    Runs while the DB connection is still checked out.  A skill with no bound
    integration keeps its raw URL and no headers — unchanged public behaviour.
    A skill *with* a bound integration is rewritten to the GitHub contents API
    so the read is authenticated and tracks the requested branch's HEAD.

    Credentials are memoised per integration for the duration of the batch:
    one repository typically hosts several skills, so the common case resolves
    (and decrypts) each credential once instead of once per skill.
    """
    from app.services.skill_source import (
        SkillCredentialError,
        contents_api_url,
        github_auth_headers,
        parse_github_skill_url,
        resolve_skill_credential,
    )

    # integration id -> resolved credential, or the error that resolving raised.
    resolved: dict[str, object] = {}

    async def _credential_for(integration_id) -> object:
        key = str(integration_id)
        if key not in resolved:
            try:
                resolved[key] = await resolve_skill_credential(db, integration_id)
            except SkillCredentialError as exc:
                resolved[key] = exc
        return resolved[key]

    plans: list[_SkillFetchPlan] = []
    for skill in skills:
        if not skill.url:
            continue
        integration_id = getattr(skill, "source_integration_id", None)
        if not integration_id:
            plans.append(_SkillFetchPlan(skill=skill, url=skill.url))
            continue

        source_ref = getattr(skill, "source_ref", None)
        source = parse_github_skill_url(skill.url, ref_override=source_ref)
        if source is None:
            # A credential is bound but the URL is not a GitHub repository file
            # URL.  Refuse rather than attaching the token to an arbitrary host
            # — sending a bearer token to an unintended origin would leak it.
            plans.append(
                _SkillFetchPlan(
                    skill=skill,
                    url=skill.url,
                    config_error=(
                        "the skill URL is not a GitHub repository file URL, so "
                        "the bound credential cannot be used"
                    ),
                )
            )
            continue

        credential = await _credential_for(integration_id)
        if isinstance(credential, SkillCredentialError):
            plans.append(
                _SkillFetchPlan(
                    skill=skill,
                    url=skill.url,
                    source=source,
                    config_error=str(credential),
                )
            )
            continue

        if credential is None:  # pragma: no cover - integration_id was truthy
            plans.append(_SkillFetchPlan(skill=skill, url=skill.url))
            continue

        plans.append(
            _SkillFetchPlan(
                skill=skill,
                url=contents_api_url(source, ref=source_ref),
                headers=github_auth_headers(credential.token),
                source=source,
                integration_name=credential.integration_name,
                route=credential.route,
            )
        )
    return plans


def _record_skill_auth_failure(
    plan: _SkillFetchPlan, now: datetime, detail: str, redacted_url: str | None
) -> None:
    """Handle an authentication failure on an authenticated skill refresh.

    An expired / revoked / under-scoped credential is a recoverable
    misconfiguration, so this path deliberately differs from the
    permanent-failure path in two ways: it advances a *separate*
    ``auth_failure_count`` (never ``refresh_failure_count``), and it never
    quarantines the skill.  ``fetched_at`` still moves to *now* so the retry
    stays on the daily cycle instead of every hour — a rotated credential then
    heals the skill on its own with no action on the skill itself.
    """
    skill = plan.skill
    skill.fetched_at = now
    new_count = (skill.auth_failure_count or 0) + 1
    skill.auth_failure_count = new_count

    logger.warning(
        "Authentication failure auto-refreshing skill '%s' (%s) via GitHub "
        "integration '%s': %s. The skill is NOT quarantined — rotate or "
        "re-authorize the credential and the next daily refresh recovers.",
        skill.name,
        redacted_url,
        plan.integration_name or "unknown",
        detail,
    )

    alert_threshold = settings.skill_refresh_auth_fail_alert_threshold
    if alert_threshold > 0 and new_count >= alert_threshold:
        logger.error(
            "Skill '%s' (%s) has failed authentication %d times in a row via "
            "GitHub integration '%s' — its content is now stale and will stay "
            "stale until the credential is fixed. Auto-refresh keeps retrying "
            "daily; the skill is still enabled.",
            skill.name,
            redacted_url,
            new_count,
            plan.integration_name or "unknown",
        )


async def _refresh_stale_skills(db: AsyncSession) -> int:
    """Auto-refresh URL-based skills whose content is older than 24 hours.

    Iterates all URL-based skills with a ``fetched_at`` timestamp older than
    ``SKILL_REFRESH_MAX_AGE_SECONDS`` (or NULL) and re-fetches their content
    from the source URL.  Failures are logged but do not abort the batch.

    Skills that have accumulated ``skill_refresh_quarantine_threshold``
    consecutive permanent (non-auth) 4xx failures are excluded from the refresh
    batch — their permanently-broken URLs are not retried until an operator
    resets the counter (by updating the URL or re-enabling the skill).

    A skill that binds a GitHub integration (``source_integration_id``) is
    fetched through the authenticated GitHub contents API instead, which is
    what makes a ``SKILL.md`` in a **private** repository refreshable.
    Authentication failures on that path are tracked separately in
    ``auth_failure_count`` and never quarantine the skill — see
    :func:`_record_skill_auth_failure`.
    """
    import httpx

    from app.models.agent_skill import AgentSkill
    from app.services.skill_source import parse_skill_frontmatter

    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=SKILL_REFRESH_MAX_AGE_SECONDS)

    quarantine_threshold = settings.skill_refresh_quarantine_threshold

    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.url.isnot(None),
            AgentSkill.enabled.is_(True),
            (AgentSkill.fetched_at.is_(None)) | (AgentSkill.fetched_at < stale_cutoff),
            # Skip skills whose URL has permanently failed too many times.
            # When threshold is 0 the feature is disabled (no filter added).
            *(
                [AgentSkill.refresh_failure_count < quarantine_threshold]
                if quarantine_threshold > 0
                else []
            ),
        )
    )
    skills = result.scalars().all()

    if not skills:
        return 0

    # Resolve each skill's fetch target (and decrypt any bound credential)
    # while the pooled connection is still available — the loop below runs
    # after it has been handed back.
    plans = await _build_skill_fetch_plans(db, skills)

    # Hand the pooled connection back before the sequential HTTP re-fetch
    # loop (30s timeout each) — the SELECT above opened a transaction on
    # the tick session. Skill mutations below are committed
    # explicitly at the end of this function.
    from app.core.database import release_db_connection_before_loop
    from app.services.skill_source import classify_github_failure, diagnose_not_found
    from app.services.web_fetch import _redact_url_for_log, _redact_urls_in_text

    await release_db_connection_before_loop(db)

    refreshed = 0
    failed = 0
    auth_failed = 0
    async with egress_client(timeout=30, follow_redirects=True) as client:
        for plan in plans:
            skill = plan.skill
            # Always log the skill's own URL, never the rewritten API URL —
            # it is what an operator recognises, and it is redacted here.
            redacted_url = _redact_url_for_log(skill.url)

            if plan.config_error:
                # Nothing to request: the bound credential is unusable.  This is
                # a credential/configuration problem, so it takes the auth path
                # (no quarantine) rather than being counted as a broken URL.
                _record_skill_auth_failure(plan, now, plan.config_error, redacted_url)
                _SKILL_REFRESH_OUTCOMES.labels(
                    outcome="auth", auth=plan.auth_label
                ).inc()
                failed += 1
                auth_failed += 1
                continue

            try:
                with egress_route(plan.route):
                    resp = await client.get(plan.url, headers=plan.headers)
                resp.raise_for_status()
                raw = resp.text

                # Parse frontmatter for metadata updates (uses the shared
                # parser that correctly handles YAML block scalars like >)
                meta, _ = parse_skill_frontmatter(raw)

                skill.content = raw
                skill.fetched_at = now
                skill.refresh_failure_count = 0
                skill.auth_failure_count = 0
                if meta.get("name"):
                    # One skill's name the column cannot hold would fail the
                    # flush of every skill refreshed with it.
                    if name_error := text_column_error(
                        AgentSkill.__table__.c.name, meta["name"], field="frontmatter name"
                    ):
                        logger.warning(
                            "Skill '%s' refreshed from %s keeps its name: %s",
                            skill.name,
                            redacted_url,
                            name_error,
                        )
                    else:
                        skill.name = meta["name"]
                if meta.get("description"):
                    skill.description = meta["description"]
                if meta.get("compatibility"):
                    skill.compatibility = meta["compatibility"]
                if meta.get("license"):
                    skill.license = meta["license"]

                refreshed += 1
                _SKILL_REFRESH_OUTCOMES.labels(
                    outcome="success", auth=plan.auth_label
                ).inc()
                logger.debug(
                    "Auto-refreshed skill '%s' from %s (%s fetch)",
                    skill.name,
                    redacted_url,
                    plan.auth_label,
                )

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if plan.sends_credentials:
                    # GitHub overloads its status codes: 403 doubles as "rate
                    # limit spent" and 404 hides "your credential cannot see
                    # this repository".  Classify before deciding the back-off.
                    kind = classify_github_failure(status, e.response.headers)
                    if kind == "not_found" and plan.source is not None:
                        with egress_route(plan.route):
                            kind = await diagnose_not_found(
                                client, plan.source, plan.headers or {}
                            )
                else:
                    # Unauthenticated URLs are classified by status alone:
                    # everything under 500 that is not 408/429 is a
                    # permanent client-side failure.
                    if status == 404:
                        kind = "not_found"
                    else:
                        kind = (
                            "client" if status < 500 and status not in (408, 429)
                            else "transient"
                        )

                failed += 1
                _SKILL_REFRESH_OUTCOMES.labels(
                    outcome=kind, auth=plan.auth_label
                ).inc()

                if kind == "auth":
                    auth_failed += 1
                    _record_skill_auth_failure(
                        plan,
                        now,
                        f"HTTP {status} {e.response.reason_phrase}",
                        redacted_url,
                    )
                    continue

                # Log only the status details — the httpx message embeds the
                # raw URL (query-string credentials included), and the URL is
                # already logged separately in redacted form.
                logger.warning(
                    "Failed to auto-refresh skill '%s' (%s): HTTP %s %s [%s]",
                    skill.name,
                    redacted_url,
                    status,
                    e.response.reason_phrase,
                    kind,
                )
                if kind in ("client", "not_found"):
                    # Permanent client-side failure (e.g. 404 Not Found):
                    # bump ``fetched_at`` to the full refresh window so the URL
                    # is not retried until the next 24 h cycle, and track how
                    # many consecutive such failures this URL has accumulated.
                    skill.fetched_at = now
                    new_fail_count = skill.refresh_failure_count + 1
                    skill.refresh_failure_count = new_fail_count
                    if quarantine_threshold > 0 and new_fail_count >= quarantine_threshold:
                        logger.error(
                            "Quarantining skill '%s' (%s) after %d consecutive "
                            "client-side failures — auto-refresh disabled. "
                            "Update the skill URL or re-enable the skill to "
                            "resume refresh.",
                            skill.name,
                            redacted_url,
                            new_fail_count,
                        )
                else:
                    # Transient failure — 5xx (e.g. 503 during a deployment),
                    # 429 (rate limiting) or 408 (request timeout): back off
                    # ~1 h to allow the server to recover without waiting a
                    # full day.  Don't count toward the quarantine threshold —
                    # a provider outage or throttle should not silently
                    # disable a skill.
                    skill.fetched_at = now - timedelta(hours=23)
            except Exception as e:
                logger.warning(
                    "Failed to auto-refresh skill '%s' (%s): %s",
                    skill.name,
                    redacted_url,
                    _redact_urls_in_text(str(e)),
                )
                _SKILL_REFRESH_OUTCOMES.labels(
                    outcome="transient", auth=plan.auth_label
                ).inc()
                # Network/timeout error (transient): short ~1 h back-off so
                # the skill is retried soon after connectivity is restored.
                # Transient errors do not count toward the quarantine threshold.
                skill.fetched_at = now - timedelta(hours=23)
                failed += 1

    if refreshed or failed:
        await db.commit()

    if auth_failed:
        logger.info(
            "Skill auto-refresh: %d refreshed, %d failed (%d authentication "
            "failures needing credential attention)",
            refreshed,
            failed,
            auth_failed,
        )

    return refreshed
