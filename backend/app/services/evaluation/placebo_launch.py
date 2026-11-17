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

"""What a placebo launch of an evaluation would run, and what refuses it.

The trigger and the estimate (``dev-docs/agent-placebo-evaluation.md`` FR-11 to
FR-14, FR-20) both read :func:`plan_placebo_launch` on the launch plan
``launch.plan_launch`` built: the runs per agent x model x arm x trial, the
targets an agent cannot take, the refusals (no configuration, an execution or
callback mode that cannot run it, no control arm, the execution cap, a placebo
that would carry a word of what it replaces) and the tool sources whose
repeated calls need a confirmation. Every placebo is built here in
characters, which proves it can be built; the runs build it again in their
model's tokens.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.evaluation import Evaluation, EvaluationPlaceboComparison, EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.fanout import resolve_effective_models
from app.services.evaluation.launch import LaunchPlan
from app.services.evaluation.placebo_arms import (
    ARM_REAL,
    MAX_COMPARISON_EXECUTIONS,
    ComparisonPlan,
    PlannedRun,
    placebo_seed,
    plan_comparison,
    target_sources,
    write_tool_sources,
)
from app.services.evaluation_placebo import (
    CHARACTER_MEASURER,
    MAX_RESCALE_ROUNDS,
    PlaceboConfig,
    PlaceboLeakError,
    build_placebo,
    parse_placebo_config,
    placebo_mode_error,
)

#: The metric a comparison's token cost and its estimate read (FR-22).
TOKENS_METRIC = "performance.tokens.total"


@dataclass
class PlaceboLaunch:
    """A placebo launch as it would run now."""

    config: PlaceboConfig | None
    seed: str
    plan: ComparisonPlan = field(default_factory=ComparisonPlan)
    entries: int = 0
    #: Corpus versions the placebos were drawn from.
    corpus_versions: set[str] = field(default_factory=set)
    #: At most this many token probes: every part of every placebo measured up
    #: to its last rescale, plus one bare probe, per placebo run.
    probe_calls: int = 0
    #: Agent name -> its tool sources, when the evaluation runs their calls unapproved.
    write_tool_sources: dict[str, list[str]] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def executions(self) -> int:
        return self.plan.executions(self.entries)

    def waves(self, concurrency: int) -> int:
        """How many rounds of *concurrency* runs the launch takes: what scales its deadline."""
        return max(1, math.ceil(len(self.plan.runs) / max(concurrency, 1)))


async def plan_placebo_launch(
    db: AsyncSession,
    evaluation: Evaluation,
    launch: LaunchPlan,
    *,
    seed: str,
) -> PlaceboLaunch:
    """The placebo launch of ``evaluation`` on the agents and models of ``launch``.

    ``seed`` is the comparison's: the placebos built here are the ones its runs
    build again in tokens. A model-independent agent gets no arm: no model call
    to influence (FR-3).
    """
    from app.services.evaluation.placebo_runs import load_placebo_target_sources
    from app.services.evaluation_engine import evaluation_owner_id

    config = parse_placebo_config(evaluation.placebo_config)
    placebo = PlaceboLaunch(config=config, seed=seed, entries=launch.entry_count)
    if config is None:
        placebo.refusals.append("This evaluation has no placebo configuration")
        return placebo
    mode_error = placebo_mode_error(config, evaluation.execution_mode)
    if mode_error:
        placebo.refusals.append(mode_error)
    if (evaluation.trigger_config or {}).get("await_callback"):
        placebo.refusals.append(
            "A placebo comparison cannot wait for external callbacks: "
            "an external system would have to answer every arm and trial"
        )
    if launch.flow_ids:
        # A placebo replaces part of an agent's configuration (#8257).
        placebo.refusals.append("A placebo comparison applies to agents only: remove the flow subjects")
    if evaluation.run_mode == "sequential":
        placebo.warnings.append("sequential_run_mode")

    owner_id = evaluation_owner_id(evaluation)
    agents = (await db.execute(select(Agent).where(Agent.id.in_(launch.agent_ids)))).scalars().all()
    by_id = {agent.id: agent for agent in agents}
    planned_agents = []
    for agent_id in launch.agent_ids:
        agent = by_id.get(agent_id)
        if agent is None:
            continue
        if agent_id in launch.skip_llm_agent_ids:
            placebo.plan.skipped.extend(
                {
                    "agent_id": str(agent_id),
                    "agent_name": agent.name,
                    "target": target.key,
                    "reason": "a model-independent agent calls no model",
                }
                for target in config.targets
            )
            continue
        default_mid, model_list = launch.agent_model_map.get(agent_id, (None, []))
        models, _ = resolve_effective_models(agent_id, launch.skip_llm_agent_ids, model_list, default_mid)
        sources = await load_placebo_target_sources(db, agent, user_id=owner_id)
        planned_agents.append((str(agent_id), agent.name, models, sources))
        for target in config.targets:
            elements, reason = target_sources(target, sources)
            if reason is not None:
                continue
            try:
                build = await build_placebo(
                    target.kind,
                    elements,
                    agent_name=agent.name,
                    seed=placebo_seed(seed, str(agent_id), target.key),
                    measurer=CHARACTER_MEASURER,
                    reserved_names=sources.names,
                    custom_text=config.custom_text,
                )
            except PlaceboLeakError as err:
                placebo.refusals.append(f"Agent '{agent.name}': {err}")
                continue
            placebo.corpus_versions.add(build.corpus_version)
            parts = sum(len(element.lengths) for element in build.elements)
            placebo.probe_calls += len(models) * config.trials_per_entry * (1 + parts * (MAX_RESCALE_ROUNDS + 2))
        if evaluation.execution_mode == "full_loop" and not evaluation.require_tool_approval:
            sources_that_write = write_tool_sources(agent)
            if sources_that_write:
                placebo.write_tool_sources[agent.name] = sources_that_write

    skipped = placebo.plan.skipped
    placebo.plan = plan_comparison(config, planned_agents, seed=seed)
    placebo.plan.skipped[:0] = skipped
    if not any(run.arm != ARM_REAL for run in placebo.plan.runs):
        placebo.refusals.append("No agent of this evaluation has any of the placebo targets")
    if placebo.executions > MAX_COMPARISON_EXECUTIONS:
        placebo.refusals.append(
            f"This comparison would run {placebo.executions} agent executions "
            f"({len(placebo.plan.runs)} runs x {placebo.entries} entries); "
            f"the limit is {MAX_COMPARISON_EXECUTIONS}"
        )
    return placebo


def new_comparison(evaluation: Evaluation, placebo: PlaceboLaunch, *, created_by: uuid.UUID | None) -> EvaluationPlaceboComparison:
    """The comparison row of a launch, with its frozen configuration and warnings. Not added.

    Every warning is one object with a ``code``: ``sequential_run_mode``, or
    ``target_skipped`` with the agent, the target and why it has no arm.
    """
    if placebo.config is None:
        raise ValueError("A launch with no placebo configuration has no comparison")
    return EvaluationPlaceboComparison(
        id=uuid.uuid4(),
        evaluation_id=evaluation.id,
        status="running",
        seed=placebo.seed,
        corpus_version=",".join(sorted(placebo.corpus_versions)) or None,
        config=placebo.config.model_dump(mode="json"),
        created_by=created_by,
        warnings=[
            *({"code": code} for code in placebo.warnings),
            *({"code": "target_skipped", **skipped} for skipped in placebo.plan.skipped),
        ],
    )


async def estimate_tokens(
    db: AsyncSession, evaluation: Evaluation, runs: Sequence[PlannedRun], entries: int
) -> int | None:
    """Agent tokens the runs would use, from what each agent x model used before (FR-12).

    Per agent x model, the mean ``performance.tokens.total`` of one entry on its
    latest completed run outside any comparison. ``None`` while one pair has
    never been measured: a partial sum would understate the comparison.
    """
    means = await _mean_entry_tokens(db, evaluation.id)
    total = 0
    for run in runs:
        mean = means.get((run.agent_id, run.model_id))
        if mean is None:
            return None
        total += round(mean * entries)
    return total


async def _mean_entry_tokens(db: AsyncSession, evaluation_id: uuid.UUID) -> dict[tuple[str, str], float]:
    """``(agent_id, model_id)`` -> the mean tokens of one entry on that pair's latest measured run."""
    measured = (
        EvaluationRun.evaluation_id == evaluation_id,
        EvaluationRun.status == "completed",
        EvaluationRun.placebo_comparison_id.is_(None),
        EvaluationMetric.key == TOKENS_METRIC,
        EvaluationMetric.value.isnot(None),
    )
    latest = (
        select(EvaluationRun.id, EvaluationRun.agent_id, EvaluationRun.model_id)
        .join(EvaluationMetric, EvaluationMetric.run_id == EvaluationRun.id)
        .where(*measured)
        .distinct(EvaluationRun.agent_id, EvaluationRun.model_id)
        .order_by(EvaluationRun.agent_id, EvaluationRun.model_id, EvaluationRun.created_at.desc())
        .subquery()
    )
    rows = (
        await db.execute(
            select(latest.c.agent_id, latest.c.model_id, func.avg(EvaluationMetric.value))
            .join(EvaluationMetric, EvaluationMetric.run_id == latest.c.id)
            .where(EvaluationMetric.key == TOKENS_METRIC, EvaluationMetric.value.isnot(None))
            .group_by(latest.c.agent_id, latest.c.model_id)
        )
    ).all()
    return {(str(agent_id), model_id): float(mean) for agent_id, model_id, mean in rows if mean is not None}
