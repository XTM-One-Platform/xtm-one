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

"""One structured model call of the pipeline, on the provider the run resolved.

The pipeline never resolves a model of its own: it runs on the provider and model of the
agent turn that called it, so an evaluation comparing models compares the whole pipeline
on each, and a team's provider rules apply unchanged. Each call asks for the step's JSON
schema natively when the provider supports it, parses the answer with the platform's
tolerant parser (``structured_output.validate_and_parse``), retries once when the answer
cannot be read (an answer cut at its token budget is not asked again: it would be cut
again), keeps of an answer only the values whose JSON type its schema allows
(:func:`conform`), and adds its token usage to the run's tally. Inside an
agent loop each call spends the run's allowance (``ToolModelSpend``): its usage is the
run's, and a call the run can no longer pay for is not made.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

#: A rough prompt size in tokens from its characters, to price a call before making it.
_CHARS_PER_TOKEN = 4


class StructuredCall(Protocol):
    """What the pipeline needs from a model: a system and a user message in, a dict
    following *schema* out (``None`` when no readable answer came back)."""

    def __call__(self, *, step: str, system: str, user: str, schema: dict) -> Awaitable[dict | None]: ...


@dataclass
class Usage:
    """The tokens and calls a pipeline run used."""

    calls: int = 0
    failed_calls: int = 0
    #: Calls not made because the run could no longer pay for them.
    refused_calls: int = 0
    #: Calls the model declined to answer (its own refusal, e.g. on exploit details):
    #: not retried, as the same request is declined again.
    declined_calls: int = 0
    #: Answers off their schema of which nothing could be kept (a bare array, an empty text).
    off_schema_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_step: dict[str, int] = field(default_factory=dict)

    def add(self, step: str, usage: dict | None) -> None:
        self.calls += 1
        self.by_step[step] = self.by_step.get(step, 0) + 1
        self.tokens(usage)

    def tokens(self, usage: dict | None) -> None:
        """Count *usage*'s tokens (a call that answered, or one billed although it failed)."""
        if not isinstance(usage, dict):
            return
        # Prompt tokens served from a provider's cache are reported apart (Anthropic):
        # they are still read, and billed at their own rate.
        self.input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) + int(
            usage.get("cache_read_input_tokens") or 0
        ) + int(usage.get("cache_creation_input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "refused_calls": self.refused_calls,
            "declined_calls": self.declined_calls,
            "off_schema_calls": self.off_schema_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "calls_by_step": dict(sorted(self.by_step.items())),
        }


def _may_drop_structured_output(llm: object) -> bool:
    """Whether *llm* is a failover whose fallback provider drops ``structured_output``."""
    from app.llm.router import FailoverLLMProvider

    return isinstance(llm, FailoverLLMProvider)


def provider_structured_call(
    llm: object,
    model: str | None,
    usage: Usage,
    *,
    max_tokens: int = 16_000,
    max_concurrency: int = 4,
) -> StructuredCall:
    """A :class:`StructuredCall` on *llm* / *model*, at most *max_concurrency* at once."""
    from app.llm.base import ChatMessage, ModelDeclinedError
    from app.llm.cut_answers import answer_was_cut
    from app.services.langfuse_tracing import traced_generation
    from app.services.structured_output import build_native_structured_output, build_schema_prompt, validate_and_parse
    from app.tools.base import current_tool_model_spend

    gate = asyncio.Semaphore(max_concurrency)

    def _spent(exc: BaseException, step: str) -> None:
        """A call that failed after the provider billed it (a declined answer, a stream cut
        partway) still spent its tokens: they are the run's, in its totals and budget."""
        billed = getattr(exc, "usage", None)
        if isinstance(billed, dict) and billed:
            usage.tokens(billed)
            spend = current_tool_model_spend()
            if spend is not None:
                spend.record(billed, getattr(exc, "model", None) or None)

    async def call(*, step: str, system: str, user: str, schema: dict) -> dict | None:
        native = build_native_structured_output(llm, schema)
        if native is None or _may_drop_structured_output(llm):
            # The format is said in the prompt where the provider cannot enforce it, or where a
            # failover's fallback drops the native format: the answer still has its shape.
            system = f"{system}\n\n{build_schema_prompt(schema)}"
        messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        estimated_input_tokens = (len(system) + len(user)) // _CHARS_PER_TOKEN
        for attempt in range(2):
            kwargs: dict = {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0.0,
                # Extraction is reading, not reasoning: thinking would share the budget.
                "disable_thinking": True,
            }
            if native is not None:
                kwargs["structured_output"] = native
            async with gate:
                spend = current_tool_model_spend()
                release = spend.reserve(estimated_input_tokens, max_tokens) if spend is not None else None
                if spend is not None and release is None:
                    usage.refused_calls += 1
                    return None
                try:
                    async with traced_generation(
                        name=f"stix_extraction.{step}", model=model, messages=messages
                    ) as generation:
                        generation.response = await _chat(llm, kwargs)
                except ModelDeclinedError as exc:  # the model's refusal: asking again changes nothing
                    logger.warning("stix extraction %s call declined: %s", step, exc)
                    usage.failed_calls += 1
                    usage.declined_calls += 1
                    _spent(exc, step)
                    return None
                except Exception as exc:  # a provider error fails this call, not the run
                    logger.warning("stix extraction %s call failed (attempt %d): %s", step, attempt + 1, exc)
                    usage.failed_calls += 1
                    _spent(exc, step)
                    continue
                else:
                    response = generation.response
                    reported = getattr(response, "usage", None)
                    usage.add(step, reported)
                    if spend is not None and isinstance(reported, dict):
                        # The model that served this call: with calls in flight, a
                        # failover's model must price this call's tokens, not another's.
                        spend.record(reported, getattr(response, "model", None) or None)
                finally:
                    if release is not None:
                        release()
            if answer_was_cut(response):
                usage.failed_calls += 1
                logger.warning("stix extraction %s answer cut at its token budget", step)
                return None
            parsed, errors = validate_and_parse(getattr(response, "content", "") or "", schema)
            conformed = conform(parsed, schema)
            if isinstance(conformed, dict):
                if errors:
                    logger.info("stix extraction %s answer off its schema, kept what fits: %s", step, "; ".join(errors)[:300])
                    if not any(conformed.get(key) for key in (schema.get("properties") or {})):
                        usage.off_schema_calls += 1
                return conformed
            usage.failed_calls += 1
            logger.warning("stix extraction %s answer unreadable: %s", step, "; ".join(errors)[:300])
        return None

    return call


_MISMATCH = object()
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "null": (type(None),),
}


def _fits(value: object, kind: str) -> bool:
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES.get(kind, (object,)))


def _conform(value: object, schema: dict) -> object:
    declared = schema.get("type")
    kinds = declared if isinstance(declared, list) else [declared] if declared else []
    if not kinds and "properties" in schema:
        kinds = ["object"]
    if not kinds:
        return value
    if "object" in kinds and isinstance(value, dict):
        properties = schema.get("properties") or {}
        out = {}
        for key, item in value.items():
            kept = _conform(item, properties[key]) if key in properties else item
            if kept is not _MISMATCH:
                out[key] = kept
        return out
    if "array" in kinds and isinstance(value, list):
        items = schema.get("items") or {}
        return [kept for kept in (_conform(item, items) for item in value) if kept is not _MISMATCH]
    if any(_fits(value, kind) for kind in kinds if kind not in ("object", "array")):
        return _in_enum(value, schema.get("enum"))
    return _MISMATCH


def _in_enum(value: object, enum: object) -> object:
    """*value* when *schema* lists no ``enum`` or lists it; the listed value a string
    differing only in case stands for (``Malware`` for ``malware``); else a mismatch,
    which a provider without native structured output may answer (``ransomware`` for a
    kind)."""
    if not isinstance(enum, list) or value in enum:
        return value
    if isinstance(value, str):
        same = [e for e in enum if isinstance(e, str) and e.casefold() == value.strip().casefold()]
        if len(same) == 1:
            return same[0]
    return _MISMATCH


def conform(value: object, schema: dict) -> object | None:
    """*value* without every part whose JSON type or ``enum`` *schema* contradicts (an
    alias list answered as a string, a mention answered as a bare string, an entry that
    is no object, a kind outside the ontology), so the pipeline reads only values of the type it expects; a property the
    schema requires and the answer left out stays left out, read as absent. ``None``
    when the root itself has the wrong type."""
    kept = _conform(value, schema)
    return None if kept is _MISMATCH else kept


async def _chat(llm: object, kwargs: dict) -> object:
    chat: Callable[..., Awaitable[object]] = llm.chat  # type: ignore[attr-defined]
    try:
        return await chat(**kwargs)
    except TypeError as exc:
        # A provider without **kwargs may refuse an optional hint by name.
        hints = ("structured_output", "disable_thinking")
        if not any(hint in kwargs and hint in str(exc) for hint in hints):
            raise
        for hint in hints:
            kwargs.pop(hint, None)
        return await chat(**kwargs)


__all__ = ["StructuredCall", "Usage", "conform", "provider_structured_call"]
