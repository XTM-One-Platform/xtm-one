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

"""The calls of the Quality Analyst and the Experimenter into levels 1 to 4 (#8259).

One adapter so the rest of level 6 names no lower-level module: when a lower
level renames a function, only this file changes. Each function is the
signature that level published on its issue (#8254, #8255, #8256, #8257);
level 6's tests stub them here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class BudgetUnavailableError(Exception):
    """Level 1 refused new work; ``code`` is its refusal code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Budget:
    """What is left of the Concierge's line (``None``: no cap)."""

    tokens: int | None
    cost_usd: Decimal | None
    refusal: str | None


@dataclass(frozen=True, slots=True)
class Subject:
    """One arm of an experiment, as level 4 evaluates it: a sandbox agent or flow."""

    kind: Literal["agent", "flow"]
    id: uuid.UUID
    #: The level 2 sandbox copy row behind it, for its simulated-write count.
    copy_id: uuid.UUID | None
    #: The evaluation overlay of this arm only (``skill_substitutes``, ...).
    overrides: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DatasetBuilt:
    dataset_id: uuid.UUID | None
    entries: int


@dataclass(frozen=True, slots=True)
class RunsCompared:
    """Level 4's comparison of the two arms, quality beside cost and latency."""

    finished: bool
    #: A run failed, was stopped, or lost entries to a withdrawal: no verdict.
    incomplete: bool
    quality_metric: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    verdict: str | None
    cost_usd: dict[str, float | None]
    latency_s: dict[str, float | None]
    #: Why there is no verdict, level 4's code: ``budget``, ``consent``, ``failed``, ``unmeasured``.
    reason: str | None = None


def _min(*values: Any) -> Any:
    present = [v for v in values if v is not None]
    return min(present) if present else None


# ── Level 1: identity, cycle and budget ──────────────────────────────────────


async def budget_left(db: AsyncSession, cycle_id: uuid.UUID | None) -> Budget:
    from app.services.concierge import budget

    left = await budget.remaining(db, cycle_id)
    return Budget(
        tokens=_min(left.cycle_tokens, left.month_tokens),
        cost_usd=_min(left.cycle_cost_usd, left.month_cost_usd),
        refusal=left.refusal,
    )


# ── Level 2: sandbox copies ──────────────────────────────────────────────────


async def create_agent_copy(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    created_for: str,
    created_by_agent_id: uuid.UUID | None,
    persona: str | None,
    source_trace_ids: list[uuid.UUID],
) -> tuple[uuid.UUID, uuid.UUID]:
    """A hidden copy of *agent_id* (``persona`` replaced when given): ``(copy id, copy agent id)``."""
    from app.services.concierge.sandbox.copies import SandboxOverrides, create_sandbox_copy

    copy = await create_sandbox_copy(
        db,
        source_type="agent",
        source_id=agent_id,
        created_for=created_for,
        created_by_agent_id=created_by_agent_id,
        overrides=SandboxOverrides(persona=persona) if persona is not None else None,
        source_trace_ids=source_trace_ids,
    )
    return copy.id, copy.copy_agent_id


async def simulated_writes(db: AsyncSession, *, copy_id: uuid.UUID, since: datetime | None) -> dict[str, int]:
    """Writes answered by a model in the copy's runs since *since*, per case."""
    from app.services.concierge.sandbox.runs import simulated_write_counts

    counts = await simulated_write_counts(db, copy_id=copy_id, since=since)
    return {str(case or ""): int(n) for case, n in counts.items()}


# ── Level 4: datasets, evaluations ───────────────────────────────────────────


class EvaluationUnavailableError(Exception):
    """Level 4 cannot measure this experiment (refused launch, or an arm it cannot run yet)."""


#: Level 4 runs each case once per subject, and a Concierge launch takes no
#: per-subject evaluation overlay yet (asked on #8257): one trial, and no skill
#: variant (its stand-in is such an overlay).
MAX_TRIALS = 1
SUBJECT_OVERLAYS_AVAILABLE = False


async def build_dataset(
    db: AsyncSession,
    *,
    target_type: str,
    target_id: uuid.UUID,
    since: datetime,
    max_entries: int,
) -> DatasetBuilt:
    """A Concierge dataset of *target*'s consented production cases; ``entries`` 0 when none could be built."""
    from app.services.concierge.datasets import DatasetBuildError, build_dataset_from_traces

    try:
        built = await build_dataset_from_traces(
            db, target_type=target_type, target_id=target_id, since=since, max_entries=max_entries
        )
    except DatasetBuildError:
        return DatasetBuilt(dataset_id=None, entries=0)
    return DatasetBuilt(dataset_id=built.dataset_id, entries=sum(int(n) for n in built.added.values()))


async def launch(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    name: str,
    baseline: Subject,
    variant: Subject,
    cycle_id: uuid.UUID,
    tokens: int,
    cost_usd: Decimal,
) -> tuple[uuid.UUID, list[uuid.UUID], list[uuid.UUID]]:
    """One evaluation with both arms on the same dataset, each on its own model:
    ``(evaluation, baseline runs, variant runs)``. Level 4 commits the launch and
    holds *tokens* / *cost_usd* on *cycle_id* until it charges what the runs
    spent; ``BudgetUnavailableError`` when level 1 refuses."""
    from sqlalchemy import select

    from app.models.evaluation import EvaluationRun
    from app.services.concierge.budget import BudgetRefusedError
    from app.services.concierge.evaluations import ConciergeEvaluationError, launch_concierge_evaluation
    from app.services.evaluation.launch import LaunchRefusedError

    if (baseline.overrides or variant.overrides) and not SUBJECT_OVERLAYS_AVAILABLE:
        raise EvaluationUnavailableError("per-arm overlay")
    arms = (baseline, variant)
    try:
        launched = await launch_concierge_evaluation(
            db,
            dataset_id=dataset_id,
            subject_agent_ids=[arm.id for arm in arms if arm.kind == "agent"],
            subject_flow_ids=[arm.id for arm in arms if arm.kind == "flow"],
            name=name,
            cycle_id=cycle_id,
            reserve_tokens=tokens,
            reserve_cost_usd=cost_usd,
        )
    except BudgetRefusedError as exc:
        raise BudgetUnavailableError(exc.code) from exc
    except (ConciergeEvaluationError, LaunchRefusedError) as exc:
        raise EvaluationUnavailableError(str(exc)) from exc
    runs = (
        await db.execute(select(EvaluationRun).where(EvaluationRun.id.in_(launched.run_ids)))
    ).scalars().all()
    return (
        launched.evaluation_id,
        [run.id for run in runs if run.subject_id == baseline.id],
        [run.id for run in runs if run.subject_id == variant.id],
    )


_RUNNING = frozenset({"pending", "queued", "running"})


async def compare(
    db: AsyncSession, *, baseline_run_ids: list[uuid.UUID], variant_run_ids: list[uuid.UUID]
) -> RunsCompared:
    """Level 4's comparison of the two arms' runs once both ended (one run per arm: one model each)."""
    from sqlalchemy import select

    from app.models.evaluation import EvaluationRun
    from app.services.concierge.evaluations import compare_concierge_runs

    if not baseline_run_ids or not variant_run_ids:
        raise EvaluationUnavailableError("an arm has no run")
    baseline_id, variant_id = baseline_run_ids[0], variant_run_ids[0]
    statuses = (
        await db.execute(select(EvaluationRun.status).where(EvaluationRun.id.in_([baseline_id, variant_id])))
    ).scalars().all()
    if any(status in _RUNNING for status in statuses) or len(statuses) < 2:
        return RunsCompared(
            finished=False, incomplete=False, quality_metric="score", baseline=None, candidate=None, delta=None,
            verdict=None, cost_usd={}, latency_s={},
        )
    compared = await compare_concierge_runs(db, baseline_run_id=baseline_id, candidate_run_id=variant_id)
    quality = next((m for m in compared.metrics if m["metric"] == compared.quality_metric), {})
    perf = compared.performance
    cost = perf.get("performance.cost.llm") or {}
    latency = perf.get("performance.latency.wall_clock") or {}
    return RunsCompared(
        finished=True,
        incomplete=compared.reason is not None,
        quality_metric=compared.quality_metric,
        baseline=quality.get("baseline"),
        candidate=quality.get("candidate"),
        delta=quality.get("delta"),
        verdict=compared.verdict,
        cost_usd={"baseline": cost.get("baseline"), "variant": cost.get("candidate")},
        latency_s={"baseline": latency.get("baseline"), "variant": latency.get("candidate")},
        reason=compared.reason_code,
    )


# ── Level 3: proposals ───────────────────────────────────────────────────────


class ProposalRefusedError(ValueError):
    """Level 3 refused the filing; its message is generic (reasons in its logs)."""


async def file_proposal(db: AsyncSession, *, author_agent: Any, **spec: Any) -> tuple[uuid.UUID, bool]:
    """File the proposal *spec* describes: ``(proposal id, created)``; an open duplicate is returned."""
    from app.services.improvement_proposals import ProposalError, ProposalSpec
    from app.services.improvement_proposals import file_proposal as _file

    try:
        filed = await _file(db, spec=ProposalSpec(**spec), author_agent=author_agent)
    except ProposalError as exc:
        raise ProposalRefusedError(exc.detail) from exc
    return filed.proposal.id, bool(filed.created)
