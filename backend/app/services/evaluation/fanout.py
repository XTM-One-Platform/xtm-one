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

"""Fan-out helpers for evaluation run creation.

A model-independent (``skip_llm``) agent produces deterministic output that does
not depend on the selected chat model, so it collapses to a single run per
evaluation instead of fanning out one run per selected model.

These helpers are the single source of truth for that collapse, shared by the
manual trigger (``api/v1/evaluations.py``) and the autonomous/scheduled trigger
(``services/autonomous.py``) so the two paths can never drift.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from app.services.evaluation.constants import MODEL_INDEPENDENT


def resolve_effective_models[AgentId](
    agent_id: AgentId,
    skip_llm_agent_ids: set[AgentId],
    model_list: Sequence[str],
    default_model_id: str | None,
) -> tuple[list[str], str | None]:
    """Return ``(effective_models, effective_default)`` for one agent's fan-out.

    A model-independent (``skip_llm``) agent collapses to a single sentinel run
    with no default-model attribution (``is_default_model`` is always ``False``).
    Every other agent keeps its resolved per-model fan-out unchanged.
    """
    if agent_id in skip_llm_agent_ids:
        return [MODEL_INDEPENDENT], None
    return list(model_list), default_model_id


def evaluation_has_runs[AgentId](
    agent_ids: Iterable[AgentId],
    skip_llm_agent_ids: set[AgentId],
    agent_model_map: Mapping[AgentId, tuple[str | None, list[str]]],
) -> bool:
    """True if at least one run would be created across the agent set.

    A ``skip_llm`` agent always yields exactly one run even when no chat model
    could be resolved, so an evaluation containing only ``skip_llm`` agents must
    not be rejected with "No models could be resolved".
    """
    return any(
        (aid in skip_llm_agent_ids) or bool(agent_model_map.get(aid, (None, []))[1])
        for aid in agent_ids
    )
