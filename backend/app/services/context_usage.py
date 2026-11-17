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

"""How full a chat turn's context window is, as the chat composers show it.

The agent loop hangs ``context_tokens`` / ``context_window`` /
``context_breakdown`` / ``context_level`` on its per-iteration ``thinking``
event (see ``agent_loop.run_agent_loop``) rather than on a status of its own,
so the figures ride an event every consumer already understands. Both chats
that show the gauge - the platform proxy behind the chat embedded in OpenCTI /
OpenAEV and the native web chat - read them here, keep the last reading of a
run on the assistant message it wrote and serve it back when the conversation
is reopened.

The level is the loop's own verdict, never re-derived from the figures:
``context_tokens`` counts the tool schemas, which the loop's compaction gate
does not measure, and the gate's threshold depends on the run's session budget,
which no reader of the figures knows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

#: The conversation is well below the point where the loop condenses it.
CONTEXT_LEVEL_NORMAL = "normal"
#: The conversation is close to (or at) the point where the loop summarizes
#: older turns to make room.
CONTEXT_LEVEL_APPROACHING = "approaching"
#: The loop had to cut older turns of this run to fit the window.
CONTEXT_LEVEL_OVERFLOWED = "overflowed"
#: Mirrored by ``CONTEXT_LEVELS`` in ``frontend/src/lib/context-usage.ts``.
CONTEXT_LEVELS: tuple[str, ...] = (
    CONTEXT_LEVEL_NORMAL,
    CONTEXT_LEVEL_APPROACHING,
    CONTEXT_LEVEL_OVERFLOWED,
)

#: The message metadata keys a reading is stored under - the same names as on
#: the ``thinking`` event and the ``done`` frame.
CONTEXT_USAGE_META_KEYS: tuple[str, ...] = (
    "context_tokens",
    "context_window",
    "context_breakdown",
    "context_level",
)

#: Keys a reading also stores on its message for the chat's ``/context``
#: command (#6590), and for it alone: never served on a message, never restored
#: to a module's embedded chat, never published on a frame. ``context_detail``
#: is what each bucket is made of (``agent_loop._context_detail``); the others
#: are the model that answered the reading's call and the prompt and cached
#: token counts its provider reported for it.
CONTEXT_COMMAND_META_KEYS: tuple[str, ...] = (
    "context_detail",
    "context_model",
    "context_provider_tokens",
    "context_cached_tokens",
)


def split_tokens(total: int, weights: Mapping[Any, int]) -> dict[Any, int]:
    """*total* tokens shared across *weights* by size, summing exactly to *total*.

    The readout's one rounding rule: floors, the leftover on the largest share,
    because lines that do not add up to their headline read as broken numbers.
    Zero shares are dropped.
    """
    weight = sum(v for v in weights.values() if v > 0)
    if total <= 0 or weight <= 0:
        return {}
    out = {k: total * v // weight for k, v in weights.items() if v > 0}
    leftover = total - sum(out.values())
    if leftover:
        out[max(out, key=lambda k: weights[k])] += leftover
    return {k: v for k, v in out.items() if v > 0}


class ContextUsage(NamedTuple):
    """One reading: the breakdown's buckets sum to ``tokens``."""

    tokens: int
    window: int
    breakdown: dict[str, int] | None = None
    level: str | None = None


def read_context_usage(source: Mapping[str, Any]) -> ContextUsage | None:
    """The reading carried by ``source`` (an event or a message's metadata).

    Returns a reading only when ``tokens`` and ``window`` are usable: a token
    count without the window it is measured against is not an occupancy, a
    non-positive window would make one out of a division by zero, and a
    negative count is nonsense. ``bool`` is rejected explicitly - it is an
    ``int`` subclass in Python, and ``True`` is not a token count.

    The breakdown and the level are optional and independently validated: they
    only enrich the readout, so a malformed one degrades to ``None`` rather
    than costing the gauge its number.
    """
    tokens = source.get("context_tokens")
    window = source.get("context_window")
    if isinstance(tokens, bool) or isinstance(window, bool):
        return None
    if not isinstance(tokens, int) or not isinstance(window, int):
        return None
    if tokens < 0 or window <= 0:
        return None

    raw_breakdown = source.get("context_breakdown")
    breakdown: dict[str, int] | None = None
    if isinstance(raw_breakdown, dict):
        cleaned = {
            k: v
            for k, v in raw_breakdown.items()
            if isinstance(k, str)
            and isinstance(v, int)
            and not isinstance(v, bool)
            and v > 0
        }
        # All-or-nothing, deliberately. The readout shows the per-bucket lines
        # AND the headline together, so a breakdown whose parts no longer add up
        # reads as broken numbers - worse than showing no breakdown at all.
        # Salvaging the valid half of a malformed payload produces exactly that.
        # The only legitimate producer (``agent_loop._context_breakdown_tokens``)
        # always sends buckets summing to ``context_tokens``, so anything that
        # needed cleaning, or that does not add up, is foreign by definition.
        if cleaned and len(cleaned) == len(raw_breakdown) and sum(cleaned.values()) == tokens:
            breakdown = cleaned

    level = source.get("context_level")
    return ContextUsage(
        tokens, window, breakdown, level if level in CONTEXT_LEVELS else None
    )


def context_usage_from_event(event: Mapping[str, Any]) -> ContextUsage | None:
    """The reading of an agent-loop event: only the iteration tick carries one."""
    if event.get("status") != "thinking":
        return None
    return read_context_usage(event)


def context_usage_meta(usage: ContextUsage | None) -> dict[str, Any]:
    """``usage`` under :data:`CONTEXT_USAGE_META_KEYS`, ``{}`` for no reading."""
    if usage is None:
        return {}
    meta: dict[str, Any] = {"context_tokens": usage.tokens, "context_window": usage.window}
    if usage.breakdown:
        meta["context_breakdown"] = dict(usage.breakdown)
    if usage.level:
        meta["context_level"] = usage.level
    return meta


class ProviderUsage(NamedTuple):
    """What the provider billed for one model call's prompt."""

    model: str | None
    prompt_tokens: int
    cached_tokens: int


def provider_prompt_usage(event: Mapping[str, Any]) -> ProviderUsage | None:
    """The prompt usage an ``llm_step`` event reports, or ``None``.

    Read through the tracing normalization (``usage_detail_buckets``), the one
    rule for every provider's usage shape: the prompt is the input plus its
    detail buckets (an OpenAI or Gemini cached part rides there), Anthropic's
    cache reads and writes included; the cached part is the cache read. A step
    flagged ``empty_response_retry`` sums several calls, so it is no reading of
    one prompt.
    """
    if event.get("status") != "llm_step" or event.get("empty_response_retry"):
        return None
    usage = event.get("llm_usage")
    if not isinstance(usage, dict) or not usage:
        return None
    from app.services.langfuse_tracing import usage_detail_buckets

    buckets = usage_detail_buckets(usage)
    prompt = buckets.get("input", 0) + sum(
        v for k, v in buckets.items() if k.startswith("input_")
    )
    if prompt <= 0:
        return None
    cached = buckets.get("cache_read_input_tokens", 0) + buckets.get("input_cached_tokens", 0)
    model = event.get("llm_model")
    return ProviderUsage(
        model if isinstance(model, str) and model else None,
        prompt,
        min(cached, prompt),
    )


class ContextUsageTracker:
    """The last reading a run published.

    It measures the RUN, not a message: a steering segment of the same run keeps
    it (its text was written from that context), and only the end of the run
    clears it - so a message no loop wrote never carries a stale reading.

    A reading is taken before its model call, so the provider's count for that
    call (its ``llm_step``) completes it; a new reading drops the previous
    call's count, which measured another prompt.
    """

    __slots__ = ("detail", "provider", "reading")

    def __init__(self) -> None:
        self.reading: ContextUsage | None = None
        self.detail: dict[str, Any] | None = None
        self.provider: ProviderUsage | None = None

    def observe(self, event: Mapping[str, Any]) -> None:
        usage = context_usage_from_event(event)
        if usage is not None:
            self.reading = usage
            detail = event.get("context_detail")
            self.detail = detail if isinstance(detail, dict) else None
            self.provider = None
            return
        if self.reading is not None:
            provider = provider_prompt_usage(event)
            if provider is not None:
                self.provider = provider

    def meta(self) -> dict[str, Any]:
        """The reading as the gauge reads it: stored, and sent on frames."""
        return context_usage_meta(self.reading)

    def command_meta(self) -> dict[str, Any]:
        """The reading's :data:`CONTEXT_COMMAND_META_KEYS`, for the message only.

        Added where the reply is STORED, never to a frame: a frame reaches
        every viewer of the conversation, the command only those who may send
        in it.
        """
        if self.reading is None:
            return {}
        meta: dict[str, Any] = {}
        if self.detail:
            meta["context_detail"] = self.detail
        if self.provider is not None:
            if self.provider.model:
                meta["context_model"] = self.provider.model
            meta["context_provider_tokens"] = self.provider.prompt_tokens
            meta["context_cached_tokens"] = self.provider.cached_tokens
        return meta

    def clear(self) -> None:
        self.reading = None
        self.detail = None
        self.provider = None
