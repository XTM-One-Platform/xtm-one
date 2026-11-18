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

"""Switch-over, live watch and rollback of an agent-to-flow replacement (#8260, B12).

A ``switch_over`` shadow that held (``flow_shadow``) hands the assignment's
trigger to a production flow created from the sandbox copy only then: flow
active, assignment paused with ``concierge_switch_over``. The flow's live runs
are then watched against the assignment's baseline for the watch window
(decision B18: 7 days). A regression rolls back: assignment resumed, flow
deactivated, both audited as ``system:concierge``. A person's change meanwhile
(assignment resumed, flow edited or deactivated) is kept and nothing is undone
(``regression_kept``). ``watch_decision`` is a pure function of counted
figures, tested on fixed numbers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment import Assignment, AssignmentRun
from app.models.flow import FLOW_STATUS_ACTIVE, FLOW_STATUS_DRAFT, Flow
from app.models.flow_run import FlowRun
from app.models.flow_shadow import (
    SHADOW_STATUS_CANCELLED,
    SHADOW_STATUS_KEPT,
    SHADOW_STATUS_REGRESSION_KEPT,
    SHADOW_STATUS_ROLLED_BACK,
    SHADOW_STATUS_WATCHING,
    FlowShadow,
)
from app.services.health.flow_comparison import TRACE_WINDOW_SLACK
from app.services.health.flow_lab import trigger_takeover_refusal
from app.services.health.flow_shadow import (
    SUCCESS_MARGIN,
    ShadowVerdict,
    _close,
    _locked,
    _pct,
    _set_proposal_status,
    shadow_durations,
)

logger = logging.getLogger(__name__)

#: The audit actor of every automatic step (``system:concierge``, level 1).
SHADOW_ACTOR = "concierge"
SWITCH_OVER_PAUSE_REASON = "concierge_switch_over"

#: The watch: the failure-spike signal of B18 (above 5 % over the last 20
#: runs), or the baseline's failure rate plus the margin when it was higher;
#: the cost-drift signal (cost per run up 20 %).
WATCH_LAST_RUNS = 20
WATCH_MIN_RUNS = 5
FAILURE_SPIKE_RATE = 0.05
COST_DRIFT = 0.20

@dataclass(frozen=True, slots=True)
class LiveMetrics:
    runs: int
    failure_rate: float | None
    cost_per_run: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"runs": self.runs, "failure_rate": self.failure_rate, "cost_per_run": self.cost_per_run}


def watch_decision(current: LiveMetrics, baseline: dict | None) -> tuple[bool | None, str]:
    """``(regressed, note)`` for the flow's live runs against the assignment's baseline.

    ``None`` while there are fewer than ``WATCH_MIN_RUNS`` settled runs: no
    verdict yet, which the watch never reads as "kept" before its window ends.
    """
    if current.runs < WATCH_MIN_RUNS or current.failure_rate is None:
        return None, f"{current.runs} live runs so far, {WATCH_MIN_RUNS} needed for a verdict."
    baseline = baseline or {}
    base_failure = baseline.get("failure_rate")
    ceiling = max(FAILURE_SPIKE_RATE, (base_failure or 0.0) + SUCCESS_MARGIN)
    failed = round(current.failure_rate * current.runs)
    # More failures than B18 tolerates over its 20 runs, not just a rate: one
    # transient failure among the first few runs is noise, not a regression.
    if current.failure_rate > ceiling and failed > ceiling * WATCH_LAST_RUNS:
        return True, (
            f"The flow failed {_pct(current.failure_rate)} of its last {current.runs} runs "
            f"(assignment before: {_pct(base_failure)}, ceiling {_pct(ceiling)})."
        )
    base_cost = baseline.get("cost_per_run")
    if base_cost and current.cost_per_run is not None and current.cost_per_run > base_cost * (1 + COST_DRIFT):
        return True, (
            f"The flow costs {current.cost_per_run:.4f} per run against {base_cost:.4f} for the assignment "
            f"(+{round((current.cost_per_run / base_cost - 1) * 100)} %, {round(COST_DRIFT * 100)} % allowed)."
        )
    return False, (
        f"The flow failed {_pct(current.failure_rate)} of its last {current.runs} runs "
        f"(ceiling {_pct(ceiling)}); no regression."
    )


# ── Reads ──────────────────────────────────────────────────────────────────


async def assignment_live_metrics(db: AsyncSession, assignment_id: uuid.UUID, *, since: datetime) -> LiveMetrics:
    """The assignment's settled top-level runs since *since*: the watch's baseline."""
    from app.models.llm_trace import LLMTrace

    rows = (
        await db.execute(
            select(AssignmentRun.status, LLMTrace.total_cost)
            .outerjoin(LLMTrace, LLMTrace.id == AssignmentRun.llm_trace_id)
            .where(
                AssignmentRun.assignment_id == assignment_id,
                AssignmentRun.parent_run_id.is_(None),
                AssignmentRun.created_at >= since,
                AssignmentRun.status.in_(("completed", "failed")),
            )
        )
    ).all()
    return _metrics([(r.status, r.total_cost) for r in rows])


async def flow_live_metrics(db: AsyncSession, flow_id: uuid.UUID, *, since: datetime) -> LiveMetrics:
    """The flow's last ``WATCH_LAST_RUNS`` settled live runs since *since*."""
    from app.models.llm_trace import LLMTrace

    runs = list(
        (
            await db.execute(
                select(FlowRun.id, FlowRun.status)
                .where(
                    FlowRun.flow_id == flow_id,
                    FlowRun.parent_run_id.is_(None),
                    FlowRun.created_at >= since,
                    FlowRun.status.in_(("completed", "failed")),
                )
                .order_by(FlowRun.created_at.desc())
                .limit(WATCH_LAST_RUNS)
            )
        ).all()
    )
    costs: dict[str, float] = {}
    if runs:
        # Grouped by the label: PostgreSQL does not match two ``->>`` expressions
        # whose key is a separate bound parameter each.
        run_key = LLMTrace.metadata_json["flow_run_id"].astext.label("flow_run_key")
        cost_rows = (
            await db.execute(
                select(run_key, func.sum(LLMTrace.total_cost))
                .where(
                    LLMTrace.source == "flow",
                    # The (source, started_at) index bounds the scan to the watch window.
                    LLMTrace.started_at >= since - TRACE_WINDOW_SLACK,
                    LLMTrace.metadata_json["flow_run_id"].astext.in_([str(r.id) for r in runs]),
                )
                .group_by(run_key)
            )
        ).all()
        costs = {row[0]: float(row[1]) for row in cost_rows if row[1] is not None}
    return _metrics([(r.status, costs.get(str(r.id))) for r in runs])


def _metrics(rows: list[tuple[str, Any]]) -> LiveMetrics:
    if not rows:
        return LiveMetrics(0, None, None)
    failed = sum(1 for status, _ in rows if status == "failed")
    costs = [float(cost) for _, cost in rows if cost is not None]
    return LiveMetrics(len(rows), failed / len(rows), (sum(costs) / len(costs)) if costs else None)


async def _anchor_user_id(db: AsyncSession, *candidates: uuid.UUID | None) -> uuid.UUID | None:
    from app.models.user import User

    wanted = [c for c in candidates if c is not None]
    if not wanted:
        return None
    found = set((await db.execute(select(User.id).where(User.id.in_(wanted)))).scalars())
    return next((c for c in wanted if c in found), None)


async def _audit(db: AsyncSession, *, anchor: uuid.UUID | None, action: str, entity: Any, entity_type: str, summary: str, details: dict) -> None:
    from app.services.audit import log_system_audit

    if anchor is None:
        logger.warning("%s of %s %s has no user left to anchor its audit entry", action, entity_type, entity.id)
        return
    await log_system_audit(
        db,
        actor=SHADOW_ACTOR,
        anchor_user_id=anchor,
        action=action,
        entity_type=entity_type,
        entity_id=entity.id,
        entity_name=entity.name,
        summary=summary,
        details=details,
    )


async def materialize_flow(db: AsyncSession, shadow: FlowShadow, assignment: Assignment, copy: Flow) -> Flow:
    """The production flow an approved replacement becomes: the sandbox copy's graph, owned like the assignment.

    Owned by the identity the assignment's automatic runs act as (never the
    Concierge, which has no user row), company-managed when the assignment
    is, version 1 attributed to the approver.
    """
    from app.models.agent import Agent
    from app.services.flow_versioning import create_flow_version
    from app.services.trigger_integration_access import assignment_acting_user_id

    agent = await db.get(Agent, assignment.agent_id)
    flow = Flow(
        id=uuid.uuid4(),
        user_id=assignment_acting_user_id(assignment, agent),
        name=f"{assignment.name} (flow)"[:200],
        description=f"Replaces the assignment '{assignment.name}' after an approved Concierge proposal."[:2000],
        status=FLOW_STATUS_DRAFT,
        graph=dict(copy.graph or {}),
        version_number=1,
        is_company_managed=bool(assignment.is_company_managed),
        # The schedule carries on from the assignment's last firing, so the
        # take-over neither fires early nor skips a slot.
        last_triggered_at=assignment.last_triggered_at,
    )
    db.add(flow)
    await db.flush()
    await create_flow_version(
        db,
        flow_id=flow.id,
        graph=flow.graph,
        author_id=await _anchor_user_id(db, shadow.started_by_user_id),
        commit_message=f"Created from Concierge proposal {shadow.proposal_id}"[:200],
    )
    return flow


async def switch_over(db: AsyncSession, shadow: FlowShadow, assignment: Assignment, copy: Flow, *, now: datetime) -> Flow:
    """Hand the assignment's trigger to a production flow: flow active, assignment paused, audited."""
    from app.services.assignment_pause import pause_assignment, record_system_pause_audit

    durations = await shadow_durations(db)
    shadow.baseline = (
        await assignment_live_metrics(db, assignment.id, since=shadow.started_at - durations.watch)
    ).as_dict()
    flow = await materialize_flow(db, shadow, assignment, copy)
    flow.status = FLOW_STATUS_ACTIVE
    pause_assignment(
        assignment,
        reason=SWITCH_OVER_PAUSE_REASON,
        details=f"Replaced by the flow '{flow.name}' after an approved Concierge proposal.",
        actor_label="Concierge switch-over",
        now=now,
    )
    shadow.status = SHADOW_STATUS_WATCHING
    shadow.production_flow_id = flow.id
    # What the watch compares a later edit against: a person's edit is kept.
    shadow.verdict = {**(shadow.verdict or {}), "applied_version": flow.version_number}
    shadow.switched_at = now
    shadow.watch_until = now + durations.watch
    anchor = await _anchor_user_id(db, shadow.started_by_user_id, assignment.user_id, flow.user_id)
    details = {"flow_shadow_id": str(shadow.id), "proposal_id": str(shadow.proposal_id) if shadow.proposal_id else None}
    if anchor is not None:
        await record_system_pause_audit(
            db,
            assignment=assignment,
            actor=SHADOW_ACTOR,
            anchor_user_id=anchor,
            reason=SWITCH_OVER_PAUSE_REASON,
            details=f"Replaced by the flow '{flow.name}'.",
            extra={**details, "flow_id": str(flow.id)},
        )
    await _audit(
        db,
        anchor=anchor,
        action="flow.activate",
        entity=flow,
        entity_type="flow",
        summary=f"Created and activated flow '{flow.name}' in place of assignment '{assignment.name}'",
        details={**details, "assignment_id": str(assignment.id), "sandbox_flow_id": str(copy.id)},
    )
    await _set_proposal_status(db, shadow, "watching", shadow.note or "", watch_until=shadow.watch_until)
    return flow


def human_change(shadow: FlowShadow, assignment: Assignment, flow: Flow, applied_version: int | None) -> str | None:
    """What a person changed since the switch-over, if anything; then nothing is undone."""
    if assignment.enabled:
        return "the assignment was resumed"
    if assignment.paused_reason != SWITCH_OVER_PAUSE_REASON:
        return "the assignment was paused for another reason"
    if flow.status != FLOW_STATUS_ACTIVE:
        return "the flow was deactivated"
    if applied_version is not None and flow.version_number != applied_version:
        return "the flow was edited"
    return None


async def rollback_switch_over(
    db: AsyncSession, shadow: FlowShadow, assignment: Assignment, flow: Flow, *, reason: str, now: datetime
) -> None:
    """Undo a regressing switch-over: assignment resumed, flow deactivated, both audited.

    An assignment held by an emergency stop stays paused (the stop decides,
    ``assignment_pause.is_emergency_stopped``); the flow is deactivated anyway.
    """
    from app.services.assignment_pause import clear_pause_provenance, is_emergency_stopped

    flow.status = FLOW_STATUS_DRAFT
    resumed = not is_emergency_stopped(assignment)
    if resumed:
        assignment.enabled = True
        clear_pause_provenance(assignment)
    # The schedule carries on from the flow's last firing, so the resumed
    # assignment does not fire at once and re-process a slot.
    fired = [at for at in (assignment.last_triggered_at, flow.last_triggered_at) if at is not None]
    assignment.last_triggered_at = max(fired) if fired else None
    shadow.status = SHADOW_STATUS_ROLLED_BACK
    shadow.settled_at = now
    shadow.note = reason[:1000]
    anchor = await _anchor_user_id(db, shadow.started_by_user_id, assignment.user_id, flow.user_id)
    details = {
        "flow_shadow_id": str(shadow.id),
        "proposal_id": str(shadow.proposal_id) if shadow.proposal_id else None,
        "reason": reason,
    }
    await _audit(
        db,
        anchor=anchor,
        action="flow.deactivate",
        entity=flow,
        entity_type="flow",
        summary=f"Deactivated flow '{flow.name}' after a live regression",
        details={**details, "assignment_id": str(assignment.id)},
    )
    if resumed:
        await _audit(
            db,
            anchor=anchor,
            action="resume",
            entity=assignment,
            entity_type="assignment",
            summary=f"Resumed assignment '{assignment.name}' after the flow replacing it regressed",
            details={**details, "agent_id": str(assignment.agent_id), "flow_id": str(flow.id)},
        )
    await _set_proposal_status(db, shadow, "rolled_back", reason)


async def _is_the_approved_graph(db: AsyncSession, shadow: FlowShadow, copy: Flow) -> bool:
    """Whether *copy* still holds exactly the graph its proposal's approver read."""
    if shadow.proposal_id is None:
        return True
    from app.models.improvement_proposal import ImprovementProposal
    from app.services.flow_graph import auto_layout, normalize_graph

    proposal = await db.get(ImprovementProposal, shadow.proposal_id)
    graph = (proposal.change or {}).get("graph") if proposal is not None else None
    if not isinstance(graph, dict):
        return False
    return (copy.graph or {}) == normalize_graph(auto_layout(graph))


async def take_over(
    db: AsyncSession, shadow: FlowShadow, assignment: Assignment, copy: Flow, verdict: ShadowVerdict, *, now: datetime
) -> str:
    """The end of a held switch-over shadow: the switch-over, unless the assignment changed meanwhile."""
    from app.models.agent import Agent
    from app.services.assignment_pause import is_paused_by_deactivated_owner
    from app.services.trigger_integration_access import assignment_acting_user_id

    # Re-checked at the last moment: a person may have changed the trigger,
    # or the owner been deactivated, during the shadow.
    agent = await db.get(Agent, assignment.agent_id)
    refusal = trigger_takeover_refusal(assignment, agent, copy.graph)
    if refusal is None and not await _is_the_approved_graph(db, shadow, copy):
        refusal = "the sandbox flow no longer holds the graph that was approved"
    if refusal is None and assignment_acting_user_id(assignment, agent) is None:
        refusal = "the assignment has no identity its runs act as, so the flow's triggers could not fire"
    if refusal or not assignment.enabled or await is_paused_by_deactivated_owner(db, assignment):
        why = refusal or "the assignment is no longer running"
        return await _close(
            db, shadow, SHADOW_STATUS_CANCELLED, f"Not switched over: {why}.", "failed", now=now
        )
    shadow.note = verdict.note[:1000]
    await switch_over(db, shadow, assignment, copy, now=now)
    return shadow.status


async def advance_watch(db: AsyncSession, shadow: FlowShadow, assignment: Assignment, *, now: datetime) -> str:
    """One step of a switch-over's watch (the caller commits): rollback, kept, or still watching."""
    flow = await _locked(db, Flow, shadow.production_flow_id)
    if flow is None:
        return await _close(
            db, shadow, SHADOW_STATUS_REGRESSION_KEPT, "The flow was deleted during the watch.", "regression_kept", now=now
        )
    change = human_change(shadow, assignment, flow, (shadow.verdict or {}).get("applied_version"))
    current = await flow_live_metrics(db, flow.id, since=shadow.switched_at or shadow.ends_at)
    regressed, note = watch_decision(current, shadow.baseline)
    measured = regressed is not None
    shadow.verdict = {**(shadow.verdict or {}), "watch": {**current.as_dict(), "note": note, "measured": measured}}
    if regressed and change:
        return await _close(
            db,
            shadow,
            SHADOW_STATUS_REGRESSION_KEPT,
            f"{note} But {change} meanwhile: that change stays, nothing was rolled back.",
            "regression_kept",
            now=now,
        )
    if regressed:
        await rollback_switch_over(db, shadow, assignment, flow, reason=note, now=now)
        return shadow.status
    if shadow.watch_until is not None and now >= shadow.watch_until:
        # Too few live runs is said as such: the flow stays (nothing
        # regressed to roll back), but the watch measured nothing.
        return await _close(
            db, shadow, SHADOW_STATUS_KEPT, note if measured else f"Watch ended unmeasured: {note}", "kept", now=now
        )
    return shadow.status
