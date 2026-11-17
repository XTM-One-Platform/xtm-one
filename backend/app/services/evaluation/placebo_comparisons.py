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

"""A placebo comparison once its runs have run: settled, measured and served (level 3).

``dev-docs/agent-placebo-evaluation.md`` FR-22 to FR-29. A comparison settles
when none of its runs is pending or running any more: ``completed`` when every
run completed, else ``failed`` or ``cancelled``. Settling computes its results
(``placebo_stats``), from the vertical's stored metrics of the counted results
(``aggregation.result_counts_in_headlines``), whatever the status: a cell
whose runs did not all complete has no verdict.

The execution settles the comparisons of its runs when its batch ends
(``execute_evaluation``); every read settles any it finds finished, so a
comparison whose worker stopped still settles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import case, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.evaluation import Evaluation, EvaluationPlaceboComparison, EvaluationResult, EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.aggregation import result_counts_in_headlines
from app.services.evaluation.metric_package import is_quality_key
from app.services.evaluation.placebo_arms import ARM_NO_TARGET, ARM_PLACEBO, ARM_REAL
from app.services.evaluation.placebo_stats import Cell, MetricValue, Trial, comparison_statistics
from app.services.evaluation.scorers.registry import judge_enabled
from app.services.evaluation_placebo import PlaceboTarget

logger = logging.getLogger(__name__)

TOKENS_KEY = "performance.tokens.total"
LATENCY_KEY = "performance.latency.wall_clock"
TOOL_CALLS_KEY = "performance.counts.tool_calls"
#: The tool that opens a skill (``builtin_tools.skills.build_skill_tools``).
LOAD_SKILL_TOOL = "load_skill"

ACTIVE_RUN_STATUSES = frozenset({"pending", "running"})
SETTLED_STATUSES = frozenset({"completed", "failed", "cancelled"})


def settled_status(run_statuses: Iterable[str]) -> str | None:
    """What a comparison settles as, or ``None`` while a run may still run.

    Only the three final run statuses settle it: an unknown one waits.
    """
    statuses = set(run_statuses)
    if not statuses:
        return "cancelled"
    if not statuses <= SETTLED_STATUSES:
        return None
    return next((status for status in ("cancelled", "failed") if status in statuses), "completed")


def _opens(asked: str, names: frozenset[str]) -> bool:
    """Whether ``load_skill(asked)`` opens one of ``names``, under its own name or the
    catalog's ``" (n)"`` suffix of a duplicate (``skills._assign_catalog_names``)."""
    asked = asked.strip().casefold()
    return any(asked == name or (asked.startswith(f"{name} (") and asked.endswith(")")) for name in names)


def _loaded_skill(traces: object, names: frozenset[str] | None) -> bool:
    """Whether a result's timeline opened one of ``names`` with ``load_skill`` (any skill when ``None``)."""
    for step in traces if isinstance(traces, list) else ():
        if not isinstance(step, dict) or step.get("tool_name") != LOAD_SKILL_TOOL:
            continue
        if names is None:
            return True
        raw = step.get("tool_input")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            parsed = None
        asked = parsed.get("name") if isinstance(parsed, dict) else raw
        if isinstance(asked, str) and _opens(asked, names):
            return True
    return False


async def _skill_loads_read(
    db: AsyncSession, runs: Sequence[EvaluationRun]
) -> dict[tuple[uuid.UUID, str, str], frozenset[str] | None]:
    """Per agent x model x skill target whose loads are measured, the names the real arm opens it by.

    The real skill's own name (``None`` for a ``skills`` target: any skill
    counts); the placebo arm opens its placebo's names, read from each run's
    snapshot. A pinned skill is never opened (its body is in the prompt), so
    a target on one is not measured.
    """
    targets = {
        (run.agent_id, run.model_id, _key(run)): PlaceboTarget.model_validate(run.placebo_target)
        for run in runs
        if run.arm == ARM_PLACEBO and (run.placebo_target or {}).get("kind") in {"skill", "skills"}
    }
    if not targets:
        return {}
    agents = {
        agent.id: agent
        for agent in (
            await db.execute(select(Agent).where(Agent.id.in_({agent_id for agent_id, _, _ in targets})))
        ).scalars()
    }
    single = {target.skill_id for target in targets.values() if target.kind == "skill"}
    live = (
        dict((await db.execute(select(AgentSkill.id, AgentSkill.name).where(AgentSkill.id.in_(single)))).all())
        if single
        else {}
    )
    measured: dict[tuple[uuid.UUID, str, str], frozenset[str] | None] = {}
    for (agent_id, model_id, key), target in targets.items():
        if target.kind == "skills":
            measured[(agent_id, model_id, key)] = None
            continue
        agent = agents.get(agent_id)
        if agent is None or str(target.skill_id) in set(agent.always_load_skill_ids):
            continue
        name = live.get(target.skill_id)
        measured[(agent_id, model_id, key)] = frozenset({name.casefold()}) if name else frozenset()
    return measured


def _key(run: EvaluationRun) -> str:
    return PlaceboTarget.model_validate(run.placebo_target).key


def _placebo_names(run: EvaluationRun) -> frozenset[str]:
    return frozenset(str(element.get("name", "")).casefold() for element in (run.placebo_snapshot or {}).get("elements") or [])


async def comparison_cells(
    db: AsyncSession, comparison: EvaluationPlaceboComparison, evaluation: Evaluation
) -> list[Cell]:
    """One cell per agent x model x target, each arm's counted results by entry."""
    runs = list(
        (
            await db.execute(
                select(EvaluationRun).where(EvaluationRun.placebo_comparison_id == comparison.id)
            )
        ).scalars()
    )
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    skill_loads = await _skill_loads_read(db, runs)
    judge_on = judge_enabled(evaluation.scorer_selection)

    metrics: dict[uuid.UUID, dict[str, MetricValue]] = {}
    for row in await db.execute(
        select(
            EvaluationMetric.result_id,
            EvaluationMetric.key,
            EvaluationMetric.value,
            EvaluationMetric.numerator,
            EvaluationMetric.denominator,
            EvaluationMetric.class_counts,
        ).where(EvaluationMetric.run_id.in_(run_ids), EvaluationMetric.value.isnot(None))
    ):
        metrics.setdefault(row.result_id, {})[str(row.key)] = MetricValue(
            value=float(row.value),
            numerator=row.numerator,
            denominator=row.denominator,
            class_counts=row.class_counts,
        )

    # Only the results of a measured skill target's real and placebo runs are
    # read for their timeline.
    traced = [
        run.id
        for run in runs
        if (run.arm == ARM_REAL and any(key[:2] == (run.agent_id, run.model_id) for key in skill_loads))
        or (run.arm == ARM_PLACEBO and run.placebo_target and (run.agent_id, run.model_id, _key(run)) in skill_loads)
    ]
    # Only a skill target's results are read for their timeline.
    traces = (
        case((EvaluationResult.run_id.in_(traced), EvaluationResult.execution_details["traces"]), else_=null())
        if traced
        else null()
    )
    #: Each run's trials by entry, with the result's timeline when it was read.
    by_run: dict[uuid.UUID, dict[str, list[tuple[Trial, object]]]] = {run_id: {} for run_id in run_ids}
    for row in await db.execute(
        select(
            EvaluationResult.id,
            EvaluationResult.run_id,
            EvaluationResult.entry_id,
            EvaluationResult.score,
            traces.label("traces"),
        ).where(EvaluationResult.run_id.in_(run_ids), result_counts_in_headlines())
    ):
        values = metrics.get(row.id, {})

        def _measure(key: str, values=values) -> float | None:
            metric = values.get(key)
            return metric.value if metric is not None else None

        trial = Trial(
            quality={key: metric for key, metric in values.items() if is_quality_key(key)},
            tokens=_measure(TOKENS_KEY),
            judge=float(row.score) if judge_on and row.score is not None else None,
            latency=_measure(LATENCY_KEY),
            tool_calls=_measure(TOOL_CALLS_KEY),
        )
        by_run[row.run_id].setdefault(str(row.entry_id), []).append((trial, row.traces))

    names = {
        agent_id: name
        for agent_id, name in await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_({run.agent_id for run in runs}))
        )
    }

    def _merged(
        arm_runs: Sequence[EvaluationRun], opens: Callable[[EvaluationRun], frozenset[str] | None] | None = None
    ) -> dict[str, list[Trial]]:
        """The arm's trials by entry; with ``opens``, each says whether it opened those skill names."""
        merged: dict[str, list[Trial]] = {}
        for run in arm_runs:
            for entry, trials in by_run[run.id].items():
                merged.setdefault(entry, []).extend(
                    trial if opens is None else replace(trial, skill_loaded=_loaded_skill(timeline, opens(run)))
                    for trial, timeline in trials
                )
        return merged

    groups: dict[tuple, list[EvaluationRun]] = {}
    for run in runs:
        target = None if run.arm == ARM_REAL else _key(run)
        groups.setdefault((run.agent_id, run.model_id, run.arm, target), []).append(run)

    cells: list[Cell] = []
    for (agent_id, model_id, arm, target_key), placebo_runs in groups.items():
        real_runs = groups.get((agent_id, model_id, ARM_REAL, None))
        if arm != ARM_PLACEBO or not real_runs:
            continue
        no_target_runs = groups.get((agent_id, model_id, ARM_NO_TARGET, target_key))
        cell_runs = [*real_runs, *placebo_runs, *(no_target_runs or [])]
        loads = (agent_id, model_id, target_key)
        real_opens = (lambda _run, names=skill_loads.get(loads): names) if loads in skill_loads else None
        warnings = {
            str(warning)
            for run in placebo_runs
            for warning in (run.placebo_snapshot or {}).get("warnings") or ()
        }
        cells.append(
            Cell(
                agent_id=str(agent_id),
                agent_name=names.get(agent_id, str(agent_id)),
                model_id=model_id,
                target=placebo_runs[0].placebo_target,
                target_key=target_key,
                real=_merged(real_runs, real_opens),
                placebo=_merged(placebo_runs, _placebo_names if loads in skill_loads else None),
                no_target=_merged(no_target_runs) if no_target_runs else None,
                complete=all(run.status == "completed" for run in cell_runs),
                warnings=tuple(sorted(warnings)),
            )
        )
    cells.sort(key=lambda cell: (cell.agent_name, cell.model_id, cell.target_key))
    return cells


async def comparison_results(
    db: AsyncSession, comparison: EvaluationPlaceboComparison, evaluation: Evaluation
) -> dict:
    """The comparison's statistics (FR-23 to FR-27), the bootstrap run off the event loop."""
    from app.services.evaluation.metric_package.catalog import DEFAULT_QUALITY_KEYS

    cells = await comparison_cells(db, comparison, evaluation)
    return await asyncio.to_thread(
        comparison_statistics,
        cells,
        seed=comparison.seed,
        quality_selection=evaluation.overall_quality_metrics,
        default_quality_keys=DEFAULT_QUALITY_KEYS,
    )


async def settle_placebo_comparisons(db: AsyncSession, comparison_ids: Iterable[uuid.UUID]) -> None:
    """Settle each comparison whose runs have all finished, with its results. Commits.

    The results are computed with no lock held, then written only if the
    comparison is still unsettled: two settlers racing compute the same
    results, one writes them, and a settled comparison is never changed.
    """
    for comparison_id in sorted(set(comparison_ids)):
        comparison = await db.get(EvaluationPlaceboComparison, comparison_id)
        if comparison is None or comparison.status in SETTLED_STATUSES:
            continue
        statuses = (
            await db.execute(
                select(EvaluationRun.status).where(EvaluationRun.placebo_comparison_id == comparison_id)
            )
        ).scalars()
        settled = settled_status(statuses)
        if settled is None:
            continue
        evaluation = await db.get(Evaluation, comparison.evaluation_id)
        warnings = list(comparison.warnings or [])
        try:
            results = await comparison_results(db, comparison, evaluation)
        except Exception as stats_err:
            logger.warning(
                "Placebo comparison %s: statistics failed: %s",
                comparison_id,
                format_exception_message(stats_err),
            )
            results = None
            warnings.append({"code": "statistics_failed"})
        await db.execute(
            update(EvaluationPlaceboComparison)
            .where(
                EvaluationPlaceboComparison.id == comparison_id,
                EvaluationPlaceboComparison.status.notin_(SETTLED_STATUSES),
            )
            .values(status=settled, results=results, warnings=warnings, completed_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        await db.refresh(comparison)


async def settle_finished_comparisons(db: AsyncSession, evaluation_id: uuid.UUID) -> None:
    """Settle the evaluation's running comparisons whose runs have all finished."""
    running = (
        await db.execute(
            select(EvaluationPlaceboComparison.id).where(
                EvaluationPlaceboComparison.evaluation_id == evaluation_id,
                EvaluationPlaceboComparison.status.notin_(SETTLED_STATUSES),
            )
        )
    ).scalars().all()
    if running:
        await settle_placebo_comparisons(db, running)


async def settle_comparisons_of_runs(db: AsyncSession, run_ids: Iterable[str | uuid.UUID]) -> None:
    """Settle the comparisons the runs belong to (an execution's batch, once it ends)."""
    ids = [run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id)) for run_id in run_ids]
    if not ids:
        return
    comparison_ids = (
        await db.execute(
            select(EvaluationRun.placebo_comparison_id)
            .where(EvaluationRun.id.in_(ids), EvaluationRun.placebo_comparison_id.isnot(None))
            .distinct()
        )
    ).scalars().all()
    if comparison_ids:
        await settle_placebo_comparisons(db, comparison_ids)


def comparison_response(comparison: EvaluationPlaceboComparison, runs: Sequence[EvaluationRun]) -> dict:
    """A comparison as the API and the agent tool serve it: its runs, warnings and results."""
    return {
        "id": str(comparison.id),
        "evaluation_id": str(comparison.evaluation_id),
        "status": comparison.status,
        "seed": comparison.seed,
        "corpus_version": comparison.corpus_version,
        "config": comparison.config,
        "warnings": comparison.warnings or [],
        "results": comparison.results,
        "created_by": str(comparison.created_by) if comparison.created_by else None,
        "created_at": comparison.created_at.isoformat(),
        "completed_at": comparison.completed_at.isoformat() if comparison.completed_at else None,
        "runs": [
            {
                "id": str(run.id),
                "agent_id": str(run.agent_id),
                "model_id": run.model_id,
                "arm": run.arm,
                "trial_index": run.trial_index,
                "placebo_target": run.placebo_target,
                "status": run.status,
            }
            for run in runs
        ],
    }


async def comparison_responses(
    db: AsyncSession, comparisons: Sequence[EvaluationPlaceboComparison], *, visible_agents=None
) -> list[dict]:
    """Each comparison with its runs, in one query for all of them.

    ``visible_agents`` (a subquery of agent ids) keeps the runs and result rows
    to those agents: an agent tool in strict mode shows no other.
    """
    runs: dict[uuid.UUID, list[EvaluationRun]] = {comparison.id: [] for comparison in comparisons}
    if comparisons:
        query = (
            select(EvaluationRun)
            .where(EvaluationRun.placebo_comparison_id.in_(list(runs)))
            .order_by(EvaluationRun.created_at, EvaluationRun.id)
        )
        if visible_agents is not None:
            query = query.where(EvaluationRun.agent_id.in_(visible_agents))
        for run in (await db.execute(query)).scalars():
            runs[run.placebo_comparison_id].append(run)
    responses = [comparison_response(comparison, runs[comparison.id]) for comparison in comparisons]
    if visible_agents is not None:
        for response in responses:
            shown = {run["agent_id"] for run in response["runs"]}
            if response["results"]:
                response["results"] = {
                    **response["results"],
                    "rows": [row for row in response["results"]["rows"] if row["agent_id"] in shown],
                }
            response["warnings"] = [
                warning
                for warning in response["warnings"]
                if "agent_id" not in warning or warning["agent_id"] in shown
            ]
    return responses
