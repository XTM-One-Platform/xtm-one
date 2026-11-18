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

"""The Concierge's evaluations (#8257, PRD 6.8, decision B9).

:func:`launch_health_evaluation` creates an evaluation on a dataset the
Concierge built, with agents and flows as subjects (sandbox copies of targets of
its supervision scope: never a production agent or flow), marks it ``concierge_built`` - the mark the engine gate
reads to run it whatever the mode - and launches it through the same plan and
runs as every other launch (``services/evaluation/launch.py``).

:func:`compare_concierge_runs` compares two completed runs of one such
evaluation: the quality metric (``QUALITY_METRIC``) and every compared
score through ``compare_metric`` with its noise margin, cost, tokens and latency
beside them, and how many writes a model simulated in each (decision B17). A
run that is not complete, or lost entries to a revocation, gives no verdict.

Budget (PRD 6.12): a launch inside a cycle reserves on it, held past the
cycle's close (``budget.reserve(hold=...)``); a launch outside one passes the
budget gate. :func:`settle_health_evaluations`, on the Concierge's tick,
charges an evaluation that ended on the cycle that reserved for it, and stops
one whose measured spend passes a cap.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.dataset import Dataset, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationResult, EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.models.flow import Flow
from app.services.evaluation.launch import add_launch_runs, check_launchable, plan_launch
from app.services.evaluation.sandbox_cases import SANDBOX_KEY
from app.services.evaluation_comparison import COMPARED_METRICS, QUALITY_METRIC, compare_metric
from app.services.health import budget
from app.services.health.consent import in_supervision_scope
from app.services.health.datasets import DATASET_FAMILY

logger = logging.getLogger(__name__)

#: The run-level performance aggregates reported beside quality (means per entry).
PERFORMANCE_KEYS: tuple[str, ...] = (
    "performance.cost.llm",
    "performance.tokens.total",
    "performance.latency.wall_clock",
)
MAX_SUBJECTS = 10
#: Tokens one entry of one run is reserved at when the caller sizes nothing: the subject's and the judge's.
ENTRY_TOKENS_ESTIMATE = 25_000
#: The work kind a Concierge evaluation holds its reservation under.
HOLD_KIND = "evaluation"
_ACTIVE_RUN_STATUSES = ("pending", "queued", "running")
#: What a run stopped at a budget cap says; :func:`compare_concierge_runs` reads it back.
BUDGET_STOP_ERROR = "Stopped: the Concierge's budget is reached."


class HealthEvaluationError(Exception):
    """A launch or a comparison refused; the message is generic."""


@dataclass
class HealthLaunch:
    evaluation_id: uuid.UUID
    run_ids: list[uuid.UUID]


@dataclass
class RunComparison:
    baseline_run_id: uuid.UUID
    candidate_run_id: uuid.UUID
    #: ``progression`` / ``neutral`` / ``regression`` on the quality metric, or
    #: ``None`` when either run cannot give one (``reason`` says why).
    verdict: str | None
    quality_metric: str
    metrics: list[dict]
    performance: dict[str, dict[str, float | None]]
    simulated_writes: dict[str, int]
    reason: str | None = None
    #: ``reason`` as a code: ``budget`` (stopped at a cap), ``consent`` (entries
    #: withdrawn), ``failed`` (a run did not complete), ``unmeasured``.
    reason_code: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["baseline_run_id"], data["candidate_run_id"] = str(self.baseline_run_id), str(self.candidate_run_id)
        return data


async def evaluation_subject_refusal(db: AsyncSession, kind: str, subject_id: uuid.UUID) -> str | None:
    """Why the Concierge may not evaluate this subject (logged by the caller), ``None`` when it may.

    Only a sandbox copy (level 2), whose writes are intercepted: an evaluation
    runs its subject's tools, and a production agent or flow would write for
    real (decision B2). Its source, when it has one, is in the supervision
    scope and is not one of the fleet's agents.
    """
    from app.services.health.identity import is_health_agent
    from app.services.health.sandbox.copies import sandbox_copy_of

    row = await db.get(Agent if kind == "agent" else Flow, subject_id)
    if row is None or not getattr(row, "is_sandbox", False):
        return "not a sandbox copy"
    copy = await sandbox_copy_of(db, **{f"{kind}_id": subject_id})
    if copy is None:
        return "no sandbox copy record"
    if copy.source_id is None:
        return None  # a new flow prototype: nothing in production behind it
    if copy.source_type == "agent":
        source = await db.get(Agent, copy.source_id)
        if source is None or is_health_agent(source):
            return "the source is missing or a fleet agent"
    if not await in_supervision_scope(db, copy.source_type, copy.source_id):  # type: ignore[arg-type]
        return "the source is outside the supervision scope"
    return None


async def launch_health_evaluation(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    subject_agent_ids: Sequence[uuid.UUID] = (),
    subject_flow_ids: Sequence[uuid.UUID] = (),
    model_ids: Sequence[str] = (),
    name: str | None = None,
    cycle_id: uuid.UUID | None = None,
    reserve_tokens: int | None = None,
    reserve_cost_usd: Decimal = Decimal(0),
) -> HealthLaunch:
    """Create and launch a Concierge evaluation on *dataset_id*. Commits, then wakes the engine.

    *model_ids* empty: each agent runs on its own model; a flow always runs on
    its nodes' models. Raises :class:`HealthEvaluationError` (generic),
    ``launch.LaunchRefusedError`` (the launch checks' own message) or
    ``budget.BudgetRefusedError``. Inside *cycle_id* it reserves
    *reserve_tokens* / *reserve_cost_usd* (by default its entries at
    :data:`ENTRY_TOKENS_ESTIMATE`), held until its spend is charged; outside
    a cycle it passes the budget gate and books nothing.
    """
    from app.services.orchestration_engine import notify_orchestration_wake

    subjects = [("agent", s) for s in dict.fromkeys(subject_agent_ids)] + [
        ("flow", s) for s in dict.fromkeys(subject_flow_ids)
    ]
    if not subjects or len(subjects) > MAX_SUBJECTS:
        raise HealthEvaluationError(f"Name between 1 and {MAX_SUBJECTS} subjects.")
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None or not dataset.concierge_built:
        raise HealthEvaluationError("The dataset is not available.")
    for kind, subject_id in subjects:
        if await evaluation_subject_refusal(db, kind, subject_id) is not None:
            raise HealthEvaluationError("A subject is not available.")
    if cycle_id is None and (refusal := await budget.budget_refusal(db)) is not None:
        raise budget.BudgetRefusedError(refusal)

    # One savepoint: a refused reservation or launch leaves no evaluation behind.
    async with db.begin_nested():
        evaluation, runs = await _create_and_reserve(
            db, dataset, subjects, model_ids, name, cycle_id, reserve_tokens, reserve_cost_usd
        )
    await db.commit()
    await notify_orchestration_wake()
    return HealthLaunch(evaluation.id, [run.id for run in runs])


async def _create_and_reserve(
    db: AsyncSession,
    dataset: Dataset,
    subjects: list[tuple[str, uuid.UUID]],
    model_ids: Sequence[str],
    name: str | None,
    cycle_id: uuid.UUID | None,
    reserve_tokens: int | None,
    reserve_cost_usd: Decimal,
) -> tuple[Evaluation, list[EvaluationRun]]:
    from app.services.evaluation.availability import engine_accepts
    from app.services.evaluation_engine import stage_evaluation_execution

    evaluation = Evaluation(
        name=(name or f"Concierge - {dataset.name}")[:200],
        family=DATASET_FAMILY,
        execution_mode="full_loop",
        use_agent_default_model=not model_ids,
        model_ids=list(dict.fromkeys(model_ids)),
        trigger_config={},
        # Every answer is produced anew: a reused one would not be this copy's
        # under this case's recordings.
        reuse_default="off",
        concierge_built=True,
        enabled=False,  # no schedule, no run on agent change: the Concierge launches it
    )
    db.add(evaluation)
    await db.flush()
    db.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
    db.add_all(
        EvaluationAgent(evaluation_id=evaluation.id, **({"agent_id": s} if kind == "agent" else {"flow_id": s}))
        for kind, s in subjects
    )
    await db.flush()
    if not await engine_accepts(db, evaluation.id):
        raise HealthEvaluationError("Evaluation is not available.")
    await check_launchable(db, evaluation)
    plan = await plan_launch(db, evaluation, model_ids=list(model_ids), use_default=not model_ids)
    execution = await stage_evaluation_execution(db, str(evaluation.id))
    runs = await add_launch_runs(db, evaluation, plan, trigger_reason="concierge", owner_execution_id=execution.id)
    evaluation.last_triggered_at = datetime.now(UTC)
    if cycle_id is not None:
        entries = sum(run.total_entries or 0 for run in runs)
        tokens = reserve_tokens if reserve_tokens is not None else entries * ENTRY_TOKENS_ESTIMATE
        await budget.reserve(db, cycle_id, tokens=tokens, cost_usd=reserve_cost_usd, hold=(HOLD_KIND, evaluation.id))
    return evaluation, list(runs)


async def health_evaluation_runs(db: AsyncSession, evaluation_id: uuid.UUID) -> list[EvaluationRun]:
    """The runs of a Concierge evaluation; refused for any other evaluation."""
    evaluation = await db.get(Evaluation, evaluation_id)
    if evaluation is None or not evaluation.concierge_built:
        raise HealthEvaluationError("The evaluation is not available.")
    return list(
        (
            await db.execute(
                select(EvaluationRun)
                .where(EvaluationRun.evaluation_id == evaluation_id)
                .order_by(EvaluationRun.created_at)
            )
        ).scalars()
    )


async def _simulated_writes(db: AsyncSession, run_id: uuid.UUID) -> int:
    details = (
        await db.execute(select(EvaluationResult.execution_details).where(EvaluationResult.run_id == run_id))
    ).scalars()
    return sum(int((d or {}).get(SANDBOX_KEY, {}).get("simulated_writes") or 0) for d in details if isinstance(d, dict))


async def compare_concierge_runs(
    db: AsyncSession, *, baseline_run_id: uuid.UUID, candidate_run_id: uuid.UUID
) -> RunComparison:
    """Compare two runs of one Concierge evaluation (any two subjects: an agent and its variant, an agent and a flow)."""
    from app.services.evaluation.metrics import get_run_aggregates

    if baseline_run_id == candidate_run_id:
        raise HealthEvaluationError("Compare two different runs.")
    runs = {
        run.id: run
        for run in (
            await db.execute(select(EvaluationRun).where(EvaluationRun.id.in_([baseline_run_id, candidate_run_id])))
        ).scalars()
    }
    baseline, candidate = runs.get(baseline_run_id), runs.get(candidate_run_id)
    if baseline is None or candidate is None or baseline.evaluation_id != candidate.evaluation_id:
        raise HealthEvaluationError("The runs are not available.")
    evaluation = await db.get(Evaluation, baseline.evaluation_id)
    if evaluation is None or not evaluation.concierge_built:
        raise HealthEvaluationError("The runs are not available.")

    quality = QUALITY_METRIC
    metrics = [
        asdict(compare_metric(m, getattr(baseline, m), getattr(candidate, m)))
        for m in dict.fromkeys((quality, *COMPARED_METRICS))
    ]
    aggregates = {run.id: await get_run_aggregates(db, run.id) for run in (baseline, candidate)}
    performance = {
        key: {
            label: (aggregates[run.id].get(key) or {}).get("mean")
            for label, run in (("baseline", baseline), ("candidate", candidate))
        }
        for key in PERFORMANCE_KEYS
    }
    simulated = {
        "baseline": await _simulated_writes(db, baseline.id),
        "candidate": await _simulated_writes(db, candidate.id),
    }

    reason = code = None
    for label, run in (("baseline", baseline), ("candidate", candidate)):
        if run.status != "completed":
            reason = f"the {label} run is {run.status}"
            code = "budget" if run.status == "cancelled" and run.error == BUDGET_STOP_ERROR else "failed"
        elif (run.headline_details or {}).get("withdrawn_entries"):
            reason, code = f"the {label} run lost entries to a consent revocation", "consent"
    verdict = None if reason else next(m["verdict"] for m in metrics if m["metric"] == quality)
    if verdict is None and reason is None:
        reason, code = "the quality metric is not measured on both runs", "unmeasured"
    warnings = []
    if simulated["baseline"] or simulated["candidate"]:
        warnings.append(
            f"A model simulated {simulated['baseline']} write(s) in the baseline and "
            f"{simulated['candidate']} in the candidate: the results rest on them."
        )
    return RunComparison(
        baseline_run_id=baseline.id,
        candidate_run_id=candidate.id,
        verdict=verdict,
        quality_metric=quality,
        metrics=metrics,
        performance=performance,
        simulated_writes=simulated,
        reason=reason,
        reason_code=code,
        warnings=warnings,
    )


# ── Budget ────────────────────────────────────────────────────────────────────


async def evaluation_spend(db: AsyncSession, run_ids: Sequence[uuid.UUID]) -> tuple[int, Decimal]:
    """``(tokens, cost)`` the entries of *run_ids* measured so far (their ``performance`` metrics)."""
    if not run_ids:
        return 0, Decimal(0)
    sums = dict(
        (
            await db.execute(
                select(EvaluationMetric.key, func.sum(EvaluationMetric.value))
                .where(
                    EvaluationMetric.run_id.in_(list(run_ids)),
                    EvaluationMetric.key.in_(("performance.tokens.total", "performance.cost.llm")),
                )
                .group_by(EvaluationMetric.key)
            )
        ).all()
    )
    tokens = int(max(sums.get("performance.tokens.total") or 0, 0))
    cost = Decimal(str(round(max(sums.get("performance.cost.llm") or 0.0, 0.0), 6)))
    return tokens, cost


def _past_cap(left: budget.BudgetRemaining, held: budget.Reservation, tokens: int, cost: Decimal) -> bool:
    """Whether the spend measured so far passes what was reserved plus what is left under a cap."""
    token_left = [v for v in (left.cycle_tokens, left.month_tokens) if v is not None]
    cost_left = [v for v in (left.cycle_cost_usd, left.month_cost_usd) if v is not None]
    return bool(
        (token_left and tokens > held.tokens + min(token_left)) or (cost_left and cost > held.cost_usd + min(cost_left))
    )


async def settle_health_evaluations(db: AsyncSession, *, limit: int = 100) -> int:
    """Charge every held Concierge evaluation that ended on the cycle that reserved
    for it; stop the runs of one whose spend passes a cap. The number charged. Commits."""
    charged = 0
    for held in await budget.held_reservations(db, HOLD_KIND, limit=limit):
        try:
            charged += await _settle_one(db, held)
        except Exception:
            logger.exception("Concierge evaluation %s: its spend could not be settled", held.hold)
            await db.rollback()
    return charged


async def _settle_one(db: AsyncSession, held: budget.Reservation) -> int:
    """Charge *held*'s evaluation when it ended (1), or stop it past a cap (0). Commits."""
    evaluation_id = held.hold[1]  # type: ignore[index]
    runs = (
        await db.execute(
            select(EvaluationRun.id, EvaluationRun.status).where(EvaluationRun.evaluation_id == evaluation_id)
        )
    ).all()
    tokens, cost = await evaluation_spend(db, [run.id for run in runs])
    active = [run.id for run in runs if run.status in _ACTIVE_RUN_STATUSES]
    if not active:
        await budget.charge(db, held.cycle_id, tokens=tokens, cost_usd=cost, reservation=held)
        await db.commit()
        return 1
    if _past_cap(await budget.remaining(db, held.cycle_id), held, tokens, cost):
        # Each run reads its own status at its next entry and stops there.
        await db.execute(
            update(EvaluationRun)
            .where(EvaluationRun.id.in_(active), EvaluationRun.status.in_(_ACTIVE_RUN_STATUSES))
            .values(status="cancelled", error=BUDGET_STOP_ERROR, completed_at=datetime.now(UTC))
        )
        logger.info("Concierge evaluation %s stopped: its spend passes the budget", evaluation_id)
    await db.commit()
    return 0
