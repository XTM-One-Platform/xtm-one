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

"""LLM-backed tool selection for the Call-tool node.

An ``action.call_tool`` node is a static invocation until a ``capability.llm``
node is wired into its ``llm`` socket; then the tool and its arguments are
decided by one bounded call instead of being typed at design time. This module
is that call.

Modelled on ``flow_condition_llm.py`` and keeping the same properties, which
are what make a model-made decision safe inside a graph walker:

* **One bounded call, no memory, no agentic loop.** The model chooses; it never
  observes a result and calls again. That shape is an Agent node, deliberately
  not this one.
* **The answer is structurally constrained.** The candidates' real JSON schemas
  are passed as the ONLY tools the provider sees, so the choice comes back as a
  native tool call rather than prose to be parsed — and a name outside the
  declared set is refused rather than executed.
* **This module never executes anything.** It returns a selection; the runner
  runs the tool it offered (``run_offered_tool``): a custom tool by its id,
  one its name designates in the flow's reach, as in deterministic mode, and
  that the run's user can use too.
* **The failure mode is an explicit error, not an exception.** An unreachable
  provider, no tool chosen, or arguments that never satisfy the schema come
  back as a ``ToolSelection`` carrying ``error`` — the node fails down its
  ``error`` edge with a message naming which of those happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message
from app.llm.cut_answers import chat_retrying_a_cut
from app.services.flow_llm import LlmSettings, resolve_llm, routing_model
from app.services.langfuse_tracing import traced_generation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

# Room for an argument object, not an essay. The condition classifier's 10-token
# budget is right for yes/no and far too small here; an explicit `max_tokens` on
# the LLM node overrides this.
_DEFAULT_MAX_TOKENS = 2048
# The objective is interpolated from upstream outputs — bound it so a large
# payload upstream cannot blow the context window at this step.
_MAX_OBJECTIVE_CHARS = 8000
_MAX_HINT_CHARS = 4000
# A selection is one decision; more than a handful of candidates means the
# author should have split the step. Kept generous, but not unbounded: every
# candidate's schema rides in the request.
MAX_CANDIDATE_TOOLS = 32


@dataclass
class ToolSelection:
    """What the model decided, or why it could not."""

    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    error: str | None = None
    # How many model calls it took (1 unless arguments had to be corrected or
    # an answer cut at its budget was asked again).
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.tool_name)


def _missing_required(definition: ToolDefinition, arguments: dict) -> list[str]:
    """Required properties the model left out.

    Absence only. An empty string is a *value*, and whether it is an
    acceptable one is the schema's business (``minLength``) — treating it as
    missing would burn a retry and then fail a node whose required argument
    legitimately accepts ``""``.
    """
    schema = definition.input_schema or {}
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [
        name
        for name in required
        if isinstance(name, str) and arguments.get(name) is None
    ]


def _describe_candidates(definitions: list[ToolDefinition]) -> str:
    """The offered tools, for the prompt and for error messages."""
    return ", ".join(d.name for d in definitions)


async def select_tool(
    db: AsyncSession,
    *,
    objective: str,
    definitions: list[ToolDefinition],
    llm_config: dict | None = None,
    argument_hints: str | None = None,
    max_attempts: int = 2,
    user_id: str | None = None,
) -> ToolSelection:
    """Pick one of *definitions* and fill its arguments to meet *objective*.

    *definitions* are the candidate tools the author declared, with their real
    schemas — the model is offered these and nothing else.  Returns a
    :class:`ToolSelection`; check ``ok`` before using it.
    """
    from app.llm.base import ChatMessage

    if not definitions:
        return ToolSelection(
            error=(
                "No candidate tools are configured — pick at least one tool "
                "the model may choose from."
            )
        )
    if len(definitions) > MAX_CANDIDATE_TOOLS:
        return ToolSelection(
            error=(
                f"{len(definitions)} candidate tools were configured; at most "
                f"{MAX_CANDIDATE_TOOLS} may be offered in one selection. Split the "
                "step, or narrow the list."
            )
        )
    objective = (objective or "").strip()
    if not objective:
        return ToolSelection(
            error="No objective is configured — the model has nothing to act on."
        )

    settings = LlmSettings.from_node_config(llm_config)
    by_name = {d.name: d for d in definitions}

    system_prompt = (
        "You select exactly ONE tool to call, and fill its arguments.\n"
        "Rules:\n"
        "- You MUST call one of the provided tools. Never answer in prose.\n"
        "- Choose the single tool that best achieves the objective.\n"
        "- Fill arguments only from the objective and the guidance given; "
        "never invent identifiers, URLs or credentials.\n"
        "- Leave an optional argument out rather than guessing a value.\n"
        "The tool is executed by the caller, not by you: you will not see its "
        "result and will not be asked again."
    )
    if settings.instructions:
        system_prompt += f"\n\nAdditional guidance:\n{settings.instructions}"

    user_prompt = f"## Objective\n{objective[:_MAX_OBJECTIVE_CHARS]}"
    hints = (argument_hints or "").strip()
    if hints:
        user_prompt += f"\n\n## Argument guidance\n{hints[:_MAX_HINT_CHARS]}"
    user_prompt += (
        f"\n\nCall exactly one of: {_describe_candidates(definitions)}."
    )

    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]

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
    # Broad by intent: the node must fail down its error edge with a readable
    # message, not raise inside the walker.
    except Exception as exc:
        detail = format_exception_message(exc)
        pinned = (
            f" (provider {settings.provider_id})" if settings.provider_id else ""
        )
        logger.warning("Flow tool selector could not resolve an LLM: %s", detail)
        return ToolSelection(
            error=f"The configured model could not be reached{pinned}: {detail}"
        )

    attempts = max(1, min(int(max_attempts or 1), 5))
    chat_kwargs = settings.chat_kwargs(default_max_tokens=_DEFAULT_MAX_TOKENS)
    # The budget an attempt starts from: raised to what a cut answer's retry
    # needed, so a correction does not begin with a request cut the same way.
    budget = chat_kwargs["max_tokens"]
    last_problem = ""
    made = 0
    # Model calls, the retries of a cut answer included (``attempts`` counts
    # them).
    model_calls = 0

    for attempt in range(1, attempts + 1):
        made = attempt
        answered_with = budget

        async def _call(max_tokens: int, _messages=messages):
            nonlocal model_calls, answered_with
            model_calls += 1
            answered_with = max_tokens
            async with traced_generation(
                name="flow_tool_selector",
                model=resolved_model,
                messages=_messages,
            ) as _gen:
                _gen.response = await llm.chat(
                    messages=_messages,
                    model=resolved_model,
                    tools=definitions,
                    **{**chat_kwargs, "max_tokens": max_tokens},
                )
            return _gen.response

        try:
            response, cut = await chat_retrying_a_cut(
                llm,
                resolved_model,
                budget,
                _call,
                label="Flow tool selector",
            )
        except Exception as exc:
            detail = format_exception_message(exc)
            logger.warning("Flow tool selector call failed: %s", detail)
            return ToolSelection(
                attempts=model_calls,
                error=f"The model could not be reached: {detail}",
            )
        if cut:
            # Cut tool arguments are not a call: the error edge, as a failed
            # call takes it.
            logger.warning("Flow tool selector answer still cut at max_tokens")
            return ToolSelection(
                attempts=model_calls,
                error=(
                    "The model's answer was cut at its output budget "
                    "(max_tokens); raise max_tokens on the LLM node."
                ),
            )
        budget = answered_with

        calls = response.tool_calls or []
        if not calls:
            last_problem = (
                "You answered in prose. Call one of the offered tools instead: "
                f"{_describe_candidates(definitions)}."
            )
        else:
            call = calls[0]
            definition = by_name.get(call.name)
            if definition is None:
                # A name outside the declared set is refused, never executed —
                # the author's list is the node's whole reach.
                last_problem = (
                    f"`{call.name}` is not one of the offered tools. Choose "
                    f"from: {_describe_candidates(definitions)}."
                )
            else:
                arguments = (
                    call.arguments if isinstance(call.arguments, dict) else {}
                )
                missing = _missing_required(definition, arguments)
                if missing:
                    last_problem = (
                        f"`{definition.name}` requires "
                        f"{', '.join(f'`{m}`' for m in missing)}, which "
                        "you did not provide. Call it again with every "
                        "required argument."
                    )
                else:
                    reason = (response.content or "").strip()
                    if len(calls) > 1:
                        # One decision per node: extra calls are dropped, and
                        # the step says so rather than silently picking one.
                        reason = (
                            f"{reason} (the model proposed "
                            f"{len(calls)} calls; the first was used)"
                        ).strip()
                    return ToolSelection(
                        tool_name=definition.name,
                        arguments=arguments,
                        reason=reason
                        or f"Model chose “{definition.name}”.",
                        attempts=model_calls,
                    )

        if attempt < attempts:
            # Feed the problem back rather than re-asking the same question:
            # a corrective turn is what makes the retry worth its cost.
            #
            # The assistant turn is echoed ONLY when the model actually wrote
            # prose. Both cases this retry corrects (a name outside the set, a
            # missing required argument) normally come back as a tool call with
            # no text, and an assistant message with empty content is rejected
            # outright by providers that forbid empty content blocks —
            # Anthropic 400s on it, which would have made every second attempt
            # fail on the default provider and report itself as "the model
            # could not be reached".
            #
            # The `tool_calls` being corrected are deliberately NOT echoed
            # either: a `tool_use` block with no matching `tool_result` is
            # rejected for the same reason, and there is no result to give —
            # the call was refused, not executed. The correction text names
            # what was wrong, which is what the model needs.
            if (response.content or "").strip():
                messages = [
                    *messages,
                    ChatMessage(role="assistant", content=response.content),
                ]
            messages = [*messages, ChatMessage(role="user", content=last_problem)]

    # `attempts` is telemetry: report the calls actually made, not the budget
    # they were allowed.
    return ToolSelection(
        attempts=model_calls,
        error=(
            f"The model did not produce a usable tool call after {made} "
            f"attempt(s). Last problem: {last_problem}"
        ),
    )
