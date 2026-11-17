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

"""Cascading evaluation/agent/run tags and the effective-tag merge.

Tags live at three levels: ``Evaluation.tags``, ``EvaluationAgent.tags`` (scoped to the
evaluation) and ``EvaluationRun.tags``. A run's **effective tags** are merged at read
time with precedence ``run > agent > evaluation``, as in ``params.py``. They are never
persisted, so editing an evaluation or agent tag updates existing runs.

The ``baseline`` key is reserved: only ``EvaluationAgent.is_baseline`` sets baseline
status, so every tag write path strips it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvaluationAgent

# Keys never accepted as free-form tags. Baseline is set only through the agent.
RESERVED_TAG_KEYS: frozenset[str] = frozenset({"baseline"})


def _coerce_value(value: object) -> str:
    """Coerce a tag value to its comparable/displayable string form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


def strip_reserved_keys(tags: dict | None) -> dict[str, str] | None:
    """Return a copy of ``tags`` with reserved keys removed and values stringified.

    ``None``/empty in → ``None`` out, so callers can store ``NULL`` for "no tags".
    Reserved keys (``baseline``) are silently dropped.
    """
    if not tags:
        return None
    cleaned = {
        str(k): _coerce_value(v)
        for k, v in tags.items()
        if str(k) not in RESERVED_TAG_KEYS
    }
    return cleaned or None


def merge_effective_tags(
    evaluation_tags: dict | None,
    agent_tags: dict | None,
    run_tags: dict | None,
) -> dict[str, str]:
    """Merge the three tag levels into a run's effective tags.

    Precedence is ``run > agent > evaluation``: later levels overwrite earlier
    ones on a key conflict. A level contributing ``None``/``{}`` adds nothing and
    never blanks a lower-priority level. Reserved keys are dropped so a stray
    legacy ``baseline`` never resurfaces.
    """
    merged: dict[str, str] = {}
    for level in (evaluation_tags, agent_tags, run_tags):
        if not level:
            continue
        for k, v in level.items():
            key = str(k)
            if key in RESERVED_TAG_KEYS:
                continue
            merged[key] = _coerce_value(v)
    return merged


async def load_evaluation_tags(
    db: AsyncSession,
    evaluation_ids: set[uuid.UUID] | list[uuid.UUID],
) -> dict[uuid.UUID, dict]:
    """Batch-load ``Evaluation.tags`` keyed by evaluation id (for search/serialisation)."""
    from app.models.evaluation import Evaluation

    ids = list(evaluation_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(Evaluation.id, Evaluation.tags).where(Evaluation.id.in_(ids))
    )
    return {rid: (tags or {}) for rid, tags in rows.all()}


async def load_agent_meta(
    db: AsyncSession,
    evaluation_ids: set[uuid.UUID] | list[uuid.UUID],
) -> dict[tuple[uuid.UUID, uuid.UUID], dict]:
    """Batch-load per-(evaluation, subject) tags + baseline flag.

    Returns a map ``{(evaluation_id, subject_id): {"tags": dict, "is_baseline": bool}}``
    for every subject linked to any of ``evaluation_ids``: the agent's id, or a
    flow subject's (#8257), as ``run.flow_id or run.agent_id`` reads it.
    """
    ids = list(evaluation_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(
            EvaluationAgent.evaluation_id,
            EvaluationAgent.agent_id,
            EvaluationAgent.flow_id,
            EvaluationAgent.tags,
            EvaluationAgent.is_baseline,
        ).where(EvaluationAgent.evaluation_id.in_(ids))
    )
    return {
        (eval_id, flow_id or agent_id): {"tags": tags or {}, "is_baseline": bool(is_baseline)}
        for eval_id, agent_id, flow_id, tags, is_baseline in rows.all()
    }
