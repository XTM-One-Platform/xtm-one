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

"""Langfuse tracing service – observability for all LLM calls (SDK v4).

Provides helpers to create Langfuse traces for:
- Chat conversations (user <-> agent / general assistant)
- Assignment/agent executions
- LLM proxy requests
- Public chat (web-embedded agents)

Best practices implemented (per Langfuse docs):
- **User tracking with emails** – ``user_id`` on every trace is the user's
  email address so the Langfuse "Users" dashboard shows real people.
- **Session grouping** – chat conversations pass ``session_id`` (conversation
  UUID) so all messages in a thread are grouped in Langfuse.
- **Rich metadata** – agent_id, agent_name, assignment_id, run_id, trigger_type,
  model, source, user_id are all attached for filtering and cost breakdown.
- **Rich tags** – ``source:<type>``, ``agent:<id>``, ``model:<name>``,
  ``assignment:<id>`` enable powerful Langfuse dashboard filters.
- **Correct usage format** – provider-specific token dicts are normalized to
  Langfuse v4's ``usage_details`` schema so cost tracking works.
- **Trace-level output** – final output is set on both the generation span
  AND the parent trace for a complete view in the Langfuse UI.

Uses the Langfuse Python SDK v4 (OpenTelemetry-based):
- ``Langfuse.start_observation()`` to create root spans (traces)
- ``LangfuseSpan.start_observation(as_type="generation")`` for generations
- ``propagate_attributes()`` context manager to attach trace-level attributes
  (user_id, session_id, tags) – the v3 ``span.update_trace()`` helper was
  removed in v4 and now raises ``AttributeError``.
- ``LangfuseSpan.set_trace_io()`` to override trace-level input/output
  (deprecated alias retained in v4 for backwards compatibility).

If Langfuse is not enabled, all functions are no-ops (return None).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import TRUNCATE_LABEL
from app.core.redaction import redact_known_secrets
from app.core.utils import (
    coerce_tool_arguments_object,
    format_exception_message,
    strip_null_bytes,
)
from app.services.branding import get_cached_branding

if TYPE_CHECKING:
    from langfuse import Langfuse

logger = logging.getLogger(__name__)

_OTEL_BSP_MAX_EXPORT_BATCH_SIZE_ENV = "OTEL_BSP_MAX_EXPORT_BATCH_SIZE"
_OTEL_BSP_MAX_QUEUE_SIZE_ENV = "OTEL_BSP_MAX_QUEUE_SIZE"
# Traces-scoped on purpose: seeding the generic ``OTEL_EXPORTER_OTLP_COMPRESSION``
# knob would also flip the anonymous-telemetry ``OTLPMetricExporter``
# (``services/telemetry.py``) to gzip.  The traces-scoped variable takes
# precedence in the SDK's ``_compression_from_env()`` lookup and only
# affects the span exporter this seeding is about.
_OTEL_EXPORTER_OTLP_TRACES_COMPRESSION_ENV = "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"
_OTEL_EXPORTER_OTLP_COMPRESSION_ENV = "OTEL_EXPORTER_OTLP_COMPRESSION"
_DEFAULT_OTEL_BSP_MAX_EXPORT_BATCH_SIZE = "32"
_DEFAULT_OTEL_BSP_MAX_QUEUE_SIZE = "2048"
_DEFAULT_OTEL_EXPORTER_OTLP_TRACES_COMPRESSION = "gzip"


def _seed_otel_exporter_limits_from_env() -> None:
    """Seed safe OpenTelemetry batch defaults for Langfuse OTLP export.

    The Langfuse v4 SDK uses OpenTelemetry under the hood. In production,
    unusually large trace trees can exceed collector request-size limits and
    trigger HTTP 413 responses when the batch span processor flushes.

    Three knobs are tuned here:
    - ``OTEL_BSP_MAX_EXPORT_BATCH_SIZE``: reduced from the SDK default (512)
      to a conservative value so each HTTP export request stays well under the
      nginx ``client_max_body_size`` limit (typically 1 MB).
    - ``OTEL_BSP_MAX_QUEUE_SIZE``: bounded so the in-process queue never
      grows unbounded under back-pressure.
    - ``OTEL_EXPORTER_OTLP_TRACES_COMPRESSION``: gzip-compress span export
      requests.  LLM spans carry large prompt/completion payloads; compression
      typically achieves 5–10× reduction, making 413s far less likely even
      under burst conditions.  The traces-scoped variable is seeded (rather
      than the generic ``OTEL_EXPORTER_OTLP_COMPRESSION``) so the
      anonymous-telemetry metric exporter is unaffected — and only when the
      operator set NEITHER variable, because the traces-scoped one takes
      precedence in the SDK and seeding it would otherwise override an
      explicit generic setting.

    We set conservative defaults only when unset, so operators can still tune
    all three knobs explicitly in deployment manifests.

    Note: the OTLP export *timeout* is intentionally NOT seeded via
    ``OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`` (nor the generic
    ``OTEL_EXPORTER_OTLP_TIMEOUT``) — the Langfuse SDK always passes an
    explicit ``timeout`` kwarg to its ``OTLPSpanExporter``, which short-circuits
    the exporter's lookup of BOTH env vars entirely.  The export deadline is
    controlled by the ``timeout`` argument on the ``Langfuse`` constructor
    instead (see ``LANGFUSE_EXPORT_TIMEOUT`` / ``_blocking_init_and_auth``).
    """
    os.environ.setdefault(
        _OTEL_BSP_MAX_EXPORT_BATCH_SIZE_ENV,
        _DEFAULT_OTEL_BSP_MAX_EXPORT_BATCH_SIZE,
    )
    os.environ.setdefault(
        _OTEL_BSP_MAX_QUEUE_SIZE_ENV,
        _DEFAULT_OTEL_BSP_MAX_QUEUE_SIZE,
    )
    if (
        _OTEL_EXPORTER_OTLP_TRACES_COMPRESSION_ENV not in os.environ
        and _OTEL_EXPORTER_OTLP_COMPRESSION_ENV not in os.environ
    ):
        os.environ[_OTEL_EXPORTER_OTLP_TRACES_COMPRESSION_ENV] = (
            _DEFAULT_OTEL_EXPORTER_OTLP_TRACES_COMPRESSION
        )


# ---------------------------------------------------------------------------
# Current trace context (for parent/child linking across nested agent calls)
# ---------------------------------------------------------------------------
#
# When an orchestrator agent consults or transfers to a child agent, the child
# ``run_agent_chat`` runs in the same asyncio task.  We stash the orchestrator's
# trace context in a ``ContextVar`` so the child can open its root span as a
# child observation of the parent — without threading ``parent_trace_ctx``
# through every call site.  The explicit kwarg on ``trace_llm_call`` and
# ``run_agent_chat`` remains for tests and opt-in overrides.

_current_trace_ctx: ContextVar[dict | None] = ContextVar(
    "langfuse_current_trace_ctx", default=None
)


def get_current_trace_ctx() -> dict | None:
    """Return the trace context active in the current asyncio task, or None."""
    return _current_trace_ctx.get()


def set_current_trace_ctx(ctx: dict | None) -> Token:
    """Push a trace context onto the contextvar; returns a reset token."""
    return _current_trace_ctx.set(ctx)


def reset_current_trace_ctx(token: Token) -> None:
    """Restore the previous trace context using a token from ``set_current_trace_ctx``."""
    _current_trace_ctx.reset(token)


def current_local_trace_root() -> dict | None:
    """The root context of the local trace the current task records, or None.

    The nearest context carrying a ``local_trace_id`` walking up from the
    contextvar: the turn's own trace, never the trace of the run it was opened
    under. Its ``local_spans`` hold the spans ended so far.
    """
    return _root_local_context(get_current_trace_ctx())


@dataclass
class RootTraceLink:
    """Metadata for the next trace :func:`trace_llm_call` opens in a context, and that trace's id.

    Set with :func:`next_root_trace` around a turn that needs its trace
    linked to another one (a resumed turn to the turn it resumes): the
    first trace opened inside takes :attr:`metadata` and records its durable
    id in :attr:`trace_id`; the traces opened after it are left as they are.
    """

    metadata: dict[str, Any]
    trace_id: uuid.UUID | None = None
    taken: bool = False


_next_root_trace: ContextVar[RootTraceLink | None] = ContextVar("langfuse_next_root_trace", default=None)


@contextlib.contextmanager
def next_root_trace(metadata: dict[str, Any]) -> Iterator[RootTraceLink]:
    """Give *metadata* to the next trace opened in this context; yields the link holding its id."""
    link = RootTraceLink(metadata=dict(metadata))
    token = _next_root_trace.set(link)
    try:
        yield link
    finally:
        _next_root_trace.reset(token)


def arm_root_trace_link() -> RootTraceLink:
    """Record the id of the next trace opened in this context, adding no metadata; returns its link.

    For a request handler that runs a turn and must name the turn's durable
    trace when the turn is cut (a cut chat turn queued for its automatic
    resume): the trace's row may not be flushed when the resume reads it.
    Set at the start of each turn; the turn's first trace takes it once.
    """
    link = RootTraceLink(metadata={})
    _next_root_trace.set(link)
    return link


def _take_root_trace_link() -> RootTraceLink | None:
    """The :class:`RootTraceLink` the trace being opened takes, once; ``None`` when none waits."""
    link = _next_root_trace.get()
    if link is None or link.taken:
        return None
    link.taken = True
    return link


def get_trace_id(ctx: dict | None) -> str | None:
    """Return the Langfuse trace id from a ``trace_llm_call`` context, or None.

    Used to persist the trace id on durable records (e.g. ``AssignmentRun``)
    so the UI can deep-link to the trace after the request completed (OBS-5).
    """
    if not ctx:
        return None
    span = ctx.get("trace")
    if span is None:
        return None
    trace_id = getattr(span, "trace_id", None) or getattr(span, "id", None)
    return str(trace_id) if trace_id else None


def get_local_trace_id(ctx: dict | None) -> uuid.UUID | None:
    """Return the local trace UUID from a ``trace_llm_call`` context, or None."""
    if not ctx:
        return None
    raw = ctx.get("local_trace_id")
    if isinstance(raw, uuid.UUID):
        return raw
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return uuid.UUID(raw)
    return None


def usage_token_totals(usage: dict | None) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)`` totals from a provider usage dict.

    Reuses the provider-key normalization in ``_normalize_usage`` (Anthropic
    ``input_tokens`` + cache tokens, OpenAI/Gemini ``prompt_tokens`` /
    ``completion_tokens``) so accounting matches what Langfuse sees (OBS-6),
    including a run whose calls reported in several conventions.
    """
    if not usage:
        return (0, 0)
    normalized = _normalize_usage(_without_usage_details(usage))
    return (int(normalized.get("input", 0)), int(normalized.get("output", 0)))


def _without_usage_details(usage: dict) -> dict:
    """*usage* without its ``*_tokens_details`` breakdown maps.

    ``_normalize_usage`` moves a breakdown OUT of ``input`` / ``output`` (the
    Langfuse shape the pricing reads), so a token count read through it would
    leave out the cached prompt and reasoning tokens the call consumed. The
    parts of a run's usage (``USAGE_PARTS_KEY``) lose theirs too.
    """
    stripped = {
        key: value
        for key, value in usage.items()
        if not (isinstance(key, str) and key.endswith("_tokens_details"))
    }
    parts = usage_parts(usage)
    if parts is not None:
        stripped[USAGE_PARTS_KEY] = [
            {"model": model, "usage": _without_usage_details(part)} for model, part in parts
        ]
    return stripped
def usage_detail_buckets(usage: dict | None) -> dict[str, int]:
    """Return the ``usage_details`` shape a span persists for ``usage``.

    Same normalization as ``_normalize_usage``, so an in-memory timeline reads the
    keys ``LLMSpan.usage_details`` holds once flushed (OBS-6). Returns ``{}`` for
    an absent or empty usage dict.
    """
    if not usage:
        return {}
    return _normalize_usage(usage)


def capture_trace_context(ctx: dict | None = None) -> dict | None:
    """Capture the active trace context in a JSON-serializable form.

    Returns ``{"trace_id": ..., "parent_span_id": ..., "local_trace_id": ...}``
    suitable for persisting in an orchestration execution payload and
    rebuilding on the worker — the live ``LangfuseSpan`` object in
    ``ctx["trace"]`` cannot cross a process boundary, but the Langfuse v4 SDK
    accepts a ``trace_context`` dict on ``start_observation`` to attach a new
    observation to an existing trace.  ``trace_id`` / ``parent_span_id`` are
    the Langfuse ids (absent when Langfuse is off, ``parent_span_id`` also
    when the root span carries no id); ``local_trace_id`` is the durable
    ``llm_traces`` id of the trace the context belongs to, which ``end_trace``
    records as the new trace's ``parent_trace_id``.

    Resolution order: explicit ``ctx`` argument → contextvar
    (``get_current_trace_ctx()``).  An explicit (non-``None``) ``ctx`` always
    wins — even an empty/cleared mapping, which means "no parent" and returns
    ``None`` rather than silently falling back to the contextvar and linking
    the wrong trace.  Already-serialized contexts (a dict carrying these keys
    but no live span) pass through unchanged.  Returns ``None`` when no trace
    is active — safe to call unconditionally.
    """
    effective = ctx if ctx is not None else get_current_trace_ctx()
    if not effective:
        return None
    out: dict = {}
    span = effective.get("trace")
    if span is None:
        # Already-serialized remote context (or a local-only one) — pass the
        # Langfuse ids through when it carries them.
        trace_id = effective.get("trace_id")
        if trace_id:
            out["trace_id"] = str(trace_id)
            if effective.get("parent_span_id"):
                out["parent_span_id"] = str(effective["parent_span_id"])
    else:
        trace_id = getattr(span, "trace_id", None)
        if trace_id:
            out["trace_id"] = str(trace_id)
            span_id = getattr(span, "id", None) or getattr(span, "span_id", None)
            if span_id:
                out["parent_span_id"] = str(span_id)
    local_trace_id = _parent_local_trace_id(effective)
    if local_trace_id is not None:
        out["local_trace_id"] = str(local_trace_id)
    return out or None


# ---------------------------------------------------------------------------
# Current retry observer (for nesting `retry_llm_call` attempts under the
# active iteration generation without changing provider signatures)
# ---------------------------------------------------------------------------
#
# ``retry_llm_call`` lives in ``app.llm.base`` and must not import Langfuse
# directly (early import + circular dependency).  Instead, callers that want
# retry visibility push an observer onto this contextvar around their
# ``llm.chat()`` call; ``retry_llm_call`` lazily resolves it via
# ``get_current_retry_observer`` when its explicit ``on_attempt`` kwarg is
# not provided.

_current_retry_observer: ContextVar[Any] = ContextVar(
    "langfuse_current_retry_observer", default=None
)


def get_current_retry_observer() -> Any:
    """Return the retry observer active in the current asyncio task, or None."""
    return _current_retry_observer.get()


def set_current_retry_observer(observer: Any) -> Token:
    """Push a retry observer onto the contextvar; returns a reset token."""
    return _current_retry_observer.set(observer)


def reset_current_retry_observer(token: Token) -> None:
    """Restore the previous retry observer using a token from ``set_current_retry_observer``."""
    _current_retry_observer.reset(token)


def make_retry_attempt_observer(parent_gen_ctx: dict | None) -> Any:
    """Build a retry observer that emits ``attempt:retry:N`` events.

    The returned callable is intended to be passed as ``on_attempt=`` to
    ``retry_llm_call`` (or registered via ``set_current_retry_observer``).
    Each invocation fires a single Langfuse event (v4 ``create_event`` —
    no start/end pair) under ``parent_gen_ctx["trace"]``, surfacing the
    triggering exception in ``status_message``.  Rendering retries as
    events keeps transient 429/529/5xx storms visible in the trace UI
    without inflating the trace tree with N child observations per
    generation, and without affecting the cost-tracked generation count.

    Returns ``None`` when ``parent_gen_ctx`` is missing or carries no span
    — safe for callers to register unconditionally.
    """
    if not parent_gen_ctx:
        return None
    parent_span = parent_gen_ctx.get("trace")
    if parent_span is None:
        return None

    def _observer(attempt: int, exc: Exception, delay: float) -> None:
        try:
            # Exception text can embed HTTP response bodies (tool results),
            # so strip null bytes before the string reaches the Langfuse API
            # as a status message.
            error_str = _sanitize_for_storage(f"{type(exc).__name__}: {exc}")
            parent_span.create_event(
                name=f"attempt:retry:{attempt + 1}",
                level="WARNING",
                status_message=error_str[:TRUNCATE_LABEL],
                metadata={
                    "observation_kind": "retry_attempt",
                    "attempt": attempt + 1,
                    "delay_s": round(delay, 3),
                    "error_type": type(exc).__name__,
                },
            )
        except Exception:
            logger.debug("retry attempt observer failed", exc_info=True)

    return _observer


# ---------------------------------------------------------------------------
# Trace input enrichment (Gap 5)
# ---------------------------------------------------------------------------

# Per-string cap applied to the **external Langfuse export only**, whose
# ingestion rejects oversized events (see ``_LangfuseBadRequestFilter``).  The
# durable store deliberately has no equivalent cap: a trace payload is audit
# material and truncating it there destroys the only copy.  Oversized durable
# payloads are offloaded to object storage instead
# (``services/tracing/payload_offload.py``), and API responses are bounded at
# the read boundary with the JSON structure preserved.
_TRACE_MESSAGE_CONTENT_MAX = 20_000


def _truncate(
    text: str | None,
    limit: int | None = None,
    *,
    max_len: int | None = None,
) -> str | None:
    if text is None:
        return None
    effective_limit = max_len if max_len is not None else limit
    if effective_limit is None:
        return text
    if len(text) <= effective_limit:
        return text
    return text[:effective_limit] + f"… [truncated {len(text) - effective_limit} chars]"


def _to_jsonable(value: Any) -> Any:
    """Normalize ``value`` into JSON-native types for durable storage.

    Trace payloads reach the sinks carrying whatever the caller had in hand —
    ``datetime``, ``UUID``, dataclasses, provider SDK objects — none of which
    the JSONB / ClickHouse-String writers accept.  A ``json.dumps(default=str)``
    round-trip coerces them, exactly as the previous ``_truncate_json`` did.

    What it deliberately no longer does is truncate.  The durable store is the
    only copy of a trace payload, so bounding it there destroyed audit
    material — and because the old implementation truncated the *serialized*
    form, an oversized payload came back as an unparseable string and lost its
    structure too (that is how ``output.reasoning`` disappeared from the span
    inspector).  Size is now handled where it belongs: oversized payloads are
    offloaded to object storage by the dispatcher (see
    ``services/tracing/payload_offload.py``), and API responses are bounded at
    the read boundary with structure intact.

    It also no longer strips media.  Replacing an image with ``"[image]"`` here
    made the blob unrecoverable from the trace, because this is the only copy;
    media blobs are moved to object storage by the dispatcher instead (see
    ``services/tracing/media_offload.py``).  The stripping survives on the
    export path only — :func:`_bound_for_langfuse`.
    """
    safe_value = _sanitize_for_storage(value)
    if safe_value is None:
        return None
    if isinstance(safe_value, str):
        return safe_value
    try:
        return json.loads(json.dumps(safe_value, default=str, ensure_ascii=False))
    except Exception:
        return repr(safe_value)


def _bound_for_langfuse(value: Any) -> Any:
    """Bound a payload for the *external* Langfuse SDK export only.

    Langfuse ingestion has its own request-size limits and rejects oversized
    events outright (the reason ``_LangfuseBadRequestFilter`` exists), and that
    sink is not ours to uncap.  The per-field caps therefore survive here and
    only here — the durable store keeps the payload whole.

    Media stripping lives here for the same reason.  The Langfuse SDK v3+
    detects ``image_url`` content parts and uploads them to its own configured
    media storage; when that storage is unreachable the retries flood the logs
    and hold every base64 blob in memory.  So the export gets placeholders while
    the durable payload keeps a reference to the real bytes in object storage.
    """
    if value is None:
        return None
    stripped = _strip_media_content(value)
    if isinstance(stripped, str):
        return _truncate(stripped, _TRACE_MESSAGE_CONTENT_MAX)
    return _truncate_deep(stripped, _TRACE_MESSAGE_CONTENT_MAX)


def _coerce_tool_arguments_object(value: Any) -> dict[str, Any] | None:
    """Normalize tool arguments into a bounded JSON object for durable storage.

    Tool arguments are expected to be object-shaped. Legacy call paths can still
    provide strings (already-serialized JSON or opaque text), so keep those
    readable under a stable ``{"raw": ...}`` envelope instead of writing a bare
    string to JSONB and breaking strict response schemas. The shape contract
    lives in :func:`app.core.utils.coerce_tool_arguments_object` (shared with
    the traces API and the ClickHouse read store); this wrapper adds the sink
    concerns: null-byte sanitization and JSON normalization.  Arguments are
    stored whole — a truncated tool call is not auditable, and oversized
    payloads are offloaded rather than clipped, as are the media blobs a tool
    result can carry (a screenshot returned by a browser tool, for instance).
    """
    coerced = coerce_tool_arguments_object(_sanitize_for_storage(value))
    if coerced is None:
        return None
    return _to_jsonable(coerced)


def _truncate_deep(data: Any, limit: int) -> Any:
    """Recursively truncate all strings in a nested structure.

    Ensures dict/list outputs (e.g. generation output with embedded
    tool_calls and their arguments) stay bounded without requiring
    each call site to manually truncate nested fields.
    """
    if data is None:
        return None
    if isinstance(data, str):
        return _truncate(data, limit)
    if isinstance(data, list):
        return [_truncate_deep(item, limit) for item in data]
    if isinstance(data, dict):
        return {k: _truncate_deep(v, limit) for k, v in data.items()}
    return data


def _serialize_trace_tools(tools: list) -> list[dict]:
    """Convert tool definitions to the compact ``{name, description}`` records
    recorded in trace input.

    Handles the three input shapes that reach the tracer:

    * ``ToolDefinition`` instances (internal agent loop) — ``name`` /
      ``description`` are attributes.
    * Top-level-keyed dicts ``{"name": ..., "description": ...}`` used by some
      internal helpers.
    * OpenAI-Chat-Completions-shaped dicts
      ``{"type": "function", "function": {"name": ..., "description": ...}}`` —
      what arrives via ``/v1/chat/completions`` (``llm_proxy``) and any provider
      mirroring the OpenAI tool schema. Without the nested-function lookup the
      catalogue would silently drop from llm-proxy traces.
    """
    tool_list: list[dict] = []
    for t in tools:
        nested: dict = (
            t.get("function", {})
            if isinstance(t, dict) and isinstance(t.get("function"), dict)
            else {}
        )
        name = (
            getattr(t, "name", None)
            or (t.get("name") if isinstance(t, dict) else None)
            or nested.get("name")
        )
        description = (
            getattr(t, "description", None)
            or (t.get("description") if isinstance(t, dict) else None)
            or nested.get("description")
        )
        if not name:
            continue
        tool_list.append(
            {
                "name": name,
                "description": description or "",
            }
        )
    return tool_list


def serialize_file_refs(files: list | None, limit: int | None = None) -> list[dict]:
    """Serialize ``FileRef``-like objects into trace-safe metadata dicts.

    Keeps only the identifying metadata (id, name, MIME type, size) — never the
    bytes — so the Trace Explorer can link the exact documents a run consumed
    or produced.  Every ref is recorded by default: silently dropping the tail
    of the list would make the trace misreport which files a run touched.
    ``limit`` remains available for callers that deliberately want a sample.
    Objects missing the expected attributes are skipped rather than failing the
    trace write.
    """
    out: list[dict] = []
    candidates = files or []
    for f in (candidates[:limit] if limit is not None else candidates):
        try:
            file_id = str(getattr(f, "file_id", "") or "")
            filename = str(getattr(f, "filename", "") or "")
            if not file_id or not filename:
                continue
            out.append(
                {
                    "file_id": file_id,
                    "filename": _truncate(filename, max_len=500),
                    "content_type": _truncate(
                        str(getattr(f, "content_type", "") or ""), max_len=255
                    ),
                    "size": int(getattr(f, "size", 0) or 0),
                }
            )
        except Exception:
            logger.debug("serialize_file_refs: skipping malformed ref", exc_info=True)
            continue
    return out


def build_trace_input(
    messages: list | None = None,
    *,
    system_prompt: str | None = None,
    tools: list | None = None,
    extra: dict | None = None,
) -> dict:
    """Build the structured ``input_data`` payload for ``trace_llm_call``.

    Includes the full conversation (messages), the assembled system prompt,
    and the tool catalogue (name + description only).  Nothing is truncated:
    the durable store is the only copy of a trace payload, so size is handled
    by offloading oversized payloads to object storage rather than by clipping
    fields (see ``services/tracing/payload_offload.py``).  Image / base64
    content is still replaced with placeholders and null bytes removed
    downstream by ``_sanitize_for_langfuse``.

    Args:
        messages: List of ``ChatMessage`` objects (or already-serialised dicts).
        system_prompt: The fully assembled system prompt string.
        tools: List of ``ToolDefinition`` objects (or dicts).  Only ``name``
            and ``description`` are recorded (not the JSON schema).
        extra: Optional dict merged into the result for caller-specific fields
            (e.g. ``{"user_message": ..., "agent": ...}``).
    """
    payload: dict = {}

    if system_prompt:
        payload["system_prompt"] = system_prompt

    if messages:
        # Lazy import to avoid an import cycle (builtin_tools imports widely
        # from services).  ``serialize_messages`` accepts ChatMessage objects;
        # already-serialised dicts pass through unchanged below.
        try:
            from app.services.builtin_tools import serialize_messages

            first = messages[0] if messages else None
            raw = (
                list(messages)
                if first is not None and isinstance(first, dict)
                else serialize_messages(messages)
            )
        except ImportError:
            logger.debug("build_trace_input: serialize_messages unavailable", exc_info=True)
            raw = []

        payload["messages"] = [m for m in raw if isinstance(m, dict)]

    if tools:
        payload["tools"] = _serialize_trace_tools(tools)

    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)

    return payload


async def cache_chat_trace_ids(
    session_id: str | None,
    trace_id: str | None,
    generation_id: str | None,
    local_trace_id: str | None = None,
) -> None:
    """Cache the latest Langfuse trace + observation IDs for a chat turn.

    The ``generation_id`` parameter actually accepts any Langfuse
    observation id (root span, generation, event) — the historical name
    is preserved for wire-format backward compatibility with
    :func:`load_latest_langfuse_ids` and the
    ``langfuse_generation_id`` field embedded in message metadata.

    Feedback scoring (``_push_langfuse_feedback_score``) prefers the
    cached observation id over the trace id (``observation_id`` is more
    granular than ``trace_id`` for Langfuse scores), so passing the root
    span id rather than ``None`` keeps user feedback attached to a
    specific observation under the trace card instead of degrading to a
    trace-wide score.
    """
    if not session_id or not trace_id:
        return
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        payload = {"trace_id": trace_id}
        if generation_id:
            payload["generation_id"] = generation_id
        if local_trace_id:
            payload["llm_trace_id"] = local_trace_id
        await redis.set(
            f"langfuse:chat:{session_id}:latest",
            json.dumps(payload),
            ex=60 * 60 * 24 * 7,
        )
    except Exception:
        logger.debug(
            "Unable to cache Langfuse IDs for session %s",
            session_id,
            exc_info=True,
        )


async def load_latest_langfuse_ids(conversation_id: str) -> dict[str, str]:
    """Load the latest Langfuse trace identifiers for a conversation.

    Reads what :func:`cache_chat_trace_ids` wrote, as the message metadata
    keys ``langfuse_trace_id`` / ``langfuse_generation_id`` / ``llm_trace_id``.
    """
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        raw = await redis.get(f"langfuse:chat:{conversation_id}:latest")
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}
        trace_id = parsed.get("trace_id")
        generation_id = parsed.get("generation_id")
        out: dict[str, str] = {}
        if isinstance(trace_id, str) and trace_id.strip():
            out["langfuse_trace_id"] = trace_id.strip()
        if isinstance(generation_id, str) and generation_id.strip():
            out["langfuse_generation_id"] = generation_id.strip()
        llm_trace_id = parsed.get("llm_trace_id")
        if isinstance(llm_trace_id, str) and llm_trace_id.strip():
            out["llm_trace_id"] = llm_trace_id.strip()
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Media sanitization – strip images / base64 from Langfuse input data
# ---------------------------------------------------------------------------

_BASE64_PREFIX = "data:"
_BASE64_PLACEHOLDER = "[base64 image]"
_IMAGE_URL_PLACEHOLDER = "[image]"


def _strip_media_content(data: Any) -> Any:
    """Recursively strip image / base64 data from Langfuse input payloads.

    The Langfuse SDK v3 automatically detects ``image_url`` content parts
    and attempts to upload them to its configured media storage (S3/MinIO).
    When that storage is unreachable the upload retries flood logs, waste
    memory (holding large base64 payloads), and can degrade the async
    event loop.

    This helper replaces:
    - ``{"type": "image_url", ...}`` → ``{"type": "text", "text": "[image]"}``
    - Inline ``data:...;base64,...`` strings → ``"[base64 image]"``

    It processes the common shapes passed as ``input_data``:
    - A list of OpenAI-format message dicts (``[{"role": ..., "content": ...}]``)
    - A single dict with a ``content`` key
    - Arbitrary nested structures
    """
    if data is None:
        return None

    if isinstance(data, str):
        if data.startswith(_BASE64_PREFIX) and ";base64," in data[:64]:
            return _BASE64_PLACEHOLDER
        return data

    if isinstance(data, list):
        return [_strip_media_content(item) for item in data]

    if isinstance(data, dict):
        # OpenAI multimodal content part: {"type": "image_url", "image_url": {...}}
        if data.get("type") == "image_url":
            return {"type": "text", "text": _IMAGE_URL_PLACEHOLDER}

        out: dict = {}
        # Anthropic content block: {"type": "base64", "media_type": ...,
        # "data": "<base64>"}.  The blob has no ``data:`` prefix, so it is only
        # identifiable from its position beside ``type``/``media_type``.
        is_base64_source = str(data.get("type") or "").lower() == "base64"

        for key, val in data.items():
            if key == "content":
                out[key] = _strip_media_content(val)
            elif key == "image_url":
                out[key] = _IMAGE_URL_PLACEHOLDER
            elif key == "data" and is_base64_source and isinstance(val, str):
                out[key] = _BASE64_PLACEHOLDER
            else:
                # Recurse into strings too, so an inline ``data:`` URI is caught
                # wherever it sits rather than only under ``content`` /
                # ``image_url``.  This path is now the only media stripper —
                # the durable store keeps the real bytes — so anything it
                # misses reaches the Langfuse SDK and triggers a media upload.
                out[key] = _strip_media_content(val)
        return out

    return data


# The Langfuse API (like PostgreSQL) rejects JSON payloads that contain null
# bytes (``\u0000``) with a 400 Bad Request response.  Tool results from
# GitHub GraphQL or other sources occasionally embed raw null bytes, which
# would otherwise reach Langfuse unmodified and trigger opaque SDK-level
# "Bad request" errors.  The recursive strip helper is
# shared with the local PG sink (``services/tracing/local_sink.py``) via
# ``core/utils.py`` — single source of truth for null-byte sanitization.
_strip_null_bytes = strip_null_bytes


def _sanitize_for_storage(data: Any) -> Any:
    """Make ``data`` safe for the durable store without losing anything.

    Null bytes are removed because Postgres rejects ``\\u0000`` in JSONB
    — that is a hard constraint of the column, not a size or fidelity choice.

    Media is deliberately *not* stripped: the durable store holds the only copy
    of a trace payload, so replacing an image with a placeholder here destroyed
    it.  Blobs are relocated (to object storage, behind a reference) rather than
    removed, by the dispatcher — see ``services/tracing/media_offload.py``.

    A secret variable value the run or turn resolved is replaced by its marker
    (``core/redaction.py``): here, in the task that captures the
    payload, since the store is written later by a task outside the run.
    """
    return _strip_null_bytes(redact_known_secrets(data))


def _sanitize_for_langfuse(data: Any) -> Any:
    """Apply the full Langfuse payload sanitization pipeline to ``data``.

    Combines, in order:
    1. ``_strip_media_content`` – replace base64 / image_url blobs with
       placeholders to avoid SDK media-upload side-effects.
    2. ``_strip_null_bytes`` – remove ``\\u0000`` characters from all strings
       so the Langfuse API never receives a payload it would reject with 400.

    Use this function in place of bare ``_strip_media_content`` calls for any
    data that is about to be set on a Langfuse span or trace.  For data headed
    for the durable store use :func:`_sanitize_for_storage`, which keeps media.
    A secret variable value the run or turn resolved goes first.
    """
    return _strip_null_bytes(_strip_media_content(redact_known_secrets(data)))


# ---------------------------------------------------------------------------
# Diagnostic logging filter for the Langfuse SDK logger
# ---------------------------------------------------------------------------


class _LangfuseBadRequestFilter(logging.Filter):
    """Enrich Langfuse SDK 400 "Bad request" error records with a diagnostic hint.

    The Langfuse SDK emits a generic error message when its API returns a
    400 response — no endpoint, no payload excerpt, no field-level detail.
    This filter intercepts those records and appends a one-line hint that
    points on-call engineers toward the most common root cause observed in
    production (null bytes in span payloads from GitHub GraphQL or similar).

    The filter is installed at module load time on the ``"langfuse"`` SDK
    logger so it is active for all Langfuse SDK log records regardless of
    whether a client has been initialized.  It never suppresses records
    (always returns ``True``).
    """

    _BAD_REQUEST_MARKER = "Bad request"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        # Format once: reused for both the marker check and the enrichment,
        # and pre-formatting lets us safely append the hint without risk of
        # double-%s expansion when the record is later rendered by the root
        # handler (``record.args`` is cleared below).
        message = record.getMessage()
        if self._BAD_REQUEST_MARKER not in message:
            return True
        record.msg = message + (
            "  [Hint: null bytes (\\u0000) in span payload are the most common"
            " cause — tool results from GitHub GraphQL or similar. "
            "Payload is sanitized via _sanitize_for_langfuse; if this recurs"
            " check for new call-sites that bypass sanitization.]"
        )
        record.args = ()
        return True


def _install_langfuse_bad_request_filter() -> None:
    """Install the diagnostic filter on the Langfuse SDK logger (idempotent)."""
    sdk_logger = logging.getLogger("langfuse")
    if not any(isinstance(f, _LangfuseBadRequestFilter) for f in sdk_logger.filters):
        sdk_logger.addFilter(_LangfuseBadRequestFilter())


# Install immediately at module load time so the filter is in place before
# the first Langfuse SDK import (which can happen lazily inside a request).
_install_langfuse_bad_request_filter()


# Cache the Langfuse client to avoid re-creating on every call.
#
# Three states for ``_langfuse_enabled``:
#   None  – not yet checked (will attempt initialization)
#   True  – client is alive
#   False – permanently disabled (config says disabled, missing keys, import error)
#
# Transient failures (auth check, network) use ``_langfuse_retry_after`` instead
# of permanently disabling — the client will retry after a cooldown period.
_langfuse_client: Langfuse | None = None
_langfuse_enabled: bool | None = None
_langfuse_retry_after: float = 0  # monotonic timestamp; 0 = no retry pending
_LANGFUSE_RETRY_COOLDOWN = 300  # retry transient failures after 5 minutes

__all__ = ["_langfuse_client", "_langfuse_enabled", "_langfuse_retry_after"]

# Cache user group lookups to avoid repeated DB hits within the same process.
# Capped to prevent unbounded memory growth in long-running processes, and
# TTL-bounded so an admin's group-membership change is reflected in trace
# attribution within a few minutes instead of only after a process restart
# (group IDs feed trace visibility filtering, so stale entries are an
# access-control-adjacent concern).
_USER_GROUPS_CACHE_MAX = 1000
_USER_GROUPS_CACHE_TTL_SECONDS = 300.0
# Maps user_id -> (group_ids, cached_at monotonic timestamp).
_user_groups_cache: dict[str, tuple[list[str], float]] = {}

# Hard timeout (seconds) for the off-loop Langfuse client init + auth check, so
# a slow/unreachable host can never hold the init lock or a worker thread long.
_LANGFUSE_AUTH_TIMEOUT = 5

# Timeout (seconds) the SDK wires into its OTLP span exporter (and the shared
# API httpx client).  The SDK default is 5 s, which production showed is
# shorter than an occasional TLS handshake to the Langfuse endpoint — the
# BatchSpanProcessor then drops the whole span batch.  30 s is
# tolerant of transient network latency while still bounding each export.
# NOTE: this MUST be passed as the ``timeout`` kwarg on the ``Langfuse``
# constructor.  Seeding ``OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`` (or the generic
# ``OTEL_EXPORTER_OTLP_TIMEOUT``) does NOT work: the SDK constructs
# ``OTLPSpanExporter(timeout=<this value>)`` explicitly, and the exporter
# only consults those env vars when no kwarg is given.
# Public (no underscore) because it is part of the module's contract:
# ``api/v1/settings.py`` must construct its throwaway test-connection client
# with the same value — the SDK's ``LangfuseResourceManager`` is a
# per-public-key singleton that ``shutdown()`` does not evict, so whichever
# ``Langfuse(...)`` construction runs first pins the exporter timeout for the
# whole process.
LANGFUSE_EXPORT_TIMEOUT = 30

# Single-flight lock: only one coroutine performs the (off-loop) client init at
# a time. Built lazily on the running loop because an ``asyncio.Lock`` binds to
# the loop it is first used on (the web app and the SAQ worker are separate
# processes, each with its own loop).
_langfuse_init_lock: asyncio.Lock | None = None

# The SDK calls that wait on the Langfuse host (client construction and auth
# check, flush) run on their own small pool, never the loop's default executor:
# that one also serves the DNS lookups of every new database and HTTP
# connection, S3 and bcrypt, so a Langfuse host that never answers must not
# hold any of its threads. Here it can only delay other Langfuse work.
_LANGFUSE_EXECUTOR_WORKERS = 2
_langfuse_executor: ThreadPoolExecutor | None = None
_langfuse_executor_lock = threading.Lock()


def _get_langfuse_executor() -> ThreadPoolExecutor:
    global _langfuse_executor
    with _langfuse_executor_lock:
        if _langfuse_executor is None:
            _langfuse_executor = ThreadPoolExecutor(
                max_workers=_LANGFUSE_EXECUTOR_WORKERS,
                thread_name_prefix="xtmone-langfuse",
            )
        return _langfuse_executor


async def run_langfuse_blocking[T](fn: Callable[..., T], /, *args: Any) -> T:
    """Run a blocking Langfuse SDK call off the event loop, on the Langfuse pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_langfuse_executor(), fn, *args)


class _LangfuseAuthError(Exception):
    """Raised by the blocking init helper when the auth check fails."""


def _get_init_lock() -> asyncio.Lock:
    """Lazily build the single-flight init lock on the running event loop."""
    global _langfuse_init_lock
    if _langfuse_init_lock is None:
        _langfuse_init_lock = asyncio.Lock()
    return _langfuse_init_lock


def _blocking_init_and_auth(public_key: str, secret_key: str, host: str) -> Langfuse:
    """Construct the Langfuse client and run the project auth check. BLOCKING.

    MUST be invoked via :func:`run_langfuse_blocking`: the Langfuse v4 SDK is
    synchronous, so running the client construction and ``api.projects.get()``
    directly on the event loop would freeze the whole worker whenever the
    Langfuse host is slow/unreachable. Raises on auth failure; the async
    caller maps that to the retry cooldown.

    A failed check never shuts the client down. The SDK keeps one resource
    manager per public key for the whole process (its consumer threads, its
    span processor on the global tracer provider), and every later
    ``Langfuse(...)`` with that key gets the same one back. Shutting it down
    cannot be undone: the next attempt would get a manager with no consumers,
    and from the third shutdown its ``flush()`` waits forever on a queue
    nothing drains, holding a thread for good at every retry. The next attempt
    reuses the running manager instead, and the SDK shuts it down once, at
    process exit.

    Two distinct timeouts are in play:

    - ``timeout=LANGFUSE_EXPORT_TIMEOUT`` on the constructor is what the SDK
      wires into its OTLP span exporter (``OTLPSpanExporter(timeout=...)``)
      for the lifetime of the process.  It must be tolerant (30 s) so a slow
      TLS handshake to the Langfuse endpoint can't make the batch span
      processor drop whole span batches.
    - the auth check below stays short via per-request options
      (``timeout_in_seconds=_LANGFUSE_AUTH_TIMEOUT``) so a dead host can't
      hold the init lock or a worker thread for the full export timeout —
      the ``asyncio.wait_for`` budget in ``get_langfuse_client`` is sized to
      the auth timeout, not the export timeout.
    """
    from langfuse import Langfuse

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        timeout=LANGFUSE_EXPORT_TIMEOUT,
    )
    projects = client.api.projects.get(
        request_options={"timeout_in_seconds": _LANGFUSE_AUTH_TIMEOUT}
    )
    if not projects.data:
        raise _LangfuseAuthError("no project found for the configured keys")
    return client


async def get_langfuse_client(db: AsyncSession) -> Langfuse | None:
    """Get or create a Langfuse client from DB configuration.

    Returns ``None`` if Langfuse is not enabled or not configured.
    Transient failures (auth check, network) are retried after a cooldown
    period rather than permanently disabling tracing.
    """
    import time as _time

    global _langfuse_client, _langfuse_enabled, _langfuse_retry_after

    # Fast paths — no lock, no blocking work.
    if _langfuse_enabled is False:  # permanently disabled
        return None
    if _langfuse_client is not None:  # already alive
        return _langfuse_client
    if _langfuse_retry_after and _time.monotonic() < _langfuse_retry_after:
        return None  # transient-failure cooldown — don't hammer the endpoint

    # Serialize initialization so concurrent first-callers don't each build a
    # client (and each spawn a blocking auth check). The winner does the
    # one-time init off the event loop; everyone else re-checks the fast paths
    # below and returns immediately (the winner has either set the client or the
    # cooldown by the time they acquire the lock).
    async with _get_init_lock():
        if _langfuse_enabled is False:
            return None
        if _langfuse_client is not None:
            return _langfuse_client
        if _langfuse_retry_after and _time.monotonic() < _langfuse_retry_after:
            return None

        from app.models.langfuse_config import LangfuseConfig

        result = await db.execute(select(LangfuseConfig))
        config = result.scalar_one_or_none()

        if not config or not config.enabled:
            _langfuse_enabled = False
            logger.info(
                "Langfuse tracing disabled: %s",
                "no config row in DB" if not config else "enabled=False in config",
            )
            return None

        if not config.public_key_enc or not config.secret_key_enc:
            _langfuse_enabled = False
            logger.warning(
                "Langfuse tracing disabled: missing %s in config",
                "public_key" if not config.public_key_enc else "secret_key",
            )
            return None

        _seed_otel_exporter_limits_from_env()

        try:
            from langfuse import Langfuse  # noqa: F401  (presence check)
        except Exception as imp_err:
            logger.warning(
                "langfuse import failed – tracing permanently disabled: %s",
                format_exception_message(imp_err),
                exc_info=True,
            )
            _langfuse_enabled = False
            return None

        # Validate the SDK v4 API is available. ``propagate_attributes`` was
        # introduced in v4 (replacing the removed ``span.update_trace()``);
        # checking here gives one clear error rather than an opaque failure
        # inside every LLM call.
        try:
            from langfuse import propagate_attributes as _  # noqa: F401
        except ImportError:
            logger.warning(
                "Langfuse SDK v4+ required: propagate_attributes not found – "
                "tracing permanently disabled. Upgrade: pip install 'langfuse>=4'",
            )
            _langfuse_enabled = False
            return None

        from app.services.vault import reveal_field

        host = config.host or "https://cloud.langfuse.com"
        # Pre-bound so the failure log below is safe even if decryption itself
        # raises before this is assigned.
        public_key = ""

        # Decrypt + construct + auth-check off the event loop, hard-bounded by a
        # timeout. ``wait_for`` hands control back after the timeout even if the
        # worker thread is still blocked on a dead host (the thread can't be
        # cancelled, but the SDK ``timeout`` keeps it short, and it is a thread
        # of the Langfuse pool); the single-flight lock + cooldown prevent any
        # pile-up. Reading the keys is inside the
        # guard too: a decryption failure (rotated SECRET_KEY / corrupted token)
        # or a vault that cannot hand a key over must arm the cooldown and
        # return None, never 500 the chat/assignment request that triggered the
        # trace.
        try:
            # Either key may be a vault reference, read here like any other secret.
            public_key = await reveal_field(config, "public_key_enc") or ""
            secret_key = await reveal_field(config, "secret_key_enc") or ""
            client = await asyncio.wait_for(
                run_langfuse_blocking(
                    _blocking_init_and_auth, public_key, secret_key, host
                ),
                timeout=_LANGFUSE_AUTH_TIMEOUT + 2,
            )
        except Exception as auth_exc:
            is_timeout = isinstance(auth_exc, TimeoutError)
            reason = "timed out" if is_timeout else str(auth_exc)
            logger.error(
                "Langfuse client init/auth FAILED (host=%s, public_key=%s…) – %s. "
                "Will retry in %ds.",
                host,
                public_key[:8] if public_key else "?",
                reason,
                _LANGFUSE_RETRY_COOLDOWN,
                exc_info=None if is_timeout else auth_exc,
            )
            _langfuse_retry_after = _time.monotonic() + _LANGFUSE_RETRY_COOLDOWN
            return None

        _langfuse_client = client
        _langfuse_enabled = True
        _langfuse_retry_after = 0
        logger.info(
            "Langfuse client initialized and auth check passed (host=%s, public_key=%s…)",
            host,
            public_key[:8] if public_key else "?",
        )
        return _langfuse_client


def flush_langfuse() -> None:
    """Flush buffered Langfuse events without resetting the cached client.

    The Langfuse v4 SDK batches spans through an OpenTelemetry batch span
    processor; anything still queued in the background exporter is lost if
    the process exits before a flush.  Call this from the app lifespan
    shutdown and the SAQ worker shutdown hook so OTEL-batched spans survive
    SIGTERM / deploys.

    Safe to call unconditionally: a no-op when Langfuse is disabled or was
    never initialized, and never raises (shutdown paths must not fail on
    observability cleanup).
    """
    client = _langfuse_client
    if client is None:
        return
    try:
        client.flush()
        logger.info("Langfuse events flushed")
    except Exception as exc:
        logger.warning(
            "Langfuse flush failed (non-critical): %s", format_exception_message(exc), exc_info=True
        )


def reset_langfuse_client() -> None:
    """Reset the cached client – call after config changes.

    Flushes the old client without shutting it down: its resource manager is
    the SDK's process-wide one for that public key (see
    :func:`_blocking_init_and_auth`).
    """
    global _langfuse_client, _langfuse_enabled, _langfuse_retry_after
    logger.info("Langfuse client cache reset requested")
    if _langfuse_client:
        flush_langfuse()
    _langfuse_client = None
    _langfuse_enabled = None
    _langfuse_retry_after = 0
    _user_groups_cache.clear()
    logger.info("Langfuse client cache cleared – next call will re-initialize from DB")


async def areset_langfuse_client() -> None:
    """Async-safe reset for request handlers (config save / test connection).

    Detaches the cached client and clears the globals **under the single-flight
    init lock**, then flushes the *detached* client on the Langfuse pool
    (outside the lock), without shutting it down (see
    :func:`reset_langfuse_client`). Two reasons for this shape:

    1. **Serialize against an in-flight init.** ``get_langfuse_client`` performs
       its (slow, offloaded) init while holding ``_get_init_lock()``. Clearing
       the globals under the same lock guarantees a config save / disable wins
       the race: either the in-flight init finishes first and we then detach +
       tear down the client it built, or it waits for us and re-reads the fresh
       config — it can never repopulate the cache with an *old-config* client
       *after* the reset.
    2. **Never block the loop.** The synchronous flush runs off the event loop
       AND outside the lock, against the now-detached reference, so an
       unreachable Langfuse host can neither stall the loop nor hold the lock
       (a concurrent re-init builds its own new-config client meanwhile).
    """
    global _langfuse_client, _langfuse_enabled, _langfuse_retry_after
    async with _get_init_lock():
        old = _langfuse_client
        _langfuse_client = None
        _langfuse_enabled = None
        _langfuse_retry_after = 0
        _user_groups_cache.clear()
        logger.info(
            "Langfuse client cache cleared – next call will re-initialize from DB"
        )
    if old is None:
        return

    def _flush(client: Langfuse) -> None:
        try:
            client.flush()
        except Exception:
            logger.debug("Langfuse flush on reset failed (non-critical)", exc_info=True)

    await run_langfuse_blocking(_flush, old)


# ---------------------------------------------------------------------------
# User group resolution (for Langfuse attribution / tenant filtering)
# ---------------------------------------------------------------------------


async def _resolve_user_group_ids(db: AsyncSession, user_id: str) -> list[str]:
    """Resolve a user UUID to the list of group IDs they belong to.

    Returns an empty list for system / anonymous / unknown users, or on
    any lookup failure — trace startup must never crash on attribution
    metadata.  Cached per ``user_id`` for the lifetime of the process
    (invalidated via ``reset_langfuse_client``).
    """
    if not user_id or user_id in ("system", "None", "anonymous"):
        return []

    import time as _time

    cached = _user_groups_cache.get(user_id)
    if cached is not None and (_time.monotonic() - cached[1]) < _USER_GROUPS_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        import uuid as _uuid

        from app.api.v1.helpers import get_user_group_ids

        group_ids = await get_user_group_ids(db, _uuid.UUID(user_id))
        # Evict oldest entries when the cache is full
        if len(_user_groups_cache) >= _USER_GROUPS_CACHE_MAX:
            _user_groups_cache.pop(next(iter(_user_groups_cache)))
        _user_groups_cache[user_id] = (group_ids, _time.monotonic())
        return group_ids
    except ValueError:
        logger.debug(
            "Invalid user_id for Langfuse trace group lookup: %s", user_id
        )
    except Exception:
        logger.debug(
            "Failed to resolve Langfuse trace groups for user_id %s",
            user_id,
            exc_info=True,
        )

    return []


# ---------------------------------------------------------------------------
# Instance identity (for cross-install trace attribution)
# ---------------------------------------------------------------------------


def _resolve_instance_host() -> str:
    """Return the hostname of this deployment's public URL.

    Stamps every trace so a shared Langfuse workspace (one project,
    many customer installs) can be filtered per deployment.  Hostname
    only — never the full URL — so paths/queries don't leak into trace
    exports if a trace JSON is ever shared outside the workspace.
    Returns ``"unknown"`` on missing / unparseable ``frontend_url`` so
    trace startup never crashes on a malformed setting.
    """
    try:
        host = urlparse(settings.frontend_url or "").hostname
        return host or "unknown"
    except Exception:
        return "unknown"


def _resolve_platform_name() -> str:
    """Return the branded platform name for this install, or a safe default.

    Reads the synchronous in-process snapshot so the hot trace-start
    path doesn't incur a DB round-trip; any branding-resolver failure
    degrades to a stable placeholder instead of breaking traces.
    """
    try:
        return get_cached_branding().name or "unknown"
    except Exception:
        return "unknown"


def _resolve_instance_id() -> str:
    """Return the stable PlatformSettings row UUID for this install.

    Same value shown as ``Instance ID`` in the admin Settings page —
    opaque, stable across restarts, and the canonical key for license
    + observability attribution.  Falls back to ``"unknown"`` when the
    branding cache hasn't been primed yet (pre-startup code paths).
    """
    try:
        return get_cached_branding().instance_id or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Trace lifecycle (Langfuse SDK v4)
# ---------------------------------------------------------------------------


async def trace_llm_call(
    db: AsyncSession,
    *,
    name: str,
    user_id: str,
    source: str,
    model: str | None = None,
    input_data: Any = None,
    metadata: dict | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    agent_slug: str | None = None,
    session_id: str | None = None,
    assignment_id: str | None = None,
    assignment_name: str | None = None,
    run_id: str | None = None,
    trigger_type: str | None = None,
    parent_trace_ctx: dict | None = None,
) -> dict | None:
    """Start a Langfuse trace + generation span with full attribution.

    Returns a context dict with ``trace``, ``generation``, and ``client``
    keys, or ``None`` if Langfuse is disabled.

    Uses the Langfuse SDK v4 API:
    - ``propagate_attributes()`` context manager attaches trace-level
      attributes (user_id, session_id, tags) to the spans created within it
    - ``client.start_observation(as_type="span")`` creates a root span (trace)
    - ``root_span.start_observation(as_type="generation")`` creates a
      generation span under the root span

    When ``parent_trace_ctx`` is provided (or the contextvar set by
    ``set_current_trace_ctx`` carries one), the root span is opened as a
    **child observation** of the parent's root span so chained consults /
    transfers nest under the orchestrator's trace in the Langfuse UI
    instead of appearing as disconnected sibling traces.

    An explicit (non-``None``) ``parent_trace_ctx`` always takes precedence
    over the contextvar — an explicit empty/cleared mapping means "no
    parent" and starts a top-level trace rather than silently falling back
    to the contextvar and linking the wrong trace (same contract as
    ``capture_trace_context``).
    """
    client = await get_langfuse_client(db)

    try:
        # Strip images / base64 from input and remove null bytes to prevent
        # Langfuse media uploads and 400 Bad Request errors.
        safe_input = _sanitize_for_langfuse(input_data)

        # Resolve effective parent context first so both Langfuse-backed and
        # local-only traces preserve parent/child linkage semantics.
        effective_parent = (
            parent_trace_ctx
            if parent_trace_ctx is not None
            else get_current_trace_ctx()
        ) or None

        # Resolve the user's group memberships so multi-tenant traces
        # can be filtered per group in the Langfuse UI without leaking
        # PII (email).
        group_ids = await _resolve_user_group_ids(db, user_id)

        # Resolve instance identity so traces collected on a shared
        # Langfuse server (one workspace, many customer installs) can be
        # filtered per deployment.  ``instance_id`` is the stable
        # PlatformSettings row UUID (same value the admin Settings page
        # exposes as "Instance ID"); ``instance_host`` is a human-readable
        # hostname label — path/query stripped so customer-specific URLs
        # don't leak into trace exports if a trace JSON is shared
        # externally.
        instance_id = _resolve_instance_id()
        instance_host = _resolve_instance_host()
        platform_name = _resolve_platform_name()

        # Build rich metadata
        meta: dict[str, Any] = {
            "source": source,
            "user_id": user_id,
            "group_ids": group_ids,
            "instance_id": instance_id,
            "instance_host": instance_host,
            "platform_name": platform_name,
            "platform_mode": settings.platform_mode,
            "app_version": settings.app_version,
        }
        if model:
            meta["model"] = model
        if agent_id:
            meta["agent_id"] = agent_id
        if agent_name:
            meta["agent_name"] = agent_name
        if agent_slug:
            meta["agent_slug"] = agent_slug
        if assignment_id:
            meta["assignment_id"] = assignment_id
        if assignment_name:
            meta["assignment_name"] = assignment_name
        if run_id:
            meta["run_id"] = run_id
        if trigger_type:
            meta["trigger_type"] = trigger_type
        if metadata:
            meta.update(metadata)
        _link = _take_root_trace_link()
        if _link is not None:
            meta.update(_link.metadata)
        meta = redact_known_secrets(meta)

        # Build tags for Langfuse filtering/grouping.  Kept lean on purpose
        # — every tag becomes a chip in the Langfuse UI, so we only promote
        # fields you actually filter by.  ``instance_host`` / ``platform_name``
        # / ``app_version`` / ``platform_mode`` live in metadata above (still
        # filterable, just not cluttering the tag chip row).
        tags: list[str] = [
            f"source:{source}",
            f"instance:{instance_id}",
        ]
        if agent_slug:
            tags.append(f"agent_slug:{agent_slug}")
        if agent_id:
            tags.append(f"agent:{agent_id}")
        if agent_name:
            tags.append(f"agent_name:{agent_name}")
        if model:
            tags.append(f"model:{model}")
        if assignment_id:
            tags.append(f"assignment:{assignment_id}")
        if trigger_type:
            tags.append(f"trigger:{trigger_type}")

        # Local trace sink must keep working even when Langfuse is disabled or
        # temporarily unavailable (no config / auth failure / network outage).
        # Returning a local-only context here guarantees ``end_trace`` still
        # enqueues durable ``llm_traces`` rows for the Trace Explorer.
        if not client:
            logger.info(
                "Langfuse trace skipped for '%s' (source=%s): client not available; recording local trace only",
                name,
                source,
            )
            local_trace_id = uuid.uuid4()
            if _link is not None:
                _link.trace_id = local_trace_id
            _bind_background_run_checkpoint(
                local_trace_id, name=name, parent_ctx=effective_parent, model=model
            )
            return _register_open_trace({
                "trace": None,
                "generation": None,
                "client": None,
                "parent_ctx": effective_parent,
                "metadata": meta,
                # Unstripped, for the reason given on the success path below.
                "input_data": input_data,
                "local_trace_id": local_trace_id,
                "local_source": _resolve_local_trace_source(name, source),
                "local_name": _truncate(name, max_len=TRUNCATE_LABEL),
                "local_input": _to_jsonable(input_data),
                "local_metadata": _to_jsonable(meta),
                "local_tags": list(tags),
                "local_started_at": datetime.now(UTC),
                "local_status": "ok",
                "local_output": None,
                "local_error_message": None,
                "local_usage": None,
                "local_model": _truncate(model, max_len=TRUNCATE_LABEL) if model else None,
                "local_spans": [],
                "local_span_counter": 0,
                "local_user_id": _coerce_uuid(user_id),
                "local_conversation_id": _coerce_uuid(session_id),
                "local_agent_id": _coerce_uuid(agent_id),
                "local_assignment_id": _coerce_uuid(assignment_id),
                "local_assignment_run_id": _coerce_uuid(run_id),
            })

        # v4: ``propagate_attributes`` is the supported way to attach
        # trace-level attributes to spans (the v3 ``span.update_trace`` helper
        # was removed and now raises ``AttributeError``).  Spans created
        # inside the context inherit these attributes for the lifetime of
        # the trace, even after the context manager exits.
        # Note: importability of propagate_attributes is pre-validated in
        # get_langfuse_client(), so this import always succeeds when client
        # is not None.
        from langfuse import propagate_attributes

        # Resolve effective parent — an explicit (non-None) kwarg always wins
        # over the contextvar, and an explicit empty/cleared mapping means
        # "no parent" (same contract as ``capture_trace_context``).  Empty
        # mappings are normalized to ``None`` so downstream consumers —
        # ``end_trace``'s sub-agent detection via ``parent_ctx is not None``
        # in particular — treat the call as a top-level trace.
        parent_span = effective_parent.get("trace") if effective_parent else None

        # Serialized remote parent (captured via ``capture_trace_context`` and
        # rehydrated across a process boundary — e.g. a background sub-agent
        # task running on the worker).  No live span object is available, but
        # the SDK can still attach the new root observation to the parent's
        # trace via the ``trace_context`` kwarg on ``start_observation``.
        remote_trace_id: str | None = None
        remote_parent_span_id: str | None = None
        if parent_span is None and effective_parent:
            remote_trace_id = effective_parent.get("trace_id")
            remote_parent_span_id = effective_parent.get("parent_span_id")
        if remote_trace_id:
            meta["parent_trace_id"] = remote_trace_id

        # At the top level, the Langfuse UI renders the trace and the root
        # span as two nested rows.  Giving them the same name produces a
        # visual duplicate (``agent-chat:foo`` → ``agent-chat:foo`` →
        # iterations).  Set ``trace_name`` to the descriptive label and use
        # a generic root-span name (``agent_loop``) so the two rows are
        # distinguishable.  For sub-agent calls (``parent_span`` is set) we
        # are creating a *child* span inside an existing trace — there the
        # descriptive name on the span itself is what identifies the
        # sub-agent in the parent's tree, so we keep it.
        attrs_kwargs: dict[str, Any] = {
            "user_id": user_id,
            "session_id": session_id,
            "tags": tags,
        }
        if parent_span is None and not remote_trace_id:
            attrs_kwargs["trace_name"] = name
            span_name = "agent_loop"
        else:
            # Sub-agent span: drop the redundant ``agent-chat:`` prefix —
            # the trace already lives under the orchestrator's name, so
            # here we identify the sub-agent compactly.
            span_name = f"agent:{agent_slug}" if agent_slug else name

        with propagate_attributes(**attrs_kwargs):
            if parent_span is not None:
                # Nest under the orchestrator's root span so the Langfuse UI
                # shows the full delegation chain as a single trace.  Input is
                # set on the sub-agent span itself (the parent's trace I/O
                # belongs to the orchestrator).
                root_span = parent_span.start_observation(
                    name=span_name,
                    as_type="span",
                    input=safe_input,
                    metadata=meta,
                )
            elif remote_trace_id:
                # Cross-process nesting: attach to the parent's trace by id.
                # Same sub-agent semantics as the live-span branch (input on
                # the span itself, no trace_name override — the trace card
                # belongs to the orchestrator).
                trace_context: dict[str, str] = {"trace_id": remote_trace_id}
                if remote_parent_span_id:
                    trace_context["parent_span_id"] = remote_parent_span_id
                root_span = client.start_observation(
                    trace_context=trace_context,
                    name=span_name,
                    as_type="span",
                    input=safe_input,
                    metadata=meta,
                )
            else:
                # Top-level: input lives on the trace card, not on the root
                # span — avoids the duplicated "Additional input" panel.
                root_span = client.start_observation(
                    name=span_name,
                    as_type="span",
                    metadata=meta,
                )
                _safe_set_trace_io(root_span, input=safe_input)

            # Phase 2: the outer ``*-generation`` observation has been
            # removed.  Per-iteration generations are now created inside
            # ``run_agent_loop`` via ``start_llm_generation`` so each
            # LLM call carries its own input/output/token usage.  The root
            # span here is purely structural — it groups the iteration
            # spans and carries trace-level attributes.
            generation = None
        # The context manager has exited, but root_span (and any future
        # child observations created via the contextvar) carry the
        # propagated attributes for the lifetime of the trace.  This is
        # intentional and SDK-documented: attributes are attached to spans
        # at creation time, not read lazily.

        trace_id = getattr(root_span, "trace_id", None) or getattr(root_span, "id", "?")
        local_trace_id = uuid.uuid4()
        if _link is not None:
            _link.trace_id = local_trace_id
        # Phase 2 removed the outer ``*-generation`` observation in favour
        # of per-iteration generations created inside ``run_agent_loop``.
        # That leaves no obvious single observation id for downstream
        # feedback scoring to target (a chat turn has many iterations,
        # picking any one is arbitrary).  Cache the root span id instead:
        # it has the same lifetime as the trace, semantically wraps the
        # whole turn's work, and lets ``_push_langfuse_feedback_score``
        # attach scores to a specific observation rather than degrading to
        # a trace-wide score (Langfuse ``observation_id`` is finer-
        # grained than ``trace_id`` and is what the score consumer in
        # ``api/v1/chat.py`` prefers).
        root_span_id = getattr(root_span, "id", None) or getattr(
            root_span, "span_id", None
        )
        await cache_chat_trace_ids(
            session_id,
            str(trace_id) if trace_id else None,
            str(root_span_id) if root_span_id else None,
            str(local_trace_id) if local_trace_id else None,
        )
        logger.info(
            "Langfuse trace started: name='%s', source='%s', user='%s', model=%s, agent=%s, trace_id=%s, parent=%s",
            name,
            source,
            user_id,
            model or "N/A",
            agent_name or "N/A",
            trace_id,
            "yes"
            if parent_span is not None
            else ("remote" if remote_trace_id else "no"),
        )

        _bind_background_run_checkpoint(
            local_trace_id, name=name, parent_ctx=effective_parent, model=model
        )
        return _register_open_trace({
            "trace": root_span,
            "generation": generation,
            "client": client,
            "parent_ctx": effective_parent,
            # Stashed so ``end_trace`` can merge the usage rollup into it
            # — ``span.update(metadata=...)`` in Langfuse v4 *replaces*.
            "metadata": meta,
            # Stashed so ``update_trace_input_tools`` can swap just the tool
            # catalogue (progressive disclosure reveals groups mid-loop)
            # without clobbering messages / system_prompt / extra fields.
            # Kept unstripped: the stash is what the durable input is rebuilt
            # from on a tools swap, so stashing the media-stripped copy would
            # lose the images on the next update.
            "input_data": input_data,
            # Local trace sink payload (Story 1.4).
            "local_trace_id": local_trace_id,
            "local_source": _resolve_local_trace_source(name, source),
            "local_name": _truncate(name, max_len=TRUNCATE_LABEL),
            "local_input": _to_jsonable(input_data),
            "local_metadata": _to_jsonable(meta),
            "local_tags": list(tags),
            "local_started_at": datetime.now(UTC),
            "local_status": "ok",
            "local_output": None,
            "local_error_message": None,
            "local_usage": None,
            "local_model": _truncate(model, max_len=TRUNCATE_LABEL) if model else None,
            "local_spans": [],
            "local_span_counter": 0,
            "local_user_id": _coerce_uuid(user_id),
            "local_conversation_id": _coerce_uuid(session_id),
            "local_agent_id": _coerce_uuid(agent_id),
            "local_assignment_id": _coerce_uuid(assignment_id),
            "local_assignment_run_id": _coerce_uuid(run_id),
        })
    except Exception as exc:
        logger.warning(
            "Langfuse trace_llm_call failed for '%s' (source=%s): %s",
            name,
            source,
            format_exception_message(exc),
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Usage normalization (SDK v4)
# ---------------------------------------------------------------------------


def _usage_int(value: Any) -> int:
    """Best-effort integer coercion for provider usage payloads.

    Usage payloads may contain ints, numeric strings, floats, or null-like
    values. Invalid, negative, or non-integral values are treated as zero so
    tracing never raises on malformed provider responses.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else 0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0
        with contextlib.suppress(ValueError):
            parsed = float(stripped)
            if parsed >= 0 and parsed.is_integer():
                return int(parsed)
        return 0
    return 0


def _usage_detail_map(raw: Any) -> dict[str, int]:
    """Parse an OpenAI-style ``*_tokens_details`` payload into ``dict[str, int]``."""
    if isinstance(raw, dict):
        payload = raw
    elif hasattr(raw, "model_dump"):
        with contextlib.suppress(Exception):
            dumped = raw.model_dump()  # type: ignore[attr-defined]
            payload = dumped if isinstance(dumped, dict) else {}
    elif hasattr(raw, "dict"):
        with contextlib.suppress(Exception):
            dumped = raw.dict()  # type: ignore[attr-defined]
            payload = dumped if isinstance(dumped, dict) else {}
    elif hasattr(raw, "__dict__"):
        attrs = getattr(raw, "__dict__", {})
        payload = attrs if isinstance(attrs, dict) else {}
    else:
        payload = {}

    out: dict[str, int] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        parsed = _usage_int(value)
        if parsed > 0:
            out[key] = parsed
    return out


def _merge_usage_detail_maps(*maps: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for detail_map in maps:
        for key, value in detail_map.items():
            merged[key] = merged.get(key, 0) + int(value)
    return merged


def _flatten_usage_details(prefix: str, detail_map: dict[str, int]) -> dict[str, int]:
    return {f"{prefix}_{key}": value for key, value in detail_map.items() if value > 0}


# A run's usage is the key-by-key sum of its calls' usage
# (``agent_loop._merge_usage``), and ``_normalize_usage`` reads the FIRST key of
# each convention it finds: the sum of an Anthropic call (``input_tokens``) and
# an OpenAI call (``prompt_tokens``) reads the Anthropic call only, and the
# OpenAI call's ``total_tokens`` stands for the whole run. Such a run's
# usage also carries its calls' usage per convention and model under this key,
# and every reader here reads each part with its own convention. A run whose sum
# reads back exactly carries no parts: its usage, and every number read from it,
# are what they were.
USAGE_PARTS_KEY = "usage_parts"

# The keys ``_normalize_usage`` reads a count from, in the order it tries them:
# Anthropic, then OpenAI (and every provider in its schema), then Langfuse.
_USAGE_INPUT_KEYS = ("input_tokens", "prompt_tokens", "input")
_USAGE_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output")
_USAGE_TOTAL_KEYS = ("total_tokens", "total")


def _is_usage_count(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def usage_convention(usage: dict) -> tuple[str | None, bool]:
    """How ``_normalize_usage`` reads one call's *usage*.

    The input key its counts are read from (``input_tokens``, ``prompt_tokens``
    or ``input``; ``None`` when it reports none of them) and whether it reports
    a total. The usage of calls sharing a convention sums and reads back
    exactly; the usage of calls that do not may not.
    """
    family = next(
        (
            input_key
            for input_key, output_key in zip(_USAGE_INPUT_KEYS, _USAGE_OUTPUT_KEYS, strict=True)
            if _is_usage_count(usage.get(input_key)) or _is_usage_count(usage.get(output_key))
        ),
        None,
    )
    return family, any(_is_usage_count(usage.get(key)) for key in _USAGE_TOTAL_KEYS)


def usage_parts(usage: dict | None) -> list[tuple[str | None, dict]] | None:
    """``(model, usage)`` per part of a run's summed *usage*, or ``None``.

    ``None`` for a call's own usage, for a run whose sum reads back exactly and
    for anything malformed: its flat keys are then read as they always were.
    """
    raw = usage.get(USAGE_PARTS_KEY) if isinstance(usage, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    parts: list[tuple[str | None, dict]] = []
    for part in raw:
        if not isinstance(part, dict):
            return None
        model, part_usage = part.get("model"), part.get("usage")
        if not isinstance(part_usage, dict) or not (model is None or isinstance(model, str)):
            return None
        parts.append((model, part_usage))
    return parts


def _sum_normalized_usage(normalized: Iterable[dict[str, int]]) -> dict[str, int]:
    """Key-by-key sum of ``_normalize_usage`` results."""
    summed: dict[str, int] = {"input": 0, "output": 0, "total": 0}
    for part in normalized:
        for key, value in part.items():
            summed[key] = summed.get(key, 0) + int(value)
    return summed


def usage_parts_needed(usage: dict, parts: list[dict]) -> bool:
    """Whether reading a run's summed *usage* loses what reading its *parts* keeps.

    *parts* are the raw usage of the run's calls, summed per convention and
    model. Both readings are compared: the priced one and the token counts
    (read without the breakdown maps).
    """
    flat = {key: value for key, value in usage.items() if key != USAGE_PARTS_KEY}
    for strip in (False, True):
        read_flat = _normalize_usage(_without_usage_details(flat) if strip else flat)
        read_parts = _sum_normalized_usage(
            _normalize_usage(_without_usage_details(part) if strip else part) for part in parts
        )
        if any(read_flat.get(key, 0) != read_parts.get(key, 0) for key in read_flat.keys() | read_parts.keys()):
            return True
    return False


def _normalize_usage(usage: dict) -> dict:
    """Convert provider-specific usage dicts to Langfuse v4 ``usage_details``.

    LLM providers return different key names:
    - Anthropic: ``input_tokens``, ``output_tokens``, and cache tokens
      (``cache_creation_input_tokens``, ``cache_read_input_tokens``)
    - OpenAI / Gemini: ``prompt_tokens``, ``completion_tokens``

    Langfuse v4 ``usage_details`` accepts arbitrary ``Dict[str, int]`` keys.
    We pass all available token breakdown fields so Langfuse can match them
    against its model pricing configuration for accurate cost calculation.

    For Anthropic with prompt caching, ``input_tokens`` only counts the
    non-cached portion — the bulk of the input is in
    ``cache_read_input_tokens``.  We include all three fields separately
    so Langfuse can price them at their respective rates.

    A run's usage carrying parts (``USAGE_PARTS_KEY``) is the sum of its
    parts, each read with its own convention.
    """
    parts = usage_parts(usage)
    if parts is not None:
        return _sum_normalized_usage(_normalize_usage(part) for _model, part in parts)
    inp = _usage_int(
        usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input")
    )
    out = _usage_int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("output")
    )
    total_hint = _usage_int(usage.get("total_tokens") or usage.get("total"))
    cache_creation = _usage_int(usage.get("cache_creation_input_tokens"))
    cache_read = _usage_int(usage.get("cache_read_input_tokens"))

    # Langfuse flattens OpenAI nested token-detail maps into top-level keys
    # (input_* / output_*) so detail metrics stay queryable and costable.
    input_details = _merge_usage_detail_maps(
        _usage_detail_map(usage.get("prompt_tokens_details")),
        _usage_detail_map(usage.get("input_tokens_details")),
    )
    output_details = _merge_usage_detail_maps(
        _usage_detail_map(usage.get("completion_tokens_details")),
        _usage_detail_map(usage.get("output_tokens_details")),
    )
    input_detail_total = sum(input_details.values())
    output_detail_total = sum(output_details.values())

    input_base = max(inp - input_detail_total, 0)
    output_base = max(out - output_detail_total, 0)

    result: dict[str, int] = {
        "input": input_base + cache_creation + cache_read,
        "output": output_base,
    }

    if total_hint > 0:
        result["total"] = total_hint
    else:
        result["total"] = (
            result["input"]
            + result["output"]
            + input_detail_total
            + output_detail_total
        )

    if cache_creation:
        result["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        result["cache_read_input_tokens"] = cache_read

    result.update(_flatten_usage_details("input", input_details))
    result.update(_flatten_usage_details("output", output_details))

    return result


# ---------------------------------------------------------------------------
# End trace (SDK v4)
# ---------------------------------------------------------------------------

# One-time warning flag: ``set_trace_io`` is a deprecated alias in v4.
# If it's removed in a future SDK release we emit a single warning rather
# than silently losing trace output on every LLM call.
_set_trace_io_warned = False


def _safe_set_trace_io(span: Any, **kwargs: Any) -> None:
    """Call ``span.set_trace_io(**kwargs)`` with a one-time deprecation guard.

    ``set_trace_io`` is a deprecated alias retained in Langfuse SDK v4 for
    backwards compatibility.  If it is removed in a future release we log a
    single warning instead of silently losing trace-level output.
    """
    global _set_trace_io_warned
    if hasattr(span, "set_trace_io"):
        with contextlib.suppress(Exception):
            span.set_trace_io(**kwargs)
    elif not _set_trace_io_warned:
        _set_trace_io_warned = True
        logger.warning(
            "set_trace_io not available on LangfuseSpan — trace output not set. "
            "Langfuse SDK upgrade may be needed.",
        )


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return uuid.UUID(str(value))
    return None


def _resolve_local_trace_source(name: str, source: str | None) -> str:
    from app.models.llm_trace import LLM_TRACE_SOURCES

    source_raw = (source or "").strip().lower()
    # The one list of valid sources (also the CHECK constraint's), so a new
    # source is never accepted by the store and folded to ``proxy`` here.
    if source_raw in LLM_TRACE_SOURCES:
        return source_raw
    lowered_name = name.lower()
    if lowered_name.startswith("assignment"):
        return "assignment"
    if lowered_name.startswith("flow-run"):
        return "flow"
    if "general" in lowered_name and "chat" in lowered_name:
        return "general-chat"
    if "chat" in lowered_name or "conversation" in lowered_name:
        return "agent-chat"
    return "proxy"


def _bind_background_run_checkpoint(
    local_trace_id: uuid.UUID,
    *,
    name: str,
    parent_ctx: dict | None,
    model: str | None,
) -> None:
    """Checkpoint a background turn's trace from its open; nothing outside one.

    The recorder is bound by the background-task runner
    (``tracing.background_run.recording_spans``) and keeps only the first
    trace opened under it: the turn's own. Never raises.
    """
    try:
        from app.services.tracing.background_run import current_span_recorder

        recorder = current_span_recorder()
        if recorder is not None:
            recorder.bind(
                trace_id=local_trace_id,
                name=_truncate(name, max_len=TRUNCATE_LABEL),
                started_at=datetime.now(UTC),
                parent_trace_id=_parent_local_trace_id(parent_ctx),
                model=_truncate(model, max_len=TRUNCATE_LABEL) if model else None,
            )
    except Exception as exc:
        logger.debug("Background run checkpoint not opened: %s", format_exception_message(exc))


def _checkpoint_background_run_span(root_ctx: dict, span_record: dict[str, Any]) -> None:
    """Add an ended span of a background turn's own trace to its checkpoint. Never raises."""
    try:
        from app.services.tracing.background_run import current_span_recorder

        recorder = current_span_recorder()
        if recorder is not None and recorder.trace_id is not None and recorder.trace_id == root_ctx.get("local_trace_id"):
            recorder.record_span(span_record)
    except Exception as exc:
        logger.debug("Background run span not checkpointed: %s", format_exception_message(exc))


def _root_local_context(ctx: dict | None) -> dict | None:
    current = ctx
    while isinstance(current, dict):
        if "local_trace_id" in current:
            return current
        current = current.get("parent_ctx")
    return None


def _parent_local_trace_id(parent_ctx: dict | None) -> uuid.UUID | None:
    """The local trace a trace opened under *parent_ctx* is a child of.

    The parent may be another trace's context (a transfer opens its target's
    trace from the source's), a span inside one (a consult opens the helper's
    trace from inside the ``consult_agent`` tool span, whose context carries
    only ``local_root_trace_id``), or the serialized form
    ``capture_trace_context`` hands a delegated task. Reading
    ``local_trace_id`` off the parent itself linked only the first.
    """
    root = _root_local_context(parent_ctx)
    if root is not None:
        return _coerce_uuid(root.get("local_trace_id"))
    if isinstance(parent_ctx, dict):
        return _coerce_uuid(parent_ctx.get("local_root_trace_id"))
    return None


# ---------------------------------------------------------------------------
# Traces still open when the process stops
# ---------------------------------------------------------------------------
#
# The local trace is assembled in ``end_trace`` only. A turn still running
# when its process stops (a rolling deploy, a drained pod, a worker restarted
# under a long tool call) never reaches it, so the run left no trace at all.
# Every root context ``trace_llm_call`` hands out is kept here until its
# ``end_trace``; the graceful-stop paths (the web lifespan, the SAQ worker
# shutdown hook) close what is left as an ``error`` trace naming the step the
# run was on, before the tracing dispatcher drains.

#: Root contexts opened and not yet ended, by local trace id (insertion order).
_OPEN_TRACES: dict[Any, dict[str, Any]] = {}
#: Bound of :data:`_OPEN_TRACES`: past it the oldest entry is forgotten (a
#: caller that never ends its trace must not grow the process without bound).
_OPEN_TRACES_MAX = 2000
#: Set on a root context closed by the stop, so its own late ``end_trace``
#: writes nothing a second time.
_CLOSED_ON_SHUTDOWN_KEY = "local_closed_on_shutdown"
#: The spans started under a root and not yet ended, by span id.
_OPEN_SPANS_KEY = "local_open_spans"
#: ``metadata.kind`` of a trace closed by the process's stop.
INTERRUPTED_TRACE_KIND = "run_interrupted"


def _register_open_trace(ctx: dict[str, Any]) -> dict[str, Any]:
    """Keep *ctx* until its ``end_trace``; returns it. Never raises."""
    with contextlib.suppress(Exception):
        key = ctx.get("local_trace_id")
        if key is not None:
            _OPEN_TRACES[key] = ctx
            while len(_OPEN_TRACES) > _OPEN_TRACES_MAX:
                _OPEN_TRACES.pop(next(iter(_OPEN_TRACES)), None)
    return ctx


def _unregister_open_trace(ctx: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        key = ctx.get("local_trace_id")
        if key is not None and _OPEN_TRACES.get(key) is ctx:
            _OPEN_TRACES.pop(key, None)


def open_trace_count() -> int:
    """How many traces this process opened and has not ended."""
    return len(_OPEN_TRACES)


def _note_open_span(root_ctx: dict[str, Any], span_ctx: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        open_spans = root_ctx.setdefault(_OPEN_SPANS_KEY, {})
        if isinstance(open_spans, dict):
            open_spans[span_ctx["local_span_id"]] = span_ctx


def _forget_open_span(root_ctx: dict[str, Any], span_ctx: dict[str, Any] | None) -> None:
    with contextlib.suppress(Exception):
        open_spans = root_ctx.get(_OPEN_SPANS_KEY)
        if isinstance(open_spans, dict) and isinstance(span_ctx, dict):
            open_spans.pop(span_ctx.get("local_span_id"), None)


def _step_summary(span: dict[str, Any], *, open_: bool) -> dict[str, Any]:
    started = span.get("local_started_at") if open_ else span.get("started_at")
    summary: dict[str, Any] = {
        "type": str((span.get("local_span_type") if open_ else span.get("type")) or ""),
        "name": span.get("local_name") if open_ else span.get("name"),
        "started_at": started.isoformat() if isinstance(started, datetime) else None,
        "open": open_,
    }
    tool_call_id = span.get("local_tool_call_id") if open_ else span.get("tool_call_id")
    if tool_call_id:
        summary["tool_call_id"] = tool_call_id
    return summary


def _interrupted_spans(
    root_ctx: dict[str, Any], reason: str, now: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The spans still open under *root_ctx*, ended now as interrupted, and the run's last step."""
    open_spans = root_ctx.get(_OPEN_SPANS_KEY)
    started: list[dict[str, Any]] = (
        [s for s in open_spans.values() if isinstance(s, dict)] if isinstance(open_spans, dict) else []
    )
    started.sort(key=lambda s: int(s.get("local_position") or 0))
    records: list[dict[str, Any]] = []
    for span in started:
        span_started = span.get("local_started_at")
        if not isinstance(span_started, datetime):
            span_started = now
        span_meta = span.get("local_metadata")
        records.append(
            {
                "id": span.get("local_span_id") or uuid.uuid4(),
                "parent_span_id": span.get("local_parent_span_id"),
                "type": str(span.get("local_span_type") or "SPAN"),
                "name": _truncate(str(span.get("local_name") or "span"), max_len=255),
                "position": int(span.get("local_position") or 0),
                "started_at": span_started,
                "ended_at": now,
                "duration_ms": max(0.0, (now - span_started).total_seconds() * 1000.0),
                "input": span.get("local_input"),
                "model": span.get("local_model"),
                "metadata": {
                    **(span_meta if isinstance(span_meta, dict) else {}),
                    "interrupted": True,
                },
                "level": "ERROR",
                "status_message": reason,
                "tool_call_id": span.get("local_tool_call_id"),
                "tool_arguments": span.get("local_tool_arguments"),
            }
        )
    last_step: dict[str, Any] | None
    if started:
        last_step = _step_summary(started[-1], open_=True)
    else:
        local_spans = root_ctx.get("local_spans")
        ended: list[Any] = local_spans if isinstance(local_spans, list) else []
        last = max(
            (s for s in ended if isinstance(s, dict)),
            key=lambda s: int(s.get("position") or 0),
            default=None,
        )
        last_step = _step_summary(last, open_=False) if last else None
    return records, last_step


async def close_open_traces_on_shutdown(reason: str = "worker_stopped") -> int:
    """Close every trace this process still holds open as an ``error`` trace.

    Called by the graceful-stop paths before the tracing dispatcher drains, so
    a run cut by the stop leaves a trace: the spans it ended, each span still
    open ended now as interrupted, the step it was on in
    ``metadata.last_step``, failure cause ``engine_abandoned`` with detail
    *reason*. Its own ``end_trace``, if it still comes, writes nothing. The
    traces reach the dispatcher's queue before it returns. A hard kill runs
    no stop path, so this cannot cover it. Returns how many traces it
    closed; never raises.
    """
    closed = 0
    try:
        from app.services.failure_causes import (
            FailureCause,
            FailureClassification,
            FailureDetermination,
        )

        failure = FailureClassification(FailureCause.ENGINE_ABANDONED, FailureDetermination.STRUCTURAL, reason)
    except Exception:
        failure = None
    for ctx in list(_OPEN_TRACES.values()):
        try:
            if ctx.get(_CLOSED_ON_SHUTDOWN_KEY):
                continue
            now = datetime.now(UTC)
            message = f"Run interrupted: its process stopped ({reason}) before the run ended"
            records, last_step = _interrupted_spans(ctx, message, now)
            if isinstance(ctx.get("local_spans"), list):
                ctx["local_spans"].extend(records)
            interrupted = {"kind": INTERRUPTED_TRACE_KIND, "interrupted_by": reason, "last_step": last_step}
            # Both stashes: the failure merge in ``end_trace`` rebuilds the
            # local one from ``metadata``.
            for stash in ("metadata", "local_metadata"):
                current = ctx.get(stash)
                ctx[stash] = {**current, **interrupted} if isinstance(current, dict) else dict(interrupted)
            if last_step and last_step.get("name"):
                message = f"{message}; last step: {last_step.get('type')} {last_step.get('name')}"
            end_trace(ctx, error=message, failure=failure)
            closed += 1
        except Exception as exc:
            logger.warning("Open trace not closed at stop: %s", format_exception_message(exc))
        finally:
            ctx[_CLOSED_ON_SHUTDOWN_KEY] = True
            _unregister_open_trace(ctx)
    if closed:
        logger.info("Stop: closed %d open trace(s) as interrupted", closed)
        with contextlib.suppress(Exception):
            from app.services.tracing.dispatcher import wait_for_submitted_traces

            await wait_for_submitted_traces()
    return closed


def trace_root_ctx(ctx: dict | None) -> dict | None:
    """The trace *ctx* belongs to, rather than the span it may be.

    The agent loop makes each iteration's span (and each tool's) current, so
    ``get_current_trace_ctx()`` is usually a child span. The root is the
    nearest context carrying a local trace id - a nested agent's trace (a
    consult, a transfer) is its own root - else the outermost one.
    """
    root = _root_local_context(ctx)
    if root is not None:
        return root
    while isinstance(ctx, dict) and isinstance(ctx.get("parent_ctx"), dict):
        ctx = ctx["parent_ctx"]
    return ctx


def _next_local_span_position(root_ctx: dict) -> int:
    current = int(root_ctx.get("local_span_counter") or 0) + 1
    root_ctx["local_span_counter"] = current
    return current


def _build_local_span_context(
    parent_ctx: dict | None,
    *,
    span_type: str,
    name: str,
    input_payload: Any,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
    tool_call_id: str | None = None,
    tool_arguments: Any = None,
) -> dict[str, Any]:
    root_ctx = _root_local_context(parent_ctx)
    local_started_at = datetime.now(UTC)
    local_input = _to_jsonable(input_payload)
    local_metadata = _to_jsonable(metadata or {})
    ctx: dict[str, Any] = {
        "local_span_id": uuid.uuid4(),
        "local_parent_span_id": (
            parent_ctx.get("local_span_id")
            if isinstance(parent_ctx, dict)
            else None
        ),
        "local_root_trace_id": root_ctx.get("local_trace_id") if root_ctx else None,
        "local_span_type": span_type,
        "local_name": _truncate(name, max_len=TRUNCATE_LABEL),
        "local_input": local_input,
        "local_metadata": local_metadata,
        "local_started_at": local_started_at,
        "local_position": _next_local_span_position(root_ctx)
        if root_ctx
        else 1,
        "local_status": "ok",
    }
    if model:
        ctx["local_model"] = _truncate(model, max_len=TRUNCATE_LABEL)
    if tool_call_id:
        ctx["local_tool_call_id"] = _truncate(tool_call_id, max_len=TRUNCATE_LABEL)
    if tool_arguments is not None:
        ctx["local_tool_arguments"] = _coerce_tool_arguments_object(tool_arguments)
    if root_ctx is not None:
        _note_open_span(root_ctx, ctx)
    return ctx


def end_trace(
    ctx: dict | None,
    *,
    output: Any = None,
    usage: dict | None = None,
    model: str | None = None,
    error: str | None = None,
    failure: Any = None,
) -> None:
    """End a Langfuse root span and update the parent trace.

    *failure* is an optional
    :class:`app.services.failure_causes.FailureClassification` describing WHY
    the run failed. It is stored structurally on the durable trace
    (``llm_traces.failure_cause``) and mirrored onto the trace metadata
    (``failure_determination`` / ``failure_detail``), so the Trace Explorer can
    filter on a cause instead of on an error string. Typed as ``Any`` to keep
    this module importable without pulling the classifier in.

    Note the asymmetry with Langfuse: a Langfuse root trace's metadata is
    immutable once created, but the LOCAL trace is only assembled here, at the
    end — so anything known at failure time can still be written to it
    accurately. That is what makes an at-failure-time correction (this cause
    today; a credit-back that reverses the charge recorded at trace-open
    tomorrow) possible on the local store at all.

    Closes the root span with the final output / error.  Phase 2 removed
    the outer ``*-generation`` observation: per-iteration generations
    inside ``run_agent_loop`` now carry the token usage natively.  When
    ``ctx["generation"]`` is non-None (legacy callers), it is still closed
    for backwards compatibility.

    The ``usage`` argument is preserved for callers that want to attach
    an aggregate token total onto the root span's metadata (Langfuse will
    sum the child generations' tokens automatically), on an error end too.
    """
    if not ctx:
        return
    if ctx.get(_CLOSED_ON_SHUTDOWN_KEY):
        # Already written as interrupted by the process's stop: a second
        # row under the same trace id would fail or duplicate it.
        return
    _unregister_open_trace(ctx)
    try:
        # The final output is the assistant's answer, which routinely quotes
        # tool results verbatim (the null-byte vector) —
        # sanitize it before it reaches ``set_trace_io`` / ``span.update``
        # below, exactly like every span input.  Error strings can embed HTTP
        # response bodies (``format_http_error`` excerpts), so they get the
        # same treatment.
        output = _sanitize_for_langfuse(output)
        if error:
            error = _sanitize_for_storage(error)

        gen = ctx.get("generation")
        root_span = ctx.get("trace")
        trace_id = (
            getattr(root_span, "trace_id", None) or getattr(root_span, "id", "?")
            if root_span
            else "?"
        )

        if error:
            # A request cut short still spent what its provider reported
            # before the end: it rides on the error end like on a normal one.
            error_usage = _normalize_usage(usage) if usage else None
            # v4: update then end the (optional) outer generation with error
            if gen is not None:
                gen_error: dict = {"status_message": error, "level": "ERROR"}
                if error_usage:
                    gen_error["usage_details"] = error_usage
                with contextlib.suppress(Exception):
                    gen.update(**gen_error)
                    gen.end()
            # Also mark the trace itself with the error.  ``set_trace_io`` is
            # the v4 replacement for the removed ``update_trace`` method.
            if root_span:
                is_sub_agent = ctx.get("parent_ctx") is not None
                error_payload = f"ERROR: {error}"
                if is_sub_agent:
                    with contextlib.suppress(Exception):
                        root_span.update(
                            output=error_payload,
                            status_message=error,
                            level="ERROR",
                        )
                else:
                    _safe_set_trace_io(root_span, output=error_payload)
                    with contextlib.suppress(Exception):
                        root_span.update(
                            status_message=error,
                            level="ERROR",
                        )
                if error_usage:
                    merged = {
                        **(ctx.get("metadata") or {}),
                        "total_usage": error_usage,
                    }
                    with contextlib.suppress(Exception):
                        root_span.update(metadata=merged)
                with contextlib.suppress(Exception):
                    root_span.end()
            logger.info(
                "Langfuse trace ended with ERROR (trace_id=%s): %s",
                trace_id,
                error[:TRUNCATE_LABEL],
            )
        else:
            # v4: update the (optional) outer generation with output / usage
            if gen is not None:
                gen_kwargs: dict = {}
                if output is not None:
                    gen_kwargs["output"] = output
                if usage:
                    gen_kwargs["usage_details"] = _normalize_usage(usage)
                if model:
                    gen_kwargs["model"] = model
                with contextlib.suppress(Exception):
                    gen.update(**gen_kwargs)
                    gen.end()

            # Output handling depends on whether this is a top-level trace
            # or a sub-agent span:
            #   * top-level → set the trace's output (shown on the trace
            #     card) and leave the root span output blank — otherwise
            #     the UI shows the answer twice.
            #   * sub-agent → set output on the sub-agent span itself; the
            #     trace I/O belongs to the orchestrator.
            is_sub_agent = ctx.get("parent_ctx") is not None
            if root_span:
                if output is not None:
                    if is_sub_agent:
                        with contextlib.suppress(Exception):
                            root_span.update(output=output)
                    else:
                        _safe_set_trace_io(root_span, output=output)
                # Attach an aggregate usage hint on the root span metadata
                # so downstream readers can see the rollup at a glance.
                # Merge with the stashed creation metadata — v4's
                # ``update(metadata=...)`` replaces the dict.
                if usage:
                    merged = {
                        **(ctx.get("metadata") or {}),
                        "total_usage": _normalize_usage(usage),
                    }
                    with contextlib.suppress(Exception):
                        root_span.update(metadata=merged)
                with contextlib.suppress(Exception):
                    root_span.end()

            usage_str = ""
            if usage:
                normalized = _normalize_usage(usage)
                usage_str = (
                    f", tokens={{in={normalized['input']}, out={normalized['output']}}}"
                )
            logger.info(
                "Langfuse trace ended OK (trace_id=%s, model=%s%s)",
                trace_id,
                model or "N/A",
                usage_str,
            )

        # Story 1.4: enqueue a local durable trace payload (never blocks).
        # The sink prices ``normalized_usage``; the token columns count every
        # token, the ones a breakdown prices at another rate included. A run
        # whose calls reported in several conventions is priced part by part,
        # each at the model its calls' generations record.
        normalized_usage = _normalize_usage(usage) if usage else None
        token_usage = _normalize_usage(_without_usage_details(usage)) if usage else None
        priced_parts = usage_parts(usage) if usage else None
        local_output = _to_jsonable(output)
        # ``llm_traces.error_message`` is a Text column: keep the whole failure
        # text.  Error bodies are exactly what a post-incident audit reads, and
        # provider errors routinely embed the response payload that explains
        # the failure past the first 20k characters.
        local_error = error or None
        ctx["local_status"] = "error" if error else "ok"
        ctx["local_error_message"] = local_error
        # Structured failure attribution. Merged into the metadata
        # through ``enrich_trace_metadata`` rather than assigned directly so
        # the Langfuse root span carries it too — and so the creation-time
        # metadata (source, agent, quota accounting) is preserved rather than
        # replaced, which is what a naive ``span.update(metadata=...)`` does.
        local_failure_cause: str | None = None
        if failure is not None:
            with contextlib.suppress(Exception):
                failure_meta = redact_known_secrets(failure.as_trace_metadata())
                local_failure_cause = str(failure.cause)
                enrich_trace_metadata(ctx, failure_meta)
                # ``enrich_trace_metadata`` rebuilds ``local_metadata`` from
                # the Langfuse-side stash, which is the same dict at trace
                # open. Re-merge onto the local side anyway: this write is the
                # durable record, and it must not depend on the two stashes
                # having stayed in lock-step for the whole run.
                local_meta = ctx.get("local_metadata")
                ctx["local_metadata"] = (
                    {**local_meta, **failure_meta}
                    if isinstance(local_meta, dict)
                    else dict(failure_meta)
                )
        ctx["local_failure_cause"] = local_failure_cause
        ctx["local_output"] = local_output
        ctx["local_usage"] = normalized_usage
        ctx["local_model"] = (
            _truncate(model, max_len=TRUNCATE_LABEL)
            if model
            else ctx.get("local_model")
        )
        local_started_at = ctx.get("local_started_at")
        if not isinstance(local_started_at, datetime):
            local_started_at = datetime.now(UTC)
        local_finished_at = datetime.now(UTC)
        wall_ms = max(0.0, (local_finished_at - local_started_at).total_seconds() * 1000.0)

        from app.services.tracing.base import SpanData, TraceData
        from app.services.tracing.dispatcher import submit_trace_for_flush
        from app.services.tracing.run_rollup import rollup_from_spans

        parent_local_trace_id = _parent_local_trace_id(ctx.get("parent_ctx"))
        trace_metadata: dict = (
            dict(ctx["local_metadata"]) if isinstance(ctx.get("local_metadata"), dict) else {}
        )
        # The run's figures from the spans already in memory: sizes and
        # timings only, so every list read carries them without a span read.
        with contextlib.suppress(Exception):
            trace_metadata["rollup"] = rollup_from_spans(
                ctx.get("local_spans") or [],
                wall_ms=wall_ms,
                first_token_ms=_elapsed_ms(local_started_at, ctx.get("local_first_output_at")),
            )
            # Where the turn's time went before the first provider request.
            from app.services.tracing import turn_stages

            trace_metadata["rollup"].update(turn_stages.rollup_fields(ctx))

        trace_data = TraceData(
            id=ctx.get("local_trace_id") or uuid.uuid4(),
            source=str(ctx.get("local_source") or "system"),
            name=_truncate(str(ctx.get("local_name") or "trace"), max_len=255),
            started_at=local_started_at,
            ended_at=local_finished_at,
            duration_ms=wall_ms,
            status=str(ctx.get("local_status") or "ok"),
            user_id=ctx.get("local_user_id"),
            agent_id=ctx.get("local_agent_id"),
            conversation_id=ctx.get("local_conversation_id"),
            assignment_run_id=ctx.get("local_assignment_run_id"),
            parent_trace_id=parent_local_trace_id,
            model=_truncate(ctx.get("local_model"), max_len=255)
            if ctx.get("local_model")
            else None,
            environment=settings.platform_mode,
            metadata=trace_metadata,
            input=ctx.get("local_input"),
            output=ctx.get("local_output"),
            tags=ctx.get("local_tags") if isinstance(ctx.get("local_tags"), list) else [],
            level="ERROR" if error else "DEFAULT",
            error_message=ctx.get("local_error_message"),
            failure_cause=ctx.get("local_failure_cause"),
            input_tokens=(
                int(token_usage.get("input"))
                if isinstance(token_usage, dict)
                and token_usage.get("input") is not None
                else None
            ),
            output_tokens=(
                int(token_usage.get("output"))
                if isinstance(token_usage, dict)
                and token_usage.get("output") is not None
                else None
            ),
            total_tokens=(
                int(token_usage.get("total"))
                if isinstance(token_usage, dict)
                and token_usage.get("total") is not None
                else None
            ),
            # The parts ride inside ``usage_details`` rather than as a field of
            # their own: a trace payload queued in Redis during a rolling
            # deploy is rebuilt with ``TraceData(**raw)`` by whichever replica
            # pops it, and an older one must not refuse an unknown field.
            usage_details=(
                {
                    **normalized_usage,
                    USAGE_PARTS_KEY: [
                        {"model": part_model, "usage_details": _normalize_usage(part)}
                        for part_model, part in priced_parts
                    ],
                }
                if priced_parts and normalized_usage is not None
                else normalized_usage
            ),
        )
        spans: list[SpanData] = []
        for span in ctx.get("local_spans") or []:
            if not isinstance(span, dict):
                continue
            with contextlib.suppress(Exception):
                spans.append(
                    SpanData(
                        id=span["id"],
                        trace_id=trace_data.id,
                        parent_span_id=span.get("parent_span_id"),
                        type=span["type"],
                        name=span["name"],
                        position=span.get("position") or 0,
                        started_at=span["started_at"],
                        ended_at=span["ended_at"],
                        duration_ms=span.get("duration_ms"),
                        time_to_first_token=span.get("time_to_first_token"),
                        model=span.get("model"),
                        input=span.get("input"),
                        output=span.get("output"),
                        metadata=span.get("metadata") if isinstance(span.get("metadata"), dict) else {},
                        level=span.get("level") or "DEFAULT",
                        status_message=span.get("status_message"),
                        input_tokens=span.get("input_tokens"),
                        output_tokens=span.get("output_tokens"),
                        usage_details=span.get("usage_details"),
                        tool_call_id=span.get("tool_call_id"),
                        tool_arguments=span.get("tool_arguments"),
                        stop_reason=span.get("stop_reason"),
                    )
                )
        submit_trace_for_flush(trace_data, spans)

        # The Langfuse SDK v4 has a built-in background consumer thread
        # that batches and sends events asynchronously. No explicit flush()
        # is needed here — it would block the response path with a
        # synchronous HTTP call to Langfuse (observed ~150-300ms latency).
        # The SDK's internal batching handles delivery reliably.
        # Explicit flush() is only used during client shutdown
        # (see reset_langfuse_client).
    except Exception as exc:
        logger.warning("Langfuse end_trace failed: %s", format_exception_message(exc), exc_info=True)


# ---------------------------------------------------------------------------
# Phase 2 — Per-iteration + per-tool spans (inside ``run_agent_loop``)
# ---------------------------------------------------------------------------
#
# These helpers turn each agentic iteration and each individual tool
# execution into their own Langfuse observations under the parent trace
# created by ``trace_llm_call``.  They are intentionally lightweight: they
# return ``None`` (no-op) whenever there is no parent context, so the loop
# code can call them unconditionally without branching on Langfuse state.
#
# The contract is symmetric to ``trace_llm_call`` / ``end_trace``:
#   * ``start_*`` returns a ctx dict ``{trace, generation, parent_ctx, client}``
#     (``generation`` may be ``None`` for spans that don't host LLM calls).
#   * ``end_span`` closes any child span; it deliberately does NOT touch
#     trace-level I/O (that's reserved for ``end_trace`` on the root).


def _has_local_trace_context(ctx: dict | None) -> bool:
    """Return True when *ctx* is attached to a local trace root."""
    return _root_local_context(ctx) is not None


def _start_child_observation(
    parent_ctx: dict | None,
    *,
    as_type: str,
    name: str,
    span_type: str,
    input_payload: Any,
    metadata: dict,
    model: str | None = None,
    bound_export: bool = False,
    log_label: str,
    **local_kwargs: Any,
) -> dict | None:
    """Open one child observation under *parent_ctx* and build its ctx dict.

    The shared scaffolding behind every ``start_*`` helper below: parent
    resolution (explicit ``parent_ctx`` → contextvar), the guarded
    ``start_observation`` call, the local-only fallback taken when Langfuse
    is disabled or its SDK raises, and the ctx dict ``end_span`` /
    ``end_generation`` consume.  Callers supply only what actually differs:
    the observation kind, its name, and its already-built input / metadata.

    *input_payload* is what the durable local span stores verbatim; the copy
    handed to the Langfuse SDK is sanitized, and additionally bounded when
    *bound_export* is set (payloads that can carry whole tool arguments).

    *model* is forwarded to the SDK only when set — generations carry one,
    spans and tool observations do not.

    Returns ``None`` when there is nothing to attach to: no parent context
    at all, or the SDK call failed with no local trace root to fall back on.
    Callers can therefore invoke it unconditionally.
    """
    effective_parent = parent_ctx or get_current_trace_ctx()
    if not effective_parent:
        return None
    parent = effective_parent.get("trace")
    if parent is None and not _has_local_trace_context(effective_parent):
        return None
    metadata = redact_known_secrets(metadata)
    observation = None
    try:
        if parent is not None:
            safe_input = _sanitize_for_langfuse(input_payload)
            observation = parent.start_observation(
                name=name,
                as_type=as_type,
                input=_bound_for_langfuse(safe_input) if bound_export else safe_input,
                metadata=metadata,
                **({"model": model} if model is not None else {}),
            )
    except Exception as exc:
        # Logged unconditionally: a failed SDK call is worth knowing about
        # even when a local trace root lets the span survive in the durable
        # store — the pre-consolidation copies stayed silent in that case.
        logger.debug("%s failed: %s", log_label, format_exception_message(exc), exc_info=True)
        if not _has_local_trace_context(effective_parent):
            return None
    return {
        "trace": observation,
        "generation": observation if as_type == "generation" else None,
        "parent_ctx": effective_parent,
        "client": None,
        "metadata": metadata,
        **_build_local_span_context(
            effective_parent,
            span_type=span_type,
            name=name,
            input_payload=input_payload,
            metadata=metadata,
            model=model,
            **local_kwargs,
        ),
    }


def start_iteration_span(
    *,
    iteration: int,
    available_tools: list[str] | None = None,
    message_count: int | None = None,
    parent_ctx: dict | None = None,
) -> dict | None:
    """Open a Langfuse child span for one agentic-loop iteration.

    Returns a ctx dict compatible with ``end_span`` / ``set_current_trace_ctx``,
    or ``None`` when no parent trace is active (Langfuse disabled or untraced
    code path).

    The span's input is a small **delta** payload — ``available_tools`` and
    ``message_count`` — not the full conversation.  The full conversation
    lives on the iteration's child generation observation (created by
    ``start_llm_generation``).
    """
    effective_parent = parent_ctx or get_current_trace_ctx()
    if not effective_parent:
        return None
    iter_meta: dict = {"iteration": iteration, "observation_kind": "iteration"}
    if iteration == 1:
        # The agentic quota meters one platform action per logical request,
        # charged before the loop starts — surface it on the first iteration
        # so the Trace Explorer can show where the action was consumed.
        # Nested handover traces carry ``quota_actions_consumed = 0`` on
        # their root, so their iterations are correctly left unmarked.
        root_ctx = _root_local_context(effective_parent)
        root_meta = root_ctx.get("metadata") if root_ctx else None
        if isinstance(root_meta, dict):
            consumed = root_meta.get("quota_actions_consumed")
            if isinstance(consumed, int) and consumed > 0:
                iter_meta["quota_action_consumed"] = True
    input_payload: dict = {"iteration": iteration}
    if available_tools is not None:
        # Tool names can originate from external MCP servers — sanitize like
        # every other Langfuse-bound payload.
        input_payload["available_tools"] = _sanitize_for_langfuse(available_tools)
    if message_count is not None:
        input_payload["message_count"] = message_count
    return _start_child_observation(
        effective_parent,
        as_type="span",
        name=f"iteration-{iteration}",
        span_type="ITERATION",
        input_payload=input_payload,
        metadata=iter_meta,
        log_label=f"start_iteration_span (iteration={iteration})",
    )


def update_iteration_span_tools(ctx: dict | None, available_tools: list[str] | None) -> None:
    """Replace the ``available_tools`` of an already-open iteration span.

    The loop opens the span with the tool list it is about to offer, but a
    budget-driven model downgrade inside the same iteration re-caps the gate
    and may unload groups BEFORE the request is placed — the span would then
    report a surface the model never saw.  Patches both the
    live Langfuse observation and the durable ``local_input`` so the span
    closed by ``end_span`` reflects what was actually offered.  No-op when
    the ctx is missing or carries no iteration input.
    """
    if not ctx:
        return
    base = ctx.get("local_input")
    if not isinstance(base, dict):
        return
    try:
        updated = dict(base)
        if available_tools is None:
            updated.pop("available_tools", None)
        else:
            updated["available_tools"] = _sanitize_for_langfuse(list(available_tools))
        ctx["local_input"] = _to_jsonable(updated)
        span = ctx.get("trace")
        if span is not None:
            with contextlib.suppress(Exception):
                span.update(input=updated)
    except Exception as exc:
        logger.debug(
            "update_iteration_span_tools failed: %s", format_exception_message(exc), exc_info=True
        )


def start_memory_span(
    *,
    short_term_enabled: bool,
    medium_term_enabled: bool,
    persistent_enabled: bool,
    user_enabled: bool,
    parent_ctx: dict | None = None,
) -> dict | None:
    """Open a Langfuse child span for the chat turn's memory load.

    Returns a ctx dict compatible with ``end_span``, or ``None`` when no
    parent trace is active (Langfuse disabled or untraced code path) — or
    when **no tier is enabled**, since an all-false input with an all-zero
    output says nothing a reader can act on.  Evaluation mode zeroes every
    tier, so that is a whole class of turns kept out of the span tree.

    The span's input is which memory tiers are *enabled* for this turn —
    never the loaded memory text itself, so recording this costs no LLM
    tokens and never duplicates PII already covered by the memory stores'
    own access controls.
    """
    if not (short_term_enabled or medium_term_enabled or persistent_enabled or user_enabled):
        return None
    return _start_child_observation(
        parent_ctx,
        as_type="span",
        name="memory",
        span_type="MEMORY",
        input_payload={
            "short_term_enabled": short_term_enabled,
            "medium_term_enabled": medium_term_enabled,
            "persistent_enabled": persistent_enabled,
            "user_enabled": user_enabled,
        },
        metadata={"observation_kind": "memory"},
        log_label="start_memory_span",
    )


def start_flow_node_span(
    parent_ctx: dict | None,
    *,
    node_id: str,
    node_kind: str,
    label: str | None = None,
    input_payload: Any = None,
) -> dict | None:
    """Open a Langfuse child span for one flow-node execution.

    The flow walker opens one of these per node under the run's root trace,
    with the node's RENDERED configuration as input — the exact values the
    node computed with, which is what a failed step needs for troubleshooting.
    Closed by :func:`end_span` with the node's outputs or error.
    """
    # Generic SPAN type: the llm_spans check constraint enumerates types and
    # downstream consumers filter on them — the flow-node identity lives in
    # the metadata (observation_kind / node_id / node_kind) instead.
    return _start_child_observation(
        parent_ctx,
        as_type="span",
        name=f"node:{node_id}",
        span_type="SPAN",
        input_payload=input_payload,
        metadata={
            "observation_kind": "flow_node",
            "node_id": node_id,
            "node_kind": node_kind,
            **({"node_label": label} if label else {}),
        },
        log_label="start_flow_node_span",
    )


#: ``metadata.observation_kind`` of a loop-breaker span. The Trace
#: Explorer and ``inspect_my_traces`` group on it, which is how "how often does
#: the breaker fire, and on which tool" is answered without reading transcripts.
LOOP_OBSERVATION_KIND = "loop"


def start_loop_span(
    parent_ctx: dict | None,
    *,
    tool_name: str,
    kind: str,
    action: str,
    count: int,
    threshold: int,
    arguments: Any = None,
) -> dict | None:
    """Open a child span recording one loop-breaker event.

    The agent loop opens one of these under the iteration span whenever the
    breaker WARNS about (``action="warn"``) or REFUSES (``action="refuse"``)
    a repeated call, and closes it at once with :func:`end_span`: the span
    is a record, not a duration. *kind* is ``identical`` (same tool, same
    arguments) or ``similar`` (same tool, same file / URL target, other
    arguments varying); *count* is how many times the call had been made
    this run when the breaker fired, *threshold* the bound it crossed. Named
    ``loop:<tool>`` with ``observation_kind = "loop"`` so the events of one
    tool group together in the Trace Explorer; the generic ``SPAN`` type
    keeps it inside the ``llm_spans`` check constraint.
    """
    return _start_child_observation(
        parent_ctx,
        as_type="span",
        name=f"loop:{tool_name}",
        span_type="SPAN",
        input_payload={
            "tool": tool_name,
            "kind": kind,
            "action": action,
            "count": count,
            "threshold": threshold,
            "arguments": _coerce_tool_arguments_object(arguments) if arguments is not None else None,
        },
        metadata={
            "observation_kind": LOOP_OBSERVATION_KIND,
            "tool_name": tool_name,
            "loop_kind": kind,
            "loop_action": action,
        },
        bound_export=True,
        log_label="start_loop_span",
    )


def start_llm_generation(
    parent_ctx: dict | None,
    *,
    model: str | None,
    messages: list | None = None,
    tools: list | None = None,
    system_prompt: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Open a Langfuse generation observation under any parent span.

    Carries the LLM call's input (messages + tools + system_prompt) and is
    later closed by :func:`end_generation` with the model output, usage,
    and optional ``stop_reason``.

    The parent is typically an iteration span (for in-loop calls) but can
    be any context — sub-agent generations or one-shot calls outside the
    agentic loop pass their own parent ctx.

    Returns ``None`` when ``parent_ctx`` is missing or carries no span.
    The first one of a turn closes the turn's pre-generation timeline
    (:mod:`app.services.tracing.turn_stages`).
    """
    if not parent_ctx:
        return None
    # Lazy: the tracing package imports this module (local_sink).
    from app.services.tracing import turn_stages

    _turn_stages = turn_stages.of_root(_root_local_context(parent_ctx))
    if _turn_stages is not None:
        _turn_stages.close()
    meta: dict = {"observation_kind": "llm_generation"}
    if metadata:
        meta.update(metadata)
    return _start_child_observation(
        parent_ctx,
        as_type="generation",
        name="llm_call",
        span_type="GENERATION",
        input_payload=build_trace_input(
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
        ),
        metadata=meta,
        model=model,
        log_label="start_llm_generation",
    )


def mark_first_output(gen_ctx: dict | None) -> None:
    """Note the instant a streamed generation produced its first output.

    Called for every streamed chunk (answer text or reasoning delta); only the
    first call of a generation is kept, and the first of the whole trace is kept
    on its root too. :func:`end_generation` turns it into the span's
    ``time_to_first_token`` and ``metadata.ttft_ms``; :func:`end_trace` into the
    run rollup's ``first_token_ms``. A dict lookup per chunk after the first:
    the measure costs nothing a reader can see. Never raises.
    """
    if not isinstance(gen_ctx, dict) or "local_first_output_at" in gen_ctx:
        return
    now = datetime.now(UTC)
    gen_ctx["local_first_output_at"] = now
    root_ctx = _root_local_context(gen_ctx.get("parent_ctx"))
    if root_ctx is not None and "local_first_output_at" not in root_ctx:
        root_ctx["local_first_output_at"] = now
        from app.services.tracing import turn_stages

        _turn_stages = turn_stages.of_root(root_ctx)
        if _turn_stages is not None:
            _turn_stages.first_output()


def _elapsed_ms(started_at: Any, at: Any) -> float | None:
    """Milliseconds from *started_at* to *at*, or ``None`` when either is not a datetime."""
    if not isinstance(started_at, datetime) or not isinstance(at, datetime):
        return None
    return round(max(0.0, (at - started_at).total_seconds() * 1000.0), 1)


def start_continuation_generation(
    parent_iter_ctx: dict | None,
    *,
    n: int,
    model: str | None,
    delta_messages: list | None = None,
    prior_message_count: int | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Open a generation for a ``max_tokens`` continuation of a prior call.

    Sibling to the original ``llm_call`` generation under the same
    iteration span — the original is already closed by the time
    continuations fire, so resurrecting it is not possible.  Named
    ``attempt:continuation:N`` (1-based) to mirror the
    ``attempt:retry:N`` convention from the retry observer.

    Input carries only the *delta* messages newly appended for this
    continuation (assistant partial + ``_CONTINUATION_PROMPT``) plus a
    ``prior_message_count`` marker — the full prefix already lives on
    the iteration generation, so duplicating it would inflate trace
    storage without adding debug value.

    Closed via ``end_span`` exactly like other generations (output +
    usage on success, ``error=`` on failure).

    Returns ``None`` when ``parent_iter_ctx`` is missing or has no span
    — safe for callers to invoke unconditionally.
    """
    if not parent_iter_ctx:
        return None
    input_payload = build_trace_input(messages=delta_messages)
    if prior_message_count is not None:
        input_payload["prior_message_count"] = prior_message_count
    input_payload["continuation_attempt"] = n
    meta: dict = {"observation_kind": "continuation", "attempt": n}
    if metadata:
        meta.update(metadata)
    return _start_child_observation(
        parent_iter_ctx,
        as_type="generation",
        name=f"attempt:continuation:{n}",
        span_type="GENERATION",
        input_payload=input_payload,
        metadata=meta,
        model=model,
        log_label=f"start_continuation_generation (n={n})",
    )


def start_empty_response_retry_generation(
    parent_iter_ctx: dict | None,
    *,
    model: str | None,
    delta_messages: list | None = None,
    prior_message_count: int | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Open a generation for the one-shot rescue LLM call that fires
    when the iteration's main call returned empty content with no
    tool calls.

    Sibling to the original ``llm_call`` generation under the same
    iteration span (the original is already closed by the time the
    rescue path runs).  Named ``attempt:empty_response_retry`` — only
    one occurrence per iteration, so no ``:N`` counter.

    Input carries only the appended ``_EMPTY_RESPONSE_RETRY`` system
    message plus a ``prior_message_count`` marker, mirroring the
    continuation generation contract.  Closed via ``end_span``.

    Returns ``None`` when ``parent_iter_ctx`` is missing or has no
    span — safe to invoke unconditionally.
    """
    if not parent_iter_ctx:
        return None
    input_payload = build_trace_input(messages=delta_messages)
    if prior_message_count is not None:
        input_payload["prior_message_count"] = prior_message_count
    meta: dict = {"observation_kind": "empty_response_retry"}
    if metadata:
        meta.update(metadata)
    return _start_child_observation(
        parent_iter_ctx,
        as_type="generation",
        name="attempt:empty_response_retry",
        span_type="GENERATION",
        input_payload=input_payload,
        metadata=meta,
        model=model,
        log_label="start_empty_response_retry_generation",
    )


def start_activation_gate_generation(
    parent_ctx: dict | None,
    *,
    model: str | None,
    messages: list | None = None,
    system_prompt: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Open a generation for an assignment's activation-gate LLM call.

    The activation gate is a single pre-flight YES/NO check that runs
    before the agent loop to decide whether the assignment should
    actually execute.  Opened as a **child generation** under the
    assignment workflow root span (passed as ``parent_ctx``) so the
    gate appears nested under the run in Langfuse — sibling to the
    iteration spans of the eventual agent loop.

    Named ``"activation-gate"`` (no ``attempt:N`` counter — one gate
    per run).  Closed via ``end_span`` with output/usage on success
    or ``error=`` on failure.

    Returns ``None`` when ``parent_ctx`` is missing or has no span —
    safe to invoke unconditionally.
    """
    if not parent_ctx:
        return None
    meta: dict = {"observation_kind": "activation_gate"}
    if metadata:
        meta.update(metadata)
    return _start_child_observation(
        parent_ctx,
        as_type="generation",
        name="activation-gate",
        span_type="GENERATION",
        input_payload=build_trace_input(messages=messages, system_prompt=system_prompt),
        metadata=meta,
        model=model,
        log_label="start_activation_gate_generation",
    )


def enrich_trace_input(ctx: dict | None, input_data: Any) -> None:
    """Update the trace-card input on an already-opened root span.

    Used when the trace must be created early (e.g. before the
    activation gate runs) with a placeholder input that is later
    replaced once the full agent-loop payload (messages, tools,
    system_prompt) is assembled.  No-op when ``ctx`` is missing or
    has neither a live span nor a local trace root — a local-only ctx
    (Langfuse disabled/unavailable — see ``trace_llm_call``'s
    ``if not client`` branch) still needs its ``local_input`` patched so
    ``end_trace``'s durable write reflects the full payload instead of
    the creation-time placeholder.
    """
    if not ctx:
        return
    span = ctx.get("trace")
    if span is None and not _has_local_trace_context(ctx):
        return
    try:
        if span is not None:
            _safe_set_trace_io(span, input=_sanitize_for_langfuse(input_data))
        # Keep the stash in sync so a later ``update_trace_input_tools`` swaps
        # tools on top of *this* payload, not the (now stale) creation-time one.
        # Stash and durable input stay unstripped; only the export above loses
        # its media.
        ctx["input_data"] = input_data
        ctx["local_input"] = _to_jsonable(input_data)
    except Exception as exc:
        logger.debug("enrich_trace_input failed: %s", format_exception_message(exc), exc_info=True)


def update_trace_input_tools(ctx: dict | None, tools: list | None) -> None:
    """Replace only the ``tools`` entry of an already-set trace-card input.

    Progressive tool disclosure reveals tool groups *during* the agent loop.
    Without this, the root trace card would forever show the full pre-gating
    catalogue (every tool the agent *could* use), which reads as "N tools were
    sent" and is misleading once gating is active. The loop calls this so the
    card's ``tools`` list stays in lock-step with what the model was actually
    offered across the run — the always-on core + ``load_tools`` + every group
    the model has revealed so far. Because the revealed set only ever grows,
    the value after the final reveal equals the union of tools available across
    all iterations.

    Every other input field (messages, system_prompt, extra) is preserved via
    the stashed ``ctx['input_data']``. No-op when ``ctx`` or the stash is
    missing, or when ``ctx`` has neither a live span nor a local trace root —
    a local-only ctx (Langfuse disabled/unavailable — see ``trace_llm_call``'s
    ``if not client`` branch) still needs ``local_input`` patched so
    ``end_trace``'s durable write reflects the revealed tool groups instead of
    the creation-time placeholder.
    """
    if not ctx:
        return
    span = ctx.get("trace")
    if span is None and not _has_local_trace_context(ctx):
        return
    base = ctx.get("input_data")
    if not isinstance(base, dict):
        return
    try:
        updated = {**base, "tools": _serialize_trace_tools(tools or [])}
        if span is not None:
            _safe_set_trace_io(span, input=_sanitize_for_langfuse(updated))
        ctx["input_data"] = updated
        ctx["local_input"] = _to_jsonable(updated)
    except Exception as exc:
        logger.debug("update_trace_input_tools failed: %s", format_exception_message(exc), exc_info=True)


def enrich_trace_metadata(ctx: dict | None, metadata: dict | None) -> None:
    """Merge additional metadata into an already-opened root span.

    Companion to ``enrich_trace_input`` for fields that belong on the
    span's metadata (and thus remain filterable in the Langfuse UI)
    rather than embedded in the input JSON.  Used when the metadata
    is only known after the trace was created — for example resolved
    model / tool counts / max-iterations on an assignment workflow
    trace whose root was opened before the agent loop was assembled.

    Critically, Langfuse v4's ``span.update(metadata=...)`` *replaces*
    the metadata dict rather than merging — same reason ``end_trace``
    stashes ``ctx["metadata"]`` and rebuilds the dict before applying
    the usage rollup (see ``end_trace`` ≈30 lines below).  A naive
    ``span.update(metadata=metadata)`` here would wipe every
    creation-time field (``source``, ``user_id``, ``session_id``,
    ``instance``, ``agent``, ``assignment``, ``observation_kind``)
    and break filterability in the Langfuse UI for every trace that
    goes through this enrichment path (today: every assignment
    workflow).

    We therefore merge the new keys on top of the stashed dict, push
    the full result to the span, AND keep ``ctx["metadata"]`` in sync
    so the subsequent ``end_trace`` usage-rollup merge sees the
    enriched fields too.  New keys take precedence over existing
    ones — the enrichment path is the canonical source for values
    only known after trace creation.

    No-op when ``ctx`` or ``metadata`` is missing, or when ``ctx`` has
    neither a live span nor a local trace root — a local-only ctx
    (Langfuse disabled/unavailable) still needs ``local_metadata``
    patched so ``end_trace``'s durable write picks up the enriched
    fields instead of only the creation-time metadata.
    """
    if not ctx or not metadata:
        return
    span = ctx.get("trace")
    if span is None and not _has_local_trace_context(ctx):
        return
    try:
        merged = {**(ctx.get("metadata") or {}), **redact_known_secrets(metadata)}
        if span is not None:
            span.update(metadata=merged)
        # Keep the stash in lock-step so later mutations
        # (``end_trace`` usage rollup, future enrichment calls) see
        # the cumulative dict rather than re-applying creation-only
        # state on top of fresher keys.
        ctx["metadata"] = merged
        ctx["local_metadata"] = _to_jsonable(merged)
    except Exception as exc:
        logger.debug("enrich_trace_metadata failed: %s", format_exception_message(exc), exc_info=True)


def set_trace_model(ctx: dict | None, model: str | None) -> None:
    """Record that the run now continues on *model* (a budget downgrade).

    Updates the model the durable trace is written with, which is otherwise
    the model the trace was opened on until ``end_trace`` names another: a
    run that ends on an error after the switch no longer names the model it
    left. ``end_trace(model=...)`` still wins.
    """
    if not ctx or not model:
        return
    ctx["local_model"] = _truncate(model, max_len=TRUNCATE_LABEL)


def start_direct_generation(
    parent_ctx: dict | None = None,
    *,
    model: str | None,
    messages: list | None = None,
    tools: list | None = None,
    system_prompt: str | None = None,
    metadata: dict | None = None,
    name: str = "llm_call",
) -> dict | None:
    """Open a generation observation directly under the trace root.

    Used by the ``agent_chat`` no-tools fast-path (and any other site that
    makes a single LLM call without going through ``run_agent_loop``).  The
    parent is resolved via the explicit ``parent_ctx`` or the contextvar,
    so the generation nests under the current agent's root span instead of
    appearing as a sibling of the trace.

    Returns ``None`` when no parent context is active (Langfuse disabled or
    untraced code path) — safe to call unconditionally.
    """
    meta: dict = {"observation_kind": "direct_generation"}
    if metadata:
        meta.update(metadata)
    return _start_child_observation(
        parent_ctx,
        as_type="generation",
        name=name,
        span_type="GENERATION",
        input_payload=build_trace_input(
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
        ),
        metadata=meta,
        model=model,
        log_label="start_direct_generation",
    )


def start_tool_span(
    *,
    tool_name: str,
    tool_call_id: str | None = None,
    arguments: Any = None,
    parent_ctx: dict | None = None,
) -> dict | None:
    """Open a Langfuse child span for one individual tool execution.

    Captures the tool name and the complete JSON-encoded arguments for the
    durable store; only the copy handed to the Langfuse SDK is bounded.
    Returns ``None`` when no parent span is available — safe to call
    unconditionally from the loop dispatch code.
    """
    # Compact JSON representation, kept whole for the durable store.
    try:
        args_repr = json.dumps(arguments, default=str, ensure_ascii=False)
    except Exception:
        args_repr = repr(arguments)
    input_payload: dict = {"tool": tool_name, "arguments": args_repr}
    if tool_call_id:
        input_payload["tool_call_id"] = tool_call_id
    return _start_child_observation(
        parent_ctx,
        as_type="tool",
        name=f"tool:{tool_name}",
        span_type="TOOL",
        input_payload=input_payload,
        metadata={
            "observation_kind": "tool",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
        },
        # Bounded and media-stripped for the external export only — Langfuse
        # ingestion has its own size limits and its SDK would upload the
        # blobs.  The local span keeps ``input_payload`` whole, media included.
        bound_export=True,
        log_label=f"start_tool_span (tool={tool_name})",
        tool_call_id=tool_call_id,
        tool_arguments=arguments,
    )


def annotate_current_tool_span(fields: dict[str, str]) -> None:
    """Record facts learned while a tool runs on the tool span it runs under.

    The agent loop pushes each tool span on the context for the duration of
    the call (each parallel call in its own task), so this reaches the right
    span; outside a tool call - a connectivity test, a sync - it does nothing.
    The fields reach the durable store and, at close, the Langfuse export.
    Never raises.
    """
    try:
        ctx = _current_trace_ctx.get()
        if not isinstance(ctx, dict) or ctx.get("local_span_type") != "TOOL":
            return
        fields = redact_known_secrets(fields)
        if isinstance(ctx.get("local_metadata"), dict):
            ctx["local_metadata"].update(fields)
        ctx.setdefault("added_metadata", {}).update(fields)
    except Exception as exc:
        logger.debug("annotate_current_tool_span failed: %s", format_exception_message(exc))


#: Bound on the distinct providers one tool span records (a tool fans out to one or two).
_MAX_HTTP_PROVIDERS_PER_SPAN = 4


def record_tool_http_call(
    provider: str,
    *,
    ms: float,
    status: int | None,
    retries: int,
    count_5xx: int,
    count_429: int,
    network_error: bool = False,
) -> None:
    """Add one integration HTTP call to the tool span it runs under.

    ``metadata.http`` on the span is ``{provider: {calls, ms, retries, 5xx,
    429, network_errors, status}}``, accumulated over the calls of one tool
    execution (``status`` is the last one seen). Figures only: no URL, no
    payload, no credential. Outside a tool call it does nothing. Never raises.
    """
    try:
        ctx = _current_trace_ctx.get()
        if not isinstance(ctx, dict) or ctx.get("local_span_type") != "TOOL":
            return
        local_meta = ctx.get("local_metadata")
        if not isinstance(local_meta, dict):
            return
        http = local_meta.get("http")
        if not isinstance(http, dict):
            http = {}
            local_meta["http"] = http
            # The same dict rides on the Langfuse export at close.
            ctx.setdefault("added_metadata", {})["http"] = http
        name = _truncate(str(provider or "unknown"), max_len=64)
        entry = http.get(name)
        if entry is None:
            if len(http) >= _MAX_HTTP_PROVIDERS_PER_SPAN:
                return
            entry = http[name] = {"calls": 0, "ms": 0.0, "retries": 0, "5xx": 0, "429": 0, "network_errors": 0}
        entry["calls"] += 1
        entry["ms"] = round(entry["ms"] + max(0.0, float(ms)), 1)
        entry["retries"] += max(0, int(retries))
        entry["5xx"] += max(0, int(count_5xx))
        entry["429"] += max(0, int(count_429))
        entry["network_errors"] += int(bool(network_error))
        entry["status"] = status
    except Exception as exc:
        logger.debug("record_tool_http_call failed: %s", format_exception_message(exc))


def _close_observation(
    ctx: dict | None,
    *,
    output: Any = None,
    usage: dict | None = None,
    model: str | None = None,
    error: str | BaseException | None = None,
    level: str | None = None,
    extra_metadata: dict | None = None,
) -> None:
    """Canonical close path for any child observation (span / generation /
    tool span).  Owns the ``_sanitize_for_langfuse`` → ``_truncate`` →
    ``_normalize_usage`` → ``span.update`` → ``span.end`` sequence so the
    three public exits (``end_span`` / ``end_generation`` / ``end_tool_span``)
    never drift.

    - ``error`` short-circuits: sets ``status_message`` + ``level="ERROR"``
      (unless an explicit ``level`` is provided) and closes immediately.
      ``usage`` still rides on it: a stream cancelled or aborted after its
      provider reported usage was billed for it, and ``None`` changes nothing.
    - ``output`` / ``usage`` / ``model`` are forwarded to ``span.update``.
    - ``level`` (e.g. ``"WARNING"``) is propagated to ``span.update`` when
      no ``error`` is present.
    - ``extra_metadata`` is merged into ``span.update(metadata=...)`` for
      facade-specific fields (``stop_reason``, ``error_kind``).
    """
    if not ctx:
        return
    # Time to first token of a streamed generation: set only when
    # ``mark_first_output`` saw a chunk, never estimated.
    ttft_ms = _elapsed_ms(ctx.get("local_started_at"), ctx.get("local_first_output_at"))
    if ttft_ms is not None:
        extra_metadata = {**(extra_metadata or {}), "ttft_ms": ttft_ms}
    span = ctx.get("trace")
    base_metadata = ctx.get("metadata") or {}
    added_metadata = ctx.get("added_metadata")
    merged_metadata: dict | None = None
    if extra_metadata or added_metadata:
        merged_metadata = {**base_metadata, **(added_metadata or {}), **(extra_metadata or {})}
    safe_output: Any = None
    # The full-fidelity payload written to the durable store, kept separate from
    # ``safe_output`` (which stays bounded for the external Langfuse export).
    full_output: Any = None
    local_level = "DEFAULT"
    local_error_message: str | None = None
    normalized_usage: dict | None = None
    effective_model = _truncate(model, max_len=TRUNCATE_LABEL) if model else ctx.get("local_model")
    try:
        if error is not None:
            # Exception text can embed HTTP response bodies (e.g.
            # ``format_http_error`` excerpts of tool results), so the null-byte
            # vector applies to status messages too, and a
            # failing tool's output (``end_tool_span``) is the error itself.
            error_str = _sanitize_for_storage(
                f"{type(error).__name__}: {error}"
                if isinstance(error, BaseException)
                else str(error)
            )
            local_level = level or "ERROR"
            # ``llm_spans.status_message`` is Text — store the failure whole.
            # Only the label handed to the Langfuse SDK below stays clipped.
            local_error_message = error_str
            err_kwargs: dict = {
                "status_message": error_str[:TRUNCATE_LABEL],
                "level": level or "ERROR",
            }
            if usage:
                normalized_usage = _normalize_usage(usage)
                err_kwargs["usage_details"] = normalized_usage
            if merged_metadata is not None:
                err_kwargs["metadata"] = merged_metadata
            if span is not None:
                with contextlib.suppress(Exception):
                    span.update(**err_kwargs)
                with contextlib.suppress(Exception):
                    span.end()
        else:
            local_level = level or "DEFAULT"
            update_kwargs: dict = {}
            if output is not None:
                # Durable store: verbatim.  External Langfuse export: bounded at
                # every embedded string (dict/list outputs carry content +
                # tool_calls + reasoning, so the top level alone is not enough).
                full_output = _to_jsonable(output)
                safe_output = _bound_for_langfuse(_sanitize_for_langfuse(output))
                update_kwargs["output"] = safe_output
            if usage:
                normalized_usage = _normalize_usage(usage)
                update_kwargs["usage_details"] = normalized_usage
            if model:
                update_kwargs["model"] = model
            if level:
                update_kwargs["level"] = level
            if merged_metadata is not None:
                update_kwargs["metadata"] = merged_metadata
            if span is not None and update_kwargs:
                with contextlib.suppress(Exception):
                    span.update(**update_kwargs)
            if span is not None:
                with contextlib.suppress(Exception):
                    span.end()
    except Exception as exc:
        logger.debug("_close_observation failed: %s", format_exception_message(exc), exc_info=True)
    finally:
        root_ctx = _root_local_context(ctx)
        if root_ctx is not None:
            _forget_open_span(root_ctx, ctx)
        if root_ctx and isinstance(root_ctx.get("local_spans"), list):
            ended_at = datetime.now(UTC)
            started_at = ctx.get("local_started_at")
            if not isinstance(started_at, datetime):
                started_at = ended_at
            duration_ms = max(
                0.0, (ended_at - started_at).total_seconds() * 1000.0
            )
            local_metadata = ctx.get("local_metadata") if isinstance(ctx.get("local_metadata"), dict) else {}
            if ttft_ms is not None:
                local_metadata = {**local_metadata, "ttft_ms": ttft_ms}
            span_record: dict[str, Any] = {
                "id": ctx.get("local_span_id") or uuid.uuid4(),
                "parent_span_id": ctx.get("local_parent_span_id"),
                "type": str(ctx.get("local_span_type") or "SPAN"),
                "name": _truncate(str(ctx.get("local_name") or "span"), max_len=255),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "input": ctx.get("local_input"),
                "output": full_output,
                "model": effective_model,
                "level": local_level,
                "status_message": local_error_message,
                "input_tokens": (
                    int(normalized_usage.get("input"))
                    if isinstance(normalized_usage, dict)
                    and normalized_usage.get("input") is not None
                    else None
                ),
                "output_tokens": (
                    int(normalized_usage.get("output"))
                    if isinstance(normalized_usage, dict)
                    and normalized_usage.get("output") is not None
                    else None
                ),
                "usage_details": normalized_usage,
                "metadata": local_metadata,
                "time_to_first_token": ttft_ms,
                "tool_call_id": ctx.get("local_tool_call_id"),
                "tool_arguments": ctx.get("local_tool_arguments"),
                "stop_reason": (
                    merged_metadata.get("stop_reason")
                    if isinstance(merged_metadata, dict)
                    else None
                ),
                "position": int(ctx.get("local_position") or 0),
            }
            root_ctx["local_spans"].append(span_record)
            _checkpoint_background_run_span(root_ctx, span_record)


def end_span(
    ctx: dict | None,
    *,
    output: Any = None,
    usage: dict | None = None,
    model: str | None = None,
    error: str | BaseException | None = None,
) -> None:
    """Close a generic child span opened by ``start_iteration_span`` or
    any other ``start_*_span`` helper that doesn't have a dedicated exit.

    For LLM generations use :func:`end_generation`; for tool spans use
    :func:`end_tool_span`.  All three delegate to the same
    ``_close_observation`` core so behaviour stays consistent.

    Unlike ``end_trace`` this helper deliberately does NOT call
    ``set_trace_io`` on the trace — that side effect is reserved for the
    root span closer.
    """
    _close_observation(ctx, output=output, usage=usage, model=model, error=error)


def end_generation(
    gen_ctx: dict | None,
    *,
    output: Any = None,
    usage: dict | None = None,
    model: str | None = None,
    stop_reason: str | None = None,
    error: str | BaseException | None = None,
    level: str | None = None,
) -> None:
    """Close an LLM generation observation opened by
    :func:`start_llm_generation`, :func:`start_continuation_generation`, or
    :func:`start_activation_gate_generation`.

    ``stop_reason`` (e.g. ``"length"``, ``"tool_use"``, ``"end_turn"``) is
    persisted as metadata since Langfuse v4 has no first-class field for
    it on generations.  ``level`` (e.g. ``"WARNING"`` for truncated /
    empty responses) propagates to the observation.
    """
    extra: dict | None = {"stop_reason": stop_reason} if stop_reason else None
    _close_observation(
        gen_ctx,
        output=output,
        usage=usage,
        model=model,
        error=error,
        level=level,
        extra_metadata=extra,
    )


def end_tool_span(
    span_ctx: dict | None,
    *,
    output: Any = None,
    is_error: bool = False,
    error: str | BaseException | None = None,
    error_kind: str | None = None,
    level: str | None = None,
) -> None:
    """Close a tool execution span opened by :func:`start_tool_span`.

    When ``is_error=True`` and no explicit ``error`` is provided, the
    ``output`` is coerced into the error field (so the tool result string
    that already describes the failure also drives the span status).

    ``error_kind`` (e.g. ``"timeout"``, ``"human_input_requested"``,
    ``"exception"``) becomes filterable metadata so retry/timeout patterns
    can be queried in Langfuse.
    """
    effective_error = error
    if is_error and effective_error is None:
        effective_error = output if output is not None else "tool error"
    extra: dict | None = {"error_kind": error_kind} if error_kind else None
    _close_observation(
        span_ctx,
        output=None if effective_error else output,
        error=effective_error,
        level=level,
        extra_metadata=extra,
    )


class _GenerationHandle:
    """Mutable handle yielded by ``traced_generation``.

    Callers assign ``handle.response = await llm.chat(...)`` (or
    ``handle.content``/``handle.usage``/``handle.model`` directly for
    non-``ChatResponse`` shapes).  The context manager reads these on
    exit to populate the observation's output, usage, and model fields.
    """

    __slots__ = ("content", "model", "response", "usage")

    def __init__(self) -> None:
        self.response: Any = None
        self.content: Any = None
        self.usage: dict | None = None
        self.model: str | None = None


@contextlib.asynccontextmanager
async def traced_generation(
    parent_ctx: dict | None = None,
    *,
    name: str = "llm_call",
    model: str | None = None,
    messages: list | None = None,
    tools: list | None = None,
    system_prompt: str | None = None,
    metadata: dict | None = None,
):
    """Open a Langfuse generation observation around a single LLM call.

    Usage::

        async with traced_generation(name="my_call", model=resolved_model,
                                     messages=msgs) as gen:
            gen.response = await llm.chat(msgs, ...)

    The helper:
    - Opens the span via :func:`start_direct_generation` (parent resolved
      from the explicit ``parent_ctx`` or the contextvar).
    - Yields a :class:`_GenerationHandle` for the caller to fill in.
    - On normal exit, closes the span with output/usage/model from
      ``handle.response`` (or the explicit ``handle.content`` / ``handle.usage``
      / ``handle.model`` fields when set).
    - On exception, closes the span with ``error=exc`` and re-raises.
    - When Langfuse is disabled (``parent_ctx is None`` and no contextvar),
      ``start_direct_generation`` returns ``None`` and exit is a no-op.
    """
    ctx = start_direct_generation(
        parent_ctx,
        model=model,
        messages=messages,
        tools=tools,
        system_prompt=system_prompt,
        metadata=metadata,
        name=name,
    )
    handle = _GenerationHandle()
    err: BaseException | None = None
    try:
        yield handle
    except asyncio.CancelledError as exc:
        # Split from the generic ``except Exception`` below so
        # ``SystemExit`` / ``KeyboardInterrupt`` pass through
        # uninstrumented (CodeQL ``py/catch-base-exception`` convention
        # applied across every tracing close-path in the codebase —
        # process-terminating exceptions must not be intercepted by
        # tracing instrumentation, even when the catcher re-raises).
        err = exc
        raise
    except Exception as exc:
        err = exc
        raise
    finally:
        if ctx is not None:
            response = handle.response
            output = (
                handle.content
                if handle.content is not None
                else getattr(response, "content", None)
            )
            usage = (
                handle.usage
                if handle.usage is not None
                else getattr(response, "usage", None)
            )
            resolved_model = (
                handle.model
                or getattr(response, "model", None)
                or model
            )
            end_span(
                ctx,
                output=output,
                usage=usage,
                model=resolved_model,
                error=err,
            )
