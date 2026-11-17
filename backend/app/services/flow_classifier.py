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

"""The flow Classify node's call to a decision model (CLM).

``condition.classify`` routes a flow on what a classifier language model (Jev,
Clef, ...) decides about a piece of content: one typed *choice* question, one
output socket per label, and the model's confidence to hold back what it is
unsure of. The typical use is a filter in front of an agent — sort the alerts a
SIEM or an ITSM such as TheHive pushes, and only the relevant ones reach the
LLM, the rest never spend an agent's tokens.

CLMs are served by the platform's LLM providers (``app.llm.decisions``); this
module picks the provider config a node runs on and makes the call:

* **A pinned provider is honoured** — and refused, never swapped, when it is
  gone, withheld from the caller, or serves no decision model.
* **Unpinned, the caller's usable providers that serve decision models**:
  the first one listing the model on its card (``usable_provider_configs``
  order: granted first, the admin's default first), else the one the
  platform's Decision Model setting names for this model, else the only
  candidate (an OpenAI-compatible endpoint alone only once its listing
  declares a decision model, ``decisions_only_when_listed``), else, while
  every listing answered in full, the only one whose own decision listing
  reports the model, or with none the only one that cannot list
  (``_only_provider_listing``, remembered); otherwise the node asks for a pin
  instead of guessing. A whole listing without the model rules its provider
  out; a pin or the platform setting still reaches it.
* **A failure is an error, not a branch.** Unlike an LLM-judged condition,
  which falls back to its conservative branch, an unanswered classification
  takes the node's error path: a filter that silently drops an alert because
  the provider was down is the failure this node must not have. The message
  stored on the step is generic; the provider's own wording goes to the logs.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from app.llm.decisions import (
    DECISION_LISTING_TIMEOUT_SECONDS,
    ChoiceDecision,
    DecisionResponseError,
    DecisionsNotSupportedError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import BaseLLMProvider
    from app.models.llm_config import LLMProviderConfig

logger = logging.getLogger(__name__)

# What the model reads is bounded: decision models take a 32K-64K token
# context, and a runaway upstream payload must not turn into a huge bill.
MAX_STATE_CHARS = 60_000
MAX_INSTRUCTIONS_CHARS = 4_000
MAX_LABEL_DESCRIPTION_CHARS = 1_000

NO_DECISION_PROVIDER_MESSAGE = (
    "No LLM provider available to you serves decision models. An "
    "administrator can add an OpenRouter, OpenAI, Ollama, Filigran AI Gateway or "
    "custom OpenAI-compatible provider in Settings > AI and models > Models."
)


AMBIGUOUS_PROVIDER_MESSAGE = (
    "Several LLM providers available to you serve decision models and none "
    "lists this model. Pick the provider on this node, or an administrator can "
    "set this model and its provider as the Decision Model in Settings > AI and models > Models."
)

UNLISTED_MODEL_MESSAGE = (
    "No LLM provider available to you lists this decision model. Pick the "
    "provider on this node, or an administrator can set this model and its "
    "provider as the Decision Model in Settings > AI and models > Models."
)

UNANSWERED_LISTING_MESSAGE = (
    "These LLM providers did not answer in full when asked for their decision "
    "models, so the one serving this model is not known: {providers}. Pick the "
    "provider on this node, or try again later."
)


#: What a sweep of decision listings found, per candidate set and model: a
#: Classify node runs once per alert, and a sweep asks every candidate. The one
#: candidate is kept a minute; none or several (``None``, with the message to
#: raise), or an answer drawn while a listing failed, briefly, so a provider
#: coming back is soon noticed.
_RESOLVED_TTL_SECONDS = 60.0
_UNRESOLVED_TTL_SECONDS = 15.0
_MAX_RESOLVED = 256
_resolved: dict[tuple[frozenset[str], str], tuple[float, str | None, str]] = {}
#: The sweep running per key, which concurrent executions share.
_inflight: dict[tuple[frozenset[str], str], asyncio.Task[tuple[str | None, str]]] = {}


class ClassifierError(Exception):
    """A classification that produced no decision; the message is safe to store."""


def decision_provider_types() -> list[str]:
    """Provider types whose class serves decision models."""
    from app.llm.router import PROVIDER_MAP

    return sorted(
        name
        for name, cls in PROVIDER_MAP.items()
        if getattr(cls, "decisions_api", None)
    )


async def _pinned_config(
    db: AsyncSession, provider_id: uuid.UUID, user_id: str | None
) -> LLMProviderConfig:
    from app.llm.provider_access import provider_id_usable_by
    from app.models.llm_config import LLMProviderConfig

    config = await db.get(LLMProviderConfig, provider_id)
    if config is None:
        raise ClassifierError("The LLM provider picked on this node no longer exists.")
    if not await provider_id_usable_by(db, config.id, user_id):
        raise ClassifierError(
            "The LLM provider picked on this node is not available to you."
        )
    if config.provider not in decision_provider_types():
        raise ClassifierError(
            "The LLM provider picked on this node does not serve decision "
            "models. Pick an OpenRouter, OpenAI, Ollama, Filigran AI Gateway or "
            "custom OpenAI-compatible provider."
        )
    return config


async def resolve_decision_provider(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID | None,
    model: str,
    user_id: str | None,
) -> BaseLLMProvider:
    """The provider instance a Classify node runs on. Raises ClassifierError."""
    from app.llm.provider_access import usable_provider_configs
    from app.llm.router import provider_for_config

    if provider_id is not None:
        config = await _pinned_config(db, provider_id, user_id)
    else:
        configs = await usable_provider_configs(db, user_id, decision_provider_types())
        if not configs:
            raise ClassifierError(NO_DECISION_PROVIDER_MESSAGE)
        listing = [c for c in configs if model in (c.available_models or [])]
        if listing:
            config = listing[0]
        elif chosen := await _platform_choice(db, configs, model):
            config = chosen
        elif len(configs) == 1 and not _only_when_listed(configs[0]):
            config = configs[0]
        else:
            config = await _only_provider_listing(configs, model)
    return await provider_for_config(config)


def _only_when_listed(config: LLMProviderConfig) -> bool:
    from app.llm.router import PROVIDER_MAP

    return bool(getattr(PROVIDER_MAP.get(config.provider), "decisions_only_when_listed", False))


def _label(config: LLMProviderConfig) -> str:
    from app.llm.attribution import config_identity

    return config_identity(config)[1] or config.provider


async def _platform_choice(
    db: AsyncSession, configs: list[LLMProviderConfig], model: str
) -> LLMProviderConfig | None:
    """The candidate the platform's Decision Model setting names for *model*: the administrator's own choice."""
    from app.services.flow_llm import decision_model

    platform_model, provider_id = await decision_model(db)
    if platform_model != model or provider_id is None:
        return None
    return next((c for c in configs if c.id == provider_id), None)


async def _only_provider_listing(configs: list[LLMProviderConfig], model: str) -> LLMProviderConfig:
    """The candidate whose decision listing reports *model*, else the only one that cannot list, while every listing answered in full.

    Decision models are seldom in a provider's chat catalogue, so each
    candidate's own decision listing is asked. A listing that failed or is
    partial (OpenAI's round) may omit the model its provider serves, so it
    stops the resolution and is named. A whole listing without the model rules
    its provider out. Otherwise one reporting provider wins; with none, the
    only provider that cannot list (``lists_decision_models``: OpenRouter's
    typed ids) does; else ask for a pin.
    A found provider is remembered a minute, anything else briefly, and
    concurrent executions share one sweep; an edited config is a new
    candidate set.
    """
    key = (frozenset(f"{c.id}@{c.updated_at}" for c in configs), model)
    hit = _resolved.get(key)
    if hit is not None and hit[0] > time.monotonic():
        _, winner_id, message = hit
    else:
        task = _inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(_sweep(configs, model, key))
            _inflight[key] = task
            task.add_done_callback(functools.partial(_forget_sweep, key))
        winner_id, message = await asyncio.shield(task)
    winner = next((c for c in configs if str(c.id) == winner_id), None)
    if winner is None:
        raise ClassifierError(message)
    return winner


def _forget_sweep(key: tuple[frozenset[str], str], task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        del _inflight[key]


async def _sweep(
    configs: list[LLMProviderConfig], model: str, key: tuple[frozenset[str], str]
) -> tuple[str | None, str]:
    """The winner's id (``None`` for none) and the message to raise without one; remembered under *key*."""
    from app.llm.router import provider_for_config

    async def answer(config: LLMProviderConfig) -> str:
        """``reports``, ``unlisted`` (a provider that cannot list), ``omits`` (a whole listing without it) or ``failed``."""
        try:
            provider = await provider_for_config(config)
            if not provider.lists_decision_models:
                return "unlisted"
            async with asyncio.timeout(DECISION_LISTING_TIMEOUT_SECONDS):
                models, whole = await provider.decision_listing()
        except Exception as exc:
            logger.warning("Decision models of provider %s not listed: %.300r", _label(config), str(exc))
            return "failed"
        if model in models:
            return "reports"
        if not whole:
            logger.warning("Decision models of provider %s listed in part", _label(config))
            return "failed"
        return "omits"

    answers = await asyncio.gather(*(answer(config) for config in configs))
    by_answer: dict[str, list[LLMProviderConfig]] = {}
    for config, outcome in zip(configs, answers, strict=True):
        by_answer.setdefault(outcome, []).append(config)
    winner: LLMProviderConfig | None = None
    if failed := by_answer.get("failed"):
        message = UNANSWERED_LISTING_MESSAGE.format(providers=", ".join(_label(c) for c in failed))
    elif reporting := by_answer.get("reports"):
        winner = reporting[0] if len(reporting) == 1 else None
        message = AMBIGUOUS_PROVIDER_MESSAGE
    else:
        unlisted = by_answer.get("unlisted", [])
        winner = unlisted[0] if len(unlisted) == 1 else None
        message = AMBIGUOUS_PROVIDER_MESSAGE if unlisted else UNLISTED_MODEL_MESSAGE
    ttl = _RESOLVED_TTL_SECONDS if winner is not None else _UNRESOLVED_TTL_SECONDS
    if key not in _resolved and len(_resolved) >= _MAX_RESOLVED:
        del _resolved[min(_resolved, key=lambda k: _resolved[k][0])]
    winner_id = str(winner.id) if winner is not None else None
    _resolved[key] = (time.monotonic() + ttl, winner_id, message)
    return winner_id, message


async def decision_context_window(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID | None,
    model: str,
    user_id: str | None,
) -> int | None:
    """The context the decision model is served with; ``None`` when unknown or unresolved.

    An unresolved provider is left to :func:`classify`, which reports it.
    """
    try:
        provider = await resolve_decision_provider(
            db, provider_id=provider_id, model=model, user_id=user_id
        )
    except ClassifierError:
        return None
    return await provider.decision_context_window(model)


def parse_provider_id(value: object) -> uuid.UUID | None:
    """The node's ``provider`` field: a config id, or None to inherit."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise ClassifierError(
            "The LLM provider picked on this node is not a valid id."
        ) from None


async def classify(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID | None,
    model: str,
    state: str,
    instructions: str,
    choices: dict[str, str],
    user_id: str | None,
    trace_name: str = "flow_classifier",
) -> ChoiceDecision:
    """Ask the decision model which of *choices* *state* belongs to.

    Raises :class:`ClassifierError` when no decision came back, whatever the
    reason, with a message that quotes nothing the provider said.
    """
    from openai import APIStatusError

    from app.llm.attribution import provider_config_metadata
    from app.services.langfuse_tracing import traced_generation

    try:
        provider = await resolve_decision_provider(
            db, provider_id=provider_id, model=model, user_id=user_id
        )
        async with traced_generation(
            name=trace_name,
            model=model,
            messages=[{"role": "user", "content": state}],
            system_prompt=instructions,
            metadata=provider_config_metadata(provider),
        ) as gen:
            decision = await provider.decide_choice(
                model=model,
                state=state,
                instructions=instructions,
                choices=choices,
            )
            gen.content = json.dumps(
                {"label": decision.choice, "confidence": decision.confidence}
            )
            gen.usage = decision.usage or None
            gen.model = decision.model or model
        return decision
    except ClassifierError:
        raise
    except (DecisionsNotSupportedError, DecisionResponseError) as exc:
        # Our own wording, never the provider's.
        logger.warning("Flow classifier got no decision from %.200r: %s", model, exc)
        raise ClassifierError(str(exc)) from exc
    except APIStatusError as exc:
        logger.warning(
            "Flow classifier call to %.200r failed with HTTP %s: %.300r",
            model,
            exc.status_code,
            str(exc),
        )
        raise ClassifierError(
            f"The decision model call failed (HTTP {exc.status_code})."
        ) from exc
    # Broad by intent: whatever the transport raised, the node takes its error
    # path with a generic message and the detail stays in the logs.
    except Exception as exc:
        logger.warning("Flow classifier call to %.200r failed: %.300r", model, str(exc))
        raise ClassifierError("The decision model could not be reached.") from exc
