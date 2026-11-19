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

"""Platform Health signals (#8261, PRD 6.3, decisions B13 and B18): thresholds read without a model call.

Four signals, each evaluated by the scheduler on its own cadence and only on
the targets of the supervision scope, at the metrics level (counts, rates,
costs, model ids: no content is read, so no opt-in is needed and nothing is
audited):

========================  ===============================================  ==================================
Signal                    Rule (defaults, ``HealthSignalSettings``)     Reads
========================  ===============================================  ==================================
``feedback_drop``         weighted score of the last 3 days 10 points      ratings given or changed in the two
                          under the 3 days before, >= 10 ratings now       windows (``annotations``)
``failure_spike``         > 5 % of the last 20 settled runs failed, for    the last N runs of the assignments
                          an assignment or a flow                          and flows that ran since last pass
``cost_drift``            cost per run of an agent's assignment runs,      ``llm_daily_stats`` (daily rollup)
                          last 7 days vs the 7 before, up >= 20 %
``model_change``          the model an agent resolves to changed           the agents' own columns
========================  ===============================================  ==================================

**Cost on large trace stores (PRD 13).** No signal reads ``llm_traces``,
``llm_spans`` or ClickHouse: every input above lives in PostgreSQL whatever
``TRACING_STORE_PROVIDER`` says. Feedback and failure passes run every 15
minutes and read only the rows of their windows through existing indexes
(``(agent_id, updated_at)`` on annotations, ``created_at`` / ``(flow_id,
created_at)`` on runs) and at most N rows per target that moved, from the
last 90 days (``FAILURE_LOOKBACK``); cost and
model passes run hourly, the cost one on at most ``2 x window`` daily rows per
agent.

Platform Health's own sandbox copies (``is_sandbox``) are never read: their
runs are its experiments, not a target's health.

**Edge-triggered** (``HealthSignalState``): a crossing starts one
investigation cycle on its target; staying crossed starts nothing; clearing
re-arms it. A crossing refused for a passing reason (budget, licence) stays
un-fired so the next pass tries again; one refused because a cycle is already
open on the target is consumed - that cycle covers it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, get_args

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_cycle import HealthScheduleMark, HealthSignalState
from app.services.health.settings import HealthSignalSettings

logger = logging.getLogger(__name__)

SignalKind = Literal["feedback_drop", "failure_spike", "cost_drift", "model_change"]
SIGNAL_KINDS: Final[tuple[str, ...]] = get_args(SignalKind)

#: How often each signal is evaluated.
CADENCE: Final[dict[str, timedelta]] = {
    "feedback_drop": timedelta(minutes=15),
    "failure_spike": timedelta(minutes=15),
    "cost_drift": timedelta(hours=1),
    "model_change": timedelta(hours=1),
}
#: The pass marks share ``concierge_schedule_marks`` with the schedules.
_MARK_KEYS: Final[dict[str, str]] = {
    "feedback_drop": "signal:feedback",
    "failure_spike": "signal:failure",
    "cost_drift": "signal:cost",
    "model_change": "signal:model",
}
#: Oldest run the failure window reads: a target idle longer has no recent window.
FAILURE_LOOKBACK: Final = timedelta(days=90)
#: Most cycles one pass of one signal starts; the rest fire on the next pass.
MAX_FIRES_PER_PASS: Final = 10
#: A refusal after which the crossing is retried on the next pass.
_RETRY_REFUSALS: Final = frozenset(
    {
        "inactive",
        "paused",
        "no_assignment",
        "start_failed",
        "concierge_disabled",
        "ai_disabled",
        "license_required",
        "transient",
        "cycle_budget_reached",
        "month_budget_reached",
    }
)
#: Signals that are events, not levels: the new value is recorded only once a
#: cycle takes the change, so a change not fired yet is seen again next pass.
_EVENT_KINDS: Final = frozenset({"model_change"})
_ENABLED_FIELD: Final[dict[str, str]] = {
    "feedback_drop": "feedback_enabled",
    "failure_spike": "failure_enabled",
    "cost_drift": "cost_enabled",
    "model_change": "model_change_enabled",
}


@dataclass(frozen=True)
class SignalReading:
    """One signal read on one target. ``crossed`` is the threshold verdict."""

    kind: str
    target_type: str
    target_id: uuid.UUID
    crossed: bool
    value: float | str | None
    baseline: float | str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_signal(self) -> dict[str, Any]:
        """What the cycle and its run receive: ids, codes and numbers only."""
        data = asdict(self)
        data["target_id"] = str(self.target_id)
        data.pop("crossed")
        return data


# ── Pure rules (unit-tested on fixed numbers) ───────────────────────────────


def feedback_crossed(current: float | None, previous: float | None, ratings: int, s: HealthSignalSettings) -> bool:
    """Scores in 0..1; the drop is in points (x100)."""
    if current is None or previous is None or ratings < s.feedback_min_ratings:
        return False
    return round((previous - current) * 100, 6) >= s.feedback_drop_points


def failure_crossed(statuses: list[str], s: HealthSignalSettings) -> tuple[bool, float | None]:
    """*statuses*: the last settled runs, newest first. Needs a full window."""
    window = statuses[: s.failure_last_runs]
    if len(window) < s.failure_last_runs:
        return False, None
    rate = 100 * sum(1 for status in window if status == "failed") / len(window)
    return round(rate, 6) > s.failure_rate_pct, rate


def cost_crossed(
    current_cost: float, current_runs: int, previous_cost: float, previous_runs: int, s: HealthSignalSettings
) -> tuple[bool, float | None, float | None]:
    """Cost per run of each window; crossed when up at least ``cost_increase_pct``."""
    if current_runs < s.cost_min_runs or previous_runs < s.cost_min_runs:
        return False, None, None
    current, previous = current_cost / current_runs, previous_cost / previous_runs
    if previous <= 0:
        return False, current, previous
    return round((current - previous) / previous * 100, 6) >= s.cost_increase_pct, current, previous


# ── Readers ─────────────────────────────────────────────────────────────────


async def read_feedback(db: AsyncSession, scope, s: HealthSignalSettings, now: datetime) -> list[SignalReading]:
    from app.models.annotation import Annotation
    from app.models.chat import Conversation, Message, MessageFeedback
    from app.services.annotations import agents_weighted_score_window

    window = timedelta(days=s.feedback_window_days)
    since = now - window
    rated = set(
        (
            await db.execute(
                select(Annotation.agent_id).where(Annotation.updated_at >= since).group_by(Annotation.agent_id)
            )
        ).scalars()
    )
    rated |= set(
        (
            await db.execute(
                select(Conversation.agent_id)
                .join(Message, Message.conversation_id == Conversation.id)
                .join(MessageFeedback, MessageFeedback.message_id == Message.id)
                .where(MessageFeedback.updated_at >= since, Conversation.agent_id.is_not(None))
                .group_by(Conversation.agent_id)
            )
        ).scalars()
    )
    rated |= await _active_targets(db, "feedback_drop", "agent")
    agent_ids = [agent_id for agent_id in rated if agent_id is not None and scope.allows_metrics(agent_id=agent_id)]
    if not agent_ids:
        return []
    current = await agents_weighted_score_window(db, agent_ids, since=since, until=now)
    previous = await agents_weighted_score_window(db, agent_ids, since=since - window, until=since)
    readings = []
    for agent_id in agent_ids:
        cur, prev = current[agent_id], previous[agent_id]
        readings.append(
            SignalReading(
                kind="feedback_drop",
                target_type="agent",
                target_id=agent_id,
                crossed=feedback_crossed(cur.score, prev.score, cur.ratings, s),
                value=None if cur.score is None else round(cur.score * 100, 1),
                baseline=None if prev.score is None else round(prev.score * 100, 1),
                detail={
                    "ratings": cur.ratings,
                    "previous_ratings": prev.ratings,
                    "window_days": s.feedback_window_days,
                },
            )
        )
    return readings


async def last_settled_statuses(
    db: AsyncSession, model: Any, key_col: Any, ids: list[uuid.UUID], limit: int, *, created_after: datetime
) -> dict[uuid.UUID, list[str]]:
    """The last *limit* settled statuses (``completed`` / ``failed``) per key, newest first.

    Shared with the Platform Health agent check ``agents.failure_spike`` (one rule, two readers).
    """
    ranked = (
        select(
            key_col.label("key"),
            model.status.label("status"),
            func.row_number().over(partition_by=key_col, order_by=model.created_at.desc()).label("rn"),
        )
        .where(key_col.in_(ids), model.status.in_(("completed", "failed")), model.created_at >= created_after)
        .subquery()
    )
    rows = await db.execute(
        select(ranked.c.key, ranked.c.status).where(ranked.c.rn <= limit).order_by(ranked.c.key, ranked.c.rn)
    )
    found: dict[uuid.UUID, list[str]] = {}
    for key, status in rows.all():
        found.setdefault(key, []).append(status)
    return found


async def read_failures(
    db: AsyncSession, scope, s: HealthSignalSettings, now: datetime, since: datetime
) -> list[SignalReading]:
    """Assignments and flows with a run settled since *since* (the last pass)."""
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.flow import Flow
    from app.models.flow_run import FlowRun

    # A run settles after it is created: bounded by the indexed ``created_at``
    # (a run outliving a day is the timeout's business, not a signal's).
    created_after = since - timedelta(days=1)
    moved_assignments = (
        await db.execute(
            select(AssignmentRun.assignment_id, AssignmentRun.agent_id)
            .join(Agent, Agent.id == AssignmentRun.agent_id)
            .where(
                Agent.is_sandbox.is_(False),
                AssignmentRun.created_at >= created_after,
                AssignmentRun.completed_at >= since,
                AssignmentRun.status.in_(("completed", "failed")),
            )
            .group_by(AssignmentRun.assignment_id, AssignmentRun.agent_id)
        )
    ).all()
    assignment_ids = [aid for aid, agent_id in moved_assignments if aid and scope.allows_metrics(agent_id=agent_id)]
    moved_flows = (
        (
            await db.execute(
                select(FlowRun.flow_id)
                .join(Flow, Flow.id == FlowRun.flow_id)
                .where(
                    Flow.is_sandbox.is_(False),
                    FlowRun.created_at >= created_after,
                    FlowRun.completed_at >= since,
                    FlowRun.status.in_(("completed", "failed")),
                )
                .group_by(FlowRun.flow_id)
            )
        )
        .scalars()
        .all()
    )
    flow_ids = [fid for fid in moved_flows if scope.allows_metrics(flow_id=fid)]

    readings: list[SignalReading] = []
    for target_type, model, key_col, ids in (
        ("assignment", AssignmentRun, AssignmentRun.assignment_id, assignment_ids),
        ("flow", FlowRun, FlowRun.flow_id, flow_ids),
    ):
        if not ids:
            continue
        statuses = await last_settled_statuses(
            db, model, key_col, ids, s.failure_last_runs, created_after=now - FAILURE_LOOKBACK
        )
        for target_id, last in statuses.items():
            crossed, rate = failure_crossed(last, s)
            readings.append(
                SignalReading(
                    kind="failure_spike",
                    target_type=target_type,
                    target_id=target_id,
                    crossed=crossed,
                    value=None if rate is None else round(rate, 1),
                    baseline=s.failure_rate_pct,
                    detail={"runs": len(last), "failed": sum(1 for x in last if x == "failed")},
                )
            )
    return readings


async def read_costs(db: AsyncSession, scope, s: HealthSignalSettings, now: datetime) -> list[SignalReading]:
    """Cost per assignment run from the daily rollup, whole days only (today is partial)."""
    from app.models.agent import Agent
    from app.models.llm_trace import LLMDailyStats

    today = now.date()
    window = timedelta(days=s.cost_window_days)
    current_from, previous_from = today - window, today - 2 * window
    period = LLMDailyStats.date >= current_from
    rows = await db.execute(
        select(
            LLMDailyStats.agent_id,
            period.label("current"),
            func.coalesce(func.sum(LLMDailyStats.total_cost), 0),
            func.coalesce(func.sum(LLMDailyStats.trace_count), 0),
        )
        .join(Agent, Agent.id == LLMDailyStats.agent_id)
        .where(
            Agent.is_sandbox.is_(False),
            LLMDailyStats.source == "assignment",
            LLMDailyStats.agent_id.is_not(None),
            LLMDailyStats.date >= previous_from,
            LLMDailyStats.date < today,
        )
        .group_by(LLMDailyStats.agent_id, period)
    )
    sums: dict[uuid.UUID, dict[bool, tuple[float, int]]] = {}
    for agent_id, is_current, cost, runs in rows.all():
        sums.setdefault(agent_id, {})[bool(is_current)] = (float(cost), int(runs))
    readings = []
    for agent_id, by_period in sums.items():
        if not scope.allows_metrics(agent_id=agent_id):
            continue
        cur_cost, cur_runs = by_period.get(True, (0.0, 0))
        prev_cost, prev_runs = by_period.get(False, (0.0, 0))
        crossed, cur, prev = cost_crossed(cur_cost, cur_runs, prev_cost, prev_runs, s)
        readings.append(
            SignalReading(
                kind="cost_drift",
                target_type="agent",
                target_id=agent_id,
                crossed=crossed,
                value=None if cur is None else round(cur, 6),
                baseline=None if prev is None else round(prev, 6),
                detail={"runs": cur_runs, "previous_runs": prev_runs, "window_days": s.cost_window_days},
            )
        )
    return readings


async def _resolved_models(db: AsyncSession, scope) -> dict[uuid.UUID, str]:
    """``{agent id: the model it resolves to}`` (``llm.agent_model.agent_model_selection``'s order).

    An agent with no model of its own reads the platform default; with none
    either, the user-dependent tiers decide per run, which no signal can read:
    it resolves to ``default``.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.llm.agent_model import LLM_MODEL_SOURCE_ROUTING
    from app.models.agent import Agent
    from app.services.flow_llm import routing_model

    platform_default = getattr(await get_platform_settings(db), "agent_default_model", None) or "default"
    routing, _ = await routing_model(db)
    rows = await db.execute(
        select(Agent.id, Agent.llm_provider, Agent.llm_model, Agent.llm_model_source).where(
            Agent.enabled.is_(True), Agent.is_sandbox.is_(False)
        )
    )
    resolved: dict[uuid.UUID, str] = {}
    for agent_id, provider, model, source in rows.all():
        if not scope.allows_metrics(agent_id=agent_id):
            continue
        if source == LLM_MODEL_SOURCE_ROUTING:
            resolved[agent_id] = routing or platform_default
        elif model:
            resolved[agent_id] = f"{provider}/{model}" if provider else model
        else:
            resolved[agent_id] = platform_default
    return resolved


async def read_models(db: AsyncSession, scope) -> list[SignalReading]:
    resolved = await _resolved_models(db, scope)
    if not resolved:
        return []
    states = {
        state.target_id: state
        for state in (
            await db.execute(
                select(HealthSignalState).where(
                    HealthSignalState.kind == "model_change", HealthSignalState.target_type == "agent"
                )
            )
        ).scalars()
    }
    readings = []
    for agent_id, model in resolved.items():
        state = states.get(agent_id)
        previous = (state.last_value or {}).get("value") if state is not None else None
        readings.append(
            SignalReading(
                kind="model_change",
                target_type="agent",
                target_id=agent_id,
                # A first sighting records the model, it changes nothing.
                crossed=previous is not None and previous != model,
                value=model,
                baseline=previous,
            )
        )
    return readings


async def _active_targets(db: AsyncSession, kind: str, target_type: str) -> set[uuid.UUID]:
    return set(
        (
            await db.execute(
                select(HealthSignalState.target_id).where(
                    HealthSignalState.kind == kind,
                    HealthSignalState.target_type == target_type,
                    HealthSignalState.active.is_(True),
                )
            )
        ).scalars()
    )


# ── State and firing ────────────────────────────────────────────────────────


def _last_value(reading: SignalReading) -> dict[str, Any]:
    return {"value": reading.value, "baseline": reading.baseline, **reading.detail}


async def record_readings(db: AsyncSession, readings: list[SignalReading], *, now: datetime) -> list[SignalReading]:
    """Store each reading; the crossings (inactive -> crossed) to fire. Flushed."""
    if not readings:
        return []
    kinds = {reading.kind for reading in readings}
    existing = {
        (state.kind, state.target_type, state.target_id): state
        for state in (await db.execute(select(HealthSignalState).where(HealthSignalState.kind.in_(kinds)))).scalars()
    }
    crossings = []
    for reading in readings:
        key = (reading.kind, reading.target_type, reading.target_id)
        state = existing.get(key)
        if state is None:
            state = HealthSignalState(kind=reading.kind, target_type=reading.target_type, target_id=reading.target_id)
            db.add(state)
            existing[key] = state
        state.evaluated_at = now
        if reading.crossed and not state.active:
            crossings.append(reading)
            if reading.kind in _EVENT_KINDS:
                continue
        elif not reading.crossed:
            state.active = False
        state.last_value = _last_value(reading)
    await db.flush()
    return crossings


async def fire_signals(db: AsyncSession, crossings: list[SignalReading], *, now: datetime) -> int:
    """Start one investigation cycle per crossing, at most :data:`MAX_FIRES_PER_PASS`; the number started. Commits."""
    from app.services.health.cycles import start_cycle

    started = 0
    for reading in crossings[:MAX_FIRES_PER_PASS]:
        result = await start_cycle(
            db,
            key="investigation",
            trigger="signal",
            target_type=reading.target_type,
            target_id=reading.target_id,
            signal=reading.as_signal(),
        )
        state = (
            await db.execute(
                select(HealthSignalState).where(
                    HealthSignalState.kind == reading.kind,
                    HealthSignalState.target_type == reading.target_type,
                    HealthSignalState.target_id == reading.target_id,
                )
            )
        ).scalar_one()
        state.refusal = result.refusal
        if result.started or result.refusal not in _RETRY_REFUSALS:
            # Started, or nothing a later pass would change (a cycle is already
            # open on the target, the target left the scope): consumed.
            state.active = True
            state.fired_at = now
            state.cycle_id = result.cycle_id if result.started else result.open_cycle_id
        if reading.kind in _EVENT_KINDS:
            # An event, not a level: re-armed at once, so the next change
            # fires again; the change itself is recorded once taken.
            if state.active:
                state.last_value = _last_value(reading)
            state.active = False
        await db.commit()
        started += int(result.started)
        if not result.started:
            logger.info(
                "Platform Health signal %s on %s %s: no cycle (%s)",
                reading.kind,
                reading.target_type,
                reading.target_id,
                result.refusal,
            )
    return started


async def evaluate_signal(db: AsyncSession, kind: str, *, now: datetime, since: datetime) -> list[SignalReading]:
    """Read one signal over the scope (no model call)."""
    from app.services.health.consent import load_consent_scope
    from app.services.health.settings import load_health_settings

    s = (await load_health_settings(db)).signals
    scope = await load_consent_scope(db)
    if kind == "feedback_drop":
        return await read_feedback(db, scope, s, now)
    if kind == "failure_spike":
        return await read_failures(db, scope, s, now, since)
    if kind == "cost_drift":
        return await read_costs(db, scope, s, now)
    return await read_models(db, scope)


async def signals_tick(*, now: datetime | None = None) -> int:
    """Evaluate every enabled signal whose cadence is due, then fire its crossings; cycles started."""
    from app.core.database import async_session
    from app.services.health.settings import load_health_settings

    now = now or datetime.now(UTC)
    started = 0
    for kind in SIGNAL_KINDS:
        async with async_session() as db:
            settings = await load_health_settings(db)
            if not settings.enabled or not getattr(settings.signals, _ENABLED_FIELD[kind]):
                continue
            mark = await db.get(HealthScheduleMark, _MARK_KEYS[kind])
            if mark is not None and now - mark.fired_at < CADENCE[kind]:
                continue
            since = mark.fired_at if mark is not None else now - CADENCE[kind]
            try:
                readings = await evaluate_signal(db, kind, now=now, since=since)
                crossings = await record_readings(db, readings, now=now)
                await _mark_pass(db, kind, now)
                await db.commit()
            except Exception:
                logger.exception("Platform Health signal %s: evaluation failed", kind)
                await db.rollback()
                # Tried again at the signal's next cadence, not on every tick.
                await _mark_pass(db, kind, now)
                await db.commit()
                continue
            started += await fire_signals(db, crossings, now=now)
    return started


async def _mark_pass(db: AsyncSession, kind: str, now: datetime) -> None:
    mark = await db.get(HealthScheduleMark, _MARK_KEYS[kind])
    if mark is None:
        db.add(HealthScheduleMark(schedule=_MARK_KEYS[kind], fired_at=now))
    else:
        mark.fired_at = now


async def list_signal_states(
    db: AsyncSession,
    *,
    active_only: bool = True,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[HealthSignalState]:
    """The stored signal states, newest evaluation first (the caller filters by its own access rule)."""
    query = select(HealthSignalState)
    if active_only:
        query = query.where(HealthSignalState.active.is_(True))
    if target_id is not None:
        query = query.where(
            and_(HealthSignalState.target_type == target_type, HealthSignalState.target_id == target_id)
        )
    return list((await db.execute(query.order_by(HealthSignalState.evaluated_at.desc()).limit(limit))).scalars())
