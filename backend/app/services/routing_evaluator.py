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

"""Routing evaluator — LLM-based condition matching for mandatory transfers.

After an agent completes its agentic loop, if it has multiple
``mandatory_transfer`` targets with conditions, this service runs a
single lightweight LLM classification call to determine which target
should receive the conversation.

The evaluator:
  1. Collects all mandatory_transfer targets + their conditions.
  2. Sends a single prompt to the routing model (platform setting).
  3. Returns the matched target agent, or the fallback (no condition).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message
from app.llm.cut_answers import chat_retrying_a_cut
from app.services.langfuse_tracing import traced_generation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import ChatResponse
    from app.models.agent import Agent
    from app.tools.agent_loop import UsageHook

logger = logging.getLogger(__name__)

# The answer is one number. An answer cut at this budget before it names one is
# retried once with twice the room, then read as an answer naming none.
_ROUTING_MAX_TOKENS = 10
_CONDITION_NUMBER = re.compile(r"\d+")


def _names_a_condition(response: ChatResponse) -> bool:
    """A cut after the number leaves the routing decision whole."""
    return _CONDITION_NUMBER.search(response.content or "") is not None


async def evaluate_routing_conditions(
    db: AsyncSession,
    *,
    agent_output: str,
    mandatory_targets: list[Agent],
    conditions: dict[str, str],
    user_id: str | None = None,
    merge_usage: UsageHook | None = None,
) -> Agent | None:
    """Evaluate routing conditions against the agent's output.

    Args:
        db: Database session.
        agent_output: The source agent's full response text.
        mandatory_targets: List of agents typed as ``mandatory_transfer``,
            ordered by priority (first match wins).
        conditions: Maps agent UUID → condition string.  Empty/missing = fallback.
        user_id: For LLM provider resolution.
        merge_usage: Handed the usage of each classification call that
            returns, with the model its response names, so the caller's run
            counts it.

    Returns:
        The matched target Agent, or None if no targets qualify.
    """
    if not mandatory_targets:
        return None

    # Single target — skip LLM, return directly (backward compat).
    if len(mandatory_targets) == 1:
        return mandatory_targets[0]

    # Separate targets with conditions vs. fallback (no condition).
    conditioned: list[tuple[Agent, str]] = []
    fallback: Agent | None = None
    for t in mandatory_targets:
        cond = (conditions.get(str(t.id)) or "").strip()
        if cond:
            conditioned.append((t, cond))
        elif fallback is None:
            fallback = t

    # All targets lack conditions → return the first one.
    if not conditioned:
        return mandatory_targets[0]

    # ── Build classification prompt ───────────────────────────────────
    options_text = "\n".join(
        f"  {i + 1}. {cond}" for i, (_, cond) in enumerate(conditioned)
    )
    none_idx = len(conditioned) + 1

    system_prompt = (
        "You are a routing classifier. Given an agent's output, determine "
        "which routing condition best matches. Respond with ONLY the number "
        "of the matching condition (e.g. '1'), or '0' if none match."
    )
    user_prompt = (
        f"## Agent Output\n{agent_output[:4000]}\n\n"
        f"## Routing Conditions\n{options_text}\n"
        f"  {none_idx}. None of the above\n\n"
        f"Which condition (1-{none_idx}) best matches the agent output? "
        f"Reply with the number only."
    )

    # ── Resolve routing model ─────────────────────────────────────────
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.llm.base import ChatMessage, ContextOverflowError
    from app.llm.router import get_llm_for_user

    ps = await get_platform_settings(db)
    routing_model_override = getattr(ps, "routing_model", None) or None
    # The routing model is a bare id, so pass the config the admin picked with
    # it — otherwise the router resolves the owner newest-config-first and the
    # routing decision can run on a different key than was configured.
    routing_provider_id = (
        getattr(ps, "routing_provider_id", None) if routing_model_override else None
    )

    try:
        llm, resolved_model = await get_llm_for_user(
            db,
            user_id,
            model_override=routing_model_override,
            provider_id_override=routing_provider_id,
        )

        routing_messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]

        async def _classify(max_tokens: int):
            async with traced_generation(
                name="routing_classifier",
                model=resolved_model,
                messages=routing_messages,
            ) as _gen:
                _gen.response = await llm.chat(
                    messages=routing_messages,
                    max_tokens=max_tokens,
                    model=resolved_model,
                    # A mechanical classification: on the models that think
                    # by default, thinking would share the few tokens with it.
                    disable_thinking=True,
                )
            _usage = getattr(_gen.response, "usage", None)
            if merge_usage is not None and isinstance(_usage, dict) and _usage:
                merge_usage(_usage, getattr(_gen.response, "model", None) or resolved_model)
            return _gen.response

        response, cut = await chat_retrying_a_cut(
            llm,
            resolved_model,
            _ROUTING_MAX_TOKENS,
            _classify,
            label="Routing classifier",
            decided=_names_a_condition,
        )
        if cut:
            # Cut before it named a condition, twice: *decided* found no
            # number, so it takes the no-match path below, the fallback target.
            logger.warning("Routing classifier answer still cut at max_tokens")

        answer = (response.content or "").strip()
        # Extract first digit(s) from the response.
        match = _CONDITION_NUMBER.search(answer)
        if match:
            idx = int(match.group()) - 1  # 0-based
            if 0 <= idx < len(conditioned):
                chosen_agent, chosen_cond = conditioned[idx]
                logger.info(
                    "Routing evaluator matched condition %d (%s) → agent %s (%s)",
                    idx + 1,
                    chosen_cond[:80],
                    chosen_agent.name,
                    chosen_agent.id,
                )
                return chosen_agent

        # No match or "none of the above" → use fallback.
        if fallback:
            logger.info(
                "Routing evaluator: no condition matched → fallback agent %s (%s)",
                fallback.name,
                fallback.id,
            )
            return fallback

        # No fallback configured → return first target as safety net.
        logger.warning(
            "Routing evaluator: no condition matched and no fallback — "
            "using first mandatory target %s",
            mandatory_targets[0].name,
        )
        return mandatory_targets[0]

    except ContextOverflowError as coe:
        logger.warning(
            "Routing evaluator hit context overflow (~%d tokens vs %d window) "
            "— falling back to first target",
            coe.estimated_tokens,
            coe.context_window,
        )
        return fallback or mandatory_targets[0]
    except Exception as exc:
        logger.error(
            "Routing evaluator failed: %s — falling back to first target", format_exception_message(exc)
        )
        return fallback or mandatory_targets[0]

