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

"""Which model an agent asks for: its own, or the platform routing model.

Every path that runs an agent hands ``get_llm_for_user`` the agent's provider,
model and provider config as overrides. They read them through
:func:`agent_model_selection`, never from the columns directly, so an agent
whose ``llm_model_source`` is ``"routing"`` runs on the platform's lightweight
model everywhere: chat, assignments, consult, background tasks, the
OpenAI-compatible proxy and the public chat.

The routing model is still resolved by ``get_llm_for_user`` for the run's
user, so provider access applies to it like to any other override.
When no routing model is configured the selection is empty and the agent falls
through to the usual tiers (agent default, user preference, platform default);
the agent pages read the routing model from the General Assistant settings and
say so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# The values ``Agent.llm_model_source`` accepts besides NULL (the agent's own
# fields). One list, read by the API schema and the seed.
LLM_MODEL_SOURCE_ROUTING = "routing"
AGENT_LLM_MODEL_SOURCES: tuple[str, ...] = (LLM_MODEL_SOURCE_ROUTING,)


def normalize_llm_model_source(value: Any) -> str | None:
    """A stored ``llm_model_source`` from untrusted input (an imported bundle).

    Anything but a known source reads as NULL: the agent then runs on its own
    model, which is what it did before the field existed.
    """
    return value if value in AGENT_LLM_MODEL_SOURCES else None


@dataclass(frozen=True)
class AgentModelSelection:
    """The overrides one agent passes to ``get_llm_for_user``; all ``None``
    leaves the choice to the router's fallback tiers."""

    provider: str | None
    model: str | None
    provider_id: uuid.UUID | None


async def agent_model_selection(db: AsyncSession, agent: Any) -> AgentModelSelection:
    """The provider / model / provider config *agent* runs on, before fallbacks."""
    if getattr(agent, "llm_model_source", None) == LLM_MODEL_SOURCE_ROUTING:
        from app.services.flow_llm import routing_model

        model, provider_id = await routing_model(db)
        if model is None:
            # None configured: not the agent's own (stale) fields - the tiers.
            return AgentModelSelection(None, None, None)
        return AgentModelSelection(None, model, provider_id)
    return AgentModelSelection(
        getattr(agent, "llm_provider", None) or None,
        getattr(agent, "llm_model", None) or None,
        getattr(agent, "llm_provider_id", None),
    )
