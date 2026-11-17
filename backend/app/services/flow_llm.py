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

"""Resolution of a ``capability.llm`` node's settings into a live provider.

One place where a wired LLM node's config becomes *(provider, model, sampling
parameters)*, shared by every consumer of the ``llm`` socket — the condition
evaluators (``flow_condition_llm``) and the Call-tool selector
(``flow_tool_selector``).

Two properties this module exists to guarantee:

* **A pinned provider is honoured.**  Model ids are not unique across
  providers — a gateway republishes its upstream's catalog verbatim, and the
  same ``claude-sonnet-4-5`` can be served by two configured rows.  The node
  stores the provider *instance* id, and it is passed as
  ``provider_id_override`` so the call lands where the author said it would.
* **A malformed setting never decides a branch.**  Config travels in a JSON
  document that hand-written API calls and older exports can shape freely, so
  every field is parsed defensively: anything unreadable degrades to "inherit
  the platform default" rather than raising inside a walker step.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# Sampling bounds mirrored from the catalog's slider so a hand-written config
# cannot send a provider something it will reject.
_TEMPERATURE_MIN = 0.0
_TEMPERATURE_MAX = 2.0
_MAX_TOKENS_CEILING = 32000

_REASONING_EFFORTS = frozenset({"low", "medium", "high", "max"})


def _clean_str(value: Any) -> str | None:
    """A non-empty trimmed string, or None. ``""`` means "inherit"."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


@dataclass(frozen=True)
class LlmSettings:
    """What a ``capability.llm`` node declares. Every field is optional."""

    provider_id: uuid.UUID | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    instructions: str | None = None

    @classmethod
    def from_node_config(cls, config: dict | None) -> LlmSettings:
        """Parse a wired LLM node's config. Never raises."""
        config = config if isinstance(config, dict) else {}

        provider_id: uuid.UUID | None = None
        raw_provider = _clean_str(config.get("provider"))
        if raw_provider:
            try:
                provider_id = uuid.UUID(raw_provider)
            except (ValueError, AttributeError, TypeError):
                # A stale or hand-written provider value must not kill the
                # step: fall back to the platform's own resolution. The
                # validator surfaces the dangling reference while authoring.
                logger.warning(
                    "Flow LLM node names an unreadable provider %r — "
                    "falling back to the platform default.",
                    raw_provider,
                )

        temperature: float | None = None
        raw_temperature = config.get("temperature")
        if isinstance(raw_temperature, (int, float)) and not isinstance(
            raw_temperature, bool
        ):
            temperature = min(
                max(float(raw_temperature), _TEMPERATURE_MIN), _TEMPERATURE_MAX
            )

        max_tokens: int | None = None
        raw_max_tokens = config.get("max_tokens")
        if isinstance(raw_max_tokens, (int, float)) and not isinstance(
            raw_max_tokens, bool
        ):
            max_tokens = min(max(int(raw_max_tokens), 1), _MAX_TOKENS_CEILING)

        effort = _clean_str(config.get("reasoning_effort"))
        if effort is not None and effort not in _REASONING_EFFORTS:
            effort = None

        return cls(
            provider_id=provider_id,
            model=_clean_str(config.get("model")),
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=effort,
            instructions=_clean_str(config.get("instructions")),
        )

    def chat_kwargs(self, *, default_max_tokens: int) -> dict[str, Any]:
        """Sampling kwargs for ``BaseLLMProvider.chat``.

        *default_max_tokens* is the caller's own cheap default — a yes/no
        condition wants a handful of tokens, an argument object wants room —
        and an explicit node setting overrides it.
        """
        kwargs: dict[str, Any] = {
            "max_tokens": self.max_tokens or default_max_tokens
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.reasoning_effort:
            # Forwarded through **kwargs; providers that do not expose an
            # effort knob ignore it (see openai/anthropic providers).
            kwargs["reasoning_effort"] = self.reasoning_effort
        return kwargs


async def resolve_llm(
    db: AsyncSession,
    settings: LlmSettings,
    *,
    user_id: str | None,
    fallback_model: str | None = None,
    fallback_provider_id: uuid.UUID | None = None,
) -> tuple[BaseLLMProvider, str | None]:
    """Resolve *settings* into ``(provider, model)``.

    *fallback_model* is used when the node pins no model — callers pass the
    platform's routing model so a judgement stays on the cheap path. Raises
    whatever the router raises; callers own the failure branch, because what a
    failure MEANS differs (a condition takes its conservative branch, a tool
    selection fails the node down its error edge).

    *fallback_provider_id* rides along with it: the routing model is a bare id,
    so without its owning config the router would resolve it newest-config-first
    and the judgement could run on a different key than the admin picked.
    Only used when the node pins no model of its own.
    """
    from app.llm.router import get_llm_for_user

    node_pins_model = bool(settings.model)
    return await get_llm_for_user(
        db,
        user_id,
        model_override=settings.model or fallback_model or None,
        provider_id_override=(
            settings.provider_id
            if node_pins_model or settings.provider_id
            else fallback_provider_id
        ),
    )


async def routing_model(db: AsyncSession) -> tuple[str | None, uuid.UUID | None]:
    """The platform's configured routing model and the config that serves it."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    model = getattr(ps, "routing_model", None) or None
    if model is None:
        return None, None
    return model, getattr(ps, "routing_provider_id", None) or None


async def decision_model(db: AsyncSession) -> tuple[str | None, uuid.UUID | None]:
    """The platform's configured decision model and the config that serves it."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    model = getattr(ps, "decision_model", None) or None
    if model is None:
        return None, None
    return model, getattr(ps, "decision_provider_id", None) or None
