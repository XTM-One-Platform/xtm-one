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

"""Agentic execution loop – the core LLM ↔ tool calling cycle.

This is the engine that makes skills truly agentic. It:
  1. Sends the conversation + available tools to the LLM.
  2. If the LLM requests tool calls → executes them.
  3. Feeds tool results back into the conversation.
  4. Repeats until the LLM produces a final text response (no tool calls)
     or the iteration limit is reached.

When approaching the iteration limit, a three-stage wind-down ensures
the LLM always produces a useful final answer:

  1. **Wind-down nudge** — injected ~4 iterations before the cap to
     encourage the LLM to stop calling tools and synthesise.
  2. **Tool stripping** — 2 iterations before the cap, all tools are
     removed so the LLM *must* produce a text response naturally.
  3. **Forced final answer** — if the loop still somehow exhausts (edge
     case), one last tool-free LLM call is made with a strong prompt.

Usage::

    from app.tools.agent_loop import run_agent_loop

    response = await run_agent_loop(
        llm=ctx.llm,
        messages=[ChatMessage(role="user", content="Review this PR")],
        tools=all_tool_definitions,
        executor=composite_executor,
    )

The *messages* list is modified **in-place** — after the loop returns, it
contains the complete conversation including all assistant messages, tool
calls and their results.  Callers that need the original messages preserved
should pass ``list(messages)`` instead.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import dataclasses
import difflib
import functools
import hashlib
import inspect
import itertools
import json
import logging
import math
import re
import time
import uuid as uuid_module
from collections import OrderedDict, deque
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

import jsonschema

from app.core.cpu_time import CpuStopwatch
from app.core.redaction import redacted_stream_callbacks
from app.core.turn_writer import bound as turn_writer_bound
from app.core.turn_writer import ensure_single_writer, superseded_reason, superseded_write_refusal
from app.core.utils import format_exception_message
from app.llm.attribution import record_model_downgrade
from app.llm.base import ModelDeclinedError, StreamUsageSink, message_payload_chars
from app.llm.base import provider_max_tools as _max_tools_of
from app.llm.cut_answers import answer_was_cut, chat_retrying_a_cut
from app.services import schema_guard
from app.services.content_safety import untrusted_block_source, untrusted_block_spans, wrap_untrusted
from app.services.context_usage import (
    CONTEXT_LEVEL_APPROACHING,
    CONTEXT_LEVEL_NORMAL,
    CONTEXT_LEVEL_OVERFLOWED,
    split_tokens,
)
from app.services.ctem_module_preflight import refusal_answer as ctem_refusal_answer
from app.services.ctem_module_preflight import refused_module as ctem_refused_module
from app.services.langfuse_tracing import (
    USAGE_PARTS_KEY as _LF_USAGE_PARTS_KEY,
)
from app.services.langfuse_tracing import (
    end_generation as _lf_end_generation,
)
from app.services.langfuse_tracing import (
    end_span as _lf_end_span,
)
from app.services.langfuse_tracing import (
    end_tool_span as _lf_end_tool_span,
)
from app.services.langfuse_tracing import (
    enrich_trace_metadata as _lf_enrich_trace_metadata,
)
from app.services.langfuse_tracing import (
    get_current_trace_ctx as _lf_get_ctx,
)
from app.services.langfuse_tracing import (
    make_retry_attempt_observer as _lf_make_retry_observer,
)
from app.services.langfuse_tracing import (
    mark_first_output as _lf_mark_first_output,
)
from app.services.langfuse_tracing import (
    reset_current_retry_observer as _lf_reset_retry_observer,
)
from app.services.langfuse_tracing import (
    reset_current_trace_ctx as _lf_reset_ctx,
)
from app.services.langfuse_tracing import (
    serialize_file_refs as _lf_serialize_file_refs,
)
from app.services.langfuse_tracing import (
    set_current_retry_observer as _lf_set_retry_observer,
)
from app.services.langfuse_tracing import (
    set_current_trace_ctx as _lf_set_ctx,
)
from app.services.langfuse_tracing import (
    start_continuation_generation as _lf_start_continuation_gen,
)
from app.services.langfuse_tracing import (
    start_direct_generation as _lf_start_direct_gen,
)
from app.services.langfuse_tracing import (
    start_empty_response_retry_generation as _lf_start_empty_retry_gen,
)
from app.services.langfuse_tracing import (
    start_iteration_span as _lf_start_iter_span,
)
from app.services.langfuse_tracing import (
    start_llm_generation as _lf_start_llm_gen,
)
from app.services.langfuse_tracing import (
    start_loop_span as _lf_start_loop_span,
)
from app.services.langfuse_tracing import (
    start_tool_span as _lf_start_tool_span,
)
from app.services.langfuse_tracing import (
    update_iteration_span_tools as _lf_update_iter_tools,
)
from app.services.langfuse_tracing import (
    update_trace_input_tools as _lf_update_trace_tools,
)
from app.services.langfuse_tracing import (
    usage_convention as _lf_usage_convention,
)
from app.services.langfuse_tracing import (
    usage_parts as _lf_usage_parts,
)
from app.services.langfuse_tracing import (
    usage_parts_needed as _lf_usage_parts_needed,
)
from app.services.output_schema_validation import output_schema_errors, output_schema_validator, raw_json_schema
from app.services.schema_guard import LOCAL_SCHEMA_REFS_ONLY as _LOCAL_SCHEMA_REFS_ONLY
from app.services.tool_classification import (
    READ_OVERRIDES,
    READ_VERBS,
    WRITE_OVERRIDES,
    WRITE_TOKENS,
    infer_tool_type,
)
from app.services.tracing.cost_calculator import input_output_tokens
from app.services.tracing.turn_stages import mark_current as _turn_stage_mark
from app.tools import read_back as write_read_back
from app.tools import send_guard
from app.tools.base import (
    TOOL_NOT_AVAILABLE_SENTINEL,
    CompositeToolExecutor,
    FileContext,
    HumanInputRequestedError,
    SendGuard,
    ToolCall,
    ToolDefinition,
    ToolModelSpend,
    ToolNameResolver,
    ToolResult,
    WriteCoalescing,
    WriteReadBack,
    build_llm_tool_names,
    clip_head_and_tail,
    declared_aliases,
    declared_cosmetic_arguments,
    fit_descriptions_to_surface,
    format_tool_listing,
    is_auth_failure_result,
    is_rate_limit_error,
    limiting_tool_results,
    offering_tools,
    spending_for_tool,
    timing_tool_call,
    tool_retention_key,
    visible_aliases,
)
from app.tools.base import (
    is_error_shaped_result as _is_error_shaped_result,
)
from app.tools.base import (
    looks_like_tool_error as _looks_like_tool_error,
)
from app.tools.run_budget import (
    MIN_OUTPUT_TOKENS,
    BudgetDimension,
    BudgetFallbackModel,
    BudgetViolation,
    CallReservation,
    RunBudget,
)
from app.tools.tool_events import (
    TOOL_PROGRESS_STATUS,
    ToolEventSink,
    bind_tool_event_sink,
    reset_tool_event_sink,
)
from app.tools.tool_gating import (
    CORE_OVERFLOW_GROUP_ID,
    LOAD_TOOLS_TOOL_NAME,
    MIN_GATED_BUDGET,
    build_tool_gate,
    tool_group,
)
from app.tools.wait_policy import (
    CHECK_TASK_STATUS_TOOL_NAME,
    MIN_GRANTED_WAIT_SECONDS,
    WAIT_REASON_ELAPSED,
    WAIT_REASON_HANDOFF,
    WAIT_REASON_INVALID,
    WAIT_REASON_REFUSED,
    WAIT_TERMINAL_STATUSES,
    WAIT_TOOL_NAME,
    WAIT_TOOL_NAMES,
    WAIT_TOOL_TIMEOUT,
    WaitBudget,
    WaitCondition,
    WaitPolicy,
    WaitResult,
    build_wait_system_prompt,
    build_wait_tool_definition,
    cut_wait_remaining,
    cut_wait_to_continue,
    execute_wait,
    format_wait_duration,
    parse_until_task_ids,
)
from app.tools.write_coalescing import (
    coalesce_write_calls,
    format_coalesced_answer,
)

if TYPE_CHECKING:
    from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
    from app.services.tool_surface_refresh import IntegrationSurfaceRefresher

    # ``True`` for a tool result the model has not been sent yet — see
    # ``_unseen_tool_result_predicate`` and the curation passes that take it.
    UnseenPredicate = Callable[[ChatMessage], bool]

# Type alias for the progress callback.
OnEventCallback = Callable[[dict[str, Any]], Awaitable[None]]

# Type alias for the token streaming callback.
# Called with each text chunk as the LLM generates the final response.
OnTokenCallback = Callable[[str], Awaitable[None]]

# Type alias for the per-iteration activity callback.
# Called once per agent-loop iteration with (iteration_number, tool_call_names,
# had_error) so a caller can stream a deterministic, LLM-independent "what the
# agent is doing right now" signal to its own UI/timeline. Best-effort: the loop
# wraps every invocation in try/except so a callback error can never break it.
OnIterationActivityCallback = Callable[[int, list[str], bool], Awaitable[None]]


class UsageHook(Protocol):
    """The loop's usage hook (``_merge_usage``), handed to the helpers that
    make calls of their own: one call's usage and the model that served it,
    the one its response names. ``None`` when nothing names it: a
    call that never returned and that no failover fallback was serving."""

    def __call__(self, usage: dict, served_model: str | None = None) -> None: ...


# Asked before a compaction summary cut at its budget is asked again,
# with the retry's estimated input tokens and output budget. ``False`` leaves
# the cut summary to the deterministic pruner.
CompactionRetryCheck = Callable[[int, int], bool]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminalToolDecision:
    final_content: str | None
    reason: str
    detail: str | None = None

# ---------------------------------------------------------------------------
# Model-aware context budgeting
#
# Instead of hardcoding one-size-fits-all limits, the agent loop derives
# per-tool-result and session-level character budgets from the LLM
# provider's ``context_window`` attribute (set on ``BaseLLMProvider``
# subclasses and optionally overridden via the admin UI).
#
# This lets Gemini/Anthropic (1M tokens) use their full capacity while
# keeping OpenAI (128K) safe — no lowest-common-denominator penalties.
# ---------------------------------------------------------------------------

# Approximate chars-per-token ratio (conservative; most tokenisers
# average 3-4 chars/token for English text).
_CHARS_PER_TOKEN = 3

# Budget allocation (fractions of the context window):
#   - 60 % for accumulated session messages (history + tool results)
#   - 15 % cap per individual tool result
#   - remaining 25 % left for system prompt, tool definitions, generation
_SESSION_FRACTION = 0.60
_TOOL_RESULT_FRACTION = 0.15

_FALLBACK_CONTEXT_WINDOW = 128_000

# Hard cap on a single tool result to avoid context overflow even with
# very large context windows (e.g. 1M tokens).  150K chars ≈ 50K tokens
# leaves ample room for history, system prompt, and tool definitions.
_MAX_TOOL_RESULT_CHARS_CAP = 150_000

_RECENT_TOOL_RESULTS_KEEP = 4  # Always keep the last N tool messages intact
# Tool-result screenshots / downloaded images are short Markdown (~800 chars)
# so they survive body-based pruning, but as native vision blocks they cost
# thousands of tokens each. Keep files only on the last N file-bearing
# tool-call GROUPS (a group = every tool result answering one assistant
# ``tool_calls`` turn), never on the last N file-bearing MESSAGES: a parallel
# batch of three image downloads is one group, and counting per message
# stripped the first image of that batch before the model had ever seen it
# (trace 722375f1, 2026-09-24 — the model then re-downloaded it 28 times).
# The most recent file-bearing group is always kept whatever this says; the
# byte budget below is what bounds an oversized batch.
_RECENT_TOOL_FILES_KEEP = 2
# Byte budget for the native vision attachments kept in the live context,
# window-scaled like ``_TOOL_RESULT_LIVE_*`` (see
# :func:`_compute_tool_result_files_budget`). Images are forwarded as raw
# pixels, not chars, so the budget is in bytes of stored image data —
# ``_TOOL_RESULT_FILES_BYTES_PER_TOKEN`` is the order-of-magnitude proxy for
# how many PNG bytes one vision token buys on the current providers.
_TOOL_RESULT_FILES_LIVE_FRACTION = 0.05
_TOOL_RESULT_FILES_BYTES_PER_TOKEN = 256
_TOOL_RESULT_FILES_LIVE_MIN_BYTES = 2 * 1024 * 1024
_TOOL_RESULT_FILES_LIVE_MAX_BYTES = 12 * 1024 * 1024
# A dropped-files note never lists more than this many files (a tool result
# carries one to three files in practice; the cap only bounds a pathological
# result, and the note says how many more are still stored).
_FILES_NOTE_MAX_ENTRIES = 20

# ── Always-on tool-result curation (context-rot guard) ────────────────────
# Tool results are the dominant driver of context bloat in heavy-tool-use
# runs. Even FAR below the model's context window, letting large tool results
# pile up dilutes the model's attention ("context rot") and degrades it into
# narrating its plan instead of executing it. Independently of the whole-
# session compaction budget (which only fires near the window limit), every
# iteration we keep only the most-recent tool-result BYTES up to a live budget
# and stub older, stale, re-fetchable results — Anthropic's "tool-result
# clearing" pattern. The budget is window-scaled with a floor (generous enough
# for the immediate working set on small models) and a cap (never unbounded on
# 1M-token models).
_TOOL_RESULT_LIVE_FRACTION = 0.12
_TOOL_RESULT_LIVE_MIN_CHARS = 48_000
_TOOL_RESULT_LIVE_MAX_CHARS = 160_000
# Minimum tool-result body size before it is worth stubbing / fingerprinting —
# small results are cheap and not worth curating.
_STALE_TOOL_RESULT_MIN_CHARS = 600

# Emergency pruning constants — used by _emergency_prune_session_context when
# the regular tool-result compaction fails to bring the context under budget.
_EMERGENCY_KEEP_MESSAGES = 10  # Minimum recent non-system messages to retain
_EMERGENCY_ASSISTANT_TRUNCATE_KEEP = 2_000  # Chars of each long assistant msg to preserve

# Phase 4 (dynamic floor) — when the most-recent _EMERGENCY_KEEP_MESSAGES
# non-system messages ALONE exceed the budget, Phase 3 cannot drop anything
# without going below its retention floor. Rather than give up (the
# empty-response loop), the remaining non-system messages are truncated IN
# PLACE — never dropped, so tool-call/tool-result coupling and the floor count
# survive — down to a per-message keep derived from the budget left after the
# system prompt, floored at this many chars so each turn keeps a usable tail.
_EMERGENCY_MIN_MESSAGE_KEEP = 160

# Phase 5 (system overflow, absolute last resort) — only fires when the system
# prompt ALONE still exceeds the budget after every conversational message has
# been truncated/evicted (e.g. a pathologically large injected agent-memory
# block: agent_memory ~34K chars). Oversized system messages are
# truncated head+tail so the persona (head) and the trailing security policy
# (tail) survive while the bulky middle is elided. System messages at or below
# this size are left untouched (transient in-loop nudges, small prompts).
_EMERGENCY_SYSTEM_TRUNCATE_MIN = 8_000
# A truncated system message is never shrunk below this many chars, so enough
# head+tail remains for the persona and the security policy even under extreme
# pressure.
_EMERGENCY_SYSTEM_MIN_KEEP = 6_000


def _compute_context_budget(context_window: int) -> tuple[int, int]:
    """Derive (max_session_chars, max_tool_result_chars) from a token-based context window."""
    max_session_chars = int(context_window * _SESSION_FRACTION * _CHARS_PER_TOKEN)
    max_tool_result_chars = min(
        int(context_window * _TOOL_RESULT_FRACTION * _CHARS_PER_TOKEN),
        _MAX_TOOL_RESULT_CHARS_CAP,
    )
    return max_session_chars, max_tool_result_chars


def _executor_withheld(executor: object) -> dict[str, str]:
    """The functions *executor* withholds from this turn (``CompositeToolExecutor.withhold``).

    Read off a scoped or bare composite alike; any other executor withholds
    nothing.
    """
    withheld = getattr(executor, "withheld", None)
    return dict(withheld) if isinstance(withheld, Mapping) else {}


def _executor_withheld_integrations(executor: object) -> dict[str, str]:
    """The integrations *executor* records as left out of this run, label -> reason
    (``CompositeToolExecutor.withhold_integrations``); any other executor: none."""
    withheld = getattr(executor, "withheld_integrations", None)
    return dict(withheld) if isinstance(withheld, Mapping) else {}


def _transcript_chars(messages: Iterable[ChatMessage]) -> int:
    """What *messages* weigh when they are sent: the one measure of every gate.

    :func:`app.llm.base.message_payload_chars` summed - each message's text
    plus the provider extras it replays (the thought parts and signatures of
    a Gemini tool turn). Never ``len(content)``: a transcript whose
    weight is its thoughts would reach no threshold, and the provider's own
    overflow check would be the only safeguard.
    """
    return sum(message_payload_chars(m) for m in messages)


# Buckets the context readout is broken down into. Grouped by what the user can
# DO about each: start a new chat (conversation), narrow the agent's tool set
# (tools, dynamic_tools), or nothing at all (system) — while the two the loop
# manages on their behalf (summary, tool_results) are what explain a long chat
# that seems to have forgotten things.
_CONTEXT_BUCKETS = (
    "system",
    "tools",
    "dynamic_tools",
    "summary",
    "conversation",
    "tool_results",
)

# Tool sources that are configured per agent rather than shipped with the
# platform — integrations, MCP servers and custom tools. Split out because they
# are the slice a user can actually shrink, and on a heavily-wired agent they
# dominate the schema payload.
_DYNAMIC_TOOL_SOURCE_PREFIXES = ("mcp:", "integration:", "custom:", "channel_bot:")


def _tool_definition_chars(
    tool: ToolDefinition,
    cache: dict[int, int] | None = None,
) -> int:
    """Approximate wire size of one tool definition.

    Each provider serialises tools its own way, so this is a JSON dump of the
    three fields they all carry. Consistent with every other figure in the
    readout being a char-derived estimate rather than a provider token count.

    ``cache`` memoizes that dump for the lifetime of one run. Without it the
    readout re-serialises every schema on every iteration — on the
    heavily-wired agent the ``dynamic_tools`` bucket exists for, that is tens
    of thousands of characters of JSON per tick, for a figure that cannot have
    changed. Keyed by ``id`` because ``ToolDefinition`` is frozen but carries a
    ``dict`` schema, so it is not hashable; the ids stay valid because the
    caller holds the tool list for the whole run, and frozen-ness means the
    fields cannot be swapped underneath a cached entry.
    """
    if cache is not None and (hit := cache.get(id(tool))) is not None:
        return hit
    try:
        payload = json.dumps(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            },
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        # A schema that will not serialise still occupies context; fall back to
        # the parts that always stringify rather than dropping the tool.
        size = len(tool.name or "") + len(tool.description or "")
    else:
        size = len(payload)
    if cache is not None:
        cache[id(tool)] = size
    return size


def _context_breakdown_chars(
    messages: list[ChatMessage],
    tools: list[ToolDefinition] | None,
    tool_chars_cache: dict[int, int] | None = None,
    sized: list[tuple[ChatMessage, str, int]] | None = None,
) -> dict[str, int]:
    """Split everything the next call will carry, by kind, in characters.

    ``summary`` covers both compactors that can put a digest in the context: the
    pre-loop conversation compaction (``[Conversation Compaction]``, written by
    ``conversation_memory``) and the in-loop semantic one
    (:data:`_SEMANTIC_COMPACTION_MARKER`). They are system messages, so without
    this they would read as an inexplicably large persona.

    Tool definitions ARE counted, even though the loop's own compaction gate
    still measures ``messages`` alone. They are genuinely sent with every call —
    on an agent with a large MCP tool set they are tens of thousands of tokens —
    so a readout that omitted them would under-report how full the window is,
    which is the one thing it exists to tell the truth about. For the same
    reason each message is weighed as the gate weighs it, provider extras
    included (``message_payload_chars``). *sized* is
    :func:`_sized_messages` of *messages* when the caller already has it.
    """
    out = dict.fromkeys(_CONTEXT_BUCKETS, 0)
    for _msg, bucket, size in sized if sized is not None else _sized_messages(messages):
        out[bucket] += size

    for tool in tools or []:
        out[_tool_bucket(tool)] += _tool_definition_chars(tool, tool_chars_cache)

    return out


@functools.cache
def _conversation_compaction_marker() -> str:
    from app.services.conversation_memory import _COMPACTION_MARKER

    return _COMPACTION_MARKER


def _message_bucket(msg: ChatMessage) -> str:
    """The readout bucket *msg* is counted in."""
    if msg.role == "system":
        content = msg.content or ""
        is_digest = content.startswith(_conversation_compaction_marker()) or content.startswith(
            _SEMANTIC_COMPACTION_MARKER
        )
        return "summary" if is_digest else "system"
    if msg.role == "tool":
        return "tool_results"
    return "conversation"


def _sized_messages(messages: Iterable[ChatMessage]) -> list[tuple[ChatMessage, str, int]]:
    """Each message that weighs anything, with its bucket and its weight.

    Measured once per reading and shared by the breakdown, its detail and the
    gate's measure, which all sum these weights.
    """
    sized = []
    for msg in messages:
        size = message_payload_chars(msg)
        if size:
            sized.append((msg, _message_bucket(msg), size))
    return sized


def _tool_bucket(tool: ToolDefinition) -> str:
    """The readout bucket *tool*'s definition is counted in."""
    source = (getattr(tool, "source", "") or "").lower()
    return "dynamic_tools" if source.startswith(_DYNAMIC_TOOL_SOURCE_PREFIXES) else "tools"


def _context_breakdown_tokens(
    messages: list[ChatMessage],
    tools: list[ToolDefinition] | None,
    tool_chars_cache: dict[int, int] | None = None,
    sized: list[tuple[ChatMessage, str, int]] | None = None,
) -> tuple[dict[str, int], int]:
    """Per-bucket token estimates plus their total.

    Returns figures that SUM EXACTLY to the total, because the readout shows
    both: per-bucket floor division would leave the lines short of the headline
    by up to one token per bucket, and a breakdown whose parts do not add up
    reads as a bug in the numbers rather than as rounding. The few leftover
    tokens land on the largest bucket, where they are proportionally invisible.

    Empty buckets are dropped — a legend line reading "0" is noise in a list
    whose whole job is to show where the context went.
    """
    per_bucket_chars = {
        k: v
        for k, v in _context_breakdown_chars(messages, tools, tool_chars_cache, sized).items()
        if v > 0
    }
    total_tokens = sum(per_bucket_chars.values()) // _CHARS_PER_TOKEN

    per_bucket = {k: v // _CHARS_PER_TOKEN for k, v in per_bucket_chars.items()}
    leftover = total_tokens - sum(per_bucket.values())
    if leftover > 0 and per_bucket:
        largest = max(per_bucket, key=lambda k: per_bucket_chars[k])
        per_bucket[largest] += leftover

    return {k: v for k, v in per_bucket.items() if v > 0}, total_tokens


# A reading's detail (``context_detail``, read by the chat's ``/context``
# command) keeps this many tool rows; the smallest beyond are folded into
# ``tools_omitted``, so a 1,000-tool agent does not store a 1,000-row reading on
# every reply. Tool-result rows are bounded the same way, names too.
_CONTEXT_DETAIL_MAX_TOOLS = 256
_CONTEXT_DETAIL_MAX_TOOL_RESULTS = 64
_CONTEXT_DETAIL_MAX_NAME_CHARS = 128


def _detail_name(name: str | None) -> str:
    return (name or "")[:_CONTEXT_DETAIL_MAX_NAME_CHARS]


def _context_detail(
    sized: list[tuple[ChatMessage, str, int]],
    tools: list[ToolDefinition] | None,
    buckets: Mapping[str, int],
    *,
    tool_chars_cache: dict[int, int] | None,
    system_sections: Mapping[str, int] | None,
    compaction: Mapping[str, int],
) -> dict[str, Any]:
    """What each bucket of a reading is made of, for the ``/context`` command.

    Every split sums exactly to the bucket it details: ``system`` to the
    ``system`` bucket, ``tools`` (with ``tools_omitted``) to ``tools`` plus
    ``dynamic_tools``, ``tool_results`` to ``tool_results``. ``system`` is
    split along the sections the caller measured as it assembled the prompt
    (*system_sections*, characters per label); what no section covers is
    ``platform``. Without sections it is left out.

    ``compaction`` carries the gate's own figures (its measure, trigger and
    target, in tokens), so a reader shows the thresholds the loop acts on
    rather than re-deriving them.

    *sized* is :func:`_sized_messages` of the transcript the reading measured.

    Names only and sizes: never a tool's description or a section's text.
    """
    detail: dict[str, Any] = {"compaction": dict(compaction)}

    if system_sections and buckets.get("system"):
        system_chars = sum(size for _msg, bucket, size in sized if bucket == "system")
        weights = {label: chars for label, chars in system_sections.items() if chars > 0}
        weights["platform"] = max(system_chars - sum(weights.values()), 0)
        detail["system"] = split_tokens(buckets["system"], weights)

    rows: list[tuple[int, ToolDefinition, str]] = [
        (index, tool, _tool_bucket(tool)) for index, tool in enumerate(tools or [])
    ]
    tool_tokens: dict[int, int] = {}
    for bucket in ("tools", "dynamic_tools"):
        tool_tokens.update(
            split_tokens(
                buckets.get(bucket, 0),
                {
                    index: _tool_definition_chars(tool, tool_chars_cache)
                    for index, tool, tool_bucket in rows
                    if tool_bucket == bucket
                },
            )
        )
    if tool_tokens:
        ranked = sorted(rows, key=lambda row: tool_tokens.get(row[0], 0), reverse=True)
        kept, omitted = (
            ranked[:_CONTEXT_DETAIL_MAX_TOOLS],
            ranked[_CONTEXT_DETAIL_MAX_TOOLS:],
        )
        tool_rows = []
        for index, tool, _bucket in kept:
            if not tool_tokens.get(index):
                continue
            group = tool_group(tool)
            source = (tool.source or "").lower()
            tool_rows.append(
                {
                    "name": _detail_name(tool.name),
                    "kind": source.partition(":")[0] if group else "builtin",
                    "group": _detail_name(group[1]) if group else None,
                    "tokens": tool_tokens[index],
                }
            )
        detail["tools"] = tool_rows
        omitted_tokens = sum(tool_tokens.get(index, 0) for index, _tool, _bucket in omitted)
        if omitted_tokens:
            detail["tools_omitted"] = {"count": len(omitted), "tokens": omitted_tokens}

    if buckets.get("tool_results"):
        result_chars: dict[str, int] = {}
        for msg, bucket, size in sized:
            if bucket == "tool_results":
                name = _detail_name(msg.name) or "unknown"
                result_chars[name] = result_chars.get(name, 0) + size
        by_name = split_tokens(buckets["tool_results"], result_chars)
        ranked_names = sorted(by_name, key=by_name.__getitem__, reverse=True)
        results = {name: by_name[name] for name in ranked_names[:_CONTEXT_DETAIL_MAX_TOOL_RESULTS]}
        rest = sum(by_name[name] for name in ranked_names[_CONTEXT_DETAIL_MAX_TOOL_RESULTS:])
        if rest:
            results["other"] = results.get("other", 0) + rest
        detail["tool_results"] = results

    return detail


def _resolve_effective_max_tokens(llm: BaseLLMProvider, model: str | None) -> int:
    """Resolve a call's ``max_tokens`` via the shared model-limit chain.

    Delegates to :func:`app.llm.model_limits.resolve_effective_max_output_tokens`
    (admin override -> metadata snapshot -> model-family floor -> provider flat
    default -> hard fallback) so the agentic loop and the failover clamp agree on
    every model, and a model released after the bundled metadata snapshot (e.g.
    ``claude-opus-4-8``) never silently regresses to a stale provider default.

    When the caller passes ``model=None`` (common — providers default
    internally to ``llm.default_model``), resolve against the provider's
    default model so per-model overrides / snapshot / family limits still
    apply to the effective model instead of falling straight through to the
    provider flat default. Mirrors the failover clamp
    (:meth:`FailoverLLMProvider._resolve_max_tokens_limit`).
    """
    from app.llm.model_limits import resolve_effective_max_output_tokens

    resolved_model = model or getattr(llm, "default_model", None)
    return resolve_effective_max_output_tokens(
        model=resolved_model,
        overrides=getattr(llm, "max_output_tokens_overrides", None),
        provider_default=getattr(llm, "max_output_tokens", None),
    )


# ---------------------------------------------------------------------------
# Tool count cap
#
# Each LLM provider declares its own ``max_tools`` limit on
# ``BaseLLMProvider``.  Typical values:
#
#   - OpenAI / Azure / Mistral / OpenRouter / LiteLLM / Custom: 128
#   - Ollama (local models): 64
#   - Bedrock (AWS Converse API): 256
#   - Gemini: 512
#   - Anthropic: 1024
#
# The cap bounds how many schemas ONE request carries.  With progressive
# tool disclosure active (``app.tools.tool_gating``) it is enforced on the
# visible list — always-on core + ``load_tools`` + revealed groups — and the
# agent's inventory stays whole: every group remains loadable, the gate
# unloads the least recently used group(s) to make room.  Only when
# gating is off does the whole list have to fit, and then it is trimmed by
# ``tool_retention_key``, i.e. by source:
#   1. builtin tools   (search_knowledge, memory, handover, etc.)
#   2. custom tools     (user-defined JS functions)
#   3. MCP tools        (external tool servers)
#   4. integration tools (auto-generated from connected integrations)
#
# ``_DEFAULT_MAX_TOOLS`` is only used as a fallback when the provider
# instance is unavailable (should not happen in practice).
# ---------------------------------------------------------------------------
_DEFAULT_MAX_TOOLS = 128

# ---------------------------------------------------------------------------
# Wind-down & forced-final-answer prompts
#
# These are injected as system messages to guide the LLM toward wrapping up
# when the iteration budget is nearly exhausted.
# ---------------------------------------------------------------------------
def _build_wind_down_nudge(*, remaining_iterations: int, max_iterations: int) -> str:
    """Build the wind-down system message with an explicit iteration budget.

    Stated budget numbers help the LLM make a concrete plan ("only N
    iteration(s) left, including this one") instead of vaguely
    "approaching the cap".  Empirically this produces tighter
    completion summaries that report ALL outcomes rather than the
    open-ended "I'm running out of room" responses the static prompt
    produced.

    The "(s)" suffix on ``iteration(s)`` is intentional — a future
    refactor that pluralises conditionally would just add complexity
    for a single-character UX win.
    """
    return (
        "[SYSTEM — iteration budget warning] "
        "You are approaching the maximum number of tool-calling iterations "
        f"({remaining_iterations} iteration(s) left out of {max_iterations}, including this one). "
        "You MUST reserve enough response budget for a final completion report. "
        "Do NOT start new exploratory tool chains. "
        "If needed, run only the most critical remaining tool call(s), then stop and report. "
        "Your answer MUST summarise everything you accomplished, every action you took, "
        "every result you obtained, and what remains pending."
    )

_LAST_CHANCE_PROMPT = (
    "[SYSTEM — tools disabled, final iteration] "
    "All tools have been removed. You cannot make any more tool calls. "
    "You MUST provide your final, comprehensive answer RIGHT NOW using "
    "ONLY the information you have already gathered from previous tool calls. "
    "Synthesise all collected data into a well-structured, detailed response. "
    "Your response MUST include: (1) a summary of ALL actions you took and their "
    "outcomes, (2) specific IDs, names, or references for any resources you "
    "created, modified, or retrieved, (3) the current status of the task. "
    "Do NOT start a new work plan or announce future steps — report on what "
    "you have ALREADY accomplished."
)

_FORCED_FINAL_ANSWER = (
    "[SYSTEM — iteration budget fully exhausted, tools disabled] "
    "You have exhausted all available iterations. This is your absolute LAST "
    "chance to respond. You MUST provide a comprehensive, well-structured "
    "SUMMARY RIGHT NOW of ALL actions taken and results obtained during this "
    "session. Review every tool call and result above. Your response MUST: "
    "(1) List every concrete action you completed (resources created, data "
    "retrieved, analyses performed). (2) Include specific IDs, names, and "
    "references. (3) Note anything that remains incomplete. "
    "Do NOT apologise, do NOT announce future plans, do NOT start new work — "
    "just report what was ACCOMPLISHED."
)

_FORCED_ANSWER_RETRY = (
    "[SYSTEM — response too brief, retry required] "
    "Your previous response was far too short and not useful. "
    "You MUST write a detailed, substantive summary. Look at ALL the tool "
    "calls and results in this conversation and compile them into a thorough, "
    "well-organised report of at least several paragraphs. Include: "
    "every action taken, every resource created/modified (with IDs and names), "
    "every piece of data retrieved, and the overall status of the task."
)

_EMPTY_RESPONSE_RETRY = (
    "[SYSTEM — empty response recovery] "
    "Your previous response was empty. You MUST respond to the user's message. "
    "Read the conversation above carefully and provide a helpful, substantive "
    "answer. If the conversation is long, focus on the most recent user message "
    "and provide a direct response."
)

# Firmer nudge used once the no-tools retry ALSO comes back empty. Rather than
# bailing straight to the forced-final-answer backstop at iteration 2,
# we escalate and keep iterating — but only up to
# ``_MAX_CONSECUTIVE_EMPTY_RESPONSES`` consecutive double-empty rounds (a round
# = both the primary turn AND its no-tools retry empty), so a systematic
# empty-output condition (e.g. a transient provider blip — two
# parallel runs can fail identically) still reaches the backstop promptly instead
# of burning the whole iteration budget on empty calls.
_EMPTY_RESPONSE_RETRY_ESCALATION = (
    "[SYSTEM — repeated empty responses] "
    "Your last responses were completely empty, which is not acceptable. You "
    "MUST now produce a final, user-facing answer in plain text — do NOT call "
    "any tools. Using the conversation above, summarise what you have done and "
    "the result of the task. Write at least a short paragraph."
)

# Cap on consecutive "double-empty" rounds before falling through to the
# forced-final-answer backstop. A round counts as empty only when BOTH the
# primary turn AND its no-tools retry come back content-less and tool-call-less;
# each such round increments the counter by one, and any genuine tool-calling
# turn resets it to zero. Once the counter reaches this cap the loop stops
# escalating and runs the one-shot forced-final-answer backstop.
_MAX_CONSECUTIVE_EMPTY_RESPONSES = 3

# Large-context empty-response recovery.
#
# Some providers return empty content with no tool calls on big conversations
# (~80k+ chars) even though they're well under the advertised token window.
# The regular ``_prune_session_context`` only compresses *tool* results, so a
# chat dominated by long user/assistant turns is never reduced — the no-tools
# retry (and the forced-final-answer backstop) then re-send the same oversized
# prompt that produced the empty output, looping straight into the generic
# fallback. When the context is this large we escalate to
# ``_emergency_prune_session_context`` (which truncates assistant turns and
# evicts old message groups) with a target that ratchets DOWN on each
# consecutive empty round so a persistently-empty context shrinks fast.
_EMPTY_RESPONSE_RECOVERY_TRIGGER_CHARS = 60_000
_EMPTY_RESPONSE_RECOVERY_MIN_CHARS = 12_000
_EMPTY_RESPONSE_RECOVERY_TARGET_FRACTIONS = (0.6, 0.4, 0.25)

# ── Semantic in-loop compaction (best-in-class, signal-driven) ───────────
# This mirrors the "auto-compact" stage of Claude Code's compaction pipeline
# and Anthropic's server-side Compaction API, made provider-agnostic so it
# works for every LLM the platform supports (OpenAI, Gemini, Bedrock, …).
#
# When the working context grows large, the OLDEST turns are replaced with a
# single high-fidelity LLM-generated SUMMARY while the original task and the
# most recent turns are kept VERBATIM and tool-call/tool-result coupling is
# preserved.  Unlike brute truncation/eviction (kept only as the deterministic
# fallback), this is information-preserving — it distils the history instead of
# deleting it.
#
# Critically it is **signal-driven, never threshold-paranoid**:
#   * It only ever summarises *older* turns — the original task and the recent
#     window are always retained, so a single legitimately-long prompt is never
#     gutted (a context with no old turns to summarise is left untouched).
#   * It is invoked by REAL signals — the model returning an empty turn (the
#     model itself signalling it has lost focus on an oversized history) and the
#     proactive near-window-limit guard — not by a fixed char threshold that
#     would needlessly compact ordinary long conversations.
_SEMANTIC_COMPACTION_KEEP_RECENT_MESSAGES = 8
_SEMANTIC_COMPACTION_MIN_DROP_CHARS = 8_000
_SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS = 1_500  # per-message cap in the summary transcript
_SEMANTIC_COMPACTION_MAX_TOKENS = 1_536
_SEMANTIC_COMPACTION_MARKER = "[Compacted earlier work]"
# Cap how many tool names are spelled out in an assistant turn's transcript
# label — a single turn can fan out into dozens of tool calls, and an unbounded
# ", ".join(...) would balloon the summariser prompt for no added signal.
_SEMANTIC_COMPACTION_MAX_TOOL_NAMES = 5
# Identifiers of an elided tool result (#2382): the head+tail excerpt drops the
# middle of a long listing, so the identifier-bearing lines of that middle are
# passed to the summariser as a compact verbatim table, bounded per result and
# per compaction. The summary budget grows with the tables it must carry.
_SEMANTIC_COMPACTION_ID_TABLE_RESULT_CHARS = 4_000
_SEMANTIC_COMPACTION_ID_TABLE_TOTAL_CHARS = 20_000
_SEMANTIC_COMPACTION_ID_TABLE_MAX_TOKENS = 6_144
_SEMANTIC_COMPACTION_ID_ENTRY_CONTEXT_CHARS = 100
_SEMANTIC_COMPACTION_RERUN_HINTS = 20
_SEMANTIC_COMPACTION_RERUN_ARGS_CHARS = 300
# Name and observation kind of the summariser call's generation.
_COMPACTION_GENERATION = "context_compaction"

# Minimum character threshold for forced answers — below this we retry
_MIN_FORCED_ANSWER_CHARS = 100

# Generation names of the forced-answer synthesis and its stronger-prompt retry.
# The backstop runs after the iteration spans closed, so its calls are
# generations of the run's own span; the name tells them from an in-loop call.
_FORCED_ANSWER_GENERATION = "forced_final_answer"
_FORCED_ANSWER_RETRY_GENERATION = "forced_final_answer:retry"

# Wall-clock deadline awareness for the forced-final-answer backstop.
# When the outer assignment engine wraps run_agent_loop with asyncio.wait_for,
# each forced-answer LLM call must use the remaining budget (not the full 900s
# _LLM_CALL_TIMEOUT) so the outer deadline never fires during synthesis and
# leaves the run with no output at all.
#
# _FORCED_ANSWER_MIN_REMAINING_SECS: if fewer seconds remain, skip the LLM call
# entirely and return an immediate fallback so the outer wait_for can never
# cancel a partial synthesis mid-flight.
#
# _FORCED_ANSWER_DEADLINE_RESERVE_SECS: safety buffer subtracted from the
# remaining time when computing the per-call timeout, leaving room for the
# function to return and the caller to process the result before the deadline.
_FORCED_ANSWER_MIN_REMAINING_SECS: int = 10
_FORCED_ANSWER_DEADLINE_RESERVE_SECS: int = 5

# Maximum number of auto-continuation attempts when the LLM hits max_tokens
# mid-response.  Capped to prevent infinite loops.
_MAX_CONTINUATIONS = 3

_CONTINUATION_PROMPT = (
    "Continue from where you left off. First provide a short checkpoint "
    "of what is already done and what remains pending, then proceed. "
    "Do not repeat what you already said."
)

# ── Stall guard: "execute, don't narrate" ─────────────────────────────────
# A long-horizon failure mode: mid-task, the model emits a "checkpoint" that
# claims it is executing RIGHT NOW ("executing now", "Pending (executing now):
# ...") but calls ZERO tools, so the loop returns the narration as a final
# answer. When tools and budget are still available we re-prompt it once to act.
#
# Deliberately gentle: a SINGLE nudge, then the model's decision to stop is
# respected. Three re-prompts (the original) was too aggressive — under a
# degraded context it just burned iterations re-prompting a model that wanted
# to checkpoint and hand back. Combined with the intentionally NARROW
# ``_ACTION_INTENT_RE`` (only blatant present-tense execution claims trip it),
# this keeps the guard a light safety net, not a behavioural straitjacket.
_MAX_STALL_CONTINUATIONS = 1

_STALL_CONTINUATION_NUDGE = (
    "[SYSTEM — you described an action but did not perform it] "
    "Your previous message announced work you were about to do (e.g. "
    '"let me run...", "executing now", "next I will...") but you called NO '
    "tools, so nothing actually happened. Do NOT narrate the plan again and "
    "do NOT send another checkpoint. If the task is incomplete, CALL THE "
    "REQUIRED TOOLS NOW to perform the work. Prefer bulk/batch tool calls and "
    "process data in chunks so you finish without re-fetching large datasets. "
    "Reply with plain text (no tools) ONLY if you are genuinely finished, are "
    "blocked, or need a decision from the user — in which case state that "
    "explicitly."
)

# DELIBERATELY NARROW markers of a tool-free answer that claims execution is
# happening RIGHT NOW yet emitted no tool call — matched against the TAIL of the
# message. This is intentionally relaxed (high precision, low recall): only an
# explicit present-tense "I'm doing it now" claim trips the guard. Plain plans
# ("let me pull the data", "next I'll write the records", "let me confirm ...")
# are NOT stalls — describing a next step is normal and must be allowed to end
# the turn. The guard only catches the blatant "executing now / pending
# (executing / writing the records NOW" with-no-tool-call pattern.
_ACTION_INTENT_RE = re.compile(
    r"(?:"
    # Literal present-tense execution claims.
    r"executing now"
    r"|execut(?:e|ing) the (?:writes?|assignments?|batch(?:es)?|plan|"
    r"operations?|updates?)"
    r"|pending \(executing"
    # Present-progressive execution verb explicitly tied to "now".
    r"|\b(?:running|writing|assigning|applying|submitting|executing|processing)"
    r"\b[^.!?\n]{0,30}\bnow\b"
    # An imperative/first-person plan ONLY when it explicitly says "now".
    r"|\b(?:let me|i'?ll|i will|i'?m going to)\b[^.!?\n]{0,40}"
    r"\b(?:run|execute|write|assign|apply|submit|fetch|pull|load)\b"
    r"[^.!?\n]{0,40}\bnow\b"
    r")",
    re.IGNORECASE,
)

# Veto markers: the message is clearly handing control back to the user (a
# question / permission request / waiting state). When present we do NOT force
# continuation — overriding a genuine question would be worse than stopping.
# Exception: "waiting for" with still-running spawned tasks in this turn is
# a stall, not a handback (the UI still shows those tasks as running).
_HANDBACK_RE = re.compile(
    r"(would you like|do you want|should i\b|shall i\b|let me know|which "
    r"(one|option|of)|your call|up to you|please confirm|please advise|"
    r"waiting for|may i\b|do you (want|need|prefer))",
    re.IGNORECASE,
)
_WAITING_NARRATION_RE = re.compile(r"\bwaiting for\b", re.IGNORECASE)

_SPAWN_BACKGROUND_TOOL_NAME = "spawn_background_task"
# The ``spawn_background_task`` tool result is a fixed one-field-per-line block
# (``builtin_tools/handover.py``): ``Task ID: <uuid>`` / ``Target agent:
# <name>`` / ``Status: <status>`` / a closing hint. Both regexes below read it
# back and both anchor on LINE STARTS: the agent name is the only free-text
# field in the block and the source flattens it to one line, so in anything
# it produced a label at column 0 is the block's own — a ``Task ID:`` inside a
# name cannot register a bogus task with the liveness probe.
_SPAWN_TASK_ID_RE = re.compile(
    r"^Task ID:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE | re.MULTILINE,
)
# ``Target agent: <name>`` — the second half of the label the park message
# names a task by. Transcripts persist tool results produced before
# the source flattened the name, so the parser still has to read a multi-line
# value whole rather than stop at the first newline (``Research\nAnalyst`` as
# ``Research`` — with the description present, a label the fallback considers
# complete and never repairs). The value runs to the LAST ``Status:`` line of
# the block: in the fixed field order nothing free-text follows ``Status:``,
# so the last such line is the block's own, whatever lines the name holds
# (``Research\nStatus: Analyst`` reads whole). Anchoring on a known status
# token instead would be defeated by the very same name — and the vocabulary
# is a plain string column, not a closed set. The first alternative is greedy
# for exactly that reason; the second covers a block that ends without a
# ``Status:`` line. Captured without surrounding whitespace, so an empty
# value yields "" (no agent), never the line after it. ``TaskLabel.render``
# flattens the whitespace.
_SPAWN_TARGET_AGENT_RE = re.compile(
    r"^Target agent:[ \t]*(?:(.*)\s*(?=^Status:)|(.*?)\s*\Z)",
    re.MULTILINE | re.DOTALL,
)
# ``spawn_background_task`` labels a parallel copy of the calling agent this
# way. It is model-facing wording ("yourself") that means nothing to the user
# reading the park message, so such a task is named by its description alone.
_SELF_COPY_TARGET_LABEL = "yourself (parallel copy)"
# Single-sourced in ``wait_policy`` so the implicit-wait liveness probe and the
# ``wait`` primitive's partial-wake detection agree on what "finished" means.
_TASK_TERMINAL_STATUSES = WAIT_TERMINAL_STATUSES


def _looks_like_unfinished_action(
    text: str, *, open_task_ids: list[str] | None = None
) -> bool:
    """True when a tool-free answer is actually an unfulfilled promise to act.

    Conservative by construction: a direct question to the user, an explicit
    permission/handback phrase, or the absence of a forward-action marker all
    return ``False`` so genuine completions / questions are never overridden.
    Only the message TAIL is inspected — that is where a "now I'll do X" promise
    lives, and it avoids matching an action verb buried in a recap of past work.

    ``open_task_ids``: spawned tasks from this turn that are still running.
    A "waiting for" narration is then a stall, not a handback.
    """
    if not text:
        return False
    tail = text[-600:]
    if open_task_ids and _WAITING_NARRATION_RE.search(tail):
        return True
    if tail.rstrip().endswith("?"):
        return False
    if _HANDBACK_RE.search(tail):
        return False
    return bool(_ACTION_INTENT_RE.search(tail))


# ── Degenerate-repetition guard: "stop looping the same text" ──────────────
# A distinct failure mode from the stall above: the model gets stuck and emits
# the SAME block of text over and over as a tool-free "answer" — hundreds of
# identical lines, tens of KB — with no tool call. Surfacing it floods the
# transcript / run result with noise and the owner sees only garbage. We CUT it
# (replace the content with a bounded diagnostic) rather than re-prompting: a
# model looping this way rarely recovers cheaply, so bounding the damage and
# letting the run end — the assignment's completion notification then carries a
# clear cause — is safer than another expensive round. Detection is high
# precision: it only fires on a long answer that is MOSTLY one block looped
# verbatim, which a genuine answer (even a long table or list) never is.
_DEGENERATE_REPETITION_MIN_CHARS = 1500
_DEGENERATE_REPETITION_MIN_LINES = 12
_DEGENERATE_REPETITION_MAX_PERIOD = 20
# A degenerate run is flagged only when a whole block repeats VERBATIM at least
# this many times back to back (the run of consecutive matches reaches
# ``period * (MIN_BLOCK_REPEATS - 1)``).
_DEGENERATE_REPETITION_MIN_BLOCK_REPEATS = 4
# The repeated block must be a real multi-line paragraph, not one flooded line
# dressed as a block, so it must hold at least this many DISTINCT lines.
_DEGENERATE_REPETITION_MIN_DISTINCT_BLOCK = 2
# The looped run must be most of the answer (share of its characters): a runaway
# keeps looping until the token budget ends it, while a genuine answer that
# quotes a few identical lines (log evidence, a template, a rota, signature
# lines) after pages of prose is mostly NOT the repeat.
_DEGENERATE_REPETITION_MIN_COVERAGE = 0.7
# While the answer streams, the detector re-runs on the shown text every time it
# grows by this many characters, so a runaway stops reaching the screen early.
_DEGENERATE_REPETITION_STREAM_CHECK_CHARS = 2000

_DEGENERATE_REPETITION_NOTICE = (
    "The run was stopped: the model entered a degenerate repetition loop, "
    "emitting the same text repeatedly without calling any tool, so no real "
    "progress was made. The runaway output was discarded. Re-run the task; if "
    "it recurs, the request may need to be split into smaller steps."
)

# The stop reason of a run the guard ended on the notice: not a finished task
# (``UNFINISHED_STOP_REASONS``), so a trace or a run page can tell it apart.
DEGENERATE_REPETITION_STOP_REASON = "degenerate_repetition"


def _lines_outside_closed_code_fences(lines: list[str]) -> list[str]:
    """``lines`` without the fenced code blocks that are closed.

    Code legitimately repeats (an empty ASCII grid, an unrolled snippet, a
    config template), so it never counts as a repetition loop. An UNCLOSED
    fence is kept: a runaway that opened one never closes it, and dropping
    everything after it would hide the loop.
    """
    kept: list[str] = []
    fenced: list[str] | None = None
    for line in lines:
        if line.startswith(("```", "~~~")):
            # An opening fence starts a block; a closing one drops it.
            fenced = [line] if fenced is None else None
            continue
        if fenced is not None:
            fenced.append(line)
        else:
            kept.append(line)
    if fenced:
        kept.extend(fenced)
    return kept


def _looks_like_degenerate_repetition(text: str) -> bool:
    """True when a tool-free answer is MOSTLY a runaway loop of a repeated MULTI-LINE block.

    High precision by construction, tuned to never flag a legitimate answer:

    * Only a long message (``>= MIN_CHARS`` over ``>= MIN_LINES`` non-empty
      lines) is considered. The analysis runs on EVERY non-empty line outside
      closed code fences, short ones included: a line that varies between
      otherwise identical blocks (a short ``### web-01`` heading, a
      ``"id": 7,`` field) must stay in the sequence so it breaks the run, which
      is exactly what keeps a legitimate per-item answer from being flagged.
      Fenced code never counts: code repeats legitimately.
    * The signal is a block emitted VERBATIM back to back: for some period
      ``>= 2`` there is a run of consecutive lines, at least
      ``period * (MIN_BLOCK_REPEATS - 1)`` long, each equal to the line
      ``period`` positions ahead — i.e. the same whole block repeated
      ``>= MIN_BLOCK_REPEATS`` times with nothing varying between repeats — and
      that block holds ``>= MIN_DISTINCT_BLOCK`` distinct lines.
    * The looped run covers ``>= MIN_COVERAGE`` of the answer's characters. A
      runaway loops until the token budget ends it, so the loop is most of the
      text; a genuine answer that quotes a few repeated lines (log evidence, a
      YAML template, a rota table, signature lines) after its prose is not.

    Those requirements are what separate a degenerate loop (a paragraph of
    several DIFFERENT lines repeated verbatim: ``A, B, C, A, B, C, …``) from
    legitimate repetition, which this never flags:

    * A Markdown table of identical rows, or an answer complying with "repeat
      this sentence N times": one value floods, so the block has a single
      distinct line (``< MIN_DISTINCT_BLOCK``).
    * A per-item list (remediation per host, records in a dump) whose shared
      lines repeat while a heading or field varies: the varying line — even a
      short one — breaks the block every period, so no run ever reaches a whole
      repeated block.
    * A short repeated passage inside a longer answer: below ``MIN_COVERAGE``.

    A single repeated line, however long, is deliberately NOT flagged: a real
    runaway loops a multi-line block, and refusing single-line repeats keeps
    legitimate identical-row output safe.
    """
    if not text or len(text) < _DEGENERATE_REPETITION_MIN_CHARS:
        return False
    all_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    total_chars = sum(len(ln) for ln in all_lines)
    lines = _lines_outside_closed_code_fences(all_lines)
    m = len(lines)
    if m < _DEGENERATE_REPETITION_MIN_LINES or not total_chars:
        return False
    max_period = min(
        m // _DEGENERATE_REPETITION_MIN_BLOCK_REPEATS,
        _DEGENERATE_REPETITION_MAX_PERIOD,
    )
    for period in range(2, max_period + 1):
        needed_run = period * (_DEGENERATE_REPETITION_MIN_BLOCK_REPEATS - 1)
        # Longest run of consecutive positions where the block repeats verbatim.
        run = 0
        best = 0
        best_end = -1
        for i in range(m - period):
            if lines[i] == lines[i + period]:
                run += 1
                if run > best:
                    best = run
                    best_end = i
            else:
                run = 0
        if best < needed_run:
            continue
        # The repeated block must be a real paragraph, not one flooded line.
        block_start = best_end - best + 1
        block = lines[block_start : block_start + period]
        if len(set(block)) < _DEGENERATE_REPETITION_MIN_DISTINCT_BLOCK:
            continue
        # ...and the loop must be most of the answer, not a quoted passage.
        looped_chars = sum(len(ln) for ln in lines[block_start : best_end + period + 1])
        if looped_chars >= _DEGENERATE_REPETITION_MIN_COVERAGE * total_chars:
            return True
    return False


class _AnswerStreamCut:
    """Streams answer text to ``on_token`` until it turns into a degenerate
    repetition loop, then shows nothing more.

    Shown answer text is never taken back (invariant 14), so a runaway must stop
    reaching the screen while it streams: the detector re-runs on the shown text
    every ``_DEGENERATE_REPETITION_STREAM_CHECK_CHARS``. One instance spans an
    answer and its max_tokens continuations.

    The cut also sets ``stopped``, which the call streaming through it races
    (:func:`_race_cancellation`, :func:`_streaming_chat`, given it as
    ``answer_cut``): the call is aborted with :class:`_AnswerCutError` instead
    of generating up to its ``max_tokens``. An aborted call is recorded with
    what its usage sink (``bind_call``) holds, so it is aborted only once a
    report made after the cut counts more output than at the cut: that report
    covers everything shown. A provider that reports its output only at the
    end (Anthropic, Bedrock, OpenAI-compatible) streams on, unshown, and its
    call is counted whole.
    """

    def __init__(self, on_token: OnTokenCallback, *, enabled: bool) -> None:
        self._on_token = on_token
        self._enabled = enabled
        self._shown: list[str] = []
        self._shown_len = self._checked_len = 0
        self.cut = False
        self.stopped = asyncio.Event()
        self._usage_sink: StreamUsageSink | None = None
        self._output_at_cut = 0

    def bind_call(self, usage_sink: StreamUsageSink | None) -> None:
        """The call now streaming through this cut reports its usage into *usage_sink*."""
        self._usage_sink = usage_sink
        self._output_at_cut = self._reported_output()

    def _reported_output(self) -> int:
        if self._usage_sink is None:
            return 0
        return input_output_tokens(self._usage_sink.reported_usage)[1]

    def _stop_when_counted(self) -> None:
        """Stop the cut call once its sink counts output reported past the cut."""
        if self.cut and self._reported_output() > self._output_at_cut:
            self.stopped.set()

    @property
    def shown_text(self) -> str:
        return "".join(self._shown)

    async def show(self, text: str) -> None:
        if self.cut:
            self._stop_when_counted()
            return
        await _stream_token(self._on_token, text)
        self._shown.append(text)
        self._shown_len += len(text)
        if (
            self._enabled
            and self._shown_len - self._checked_len >= _DEGENERATE_REPETITION_STREAM_CHECK_CHARS
        ):
            self._checked_len = self._shown_len
            if _looks_like_degenerate_repetition(self.shown_text):
                self.cut = True
                self._output_at_cut = self._reported_output()
                logger.warning(
                    "Agent loop: streamed answer is a degenerate repetition loop "
                    "after %d chars; no further text is shown",
                    self._shown_len,
                )

    def reset(self) -> None:
        """The answer restarts from the beginning (provider failover)."""
        self._shown.clear()
        self._shown_len = self._checked_len = 0
        self.cut = False
        self.stopped.clear()

    def aborted_response(self, *, model: str | None, usage: dict) -> ChatResponse:
        """The answer of a call this cut aborted: the text shown, no tool call."""
        from app.llm.base import ChatResponse

        return ChatResponse(
            content=self.shown_text,
            model=model or "unknown",
            usage=usage,
            stop_reason="end_turn",
        )

    def on_restart(self, retract: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """``on_stream_reset`` of a stream answering again from the start:
        ``retract`` takes back what was shown, then the cut starts over."""

        async def _restart() -> None:
            await retract()
            self.reset()

        return _restart


def _is_degenerate_answer(text: str, stream: _AnswerStreamCut | None = None) -> bool:
    """Whether an answer is a runaway loop: cut while it streamed, or detected whole."""
    return (stream is not None and stream.cut) or _looks_like_degenerate_repetition(text)


async def _end_runaway(
    response: ChatResponse,
    stream: _AnswerStreamCut | None,
    on_token: OnTokenCallback | None,
    *,
    shown: bool,
) -> None:
    """End a runaway answer on the notice, every path through this one exit.

    Shown text is never taken back (invariant 14): a ``shown`` answer is what
    was shown, and the notice streams to ``on_token`` as its tail. An answer
    not shown is replaced by the notice.
    """
    response.stop_reason = DEGENERATE_REPETITION_STOP_REASON
    if not shown:
        response.content = _DEGENERATE_REPETITION_NOTICE
        return
    if stream is not None and stream.cut:
        response.content = stream.shown_text
    tail = f"\n\n{_DEGENERATE_REPETITION_NOTICE}"
    await _stream_token(on_token, tail)
    response.content = (response.content or "") + tail


# ── Leaked tool-call guard: "invoke it, don't write it as text" ───────────
# A distinct failure mode from the narration stall above: the model MEANS to
# call a tool but emits the call's markup as literal TEXT instead of a
# structured tool call, so nothing runs and the turn stalls on a "final" answer
# that is really a dropped call. Recognising and PARSING that markup is a vendor
# concern (it is Anthropic-specific), so it lives in the provider adapter
# (``app.llm.tool_markup_recovery``): a parseable leak is turned back into a
# real tool call there and reaches this loop as an ordinary ``tool_use``, while
# a leak the adapter cannot parse sets the generic
# ``ChatResponse.leaked_tool_markup_unrecovered`` flag. This loop keys its
# bounded re-prompt on that flag alone — it holds no vendor markup knowledge.
_MAX_LEAKED_TOOL_CALL_RECOVERIES = 2

_LEAKED_TOOL_CALL_NUDGE = (
    "[SYSTEM — you wrote a tool call as text instead of invoking it] "
    "Your previous message contained tool-call markup (the function_calls / "
    "invoke XML wrapper tags) written as literal text, so NO tool actually ran "
    "and nothing happened. Do NOT write tool-call markup in your message text. "
    "If the task is incomplete, make the tool call for real now using the "
    "proper tool-calling mechanism. Reply with plain text (no tools) ONLY if "
    "you are genuinely finished, are blocked, or need a decision from the user."
)


# ── Steering resume-promise guard ─────────────────────────────────────────
# Mid-run steering failure mode: the user steers the run ("BlackRock is
# validated"), the model handles the steer, then ends the turn with a
# TEXT-ONLY answer whose closing PROMISES to resume the pre-steer task
# ("Resuming the ComCyberMI draft now..."). The loop's unconditional
# text-only exit then ends the turn — the promise is the last message and
# the original task is silently abandoned. When the run was steered, tools
# are still offered and budget remains, we re-prompt ONCE: either actually
# do the remaining work (tool calls) or close without promising imminent
# work. The ``resume_promise_nudged`` one-shot guard caps this at a single
# extra LLM round per run — if the model answers text-only again, that
# answer is final even if it still contains a promise.
_RESUME_PROMISE_NUDGE = (
    "[SYSTEM — you promised to resume work but ended the turn] "
    "You ended your reply by telling the user you would resume or continue "
    "work (the unfinished task from earlier in this turn). Do not end the "
    "turn with a promise. Either actually perform the remaining work now "
    "(make the tool calls), or — if there is genuinely nothing left to do "
    "or you need the user's input to proceed — restate your closing "
    "without promising imminent work."
)

# DELIBERATELY CONSERVATIVE markers of a closing that promises to resume or
# continue earlier work — matched against the FINAL SENTENCE of the message
# only (see :func:`_ends_with_resume_promise`), which is where a sign-off
# promise lives. High precision over recall: only explicit first-person /
# present-progressive resume phrasings trip the guard, and a promise phrase
# in an EARLIER sentence ("Getting back to the draft, I completed every
# section. The deliverable is attached.") never does — the closing sentence
# of a completed recap is a completion statement, not a promise.
# Questions ("want me to resume?") are vetoed by the trailing-"?" check in
# :func:`_ends_with_resume_promise`, and "get(ting) back to you" (a reply
# promise, not a task-resume promise) is excluded via a negative lookahead.
_RESUME_PROMISE_RE = re.compile(
    r"(?:"
    # "Resuming the X draft now" / "I'm resuming the draft now" / "OK,
    # resuming the work now". Clause-initial (sentence start or after a
    # comma/semicolon/colon), optionally led by a first-person progressive
    # — a mid-clause "resuming ... now" after completion language ("I
    # finished resuming the draft just now.") is a recap of PAST work and
    # must not match. "now" must CLOSE the clause (sentence end or a
    # following comma clause, e.g. "now, unless you object"): a finite
    # predicate directly after it ("Resuming the deployment now risks
    # corrupting the database.") makes the gerund a SUBJECT — a warning,
    # not a sign-off.
    r"(?:^|(?<=[,;:]\s))(?:(?:i(?:'|\u2019)?m|i am|we(?:'|\u2019)?re|we are) )?"
    r"resuming\b[^.!?\n]{0,80}\bnow\b(?=\W*$|,)"
    # BARE gerund alternatives (no clause-closing "now" to anchor on) all
    # require an explicit sign-off anchor — a "now" adverb or a first-person
    # progressive lead — because an ANCHORLESS gerund phrase is equally the
    # SUBJECT of a completed/failure report ("Resuming the deployment ran
    # the migration twice.") and no verb list can enumerate the finite
    # predicates that follow such a subject. The anchored forms cannot take
    # a finite predicate grammatically, and the named groups still get a
    # structural remainder check (:data:`_MID_REMAINDER_FINITE_TOKEN_RE`)
    # as a belt. Anchorless sign-offs ("Resuming the draft." / "Getting
    # back to the draft.") are ACCEPTED false negatives — the cheap side
    # of this guard's trade-off.
    r"|(?P<bare1>\bnow resuming\b)"
    # First-person promise: "I'll resume", "I will continue with", "let me
    # get back to the draft". "continue" alone is too broad ("I'll continue
    # to monitor..." is a sign-off, not a task-resume promise) and "get back
    # to YOU" is a reply promise, not a task-resume promise. Clause-initial
    # or led by a coordinating conjunction ("...validated, and I'll resume
    # the draft now.") — a SUBORDINATE occurrence is reported text, not the
    # assistant's own commitment ("The audit incorrectly claims I'll resume
    # the deployment." must not match).
    r"|(?:^|(?<=[,;:\u2013\u2014-]\s)|(?<=\band )|(?<=\bso )|(?<=\bthen )"
    r"|(?<=\bbut ))"
    r"(?:i(?:'|\u2019)?ll|i will|let me) "
    r"(?:resume\b"
    r"|continue (?:with|the|on|working)\b"
    r"|pick\b[^.!?\n]{0,60}\bback up\b"
    r"|get back to (?!you\b))"
    # "Continuing with X now" / "continuing on X now" — clause-initial with
    # an optional first-person progressive lead-in and clause-closing "now",
    # like the "resuming ... now" alternative: a subordinate "...says
    # continuing with the rollout now." is reported text, and "Continuing
    # with the migration now violates the maintenance policy." is a warning.
    r"|(?:^|(?<=[,;:]\s))(?:(?:i(?:'|\u2019)?m|i am|we(?:'|\u2019)?re|we are) )?"
    r"continuing (?:with|on)\b[^.!?\n]{0,80}\bnow\b(?=\W*$|,)"
    # "I'm getting back to the ComCyberMI draft" (not "getting back to
    # you") / "We're picking the draft back up" — constrained to an actual
    # SIGN-OFF shape: clause-initial, a MANDATORY first-person progressive
    # or "now" lead (an anchorless "Picking the deployment back up sent
    # duplicate notifications." is a gerund SUBJECT plus a finite
    # predicate — a report, not a sign-off — and the anchored forms cannot
    # take a finite predicate grammatically), and no further clause up to
    # the sentence end (a "Getting back to the draft, I wrote every
    # section..." recap has one). Blocked closings ("Getting back to the
    # draft would be unsafe...") are handled by the shared
    # :data:`_BLOCKED_AFTER_PROMISE_RE` veto, which covers every
    # alternative. The tail check is a LOOKAHEAD so the vetoes still see
    # the remainder.
    r"|(?P<bare3>(?:^|(?<=[,;:]\s))"
    r"(?:i(?:'|\u2019)?m|i am|we(?:'|\u2019)?re|we are|now) "
    r"(?:getting back to (?!you\b)|picking\b[^.!?\n,;:]{0,60}\bback up\b)"
    r"(?=[^.!?\n,;:]{0,60}[.!?]?\s*$))"
    # "Back to the ComCyberMI draft now" / "I'm back to the draft now" —
    # clause-initial like the "resuming ... now" alternative, so a
    # past-tense "I got back to the draft just now." recap never matches;
    # clause-closing "now" for the same reason as above.
    r"|(?:^|(?<=[,;:]\s))(?:(?:i(?:'|\u2019)?m|i am) )?"
    r"back to (?:the|your|our)\b[^.!?\n]{0,60}\bnow\b(?=\W*$|,)"
    r")",
    re.IGNORECASE,
)


# Sentence boundaries for the closing-sentence extraction: a terminator
# followed by whitespace, or any newline run (markdown lists / paragraphs).
# A terminator at end-of-string has no following whitespace, so it stays
# attached to the last sentence — harmless, the promise regex ignores it.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]\s+|\n+")

# Completion veto for the SAME closing sentence: a promise-shaped opening
# whose remainder states the work is done ("Getting back to the draft, I
# completed every section." / "Picking the draft back up earlier let me
# finish it.") is a recap of finished work, not a promise. Past/state
# completion markers only — forward infinitives ("continue with the report
# to finish the remaining sections") must NOT veto a genuine promise, so
# bare "finish" / "complete-as-verb" forms are deliberately absent except
# "complete", which doubles as the state adjective ("is complete").
_COMPLETION_AFTER_PROMISE_RE = re.compile(
    r"\b(?:complet(?:e|ed)|finished|done|delivered|attached|already|earlier"
    r"|wrapped up)\b",
    re.IGNORECASE,
)

# Deferral/conditional veto for the promise's NEARBY CONTEXT: a commitment
# that names an explicit or relative future time ("I will resume the
# deployment tomorrow." / "I'll resume the deployment in 30 minutes.") or a
# prerequisite gate ("I'll continue with the migration after you approve
# it." / "I'll resume the deployment after the maintenance window opens.")
# is deliberately NOT imminent — nudging it would push the model to act
# before its stated time or precondition. The conditional markers are
# DELIBERATELY BROAD (any "after/once/when/... <something>" clause counts,
# whoever or whatever the gate is): a vetoed genuine promise is only a false
# negative, the cheap side, while a nudged deferred action can fire before a
# maintenance/safety gate. Vague imminence ("shortly", "in a moment") is NOT
# vetoed: an unqualified promise the turn cannot keep is exactly the hollow
# sign-off this guard exists for. "unless you ..." is NOT a gate either — it
# is an opt-out on an otherwise-imminent action ("Resuming now, unless you
# want something else first."), imminent by default. Checked against the
# closing sentence PLUS the two sentences before it: the qualifier can
# precede the promise within the sentence ("Once you confirm the scope,
# I'll resume the migration.") or sit in the sentence just before it ("I
# need your approval first. I'll resume the deployment.") — while the
# promise MATCH itself stays anchored to the closing sentence.
_DEFERRED_PROMISE_RE = re.compile(
    r"\b(?:"
    # Explicit or relative future time. "in a moment" is deliberately NOT
    # here — the unit list has no "moment(s)", keeping vague imminence a
    # promise.
    r"tomorrow|tonight|overnight"
    r"|next (?:week|month|quarter|year|monday|tuesday|wednesday|thursday"
    r"|friday|saturday|sunday)"
    r"|(?:on|by|before|this) (?:monday|tuesday|wednesday|thursday|friday"
    r"|saturday|sunday)"
    r"|at \d{1,2}(?::\d{2})?\s*(?:am|pm|utc)?\b"
    r"|(?:in the|this) (?:morning|afternoon|evening)"
    # Bare "later" counts too ("I'll resume the deployment later."): a
    # relative deferral with no fixed time is still not imminent, and the
    # veto's cost is only a false negative.
    r"|later\b"
    # Quantities spelled with ordinary words ("in two hours") defer exactly
    # like digits do; "within" is the same commitment shape as "in", and
    # "business day(s)" the same unit as "day(s)".
    r"|(?:in|within) (?:\d+|about \d+|a few|several|a couple(?: of)?|an?"
    r"|half an?"
    r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|fifteen|twenty|thirty|forty(?:-five)?|fifty|sixty|ninety) "
    r"(?:seconds?|minutes?|hours?|(?:business )?days?|weeks?|months?)\b"
    # Named clock times and business-day anchors ("at noon", "by end of
    # day") — the numeric "at 5pm" form is covered above.
    r"|(?:at|by|around|before|until) (?:noon|midnight|midday|dawn|dusk"
    r"|sunrise|sunset|lunch(?:time)?|eod|cob|eow"
    r"|(?:the )?(?:end of (?:the )?(?:day|week|month)|close of business))\b"
    r"|(?:in|by|during|on) (?:january|february|march|april|may|june|july"
    r"|august|september|october|november|december)\b"
    # Numeric/ISO date forms: "on 2026-09-10", "by 09/10", "on the 10th".
    r"|(?:on|by|before) (?:the )?(?:\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}(?:st|nd|rd|th)?(?:[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?)?)\b"
    # General prerequisite/conditional markers, whatever follows them.
    r"|(?:after|once|when|whenever|as soon as|if|upon|pending|until"
    r"|provided|assuming|subject to)\s+\S"
    # Approval-family gates behind markers too common to generalise
    # ("with" / "following"). The gate-holder is anything short — "your",
    # "the user's", "security", "CAB", "the board's" — or absent entirely
    # ("with approval"), so allow zero to three modifier words before the
    # approval noun rather than enumerating possessors (veto direction: a
    # miss is only a false negative).
    r"|(?:with|following) (?:[\w'\u2019-]+ ){0,3}?"
    r"(?:approval|confirmation|review|sign-?off|go-?ahead|green light|input"
    r"|feedback|reply|response|decision)"
    # Explicit need/await-approval clauses anywhere in the closing sentence
    # ("Resuming the deployment now, but I need your approval first.").
    r"|(?:need|needs|require|requires|await|awaits|awaiting"
    r"|waiting (?:on|for)|must (?:first )?(?:get|have|obtain|secure)) "
    r"(?:[\w'\u2019-]+ ){0,3}?"
    r"(?:approval|confirmation|sign-?off|go-?ahead|green light"
    r"|authorization|authorisation|permission|consent)"
    r")",
    re.IGNORECASE,
)

# Negation veto for the SAME CLAUSE preceding the promise-shaped phrase: a
# negated / refused / doubted commitment ("I can't promise I'll resume the
# deployment." / "I don't think I'll resume the deployment.") is the
# OPPOSITE of a promise, and retracting it would nudge the model to execute
# the very action the sentence declines. Scoped to the clause the match sits
# in (from the last comma/semicolon/colon/dash before it), so a negation in
# an EARLIER clause never vetoes a genuine promise after the boundary ("That
# issue won't block us; I'll resume the draft now."). Contractions accept a
# straight or curly apostrophe — or none, since models do drop them — via
# the same closed stem list as :data:`_BLOCKED_AFTER_PROMISE_RE` (a bare
# ``\\w+n't`` shape would swallow words like "want").
_NEGATION_BEFORE_PROMISE_RE = re.compile(
    r"\b(?:not|no|never|cannot|unable|unlikely"
    r"|doubt(?:s|ed|ing|ful)?|refus(?:e[sd]?|ing|al)|declin(?:e[sd]?|ing)"
    r"|(?:is|are|was|were|wo|would|sha|should|ca|could|must|do|does|did"
    r"|has|have|had|ai|need)n(?:'|\u2019)?t)\b",
    re.IGNORECASE,
)

# Clause boundaries for the negation veto's backward scope.
_PRE_PROMISE_CLAUSE_RE = re.compile(r"[,;:\u2013\u2014]")

# Blocked/modal-state veto for the remainder of the SAME closing sentence: a
# copula or modal AFTER the promise-shaped phrase means the sentence REASONS
# ABOUT the action instead of committing to it ("Resuming the deployment now
# would be unsafe." / "Continuing with the deployment now would violate the
# maintenance policy.") — retracting it would nudge the model to execute the
# very action the sentence rules out. A closed grammatical class (not an
# open verb list), shared by ALL promise alternatives. Negated contractions
# count too ("Resuming the deployment now isn't safe."), with straight or
# curly apostrophes — and without one, since models do drop them.
_BLOCKED_AFTER_PROMISE_RE = re.compile(
    r"\b(?:is|are|was|were|be|being|been|will|would|shall|should|can|cannot"
    r"|could|may|might|must|do|does|did|has|have|had"
    r"|(?:is|are|was|were|wo|would|sha|should|ca|could|must|do|does|did"
    r"|has|have|had|ai)n(?:'|\u2019)?t)\b",
    re.IGNORECASE,
)

# Structural sign-off belt for the remainder of the BARE gerund
# alternatives (the ``bare1``/``bare3`` groups — no clause-closing "now" to
# anchor on): those alternatives already require a "now" adverb or a
# first-person progressive lead, which a gerund SUBJECT cannot carry, so a
# following finite predicate is ungrammatical — but a model's prose is not
# guaranteed grammatical, so a MID-remainder finite token (third-person -s
# or past-tense -ed followed by its complement, e.g. "Now resuming the
# deployment causes an outage.") still vetoes. A genuine sign-off's
# remainder is a bare noun phrase, where such a token (a plural noun, a
# participial adjective) can only sit at the very end ("Now resuming the
# remaining sections."). Rejecting on any mid-remainder -s/-ed token needs
# NO verb list; the cost is false negatives on mid-phrase plurals or
# participles ("Now resuming the systems integration draft.") — the cheap
# side.
_MID_REMAINDER_FINITE_TOKEN_RE = re.compile(r"\b\w+(?:s|ed)\s+\S", re.IGNORECASE)

# Warning-predicate veto for the remainder of the SAME closing sentence,
# whatever alternative matched: a curated list of consequence/assessment
# verbs — third-person -s forms (a gerund subject is singular) AND their
# past-tense forms — that backstops the shapes the structural checks cannot
# reach: chiefly a comma clause after an anchored "... now," ("Resuming the
# deployment now, which risks corruption.") and irregular or invariant past
# forms the -ed structural belt misses ("broke", "put", "hurt", "cost",
# "took"). Anchorless gerund REPORTS ("Resuming the deployment broke the
# replication.") no longer reach this veto at all — the bare alternatives
# require a sign-off anchor precisely because no verb list can enumerate
# their finite predicates. "left" is deliberately absent — "I'll resume
# where I left off." is a canonical genuine promise. An open class by
# nature, but every verb listed only costs false negatives on genuine
# promises, the cheap side.
_WARNING_PREDICATE_RE = re.compile(
    r"\b(?:risk(?:s|ed)|violat(?:es|ed)|breaks|broke|threaten(?:s|ed)"
    r"|jeopardi[sz](?:es|ed)|endanger(?:s|ed)|requir(?:es|ed)|need(?:s|ed)"
    r"|caus(?:es|ed)|creat(?:es|ed)|introduc(?:es|ed)|prevent(?:s|ed)"
    r"|block(?:s|ed)|defeat(?:s|ed)|conflict(?:s|ed)|contradict(?:s|ed)"
    r"|expos(?:es|ed)|invalidat(?:es|ed)|corrupt(?:s|ed)|destroy(?:s|ed)"
    r"|delet(?:es|ed)|drop(?:s|ped)|overwrites|overwrote|disrupt(?:s|ed)"
    r"|compromis(?:es|ed)|undermin(?:es|ed)|void(?:s|ed)|seem(?:s|ed)"
    r"|appear(?:s|ed)|look(?:s|ed)|sound(?:s|ed)|feels|felt|remain(?:s|ed)"
    r"|mean(?:s|t)|impli(?:es|ed)"
    r"|put|hurt|cost|set|took|brought|made|lost|threw|shut)\b",
    re.IGNORECASE,
)

# Closing delimiters ignored by the question veto in
# :func:`_ends_with_resume_promise`: straight/curly quotes, backtick,
# markdown emphasis, brackets and guillemets that can legitimately wrap a
# closing question ("\u201cI'll resume the draft?\u201d").
_CLOSING_DELIMS = "\"'\u201c\u201d\u2018\u2019`*_)]}\u00bb"

# Quoted-span mask for the promise search: a promise inside quotation marks
# is REPORTED text, not the assistant's own commitment — 'Please disregard
# \u201cI'll resume the deployment.\u201d' explicitly CANCELS the quoted
# promise, and matching inside the quote would retract the cancellation and
# nudge the cancelled action. Only unambiguous quote pairs are masked
# (straight/curly double quotes, curly single quotes, guillemets,
# backticks); straight single quotes are deliberately left alone — they are
# contraction apostrophes ("I'll") far more often than quotes, and masking
# them would break the first-person alternatives entirely, while a quoted
# promise they carry is only a residual false positive.
_QUOTED_SPAN_RE = re.compile(
    r"\"[^\"\n]*\""
    r"|\u201c[^\u201d\n]*\u201d"
    r"|\u2018[^\u2019\n]*\u2019"
    r"|\u00ab[^\u00bb\n]*\u00bb"
    r"|`[^`\n]*`"
)


def _ends_with_resume_promise(text: str) -> bool:
    """True when a tool-free answer CLOSES with a promise to resume work.

    Conservative by construction (mirrors :func:`_looks_like_unfinished_action`):
    the promise MATCH is anchored to the CLOSING SENTENCE of the message
    tail — that is where a sign-off promise lives, and it keeps a
    completed-work recap ("Getting back to the draft, I completed every
    section. The deliverable is attached.") from being mistaken for a
    promise when the promise-like phrase sits in an earlier sentence. A
    tail that ends with a question hands control back to the user, so it is
    never treated as a promise, and an explicitly deferred or
    prerequisite-gated commitment is vetoed (:data:`_DEFERRED_PROMISE_RE`,
    scanned over the closing sentence plus the two before it, because the
    prerequisite is often stated just before the promise), as is a negated
    or declined one (:data:`_NEGATION_BEFORE_PROMISE_RE`) — only immediate,
    unqualified, affirmative promises trip the guard. The cost of the anchoring is only false NEGATIVES (a
    promise followed by a trailing pleasantry sentence is missed), which
    merely leaves the pre-existing behaviour in place — the cheap side of
    this guard's trade-off.
    """
    if not text:
        return False
    tail = text[-600:].rstrip()
    # Question veto — quote/markdown-aware: a closing question can sit inside
    # quotes or emphasis ("\u201cI'll resume the draft?\u201d"), so trailing
    # closing delimiters are ignored before the "?" check. Retracting a
    # legitimate question would nudge the model to execute before the user
    # answers it.
    if tail.rstrip(_CLOSING_DELIMS).endswith("?"):
        return False
    # Mask quoted spans BEFORE sentence splitting so a quoted promise —
    # reported or explicitly cancelled text, not the assistant's own
    # commitment — never reaches the promise search, including when the
    # quote spans sentence boundaries.
    tail = _QUOTED_SPAN_RE.sub(" ", tail)
    sentences = [s for s in _SENTENCE_BOUNDARY_RE.split(tail) if s.strip()]
    if not sentences:
        return False
    closing = sentences[-1]
    match = _RESUME_PROMISE_RE.search(closing)
    if not match:
        return False
    # Negation veto on the SAME CLAUSE before the match: "I can't promise
    # I'll resume the deployment." DECLINES the commitment — the remainder
    # vetoes below cannot see it because it precedes the match. Clause-scoped
    # so a negation before a comma/semicolon boundary ("That issue won't
    # block us; I'll resume the draft now.") never vetoes the genuine promise
    # after it.
    pre_clause = _PRE_PROMISE_CLAUSE_RE.split(closing[: match.start()])[-1]
    if _NEGATION_BEFORE_PROMISE_RE.search(pre_clause):
        return False
    # Deferral/conditional veto: an explicitly deferred or prerequisite-gated
    # commitment ("I will resume the deployment tomorrow." / "I'll continue
    # with the migration after you approve it.") is a valid closing, and
    # nudging it would push the model to act BEFORE its stated time or
    # precondition — the one failure mode worse than a hollow promise.
    # Scanned over the closing sentence PLUS the two sentences before it:
    # the prerequisite is often stated just before the promise ("I need
    # your approval first. I'll resume the deployment."), where a
    # closing-sentence-only veto cannot see it. The promise MATCH stays
    # anchored to the closing sentence (an earlier-sentence promise inside
    # a recap must not re-trigger), and the window is bounded so a time or
    # condition mentioned much earlier in the tail — about something else
    # entirely — does not veto a genuinely immediate sign-off.
    if _DEFERRED_PROMISE_RE.search(" ".join(sentences[-3:])):
        return False
    remainder = closing[match.end() :]
    # Blocked/modal-state veto: a copula or modal after the promise-shaped
    # phrase means the closing reasons about the action instead of
    # committing to it ("Resuming the deployment now would be unsafe.") —
    # retracting it would nudge the model to execute the very action the
    # sentence rules out.
    if _BLOCKED_AFTER_PROMISE_RE.search(remainder):
        return False
    # Structural sign-off belt for the bare gerund alternatives: a
    # mid-remainder -s or -ed token is a finite predicate after a gerund
    # SUBJECT ("Now resuming the deployment causes an outage.") — a warning
    # or failure report, not a sign-off. The anchors those alternatives
    # require make that reading ungrammatical already; this belt covers
    # ungrammatical model prose. No verb list involved (see
    # _MID_REMAINDER_FINITE_TOKEN_RE).
    if any(
        match.group(g) for g in ("bare1", "bare3")
    ) and _MID_REMAINDER_FINITE_TOKEN_RE.search(remainder):
        return False
    # Warning-predicate veto: a consequence/assessment verb after the phrase
    # ("Resuming the deployment now, which risks corruption.") means the
    # closing warns about the action — nudging it would push the model to
    # execute the very action it warned against.
    if _WARNING_PREDICATE_RE.search(remainder):
        return False
    # Same-sentence completion veto: when the clause AFTER the promise-shaped
    # phrase states the work is done, the sentence is a completed-work recap
    # and retracting it would risk duplicate tool side effects. The cost is
    # again only false negatives (a genuine promise mentioning e.g. an
    # "already attached" artifact is missed) — the cheap side.
    return not _COMPLETION_AFTER_PROMISE_RE.search(remainder)


# ``composing`` events whose ``reason`` marks a mid-loop RE-PROMPT — the run
# continues with its FULL budget — as opposed to the budget-driven wind-down
# emissions (Stage-2 tool strip, forced final answer). Truncation latches
# (web chat, platform proxy) and the eval-trace labeller must treat these as
# continuations, never as "turn limit reached". Single source of
# truth: every new guard that re-prompts via ``composing`` adds its reason
# here so the latch sites can never drift.
CONTINUATION_COMPOSING_REASONS: frozenset[str] = frozenset(
    {
        "stall_continuation",
        "resume_promise_continuation",
        "leaked_tool_call_continuation",
        "frictions_continuation",
    }
)


# The assistant "checkpoint" text a continuation guard keeps in the working
# transcript (immediately followed by its system nudge) is NOT a deliverable: a
# retracted "about to act" narration, a tool call emitted as markup, or a resume
# promise. ``select_best_result_summary`` identifies such a message by the nudge
# that follows it and skips it, so none is ever surfaced as the run result.
# Every continuation guard that appends a (checkpoint, FIXED nudge) pair lists
# its nudge here, next to its reason above, so the two can never drift. (The
# frictions guard's nudge is per-run dynamic text, not a constant, so it is not
# listed here.)
CONTINUATION_NUDGES: frozenset[str] = frozenset(
    {
        _STALL_CONTINUATION_NUDGE,
        _LEAKED_TOOL_CALL_NUDGE,
        _RESUME_PROMISE_NUDGE,
    }
)


# ── Session variable ($collected / $input) reference resolution ────────────
# ``collect_task_results`` stores merged background-task output under
# ``session_vars["$collected"]``; a transformer opted in sees its input as
# ``$input``. Downstream tool calls reference a variable either whole
# (``$collected``) or by sub-path (``$collected.rules[0].name``) to create one
# entity per collected item. Resolving ONLY the exact whole-value match
# silently passed sub-path strings through to tools verbatim — e.g. an
# indicator was created in OpenCTI with the literal name
# ``$collected.rules[9].name``. These helpers resolve ``.key`` / ``[index]`` /
# ``["key"]`` paths so per-item references work, and let the loop ERROR a
# reference it cannot resolve instead of writing a literal placeholder
# downstream.
#
# Segments: ``.identifier``; ``[integer]`` (a list index, or the i-th value of
# an object); ``["any key"]`` / ``['any key']`` for a key holding a dash, dot,
# colon or space (a STIX ``extension-definition--UUID`` key), a backslash
# escaping the quote or the backslash inside. The quoted alternatives are
# disjoint (``[^"\\]`` or ``\\.``), so matching stays linear: no catastrophic
# backtracking on a long or hostile argument.
_SESSION_PATH_TOKEN_RE = re.compile(
    r"\.([A-Za-z_]\w*)"
    r"|\[(\d+)\]"
    r"|\[\"((?:[^\"\\]|\\.)*)\"\]"
    r"|\['((?:[^'\\]|\\.)*)'\]",
    re.DOTALL,
)
_SESSION_PATH_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)

# Sentinel for "no value at this index" so a legitimately-stored ``None`` is
# never confused with a missing element when indexing a dict positionally.
_SESSION_PATH_MISSING = object()

_SESSION_PATH_SYNTAX = (
    "{var}.key, {var}[0] (a list index, or the i-th value of an object), "
    '{var}["any-key"] for a key holding a dash, dot, colon or space '
    '(a backslash escapes a quote inside), e.g. {var}.items[0]["name-with-dash"].value'
)


def _quote_session_key(key: str) -> str:
    """The ``["key"]`` segment that addresses *key* in a session path."""
    return '["' + key.replace("\\", "\\\\").replace('"', '\\"') + '"]'


def _walk_session_path(obj: Any, path: str, label: str = "") -> tuple[bool, Any, str | None]:
    """Walk a session reference *path* into a nested structure.

    Returns ``(ok, value, failure)``: ``failure`` says which segment failed and
    why (malformed, missing key, index out of range, not a container) when
    ``ok`` is False, so the caller can tell the model how to fix the call.
    *label* (the variable name) prefixes the paths the failure quotes.
    """
    tokens: list[re.Match[str]] = []
    pos = 0
    for match in _SESSION_PATH_TOKEN_RE.finditer(path):
        if match.start() != pos:  # junk between tokens → not a clean path
            break
        tokens.append(match)
        pos = match.end()
    if pos != len(path):
        # The bad segment: a dotted key that runs on into junk
        # (``.extension-definition--x``) starts at that key.
        start = pos
        if path[pos] not in ".[" and tokens and tokens[-1].end() == pos and tokens[-1].group(1) is not None:
            start = tokens[-1].start()
        end = len(path)
        for stop in (".", "["):
            cut = path.find(stop, start + 1)
            if cut != -1:
                end = min(end, cut)
        segment = path[start:end]
        failure = f"malformed segment '{segment}'"
        if segment.startswith(".") and len(segment) > 1:
            failure += f"; write a key with a dash, dot, colon or space as {_quote_session_key(segment[1:])}"
        return False, None, failure
    cur = obj
    for match in tokens:
        where = (label + path[: match.start()]) or "the variable"
        index = match.group(2)
        if index is None:
            key = match.group(1)
            if key is None:
                quoted = match.group(3) if match.group(3) is not None else match.group(4)
                key = _SESSION_PATH_ESCAPE_RE.sub(r"\1", quoted)
            if not isinstance(cur, dict):
                return False, None, f"'{where}' is a {type(cur).__name__}, it has no key '{key}'"
            if key not in cur:
                return False, None, f"key '{key}' not found at '{where}'"
            cur = cur[key]
            continue
        i = int(index)
        if isinstance(cur, list):
            if not 0 <= i < len(cur):
                return False, None, f"index [{i}] out of range at '{where}' ({len(cur)} item(s))"
            cur = cur[i]
        elif isinstance(cur, dict):
            # ``collect_task_results`` stores merged items as an
            # insertion-ordered dict — keyed by item id so duplicates
            # across tasks collapse, and because bulk consumers such as
            # ``import_opencti_dashboard`` (raw_widgets) require the object
            # form. A positional ``[i]`` therefore selects the i-th
            # collected item, the "one entity per item" navigation the
            # personas document (e.g. ``$collected.rules[i].name``). Dicts
            # preserve insertion order (py3.7+), so the index is stable.
            # ``islice`` reaches the i-th value WITHOUT materializing the
            # whole ``list(values)`` on every access (large payloads ×
            # many per-item tool calls) — still O(i), but no O(n) alloc.
            picked = next(
                itertools.islice(cur.values(), i, i + 1),
                _SESSION_PATH_MISSING,
            )
            if picked is _SESSION_PATH_MISSING:
                return False, None, f"index [{i}] out of range at '{where}' ({len(cur)} key(s))"
            cur = picked
        else:
            return False, None, f"'{where}' is a {type(cur).__name__}, it has no index [{i}]"
    return True, cur, None


def _navigate_session_path(obj: Any, path: str) -> tuple[bool, Any]:
    """Walk a ``.key`` / ``[index]`` / ``["key"]`` *path* into a nested structure.

    Returns ``(ok, value)``. ``ok`` is False when the path is malformed or a
    segment is missing, so the caller surfaces an actionable error instead of
    passing an unresolved reference downstream.
    """
    ok, value, _failure = _walk_session_path(obj, path)
    return ok, value


def _session_reference_variable(value: str, session_vars: dict) -> str | None:
    """The session variable *value* references (whole or by path), else None."""
    if value in session_vars:
        return value
    # Longest key first so a var that is a prefix of another resolves correctly.
    for var_key in sorted(session_vars, key=len, reverse=True):
        if value.startswith(var_key) and len(value) > len(var_key) and value[len(var_key)] in ".[":
            return var_key
    return None


def _resolve_session_reference(
    value: str, session_vars: dict
) -> tuple[bool, bool, Any]:
    """Resolve a ``$var`` reference, optionally with a ``.key`` / ``[i]`` / ``["key"]`` path.

    Returns ``(is_reference, resolved_ok, resolved_value)``:
      * ``(False, False, None)`` — not a session reference; leave untouched.
      * ``(True, True, <value>)`` — resolved (whole variable or a sub-path).
      * ``(True, False, None)`` — references a known variable but the path could
        not be navigated; the caller must error the call, never pass a literal.
    """
    var_key = _session_reference_variable(value, session_vars)
    if var_key is None:
        return False, False, None
    if value == var_key:
        return True, True, session_vars[var_key]
    ok, resolved = _navigate_session_path(session_vars[var_key], value[len(var_key) :])
    return True, ok, resolved


def _session_reference_error(value: str, key: str, session_vars: dict) -> str:
    """The tool-result error for a reference *value* in argument *key* that did not resolve.

    Names the variable used, the segment that failed and the supported path
    syntax for THAT variable (never a ``$collected`` example on ``$input``).
    """
    var_key = _session_reference_variable(value, session_vars) or "$var"
    failure = None
    if var_key in session_vars:
        _ok, _value, failure = _walk_session_path(session_vars[var_key], value[len(var_key) :], var_key)
    message = f"Could not resolve session reference '{value}' for argument '{key}'"
    message += f": {failure}." if failure else "."
    message += f" The tool was NOT executed. Path syntax: {_SESSION_PATH_SYNTAX.format(var=var_key)}."
    if var_key == "$collected":
        message += " Check the field names and the item count in the collect_task_results output."
    return message


# ── Retyped $collected content guard (#2686) ─────────────────────────────
# A run that holds ``$collected`` must hand an item's content to a write tool
# BY REFERENCE (``$collected.rules[3].pattern``): a model that retypes a long
# converted rule silently writes its own text, presented as the producer's
# verified output. A write-tool argument that is a near copy of a collected
# field (similar, not identical) is refused before the tool runs, naming the
# path to pass. Short values (a type, a score, a label) legitimately repeat
# and are never compared; an exact copy is the same content and passes.
_RETYPE_MIN_CHARS = 40
_RETYPE_SIMILARITY = 0.75
_RETYPE_COMPARE_CHARS = 4000
_RETYPE_MAX_ITEMS = 200


def _collected_item_lists(collected: Any) -> list[tuple[str, list[tuple[int, dict[str, Any]]]]]:
    """The item lists of a ``$collected`` value: ``(path, [(index, item), ...])``."""
    lists: list[tuple[str, list[tuple[int, dict[str, Any]]]]] = []
    if not isinstance(collected, dict):
        return lists
    for key, value in collected.items():
        if isinstance(value, dict):
            values = list(value.values())
        elif isinstance(value, list):
            values = value
        else:
            continue
        items = [(i, item) for i, item in enumerate(values[:_RETYPE_MAX_ITEMS]) if isinstance(item, dict)]
        if items:
            segment = f".{key}" if str(key).isidentifier() else _quote_session_key(str(key))
            lists.append((f"$collected{segment}", items))
    return lists


def _near_copy(typed: str, stored: str) -> bool:
    """True when *typed* is a retyped (similar, not identical) copy of *stored*."""
    if typed == stored:
        return False
    a, b = typed[:_RETYPE_COMPARE_CHARS], stored[:_RETYPE_COMPARE_CHARS]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < _RETYPE_SIMILARITY or matcher.quick_ratio() < _RETYPE_SIMILARITY:
        return False
    return matcher.ratio() >= _RETYPE_SIMILARITY


def _retyped_collected_error(arguments: dict[str, Any] | None, session_vars: dict[str, Any] | None) -> str | None:
    """The refusal for a write call that retypes a ``$collected`` field, else None.

    An argument is compared with the field of the same name of every collected
    item (``update_*_field`` style tools, ``key`` + ``value``, compare
    ``value`` with the field ``key`` names). Only strings of at least
    ``_RETYPE_MIN_CHARS`` on both sides are compared.
    """
    if not arguments or not session_vars or "$collected" not in session_vars:
        return None
    lists = _collected_item_lists(session_vars["$collected"])
    if not lists:
        return None
    pairs: list[tuple[str, str, str]] = []  # (argument, field, typed value)
    for arg, value in arguments.items():
        if isinstance(value, str) and len(value) >= _RETYPE_MIN_CHARS:
            pairs.append((arg, arg, value))
    field = arguments.get("key")
    typed_value = arguments.get("value")
    if isinstance(field, str) and isinstance(typed_value, str) and len(typed_value) >= _RETYPE_MIN_CHARS:
        pairs.append(("value", field, typed_value))
    for arg, field_name, typed in pairs:
        candidates: list[tuple[str, int, str]] = []
        for base, items in lists:
            for index, item in items:
                stored = item.get(field_name)
                if isinstance(stored, str) and len(stored) >= _RETYPE_MIN_CHARS:
                    candidates.append((base, index, stored))
        # The exact text of any item is that item's content: nothing retyped.
        if any(stored == typed for _base, _index, stored in candidates):
            continue
        for base, index, held in candidates:
            if _near_copy(typed, held):
                segment = f".{field_name}" if field_name.isidentifier() else _quote_session_key(field_name)
                path = f"{base}[{index}]{segment}"
                return (
                    f"Argument '{arg}' retypes the collected field {path} instead of "
                    f"passing it by reference: the text differs from the stored value. "
                    f"The tool was NOT executed. Pass the string `{path}` as the value of "
                    f"'{arg}' and the platform writes the collected text verbatim."
                )
    return None


def _build_truncated_tool_call_retry(*, remaining_iterations: int) -> str:
    """Build the retry-guidance system message for truncated tool-call JSON.

    Dynamic budget context (vs. the previous static "more concise
    approach" hand-wave) makes the LLM trade off "retry this tool"
    against "abandon and answer with what I have" more accurately when
    the iteration budget is nearly spent.  The checkpoint demand is
    consistent with :data:`_CONTINUATION_PROMPT` and
    :func:`interrupted_turn_framing` so users see the same
    "checkpoint, then continue / answer" pattern across all three
    truncation-recovery paths.
    """
    return (
        "[SYSTEM — output truncated] Your previous response was cut off "
        "by the output token limit while generating a tool call. The "
        "tool call was incomplete and could not be executed. "
        f"Budget warning: only {remaining_iterations} iteration(s) remain. "
        "Prioritize a checkpoint-style status and final completion summary. "
        "If you can answer the user WITHOUT calling this tool, do so now. "
        "If the tool call is absolutely essential, retry with a concise "
        "approach: keep reasoning brief and emit the tool call immediately."
    )

# Maximum consecutive output-truncation events before the loop aborts
# with an error.  Prevents burning 20+ iterations when the model
# physically cannot fit a tool call within its output budget.
_MAX_TRUNCATION_RETRIES = 2

# Per-LLM-call timeout (seconds).  Prevents a single hung provider from
# blocking the loop indefinitely (SDK defaults are often 600s+).
_LLM_CALL_TIMEOUT = 900

# Per-iteration LLM timeout retry.  When a single LLM call times out
# due to a transient provider latency spike, the loop retries up to this many
# times with a short backoff before declaring the iteration terminal.  This is
# distinct from (a) the assignment-level retry (whole-run restart) and (b)
# ``retry_llm_call``'s API-error retries (transient 429/5xx from the
# provider).  The counter resets on each successful LLM call so isolated
# latency spikes cannot permanently reduce the retry budget for later
# iterations.
#
# The guard requires the remaining wall-clock budget to exceed
# ``MIN_REMAINING + BACKOFF`` before scheduling a retry, so that AFTER the
# backoff sleep the next iteration still has at least ``MIN_REMAINING``
# seconds to work with.  This keeps the retry composable with a
# pre-iteration minimum-budget guard at the top of the loop: a
# scheduled retry is never immediately re-routed to the backstop, and no
# near-zero-timeout LLM call is ever issued by a retry.
#
# Deadline-driven timeouts are inherently NOT retried: when the per-call
# timeout was derived from the wall-clock deadline (``_remaining_llm_timeout``
# capped it below ``_LLM_CALL_TIMEOUT``), the timing-out call by construction
# burns the budget down to ~``_IN_LOOP_DEADLINE_RESERVE_SECS`` seconds, which
# is far below ``MIN_REMAINING`` — so the guard skips the retry and the run
# degrades through the existing terminal / backstop paths.  Only timeouts
# with genuine budget left (the 900 s provider cap or the idle-chunk
# watchdog) can retry, i.e. exactly the transient-provider-spike cases.
_LLM_TIMEOUT_MAX_RETRIES = 2
_LLM_TIMEOUT_RETRY_BACKOFF_SECS = 5.0
_LLM_TIMEOUT_RETRY_MIN_REMAINING_SECS = 30.0

# Streaming idle-chunk watchdog (P1.2).  A stalled SSE
# stream (the provider accepted the request but stops sending events) was
# previously bounded only by the SDK read timeout (600 s) or the outer
# 900 s per-call cap.  ``_streaming_chat`` aborts the stream when NO
# activity — neither a yielded text chunk nor an ``on_thinking`` reasoning
# delta — arrives within this window.  Thinking deltas count as activity
# because reasoning models legitimately produce no text chunks for minutes
# while emitting thinking tokens through the callback.
_STREAM_IDLE_TIMEOUT_SECS = 120.0

# Wall-clock-budget awareness inside the loop (P1.1).
# ``wall_clock_deadline`` was previously honoured only by the forced-final-
# answer backstop; in-loop LLM calls and compaction summarisers still used
# the full ``_LLM_CALL_TIMEOUT`` (900 s), so a turn near its budget could be
# cancelled mid-call by the caller's outer ``asyncio.wait_for`` instead of
# degrading gracefully.  In-loop calls now cap their timeout to the
# remaining budget (minus a small reserve so the loop can still return).
_IN_LOOP_DEADLINE_RESERVE_SECS = 5.0

# Minimum remaining wall-clock budget (seconds) required to START a new
# iteration.  When fewer seconds remain at iteration-start, the loop breaks
# immediately to the forced-final-answer backstop instead of issuing an LLM
# call whose ``_remaining_llm_timeout()`` would be capped to the 1 s floor
# — a near-zero-timeout call that always fails with "timed out after 1s"
# even though the run had made substantial prior progress.
#
# Must be > ``_FORCED_ANSWER_MIN_REMAINING_SECS`` (10 s) so the backstop
# still has enough budget to attempt synthesis instead of degrading to its
# immediate pre-built fallback.  Derived structurally (3x the backstop
# minimum = 30 s) so the invariant cannot silently break if the backstop
# threshold is retuned; the margin gives the backstop a comfortable ~25 s
# synthesis window (30 − ``_FORCED_ANSWER_DEADLINE_RESERVE_SECS``).
_IN_LOOP_MIN_REMAINING_SECS: int = 3 * _FORCED_ANSWER_MIN_REMAINING_SECS

# Slow-turn watchdog.  A legitimate chat turn can run for tens of
# minutes (long conversation -> huge context -> multi-minute LLM calls +
# compaction passes), but all per-iteration progress logging is INFO-level,
# so production log captures (WARNING+) show NOTHING for the whole turn and
# it looks like a silent death.  Once a run exceeds SLOW_TURN_WARN_SECONDS,
# the loop emits a WARNING progress heartbeat at every iteration boundary
# (at most once per _LOOP_PROGRESS_WARN_EVERY seconds) with iteration,
# elapsed time and context stats so operators can see the loop is alive.
# The public name is the import surface for the chat services, which log a
# completion line with the same threshold so the two can never drift: at
# WARNING when it closes a heartbeat series (:func:`watch_slow_turn`), at INFO
# when the loop emitted none and there is nothing to close.
SLOW_TURN_WARN_SECONDS = 300.0
_LOOP_PROGRESS_WARN_AFTER = SLOW_TURN_WARN_SECONDS
_LOOP_PROGRESS_WARN_EVERY = 300.0

_slow_turn_heartbeats: ContextVar[list[float] | None] = ContextVar("slow_turn_heartbeats", default=None)


@contextlib.contextmanager
def watch_slow_turn() -> Iterator[list[float]]:
    """Collect the elapsed seconds of every slow-turn heartbeat a loop started inside the block emits."""
    record: list[float] = []
    token = _slow_turn_heartbeats.set(record)
    try:
        yield record
    finally:
        _slow_turn_heartbeats.reset(token)

# Per-tool execution timeout (seconds).  Prevents a single hung tool
# (e.g. a slow Notion search or unresponsive MCP server) from blocking the
# entire agentic loop indefinitely.  The public name is the import surface
# for other modules (e.g. the assignment engine derives its per-tool budget
# cap ceiling from it so the two values cannot drift apart); the underscored
# alias is kept for the loop's own call sites and existing test
# monkeypatches.
TOOL_EXECUTION_TIMEOUT_SECONDS = 300
_TOOL_EXECUTION_TIMEOUT = TOOL_EXECUTION_TIMEOUT_SECONDS

# Agent-to-agent tools (consult, transfer, spawn) run a full agentic loop
# on the target agent, which can itself make many tool calls.  These need
# a much longer timeout than regular tools.
_AGENT_TOOL_TIMEOUT = 900
_AGENT_TOOL_NAMES = frozenset(
    {
        "consult_agent",
        "transfer_to_agent",
        "spawn_background_task",
    }
)


#: Fallback tool result when a ``wait`` somehow outlives even its own backstop
#: timeout (a hung ``until`` status poll).  Degrading to "continue without
#: waiting" keeps a stuck condition from killing an otherwise healthy run.
_WAIT_TIMEOUT_RESULT = WaitResult(
    message=(
        "The wait ended early: the status check it was polling stopped "
        "responding. Continue with the information you have."
    ),
    reason="timeout",
)


def _tool_timeout_for(
    name: str,
    effective: float,
    wall_clock_deadline: float | None = None,
    declared: int = 0,
) -> float:
    """Per-tool execution timeout.

    Three tiers, and the exceptions are the point: agent-to-agent tools run a
    whole nested loop, and ``wait`` is *supposed* to take a long time — it is
    bounded by the platform's wait policy before it ever starts, so the timeout
    here is only a backstop against a hung poll.  Leaving ``wait`` on the
    regular ceiling would kill any pause longer than the per-tool timeout and
    report it to the model as a tool failure.

    With a ``wall_clock_deadline`` every tier is also capped to what remains of
    the run's budget, like the in-loop LLM calls (``_remaining_llm_timeout``):
    a call still running at the deadline is cancelled together with the whole
    run by the caller's outer ``asyncio.wait_for``, while one that times out
    here returns to the loop, whose deadline guard then answers with what the
    run gathered.

    ``declared`` is the tool's own ``ToolDefinition.execution_timeout``: a
    tool whose one call does long work by design declares it, and it replaces
    the regular ceiling as the agent tier does.
    """
    if declared > 0:
        timeout = declared
    elif name in _AGENT_TOOL_NAMES:
        timeout = _AGENT_TOOL_TIMEOUT
    elif name == WAIT_TOOL_NAME:
        timeout = WAIT_TOOL_TIMEOUT
    else:
        timeout = effective
    if wall_clock_deadline is None:
        return timeout
    remaining = wall_clock_deadline - time.monotonic() - _IN_LOOP_DEADLINE_RESERVE_SECS
    return min(timeout, max(1, int(remaining)))


class _ExecutorNameMap(Mapping[str, str]):
    """Model-supplied call name -> the ORIGINAL name the executor registered.

    The loop offers the model *sanitised* names (``build_llm_tool_names``:
    ``a.b`` -> ``a_b``, and a colliding ``a/b`` -> ``a_b_2``), while the
    executors register the originals.  A call name therefore crosses two name
    spaces on its way to dispatch, and the lookup composes two stages:

    1. **canonicalise in the LLM-visible space** — exact / unambiguous
       reordered tokens / known alias, through the SAME ``ToolNameResolver``
       rules the progressive-disclosure gate resolves with, over the sanitised
       ``effective_tools`` names (``b_a_2`` -> ``a_b_2``);
    2. **reverse-map** that canonical visible name to the original the
       executor knows (``a_b_2`` -> ``a/b``).

    Reverse-mapping only *exact* visible names — the previous behaviour — let
    the gate and dispatch disagree on collision-suffixed names: the gate
    accepted ``b_a_2`` (pinning / protecting ``a_b_2``'s group) while dispatch
    forwarded ``b_a_2`` untouched to the original-name executor, where no
    original carries the synthetic ``2`` token, so the call was refused.
    Stage 1 establishes the invariant that every name the
    gate accepts is a name the executor executes.

    A name that resolves to nothing (unknown, or an ambiguous token key) is
    absent from the mapping: ``get(name, name)`` hands it to the executor
    untouched, whose own resolver — the same rules over its (possibly wider)
    original-name registry — then executes or refuses it.  The two stages
    compose: a tool that never went through the sanitiser has identical
    visible and original names, so stage 2 is the identity for it.

    ``aliases`` is the known-alias table translated into the visible space
    (``visible_aliases``).  Stage 1 must not apply the original-name table
    verbatim: after a sanitisation collision (``search.github.issues`` ->
    ``search_github_issues``, ``search_github_issues`` ->
    ``search_github_issues_2``) the alias's canonical *string* names the
    wrong visible tool.

    The stage-1 resolver is exposed as ``resolver`` and handed to the
    progressive-disclosure gate as ITS resolver, so the two never disagree on
    which tool a call name denotes — the gate resolving over the gated tools
    alone let core ``jira_search_issues`` token-match gated
    ``search_jira_issues`` and protect the wrong group while dispatch ran the
    exact core tool.  The dual of the invariant above: the
    gate resolves a name to the SAME tool dispatch executes it as.
    """

    __slots__ = ("_renamed", "_visible", "_visible_names")

    def __init__(
        self,
        renamed: Mapping[str, str],
        visible_names: Iterable[str],
        *,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        #: sanitised -> original, for the names the sanitiser actually changed.
        self._renamed = dict(renamed)
        self._visible_names = list(visible_names)
        self._visible = ToolNameResolver(self._visible_names, aliases=aliases)

    @property
    def resolver(self) -> ToolNameResolver:
        """The stage-1 resolver, over every offered visible name — share it."""
        return self._visible

    def canonical_visible(self, name: str) -> str | None:
        """Stage 1 alone: the exact LLM-visible name *name* denotes, or ``None``."""
        return self._visible.canonical(name)

    def __getitem__(self, name: str) -> str:
        canonical = self._visible.canonical(name)
        if canonical is None:
            raise KeyError(name)
        return self._renamed.get(canonical, canonical)

    def __iter__(self) -> Iterator[str]:
        return iter(self._visible_names)

    def __len__(self) -> int:
        return len(self._visible_names)


def _is_wait_only_round(
    tool_calls: list[ToolCall], name_map: Mapping[str, str]
) -> bool:
    """True when every tool call in this round only waited.

    ``name_map`` reverses the sanitised LLM-facing names so a renamed tool is
    still recognised.  An empty round is not a wait round.
    """
    if not tool_calls:
        return False
    return all(
        name_map.get(tc.name, tc.name) in WAIT_TOOL_NAMES for tc in tool_calls
    )


#: The ``source`` of the built-in ``check_task_status`` (``builtin_tools/handover.py``).
_BUILTIN_STATUS_POLL_SOURCE = "builtin:sub_agent"


def _run_pause_names(
    tools: Iterable[ToolDefinition] | None, name_map: Mapping[str, str], *, wait_offered: bool
) -> frozenset[str]:
    """The ``WAIT_TOOL_NAMES`` this run owns as pauses: the loop's own timer
    when it is offered, and ``check_task_status`` when every definition behind
    that name is the built-in one. A custom or MCP tool carrying either name is
    a tool of the run and counts like any other."""
    owned = {WAIT_TOOL_NAME} if wait_offered else set()
    polls = [t for t in tools or () if name_map.get(t.name, t.name) == CHECK_TASK_STATUS_TOOL_NAME]
    if polls and all(t.source == _BUILTIN_STATUS_POLL_SOURCE for t in polls):
        owned.add(CHECK_TASK_STATUS_TOOL_NAME)
    return frozenset(owned)


@dataclass(frozen=True, slots=True)
class _ToolSurface:
    """The per-run maps the loop derives from its tool list, built in one place.

    ``effective_tools`` is the sanitised list offered to the model;
    ``name_map`` resolves a call name back to the original the executor
    registered; the other maps are keyed by LLM-visible name. A refresh of the
    tools inside the turn (#1874) rebuilds all of them through
    ``_build_tool_surface``, so no map can describe another list.
    """

    effective_tools: list[ToolDefinition] | None
    name_map: _ExecutorNameMap
    schema_map: dict[str, dict[str, Any]]
    coalescing_map: dict[str, WriteCoalescing]
    execution_timeouts: dict[str, int]
    send_guard_map: dict[str, SendGuard]
    read_back_map: dict[str, tuple[WriteReadBack, str]]
    #: ORIGINAL names of the tools whose successful call reloads the surface
    #: (``ToolDefinition.changes_tool_surface``, #1874).
    surface_changing: frozenset[str] = frozenset()


def _build_tool_surface(tools: list[ToolDefinition] | None) -> _ToolSurface:
    """Sanitise *tools* for the provider and derive every per-name map of the run.

    Every dispatch-side lookup of "which registered tool did the model mean"
    goes through ONE map: it canonicalises in the LLM-visible name space first
    (the gate's rules), then reverse-maps to the original (``_ExecutorNameMap``).
    It is built from the FULL sanitised list, before any provider truncation,
    so a call is always resolved against what the model could have been
    offered. The known-alias table is translated into the visible space once
    and handed to that map and, through its resolver, to the gate, so an alias
    can never mean one tool to the gate and another to dispatch.

    The schema map is keyed by the sanitised names and resolved through the
    same canonicalisation, so an ambiguous name validates against no schema,
    never the wrong one.
    """
    renamed: dict[str, str] = {}  # sanitised -> original (renamed only)
    original_to_visible: dict[str, str] = {}  # every original -> its visible name
    effective_tools: list[ToolDefinition] | None = None
    if tools:
        effective_tools = []
        for t, (_orig, clean) in zip(tools, build_llm_tool_names([t.name for t in tools]), strict=True):
            original_to_visible[t.name] = clean
            if clean != t.name:
                renamed[clean] = t.name
                logger.debug("Sanitised tool name '%s' -> '%s' (source: %s)", t.name, clean, t.source)
                effective_tools.append(dataclasses.replace(t, name=clean))
            else:
                effective_tools.append(t)
    name_map = _ExecutorNameMap(
        renamed,
        (t.name for t in effective_tools or []),
        aliases=visible_aliases(original_to_visible, declared_aliases(tools or ())),
    )
    read_back_map: dict[str, tuple[WriteReadBack, str]] = {}
    for td in effective_tools or []:
        if td.read_back is None:
            continue
        original = name_map.get(td.name, td.name)
        suffix = original[len(td.base_name) :] if td.base_name and original.startswith(td.base_name) else ""
        read_back_map[td.name] = (td.read_back, suffix)
    return _ToolSurface(
        effective_tools=effective_tools,
        name_map=name_map,
        schema_map={td.name: td.input_schema for td in effective_tools or []},
        coalescing_map={td.name: td.coalesce for td in effective_tools or [] if td.coalesce is not None},
        execution_timeouts={td.name: td.execution_timeout for td in effective_tools or [] if td.execution_timeout > 0},
        send_guard_map={td.name: td.send_guard for td in effective_tools or [] if td.send_guard is not None},
        read_back_map=read_back_map,
        surface_changing=frozenset(
            name_map.get(td.name, td.name) for td in effective_tools or [] if td.changes_tool_surface
        ),
    )


def _surface_refresh_note(added: Sequence[str], removed: Sequence[str], *, gated: bool) -> str:
    """The system note that tells the model its tool list changed inside the turn (#1874)."""
    parts: list[str] = []
    if added:
        parts.append(f"added {', '.join(added[:_TRUNCATION_NOTICE_MAX_NAMES])}")
        if len(added) > _TRUNCATION_NOTICE_MAX_NAMES:
            parts[-1] += f" and {len(added) - _TRUNCATION_NOTICE_MAX_NAMES} more"
    if removed:
        parts.append(f"removed {', '.join(removed[:_TRUNCATION_NOTICE_MAX_NAMES])}")
        if len(removed) > _TRUNCATION_NOTICE_MAX_NAMES:
            parts[-1] += f" and {len(removed) - _TRUNCATION_NOTICE_MAX_NAMES} more"
    load_hint = f" (through `{LOAD_TOOLS_TOOL_NAME}` when their group is not loaded)" if gated and added else ""
    return (
        "Note: the integration tools of this turn were reloaded after that change: "
        f"{'; '.join(parts)}. The added tools can be called from the next step{load_hint}; "
        "the removed ones no longer exist."
    )


_SURFACE_REFRESH_FAILED_NOTE = (
    "Note: the integration tools could not be reloaded after that change; this turn keeps the "
    "tool list it had, and the next turn reads the new one."
)


def _debited_tool_calls(
    tool_calls: list[ToolCall], name_map: Mapping[str, str], *, pauses: frozenset[str]
) -> list[ToolCall]:
    """The calls of a batch debited from (and in strict mode cut by) the
    per-run tool-call budget: all of them but the batch's first pause the run
    owns (``_run_pause_names``). One pause a batch rides free; another is
    charged like any call, since status polls run in the parallel gather and a
    batch of them would be the fan-out the ceiling exists to bound."""
    free = next((tc for tc in tool_calls if name_map.get(tc.name, tc.name) in pauses), None)
    return [tc for tc in tool_calls if tc is not free]


def _wind_down_points(budget: int) -> tuple[int, int]:
    """Iteration indexes at which the two wind-down stages fire.

    Stage 1 — Wind-down nudge: give the LLM early notice to wrap up.
      Budget > 6:  nudge at max-4 (4 iterations of notice)
      Budget 5–6:  nudge at max-3 (3 iterations of notice)
      Budget ≤ 4:  skip nudge (``-1``), rely on tool stripping + forced answer

    Stage 2 — Tool stripping: remove all tools so the LLM MUST produce text.
      Budget > 3:  strip 2 iterations before the end, keeping a 1-iteration
                   safety buffer between the strip and the forced-answer
                   backstop.  Without it, an LLM that ignored the wind-down
                   nudge AND produced empty text on the stripped iteration
                   would land in the expensive backstop with no margin.
      Budget ≤ 3:  strip on the last iteration (no room for a buffer).

    Recomputed whenever the effective budget changes — a waived wait round
    extends the budget, and stale thresholds would wind the run down
    several iterations before its real end.
    """
    if budget > 6:
        wind_down_at = budget - 4
    elif budget > 4:
        wind_down_at = budget - 3
    else:
        wind_down_at = -1
    strip_tools_at = budget - 2 if budget > 3 else budget - 1
    return wind_down_at, strip_tools_at


# Rounds of wall clock, at the run's own pace, an iteration-budget extension
# must leave for the wind-down that ends the run: the longest notice
# ``_wind_down_points`` gives. Past it, extending would only move the nudges
# beyond the wall clock, which then ends the run without them.
_EXTENSION_WIND_DOWN_ROUNDS = 4

# Strict ASCII ID/slug alphabet for the target-agent log context below.
# ``re.ASCII`` keeps ``\w`` from matching non-ASCII "word" characters
# (Unicode homoglyphs, zero-width letters) that would make log output
# visually confusing while still passing the filter.
_TARGET_AGENT_ID_UNSAFE_RE = re.compile(r"[^\w.:@-]", re.ASCII)


def _agent_tool_target_ctx(tc: ToolCall) -> str:
    """Return extra log context for agent-to-agent tool timeout/error lines.

    For tools that delegate work to a named target agent (consult_agent,
    spawn_background_task, transfer_to_agent) the LLM passes an ``agent_id``
    argument.  Appending it to warning/error log lines lets operators
    correlate a timed-out ``consult_agent`` call in the parent agent's logs
    with the target sub-agent's own execution logs — without having to
    decode the full tool-call arguments blob.

    Returns an empty string for non-agent-tool calls so callers can always
    use ``"… (id=%s%s)" % (tc.id, _agent_tool_target_ctx(tc))`` without
    branching.

    The value is LLM-provided (the schema accepts "ID or slug"), so it is
    sanitised before interpolation: characters outside the strict ASCII
    ID/slug alphabet are replaced and the length is capped, preventing a
    crafted argument from forging extra log lines or blowing up the log
    record.
    """
    if tc.name not in _AGENT_TOOL_NAMES:
        return ""
    # ``agent_id`` comes from parsed JSON, so it can be ``null`` even when
    # the schema says string — fall back to "unknown" rather than logging
    # the misleading literal ``str(None)``.
    raw_val = tc.arguments.get("agent_id")
    raw = str(raw_val) if raw_val not in (None, "") else "unknown"
    target_id = _TARGET_AGENT_ID_UNSAFE_RE.sub("_", raw)[:80]
    return f", target_agent_id={target_id}"


# Terminal content returned when the caller's ``cancel_event`` aborts the
# loop — single source of truth shared by the iteration-start check and the
# mid-call abort paths so downstream handling is identical for both.
_CANCELLED_MESSAGE = "The request was cancelled."


@dataclass
class SteeringMessage:
    """A mid-run steering message with optional file attachments.

    Plain strings remain valid queue items (the historical contract);
    this richer shape carries ``FileRef`` attachments so a screenshot or
    document sent mid-run reaches the model as a true multimodal user
    message instead of being blocked until the turn ends.
    ``files`` holds ``app.llm.base.FileRef`` objects (typed loosely to
    avoid a circular import at module load).
    """

    text: str
    files: list[Any] | None = None


def steering_text(item: str | SteeringMessage) -> str:
    """Return the plain text of a steering queue item (str or rich)."""
    return item.text if isinstance(item, SteeringMessage) else item


def drain_steering_queue(
    queue: asyncio.Queue[str | SteeringMessage] | None,
) -> list[str | SteeringMessage]:
    """Drain *queue* non-blockingly, returning the non-empty steering items.

    Mid-run steering messages (a user message sent while the agent is still
    generating) are pushed onto a per-turn ``asyncio.Queue`` by the chat's
    turn runner.  The agent loop drains the queue at every iteration
    start and injects each item as a ``user`` message (multimodal when the
    item is a :class:`SteeringMessage` with files), so the model
    incorporates the correction at the next safe boundary instead of the
    user having to cancel and restart.  Shared with the chat's turn runner, which
    drains leftovers after the loop returns (messages that arrived during
    the final answer stream) to fold them into an immediate follow-up turn.

    ``None`` and blank entries are skipped; ``queue=None`` returns ``[]``.
    """
    items: list[str | SteeringMessage] = []
    if queue is None:
        return items
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if steering_text(item).strip():
            items.append(item)
    return items


class SteeringQueue(asyncio.Queue):
    """A steering queue that admits what it holds again as the loop takes it.

    The loop drains it at the iteration boundary, after its cancel check, and
    injects what :meth:`admit` returns: an item can stop being deliverable
    while it waits for that boundary - a partner's answer whose work the user
    stopped meanwhile. This base admits everything, like a plain queue.

    ``interactive`` is false for a queue no person waits behind (the notes
    relayed to a run holding a work item): such a run keeps the park
    semantics of an assignment run.
    """

    interactive: bool = True

    async def admit(
        self, items: list[str | SteeringMessage]
    ) -> list[str | SteeringMessage]:
        return items


class _CancelledMidCallError(Exception):
    """Internal: the caller's ``cancel_event`` fired during an in-flight await.

    Raised by :func:`_race_cancellation` (LLM calls / tool execution) and the
    between-chunks check inside :func:`_streaming_chat` so each call site can
    convert the abort into the exact same terminal outcome the iteration-start
    cancellation check produces (a :data:`_CANCELLED_MESSAGE` ChatResponse).
    Never escapes :func:`run_agent_loop`; direct-chat callers that invoke
    :func:`_streaming_chat` / :func:`_continue_truncated_response` outside the
    loop (``agent_chat`` / ``general_chat`` no-tools paths) catch it themselves
    and substitute :func:`_cancelled_chat_response`.
    """


class _AnswerCutError(Exception):
    """Internal: the degenerate-repetition stream cut aborted an in-flight call.

    Raised by :func:`_race_cancellation` and :func:`_streaming_chat` once the
    :class:`_AnswerStreamCut` the call streams through (``stream``) cuts, so a
    runaway stops generating instead of running to its ``max_tokens``. The
    call is aborted as a user cancel aborts it (task cancelled, stream
    closed), outside the provider's code, so the failover wrapper never takes
    it for a provider failure. Unlike :class:`_CancelledMidCallError` the run
    is not cancelled: the caller closes the generation with
    :func:`_end_aborted_generation` and answers with the text shown
    (:meth:`_AnswerStreamCut.aborted_response`), which the guard ends.
    """

    def __init__(self, stream: _AnswerStreamCut) -> None:
        super().__init__("the answer stream cut a degenerate repetition loop")
        self.stream = stream


#: Seconds the wait card's description of the watched ids may take: it is
#: cosmetic, and the pause must not wait on it.
_WAIT_ITEMS_TIMEOUT = 3.0


async def _watched_items(
    condition: WaitCondition | None, ids: list[str]
) -> list[dict] | None:
    """What the wait card shows of each watched id, or ``None`` when unknown.

    Read through the condition's ``details`` hook
    (``builtin_tools.wait.make_task_condition``): kind, label, status and
    start of each background task or assignment run. Bounded and never
    raises - a card without them still counts down.
    """
    details = getattr(condition, "details", None) if condition is not None else None
    if not ids or not callable(details):
        return None
    try:
        return await asyncio.wait_for(details(ids), timeout=_WAIT_ITEMS_TIMEOUT)
    except Exception:
        logger.debug("Agent loop: could not describe the watched ids", exc_info=True)
        return None


def _wait_start_frame(
    tool_name: str,
    iteration: int,
    grant: Any,
    args: dict,
    items: list[dict] | None,
    wait_budget: WaitBudget,
) -> dict:
    """The ``wait_start`` frame of a settled pause: the chat's countdown, reason and watched items."""
    return {
        "status": "wait_start",
        "tool": tool_name,
        "iteration": iteration + 1,
        "seconds": round(grant.granted, 1),
        "requested_seconds": grant.requested,
        "clamped": grant.clamped,
        "reason": args.get("reason"),
        "until": args.get("until"),
        "items": items,
        "remaining_wait_seconds": round(wait_budget.remaining_total(), 1),
    }


class _WaitCard:
    """The wait card of one pause: the ``wait`` tool's and the implicit park's alike.

    :meth:`items` is what ``wait_start`` carries; :meth:`report` is
    ``execute_wait``'s ``on_status`` hook, which sends ``wait_status`` with
    fresh items when a poll sees a watched id in another state than the card
    shows.
    """

    def __init__(
        self,
        condition: WaitCondition | None,
        ids: list[str],
        on_event: OnEventCallback | None,
        tool_name: str,
    ) -> None:
        self._condition = condition
        self._ids = ids
        self._on_event = on_event
        self._tool_name = tool_name
        self._shown: dict[str, str] = {}

    async def items(self) -> list[dict] | None:
        items = await _watched_items(self._condition, self._ids)
        self._shown.update({str(item.get("id")): str(item.get("status")) for item in items or ()})
        return items

    async def report(self, statuses: dict[str, str]) -> None:
        if not statuses or all(self._shown.get(tid) == state for tid, state in statuses.items()):
            return
        items = await self.items()
        if items is not None:
            await _emit(self._on_event, {"status": "wait_status", "tool": self._tool_name, "items": items})


async def _execute_wait_calls(
    wait_calls: list[ToolCall],
    *,
    iteration: int,
    messages: list,
    on_event: OnEventCallback | None,
    wait_budget: WaitBudget,
    wall_clock_deadline: float | None,
    wait_skip_event: asyncio.Event | None,
    wait_condition: WaitCondition | None,
    cancel_event: asyncio.Event | None,
    track_tool_result: Callable[..., None],
    pending_input: Callable[[], bool] | None = None,
    report_tool_durations: bool = False,
    stopping: Callable[[], bool] | None = None,
) -> tuple[bool, list[str], float]:
    """Run in-loop ``wait`` calls and append their tool results.

    Returns ``(paused, handoff_ids, paused_elapsed)``. Raises
    :class:`_CancelledMidCallError` if the user cancelled during a pause.
    ``pending_input`` (the loop's steering-queue check) refuses a pause up
    front when a user message is already queued — see ``execute_wait``.
    ``stopping`` ends a pause once the worker winds down, the call answered
    and checkpointed — see ``execute_wait``.
    """
    from app.llm.base import ChatMessage as LLMMessage

    paused = False
    handoff_ids: list[str] = []
    paused_elapsed = 0.0
    for wtc in wait_calls:
        wait_args = wtc.arguments or {}
        watched = parse_until_task_ids(wait_args.get("until"))
        card = _WaitCard(wait_condition, watched, on_event, wtc.name)

        async def _announce_wait(
            grant, _tc=wtc, _args=wait_args, _iter=iteration, _card=card
        ):
            """Tell the UI how long the pause really is, and what it watches.

            Fired once the grant is settled, so the countdown ticks the
            GRANTED duration — a clamped 10m request must not render a
            10-minute countdown the run will never honour.
            """
            await _emit(
                on_event,
                _wait_start_frame(
                    _tc.name, _iter, grant, _args, await _card.items(), wait_budget
                ),
            )

        _wait_started = time.perf_counter()
        try:
            wait_result = await _race_cancellation(
                execute_wait(
                    wait_args,
                    budget=wait_budget,
                    remaining_wall_clock=(
                        wall_clock_deadline - time.monotonic()
                        if wall_clock_deadline is not None
                        else None
                    ),
                    skip_event=wait_skip_event,
                    condition=wait_condition,
                    on_grant=_announce_wait,
                    pending_input=pending_input,
                    on_status=card.report,
                    stopping=stopping,
                ),
                cancel_event,
                timeout=WAIT_TOOL_TIMEOUT,
            )
        except TimeoutError:
            # Unreachable in practice: a wait is clamped to the policy
            # ceiling, which sits below WAIT_TOOL_TIMEOUT. Surface it as a
            # tool result rather than killing the run, so a hung condition
            # poll degrades gracefully.
            wait_result = _WAIT_TIMEOUT_RESULT
        paused = paused or wait_result.paused
        if wait_result.paused:
            paused_elapsed += wait_result.elapsed
        until_ids = watched
        if wait_result.reason == WAIT_REASON_HANDOFF:
            handoff_ids = until_ids or handoff_ids
        elif (
            wait_result.reason
            in (WAIT_REASON_ELAPSED, WAIT_REASON_REFUSED)
            and wait_budget.remaining_total() < MIN_GRANTED_WAIT_SECONDS
            and until_ids
        ):
            # Last useful pause is spent and the named children are still
            # running: do not spend another LLM round that will only refuse
            # the next wait.
            handoff_ids = until_ids
        is_wait_error = wait_result.reason == WAIT_REASON_INVALID
        messages.append(
            LLMMessage(
                role="tool",
                content=wait_result.message,
                tool_call_id=wtc.id,
                name=wtc.name,
                # A pause cut by the worker stopping: what the resumed turn
                # finishes it from (:func:`_continue_cut_wait`).
                wait_cut=wait_result.cut,
            )
        )
        track_tool_result(
            wtc.name,
            is_error=is_wait_error,
            result_content=wait_result.message,
        )
        await _emit(
            on_event,
            {
                "status": "tool_done",
                **_tool_timing(report_tool_durations, _elapsed_ms(_wait_started)),
                "tool": wtc.name,
                "success": not is_wait_error,
                "wait_reason": wait_result.reason,
                "wait_elapsed": round(wait_result.elapsed, 1),
                "tool_input": _serialize_tool_arguments(wait_args),
                "tool_input_full": _serialize_tool_arguments_full(wait_args),
                "tool_output": wait_result.message,
                "tool_output_full": wait_result.message,
            },
        )
    return paused, handoff_ids, paused_elapsed


async def _continue_cut_wait(
    messages: list,
    *,
    on_event: OnEventCallback | None,
    wait_budget: WaitBudget,
    wall_clock_deadline: float | None,
    wait_skip_event: asyncio.Event | None,
    wait_condition: WaitCondition | None,
    cancel_event: asyncio.Event | None,
    pending_input: Callable[[], bool] | None,
    stopping: Callable[[], bool] | None,
) -> float | None:
    """Finish, before the first model call, the ``wait`` a stopping worker cut.

    A turn handed back during a pause resumes with that pause answered
    "cut short": without this, its first model generation is spent only on
    calling ``wait`` again for the rest. The rest is waited here instead:
    the grant less the time since the pause started
    (``wait_policy.cut_wait_remaining``, the pause made before the cut
    charged to the budget), bounded by the policy like any wait, on the same
    ``until`` ids, so a target that finished while the worker was down ends
    it at once. The cut result is replaced in place by one result covering
    the whole pause: the model sees one call and one answer. Cut again, the
    new result carries the same start, for the next attempt.

    Returns the seconds waited here, ``None`` when there was nothing to
    continue. Raises :class:`_CancelledMidCallError` on a cancellation.
    """
    found = cut_wait_to_continue(messages)
    if found is None:
        return None
    index, message = found
    cut = dict(message.wait_cut or {})
    remaining = cut_wait_remaining(cut, now=time.time(), budget=wait_budget)
    arguments: dict[str, Any] = {"duration": str(max(1, math.ceil(remaining)))}
    if cut.get("until"):
        arguments["until"] = ",".join(str(i) for i in cut["until"])
    if remaining < MIN_GRANTED_WAIT_SECONDS and not cut.get("until"):
        # Already over while the worker was down: report the pause as elapsed.
        arguments["duration"] = "1"
    # The chat shows the continued pause like any wait (reason, countdown,
    # watched items, Skip): without this frame the last status the turn sent
    # ("Connecting integrations") stays on screen for the whole pause.
    original = _cut_call_arguments(messages, message.tool_call_id)
    card = _WaitCard(wait_condition, list(cut.get("until") or []), on_event, message.name)

    async def _announce_continued(grant) -> None:
        shown = {**arguments, "reason": original.get("reason"), "until": arguments.get("until")}
        await _emit(on_event, _wait_start_frame(message.name, 0, grant, shown, await card.items(), wait_budget))

    result = await _race_cancellation(
        execute_wait(
            arguments,
            budget=wait_budget,
            remaining_wall_clock=(
                wall_clock_deadline - time.monotonic() if wall_clock_deadline is not None else None
            ),
            skip_event=wait_skip_event,
            condition=wait_condition,
            on_grant=_announce_continued,
            pending_input=pending_input,
            on_status=card.report,
            stopping=stopping,
            carried=cut,
        ),
        cancel_event,
        timeout=WAIT_TOOL_TIMEOUT,
    )
    messages[index] = type(message)(
        role="tool",
        content=result.message,
        tool_call_id=message.tool_call_id,
        name=message.name,
        wait_cut=result.cut,
    )
    logger.info(
        "Resumed turn continued the wait its stopping worker cut: %.0fs left of %.0fs granted, "
        "waited %.1fs more (%s)",
        remaining,
        float(cut.get("granted") or 0.0),
        result.elapsed,
        result.reason,
    )
    await _emit(
        on_event,
        {
            "status": "wait_continued",
            "tool": message.name,
            "wait_reason": result.reason,
            "wait_elapsed": round(result.elapsed, 1),
            "remaining_seconds": round(remaining, 1),
        },
    )
    return result.elapsed


def _cut_call_arguments(messages: list, call_id: str | None) -> dict:
    """The arguments of the ``wait`` call *call_id* (its ``reason`` for the chat), ``{}`` when not found."""
    for message in reversed(messages):
        for call in getattr(message, "tool_calls", None) or ():
            if call.id == call_id:
                return dict(call.arguments or {})
    return {}


def _round_of_call(messages: list, call_id: str | None) -> list:
    """The assistant message that made the call *call_id* and every result of its round."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if getattr(message, "role", None) != "assistant":
            continue
        ids = {call.id for call in getattr(message, "tool_calls", None) or ()}
        if call_id not in ids:
            continue
        round_: list = [message]
        for later in messages[index + 1 :]:
            if getattr(later, "role", None) == "tool" and getattr(later, "tool_call_id", None) in ids:
                round_.append(later)
        return round_
    return []


def _waiting_on_tasks_response(
    model: str | None,
    usage: Any,
    task_ids: list[str],
    content: str,
):
    """Terminal park response: *content* is the already-resolved user text.

    The caller builds it once with :func:`resolve_task_labels` (an async
    lookup) and streams the same string, so the streamed park and the
    persisted reply can never disagree.
    """
    from app.llm.base import ChatResponse

    return ChatResponse(
        content=content,
        model=model or "unknown",
        usage=usage,
        stop_reason="waiting_on_tasks",
        pending_task_ids=task_ids,
    )


def _cancelled_chat_response(
    model: str | None, usage: dict | None = None
) -> ChatResponse:
    """Build the terminal response for a user-cancelled request.

    Mirrors the iteration-start cancellation check exactly so callers see one
    consistent outcome regardless of WHERE in the iteration the cancel landed.
    *usage* is what the run spent up to the cancel, carried like every other
    terminal response so the root trace and the run's token totals keep it.
    """
    from app.llm.base import ChatResponse

    # ``stop_reason`` carries the cancellation structurally so a caller
    # can tell "the user stopped this" from "the agent answered" without
    # matching on the message text.
    return ChatResponse(
        content=_CANCELLED_MESSAGE,
        model=model or "unknown",
        usage=usage if usage is not None else {},
        stop_reason="cancelled",
    )


def _end_aborted_generation(
    gen_ctx: dict | None,
    error: BaseException,
    usage_sink: StreamUsageSink | None,
    merge_usage: UsageHook,
) -> dict:
    """Close the generation of a call that did not return - cancelled, timed
    out, failed or cut - with the usage its provider reported before the end,
    which it returns.

    It feeds the two sinks a returned call feeds, the generation span and the
    loop's usage hook (the run budget and the usage the run reports), so the
    trace and the budget agree. The sink hands its usage out once and a call
    that returned settled it, so no call is counted twice. With nothing
    reported (a non-streamed call, a stream stopped before its provider's
    first usage event) the span closes with the error alone, as before -
    unless the model declined (``ModelDeclinedError``): a decline is billed,
    and the error carries what it consumed. A call a failover fallback was
    serving is closed, and its usage recorded, under that fallback's model,
    which the wrapper noted on the sink.
    """
    served_model = getattr(usage_sink, "served_model", None)
    if not (isinstance(served_model, str) and served_model):
        served_model = None
    usage = usage_sink.take() if usage_sink is not None else {}
    if not usage and isinstance(error, ModelDeclinedError):
        usage = dict(error.usage)
    if gen_ctx is not None:
        close_kwargs: dict[str, Any] = {"usage": usage} if usage else {}
        if served_model:
            close_kwargs["model"] = served_model
        _lf_end_generation(gen_ctx, error=error, **close_kwargs)
    if usage:
        # Runs inside the handler of *error*: a failure here must never
        # replace the error being handled, a cancellation least of all.
        try:
            merge_usage(usage, served_model=served_model)
        except Exception:
            logger.exception("Could not record the usage of an aborted LLM call")
    return usage


def _end_cut_call(
    gen_ctx: dict | None,
    error: _AnswerCutError,
    usage_sink: StreamUsageSink,
    merge_usage: UsageHook,
    model: str | None,
) -> ChatResponse:
    """Close a call the stream cut aborted, keeping the usage it reported, and
    answer with what was shown, for the guard to end the run on."""
    served_model = usage_sink.served_model
    usage = _end_aborted_generation(gen_ctx, error, usage_sink, merge_usage)
    return error.stream.aborted_response(model=served_model or model, usage=usage)


async def _timeout_retry_backoff(cancel_event: asyncio.Event | None) -> None:
    """Sleep the LLM-timeout retry backoff, waking early on user cancel.

    A plain ``asyncio.sleep`` would delay cancellation handling by up to
    ``_LLM_TIMEOUT_RETRY_BACKOFF_SECS`` after a timed-out call.  Waiting on
    *cancel_event* (bounded by the backoff) wakes immediately when the user
    cancels; the iteration-start cancel check right after the caller's
    ``continue`` then returns the standard cancelled response.
    """
    if cancel_event is None:
        await asyncio.sleep(_LLM_TIMEOUT_RETRY_BACKOFF_SECS)
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(
            cancel_event.wait(), timeout=_LLM_TIMEOUT_RETRY_BACKOFF_SECS
        )


async def _race_cancellation(
    awaitable: Awaitable[Any],
    cancel_event: asyncio.Event | None,
    *,
    timeout: float | None = None,
    answer_cut: _AnswerStreamCut | None = None,
) -> Any:
    """Await *awaitable* (with optional *timeout*) racing *cancel_event*.

    With ``cancel_event=None`` this is an exact passthrough to
    ``asyncio.wait_for`` (or a bare ``await``) — zero behaviour change for
    callers that don't support cancellation (assignments, external channels).

    When the event fires first, the in-flight task is cancelled and awaited
    (suppressing ``CancelledError`` so no "exception was never retrieved"
    warnings leak) and :class:`_CancelledMidCallError` is raised.  Cancelling a
    tool mid-flight may leave external side effects — that is acceptable and
    matches user intent.  When neither side completes within *timeout*,
    ``TimeoutError`` is raised to mirror ``asyncio.wait_for`` semantics so the
    existing timeout handlers at each call site keep working unchanged.

    *answer_cut*, the :class:`_AnswerStreamCut` the call streams through,
    aborts it the same way once it cuts, raising :class:`_AnswerCutError`; a
    user cancel seen at the same time wins.
    """
    if cancel_event is None and answer_cut is None:
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout)

    task = asyncio.ensure_future(awaitable)
    # Each event's waiter with the error its firing raises, a user cancel first.
    waiters: list[tuple[asyncio.Future, Callable[[], Exception]]] = []
    if cancel_event is not None:
        waiters.append((asyncio.ensure_future(cancel_event.wait()), _CancelledMidCallError))
    if answer_cut is not None:
        waiters.append(
            (
                asyncio.ensure_future(answer_cut.stopped.wait()),
                functools.partial(_AnswerCutError, answer_cut),
            )
        )
    futures = [task, *(waiter for waiter, _error in waiters)]
    try:
        done, _pending = await asyncio.wait(
            futures,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return task.result()  # propagates the call's own exception
        for waiter, error in waiters:
            if waiter in done:
                raise error()
        # Neither completed — overall timeout, mirror asyncio.wait_for.
        raise TimeoutError(f"call did not complete within {timeout}s")
    finally:
        for fut in futures:
            if not fut.done():
                fut.cancel()
        # Drain every future unconditionally — not just the pending ones —
        # so the event loop never logs a "Task exception was never
        # retrieved" warning.  This covers the race where the losing task
        # finishes with an exception after ``asyncio.wait`` returned but
        # before this cleanup runs (``fut.done()`` is already True, yet its
        # exception was never consumed).  ``return_exceptions=True`` makes
        # the drain side-effect-only: results and errors of the abandoned
        # call are irrelevant here.
        await asyncio.gather(*futures, return_exceptions=True)


_REPEATED_FAILURE_NUDGE = (
    "[SYSTEM — repeated tool failure] "
    "The tool '{tool_name}' has failed in {count} turn(s) in a row. "
    "Do NOT call this tool again with the same approach. "
    "Either: (1) carefully re-read the error messages and fix ALL missing/incorrect "
    "parameters, OR (2) use a DIFFERENT tool or strategy to accomplish your goal. "
    "Do not repeat the same failing call."
)

# Maximum consecutive failed TURNS of the same tool before it is flagged.
# Every streak below counts model turns, not calls: the parallel calls of one
# turn come from one decision the model took before it saw any of their
# results, so all of them failing is ONE failure toward the nudge, the
# cooldown and the brake (5 parallel lookups with a malformed id used to
# withdraw every tool on the first batch, before the model could switch method).
_MAX_TOOL_FAILURES = 2

# Consecutive-same-tool-failure brake (P4.1).  The nudge +
# cooldown above only *suggests* a different approach — the 2026-07-09
# incident turn kept grinding failing Metabase SQL for 30+ minutes anyway.
# Once ONE tool accumulates this many consecutive failures, the loop forces
# the Stage-2 wind-down (tools stripped) so the model must produce an honest
# "I could not get this working" answer instead of burning the remaining
# iteration budget on the same broken call.
_TOOL_FAILURE_BRAKE_THRESHOLD = 4

# The cumulative backstop of the brake for work a tool marks rejected
# (``ToolResult.outcome_failure``): those are counted per CALL above, so
# a model rewording each paid generation a little would never be braked. This
# many rejected results of one tool with no success of it in between wind the
# turn down the same way, whatever their arguments.
_OUTCOME_FAILURE_BRAKE_THRESHOLD = 8

_OUTCOME_FAILURE_BRAKE_NUDGE = (
    "[SYSTEM — rejected work, stopping] "
    "{count} results of '{tool_name}' in a row came out rejected, each asked "
    "differently, and each was a paid attempt. Tool access is being withdrawn "
    "for the rest of this turn. Produce your final answer NOW: say that "
    "{count} attempts were rejected and why, share what came out usable, and "
    "suggest what the user could change (another model, another approach) "
    "before asking again."
)

_TOOL_FAILURE_BRAKE_NUDGE = (
    "[SYSTEM — repeated tool failure, stopping] "
    "The tool '{tool_name}' has failed in {count} turns in a row. Tool access "
    "is being withdrawn for the rest of this turn. Produce your final "
    "answer NOW: state plainly that you could not get '{tool_name}' "
    "working, summarize what you tried and the errors you hit, share any "
    "partial results already gathered, and suggest what the user could "
    "check or try next."
)

# Wall-clock budget-warning nudge (P4.2).  When a turn
# crosses this fraction of its wall-clock budget, a one-shot user-visible
# warning + system nudge is emitted so the model starts consolidating
# instead of exploring — mirroring the existing iteration-budget wind-down
# but keyed on elapsed TIME (long LLM calls / slow tools burn wall clock
# without burning iterations).
_WALL_CLOCK_NUDGE_FRACTION = 0.5

_WALL_CLOCK_NUDGE = (
    "[SYSTEM — time budget warning] More than half of the wall-clock time "
    "budget for this run has been used. Stop exploring new approaches. "
    "Prioritize consolidating what you already have: finish the most "
    "critical remaining step (if any), then produce the final answer. If "
    "something has repeatedly failed, report it honestly instead of "
    "retrying."
)

# Per-run spending-budget warning nudge.  One-shot per dimension —
# see ``RunBudget.take_warning`` — and deliberately worded like the
# wall-clock nudge above: the model cannot see the budget, so the only way it
# can spend the remainder well is to be told the remainder is small.  Without
# this, a hard budget block lands as an abrupt cut-off mid-exploration.
_BUDGET_NUDGE = (
    "[SYSTEM — run budget warning] This run is close to its spending budget "
    "(cost / tokens / tool calls). Stop exploring new approaches and stop "
    "issuing optional tool calls. Consolidate what you already have into the "
    "final answer now, and report anything still unresolved honestly instead "
    "of retrying it."
)

# ── Capability-not-found tracking ────────────────────────────────────
# ``TOOL_NOT_AVAILABLE_SENTINEL`` (imported from ``app.tools.base``, where
# ``ScopedToolExecutor`` AND ``CompositeToolExecutor`` build their rejections
# from the same constant) is used to aggregate "not available"
# failures across different hallucinated names — the per-tool
# ``_MAX_TOOL_FAILURES`` counter does not help when the LLM cycles through a
# dozen variants of the same non-existent capability.  A name that is a real
# built-in tool the agent does not carry is told so on its FIRST refusal
# (``describe_unavailable_builtin``); the nudge below is the backstop for a
# model still guessing after several distinct names.

# After this many *distinct* tool names are rejected as "not available",
# inject the capability-not-found nudge so the LLM stops guessing names.
_MAX_NOT_AVAILABLE_DISTINCT_NAMES = 3

_CAPABILITY_NOT_FOUND_NUDGE = (
    "[SYSTEM — capability not found] "
    "You have tried {count} different tool name(s) that are not available in this "
    "context: {tried_names}. "
    "Stop guessing alternative names for the same operation — none of those tools "
    "exist in this run. "
    "The ONLY tools available to you right now are: {available_tools}. "
    "If none of the available tools can accomplish your goal, clearly state that "
    "the requested capability is not available and do NOT attempt any further "
    "tool calls for it."
)

# ── Identical tool-call deduplication (CORE-6) ───────────────────────
# Tools that are legitimately repeatable with identical arguments —
# polling tools whose result changes between calls even though the
# arguments do not, stateful tools whose output depends on hidden
# session state mutated by earlier calls, and the tool whose whole purpose
# is to put a stored file BACK into the working context.  Calls to these
# are never deduplicated.
DEDUP_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {
        "check_task_status",
        "get_task_result",
        "collect_task_results",
        # The built-in browser keeps a persistent session across calls:
        # navigate → click → get_content legitimately repeats
        # {"action": "get_content"} with identical arguments and must see
        # the CURRENT page, not a stale cached snapshot.
        "browser",
        # ``read_file_content`` is the recovery path every dropped-files note
        # and dedup nudge names ("view it again with read_file_content"), and
        # it is called again precisely BECAUSE its previous result is no
        # longer in context.  Served from the cache it would replay the text
        # of that earlier result — with a self-referential nudge telling the
        # model to call read_file_content instead of read_file_content — and
        # the recovery path the notes advertise would be a dead end.
        # Re-executing it is cheap: the
        # image path returns the stored ``FileRef``, the text path re-reads
        # the stored bytes, and an identical text body is collapsed by
        # ``_collapse_redundant_tool_results`` like any other duplicate.
        "read_file_content",
    }
)

# A polling tool of an integration or a built-in tool declares
# ``ToolDefinition.live_state`` (#5303): its name is never read for it. The
# cache entries expire (``_DEDUP_CACHE_TTL_S``) and a real pause clears them,
# so an undeclared poll across a ``wait`` still reads live state.

# Arguments that name the OUTPUT of a tool without changing what it returns
# — an optional "save the download under this name" override — are declared by
# the tool's owner (``ToolDefinition.cosmetic_arguments``, #5303). Two calls
# that differ only in such an argument fetch the same bytes, so they share one
# dedup key (and one near-duplicate count): a model steered to "try different
# arguments" otherwise renames the output (``v1.png``, ``v1b.png``, ...
# ``v1-final.png``) and re-downloads the same file every time, which is
# exactly how trace 722375f1 persisted 84 byte-identical PNGs. Per tool on
# purpose — a generic "drop any argument called filename" rule would collapse
# tools for which the filename SELECTS the file.

# Defensive cap on the per-run dedup cache — once full, new results are
# simply not cached (existing entries keep serving repeats).
_DEDUP_CACHE_MAX_ENTRIES = 200

# Age bound of a cached read. The cache stops a model stuck re-asking the
# same question in one stretch of work; it must never answer a poll. A read
# older than this executes again (the PR may have merged, the CI finished),
# and a pause that really waited clears the whole cache (see the loop's
# ``_forget_reads_after_pause``), so a CI watch or a merge read-back across a
# ``wait`` always reads live state.
_DEDUP_CACHE_TTL_S = 120.0

# Window of a tool that declares ``ToolDefinition.live_read`` (a pull
# request, its checks, a CI run, a work item: state other actors move). Its
# cached answer lives this long instead of ``_DEDUP_CACHE_TTL_S``, and the
# loop breaker forgets an ask of it older than this: the same read minutes
# later is a new question, the same read seconds apart is still a loop.
_LIVE_READ_WINDOW_S = 30.0

# A cache hit replays the FULL cached payload under one header. The
# curation may have replaced the earlier copy with the stub that told the
# model to re-call, so the replay never points back at "the previous result"
# (the very stub that sent it here): the payload IS the answer, and the header
# is true whether or not an earlier copy still stands.
_DEDUP_CACHE_MARKER = (
    "[Identical call already executed this turn - the result is unchanged and "
    "repeated in full below]"
)
# An intra-batch duplicate of a call that FAILED mirrors the failure, not a
# result: its own header, so a failure never reads as "repeated in full".
_DEDUP_BATCH_FAILURE_MARKER = "[Identical call in this batch]"
# The nudge must never invite a cosmetic argument change: for a download or
# capture tool "different arguments" means a different output name, which
# defeats the cache and re-fetches the same bytes. When the previous result
# carried a file that curation has since dropped from the context, the way to
# see it again is ``read_file_content`` on the file id the result named.
_DEDUP_CACHE_NUDGE = (
    "If it carried a file or image that is no longer visible, open it with "
    "read_file_content(file_id=...) on the id it named rather than calling this tool again."
)

# ── Call order of a stateful tool in a parallel round (#12182) ───────
# A round's calls run together (``asyncio.gather``). The calls of a tool that
# declares ``ToolDefinition.ordered_calls`` (the built-in browser: one session
# whose page each call changes) run one after another, in the order the model
# sent them: a navigate and the evaluate sent with it raced, and the evaluate
# read the page the navigate was leaving. The round's other calls stay parallel.


def _call_order_chain(
    calls: list[tuple[int, str]], ordered: frozenset[str]
) -> dict[int, tuple[asyncio.Event | None, asyncio.Event]]:
    """For each call *(index, tool name)* of an ordered tool: the event of the call of that tool before it
    (``None`` for the first) and its own, set when it ends. Calls of other tools are left out."""
    chain: dict[int, tuple[asyncio.Event | None, asyncio.Event]] = {}
    last: dict[str, asyncio.Event] = {}
    for index, name in calls:
        if name not in ordered:
            continue
        done = asyncio.Event()
        chain[index] = (last.get(name), done)
        last[name] = done
    return chain


async def _in_call_order(
    slot: tuple[asyncio.Event | None, asyncio.Event] | None, run: Callable[[], Awaitable[Any]]
) -> Any:
    """Run *run* once the call before it in its chain ended (:func:`_call_order_chain`), whatever that call's
    outcome, and mark it ended whatever its own; a call in no chain (*slot* ``None``) runs at once."""
    if slot is None:
        return await run()
    previous, done = slot
    try:
        if previous is not None:
            await previous.wait()
        return await run()
    finally:
        done.set()


# ── Near-duplicate read guard ────────────────────────────────────────
# Steering, not enforcement: after a read tool has been called this many times
# with the same EFFECTIVE arguments (cosmetic ones removed) and no write tool
# ran in between, a one-shot system nudge names the tool, the count and the
# ``read_file_content`` path. The call itself is never blocked — a legitimate
# re-read after context clearing must still work — and the count is per
# ``(tool, effective arguments)`` while the nudge is one-shot per tool.
_MAX_NEAR_DUPLICATE_CALLS = 3

_NEAR_DUPLICATE_NUDGE = (
    "[SYSTEM — repeated identical read] "
    "You have called '{tool_name}' {count} times this run with the same "
    "effective arguments (only output-naming arguments such as a filename "
    "differed, if anything). The data has not changed: the result you already "
    "have IS the result. If that result carried a file or image that is no "
    "longer visible in this context, open it with "
    "read_file_content(file_id=...) with the file_id named in that result instead of "
    "downloading or capturing it again. Do not repeat this call."
)

# ── Loop breaker ─────────────────────────────────────────────
# The near-duplicate guard above steers reads that the dedup cache serves;
# it never counts a dedup-EXEMPT tool (``read_file_content``, ``browser``) or
# a write, and it never blocks. The 25 Sep 2026 run showed what that leaves
# open: ~40 ``read_file_content`` calls over three file ids, interleaved with
# ``edit_presentation`` and ``browser`` calls, each one answered, none of
# them stopping the next. The breaker counts EVERY dispatched call this run
# in two shapes and escalates: a system message when a count reaches the
# warn bound, a refusal (the call never reaches the executor) at the refuse
# bound. Both bounds come from settings (``agent_loop_identical_call_*`` /
# ``agent_loop_similar_call_*``); ``0`` disables one.
#
# * ``identical`` - same tool, same canonical arguments (the dedup key).
# * ``similar``   - same tool, same TARGET, other arguments varying: the
#   target is the ``file_id`` / ``url`` the call names, so a re-read that
#   changes only ``offset`` / ``max_chars`` to defeat a cache is one target
#   read N times. Bounded higher than ``identical``: paging through a long
#   file IS a series of similar calls, and one that is going somewhere.
#
# Three rules keep legitimate repetition out of the count. A WRITE resets the
# counts of every check - every tool that is not a write BY ITS NAME, an
# unknown verb included (a re-read after a mutation is a new question - the
# near-duplicate guard has the same rule); a STATEFUL tool's count
# resets when the same tool is called with OTHER arguments in between - two
# ``browser(action=screenshot)`` around a ``browser(action=navigate)`` are two
# pages, two back-to-back are one page twice; and an ask whose ANSWER the
# harness has since taken away is forgiven: a count is the number of
# asks whose answer the model still has in front of it. The tool-result
# curation clears an older large text result with a stub that reads "the
# result was not empty ... it is re-fetchable. Re-call the tool before stating
# anything about it", and compaction drops old turns outright - a re-read after either is
# the re-read the harness asked for, not a loop. A breaker that counted it
# would tell a run reading six source files through
# ``get_github_file`` "the same tool call is being repeated" for
# re-reading the two the curation had cleared, and a fifth re-read would be
# refused with "the result would not differ from the ones you already
# have" - about a result that was gone. Every ask therefore keeps the tool
# message that answered it (``record_answer``), and ``screen`` first drops the
# asks whose answer the HARNESS took away: the body replaced by the
# re-fetchable stub (a stub-shaped body the tool itself returned is unchanged
# and is not a clearing), or the message object dropped from the transcript
# (semantic compaction, the emergency prune's eviction). A file-bearing
# result's stub (``read_file_content`` is its recovery path, never a re-call),
# a ``[Duplicate ...]``, a ``[Pruned ...]`` and an in-place ``[Truncated ...]``
# body keep counting, and so does an ask the breaker REFUSED whatever later
# happens to the refusal (it carried no data; forgiving it would hand the
# model a fresh run to the bound). Polling tools are never counted: an
# unchanged ``check_task_status`` argument is the point of polling.
#
# A refused call is answered like a validation failure - a tool result the
# model reads, counted toward the consecutive-failure brake - so a model that
# ignores the refusal and asks again is wound down by the brake, and every
# event opens a ``loop`` span (``start_loop_span``) so the Trace Explorer can
# count how often the breaker fires and on which tool.
_LOOP_BREAKER_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {
        "check_task_status",
        "get_task_result",
        "get_full_task_result",
        "collect_task_results",
        "list_background_tasks",
    }
)
#: Arguments whose value is the TARGET of a call, for the ``similar`` shape.
#: Order is the lookup order: the first one present on the call wins.
_LOOP_BREAKER_TARGET_ARGUMENTS: tuple[str, ...] = ("file_id", "url")
#: Tools whose identical-argument count resets when the same tool is called
#: with other arguments in between (a persistent session: what a screenshot
#: shows depends on the navigate before it). ``browser_control`` is the
#: runner's real browser, the same single-tool session as ``browser``.
_LOOP_BREAKER_STATEFUL_TOOLS: frozenset[str] = frozenset({"browser", "browser_control"})
#: Arguments that name a POSITION in the target. A call that names a
#: position no held ask on the same target read is a PAGE -
#: ``read_file_content(offset=0)``, ``(offset=50000)``, ``(offset=100000)``
#: is one file read front to back, not one target read three times - and is
#: exempt from the ``similar`` count. Re-reading a position the model still
#: holds, or varying only ``max_chars`` at the same offset (the
#: cache-defeat pattern), is a repeat on the target and is counted; a position
#: whose answer the harness took away is free again, like the ask itself.
#: The first one present wins.
_LOOP_BREAKER_PAGING_ARGUMENTS: tuple[str, ...] = (
    "offset",
    "start_index",
    "start_at",
    "skip",
    "page",
    "page_number",
    "cursor",
    "start_cursor",
    "next_cursor",
    "page_token",
    "after",
    "start",
)
#: Paging arguments that name a position only with a NUMBER: ``start`` is a
#: timestamp as often as an index, and a time-range scan is not paging.
_LOOP_BREAKER_NUMERIC_ONLY_PAGING_ARGUMENTS: frozenset[str] = frozenset({"start"})
#: Tools that address a PART of their target with arguments of their own:
#: each distinct combination is a new position, like a page. The tool this PR
#: steers the model to for embedded data reads one JSONPath of a file per call,
#: and ten different questions about one plan are not a loop.
_LOOP_BREAKER_TOOL_POSITION_ARGUMENTS: dict[str, tuple[str, ...]] = {"query_file_json": ("path", "block")}
#: What one position IS for such a tool, in the page messages (singular, plural).
_LOOP_BREAKER_PART_NOUNS: dict[str, tuple[str, str]] = {"query_file_json": ("query", "queries")}
#: A date / date-time value is never a page position, whatever the argument
#: is called (``after="2026-09-01"``): each window is a different question.
_LOOP_BREAKER_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\Z")
#: A longer "cursor" is data, not a position token (bounds the position sets).
_LOOP_BREAKER_PAGE_TOKEN_MAX_CHARS = 512
#: The position of a call on a target that names none: the start. So
#: ``read_file_content(file_id=X)`` then ``(file_id=X, offset=0)`` is one
#: position read twice, never a page.
_LOOP_BREAKER_IMPLICIT_START = 0.0
#: A page is exempt from the ``similar`` count, never from a bound: the pages
#: one target serves in a turn are counted on their own ledger - monotonic,
#: never forgiven, since an endless cursor whose older pages the curation
#: clears would otherwise never reach a bound - and warned / refused at this
#: multiple of the ``similar`` bounds (``0`` there disables it here too).
_LOOP_BREAKER_PAGE_BUDGET_FACTOR = 3

_LOOP_BREAKER_IDENTICAL_NUDGE = (
    "[SYSTEM - repeated identical call] You have called '{tool_name}' with "
    "identical arguments {count} times this turn and received the same result. "
    "Stop and either change approach or report to the user."
)
_LOOP_BREAKER_SIMILAR_NUDGE = (
    "[SYSTEM - repeated call on one target] You have called '{tool_name}' "
    "{count} times this turn on the same {anchor_name} ({anchor}), varying only "
    "the other arguments. The target has not changed and asking again will not "
    "change it. Stop and either change approach or report to the user."
)
#: How every loop-breaker refusal begins: the call was answered, never run.
_LOOP_BREAKER_REFUSAL_PREFIX = "[Loop breaker] Refused: "
_LOOP_BREAKER_REFUSAL = (
    _LOOP_BREAKER_REFUSAL_PREFIX + "this is call {count} to '{tool_name}' {shape} this "
    "turn. The result would not differ from the ones you already have, so the "
    "call was not executed. Do not repeat it: change approach, or report to the "
    "user what you found and what is blocking you."
)
# A page series is going somewhere, so its messages never claim the result
# would not differ - they say how far the series has gone.
_LOOP_BREAKER_PAGE_NUDGE = (
    "[SYSTEM - long page series] You have read {count} {units} of the same "
    "{anchor_name} ({anchor}) this turn. Go on only if the answer needs the "
    "rest; otherwise answer from what you have."
)
_LOOP_BREAKER_PAGE_REFUSAL = (
    _LOOP_BREAKER_REFUSAL_PREFIX + "this is {unit} {count} of the same {anchor_name} "
    "({anchor}) this turn, the {unit} budget for one target. Answer from the "
    "{units} you have, or report what is still missing."
)


#: What one position of a tool that DECLARES its position arguments
#: (``ToolDefinition.position_arguments``) is called in the messages.
_LOOP_BREAKER_DECLARED_PART_NOUNS: tuple[str, str] = ("position", "positions")


def _page_units(tool_name: str, declared: bool = False) -> tuple[str, str]:
    """What one position of *tool_name* is called in the page messages: ``(page, pages)``."""
    if declared:
        return _LOOP_BREAKER_DECLARED_PART_NOUNS
    return _LOOP_BREAKER_PART_NOUNS.get(tool_name, ("page", "pages"))


def _declared_positions(tools: Iterable[ToolDefinition] | None) -> dict[str, tuple[tuple[str, ...], int]]:
    """The tools that declare which arguments address a part of their target, by name.

    ``{name: (position arguments, page budget)}`` - the budget ``0`` when the
    definition leaves it to the loop's default.
    """
    return {
        t.name: (tuple(t.position_arguments), max(0, int(t.position_budget or 0)))
        for t in tools or ()
        if t.position_arguments
    }


@dataclass
class _LoopDecision:
    """What the loop breaker decided about one call."""

    action: str  # "allow" | "warn" | "refuse"
    kind: str = "identical"  # "identical" | "similar" | "page"
    count: int = 0
    threshold: int = 0
    anchor_name: str | None = None
    anchor: str | None = None
    #: The tool declares its position arguments: its pages are "positions".
    declared: bool = False

    @property
    def refused(self) -> bool:
        return self.action == "refuse"


def _loop_breaker_target(arguments: dict | None) -> tuple[str, str] | None:
    """The ``(argument name, value)`` a call is ABOUT, for the similar shape.

    The first of :data:`_LOOP_BREAKER_TARGET_ARGUMENTS` present with a
    non-empty scalar value; the ``[[FILE:<id>]]`` / ``file:<uuid>`` spellings
    of a file id are unwrapped so two spellings of one file are one target.
    ``None`` for a call with no target argument (those only have the
    identical shape).
    """
    if not isinstance(arguments, dict):
        return None
    for name in _LOOP_BREAKER_TARGET_ARGUMENTS:
        raw = arguments.get(name)
        if raw is None or isinstance(raw, (dict, list, bool)):
            continue
        value = str(raw).strip()
        if not value:
            continue
        if name == "file_id":
            match = re.fullmatch(
                r"\[\[FILE:(?P<marker>[0-9a-fA-F-]{6,})\]\]|file:(?P<uuid>[0-9a-fA-F-]{36})",
                value,
            )
            if match:
                value = match.group("marker") or match.group("uuid")
            value = value.removeprefix("file-")
        return name, value
    return None


def _loop_breaker_page(
    arguments: dict | None, tool_name: str | None = None, own: tuple[str, ...] | None = None
) -> float | str | None:
    """The POSITION a call names in its target, for the paging exemption.

    A tool that declares ``position_arguments`` (*own*), or one of
    :data:`_LOOP_BREAKER_TOOL_POSITION_ARGUMENTS`, names its position with
    its own arguments (``query_file_json``: the path and block it reads;
    ``save_video_frame``: the moment and the region it cuts) - a structured
    value (a region box) canonically, so two spellings of one are one.
    Otherwise the first of :data:`_LOOP_BREAKER_PAGING_ARGUMENTS` present -
    matched with case, ``_`` and ``-`` ignored, so an MCP server's
    ``startAt`` / ``pageToken`` is a position too - with a non-empty scalar
    value: a number (or a numeric string) as a ``float`` (a negative one reads
    from the start, as every paging API here does), an opaque cursor / token
    as the string itself. ``None`` for a call that names no position, and for
    a value that is not one: a date or a timestamp (a time-range scan asks a
    new question each call - it does not page one document), and a
    non-numeric ``start`` (:data:`_LOOP_BREAKER_NUMERIC_ONLY_PAGING_ARGUMENTS`).
    """
    if not isinstance(arguments, dict):
        return None
    own = own or _LOOP_BREAKER_TOOL_POSITION_ARGUMENTS.get(tool_name or "")
    if own:
        parts = [
            f"{name}={_position_text(arguments[name])}"
            for name in own
            if arguments.get(name) is not None and str(arguments[name]).strip()
        ]
        if parts:
            text = "|".join(parts)
            if len(text) > _LOOP_BREAKER_PAGE_TOKEN_MAX_CHARS:
                # Still one position per distinct read, at a bounded size.
                text = "sha:" + hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
            return text
    present = {
        re.sub(r"[_-]", "", key).lower(): key for key in arguments if isinstance(key, str)
    }
    for name in _LOOP_BREAKER_PAGING_ARGUMENTS:
        key = present.get(name.replace("_", ""))
        if key is None:
            continue
        raw = arguments.get(key)
        if raw is None or isinstance(raw, (dict, list, bool)):
            continue
        if isinstance(raw, (int, float)):
            return max(0.0, float(raw)) if math.isfinite(raw) else None
        value = str(raw).strip()
        if not value:
            continue
        if len(value) > _LOOP_BREAKER_PAGE_TOKEN_MAX_CHARS:
            return None
        try:
            number = float(value)
        except ValueError:
            number = None
        if number is not None:
            return max(0.0, number) if math.isfinite(number) else None
        if name in _LOOP_BREAKER_NUMERIC_ONLY_PAGING_ARGUMENTS or _LOOP_BREAKER_DATE_RE.match(value):
            return None
        return value
    return None


def _position_text(value: object) -> str:
    """One position argument as text: a structured value canonically (sorted keys), a scalar as it is."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return str(value)


class _Ask:
    """One counted call and, once the loop appended it, the tool message that answered it.

    The SAME object sits in the identical ledger and in the similar (or page)
    ledger, so the answer recorded once is read by both. ``answer_len`` is the
    length of the body the tool answered WITH: a re-fetchable stub found later
    in its place is the harness's clearing only when the body changed, so a
    tool whose own output reads like the stub never forgives its ask.
    ``refused`` marks the ask the breaker answered itself: a refusal carried
    no data a re-call would bring back, so it counts whatever later happens to
    its message (cleared, compacted, evicted). ``position`` is where in its
    target the call read (:func:`_loop_breaker_page`, or
    :data:`_LOOP_BREAKER_IMPLICIT_START` for a call naming none), so the
    paging exemption knows which positions the model still holds.
    """

    __slots__ = ("answer", "answer_len", "asked_at", "position", "refused")

    def __init__(self, position: float | str | None = None, asked_at: float | None = None) -> None:
        self.answer: ChatMessage | None = None
        self.answer_len: int = -1
        self.position: float | str | None = position
        self.refused: bool = False
        # Monotonic time of the ask: a live read's ask older than
        # ``_LIVE_READ_WINDOW_S`` is forgotten (the state may have moved).
        self.asked_at: float = time.monotonic() if asked_at is None else asked_at


class _LoopBreaker:
    """Per-run counter of repeated calls, with the warn / refuse escalation.

    ``screen()`` is called once per dispatched call BEFORE it runs (the loop
    drops a refused call and answers it with a tool message);
    ``record_answer()`` once the tool message answering a screened call has
    been appended to the transcript (executed, replayed from the dedup cache,
    failed or refused alike); ``after_write()`` once a write tool has
    executed; ``after_call()`` after every executed call, for the
    stateful-tool reset. Keys are ORIGINAL tool names, so a sanitised alias
    of one tool shares the count with it.

    A count is the number of asks whose answer the model STILL HAS:
    ``screen`` first forgets every recorded ask whose answer the HARNESS has
    since taken away - the body replaced by the re-fetchable curation stub
    (``_is_refetchable_stub``, and only when the body changed: a tool whose
    own output reads like the stub answered, nothing was cleared), or the
    message object dropped from the transcript (semantic compaction, the
    emergency prune's eviction phase). Everything else is held: an ask not
    yet answered, or one a site never recorded (fail-closed - a missed hook
    makes the breaker stricter, never looser); an answer truncated or pruned
    IN PLACE (``[Pruned ...]``, ``[Truncated ...]``, a file-bearing stub), which
    is the same message object still in the transcript; and an ask the breaker
    REFUSED, whatever later happens to the refusal message - it carried no
    data a re-call would bring back, and forgiving it would hand the model a
    fresh run up to the bound. A forgiven ask is dropped, reference included,
    the next time its key is screened; asks under a key that is never screened
    again stay until the run ends (bounded by the calls the run dispatched).

    A PAGE - a call naming a position (``offset``, ``cursor``, ...) that no
    held ask on its target read - is not a repeat and skips the similar
    ledger; it lands on the page ledger instead, whose count is the
    pages the target served this turn, never forgiven, bounded at
    ``_LOOP_BREAKER_PAGE_BUDGET_FACTOR`` times the similar bounds. The one
    forgiveness mechanism is still the held-answer rule above: it frees a
    position the harness cleared (so the re-read is a page again), while the
    page count keeps an endless cursor bounded.
    """

    __slots__ = (
        "_cosmetic",
        "_declared",
        "_identical",
        "_identical_refuse",
        "_identical_warn",
        "_live_reads",
        "_live_tools",
        "_page_counts",
        "_page_refuse",
        "_page_warn",
        "_pages",
        "_rechecks",
        "_similar",
        "_similar_refuse",
        "_similar_warn",
        "_transcript",
        "_warned",
        "events",
    )

    def __init__(
        self,
        *,
        identical_warn: int,
        identical_refuse: int,
        similar_warn: int,
        similar_refuse: int,
        transcript: list[ChatMessage] | None = None,
        live_tools: frozenset[str] = frozenset(),
        live_reads: frozenset[str] = frozenset(),
        declared_positions: dict[str, tuple[tuple[str, ...], int]] | None = None,
        cosmetic_arguments: Mapping[str, frozenset[str]] | None = None,
        rechecks: frozenset[str] = frozenset(),
    ) -> None:
        # Tools whose definition declares ``recheck_after_write``: a write by
        # name that checks what the other writes change (a sandbox check run),
        # so another tool's write starts its counts over like a read's (#175).
        self._rechecks = rechecks
        # Tools whose definition declares ``live_state``: polled like
        # the exempt tools below, their unchanged argument is the point.
        self._live_tools = live_tools
        # Tools whose definition declares ``live_read``: counted, but an
        # ask older than ``_LIVE_READ_WINDOW_S`` is forgotten (a re-read of
        # a PR minutes later is a new question, not a loop).
        self._live_reads = live_reads
        # Output-naming arguments each tool declares: left out of the call's key.
        self._cosmetic = dict(cosmetic_arguments or {})
        # Tools whose definition declares ``position_arguments``, with
        # their page budget (``0``: the default below): each distinct position
        # of one target is a page, never a repeat on the target.
        self._declared = dict(declared_positions or {})
        self._identical_warn = max(0, int(identical_warn))
        self._identical_refuse = max(0, int(identical_refuse))
        self._similar_warn = max(0, int(similar_warn))
        self._similar_refuse = max(0, int(similar_refuse))
        self._page_warn = self._similar_warn * _LOOP_BREAKER_PAGE_BUDGET_FACTOR
        self._page_refuse = self._similar_refuse * _LOOP_BREAKER_PAGE_BUDGET_FACTOR
        # (tool, args hash) -> the asks this run whose answer the model still has
        self._identical: dict[tuple[str, str], list[_Ask]] = {}
        # (tool, anchor name, anchor value) -> the same, per target
        self._similar: dict[tuple[str, str, str], list[_Ask]] = {}
        # similar key -> the PAGE asks whose answer the model still has:
        # their positions are the ones a new page must differ from.
        self._pages: dict[tuple[str, str, str], list[_Ask]] = {}
        # similar key -> pages served on that target this turn (never forgiven)
        self._page_counts: dict[tuple[str, str, str], int] = {}
        # keys already warned about (one system message per key)
        self._warned: set[tuple] = set()
        # The live transcript (mutated in place by the loop): an answer no
        # longer in it was dropped by compaction. ``None`` skips that check.
        self._transcript = transcript
        #: every warn / refuse decision, for the run digest and tests
        self.events: list[_LoopDecision] = []

    @staticmethod
    def is_exempt(tool_name: str) -> bool:
        """The platform's own background-task and pause tools: an unchanged
        argument is their contract.

        Any other polling tool declares ``ToolDefinition.live_state`` and is
        exempted through ``live_tools`` (:meth:`screen`); a tool's name is
        never read for it (#5303): ``set_slack_user_status`` repeated with
        identical arguments is a stuck run like any other call.
        """
        return tool_name in _LOOP_BREAKER_EXEMPT_TOOLS or tool_name in WAIT_TOOL_NAMES

    @property
    def enabled(self) -> bool:
        return bool(
            self._identical_warn
            or self._identical_refuse
            or self._similar_warn
            or self._similar_refuse
        )

    def _answer_held(self, ask: _Ask, live_ids: set[int] | None) -> bool:
        """Whether the model still has the answer to *ask* in front of it.

        *live_ids* is the identity set of the transcript's messages, or
        ``None`` when the breaker has no transcript to check against.
        """
        if ask.refused:
            return True  # the breaker's own answer: nothing a re-call would restore
        answer = ask.answer
        if answer is None:
            return True  # in flight, or a site that never records: counted as before
        content = answer.content or ""
        if _is_refetchable_stub(content) and len(content) != ask.answer_len:
            # The curation replaced the body the tool answered with (a body
            # of at least ``_STALE_TOOL_RESULT_MIN_CHARS``) by its short stub.
            # A stub-shaped body of the SAME length is what the tool itself
            # returned - no clearing happened, so nothing invited a re-call.
            return False
        return live_ids is None or id(answer) in live_ids

    def _forget_taken_answers(
        self, key: tuple[str, str], similar_key: tuple[str, str, str] | None
    ) -> None:
        """Drop the asks under *key* / *similar_key* whose answer the harness took away.

        A ledger emptied this way is a fresh start for its key: the one-shot
        warning is re-armed with it, so a genuine repeat after the reset is
        nudged again instead of silently counted up to the refusal. The page
        ledger is pruned the same way - a cleared page's position is free to
        be read again - but its COUNT is not (the page budget never forgives).
        """
        live_ids: set[int] | None = None
        ledgers: list[tuple[dict, tuple]] = [(self._identical, key)]
        if similar_key is not None:
            ledgers.append((self._similar, similar_key))
            ledgers.append((self._pages, similar_key))
        for ledger, ledger_key in ledgers:
            asks = ledger.get(ledger_key)
            if not asks:
                continue
            if live_ids is None and self._transcript is not None and any(a.answer is not None for a in asks):
                live_ids = {id(m) for m in self._transcript}
            held = [a for a in asks if self._answer_held(a, live_ids)]
            if len(held) == len(asks):
                continue
            if held:
                ledger[ledger_key] = held
            else:
                del ledger[ledger_key]
                if ledger is not self._pages:
                    self._warned.discard(ledger_key)

    def _forget_aged_asks(self, key: tuple[str, str], similar_key: tuple[str, str, str] | None, now: float) -> None:
        """Drop a live read's asks older than ``_LIVE_READ_WINDOW_S``.

        The state such a tool reads (a pull request, its checks, a work item)
        moves on its own: the same read minutes later is a new question, so
        its count starts over with the asks of the window alone. A refused
        ask ages out too - the refusal was about a state that has moved.
        A ledger emptied this way re-arms its one-shot warning, like a reset.
        """
        ledgers: list[tuple[dict[Any, list[_Ask]], tuple[str, ...]]] = [(self._identical, key)]
        if similar_key is not None:
            ledgers.append((self._similar, similar_key))
        for ledger, ledger_key in ledgers:
            asks = ledger.get(ledger_key)
            if not asks:
                continue
            recent = [a for a in asks if now - a.asked_at < _LIVE_READ_WINDOW_S]
            if len(recent) == len(asks):
                continue
            if recent:
                ledger[ledger_key] = recent
            else:
                del ledger[ledger_key]
                self._warned.discard(ledger_key)

    def _position_held(self, similar_key: tuple[str, str, str], position: float | str) -> bool:
        """Whether a held ask on the target READ *position* (a page must not have).

        A refused ask read nothing: it holds no position. Counting it would
        route the re-ask of a refused page to the ``similar`` ledger, whose
        count is low, and run the very page the budget just refused - the
        re-ask is the next page, and is refused in turn.
        """
        return any(
            ask.position == position and not ask.refused
            for ledger in (self._similar, self._pages)
            for ask in ledger.get(similar_key, ())
        )

    def record_answer(self, tool_name: str, arguments: dict | None, answer: ChatMessage) -> None:
        """Attach the tool message that answered the latest unanswered ask of this call.

        A no-op for a call that was never screened (an exempt tool, a disabled
        breaker). The message object itself is kept, never a copy: what the
        curation later does to it (a stub, a drop) is what ``screen`` reads.
        """
        asks = self._identical.get(_dedup_key(tool_name, arguments, self._cosmetic))
        if not asks:
            return
        for ask in reversed(asks):
            if ask.answer is None:
                ask.answer = answer
                ask.answer_len = len(answer.content or "")
                return

    def screen(self, tool_name: str, arguments: dict | None, *, now: float | None = None) -> _LoopDecision:
        """Count one call and decide: allow it, allow it with a warning, or refuse it.

        *now* is the monotonic time of the call (tests pass it; ``None`` reads
        the clock): a live read's asks older than ``_LIVE_READ_WINDOW_S`` are
        forgotten before the count.
        """
        if not self.enabled or tool_name in self._live_tools or self.is_exempt(tool_name):
            return _LoopDecision(action="allow")
        current = time.monotonic() if now is None else now
        key = _dedup_key(tool_name, arguments, self._cosmetic)
        target = _loop_breaker_target(arguments)
        similar_key: tuple[str, str, str] | None = None
        if target is not None:
            similar_key = (tool_name, target[0], target[1])
        self._forget_taken_answers(key, similar_key)
        if tool_name in self._live_reads:
            self._forget_aged_asks(key, similar_key, current)
        own, own_budget = self._declared.get(tool_name, ((), 0))
        named_position = _loop_breaker_page(arguments, tool_name, own or None)
        ask = _Ask(_LOOP_BREAKER_IMPLICIT_START if named_position is None else named_position, asked_at=current)
        asks = self._identical.setdefault(key, [])
        asks.append(ask)
        count = len(asks)
        similar_count = 0
        page_count = 0
        if similar_key is not None:
            if named_position is not None and not self._position_held(similar_key, named_position):
                # A page series is one read going somewhere, not a repeat on
                # the target: the identical shape still catches an exact
                # repeat, the page ledger bounds the series.
                self._pages.setdefault(similar_key, []).append(ask)
                page_count = self._page_counts.get(similar_key, 0) + 1
                self._page_counts[similar_key] = page_count
            else:
                similar_asks = self._similar.setdefault(similar_key, [])
                similar_asks.append(ask)
                similar_count = len(similar_asks)
        # The page ledger's one-shot warning key: the similar key plus a tag,
        # so it resets with the tool's other keys (``after_write`` /
        # ``after_call`` read ``k[0]``) and never shares the similar warning.
        page_warned_key = (*similar_key, "page") if similar_key is not None else None
        # A declared budget replaces the default one, warned at the same share of it;
        # a breaker whose similar bounds are off (``0``) keeps the page budget off too.
        page_warn, page_refuse = self._page_warn, self._page_refuse
        if own_budget and self._page_refuse:
            page_refuse = own_budget
            page_warn = max(1, (own_budget * self._page_warn) // self._page_refuse) if self._page_warn else 0
        declared = bool(own)

        decision = _LoopDecision(action="allow")
        if self._identical_refuse and count >= self._identical_refuse:
            decision = _LoopDecision(
                action="refuse", kind="identical", count=count, threshold=self._identical_refuse
            )
        elif page_count and page_refuse and page_count >= page_refuse:
            decision = _LoopDecision(
                action="refuse",
                kind="page",
                count=page_count,
                threshold=page_refuse,
                anchor_name=target[0] if target else None,
                anchor=target[1] if target else None,
                declared=declared,
            )
        elif similar_key is not None and self._similar_refuse and similar_count >= self._similar_refuse:
            decision = _LoopDecision(
                action="refuse",
                kind="similar",
                count=similar_count,
                threshold=self._similar_refuse,
                anchor_name=target[0] if target else None,
                anchor=target[1] if target else None,
            )
        elif self._identical_warn and count >= self._identical_warn and key not in self._warned:
            self._warned.add(key)
            decision = _LoopDecision(
                action="warn", kind="identical", count=count, threshold=self._identical_warn
            )
        elif (
            similar_key is not None
            and self._similar_warn
            and similar_count >= self._similar_warn
            and similar_key not in self._warned
        ):
            self._warned.add(similar_key)
            decision = _LoopDecision(
                action="warn",
                kind="similar",
                count=similar_count,
                threshold=self._similar_warn,
                anchor_name=target[0] if target else None,
                anchor=target[1] if target else None,
            )
        elif (
            page_count
            and page_warn
            and page_count >= page_warn
            and page_warned_key not in self._warned
        ):
            self._warned.add(page_warned_key)
            decision = _LoopDecision(
                action="warn",
                kind="page",
                count=page_count,
                threshold=page_warn,
                anchor_name=target[0] if target else None,
                anchor=target[1] if target else None,
                declared=declared,
            )
        if decision.action != "allow":
            self.events.append(decision)
        # A refused call still counts: the refusal numbers the ASK ("this is
        # call 6"), so a model that keeps asking reads a rising count - one
        # that only dips when the harness clears an EARLIER answer, never
        # because a refusal was forgotten. The refused ask keeps counting
        # whatever later happens to the refusal message (``_answer_held``):
        # the curation can stub a refusal whose ``similar`` anchor is a long
        # URL, compaction can drop it, and neither hands back data a re-call
        # would restore.
        if decision.refused:
            ask.refused = True
        return decision

    def after_write(self, writers: Collection[str] = ()) -> None:
        """A write took effect: every check starts over (a re-read is a new question).

        A check is any tool that is not a write BY ITS NAME
        (:func:`_is_named_write`), not every tool the classifier merely failed
        to read. The classifier's fallback reads an unknown verb as a write
        (``capture_presentation_slides``, ``workset_next_pending``,
        ``computer_screenshot``, a camelCase MCP ``getJiraIssue``), so keying
        the reset on it left every such check counting across the writes it
        verifies: edit, capture, edit, capture was warned at the third capture
        and refused at the fifth. Named writes keep their counts, so a stuck
        write loop is still caught; *writers* (the tools whose write took
        effect in this step) keep theirs too, so an unknown-verb write
        repeated identically is not wiped by its own reset.
        """
        exempt = set(writers)

        def _starts_over(tool_name: str) -> bool:
            if tool_name in exempt:
                return False
            # A recheck tool (``recheck_after_write``) checks the state the
            # write changed: a new question, whatever its name reads as (#175).
            return tool_name in self._rechecks or not _is_named_write(tool_name)

        for key in [k for k in self._identical if _starts_over(k[0])]:
            del self._identical[key]
        for key in [k for k in self._similar if _starts_over(k[0])]:
            del self._similar[key]
        # A check's page series starts over too: after a mutation the
        # pages of the target are a new read, like its identical asks.
        for key in [k for k in self._pages if _starts_over(k[0])]:
            del self._pages[key]
        for key in [k for k in self._page_counts if _starts_over(k[0])]:
            del self._page_counts[key]
        self._warned = {k for k in self._warned if not _starts_over(k[0])}

    def after_call(self, tool_name: str, arguments: dict | None) -> None:
        """A stateful tool ran: its OTHER argument sets start over (the page moved on).

        Both ledgers, not the identical one alone: after a
        ``browser(action=navigate, url=B)`` the similar count on url A is
        about a page that is gone, and the warning recorded for it would
        otherwise silence the nudge a return to A should get afresh. Only
        this call's own keys - the identical key and, when the call names a
        target, its similar key - survive, so two back-to-back identical calls
        are still one page twice.
        """
        if tool_name not in _LOOP_BREAKER_STATEFUL_TOOLS:
            return
        key = _dedup_key(tool_name, arguments, self._cosmetic)
        target = _loop_breaker_target(arguments)
        similar_key = (tool_name, target[0], target[1]) if target is not None else None
        for other in [k for k in self._identical if k[0] == tool_name and k != key]:
            del self._identical[other]
        for other in [k for k in self._similar if k[0] == tool_name and k != similar_key]:
            del self._similar[other]
        for other in [k for k in self._pages if k[0] == tool_name and k != similar_key]:
            del self._pages[other]
        for other in [k for k in self._page_counts if k[0] == tool_name and k != similar_key]:
            del self._page_counts[other]
        page_warned_key = (*similar_key, "page") if similar_key is not None else None
        self._warned = {
            k for k in self._warned if k[0] != tool_name or k in (key, similar_key, page_warned_key)
        }

    def count_of(self, tool_name: str, arguments: dict | None) -> int:
        """How many asks of this exact call the model still holds the answer to (tests / digests)."""
        key = _dedup_key(tool_name, arguments, self._cosmetic)
        self._forget_taken_answers(key, None)
        return len(self._identical.get(key, ()))


#: Write-classified tools whose call mutates nothing a read tool re-reads, so
#: they never reset the read counts: ``annotate_file`` writes the model's OWN
#: notes about a file (the tool description asks for it right after every
#: image view - ``read, annotate, read, annotate`` is the designed sequence,
#: and a reset on each annotate would keep the read count under the bound
#: forever), and a stateful tool's call is a step of the agent's own session.
_LOOP_BREAKER_NON_RESETTING_WRITES: frozenset[str] = frozenset({"annotate_file"}) | _LOOP_BREAKER_STATEFUL_TOOLS


def _loop_breaker_write_resets(tool_name: str) -> bool:
    """Whether a WRITE by *tool_name* starts the read tools' counts over.

    The shared classifier reads a stateful tool (``browser``) as a write - it
    can submit a form - and an unknown verb (``annotate_file``) as a write
    too, its fail-safe bias for the dedup cache; neither mutates the files and
    decks the read tools re-read. The 25 Sep 2026 run interleaved its ~40
    ``read_file_content`` calls with ``browser`` calls; a reset on each would
    have kept every read count under the bound. The dedup cache and the
    near-duplicate guard keep the classifier's plain reading (a cleared cache
    costs a re-execution, never a missed refusal).
    """
    return _is_write_tool(tool_name) and tool_name not in _LOOP_BREAKER_NON_RESETTING_WRITES


#: The tool-result text of a call cut from a batch by the tool-call budget.
#: ``{limit}`` is the limit with thousands separators. It is for the
#: model only: the message is tagged ``not_run="budget"``, and what reads a cut
#: call back (the digest, usage tracking) reads the tag, never this text.
_TOOL_BUDGET_REFUSAL_TEMPLATE = (
    "Not executed: this run reached its tool-call budget ({limit} calls), "
    "so this call was not run. Work with the results you already have."
)


def _format_tool_budget_refusal(budget: RunBudget) -> str:
    """The tool-result text of a call cut from a batch by the tool-call budget."""
    return _TOOL_BUDGET_REFUSAL_TEMPLATE.format(limit=f"{int(budget.max_tool_calls or 0):,}")


def _format_loop_breaker_refusal(tool_name: str, decision: _LoopDecision) -> str:
    """The tool-result text of a refused call: its count, its shape, what to do instead."""
    if decision.kind == "page" and decision.anchor_name:
        unit, units = _page_units(tool_name, decision.declared)
        return _LOOP_BREAKER_PAGE_REFUSAL.format(
            count=decision.count,
            anchor_name=decision.anchor_name,
            anchor=decision.anchor,
            unit=unit,
            units=units,
        )
    if decision.kind == "similar" and decision.anchor_name:
        shape = f"on the same {decision.anchor_name} ({decision.anchor})"
    else:
        shape = "with identical arguments"
    return _LOOP_BREAKER_REFUSAL.format(count=decision.count, tool_name=tool_name, shape=shape)


def _format_loop_breaker_nudge(tool_name: str, decision: _LoopDecision) -> str:
    """The system message injected when a count reaches its warn bound."""
    if decision.kind == "page" and decision.anchor_name:
        return _LOOP_BREAKER_PAGE_NUDGE.format(
            count=decision.count,
            anchor_name=decision.anchor_name,
            anchor=decision.anchor,
            units=_page_units(tool_name, decision.declared)[1],
        )
    if decision.kind == "similar" and decision.anchor_name:
        return _LOOP_BREAKER_SIMILAR_NUDGE.format(
            tool_name=tool_name,
            count=decision.count,
            anchor_name=decision.anchor_name,
            anchor=decision.anchor,
        )
    return _LOOP_BREAKER_IDENTICAL_NUDGE.format(tool_name=tool_name, count=decision.count)


def _format_loop_breaker_user_warning(tool_name: str, decision: _LoopDecision) -> str:
    """The reasoning-pane line a warning shows the USER.

    Names the tool, the count and the shape, like the failure brake's and the
    near-duplicate guard's lines do, so the reader can check the claim against
    the tool list instead of being told "the same tool call is being repeated".
    """
    if decision.kind == "page" and decision.anchor_name:
        return (
            f"⚠️ '{tool_name}' has read {decision.count} {_page_units(tool_name, decision.declared)[1]} of the same "
            f"{decision.anchor_name} this turn. I will answer from what I have "
            "unless the rest is needed."
        )
    if decision.kind == "similar" and decision.anchor_name:
        shape = f"on the same {decision.anchor_name}"
    else:
        shape = "with identical arguments"
    return (
        f"⚠️ '{tool_name}' has been called {decision.count} times {shape} this turn. "
        "I will change approach or report what I have instead of asking again."
    )


_STEP_BUDGET_NUDGE = (
    "[SYSTEM - step budget] You have used {percent}% of your step budget "
    "({used} of {total} iterations){spent_on}. Finish or report: consolidate what "
    "you have into the answer, and say what is still open instead of retrying it."
)


def _format_step_budget_nudge(
    *, used: int, total: int, tool_call_totals: Mapping[str, int]
) -> str:
    """The one-shot step-budget message, naming the top three tools by call count."""
    top = sorted(tool_call_totals.items(), key=lambda item: (-item[1], item[0]))[:3]
    spent_on = ""
    if top:
        spent_on = " on " + ", ".join(f"{name} ({count})" for name, count in top)
    percent = round(100 * used / total) if total > 0 else 0
    return _STEP_BUDGET_NUDGE.format(percent=percent, used=used, total=total, spent_on=spent_on)


def _step_budget_nudge_iteration(max_iterations: int, fraction: float) -> int | None:
    """The 0-based iteration at whose top the step-budget nudge fires, or ``None``.

    ``ceil(fraction * max)`` iterations have been USED when the loop reaches
    iteration ``ceil(fraction * max)``; a fraction outside ``(0, 1)``, a cap
    too small for the nudge to land before the wind-down, or ``0`` disables it.
    """
    if max_iterations <= 0 or not (0 < fraction < 1):
        return None
    at = math.ceil(max_iterations * fraction)
    if at < 1 or at >= max_iterations:
        return None
    return at


# Read/write classification is the security-graded catalog classifier
# (``services/tool_classification.py``) so the dedup policy and the "Read"
# badge customer guarantee share ONE source of truth and can never drift.
# That classifier fails safe to "write" for unknown verbs, which is exactly
# the bias the cache needs (never treat an unclassifiable tool as a safe,
# cacheable read).


def _tool_rw_class(tool_name: str) -> str:
    """Classify *tool_name* as ``"read"`` or ``"write"`` (shared classifier)."""
    return infer_tool_type(tool_name)


def _is_named_write(tool_name: str) -> bool:
    """True when the shared classifier reads *tool_name* as a write BY ITS NAME.

    The classifier's positive evidence only - a write override, or a write
    token (``set``, ``update``, ``post``, ``delete``, ...) in a name that does
    not open with a read verb - never its "unknown name is a write" fallback
    (rule 6 of ``classify_tool_name``). The loop breaker needs exactly that
    distinction twice: for its polling exemption - ``poll_job`` is a
    write to the fallback and a polling read to anyone reading the name,
    while ``set_slack_user_status`` is a write by name whatever hint it
    carries - and for which counts a write starts over (``after_write``): a
    check with an unknown verb is still a check.
    """
    lowered = tool_name.lower()
    if lowered in READ_OVERRIDES:
        return False
    if lowered in WRITE_OVERRIDES:
        return True
    tokens = lowered.split("_")
    if tokens[0] in READ_VERBS:
        return False
    return any(token in WRITE_TOKENS for token in tokens)


def _is_write_tool(tool_name: str) -> bool:
    """True when *tool_name* is write-classified (a mutating tool).

    Mutating tools must ALWAYS execute (never be served from or stored in the
    dedup cache) AND must invalidate the read cache when they run — see
    ``_is_dedup_exempt`` and the invalidate-on-write logic in the loop.
    """
    return _tool_rw_class(tool_name) == "write"


def _is_dedup_exempt(tool_name: str, live_tools: frozenset[str] = frozenset()) -> bool:
    """Return True when *tool_name* must never be deduplicated.

    A tool is exempt when EITHER:

    * it is one of the platform's own polling/stateful tools (explicit set)
      whose identical-argument result legitimately changes between calls, OR
    * its definition declares ``live_state`` (*live_tools*): it reports
      a background job or a session whose state moves without any call of
      the run, so a replay would hand back a state that is gone, OR
    * it is **write-classified** — a mutating tool must always re-execute, so
      an identical-argument replay is never suppressed.  Otherwise the real
      mutation would be silently skipped while the model is told (via the
      cached "success") that it happened, diverging its world-model from
      reality.

    Only read-classified, non-polling tools remain dedup-eligible.
    """
    if tool_name in DEDUP_EXEMPT_TOOLS or tool_name in live_tools:
        return True
    return _is_write_tool(tool_name)


def _dedup_cosmetic_arguments(
    tool_name: str, cosmetic_table: Mapping[str, frozenset[str]] | None = None
) -> frozenset[str]:
    """Output-naming arguments excluded from *tool_name*'s dedup key.

    *cosmetic_table* is what the run's definitions declare
    (:func:`app.tools.base.declared_cosmetic_arguments`). Exact lookup on the
    original name first; a multi-instance suffixed name
    (``download_notion_file__acme``, see ``_suffix_instance_tools``) falls back
    to the base before the suffix. Every other tool gets the empty set — all
    of its arguments stay in the key.
    """
    if not cosmetic_table:
        return frozenset()
    cosmetic = cosmetic_table.get(tool_name)
    if cosmetic is None and "__" in tool_name:
        cosmetic = cosmetic_table.get(tool_name.rsplit("__", 1)[0])
    return cosmetic or frozenset()


def _dedup_key(
    tool_name: str,
    arguments: dict | None,
    cosmetic_table: Mapping[str, frozenset[str]] | None = None,
) -> tuple[str, str]:
    """Cache key for a tool call: ``(name, sha256(canonical_json(effective args)))``.

    The effective arguments are the call's arguments minus the cosmetic
    (output-naming) ones the tool declares in *cosmetic_table*
    (``ToolDefinition.cosmetic_arguments``), so
    ``download_notion_file(block_id=X, filename="a.png")`` and
    ``(block_id=X, filename="b.png")`` are one call while a different
    ``block_id`` is not.
    """
    cosmetic = _dedup_cosmetic_arguments(tool_name, cosmetic_table)
    effective = arguments or {}
    if cosmetic and isinstance(effective, dict):
        effective = {k: v for k, v in effective.items() if k not in cosmetic}
    canonical = json.dumps(effective, sort_keys=True, default=str)
    return (tool_name, hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _format_cached_result(cached_content: str, age_s: float | None = None) -> str:
    """The FULL cached payload under the replay header, with its age.

    Never a pointer at "the previous result": the curation may have replaced
    it with the stub that sent the model here, so the replay carries the data
    itself and its header is true whether or not an earlier copy still stands.
    *age_s* (seconds since the call really ran) is named in the header, so a
    model reading a live state knows how old the answer is.
    """
    age = f" Read {max(0, round(age_s))} s ago." if age_s is not None else ""
    return f"{_DEDUP_CACHE_MARKER}{age} {_DEDUP_CACHE_NUDGE}\n\n{cached_content}"


def _cache_entry_fresh(entry: _CachedToolResult, now: float | None = None) -> bool:
    """True while *entry* is younger than its bound: its own ``ttl`` (a live
    read's :data:`_LIVE_READ_WINDOW_S`), else :data:`_DEDUP_CACHE_TTL_S`."""
    current = time.monotonic() if now is None else now
    ttl = _DEDUP_CACHE_TTL_S if entry.ttl is None else min(entry.ttl, _DEDUP_CACHE_TTL_S)
    # Compared against the expiry instant, not the age: ``(t + ttl) - t`` can
    # round just under ``ttl`` and keep an entry fresh at its exact bound.
    return current < entry.stored_at + ttl


class _CachedToolResult:
    """What the per-run dedup cache keeps for one successful call.

    The text AND the files: a cache hit on a download whose value was an
    attached image must replay the image, not a sentence saying it is
    attached — the replayed message is file-bearing like the original, and
    the retention pass (:func:`_clear_stale_tool_result_files`) then treats it
    exactly like any other just-returned result: never stripped before the
    model has been sent it once, stubbed with the ``read_file_content`` note
    if it later has to drop the pixels. ``read_file_content`` itself is never
    cached (:data:`DEDUP_EXEMPT_TOOLS`): it is the recovery path that note
    names, and a replay of its stripped result would be a dead end.

    ``files`` holds the SAME ``FileRef`` objects the original tool result and
    the run's ``FileContext`` already reference (the loop registers every
    result's files there), never a copy of their bytes — so an inline
    ``FileRef.from_bytes`` ref costs nothing extra here, and the cache is
    bounded by ``_DEDUP_CACHE_MAX_ENTRIES`` like before.
    """

    __slots__ = ("content", "files", "stored_at", "ttl")

    def __init__(self, content: str, files: list | None = None, ttl: float | None = None) -> None:
        self.content = content
        self.files = list(files) if files else None
        # A shorter life than the default bound (a live read), or ``None``.
        self.ttl = ttl
        # Monotonic time the call ran: bounds the entry's life
        # (``_DEDUP_CACHE_TTL_S``) and dates the replay header.
        self.stored_at = time.monotonic()

    def age(self) -> float:
        return time.monotonic() - self.stored_at


class _DedupedCall:
    """Sentinel result for a deduplicated call in the parallel path.

    Carries the (marker-wrapped) content to feed back to the LLM, whether the
    original call it mirrors succeeded, and the files that call carried (so
    the mirror is file-bearing too — see :class:`_CachedToolResult`).
    """

    __slots__ = ("content", "files", "success")

    def __init__(
        self, content: str, success: bool = True, files: list | None = None
    ) -> None:
        self.content = content
        self.success = success
        self.files = list(files) if files else None

# When a tool hits a rate-limit (classified by ``base.is_rate_limit_error``),
# disable it for this many iterations to prevent the LLM from hammering the
# same API in a tight loop.
_RATE_LIMIT_COOLDOWN_ITERS = 3
# Auth/permission failures (classified by ``base.is_auth_or_permission_error``)
# are effectively permanent for the run — a broken token cannot recover on its
# own — so the cooldown is much longer than the transient rate-limit one.
_AUTH_FAILURE_COOLDOWN_ITERS = 8

# Generic user-facing fallback used when the LLM returns no useful text.
# Centralised here so all empty-response paths share the same message.
EMPTY_RESPONSE_FALLBACK = (
    "I gathered information but was unable to compose a complete response. "
    "Please try rephrasing your question or narrowing the scope."
)

# User-facing lead-ins when the forced-final-answer backstop call itself
# timed out or errored.  Without them the user would only see the generic
# tool-results digest (or ``EMPTY_RESPONSE_FALLBACK``) with no hint that the
# model ran out of time while composing the final answer.
FORCED_ANSWER_TIMEOUT_MESSAGE = (
    "The agent ran out of time while composing the final answer after "
    "using its tools."
)
FORCED_ANSWER_ERROR_MESSAGE = (
    "The agent hit an internal error while composing the final answer after "
    "using its tools."
)
# Per-run budget stop notices.  ``_BUDGET_STOP_NOTICE`` is
# model-facing: appended alongside ``_FORCED_FINAL_ANSWER`` so the synthesis
# itself can say what was left unfinished, which reads far better than
# string-prefixing an otherwise good answer.  ``BUDGET_EXHAUSTED_MESSAGE`` is
# user-facing and only used on the path where not even the held-back
# closing-synthesis reserve is left, so there is no LLM call to explain
# anything (the deterministic tool-result digest below).
_BUDGET_STOP_NOTICE = (
    "[SYSTEM — run budget exhausted] Tool use is now disabled because "
    "{reason}. Produce the final answer from what you already have, and state "
    "plainly which parts remain unfinished because the budget ran out."
)
BUDGET_EXHAUSTED_MESSAGE = (
    "The agent stopped because {reason}, leaving too little budget to compose "
    "a final answer."
)


# The stop reason of an answer the per-run budget cut short: a sized
# answer cut at its ``max_tokens`` that the budget could not continue. Its text
# ends with ``BUDGET_EXHAUSTED_MESSAGE``; a caller reads it through
# :func:`answer_cut_by_budget`, never through the text.
BUDGET_EXHAUSTED_STOP_REASON = "budget_exhausted"


def answer_cut_by_budget(response: ChatResponse | None) -> bool:
    """Whether the per-run budget cut *response* short: it is not a complete
    answer, and not a complete structured payload either, so a structured
    output repair must not re-serialize it as one."""
    return getattr(response, "stop_reason", None) == BUDGET_EXHAUSTED_STOP_REASON


#: The stop reasons of a turn that ended before its work was done, with the
#: wind-down code :func:`turn_wound_down` answers for each.
_WIND_DOWN_STOP_REASONS = {
    BUDGET_EXHAUSTED_STOP_REASON: "budget_stop",
    DEGENERATE_REPETITION_STOP_REASON: "degenerate_repetition",
    "timeout": "timeout",
    "waiting_on_tasks": "waiting_on_tasks",
    "cancelled": "cancelled",
}


def turn_wound_down(response: ChatResponse | None, messages: Iterable[Any]) -> str | None:
    """The code of the stop that ended this turn before its work was done, or ``None``.

    A turn wound down by the tool failure brake, the iteration backstop or the
    run budget still returns an answer, and the run still completes: a caller
    that settles a task from the turn's end (the work item inbox) reads this
    instead of the run status. Read from the stop reason and from the
    ``wind_down`` field of the system messages the loop appended
    (``tool_failure_brake``, ``rejected_work_brake``, ``iteration_backstop``,
    ``budget_stop``), never from any text.
    """
    stop = getattr(response, "stop_reason", None)
    if stop in _WIND_DOWN_STOP_REASONS:
        return _WIND_DOWN_STOP_REASONS[stop]
    for message in messages or ():
        code = getattr(message, "wind_down", None)
        if code and getattr(message, "role", None) == "system":
            return str(code)
    return None


# The stop reasons of a loop that ended on one of its own limits: the
# iteration budget ran out (the tools were taken away at the cap, so the answer
# is the wind-down summary) or the wall clock did. The answer is real but the
# task may be unfinished, and without these the caller saw an ordinary
# ``end_turn``: a run that hit its cap was recorded as a clean success.
ITERATION_LIMIT_STOP_REASON = "max_iterations"
DEADLINE_STOP_REASON = "deadline"

# Every terminal ``stop_reason`` that means the loop stopped before the task
# was done: its own limits above, the per-run budget cutting an answer short,
# a model call that never answered, background work still running when the
# wait budget ran out. ``finalize_tool`` is a finished task and not listed.
UNFINISHED_STOP_REASONS: frozenset[str] = frozenset(
    {
        ITERATION_LIMIT_STOP_REASON,
        DEADLINE_STOP_REASON,
        BUDGET_EXHAUSTED_STOP_REASON,
        DEGENERATE_REPETITION_STOP_REASON,
        "timeout",
        "waiting_on_tasks",
    }
)


def unfinished_stop_reason(response: ChatResponse | None) -> str | None:
    """Why the loop that returned *response* stopped before the task was done,
    or ``None`` when it ended on its own terms. Read off ``stop_reason``,
    never off the text."""
    reason = getattr(response, "stop_reason", None)
    return reason if reason in UNFINISHED_STOP_REASONS else None


_OUTPUT_TOKEN_KEYS: tuple[str, ...] = ("output_tokens", "completion_tokens")


def deadline_stop_without_output(response: ChatResponse | None) -> bool:
    """The loop stopped on its wall clock before any model call produced output.

    A run that starts late, with less of its deadline left than one model call
    needs, goes straight to the forced-answer shortcut: its answer is the canned
    ``FORCED_ANSWER_TIMEOUT_MESSAGE``, no model wrote a token of it. That is a
    run the deadline never let start, not a completed one. Read off the run's
    accumulated usage (every returned call merges its output tokens into it),
    never off the text.
    """
    if getattr(response, "stop_reason", None) != DEADLINE_STOP_REASON:
        return False
    usage = getattr(response, "usage", None) or {}
    produced = 0
    for key in _OUTPUT_TOKEN_KEYS:
        value = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(value, (int, float)):
            produced += int(value)
    return produced <= 0


def _with_limit_stop(response: ChatResponse, limit_stop: str | None) -> ChatResponse:
    """Mark *response* with the loop limit it ended on, when it ended on one.

    Only an ordinary ``end_turn`` is replaced: a stop reason the answer
    already carries (``max_tokens``, ``budget_exhausted``, ...) says more about
    that answer than the limit that preceded it.
    """
    if limit_stop is not None and response.stop_reason in (None, "end_turn"):
        response.stop_reason = limit_stop
    return response


def _budget_exhausted_text(violation: BudgetViolation, digest: str | None) -> str:
    """The answer of a run that ends without a synthesis the budget paid for."""
    lead_in = BUDGET_EXHAUSTED_MESSAGE.format(reason=violation.user_message())
    if digest:
        return f"{lead_in}\n\nPartial results:\n\n{digest}"
    return f"{lead_in} {EMPTY_RESPONSE_FALLBACK}"


# Budget-driven model downgrade.  Model-facing: the model cannot see
# the budget, so a silent mid-task capability change would look like the model
# suddenly got worse at its own job.  Telling it what happened — and that the
# work already recorded above still counts — is what turns the downgrade into
# a finished task instead of a restarted one.
_MODEL_DOWNGRADE_NOTICE = (
    "[SYSTEM — switched to a cheaper model to stay within budget] Continuing "
    "on {to_model}: {reason} at its primary model, so the run changed models "
    "instead of stopping. Your capabilities may be reduced from here on: do "
    "NOT redo work already recorded above, prefer consolidating what has been "
    "gathered over opening new lines of investigation, and keep the remaining "
    "tool use to what the task genuinely still needs."
)
# User-facing line for the same event, emitted on the ``thinking_text``
# channel so the switch is visible in the run's reasoning pane rather than
# only in the backend logs.
MODEL_DOWNGRADE_MESSAGE = (
    "⚠️ Continuing on a cheaper model ({to_model}): {reason} on the primary "
    "model. The answer may be less detailed than usual."
)

# ── Guards on the budget-driven model downgrade ───────────────────
# A downgrade must leave the run *runnable*, not merely cheaper.  Three things
# the loop pinned to the primary model can make the switch a worse outcome
# than the hard stop it replaces, so each one refuses it instead:
#
#   1. Context window.  The fallback may be smaller than what the transcript
#      already occupies.  The per-iteration compaction can shrink the
#      conversational part (it re-derives its budgets from the new window),
#      but it cannot shrink the SYSTEM prompt without the emergency
#      head+tail truncation that mutilates the persona and the security
#      policy.  So the switch is refused when the system prompt alone would
#      take more than this fraction of the fallback's session budget — at
#      that point the fallback is simply too small for this agent.
#   2. Tool surface.  Providers cap ``max_tools`` differently (128 on OpenAI
#      vs 1024 on Anthropic).  With progressive disclosure active the gate
#      re-caps the revealed groups itself (unloading the least recently used
#      ones, which stay loadable), so only the always-on core has to fit;
#      without it the whole list does.  Trimming THAT surface mid-run would
#      remove tools the model has already been told about and cannot get
#      back; refusing is honest.
#   3. Native structured output.  The payload was formatted for the primary's
#      provider, and dropping it would silently break the run's output
#      contract (an ``output_format=json`` agent would return prose).  A
#      same-provider downgrade keeps it and is allowed.
_DOWNGRADE_MAX_SYSTEM_FRACTION = 0.5

# Cap on how many dropped tool names are spelled out in the tool-truncation
# notice injected when the tool count exceeds the provider's ``max_tools``
# limit.  Beyond this, the remaining count is summarised ("and N more").
_TRUNCATION_NOTICE_MAX_NAMES = 20


def _build_tool_truncation_notice(dropped_names: list[str]) -> str:
    """System message telling the model which tools were truncated away.

    Only for tools the run genuinely cannot offer: the whole list trimmed to
    the provider's ``max_tools`` when progressive disclosure is off, or
    always-on core tools that overflow the cap on their own when it is on.
    Gated groups are NEVER reported here — the gate keeps them loadable under
    the cap.  Without an explicit notice the model may hallucinate
    calls to tool names it saw in earlier turns (or expects from the agent's
    persona) that are no longer on the wire.
    """
    shown = dropped_names[:_TRUNCATION_NOTICE_MAX_NAMES]
    listed = ", ".join(shown)
    hidden = len(dropped_names) - len(shown)
    if hidden > 0:
        listed += f", and {hidden} more"
    return (
        f"Note: {len(dropped_names)} tool(s) were unavailable this run due to "
        f"provider tool-count limits: {listed}. Do not attempt to call them "
        "or refer to them as available."
    )


def _build_tool_restored_notice(restored_names: list[str]) -> str:
    """Counterpart of ``_build_tool_truncation_notice`` for a model cap that rose mid-run."""
    shown = restored_names[:_TRUNCATION_NOTICE_MAX_NAMES]
    listed = ", ".join(shown)
    hidden = len(restored_names) - len(shown)
    if hidden > 0:
        listed += f", and {hidden} more"
    return (
        f"Note: the new model accepts more tools, so {len(restored_names)} tool(s) reported "
        f"unavailable earlier are available again: {listed}."
    )


def _as_str_list(value: object) -> list[str]:
    """Coerce a model-supplied ``load_tools`` argument into a list of strings.

    Models send a bare string as often as a one-element array; anything else
    (``None``, numbers, nested objects) is treated as "nothing requested".
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []

_COMPACTION_TRIGGER_UTILIZATION = 0.80
_COMPACTION_TARGET_UTILIZATION = 0.70
# The chat's context gauge warns once the conversation reaches this share of
# the compaction trigger: close enough that the next few steps will summarize
# older turns, far enough that the warning comes before they do.
_CONTEXT_APPROACHING_SHARE = 0.85


def _context_level(gate_chars: int, trigger_chars: int, *, overflowed: bool) -> str:
    """The gauge's level, from the compaction gate's own measure and trigger.

    ``gate_chars`` is what the gate compares against ``trigger_chars`` (the
    messages, not the tool schemas), so the gauge warns exactly as the loop
    will act. ``overflowed`` is set once the emergency prune cut older turns of
    this run to fit the window.
    """
    if overflowed:
        return CONTEXT_LEVEL_OVERFLOWED
    if trigger_chars > 0 and gate_chars >= trigger_chars * _CONTEXT_APPROACHING_SHARE:
        return CONTEXT_LEVEL_APPROACHING
    return CONTEXT_LEVEL_NORMAL


# The error-shaped-result classifiers (``_looks_like_tool_error`` /
# ``_is_error_shaped_result``, imported above from ``app.tools.base`` under
# their historical private names) live next to the rate-limit and
# auth/permission classifiers in ``base.py`` because
# ``CallableToolExecutor.execute_tool`` is the choke point that has to stamp
# ``is_error`` from them.  They are used here by the user-facing tool
# digest and by the dedup cache.


# Matches the stub prefix `_emergency_truncate_text` leaves on tool-result
# bodies ("[Truncated — originally N chars. Showing last K chars.] …tail").
# The marker is an internal context-management artifact that must never be
# quoted verbatim in a user-facing digest.
_TRUNCATION_STUB_RE = re.compile(
    r"^\[Truncated — originally [\d,]+ chars\. Showing last [\d,]+ chars\.\]"
    r"\s*…?\s*"
)

# Minimum useful characters a truncation-stubbed tool result must retain for
# its tail to be worth showing in the digest; shorter tails are mid-sentence
# fragments that read as garbage.
_DIGEST_MIN_USEFUL_CHARS = 80

# ``generate_file`` (and similar) persist a ``[[FILE:uuid]]`` marker as the
# entire tool result. The chat renderer turns that into a download card, so
# the digest line became "Generate file --" with nothing after the dash.
# Substitute the filename from ``msg.files`` before previewing.
_DIGEST_FILE_MARKER_RE = re.compile(
    r"\[\[FILE:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

# Tool names whose results are loop plumbing, not findings. The forced-final
# digest must never quote them: a wait-budget death otherwise surfaces as
# "here is a summary of what I found" followed by four wait() strings.
_PLUMBING_TOOL_NAMES = frozenset(
    {
        WAIT_TOOL_NAME,
        CHECK_TASK_STATUS_TOOL_NAME,
        LOAD_TOOLS_TOOL_NAME,
    }
)


def _is_plumbing_tool_name(name: str | None) -> bool:
    """True when *name* is wait / check_task_status / load_tools (or a suffix).

    Integration-prefixed names (``notion_wait``) must match the same way
    ``collect_pending_wait_task_ids`` matches ``*_wait``, or a wait-budget
    death still quotes Wait / Wait / Wait in the digest.
    """
    n = (name or "").strip()
    if not n:
        return False
    if n in _PLUMBING_TOOL_NAMES:
        return True
    return any(n.endswith(f"_{p}") for p in _PLUMBING_TOOL_NAMES)


# Per-task description budget in the park message: long enough to recognise
# the task, short enough that several tasks still read as one sentence.
_PARK_LABEL_DESCRIPTION_CHARS = 80
# Agent-name budget: the column is 200 chars, but a name that long is a
# configuration accident, and the park message must stay one sentence.
_PARK_LABEL_AGENT_CHARS = 60
# Named tasks listed in full before the rest folds into "N other tasks".
_PARK_LABEL_MAX_LISTED = 5
# Ceiling on the database label lookup behind a park message. The label is
# cosmetic: a stalled pool or query must degrade to the transcript-only
# label, never hold the park past the caller's ``asyncio.wait_for`` deadline
# and turn a delivered message into a cancelled loop with no output.
_PARK_LABEL_LOOKUP_TIMEOUT_SECS = 2.0


@dataclass(frozen=True)
class TaskLabel:
    """Human-readable identity of a background task.

    What the user is told a still-running delegation IS — the target agent's
    name and the task it was given — instead of the ``sub_agent_tasks`` UUID,
    which means nothing to the person reading the chat. Both halves are
    optional; a label with neither is treated as unknown and folded into a
    count rather than rendered as an id.

    ``agent_omitted`` records that the agent half was left out ON PURPOSE (a
    parallel copy of the calling agent, whose model-facing ``yourself`` label
    is not a name the user knows) — as opposed to merely not found yet, which
    is what the database fallback exists to fill in.
    """

    agent_name: str | None = None
    task_description: str | None = None
    agent_omitted: bool = False

    @property
    def known(self) -> bool:
        # Whitespace-only halves render to nothing, so they do not count —
        # otherwise a blank label would list an empty name in the sentence.
        return bool(
            (self.agent_name or "").strip() or (self.task_description or "").strip()
        )

    @property
    def complete(self) -> bool:
        """Nothing left for the database to add.

        A label with only one half is still *known* (it renders), but it is
        not the label the user was promised: a spawn result that outlived its
        pruned tool call yields the agent alone, and the persisted description
        must still be fetched — otherwise the park shows ``**Agent**`` where
        ``**Agent** - "task"`` was available one query away.
        """
        agent_settled = bool((self.agent_name or "").strip()) or self.agent_omitted
        return agent_settled and bool((self.task_description or "").strip())

    def filled_from(self, other: TaskLabel) -> TaskLabel:
        """This label with each MISSING half taken from *other*.

        The half already known keeps its value — a transcript agent name is
        never thrown away because the database row has none, and a deliberate
        omission stays omitted.
        """
        agent_name = self.agent_name
        if not (agent_name or "").strip() and not self.agent_omitted:
            agent_name = other.agent_name
        description = self.task_description
        if not (description or "").strip():
            description = other.task_description
        return TaskLabel(
            agent_name=agent_name,
            task_description=description,
            agent_omitted=self.agent_omitted,
        )

    def render(self) -> str:
        """``**Agent** - "description"``, or whichever half is present."""
        parts: list[str] = []
        if self.agent_name:
            # Both halves are user-controlled text (an admin names the agent,
            # the model writes the task) and the message must stay ONE line:
            # collapse any run of whitespace, including newlines, to a space.
            name = " ".join(self.agent_name.split())
            if name:
                parts.append(f"**{_safe_preview(name, _PARK_LABEL_AGENT_CHARS)}**")
        if self.task_description:
            # Single quotes inside so the delimiting double quotes stay
            # unambiguous. Word-boundary truncation, as the digest does — a
            # hard slice could cut a URL or a name in half.
            flat = " ".join(self.task_description.split()).replace('"', "'")
            if flat:
                preview = _safe_preview(flat, _PARK_LABEL_DESCRIPTION_CHARS)
                parts.append(f'"{preview}"')
        return " - ".join(parts)


def waiting_on_tasks_message(
    task_ids: list[str] | None = None,
    labels: Mapping[str, TaskLabel] | None = None,
) -> str:
    """User-facing text when a run dies waiting on still-running children.

    Distinct from ``summarise_tool_results``: this is not a digest of
    findings, and must never be mistaken for a completed answer.

    Tasks are named by *labels* (agent + description), never by id —
    the Background Tasks banner above the message names the same task that
    way, and a raw UUID in the sentence gave the user nothing to recognise.
    A task without a resolvable label is counted ("and 1 other background
    task"), and the follow-up agrees in number with what is listed.
    """
    ids = [t for t in (task_ids or []) if t]
    if not ids:
        return (
            "Background work is still in flight. I'll pick this up automatically "
            "as soon as it finishes and continue from there - no need to prompt me."
        )
    named: list[str] = []
    for tid in ids:
        label = (labels or {}).get(tid) or (labels or {}).get(tid.lower())
        if label is not None and label.known:
            named.append(label.render())
    listed = named[:_PARK_LABEL_MAX_LISTED]
    unlisted = len(ids) - len(listed)
    plural = len(ids) > 1
    if listed:
        subject = "; ".join(listed)
        if unlisted:
            subject += (
                f" and {unlisted} other background "
                f"{'tasks' if unlisted > 1 else 'task'}"
            )
        lead = f"Background work is still running: {subject}."
    else:
        lead = (
            f"{len(ids)} background {'tasks are' if plural else 'task is'} "
            "still running."
        )
    finish = "they finish" if plural else "it finishes"
    return (
        f"{lead} I'll pick this up automatically as soon as {finish} and "
        "continue from there - no need to prompt me. You can also check task "
        "status in the meantime."
    )


def collect_task_labels(messages: list[ChatMessage]) -> dict[str, TaskLabel]:
    """Task id -> :class:`TaskLabel` for every spawn visible in *messages*.

    The ``spawn_background_task`` tool RESULT carries ``Task ID:`` and
    ``Target agent:``; the tool CALL it answers (paired through
    ``tool_call_id``) carries the ``task`` argument — the exact text persisted
    as ``SubAgentTask.task_description``. The whole transcript is scanned, not
    just the current turn: a park can wait on a task spawned turns earlier
    (``wait(until=...)`` on prior work), and unlike the liveness probe in
    :func:`collect_spawned_task_ids` a label cannot park anything, so there is
    no cross-turn hazard in knowing it. Keys are lower-cased.

    Calls and results are paired in TRANSCRIPT ORDER, not through a global
    ``tool_call_id`` map: a ``tool_call_id`` is only unique within the
    response that minted it. Gemini derives it deterministically
    (``call_{function_name}_{index}``, ``llm/gemini_provider.py``), so two
    turns whose first call is a spawn share the id ``call_spawn_background_task_0``
    — a last-writer-wins map would then render the OLDER pending task with the
    NEWER task's description. Every other provider passes its own opaque ids
    through, unique per response but with no cross-turn contract either. A
    result therefore consumes the oldest still-unanswered call carrying its id
    (FIFO per id), which is the same local pairing the context-eviction
    grouping (:func:`_build_evictable_message_groups`) already relies on. A
    result whose call was pruned finds nothing pending and yields the agent
    alone — an incomplete label the database fallback completes.
    """
    suffix = f"_{_SPAWN_BACKGROUND_TOOL_NAME}"

    def _is_spawn(name: str | None) -> bool:
        n = (name or "").strip()
        return n == _SPAWN_BACKGROUND_TOOL_NAME or n.endswith(suffix)

    # tool_call_id -> descriptions of the spawn calls not yet answered by a
    # result, oldest first. An entry is pushed for EVERY spawn call, even one
    # with no usable ``task`` (None), so its own result consumes it instead of
    # stealing the description of a later call that reused the id.
    pending: dict[str, deque[str | None]] = {}
    labels: dict[str, TaskLabel] = {}
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if not tc.id or not _is_spawn(tc.name):
                    continue
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                task = args.get("task")
                pending.setdefault(tc.id, deque()).append(
                    task.strip() if isinstance(task, str) and task.strip() else None
                )
            continue
        if msg.role != "tool" or not _is_spawn(msg.name):
            continue
        description: str | None = None
        queue = pending.get(msg.tool_call_id or "")
        if queue:
            description = queue.popleft()
        content = msg.content or ""
        id_match = _SPAWN_TASK_ID_RE.search(content)
        if not id_match:
            # A stub or an error result still answered its call (consumed
            # above); it just names no task.
            continue
        agent_name: str | None = None
        agent_omitted = False
        agent_match = _SPAWN_TARGET_AGENT_RE.search(content)
        if agent_match:
            # Whichever alternative matched (up to the last ``Status:`` line,
            # or to the end of a block that has none).
            raw_name = agent_match.group(1)
            if raw_name is None:
                raw_name = agent_match.group(2) or ""
            agent_name = raw_name.strip() or None
            if agent_name == _SELF_COPY_TARGET_LABEL:
                # Deliberate, not missing: the database fallback must not
                # spend a query re-discovering the calling agent's own name.
                agent_name = None
                agent_omitted = True
        labels[id_match.group(1).lower()] = TaskLabel(
            agent_name=agent_name,
            task_description=description,
            agent_omitted=agent_omitted,
        )
    return labels


def _label_lookup_budget(wall_clock_deadline: float | None) -> float:
    """Seconds a park-label database lookup may take right now; ``0`` = none.

    Capped at :data:`_PARK_LABEL_LOOKUP_TIMEOUT_SECS` and, when the run has a
    wall-clock deadline, at what remains of it after the reserve the forced
    answer keeps for returning — a cosmetic lookup must never be the thing
    that pushes a delivered message past the outer ``asyncio.wait_for``.
    """
    if wall_clock_deadline is None:
        return _PARK_LABEL_LOOKUP_TIMEOUT_SECS
    remaining = (
        wall_clock_deadline - time.monotonic() - _FORCED_ANSWER_DEADLINE_RESERVE_SECS
    )
    return max(0.0, min(remaining, _PARK_LABEL_LOOKUP_TIMEOUT_SECS))


async def resolve_task_labels(
    messages: list[ChatMessage],
    task_ids: list[str],
    wait_condition: WaitCondition | None,
    *,
    lookup_timeout: float = _PARK_LABEL_LOOKUP_TIMEOUT_SECS,
) -> dict[str, TaskLabel]:
    """Labels for *task_ids*: the transcript first, the database for the rest.

    The transcript is free and usually complete. It is not always: the spawn
    result may have been pruned to a stub by the session budget, the task may
    predate the loaded history window, or the model may ``wait(until=...)``
    on a task it never spawned in this conversation. Those fall through to
    the wait condition's ``labels`` lookup (one query, when a user is in
    context). Any id still unlabelled is rendered as a count by
    :func:`waiting_on_tasks_message` — never as the id. A lookup failure is
    swallowed: the label is cosmetic and must never fail the park.

    A HALF-known label falls through too: when the spawn result survived but
    its tool call was pruned, the transcript yields the agent alone, and the
    persisted description is one query away. The database fills whichever
    half is missing and never overrides a half the transcript already has
    (:meth:`TaskLabel.filled_from`). A label that is :attr:`TaskLabel.complete`
    costs no query at all.

    The lookup is bounded by *lookup_timeout* (``<= 0`` skips it outright): a
    park is rendered under the run's own deadline, and a stalled pool must
    degrade to the transcript label rather than outlive that deadline.
    """
    labels = collect_task_labels(messages)
    missing: list[str] = []
    for tid in task_ids:
        if not tid:
            continue
        existing = labels.get(tid.lower())
        if existing is None or not existing.complete:
            missing.append(tid)
    lookup = getattr(wait_condition, "labels", None)
    if not missing or lookup is None or lookup_timeout <= 0:
        return labels
    try:
        fetched = await asyncio.wait_for(lookup(missing), timeout=lookup_timeout)
    except TimeoutError:
        logger.debug(
            "Agent loop: background task label lookup exceeded %.1fs, "
            "keeping transcript labels",
            lookup_timeout,
        )
        return labels
    except Exception:
        logger.debug("Agent loop: could not read background task labels", exc_info=True)
        return labels
    for tid, raw in (fetched or {}).items():
        if not isinstance(raw, dict):
            continue
        agent_name = raw.get("agent_name")
        description = raw.get("task_description")
        fetched_label = TaskLabel(
            agent_name=(
                (agent_name.strip() or None) if isinstance(agent_name, str) else None
            ),
            task_description=(
                (description.strip() or None) if isinstance(description, str) else None
            ),
        )
        key = str(tid).lower()
        existing = labels.get(key)
        merged = fetched_label if existing is None else existing.filled_from(fetched_label)
        if merged.known:
            labels[key] = merged
    return labels


async def build_waiting_on_tasks_message(
    messages: list[ChatMessage],
    task_ids: list[str],
    wait_condition: WaitCondition | None,
    *,
    lookup_timeout: float = _PARK_LABEL_LOOKUP_TIMEOUT_SECS,
) -> str:
    """The park message for *task_ids*, labels fully resolved.

    The ONE entry point every park site and the wait-budget digest go
    through (:func:`resolve_task_labels` then :func:`waiting_on_tasks_message`),
    so the live park, the persisted reply and the digest can never drift on
    how a still-running task is named. *lookup_timeout* bounds the
    database half; in-loop sites pass :func:`_label_lookup_budget` so it also
    respects the run's wall-clock deadline.
    """
    return waiting_on_tasks_message(
        task_ids,
        await resolve_task_labels(
            messages, task_ids, wait_condition, lookup_timeout=lookup_timeout
        ),
    )


def collect_pending_wait_task_ids(messages: list[ChatMessage]) -> list[str]:
    """Best-effort ``until=`` ids from the most recent wait tool call."""
    found: list[str] = []
    seen: set[str] = set()
    for msg in reversed(messages):
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                name = (tc.name or "").strip()
                if name != WAIT_TOOL_NAME and not name.endswith(
                    f"_{WAIT_TOOL_NAME}"
                ):
                    continue
                for tid in parse_until_task_ids((tc.arguments or {}).get("until")):
                    if tid not in seen:
                        seen.add(tid)
                        found.append(tid)
            if found:
                return found
    return found


def never_started_task_ids(live_ids: list[str], statuses: dict[str, str], answer: str) -> list[str]:
    """The open children a tool-free final answer may leave behind, or ``[]``.

    Non-empty only when every id of *live_ids* reads ``pending`` in *statuses*
    (queued, never started: a running, unknown or unread child keeps the park)
    and *answer* is a deliverable, not a "waiting for" narration. The run then
    ends on its answer instead of parking on implicit waits for a child that
    may not start within its budget; the child keeps running on its own.
    """
    if not live_ids or not (answer or "").strip():
        return []
    if any(statuses.get(tid) != "pending" for tid in live_ids):
        return []
    if _WAITING_NARRATION_RE.search(answer[-600:]):
        return []
    return list(live_ids)


def collect_spawned_task_ids(messages: list[ChatMessage]) -> list[str]:
    """Task ids spawned in the CURRENT turn via ``spawn_background_task``.

    Only tool results after the last ``user`` message are scanned: chat
    history rehydrates prior-turn tool messages, so a task spawned three
    turns ago (possibly still running for a long time) must never park —
    or retract the streamed answer of — an unrelated later turn.
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            start = i + 1
            break
    found: list[str] = []
    seen: set[str] = set()
    suffix = f"_{_SPAWN_BACKGROUND_TOOL_NAME}"
    for msg in messages[start:]:
        if msg.role != "tool":
            continue
        name = (msg.name or "").strip()
        if name != _SPAWN_BACKGROUND_TOOL_NAME and not name.endswith(suffix):
            continue
        # One spawn result carries exactly one task, on the block's own
        # ``Task ID:`` line, which precedes the agent name: the FIRST match is
        # the task, and any later look-alike (a legacy multi-line name with a
        # line of its own that reads ``Task ID: <uuid>``) is not one.
        match = _SPAWN_TASK_ID_RE.search(msg.content or "")
        if match:
            tid = match.group(1)
            if tid not in seen:
                seen.add(tid)
                found.append(tid)
    return found


def _humanise_tool_name(name: str) -> str:
    """Render an internal tool name for the user-facing digest.

    ``list_runs`` → ``List runs``. Keeps the digest readable for end users
    who never see internal snake_case tool identifiers anywhere else.
    """
    cleaned = name.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return "Tool"
    return cleaned[0].upper() + cleaned[1:]


def _safe_preview(content: str, max_chars: int = 600) -> str:
    """Truncate *content* to roughly *max_chars*, preferring a word boundary.

    Hard-slicing at exactly *max_chars* can split a word, a URL, or a
    markdown link in half, producing garbled output in the user-facing
    digest.  We try to cut at the last whitespace within the slice
    *provided* it does not eat too much of the preview (the cut must
    happen in the last 1/3 of the budget); otherwise we fall back to a
    hard cut to keep the preview length predictable.
    """
    if len(content) <= max_chars:
        return content
    head = content[:max_chars]
    cut = head.rfind(" ")
    if cut <= int(max_chars * 2 / 3):
        return head + " …"
    return head[:cut] + " …"


def _digest_file_labels(msg: ChatMessage) -> dict[str, str]:
    """Map file_id -> filename from a tool-result message's FileRefs."""
    labels: dict[str, str] = {}
    for ref in getattr(msg, "files", None) or []:
        fid = getattr(ref, "file_id", None)
        name = getattr(ref, "filename", None)
        if fid and name:
            labels[str(fid)] = str(name)
    return labels


def _digest_tool_preview(msg: ChatMessage, stripped: str) -> str:
    """Build a digest line that names generated files instead of ``[[FILE:]]``.

    A marker-only ``generate_file`` result otherwise renders as an empty
    preview once the chat UI consumes the marker.
    """
    labels = _digest_file_labels(msg)

    def _replace(match: re.Match[str]) -> str:
        fid = match.group(1)
        return labels.get(fid) or labels.get(fid.lower()) or f"file {fid}"

    replaced = _DIGEST_FILE_MARKER_RE.sub(_replace, stripped or "")
    preview = _safe_preview(replaced.strip())
    if preview:
        return preview
    if labels:
        return _safe_preview(", ".join(labels.values()))
    return ""


def _digest_lines(
    messages: list[ChatMessage], max_tools: int
) -> tuple[list[str], int, bool]:
    """``(body_lines, omitted, any_success)`` behind the tool-result digest.

    Shared by :func:`summarise_tool_results` and its label-resolving async
    twin so the two can never disagree on what counts as a finding.
    """
    # A call the tool-call budget cut or the loop breaker refused never ran,
    # and the loop tagged its answer (``not_run``). It is not a
    # finding - listed, a skipped write would read as completed work - and it
    # must not use up the most-recent window below: a cut batch answers its
    # dropped calls first, so a large cut would crowd out the results that ran.
    tool_msgs = [
        m
        for m in messages
        if m.role == "tool"
        and ((m.content or "").strip() or getattr(m, "files", None))
        and not getattr(m, "not_run", None)
    ]
    if not tool_msgs:
        return [], 0, False

    # Keep the most recent results — they are usually the most relevant.
    recent = tool_msgs[-max_tools:]

    # Entries are deduplicated on (tool, SHA-256 of the full stub-stripped
    # content): a budget-exhausted loop often repeats the same call with
    # identical output (e.g. eight "No runs found." in a row) and listing
    # each one buries the signal. Keying on the full content — never the
    # truncated preview — guarantees two different results sharing a
    # ~600-char prefix are not collapsed into one misleading entry.
    entries: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    omitted = 0
    for msg in recent:
        name = msg.name or "tool"
        content = (msg.content or "").strip()
        # Loop plumbing (wait / status poll / load_tools) is not a finding.
        # Quoting it is how a wait-budget death became "here is a summary
        # of what I found" followed by Wait / Wait / Wait.
        if _is_plumbing_tool_name(name):
            continue
        # Drop curation stubs ("[Pruned tool result …]", "[Duplicate …]",
        # "[Earlier …]") — they are internal context-management artifacts
        # that carry no useful information and must never be quoted to the
        # user.  Count them so the user knows the digest is partial.
        if _is_curation_stub(content):
            omitted += 1
            continue
        # Strip the emergency-truncation stub: it is an internal
        # context-management artifact ("[Truncated — originally 2,066
        # chars. …]") that must never be quoted to the user.  When the
        # surviving tail is too short to be meaningful, count the result
        # as omitted instead of showing a garbled fragment.
        stripped = _TRUNCATION_STUB_RE.sub("", content)
        was_stubbed = stripped != content
        if was_stubbed and len(stripped) < _DIGEST_MIN_USEFUL_CHARS:
            omitted += 1
            continue
        preview = _digest_tool_preview(msg, stripped)
        if not preview:
            omitted += 1
            continue
        if was_stubbed:
            preview = "… " + preview
        # Key on the FULL stripped content (not the truncated preview) so
        # two different results that merely share the same first ~600 chars
        # are never collapsed into one misleading "identical" entry.
        key = (name, hashlib.sha256(stripped.encode()).hexdigest())
        if key in seen:
            seen[key]["count"] += 1
            continue
        entry = {
            "name": name,
            "preview": preview,
            "error": _looks_like_tool_error(stripped),
            "count": 1,
        }
        seen[key] = entry
        entries.append(entry)

    body_lines: list[str] = []
    for entry in entries:
        prefix = "✗" if entry["error"] else "•"
        line = f"{prefix} **{_humanise_tool_name(entry['name'])}** — {entry['preview']}"
        if entry["count"] > 1:
            line += f" _(identical result {entry['count']} times)_"
        body_lines.append(line)

    any_success = any(not entry["error"] for entry in entries)
    return body_lines, omitted, any_success


def _render_digest(body_lines: list[str], omitted: int, any_success: bool) -> str:
    """Assemble the digest text around non-empty *body_lines*."""
    # Lead with a completion summary when any tool actually produced a
    # result. The historical "wasn't able to finish composing" framing
    # was a lie whenever the tools had already done the work (Notion
    # upload/update/delete, generate_file) and only the final prose
    # call came back empty.
    lead_in = (
        "Here is a summary of what was completed this turn:"
        if any_success
        else (
            "The turn stopped before a final answer. Here is what failed:"
        )
    )
    lines: list[str] = [
        lead_in,
        "",
        *body_lines,
    ]
    if omitted:
        lines.append("")
        lines.append(
            f"_({omitted} tool result(s) could not be included here because "
            "they were trimmed while managing memory.)_"
        )
    lines.append("")
    lines.append(
        "You can ask me to continue from here, or narrow the question to "
        "the specific part that matters most."
    )
    return "\n".join(lines)


def summarise_tool_results(
    messages: list[ChatMessage], *, max_tools: int = 12
) -> str:
    """Build a compact, user-readable digest from recent tool-call results.

    Used as an emergency fallback when the LLM repeatedly fails to produce a
    text response despite having executed tool calls.  Rather than returning
    the generic "I gathered information" error, we summarise what we already
    know so the user gets some value from the iterations they paid for.

    When any tool produced a non-error result the lead-in is a completion
    summary ("Here is a summary of what was completed this turn") — the
    tools often already did the work, and saying "I wasn't able to finish
    composing" is a lie.  All-error digests keep a failure lead-in.

    Returns an empty string when there are no useful tool results.

    The digest includes an "N earlier tool results omitted for brevity"
    notice when pruned tool messages are skipped, so the user understands
    the digest is partial and not the full set of gathered data.

    The synchronous form names a pending background task from the transcript
    only. Inside the loop, where a wait condition is at hand, use
    :func:`summarise_tool_results_resolved` so the database fallback applies
    too — EXCEPT on a path whose contract is to return immediately
    (the wall-clock shortcut of the forced-answer backstop), which stays on
    this form on purpose: no database round-trip, however bounded, belongs
    on the path that exists because the deadline is already upon us.
    """
    body_lines, omitted, any_success = _digest_lines(messages, max_tools)
    if not body_lines:
        # Only claim "still waiting on background tasks" when the transcript
        # actually names pending task ids. A plain wait(duration) pacing call
        # (e.g. polling an external API) leaves plumbing-only results too,
        # and the waiting message would then mislead the user into thinking
        # unfinished background tasks exist.
        pending = collect_pending_wait_task_ids(messages)
        if pending:
            return waiting_on_tasks_message(pending, collect_task_labels(messages))
        return ""
    return _render_digest(body_lines, omitted, any_success)


async def summarise_tool_results_resolved(
    messages: list[ChatMessage],
    wait_condition: WaitCondition | None,
    *,
    max_tools: int = 12,
    lookup_timeout: float = _PARK_LABEL_LOOKUP_TIMEOUT_SECS,
) -> str:
    """:func:`summarise_tool_results` with the park branch fully resolved.

    Same digest, same wording. The one difference: a pending task the
    transcript can no longer name (spawn result pruned to a stub, task
    predating the loaded history window) is looked up through
    *wait_condition* exactly as the live park does, so the wait-budget digest
    and the park read identically. The lookup runs only when the park
    branch is actually taken — a digest with findings never touches the
    database — and is bounded by *lookup_timeout*: past it (or on any
    error) the text is exactly what the synchronous form would have produced,
    so a stalled pool can only cost the label, never the message. In-loop
    callers pass :func:`_label_lookup_budget` so the bound also shrinks with
    the run's remaining wall-clock time, down to no lookup at all.
    """
    body_lines, omitted, any_success = _digest_lines(messages, max_tools)
    if not body_lines:
        pending = collect_pending_wait_task_ids(messages)
        if pending:
            return await build_waiting_on_tasks_message(
                messages, pending, wait_condition, lookup_timeout=lookup_timeout
            )
        return ""
    return _render_digest(body_lines, omitted, any_success)


async def recover_empty_response(
    messages: list[ChatMessage],
    *,
    context: str,
    conversation_id: str | None,
    model: str | None,
    wait_condition: WaitCondition | None = None,
) -> str:
    """Recover from an empty LLM response by digesting recent tool results.

    Centralised version of the empty-response handling previously duplicated
    in ``agent_chat.py`` and ``general_chat.py``.  Tries a tool-results
    digest first; falls back to the generic ``EMPTY_RESPONSE_FALLBACK``
    string when there are no tool results to summarise.

    *context* is a short label injected into the log line (e.g. ``"Agent
    xxx"`` or ``"General assistant"``) so the source of the empty response
    is obvious in production logs. *wait_condition* is the caller's task
    resolver: when the digest turns out to be a background-task park, the
    tasks are named through it like everywhere else. This runs
    outside the loop, after it returned, so there is no run deadline to track
    here — the default :data:`_PARK_LABEL_LOOKUP_TIMEOUT_SECS` bound alone
    keeps a stalled pool from delaying the reply.
    """
    synthesised = await summarise_tool_results_resolved(messages, wait_condition)
    tool_count = sum(1 for m in messages if m.role == "tool")
    if synthesised:
        logger.warning(
            "%s returned empty response for conversation %s "
            "(model=%s, history_msgs=%d) — using synthesised digest "
            "from %d tool results",
            context,
            conversation_id,
            model,
            len(messages),
            tool_count,
        )
        return synthesised
    logger.error(
        "%s returned empty response for conversation %s "
        "(model=%s, history_msgs=%d) and no tool results to "
        "synthesise — using generic fallback",
        context,
        conversation_id,
        model,
        len(messages),
    )
    return EMPTY_RESPONSE_FALLBACK


def _trim_trace_text(text: str | None, limit: int) -> str:
    """Keep trace payloads bounded to avoid oversized evaluation JSON blobs."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... [TRUNCATED IN TRACE] ..."


def _merge_trace_reasoning(captured: str | None, response_thinking: str | None) -> str | None:
    """Combine streamed and non-stream reasoning into one trace string.

    Deliberately not truncated.  Reasoning is what a post-hoc audit reads to
    understand why a run did what it did, and the trace store holds the only
    copy.  The previous 20 000-character cap was also the whole-payload cap, so
    a long reasoning block pushed the serialized generation output past the
    limit, the JSON came back as an unparseable string, and the inspector's
    reasoning pane disappeared entirely.  Size is now handled at persistence
    time by offloading to object storage
    (``services/tracing/payload_offload.py``).
    """
    parts: list[str] = []
    for candidate in (captured, response_thinking):
        if isinstance(candidate, str) and candidate.strip():
            parts.append(candidate.strip())
    if not parts:
        return None
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]
    return "\n\n".join(parts)


def _build_generation_trace_output(
    response: ChatResponse,
    *,
    captured_reasoning: str | None = None,
) -> Any:
    """Build generation output payload including reasoning when available."""
    reasoning = _merge_trace_reasoning(captured_reasoning, response.thinking)

    if response.tool_calls:
        payload: dict[str, Any] = {
            "content": response.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, default=str),
                    },
                }
                for tc in response.tool_calls
            ],
        }
        if reasoning:
            payload["reasoning"] = reasoning
        return payload

    if reasoning:
        return {
            "content": response.content,
            "reasoning": reasoning,
        }

    return response.content


def _record_request_trace_metadata(
    gen_ctx: dict | None, response: ChatResponse
) -> None:
    """Merge what the provider reported about the request into its generation.

    ``ChatResponse.trace_metadata`` carries facts only the provider knows
    (Anthropic: ``thinking_replay``); ``enrich_trace_metadata`` patches
    the live Langfuse observation and the durable local span alike.
    """
    metadata = getattr(response, "trace_metadata", None)
    if gen_ctx is not None and isinstance(metadata, dict) and metadata:
        _lf_enrich_trace_metadata(gen_ctx, metadata)


def _end_returned_generation(
    gen_ctx: dict | None,
    response: ChatResponse,
    *,
    model: str | None,
    reasoning: list[str] | None,
) -> None:
    """Close the generation of a call that returned, with what it returned:
    its output (and the reasoning captured while it streamed), its usage, its
    model and its stop reason."""
    _record_request_trace_metadata(gen_ctx, response)
    _lf_end_generation(
        gen_ctx,
        output=_build_generation_trace_output(
            response,
            captured_reasoning="".join(reasoning) if reasoning else None,
        ),
        usage=response.usage,
        model=response.model or model,
        stop_reason=response.stop_reason,
    )


def _build_llm_prompt_trace(messages: list[ChatMessage]) -> tuple[str | None, str | None]:
    """Build a compact prompt snapshot for per-iteration evaluation traces."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    other_parts: list[str] = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        if m.role == "system":
            system_parts.append(content)
        elif m.role == "user":
            user_parts.append(content)
        elif m.role == "tool":
            tool_name = m.name or "tool"
            other_parts.append(f"[tool:{tool_name}] {content}")
        elif m.role == "assistant":
            other_parts.append(f"[assistant] {content}")

    system_prompt = _trim_trace_text("\n\n".join(system_parts), 30_000) or None
    # User prompt contains user turns and compact conversation/tool context.
    user_prompt = _trim_trace_text("\n\n".join([*user_parts, *other_parts]), 80_000) or None
    return system_prompt, user_prompt


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started*, a ``time.perf_counter()`` reading."""
    return max(0, int((time.perf_counter() - started) * 1000))


def _tool_timing(enabled: bool, duration_ms: int, group: str | None = None) -> dict[str, Any]:
    """The time a ``tool_done``'s own tool ran, for a run that asks for it.

    An evaluation times each tool from these: a batch's one ``tool_start``
    cannot split the batch's time between its calls. A call that never ran
    (refused, over budget, answered from the cache) carries 0; the calls that
    ran together share *group*. Only an evaluation asks
    (``report_tool_durations``); other runs' events carry no timing.
    """
    if not enabled:
        return {}
    timing: dict[str, Any] = {"duration_ms": duration_ms}
    if group:
        timing["parallel_group"] = group
    return timing


def _serialize_tool_arguments(arguments: dict[str, Any] | None) -> str:
    """Serialize tool arguments for trace payloads with bounded size."""
    if not arguments:
        return "{}"
    try:
        serialized = json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        serialized = str(arguments)
    return _trim_trace_text(serialized, 30_000)


def _serialize_tool_arguments_full(arguments: dict[str, Any] | None) -> str:
    """Serialize tool arguments without trace truncation."""
    if not arguments:
        return "{}"
    try:
        return json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return str(arguments)


# ---------------------------------------------------------------------------
# Pre-execution tool-argument validation (CORE-2)
#
# Tool arguments are validated against the ToolDefinition.input_schema BEFORE
# the call is dispatched to the executor.  On failure the tool is NOT executed
# — a structured tool-result error is appended instead so the model receives
# "missing required field X" feedback and self-corrects on the next iteration,
# rather than the executor silently dropping unknown kwargs or failing
# opaquely.  Validation uses the ``jsonschema`` library (already a pinned
# backend dependency — it ships with the MCP SDK and is listed in
# pyproject.toml), so no minimal hand-rolled validator is needed.
# ---------------------------------------------------------------------------

# Cap the compact-schema echo embedded in validation feedback so a huge MCP
# schema cannot bloat the conversation.
_VALIDATION_SCHEMA_MAX_CHARS = 1_500

# Cap the number of individual schema violations reported per call — the
# first few are enough for the LLM to self-correct.
_VALIDATION_MAX_ERRORS = 3

# Cap each individual violation message — jsonschema embeds the offending
# instance value in its messages, which can carry arbitrarily large payloads.
_VALIDATION_ERROR_MAX_CHARS = 300


# Schema keywords that carry prose, not structure. They are stripped from the
# validation echo BEFORE the length cap so a verbose schema is cut in its
# descriptions, never in the property names / types / enums / ``required`` the
# model needs to correct the call (a 900-char ``inspect_my_traces``
# schema was echoed with its descriptions and the model never saw the
# parameter list whole). Only STRING values are stripped - a property that
# happens to be NAMED ``description`` or ``title`` (Jira, Notion, ...) has a
# dict value and stays.
_SCHEMA_PROSE_KEYS = frozenset({"description", "title", "$comment", "examples"})


def _strip_schema_prose(node: Any, _depth: int = 0) -> Any:
    """Return *node* with description-like prose removed (structure intact)."""
    if _depth > 32:
        return node
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _SCHEMA_PROSE_KEYS and isinstance(value, (str, list)):
                continue
            out[key] = _strip_schema_prose(value, _depth + 1)
        return out
    if isinstance(node, list):
        return [_strip_schema_prose(item, _depth + 1) for item in node]
    return node


# ---------------------------------------------------------------------------
# Schema regexes are never evaluated server-side
#
# Tool schemas come from MCP servers, custom tools and imported bundles, none
# of which the platform authored. Every check against one is built on
# ``app.services.schema_guard``, the mechanism the agent output-schema checks
# use too: a registry that retrieves nothing, the class the check chose
# kept for the whole document, and one set of regex keywords to which each
# check passes its own judge.
#
# Argument validation passes a judge that never runs a pattern, wherever the
# validator meets it: the tool (or remote server) validates its own input, and
# the schema the model is given keeps its patterns, so the constraint is still
# visible to it. Structure (required, types, enums, bounds) is enforced as
# before.
#
# Ignoring a keyword only loosens a schema where no enclosing applicator is
# non-monotone: under ``not`` / ``oneOf`` / ``if`` it refuses valid calls
# instead (``not: {"pattern": ...}`` reads as ``not: {}``), and a ``$ref`` can
# reach such a place from anywhere in the document. A schema holding both, or
# referencing another document, is not pre-validated at all: the tool
# validates the call. Argument coercion is unchanged by any of this (see
# ``_coercion_validator``).
# ---------------------------------------------------------------------------


def _never_judged(pattern: Any, text: str) -> None:
    return None


def _no_verdict_needed(what: str, why: str) -> None:
    return None


_REGEX_FREE_VALIDATORS: dict[tuple[type, bool], type] = {}


def _regex_free_validator_class(base: type, *, ignore_unevaluated_properties: bool) -> type:
    """*base* with every keyword that evaluates a schema regex doing nothing.

    ``unevaluatedProperties`` matches keys against every ``patternProperties``
    it can reach, so it is ignored as well when the schema holds one.
    """
    key = (base, ignore_unevaluated_properties)
    if key not in _REGEX_FREE_VALIDATORS:
        keywords = schema_guard.regex_keywords(
            base,
            judge=_never_judged,
            no_verdict=_no_verdict_needed,
            holds_pattern_properties=ignore_unevaluated_properties,
        )
        _REGEX_FREE_VALIDATORS[key] = schema_guard.extend_pinned(
            base,
            keywords,
            functools.partial(
                _regex_free_validator_class, ignore_unevaluated_properties=ignore_unevaluated_properties
            ),
        )
    return _REGEX_FREE_VALIDATORS[key]


def _left_to_the_tool(census: schema_guard.SchemaCensus) -> bool:
    """Could the schema not be checked without fetching a document or tightening it?"""
    return census.remote_ref or (census.regex and census.non_monotone)


def _argument_validator(schema: Any) -> Any | None:
    """A validator for *schema* that evaluates none of its regexes and fetches nothing.

    ``None`` when pre-validation must be skipped: every validator returned
    accepts every call *schema* accepts.
    """
    census = schema_guard.take_census(schema)
    if _left_to_the_tool(census):
        return None
    validator_cls = _regex_free_validator_class(
        jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator),
        ignore_unevaluated_properties=census.pattern_properties,
    )
    return validator_cls(schema, registry=_LOCAL_SCHEMA_REFS_ONLY)


# The union check of argument coercion (``_already_valid``) keeps the verdict
# it always gave, patterns included: a value an alternative's pattern admits
# must not be converted, and one no alternative admits still is. Patterns run
# on the bounded engine, read as ``re`` reads them, within a budget shared by
# one call's coercion (CPU time, ``schema_guard.regex_stopwatch``); a pattern
# with no verdict in time, or none ``re`` is sure to give, leaves the value as
# sent, as does a reference to another document.
_COERCION_REGEX_BUDGET_SECONDS = 0.1
_coercion_regex_stopwatch: ContextVar[CpuStopwatch | None] = ContextVar("_coercion_regex_stopwatch", default=None)
_NOT_JUDGED_WARNED: set[str] = set()


class _NotJudgedError(Exception):
    """The union check could not give its verdict within its bounds."""


def _bounded_search(pattern: Any, text: str) -> bool:
    stopwatch = _coercion_regex_stopwatch.get()
    if stopwatch is None:
        stopwatch = schema_guard.regex_stopwatch()
    remaining = _COERCION_REGEX_BUDGET_SECONDS - stopwatch.charged()
    try:
        return schema_guard.bounded_search(pattern, text, remaining=remaining)
    except schema_guard.PatternUnjudgedError as exc:
        why = exc.why
        if why in (schema_guard.NO_TIME, schema_guard.TIMED_OUT):
            why = f"did not finish within {_COERCION_REGEX_BUDGET_SECONDS} s"
        raise _NotJudgedError(pattern, why) from None


def _no_union_verdict(what: str, why: str) -> NoReturn:
    raise _NotJudgedError(what, why)


_COERCION_VALIDATORS: dict[tuple[type, bool], type] = {}


def _coercion_validator_class(base: type, holds_pattern_properties: bool) -> type:
    key = (base, holds_pattern_properties)
    if key not in _COERCION_VALIDATORS:
        keywords = schema_guard.regex_keywords(
            base,
            judge=_bounded_search,
            no_verdict=_no_union_verdict,
            holds_pattern_properties=holds_pattern_properties,
        )
        _COERCION_VALIDATORS[key] = schema_guard.extend_pinned(
            base,
            keywords,
            functools.partial(_coercion_validator_class, holds_pattern_properties=holds_pattern_properties),
        )
    return _COERCION_VALIDATORS[key]


def _coercion_validator(schema: Any) -> Any:
    """A validator for *schema* with its patterns bounded and nothing fetched."""
    base = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    validator_cls = _coercion_validator_class(base, schema_guard.take_census(schema).pattern_properties)
    return validator_cls(schema, registry=_LOCAL_SCHEMA_REFS_ONLY)


def _warn_not_judged(what: str, why: str) -> None:
    what = what[:120]
    if what in _NOT_JUDGED_WARNED or len(_NOT_JUDGED_WARNED) >= 256:
        return
    _NOT_JUDGED_WARNED.add(what)
    logger.warning("Argument coercion left a union value as sent: %r %s", what, why)


def _compact_schema(schema: dict[str, Any]) -> str:
    """Serialise *schema* compactly for inclusion in validation feedback.

    Descriptions are dropped: the echo exists so the model can fix the
    argument SHAPE, and the prose is what pushed the shape past the cap.
    """
    try:
        text = json.dumps(
            _strip_schema_prose(schema),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except Exception:
        return "<unavailable>"
    if len(text) > _VALIDATION_SCHEMA_MAX_CHARS:
        text = text[:_VALIDATION_SCHEMA_MAX_CHARS] + "... [truncated]"
    return text


# ---------------------------------------------------------------------------
# Pre-validation argument coercion
#
# Several models emit every tool argument as a JSON string - ``"limit": "1"``,
# ``"include_spans": "true"`` - and, told "'true' is not of type 'boolean'",
# retry the exact same shape until the failure budget is spent. A string
# that reads unambiguously as the declared primitive is not an error to teach
# the model out of; it is an input to accept. Coercion is STRICT by design:
#
# * it only fires when the declared type does NOT admit ``string`` (a
#   ``["string", "integer"]`` union keeps the string - the model may mean it);
# * ``"true"`` / ``"false"`` -> bool (case-insensitive), ``"12"`` -> int,
#   ``"1.5"`` -> float (finite only), ``"null"`` -> None when ``null`` is
#   declared, a JSON-looking string -> list / dict when ``array`` / ``object``
#   is declared and it parses to that type;
# * anything else is left as-is for :func:`validate_tool_arguments` to report.
#
# Nested values are coerced through ``items`` / ``properties`` so a
# ``["1", "2"]`` for an integer array also lands. Recursion is bounded.
#
# ``anyOf`` / ``oneOf`` unions are the one place a coercion could change a
# VALID value's meaning, so two guards apply there: a value that already
# validates against the union is left untouched (``{"id": "001"}`` under an
# integer-or-string ``id`` keeps its zeros), and the walk descends into an
# alternative only when exactly ONE alternative declares the value's JSON
# type - with several, nothing is coerced and the validator reports.
# ---------------------------------------------------------------------------

_COERCE_MAX_DEPTH = 6
_INT_LITERAL_RE = re.compile(r"^[+-]?\d+$")


def _declared_types(prop_schema: Any) -> set[str]:
    """The JSON Schema ``type`` set a property admits (unions flattened)."""
    if not isinstance(prop_schema, dict):
        return set()
    declared = prop_schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    for key in ("anyOf", "oneOf"):
        alternatives = prop_schema.get(key)
        if isinstance(alternatives, list):
            out: set[str] = set()
            for alt in alternatives:
                out |= _declared_types(alt)
            if out:
                return out
    return set()


def _is_union(prop_schema: dict[str, Any]) -> bool:
    """Does *prop_schema* offer ``anyOf`` / ``oneOf`` alternatives?"""
    return any(isinstance(prop_schema.get(key), list) for key in ("anyOf", "oneOf"))


def _already_valid(value: Any, prop_schema: dict[str, Any]) -> bool:
    """Does *value* satisfy *prop_schema* exactly as the model sent it?

    Consulted on union nodes only: a value that already validates against
    the whole union is the model's choice of branch, and coercing it into a
    different branch would silently change its meaning (``{"id": "001"}``
    under an integer-or-string ``id`` is the string branch - turning it into
    ``{"id": 1}`` loses the leading zeros and nothing ever reports it). A
    schema the validator cannot evaluate reads as "not known to be valid",
    which only ever falls through to the unique-alternative rule below. A
    verdict that would need a pattern past its budget, or a document that is
    never fetched, reads as valid: the value reaches the tool exactly as sent.
    """
    try:
        return _coercion_validator(prop_schema).is_valid(value)
    except _NotJudgedError as exc:
        what, why = exc.args
        _warn_not_judged(str(what), why)
        return True
    except Exception as exc:
        if not schema_guard.retrieval_refused(exc):
            return False
        _warn_not_judged(format_exception_message(exc), "needs a document that is never fetched")
        return True


def _unique_alternative(prop_schema: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """The sub-schema to descend into for a value of JSON type *kind*.

    A plain schema is its own answer. For an ``anyOf`` / ``oneOf`` union it
    is the alternative declaring *kind* - and ONLY when exactly one does:
    with two object alternatives the value's shape does not say which the
    model meant, and descending into the first would coerce the nested
    fields toward a branch the model never chose. ``None`` means "do not
    coerce; let the validator report what is wrong".
    """
    if not _is_union(prop_schema):
        return prop_schema
    matches: list[dict[str, Any]] = []
    for key in ("anyOf", "oneOf"):
        alternatives = prop_schema.get(key)
        if isinstance(alternatives, list):
            matches.extend(alt for alt in alternatives if kind in _declared_types(alt))
    return matches[0] if len(matches) == 1 else None


def _coerce_string(value: str, types: set[str]) -> Any:
    """Coerce a string scalar to the declared primitive when unambiguous."""
    if "string" in types or not types:
        return value
    text = value.strip()
    lowered = text.lower()
    if "boolean" in types and lowered in ("true", "false"):
        return lowered == "true"
    if "null" in types and lowered == "null":
        return None
    if "integer" in types and _INT_LITERAL_RE.match(text):
        try:
            return int(text)
        except ValueError:
            return value
    if "number" in types:
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None and number == number and abs(number) != float("inf"):
            return int(text) if _INT_LITERAL_RE.match(text) else number
    if ("array" in types and text.startswith("[")) or (
        "object" in types and text.startswith("{")
    ):
        try:
            parsed = json.loads(text)
        except ValueError:
            return value
        if isinstance(parsed, list) and "array" in types:
            return parsed
        if isinstance(parsed, dict) and "object" in types:
            return parsed
    return value


def _coerce_value(value: Any, prop_schema: Any, depth: int) -> Any:
    if depth > _COERCE_MAX_DEPTH or not isinstance(prop_schema, dict):
        return value
    # A union the value already satisfies is left exactly as sent: the model
    # picked a branch, and every coercion below would move it to another.
    if _is_union(prop_schema) and _already_valid(value, prop_schema):
        return value
    types = _declared_types(prop_schema)
    if isinstance(value, str):
        value = _coerce_string(value, types)
    if isinstance(value, list) and "array" in types:
        alternative = _unique_alternative(prop_schema, "array")
        if alternative is None:
            return value
        items_schema = alternative.get("items")
        if isinstance(items_schema, dict):
            new_items = [_coerce_value(item, items_schema, depth + 1) for item in value]
            if all(new is old for new, old in zip(new_items, value, strict=True)):
                return value
            return new_items
        return value
    if isinstance(value, dict) and "object" in types:
        alternative = _unique_alternative(prop_schema, "object")
        if alternative is None:
            return value
        properties = alternative.get("properties")
        if isinstance(properties, dict):
            new_dict = {
                key: (
                    _coerce_value(item, properties[key], depth + 1)
                    if key in properties
                    else item
                )
                for key, item in value.items()
            }
            if all(new_dict[key] is value[key] for key in value):
                return value
            return new_dict
        return value
    return value


def coerce_tool_arguments(
    arguments: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    *,
    skip_keys: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """Return *arguments* with stringified scalars coerced to their declared types.

    Returns the SAME object when nothing changes (so callers can keep identity
    checks) and a shallow copy with the coerced values otherwise. Never
    raises: an uninterpretable schema leaves the arguments untouched for the
    validator to judge.

    *skip_keys* are left exactly as passed - the loop names the keys a
    ``$collected`` injection replaced with a platform-written JSON string,
    which must reach the tool as that string whatever the schema declares.
    """
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        return arguments
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return arguments
    budget = _coercion_regex_stopwatch.set(schema_guard.regex_stopwatch())
    try:
        coerced: dict[str, Any] | None = None
        for key, value in arguments.items():
            if skip_keys and key in skip_keys:
                continue
            prop_schema = properties.get(key)
            if prop_schema is None:
                continue
            new_value = _coerce_value(value, prop_schema, 0)
            if new_value is value:
                continue
            if coerced is None:
                coerced = dict(arguments)
            coerced[key] = new_value
    except Exception:  # pragma: no cover - defensive: never block a call here
        logger.debug("Argument coercion skipped", exc_info=True)
        return arguments
    finally:
        _coercion_regex_stopwatch.reset(budget)
    return coerced if coerced is not None else arguments


#: Checked validators by schema object. ``check_schema`` walks a
#: tool's whole schema against the metaschema - ~100 ms for an MCP schema with
#: a large ``$defs`` - and ran on every call of the tool, on the event loop.
#: Keyed by identity with the schema held in the entry, so an id cannot be
#: reused while its entry lives; a tool's schema is the same object for the
#: whole run (``ToolDefinition.input_schema``). Bounded above a provider's tool
#: cap, since an entry keeps its schema alive.
_SCHEMA_VALIDATORS_MAX = 256
_schema_validators: OrderedDict[int, tuple[dict[str, Any], Any]] = OrderedDict()
#: ``_validate_tool_arguments_excluding``'s reduced schemas, by schema object and
#: excluded keys, so the reduced schema is one object too and its validator is
#: kept like any other.
_reduced_schemas: OrderedDict[
    tuple[int, frozenset[str]], tuple[dict[str, Any], dict[str, Any]]
] = OrderedDict()


def _remember(cache: OrderedDict, key: Any, entry: tuple) -> None:
    cache[key] = entry
    cache.move_to_end(key)
    while len(cache) > _SCHEMA_VALIDATORS_MAX:
        cache.popitem(last=False)


def _checked_validator(schema: dict[str, Any]) -> Any | None:
    """``_argument_validator(schema)``, whose metaschema check runs once per schema.

    ``None`` (kept too) when the schema is not pre-validated. Raises what the
    check raises for a schema that cannot be interpreted, on every call: a
    failed check is not kept, so the caller's skip-and-warn path is unchanged.
    """
    key = id(schema)
    entry = _schema_validators.get(key)
    if entry is not None and entry[0] is schema:
        _schema_validators.move_to_end(key)
        return entry[1]
    # Never the schema's own regexes, never a remote document.
    validator = _argument_validator(schema)
    if validator is not None:
        # No format checker: it would compile every ``pattern`` of the schema.
        jsonschema.validators.validator_for(
            schema, default=jsonschema.Draft202012Validator
        ).check_schema(schema, format_checker=None)
    _remember(_schema_validators, key, (schema, validator))
    return validator


def validate_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any] | None,
    schema: dict[str, Any] | None,
) -> str | None:
    """Validate *arguments* against *schema*; return feedback text on failure.

    Returns ``None`` when the arguments are valid — or when validation is not
    possible (no/invalid schema), in which case execution proceeds as before
    so an exotic MCP schema can never block a legitimate call.

    Two failure shapes are handled:

    * ``{"raw": ...}`` — the marker ``app.llm.base.parse_tool_arguments``
      emits when the LLM produced malformed JSON.  Reported as such whenever
      the schema declares a ``properties`` map that lacks ``raw`` — including
      the empty ``properties: {}`` default of no-arg tools, for which a
      ``{"raw": ...}`` payload can only be the marker.  Only a schema with
      NO ``properties`` map at all (free-form object) cannot distinguish the
      marker from a legitimate ``{"raw": ...}`` argument, so it falls through
      to plain schema validation instead.
    * Schema violations — missing required fields, wrong types, etc., found
      via ``jsonschema`` ``iter_errors``.
    """
    if not isinstance(schema, dict) or not schema:
        return None

    args = arguments if isinstance(arguments, dict) else {}

    # Malformed-JSON fallback from parse_tool_arguments: the LLM emitted
    # invalid JSON and the provider layer wrapped it as {"raw": "..."}.
    # Flag it when the tool declares a ``properties`` map that lacks ``raw``
    # — ``declared is not None`` (not truthiness) so the EMPTY map of no-arg
    # tools (the ``ToolDefinition.input_schema`` default) also counts: a
    # ``{"raw": ...}`` payload for a tool with ``properties: {}`` can only be
    # the marker.  Only a free-form schema (no ``properties`` key at all)
    # cannot distinguish the marker from a legitimate ``{"raw": ...}``
    # payload, and there pre-validation behaviour (execute) is the safer
    # default.
    properties = schema.get("properties")
    declared = (
        set(properties.keys()) if isinstance(properties, dict) else None
    )
    if (
        set(args.keys()) == {"raw"}
        and declared is not None
        and "raw" not in declared
    ):
        return (
            f"Invalid arguments for tool {tool_name}: the arguments were not "
            "valid JSON (the raw text could not be parsed). "
            f"Expected schema: {_compact_schema(schema)}. "
            "Please emit a single well-formed JSON object matching the schema "
            "and retry. The tool was NOT executed."
        )

    try:
        validator = _checked_validator(schema)
        if validator is None:
            logger.debug(
                "Skipping argument pre-validation for tool '%s': its schema "
                "references another document, or mixes a regex with not / "
                "oneOf / if",
                tool_name,
            )
            return None
        # Sort key stringifies every path element: ``absolute_path`` mixes
        # ints (array indices) and strs (property names), and a bare
        # ``list(...)`` key would make ``sorted`` raise ``TypeError`` on an
        # int-vs-str comparison — which the broad except below would then
        # misread as "uninterpretable schema" and silently skip validation.
        errors = sorted(
            validator.iter_errors(args),
            key=lambda e: [str(p) for p in e.absolute_path],
        )
    except Exception:
        # The schema itself is invalid or unsupported (MCP servers can return
        # anything) — never block execution on a schema we cannot interpret.
        # WARNING (not DEBUG): a broken schema silently disables argument
        # validation for every call to this tool, which is worth surfacing
        # in production logs.
        logger.warning(
            "Skipping argument validation for tool '%s' — schema could not "
            "be interpreted (validation is disabled for this tool)",
            tool_name,
            exc_info=True,
        )
        return None

    if not errors:
        return None

    details: list[str] = []
    for err in errors[:_VALIDATION_MAX_ERRORS]:
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        details.append(f"{path}: {err.message}"[:_VALIDATION_ERROR_MAX_CHARS])
    if len(errors) > _VALIDATION_MAX_ERRORS:
        details.append(f"... and {len(errors) - _VALIDATION_MAX_ERRORS} more error(s)")
    return (
        f"Invalid arguments for tool {tool_name}: {'; '.join(details)}. "
        f"Expected schema: {_compact_schema(schema)}. "
        "Please correct the arguments and retry. The tool was NOT executed."
    )


def _validate_tool_arguments_excluding(
    tool_name: str,
    arguments: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    exclude_keys: set[str],
) -> str | None:
    """Validate *arguments* against *schema* while ignoring *exclude_keys*.

    The excluded keys hold a platform-controlled ``$collected`` JSON-string
    injection whose serialized form intentionally may not match the declared
    parameter type (the documented bulk contract passes a string even to an
    array/object-typed parameter), so they are trusted and dropped from BOTH
    the argument object and the schema (``properties`` + ``required``) before
    validation. Every OTHER argument is still schema-checked — missing required
    fields and wrong types on the same call are no longer masked by the
    injection (decoding the JSON string and validating it instead would falsely
    reject a string-typed parameter, so exclusion is the correct approach).
    """
    if not isinstance(schema, dict) or not schema or not exclude_keys:
        return validate_tool_arguments(tool_name, arguments, schema)
    reduced_args = {
        k: v
        for k, v in (arguments or {}).items()
        if k not in exclude_keys
    }
    return validate_tool_arguments(
        tool_name, reduced_args, _schema_without(schema, frozenset(exclude_keys))
    )


def _schema_without(schema: dict[str, Any], exclude_keys: frozenset[str]) -> dict[str, Any]:
    """*schema* with *exclude_keys* dropped from ``properties`` and ``required``."""
    key = (id(schema), exclude_keys)
    entry = _reduced_schemas.get(key)
    if entry is not None and entry[0] is schema:
        _reduced_schemas.move_to_end(key)
        return entry[1]
    reduced_schema = dict(schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        reduced_schema["properties"] = {
            k: v for k, v in properties.items() if k not in exclude_keys
        }
    required = schema.get("required")
    if isinstance(required, list):
        reduced_schema["required"] = [r for r in required if r not in exclude_keys]
    _remember(_reduced_schemas, key, (schema, reduced_schema))
    return reduced_schema


def _terminal_tool_decision(
    *,
    tool_name: str,
    result: ToolResult,
    finalize_on_tool: dict[str, Any] | None,
    tool_name_map: Mapping[str, str],
    output_schema: dict[str, Any] | None,
) -> TerminalToolDecision:
    """Decide whether a trusted tool may end the loop.

    The terminal signal is runtime control metadata, not model-facing prompt
    text. Only an explicitly configured tool can trigger this path, and only
    when its JSON result declares success and exposes a dict output. Schema
    failures deliberately fall back to the normal loop so the model can see
    the validator feedback instead of receiving an invalid final response.
    """
    if not isinstance(finalize_on_tool, dict):
        return TerminalToolDecision(None, "not_configured")
    if result.is_error:
        return TerminalToolDecision(None, "tool_result_error")

    configured_tool = finalize_on_tool.get("tool_name")
    if not isinstance(configured_tool, str) or not configured_tool:
        return TerminalToolDecision(None, "configured_tool_missing")

    original_tool_name = tool_name_map.get(tool_name, tool_name)
    if configured_tool not in {tool_name, original_tool_name}:
        return TerminalToolDecision(
            None,
            "tool_name_mismatch",
            f"configured={configured_tool} actual={tool_name} original={original_tool_name}",
        )

    # A tool that keeps a large output out of the model's view (the STIX
    # validator answers a big bundle by reference) gives the whole result on
    # ``structured``: the final output is read there, never from the text.
    if isinstance(result.structured, dict):
        payload: Any = result.structured
    else:
        try:
            payload = json.loads(result.content)
        except (TypeError, json.JSONDecodeError) as exc:
            return TerminalToolDecision(None, "invalid_tool_json", str(exc))

    if not isinstance(payload, dict):
        return TerminalToolDecision(
            None,
            "tool_payload_not_object",
            type(payload).__name__,
        )

    valid_key = finalize_on_tool.get("valid_key", "valid")
    output_key = finalize_on_tool.get("output_key", "bundle")
    # Defensive: this config is persisted JSON that an admin can edit, so a
    # mis-typed key (e.g. a list/dict) would otherwise raise
    # ``TypeError: unhashable type`` inside ``payload.get(...)`` and abort the
    # whole agent loop instead of cleanly falling back.  Coerce any non-string
    # (or empty) key back to the documented default.
    if not isinstance(valid_key, str) or not valid_key:
        valid_key = "valid"
    if not isinstance(output_key, str) or not output_key:
        output_key = "bundle"
    if payload.get(valid_key) is not True:
        return TerminalToolDecision(
            None,
            "validation_not_successful",
            f"{valid_key}={payload.get(valid_key)!r}",
        )

    final_output = payload.get(output_key)
    if not isinstance(final_output, dict):
        return TerminalToolDecision(
            None,
            "bundle_not_object",
            f"{output_key}={type(final_output).__name__}",
        )

    should_validate_schema = finalize_on_tool.get("validate_output_schema") is True
    schema = raw_json_schema(output_schema) if should_validate_schema else None
    if schema:
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
            # Bounded regexes, nothing retrieved; the first error, as ``validate`` raised it.
            for error in output_schema_errors(
                output_schema_validator(schema, validator_cls), final_output, limit=1
            ):
                raise error
        except Exception as exc:
            return TerminalToolDecision(
                None,
                "structured_output_validation_failed",
                str(exc),
            )
    elif should_validate_schema and output_schema:
        return TerminalToolDecision(
            None,
            "structured_output_schema_unrecognized",
            f"type={type(output_schema).__name__} keys={list(output_schema.keys())[:8]}",
        )

    return TerminalToolDecision(
        json.dumps(final_output, ensure_ascii=False),
        "final_llm_call_skipped",
    )


def _log_tool_result(tc: ToolCall, result: ToolResult, probe: str | None = None) -> None:
    """Log a tool result at the appropriate level.

    - ``is_error=True`` → WARNING with content preview.
    - Content starting with ``"Error"`` → WARNING (integration tools often
      return error strings without setting ``is_error``).
    - Otherwise → INFO with character count only.

    Either error is also a friction of the run (``services/frictions.py``),
    recorded in the background under the run's bound scope, with no model call.
    A call that declared ``expect_error`` (*probe*, its reason, see
    :func:`_strip_expect_error`) records its error already disposed of
    (``expected_error``) and its success as ``probe_passed_unexpectedly``.
    """
    errored = result.is_error or result.content.startswith("Error")
    if probe is not None:
        from app.services.frictions import note_expected_error, note_probe_passed

        if errored:
            note_expected_error(tc.name, result.content, probe)
        else:
            note_probe_passed(tc.name, probe)
    elif errored:
        from app.services.ctem_module_preflight import NO_MODULE_ACCOUNT, note_tool_refusal
        from app.services.frictions import note_expected_error, note_tool_error
        from app.services.integration_auth_guard import AUTH_FAILURE_RECORDED_REF

        # A per-user CTEM stage run refused by a module the member has no
        # account on: expected, recorded already disposed of (#2042).
        if (refused := note_tool_refusal(tc.name, result.content)) is not None:
            note_expected_error(tc.name, result.content, f"{NO_MODULE_ACCOUNT}: {refused}")
        elif result.auth_failure_recorded:
            # An integration 401 / 403 the run's auth guard already recorded on
            # integration_auth_failures and the coverage notice: reported once,
            # never an undisposed friction on top (#2204).
            note_expected_error(
                tc.name,
                result.content,
                f"{AUTH_FAILURE_RECORDED_REF}: {result.auth_failure_recorded}",
            )
        else:
            note_tool_error(tc.name, result.content)
    if result.is_error:
        logger.warning(
            "Tool '%s' returned error (id=%s): %.500s",
            tc.name,
            tc.id,
            result.content,
        )
    elif result.content.startswith("Error"):
        # Integration tools conventionally return "Error: ..." strings
        # without setting is_error — still log prominently.
        logger.warning(
            "Tool '%s' returned error result (id=%s): %.500s",
            tc.name,
            tc.id,
            result.content,
        )
    else:
        logger.info(
            "Tool '%s' completed (id=%s, %d chars)",
            tc.name,
            tc.id,
            len(result.content),
        )


def _strip_expect_error(tc: ToolCall, schema: dict[str, Any] | None) -> str | None:
    """Take the ``expect_error`` probe declaration off *tc*; return its reason, or ``None``.

    Any tool call may carry a top-level ``expect_error`` (``true`` or a
    one-line reason) to declare its error the expected outcome of a probe
    (``services/frictions.py``). It is removed before validation and dispatch,
    so the tool never sees it. A tool whose own schema declares
    ``expect_error`` keeps the argument and declares no probe. A call without
    it is untouched.
    """
    from app.services.frictions import EXPECT_ERROR_ARGUMENT, expected_error_reason

    arguments = tc.arguments
    if not isinstance(arguments, dict) or EXPECT_ERROR_ARGUMENT not in arguments:
        return None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(properties, dict) and EXPECT_ERROR_ARGUMENT in properties:
        return None
    tc.arguments = {k: v for k, v in arguments.items() if k != EXPECT_ERROR_ARGUMENT}
    return expected_error_reason(arguments[EXPECT_ERROR_ARGUMENT])


def _truncate_tool_result(content: str, max_chars: int) -> str:
    """A tool result over *max_chars*, cut to its head and its tail (:func:`clip_head_and_tail`).

    The tail is kept because a log or an API answer ends with what matters
    (an error summary, a continuation token).
    """
    if len(content) <= max_chars:
        return content
    logger.warning(
        "Truncating tool result: %d chars → %d chars",
        len(content),
        max_chars,
    )
    return clip_head_and_tail(
        content,
        max_chars,
        hint=(
            f"The limit is {max_chars:,}: use more specific queries or smaller limits, "
            "or to_workspace_path where the tool offers it."
        ),
    )


def _estimate_prompt_tokens(
    messages: list[ChatMessage],
    tools: list[ToolDefinition] | None = None,
    tool_chars_cache: dict[int, int] | None = None,
) -> int:
    """Rough prompt-token estimate for one LLM call (chars / _CHARS_PER_TOKEN).

    Provider-independent and deliberately cheap: the per-run budget uses it
    only to size a call's *worst-case* reservation, which is released and
    replaced by the provider's real usage numbers the moment the call
    returns.  Over-estimating therefore costs a little conservatism at the
    gate, never accounting drift.

    ``tools`` adds the definitions the call carries, weighed as the context
    readout weighs them (:func:`_tool_definition_chars`).
    """
    tool_chars = sum(_tool_definition_chars(tool, tool_chars_cache) for tool in tools or ())
    return (_transcript_chars(messages) + tool_chars) // _CHARS_PER_TOKEN


@dataclass(frozen=True, slots=True)
class _DowngradePlan:
    """Everything the loop must re-derive to run on the fallback model.

    The loop pins several values to the model at start-up; leaving any of them
    stale after a switch is what turns "continued more cheaply" into a broken
    run.  ``max_session_chars`` / ``max_tool_result_chars`` are ``None`` when
    the caller pinned them explicitly — an explicit caller budget is not the
    loop's to re-derive.
    """

    llm: BaseLLMProvider
    model: str
    max_tokens: int
    context_window: int
    max_session_chars: int | None
    max_tool_result_chars: int | None
    provider_max_tools: int


def _plan_model_downgrade(
    fallback: BudgetFallbackModel,
    *,
    messages: list[ChatMessage],
    current_llm: BaseLLMProvider,
    current_model: str | None,
    current_max_tokens: int,
    max_tokens_is_explicit: bool,
    derive_context_budget: bool,
    tool_count: int,
    has_structured_output: bool,
    max_tools: int,
    gated: bool = False,
) -> _DowngradePlan | None:
    """Re-derive the loop's model-pinned values for the fallback, or refuse.

    Pure — it decides nothing about the budget and mutates nothing — so the
    three ways a downgrade can be the *wrong* move (see
    ``_DOWNGRADE_MAX_SYSTEM_FRACTION``) are all answered before anything is
    committed.  ``None`` means "no switch", always with a WARNING saying which
    guard refused and why, because "the run did not switch even though a
    fallback was configured" is otherwise unexplainable from the outside.
    """
    provider = fallback.provider
    if provider is None:
        logger.warning(
            "Budget-driven model downgrade to %s is not possible: no provider "
            "instance was resolved for it — not switching",
            fallback.model_id,
        )
        return None
    if fallback.model_id == current_model:
        logger.warning(
            "Budget-driven model downgrade skipped: the configured fallback "
            "model (%s) is the model this run is already using",
            fallback.model_id,
        )
        return None

    window = (
        (getattr(provider, "context_windows", None) or {}).get(fallback.model_id)
        or getattr(provider, "context_window", None)
        or _FALLBACK_CONTEXT_WINDOW
    )
    session_chars, tool_result_chars = _compute_context_budget(window)

    # Guard 1 — context window.  The conversational part of the transcript is
    # compacted against the newly derived budgets later in this same
    # iteration; the system prompt is not, so it is the part that decides
    # whether the fallback can hold this run at all.
    system_chars = _transcript_chars(m for m in messages if m.role == "system")
    if system_chars > session_chars * _DOWNGRADE_MAX_SYSTEM_FRACTION:
        logger.warning(
            "Budget-driven model downgrade to %s refused: its %s-token context "
            "window is too small for this run's system prompt (%s chars vs a "
            "%s-char session budget) — switching would require truncating the "
            "persona and security policy, so the run does not switch",
            fallback.model_id,
            f"{window:,}",
            f"{system_chars:,}",
            f"{session_chars:,}",
        )
        return None

    # Guard 2 — tool surface.  Providers cap ``max_tools`` differently.
    # ``tool_count`` is what the fallback would have to accept on EVERY
    # request: with progressive disclosure active the gate's REQUIRED budget
    # — the always-on core, ``load_tools`` and one slot for a gated tool
    # (``ToolGate.required_budget``; the revealed groups are re-capped by the
    # gate after the switch) — the whole list otherwise.  For a gated
    # run that count is at least ``MIN_GATED_BUDGET``, so a fallback that
    # could hold ``load_tools`` and nothing else (or nothing at all) is
    # refused here too: re-capping the gate to it would leave a gate that can
    # never reveal a tool (and, at 0, an over-cap request), and switching the
    # run to the legacy path mid-flight would drop the catalogue the model
    # has been told about — the same "worse than stopping" as dropping tools.
    # ``max_tools``: the fallback model's own cap (``max_tools_for``).
    provider_max_tools = max_tools
    if tool_count > provider_max_tools:
        logger.warning(
            "Budget-driven model downgrade to %s refused: this run %s %d "
            "tool(s) but provider '%s' caps at %d — dropping tools the model "
            "has already been told about mid-run is worse than stopping%s",
            fallback.model_id,
            "needs a tool budget of"
            if gated
            else "offers",
            tool_count,
            getattr(provider, "provider_name", "unknown"),
            provider_max_tools,
            f"; gated: core + load_tools + {MIN_GATED_BUDGET - 1} gated slot"
            if gated
            else "",
        )
        return None

    # Guard 3 — native structured output is formatted for the primary's
    # provider and carries the run's output contract.
    if has_structured_output and getattr(
        provider, "provider_name", None
    ) != getattr(current_llm, "provider_name", None):
        logger.warning(
            "Budget-driven model downgrade to %s refused: this run uses native "
            "structured output built for provider '%s' and the fallback is on "
            "'%s' — silently dropping it would break the run's output "
            "contract",
            fallback.model_id,
            getattr(current_llm, "provider_name", "unknown"),
            getattr(provider, "provider_name", "unknown"),
        )
        return None

    resolved_max_tokens = _resolve_effective_max_tokens(provider, fallback.model_id)
    if not isinstance(resolved_max_tokens, int) or resolved_max_tokens <= 0:
        logger.warning(
            "Budget-driven model downgrade to %s refused: unable to resolve an "
            "output-token limit for it on provider '%s'",
            fallback.model_id,
            getattr(provider, "provider_name", "unknown"),
        )
        return None
    # An explicit caller ``max_tokens`` is honoured, only clamped down to what
    # the fallback actually accepts — the same rule the failover clamp applies.
    max_tokens = (
        min(current_max_tokens, resolved_max_tokens)
        if max_tokens_is_explicit
        else resolved_max_tokens
    )

    return _DowngradePlan(
        llm=provider,
        model=fallback.model_id,
        max_tokens=max_tokens,
        context_window=window,
        max_session_chars=session_chars if derive_context_budget else None,
        max_tool_result_chars=tool_result_chars if derive_context_budget else None,
        provider_max_tools=provider_max_tools,
    )


def _working_window(context_window: int, working_window_tokens: int | None) -> int:
    """The window the working-context budgets of a run are computed on.

    The model's *context_window*, or *working_window_tokens* when that is set
    (above ``0``) and smaller. An event-driven listener that loops 40+ tool
    calls on a 1M-token model has no reason to carry more working context
    than it would on a 128k one: the budgets scaled on the 1M window let it
    re-send ~800k characters of tool results on every call (#2430).
    """
    if working_window_tokens and working_window_tokens > 0 and context_window > 0:
        return min(context_window, working_window_tokens)
    return context_window


def _compute_live_tool_result_budget(context_window: int) -> int:
    """Char budget for tool-result bodies kept verbatim in the live context.

    Window-scaled with a floor and a cap so the budget is generous enough for
    the immediate working set on small-context models and never unbounded on
    very large (1M-token) ones. See :func:`_clear_stale_tool_results`.
    """
    window = (
        context_window
        if context_window and context_window > 0
        else _FALLBACK_CONTEXT_WINDOW
    )
    budget = int(window * _TOOL_RESULT_LIVE_FRACTION * _CHARS_PER_TOKEN)
    return max(_TOOL_RESULT_LIVE_MIN_CHARS, min(budget, _TOOL_RESULT_LIVE_MAX_CHARS))


def _compute_tool_result_files_budget(context_window: int) -> int:
    """Byte budget for tool-result vision attachments kept in the live context.

    The vision counterpart of :func:`_compute_live_tool_result_budget`: images
    a tool attached to its result are forwarded to the model as native vision
    blocks, so what bounds them is not chars but the bytes of image data that
    stay attached. Window-scaled through the ``_TOOL_RESULT_FILES_*`` proxy
    with a floor (a few screenshots even on a small-context model) and a cap
    (never unbounded on a 1M-token window). See
    :func:`_clear_stale_tool_result_files`.
    """
    window = (
        context_window
        if context_window and context_window > 0
        else _FALLBACK_CONTEXT_WINDOW
    )
    budget = int(
        window * _TOOL_RESULT_FILES_LIVE_FRACTION * _TOOL_RESULT_FILES_BYTES_PER_TOKEN
    )
    return max(
        _TOOL_RESULT_FILES_LIVE_MIN_BYTES,
        min(budget, _TOOL_RESULT_FILES_LIVE_MAX_BYTES),
    )


def _is_curation_stub(content: str) -> bool:
    """True when a tool body was already replaced by a curation/prune stub."""
    return content.startswith(("[Earlier ", "[Duplicate ", "[Pruned tool result"))


#: The stale-clearing stub of a TEXT result - the one curation stub that tells
#: the model the data is gone AND that it may re-call the tool
#: (``_clear_stale_tool_results`` writes it, ``_is_refetchable_stub`` reads it
#: back through :data:`_REFETCHABLE_STUB_RE`, derived from this same string so
#: the writer and the matcher cannot drift). The loop breaker keys on it.
#: ``{tool}`` is the tool's LLM-visible name, ``{chars}`` the cleared
#: body's length with thousands separators, ``{target}`` what the call
#: addressed (`` for path=a.py, offset_line=100``, :func:`_tool_call_target`)
#: or empty: a model whose read windows were cleared is told WHICH window
#: went, so it re-reads that one alone instead of every range of the file
#: (gap reread-after-context-prune, run 3350c28d: the same windows read 2-3
#: times after two resumes). ``{digest}`` is a short trace of what the body
#: held (`` , 2 items`` or `` , first line "2 matching assets"``,
#: :func:`_tool_result_digest`) or empty, and the sentence says the result was
#: not empty and must be re-called before anything is stated about it: a stub
#: that gave only a size read as "no rows" to a model writing its conclusion,
#: a silent false negative (#2722).
_REFETCHABLE_STUB_TEMPLATE = (
    "[Earlier {tool} result{target} ({chars} chars{digest}) cleared to keep the "
    "working context focused; the result was not empty, but its content is no "
    "longer shown and it is re-fetchable. Re-call the tool before stating "
    "anything about it.]"
)
_REFETCHABLE_STUB_RE = re.compile(
    re.escape(_REFETCHABLE_STUB_TEMPLATE)
    .replace(re.escape("{tool}"), r"[^\n]+?")
    .replace(re.escape("{target}"), r"(?: for [^\n\]]+?)?")
    .replace(re.escape("{chars}"), r"\d{1,3}(?:,\d{3})*")
    .replace(re.escape("{digest}"), r"(?:, [^\n\]]+?)?")
)

#: Ceiling on the digest a re-fetchable stub keeps of the body it replaced.
_TOOL_RESULT_DIGEST_MAX = 200


def _bounded_stub_text(text: str, limit: int) -> str:
    """*text* on one line, without ``]`` (the stub's terminator), at most *limit* chars."""
    flat = " ".join(text.split()).replace("]", ")")
    if len(flat) > limit:
        flat = flat[: limit - 3].rstrip() + "..."
    return flat


def _tool_result_digest(body: str) -> str:
    """`` , 2 items`` / `` , first line "..."`` for a cleared body, or ``""``.

    Read from the RESULT, never from the call's arguments (a body argument is
    never echoed, :data:`_TOOL_CALL_TARGET_KEYS`). A JSON list gives its item
    count; a JSON object gives the count of its first non-empty list (its
    first list when all are empty) and its ``total``
    when present; anything else gives its first non-blank line. Bounded by
    :data:`_TOOL_RESULT_DIGEST_MAX`, on one line and without ``]``, so the
    stub always matches :data:`_REFETCHABLE_STUB_RE`.
    """
    text = body.strip()
    digest = ""
    if text and text[0] in "[{":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            digest = f"{len(parsed)} item" + ("" if len(parsed) == 1 else "s")
        elif isinstance(parsed, dict):
            parts: list[str] = []
            lists = [(key, value) for key, value in parsed.items() if isinstance(value, list)]
            # The first NON-EMPTY list: `{"warnings": [], "results": [...]}` must
            # never read "warnings: 0 items" (#2725 review); all empty, the first.
            named = next((pair for pair in lists if pair[1]), lists[0] if lists else None)
            if named is not None:
                key, value = named
                parts.append(f"{key}: {len(value)} item" + ("" if len(value) == 1 else "s"))
            total = parsed.get("total")
            if isinstance(total, int) and not isinstance(total, bool):
                parts.append(f"total {total}")
            digest = ", ".join(parts)
    if not digest:
        first = next((line for line in text.splitlines() if line.strip()), "")
        line = _bounded_stub_text(first, _TOOL_RESULT_DIGEST_MAX - len('first line ""'))
        if line:
            digest = f'first line "{line}"'
    digest = _bounded_stub_text(digest, _TOOL_RESULT_DIGEST_MAX)
    return f", {digest}" if digest else ""


#: The arguments that ADDRESS what a call read (a file, a window of it, a
#: document, a page), in the order the stub names them. Generic names only:
#: a body argument (content, code, query text) is never echoed into a stub.
_TOOL_CALL_TARGET_KEYS = (
    "path",
    "file_id",
    "document_id",
    "url",
    "repository",
    "ref",
    "symbol",
    "section",
    "key",
    "offset_line",
    "max_lines",
    "offset",
    "max_chars",
    "page",
)
#: Ceiling on one value and on the whole target segment of a stub.
_TOOL_CALL_TARGET_VALUE_MAX = 120
_TOOL_CALL_TARGET_MAX = 240


def _tool_call_target(arguments: object) -> str:
    """`` for path=a.py, offset_line=100`` from a call's arguments, or ``""``.

    Only the scalar arguments named in :data:`_TOOL_CALL_TARGET_KEYS`, each
    on one line without ``]`` (the stub's terminator) and bounded, so the
    segment always matches :data:`_REFETCHABLE_STUB_RE`.
    """
    if not isinstance(arguments, dict):
        return ""
    parts: list[str] = []
    for key in _TOOL_CALL_TARGET_KEYS:
        value = arguments.get(key)
        if value is None or value == "" or isinstance(value, (dict, list, tuple, set)):
            continue
        parts.append(f"{key}={_bounded_stub_text(str(value), _TOOL_CALL_TARGET_VALUE_MAX)}")
    if not parts:
        return ""
    segment = ", ".join(parts)
    if len(segment) > _TOOL_CALL_TARGET_MAX:
        segment = segment[: _TOOL_CALL_TARGET_MAX - 3].rstrip() + "..."
    return f" for {segment}"


def _tool_call_arguments_by_result(messages: list[ChatMessage]) -> dict[int, object]:
    """Index of each tool result in *messages* -> the arguments of its call.

    Paired in TRANSCRIPT ORDER, never through a global ``tool_call_id`` map
    (``backend/app/tools/AGENTS.md``; :func:`collect_task_labels`): an id is
    only unique within the response that minted it, so a result consumes the
    oldest still-unanswered call carrying its id (FIFO per id). A result whose
    call was pruned finds nothing pending and gets no entry.
    """
    pending: dict[str, deque[object]] = {}
    found: dict[int, object] = {}
    for index, m in enumerate(messages):
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                call_id = getattr(tc, "id", None)
                if call_id:
                    pending.setdefault(str(call_id), deque()).append(getattr(tc, "arguments", None))
            continue
        if m.role != "tool":
            continue
        queue = pending.get(str(m.tool_call_id or ""))
        if queue:
            found[index] = queue.popleft()
    return found


def _is_refetchable_stub(content: str) -> bool:
    """True when a tool body IS the stale-clearing stub that invites a re-call.

    The complete stub and nothing else (a full match of the template, never a
    prefix or a phrase inside a longer body): a file-bearing result's stub
    points at ``read_file_content`` and never says re-fetchable (re-calling a
    download or capture tool makes a NEW file), a ``[Duplicate ...]`` stub says
    the same output is still in the conversation, and a
    ``[Pruned tool result ...]`` stub is the emergency prune's, which invites
    nothing. A re-call after any of those is a repeat the breaker still
    counts; a re-call after THIS one is the re-read the harness asked for.
    The shape alone is not proof the HARNESS wrote it - a tool can
    return this text - which is why the breaker also requires the body to
    have changed since the tool answered (``_LoopBreaker._answer_held``).
    """
    return _REFETCHABLE_STUB_RE.fullmatch(content) is not None


# ── Dropped-files notes (self-describing stubs) ──────────────────────────────
# Whenever curation drops ``msg.files`` from a tool result, the tool's own text
# still says what it said when the file WAS attached ("the image is attached
# to this result", "[[FILE:id]]"), so the model is told the pixels are there
# when they are not. Every drop site therefore appends ONE note, built here
# from the ``FileRef`` metadata the tool already emitted (file id, size —
# never an invented id; the name, MIME type and saved notes follow it in an
# untrusted block, :func:`_files_note_facts`), that says the file is no longer
# attached and that it is still stored. For a result the model has SEEN it
# says at which step, quotes the notes the model saved for the file and
# tells it to work from them; only for a result the model never saw
# does it name ``read_file_content(file_id=…)`` as the way to see it. It
# deliberately never says "re-fetchable" or "re-call the tool": re-calling a
# download / screenshot tool creates a NEW file and lands the model back in
# the same retention pass (trace 722375f1).
# The note's own line - one line; the untrusted block that leads it (the
# files' names, types and notes) is paired with it by
# :func:`_split_files_removed_note`, which only takes a line outside every
# quarantine block.
_FILES_REMOVED_NOTE_RE = re.compile(
    r"\[(?:Image|File)\(s\) (?:removed from the working context to save space"
    r"|not attached - vision budget exhausted for this turn)[^\]\n]*\]"
)
# The attachment claims the platform's own file-bearing tools write ("the
# image is attached to this result", "The PNG is attached as a native vision
# block - you can see the pixels", "The PNGs are attached to this result as
# native vision blocks - LOOK at them", the download tools' "The file has been
# attached for analysis" / "It is attached to this conversation for
# analysis"). When the pixels are dropped the WHOLE sentence carrying the
# claim is removed from the kept body (:func:`_strip_attachment_claims`), so
# the message states one thing — the note — instead of "the image was
# attached" right before "the image is no longer attached" (the model
# read the two sentences as a contradiction and re-read the file).
# Deterministic and narrow: only the platform's phrasings match, anything
# else is left alone — the note's own "no longer attached" included, since no
# copula precedes it.
_ATTACHMENT_CLAIM_RE = re.compile(
    r"\b(?:is|are|has been|have been) attached "
    r"(?:to this (?:result|conversation)|as (?:a )?native vision blocks?|for analysis)"
)
# Where the sentence that carries such a claim ends: a terminator followed by
# whitespace or the end of the text (so ``377.5 KB`` and ``diagram.png`` do
# not end a sentence), or a line break (a Markdown list never is one).
_CLAIM_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)|\n")
# ... and where it starts: right after the previous such terminator + space, or
# after a line break.
_CLAIM_SENTENCE_START_RE = re.compile(r"[.!?]\s+|\n")


def _format_file_size(size: int | None) -> str:
    """Compact human size for a dropped-files note (``377.5 KB``)."""
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


#: Ceiling on the agent notes quoted inside a dropped-files note, per file.
_FILES_NOTE_MAX_NOTES_CHARS = 400


def _note_safe(text: object) -> str:
    """One line, no ``]`` (the note's own terminator), bounded."""
    flat = " ".join(str(text or "").split()).replace("]", ")")
    if len(flat) > _FILES_NOTE_MAX_NOTES_CHARS:
        flat = flat[: _FILES_NOTE_MAX_NOTES_CHARS - 3].rstrip() + "..."
    return flat


def _files_note_entry(ref: Any) -> str:
    """``file_id <id> (<size>)``: how a dropped-files note names a file in its own text."""
    file_id = str(getattr(ref, "file_id", "") or "").replace("]", ")")
    size = _format_file_size(getattr(ref, "size", None))
    return f"file_id {file_id} ({size})" if file_id else size


#: The source label of the block :func:`_files_note_facts` writes: what tells
#: the note's own block from any other block that precedes a note.
_FILES_NOTE_SOURCE = "stored files"


def _files_note_facts(refs: list, *, with_notes: bool) -> str:
    """The name, MIME type and saved notes of each file of a note, in an untrusted block.

    A stored name is often a third party's text (a downloaded document's
    title, an attachment's name) and a saved note may quote what the model
    read in an image, so neither sits in the note's own line. The block is
    ``stable``: re-stubbing the same message gives the same text, and the
    provider's prompt cache still hits.
    """
    lines = []
    for ref in refs[:_FILES_NOTE_MAX_ENTRIES]:
        name = " ".join(str(getattr(ref, "filename", "") or "").split()) or "file"
        parts = [f"name {name}"]
        content_type = " ".join(str(getattr(ref, "content_type", "") or "").split())
        if content_type:
            parts.append(f"type {content_type}")
        if with_notes:
            notes = _note_safe(getattr(ref, "notes", None))
            parts.append(f"your notes: {notes}" if notes else "no notes saved")
        lines.append(f"file_id {getattr(ref, 'file_id', '') or '?'}: {', '.join(parts)}")
    return wrap_untrusted("\n".join(lines), source=_FILES_NOTE_SOURCE, stable=True)


def _files_removed_note(files: list, *, seen_at: int | None = None) -> str:
    """The note appended to a tool result whose ``files`` were dropped.

    Names every file by **file id** (the persisted platform id the tool
    returned — a ``FileRef.file_id``) and size; its name, MIME type and saved
    notes follow in an untrusted block (:func:`_files_note_facts`). What it says NEXT
    depends on whether the model has seen the pixels:

    * ``seen_at`` set — the model viewed this result at that step. The note
      says so, quotes the notes the model saved for each file
      (``FileRef.notes``, written with ``annotate_file``) or says none were
      saved, and tells the model to work from them. It never invites another
      ``read_file_content`` call: that invitation was the retry loop of the
      25 Sep 2026 run (the pixels were shown, pruned, and the stub said
      "call read_file_content to view it" - forty times).
    * ``seen_at`` unset — the pixels were never shown (only the emergency
      overflow prune can do that). ``read_file_content`` is then the one
      recovery path and the note points at it, once.

    The block, then one line with no ``]`` inside, so :data:`_FILES_REMOVED_NOTE_RE`
    can find and preserve both when a later pass re-stubs the same message; the
    line comes last, so a cut that keeps the end keeps the ids and the way back.
    """
    refs = [f for f in (files or []) if f is not None]
    if not refs:
        return ""
    label = (
        "Image(s)"
        if any(
            str(getattr(f, "content_type", "") or "").startswith("image/")
            for f in refs
        )
        else "File(s)"
    )
    entries = [_files_note_entry(ref) for ref in refs[:_FILES_NOTE_MAX_ENTRIES]]
    facts = _files_note_facts(refs, with_notes=seen_at is not None)
    more = len(refs) - len(entries)
    if more > 0:
        entries.append(f"and {more} more still-stored file(s)")
    plural = len(refs) != 1
    head = (
        f"{facts}\n"
        f"[{label} removed from the working context to save space - no longer "
        f"attached to this result: {'; '.join(entries)}. "
    )
    if seen_at is not None:
        return (
            head
            + f"Already viewed at step {seen_at}; the pixels are not re-sent this "
            f"turn. Work from your notes"
            + (
                ""
                if all(_note_safe(getattr(r, "notes", None)) for r in refs[:_FILES_NOTE_MAX_ENTRIES])
                else " - and save 1-3 lines with annotate_file right after viewing an image, so nothing is lost when it is pruned"
            )
            + ".]"
        )
    return (
        head
        + f"{'They are' if plural else 'It is'} stored but not in context; call "
        "read_file_content(file_id=...) with a file_id above to view "
        f"{'one' if plural else 'it'}. Do NOT call the download or screenshot "
        f"tool again for {'them' if plural else 'it'}.]"
    )


def _image_file_ids(msg: ChatMessage) -> list[str]:
    """The ids of the IMAGE files a tool result carries (a CSV is not a view)."""
    ids: list[str] = []
    for ref in msg.files or []:
        file_id = getattr(ref, "file_id", None)
        content_type = str(getattr(ref, "content_type", "") or "").lower()
        if file_id and content_type.startswith("image/"):
            ids.append(str(file_id))
    return ids


#: The steps one block of the vision budget is sized for. Documented as
#: ``AGENT_LOOP_VISION_VIEWS_PER_TURN`` images per 25 steps, independent of
#: the platform's default step budget.
VISION_STEPS_PER_BLOCK = 25


def vision_cap_for(step_budget: int) -> int:
    """The distinct images a turn may show with a step budget of *step_budget*.

    ``AGENT_LOOP_VISION_VIEWS_PER_TURN`` per :data:`VISION_STEPS_PER_BLOCK`
    steps: a run allowed more steps - an agent's own ``max_iterations``, a
    run its progress extended - may look at proportionally more
    images, so a long visual job (a video, a deck) does not run out of sight
    while it still has steps. ``0`` = no cap.
    """
    from app.core.config import settings

    per_block = max(0, int(settings.agent_loop_vision_views_per_turn))
    if not per_block:
        return 0
    return per_block * max(1, math.ceil(max(int(step_budget), 1) / VISION_STEPS_PER_BLOCK))


def _vision_budget_note(files: list, *, in_flight: int, cap: int) -> str:
    """The note appended to a tool result whose images the turn's vision budget refused.

    Written by the loop at the ONE boundary every tool result crosses, for a
    screenshot, a rendered figure, a generated image or a read alike: past
    ``AGENT_LOOP_VISION_VIEWS_PER_TURN`` distinct images the result keeps
    its text and loses its pixels, and this note says so - the files by id
    and size, then their names, types and saved notes in an untrusted block - and
    tells the model to work from those notes. It never invites another
    ``read_file_content``: the budget is spent for the rest of the turn, so
    a re-read would be refused the same way. The block, then one line with no
    ``]`` inside, matched by :data:`_FILES_REMOVED_NOTE_RE` so a later pass
    that re-stubs the message keeps both.
    """
    refs = [f for f in (files or []) if f is not None]
    if not refs:
        return ""
    entries = [_files_note_entry(ref) for ref in refs[:_FILES_NOTE_MAX_ENTRIES]]
    more = len(refs) - len(entries)
    if more > 0:
        entries.append(f"and {more} more still-stored file(s)")
    plural = len(refs) != 1
    return (
        f"{_files_note_facts(refs, with_notes=True)}\n"
        f"[Image(s) not attached - vision budget exhausted for this turn: {in_flight} "
        f"distinct images already shown (the cap is {cap}). Stored but not shown: "
        f"{'; '.join(entries)}. The pixels are not sent this turn; work from your "
        f"notes and from the text of this result - a re-read or a new capture of "
        f"{'them' if plural else 'it'} is refused the same way.]"
    )


def _strip_attachment_claims(text: str) -> str:
    """Remove every sentence of *text* that claims a file is attached.

    Applied to a tool body that KEEPS its text while its files are dropped.
    The platform's own file-bearing tools assert the attachment in the
    present tense — "the image is attached to this result", "The PNG is
    attached as a native vision block - you can see the pixels" — and a body
    that goes on saying so right before the note that says the pixels are
    gone states two contradictory things in one message. The sentence that
    carries the claim is cut out whole (see :data:`_ATTACHMENT_CLAIM_RE` and
    the sentence bounds next to it); the rest of the body — the ``[[FILE:id]]``
    marker, the Markdown image tag the reply must carry, page numbers,
    evidence ids — is untouched, and a body without such a claim is returned
    unchanged. Never a tense change: "was attached" next to "no longer
    attached" is still two statements about the same pixels.
    """
    out = text
    while True:
        claim = _ATTACHMENT_CLAIM_RE.search(out)
        if claim is None:
            return out
        starts = [m.end() for m in _CLAIM_SENTENCE_START_RE.finditer(out, 0, claim.start())]
        start = starts[-1] if starts else 0
        end_match = _CLAIM_SENTENCE_END_RE.search(out, claim.end())
        if end_match is None:
            end = len(out)
        elif out[end_match.start()] == "\n":
            end = end_match.start()  # the line break is kept
        else:
            end = end_match.end()
        head = out[:start]
        tail = out[end:]
        # Whitespace bookkeeping: the cut sentence took its separator with it
        # on one side; never leave two behind, or none.
        if not tail:
            head = head.rstrip()
        elif not head:
            tail = tail.lstrip()
        elif head[-1].isspace() and tail[0].isspace():
            tail = tail.lstrip(" ")
        elif not head[-1].isspace() and not tail[0].isspace():
            head += " "
        out = head + tail


def _split_files_removed_note(content: str) -> tuple[str, str | None]:
    """Split a tool body into ``(text, note)`` when it carries a dropped-files note.

    A later curation pass that replaces the body (stale clearing, pruning)
    must keep that note: it is the only place the file ids and the
    ``read_file_content`` recovery path survive once the tool's own text is
    gone. Returns ``(content, None)`` when no note is present.
    """
    content = content or ""
    spans = untrusted_block_spans(content)
    span_starts = [span_start for span_start, _ in spans]
    # The last note line OUTSIDE every quarantine block: a look-alike inside
    # one (a file name, a saved note, a fetched page) is data, and taking it
    # would carry its text out of the fence on the next re-stub.
    match = None
    pos = 0
    while (candidate := _FILES_REMOVED_NOTE_RE.search(content, pos)) is not None:
        index = bisect.bisect_right(span_starts, candidate.start()) - 1
        if index >= 0 and candidate.start() < spans[index][1]:
            pos = spans[index][1]
            continue
        match, pos = candidate, candidate.end()
    if match is None:
        return content, None
    start = match.start()
    # The note's own block ends right before the line - found by the linear
    # block reader, never by a lazy regex over the body, and only when it is
    # the platform's ``stored files`` block: any other block before a note
    # (a fetched page before an older note) stays in the body.
    # The source is read at the block's own source line, never searched for
    # in the block: a payload may write ``[source: stored files]``.
    index = bisect.bisect_right(span_starts, start) - 1
    if index >= 0:
        span_start, span_end = spans[index]
        if (
            span_end + 1 == start
            and content[span_end] == "\n"
            and untrusted_block_source(content, span_start) == _FILES_NOTE_SOURCE
        ):
            start = span_start
    return (content[:start] + content[match.end() :]).rstrip(), content[start : match.end()]


def _drop_tool_result_files(
    msg: ChatMessage, *, replace_content: str | None = None, note: str | None = None
) -> str:
    """Drop ``msg.files`` and make the message say so. Returns the note used.

    The single choke point for every site that removes vision attachments from
    a tool result. ``replace_content`` swaps the body for a curation stub at
    the same time (the note is appended to the stub); without it the tool's
    text is kept — minus every sentence claiming the attachment
    (:func:`_strip_attachment_claims`) — and the note is appended to it, so
    the message states ONE thing about the pixels: stored, not in context,
    ``read_file_content`` to view. A body that was nothing but the claim
    ("File: x.png (image/png, 377.5 KB) - the image is attached to this
    result.") becomes the note alone. A message that already carries a note
    (files dropped on an earlier pass) keeps that note rather than gaining a
    second one. ``note`` supplies the sentence for a drop that is not a
    retention prune - the vision budget's refusal at append time
    (:func:`_vision_budget_note`), which must not invite the re-read the
    default note names.
    """
    body, existing_note = _split_files_removed_note(
        replace_content if replace_content is not None else (msg.content or "")
    )
    if existing_note is None and replace_content is not None:
        _, existing_note = _split_files_removed_note(msg.content or "")
    dropping_now = bool(msg.files)
    note = (
        existing_note
        or note
        or _files_removed_note(msg.files or [], seen_at=getattr(msg, "seen_at_iteration", None))
    )
    msg.files = None
    if not note:
        msg.content = body if replace_content is not None else (msg.content or "")
        return ""
    if replace_content is None and dropping_now:
        body = _strip_attachment_claims(body)
    msg.content = f"{body}\n\n{note}" if body else note
    return note


# ── Model-consumed tool results ──────────────────────────────────────────────
# Every always-on curation pass, and the two ``_prune_session_context`` calls
# the loop makes before a request (the proactive compaction gate, the
# forced-answer backstop), takes an optional ``is_unseen`` predicate:
# ``True`` for a tool result the model has NOT been sent yet (the loop hands
# in :func:`_unseen_tool_result_predicate`). Such a result is never cleared,
# collapsed, pruned or stripped of its files, whatever the budgets say — its
# bytes are counted first instead, so what gives way is the OLDER material.
# Without the predicate (unit tests, callers outside the loop; the emergency
# prune's phase 1 passes only the file-recovery guard's pins) each pass keeps
# its structural floor: the single newest text result / the first
# file-bearing result of the latest group / the last
# ``_RECENT_TOOL_RESULTS_KEEP`` results.
#
# Why: the passes run at the top of an iteration, BEFORE the request that
# would carry the results the previous iteration appended. Bounding those by
# a budget spent within the batch stripped the second image of a
# ``[screenshot, read_file_content]`` batch — and the replayed image of a
# dedup-cache hit — before the model had taken a single step on them
# (agent "[Sam] General Assistant"): eight file-bearing results in a row and
# zero visible images. A result the model requested is therefore not curated
# until a successful provider request has carried it; the budgets bound how
# long it STAYS visible afterwards. Semantic compaction needs no predicate
# (its verbatim tail always includes the latest tool-call group). The one
# path deliberately outside this guarantee is the emergency overflow prune
# (context still over 95% of the window after everything above): there the
# alternative is a provider rejection, which shows the model nothing at all.
# The loop's predicate also answers ``True`` for a result the file-recovery
# guard PINNED (see ``_FileRecoveryGuard`` below) — and that pin is the one
# thing the emergency prune honours too, through its ``keep`` argument.


def _unseen_tool_result_predicate(
    consumed: dict[int, ChatMessage],
) -> UnseenPredicate:
    """Build the ``is_unseen`` predicate over a ``consumed`` identity map.

    ``consumed`` maps ``id(message)`` to the message object for every tool
    result a completed provider request has carried (the loop marks the whole
    transcript after each successful LLM call, and the caller's history at
    entry). Keeping the object itself — not just its id — is what makes the
    map safe: a message compaction dropped stays alive in it, so its id can
    never be reused by a new, unseen result that would then be curated early.
    """

    def _is_unseen(msg: ChatMessage) -> bool:
        return msg.role == "tool" and consumed.get(id(msg)) is not msg

    return _is_unseen


def _mark_tool_results_consumed(
    messages: list[ChatMessage],
    consumed: dict[int, ChatMessage],
    *,
    iteration: int | None = None,
) -> None:
    """Record every tool result of *messages* as sent to the model.

    A result is dated the first time it is marked (``ChatMessage.seen_at_iteration``):
    the step at which the model viewed it, which is what a later
    dropped-files note says instead of inviting another ``read_file_content``
    call. The step is the one the result was APPENDED at when the loop stamped
    it (``appended_at_iteration`` - the step its tool ran in, the number the
    tool's own answer uses), else *iteration* (1-based, the request that
    carried it). A result already marked keeps its first step; the caller's
    history (marked at entry, no iteration) stays undated.
    """
    for msg in messages:
        if msg.role == "tool":
            if id(msg) not in consumed and msg.seen_at_iteration is None:
                step = getattr(msg, "appended_at_iteration", None) or iteration
                if step is not None:
                    msg.seen_at_iteration = step
            consumed.setdefault(id(msg), msg)


# ── Turn-scoped protection of results not yet written about ─────────
# "Seen" is not "used". The byte-budget pass stubs a result the moment ONE
# completed request has carried it, so a turn that gathers its material in
# two or three tool batches - Notion page bodies, memory searches, Metabase
# results - loses the first batch as the second lands, before the model has
# written a word about any of it. The stub then says "re-call the tool", the
# re-call is served from the cache, and the same pass clears it again (the
# 25 Sep 2026 run). A tool result produced in the CURRENT turn is therefore
# kept out of the byte-budget pass and the compaction gate's prune until the
# model has WRITTEN since: a later assistant message carrying at least
# ``_SUBSTANTIVE_TEXT_MIN_CHARS`` of text - not another batch of tool calls,
# whose short preamble ("Let me check HiBob now") says nothing. This is a
# deterministic proxy, deliberately not a claim that the text USES that
# result: any substantive prose releases every result before it, which errs
# on the side of the ordinary budget. Results the caller's history brought in
# were used on earlier turns and stay under the ordinary budget; the
# window-utilization gate (semantic compaction, then the emergency prune) is
# what keeps a turn of tool-call-only batches inside the provider's window.
#: The loop's own line between a preamble and an answer
#: (``_PREAMBLE_HOLDBACK_CHARS``, defined with the streaming code below, pinned
#: equal by a tripwire): stage plans narrating a batch run 350-900 characters,
#: and a model that narrates each batch must not release the previous one.
_SUBSTANTIVE_TEXT_MIN_CHARS = 900


#: How much of the current turn's unwritten results the protection holds out
#: of the byte budget, as a multiple of the live tool-result budget: newest
#: first, the older ones past it fall back under the ordinary budget. Without
#: a ceiling a tool-call-only turn re-sent everything it gathered on every
#: request up to the compaction trigger (80% of the window - 1.8M characters
#: on a 1M-token model); four live budgets is half a 128k window and a fifth
#: of a 1M one. Never under four times the live budget's floor: a small
#: window is bounded by the window gate, not by this.
_TURN_PROTECTION_LIVE_BUDGET_FACTOR = 4
_TURN_PROTECTION_MIN_CHARS = _TURN_PROTECTION_LIVE_BUDGET_FACTOR * _TOOL_RESULT_LIVE_MIN_CHARS


#: Under a working window (#2430) the protection holds ONE live budget: an
#: event-driven listener works by tool calls and seldom writes 900
#: characters, so the protection is never released and its four live
#: budgets (192,000 characters at the floor) were re-sent on every call.
_WORKING_WINDOW_TURN_PROTECTION_FACTOR = 1


def _turn_protection_budget(context_window: int, working_window_tokens: int | None) -> int:
    """Characters of unwritten current-turn results the protection holds.

    On the model's window: ``max(_TURN_PROTECTION_MIN_CHARS, 4 x the live
    budget)``, as before. Under a working window smaller than the model's:
    one live budget of that window, so a tool-call-only loop carries at most
    about one live budget of tool results besides the batch it is shown.
    """
    window = _working_window(context_window, working_window_tokens)
    if window < context_window:
        return _WORKING_WINDOW_TURN_PROTECTION_FACTOR * _compute_live_tool_result_budget(window)
    return max(
        _TURN_PROTECTION_MIN_CHARS,
        _TURN_PROTECTION_LIVE_BUDGET_FACTOR * _compute_live_tool_result_budget(window),
    )


def _unwritten_turn_results(
    messages: list[ChatMessage],
    *,
    is_history: Callable[[ChatMessage], bool],
    budget_chars: int | None = None,
) -> set[int]:
    """``id()`` of the current-turn tool results no substantive assistant
    message follows - the results the model has not written a word since -
    newest first up to *budget_chars* (``None``: all of them)."""
    last_substantive = -1
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if (
            msg.role == "assistant"
            and len((msg.content or "").strip()) >= _SUBSTANTIVE_TEXT_MIN_CHARS
        ):
            last_substantive = index
            break
    candidates = [
        msg
        for msg in messages[last_substantive + 1 :]
        if msg.role == "tool" and not is_history(msg)
    ]
    protected: set[int] = set()
    used = 0
    for msg in reversed(candidates):
        size = message_payload_chars(msg)
        if budget_chars is not None and used + size > budget_chars:
            break
        used += size
        protected.add(id(msg))
    return protected


# ── File-recovery loop guard ─────────────────────────────────────────────────
# Every dropped-files note names ONE way back to the pixels:
# ``read_file_content(file_id=...)``. The consumed-tracking above is what
# keeps that result visible until the model has read it; this guard is the
# belt for the case where that fails — a result the loop appended WITH its
# image and a completed provider request then carried WITHOUT it. From the
# model's side that is a recovery path that does not recover, and its only
# move is to call again: the run of 24 Sept 2026 22:30 (agent "[Sam] General
# Assistant", gpt-6-astra) made 25 ``read_file_content`` calls over three
# file ids, varying ``max_chars`` to defeat the dedup cache, before two of
# them showed pixels — ~30 iterations on a task whose real work took 40.
#
# The guard counts, per ``file_id``, the ``read_file_content`` results that
# reached the model without their pixels. Once a file id has been answered
# that way ``_FILE_RECOVERY_FORCE_KEEP_AFTER`` times, the NEXT
# ``read_file_content`` result for it is **pinned**: the loop's protection
# predicate answers ``True`` for it whatever the consumed map says, so every
# always-on pass, the compaction gate's prune and the forced-answer prune
# leave it alone — and the emergency overflow prune, the one layer that may
# otherwise touch an unseen result, skips it too (``keep=``). A pinned
# message is released by the first completed request that carries it — with
# its files (the success the pin waited for) or without them (a counted
# failure: nothing is left to keep) — observed here, not inferred from the
# consumed map, so the belt never keeps stale pixels in context for the rest
# of the run, and a live pin always sits in the latest tool-call group (the
# one semantic compaction keeps verbatim, so that layer takes no ``keep``);
# at most one result per file id is pinned at a time. Every pixel-less answer and every
# pin is logged at WARNING with the message index and the budget state, and
# recorded on the iteration span, so the root cause is readable from the
# trace instead of from a count of repeated calls.
_FILE_RECOVERY_TOOL = "read_file_content"
_FILE_RECOVERY_FORCE_KEEP_AFTER = 2


class _FileRecoveryGuard:
    """Per-run watch over ``read_file_content`` as the recovery path.

    ``track()`` is called when the loop appends a ``read_file_content``
    result; ``observe()`` right after every completed provider request, on
    the transcript that request carried; ``is_pinned()`` is folded into the
    protection predicate every curation pass takes and handed to the
    emergency prune as ``keep``. Identity-keyed with strong references, like
    the consumed map: a message compaction dropped stays alive in here, so
    its ``id()`` can never be reused by a new result.
    """

    __slots__ = ("_failures", "_observed", "_pinned", "_pinned_by_file", "_tracked")

    def __init__(self) -> None:
        # id(msg) -> (msg, file_id, carried files when appended)
        self._tracked: dict[int, tuple[ChatMessage, str, bool]] = {}
        # id(msg) of tracked results a completed request has carried
        self._observed: set[int] = set()
        # file_id -> pixel-less answers so far
        self._failures: dict[str, int] = {}
        self._pinned: dict[int, ChatMessage] = {}
        self._pinned_by_file: dict[str, ChatMessage] = {}

    @staticmethod
    def file_id_of(tool_name: str, arguments: dict | None) -> str | None:
        """The ``file_id`` a ``read_file_content`` call names, else ``None``."""
        if tool_name != _FILE_RECOVERY_TOOL or not isinstance(arguments, dict):
            return None
        raw = arguments.get("file_id")
        if raw is None:
            return None
        file_id = str(raw).strip()
        return file_id or None

    def failures(self, file_id: str) -> int:
        return self._failures.get(file_id, 0)

    def should_pin(self, file_id: str) -> bool:
        """True once *file_id* has been answered without pixels enough times."""
        return self.failures(file_id) >= _FILE_RECOVERY_FORCE_KEEP_AFTER

    def track(self, msg: ChatMessage, file_id: str) -> None:
        """Remember a just-appended ``read_file_content`` result."""
        self._tracked[id(msg)] = (msg, file_id, bool(msg.files))

    def pin(self, msg: ChatMessage, file_id: str) -> None:
        """Force-keep *msg* through every pruning layer (one per file id)."""
        previous = self._pinned_by_file.get(file_id)
        if previous is not None and previous is not msg:
            self._pinned.pop(id(previous), None)
        self._pinned[id(msg)] = msg
        self._pinned_by_file[file_id] = msg

    def is_pinned(self, msg: ChatMessage) -> bool:
        return self._pinned.get(id(msg)) is msg

    @property
    def pinned_count(self) -> int:
        return len(self._pinned)

    def _unpin(self, msg: ChatMessage, file_id: str) -> None:
        if self._pinned_by_file.get(file_id) is msg:
            self._pinned.pop(id(msg), None)
            self._pinned_by_file.pop(file_id, None)

    def observe(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Read what a completed request carried for every tracked result.

        Called once per completed provider request, BEFORE the consumed map
        is updated — but independent of it: a tracked result is observed the
        first time it appears in the transcript a request carried. A result
        appended with files and carried without them is a pixel-less answer
        (counted per file id, returned for logging); one carried with its
        files is the success the pin waited for. Either way the observation
        releases the message's pin: a pin exists only for a result that
        still has its files AND that no completed request has carried yet,
        which is what keeps every live pin inside the latest tool-call group
        — the one group semantic compaction always keeps verbatim, so that
        layer needs no ``keep`` of its own. Returns one record per failure,
        with the message index the request saw.
        """
        failures: list[dict[str, Any]] = []
        for index, msg in enumerate(messages):
            key = id(msg)
            tracked = self._tracked.get(key)
            if tracked is None or tracked[0] is not msg or key in self._observed:
                continue
            self._observed.add(key)
            _msg, file_id, had_files = tracked
            if not had_files:
                continue
            self._unpin(msg, file_id)
            if msg.files:
                continue
            count = self._failures.get(file_id, 0) + 1
            self._failures[file_id] = count
            failures.append(
                {
                    "file_id": file_id,
                    "tool_call_id": msg.tool_call_id,
                    "message_index": index,
                    "failures": count,
                    "force_keep_after": _FILE_RECOVERY_FORCE_KEEP_AFTER,
                }
            )
        return failures


def _context_budget_state(
    messages: list[ChatMessage], context_window: int
) -> dict[str, Any]:
    """The retention budgets and what the transcript weighs against them.

    Logged next to every file-recovery guard event so a trace shows WHY a
    result could have been stripped: the vision byte budget and the image
    bytes still attached, the live text budget and the tool-result chars,
    the file-bearing groups against the retention window, and the
    transcript's size against the window.
    """
    window = (
        context_window
        if context_window and context_window > 0
        else _FALLBACK_CONTEXT_WINDOW
    )
    tool_msgs = [m for m in messages if m.role == "tool"]
    file_bearing = [m for m in tool_msgs if m.files]
    total_chars = _transcript_chars(messages)
    return {
        "context_window": window,
        "transcript_chars": total_chars,
        "estimated_tokens": total_chars // _CHARS_PER_TOKEN,
        "messages": len(messages),
        "tool_results": len(tool_msgs),
        "tool_result_chars": _transcript_chars(tool_msgs),
        "live_tool_result_budget_chars": _compute_live_tool_result_budget(window),
        "file_bearing_results": len(file_bearing),
        "file_bearing_groups": sum(
            1
            for group in _tool_result_groups(messages)
            if any(messages[i].files for i in group)
        ),
        "recent_file_groups_kept": _RECENT_TOOL_FILES_KEEP,
        "attached_vision_bytes": sum(
            _tool_result_vision_bytes(m.files) for m in file_bearing
        ),
        "vision_budget_bytes": _compute_tool_result_files_budget(window),
    }


def _collapse_redundant_tool_results(
    messages: list[ChatMessage], *, is_unseen: UnseenPredicate | None = None
) -> int:
    """Stub older EXACT duplicates of a large tool result, keeping the newest.

    Re-running the same expensive read (identical tool + identical body) leaves
    several verbatim copies in the transcript. This is the cheap, zero-risk
    half of tool-result curation: it removes pure redundancy regardless of the
    overall size pressure and never collapses two genuinely different results.
    The fingerprint pins the tool name and a SHA-256 of the FULL body, so two
    outputs are collapsed only when byte-for-byte identical — a (length, head,
    tail) fingerprint could otherwise collide on results that differ only in
    the middle and silently stub a non-duplicate. A copy the model has not
    been sent yet (``is_unseen``) is never the one stubbed. Returns the number
    of chars freed.
    """
    groups: dict[tuple, list[int]] = {}
    for i, m in enumerate(messages):
        if m.role != "tool":
            continue
        body = m.content or ""
        if len(body) < _STALE_TOOL_RESULT_MIN_CHARS or _is_curation_stub(body):
            continue
        fingerprint = (
            m.name or "",
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
        groups.setdefault(fingerprint, []).append(i)

    freed = 0
    for indices in groups.values():
        if len(indices) < 2:
            continue
        # Keep the most recent occurrence; stub the earlier identical ones.
        for idx in indices[:-1]:
            msg = messages[idx]
            if is_unseen is not None and is_unseen(msg):
                continue
            original_len = len(msg.content or "")
            stub = (
                f"[Duplicate {msg.name or 'tool'} result collapsed — identical "
                f"output ({original_len:,} chars) appears again later in the "
                "conversation; see the most recent copy.]"
            )
            # A stubbed copy loses its files too; the note names them and the
            # ``read_file_content`` path so the stub never claims an
            # attachment it no longer carries.
            _drop_tool_result_files(msg, replace_content=stub)
            freed += original_len - len(msg.content or "")
    if freed > 0:
        logger.info("Collapsed redundant tool results: freed %s chars", f"{freed:,}")
    return freed


def _clear_stale_tool_results(
    messages: list[ChatMessage],
    live_budget_chars: int,
    *,
    is_unseen: UnseenPredicate | None = None,
) -> int:
    """Keep the most-recent tool-result bytes; stub older, re-fetchable ones.

    The generic, always-on context-rot guard. Walking newest → oldest, tool
    results are kept verbatim while their cumulative size stays within
    ``live_budget_chars``; once the budget is crossed, every older result large
    enough to be worth clearing is replaced with a compact stub that records
    what was cleared and that it is re-fetchable (small or already-stubbed
    bodies are left in place). Each stub's bytes still count toward the live
    size, so ``live_budget_chars`` is actually enforced.

    Crucially this is **independent of duplication, tool inputs, and overall
    window utilization** — it fires whenever large results have piled up, which
    is the real failure mode (e.g. several different ``list_*`` calls returning
    tens of thousands of chars each, well under the context window). The single
    most-recent tool result is always kept verbatim so the model never loses
    the data it is actively working on — and so is every result the model has
    not been sent yet (``is_unseen``): a batch of five large reads is what the
    model asked for, and it is sent whole, once, whatever the budget; its bytes
    are counted first, so it is the OLDER results that make room. Returns the
    number of chars freed.

    The stub depends on what the result WAS. A text result is stale and
    re-fetchable, so the stub says so. A result whose value was an attached
    file or image (still attached, or already dropped by
    :func:`_clear_stale_tool_result_files` and carrying its note) is neither:
    re-calling a download or screenshot tool creates a NEW file, which is how
    trace 722375f1 turned one missing image into 84 identical downloads. That
    stub names the file ids and points at ``read_file_content`` instead, and
    never says "re-fetchable" or "re-call the tool".
    """
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_indices) <= 1:
        return 0

    kept_chars = 0
    freed = 0
    cleared = 0
    budget_exceeded = False
    call_arguments: dict[int, object] | None = None
    for position, idx in enumerate(reversed(tool_indices)):
        msg = messages[idx]
        body = msg.content or ""
        body_len = len(body)
        # Always keep the single most-recent tool result verbatim (the data the
        # model is most likely acting on right now), and everything the model
        # has not consumed yet.
        if position == 0 or (is_unseen is not None and is_unseen(msg)):
            kept_chars += body_len
            continue
        # Small or already-curated bodies are not worth replacing with a stub;
        # leave them as-is but still count them toward the live size.
        if body_len < _STALE_TOOL_RESULT_MIN_CHARS or _is_curation_stub(body):
            kept_chars += body_len
            continue
        # Keep large results verbatim only while the recent working set is still
        # within budget. Once the budget is crossed, every older large result is
        # cleared (recency cliff) — otherwise a slightly smaller older result
        # could slip back under the budget and survive, under-enforcing it.
        if not budget_exceeded and kept_chars + body_len <= live_budget_chars:
            kept_chars += body_len
            continue
        budget_exceeded = True
        _, files_note = _split_files_removed_note(body)
        if msg.files or files_note:
            # File-bearing result: the stub keeps the file identities and the
            # read_file_content path (``_drop_tool_result_files`` appends the
            # existing note, or builds one from the still-attached files).
            stub = (
                f"[Earlier {msg.name or 'tool'} result ({body_len:,} chars) "
                "cleared to keep the working context focused.]"
            )
            _drop_tool_result_files(msg, replace_content=stub)
        else:
            # The ONE stub that invites a re-call (``_is_refetchable_stub``):
            # the loop breaker forgives the re-read it invites. It names what
            # the call addressed, so the re-read is that window alone.
            # It keeps a digest of the body, so it never reads as empty (#2722).
            if call_arguments is None:
                call_arguments = _tool_call_arguments_by_result(messages)
            stub = _REFETCHABLE_STUB_TEMPLATE.format(
                tool=msg.name or "tool",
                target=_tool_call_target(call_arguments.get(idx)),
                chars=f"{body_len:,}",
                digest=_tool_result_digest(body),
            )
            if len(stub) >= body_len:
                # A stub as long as the body frees nothing: keep the body.
                kept_chars += body_len
                continue
            msg.content = stub
        freed += body_len - len(msg.content or "")
        # The stub still occupies space in the live context; count it so the
        # budget reflects the true post-clearing size.
        kept_chars += len(msg.content or "")
        cleared += 1

    if cleared:
        logger.info(
            "Stale tool-result clearing: freed %s chars (%d result(s) cleared, "
            "live budget %s chars)",
            f"{freed:,}",
            cleared,
            f"{live_budget_chars:,}",
        )
    return freed


def _tool_result_groups(messages: list[ChatMessage]) -> list[list[int]]:
    """Indices of ``tool`` messages, grouped by the tool-call turn they answer.

    The loop appends every result of one assistant ``tool_calls`` turn back to
    back (sequential and parallel paths alike; nudges, steering and the next
    assistant turn only ever follow the whole batch), so a maximal run of
    consecutive ``tool`` messages IS one tool-call group. Grouping by
    adjacency rather than through ``tool_call_id`` keeps this local — an id
    is only unique within the response that minted it (Gemini reuses
    ``call_{name}_{index}`` across turns).
    """
    groups: list[list[int]] = []
    current: list[int] = []
    for i, m in enumerate(messages):
        if m.role == "tool":
            current.append(i)
            continue
        if current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _tool_result_vision_bytes(files: list | None) -> int:
    """Bytes of image data a tool result forwards as native vision blocks.

    Only images count: a non-image attachment (an integrity sidecar, a PDF the
    provider cannot take natively) is forwarded as a short text snippet, not
    as pixels.
    """
    total = 0
    for ref in files or []:
        if str(getattr(ref, "content_type", "") or "").startswith("image/"):
            try:
                total += max(0, int(getattr(ref, "size", 0) or 0))
            except (TypeError, ValueError):
                continue
    return total


def _clear_stale_tool_result_files(
    messages: list[ChatMessage],
    *,
    files_budget_bytes: int | None = None,
    is_unseen: UnseenPredicate | None = None,
) -> int:
    """Drop vision attachments on older tool results — batch-aware, byte-bounded.

    Runner / browser screenshots and downloaded images come back as short
    Markdown plus ``ChatMessage.files``. Body-based pruning never touches them
    (they sit well under ``_STALE_TOOL_RESULT_MIN_CHARS``), but once those
    files are forwarded as native vision blocks they dominate the token
    budget, so older captures stay in stored evidence and stop being re-sent.

    Four rules, in this order:

    1. **A result the model has not been sent yet is never stripped here**
       (``is_unseen``, the loop's consumed-tracking). It is what the model
       just asked for, so this pass keeps its pixels whatever it weighs: its
       bytes are counted against the budget FIRST and only the older,
       already-seen material gives way. Before this rule the budget
       was spent within the newest batch too, and the second image of a
       ``[screenshot, read_file_content]`` batch — or the replayed image of a
       dedup-cache hit — was stubbed before the model had taken a single step
       on it ("[Sam] General Assistant": eight file-bearing results in
       a row, zero visible images).
    2. **The retention window is counted in tool-call GROUPS, not messages.**
       Files are kept on the last ``_RECENT_TOOL_FILES_KEEP`` file-bearing
       groups (see :func:`_tool_result_groups`). A parallel batch of N image
       downloads is ONE group — counting per message stripped the first N-2
       of such a batch before the model had ever seen them (trace 722375f1),
       and the model then re-downloaded them.
    3. **The most recent file-bearing group is never stripped by the window**,
       whatever ``_RECENT_TOOL_FILES_KEEP`` says.
    4. **The exception is bounded by size, not count.** The kept, already-seen
       results share what is left of ``files_budget_bytes`` of image data
       (:func:`_compute_tool_result_files_budget`), spent newest group first
       and in call order within a group: once the budget is crossed every
       later attachment is dropped (recency cliff, as in
       :func:`_clear_stale_tool_results`). Without an ``is_unseen`` predicate
       the first file-bearing result of the latest group is kept regardless
       (the structural floor), so the model can never be left with nothing to
       look at.

    Every drop goes through :func:`_drop_tool_result_files`, so a message that
    lost its pixels says so, names the file ids and points at
    ``read_file_content`` — never silently. Returns the number of tool
    messages whose files were dropped.
    """
    budget = (
        files_budget_bytes
        if files_budget_bytes is not None and files_budget_bytes > 0
        else _compute_tool_result_files_budget(_FALLBACK_CONTEXT_WINDOW)
    )
    file_groups = [
        group
        for group in _tool_result_groups(messages)
        if any(messages[i].files for i in group)
    ]
    if not file_groups:
        return 0

    def _unseen(msg: ChatMessage) -> bool:
        return is_unseen is not None and is_unseen(msg)

    # Rule 1: what the model has not consumed is kept and paid for first.
    used_bytes = sum(
        _tool_result_vision_bytes(messages[idx].files)
        for group in file_groups
        for idx in group
        if messages[idx].files and _unseen(messages[idx])
    )
    any_unseen = any(
        messages[idx].files and _unseen(messages[idx])
        for group in file_groups
        for idx in group
    )

    keep_groups = max(1, _RECENT_TOOL_FILES_KEEP)
    kept_groups = file_groups[-keep_groups:]
    older_groups = file_groups[:-keep_groups]

    dropped_by_window = 0
    for group in older_groups:
        for idx in group:
            if messages[idx].files and not _unseen(messages[idx]):
                _drop_tool_result_files(messages[idx])
                dropped_by_window += 1

    dropped_by_budget = 0
    budget_exceeded = False
    for group_position, group in enumerate(reversed(kept_groups)):
        # The structural floor: with no consumed-tracking, the first result of
        # the latest batch is what the model is acting on right now — kept
        # even when it alone exceeds the budget (mirrors the single-newest
        # rule of the text pass). With tracking, rule 1 already covers it.
        keep_first_regardless = group_position == 0 and not any_unseen
        for idx in group:
            msg = messages[idx]
            if not msg.files or _unseen(msg):
                continue
            cost = _tool_result_vision_bytes(msg.files)
            if keep_first_regardless:
                keep_first_regardless = False
                used_bytes += cost
                continue
            if not budget_exceeded and used_bytes + cost <= budget:
                used_bytes += cost
                continue
            budget_exceeded = True
            _drop_tool_result_files(msg)
            dropped_by_budget += 1

    dropped = dropped_by_window + dropped_by_budget
    if dropped:
        logger.info(
            "Dropped files from %d tool result(s) (%d outside the last %d "
            "file-bearing tool-call group(s), %d over the %s-byte vision budget; "
            "%s); each stub names its file ids and read_file_content",
            dropped,
            dropped_by_window,
            keep_groups,
            dropped_by_budget,
            f"{budget:,}",
            (
                "results the model has not seen yet were kept regardless"
                if any_unseen
                else "no unseen result in the transcript"
            ),
        )
    return dropped


def _prune_session_context(
    messages: list[ChatMessage],
    max_session_chars: int = int(
        _FALLBACK_CONTEXT_WINDOW * _SESSION_FRACTION * _CHARS_PER_TOKEN
    ),
    *,
    is_unseen: UnseenPredicate | None = None,
) -> None:
    """Compress older tool results in-place when the session exceeds the budget.

    Strategy (single pass over the message list):
      1. Measure what the transcript weighs (provider extras included,
         :func:`_transcript_chars`) and collect tool-role indices together.
      2. If under budget, do nothing.
      3. Replace oldest tool-result content with a compact summary, keeping
         the most recent N tool results intact — and every result the model
         has not been sent yet (``is_unseen``), whatever N says: the proactive
         compaction gate runs in the same iteration as the always-on curation,
         before the request, and a batch wider than N must reach the model
         whole once. The forced-answer backstop passes the predicate for the
         same reason — the iteration cap or the wall-clock guard can end the
         loop right after a batch was appended, and the synthesis request is
         what shows it. The emergency prune behind both stays the last resort
         that may still touch such a result (its phase 1 calls this function
         with only the file-recovery guard's pins as ``is_unseen``, on
         purpose) — the alternative there is a provider rejection that shows
         the model nothing at all.

    The function mutates *messages* in-place for efficiency.
    """
    total_chars = 0
    tool_indices: list[int] = []
    for i, m in enumerate(messages):
        total_chars += message_payload_chars(m)
        if m.role == "tool":
            tool_indices.append(i)

    if total_chars <= max_session_chars:
        return

    if len(tool_indices) <= _RECENT_TOOL_RESULTS_KEEP:
        return

    prunable = tool_indices[:-_RECENT_TOOL_RESULTS_KEEP]

    pruned_chars = 0
    pruned_count = 0
    for idx in prunable:
        msg = messages[idx]
        if is_unseen is not None and is_unseen(msg):
            continue
        original_len = len(msg.content)
        if original_len <= 200:
            continue
        summary = f"[Pruned tool result — originally {original_len:,} chars. Content omitted to stay within context budget.]"
        weight = message_payload_chars(msg)
        # The pruned body loses its files too; the note keeps the file ids and
        # the read_file_content path in the stub.
        _drop_tool_result_files(msg, replace_content=summary)
        pruned_chars += weight - message_payload_chars(msg)
        pruned_count += 1

        if total_chars - pruned_chars <= max_session_chars:
            break

    if pruned_chars > 0:
        logger.info(
            "Session context pruning: freed %s chars (%d tool results compressed)",
            f"{pruned_chars:,}",
            pruned_count,
        )


def _emergency_truncate_text(
    content: str, keep_chars: int, *, head_tail: bool = False
) -> str:
    """Shrink *content* toward ~*keep_chars*, never returning a LONGER string.

    Two modes:
      * ``tail`` (default) — keep the final ``keep_chars`` chars. Used for
        conversational turns, where the most recent text carries the signal.
      * ``head_tail`` — keep the first half + last half of the budget and elide
        the middle. Used for the system prompt, where the persona (head) and
        the trailing security policy (tail) must survive while a bulky middle
        (e.g. an injected memory block) is dropped.

    The truncation marker itself costs characters, so for a very small
    ``keep_chars`` the rendered stub could end up longer than the original; the
    loop shrinks the kept window until the result is no longer than the input,
    and returns the input **unchanged** when even the bare marker would not be
    shorter. This guarantees the function is always size-monotonic — callers
    can apply it unconditionally without risking context growth.
    """
    original_len = len(content)
    if original_len <= keep_chars:
        return content
    keep = max(keep_chars, 0)
    # A head+tail cut never splits a quarantine block: the head would keep its
    # opening marker without the closing one, and the tail, security policy
    # included, would then read as quarantined data.
    from app.services.content_safety import (
        head_tail_cuts_outside_blocks,
        refenced_block_tail,
        untrusted_block_spans,
    )

    spans = untrusted_block_spans(content)
    while True:
        if head_tail and keep >= 2:
            head_len = keep // 2
            tail_len = keep - head_len
            head_end, tail_start = head_tail_cuts_outside_blocks(
                spans, head_len, original_len - tail_len
            )
            elided = tail_start - head_end
            rendered = (
                f"{content[:head_end]}"
                f"\n[… {elided:,} chars elided to fit context budget …]\n"
                f"{content[tail_start:]}"
            )
        else:
            # A tail cut inside a quarantine block keeps the end of its data
            # fenced again (``refenced_block_tail``): the raw tail would hold
            # the closing marker without the opening one, its data read as
            # the platform's own text.
            start = original_len - keep
            kept = content[start:] if keep else ""
            for span_start, span_end in spans:
                if keep and span_start < start < span_end:
                    kept = (
                        refenced_block_tail(content[span_start:span_end], span_end - start)
                        + content[span_end:]
                    )
                    break
            rendered = (
                f"[Truncated — originally {original_len:,} chars. Showing last "
                f"{len(kept):,} chars.] …{kept}"
            )
        if len(rendered) <= original_len:
            return rendered
        if keep == 0:
            # Even the bare marker is longer than the original — leave it alone.
            return content
        overflow = len(rendered) - original_len
        keep = max(0, keep - max(overflow, 1))


def _emergency_prune_session_context(
    messages: list[ChatMessage],
    max_session_chars: int,
    *,
    keep: UnseenPredicate | None = None,
) -> None:
    """Aggressive, tiered context pruning for emergency overflow situations.

    Called when the regular ``_prune_session_context`` compaction fails to
    bring the session under budget (e.g. very few tool results, or a
    conversation dominated by long user/assistant turns).

    ``keep`` names the messages this prune must not touch — the results the
    file-recovery guard pinned (:class:`_FileRecoveryGuard`). This is the one
    layer that may otherwise shrink a result the model has not seen yet, so
    it is the one place the pin has to be spelled out: phase 1 skips them as
    the count-based prune skips unseen results, phase 2 never truncates one
    or drops its files, phase 3 never evicts a group holding one, phase 4
    never truncates one in place. Everything else is pruned as before —
    a pinned result is a short line plus an image, so the prune still makes
    its progress on the rest.

    The phases run cheapest-first and each is a no-op once the session is back
    under budget. Together they guarantee the prune is **size-monotonic and
    makes maximal progress** — it can always reach the target whenever the
    immovable system prompt fits within it, and otherwise shrinks the system
    prompt too as an absolute last resort. This is what stops the
    "nothing safe to drop → empty-response loop → forced-final-answer also
    fails" cascade.

    Tiered strategy
    ---------------
    Phase 1 — Tool result compression (same as normal prune).
      Replace oldest prunable tool-result bodies with a compact stub.

    Phase 2 — User / assistant / tool message truncation.
      If still over budget, shorten **every** user, assistant, **and tool**
      message longer than ``_EMERGENCY_ASSISTANT_TRUNCATE_KEEP`` chars to its
      tail so each one retains its most recent content while freeing the bulk
      of the history. Including ``user`` turns (not just ``assistant``) is
      essential for user-dominated histories; including ``tool`` turns
      is essential because Phase 1 keeps the most recent ``tool`` results
      intact, and when those recent results alone are huge only an
      explicit truncation here can shrink them. Tool truncation only edits the
      body — ``tool_call_id`` is preserved, so coupling is unaffected.

    Phase 3 — Oldest-message-group eviction.
      If still over budget, remove the oldest non-system message *groups*
      (assistant-with-``tool_calls`` + matching ``tool`` results stay
      together) from the front of the history, preserving the most recent
      ``_EMERGENCY_KEEP_MESSAGES`` non-system messages so the agent retains
      enough recent context to produce a useful (if degraded) response.

    Phase 4 — Dynamic floor (in-place truncation of the protected tail).
      Phase 3 will not drop below its retention floor, so when the most-recent
      ``_EMERGENCY_KEEP_MESSAGES`` messages **alone** exceed the budget it
      cannot help (a handful of large recent tool results).
      Instead of giving up, truncate the remaining non-system messages **in
      place** (oldest-first, never dropped — so the floor count and tool
      coupling survive) down to a per-message keep derived from the budget left
      after the system prompt. This makes the floor effectively dynamic.

    Phase 5 — System-prompt overflow (absolute last resort).
      Reached only when the system prompt **alone** still exceeds the budget
      after every conversational message has been truncated/evicted — typically
      a pathologically large injected agent-memory block (``agent_memory``
      ~34K chars). Oversized system messages are truncated
      head+tail so the persona (head) and the trailing security policy (tail)
      survive while the bulky middle is elided, trimming only as much as needed
      to fit. Has zero effect on healthy runs (system messages are otherwise
      never touched).

    All phases mutate *messages* in-place. System messages are never *dropped*
    (Phase 5 may shrink an oversized one); tool-call ↔ tool-result coupling is
    preserved throughout.

    Every running total is the transcript's weight, provider extras included
    (:func:`_transcript_chars`). The extras are never stripped or
    rewritten to save space: an evicted message takes them with it, and a
    message truncated in place is re-measured - a Gemini tool turn is replayed
    as its parts whatever its content says, so truncating it frees nothing.
    """
    def _kept(msg: ChatMessage) -> bool:
        return keep is not None and keep(msg)

    # ── Phase 1: tool result compression ─────────────────────────────
    # Deliberately WITHOUT the loop's unseen predicate (see the module note
    # on model-consumed tool results) — only the pinned results are spared.
    _prune_session_context(messages, max_session_chars=max_session_chars, is_unseen=keep)

    total_chars = _transcript_chars(messages)
    if total_chars <= max_session_chars:
        return

    # ── Phase 2: truncate long user / assistant / tool messages ───────
    # ``tool`` is included because Phase 1 keeps the most recent
    # _RECENT_TOOL_RESULTS_KEEP tool results intact — when those alone are huge
    # only an explicit truncation here can shrink them. Editing a tool
    # message's body never touches its ``tool_call_id``, so coupling is safe.
    truncated_messages = 0
    for msg in messages:
        if msg.role not in {"assistant", "user", "tool"} or _kept(msg):
            continue
        content = msg.content or ""
        if len(content) <= _EMERGENCY_ASSISTANT_TRUNCATE_KEEP:
            continue
        if msg.role == "tool" and msg.files:
            # A tool result that loses its files must say so (file ids +
            # read_file_content). The note is carved out of the keep window so
            # the phase stays size-monotonic; when even that cannot make the
            # message shorter, the text is truncated and the attachments are
            # left in place — the vision pass bounds them by bytes anyway.
            # The note the choke point will append: an existing one is kept
            # rather than doubled, so size it the same way here.
            body, existing_note = _split_files_removed_note(content)
            note = existing_note or _files_removed_note(msg.files)
            # ``keep_chars``, never ``keep``: that name is the pin predicate
            # this function takes, and ``_kept()`` reads it on every message.
            keep_chars = max(
                _EMERGENCY_MIN_MESSAGE_KEEP,
                _EMERGENCY_ASSISTANT_TRUNCATE_KEEP - len(note) - 2,
            )
            # The body is cut without its note, which is appended whole.
            truncated = _emergency_truncate_text(body, keep_chars)
            if len(truncated) + 2 + len(note) <= len(content):
                # Same choke point as every other drop site: the note is
                # appended to the truncated body and ``files`` is cleared
                # (never longer than the bound checked above).
                _drop_tool_result_files(msg, replace_content=truncated)
                truncated_messages += 1
                continue
        new_content = _emergency_truncate_text(
            content, _EMERGENCY_ASSISTANT_TRUNCATE_KEEP
        )
        if new_content != content:
            msg.content = new_content
            truncated_messages += 1

    total_chars = _transcript_chars(messages)
    if truncated_messages:
        logger.info(
            "Emergency prune phase 2: truncated %d user/assistant/tool "
            "message(s); new total ~%s chars",
            truncated_messages,
            f"{total_chars:,}",
        )
    if total_chars <= max_session_chars:
        return

    # ── Phase 3: evict oldest non-system message groups ───────────────
    # Build "groups" so we drop an assistant-with-tool_calls together with
    # every tool-result message that consumes one of its tool_call_ids.
    # Without this, dropping an assistant turn but keeping the tool result
    # that referenced it produces an orphan ``tool_call_id`` that Anthropic
    # ("each tool_result block must have a corresponding tool_use block in
    # the previous turn") and OpenAI's strict tool-calling mode both reject
    # with a 400 — which would turn an "emergency prune to recover" into a
    # "request keeps failing forever".
    #
    # We also retain at least _EMERGENCY_KEEP_MESSAGES of the most-recent
    # non-system messages so the agent still has enough context to produce
    # a coherent (if degraded) answer.
    groups = _build_evictable_message_groups(messages)

    # Count how many recent messages are protected by the minimum-retention
    # guard: pop groups off the tail until the protected suffix covers at
    # least _EMERGENCY_KEEP_MESSAGES non-system messages.
    protected_count = 0
    droppable_groups: list[list[int]] = list(groups)
    while droppable_groups and protected_count < _EMERGENCY_KEEP_MESSAGES:
        last = droppable_groups.pop()
        protected_count += len(last)

    if droppable_groups:
        to_remove: set[int] = set()
        for group in droppable_groups:
            if total_chars <= max_session_chars:
                break
            if any(_kept(messages[idx]) for idx in group):
                # A pinned result stays with the assistant turn that asked
                # for it (never an orphaned ``tool_call_id``).
                continue
            for idx in group:
                total_chars -= message_payload_chars(messages[idx])
                to_remove.add(idx)

        if to_remove:
            # Remove in reverse index order to keep remaining indices valid.
            for idx in sorted(to_remove, reverse=True):
                messages.pop(idx)
            logger.info(
                "Emergency prune phase 3: dropped %d oldest non-system "
                "message(s); new total ~%s chars",
                len(to_remove),
                f"{total_chars:,}",
            )

    total_chars = _transcript_chars(messages)
    if total_chars <= max_session_chars:
        return

    # ── Phase 4: dynamic floor — truncate the protected tail in place ──
    # Phase 3 deliberately keeps the most-recent _EMERGENCY_KEEP_MESSAGES
    # non-system messages so the agent keeps recent context. But when those
    # messages ALONE exceed the budget (e.g. a few large recent tool results),
    # dropping nothing leaves the loop re-sending an
    # oversized prompt forever. Truncate the remaining non-system messages IN
    # PLACE instead — never dropping them, so the floor count and tool coupling
    # survive — shrinking their bodies so the floor itself fits. Oldest-first
    # with an early exit means the most recent turns keep the most content.
    system_chars = _transcript_chars(m for m in messages if m.role == "system")
    nonsystem_msgs = [m for m in messages if m.role != "system"]
    if nonsystem_msgs:
        avail_for_nonsystem = max(max_session_chars - system_chars, 0)
        per_msg_keep = max(
            _EMERGENCY_MIN_MESSAGE_KEEP,
            avail_for_nonsystem // len(nonsystem_msgs),
        )
        truncated_inplace = 0
        for msg in nonsystem_msgs:
            if total_chars <= max_session_chars:
                break
            content = msg.content or ""
            if len(content) <= per_msg_keep or _kept(msg):
                continue
            new_content = _emergency_truncate_text(content, per_msg_keep)
            if new_content != content:
                weight = message_payload_chars(msg)
                msg.content = new_content
                total_chars -= weight - message_payload_chars(msg)
                truncated_inplace += 1
        if truncated_inplace:
            logger.warning(
                "Emergency prune phase 4: hard-truncated %d recent non-system "
                "message(s) in place (per-message keep ~%d chars) — new total "
                "~%s chars",
                truncated_inplace,
                per_msg_keep,
                f"{total_chars:,}",
            )

    total_chars = _transcript_chars(messages)
    if total_chars <= max_session_chars:
        return

    # ── Phase 5: system-prompt overflow (absolute last resort) ────────
    # Reaching here means the SYSTEM prompt alone still pushes the context over
    # budget after every conversational message has been truncated/evicted —
    # typically a pathologically large injected agent-memory block (agent_memory
    # ~34K chars). Truncate the largest oversized system messages
    # head+tail so the persona (head) and the trailing security policy (tail)
    # survive while the bulky middle is elided, trimming only as much as needed.
    nonsystem_chars = _transcript_chars(m for m in messages if m.role != "system")
    system_budget = max(
        max_session_chars - nonsystem_chars, _EMERGENCY_SYSTEM_MIN_KEEP
    )
    sys_indices = sorted(
        (i for i, m in enumerate(messages) if m.role == "system"),
        key=lambda i: len(messages[i].content or ""),
        reverse=True,
    )
    cur_system_chars = _transcript_chars(messages[i] for i in sys_indices)
    truncated_system = 0
    for i in sys_indices:
        if cur_system_chars <= system_budget:
            break
        content = messages[i].content or ""
        if len(content) <= _EMERGENCY_SYSTEM_TRUNCATE_MIN:
            continue
        weight = message_payload_chars(messages[i])
        # Budget this message may occupy = total system budget minus the chars
        # the OTHER system messages already consume, floored so persona +
        # policy always survive. ``sys_keep_chars``, never ``keep``: that
        # name is the pin predicate this function takes (see phase 2).
        sys_keep_chars = max(
            _EMERGENCY_SYSTEM_MIN_KEEP,
            system_budget - (cur_system_chars - weight),
        )
        new_content = _emergency_truncate_text(content, sys_keep_chars, head_tail=True)
        if new_content != content:
            messages[i].content = new_content
            cur_system_chars -= weight - message_payload_chars(messages[i])
            truncated_system += 1

    if truncated_system:
        total_chars = _transcript_chars(messages)
        logger.warning(
            "Emergency prune phase 5: head+tail-truncated %d oversized system "
            "message(s) (agent-memory overflow guard) — new total ~%s chars",
            truncated_system,
            f"{total_chars:,}",
        )


# ── Semantic in-loop compaction (auto-compact layer) ─────────────────────
# The provider-agnostic equivalent of Anthropic's server-side Compaction API
# and the "auto-compact" stage of Claude Code's compaction pipeline: distil
# the older turns into one high-fidelity LLM summary instead of deleting them.

_IN_LOOP_COMPACTION_SYSTEM_PROMPT = (
    "You are compacting the MIDDLE of an in-progress AI agent session to free "
    "up context while preserving everything needed to finish the task. You are "
    "given a transcript excerpt of older turns (user / assistant / tool "
    "messages). Produce a dense, factual summary that preserves:\n"
    "- The user's original request and any explicit constraints or "
    "corrections.\n"
    "- Concrete results from tool calls: data, IDs, names, file paths, URLs, "
    "error messages, and counts — keep identifiers VERBATIM. A tool result "
    "whose middle was elided comes with an 'Identifiers in the elided part' "
    "table: carry every row of it verbatim, never write that an id was not "
    "captured when the table holds it.\n"
    "- Decisions made and conclusions reached so far.\n"
    "- What still remains to be done.\n"
    "Do NOT invent information, do NOT add pleasantries, and do NOT write a "
    "final answer to the user. Output ONLY the summary."
)


def _format_tool_call_names(tool_calls: list[ToolCall]) -> str:
    """Render an assistant turn's tool-call names, bounded for the transcript.

    A single assistant turn can fan out into many tool calls, so the names are
    capped at ``_SEMANTIC_COMPACTION_MAX_TOOL_NAMES`` with a ``+N more`` suffix.
    This keeps the summariser prompt bounded while still conveying that the turn
    was tool-heavy (the exact long tail of names carries little signal).
    """
    names = [tc.name for tc in tool_calls]
    shown = names[:_SEMANTIC_COMPACTION_MAX_TOOL_NAMES]
    suffix = ""
    if len(names) > _SEMANTIC_COMPACTION_MAX_TOOL_NAMES:
        suffix = f", +{len(names) - _SEMANTIC_COMPACTION_MAX_TOOL_NAMES} more"
    return ", ".join(shown) + suffix


def _excerpt_for_transcript(text: str, cap: int) -> str:
    """Excerpt *text* to a head+tail window of ~*cap* chars.

    Tool outputs frequently carry the very identifiers the compaction prompt
    must keep verbatim — IDs, file paths, URLs, error tails, pagination cursors
    — at the **end**, so a prefix-only cut (``text[:cap]``) would silently drop
    them. Keeping both ends within the same budget gives far higher identifier
    recall for the summariser at no extra cost.
    """
    if len(text) <= cap:
        return text
    head_len = cap // 2
    tail_len = cap - head_len
    return (
        f"{text[:head_len]}\n…[{len(text) - cap} chars elided]…\n{text[-tail_len:]}"
    )


# UUIDs, ULIDs, URLs and the value of a JSON ``id`` / ``*_id`` key: the
# identifiers an agent writes back later (#2382).
_TRANSCRIPT_IDENTIFIER_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9A-HJKMNP-TV-Z]{26}\b"
    r"|https?://[^\s\"'<>()\[\]]+"
    r"|(?<=\"id\": \")[^\"\s]{1,128}(?=\")"
    r"|(?<=_id\": \")[^\"\s]{1,128}(?=\")"
)


def _elided_identifier_table(text: str, cap: int, budget: int) -> tuple[str, int]:
    """The identifiers of the part of *text* the excerpt elides, as a table.

    Each identifier is kept verbatim with the start of its line before it (up
    to ``_SEMANTIC_COMPACTION_ID_ENTRY_CONTEXT_CHARS`` chars: the ``name =``
    of a listing row), once, in order. The table is bounded by
    ``_SEMANTIC_COMPACTION_ID_TABLE_RESULT_CHARS`` and by *budget*, what is
    left of the per-compaction cap; what does not fit is counted. Returns the
    table (``""`` when nothing is elided or no identifier is found) and the
    characters it uses.
    """
    if len(text) <= cap or budget <= 0:
        return "", 0
    head_len = cap // 2
    tail_len = cap - head_len
    # Overlap the cut by an identifier's length on each side, so an id the
    # head or tail cut in two is still read whole.
    middle = text[max(0, head_len - 128) : len(text) - max(0, tail_len - 128)]
    header = "Identifiers in the elided part (verbatim):\n"
    # Room for the header and the "+N more" line: the table never passes its caps.
    limit = min(_SEMANTIC_COMPACTION_ID_TABLE_RESULT_CHARS, budget) - len(header) - 48
    rows: list[str] = []
    seen: set[str] = set()
    used = 0
    dropped = 0
    for match in _TRANSCRIPT_IDENTIFIER_RE.finditer(middle):
        ident = match.group(0)
        if ident in seen:
            continue
        seen.add(ident)
        line_start = middle.rfind("\n", 0, match.start()) + 1
        before = middle[
            max(line_start, match.start() - _SEMANTIC_COMPACTION_ID_ENTRY_CONTEXT_CHARS) : match.start()
        ].strip()
        row = f"- {before} {ident}" if before else f"- {ident}"
        if used + len(row) + 1 > limit:
            dropped += 1
            continue
        rows.append(row)
        used += len(row) + 1
    if not rows:
        return "", 0
    table = header + "\n".join(rows)
    if dropped:
        table += f"\n- +{dropped} more identifier(s) not listed"
    return table, len(table)


def _rerun_hint(tool_call: ToolCall | None, tool_name: str) -> str:
    """``re-run <tool>(<args>)`` for an elided tool result, arguments bounded."""
    args = ""
    if tool_call is not None and tool_call.arguments:
        try:
            args = json.dumps(tool_call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = str(tool_call.arguments)
        if len(args) > _SEMANTIC_COMPACTION_RERUN_ARGS_CHARS:
            args = args[:_SEMANTIC_COMPACTION_RERUN_ARGS_CHARS] + "…"
    return f"re-run {tool_name}({args}) to read the rest"


def _inloop_transcript_parts(
    messages: list[ChatMessage],
) -> tuple[str, list[str], int]:
    """The summariser transcript of *messages*, its re-run hints and id chars.

    Returns the transcript (see :func:`_build_inloop_transcript`), one
    ``re-run <tool>(<args>)`` hint per elided tool result (at most
    ``_SEMANTIC_COMPACTION_RERUN_HINTS``) and the characters of identifier
    tables the transcript carries, which sizes the summary budget.
    """
    cap = _SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS
    calls_by_id: dict[str, ToolCall] = {}
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                calls_by_id[tc.id] = tc
    parts: list[str] = []
    hints: list[str] = []
    id_budget = _SEMANTIC_COMPACTION_ID_TABLE_TOTAL_CHARS
    id_chars = 0
    for m in messages:
        content = (m.content or "").strip()
        if m.role == "assistant":
            label = "Assistant"
            if m.tool_calls:
                label = f"Assistant (called: {_format_tool_call_names(m.tool_calls)})"
            body = _excerpt_for_transcript(content, cap)
            parts.append(f"{label}: {body}".rstrip())
        elif m.role == "tool":
            tool_name = m.name or "tool"
            part = f"Tool ({tool_name}): {_excerpt_for_transcript(content, cap)}"
            if len(content) > cap:
                hint = _rerun_hint(calls_by_id.get(m.tool_call_id or ""), tool_name)
                part += f"\n[Body elided: {hint}]"
                if len(hints) < _SEMANTIC_COMPACTION_RERUN_HINTS:
                    hints.append(hint)
                table, used = _elided_identifier_table(content, cap, id_budget)
                if table:
                    part += "\n" + table
                    id_budget -= used
                    id_chars += used
            parts.append(part)
        elif m.role == "user":
            parts.append(f"User: {_excerpt_for_transcript(content, cap)}")
        # ``system`` messages in the middle are transient nudges — skip them.
    return "\n\n".join(p for p in parts if p.strip()), hints, id_chars


def _build_inloop_transcript(messages: list[ChatMessage]) -> str:
    """Build a compact, role-labelled transcript of *messages* for summarising.

    Each message's content is excerpted to a head+tail window of
    ``_SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS`` chars (via
    :func:`_excerpt_for_transcript`) so the summariser call stays cheap even
    when the slice contains very large tool results, while still surfacing the
    identifiers that tend to live at the tail. A tool result whose middle is
    elided also carries the identifiers of that middle as a bounded verbatim
    table and a ``re-run <tool>(<args>)`` hint (#2382). Transient in-loop
    ``system`` nudges are skipped — they carry no task content.
    """
    return _inloop_transcript_parts(messages)[0]


def _with_rerun_hints(summary: str, hints: list[str]) -> str:
    """*summary* followed by the re-run hints of the elided tool results.

    Added after the summariser answers, so the way back to an elided body
    never depends on the summariser keeping it (#2382).
    """
    if not summary or not hints:
        return summary
    lines = "\n".join(f"- {h}" for h in hints)
    return (
        f"{summary}\n\nTool results whose body was elided from this summary "
        f"(re-read them before writing an id they held; never declare such an "
        f"id wrong from memory):\n{lines}"
    )


class _SummaryRetryUnaffordableError(Exception):
    """The run cannot pay for asking a cut compaction summary again."""


async def _summarize_messages_for_context(
    llm: BaseLLMProvider,
    middle: list[ChatMessage],
    *,
    model: str | None,
    cancel_event: asyncio.Event | None = None,
    llm_timeout: float = _LLM_CALL_TIMEOUT,
    merge_usage: UsageHook | None = None,
    reason: str | None = None,
    can_retry: CompactionRetryCheck | None = None,
) -> str:
    """Return a high-fidelity LLM summary of *middle*, or ``""`` on failure.

    Provider-agnostic single ``llm.chat`` call (each provider already wraps the
    underlying SDK call in ``retry_llm_call``). Any failure / timeout returns
    ``""`` so the caller falls back to the deterministic pruner — semantic
    compaction is therefore never worse than the legacy behaviour.

    A user cancel (*cancel_event* fires mid-call) is treated like any other
    summarizer miss: the in-flight call is aborted and ``""`` is returned, so
    the caller falls back to the (CPU-only, fast) deterministic pruner and the
    next LLM call observes the cancel immediately.  Compaction is best-effort
    by contract — no cancellation exception escapes this helper.

    *llm_timeout* caps the summariser call; callers racing an outer
    wall-clock deadline (the forced-answer backstop) pass a
    remaining-budget-derived value so this call can never be the one the
    outer ``asyncio.wait_for`` cancels mid-flight.  A timeout here surfaces
    as the generic ``Exception`` miss below → ``""`` → deterministic prune.

    The call is recorded like the loop's own calls: a generation of the
    observation it runs in (the iteration span, or the run's own span in the
    forced-answer backstop), with its retries as attempt events, and its usage
    through *merge_usage* (the loop's ``_merge_usage``: the run budget and the
    usage the run reports) under the model its response names, once, when it
    returns, whether or not the summary is usable. A call that raises, times
    out or is cancelled returned no usage,
    a non-streamed call reporting it in its response only: its generation is
    closed with the error and nothing is recorded. *reason* is the compaction
    trigger, kept on the generation.

    A summary cut at ``_SEMANTIC_COMPACTION_MAX_TOKENS`` is asked once more
    with twice the room (``chat_retrying_a_cut``), inside the same
    *llm_timeout*; one still cut returns ``""``, never a partial account of the
    middle turns. The retry is a call like the first: its own generation,
    flagged ``cut_answer_retry``, and its usage merged when it returns. The
    summariser takes no budget reservation, so *can_retry* (the loop's run
    budget check) is asked first: when it refuses, the cut summary returns
    ``""`` as one still cut does.
    """
    transcript, rerun_hints, id_chars = _inloop_transcript_parts(middle)
    if not transcript.strip():
        return ""
    # A summary that must carry identifier tables gets the room for them.
    summary_max_tokens = _SEMANTIC_COMPACTION_ID_TABLE_MAX_TOKENS if id_chars else _SEMANTIC_COMPACTION_MAX_TOKENS

    from app.llm.base import ChatMessage as LLMMessage

    summary_messages = [
        LLMMessage(role="system", content=_IN_LOOP_COMPACTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content="Transcript to compact:\n\n" + transcript),
    ]
    _gen_metadata: dict[str, Any] = {
        "observation_kind": _COMPACTION_GENERATION,
        "summarised_messages": len(middle),
    }
    if reason:
        _gen_metadata["compaction_reason"] = reason
    # One *llm_timeout* for the call AND its retry of a cut answer: a caller
    # racing an outer deadline derived it from that deadline.
    _loop = asyncio.get_running_loop()
    _deadline = _loop.time() + llm_timeout
    _requests = 0

    async def _summarize(max_tokens: int) -> ChatResponse:
        # Each request is recorded as its own call, the retry of a cut answer
        # included: a generation of its own, its usage merged once.
        nonlocal _requests
        timeout = llm_timeout
        metadata = _gen_metadata
        if _requests:
            timeout = _deadline - _loop.time()
            if timeout <= 0:
                raise TimeoutError("semantic compaction time budget spent")
            if can_retry is not None and not can_retry(
                _estimate_prompt_tokens(summary_messages), max_tokens
            ):
                raise _SummaryRetryUnaffordableError
            metadata = {**_gen_metadata, "cut_answer_retry": True}
        _requests += 1
        _gen_ctx = _lf_start_direct_gen(
            model=model,
            messages=summary_messages,
            name=_COMPACTION_GENERATION,
            metadata=metadata,
        )
        _retry_tok = _lf_set_retry_observer(_lf_make_retry_observer(_gen_ctx))
        try:
            resp = await _race_cancellation(
                llm.chat(
                    messages=summary_messages,
                    tools=None,
                    model=model,
                    temperature=0.2,
                    max_tokens=max_tokens,
                ),
                cancel_event,
                timeout=timeout,
            )
        except (asyncio.CancelledError, Exception) as _exc:
            _lf_end_generation(_gen_ctx, error=_exc)
            raise
        finally:
            _lf_reset_retry_observer(_retry_tok)
        usable = bool((resp.content or "").strip()) and not answer_was_cut(resp)
        _record_request_trace_metadata(_gen_ctx, resp)
        _lf_end_generation(
            _gen_ctx,
            output=_build_generation_trace_output(resp),
            usage=resp.usage,
            model=resp.model or model,
            stop_reason=resp.stop_reason,
            level=None if usable else "WARNING",
        )
        if merge_usage is not None:
            # The provider delivered the answer: a failing usage hook must not
            # take it back from the compaction.
            try:
                merge_usage(resp.usage or {}, served_model=resp.model or model)
            except Exception:
                logger.exception("Could not record the usage of the compaction summariser call")
        return resp

    try:
        resp, cut = await chat_retrying_a_cut(
            llm,
            model,
            summary_max_tokens,
            _summarize,
            label="Semantic compaction summary",
        )
    except asyncio.CancelledError:
        raise
    except _CancelledMidCallError:
        logger.info(
            "Semantic compaction summary call aborted — request cancelled "
            "by user; falling back to deterministic prune"
        )
        return ""
    except _SummaryRetryUnaffordableError:
        logger.warning(
            "Semantic compaction summary cut at max_tokens and the run budget "
            "cannot pay for its retry - falling back to deterministic prune"
        )
        return ""
    except Exception:
        logger.warning(
            "Semantic compaction summary call failed — falling back to "
            "deterministic prune",
            exc_info=True,
        )
        return ""
    if cut:
        # A cut summary would replace the middle turns with a partial account.
        logger.warning(
            "Semantic compaction summary still cut at max_tokens - falling "
            "back to deterministic prune"
        )
        return ""
    return _with_rerun_hints((resp.content or "").strip(), rerun_hints)


async def _semantic_compact_session_context(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    *,
    model: str | None,
    keep_recent: int = _SEMANTIC_COMPACTION_KEEP_RECENT_MESSAGES,
    reason: str = "context pressure",
    cancel_event: asyncio.Event | None = None,
    llm_timeout: float = _LLM_CALL_TIMEOUT,
    on_event: OnEventCallback | None = None,
    merge_usage: UsageHook | None = None,
    can_retry: CompactionRetryCheck | None = None,
) -> bool:
    """Replace the OLDEST turns with one high-fidelity LLM summary, in place.

    Best-in-class, information-preserving compaction (the "auto-compact" stage
    of the Claude Code pipeline / Anthropic's Compaction API), made
    provider-agnostic.

    Invariants that keep it safe for long-prompt workflows and tool calling:

    * The original task (everything through the **first user turn**) and the
      most recent ``keep_recent`` messages are kept VERBATIM — only the middle
      is summarised, so a single legitimately-long prompt is never gutted.
    * Every ``system`` message is preserved in place.
    * The summary is injected as a ``user``-role context message (NOT
      ``system``): several providers merge every ``system`` message into the
      single system instruction, which would wrongly elevate this history
      recap to a top-priority directive and make it non-evictable
      (``_build_evictable_message_groups`` skips ``system``). As a ``user``
      turn it is treated as prior conversation, stays evictable by the
      deterministic fallback, and survives a *second* compaction (the transcript
      builder skips ``system`` nudges, so a ``system`` summary would be lost).
    * It operates on whole tool-call **groups** (an assistant ``tool_calls``
      turn stays with its ``tool`` results), so no ``tool_result`` is ever left
      orphaned — which Anthropic / OpenAI strict mode reject with a 400.
    * Requires the **first non-system group to be the user task** — because the
      head is kept verbatim, this is what guarantees the post-compaction
      conversation still starts with a user message (an Anthropic requirement).
      A history led by a non-user turn is left to the deterministic fallback
      rather than risking an invalid message sequence.

    Returns ``True`` when it compacted; ``False`` when there is nothing safe to
    summarise (the caller then falls back to the deterministic pruner).

    *merge_usage* is the loop's usage hook, handed to the summariser call
    (:func:`_summarize_messages_for_context`), which records its usage there,
    and *can_retry* the loop's check before a cut summary is asked again.
    """
    groups = _build_evictable_message_groups(messages)
    if len(groups) < 2:
        return False

    # The first non-system group must itself be the original user task. Keeping
    # the head verbatim only guarantees a user-led post-compaction sequence (an
    # Anthropic requirement) when that leading group is a user turn; if a
    # non-user turn leads the conversation (e.g. an assistant turn before any
    # user turn) compacting would leave it leading and emit an invalid sequence,
    # so bail and let the deterministic fallback handle it instead.
    if not any(messages[idx].role == "user" for idx in groups[0]):
        return False
    head_end = 0

    # Tail: most-recent groups kept verbatim until they cover >= keep_recent
    # non-system messages.
    tail_start = len(groups)
    covered = 0
    while tail_start - 1 > head_end and covered < keep_recent:
        tail_start -= 1
        covered += len(groups[tail_start])

    middle_groups = groups[head_end + 1 : tail_start]
    if not middle_groups:
        return False

    middle_indices = [idx for group in middle_groups for idx in group]
    middle_msgs = [messages[idx] for idx in middle_indices]
    # What the summary takes off the wire: the middle's weight, provider
    # extras included (a Gemini turn's thoughts go with it).
    middle_chars = _transcript_chars(middle_msgs)
    if middle_chars < _SEMANTIC_COMPACTION_MIN_DROP_CHARS:
        # Too little to gain — leave it to the cheaper deterministic prune.
        return False

    # Compaction liveness (P2.2): the summariser below is
    # a full LLM call that can take tens of seconds — emit the event BEFORE
    # it so the user sees a live "compacting" badge during the call instead
    # of a frozen status until it returns.
    await _emit(
        on_event,
        {
            "status": "context_compaction",
            "phase": "summarizing",
            "reason": reason,
        },
    )

    summary = await _summarize_messages_for_context(
        llm,
        middle_msgs,
        model=model,
        cancel_event=cancel_event,
        llm_timeout=llm_timeout,
        merge_usage=merge_usage,
        reason=reason,
        can_retry=can_retry,
    )
    if not summary:
        return False

    from app.llm.base import ChatMessage as LLMMessage

    # Injected as a USER-role context turn, not ``system``: provider message
    # builders fold every ``system`` message into the single system instruction
    # (anthropic_provider / gemini_provider), which would elevate this history
    # recap to a top-priority directive and — because eviction skips ``system``
    # — make it permanently un-prunable. A ``user`` turn is treated as prior
    # conversation context, stays evictable, and survives re-compaction.
    summary_msg = LLMMessage(
        role="user",
        content=(
            f"{_SEMANTIC_COMPACTION_MARKER} — {len(middle_indices)} earlier "
            "message(s) from this session were summarised to keep the context "
            "focused. The summary below is an accurate record of the prior "
            "conversation (the task, key results with identifiers, decisions, "
            "and pending work) — treat it as context, not a new instruction:"
            "\n\n" + summary
        ),
    )

    remove = set(middle_indices)
    before = _transcript_chars(messages)
    new_messages: list[ChatMessage] = []
    inserted = False
    for i, m in enumerate(messages):
        if i in remove:
            if not inserted:
                new_messages.append(summary_msg)
                inserted = True
            continue
        new_messages.append(m)
    messages[:] = new_messages
    after = _transcript_chars(messages)
    logger.info(
        "Semantic compaction (%s): summarised %d message(s); context %s → %s chars",
        reason,
        len(middle_indices),
        f"{before:,}",
        f"{after:,}",
    )
    return True


async def _compact_for_empty_recovery(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    *,
    model: str | None,
    max_session_chars: int,
    attempt: int = 0,
    phase: str = "next LLM call",
    cancel_event: asyncio.Event | None = None,
    llm_timeout: float = _LLM_CALL_TIMEOUT,
    on_event: OnEventCallback | None = None,
    keep: UnseenPredicate | None = None,
    merge_usage: UsageHook | None = None,
    can_retry: CompactionRetryCheck | None = None,
) -> int:
    """Shrink the session before an empty-response retry / forced-answer backstop.

    An empty turn on a large context is the model itself signalling it has lost
    focus on an oversized history. The recovery is **layered**, cheapest first:

    1. Below the large-context trigger the model's empty output is not a
       context-size problem, so we only run the cheap tool-result prune.
    2. Above it we first try **semantic compaction** — distilling the older
       turns into one high-fidelity summary while keeping the task and recent
       turns verbatim. This is information-preserving, so the retry re-sends a
       focused (not gutted) prompt.
    3. If semantic compaction can't help (no old turns to summarise, or the
       summariser call failed) we fall back to the deterministic
       :func:`_emergency_prune_session_context`, targeting a fraction of the
       *current* size that ratchets down with ``attempt`` so a persistently
       empty context shrinks quickly instead of plateauing.

    The system prompt is always preserved. Semantic compaction additionally
    keeps the original task and the most recent turns verbatim; the deterministic
    fallback keeps system messages and prioritises the most recent turns, but may
    truncate or evict older turns (**including the original task**) under extreme
    pressure so the loop can still make progress. Every size here is the
    transcript's weight, provider extras included (:func:`_transcript_chars`),
    and so is the post-compaction figure it returns.

    ``phase`` is a short human label for the recovery site (e.g.
    ``"empty-response retry"`` or ``"forced-answer backstop"``) surfaced in the
    logs so production traces show which path shrank the context.

    ``llm_timeout`` caps the semantic-compaction summariser LLM call.
    Deadline-racing callers (the forced-answer backstop) pass a
    remaining-budget-derived value; a timed-out summariser degrades to the
    deterministic pruner instead of being cancelled by the outer
    ``asyncio.wait_for``.

    ``keep`` is the file-recovery guard's pin predicate: the deterministic
    prunes below skip a pinned result (semantic compaction keeps the latest
    tool-call group verbatim anyway, which is where a pinned result sits).

    ``merge_usage`` is the loop's usage hook the summariser call records its
    usage through, and ``can_retry`` its check before a cut summary is asked
    again (see :func:`_summarize_messages_for_context`).
    """
    total = _transcript_chars(messages)
    if total < _EMPTY_RESPONSE_RECOVERY_TRIGGER_CHARS:
        _prune_session_context(
            messages, max_session_chars=max_session_chars, is_unseen=keep
        )
        return _transcript_chars(messages)

    # ── Preferred: information-preserving semantic compaction ─────────
    if await _semantic_compact_session_context(
        llm,
        messages,
        model=model,
        reason=f"empty-response {phase}",
        cancel_event=cancel_event,
        llm_timeout=llm_timeout,
        on_event=on_event,
        merge_usage=merge_usage,
        can_retry=can_retry,
    ):
        new_total = _transcript_chars(messages)
        if new_total < total:
            return new_total
        # Ran but didn't shrink (rare) — fall through to the brute pruner.
        total = new_total

    # ── Fallback: deterministic ratcheting emergency prune ────────────
    idx = min(max(attempt, 0), len(_EMPTY_RESPONSE_RECOVERY_TARGET_FRACTIONS) - 1)
    fraction = _EMPTY_RESPONSE_RECOVERY_TARGET_FRACTIONS[idx]
    target = max(_EMPTY_RESPONSE_RECOVERY_MIN_CHARS, int(total * fraction))
    target = min(target, max_session_chars)
    if target >= total:
        # Already at/under the would-be target — nothing aggressive to do.
        _prune_session_context(
            messages, max_session_chars=max_session_chars, is_unseen=keep
        )
        return _transcript_chars(messages)

    _emergency_prune_session_context(messages, max_session_chars=target, keep=keep)
    new_total = _transcript_chars(messages)
    if new_total < total:
        logger.warning(
            "Empty-response recovery (%s, attempt %d): emergency-compacted "
            "context from %s to %s chars (target %s)",
            phase,
            attempt,
            f"{total:,}",
            f"{new_total:,}",
            f"{target:,}",
        )
    return new_total


def _build_evictable_message_groups(
    messages: list[ChatMessage],
) -> list[list[int]]:
    """Group non-system messages into atomic eviction units.

    An assistant message that requested one or more tool calls (``tool_calls``
    is non-empty) is grouped with every following ``tool``-role message whose
    ``tool_call_id`` matches one of its requested ids, until we hit a message
    that breaks the pairing (a new assistant turn, a user turn, or a tool
    message that doesn't reference one of the in-flight ids). Other non-system
    messages — plain user turns and assistant turns without tool calls — are
    their own single-element group.

    System messages are not represented (they are never evicted). Tool messages
    whose preceding assistant is not in the message list (e.g. it was already
    dropped on a previous pass) are still represented as their own group so the
    caller can decide to drop them rather than leave a permanent orphan.
    """
    groups: list[list[int]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.role == "system":
            i += 1
            continue
        if msg.role == "assistant" and msg.tool_calls:
            expected_ids = {tc.id for tc in msg.tool_calls if tc.id}
            group = [i]
            j = i + 1
            while j < n and messages[j].role == "tool":
                tool_id = messages[j].tool_call_id
                if tool_id and tool_id in expected_ids:
                    group.append(j)
                    j += 1
                    continue
                break
            groups.append(group)
            i = j
            continue
        groups.append([i])
        i += 1
    return groups


# ── Tool-execution liveness heartbeat ────────────────────────────────
# Long-running tool executions (consult_agent runs a full nested agent
# loop, background-task collection, big MCP/integration calls, browser
# automation, ...) produce NO events between ``tool_start`` and
# ``tool_done`` — for multi-minute executions the user stares at a
# static status bubble with no liveness signal ("is it stuck or
# working?").  While a tool batch runs, the loop emits a periodic
# ``{"status": "tool_heartbeat", "tools": [...], "elapsed_s": N}`` so
# frontends can render a live elapsed-time indicator.  Frontends that
# don't know the status must keep their current label (the event
# carries no new semantic state).
_TOOL_HEARTBEAT_INTERVAL_S = 10.0


async def _await_with_heartbeat(
    awaitable: Any,
    *,
    on_event: OnEventCallback | None,
    tool_names: list[str],
) -> Any:
    """Await *awaitable* while emitting periodic ``tool_heartbeat`` events.

    Returns the awaitable's result and re-raises its exceptions
    unchanged.  On outer cancellation (user cancel / tool timeout via
    ``_race_cancellation``) the inner task is cancelled and awaited so
    no orphaned task leaks.  Without an ``on_event`` consumer the
    awaitable is awaited directly (zero overhead).
    """
    if on_event is None:
        return await awaitable
    inner = asyncio.ensure_future(awaitable)
    started = time.monotonic()
    try:
        while True:
            done, _pending = await asyncio.wait(
                {inner}, timeout=_TOOL_HEARTBEAT_INTERVAL_S
            )
            if done:
                return inner.result()
            await _emit(
                on_event,
                {
                    "status": "tool_heartbeat",
                    "tools": tool_names,
                    "elapsed_s": int(time.monotonic() - started),
                },
            )
    except BaseException:
        if not inner.done():
            inner.cancel()
            # Wait for the inner task to acknowledge cancellation so no
            # orphaned task leaks; ``return_exceptions=True`` also retrieves
            # its exception so asyncio never logs "exception was never
            # retrieved" for it.
            await asyncio.gather(inner, return_exceptions=True)
        raise


async def _emit(on_event: OnEventCallback | None, event: dict[str, Any]) -> None:
    """Fire the progress callback if provided, suppressing errors.

    Errors are logged at WARNING with the event status so frontend
    delivery issues are diagnosable (stream dropped mid-turn,
    serialisation failure, etc.). Without the status context these
    logs are very hard to triage.
    """
    if on_event is not None:
        try:
            await on_event(event)
        except Exception:
            logger.warning(
                "on_event callback failed (status=%s) — frontend may "
                "miss this progress signal",
                event.get("status"),
                exc_info=True,
            )


async def _retract_or_raise(on_event: OnEventCallback | None) -> None:
    """Take back an answer that already streamed, or raise.

    The ``on_stream_reset`` of a mid-answer failover must not use
    :func:`_emit`, which is a no-op without a callback and swallows a failed
    delivery: the failover wrapper would then stream the fallback's answer
    after the partial one. Raising makes it keep the partial answer and
    surface the provider's error instead.
    """
    if on_event is None:
        raise RuntimeError("no event channel to take the streamed answer back")
    await on_event({"status": "stream_retract"})


async def _stream_token(on_token: OnTokenCallback | None, chunk: str) -> None:
    """Forward a text chunk to the token callback, suppressing errors.

    Token streaming failures are particularly visible to users (the
    response appears to hang). Logged at WARNING so they're searchable
    in production without firing alerts (LLM completion still returns
    via the non-streaming path).
    """
    if on_token is not None:
        try:
            await on_token(chunk)
        except Exception:
            logger.warning(
                "on_token callback failed (chunk_len=%d) — user may "
                "see hung response until LLM completes",
                len(chunk or ""),
                exc_info=True,
            )


async def _emit_budget_warning(
    on_event: OnEventCallback | None, message: str
) -> None:
    """Emit a user-visible budget warning via ``thinking_text``.

    Reuses the existing ``thinking_text`` event channel so the frontend
    surfaces the warning in the same "reasoning" pane it already
    renders for LLM thinking tokens — no new event type for the
    frontend to handle, and the user sees the budget context inline
    with the model's reasoning instead of as a separate banner.

    Extracted into a shared helper because both the wind-down and the
    truncated-tool-call-retry sites emit the same shape; before this
    helper existed, the two sites carried near-identical inline event
    dicts that drifted in wording during reviews.
    """
    await _emit(on_event, {"status": "thinking_text", "content": message})


# Chunk size (chars) and delay (seconds) for simulated streaming when the
# full response is already available (e.g. after a tool-calling iteration).
# Sized so a typical 1-2KB tool-result follow-up renders in ~50–100 ms
# (one quick burst rather than artificially typed). The previous values
# (64 chars / 10 ms) added 150–300 ms of synthetic typing latency on top
# of every tool-call iteration without any user-visible benefit — the
# frontend's incremental renderer is already smooth at these sizes.
_SIMULATED_CHUNK_SIZE = 256
_SIMULATED_CHUNK_DELAY = 0.002

# Hold-back window (chars) for text produced by a stream-with-tools LLM call.
# While such a call is in flight we cannot know whether the text is a short
# pre-tool-call preamble ("Let me check X…") or the start of the final
# answer — that is only decidable when tool calls appear (or don't).
# Preambles are consistently short, so text is buffered up to this many
# characters: if tool calls follow, the held text belongs in the reasoning
# pane; once the buffer exceeds the window it is almost certainly the final
# answer and is flushed to the live answer stream (costing at most ~2-3 s of
# perceived latency on long answers).
#
# Sized to cover real multi-sentence stage plans ("I'll work through this in
# stages — correlate X, then map Y. Let me pull the live data first."): with
# the previous 350-char window those still flushed to the answer stream and
# were visibly retracted into the reasoning pane once tool calls followed
# (the "normal text, then flips below" glitch). 900 chars absorbs virtually
# every observed preamble while final answers almost always blow through it
# within the first sentence or two of structured output.
_PREAMBLE_HOLDBACK_CHARS = 900


async def _stream_text_chunked(on_token: OnTokenCallback, text: str) -> None:
    """Stream pre-generated text in small chunks for a smooth typing effect.

    Used when the full text is already available (from a non-streaming
    ``llm.chat()`` call) but we still want the frontend to render it
    progressively instead of as a single block.

    Skips the artificial typing delay entirely for short payloads
    (< _SIMULATED_CHUNK_SIZE) — a single tool-result acknowledgement
    has no business simulating typing. The delay paces one chunk after
    another, so nothing waits once the last chunk is out.
    """
    # Empty input must be a no-op. The pre-fix ``range(0, len(text), ...)``
    # loop emitted nothing for ``text == ""``; the new short-payload fast
    # path would otherwise call ``_stream_token`` with an empty chunk and
    # surface a spurious empty-token event to the frontend (e.g. when a
    # tool-result follow-up legitimately ends up empty).
    if not text:
        return
    if len(text) <= _SIMULATED_CHUNK_SIZE:
        await _stream_token(on_token, text)
        return
    for i in range(0, len(text), _SIMULATED_CHUNK_SIZE):
        if i:
            await asyncio.sleep(_SIMULATED_CHUNK_DELAY)
        await _stream_token(on_token, text[i : i + _SIMULATED_CHUNK_SIZE])


def _sum_usage_into(dst: dict, src: dict | None) -> None:
    """Fold *src* usage counters into *dst* in place.

    Numeric fields are SUMMED (both sides numeric); anything else is
    overwritten by the newer value. Used where one logical LLM call is
    served by several physical attempts (empty-response rescue, max_tokens
    continuations) and the trace must report the combined spend.
    """
    for key, value in (src or {}).items():
        if isinstance(value, (int, float)) and isinstance(
            dst.get(key), (int, float)
        ):
            dst[key] = dst[key] + value
        else:
            dst[key] = value


def _sum_usage_details_into(dst: dict, src: dict) -> None:
    """Sum the ``*_tokens_details`` breakdown maps of *src* into *dst* in place.

    A breakdown prices part of a count at another rate - the cached prompt
    tokens of ``prompt_tokens_details`` at the cache-read rate - so a
    run's usage without them prices its root trace above the sum of its calls.
    """
    for key, details in src.items():
        if not (isinstance(key, str) and key.endswith("_tokens_details")):
            continue
        if not isinstance(details, dict):
            continue
        merged = dst.get(key)
        if not isinstance(merged, dict):
            merged = dst[key] = {}
        for name, count in details.items():
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                merged[name] = merged.get(name, 0) + int(count)


def _usage_reports_tokens(usage: dict) -> bool:
    """Whether *usage* counts a token: a count or a breakdown entry above zero."""
    for key, value in usage.items():
        if isinstance(value, dict) and isinstance(key, str) and key.endswith("_tokens_details"):
            if any(isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0 for count in value.values()):
                return True
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


class _RunUsageParts:
    """A run's usage per usage convention and serving model.

    The run's usage sums its calls' usage key by key, and a reader takes the
    first key of each convention it finds: the sum of an Anthropic call
    (``input_tokens``) and an OpenAI call (``prompt_tokens``), which a failover
    or a budget downgrade puts in one run, reads the Anthropic call
    only. The same calls kept apart per convention and per model read back
    exactly and are priced at their own models. They ride on the summed usage
    (``USAGE_PARTS_KEY``) while the calls that reported tokens span several
    conventions and the flat reading loses tokens, or were served by more
    than one model, which one price row cannot price. A run one model
    served carries exactly the usage it always did.
    """

    def __init__(self) -> None:
        self._parts: dict[tuple[str | None, bool, str | None], dict[str, Any]] = {}
        self._serving_models: set[str | None] = set()

    def add(
        self,
        usage: dict,
        served_model: str | None,
        run_model: str | None,
        fallback_model: str | None = None,
    ) -> None:
        """Record one call's *usage* under the model that served it.

        *served_model* is the model the call's response names, the one its
        generation records. A usage handed over without its model (a call
        that never returned and that no failover fallback was serving) goes to
        *run_model*, the model the call was made with and its generation
        keeps, whichever model served its convention before (a fallback, or
        the model a budget downgrade moved away from). Counted like the run's
        sum: numbers and breakdown maps.

        The call counts toward the models serving the run under the model it
        was made with: *fallback_model* when a failover fallback served it,
        else *run_model*, which a budget downgrade replaces. Not the name its
        response carries: the calls of one model name it differently (a
        non-streamed OpenAI or Anthropic call names the API's dated model, a
        streamed one the model it was asked for).
        """
        nested = _lf_usage_parts(usage)
        if nested is not None:
            for nested_model, nested_usage in nested:
                self.add(nested_usage, nested_model or served_model, run_model, fallback_model)
            return
        if not any(
            isinstance(value, (int, float))
            or (isinstance(key, str) and key.endswith("_tokens_details") and isinstance(value, dict))
            for key, value in usage.items()
        ):
            return
        if _usage_reports_tokens(usage):
            self._serving_models.add(
                fallback_model if isinstance(fallback_model, str) and fallback_model else run_model
            )
        convention = _lf_usage_convention(usage)
        if not (isinstance(served_model, str) and served_model):
            served_model = run_model or None
        part = self._parts.setdefault((*convention, served_model), {"model": served_model, "usage": {}})
        counts = part["usage"]
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                counts[key] = counts.get(key, 0) + int(value)
        _sum_usage_details_into(counts, usage)

    def attach(self, summed: dict) -> None:
        """Put the parts on the run's *summed* usage, or keep them off it.

        On while the calls that reported tokens fill more than one part and
        either span more than one convention whose flat sum loses tokens the
        parts keep, or were served by more than one model: one model's price
        bills the other's tokens at the wrong rates, and a cache-read bucket
        in the sum drops the cached tokens a provider nests in its prompt
        count (a Gemini call next to a Bedrock one). Calls of one convention
        that one model served never split, whatever their names or their
        figures. A response that reported nothing but zeros (a compatible
        server's response without a usage object) is no second convention and
        no second model.
        """
        reporting = [key for key, part in self._parts.items() if _usage_reports_tokens(part["usage"])]
        conventions = {key[:2] for key in reporting}
        parts = list(self._parts.values())
        if len(reporting) > 1 and (
            len(self._serving_models) > 1
            or (len(conventions) > 1 and _lf_usage_parts_needed(summed, [part["usage"] for part in parts]))
        ):
            summed[_LF_USAGE_PARTS_KEY] = parts
        else:
            summed.pop(_LF_USAGE_PARTS_KEY, None)


def add_call_usage(
    run_usage: dict | None,
    usage: dict,
    *,
    run_model: str | None,
    served_model: str | None = None,
) -> dict:
    """*run_usage* plus one call made once the loop had returned.

    The run's caller may still call a model after ``run_agent_loop`` returned
    (the mandatory-transfer routing call of ``run_agent_chat``), and the run's
    root trace counts every call of the run. Returned is a new dict summed as
    ``_merge_usage`` sums a call, with the parts ``_RunUsageParts`` keeps: a
    call another model served or that reports in another convention is read
    and priced on its own. *run_model* is the model the run's usage is priced
    at, the one its trace records; *served_model* the one the call's response
    names, and a call without one goes to *run_model*. Neither input is
    changed.
    """
    base = dict(run_usage or {})
    parts = _RunUsageParts()
    nested = _lf_usage_parts(base)
    if nested is not None:
        for part_model, part_usage in nested:
            parts.add(part_usage, part_model, part_model or run_model)
    else:
        parts.add(base, run_model, run_model)
    call_model = served_model if isinstance(served_model, str) and served_model else run_model
    parts.add(usage, call_model, call_model)
    summed = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in base.items()
        if key != _LF_USAGE_PARTS_KEY
    }
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            summed[key] = summed.get(key, 0) + int(value)
    _sum_usage_details_into(summed, usage)
    parts.attach(summed)
    return summed


async def _continue_truncated_response(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    initial_response: ChatResponse,
    *,
    on_token: OnTokenCallback | None,
    on_event: OnEventCallback | None,
    model: str | None,
    temperature: float,
    max_tokens: int,
    max_session_chars: int,
    llm_extra: dict,
    merge_usage: UsageHook,
    new_on_thinking: Callable[[], Callable[[str], Awaitable[None]]] | None = None,
    parent_iter_ctx: dict | None = None,
    cancel_event: asyncio.Event | None = None,
    llm_timeout: float = _LLM_CALL_TIMEOUT,
    next_max_tokens: Callable[[list[ChatMessage]], int | None] | None = None,
    stop: Callable[[str], bool] | None = None,
    answer_cut: _AnswerStreamCut | None = None,
) -> ChatResponse:
    """Auto-continue when the LLM stopped due to max_tokens truncation.

    ``llm_timeout`` caps each continuation LLM call; deadline-racing
    callers (the agent loop when ``wall_clock_deadline`` is set, P1.1)
    pass a remaining-budget-derived value so a continuation issued near
    the budget fails cleanly inside the loop instead of being cancelled
    mid-flight by the caller's outer ``asyncio.wait_for``.

    ``next_max_tokens`` prices each continuation, given the transcript it
    would carry, before it is made: its ``max_tokens``, or ``None`` to stop
    continuing.  The agent loop passes it for a call the per-run budget
    sized down, whose continuations must fit what is left too.

    ``stop`` is asked, with the answer so far, after each continuation:
    ``True`` stops continuing.

    ``answer_cut`` is the :class:`_AnswerStreamCut` behind ``on_token``: a
    streamed continuation it cuts is aborted, its
    generation closed with the usage reported so far, and
    :class:`_AnswerCutError` re-raised for the caller to answer with the
    text shown.

    Appends the partial assistant text and a continuation prompt, then
    calls the LLM again (up to ``_MAX_CONTINUATIONS`` times) until it
    finishes naturally or the cap is reached.  Each continuation is
    streamed to the user in real-time so there is no jarring pause.

    When ``parent_iter_ctx`` is provided, each continuation opens a
    sibling generation under it named ``attempt:continuation:N`` for
    Langfuse visibility: the iteration span in the loop, the run's own
    span in the forced-answer backstop, which runs after the iteration
    spans closed.  Passing ``None`` silently disables tracing.

    When ``cancel_event`` is provided, a user cancel mid-continuation
    aborts the in-flight LLM call (streaming: between chunks;
    non-streaming: via :func:`_race_cancellation`) and re-raises
    :class:`_CancelledMidCallError` for the caller to convert into the
    canonical cancelled outcome.  ``None`` keeps legacy behaviour.
    """
    from app.llm.base import ChatMessage as LLMMessage

    response = initial_response
    for attempt in range(_MAX_CONTINUATIONS):
        if response.stop_reason != "max_tokens":
            break
        assistant_msg = LLMMessage(role="assistant", content=response.content or "")
        cont_user_msg = LLMMessage(role="user", content=_CONTINUATION_PROMPT)
        if next_max_tokens is not None:
            allowance = next_max_tokens([*messages, assistant_msg, cont_user_msg])
            if allowance is None:
                break
            max_tokens = allowance
        logger.info(
            "max_tokens continuation %d/%d — response truncated at %d chars, continuing",
            attempt + 1,
            _MAX_CONTINUATIONS,
            len(response.content or ""),
        )
        logger.info(
            "LLM call config (continuation): model=%s max_tokens=%d temperature=%.3f",
            model or "default",
            max_tokens,
            temperature,
        )
        prior_count = len(messages)
        messages.append(assistant_msg)
        messages.append(cont_user_msg)
        _prune_session_context(messages, max_session_chars=max_session_chars)

        is_last = attempt == _MAX_CONTINUATIONS - 1
        _cont_meta = {"cap_hit": True} if is_last else None
        _cont_ctx = _lf_start_continuation_gen(
            parent_iter_ctx,
            n=attempt + 1,
            model=model,
            delta_messages=[
                {"role": "assistant", "content": assistant_msg.content},
                {"role": "user", "content": cont_user_msg.content},
            ],
            prior_message_count=prior_count,
            metadata=_cont_meta,
        )
        _retry_tok = _lf_set_retry_observer(_lf_make_retry_observer(_cont_ctx))

        # Strip kwargs that must never reach a continuation call:
        #
        # - ``on_thinking``: we pass each continuation its own, made by
        #   ``new_on_thinking``, explicitly (so thinking text continues to
        #   fan out to the parent's ``on_event`` during truncation rescue,
        #   each call on a paragraph of its own), and Python would raise
        #   ``TypeError: got multiple values for keyword argument
        #   'on_thinking'`` the moment a caller populated
        #   ``llm_extra['on_thinking']`` too.
        # - ``structured_output``: native schema enforcement (OpenAI
        #   ``response_format``, Anthropic ``output_config.format``, Gemini
        #   ``response_schema``) grammar-constrains the continuation into a
        #   fresh, complete JSON document — it cannot resume the truncated
        #   text, so stitching the two would corrupt the payload.  The
        #   agent loop's own call sites already omit it (they pass
        #   ``_llm_extra``), but direct-chat callers (``agent_chat``)
        #   forward their full stream kwargs; filtering here keeps every
        #   caller safe.
        #
        # ``_llm_extra`` in the current agent loop only ever carries
        # ``reasoning_effort``, but this function is reachable from any
        # direct-chat streaming path that builds its own ``llm_extra``
        # shape, so the filter is a cheap defence against truncation rescue
        # misbehaving on a configuration that worked fine until the
        # response happened to hit ``max_tokens``.
        _extra_for_continuation = {
            k: v
            for k, v in llm_extra.items()
            if k not in ("on_thinking", "structured_output")
        }
        _cont_sink = StreamUsageSink()
        try:
            try:
                if on_token:
                    await _emit(on_event, {"status": "streaming"})
                    cont_resp = await _streaming_chat(
                        llm,
                        messages,
                        on_token,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        on_thinking=new_on_thinking() if new_on_thinking else None,
                        cancel_event=cancel_event,
                        timeout=llm_timeout,
                        usage_sink=_cont_sink,
                        trace_gen_ctx=_cont_ctx,
                        answer_cut=answer_cut,
                        **_extra_for_continuation,
                    )
                else:
                    cont_resp = await _race_cancellation(
                        llm.chat(
                            messages=messages,
                            tools=None,
                            model=model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            **_extra_for_continuation,
                        ),
                        cancel_event,
                        timeout=llm_timeout,
                    )
                merge_usage(cont_resp.usage, served_model=cont_resp.model or model)
            except TimeoutError as _to_exc:
                logger.error(
                    "max_tokens continuation %d timed out after %.0fs",
                    attempt + 1,
                    llm_timeout,
                )
                # Pass the exception object (not the string ``"timeout"``)
                # so the facade renders it as ``"TimeoutError: ..."`` —
                # matches the convention used by the streaming-LLM and
                # iteration-span sites in this file.
                _end_aborted_generation(_cont_ctx, _to_exc, _cont_sink, merge_usage)
                break
            except asyncio.CancelledError as _cont_exc:
                # Cancellation mid-continuation must record the
                # cancellation on the Langfuse generation BEFORE
                # propagating — otherwise the ``attempt:continuation:N``
                # generation stays open and the trace looks like a
                # clean success.  Pass the exception object directly
                # (not ``str(exc)``) because ``str(CancelledError())``
                # is empty; the facade's ``_close_observation`` already
                # renders the exception as ``f"{type(exc).__name__}: {exc}"``.
                # Re-raise so the asyncio scheduler actually sees the
                # cancellation — swallowing it would break structured
                # concurrency invariants the loop above relies on.
                _end_aborted_generation(_cont_ctx, _cont_exc, _cont_sink, merge_usage)
                raise
            except _CancelledMidCallError as _cont_exc:
                # User cancel mid-continuation — close the generation and
                # re-raise so the caller converts the abort into the
                # canonical cancelled outcome instead of treating it as an
                # ordinary continuation failure (which would surface the
                # partial response as if the request completed).
                _end_aborted_generation(_cont_ctx, _cont_exc, _cont_sink, merge_usage)
                raise
            except _AnswerCutError as _cont_exc:
                # The caller's stream cut stopped the continuation: close it
                # with what it consumed; the caller answers with what it showed.
                _end_aborted_generation(_cont_ctx, _cont_exc, _cont_sink, merge_usage)
                raise
            except Exception as _cont_exc:
                # Ordinary continuation failure: record the error on the
                # generation and break the continuation loop so the
                # caller can fall through to whatever the partial
                # response was.  Pass the exception object for the same
                # reason as the cancellation branch above.
                logger.exception("max_tokens continuation %d failed", attempt + 1)
                _end_aborted_generation(_cont_ctx, _cont_exc, _cont_sink, merge_usage)
                break

            _record_request_trace_metadata(_cont_ctx, cont_resp)
            _lf_end_generation(
                _cont_ctx,
                output=_build_generation_trace_output(
                    cont_resp,
                    captured_reasoning=None,
                ),
                usage=cont_resp.usage,
                model=cont_resp.model or model,
                stop_reason=cont_resp.stop_reason,
            )
        finally:
            _lf_reset_retry_observer(_retry_tok)

        initial_response.content = (initial_response.content or "") + (
            cont_resp.content or ""
        )
        response = cont_resp
        if stop is not None and stop(initial_response.content):
            break

    initial_response.stop_reason = response.stop_reason
    return initial_response


async def _streaming_chat(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    on_token: OnTokenCallback | None,
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    structured_output: dict | None = None,
    timeout: float = _LLM_CALL_TIMEOUT,
    cancel_event: asyncio.Event | None = None,
    idle_timeout: float | None = _STREAM_IDLE_TIMEOUT_SECS,
    on_stream_reset: Callable[[], Awaitable[None]] | None = None,
    usage_sink: StreamUsageSink | None = None,
    trace_gen_ctx: dict | None = None,
    answer_cut: _AnswerStreamCut | None = None,
) -> ChatResponse:
    """Call LLM with streaming, forwarding tokens via on_token callback.

    Returns a complete ChatResponse with the accumulated content.
    A per-call *timeout* prevents hung SSE connections from blocking
    the loop indefinitely.

    *usage_sink* is handed to the provider, which reports into it the usage
    it learns while streaming; it is settled when this returns (the usage is
    then on the response), so after any exception it holds what the aborted
    stream reported, for :func:`_end_aborted_generation`.

    *idle_timeout* is the idle-chunk watchdog (P1.2): when NO activity —
    neither a yielded text chunk nor an ``on_thinking`` reasoning delta —
    arrives for this many seconds, the stream is treated as stalled and a
    ``TimeoutError`` is raised so the existing per-call timeout handlers
    (retry / failover / terminal fallback) take over immediately instead
    of waiting out the 600 s SDK read timeout.  ``None`` disables the
    watchdog.

    When *cancel_event* is provided, it is checked between received chunks
    so a user cancel aborts the stream promptly (raising
    :class:`_CancelledMidCallError`) instead of waiting for the full response.
    ``None`` keeps the legacy behaviour unchanged.

    *structured_output* is forwarded to ``chat_stream()`` only when the
    provider declares ``supports_streaming_structured_output`` (OpenAI
    ``response_format``, Anthropic ``output_config.format``, Gemini
    ``response_schema``, Bedrock ``outputConfig``).  For other providers
    the kwarg is dropped here and schema enforcement relies on the
    prompt-based SAP instructions + post-response validation in
    ``app.services.structured_output``.

    *on_stream_reset* is handed to a provider that can restart an answer
    that already streamed (``supports_stream_reset``: the failover wrapper
    moving a failed call to a fallback): the text accumulated so far
    is dropped and the callback takes back what the caller showed. Without
    it such a failure raises instead, as before.

    *trace_gen_ctx* is the generation's trace context: its first text chunk
    or reasoning delta is noted on it (``mark_first_output``), which gives the
    span its time to first token. Telemetry only.

    *answer_cut* is the :class:`_AnswerStreamCut` behind *on_token*, bound to
    *usage_sink*: once it stops the call, the stream is closed and
    :class:`_AnswerCutError` raised, so a runaway stops generating.
    """
    from app.llm.base import ChatResponse, ThinkingActivitySink

    if max_tokens is None:
        max_tokens = _resolve_effective_max_tokens(llm, model)
    if max_tokens is None:
        raise RuntimeError(
            f"Unable to resolve max_tokens for provider={llm.provider_name} model={model or 'default'}."
        )

    async def _inner() -> ChatResponse:
        chunks: list[str] = []
        # Idle-chunk watchdog state (P1.2): ``last_activity`` is bumped by
        # every yielded text chunk AND every ``on_thinking`` reasoning
        # delta — reasoning models legitimately emit no text for minutes
        # while thinking, and that must never trip the watchdog.
        last_activity = time.monotonic()

        wrapped_on_thinking = on_thinking
        if idle_timeout is not None:
            if on_thinking is not None:
                _orig_on_thinking = on_thinking

                async def _thinking_with_activity(text: str) -> None:
                    nonlocal last_activity
                    last_activity = time.monotonic()
                    _lf_mark_first_output(trace_gen_ctx)
                    await _orig_on_thinking(text)

                wrapped_on_thinking = _thinking_with_activity
            else:
                # No caller-side thinking consumer — providers would drop
                # thinking deltas entirely, leaving the watchdog blind to a
                # reasoning model that legitimately produces no TEXT for
                # minutes.  Hand the provider an activity-only sink so
                # thinking deltas still count as liveness (the deltas are
                # otherwise discarded, exactly like the no-callback path).
                # Its type tells providers nothing reads the text, so they
                # never ask the model for reasoning on its behalf.
                def _mark_activity() -> None:
                    nonlocal last_activity
                    last_activity = time.monotonic()
                    _lf_mark_first_output(trace_gen_ctx)

                wrapped_on_thinking = ThinkingActivitySink(_mark_activity)

        extra_kwargs: dict = {}
        if reasoning_effort:
            extra_kwargs["reasoning_effort"] = reasoning_effort
        if wrapped_on_thinking:
            extra_kwargs["on_thinking"] = wrapped_on_thinking
        if structured_output and getattr(
            llm, "supports_streaming_structured_output", False
        ):
            extra_kwargs["structured_output"] = structured_output
        if on_stream_reset is not None and getattr(llm, "supports_stream_reset", False):
            _caller_reset = on_stream_reset

            async def _restart() -> None:
                await _caller_reset()
                chunks.clear()

            extra_kwargs["on_stream_reset"] = _restart
        if usage_sink is not None:
            extra_kwargs["usage_sink"] = usage_sink
        if answer_cut is not None:
            answer_cut.bind_call(usage_sink)
        logger.info(
            "LLM call config (stream): model=%s max_tokens=%d temperature=%.3f",
            model or "default",
            max_tokens,
            temperature,
        )
        stream = llm.chat_stream(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra_kwargs,
        )
        _next_task: asyncio.Task | None = None
        try:
            stream_iter = stream.__aiter__()
            while True:
                if idle_timeout is None:
                    try:
                        chunk = await stream_iter.__anext__()
                    except StopAsyncIteration:
                        break
                else:
                    # Idle watchdog: wait on the in-flight ``__anext__``
                    # with the REMAINING idle window as the timeout.  The
                    # wait deliberately does NOT cancel the task on
                    # timeout — a reasoning delta observed through the
                    # thinking callback re-arms the window and the loop
                    # keeps waiting on the SAME task (cancelling
                    # ``__anext__`` would kill the underlying generator).
                    # Only a genuinely idle stream is aborted.
                    if _next_task is None:
                        _next_task = asyncio.ensure_future(
                            stream_iter.__anext__()
                        )
                    remaining_idle = idle_timeout - (
                        time.monotonic() - last_activity
                    )
                    done, _pending = await asyncio.wait(
                        {_next_task}, timeout=max(remaining_idle, 0.05)
                    )
                    if not done:
                        if time.monotonic() - last_activity < idle_timeout:
                            # Thinking activity re-armed the window — keep
                            # waiting on the same in-flight chunk.
                            continue
                        _next_task.cancel()
                        await asyncio.gather(
                            _next_task, return_exceptions=True
                        )
                        _next_task = None
                        logger.warning(
                            "Streaming LLM call stalled: no chunk or "
                            "reasoning delta for %.0fs — aborting stream "
                            "(idle watchdog)",
                            idle_timeout,
                        )
                        raise TimeoutError(
                            f"LLM stream produced no data for {idle_timeout:.0f}s"
                        )
                    try:
                        chunk = _next_task.result()
                    except StopAsyncIteration:
                        _next_task = None
                        break
                    finally:
                        if _next_task is not None and _next_task.done():
                            _next_task = None
                # Between-chunks cancellation check: aborting here (instead
                # of after the full response) is what makes a user cancel
                # take effect mid-stream rather than after up to
                # ``_LLM_CALL_TIMEOUT`` seconds.  The ``finally`` below
                # closes the stream cleanly on the way out.
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(
                        "Streaming LLM call cancelled between chunks — aborting stream"
                    )
                    raise _CancelledMidCallError()
                if chunk:
                    last_activity = time.monotonic()
                    _lf_mark_first_output(trace_gen_ctx)
                    chunks.append(chunk)
                    await _stream_token(on_token, chunk)
                    # The chunk just shown turned the answer into a runaway:
                    # stop the stream now rather than drop the rest of it.
                    if answer_cut is not None and answer_cut.stopped.is_set():
                        logger.info(
                            "Streaming LLM call stopped by the answer stream cut "
                            "— aborting stream"
                        )
                        raise _AnswerCutError(answer_cut)
        finally:
            # Drain any still-pending ``__anext__`` task (cancellation /
            # outer timeout paths) so no "exception was never retrieved"
            # warning leaks and the generator can close cleanly below.
            if _next_task is not None and not _next_task.done():
                _next_task.cancel()
                await asyncio.gather(_next_task, return_exceptions=True)
            # Close the async generator explicitly so an early exit
            # (e.g. cancellation) releases the provider's streaming
            # connection immediately instead of at GC time.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        streamed = "".join(chunks)
        stop_reason = getattr(llm, "last_stop_reason", None) or "end_turn"
        # Text streaming generators can't return metadata, so providers stash
        # token usage on ``last_usage`` (companion to ``last_stop_reason``).
        # Forward it so the Langfuse generation records real token counts /
        # cost for streamed turns instead of zeroes.
        usage = getattr(llm, "last_usage", None) or {}
        provider_extra = getattr(llm, "last_provider_extra", None)
        trace_metadata = getattr(llm, "last_trace_metadata", None)
        # The failover wrapper names the fallback model that served the stream;
        # every other provider leaves the model the call asked for.
        served_model = getattr(llm, "last_model", None)
        if usage_sink is not None:
            usage_sink.settle()
        return ChatResponse(
            content=streamed,
            model=served_model if isinstance(served_model, str) and served_model else model or "unknown",
            stop_reason=stop_reason,
            usage=usage,
            _provider_extra=provider_extra if isinstance(provider_extra, dict) else None,
            trace_metadata=trace_metadata if isinstance(trace_metadata, dict) else None,
        )

    return await asyncio.wait_for(_inner(), timeout=timeout)


def _tools_not_in_cooldown(
    tools: list[ToolDefinition] | None,
    cooldown: dict[str, int],
    iteration: int,
) -> list[ToolDefinition] | None:
    """Drop tools still inside their cooldown window (rate-limit or auth failure).

    A tool is filtered out only while ``cooldown[name] > iteration``; once the
    recorded cooldown iteration is reached the tool is offered again. This is
    the single definition of the per-iteration cooldown filter, shared by the
    actual tool list sent to the LLM and the trace's ``available_tools`` so the
    two can never disagree. ``tools`` is returned unchanged when
    there is nothing to filter; an empty list is returned (not ``None``) when
    every tool is cooling — callers decide whether ``[]`` collapses to ``None``.
    """
    if not tools or not cooldown:
        return tools
    return [
        t
        for t in tools
        if t.name not in cooldown or cooldown[t.name] <= iteration
    ]


def _offering_the_run_tools(
    run: Callable[..., Awaitable[ChatResponse]],
) -> Callable[..., Awaitable[ChatResponse]]:
    """Bind a run's ``tools`` as the offered surface for the whole run.

    Around every exit of the loop in one place: a tool result or an inlined
    attachment names another tool only when ``offered_tools()`` carries it,
    and a nested loop run in its caller's task (a consult, a transfer) hands
    the caller's surface back when it returns. The run gets its tools with
    their surface-dependent descriptions resolved against that same list
    (``fit_descriptions_to_surface``), so what a tool says and what its
    results say agree.
    """
    signature = inspect.signature(run)

    @functools.wraps(run)
    async def _run(*args: Any, **kwargs: Any) -> ChatResponse:
        call = signature.bind_partial(*args, **kwargs)
        # What a description reads that only a query can say (the sandbox
        # workspace's manifest), read once before it is written.
        prepare = getattr(call.arguments.get("executor"), "prepare_for_run", None)
        if inspect.iscoroutinefunction(prepare):
            try:
                await prepare()
            except Exception as exc:
                logger.warning("Preparing the run's tools failed: %s", format_exception_message(exc))
        if "tools" in call.arguments:
            call.arguments["tools"] = fit_descriptions_to_surface(call.arguments["tools"])
        with offering_tools(call.arguments.get("tools")):
            return await run(*call.args, **call.kwargs)

    return _run


def _known_secrets_streamed(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Run the loop with its ``on_token`` / ``on_event`` passing model output through the known secrets.

    A secret variable value the model echoes never reaches a stream, even split
    across tokens (``core/redaction.redacted_stream_callbacks``); the text held
    back is released when the loop ends.
    """

    @functools.wraps(fn)
    async def streamed(*args: Any, **kwargs: Any) -> Any:
        on_token, on_event, release = redacted_stream_callbacks(kwargs.get("on_token"), kwargs.get("on_event"))
        if release is None:
            return await fn(*args, **kwargs)
        kwargs["on_token"], kwargs["on_event"] = on_token, on_event
        try:
            return await fn(*args, **kwargs)
        finally:
            with contextlib.suppress(Exception):
                await release()

    return streamed


@_known_secrets_streamed
@_offering_the_run_tools
async def run_agent_loop(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    tools: list[ToolDefinition] | None = None,
    executor: CompositeToolExecutor | None = None,
    *,
    max_iterations: int = 25,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tool_timeout_seconds: int | None = None,
    max_tool_result_chars: int | None = None,
    max_session_chars: int | None = None,
    max_total_tokens: int | None = None,
    on_event: OnEventCallback | None = None,
    on_token: OnTokenCallback | None = None,
    reasoning_effort: str | None = None,
    structured_output: dict | None = None,
    cancel_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str | SteeringMessage] | None = None,
    # Mid-turn approval channel. Given the pending proposals, returns
    # ``{tool_call_id: decision}``. Supplying it makes the loop WAIT for a human
    # rather than unwind — the right shape when one is already present (chat).
    # Left unset, the loop raises HumanInputRequestedError as before, which is
    # what assignment runs and plain REST rely on.
    request_approval: (
        Callable[[list[dict]], Awaitable[dict[str, dict]]] | None
    ) = None,
    # Called with the proposal and decision behind each ``approve_always``
    # verdict, so the caller can persist the standing approval the reviewer was
    # promised. The loop cannot do it itself — it holds no session and no user —
    # but it is the only place that has both the decision and the proposal it
    # belongs to, the proposal being what carries the schema the consent is
    # bound to and the decision who gave it.
    on_approve_always: Callable[[dict, dict], Awaitable[None]] | None = None,
    # The caller's gate-outcome sink. A paused call has no outcome yet — the
    # human's verdict *is* its outcome (FR-020) — and only this layer sees the
    # verdict, so without the sink every human-decided call is recorded as
    # having no outcome at all.
    approval_outcomes: dict[str, str] | None = None,
    session_vars: dict | None = None,
    exit_tool_names: frozenset[str] | set[str] | None = None,
    file_context: FileContext | None = None,
    progressive_tools: bool | None = None,
    preload_tool_groups: list[str] | None = None,
    finalize_on_tool: dict | None = None,
    finalize_output_schema: dict | None = None,
    input_reference: bool = False,
    wall_clock_deadline: float | None = None,
    wait_policy: WaitPolicy | None = None,
    wait_skip_event: asyncio.Event | None = None,
    wait_condition: WaitCondition | None = None,
    budget: RunBudget | None = None,
    on_approval_wait: Callable[[float], None] | None = None,
    on_iteration_activity: OnIterationActivityCallback | None = None,
    deliver_segments: bool = False,
    on_checkpoint: Callable[[list[ChatMessage]], None] | None = None,
    extend_iterations_to: int | None = None,
    # Evaluation only: each tool_done carries its tool's time.
    report_tool_durations: bool = False,
    context_sections: Mapping[str, int] | None = None,
    # The caller's own integration build and narrowing (#1874): a tool of the
    # turn that changes an integration the run is bound to refreshes the
    # surface through it at the end of that iteration. None: no refresh.
    tool_surface_refresher: IntegrationSurfaceRefresher | None = None,
    # The working window of an event-driven run (#2430): see the docstring.
    working_window_tokens: int | None = None,
) -> ChatResponse:
    """Run the agentic loop until the LLM stops calling tools.

    The *messages* list is mutated in-place: assistant messages (with their
    ``tool_calls``) and tool-result messages are appended as the loop
    progresses.  On return the list contains the **full conversation**, with
    one deliberate exception: when ``finalize_on_tool`` triggers, the loop
    returns **before** the terminating tool result is appended, so that final
    tool result is intentionally absent from *messages* (see the
    ``finalize_on_tool`` argument below).

    **Three-stage wind-down** (agentic AI best practice):

    * **Stage 1 — Nudge**: ~4 iterations before the cap, a system message
      encourages the LLM to stop calling tools and synthesise.
    * **Stage 2 — Tool stripping**: 2 iterations before the cap, all tools
      are removed (``tools=None``) so the LLM *must* produce text.  This
      is far more reliable than nudges alone.
    * **Stage 3 — Forced answer**: If the loop somehow still exhausts (edge
      case), a final tool-free LLM call is made with a strong prompt.  If
      the response is too short (< 100 chars), it retries once.

    Args:
        llm: The LLM provider to use for chat completions.
        messages: Conversation so far (system + user messages).  **Modified
            in-place** — on return contains the complete agentic transcript.
        tools: Available tool definitions the LLM may call.
        executor: Composite executor that routes tool calls.
        max_iterations: Safety limit to prevent infinite loops.
        model: Override the default model for this loop.
        temperature: LLM temperature.
        max_tokens: Max tokens per LLM call (``None`` = auto-derive from
            the provider's ``max_output_tokens`` attribute with per-model
            overrides from admin config).
        tool_timeout_seconds: Optional per-tool timeout override for
            non-agent tools. ``None`` (default) keeps the built-in
            ``_TOOL_EXECUTION_TIMEOUT``. There is no "disabled" sentinel:
            non-positive values are clamped to a 1s minimum (a passed ``0``
            becomes a 1s timeout, not "no timeout") — pass ``None`` to keep
            the default.
        max_tool_result_chars: Per-tool-result character cap (``None`` =
            auto-derive from the provider's context window).  Results
            exceeding this are truncated with a notice.
        max_session_chars: Total session character budget (``None`` =
            auto-derive from the provider's context window).  When the
            accumulated conversation exceeds this, older tool results are
            compressed to keep the context within bounds.
        max_total_tokens: Optional per-run token budget (``None`` or
            ``<= 0`` = disabled).  When the accumulated LLM usage across
            all iterations exceeds this, the loop forces the Stage-2
            wind-down (tools stripped + last-chance prompt) so the LLM
            produces a final answer instead of burning more budget.
        on_event: Optional async callback for streaming progress events to
            the caller (e.g. the chat's SSE stream).  Events are dicts with a ``status``
            key: ``thinking``, ``tool_start``, ``tool_done``, ``wind_down``,
            ``composing``.
        steering_queue: Optional queue of mid-run user messages ("steering").
            Drained at the start of every iteration; each text is appended
            to *messages* as a ``user`` message and a ``steering`` event is
            emitted, so a correction sent while the agent is still working
            is incorporated at the next loop boundary instead of requiring
            a full cancel + resend.  A :class:`SteeringQueue` admits what
            it held once more at that boundary.  ``None`` (default)
            disables steering (assignments, sub-agents, consults, external
            channels).
        exit_tool_names: Optional set of tool names that cause the loop to
            exit immediately after execution — without making another LLM
            call.  Used for ``transfer_to_agent`` so the transferring agent
            does not stream a redundant announcement message.
        progressive_tools: Enable progressive tool disclosure (the
            ``load_tools`` tool multiplexer) for this loop.  ``None`` (default)
            resolves to ``settings.progressive_tools_enabled``.  When active
            and the agent has enough gateable tools, only a compact catalogue
            + the ``load_tools`` meta-tool are sent up-front; integration / MCP
            / custom tool schemas are revealed on demand.  This keeps simple
            turns (e.g. "hello") cheap.  See ``app/tools/tool_gating.py``.
        preload_tool_groups: Tool group ids (or provider names) revealed
            before the first request, so the tool list, the head of the
            provider's cached prefix, is stable for the whole run (an
            assignment's ``trigger_config.preload_tool_groups``).  Resolved
            over the gate's own groups, never widening the surface.
        finalize_on_tool: Optional terminal-tool fast-path config (runtime
            control metadata, not model-facing prompt text).  When a
            configured trusted tool (``{"tool_name", "valid_key",
            "output_key", "validate_output_schema"?}``) returns a successful
            JSON result exposing a dict output, the loop returns that output
            directly and skips the otherwise-redundant final LLM call.  The
            returned ``ChatResponse`` carries ``stop_reason="finalize_tool"``
            and the terminating tool result is **not** appended to *messages*
            (see above).  ``None`` (default) disables the fast path.
        finalize_output_schema: Optional JSON Schema (raw or provider-native)
            used to validate the terminal tool's output before it ends the
            loop — only consulted when ``finalize_on_tool`` enables
            ``validate_output_schema``.  A schema failure falls back to the
            normal loop so the model can see the validator feedback instead of
            receiving an invalid final response.
        input_reference: Expose the JSON document of the newest user message
            as the ``$input`` session reference (``app.tools.session_input``),
            resolvable in tool arguments with the ``$collected`` path syntax
            (a quoted placeholder reads as the placeholder). An agent opts in
            with ``config.output_mode = "patch"`` or
            ``config.input_reference = true`` (an agent finalising on
            ``stix_bundle_validator`` by default); a chat message carrying a
            STIX bundle exposes it unless ``input_reference`` is false. A
            reference to ``$input`` when the input carries no JSON document errors the call; without
            the opt-in, a ``$input`` placeholder errors the call with the
            opt-in named instead of reaching schema validation.
        wall_clock_deadline: Optional ``time.monotonic()`` value representing
            the wall-clock deadline imposed by the outer caller (e.g. the
            assignment engine's ``asyncio.wait_for`` timeout).  When set, the
            forced-final-answer backstop uses the remaining budget instead of
            the full ``_LLM_CALL_TIMEOUT`` (900 s) for its LLM calls.  If
            fewer than ``_FORCED_ANSWER_MIN_REMAINING_SECS`` remain at the
            time the backstop fires, a pre-built fallback is returned
            immediately so the outer deadline can never cancel a partial
            synthesis mid-flight and leave the run record empty.
            Tool calls are capped to the remaining budget too
            (``_tool_timeout_for``).
        wait_policy: Resolved platform bounds for the ``wait`` primitive — the
            longest single pause and the total a run may spend waiting.
            ``None`` uses the platform defaults; the tool is always offered
            (when the run has tools at all) because an agent that cannot wait
            falls back to tight polling, which is what this replaces.  Waiting
            does not consume ``max_iterations``: a round that only waited is
            refunded, bounded by these caps and ``wall_clock_deadline``.
        wait_skip_event: Set by the user to end the *current* pause early
            without cancelling the run ("continue now").  Cleared after each
            wait so skipping one pause never auto-skips the next.  Cancelling
            the whole run needs nothing extra — ``cancel_event`` already races
            the pause.
        wait_condition: Resolves a ``wait(until=...)`` condition to
            ``(all_terminal, {task_id: status})`` so a pause ends the moment the
            delegated work finishes.  Supplied by call sites that have a user
            context (see ``builtin_tools.wait.make_task_condition``); ``None``
            degrades ``until`` to a plain timer and says so in the tool result.
        budget: Optional per-run spending budget unifying cost
            (USD), tokens, tool calls and duration into ONE object enforced
            with the reserve -> call -> debit -> block pattern: before each
            LLM call the loop reserves that call's worst-case spend
            (estimated prompt tokens + the full ``max_tokens`` output
            allowance, priced from the model's own rates); a call the budget
            cannot cover in full is made with the largest ``max_tokens`` it
            covers, down to ``MIN_OUTPUT_TOKENS``, and **refused** only below
            that floor.  A refusal ends
            the tool-calling phase and routes to the Stage-3 backstop, which
            spends the held-back closing-synthesis reserve
            (``final_answer_reserve_fraction``) so an exhausted budget still
            produces a real answer; when even that reserve is gone a
            deterministic tool-result digest is returned without any further
            LLM call.  Enforcement is entirely in the loop — nothing here is
            provider-specific — and the budget's duration limit is folded
            into ``wall_clock_deadline`` (tighter bound wins) so duration
            keeps its existing enforcement path.  Distinct from the legacy
            ``max_total_tokens`` argument, which keeps its softer wind-down
            semantics for callers that pass it directly; when both
            are given they are enforced independently.  ``None`` (default)
            disables budget enforcement entirely.

            When the budget carries a ``BudgetFallbackModel``
            (opt-in via ``AGENT_LOOP_BUDGET_FALLBACK_ENABLED``), a **cost**
            refusal takes a third path instead of stopping: the run is
            downgraded to that cheaper model — once per run, never a ladder —
            and continues.  The loop then re-derives everything it had pinned
            to the primary (``model``, the provider, ``max_tokens`` and, when
            it derived them itself, the context budgets) and the budget
            re-prices the rest of the run at the fallback's rates.  Only cost
            can degrade: tokens, tool calls and duration are model-independent.
            See ``_plan_model_downgrade`` for the three cases where the switch
            is refused.
        deliver_segments: Opt-in mid-turn segment delivery.
            When a live-streamed answer overflowed the hold-back window (so
            the user already read it) and the turn then CONTINUES — the model
            calls tools after the text (any tool round: a wait-family call
            after a partial wake, a side-effect tool such as
            ``store_memory``, a lookup tool, an MCP tool), the implicit park
            fires on a text-only response with live spawned tasks, the spent
            wait budget turns the response into a ``waiting_on_tasks`` park,
            or the stall / steering resume-promise guards re-prompt — the
            loop emits ``{"status": "segment_complete", "content": ...}``
            instead of ``stream_retract``, so the consumer persists the text
            as a completed intermediate message (the steering fold-in
            contract) rather than discarding a bubble the user just read.
            Text that was shown is only ever retracted when it is about to
            be REGENERATED: LLM timeout retries (the same content re-streams)
            and a max_tokens-truncated tool round (discarded and retried by
            the truncation guard).  Default ``False``: assignments,
            sub-agents, consults, evaluations, public embed chat, the REST
            path and external channels keep the retract + ``thinking_text``
            contract, byte-identical to the previous behavior.
        on_iteration_activity: Optional async callback invoked once per
            iteration (right where the per-iteration summary is logged) with
            ``(iteration_number, tool_call_names, had_error)``.  It lets a
            caller stream a deterministic, LLM-independent "what the agent is
            doing right now" signal to its own UI/timeline even when the model
            never narrates - the frozen-cockpit fix for the autonomous
            orchestrator.  ``tool_call_names`` is this iteration's requested
            tool calls (``[]`` on a final-answer turn); ``had_error`` is a
            best-effort flag, true when a tool result from the PREVIOUS
            iteration was an error (results for the current iteration are not
            executed yet at the call site).  Every invocation is wrapped in
            try/except so a callback error can never break the loop.
        on_checkpoint: Optional SYNCHRONOUS callback handed the messages the
            loop appended to *messages* (everything that was not in the
            caller's list), at the top of every iteration and before the
            forced final answer - the boundaries where every tool call has
            its result. It must not block: the web chat keeps the snapshot
            and writes it from a task of its own, so the tool work of a turn
            whose process dies is resumed by the next turn instead of being
            done again (``services/turn_checkpoint.py``). A callback
            that raises is logged and never breaks the loop.
        extend_iterations_to: Makes ``max_iterations`` a block rather than
            an end. When the run reaches the first point where it
            would be told to wrap up (the step-budget nudge, the wind-down,
            the tool strip) and the block it just spent made progress - a
            tool call that succeeded, no loop-breaker refusal, no failure
            brake, no token wind-down, and the wall clock leaves room for
            another wind-down - it gets another block of ``max_iterations``
            rounds, up to this ceiling. The first block that made none ends
            the extensions, and the run winds down as before. ``None`` (the
            default) keeps the cap.
        context_sections: Characters per labelled section of the system
            prompt, as its assembler measured them (``persona``,
            ``agent_memory``, ``skills_catalog``...). Splits the ``system``
            bucket of each reading's ``context_detail``, what the chat's
            ``/context`` command shows; ``None`` leaves that split out.
        working_window_tokens: A window, in tokens, that the working-context
            budgets are computed on instead of the model's when it is
            smaller: the live tool-result budget, the unwritten-turn
            protection, the vision budget and the compaction trigger and
            target. The model's real window stays the hard bound of every
            request (the overflow guard, the emergency prune). An
            event-driven run on a 1M-token model otherwise re-sent up to
            ~800k characters of tool results on every call of a 40-call
            loop (#2430). ``None`` or ``0``: the model's window, as before.

    Returns:
        The final ``ChatResponse`` (the one without tool calls; the
        forced-final-answer response if the budget was exhausted; or, when
        ``finalize_on_tool`` triggers, the terminal tool's output wrapped as a
        ``ChatResponse`` with ``stop_reason="finalize_tool"``).
    """
    from app.llm.base import ChatMessage as LLMMessage
    from app.llm.model_limits import resolve_effective_context_window

    def _resolve_ctx_window() -> int:
        """The model's real context window, via the shared resolution chain.

        One definition for the whole run so the derived session budget and the
        per-iteration compaction gate can never disagree about how big the window
        is — they did while one read the provider's flat class default and the
        other the model's real capacity.
        """
        return resolve_effective_context_window(
            model=model,
            overrides=getattr(llm, "context_windows", None),
            provider_default=getattr(llm, "context_window", None),
        )

    # Whether the loop derived these from the model itself, or the caller
    # pinned them.  Only what the loop derived may be re-derived after a
    # budget-driven model downgrade — an explicit caller budget is not
    # the loop's to override.
    _derived_context_budget = max_session_chars is None or max_tool_result_chars is None
    _max_tokens_is_explicit = max_tokens is not None

    # ── Auto-derive context budgets from provider/model when not explicit ─
    if max_session_chars is None or max_tool_result_chars is None:
        ctx_win = _resolve_ctx_window()
        auto_session, auto_tool = _compute_context_budget(ctx_win)
        if max_session_chars is None:
            max_session_chars = auto_session
        if max_tool_result_chars is None:
            max_tool_result_chars = auto_tool
        logger.debug(
            "Context budget for %s model=%s (%s tokens): session=%s chars, tool_result=%s chars",
            llm.provider_name,
            model or "default",
            f"{ctx_win:,}",
            f"{max_session_chars:,}",
            f"{max_tool_result_chars:,}",
        )

    # ── Auto-derive max_tokens from provider/model when not explicit ────
    if max_tokens is None:
        max_tokens = _resolve_effective_max_tokens(llm, model)
    if max_tokens is None:
        raise RuntimeError(
            f"Unable to resolve max_tokens for provider={llm.provider_name} model={model or 'default'}."
        )
    logger.debug(
        "max_tokens for %s model=%s: %s",
        llm.provider_name,
        model or "default",
        f"{max_tokens:,}",
    )

    # ── Per-run spending budget ───────────────────────────────
    # One object bounds cost / tokens / tool calls / duration for this run.
    # Only its DURATION limit is delegated: it is folded into
    # ``wall_clock_deadline`` (tighter bound wins) so duration keeps the
    # existing, battle-tested enforcement path — the in-loop minimum-budget
    # guard, the per-call timeout capping and the forced-answer reserve
    # — instead of gaining a second, parallel one.  Cost,
    # tokens and tool calls are enforced by the reserve/debit gate below.
    _budget = budget if (budget is not None and budget.active) else None
    if _budget is not None:
        _budget.start()
        _budget_deadline = _budget.deadline()
        if _budget_deadline is not None and (
            wall_clock_deadline is None or _budget_deadline < wall_clock_deadline
        ):
            wall_clock_deadline = _budget_deadline
        logger.info("Agent loop per-run budget: %s", _budget.describe_limits())
        # Operator guard: a budget too small to pay for even ONE call of this
        # model, sized down to the smallest allowance a call gets, would make
        # every run return the deterministic digest.  Say so once, up front,
        # instead of letting it look like a model failure.
        _smallest_call = min(MIN_OUTPUT_TOKENS, max_tokens)
        _cold_start = _budget.can_afford(
            estimated_input_tokens=0, max_output_tokens=_smallest_call, final=True
        )
        if _cold_start is not None:
            logger.warning(
                "Agent loop per-run budget cannot cover a single %s-token "
                "call for model=%s (%s) — this run will %s. Raise the budget.",
                f"{_smallest_call:,}",
                model or "default",
                _cold_start.log_detail(),
                (
                    f"start on the fallback model ({_budget.fallback.model_id}) "
                    "instead"
                    if _budget.fallback_available and _budget.fallback is not None
                    else "stop immediately and return a tool-result digest"
                ),
            )

    effective_tool_timeout = (
        max(1, int(tool_timeout_seconds))
        if tool_timeout_seconds is not None
        else _TOOL_EXECUTION_TIMEOUT
    )

    def _remaining_llm_timeout() -> float:
        """Per-call LLM timeout capped to the remaining wall-clock budget.

        P1.1: ``wall_clock_deadline`` was previously
        honoured only by the forced-final-answer backstop, so an in-loop
        LLM call issued near the budget could still be granted the full
        ``_LLM_CALL_TIMEOUT`` (900 s) and be cancelled mid-flight by the
        caller's outer ``asyncio.wait_for``.  Capping every in-loop call
        (and the compaction summarisers) to the remaining budget lets the
        call fail cleanly INSIDE the loop, where the existing timeout
        handlers produce a graceful terminal response.  The 1 s floor
        keeps a nearly-expired budget from turning into a zero/negative
        timeout (the call still fails fast, just through the normal
        timeout path).
        """
        if wall_clock_deadline is None:
            return _LLM_CALL_TIMEOUT
        remaining = wall_clock_deadline - time.monotonic()
        capped = remaining - _IN_LOOP_DEADLINE_RESERVE_SECS
        return min(max(capped, 1.0), _LLM_CALL_TIMEOUT)

    # Build extra kwargs forwarded to every llm.chat() / _streaming_chat() call
    _llm_extra: dict = {}
    if reasoning_effort:
        _llm_extra["reasoning_effort"] = reasoning_effort

    # Structured output kwargs — only applied when tools=None (final answer)
    _structured_extra: dict = {}

    # ``$input``: the run's input document, kept apart from ``session_vars``
    # (the chat path persists those across turns) and merged at resolution.
    _input_vars: dict[str, Any] | None = None
    from app.tools.session_input import (
        INPUT_MOUNT_NAME,
        INPUT_VARIABLE,
        detach_input_document,
        input_document_bytes,
        oversize_input_chars,
    )

    if input_reference:
        _input_document, _input_detached = detach_input_document(messages)
        _input_vars = {} if _input_document is None else {INPUT_VARIABLE: _input_document}
    else:
        # The size protection is not the opt-in: a document the window cannot
        # hold is detached and mounted all the same, ``$input`` left unexposed.
        _input_document, _input_detached = detach_input_document(
            messages,
            max_inline_chars=oversize_input_chars(_resolve_ctx_window(), _CHARS_PER_TOKEN),
            exposed=False,
        )
    if _input_detached:
        # Detached from the prompt: ``code_interpreter`` reads it whole at
        # ``attachments/input.json``, staged before the first model call.
        if file_context is None:
            _executor_ctx = getattr(executor, "file_context", None)
            file_context = _executor_ctx if isinstance(_executor_ctx, FileContext) else FileContext()
        file_context.add_sandbox_input(INPUT_MOUNT_NAME, input_document_bytes(_input_document))
    if structured_output:
        _structured_extra["structured_output"] = structured_output

    if finalize_on_tool:
        _lf_enrich_trace_metadata(
            _lf_get_ctx(),
            {
                "agent.finalize_on_builtin_tool": finalize_on_tool,
                "agent.terminal_tool_configured": finalize_on_tool.get("tool_name")
                if isinstance(finalize_on_tool, dict)
                else None,
            },
        )

    # Reasoning deltas for the generation currently in flight.  ``None`` between
    # generations so a continuation's or retry's deltas can never be attributed
    # to a generation that has already been closed.  Accumulated in full: the
    # provider's own output limit bounds it, and the persistence layer offloads
    # the payload if it turns out to be large.
    _current_generation_reasoning: list[str] | None = None

    async def _retract_answer_stream() -> None:
        """``on_stream_reset`` of a stream that owns a fresh answer bubble.

        The failover wrapper is about to answer again from the start on
        another provider: take back what streamed, as the timeout
        retry does, and put the bubble back in its streaming state. Raises
        when the retract cannot reach the caller (``_retract_or_raise``).
        """
        await _retract_or_raise(on_event)
        await _emit(on_event, {"status": "streaming"})

    def _new_on_thinking(trace_gen_ctx: dict | None = None) -> Callable[[str], Awaitable[None]]:
        """The ``on_thinking`` of ONE model call, forwarding its reasoning
        chunks to the frontend.

        Its first chunk opens a new paragraph: deltas are forwarded verbatim,
        so the reasoning of consecutive model calls would otherwise run
        together in the reasoning pane and the persisted reasoning
        ("...search first.Found three..."). Every call takes a fresh
        one - continuations, retries and forced answers included. The
        generation's own capture keeps the text as the provider sent it.

        *trace_gen_ctx*, when given, has its first reasoning delta noted as
        the generation's first output: a call that does not go through
        ``_streaming_chat`` measures its time to first token here.
        """
        opened = False

        async def _on_thinking(text: str) -> None:
            nonlocal opened
            content = text
            if text:
                _lf_mark_first_output(trace_gen_ctx)
                if not opened:
                    opened = True
                    content = f"\n\n{text}"
                if _current_generation_reasoning is not None:
                    _current_generation_reasoning.append(str(text))
            await _emit(on_event, {"status": "thinking_text", "content": content})

        return _on_thinking

    # ── The wait primitive ────────────────────────────────────
    # ``wait`` is offered by the loop itself rather than by a call site, for the
    # same reason ``load_tools`` is: it is a base primitive of the flow, it is
    # executed *in* the loop instead of through the executor, and everything it
    # must respect is loop state — the wait policy, ``wall_clock_deadline``,
    # ``cancel_event`` and the iteration budget it is exempt from.  The
    # definition is appended after the provider tool-count cap below, so the
    # timer can never displace one of the agent's own tools.
    _wait_policy = wait_policy or WaitPolicy()
    _wait_budget = WaitBudget(_wait_policy)

    def _steering_pending() -> bool:
        """True when a mid-run user message is queued for injection.

        Passed to every ``execute_wait`` call as ``pending_input`` so no pause
        (explicit ``wait`` call or implicit spawned-task park) can start — or
        keep running — while the user's message sits in the steering queue.
        The skip event alone cannot guarantee this: a message that arrives
        while the model is mid-generation sets the skip with no pause in
        flight, and ``execute_wait``'s stray-skip guard would drop it.
        """
        return steering_queue is not None and not steering_queue.empty()

    # ── Tool-name sanitisation ────────────────────────────────────────
    # MCP servers and custom tools can return names with dots, slashes,
    # spaces, etc. that violate LLM provider restrictions.  We sanitise
    # tool names before sending them to the LLM and keep a reverse map
    # so we can translate LLM-returned names back to originals for the
    # underlying executor (which still uses original names).  The
    # (original, llm_visible) pairs — including the order-dependent
    # collision-suffix disambiguation — come from the shared
    # ``build_llm_tool_names`` so the executors' self-correction listings
    # can never drift from the names actually offered here.
    # Built by ONE function so a refresh inside the turn (#1874) rebuilds
    # every map from the same rules; the closures below read these names at
    # call time, so rebinding them swaps the surface for all of them.
    _surface = _build_tool_surface(tools)
    effective_tools = _surface.effective_tools
    _tool_name_map = _surface.name_map
    _tool_schema_map = _surface.schema_map

    def _lookup_tool_schema(name: str) -> dict[str, Any] | None:
        canonical = _tool_name_map.canonical_visible(name)
        return _tool_schema_map.get(canonical) if canonical is not None else None

    _tool_coalescing_map = _surface.coalescing_map

    def _lookup_tool_coalescing(name: str) -> WriteCoalescing | None:
        return _tool_coalescing_map.get(name)

    _tool_execution_timeouts = _surface.execution_timeouts

    def _declared_timeout(name: str) -> int:
        canonical = _tool_name_map.canonical_visible(name)
        return _tool_execution_timeouts.get(canonical, 0) if canonical is not None else 0

    _tool_send_guard_map = _surface.send_guard_map
    _surface_changing_tools = _surface.surface_changing
    _send_ledger = send_guard.SendLedger()

    async def _record_send(tc: ToolCall, result: Any) -> None:
        """Remember a send that went through, for the duplicate-send guard."""
        _sg_spec = _tool_send_guard_map.get(tc.name)
        if _sg_spec is None or not _app_settings.self_verification_loop:
            return
        if getattr(result, "is_error", True) or getattr(result, "no_op", False):
            return
        _sg_fp = send_guard.fingerprint(tc, _sg_spec, tool=_tool_name_map.get(tc.name, tc.name))
        if _sg_fp is not None:
            await _send_ledger.record(_sg_fp, tc.id)

    _tool_read_back_map = _surface.read_back_map
    _read_back_ledger = write_read_back.ReadBackLedger()

    async def _with_read_back(tc: ToolCall, result: Any, content: str) -> str:
        """Append the read-back and consistency blocks to a successful write."""
        _rb = _tool_read_back_map.get(tc.name)
        if _rb is None or executor is None or not _app_settings.self_verification_loop:
            return content
        if getattr(result, "is_error", True) or getattr(result, "no_op", False):
            return content
        _rb_spec, _rb_suffix = _rb

        async def _read(name: str, arguments: dict[str, Any]) -> ToolResult | None:
            return await executor.execute_tool(
                ToolCall(id=f"{tc.id}:read_back", name=f"{name}{_rb_suffix}", arguments=arguments)
            )

        _rb_call = ToolCall(id=tc.id, name=tc.name, arguments=tc.arguments)
        return content + await write_read_back.build(_rb_spec, _rb_call, result, _read_back_ledger, _read)

    # ── Prompt-guard screening of tool results ────────────────
    # Opt-in per tool in Settings > Access and security, read ONCE per run from the
    # security options snapshot. ``None`` (the default: no tool screened)
    # leaves every tool result exactly as it is and costs nothing more.
    from app.services.tool_result_guard import ToolResultScreen

    _tool_screen = ToolResultScreen.for_run(tools, wall_clock_deadline=wall_clock_deadline)

    # ── Provider tool limit ───────────────────────────────────────────
    # Each LLM provider declares its own ``max_tools`` cap — how many tool
    # schemas ONE request may carry (128 on OpenAI, 1024 on Anthropic, …;
    # per model on a gateway). It is a property of the wire, not of the
    # agent: which list it applies to depends on the gating decision below.
    provider_max_tools = await _max_tools_of(llm, model)

    # ── Progressive tool disclosure (tool multiplexer) ────────────────────
    # When the agent has many gateable tools, expose a compact catalogue plus
    # a single ``load_tools`` meta-tool instead of every schema up-front.  The
    # model reveals a group only when it actually needs it, so trivial turns
    # ("hello") stay cheap.  ``tool_gate`` is None when gating is disabled or
    # the agent is below the activation threshold — in which case behaviour is
    # exactly the legacy "send everything" path.
    #
    # The provider cap is handed to the gate as its ``max_visible`` budget and
    # enforced on the VISIBLE list (always-on core + ``load_tools`` + revealed
    # groups) — never on the inventory.  Truncating the inventory here used to
    # delete whole integration groups from the catalogue on a 128-tool
    # provider: an agent with ~770 tools lost 640 of them BEFORE the gate was
    # built, ``load_tools`` could no longer name them, and a system notice
    # then told the model they were "unavailable this run" — so the very
    # capability progressive disclosure exists to preserve was thrown away,
    # deterministically, on every fresh chat.  The gate now keeps every
    # group loadable and makes room by unloading the least recently used
    # revealed group(s), or by revealing an oversized group partially.
    from app.core.config import settings as _app_settings

    _progressive_enabled = (
        progressive_tools
        if progressive_tools is not None
        else _app_settings.progressive_tools_enabled
    )
    _progressive_min_tools = _app_settings.progressive_tools_min_tools
    # ``build_tool_gate`` also declines a budget below ``MIN_GATED_BUDGET``
    # (the meta-tool plus one real tool): a gate always sends ``load_tools``,
    # so on a zero-tool provider it would break the cap with a single schema —
    # the legacy path below truncates the wire list to the cap (to nothing)
    # instead.  The gate resolves call names with the dispatch map's OWN
    # resolver (same candidates — the whole offered surface, core included —
    # same alias table), so a name can never denote one tool to the gate and
    # another to dispatch.  The large built-in groups are gated too, outside
    # the always-on core the built-in registry declares for each, so a
    # "hello" no longer carries every Copilot or Video Studio schema.
    from app.services.builtin_tool_groups import builtin_on_demand_groups

    tool_gate = (
        build_tool_gate(
            effective_tools,
            enabled=True,
            min_tools=_progressive_min_tools,
            max_visible=provider_max_tools,
            resolver=_tool_name_map.resolver,
            on_demand=builtin_on_demand_groups(),
            # What this turn withholds on purpose is named as such by
            # ``load_tools``, never "unknown" (``CompositeToolExecutor.withhold``).
            withheld=_executor_withheld(executor),
            # A requested tool of an integration this run left out names the
            # integration and why (#11866).
            withheld_integrations=_executor_withheld_integrations(executor),
        )
        if effective_tools and _progressive_enabled and executor is not None
        else None
    )
    # ── Loaded groups persist across turns ─────────────────────
    # The gate is per run, the conversation is not: the groups earlier turns
    # loaded and called into (read off the transcript the caller handed in -
    # ``load_tools`` calls, tool calls, tool results) are revealed before the
    # first request, so a follow-up turn on the same integrations starts
    # with their schemas on the wire instead of a ``load_tools`` round-trip.
    if tool_gate is not None:
        # Best effort: a history the preload cannot read costs the preload
        # (the groups stay one ``load_tools`` away), never the turn.
        try:
            _preloaded_groups = tool_gate.preload_from_transcript(messages)
            if preload_tool_groups:
                # Declared groups go last: an LRU eviction under the cap
                # keeps them over the transcript's.
                for _gid in tool_gate.preload_groups(preload_tool_groups):
                    if _gid not in _preloaded_groups:
                        _preloaded_groups.append(_gid)
        except Exception:
            logger.warning("Progressive tool disclosure: transcript preload failed", exc_info=True)
            _preloaded_groups = []
        if _preloaded_groups:
            logger.info(
                "Progressive tool disclosure: %d group(s) pre-loaded from the "
                "conversation transcript: %s",
                len(_preloaded_groups),
                ", ".join(_preloaded_groups),
            )

    # ── Tool count cap ─────────────────────────────────────────────────
    # One notice format: the model is told exactly which tools it
    # genuinely cannot call this run so it does not hallucinate them from an
    # earlier turn or from the persona.  This block runs once per loop run (it
    # sits before the iteration loop), satisfying the once-per-run contract.
    _dropped_tool_names: list[str] = []
    # The untrimmed list, so a later rise of the model cap (``_follow_model_cap``) can restore what this cut.
    _ungated_pool = list(effective_tools) if tool_gate is None and effective_tools else []
    if tool_gate is not None:
        # Gated path: nothing is dropped.  The gate keeps every group loadable
        # and, when the always-on core alone would leave no room for a gated
        # tool, demotes the least important core tools into a catalogued
        # ``builtin:overflow`` group instead of discarding them (see
        # ``ToolGate._fit_core``); the catalogue names each demoted tool and
        # the ``load_tools`` call that brings it back (#2191).
        if tool_gate.demoted_always_on:
            logger.info(
                "Always-on tool count leaves no room for a gated tool under "
                "the %s provider limit (%d) — %d core tool(s) demoted to the "
                "loadable '%s' group: %s",
                getattr(llm, "provider_name", "unknown"),
                provider_max_tools,
                len(tool_gate.demoted_always_on),
                CORE_OVERFLOW_GROUP_ID,
                ", ".join(
                    td.name
                    for td in tool_gate.demoted_always_on[:_TRUNCATION_NOTICE_MAX_NAMES]
                ),
            )
    elif effective_tools and len(effective_tools) > provider_max_tools:
        # Legacy "send everything" path (gating off or below threshold): the
        # whole list goes on the wire, so the whole list is what the cap
        # trims.  Retention order is ``tool_retention_key`` — source category
        # first (builtin < custom < mcp < integration) so the platform's own
        # builtin tools can never be evicted by integration tools,
        # then per-tool priority within a category so "essential" integration
        # tools (priority=0) are kept when the bulk (priority=1/2) is dropped.
        original_count = len(effective_tools)
        _sorted_tools = sorted(effective_tools, key=tool_retention_key)
        effective_tools = _sorted_tools[:provider_max_tools]
        _dropped_tool_names = [td.name for td in _sorted_tools[provider_max_tools:]]
        logger.warning(
            "Tool count (%d) exceeds %s provider limit (%d) — "
            "truncated to %d tools (dropped %d lower-priority tools). "
            "Consider reducing the number of integrations bound to this agent.",
            original_count,
            getattr(llm, "provider_name", "unknown"),
            provider_max_tools,
            len(effective_tools),
            original_count - len(effective_tools),
        )
    if _dropped_tool_names:
        messages.append(
            LLMMessage(
                role="system",
                content=_build_tool_truncation_notice(_dropped_tool_names),
            )
        )

    # ── Offer the wait primitive, if there is room for it ─────
    # Offered alongside whatever the model can see each iteration — including
    # the progressively-revealed set — but never counted into ``effective_tools``
    # and never in place of a real tool: on a provider with a tight
    # ``max_tools`` the agent's own capabilities matter more than its timer.
    # Skipped when the run has no tools (nothing to wait for) or when a tool
    # named ``wait`` is already offered (an MCP server may own that name).
    _wait_definition: ToolDefinition | None = None
    if effective_tools and _wait_policy.enabled:
        if any(t.name == WAIT_TOOL_NAME for t in effective_tools):
            logger.info(
                "Not offering the built-in wait primitive: a tool named '%s' "
                "is already offered by this run",
                WAIT_TOOL_NAME,
            )
        else:
            _wait_definition = build_wait_tool_definition(_wait_policy)
    _run_pauses = _run_pause_names(
        effective_tools, _tool_name_map, wait_offered=_wait_definition is not None
    )
    # One warning per run for a batch that crosses the tool-call budget
    # without ``AGENT_LOOP_MAX_TOOL_CALLS_STRICT`` (it runs in full).
    _tool_budget_overshoot_logged = False

    def _with_wait_tool(
        offered: list[ToolDefinition] | None,
    ) -> list[ToolDefinition] | None:
        """Append the timer to an offered tool list when the provider has room.

        Re-evaluated per iteration because the visible set grows as the model
        reveals gated groups: the timer yields its slot to the agent's own
        tools rather than pushing one over the provider's ``max_tools``.
        """
        if _wait_definition is None or not offered:
            return offered
        if len(offered) >= provider_max_tools:
            return offered
        return [*offered, _wait_definition]

    def _offer_at(at_iteration: int) -> list[ToolDefinition] | None:
        """The tool list a request placed at *at_iteration* would carry.

        Read from the LIVE visible set (gate or legacy list) every time, never
        from an earlier snapshot: a budget downgrade re-caps the gate
        mid-iteration and a ``load_tools`` reveal evicts groups, so a snapshot
        taken at the top of an iteration can name tools the next request will
        not offer.
        """
        visible = tool_gate.visible_tools() if tool_gate is not None else effective_tools
        return _with_wait_tool(
            _tools_not_in_cooldown(visible, tool_cooldown_until, at_iteration)
        )

    def _span_tools(
        offered: list[ToolDefinition] | None, stripped: bool
    ) -> list[str] | None:
        """``available_tools`` for the iteration span: names, or ``None`` when
        the iteration runs tool-free."""
        if stripped:
            return None
        return [t.name for t in (offered or [])] or None

    if _wait_definition is not None:
        # Every agent gets the timer, so every agent must be told how to use it:
        # ``wait`` is not a bindable built-in an owner can forget to enable, and
        # nothing else in the prompt would mention it.  Appended here, once per
        # run, exactly like the progressive-disclosure catalogue below.
        messages.append(
            LLMMessage(role="system", content=build_wait_system_prompt(_wait_policy))
        )

    # The catalogue system message, kept by identity so a mid-run re-cap of the
    # gate (budget-driven model downgrade) can rewrite it in place — the cap
    # is part of the catalogue text, and an appended notice would leave the
    # stale one in front of the model.
    _catalog_message: LLMMessage | None = None
    # Whether a ``load_tools`` cut down by the tool budget was already logged as a warning.
    _tool_budget_warned = False
    if tool_gate is not None:
        # Keep the meta-tool in ``effective_tools`` so it is part of the
        # Langfuse tool listing and the wind-down/strip truthiness checks.
        # The actual per-iteration tool list comes from ``visible_tools()``.
        effective_tools = [*effective_tools, tool_gate.load_tools_definition]
        _catalog_message = LLMMessage(role="system", content=tool_gate.catalog_prompt)
        messages.append(_catalog_message)
        logger.info(
            "Progressive tool disclosure active: %d tool(s) across %d "
            "group(s) gated behind '%s'",
            tool_gate.gated_tool_count,
            tool_gate.group_count,
            LOAD_TOOLS_TOOL_NAME,
        )
        # ── Trace clarity ─────────────────────────
        # The caller seeds the root trace card with the agent's *full* tool
        # catalogue (``composite.all_tools``) before this loop runs — but once
        # gating is active that reads as "N tools were sent to the model",
        # which is no longer true.  Capture the root trace ctx (set by the
        # caller; ``get_current_trace_ctx`` still points at it here because no
        # iteration span has been pushed yet) and:
        #   1. rewrite the card's ``tools`` to what the model is actually
        #      offered — initially the always-on core + ``load_tools`` only.
        #      It is re-synced on every ``load_tools`` reveal (see the reveal
        #      handler below), so the value after the final reveal equals the
        #      union of tools available across all iterations.
        #   2. annotate the span with the gating split so the counts are
        #      unambiguous even at a glance.
        #
        # ``tools_total`` is the *catalogue* size — it mirrors the root
        # card's ``tool_count`` so the two read identically. It is taken
        # from the original ``tools`` passed in (== the basis the callers
        # use for ``tool_count``), NOT from ``effective_tools``: the latter
        # both counts the synthetic ``load_tools`` meta-tool (off-by-one)
        # and reflects the provider max-tools truncation, either of which
        # would make ``tools_total`` silently diverge from ``tool_count``.
        from app.services.prompt_budget import sent_context

        _root_trace_ctx = _lf_get_ctx()
        _visible_upfront = tool_gate.visible_tools()
        _upfront = len(_visible_upfront)
        _lf_update_trace_tools(_root_trace_ctx, _visible_upfront)
        _lf_enrich_trace_metadata(
            _root_trace_ctx,
            {
                "progressive_tools_active": True,
                "tools_total": len(tools or []),
                "tools_visible_upfront": _upfront,
                "tools_gated": tool_gate.gated_tool_count,
                "tool_groups": tool_gate.group_count,
                # What the first generation receives once the gate applies:
                # the visible tool schemas and the catalog message (#8777).
                "sent_context": sent_context(_visible_upfront, tool_gate.catalog_prompt),
            },
        )
    else:
        _root_trace_ctx = None

    def _recap_gate(cap: int, why: str) -> LLMMessage | None:
        """Re-cap the gate to *cap*; returns the note naming what had to go, if anything did.

        The catalogue carries the budget paragraph, so it is rewritten in place
        (compaction keeps ``system`` messages by identity), else appended.
        """
        evicted, demoted = tool_gate.set_max_visible(cap)
        if _catalog_message is not None and any(m is _catalog_message for m in messages):
            _catalog_message.content = tool_gate.catalog_prompt
        else:
            messages.append(LLMMessage(role="system", content=tool_gate.catalog_prompt))
        if not evicted and not demoted:
            return None
        logger.warning(
            "%s re-capped the visible tool list at %d: unloaded group(s) %s%s",
            why,
            cap,
            evicted or "none",
            f", demoted core tool(s) {demoted} to '{CORE_OVERFLOW_GROUP_ID}'" if demoted else "",
        )
        _lf_update_trace_tools(_root_trace_ctx, tool_gate.visible_tools())
        # A group the model loaded earlier vanished from its tool list, and only the loop knows why.
        changes: list[str] = []
        if evicted:
            changes.append(f"the least recently used tool group(s) were unloaded: {', '.join(evicted)}")
        if demoted:
            changes.append(
                f"the always-on tool(s) {', '.join(demoted)} moved to the loadable group `{CORE_OVERFLOW_GROUP_ID}`"
            )
        return LLMMessage(
            role="system",
            content=(
                f"Note: the new model accepts at most {cap} tool schemas per request, so "
                f"{'; '.join(changes)}. They are still available — call `{LOAD_TOOLS_TOOL_NAME}` "
                "again to reload one when you need it."
            ),
        )

    async def _follow_model_cap(cap: int) -> int:
        """Fit the offered tools to a new per-request cap; returns the cap now in force."""
        nonlocal effective_tools
        if tool_gate is not None:
            if cap < MIN_GATED_BUDGET:
                # ``set_max_visible`` refuses it; the router trims each request instead.
                return provider_max_tools
            _note = _recap_gate(cap, f"Model cap change ({getattr(llm, 'provider_name', 'unknown')}, {model})")
            if _note is not None:
                messages.append(_note)
        elif _ungated_pool and effective_tools:
            # Re-cut from the untrimmed list: a higher cap restores tools a lower one dropped.
            _old = effective_tools
            _before = {td.name for td in _old}
            effective_tools = sorted(_ungated_pool, key=tool_retention_key)[:cap]
            _after = {td.name for td in effective_tools}
            _dropped = [td.name for td in _old if td.name not in _after]
            _restored = [n for n in _after if n not in _before]
            if _dropped:
                logger.warning("Model cap change dropped %d tool(s) to fit %d", len(_dropped), cap)
                messages.append(LLMMessage(role="system", content=_build_tool_truncation_notice(_dropped)))
            if _restored:
                logger.info("Model cap change restored %d tool(s) under %d", len(_restored), cap)
                messages.append(LLMMessage(role="system", content=_build_tool_restored_notice(_restored)))
        return cap

    async def _refresh_tool_surface() -> LLMMessage | None:
        """Reload the integration tools after a call changed them (#1874).

        The caller's refresher rebuilds the integrations through its own allow
        lists and narrowing and swaps the scoped executor's allowed set; the
        loop then rebuilds every per-name map with ``_build_tool_surface`` and,
        when the run is gated, a new gate over the new list with the groups
        the model had loaded revealed again. A failure keeps the old surface.
        """
        nonlocal _surface, effective_tools, _tool_name_map, _tool_schema_map, _tool_coalescing_map
        nonlocal _tool_execution_timeouts, _tool_send_guard_map, _tool_read_back_map, _surface_changing_tools
        nonlocal tool_gate, _ungated_pool, _run_pauses
        if tool_surface_refresher is None:
            return None
        try:
            refreshed = await tool_surface_refresher.refresh()
        except Exception:
            logger.warning("Tool surface refresh failed; the turn keeps its tool list", exc_info=True)
            return LLMMessage(role="system", content=_SURFACE_REFRESH_FAILED_NOTE)
        if not refreshed.added and not refreshed.removed:
            return None
        _surface = _build_tool_surface(refreshed.tools)
        effective_tools = _surface.effective_tools
        _tool_name_map = _surface.name_map
        _tool_schema_map = _surface.schema_map
        _tool_coalescing_map = _surface.coalescing_map
        _tool_execution_timeouts = _surface.execution_timeouts
        _tool_send_guard_map = _surface.send_guard_map
        _tool_read_back_map = _surface.read_back_map
        _surface_changing_tools = _surface.surface_changing
        _gated = False
        if tool_gate is not None:
            _revealed = sorted(tool_gate.revealed_group_ids)
            _new_gate = (
                build_tool_gate(
                    effective_tools,
                    enabled=True,
                    min_tools=_progressive_min_tools,
                    max_visible=provider_max_tools,
                    resolver=_tool_name_map.resolver,
                    on_demand=builtin_on_demand_groups(),
                    withheld=_executor_withheld(executor),
                    withheld_integrations=_executor_withheld_integrations(executor),
                )
                if effective_tools
                else None
            )
            if _new_gate is not None:
                try:
                    _new_gate.preload_groups(_revealed)
                except Exception:
                    logger.warning("Tool surface refresh: loaded groups not carried over", exc_info=True)
                tool_gate = _new_gate
                _gated = True
                effective_tools = [*(effective_tools or []), tool_gate.load_tools_definition]
                if _catalog_message is not None:
                    _catalog_message.content = tool_gate.catalog_prompt
                _lf_update_trace_tools(_root_trace_ctx, tool_gate.visible_tools())
            else:
                tool_gate = None
                if _catalog_message is not None:
                    _catalog_message.content = (
                        f"Note: every tool of this run is now offered directly; `{LOAD_TOOLS_TOOL_NAME}` is gone."
                    )
        if tool_gate is None:
            _ungated_pool = list(effective_tools or [])
            if effective_tools and len(effective_tools) > provider_max_tools:
                effective_tools = sorted(effective_tools, key=tool_retention_key)[:provider_max_tools]
        _run_pauses = _run_pause_names(effective_tools, _tool_name_map, wait_offered=_wait_definition is not None)
        return LLMMessage(
            role="system",
            content=_surface_refresh_note(refreshed.added, refreshed.removed, gated=_gated),
        )

    def _changes_own_agent_tools(result: Any) -> bool:
        """An ``update_agent`` result that changed the tools of the agent this run serves (#2205)."""
        return tool_surface_refresher is not None and tool_surface_refresher.follows(
            getattr(result, "agent_tool_scope", None)
        )

    # Set by a successful call of a ``changes_tool_surface`` tool; the refresh
    # runs once at the end of that step.
    _tool_surface_changed = False

    # Track whether we already injected the wind-down nudge
    wind_down_injected = False
    # Track whether we already injected the last-chance (tool-strip) prompt
    strip_tools_injected = False

    # ── Wind-down timing ──────────────────────────────────────────────
    # Both thresholds are derived from the CURRENT effective budget (see
    # ``_wind_down_points``) and recomputed at the top of every round, because
    # a waived wait round extends ``max_iterations`` mid-run.
    wind_down_at, strip_tools_at = _wind_down_points(max_iterations)

    # ── Iteration-free waiting ────────────────────────────────
    # A round that only *waited* is not charged to the iteration budget: the
    # budget bounds reasoning and acting, while elapsed time is bounded by the
    # wait policy (per-call + per-run caps) and the wall-clock deadline.  The
    # refund is implemented by extending ``max_iterations``, so every existing
    # budget computation, nudge and event payload keeps telling the truth.
    #
    # ``_max_wait_waivers`` is the backstop that keeps the loop finite no matter
    # what the policy allows: a run can at most double its rounds by waiting.
    _max_wait_waivers = max(1, max_iterations)
    _wait_waivers_used = 0

    def _waive_wait_round(label: str) -> None:
        """Refund the current iteration because this round only waited.

        Implemented as an extension of ``max_iterations`` so every existing
        budget computation — the wind-down thresholds, the nudges, the event
        payloads — keeps describing the run's real remaining budget.  Tokens are
        NOT refunded: a waived round is still an LLM call and still counts
        against ``max_total_tokens`` and the wall-clock deadline.
        """
        nonlocal max_iterations, _wait_waivers_used
        if _wait_waivers_used >= _max_wait_waivers:
            logger.info(
                "Iteration %d only waited (%s) but the per-run waiver cap "
                "(%d) is spent — charging it to the budget",
                iteration + 1,
                label,
                _max_wait_waivers,
            )
            return
        _wait_waivers_used += 1
        max_iterations += 1
        logger.info(
            "Iteration %d only waited (%s) — refunding it to the budget "
            "(now %d, waiver %d/%d)",
            iteration + 1,
            label,
            max_iterations,
            _wait_waivers_used,
            _max_wait_waivers,
        )

    # ── Repeated-failure tracking ─────────────────────────────────────
    # Maps tool_name → count of consecutive error results.  When a tool
    # fails _MAX_TOOL_FAILURES times in a row, a system nudge is injected
    # to steer the LLM away from repeating the same broken call pattern.
    tool_error_counts: dict[str, int] = {}
    # The iteration whose failure last grew a tool's streak above: a second
    # failure of the same tool in that iteration (a parallel call of the same
    # batch) is not counted again, so the streak counts failed TURNS.
    tool_error_turns: dict[str, int] = {}
    # The same streaks for results a tool marks ``outcome_failure`` (a paid
    # generation that came out rejected), keyed by the CALL - ``_dedup_key``,
    # the tool and its canonical arguments - so a retry the model changed
    # starts its own count. They neither count toward nor clear the tool's
    # own streak above.
    outcome_error_counts: dict[tuple[str, str], int] = {}
    outcome_nudged: set[tuple[str, str]] = set()
    # The same results counted per TOOL, whatever the arguments, until one of
    # its calls succeeds: the cumulative backstop of the brake.
    outcome_tool_streaks: dict[str, int] = {}
    # Monotonic count of tool errors ever observed - incremented in
    # ``_track_tool_result`` on every error result and NEVER decremented.
    # ``on_iteration_activity`` diffs this against ``_activity_err_seen`` to
    # detect whether the previous iteration produced any tool error.  A plain
    # ``sum(tool_error_counts.values())`` cannot: that dict holds CONSECUTIVE
    # failure streaks and clears a tool's entry on success, so one tool's streak
    # clearing while another tool newly fails nets to zero and hides the error.
    tool_errors_observed = 0
    # Its counterpart for results that succeeded, never decremented either:
    # the progress the iteration-budget extension reads.
    tool_successes_observed = 0
    # Value of ``tool_errors_observed`` the LAST time the per-iteration activity
    # callback fired.  Diffing the two is a best-effort "did the previous
    # iteration error" signal (the current iteration's tools have not executed
    # yet at the summary-log call site, so their results are not available).
    _activity_err_seen = 0
    tool_nudged: set[str] = set()  # tools we already nudged about
    # Consecutive-same-tool-failure brake (P4.1): once ONE tool reaches
    # _TOOL_FAILURE_BRAKE_THRESHOLD consecutive failures, the Stage-2
    # wind-down is forced (tools stripped) for the rest of the run.
    tool_failure_brake = False
    # Tracks distinct tool names rejected as "not available in this context"
    # (ScopedToolExecutor).  The per-name counter doesn't help when the LLM
    # cycles through many hallucinated name variants — this set aggregates
    # them so a cross-name capability-not-found nudge fires after
    # _MAX_NOT_AVAILABLE_DISTINCT_NAMES distinct rejections.
    not_available_tool_names: set[str] = set()
    not_available_nudge_injected: bool = False
    consecutive_truncations: int = 0  # consecutive max_tokens-with-tool-calls
    # Empty-response rounds feeding the backstop's emergency-compaction gate.
    # With-tools branch: incremented per DOUBLE-empty round.  Streaming
    # no-tools branch: incremented on the primary empty, before its one-shot
    # retry — a successful retry returns immediately, so a non-zero
    # value always means the model produced at least one confirmed empty turn.
    consecutive_empty_responses: int = 0
    # Stall guard (execute-don't-narrate): bounded re-prompts when a tool-free
    # answer is actually an unfulfilled promise to act. ``had_tool_calls_this_run``
    # restricts the guard to mid-task runs (the observed failure mode) so a pure
    # text Q&A turn that legitimately ends with an action verb is never forced.
    stall_continuations: int = 0
    # Leaked tool-call guard: bounded re-prompts when a tool-free answer carries
    # tool-call markup emitted as text (the model meant to call a tool but wrote
    # the ``<function_calls>``/``<invoke>`` wrapper into its message). Its own
    # budget, separate from the narration stall guard above.
    leaked_tool_recoveries: int = 0
    had_tool_calls_this_run: bool = False
    # Steering resume-promise guard: ``steered_this_run`` records that at
    # least one mid-run steering message was injected; ``resume_promise_nudged``
    # is the one-shot latch that caps the guard at a single extra LLM round.
    steered_this_run: bool = False
    resume_promise_nudged: bool = False
    # Friction finalisation guard (#8528): one extra round, once per run, to
    # dispose of the frictions the run recorded and left undisposed.
    frictions_nagged: bool = False

    accumulated_usage: dict[str, Any] = {}
    _usage_parts = _RunUsageParts()

    # Per-run memo for the context readout's tool-schema sizes. Tool definitions
    # do not change within a run, so serialising them once instead of once per
    # iteration keeps the readout off the hot path.
    tool_chars_cache: dict[int, int] = {}
    # Set once the emergency prune cut older turns of this run: the readout
    # says so for the rest of the run, since the cut turns stay cut.
    context_overflowed = False

    # ── Identical tool-call dedup cache (CORE-6) ──────────────────────
    # Maps (tool_name, sha256(canonical_json(effective arguments))) → the
    # truncated content AND the files of a SUCCESSFUL call earlier in this
    # run.  Repeated identical calls are served from the cache (with a marker
    # + nudge, files mirrored onto the replayed message) instead of
    # re-executing the tool.  Error results are never cached so failed
    # calls can be retried; polling tools are exempt entirely, and so are
    # the tools whose definition declares ``live_state``, by their
    # ORIGINAL names - the names the cache and the loop breaker key on.
    tool_result_cache: dict[tuple[str, str], _CachedToolResult] = {}
    _live_state_tools = frozenset(t.name for t in tools or () if t.live_state)
    # The tools whose definition declares ``live_read`` (a PR, its checks, a
    # work item): cached and counted within ``_LIVE_READ_WINDOW_S`` only.
    _live_read_tools = frozenset(t.name for t in tools or () if t.live_read)
    # The tools whose calls in one round run in the order the model sent them
    # (``ToolDefinition.ordered_calls``, #12182), by ORIGINAL name.
    _ordered_call_tools = frozenset(t.name for t in tools or () if t.ordered_calls)

    def _cache_ttl(orig_name: str) -> float | None:
        """The cache life of a result of *orig_name*: a live read's window, else the default."""
        return _LIVE_READ_WINDOW_S if orig_name in _live_read_tools else None

    # Output-naming arguments each tool declares, by ORIGINAL name: left out
    # of the dedup and loop-breaker keys.
    _cosmetic_arguments = declared_cosmetic_arguments(tools)
    # ── Near-duplicate read guard ─────────────────────────────────────
    # Per-run count of dedup-eligible calls per (tool, effective arguments)
    # key — served from the cache or executed alike.  Cleared whenever a
    # write tool runs (the state may have changed, so a re-read is legit).
    # Crossing ``_MAX_NEAR_DUPLICATE_CALLS`` injects ``_NEAR_DUPLICATE_NUDGE``
    # once per tool (``near_duplicate_nudged``); the call is never blocked.
    near_duplicate_counts: dict[tuple[str, str], int] = {}
    near_duplicate_nudged: set[str] = set()

    def _cached_read(key: tuple[str, str] | None) -> _CachedToolResult | None:
        """The cached result for *key* while it is fresh; an expired one is dropped."""
        if key is None:
            return None
        entry = tool_result_cache.get(key)
        if entry is None:
            return None
        if not _cache_entry_fresh(entry):
            del tool_result_cache[key]
            return None
        return entry

    def _forget_reads_after_pause() -> None:
        """A pause that really waited: every cached read may be stale now.

        The repeat of a read after a ``wait`` is a poll (CI, a merge, a job),
        never a loop, so it executes again; its near-duplicate and loop-breaker
        counts start over for the same reason a write resets them.
        """
        tool_result_cache.clear()
        near_duplicate_counts.clear()
        loop_breaker.after_write()

    # ── Loop breaker ──────────────────────────────────────────
    # Counts EVERY dispatched call (writes and dedup-exempt reads included,
    # polling tools excluded) per identical arguments and per target, and
    # escalates to a system message then a refusal - see ``_LoopBreaker``.
    # Screened in the pre-dispatch pass right after argument validation, so
    # a refused call never reaches the executor and is answered like a
    # validation failure. Warnings queue here and are injected AFTER the
    # batch's tool results (a system message between an assistant's tool
    # calls and their results would break the pairing every provider
    # checks), on the same site as the near-duplicate nudge.
    loop_breaker = _LoopBreaker(
        identical_warn=_app_settings.agent_loop_identical_call_warn_after,
        identical_refuse=_app_settings.agent_loop_identical_call_refuse_after,
        similar_warn=_app_settings.agent_loop_similar_call_warn_after,
        similar_refuse=_app_settings.agent_loop_similar_call_refuse_after,
        # The live transcript: an ask whose answer curation stubbed or
        # compaction dropped is forgiven at the next screen.
        transcript=messages,
        live_tools=_live_state_tools,
        live_reads=_live_read_tools,
        # A part of one target per distinct position, as each tool declares it.
        declared_positions=_declared_positions(tools),
        cosmetic_arguments=_cosmetic_arguments,
        # Writes by name that check what the other writes change (#175).
        rechecks=frozenset(t.name for t in tools or () if t.recheck_after_write),
    )
    # (LLM-visible tool name, decision) per warning queued this batch.
    _loop_breaker_pending_nudges: list[tuple[str, _LoopDecision]] = []

    def _record_breaker_answer(tc: ToolCall) -> None:
        """Hand the breaker the tool message just appended for *tc*.

        Called right after EVERY ``messages.append`` of a tool result the
        pre-dispatch screen counted - executed, replayed from the dedup cache,
        timed out, raised or refused - so the breaker can later tell an answer
        the model still has from one the harness took away.
        """
        loop_breaker.record_answer(_tool_name_map.get(tc.name, tc.name), tc.arguments, messages[-1])
    # Per-run dispatched calls per LLM-visible tool name: what the step-budget
    # nudge names as "where the steps went".
    tool_call_totals: dict[str, int] = {}
    _step_budget_fraction = _app_settings.agent_loop_step_budget_nudge_fraction
    step_budget_nudge_injected = False
    # The turn's vision budget: distinct images shown, across tools -
    # sized for the run's step budget as it stands, extensions included.
    def _vision_cap() -> int:
        return vision_cap_for(max_iterations)

    async def _inject_loop_breaker_nudges() -> None:
        """Append the breaker's queued system messages (+ a user-visible warning), then empty the queue.

        The ONE injection site, called after a batch's tool results and on the
        sequential cache-hit path that ``continue``s before that block - the
        same two sites the near-duplicate nudge uses, for the same reason.
        """
        if not _loop_breaker_pending_nudges:
            return
        pending = list(_loop_breaker_pending_nudges)
        _loop_breaker_pending_nudges.clear()
        for tool_name, decision in pending:
            messages.append(
                LLMMessage(role="system", content=_format_loop_breaker_nudge(tool_name, decision))
            )
            # The user-visible line names the tool, the count and the shape:
            # "the same tool call is being repeated" gave the reader
            # nothing to check against the tool list.
            await _emit_budget_warning(on_event, _format_loop_breaker_user_warning(tool_name, decision))

    # ── Model-consumed tool results ───────────────────────────────────
    # Identity map of every tool result a completed provider request has
    # carried (see ``_unseen_tool_result_predicate``).  The caller's history
    # counts as consumed — it was sent on earlier turns, or IS the prompt —
    # and the whole transcript is marked after each successful in-loop LLM
    # call.  The always-on curation passes at the top of an iteration and
    # the two prunes that run before a request (the compaction gate's, the
    # forced-answer backstop's) then never clear, collapse or strip the
    # files of a result the model has not taken a single step on: a
    # file-bearing result is never curated before a completed request has
    # carried it (only the emergency prune, for a transcript that would not
    # fit the window at all, may still shrink one).  Strong references on
    # purpose: a message compaction dropped must stay alive here, or its
    # ``id()`` could be reused by a new, unseen result that would then be
    # curated early.
    consumed_tool_results: dict[int, LLMMessage] = {}
    _mark_tool_results_consumed(messages, consumed_tool_results)
    _is_unseen_only = _unseen_tool_result_predicate(consumed_tool_results)
    # The 1-based iteration a completed request belongs to, read by
    # ``_consume_transcript`` to date the results it marks. Set at the
    # top of every iteration; the forced-answer backstop keeps the last value.
    _loop_progress: dict[str, int] = {"iteration": 0}

    # ── File-recovery loop guard ──────────────────────────────────────
    # Counts the ``read_file_content`` results a completed request carried
    # WITHOUT the pixels they were appended with, per file id, and pins the
    # next result for a file id that was answered that way twice (see
    # ``_FileRecoveryGuard``).  A pinned result is protected exactly like an
    # unseen one — the predicate below is what every curation pass takes —
    # and is additionally handed to the emergency prune as ``keep``, the
    # one layer the unseen rule deliberately leaves out.
    file_recovery = _FileRecoveryGuard()
    # The root trace ctx, read before the first iteration opens a child
    # span: a guard record produced after the last iteration span closed
    # (the forced-answer backstop's requests) lands on the root trace's
    # metadata, so the trace still says what happened and where.
    _file_recovery_root_ctx = _lf_get_ctx()
    _file_recovery_root_records: list[dict[str, Any]] = []

    def _is_unseen_tool_result(msg: LLMMessage) -> bool:
        return file_recovery.is_pinned(msg) or _is_unseen_only(msg)

    # ── Turn-scoped protection of results not yet written about ──
    # The results the caller's history brought in (strong references, like
    # the consumed map) are the previous turns'; everything appended from
    # here on is this turn's, and stays out of the byte-budget clearing and
    # the compaction gate's prune until a substantive assistant message
    # follows it (``_unwritten_turn_results``). Recomputed right before those
    # passes run: a new batch joins the set, a substantive message empties it.
    _entry_tool_results: dict[int, LLMMessage] = dict(consumed_tool_results)
    _turn_protected_ids: set[int] = set()

    def _is_history_tool_result(msg: LLMMessage) -> bool:
        return _entry_tool_results.get(id(msg)) is msg

    # The caller's messages, by identity and held strongly like the maps
    # above: what ``on_checkpoint`` is handed is everything else.
    _entry_messages: dict[int, LLMMessage] = {id(m): m for m in messages}
    _checkpoint_failed = False
    # A resumed turn: the writes its interrupted attempt had started and not
    # recorded as answered, by call digest. The first identical repeat of
    # each is answered without running (``not_run="in_flight_repeat"``),
    # except a tool that answers or replaces the job an identical call
    # started (``ToolDefinition.replay_safe``): it runs at once. So does a
    # call its tool declares read-only (``ToolDefinition.read_only`` /
    # ``read_only_call``, :func:`call_is_read_only`): a read never landed.
    from app.services.turn_checkpoint import call_digest as _call_digest
    from app.services.turn_checkpoint import (
        in_flight_landed_answer,
        in_flight_poison,
        in_flight_poison_refusal,
        in_flight_repeat_refusal,
        in_flight_writes,
        landed_note_lines,
    )
    from app.tools.base import call_is_read_only as _call_is_read_only
    from app.tools.base import landed_key_of as _landed_key_of
    from app.tools.base import probe_landed as _probe_landed

    _in_flight_writes = in_flight_writes(
        messages,
        replay_safe=frozenset(t.name for t in tools or () if t.replay_safe),
        read_only=frozenset(t.name for t in tools or () if t.read_only),
    )
    _in_flight_definitions = {t.name: t for t in tools or ()}
    # The same writes by natural key (``ToolDefinition.landed_key``: an issue's
    # repository, a pull request's head branch): a REWORDED repeat (another
    # title, another body) misses the digest, so it is matched here and the
    # interrupted call is probed on its own arguments. Landed, the repeat is
    # answered with what landed, once; anything else runs as asked.
    _in_flight_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for _ifk_digest, _ifk_call in _in_flight_writes.items():
        _ifk_name = str(_ifk_call.get("name") or "")
        _ifk_key = _landed_key_of(_in_flight_definitions.get(_ifk_name), _ifk_call.get("arguments"))
        if _ifk_key is not None:
            _in_flight_by_key.setdefault((_ifk_name, _ifk_key), {**_ifk_call, "digest": _ifk_digest})
    # Each of those writes a tool can look up is read back NOW, on the
    # interrupted call's own arguments, before the first model call: a write
    # that landed is named on the resume note with what it created, so the
    # model starts from the object instead of deciding blind (#11847). The
    # verdict is kept for the guard below, which answers a repeat with it.
    _in_flight_probed: dict[str, str] = {}
    _if_note = next((m for m in reversed(messages) if m.role == "assistant" and m.in_flight), None)
    for _ifp_digest, _ifp_call in list(_in_flight_writes.items()):
        _ifp_definition = _in_flight_definitions.get(str(_ifp_call.get("name") or ""))
        if _ifp_definition is None or _ifp_definition.landed_probe is None:
            continue
        _ifp_verdict = await _probe_landed(_ifp_definition, _ifp_call.get("arguments"))
        if isinstance(_ifp_verdict, str):
            _in_flight_probed[_ifp_digest] = _ifp_verdict
    if _in_flight_probed and _if_note is not None and isinstance(_if_note.content, str):
        _if_note.content = landed_note_lines(_if_note.content, _in_flight_writes, _in_flight_probed)
    # A call in flight at two consecutive cuts, read or write: every identical
    # repeat in this turn is answered unrun (``not_run="in_flight_poison"``),
    # except a tool declared ``cut_safe`` (a service start answers its own job).
    _in_flight_poison = in_flight_poison(
        messages, cut_safe=frozenset(t.name for t in tools or () if t.cut_safe)
    )
    # A checkpointed turn ends its pause when its worker winds down, the wait
    # answered and checkpointed, so the attempt that resumes it counts the
    # call as progress. Without a checkpoint nothing resumes it: the
    # pause runs on to the grace's end as before.
    _wait_stopping: Callable[[], bool] | None = None
    if on_checkpoint is not None:
        from app.core import worker_shutdown as _worker_shutdown

        _wait_stopping = _worker_shutdown.wind_down_due

    def _offer_checkpoint() -> None:
        nonlocal _checkpoint_failed
        if on_checkpoint is None:
            return
        work = [m for m in messages if _entry_messages.get(id(m)) is not m]
        if not work:
            return
        try:
            on_checkpoint(work)
        except Exception as exc:
            if not _checkpoint_failed:
                _checkpoint_failed = True
                logger.warning(
                    "Turn checkpoint callback failed; the turn goes on without "
                    "it: %s",
                    format_exception_message(exc),
                )

    def _refresh_turn_protection() -> None:
        _turn_protected_ids.clear()
        _turn_protected_ids.update(
            _unwritten_turn_results(
                messages,
                is_history=_is_history_tool_result,
                budget_chars=_turn_protection_budget(_resolve_ctx_window(), working_window_tokens),
            )
        )

    def _is_protected_tool_body(msg: LLMMessage) -> bool:
        """The predicate the BODY-clearing passes take: unseen, pinned, or a
        current-turn result no substantive assistant text has followed yet."""
        return _is_unseen_tool_result(msg) or id(msg) in _turn_protected_ids

    def _consume_transcript(
        iter_outcome: dict[str, Any] | None = None, *, phase: str = "iteration"
    ) -> None:
        """A provider request completed on ``messages``: record it.

        The file-recovery guard reads what that request carried FIRST (a
        tracked result appended with files and carried without them is a
        pixel-less answer, logged with its message index and the budget
        state and recorded on the iteration span — or, for a request made
        after the last iteration span closed, on the root trace's
        metadata), then every tool result in the transcript is marked
        consumed.
        """
        failures = file_recovery.observe(messages)
        if failures:
            state = _context_budget_state(messages, _resolve_ctx_window())
            for failure in failures:
                logger.warning(
                    "File-recovery guard: read_file_content(file_id=%s) result "
                    "(tool_call_id=%s) reached the model WITHOUT its pixels — "
                    "answer %d/%d before force-keep; message index %d; budget "
                    "state %s; request: %s",
                    failure["file_id"],
                    failure["tool_call_id"],
                    failure["failures"],
                    failure["force_keep_after"],
                    failure["message_index"],
                    state,
                    phase,
                )
            if iter_outcome is not None:
                iter_outcome.setdefault("file_recovery_pixelless_answers", []).extend(
                    failures
                )
                iter_outcome["file_recovery_budget_state"] = state
            else:
                _file_recovery_root_records.extend(
                    {**failure, "phase": phase} for failure in failures
                )
                _lf_enrich_trace_metadata(
                    _file_recovery_root_ctx,
                    {
                        "file_recovery_pixelless_answers": list(
                            _file_recovery_root_records
                        ),
                        "file_recovery_budget_state": state,
                    },
                )
        _record_vision_views(messages, iteration=_loop_progress["iteration"] or None)
        _mark_tool_results_consumed(
            messages,
            consumed_tool_results,
            iteration=_loop_progress["iteration"] or None,
        )

    def _track_file_recovery(
        tc: ToolCall, msg: LLMMessage, iter_outcome: dict[str, Any]
    ) -> None:
        """Register a just-appended tool result with the guard; pin if due."""
        file_id = _FileRecoveryGuard.file_id_of(
            _tool_name_map.get(tc.name, tc.name), tc.arguments
        )
        if file_id is None:
            return
        file_recovery.track(msg, file_id)
        if not msg.files or not file_recovery.should_pin(file_id):
            return
        file_recovery.pin(msg, file_id)
        index = len(messages) - 1
        while index >= 0 and messages[index] is not msg:
            index -= 1
        state = _context_budget_state(messages, _resolve_ctx_window())
        record = {
            "file_id": file_id,
            "tool_call_id": tc.id,
            "message_index": index,
            "pixelless_answers": file_recovery.failures(file_id),
            "attached_files": [
                getattr(f, "file_id", None) for f in (msg.files or [])
            ],
            "budget_state": state,
        }
        logger.warning(
            "File-recovery guard: read_file_content(file_id=%s) was answered "
            "without pixels %d time(s) this run — force-keeping this result "
            "(tool_call_id=%s, message index %d) through every pruning layer "
            "until a completed request has carried it; budget state %s",
            file_id,
            record["pixelless_answers"],
            tc.id,
            index,
            state,
        )
        iter_outcome.setdefault("file_recovery_force_keep", []).append(record)

    def _ledger_context() -> FileContext | None:
        """The ``FileContext`` whose ledger ``read_file_content`` reads.

        The loop's own when the caller passed one (it is set on the executor
        below, so the two are one object); else the executor's, so a caller
        that wired the context on the executor alone still gets one ledger
        written by the loop and read by the tools.
        """
        if file_context is not None:
            return file_context
        ctx = getattr(executor, "file_context", None)
        return ctx if isinstance(ctx, FileContext) else None

    def _record_vision_views(
        transcript: list[LLMMessage], *, iteration: int | None
    ) -> None:
        """Enter into the turn's vision ledger every image a completed request carried.

        Called from ``_consume_transcript`` BEFORE the consumed map is
        updated, so "newly consumed" is readable: a tool result not yet in
        the map that still has its image files was just seen by the model.
        Dated with the step its tool ran in (``appended_at_iteration``, the
        number ``read_file_content``'s own answer uses), else *iteration*.
        Recording here rather than at append time is what keeps the ledger
        honest: a result appended with its image and pruned before any
        request carried it (the emergency overflow prune) never enters, so
        the next ``read_file_content`` carries the pixels again - the
        recovery path every dropped-files note names - and the
        file-recovery guard can count and pin that retry. First entry wins.
        ``FileContext.vision_views`` is what ``read_file_content`` consults
        before re-sending a file's pixels, and recording EVERY image-bearing
        result (a browser screenshot, a rendered figure, a generated image, a
        cache replay) is what makes "shown at most once per turn" hold across
        tools instead of within one.
        """
        ctx = _ledger_context()
        if ctx is None:
            return
        for msg in transcript:
            if msg.role != "tool" or not msg.files:
                continue
            if consumed_tool_results.get(id(msg)) is msg:
                continue
            step = getattr(msg, "appended_at_iteration", None) or iteration
            if step is None:
                continue
            for file_id in _image_file_ids(msg):
                ctx.record_vision(file_id, step)

    def _vision_in_flight() -> set[str]:
        """The distinct images the turn has shown or is about to show.

        The ledger (carried by a completed request) plus the images on tool
        results appended and not yet carried - a batch of three screenshots
        must spend three of the budget before any request completes.
        """
        ctx = _ledger_context()
        shown: set[str] = set(ctx.vision_views) if ctx is not None else set()
        for m in messages:
            if m.role == "tool" and m.files and consumed_tool_results.get(id(m)) is not m:
                shown.update(_image_file_ids(m))
        return shown

    def _admit_tool_result_images(msg: LLMMessage) -> None:
        """Date a just-appended tool result and apply the turn's vision budget to its images.

        The ONE boundary every tool result crosses, so the rule holds for a
        screenshot, a rendered figure, a generated image, a cache replay and
        a read alike. The step is the one the tool ran in
        (``_loop_progress["iteration"]``, 1-based), stamped as
        ``appended_at_iteration`` so the consumed-tracking dates the view
        with it rather than with the later request that carried the result.
        Then, with a ``FileContext`` to keep the ledger on:

        * a result whose EVERY image the model has already been shown this
          turn (a cache replay of a render, a second tool attaching the same
          file) loses its pixels with the dropped-files note that says at
          which step they were viewed and to work from the notes - the
          once-per-turn rule, enforced here for the tools that do not
          consult the ledger themselves;
        * a result whose new images would take the turn past
          ``AGENT_LOOP_VISION_VIEWS_PER_TURN`` distinct images (the ledger
          plus the images still in flight) loses its pixels with the
          explicit ``vision budget exhausted`` note (:func:`_vision_budget_note`)
          and keeps its text - never a silent send, never a partial one.

        Runs BEFORE the file-recovery guard registers the result, so a
        refusal here is a result appended WITHOUT files: not a pixel-less
        answer to count, never a pin.
        """
        step = _loop_progress.get("iteration") or 1
        if getattr(msg, "appended_at_iteration", None) is None:
            msg.appended_at_iteration = step
        ctx = _ledger_context()
        if ctx is None or not msg.files:
            return
        image_ids = _image_file_ids(msg)
        if not image_ids:
            return
        seen_steps = [ctx.vision_step(fid) for fid in image_ids]
        if all(s is not None for s in seen_steps):
            first_seen = min(s for s in seen_steps if s is not None)
            logger.info(
                "Vision ledger: %s re-attached image(s) %s already shown at step %d - "
                "pixels dropped, notes kept (tool_call_id=%s)",
                msg.name,
                image_ids,
                first_seen,
                msg.tool_call_id,
            )
            _drop_tool_result_files(
                msg, note=_files_removed_note(msg.files or [], seen_at=first_seen)
            )
            return
        cap = _vision_cap()
        if not cap:
            return
        in_flight = _vision_in_flight()
        in_flight.difference_update(image_ids)  # this result's own, appended already
        new_ids = [fid for fid in image_ids if fid not in in_flight]
        if new_ids and len(in_flight) + len(new_ids) > cap:
            logger.warning(
                "Vision budget: %s would show %d new image(s) past the cap of %d "
                "(%d already shown or in flight this turn) - pixels dropped, text "
                "kept (tool_call_id=%s)",
                msg.name,
                len(new_ids),
                cap,
                len(in_flight),
                msg.tool_call_id,
            )
            _drop_tool_result_files(
                msg,
                note=_vision_budget_note(msg.files or [], in_flight=len(in_flight), cap=cap),
            )

    def _count_near_duplicate(tool_name: str, key: tuple[str, str]) -> int | None:
        """Count one dedup-eligible call; return the count when the guard fires.

        Fires (returns the count) exactly once per tool per run, the first
        time a key's count exceeds ``_MAX_NEAR_DUPLICATE_CALLS``; ``None``
        otherwise. Never influences whether the call runs.
        """
        count = near_duplicate_counts.get(key, 0) + 1
        near_duplicate_counts[key] = count
        # The loop breaker counts the SAME key one call earlier, warns
        # and then refuses; with it enabled this nudge would be a second
        # system message about the same repeat, one call after the first. The
        # guard stays the steering for a deployment that switched the breaker
        # off.
        if loop_breaker.enabled:
            return None
        if count > _MAX_NEAR_DUPLICATE_CALLS and tool_name not in near_duplicate_nudged:
            near_duplicate_nudged.add(tool_name)
            return count
        return None

    async def _inject_near_duplicate_nudges(fired: list[tuple[str, int]]) -> None:
        """Append ``_NEAR_DUPLICATE_NUDGE`` (+ a user-visible ``thinking_text``)
        for every guard crossing in *fired*, then empty the list.

        The ONE injection site for the guard. It runs after the batch's tool
        results (next to the repeated-failure nudge) AND on the sequential
        cache-hit path, which ``continue``s to the next iteration before that
        post-batch block: ``_count_near_duplicate`` marks the tool as nudged
        the moment it fires, so a crossing served from the cache there would
        otherwise consume the one-shot without the model ever reading it — and
        a cached re-read IS the trace 722375f1 pattern (the model asking for a
        result it already has). Steering only: the call already ran (or was
        served from the cache); nothing here blocks the next one.
        """
        for _nd_tool, _nd_count in fired:
            logger.warning(
                "Tool '%s' called %d time(s) with the same effective "
                "arguments — injecting near-duplicate read nudge",
                _nd_tool,
                _nd_count,
            )
            messages.append(
                LLMMessage(
                    role="system",
                    content=_NEAR_DUPLICATE_NUDGE.format(
                        tool_name=_nd_tool, count=_nd_count
                    ),
                )
            )
            await _emit_budget_warning(
                on_event,
                (
                    f"⚠️ '{_nd_tool}' has been called {_nd_count} times with "
                    "the same arguments. I will reuse the result I already "
                    "have (read_file_content for a file) instead of fetching "
                    "it again."
                ),
            )
        fired.clear()

    # ── Per-run token budget (CORE-7) ─────────────────────────────────
    # When ``max_total_tokens`` is set and the accumulated usage crosses
    # it, the loop reuses the Stage-2 wind-down (tool stripping + the
    # last-chance prompt) to force a final answer instead of hard-aborting.
    token_budget_exceeded = False

    def _accumulated_total_tokens() -> int:
        """Best-effort total across provider-specific usage keys."""
        total = accumulated_usage.get("total_tokens", 0)
        if total:
            return int(total)
        return int(
            accumulated_usage.get("input_tokens", 0)
            + accumulated_usage.get("output_tokens", 0)
            + accumulated_usage.get("prompt_tokens", 0)
            + accumulated_usage.get("completion_tokens", 0)
        )

    # ── FileContext propagation ────────────────────────────────────────
    # Set the file_context on the executor so all tools can access it.
    if file_context and executor:
        executor.set_file_context(file_context)

    # Tool cooldown (rate-limit or auth/permission failure): maps tool_name →
    # iteration number when it becomes available again.  While on cooldown the
    # tool is stripped from the list sent to the LLM so it physically cannot
    # be called.
    tool_cooldown_until: dict[str, int] = {}

    def _merge_usage(usage: dict, served_model: str | None = None) -> None:
        nonlocal token_budget_exceeded
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                accumulated_usage[k] = accumulated_usage.get(k, 0) + int(v)
        _sum_usage_details_into(accumulated_usage, usage)
        # Kept apart per convention and serving model too, so a run whose
        # calls two providers or two models answered still reads and prices
        # every call.  *served_model* is the model the call's
        # response names, the one its generation records; the failover
        # wrapper's ``last_model`` names the fallback that served the call it
        # made last, if one did, and a usage is merged as its call returns.
        _usage_parts.add(usage, served_model, model, getattr(llm, "last_model", None))
        _usage_parts.attach(accumulated_usage)
        # Per-run budget: debit the ACTUAL spend of the call that
        # just returned.  This is the single sink for LLM usage in the loop
        # (in-loop calls, max-tokens continuations, the compaction summariser
        # - handed this hook as ``merge_usage`` by every compaction site - and
        # the forced-answer backstop all route through it), so nothing the
        # run pays for can escape the accounting.  The worst-case reservation
        # taken before the call is released separately.
        if _budget is not None:
            _budget.record_llm_usage(usage)
        # Token-budget enforcement (CORE-7): checked after every usage
        # update so the very next iteration sees the strip.  One-shot —
        # once exceeded the wind-down stays forced for the rest of the run.
        if (
            max_total_tokens is not None
            and max_total_tokens > 0
            and not token_budget_exceeded
        ):
            _spent = _accumulated_total_tokens()
            if _spent >= max_total_tokens:
                token_budget_exceeded = True
                logger.warning(
                    "Agent loop token budget exceeded: %d token(s) used >= "
                    "budget %d (usage=%s) — stripping tools to force a "
                    "final answer",
                    _spent,
                    max_total_tokens,
                    accumulated_usage,
                )

    # A tool that calls a model itself (the grounded STIX extraction) spends the
    # run's allowance: its usage goes through ``_merge_usage`` like the loop's own
    # calls, and each of its calls reserves its worst case first, against the run's
    # budget and against ``max_total_tokens`` (the tokens spent plus those its calls
    # in flight may still spend), so calls made at once cannot together outspend
    # the run.
    _tool_tokens_in_flight = 0

    def _tool_call_reserve(
        estimated_input_tokens: int, max_output_tokens: int
    ) -> Callable[[], None] | None:
        nonlocal _tool_tokens_in_flight
        if token_budget_exceeded:
            return None
        worst = max(0, estimated_input_tokens) + max(0, max_output_tokens)
        if (
            max_total_tokens is not None
            and max_total_tokens > 0
            and _accumulated_total_tokens() + _tool_tokens_in_flight + worst > max_total_tokens
        ):
            return None
        reservation = None
        if _budget is not None:
            reservation = _budget.try_reserve(
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
            )
            if not reservation.granted:
                return None
        _tool_tokens_in_flight += worst
        budget = _budget

        def release() -> None:
            nonlocal _tool_tokens_in_flight
            _tool_tokens_in_flight -= worst
            if budget is not None and reservation is not None:
                budget.release(reservation)

        return release

    _tool_spend = ToolModelSpend(record=_merge_usage, reserve=_tool_call_reserve)

    # Whether the compaction that just ran spent anything: its summariser's
    # usage reached ``_merge_usage`` through this hook.  Reset before every
    # compaction site; a compaction that spent is followed by the budget check
    # again before the next LLM call, which was priced before that spend.
    _compaction_spent = False

    def _merge_compaction_usage(usage: dict, served_model: str | None = None) -> None:
        nonlocal _compaction_spent
        _compaction_spent = True
        _merge_usage(usage, served_model=served_model)

    # Whether the run can pay for asking a cut compaction summary again.
    # The summariser takes no reservation, so its retry is a second
    # unreserved call: it is made only while the budget still pays for the call
    # the compaction precedes too, so it never costs the run a call the single
    # summariser call would have left it. In the loop the iteration's
    # reservation holds that call already; in the backstop nothing does, and the
    # synthesis is gated at the full limit.
    def _compaction_retry_affordable(estimated_input_tokens: int, max_output_tokens: int) -> bool:
        if _budget is None:
            return True
        return (
            _budget.can_afford(
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
            )
            is None
        )

    def _backstop_compaction_retry_affordable(estimated_input_tokens: int, max_output_tokens: int) -> bool:
        if _budget is None:
            return True
        return (
            _budget.can_afford(
                estimated_input_tokens=estimated_input_tokens + _estimate_prompt_tokens(messages),
                max_output_tokens=max_output_tokens + max_tokens,
                final=True,
            )
            is None
        )

    # The ``max_tokens`` the next model call is made with: the model's full
    # allowance, unless the per-run budget sized the call down to what it can
    # still pay for.
    _call_max_tokens = max_tokens
    # Whether a call of this iteration (or the synthesis) was sized: the calls
    # that follow it stay priced even once one fits in full again, as when a
    # compaction shrank the transcript it was sized on.  Reset by the gate of
    # every iteration and before the synthesis.
    _chain_sized = False
    # What refused the call the budget priced last, ``None`` when it fitted:
    # the reason a sized answer the budget stopped continuing gives.
    _last_refusal: BudgetViolation | None = None

    def _call_sized() -> bool:
        return _chain_sized or _call_max_tokens < max_tokens

    def _reprice_call(
        prospective: list[ChatMessage], tools: list[ToolDefinition] | None = None
    ) -> int | None:
        """Replace the iteration's reservation with one for its next call.

        Priced on the transcript that call carries (and ``tools``, the
        definitions it carries), and sized again when a call before it was:
        a call the budget sized is followed only by calls it can still pay
        for.  The call's ``max_tokens``, or ``None`` - with nothing held -
        when the budget refuses it.
        """
        nonlocal _iter_reservation, _call_max_tokens, _last_refusal, _chain_sized
        if _budget is None:
            return _call_max_tokens
        _chain_sized = _call_sized()
        _budget.release(_iter_reservation)
        _iter_reservation = _budget.try_reserve(
            estimated_input_tokens=_estimate_prompt_tokens(prospective, tools, tool_chars_cache),
            max_output_tokens=max_tokens,
            min_output_tokens=MIN_OUTPUT_TOKENS if _chain_sized else None,
        )
        _last_refusal = _iter_reservation.violation
        if _last_refusal is not None:
            _iter_reservation = None
            return None
        _call_max_tokens = _iter_reservation.max_output_tokens
        return _call_max_tokens

    async def _say_budget_cut(response: ChatResponse) -> None:
        """End a sized answer the budget stopped continuing with the budget
        message, instead of delivering it as if it were complete.

        The stop reason says so outside the text: a structured-output repair
        would close a cut payload into one that parses and drop the message.
        """
        if not _call_sized() or response.stop_reason != "max_tokens" or _last_refusal is None:
            return
        note = f"\n\n{BUDGET_EXHAUSTED_MESSAGE.format(reason=_last_refusal.user_message())}"
        response.content = (response.content or "") + note
        response.stop_reason = BUDGET_EXHAUSTED_STOP_REASON
        if on_token:
            await _stream_text_chunked(on_token, note)

    def _call_refused_after_compaction() -> bool:
        """Re-run the iteration gate for the call that follows a compaction,
        or any later call of an iteration whose call the budget sized down.

        The gate at the top of the iteration priced this call before the
        summariser (or the sized call before it) was debited.  ``True`` when
        the call must not be made now: the per-run budget refuses it, or the
        summariser's usage crossed the token wind-down while tools are still
        offered.  The iteration then makes no call, and the gate at the next
        iteration boundary takes the run down the path every run that reaches
        its budget there takes.  A call that still fits keeps a reservation
        priced on the current prompt, in place of the one taken before.
        """
        if token_budget_exceeded and effective_tools and not _tools_stripped_this_iter:
            return True
        return _reprice_call(messages) is None

    # Explicit init so the forced-final-answer backstop below can reference
    # ``iteration`` safely even when ``max_iterations == 0`` (the loop body
    # never runs).  Inside the loop this is reassigned each turn.
    iteration: int = 0
    # Slow-turn watchdog state: once the run exceeds
    # ``_LOOP_PROGRESS_WARN_AFTER`` seconds, emit a WARNING progress
    # heartbeat at iteration boundaries (throttled to one per
    # ``_LOOP_PROGRESS_WARN_EVERY`` seconds) so long-conversation turns are
    # visible in WARNING+ production log captures instead of looking like a
    # silent death.
    _loop_started_monotonic = time.monotonic()
    _next_progress_warn = _loop_started_monotonic + _LOOP_PROGRESS_WARN_AFTER
    _heartbeat_record = _slow_turn_heartbeats.get()
    # Short per-run identifier carried on every heartbeat line so
    # interleaved heartbeats from different runs on the same conversation
    # (e.g. nested consults, or two loops running concurrently) can be told
    # apart at a glance.
    _turn_id = uuid_module.uuid4().hex[:8]
    # Wall-clock budget-warning nudge state (P4.2): total budget as seen at
    # loop start, and the one-shot flag.  ``None`` when the caller imposes
    # no deadline (the nudge is then disabled).
    _wall_clock_total = (
        wall_clock_deadline - _loop_started_monotonic
        if wall_clock_deadline is not None
        else None
    )
    wall_clock_nudge_injected = False
    # Wait pauses are not "acting" time: Stage-1 / wall-clock "stop exploring"
    # nudges must not fire because the parent spent five minutes parked.
    _wait_paused_total = 0.0
    # Children already ``pending`` (queued, never started) at an implicit park
    # of this run: a final answer is only allowed to leave them behind once
    # they stayed ``pending`` through one whole park (#1661).
    _parked_pending_ids: set[str] = set()
    _last_round_was_wait = False
    # One-shot: set at a wait-budget hand-off when an interactive user opened
    # this turn and the model re-waited on background tasks it did NOT spawn
    # this turn.  Forces the very next iteration tool-free so the model
    # answers the user instead of re-parking behind another canned "still
    # running" notice.  Consumed (reset) where tool-stripping is decided.
    _answer_user_after_park = False
    # Consecutive LLM-call timeout retry counter.  Reset to 0 after
    # each successful LLM call so isolated provider latency spikes do not
    # permanently deplete the per-run retry budget.
    _llm_timeout_retries = 0
    # Set when the per-run budget refused an LLM call, so the
    # forced-final-answer backstop below can name the exhausted dimension in
    # its logs and in what the user sees.
    _budget_violation: BudgetViolation | None = None
    # The loop limit the run ended on, carried onto the answer by
    # ``_with_limit_stop``: ``ITERATION_LIMIT_STOP_REASON`` when the cap was
    # reached, ``DEADLINE_STOP_REASON`` when the wall clock ran out.
    _limit_stop: str | None = None
    # Whether the current iteration runs tool-free because the iteration
    # budget reached its Stage-2 strip point (not the token budget, the
    # failure brake or a park hand-off).
    _iteration_limit_strip = False

    async def _attempt_budget_downgrade(
        violation: BudgetViolation,
        *,
        estimated_input_tokens: int,
        final: bool,
        iteration_label: int | None = None,
    ) -> bool:
        """Continue on a cheaper model instead of stopping, if allowed.

        The third outcome of the budget gate.  A per-run cost cap and a usable
        result are not actually in conflict: capping spend *at a quality tier*
        and then continuing more cheaply gets both, and an operator who caps a
        run at $0.50 on an expensive model usually means "not more than $0.50
        at this quality", not "abandon the task".

        Returns ``True`` when the run has been switched — the budget is
        re-priced and every value the loop had pinned to the old model
        (``model``, the provider, ``max_tokens`` and the context budgets) has
        been re-derived, so the caller can simply carry on.  ``False`` means
        no switch, and the reason is always in the logs.

        Only ``COST`` degrades: a cheaper model buys no extra wall-clock, no
        extra tool calls and no extra tokens, so those dimensions keep
        stopping the run.
        """
        nonlocal llm, model, max_tokens, max_session_chars, max_tool_result_chars
        nonlocal provider_max_tools
        if violation.dimension is not BudgetDimension.COST:
            # Tokens / tool calls / duration are model-independent — there is
            # nothing a cheaper model can buy back.
            return False
        if _budget is None or not _budget.fallback_available:
            return False
        _fallback = _budget.fallback
        if _fallback is None:
            return False
        _plan = _plan_model_downgrade(
            _fallback,
            messages=messages,
            current_llm=llm,
            current_model=model,
            current_max_tokens=max_tokens,
            max_tokens_is_explicit=_max_tokens_is_explicit,
            derive_context_budget=_derived_context_budget,
            # Everything the fallback would have to accept on the wire.  With
            # gating active that is the gate's REQUIRED budget — the always-on
            # core, ``load_tools`` and one slot for a gated tool — because the
            # gate re-caps the revealed groups itself (``set_max_visible``
            # below); the inventory is not what goes on the wire.  One
            # slot less and the re-cap would have to demote a core tool the
            # model has been using as always-on into the loadable overflow
            # group mid-run — a change to what it was told, the same "worse
            # than stopping" as dropping.  Without gating it is the whole
            # list.  The built-in timer is deliberately NOT counted:
            # ``_with_wait_tool`` only appends ``wait`` when the offered list
            # is below the provider's cap, so a surface that exactly fills the
            # fallback's ``max_tools`` still produces a valid request — with
            # the timer omitted, as on the primary.  Counting it would refuse
            # that exact-fit fallback for a slot the timer yields anyway.  The
            # closing synthesis is tool-free, so a fallback provider with a
            # small ``max_tools`` must not be refused for a tool surface that
            # call will never send.
            tool_count=(
                0
                if final
                else (
                    tool_gate.required_budget
                    if tool_gate is not None
                    else len(effective_tools or [])
                )
            ),
            has_structured_output=bool(structured_output),
            # Only shapes the refusal message: the count above already carries
            # the gate's floor (``required_budget >= MIN_GATED_BUDGET``).
            gated=tool_gate is not None and not final,
            # Read on a ``final`` switch too: the plan's cap re-caps the gate.
            max_tools=(
                await _max_tools_of(_fallback.provider, _fallback.model_id)
                if _fallback.provider is not None
                else 0
            ),
        )
        if _plan is None:
            return False
        # Priced with the FALLBACK's own output allowance — the reservation the
        # run is about to take is the one that has to fit, not the primary's.
        if not _budget.can_downgrade(
            violation,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=_plan.max_tokens,
            final=final,
        ):
            logger.warning(
                "Budget-driven model downgrade to %s would not fit either — "
                "%s — not switching",
                _plan.model,
                violation.log_detail(),
            )
            return False

        _downgrade = _budget.commit_downgrade(violation, from_model=model)
        _previous_provider = getattr(llm, "provider_name", None)
        llm = _plan.llm
        model = _plan.model
        # The rest of the run is served on the fallback: the trace says so.
        record_model_downgrade(llm, from_model=_downgrade.from_model, to_model=model)
        max_tokens = _plan.max_tokens
        if _plan.max_session_chars is not None:
            max_session_chars = _plan.max_session_chars
        if _plan.max_tool_result_chars is not None:
            max_tool_result_chars = _plan.max_tool_result_chars
        # ``_with_wait_tool`` re-checks this every iteration, so leaving it on
        # the primary's cap would let the timer push the offered list over the
        # FALLBACK provider's limit.
        provider_max_tools = _plan.provider_max_tools
        # The gate enforces the cap on the visible list; hand it the fallback's
        # so the next request fits.  Groups it has to unload (least recently
        # used first) are named to the model below — they are still in the
        # catalogue and can be loaded again.  Guard 2 already refused
        # any fallback that cannot hold the core plus a gated slot
        # (``required_budget``), so no always-on tool is demoted here.
        _recap_note: LLMMessage | None = None
        if tool_gate is not None and (
            not final or provider_max_tools >= MIN_GATED_BUDGET
        ):
            # The closing synthesis is tool-free, so a ``final`` switch onto a
            # provider that cannot carry a gate at all (below
            # ``MIN_GATED_BUDGET`` — guard 2 only refuses that for the
            # tool-calling phase) leaves the gate as it is: no request with
            # tools follows, and ``set_max_visible`` refuses such a cap.
            _recap_note = _recap_gate(provider_max_tools, "Budget-driven model downgrade")
        # ``reasoning_effort`` was tuned for the primary and is
        # provider/model-specific enough to 400 on the fallback — the same
        # reason ``FailoverLLMProvider`` strips it on every failover call.  A
        # deliberate downgrade is no place to gamble on it.
        _dropped_effort = _llm_extra.pop("reasoning_effort", None)
        logger.warning(
            "Agent loop %s instead of stopping: %s (provider %s -> %s, "
            "max_tokens=%s, context_window=%s, session_chars=%s%s). Budget: %s",
            "continuing the closing synthesis on the fallback model"
            if final
            else "continuing the tool-calling phase on the fallback model",
            _downgrade.log_detail(),
            _previous_provider or "unknown",
            getattr(llm, "provider_name", None) or "unknown",
            f"{max_tokens:,}",
            f"{_plan.context_window:,}",
            f"{max_session_chars:,}" if max_session_chars else "caller-pinned",
            ", dropped reasoning_effort" if _dropped_effort else "",
            _budget.snapshot(),
        )
        await _emit(
            on_event,
            {
                "status": "model_downgraded",
                "from_model": _downgrade.from_model,
                "to_model": _downgrade.to_model,
                "provider": getattr(llm, "provider_name", None),
                "reason": violation.dimension.value,
                "iteration": iteration_label,
            },
        )
        await _emit_budget_warning(
            on_event,
            MODEL_DOWNGRADE_MESSAGE.format(
                to_model=_downgrade.to_model, reason=violation.user_message()
            ),
        )
        # The model cannot see the budget: without this it would only
        # experience an unexplained mid-task capability drop.
        messages.append(
            LLMMessage(
                role="system",
                content=_MODEL_DOWNGRADE_NOTICE.format(
                    reason=violation.user_message(), to_model=_downgrade.to_model
                ),
            )
        )
        if _recap_note is not None:
            messages.append(_recap_note)
        return True

    # ── Keep going while working ──────────────────────────────
    # ``extend_iterations_to`` turns the cap into a block (see its docstring).
    # A block's progress is read off counters the loop already keeps, against
    # their value when the block began. Granted like the wait refunds above, by
    # raising ``max_iterations``, so every threshold, nudge and event payload
    # keeps describing the run's real budget.
    _extension_block = max_iterations
    _extension_ceiling = max(max_iterations, extend_iterations_to or 0)
    _extension_closed = _extension_ceiling <= max_iterations
    _extension_mark = (0, 0)

    def _loop_breaker_refusals() -> int:
        return sum(1 for decision in loop_breaker.events if decision.refused)

    def _extend_iterations_if_working() -> int:
        """Grant another block at the block's wrap-up point when it made progress; the rounds granted."""
        nonlocal max_iterations, _extension_closed, _extension_mark
        if _extension_closed:
            return 0
        wrap_up_points = [
            point
            for point in (
                _step_budget_nudge_iteration(max_iterations, _step_budget_fraction),
                *_wind_down_points(max_iterations),
            )
            if point is not None and point >= 0
        ]
        if not wrap_up_points or iteration < min(wrap_up_points):
            return 0
        successes, refusals = tool_successes_observed, _loop_breaker_refusals()
        working = (
            bool(effective_tools)
            and successes > _extension_mark[0]
            and refusals == _extension_mark[1]
            and not tool_failure_brake
            and not token_budget_exceeded
        )
        room_needed = 0.0
        if working and wall_clock_deadline is not None and iteration > 0:
            pace = (time.monotonic() - _loop_started_monotonic - _wait_paused_total) / iteration
            room_needed = pace * _EXTENSION_WIND_DOWN_ROUNDS + _IN_LOOP_MIN_REMAINING_SECS
            working = wall_clock_deadline - time.monotonic() >= room_needed
        if not working:
            _extension_closed = True
            logger.info(
                "Agent loop keeps its iteration budget of %d at iteration %d: "
                "%d successful tool result(s) and %d loop-breaker refusal(s) in "
                "the last block, failure brake %s, token wind-down %s, %.0fs of "
                "wall clock needed for another wind-down",
                max_iterations,
                iteration + 1,
                successes - _extension_mark[0],
                refusals - _extension_mark[1],
                tool_failure_brake,
                token_budget_exceeded,
                room_needed,
            )
            return 0
        granted = min(_extension_block, _extension_ceiling - max_iterations)
        max_iterations += granted
        _extension_mark = (successes, refusals)
        _extension_closed = max_iterations >= _extension_ceiling
        logger.warning(
            "Agent loop still making progress at iteration %d: iteration budget "
            "extended by %d to %d (ceiling %d)",
            iteration + 1,
            granted,
            max_iterations,
            _extension_ceiling,
        )
        return granted

    # A turn resumed after its worker stopped during a wait finishes that wait
    # here, before its first model call: the model then reads one wait result
    # covering the whole pause instead of spending a generation on calling
    # wait again (dev-docs/background-task-resume.md).
    # The continued round is this turn's work from here on, not the caller's:
    # checkpointed with the new result, it replaces the inherited cut copy, so
    # a later resume replays "Waited" and never the stale "cut short" text
    # (cut again here, the new result carries the same start).
    _cut_found = cut_wait_to_continue(messages)
    # The chat turn's pre-generation timeline (tracing/turn_stages, #9043):
    # the loop's own setup, then the continued pause as a stage of its own,
    # kept out of pre_generation_ms; the first iteration's context work below.
    _stages_ctx = _lf_get_ctx()
    _turn_stage_mark(_stages_ctx, "loop_setup")
    try:
        _continued = await _continue_cut_wait(
            messages,
            on_event=on_event,
            wait_budget=_wait_budget,
            wall_clock_deadline=wall_clock_deadline,
            wait_skip_event=wait_skip_event,
            wait_condition=wait_condition,
            cancel_event=cancel_event,
            pending_input=_steering_pending,
            stopping=_wait_stopping,
        )
    except _CancelledMidCallError:
        logger.info("Continued wait aborted before the first iteration: request cancelled by user")
        return _cancelled_chat_response(model, accumulated_usage)
    if _continued is not None:
        _turn_stage_mark(_stages_ctx, "continued_cut_wait")
    if _continued is not None and _cut_found is not None:
        for _round_message in _round_of_call(messages, _cut_found[1].tool_call_id):
            _entry_messages.pop(id(_round_message), None)

    # The degenerate-repetition guard spares an output-schema agent: its answer
    # is a machine-validated document the schema layer judges, and cutting it to
    # prose would fail the consumer.
    _degenerate_guard_on = structured_output is None and finalize_output_schema is None

    def _is_runaway(text: str, stream: _AnswerStreamCut | None = None) -> bool:
        """Whether the guard ends this answer as a runaway (never when it is off)."""
        return _degenerate_guard_on and _is_degenerate_answer(text, stream)

    # ``itertools.count`` rather than ``range(max_iterations)``: the effective
    # budget grows when a wait round is waived, and the guard below is
    # re-evaluated each round so the loop honours the CURRENT budget.  Every
    # ``continue`` in the body still advances the counter exactly as before.
    for iteration in itertools.count():
        if iteration >= max_iterations:
            _limit_stop = ITERATION_LIMIT_STOP_REASON
            break
        _loop_progress["iteration"] = iteration + 1
        _granted_rounds = _extend_iterations_if_working()
        if _granted_rounds:
            await _emit_budget_warning(
                on_event,
                f"Still making progress after {iteration} steps: the step budget "
                f"is extended to {max_iterations}.",
            )
        # Keep the wind-down stages aligned with the current budget.
        wind_down_at, strip_tools_at = _wind_down_points(max_iterations)
        if cancel_event and cancel_event.is_set():
            logger.info("Agent loop cancelled at iteration %d", iteration + 1)
            return _cancelled_chat_response(model, accumulated_usage)

        # ── Wall-clock budget guard (P1.3) ────────────────────
        # When the remaining budget at iteration start is below
        # ``_IN_LOOP_MIN_REMAINING_SECS``, ``_remaining_llm_timeout()``
        # would return its 1 s floor — a near-zero-timeout call that
        # always fails with "timed out after 1s" despite prior progress.
        # Break immediately so the forced-final-answer backstop can
        # produce a graceful synthesis within the remaining budget.
        if wall_clock_deadline is not None:
            _iter_remaining = wall_clock_deadline - time.monotonic()
            if _iter_remaining < _IN_LOOP_MIN_REMAINING_SECS:
                logger.warning(
                    "Agent loop breaking at iteration %d: only %.1fs remain "
                    "(< %ds minimum) — routing to forced-final-answer backstop "
                    "instead of issuing a near-zero-timeout LLM call",
                    iteration + 1,
                    _iter_remaining,
                    _IN_LOOP_MIN_REMAINING_SECS,
                )
                _limit_stop = DEADLINE_STOP_REASON
                break

        _watchdog_now = time.monotonic()
        if _watchdog_now >= _next_progress_warn:
            _elapsed = _watchdog_now - _loop_started_monotonic
            logger.warning(
                "Agent loop still running (run %s): iteration %d/%d, "
                "elapsed %.0fs, context ~%d chars (%d messages, %d tool "
                "results), tokens used=%d — long conversations "
                "legitimately take multiple minutes per LLM call; this "
                "heartbeat repeats every %.0fs while the turn is in "
                "progress",
                _turn_id,
                iteration + 1,
                max_iterations,
                _elapsed,
                _transcript_chars(messages),
                len(messages),
                sum(1 for m in messages if m.role == "tool"),
                _accumulated_total_tokens(),
                _LOOP_PROGRESS_WARN_EVERY,
            )
            if _heartbeat_record is not None:
                _heartbeat_record.append(_elapsed)
            _next_progress_warn = _watchdog_now + _LOOP_PROGRESS_WARN_EVERY

        # ── Wall-clock budget-warning nudge (P4.2) ────────────────────
        # One-shot: when the run crosses ~50% of its wall-clock budget,
        # tell the model (system nudge) and the user (thinking_text) so it
        # starts consolidating instead of exploring.  Iteration-based
        # wind-down cannot catch this — long LLM calls and slow tools burn
        # wall clock without burning iterations.
        if (
            _wall_clock_total is not None
            and _wall_clock_total > 0
            and not wall_clock_nudge_injected
            and not _last_round_was_wait
            and (
                (_watchdog_now - _loop_started_monotonic) - _wait_paused_total
            )
            >= _wall_clock_total * _WALL_CLOCK_NUDGE_FRACTION
        ):
            wall_clock_nudge_injected = True
            logger.warning(
                "Agent loop crossed %.0f%% of its wall-clock budget "
                "(%.0fs of %.0fs) at iteration %d/%d — injecting the "
                "consolidation nudge",
                _WALL_CLOCK_NUDGE_FRACTION * 100,
                _watchdog_now - _loop_started_monotonic,
                _wall_clock_total,
                iteration + 1,
                max_iterations,
            )
            messages.append(
                LLMMessage(role="system", content=_WALL_CLOCK_NUDGE)
            )
            await _emit_budget_warning(
                on_event,
                (
                    "⚠️ Over half of the time budget for this turn is used. "
                    "I will prioritize consolidating results and finishing."
                ),
            )

        _offer_checkpoint()

        # ── Graceful stop ─────────────────────────────────────
        # A checkpointed turn whose worker is stopping ends here, every tool
        # call it made answered and checkpointed, rather than being cancelled
        # mid-call when the grace runs out: its handlers store the interrupted
        # reply and flush the checkpoint, and "Continue where you left off"
        # resumes it. A turn with no checkpoint runs on to the grace's end.
        if on_checkpoint is not None:
            from app.core.worker_shutdown import WIND_DOWN_CANCEL_MESSAGE, TurnWoundDown, wind_down_due

            if wind_down_due():
                logger.warning(
                    "Agent loop winds down at iteration %d/%d: its server process is stopping; "
                    "the completed tool calls are checkpointed for the next turn",
                    iteration + 1,
                    max_iterations,
                )
                raise TurnWoundDown(WIND_DOWN_CANCEL_MESSAGE)

        # ── Mid-run steering ──────────────────────────────────────────
        # Inject any user messages that arrived while the previous
        # iteration was running (sent from the chat composer during
        # generation).  The iteration boundary is the safe injection
        # point: every pending tool_result has been appended, so the
        # provider-side tool_use/tool_result pairing stays intact.
        # Rich items (SteeringMessage) carry FileRef attachments so a
        # screenshot sent mid-run reaches the model as a multimodal
        # user message.
        _steered = drain_steering_queue(steering_queue)
        if _steered and isinstance(steering_queue, SteeringQueue):
            _steered = await steering_queue.admit(_steered)
        if _steered:
            steered_this_run = True
            _steered_new_files: list = []
            for _steer_item in _steered:
                _steer_files = (
                    _steer_item.files
                    if isinstance(_steer_item, SteeringMessage)
                    else None
                )
                # A mid-run attachment must be exactly as usable as one
                # attached at turn start.  The FileContext and the
                # system prompt's "Available Files" section were both built
                # BEFORE the run began, so without this the file's content
                # is readable inline but (a) tools cannot resolve its
                # file_id and (b) the stale prompt section actively tells
                # the model the file is NOT available — which is why agents
                # answered that a steered file was "read-only".
                for _steer_ref in _steer_files or []:
                    _already_known = file_context is not None and (
                        file_context.get(_steer_ref.file_id) is not None
                    )
                    if file_context is not None and not _already_known:
                        file_context.add(_steer_ref)
                    if not _already_known:
                        _steered_new_files.append(_steer_ref)
                messages.append(
                    LLMMessage(
                        role="user",
                        content=steering_text(_steer_item),
                        files=_steer_files or None,
                    )
                )
            if _steered_new_files:
                _steer_lines = [
                    (
                        f'- file_id="{_ref.file_id}"  '
                        f'filename="{_ref.filename}"  '
                        f"type={_ref.content_type}  size={_ref.size}"
                    )
                    for _ref in _steered_new_files
                ]
                messages.append(
                    LLMMessage(
                        role="system",
                        content=(
                            "[File availability update] The user attached "
                            "new file(s) with their latest message:\n"
                            + "\n".join(_steer_lines)
                            + "\nThese files are FULLY available, exactly "
                            "like any file attached at the start of the "
                            "turn: their contents are included on the user "
                            "message above, and their file_id can be passed "
                            "to any tool that accepts a file reference. The "
                            "\"Available Files\" list in the system prompt "
                            "was generated before these arrived and does "
                            "not include them — this update supersedes it."
                        ),
                    )
                )
            # The text size makes a stale-injection incident
            # diagnosable from logs alone — an "iteration 1" injection
            # of a message-sized text right after a turn started is the
            # signature of an unconsumed steering inbox entry.
            logger.info(
                "Agent loop iteration %d: injected %d mid-run steering "
                "message(s) (%d new file(s), %d chars)",
                iteration + 1,
                len(_steered),
                len(_steered_new_files),
                sum(len(steering_text(_s)) for _s in _steered),
            )
            await _emit(on_event, {"status": "steering", "count": len(_steered)})

        # ── Per-iteration Langfuse span (Phase 2) ─────────────────
        # ``available_tools`` must reflect what the model can actually see
        # this iteration. When progressive disclosure is active that is the
        # always-on core + ``load_tools`` + any groups revealed so far via
        # ``load_tools`` — NOT the full catalogue, which would falsely claim
        # every gated tool was available before it had been loaded.
        # ``visible_tools()`` grows as the model reveals groups,
        # so later iterations correctly show the expanded set.
        #
        # Stage-2 tool stripping (below) sends the LLM *no* tools near the
        # budget cap to force a text answer; on those iterations the span must
        # report ``None`` so it matches what was actually sent rather than the
        # still-"visible" set. The predicate is deterministic
        # from ``iteration`` / ``strip_tools_at`` so it's computed here and
        # reused by the strip block below — the span and the actual tool list
        # can't drift.
        #
        # Likewise, rate-limited tools in their cooldown window are stripped
        # from ``iter_tools`` before the call, so the span applies the same
        # ``_tools_not_in_cooldown`` filter (the shared definition the cooldown
        # block uses) to the visible set — otherwise it would list tools that
        # were not actually offered this iteration.
        # The per-run token budget (CORE-7) reuses the same strip stage:
        # once exceeded, every remaining iteration runs tool-free so the
        # LLM must wind down to a final answer.
        _iteration_limit_strip = iteration >= strip_tools_at and bool(effective_tools)
        _tools_stripped_this_iter = (
            iteration >= strip_tools_at
            or token_budget_exceeded
            or tool_failure_brake
            or _answer_user_after_park
        ) and bool(effective_tools)
        # One-shot consume: the answer-after-park strip applies to THIS
        # iteration only.  Reset now so a later iteration is not
        # permanently held tool-free by a flag set once at a hand-off.
        _answer_user_after_park = False
        # A decline route the failover wrapper took is sticky for the rest of
        # the run, and its model may carry a smaller cap: follow it.
        if iteration and effective_tools:
            _cap_now = await _max_tools_of(llm, model)
            if _cap_now != provider_max_tools:
                provider_max_tools = await _follow_model_cap(_cap_now)
        _offered_this_iter = _offer_at(iteration)
        _iter_ctx = _lf_start_iter_span(
            iteration=iteration + 1,
            available_tools=_span_tools(_offered_this_iter, _tools_stripped_this_iter),
            message_count=len(messages),
        )
        _iter_token = _lf_set_ctx(_iter_ctx) if _iter_ctx else None
        # Per-iteration outcome dict — populated at meaningful exit points
        # so the iteration span in Langfuse shows what actually happened
        # (terminal answer, tool dispatch, timeout, etc.) instead of being
        # blank.  Mutated in place; closed by the ``finally`` below.
        _iter_outcome: dict[str, Any] = {}
        _iter_error: BaseException | None = None
        # Worst-case budget reservation held for this iteration's LLM call.
        # Released in the ``finally`` below, whatever exit the
        # iteration takes, so a reservation can never leak and shrink the
        # remaining budget of a run that is still healthy.
        _iter_reservation: CallReservation | None = None
        try:
            logger.debug("Agent loop iteration %d / %d", iteration + 1, max_iterations)

            # ── Per-run budget gate ──────────────────────────────
            # reserve -> call -> debit -> block.  The reservation prices this
            # iteration's call at its worst case (the prompt as it stands now
            # plus the FULL ``max_tokens`` output allowance, or the largest
            # allowance what is left still covers); ``_merge_usage`` debits
            # the real spend when the call returns and the ``finally`` hands
            # the reservation back, so the gap between worst case and reality
            # is never lost to the run.
            #
            # A reservation refused even at ``MIN_OUTPUT_TOKENS`` is a HARD
            # stop for the tool-calling phase, unlike the soft token wind-down
            # of the legacy ``max_total_tokens`` argument: a call the budget
            # cannot cover is never made. Breaking here routes to the Stage-3
            # backstop, which is gated against the FULL limit
            # (``final=True``) and can therefore spend the held-back
            # closing-synthesis reserve to turn the work done so far into a
            # real answer.
            _chain_sized = False
            _last_refusal = None
            if _budget is not None:
                _iter_reservation = _budget.try_reserve(
                    estimated_input_tokens=_estimate_prompt_tokens(messages),
                    max_output_tokens=max_tokens,
                )
                if _iter_reservation.violation is not None and await _attempt_budget_downgrade(
                    _iter_reservation.violation,
                    estimated_input_tokens=_estimate_prompt_tokens(messages),
                    final=False,
                    iteration_label=iteration + 1,
                ):
                    # Switched to the fallback model instead of stopping.
                    # Re-reserve at the fallback's rates and with its
                    # own output allowance; the rest of this iteration then
                    # re-derives everything else from the new model — including
                    # the context compaction below, which is what brings an
                    # oversized transcript under a SMALLER fallback window
                    # before the call is made.
                    _iter_reservation = _budget.try_reserve(
                        estimated_input_tokens=_estimate_prompt_tokens(messages),
                        max_output_tokens=max_tokens,
                    )
                    # The switch re-capped the gate (groups may have been
                    # unloaded) and changed the wait-tool headroom, so the
                    # offered snapshot taken above — and the iteration span
                    # opened from it — describe a surface this request will
                    # not carry.  Refresh both to what actually goes out.
                    _offered_this_iter = _offer_at(iteration)
                    _lf_update_iter_tools(
                        _iter_ctx,
                        _span_tools(_offered_this_iter, _tools_stripped_this_iter),
                    )
                if _iter_reservation.violation is not None:
                    # Refused at its full allowance and not taken by a
                    # downgrade: make the call with the largest ``max_tokens``
                    # the budget still covers instead.  That call
                    # spends everything left, so the tool definitions it
                    # carries are priced too; refused even so, the full
                    # allowance's refusal stands.
                    _sized_reservation = _budget.try_reserve(
                        estimated_input_tokens=_estimate_prompt_tokens(
                            messages,
                            None if _tools_stripped_this_iter else _offered_this_iter,
                            tool_chars_cache,
                        ),
                        max_output_tokens=max_tokens,
                        min_output_tokens=MIN_OUTPUT_TOKENS,
                    )
                    if _sized_reservation.violation is None:
                        _iter_reservation = _sized_reservation
                if _iter_reservation.violation is None:
                    _call_max_tokens = _iter_reservation.max_output_tokens
                    # Warn only once the call is actually going to happen: a
                    # consolidation nudge appended to a transcript no model
                    # will ever see is pure waste.
                    # A wait-only previous round is not acting: do not consume
                    # the warning (or inject "stop exploring") across a pause.
                    _budget_nudge = (
                        None
                        if _last_round_was_wait
                        else _budget.take_warning()
                    )
                    if _budget_nudge:
                        logger.warning(
                            "Agent loop crossed a budget warning threshold at "
                            "iteration %d/%d (%s) — injecting the "
                            "consolidation nudge",
                            iteration + 1,
                            max_iterations,
                            _budget.snapshot(),
                        )
                        messages.append(
                            LLMMessage(role="system", content=_BUDGET_NUDGE)
                        )
                        await _emit_budget_warning(on_event, _budget_nudge)
                else:
                    _budget_violation = _iter_reservation.violation
                    logger.warning(
                        "Agent loop blocked by its per-run budget at "
                        "iteration %d/%d — %s — ending the tool-calling "
                        "phase and routing to the final-answer backstop",
                        iteration + 1,
                        max_iterations,
                        _budget_violation.log_detail(),
                    )
                    _iter_outcome.update(
                        {
                            "terminal": True,
                            "stop_reason": BUDGET_EXHAUSTED_STOP_REASON,
                            "budget_dimension": _budget_violation.dimension.value,
                        }
                    )
                    await _emit_budget_warning(
                        on_event,
                        f"⚠️ Stopping tool use: {_budget_violation.user_message()}.",
                    )
                    break

            # ── Step-budget nudge ────────────────────────────────
            # Stage 0: once, when the run has spent the configured fraction
            # of its iterations (60% by default) and the wind-down is still
            # ahead, tell the model how many steps went where - the top three
            # tools by call count - so a run that is circling learns it from
            # the count before the wind-down forces its hand. Skipped when the
            # wind-down fires at or before the same iteration, and when the
            # tools are already stripped (the Stage-2 strip, the token budget,
            # the failure brake - a tiny cap strips before 60% lands): one
            # message about the budget, not two, and never "finish or report
            # instead of retrying" next to a request that offers no tool to
            # retry.
            _step_nudge_at = _step_budget_nudge_iteration(max_iterations, _step_budget_fraction)
            if (
                _step_nudge_at is not None
                and iteration >= _step_nudge_at
                and not step_budget_nudge_injected
                and effective_tools
                and not _tools_stripped_this_iter
                and iteration < strip_tools_at
                and not _last_round_was_wait
                and (wind_down_at < 0 or iteration < wind_down_at)
            ):
                step_budget_nudge_injected = True
                _step_nudge = _format_step_budget_nudge(
                    used=iteration, total=max_iterations, tool_call_totals=tool_call_totals
                )
                logger.info(
                    "Injecting step-budget nudge at iteration %d / %d (%s)",
                    iteration + 1,
                    max_iterations,
                    ", ".join(f"{n}={c}" for n, c in sorted(tool_call_totals.items(), key=lambda i: -i[1])[:3]),
                )
                messages.append(LLMMessage(role="system", content=_step_nudge))
                await _emit_budget_warning(
                    on_event,
                    (
                        f"⚠️ {round(100 * iteration / max_iterations)}% of the step "
                        "budget is spent. I will finish or report rather than keep exploring."
                    ),
                )

            # ── Wind-down nudge ──────────────────────────────────────────
            # Stage 1: Inject a system message several iterations before
            # the cap to encourage the LLM to stop calling tools.  The
            # nudge text + the ``wind_down`` event carry the actual
            # remaining-budget count so the LLM has a concrete number to
            # plan against and the frontend can render an honest "N
            # iteration(s) left" warning instead of a generic "winding
            # down" badge.
            if iteration == wind_down_at and not wind_down_injected and effective_tools and not _last_round_was_wait:
                _remaining = max_iterations - iteration
                logger.info(
                    "Injecting wind-down nudge at iteration %d / %d",
                    iteration + 1,
                    max_iterations,
                )
                messages.append(
                    LLMMessage(
                        role="system",
                        content=_build_wind_down_nudge(
                            remaining_iterations=_remaining,
                            max_iterations=max_iterations,
                        ),
                    )
                )
                wind_down_injected = True
                await _emit(
                    on_event,
                    {
                        "status": "wind_down",
                        "remaining_iterations": _remaining,
                        "max_iterations": max_iterations,
                    },
                )
                await _emit_budget_warning(
                    on_event,
                    (
                        "⚠️ Turn budget is running low. I will prioritize a completion "
                        "checkpoint and final summary before this turn ends."
                    ),
                )

            # ── Tool stripping ───────────────────────────────────────────
            # Stage 2: Near the end, remove all tools so the LLM is forced
            # to produce a text response.  This is much more reliable than
            # the nudge alone (LLMs often ignore system nudges and keep
            # calling tools).  With tools=None, the LLM *must* produce text.
            # When progressive disclosure is active, the per-iteration tool
            # list is the always-on core + meta-tool + any groups the model has
            # revealed so far via ``load_tools`` — not the full set.
            iter_tools = (
                tool_gate.visible_tools()
                if tool_gate is not None
                else effective_tools
            )
            if _tools_stripped_this_iter:
                if not strip_tools_injected:
                    logger.info(
                        "Stripping tools at iteration %d / %d — forcing text output",
                        iteration + 1,
                        max_iterations,
                    )
                    messages.append(LLMMessage(role="system", content=_LAST_CHANCE_PROMPT))
                    strip_tools_injected = True
                    await _emit(on_event, {"status": "composing"})
                iter_tools = None

            _ctx_window = _resolve_ctx_window()
            # The budgets below are computed on the working window (#2430);
            # the overflow guard further down stays on the model's.
            _work_window = _working_window(_ctx_window, working_window_tokens)

            # ── Tool-result curation (always-on context-rot guard) ────────
            # BEFORE measuring/compacting: keep the working context focused even
            # when it is comfortably under the window limit. Heavy-tool-use runs
            # degrade from accumulated large tool results (e.g. several big
            # `list_*` calls), not just from hitting the window — so this runs
            # every iteration, independent of the whole-session compaction gate
            # below. First collapse exact duplicates, then keep only the most-
            # recent tool-result bytes up to the live budget (Anthropic
            # "tool-result clearing").  Every pass skips the results the
            # previous iteration appended (``_is_unseen_tool_result``): they
            # are what THIS request exists to show the model, and are sent
            # whole, once, whatever the budgets say.  The body-clearing pass
            # additionally skips every CURRENT-TURN result no substantive
            # assistant text has followed yet (``_is_protected_tool_body``):
            # seen is not used, and a result cleared between two
            # gathering batches is a result the model is then told to fetch
            # again. What bounds such a turn is the window gate below.
            _refresh_turn_protection()
            _collapse_redundant_tool_results(
                messages, is_unseen=_is_unseen_tool_result
            )
            _clear_stale_tool_results(
                messages,
                live_budget_chars=_compute_live_tool_result_budget(_work_window),
                is_unseen=_is_protected_tool_body,
            )
            _clear_stale_tool_result_files(
                messages,
                files_budget_bytes=_compute_tool_result_files_budget(_work_window),
                is_unseen=_is_unseen_tool_result,
            )

            # ── Session context pruning / compaction (gated) ─────────────
            # Single scan reused for compaction gate and overflow guard, on
            # the one measure: text plus provider extras.
            _estimated_session_chars = _transcript_chars(messages)
            _estimated_tokens = _estimated_session_chars // _CHARS_PER_TOKEN
            _context_utilization = _estimated_tokens / _work_window if _work_window > 0 else 0.0
            _compaction_trigger_chars = min(
                max_session_chars,
                int(_work_window * _COMPACTION_TRIGGER_UTILIZATION * _CHARS_PER_TOKEN),
            )
            _compaction_target_chars = min(
                max_session_chars,
                int(_work_window * _COMPACTION_TARGET_UTILIZATION * _CHARS_PER_TOKEN),
            )
            _overflow_target_chars = _compaction_target_chars

            # Auto-compaction trigger honoring both model context utilization and
            # caller-provided session budget.
            if (
                _context_utilization >= _COMPACTION_TRIGGER_UTILIZATION
                or _estimated_session_chars > _compaction_trigger_chars
            ):
                logger.warning(
                    "Context utilization %.1f%% or session budget %d chars exceeded — compacting session context",
                    _context_utilization * 100,
                    _compaction_trigger_chars,
                )
                await _emit(
                    on_event,
                    {
                        "status": "context_compaction",
                        "utilization_percent": round(_context_utilization * 100, 1),
                    },
                )
                # Layered, cheapest-first (Claude Code pipeline order):
                #   1. Cheap tool-result clearing (stub old tool bodies).
                #   2. If still over target, information-preserving SEMANTIC
                #      compaction — distil old turns into one summary while
                #      keeping the task + recent turns verbatim. This only fires
                #      near the real window limit, so within-window long prompts
                #      are untouched; and it summarises only OLDER turns, so a
                #      single long prompt is never gutted.
                # The results the previous iteration appended are skipped here
                # too (``_is_unseen_tool_result``) — this gate runs before the
                # request that shows them to the model — and so are the
                # current turn's results no substantive text has followed:
                # semantic compaction below summarises them rather
                # than stubbing them. Past 95% of the window even after all of
                # this, the protection gives way first (the overflow guard
                # below stubs the older seen results), then the emergency
                # prune, and past it the loop raises ``ContextOverflowError``
                # instead of sending an oversized request - a turn of
                # tool-call-only batches never overflows.
                _prune_session_context(
                    messages,
                    max_session_chars=_compaction_target_chars,
                    is_unseen=_is_protected_tool_body,
                )
                if _transcript_chars(messages) > _compaction_target_chars:
                    _compaction_spent = False
                    await _semantic_compact_session_context(
                        llm,
                        messages,
                        model=model,
                        reason="proactive near-window-limit",
                        cancel_event=cancel_event,
                        # Cap the summariser to the remaining wall-clock
                        # budget (P1.1) — a slow summariser degrades to the
                        # deterministic pruner instead of racing the
                        # caller's outer ``asyncio.wait_for``.
                        llm_timeout=_remaining_llm_timeout(),
                        on_event=on_event,
                        merge_usage=_merge_compaction_usage,
                        can_retry=_compaction_retry_affordable,
                    )
                    if _compaction_spent and _call_refused_after_compaction():
                        # The call made nowhere does not count: the next
                        # iteration's gate decides, as for any exhausted run.
                        max_iterations += 1
                        _iter_outcome["budget_recheck_after_compaction"] = True
                        continue
                _estimated_session_chars = _transcript_chars(messages)
                _estimated_tokens = _estimated_session_chars // _CHARS_PER_TOKEN

            # ── Context overflow guard ────────────────────────────────────
            if _estimated_tokens > _ctx_window * 0.95:
                # The turn protection gives way to the window before
                # anything is TRUNCATED: the older, already-seen results are
                # stubbed with ``_is_unseen_tool_result`` alone (the unseen
                # batch this request exists to show and the pins stay whole),
                # as main did when semantic compaction could not help (one
                # large batch, a summariser that failed).
                _prune_session_context(
                    messages,
                    max_session_chars=_overflow_target_chars,
                    is_unseen=_is_unseen_tool_result,
                )
                _estimated_tokens = _transcript_chars(messages) // _CHARS_PER_TOKEN
            if _estimated_tokens > _ctx_window * 0.95:
                logger.warning(
                    "Context overflow detected: ~%d tokens vs %d window — forcing emergency prune",
                    _estimated_tokens,
                    _ctx_window,
                )
                _emergency_prune_session_context(
                    messages,
                    max_session_chars=_overflow_target_chars,
                    keep=file_recovery.is_pinned,
                )
                context_overflowed = True
                _estimated_tokens = _transcript_chars(messages) // _CHARS_PER_TOKEN
                if _estimated_tokens > _ctx_window * 0.95:
                    logger.error(
                        "Context still too large after emergency prune (%d tokens) — aborting loop",
                        _estimated_tokens,
                    )
                    from app.llm.base import ContextOverflowError

                    raise ContextOverflowError(
                        estimated_tokens=_estimated_tokens,
                        context_window=_ctx_window,
                        model=model,
                        tool_tokens=0,
                        message_tokens=_estimated_tokens,
                    )

            # ── Cooldown filtering (rate-limit / auth failure) ────────────
            # Temporarily remove cooled-down tools so the LLM cannot call
            # them again until the cooldown period expires.
            if iter_tools and tool_cooldown_until:
                cooled = _tools_not_in_cooldown(
                    iter_tools, tool_cooldown_until, iteration
                )
                removed = len(iter_tools) - len(cooled)
                if removed:
                    logger.info(
                        "Tool cooldown: %d tool(s) temporarily disabled",
                        removed,
                    )
                    iter_tools = cooled if cooled else None

            # Offer the timer last: only alongside tools the model can
            # actually call this iteration.  An empty offer — tools stripped for
            # the wind-down, or every tool cooled down — is the loop telling the
            # model to conclude, and adding a way to pause would undo that.
            iter_tools = _with_wait_tool(iter_tools)

            # A sized call spends everything the gate left it, and the
            # messages appended above came after the gate priced it: price it
            # again on the transcript and the tools it is sent with.
            if _call_sized() and _reprice_call(messages, iter_tools) is None:
                max_iterations += 1
                continue

            # Steering admit, context curation and any compaction of this
            # iteration, on the turn's timeline until its first request.
            _turn_stage_mark(_stages_ctx, "iteration_context")

            # Notify caller that the LLM is thinking.
            #
            # The context readout rides along on this event rather than on one
            # of its own: it is emitted once per iteration, at the point where
            # the figures above are final (post-curation, post-compaction,
            # post-emergency-prune), and extra keys on an existing event are
            # ignored by every consumer that does not want them — a new
            # ``status`` value would instead surface as an unknown phase in the
            # web chat's thinking indicator.
            #
            # ``context_tokens`` is a char-derived estimate, like the gate's
            # own: the real count only arrives with the response, and a gauge
            # that lags the turn it describes is worse than one that is honestly
            # approximate.
            #
            # It is NOT the gate's ``_estimated_tokens`` though. The total comes
            # from the breakdown, so the headline and the per-bucket lines can
            # never disagree, and the breakdown counts the tool schemas — which
            # the gate does not, measuring ``messages`` alone. The readout is
            # therefore HIGHER, by the size of those schemas, and deliberately
            # so: they are genuinely sent with every call, and reporting the
            # smaller figure would hide real context pressure.
            #
            # Which is why the warning is ``context_level``, decided here on
            # the gate's own measure against its own trigger: a reader
            # of ``context_tokens`` would warn on the tool schemas, and at a
            # fixed share of the window where the gate's trigger follows the
            # session budget (60% of it with the automatic budget).
            #
            # ``context_detail`` is what the buckets are made of, for the
            # chat's ``/context`` command. It is read server-side only: the
            # published frame drops it (``chat_turn_stream.slim_frame``).
            _ctx_sized = _sized_messages(messages)
            _ctx_buckets, _ctx_total = _context_breakdown_tokens(
                messages, iter_tools, tool_chars_cache, _ctx_sized
            )
            # ``_transcript_chars(messages)``, from the weights just measured.
            _gate_chars = sum(size for _msg, _bucket, size in _ctx_sized)
            await _emit(
                on_event,
                {
                    "status": "thinking",
                    "iteration": iteration + 1,
                    "context_tokens": _ctx_total,
                    "context_window": _ctx_window,
                    "context_breakdown": _ctx_buckets,
                    "context_level": _context_level(
                        _gate_chars,
                        _compaction_trigger_chars,
                        overflowed=context_overflowed,
                    ),
                    "context_detail": _context_detail(
                        _ctx_sized,
                        iter_tools,
                        _ctx_buckets,
                        tool_chars_cache=tool_chars_cache,
                        system_sections=context_sections,
                        compaction={
                            "gate_tokens": _gate_chars // _CHARS_PER_TOKEN,
                            "approaching_tokens": int(
                                _compaction_trigger_chars * _CONTEXT_APPROACHING_SHARE
                            )
                            // _CHARS_PER_TOKEN,
                            "trigger_tokens": _compaction_trigger_chars // _CHARS_PER_TOKEN,
                            "target_tokens": _compaction_target_chars // _CHARS_PER_TOKEN,
                        },
                    ),
                },
            )

            # When tools are stripped (or were never present), we can stream
            # the response token-by-token for a better chat UX.
            if on_token and not iter_tools:
                await _emit(on_event, {"status": "streaming"})
                _current_generation_reasoning = []
                # Streams through the degenerate-repetition stream cut; a
                # runaway ends on the notice before either return below.
                _stripped_stream = _AnswerStreamCut(on_token, enabled=_degenerate_guard_on)
                # Native structured output is forwarded to providers whose
                # ``chat_stream`` supports it (``_streaming_chat`` gates on
                # ``supports_streaming_structured_output``); prompt-based SAP
                # instructions + post-response validation remain the safety
                # net for the rest.
                _gen_ctx = _lf_start_llm_gen(
                    _iter_ctx,
                    model=model,
                    messages=messages,
                    tools=None,
                )
                # Cap the call to the remaining wall-clock budget (P1.1).
                _iter_llm_timeout = _remaining_llm_timeout()
                _usage_sink = StreamUsageSink()
                try:
                    _retry_tok = _lf_set_retry_observer(
                        _lf_make_retry_observer(_gen_ctx)
                    )
                    try:
                        response = await _streaming_chat(
                            llm,
                            messages,
                            _stripped_stream.show,
                            model=model,
                            temperature=temperature,
                            max_tokens=_call_max_tokens,
                            on_thinking=_new_on_thinking() if on_event else None,
                            cancel_event=cancel_event,
                            structured_output=structured_output,
                            timeout=_iter_llm_timeout,
                            on_stream_reset=_stripped_stream.on_restart(
                                _retract_answer_stream
                            ),
                            usage_sink=_usage_sink,
                            trace_gen_ctx=_gen_ctx,
                            answer_cut=_stripped_stream,
                            **_llm_extra,
                        )
                    finally:
                        _lf_reset_retry_observer(_retry_tok)
                except _AnswerCutError as _cut_exc:
                    # The stream cut aborted a runaway: closed with the usage
                    # it reported, answered with the text shown, which the
                    # guard below ends on the notice.
                    response = _end_cut_call(
                        _gen_ctx, _cut_exc, _usage_sink, _merge_usage, model
                    )
                    _gen_ctx = None
                except _CancelledMidCallError as _mc_exc:
                    # Mid-stream user cancel — produce the exact outcome of
                    # the iteration-start cancellation check so downstream
                    # handling is identical wherever the cancel landed.
                    _end_aborted_generation(_gen_ctx, _mc_exc, _usage_sink, _merge_usage)
                    _iter_error = _mc_exc
                    _iter_outcome.update(
                        {
                            "terminal": True,
                            "stop_reason": "cancelled",
                            "error_kind": "CancelledMidCall",
                        }
                    )
                    logger.info(
                        "Agent loop cancelled mid-stream at iteration %d",
                        iteration + 1,
                    )
                    return _cancelled_chat_response(model, accumulated_usage)
                except TimeoutError as _to_exc:
                    _end_aborted_generation(_gen_ctx, _to_exc, _usage_sink, _merge_usage)
                    _gen_ctx = None
                    _current_generation_reasoning = None
                    # Retry on transient provider latency spikes.
                    # Only attempt when retries remain AND the remaining
                    # wall-clock budget still exceeds the minimum floor
                    # AFTER the backoff sleep (see the constants block).
                    # Decide first so the iteration span below is labelled
                    # honestly: a retried iteration is NOT terminal.
                    _remaining_wall_clock = (
                        wall_clock_deadline - time.monotonic()
                        if wall_clock_deadline is not None
                        else float("inf")
                    )
                    _will_retry_timeout = (
                        _llm_timeout_retries < _LLM_TIMEOUT_MAX_RETRIES
                        and _remaining_wall_clock
                        > _LLM_TIMEOUT_RETRY_MIN_REMAINING_SECS
                        + _LLM_TIMEOUT_RETRY_BACKOFF_SECS
                    )
                    # Surface the timeout on the surrounding iteration
                    # span too — the ``finally`` block at the bottom of
                    # the loop reads ``_iter_error`` / ``_iter_outcome``
                    # when closing ``_iter_ctx``. Without this the
                    # iteration would silently appear as a clean success
                    # (empty output, no error) even though the LLM call
                    # never returned, hiding the failure mode from anyone
                    # inspecting the trace after the fact.
                    _iter_error = _to_exc
                    _iter_outcome.update(
                        {
                            "terminal": not _will_retry_timeout,
                            "stop_reason": "timeout",
                            "error_kind": "TimeoutError",
                        }
                    )
                    if _will_retry_timeout:
                        _llm_timeout_retries += 1
                        _iter_outcome["timeout_retry"] = _llm_timeout_retries
                        # Partial answer tokens may already have streamed to
                        # the live answer bubble before the stream stalled —
                        # retract them so the retried call starts clean
                        # instead of appending a duplicate answer.
                        await _emit(on_event, {"status": "stream_retract"})
                        logger.warning(
                            "Streaming LLM call timed out after %.0fs at"
                            " iteration %d — retrying (%d/%d) in %.0fs",
                            _iter_llm_timeout,
                            iteration + 1,
                            _llm_timeout_retries,
                            _LLM_TIMEOUT_MAX_RETRIES,
                            _LLM_TIMEOUT_RETRY_BACKOFF_SECS,
                        )
                        # ``continue`` lets the outer ``finally`` close the
                        # current iteration span with the timeout error
                        # before starting a fresh iteration.
                        await _timeout_retry_backoff(cancel_event)
                        continue
                    logger.error(
                        "Streaming LLM call timed out after %.0fs",
                        _iter_llm_timeout,
                    )
                    from app.llm.base import ChatResponse

                    return ChatResponse(
                        content="I'm sorry, the AI model took too long to respond. Please try again.",
                        model=model or "unknown",
                        usage=accumulated_usage,
                        # Structured terminal reason: without it the
                        # caller sees an ordinary successful answer and the
                        # run is recorded as a clean success, when in fact the
                        # model never responded. Every ``stop_reason`` the loop
                        # already computes for its own control flow should be
                        # readable by whoever consumes the result.
                        stop_reason="timeout",
                    )
                except asyncio.CancelledError as _gen_exc:
                    # Split from the generic ``except Exception`` below so
                    # ``SystemExit`` / ``KeyboardInterrupt`` pass through
                    # uninstrumented (CodeQL ``py/catch-base-exception``
                    # convention applied across the loop's failure sites).
                    # The exception object is still forwarded so the
                    # facade renders ``CancelledError`` on the trace —
                    # ``str(exc)`` alone is empty for cancellation and
                    # would lose the type entirely.
                    _end_aborted_generation(_gen_ctx, _gen_exc, _usage_sink, _merge_usage)
                    raise
                except Exception as _gen_exc:
                    # Pass the exception object (not ``str(exc)``) so the
                    # facade renders ``f"{type(...).__name__}: {exc}"``
                    # and the trace preserves the exception type even for
                    # message-less third-party errors.
                    _end_aborted_generation(_gen_ctx, _gen_exc, _usage_sink, _merge_usage)
                    raise
                else:
                    _record_request_trace_metadata(_gen_ctx, response)
                    _lf_gen_output = _build_generation_trace_output(
                        response,
                        captured_reasoning=(
                            "".join(_current_generation_reasoning)
                            if _current_generation_reasoning
                            else None
                        ),
                    )
                    _lf_end_generation(
                        _gen_ctx,
                        output=_lf_gen_output,
                        usage=response.usage,
                        model=response.model or model,
                        stop_reason=response.stop_reason,
                    )
                    _merge_usage(response.usage, served_model=response.model or model)
                # The request carried the whole transcript: every tool result
                # in it is now consumed and eligible for curation (and the
                # file-recovery guard reads what the request carried).
                _consume_transcript(_iter_outcome)
                _current_generation_reasoning = None
                # No ``_llm_timeout_retries`` reset here: this streaming
                # no-tools branch always terminates the loop (return, or
                # break to the backstop) — it never reaches another
                # iteration, so a reset would be a dead store. A runaway is
                # ended on the notice below, never continued.
                if response.stop_reason == "max_tokens" and not _is_runaway(
                    response.content or "", _stripped_stream
                ):
                    try:
                        response = await _continue_truncated_response(
                            llm,
                            messages,
                            response,
                            on_token=_stripped_stream.show,
                            on_event=on_event,
                            model=model,
                            temperature=temperature,
                            max_tokens=_call_max_tokens,
                            max_session_chars=max_session_chars,
                            llm_extra=_llm_extra,
                            merge_usage=_merge_usage,
                            stop=functools.partial(_is_runaway, stream=_stripped_stream),
                            new_on_thinking=_new_on_thinking if on_event else None,
                            parent_iter_ctx=_iter_ctx,
                            cancel_event=cancel_event,
                            llm_timeout=_remaining_llm_timeout(),
                            next_max_tokens=_reprice_call if _call_sized() else None,
                            answer_cut=_stripped_stream,
                        )
                    except _AnswerCutError:
                        # The stream cut stopped a continuation (the helper
                        # closed it with its usage): the guard below ends the
                        # answer on the text shown.
                        response.stop_reason = "end_turn"
                    except _CancelledMidCallError as _mc_exc:
                        # User cancel during the max_tokens continuation —
                        # produce the exact outcome of the iteration-start
                        # cancellation check (the continuation generation was
                        # already closed inside the helper).
                        _iter_error = _mc_exc
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "cancelled",
                                "error_kind": "CancelledMidCall",
                            }
                        )
                        logger.info(
                            "Agent loop cancelled during max_tokens "
                            "continuation at iteration %d",
                            iteration + 1,
                        )
                        return _cancelled_chat_response(model, accumulated_usage)
                    await _say_budget_cut(response)

                # Empty-response recovery: when the model streamed zero tokens,
                # attempt one no-tools retry (with context compaction for large
                # contexts) before falling through to the forced-final-answer
                # backstop.  This mirrors the same recovery logic in the
                # with-tools branch and avoids needlessly burning the backstop
                # on what is often a transient provider blip.
                if not (response.content or "").strip():
                    _context_chars = _transcript_chars(messages)
                    logger.warning(
                        "Agent loop iter %d: streamed response was empty "
                        "(stop_reason=%s, context=%d chars) — retrying once "
                        "before forced final answer",
                        iteration + 1,
                        response.stop_reason,
                        _context_chars,
                    )
                    consecutive_empty_responses += 1
                    _prior_msg_count = len(messages)
                    messages.append(
                        LLMMessage(role="system", content=_EMPTY_RESPONSE_RETRY)
                    )
                    # ``attempt`` drives the deterministic-fallback ratchet in
                    # _compact_for_empty_recovery; the counter was already
                    # incremented above, so subtract 1 to start the first empty
                    # turn at the baseline target fraction — parity with the
                    # with-tools retry, which passes the pre-increment count.
                    _compaction_spent = False
                    await _compact_for_empty_recovery(
                        llm,
                        messages,
                        model=model,
                        max_session_chars=max_session_chars,
                        attempt=consecutive_empty_responses - 1,
                        phase="streaming empty-response retry",
                        cancel_event=cancel_event,
                        llm_timeout=_remaining_llm_timeout(),
                        on_event=on_event,
                        merge_usage=_merge_compaction_usage,
                        can_retry=_compaction_retry_affordable,
                        keep=file_recovery.is_pinned,
                    )
                    if (_compaction_spent or _call_sized()) and _call_refused_after_compaction():
                        max_iterations += 1
                        if _compaction_spent:
                            _iter_outcome["budget_recheck_after_compaction"] = True
                        continue
                    _empty_retry_ctx = _lf_start_empty_retry_gen(
                        _iter_ctx,
                        model=model,
                        delta_messages=[
                            {"role": "system", "content": _EMPTY_RESPONSE_RETRY}
                        ],
                        prior_message_count=_prior_msg_count,
                        metadata={"context_chars": _context_chars},
                    )
                    _retry_tok = _lf_set_retry_observer(
                        _lf_make_retry_observer(_empty_retry_ctx)
                    )
                    # Arm the reasoning buffer for the retry: the primary
                    # generation closed above and set it back to ``None``, so
                    # without this the retry's thinking deltas would reach the
                    # user's screen and nothing else.
                    _current_generation_reasoning = []
                    _empty_retry_sink = StreamUsageSink()
                    try:
                        try:
                            await _emit(on_event, {"status": "streaming"})
                            _stripped_stream = _AnswerStreamCut(
                                on_token, enabled=_degenerate_guard_on
                            )
                            try:
                                retry_resp = await _streaming_chat(
                                    llm,
                                    messages,
                                    _stripped_stream.show,
                                    model=model,
                                    temperature=temperature,
                                    max_tokens=_call_max_tokens,
                                    on_thinking=_new_on_thinking() if on_event else None,
                                    cancel_event=cancel_event,
                                    structured_output=structured_output,
                                    timeout=_remaining_llm_timeout(),
                                    on_stream_reset=_stripped_stream.on_restart(
                                        _retract_answer_stream
                                    ),
                                    usage_sink=_empty_retry_sink,
                                    trace_gen_ctx=_empty_retry_ctx,
                                    answer_cut=_stripped_stream,
                                    **_llm_extra,
                                )
                            except _AnswerCutError as _cut_exc:
                                # A runaway retry: closed with the usage it
                                # reported, answered with the text shown,
                                # which the guard below ends.
                                retry_resp = _end_cut_call(
                                    _empty_retry_ctx,
                                    _cut_exc,
                                    _empty_retry_sink,
                                    _merge_usage,
                                    model,
                                )
                                _empty_retry_ctx = None
                            else:
                                _merge_usage(
                                    retry_resp.usage, served_model=retry_resp.model or model
                                )
                            _record_request_trace_metadata(_empty_retry_ctx, retry_resp)
                            retry_content = (retry_resp.content or "").strip()
                            if retry_content:
                                _lf_end_generation(
                                    _empty_retry_ctx,
                                    output=_build_generation_trace_output(
                                        retry_resp,
                                        captured_reasoning=(
                                            "".join(_current_generation_reasoning)
                                            if _current_generation_reasoning
                                            else None
                                        ),
                                    ),
                                    usage=retry_resp.usage,
                                    model=retry_resp.model or model,
                                    stop_reason=retry_resp.stop_reason,
                                )
                                # Mirror the primary streaming path (and the
                                # with-tools retry): a truncated retry answer
                                # must be continued, not returned as-is
                                # (a runaway is ended on the notice instead).
                                if retry_resp.stop_reason == "max_tokens" and not _is_runaway(
                                    retry_resp.content or "", _stripped_stream
                                ):
                                    try:
                                        retry_resp = await _continue_truncated_response(
                                            llm,
                                            messages,
                                            retry_resp,
                                            on_token=_stripped_stream.show,
                                            on_event=on_event,
                                            model=model,
                                            temperature=temperature,
                                            max_tokens=_call_max_tokens,
                                            max_session_chars=max_session_chars,
                                            llm_extra=_llm_extra,
                                            merge_usage=_merge_usage,
                                            stop=functools.partial(
                                                _is_runaway, stream=_stripped_stream
                                            ),
                                            new_on_thinking=_new_on_thinking if on_event else None,
                                            parent_iter_ctx=_empty_retry_ctx,
                                            cancel_event=cancel_event,
                                            llm_timeout=_remaining_llm_timeout(),
                                            next_max_tokens=_reprice_call
                                            if _call_sized()
                                            else None,
                                            answer_cut=_stripped_stream,
                                        )
                                    except _AnswerCutError:
                                        # A continuation the stream cut stopped
                                        # (closed by the helper): ended below.
                                        retry_resp.stop_reason = "end_turn"
                                    await _say_budget_cut(retry_resp)
                                if _is_runaway(retry_resp.content or "", _stripped_stream):
                                    await _end_runaway(
                                        retry_resp, _stripped_stream, on_token, shown=True
                                    )
                                logger.info(
                                    "Streaming empty-response retry succeeded: %d chars",
                                    len((retry_resp.content or "").strip()),
                                )
                                retry_resp.usage = accumulated_usage
                                _with_limit_stop(
                                    retry_resp,
                                    ITERATION_LIMIT_STOP_REASON
                                    if _iteration_limit_strip
                                    else None,
                                )
                                _iter_outcome.update(
                                    {
                                        "terminal": True,
                                        "stop_reason": retry_resp.stop_reason,
                                        "content_preview": _trim_trace_text(
                                            retry_resp.content or "", 500
                                        ),
                                        "empty_response_retry": True,
                                    }
                                )
                                return retry_resp
                            # An empty retry still has reasoning worth keeping:
                            # it is the only record of what the model spent the
                            # call doing before returning nothing.
                            _lf_end_generation(
                                _empty_retry_ctx,
                                output=_build_generation_trace_output(
                                    retry_resp,
                                    captured_reasoning=(
                                        "".join(_current_generation_reasoning)
                                        if _current_generation_reasoning
                                        else None
                                    ),
                                ),
                                usage=retry_resp.usage,
                                model=retry_resp.model or model,
                                stop_reason=retry_resp.stop_reason,
                                level="WARNING",
                            )
                            logger.warning(
                                "Streaming empty-response retry also returned empty "
                                "— falling through to forced final answer"
                            )
                        except TimeoutError as _to_exc:
                            _end_aborted_generation(
                                _empty_retry_ctx, _to_exc, _empty_retry_sink, _merge_usage
                            )
                            logger.error(
                                "Streaming empty-response retry timed out — "
                                "falling through to forced final answer"
                            )
                        except asyncio.CancelledError as _empty_retry_exc:
                            _end_aborted_generation(
                                _empty_retry_ctx,
                                _empty_retry_exc,
                                _empty_retry_sink,
                                _merge_usage,
                            )
                            raise
                        except _CancelledMidCallError as _mc_exc:
                            _end_aborted_generation(
                                _empty_retry_ctx, _mc_exc, _empty_retry_sink, _merge_usage
                            )
                            _iter_error = _mc_exc
                            _iter_outcome.update(
                                {
                                    "terminal": True,
                                    "stop_reason": "cancelled",
                                    "error_kind": "CancelledMidCall",
                                }
                            )
                            logger.info(
                                "Agent loop cancelled during streaming "
                                "empty-response retry at iteration %d",
                                iteration + 1,
                            )
                            return _cancelled_chat_response(model, accumulated_usage)
                        except Exception as _empty_retry_exc:
                            _end_aborted_generation(
                                _empty_retry_ctx,
                                _empty_retry_exc,
                                _empty_retry_sink,
                                _merge_usage,
                            )
                            logger.exception(
                                "Streaming empty-response retry failed — "
                                "falling through to forced final answer"
                            )
                    finally:
                        _lf_reset_retry_observer(_retry_tok)
                        _current_generation_reasoning = None
                    # Don't return — let the loop reach the forced-answer
                    # backstop below.
                    break

                llm_system_prompt, llm_user_prompt = _build_llm_prompt_trace(messages)
                llm_response_content = _trim_trace_text(response.content or "", 80_000)
                await _emit(
                    on_event,
                    {
                        "status": "llm_step",
                        "iteration": iteration + 1,
                        "llm_has_tool_calls": False,
                        "llm_tools": [],
                        "llm_model": response.model or model,
                        "llm_stop_reason": response.stop_reason,
                        "llm_usage": response.usage or {},
                        "llm_system_prompt": llm_system_prompt,
                        "llm_user_prompt": llm_user_prompt,
                        "llm_raw_response": llm_response_content,
                    },
                )
                logger.debug(
                    "Agent loop completed (streamed) after %d iteration(s)", iteration + 1
                )
                if _is_runaway(response.content or "", _stripped_stream):
                    await _end_runaway(response, _stripped_stream, on_token, shown=True)
                response.usage = accumulated_usage
                _with_limit_stop(
                    response,
                    ITERATION_LIMIT_STOP_REASON if _iteration_limit_strip else None,
                )
                _iter_outcome.update(
                    {
                        "terminal": True,
                        "stop_reason": response.stop_reason,
                        "content_preview": _trim_trace_text(response.content or "", 500),
                    }
                )
                return response

            _used_stream_tools = False
            # Stream-with-tools hold-back state (see _on_stream_token below):
            # whether held text overflowed into the live answer stream, and
            # any text still held back when the call returned.
            _answer_flushed = False
            # Degenerate-repetition guard state: the live answer stream that
            # cuts a runaway off mid-stream, and whether the guard ends this
            # answer.
            _answer_stream: _AnswerStreamCut | None = None
            _degenerate_cut = False
            # Whether this iteration's no-tool answer came from the
            # empty-response rescue. The rescued text routes through the
            # shared finalization path so the steering resume-promise guard
            # still applies, but the STALL guard is skipped for it: before
            # the route-through a successful rescue was terminal, and
            # spending a stall-nudge LLM round on a context that just
            # produced an empty response risks re-triggering the failure it
            # recovered from (and changes non-steered assignment/sub-agent
            # semantics for no reviewed reason).
            _rescued_no_tool_answer = False
            # Deferred rescue trace state: the flagged ``llm_step`` reporting
            # the rescue's payload is emitted AFTER the shared max_tokens
            # continuation block, so a truncated rescue's span reflects the
            # post-continuation state (final text, final stop reason, summed
            # usage including the continuation spend) instead of freezing the
            # partial raw response and undercounted usage.
            _rescue_trace_pending = False
            _pending_rescue_usage: dict = {}
            _held_text = ""
            _gen_ctx = None
            _usage_sink = StreamUsageSink()
            try:
                _current_generation_reasoning = []
                _call_extra = {
                    **_llm_extra,
                    **(_structured_extra if not iter_tools else {}),
                }
                _gen_ctx = _lf_start_llm_gen(
                    _iter_ctx,
                    model=model,
                    messages=messages,
                    tools=iter_tools,
                )
                # Time to first token of a tool-calling generation: the
                # provider's first content block (tool call included), not
                # only the first text or thinking chunk the caller sees.
                _usage_sink.bind_first_output(
                    lambda _ctx=_gen_ctx: _lf_mark_first_output(_ctx)
                )
                if on_token and iter_tools:
                    # Stream with tools — while the call is in flight we
                    # cannot know whether the text being produced is a
                    # pre-tool-call preamble ("Let me check X…") or the
                    # final answer; that is only decidable once tool calls
                    # appear (or don't).  Routing everything through
                    # ``on_token`` made preambles render as a full-size
                    # answer bubble that later vanished; routing
                    # everything into the reasoning pane made real answers
                    # appear as "thinking" before popping out.
                    #
                    # Best-practice middle ground (hold-back window): buffer
                    # the first ``_PREAMBLE_HOLDBACK_CHARS`` characters.
                    # Preambles are consistently short — if tool calls
                    # follow, the held text is surfaced in the reasoning
                    # pane (see the post-call emission below).  Once the
                    # buffer exceeds the window the text is almost
                    # certainly the final answer: flush it to the live
                    # answer stream and pass subsequent chunks through.
                    _st_buffer: list[str] = []
                    _st_buffered_len = 0
                    _st_flushed = False
                    # Flushed text streams through the degenerate-repetition
                    # stream cut: once a runaway is detected no further chunk
                    # is shown, and the guard ends the answer on the shown text.
                    _answer_stream = _AnswerStreamCut(
                        on_token, enabled=_degenerate_guard_on
                    )
                    _answer_stream.bind_call(_usage_sink)

                    async def _on_stream_token(
                        chunk: str,
                        _buf: list[str] = _st_buffer,
                        _ttft_ctx: dict | None = _gen_ctx,
                        _stream: _AnswerStreamCut = _answer_stream,
                    ) -> None:
                        nonlocal _st_buffered_len, _st_flushed
                        _lf_mark_first_output(_ttft_ctx)
                        if _st_flushed:
                            await _stream.show(chunk)
                            return
                        _buf.append(chunk)
                        _st_buffered_len += len(chunk)
                        if _st_buffered_len >= _PREAMBLE_HOLDBACK_CHARS:
                            _st_flushed = True
                            await _emit(on_event, {"status": "streaming"})
                            await _stream.show("".join(_buf))
                            _buf.clear()

                    async def _restart_answer_stream(
                        _buf: list[str] = _st_buffer,
                        _stream: _AnswerStreamCut = _answer_stream,
                    ) -> None:
                        # The failover wrapper is about to answer again from
                        # the start on another provider: take back
                        # what flushed, as the timeout retry does, and hold
                        # the new answer back like the first. Held-back text
                        # never reached the caller, so it needs no retract.
                        nonlocal _st_buffered_len, _st_flushed
                        if _st_flushed:
                            await _retract_or_raise(on_event)
                        _buf.clear()
                        _st_buffered_len = 0
                        _st_flushed = False
                        _stream.reset()

                    _reset_kwargs = (
                        {"on_stream_reset": _restart_answer_stream}
                        if getattr(llm, "supports_stream_reset", False)
                        else {}
                    )

                    logger.info(
                        "LLM call config (stream_with_tools): model=%s max_tokens=%d temperature=%.3f tools=%d",
                        model or "default",
                        _call_max_tokens,
                        temperature,
                        len(iter_tools),
                    )
                    _retry_tok = _lf_set_retry_observer(
                        _lf_make_retry_observer(_gen_ctx)
                    )
                    # Cap to the remaining wall-clock budget (P1.1).
                    _iter_llm_timeout = _remaining_llm_timeout()
                    try:
                        response = await _race_cancellation(
                            llm.chat_stream_with_tools(
                                messages=messages,
                                tools=iter_tools,
                                on_token=_on_stream_token,
                                model=model,
                                temperature=temperature,
                                max_tokens=_call_max_tokens,
                                on_thinking=_new_on_thinking(_gen_ctx) if on_event else None,
                                usage_sink=_usage_sink,
                                **_call_extra,
                                **_reset_kwargs,
                            ),
                            cancel_event,
                            timeout=_iter_llm_timeout,
                            answer_cut=_answer_stream,
                        )
                    finally:
                        _lf_reset_retry_observer(_retry_tok)
                    _usage_sink.settle()
                    _used_stream_tools = True
                    _answer_flushed = _st_flushed
                    _held_text = "".join(_st_buffer)
                else:
                    logger.info(
                        "LLM call config (chat): model=%s max_tokens=%d temperature=%.3f tools=%d",
                        model or "default",
                        _call_max_tokens,
                        temperature,
                        len(iter_tools) if iter_tools else 0,
                    )
                    _retry_tok = _lf_set_retry_observer(
                        _lf_make_retry_observer(_gen_ctx)
                    )
                    # Cap to the remaining wall-clock budget (P1.1).
                    _iter_llm_timeout = _remaining_llm_timeout()
                    try:
                        response = await _race_cancellation(
                            llm.chat(
                                messages=messages,
                                tools=iter_tools,
                                model=model,
                                temperature=temperature,
                                max_tokens=_call_max_tokens,
                                **_call_extra,
                            ),
                            cancel_event,
                            timeout=_iter_llm_timeout,
                        )
                    finally:
                        _lf_reset_retry_observer(_retry_tok)
                _record_request_trace_metadata(_gen_ctx, response)
                _lf_gen_output = _build_generation_trace_output(
                    response,
                    captured_reasoning=(
                        "".join(_current_generation_reasoning)
                        if _current_generation_reasoning
                        else None
                    ),
                )
                _lf_end_generation(
                    _gen_ctx,
                    output=_lf_gen_output,
                    usage=response.usage,
                    model=response.model or model,
                    stop_reason=response.stop_reason,
                )
                _gen_ctx = None
                _merge_usage(response.usage, served_model=response.model or model)
                # The request carried the whole transcript: every tool result
                # in it is now consumed and eligible for curation at the top
                # of the next iteration.  Marked only on a completed call — a
                # timed-out or failed request is retried with the results
                # still protected.  The file-recovery guard reads what the
                # request carried first.
                _consume_transcript(_iter_outcome)
                _current_generation_reasoning = None
                _llm_timeout_retries = 0  # reset on successful LLM call
            except _CancelledMidCallError as _mc_exc:
                # Mid-LLM-call user cancel (the ``_race_cancellation`` race
                # fired) — produce the exact outcome of the iteration-start
                # cancellation check so downstream handling is identical.
                _end_aborted_generation(_gen_ctx, _mc_exc, _usage_sink, _merge_usage)
                _current_generation_reasoning = None
                _iter_error = _mc_exc
                _iter_outcome.update(
                    {
                        "terminal": True,
                        "stop_reason": "cancelled",
                        "error_kind": "CancelledMidCall",
                    }
                )
                logger.info(
                    "Agent loop cancelled mid-LLM-call at iteration %d",
                    iteration + 1,
                )
                return _cancelled_chat_response(model, accumulated_usage)
            except _AnswerCutError as _cut_exc:
                # The stream cut aborted a runaway answer: the call stops
                # generating, its generation closes with the usage reported so
                # far, and the answer is the text shown, without tool calls,
                # which the degenerate-repetition guard below ends the run on.
                response = _end_cut_call(
                    _gen_ctx, _cut_exc, _usage_sink, _merge_usage, model
                )
                _gen_ctx = None
                # The cut request carried the whole transcript, as a returned
                # one does: the file-recovery guard reads it, its results are
                # consumed.
                _consume_transcript(_iter_outcome)
                _current_generation_reasoning = None
                _llm_timeout_retries = 0
                _used_stream_tools = True
                _answer_flushed = True
                _held_text = ""
            except TimeoutError as _to_exc:
                _end_aborted_generation(_gen_ctx, _to_exc, _usage_sink, _merge_usage)
                _gen_ctx = None
                _current_generation_reasoning = None
                # Retry on transient provider latency spikes.
                # Only attempt when retries remain AND the remaining
                # wall-clock budget still exceeds the minimum floor AFTER
                # the backoff sleep (see the constants block).  Decide
                # first so the iteration span below is labelled honestly:
                # a retried iteration is NOT terminal.
                _remaining_wall_clock = (
                    wall_clock_deadline - time.monotonic()
                    if wall_clock_deadline is not None
                    else float("inf")
                )
                _will_retry_timeout = (
                    _llm_timeout_retries < _LLM_TIMEOUT_MAX_RETRIES
                    and _remaining_wall_clock
                    > _LLM_TIMEOUT_RETRY_MIN_REMAINING_SECS
                    + _LLM_TIMEOUT_RETRY_BACKOFF_SECS
                )
                # Surface the timeout on the iteration span (closed in
                # the outer ``finally``) too — without populating
                # ``_iter_error`` / ``_iter_outcome`` the iteration is
                # recorded as a clean success despite the LLM call
                # never returning. Mirror the streaming-path
                # treatment.
                _iter_error = _to_exc
                _iter_outcome.update(
                    {
                        "terminal": not _will_retry_timeout,
                        "stop_reason": "timeout",
                        "error_kind": "TimeoutError",
                    }
                )
                if _will_retry_timeout:
                    _llm_timeout_retries += 1
                    _iter_outcome["timeout_retry"] = _llm_timeout_retries
                    # The stream-with-tools sub-branch may have flushed
                    # partial answer tokens to the live answer bubble
                    # before timing out (hold-back window overflow) —
                    # retract them so the retried call starts clean
                    # instead of appending a duplicate answer.
                    if on_token and iter_tools and _st_flushed:
                        await _emit(on_event, {"status": "stream_retract"})
                    logger.warning(
                        "LLM call timed out after %.0fs at iteration %d — retrying"
                        " (%d/%d) in %.0fs",
                        _iter_llm_timeout,
                        iteration + 1,
                        _llm_timeout_retries,
                        _LLM_TIMEOUT_MAX_RETRIES,
                        _LLM_TIMEOUT_RETRY_BACKOFF_SECS,
                    )
                    # ``continue`` lets the outer ``finally`` close the
                    # current iteration span with the timeout error before
                    # starting a fresh iteration.
                    await _timeout_retry_backoff(cancel_event)
                    continue
                logger.error(
                    "LLM call timed out after %.0fs at iteration %d",
                    _iter_llm_timeout,
                    iteration + 1,
                )
                from app.llm.base import ChatResponse

                return ChatResponse(
                    content="I'm sorry, the AI model took too long to respond. Please try again.",
                    model=model or "unknown",
                    usage=accumulated_usage,
                    # See the streaming branch above: a terminal timeout must
                    # be legible to the caller, not disguised as an answer.
                    stop_reason="timeout",
                )
            except asyncio.CancelledError as _exc:
                # Split from the generic ``except Exception`` below so
                # ``SystemExit`` / ``KeyboardInterrupt`` pass through
                # uninstrumented (CodeQL ``py/catch-base-exception``
                # convention applied across the loop's failure sites).
                _end_aborted_generation(_gen_ctx, _exc, _usage_sink, _merge_usage)
                _current_generation_reasoning = None
                raise
            except Exception as _exc:
                # Pass the exception object so the facade can render
                # ``type.__name__: msg`` — ``str(exc)`` is empty for
                # message-less errors and would lose the type context for
                # any exception class without a message.
                _end_aborted_generation(_gen_ctx, _exc, _usage_sink, _merge_usage)
                _current_generation_reasoning = None
                raise

            llm_system_prompt, llm_user_prompt = _build_llm_prompt_trace(messages)
            llm_response_content = _trim_trace_text(response.content or "", 80_000)
            await _emit(
                on_event,
                {
                    "status": "llm_step",
                    "iteration": iteration + 1,
                    "llm_has_tool_calls": bool(response.tool_calls),
                    "llm_tools": [tc.name for tc in response.tool_calls] if response.tool_calls else [],
                    "llm_model": response.model or model,
                    "llm_stop_reason": response.stop_reason,
                    "llm_usage": response.usage or {},
                    "llm_system_prompt": llm_system_prompt,
                    "llm_user_prompt": llm_user_prompt,
                    "llm_raw_response": llm_response_content,
                },
            )

            _ctx_chars = _transcript_chars(messages)
            _resp_len = len(response.content or "")
            _tool_names = (
                [tc.name for tc in response.tool_calls] if response.tool_calls else []
            )
            logger.info(
                "Agent loop iter %d: context=%d chars, response=%d chars, "
                "tool_calls=%s, has_thinking=%s",
                iteration + 1,
                _ctx_chars,
                _resp_len,
                _tool_names or "none (final answer)",
                bool(response.thinking),
            )
            if (
                response.tool_calls
                and _is_runaway(response.content or "", _answer_stream)
            ):
                # A runaway ends the run, tool calls or not: they are not run,
                # and the tool-free path below ends the answer on the notice.
                # Gemini replays its raw parts instead of the content, so they
                # go too; the trace above keeps the whole response.
                response.tool_calls = None
                response._provider_extra = None
                _tool_names = []

            # Stream a deterministic, LLM-independent per-iteration activity
            # signal to the caller (e.g. the autonomous orchestrator cockpit),
            # so its live view keeps moving even across the many iterations
            # where the model calls tools silently and narrates nothing.
            # ``had_error`` is best-effort: the current iteration's tools have
            # not executed yet at this point, so we report whether the PREVIOUS
            # iteration produced any tool error (the delta in the monotonic
            # ``tool_errors_observed`` tally since the last call).  Fully
            # guarded - a callback failure must never break the agent loop.
            if on_iteration_activity is not None:
                _iter_had_error = tool_errors_observed > _activity_err_seen
                _activity_err_seen = tool_errors_observed
                try:
                    await on_iteration_activity(
                        iteration + 1, list(_tool_names), _iter_had_error
                    )
                except Exception:
                    logger.debug(
                        "on_iteration_activity callback raised (non-fatal)",
                        exc_info=True,
                    )

            # Emit thinking/reasoning text.  When streaming-with-tools was
            # used, thinking text was already forwarded via the on_thinking
            # callback during the stream; here we only need to resolve the
            # held-back text now that we know whether tool calls followed.
            # Block-level emissions below are whole-paragraph units (one per
            # iteration), unlike the token deltas forwarded live by
            # ``_new_on_thinking``.  Frontends append ``thinking_text``
            # verbatim, so consecutive iterations' preambles would otherwise
            # concatenate without whitespace ("...data first.Let me pull...").
            # The leading blank line keeps each block readable; frontends
            # trim/collapse it at the start of the pane.
            if not _used_stream_tools:
                if on_event and response.thinking:
                    await _emit(
                        on_event,
                        {
                            "status": "thinking_text",
                            "content": f"\n\n{response.thinking}",
                        },
                    )
                elif on_event and response.tool_calls and response.content:
                    await _emit(
                        on_event,
                        {
                            "status": "thinking_text",
                            "content": f"\n\n{response.content}",
                        },
                    )
            elif response.tool_calls and on_event:
                if _answer_flushed:
                    if (
                        deliver_segments
                        and (response.content or "").strip()
                        # A max_tokens-truncated round is discarded and
                        # retried by the guard below — its narration is a
                        # half-finished checkpoint the retry regenerates,
                        # so it keeps the retract (same rationale as the
                        # timeout-retry retraction).
                        and response.stop_reason != "max_tokens"
                    ):
                        # The model wrote more than the hold-back window and
                        # THEN called tools — the text already streamed as a
                        # provisional answer the user has read.  Whatever the
                        # tool round is (a wait-family call after a partial
                        # wake, or a side-effect / lookup tool such as
                        # ``store_memory`` or ``search_knowledge``),
                        # text that was shown is never taken back: hand it to
                        # the consumer as a completed segment (persisted +
                        # finalized bubble), the tool activity shows below it
                        # and the post-tool continuation streams as the next
                        # bubble — the ChatGPT / Claude.ai contract.  It is
                        # NOT re-emitted as thinking_text: it is a real
                        # message now.  Retracting it into the reasoning pane
                        # made the answer visibly vanish, scroll through the
                        # reasoning window and come back merged at ``done``
                        # via the rescue.
                        await _emit(
                            on_event,
                            {
                                "status": "segment_complete",
                                "content": response.content,
                            },
                        )
                        # Nothing provisional is left on screen — the bubble
                        # is a persisted message now — so a later retract in
                        # this iteration (``_arm_answer_after_park``) must not
                        # fire against it.
                        _answer_flushed = False
                    else:
                        # Consumers without ``deliver_segments`` (public embed
                        # widget, REST, channels, assignments) and the
                        # max_tokens-truncated round: retract the provisional
                        # answer bubble and surface the text in the reasoning
                        # pane instead.
                        await _emit(on_event, {"status": "stream_retract"})
                        if response.content:
                            await _emit(
                                on_event,
                                {
                                    "status": "thinking_text",
                                    "content": f"\n\n{response.content}",
                                },
                            )
                elif _held_text.strip():
                    # Short pre-tool preamble — exactly what the hold-back
                    # window is for: it belongs in the reasoning pane.
                    await _emit(
                        on_event,
                        {"status": "thinking_text", "content": f"\n\n{_held_text}"},
                    )

            # Any tool-calling turn — even a truncated one (handled just below)
            # — is genuine progress, so reset the consecutive double-empty-round
            # counter here. Doing it BEFORE the truncation guard ensures a
            # truncated tool call (which ``continue``s) still resets the counter,
            # so it only ever reflects *consecutive* empty rounds.
            if response.tool_calls:
                consecutive_empty_responses = 0
                had_tool_calls_this_run = True

            # Guard: if the LLM hit max_tokens while generating tool-call JSON,
            # the calls are likely incomplete (e.g. missing required arguments).
            # Discard them and ask the LLM to retry more concisely instead of
            # executing broken calls and burning through tool-failure budget.
            if response.tool_calls and response.stop_reason == "max_tokens":
                consecutive_truncations += 1
                _tool_names = [tc.name for tc in response.tool_calls]
                # The current iteration was wasted (truncated) and we
                # ``continue`` immediately, so the system message we append
                # below is read by the LLM on iteration ``N + 1`` — by then
                # the strictly-remaining iterations are
                # ``max_iterations - (iteration + 1)``, not ``max_iterations -
                # iteration``.  Without the ``- 1`` the prompt overstates the
                # budget by one and the LLM keeps trying tools believing it
                # has more room than it actually does.
                #
                # NB: this differs from the wind-down nudge above, which
                # injects BEFORE the LLM call for the current iteration and
                # therefore correctly says "(N left ... including this one)".
                # The asymmetry is intentional — the two prompts run at
                # different points in the loop, so each one reports the
                # count that's accurate at the moment the LLM reads it.
                _remaining = max(0, max_iterations - iteration - 1)
                logger.warning(
                    "Agent loop iter %d: output truncated (max_tokens) with %d "
                    "pending tool call(s) %s, discarding and retrying "
                    "(%d/%d consecutive)",
                    iteration + 1,
                    len(response.tool_calls),
                    _tool_names,
                    consecutive_truncations,
                    _MAX_TRUNCATION_RETRIES,
                )
                # After repeated truncations, the model cannot fit the tool call
                # within its output budget.  Instead of aborting with a generic
                # error, fall through to the forced-final-answer backstop so the
                # user gets a synthesised summary of what was already gathered.
                if consecutive_truncations >= _MAX_TRUNCATION_RETRIES:
                    logger.warning(
                        "Agent loop: %d consecutive truncations — exiting loop "
                        "and falling through to forced final answer",
                        consecutive_truncations,
                    )
                    break
                await _emit_budget_warning(
                    on_event,
                    (
                        "⚠️ I hit the output budget while preparing the next tool call. "
                        "I will switch to a concise checkpoint/completion report unless "
                        "one critical tool call is still required."
                    ),
                )
                messages.append(
                    LLMMessage(
                        role="system",
                        content=_build_truncated_tool_call_retry(
                            remaining_iterations=_remaining
                        ),
                    )
                )
                continue
            else:
                # Reset on any successful (non-truncated) iteration
                consecutive_truncations = 0

            # If no tool calls → we're done
            if not response.tool_calls:
                # Retry once when the LLM returns empty content — this
                # commonly happens in long conversations where the context
                # fills most of the window and the LLM produces nothing.
                if not (response.content or "").strip():
                    _context_chars = _transcript_chars(messages)
                    logger.warning(
                        "Agent loop iter %d: LLM returned empty content with "
                        "no tool calls (context=%d chars) — retrying without tools",
                        iteration + 1,
                        _context_chars,
                    )
                    _prior_msg_count = len(messages)
                    messages.append(
                        LLMMessage(role="system", content=_EMPTY_RESPONSE_RETRY)
                    )
                    # An empty turn on a large context is the model signalling
                    # it lost focus on an oversized history. Rather than
                    # re-sending the same oversized prompt, distil
                    # the older turns into a focused summary (semantic
                    # compaction), keeping the task + recent turns verbatim;
                    # deterministic truncation/eviction is the fallback.
                    # ``_context_chars`` above intentionally keeps the
                    # pre-compaction size that produced the empty response.
                    _compaction_spent = False
                    await _compact_for_empty_recovery(
                        llm,
                        messages,
                        model=model,
                        max_session_chars=max_session_chars,
                        attempt=consecutive_empty_responses,
                        phase="empty-response retry",
                        cancel_event=cancel_event,
                        llm_timeout=_remaining_llm_timeout(),
                        on_event=on_event,
                        merge_usage=_merge_compaction_usage,
                        can_retry=_compaction_retry_affordable,
                        keep=file_recovery.is_pinned,
                    )
                    if (_compaction_spent or _call_sized()) and _call_refused_after_compaction():
                        max_iterations += 1
                        if _compaction_spent:
                            _iter_outcome["budget_recheck_after_compaction"] = True
                        continue
                    _empty_retry_ctx = _lf_start_empty_retry_gen(
                        _iter_ctx,
                        model=model,
                        delta_messages=[
                            {"role": "system", "content": _EMPTY_RESPONSE_RETRY}
                        ],
                        prior_message_count=_prior_msg_count,
                        metadata={"context_chars": _context_chars},
                    )
                    _retry_tok = _lf_set_retry_observer(
                        _lf_make_retry_observer(_empty_retry_ctx)
                    )
                    # Arm the reasoning buffer for the retry (see the streaming
                    # empty-response retry above for why this is needed).
                    _current_generation_reasoning = []
                    _empty_retry_sink = StreamUsageSink()
                    try:
                        try:
                            # Cap to the remaining wall-clock budget (P1.1).
                            _retry_llm_timeout = _remaining_llm_timeout()
                            if on_token:
                                await _emit(on_event, {"status": "streaming"})
                                _answer_stream = _AnswerStreamCut(
                                    on_token, enabled=_degenerate_guard_on
                                )
                                try:
                                    retry_resp = await _streaming_chat(
                                        llm,
                                        messages,
                                        _answer_stream.show,
                                        model=model,
                                        temperature=temperature,
                                        max_tokens=_call_max_tokens,
                                        on_thinking=(_new_on_thinking() if on_event else None),
                                        structured_output=structured_output,
                                        cancel_event=cancel_event,
                                        timeout=_retry_llm_timeout,
                                        on_stream_reset=_answer_stream.on_restart(
                                            _retract_answer_stream
                                        ),
                                        usage_sink=_empty_retry_sink,
                                        trace_gen_ctx=_empty_retry_ctx,
                                        answer_cut=_answer_stream,
                                        **_llm_extra,
                                    )
                                except _AnswerCutError as _cut_exc:
                                    # A runaway rescue: closed with the usage
                                    # it reported, answered with the text
                                    # shown, which the guard below ends.
                                    retry_resp = _end_cut_call(
                                        _empty_retry_ctx,
                                        _cut_exc,
                                        _empty_retry_sink,
                                        _merge_usage,
                                        model,
                                    )
                                    _empty_retry_ctx = None
                                else:
                                    _merge_usage(
                                        retry_resp.usage,
                                        served_model=retry_resp.model or model,
                                    )
                            else:
                                retry_resp = await _race_cancellation(
                                    llm.chat(
                                        messages=messages,
                                        tools=None,
                                        model=model,
                                        temperature=temperature,
                                        max_tokens=_call_max_tokens,
                                        **_llm_extra,
                                        **_structured_extra,
                                    ),
                                    cancel_event,
                                    timeout=_retry_llm_timeout,
                                )
                                _merge_usage(
                                    retry_resp.usage, served_model=retry_resp.model or model
                                )
                            _record_request_trace_metadata(_empty_retry_ctx, retry_resp)
                            retry_content = (retry_resp.content or "").strip()
                            if retry_content:
                                _lf_end_generation(
                                    _empty_retry_ctx,
                                    output=_build_generation_trace_output(
                                        retry_resp,
                                        captured_reasoning=(
                                            "".join(_current_generation_reasoning)
                                            if _current_generation_reasoning
                                            else None
                                        ),
                                    ),
                                    usage=retry_resp.usage,
                                    model=retry_resp.model or model,
                                    stop_reason=retry_resp.stop_reason,
                                )
                                # A truncated rescue is deliberately NOT
                                # continued here: the rescued response falls
                                # through to the shared
                                # ``stop_reason == "max_tokens"`` block below,
                                # which is the SINGLE continuation site for
                                # this path — a rescue-local continuation
                                # would let a response that is still
                                # truncated after ``_MAX_CONTINUATIONS`` be
                                # continued a second time by the shared
                                # block, doubling the continuation cap.
                                logger.info(
                                    "Empty-response retry succeeded: %d chars",
                                    len(retry_content),
                                )
                                # Do NOT return here: route the rescued text
                                # through the SHARED no-tool finalization path
                                # below (spawned-task park, steering
                                # resume-promise guard, final exit).
                                # An early return would let a steered run end
                                # on a rescued answer that promises to resume
                                # the pre-steer task, bypassing the guard.
                                # The final exit re-applies usage/outcome
                                # bookkeeping; only the retry marker must be
                                # recorded here (``update`` merges, so the
                                # terminal exit keeps it).
                                # Captured BEFORE ``response`` is rebound: the
                                # flagged trace step below reports the span's
                                # combined spend (an empty primary still
                                # consumes tokens).
                                _primary_trace_usage = response.usage or {}
                                response = retry_resp
                                # Marks the rescue so the STALL guard skips
                                # it (see the flag's init comment): a
                                # successful rescue was terminal before the
                                # route-through, and non-steered runs must
                                # keep that contract.
                                _rescued_no_tool_answer = True
                                # A content-bearing rescue breaks the empty
                                # streak. This used to be moot (the path
                                # returned); now that it falls through and a
                                # guard can re-prompt, a stale count from an
                                # earlier double-empty round would let the
                                # first post-nudge empty round hit the
                                # forced-answer backstop prematurely even
                                # though the empties are no longer
                                # consecutive.
                                consecutive_empty_responses = 0
                                _iter_outcome.update(
                                    {"empty_response_retry": True}
                                )
                                if on_token:
                                    # The rescue streamed its full text live
                                    # (plain streaming, no hold-back window),
                                    # so mark the answer as flushed: the
                                    # final exit must not re-stream it, and
                                    # a guard that re-prompts must retract
                                    # the already-visible bubble.
                                    _used_stream_tools = True
                                    _answer_flushed = True
                                # Report the rescue call's OWN payload to the
                                # eval-trace labeller. On the streamed path
                                # the ``streaming`` event above already closed
                                # this call's span with the PRIMARY (empty)
                                # payload from the iteration's ``llm_step`` —
                                # without this flagged step the rescue's
                                # prompt, usage, and output are traced
                                # nowhere. The labeller folds it into the
                                # provisionally-closed span (streamed) or
                                # supersedes the stashed primary payload
                                # (non-streamed). The emission itself is
                                # DEFERRED to after the shared max_tokens
                                # continuation block: a truncated rescue is
                                # continued there, and an eager step would
                                # freeze the partial raw response, the
                                # ``max_tokens`` stop reason, and undercounted
                                # usage in the span. The prompt is captured
                                # HERE, before the continuation appends its
                                # assistant/continue messages — it must
                                # reflect the rescue call's own input.
                                (
                                    _retry_sys_prompt,
                                    _retry_user_prompt,
                                ) = _build_llm_prompt_trace(messages)
                                # The span covers BOTH attempts (its duration
                                # is accumulated the same way), and an empty
                                # primary still consumes tokens — sum numeric
                                # usage counters instead of letting the
                                # rescue's figures replace the primary's.
                                _pending_rescue_usage = dict(_primary_trace_usage)
                                _sum_usage_into(
                                    _pending_rescue_usage, retry_resp.usage
                                )
                                _rescue_trace_pending = True
                            else:
                                # An empty retry still has reasoning worth
                                # keeping: it is the only record of what the
                                # model spent the call doing before returning
                                # nothing.
                                _lf_end_generation(
                                    _empty_retry_ctx,
                                    output=_build_generation_trace_output(
                                        retry_resp,
                                        captured_reasoning=(
                                            "".join(_current_generation_reasoning)
                                            if _current_generation_reasoning
                                            else None
                                        ),
                                    ),
                                    usage=retry_resp.usage,
                                    model=retry_resp.model or model,
                                    stop_reason=retry_resp.stop_reason,
                                    level="WARNING",
                                )
                                consecutive_empty_responses += 1
                                if (
                                    consecutive_empty_responses
                                    < _MAX_CONSECUTIVE_EMPTY_RESPONSES
                                ):
                                    # Don't bail to the forced-final-answer
                                    # backstop at iteration 2. Escalate
                                    # with a firmer instruction and keep
                                    # iterating — bounded by
                                    # _MAX_CONSECUTIVE_EMPTY_RESPONSES so a
                                    # systematic empty-output condition still
                                    # reaches the backstop promptly instead of
                                    # burning the budget.
                                    logger.warning(
                                        "Empty-response retry also returned empty "
                                        "(%d/%d consecutive) — escalating and "
                                        "continuing the loop",
                                        consecutive_empty_responses,
                                        _MAX_CONSECUTIVE_EMPTY_RESPONSES,
                                    )
                                    messages.append(
                                        LLMMessage(
                                            role="system",
                                            content=_EMPTY_RESPONSE_RETRY_ESCALATION,
                                        )
                                    )
                                    # Nothing here is unseen (the primary
                                    # request carried the whole transcript
                                    # and was recorded above).
                                    # ``_is_unseen_tool_result`` alone, on
                                    # purpose: an empty answer
                                    # on a big context is what this prune
                                    # exists to shrink, so the turn
                                    # protection gives way here as it does
                                    # past the window.
                                    _prune_session_context(
                                        messages,
                                        max_session_chars=max_session_chars,
                                        is_unseen=_is_unseen_tool_result,
                                    )
                                    continue
                                logger.warning(
                                    "Empty-response retry returned empty %d times "
                                    "in a row — falling through to forced final "
                                    "answer",
                                    consecutive_empty_responses,
                                )
                                # Break out of the loop so the forced-final-answer
                                # backstop can run instead of returning an empty
                                # response that would surface the generic
                                # fallback.
                                break
                        except TimeoutError as _to_exc:
                            # Pass the exception object so the facade
                            # renders it as ``"TimeoutError: ..."`` —
                            # matches the convention used by the
                            # streaming-LLM and iteration-span sites
                            # in this file.
                            _end_aborted_generation(
                                _empty_retry_ctx, _to_exc, _empty_retry_sink, _merge_usage
                            )
                            logger.error(
                                "Empty-response retry timed out after %.0fs — "
                                "falling through to forced final answer",
                                _retry_llm_timeout,
                            )
                            break
                        except asyncio.CancelledError as _empty_retry_exc:
                            # Cancellation mid-rescue must record on
                            # Langfuse BEFORE propagating — otherwise
                            # the ``attempt:empty_response_retry``
                            # generation stays open and the trace
                            # looks like a clean success.  Pass the
                            # exception object so the facade preserves
                            # the type (``str(CancelledError())`` is
                            # empty), then re-raise so the asyncio
                            # scheduler actually sees the cancellation.
                            _end_aborted_generation(
                                _empty_retry_ctx,
                                _empty_retry_exc,
                                _empty_retry_sink,
                                _merge_usage,
                            )
                            raise
                        except _CancelledMidCallError as _mc_exc:
                            # User cancel during the empty-response rescue —
                            # produce the exact outcome of the iteration-start
                            # cancellation check instead of falling through
                            # to the forced-final-answer backstop.
                            _end_aborted_generation(
                                _empty_retry_ctx, _mc_exc, _empty_retry_sink, _merge_usage
                            )
                            _iter_error = _mc_exc
                            _iter_outcome.update(
                                {
                                    "terminal": True,
                                    "stop_reason": "cancelled",
                                    "error_kind": "CancelledMidCall",
                                }
                            )
                            logger.info(
                                "Agent loop cancelled during empty-response "
                                "retry at iteration %d",
                                iteration + 1,
                            )
                            return _cancelled_chat_response(model, accumulated_usage)
                        except Exception as _empty_retry_exc:
                            # Ordinary rescue failure: record the error
                            # and break out of the empty-content rescue
                            # path so the forced-final-answer backstop
                            # can take over.
                            _end_aborted_generation(
                                _empty_retry_ctx,
                                _empty_retry_exc,
                                _empty_retry_sink,
                                _merge_usage,
                            )
                            logger.exception(
                                "Empty-response retry failed — "
                                "falling through to forced final answer"
                            )
                            break
                    finally:
                        _lf_reset_retry_observer(_retry_tok)
                        _current_generation_reasoning = None

                # A runaway is ended by the degenerate-repetition guard
                # below, never continued.
                if (
                    response.stop_reason == "max_tokens"
                    and (response.content or "").strip()
                    and not _is_runaway(response.content or "", _answer_stream)
                ):
                    # When stream-with-tools held the truncated front half
                    # back (it never exceeded the hold-back window), replay
                    # it through the answer channel BEFORE the continuation
                    # streams the rest — keeping the visible answer
                    # contiguous.  If it was already flushed live, nothing
                    # to replay.
                    if _used_stream_tools and not _answer_flushed and on_token:
                        if (response.content or "").strip():
                            await _emit(on_event, {"status": "streaming"})
                            await _stream_text_chunked(
                                _answer_stream.show if _answer_stream else on_token,
                                response.content or "",
                            )
                        _answer_flushed = True
                    # The continuation streams through the same stream cut and
                    # stops once the answer turns into a runaway (``stop``).
                    #
                    # ``_continue_truncated_response`` deliberately strips
                    # ``structured_output`` from continuation calls (a
                    # grammar-constrained continuation produces a fresh,
                    # complete JSON document that cannot be stitched onto
                    # the truncated text), so only
                    # ``_llm_extra`` is forwarded here.
                    #
                    # When a rescue trace is pending, the continuation spend
                    # must also fold into the deferred flagged ``llm_step``'s
                    # usage: the continued response's own ``usage`` field
                    # keeps only the INITIAL call's figures (continuation
                    # usage flows through ``merge_usage`` alone), so without
                    # this wrapper the span would undercount the very calls
                    # that produced its final text.
                    _merge_for_continuation = _merge_usage
                    if _rescue_trace_pending:
                        # Default-bound so the wrapper captures THIS
                        # iteration's dict (B023): the continuation calls it
                        # before the next iteration rebinds the name.
                        def _merge_for_continuation(
                            u: dict,
                            served_model: str | None = None,
                            _dst: dict = _pending_rescue_usage,
                        ) -> None:
                            _merge_usage(u, served_model=served_model)
                            _sum_usage_into(_dst, u)

                    try:
                        response = await _continue_truncated_response(
                            llm,
                            messages,
                            response,
                            on_token=_answer_stream.show if _answer_stream else on_token,
                            on_event=on_event,
                            model=model,
                            temperature=temperature,
                            max_tokens=_call_max_tokens,
                            max_session_chars=max_session_chars,
                            llm_extra=_llm_extra,
                            merge_usage=_merge_for_continuation,
                            stop=functools.partial(_is_runaway, stream=_answer_stream),
                            new_on_thinking=_new_on_thinking if on_event else None,
                            parent_iter_ctx=_iter_ctx,
                            cancel_event=cancel_event,
                            llm_timeout=_remaining_llm_timeout(),
                            next_max_tokens=_reprice_call if _call_sized() else None,
                            answer_cut=_answer_stream,
                        )
                    except _AnswerCutError:
                        # The stream cut stopped a continuation (the helper
                        # closed it with its usage): the guard below ends the
                        # answer on the text shown.
                        response.stop_reason = "end_turn"
                    except _CancelledMidCallError as _mc_exc:
                        # User cancel during the max_tokens continuation —
                        # produce the exact outcome of the iteration-start
                        # cancellation check (the continuation generation was
                        # already closed inside the helper).
                        _iter_error = _mc_exc
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "cancelled",
                                "error_kind": "CancelledMidCall",
                            }
                        )
                        logger.info(
                            "Agent loop cancelled during max_tokens "
                            "continuation at iteration %d",
                            iteration + 1,
                        )
                        return _cancelled_chat_response(model, accumulated_usage)
                    await _say_budget_cut(response)

                _degenerate_cut = _is_runaway(response.content or "", _answer_stream)

                # Deferred rescue trace: emitted AFTER the continuation block
                # so a truncated rescue's span reflects the post-continuation
                # state — final stitched text, final stop reason, and the
                # summed usage of the empty primary attempt, the rescue call,
                # and every continuation. The non-truncated path skips the
                # block above entirely, so this is the same single emission
                # with the same payload as before the deferral.
                if _rescue_trace_pending:
                    _rescue_trace_pending = False
                    await _emit(
                        on_event,
                        {
                            "status": "llm_step",
                            "iteration": iteration + 1,
                            "llm_has_tool_calls": False,
                            "llm_tools": [],
                            "llm_model": response.model or model,
                            "llm_stop_reason": response.stop_reason,
                            "llm_usage": _pending_rescue_usage,
                            "llm_system_prompt": _retry_sys_prompt,
                            "llm_user_prompt": _retry_user_prompt,
                            "llm_raw_response": _trim_trace_text(
                                response.content or "", 80_000
                            ),
                            "empty_response_retry": True,
                        },
                    )

                # ── Degenerate-repetition guard: end the run on a runaway ──
                # Shown text is never taken back (invariant 14), so the notice
                # follows it; an answer not shown yet is replaced by it. Every
                # later guard and the implicit park leave the answer alone.
                if _degenerate_cut:
                    logger.warning(
                        "Agent loop iter %d: tool-free answer is a degenerate "
                        "repetition loop (%d chars) — ending it on a bounded "
                        "diagnostic instead of surfacing the runaway text",
                        iteration + 1,
                        len(response.content or ""),
                    )
                    await _end_runaway(
                        response,
                        _answer_stream,
                        on_token,
                        shown=bool(on_token) and _answer_flushed,
                    )

                # ── Park until spawned tasks in this turn are terminal ──
                # A tool-free "I launched work, waiting" answer used to end
                # the turn while the banner still showed running tasks.
                # Implicit wait (or a wait-budget hand-off) keeps the parent
                # in the loop. No extra system-prompt text.
                _live_spawned: list[str] = []
                _spawn_statuses: dict[str, str] = {}
                _spawned_ids = collect_spawned_task_ids(messages)
                if _spawned_ids and wait_condition is not None:
                    try:
                        _all_done, _spawn_statuses = await wait_condition(
                            _spawned_ids
                        )
                    except Exception:
                        logger.debug(
                            "Agent loop: could not read spawned-task statuses",
                            exc_info=True,
                        )
                        _all_done, _spawn_statuses = False, {}
                    if not _all_done:
                        _live_spawned = [
                            tid
                            for tid in _spawned_ids
                            if _spawn_statuses.get(tid, "unknown")
                            not in _TASK_TERMINAL_STATUSES
                        ]
                elif _spawned_ids:
                    _live_spawned = list(_spawned_ids)
                # A final answer while every open child is still ``pending``
                # (queued, never started) parks once, as before: a child is
                # ``pending`` for the moments before a worker picks it up, and
                # a delegating run stays open until its child finishes. When
                # the same children are still ``pending`` after that whole
                # park, the run ends on its answer instead of parking again:
                # a child that never started may never start within this run's
                # budget. They are left to run on their own and named on the
                # response (``pending_task_ids``) and the iteration outcome.
                _never_started = never_started_task_ids(_live_spawned, _spawn_statuses, response.content or "")
                _first_park_pending: list[str] = []
                if _never_started and not set(_never_started) <= _parked_pending_ids:
                    _first_park_pending = _never_started
                    _never_started = []
                if _never_started:
                    logger.info(
                        "Agent loop iter %d: final answer with %d never-started "
                        "background task(s); ending the run instead of parking",
                        iteration + 1,
                        len(_never_started),
                    )
                    response.pending_task_ids = list(_never_started)
                    _iter_outcome["detached_task_ids"] = list(_never_started)
                    _live_spawned = []
                if (
                    _live_spawned
                    and _wait_definition is not None
                    and wait_condition is not None
                    and iteration < strip_tools_at
                    and not token_budget_exceeded
                    and not tool_failure_brake
                    and not _degenerate_cut
                ):
                    if _wait_budget.remaining_total() < MIN_GRANTED_WAIT_SECONDS:
                        _park_content = await build_waiting_on_tasks_message(
                            messages,
                            _live_spawned,
                            wait_condition,
                            lookup_timeout=_label_lookup_budget(wall_clock_deadline),
                        )
                        if on_token:
                            if _answer_flushed:
                                if deliver_segments and (
                                    response.content or ""
                                ).strip():
                                    # The flushed report becomes a completed
                                    # segment; the park message then
                                    # streams as this turn's own final
                                    # content — the segment never duplicates
                                    # it (the park text is generated, not
                                    # part of the model's report).
                                    await _emit(
                                        on_event,
                                        {
                                            "status": "segment_complete",
                                            "content": response.content,
                                        },
                                    )
                                else:
                                    await _emit(
                                        on_event, {"status": "stream_retract"}
                                    )
                            await _emit(on_event, {"status": "streaming"})
                            await _stream_text_chunked(on_token, _park_content)
                        from app.llm.base import ChatResponse

                        return ChatResponse(
                            content=_park_content,
                            model=model or "unknown",
                            usage=accumulated_usage,
                            stop_reason="waiting_on_tasks",
                            pending_task_ids=_live_spawned,
                        )
                    if on_token and _answer_flushed:
                        if deliver_segments and (response.content or "").strip():
                            # Implicit park after a flushed report:
                            # the model answered text-only ("here is what
                            # task 1 found, still waiting on the others")
                            # while spawned tasks are live.  The report the
                            # user already read is delivered as a completed
                            # segment; the narration also stays on the
                            # injected implicit-wait assistant message below
                            # (transcript continuity is unchanged).
                            await _emit(
                                on_event,
                                {
                                    "status": "segment_complete",
                                    "content": response.content,
                                },
                            )
                        else:
                            await _emit(on_event, {"status": "stream_retract"})
                    _implicit_id = f"implicit-wait-{iteration + 1}"
                    _implicit_args = {
                        "duration": format_wait_duration(
                            int(_wait_policy.per_call_seconds)
                        ),
                        "until": ",".join(_live_spawned),
                        "reason": "open background tasks",
                    }
                    # Keep the model's own narration in the transcript (like
                    # the stall guard does) — the next round should see what
                    # it said, not an unexplained bare wait call.
                    messages.append(
                        LLMMessage(
                            role="assistant",
                            content=response.content or "",
                            tool_calls=[
                                ToolCall(
                                    id=_implicit_id,
                                    name=WAIT_TOOL_NAME,
                                    arguments=_implicit_args,
                                )
                            ],
                        )
                    )

                    _implicit_card = _WaitCard(
                        wait_condition, _live_spawned, on_event, WAIT_TOOL_NAME
                    )

                    async def _announce_implicit_wait(
                        grant, _args=_implicit_args, _iter=iteration, _card=_implicit_card
                    ):
                        """Tell the UI this park is a deliberate pause.

                        The explicit ``wait`` tool announces its grant via
                        ``wait_start`` so the chat renders the "Waiting —
                        {reason}" indicator with a countdown and skip button.
                        Without this the implicit park is invisible: the last
                        status the UI saw was the streaming/segment delivery,
                        so the indicator falls back to a generic "Thinking…"
                        for the whole pause.  Same payload as the explicit
                        announcer — the frontend needs no new event type.
                        """
                        await _emit(
                            on_event,
                            {
                                "status": "wait_start",
                                "tool": WAIT_TOOL_NAME,
                                "iteration": _iter + 1,
                                "seconds": round(grant.granted, 1),
                                "requested_seconds": grant.requested,
                                "clamped": grant.clamped,
                                "reason": _args.get("reason"),
                                "until": _args.get("until"),
                                "items": await _card.items(),
                                "remaining_wait_seconds": round(
                                    _wait_budget.remaining_total(), 1
                                ),
                            },
                        )

                    try:
                        _implicit_result = await _race_cancellation(
                            execute_wait(
                                _implicit_args,
                                budget=_wait_budget,
                                remaining_wall_clock=(
                                    wall_clock_deadline - time.monotonic()
                                    if wall_clock_deadline is not None
                                    else None
                                ),
                                skip_event=wait_skip_event,
                                condition=wait_condition,
                                on_grant=_announce_implicit_wait,
                                pending_input=_steering_pending,
                                on_status=_implicit_card.report,
                                stopping=_wait_stopping,
                            ),
                            cancel_event,
                            timeout=WAIT_TOOL_TIMEOUT,
                        )
                    except _CancelledMidCallError:
                        return _cancelled_chat_response(model, accumulated_usage)
                    except TimeoutError:
                        _implicit_result = _WAIT_TIMEOUT_RESULT
                    if _implicit_result.paused:
                        _wait_paused_total += _implicit_result.elapsed
                        _waive_wait_round(WAIT_TOOL_NAME)
                        _forget_reads_after_pause()
                        # Only a park that really paused counts as the one
                        # chance a never-started child gets to start.
                        _parked_pending_ids.update(_first_park_pending)
                    # Suppress next-round nudges only when the park REALLY
                    # paused — a refused park (steered, already-done) spent no
                    # time, so budget / wall-clock warnings must still fire.
                    _last_round_was_wait = _implicit_result.paused
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_implicit_result.message,
                            tool_call_id=_implicit_id,
                            name=WAIT_TOOL_NAME,
                        )
                    )
                    if _implicit_result.reason == WAIT_REASON_HANDOFF:
                        _park_content = await build_waiting_on_tasks_message(
                            messages,
                            _live_spawned,
                            wait_condition,
                            lookup_timeout=_label_lookup_budget(wall_clock_deadline),
                        )
                        if on_token:
                            await _emit(on_event, {"status": "streaming"})
                            await _stream_text_chunked(on_token, _park_content)
                        from app.llm.base import ChatResponse

                        return ChatResponse(
                            content=_park_content,
                            model=model or "unknown",
                            usage=accumulated_usage,
                            stop_reason="waiting_on_tasks",
                            pending_task_ids=_live_spawned,
                        )
                    continue

                # ── Leaked tool-call guard: markup emitted as text ─────────
                # The provider adapter flags a tool-free answer whose text was a
                # tool call it could NOT parse back into a real call (a parseable
                # leak already arrived as an ordinary tool_use and never reaches
                # here). Re-prompt the model to make the call for real. Bounded
                # by ``_MAX_LEAKED_TOOL_CALL_RECOVERIES`` and gated like the
                # stall guard so a genuinely finished / blocked turn still ends.
                if (
                    executor is not None
                    and iter_tools
                    and iteration + 1 < strip_tools_at
                    and not token_budget_exceeded
                    and not tool_failure_brake
                    and had_tool_calls_this_run
                    and leaked_tool_recoveries < _MAX_LEAKED_TOOL_CALL_RECOVERIES
                    and not _rescued_no_tool_answer
                    # A runaway the degenerate-repetition guard above cut ends
                    # the run: it is never re-prompted.
                    and not _degenerate_cut
                    and response.leaked_tool_markup_unrecovered
                ):
                    leaked_tool_recoveries += 1
                    logger.warning(
                        "Agent loop iter %d: tool-free answer contains tool-call "
                        "markup emitted as text (leaked-tool-call recovery %d/%d) "
                        "— re-prompting to invoke the tool for real",
                        iteration + 1,
                        leaked_tool_recoveries,
                        _MAX_LEAKED_TOOL_CALL_RECOVERIES,
                    )
                    if on_token and _answer_flushed:
                        if deliver_segments and (response.content or "").strip():
                            await _emit(
                                on_event,
                                {
                                    "status": "segment_complete",
                                    "content": response.content,
                                },
                            )
                        else:
                            await _emit(on_event, {"status": "stream_retract"})
                    await _emit(
                        on_event,
                        {
                            "status": "composing",
                            "reason": "leaked_tool_call_continuation",
                        },
                    )
                    messages.append(
                        LLMMessage(role="assistant", content=response.content or "")
                    )
                    messages.append(
                        LLMMessage(
                            role="system", content=_LEAKED_TOOL_CALL_NUDGE
                        )
                    )
                    continue

                # ── Stall guard: narration without execution ──────────────
                # The model produced a tool-free "final" answer that actually
                # DESCRIBES work it was about to do (mid-task, tools still
                # available, budget remaining) but performed none of it. Rather
                # than surfacing the half-finished checkpoint to the user (who
                # then has to say "continue"), re-prompt it to act. Bounded by
                # ``_MAX_STALL_CONTINUATIONS`` and gated so a genuinely finished,
                # blocked, or question-asking turn always ends normally.
                if (
                    executor is not None
                    and iter_tools
                    and iteration < strip_tools_at
                    and not token_budget_exceeded
                    and not tool_failure_brake
                    and had_tool_calls_this_run
                    and stall_continuations < _MAX_STALL_CONTINUATIONS
                    # An empty-response rescue is exempt: it was a terminal
                    # answer before the guard route-through, and nudging a
                    # context that just returned empty risks re-triggering
                    # the failure the rescue recovered from. Only the
                    # steering resume-promise guard below applies to it.
                    and not _rescued_no_tool_answer
                    and not _degenerate_cut
                    and _looks_like_unfinished_action(
                        response.content or "",
                        open_task_ids=_live_spawned or None,
                    )
                ):
                    stall_continuations += 1
                    logger.warning(
                        "Agent loop iter %d: tool-free answer narrates an "
                        "unperformed action (stall nudge %d/%d) — re-prompting "
                        "to execute instead of returning the checkpoint",
                        iteration + 1,
                        stall_continuations,
                        _MAX_STALL_CONTINUATIONS,
                    )
                    # The narration already streamed to the user.  With
                    # ``deliver_segments`` it stays on screen as a completed
                    # segment (shown text is never taken back — the
                    # re-prompt CONTINUES the turn, it does not regenerate
                    # this text); the transcript copy below is still skipped
                    # by ``select_best_result_summary`` and the delivered
                    # segment is excluded from the chat rescue, so it is never
                    # duplicated into the final reply.  Without the opt-in the
                    # provisional bubble is retracted (reuses the
                    # stream-with-tools retract path); accumulated reasoning
                    # text is preserved either way.
                    if on_token and _answer_flushed:
                        if deliver_segments and (response.content or "").strip():
                            await _emit(
                                on_event,
                                {
                                    "status": "segment_complete",
                                    "content": response.content,
                                },
                            )
                        else:
                            await _emit(on_event, {"status": "stream_retract"})
                    # ``reason`` distinguishes this re-prompt from the
                    # budget-driven ``composing`` emissions (Stage-2 tool strip,
                    # forced final answer): the loop is NOT winding down here —
                    # it continues with full budget — so truncation latches
                    # (web chat, platform proxy) must NOT flag the turn as
                    # "turn limit reached" on this event.
                    await _emit(
                        on_event,
                        {"status": "composing", "reason": "stall_continuation"},
                    )
                    # Keep the narration in the WORKING transcript so the next
                    # turn has continuity, then inject the execute-now
                    # instruction and loop. NOTE: ``messages`` is mutated
                    # in-place and IS surfaced downstream - the assignment engine
                    # persists the full transcript to ``AssignmentRun.messages``
                    # (run-detail view) and ``select_best_result_summary``
                    # inspects it - so this narration is recorded as a normal
                    # recovery step (like the wind-down nudge). It is
                    # deliberately excluded from the user-facing result summary
                    # (see ``select_best_result_summary``) so the retracted
                    # checkpoint never becomes the deliverable.
                    messages.append(
                        LLMMessage(role="assistant", content=response.content or "")
                    )
                    messages.append(
                        LLMMessage(role="system", content=_STALL_CONTINUATION_NUDGE)
                    )
                    continue

                # ── Steering resume-promise guard ─────────────────────────
                # The run was steered mid-turn and the model now ends the
                # turn with a text-only answer whose CLOSING promises to
                # resume the pre-steer task ("Resuming the ComCyberMI draft
                # now..."). Exiting here would silently abandon that task —
                # the promise becomes the last message and nothing happens.
                # While tools are still offered and budget remains, re-prompt
                # ONCE: do the remaining work now (tool calls) or close
                # without promising imminent work. ``resume_promise_nudged``
                # caps this at a single extra LLM round per run — a second
                # text-only answer is final even if it still promises.
                if (
                    steered_this_run
                    and not resume_promise_nudged
                    and executor is not None
                    and iter_tools
                    # The CONTINUATION round must also be tool-enabled: at
                    # the last tool-enabled iteration the nudge would ask
                    # for tool calls the next (tool-stripped) iteration
                    # cannot make.
                    and iteration + 1 < strip_tools_at
                    and not token_budget_exceeded
                    and not tool_failure_brake
                    and not _degenerate_cut
                    and _ends_with_resume_promise(response.content or "")
                ):
                    resume_promise_nudged = True
                    logger.warning(
                        "Agent loop iter %d: steered run ended on a promise "
                        "to resume earlier work with no tool calls — "
                        "re-prompting once to execute or close cleanly",
                        iteration + 1,
                    )
                    # The promise already streamed to the user.  With
                    # ``deliver_segments`` it stays on screen as a completed
                    # segment (the single re-prompt continues the turn
                    # and does not regenerate this text, so nothing shown is
                    # taken back).  Without the opt-in, retract the
                    # provisional answer bubble (reuses the flushed-then-tools
                    # retract path) and surface the text in the reasoning
                    # pane instead, so the retracted closing is never lost.
                    if on_token and _answer_flushed:
                        if deliver_segments and (response.content or "").strip():
                            await _emit(
                                on_event,
                                {
                                    "status": "segment_complete",
                                    "content": response.content,
                                },
                            )
                        else:
                            await _emit(on_event, {"status": "stream_retract"})
                            if response.content:
                                await _emit(
                                    on_event,
                                    {
                                        "status": "thinking_text",
                                        "content": f"\n\n{response.content}",
                                    },
                                )
                    # The loop is NOT winding down — it continues with full
                    # budget — so truncation latches must not flag the turn
                    # as "turn limit reached" on this event (see the stall
                    # guard's ``stall_continuation`` reason).
                    await _emit(
                        on_event,
                        {
                            "status": "composing",
                            "reason": "resume_promise_continuation",
                        },
                    )
                    # Keep the promise in the working transcript (provider
                    # pairing stays valid: plain assistant text, no tool
                    # calls), then inject the one-shot nudge and loop.
                    messages.append(
                        LLMMessage(role="assistant", content=response.content or "")
                    )
                    messages.append(
                        LLMMessage(role="system", content=_RESUME_PROMISE_NUDGE)
                    )
                    continue

                # ── Friction finalisation guard (#8528) ─────────────────────
                # The run recorded frictions (services/frictions.py) and left
                # some with no disposition: show ONE system line naming them
                # and give one more round to call dispose_friction. Once per
                # run (``frictions_nagged``); only when the tool is offered and
                # the next round still has tools; a run with none, or no bound
                # run scope, ends exactly as before. What stays undisposed is
                # counted on the run by ``settle_run_frictions``.
                if (
                    not frictions_nagged
                    and not _degenerate_cut
                    and executor is not None
                    and iter_tools
                    and any(getattr(t, "name", "") == "dispose_friction" for t in iter_tools)
                    and iteration + 1 < strip_tools_at
                    and not token_budget_exceeded
                    and not tool_failure_brake
                ):
                    frictions_nagged = True
                    from app.services.frictions import finalisation_nag

                    _friction_line = await finalisation_nag()
                    if _friction_line:
                        logger.info(
                            "Agent loop iter %d: run ends with undisposed frictions; one disposal round",
                            iteration + 1,
                        )
                        if on_token and _answer_flushed:
                            if deliver_segments and (response.content or "").strip():
                                await _emit(
                                    on_event,
                                    {"status": "segment_complete", "content": response.content},
                                )
                            else:
                                await _emit(on_event, {"status": "stream_retract"})
                        await _emit(
                            on_event,
                            {"status": "composing", "reason": "frictions_continuation"},
                        )
                        messages.append(
                            LLMMessage(role="assistant", content=response.content or "")
                        )
                        messages.append(LLMMessage(role="system", content=_friction_line))
                        continue

                logger.debug("Agent loop completed after %d iteration(s)", iteration + 1)
                response.usage = accumulated_usage
                _with_limit_stop(
                    response,
                    ITERATION_LIMIT_STOP_REASON if _iteration_limit_strip else None,
                )
                if on_token and response.content and (
                    not _used_stream_tools or not _answer_flushed
                ):
                    # The answer never reached the live stream: either the
                    # call wasn't streamed at all, or it was short enough to
                    # stay inside the hold-back window.  Stream it now.
                    await _emit(on_event, {"status": "streaming"})
                    await _stream_text_chunked(on_token, response.content)
                _iter_outcome.update(
                    {
                        "terminal": True,
                        "stop_reason": response.stop_reason,
                        "content_preview": _trim_trace_text(response.content or "", 500),
                    }
                )
                return response

            # Append the assistant message (with its tool_calls) to the conversation.
            # Carry _provider_extra (e.g. Gemini raw parts with thought_signature)
            # so the provider can replay them on the next turn.
            assistant_msg = LLMMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=response.tool_calls,
            )
            if response._provider_extra:
                assistant_msg._provider_extra = response._provider_extra
            messages.append(assistant_msg)
            # Checkpointed before the calls run: an attempt that ends while they
            # do leaves them named as in flight, so the turn that resumes it
            # knows a write may have landed (``turn_checkpoint.in_flight_calls``).
            _offer_checkpoint()

            # Mark the iteration outcome with the tool dispatch so the
            # iteration span in Langfuse shows what was called.
            _iter_outcome.update(
                {
                    "terminal": False,
                    "stop_reason": response.stop_reason,
                    "tool_calls": [tc.name for tc in response.tool_calls],
                }
            )

            # Execute tool calls — parallel when multiple, sequential for single
            tool_calls = response.tool_calls

            # ── Progressive tool disclosure: intercept ``load_tools`` ─────
            # ``load_tools`` is a meta-tool handled entirely inside the loop:
            # it reveals gated tool groups for subsequent iterations instead of
            # routing through the composite executor.  We append a tool-result
            # for each meta-call (providers require every tool_call id to have a
            # matching result) and drop them from the dispatch list.  If only
            # meta-calls were requested, ``continue`` so the next LLM call sees
            # the freshly-revealed tools.
            if tool_gate is not None:
                # A group whose tools are being called is in active use:
                # refresh its recency AND protect it from every ``load_tools``
                # of this response, so a reveal that has to make room under
                # the provider's tool budget unloads something else.
                # This happens BEFORE the reveals below on purpose: a mixed
                # response can call into group A and load group C in the same
                # batch, and touching A only at dispatch time would come too
                # late — the reveal would already have evicted it as the least
                # recently used group, and an unloaded group cannot be touched
                # back into the visible list.  Recency alone is not enough
                # either when the response carries SEVERAL meta-calls: the
                # first reveal makes its own group the newest, so the second
                # would evict A — hence one protection set for the whole
                # batch, which each reveal also extends with what it loaded.
                # The names go to the gate EXACTLY as the model sent them: the
                # gate was built from ``effective_tools``, i.e. the sanitised
                # (and collision-suffixed) definitions, so it lives in the
                # LLM-visible name space.  Reverse-mapping through
                # ``_tool_name_map`` here would break that identity — with
                # originals ``a.b`` / ``a/b`` exposed as ``a_b`` / ``a_b_2``,
                # a call to ``a_b_2`` would become ``a/b``, token-match ``a_b``
                # and touch the WRONG group.  Only executor dispatch needs the
                # original name (``_exec_call`` below), and it gets there by
                # canonicalising in THIS name space first (``_ExecutorNameMap``),
                # so the gate and the executor always agree on which tool a
                # call denotes.
                _active_tool_names = [
                    tc.name for tc in tool_calls if tc.name != LOAD_TOOLS_TOOL_NAME
                ]
                tool_gate.touch(_active_tool_names)
                _batch_protect: set[str] = set(
                    tool_gate.groups_in_use(_active_tool_names)
                )
            if tool_gate is not None and any(
                tc.name == LOAD_TOOLS_TOOL_NAME for tc in tool_calls
            ):
                meta_calls = [
                    tc for tc in tool_calls if tc.name == LOAD_TOOLS_TOOL_NAME
                ]
                for meta_tc in meta_calls:
                    requested = _as_str_list(
                        meta_tc.arguments.get("groups") if meta_tc.arguments else None
                    )
                    # Tool names are passed through in the LLM-visible form the
                    # model saw them in (the gate indexes the sanitised
                    # definitions — see the touch above); the partial-reveal
                    # listing the model copies them from is in that same space.
                    requested_tools = _as_str_list(
                        meta_tc.arguments.get("tools") if meta_tc.arguments else None
                    )
                    _search_raw = meta_tc.arguments.get("search") if meta_tc.arguments else None
                    _search_query = _search_raw.strip() if isinstance(_search_raw, str) else ""
                    await _emit(
                        on_event,
                        {
                            "status": "tool_start",
                            "tools": [LOAD_TOOLS_TOOL_NAME],
                            "iteration": iteration + 1,
                        },
                    )
                    _reveal = tool_gate.reveal(
                        requested, requested_tools, protect=_batch_protect
                    )
                    # ``search`` is read-only and composes with the loads
                    # above: scoped to the requested groups when any were
                    # given.  A search-only call gets ONLY the search text —
                    # the "nothing was loaded" line would describe a request
                    # the model never made.
                    if _search_query:
                        _search = tool_gate.search(_search_query, requested)
                        _search_text = tool_gate.describe_search(_search)
                        meta_result = (
                            _search_text
                            if not requested and not requested_tools
                            else f"{tool_gate.describe_reveal(_reveal)}\n{_search_text}"
                        )
                    else:
                        meta_result = tool_gate.describe_reveal(_reveal)
                    # Only mention the noteworthy parts: unknown tokens when the
                    # model actually sent one, evictions / partial reveals when
                    # the provider's tool budget bound — logging
                    # "(unknown: none)" on every healthy reveal is just noise.
                    _reveal_notes = [
                        f"partial: {_reveal.partial}" if _reveal.partial else "",
                        f"at capacity: {_reveal.at_capacity}"
                        if _reveal.at_capacity
                        else "",
                        f"by name: {_reveal.pinned}" if _reveal.pinned else "",
                        f"unloaded: {_reveal.evicted}" if _reveal.evicted else "",
                        f"trimmed: {_reveal.trimmed}" if _reveal.trimmed else "",
                        f"displaced: {_reveal.displaced}" if _reveal.displaced else "",
                        f"no room: {_reveal.no_room}" if _reveal.no_room else "",
                        f"unknown group(s): {_reveal.unknown}" if _reveal.unknown else "",
                        f"unknown tool(s): {_reveal.unknown_tools}"
                        if _reveal.unknown_tools
                        else "",
                        f"search: {_search_query!r}" if _search_query else "",
                    ]
                    # The first load the tool budget cuts down is a warning, once per run:
                    # on a small-cap provider most loads after it are cut too.
                    _budget_warn = not _tool_budget_warned and any(
                        (_reveal.partial, _reveal.evicted, _reveal.trimmed, _reveal.displaced, _reveal.no_room)
                    )
                    _tool_budget_warned = _tool_budget_warned or _budget_warn
                    logger.log(
                        logging.WARNING if _budget_warn else logging.INFO,
                        "load_tools revealed group(s): %s%s%s",
                        _reveal.revealed or "none",
                        "".join(f"; {note}" for note in _reveal_notes if note),
                        f" (tool budget of {getattr(llm, 'provider_name', 'unknown')}, model {model}: "
                        f"{provider_max_tools} tools per request)"
                        if _budget_warn
                        else "",
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=meta_result,
                            tool_call_id=meta_tc.id,
                            name=meta_tc.name,
                        )
                    )
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": LOAD_TOOLS_TOOL_NAME,
                            "success": True,
                            "tool_input": _serialize_tool_arguments(meta_tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(
                                meta_tc.arguments
                            ),
                            "tool_output": _trim_trace_text(meta_result, 30_000),
                            "tool_output_full": meta_result,
                        },
                    )
                # Keep the root trace card's ``tools`` in lock-step with what
                # the model has now been offered: the freshly-revealed groups
                # are part of ``visible_tools()`` from here on.  Under a binding provider cap the set can also
                # shrink (a reveal may unload the least recently used group),
                # so this is "what is offered now", not a monotonic union.
                _lf_update_trace_tools(_root_trace_ctx, tool_gate.visible_tools())
                tool_calls = [
                    tc for tc in tool_calls if tc.name != LOAD_TOOLS_TOOL_NAME
                ]
                if not tool_calls:
                    # Nothing else to run this turn — loop so the next LLM call
                    # sees the newly-revealed tools.
                    continue

            if executor is None:
                # Dry-run / preview mode (CORE-1): tools are offered so the
                # model can describe its plan, but nothing is executed.
                # Returning early here would leave an assistant message with
                # tool_calls and no matching tool results — an invalid
                # transcript for every provider — and would abort the dry run
                # on the FIRST tool intent.  Instead, append a synthetic
                # result for EACH requested call and continue the loop so the
                # model can produce a full plan.  ``max_iterations`` bounds
                # the loop as usual.
                #
                # This branch deliberately sits AFTER the ``load_tools``
                # interception above.  Today the interception is inert in
                # dry runs — ``tool_gate`` is only built when an executor is
                # present (see the gating decision above), so dry runs offer
                # every tool schema up-front and ``load_tools`` is never a
                # real meta-tool here.  Should gating ever be enabled for
                # executor-less loops, this ordering keeps reveal semantics
                # correct: the meta-tool is handled entirely inside the loop
                # and must not receive a synthetic "[dry-run]" result.
                logger.info(
                    "LLM requested %d tool call(s) but no executor is "
                    "available (dry-run) — appending synthetic results and "
                    "continuing the loop",
                    len(tool_calls),
                )
                for tc in tool_calls:
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=(
                                f"[dry-run] Tool '{tc.name}' was not "
                                "executed. Assume a plausible result and "
                                "continue describing your plan."
                            ),
                            tool_call_id=tc.id,
                            name=tc.name,
                        )
                    )
                continue

            # ── Reverse-map sanitised names for executor dispatch ─────────
            # The LLM returns sanitised tool names; the executor expects the
            # original names (registered by MCP servers, custom tools, etc.).
            # Build per-call executor-facing ToolCall objects with original
            # names while keeping the sanitised names in conversation messages.
            # ``_tool_name_map`` canonicalises the model's name in the
            # LLM-visible space BEFORE reverse-mapping (``b_a_2`` -> ``a_b_2``
            # -> ``a/b``), so a reordered collision-suffixed call the gate
            # accepted is the same call the executor runs; a name it cannot
            # resolve is forwarded untouched for the executor to refuse.
            def _exec_call(tc: ToolCall) -> ToolCall:
                original = _tool_name_map.get(tc.name, tc.name)
                if original != tc.name:
                    return ToolCall(id=tc.id, name=original, arguments=tc.arguments)
                return tc

            async def _execute_fenced(tc: ToolCall) -> ToolResult:
                """Run *tc*, unless it writes and its attempt was taken over since the round's check (#11802).

                A round's calls run one after another: a write that starts after
                another attempt took the execution over is answered unrun, so a
                replaced attempt never lands a write after the resumed one
                started. A read runs. A no-op for a turn no engine execution backs.
                """
                from app.services.tool_classification import classify_tool_name

                call = _exec_call(tc)
                if (
                    turn_writer_bound()
                    and not _call_is_read_only(_in_flight_definitions.get(call.name), call.arguments)
                    and classify_tool_name(call.name)[0] != "read"
                    and (reason := await superseded_reason())
                ):
                    logger.warning("Write '%s' (id=%s) not run: %s", call.name, call.id, reason)
                    return ToolResult(
                        tool_call_id=tc.id,
                        content=superseded_write_refusal(call.name, reason),
                        is_error=True,
                    )
                return await executor.execute_tool(call)

            # ── Tool-side progress ────────────────────────────────
            # A handler that commits something the user is looking at and
            # then keeps working (the deck tools render a layout check AFTER
            # the save) reports the commit through ``emit_tool_event``; the
            # sink bound around each call below forwards it as a
            # ``tool_progress`` frame on the same ``on_event`` as ``tool_done``,
            # tagged with the LLM-visible name and id of the call it belongs
            # to.  Bound per call through a contextvar, like the Langfuse tool
            # span: a parallel batch keeps each call's events on its own call.
            # The loop owns the tagging keys — a payload cannot override them.
            def _progress_sink(tc: ToolCall) -> ToolEventSink | None:
                if on_event is None:
                    return None

                async def _sink(payload: dict[str, Any]) -> None:
                    await _emit(
                        on_event,
                        {
                            **payload,
                            "status": TOOL_PROGRESS_STATUS,
                            "tool": tc.name,
                            "tool_call_id": tc.id,
                        },
                    )

                return _sink

            # ── Session variable injection ──────────────────────────────
            # Replace $variable references in tool call arguments with the
            # actual stored data.  This lets collect_task_results store
            # large payloads that downstream tools receive transparently.
            # The KEYS that received a dict/list (JSON-string) injection are
            # recorded per call so the validation below can exclude exactly
            # those keys: the injected value is a platform-controlled JSON
            # *string* (regardless of the declared parameter type), so
            # validating it against e.g. an array/object-typed parameter would
            # reject the documented ``$collected`` indirection — but every
            # OTHER argument on the call is still schema-checked.
            _session_injected_keys: dict[str, set[str]] = {}
            _session_ref_errors: dict[str, str] = {}
            # Keys whose value came from a resolved reference (never a literal).
            _session_resolved_keys: dict[str, set[str]] = {}
            _ref_vars = {**(session_vars or {}), **_input_vars} if _input_vars is not None else session_vars
            # A ``$input`` placeholder on a run that does not expose the input
            # is refused with the opt-in named, before schema validation would
            # answer an unhelpful type error on the literal.
            _check_unexposed_input = _input_vars is None and not (session_vars and "$input" in session_vars)
            if _ref_vars or _input_vars is not None or _check_unexposed_input:
                for tc in tool_calls:
                    if not tc.arguments:
                        continue
                    for key, val in list(tc.arguments.items()):
                        if not isinstance(val, str):
                            continue
                        if _input_vars is not None:
                            # A model often quotes the placeholder ('"$input"'):
                            # on a run that exposes the input it reads as $input.
                            from app.tools.session_input import normalize_input_placeholder

                            val = normalize_input_placeholder(val)
                        is_ref, resolved_ok, resolved = _resolve_session_reference(val, _ref_vars or {})
                        if not is_ref and _check_unexposed_input and val.lstrip("\"'` ").startswith("$input"):
                            from app.tools.session_input import unexposed_input_reference_error

                            _param_schema = ((_lookup_tool_schema(tc.name) or {}).get("properties") or {}).get(key)
                            _unexposed = unexposed_input_reference_error(key, val, _param_schema)
                            if _unexposed is not None:
                                _session_ref_errors[tc.id] = _unexposed
                                break
                            continue
                        if (
                            not is_ref
                            and _input_vars is not None
                            and not _input_vars
                            and (val == "$input" or val.startswith(("$input.", "$input[")))
                        ):
                            # Opted in but the input carries no JSON document:
                            # never pass the literal placeholder to the tool.
                            _session_ref_errors[tc.id] = (
                                f"Could not resolve session reference '{val}' for "
                                f"argument '{key}': the input carries no JSON "
                                f"document, so $input is not defined in this run."
                            )
                            break
                        if not is_ref:
                            continue
                        if not resolved_ok:
                            # Reference to a known variable that could not be
                            # navigated — record an error so the call is NOT
                            # executed with the literal placeholder.
                            _session_ref_errors[tc.id] = _session_reference_error(val, key, _ref_vars or {})
                            break
                        # Whole objects/arrays are passed as a JSON string (the
                        # documented $collected bulk contract); resolved scalars
                        # are injected natively so e.g. an indicator name is the
                        # real string, not a quoted/JSON-encoded one.
                        if isinstance(resolved, (dict, list)):
                            tc.arguments[key] = json.dumps(resolved, ensure_ascii=False)
                            # Record only THIS key: the JSON-string bulk form is
                            # excluded from CORE-2 validation (its serialized
                            # form may not match the declared type), while the
                            # other arguments on the same call stay validated.
                            _session_injected_keys.setdefault(tc.id, set()).add(key)
                        else:
                            # Native scalar (e.g. $collected.rules[i].name).
                            # Leave it for CORE-2 validation — a real
                            # string/number/bool can be validated correctly
                            # against the declared type, so there is no reason
                            # to weaken the schema check for it.
                            tc.arguments[key] = resolved
                        _session_resolved_keys.setdefault(tc.id, set()).add(key)
            # A write call that retypes a collected field instead of passing
            # its reference is refused before it runs (#2686): the literal is
            # compared against the collected items only, after the references
            # of the same call were resolved (a resolved value is the stored
            # value itself and passes).
            if session_vars and "$collected" in session_vars:
                for tc in tool_calls:
                    if tc.id in _session_ref_errors or not _is_write_tool(tc.name):
                        continue
                    _literal_args = {
                        k: v
                        for k, v in (tc.arguments or {}).items()
                        if k not in _session_resolved_keys.get(tc.id, set())
                    }
                    _retyped = _retyped_collected_error(_literal_args, session_vars)
                    if _retyped is not None:
                        _session_ref_errors[tc.id] = _retyped
            # ``$workspace:PATH`` arguments are read from the run's sandbox
            # workspace at dispatch (``tools.base.resolve_workspace_references``,
            # which checks the file against the declared type and refuses the
            # call when it cannot): the reference itself is excluded from
            # coercion and schema validation here, like a ``$collected`` key.
            from app.tools.base import workspace_reference_keys

            for tc in tool_calls:
                if tc.id in _session_ref_errors:
                    continue
                for key in workspace_reference_keys(tc.arguments):
                    _session_injected_keys.setdefault(tc.id, set()).add(key)

            # Per-run budget: debit this iteration's tool calls.  This
            # is the single pre-dispatch choke point shared by the sequential
            # and parallel execution paths, and it sits AFTER the ``load_tools``
            # meta-tool has been filtered out (a loop-internal reveal is not
            # work the user asked for) and after the executor-less dry-run
            # branch has returned (nothing is executed there).  Counting the
            # batch — not the iteration — is the whole point: one iteration can
            # fan out into many parallel calls, which is exactly what
            # ``max_iterations`` fails to bound.  The debit lands before the
            # calls run, so the NEXT iteration's gate sees them even if this
            # batch is what exhausts the budget.
            #
            # A batch larger than what is left crosses the ceiling. With
            # ``AGENT_LOOP_MAX_TOOL_CALLS_STRICT`` it is CUT to the allowance:
            # the gate only refuses the next LLM call, so without the
            # cut one response can fan out without bound. The calls past the
            # allowance are answered in place below, like a loop-breaker
            # refusal, so the transcript stays well-formed. Without it the batch
            # runs in full, as it always did, and the overshoot is logged once
            # per run and recorded on the iteration's trace span.
            #
            # One pause the run owns (``_run_pause_names``) per batch is neither
            # debited nor cut: it is not acting, for the same reason its
            # round is exempt from ``max_iterations``. A second is charged like
            # any call (``_debited_tool_calls``).
            _batch_names = [tc.name for tc in tool_calls]
            _budget_dropped: list[ToolCall] = []
            if _budget is not None:
                _debited = _debited_tool_calls(tool_calls, _tool_name_map, pauses=_run_pauses)
                _allowance = _budget.remaining_tool_calls()
                _crosses = _allowance is not None and len(_debited) > _allowance
                if _crosses and _budget.strict_tool_calls:
                    _budget_dropped = _debited[_allowance:]
                    _debited = _debited[:_allowance]
                    _dropped_ids = {id(tc) for tc in _budget_dropped}
                    tool_calls = [tc for tc in tool_calls if id(tc) not in _dropped_ids]
                elif _crosses:
                    _overshoot = {
                        "limit": int(_budget.max_tool_calls or 0),
                        "batch": len(_debited),
                        "allowance": _allowance,
                        "overshoot": len(_debited) - _allowance,
                    }
                    _iter_outcome["tool_call_budget_overshoot"] = _overshoot
                    if not _tool_budget_overshoot_logged:
                        _tool_budget_overshoot_logged = True
                        logger.warning(
                            "Agent loop per-run tool-call budget: a batch of %d call(s) crosses "
                            "the %d-call budget with %d left and runs in full, %d past it; the "
                            "next LLM call is refused. AGENT_LOOP_MAX_TOOL_CALLS_STRICT=true cuts "
                            "such a batch to the calls left instead.",
                            _overshoot["batch"],
                            _overshoot["limit"],
                            _overshoot["allowance"],
                            _overshoot["overshoot"],
                        )
                _budget.record_tool_calls(len(_debited))

            # Notify caller about upcoming tool execution
            tool_names = _batch_names
            await _emit(
                on_event,
                {
                    "status": "tool_start",
                    "tools": tool_names,
                    "iteration": iteration + 1,
                    "llm_model": response.model or model,
                    "llm_stop_reason": response.stop_reason,
                    "llm_usage": response.usage or {},
                    "llm_system_prompt": llm_system_prompt,
                    "llm_user_prompt": llm_user_prompt,
                    "llm_raw_response": llm_response_content,
                },
            )

            if _budget_dropped and _budget is not None:
                _budget_notice = _format_tool_budget_refusal(_budget)
                logger.warning(
                    "Agent loop per-run tool-call budget: %d of %d call(s) in this "
                    "batch not executed (%s)",
                    len(_budget_dropped),
                    len(_budget_dropped) + len(tool_calls),
                    _budget.snapshot(),
                )
                for tc in _budget_dropped:
                    _bd_tool_ctx = _lf_start_tool_span(
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        arguments=tc.arguments,
                        parent_ctx=_iter_ctx,
                    )
                    _lf_end_tool_span(
                        _bd_tool_ctx, is_error=True, error=_budget_notice, error_kind="budget"
                    )
                    # Answered, never executed. Deliberately NOT tracked as a
                    # tool failure (no failure brake, no cooldown) and never
                    # screened by the loop breaker: the tool did nothing wrong.
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_budget_notice,
                            tool_call_id=tc.id,
                            name=tc.name,
                            not_run="budget",
                        )
                    )
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": tc.name,
                            "success": False,
                            "budget_exhausted": True,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(tc.arguments),
                            "tool_output": _budget_notice,
                            "tool_output_full": _budget_notice,
                        },
                    )

            # Helper: track tool error counts, rate-limit cooldowns, and nudges
            def _track_tool_result(
                tool_name: str,
                is_error: bool,
                result_content: str = "",
                *,
                current_iteration: int = iteration,
                target_throttled: bool = False,
                outcome_call: tuple[str, str] | None = None,
                refused: bool = False,
            ) -> None:
                nonlocal tool_errors_observed, tool_successes_observed
                if is_error and refused:
                    # The tool turned the call down for a validation reason
                    # (``ToolResult.refused``) and said what to do instead:
                    # not a runtime failure, so no streak, no cooldown and
                    # no brake. The loop breaker still counts the repeat.
                    tool_errors_observed += 1
                    return
                if is_error and target_throttled:
                    # One target of the tool is throttling the platform and the
                    # tool refuses it on its own (``ToolResult.target_throttled``):
                    # a tool-wide cooldown or a failure streak would take every
                    # other target away with it. Still an error for the
                    # activity signal; the streak is left as it was.
                    tool_errors_observed += 1
                    return
                if is_error and outcome_call is not None:
                    # The call ran and its work came out rejected
                    # (``ToolResult.outcome_failure``): counted for THIS call
                    # only, so a retry the model fixed is never one failure
                    # closer to the brake. No tool-wide cooldown either.
                    outcome_error_counts[outcome_call] = outcome_error_counts.get(outcome_call, 0) + 1
                    outcome_tool_streaks[tool_name] = outcome_tool_streaks.get(tool_name, 0) + 1
                    tool_errors_observed += 1
                    return
                if is_error:
                    # One increment per turn and tool: the other failed calls
                    # of the same batch are the same decision (see
                    # ``_MAX_TOOL_FAILURES``). Each call still counts below as
                    # an error for the activity signal and the friction ledger.
                    if tool_error_turns.get(tool_name) != current_iteration:
                        tool_error_counts[tool_name] = tool_error_counts.get(tool_name, 0) + 1
                        tool_error_turns[tool_name] = current_iteration
                    # Monotonic tally for the per-iteration activity signal (see
                    # ``tool_errors_observed`` init) - counts every error, even
                    # one that coincides with another tool's streak clearing.
                    tool_errors_observed += 1
                    # When the error indicates the tool name is not callable
                    # in this run (ScopedToolExecutor rejection or the
                    # composite's not-found error), record the name so we
                    # can aggregate across different hallucinated variants
                    # and fire the capability-not-found nudge.
                    if TOOL_NOT_AVAILABLE_SENTINEL in result_content:
                        not_available_tool_names.add(tool_name)
                    # The auth classifier excludes rate-limit messages
                    # internally, so GitHub's secondary-rate-limit phrasing
                    # ("Permission Denied: You have exceeded a secondary rate
                    # limit…") always takes the rate-limit branch regardless
                    # of ordering.  Both branches only ever EXTEND an
                    # existing cooldown — a shorter rate-limit hit from a
                    # parallel call in the same batch must not shorten a longer
                    # auth cooldown already recorded for the tool — and the log
                    # reports the EFFECTIVE recorded cooldown, not the candidate.
                    # The classifiers lowercase internally, so the raw result
                    # content is passed as-is.
                    #
                    # ``_tools_not_in_cooldown`` offers a tool again once
                    # ``cooldown[name] <= iteration``, so suppressing it for N
                    # FULL future iterations requires ``current + N + 1`` — with
                    # a bare ``current + N`` the tool would come back one
                    # iteration early (N-1 suppressed iterations).
                    if is_rate_limit_error(result_content):
                        cooldown_end = (
                            current_iteration + _RATE_LIMIT_COOLDOWN_ITERS + 1
                        )
                        if cooldown_end > tool_cooldown_until.get(tool_name, -1):
                            tool_cooldown_until[tool_name] = cooldown_end
                        logger.warning(
                            "Tool '%s' hit rate limit — on cooldown until iteration %d",
                            tool_name,
                            tool_cooldown_until[tool_name] + 1,
                        )
                    # The whole-result classifier, the same rule as the
                    # ``is_error`` stamp: an error that merely QUOTES "401"
                    # or "permission denied" (a 404 detail) is not an auth
                    # failure and keeps the normal error handling.
                    elif is_auth_failure_result(result_content):
                        cooldown_end = (
                            current_iteration + _AUTH_FAILURE_COOLDOWN_ITERS + 1
                        )
                        if cooldown_end > tool_cooldown_until.get(tool_name, -1):
                            tool_cooldown_until[tool_name] = cooldown_end
                        logger.warning(
                            "Tool '%s' hit auth/permission failure — on cooldown "
                            "until iteration %d",
                            tool_name,
                            tool_cooldown_until[tool_name] + 1,
                        )
                else:
                    tool_error_counts.pop(tool_name, None)
                    tool_error_turns.pop(tool_name, None)
                    outcome_tool_streaks.pop(tool_name, None)
                    for call in [c for c in outcome_error_counts if c[0] == tool_name]:
                        outcome_error_counts.pop(call, None)
                    tool_successes_observed += 1

            # ── Pre-execution argument validation (CORE-2) ───────────────
            # Validate each call's arguments against the tool's declared
            # input_schema BEFORE dispatch (shared by the single-call and
            # parallel paths below).  Invalid calls are NOT executed — a
            # structured tool-result error is appended so the model receives
            # "missing required field X" feedback and self-corrects on the
            # next iteration.  Failures count toward ``tool_error_counts``
            # so repeated garbage still trips the ``_MAX_TOOL_FAILURES``
            # nudge + cooldown at the end of this iteration.
            _valid_calls: list[ToolCall] = []
            # Probe declarations (``expect_error``) taken off the calls of
            # this round, by call id: read back when the result is logged.
            _probe_reasons: dict[str, str] = {}
            for tc in tool_calls:
                _tc_schema = _lookup_tool_schema(tc.name)
                _tc_probe = _strip_expect_error(tc, _tc_schema)
                if _tc_probe is not None:
                    _probe_reasons[tc.id] = _tc_probe
                if tc.id not in _session_ref_errors:
                    # Stringified scalars ("1", "true") become the declared
                    # primitive BEFORE validation - the model that
                    # emits them retries the same shape forever otherwise. A
                    # $collected-injected key is a platform-written JSON
                    # string the tool must receive verbatim, so it is
                    # skipped (never coerced) - it is excluded from the
                    # validation below for the same reason.
                    _coerced_args = coerce_tool_arguments(
                        tc.arguments,
                        _tc_schema,
                        skip_keys=_session_injected_keys.get(tc.id),
                    )
                    if _coerced_args is not tc.arguments:
                        tc.arguments = _coerced_args
                if tc.id in _session_ref_errors:
                    # An argument referenced a $session variable / path that
                    # could not be resolved.  Never execute with the literal
                    # placeholder (it would be written verbatim into the target
                    # system); surface actionable feedback so the model
                    # re-references the collected data correctly.
                    _validation_error = _session_ref_errors[tc.id]
                    logger.warning(
                        "Tool '%s' called with an unresolved session reference "
                        "(id=%s) — returning feedback instead of executing",
                        tc.name,
                        tc.id,
                    )
                elif tc.id in _session_injected_keys:
                    # A dict/list $collected injection replaced these keys with a
                    # platform-controlled JSON string whose serialized form may
                    # not match the declared parameter type. Validate with ONLY
                    # those keys excluded so every OTHER argument is still
                    # schema-checked (missing required fields, wrong types)
                    # instead of the whole call bypassing CORE-2.
                    _validation_error = _validate_tool_arguments_excluding(
                        tc.name,
                        tc.arguments,
                        _tc_schema,
                        _session_injected_keys[tc.id],
                    )
                    if _validation_error is None:
                        _valid_calls.append(tc)
                        continue
                    logger.warning(
                        "Tool '%s' called with invalid non-injected arguments "
                        "(id=%s) — returning validation feedback instead of "
                        "executing",
                        tc.name,
                        tc.id,
                    )
                else:
                    _validation_error = validate_tool_arguments(
                        tc.name, tc.arguments, _tc_schema
                    )
                    if _validation_error is None:
                        _valid_calls.append(tc)
                        continue
                    logger.warning(
                        "Tool '%s' called with invalid arguments (id=%s) — "
                        "returning validation feedback instead of executing",
                        tc.name,
                        tc.id,
                    )
                # Record a (closed) tool span so Langfuse traces keep one
                # span per requested call — parity with the timeout /
                # exception paths below, which also never reach a result.
                _val_tool_ctx = _lf_start_tool_span(
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    arguments=tc.arguments,
                    parent_ctx=_iter_ctx,
                )
                _lf_end_tool_span(
                    _val_tool_ctx,
                    is_error=True,
                    error=_validation_error,
                    error_kind="validation",
                )
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=_validation_error,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )
                _track_tool_result(
                    tc.name, is_error=True, result_content=_validation_error
                )
                await _emit(
                    on_event,
                    {
                        "status": "tool_done",
                        **_tool_timing(report_tool_durations, 0),
                        "tool": tc.name,
                        "success": False,
                        "tool_input": _serialize_tool_arguments(tc.arguments),
                        "tool_input_full": _serialize_tool_arguments_full(
                            tc.arguments
                        ),
                        "tool_output": _trim_trace_text(
                            _validation_error, 30_000
                        ),
                        "tool_output_full": _validation_error,
                    },
                )
            tool_calls = _valid_calls

            # ── Loop breaker ──────────────────────────────────────
            # Screen every validated call against the per-run repeat counts.
            # A refused call is answered here like a validation failure - a
            # tool result the model reads, counted toward the failure brake,
            # one ``loop`` span on the trace - and never reaches the executor.
            # A warning is queued and injected after the batch's results.
            _screened_calls: list[ToolCall] = []
            for tc in tool_calls:
                _lb_orig_name = _tool_name_map.get(tc.name, tc.name)
                tool_call_totals[tc.name] = tool_call_totals.get(tc.name, 0) + 1
                # A module that refused the member of a per-user CTEM stage
                # run is not called again in the run (#2042).
                if (_refused_module := ctem_refused_module(_lb_orig_name)) is not None:
                    _refused_answer = ctem_refusal_answer(_refused_module)
                    logger.info(
                        "CTEM guard: '%s' not sent, %s already refused this member (id=%s)",
                        tc.name,
                        _refused_module,
                        tc.id,
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_refused_answer,
                            tool_call_id=tc.id,
                            name=tc.name,
                            not_run="module_refused",
                        )
                    )
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": tc.name,
                            "success": False,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(tc.arguments),
                            "tool_output": _refused_answer,
                            "tool_output_full": _refused_answer,
                        },
                    )
                    continue
                _lb_decision = loop_breaker.screen(_lb_orig_name, tc.arguments)
                if _lb_decision.action == "allow":
                    _screened_calls.append(tc)
                    continue
                _lf_end_span(
                    _lf_start_loop_span(
                        _iter_ctx,
                        tool_name=tc.name,
                        kind=_lb_decision.kind,
                        action=_lb_decision.action,
                        count=_lb_decision.count,
                        threshold=_lb_decision.threshold,
                        arguments=tc.arguments,
                    ),
                    output={"action": _lb_decision.action, "count": _lb_decision.count},
                )
                if _lb_decision.action == "warn":
                    logger.warning(
                        "Loop breaker: '%s' called %d time(s) this run (%s) - warning the model",
                        tc.name,
                        _lb_decision.count,
                        _lb_decision.kind,
                    )
                    _loop_breaker_pending_nudges.append((tc.name, _lb_decision))
                    _screened_calls.append(tc)
                    continue
                _refusal = _format_loop_breaker_refusal(tc.name, _lb_decision)
                logger.warning(
                    "Loop breaker: refusing call %d to '%s' (%s) this run (id=%s)",
                    _lb_decision.count,
                    tc.name,
                    _lb_decision.kind,
                    tc.id,
                )
                _lb_tool_ctx = _lf_start_tool_span(
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    arguments=tc.arguments,
                    parent_ctx=_iter_ctx,
                )
                _lf_end_tool_span(
                    _lb_tool_ctx, is_error=True, error=_refusal, error_kind="loop_breaker"
                )
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=_refusal,
                        tool_call_id=tc.id,
                        name=tc.name,
                        not_run="loop_breaker",
                    )
                )
                # The refusal IS this ask's answer, recorded like any other so
                # the ask is not left "in flight". What keeps it counted is the
                # ``_Ask.refused`` flag ``screen()`` set, never the message: a
                # ``similar``-shape refusal quoting a long URL is over the
                # clearing threshold and the curation stubs it, and compaction
                # can drop it - the refused ask counts either way, so a model
                # that ignores the refusal reads a rising count.
                _record_breaker_answer(tc)
                _track_tool_result(tc.name, is_error=True, result_content=_refusal)
                await _emit(
                    on_event,
                    {
                        "status": "tool_done",
                        **_tool_timing(report_tool_durations, 0),
                        "tool": tc.name,
                        "success": False,
                        "loop_breaker": True,
                        "tool_input": _serialize_tool_arguments(tc.arguments),
                        "tool_input_full": _serialize_tool_arguments_full(tc.arguments),
                        "tool_output": _refusal,
                        "tool_output_full": _refusal,
                    },
                )
            tool_calls = _screened_calls
            # ── Write coalescing ──────────────────────────────────
            # Several updates of one calendar event (or one edit of one Teams
            # message) in this round are merged into the first of them, which
            # the provider receives once: one update, one notification. Every
            # absorbed call is answered here, naming its carrier, and never
            # reaches the executor.
            if tool_calls and _app_settings.self_verification_loop:
                _coalesced = coalesce_write_calls(
                    tool_calls,
                    coalescing_for=_lookup_tool_coalescing,
                    original_name=lambda _n: _tool_name_map.canonical_visible(_n) or _n,
                )
                for _absorbed_tc, _leader_tc in _coalesced.absorbed:
                    _merged_note = format_coalesced_answer(_leader_tc)
                    logger.info(
                        "Write coalescing: '%s' (id=%s) merged into call %s",
                        _absorbed_tc.name,
                        _absorbed_tc.id,
                        _leader_tc.id,
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_merged_note,
                            tool_call_id=_absorbed_tc.id,
                            name=_absorbed_tc.name,
                            not_run="coalesced",
                        )
                    )
                    _record_breaker_answer(_absorbed_tc)
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": _absorbed_tc.name,
                            "success": True,
                            "coalesced_into": _leader_tc.id,
                            "tool_input": _serialize_tool_arguments(_absorbed_tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(
                                _absorbed_tc.arguments
                            ),
                            "tool_output": _merged_note,
                            "tool_output_full": _merged_note,
                        },
                    )
                tool_calls = _coalesced.calls
            # ── Duplicate-send guard ──────────────────────────────
            # A send whose recipients and body match one that succeeded in
            # this run, or one the same agent made in the last ten minutes,
            # is answered here with the earlier call's id and never sent.
            # ``allow_duplicate: true`` lets it through (audited), and the
            # flag is taken off before the tool runs.
            if tool_calls and _tool_send_guard_map and _app_settings.self_verification_loop:
                _sg_kept: list[ToolCall] = []
                # Two identical sends in ONE round: the second waits on no
                # record (both would run at once), so it matches the first here.
                _sg_round: dict[str, send_guard.EarlierSend] = {}
                for tc in tool_calls:
                    _sg_spec = _tool_send_guard_map.get(tc.name)
                    if _sg_spec is None:
                        _sg_kept.append(tc)
                        continue
                    _sg_tool = _tool_name_map.get(tc.name, tc.name)
                    _sg_clean = send_guard.strip_flag(tc)
                    _sg_fp = send_guard.fingerprint(_sg_clean, _sg_spec, tool=_sg_tool)
                    if _sg_fp is None:
                        _sg_kept.append(_sg_clean)
                        continue
                    _sg_earlier = _sg_round.get(_sg_fp.key) or await _send_ledger.earlier(_sg_fp)
                    if _sg_earlier is None:
                        _sg_round[_sg_fp.key] = send_guard.EarlierSend(call_id=tc.id, at=time.time(), same_run=True)
                        _sg_kept.append(_sg_clean)
                        continue
                    if send_guard.wants_duplicate(tc):
                        await send_guard.audit(
                            "duplicate_send_allowed", tool=_sg_tool, fp=_sg_fp, call_id=tc.id, earlier=_sg_earlier
                        )
                        _sg_kept.append(_sg_clean)
                        continue
                    _sg_refusal = send_guard.format_refusal(_sg_earlier)
                    logger.warning(
                        "Duplicate-send guard: refusing '%s' (id=%s), same as call %s",
                        tc.name,
                        tc.id,
                        _sg_earlier.call_id,
                    )
                    await send_guard.audit(
                        "duplicate_send_refused", tool=_sg_tool, fp=_sg_fp, call_id=tc.id, earlier=_sg_earlier
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_sg_refusal,
                            tool_call_id=tc.id,
                            name=tc.name,
                            not_run="duplicate_send",
                        )
                    )
                    _record_breaker_answer(tc)
                    _track_tool_result(tc.name, is_error=True, result_content=_sg_refusal, refused=True)
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": tc.name,
                            "success": False,
                            "duplicate_of": _sg_earlier.call_id,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(tc.arguments),
                            "tool_output": _sg_refusal,
                            "tool_output_full": _sg_refusal,
                        },
                    )
                tool_calls = _sg_kept
            # ── In-flight repeat guard ────────────────────────────
            # A resumed turn repeating, as it was, a write its interrupted
            # attempt had started (the resume note names it) is answered here
            # once, unrun: the write may have landed. Repeating it again runs it.
            # Before it, a call in flight at two consecutive cuts (read or
            # write) is never run again unchanged: it may be what stops the turn.
            if tool_calls and _in_flight_poison:
                _ip_kept: list[ToolCall] = []
                for tc in tool_calls:
                    _ip_seen = _in_flight_poison.get(_call_digest(tc.name, tc.arguments))
                    if _ip_seen is None:
                        _ip_kept.append(tc)
                        continue
                    _ip_refusal = in_flight_poison_refusal(_ip_seen)
                    logger.warning(
                        "In-flight poison guard: '%s' (id=%s) was in flight at %s consecutive cuts; not run",
                        tc.name,
                        tc.id,
                        _ip_seen.get("cuts"),
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_ip_refusal,
                            tool_call_id=tc.id,
                            name=tc.name,
                            not_run="in_flight_poison",
                        )
                    )
                    _record_breaker_answer(tc)
                    _track_tool_result(tc.name, is_error=True, result_content=_ip_refusal, refused=True)
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": tc.name,
                            "success": False,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(tc.arguments),
                            "tool_output": _ip_refusal,
                            "tool_output_full": _ip_refusal,
                        },
                    )
                tool_calls = _ip_kept
            if tool_calls and (_in_flight_writes or _in_flight_by_key):
                _if_kept: list[ToolCall] = []
                for tc in tool_calls:
                    _if_digest = _call_digest(tc.name, tc.arguments)
                    _if_earlier = _in_flight_writes.pop(_if_digest, None)
                    _if_definition = _in_flight_definitions.get(tc.name)
                    _if_reworded = False
                    if _if_earlier is None and _in_flight_by_key:
                        _if_key = _landed_key_of(_if_definition, tc.arguments)
                        _if_earlier = _in_flight_by_key.pop((tc.name, _if_key), None) if _if_key is not None else None
                        if _if_earlier is not None:
                            _if_reworded = True
                            _in_flight_writes.pop(str(_if_earlier.get("digest") or ""), None)
                    elif _if_earlier is not None:
                        _ifk = _landed_key_of(_if_definition, tc.arguments)
                        if _ifk is not None:
                            _in_flight_by_key.pop((tc.name, _ifk), None)
                    if _if_earlier is None or _call_is_read_only(_if_definition, tc.arguments):
                        # Not a started write, or this call reads only (a wait on a running command).
                        _if_kept.append(tc)
                        continue
                    # A write the tool can look up (an issue by title, a commit on
                    # its branch): landed, it is answered with what landed; not
                    # landed, the repeat runs; unknown, it is held back once. A
                    # reworded repeat is probed on the interrupted call's own
                    # arguments and runs unless that call landed.
                    _if_landed = _in_flight_probed.get(str(_if_earlier.get("digest") or "")) or await _probe_landed(
                        _if_definition,
                        _if_earlier.get("arguments") if _if_reworded else tc.arguments,
                    )
                    if _if_landed is False or (_if_reworded and not isinstance(_if_landed, str)):
                        _if_kept.append(tc)
                        continue
                    _if_refusal = (
                        in_flight_landed_answer(_if_earlier, _if_landed, reworded=_if_reworded)
                        if isinstance(_if_landed, str)
                        else in_flight_repeat_refusal(_if_earlier)
                    )
                    logger.warning(
                        "In-flight repeat guard: '%s' (id=%s) repeats call %s, started before the resume",
                        tc.name,
                        tc.id,
                        _if_earlier.get("id"),
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=_if_refusal,
                            tool_call_id=tc.id,
                            name=tc.name,
                            not_run="in_flight_landed" if isinstance(_if_landed, str) else "in_flight_repeat",
                        )
                    )
                    _record_breaker_answer(tc)
                    _track_tool_result(
                        tc.name,
                        is_error=not isinstance(_if_landed, str),
                        result_content=_if_refusal,
                        refused=not isinstance(_if_landed, str),
                    )
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": tc.name,
                            "success": isinstance(_if_landed, str),
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(tc.arguments),
                            "tool_output": _if_refusal,
                            "tool_output_full": _if_refusal,
                        },
                    )
                tool_calls = _if_kept
            # ── One writer per engine-run turn ────────────────────
            # A turn whose execution another attempt took over (taken back,
            # claimed by another worker) runs no further call: it stops here,
            # before any call of this round, and writes nothing. A no-op for a
            # turn no engine execution backs.
            if tool_calls:
                await ensure_single_writer()
            # Wall-clock start of this round's tool execution — the iteration
            # waiver below only refunds rounds that actually spent time waiting,
            # so a refused or malformed ``wait`` that returns instantly
            # still costs its iteration and cannot be spammed for free rounds.
            _tool_round_started = time.monotonic()

            # ── The wait primitive: executed in-loop ───────────────
            # ``wait`` never reaches the executor, for the same reason
            # ``load_tools`` doesn't: everything it has to respect is loop
            # state.  The pause is clamped against ``wall_clock_deadline`` so it
            # cannot eat the budget the final answer needs, it is raced against
            # ``cancel_event`` so a user cancel ends it instantly instead of at
            # the next poll, and the round it belongs to is exempt from the
            # iteration budget.
            #
            # Gated on ``_wait_definition``: when the run already carries a tool
            # of its own named ``wait`` (an MCP server or custom tool may own
            # that name) the built-in was never offered, and intercepting here
            # would silently swallow every call to the real tool.  Same when the
            # policy disables waiting — the call then falls through to the
            # executor, which answers "not available" like any other unknown
            # tool instead of performing a pause nobody offered.
            #
            # Mixed rounds (wait + other tools): sibling tools run FIRST, then
            # the pause. Parking before independent work is what made "keep
            # working after spawn" the exception instead of the rule.
            _deferred_wait_calls: list[ToolCall] = []

            async def _flush_deferred_waits(
                *,
                _calls=_deferred_wait_calls,
                _iter=iteration,
                _outcome=_iter_outcome,
            ) -> list[str]:
                """Run a mixed-round pause after sibling tools. Empty = no handoff."""
                nonlocal _wait_paused_total
                if not _calls:
                    return []
                if cancel_event is not None and cancel_event.is_set():
                    return []
                calls = list(_calls)
                _calls.clear()
                try:
                    _paused, _handoff, _elapsed = await _execute_wait_calls(
                        calls,
                        iteration=_iter,
                        messages=messages,
                        on_event=on_event,
                        wait_budget=_wait_budget,
                        # Late-bound on purpose, unlike the defaults above: an
                        # approval pause earlier in this turn extends the
                        # deadline, and a deferred wait must respect the
                        # extension rather than the value at definition time.
                        wall_clock_deadline=wall_clock_deadline,  # noqa: B023
                        wait_skip_event=wait_skip_event,
                        wait_condition=wait_condition,
                        cancel_event=cancel_event,
                        track_tool_result=_track_tool_result,
                        pending_input=_steering_pending,
                        report_tool_durations=report_tool_durations,
                        stopping=_wait_stopping,
                    )
                except _CancelledMidCallError:
                    _outcome.update(
                        {
                            "terminal": True,
                            "stop_reason": "cancelled",
                            "error_kind": "CancelledMidCall",
                        }
                    )
                    logger.info(
                        "Wait aborted at iteration %d — request cancelled by user",
                        _iter + 1,
                    )
                    raise
                _wait_paused_total += _elapsed
                if _paused:
                    _forget_reads_after_pause()
                return _handoff

            def _should_answer_user_instead_of_park(
                handoff_ids: list[str],
            ) -> bool:
                """A wait-budget hand-off must yield to an interactive user.

                When the model burns its wait budget re-waiting on background
                tasks it did NOT spawn in THIS turn, it is stalling on
                pre-existing work while a human waits for a reply — parking
                would swallow the user's message behind another canned "still
                running" notice for the whole budget (up to 30 min).  Answer
                the user now; parent-wake still resumes the conversation when
                those tasks finish.

                Gated to interactive chat turns only (a ``steering_queue`` is
                present): assignment runs and sub-agent continuation turns have
                no human awaiting a reply and must keep their park/continuation
                semantics.  A turn that spawned its own work THIS turn and
                parked is the intended "I launched work, waiting" behaviour and
                is left untouched (its ids intersect this turn's spawns).
                """
                if steering_queue is None or not getattr(steering_queue, "interactive", True) or not handoff_ids:
                    return False
                _spawned_now = set(collect_spawned_task_ids(messages))
                return not (set(handoff_ids) & _spawned_now)

            async def _arm_answer_after_park(handoff_ids: list[str]) -> None:
                """Force the next iteration tool-free so the model answers the
                user instead of returning the canned park."""
                nonlocal _answer_user_after_park, _answer_flushed
                _answer_user_after_park = True
                # Retract any provisional answer already streamed this turn so
                # the forced final answer replaces it (mirrors the park path).
                if on_token and _answer_flushed:
                    await _emit(on_event, {"status": "stream_retract"})
                    _answer_flushed = False
                await _emit(on_event, {"status": "composing"})
                messages.append(
                    LLMMessage(
                        role="system",
                        content=(
                            "Background task(s) still running: "
                            + ", ".join(handoff_ids)
                            + ". They will continue in the background and you "
                            "will be resumed automatically when they finish. "
                            "The user has sent a new message this turn — answer "
                            "it now with what you already have. Do not wait on "
                            "those tasks again; briefly note they are still in "
                            "progress and respond to the user's request directly."
                        ),
                    )
                )

            async def _after_sibling_tools():
                """Flush a deferred wait; return a terminal response or None."""
                try:
                    _handoff = await _flush_deferred_waits()
                except _CancelledMidCallError:
                    return _cancelled_chat_response(model, accumulated_usage)
                if not _handoff:
                    return None
                if _should_answer_user_instead_of_park(_handoff):
                    await _arm_answer_after_park(_handoff)
                    return None
                _handoff_content = await build_waiting_on_tasks_message(
                    messages,
                    _handoff,
                    wait_condition,
                    # Late-bound for the same reason as the deferred wait above:
                    # an approval pause this turn extends the deadline,
                    # and the lookup budget must be measured against the
                    # extension, not the value at definition time.
                    lookup_timeout=_label_lookup_budget(wall_clock_deadline),  # noqa: B023
                )
                if on_token:
                    await _emit(on_event, {"status": "streaming"})
                    await _stream_text_chunked(on_token, _handoff_content)
                return _waiting_on_tasks_response(
                    model, accumulated_usage, _handoff, _handoff_content
                )

            if _wait_definition is not None and any(
                _tool_name_map.get(tc.name, tc.name) == WAIT_TOOL_NAME for tc in tool_calls
            ):
                _wait_calls = [tc for tc in tool_calls if _tool_name_map.get(tc.name, tc.name) == WAIT_TOOL_NAME]
                _sibling_calls = [tc for tc in tool_calls if _tool_name_map.get(tc.name, tc.name) != WAIT_TOOL_NAME]
                if _sibling_calls:
                    _deferred_wait_calls.extend(_wait_calls)
                    tool_calls = _sibling_calls
                else:
                    try:
                        _wait_paused, _handoff_ids, _elapsed = await _execute_wait_calls(
                            _wait_calls,
                            iteration=iteration,
                            messages=messages,
                            on_event=on_event,
                            wait_budget=_wait_budget,
                            wall_clock_deadline=wall_clock_deadline,
                            wait_skip_event=wait_skip_event,
                            wait_condition=wait_condition,
                            cancel_event=cancel_event,
                            track_tool_result=_track_tool_result,
                            pending_input=_steering_pending,
                            report_tool_durations=report_tool_durations,
                            stopping=_wait_stopping,
                        )
                    except _CancelledMidCallError:
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "cancelled",
                                "error_kind": "CancelledMidCall",
                            }
                        )
                        logger.info(
                            "Wait aborted at iteration %d — request cancelled by user",
                            iteration + 1,
                        )
                        return _cancelled_chat_response(model, accumulated_usage)
                    _wait_paused_total += _elapsed
                    # Nothing else to run this turn. A wait-budget cliff with
                    # named children still running is a terminal hand-off, not
                    # another LLM round (and not a digest of wait() results) —
                    # UNLESS an interactive user opened this turn and the model
                    # re-waited on pre-existing tasks: then answer the
                    # user instead of re-parking (falls through to a tool-free
                    # final-answer round).
                    if _handoff_ids and not _should_answer_user_instead_of_park(
                        _handoff_ids
                    ):
                        _handoff_content = await build_waiting_on_tasks_message(
                            messages,
                            _handoff_ids,
                            wait_condition,
                            lookup_timeout=_label_lookup_budget(wall_clock_deadline),
                        )
                        if on_token:
                            await _emit(on_event, {"status": "streaming"})
                            await _stream_text_chunked(on_token, _handoff_content)
                        return _waiting_on_tasks_response(
                            model, accumulated_usage, _handoff_ids, _handoff_content
                        )
                    if _handoff_ids:
                        await _arm_answer_after_park(_handoff_ids)
                    # A round that really paused is refunded (a refused or
                    # malformed wait is not) and the loop goes straight back
                    # to the model so it can re-check whatever it was waiting
                    # for.
                    if _wait_paused:
                        _waive_wait_round(WAIT_TOOL_NAME)
                        _forget_reads_after_pause()
                        _last_round_was_wait = True
                    continue

            # When every call was rejected, fall through with an empty list:
            # the parallel branch gathers nothing, and the repeated-failure
            # nudge block below still runs before the next LLM iteration.

            # ── Per-tool approval pre-screen ──────────────────────
            # Screen the WHOLE turn before executing any of it. Gating from
            # inside dispatch would be too late: in the parallel path below,
            # ``asyncio.gather`` has already run every call by the time a pause
            # propagates, so calls after the gated one would have taken effect
            # without approval, and the post-gather loop appends tool results in
            # order — so it would stop at the pause and leave the earlier
            # ``tool_use`` blocks without matching ``tool_result``s. Strict-mode
            # Anthropic/OpenAI reject that conversation on resume.
            #
            # Screening here means: either the entire batch is approved and runs
            # normally, or nothing runs and the human sees every proposed call at
            # once. Invalid calls were already answered above and are excluded
            # from ``tool_calls``, so the pause covers exactly the calls that
            # still need results.
            # A consent screen loads what it decides on only now that
            # a call needs it.
            _prepare_consent = getattr(executor, "prepare_consent", None)
            if _prepare_consent is not None and tool_calls:
                await _prepare_consent(list(tool_calls))
            _screen = getattr(executor, "screen_for_approval", None)
            if _screen is not None and tool_calls:
                _proposals = _screen(list(tool_calls))
                if _proposals and request_approval is not None:
                    # Pause WITHIN the turn. A human is already present —
                    # this is chat, not a scheduled run — so unwinding and
                    # restarting would discard the reasoning done so far and
                    # make an approval feel like a rejection.
                    #
                    # The channel is responsible for reaching the reviewer and
                    # returning their decisions; the loop only waits.
                    # A cancel (the user's, or an emergency stop) ends the wait:
                    # an answer given later must not run a tool for a turn that
                    # was stopped. Cancelling the channel's wait runs its own
                    # cleanup, so the prompt is withdrawn from every client.
                    if cancel_event is not None and cancel_event.is_set():
                        return _cancelled_chat_response(model, accumulated_usage)
                    # The budget bounds work, not a reviewer. Waiting for a
                    # person is not the runaway this deadline exists to stop,
                    # and letting it count meant someone who read the arguments
                    # carefully and answered twelve minutes later had the turn
                    # killed as "took too long" — losing the reasoning the
                    # mid-turn pause was chosen to preserve, right after they
                    # did the careful thing.
                    _paused_at = time.monotonic()
                    try:
                        _decisions = await _race_cancellation(
                            request_approval(_proposals), cancel_event
                        )
                    except _CancelledMidCallError as _mc_exc:
                        _iter_error = _mc_exc
                        _decisions = None
                    finally:
                        _paused_for = time.monotonic() - _paused_at
                        if wall_clock_deadline is not None:
                            wall_clock_deadline += _paused_for
                        # Waiting on a person is not "acting" time, exactly as
                        # for a wait pause. ``_wall_clock_total`` is snapshotted
                        # before the loop and never re-derived, so extending the
                        # deadline alone left a 20-minute review counting
                        # against a 10-minute budget: the next iteration fired
                        # the "consolidate, stop exploring" nudge and told the
                        # user half their time was gone, with the whole
                        # extension still unspent.
                        _wait_paused_total += _paused_for
                        if on_approval_wait is not None:
                            # The caller owns the outer timeout; only it can
                            # move that one.
                            on_approval_wait(_paused_for)
                    # Checked again, not read off the race: a cancel landing
                    # with the answer hands the race back the answer.
                    if cancel_event is not None and cancel_event.is_set():
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "cancelled",
                                "error_kind": "CancelledMidCall",
                            }
                        )
                        logger.info(
                            "Approval wait ended by a cancel at iteration %d — "
                            "no proposed tool runs",
                            iteration + 1,
                        )
                        return _cancelled_chat_response(model, accumulated_usage)
                    from app.tools.approval import is_approval

                    # Same allow-list the applier uses. Bypassing the gate for a
                    # call whose verdict is not recognisably an approval would
                    # hand it a free pass on the way through.
                    _approved_now = {
                        cid
                        for cid, d in (_decisions or {}).items()
                        if is_approval((d or {}).get("decision"))
                    }
                    # Approved calls must not re-gate on the way through.
                    _bypass = getattr(executor, "bypass_approval_for", None)
                    if _bypass is not None:
                        _bypass(_approved_now)

                    from app.services.assignment_engine import (
                        _apply_approval_decisions,
                    )

                    await _apply_approval_decisions(
                        executor=executor,
                        messages=messages,
                        proposals=_proposals,
                        decisions=_decisions or {},
                        on_approve_always=on_approve_always,
                        outcomes=approval_outcomes,
                        cancel_event=cancel_event,
                    )
                    # A cancel during the approved calls ends the turn before
                    # the calls that passed the gate run.
                    if cancel_event is not None and cancel_event.is_set():
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "cancelled",
                                "error_kind": "CancelledMidCall",
                            }
                        )
                        return _cancelled_chat_response(model, accumulated_usage)
                    # Every *proposed* call now has a result — but a batch
                    # can mix them with calls that passed the gate (whitelisted,
                    # or covered by a standing approval). Those were never
                    # proposed, so nothing above gave them a result; clearing the
                    # whole list here dropped them, leaving their ``tool_use``
                    # blocks orphaned and the next request rejected outright by
                    # every provider. Dispatch them normally instead: the gate
                    # already cleared them, and the pause they waited through was
                    # never about them.
                    _proposed_ids = {p["tool_call_id"] for p in _proposals}
                    tool_calls = [c for c in tool_calls if c.id not in _proposed_ids]
                elif _proposals:
                    _names = ", ".join(p["tool_name"] for p in _proposals)
                    raise HumanInputRequestedError(
                        question=(
                            f"Approval required before running: {_names}"
                        ),
                        # First gated call, for compatibility with consumers
                        # that read a single id. Resume matches on the id inside
                        # each proposal, never on this one or on a tool name.
                        tool_call_id=_proposals[0]["tool_call_id"],
                        conversation=list(messages),
                        proposals=_proposals,
                    )

            # Near-duplicate reads that crossed the guard threshold in THIS
            # batch — (LLM-visible tool name, count); nudged after the batch,
            # next to the repeated-failure nudge.
            _near_dup_fired: list[tuple[str, int]] = []

            if len(tool_calls) == 1:
                _last_round_was_wait = False
                # Single tool call — execute directly (no gather overhead)
                tc = tool_calls[0]
                # ── Identical-call dedup (CORE-6) ─────────────────────
                # Keyed on the ORIGINAL tool name so sanitised aliases of
                # the same tool share one cache slot.  Exit tools are not
                # deduplicated — their side effect (early loop exit) must
                # always fire.
                _tc_orig_name = _tool_name_map.get(tc.name, tc.name)
                # A write-classified tool that actually runs invalidates the
                # whole read cache (see the ``finally`` below): any read cached
                # before this mutation could now be a stale pre-mutation
                # snapshot.  Computed up front so it is available on every exit
                # path of the execution ``try``.
                _tc_is_write = _is_write_tool(_tc_orig_name)
                # Whether that write TOOK EFFECT (ran, no error, not a no-op):
                # the loop breaker's read-count reset keys on this, never on
                # the classification alone - a failed, timed-out or ``No
                # change`` write mutated nothing a read would re-read.
                _tc_write_took_effect = False
                _tc_cache_key: tuple[str, str] | None = None
                if not _is_dedup_exempt(_tc_orig_name, _live_state_tools) and not (
                    exit_tool_names and _tc_orig_name in exit_tool_names
                ):
                    _tc_cache_key = _dedup_key(_tc_orig_name, tc.arguments, _cosmetic_arguments)
                    _fired = _count_near_duplicate(tc.name, _tc_cache_key)
                    if _fired is not None:
                        _near_dup_fired.append((tc.name, _fired))
                _cached_hit = _cached_read(_tc_cache_key)
                if _cached_hit is not None:
                    # The FULL payload, always: never a pointer at an
                    # earlier copy the curation may have stubbed.
                    content = _format_cached_result(_cached_hit.content, _cached_hit.age())
                    logger.info(
                        "Tool '%s' called again with identical arguments — "
                        "returning cached result without re-execution (id=%s)",
                        tc.name,
                        tc.id,
                    )
                    # The cache only ever holds SUCCESSFUL results, so a
                    # cache hit must clear the consecutive-failure tracking
                    # exactly like a real successful execution — otherwise a
                    # stale error count from an earlier failure (different
                    # arguments) could trigger the repeated-failure nudge
                    # even though the latest surfaced result succeeded.
                    _track_tool_result(tc.name, is_error=False)
                    # The files ride along: a replayed download IS the image,
                    # not a sentence saying the image is attached (the
                    # retention pass bounds them like any other result). Own
                    # list per message, so a later in-place edit of one
                    # replayed message can never reach into the cache entry.
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                            files=list(_cached_hit.files) if _cached_hit.files else None,
                        )
                    )
                    _record_breaker_answer(tc)
                    # A replay crosses the same vision boundary as the call
                    # it replays: an image the model has already been
                    # shown this turn is not re-sent, and one that would take
                    # the turn past the cap is refused with the budget note.
                    _admit_tool_result_images(messages[-1])
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, 0),
                            "tool": tc.name,
                            "success": True,
                            "cached": True,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(
                                tc.arguments
                            ),
                            "tool_output": _trim_trace_text(content, 30_000),
                            "tool_output_full": content,
                        },
                    )
                    _early = await _after_sibling_tools()
                    if _early is not None:
                        return _early
                    # This ``continue`` skips the post-batch nudge block, and a
                    # crossing served from the cache is exactly the re-read the
                    # guard exists for: inject here, after the replayed result.
                    await _inject_near_duplicate_nudges(_near_dup_fired)
                    await _inject_loop_breaker_nudges()
                    continue
                _tc_timeout = _tool_timeout_for(
                    tc.name, effective_tool_timeout, wall_clock_deadline, _declared_timeout(tc.name)
                )
                logger.info("Executing tool: %s (id=%s)", tc.name, tc.id)
                _tool_ctx = _lf_start_tool_span(
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    arguments=tc.arguments,
                    parent_ctx=_iter_ctx,
                )
                # Push the tool span on the contextvar so any sub-agent
                # spawned by the tool (consult/transfer/delegate) nests
                # under it — making the causal chain visible in Langfuse.
                _tool_token = _lf_set_ctx(_tool_ctx) if _tool_ctx else None
                # Bound BEFORE the handler's task is created below (a task
                # copies the context at creation); closed + reset in the
                # ``finally``, so a task the handler spawned and that outlives
                # the call cannot report on a call that already finished.
                _progress_binding = bind_tool_event_sink(
                    _progress_sink(tc),
                    tool_name=tc.name,
                    iteration=iteration + 1,
                    timeout=_tc_timeout,
                )
                _tc_started = time.perf_counter()
                try:
                    # The cap the result gets, for a tool that pages its own
                    # answer; the task below copies it.
                    with limiting_tool_results(max_tool_result_chars), spending_for_tool(_tool_spend), timing_tool_call(
                        _tc_timeout
                    ):
                        result = await _race_cancellation(
                            _await_with_heartbeat(
                                _execute_fenced(tc),
                                on_event=on_event,
                                tool_names=[tc.name],
                            ),
                            cancel_event,
                            timeout=_tc_timeout,
                        )
                except _CancelledMidCallError as _mc_exc:
                    # Mid-tool user cancel — the tool's asyncio task was
                    # cancelled and awaited by ``_race_cancellation`` (any
                    # external side effects it already performed stand, which
                    # matches user intent).  Produce the exact outcome of the
                    # iteration-start cancellation check.
                    _lf_end_tool_span(
                        _tool_ctx,
                        is_error=True,
                        error=_mc_exc,
                        error_kind="cancelled",
                    )
                    _iter_error = _mc_exc
                    _iter_outcome.update(
                        {
                            "terminal": True,
                            "stop_reason": "cancelled",
                            "error_kind": "CancelledMidCall",
                        }
                    )
                    logger.info(
                        "Tool '%s' aborted mid-execution — request cancelled "
                        "by user (id=%s)",
                        tc.name,
                        tc.id,
                    )
                    return _cancelled_chat_response(model, accumulated_usage)
                except TimeoutError as _tool_timeout_exc:
                    # Pass the exception object so the Langfuse facade
                    # renders 'TimeoutError: ...' consistently with the
                    # streaming/non-streaming LLM-call sites and the
                    # parallel branch below.
                    _lf_end_tool_span(
                        _tool_ctx,
                        is_error=True,
                        error=_tool_timeout_exc,
                        error_kind="timeout",
                    )
                    logger.warning(
                        "Tool '%s' timed out after %ss (id=%s%s)",
                        tc.name,
                        _tc_timeout,
                        tc.id,
                        _agent_tool_target_ctx(tc),
                    )
                    content = (
                        f"Error: tool '{tc.name}' timed out after "
                        f"{_tc_timeout}s. Try a simpler query or "
                        f"a different approach."
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                        )
                    )
                    _record_breaker_answer(tc)
                    _track_tool_result(tc.name, is_error=True, result_content=content)
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, _elapsed_ms(_tc_started)),
                            "tool": tc.name,
                            "success": False,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(
                                tc.arguments
                            ),
                            "tool_output": _trim_trace_text(content, 30_000),
                            "tool_output_full": content,
                        },
                    )
                    _early = await _after_sibling_tools()
                    if _early is not None:
                        return _early
                    continue
                except HumanInputRequestedError as hitl:
                    # HITL is flow-control, not an error: keep the
                    # human-readable status string so the trace lists it
                    # as 'human_input_requested' rather than the verbose
                    # exception repr.  The ``error_kind`` tag preserves
                    # filterability in Langfuse.
                    _lf_end_tool_span(
                        _tool_ctx,
                        is_error=True,
                        error="human_input_requested",
                        error_kind="human_input_requested",
                    )
                    hitl.tool_call_id = tc.id
                    hitl.conversation = list(messages)
                    raise
                except asyncio.CancelledError as _tool_cancel_exc:
                    # Cancellation mid-tool MUST record on the Langfuse
                    # tool span BEFORE propagating — otherwise the span
                    # stays open and the trace looks like a clean
                    # success.  Pass the exception object directly
                    # (``str(CancelledError())`` is empty) so the
                    # facade's ``_close_observation`` renders it as
                    # ``'CancelledError: ...'``.  Re-raise so the
                    # asyncio scheduler actually sees the cancellation;
                    # swallowing would break the structured-concurrency
                    # invariants the agent loop's outer awaits rely on.
                    # ``SystemExit`` / ``KeyboardInterrupt`` are
                    # intentionally NOT caught — they're terminating
                    # signals the rescue logic has no business
                    # rationalising (same convention adopted by the
                    # continuation / empty-response-retry rescue blocks
                    # in this file, see CodeQL ``py/catch-base-exception``
                    # rounds 4-5).
                    _lf_end_tool_span(
                        _tool_ctx,
                        is_error=True,
                        error=_tool_cancel_exc,
                        error_kind="cancelled",
                    )
                    raise
                except Exception as exc:
                    # Pass the exception object so the facade renders
                    # ``f'{type(exc).__name__}: {exc}'`` — preserves
                    # the type when the message is empty (some
                    # third-party tool errors have empty ``str()``)
                    # and matches the streaming / non-streaming
                    # generation-close sites.
                    _lf_end_tool_span(
                        _tool_ctx,
                        is_error=True,
                        error=exc,
                        error_kind="exception",
                    )
                    logger.error(
                        "Tool '%s' raised unexpected exception (id=%s%s): %s",
                        tc.name,
                        tc.id,
                        _agent_tool_target_ctx(tc),
                        format_exception_message(exc),
                        exc_info=True,
                    )
                    content = (
                        f"Error executing tool: {format_exception_message(exc)}"
                    )
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                        )
                    )
                    _record_breaker_answer(tc)
                    _track_tool_result(tc.name, is_error=True, result_content=content)
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, _elapsed_ms(_tc_started)),
                            "tool": tc.name,
                            "success": False,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(
                                tc.arguments
                            ),
                            "tool_output": _trim_trace_text(content, 30_000),
                            "tool_output_full": content,
                        },
                    )
                    _early = await _after_sibling_tools()
                    if _early is not None:
                        return _early
                    continue
                else:
                    # Success path — span close + result handling stay
                    # in ``else`` so the ``finally`` below runs after
                    # everything (including on ``continue`` / ``raise``
                    # in the ``except`` branches) and the contextvar is
                    # reset exactly once per call.
                    # A result the prompt guard withholds (blocking mode)
                    # replaces the tool's own from here on, and is
                    # never cached for a replay.
                    if _tool_screen is not None:
                        _screened_result = await _tool_screen.screen(
                            _tc_orig_name,
                            result,
                            truncate=lambda text: _truncate_tool_result(text, max_tool_result_chars),
                        )
                        if _screened_result is not result:
                            result, _tc_cache_key = _screened_result, None
                    # ── Tool-result fast path ─────────────────────────────
                    # Compute before closing the tool span so the decision is
                    # visible directly on the validator observation.
                    _terminal_decision = _terminal_tool_decision(
                        tool_name=tc.name,
                        result=result,
                        finalize_on_tool=finalize_on_tool,
                        tool_name_map=_tool_name_map,
                        output_schema=finalize_output_schema or structured_output,
                    )
                    if (
                        isinstance(finalize_on_tool, dict)
                        and _terminal_decision.reason != "not_configured"
                    ):
                        _decision_meta = {
                            "agent.terminal_tool_decision": _terminal_decision.reason,
                            "agent.terminal_tool": tc.name,
                            "agent.final_llm_call_skipped": (
                                _terminal_decision.final_content is not None
                            ),
                        }
                        if _terminal_decision.detail:
                            _decision_meta["agent.terminal_tool_decision_detail"] = (
                                _terminal_decision.detail[:500]
                            )
                        _lf_enrich_trace_metadata(_tool_ctx, _decision_meta)
                    _lf_end_tool_span(
                        _tool_ctx,
                        output={
                            "is_error": bool(result.is_error),
                            "content": _truncate_tool_result(
                                result.content, max_tool_result_chars
                            ),
                            "files_count": len(result.files or []),
                            **(
                                {"files": _lf_serialize_file_refs(result.files)}
                                if result.files
                                else {}
                            ),
                        },
                        is_error=bool(result.is_error),
                    )
                    _log_tool_result(tc, result, _probe_reasons.get(tc.id))
                    content = _truncate_tool_result(
                        result.content, max_tool_result_chars
                    )
                    content = await _with_read_back(tc, result, content)
                    # Capture output files from the tool into FileContext
                    if file_context and result.files:
                        for f_ref in result.files:
                            file_context.add(f_ref)
                    await _emit(
                        on_event,
                        {
                            "status": "tool_done",
                            **_tool_timing(report_tool_durations, _elapsed_ms(_tc_started)),
                            "tool": tc.name,
                            "success": not result.is_error,
                            "tool_input": _serialize_tool_arguments(tc.arguments),
                            "tool_input_full": _serialize_tool_arguments_full(
                                tc.arguments
                            ),
                            "tool_output": _trim_trace_text(content, 30_000),
                            "tool_output_full": result.content,
                        },
                    )
                    # ── Tool-result fast path ─────────────────────────────
                    # A trusted validator/finalizer can end the loop before
                    # its result is appended to history, avoiding the next
                    # LLM call that would only re-emit the same JSON.
                    if (
                        isinstance(finalize_on_tool, dict)
                        and _terminal_decision.reason != "not_configured"
                    ):
                        logger.info(
                            "Terminal tool decision for '%s': %s%s",
                            tc.name,
                            _terminal_decision.reason,
                            f" ({_terminal_decision.detail})"
                            if _terminal_decision.detail
                            else "",
                        )
                        _iter_outcome.update(
                            {
                                "terminal_tool": tc.name,
                                "terminal_tool_decision": _terminal_decision.reason,
                            }
                        )
                        if _terminal_decision.detail:
                            _iter_outcome["terminal_tool_decision_detail"] = (
                                _terminal_decision.detail[:500]
                            )
                        await _emit(
                            on_event,
                            {
                                "status": "terminal_tool_decision",
                                "tool": tc.name,
                                "reason": _terminal_decision.reason,
                                "detail": _terminal_decision.detail,
                            },
                        )
                    if _terminal_decision.final_content is not None:
                        logger.info(
                            "Agent loop: tool '%s' produced terminal output; "
                            "final LLM call skipped",
                            tc.name,
                        )
                        from app.llm.base import ChatResponse as _FinalizeResp

                        # Preserve the real model the provider reported, just
                        # like the normal ``return response`` paths do (the
                        # ``model`` parameter is only the requested override and
                        # may be ``None``).  This keeps run/trace model
                        # attribution identical between the fast path and the
                        # slow path.  ``stop_reason`` is set to ``finalize_tool``
                        # so the returned response matches the iteration-outcome
                        # metadata below instead of reporting the default
                        # ``end_turn`` for a loop that actually exited via the
                        # terminal-tool path.
                        _fin_response = _FinalizeResp(
                            content=_terminal_decision.final_content,
                            model=response.model or model or "unknown",
                            stop_reason="finalize_tool",
                        )
                        _fin_response.usage = accumulated_usage
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "finalize_tool",
                                "terminal_tool": tc.name,
                                "final_llm_call_skipped": True,
                                "final_output_source": "tool.bundle",
                                "content_preview": _terminal_decision.final_content[:500],
                            }
                        )
                        if on_token:
                            await _emit(on_event, {"status": "streaming"})
                            await _stream_text_chunked(
                                on_token,
                                _terminal_decision.final_content,
                            )
                        return _fin_response
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                            files=result.files,
                        )
                    )
                    _record_breaker_answer(tc)
                    await _record_send(tc, result)
                    _admit_tool_result_images(messages[-1])
                    _track_file_recovery(tc, messages[-1], _iter_outcome)
                    _tc_write_took_effect = (
                        _tc_is_write and not result.is_error and not getattr(result, "no_op", False)
                    )
                    if (
                        (_tc_orig_name in _surface_changing_tools or _changes_own_agent_tools(result))
                        and not result.is_error
                        and not getattr(result, "no_op", False)
                    ):
                        _tool_surface_changed = True
                    _track_tool_result(
                        tc.name,
                        is_error=result.is_error,
                        result_content=content,
                        target_throttled=getattr(result, "target_throttled", False),
                        outcome_call=(
                            _dedup_key(tc.name, tc.arguments, _cosmetic_arguments)
                            if getattr(result, "outcome_failure", False)
                            else None
                        ),
                        refused=getattr(result, "refused", False),
                    )
                    # Cache successful results for identical-call dedup
                    # (errors stay uncached so retries remain possible, and
                    # a transient answer - a state still being computed -
                    # too, so the next read reaches the settled state).
                    if (
                        _tc_cache_key is not None
                        and not result.is_error
                        and not getattr(result, "transient", False)
                        and not _is_error_shaped_result(content)
                        and len(tool_result_cache) < _DEDUP_CACHE_MAX_ENTRIES
                    ):
                        tool_result_cache[_tc_cache_key] = _CachedToolResult(
                            content, result.files, ttl=_cache_ttl(_tc_orig_name)
                        )
                finally:
                    # DRY: contextvar reset always runs regardless of
                    # which branch exited (success, timeout, HITL,
                    # cancellation, generic exc).  Previously each
                    # branch repeated this line; one missed branch and
                    # the contextvar would leak across tool calls.
                    reset_tool_event_sink(_progress_binding)
                    if _tool_token is not None:
                        _lf_reset_ctx(_tool_token)
                    # ── Invalidate the read cache after a write executes ──
                    # This ``finally`` is only reached once the tool actually
                    # ran (a dedup cache hit ``continue``s BEFORE the ``try``),
                    # so a write-classified tool here definitely executed.  A
                    # mutation may have invalidated any read cached earlier in
                    # the run, so drop the whole read cache: the next identical
                    # read then re-executes against fresh state instead of
                    # being served a stale pre-mutation snapshot.  Runs on the
                    # success path AND on error/timeout — a failed or timed-out
                    # mutation may still have applied server-side.  A write
                    # also resets the near-duplicate counts: re-reading after
                    # a mutation is legitimate, not a loop.
                    if _tc_is_write:
                        tool_result_cache.clear()
                        near_duplicate_counts.clear()
                    # The breaker's reset needs the write to have TAKEN EFFECT
                    # (the cache clear above stays conservative): a ``No
                    # change`` probe or a failed write between two identical
                    # reads mutated nothing, so the read count stands.
                    if _tc_write_took_effect and _loop_breaker_write_resets(_tc_orig_name):
                        loop_breaker.after_write({_tc_orig_name})
                    loop_breaker.after_call(_tc_orig_name, tc.arguments)
            else:
                _last_round_was_wait = False
                # Multiple tool calls — execute in parallel for speed
                # ── Identical-call dedup (CORE-6) ─────────────────────
                # Three cases per call: served from the per-run cache
                # (identical call already succeeded in a previous
                # iteration), duplicate of an earlier call in THIS batch
                # (executed once, result mirrored), or fresh — executed
                # normally below.
                _call_keys: list[tuple[str, str] | None] = []
                _cache_hit_indices: dict[int, _CachedToolResult] = {}  # index → cached
                _batch_dups: dict[int, int] = {}  # dup index → primary index
                _batch_first: dict[tuple[str, str], int] = {}
                _exec_indices: list[int] = []
                for _ci, tc in enumerate(tool_calls):
                    _ci_orig_name = _tool_name_map.get(tc.name, tc.name)
                    _ci_key: tuple[str, str] | None = None
                    if not _is_dedup_exempt(_ci_orig_name, _live_state_tools) and not (
                        exit_tool_names and _ci_orig_name in exit_tool_names
                    ):
                        _ci_key = _dedup_key(_ci_orig_name, tc.arguments, _cosmetic_arguments)
                        _fired = _count_near_duplicate(tc.name, _ci_key)
                        if _fired is not None:
                            _near_dup_fired.append((tc.name, _fired))
                    _call_keys.append(_ci_key)
                    _ci_cached = _cached_read(_ci_key)
                    if _ci_cached is not None:
                        _cache_hit_indices[_ci] = _ci_cached
                        continue
                    if _ci_key is not None and _ci_key in _batch_first:
                        _batch_dups[_ci] = _batch_first[_ci_key]
                        continue
                    if _ci_key is not None:
                        _batch_first[_ci_key] = _ci
                    _exec_indices.append(_ci)

                for _ci in _exec_indices:
                    tc = tool_calls[_ci]
                    logger.info("Executing tool (parallel): %s (id=%s)", tc.name, tc.id)

                # Each call's own time, and the group the calls that ran together
                # share: a batch with one call left to run ran it alone.
                _par_elapsed: dict[int, int] = {}
                _par_group = (
                    uuid_module.uuid4().hex[:12]
                    if report_tool_durations and len(_exec_indices) > 1
                    else None
                )

                async def _exec_with_span(
                    tc: ToolCall,
                    timeout: float,
                    index: int,
                    _iter_ctx=_iter_ctx,
                    _step=iteration + 1,
                    _elapsed=_par_elapsed,
                ):
                    started = time.perf_counter()
                    tool_ctx = _lf_start_tool_span(
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        arguments=tc.arguments,
                        parent_ctx=_iter_ctx,
                    )
                    # Push the tool span on the contextvar so any sub-agent
                    # spawned by the tool nests under it.  Tasks created by
                    # asyncio.gather copy the contextvar at task creation,
                    # so each parallel tool gets its own correct parent.
                    tool_token = _lf_set_ctx(tool_ctx) if tool_ctx else None
                    # Same isolation for the tool's own progress events: this
                    # runs inside the gathered task, so the sink is this
                    # call's alone.
                    progress_binding = bind_tool_event_sink(
                        _progress_sink(tc),
                        tool_name=tc.name,
                        iteration=_step,
                        timeout=timeout,
                    )
                    try:
                        with limiting_tool_results(max_tool_result_chars), spending_for_tool(
                            _tool_spend
                        ), timing_tool_call(timeout):
                            result = await _race_cancellation(
                                _execute_fenced(tc),
                                cancel_event,
                                timeout=timeout,
                            )
                    except _CancelledMidCallError as _mc_exc:
                        # Mid-tool user cancel — close the span and re-raise
                        # so the outer ``asyncio.gather`` returns it for
                        # caller-side terminal handling (mirrors HITL).
                        _lf_end_tool_span(
                            tool_ctx,
                            is_error=True,
                            error=_mc_exc,
                            error_kind="cancelled",
                        )
                        raise
                    except TimeoutError as _par_timeout_exc:
                        # Pass the exception object so the Langfuse
                        # facade renders 'TimeoutError: ...' consistently
                        # with the single-tool branch above and every
                        # other generation-close site in this file.
                        _lf_end_tool_span(
                            tool_ctx,
                            is_error=True,
                            error=_par_timeout_exc,
                            error_kind="timeout",
                        )
                        raise
                    except HumanInputRequestedError:
                        # HITL is flow-control, not an error: keep the
                        # human-readable status string (matches the
                        # single-tool branch above).  The exception
                        # itself is re-raised so the outer
                        # ``asyncio.gather`` returns it for caller-side
                        # handover handling.
                        _lf_end_tool_span(
                            tool_ctx,
                            is_error=True,
                            error="human_input_requested",
                            error_kind="human_input_requested",
                        )
                        raise
                    except asyncio.CancelledError as _par_cancel_exc:
                        # Cancellation must record on the Langfuse tool
                        # span BEFORE propagating, otherwise the span
                        # stays open and the trace looks like a clean
                        # success.  Pass the exception object directly
                        # (``str(CancelledError())`` is empty).  Split
                        # out from the previous single ``except
                        # BaseException`` to satisfy CodeQL
                        # ``py/catch-base-exception`` (same convention
                        # adopted in rounds 4-5 for the continuation /
                        # empty-response-retry rescue blocks).
                        # ``SystemExit`` / ``KeyboardInterrupt`` are
                        # NOT caught here — they're handled at the
                        # outer ``gather`` site (line ~2609) via
                        # ``isinstance(result, (KeyboardInterrupt,
                        # SystemExit)): raise result``.
                        _lf_end_tool_span(
                            tool_ctx,
                            is_error=True,
                            error=_par_cancel_exc,
                            error_kind="cancelled",
                        )
                        raise
                    except Exception as exc:
                        # Pass the exception object so the facade
                        # renders ``f'{type(exc).__name__}: {exc}'`` —
                        # preserves the type when ``str(exc)`` is
                        # empty (some third-party errors), without
                        # the ``str(exc) or type(exc).__name__``
                        # fallback that loses type info when the
                        # message is non-empty.
                        _lf_end_tool_span(
                            tool_ctx,
                            is_error=True,
                            error=exc,
                            error_kind="exception",
                        )
                        raise
                    finally:
                        _elapsed[index] = _elapsed_ms(started)
                        reset_tool_event_sink(progress_binding)
                        if tool_token is not None:
                            _lf_reset_ctx(tool_token)
                    _lf_end_tool_span(
                        tool_ctx,
                        output={
                            "is_error": bool(result.is_error),
                            "content": _truncate_tool_result(
                                result.content, max_tool_result_chars
                            ),
                            "files_count": len(result.files or []),
                            **(
                                {"files": _lf_serialize_file_refs(result.files)}
                                if result.files
                                else {}
                            ),
                        },
                        is_error=bool(result.is_error),
                    )
                    return result

                # Kept per call: a timeout below must report the cap the call
                # ran under, not one recomputed after the batch returned.
                _par_timeouts = {
                    _ci: _tool_timeout_for(
                        tool_calls[_ci].name,
                        effective_tool_timeout,
                        wall_clock_deadline,
                        _declared_timeout(tool_calls[_ci].name),
                    )
                    for _ci in _exec_indices
                }
                # A stateful tool's calls of this round wait for the one sent before them (#12182).
                _order_chain = _call_order_chain(
                    [(_ci, _tool_name_map.get(tool_calls[_ci].name, tool_calls[_ci].name)) for _ci in _exec_indices],
                    _ordered_call_tools,
                )
                gathered = await _await_with_heartbeat(
                    asyncio.gather(
                        *[
                            _in_call_order(
                                _order_chain.get(_ci),
                                functools.partial(_exec_with_span, tool_calls[_ci], _par_timeouts[_ci], _ci),
                            )
                            for _ci in _exec_indices
                        ],
                        return_exceptions=True,
                    ),
                    on_event=on_event,
                    tool_names=[tool_calls[_ci].name for _ci in _exec_indices],
                )
                results: list[Any] = [None] * len(tool_calls)
                for _ci, _gr in zip(_exec_indices, gathered, strict=True):
                    results[_ci] = _gr
                # Before the mirrors below: a result the prompt guard withholds
                # (blocking mode) is what its duplicates mirror, and it
                # is never cached. A call that raised is not a result.
                if _tool_screen is not None:
                    _withheld_results = await _tool_screen.screen_batch(
                        [
                            (
                                _ci,
                                _tool_name_map.get(tool_calls[_ci].name, tool_calls[_ci].name),
                                results[_ci],
                            )
                            for _ci in _exec_indices
                            if not isinstance(results[_ci], BaseException)
                        ],
                        truncate=lambda text: _truncate_tool_result(text, max_tool_result_chars),
                    )
                    for _ci, _withheld_result in _withheld_results.items():
                        results[_ci] = _withheld_result
                        _call_keys[_ci] = None
                # Cache hits from previous iterations — files mirrored so a
                # replayed download is the image, not a claim about one.
                for _ci, _cached in _cache_hit_indices.items():
                    results[_ci] = _DedupedCall(
                        _format_cached_result(_cached.content, _cached.age()), files=_cached.files
                    )
                    logger.info(
                        "Tool '%s' called again with identical arguments — "
                        "returning cached result without re-execution (id=%s)",
                        tool_calls[_ci].name,
                        tool_calls[_ci].id,
                    )
                # Intra-batch duplicates mirror their primary's outcome.
                # The primary always precedes the duplicate in the zip
                # below, so fatal exceptions (HITL/KI/SE) raise on the
                # primary before the duplicate is ever processed.
                for _dup_idx, _primary_idx in _batch_dups.items():
                    _primary = results[_primary_idx]
                    if isinstance(_primary, BaseException):
                        results[_dup_idx] = _DedupedCall(
                            f"{_DEDUP_BATCH_FAILURE_MARKER} The identical call in "
                            f"this batch failed: {_primary}",
                            success=False,
                        )
                    else:
                        _primary_content = _truncate_tool_result(
                            _primary.content, max_tool_result_chars
                        )
                        # Mirror the primary's outcome, but treat an error-shaped
                        # result (an upstream 401/5xx an integration RETURNED as a
                        # normal is_error=False string, e.g. OpenAEV's _api) as a
                        # FAILURE mirror. Otherwise the duplicate would read as a
                        # success and clear the consecutive-failure tracking on a
                        # call that actually failed — the same trap the
                        # cross-iteration cache already guards against with
                        # _is_error_shaped_result below.
                        _primary_ok = not _primary.is_error and not (
                            _is_error_shaped_result(_primary_content)
                        )
                        results[_dup_idx] = _DedupedCall(
                            _format_cached_result(_primary_content),
                            success=_primary_ok,
                            files=_primary.files if _primary_ok else None,
                        )
                    logger.info(
                        "Tool '%s' deduplicated within parallel batch — "
                        "mirroring the identical call's result (id=%s)",
                        tool_calls[_dup_idx].name,
                        tool_calls[_dup_idx].id,
                    )
                # Original names of the writes that TOOK EFFECT this batch
                # (ran, no error, not a no-op): what the loop breaker's
                # read-count reset keys on.
                _effective_writes: list[str] = []
                for _ri, (tc, result) in enumerate(
                    zip(tool_calls, results, strict=True)
                ):
                    if isinstance(result, _DedupedCall):
                        content = result.content
                        # Mirror the success/failure semantics of a real
                        # execution: a deduplicated SUCCESS (cache hits are
                        # always successes; intra-batch mirrors of a
                        # successful primary) clears the consecutive-failure
                        # tracking so a stale error count can't trigger the
                        # repeated-failure nudge.  Mirrored FAILURES are NOT
                        # tracked here — the primary's own result branch
                        # already counted that failure once, and counting the
                        # mirror too would double-count a single execution.
                        if result.success:
                            _track_tool_result(tc.name, is_error=False)
                        await _emit(
                            on_event,
                            {
                                "status": "tool_done",
                                **_tool_timing(report_tool_durations, 0),
                                "tool": tc.name,
                                "success": result.success,
                                "cached": True,
                                "tool_input": _serialize_tool_arguments(tc.arguments),
                                "tool_input_full": _serialize_tool_arguments_full(
                                    tc.arguments
                                ),
                                "tool_output": _trim_trace_text(content, 30_000),
                                "tool_output_full": content,
                            },
                        )
                    elif isinstance(result, HumanInputRequestedError):
                        result.tool_call_id = tc.id
                        result.conversation = list(messages)
                        raise result
                    elif isinstance(result, (KeyboardInterrupt, SystemExit)):
                        raise result
                    elif isinstance(result, _CancelledMidCallError):
                        # User cancelled while parallel tools were running —
                        # every sibling race fired too (same event), so no
                        # in-flight task is left behind.  Produce the exact
                        # outcome of the iteration-start cancellation check.
                        _iter_error = result
                        _iter_outcome.update(
                            {
                                "terminal": True,
                                "stop_reason": "cancelled",
                                "error_kind": "CancelledMidCall",
                            }
                        )
                        logger.info(
                            "Parallel tool execution aborted at iteration %d — "
                            "request cancelled by user",
                            iteration + 1,
                        )
                        return _cancelled_chat_response(model, accumulated_usage)
                    elif isinstance(result, (asyncio.TimeoutError, TimeoutError)):
                        _par_timeout = _par_timeouts[_ri]
                        logger.warning(
                            "Tool '%s' timed out after %ss (id=%s%s)",
                            tc.name,
                            _par_timeout,
                            tc.id,
                            _agent_tool_target_ctx(tc),
                        )
                        content = (
                            f"Error: tool '{tc.name}' timed out after "
                            f"{_par_timeout}s. Try a simpler query or "
                            f"a different approach."
                        )
                        _track_tool_result(tc.name, is_error=True, result_content=content)
                        await _emit(
                            on_event,
                            {
                                "status": "tool_done",
                                **_tool_timing(report_tool_durations, _par_elapsed.get(_ri, 0), _par_group),
                                "tool": tc.name,
                                "success": False,
                                "tool_input": _serialize_tool_arguments(tc.arguments),
                                "tool_input_full": _serialize_tool_arguments_full(
                                    tc.arguments
                                ),
                                "tool_output": _trim_trace_text(content, 30_000),
                                "tool_output_full": content,
                            },
                        )
                    elif isinstance(result, BaseException):
                        logger.error(
                            "Tool '%s' raised exception (id=%s%s): %s",
                            tc.name,
                            tc.id,
                            _agent_tool_target_ctx(tc),
                            format_exception_message(result),
                            exc_info=result,
                        )
                        content = (
                            f"Error executing tool: {format_exception_message(result)}"
                        )
                        _track_tool_result(tc.name, is_error=True, result_content=content)
                        await _emit(
                            on_event,
                            {
                                "status": "tool_done",
                                **_tool_timing(report_tool_durations, _par_elapsed.get(_ri, 0), _par_group),
                                "tool": tc.name,
                                "success": False,
                                "tool_input": _serialize_tool_arguments(tc.arguments),
                                "tool_input_full": _serialize_tool_arguments_full(
                                    tc.arguments
                                ),
                                "tool_output": _trim_trace_text(content, 30_000),
                                "tool_output_full": content,
                            },
                        )
                    else:
                        _log_tool_result(tc, result, _probe_reasons.get(tc.id))
                        content = _truncate_tool_result(
                            result.content, max_tool_result_chars
                        )
                        content = await _with_read_back(tc, result, content)
                        # Capture output files from the tool into FileContext
                        if file_context and result.files:
                            for f_ref in result.files:
                                file_context.add(f_ref)
                        await _record_send(tc, result)
                        _result_orig_name = _tool_name_map.get(tc.name, tc.name)
                        if (
                            _is_write_tool(_result_orig_name)
                            and not result.is_error
                            and not getattr(result, "no_op", False)
                        ):
                            _effective_writes.append(_result_orig_name)
                        if (
                            (_result_orig_name in _surface_changing_tools or _changes_own_agent_tools(result))
                            and not result.is_error
                            and not getattr(result, "no_op", False)
                        ):
                            _tool_surface_changed = True
                        _track_tool_result(
                            tc.name,
                            is_error=result.is_error,
                            result_content=content,
                            target_throttled=getattr(result, "target_throttled", False),
                            outcome_call=(
                                _dedup_key(tc.name, tc.arguments, _cosmetic_arguments)
                                if getattr(result, "outcome_failure", False)
                                else None
                            ),
                            refused=getattr(result, "refused", False),
                        )
                        # Cache successful results for identical-call dedup
                        # (errors and transient answers stay uncached).
                        _ri_key = _call_keys[_ri]
                        if (
                            _ri_key is not None
                            and not result.is_error
                            and not getattr(result, "transient", False)
                            and not _is_error_shaped_result(content)
                            and len(tool_result_cache) < _DEDUP_CACHE_MAX_ENTRIES
                        ):
                            tool_result_cache[_ri_key] = _CachedToolResult(
                                content, result.files, ttl=_cache_ttl(_ri_key[0])
                            )
                        await _emit(
                            on_event,
                            {
                                "status": "tool_done",
                                **_tool_timing(report_tool_durations, _par_elapsed.get(_ri, 0), _par_group),
                                "tool": tc.name,
                                "success": not result.is_error,
                                "tool_input": _serialize_tool_arguments(tc.arguments),
                                "tool_input_full": _serialize_tool_arguments_full(
                                    tc.arguments
                                ),
                                "tool_output": _trim_trace_text(content, 30_000),
                                "tool_output_full": result.content,
                            },
                        )
                    # A real result and a deduplicated mirror both carry their
                    # files (``_DedupedCall.files`` is the mirrored primary's /
                    # the cached call's); only a failure carries none.
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=content,
                            tool_call_id=tc.id,
                            name=tc.name,
                            files=(
                                None
                                if isinstance(result, BaseException)
                                else result.files
                            ),
                        )
                    )
                    _record_breaker_answer(tc)
                    if not isinstance(result, BaseException):
                        _admit_tool_result_images(messages[-1])
                    if not isinstance(result, (BaseException, _DedupedCall)):
                        _track_file_recovery(tc, messages[-1], _iter_outcome)

                # ── Invalidate the read cache after any write executes ──
                # Write-classified tools are dedup-exempt, so they are never
                # served from the cache — they always land in ``_exec_indices``
                # (i.e. they actually ran this batch).  If any of them did,
                # drop the WHOLE read cache after the batch so nothing cached
                # this batch or earlier — possibly a pre-mutation snapshot —
                # leaks into the next iteration.  The full ``.clear()`` is
                # correct and cheap: writes are far rarer than reads, so
                # re-executing a few reads is negligible next to correctness.
                _executed_names = [
                    _tool_name_map.get(tool_calls[_ci].name, tool_calls[_ci].name)
                    for _ci in _exec_indices
                ]
                if any(_is_write_tool(_name) for _name in _executed_names):
                    tool_result_cache.clear()
                    near_duplicate_counts.clear()
                if any(_loop_breaker_write_resets(_name) for _name in _effective_writes):
                    loop_breaker.after_write(_effective_writes)
                for _ci in _exec_indices:
                    loop_breaker.after_call(
                        _tool_name_map.get(tool_calls[_ci].name, tool_calls[_ci].name),
                        tool_calls[_ci].arguments,
                    )

            # ── Tool surface refresh (#1874) ──────────────────────
            # A call of this step changed an integration the run is bound to:
            # the next request carries the reloaded tool list.
            if _tool_surface_changed:
                _tool_surface_changed = False
                _surface_note = await _refresh_tool_surface()
                if _surface_note is not None:
                    messages.append(_surface_note)

            # Mixed wait+work: pause only after sibling tools have run.
            _early = await _after_sibling_tools()
            if _early is not None:
                return _early

            # ── Iteration waiver for wait-only rounds ─────────────
            # Waiting is not acting: a round whose every tool call only waited
            # gets its iteration refunded, which is what stops a 10-minute
            # background task from consuming a 25-iteration budget in short
            # polls and hitting the forced final answer while the delegated work
            # it is waiting for is still running.  The round must have actually
            # spent time waiting — an instantly-satisfied ``check_task_status``
            # is real progress and is charged like any other call.
            if (
                time.monotonic() - _tool_round_started >= MIN_GRANTED_WAIT_SECONDS
                and _is_wait_only_round(tool_calls, _tool_name_map)
            ):
                _waive_wait_round(", ".join(sorted({tc.name for tc in tool_calls})))
                _forget_reads_after_pause()

            # ── Early exit for terminal tools (e.g. transfer_to_agent) ────
            # When a tool in exit_tool_names was executed, skip the next LLM
            # call entirely — the caller handles the side-effect (e.g. running
            # the target agent).  This avoids a redundant "I've transferred
            # you" message that blocks the flow.
            if exit_tool_names:
                executed_names = {tc.name for tc in tool_calls}
                _orig_executed = {_tool_name_map.get(n, n) for n in executed_names}
                if _orig_executed & exit_tool_names:
                    logger.info(
                        "Terminal tool executed (%s) — exiting agent loop early",
                        _orig_executed & exit_tool_names,
                    )
                    from app.llm.base import ChatResponse

                    response_obj = ChatResponse(content="", model=model or "unknown")
                    response_obj.usage = accumulated_usage
                    return response_obj

            # ── Consecutive-same-tool-failure brake (P4.1) ────────────────
            # The nudge + cooldown below only *suggest* a different approach;
            # once one tool has failed _TOOL_FAILURE_BRAKE_THRESHOLD times in
            # a row, force the Stage-2 wind-down (tools stripped from the
            # next iteration on) so the model must produce an honest final
            # answer instead of burning the whole iteration budget on the
            # same broken call (the 2026-07-09 incident pattern).
            if not tool_failure_brake:
                _brake = next(
                    (
                        (t, c)
                        for t, c in [
                            *tool_error_counts.items(),
                            *((call[0], c) for call, c in outcome_error_counts.items()),
                        ]
                        if c >= _TOOL_FAILURE_BRAKE_THRESHOLD
                    ),
                    None,
                )
                _rejected = (
                    next(
                        ((t, c) for t, c in outcome_tool_streaks.items() if c >= _OUTCOME_FAILURE_BRAKE_THRESHOLD),
                        None,
                    )
                    if _brake is None
                    else None
                )
                if _rejected is not None:
                    tool_failure_brake = True
                    _rejected_tool, _rejected_count = _rejected
                    logger.warning(
                        "Tool '%s' had %d rejected results in a row (each asked "
                        "differently) — engaging the tool-failure brake (iteration %d/%d)",
                        _rejected_tool,
                        _rejected_count,
                        iteration + 1,
                        max_iterations,
                    )
                    messages.append(
                        LLMMessage(
                            role="system",
                            content=_OUTCOME_FAILURE_BRAKE_NUDGE.format(
                                tool_name=_rejected_tool, count=_rejected_count
                            ),
                            wind_down="rejected_work_brake",
                        )
                    )
                    await _emit_budget_warning(
                        on_event,
                        (
                            f"⚠️ {_rejected_count} attempts of '{_rejected_tool}' came "
                            "out rejected. I will stop retrying and summarize what "
                            "came out and what went wrong."
                        ),
                    )
                if _brake is not None:
                    tool_failure_brake = True
                    _brake_tool, _brake_count = _brake
                    logger.warning(
                        "Tool '%s' failed %d consecutive time(s) — engaging "
                        "the tool-failure brake: tools are withdrawn and the "
                        "loop winds down to a final answer (iteration %d/%d)",
                        _brake_tool,
                        _brake_count,
                        iteration + 1,
                        max_iterations,
                    )
                    messages.append(
                        LLMMessage(
                            role="system",
                            wind_down="tool_failure_brake",
                            content=_TOOL_FAILURE_BRAKE_NUDGE.format(
                                tool_name=_brake_tool,
                                count=_brake_count,
                            ),
                        )
                    )
                    await _emit_budget_warning(
                        on_event,
                        (
                            f"⚠️ The tool '{_brake_tool}' keeps failing "
                            f"({_brake_count} turns in a row). I will stop retrying "
                            "and summarize what I found and what went wrong."
                        ),
                    )

            # ── Repeated-failure nudge ────────────────────────────────────
            # After processing all tool results for this iteration, check if
            # any tool has hit the failure threshold and inject a system hint.
            # Also put the tool on cooldown so the LLM physically cannot call
            # it again (LLMs often ignore system nudges and keep retrying).
            for tname, count in tool_error_counts.items():
                if count >= _MAX_TOOL_FAILURES and tname not in tool_nudged:
                    logger.warning(
                        "Tool '%s' failed %d time(s) — injecting repeated-failure nudge",
                        tname,
                        count,
                    )
                    messages.append(
                        LLMMessage(
                            role="system",
                            content=_REPEATED_FAILURE_NUDGE.format(
                                tool_name=tname,
                                count=count,
                            ),
                        )
                    )
                    tool_nudged.add(tname)
                    if (
                        tname not in tool_cooldown_until
                        or tool_cooldown_until[tname] <= iteration
                    ):
                        # Same ``+ 1`` arithmetic as ``_track_tool_result``:
                        # ``_tools_not_in_cooldown`` re-offers the tool once
                        # ``cooldown[name] <= iteration``, so withholding it
                        # for N full future iterations needs ``iteration + N
                        # + 1``.
                        tool_cooldown_until[tname] = (
                            iteration + _RATE_LIMIT_COOLDOWN_ITERS + 1
                        )
            # The same nudge for one call whose work keeps coming out
            # rejected, without the cooldown: that would also take away the
            # retry the model fixes (another prompt, another model).
            for call, count in outcome_error_counts.items():
                if count >= _MAX_TOOL_FAILURES and call not in outcome_nudged:
                    messages.append(
                        LLMMessage(
                            role="system",
                            content=_REPEATED_FAILURE_NUDGE.format(tool_name=call[0], count=count),
                        )
                    )
                    outcome_nudged.add(call)

            # ── Near-duplicate read nudge ─────────────────────────────────
            # A read tool called more than _MAX_NEAR_DUPLICATE_CALLS times
            # with the same effective arguments (cosmetic output-naming
            # arguments removed) and no write in between is a model re-fetching
            # what it already has — the trace 722375f1 pattern, where a
            # dropped image was re-downloaded 28 times under new filenames.
            # One-shot per tool, steering only: the call already ran (or was
            # served from the cache); nothing here blocks the next one, so a
            # legitimate re-read after context clearing still works. (The
            # sequential cache-hit path injects through the same helper before
            # it ``continue``s past this block.)
            await _inject_near_duplicate_nudges(_near_dup_fired)
            # ── Loop-breaker warnings ─────────────────────────────
            # Queued by the pre-dispatch screen; injected after the results so
            # the assistant's tool calls and their results stay adjacent.
            await _inject_loop_breaker_nudges()

            # ── Capability-not-found nudge ────────────────────────────────
            # When the LLM has tried _MAX_NOT_AVAILABLE_DISTINCT_NAMES
            # different tool names that are all "not available in this
            # context" (ScopedToolExecutor rejections and composite not-found
            # errors alike), it is cycling through
            # hallucinated variants of the same capability.  Inject a single,
            # strong corrective message that lists the tools that ARE
            # available, so the LLM can either pick the right one or
            # conclude the capability does not exist and give up gracefully.
            # This fires at most once per run to avoid message-list noise.
            if (
                len(not_available_tool_names) >= _MAX_NOT_AVAILABLE_DISTINCT_NAMES
                and not not_available_nudge_injected
            ):
                # What the NEXT request will offer — read live, not from this
                # iteration's snapshot: a ``load_tools`` call in this very
                # batch may have unloaded a group, and naming an evicted tool
                # as "available" would send the model straight into another
                # not-available refusal.
                _avail = sorted(t.name for t in (_offer_at(iteration + 1) or []))
                _tried = sorted(not_available_tool_names)
                logger.warning(
                    "LLM tried %d distinct not-available tool name(s) (%s) — "
                    "injecting capability-not-found nudge",
                    len(_tried),
                    ", ".join(_tried),
                )
                # Both listings share the executors' cap (``... and N
                # more``) so the one-shot nudge stays small even for
                # agents with 100+ bound tools or a pathological run
                # that batched many hallucinated names before the nudge.
                messages.append(
                    LLMMessage(
                        role="system",
                        content=_CAPABILITY_NOT_FOUND_NUDGE.format(
                            count=len(_tried),
                            tried_names=format_tool_listing(
                                [f"'{n}'" for n in _tried]
                            ),
                            available_tools=format_tool_listing(_avail),
                        ),
                    )
                )
                not_available_nudge_injected = True

        except asyncio.CancelledError as _exc:
            # Split from the generic ``except Exception`` below so
            # ``SystemExit`` / ``KeyboardInterrupt`` pass through
            # uninstrumented at the iteration boundary (CodeQL
            # ``py/catch-base-exception`` convention applied across the
            # loop's failure sites — process-terminating exceptions must
            # never be caught by tracing instrumentation, even when the
            # catcher re-raises).
            _iter_error = _exc
            raise
        except Exception as _exc:
            _iter_error = _exc
            raise
        finally:
            # Hand this iteration's worst-case reservation back: the
            # real spend was already debited by ``_merge_usage``.
            if _budget is not None:
                _budget.release(_iter_reservation)
            _lf_end_span(
                _iter_ctx,
                output=_iter_outcome or None,
                error=_iter_error,
            )
            if _iter_token is not None:
                with contextlib.suppress(Exception):
                    _lf_reset_ctx(_iter_token)

    # ── Forced final answer (Stage 3 — backstop) ────────────────────
    # Reached when: max_iterations exhausted, mid-loop empty-response retry
    # failed, or the streaming-no-tools branch returned empty content.
    # Stage 2 (tool stripping) prevents this path in most healthy runs.
    # ``iteration`` is initialised to 0 above the loop and rebound each
    # turn — no introspection needed.
    logger.warning(
        "Agent loop entered forced-final-answer backstop after %d iteration(s) "
        "(max=%d) — attempting to synthesise final response",
        iteration + 1,
        max_iterations,
    )
    # A tool-free iteration at the cap that broke out here (an empty answer
    # the retry could not rescue) ended on the iteration budget too.
    if _limit_stop is None and _iteration_limit_strip:
        _limit_stop = ITERATION_LIMIT_STOP_REASON
    _offer_checkpoint()
    # Mirror the iteration-start cancellation check: when the user already
    # cancelled, synthesising a forced answer would be wasted work delivered
    # to nobody.  Mid-call aborts below are handled by the
    # ``_CancelledMidCallError`` handlers on each LLM call.
    if cancel_event is not None and cancel_event.is_set():
        logger.info("Agent loop cancelled before forced-final-answer backstop")
        return _cancelled_chat_response(model, accumulated_usage)

    # ── Wall-clock deadline guard ─────────────────────────────────
    # When a wall_clock_deadline is set, cap each forced-answer LLM call to
    # the remaining budget so the outer asyncio.wait_for never fires mid-call
    # and leaves the run record with a hard timeout error and no output.
    def _remaining_forced_timeout(fraction: float = 1.0) -> float:
        """Return the timeout (s) to use for the next forced-answer LLM call.

        ``fraction`` reserves part of the post-reserve remaining budget for a
        FOLLOW-UP call: the pre-synthesis compaction passes ``0.5`` so its
        best-effort summariser can never starve the synthesis call that must
        run after it (a timed-out summariser just degrades to the
        deterministic pruner).
        """
        if wall_clock_deadline is None:
            return _LLM_CALL_TIMEOUT
        remaining = wall_clock_deadline - time.monotonic()
        capped = (remaining - _FORCED_ANSWER_DEADLINE_RESERVE_SECS) * fraction
        return min(max(capped, 1.0), _LLM_CALL_TIMEOUT)

    # Checked BEFORE the forced-answer system message and the pre-synthesis
    # compaction: ``_compact_for_empty_recovery`` can itself make a summariser
    # LLM call with the full ``_LLM_CALL_TIMEOUT`` — with a nearly-expired
    # budget that call would be the one the outer deadline cancels mid-flight.
    if wall_clock_deadline is not None:
        _wc_remaining = wall_clock_deadline - time.monotonic()
        if _wc_remaining < _FORCED_ANSWER_MIN_REMAINING_SECS:
            # Insufficient budget — return a pre-built fallback immediately
            # rather than attempting a doomed LLM call that the outer
            # asyncio.wait_for would cancel mid-flight, yielding a hard
            # TimeoutError with no output.
            logger.warning(
                "Forced-final-answer backstop: only %.1fs remain "
                "(< %ds minimum) — returning immediate fallback to avoid "
                "outer deadline cancellation",
                _wc_remaining,
                _FORCED_ANSWER_MIN_REMAINING_SECS,
            )
            # TRANSCRIPT-ONLY on purpose: this branch exists because the
            # deadline is nearly upon us and its contract is to return
            # immediately. The label-resolving digest opens a database
            # session for a cosmetic name; if the pool or the query stalled
            # past the remaining seconds, the outer asyncio.wait_for would
            # cancel the loop and the fallback would produce NO output — the
            # exact failure this shortcut is here to avoid.
            synthesised = summarise_tool_results(messages)
            if synthesised:
                _immediate_fallback = (
                    f"{FORCED_ANSWER_TIMEOUT_MESSAGE}\n\nPartial results:\n\n{synthesised}"
                )
            else:
                _immediate_fallback = f"{FORCED_ANSWER_TIMEOUT_MESSAGE} {EMPTY_RESPONSE_FALLBACK}"
            if on_token:
                # Mirror the normal backstop path: announce streaming before
                # tokens flow so the frontend swaps the working-state UI for
                # the live answer stream.
                await _emit(on_event, {"status": "streaming"})
                await _stream_text_chunked(on_token, _immediate_fallback)
            from app.llm.base import ChatResponse

            # Carry the accumulated token usage so run accounting survives
            # the shortcut exactly as it does on the normal backstop exit.
            # The wall clock is what left no room for a synthesis, so it is
            # the limit named unless the iteration budget ran out first.
            return _with_limit_stop(
                ChatResponse(
                    content=_immediate_fallback,
                    model=model or "unknown",
                    usage=accumulated_usage,
                ),
                _limit_stop or DEADLINE_STOP_REASON,
            )

    _synthesis_instructions = [
        LLMMessage(role="system", content=_FORCED_FINAL_ANSWER, wind_down="iteration_backstop")
    ]
    if _budget_violation is not None:
        # Let the synthesis itself explain the early stop (see the notice's
        # definition) instead of prefixing its output after the fact.
        _synthesis_instructions.append(
            LLMMessage(
                role="system",
                content=_BUDGET_STOP_NOTICE.format(
                    reason=_budget_violation.user_message()
                ),
                wind_down="budget_stop",
            )
        )
    # What refused the synthesis at its full allowance, when it was sized
    # down to what is left instead.
    _sized_synthesis_violation: BudgetViolation | None = None
    # The synthesis is priced on its own: the last iteration's sizing does
    # not carry over to it.
    _chain_sized = False
    _last_refusal = None

    # ── Per-run budget guard ──────────────────────────────────────
    # The mirror image of the wall-clock guard above, in money/tokens instead
    # of seconds.  The in-loop gate reserved a slice of the budget precisely so
    # this synthesis could run, so it is priced against the FULL limit
    # (``final=True``), and made with the largest ``max_tokens`` that limit
    # still covers when it cannot cover the full allowance — and
    # unlike the in-loop gate it takes no reservation: this is the last call
    # of the run, so there is nothing left to protect it from.  When even the
    # reserve is gone (a single very expensive call, or a tiny budget),
    # returning the deterministic digest immediately is strictly better than
    # issuing a call the budget cannot pay for.  The same guard runs again
    # after a backstop compaction that spent, before the call it precedes.
    async def _budget_digest_if_unaffordable(
        unsent: Iterable[ChatMessage] = (),
    ) -> ChatResponse | None:
        nonlocal _call_max_tokens, _chain_sized, _sized_synthesis_violation
        if _budget is None:
            return None
        _forced_violation = _budget.can_afford(
            estimated_input_tokens=_estimate_prompt_tokens(messages),
            max_output_tokens=max_tokens,
            final=True,
        )
        if _forced_violation is not None and await _attempt_budget_downgrade(
            _forced_violation,
            estimated_input_tokens=_estimate_prompt_tokens(messages),
            final=True,
        ):
            # Writing the final answer on the cheap model is strictly better
            # than returning the deterministic digest, so the synthesis gets
            # the same downgrade the tool-calling phase does.
            _forced_violation = _budget.can_afford(
                estimated_input_tokens=_estimate_prompt_tokens(messages),
                max_output_tokens=max_tokens,
                final=True,
            )
        _call_max_tokens = max_tokens
        if _forced_violation is not None:
            # A sized synthesis spends everything the budget has left, so it
            # is priced with the messages it is sent with.
            _fitted = _budget.affordable_output_tokens(
                estimated_input_tokens=_estimate_prompt_tokens([*messages, *unsent]),
                max_output_tokens=max_tokens,
                final=True,
            )
            if _fitted is not None:
                # Once sized, the synthesis stays sized: a compaction that
                # lets it fit in full again does not free its continuation
                # or its stronger-prompt retry from the budget.
                _chain_sized = True
                _sized_synthesis_violation = _forced_violation
                _forced_violation = None
                _call_max_tokens = _fitted
        if _forced_violation is not None:
            logger.warning(
                "Forced-final-answer backstop: per-run budget cannot pay for "
                "the synthesis call — %s — returning the deterministic "
                "tool-result digest instead. Budget: %s",
                _forced_violation.log_detail(),
                _budget.snapshot(),
            )
            # Money ran out, not time: the wall-clock guard above already let
            # this path through with >= _FORCED_ANSWER_MIN_REMAINING_SECS to
            # spare, so the label lookup is worth having — bounded by what
            # remains of the deadline (and never more than a couple of
            # seconds), falling back to the transcript label on a stall.
            _synthesised = await summarise_tool_results_resolved(
                messages,
                wait_condition,
                lookup_timeout=_label_lookup_budget(wall_clock_deadline),
            )
            _budget_fallback = _budget_exhausted_text(
                _budget_violation or _forced_violation, _synthesised
            )
            if on_token:
                # Mirror the normal backstop path: announce streaming before
                # tokens flow so the frontend swaps the working-state UI for
                # the live answer stream.
                await _emit(on_event, {"status": "streaming"})
                await _stream_text_chunked(on_token, _budget_fallback)
            from app.llm.base import ChatResponse

            # Carry the accumulated usage so run accounting survives the
            # shortcut exactly as it does on the normal backstop exit.
            return _with_limit_stop(
                ChatResponse(
                    content=_budget_fallback,
                    model=model or "unknown",
                    usage=accumulated_usage,
                ),
                _limit_stop,
            )
        return None

    _budget_digest = await _budget_digest_if_unaffordable(_synthesis_instructions)
    if _budget_digest is not None:
        return _budget_digest

    messages.extend(_synthesis_instructions)
    # Final pruning pass before the forced answer call.
    #
    # Only escalate to the large-context-aware emergency compaction when we
    # reached this backstop *because the model kept returning empty on a big
    # context* (``consecutive_empty_responses > 0``). There the
    # regular tool-result prune leaves the oversized prompt intact and the
    # synthesis call returns empty too, degrading to the generic fallback, so
    # emergency-compaction (which truncates assistant turns / evicts old groups)
    # is the lesser evil.
    #
    # When the backstop is instead reached via budget exhaustion (iteration cap
    # or repeated tool-call truncation), the large context is the product of
    # legitimate work — aggressively truncating it would needlessly degrade the
    # final synthesis even though ``max_session_chars`` would allow the full
    # history. Keep the cheaper tool-result prune for that path.
    if consecutive_empty_responses > 0:
        # Cap the best-effort summariser to HALF the post-reserve remaining
        # budget so the mandatory synthesis call after it always keeps a
        # slice of the wall-clock budget for itself — a timed-out
        # summariser degrades to the deterministic pruner, not to an outer
        # asyncio.wait_for cancellation.
        _compaction_spent = False
        await _compact_for_empty_recovery(
            llm,
            messages,
            model=model,
            max_session_chars=max_session_chars,
            attempt=consecutive_empty_responses,
            phase="forced-answer backstop",
            cancel_event=cancel_event,
            llm_timeout=_remaining_forced_timeout(0.5),
            on_event=on_event,
            merge_usage=_merge_compaction_usage,
            can_retry=_backstop_compaction_retry_affordable,
            keep=file_recovery.is_pinned,
        )
        if _compaction_spent:
            _budget_digest = await _budget_digest_if_unaffordable()
            if _budget_digest is not None:
                return _budget_digest
    else:
        # The last iteration's results may never have been sent (the cap
        # hit right after they were appended): the synthesis is the request
        # that shows them, so the prune skips them like the in-loop gate -
        # and skips the turn's results no substantive text has followed too:
        # the answer is what they were gathered for.
        _refresh_turn_protection()
        _prune_session_context(
            messages,
            max_session_chars=max_session_chars,
            is_unseen=_is_protected_tool_body,
        )
        # The window stays the hard bound of the synthesis request, as it is
        # of every in-loop request: the protection above may leave the last
        # batch on top of what the last gate measured, so the in-loop
        # overflow guard is applied here too (pins honoured, like there) -
        # cheapest first. The protection gives way to the window before
        # anything is TRUNCATED: the older, already-seen results are stubbed
        # with ``_is_unseen_tool_result`` alone (the unseen last batch and the
        # pins stay whole), and only a context still over the bound reaches the
        # emergency prune, which truncates every long message - the very
        # batch the synthesis exists to show among them.
        _final_ctx_window = _resolve_ctx_window()
        _forced_target_chars = min(
            max_session_chars,
            int(_final_ctx_window * _COMPACTION_TARGET_UTILIZATION * _CHARS_PER_TOKEN),
        )

        def _over_final_window() -> bool:
            return (
                _final_ctx_window > 0
                and _transcript_chars(messages) // _CHARS_PER_TOKEN
                > _final_ctx_window * 0.95
            )

        if _over_final_window():
            _prune_session_context(
                messages,
                max_session_chars=_forced_target_chars,
                is_unseen=_is_unseen_tool_result,
            )
        if _over_final_window():
            logger.warning(
                "Context overflow before the forced final answer — forcing emergency prune"
            )
            _emergency_prune_session_context(
                messages,
                max_session_chars=_forced_target_chars,
                keep=file_recovery.is_pinned,
            )
            if _over_final_window():
                # The synthesis request's own failure path answers for it
                # (``forced_call_failure``); this says why it will fail.
                logger.error(
                    "Context still over the window after the emergency prune before the forced final answer"
                )
    await _emit(on_event, {"status": "composing"})

    forced_response = None
    # Each synthesis response is debited once, by whichever of the retry, the
    # continuation and the final exit reaches it first: the continuation
    # returns the response it continued, and the retry replaces the first one.
    # It is recorded under the model it names, the one its generation records.
    _recorded_forced_responses: set[int] = set()

    def _record_forced_usage(response: ChatResponse) -> None:
        if id(response) not in _recorded_forced_responses:
            _recorded_forced_responses.add(id(response))
            _merge_usage(response.usage, served_model=response.model or model)

    # Tracks whether the forced-answer synthesis itself failed ("timeout" /
    # "error" / None).  Read by the final fallback below so the user gets an
    # explicit explanation instead of a bare tool-results digest.
    forced_call_failure: str | None = None
    _forced_timeout = _remaining_forced_timeout()
    # Each synthesis call is traced like an in-loop call: a generation
    # of its own, under the run's own span since no iteration span is open any
    # more, closed on every exit. Its usage reaches ``_merge_usage`` once: a
    # returned call's through ``_record_forced_usage``, an aborted call's
    # through ``_end_aborted_generation``.
    _forced_gen_ctx = _lf_start_direct_gen(
        model=model,
        messages=messages,
        name=_FORCED_ANSWER_GENERATION,
        metadata={"observation_kind": _FORCED_ANSWER_GENERATION},
    )
    _current_generation_reasoning = []
    _retry_tok = _lf_set_retry_observer(_lf_make_retry_observer(_forced_gen_ctx))
    _forced_sink = StreamUsageSink()
    # The synthesis streams through the degenerate-repetition stream cut like
    # every in-loop answer; a runaway ends on the notice below.
    _forced_stream = _AnswerStreamCut(on_token, enabled=_degenerate_guard_on) if on_token else None
    try:
        if _forced_stream is not None:
            await _emit(on_event, {"status": "streaming"})
            forced_response = await _streaming_chat(
                llm,
                messages,
                _forced_stream.show,
                model=model,
                temperature=temperature,
                max_tokens=_call_max_tokens,
                on_thinking=_new_on_thinking() if on_event else None,
                structured_output=structured_output,
                timeout=_forced_timeout,
                cancel_event=cancel_event,
                on_stream_reset=_forced_stream.on_restart(_retract_answer_stream),
                usage_sink=_forced_sink,
                trace_gen_ctx=_forced_gen_ctx,
                answer_cut=_forced_stream,
                **_llm_extra,
            )
        else:
            logger.info(
                "LLM call config (forced_final_chat): model=%s max_tokens=%d temperature=%.3f",
                model or "default",
                _call_max_tokens,
                temperature,
            )
            forced_response = await _race_cancellation(
                llm.chat(
                    messages=messages,
                    tools=None,
                    model=model,
                    temperature=temperature,
                    max_tokens=_call_max_tokens,
                    **_llm_extra,
                    **_structured_extra,
                ),
                cancel_event,
                timeout=_forced_timeout,
            )
    except _CancelledMidCallError as _forced_exc:
        # User cancel during the forced-answer synthesis — same canonical
        # outcome as everywhere else in the loop.
        _end_aborted_generation(_forced_gen_ctx, _forced_exc, _forced_sink, _merge_usage)
        logger.info("Agent loop cancelled during forced-final-answer call")
        return _cancelled_chat_response(model, accumulated_usage)
    except TimeoutError as _forced_exc:
        _end_aborted_generation(_forced_gen_ctx, _forced_exc, _forced_sink, _merge_usage)
        forced_call_failure = "timeout"
        logger.error(
            "Forced final answer LLM call timed out after %.0fs", _forced_timeout
        )
    except _AnswerCutError as _forced_cut:
        # The stream cut aborted a runaway synthesis: closed with the usage it
        # reported (recorded there, never again), answered with the text
        # shown, which the guard below ends on the notice.
        forced_response = _end_cut_call(
            _forced_gen_ctx, _forced_cut, _forced_sink, _merge_usage, model
        )
        _recorded_forced_responses.add(id(forced_response))
    except asyncio.CancelledError as _forced_exc:
        _end_aborted_generation(_forced_gen_ctx, _forced_exc, _forced_sink, _merge_usage)
        raise
    except Exception as _forced_exc:
        _end_aborted_generation(_forced_gen_ctx, _forced_exc, _forced_sink, _merge_usage)
        forced_call_failure = "error"
        logger.exception("Forced final answer LLM call failed unexpectedly")
    else:
        _end_returned_generation(
            _forced_gen_ctx,
            forced_response,
            model=model,
            reasoning=_current_generation_reasoning,
        )
    finally:
        _lf_reset_retry_observer(_retry_tok)
        _current_generation_reasoning = None

    if forced_response is None:
        from app.llm.base import ChatResponse

        forced_response = ChatResponse(content="", model=model or "unknown")
    else:
        # The synthesis carried the whole transcript — the results the last
        # iteration appended included, when the cap hit right after them.
        # Record it like every other completed request: the file-recovery
        # guard reads whether a tracked ``read_file_content`` result reached
        # the model with its pixels. A timed-out or failed call showed the
        # model nothing, so it is neither counted nor a release (the
        # ``except`` branches above leave ``forced_response`` unset). No
        # iteration span is open here: a failure is recorded on the root
        # trace's metadata.
        _consume_transcript(phase="forced_answer")

    forced_content = (forced_response.content or "").strip()
    logger.info(
        "Forced final answer generated after %d iterations (%d chars)",
        max_iterations,
        len(forced_content),
    )

    # Guard against empty or very short forced responses.
    # LLMs sometimes produce near-empty output (e.g. 8 tokens) when the
    # context is very long.  Retry once with a more emphatic prompt.
    #
    # Wall-clock deadline guard: the retry path first runs
    # ``_compact_for_empty_recovery`` — whose summariser LLM call uses the
    # full ``_LLM_CALL_TIMEOUT`` — and then a second synthesis call.  When
    # the remaining budget is below the minimum threshold, skip the retry
    # entirely: a short-but-nonempty answer is returned as-is, and an empty
    # one degrades to the final fallback digest below — either way nothing
    # races the outer ``asyncio.wait_for``.
    _retry_low_budget = wall_clock_deadline is not None and (
        wall_clock_deadline - time.monotonic() < _FORCED_ANSWER_MIN_REMAINING_SECS
    )
    if len(forced_content) < _MIN_FORCED_ANSWER_CHARS and _retry_low_budget:
        logger.warning(
            "Forced final answer too short (%d chars < %d) but the wall-clock "
            "budget is nearly exhausted — skipping the stronger-prompt retry "
            "to stay inside the outer deadline",
            len(forced_content),
            _MIN_FORCED_ANSWER_CHARS,
        )
    elif len(forced_content) < _MIN_FORCED_ANSWER_CHARS and _call_sized():
        # The per-run budget sized this synthesis to what was left: a second
        # one would spend past it.
        logger.warning(
            "Forced final answer too short (%d chars < %d) but the per-run "
            "budget sized the synthesis down to %s tokens — skipping the "
            "stronger-prompt retry",
            len(forced_content),
            _MIN_FORCED_ANSWER_CHARS,
            f"{_call_max_tokens:,}",
        )
    elif len(forced_content) < _MIN_FORCED_ANSWER_CHARS:
        logger.warning(
            "Forced final answer too short (%d chars < %d) — retrying with stronger prompt",
            len(forced_content),
            _MIN_FORCED_ANSWER_CHARS,
        )
        _record_forced_usage(forced_response)
        messages.append(LLMMessage(role="assistant", content=forced_content or "..."))
        messages.append(LLMMessage(role="system", content=_FORCED_ANSWER_RETRY))
        # The first forced-answer call came back near-empty — that is itself the
        # empty-output-on-oversized-context symptom, so emergency-compact
        # before the stronger-prompt retry regardless of how the backstop was
        # entered (this is the one site where compaction is justified even when
        # ``consecutive_empty_responses == 0``, because we just observed a
        # near-empty synthesis). ``_compact_for_empty_recovery`` still falls back
        # to the cheap tool-result prune below the large-context trigger.
        #
        # We pass ``attempt + 1`` but the fraction index is clamped to the most
        # aggressive entry, so when the backstop is reached after >=2 double-empty
        # rounds this reuses the first backstop call's fraction rather than
        # advancing a step. The context still shrinks further regardless, because
        # the fraction targets the *current* size — already reduced if the first
        # call emergency-compacted, still full-size if it only tool-result-pruned
        # (the budget-exhaustion path) — so a stubbornly-empty context keeps
        # getting smaller on the retry.
        # Same half-the-remaining-budget cap as the first backstop
        # compaction: the retry synthesis call below must keep its own
        # slice of the wall-clock budget.
        _compaction_spent = False
        await _compact_for_empty_recovery(
            llm,
            messages,
            model=model,
            max_session_chars=max_session_chars,
            attempt=consecutive_empty_responses + 1,
            phase="forced-answer retry",
            cancel_event=cancel_event,
            llm_timeout=_remaining_forced_timeout(0.5),
            on_event=on_event,
            merge_usage=_merge_compaction_usage,
            can_retry=_backstop_compaction_retry_affordable,
            keep=file_recovery.is_pinned,
        )
        if _compaction_spent:
            _budget_digest = await _budget_digest_if_unaffordable()
            if _budget_digest is not None:
                return _budget_digest

        _forced_retry_timeout = _remaining_forced_timeout()
        _retry_gen_ctx = _lf_start_direct_gen(
            model=model,
            messages=messages,
            name=_FORCED_ANSWER_RETRY_GENERATION,
            metadata={"observation_kind": _FORCED_ANSWER_GENERATION},
        )
        _current_generation_reasoning = []
        _retry_tok = _lf_set_retry_observer(_lf_make_retry_observer(_retry_gen_ctx))
        _forced_retry_sink = StreamUsageSink()
        _forced_stream = (
            _AnswerStreamCut(on_token, enabled=_degenerate_guard_on) if on_token else None
        )
        try:
            if _forced_stream is not None:
                forced_response = await _streaming_chat(
                    llm,
                    messages,
                    _forced_stream.show,
                    model=model,
                    temperature=temperature,
                    max_tokens=_call_max_tokens,
                    on_thinking=_new_on_thinking() if on_event else None,
                    structured_output=structured_output,
                    timeout=_forced_retry_timeout,
                    cancel_event=cancel_event,
                    on_stream_reset=_forced_stream.on_restart(_retract_answer_stream),
                    usage_sink=_forced_retry_sink,
                    trace_gen_ctx=_retry_gen_ctx,
                    answer_cut=_forced_stream,
                    **_llm_extra,
                )
            else:
                logger.info(
                    "LLM call config (forced_retry_chat): model=%s max_tokens=%d temperature=%.3f",
                    model or "default",
                    _call_max_tokens,
                    temperature,
                )
                forced_response = await _race_cancellation(
                    llm.chat(
                        messages=messages,
                        tools=None,
                        model=model,
                        temperature=temperature,
                        max_tokens=_call_max_tokens,
                        **_llm_extra,
                        **_structured_extra,
                    ),
                    cancel_event,
                    timeout=_forced_retry_timeout,
                )
        except _CancelledMidCallError as _forced_exc:
            # User cancel during the forced-answer retry — same canonical
            # outcome as everywhere else in the loop.
            _end_aborted_generation(_retry_gen_ctx, _forced_exc, _forced_retry_sink, _merge_usage)
            logger.info("Agent loop cancelled during forced-answer retry call")
            return _cancelled_chat_response(model, accumulated_usage)
        except TimeoutError as _forced_exc:
            _end_aborted_generation(_retry_gen_ctx, _forced_exc, _forced_retry_sink, _merge_usage)
            forced_call_failure = "timeout"
            logger.error(
                "Retry forced answer timed out after %.0fs", _forced_retry_timeout
            )
        except _AnswerCutError as _forced_cut:
            # A runaway retry, aborted at the cut: recorded once, with the
            # usage it reported, and ended below on the text shown.
            forced_response = _end_cut_call(
                _retry_gen_ctx, _forced_cut, _forced_retry_sink, _merge_usage, model
            )
            _recorded_forced_responses.add(id(forced_response))
            forced_call_failure = None
            _consume_transcript(phase="forced_answer_retry")
        except asyncio.CancelledError as _forced_exc:
            _end_aborted_generation(_retry_gen_ctx, _forced_exc, _forced_retry_sink, _merge_usage)
            raise
        except Exception as _forced_exc:
            _end_aborted_generation(_retry_gen_ctx, _forced_exc, _forced_retry_sink, _merge_usage)
            forced_call_failure = "error"
            logger.exception("Retry forced answer failed unexpectedly")
        else:
            _end_returned_generation(
                _retry_gen_ctx,
                forced_response,
                model=model,
                reasoning=_current_generation_reasoning,
            )
            # The stronger-prompt retry completed, so the synthesis call no
            # longer failed — clear any failure recorded for the FIRST call.
            # Otherwise a successful-but-empty retry would surface a
            # misleading "ran out of time / internal error" lead-in for what
            # is actually a plain empty model output.
            forced_call_failure = None
            # Same as the first synthesis: a completed request, observed by
            # the file-recovery guard (the compaction above may have
            # re-shaped the transcript it carried).
            _consume_transcript(phase="forced_answer_retry")
        finally:
            _lf_reset_retry_observer(_retry_tok)
            _current_generation_reasoning = None
        forced_content = (forced_response.content or "").strip()
        logger.info(
            "Retry forced answer: %d chars",
            len(forced_content),
        )

    # Auto-continue if the forced answer was truncated by max_tokens.
    # ``_continue_truncated_response`` strips ``structured_output`` from
    # continuation calls by design (a grammar-constrained
    # continuation cannot resume the truncated text), so only
    # ``_llm_extra`` is forwarded. A runaway is ended below, never continued.
    if (
        forced_content
        and forced_response.stop_reason == "max_tokens"
        and not _is_runaway(forced_content, _forced_stream)
    ):
        _record_forced_usage(forced_response)

        def _synthesis_continuation_tokens(prospective: list[ChatMessage]) -> int | None:
            nonlocal _last_refusal
            if _budget is None:
                return _call_max_tokens
            estimated = _estimate_prompt_tokens(prospective)
            allowance = _budget.affordable_output_tokens(
                estimated_input_tokens=estimated, max_output_tokens=max_tokens, final=True
            )
            _last_refusal = None
            if allowance is None:
                _last_refusal = _budget.can_afford(
                    estimated_input_tokens=estimated, max_output_tokens=max_tokens, final=True
                )
            return allowance

        try:
            forced_response = await _continue_truncated_response(
                llm,
                messages,
                forced_response,
                on_token=_forced_stream.show if _forced_stream is not None else None,
                on_event=on_event,
                model=model,
                temperature=temperature,
                max_tokens=_call_max_tokens,
                max_session_chars=max_session_chars,
                llm_extra=_llm_extra,
                merge_usage=_merge_usage,
                stop=functools.partial(_is_runaway, stream=_forced_stream),
                new_on_thinking=_new_on_thinking if on_event else None,
                parent_iter_ctx=_lf_get_ctx(),
                cancel_event=cancel_event,
                # The backstop's own remaining-budget cap also
                # applies to a truncated forced answer's continuation.
                llm_timeout=_remaining_forced_timeout(),
                next_max_tokens=_synthesis_continuation_tokens if _call_sized() else None,
                answer_cut=_forced_stream,
            )
        except _AnswerCutError:
            # A continuation the stream cut stopped (closed by the helper):
            # ended below on the text shown.
            forced_response.stop_reason = "end_turn"
        except _CancelledMidCallError:
            # User cancel during the forced-answer continuation — same
            # canonical outcome as everywhere else in the loop.
            logger.info(
                "Agent loop cancelled during forced-answer continuation"
            )
            return _cancelled_chat_response(model, accumulated_usage)
        await _say_budget_cut(forced_response)
        forced_content = (forced_response.content or "").strip()

    if forced_content and _is_runaway(forced_content, _forced_stream):
        logger.warning(
            "Forced final answer is a degenerate repetition loop (%d chars) — "
            "ending it on the notice",
            len(forced_content),
        )
        await _end_runaway(
            forced_response, _forced_stream, on_token, shown=_forced_stream is not None
        )
        forced_content = forced_response.content.strip()

    # Final fallback when the LLM still produced nothing useful.
    # Prefer a synthesised digest of tool results over the generic message —
    # this gives the user something actionable when many tool calls succeeded
    # but the model failed to compose them into a final answer.
    if not forced_content:
        # The synthesis call may have consumed most of the remaining wall
        # clock (a timeout here IS the deadline arriving), so the label
        # lookup is bounded by what is left after the return reserve — zero
        # means the transcript label only, same as the shortcut above.
        synthesised = await summarise_tool_results_resolved(
            messages,
            wait_condition,
            lookup_timeout=_label_lookup_budget(wall_clock_deadline),
        )
        if forced_call_failure is not None:
            # The synthesis call itself timed out or errored — say so
            # explicitly instead of surfacing a bare digest the user can't
            # tell apart from a normal answer.  Keep the digest as
            # "partial results" when available so nothing actionable is lost.
            lead_in = (
                FORCED_ANSWER_TIMEOUT_MESSAGE
                if forced_call_failure == "timeout"
                else FORCED_ANSWER_ERROR_MESSAGE
            )
            if synthesised:
                text = f"{lead_in}\n\nPartial results:\n\n{synthesised}"
            else:
                text = f"{lead_in} {EMPTY_RESPONSE_FALLBACK}"
            logger.warning(
                "Forced final answer failed (%s) — returning explicit "
                "%s message (partial_digest=%s)",
                forced_call_failure,
                forced_call_failure,
                bool(synthesised),
            )
            forced_response.content = text
            if on_token:
                await _stream_text_chunked(on_token, text)
        elif _call_max_tokens < max_tokens and _sized_synthesis_violation is not None:
            # A synthesis the budget sized down came back empty (a reasoning
            # model can spend a lowered allowance whole): end the run as one
            # whose synthesis the budget could not pay for ends.
            text = _budget_exhausted_text(
                _budget_violation or _sized_synthesis_violation, synthesised
            )
            logger.warning(
                "Forced final answer empty after a synthesis the per-run "
                "budget sized down to %s tokens — returning the budget "
                "message (partial_digest=%s)",
                f"{_call_max_tokens:,}",
                bool(synthesised),
            )
            forced_response.content = text
            if on_token:
                await _stream_text_chunked(on_token, text)
        elif synthesised:
            logger.warning(
                "Forced final answer empty after retries — synthesised digest "
                "from %d-message conversation",
                len(messages),
            )
            forced_response.content = synthesised
            # Stream the synthesised digest so the user sees it in real-time
            # when streaming is enabled, instead of an apparently empty reply.
            if on_token:
                await _stream_text_chunked(on_token, synthesised)
        else:
            logger.error(
                "Forced final answer empty and no tool results to synthesise "
                "— falling back to generic message (conversation_msgs=%d)",
                len(messages),
            )
            forced_response.content = EMPTY_RESPONSE_FALLBACK
            if on_token:
                await _stream_text_chunked(on_token, EMPTY_RESPONSE_FALLBACK)

    _record_forced_usage(forced_response)
    forced_response.usage = accumulated_usage
    return _with_limit_stop(forced_response, _limit_stop)
