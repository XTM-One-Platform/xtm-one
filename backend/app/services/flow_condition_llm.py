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

"""LLM-backed condition evaluation for flow branch nodes.

A ``condition.if`` / ``condition.switch`` node is deterministic until an
``capability.llm`` node is wired into its ``llm`` socket; then the branch is
decided by a single lightweight classification call instead of a string
comparison.  This module is that call.

Modelled on ``routing_evaluator.py`` (mandatory-transfer routing), and it keeps
the same properties, which matter for a graph walker:

* **One short call, no tools, no memory.**  The model classifies, it does not
  act — so a condition can never reach outside the flow.
* **The failure mode is a branch, not an exception.**  An unreachable provider
  or an unparseable answer resolves to the false / default branch and says so
  in the reason, because a run that dies at a fork is worse than one that took
  the conservative path.
* **The answer is constrained to what the graph declares.**  The model picks a
  number from the offered branches; anything else is treated as "no match".
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message
from app.llm.cut_answers import chat_retrying_a_cut
from app.services.flow_llm import LlmSettings, resolve_llm, routing_model
from app.services.langfuse_tracing import traced_generation

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.llm.base import ChatResponse

logger = logging.getLogger(__name__)

# The judgement is a number (or yes/no); a handful of tokens is plenty and
# caps the cost of a fork.
_MAX_TOKENS = 10
# Values are interpolated into the prompt — bound them so a large upstream
# payload cannot blow the context window at a branch.
_MAX_VALUE_CHARS = 4000
_YES_WORDS = ("yes", "true")
_NO_WORDS = ("no", "false")
_OPTION_NUMBER = re.compile(r"\d+")


# A decision read from the start of the answer: a cut after it leaves it whole,
# so only an answer cut before it is asked again.
def _answers_yes_or_no(response: ChatResponse) -> bool:
    return (response.content or "").strip().lower().startswith(_YES_WORDS + _NO_WORDS)


def _names_an_option(response: ChatResponse) -> bool:
    return _OPTION_NUMBER.search(response.content or "") is not None


async def _classify(
    db: AsyncSession,
    *,
    system_prompt: str,
    user_prompt: str,
    settings: LlmSettings,
    user_id: str | None,
    decided: Callable[[ChatResponse], bool],
) -> str | None:
    """Run the classification call; None when the model could not be reached."""
    from app.llm.base import ChatMessage

    try:
        # Inside the ``try`` on purpose: ``routing_model`` reads (and can
        # create) the platform-settings singleton, so a session error here must
        # take the same contained path as a resolution failure.
        routing_fallback_model, routing_fallback_provider_id = await routing_model(db)
        llm, resolved_model = await resolve_llm(
            db,
            settings,
            user_id=user_id,
            fallback_model=routing_fallback_model,
            fallback_provider_id=routing_fallback_provider_id,
        )
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]
        # A yes/no answer stays on the cheap path unless the node explicitly
        # asked for more room.
        chat_kwargs = settings.chat_kwargs(default_max_tokens=_MAX_TOKENS)
        if not settings.reasoning_effort:
            # A mechanical classification: on the models that think by
            # default, thinking would share the few tokens with the answer. A
            # node that asked for a reasoning effort keeps it.
            chat_kwargs["disable_thinking"] = True

        async def _call(max_tokens: int):
            async with traced_generation(
                name="flow_condition_classifier",
                model=resolved_model,
                messages=messages,
            ) as _gen:
                _gen.response = await llm.chat(
                    messages=messages,
                    model=resolved_model,
                    **{**chat_kwargs, "max_tokens": max_tokens},
                )
            return _gen.response

        response, cut = await chat_retrying_a_cut(
            llm,
            resolved_model,
            chat_kwargs["max_tokens"],
            _call,
            label="Flow condition classifier",
            decided=decided,
        )
        if cut:
            # Cut before it gave a verdict, twice: *decided* found none, so the
            # caller reads it as any answer without one, the False / Default
            # branch with the answer quoted in the reason.
            logger.warning(
                "Flow condition classifier answer still cut at max_tokens; "
                "taking the fallback branch."
            )
        return (response.content or "").strip()
    # Broad by intent: a fork must not kill the run, whatever the provider did.
    except Exception as exc:
        logger.warning(
            "Flow condition classifier failed (%s); taking the fallback branch.",
            format_exception_message(exc),
        )
        return None


async def evaluate_question(
    db: AsyncSession,
    *,
    question: str,
    llm_config: dict | None = None,
    user_id: str | None = None,
) -> tuple[bool, str]:
    """Answer a yes/no *question*. Returns ``(verdict, reason)``.

    An unreachable model or an unreadable answer is False — the conservative
    branch — with the reason recorded on the step so the run stays explainable.
    """
    settings = LlmSettings.from_node_config(llm_config)
    guidance = settings.instructions or ""
    system_prompt = (
        "You judge a yes/no question about the data you are given. "
        "Answer with ONLY 'yes' or 'no'."
    )
    if guidance:
        system_prompt += f"\n\nAdditional guidance:\n{guidance}"

    answer = await _classify(
        db,
        system_prompt=system_prompt,
        user_prompt=f"{question[:_MAX_VALUE_CHARS]}\n\nAnswer yes or no.",
        settings=settings,
        user_id=user_id,
        decided=_answers_yes_or_no,
    )
    if answer is None:
        return False, "The model could not be reached — took the False branch."

    normalized = answer.strip().lower()
    if normalized.startswith(_YES_WORDS):
        return True, f"Model answered “{answer[:80]}”."
    if normalized.startswith(_NO_WORDS):
        return False, f"Model answered “{answer[:80]}”."
    return False, f"Unreadable answer “{answer[:80]}” — took the False branch."


async def evaluate_cases(
    db: AsyncSession,
    *,
    value: str,
    cases: dict[str, str],
    llm_config: dict | None = None,
    user_id: str | None = None,
) -> tuple[str | None, str]:
    """Pick the case *value* belongs to. Returns ``(case_name, reason)``.

    ``None`` means no case fits (the caller takes its default branch), which is
    also what an unreachable model resolves to.
    """
    if not cases:
        return None, "No cases are configured."

    names = list(cases.keys())
    options = "\n".join(
        f"  {i + 1}. {name}: {cases[name]}" for i, name in enumerate(names)
    )
    none_idx = len(names) + 1

    settings = LlmSettings.from_node_config(llm_config)
    guidance = settings.instructions or ""
    system_prompt = (
        "You are a classifier. Given a value, choose the option that best "
        "describes it. Respond with ONLY the number of that option."
    )
    if guidance:
        system_prompt += f"\n\nAdditional guidance:\n{guidance}"

    user_prompt = (
        f"## Value\n{value[:_MAX_VALUE_CHARS]}\n\n"
        f"## Options\n{options}\n  {none_idx}. None of the above\n\n"
        f"Which option (1-{none_idx}) best matches? Reply with the number only."
    )

    answer = await _classify(
        db,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        settings=settings,
        user_id=user_id,
        decided=_names_an_option,
    )
    if answer is None:
        return None, "The model could not be reached — took the Default branch."

    match = _OPTION_NUMBER.search(answer)
    if match:
        idx = int(match.group()) - 1
        if 0 <= idx < len(names):
            return names[idx], f"Model chose “{names[idx]}”."
    return None, f"No case matched (“{answer[:80]}”) — took the Default branch."
