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

"""An evaluation's settings: the one list create, copy, export and import carry.

A setting is a column that decides what an evaluation runs and how it scores.
Each path reads this list, so a new setting cannot be forgotten by one of them:
``tests/test_evaluation_settings.py`` fails when a column of the model is in
neither list here.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from app.models.evaluation import Evaluation, EvaluationAgent

#: The judge's criteria and their default weights. The judge's verdict schema is
#: built from these keys, and ``sub_score_weights`` may weigh only these.
SUB_SCORE_WEIGHTS: dict[str, float] = {
    "tool_selection_quality": 0.20,
    "response_completeness": 0.20,
    "response_accuracy": 0.20,
    "instruction_adherence": 0.20,
    "coherence": 0.20,
}

#: The highest ``penalty_factor``: the scoring card's slider goes from 0 to this.
PENALTY_FACTOR_MAX = 1.5


def sub_score_weights_error(weights: dict[str, float]) -> str | None:
    """Why *weights* cannot weigh the judge's score, or None.

    Only known criteria, each weighted from 0 to 1, summing to 1 (the
    creation page normalises them so): otherwise the judge's score can
    leave 0-100 or drop a criterion the author never meant to drop.
    """
    unknown = sorted(set(weights) - set(SUB_SCORE_WEIGHTS))
    if unknown:
        return f"unknown criteria {', '.join(unknown)}; known: {', '.join(SUB_SCORE_WEIGHTS)}"
    if any(not 0.0 <= value <= 1.0 for value in weights.values()):
        return "each weight must be between 0 and 1"
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6):
        return "the weights must sum to 1"
    return None

EVALUATION_SETTINGS: tuple[str, ...] = (
    "description",
    "family",
    "trigger_config",
    "use_agent_default_model",
    "model_ids",
    "disabled_model_ids",
    "execution_mode",
    "apply_prompt_guards",
    "require_tool_approval",
    "run_mode",
    "evaluator_model",
    "evaluator_prompt",
    "evaluation_instructions",
    "judge_max_tokens",
    "performance_tolerance",
    "sub_score_weights",
    "quality_source",
    "penalty_factor",
    "timeout_seconds",
    "custom_params",
    "tags",
    "field_scoring_overrides",
    "scorer_selection",
    "reuse_default",
    "overall_quality_metrics",
    "overall_performance_metrics",
    # The placebo-controlled method: an import drops its ``skill``
    # targets, whose ids name skills of the exporting instance.
    "placebo_config",
)

# Not settings: identity, ownership and sharing, state each path sets itself
# (``enabled``: a copy has no trigger to pause, an import arrives paused),
# and ``quality_weight``, retired: a column nothing reads, kept as data.
EVALUATION_NOT_SETTINGS: tuple[str, ...] = (
    "id",
    "user_id",
    "is_company_managed",
    "shared_group_ids",
    "shared_group_permissions",
    "shared_user_ids",
    "shared_user_permissions",
    "access_restricted",
    "name",
    "slug",
    "enabled",
    "last_triggered_at",
    "created_at",
    "updated_at",
    "quality_weight",
    # Platform Health's mark (#8257): only its own tools set it.
    "concierge_built",
)

# What an agent's link to an evaluation carries besides the link itself.
EVALUATION_AGENT_SETTINGS: tuple[str, ...] = ("tags", "is_baseline", "output_extractor")

# ``flow_id`` names a flow subject (#8257): the subject itself, like ``agent_id``.
EVALUATION_AGENT_NOT_SETTINGS: tuple[str, ...] = ("id", "evaluation_id", "agent_id", "flow_id", "created_at")

# The settings an update clears with an explicit ``null``: the nullable ones,
# whose null means "none" or "the default" (no description, the platform's judge
# model, inherit every headline metric). Omitted means unchanged for every
# setting, and ``null`` too on a non-nullable one and on the two whose null is
# not a reset anyone would mean: ``family`` (an evaluation with none cannot run)
# and ``scorer_selection`` (null runs every scorer, the judge included).
NULL_KEEPS: frozenset[str] = frozenset({"family", "scorer_selection"})
CLEARED_BY_NULL: frozenset[str] = frozenset(
    field
    for field in EVALUATION_SETTINGS
    if Evaluation.__table__.c[field].nullable and field not in NULL_KEEPS
)


def evaluation_settings(evaluation: Evaluation) -> dict[str, Any]:
    """Every setting of *evaluation*, copied: what a copy or an export starts from."""
    return {field: copy.deepcopy(getattr(evaluation, field)) for field in EVALUATION_SETTINGS}


def evaluation_agent_settings(link: EvaluationAgent) -> dict[str, Any]:
    """Every setting of an agent's link to an evaluation, copied."""
    return {field: copy.deepcopy(getattr(link, field)) for field in EVALUATION_AGENT_SETTINGS}
