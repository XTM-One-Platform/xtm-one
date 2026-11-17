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

"""Compare evaluations of one family on the entries their datasets share (#12282).

Two evaluations measure different things on overlapping data: another agent, another
dataset, another scorer version. This module gathers what a page needs to say what can be
compared and to compare it:

* what each evaluation is (datasets, subjects, judge, scorer versions, limits), so the page
  states which dimension changed;
* one run per subject and model per evaluation (the newest completed one, or the runs the
  caller picks) and the completed runs it can pick from;
* the entries, paired across evaluations when the family's scorer reads their expected
  answers as the same ground truth (:func:`reference_key`);
* per run and entry, the stored counts of the family's pooled quality metrics (micro
  figures add them up), the LLM cost and the duration.

Statistics (pooled values, intervals, ranks) are the page's: it re-pools them for whatever
subset of entries it shows. Results are counted as a run's headlines count them
(:func:`counts_in_headlines`), so a pooled figure here is the run's own on the same entries.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersionEntry
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationResult, EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.models.flow import Flow
from app.services.evaluation.aggregation import counts_in_headlines
from app.services.evaluation.metric_package import (
    is_pooled_key,
    metric_direction,
    quality_catalog,
    reference_key,
)

#: The most evaluations one comparison takes: the page lays them out side by side.
MAX_COMPARED_EVALUATIONS = 4
#: The completed runs offered per evaluation for the run picker, newest first.
RUN_CHOICES_PER_EVALUATION = 50
COST_KEY = "performance.cost.llm"
_ENTRY_LABEL_CHARS = 120


class ComparisonRefusedError(ValueError):
    """The evaluations cannot be compared (another family, one never run, too many)."""


def _label(description: str | None, prompt: str | None, position: int) -> str:
    for text in (description, prompt):
        stripped = (text or "").strip()
        if stripped:
            return stripped.splitlines()[0][:_ENTRY_LABEL_CHARS]
    return f"Entry {position + 1}"


def _digest(text: str | None) -> str | None:
    """A short fingerprint of a free text, so two judges' instructions compare equal or not
    without sending the texts."""
    if not (text or "").strip():
        return None
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def pooled_quality_keys(family: str | None) -> list[dict[str, Any]]:
    """The family's pooled quality metrics: the ones whose run figure adds up entry counts."""
    return [
        {"key": item["key"], "label": item["label"], "direction": metric_direction(item["key"])}
        for item in quality_catalog(family)
        if is_pooled_key(item["key"])
    ]


async def _subjects(db: AsyncSession, evaluations: Sequence[Evaluation]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Each evaluation's subjects as the comparability table shows them."""
    ids = [e.id for e in evaluations]
    links = (await db.execute(select(EvaluationAgent).where(EvaluationAgent.evaluation_id.in_(ids)))).scalars().all()
    agents = {
        a.id: a
        for a in (
            await db.execute(select(Agent).where(Agent.id.in_([lk.agent_id for lk in links if lk.agent_id])))
        ).scalars()
    }
    flows = {
        f.id: f
        for f in (await db.execute(select(Flow).where(Flow.id.in_([lk.flow_id for lk in links if lk.flow_id])))).scalars()
    }
    skill_ids = {str(s) for a in agents.values() for s in (a.skill_ids or [])}
    skill_names: dict[str, str] = {}
    if skill_ids:
        parsed = [uuid.UUID(s) for s in skill_ids if _is_uuid(s)]
        rows = await db.execute(select(AgentSkill.id, AgentSkill.name).where(AgentSkill.id.in_(parsed)))
        skill_names = {str(i): n for i, n in rows.all()}
    out: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        if link.agent_id and link.agent_id in agents:
            a = agents[link.agent_id]
            out[link.evaluation_id].append({
                "id": str(a.id), "kind": "agent", "name": a.name, "is_baseline": bool(link.is_baseline),
                "builtin_tools": list(a.builtin_tool_ids or []),
                "custom_tool_count": len(a.custom_tool_ids or []),
                "mcp_server_count": len(a.mcp_server_ids or []),
                "skills": [skill_names.get(str(s), str(s)) for s in (a.skill_ids or [])],
                "max_iterations": a.max_iterations, "temperature": a.temperature, "model": a.llm_model,
            })
        elif link.flow_id and link.flow_id in flows:
            f = flows[link.flow_id]
            out[link.evaluation_id].append({
                "id": str(f.id), "kind": "flow", "name": f.name, "is_baseline": bool(link.is_baseline),
                "builtin_tools": [], "custom_tool_count": 0, "mcp_server_count": 0, "skills": [],
                "max_iterations": None, "temperature": None, "model": None,
            })
    return out


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _datasets(db: AsyncSession, evaluations: Sequence[Evaluation]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    ids = [e.id for e in evaluations]
    rows = await db.execute(
        select(EvaluationDataset.evaluation_id, Dataset.id, Dataset.name, Dataset.evaluation_instructions)
        .join(Dataset, Dataset.id == EvaluationDataset.dataset_id)
        .where(EvaluationDataset.evaluation_id.in_(ids))
        .order_by(EvaluationDataset.position)
    )
    linked = rows.all()
    count_rows = await db.execute(
        select(DatasetEntry.dataset_id, func.count())
        .where(DatasetEntry.dataset_id.in_([r[1] for r in linked]))
        .group_by(DatasetEntry.dataset_id)
    )
    counts: dict[uuid.UUID, int] = {dataset_id: int(n) for dataset_id, n in count_rows.all()}
    out: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for evaluation_id, dataset_id, name, instructions in linked:
        out[evaluation_id].append({
            "id": str(dataset_id), "name": name, "entry_count": int(counts.get(dataset_id, 0)),
            "instructions_digest": _digest(instructions),
        })
    return out


def _subject_id(run: EvaluationRun) -> uuid.UUID | None:
    return run.agent_id or run.flow_id


def _pick_runs(
    runs: Sequence[EvaluationRun], wanted: set[uuid.UUID]
) -> list[EvaluationRun]:
    """The runs an evaluation is compared with: the caller's when it named some of this
    evaluation's runs, else the newest completed run per subject and model (*runs* is
    newest first)."""
    named = [r for r in runs if r.id in wanted]
    if named:
        return named
    seen: set[tuple[uuid.UUID | None, str]] = set()
    picked = []
    for run in runs:
        key = (_subject_id(run), run.model_id)
        if key not in seen:
            seen.add(key)
            picked.append(run)
    return picked


async def compare_evaluations(
    db: AsyncSession,
    evaluations: Sequence[Evaluation],
    run_ids: Sequence[uuid.UUID] = (),
) -> dict[str, Any]:
    """The comparison of *evaluations*, visible to the caller already (see the module doc).

    Raises:
        ComparisonRefusedError: fewer than two or more than :data:`MAX_COMPARED_EVALUATIONS`
            evaluations, another family, or one never given a family.
    """
    if not 2 <= len(evaluations) <= MAX_COMPARED_EVALUATIONS:
        raise ComparisonRefusedError(f"Compare 2 to {MAX_COMPARED_EVALUATIONS} evaluations")
    families = {e.family for e in evaluations}
    if None in families:
        raise ComparisonRefusedError("An evaluation without a family has never been run")
    if len(families) > 1:
        raise ComparisonRefusedError("Only evaluations of one family can be compared")
    family = evaluations[0].family
    ids = [e.id for e in evaluations]

    completed = (
        await db.execute(
            select(EvaluationRun)
            .where(
                EvaluationRun.evaluation_id.in_(ids),
                EvaluationRun.status == "completed",
                EvaluationRun.arm == "real",
            )
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id)
        )
    ).scalars().all()
    by_eval: dict[uuid.UUID, list[EvaluationRun]] = defaultdict(list)
    for run in completed:
        by_eval[run.evaluation_id].append(run)
    wanted = set(run_ids)
    selected = {eid: _pick_runs(by_eval[eid], wanted) for eid in ids}
    selected_runs = [r for eid in ids for r in selected[eid]]
    run_by_id = {r.id: r for r in selected_runs}

    # Results and the entry each was scored against: the snapshot first, then the live entry.
    results = (
        await db.execute(
            select(
                EvaluationResult.id, EvaluationResult.run_id, EvaluationResult.entry_id,
                EvaluationResult.execution_anomaly_kind, EvaluationResult.is_judge_failure,
                EvaluationResult.duration_seconds,
                DatasetVersionEntry.entry_id, DatasetVersionEntry.position, DatasetVersionEntry.description,
                DatasetVersionEntry.input_prompt, DatasetVersionEntry.expected_answer, DatasetVersionEntry.file_ids,
                DatasetEntry.position, DatasetEntry.description, DatasetEntry.input_prompt,
                DatasetEntry.expected_answer, DatasetEntry.file_ids,
            )
            .outerjoin(DatasetVersionEntry, DatasetVersionEntry.id == EvaluationResult.version_entry_id)
            .outerjoin(DatasetEntry, DatasetEntry.id == EvaluationResult.entry_id)
            .where(EvaluationResult.run_id.in_(list(run_by_id)))
        )
    ).all()

    keys_cache: dict[str, str | None] = {}
    entries: dict[str, dict[str, Any]] = {}
    occupied: dict[tuple[uuid.UUID, str], uuid.UUID | None] = {}
    cells: list[dict[str, Any]] = []
    result_ids: list[uuid.UUID] = []
    for row in results:
        (rid, run_id, live_id, anomaly, judge_failed, seconds,
         v_entry, v_pos, v_desc, v_prompt, v_expected, v_files,
         l_pos, l_desc, l_prompt, l_expected, l_files) = row
        snapshot = v_expected is not None
        expected = v_expected if snapshot else l_expected
        if expected is None:
            continue  # the entry and its snapshot are gone: nothing says what it was scored against
        evaluation_id = run_by_id[run_id].evaluation_id
        entry_id = (v_entry if snapshot else live_id) or live_id
        position = (v_pos if snapshot else l_pos) or 0
        digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        if digest not in keys_cache:
            keys_cache[digest] = reference_key(family, expected)
        ref = keys_cache[digest] or f"unreadable:{digest}"
        # Two entries of one evaluation with the same expected answer stay two entries.
        slot = (evaluation_id, ref)
        n = 0
        while slot in occupied and occupied[slot] != entry_id:
            n += 1
            slot = (evaluation_id, f"{ref}#{n}")
        occupied[slot] = entry_id
        key = slot[1]
        entry = entries.setdefault(key, {"key": key, "label": None, "members": {}})
        member = entry["members"].setdefault(str(evaluation_id), {
            "evaluation_id": str(evaluation_id), "entry_id": str(entry_id) if entry_id else None,
            "position": position, "has_file": bool(v_files if snapshot else l_files),
        })
        if entry["label"] is None:
            entry["label"] = _label(v_desc if snapshot else l_desc, v_prompt if snapshot else l_prompt, member["position"])
        result_ids.append(rid)
        cells.append({
            "run_id": str(run_id), "entry_key": key, "result_id": str(rid),
            "counted": counts_in_headlines(_Counted(anomaly, bool(judge_failed))),
            "anomaly": anomaly, "metrics": {}, "cost": None, "seconds": seconds,  # agent time
        })

    metrics = pooled_quality_keys(family)
    wanted_keys = [m["key"] for m in metrics] + [COST_KEY]
    by_result: dict[str, dict[str, Any]] = {c["result_id"]: c for c in cells}
    if result_ids:
        rows = await db.execute(
            select(EvaluationMetric.result_id, EvaluationMetric.key, EvaluationMetric.value,
                   EvaluationMetric.numerator, EvaluationMetric.denominator)
            .where(EvaluationMetric.result_id.in_(result_ids), EvaluationMetric.key.in_(wanted_keys))
        )
        for rid, key, value, numerator, denominator in rows.all():
            cell = by_result[str(rid)]
            if key == COST_KEY:
                cell["cost"] = value
            elif cell["counted"] and numerator is not None and denominator is not None:
                cell["metrics"][key] = [numerator, denominator]

    subjects = await _subjects(db, evaluations)
    datasets = await _datasets(db, evaluations)
    subject_names = {s["id"]: s["name"] for group in subjects.values() for s in group}

    def run_view(run: EvaluationRun) -> dict[str, Any]:
        sid = _subject_id(run)
        return {
            "id": str(run.id), "evaluation_id": str(run.evaluation_id),
            "subject_id": str(sid) if sid else None, "subject_name": subject_names.get(str(sid)) if sid else None,
            "model_id": run.model_id, "is_default_model": bool(run.is_default_model),
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "metric_suite_version": run.metric_suite_version, "execution_mode": run.execution_mode,
            "quality_score": run.quality_score, "completed_entries": run.completed_entries,
            "total_entries": run.total_entries,
        }

    return {
        "family": family,
        "metrics": metrics,
        "evaluations": [
            {
                "id": str(e.id), "name": e.name, "family": e.family,
                "execution_mode": e.execution_mode, "timeout_seconds": e.timeout_seconds,
                "judge_on": e.scorer_selection is None or "judge" in (e.scorer_selection or []),
                "evaluator_model": e.evaluator_model, "judge_weights": e.sub_score_weights,
                "judge_instructions_digest": _digest(
                    "\n".join(filter(None, [e.evaluation_instructions, e.evaluator_prompt]))
                ),
                "subjects": subjects.get(e.id, []),
                "datasets": datasets.get(e.id, []),
                "runs": [run_view(r) for r in selected[e.id]],
                "run_choices": [run_view(r) for r in by_eval[e.id][:RUN_CHOICES_PER_EVALUATION]],
            }
            for e in evaluations
        ],
        "entries": [
            {"key": v["key"], "label": v["label"], "members": list(v["members"].values())}
            for v in entries.values()
        ],
        "cells": cells,
    }


@dataclass(frozen=True)
class _Counted:
    """The two fields :func:`counts_in_headlines` reads, from a selected row."""

    execution_anomaly_kind: str | None
    is_judge_failure: bool


__all__ = [
    "MAX_COMPARED_EVALUATIONS",
    "ComparisonRefusedError",
    "compare_evaluations",
    "pooled_quality_keys",
]
