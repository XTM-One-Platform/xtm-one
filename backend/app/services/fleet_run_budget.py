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

"""Optional per-fleet run budget: runs and tokens per rolling hour (#1526).

``Fleet.run_budget`` (``{runs_per_hour, tokens_per_hour}``, each optional;
NULL = no budget, the default) bounds what the HOME members of a fleet start
in the last hour. When a free slot claims due assignment runs
(``AssignmentAdapter.deal_claims``), the due runs of a fleet at or over either
bound are HELD: left out of the claim, never failed, still queued. They are
claimed again by the first claim after the fleet's usage falls back under its
budget. A delegated child of a live parent is never held (its parent holds a
slot waiting for it).

A held run's queue-wait bound (``deadline_at`` of an execution no worker has
started yet, ``orchestration_engine.queued_deadline``) counts from its latest
hold, never from its first due time: each claim that holds it pushes that
bound to the claim time plus the run's own queue window (recorded once under
:data:`QUEUE_WINDOW_PAYLOAD_KEY`), so ``sweep_deadlines`` never fails a run
for the time the budget held it (``deadline_exceeded_before_start``). Once the
fleet is back under budget, the ordinary bound applies from the last hold.

The fleet's owner gets one notification-bell entry per fleet and per hour (a
Redis ``SET NX`` key; a Redis that cannot answer sends nothing, so a claim
storm is never a notification storm), the durable record of the hold.

Usage counts the runs of the home members started in the last hour and the
tokens of those runs written so far (a run still running adds its tokens when
it ends). ``AssignmentRun.started_at`` is set when a worker starts the run, so a
queued or held run never counts toward its own fleet's budget. A fleet with no
budget costs one indexed read per claim and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: The rolling window a fleet budget counts over.
BUDGET_WINDOW = timedelta(hours=1)
NOTICE_KEY_PREFIX = "fleet:run_budget_notified:"
#: A little over the window: one notice per fleet and per hour.
NOTICE_TTL_SECONDS = 3600
NOTIFICATION_SOURCE = "fleet_run_budget"
SOURCE_LABEL = "Fleet run budget"
_REDIS_TIMEOUT_SECONDS = 0.5
#: Where a held execution records its queue window (seconds from due to queue-wait bound) at its first hold.
QUEUE_WINDOW_PAYLOAD_KEY = "run_budget_queue_window_seconds"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def held_queue_deadline(execution: Any, now: datetime) -> tuple[datetime, float] | None:
    """The queue-wait bound a held *execution* gets at *now*, with its queue window; ``None``: nothing to move.

    Only for work no worker started yet whose ``deadline_at`` is its queue-wait bound (a pending execution
    carrying the start budget and no start). The window is the one recorded at its first hold, else its
    ``deadline_at - run_at``. The bound never moves back.
    """
    from app.services.orchestration_engine import START_BUDGET_PAYLOAD_KEY, STARTED_AT_PAYLOAD_KEY

    payload = getattr(execution, "payload", None) or {}
    deadline = getattr(execution, "deadline_at", None)
    if (
        deadline is None
        or (getattr(execution, "state", None) or "pending") != "pending"
        or payload.get(STARTED_AT_PAYLOAD_KEY)
        or not isinstance(payload.get(START_BUDGET_PAYLOAD_KEY), (int, float))
    ):
        return None
    window = payload.get(QUEUE_WINDOW_PAYLOAD_KEY)
    if isinstance(window, bool) or not isinstance(window, (int, float)):
        run_at = getattr(execution, "run_at", None)
        if run_at is None:
            return None
        window = (_aware(deadline) - _aware(run_at)).total_seconds()
    if window <= 0:
        return None
    return max(_aware(deadline), now + timedelta(seconds=float(window))), float(window)


async def keep_queue_clock(db: AsyncSession, held: list[Any], now: datetime) -> int:
    """Push the queue-wait bound of each *held* execution to *now* plus its queue window; how many moved.

    One guarded ``UPDATE`` per execution (still pending, still not started), so a row a worker claimed or the
    sweep dead-lettered meanwhile is left as it is.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_engine import STARTED_AT_PAYLOAD_KEY

    moved = 0
    for execution in held:
        fresh = held_queue_deadline(execution, now)
        if fresh is None:
            continue
        deadline, window = fresh
        await db.execute(
            update(OrchestrationExecution)
            .where(
                OrchestrationExecution.id == execution.id,
                OrchestrationExecution.state == "pending",
                ~OrchestrationExecution.payload.has_key(STARTED_AT_PAYLOAD_KEY),
            )
            .values(
                deadline_at=func.greatest(OrchestrationExecution.deadline_at, deadline),
                payload=OrchestrationExecution.payload.op("||")(literal({QUEUE_WINDOW_PAYLOAD_KEY: window}, JSONB)),
            )
            .execution_options(synchronize_session=False)
        )
        moved += 1
    return moved


def normalized_budget(budget: Any) -> dict[str, int]:
    """The positive bounds of a stored ``run_budget`` (``{}``: no budget)."""
    if not isinstance(budget, dict):
        return {}
    bounds: dict[str, int] = {}
    for key in ("runs_per_hour", "tokens_per_hour"):
        value = budget.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        bounds[key] = value
    return bounds


def over_budget(budget: Any, *, runs: int, tokens: int) -> str | None:
    """Why a fleet with *budget* that started *runs* runs and spent *tokens* tokens in the window is over it.

    ``None`` when it is under every bound, or has none.
    """
    bounds = normalized_budget(budget)
    if "runs_per_hour" in bounds and runs >= bounds["runs_per_hour"]:
        return f"{runs} runs started in the last hour, budget {bounds['runs_per_hour']} runs per hour"
    if "tokens_per_hour" in bounds and tokens >= bounds["tokens_per_hour"]:
        return f"{tokens} tokens spent in the last hour, budget {bounds['tokens_per_hour']} tokens per hour"
    return None


async def _budgeted_fleets(db: AsyncSession, agent_ids: set[uuid.UUID]) -> dict[str, tuple[uuid.UUID, Any]]:
    """``{agent id: (fleet id, run_budget)}`` for the agents whose home fleet has a budget."""
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember

    rows = (
        await db.execute(
            select(FleetMember.agent_id, Fleet.id, Fleet.run_budget)
            .join(Fleet, Fleet.id == FleetMember.fleet_id)
            .where(
                FleetMember.agent_id.in_(list(agent_ids)),
                FleetMember.role == FLEET_ROLE_MEMBER,
                Fleet.run_budget.is_not(None),
            )
        )
    ).all()
    return {str(agent): (fleet, budget) for agent, fleet, budget in rows if normalized_budget(budget)}


async def _usage(db: AsyncSession, fleet_ids: set[uuid.UUID], now: datetime) -> dict[uuid.UUID, tuple[int, int]]:
    """``{fleet id: (runs, tokens)}`` of the home members of *fleet_ids* over the window ending at *now*."""
    from app.models.assignment import AssignmentRun
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    tokens = func.coalesce(
        func.sum(
            func.coalesce(AssignmentRun.total_input_tokens, 0) + func.coalesce(AssignmentRun.total_output_tokens, 0)
        ),
        0,
    )
    rows = (
        await db.execute(
            select(FleetMember.fleet_id, func.count(AssignmentRun.id), tokens)
            .join(AssignmentRun, AssignmentRun.agent_id == FleetMember.agent_id)
            .where(
                FleetMember.fleet_id.in_(list(fleet_ids)),
                FleetMember.role == FLEET_ROLE_MEMBER,
                AssignmentRun.started_at >= now - BUDGET_WINDOW,
            )
            .group_by(FleetMember.fleet_id)
        )
    ).all()
    return {fleet: (int(runs or 0), int(spent or 0)) for fleet, runs, spent in rows}


async def hold_over_budget(db: AsyncSession, ordered: list[Any], *, now: datetime | None = None) -> list[Any]:
    """*ordered* (due assignment executions in claim order) without the runs of a fleet over its budget.

    The order of what stays is kept. An execution whose run cannot be read, a
    delegated child (a run with a parent) and the runs of an agent outside any
    budgeted fleet are never held.
    """
    from app.models.assignment import AssignmentRun

    if not ordered:
        return ordered
    run_of: dict[Any, uuid.UUID] = {}
    for execution in ordered:
        run_id = safe_uuid((getattr(execution, "payload", None) or {}).get("run_id"))
        if run_id is not None:
            run_of[execution.id] = run_id
    if not run_of:
        return ordered
    runs = {
        row[0]: (row[1], row[2])
        for row in (
            await db.execute(
                select(AssignmentRun.id, AssignmentRun.agent_id, AssignmentRun.parent_run_id).where(
                    AssignmentRun.id.in_(list(run_of.values()))
                )
            )
        ).all()
    }
    agents = {agent for agent, _parent in runs.values() if agent is not None}
    if not agents:
        return ordered
    budgeted = await _budgeted_fleets(db, agents)
    if not budgeted:
        return ordered
    now = now or datetime.now(UTC)
    usage = await _usage(db, {fleet for fleet, _budget in budgeted.values()}, now)
    over: dict[uuid.UUID, str] = {}
    for fleet, budget in budgeted.values():
        runs_used, tokens_used = usage.get(fleet, (0, 0))
        reason = over_budget(budget, runs=runs_used, tokens=tokens_used)
        if reason is not None:
            over[fleet] = reason
    if not over:
        return ordered
    kept: list[Any] = []
    held: dict[uuid.UUID, int] = {}
    held_executions: list[Any] = []
    for execution in ordered:
        run = runs.get(run_of.get(execution.id))
        if run is None or run[1] is not None:
            kept.append(execution)
            continue
        home = budgeted.get(str(run[0]))
        if home is None or home[0] not in over:
            kept.append(execution)
            continue
        held[home[0]] = held.get(home[0], 0) + 1
        held_executions.append(execution)
    # The hold is not queue time: a held run is never failed by its queue-wait bound.
    await keep_queue_clock(db, held_executions, now)
    for fleet, count in held.items():
        await notify_over_budget_once(fleet, over[fleet], held=count, now=now)
    return kept


def notice_key(fleet_id: Any, now: datetime) -> str:
    """The Redis key that marks *fleet_id*'s notice of the UTC hour of *now* as sent."""
    return f"{NOTICE_KEY_PREFIX}{fleet_id}:{now.astimezone(UTC).strftime('%Y-%m-%dT%H')}"


async def _first_of_the_hour(fleet_id: Any, now: datetime) -> bool:
    """Whether this call takes the fleet's notice of the hour; ``False`` when Redis cannot answer."""
    try:
        from app.core.redis import redis_client

        redis = await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
        taken = await asyncio.wait_for(
            redis.set(notice_key(fleet_id, now), "1", nx=True, ex=NOTICE_TTL_SECONDS),
            _REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("Fleet run budget notice not taken for fleet %s: %s", fleet_id, format_exception_message(exc))
        return False
    return bool(taken)


async def notify_over_budget_once(fleet_id: Any, reason: str, *, held: int, now: datetime) -> bool:
    """Tell the fleet's owner, once per hour, that its runs are held over its run budget; whether it was sent.

    Never raises; runs in a session of its own, so the claim's is never touched.
    """
    logger.info("Fleet %s is over its run budget (%s): %d due run(s) held", fleet_id, reason, held)
    if not await _first_of_the_hour(fleet_id, now):
        return False
    try:
        from app.core.database import async_session
        from app.models.fleet import Fleet
        from app.services.notify import notify_web_user

        async with async_session() as db:
            fleet = await db.get(Fleet, fleet_id)
            if fleet is None or fleet.user_id is None:
                return False
            await notify_web_user(
                db,
                str(fleet.user_id),
                title=f"Fleet '{fleet.name}' is over its run budget: new runs are held",
                summary=(
                    f"{reason}. Its new runs wait in the queue, none is failed, and they start again as soon as "
                    "the last hour's usage is back under the budget. Change the budget on the fleet's settings. "
                    "This notice is sent once an hour per fleet."
                ),
                source_label=SOURCE_LABEL,
                extra_data={
                    "notification_source": NOTIFICATION_SOURCE,
                    "fleet_id": str(fleet_id),
                    "reason": reason,
                    "held": held,
                    "path": f"/fleets/{fleet_id}",
                },
            )
            await db.commit()
        return True
    except Exception as exc:
        logger.warning(
            "Fleet run budget notice for fleet %s could not be sent: %s", fleet_id, format_exception_message(exc)
        )
        return False
