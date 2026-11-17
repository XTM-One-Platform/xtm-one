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

"""Server-side combined run search.

Filters evaluation runs by params (exact key=value), metric aggregates
(mean/percentile/min/max with comparison ops), score ranges, dataset version,
tags (key-exists or key=value), and creation-date bounds. Filters referencing a
nonexistent param/metric/tag simply exclude every run (empty set), never error.

Tag filtering matches a run's **effective tags** — the read-time merge of
evaluation ⊕ agent ⊕ run tags — so a single evaluation- or
agent-level tag selects runs across agents and evaluations. ``baseline_only``
restricts to runs whose agent is the evaluation baseline.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.models.evaluation_param import EvaluationRunParam
from app.services.evaluation.metrics import aggregate_metrics
from app.services.evaluation.tags import (
    RESERVED_TAG_KEYS,
    _coerce_value,
    load_agent_meta,
    load_evaluation_tags,
    merge_effective_tags,
)

_OPS = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}

# The headline columns plus the retired `overall_score` and `speed_score`,
# still stored, so a saved filter on them keeps matching the runs it was written for.
_SCORE_FIELDS = {
    "score",
    "speed_score",
    "overall_score",
    "f1_score",
    "quality_score",
    "performance_score",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def search_evaluation_runs(
    db: AsyncSession,
    body,
    visible_eval_ids: set[uuid.UUID],
) -> list[tuple[EvaluationRun, dict[str, str], bool]]:
    """Return ``(run, effective_tags, is_baseline)`` triples matching every filter.

    Runs are scoped to ``visible_eval_ids``; ``effective_tags`` and ``is_baseline``
    are computed once per run and reused for filtering + serialisation.
    """
    query = select(EvaluationRun).where(
        EvaluationRun.evaluation_id.in_(visible_eval_ids or [])
    )
    if not visible_eval_ids:
        return []

    if body.evaluation_id:
        try:
            query = query.where(
                EvaluationRun.evaluation_id == uuid.UUID(body.evaluation_id)
            )
        except ValueError:
            return []

    # Score-range filters map directly onto run columns.
    for sf in body.scores or []:
        if sf.field not in _SCORE_FIELDS or sf.op not in _OPS:
            return []
        col = getattr(EvaluationRun, sf.field)
        op = sf.op
        if op == "lt":
            query = query.where(col < sf.value)
        elif op == "lte":
            query = query.where(col <= sf.value)
        elif op == "gt":
            query = query.where(col > sf.value)
        elif op == "gte":
            query = query.where(col >= sf.value)
        else:
            query = query.where(col == sf.value)

    after = _parse_dt(body.created_after)
    before = _parse_dt(body.created_before)
    if after:
        query = query.where(EvaluationRun.created_at >= after)
    if before:
        query = query.where(EvaluationRun.created_at <= before)

    query = query.order_by(EvaluationRun.created_at.desc()).limit(
        max(int(body.limit or 100) * 5, 100)
    )
    runs = list((await db.execute(query)).scalars().all())

    # Effective tags + baseline flag: batch-load evaluation +
    # agent metadata for the candidate set, then merge per run.
    eval_ids = {r.evaluation_id for r in runs}
    eval_tags = await load_evaluation_tags(db, eval_ids)
    agent_meta = await load_agent_meta(db, eval_ids)

    def _effective(run: EvaluationRun) -> dict[str, str]:
        meta = agent_meta.get((run.evaluation_id, run.flow_id or run.agent_id), {})
        return merge_effective_tags(
            eval_tags.get(run.evaluation_id), meta.get("tags"), run.tags
        )

    def _is_baseline(run: EvaluationRun) -> bool:
        meta = agent_meta.get((run.evaluation_id, run.flow_id or run.agent_id), {})
        return bool(meta.get("is_baseline"))

    eff_by_run: dict[uuid.UUID, dict[str, str]] = {
        r.id: _effective(r) for r in runs
    }
    baseline_by_run: dict[uuid.UUID, bool] = {r.id: _is_baseline(r) for r in runs}

    # Param equality (exact key=value). A referenced key that no run carries
    # yields an empty result — never an error.
    if body.params:
        param_rows = await db.execute(
            select(
                EvaluationRunParam.run_id,
                EvaluationRunParam.key,
                EvaluationRunParam.value,
            ).where(EvaluationRunParam.run_id.in_([r.id for r in runs]))
        )
        by_run: dict[uuid.UUID, dict[str, str | None]] = {}
        for rid, key, value in param_rows.all():
            by_run.setdefault(rid, {})[key] = value
        runs = [
            r
            for r in runs
            if all(
                by_run.get(r.id, {}).get(k) == v for k, v in body.params.items()
            )
        ]

    # Baseline filter: explicit request flag OR the legacy convention of a
    # ``tags={"baseline": "true"}`` request (mapped to baseline_only here so the
    # reserved key never reaches effective-tag filtering).
    baseline_only = bool(getattr(body, "baseline_only", False))
    tag_filters = dict(body.tags) if body.tags else {}
    for reserved in RESERVED_TAG_KEYS:
        if reserved in tag_filters:
            baseline_only = True
            tag_filters.pop(reserved, None)

    if baseline_only:
        runs = [r for r in runs if baseline_by_run.get(r.id, False)]

    # Tag filters over EFFECTIVE tags: "key" (exists) or "key=value".
    if tag_filters:
        def _tag_ok(run: EvaluationRun) -> bool:
            tags = eff_by_run.get(run.id, {})
            for k, v in tag_filters.items():
                # A None/""/boolean-True filter means "key exists"; use identity
                # for True so a numeric 1 (== True) is not misread as exists.
                if v is None or v == "" or v is True:
                    if k not in tags:
                        return False
                # Exact match: coerce the filter value the same way effective
                # tags are stored (bool/number → their canonical string form).
                elif tags.get(k) != _coerce_value(v):
                    return False
            return True

        runs = [r for r in runs if _tag_ok(r)]

    # Dataset-version filter: any pinned dataset matches the requested version.
    if body.dataset_version is not None:
        runs = [
            r
            for r in runs
            if r.pinned_dataset_versions
            and body.dataset_version in set(r.pinned_dataset_versions.values())
        ]

    # Metric-aggregate filters: compute per-run aggregates and compare.
    if body.metrics:
        run_ids = [r.id for r in runs]
        metric_rows = await db.execute(
            select(
                EvaluationMetric.run_id,
                EvaluationMetric.key,
                EvaluationMetric.value,
            ).where(EvaluationMetric.run_id.in_(run_ids))
        )
        rows_by_run: dict[uuid.UUID, list[tuple[str, float | None]]] = {}
        for rid, key, value in metric_rows.all():
            rows_by_run.setdefault(rid, []).append((key, value))

        def _metric_ok(run: EvaluationRun) -> bool:
            aggs = aggregate_metrics(rows_by_run.get(run.id, []))
            for mf in body.metrics:
                stats = aggs.get(mf.key)
                if stats is None or mf.op not in _OPS:
                    return False
                actual = stats.get(mf.agg)
                if actual is None or not _OPS[mf.op](actual, mf.value):
                    return False
            return True

        runs = [r for r in runs if _metric_ok(r)]

    return [
        (r, eff_by_run.get(r.id, {}), baseline_by_run.get(r.id, False))
        for r in runs[: int(body.limit or 100)]
    ]

