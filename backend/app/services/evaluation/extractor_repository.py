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

"""Database accessors for per-agent extractors.

Service-side on purpose. ``evaluation_engine`` needs ``load_extractor_for_agent``
on every run, and importing it from ``api/v1`` would drag the FastAPI router
chain into every worker and make the service -> API dependency arrow point both
ways across the same seam.

Beside the extraction package rather than inside it: that package is a pure fold
with no I/O of any kind, and its tests pin that. These functions talk to the
database, so they live next to it, not in it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvaluationAgent


def as_extractor_dict(value: Any) -> dict[str, Any] | None:
    """Normalise a submitted extractor (model or mapping) to a plain dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    dumped = value.model_dump(exclude_none=True)
    return dict(dumped) if isinstance(dumped, dict) else None


async def load_evaluation_agents(
    db: AsyncSession, evaluation_id: uuid.UUID
) -> list[EvaluationAgent]:
    """The evaluation's agent links; a flow subject's (#8257) takes no extractor."""
    result = await db.execute(
        select(EvaluationAgent).where(
            EvaluationAgent.evaluation_id == evaluation_id, EvaluationAgent.agent_id.is_not(None)
        )
    )
    return list(result.scalars().all())


async def load_agent_extractors(
    db: AsyncSession, evaluation_id: uuid.UUID
) -> dict[str, Any]:
    """The stored map, for the response body. Absent entries mean raw output."""
    rows = await load_evaluation_agents(db, evaluation_id)
    return {
        str(row.agent_id): row.output_extractor
        for row in rows
        if isinstance(row.output_extractor, dict)
    }


async def load_extractor_for_agent(
    db: AsyncSession, evaluation_id: uuid.UUID, agent_id: uuid.UUID
) -> dict[str, Any] | None:
    """The extractor for one agent, resolved once per run by the engine."""
    result = await db.execute(
        select(EvaluationAgent.output_extractor).where(
            EvaluationAgent.evaluation_id == evaluation_id,
            EvaluationAgent.agent_id == agent_id,
        )
    )
    stored = result.scalar_one_or_none()
    return stored if isinstance(stored, dict) else None


async def apply_agent_extractors(
    db: AsyncSession,
    evaluation_id: uuid.UUID,
    agent_extractors: dict[str, Any] | None,
) -> None:
    """Persist a validated map onto the join rows. ``None`` clears an extractor."""
    if not agent_extractors:
        return
    rows = await load_evaluation_agents(db, evaluation_id)
    by_agent = {str(row.agent_id): row for row in rows}
    for agent_id, extractor in agent_extractors.items():
        row = by_agent.get(agent_id)
        if row is None:
            # validate_agent_extractors refuses unknown ids, so this means the two
            # drifted or the link changed meanwhile. Failing beats a 200 that
            # persisted nothing.
            raise ValueError(
                f"agent {agent_id!r} is not linked to evaluation {evaluation_id}"
            )
        row.output_extractor = as_extractor_dict(extractor)


__all__ = [
    "apply_agent_extractors",
    "as_extractor_dict",
    "load_agent_extractors",
    "load_evaluation_agents",
    "load_extractor_for_agent",
]
