# SPDX-License-Identifier: Apache-2.0

"""Anthropic (Claude) LLM provider with full tool-calling support."""

from __future__ import annotations

import base64
import json as _json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack

import anthropic

from app.core.config import settings
from app.llm.anthropic_thinking_replay import (
    REASON_CONTEXT_BUDGET,
    StreamedBlocks,
    ThinkingReplay,
    block_chars,
    blocks_from_content,
    plain_assistant_content,
    structured_error_message,
)
from app.llm.base import (
    IMAGE_UNAVAILABLE_TEXT,
    TOOL_RESULT_IMAGE_DATA_NOTE,
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    StreamUsageSink,
    declined_error,
    parse_tool_arguments,
    prepare_base64_image_for_llm_async,
    prepare_image_bytes_for_llm_async,
    reads_reasoning,
    retry_llm_call,
    tool_results_in_call_order,
    usage_sink_of,
)
from app.llm.file_resolver import (
    FallbackText,
    NativeFile,
    PayloadFilePlan,
    prepare_tool_result_attachments,
    resolve_file,
    resolve_payload_files,
)
from app.llm.model_capabilities import (
    between_tools_thinking_off,
    capped_effort,
    defaults_to_adaptive_thinking,
    omit_temperature,
    requires_summarized_thinking_opt_in,
    supported_effort,
    supports_in_place_system_messages,
    thinking_always_on,
    thinking_off_max_effort,
)
from app.llm.model_limits import (
    reported_context_window,
    resolve_provider_max_output_tokens,
)
from app.llm.system_prompt_split import split_system_prompt
from app.llm.tool_markup_recovery import recover_leaked_tool_calls
from app.tools.base import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)


class _StreamUsageAccumulator:
    """Accumulate Anthropic streaming token usage across stream events.

    Anthropic's streaming API splits usage across two events:

    * ``message_start`` carries the prompt-side counts — ``input_tokens``
      plus the cache breakdown (``cache_creation_input_tokens`` /
      ``cache_read_input_tokens``) and an initial (small) ``output_tokens``.
    * each ``message_delta`` carries the running ``output_tokens`` total,
      with the final delta holding the complete output count.

    Reading only one event yields partial numbers, so we fold both into a
    single dict shaped like the non-streaming ``chat()`` usage (the keys
    ``app.services.langfuse_tracing._normalize_usage`` already understands)
    so cost tracking works identically on the streaming and non-streaming
    paths.  All field access is defensive so a missing attribute (or a
    test stub that emits no usage) degrades to zero rather than raising.

    Built with the call's :class:`StreamUsageSink`, it reports ``to_dict()``
    after every event it absorbs, so a stream that never ends still leaves
    what ``message_start`` and the deltas so far reported.
    """

    __slots__ = (
        "_sink",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "input_tokens",
        "output_tokens",
    )

    def __init__(self, sink: StreamUsageSink | None = None) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self._sink = sink

    def absorb_message_start(self, event: object) -> None:
        message = getattr(event, "message", None)
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        self.input_tokens = getattr(usage, "input_tokens", 0) or 0
        self.cache_creation_input_tokens = (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        self.cache_read_input_tokens = (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        # message_start may carry a small initial output token count.
        self.output_tokens = getattr(usage, "output_tokens", 0) or 0
        self._report()

    def absorb_message_delta(self, event: object) -> None:
        usage = getattr(event, "usage", None)
        if usage is None:
            return
        # message_delta usage is cumulative — the last one wins.
        out = getattr(usage, "output_tokens", None)
        if out is not None:
            self.output_tokens = out or 0
        self._report()

    def _report(self) -> None:
        if self._sink is not None:
            self._sink.report(self.to_dict())

    def to_dict(self) -> dict:
        """Return the usage dict, or ``{}`` when nothing was captured.

        An empty dict (rather than zero-valued keys) preserves the prior
        behaviour for code paths and stubs that never emit usage events,
        so downstream consumers can keep treating "no usage" as falsy.
        """
        if not (
            self.input_tokens
            or self.output_tokens
            or self.cache_creation_input_tokens
            or self.cache_read_input_tokens
        ):
            return {}
        usage: dict = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.cache_creation_input_tokens:
            usage["cache_creation_input_tokens"] = self.cache_creation_input_tokens
        if self.cache_read_input_tokens:
            usage["cache_read_input_tokens"] = self.cache_read_input_tokens
        return usage


def build_cache_control(prompt_cache: bool | None = None) -> dict | None:
    """Return the ``cache_control`` marker to use, or ``None`` when disabled.

    Caching is on by default (``PROMPT_CACHING_ENABLED``); callers can force
    it off for a single request via the ``prompt_cache=False`` kwarg.  The
    global setting is authoritative: ``prompt_cache`` is opt-out only, so a
    ``True`` value can never re-enable caching once the platform kill switch
    is off.  The ``ttl`` field is only included for the non-default 1-hour
    duration so the request body stays minimal in the common case.
    """
    if not settings.prompt_caching_enabled or prompt_cache is False:
        return None
    cache_control: dict = {"type": "ephemeral"}
    if settings.prompt_cache_ttl == "1h":
        cache_control["ttl"] = "1h"
    return cache_control


def _apply_prompt_caching(params: dict, prompt_cache: bool | None = None) -> None:
    """Apply the platform prompt-caching strategy to an outbound request.

    Best-practice layout per Anthropic's prompt-caching guide (cache
    hierarchy is ``tools`` → ``system`` → ``messages``); uses 3 of the 4
    available breakpoint slots:

    1. **Explicit breakpoint on the last tool definition** — tool schemas are
       the most stable prefix segment (identical across every iteration of an
       agentic run, and across runs of the same agent), so they cache
       independently of the more volatile system prompt.
    2. **Explicit breakpoint on the stable system prefix** — the merged
       system string is split at its volatile tail (the clock line and what
       follows it: local time, memory, recent activity;
       :func:`~app.llm.system_prompt_split.split_system_prompt`). The stable
       prefix becomes a text block carrying ``cache_control``, so the next run
       of the same agent reads tools + persona + skills + policies from the
       cache; the volatile tail is a second, unmarked text block after it. A
       prompt without a clock line stays one marked block.
    3. **Automatic conversation caching** — a top-level ``cache_control``
       (sent via ``extra_body`` so it works regardless of SDK signature
       support) lets the API move the final breakpoint forward to the last
       cacheable block as the conversation grows.  This is Anthropic's
       recommended default for multi-turn conversations and exactly matches
       the agentic loop's monotonically growing message list.

    No explicit ``cache_control`` is ever placed on message blocks, and all
    markers share one TTL, so the documented automatic-caching edge cases
    (conflicting last-block TTL, exhausted breakpoint slots) cannot trigger.
    Requests below the per-model minimum cacheable length are processed
    uncached by the API without error.
    """
    cache_control = build_cache_control(prompt_cache)
    if cache_control is None:
        return

    tools = params.get("tools")
    if tools:
        tools[-1]["cache_control"] = cache_control

    system = params.get("system")
    if isinstance(system, str) and system:
        stable, volatile = split_system_prompt(system)
        blocks: list[dict] = [
            {"type": "text", "text": stable, "cache_control": cache_control}
        ]
        if volatile.strip():
            blocks.append({"type": "text", "text": volatile})
        else:
            blocks[0]["text"] = system
        params["system"] = blocks

    extra_body = dict(params.get("extra_body") or {})
    extra_body["cache_control"] = cache_control
    params["extra_body"] = extra_body


def _usage_from_response(usage: object) -> dict:
    """Build the standard usage dict from a non-streaming Anthropic response.

    Includes the cache breakdown (``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``) when present so cost tracking prices cached
    tokens at their discounted/premium rates on the non-streaming path,
    matching what ``_StreamUsageAccumulator`` already does for streaming.
    """
    result: dict = {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    if cache_creation:
        result["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    return result


# The stop reason of an answer the model's safety classifiers declined.
_DECLINED_STOP_REASON = "refusal"


# JSON Schema combinators the Anthropic Messages API rejects when they appear
# at the TOP LEVEL of a tool ``input_schema`` (HTTP 400: "input_schema does not
# support oneOf, allOf, or anyOf at the top level"). Nested occurrences (inside
# a property, an ``if``/``then`` branch, ...) are accepted and must be kept.
_TOP_LEVEL_UNSUPPORTED_SCHEMA_KEYS = ("allOf", "anyOf", "oneOf")


def _sanitize_input_schema(schema: dict) -> dict:
    """Drop top-level ``allOf``/``anyOf``/``oneOf`` from a tool input schema.

    Some tool schemas legitimately carry top-level combinators to encode
    conditional requirements (e.g. the OpenCRQ either-or selectors and
    per-action ``allOf``/``if``/``then`` guards), and MCP servers can return
    arbitrary JSON Schema. Anthropic rejects those keys at the top level, and
    one bad tool 400s the ENTIRE request — including every failover retry on
    another Anthropic model. Stripping them from the wire copy is safe: the
    pre-dispatch ``jsonschema`` validation in the agent loop still validates
    tool arguments against the full ``ToolDefinition.input_schema``, so the
    conditional constraints keep being enforced.

    Returns the original dict unchanged (no copy) when nothing needs
    stripping; otherwise returns a shallow copy so the shared
    ``ToolDefinition.input_schema`` is never mutated.
    """
    if not isinstance(schema, dict):
        return schema
    if not any(key in schema for key in _TOP_LEVEL_UNSUPPORTED_SCHEMA_KEYS):
        return schema
    return {
        key: value
        for key, value in schema.items()
        if key not in _TOP_LEVEL_UNSUPPORTED_SCHEMA_KEYS
    }


def _wire_tools(tools: list[ToolDefinition] | None) -> list[dict] | None:
    """The tool definitions as sent: deduplicated by name (last definition
    wins), schemas sanitized. ``None`` when there are none."""
    if not tools:
        return None
    seen: dict[str, dict] = {}
    for t in tools:
        seen[t.name] = {
            "name": t.name,
            "description": t.description,
            "input_schema": _sanitize_input_schema(t.input_schema),
        }
    return list(seen.values())


def _is_retryable_anthropic(exc: Exception) -> bool:
    """Return True for transient Anthropic errors worth retrying.

    Whether a retry is SAFE is the caller's: ``chat_stream_with_tools`` only
    retries while nothing reached its consumer, ``chat_stream`` only while
    opening the stream.
    """
    # 500 Internal Server Error
    if isinstance(exc, anthropic.InternalServerError):
        return True
    # 529 Overloaded (the SDK's own class for it is not an InternalServerError),
    # and an ``overloaded_error`` event mid-stream: the SDK raises it with the
    # stream's 200 response, so only the body's ``error.type`` names it.
    if isinstance(exc, anthropic.APIStatusError) and (
        exc.status_code == 529 or exc.type == "overloaded_error"
    ):
        return True
    # 429 Rate Limit
    if isinstance(exc, anthropic.RateLimitError):
        return True
    # Network / connection failures
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    # 502/503 via generic APIStatusError
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in (502, 503)


def _should_omit_temperature(model: str) -> bool:
    """Return whether the static model rules omit ``temperature``."""
    omit = omit_temperature(model)
    if omit:
        logger.debug(
            "Omitting unsupported temperature parameter for model '%s'",
            model,
        )
    return omit


def _defaults_to_adaptive_thinking(model: str) -> bool:
    """Return True for models that run *adaptive thinking on by default*.

    Claude Sonnet 5, Opus 4.7/4.8, Opus 5, Fable 5 and Mythos enable adaptive
    thinking automatically, and thinking shares the ``max_tokens`` budget with
    the response text (Anthropic "What's new in Claude Sonnet 5"). The shared
    capability rule covers both this behavior and non-default sampling
    parameters. Callers use it to decide whether it is worth sending
    ``thinking: {"type": "disabled"}`` — older models leave thinking off by
    default, and pre-4.x models would reject the parameter outright.
    """
    return defaults_to_adaptive_thinking(model)


# ---------------------------------------------------------------------------
# Turning thinking off on a model that refuses ``thinking.type.disabled``
# ---------------------------------------------------------------------------

# Lowest ``output_config.effort`` level the pinned SDK accepts
# (``anthropic.types.OutputConfigParam.effort``: low < medium < high < xhigh
# < max). On a model that refuses ``thinking.type.disabled`` this is what "as
# little thinking as possible" means.
_LOWEST_EFFORT = "low"

# Room added to ``max_tokens`` on the adaptive thinking-off form.
# Thinking cannot be switched off there and ``max_tokens`` caps thinking and
# answer together (Anthropic, "Adaptive thinking" > "Cost control"), so a
# budget the caller sized for its answer alone would be shared with whatever
# the model still thinks at the lowest effort. A cap, not a target: output is
# billed per generated token, and ``max_tokens`` does not count towards rate
# limits.
_ADAPTIVE_FORM_HEADROOM = 4096

# The parameter path the API names in its refusal. The quoting around it
# varies, so the match is on the bare dotted path, case-insensitively, in the
# API's structured error message only (``structured_error_message``).
_DISABLED_THINKING_REFUSAL = "thinking.type.disabled"

# Model ids observed to refuse ``thinking: {"type": "disabled"}`` in THIS
# process. The set of such models is not knowable up front (the 5.x family
# keeps growing and a prefix table would be stale on release day), so it is
# learned from the exact 400 in :func:`_refuses_disabled_thinking` and read by
# :func:`_apply_thinking_off` on every later call. Module-level and
# process-wide on purpose: it resets on process restart, which costs one
# wasted round trip per process per model - accepted. A model the capability
# rules already declare ``thinking_always_on`` or ``between_tools_thinking_off``
# never pays it.
_MODELS_REFUSING_DISABLED_THINKING: set[str] = set()

# A model whose lowest setting is ``between_tools`` (Sonnet 5.5) refuses
# ``disabled`` without naming the parameter path: 'To turn thinking off on this
# model, send "thinking": {"type": "between_tools"} instead of {"type":
# "disabled"}.' Such a model is learned like the set above, until a rule
# declares ``between_tools_thinking_off`` for it.
_BETWEEN_TOOLS_REFUSAL = "between_tools"
_MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS: set[str] = set()

# The ``between_tools`` form takes no effort above ``high`` on Sonnet 5.5, the
# model documented with it; a learned model is held to the same ceiling so the
# retry is not refused for its effort.
_BETWEEN_TOOLS_MAX_EFFORT = "high"


def _asks_for_between_tools(exc: Exception) -> bool:
    """Return True for the 400 that asks for ``between_tools`` instead of ``disabled``.

    Anchored like :func:`_refuses_disabled_thinking`: a 400, and both forms
    named in the API's structured error message.
    """
    if not isinstance(exc, anthropic.APIStatusError) or exc.status_code != 400:
        return False
    message = structured_error_message(exc).lower()
    return _BETWEEN_TOOLS_REFUSAL in message and "disabled" in message


def _refuses_disabled_thinking(exc: Exception) -> bool:
    """Return True for the exact 400 that rejects ``thinking.type.disabled``.

    Anchored on the SDK's status error (``BadRequestError`` is its 400
    subclass) AND on the parameter path the API names in its structured error
    message - ``'"thinking.type.disabled" is not supported for this model. Use
    "thinking.type.adaptive" and "output_config.effort" to control thinking
    behavior.'`` - so a different 400 (a rejected tool schema, an unknown
    model) or a 429 / 5xx is never mistaken for it. Those keep their normal
    handling: the transient ones go through ``retry_llm_call``, the rest
    through the router's failover chain. Never the SDK's rendering of the
    exception: it folds in the whole body, which a proxy can fill with an
    echo of the request - and a match here is remembered process-wide.
    """
    if not isinstance(exc, anthropic.APIStatusError) or exc.status_code != 400:
        return False
    return _DISABLED_THINKING_REFUSAL in structured_error_message(exc).lower()


def _apply_thinking_off(
    params: dict, model: str, *, max_output_tokens: int | None = None
) -> None:
    """Turn thinking off in *params*, as far as *model* is known to allow.

    ``thinking: {"type": "disabled"}`` by default - thinking off shares no
    ``max_tokens`` budget with the answer, which is the whole reason the
    ``disable_thinking`` hint exists. A model the capability rules declare
    ``between_tools_thinking_off`` (Sonnet 5.5), or remembered in
    :data:`_MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS`, gets
    ``thinking: {"type": "between_tools"}``, its documented lowest setting:
    no up-front thinking either. A model remembered in
    :data:`_MODELS_REFUSING_DISABLED_THINKING`, or declared
    ``thinking_always_on`` (Opus 5.5, Fable 5 / 5.1, Mythos), gets the
    equivalent the API documents for it instead:
    ``thinking: {"type": "adaptive"}`` plus ``output_config.effort`` at its
    lowest level. That thinking still shares ``max_tokens`` with the answer,
    so the adaptive form also raises ``max_tokens`` by
    :data:`_ADAPTIVE_FORM_HEADROOM`, up to the model's *max_output_tokens*
    when known (a larger ``max_tokens`` is an HTTP 400) and never below the
    caller's own budget.

    ``disable_thinking`` governs thinking, and only thinking. On the
    ``disabled`` and ``between_tools`` forms it adds no ``effort`` of its own
    and keeps a caller-provided ``reasoning_effort``: effort applies to every
    output token and is valid with thinking off (Anthropic, "How effort
    works"), where it bounds the text rather than any thinking. Only a level
    above the model's ``thinking_off_max_effort`` is lowered to it, since the
    form is refused there (Opus 5 and Sonnet 5.5 above ``high``). On the
    adaptive form ``effort`` IS the thinking lever, so it is written over any
    caller value: the caller asked for no thinking at all, and the lowest
    effort is the closest that model gets.
    """
    if model in _MODELS_REFUSING_DISABLED_THINKING or thinking_always_on(model):
        params["thinking"] = {"type": "adaptive"}
        params.setdefault("output_config", {})["effort"] = _LOWEST_EFFORT
        budget = params.get("max_tokens")
        if isinstance(budget, int):
            raised = budget + _ADAPTIVE_FORM_HEADROOM
            if max_output_tokens is not None:
                raised = min(raised, max(budget, max_output_tokens))
            params["max_tokens"] = raised
        return
    learned_between_tools = model in _MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS
    off = (
        "between_tools"
        if learned_between_tools or between_tools_thinking_off(model)
        else "disabled"
    )
    params["thinking"] = {"type": off}
    output_config = params.get("output_config")
    if output_config and output_config.get("effort"):
        ceiling = thinking_off_max_effort(model)
        if ceiling is None and learned_between_tools:
            ceiling = _BETWEEN_TOOLS_MAX_EFFORT
        output_config["effort"] = capped_effort(output_config["effort"], ceiling)


def _fits_in_place(
    messages: list[ChatMessage], index: int, conversation: list[dict]
) -> bool:
    """Whether the system message at *index* may be sent where it sits.

    The API's rule: an in-place ``system`` message must follow a ``user``
    message (a tool-result turn is one) or another ``system`` message, and
    precede an ``assistant`` message or end the array. The next message is
    read from the transcript, skipping system messages: a system message
    before a tool or user message (inside a batch of tool results, or before
    a human turn) is merged into the top-level prompt as before.
    """
    if not messages[index].content or not conversation:
        return False
    if conversation[-1]["role"] not in ("user", "system"):
        return False
    for later in messages[index + 1 :]:
        if later.role != "system":
            return later.role == "assistant"
    return True


def _refuses_in_place_system(exc: Exception) -> bool:
    """Return True for a 400 that refuses a ``system`` message in ``messages``.

    Read off the API's structured error message only, like the other
    refusals: "role 'system' is not supported on this model" and the
    placement refusals ("role 'system' must follow ...") both name the role.
    """
    if not isinstance(exc, anthropic.APIStatusError) or exc.status_code != 400:
        return False
    return "role 'system'" in structured_error_message(exc)


def _fold_in_place_system(params: dict) -> dict | None:
    """*params* with every in-place system message folded into ``system``.

    The rendering a model without in-place system messages gets: each one
    appended to the top-level prompt in transcript order. ``None`` when the
    request carries none.
    """
    messages = params.get("messages") or []
    folded = [m["content"] for m in messages if m.get("role") == "system"]
    if not folded:
        return None
    extra = "\n\n".join(folded)
    system = params.get("system")
    if isinstance(system, list) and system:
        tail = dict(system[-1])
        text = tail.get("text") or ""
        tail["text"] = f"{text}\n\n{extra}" if text else extra
        new_system: str | list[dict] = [*system[:-1], tail]
    else:
        new_system = f"{system}\n\n{extra}" if system else extra
    return {
        **params,
        "system": new_system,
        "messages": [m for m in messages if m.get("role") != "system"],
    }


class _RefusalRecovery:
    """The request to send again after a 400 refusing part of it, or ``None``.

    Two parts of a request can be refused, and each is taken back once: the
    replayed thinking blocks (``replay.retry_without``) and the in-place system
    messages (folded into ``system``). A model refusing both answers the first
    retry with the other refusal, so each recovery stays available after the
    other one ran. The replay's fallback is the conversation as built, which
    carries the in-place system messages again: once they were folded, they are
    folded again from the original ``system``, never onto the folded one.
    """

    def __init__(self, params: dict, replay: ThinkingReplay) -> None:
        self._replay = replay
        self._has_system = "system" in params
        self._system = params.get("system")
        self._reverted = False
        self._folded = False

    def _fold_from_original(self, params: dict) -> dict:
        base = {k: v for k, v in params.items() if k != "system"}
        if self._has_system:
            base["system"] = self._system
        return _fold_in_place_system(base) or params

    def next_params(self, exc: Exception, params: dict) -> dict | None:
        if not self._reverted and self._replay.refused_by(exc):
            self._reverted = True
            retry = {**params, "messages": self._replay.retry_without(exc)}
            return self._fold_from_original(retry) if self._folded else retry
        if not self._folded and _refuses_in_place_system(exc):
            folded = _fold_in_place_system(params)
            if folded is not None:
                self._folded = True
                return folded
        return None


def _sends_disabled_thinking(params: dict) -> bool:
    """Return True when *params* carry ``thinking: {"type": "disabled"}``."""
    thinking = params.get("thinking")
    return isinstance(thinking, dict) and thinking.get("type") == "disabled"


# What a streaming call with a reasoning consumer sends to a model that thinks
# by default but omits the text. ``adaptive`` is that model's default
# mode, so the request differs from the default only by returning the text.
_SUMMARIZED_THINKING = {"type": "adaptive", "display": "summarized"}


def _apply_summarized_thinking(params: dict, model: str, on_thinking: object) -> None:
    """Ask for the thinking text when a caller streams it and *model* hides it.

    Claude Sonnet 5, Opus 5 / 5.5, Fable 5 / 5.1 and Mythos default
    ``thinking.display`` to ``"omitted"``: their thinking blocks stream no
    ``thinking_delta`` at all, so ``on_thinking`` is never called and the chat
    reasoning window stays empty. Only a call whose ``on_thinking`` reads the
    text opts in - a caller that reads no reasoning, the agent loop's
    liveness-only sink included, keeps the faster omitted default - and a
    ``thinking`` config the call already carries is left alone.
    """
    if not reads_reasoning(on_thinking) or "thinking" in params:
        return
    if requires_summarized_thinking_opt_in(model):
        params["thinking"] = dict(_SUMMARIZED_THINKING)


class _ThinkingBlockJoiner:
    """Open every thinking block after a response's first on a new paragraph.

    One response can carry several thinking blocks (Fable 5.x writes a progress
    update as a block of its own before a tool call); streamed back to back,
    their deltas would run together mid-sentence in the reasoning pane.
    """

    __slots__ = ("_block_has_text", "_response_has_text")

    def __init__(self) -> None:
        self._block_has_text = False
        self._response_has_text = False

    def start_block(self) -> None:
        self._block_has_text = False

    def delta(self, text: str) -> str:
        if self._response_has_text and not self._block_has_text:
            text = f"\n\n{text}"
        self._block_has_text = True
        self._response_has_text = True
        return text


# Anthropic models that support the ``output_config.format`` (structured
# outputs) parameter.  Per Anthropic's GA list this covers Sonnet 4.5+,
# Opus 4.5+, Haiku 4.5+ and the 5th-generation models (Fable 5, Sonnet 5,
# Opus 5).  Older Sonnet 4 / Opus 4 / Opus 4.1 and all Claude 3.x models
# reject the parameter with HTTP 400 ("'<model>' does not support output
# format.").
#
# Keep this as a tuple of model-name **prefixes** so dated aliases
# (e.g. ``claude-sonnet-4-5-20250929``) and regional aliases match
# without separate entries. Omitting a supported model here is not a hard
# error but silently downgrades it to prompt-only JSON, which newer models
# follow far less reliably — that is exactly what made ``claude-sonnet-5``
# emit prose for the memory-reconciliation and conversation-compaction
# calls (non-JSON parse failures) until it was added here.
# Ref: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
_STRUCTURED_OUTPUT_SUPPORTED_PREFIXES = (
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-5",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-haiku-4-5",
)


def _supports_structured_output(model: str) -> bool:
    """Return True if *model* accepts ``output_config.format``.

    Anthropic added native structured outputs as a GA feature on
    Sonnet 4.5+, Opus 4.5+, Haiku 4.5+ and the 5th-generation models
    (Fable 5, Sonnet 5, Opus 5).  Sending the parameter
    to an older model (e.g. ``claude-sonnet-4-20250514``) triggers a
    ``400 invalid_request_error``: ``"'<model>' does not support output
    format."``.  The schema is still enforced via the prompt-based
    Schema-Aligned Parsing pipeline in
    :mod:`app.services.structured_output`, so silently dropping the
    native parameter on incompatible models is safe.
    """
    return any(model.startswith(prefix) for prefix in _STRUCTURED_OUTPUT_SUPPORTED_PREFIXES)


def _normalize_temperature(model: str, temperature: float) -> float:
    """Sanitise an Anthropic temperature value before sending it to the API.

    Two behaviours, applied in order:

    1. **Non-finite inputs** (``NaN`` / ``+inf`` / ``-inf``) are replaced
       with Anthropic's documented default of ``0.7`` and a WARNING is
       logged. Doing this before clamping keeps the control flow obvious
       — Python's ``min``/``max`` aren't IEEE-754 compliant on ``NaN`` and
       can leak it through, so the early-return is also a safety net.
    2. **Finite inputs** are clamped to the provider-supported
       ``[0.0, 1.0]`` range. An INFO is logged only when the clamp
       actually changes the value, so in-range temperatures stay silent.
    """
    if not math.isfinite(temperature):
        logger.warning(
            "Received non-finite temperature=%s for model '%s'; using default 0.7",
            temperature,
            model,
        )
        return 0.7
    normalized = min(max(temperature, 0.0), 1.0)
    if normalized != temperature:
        logger.info(
            "Clamped Anthropic temperature for model '%s' from %s to %s",
            model,
            temperature,
            normalized,
        )
    return normalized


class AnthropicProvider(BaseLLMProvider):
    """Wraps the Anthropic SDK for chat completions with tool calling."""

    provider_name = "anthropic"
    # Default chat model used by chat / chat_stream / chat_stream_with_tools
    # when no explicit model is provided.  Exposed as a class attribute so
    # the FailoverLLMProvider can resolve per-model max_output_tokens
    # overrides for fallback calls that arrive with model=None.
    default_model = "claude-sonnet-5"
    context_window = 1_000_000
    max_output_tokens = 8_192
    # Anthropic has no hard tool-count limit; the constraint is context-window
    # tokens consumed by tool definitions.  Claude handles hundreds of tools.
    max_tools = 1024

    # ``chat_stream()`` forwards ``structured_output`` as
    # ``output_config.format`` (already gated per model via
    # ``_supports_structured_output`` — older models silently fall back to
    # prompt-based SAP), so the agentic loop may forward the kwarg to
    # streaming calls.
    supports_streaming_structured_output: bool = True

    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=600,
        )

    # ------------------------------------------------------------------
    # Message building (shared by chat, chat_stream_with_tools)
    # ------------------------------------------------------------------

    async def _prepare_request(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
        model: str,
        *,
        replay: ThinkingReplay | None = None,
    ) -> tuple[str | None, list[dict]]:
        """Resolve attachments, build the payload and run the overflow check.

        One path for ``chat``, ``chat_stream`` and ``chat_stream_with_tools``:
        the attachments of the whole request are resolved together
        under the provider's page cap and a share of *model*'s context window
        (:func:`resolve_payload_files`), the builder reads that plan, and the
        overflow check costs the native PDFs it kept by their PAGES rather
        than by the base64 bytes the measurement sees.

        *replay* then gives the current tool round's turns their thinking
        blocks back where their prefix is unchanged. The payload as
        built is checked exactly as before; blocks that would take the request
        over the overflow threshold are dropped again (``context_budget``) - a
        replay never fails a request.
        """
        plan = await resolve_payload_files(
            messages,
            "anthropic",
            context_window=self._resolve_context_window(model),
        )
        sources: list[tuple[int, ChatMessage]] = []
        system_msg, conversation = await self._build_anthropic_messages(
            messages,
            file_plan=plan,
            assistant_sources=sources,
            in_place_system=supports_in_place_system_messages(model),
        )
        text_chars, image_count, native_file_bytes = self._measure_anthropic_payload(
            system_msg, conversation
        )
        native_bytes = max(native_file_bytes - plan.native_pdf_bytes, 0)
        self._check_resolved_overflow(
            text_chars=text_chars,
            image_count=image_count,
            native_file_bytes=native_bytes,
            native_document_tokens=plan.native_pdf_tokens,
            tools=tools,
            model=model,
        )
        if replay is None:
            return system_msg, conversation
        conversation = replay.apply(system_msg, conversation, sources)
        if replay.replayed:
            estimated = self._estimate_resolved_tokens(
                text_chars=text_chars + replay.replayed_chars(),
                image_count=image_count,
                native_file_bytes=native_bytes,
                native_document_tokens=plan.native_pdf_tokens,
                tool_chars=self._tool_definition_chars(tools),
            )[0]
            window = self._resolve_context_window(model)
            if estimated > window * self._OVERFLOW_THRESHOLD:
                logger.info(
                    "Dropping replayed thinking blocks (model=%s): ~%d tokens with "
                    "them vs a %d-token window",
                    model,
                    estimated,
                    window,
                )
                conversation = replay.revert(REASON_CONTEXT_BUDGET)
        return system_msg, conversation

    async def _build_anthropic_messages(
        self,
        messages: list[ChatMessage],
        *,
        file_plan: PayloadFilePlan | None = None,
        assistant_sources: list[tuple[int, ChatMessage]] | None = None,
        in_place_system: bool = False,
    ) -> tuple[str | None, list[dict]]:
        """Convert generic ChatMessages to Anthropic-native format.

        Returns ``(system_msg, conversation)`` where *system_msg* is the
        merged system prompt (or None) and *conversation* is the list of
        Anthropic message dicts.

        The leading system messages form *system_msg*. A system message the
        loop appends later (a nudge, a notice) is merged into it too, unless
        *in_place_system* (the model accepts ``role: system`` inside
        ``messages``) and the API's placement rule holds where it sits
        (:func:`_fits_in_place`): then it stays in place, so the top-level
        prompt - and every cached turn after it - is unchanged.

        *file_plan* is the request-wide attachment resolution
        (:func:`resolve_payload_files`); built here when the caller has none,
        so a direct call still gets the page cap and the duplicate stubs —
        only the context-window share needs the model, which
        :meth:`_prepare_request` knows.

        *assistant_sources*, when given, is filled with ``(index, message)``
        for every assistant entry of *conversation*: which message each entry
        was built from, for the thinking replay. An assistant turn is
        always built as text + ``tool_use`` here.
        """
        if file_plan is None:
            file_plan = await resolve_payload_files(messages, "anthropic")
        system_msg: str | None = None
        conversation: list[dict] = []

        for m_idx, msg in enumerate(tool_results_in_call_order(messages)):
            if msg.role == "system":
                if in_place_system and _fits_in_place(messages, m_idx, conversation):
                    conversation.append({"role": "system", "content": msg.content})
                    continue
                system_msg = (
                    (system_msg + "\n\n" + msg.content) if system_msg else msg.content
                )

            elif msg.role == "user":
                if msg.images or msg.files:
                    content_blocks: list[dict] = []
                    for i, img in enumerate(msg.images or []):
                        if img.startswith(("http://", "https://")):
                            content_blocks.append(
                                {
                                    "type": "image",
                                    "source": {"type": "url", "url": img},
                                }
                            )
                        else:
                            prepared = await prepare_base64_image_for_llm_async(
                                img,
                                source=f"anthropic:user_message_image:{i}",
                            )
                            if prepared is None:
                                content_blocks.append(
                                    {"type": "text", "text": IMAGE_UNAVAILABLE_TEXT}
                                )
                                continue
                            optimized, mime = prepared
                            content_blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime,
                                        "data": optimized,
                                    },
                                }
                            )
                    for f_idx, file_ref in enumerate(msg.files or []):
                        resolved = file_plan.get(m_idx, f_idx)
                        if resolved is None:
                            resolved = await resolve_file(file_ref, "anthropic")
                        if isinstance(resolved, NativeFile):
                            if resolved.content_type.startswith("image/"):
                                prepared_bytes = await prepare_image_bytes_for_llm_async(
                                    resolved.data,
                                    resolved.content_type,
                                    source=(
                                        f"anthropic:user_file_image:{f_idx}"
                                        f"[{file_ref.file_id}]"
                                    ),
                                )
                                if prepared_bytes is None:
                                    content_blocks.append(
                                        {"type": "text", "text": IMAGE_UNAVAILABLE_TEXT}
                                    )
                                    continue
                                opt_bytes, opt_mime = prepared_bytes
                                content_blocks.append(
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": opt_mime,
                                            "data": base64.b64encode(opt_bytes).decode(
                                                "ascii"
                                            ),
                                        },
                                    }
                                )
                            elif resolved.content_type == "application/pdf":
                                content_blocks.append(
                                    {
                                        "type": "document",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "application/pdf",
                                            "data": base64.b64encode(
                                                resolved.data
                                            ).decode("ascii"),
                                        },
                                    }
                                )
                        elif isinstance(resolved, FallbackText):
                            content_blocks.append(
                                {"type": "text", "text": resolved.text}
                            )
                    content_blocks.append({"type": "text", "text": msg.content})
                    conversation.append({"role": "user", "content": content_blocks})
                else:
                    conversation.append({"role": "user", "content": msg.content or ""})

            elif msg.role == "assistant":
                if assistant_sources is not None:
                    assistant_sources.append((len(conversation), msg))
                conversation.append(
                    {
                        "role": "assistant",
                        "content": plain_assistant_content(msg.content, msg.tool_calls),
                    }
                )

            elif msg.role == "tool":
                content: str | list[dict] = msg.content
                if msg.files:
                    images, extra_texts = await prepare_tool_result_attachments(
                        msg.files,
                        "anthropic",
                        source=f"anthropic:tool_result:{msg.tool_call_id}",
                    )
                    text = msg.content or ""
                    if extra_texts:
                        text = (
                            (text + "\n\n" if text else "")
                            + "\n\n".join(extra_texts)
                        )
                    if images:
                        blocks: list[dict] = []
                        if text:
                            blocks.append({"type": "text", "text": text})
                        for img in images:
                            blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": img.mime,
                                        "data": base64.b64encode(img.data).decode(
                                            "ascii"
                                        ),
                                    },
                                }
                            )
                        if TOOL_RESULT_IMAGE_DATA_NOTE:
                            blocks.append({"type": "text", "text": TOOL_RESULT_IMAGE_DATA_NOTE})
                        content = blocks
                    else:
                        content = text
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": content,
                }
                if (
                    conversation
                    and conversation[-1]["role"] == "user"
                    and isinstance(conversation[-1]["content"], list)
                    and conversation[-1]["content"]
                    and conversation[-1]["content"][0].get("type") == "tool_result"
                ):
                    conversation[-1]["content"].append(tool_result_block)
                else:
                    conversation.append(
                        {"role": "user", "content": [tool_result_block]}
                    )

        # Newer Claude models reject assistant-message prefill. Ensure outbound
        # Anthropic conversations always end on a user turn.
        trimmed_assistant_tail = 0
        trimmed_tool_use_blocks = 0
        # If conversation history ends with assistant tool_use blocks without
        # matching tool-result user turns, those pending tool invocations are
        # intentionally dropped to satisfy Anthropic's user-final-turn rule.
        while conversation and conversation[-1]["role"] == "assistant":
            tail = conversation.pop()
            trimmed_assistant_tail += 1
            content = tail.get("content")
            if isinstance(content, list):
                trimmed_tool_use_blocks += sum(
                    1 for block in content if block.get("type") == "tool_use"
                )

        if assistant_sources is not None:
            assistant_sources[:] = [
                (index, source)
                for index, source in assistant_sources
                if index < len(conversation)
            ]

        if trimmed_assistant_tail:
            logger.info(
                "Stripped %d trailing assistant message(s) to satisfy Anthropic prefill constraint",
                trimmed_assistant_tail,
            )
        if trimmed_tool_use_blocks:
            logger.debug(
                "Dropped %d trailing assistant tool_use block(s) while normalizing Anthropic conversation tail",
                trimmed_tool_use_blocks,
            )

        if not conversation:
            raise ValueError(
                "Anthropic API requires at least one non-system message, "
                "but the conversation list is empty after filtering."
                f" Trailing assistant messages removed: {trimmed_assistant_tail}. "
                f"Original messages count: {len(messages)}, "
                f"roles: {[m.role for m in messages]}"
            )

        return system_msg, conversation

    @staticmethod
    def _measure_anthropic_payload(
        system_msg: str | None,
        conversation: list[dict],
    ) -> tuple[int, int, int]:
        """Measure an Anthropic-format payload after file resolution.

        Returns (text_chars, image_count, native_file_bytes).
        """
        text_chars = len(system_msg or "")
        image_count = 0
        native_file_bytes = 0

        for msg in conversation:
            content = msg.get("content")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    btype = block.get("type", "")
                    if btype == "text":
                        text_chars += len(block.get("text", ""))
                    elif btype == "image":
                        image_count += 1
                    elif btype == "document":
                        # Native PDF — estimate from base64 length
                        source = block.get("source", {})
                        b64_data = source.get("data", "")
                        native_file_bytes += len(b64_data) * 3 // 4
                    elif btype == "tool_use":
                        # Tool-use blocks have input (dict) — estimate chars
                        text_chars += len(_json.dumps(block.get("input", {})))
                    elif btype in ("thinking", "redacted_thinking"):
                        # A replayed block - its reasoning is input.
                        text_chars += block_chars(block)
                    elif btype == "tool_result":
                        tr_content = block.get("content", "")
                        if isinstance(tr_content, str):
                            text_chars += len(tr_content)
                        elif isinstance(tr_content, list):
                            for inner in tr_content:
                                if isinstance(inner, dict):
                                    if inner.get("type") == "text":
                                        text_chars += len(inner.get("text", ""))
                                    elif inner.get("type") == "image":
                                        image_count += 1

        return text_chars, image_count, native_file_bytes

    # ------------------------------------------------------------------
    # chat (with tool support)
    # ------------------------------------------------------------------

    async def _open_event_stream(
        self, request: dict
    ) -> anthropic.AsyncStream[anthropic.types.RawMessageStreamEvent]:
        """``messages.create(stream=True)``: the raw server-sent events.

        Not the SDK's ``messages.stream()`` helper: it rebuilds a snapshot of
        the whole message on every event and re-parses a tool call's
        accumulated input JSON on each ``input_json_delta``. The API sends
        that input 7 bytes at a time, so the helper's cost is quadratic in the
        input's size - 0.3 s of event loop for a 60 KB deck spec, 1.1 s for
        120 KB - while both streaming paths read only the raw events
        and accumulate what they need themselves.
        """
        return await self.client.messages.create(**request, stream=True)

    async def _enter_event_stream(
        self, stack: AsyncExitStack, request: dict
    ) -> anthropic.AsyncStream[anthropic.types.RawMessageStreamEvent]:
        return await stack.enter_async_context(await self._open_event_stream(request))

    async def _create_message(self, params: dict) -> anthropic.types.Message:
        """``messages.create(**params)`` with the transient-error retry, plus
        ONE deterministic retry when the model refuses ``thinking.type.disabled``.

        A 400 is not transient, so this is a branch of its own around
        ``retry_llm_call`` rather than a tweak to its policy: on the exact
        refusal (:func:`_refuses_disabled_thinking`) the model is added to
        :data:`_MODELS_REFUSING_DISABLED_THINKING` - every later call in this
        process sends the adaptive form first; on the refusal that asks for
        ``between_tools`` (:func:`_asks_for_between_tools`) it is added to
        :data:`_MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS` - and the SAME request is sent
        once more with thinking turned off the way that model documents it
        (:func:`_apply_thinking_off`). The retry runs under the same client
        timeout and the same transient-error policy as any request. Nothing
        is counted twice: a 400 carries no usage, and the caller's trace
        generation wraps this whole call, not each attempt.

        Any other error, and a failure of the adaptive form itself, propagates
        unchanged so the router's failover chain runs as before. A request
        that never sent ``disabled`` (a remembered or always-on model,
        ``disable_thinking`` unset) cannot enter the branch, so there is never
        a second retry.
        """
        try:
            return await retry_llm_call(
                lambda: self.client.messages.create(**params),
                is_retryable=_is_retryable_anthropic,
            )
        except anthropic.APIStatusError as exc:
            if not _sends_disabled_thinking(params):
                raise
            if _asks_for_between_tools(exc):
                learned, form = _MODELS_TURNING_THINKING_OFF_BETWEEN_TOOLS, "thinking.type.between_tools"
            elif _refuses_disabled_thinking(exc):
                learned, form = _MODELS_REFUSING_DISABLED_THINKING, f"adaptive thinking at effort={_LOWEST_EFFORT}"
            else:
                raise

        # Only reached for the exact refusal of the ``disabled`` form.
        model = str(params["model"])
        learned.add(model)
        logger.info(
            "Anthropic model '%s' refuses thinking.type.disabled; retrying once with "
            "%s and remembering the model for this process",
            model,
            form,
        )
        # Same request, thinking turned off the way this model documents it.
        # The first attempt's dict is left as it was sent; ``output_config`` is
        # copied too because both forms may write ``effort`` into it.
        retry_params = dict(params)
        if "output_config" in params:
            retry_params["output_config"] = dict(params["output_config"])
        _apply_thinking_off(
            retry_params,
            model,
            max_output_tokens=resolve_provider_max_output_tokens(self, model),
        )
        return await retry_llm_call(
            lambda: self.client.messages.create(**retry_params),
            is_retryable=_is_retryable_anthropic,
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs,
    ) -> ChatResponse:
        model = model or self.default_model

        # --- build params (the conversation is assembled below, once the
        # thinking config and the tools it is sent with are known) ---
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [],
        }
        if not _should_omit_temperature(model):
            params["temperature"] = _normalize_temperature(model, temperature)

        # --- output_config (reasoning effort + structured output) ---
        # Both features share the same top-level output_config dict so we
        # merge them incrementally.
        if reasoning_effort := supported_effort(model, kwargs.get("reasoning_effort")):
            params.setdefault("output_config", {})["effort"] = reasoning_effort
        if structured_output := kwargs.get("structured_output"):
            if _supports_structured_output(model):
                params.setdefault("output_config", {})["format"] = structured_output
            else:
                logger.debug(
                    "Anthropic model '%s' does not support output_config.format; "
                    "falling back to prompt-based schema enforcement.",
                    model,
                )

        # Turn OFF adaptive thinking for bounded, mechanical structured-output
        # utility calls (JSON extraction / classification). On Claude Sonnet 5 /
        # Opus 4.7+ adaptive thinking is ON by default and shares the max_tokens
        # budget with the response, so a small max_tokens leaves too little room
        # for the JSON answer — and thinking + structured output has known edge
        # cases. Gated to the new-gen models that actually default to adaptive
        # thinking so the parameter is never sent to models that reject it. A
        # model that refuses ``disabled`` outright is learned from its
        # 400 in ``_create_message`` and gets the adaptive + lowest-effort form;
        # one the rules declare gets its documented form at once.
        if kwargs.get("disable_thinking") and _defaults_to_adaptive_thinking(model):
            _apply_thinking_off(
                params,
                model,
                max_output_tokens=resolve_provider_max_output_tokens(self, model),
            )

        # --- tools (deduplicate by name — last definition wins) ---
        wire_tools = _wire_tools(tools)
        if wire_tools:
            params["tools"] = wire_tools

        # --- conversation (+ the current tool round's thinking blocks;
        # a call that asked for no thinking never re-sends any, whichever form
        # turned it off - the adaptive one on a model that refuses
        # ``disabled``) ---
        replay = ThinkingReplay(
            model=model,
            thinking=(
                {"type": "disabled"}
                if kwargs.get("disable_thinking")
                else params.get("thinking")
            ),
            tools=wire_tools,
            enabled=settings.anthropic_thinking_replay_enabled,
        )
        system_msg, params["messages"] = await self._prepare_request(
            messages, tools, model, replay=replay
        )
        if system_msg:
            params["system"] = system_msg

        # --- prompt caching (tools + system breakpoints, automatic messages) ---
        _apply_prompt_caching(params, kwargs.get("prompt_cache"))

        # --- call (retry on transient errors; one adaptive retry on a refused
        # ``thinking.type.disabled``; one retry without replayed thinking
        # blocks and one with the in-place system messages folded, each on
        # its refusal) ---
        recovery = _RefusalRecovery(params, replay)
        while True:
            try:
                response = await self._create_message(params)
                break
            except anthropic.APIStatusError as exc:
                retry = recovery.next_params(exc, params)
                if retry is None:
                    raise
                params = retry

        if response.stop_reason == _DECLINED_STOP_REASON:
            raise declined_error(
                model,
                getattr(response, "stop_details", None),
                _usage_from_response(response.usage),
            )

        # --- parse response ---
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        if response.stop_reason == "tool_use":
            stop_reason = "tool_use"
        elif response.stop_reason == "max_tokens":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        content = "\n".join(text_parts)
        # Recover a tool call the model serialised as text instead of returning
        # a structured ``tool_use`` block: a trailing, unquoted leak of declared
        # tools becomes a real call here, any other trailing leak is flagged
        # for the loop to re-prompt (``app.llm.tool_markup_recovery``).
        leaked_unrecovered = False
        recovery = recover_leaked_tool_calls(content, tool_calls, tools)
        if recovery is not None:
            if recovery.tool_calls:
                tool_calls = recovery.tool_calls
                content = recovery.content
                stop_reason = "tool_use"
            leaked_unrecovered = recovery.unrecovered
        return ChatResponse(
            content=content,
            model=response.model,
            usage=_usage_from_response(response.usage),
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=stop_reason,
            thinking="\n".join(thinking_parts) if thinking_parts else None,
            _provider_extra=replay.capture(
                blocks_from_content(response.content), content, tool_calls
            ),
            trace_metadata=replay.report(),
            leaked_tool_markup_unrecovered=leaked_unrecovered,
        )

    # ------------------------------------------------------------------
    # chat_stream (native Anthropic streaming)
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream text chunks from Anthropic Claude.

        Handles the full message format including assistant messages with
        tool_calls and tool-result messages so that streaming works correctly
        after tool-calling iterations (e.g. during agentic loop wind-down).
        """
        model = model or self.default_model
        usage_sink = usage_sink_of(kwargs)
        if usage_sink is not None:
            usage_sink.restart()

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [],
        }
        if not _should_omit_temperature(model):
            create_kwargs["temperature"] = _normalize_temperature(model, temperature)

        # --- output_config (reasoning effort + structured output) ---
        if reasoning_effort := supported_effort(model, kwargs.get("reasoning_effort")):
            create_kwargs.setdefault("output_config", {})["effort"] = reasoning_effort
        if structured_output := kwargs.get("structured_output"):
            if _supports_structured_output(model):
                create_kwargs.setdefault("output_config", {})["format"] = structured_output
            else:
                logger.debug(
                    "Anthropic model '%s' does not support output_config.format; "
                    "falling back to prompt-based schema enforcement.",
                    model,
                )

        on_thinking = kwargs.get("on_thinking")
        _apply_summarized_thinking(create_kwargs, model, on_thinking)

        # --- conversation (+ the current tool round's thinking blocks;
        # this call sends no tools, so a tool round's prefix never matches) ---
        replay = ThinkingReplay(
            model=model,
            thinking=create_kwargs.get("thinking"),
            tools=None,
            enabled=settings.anthropic_thinking_replay_enabled,
        )
        system_msg, create_kwargs["messages"] = await self._prepare_request(
            messages, tools, model, replay=replay
        )
        if system_msg:
            create_kwargs["system"] = system_msg

        # --- prompt caching (tools + system breakpoints, automatic messages) ---
        _apply_prompt_caching(create_kwargs, kwargs.get("prompt_cache"))

        self.last_stop_reason = None
        self.last_usage = None
        self.last_trace_metadata = None
        self.last_provider_extra = None
        _usage = _StreamUsageAccumulator(usage_sink)
        _joiner = _ThinkingBlockJoiner()
        _blocks = StreamedBlocks()
        _text_parts: list[str] = []
        _declined_details: object = None
        _was_declined = False
        # Retry transient errors on stream establishment only — nothing has
        # been yielded to the caller until the stream context is entered.
        # Each retry attempt opens a fresh stream; a failed open registers
        # nothing on the stack, so only the successfully opened stream is
        # closed when the block exits.
        async with AsyncExitStack() as _stack:
            recovery = _RefusalRecovery(create_kwargs, replay)
            attempt_kwargs = create_kwargs
            while True:
                try:
                    stream = await retry_llm_call(
                        lambda kw=attempt_kwargs: self._enter_event_stream(_stack, kw),
                        is_retryable=_is_retryable_anthropic,
                    )
                    break
                except anthropic.APIStatusError as exc:
                    # Refused at open, before anything reached the caller.
                    retry = recovery.next_params(exc, attempt_kwargs)
                    if retry is None:
                        raise
                    attempt_kwargs = retry
            _in_thinking = False
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "message_start":
                    _usage.absorb_message_start(event)
                elif etype == "content_block_start":
                    if usage_sink is not None:
                        usage_sink.first_output()
                    block = getattr(event, "content_block", None)
                    _in_thinking = getattr(block, "type", "") == "thinking"
                    _joiner.start_block()
                    _blocks.start(block)
                elif etype == "content_block_stop":
                    _in_thinking = False
                    _blocks.stop()
                elif etype == "message_delta":
                    _usage.absorb_message_delta(event)
                    delta = getattr(event, "delta", None)
                    sr = getattr(delta, "stop_reason", None)
                    if sr == "max_tokens":
                        self.last_stop_reason = "max_tokens"
                    elif sr == "end_turn":
                        self.last_stop_reason = "end_turn"
                    elif sr == _DECLINED_STOP_REASON:
                        _was_declined = True
                        _declined_details = getattr(delta, "stop_details", None)
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", "")
                    _blocks.delta(delta)
                    if dtype == "thinking_delta" and on_thinking:
                        text = getattr(delta, "thinking", "")
                        if text:
                            await on_thinking(_joiner.delta(text))
                    elif dtype == "text_delta" and not _in_thinking:
                        text = getattr(delta, "text", "")
                        if text:
                            _text_parts.append(text)
                            yield text

        # Surface token usage to the agentic loop (which reads
        # ``last_usage`` after the generator is exhausted) so streamed
        # turns are cost-tracked in Langfuse instead of recording zero
        # tokens.
        self.last_usage = _usage.to_dict()
        self.last_trace_metadata = replay.report()
        if _was_declined:
            raise declined_error(model, _declined_details, self.last_usage)
        self.last_provider_extra = replay.capture(
            _blocks.layout(), "".join(_text_parts), None
        )

    # ------------------------------------------------------------------
    # chat_stream_with_tools (native streaming with tool-call detection)
    # ------------------------------------------------------------------

    async def chat_stream_with_tools(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> ChatResponse:
        model = model or self.default_model
        usage_sink = usage_sink_of(kwargs)
        if usage_sink is not None:
            usage_sink.restart()

        # --- build params (the conversation is assembled below, once the
        # thinking config and the tools it is sent with are known) ---
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [],
        }
        if not _should_omit_temperature(model):
            params["temperature"] = _normalize_temperature(model, temperature)
        if reasoning_effort := supported_effort(model, kwargs.get("reasoning_effort")):
            params.setdefault("output_config", {})["effort"] = reasoning_effort
        wire_tools = _wire_tools(tools)
        if wire_tools:
            params["tools"] = wire_tools

        on_thinking = kwargs.get("on_thinking")
        _apply_summarized_thinking(params, model, on_thinking)

        # --- conversation (+ the current tool round's thinking blocks) ---
        replay = ThinkingReplay(
            model=model,
            thinking=params.get("thinking"),
            tools=wire_tools,
            enabled=settings.anthropic_thinking_replay_enabled,
        )
        system_msg, params["messages"] = await self._prepare_request(
            messages, tools, model, replay=replay
        )
        if system_msg:
            params["system"] = system_msg

        # --- prompt caching (tools + system breakpoints, automatic messages) ---
        _apply_prompt_caching(params, kwargs.get("prompt_cache"))

        # Retry is only safe while no output has escaped to the caller.  The
        # flag flips just before the first ``on_token`` / ``on_thinking``
        # delivery; afterwards the retryable predicate returns False and any
        # error propagates immediately (replaying would duplicate output).
        emitted = False

        async def _stream_once(request: dict) -> ChatResponse:
            nonlocal emitted

            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            stop_reason = "end_turn"
            was_declined = False
            declined_details: object = None
            has_tool_calls = False
            current_tool: dict | None = None
            if usage_sink is not None:
                usage_sink.restart()
            usage_acc = _StreamUsageAccumulator(usage_sink)
            joiner = _ThinkingBlockJoiner()
            blocks = StreamedBlocks()

            async with await self._open_event_stream(request) as stream:
                async for event in stream:
                    etype = getattr(event, "type", "")

                    if etype == "message_start":
                        usage_acc.absorb_message_start(event)

                    elif etype == "content_block_start":
                        if usage_sink is not None:
                            usage_sink.first_output()
                        block = getattr(event, "content_block", None)
                        block_type = getattr(block, "type", "")
                        joiner.start_block()
                        blocks.start(block)
                        if block_type == "tool_use":
                            has_tool_calls = True
                            current_tool = {
                                "id": getattr(block, "id", ""),
                                "name": getattr(block, "name", ""),
                                "arguments": [],
                            }

                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", "")
                        blocks.delta(delta)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                text_parts.append(text)
                                if not has_tool_calls and on_token:
                                    emitted = True
                                    await on_token(text)
                        elif dtype == "input_json_delta":
                            if current_tool is not None:
                                current_tool["arguments"].append(
                                    getattr(delta, "partial_json", "")
                                )
                        elif dtype == "thinking_delta":
                            thinking = getattr(delta, "thinking", "")
                            if thinking:
                                thinking = joiner.delta(thinking)
                                thinking_parts.append(thinking)
                                if on_thinking:
                                    emitted = True
                                    await on_thinking(thinking)

                    elif etype == "content_block_stop":
                        blocks.stop()
                        if current_tool is not None:
                            tool_calls.append(
                                ToolCall(
                                    id=current_tool["id"],
                                    name=current_tool["name"],
                                    arguments=parse_tool_arguments(
                                        "".join(current_tool["arguments"]),
                                        tool_name=current_tool["name"],
                                    ),
                                )
                            )
                            current_tool = None

                    elif etype == "message_delta":
                        usage_acc.absorb_message_delta(event)
                        delta = getattr(event, "delta", None)
                        sr = getattr(delta, "stop_reason", None)
                        if sr == "tool_use":
                            stop_reason = "tool_use"
                        elif sr == "max_tokens":
                            stop_reason = "max_tokens"
                        else:
                            stop_reason = "end_turn"
                        if sr == _DECLINED_STOP_REASON:
                            was_declined = True
                            declined_details = getattr(delta, "stop_details", None)

            if was_declined:
                raise declined_error(model, declined_details, usage_acc.to_dict())
            content = "".join(text_parts)
            # Recover a tool call streamed as text (see the non-streaming path).
            # The tokens were already delivered, so the recovered call is the
            # correction: the loop runs it and the real output follows.
            leaked_unrecovered = False
            recovery = recover_leaked_tool_calls(content, tool_calls, tools)
            if recovery is not None:
                if recovery.tool_calls:
                    tool_calls = recovery.tool_calls
                    content = recovery.content
                    stop_reason = "tool_use"
                leaked_unrecovered = recovery.unrecovered
            return ChatResponse(
                content=content,
                model=model,
                usage=usage_acc.to_dict(),
                tool_calls=tool_calls if tool_calls else None,
                stop_reason=stop_reason,
                thinking="".join(thinking_parts) if thinking_parts else None,
                _provider_extra=replay.capture(blocks.layout(), content, tool_calls),
                trace_metadata=replay.report(),
                leaked_tool_markup_unrecovered=leaked_unrecovered,
            )

        def _retryable(exc: Exception) -> bool:
            return not emitted and _is_retryable_anthropic(exc)

        recovery = _RefusalRecovery(params, replay)
        request = params
        while True:
            try:
                return await retry_llm_call(
                    lambda sent=request: _stream_once(sent), is_retryable=_retryable
                )
            except anthropic.APIStatusError as exc:
                # A refusal is an HTTP 400 at stream open: nothing was emitted.
                if emitted:
                    raise
                retry = recovery.next_params(exc, request)
                if retry is None:
                    raise
                request = retry

    # ------------------------------------------------------------------
    # embed
    # ------------------------------------------------------------------

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        raise NotImplementedError(
            "Anthropic does not provide a native embeddings endpoint. "
            "Configure an OpenAI provider for embedding operations."
        )

    # ------------------------------------------------------------------
    # list_models
    # ------------------------------------------------------------------

    async def list_models(self) -> list[str]:
        """Fetch available models from the Anthropic API."""
        models: list[str] = []
        page = await self.client.models.list(limit=100)
        for model in page.data:
            models.append(model.id)
        models.sort(reverse=True)
        return models

    async def list_model_context_windows(self) -> dict[str, int] | None:
        """``max_input_tokens`` of each model the Models API lists (same page as ``list_models``)."""
        page = await self.client.models.list(limit=100)
        windows: dict[str, int] = {}
        for model in page.data:
            if tokens := reported_context_window(getattr(model, "max_input_tokens", None)):
                windows[model.id] = tokens
        return windows
