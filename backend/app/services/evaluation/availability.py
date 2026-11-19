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

"""Whether evaluation is on for this deployment (#5503), in two gates (#8257).

The **surface gate**, :func:`evaluation_enabled`: on in a mode where AI runs
(``xtm_one``, ``copilot``, ``dev``; never ``ai_disabled``) **and** with
``EVALUATION`` in ``ENABLED_FEATURES``. The mode rule
is the flag's condition in ``core/feature_flags.py`` (``FLAG_CONDITIONS``), so
``is_feature_enabled("EVALUATION")`` and ``GET /auth/config`` apply it too. It
gates what people see and start: pages, REST, user agent tools, the schedule.

The **engine gate**, :func:`engine_accepts`: the surface gate, or an evaluation
Platform Health built (``Evaluation.concierge_built``, read on the row) while the
Platform Health is switched on (Settings > Agents and automations > Platform Health), whatever the mode (decision B9 of
``dev-docs/platform-concierge.md``). Platform Health has no user row: its
evaluations have no owner and carry that marker, which only server code sets
(never REST, copy, export or import). It gates the work the engine does: the
queued evaluation and rescore executions. See ``dev-docs/evaluation-feature-flag.md``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.feature_flags import feature_disabled_detail, is_feature_enabled, require_feature
from app.models.evaluation import Evaluation, EvaluationRun
from app.services.health.availability import health_active

DISABLED_DETAIL = feature_disabled_detail("EVALUATION")

#: FastAPI dependency: 403 while the evaluation surface is off.
require_evaluation = require_feature("EVALUATION")


def evaluation_enabled() -> bool:
    """The surface gate. Read on each call; ``main.py`` reads it once at import for the published schema."""
    return is_feature_enabled("EVALUATION")


async def engine_accepts(db: AsyncSession, evaluation_id: uuid.UUID | str) -> bool:
    """The engine gate for one evaluation: the surface gate, or a Platform Health launch."""
    if evaluation_enabled():
        return True
    if not await health_active(db):
        return False
    try:
        evaluation_uuid = uuid.UUID(str(evaluation_id))
    except ValueError:
        return False
    built = await db.scalar(select(Evaluation.concierge_built).where(Evaluation.id == evaluation_uuid))
    return built is True


async def engine_accepts_runs(db: AsyncSession, run_ids: Iterable[uuid.UUID | str]) -> bool:
    """The engine gate for a rescore batch: every run's evaluation must pass it."""
    if evaluation_enabled():
        return True
    if not await health_active(db):
        return False
    try:
        ids = {uuid.UUID(str(run_id)) for run_id in run_ids}
    except ValueError:
        return False
    marks = set(
        (
            await db.execute(
                select(Evaluation.concierge_built)
                .join(EvaluationRun, EvaluationRun.evaluation_id == Evaluation.id)
                .where(EvaluationRun.id.in_(ids))
                .distinct()
            )
        ).scalars()
    )
    return marks == {True}
