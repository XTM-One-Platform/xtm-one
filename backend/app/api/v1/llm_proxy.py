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

"""LLM proxy – per-user API key authentication.

Universal chat completions proxy that works with all configured providers.
For providers using the chat completions standard (OpenAI, OpenRouter,
Ollama, Mistral, Azure OpenAI, LiteLLM, Custom OpenAI), requests are
passed through directly.  For Anthropic, requests are automatically
converted between chat completions and Anthropic Messages format.

Also provides a native Anthropic Messages API pass-through at
``/v1/messages`` for Claude Code compatibility.

Streaming (``stream: true``) and non-streaming modes are both supported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_ai_enabled
from app.api.v1.helpers import get_user_group_ids, user_visible_filter
from app.core.database import get_db, release_db_connection_before_loop
from app.core.redaction import known_secrets_scoped, redact_known_secrets_for_display
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.llm.attribution import config_metadata, provider_config_metadata
from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ContextOverflowError,
    ModelDeclinedError,
    StreamUsageSink,
    declined_error,
    overflow_api_message,
)
from app.llm.cut_answers import StreamStopReason, answer_was_cut
from app.llm.openai_compat import (
    content_filter_finish,
    declined_by_filter,
    normalize_openai_reasoning_effort,
    openai_content_parts,
)
from app.llm.provider_access import (
    ProviderNotUsableError,
    provider_routing,
)
from app.llm.router import (
    FailoverLLMProvider,
    get_llm_for_user,
    select_provider_configs,
)
from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services.api_key_access import (
    APIKEY_STATS_PREFIX,
    ApiKeySurface,
    agents_for_key,
    lists_raw_models,
    looks_like_api_key,
    require_agent_run,
    require_model_access,
    resolve_api_key,
)
from app.services.failure_causes import FailureCause, FailureContext, classify_run_failure
from app.services.langfuse_tracing import (
    build_trace_input,
    reset_current_trace_ctx,
    set_current_trace_ctx,
    trace_llm_call,
)
from app.services.langfuse_tracing import end_trace as _end_langfuse_trace
from app.services.vault import reveal_field

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constants ────────────────────────────────────────────────────────────────
_MODEL_USAGE_TTL = 86_400 * 7  # 7 days
_NON_CHAT_MODEL_PREFIXES = (
    "text-embedding-",
    "text-moderation-",
    "omni-moderation-",
    "whisper-",
    "tts-",
    "dall-e-",
)
_NON_CHAT_MODEL_EXACT = {
    "gpt-3.5-turbo-instruct",
    "babbage-002",
    "davinci-002",
}


# ── Context overflow helpers for OpenAI-compatible responses ─────────────────


def _overflow_sse_event(exc: ContextOverflowError) -> str:
    """Return a serialized SSE data frame carrying a context overflow error."""
    return json.dumps(
        {
            "error": {
                "message": overflow_api_message(exc),
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        }
    )


def _overflow_http_exc(exc: ContextOverflowError) -> HTTPException:
    """Return an HTTPException(400) for a non-streaming context overflow."""
    return HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": overflow_api_message(exc),
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        },
    )


def _provider_client_error(exc: Exception) -> HTTPException | None:
    """Return an HTTPException for provider 4xx errors, else None.

    401 and 403 are intentionally excluded: those reflect the *platform's* own
    provider credentials being invalid/expired and must not be surfaced to
    callers (it would disclose internal configuration state).
    """
    status = getattr(exc, "status_code", None)
    if not (status and 400 <= status < 500) or status in (401, 403):
        return None
    body = getattr(exc, "body", None) or {}
    # Prefer the structured provider error message; never fall back to str(exc)
    # which may contain raw HTTP bodies or internal SDK details.
    msg = (body.get("error", {}).get("message") if isinstance(body, dict) else None) or "Provider error"
    return HTTPException(status_code=status, detail=msg)


# ── Chat gate ───────────────────────────────────────────────────────────────


async def _refuse_unless_chat_enabled(db: AsyncSession) -> None:
    """The chat gate, with the reason named — for the two proxy entry points.

    Same two-reason shape as the MCP agent endpoints (FR-029): AI switched off
    answers with the contract payload a client branches on (``detail.code``),
    a missing licence answers with the mode-aware sentence every licence gate
    carries (``deps.refuse_chat_unavailable``). Collapsing both into one
    licence message told an operator who had switched AI off to buy a licence
    they already had, and named the retired "XTM One trial or Enterprise
    license" remedy to an unlicensed Copilot deployment.
    """
    from app.api.deps import refuse_chat_unavailable
    from app.api.v1.platform import is_chat_licensed
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()
    if not await is_chat_licensed(db):
        await refuse_chat_unavailable(db)


# ── API-Key Authentication ──────────────────────────────────────────────────


async def _authenticate_api_key(request: Request, db: AsyncSession) -> User:
    """Validate API key and return the owning user, held to the key's scopes.

    Accepts the key from two sources (checked in order):
    1. ``Authorization: Bearer fcp-...`` — OpenAI-compatible clients, Cursor,
       and Claude Code with ``ANTHROPIC_AUTH_TOKEN``.
    2. ``x-api-key: fcp-...`` — Anthropic SDK / Claude Code with
       ``ANTHROPIC_API_KEY``.
    """
    raw_key: str | None = None

    # 1. Authorization: Bearer (OpenAI-compatible / ANTHROPIC_AUTH_TOKEN)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_key = auth_header[7:]

    # 2. x-api-key (Anthropic SDK / Claude Code with ANTHROPIC_API_KEY)
    if not raw_key:
        raw_key = request.headers.get("x-api-key")

    if not looks_like_api_key(raw_key):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")
    return await resolve_api_key(raw_key, db, surface=ApiKeySurface.LLM_PROXY, request=request)


# ── API-Key Stats Flush ─────────────────────────────────────────────────────

MODEL_USAGE_KEY = "ai_adoption:model_usage"


async def _track_model_usage(model: str, user_id: str | None = None) -> None:
    """Increment Redis counter for model usage tracking (best-effort).

    When *user_id* is provided, also increments the LLM proxy quota
    counters so quota enforcement stays accurate.
    """
    if not model:
        return
    try:
        redis = await redis_client()
        await redis.hincrby(MODEL_USAGE_KEY, model, 1)
        await redis.expire(MODEL_USAGE_KEY, _MODEL_USAGE_TTL)
    except Exception:
        message = "Failed to track model usage in Redis"
        logger.debug(message, exc_info=True)

    if user_id:
        try:
            import uuid as _uuid_mod

            from app.core.database import async_session
            from app.services.quota import increment_llm_proxy_usage

            uid = _uuid_mod.UUID(user_id) if isinstance(user_id, str) else user_id
            async with async_session() as db:
                await increment_llm_proxy_usage(uid, model, db=db)
        except Exception:
            message = "Failed to increment LLM proxy quota usage for model %s user %s"
            logger.debug(message, model, user_id, exc_info=True)


# ── Streams the client abandons ──────────────────────────────────────────────
#
# A client that disconnects mid-stream makes Starlette stop the generator:
# ``GeneratorExit`` at the ``yield`` it was suspended on, or ``CancelledError``
# at the ``await`` it was running. Neither is an ``Exception``, so without a
# handler of their own the trace was never closed and the request never metered.

_CLIENT_DISCONNECTED = "The client disconnected before the end of the stream"

# Metering tasks of streamed requests, held until they finish. The metering
# opens a Redis connection and a DB session, and it cannot run inside the
# generator: a client leaving cancels whatever the generator awaits, and an
# anyio cancel scope delivers the cancellation again at every later ``await``.
_STREAM_METERING: set[asyncio.Task] = set()


def _end_abandoned_stream(
    trace_ctx: Any,
    exc: BaseException,
    *,
    output: str | None,
    model: str | None,
    usage: dict | None,
) -> None:
    """Close the trace of a stream whose client went away.

    It carries the usage the provider reported before the cut, and nothing
    else: never an estimate, so a stream stopped before its provider reported
    any usage records none. The failure is a structural ``run_cancelled``.
    """
    _end_langfuse_trace(
        trace_ctx,
        output=output or None,
        model=model,
        usage=usage or None,
        error=_CLIENT_DISCONNECTED,
        failure=classify_run_failure(exc, FailureContext(stop_reason="cancelled")),
    )


def _meter_stream(model: str, user_id: str) -> asyncio.Task | None:
    """Meter a streamed request from a task of its own, held until it finishes.

    A finished stream counts, and so does one whose client left mid-stream:
    the request reached the provider, which billed it (``/v1/messages``
    meters every request before it forwards it).
    """
    try:
        task = asyncio.get_running_loop().create_task(
            _track_model_usage(model, user_id=user_id)
        )
    except RuntimeError:
        return None
    _STREAM_METERING.add(task)
    task.add_done_callback(_STREAM_METERING.discard)
    return task


async def _meter_finished_stream(model: str, user_id: str) -> None:
    """Meter a stream that ended normally, and wait for the metering.

    The wait is shielded: a client leaving right after ``[DONE]`` cancels the
    wait, never the metering, which finishes in its task.
    """
    task = _meter_stream(model, user_id)
    if task is not None:
        await asyncio.shield(task)


class _AnthropicSseTally:
    """What a streamed Anthropic Messages response has said so far, read off
    the raw SSE bytes the ``/v1/messages`` pass-through pipes along: its text
    and its usage.

    ``message_start`` carries the prompt side (input, cache write and read);
    each ``message_delta`` carries the output count so far, cumulative (the
    streaming docs: "The token counts shown in the usage field of the
    message_delta event are cumulative"), so the latest one wins and the counts
    are never added up - the rule of the provider's ``_StreamUsageAccumulator``.
    """

    __slots__ = (
        "_line_buf",
        "cache_creation_tokens",
        "cache_read_tokens",
        "input_tokens",
        "output_tokens",
        "text_parts",
    )

    def __init__(self) -> None:
        self._line_buf = b""
        self.text_parts: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_tokens = 0
        self.cache_read_tokens = 0

    def feed(self, raw_chunk: bytes) -> None:
        self._line_buf += raw_chunk
        while b"\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split(b"\n", 1)
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]
            if data_str == "[DONE]":
                continue
            try:
                evt = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
            evt_type = evt.get("type", "")
            if evt_type == "message_start":
                u = evt.get("message", {}).get("usage", {})
                self.input_tokens += u.get("input_tokens", 0)
                self.cache_creation_tokens += u.get("cache_creation_input_tokens", 0)
                self.cache_read_tokens += u.get("cache_read_input_tokens", 0)
                # The initial output count; every ``message_delta`` replaces it.
                output_tokens = u.get("output_tokens")
                if output_tokens is not None:
                    self.output_tokens = output_tokens
            elif evt_type == "message_delta":
                output_tokens = evt.get("usage", {}).get("output_tokens")
                if output_tokens is not None:
                    self.output_tokens = output_tokens
            elif evt_type == "content_block_delta":
                delta = evt.get("delta", {})
                if delta.get("type") == "text_delta":
                    self.text_parts.append(delta.get("text", ""))

    def text(self) -> str | None:
        return "".join(self.text_parts) or None

    def usage(self) -> dict | None:
        if not (
            self.input_tokens
            or self.output_tokens
            or self.cache_creation_tokens
            or self.cache_read_tokens
        ):
            return None
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
        }


async def get_and_reset_model_usage() -> dict[str, int]:
    """Read and reset the model usage counters from Redis.

    Returns a dict of {model_name: request_count}.
    Called by the AI adoption metrics collector.
    """
    try:
        redis = await redis_client()
        raw = await redis.hgetall(MODEL_USAGE_KEY)
        if raw:
            await redis.delete(MODEL_USAGE_KEY)
            return {
                (k if isinstance(k, str) else k.decode()): int(v)
                for k, v in raw.items()
            }
    except Exception:
        message = "Failed to read/reset model usage counters from Redis"
        logger.debug(message, exc_info=True)
    return {}


async def flush_api_key_stats() -> int:
    """Flush buffered API key usage stats from Redis to PostgreSQL.

    Called periodically by the autonomous scheduler.  For each
    ``apikey:stats:{key_id}`` hash in Redis, atomically reads + deletes the
    buffered count and last_used timestamp, then batch-updates the DB rows.

    Returns the number of keys flushed.
    """
    from sqlalchemy import update

    from app.core.database import async_session

    redis = await redis_client()
    flushed = 0

    # Collect all buffered stats keys
    keys: list[str] = []
    async for key in redis.scan_iter(match=f"{APIKEY_STATS_PREFIX}*", count=100):
        keys.append(key if isinstance(key, str) else key.decode())

    if not keys:
        return 0

    # Read and delete each key atomically via pipeline
    pipe = redis.pipeline()
    for key in keys:
        pipe.hgetall(key)
        pipe.delete(key)
    results = await pipe.execute()

    # results alternate: hgetall, delete, hgetall, delete, ...
    updates: list[tuple[uuid.UUID, int, str | None]] = []
    for i in range(0, len(results), 2):
        raw: dict = results[i]
        if not raw:
            continue
        key_id_str = keys[i // 2].removeprefix(APIKEY_STATS_PREFIX)
        try:
            key_uuid = uuid.UUID(key_id_str)
        except ValueError:
            continue
        count = int(raw.get(b"count", raw.get("count", 0)))
        last_used = (
            (raw.get(b"last_used") or raw.get("last_used") or b"").decode()
            if isinstance(raw.get(b"last_used", raw.get("last_used")), bytes)
            else raw.get("last_used")
        )
        updates.append((key_uuid, count, last_used))

    if not updates:
        return 0

    # Batch-update PostgreSQL
    async with async_session() as db:
        for key_uuid, count, last_used in updates:
            values: dict[str, Any] = {
                "request_count": UserApiKey.request_count + count,
            }
            if last_used:
                with contextlib.suppress(ValueError):
                    values["last_used_at"] = datetime.fromisoformat(last_used)
            await db.execute(
                update(UserApiKey).where(UserApiKey.id == key_uuid).values(**values)
            )
            flushed += 1
        await db.commit()

    if flushed:
        logger.debug("Flushed API key usage stats for %d keys", flushed)
    return flushed


# ── Request / Response Schemas (OpenAI-compatible) ──────────────────────────


class ProxyMessage(BaseModel):
    """Single message in OpenAI chat format."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list | None = None
    name: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class StreamOptions(BaseModel):
    """Streaming options (OpenAI extension)."""

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request.

    Extra fields sent by clients are silently accepted via ``extra="allow"``.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ProxyMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    reasoning_effort: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_text_content(content: str | list | None) -> str:
    """Extract plain text from OpenAI-format message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts) if parts else ""
    return str(content)


def _build_raw_openai_messages(proxy_msgs: list[ProxyMessage]) -> list[dict]:
    """Convert proxy messages to raw OpenAI-format dicts (transparent pass-through)."""
    raw: list[dict] = []
    for m in proxy_msgs:
        entry: dict = {"role": m.role}
        if m.content is not None:
            entry["content"] = m.content
        elif m.role != "assistant":
            entry["content"] = ""
        if m.tool_calls:
            entry["tool_calls"] = m.tool_calls
            if "content" not in entry:
                entry["content"] = None
        if m.tool_call_id:
            entry["tool_call_id"] = m.tool_call_id
        if m.name:
            entry["name"] = m.name
        raw.append(entry)
    return raw


def _has_openai_sdk(provider: BaseLLMProvider) -> bool:
    """Return True if the provider exposes an OpenAI-SDK-compatible client."""
    return hasattr(provider, "client") and hasattr(
        getattr(provider.client, "chat", None), "completions"
    )


def _supports_chat_completions(model: str | None) -> bool:
    """Return whether *model* appears compatible with /v1/chat/completions.

    Prefix-based detection (rather than substring keyword matching) keeps the
    false-positive rate down: a hypothetical chat model named e.g.
    ``embedding-aware-gpt-5`` should be allowed through, only models whose
    name actually *starts* with a known non-chat prefix are rejected.

    OpenRouter / LiteLLM-style ``org/model`` ids are normalised by stripping
    the leading provider segment so e.g. ``openai/text-embedding-3-large``,
    ``openai/whisper-1`` and ``openai/dall-e-3`` are correctly matched
    against the bare-name prefix list.

    Note on ``-instruct``: instruction-tuned chat models from non-OpenAI
    providers commonly end in ``-instruct`` (e.g.
    ``meta-llama/llama-3.3-70b-instruct``,
    ``mistralai/mixtral-8x7b-instruct``) and are routed through the
    chat-completions endpoint, so we deliberately do NOT have a generic
    ``-instruct`` suffix rule. OpenAI's only completion-only ``-instruct``
    model — ``gpt-3.5-turbo-instruct`` — is enumerated explicitly in
    ``_NON_CHAT_MODEL_EXACT`` instead. If OpenAI ships a future
    completion-only ``gpt-X-instruct``, add it to the exact-match set.
    """
    if not model:
        return True
    normalized = model.strip().lower()
    if not normalized:
        return True
    # OpenRouter / LiteLLM ``org/model`` shape — match against the bare
    # model name. ``rsplit`` (not ``split``) handles paths like
    # ``foo/bar/baz`` by keeping only the trailing segment.
    bare_name = normalized.rsplit("/", 1)[-1]
    if bare_name in _NON_CHAT_MODEL_EXACT:
        return False
    return not bare_name.startswith(_NON_CHAT_MODEL_PREFIXES)


def _is_non_chat_model_error(exc: Exception) -> bool:
    """Return True when an upstream error indicates a non-chat model was used.

    Tightened beyond the original "message contains keywords" check to also
    require ``exc.__class__.__name__ == "NotFoundError"`` — that's the only
    exception class the OpenAI / OpenAI-compatible SDKs raise for this case,
    and gating on it avoids false positives from unrelated 404 messages that
    happen to mention ``/v1/chat/completions`` (e.g. a webhook URL embedded
    in a transient gateway error).
    """
    if exc.__class__.__name__ != "NotFoundError":
        return False
    msg = str(exc).lower()
    return "not a chat model" in msg and "v1/chat/completions" in msg


def _non_chat_model_http_exception(model: str | None) -> HTTPException:
    """Build a user-facing validation error for non-chat model requests.

    Kept for the call sites that still raise an ``HTTPException`` directly
    (auth-side validation, proxy fallback paths). New code paths that
    need to return an OpenAI-style error body should use
    ``_chat_model_mismatch_error_detail`` instead.
    """
    model_name = model or "selected model"
    return HTTPException(
        status_code=400,
        detail=(
            f"Model '{model_name}' is not chat-compatible and cannot be used with "
            "/v1/chat/completions. Please select a chat-capable model, or use "
            "/v1/completions for legacy completion-only models."
        ),
    )


def _chat_model_mismatch_error_detail(model: str) -> dict[str, dict[str, str | None]]:
    """Build an OpenAI-style invalid_request_error payload for model mismatch."""
    return {
        "error": {
            "message": (
                f"Model '{model}' is not supported by /v1/chat/completions. "
                "Use a chat model or route the request to /v1/completions."
            ),
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_supported_for_chat_completions",
        }
    }


def _proxy_tools_to_definitions(
    tools: list[dict] | None,
) -> list | None:
    """Convert OpenAI-format tool dicts to ToolDefinition for overflow estimation."""
    if not tools:
        return None
    from app.tools.base import ToolDefinition

    defs: list[ToolDefinition] = []
    for t in tools:
        func = t.get("function", {})
        defs.append(
            ToolDefinition(
                name=func.get("name", ""),
                description=func.get("description", ""),
                input_schema=func.get("parameters", {}),
            )
        )
    return defs


# ── Anthropic bridge (OpenAI format ↔ Anthropic Messages API) ───────────────


def _tool_call_input(arguments: Any) -> dict:
    """The ``input`` object of an Anthropic ``tool_use`` block from an OpenAI
    tool call's ``arguments`` (a JSON string, or already an object). Anthropic
    takes an object only: anything else is replayed as ``{}``."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _assistant_msg_to_anthropic(content: Any, tool_calls: list) -> dict:
    """An OpenAI assistant turn with ``tool_calls`` as Anthropic content
    blocks: its text first, then one ``tool_use`` block per call."""
    blocks: list[dict] = []
    text = _extract_text_content(content)
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        func = call.get("function") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "",
                "name": func.get("name") or "",
                "input": _tool_call_input(func.get("arguments")),
            }
        )
    return {"role": "assistant", "content": blocks}


def _is_tool_result_turn(message: dict | None) -> bool:
    """True when *message* is a user turn made only of ``tool_result`` blocks."""
    if message is None or message.get("role") != "user":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _openai_msgs_to_anthropic(raw_messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert OpenAI-format messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).
    Anthropic requires system as a top-level param, not in the messages list.
    An assistant turn's ``tool_calls`` become ``tool_use`` blocks and a
    ``role: tool`` message a ``tool_result`` block in a user turn, the results
    of consecutive tool messages in one turn, as Anthropic expects them.
    """
    system_parts: list[str] = []
    anthropic_msgs: list[dict] = []
    for m in raw_messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_parts.append(content if isinstance(content, str) else str(content))
        elif role == "assistant" and m.get("tool_calls"):
            anthropic_msgs.append(
                _assistant_msg_to_anthropic(content, m["tool_calls"])
            )
        elif role == "assistant":
            anthropic_msgs.append({"role": "assistant", "content": content or ""})
        elif role == "tool":
            result_block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": _extract_text_content(content),
            }
            previous = anthropic_msgs[-1] if anthropic_msgs else None
            if previous is not None and _is_tool_result_turn(previous):
                previous["content"].append(result_block)
            else:
                anthropic_msgs.append({"role": "user", "content": [result_block]})
        else:
            anthropic_msgs.append({"role": "user", "content": content or ""})
    return "\n\n".join(system_parts), anthropic_msgs


def _proxy_tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    """OpenAI function tools as the Anthropic Messages API takes them
    (``{name, description, input_schema}``), through the provider's own wire
    form: deduplicated by name, top-level schema combinators stripped."""
    from app.llm.anthropic_provider import _wire_tools

    definitions = _proxy_tools_to_definitions(tools)
    if not definitions:
        return None
    return _wire_tools([d for d in definitions if d.name])


def _anthropic_tool_choice(
    tool_choice: str | dict | None, parallel_tool_calls: Any = None
) -> dict | None:
    """An OpenAI ``tool_choice`` as Anthropic's: ``auto``, ``none``,
    ``required`` (``any``) or a named function (``tool``); an Anthropic-shaped
    choice is kept. ``parallel_tool_calls: false`` sets
    ``disable_parallel_tool_use``. ``None`` leaves the default."""
    choice: dict | None = None
    if isinstance(tool_choice, str):
        choice = {
            "auto": {"type": "auto"},
            "none": {"type": "none"},
            "required": {"type": "any"},
            "any": {"type": "any"},
        }.get(tool_choice)
    elif isinstance(tool_choice, dict):
        ctype = tool_choice.get("type")
        if ctype == "function":
            name = (tool_choice.get("function") or {}).get("name")
            choice = {"type": "tool", "name": name} if name else {"type": "any"}
        elif ctype == "allowed_tools":
            mode = (tool_choice.get("allowed_tools") or {}).get("mode")
            choice = {"type": "any" if mode == "required" else "auto"}
        elif ctype in ("auto", "none", "any", "tool"):
            choice = dict(tool_choice)
    if parallel_tool_calls is False:
        choice = choice or {"type": "auto"}
        if choice["type"] != "none":
            choice["disable_parallel_tool_use"] = True
    return choice


def _anthropic_tool_calls(blocks: Any) -> list[dict]:
    """The ``tool_use`` blocks of an Anthropic response as OpenAI
    ``tool_calls`` (arguments as a JSON string)."""
    calls: list[dict] = []
    for block in blocks or []:
        if getattr(block, "type", "") != "tool_use":
            continue
        calls.append(
            {
                "id": getattr(block, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(block, "name", ""),
                    "arguments": json.dumps(getattr(block, "input", None) or {}),
                },
            }
        )
    return calls


def _tool_call_chunk(model: str, tool_call: dict) -> str:
    """One SSE ``chat.completion.chunk`` carrying a ``delta.tool_calls`` entry."""
    import time

    chunk = {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [tool_call]},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _anthropic_response_to_openai(result: Any, model: str) -> dict:
    """Convert an Anthropic Messages response to OpenAI chat completions format."""
    import time

    content = ""
    reasoning = ""
    for block in getattr(result, "content", []):
        btype = getattr(block, "type", "")
        if btype == "text":
            content += getattr(block, "text", "")
        elif btype == "thinking":
            reasoning += getattr(block, "thinking", "")

    usage = getattr(result, "usage", None)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls := _anthropic_tool_calls(getattr(result, "content", [])):
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{getattr(result, 'id', 'unknown')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _map_anthropic_stop(
                    getattr(result, "stop_reason", None)
                ),
            }
        ],
        "usage": {
            "prompt_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            "total_tokens": (
                (getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0))
                if usage
                else 0
            ),
        },
    }


def _map_anthropic_stop(reason: str | None) -> str:
    """Map Anthropic stop_reason to OpenAI finish_reason."""
    if reason == "end_turn":
        return "stop"
    if reason == "max_tokens":
        return "length"
    if reason == "tool_use":
        return "tool_calls"
    if reason == "refusal":
        # The model's safety classifiers declined: OpenAI's word for an
        # answer withheld by a content filter.
        return "content_filter"
    return "stop"


def _model_declined(exc: Exception) -> bool:
    """True when *exc* is a model's decline, raised by the provider or ending a
    failover chain: the ``content_filter`` the Anthropic bridge answers, never
    a provider error."""
    return classify_run_failure(exc).cause is FailureCause.MODEL_DECLINED


def _end_declined_trace(trace_ctx: Any, decline: Exception, usage: dict | None) -> None:
    """Close the trace of a request the model declined: answered to the client
    as ``content_filter``, recorded as the ``model_declined`` failure with what
    the decline consumed (a decline is billed)."""
    _end_langfuse_trace(
        trace_ctx,
        error=str(decline),
        usage=usage or None,
        failure=classify_run_failure(decline),
    )


# A ``tool_choice`` that forces a tool call: OpenAI's ``required`` and named
# function, and Anthropic's ``any`` / ``tool`` a client may send through.
_FORCED_TOOL_CHOICES = frozenset({"required", "any"})
_FORCED_TOOL_CHOICE_TYPES = frozenset({"function", "tool", "any"})


def _proxy_tool_choice(model: str, tool_choice: str | dict) -> str | dict:
    """*tool_choice* as *model* takes it, on a model that refuses a forced tool
    call (Claude Sonnet 5.5) with HTTP 400: ``auto`` in place of a forced
    choice, and OpenAI's ``allowed_tools`` kept with ``mode: "auto"`` in place
    of ``required``. The Anthropic bridge then maps it to Anthropic's form
    with ``_anthropic_tool_choice``."""
    from app.llm.model_capabilities import refuses_forced_tool_choice

    if not refuses_forced_tool_choice(model):
        return tool_choice
    if isinstance(tool_choice, str):
        if tool_choice not in _FORCED_TOOL_CHOICES:
            return tool_choice
        relaxed: str | dict = "auto"
    elif not isinstance(tool_choice, dict):
        return tool_choice
    elif tool_choice.get("type") in _FORCED_TOOL_CHOICE_TYPES:
        relaxed = "auto"
    else:
        allowed = tool_choice.get("allowed_tools")
        if (
            tool_choice.get("type") != "allowed_tools"
            or not isinstance(allowed, dict)
            or allowed.get("mode") != "required"
        ):
            return tool_choice
        relaxed = {**tool_choice, "allowed_tools": {**allowed, "mode": "auto"}}
    logger.info(
        "LLM proxy: model %s refuses a forced tool_choice; sending it as 'auto'", model
    )
    return relaxed


async def _proxy_via_anthropic(
    provider: Any,
    body: Any,
    raw_messages: list[dict],
    effective_model_name: str,
    max_tokens: int,
    trace_ctx: Any,
    *,
    user_id: str,
) -> Any:
    """Handle chat completions request by bridging to the Anthropic Messages API.

    Converts OpenAI format → Anthropic, calls the API, converts back → OpenAI.
    Supports both streaming and non-streaming.
    """
    system_prompt, anthropic_msgs = _openai_msgs_to_anthropic(raw_messages)

    if not anthropic_msgs:
        anthropic_msgs = [{"role": "user", "content": "Hello"}]

    params: dict[str, Any] = {
        "model": effective_model_name,
        "messages": anthropic_msgs,
        "max_tokens": max_tokens,
    }
    if system_prompt:
        params["system"] = system_prompt
    from app.llm.model_capabilities import omit_temperature, supported_effort

    if body.temperature is not None and not omit_temperature(effective_model_name):
        params["temperature"] = body.temperature
    if body.top_p is not None:
        params["top_p"] = body.top_p
    if body.stop is not None:
        params["stop_sequences"] = (
            body.stop if isinstance(body.stop, list) else [body.stop]
        )
    # The level the model takes, as the provider sends it: one it refuses is
    # an HTTP 400 (model_capabilities.supported_effort).
    if effort := supported_effort(effective_model_name, body.reasoning_effort):
        params["output_config"] = {"effort": effort}
    # Tool calling, as the OpenAI-compatible pass-through forwards it.
    if anthropic_tools := _proxy_tools_to_anthropic(body.tools):
        params["tools"] = anthropic_tools
        choice = _anthropic_tool_choice(
            _proxy_tool_choice(effective_model_name, body.tool_choice)
            if body.tool_choice is not None
            else None,
            (body.model_extra or {}).get("parallel_tool_calls"),
        )
        if choice is not None:
            params["tool_choice"] = choice

    # ── Pre-flight context overflow check ────────────────────────────────
    from app.llm.anthropic_provider import (
        AnthropicProvider,
        _StreamUsageAccumulator,
    )

    text_chars, image_count, native_bytes = AnthropicProvider._measure_anthropic_payload(
        system_prompt, anthropic_msgs
    )
    try:
        provider._check_resolved_overflow(
            text_chars=text_chars,
            image_count=image_count,
            native_file_bytes=native_bytes,
            tools=_proxy_tools_to_definitions(body.tools) if "tools" in params else None,
            model=effective_model_name,
        )
    except ContextOverflowError as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        raise _overflow_http_exc(exc) from exc

    if body.stream:
        import time as _time

        async def _anthropic_stream() -> AsyncIterator[str]:
            full_content = ""
            usage_dict = None
            # The usage the stream has reported so far, for a client that
            # leaves before ``get_final_message``.
            usage_so_far = _StreamUsageAccumulator()
            ended = False
            # The OpenAI ``tool_calls`` index of each Anthropic content block
            # that is a ``tool_use``, keyed by the block's index.
            tool_call_index: dict[int, int] = {}
            try:
                async with provider.client.messages.stream(**params) as s:
                    _in_thinking = False
                    async for event in s:
                        etype = getattr(event, "type", "")
                        if etype == "message_start":
                            usage_so_far.absorb_message_start(event)
                        elif etype == "message_delta":
                            usage_so_far.absorb_message_delta(event)
                        elif etype == "content_block_start":
                            block = getattr(event, "content_block", None)
                            _in_thinking = getattr(block, "type", "") == "thinking"
                            if getattr(block, "type", "") == "tool_use":
                                position = len(tool_call_index)
                                tool_call_index[getattr(event, "index", position)] = position
                                yield _tool_call_chunk(
                                    effective_model_name,
                                    {
                                        "index": position,
                                        "id": getattr(block, "id", ""),
                                        "type": "function",
                                        "function": {
                                            "name": getattr(block, "name", ""),
                                            "arguments": "",
                                        },
                                    },
                                )
                        elif etype == "content_block_stop":
                            _in_thinking = False
                        elif etype == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            dtype = getattr(delta, "type", "")
                            if dtype == "input_json_delta":
                                partial = getattr(delta, "partial_json", "")
                                position = tool_call_index.get(
                                    getattr(event, "index", -1)
                                )
                                if partial and position is not None:
                                    yield _tool_call_chunk(
                                        effective_model_name,
                                        {
                                            "index": position,
                                            "function": {"arguments": partial},
                                        },
                                    )
                            elif dtype == "thinking_delta":
                                text = getattr(delta, "thinking", "")
                                if text:
                                    chunk = {
                                        "id": "chatcmpl-stream",
                                        "object": "chat.completion.chunk",
                                        "created": int(_time.time()),
                                        "model": effective_model_name,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {
                                                    "reasoning_content": text,
                                                },
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
                            elif dtype == "text_delta" and not _in_thinking:
                                text = getattr(delta, "text", "")
                                if text:
                                    full_content += text
                                    chunk = {
                                        "id": "chatcmpl-stream",
                                        "object": "chat.completion.chunk",
                                        "created": int(_time.time()),
                                        "model": effective_model_name,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": text},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
                    final_msg = await s.get_final_message()
                    stream_cut = answer_was_cut(final_msg)
                    stream_declined = (
                        getattr(final_msg, "stop_reason", None) == "refusal"
                    )
                    stream_tool_calls = bool(tool_call_index) or (
                        getattr(final_msg, "stop_reason", None) == "tool_use"
                    )
                    if final_msg and getattr(final_msg, "usage", None):
                        u = final_msg.usage
                        usage_dict = {
                            "input_tokens": getattr(u, "input_tokens", 0),
                            "output_tokens": getattr(u, "output_tokens", 0),
                            "cache_creation_input_tokens": getattr(
                                u, "cache_creation_input_tokens", 0
                            ),
                            "cache_read_input_tokens": getattr(
                                u, "cache_read_input_tokens", 0
                            ),
                        }
                # Final chunk with finish_reason
                final_chunk = {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": int(_time.time()),
                    "model": effective_model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": (
                                "content_filter"
                                if stream_declined
                                else "length"
                                if stream_cut
                                else "tool_calls"
                                if stream_tool_calls
                                else "stop"
                            ),
                        }
                    ],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                ended = True
                if stream_declined:
                    _end_declined_trace(
                        trace_ctx,
                        declined_error(
                            effective_model_name,
                            getattr(final_msg, "stop_details", None),
                            usage_dict or {},
                        ),
                        usage_dict,
                    )
                else:
                    _end_langfuse_trace(
                        trace_ctx,
                        output=full_content,
                        model=effective_model_name,
                        usage=usage_dict,
                    )
                await _meter_finished_stream(effective_model_name, user_id)
            except ContextOverflowError as exc:
                _end_langfuse_trace(trace_ctx, error=str(exc))
                logger.error("LLM proxy streaming error: %s", format_exception_message(exc), exc_info=True)
                yield f"data: {_overflow_sse_event(exc)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                _end_langfuse_trace(
                    trace_ctx, error=str(exc), usage=usage_so_far.to_dict() or None
                )
                logger.error("LLM proxy streaming error: %s", format_exception_message(exc), exc_info=True)
                if _is_non_chat_model_error(exc):
                    error_data = _chat_model_mismatch_error_detail(
                        effective_model_name
                    )
                else:
                    error_data = {
                        "error": {
                            "message": "An internal error occurred while processing the request.",
                            "type": "server_error",
                        }
                    }
                yield f"data: {json.dumps(error_data)}\n\n"
                yield "data: [DONE]\n\n"
            except (GeneratorExit, asyncio.CancelledError) as exc:
                if not ended:
                    _end_abandoned_stream(
                        trace_ctx,
                        exc,
                        output=full_content,
                        model=effective_model_name,
                        usage=usage_so_far.to_dict(),
                    )
                    _meter_stream(effective_model_name, user_id)
                raise

        return StreamingResponse(
            _anthropic_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming
    try:
        result = await provider.client.messages.create(**params)
    except ContextOverflowError as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        raise _overflow_http_exc(exc) from exc
    except Exception as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        logger.error("LLM provider error: %s", format_exception_message(exc), exc_info=True)
        raise HTTPException(
            status_code=502, detail="LLM provider error. Please try again."
        ) from exc

    response_dict = _anthropic_response_to_openai(result, effective_model_name)
    content = (
        response_dict["choices"][0]["message"]["content"]
        if response_dict["choices"]
        else ""
    )
    usage_dict = None
    if getattr(result, "usage", None):
        u = result.usage
        usage_dict = {
            "input_tokens": getattr(u, "input_tokens", 0),
            "output_tokens": getattr(u, "output_tokens", 0),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
        }
    if getattr(result, "stop_reason", None) == "refusal":
        _end_declined_trace(
            trace_ctx,
            declined_error(
                effective_model_name,
                getattr(result, "stop_details", None),
                usage_dict or {},
            ),
            usage_dict,
        )
    else:
        _end_langfuse_trace(
            trace_ctx, output=content, model=effective_model_name, usage=usage_dict
        )
    await _track_model_usage(effective_model_name, user_id=user_id)
    return response_dict


async def _proxy_via_provider_chat(
    provider: Any,
    body: Any,
    raw_messages: list[dict],
    effective_model_name: str,
    max_tokens: int,
    trace_ctx: Any,
    *,
    user_id: str,
) -> Any:
    """Handle chat completions for non-OpenAI, non-Anthropic providers (Gemini, Bedrock).

    Uses the provider's own chat() method which handles native format conversion,
    then wraps the ChatResponse into OpenAI chat completions format.
    """
    from app.llm.base import ChatMessage

    chat_messages = []
    for m in raw_messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        chat_messages.append(ChatMessage(role=role, content=content or ""))

    extra_kwargs: dict[str, Any] = {}
    if body.reasoning_effort:
        extra_kwargs["reasoning_effort"] = body.reasoning_effort

    if body.stream:
        import time as _time

        async def _generic_stream() -> AsyncIterator[str]:
            full_content = ""
            thinking_queue: asyncio.Queue[str] = asyncio.Queue()
            # This call's usage as the provider reports it while it streams
            # (Gemini on every chunk, Bedrock in its closing ``metadata``
            # event). Per call: ``last_usage`` lives on a provider instance
            # other requests share.
            usage_sink = StreamUsageSink()
            # How THIS stream ended, for the same reason.
            stream_stop = StreamStopReason(provider)
            ended = False
            decline: Exception | None = None

            async def _on_thinking(text: str) -> None:
                await thinking_queue.put(text)

            # Same as the non-streaming call below: a failover re-points the trace.
            _trace_token = set_current_trace_ctx(trace_ctx) if trace_ctx else None
            try:
                try:
                    async for chunk in provider.chat_stream(
                        chat_messages,
                        model=effective_model_name,
                        temperature=body.temperature or 0.7,
                        max_tokens=max_tokens,
                        on_thinking=_on_thinking,
                        usage_sink=usage_sink,
                        **extra_kwargs,
                    ):
                        while not thinking_queue.empty():
                            think_text = thinking_queue.get_nowait()
                            data = {
                                "id": "chatcmpl-stream",
                                "object": "chat.completion.chunk",
                                "created": int(_time.time()),
                                "model": effective_model_name,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "reasoning_content": think_text,
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(data)}\n\n"
                        full_content += chunk
                        data = {
                            "id": "chatcmpl-stream",
                            "object": "chat.completion.chunk",
                            "created": int(_time.time()),
                            "model": effective_model_name,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": chunk},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(data)}\n\n"
                except Exception as exc:
                    if not _model_declined(exc):
                        raise
                    decline = exc
                final = {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": int(_time.time()),
                    "model": effective_model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": (
                                "content_filter"
                                if decline is not None
                                else "length" if stream_stop.was_cut() else "stop"
                            ),
                        }
                    ],
                }
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"
                ended = True
                if decline is not None:
                    _end_declined_trace(trace_ctx, decline, usage_sink.take())
                else:
                    _end_langfuse_trace(
                        trace_ctx,
                        output=full_content,
                        model=effective_model_name,
                        usage=usage_sink.take() or None,
                    )
                await _meter_finished_stream(effective_model_name, user_id)
            except ContextOverflowError as exc:
                _end_langfuse_trace(trace_ctx, error=str(exc))
                logger.error("LLM proxy streaming error: %s", format_exception_message(exc), exc_info=True)
                yield f"data: {_overflow_sse_event(exc)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                _end_langfuse_trace(
                    trace_ctx, error=str(exc), usage=usage_sink.take() or None
                )
                logger.error("LLM proxy streaming error: %s", format_exception_message(exc), exc_info=True)
                error_data = {
                    "error": {
                        "message": "An internal error occurred while processing the request.",
                        "type": "server_error",
                    }
                }
                yield f"data: {json.dumps(error_data)}\n\n"
                yield "data: [DONE]\n\n"
            except (GeneratorExit, asyncio.CancelledError) as exc:
                if not ended:
                    _end_abandoned_stream(
                        trace_ctx,
                        exc,
                        output=full_content,
                        model=effective_model_name,
                        usage=usage_sink.take(),
                    )
                    _meter_stream(effective_model_name, user_id)
                raise
            finally:
                if _trace_token is not None:
                    with contextlib.suppress(Exception):
                        reset_current_trace_ctx(_trace_token)

        return StreamingResponse(
            _generic_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming
    decline: Exception | None = None
    try:
        # The provider may be a failover wrapper: with this trace current, a
        # fallback that answers re-points the trace at its own config.
        _trace_token = set_current_trace_ctx(trace_ctx) if trace_ctx else None
        try:
            result = await provider.chat(
                chat_messages,
                model=effective_model_name,
                temperature=body.temperature or 0.7,
                max_tokens=max_tokens,
                **extra_kwargs,
            )
        finally:
            if _trace_token is not None:
                with contextlib.suppress(Exception):
                    reset_current_trace_ctx(_trace_token)
    except ContextOverflowError as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        raise _overflow_http_exc(exc) from exc
    except Exception as exc:
        if not _model_declined(exc):
            _end_langfuse_trace(trace_ctx, error=str(exc))
            logger.error("LLM provider error: %s", format_exception_message(exc), exc_info=True)
            raise HTTPException(
                status_code=502, detail="LLM provider error. Please try again."
            ) from exc
        decline = exc
        result = ChatResponse(
            content="",
            model=effective_model_name,
            usage=exc.usage if isinstance(exc, ModelDeclinedError) else {},
        )

    content = result.content or ""
    usage_dict = result.usage if result.usage else None
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if result.thinking:
        message["reasoning_content"] = result.thinking

    response_dict = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": effective_model_name,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": (
                    "content_filter"
                    if decline is not None
                    else "length" if answer_was_cut(result) else "stop"
                ),
            }
        ],
        "usage": usage_dict
        or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    if decline is not None:
        _end_declined_trace(trace_ctx, decline, usage_dict)
    else:
        _end_langfuse_trace(
            trace_ctx, output=content, model=effective_model_name, usage=usage_dict
        )
    await _track_model_usage(effective_model_name, user_id=user_id)
    return response_dict


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/chat/completions")
async def proxy_chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Chat completions endpoint – works with all configured providers.

    Supports all LLM providers: OpenAI, Anthropic, Gemini, OpenRouter,
    Ollama, Mistral, Azure OpenAI, Bedrock, LiteLLM, Custom OpenAI.

    For Anthropic models, requests are automatically converted between
    chat completions format and the Anthropic Messages API.
    """
    user = await _authenticate_api_key(request, db)

    # Community Edition blocks all chat — including the OpenAI-compatible
    # proxy used by Cursor/Claude Code/etc. The proxy is the easiest backdoor
    # around the UI splash, so it has to enforce the same gate.
    await _refuse_unless_chat_enabled(db)

    logger.info(
        "Proxy request from %s: model=%s, stream=%s, messages=%d",
        user.email,
        body.model,
        body.stream,
        len(body.messages),
    )

    requested_model = body.model
    routes_to_agent = bool(requested_model and requested_model.startswith("agent:"))
    if not routes_to_agent:
        # A raw model is no agent: a key restricted to agents stops here.
        require_model_access(user)

    # ── Quota enforcement ────────────────────────────────────────────────
    from app.services.quota import (
        QuotaExceededError,
        check_llm_proxy_quota,
        seconds_until_period_reset,
    )

    try:
        await check_llm_proxy_quota(db, user.id, requested_model)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(seconds_until_period_reset(exc.period))},
        ) from exc

    # ── Agent routing: model = "agent:<slug>" ────────────────────────────
    if routes_to_agent:
        agent_slug = (requested_model or "")[6:]
        return await _handle_agent_request(db, user, agent_slug, body, request)

    # ── Resolve provider (exact model match only) ────────────────────────
    explicit_model = bool(requested_model) and requested_model.lower() not in ("auto", "default", "")
    try:
        if explicit_model:
            from app.llm.router import resolve_provider_for_model

            provider, effective_model = await resolve_provider_for_model(
                db, requested_model, user_id=str(user.id)
            )
        else:
            provider, effective_model = await get_llm_for_user(db, user_id=str(user.id))
    except ProviderNotUsableError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{requested_model}' is not available. "
                if explicit_model
                else "No model is available for this request. "
            )
            + "Check the available models in Settings > AI and models > Models.",
        ) from exc

    # A failover wrapper created only for the primary's own decline fallbacks
    # (no cross-provider chain, no admin fallback model) has no
    # ``.client``, so the dispatch below would drop to the generic
    # provider-chat bridge - losing top_p / stop and the Anthropic overflow
    # pre-flight - and would re-wrap a raw provider error as
    # ``AllProvidersFailedError``. The proxy answers a decline itself
    # (``content_filter``) and does not fail over, so dispatch on the
    # underlying provider, exactly as the unwrapped single-provider install did.
    if isinstance(provider, FailoverLLMProvider) and not provider.has_configured_fallbacks:
        provider = provider.primary

    # ── Build common params ─────────────────────────────────────────────
    raw_messages = _build_raw_openai_messages(body.messages)

    from app.llm.model_capabilities import build_openai_generation_params

    effective_model_name = (
        effective_model or getattr(provider, "default_model", None) or "gpt-4o"
    )
    provider_name = getattr(provider, "provider_name", None)
    if not _supports_chat_completions(effective_model_name):
        raise HTTPException(
            status_code=400,
            detail=_chat_model_mismatch_error_detail(effective_model_name),
        )
    max_tokens = body.max_completion_tokens or body.max_tokens or 4096
    logger.info(
        "LLM proxy request: user=%s requested_model=%s effective_model=%s provider=%s",
        user.id,
        requested_model,
        effective_model_name,
        provider_name,
    )

    # ── Langfuse tracing (if enabled) ───────────────────────────────────
    trace_ctx = await trace_llm_call(
        db,
        name="llm-proxy",
        user_id=str(user.id),
        source="llm-proxy",
        model=effective_model_name,
        input_data=build_trace_input(
            messages=raw_messages,
            tools=body.tools,
        ),
        metadata={
            "provider": provider_name,
            "llm_provider": provider_name,
            **provider_config_metadata(provider),
            "stream": body.stream,
            "requested_model": requested_model,
        },
    )

    # ── Anthropic provider: convert OpenAI → Anthropic → OpenAI ────────
    if (
        not _has_openai_sdk(provider)
        and hasattr(provider, "client")
        and hasattr(provider.client, "messages")
    ):
        # Release the pooled DB connection (checked out at _authenticate_api_key)
        # BEFORE the bridge streams the completion. _proxy_via_anthropic streams
        # token-by-token from provider.client and only uses its own short-lived
        # sessions for usage tracking — it never touches this request's `db`
        # again. Without this, one main-pool connection stays pinned (idle-in-
        # transaction) for the whole Claude completion; Claude is the platform
        # default, so this streaming path is the dominant pool-exhaustion source
        # (the OpenAI-compatible path and /v1/messages already release).
        await release_db_connection_before_loop(db)
        return await _proxy_via_anthropic(
            provider,
            body,
            raw_messages,
            effective_model_name,
            max_tokens,
            trace_ctx,
            user_id=str(user.id),
        )

    # ── Non-OpenAI providers (Gemini, Bedrock, etc.): use provider.chat() ──
    if not _has_openai_sdk(provider):
        # Same release: the Gemini/Bedrock bridge streams from
        # provider.chat_stream and uses its own sessions, so the request's
        # pooled connection must not be held for the whole streamed completion.
        await release_db_connection_before_loop(db)
        return await _proxy_via_provider_chat(
            provider,
            body,
            raw_messages,
            effective_model_name,
            max_tokens,
            trace_ctx,
            user_id=str(user.id),
        )

    # ── Standard chat completions (OpenAI-compatible providers) ────────
    params: dict[str, Any] = {
        "model": effective_model_name,
        "messages": raw_messages,
    }
    params.update(
        build_openai_generation_params(
            effective_model_name,
            temperature=body.temperature,
            max_tokens=max_tokens,
        )
    )
    if body.top_p is not None:
        params["top_p"] = body.top_p
    if body.frequency_penalty is not None:
        params["frequency_penalty"] = body.frequency_penalty
    if body.presence_penalty is not None:
        params["presence_penalty"] = body.presence_penalty
    if body.stop is not None:
        params["stop"] = body.stop
    if body.tools:
        params["tools"] = body.tools
    if body.tool_choice is not None:
        params["tool_choice"] = _proxy_tool_choice(effective_model_name, body.tool_choice)
    # The per-model effort matrix the OpenAI-compatible providers apply: a
    # level the model refuses is an HTTP 400, so it is lowered to one it
    # takes or dropped; a value outside the platform's levels passes through.
    if effort := normalize_openai_reasoning_effort(
        body.reasoning_effort, effective_model_name, has_tools=bool(body.tools)
    ):
        params["reasoning_effort"] = effort
    if body.n is not None:
        params["n"] = body.n

    # ── Pre-flight context overflow check ────────────────────────────────
    text_chars, image_count, native_bytes = BaseLLMProvider._measure_oai_payload(
        raw_messages
    )
    try:
        provider._check_resolved_overflow(
            text_chars=text_chars,
            image_count=image_count,
            native_file_bytes=native_bytes,
            tools=_proxy_tools_to_definitions(body.tools),
            model=effective_model_name,
        )
    except ContextOverflowError as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        raise _overflow_http_exc(exc) from exc

    # ── Streaming ────────────────────────────────────────────────────────
    if body.stream:
        stream_params = {
            **params,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            stream = await provider.client.chat.completions.create(**stream_params)
        except Exception as exc:
            _end_langfuse_trace(trace_ctx, error=str(exc))
            if http_exc := _provider_client_error(exc):
                logger.warning(
                    "LLM provider client error: provider=%s requested_model=%s "
                    "effective_model=%s user=%s status=%s detail=%s",
                    provider_name,
                    requested_model,
                    effective_model_name,
                    user.email,
                    http_exc.status_code,
                    http_exc.detail,
                )
                raise http_exc from exc
            logger.error("LLM proxy streaming setup error: %s", format_exception_message(exc), exc_info=True)
            raise HTTPException(
                status_code=502, detail="LLM provider error. Please try again."
            ) from exc

        async def _stream() -> AsyncIterator[str]:
            full_content = ""
            usage_dict = None
            filtered = None
            # The chunk being handed to the client and not read yet: a client
            # that leaves at that ``yield`` still leaves its usage behind.
            unread = None
            ended = False

            def _read(chunk: Any) -> None:
                nonlocal full_content, usage_dict, filtered
                if chunk.choices:
                    # A string, or Mistral's list of ``thinking`` / ``text``
                    # chunks once reasoning is on: the answer is the text.
                    full_content += openai_content_parts(chunk.choices[0].delta.content)[0]
                    filtered = content_filter_finish(chunk.choices[0]) or filtered
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    usage_dict = {
                        "prompt_tokens": getattr(u, "prompt_tokens", 0),
                        "completion_tokens": getattr(u, "completion_tokens", 0),
                        "total_tokens": getattr(u, "total_tokens", 0),
                    }

            try:
                async for chunk in stream:
                    unread = chunk
                    # ``warnings=False``: a Mistral ``content`` list is not the
                    # SDK's declared string; pydantic would log a warning that
                    # quotes the model's output for every such chunk.
                    yield f"data: {chunk.model_dump_json(warnings=False)}\n\n"
                    unread = None
                    _read(chunk)
                yield "data: [DONE]\n\n"
                ended = True
                # The client got ``content_filter`` as the upstream sent it;
                # the trace records the decline, not an answer.
                if decline := declined_by_filter(filtered, effective_model_name, usage_dict or {}):
                    _end_declined_trace(trace_ctx, decline, usage_dict)
                else:
                    _end_langfuse_trace(
                        trace_ctx,
                        output=full_content,
                        model=effective_model_name,
                        usage=usage_dict,
                    )
                await _meter_finished_stream(effective_model_name, str(user.id))
            except ContextOverflowError as exc:
                _end_langfuse_trace(trace_ctx, error=str(exc))
                logger.error("LLM proxy streaming error: %s", format_exception_message(exc), exc_info=True)
                yield f"data: {_overflow_sse_event(exc)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                _end_langfuse_trace(trace_ctx, error=str(exc), usage=usage_dict)
                logger.error("LLM proxy streaming error: %s", format_exception_message(exc), exc_info=True)
                if _is_non_chat_model_error(exc):
                    error_data = _chat_model_mismatch_error_detail(effective_model_name)
                    yield f"data: {json.dumps(error_data)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                error_data = {
                    "error": {
                        "message": "An internal error occurred while processing the request.",
                        "type": "server_error",
                    }
                }
                yield f"data: {json.dumps(error_data)}\n\n"
                yield "data: [DONE]\n\n"
            except (GeneratorExit, asyncio.CancelledError) as exc:
                if not ended:
                    if unread is not None:
                        with contextlib.suppress(Exception):
                            _read(unread)
                    _end_abandoned_stream(
                        trace_ctx,
                        exc,
                        output=full_content,
                        model=effective_model_name,
                        usage=usage_dict,
                    )
                    _meter_stream(effective_model_name, str(user.id))
                raise

        # Return the pooled DB connection before the (potentially multi-minute)
        # streamed completion. ``_stream`` uses its own short-lived session for
        # usage tracking and never touches the request ``db`` again, but the
        # ``get_db`` teardown only runs when the stream ends. Without this
        # release, each in-flight proxy stream pins one main-pool connection for
        # the whole generation, exhausting the pool under concurrent
        # Cursor/Claude Code/OpenAI-compatible traffic.
        await release_db_connection_before_loop(db)
        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Non-streaming ────────────────────────────────────────────────────
    # Return the pooled DB connection before the upstream completion call:
    # a non-streaming generation routinely takes 10-120s and everything
    # after it (`_end_langfuse_trace` is sync, `_track_model_usage` opens
    # its own session) never touches the request ``db`` again. Without this
    # release, each in-flight non-streaming completion pins one main-pool
    # connection for the whole generation.
    await release_db_connection_before_loop(db)
    try:
        result = await provider.client.chat.completions.create(**params)
    except Exception as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        # Specific chat-model-mismatch mapping comes first so the caller
        # gets the OpenAI-style invalid_request_error body instead of a
        # generic 4xx — see ``_chat_model_mismatch_error_detail`` for the
        # body shape (matches the streaming branches' shape too).
        if _is_non_chat_model_error(exc):
            raise HTTPException(
                status_code=400,
                detail=_chat_model_mismatch_error_detail(effective_model_name),
            ) from exc
        # Other client-side (4xx) provider errors get the explicit
        # provider-client-error mapping from ``_provider_client_error``,
        # which preserves the upstream status code + detail.
        if http_exc := _provider_client_error(exc):
            logger.warning(
                "LLM provider client error: provider=%s requested_model=%s "
                "effective_model=%s user=%s status=%s detail=%s",
                provider_name,
                requested_model,
                effective_model_name,
                user.email,
                http_exc.status_code,
                http_exc.detail,
            )
            raise http_exc from exc
        logger.error("LLM provider error: %s", format_exception_message(exc), exc_info=True)
        raise HTTPException(
            status_code=502, detail="LLM provider error. Please try again."
        ) from exc

    # Return the SDK response directly — fully transparent
    response_dict = result.model_dump(warnings=False)
    content = ""
    if result.choices:
        content = openai_content_parts(result.choices[0].message.content)[0]
    usage_dict = None
    if getattr(result, "usage", None):
        u = result.usage
        usage_dict = {
            "prompt_tokens": getattr(u, "prompt_tokens", 0),
            "completion_tokens": getattr(u, "completion_tokens", 0),
            "total_tokens": getattr(u, "total_tokens", 0),
        }
    if result.choices and (
        decline := declined_by_filter(result.choices[0], effective_model_name, usage_dict or {})
    ):
        _end_declined_trace(trace_ctx, decline, usage_dict)
    else:
        _end_langfuse_trace(
            trace_ctx, output=content, model=effective_model_name, usage=usage_dict
        )
    await _track_model_usage(effective_model_name, user_id=str(user.id))

    return response_dict


@router.get("/models")
async def proxy_list_models(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible models listing.

    Returns both admin-configured LLM models and available agents.
    Agents are listed with model ID ``agent:<slug>`` and ``owned_by: "copilot"``.
    """
    user = await _authenticate_api_key(request, db)

    all_models: list[dict] = []
    # A key restricted to agents lists those agents only.
    if lists_raw_models(user):
        result = await db.execute(select(LLMProviderConfig))
        routing = await provider_routing(db, str(user.id), result.scalars().all())
        for config in routing.usable:
            for m in config.available_models or []:
                all_models.append(
                    {
                        "id": m,
                        "object": "model",
                        "created": int(config.created_at.timestamp()),
                        "owned_by": config.provider,
                    }
                )

    user_group_ids = await get_user_group_ids(db, user.id)
    agent_result = await db.execute(
        select(Agent).where(
            user_visible_filter(Agent, user.id, user_group_ids),
            Agent.enabled.is_(True),
            Agent.disable_chat.is_(False),
            Agent.slug.isnot(None),
        )
    )
    # A read-only key runs no agent, a restricted key only the ones it names.
    for agent in agents_for_key(user, agent_result.scalars().all()):
        all_models.append(
            {
                "id": f"agent:{agent.slug}",
                "object": "model",
                "created": int(agent.created_at.timestamp()),
                "owned_by": "copilot",
                "meta": {
                    "type": "agent",
                    "name": agent.name,
                    "description": agent.description,
                },
            }
        )

    if not all_models:
        raise HTTPException(
            status_code=503, detail="No LLM providers or agents configured"
        )

    return {"object": "list", "data": all_models}


@router.get("/quota")
async def proxy_quota_usage(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """The key owner's current LLM proxy quota usage: no completion, no counter moved.

    ``{used, limit, period, period_key, resets_at, per_model}``, the numbers
    ``check_llm_proxy_quota`` compares (``services/quota.py``
    ``llm_proxy_quota_usage``). A key scoped to the proxy alone cannot reach
    ``/api/v1/settings/my-quotas``; this is how a lab tells whether a step spent
    a call (declared check ``lab-model-quota``).
    """
    from app.services.quota import llm_proxy_quota_usage

    user = await _authenticate_api_key(request, db)
    return await llm_proxy_quota_usage(db, user.id)


# ── Agent routing helper ─────────────────────────────────────────────────────


@known_secrets_scoped()
async def _handle_agent_request(
    db: AsyncSession,
    user: User,
    agent_slug: str,
    body: ChatCompletionRequest,
    request: Request,
):
    """Route a proxy request through an agent's full agentic loop."""
    from app.services.agent_chat import run_agent_chat

    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(Agent).where(
            Agent.slug == agent_slug,
            user_visible_filter(Agent, user.id, user_group_ids),
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_slug}' not found. Use GET /v1/models to list available agents.",
        )
    # Before anything runs: the key's agent restriction, and read-only.
    require_agent_run(user, agent.id)
    if not agent.enabled:
        raise HTTPException(status_code=400, detail=f"Agent '{agent_slug}' is disabled")
    if agent.disable_chat:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent_slug}' is not available for chat or proxy use",
        )

    # Pass all request headers — the resolver extracts scope values
    # from headers listed in each integration's scope_config.
    _scope_headers: dict[str, str] = dict(request.headers)

    history = [
        ChatMessage(role=m.role, content=_extract_text_content(m.content))
        for m in body.messages[:-1]
    ]
    user_message = (
        _extract_text_content(body.messages[-1].content) if body.messages else ""
    )

    # Trace metadata must carry the effective provider so pricing can resolve
    # costs for proxy-agent traces even when the agent inherits defaults.
    from app.llm.agent_model import agent_model_selection

    _agent_model = await agent_model_selection(db, agent)
    proxy_provider_name = _agent_model.provider
    if not proxy_provider_name:
        from app.llm.router import get_llm_for_user

        try:
            _proxy_llm, _ = await get_llm_for_user(
                db,
                user_id=str(user.id),
                provider_override=_agent_model.provider,
                model_override=_agent_model.model,
                provider_id_override=_agent_model.provider_id,
            )
            proxy_provider_name = getattr(_proxy_llm, "provider_name", None)
        except Exception:
            proxy_provider_name = None

    trace_ctx = await trace_llm_call(
        db,
        name="llm-proxy-agent",
        user_id=str(user.id),
        source=agent.slug or agent.name,
        model=_agent_model.model,
        input_data=build_trace_input(
            messages=[
                {"role": m.role, "content": m.content} for m in body.messages
            ]
            if body.messages
            else None,
            tools=getattr(body, "tools", None),
        ),
        agent_id=str(agent.id),
        agent_name=agent.name,
        agent_slug=agent.slug,
        metadata={
            "provider": proxy_provider_name,
            "llm_provider": proxy_provider_name,
            "agent_template": agent.template_slug,
            "is_company_managed": agent.is_company_managed,
            "stream": body.stream,
        },
    )

    try:
        # Push the proxy trace onto the contextvar so the inner
        # ``run_agent_chat`` opens its own trace as a child observation
        # instead of as a disconnected sibling root in the Langfuse UI.
        _proxy_token = set_current_trace_ctx(trace_ctx) if trace_ctx else None
        try:
            response_text, _new_agent_id = await run_agent_chat(
                db=db,
                user_id=str(user.id),
                agent=agent,
                history=history,
                user_message=user_message,
                allow_handover=False,
                scope_headers=_scope_headers or None,
            )
        finally:
            if _proxy_token is not None:
                with contextlib.suppress(Exception):
                    reset_current_trace_ctx(_proxy_token)
    except ContextOverflowError as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        raise _overflow_http_exc(exc) from exc
    except Exception as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        logger.error(
            "Agent proxy error for user %s, agent %s: %s", user.email, agent_slug, format_exception_message(exc)
        )
        raise HTTPException(
            status_code=502, detail="Agent error. Please try again."
        ) from exc

    _end_langfuse_trace(trace_ctx, output=response_text, model=_agent_model.model)
    # The caller reads the answer: a secret variable value it echoed
    # is the display text.
    response_text = redact_known_secrets_for_display(response_text)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    agent_model = f"agent:{agent_slug}"
    created = int(time.time())

    if body.stream:

        async def _agent_stream() -> AsyncIterator[str]:
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': agent_model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': agent_model, 'choices': [{'index': 0, 'delta': {'content': response_text}, 'finish_reason': None}]})}\n\n"
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': agent_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _agent_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": agent_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ── Anthropic Messages API Pass-Through (Claude Code compatibility) ──────────
#
# Claude Code requires the Anthropic Messages API format (``/v1/messages``)
# with ``anthropic-beta`` and ``anthropic-version`` headers forwarded.
# These endpoints act as a raw HTTP pass-through to the Anthropic API,
# authenticating with the platform API key (``fcp-...``) and forwarding
# the request/response without transformation.

ANTHROPIC_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_FORWARD_HEADERS = ("anthropic-beta", "anthropic-version")
_ANTHROPIC_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
_ANTHROPIC_COUNT_TOKENS_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def _get_anthropic_config(
    db: AsyncSession, model_id: str | None = None, *, user_id: object = None
) -> LLMProviderConfig:
    """The Anthropic config that serves a pass-through request.

    Several Anthropic configs may exist (an env-seeded row plus a UI-created
    one, for instance), so the row is chosen by ``choose_provider_config`` rather
    than read as the only one: ``scalar_one_or_none()`` raised on the second
    row and turned every pass-through request into a 500.

    Chosen among the configs *user_id* may use, their team's first: a
    caller none of them serves is told it is a matter of access (403), which
    an administrator can grant, not of a missing provider.
    """
    result = await db.execute(
        select_provider_configs().where(LLMProviderConfig.provider == "anthropic")
    )
    configs = list(result.scalars().all())
    routing = await provider_routing(db, user_id, configs)
    if routing.is_reserved(model_id):
        raise HTTPException(status_code=403, detail=routing.reserved_model_message(model_id))
    config = routing.choose("anthropic", model_id)
    if not config:
        if configs or routing.audience.ai_disabled:
            raise HTTPException(status_code=403, detail=routing.no_usable_message())
        raise HTTPException(
            status_code=503,
            detail="No Anthropic provider configured. An admin must add an "
            "Anthropic API key in Settings > AI and models > Models.",
        )
    return config


def _build_anthropic_headers(request: Request, api_key: str) -> dict[str, str]:
    """Build headers for forwarding to the Anthropic API.

    Includes the platform API key and forwards ``anthropic-beta`` and
    ``anthropic-version`` from the incoming request (required by Claude Code
    for feature negotiation).
    """
    headers: dict[str, str] = {
        "x-api-key": api_key,
        "content-type": "application/json",
    }
    for name in _ANTHROPIC_FORWARD_HEADERS:
        if value := request.headers.get(name):
            headers[name] = value
    return headers


def _anthropic_response(
    resp: httpx.Response,
    media_type: str = "application/json",
) -> Response:
    """Build a FastAPI Response from an upstream Anthropic HTTP response.

    Forwards the ``request-id`` header when present (useful for debugging).
    """
    response_headers: dict[str, str] = {}
    if request_id := resp.headers.get("request-id"):
        response_headers["request-id"] = request_id
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type", media_type),
    )


@router.post("/messages")
async def proxy_anthropic_messages(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Anthropic Messages API pass-through for Claude Code compatibility.

    Accepts requests in Anthropic Messages format, authenticates with the
    platform API key (``fcp-...``), and forwards to the Anthropic API with
    ``anthropic-beta`` and ``anthropic-version`` headers preserved.

    Both streaming (``stream: true``) and non-streaming modes are supported.
    Responses are returned as-is from Anthropic without transformation.
    """
    user = await _authenticate_api_key(request, db)
    # A raw model pass-through: no agent for a restricted key to reach.
    require_model_access(user)

    # See ``proxy_chat_completions`` — the Anthropic pass-through is the
    # other "OpenAI-equivalent" backdoor and must enforce the same gate.
    await _refuse_unless_chat_enabled(db)

    body = await request.body()

    body_json: dict | None = None
    try:
        body_json = json.loads(body)
        is_stream = body_json.get("stream", False)
        requested_model = body_json.get("model")
    except (json.JSONDecodeError, AttributeError):
        is_stream = False
        requested_model = None

    anthropic_config = await _get_anthropic_config(
        db,
        requested_model if isinstance(requested_model, str) else None,
        user_id=str(user.id),
    )
    headers = _build_anthropic_headers(request, await reveal_field(anthropic_config, "api_key_enc") or "")

    # ── Quota enforcement ────────────────────────────────────────────────
    from app.services.quota import (
        QuotaExceededError,
        check_llm_proxy_quota,
        seconds_until_period_reset,
    )

    try:
        await check_llm_proxy_quota(db, user.id, requested_model)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(seconds_until_period_reset(exc.period))},
        ) from exc

    trace_input = None
    if body_json:
        msgs = body_json.get("messages", [])
        if msgs:
            sys_prompt = body_json.get("system")
            if isinstance(sys_prompt, list):
                # Anthropic supports system as list of content blocks; join text parts.
                sys_prompt = "\n".join(
                    p.get("text", "") for p in sys_prompt if isinstance(p, dict)
                )
            trace_input = build_trace_input(
                messages=msgs,
                system_prompt=sys_prompt if isinstance(sys_prompt, str) else None,
                tools=body_json.get("tools"),
            )

    trace_ctx = await trace_llm_call(
        db,
        name="anthropic-messages-proxy",
        user_id=str(user.id),
        source="llm-proxy",
        model=requested_model,
        input_data=trace_input,
        metadata={
            # The type is right by construction (this endpoint only ever calls
            # the Anthropic API); the config says whose key it used.
            "provider": "anthropic",
            "llm_provider": "anthropic",
            **config_metadata(anthropic_config),
            "stream": is_stream,
            "pass_through": True,
        },
    )

    await _track_model_usage(
        requested_model or "anthropic-unknown", user_id=str(user.id)
    )

    url = f"{ANTHROPIC_API_BASE}/v1/messages"

    if is_stream:
        client = httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT)
        try:
            upstream = await client.send(
                client.build_request("POST", url, headers=headers, content=body),
                stream=True,
            )
        except httpx.TimeoutException as exc:
            await client.aclose()
            _end_langfuse_trace(trace_ctx, error="Anthropic API timeout")
            raise HTTPException(
                status_code=504, detail="Anthropic API timeout"
            ) from exc
        except Exception as exc:
            await client.aclose()
            _end_langfuse_trace(trace_ctx, error=str(exc))
            logger.error("Anthropic API error: %s", format_exception_message(exc), exc_info=True)
            raise HTTPException(
                status_code=502, detail="Anthropic API error. Please try again."
            ) from exc

        if upstream.status_code != 200:
            error_body = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            _end_langfuse_trace(trace_ctx, error=f"HTTP {upstream.status_code}")
            return Response(
                content=error_body,
                status_code=upstream.status_code,
                media_type="application/json",
            )

        async def _pipe_stream() -> AsyncIterator[bytes]:
            """Pipe SSE bytes through while extracting usage from Anthropic events.

            Anthropic streaming sends ``message_start`` with
            ``message.usage.input_tokens`` and ``message_delta`` events with
            the cumulative ``usage.output_tokens``.  We parse these on-the-fly
            so Langfuse gets accurate token counts.
            """
            tally = _AnthropicSseTally()
            # The bytes being handed to the client and not read yet: a client
            # that leaves at that ``yield`` still leaves their usage behind.
            unread = b""
            ended = False
            try:
                async for raw_chunk in upstream.aiter_bytes():
                    unread = raw_chunk
                    yield raw_chunk
                    unread = b""
                    tally.feed(raw_chunk)
                ended = True
                _end_langfuse_trace(
                    trace_ctx,
                    output=tally.text(),
                    model=requested_model,
                    usage=tally.usage(),
                )
            except Exception as exc:
                _end_langfuse_trace(trace_ctx, error=str(exc), usage=tally.usage())
            except (GeneratorExit, asyncio.CancelledError) as exc:
                # Metered before the request was forwarded: only the trace.
                if not ended:
                    with contextlib.suppress(Exception):
                        tally.feed(unread)
                    _end_abandoned_stream(
                        trace_ctx,
                        exc,
                        output=tally.text(),
                        model=requested_model,
                        usage=tally.usage(),
                    )
                raise
            finally:
                await upstream.aclose()
                await client.aclose()

        # Return the pooled DB connection before the streamed pass-through:
        # ``_pipe_stream`` only uses its own httpx client and never touches the
        # request ``db`` again, so holding it here would pin one main-pool
        # connection per in-flight Anthropic stream (used by Claude Code).
        await release_db_connection_before_loop(db)
        return StreamingResponse(
            _pipe_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming pass-through: release the pooled DB connection before
    # the upstream Anthropic call (used by Claude Code with stream=false) —
    # the generation can take minutes and nothing below touches the request
    # ``db`` again (usage tracking opens its own session).
    await release_db_connection_before_loop(db)
    try:
        async with httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, content=body)
    except httpx.TimeoutException as exc:
        _end_langfuse_trace(trace_ctx, error="Anthropic API timeout")
        raise HTTPException(status_code=504, detail="Anthropic API timeout") from exc
    except Exception as exc:
        _end_langfuse_trace(trace_ctx, error=str(exc))
        logger.error("Anthropic API error: %s", format_exception_message(exc), exc_info=True)
        raise HTTPException(
            status_code=502, detail="Anthropic API error. Please try again."
        ) from exc

    if resp.status_code == 200:
        output_text = None
        usage_dict = None
        resp_model = requested_model
        try:
            resp_json = resp.json()
            content_blocks = resp_json.get("content", [])
            text_parts = [
                b.get("text", "") for b in content_blocks if b.get("type") == "text"
            ]
            if text_parts:
                output_text = "".join(text_parts)
            u = resp_json.get("usage", {})
            if u:
                usage_dict = {
                    "input_tokens": u.get("input_tokens", 0),
                    "output_tokens": u.get("output_tokens", 0),
                    "cache_creation_input_tokens": u.get(
                        "cache_creation_input_tokens", 0
                    ),
                    "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                }
            resp_model = resp_json.get("model", requested_model)
        except (json.JSONDecodeError, AttributeError, KeyError):
            pass  # non-critical: trace proceeds with defaults if parsing fails
        _end_langfuse_trace(
            trace_ctx,
            output=output_text,
            model=resp_model,
            usage=usage_dict,
        )
    else:
        _end_langfuse_trace(trace_ctx, error=f"HTTP {resp.status_code}")

    return _anthropic_response(resp)


@router.post("/messages/count_tokens")
async def proxy_anthropic_count_tokens(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Anthropic token counting pass-through for Claude Code compatibility.

    Forwards requests to Anthropic's ``/v1/messages/count_tokens`` endpoint
    with ``anthropic-beta`` and ``anthropic-version`` headers preserved.
    """
    user = await _authenticate_api_key(request, db)
    require_model_access(user)
    body = await request.body()
    requested_model = None
    with contextlib.suppress(json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        requested_model = json.loads(body).get("model")
    anthropic_config = await _get_anthropic_config(
        db,
        requested_model if isinstance(requested_model, str) else None,
        user_id=str(user.id),
    )
    headers = _build_anthropic_headers(request, await reveal_field(anthropic_config, "api_key_enc") or "")

    try:
        async with httpx.AsyncClient(timeout=_ANTHROPIC_COUNT_TOKENS_TIMEOUT) as client:
            resp = await client.post(
                f"{ANTHROPIC_API_BASE}/v1/messages/count_tokens",
                headers=headers,
                content=body,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Anthropic API timeout") from exc
    except Exception as exc:
        logger.error("Anthropic API count_tokens error: %s", format_exception_message(exc), exc_info=True)
        raise HTTPException(
            status_code=502, detail="Anthropic API error. Please try again."
        ) from exc

    return _anthropic_response(resp)
